# -*- coding: utf-8 -*-
"""局域网口令解析：来源优先级与降级。

全程不启动服务、不碰真实 config/webui配置.json，全部指到临时文件。
"""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from _common import import_module

webserver = import_module("webserver")


class TestResolveLanPassword(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aiplan-pw-")
        self.path = os.path.join(self.tmp, "webui配置.json")
        patcher = mock.patch.object(webserver, "LAN_PASSWORD_FILE", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.environ.pop(webserver.LAN_PASSWORD_ENV, None)

    def write(self, text, encoding="utf-8"):
        with open(self.path, "w", encoding=encoding) as fh:
            fh.write(text)

    def test_explicit_wins(self):
        self.write('{"密码": "from-file"}')
        os.environ[webserver.LAN_PASSWORD_ENV] = "from-env"
        try:
            value, source = webserver.resolve_lan_password("from-flag")
        finally:
            os.environ.pop(webserver.LAN_PASSWORD_ENV, None)
        self.assertEqual(value, "from-flag")
        self.assertEqual(source, "--password")

    def test_env_wins_over_file(self):
        self.write('{"密码": "from-file"}')
        os.environ[webserver.LAN_PASSWORD_ENV] = "from-env"
        try:
            value, source = webserver.resolve_lan_password()
        finally:
            os.environ.pop(webserver.LAN_PASSWORD_ENV, None)
        self.assertEqual(value, "from-env")
        self.assertIn(webserver.LAN_PASSWORD_ENV, source)

    def test_file_chinese_key(self):
        self.write('{"密码": "local-secret"}')
        value, source = webserver.resolve_lan_password()
        self.assertEqual(value, "local-secret")
        self.assertIn("webui配置.json", source)

    def test_file_english_key_with_bom(self):
        self.write('{"password": "bom-secret"}', encoding="utf-8-sig")
        value, _ = webserver.resolve_lan_password()
        self.assertEqual(value, "bom-secret")

    def test_broken_json_falls_back_to_random(self):
        self.write("{ 这不是 JSON }")
        value, source = webserver.resolve_lan_password()
        self.assertGreaterEqual(len(value), 8)
        self.assertIn("随机", source)

    def test_missing_file_gives_unique_random(self):
        first, _ = webserver.resolve_lan_password()
        second, _ = webserver.resolve_lan_password()
        self.assertTrue(first.strip() and second.strip())
        self.assertNotEqual(first, second)

    def test_blank_value_treated_as_missing(self):
        self.write('{"密码": "   "}')
        value, source = webserver.resolve_lan_password()
        self.assertIn("随机", source)
        self.assertTrue(value.strip())


if __name__ == "__main__":
    unittest.main(verbosity=2)
