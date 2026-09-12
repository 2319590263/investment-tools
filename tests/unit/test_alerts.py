# -*- coding: utf-8 -*-
"""到价消息队列：追加 / 倒序读取 / 裁剪 / 清空（全部用临时文件，不碰真实 data/）。"""

import io
import json
import os
import shutil
import tempfile
import unittest

from _common import import_module


class TestAlerts(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.al = import_module("alerts")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alerts_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "alerts.jsonl")
        self.al.ALERTS_PATH = self.path

    def msg(self, n, kind="已到买点"):
        return {"代码": "6001%03d" % n, "名称": "测试%d" % n, "类型": kind, "价位": 10.0 + n,
                "手数": "2 手", "文案": "文案 %d" % n, "时间": "10:00:%02d" % n, "ts": 1000 + n,
                "报告路径": "data/ai/x.json"}

    def test_append_and_list_is_newest_first(self):
        self.assertEqual(self.al.append([self.msg(1), self.msg(2)]), 2)
        self.assertEqual(self.al.append([self.msg(3)]), 1)
        items = self.al.list_alerts(10)
        self.assertEqual([i["代码"] for i in items], ["6001003", "6001002", "6001001"])
        self.assertEqual(self.al.count(), 3)
        self.assertTrue(self.al.info()["路径"].endswith("alerts.jsonl"))
        self.assertEqual(self.al.info()["保留上限"], 500)

    def test_append_ignores_junk(self):
        self.assertEqual(self.al.append([]), 0)
        self.assertEqual(self.al.append([None, "x", 3]), 0)
        self.assertEqual(self.al.count(), 0)

    def test_trim_keeps_last_500(self):
        self.al.append([self.msg(n) for n in range(1, 601)])
        self.assertEqual(self.al.count(), 500)
        items = self.al.list_alerts(3)
        self.assertEqual([i["代码"] for i in items], ["6001600", "6001599", "6001598"],
                         "裁剪后应保留最新 500 条（101~600）")

    def test_limit_is_clamped(self):
        self.al.append([self.msg(n) for n in range(1, 6)])
        self.assertEqual(len(self.al.list_alerts(2)), 2)
        self.assertEqual(len(self.al.list_alerts(0)), 1)
        self.assertEqual(len(self.al.list_alerts(9999)), 5)

    def test_bad_lines_are_skipped(self):
        with io.open(self.path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(self.msg(1), ensure_ascii=False) + "\n")
            fh.write("{坏行\n")
            fh.write("\n")
        self.assertEqual(self.al.count(), 2)          # 坏行与空行不进列表，但行数按字面统计
        self.assertEqual([i["代码"] for i in self.al.list_alerts(10)], ["6001001"])

    def test_clear(self):
        self.al.append([self.msg(1), self.msg(2)])
        self.assertEqual(self.al.clear(), 2)
        self.assertEqual(self.al.list_alerts(10), [])
        self.assertEqual(self.al.count(), 0)

    def test_missing_file_is_empty(self):
        self.assertEqual(self.al.list_alerts(10), [])
        self.assertEqual(self.al.count(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
