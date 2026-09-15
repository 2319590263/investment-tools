# -*- coding: utf-8 -*-
"""总控台荐股榜：新版（模型评分）与旧版（机械分）产物都要能读，打法筛选给全 5 档。"""

import unittest

from _common import import_module

NEW_DOC = {
    "kind": "pick", "schema_version": "2", "生成时间": "2026-09-16 10:00:00",
    "交易日": "2026-09-15",
    "扫描": {"全市场总数": 5915, "候选池数量": 400, "排除统计": {"ST": 30}},
    "扫描参数": {"候选池上限": 400, "送模型数量": 150},
    "机械口径": {"可得总分": 89, "缺失": [{"指标": "近20日北向资金累计净流入", "满分": 3}]},
    "被否决": [{"阶段": "全池", "代码": "000001", "名称": "平安银行", "原因": "ST"}],
    "配置": {"profile": "deepseek-双档", "model": "deepseek-v4-pro"},
    "推荐榜": [
        {"排名": 1, "代码": "600967", "名称": "内蒙一机", "打法": "超短线", "评分": 91,
         "评级": "关注", "理由": "主力净流入 3.4 亿", "所属板块": "国防军工",
         "机械分": 72.5, "现价": 13.42, "涨跌幅_pct": 1.2, "来源": "模型"},
        {"排名": 2, "代码": "002463", "名称": "沪电股份", "打法": "波段", "评分": 88,
         "评级": "观察", "理由": "20 日超额收益 6%", "所属板块": "元件",
         "机械分": 80.0, "现价": 125.9, "涨跌幅_pct": -0.5, "来源": "模型"},
        {"排名": 3, "代码": "512890", "名称": "红利低波ETF", "打法": "未定", "评分": None,
         "评级": None, "理由": "模型没有点评这只（按机械分补位）", "所属板块": "基金",
         "机械分": 65.0, "现价": 1.203, "涨跌幅_pct": 0.1, "来源": "机械分补位"},
    ],
}

OLD_DOC = {
    "kind": "pick", "schema_version": "1", "生成时间": "2026-09-13 09:00:00",
    "参数": {"模块": ["短线"], "筛选": {}},
    "候选": {"短线": [{"代码": "600967", "名称": "内蒙一机", "机械分": 61.0, "评级": "偏强",
                       "现价": 13.0, "涨跌幅_pct": 2.0, "来源板块": "国防军工",
                       "换手率_pct": 2.0, "量比": 1.1}]},
    "模型层": {"error": "模型调用失败"},
}


class TestBuildRows(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.rank = import_module("pickrank")

    def test_new_product_rows(self):
        rows = self.rank.build_rows(NEW_DOC, {"002463"}, {"512890"})
        self.assertEqual([r["代码"] for r in rows], ["600967", "002463", "512890"])
        top = rows[0]
        self.assertEqual(top["推荐度"], 91)
        self.assertEqual(top["模型分"], 91)
        self.assertEqual(top["机械分"], 72.5)
        self.assertEqual(top["打法"], "超短线")
        self.assertTrue(top["是否持仓"] is False)
        self.assertTrue(rows[1]["是否持仓"])
        self.assertTrue(rows[2]["是否自选"])
        self.assertEqual(rows[2]["打法"], "未定", "模型没点评的行标「未定」")

    def test_old_product_falls_back_to_mech(self):
        rows = self.rank.build_rows(OLD_DOC)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["推荐度"], 61.0)
        self.assertIsNone(rows[0]["模型分"])
        self.assertEqual(rows[0]["打法"], "未定")
        self.assertEqual(rows[0]["行来源"], "旧产物")

    def test_style_facets_always_five(self):
        facets = self.rank.facet_styles(self.rank.build_rows(NEW_DOC))
        names = [f["名称"] for f in facets]
        self.assertEqual(names, ["超短线", "短线", "波段", "中线", "长线", "未定"])
        counts = {f["名称"]: f["候选数"] for f in facets}
        self.assertEqual(counts["超短线"], 1)
        self.assertEqual(counts["中线"], 0)
        self.assertEqual(counts["未定"], 1)

    def test_empty_rank_shape(self):
        empty = self.rank.empty_rank()
        self.assertEqual(empty["行"], [])
        self.assertIsNone(empty["产物"])
        self.assertEqual(len(empty["打法"]), 5)
        self.assertTrue(empty["提示"])

    def test_apply_quotes_only_when_refresh(self):
        rows = self.rank.build_rows(NEW_DOC)
        quotes = {"600967": {"价格": 13.99, "涨跌幅_pct": 3.3}}
        self.rank.apply_quotes(rows, quotes, refresh=False)
        self.assertEqual(rows[0]["现价"], 13.42)
        self.rank.apply_quotes(rows, quotes, refresh=True)
        self.assertEqual(rows[0]["现价"], 13.99)
        self.assertEqual(rows[0]["价格来源"], "东财实时行情")


if __name__ == "__main__":
    unittest.main()
