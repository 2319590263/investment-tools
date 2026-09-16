# -*- coding: utf-8 -*-
"""交易流的 HTTP 接口回归：开流（无标的）→ 加标的（多只 · 打法 · 资金上限）→ 计划与体检
（真实失败、零成本）→ 成交 → 结束（单只 / 整条）→ 删除。

写盘范围：data/ai/flows/ 下本次创建的流（结束测试时移入回收站后再删掉），以及生成计划时
落下的 data/ai/track/ 产物与 latest 指针；不碰自选股、持仓、账户配置与用户的既有交易流。
"""

import io
import json
import os
import time
import unittest

from _common import ROOT, LocalServer

FLOW_DIR = os.path.join(ROOT, "data", "ai", "flows")
TRACK_DIR = os.path.join(ROOT, "data", "ai", "track")
CANDIDATES = ("000001", "000002", "000004", "000005", "000006", "000007", "000008")


class TestFlowApi(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.server = LocalServer().start()
        cls.keep = set(os.listdir(FLOW_DIR)) if os.path.isdir(FLOW_DIR) else set()
        cls.keep_track = set(os.listdir(TRACK_DIR)) if os.path.isdir(TRACK_DIR) else set()
        cls.created = []
        cls.artifacts = []
        status, account = cls.server.get("/api/account")
        total = float(((account or {}).get("配置") or {}).get("总资金") or 0)
        cls.capital = max(1000, int(total * 0.05)) if total else 1000
        # 用户可能本来就有在跑的流：把「开测之前的已占用资金」当基线，断言只看增量
        _st, listed = cls.server.get("/api/flows")
        cls.base_used = float(((listed or {}).get("资金") or {}).get("已占用") or 0)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        for path in cls.artifacts:
            for name in (path, path.replace("_track.json", "_track.md"),
                         path.replace("_track.json", "_track_exec.json")):
                if os.path.exists(name):
                    os.remove(name)
        if os.path.isdir(TRACK_DIR):
            for name in os.listdir(TRACK_DIR):
                path = os.path.join(TRACK_DIR, name)
                if name not in cls.keep_track and os.path.isfile(path):
                    os.remove(os.path.join(TRACK_DIR, name))
            for base, _dirs, _files in os.walk(TRACK_DIR, topdown=False):
                if base != TRACK_DIR and not os.listdir(base):
                    os.rmdir(base)
        # 流文件：delete 接口会移进回收站，这里把测试残片也清掉
        if os.path.isdir(FLOW_DIR):
            for name in os.listdir(FLOW_DIR):
                if name not in cls.keep and name.endswith(".json"):
                    os.remove(os.path.join(FLOW_DIR, name))
        for path in cls.created:
            if os.path.exists(path):
                os.remove(path)

    def _poll(self, jid, timeout=240):
        deadline = time.time() + timeout
        data = {}
        while time.time() < deadline:
            status, data = self.server.get("/api/jobs/%s?from=0" % jid)
            if data.get("status") != "running":
                return data
            time.sleep(1.0)
        self.fail("任务没有在 %d 秒内结束" % timeout)

    def _new_flow(self):
        """开一条空流；挑不到可用资金就跳过用例。"""
        status, out = self.server.post("/api/flows", {
            "流资金": self.capital, "目标收益率_pct": 10, "最大亏损_pct": 5,
            "备注": "接口测试"})
        if status != 200:
            self.skipTest("开不出测试流（在跑的流已占满账户总资金）：%s" % out.get("error"))
        self.created.append(os.path.join(FLOW_DIR, "%s.json" % out["流编号"]))
        return out["流编号"], out

    def _add_first_target(self, fid):
        """加第一只标的（跳过已有进行中流的代码）。"""
        for cand in CANDIDATES:
            status, out = self.server.post("/api/flow/target/add", {
                "流编号": fid, "代码": cand, "名称": "测试标的"})
            if status == 200:
                return cand
            self.assertIn("只属于一条流", out.get("error", ""), out)
        self.skipTest("候选代码都已有进行中的交易流")

    def _free_codes(self, n):
        """没被任何进行中流占用的候选代码（一只标的只属于一条流）。"""
        _status, listed = self.server.get("/api/flows")
        used = {t["代码"] for c in (listed.get("流") or []) for t in (c.get("标的") or [])}
        free = [c for c in CANDIDATES if c not in used]
        if len(free) < n:
            self.skipTest("候选代码不够用（已有进行中的交易流占用了它们）")
        return free[:n]

    def test_flow_lifecycle(self):
        fid, out = self._new_flow()
        self.assertEqual(out["流编号"], fid)
        self.assertEqual(out["流"]["状态"], "进行中")
        self.assertEqual(out["流"]["标的"], [], "开流时可以先不加标的")

        # 参数校验：流资金 / 目标收益率 / 最大亏损
        for body, want in (({"流资金": 0, "目标收益率_pct": 10, "最大亏损_pct": 5}, "流资金"),
                           ({"流资金": 1000, "目标收益率_pct": 0, "最大亏损_pct": 5}, "目标收益率"),
                           ({"流资金": 1000, "目标收益率_pct": 10, "最大亏损_pct": -1}, "最大亏损")):
            status, bad = self.server.post("/api/flows", body)
            self.assertEqual(status, 400, bad)
            self.assertIn(want, bad["error"])

        # 加第一只标的：打法 + 分配资金（默认全部流资金）
        code_a, code_b = self._free_codes(2)
        # 按页面的口径发（中文键；曾经只认 code 导致「请填 6 位证券代码」的假报错）
        status, added = self.server.post("/api/flow/target/add", {
            "流编号": fid, "代码": code_a, "名称": "平安银行"})
        self.assertEqual(status, 200, added)
        self.assertEqual(added["标的"]["代码"], code_a)
        self.assertEqual(added["标的"]["打法"], "短线", "打法默认短线")
        self.assertEqual(added["标的"]["分配资金"], float(self.capital))
        status, set0 = self.server.post("/api/flow/target/set", {
            "流编号": fid, "代码": code_a, "打法": "超短线"})
        self.assertEqual(status, 200, set0)
        self.assertEqual(set0["卡"]["标的"][0]["打法"], "超短线")
        # 资金已经分完：再加一只就不行
        status, full = self.server.post("/api/flow/target/add", {
            "流编号": fid, "code": code_b, "name": "万科A"})
        self.assertEqual(status, 400, full)
        self.assertIn("分完", full["error"])
        # 打法只能三选一
        status, badstyle = self.server.post("/api/flow/target/set", {
            "流编号": fid, "代码": code_a, "打法": "长线"})
        self.assertEqual(status, 400)
        self.assertIn("打法", badstyle["error"])

        # 改小分配资金 → 再加第二只（资金之和不能超过流资金）
        half = self.capital // 2
        status, set1 = self.server.post("/api/flow/target/set", {
            "流编号": fid, "代码": code_a, "打法": "短线", "分配资金": half})
        self.assertEqual(status, 200, set1)
        status, added_b = self.server.post("/api/flow/target/add", {
            "流编号": fid, "code": code_b, "name": "万科A", "打法": "波段"})
        self.assertEqual(status, 200, added_b)
        self.assertEqual(added_b["标的"]["打法"], "波段")
        self.assertEqual(added_b["标的"]["分配资金"], float(self.capital - half))
        # 两只标的分掉了全部流资金：再把第一只调回全部流资金就会超
        status, over = self.server.post("/api/flow/target/set", {
            "流编号": fid, "代码": code_a, "分配资金": self.capital})
        self.assertEqual(status, 400, over)
        self.assertIn("超过流资金", over["error"])
        # 流资金不能小于流内标的分配资金合计
        status, small = self.server.post("/api/flow/params", {
            "流编号": fid, "流资金": half, "目标收益率_pct": 10, "最大亏损_pct": 5})
        self.assertEqual(status, 400, small)
        self.assertIn("不能小于", small["error"])
        status, mid_params = self.server.post("/api/flow/params", {
            "流编号": fid, "流资金": self.capital, "目标收益率_pct": 12, "最大亏损_pct": 6})
        self.assertEqual(status, 200, mid_params)
        self.assertEqual(mid_params["卡"]["参数"]["目标收益率_pct"], 12.0)
        status, two = self.server.get("/api/flows")
        card = [c for c in two["流"] if c["流编号"] == fid][0]
        self.assertEqual(len(card["标的"]), 2)
        self.assertEqual(card["汇总"]["分配合计"], float(self.capital),
                         "两只标的的分配资金之和 = 流资金")
        self.assertEqual(two["资金"]["已占用"], self.base_used + float(self.capital))
        # 一只标的只属于一条流
        status, dup = self.server.post("/api/flows", {
            "流资金": 1000, "目标收益率_pct": 10, "最大亏损_pct": 5,
            "code": code_a, "name": "平安银行", "分配资金": 1000})
        self.assertEqual(status, 400, dup)
        self.assertIn("只属于一条流", dup["error"])

        # 生成计划：只给第一只标的（打不通的模型 → 真实失败、零成本）
        status, plan_job = self.server.post("/api/jobs", {
            "kind": "flow_plan", "流编号": fid, "代码": [code_a],
            "api_base": "http://127.0.0.1:1", "max_chars": 12000})
        self.assertEqual(status, 200, plan_job)
        done = self._poll(plan_job["id"])
        self.assertEqual(done.get("status"), "done", done.get("result"))
        result = done.get("result") or {}
        self.assertEqual(result.get("计划数"), 1)
        row = (result.get("计划") or [])[0]
        self.assertEqual(row["代码"], code_a)
        self.assertEqual(row["打法"], "短线")
        path = os.path.join(ROOT, row["计划"])
        self.artifacts.append(path)
        self.assertTrue(os.path.exists(path))
        with io.open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertTrue(payload["研判"]["error"], "打不通的模型必须留下错误原因")
        self.assertIn("交易流", payload)
        self.assertEqual(payload["打法"], "短线")
        self.assertEqual(payload["交易流"]["本标的"]["分配资金"], float(half))
        self.assertEqual(payload["交易流"]["流内标的数"], 2)
        self.assertIn("交易流状态（成交与盈亏，权威口径）", payload["事实包文本"])
        self.assertLessEqual(payload["事实包"]["字符数"], 12000)
        # 机械打分 + 硬约束：计划就是这么算出来的（事实包最前面两章永不裁剪）
        self.assertIn("机械打分", payload)
        self.assertIn("硬约束", payload)
        self.assertIn("约束校正", payload)
        self.assertIn("机械打分（100分制", payload["事实包文本"])
        self.assertIn("硬约束（不可越界）", payload["事实包文本"])
        cons = payload["硬约束"]
        self.assertIn(cons["档位"], ("可满配", "半配", "小仓", "只减不加", "一票否决"))
        self.assertGreater(cons["亏损预算_元"], 0)
        self.assertGreater(cons["仓位金额上限_元"], 0)
        self.assertIn(cons["止损幅度上限_pct"], (3.0, 5.0, 8.0))
        self.assertEqual(cons["允许买入"], cons["档位"] not in ("只减不加", "一票否决"))

        status, detail = self.server.get("/api/flow?id=" + fid)
        self.assertEqual(status, 200, detail)
        by_code = {t["代码"]: t for t in detail["标的"]}
        self.assertEqual(detail["选中"], code_a)
        self.assertEqual(by_code[code_a]["计划"]["产物路径"], row["计划"])
        self.assertTrue(by_code[code_a]["计划"]["错误"])
        self.assertIn("机械打分", by_code[code_a])
        self.assertIn("硬约束", by_code[code_a])
        self.assertIsInstance(by_code[code_a]["约束校正"], list)
        self.assertEqual(detail["卡"]["状态"], "进行中")
        status, detail_b = self.server.get("/api/flow?id=%s&code=%s" % (fid, code_b))
        self.assertEqual(detail_b["选中"], code_b)
        self.assertIsNone(detail_b["标的"][1]["计划"]["产物路径"])
        self.assertIsNone(detail_b["标的"][1]["机械打分"])

        # 补录成交：不改计划
        status, filled = self.server.post("/api/flow/fill", {
            "流编号": fid, "代码": code_a, "方向": "买入", "价格": 10.0, "数量": 1000,
            "备注": "接口测试"})
        self.assertEqual(status, 200, filled)
        self.assertEqual(filled["持仓"]["股数"], 1000.0)
        self.assertEqual(filled["盈亏"]["已实现_元"], 0.0)
        status, err = self.server.post("/api/flow/fill", {
            "流编号": fid, "代码": code_a, "方向": "买入", "价格": 0, "数量": 100})
        self.assertEqual(status, 400)
        status, mid = self.server.get("/api/flow?id=" + fid)
        self.assertTrue([t for t in mid["标的"] if t["代码"] == code_a][0]["成交"])
        self.assertEqual([t for t in mid["标的"] if t["代码"] == code_a][0]["计划"]["产物路径"],
                         row["计划"], "补录成交不该改动计划")
        self.assertEqual([t for t in mid["标的"] if t["代码"] == code_b][0]["成交"], [],
                         "成交只记在指定的标的上")

        # 体检：同一只标的，模型打不通要如实记录失败
        status, chk_job = self.server.post("/api/jobs", {
            "kind": "flow_check", "流编号": fid, "代码": [code_a],
            "补充说明": "接口测试看有没有意外", "api_base": "http://127.0.0.1:1"})
        self.assertEqual(status, 200, chk_job)
        chk = self._poll(chk_job["id"])
        self.assertEqual(chk.get("status"), "done", chk.get("result"))
        res = chk.get("result") or {}
        self.assertTrue(res.get("error"), "模型打不通要如实记录失败")
        self.assertEqual(res.get("结论"), "未判定")
        status, detail2 = self.server.get("/api/flow?id=" + fid)
        by_code2 = {t["代码"]: t for t in detail2["标的"]}
        self.assertEqual(len(by_code2[code_a]["体检"]), 1)
        self.assertEqual(by_code2[code_a]["体检"][0]["补充说明"], "接口测试看有没有意外")
        self.assertTrue(any(e["类型"] == "体检" for e in by_code2[code_a]["事件"]))
        self.assertEqual(by_code2[code_b]["体检"], [])

        # 结束一只标的：整条流还在跑
        status, closed_a = self.server.post("/api/flow/close", {
            "流编号": fid, "代码": code_a, "原因": "接口测试结束这一只"})
        self.assertEqual(status, 200, closed_a)
        self.assertEqual(closed_a["卡"]["状态"], "进行中")
        closed_map = {t["代码"]: t["状态"] for t in closed_a["卡"]["标的"]}
        self.assertEqual(closed_map[code_a], "已结束")
        self.assertEqual(closed_map[code_b], "进行中")
        status, blocked = self.server.post("/api/flow/fill", {
            "流编号": fid, "代码": code_a, "方向": "买入", "价格": 10.0, "数量": 100})
        self.assertEqual(status, 400, "结束的标的不能再补录")

        # 移除第二只标的 → 分配资金退回；没有标的时不能再加同一只之外的操作
        status, removed = self.server.post("/api/flow/target/remove", {
            "流编号": fid, "代码": code_b})
        self.assertEqual(status, 200, removed)
        self.assertEqual(removed["卡"]["汇总"]["剩余资金"], float(self.capital - half),
                         "移除标的后它占的资金退回这条流")
        self.assertEqual(removed["卡"]["标的"][0]["分配资金"], float(half),
                         "移除别的标的不会动剩下那只的分配资金")
        # 流里剩下的那只已经结束 → 整条流自动结束，再点结束会如实拒绝
        self.assertEqual(removed["卡"]["状态"], "已结束")
        self.assertIn("结束", removed["卡"]["结束原因"])
        status, again = self.server.post("/api/flow/close", {"流编号": fid})
        self.assertEqual(status, 400)
        self.assertIn("已经结束", again["error"])
        status, listed = self.server.get("/api/flows")
        self.assertEqual(status, 200)
        self.assertNotIn(fid, [c["流编号"] for c in listed["流"]])
        self.assertIn(fid, [c["流编号"] for c in listed["已结束"]])

        status, deleted = self.server.post("/api/flow/delete", {"流编号": fid})
        self.assertEqual(status, 200, deleted)
        self.assertFalse(os.path.exists(os.path.join(FLOW_DIR, "%s.json" % fid)))
        for moved in deleted.get("移入") or []:
            target = os.path.join(ROOT, moved)
            if os.path.exists(target):
                os.remove(target)
        self.created = []

    def test_sell_more_than_position_settles(self):
        fid, _out = self._new_flow()
        code = self._add_first_target(fid)
        self.server.post("/api/flow/fill", {"流编号": fid, "代码": code, "方向": "买入",
                                            "价格": 20.0, "数量": 100})
        status, over = self.server.post("/api/flow/fill", {
            "流编号": fid, "代码": code, "方向": "卖出", "价格": 22.0, "数量": 500})
        self.assertEqual(status, 200, over)
        self.assertEqual(over["持仓"]["股数"], 0.0)
        self.assertTrue(any("超过流内持仓" in t for t in (over["提示"] or [])))
        self.assertNotEqual(over["卡"]["状态"], "进行中", "持仓归零后自动结算")
        status, listed = self.server.get("/api/flows")
        self.assertNotIn(fid, [c["流编号"] for c in listed["流"]], "唯一标的结算完 → 整条流结束")
        status, deleted = self.server.post("/api/flow/delete", {"流编号": fid})
        self.assertEqual(status, 200, deleted)
        for moved in deleted.get("移入") or []:
            target = os.path.join(ROOT, moved)
            if os.path.exists(target):
                os.remove(target)
        self.created = []

    def test_readonly_and_errors(self):
        status, body = self.server.get("/api/flows")
        self.assertEqual(status, 200, body)
        for key in ("流", "设置", "时段", "待重算", "资金", "目录", "刷新", "计数", "标的数"):
            self.assertIn(key, body, "缺少字段 %s" % key)
        status, body = self.server.get("/api/flow")
        self.assertEqual(status, 400)
        self.assertIn("id", body["error"])
        status, body = self.server.get("/api/flow?id=__missing__")
        self.assertEqual(status, 404)
        status, body = self.server.get("/api/flow?id=F-20200101-99")
        self.assertEqual(status, 404)
        status, body = self.server.post("/api/flow/target/add", {"流编号": "__missing__",
                                                                "代码": "600967"})
        self.assertEqual(status, 404)

    def test_chart_modes(self):
        """行内图：mode=minute（默认，字段向后兼容）与 mode=day（日K + 同一份关键价位）。"""
        fid, _out = self._new_flow()
        code = self._add_first_target(fid)
        status, minute = self.server.get("/api/flow/minutes?id=%s&code=%s" % (fid, code))
        self.assertEqual(status, 200, minute)
        self.assertEqual(minute["模式"], "minute")
        self.assertIn("分时", minute)
        self.assertNotIn("日K", minute)
        self.assertIn("关键价位", minute)
        self.assertIn("成交", minute)
        status, day = self.server.get("/api/flow/minutes?id=%s&code=%s&mode=day" % (fid, code))
        self.assertEqual(status, 200, day)
        self.assertEqual(day["模式"], "day")
        self.assertIn("bars", day["日K"])
        self.assertIn(day["日K"]["复权"], ("前复权", None))
        self.assertEqual(day["日K"]["根数"], len(day["日K"]["bars"]))
        self.assertIn("买卖线", day["口径"])
        self.assertEqual(day["关键价位"], minute["关键价位"], "两种模式用同一份计划价位")
        status, bad = self.server.get("/api/flow/minutes?id=%s&code=%s&mode=week" % (fid, code))
        self.assertEqual(status, 400, bad)
        self.assertIn("mode", bad["error"])
        status, missing = self.server.get("/api/flow/minutes?id=%s&code=999999" % fid)
        self.assertEqual(status, 404, missing)
        status, deleted = self.server.post("/api/flow/delete", {"流编号": fid})
        self.assertEqual(status, 200, deleted)
        for moved in deleted.get("移入") or []:
            target = os.path.join(ROOT, moved)
            if os.path.exists(target):
                os.remove(target)
        self.created = []


if __name__ == "__main__":
    unittest.main(verbosity=2)
