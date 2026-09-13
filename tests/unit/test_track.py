# -*- coding: utf-8 -*-
"""标的跟踪：适用交易日、执行记录、事实包、机械参考、清单读写、页面组装。

全部 hermetic：TRACK_DIR / 清单路径都指向临时目录，行情与日历打桩，不联网、不写用户数据。
"""

import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from _common import import_module


def plan_payload(plan=None, levels=None, apply_date="2026-09-15", trade_date="2026-09-14"):
    return {
        "tool": "webui-track", "phase": "track", "generated_at": "2026-09-14T15:30:00",
        "trade_date": trade_date, "适用交易日": apply_date,
        "标的": {"代码": "600967.SH", "名称": "内蒙一机", "类型": "stock"},
        "研判": {"called": True, "ok": True, "model": "m", "usage": {"输入": 100, "输出": 200},
                 "cost": {"人民币_估算": 0.01},
                 "json": {"方向": "偏多", "置信度": 60, "一句话结论": "回踩建仓",
                          "关键价位": levels if levels is not None else {
                              "支撑": [{"价位": 13.9, "依据": "MA5"}],
                              "压力": [{"价位": 15.04, "依据": "BOLL"}],
                              "止损价": 13.4, "目标位": [{"价位": 15.22, "依据": "平台高"}]},
                          "计划": plan if plan is not None else [
                              {"动作": "建仓", "触发条件": "回踩企稳", "价格区间": [13.4, 14.0],
                               "股数": 200, "金额_元": 2740, "失效条件": "跌破 13.4", "优先级": 1},
                              {"动作": "减仓", "触发条件": "冲高滞涨", "价格区间": [15.2, 15.4],
                               "股数": 100, "失效条件": "回落到 15 以下", "优先级": 2}]}},
        "复核": {"called": False}, "机械校验": [], "现价": 14.2, "降级": [],
    }


class Fixture(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.track = import_module("track")
        cls.view = import_module("trackview")
        cls.store = import_module("store")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="track_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.track._PLAN_CACHE.clear()
        self.track._EXEC_CACHE.clear()
        for obj, name, value in ((self.track, "TRACK_DIR", self.tmp),
                                 (self.track, "TRASH_DIR", os.path.join(self.tmp, "trash"))):
            p = mock.patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)

    def write_plan(self, day="20260914", stamp="153000", payload=None, code="600967",
                   apply_date="2026-09-15"):
        js, md = self.track.plan_paths(day, stamp, code)
        os.makedirs(os.path.dirname(js), exist_ok=True)
        body = payload if payload is not None else plan_payload(apply_date=apply_date)
        with io.open(js, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(body, ensure_ascii=False))
        with io.open(md, "w", encoding="utf-8") as fh:
            fh.write("# 计划\n")
        return js


class TestSession(Fixture):

    def _session(self, name, trading, now="2026-09-14 15:30:00"):
        return {"名称": name, "是否交易日": trading, "判定依据": "test", "现在": now}

    def test_next_trade_date(self):
        days = {"2026-09-11", "2026-09-14", "2026-09-15"}
        self.assertEqual(self.track.next_trade_date(days, datetime.date(2026, 9, 11)), "2026-09-14")
        self.assertEqual(self.track.next_trade_date(days, datetime.date(2026, 9, 15)), None)
        self.assertEqual(self.track.next_trade_date(None, datetime.date(2026, 9, 15)), None)

    def test_after_close_goes_to_next_trading_day(self):
        with mock.patch.object(self.track, "session_of", lambda now=None: self._session("盘后", True)), \
                mock.patch.object(self.track, "calendar_days",
                                  lambda: {"2026-09-14", "2026-09-15"}):
            out = self.track.apply_trade_date(datetime.datetime(2026, 9, 14, 15, 30))
        self.assertEqual(out["适用交易日"], "2026-09-15")
        self.assertTrue(out["盘后"])
        self.assertIn("顺延", out["口径"])

    def test_intraday_uses_today(self):
        with mock.patch.object(self.track, "session_of", lambda now=None: self._session("盘中（上午）", True)), \
                mock.patch.object(self.track, "calendar_days", lambda: {"2026-09-14"}):
            out = self.track.apply_trade_date(datetime.datetime(2026, 9, 14, 10, 0))
        self.assertEqual(out["适用交易日"], "2026-09-14")
        self.assertFalse(out["盘后"])

    def test_calendar_missing_falls_back_to_weekday(self):
        """周六、无日历时：下一个交易日按下周一算，并把口径写清楚。"""
        with mock.patch.object(self.track, "session_of", lambda now=None: self._session("非交易日", False)), \
                mock.patch.object(self.track, "calendar_days", lambda: None):
            out = self.track.apply_trade_date(datetime.datetime(2026, 9, 12, 10, 0))
        self.assertEqual(out["适用交易日"], "2026-09-14")
        self.assertIn("跳过周末", out["口径"])

    def test_stale_calendar_still_trades_today(self):
        """日历只覆盖到上一天时，工作日盘前仍按“今天”出计划（否则会把计划错指到次日）。"""
        with mock.patch.object(self.track, "session_of", lambda now=None: self._session("非交易日", False)), \
                mock.patch.object(self.track, "calendar_days", lambda: {"2026-09-10", "2026-09-11"}):
            out = self.track.apply_trade_date(datetime.datetime(2026, 9, 14, 8, 30))
        self.assertEqual(out["适用交易日"], "2026-09-14")
        self.assertIn("未覆盖今天", out["口径"])


class TestPlans(Fixture):

    def test_plan_items_and_listing(self):
        self.write_plan()
        items = self.track.list_plans("600967")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["适用交易日"], "2026-09-15")
        self.assertEqual(items[0]["方向"], "偏多")
        self.assertEqual(items[0]["计划条数"], 2)
        self.assertFalse(items[0]["执行"]["是否录入"])
        doc, path = self.track.latest_plan("600967")
        self.assertEqual(doc["标的"]["名称"], "内蒙一机")
        self.assertTrue(path.endswith("_track.json"))
        self.assertEqual([r["编号"] for r in self.track.plan_items(doc)], [1, 2])

    def test_key_levels_share_one_rule(self):
        js = self.write_plan()
        levels = self.track.report_levels(js)
        self.assertEqual(levels["买点"]["下沿"], 13.4)
        self.assertEqual(levels["减仓"]["动作"], "减仓")
        self.assertEqual(levels["止损"], 13.4)
        self.assertEqual([g["价位"] for g in levels["目标"]], [15.22])

    def test_save_plan_and_pointer(self):
        path = self.track.save_plan(plan_payload(), "# md\n")
        self.assertTrue(os.path.exists(path))
        self.assertTrue(os.path.exists(self.track.exec_path_of(path).replace("_track_exec.json", "_track.md")))
        pointer = self.track.latest_pointer_of("600967")
        self.assertTrue(os.path.exists(pointer))
        with io.open(pointer, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["产物路径"], self.track.rel(path))


class TestExec(Fixture):

    def test_roundtrip_and_defaults(self):
        js = self.write_plan()
        self.assertFalse(self.track.exec_summary(None)["是否录入"])
        doc, err = self.track.save_exec(js, [{"编号": 1, "执行状态": "已执行", "成交价": 13.55,
                                              "成交股数": 200, "备注": "回踩成交"}], "已建仓")
        self.assertIsNone(err)
        self.assertEqual(doc["代码"], "600967")
        self.assertEqual(doc["条目"][0]["执行状态"], "已执行")
        self.assertEqual(doc["条目"][1]["执行状态"], "未执行", "没提交的条目按未执行补全")
        again, path = self.track.load_exec(js)
        self.assertEqual(again["总体备注"], "已建仓")
        summary = self.track.exec_summary(again)
        self.assertTrue(summary["是否录入"])
        self.assertEqual(summary["计数"]["已执行"], 1)
        self.assertEqual(summary["计数"]["未执行"], 1)

    def test_bad_state_and_unknown_item_rejected(self):
        js = self.write_plan()
        doc, err = self.track.save_exec(js, [{"编号": 1, "执行状态": "也许吧"}])
        self.assertIsNone(doc)
        self.assertIn("执行状态", err)
        doc, err = self.track.save_exec(js, [{"编号": 9, "执行状态": "已执行"}])
        self.assertIsNone(doc)
        self.assertIn("不在这份计划里", err)

    def test_noop_item_defaults_to_void(self):
        payload = plan_payload(plan=[{"动作": "观望", "价格区间": [1.0, 2.0], "优先级": 1}])
        js = self.write_plan(payload=payload)
        doc, err = self.track.save_exec(js, [])
        self.assertIsNone(err)
        self.assertEqual(doc["条目"][0]["执行状态"], "已作废")

    def test_outside_dir_rejected(self):
        outside = os.path.join(self.tmp, "..", "nope_track.json")
        doc, err = self.track.save_exec(outside, [])
        self.assertIsNone(doc)
        self.assertIsNotNone(err)


class TestReferencePrev(Fixture):

    def test_needs_exec_only_across_trading_days(self):
        self.write_plan(apply_date="2026-09-14")
        prev = self.track.reference_prev("600967", "2026-09-15")
        self.assertEqual(prev["来源"], "跟踪产物")
        self.assertTrue(prev["需要执行记录"])
        js = prev["路径"]
        self.track.save_exec(js, [{"编号": 1, "执行状态": "未执行"}])
        self.assertFalse(self.track.reference_prev("600967", "2026-09-15")["需要执行记录"],
                         "填过执行记录后就不再拦")
        self.assertFalse(self.track.reference_prev("600967", "2026-09-14")["需要执行记录"],
                         "同一适用交易日重跑不要求执行记录")

    def test_falls_back_to_manual_report(self):
        doc, path = plan_payload(apply_date=None), os.path.join(self.tmp, "r.json")
        with mock.patch.object(self.track, "_newest_report_for", lambda code: (doc, path)):
            prev = self.track.reference_prev("600967", "2026-09-15")
        self.assertEqual(prev["来源"], "手工研判报告")
        self.assertFalse(prev["需要执行记录"])
        self.assertIn("手工研判报告", prev["说明"])

    def test_first_plan(self):
        with mock.patch.object(self.track, "_newest_report_for", lambda code: (None, None)):
            prev = self.track.reference_prev("600967", "2026-09-15")
        self.assertIsNone(prev["来源"])
        self.assertIn("首份", prev["说明"])


class TestMechanical(Fixture):

    def test_reference_rows(self):
        doc = plan_payload()
        mech = self.track.mech_reference(doc, {"最高": 14.05, "最低": 13.3})
        self.assertEqual(mech["计划"][0]["状态"], "区间已触及")
        self.assertEqual(mech["计划"][1]["状态"], "区间未触及")
        self.assertTrue(any(r["类型"] == "止损" for r in mech["关键价位"]),
                        "最低 13.3 已跌破止损 13.4")
        self.assertIn("不代表用户是否成交", mech["说明"])

    def test_without_quote_is_undecided(self):
        mech = self.track.mech_reference(plan_payload(), None)
        self.assertTrue(all(r["状态"] == "未判定" for r in mech["计划"]))


class TestFactpack(Fixture):

    def data(self):
        prev = self.track.reference_prev("600967", "2026-09-15")
        quote = {"价格": 14.2, "涨跌幅_pct": 1.2, "来源": "东财实时行情", "口径": "盘中实时价",
                 "时间": "10:00:00", "最高": 14.3, "最低": 13.9, "昨收": 14.0, "今开": 14.05,
                 "量比": 1.2, "换手率_pct": 2.1, "成交额": 3.4e7, "主力净流入": 1.2e6, "实时": True}
        return {"标的": {"代码": "600967.SH", "名称": "内蒙一机"},
                "适用交易日": "2026-09-15", "交易日口径": "test",
                "账户": {"总资金": 50000, "单票仓位上限_pct": 60, "单笔最大亏损_pct": 2,
                         "最低现金比例_pct": 10},
                "持仓": {"是否持仓": True, "持有股数": 200, "可用股数_可卖": 0, "成本价": 13.55,
                         "现价": 14.2, "持仓市值_元": 2840, "占总资金_pct": 5.7,
                         "浮动盈亏_元": 130},
                "实盘": self.track.quote_brief(None, quote, None),
                "上一份计划": prev, "机械参考": self.track.mech_reference(prev["文档"], quote),
                "背景": {"个股量价与形态": {"均线与分位": {"last": 14.2}},
                         "大盘": {"上涨占比_pct": 55.0}, "板块": {"行业领涨": [{"名称": "军工"}]}}}

    def test_sections_and_authority_note(self):
        self.write_plan(apply_date="2026-09-14")
        prev_path = self.track.list_plans("600967")[0]["json路径"]
        self.track.save_exec(prev_path, [{"编号": 1, "执行状态": "已执行", "成交价": 13.55,
                                          "成交股数": 200}], "回踩成交")
        fact = self.track.factpack_sections(self.data())
        titles = [s["标题"] for s in fact["章节"]]
        self.assertIn("用户录入的执行情况（权威口径）", titles)
        self.assertIn("机械参考（只读，仅用于核对）", titles)
        self.assertIn("实际：已执行", fact["文本"])
        self.assertIn("回踩成交", fact["文本"])
        self.assertEqual(fact["裁剪"], [])

    def test_trim_order_drops_background_first(self):
        data = self.data()
        cap = len(self.track.factpack_sections(data)["文本"]) - 1
        fact = self.track.factpack_sections(data, cap)
        self.assertEqual(fact["裁剪"], ["板块"])
        self.assertIn("用户录入的执行情况（权威口径）", [s["标题"] for s in fact["章节"]])
        tiny = self.track.factpack_sections(data, 900)
        self.assertEqual(tiny["裁剪"], ["板块", "大盘", "个股量价与形态"])
        self.assertIn("上一份计划（原文）", [s["标题"] for s in tiny["章节"]])


class TestMarkdown(Fixture):

    def test_markdown_covers_plan_and_exec(self):
        self.write_plan(apply_date="2026-09-14")
        prev_path = self.track.list_plans("600967")[0]["json路径"]
        self.track.save_exec(prev_path, [{"编号": 1, "执行状态": "部分执行", "成交价": 13.9,
                                          "成交股数": 100}])
        payload = plan_payload()
        payload["上次计划"] = self.track.reference_prev("600967", "2026-09-15")
        payload["执行记录摘要"] = payload["上次计划"]["执行摘要"]
        payload["机械参考"] = self.track.mech_reference(payload["上次计划"]["文档"], None)
        payload["实时"] = self.track.quote_brief(None, {"价格": 14.2}, None)
        md = self.track.render_md(payload)
        self.assertIn("# 标的跟踪", md)
        self.assertIn("## 3 本次交易计划", md)
        self.assertIn("实际：部分执行", md)
        self.assertIn("止损价：13.40", md)


class TestChecklist(Fixture):

    def test_store_add_remove_import(self):
        board = os.path.join(self.tmp, "跟踪标的.md")
        watch = os.path.join(self.tmp, "自选股.md")
        with mock.patch.object(self.store, "TRACKLIST_PATH", board), \
                mock.patch.object(self.store, "WATCHLIST_PATH", watch):
            self.store.watchlist_add("600967", "内蒙一机")
            self.store.watchlist_add("300563", "神宇股份", "荐股")
            self.assertEqual(self.store.load_tracklist(), [])
            added, items = self.store.tracklist_import_watchlist()
            self.assertEqual(added, 2)
            self.assertEqual([it["代码"] for it in items], ["600967", "300563"])
            self.assertEqual(self.store.tracklist_import_watchlist()[0], 0, "重复导入不该新增")
            self.store.tracklist_add("002463", "沪电股份")
            self.assertIn("002463", [it["代码"] for it in self.store.load_tracklist()])
            items, err = self.store.tracklist_remove("002463")
            self.assertIsNone(err)
            self.assertNotIn("002463", [it["代码"] for it in items])
            items, err = self.store.tracklist_remove("002463")
            self.assertIsNone(items)
            self.assertIn("跟踪清单", err)

    def test_import_creates_file_when_missing(self):
        board = os.path.join(self.tmp, "子目录", "跟踪标的.md")
        watch = os.path.join(self.tmp, "自选股.md")
        with mock.patch.object(self.store, "TRACKLIST_PATH", board), \
                mock.patch.object(self.store, "WATCHLIST_PATH", watch):
            self.store.watchlist_add("600967", "内蒙一机")
            added, items = self.store.tracklist_import_watchlist()
            self.assertEqual(added, 1)
            self.assertTrue(os.path.exists(board))
            with io.open(board, encoding="utf-8") as fh:
                self.assertIn("| 600967 |", fh.read())


class TestViewData(Fixture):

    def _apply(self):
        return {"适用交易日": "2026-09-15", "口径": "test 口径",
                "时段": {"名称": "盘后", "是否交易日": True, "判定依据": "test",
                         "现在": "2026-09-14 15:30:00"}, "现在": "2026-09-14 15:30:00",
                "盘后": True}

    def _patch(self, listed=None):
        patches = [mock.patch.object(self.view, "apply_trade_date", self._apply),
                   mock.patch.object(self.view, "load_tracklist",
                                     lambda: listed if listed is not None else [
                                         {"代码": "600967", "名称": "内蒙一机", "备注": "",
                                          "现价": 14.2, "涨跌幅_pct": 1.2, "在缓存": True}]),
                   mock.patch.object(self.view.quotes, "fetch_quotes", lambda codes, refresh=False: ({}, [])),
                   mock.patch.object(self.view, "load_holdings_bundle",
                                     lambda: {"持仓": [{"代码": "600967", "可用股数_可卖": 0,
                                                        "持有股数": 200}]})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_overview_flags_pending_then_done(self):
        self._patch()
        out = self.view.build_overview(refresh=False)
        self.assertEqual(out["适用交易日"], "2026-09-15")
        self.assertEqual(out["待生成"], ["600967"])
        self.assertEqual(out["清单"][0]["现价"], 14.2)
        self.assertEqual(out["清单"][0]["价格来源"], "stock3d 缓存（本次没取实时价）")
        self.write_plan(apply_date="2026-09-15")
        out2 = self.view.build_overview(refresh=False)
        self.assertEqual(out2["待生成"], [])
        self.assertEqual(out2["清单"][0]["方向"], "偏多")
        self.assertEqual(out2["清单"][0]["历史条数"], 1)

    def test_overview_marks_missing_exec(self):
        self.write_plan(apply_date="2026-09-14")
        self._patch()
        out = self.view.build_overview(refresh=False)
        self.assertEqual(out["待录入执行"], ["600967"])
        self.assertTrue(out["清单"][0]["需要执行记录"])

    def test_detail_carries_levels_lots_and_history(self):
        self.write_plan(apply_date="2026-09-14")
        self._patch()
        detail, err = self.view.build_detail("600967", refresh=False)
        self.assertIsNone(err)
        self.assertEqual(detail["代码"], "600967")
        self.assertEqual(detail["最新"]["关键价位"]["止损"], 13.4)
        self.assertEqual(detail["最新"]["手数"]["止盈"], "2 手")
        self.assertEqual(len(detail["历史"]), 1)
        self.assertTrue(detail["需录入执行记录"])
        self.assertEqual(detail["机械参考"]["计划"][0]["状态"], "未判定")

    def test_detail_rejects_bad_code(self):
        self._patch()
        detail, err = self.view.build_detail("abc", refresh=False)
        self.assertIsNone(detail)
        self.assertIn("6 位", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
