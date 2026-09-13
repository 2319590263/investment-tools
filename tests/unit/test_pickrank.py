# -*- coding: utf-8 -*-
"""总控台荐股榜：推荐度加成、排序、两级筛选树、报价降级。全部打桩，不碰真实产物。"""

import json
import os
import tempfile
import unittest
from unittest import mock

from _common import import_module

rank = import_module("pickrank")


def doc(**kw):
    base = {
        "生成时间": "2026-09-12T21:06:00", "交易日": "2026-09-11",
        "参数": {"筛选": {"行业": [{"代码": "BK1201", "名称": "电子", "细分": "全部"}],
                         "概念": []}, "模块": ["短线"]},
        "板块": {"行业": [{"名称": "半导体", "一级行业": "电子"},
                         {"名称": "元件", "一级行业": "电子"}],
                 "概念": [{"名称": "英伟达概念", "主题": "AI与算力"}]},
        "候选": {"短线": [
            {"代码": "600237", "名称": "铜峰电子", "来源板块": "元件", "板块类型": "行业",
             "机械分": 93.2, "评级": "强", "现价": 10.0, "涨跌幅_pct": 3.0, "所属行业": "元件"},
            {"代码": "300563", "名称": "神宇股份", "来源板块": "半导体", "板块类型": "行业",
             "机械分": 95.4, "评级": "强", "现价": 27.92, "涨跌幅_pct": 19.98, "所属行业": "半导体"},
            {"代码": "002902", "名称": "铭普光磁", "来源板块": "英伟达概念", "板块类型": "概念",
             "机械分": 90.0, "评级": "偏强", "现价": 20.0, "涨跌幅_pct": 1.0, "所属行业": "元件"},
        ]},
        "模型层": {"json": {"模块": [{"模块": "短线", "评分": 70, "首推": [
            {"代码": "300563", "名称": "神宇股份", "评级": "关注",
             "关注买点": {"价位": "26元附近", "依据": "整数关口回踩"},
             "止损": {"价位": "25", "依据": "跌破动能破坏"},
             "目标位": [{"价位": "30", "依据": "整数关口"}, {"价位": "33", "依据": "前高"}]},
            {"代码": "600237", "名称": "铜峰电子", "评级": "观察"},
        ]}]}},
        "降级": [], "成本": {}, "配置": {"profile": "zhipu-双档", "model": "glm"},
    }
    base.update(kw)
    return base


class TestScore(unittest.TestCase):

    def test_bonus_by_place(self):
        self.assertEqual([rank.rank_bonus(i) for i in (1, 2, 3)], [8.0, 6.0, 4.0])
        self.assertEqual(rank.rank_bonus(None), 0.0)
        self.assertEqual(rank.rank_bonus(4), 0.0)

    def test_score_caps_at_100(self):
        self.assertEqual(rank.recommend_score(95.4, 1), 100.0)     # 95.4 + 8 → 封顶 100
        self.assertEqual(rank.recommend_score(93.2, 2), 99.2)
        self.assertEqual(rank.recommend_score(90.0, None), 90.0)
        self.assertIsNone(rank.recommend_score(None, 1))


class TestRows(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.rows = rank.build_rows(doc(), {"300563"}, {"600237"})

    def test_sorted_by_recommend(self):
        self.assertEqual([r["代码"] for r in self.rows], ["300563", "600237", "002902"])
        self.assertEqual([r["推荐度"] for r in self.rows], [100.0, 99.2, 90.0])
        self.assertEqual([r["首推名次"] for r in self.rows], [1, 2, None])

    def test_first_pick_price_text_is_passed_through(self):
        top = self.rows[0]
        self.assertEqual(top["买点"]["价位"], "26元附近")
        self.assertEqual(top["止损"]["价位"], "25")
        self.assertEqual(top["止盈点"]["价位"], "30")      # 目标位取第一个
        self.assertEqual(top["止盈点数"], 2)
        self.assertEqual(self.rows[1]["买点"], None)       # 没给价位的首推原样为 None

    def test_held_and_watch_flags(self):
        flags = {r["代码"]: (r["是否持仓"], r["是否自选"]) for r in self.rows}
        self.assertEqual(flags["300563"], (True, False))
        self.assertEqual(flags["600237"], (False, True))
        self.assertEqual(flags["002902"], (False, False))

    def test_concept_row_uses_own_industry_for_l1(self):
        row = [r for r in self.rows if r["代码"] == "002902"][0]
        self.assertEqual(row["概念主题"], "AI与算力")
        self.assertEqual(row["一级行业"], "电子")           # 概念股用所属行业归到一级
        self.assertEqual(row["细分"], "元件")

    def test_facet_tree_counts(self):
        tree = rank.facet_tree(self.rows)
        self.assertEqual([x["名称"] for x in tree["行业"]], ["电子"])
        self.assertEqual({x["名称"]: x["候选数"] for x in tree["行业"][0]["子项"]},
                         {"元件": 2, "半导体": 1})
        self.assertEqual(tree["概念"][0]["名称"], "AI与算力")
        self.assertEqual(tree["概念"][0]["子项"][0]["名称"], "英伟达概念")
        self.assertEqual(tree["未归类"], [])

    def test_unmapped_board_goes_to_other(self):
        d = doc()
        d["候选"]["短线"] = [dict(d["候选"]["短线"][0], 来源板块="外星板块", 所属行业="外星行业")]
        rows = rank.build_rows(d)
        self.assertEqual(rows[0]["一级行业"], rank.OTHER_L1)
        tree = rank.facet_tree(rows)
        self.assertEqual(tree["行业"], [])
        self.assertEqual(tree["未归类"], [{"名称": "外星板块", "候选数": 1}])

    def test_apply_quotes_overrides_snapshot(self):
        rows = rank.build_rows(doc())
        before = {r["代码"]: r["现价"] for r in rows}
        rank.apply_quotes(rows, {"300563": {"价格": 30.5, "涨跌幅_pct": -1.2}}, refresh=True)
        hit = [r for r in rows if r["代码"] == "300563"][0]
        self.assertEqual(hit["现价"], 30.5)
        self.assertEqual(hit["价格来源"], "东财实时行情")
        other = [r for r in rows if r["代码"] == "600237"][0]
        self.assertEqual(other["现价"], before["600237"])       # 没报价的保留产物快照价
        self.assertEqual(other["价格来源"], "产物快照")

    def test_apply_quotes_noop_without_refresh(self):
        rows = rank.build_rows(doc())
        rank.apply_quotes(rows, {"300563": {"价格": 30.5}}, refresh=False)
        hit = [r for r in rows if r["代码"] == "300563"][0]
        self.assertEqual(hit["现价"], 27.92)
        self.assertEqual(hit["价格来源"], "产物快照")

    def test_same_stock_across_modules_is_merged(self):
        d = doc()
        d["参数"]["模块"] = ["短线", "中线"]
        d["候选"]["中线"] = [
            {"代码": "300563", "名称": "神宇股份", "来源板块": "半导体", "板块类型": "行业",
             "机械分": 91.0, "评级": "偏强", "现价": 27.0, "涨跌幅_pct": 1.0, "所属行业": "半导体"},
            {"代码": "600519", "名称": "贵州茅台", "来源板块": "半导体", "板块类型": "行业",
             "机械分": 70.0, "评级": "中性", "现价": 1500.0, "涨跌幅_pct": 0.5, "所属行业": "半导体"},
        ]
        rows = rank.build_rows(d)
        codes = [r["代码"] for r in rows]
        self.assertEqual(len(codes), len(set(codes)), "同一只股票只占一行")
        self.assertEqual(len(codes), 4)
        hit = [r for r in rows if r["代码"] == "300563"][0]
        self.assertEqual(hit["模块列表"], ["短线", "中线"])       # 固定顺序（短线→波段→中线→长线）
        self.assertEqual(hit["模块数"], 2)
        self.assertEqual(hit["推荐度"], 100.0)                    # 取该股在各模块里的最高推荐度
        self.assertEqual(hit["模块"], "短线")                     # 主行 = 推荐度最高的那条

    def test_facet_modules_always_four(self):
        facets = rank.facet_modules(rank.build_rows(doc()))
        self.assertEqual([f["名称"] for f in facets], ["短线", "波段", "中线", "长线"])
        self.assertEqual([f["候选数"] for f in facets], [3, 0, 0, 0])


class TestBuildRank(unittest.TestCase):

    def test_empty_when_no_product(self):
        with mock.patch.object(rank, "latest_pick_path", return_value=None):
            out = rank.build_rank()
        self.assertIsNone(out["产物"])
        self.assertEqual(out["行"], [])
        self.assertIn("还没有荐股结果", out["提示"][0])
        self.assertEqual(out["筛选树"], {"行业": [], "概念": [], "未归类": []})
        self.assertEqual([f["候选数"] for f in out["模块"]], [0, 0, 0, 0], "没产物也给全 4 个模块")

    def test_reads_latest_product(self):
        tmp = tempfile.mkdtemp(prefix="aiplan-rank-")
        path = os.path.join(tmp, "210706_pick.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc(), fh, ensure_ascii=False)
        with mock.patch.object(rank, "latest_pick_path", return_value=path):
            out = rank.build_rank()
        self.assertEqual(len(out["行"]), 3)
        self.assertEqual(out["产物"]["模块"], ["短线"])
        self.assertEqual(out["产物"]["交易日"], "2026-09-11")
        self.assertIn("只覆盖「短线」", "".join(out["提示"]))
        self.assertIn("波段、中线、长线", "".join(out["提示"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
