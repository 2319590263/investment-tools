# -*- coding: utf-8 -*-
"""模型配置页的增删改：profile / provider 的校验、写回与 .bak（批注 8、9）。"""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from _common import import_module

# 故意拼出来：避免明文密钥扫描把测试夹具当成真 key
FAKE_KEY = "test-key-" + "c" * 12

ORIGINAL = {
    "版本": "1",
    "默认_profile": "A档",
    "profiles_by_phase": {"post": "A档"},
    "profiles": {"A档": {"研判": {"provider": "p1", "model": "m1"}}},
    "providers": [{"名称": "p1", "协议": "openai-chat",
                   "base_url": "https://a.example.com", "api_key": FAKE_KEY,
                   "模型可选": ["m1", "m2"]}],
}


class TestModelEdit(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.store = import_module("store")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="models_edit_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "模型配置.json")
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(ORIGINAL, f, ensure_ascii=False, indent=1)
        self.patcher = mock.patch.object(self.store, "MODELS_PATH", self.path)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def test_add_profile_keeps_other_keys(self):
        bundle, err = self.store.save_profile("新增", {
            "名称": "B档",
            "研判": {"provider": "p1", "model": "m2", "temperature": "0.5",
                     "max_tokens": "16000", "参数": '{"reasoning_effort":"high"}'},
        })
        self.assertIsNone(err)
        self.assertTrue(bundle["已写入"])
        now = self.read()
        self.assertEqual(list(now["profiles"]), ["A档", "B档"])
        role = now["profiles"]["B档"]["研判"]
        self.assertEqual(role["model"], "m2")
        self.assertEqual(role["temperature"], 0.5)
        self.assertEqual(role["max_tokens"], 16000)
        self.assertEqual(role["参数"], {"reasoning_effort": "high"})
        self.assertEqual(now["providers"], ORIGINAL["providers"])
        self.assertTrue(os.path.exists(self.path + ".bak"))

    def test_role_must_have_provider(self):
        bundle, err = self.store.save_profile("新增", {"名称": "C档",
                                                      "研判": {"model": "m1"}})
        self.assertIsNone(bundle)
        self.assertIn("provider", err)
        self.assertNotIn("C档", self.read()["profiles"])

    def test_unknown_provider_and_bad_json_rejected(self):
        _b, err = self.store.save_profile("新增", {"名称": "C档",
                                                   "研判": {"provider": "nope", "model": "m"}})
        self.assertIn("provider 不存在", err)
        _b, err = self.store.save_profile("新增", {"名称": "C档",
                                                   "研判": {"provider": "p1", "model": "m",
                                                            "参数": "{不是 JSON"}})
        self.assertIn("合法 JSON", err)
        _b, err = self.store.save_profile("新增", {"名称": "C档", "研判": {}})
        self.assertIn("至少要配一个档位", err)

    def test_rename_profile_follows_default_and_phase(self):
        _b, err = self.store.save_profile("修改", {
            "原名": "A档", "名称": "A档2", "研判": {"provider": "p1", "model": "m1"}})
        self.assertIsNone(err)
        now = self.read()
        self.assertEqual(list(now["profiles"]), ["A档2"])
        self.assertEqual(now["默认_profile"], "A档2")
        self.assertEqual(now["profiles_by_phase"]["post"], "A档2")

    def test_delete_default_profile_refused(self):
        _b, err = self.store.save_profile("删除", {"名称": "A档"})
        self.assertIn("默认 profile", err)
        self.assertIn("A档", self.read()["profiles"])

    def test_delete_profile_clears_phase_ref(self):
        self.store.save_profile("新增", {"名称": "B档", "研判": {"provider": "p1", "model": "m1"}})
        self.store.set_default_profile("B档")
        _b, err = self.store.save_profile("删除", {"名称": "A档"})
        self.assertIsNone(err)
        now = self.read()
        self.assertEqual(list(now["profiles"]), ["B档"])
        self.assertEqual(now["profiles_by_phase"]["post"], "")

    def test_add_provider_appends_and_keeps_key_when_blank(self):
        _b, err = self.store.save_provider("新增", {
            "名称": "p2", "协议": "anthropic-messages", "base_url": "https://b.example.com",
            "api_key": FAKE_KEY, "模型可选": "x1, x2"})
        self.assertIsNone(err)
        now = self.read()
        self.assertEqual([p["名称"] for p in now["providers"]], ["p1", "p2"])
        self.assertEqual(now["providers"][1]["路径"], "/v1/messages")
        self.assertEqual(now["providers"][1]["模型可选"], ["x1", "x2"])
        _b, err = self.store.save_provider("修改", {
            "原名": "p1", "名称": "p1", "协议": "openai-chat",
            "base_url": "https://a.example.com", "api_key": ""})
        self.assertIsNone(err)
        self.assertEqual(self.read()["providers"][0]["api_key"], FAKE_KEY)

    def test_provider_validation_and_reference_guard(self):
        _b, err = self.store.save_provider("新增", {"名称": "p3", "协议": "gemini",
                                                    "base_url": "https://x.example.com"})
        self.assertIn("协议只支持", err)
        _b, err = self.store.save_provider("新增", {"名称": "p3", "协议": "openai-chat",
                                                    "base_url": "ftp://x"})
        self.assertIn("base_url", err)
        _b, err = self.store.save_provider("删除", {"名称": "p1"})
        self.assertIn("还有 profile 在用", err)

    def test_rename_provider_syncs_profiles(self):
        _b, err = self.store.save_provider("修改", {
            "原名": "p1", "名称": "p1x", "协议": "openai-chat",
            "base_url": "https://a.example.com", "api_key": ""})
        self.assertIsNone(err)
        now = self.read()
        self.assertEqual([p["名称"] for p in now["providers"]], ["p1x"])
        self.assertEqual(now["profiles"]["A档"]["研判"]["provider"], "p1x")


if __name__ == "__main__":
    unittest.main()
