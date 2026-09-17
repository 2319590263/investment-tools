# -*- coding: utf-8 -*-
"""大盘评分取数层（mktdata）：取数失败留错误、缓存命中不重取、本地历史窗口口径。

全部 hermetic：HTTP 与磁盘都打桩，不联网、不写盘。
"""

import unittest
from unittest import mock

from _common import import_module


def bars(n=40, close=3000.0, amount=8e11):
    return [{"date": "2026-09-%02d" % (1 + i % 28), "open": close, "close": close,
             "high": close + 1, "low": close - 1, "vol": 1e8, "额": amount} for i in range(n)]


class TestEvidence(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.md = import_module("mktdata")

    def test_ev_shape(self):
        e = self.md.ev(1.5, "来源A", "2026-09-17", "口径B")
        self.assertEqual(e["值"], 1.5)
        self.assertEqual(e["来源"], "来源A")
        self.assertIsNone(e["错误"])
        self.assertIsNone(self.md.ev(None, "s")["值"])


class TestCache(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.md = import_module("mktdata")

    def test_read_write_round_trip(self):
        with mock.patch.object(self.md, "atomic_write") as w, \
                mock.patch.object(self.md.os, "makedirs"), \
                mock.patch.object(self.md, "_read", return_value={"x": 1}):
            self.md._write("data/cache/mkt/20260918/a.json", {"a": 1})
            self.assertTrue(w.called)

    def test_index_bars_cache_hit_no_http(self):
        with mock.patch.object(self.md, "_read", return_value={"bars": bars(30), "有成交额": True}), \
                mock.patch.object(self.md.mechdata, "_kline_http") as http:
            out = self.md.index_bars("1.000001", 40)
        self.assertEqual(len(out["bars"]), 30)
        self.assertFalse(http.called)

    def test_index_bars_needs_amount_refetch(self):
        """缓存里没有成交额（腾讯兜底）→ need_amount=True 时必须重取。"""
        stale = {"bars": bars(30, amount=None), "有成交额": False}
        with mock.patch.object(self.md, "_read", return_value=stale), \
                mock.patch.object(self.md, "_write", side_effect=lambda p, o: o), \
                mock.patch.object(self.md.mechdata, "_kline_http",
                                  return_value=bars(40)) as http:
            out = self.md.index_bars("1.000001", 40, need_amount=True)
        self.assertTrue(http.called)
        self.assertTrue(out["有成交额"])

    def test_index_bars_http_failure_marks_empty(self):
        with mock.patch.object(self.md, "_read", return_value=None), \
                mock.patch.object(self.md.mechdata, "_kline_http", return_value=[]), \
                mock.patch.object(self.md, "http_json", return_value=None), \
                mock.patch.object(self.md.mechdata, "INDEX_TX_SYMBOL", {}):
            out = self.md.index_bars("9.999999", 40, klt="103")
        self.assertEqual(out["bars"], [])
        self.assertFalse(out["有成交额"])


class TestPercentile(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.md = import_module("mktdata")

    def test_pe_percentile(self):
        series = [["2026-01-%02d" % (i + 1), float(i)] for i in range(100)]
        with mock.patch.object(self.md, "csindex_pe_series", return_value=series):
            out = self.md.pe_percentile("000300")
        self.assertEqual(out["值"], 99.0)
        self.assertEqual(out["分位"], 100.0)
        self.assertEqual(out["序列条数"], 100)

    def test_pe_percentile_no_data(self):
        with mock.patch.object(self.md, "csindex_pe_series", return_value=[]):
            out = self.md.pe_percentile("000300")
        self.assertIsNone(out["值"])
        self.assertTrue(out["错误"])

    def test_series_chunks_by_year(self):
        """按年分段抓：每次请求区间不超过一年，且结果按日期排序去重。"""
        calls = []

        def fake(url, params=None, headers=None, timeout=15, tries=2):
            calls.append(params["startDate"] + "~" + params["endDate"])
            return {"data": [{"tradeDate": "20260917", "peg": 14.0}]}

        with mock.patch.object(self.md, "http_json", side_effect=fake), \
                mock.patch.object(self.md, "_read", return_value=None), \
                mock.patch.object(self.md, "_write"), \
                mock.patch.object(self.md.time, "sleep"):
            series = self.md.csindex_pe_series("000300", years=3)
        self.assertTrue(calls)
        self.assertEqual(series, [["20260917", 14.0]])


class TestHistoryWindows(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.md = import_module("mktdata")

    def test_window_mean_needs_full_days(self):
        hist = {"2026-09-%02d" % d: {"成交额_亿": 10000 + d} for d in range(10, 18)}
        val, n = self.md.window_mean(hist, "成交额_亿", 5, "2026-09-17")
        self.assertEqual(n, 5)
        self.assertEqual(val, round(sum(10013 + i for i in range(5)) / 5.0, 2))
        val, n = self.md.window_mean({"2026-09-17": {"成交额_亿": 1}}, "成交额_亿", 5,
                                     "2026-09-17")
        self.assertIsNone(val)
        self.assertEqual(n, 1)

    def test_window_sum_ratio(self):
        hist = {"2026-09-%02d" % d: {"上涨家数": 3000, "下跌家数": 2000}
                for d in range(10, 15)}
        val, n = self.md.window_sum_ratio(hist, 5, "2026-09-14")
        self.assertEqual(n, 5)
        self.assertEqual(val, 1.5)
        val, n = self.md.window_sum_ratio(hist, 5, "2026-09-12")
        self.assertIsNone(val)

    def test_index_amount5(self):
        sh, sz = bars(10, amount=8e11), bars(10, amount=9e11)
        val, days = self.md.index_amount5(sh, sz)
        self.assertEqual(days, 5)
        self.assertEqual(val, round((8e11 + 9e11) / 1e8, 2))
        val, days = self.md.index_amount5(bars(3), bars(3))
        self.assertIsNone(val)
        self.assertEqual(days, 0)

    def test_midprice_change_needs_20_days(self):
        hist = {"2026-08-%02d" % d: 7.0 + d / 100.0 for d in range(1, 21)}
        val, n = self.md.midprice_change(hist, 20)
        self.assertEqual(n, 20)
        self.assertLess(val, 0)          # USDCNY 上行 = 人民币贬值 → 升值幅度为负
        val, n = self.md.midprice_change({"2026-09-17": 7.0}, 20)
        self.assertIsNone(val)
        self.assertEqual(n, 1)

    def test_record_snapshot_writes_breadth(self):
        pandoc = {"trade_date": "2026-09-17",
                  "data": {"sentiment": {"广度": {"上涨家数": 4170, "下跌家数": 1189,
                                               "平盘家数": 192, "涨停家数_阈值口径": 97,
                                               "跌停家数_阈值口径": 7, "ST涨停家数_阈值口径": 6,
                                               "ST跌停家数_阈值口径": 2,
                                               "两市成交额_亿": 18525.36}}}}
        with mock.patch.object(self.md, "load_daily_history", return_value={}), \
                mock.patch.object(self.md, "save_daily_history", side_effect=lambda h: h):
            hist = self.md.record_snapshot(pandoc)
        row = hist["2026-09-17"]
        self.assertEqual(row["涨停_非ST"], 91)         # 97 - 6（不含 ST）
        self.assertEqual(row["跌停_非ST"], 5)
        self.assertEqual(row["成交额_亿"], 18525.36)


class TestFullScanPaths(unittest.TestCase):
    """全量模式的两条重路径（全市场汇总、宽度样本）用桩数据验一遍，不联网。"""

    @classmethod
    def setUpClass(cls):
        cls.md = import_module("mktdata")

    def test_market_summary_sums_and_caches(self):
        pages = {1: ([{"f12": "600000", "f21": 1e10, "f6": 3e9},
                     {"f12": "600001", "f21": 2e10, "f6": 2e9}], 3),
                 2: ([{"f12": "600002", "f21": 3e10, "f6": 1e9}], 3)}
        written = {}
        with mock.patch.object(self.md, "_clist_page", side_effect=lambda pn, **k: pages[pn]), \
                mock.patch.object(self.md, "_read", return_value=None), \
                mock.patch.object(self.md, "_write", side_effect=lambda p, o: written.update(o) or o), \
                mock.patch.object(self.md.time, "sleep"):
            out = self.md.market_summary(full=True)
        self.assertEqual(out["家数"], 3)
        self.assertEqual(out["值"], round(6e10 / 1e8, 2))       # 流通市值合计
        self.assertEqual(out["成交额_亿"], round(6e9 / 1e8, 2))
        self.assertTrue(written)                                # 成功要落缓存

    def test_market_summary_failure_not_cached(self):
        with mock.patch.object(self.md, "_clist_page", return_value=([], None)), \
                mock.patch.object(self.md, "_read", return_value=None), \
                mock.patch.object(self.md, "_write") as w:
            out = self.md.market_summary(full=True)
        self.assertIsNone(out["值"])
        self.assertTrue(out["错误"])
        self.assertFalse(w.called, "取不到全市场汇总时不能落缓存（下次还要能重试）")

    def test_market_summary_fast_mode_marks_pending(self):
        out = self.md.market_summary(full=False)
        self.assertIsNone(out["值"])
        self.assertIn("快速版未跑", out["错误"])

    def test_width_above_ma20_counts_ratio(self):
        rows = [{"f12": "60000%d" % i, "f6": 1e9} for i in range(4)]
        def series(step):
            return [{"date": "2026-08-%02d" % (i + 1), "open": 10.0,
                     "close": 10.0 + i * step, "high": 10.0, "low": 10.0, "vol": 1e6,
                     "额": 1e9} for i in range(30)]

        up, down = series(0.1), series(-0.1)            # 一路上涨 / 一路下跌
        with mock.patch.object(self.md, "_clist_page", return_value=(rows, 4)), \
                mock.patch.object(self.md.mechdata, "kline",
                                  side_effect=lambda c6, **k: (up if c6.endswith(("0", "1"))
                                                               else down)), \
                mock.patch.object(self.md, "_read", return_value=None), \
                mock.patch.object(self.md, "_write", side_effect=lambda p, o: o), \
                mock.patch.object(self.md.time, "sleep"):
            out = self.md.width_above_ma20(full=True, sample=4)
        self.assertEqual(out["样本"], 4)
        self.assertEqual(out["值"], 50.0)
        self.assertIn("样本口径", out["口径"])

    def test_width_failure_not_cached(self):
        with mock.patch.object(self.md, "_clist_page", return_value=([], None)), \
                mock.patch.object(self.md, "_read", return_value=None), \
                mock.patch.object(self.md, "_write") as w:
            out = self.md.width_above_ma20(full=True)
        self.assertIsNone(out["值"])
        self.assertFalse(w.called)


class TestNoSourceItems(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.md = import_module("mktdata")
        cls.mk = import_module("mktscore")

    def test_no_source_reasons_are_written(self):
        for name, reason in self.mk.NO_SOURCE.items():
            self.assertIn("无数据源", reason, name)

    def test_collect_swallows_errors(self):
        """取数全线失败时 collect 也要能返回结构（不抛错），缺失原因留给 mktscore。"""
        with mock.patch.object(self.md, "newest_file", return_value=None), \
                mock.patch.object(self.md, "limit_counts_from_pan_dirs", return_value={}), \
                mock.patch.object(self.md, "record_snapshot", return_value={}), \
                mock.patch.object(self.md, "cnh_midprice", return_value=(None, {})), \
                mock.patch.object(self.md, "index_bars", side_effect=lambda *a, **k: {"bars": []}), \
                mock.patch.object(self.md, "index_fflow", return_value={"明细": []}), \
                mock.patch.object(self.md, "market_summary", return_value={"值": None}), \
                mock.patch.object(self.md, "pe_percentile", return_value={"值": None}), \
                mock.patch.object(self.md, "bond10y", return_value={"值": None}), \
                mock.patch.object(self.md, "m2_yoy", return_value={"值": None}), \
                mock.patch.object(self.md, "shibor_3m", return_value={"值": None}), \
                mock.patch.object(self.md, "margin_balance", return_value={"值": None}), \
                mock.patch.object(self.md, "width_above_ma20", return_value={"值": None}):
            ev = self.md.collect(full=False)
        self.assertIsNone(ev["交易日"])
        block = self.mk.score(ev)
        self.assertEqual(len(block["无数据源"]), 7)
        self.assertTrue(block["缺失"])
