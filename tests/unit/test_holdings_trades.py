# -*- coding: utf-8 -*-
"""成交窗口：当日 + 近一周历史成交 的日期解析、窗口过滤与去重；凌晨/非交易日也有数据。"""

import unittest
from datetime import datetime

from _common import import_module

trades = import_module("holdings_trades")

TODAY = datetime(2026, 9, 17, 1, 30)          # 凌晨一点半：当日成交一定是空的


def row(day, seq, price=8.10, qty=100, when="09:31:00", code="601398"):
    return {"成交日期": day, "成交时间": when, "委托序号": seq, "证券代码": code,
            "成交价格": price, "成交数量": qty, "买卖标志": "买入"}


class TestTradeWindow(unittest.TestCase):

    def test_trade_date_of(self):
        self.assertEqual(trades.trade_date_of("2026-09-16"), "2026-09-16")
        self.assertEqual(trades.trade_date_of("20260916"), "2026-09-16")
        self.assertEqual(trades.trade_date_of("09-16", TODAY), "2026-09-16")
        self.assertEqual(trades.trade_date_of("260916"), "2026-09-16")
        self.assertEqual(trades.trade_date_of(""), "")
        self.assertEqual(trades.trade_date_of("垃圾"), "")
        self.assertEqual(trades.trade_date_of("2026-13-40"), "")

    def test_window_filters_and_dedupes(self):
        recent = [row("2026-09-17", "1")]
        history = [row("2026-09-16", "2"),
                   row("2026-09-16", "2"),                 # 同一笔重复出现
                   row("2026-09-11", "3"),                 # 窗口内（7 天：09-11~09-17）
                   row("2026-09-10", "4")]                 # 超窗口
        merged, info = trades.merge_trades(recent, history, today=TODAY)
        self.assertEqual([r["委托序号"] for r in merged], ["3", "2", "1"])
        self.assertEqual(info["天数"], 7)
        self.assertEqual(info["起"], "2026-09-11")
        self.assertEqual(info["当日条数"], 1)
        self.assertEqual(info["历史条数"], 4)
        self.assertEqual(info["去重后"], 3)
        self.assertEqual(info["超窗口剔除"], 1)

    def test_empty_today_still_returns_history(self):
        """凌晨/非交易日：当日成交为空，历史成交仍能落地（用户反馈的核心问题）。"""
        merged, info = trades.merge_trades([], [row("2026-09-16", "7")], today=TODAY)
        self.assertEqual(len(merged), 1)
        self.assertEqual(info["当日条数"], 0)
        self.assertEqual(merged[0]["成交日期"], "2026-09-16")

    def test_short_dates_are_normalised(self):
        merged, _info = trades.merge_trades([], [row("09-16", "8")], today=TODAY)
        self.assertEqual(merged[0]["成交日期"], "2026-09-16")


def fake_user(payload, fail_menu=""):
    calls = []

    class User:
        _config = type("Config", (), {"COMMON_GRID_CONTROL_ID": 1047})()

        def _switch_left_menus(self, path):
            calls.append(tuple(path))
            if fail_menu and "/".join(path) == fail_menu:
                raise RuntimeError("菜单不存在")

        def _get_grid_data(self, _control_id):
            return payload

    user = User()
    user.calls = calls
    return user


class TestHistoryReader(unittest.TestCase):

    def test_reads_first_working_menu(self):
        user = fake_user([{"成交日期": "2026-09-16"}])
        self.assertEqual(len(trades.read_history_trades(user)), 1)
        self.assertEqual(user.calls[0], ("查询[F4]", "历史成交"))

    def test_falls_back_to_next_menu(self):
        user = fake_user([{"成交日期": "2026-09-16"}], fail_menu="查询[F4]/历史成交")
        self.assertEqual(len(trades.read_history_trades(user)), 1)
        self.assertEqual(user.calls[1], ("查询[F4]", "成交查询"))

    def test_all_menus_fail_returns_empty(self):
        self.assertEqual(trades.read_history_trades(fake_user(None)), [])


if __name__ == "__main__":
    unittest.main()
