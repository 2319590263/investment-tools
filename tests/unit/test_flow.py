# -*- coding: utf-8 -*-
"""交易流：平均成本与盈亏、达标/止损、接近带提醒去重、开流校验、台账合并、页面组装。

全部 hermetic：流目录 / 设置 / 台账都指向临时文件，行情与提醒写入打桩，不碰用户数据。
"""

import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from _common import import_module

ACCOUNT = {"总资金": 50000, "佣金费率_pct": 0.025, "佣金最低_元": 5,
           "印花税率_pct": 0.05, "过户费率_pct": 0.001, "单票仓位上限_pct": 60,
           "单笔最大亏损_pct": 2, "最低现金比例_pct": 10}


def levels(buy=(13.4, 14.0), sell=(15.2, 15.4), stop=13.0, goals=(15.22, 16.0)):
    return {"买点": {"下沿": buy[0], "上沿": buy[1], "动作": "建仓", "股数": 300},
            "减仓": {"下沿": sell[0], "上沿": sell[1], "动作": "减仓", "股数": 200},
            "止损": stop,
            "目标": [{"价位": v, "依据": "平台"} for v in goals],
            "支撑": [], "压力": [],
            "报告路径": "x.json", "报告交易日": "2026-09-11"}


class Fixture(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.f = import_module("flow")
        cls.view = import_module("flowview")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="flow_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.flows = os.path.join(self.tmp, "flows")
        self.ledger = os.path.join(self.tmp, "交易台账.md")
        for obj, name, value in ((self.f, "FLOW_DIR", self.flows),
                                 (self.f, "FLOW_SETTINGS", os.path.join(self.flows, "settings.json")),
                                 (self.f, "TRASH_DIR", os.path.join(self.tmp, "trash")),
                                 (self.f, "LEDGER_PATH", self.ledger)):
            p = mock.patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.f._FLOW_CACHE.clear()

    def flow(self, **kw):
        kw.setdefault("capital", 30000)
        kw.setdefault("target_pct", 15)
        kw.setdefault("loss_pct", 8)
        kw.setdefault("account_total", 50000)
        doc, err = self.f.create_flow(kw.pop("code", "600967"),
                                      kw.pop("name", "内蒙一机"), **kw)
        self.assertIsNone(err, err)
        return doc

    def with_plan(self, doc, level=None):
        doc["计划"] = {"产物路径": "data/ai/track/x.json", "生成时间": "2026-09-14 15:20:00",
                       "适用交易日": "2026-09-15", "方向": "偏多", "置信度": 60,
                       "一句话结论": "回踩建仓", "关键价位": level or levels(),
                       "条目": [{"编号": 1, "动作": "建仓", "价格区间": [13.4, 14.0],
                                 "股数": 300, "无动作": False}]}
        return doc


class TestCreate(Fixture):

    def test_validation(self):
        cases = [({}, "6 位"), ({"capital": 0}, "本流资金"),
                 ({"target_pct": 0}, "目标收益率"), ({"loss_pct": -1}, "最大亏损")]
        for kw, want in cases:
            args = {"capital": 10000, "target_pct": 10, "loss_pct": 5, "account_total": 50000}
            args.update(kw)
            code = "600967" if kw else ""
            doc, err = self.f.create_flow(code, "内蒙一机", **args)
            self.assertIsNone(doc)
            self.assertIn(want, err)

    def test_duplicate_and_capital_limit(self):
        self.flow()
        doc, err = self.f.create_flow("600967", "内蒙一机", 1000, 10, 5, account_total=50000)
        self.assertIsNone(doc)
        self.assertTrue("已经开过" in err or "进行中的流" in err, err)
        doc, err = self.f.create_flow("300563", "神宇股份", 25000, 10, 5, account_total=50000)
        self.assertIsNone(doc)
        self.assertIn("超过账户总资金", err)
        self.assertEqual(self.f.active_capital(), 30000.0)

    def test_target_amounts_and_events(self):
        doc = self.flow()
        self.assertEqual(doc["参数"]["目标盈利_元"], 4500.0)
        self.assertEqual(doc["参数"]["最大亏损_元"], 2400.0)
        self.assertEqual(doc["状态"], "进行中")
        self.assertTrue(any(e["类型"] == "开流" for e in doc["事件"]))
        self.assertTrue(os.path.exists(self.f.flow_path(doc["流编号"])))

    def test_start_position_becomes_initial_fill(self):
        doc = self.flow(start={"股数": 2000, "成本价": 13.5, "可用": 0})
        self.assertEqual(len(doc["成交"]), 1)
        fill = doc["成交"][0]
        self.assertEqual(fill["来源"], "期初")
        self.assertEqual(fill["手续费"], 0.0)
        self.assertEqual(doc["持仓"]["股数"], 2000.0)
        self.assertEqual(doc["持仓"]["平均成本"], 13.5)
        self.assertIn("期初", doc["期初"]["说明"])


class TestPnl(Fixture):

    def test_avg_cost_and_realized(self):
        doc = self.flow()
        self.f.add_fill(doc, "买入", 13.9, 2000, account=ACCOUNT)
        fee1 = doc["成交"][0]["手续费"]
        self.assertAlmostEqual(fee1, 6.95 + 0.28, places=2)
        self.assertAlmostEqual(doc["持仓"]["平均成本"], (27800 + fee1) / 2000, places=4)
        self.f.add_fill(doc, "买入", 14.2, 1000, account=ACCOUNT)
        self.assertEqual(doc["持仓"]["股数"], 3000.0)
        avg = doc["持仓"]["平均成本"]
        self.f.add_fill(doc, "卖出", 15.0, 1000, account=ACCOUNT)
        sell = doc["成交"][-1]
        self.assertAlmostEqual(doc["盈亏"]["已实现_元"], 15000 - sell["手续费"] - avg * 1000,
                               places=1)
        self.assertEqual(doc["持仓"]["股数"], 2000.0)
        self.f.recompute(doc, account=ACCOUNT)
        self.assertEqual(doc["持仓"]["股数"], 2000.0)

    def test_available_excludes_today_buys(self):
        doc = self.flow()
        self.f.add_fill(doc, "买入", 13.9, 2000, account=ACCOUNT)
        self.assertEqual(doc["持仓"]["可用"], 0.0, "当日买入 T+1 才可卖")

    def test_over_sell_is_warned(self):
        doc = self.flow()
        self.f.add_fill(doc, "买入", 13.9, 1000, account=ACCOUNT)
        self.f.add_fill(doc, "卖出", 15.0, 5000, account=ACCOUNT)
        self.assertEqual(doc["持仓"]["股数"], 0.0)
        self.assertTrue(any("超过流内持仓" in t for t in doc["提示"]))

    def test_target_and_loss_boundaries(self):
        doc = self.flow()
        self.f.add_fill(doc, "买入", 13.9, 2000, account=ACCOUNT)
        avg = doc["持仓"]["平均成本"]
        pnl = self.f.apply_price(doc, avg + 4500 / 2000)
        self.assertTrue(pnl["达标"], "等于阈值也应触发达标")
        self.assertFalse(pnl["触及最大亏损"])
        pnl = self.f.apply_price(doc, avg - 2400 / 2000)
        self.assertTrue(pnl["触及最大亏损"], "等于阈值也应触发最大亏损")
        pnl = self.f.apply_price(doc, avg)
        self.assertFalse(pnl["达标"])
        self.assertFalse(pnl["触及最大亏损"])

    def test_settle_when_flat(self):
        doc = self.flow()
        self.f.add_fill(doc, "买入", 13.9, 2000, account=ACCOUNT)
        avg = doc["持仓"]["平均成本"]
        self.f.add_fill(doc, "卖出", avg + 10, 2000, account=ACCOUNT)
        self.assertEqual(doc["持仓"]["股数"], 0.0)
        self.assertIn(doc["状态"], ("已达标", "未达标清仓", "已止损"))
        self.assertTrue(doc["结束时间"])


class TestMarks(Fixture):

    def test_near_and_hit_marks(self):
        doc = self.with_plan(self.flow())
        self.f.apply_price(doc, 14.3)                  # 离买点上沿 14.0 约 2%
        self.assertEqual(self.f.marks(doc), [], "太远不该提醒")
        self.f.apply_price(doc, 14.02)
        near = [m["类型"] for m in self.f.marks(doc)]
        self.assertIn("接近买点", near)
        self.f.apply_price(doc, 13.7)
        hit = [m["类型"] for m in self.f.marks(doc, band_pct=0.2)]
        self.assertIn("触及买点", hit)
        self.f.apply_price(doc, 13.0)
        stop = [m for m in self.f.marks(doc, band_pct=0.2) if m["类型"] == "触及止损"]
        self.assertTrue(stop, "现价跌破止损要报触及止损")
        self.assertEqual(stop[0]["级别"], "bad")
        self.f.apply_price(doc, 15.22)
        ok = [m for m in self.f.marks(doc, band_pct=0.2) if m["类型"] == "触及止盈点"]
        self.assertTrue(ok, "到止盈点要报触及止盈")
        self.assertEqual(ok[0]["级别"], "ok")

    def test_alert_dedup_within_day(self):
        doc = self.with_plan(self.flow())
        self.f.apply_price(doc, 14.0)
        fresh = self.f.due_alerts(doc)
        self.assertTrue(fresh)
        self.f.mark_alerted(doc, fresh)
        self.assertEqual(self.f.due_alerts(doc), [], "同一天同一价位只提醒一次")
        items = self.f.alert_items(doc, fresh)
        self.assertEqual(items[0]["代码"], "600967")
        self.assertEqual(items[0]["流编号"], doc["流编号"])
        self.assertIn("点位类型", items[0])
        # 换了日期要重新提醒
        self.assertTrue(self.f.due_alerts(doc, today="2026-09-15"))

    def test_next_action(self):
        doc = self.with_plan(self.flow())
        self.f.apply_price(doc, 14.02)
        self.assertIn("接近买点", self.f.next_action(doc))
        doc["计划"]["关键价位"] = {}
        self.assertIn("建仓", self.f.next_action(doc))
        doc["计划"]["条目"] = []
        self.assertIn("还没有计划", self.f.next_action(doc))


class TestLedger(Fixture):

    def write_ledger(self, rows):
        header = ("# 交易台账（同花顺自动同步，按委托去重）\n\n"
                  "| 日期 | 时间 | 委托序号 | 方向 | 代码 | 名称 | 成交价格 | 成交数量 | 成交金额 | 交易市场 |\n"
                  "|---|---|---|---|---|---|---|---|---|---|\n")
        body = "".join("| %s |\n" % " | ".join(str(x) for x in r) for r in rows)
        with io.open(self.ledger, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(header + body)

    def test_sync_merges_only_since_start(self):
        doc = self.flow()
        self.write_ledger([
            ["2026-09-10", "10:00:00", "1001", "买入", "600967", "内蒙一机", "13.50", "1000", "13500", "SH"],
            ["2026-09-14", "10:31:02", "1002", "买入", "600967", "内蒙一机", "13.90", "2000", "27800", "SH"],
            ["2026-09-14", "11:02:00", "1003", "买入", "300563", "神宇股份", "20.00", "100", "2000", "SZ"],
        ])
        added = self.f.sync_ledger(doc, account=ACCOUNT)
        self.assertEqual(added, 1, "只并入该标的且不早于开流日的成交")
        self.assertEqual(doc["成交"][-1]["来源"], "台账")
        self.assertEqual(doc["台账"]["未并入"], 1)
        self.assertEqual(self.f.sync_ledger(doc, account=ACCOUNT), 0, "同步是幂等的")

    def test_manual_first_then_sync_skips_duplicate(self):
        doc = self.flow()
        self.f.add_fill(doc, "买入", 13.9, 2000, account=ACCOUNT)
        self.write_ledger([["2026-09-14", "10:31:02", "", "买入", "600967", "内蒙一机",
                            "13.90", "2000", "27800", "SH"]])
        self.assertEqual(self.f.sync_ledger(doc, account=ACCOUNT), 0)
        self.assertEqual(len(doc["成交"]), 1)

    def test_delete_fill_recomputes(self):
        doc = self.flow()
        self.f.add_fill(doc, "买入", 13.9, 2000, account=ACCOUNT)
        self.f.add_fill(doc, "买入", 14.2, 1000, account=ACCOUNT)
        doc, err = self.f.delete_fill(doc, doc["成交"][-1]["序号"], account=ACCOUNT)
        self.assertIsNone(err)
        self.assertEqual(doc["持仓"]["股数"], 2000.0)

    def test_initial_fill_is_protected(self):
        doc = self.flow(start={"股数": 2000, "成本价": 13.5, "可用": 0})
        out, err = self.f.delete_fill(doc, 1, account=ACCOUNT)
        self.assertIsNone(out)
        self.assertIsNotNone(err)
        self.assertIn("期初", err)
        out, err = self.f.delete_fill(doc, 99, account=ACCOUNT)
        self.assertIsNone(out)
        self.assertIn("没有序号", err)


class TestPlanWindow(Fixture):

    def _session(self, name, trading, now):
        return {"名称": name, "是否交易日": trading, "判定依据": "test", "现在": now}

    def test_after_close_window(self):
        doc = self.with_plan(self.flow())
        doc["计划"]["生成时间"] = "2026-09-14 09:05:00"      # 早上生成的，盘后要提醒重算
        with mock.patch.object(self.f, "session_of",
                               lambda now=None: self._session("盘中（上午）", True,
                                                              "2026-09-14 10:00:00")):
            self.assertFalse(self.f.plan_stale(doc)["待重算"])
        with mock.patch.object(self.f, "session_of",
                               lambda now=None: self._session("盘后", True, "2026-09-14 15:30:00")):
            stale = self.f.plan_stale(doc)
            self.assertTrue(stale["待重算"], "盘后且今天没重算过 → 提示重算")
        doc["计划"]["生成时间"] = "2026-09-14 15:20:00"
        with mock.patch.object(self.f, "session_of",
                               lambda now=None: self._session("盘后", True, "2026-09-14 15:30:00")):
            self.assertFalse(self.f.plan_stale(doc)["待重算"], "过点后已重算就不再提醒")
        with mock.patch.object(self.f, "session_of",
                               lambda now=None: self._session("非交易日", False, "2026-09-13 16:00:00")):
            self.assertFalse(self.f.plan_stale(doc)["待重算"])

    def test_settings_clamp(self):
        out = self.f.save_settings({"接近带_pct": 99, "轮询间隔_秒": 3})
        self.assertEqual(out["接近带_pct"], 5.0)
        self.assertEqual(out["轮询间隔_秒"], 5)
        out = self.f.save_settings({"盘后重算时间": "abc"})
        self.assertEqual(out["盘后重算时间"], "15:10")
        self.assertFalse(out["打开页面自动补跑"])

    def test_exec_record_shape(self):
        doc = self.flow()
        self.assertIsNone(self.f.exec_record(doc))
        self.f.add_fill(doc, "买入", 13.9, 2000, account=ACCOUNT)
        rec = self.f.exec_record(doc)
        self.assertEqual(rec["条目"][0]["执行状态"], "已执行")
        self.assertEqual(rec["条目"][0]["成交股数"], 2000.0)


class TestViewData(Fixture):

    def test_card_and_check_pass(self):
        doc = self.with_plan(self.flow())
        self.f.add_fill(doc, "买入", 13.9, 2000, account=ACCOUNT)
        self.f.save_flow(doc)
        sent = []
        with mock.patch.object(self.view.alerts_store, "append", lambda items: sent.extend(items)):
            rows, fresh = self.view.check_pass(
                [self.f.load_flow(doc["流编号"])[0]],
                {"600967": {"价格": 14.0, "涨跌幅_pct": 1.0}},
                refresh=True)
        self.assertTrue(fresh, "触及买点要写提醒")
        self.assertEqual(sent[0]["流编号"], doc["流编号"])
        card = rows[0]
        self.assertEqual(card["标的"]["名称"], "内蒙一机")
        self.assertEqual(card["盈亏"]["现价"], 14.0)
        self.assertIn("下一步", card)
        self.assertTrue(card["到价"])
        again, _fresh2 = self.view.check_pass(
            [self.f.load_flow(doc["流编号"])[0]],
            {"600967": {"价格": 14.0}}, refresh=True)
        self.assertEqual(_fresh2, [], "第二次取值不再重复提醒")

    def test_overview_and_console_block(self):
        doc = self.with_plan(self.flow())
        self.f.save_flow(doc)
        out = self.view.overview({"600967": {"价格": 14.0}}, refresh=True)
        self.assertEqual(out["计数"]["进行中"], 1)
        self.assertEqual(out["资金"]["已占用"], 30000.0)
        self.assertIn("设置", out)
        block = self.view.console_block({"600967": {"价格": 14.0}}, refresh=True)
        self.assertEqual(block["计数"]["显示"], 1)
        self.assertIn("接近带", block["口径"])

    def test_detail_has_handed_levels(self):
        doc = self.with_plan(self.flow())
        self.f.add_fill(doc, "买入", 13.9, 2000, account=ACCOUNT)
        self.f.save_flow(doc)
        with mock.patch.object(self.view, "lots_of", lambda code, lv: {"买点": "3 手"}):
            data, err = self.view.detail(doc["流编号"], quote_map={"600967": {"价格": 14.0}})
        self.assertIsNone(err)
        self.assertEqual(data["成交"][0]["方向"], "买入")
        self.assertEqual(data["手数"]["买点"], "3 手")
        self.assertEqual(data["卡"]["状态"], "进行中")

    def test_delete_flow_moves_to_trash(self):
        doc = self.flow()
        info, err = self.f.delete_flow(doc["流编号"])
        self.assertIsNone(err)
        self.assertTrue(info["移入"])
        self.assertFalse(os.path.exists(self.f.flow_path(doc["流编号"])))
