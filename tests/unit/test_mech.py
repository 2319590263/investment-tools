# -*- coding: utf-8 -*-
"""机械打分：阈值边界、缺失口径、一票否决（严格照《机器打分逻辑.txt》）。"""

import unittest

from _common import import_module


class TestMechScore(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.mech = import_module("mech")
        cls.tech = import_module("mechtech")

    def test_missing_items_leave_denominator(self):
        """三项无数据源 → 分母 89；缺失项进「缺失」清单。"""
        full = {k: None for _m, _t, items in self.mech.MODULES for k, _l, _f, _fn in items}
        full.update({"roe": 20, "gross_margin": 50, "net_margin": 20, "deduct_yoy": 40,
                     "rev_yoy_q": 40, "debt_ratio": 30, "ocf_to_profit": 120,
                     "goodwill_ratio": 5, "ar_vs_rev": -3})
        res = self.mech.score_stock(full)
        self.assertEqual(res["可得"], 25.0)         # 只有模块3 的 25 分有数据
        self.assertEqual(res["机械分"], 100.0)
        labels = [x["指标"] for x in res["缺失"]]
        self.assertIn("沪深300 PE-TTM 近10年历史分位", labels)
        self.assertIn("近20日北向资金累计净流入", labels)
        self.assertIn("行业 PE-TTM 近10年历史分位", labels)
        self.assertEqual(self.mech.COVERED_TOTAL, 89)

    def test_module3_boundaries(self):
        s = self.mech
        self.assertEqual(s.s_roe(15, {}), 4)
        self.assertEqual(s.s_roe(14.99, {}), 2)
        self.assertEqual(s.s_roe(10, {}), 2)
        self.assertEqual(s.s_roe(9.99, {}), 1)
        self.assertEqual(s.s_roe(5, {}), 1)
        self.assertEqual(s.s_roe(4.99, {}), 0)
        self.assertEqual(s.s_gross_margin(40, {}), 3)
        self.assertEqual(s.s_gross_margin(39.9, {}), 2)
        self.assertEqual(s.s_gross_margin(19.9, {}), 0)
        self.assertEqual(s.s_net_margin(15, {}), 3)
        self.assertEqual(s.s_net_margin(8, {}), 2)
        self.assertEqual(s.s_net_margin(3, {}), 1)
        self.assertEqual(s.s_net_margin(2.9, {}), 0)
        self.assertEqual(s.s_deduct_yoy(30, {}), 5)
        self.assertEqual(s.s_deduct_yoy(15, {}), 3)
        self.assertEqual(s.s_deduct_yoy(0, {}), 1)
        self.assertEqual(s.s_deduct_yoy(-0.1, {}), 0)
        self.assertEqual(s.s_ar_vs_rev(-1, {}), 1)
        self.assertEqual(s.s_ar_vs_rev(10, {}), 0.5)
        self.assertEqual(s.s_ar_vs_rev(10.1, {}), 0)
        self.assertEqual(s.s_ocf_to_profit(100, {}), 2)
        self.assertEqual(s.s_ocf_to_profit(50, {}), 1)
        self.assertEqual(s.s_ocf_to_profit(49.9, {}), 0)
        self.assertEqual(s.s_goodwill_ratio(9.9, {}), 2)
        self.assertEqual(s.s_goodwill_ratio(10, {}), 1)
        self.assertEqual(s.s_goodwill_ratio(30, {}), 0)

    def test_debt_ratio_by_industry(self):
        s = self.mech
        self.assertEqual(s.s_debt_ratio(39, {}), 2)
        self.assertEqual(s.s_debt_ratio(45, {}), 1)
        self.assertEqual(s.s_debt_ratio(61, {}), 0)
        self.assertEqual(s.s_debt_ratio(91, {"行业": "银行"}), 2)
        self.assertEqual(s.s_debt_ratio(92, {"行业": "银行"}), 0)
        self.assertEqual(s.s_debt_ratio(69, {"行业": "房地产"}), 2)
        self.assertEqual(s.s_debt_ratio(70, {"行业": "房地产"}), 0)

    def test_industry_specials(self):
        s = self.mech
        self.assertEqual(s.s_ind_profit_yoy(30, {"行业": "电子"}), 5)
        self.assertEqual(s.s_ind_profit_yoy(15, {"行业": "银行"}), 5)      # 银行阈值减半
        self.assertEqual(s.s_ind_profit_yoy(8, {"行业": "证券"}), 3)
        self.assertIsNone(s.s_ind_profit_yoy(30, {"行业": "煤炭"}))        # 强周期要 PPI → 缺失

    def test_module4_boundaries(self):
        s = self.mech
        self.assertEqual(s.s_ma(0, {}), 2)
        self.assertEqual(s.s_ma(-0.01, {}), 0)
        self.assertEqual(s.s_vol_trend(1.2, {}), 4)
        self.assertEqual(s.s_vol_trend(1.19, {}), 2)
        self.assertEqual(s.s_vol_trend(0.89, {}), 0)
        self.assertEqual(s.s_vol_price(1.2, {}), 3)
        self.assertEqual(s.s_vol_price(0.95, {}), 1)
        self.assertEqual(s.s_vol_price(0.89, {}), 0)
        self.assertEqual(s.s_macd(3, {}), 3)
        self.assertEqual(s.s_macd(1, {}), 1)
        self.assertEqual(s.s_macd(0, {}), 0)
        self.assertEqual(s.s_rsi14(50, {}), 2)
        self.assertEqual(s.s_rsi14(35, {}), 1)
        self.assertEqual(s.s_rsi14(65, {}), 1)
        self.assertEqual(s.s_rsi14(70.1, {}), 0)
        self.assertEqual(s.s_rsi14(29.9, {}), 0)
        self.assertEqual(s.s_excess20(5, {}), 3)
        self.assertEqual(s.s_excess20(0, {}), 1)
        self.assertEqual(s.s_excess20(-0.1, {}), 0)
        self.assertEqual(s.s_chip_focus(9.9, {}), 2)
        self.assertEqual(s.s_chip_focus(20, {}), 0)

    def test_module5_and_6(self):
        s = self.mech
        self.assertEqual(s.s_main_flow_ratio(2, {}), 4)
        self.assertEqual(s.s_main_flow_ratio(0.1, {}), 1)
        self.assertEqual(s.s_main_flow_ratio(-2, {}), 0)
        self.assertEqual(s.s_lhb_inst(1, {}), 2)
        self.assertEqual(s.s_lhb_inst(0, {}), 1)
        self.assertEqual(s.s_lhb_inst(-1, {}), 0)
        self.assertEqual(s.s_top10_free(60, {}), 2)
        self.assertEqual(s.s_top10_free(40, {}), 1)
        self.assertEqual(s.s_top10_free(39, {}), 0)
        self.assertEqual(s.s_turnover10(4.9, {}), 2)
        self.assertEqual(s.s_turnover10(9.9, {}), 1)
        self.assertEqual(s.s_turnover10(10, {}), 0)
        self.assertEqual(s.s_no_penalty(1, {}), 2)
        self.assertEqual(s.s_no_penalty(0, {}), 0)
        self.assertEqual(s.s_no_reduction(1, {}), 1)
        self.assertEqual(s.s_no_reduction(0, {}), 0)

    def test_veto_batch_and_announce(self):
        s = self.mech
        self.assertIn("ST", s.veto_batch({"名称": "*ST 海投"}))
        self.assertIn("一字跌停", s.veto_batch({"名称": "A", "一字跌停": True}))
        self.assertIn("自由流通市值", s.veto_batch({"名称": "A", "自由流通市值_亿": 19.9}))
        self.assertIn("营收", s.veto_batch({"名称": "A", "两年亏损且营收不足1亿": True}))
        self.assertIsNone(s.veto_batch({"名称": "A", "自由流通市值_亿": 20.0}))
        self.assertIn("立案", s.veto_announce({"立案或谴责": True}))
        self.assertIn("质押", s.veto_announce({"质押比例_pct": 85}))
        self.assertIsNone(s.veto_announce({"质押比例_pct": 84.9}))
        self.assertIn("减持", s.veto_announce({"减持比例_pct": 2}))
        self.assertIsNone(s.veto_announce({"减持比例_pct": 1.9, "有减持计划": True}))
        self.assertEqual(len(s.veto_summary()["未覆盖"]), 1)

    def test_indicators(self):
        t = self.tech
        bars = []
        for i in range(60):
            close = 10 + i * 0.1
            bars.append({"date": "2026-%02d-%02d" % (1 + i // 28, 1 + i % 28),
                         "open": close - 0.02, "close": close, "high": close + 0.05,
                         "low": close - 0.05, "vol": 1000 + i * 10, "换手_pct": 1.0})
        closes_ = t.closes(bars)
        self.assertAlmostEqual(t.sma(closes_, 20),
                               sum(closes_[-20:]) / 20.0, places=6)
        self.assertIsNone(t.sma(t.closes(bars), 120))
        self.assertGreater(t.vol_ratio(bars, 20, 60), 1.1)
        self.assertAlmostEqual(t.rsi(t.closes(bars), 14), 100.0, places=1)
        dif, dea = t.macd(t.closes(bars))
        self.assertIsNotNone(dif)
        self.assertGreater(dif, dea)
        self.assertEqual(t.macd(t.closes(bars[:20])), (None, None))
        # 20 日前收盘 = 10 + 39*0.1 = 13.9，最新 = 10 + 59*0.1 = 15.9
        self.assertAlmostEqual(t.return_pct(t.closes(bars), 20), (15.9 / 13.9 - 1) * 100, places=2)
        self.assertIsNone(t.excess_return_pct(bars, bars[:10], 20))
        self.assertGreater(t.range_pos_pct(bars, 20), 90.0)
        self.assertIsNone(t.volume_price_ratio(bars))       # 全阳线：没有阴线均量 → 缺失


if __name__ == "__main__":
    unittest.main()
