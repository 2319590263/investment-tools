# -*- coding: utf-8 -*-
"""名称解析（批注 1）与事实包数据日期（批注 3）：纯函数 + 打桩读文件，不联网。"""

import unittest
from unittest import mock

from _common import import_module


class TestResolveName(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fb = import_module("flowbook")

    def setUp(self):
        # 名称簿有 30 秒缓存：每个用例前清掉，免得读到上一个用例（或真实用户文件）的结果
        self.fb._NAME["ts"], self.fb._NAME["book"] = 0.0, {}

    def book(self, holdings=(), watch=()):
        return mock.patch.multiple(
            self.fb, load_holdings_bundle=mock.Mock(return_value={"持仓": list(holdings)}),
            load_watchlist=mock.Mock(return_value=list(watch)))

    def test_quote_name_wins(self):
        with self.book(holdings=[{"代码": "601398", "名称": "持仓里的名字"}]):
            got = self.fb.resolve_name({"代码": "601398", "名称": "601398"},
                                       {"601398": {"名称": "工商银行"}})
        self.assertEqual(got, "工商银行")

    def test_book_when_no_quote(self):
        with self.book(holdings=[{"代码": "601398", "名称": "工商银行"}]):
            got = self.fb.resolve_name({"代码": "601398", "名称": "601398"})
        self.assertEqual(got, "工商银行")

    def test_watchlist_and_stored_name(self):
        with self.book(watch=[{"代码": "601899", "名称": "紫金矿业"}]):
            self.assertEqual(self.fb.resolve_name({"代码": "601899", "名称": "601899"}),
                             "紫金矿业")
        with self.book():
            # 名字簿里没有：存的名字只要不是代码就用它
            self.assertEqual(self.fb.resolve_name({"代码": "600000", "名称": "浦发银行"}),
                             "浦发银行")
            # 存的名字就是代码 → 退回代码
            self.assertEqual(self.fb.resolve_name({"代码": "600000", "名称": "600000"}),
                             "600000")

    def test_book_ignores_code_like_names(self):
        """名单里的「名称」本身是代码时不采信（否则等于没解析）。"""
        with self.book(holdings=[{"代码": "601398", "名称": "601398"}]):
            book = self.fb.name_book()
        self.assertNotIn("601398", book)


class TestFactDates(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fr = import_module("flow_run")

    def test_s3_date(self):
        self.assertEqual(self.fr._s3_date("data/stock3d_20260916.json"), "2026-09-16")
        self.assertIsNone(self.fr._s3_date("data/other.json"))
        self.assertIsNone(self.fr._s3_date(None))

    def test_text_carries_every_source(self):
        out = self.fr.fact_dates(
            pan_doc={"trade_date": "2026-09-16"}, pan_path="data/pan/20260916/latest_all.json",
            s3_path="data/stock3d_20260916.json",
            score={"日K最新交易日": "2026-09-16", "取数时间": "2026-09-17 02:14:58"},
            brief={"时间": "02:14:58", "来源": "东财实时行情"},
            apply={"适用交易日": "2026-09-17", "口径": "盘中"})
        text = out["文本"]
        for want in ("2026-09-16", "2026-09-17", "东财实时行情", "02:14:58"):
            self.assertIn(want, text)
        self.assertIn("日K最新交易日", text)
        self.assertEqual(len(out["项"]), 6)

    def test_missing_sources_say_so(self):
        out = self.fr.fact_dates()
        self.assertIn("没有快照", out["文本"])
        self.assertIn("没有该标的的快照", out["文本"])
        self.assertNotIn("None", out["文本"])


if __name__ == "__main__":
    unittest.main()
