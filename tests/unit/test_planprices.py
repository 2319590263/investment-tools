# -*- coding: utf-8 -*-
"""计划价位的显示口径：区间 → 精确价；失效条件过滤掉「只说反面」的废话。"""

import unittest

from _common import import_module


class TestTriggerPrice(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.pl = import_module("planlines")

    def test_interval_with_current_price_takes_lower(self):
        self.assertEqual(self.pl.trigger_price([8.22, 8.30], "加仓", 8.25), 8.22)

    def test_interval_above_price_takes_lower(self):
        self.assertEqual(self.pl.trigger_price([8.30, 8.40], "加仓", 8.13), 8.30)

    def test_interval_below_price_takes_upper(self):
        self.assertEqual(self.pl.trigger_price([7.80, 8.00], "止损", 8.13), 8.00)

    def test_single_value_and_string_forms(self):
        self.assertEqual(self.pl.trigger_price([8.09], "止损", 8.2), 8.09)
        self.assertEqual(self.pl.trigger_price("8.22-8.30", "加仓", 8.25), 8.22)
        self.assertEqual(self.pl.trigger_price("价格 ≥ 8.22", "加仓", 8.13), 8.22)
        self.assertIsNone(self.pl.trigger_price(None, "加仓", 8.2))

    def test_sell_side_without_price_uses_upper(self):
        self.assertEqual(self.pl.trigger_price([8.30, 8.40], "减仓", None), 8.40)
        self.assertEqual(self.pl.trigger_price([8.30, 8.40], "清仓", None), 8.40)
        self.assertEqual(self.pl.trigger_price([8.30, 8.40], "加仓", None), 8.30)

    def test_meaningless_failure_is_dropped(self):
        # 「价格<8.22」= 触发价的反面 → 没有信息量
        self.assertEqual(self.pl.meaningful_failure("价格<8.22", [8.22, 8.30], "加仓"), "")
        self.assertEqual(self.pl.meaningful_failure("≥8.22", [8.22, 8.30], "加仓"), "")
        self.assertEqual(self.pl.meaningful_failure("", [8.22, 8.30], "加仓"), "")

    def test_meaningful_failure_is_kept(self):
        keep = "跌破 8.09 止损 / 板块资金转为净流出"
        self.assertEqual(self.pl.meaningful_failure(keep, [8.22, 8.30], "加仓"), keep)
        self.assertEqual(self.pl.meaningful_failure("量能萎缩", [8.22, 8.30], "加仓"),
                         "量能萎缩")


if __name__ == "__main__":
    unittest.main()
