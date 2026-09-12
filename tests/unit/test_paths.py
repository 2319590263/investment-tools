# -*- coding: utf-8 -*-
"""目录约定、包结构与路径解析。

最容易改坏的就是这些约定：三个 CLI 留在根目录（它们以自身目录为基准找 data/ 与配置），
控制台必须能从 src/webui 包正确反推项目根，个人文件必须落在 config/ 与 data/user/。
"""

import json
import os
import unittest

from _common import DATA, ROOT, SRC, STATIC, WEBUI, import_module, import_webui

FROZEN = ("aiplan.py", "pan.py", "stock3d.py")
PERSONAL = {
    "config/账户配置.json": "账户配置",
    "config/模型配置.json": "模型配置",
    "data/user/持仓数据.md": "持仓数据",
    "data/user/自选股.md": "自选股",
}
DOCS = ("README_aiplan.md", "README_pan.md", "README_stock3d.md", "README_webui.md",
        "大盘需要的数据.txt", "个股需要的数据.txt")
PKG_MODULES = ("__init__.py", "__main__.py", "paths.py", "sources.py", "store.py",
               "archive.py", "market.py", "jobs.py", "pick.py", "pick_run.py",
               "background.py", "plancheck.py", "quotes.py", "overview.py",
               "alerts.py", "trash.py", "webserver.py", "run_aiplan.py")


class TestLayout(unittest.TestCase):

    def test_frozen_cli_stay_at_root(self):
        for name in FROZEN:
            self.assertTrue(os.path.isfile(os.path.join(ROOT, name)), "%s 必须在项目根" % name)
            self.assertFalse(os.path.exists(os.path.join(ROOT, "src", name)),
                             "%s 不能挪进 src/（会改变 data/ 与配置的查找目录）" % name)

    def test_personal_files_in_config_and_data_user(self):
        for rel in PERSONAL:
            self.assertTrue(os.path.isfile(os.path.join(ROOT, *rel.split("/"))), "%s 缺失" % rel)
        for name in ("账户配置.json", "模型配置.json", "持仓数据.md", "自选股.md"):
            self.assertFalse(os.path.isfile(os.path.join(ROOT, name)),
                             "%s 不该再堆在根目录" % name)

    def test_webui_source_layout(self):
        for name in PKG_MODULES:
            self.assertTrue(os.path.isfile(os.path.join(WEBUI, name)), "src/webui/%s 缺失" % name)
        self.assertFalse(os.path.exists(os.path.join(WEBUI, "server.py")),
                         "单文件 server.py 应已拆成包内模块")
        for rel in ("static/index.html", "static/styles.css", "static/js/main.js"):
            self.assertTrue(os.path.isfile(os.path.join(WEBUI, *rel.split("/"))), rel)

    def test_modules_stay_small(self):
        """单文件不得超过 800 行：拆包的意义就在这里，别让它重新长回去。"""
        for name in PKG_MODULES:
            if name == "run_aiplan.py":
                continue
            path = os.path.join(WEBUI, name)
            with open(path, encoding="utf-8") as fh:
                lines = sum(1 for _ in fh)
            self.assertLessEqual(lines, 800, "%s 有 %d 行，超过 800 行上限" % (name, lines))

    def test_entry_and_scripts(self):
        for rel in ("main.py", "README.md", "requirements.txt", "requirements-dev.txt",
                    ".editorconfig", ".gitignore", "启动WebUI.cmd",
                    "scripts/check.py", "scripts/build.py", "scripts/run_tests.cmd"):
            self.assertTrue(os.path.isfile(os.path.join(ROOT, *rel.split("/"))), rel)

    def test_docs_folder(self):
        for name in DOCS:
            self.assertTrue(os.path.isfile(os.path.join(ROOT, "docs", name)), name)
        self.assertFalse(os.path.isfile(os.path.join(ROOT, "README_aiplan.md")),
                         "说明文档应集中在 docs/")

    def test_config_examples_are_redacted(self):
        for name in ("账户配置.example.json", "模型配置.example.json"):
            path = os.path.join(ROOT, "config", name)
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
            self.assertIsInstance(doc, dict)
            if name.startswith("账户"):
                self.assertEqual(doc.get("总资金"), 0, "模板不能带真实资金")
            else:
                for prov in doc.get("providers") or []:
                    self.assertFalse(str(prov.get("api_key") or "").strip(), "模板不能带 Key")


class TestPackagePathResolution(unittest.TestCase):
    """webui.paths 从包位置反推 ROOT，所有路径都该落在约定的位置。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = import_webui()
        cls.P = import_module("paths")

    def test_root_and_static(self):
        self.assertEqual(os.path.abspath(self.P.ROOT), ROOT)
        self.assertEqual(os.path.abspath(self.P.WEBUI_DIR), WEBUI)
        self.assertEqual(os.path.abspath(self.P.STATIC_DIR), STATIC)
        self.assertEqual(os.path.abspath(self.P.SRC_DIR), SRC)

    def test_cli_and_personal_file_paths(self):
        self.assertEqual(self.P.AIPLAN, os.path.join(ROOT, "aiplan.py"))
        self.assertEqual(self.P.RUNNER, os.path.join(WEBUI, "run_aiplan.py"))
        for rel in PERSONAL:
            key = {"config/账户配置.json": "ACCOUNT_PATH", "config/模型配置.json": "MODELS_PATH",
                   "data/user/持仓数据.md": "POOL_PATH",
                   "data/user/自选股.md": "WATCHLIST_PATH"}[rel]
            self.assertEqual(getattr(self.P, key), os.path.join(ROOT, *rel.split("/")), key)

    def test_reused_modules_come_from_root(self):
        self.assertEqual(os.path.abspath(self.P.aiplan.__file__), os.path.join(ROOT, "aiplan.py"))
        self.assertEqual(os.path.abspath(self.P.pan.__file__), os.path.join(ROOT, "pan.py"))
        self.assertEqual(os.path.abspath(self.P.aiplan.DATA_DIR), DATA)
        self.assertEqual(self.P.aiplan.DEFAULT_ACCOUNT, os.path.join(ROOT, "config", "账户配置.json"))
        self.assertEqual(self.P.aiplan.DEFAULT_POOL, os.path.join(ROOT, "data", "user", "持仓数据.md"))

    def test_runtime_dirs_inside_data(self):
        for attr in ("AI_DIR", "PICK_DIR", "MARKET_DIR", "TRASH_DIR", "PAN_DIR", "HISTORY_DIR"):
            path = os.path.abspath(getattr(self.P, attr))
            self.assertTrue(path.startswith(DATA + os.sep), "%s 不在 data/ 下：%s" % (attr, path))

    def test_package_does_not_touch_sys_path_or_frozen_cli(self):
        """包内模块不许改 sys.path，也不许写根目录的三个 CLI。"""
        for name in PKG_MODULES:
            if not name.endswith(".py") or name == "run_aiplan.py":
                continue
            with open(os.path.join(WEBUI, name), encoding="utf-8") as fh:
                src = fh.read()
            self.assertNotIn("sys.path.insert", src, "%s 里不该有 sys.path 改写" % name)
            for frozen in FROZEN:
                self.assertNotIn("open(os.path.join(ROOT, %r), \"w\"" % frozen, src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
