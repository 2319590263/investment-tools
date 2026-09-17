# -*- coding: utf-8 -*-
"""大盘评分（mktscore）：27 个分项阈值边界、折算、机械7:模型3、缺失与无数据源清单。

纯函数测试：直接喂证据 dict，不联网、不读盘。
"""

import unittest

from _common import import_module


def bars(closes, amounts=None):
    out = []
    for i, c in enumerate(closes):
        out.append({"date": "2026-%02d-%02d" % (1 + i // 28, 1 + i % 28), "open": c, "high": c + 1,
                    "low": c - 1, "close": c, "vol": 1e8,
                    "额": None if amounts is None else amounts[i]})
    return out


def ev_full(**over):
    """一份「全部取到」的证据（不含无数据源项，那些是写死的）。"""
    e = {
        "交易日": "2026-09-17", "取数时间": "2026-09-18T02:00:00+08:00",
        "估值": {"沪深300PE": {"值": 14.31, "分位": 76.9, "序列条数": 2430,
                            "起": "20160104", "止": "20251231",
                            "来源": "中证指数官网 index-perf"},
                "中证500PE": {"值": 26.6, "分位": 75.8, "起": "20160104", "止": "20251231"}},
        "宏观": {"国债10Y": {"值": 1.696, "来源": "pan"}, "M2同比": {"值": 7.5, "数据日期": "2026-08-01"},
                "拆借3M": {"值": 1.43, "数据日期": "2026-09-17"}},
        "行情": {"沪深300日K": {"bars": bars([3000 + i * 5 for i in range(300)]), "数据日期": "2026-09-17"},
                "沪深300月K": {"bars": bars([3000 + i * 20 for i in range(60)]), "数据日期": "2026-09-17"},
                "上证日K": {"bars": bars([3000] * 40, [8e11] * 40)},
                "深证日K": {"bars": bars([3000] * 40, [9e11] * 40)},
                "美元指数": {"bars": bars([100 + i * 0.1 for i in range(40)]), "数据日期": "2026-09-17"},
                "标普500": {"bars": bars([7000 + i * 10 for i in range(40)]), "数据日期": "2026-09-17"}},
        "资金": {"资金流": {"1.000001": {"明细": [["2026-09-17", 200.0]]},
                          "0.399106": {"明细": [["2026-09-17", 100.0]]}},
                "全市场汇总": {"值": 500000.0, "成交额_亿": 18000.0, "家数": 5900},
                "两融": {"值": 26000.0, "数据日期": "2026-09-16"},
                "成交额5日": {"值": 11000.0, "天数": 5, "来源": "上证综指 + 深证综指 日K"}},
        "情绪": {"历史": {"2026-09-17": {}}, "涨停5日": {"值": 50.0, "天数": 5},
                "跌停5日": {"值": 4.0, "天数": 5}, "涨跌家数比5日": {"值": 1.2, "天数": 5}},
        "外围": {"中间价变动20日": {"值": 0.2, "天数": 20, "来源": "新浪财经"}},
        "宽度": {"值": 60.0, "样本": 400, "口径": "成交额前 400 只"},
    }
    e.update(over)
    return e


class TestThresholds(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.mk = import_module("mktscore")

    def _items(self, ev):
        out = {}
        for build in self.mk.MODULES:
            _name, _full, items = build(ev)
            for i in items:
                out[i["指标"]] = i
        return out

    def test_asc_bands(self):
        asc = self.mk._asc
        # 沪深300 PE 分位：<20 → 10；20~40 → 7；40~60 → 4；60~80 → 2；≥80 → 0
        cuts, default = ((20, 7), (40, 4), (60, 2), (80, 0)), 10
        self.assertEqual([asc(v, cuts, default) for v in (19.9, 20, 39.9, 40, 59.9, 60, 79.9, 80)],
                         [10, 7, 7, 4, 4, 2, 2, 0])
        # 10 年国债：<2.6 → 5；2.6~2.9 → 4；2.9~3.2 → 2；3.2~3.5 → 1；≥3.5 → 0
        cuts, default = ((2.6, 4), (2.9, 2), (3.2, 1), (3.5, 0)), 5
        self.assertEqual([asc(v, cuts, default) for v in (2.59, 2.6, 2.9, 3.2, 3.5)],
                         [5, 4, 2, 1, 0])
        # M2：<6 → 0；6~8 → 1；8~10 → 2；10~12 → 3；≥12 → 4
        cuts, default = ((6, 1), (8, 2), (10, 3), (12, 4)), 0
        self.assertEqual([asc(v, cuts, default) for v in (5.9, 6, 8, 10, 12)],
                         [0, 1, 2, 3, 4])
        # 成交额：6000/8000/10000/12000
        cuts, default = ((6000, 1), (8000, 2), (10000, 3), (12000, 4)), 0
        self.assertEqual([asc(v, cuts, default) for v in (5999, 6000, 8000, 10000, 12000)],
                         [0, 1, 2, 3, 4])
        # 两融占流通市值：<2.0 → 4；2.0~2.5 → 3；2.5~3.0 → 2；3.0~3.5 → 1；≥3.5 → 0
        cuts, default = ((2.0, 3), (2.5, 2), (3.0, 1), (3.5, 0)), 4
        self.assertEqual([asc(v, cuts, default) for v in (1.99, 2.0, 2.5, 3.0, 3.5)],
                         [4, 3, 2, 1, 0])
        # 主力 10 日：<-100 → 0；-100~100 → 1；100~500 → 2；≥500 → 3
        cuts, default = ((-100, 1), (100, 2), (500, 3)), 0
        self.assertEqual([asc(v, cuts, default) for v in (-100.1, -100, 100, 500)],
                         [0, 1, 2, 3])
        # 宽度：<30 → 0；30~50 → 1；50~70 → 2；≥70 → 4
        cuts, default = ((30, 1), (50, 2), (70, 4)), 0
        self.assertEqual([asc(v, cuts, default) for v in (29.9, 30, 50, 70)],
                         [0, 1, 2, 4])
        # 涨停：<20 → 0；20~40 → 1；40~60 → 2；≥60 → 3
        cuts, default = ((20, 1), (40, 2), (60, 3)), 0
        self.assertEqual([asc(v, cuts, default) for v in (19.9, 20, 40, 60)],
                         [0, 1, 2, 3])
        # 换手：<1 → 0；1~2 → 1；2~3 → 1.5；≥3 → 2
        cuts, default = ((1, 1), (2, 1.5), (3, 2)), 0
        self.assertEqual([asc(v, cuts, default) for v in (0.9, 1, 2, 3)],
                         [0, 1, 1.5, 2])
        # 涨跌家数比：<0.9 → 0；0.9~1.1 → 1；1.1~1.5 → 2；≥1.5 → 3
        cuts, default = ((0.9, 1), (1.1, 2), (1.5, 3)), 0
        self.assertEqual([asc(v, cuts, default) for v in (0.89, 0.9, 1.1, 1.5)],
                         [0, 1, 2, 3])
        # 标普：<0 → 0；0~5 → 1；≥5 → 2
        cuts, default = ((0, 1), (5, 2)), 0
        self.assertEqual([asc(v, cuts, default) for v in (-0.1, 0, 4.9, 5)],
                         [0, 1, 1, 2])
        # 美元指数：跌幅 ≥2% → 2；-2~2 → 1；≥2 → 0
        cuts, default = ((-2, 1), (2, 0)), 2
        self.assertEqual([asc(v, cuts, default) for v in (-3, -2, 0, 2, 3)],
                         [2, 1, 1, 0, 0])
        # 人民币中间价（升值幅度）：≥1 → 3；-0.5~1 → 2；-1~-0.5 → 1；< -1 → 0
        cuts, default = ((-1, 1), (-0.5, 2), (1, 3)), 0
        self.assertEqual([asc(v, cuts, default) for v in (-1.1, -1, -0.5, 0, 1)],
                         [0, 1, 2, 2, 3])

    def test_desc_bands(self):
        desc = self.mk._desc
        # 跌停家数：≤5 → 2；5~15 → 1；>15 → 0
        cuts = ((5, 2), (15, 1))
        self.assertEqual([desc(v, cuts) for v in (4.9, 5, 15, 15.1)], [2, 2, 1, 0])

    def test_ma_and_macd(self):
        ev = ev_full()
        items = self._items(ev)
        self.assertEqual(items["沪深300 收盘价 vs 250 日均线"]["得分"], 3)   # 一路上涨 → 站在均线上
        self.assertEqual(items["沪深300 收盘价 vs 120 日均线"]["得分"], 2)
        self.assertEqual(items["沪深300 收盘价 vs 60 日均线"]["得分"], 2)
        self.assertEqual(items["沪深300 月线 MACD 状态"]["得分"], 4)        # 单边上涨 → DIF>DEA 且 DIF>0

    def test_macd_states(self):
        mk = self.mk
        up = [3000 + i * 10 for i in range(60)]
        dif, dea = mk.macd(up)
        self.assertGreater(dif, dea)
        self.assertGreater(dif, 0)
        dif, dea = mk.macd([])
        self.assertIsNone(dif)

    def test_module_items_all_present(self):
        """27 个分项一个不少，分值与文件一致，7 个无数据源项固定标注。"""
        items = self._items(ev_full())
        self.assertEqual(len(items), 27)
        self.assertEqual(sum(i["满分"] for i in items.values()), 100.0)
        named = [k for k, v in items.items() if v.get("无数据源")]
        self.assertEqual(len(named), len(self.mk.NO_SOURCE))
        self.assertEqual(sum(items[k]["满分"] for k in named), 32.0)

    def test_each_module_full_marks(self):
        totals = {}
        for build in self.mk.MODULES:
            name, full, items = build(ev_full())
            totals[name] = (full, sum(i["满分"] for i in items))
        self.assertEqual([v[0] for v in totals.values()], [25.0, 20.0, 20.0, 15.0, 10.0, 10.0])
        for full, got in totals.values():
            self.assertEqual(full, got)


class TestCombine(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.mk = import_module("mktscore")

    def test_ratio_and_missing(self):
        b = self.mk.score(ev_full())
        self.assertEqual(b["机械可得"], 68.0)          # 100 - 32（无数据源）
        self.assertIsNone(b["模型分"])
        self.assertEqual(b["总分"], b["机械分"])         # 模型分未生成 → 暂 100% 机械
        self.assertIn("暂 100% 机械", b["权重"])
        self.assertEqual(len(b["无数据源"]), 7)
        self.assertEqual(b["缺失"], [])
        self.assertEqual(b["机械分"], round(b["机械实得"] / b["机械可得"] * 100, 1))

    def test_weights_7_3(self):
        b = self.mk.score(ev_full(), model_score=80)
        self.assertEqual(b["总分"], round(b["机械分"] * 0.7 + 80 * 0.3, 1))
        self.assertEqual(b["权重"], "机械 7 : 模型 3")
        self.assertEqual(b["分歧"], round(abs(b["机械分"] - 80), 1))

    def test_divergence_hint(self):
        mech = self.mk.score(ev_full())["机械分"]
        b = self.mk.score(ev_full(), model_score=max(0.0, mech - 20))     # 差 20 分 → 提示
        self.assertIsNotNone(b["分歧提示"])
        b = self.mk.score(ev_full(), model_score=min(100.0, mech + 14))   # 差 14 分 → 不提示
        self.assertIsNone(b["分歧提示"])

    def test_missing_item_shrinks_denominator(self):
        ev = ev_full()
        ev["宏观"]["M2同比"] = {"值": None, "错误": "取不到"}
        b = self.mk.score(ev)
        self.assertEqual(b["机械可得"], 64.0)           # 少了 M2 的 4 分
        self.assertIn("M2 同比增速", [x["指标"] for x in b["缺失"]])
        self.assertEqual(b["机械分"], round(b["机械实得"] / 64.0 * 100, 1))

    def test_apply_read_merges_model_score(self):
        block = self.mk.score(ev_full())
        read = {"generated_at": "2026-09-18T02:30:00+08:00", "provider": "zhipu",
                "model": "glm-4.6v", "profile": "zhipu-双档", "cost": {"人民币_估算": 0.01},
                "json": {"模型评分": 55, "评分理由": "中性略偏空", "一句话结论": "缩量反弹，别追高",
                         "风险与应对": [{"风险": "外部扰动", "监控指标": "美债", "应对": "降仓"}],
                         "操作建议": ["保持低仓"], "数据依赖": ["北向缺失"]}}
        out = self.mk.apply_read(block, read, "data/ai/market/read_20260918/x.json")
        self.assertEqual(out["模型分"], 55.0)
        self.assertEqual(out["总分"], round(out["机械分"] * 0.7 + 55 * 0.3, 1))
        self.assertEqual(out["模型解读"]["一句话结论"], "缩量反弹，别追高")
        self.assertEqual(out["读取解读"]["模型"], "zhipu / glm-4.6v")
        # 没解读时模型分为空、总分退回机械分
        out2 = self.mk.apply_read(block, {"json": {}}, None)
        self.assertIsNone(out2["模型分"])
        self.assertEqual(out2["总分"], block["机械分"])
