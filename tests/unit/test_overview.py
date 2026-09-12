# -*- coding: utf-8 -*-
"""总控台数据层：行情取数、计划线提取、触发判定、卡片组装。

全部 hermetic：打桩行情接口、用临时目录放报告，不联网、不写用户的 data/。
"""

import io
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from _common import import_module


def hold_row(**kw):
    row = {"代码": "512890", "名称": "红利低波ETF", "是否ETF": True, "成本价": 1.206,
           "持有股数": 6100, "可用股数_可卖": 2100, "冻结股数_当日买入不可卖": 4000,
           "现价": 1.203, "持仓市值_元": 7338.3, "浮动盈亏_元": -21.3,
           "盈亏比例_pct": -0.249, "占总资金_pct": 14.68, "持股天数": 4}
    row.update(kw)
    return row


def watch_row(**kw):
    row = {"代码": "600967", "名称": "内蒙一机", "备注": "", "现价": 14.98,
           "涨跌幅_pct": 9.99, "在缓存": True}
    row.update(kw)
    return row


def em_row(code, market, price, chg=1.0, prev=10.0):
    return {"f12": code, "f13": market, "f14": "测试", "f2": price, "f3": chg, "f18": prev,
            "f15": price + 0.1, "f16": price - 0.1, "f17": prev, "f5": 1e6, "f6": 2e7,
            "f8": 1.2, "f10": 1.1, "f62": 3e6}


def write_report(tmp, plan=None, levels=None, trade_date="2026-09-11"):
    """写一份最小可用的报告 JSON，供计划线 / 卡片用例使用。"""
    path = os.path.join(tmp, "040458_post.json")
    payload = {"trade_date": trade_date, "generated_at": "2026-09-12T04:04:58",
               "标的": {"代码": "600967.SH", "名称": "内蒙一机"},
               "研判": {"json": {"方向": "偏多", "置信度": 55,
                                 "关键价位": levels if levels is not None else {
                                     "支撑": [{"价位": 14.05, "依据": "MA5"}],
                                     "压力": [{"价位": 15.04, "依据": "BOLL"}],
                                     "止损价": 12.19,
                                     "目标位": [{"价位": 15.22, "依据": "平台高"}]},
                                 "计划": plan if plan is not None else [
                                     {"动作": "观望", "价格区间": [14.98, 15.27], "优先级": 1},
                                     {"动作": "建仓", "价格区间": [13.4, 14.0], "优先级": 2,
                                      "股数": 200, "失效条件": "跌破 12.19"},
                                     {"动作": "减仓", "价格区间": [15.2, 15.4], "优先级": 3}]}}}
    with io.open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    return path


class Fixture(unittest.TestCase):
    """公共夹具：临时目录 + 报告构造（本身不含 test_* 方法，不会被当用例跑）。"""

    @classmethod
    def setUpClass(cls):
        cls.ov = import_module("overview")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="overview_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ov._LEVELS_CACHE.clear()
        self.ov.reset_alert_state()

    def _report(self, plan=None, levels=None, trade_date="2026-09-11"):
        return write_report(self.tmp, plan, levels, trade_date)


class TestSymbolMapping(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.q = import_module("quotes")

    def test_thscode_of(self):
        cases = {"600967": "600967.SH", "512890": "512890.SH", "002463": "002463.SZ",
                 "300563": "300563.SZ", "159915": "159915.SZ", "430047": "430047.BJ",
                 "000300.SH": "000300.SH", "110038": "110038.SH"}
        for raw, want in cases.items():
            self.assertEqual(self.q.thscode_of(raw), want, raw)
        self.assertIsNone(self.q.thscode_of("abc"))

    def test_tx_symbol_of(self):
        self.assertEqual(self.q.tx_symbol_of("600967"), "sh600967")
        self.assertEqual(self.q.tx_symbol_of("002463"), "sz002463")
        self.assertIsNone(self.q.tx_symbol_of("430047"))


class TestQuoteFetch(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.q = import_module("quotes")

    def setUp(self):
        self.q._QUOTE_CACHE["ts"] = 0.0
        self.q._QUOTE_CACHE["data"] = {}
        self.q._MINUTE_CACHE.clear()

    def test_batch_quotes_single_request(self):
        rows = [em_row("512890", 1, 1.21), em_row("600967", 1, 15.2)]
        with mock.patch.object(self.q.pan, "em_ulist", return_value=rows) as em:
            got, hints = self.q.fetch_quotes(["512890", "600967"], refresh=True)
        self.assertEqual(sorted(got), ["512890", "600967"])
        self.assertEqual(got["512890"]["价格"], 1.21)
        self.assertEqual(em.call_count, 1, "两个代码必须共用一次请求")
        self.assertEqual(hints, [])

    def test_quote_cache_within_ttl(self):
        rows = [em_row("512890", 1, 1.21)]
        with mock.patch.object(self.q.pan, "em_ulist", return_value=rows) as em:
            self.q.fetch_quotes(["512890"], refresh=True)
            self.q.fetch_quotes(["512890"])
            self.q.fetch_quotes(["512890"], refresh=True)
        self.assertEqual(em.call_count, 2)

    def test_quote_failure_degrades(self):
        with mock.patch.object(self.q.pan, "em_ulist", return_value=None):
            got, hints = self.q.fetch_quotes(["512890"], refresh=True)
        self.assertEqual(got, {})
        self.assertTrue(hints and "东财" in hints[0])

    def test_minutes_shape_and_cache(self):
        rows = [{"时间": "0930", "价格": 10.0, "分时均价": 10.0},
                {"时间": "0931", "价格": 10.1, "分时均价": 10.05}]
        with mock.patch.object(self.q.pan, "fetch_tx_minute",
                               return_value=(rows, 10.1, 10.05, None)) as tx:
            first = self.q.fetch_minutes("600967", refresh=True)
            second = self.q.fetch_minutes("600967")
        self.assertEqual(first["价格"], [10.0, 10.1])
        self.assertEqual(first["均价"], [10.0, 10.05])
        self.assertEqual(second["最新"], 10.1)
        self.assertEqual(tx.call_count, 1)

    def test_minutes_failure_is_cached(self):
        with mock.patch.object(self.q.pan, "fetch_tx_minute",
                               side_effect=RuntimeError("boom")) as tx:
            self.assertIsNone(self.q.fetch_minutes("600967", refresh=True))
            self.assertIsNone(self.q.fetch_minutes("600967"))
        self.assertEqual(tx.call_count, 1, "失败结果也要缓存，避免每档重试打接口")


class TestPlanLines(Fixture):

    def test_levels_extraction(self):
        path = self._report()
        lv = self.ov.report_levels(path)
        self.assertEqual(lv["买点"]["下沿"], 13.4)
        self.assertEqual(lv["买点"]["上沿"], 14.0)
        self.assertEqual(lv["买点"]["动作"], "建仓")
        self.assertEqual(lv["买点"]["股数"], 200)
        self.assertEqual(lv["减仓"]["下沿"], 15.2)
        self.assertEqual(lv["止损"], 12.19)
        self.assertEqual([g["价位"] for g in lv["目标"]], [15.22])
        self.assertEqual([g["价位"] for g in lv["支撑"]], [14.05])
        self.assertEqual(lv["报告交易日"], "2026-09-11")

    def test_levels_cache_follows_mtime(self):
        path = self._report()
        self.assertEqual(self.ov.report_levels(path)["止损"], 12.19)
        with mock.patch.object(self.ov.aiplan, "read_json",
                               side_effect=AssertionError("命中缓存时不该重复解析")):
            again = self.ov.report_levels(path)
        self.assertEqual(again["止损"], 12.19)
        self._report(levels={"止损价": 11.5, "目标位": [], "支撑": [], "压力": []})
        os.utime(path, (time.time() + 5, time.time() + 5))
        self.assertEqual(self.ov.report_levels(path)["止损"], 11.5)

    def test_trigger_marks(self):
        levels = {"买点": {"下沿": 13.4, "上沿": 14.0, "动作": "建仓", "股数": 200},
                  "止损": 12.19, "目标": [{"价位": 15.22, "依据": "平台高"}],
                  "支撑": [{"价位": 14.05}], "压力": [{"价位": 15.04}]}
        in_range = [m["类型"] for m in self.ov.trigger_marks(13.7, levels)]
        self.assertIn("已到买点", in_range)
        self.assertNotIn("已破止损", in_range)
        self.assertIn("已破止损", [m["类型"] for m in self.ov.trigger_marks(12.1, levels)])
        self.assertIn("已达目标", [m["类型"] for m in self.ov.trigger_marks(15.3, levels)])
        marks = [m["类型"] for m in self.ov.trigger_marks(16.0, levels)]
        self.assertIn("高于买点", marks)
        self.assertIn("已上破压力", marks)
        self.assertIn("已跌破支撑", [m["类型"] for m in self.ov.trigger_marks(13.0, levels)])
        self.assertEqual(self.ov.trigger_marks(None, levels), [])

    def test_only_new_triggers_alert_once(self):
        levels = {"买点": {"下沿": 13.4, "上沿": 14.0, "动作": "建仓"}, "止损": 12.19,
                  "目标": [], "支撑": [], "压力": []}
        marks = self.ov.trigger_marks(13.7, levels)
        first = self.ov._alerts_for("600967", "内蒙一机", marks, {"报告路径": "x.json"})
        self.assertEqual(len(first), 1)
        self.assertEqual(self.ov._alerts_for("600967", "内蒙一机", marks, {"报告路径": "x.json"}), [])
        self.ov._prune_alert_state("600967", [])
        back = self.ov._alerts_for("600967", "内蒙一机", marks, {"报告路径": "x.json"})
        self.assertEqual(len(back), 1, "回到未触发后再触发要重新提醒")


class TestBuildOverview(Fixture):

    def _fixtures(self, path):
        hold = {"持仓": [hold_row(), hold_row(代码="002463", 名称="沪电股份", 是否ETF=False,
                                              现价=128.25, 浮动盈亏_元=593.52)],
                "汇总": {"持仓市值_元": 32988.3, "标的数": 2, "持仓占比_pct": 65.98},
                "路径": "data/user/持仓数据.md"}
        watch = [watch_row(), watch_row(代码="300563", 名称="神宇股份", 现价=None, 在缓存=False)]
        reports = [{"json路径": path, "时间": "2026-09-12 04:04:58",
                    "摘要": {"标的代码": "600967.SH", "标的名称": "内蒙一机"}}]
        session = {"名称": "盘中（上午）", "是否交易日": True, "判定依据": "测试",
                   "现在": "2026-09-11 10:00:00"}
        return hold, watch, reports, session

    def test_local_mode_uses_local_prices(self):
        path = self._report()
        hold, watch, reports, session = self._fixtures(path)
        with mock.patch.object(self.ov, "load_holdings_bundle", return_value=hold), \
                mock.patch.object(self.ov, "load_watchlist", return_value=watch), \
                mock.patch.object(self.ov, "list_reports", return_value=reports), \
                mock.patch.object(self.ov, "session_of", return_value=session):
            data = self.ov.build_overview(refresh=False)
        self.assertEqual(data["时段"]["名称"], "盘中（上午）")
        self.assertEqual([c["现价"] for c in data["持仓"]], [1.203, 128.25])
        self.assertEqual(data["持仓"][0]["价格来源"], "持仓文件市价")
        self.assertIsNone(data["持仓"][0]["分时"])
        by_code = {c["代码"]: c for c in data["自选"]}
        self.assertEqual(by_code["600967"]["价格来源"], "stock3d 缓存")
        self.assertEqual(by_code["300563"]["价格来源"], "未抓数")
        self.assertIsNone(by_code["300563"]["现价"])
        self.assertTrue(data["刷新"]["提示"])
        self.assertEqual(data["路径"]["自选"], "data/user/自选股.md")

    def test_watch_marks_already_held(self):
        path = self._report()
        hold, _watch, reports, session = self._fixtures(path)
        watch = [watch_row(代码="512890", 名称="红利低波ETF", 现价=1.203)]
        with mock.patch.object(self.ov, "load_holdings_bundle", return_value=hold), \
                mock.patch.object(self.ov, "load_watchlist", return_value=watch), \
                mock.patch.object(self.ov, "list_reports", return_value=reports), \
                mock.patch.object(self.ov, "session_of", return_value=session):
            data = self.ov.build_overview(refresh=False)
        self.assertTrue(data["自选"][0]["已持仓"])

    def test_refresh_mode_sets_live_price_and_alerts_once(self):
        path = self._report()
        hold, watch, reports, session = self._fixtures(path)
        quote = {"600967": {"代码": "600967", "价格": 13.7, "涨跌幅_pct": -2.0, "昨收": 14.0}}
        minute = {"代码": "600967", "时间": ["0930"], "价格": [13.7], "均价": [13.7]}
        levels = self.ov.report_levels(path)
        with mock.patch.object(self.ov, "load_holdings_bundle", return_value=hold), \
                mock.patch.object(self.ov, "load_watchlist", return_value=watch), \
                mock.patch.object(self.ov, "list_reports", return_value=reports), \
                mock.patch.object(self.ov, "session_of", return_value=session), \
                mock.patch.object(self.ov, "report_levels", return_value=levels), \
                mock.patch.object(self.ov.quotes, "fetch_quotes", return_value=(quote, [])), \
                mock.patch.object(self.ov.quotes, "fetch_minutes", return_value=minute):
            live = self.ov.build_overview(refresh=True)
            again = self.ov.build_overview(refresh=True)
        card = {c["代码"]: c for c in live["自选"]}["600967"]
        self.assertEqual(card["现价"], 13.7)
        self.assertEqual(card["价格来源"], "东财实时行情")
        self.assertEqual(card["分时"]["价格"], [13.7])
        self.assertEqual(card["计划"]["买点"]["下沿"], 13.4)
        self.assertIn("已到买点", [m["类型"] for m in card["触发"]])
        self.assertEqual([a["代码"] for a in live["提醒"]], ["600967"])
        self.assertEqual([a for a in again["提醒"] if a["代码"] == "600967"], [],
                         "同一标记不能每档重复提醒")

    def test_queue_seeds_state_after_restart(self):
        """服务重启后：队列里已有的同一条提醒不能重发（价格变了才重新提醒）。"""
        path = self._report()
        hold, watch, reports, session = self._fixtures(path)
        quote = {"600967": {"代码": "600967", "价格": 13.7, "涨跌幅_pct": -2.0, "昨收": 14.0}}
        levels = self.ov.report_levels(path)
        queue_file = os.path.join(self.tmp, "alerts.jsonl")
        with mock.patch.object(self.ov.alerts_store, "ALERTS_PATH", queue_file), \
                mock.patch.object(self.ov, "load_holdings_bundle", return_value=hold), \
                mock.patch.object(self.ov, "load_watchlist", return_value=watch), \
                mock.patch.object(self.ov, "list_reports", return_value=reports), \
                mock.patch.object(self.ov, "session_of", return_value=session), \
                mock.patch.object(self.ov, "report_levels", return_value=levels), \
                mock.patch.object(self.ov.quotes, "fetch_quotes", return_value=(quote, [])), \
                mock.patch.object(self.ov.quotes, "fetch_minutes", return_value=None):
            first = self.ov.build_overview(refresh=True)
            self.assertEqual([a["代码"] for a in first["提醒"]], ["600967"])
            self.assertEqual(self.ov.alerts_store.count(), 1)
            self.ov.reset_alert_state()          # 模拟服务重启：内存状态清空
            again = self.ov.build_overview(refresh=True)
            self.assertEqual([a for a in again["提醒"] if a["代码"] == "600967"], [],
                             "重启后同一价位的提醒不该重复")
            self.assertEqual(self.ov.alerts_store.count(), 1, "也不该重复写入队列")

    def test_price_change_after_restart_alerts_again(self):
        """价格变了（新情况）仍然要提醒。"""
        path = self._report()
        hold, watch, reports, session = self._fixtures(path)
        levels = self.ov.report_levels(path)
        hold2 = dict(hold)
        hold2["持仓"] = [hold_row(), hold_row(代码="002463", 名称="沪电股份", 现价=128.25)]
        watch2 = [watch_row(代码="600967", 名称="内蒙一机", 现价=13.7)]
        queue_file = os.path.join(self.tmp, "alerts.jsonl")
        first_quote = {"600967": {"代码": "600967", "价格": 13.7, "昨收": 14.0}}
        second_quote = {"600967": {"代码": "600967", "价格": 12.10, "昨收": 14.0}}
        with mock.patch.object(self.ov.alerts_store, "ALERTS_PATH", queue_file), \
                mock.patch.object(self.ov, "load_holdings_bundle", return_value=hold2), \
                mock.patch.object(self.ov, "load_watchlist", return_value=watch2), \
                mock.patch.object(self.ov, "list_reports", return_value=reports), \
                mock.patch.object(self.ov, "session_of", return_value=session), \
                mock.patch.object(self.ov, "report_levels", return_value=levels), \
                mock.patch.object(self.ov.quotes, "fetch_minutes", return_value=None), \
                mock.patch.object(self.ov.quotes, "fetch_quotes", return_value=(first_quote, [])):
            self.ov.build_overview(refresh=True)
            self.ov.reset_alert_state()          # 重启
        with mock.patch.object(self.ov.alerts_store, "ALERTS_PATH", queue_file), \
                mock.patch.object(self.ov, "load_holdings_bundle", return_value=hold2), \
                mock.patch.object(self.ov, "load_watchlist", return_value=watch2), \
                mock.patch.object(self.ov, "list_reports", return_value=reports), \
                mock.patch.object(self.ov, "session_of", return_value=session), \
                mock.patch.object(self.ov, "report_levels", return_value=levels), \
                mock.patch.object(self.ov.quotes, "fetch_minutes", return_value=None), \
                mock.patch.object(self.ov.quotes, "fetch_quotes", return_value=(second_quote, [])):
            third = self.ov.build_overview(refresh=True)
        kinds = [a["类型"] for a in third["提醒"] if a["代码"] == "600967"]
        self.assertIn("已破止损", kinds, "跌到止损是新情况，必须提醒")


if __name__ == "__main__":
    unittest.main(verbosity=2)
