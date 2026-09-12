# -*- coding: utf-8 -*-
"""报告实盘复核的机械层：纯函数、降级链、产物读写。

全部 hermetic：不联网、不写用户的 data/，取数与目录都用打桩或临时目录。
"""

import io
import json
import os
import shutil
import tempfile
import unittest
from datetime import date, datetime
from unittest import mock

from _common import import_module


def quote(price=14.98, source="东财实时行情"):
    return {"代码": "600967", "名称": "内蒙一机", "价格": price, "涨跌幅_pct": 1.2,
            "昨收": 14.8, "今开": 14.9, "最高": 15.1, "最低": 14.7,
            "成交额": 1e9, "换手率_pct": 3.2, "量比": 1.1, "主力净流入": 1e7,
            "来源": source, "口径": "盘中实时价", "抓取时间": "2026-09-11 10:00:00"}


class TestSession(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.pc = import_module("plancheck")

    def test_weekend_is_non_trading(self):
        with mock.patch.object(self.pc, "calendar_days", return_value=None):
            s = self.pc.session_of(datetime(2026, 9, 12, 10, 0))     # 周六
        self.assertEqual(s["名称"], "非交易日")
        self.assertFalse(s["是否交易日"])
        self.assertIn("周一至周五", s["判定依据"])

    def test_sessions_across_the_day(self):
        cases = [((9, 0), "盘前"), ((9, 20), "集合竞价"), ((10, 30), "盘中（上午）"),
                 ((12, 0), "午间休市"), ((14, 30), "盘中（下午）"), ((16, 0), "盘后")]
        for (h, m), want in cases:
            with mock.patch.object(self.pc, "calendar_days", return_value=None):
                s = self.pc.session_of(datetime(2026, 9, 11, h, m))   # 周五
            self.assertEqual(s["名称"], want, "%02d:%02d" % (h, m))
            self.assertTrue(s["是否交易日"])

    def test_calendar_overrides_weekday(self):
        """周四但不在本地日历里（节假日）→ 判成非交易日。"""
        with mock.patch.object(self.pc, "calendar_days",
                               return_value={"2026-09-10"}):
            s = self.pc.session_of(datetime(2026, 9, 11, 10, 0))
        self.assertEqual(s["名称"], "非交易日")
        self.assertFalse(s["是否交易日"])
        self.assertIn("交易日历", s["判定依据"])

    def test_report_relation(self):
        r = self.pc.report_relation("2026-09-11", today=date(2026, 9, 11))
        self.assertEqual(r["天数"], 0)
        self.assertIn("今天", r["文本"])
        r = self.pc.report_relation("2026-09-09", today=date(2026, 9, 11))
        self.assertEqual(r["天数"], 2)
        self.assertIn("已过 2 天", r["文本"])
        r = self.pc.report_relation(None)
        self.assertIsNone(r["天数"])

    def test_phase_hint_and_quote_label(self):
        for name in ("非交易日", "盘前", "集合竞价", "盘中（上午）", "午间休市",
                     "盘中（下午）", "盘后"):
            self.assertTrue(self.pc.phase_hint(name))
        self.assertIn("最近收盘价", self.pc.quote_label("非交易日"))
        self.assertIn("未开盘", self.pc.quote_label("盘前"))
        self.assertEqual(self.pc.quote_label("盘中（下午）"), "盘中实时价")
        self.assertEqual(self.pc.quote_label("盘后"), "今日收盘价")


class TestVerdict(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.pc = import_module("plancheck")

    def item(self, act="建仓", rng=(13.4, 14.0), **kw):
        d = {"动作": act, "价格区间": list(rng), "优先级": 2, "股数": 200,
             "失效条件": "跌破 12.19 止损"}
        d.update(kw)
        return d

    def test_plan_kind(self):
        for act, want in (("建仓", "买入"), ("加仓", "买入"), ("减仓", "卖出"),
                          ("清仓", "卖出"), ("观望", "观察"), ("持有", "观察")):
            self.assertEqual(self.pc.plan_kind(act), want, act)

    def test_noop_actions(self):
        """观望 / 持有 / 等待没有执行动作（加入自选或持仓本身就等于在观望）。"""
        for act in ("观望", "持有", "等待", "继续观察"):
            self.assertTrue(self.pc.is_noop(act), act)
        for act in ("建仓", "加仓", "减仓", "清仓", "止损"):
            self.assertFalse(self.pc.is_noop(act), act)

    def test_parse_range(self):
        self.assertEqual(self.pc.parse_range([14.98, 15.27]), (14.98, 15.27))
        self.assertEqual(self.pc.parse_range([15.27, 14.98]), (14.98, 15.27))
        self.assertEqual(self.pc.parse_range("13.40-14.00"), (13.4, 14.0))
        self.assertEqual(self.pc.parse_range("14.98 ~ 15.27"), (14.98, 15.27))
        self.assertEqual(self.pc.parse_range(13.7), (13.7, 13.7))
        self.assertIsNone(self.pc.parse_range(""))

    def test_in_range_is_triggered(self):
        v = self.pc.verdict_of_item(self.item(), 13.70, stop=12.19)
        self.assertEqual(v["状态"], "已触发")
        self.assertIn("可按该条买入", v["说明"])
        v = self.pc.verdict_of_item(self.item(), 14.01, stop=12.19)   # 容差 0.01
        self.assertEqual(v["状态"], "已触发")

    def test_below_and_above_range(self):
        v = self.pc.verdict_of_item(self.item(), 13.00, stop=12.19)
        self.assertEqual(v["状态"], "未触发")
        self.assertAlmostEqual(v["距离_pct"], (13.4 - 13.0) / 13.4 * 100, places=2)
        self.assertIn("还差", v["说明"])
        v = self.pc.verdict_of_item(self.item(), 14.98, stop=12.19)
        self.assertEqual(v["状态"], "未触发")
        self.assertIn("已错过", v["说明"])

    def test_stop_loss_kills_the_item(self):
        v = self.pc.verdict_of_item(self.item(), 12.10, stop=12.19)
        self.assertEqual(v["状态"], "已失效")
        self.assertIn("跌破止损", v["说明"])

    def test_voided_and_unparsable(self):
        v = self.pc.verdict_of_item(self.item(_作废=True), 13.7, stop=12.19)
        self.assertEqual(v["状态"], "已作废")
        v = self.pc.verdict_of_item({"动作": "观望", "价格区间": None}, 13.7)
        self.assertEqual(v["状态"], "无法判定")
        self.assertEqual(self.pc.verdict_of_item(self.item(), None)["状态"], "无法判定")

    def test_levels(self):
        levels = {"支撑": [{"价位": 14.05, "依据": "MA5"}],
                  "压力": [{"价位": 15.04, "依据": "BOLL 上轨"}, {"价位": 15.22, "依据": "平台高"}],
                  "止损价": 12.19,
                  "目标位": [{"价位": 15.22, "依据": "平台高"}]}
        lv = self.pc.verdict_levels(levels, 14.98)
        self.assertEqual(lv["止损"]["状态"], "未破位")
        self.assertEqual(lv["支撑"]["价位"], 14.05)
        self.assertEqual(lv["支撑"]["状态"], "未触及")
        self.assertEqual(lv["压力"]["价位"], 15.04)
        self.assertFalse(lv["目标"][0]["已达标"])
        lv = self.pc.verdict_levels(levels, 11.90)
        self.assertEqual(lv["止损"]["状态"], "已破位")
        self.assertEqual(lv["支撑"]["状态"], "已跌破")
        lv = self.pc.verdict_levels(levels, 15.30)
        self.assertTrue(lv["目标"][0]["已达标"])
        self.assertEqual(lv["压力"]["状态"], "已上破")

    def test_summary_covers_state(self):
        session = {"名称": "非交易日", "是否交易日": False}
        rel = self.pc.report_relation("2026-09-11", today=date(2026, 9, 12))
        items = [self.pc.verdict_of_item(self.item(), 14.98, stop=12.19)]
        lv = self.pc.verdict_levels({"止损价": 12.19}, 14.98)
        text = self.pc.mechanical_summary(items, lv, 14.98, session, rel)
        self.assertIn("非交易日", text)
        self.assertIn("未触发", text)
        self.assertIn("已过 1 天", text)
        items = [self.pc.verdict_of_item(self.item(), 12.10, stop=12.19)]
        text = self.pc.mechanical_summary(items, self.pc.verdict_levels({"止损价": 12.19}, 12.10),
                                          12.10, session, rel)
        self.assertIn("跌破止损", text)

    def test_summary_without_actionable_items(self):
        session = {"名称": "盘中（上午）", "是否交易日": True}
        rel = self.pc.report_relation("2026-09-11", today=date(2026, 9, 11))
        text = self.pc.mechanical_summary([], self.pc.verdict_levels({}, 14.98), 14.98, session, rel)
        self.assertIn("没有需要执行的动作", text)


class TestQuoteFallback(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.pc = import_module("plancheck")

    def setUp(self):
        self.pc._QUOTE_CACHE.clear()

    def test_em_first(self):
        with mock.patch.object(self.pc, "_quote_from_em", return_value=quote()):
            q, hints = self.pc.fetch_quote("600967.SH", refresh=True)
        self.assertEqual(q["来源"], "东财实时行情")
        self.assertEqual(hints, [])

    def test_falls_back_to_stock3d(self):
        with mock.patch.object(self.pc, "_quote_from_em", return_value=None), \
                mock.patch.object(self.pc, "_quote_from_stock3d",
                                  return_value=quote(14.5, "stock3d 缓存（降级）")):
            q, hints = self.pc.fetch_quote("600967.SH", refresh=True)
        self.assertIn("降级", q["来源"])
        self.assertTrue(any("stock3d" in h for h in hints))

    def test_falls_back_to_report_price(self):
        with mock.patch.object(self.pc, "_quote_from_em", return_value=None), \
                mock.patch.object(self.pc, "_quote_from_stock3d", return_value=None):
            q, hints = self.pc.fetch_quote("600967.SH", fallback_price=14.98, refresh=True)
        self.assertEqual(q["来源"], "报告内现价（降级）")
        self.assertEqual(q["价格"], 14.98)
        self.assertTrue(hints)

    def test_total_failure(self):
        with mock.patch.object(self.pc, "_quote_from_em", return_value=None), \
                mock.patch.object(self.pc, "_quote_from_stock3d", return_value=None):
            q, hints = self.pc.fetch_quote("600967.SH", refresh=True)
        self.assertIsNone(q)
        self.assertTrue(hints)

    def test_no_code(self):
        q, hints = self.pc.fetch_quote("", refresh=True)
        self.assertIsNone(q)
        self.assertTrue(hints)

    def test_short_cache(self):
        calls = {"n": 0}

        def fake(thscode):
            calls["n"] += 1
            return quote()

        with mock.patch.object(self.pc, "_quote_from_em", side_effect=fake):
            self.pc.fetch_quote("600967.SH")
            self.pc.fetch_quote("600967.SH")            # 5 秒内应命中缓存
            self.pc.fetch_quote("600967.SH", refresh=True)
        self.assertEqual(calls["n"], 2)


class TestBuildAndStore(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.pc = import_module("plancheck")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="plancheck_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pc._QUOTE_CACHE.clear()

    def _fake_report(self):
        d = os.path.join(self.tmp, "20260911")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "040458_post.json")
        payload = {
            "phase": "post", "trade_date": "2026-09-11", "generated_at": "2026-09-12T04:04:58",
            "标的": {"代码": "600967.SH", "名称": "内蒙一机"}, "现价": 14.98,
            "profile": {"名称": "zhipu-双档"},
            "研判": {"json": {"方向": "偏多", "置信度": 55, "一句话结论": "涨停封板但乖离偏高",
                              "关键价位": {"支撑": [{"价位": 14.05, "依据": "MA5"}],
                                           "压力": [{"价位": 15.04, "依据": "BOLL 上轨"}],
                                           "止损价": 12.19,
                                           "目标位": [{"价位": 15.22, "依据": "平台高"}]},
                              "计划": [{"动作": "观望", "价格区间": [14.98, 15.27], "优先级": 1,
                                        "股数": 0, "失效条件": "站稳 15.22"},
                                       {"动作": "建仓", "价格区间": [13.4, 14.0], "优先级": 2,
                                        "股数": 200, "失效条件": "跌破 12.19"}]}},
        }
        with io.open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        return path

    def test_build_with_fixed_quote(self):
        path = self._fake_report()
        session = {"名称": "盘中（上午）", "是否交易日": True, "判定依据": "测试", "现在": "2026-09-11 10:00:00"}
        with mock.patch.object(self.pc, "AI_DIR", self.tmp), \
                mock.patch.object(self.pc, "session_of", return_value=session), \
                mock.patch.object(self.pc, "stock3d_tech", return_value=({}, None)), \
                mock.patch.object(self.pc, "latest_pan", return_value=({}, None)), \
                mock.patch.object(self.pc, "fetch_quote", return_value=(quote(13.70), [])):
            data, err = self.pc.build_plan_check(path, refresh=True)
        self.assertIsNone(err)
        self.assertEqual(data["实盘"]["价格"], 13.70)
        self.assertEqual(data["时段"]["名称"], "盘中（上午）")
        # 观望条目被省略，只剩「建仓」这一条可执行计划；价 13.70 落在 13.40~14.00 内
        self.assertEqual([i["状态"] for i in data["机械"]["计划"]], ["已触发"])
        self.assertEqual(data["机械"]["省略"]["条数"], 1)
        self.assertIn("已触发", data["机械"]["一句话"])
        self.assertIn("背景数据", data)
        self.assertIn("触发条件", data["机械"]["计划"][0])

    def test_build_skips_watch_only_items(self):
        """观望条目不进计划表，但要在页面上留一行「已省略 N 条」。"""
        path = self._fake_report()
        session = {"名称": "盘中（上午）", "是否交易日": True, "判定依据": "测试", "现在": "2026-09-11 10:00:00"}
        with mock.patch.object(self.pc, "AI_DIR", self.tmp), \
                mock.patch.object(self.pc, "session_of", return_value=session), \
                mock.patch.object(self.pc, "stock3d_tech", return_value=({}, None)), \
                mock.patch.object(self.pc, "latest_pan", return_value=({}, None)), \
                mock.patch.object(self.pc, "fetch_quote", return_value=(quote(13.70), [])):
            data, err = self.pc.build_plan_check(path, refresh=True)
        self.assertIsNone(err)
        self.assertEqual([i["动作"] for i in data["机械"]["计划"]], ["建仓"])
        self.assertEqual(data["机械"]["省略"]["条数"], 1)
        self.assertIn("观望", data["机械"]["省略"]["说明"])
        fact = self.pc.plancheck_factpack(data)
        self.assertNotIn("观望", fact.split("【脚本机械判定】")[1].split("【背景数据")[0])

    def test_build_rejects_outside_paths(self):
        with mock.patch.object(self.pc, "AI_DIR", self.tmp):
            data, err = self.pc.build_plan_check(os.path.join(os.path.dirname(self.tmp), "x.json"))
            self.assertIsNone(data)
            self.assertIn("不在 data/ai", err)
            data, err = self.pc.build_plan_check("")
            self.assertIsNone(data)
            self.assertIn("缺少 report", err)

    def test_save_and_latest_by_report(self):
        store = os.path.join(self.tmp, "plancheck")
        with mock.patch.object(self.pc, "PLANCHECK_DIR", store):
            p1 = self.pc.save_plan_check({"报告路径": "data/ai/a.json", "点评": {"一句话结论": "A"}})
            p2 = self.pc.save_plan_check({"报告路径": "data/ai/b.json", "点评": {"一句话结论": "B"}})
            self.assertTrue(os.path.isfile(p1) and os.path.isfile(p2))
            self.assertTrue(os.path.isfile(os.path.join(os.path.dirname(p1), "latest.json")))
            doc, path = self.pc.latest_plan_check("data/ai/a.json")
            self.assertEqual(doc["点评"]["一句话结论"], "A")
            self.assertEqual(path, p1)
            doc, _ = self.pc.latest_plan_check()
            self.assertIn(doc["报告路径"], ("data/ai/a.json", "data/ai/b.json"))
            self.assertIsNone(self.pc.latest_plan_check("data/ai/不存在.json")[0])

    def test_degraded_bundle_marks_source(self):
        """东财与本地缓存都不可用时，走报告内现价降级并给出提示。"""
        path = self._fake_report()
        with mock.patch.object(self.pc, "AI_DIR", self.tmp), \
                mock.patch.object(self.pc, "PLANCHECK_DIR", os.path.join(self.tmp, "plancheck")), \
                mock.patch.object(self.pc, "session_of",
                                  return_value={"名称": "非交易日", "是否交易日": False,
                                                "判定依据": "测试", "现在": "2026-09-12 10:00:00"}), \
                mock.patch.object(self.pc, "stock3d_tech", return_value=({}, None)), \
                mock.patch.object(self.pc, "latest_pan", return_value=({}, None)), \
                mock.patch.object(self.pc, "_quote_from_em", return_value=None), \
                mock.patch.object(self.pc, "_quote_from_stock3d", return_value=None):
            data, err = self.pc.plancheck_bundle(path, refresh=True)
        self.assertIsNone(err)
        self.assertEqual(data["实盘"]["来源"], "报告内现价（降级）")
        self.assertTrue(any("降级" in h for h in data["提示"]))
        self.assertIsNone(data["点评"])


class TestBackgroundData(unittest.TestCase):
    """喂给模型的背景数据：个股量价形态 + 大盘 + 板块，都是现成字段的裁剪。"""

    @classmethod
    def setUpClass(cls):
        cls.pc = import_module("plancheck")

    TECH = {"analysis": {"last": 14.98, "ma5": 14.05, "ma20": 12.23, "pos60": 100.0,
                         "vol_ratio": 1.289, "macd_dif": 0.79},
            "volume_price": {"近5日": {"上涨天数": 3, "下跌天数": 2, "涨跌量比": 1.545}},
            "turnover": {"日": 10.36, "5日均": 8.1},
            "levels": {"近60日平台高": 15.22, "MA250": 15.27, "换手率_pct": 10.36},
            "divergence": {"小结": "量价齐升"},
            "limit_board": {"连板": 1}}

    PAN = {"phase": "post", "trade_date": "2026-09-11", "is_trading_day": True,
           "data": {"sentiment": {"广度": {"上涨家数": 643, "下跌家数": 4870,
                                           "上涨占比_pct": 11.58, "涨停家数_阈值口径": 49,
                                           "两市成交额_亿": 19869.9, "主力净流入_亿": -529.32}},
                    "funds": {"南向资金": {"日期": "2026-09-11", "南向合计_净买入_亿": 44.31}},
                    "sectors": {"行业板块": {"领涨": [{"名称": "地面兵装Ⅲ", "涨跌幅_pct": 4.48,
                                                     "主力净流入_亿": 13.58,
                                                     "板块内上涨家数": 12, "板块内下跌家数": 0,
                                                     "领涨股": "内蒙一机"}]},
                                "概念板块": {"领跌": [{"名称": "某概念", "涨跌幅_pct": -3.2,
                                                     "主力净流入_亿": -5.1}]}},
                    "next_day": {"次日": "2026-09-14", "限售解禁": {"解禁总市值_亿": 302.26}}}}

    def test_extracts_stock_market_sectors(self):
        ctx = self.pc.market_context(self.TECH, self.PAN, name="内蒙一机", pan_rel="data/pan/x.json")
        stock = ctx["个股量价与形态"]
        self.assertEqual(stock["均线与分位"]["vol_ratio"], 1.289)
        self.assertEqual(stock["量价（近5/10/20日）"]["近5日"]["涨跌量比"], 1.545)
        self.assertIn("量价背离", stock)
        self.assertIn("涨跌停与连板", stock)
        market = ctx["大盘"]
        self.assertEqual(market["上涨占比_pct"], 11.58)
        self.assertEqual(market["主力净流入_亿"], -529.32)
        self.assertEqual(market["南向资金净买入_亿"], 44.31)
        self.assertEqual(market["pan快照"]["交易日"], "2026-09-11")
        blocks = ctx["板块"]
        self.assertEqual(blocks["行业领涨"][0]["名称"], "地面兵装Ⅲ")
        self.assertEqual(blocks["行业领涨"][0]["板块内涨跌家数"], "12涨/0跌")
        self.assertEqual(blocks["本股领涨的板块"][0]["板块"], "地面兵装Ⅲ")
        self.assertEqual(blocks["概念领跌"][0]["名称"], "某概念")

    def test_empty_inputs_are_safe(self):
        ctx = self.pc.market_context(None, None)
        self.assertEqual(ctx["个股量价与形态"], {})
        self.assertEqual(ctx["大盘"], {})
        self.assertEqual(ctx["板块"], {})

    def test_factpack_carries_background_and_trigger(self):
        data = {"报告": {"标的": {"名称": "内蒙一机", "代码": "600967.SH"}, "phase": "post",
                         "trade_date": "2026-09-11", "generated_at": "x", "报告现价": 14.98,
                         "方向": "偏多", "置信度": 55},
                "实盘": quote(), "时段": {"名称": "盘中（上午）", "是否交易日": True,
                                          "判定依据": "测试", "现在": "2026-09-11 10:00:00"},
                "关系": {"文本": "报告交易日就是今天"},
                "机械": {"一句话": "…",
                         "计划": [{"优先级": 2, "动作": "建仓", "类别": "买入", "区间文本": "13.40 ~ 14.00",
                                   "状态": "未触发", "距离_pct": 7.0, "股数": 200, "金额_元": 2740,
                                   "触发条件": "回踩至13.40-14.00 且缩量企稳",
                                   "失效条件": "跌破 12.19", "说明": "还差 7%"}],
                         "关键价位": {"止损": {"价位": 12.19, "状态": "未破位", "距离_pct": 22.9},
                                      "目标": [{"价位": 15.22, "已达标": False}]}},
                "背景数据": self.pc.market_context(self.TECH, self.PAN, name="内蒙一机"),
                "提示": ["提示 1"]}
        fact = self.pc.plancheck_factpack(data)
        self.assertIn("触发条件：回踩至13.40-14.00 且缩量企稳", fact)
        self.assertIn("背景数据", fact)
        self.assertIn("涨跌量比", fact)
        self.assertIn("上涨占比_pct", fact)
        self.assertIn("提示 1", fact)

    def test_prompt_forbids_manual_confirmation(self):
        """用户要求：条件由模型自己判，不许把结论推回人工确认。"""
        text = self.pc.PLANCHECK_SYSTEM + self.pc.PLANCHECK_PROMPT
        self.assertIn('"状态": "已满足|未满足"', self.pc.PLANCHECK_PROMPT)
        self.assertNotIn("已满足|未满足|需人工确认", self.pc.PLANCHECK_PROMPT)
        self.assertIn("禁止出现「需人工确认」", text)
        self.assertIn("依据", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
