# -*- coding: utf-8 -*-
"""荐股与写回逻辑里的纯函数（不起服务、不联网、不花钱）。

覆盖：筛选参数整理与越界夹取、板块主题归类、排除规则、机械打分单调性、
路径越界保护、配置文件写回（先校验、先备份）。
"""

import json
import os
import shutil
import tempfile
import unittest

from _common import ROOT, import_module


def stock(**kw):
    base = {"f12": "600000", "f14": "浦发银行", "f2": 10.0, "f3": 1.0, "f6": 3e8,
            "f109": 2.0, "f110": 3.0, "f160": 1.0, "f24": 5.0, "f25": 8.0}
    base.update(kw)
    return base


class _FakeHandler:
    """只借 _save_json_config 的返回值，不碰真实 HTTP 层。"""

    @staticmethod
    def _err(msg, code=400):
        return {"code": code, "err": msg}

    @staticmethod
    def _json(obj, code=200):
        return obj


class TestPickParams(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.mod = import_module("pick")

    def test_defaults_when_empty(self):
        p = self.mod.pick_param({})
        self.assertEqual(p["modules"], ["短线"])          # 模块多选，空值回退第一个
        self.assertEqual(p["industry"], [])
        self.assertEqual(p["concepts"], [])
        self.assertEqual(p["per_board"], 5)
        self.assertEqual(p["max_candidates"], 400)
        self.assertEqual(p["max_chars"], 40000)
        self.assertEqual(p["exclude"], {"st": True, "new": True, "low_price": True,
                                        "low_amount": True, "skip_688_bj": False})

    def test_modules_multi_select(self):
        # 去重 + 按固定顺序（短线→波段→中线→长线）
        self.assertEqual(self.mod.pick_param({"modules": ["长线", "中线", "不存在", "长线"]})["modules"],
                         ["中线", "长线"])
        # module 与 modules 合并
        self.assertEqual(self.mod.pick_param({"module": "波段", "modules": ["长线"]})["modules"],
                         ["波段", "长线"])
        # 四个一起选也在（上限就是 4 个模块）
        self.assertEqual(self.mod.pick_param({"modules": ["长线", "短线", "中线", "波段"]})["modules"],
                         ["短线", "波段", "中线", "长线"])
        # 全非法时回退默认
        self.assertEqual(self.mod.pick_param({"modules": ["不存在"]})["modules"], ["短线"])

    def test_clamps_out_of_range(self):
        p = self.mod.pick_param({"per_board": 0, "max_candidates": 99999, "max_chars": 10})
        self.assertEqual(p["per_board"], 1)
        self.assertEqual(p["max_candidates"], 2000)
        self.assertEqual(p["max_chars"], 4000)
        p = self.mod.pick_param({"per_board": "abc", "max_candidates": None})
        self.assertEqual((p["per_board"], p["max_candidates"]), (5, 400))

    def test_industry_and_concept_normalisation(self):
        p = self.mod.pick_param({
            "industry": [{"code": "bk1201", "name": "电子", "subs": [" 半导体 ", ""]},
                         {"code": "bad", "name": "无效板块"},
                         {"code": "BK1201", "name": "重复"}],
            "concepts": ["bk0999", "BAD", "bk0999", "bk1000"],
        })
        self.assertEqual([x["code"] for x in p["industry"]], ["BK1201"])
        self.assertEqual(p["industry"][0]["subs"], ["半导体"])
        self.assertEqual(p["concepts"], ["BK0999", "BK1000"])

    def test_concept_limit(self):
        codes = ["BK%04d" % i for i in range(200)]
        self.assertEqual(len(self.mod.pick_param({"concepts": codes})["concepts"]),
                         self.mod.PICK_MAX_CONCEPTS)


class TestThemesAndExclude(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.mod = import_module("pick")

    def test_concept_theme_samples(self):
        cases = {"英伟达概念": "AI与算力", "半导体": "半导体与电子", "白酒Ⅲ": "消费与传媒",
                 "昨日涨停": "打板与热度", "固态电池": "新能源与电力", "券商概念": "金融与地产",
                 "低空经济": "军工与安全", "人形机器人": "机器人与智能制造",
                 "减肥药": "医药与生物", "量子科技": "通信与卫星", "莫名其妙XYZ": "其他"}
        for name, theme in cases.items():
            self.assertEqual(self.mod.pick_concept_theme(name), theme, name)
        self.assertIn(self.mod.PICK_THEME_OTHER, [self.mod.pick_concept_theme("某某")])

    def test_exclude_rules(self):
        m = self.mod
        ex = {"st": True, "new": True, "low_price": True, "low_amount": True, "skip_688_bj": False}
        self.assertIsNone(m.pick_exclude_reason(stock(), ex))
        self.assertEqual(m.pick_exclude_reason(stock(f14="ST 天成"), ex), "ST")
        self.assertEqual(m.pick_exclude_reason(stock(f14="*ST 海投"), ex), "ST")
        self.assertEqual(m.pick_exclude_reason(stock(f3="-"), ex), "无行情/停牌")
        self.assertEqual(m.pick_exclude_reason(stock(f2=1.8), ex), "低价股")
        self.assertEqual(m.pick_exclude_reason(stock(f6=1e7), ex), "成交额过低")
        new = stock(f24=12.0, f25=12.0, f109=12.0, f110=12.0, f160=12.0)
        self.assertEqual(m.pick_exclude_reason(new, ex), "次新/上市不足60日")
        self.assertEqual(m.pick_exclude_reason(stock(f12="688260"), dict(ex, skip_688_bj=True)),
                         "科创板/北交所")

    def test_exclude_can_be_disabled(self):
        m = self.mod
        off = {"st": False, "new": False, "low_price": False,
               "low_amount": False, "skip_688_bj": False}
        for row in (stock(f14="ST 天成"), stock(f2=1.8), stock(f6=1e7),
                    stock(f24=12.0, f25=12.0, f109=12.0, f110=12.0, f160=12.0)):
            self.assertIsNone(m.pick_exclude_reason(row, off))


class TestScoring(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.mod = import_module("pick")

    def test_board_score_is_monotone(self):
        rows = [stock(f3=1, f109=1, f110=1, f160=1, f184=1, f62=1e8, f104=10, f105=2, f8=1, f6=1e9),
                stock(f12="BK2", f3=9, f109=9, f110=9, f160=9, f184=9, f62=9e8, f104=30,
                      f105=1, f8=9, f6=9e9)]
        self.mod.pick_score_boards(rows)
        self.assertGreater(rows[1]["机械分"], rows[0]["机械分"])
        self.assertIn(rows[1]["评级"], ("强", "偏强"))

    def test_stock_score_is_monotone_for_every_module(self):
        rows = []
        for i in range(6):
            r = stock(f12="60000%d" % i, f3=i, f109=i, f110=i, f160=i, f24=i * 3, f25=i * 2,
                      f184=i, f62=i * 1e7, f8=i + 0.5, f10=i + 0.2, f7=i + 1, f6=(i + 1) * 1e8,
                      f21=(i + 1) * 1e10, f20=(i + 1) * 2e10, f115=(i + 1) * 10, f23=(i + 1) * 1.5)
            r["板块分"] = 50 + i
            rows.append(r)
        for module in ("短线", "波段", "中线", "长线"):
            self.mod.pick_score_stocks(rows, module)
            scores = [r["机械分"] for r in rows]
            self.assertEqual(scores, sorted(scores), "%s 打分应随池内强度单调" % module)
            self.assertTrue(all(s is not None for s in scores))

    def test_grade_thresholds(self):
        g = self.mod.pick_grade
        self.assertEqual(g(90), "强")
        self.assertEqual(g(65), "偏强")
        self.assertEqual(g(50), "中性")
        self.assertEqual(g(10), "弱")


class TestPathGuardAndWriteback(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.HTTP = import_module("webserver")
        cls.P = import_module("paths")

    def test_inside_guard(self):
        inside = self.P.inside
        data = os.path.join(ROOT, "data")
        self.assertTrue(inside(os.path.join(data, "ai", "x.json"), data))
        self.assertFalse(inside(os.path.join(ROOT, "账户配置.json"), data))
        self.assertFalse(inside(os.path.join(data, "..", "aiplan.py"), data))

    def test_json_writeback_validates_then_backs_up(self):
        tmp = tempfile.mkdtemp(prefix="aiplan_writeback_")
        path = os.path.join(tmp, "账户配置.json")
        original = json.dumps({"总资金": 1, "说明": "原始"}, ensure_ascii=False, indent=1)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(original)
        orig_const, self.HTTP.ACCOUNT_PATH = self.HTTP.ACCOUNT_PATH, path
        try:
            handler = _FakeHandler()
            r = self.HTTP.Handler._save_json_config(handler, {"text": "{不是 JSON"}, path, lambda: None)
            self.assertIn("不是合法 JSON", r["err"])
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), original, "非法 JSON 不能改动原文件")

            r = self.HTTP.Handler._save_json_config(handler, {"text": '{"总资金": 0}'}, path, lambda: None)
            self.assertIn("总资金", r["err"])
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), original)

            r = self.HTTP.Handler._save_json_config(handler, {"text": '{"总资金": 12345}'},
                                                    path, lambda: {"总资金": 12345})
            self.assertTrue(r["ok"])
            self.assertIsNotNone(r["备份"])
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["总资金"], 12345)
            with open(path + ".bak", encoding="utf-8") as fh:
                self.assertEqual(fh.read(), original, ".bak 应是写入前的原文")
        finally:
            self.HTTP.ACCOUNT_PATH = orig_const
            shutil.rmtree(tmp, ignore_errors=True)

    def test_models_writeback_requires_providers_and_profiles(self):
        tmp = tempfile.mkdtemp(prefix="aiplan_models_")
        path = os.path.join(tmp, "模型配置.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{}")
        orig_const, self.HTTP.MODELS_PATH = self.HTTP.MODELS_PATH, path
        try:
            handler = _FakeHandler()
            r = self.HTTP.Handler._save_json_config(
                handler, {"text": '{"providers": [], "profiles": {}}'}, path, lambda: None)
            self.assertIn("providers", r["err"])
            r = self.HTTP.Handler._save_json_config(
                handler, {"text": '{"providers": [{"名称": "x"}], "profiles": {}}'}, path, lambda: None)
            self.assertIn("profiles", r["err"])
        finally:
            self.HTTP.MODELS_PATH = orig_const
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
