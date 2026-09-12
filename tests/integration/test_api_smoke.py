# -*- coding: utf-8 -*-
"""HTTP 接口回归：真实起一个服务进程，逐个打只读接口。

刻意只碰「只读 + 零成本」的路径：不触发模型调用、不写回任何配置文件、
不改动 data/ 下的产物（唯一会写盘的是 /api/trash 的惰性清理，那是既有行为）。
"""

import json
import unittest

from _common import LocalServer

GET_KEYS = {
    "/api/state": ("根目录", "python", "时间", "账户", "模型", "持仓"),
    "/api/holdings": ("路径", "存在", "列名", "持仓", "汇总", "原文"),
    "/api/account": ("路径", "配置", "原文", "模板字段", "字段说明"),
    "/api/models": ("路径", "原文", "profiles", "providers", "默认_profile"),
    "/api/reports": ("items",),
    "/api/history": ("plan_log", "reports"),
    "/api/symbols": ("items",),
    "/api/market": ("pan", "stock3d"),
    "/api/market/forecast": ("forecast", "路径", "mtime"),
    "/api/trash": ("items", "目录", "过期天数", "上次清理"),
    "/api/pick": ("json路径", "md", "json", "mtime"),
    "/api/pick/list": ("items", "目录", "每板块候选默认", "候选上限默认", "概念上限"),
    "/api/watchlist": ("路径", "条目", "原文"),
    "/api/report": ("json路径", "摘要", "json", "md", "mtime"),
}


class TestApiSmoke(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.server = LocalServer().start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def test_readonly_endpoints(self):
        for path, keys in GET_KEYS.items():
            with self.subTest(path=path):
                status, body = self.server.get(path)
                self.assertEqual(status, 200, "%s -> %s" % (path, body))
                self.assertIsInstance(body, dict)
                for key in keys:
                    self.assertIn(key, body, "%s 缺少字段 %s" % (path, key))

    def test_report_has_markdown_and_json(self):
        status, body = self.server.get("/api/report")
        self.assertEqual(status, 200)
        self.assertTrue(body["md"].strip())
        self.assertIsInstance(body["json"], (dict, list))
        self.assertTrue(body["摘要"], "摘要不该为空")

    def test_static_page_and_assets(self):
        status, html = self.server.get("/", raw=True)
        self.assertEqual(status, 200)
        self.assertIn("view-pick", html)
        self.assertIn("/js/main.js", html, "入口应是唯一模块脚本")
        for path, needle in (("/js/main.js", "import"), ("/js/views/pick.js", "registerView"),
                             ("/styles.css", ".view"), ("/favicon.svg", "<svg")):
            with self.subTest(path=path):
                status, text = self.server.get(path, raw=True)
                self.assertEqual(status, 200, path)
                self.assertIn(needle, text)
        status, _ = self.server.get("/app.js", raw=True)
        self.assertEqual(status, 404, "旧的单文件 app.js 应该已下线")

    def test_pick_boards_shape(self):
        status, body = self.server.get("/api/pick/boards", timeout=300)
        if status == 502:
            self.skipTest("东财接口不可用，跳过（降级路径由界面提示）")
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["一级行业"]), 31)
        self.assertTrue(body["概念"])
        self.assertTrue(all("主题" in c for c in body["概念"]))

    def test_path_escape_is_forbidden(self):
        for bad in ("../aiplan.py", "..%2Faiplan.py", "C:/Windows/win.ini"):
            with self.subTest(path=bad):
                status, body = self.server.get("/api/blob?path=" + bad)
                self.assertEqual(status, 403, body)

    def test_unknown_endpoint_404(self):
        status, body = self.server.get("/api/nope")
        self.assertEqual(status, 404)

    def test_pick_job_requires_selection(self):
        status, body = self.server.post("/api/jobs", {"kind": "pick"})
        self.assertEqual(status, 400, body)
        self.assertFalse(body.get("ok", True))
        self.assertIn("请先筛选", body.get("error", ""))

    def test_cancel_unknown_job(self):
        status, body = self.server.post("/api/jobs/not-a-real-job/cancel", {})
        self.assertEqual(status, 200, body)
        self.assertFalse(body["ok"], "未知任务的取消请求应返回 ok=false")

    def test_plancheck_mechanical(self):
        """默认取最新报告做机械复核：时段、实盘价、逐条判定都要在。"""
        status, body = self.server.get("/api/plancheck")
        if status == 404:
            self.skipTest("还没有报告")
        self.assertEqual(status, 200, body)
        for key in ("报告", "实盘", "时段", "机械", "关系", "提示"):
            self.assertIn(key, body)
        self.assertTrue(body["时段"]["名称"])
        self.assertIsInstance(body["机械"]["计划"], list)
        self.assertTrue(body["机械"]["一句话"])
        self.assertIn("止损", body["机械"]["关键价位"])

    def test_plancheck_rejects_outside_paths(self):
        for bad in ("aiplan.py", "../aiplan.py", "data/ai/19700101/000000_post.json"):
            with self.subTest(path=bad):
                status, body = self.server.get("/api/plancheck?report=" + bad)
                self.assertEqual(status, 400, body)

    def test_plancheck_job_requires_report(self):
        status, body = self.server.post("/api/jobs", {"kind": "plancheck"})
        self.assertEqual(status, 400, body)
        self.assertIn("report", body.get("error", ""))

    def test_overview_structure(self):
        """总控台数据：卡片、汇总、时段、刷新元信息都要在（默认本地口径，不联网）。"""
        status, body = self.server.get("/api/overview")
        self.assertEqual(status, 200, body)
        for key in ("时段", "刷新", "汇总", "持仓", "自选", "提醒", "路径"):
            self.assertIn(key, body)
        self.assertTrue(body["时段"]["名称"])
        self.assertIn(body["刷新"]["模式"], ("本地", "实时"))
        self.assertIsInstance(body["持仓"], list)
        self.assertIsInstance(body["自选"], list)
        for card in body["持仓"] + body["自选"]:
            for field in ("代码", "名称", "现价", "价格来源", "计划", "触发"):
                self.assertIn(field, card)
        if body["持仓"]:
            card = body["持仓"][0]
            for field in ("成本价", "持仓市值_元", "浮动盈亏_元", "可用股数_可卖"):
                self.assertIn(field, card)

    def test_alerts_queue(self):
        """消息队列接口（只读；清空接口有破坏性，留给单元测试用临时文件验证）。"""
        status, body = self.server.get("/api/alerts?limit=5")
        self.assertEqual(status, 200, body)
        self.assertIn("items", body)
        self.assertIn("队列", body)
        self.assertIn("路径", body["队列"])
        self.assertIn("保留上限", body["队列"])
        self.assertLessEqual(len(body["items"]), 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)

