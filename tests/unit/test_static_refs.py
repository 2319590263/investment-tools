# -*- coding: utf-8 -*-
"""前端静态资源一致性：模块入口、id 引用、注册表。

拆分后最容易踩的坑：重命名了一个 id 或忘了给视图注册，页面不报错但某个功能静默失效。
"""

import os
import re
import unittest

from _common import STATIC

JS_DIR = os.path.join(STATIC, "js")
VIEWS = ("console", "flow", "run", "report", "holdings", "watch", "pick",
         "models", "market")


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class TestStaticRefs(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.html = read(os.path.join(STATIC, "index.html"))
        cls.js = {}
        for base, _dirs, names in os.walk(JS_DIR):
            for name in names:
                if name.endswith(".js"):
                    p = os.path.join(base, name)
                    cls.js[os.path.relpath(p, JS_DIR).replace("\\", "/")] = read(p)

    def test_single_module_entry(self):
        scripts = re.findall(r'<script[^>]*src="([^"]+)"', self.html)
        self.assertEqual(scripts, ["/js/main.js"], "只允许一个模块入口")
        self.assertIn('type="module"', self.html)
        self.assertFalse(os.path.exists(os.path.join(STATIC, "app.js")), "旧的 app.js 应已删除")

    def test_ids_referenced_by_js_exist(self):
        html_ids = set(re.findall(r'id="([^"]+)"', self.html))
        js_ids, refs = set(), set()
        for text in self.js.values():
            js_ids |= set(re.findall(r'id="([^"]+)"', text))
            refs |= set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"\)', text))
        self.assertTrue(refs, "没有解析到任何 $('#id') 引用，测试本身可能失效了")
        missing = sorted(r for r in refs if r not in html_ids and r not in js_ids)
        self.assertEqual(missing, [], "引用了不存在的 id：%s" % missing)

    def test_no_duplicate_html_ids(self):
        ids = re.findall(r'id="([^"]+)"', self.html)
        dups = sorted({i for i in ids if ids.count(i) > 1})
        self.assertEqual(dups, [], "重复的 id：%s" % dups)

    def test_views_and_nav_match(self):
        for view in VIEWS:
            self.assertIn('id="view-%s"' % view, self.html, "缺少视图 view-%s" % view)
            self.assertIn('data-view="%s"' % view, self.html, "导航缺少 %s" % view)

    def test_assets_referenced_by_html_exist(self):
        rels = [r for r in re.findall(r'(?:src|href)="([^"]+)"', self.html)
                if "://" not in r and not r.startswith("#")]
        self.assertTrue(rels, "页面没有引用任何本地资源？")
        for rel in rels:
            if rel in ("/", "/m", "/m/"):
                continue
            target = os.path.join(STATIC, rel.lstrip("/").replace("/", os.sep))
            self.assertTrue(os.path.isfile(target), "静态资源不存在：%s" % rel)

    def test_no_inline_handlers(self):
        for rel, text in list(self.js.items()) + [("index.html", self.html)]:
            self.assertIsNone(re.search(r'\bon(click|change|input|keydown)\s*=', text),
                              "%s 里还有内联事件" % rel)

    def test_every_view_registers_itself(self):
        for view in VIEWS:
            rel = "views/%s.js" % view
            self.assertIn(rel, self.js, "缺少模块 %s" % rel)
            self.assertIn('registerView("%s"' % view, self.js[rel],
                          "%s 没有注册到 core/app.js" % rel)

    def test_views_do_not_import_each_other(self):
        for rel, text in self.js.items():
            if not rel.startswith("views/"):
                continue
            for target in re.findall(r'from\s+"\./([A-Za-z]+)\.js"', text):
                self.fail("%s 直接 import 了同级的 %s.js；跨页动作请走 core/app.js 的注册表" % (rel, target))


if __name__ == "__main__":
    unittest.main(verbosity=2)
