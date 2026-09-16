# -*- coding: utf-8 -*-
"""交易流硬约束（flowplan）：档位、亏损预算反推股数、打法止损幅度、自动校正、止盈完成度。

全部纯函数：不取数、不调模型、不写盘。
"""

import unittest

from _common import import_module


def score_of(value, veto=()):
    return {"机械分": {"机械分": value, "实得": value, "可得": 100.0},
            "模块": [], "缺失": [], "否决": list(veto), "行业": "银行Ⅱ"}


def doc_of(capital=30000, target=8, loss=4):
    return {"参数": {"流资金": capital, "目标收益率_pct": target, "最大亏损_pct": loss}}


def node_of(alloc=15000, style="短线", qty=0, cost=None, avail=None):
    return {"代码": "601398", "名称": "工商银行", "打法": style, "分配资金": alloc,
            "持仓": {"股数": qty, "可用": qty if avail is None else avail, "平均成本": cost},
            "盈亏": {"已实现_元": 0, "现价": 8.0}}


def plan_of(entries, stop=7.5, goals=(8.6,)):
    return {"方向": "偏多", "置信度": 60,
            "关键价位": {"止损价": stop,
                         "目标位": [{"价位": v, "依据": "平台"} for v in goals]},
            "计划": entries}


class TestTier(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fp = import_module("flowplan")

    def test_tier_boundaries(self):
        fp = self.fp
        self.assertEqual(fp.tier_of(70)["档位"], "可满配")
        self.assertEqual(fp.tier_of(69.9)["档位"], "半配")
        self.assertEqual(fp.tier_of(55)["档位"], "半配")
        self.assertEqual(fp.tier_of(54.9)["档位"], "小仓")
        self.assertEqual(fp.tier_of(40)["档位"], "小仓")
        self.assertEqual(fp.tier_of(39.9)["档位"], "只减不加")
        self.assertFalse(fp.tier_of(39.9)["允许买入"])
        self.assertEqual(fp.tier_of(70)["仓位上限_pct"], 100.0)
        self.assertAlmostEqual(fp.tier_of(60)["仓位上限_pct"], 66.67, places=2)
        self.assertAlmostEqual(fp.tier_of(45)["仓位上限_pct"], 33.33, places=2)

    def test_tier_missing_score_is_small(self):
        """机械分缺失 → 小仓档并标注。"""
        fp = self.fp
        t = fp.tier_of(None)
        self.assertEqual(t["档位"], "小仓")
        self.assertTrue(t["允许买入"])
        self.assertIn("缺失", t["说明"])

    def test_veto_blocks_buying(self):
        fp = self.fp
        t = fp.tier_of(88, [{"范围": "公告", "原因": "控股股东质押 90% ≥ 85%"}])
        self.assertEqual(t["档位"], "一票否决")
        self.assertFalse(t["允许买入"])
        self.assertIn("85%", t["说明"])

    def test_constraints_budget_and_caps(self):
        fp = self.fp
        cons = fp.constraints(score_of(60), doc_of(capital=30000, target=8, loss=4),
                              node_of(alloc=15000), 8.0)
        self.assertEqual(cons["档位"], "半配")
        self.assertAlmostEqual(cons["仓位金额上限_元"], 10000.5, places=1)   # 15000 × 66.67%
        self.assertEqual(cons["亏损预算_元"], 600.0)                # 15000 × 4%
        self.assertEqual(cons["目标盈利_元"], 1200.0)               # 15000 × 8%
        self.assertEqual(cons["止损幅度上限_pct"], 5.0)              # 短线


class TestEnforce(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fp = import_module("flowplan")

    def cons(self, value=70, alloc=15000, loss=4, style="短线", qty=0, cost=None, veto=()):
        return self.fp.constraints(score_of(value, veto), doc_of(loss=loss),
                                   node_of(alloc=alloc, style=style, qty=qty, cost=cost), 8.0)

    def test_budget_caps_shares(self):
        """亏损预算 600 元、买点上沿 8.2、止损 7.9 → 每股风险 0.3 → 上限 2000 股。"""
        cons = self.cons(value=70, alloc=30000, loss=2)         # 预算 600、仓位上限 30000
        plan = plan_of([{"动作": "建仓", "价格区间": [8.0, 8.2], "股数": 5000}], stop=7.9)
        new, corr, _tips = self.fp.enforce(plan, cons, 8.0)
        self.assertEqual(new["计划"][0]["股数"], 2000)
        self.assertEqual(any(c["项"] == "建仓 股数" for c in corr), True)
        self.assertAlmostEqual(new["计划"][0]["金额_元"], 16400.0, places=2)

    def test_budget_too_small_voids_entry(self):
        """预算买不到 1 手 → 作废并写原因。"""
        cons = self.cons(value=70, alloc=1000, loss=1)          # 预算 10 元 → 33 股
        plan = plan_of([{"动作": "建仓", "价格区间": [8.0, 8.2], "股数": 300}], stop=7.9)
        new, corr, _tips = self.fp.enforce(plan, cons, 8.0)
        self.assertTrue(new["计划"][0]["作废"])
        self.assertIn("预算不足", new["计划"][0]["作废原因"])
        self.assertEqual(corr[0]["校正值"], None)

    def test_stop_too_far_is_tightened(self):
        """止损 7.2 距买点 8.2 超过短线 5% → 收窄到 8.2×0.95=7.79。"""
        cons = self.cons(value=70, style="短线")
        plan = plan_of([{"动作": "建仓", "价格区间": [8.1, 8.2], "股数": 100}], stop=7.2)
        new, corr, _tips = self.fp.enforce(plan, cons, 8.1)
        self.assertAlmostEqual(new["关键价位"]["止损价"], 7.79, places=3)
        self.assertEqual(corr[0]["项"], "止损价")

    def test_stop_above_price_is_pushed_down(self):
        cons = self.cons(value=70, style="波段")
        plan = plan_of([{"动作": "建仓", "价格区间": [8.1, 8.2], "股数": 100}], stop=8.3)
        new, corr, _tips = self.fp.enforce(plan, cons, 8.1)
        self.assertLess(new["关键价位"]["止损价"], 8.1)
        self.assertEqual(any(c["项"] == "止损价" for c in corr), True)

    def test_missing_stop_is_derived(self):
        cons = self.cons(value=70, style="短线")
        plan = plan_of([{"动作": "建仓", "价格区间": [8.0, 8.0], "股数": 100}], stop=None)
        new, corr, _tips = self.fp.enforce(plan, cons, 8.0)
        self.assertAlmostEqual(new["关键价位"]["止损价"], 7.6, places=3)
        self.assertIn("推算", corr[0]["原因"])

    def test_band_below_stop_is_lifted(self):
        """买入区间下沿低于止损 → 上移到止损上方（保持「跌破止损即离场」自洽）。"""
        cons = self.cons(value=70, style="波段", alloc=100000, loss=10)
        plan = plan_of([{"动作": "建仓", "价格区间": [7.4, 8.2], "股数": 100}], stop=7.5)
        new, corr, _tips = self.fp.enforce(plan, cons, 8.0)
        self.assertGreaterEqual(new["计划"][0]["价格区间"][0], 7.5)
        self.assertEqual(any(c["项"] == "买入区间" for c in corr), True)

    def test_low_tier_voids_all_buys(self):
        cons = self.cons(value=35, qty=1000, cost=8.1, alloc=30000, loss=10)
        plan = plan_of([{"动作": "建仓", "价格区间": [8.0, 8.2], "股数": 100},
                        {"动作": "减仓", "价格区间": [8.6, 8.7], "股数": 100}])
        new, corr, _tips = self.fp.enforce(plan, cons, 8.0)
        self.assertTrue(new["计划"][0]["作废"])
        self.assertIn("不允许买入", new["计划"][0]["作废原因"])
        self.assertFalse(new["计划"][1].get("作废"))

    def test_veto_asks_for_full_exit(self):
        """一票否决：不给买入条目；持仓按文件口径清仓（模型给清仓条目照样可执行）。"""
        cons = self.cons(value=90, qty=1200, cost=8.1,
                         veto=[{"范围": "公告", "原因": "公司被立案调查"}])
        plan = plan_of([{"动作": "建仓", "价格区间": [8.0, 8.2], "股数": 100},
                        {"动作": "清仓", "价格区间": [8.1, 8.2], "股数": None}])
        new, corr, _tips = self.fp.enforce(plan, cons, 8.1)
        self.assertTrue(new["计划"][0]["作废"])
        self.assertEqual(new["计划"][1]["股数"], 1200)

    def test_sell_capped_by_position(self):
        cons = self.cons(value=70, qty=1000, cost=8.1, alloc=100000, loss=10)
        plan = plan_of([{"动作": "减仓", "价格区间": [8.6, 8.7], "股数": 99999}])
        new, corr, _tips = self.fp.enforce(plan, cons, 8.1)
        self.assertEqual(new["计划"][0]["股数"], 1000)
        self.assertEqual(any("超过可卖数量" in c["原因"] for c in corr), True)

    def test_sell_without_position_is_void(self):
        cons = self.cons(value=70, qty=0, alloc=100000, loss=10)
        plan = plan_of([{"动作": "减仓", "价格区间": [8.6, 8.7], "股数": 500}])
        new, _corr, _tips = self.fp.enforce(plan, cons, 8.1)
        self.assertTrue(new["计划"][0]["作废"])
        self.assertIn("没有可卖持仓", new["计划"][0]["作废原因"])

    def test_buy_then_sell_is_allowed(self):
        """空仓开流：先建仓再止盈，可卖数量 = 计划买入量（不该被作废）。"""
        cons = self.cons(value=70, qty=0, alloc=100000, loss=10)
        plan = plan_of([{"动作": "建仓", "价格区间": [8.0, 8.2], "股数": 1000},
                        {"动作": "止盈", "价格区间": [8.6, 8.7], "股数": 1000}])
        new, _corr, _tips = self.fp.enforce(plan, cons, 8.0)
        self.assertFalse(new["计划"][0].get("作废"))
        self.assertEqual(new["计划"][1]["股数"], 1000)

    def test_priority_order_shares_budget(self):
        """两条买入按优先级抢同一份预算：先满足优先级高的。"""
        cons = self.cons(value=70, alloc=15000, loss=4)        # 预算 600
        plan = plan_of([{"动作": "加仓", "价格区间": [8.1, 8.2], "股数": 5000, "优先级": 2},
                        {"动作": "建仓", "价格区间": [8.0, 8.1], "股数": 5000, "优先级": 1}],
                       stop=7.9)
        new, _corr, _tips = self.fp.enforce(plan, cons, 8.0)
        first = [e for e in new["计划"] if e.get("优先级") == 1][0]
        second = [e for e in new["计划"] if e.get("优先级") == 2][0]
        # 预算 600 ÷ 0.2 = 3000 股，但仓位金额上限 15000 ÷ 8.1 = 1851 → 取整 1800 股
        self.assertEqual(first["股数"], 1800)
        self.assertTrue(second.get("作废") or second["股数"] < 3000)

    def test_missing_shares_filled_by_cap(self):
        cons = self.cons(value=70, alloc=15000, loss=4)          # 上限 15000 元 > 16400？不，见下
        plan = plan_of([{"动作": "建仓", "价格区间": [8.0, 8.2], "股数": None}], stop=7.9)
        new, corr, _tips = self.fp.enforce(plan, cons, 8.0)
        # 预算 600 ÷ 0.3 = 2000 股，但仓位金额上限 15000 ÷ 8.2 = 1829 → 取整 1800 股
        self.assertEqual(new["计划"][0]["股数"], 1800)
        self.assertEqual(corr[0]["原值"], None)

    def test_target_tip_and_risk_tip(self):
        """止盈完成度 + 持仓敞口提示（波段 8% 幅度内保留 7.6 的止损）。"""
        cons = self.cons(value=70, qty=2600, cost=8.1, alloc=30000, loss=4, style="波段")
        plan = plan_of([{"动作": "减仓", "价格区间": [8.6, 8.7], "股数": 100}], stop=7.6,
                       goals=(8.6,))
        _new, _corr, tips = self.fp.enforce(plan, cons, 8.1)
        self.assertTrue(any("止盈点" in t for t in tips))
        self.assertTrue(any("敞口" in t for t in tips))        # 2600×0.5=1300 > 预算 1200

    def test_compliant_plan_has_no_correction(self):
        cons = self.cons(value=70, alloc=30000, loss=4)          # 预算 1200 元、上限 30000 元
        plan = plan_of([{"动作": "建仓", "价格区间": [8.0, 8.2], "股数": 2000}], stop=7.9)
        _new, corr, _tips = self.fp.enforce(plan, cons, 8.0)
        self.assertEqual([c for c in corr if c["项"] != "止损价"], [])


class TestText(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fp = import_module("flowplan")

    def test_score_and_constraint_lines(self):
        fp = self.fp
        cons = fp.constraints(score_of(72.5), doc_of(), node_of(), 8.0)
        score = {"机械分": {"机械分": 72.5, "实得": 65.0, "可得": 89.0},
                 "模块": [{"模块": "模块4 技术面趋势", "得分": 20.0, "满分": 25}],
                 "缺失": [{"指标": "商誉/净资产比例", "满分": 2, "原因": "没有数据源"}],
                 "否决": [], "行业": "银行Ⅱ", "技术": {"MA20距离_pct": 1.2},
                 "取数时间": "2026-09-17 10:00:00", "降级": []}
        text = fp.score_lines(score, cons)
        self.assertIn("72.5", text)
        self.assertIn("商誉", text)
        self.assertIn("模块4", text)
        body = fp.constraint_lines(cons)
        self.assertIn("亏损预算", body)
        self.assertIn("100 的整数倍", body)

    def test_summary(self):
        fp = self.fp
        cons = fp.constraints(score_of(40), doc_of(), node_of(), 8.0)
        text = fp.summary(cons, [{"项": "x"}])
        self.assertIn("小仓", text)
        self.assertIn("校正 1 条", text)


if __name__ == "__main__":
    unittest.main()
