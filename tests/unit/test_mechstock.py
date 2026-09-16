# -*- coding: utf-8 -*-
"""单只标的取数与打分（mechstock）：全量 6 模块、缺失只从分母去掉、一票否决、单股行解析。

全部 hermetic：所有 mechdata 取数打桩，不联网、不写盘。
"""

import unittest
from unittest import mock

from _common import import_module


def bars(n=300, base=8.0, step=0.005):
    out = []
    for i in range(n):
        close = base + i * step
        out.append({"date": "2026-01-%02d" % ((i % 28) + 1), "open": close - 0.01,
                    "high": close + 0.02, "low": close - 0.02, "close": close,
                    "vol": 1000000 + i * 100, "换手_pct": 1.2})
    return out


ROW = {"代码": "601398", "名称": "工商银行", "现价": 8.11, "涨跌幅_pct": -0.25,
       "成交额_亿": 18.4, "换手率_pct": 0.08, "量比": 0.9, "最高": 8.16, "最低": 8.04,
       "今开": 8.12, "昨收": 8.13, "总市值_亿": 28904.5, "流通市值_亿": 21865.5,
       "自由流通市值_亿": 21865.5, "资产负债率_pct": 92.3, "毛利率_pct": 0.0,
       "ROE_pct": 4.3, "营收同比_pct": 9.0, "净利同比_pct": 3.3,
       "主力净流入_万": 8350.7, "总股本": 356406257089.0, "流通股本": 26961172418.0,
       "行业": "银行Ⅱ", "停牌": False, "一字跌停": False}


class TestMarketRow(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.ms = import_module("mechstock")

    def test_parses_ulist_row(self):
        """1 次 ulist 的 f 字段 → 与 pool_row 同构（口径与全市场扫描一致）。"""
        with mock.patch.object(self.ms.mechdata, "cache_read", return_value=None), \
                mock.patch.object(self.ms.mechdata, "cache_write") as w, \
                mock.patch.object(self.ms.mechdata, "pan") as pan:
            pan.em_ulist.return_value = [{"f12": "601398", "f14": "工商银行", "f2": 8.11,
                                          "f3": -0.25, "f6": 1.84e9, "f21": 2186555043691.0,
                                          "f38": 356406257089.0, "f57": 92.3, "f100": "银行Ⅱ"}]
            row, err = self.ms.market_row("601398")
        self.assertIsNone(err)
        self.assertEqual(row["代码"], "601398")
        self.assertEqual(row["行业"], "银行Ⅱ")
        self.assertAlmostEqual(row["流通市值_亿"], 21865.55, places=1)
        self.assertEqual(w.call_count, 1)

    def test_bad_code(self):
        row, err = self.ms.market_row("abc")
        self.assertIsNone(row)
        self.assertIn("6 位", err)

    def test_cache_hit_skips_request(self):
        with mock.patch.object(self.ms.mechdata, "cache_read",
                               return_value={"row": dict(ROW)}), \
                mock.patch.object(self.ms.mechdata, "pan") as pan:
            row, err = self.ms.market_row("601398")
        self.assertIsNone(err)
        self.assertEqual(row["代码"], "601398")
        pan.em_ulist.assert_not_called()


class TestScoreOne(unittest.TestCase):
    """打桩全部取数：6 模块合成 / 缺失 / 一票否决。"""

    @classmethod
    def setUpClass(cls):
        cls.ms = import_module("mechstock")

    def patch(self, **over):
        data = {
            "market_row": (dict(ROW), None),
            "finance_by_period": ({}, "2026-06-30", "2026-03-31", "2025-06-30"),
            "balance_by_period": {},
            "code_industry_map": {},
            "scanned_industry_map": {},
            "market_scan": ([], {}, [], 5900),
            "industry_agg": {},
            "macro_inputs": {},
            "kline": bars(),
            "bench_kline": bars(n=260, base=4000.0, step=1.0),
            "lhb_state": {"601398": 0},
            "etf_flow_ratio": None,
            "pledge_map": {},
            "north_change": {},
            "announce_flags": {"立案或谴责": False, "有减持计划": False, "减持比例_pct": None},
            "profit_warning": {},
            "fund_flow": {"净额_万": 1000.0, "天数": 10},
            "holder_focus": 12.0,
            "top10_free_ratio": 55.0,
        }
        data.update(over)
        for name, value in data.items():
            if name == "market_row":
                p = mock.patch.object(self.ms, "market_row", return_value=value)
            else:
                p = mock.patch.object(self.ms.mechdata, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def test_six_modules_and_missing_denominator(self):
        self.patch()
        res, err = self.ms.score_one("601398")
        self.assertIsNone(err)
        names = [m["模块"] for m in res["模块"]]
        self.assertEqual(len(names), 6)
        self.assertEqual(names[0], "模块1 宏观市场环境")
        labels = [x["指标"] for x in res["缺失"]]
        self.assertIn("沪深300 PE-TTM 近10年历史分位", labels)   # 无数据源
        self.assertIn("商誉/净资产比例", labels)                  # 无数据源
        self.assertLess(res["机械分"]["可得"], 100.0)
        self.assertEqual(res["机械分"]["机械分"],
                         round(res["机械分"]["实得"] / res["机械分"]["可得"] * 100.0, 1))

    def test_veto_hits(self):
        self.patch(market_row=(dict(ROW, 名称="*ST 工行"), None))
        res, _err = self.ms.score_one("601398")
        self.assertTrue(res["否决"])
        self.assertIn("ST", res["否决"][0]["原因"])

    def test_veto_from_announcement(self):
        self.patch(announce_flags={"立案或谴责": True, "有减持计划": False,
                                   "减持比例_pct": None})
        res, _err = self.ms.score_one("601398")
        self.assertTrue(any("立案" in v["原因"] for v in res["否决"]))

    def test_row_failure_returns_error(self):
        self.patch(market_row=(None, "东财 ulist 没有返回 601398"))
        res, err = self.ms.score_one("601398")
        self.assertIsNone(res)
        self.assertIn("ulist", err)

    def test_industry_missing_degrades_but_still_scores(self):
        self.patch(scanned_industry_map={}, code_industry_map={})
        res, _err = self.ms.score_one("601398")
        self.assertTrue(any("行业景气" in x or "横截面" in x for x in res["降级"]))
        self.assertIsNotNone(res["机械分"]["机械分"])

    def test_module_totals_sum_to_score(self):
        self.patch()
        res, _err = self.ms.score_one("601398")
        got = sum(m["得分"] for m in res["模块"])
        self.assertAlmostEqual(got, res["机械分"]["实得"], places=1)
        self.assertEqual(sum(m["满分"] for m in res["模块"]), 100)

    def test_module_totals_helper(self):
        score = {"明细": [{"模块": "模块6 风险合规", "得分": 2, "满分": 2, "指标": "x"},
                          {"模块": "模块6 风险合规", "得分": None, "满分": 2, "指标": "y"}]}
        out = self.ms.module_totals(score)
        last = [m for m in out if m["模块"] == "模块6 风险合规"][0]
        self.assertEqual(last["得分"], 2.0)
        self.assertEqual(last["可得"], 2.0)


if __name__ == "__main__":
    unittest.main()
