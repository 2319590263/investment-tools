# -*- coding: utf-8 -*-
"""标的跟踪的 HTTP 接口回归。

写盘范围刻意收窄：data/user/跟踪标的.md（进测试前备份、结束后还原）与
data/ai/track/ 下本次生成的产物（结束后删除）；不碰自选股、持仓与账户配置。
模型调用用 api_base=http://127.0.0.1:1 打不通的真实失败，验证「失败也落盘、零成本」。
"""

import io
import json
import os
import shutil
import time
import unittest

from _common import ROOT, LocalServer

TRACKLIST = os.path.join(ROOT, "data", "user", "跟踪标的.md")
TRACK_DIR = os.path.join(ROOT, "data", "ai", "track")
TEST_CODE = "000001"


class TestTrackApi(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.makedirs(os.path.dirname(TRACKLIST), exist_ok=True)
        cls.had_file = os.path.exists(TRACKLIST)
        cls.original = None
        if cls.had_file:
            with io.open(TRACKLIST, encoding="utf-8") as fh:
                cls.original = fh.read()
        cls.server = LocalServer().start()
        cls.created = []

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        if cls.had_file:
            with io.open(TRACKLIST, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(cls.original)
        elif os.path.exists(TRACKLIST):
            os.remove(TRACKLIST)
        for path in cls.created:
            for name in (path, path.replace("_track.json", "_track.md"),
                         path.replace("_track.json", "_track_exec.json")):
                if os.path.exists(name):
                    os.remove(name)
        pointer = os.path.join(TRACK_DIR, "latest_%s.json" % TEST_CODE)
        if os.path.exists(pointer):
            os.remove(pointer)
        for base, dirs, _files in os.walk(TRACK_DIR, topdown=False):
            if base == TRACK_DIR:
                continue
            if not os.listdir(base):
                os.rmdir(base)

    def _add(self):
        status, body = self.server.post("/api/tracklist/add",
                                        {"code": TEST_CODE, "name": "平安银行"})
        self.assertEqual(status, 200, body)
        return body

    def test_readonly_endpoints(self):
        for path, keys in (("/api/tracklist", ("路径", "条目", "原文")),
                           ("/api/track/all", ("时段", "适用交易日", "清单", "待生成",
                                               "刷新", "目录")),
                           ("/api/track?code=600967", ("代码", "名称", "最新", "历史",
                                                       "执行摘要", "适用交易日"))):
            with self.subTest(path=path):
                status, body = self.server.get(path)
                self.assertEqual(status, 200, body)
                self.assertIsInstance(body, dict)
                for key in keys:
                    self.assertIn(key, body, "%s 缺少字段 %s" % (path, key))
        status, body = self.server.get("/api/track")
        self.assertEqual(status, 400)
        self.assertIn("code", body["error"])

    def test_list_add_remove(self):
        body = self._add()
        self.assertIn(TEST_CODE, [it["代码"] for it in body["跟踪"]])
        status, bad = self.server.post("/api/tracklist/add", {"code": "abc"})
        self.assertEqual(status, 400)
        self.assertIn("6 位", bad["error"])
        status, body = self.server.post("/api/tracklist/remove", {"code": TEST_CODE})
        self.assertEqual(status, 200, body)
        self.assertNotIn(TEST_CODE, [it["代码"] for it in body["跟踪"]])
        status, body = self.server.get("/api/tracklist")
        self.assertNotIn(TEST_CODE, [it["代码"] for it in body["条目"]])

    def test_generate_failure_still_writes_artifact(self):
        os.makedirs(TRACK_DIR, exist_ok=True)
        before = set(os.listdir(TRACK_DIR))
        self._add()
        status, models = self.server.get("/api/models")
        profile = (models or {}).get("默认_profile")
        if not profile:
            self.skipTest("模型配置里没有默认 profile")
        status, job = self.server.post("/api/jobs", {
            "kind": "track", "codes": [TEST_CODE], "profile": profile,
            "api_base": "http://127.0.0.1:1", "max_chars": 12000})
        self.assertEqual(status, 200, job)
        jid = job["id"]
        deadline = time.time() + 240
        data = {}
        while time.time() < deadline:
            status, data = self.server.get("/api/jobs/" + jid + "?from=0")
            if data.get("status") != "running":
                break
            time.sleep(1.0)
        self.assertNotEqual(data.get("status"), "running", "任务没有在 240 秒内结束")
        self.assertEqual(data.get("status"), "done", data.get("result"))
        made = (data.get("result") or {}).get("生成") or []
        self.assertEqual(len(made), 1, "应当生成 1 份产物：%s" % (data.get("result"),))
        rel_path = made[0]["路径"]
        path = os.path.join(ROOT, rel_path)
        self.created.append(path)
        self.assertTrue(os.path.exists(path))
        log_text = " ".join(line["text"] for line in data.get("lines") or [])
        self.assertIn("适用交易日", log_text)
        self.assertIn("调用", log_text)

        with io.open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertEqual(payload["phase"], "track")
        self.assertEqual(payload["标的"]["代码"], "000001.SZ")
        self.assertTrue(payload["适用交易日"])
        self.assertIn("error", payload["研判"])
        self.assertTrue(payload["研判"]["error"], "打不通的模型必须留下错误原因")
        self.assertTrue(payload["事实包"]["字符数"] > 0)
        self.assertIsInstance(payload["机械校验"], list)
        self.assertIsInstance(payload["降级"], list)
        self.assertIn("执行情况（权威口径）", payload["事实包文本"])

        status, bundle = self.server.get("/api/report?path=" + rel_path)
        self.assertEqual(status, 200, "跟踪产物要能被报告页打开")
        self.assertTrue((bundle.get("摘要") or {}).get("标的代码"))

        plan = payload["研判"]["json"]
        if plan and plan.get("计划"):
            entries = [{"编号": i + 1, "执行状态": "未执行"} for i in range(len(plan["计划"]))]
            status, saved = self.server.post("/api/track/exec",
                                             {"计划路径": rel_path, "条目": entries,
                                              "总体备注": "接口测试"})
            self.assertEqual(status, 200, saved)
            self.assertTrue(saved["执行摘要"]["是否录入"])
            status, detail = self.server.get("/api/track?code=" + TEST_CODE)
            self.assertEqual(status, 200)
            self.assertEqual(detail["最新"]["json路径"], rel_path)
            self.assertTrue(detail["执行摘要"]["是否录入"])

        status, deleted = self.server.post("/api/track/delete", {"path": rel_path})
        self.assertEqual(status, 200, deleted)
        self.assertFalse(os.path.exists(path), "删除应当是移入回收站")
        for moved in deleted.get("移入") or []:
            target = os.path.join(ROOT, moved)
            if os.path.exists(target):
                os.remove(target)
        self.created = []
        after = set(os.listdir(TRACK_DIR))
        self.assertTrue(before <= after, "原有产物不该被删：%s" % sorted(before - after))


if __name__ == "__main__":
    unittest.main(verbosity=2)
