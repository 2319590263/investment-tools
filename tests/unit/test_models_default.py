# -*- coding: utf-8 -*-
"""切换默认 profile：只动两个键、留 .bak、非法档拒绝、内容可还原。"""

import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from _common import import_module

ORIGINAL = {
    "版本": "1",
    "默认_profile": "A档",
    "profiles_by_phase": {"prep": "", "live": "", "post": "", "all": ""},
    "profiles": {
        "A档": {"研判": {"provider": "p1", "model": "m1"}, "复核": {"provider": "p1", "model": "m2"}},
        "B档": {"研判": {"provider": "p2", "model": "x1"}, "复核": None},
    },
    "providers": [{"名称": "p1", "api_key": "secret-key-1"},
                  {"名称": "p2", "api_key": "secret-key-2"}],
    "汇率": {"USD_CNY": 7.1},
}


class TestDefaultProfile(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.store = import_module("store")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="models_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "模型配置.json")
        with io.open(self.path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(ORIGINAL, fh, ensure_ascii=False, indent=1)
        with io.open(self.path, encoding="utf-8") as fh:
            self.original = fh.read()
        p = mock.patch.object(self.store, "MODELS_PATH", self.path)
        p.start()
        self.addCleanup(p.stop)

    def text(self):
        with io.open(self.path, encoding="utf-8") as fh:
            return fh.read()

    def test_switch_default_keeps_everything_else(self):
        bundle, err = self.store.set_default_profile("B档")
        self.assertIsNone(err)
        self.assertTrue(bundle["已写入"])
        self.assertEqual(bundle["默认_profile"], "B档")
        self.assertEqual(bundle["改动"], ["默认_profile=B档"])
        now = json.loads(self.text())
        self.assertEqual(now["默认_profile"], "B档")
        self.assertEqual(now["providers"], ORIGINAL["providers"], "providers 原样保留")
        self.assertEqual(now["profiles"], ORIGINAL["profiles"], "profiles 原样保留")
        self.assertEqual(now["profiles_by_phase"], ORIGINAL["profiles_by_phase"])
        self.assertTrue(os.path.exists(self.path + ".bak"), "写回前要留 .bak")
        with io.open(self.path + ".bak", encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["默认_profile"], "A档")
        self.assertEqual([l for l in self.text().splitlines() if "默认_profile" in l][0].strip(),
                         '"默认_profile": "B档",')

    def test_same_value_writes_nothing(self):
        bundle, err = self.store.set_default_profile("A档")
        self.assertIsNone(err)
        self.assertFalse(bundle["已写入"])
        self.assertEqual(bundle["改动"], [])
        self.assertEqual(self.text(), self.original, "内容一致就不该改写文件")
        self.assertFalse(os.path.exists(self.path + ".bak"))

    def test_unknown_profile_rejected(self):
        bundle, err = self.store.set_default_profile("不存在档")
        self.assertIsNone(bundle)
        self.assertIn("profile 不存在", err)
        self.assertIn("A档", err)
        self.assertEqual(self.text(), self.original)

    def test_phase_overrides(self):
        bundle, err = self.store.set_default_profile(None, {"post": "B档", "prep": ""})
        self.assertIsNone(err)
        self.assertEqual(bundle["profiles_by_phase"]["post"], "B档")
        self.assertEqual(bundle["profiles_by_phase"]["prep"], "")
        self.assertIn("profiles_by_phase.post=B档", bundle["改动"])
        bundle, err = self.store.set_default_profile(None, {"post": ""})
        self.assertEqual(bundle["profiles_by_phase"]["post"], "")
        self.assertEqual(json.loads(self.text())["默认_profile"], "A档", "只改分时段时默认档不动")

    def test_phase_unknown_rejected(self):
        bundle, err = self.store.set_default_profile(None, {"live": "不存在档"})
        self.assertIsNone(bundle)
        self.assertIn("live", err)
        self.assertEqual(self.text(), self.original)

    def test_round_trip_restores_text(self):
        self.store.set_default_profile("B档")
        self.store.set_default_profile("A档")
        self.assertEqual(self.text(), self.original, "来回切换后内容应与原文件一致")


if __name__ == "__main__":
    unittest.main(verbosity=2)
