# -*- coding: utf-8 -*-
"""费用换算：USD 计费的 provider 也要能算出人民币（回归 float * dict 那个坑）。

aiplan.compute_cost(provider, model, usage, fx) 的第 4 个参数是**汇率数字**，
网页端统一走 plancheck.cost_of(provider, model, usage, cfg) 从配置里取 USD_CNY。
"""

import unittest

from _common import import_module


def provider(currency):
    return {"名称": "p", "协议": "openai-chat", "单价": {"输入": 1.0, "输出": 2.0,
                                                    "缓存读取": 0.1, "币种": currency}}


USAGE = {"输入": 1_000_000, "输出": 1_000_000, "缓存读取": 200_000}


class TestCostFx(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.pc = import_module("plancheck")

    def test_usd_provider_with_fx_dict_config(self):
        """配置里给的是「汇率」字典，也必须按 USD_CNY 折算，而不是把字典乘进去。"""
        cost = self.pc.cost_of(provider("USD"), "m", USAGE, {"汇率": {"USD_CNY": 7.1}})
        # 计费口径：输入按「输入 − 缓存读取」计，缓存读取另算
        expect = (1_000_000 - 200_000) / 1e6 * 1.0 + 1_000_000 / 1e6 * 2.0 + 200_000 / 1e6 * 0.1
        self.assertAlmostEqual(cost["金额"], expect, places=4)
        self.assertEqual(cost["币种"], "USD")
        self.assertAlmostEqual(cost["人民币_估算"], round(cost["金额"] * 7.1, 4), places=3)

    def test_cny_provider_not_converted(self):
        cost = self.pc.cost_of(provider("CNY"), "m", USAGE, {"汇率": {"USD_CNY": 7.1}})
        self.assertEqual(cost["人民币_估算"], cost["金额"])

    def test_missing_or_broken_fx_falls_back(self):
        for cfg in (None, {}, {"汇率": {}}, {"汇率": {"USD_CNY": "abc"}}):
            cost = self.pc.cost_of(provider("USD"), "m", USAGE, cfg)
            self.assertAlmostEqual(cost["人民币_估算"], round(cost["金额"] * 7.1, 4), places=3)

    def test_no_price_configured_reports_tokens_only(self):
        cost = self.pc.cost_of({"名称": "p"}, "m", USAGE, {"汇率": {"USD_CNY": 7.1}})
        self.assertIsNone(cost["金额"])
        self.assertIn("未配置单价", cost["说明"])

    def test_real_config_providers_do_not_raise(self):
        """用真实的模型配置把每个 provider 都算一遍：USD 计费的那几个不能再抛异常。"""
        cfg = self.pc.aiplan.read_json(self.pc.MODELS_PATH) or {}
        providers = cfg.get("providers") or []
        if not providers:
            self.skipTest("没有可用的模型配置（跳过真实配置回归）")
        fxcfg = {"汇率": cfg.get("汇率") or {}}
        for p in providers:
            with self.subTest(provider=p.get("名称")):
                cost = self.pc.cost_of(p, "any-model", USAGE, fxcfg)
                self.assertIsInstance(cost, dict)
                self.assertIn("金额", cost)


if __name__ == "__main__":
    unittest.main(verbosity=2)
