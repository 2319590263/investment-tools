# -*- coding: utf-8 -*-
"""荐股纯逻辑（全大盘版）：参数收敛、打法归一、排除规则、事实包与 Markdown。"""

import unittest

from _common import import_module


class TestPickParams(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.pick = import_module("pick")

    def test_defaults(self):
        opts = self.pick.pick_param({})
        self.assertEqual(opts["pool_size"], 400)
        self.assertEqual(opts["model_top"], 150)
        self.assertEqual(opts["max_chars"], 40000)
        self.assertTrue(opts["exclude"]["st"])
        self.assertFalse(opts["exclude"]["skip_688_bj"])
        self.assertIsNone(opts["profile"])

    def test_clamps_and_ignores_legacy_keys(self):
        opts = self.pick.pick_param({"pool_size": 9, "model_top": 9999, "max_chars": 1,
                                     "module": "短线", "industry": [{"code": "BK1036"}],
                                     "concepts": ["BK1000"], "per_board": 5})
        self.assertEqual(opts["pool_size"], 40)         # 下限 40
        self.assertEqual(opts["model_top"], 400)        # 上限 400
        self.assertEqual(opts["max_chars"], 4000)       # 下限 4000
        self.assertNotIn("modules", opts)
        self.assertNotIn("industry", opts)
        self.assertNotIn("concepts", opts)

    def test_style_normalisation(self):
        p = self.pick
        self.assertEqual(p.pick_style_of("超短线"), "超短线")
        self.assertEqual(p.pick_style_of("短线（1-5 日）"), "短线")
        self.assertEqual(p.pick_style_of("波段操作"), "波段")
        self.assertEqual(p.pick_style_of("我没写"), "短线")      # 回退默认
        self.assertEqual(p.pick_style_of(""), "短线")
        self.assertEqual([n for n, _c in p.PICK_STYLES],
                         ["超短线", "短线", "波段", "中线", "长线"])

    def test_exclude_rules(self):
        p = self.pick
        ex = {"st": True, "new": True, "low_price": True, "low_amount": True,
              "skip_688_bj": False}
        self.assertEqual(p.pick_exclude_reason({"f14": "*ST 海投", "f2": 5, "f6": 1e9,
                                                "f3": 1}, ex), "ST")
        self.assertEqual(p.pick_exclude_reason({"f14": "A", "f2": 1.5, "f6": 1e9,
                                                "f3": 1}, ex), "低价股")
        self.assertEqual(p.pick_exclude_reason({"f14": "A", "f2": 10, "f6": 1e7,
                                                "f3": 1}, ex), "成交额过低")
        self.assertIsNone(p.pick_exclude_reason({"f14": "A", "f2": 10, "f6": 1e9,
                                                 "f3": 1, "f24": 5, "f25": 6,
                                                 "f109": 3, "f110": 2, "f160": 1}, ex))
        self.assertEqual(p.pick_exclude_reason({"f14": "A", "f2": 10, "f6": 1e9, "f3": 1,
                                                "f12": "688111", "f24": 5, "f25": 6,
                                                "f109": 3, "f110": 2, "f160": 1},
                                               dict(ex, skip_688_bj=True)), "科创板/北交所")


class TestFactpack(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.pick = import_module("pick")

    def _payload(self):
        rows = [{"代码": "600967", "名称": "内蒙一机", "行业": "国防军工", "现价": 13.42,
                 "涨跌幅_pct": 1.2, "成交额_亿": 5.1, "换手率_pct": 2.0, "量比": 1.1,
                 "20日_pct": 3.0, "60日_pct": 8.0, "年初至今_pct": 12.0, "PE_TTM": 30.1,
                 "PB": 2.1, "总市值_亿": 200.0, "自由流通市值_亿": 90.0,
                 "主力净流入_万": 1200.0, "主力净占比_pct": 3.4, "ROE_pct": 11.2,
                 "毛利率_pct": 30.0, "净利率_pct": 9.0, "营收同比_pct": 12.0,
                 "净利同比_pct": 20.0, "资产负债率_pct": 45.0}]
        return {
            "生成时间": "2026-09-16 10:00:00", "交易日": "2026-09-15",
            "扫描参数": {"候选池上限": 400, "送模型数量": 1}, "扫描": {"全市场总数": 5915,
                                                        "候选池数量": 400, "排除统计": {}},
            "市场环境": {"bond10y": 2.6, "amount5": 12000.0, "limitup5": 55.0},
            "板块背景": {"行业": {"前10": [{"名称": "半导体", "涨跌幅_pct": 3.1}], "后10": []},
                         "概念": {"前10": [{"名称": "算力", "涨跌幅_pct": 4.0}]}},
            "候选池": rows,
            "候选技术": {"600967": {"MA20距离_pct": 1.2, "MACD": 3, "RSI14": 55.0,
                                     "换手10日_pct": 2.2, "主力10日_万": 3400.0,
                                     "龙虎榜": 0, "质押比例_pct": 0.0, "北向变动_pp": 0.1,
                                     "股东户数集中度": 8.5, "十大流通占比_pct": 55.0}},
            "降级": ["没有 pan 快照"], "机械口径": {"缺失": [{"指标": "X", "满分": 3}]},
        }

    def test_factpack_has_no_mech_score(self):
        txt = self.pick.pick_factpack(self._payload(), 40000)
        self.assertIn("600967", txt)
        self.assertIn("半导体", txt)
        # 个股机械分不能进事实包（避免锚定模型）；大盘评分块是行情背景，允许且必须带
        self.assertIn("大盘评分", txt)
        body = txt.split("## 0 市场环境")[0]
        self.assertIn("大盘评分", body or "")          # 大盘评分块在最前面
        for line in txt.split("\n"):
            if line.startswith("|"):
                self.assertNotIn("机械分", line, "候选表里不能有个股机械分")
        self.assertNotIn("候选表（原始行情与财务，共 0 只）\n| 代码 | 名称 | 机械分", txt)
        self.assertNotIn("推荐度", txt)

    def test_factpack_trims_to_cap(self):
        p = self._payload()
        p["候选池"] = [dict(p["候选池"][0], 代码="6009%02d" % i) for i in range(150)]
        txt = self.pick.pick_factpack(p, 6000)
        self.assertLess(len(txt), 6000 * 3, "超预算时应逐条截断")

    def test_markdown_contains_rank_and_caliber(self):
        p = self._payload()
        p["推荐榜"] = [{"排名": 1, "代码": "600967", "名称": "内蒙一机", "打法": "短线",
                        "评分": 88, "评级": "关注", "机械分": 70.0, "现价": 13.42,
                        "涨跌幅_pct": 1.2, "所属板块": "国防军工", "理由": "主力连续流入"}]
        p["机械层"] = [{"代码": "600967", "名称": "内蒙一机", "机械分": 70.0,
                        "实得": 62.0, "可得": 89.0}]
        p["被否决"] = [{"阶段": "全池", "代码": "000001", "名称": "平安银行", "原因": "ST"}]
        md = self.pick.pick_markdown(p)
        self.assertIn("推荐榜", md)
        self.assertIn("内蒙一机", md)
        self.assertIn("一票否决", md)
        self.assertIn("不构成投资建议", md)


if __name__ == "__main__":
    unittest.main()
