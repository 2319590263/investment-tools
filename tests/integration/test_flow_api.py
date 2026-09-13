# -*- coding: utf-8 -*-
"""交易流的 HTTP 接口回归：开流 / 补录 / 计划与体检查询（真实失败、零成本）/ 结束 / 删除。

写盘范围：data/ai/flows/ 下本次创建的流（结束测试时移入回收站后再删掉），以及重算计划时
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
CODE = "000001"
CANDIDATES = ("000001", "000002", "000004", "000005", "000006", "000007", "000008")


class TestFlowApi(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.server = LocalServer().start()
        cls.keep = set(os.listdir(FLOW_DIR)) if os.path.isdir(FLOW_DIR) else set()
        cls.created = []
        cls.artifacts = []
        status, account = cls.server.get("/api/account")
        total = float(((account or {}).get("配置") or {}).get("总资金") or 0)
        cls.capital = max(1000, int(total * 0.05)) if total else 1000

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        for path in cls.artifacts:
            for name in (path, path.replace("_track.json", "_track.md"),
                         path.replace("_track.json", "_track_exec.json")):
                if os.path.exists(name):
                    os.remove(name)
        if os.path.isdir(TRACK_DIR):
            for base, _dirs, _files in os.walk(TRACK_DIR, topdown=False):
                if base != TRACK_DIR and not os.listdir(base):
                    os.rmdir(base)
        pointer = os.path.join(TRACK_DIR, "latest_%s.json" % CODE)
        if os.path.exists(pointer):
            os.remove(pointer)
        # 流文件：delete 接口会移进回收站，这里把回收站里的测试残片也清掉
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

    def test_flow_lifecycle(self):
        # 用户可能已经对某个代码开过流：挑一个没有进行中流的代码来跑
        body, fid, code = None, None, None
        for cand in CANDIDATES:
            status, out = self.server.post("/api/flows", {
                "code": cand, "name": "平安银行", "本流资金": self.capital,
                "目标收益率_pct": 10, "最大亏损_pct": 5, "最大加仓次数": 1})
            if status == 200:
                body, fid, code = out, out["流编号"], cand
                break
            self.assertIn("已经开过", out.get("error", ""))
        if not fid:
            self.skipTest("候选代码都已有进行中的交易流，跳过本次生命周期用例")
        self.assertEqual(body["流编号"], fid)
        self.assertEqual(body["流"]["状态"], "进行中")
        self.created.append(os.path.join(FLOW_DIR, "%s.json" % fid))

        status, dup = self.server.post("/api/flows", {
            "code": code, "name": "平安银行", "本流资金": self.capital,
            "目标收益率_pct": 10, "最大亏损_pct": 5})
        self.assertEqual(status, 400)
        self.assertTrue("已经开过" in dup["error"] or "进行中的流" in dup["error"], dup)
        status, bad = self.server.post("/api/flows", {
            "code": "300563", "name": "神宇股份", "本流资金": 0,
            "目标收益率_pct": 10, "最大亏损_pct": 5})
        self.assertEqual(status, 400)

        status, plan_job = self.server.post("/api/jobs", {
            "kind": "flow_plan", "流编号": fid, "api_base": "http://127.0.0.1:1",
            "max_chars": 12000})
        self.assertEqual(status, 200, plan_job)
        done = self._poll(plan_job["id"])
        self.assertEqual(done.get("status"), "done", done.get("result"))
        result = done.get("result") or {}
        self.assertTrue(result.get("计划"), result)
        path = os.path.join(ROOT, result["计划"])
        self.artifacts.append(path)
        self.assertTrue(os.path.exists(path))
        with io.open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertTrue(payload["研判"]["error"], "打不通的模型必须留下错误原因")
        self.assertIn("交易流", payload)
        self.assertIn("交易流状态（成交与盈亏，权威口径）", payload["事实包文本"])
        self.assertLessEqual(payload["事实包"]["字符数"], 12000)

        status, detail = self.server.get("/api/flow?id=" + fid)
        self.assertEqual(status, 200, detail)
        self.assertEqual(detail["计划"]["产物路径"], result["计划"])
        self.assertTrue(detail["计划"]["错误"])
        self.assertEqual(detail["卡"]["状态"], "进行中")

        status, filled = self.server.post("/api/flow/fill", {
            "流编号": fid, "方向": "买入", "价格": 10.0, "数量": 1000, "备注": "接口测试"})
        self.assertEqual(status, 200, filled)
        self.assertEqual(filled["持仓"]["股数"], 1000.0)
        self.assertEqual(filled["盈亏"]["已实现_元"], 0.0)
        status, err = self.server.post("/api/flow/fill", {"流编号": fid, "方向": "买入",
                                                          "价格": 0, "数量": 100})
        self.assertEqual(status, 400)
        status, detail_mid = self.server.get("/api/flow?id=" + fid)
        self.assertTrue(detail_mid["成交"], "补录的成交要出现在详情里")
        self.assertEqual(detail_mid["计划"]["产物路径"], result["计划"],
                         "补录成交不该改动计划")

        status, chk_job = self.server.post("/api/jobs", {
            "kind": "flow_check", "流编号": fid, "补充说明": "接口测试看有没有意外",
            "api_base": "http://127.0.0.1:1"})
        self.assertEqual(status, 200, chk_job)
        chk = self._poll(chk_job["id"])
        self.assertEqual(chk.get("status"), "done", chk.get("result"))
        res = chk.get("result") or {}
        self.assertTrue(res.get("error"), "模型打不通要如实记录失败")
        self.assertEqual(res.get("结论"), "未判定")
        status, detail2 = self.server.get("/api/flow?id=" + fid)
        self.assertEqual(len(detail2["体检"]), 1)
        self.assertEqual(detail2["体检"][0]["补充说明"], "接口测试看有没有意外")
        self.assertTrue(any(e["类型"] == "体检" for e in detail2["事件"]))

        status, closed = self.server.post("/api/flow/close", {"流编号": fid, "原因": "接口测试结束"})
        self.assertEqual(status, 200, closed)
        self.assertNotEqual(closed["卡"]["状态"], "进行中")
        status, again = self.server.post("/api/flow/close", {"流编号": fid})
        self.assertEqual(status, 400)

        status, listed = self.server.get("/api/flows")
        self.assertEqual(status, 200)
        self.assertNotIn(fid, [c["流编号"] for c in listed["流"]])

        status, deleted = self.server.post("/api/flow/delete", {"流编号": fid})
        self.assertEqual(status, 200, deleted)
        self.assertFalse(os.path.exists(os.path.join(FLOW_DIR, "%s.json" % fid)))
        for moved in deleted.get("移入") or []:
            target = os.path.join(ROOT, moved)
            if os.path.exists(target):
                os.remove(target)
        self.created = []

        # 第二条流：验「卖出超过持仓」会被提示并自动结算，不再允许补录
        second, fid2 = None, None
        for cand in [c for c in CANDIDATES if c != code]:
            status, out = self.server.post("/api/flows", {
                "code": cand, "name": "神宇股份", "本流资金": self.capital,
                "目标收益率_pct": 10, "最大亏损_pct": 5})
            if status == 200:
                second, fid2 = out, out["流编号"]
                break
            self.assertIn("已经开过", out.get("error", ""))
        if not fid2:
            self.skipTest("没有可用的第二个代码，跳过成交结算用例")
        self.created.append(os.path.join(FLOW_DIR, "%s.json" % fid2))
        self.server.post("/api/flow/fill", {"流编号": fid2, "方向": "买入",
                                            "价格": 20.0, "数量": 100})
        status, over = self.server.post("/api/flow/fill", {"流编号": fid2, "方向": "卖出",
                                                          "价格": 22.0, "数量": 500})
        self.assertEqual(status, 200, over)
        self.assertEqual(over["持仓"]["股数"], 0.0)
        self.assertTrue(any("超过流内持仓" in t for t in (over["提示"] or [])))
        self.assertNotEqual(over["卡"]["状态"], "进行中", "持仓归零后自动结算")
        status, blocked = self.server.post("/api/flow/fill", {"流编号": fid2, "方向": "买入",
                                                             "价格": 20.0, "数量": 100})
        self.assertEqual(status, 400, "结束的流不能再补录")
        status, deleted2 = self.server.post("/api/flow/delete", {"流编号": fid2})
        self.assertEqual(status, 200, deleted2)
        for moved in deleted2.get("移入") or []:
            target = os.path.join(ROOT, moved)
            if os.path.exists(target):
                os.remove(target)
        self.created = []

    def test_readonly_and_errors(self):
        for path, keys in (("/api/flows", ("流", "设置", "时段", "待重算", "资金", "目录", "刷新")),):
            status, body = self.server.get(path)
            self.assertEqual(status, 200, body)
            for key in keys:
                self.assertIn(key, body, "%s 缺少字段 %s" % (path, key))
        status, body = self.server.get("/api/flow")
        self.assertEqual(status, 400)
        self.assertIn("id", body["error"])
        status, body = self.server.get("/api/flow?id=__missing__")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
