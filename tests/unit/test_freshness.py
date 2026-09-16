# -*- coding: utf-8 -*-
"""数据新鲜度：过期行情快照**直接删除**（不进回收站）；新鲜数据与交易日历不动。"""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from _common import import_module


class TestFreshness(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fresh = import_module("freshness")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fresh_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = os.path.join(self.tmp, "data")
        self.trash = os.path.join(self.tmp, "trash")
        os.makedirs(os.path.join(self.data, "pan", "state"))
        patchers = [mock.patch.object(self.fresh, "DATA_DIR", self.data),
                    mock.patch.object(self.fresh, "TRASH_DIR", self.trash),
                    mock.patch.object(self.fresh, "STALE_DIR", os.path.join(self.trash, "stale")),
                    mock.patch.object(self.fresh, "_PURGE_TS", [0.0])]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def mk(self, rel_path, content="x"):
        path = os.path.join(self.data, rel_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return path

    def test_latest_trade_day_is_a_weekday(self):
        day = self.fresh.latest_trade_day()
        self.assertRegex(day, r"^\d{4}-\d{2}-\d{2}$")
        self.assertNotIn(day[-2:], ("00",))

    def test_old_snapshots_deleted_and_fresh_stays(self):
        old = self.mk("pan/20200103/latest_all.json")
        new = self.mk("pan/29990102/latest_all.json")          # 未来日期：肯定不删
        self.mk("pan/state/20200103/state.json")
        self.mk("s20200103/600967/pan.json")
        self.mk("pan/state/calendar.json")
        res = self.fresh.purge_stale(force=True)
        gone = [os.path.basename(x["路径"]) for x in res["删除"]]
        self.assertIn("20200103", gone)
        self.assertEqual(len(res["删除"]), 3, "pan 日目录 / pan state / stock3d 快照各一份")
        tags = sorted(x["类型"] for x in res["删除"])
        self.assertEqual(tags, ["pan", "panstate", "stock3d"])
        self.assertFalse(os.path.exists(os.path.dirname(old)), "过期 pan 目录要被直接删掉")
        self.assertTrue(os.path.exists(new), "新鲜快照不能被删")
        self.assertTrue(os.path.exists(os.path.join(self.data, "pan", "state", "calendar.json")),
                        "交易日历不能删")

    def test_stock3d_json_files_are_purged(self):
        """data/stock3d_<日期>.json（文件型快照）也要按同一口径清掉——老实现只认目录。"""
        old = self.mk("stock3d_20200103.json", "{}")
        fresh = self.mk("stock3d_29990102.json", "{}")          # 未来日期：不删
        other = self.mk("stock3d_notes.txt", "x")               # 不是快照命名：不动
        res = self.fresh.purge_stale(force=True)
        gone = [os.path.basename(x["路径"]) for x in res["删除"]]
        self.assertEqual(gone, ["stock3d_20200103.json"])
        self.assertEqual([x["类型"] for x in res["删除"]], ["stock3d文件"])
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))
        self.assertTrue(os.path.exists(other))
        stale = os.path.join(self.tmp, "trash", "stale")
        self.assertFalse(os.path.isdir(stale) and os.listdir(stale),
                         "过期快照不进回收站（用户明确要求）")

    def test_legacy_stale_dir_is_cleared(self):
        legacy = os.path.join(self.tmp, "trash", "stale", "pan_20200103")
        os.makedirs(legacy, exist_ok=True)
        with open(os.path.join(legacy, "latest_all.json"), "w", encoding="utf-8") as fh:
            fh.write("x")
        self.mk("pan/20200103/latest_all.json")
        res = self.fresh.purge_stale(force=True)
        self.assertEqual(len(res["删除"]), 1)
        self.assertTrue(res["历史遗留"], "历史遗留的 stale 条目也要清掉")
        self.assertFalse(os.path.exists(legacy))

    def test_throttle_skips_second_run(self):
        self.mk("pan/20200103/latest_all.json")
        self.assertNotIn("跳过", self.fresh.purge_stale())
        self.assertTrue(self.fresh.purge_stale()["跳过"])

    def test_env_flag_disables_purge(self):
        self.mk("pan/20200103/latest_all.json")
        with mock.patch.dict(os.environ, {self.fresh.NO_PURGE_ENV: "1"}):
            res = self.fresh.purge_stale(force=True)
        self.assertTrue(res["跳过"])
        self.assertTrue(os.path.isdir(os.path.join(self.data, "pan", "20200103")))


if __name__ == "__main__":
    unittest.main()
