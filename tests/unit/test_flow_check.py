# -*- coding: utf-8 -*-
"""体检（意外排查）：事实包组装、消息面缺失标注、四档结论兜底、run_check 记账。

全打桩，不联网、不调模型、不写用户数据。
"""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from _common import import_module


NEWS = {
    "code": "600967.SH", "target_date": "2026-09-13", "fetched_at": "2026-09-13T00:58:44",
    "sources": {"公告": "OK(11)", "快讯池": "OK(7 源, 316 条, 命中 1)", "机构调研": "FAIL(超时)"},
    "degrade": ["快讯池部分源失败(2/7)"],
    "notices": {"window": "2026-08-15~2026-09-13", "note": "近 30 天共 11 条公告，其中近 7 天 3 条",
                "items": [{"时间": "2026-09-07", "标题": "第六届董事会第十一次会议决议公告",
                           "事件分类": "公司治理", "近7天": True},
                          {"时间": "2026-09-02", "标题": "关于收到监管问询函的公告",
                           "事件分类": "风险公告", "近7天": False}]},
    "risk_notices": {"条数": 1, "列表": [{"标题": "关于收到监管问询函的公告"}]},
    "flash": [{"时间": "2026-09-13 00:58", "title": "【板块】通信",
               "content": "涨跌幅 0.0024% | 领涨股: 内蒙一机"}],
    "tags": {"公告事件": "近 30 天 11 条（近 7 天 3 条）", "当前热榜": "未在榜"},
    "lhb": {"items": [{"日期": "2026-09-11", "净买_亿": 1.2695, "上榜原因": "日涨幅达到15%的前5只证券"}]},
    "contracts": {"列表": [{"标题": "关于签订重大合同的公告"}]},
    "north_note": "北向资金自 2024-08 起停止披露",
}


class Fixture(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.f = import_module("flow")
        cls.fr = import_module("flow_run")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="flowchk_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for obj, name, value in ((self.f, "FLOW_DIR", os.path.join(self.tmp, "flows")),
                                 (self.f, "FLOW_SETTINGS", os.path.join(self.tmp, "flows", "s.json")),
                                 (self.f, "TRASH_DIR", os.path.join(self.tmp, "trash")),
                                 (self.f, "LEDGER_PATH", os.path.join(self.tmp, "台账.md"))):
            p = mock.patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.f._FLOW_CACHE.clear()
        doc, err = self.f.create_flow("600967", "内蒙一机", 30000, 15, 8, account_total=50000)
        self.assertIsNone(err)
        self.f.add_fill(doc, "买入", 13.9, 2000, account={"佣金费率_pct": 0.025, "佣金最低_元": 5,
                                                        "印花税率_pct": 0.05, "过户费率_pct": 0.001})
        self.f.apply_price(doc, 14.0)
        doc["计划"] = {"产物路径": "data/ai/track/x.json", "生成时间": "2026-09-14 15:20:00",
                       "适用交易日": "2026-09-15", "方向": "偏多", "置信度": 60,
                       "一句话结论": "回踩建仓",
                       "条目": [{"编号": 1, "动作": "建仓", "触发条件": "缩量企稳",
                                 "价格区间": [13.4, 14.0], "股数": 300, "失效条件": "跌破 13.0"}],
                       "关键价位": {"止损": 13.0, "目标": [{"价位": 15.22}],
                                  "支撑": [], "压力": []}}
        self.f.save_flow(doc)
        self.doc = doc


class TestFactpack(Fixture):

    def test_state_lines(self):
        text = self.fr.flow_state_lines(self.doc)
        self.assertIn("本流资金 30000.00 元", text)
        self.assertIn("目标收益率 15.00%", text)
        self.assertIn("最大亏损 8.00%", text)
        self.assertIn("成交明细（权威口径", text)
        self.assertIn("买入", text)
        self.assertIn("平均成本", text)
        self.assertIn("目标进度", text)

    def test_news_text_lists_notices_and_degrade(self):
        text = self.fr._news_text(NEWS, "data/stock3d_20260913.json", "2026-09-13")
        self.assertIn("董事会第十一次会议", text)
        self.assertIn("风险公告：1 条", text)
        self.assertIn("监管问询函", text)
        self.assertIn("快讯池 OK", text)
        self.assertIn("降级", text)
        self.assertIn("北向资金自 2024-08", text)

    def test_missing_news_is_stated_not_faked(self):
        text = self.fr._news_text({}, None, None)
        self.assertIn("没有公告", text)
        self.assertIn("消息面缺失", text)

    def test_factpack_sections_and_trim(self):
        bg = {"个股量价与形态": {"均线与分位": {"last": 14.0}},
              "板块": {"行业领涨": [{"名称": "军工"}]},
              "大盘": {"上涨占比_pct": 40.0}}
        brief = {"价格": 14.0, "来源": "东财实时行情", "时间": "10:31:02"}
        fact = self.fr.check_factpack(self.doc, NEWS, bg, brief, "听说董事长变更",
                                       "data/stock3d_20260913.json", "2026-09-13", 14.0)
        for title in ("体检对象与交易流状态", "当前交易计划原文", "消息面（stock3d）",
                      "个股量价与形态", "板块", "大盘", "用户手填的观察与异常"):
            self.assertIn("【%s】" % title, fact)
        self.assertIn("听说董事长变更", fact)
        self.assertIn("董事会第十一次会议", fact)
        small = self.fr.check_factpack(self.doc, NEWS, bg, brief, "", None, None, 14.0, cap=1200)
        self.assertIn("预算裁剪", small)
        self.assertIn("消息面（stock3d）", small)
        self.assertNotIn("【大盘】", small)

    def test_normalize_check_levels(self):
        out = self.fr.normalize_check({"结论": "乱写", "是否失效": True})
        self.assertEqual(out["结论"], "提高警惕")
        out = self.fr.normalize_check({"结论": "建议作废重算", "逐条依据": "只有一条"})
        self.assertEqual(out["结论"], "建议作废重算")
        self.assertEqual(out["逐条依据"], ["只有一条"])
        self.assertEqual(self.fr.normalize_check(None), None)


class TestRunCheck(Fixture):

    def test_run_check_records_result_and_alerts(self):
        settings = {"cfg": {}, "名称": "p", "profile": {}, "来源": "test",
                    "研判": {"temperature": 0.2}, "复核档": None,
                    "provider": {"名称": "prov", "协议": "openai-chat", "鉴权": "bearer"},
                    "model": "m", "key": "k", "api_base": None, "api_key": None}
        block = {"json": {"结论": "暂停新动作", "是否失效": False, "一句话结论": "不再加仓",
                          "逐条依据": ["近 7 天 1 条风险公告"], "风险与失效条件": ["跌破止损即离场"],
                          "数据依赖与不确定性": {"降级项": [], "缺失导致的不确定性": ""}},
                 "usage": {"输入": 100, "输出": 50}, "cost": {"人民币_估算": 0.01},
                 "error": None, "ok": True, "model": "m"}
        sent = []
        with mock.patch.object(self.fr.track_run, "_settings", lambda opts: settings), \
                mock.patch.object(self.fr, "_stock3d_symbol",
                                  lambda code: ({"news": NEWS, "tech": {}},
                                                "data/stock3d_20260913.json", "2026-09-13")), \
                mock.patch.object(self.fr.background, "latest_pan", lambda: ({}, None)), \
                mock.patch.object(self.fr.background, "market_context", lambda *a, **k: {}), \
                mock.patch.object(self.fr.quotes_mod, "fetch_quotes",
                                  lambda codes, refresh=False: ({"600967": {"价格": 14.1}}, [])), \
                mock.patch.object(self.fr, "_call_check", lambda log, s, fact, ctl: (block["json"], block)), \
                mock.patch.object(self.fr.alerts_store, "append", lambda items: sent.extend(items)):
            out = self.fr.run_check(lambda m: None, {}, {"流编号": self.doc["流编号"],
                                                          "补充说明": "看到问询函"})
        self.assertEqual(out["结论"], "暂停新动作")
        doc, _err = self.f.load_flow(self.doc["流编号"])
        self.assertEqual(len(doc["体检"]), 1)
        self.assertEqual(doc["体检"][0]["补充说明"], "看到问询函")
        self.assertTrue(any(e["类型"] == "体检" for e in doc["事件"]))
        self.assertEqual(sent[0]["点位类型"], "体检")
        self.assertEqual(sent[0]["级别"], "warn")

    def test_run_check_error_is_recorded(self):
        settings = {"cfg": {}, "名称": "p", "profile": {}, "来源": "test", "研判": {},
                    "复核档": None,
                    "provider": {"名称": "prov", "协议": "openai-chat", "鉴权": "bearer"},
                    "model": "m", "key": "k", "api_base": None, "api_key": None}
        with mock.patch.object(self.fr.track_run, "_settings", lambda opts: settings), \
                mock.patch.object(self.fr, "_stock3d_symbol", lambda code: (None, None, None)), \
                mock.patch.object(self.fr.background, "latest_pan", lambda: ({}, None)), \
                mock.patch.object(self.fr.background, "market_context", lambda *a, **k: {}), \
                mock.patch.object(self.fr.quotes_mod, "fetch_quotes",
                                  lambda codes, refresh=False: ({}, ["东财没取到"])), \
                mock.patch.object(self.fr, "_call_check",
                                  lambda log, s, fact, ctl: (None, {"error": "模型调用失败：连接被拒"})), \
                mock.patch.object(self.fr.alerts_store, "append", lambda items: None):
            out = self.fr.run_check(lambda m: None, {}, {"流编号": self.doc["流编号"]})
        self.assertEqual(out["结论"], "未判定")
        self.assertIn("连接被拒", out["error"])
        doc, _err = self.f.load_flow(self.doc["流编号"])
        self.assertEqual(doc["体检"][0]["结论"], "未判定")
        self.assertTrue(doc["体检"][0]["事实包字符数"] > 0, "失败也要留下事实包大小")


if __name__ == "__main__":
    unittest.main(verbosity=2)
