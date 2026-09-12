# -*- coding: utf-8 -*-
"""同花顺持仓同步的纯逻辑与写盘测试（不启动客户端）。"""

import json
import os
import tempfile
import unittest
from datetime import datetime

from _common import import_module

ths = import_module("holdings_ths")
sync = import_module("holdings_sync")
paths = import_module("paths")


def fake_capture(_exe=None, _cache=None, _log=None):
    return {
        "客户端": {"路径": r"D:\ths\同花顺\xiadan.exe", "连接模式": "universal_client"},
        "资金": {
            "总资产": 50000.0,
            "可用资金": 10000.0,
            "资金余额": 9000.0,
            "参考市值": 41000.0,
            "股票市值": 41000.0,
            "冻结资金": 0.0,
            "在途资金": None,
        },
        "持仓": [{
            "证券代码": "002463", "证券名称": "沪电股份", "股票余额": 200.0,
            "可用余额": 100.0, "冻结数量": 100.0, "成本价": 125.282,
            "市价": 128.25, "盈亏": 593.52, "盈亏比例(%)": 2.369,
            "当日盈亏": 12.0, "当日盈亏比(%)": 0.1, "市值": 25650.0,
            "仓位占比(%)": 51.3, "当日买入": 100.0, "当日卖出": None,
            "交易市场": "深圳Ａ股", "持股天数": 3.0,
        }, {
            "证券代码": "512890", "证券名称": "红利低波ETF华泰柏瑞", "股票余额": 6100.0,
            "可用余额": 6100.0, "冻结数量": 0.0, "成本价": 1.2,
            "市价": 1.25, "盈亏": 300.0, "盈亏比例(%)": 4.0,
            "当日盈亏": None, "当日盈亏比(%)": None, "市值": 7625.0,
            "仓位占比(%)": 15.25, "当日买入": None, "当日卖出": None,
            "交易市场": "上海Ａ股", "持股天数": 10.0,
        }],
        "当日成交": [{
            "成交日期": "2026-09-13", "成交时间": "10:01:02", "委托序号": "12345",
            "买卖标志": "买入", "证券代码": "002463", "证券名称": "沪电股份",
            "成交价格": 128.0, "成交数量": 100.0, "成交金额": 12800.0,
            "交易市场": "深圳Ａ股",
        }],
        "抓取时间": "2026-09-13 15:10:00",
    }


class TestNormalizeAndValidate(unittest.TestCase):

    def test_normalize_raw_rows(self):
        raw_balance = [{"总资产": "50,000.00", "可用资金": "10000", "资金余额": "9000",
                        "参考市值": "41000"}]
        balance = ths.normalize_balance(raw_balance)
        self.assertEqual(balance["总资产"], 50000.0)

        raw_position = [{
            "证券代码": "002463", "证券名称": "沪电股份", "股票余额": "200",
            "可用余额": "100", "参考市价": "128.25", "参考成本价": "125.282",
            "参考盈亏": "593.52", "盈亏比例(%)": "2.369", "市值": "25650",
            "交易市场": "深圳Ａ股", "持股天数": "3",
        }]
        positions = ths.normalize_positions(raw_position)
        self.assertEqual(positions[0]["证券代码"], "002463")
        self.assertEqual(positions[0]["冻结数量"], 100.0)
        self.assertEqual(positions[0]["持股天数"], 3.0)

        raw_trades = [{"成交日期": "2026-09-13", "委托序号": "1", "买卖标志": "证券买入",
                       "证券代码": "002463", "成交价格": "128", "成交数量": "100",
                       "成交金额": "12800"}]
        trades = ths.normalize_trades(raw_trades)
        self.assertEqual(trades[0]["买卖标志"], "买入")

    def test_trading_day_prefers_local_calendar(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "data", "pan", "state"), exist_ok=True)
            path = os.path.join(tmp, "data", "pan", "state", "calendar.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"days": ["2026-09-11"]}, handle)
            self.assertTrue(sync.is_trading_day(datetime(2026, 9, 11, 10, 0), path))
            self.assertFalse(sync.is_trading_day(datetime(2026, 9, 13, 10, 0), path))
    def test_shape_check_rejects_position_rows_as_trades(self):
        position_row = {"证券代码": "002463", "股票余额": "200", "市值": "25650"}
        trade_row = {"委托序号": "1", "证券代码": "002463", "成交价格": "128",
                     "成交数量": "100", "成交金额": "12800"}
        self.assertFalse(ths._shape_ok("trades", [position_row]))
        self.assertTrue(ths._shape_ok("trades", [trade_row]))
        self.assertTrue(ths._shape_ok("position", [position_row]))
    def test_no_trades_option_skips_trade_page(self):
        result = sync.run_sync(preview=True, capture_fn=fake_capture, trading_day=True,
                               skip_trades=True, log=lambda _x: None,
                               now=datetime(2026, 9, 13, 15, 10, 0))
        self.assertEqual(result["成交数"], 0)
        self.assertEqual(result["跳过"]["当日成交"], "手动跳过")
    def test_validation_rejects_positive_market_with_empty_positions(self):
        balance = fake_capture()["资金"]
        errors, _warnings = ths.validate(balance, [], [])
        self.assertTrue(any("持仓为空" in item for item in errors))

    def test_validation_accepts_fake_capture(self):
        captured = fake_capture()
        errors, warnings = ths.validate(captured["资金"], captured["持仓"], captured["当日成交"])
        self.assertEqual(errors, [])
        self.assertIsInstance(warnings, list)


class TestCaptchaSession(unittest.TestCase):

    def test_manual_captcha_request_answer_roundtrip(self):
        with tempfile.TemporaryDirectory() as root:
            request = sync.create_captcha_request(root, b"fake-png", ttl=30)
            self.assertEqual(len(request["id"]), 32)
            self.assertEqual(sync.captcha_image(root, request["id"]), b"fake-png")
            sync.submit_captcha_answer(root, request["id"], "a1b2")
            self.assertEqual(sync.wait_captcha_answer(root, request["id"], 5, poll_seconds=0.01), "a1b2")
            paths = sync.captcha_paths(root, request["id"])
            self.assertFalse(os.path.exists(paths["answer"]))

    def test_captcha_rejects_invalid_id_and_code(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ths.SyncError):
                sync.captcha_paths(root, "../escape")
            with self.assertRaises(ths.SyncError):
                sync.create_captcha_request(root, b"", ttl=30)

class TestRenderingAndPersistence(unittest.TestCase):

    def test_holdings_round_trip(self):
        text = sync.render_holdings(fake_capture()["持仓"])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "持仓数据.md")
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            header, rows = paths.aiplan.parse_pool_rows(path)
        self.assertIn("证券代码", header)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["证券代码"], "002463")
        self.assertEqual(rows[0]["冻结数量"], "100")

    def test_ledger_is_idempotent(self):
        trades = fake_capture()["当日成交"]
        first, added1 = sync.append_ledger("", trades)
        second, added2 = sync.append_ledger(first, trades)
        self.assertEqual(added1, 1)
        self.assertEqual(added2, 0)
        self.assertEqual(first, second)
        self.assertEqual(first.count("| 12345 |"), 1)

    def _root_with_account(self):
        tmp = tempfile.TemporaryDirectory()
        os.makedirs(os.path.join(tmp.name, "config"), exist_ok=True)
        os.makedirs(os.path.join(tmp.name, "data", "user"), exist_ok=True)
        account = {
            "版本": "1", "总资金": 40000, "单票仓位上限_pct": 60,
            "说明": "keep me", "ETC_佣金最低_元": 0.5,
        }
        with open(os.path.join(tmp.name, "config", "账户配置.json"), "w", encoding="utf-8") as handle:
            json.dump(account, handle, ensure_ascii=False, indent=1)
        with open(os.path.join(tmp.name, "data", "user", "持仓数据.md"), "w", encoding="utf-8") as handle:
            handle.write("old holdings\n")
        return tmp

    def test_preview_never_writes(self):
        with self._root_with_account() as root:
            before = {}
            for base, _dirs, files in os.walk(root):
                for name in files:
                    path = os.path.join(base, name)
                    with open(path, "rb") as handle:
                        before[path] = handle.read()
            result = sync.run_sync(preview=True, capture_fn=fake_capture, root=root,
                                   now=datetime(2026, 9, 13, 15, 10, 0), log=lambda _x: None)
            after = {}
            for base, _dirs, files in os.walk(root):
                for name in files:
                    path = os.path.join(base, name)
                    with open(path, "rb") as handle:
                        after[path] = handle.read()
            self.assertTrue(result["预览"])
            self.assertEqual(before, after)

    def test_sync_updates_account_snapshot_and_ledger(self):
        with self._root_with_account() as root:
            now = datetime(2026, 9, 13, 15, 10, 0)
            first = sync.run_sync(capture_fn=fake_capture, root=root, now=now, trading_day=True, log=lambda _x: None)
            self.assertTrue(first["写入"]["持仓"]["已写入"])
            account_path = os.path.join(root, "config", "账户配置.json")
            with open(account_path, "r", encoding="utf-8") as handle:
                account = json.load(handle)
            self.assertEqual(account["总资金"], 50000.0)
            self.assertEqual(account["说明"], "keep me")
            self.assertTrue(os.path.isfile(account_path + ".bak"))
            ledger_path = os.path.join(root, "data", "user", "交易台账.md")
            with open(ledger_path, encoding="utf-8") as handle:
                self.assertEqual(handle.read().count("| 12345 |"), 1)

            second = sync.run_sync(capture_fn=fake_capture, root=root, now=now, trading_day=True, log=lambda _x: None)
            self.assertFalse(second["写入"]["持仓"]["已写入"])
            self.assertEqual(second["写入"]["台账"]["新增"], 0)
            with open(ledger_path, encoding="utf-8") as handle:
                self.assertEqual(handle.read().count("| 12345 |"), 1)
            snapshots = []
            for base, _dirs, files in os.walk(os.path.join(root, "data", "holdings", "ths", "snapshots")):
                snapshots.extend(os.path.join(base, name) for name in files if name.endswith(".json"))
            self.assertEqual(len(snapshots), 2)

    def test_validation_failure_does_not_write(self):
        with self._root_with_account() as root:
            bad = dict(fake_capture())
            bad["资金"] = dict(bad["资金"])
            bad["资金"]["总资产"] = 0
            with self.assertRaises(ths.SyncError) as caught:
                sync.run_sync(capture_fn=lambda *_args, **_kwargs: bad, root=root,
                              now=datetime(2026, 9, 13, 15, 10, 0), log=lambda _x: None)
            self.assertEqual(caught.exception.code, sync.EXIT_VALIDATION_FAILED)
            with open(os.path.join(root, "data", "user", "持仓数据.md"), encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "old holdings\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
