# -*- coding: utf-8 -*-
"""入口脚本可用性：main.py 各子命令、控制台入口、静态自检脚本。

这些用例只跑 --help / --list 这类「看一眼就退出」的路径，不起服务、不联网、不花钱。
"""

import os
import subprocess
import sys
import unittest

from _common import ROOT, child_env


def run(*args, timeout=300):
    return subprocess.run([sys.executable, "-X", "utf8"] + list(args), cwd=ROOT,
                          env=child_env(), stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)


class TestMainEntry(unittest.TestCase):

    def test_help_lists_subcommands(self):
        p = run("main.py", "--help")
        out = p.stdout.decode("utf-8", "replace")
        self.assertEqual(p.returncode, 0, out)
        for word in ("webui", "aiplan", "pan", "stock3d", "test", "check"):
            self.assertIn(word, out)

    def test_version(self):
        p = run("main.py", "--version")
        self.assertEqual(p.returncode, 0, p.stdout.decode("utf-8", "replace"))

    def test_unknown_subcommand_is_rejected(self):
        p = run("main.py", "definitely-not-a-command")
        self.assertEqual(p.returncode, 2)
        self.assertIn("未知子命令", p.stdout.decode("utf-8", "replace"))

    def test_aiplan_passthrough(self):
        p = run("main.py", "aiplan", "--help")
        out = p.stdout.decode("utf-8", "replace")
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("prep", out)
        self.assertIn("post", out)

    def test_pan_and_stock3d_passthrough(self):
        for tool in ("pan.py", "stock3d.py"):
            p = run(tool, "--help")
            self.assertEqual(p.returncode, 0, "%s --help 失败：%s" % (tool, p.stdout[:400]))


class TestWebuiEntrypoints(unittest.TestCase):

    def test_server_help(self):
        p = run("-m", "webui", "--help")
        out = p.stdout.decode("utf-8", "replace")
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("--port", out)
        self.assertIn("--no-browser", out)

    def test_aiplan_wrapper_help(self):
        p = run(os.path.join("src", "webui", "run_aiplan.py"), "--help")
        self.assertEqual(p.returncode, 0, p.stdout.decode("utf-8", "replace")[:400])

    def test_wrapper_does_not_touch_frozen_source(self):
        with open(os.path.join(ROOT, "src", "webui", "run_aiplan.py"), encoding="utf-8") as fh:
            wrapper = fh.read()
        self.assertIn("aiplan._ascii = _raw", wrapper)
        self.assertNotIn("open(aiplan.py", wrapper)


class TestToolingScripts(unittest.TestCase):

    def test_static_check_passes(self):
        p = run(os.path.join("scripts", "check.py"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("结果：PASS", out)

    def test_build_lists_but_never_packs_secrets(self):
        p = run(os.path.join("scripts", "build.py"), "--list")
        out = p.stdout.decode("utf-8", "replace")
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("main.py", out)
        self.assertIn("src/webui/__main__.py", out)
        for secret in ("模型配置.json", "账户配置.json", "持仓数据.md", "自选股.md"):
            self.assertNotIn(secret, out, "打包清单不能包含 %s" % secret)
        rels = [line.strip() for line in out.splitlines()]
        self.assertFalse(any(r.startswith("data/") for r in rels), "运行产物 data/ 不能进包")
        self.assertFalse(any(r.startswith("build/") for r in rels), "build/ 自身不能进包")


if __name__ == "__main__":
    unittest.main(verbosity=2)
