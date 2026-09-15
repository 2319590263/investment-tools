# -*- coding: utf-8 -*-
"""交易流（一条流 = 一笔流资金 + 多只标的，每只一种打法）：资金上限、打法、平均成本与盈亏、
达标/止损、接近带提醒去重、台账合并、页面组装。

全部 hermetic：流目录 / 设置 / 台账都指向临时文件，行情与提醒写入打桩，不碰用户数据。
"""

import io
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
        cls.fb = import_module("flowbook")
        cls.view = import_module("flowview")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="flow_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.flows = os.path.join(self.tmp, "flows")
        self.ledger = os.path.join(self.tmp, "交易台账.md")
        # 路径常量分别落在 flow（流文件 / 回收站）与 flowbook（设置 / 台账）里，两个都要打桩
        for mod in (self.f, self.fb):
            for name, value in (("FLOW_DIR", self.flows),
                                ("FLOW_SETTINGS", os.path.join(self.flows, "settings.json")),
                                ("TRASH_DIR", os.path.join(self.tmp, "trash")),
                                ("LEDGER_PATH", self.ledger)):
                if hasattr(mod, name):
                    p = mock.patch.object(mod, name, value)
                    p.start()
                    self.addCleanup(p.stop)
        self.f._FLOW_CACHE.clear()

    def flow(self, code="600967", name="内蒙一机", capital=30000, target_pct=15, loss_pct=8,
             alloc=None, style=None, **kw):
        kw.setdefault("account_total", 50000)
        doc, err = self.f.create_flow(capital=capital, target_pct=target_pct, loss_pct=loss_pct,
                                      code=code, name=name, alloc=alloc, style=style, **kw)
        self.assertIsNone(err, err)
        return doc

    def empty_flow(self, capital=30000, target_pct=15, loss_pct=8, **kw):
        doc, err = self.f.create_flow(capital=capital, target_pct=target_pct,
                                      loss_pct=loss_pct, **kw)
        self.assertIsNone(err, err)
        return doc

    def target(self, doc, code="600967"):
        node, err = self.f.find_target(doc, code)
        self.assertIsNone(err, err)
        return node

    def with_plan(self, node, level=None):
        node["计划"] = {"产物路径": "data/ai/track/x.json", "生成时间": "2026-09-14 15:20:00",
                        "适用交易日": "2026-09-15", "方向": "偏多", "置信度": 60,
                        "一句话结论": "回踩建仓", "关键价位": level or levels(),
                        "条目": [{"编号": 1, "动作": "建仓", "价格区间": [13.4, 14.0],
                                  "股数": 300, "无动作": False}]}
        return node


class TestCreate(Fixture):

    def test_validation(self):
        cases = [({"capital": 0}, "流资金"), ({"target_pct": 0}, "目标收益率"),
                 ({"loss_pct": -1}, "最大亏损")]
        for kw, want in cases:
            args = {"capital": 30000, "target_pct": 15, "loss_pct": 8, "account_total": 50000}
            args.update(kw)
            doc, err = self.f.create_flow(**args)
            self.assertIsNone(doc)
            self.assertIn(want, err)

    def test_account_total_limit(self):
        self.flow(capital=30000)
        doc, err = self.f.create_flow(capital=25000, target_pct=10, loss_pct=5,
                                      account_total=50000)
        self.assertIsNone(doc)
        self.assertIn("超过账户总资金", err)
        self.assertEqual(self.f.active_capital(), 30000.0)

    def test_flow_can_start_empty_then_add_targets(self):
        doc = self.empty_flow()
        self.assertEqual(doc["标的"], [])
        self.assertEqual(doc["状态"], "进行中")
        self.assertEqual(doc["汇总"]["剩余资金"], 30000.0)
        node, err = self.f.add_target(doc, "600967", "内蒙一机")
        self.assertIsNone(err, err)
        self.assertEqual(node["分配资金"], 30000.0, "分配资金默认取剩余")
        self.assertEqual(node["打法"], "短线", "打法默认短线")
        self.assertEqual(self.f.remaining(doc), 0.0)
        self.assertTrue(os.path.exists(self.f.flow_path(doc["流编号"])))

    def test_capital_sum_must_not_exceed_flow(self):
        doc = self.flow(code="600967", alloc=20000)
        node, err = self.f.add_target(doc, "300563", "神宇股份", alloc=20000)
        self.assertIsNone(node)
        self.assertIn("超过流资金", err)
        node, err = self.f.add_target(doc, "300563", "神宇股份")
        self.assertIsNone(err, err)
        self.assertEqual(node["分配资金"], 10000.0, "不填就取剩余")
        _out, err = self.f.set_target(doc, "300563", alloc=15000)
        self.assertIsNone(_out)
        self.assertIn("超过流资金", err)
        # 流资金不能小于已分配的钱
        _out, err = self.f.set_params(doc, capital=20000)
        self.assertIsNone(_out)
        self.assertIn("不能小于", err)
        doc, err = self.f.set_params(doc, capital=40000)
        self.assertIsNone(err, err)
        self.assertEqual(self.f.remaining(doc), 10000.0)
        self.assertEqual(self.f.allocated(doc), 30000.0)

    def test_one_stock_belongs_to_one_flow(self):
        first = self.flow(code="600967", name="内蒙一机", capital=20000, alloc=20000)
        doc, err = self.f.create_flow(capital=10000, target_pct=10, loss_pct=5, code="600967",
                                      name="内蒙一机", alloc=10000, account_total=50000)
        self.assertIsNone(doc)
        self.assertIn("只属于一条流", err)
        second = self.flow(code="300563", name="神宇股份", capital=10000, alloc=10000)
        node, err = self.f.add_target(second, "600967")
        self.assertIsNone(node)
        self.assertIn("只属于一条流", err)
        node, err = self.f.add_target(first, "600967")
        self.assertIsNone(node)
        self.assertIn("已经在这条流里", err)
        # 结束掉第一条流之后，同一只标的可以开新流
        first, err = self.f.close_flow(first, "人工结束")
        self.assertIsNone(err, err)
        self.assertEqual(first["状态"], "已结束")
        self.assertIsNone(self.f.flow_id_of_code("600967"))
        doc, err = self.f.create_flow(capital=10000, target_pct=10, loss_pct=5, code="600967",
                                      name="内蒙一机", alloc=10000, account_total=50000)
        self.assertIsNone(err, err)
        self.assertEqual(len(doc["标的"]), 1)

    def test_style_choices(self):
        doc = self.empty_flow()
        node, err = self.f.add_target(doc, "600967", "内蒙一机", style="乱写")
        self.assertIsNone(node)
        self.assertIn("打法", err)
        node, err = self.f.add_target(doc, "600967", "内蒙一机", style="超短线", alloc=10000)
        self.assertIsNone(err, err)
        self.assertEqual(node["打法"], "超短线")
        doc, err = self.f.set_target(doc, "600967", style="波段")
        self.assertIsNone(err, err)
        self.assertEqual(self.target(doc)["打法"], "波段")
        doc, err = self.f.set_target(doc, "600967", style="长线")
        self.assertIsNone(doc)
        self.assertIn("打法", err)
        self.assertEqual(self.f.STYLES, ("超短线", "短线", "波段"))

    def test_code_is_required_for_a_target(self):
        doc = self.empty_flow()
        node, err = self.f.add_target(doc, "")
        self.assertIsNone(node)
        self.assertIn("6 位", err)

    def test_target_amounts_and_flow_summary(self):
        doc = self.flow(code="600967", alloc=30000)
        node = self.target(doc)
        self.assertEqual(node["参数"]["目标盈利_元"], 4500.0)
        self.assertEqual(node["参数"]["最大亏损_元"], 2400.0)
        self.assertEqual(doc["汇总"]["目标盈利_元"], 4500.0)
        self.assertEqual(doc["汇总"]["最大亏损_元"], 2400.0)
        self.assertTrue(any(e["类型"] == "开流" for e in doc["事件"]))
        self.assertTrue(any(e["类型"] == "加标的" for e in doc["事件"]))

    def test_start_position_becomes_initial_fill(self):
        doc = self.empty_flow()
        node, err = self.f.add_target(doc, "600967", "内蒙一机", alloc=30000,
                                      start={"股数": 2000, "成本价": 13.5, "可用": 0})
        self.assertIsNone(err, err)
        self.assertEqual(len(node["成交"]), 1)
        fill = node["成交"][0]
        self.assertEqual(fill["来源"], "期初")
        self.assertEqual(fill["手续费"], 0.0)
        self.assertEqual(node["持仓"]["股数"], 2000.0)
        self.assertEqual(node["持仓"]["平均成本"], 13.5)
        self.assertIn("期初", node["期初"]["说明"])
        node, err = self.f.add_target(self.empty_flow(), "300563", start={"股数": 100})
        self.assertIsNone(node)
        self.assertIn("成本价", err)

    def test_initial_position_keeps_broker_available(self):
        """期初底仓不按 T+1 冻结（沿用持仓文件的可用）；当天新买的仍然不可卖。"""
        doc = self.empty_flow()
        node, err = self.f.add_target(doc, "600967", "内蒙一机", alloc=30000,
                                      start={"股数": 2000, "成本价": 13.5, "可用": 2000})
        self.assertIsNone(err, err)
        self.assertEqual(node["持仓"]["可用"], 2000.0)
        self.f.add_fill(node, "买入", 14.0, 1000, account=ACCOUNT)
        self.assertEqual(node["持仓"]["股数"], 3000.0)
        self.assertEqual(node["持仓"]["可用"], 2000.0, "当天买入的 1000 股 T+1 才可卖")

    def test_remove_target_frees_capital(self):
        doc = self.flow(code="600967", alloc=30000)
        node, err = self.f.add_target(doc, "300563", "神宇股份", alloc=0)
        self.assertIsNone(node)
        self.assertIn("已经分完", err)
        doc, err = self.f.set_target(doc, "600967", alloc=20000)
        self.assertIsNone(err, err)
        node, err = self.f.add_target(doc, "300563", "神宇股份", alloc=5000)
        self.assertIsNone(err, err)
        doc, err = self.f.remove_target(doc, "300563")
        self.assertIsNone(err, err)
        self.assertEqual(len(doc["标的"]), 1)
        self.assertEqual(self.f.remaining(doc), 10000.0)
        doc, err = self.f.remove_target(doc, "000000")
        self.assertIsNone(doc)
        self.assertIn("没有标的", err)


class TestPnl(Fixture):

    def test_avg_cost_and_realized(self):
        doc = self.flow()
        node = self.target(doc)
        self.f.add_fill(node, "买入", 13.9, 2000, account=ACCOUNT)
        fee1 = node["成交"][0]["手续费"]
        self.assertAlmostEqual(fee1, 6.95 + 0.28, places=2)
        self.assertAlmostEqual(node["持仓"]["平均成本"], (27800 + fee1) / 2000, places=4)
        self.f.add_fill(node, "买入", 14.2, 1000, account=ACCOUNT)
        self.assertEqual(node["持仓"]["股数"], 3000.0)
        avg = node["持仓"]["平均成本"]
        self.f.add_fill(node, "卖出", 15.0, 1000, account=ACCOUNT)
        sell = node["成交"][-1]
        self.assertAlmostEqual(node["盈亏"]["已实现_元"], 15000 - sell["手续费"] - avg * 1000,
                               places=1)
        self.assertEqual(node["持仓"]["股数"], 2000.0)
        self.f.recompute(node, account=ACCOUNT)
        self.assertEqual(node["持仓"]["股数"], 2000.0)

    def test_available_excludes_today_buys(self):
        doc = self.flow()
        node = self.target(doc)
        self.f.add_fill(node, "买入", 13.9, 2000, account=ACCOUNT)
        self.assertEqual(node["持仓"]["可用"], 0.0, "当日买入 T+1 才可卖")

    def test_over_sell_is_warned(self):
        doc = self.flow()
        node = self.target(doc)
        self.f.add_fill(node, "买入", 13.9, 1000, account=ACCOUNT)
        self.f.add_fill(node, "卖出", 15.0, 5000, account=ACCOUNT)
        self.assertEqual(node["持仓"]["股数"], 0.0)
        self.assertTrue(any("超过流内持仓" in t for t in node["提示"]))

    def test_target_and_loss_boundaries(self):
        doc = self.flow()
        node = self.target(doc)
        self.f.add_fill(node, "买入", 13.9, 2000, account=ACCOUNT)
        avg = node["持仓"]["平均成本"]
        pnl = self.f.apply_price(node, avg + 4500 / 2000)
        self.assertTrue(pnl["达标"], "等于阈值也应触发达标")
        self.assertFalse(pnl["触及最大亏损"])
        pnl = self.f.apply_price(node, avg - 2400 / 2000)
        self.assertTrue(pnl["触及最大亏损"], "等于阈值也应触发最大亏损")
        pnl = self.f.apply_price(node, avg)
        self.assertFalse(pnl["达标"])
        self.assertFalse(pnl["触及最大亏损"])

    def test_settle_when_flat(self):
        doc = self.flow()
        node = self.target(doc)
        self.f.add_fill(node, "买入", 13.9, 2000, account=ACCOUNT)
        avg = node["持仓"]["平均成本"]
        self.f.add_fill(node, "卖出", avg + 10, 2000, account=ACCOUNT)
        self.assertEqual(node["持仓"]["股数"], 0.0)
        self.assertIn(node["状态"], ("已达标", "未达标清仓", "已止损"))
        self.assertTrue(node["结束时间"])
        # 唯一一只标的结算完 → 整条流自动结束
        self.f.save_flow(doc)
        doc, _err = self.f.load_flow(doc["流编号"])
        self.assertEqual(doc["状态"], "已结束")

    def test_flow_summary_aggregates_targets(self):
        doc = self.empty_flow(capital=30000, target_pct=10, loss_pct=10)
        self.f.add_target(doc, "600967", "内蒙一机", alloc=10000)
        self.f.add_target(doc, "300563", "神宇股份", alloc=10000, style="波段")
        for code in ("600967", "300563"):
            node = self.target(doc, code)
            self.f.add_fill(node, "买入", 10.0, 1000, account={"佣金费率_pct": 0, "佣金最低_元": 0,
                                                             "印花税率_pct": 0, "过户费率_pct": 0})
            self.f.apply_price(node, 11.0)
        self.f.save_flow(doc)
        doc, _err = self.f.load_flow(doc["流编号"])
        # 每只标的的收益率分母是它自己的分配资金
        self.assertAlmostEqual(self.target(doc, "600967")["盈亏"]["收益率_pct"], 10.0, places=2)
        sum_row = doc["汇总"]
        self.assertAlmostEqual(sum_row["合计_元"], 2000.0, places=2)
        self.assertAlmostEqual(sum_row["收益率_pct"], 2000 / 30000 * 100, places=2)
        self.assertAlmostEqual(sum_row["进度_pct"], 2000 / 3000 * 100, places=2)
        self.assertFalse(sum_row["达标"])
        self.assertEqual(sum_row["标的数"], 2)
        self.assertEqual(sum_row["分配合计"], 20000.0)
        self.assertEqual(sum_row["剩余资金"], 10000.0)


class TestMarks(Fixture):

    def test_near_and_hit_marks(self):
        node = self.with_plan(self.target(self.flow()))
        self.f.apply_price(node, 14.3)                  # 离买点上沿 14.0 约 2%
        self.assertEqual(self.f.marks(node), [], "太远不该提醒")
        self.f.apply_price(node, 14.02)
        near = [m["类型"] for m in self.f.marks(node)]
        self.assertIn("接近买点", near)
        self.f.apply_price(node, 13.7)
        hit = [m["类型"] for m in self.f.marks(node, band_pct=0.2)]
        self.assertIn("触及买点", hit)
        self.f.apply_price(node, 13.0)
        stop = [m for m in self.f.marks(node, band_pct=0.2) if m["类型"] == "触及止损"]
        self.assertTrue(stop, "现价跌破止损要报触及止损")
        self.assertEqual(stop[0]["级别"], "bad")
        self.f.apply_price(node, 15.22)
        ok = [m for m in self.f.marks(node, band_pct=0.2) if m["类型"] == "触及止盈点"]
        self.assertTrue(ok, "到止盈点要报触及止盈")
        self.assertEqual(ok[0]["级别"], "ok")

    def test_alert_dedup_within_day(self):
        doc = self.flow()
        node = self.with_plan(self.target(doc))
        self.f.apply_price(node, 14.0)
        fresh = self.f.due_alerts(node)
        self.assertTrue(fresh)
        self.f.mark_alerted(node, fresh)
        self.assertEqual(self.f.due_alerts(node), [], "同一天同一价位只提醒一次")
        items = self.f.alert_items(node, fresh, doc["流编号"])
        self.assertEqual(items[0]["代码"], "600967")
        self.assertEqual(items[0]["流编号"], doc["流编号"])
        self.assertIn("点位类型", items[0])
        # 换了日期要重新提醒
        # 不能用「今天」「明天」这类跟真实日历挂钩的日期：真实日期正好走到那天时，
        # 去重状态里的日期与查询日期相同，会误判成「已提醒过」。用一个远期日期。
        self.assertTrue(self.f.due_alerts(node, today="2099-01-04"))

    def test_next_action(self):
        node = self.with_plan(self.target(self.flow()))
        self.f.apply_price(node, 14.02)
        self.assertIn("接近买点", self.f.next_action(node))
        node["计划"]["关键价位"] = {}
        self.assertIn("建仓", self.f.next_action(node))
        node["计划"]["条目"] = []
        self.assertIn("还没有计划", self.f.next_action(node))


class TestLedger(Fixture):

    def write_ledger(self, rows):
        header = ("# 交易台账（同花顺自动同步，按委托去重）\n\n"
                  "| 日期 | 时间 | 委托序号 | 方向 | 代码 | 名称 | 成交价格 | 成交数量 | 成交金额 | 交易市场 |\n"
                  "|---|---|---|---|---|---|---|---|---|---|\n")
        body = "".join("| %s |\n" % " | ".join(str(x) for x in r) for r in rows)
        with io.open(self.ledger, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(header + body)

    def test_sync_merges_only_since_start(self):
        doc = self.flow()                       # 加入日 = 今天
        node = self.target(doc)
        node["加入日"] = "2026-09-14"
        self.write_ledger([
            ["2026-09-10", "10:00:00", "1001", "买入", "600967", "内蒙一机", "13.50", "1000", "13500", "SH"],
            ["2026-09-14", "10:31:02", "1002", "买入", "600967", "内蒙一机", "13.90", "2000", "27800", "SH"],
            ["2026-09-14", "11:02:00", "1003", "买入", "300563", "神宇股份", "20.00", "100", "2000", "SZ"],
        ])
        added = self.f.sync_ledger(node, account=ACCOUNT)
        self.assertEqual(added, 1, "只并入该标的且不早于加入日的成交")
        self.assertEqual(node["成交"][-1]["来源"], "台账")
        self.assertEqual(node["台账"]["未并入"], 1)
        self.assertEqual(self.f.sync_ledger(node, account=ACCOUNT), 0, "同步是幂等的")

    def test_manual_first_then_sync_skips_duplicate(self):
        doc = self.flow()
        node = self.target(doc)
        self.f.add_fill(node, "买入", 13.9, 2000, account=ACCOUNT)
        self.write_ledger([["2026-09-14", "10:31:02", "", "买入", "600967", "内蒙一机",
                            "13.90", "2000", "27800", "SH"]])
        self.assertEqual(self.f.sync_ledger(node, account=ACCOUNT), 0)
        self.assertEqual(len(node["成交"]), 1)

    def test_delete_fill_recomputes(self):
        doc = self.flow()
        node = self.target(doc)
        self.f.add_fill(node, "买入", 13.9, 2000, account=ACCOUNT)
        self.f.add_fill(node, "买入", 14.2, 1000, account=ACCOUNT)
        node, err = self.f.delete_fill(node, node["成交"][-1]["序号"], account=ACCOUNT)
        self.assertIsNone(err)
        self.assertEqual(node["持仓"]["股数"], 2000.0)

    def test_initial_fill_is_protected(self):
        doc = self.empty_flow()
        node, _err = self.f.add_target(doc, "600967", "内蒙一机", alloc=30000,
                                       start={"股数": 2000, "成本价": 13.5, "可用": 0})
        out, err = self.f.delete_fill(node, 1, account=ACCOUNT)
        self.assertIsNone(out)
        self.assertIsNotNone(err)
        self.assertIn("期初", err)
        out, err = self.f.delete_fill(node, 99, account=ACCOUNT)
        self.assertIsNone(out)
        self.assertIn("没有序号", err)


class TestPlanWindow(Fixture):

    def _session(self, name, trading, now):
        return {"名称": name, "是否交易日": trading, "判定依据": "test", "现在": now}

    def test_after_close_window(self):
        node = self.with_plan(self.target(self.flow()))
        node["计划"]["生成时间"] = "2026-09-14 09:05:00"      # 早上生成的，盘后要提醒重算
        with mock.patch.object(self.fb, "session_of",
                               lambda now=None: self._session("盘中（上午）", True,
                                                              "2026-09-14 10:00:00")):
            self.assertFalse(self.f.plan_stale(node)["待重算"])
        with mock.patch.object(self.fb, "session_of",
                               lambda now=None: self._session("盘后", True, "2026-09-14 15:30:00")):
            stale = self.f.plan_stale(node)
            self.assertTrue(stale["待重算"], "盘后且今天没重算过 → 提示重算")
        node["计划"]["生成时间"] = "2026-09-14 15:20:00"
        with mock.patch.object(self.fb, "session_of",
                               lambda now=None: self._session("盘后", True, "2026-09-14 15:30:00")):
            self.assertFalse(self.f.plan_stale(node)["待重算"], "过点后已重算就不再提醒")
        with mock.patch.object(self.fb, "session_of",
                               lambda now=None: self._session("非交易日", False, "2026-09-13 16:00:00")):
            self.assertFalse(self.f.plan_stale(node)["待重算"])

    def test_settings_clamp(self):
        out = self.f.save_settings({"接近带_pct": 99, "轮询间隔_秒": 3})
        self.assertEqual(out["接近带_pct"], 5.0)
        self.assertEqual(out["轮询间隔_秒"], 5)
        out = self.f.save_settings({"盘后重算时间": "abc"})
        self.assertEqual(out["盘后重算时间"], "15:10")
        self.assertFalse(out["打开页面自动补跑"])

    def test_exec_record_shape(self):
        doc = self.flow()
        node = self.target(doc)
        self.assertIsNone(self.f.exec_record(node))
        self.f.add_fill(node, "买入", 13.9, 2000, account=ACCOUNT)
        rec = self.f.exec_record(node)
        self.assertEqual(rec["条目"][0]["执行状态"], "已执行")
        self.assertEqual(rec["条目"][0]["成交股数"], 2000.0)


class TestViewData(Fixture):

    def test_card_and_check_pass(self):
        doc = self.flow()
        node = self.with_plan(self.target(doc))
        self.f.add_fill(node, "买入", 13.9, 2000, account=ACCOUNT)
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
        self.assertEqual(card["标的"][0]["名称"], "内蒙一机")
        self.assertEqual(card["标的"][0]["打法"], "短线")
        self.assertEqual(card["标的"][0]["盈亏"]["现价"], 14.0)
        self.assertIn("下一步", card["标的"][0])
        self.assertTrue(card["标的"][0]["到价"])
        self.assertAlmostEqual(card["汇总"]["合计_元"], (14.0 - 13.9) * 2000 - 7.23, places=1)
        again, _fresh2 = self.view.check_pass(
            [self.f.load_flow(doc["流编号"])[0]],
            {"600967": {"价格": 14.0}}, refresh=True)
        self.assertEqual(_fresh2, [], "第二次取值不再重复提醒")

    def test_overview_and_console_block(self):
        doc = self.empty_flow(capital=30000)
        self.f.add_target(doc, "600967", "内蒙一机", alloc=20000)
        self.f.add_target(doc, "300563", "神宇股份", alloc=10000, style="波段")
        self.f.save_flow(doc)
        out = self.view.overview({"600967": {"价格": 14.0}, "300563": {"价格": 20.0}}, refresh=True)
        self.assertEqual(out["计数"]["进行中"], 1)
        self.assertEqual(out["计数"]["标的"], 2)
        self.assertEqual(out["资金"]["已占用"], 30000.0)
        self.assertEqual(out["资金"]["分配合计"], 30000.0)
        self.assertIn("设置", out)
        block = self.view.console_block({"600967": {"价格": 14.0}}, refresh=True)
        self.assertEqual(block["计数"]["显示"], 2, "总控台一行 = 一只标的")
        self.assertEqual(block["计数"]["标的"], 2)
        self.assertEqual(block["行"][0]["打法"], "短线")
        self.assertEqual(block["行"][1]["打法"], "波段")
        self.assertIn("接近带", block["口径"])

    def test_daily_plan_window_is_per_target(self):
        doc = self.empty_flow(capital=30000)
        self.f.add_target(doc, "600967", "内蒙一机", alloc=20000)
        self.f.add_target(doc, "300563", "神宇股份", alloc=10000)
        node = self.with_plan(self.target(doc, "600967"))
        node["计划"]["生成时间"] = "2026-09-15 15:20:00"     # 今天盘后已经重算过
        self.f.save_flow(doc)
        session = {"名称": "盘后", "是否交易日": True, "判定依据": "test",
                   "现在": "2026-09-15 15:30:00"}
        with mock.patch.object(self.fb, "session_of", lambda now=None: session):
            out = self.view.overview({}, refresh=False)
        pending = out["待重算"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["流编号"], doc["流编号"])
        self.assertEqual(pending[0]["代码"], ["300563"], "没出过计划的那只才待重算")
        self.assertIn("300563", pending[0]["标签"])

    def test_detail_has_all_targets(self):
        doc = self.empty_flow(capital=30000)
        self.f.add_target(doc, "600967", "内蒙一机", alloc=20000)
        self.f.add_target(doc, "300563", "神宇股份", alloc=10000)
        node = self.target(doc, "600967")
        self.f.add_fill(node, "买入", 13.9, 2000, account=ACCOUNT)
        self.f.save_flow(doc)
        with mock.patch.object(self.view, "lots_of", lambda n: {"买点": "3 手"}):
            data, err = self.view.detail(doc["流编号"], quote_map={"600967": {"价格": 14.0}})
        self.assertIsNone(err)
        self.assertEqual(data["选中"], "600967")
        self.assertEqual(len(data["标的"]), 2)
        self.assertEqual(data["标的"][0]["成交"][0]["方向"], "买入")
        self.assertEqual(data["标的"][0]["手数"]["买点"], "3 手")
        self.assertEqual(data["卡"]["状态"], "进行中")
        by_code = {t["代码"]: t for t in data["标的"]}
        self.assertEqual(by_code["300563"]["成交"], [])
        self.assertIn("超短线", data["打法口径"])

    def test_delete_flow_moves_to_trash(self):
        doc = self.flow()
        info, err = self.f.delete_flow(doc["流编号"])
        self.assertIsNone(err)
        self.assertTrue(info["移入"])
        self.assertFalse(os.path.exists(self.f.flow_path(doc["流编号"])))

    def test_old_single_target_file_is_upgraded(self):
        """老结构（<代码>-<日期>.json）读进来要自动折成 v2 的标的清单。"""
        old = {"流编号": "600967-20260914", "标的": {"代码": "600967", "名称": "内蒙一机",
                                                  "是否ETF": False},
               "状态": "进行中", "创建时间": "2026-09-14 09:00:00", "起始日": "2026-09-14",
               "参数": {"本流资金": 20000, "目标收益率_pct": 12, "最大亏损_pct": 6,
                        "最大加仓次数": 2},
               "成交": [], "持仓": {}, "盈亏": {}, "计划": {}, "体检": [], "事件": [],
               "提醒状态": {}, "台账": {}, "提示": []}
        doc = self.f.normalize(old)
        self.assertEqual(doc["版本"], 2)
        self.assertEqual(doc["参数"]["流资金"], 20000)
        self.assertEqual(len(doc["标的"]), 1)
        node = doc["标的"][0]
        self.assertEqual(node["代码"], "600967")
        self.assertEqual(node["分配资金"], 20000)
        self.assertEqual(node["打法"], "短线")
        self.assertEqual(node["参数"]["目标盈利_元"], 2400.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
