# -*- coding: utf-8 -*-
"""同花顺下单程序只读适配层。

只在独立 .venv-holdings 中导入 easytrader / pywinauto；主 WebUI 启动时不会加载这些
第三方包。适配层不登录、不保存凭据、不调用任何交易接口，只读取资金、持仓和近一周成交。
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime
from typing import Any, Callable, Iterable

from .holdings_trades import (HISTORY_TRADE_MENUS, TRADE_WINDOW_DAYS, merge_trades,
                              read_history_trades, trade_date_of)  # noqa: F401

EXIT_CLIENT_NOT_RUNNING = 3
EXIT_WINDOW_UNAVAILABLE = 4
EXIT_FETCH_FAILED = 5
EXIT_DEPENDENCY_MISSING = 8

DEFAULT_EXE_PATHS = (
    r"D:\ths\同花顺\xiadan.exe",
    r"C:\同花顺\xiadan.exe",
    r"C:\同花顺软件\同花顺\xiadan.exe",
    r"C:\htzqzyb2\xiadan.exe",
    r"C:\ths\xiadan.exe",
)

KEYS_BALANCE = {
    "total": ("总资产", "资产总额"),
    "available": ("可用资金", "可用金额"),
    "balance": ("资金余额", "余额"),
    "market_value": ("参考市值", "股票市值", "证券市值"),
}
KEYS_POSITION = {
    "code": ("证券代码", "股票代码"),
    "name": ("证券名称", "股票名称"),
    "amount": ("当前持仓", "股份余额", "股票余额", "持仓"),
    "available": ("股份可用", "可用余额", "可用"),
    "frozen": ("冻结数量", "冻结股份", "冻结余额"),
    "price": ("参考市价", "市价", "最新价"),
    "cost": ("参考成本价", "成本价", "持仓成本价"),
    "profit": ("参考盈亏", "盈亏", "浮动盈亏"),
    "profit_ratio": ("盈亏比例(%)", "盈亏比例", "盈亏率(%)"),
    "market_value": ("市值", "参考市值"),
    "market": ("交易市场", "市场"),
    "shareholder": ("股东代码", "股东帐号"),
    "today_profit": ("当日盈亏",),
    "today_profit_ratio": ("当日盈亏比(%)", "当日盈亏比例(%)"),
    "today_buy": ("当日买入",),
    "today_sell": ("当日卖出",),
    "hold_days": ("持股天数",),
}
KEYS_TRADES = {
    "entrust_no": ("委托序号", "合同编号", "委托编号", "成交编号"),
    "direction": ("买卖标志", "操作", "方向"),
    "code": ("证券代码", "股票代码"),
    "name": ("证券名称", "股票名称"),
    "price": ("成交价格", "成交均价"),
    "amount": ("成交数量", "成交股数"),
    "money": ("成交金额", "发生金额"),
    "date": ("成交日期", "日期"),
    "time": ("成交时间", "时间"),
    "market": ("交易市场", "市场"),
}

class SyncError(RuntimeError):
    """带稳定退出码的抓取错误。"""

    def __init__(self, message: str, code: int = EXIT_FETCH_FAILED):
        super().__init__(message)
        self.code = code

def _clean_key(value: Any) -> str:
    text = str(value or "").replace("\ufeff", "")
    text = text.replace(" ", "").replace("\u3000", "")
    text = text.replace("（", "(").replace("）", ")").replace("：", ":")
    return text.strip().lower()

def pick(row: dict, candidates: Iterable[str]) -> Any:
    """按列名取值，兼容空格、全半角和大小写差异。"""
    if not isinstance(row, dict):
        return None
    wanted = {_clean_key(x) for x in candidates}
    for key, value in row.items():
        if _clean_key(key) in wanted:
            return value
    return None

def to_num(value: Any) -> float | None:
    """宽松数值转换；空值返回 None，避免把抓取缺失误当成 0。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "").replace(" ", "").strip()
    if text in ("", "-", "--", "None", "nan", "NaN"):
        return None
    text = text.rstrip("%")
    try:
        return float(text)
    except ValueError:
        return None

def to_int(value: Any) -> int | None:
    number = to_num(value)
    return None if number is None else int(round(number))

def code6(value: Any) -> str:
    text = str(value or "").strip()
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
    if match:
        return match.group(1)
    digits = re.sub(r"\D", "", text)
    return digits if len(digits) == 6 else ""

def _rows(raw: Any) -> list[dict]:
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, (list, tuple)):
        return [x for x in raw if isinstance(x, dict)]
    return []

def _first_row(raw: Any) -> dict:
    rows = _rows(raw)
    return rows[0] if rows else {}

def _read_static(main: Any, control_id: int) -> float | None:
    try:
        text = main.child_window(control_id=control_id, class_name="Static").window_text()
    except Exception:
        return None
    text = str(text or "").strip()
    if re.match(r"^-?[\d,]+(\.\d+)?$", text):
        return to_num(text)
    return None

def normalize_balance(raw: Any, main: Any = None) -> dict:
    """把 easytrader 的资金结果归一化为稳定中文结构。"""
    row = _first_row(raw)
    total = to_num(pick(row, KEYS_BALANCE["total"]))
    available = to_num(pick(row, KEYS_BALANCE["available"]))
    balance = to_num(pick(row, KEYS_BALANCE["balance"]))
    market_value = to_num(pick(row, KEYS_BALANCE["market_value"]))
    frozen = _read_static(main, 1013) if main is not None else None
    stock_value = _read_static(main, 1014) if main is not None else None
    in_transit = _read_static(main, 1018) if main is not None else None
    return {
        "总资产": total,
        "可用资金": available,
        "资金余额": balance,
        "参考市值": market_value,
        "股票市值": stock_value if stock_value is not None else market_value,
        "冻结资金": frozen,
        "在途资金": in_transit,
    }

def _frozen_value(row: dict, amount: float | None, available: float | None) -> float | None:
    frozen = to_num(pick(row, KEYS_POSITION["frozen"]))
    if frozen is not None:
        return max(0.0, frozen)
    if amount is None or available is None:
        return None
    return max(0.0, amount - available)

def normalize_positions(raw: Any) -> list[dict]:
    """把 easytrader 持仓结果归一化为现有持仓文件的列口径。"""
    out = []
    for row in _rows(raw):
        amount = to_num(pick(row, KEYS_POSITION["amount"]))
        price = to_num(pick(row, KEYS_POSITION["price"]))
        market_value = to_num(pick(row, KEYS_POSITION["market_value"]))
        raw_code = str(pick(row, KEYS_POSITION["code"]) or "").strip()
        code = code6(raw_code)
        if not raw_code and (amount is None or amount <= 0) and (market_value is None or market_value <= 0):
            continue
        if (amount is None or amount <= 0) and (market_value is None or market_value <= 0):
            continue
        available = to_num(pick(row, KEYS_POSITION["available"]))
        if market_value is None and amount is not None and price is not None:
            market_value = amount * price
        out.append({
            "证券代码": code or raw_code,
            "原始证券代码": raw_code,
            "可纳入持仓文件": bool(code),
            "证券名称": str(pick(row, KEYS_POSITION["name"]) or "").strip(),
            "股票余额": amount,
            "可用余额": available,
            "冻结数量": _frozen_value(row, amount, available),
            "成本价": to_num(pick(row, KEYS_POSITION["cost"])),
            "市价": price,
            "盈亏": to_num(pick(row, KEYS_POSITION["profit"])),
            "盈亏比例(%)": to_num(pick(row, KEYS_POSITION["profit_ratio"])),
            "当日盈亏": to_num(pick(row, KEYS_POSITION["today_profit"])),
            "当日盈亏比(%)": to_num(pick(row, KEYS_POSITION["today_profit_ratio"])),
            "市值": market_value,
            "仓位占比(%)": None,
            "当日买入": to_num(pick(row, KEYS_POSITION["today_buy"])),
            "当日卖出": to_num(pick(row, KEYS_POSITION["today_sell"])),
            "交易市场": str(pick(row, KEYS_POSITION["market"]) or "").strip(),
            "持股天数": to_num(pick(row, KEYS_POSITION["hold_days"])),
            "股东代码": str(pick(row, KEYS_POSITION["shareholder"]) or "").strip(),
        })
    return out

def _direction(value: Any) -> str:
    text = str(value or "").strip()
    if "买" in text:
        return "买入"
    if "卖" in text:
        return "卖出"
    return text

def normalize_trades(raw: Any) -> list[dict]:
    """把 easytrader 成交页结果归一化并按委托标识去重（历史成交页同样用它）。"""
    out, seen = [], set()
    for row in _rows(raw):
        entrust_no = str(pick(row, KEYS_TRADES["entrust_no"]) or "").strip()
        code = code6(pick(row, KEYS_TRADES["code"]))
        price = to_num(pick(row, KEYS_TRADES["price"]))
        amount = to_num(pick(row, KEYS_TRADES["amount"]))
        money = to_num(pick(row, KEYS_TRADES["money"]))
        date = str(pick(row, KEYS_TRADES["date"]) or "").strip()
        time_text = str(pick(row, KEYS_TRADES["time"]) or "").strip()
        key = (date, time_text, entrust_no, code, price, amount, money)
        if key in seen:
            continue
        if not any((date, time_text, entrust_no, code, price, amount, money)):
            continue
        seen.add(key)
        out.append({
            "成交日期": date,
            "成交时间": time_text,
            "委托序号": entrust_no,
            "买卖标志": _direction(pick(row, KEYS_TRADES["direction"])),
            "证券代码": code,
            "证券名称": str(pick(row, KEYS_TRADES["name"]) or "").strip(),
            "成交价格": price,
            "成交数量": amount,
            "成交金额": money,
            "交易市场": str(pick(row, KEYS_TRADES["market"]) or "").strip(),
        })
    return out


def apply_position_ratios(positions: list[dict], balance: dict) -> None:
    total = to_num(balance.get("总资产"))
    for row in positions:
        market_value = to_num(row.get("市值"))
        row["仓位占比(%)"] = (round(market_value / total * 100, 3)
                             if market_value is not None and total else None)

def validate(balance: dict, positions: list[dict], trades: list[dict],
             previous_total: float | None = None) -> tuple[list[str], list[str]]:
    """机械校验抓取结果。返回 (errors, warnings)。"""
    errors, warnings = [], []
    total = to_num(balance.get("总资产"))
    balance_cash = to_num(balance.get("资金余额"))
    available = to_num(balance.get("可用资金"))
    stock_value = to_num(balance.get("股票市值"))
    if stock_value is None:
        stock_value = to_num(balance.get("参考市值"))
    if total is None or total <= 0:
        errors.append("总资产缺失或小于等于 0，疑似抓取失败")

    market_total = sum(to_num(x.get("市值")) or 0.0 for x in positions)
    for index, row in enumerate(positions, 1):
        raw_code = str(row.get("原始证券代码") or row.get("证券代码") or "").strip()
        code = code6(row.get("证券代码"))
        amount = to_num(row.get("股票余额"))
        row_available = to_num(row.get("可用余额"))
        price = to_num(row.get("市价"))
        market_value = to_num(row.get("市值"))
        if not code:
            if not raw_code:
                errors.append("第 %d 条持仓缺少证券代码" % index)
            else:
                warnings.append("%s 非 6 位证券代码，仅保留在原始快照，不写入持仓分析文件" % raw_code)
            continue
        if amount is None or amount <= 0:
            errors.append("%s 持仓数量缺失或小于等于 0" % (code or ("第 %d 条" % index)))
        if price is not None and price < 0:
            errors.append("%s 市价小于 0" % (code or ("第 %d 条" % index)))
        if market_value is not None and market_value < 0:
            errors.append("%s 市值小于 0" % (code or ("第 %d 条" % index)))
        if amount is not None and row_available is not None and row_available > amount + 0.01:
            warnings.append("%s 可用数量大于持仓数量，冻结数已按 0 处理" % (code or index))

    codes = [code6(x.get("证券代码")) for x in positions if code6(x.get("证券代码")) and x.get("可纳入持仓文件", True)]
    duplicates = sorted({x for x in codes if codes.count(x) > 1})
    if duplicates:
        errors.append("持仓存在重复证券代码：" + "、".join(duplicates))

    reference = stock_value if stock_value is not None else to_num(balance.get("参考市值"))
    if reference is not None and reference > 1 and not positions:
        errors.append("资金页显示股票市值 %.2f，但持仓为空，疑似抓取失败" % reference)
    if reference is not None and positions:
        tolerance = max(50.0, abs(reference) * 0.005)
        if abs(market_total - reference) > tolerance:
            warnings.append("持仓市值合计 %.2f 与资金页股票市值 %.2f 相差 %.2f"
                            % (market_total, reference, market_total - reference))

    if balance_cash is not None and available is not None and available > balance_cash + 0.01:
        warnings.append("可用资金大于资金余额，按逆回购在途/冻结处理")

    for trade in trades:
        price = to_num(trade.get("成交价格"))
        amount = to_num(trade.get("成交数量"))
        money = to_num(trade.get("成交金额"))
        if price is None or amount is None or money is None:
            warnings.append("成交 %s 存在缺失字段" % (trade.get("委托序号") or trade.get("证券代码") or "未知"))
            continue
        expected = price * amount
        if amount > 0 and abs(money - expected) > 0.5 + expected * 0.001:
            warnings.append("成交 %s 金额 %.2f 与价×量 %.2f 不一致（可能含费用）"
                            % (trade.get("证券代码") or "未知", money, expected))

    previous = to_num(previous_total)
    if previous and total and abs(total - previous) / previous > 0.2:
        warnings.append("总资产较上次快照变动超过 20%%：%.2f -> %.2f" % (previous, total))
    return errors, warnings

def _pick_window(app: Any) -> Any:
    wins = app.windows()
    for win in wins:
        try:
            title = win.window_text()
        except Exception:
            title = ""
        if "网上股票" in title or "交易系统" in title:
            return win
    return wins[0] if wins else None

def find_exe(explicit: str | None = None, cache_path: str | None = None) -> str | None:
    """按显式路径、运行进程、常见路径和缓存定位 xiadan.exe。"""
    if explicit:
        path = os.path.abspath(os.path.expandvars(os.path.expanduser(explicit)))
        if os.path.isfile(path):
            return path
        raise SyncError("指定的 xiadan.exe 不存在：%s" % path, EXIT_CLIENT_NOT_RUNNING)

    try:
        import psutil
        for proc in psutil.process_iter(["name", "exe"]):
            try:
                if (proc.info.get("name") or "").lower() == "xiadan.exe" and proc.info.get("exe"):
                    return proc.info["exe"]
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except ImportError:
        raise SyncError("缺少 psutil，请先安装 .venv-holdings", EXIT_DEPENDENCY_MISSING)

    for path in DEFAULT_EXE_PATHS:
        if os.path.isfile(path):
            return path
    if cache_path and os.path.isfile(cache_path):
        import json
        try:
            with open(cache_path, "r", encoding="utf-8") as handle:
                path = str((json.load(handle) or {}).get("exe_path") or "")
            if path and os.path.isfile(path):
                return path
        except (OSError, ValueError, TypeError):
            pass
    return None

def remember_exe(exe_path: str, cache_path: str) -> None:
    """缓存客户端路径；业务数据不进入此文件。"""
    import json
    os.makedirs(os.path.dirname(os.path.abspath(cache_path)), exist_ok=True)
    tmp = cache_path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump({"exe_path": os.path.abspath(exe_path)}, handle, ensure_ascii=False, indent=1)
        handle.write("\n")
    os.replace(tmp, cache_path)

def captcha_window_info() -> tuple[int, str] | None:
    """返回当前可见的复制验证码窗口 (hwnd, title)。

    同花顺不同版本的验证码窗口标题不稳定，因此优先按固定控件 ID 识别：
    0x965=图片、0x964=输入框。标题只作为兜底条件。
    """
    try:
        import win32gui
    except ImportError as exc:
        raise SyncError("缺少 pywin32，请先安装 .venv-holdings", EXIT_DEPENDENCY_MISSING) from exc
    found = []

    def has_captcha_controls(hwnd: int) -> bool:
        hit = {"image": False, "edit": False}

        def child_callback(child: int, _extra: Any) -> bool:
            if not win32gui.IsWindowVisible(child):
                return True
            control_id = win32gui.GetDlgCtrlID(child)
            if control_id == 0x965:
                hit["image"] = True
            elif control_id == 0x964:
                hit["edit"] = True
            return not (hit["image"] and hit["edit"])

        try:
            win32gui.EnumChildWindows(hwnd, child_callback, None)
        except Exception:
            return False
        return hit["image"] and hit["edit"]

    def callback(hwnd: int, _extra: Any) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if "股票复制识别" in title or "复制验证" in title or has_captcha_controls(hwnd):
            found.append((hwnd, title or "同花顺复制验证码"))
        return True

    win32gui.EnumWindows(callback, None)
    return found[0] if found else None

def wait_for_captcha_window(timeout: float = 3.0) -> tuple[int, str] | None:
    """短时间等待验证码弹窗出现，容忍复制后页面切换/弹窗延迟。"""
    deadline = time.monotonic() + max(0.5, timeout)
    while time.monotonic() < deadline:
        info = captcha_window_info()
        if info:
            return info
        time.sleep(0.2)
    return None

def captcha_windows() -> list[str]:
    """兼容旧调用：只返回可见验证码窗口标题。"""
    found = captcha_window_info()
    return [found[1]] if found else []

def _find_child_control(hwnd: int, control_id: int) -> int | None:
    found = []

    def callback(child: int, _extra: Any) -> bool:
        if win32gui.GetDlgCtrlID(child) == control_id and win32gui.IsWindowVisible(child):
            found.append(child)
            return False
        return True

    import win32gui
    win32gui.EnumChildWindows(hwnd, callback, None)
    return found[0] if found else None

def capture_captcha_image(hwnd: int) -> bytes:
    """按控件屏幕坐标截取验证码图片。

    pywinauto 的 capture_as_image() 对同花顺这个自绘窗口实测会返回全黑区域，
    这里改用 GetWindowRect + ImageGrab，并在单色/空白时短暂重试。
    """
    import io
    import statistics
    try:
        import win32gui
        from PIL import ImageGrab
    except ImportError as exc:
        raise SyncError("缺少 Pillow / pywin32，请先安装 .venv-holdings",
                        EXIT_DEPENDENCY_MISSING) from exc
    try:
        control_hwnd = _find_child_control(hwnd, 0x965)
        if not control_hwnd:
            raise RuntimeError("未找到验证码图片控件 0x965")
        image = None
        for _attempt in range(4):
            left, top, right, bottom = win32gui.GetWindowRect(control_hwnd)
            if right - left < 8 or bottom - top < 8:
                time.sleep(0.2)
                continue
            # 向内收 1 像素，避开控件边框。
            image = ImageGrab.grab(bbox=(left + 1, top + 1, right - 1, bottom - 1), all_screens=True)
            gray = image.convert("L")
            values = list(gray.getdata())
            if values and statistics.pstdev(values) > 3.0:
                break
            image = None
            time.sleep(0.25)
        if image is None:
            raise RuntimeError("验证码区域为空或过于单色")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        data = buffer.getvalue()
        if not data:
            raise RuntimeError("截图结果为空")
        return data
    except SyncError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SyncError("读取验证码图片失败：%s" % exc, EXIT_FETCH_FAILED) from exc

def submit_captcha(hwnd: int, code: str) -> None:
    """模拟真实键盘填写验证码并点击确定。

    同花顺验证码输入框是自绘控件：实测 WM_SETTEXT、WM_CHAR、GetWindowText
    都是空操作。因此必须点击控件后发送真实键盘事件，不能后台 set_text。
    """
    text = str(code or "").strip()
    if not text or len(text) > 12 or not all(ch.isascii() and ch.isalnum() for ch in text):
        raise SyncError("验证码格式无效", EXIT_FETCH_FAILED)
    try:
        import pywinauto.keyboard
        from pywinauto import Desktop
        current = captcha_window_info()
        if current:
            hwnd = current[0]
        window = Desktop(backend="win32").window(handle=hwnd)
        edit = window.child_window(control_id=0x964, class_name="Edit")
        button = window.child_window(control_id=1, class_name="Button")
        window.set_focus()
        time.sleep(0.15)
        edit.click_input()
        time.sleep(0.08)
        # 同花顺自绘输入框不响应 Ctrl+A，连续退格可可靠清空旧尝试残留。
        for _ in range(24):
            pywinauto.keyboard.send_keys("{BACKSPACE}")
        pywinauto.keyboard.send_keys(text, pause=0.04)
        time.sleep(0.15)
        button.click_input()
    except Exception as exc:  # noqa: BLE001
        raise SyncError("提交验证码失败：%s" % exc, EXIT_FETCH_FAILED) from exc

def close_captcha_window(timeout: float = 3.0) -> bool:
    """关闭同步前遗留的验证码窗口；返回是否已确认关闭。"""
    info = captcha_window_info()
    if not info:
        return True
    hwnd = info[0]
    try:
        import win32con
        import win32gui
        cancel = _find_child_control(hwnd, 2)
        if cancel:
            win32gui.SendMessage(cancel, win32con.BM_CLICK, 0, 0)
        else:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    except Exception:
        return False
    deadline = time.monotonic() + max(0.5, timeout)
    while time.monotonic() < deadline:
        if not captcha_window_info():
            return True
        time.sleep(0.15)
    return not bool(captcha_window_info())

def check_window(exe_path: str) -> None:
    """确认交易窗口存在且可交互；最小化时静默恢复，不抢前台焦点。"""
    import warnings
    warnings.filterwarnings("ignore", message="32-bit application should be automated using 32-bit Python")
    try:
        import win32con
        import win32gui
        from pywinauto import Application
    except ImportError as exc:
        raise SyncError("缺少 pywinauto / pywin32，请先安装 .venv-holdings",
                        EXIT_DEPENDENCY_MISSING) from exc
    try:
        app = Application().connect(path=exe_path, timeout=5)
        win = _pick_window(app)
    except Exception as exc:
        raise SyncError("连接同花顺交易窗口失败：%s" % exc, EXIT_WINDOW_UNAVAILABLE) from exc
    if win is None:
        raise SyncError("未找到交易窗口，请确认同花顺交易界面已打开并登录", EXIT_WINDOW_UNAVAILABLE)
    try:
        if not win.is_visible() or win.is_minimized():
            win32gui.ShowWindow(win.handle, win32con.SW_SHOWNOACTIVATE)
            time.sleep(1.0)
        if not win.is_visible() or win.is_minimized():
            raise SyncError("交易窗口恢复失败，请手动还原《网上股票交易系统》窗口",
                            EXIT_WINDOW_UNAVAILABLE)
    except SyncError:
        raise
    except Exception as exc:
        raise SyncError("交易窗口状态检查失败：%s" % exc, EXIT_WINDOW_UNAVAILABLE) from exc

def _silent_switch(self: Any, path: list[str], sleep: float = 1.5) -> None:
    self.close_pop_dialog()
    self._get_left_menus_handle().get_item(path).select()
    try:
        import win32con
        import win32gui
        hwnd = self._app.top_window().handle
        win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, win32con.VK_F5, 0)
        win32gui.PostMessage(hwnd, win32con.WM_KEYUP, win32con.VK_F5, 0)
    except Exception:
        pass
    self.wait(sleep)

def connect(exe_path: str) -> tuple[Any, str]:
    """连接已登录客户端，优先 universal_client，失败后回退 ths。"""
    import warnings
    warnings.filterwarnings("ignore", message="32-bit application should be automated using 32-bit Python")
    try:
        import easytrader
        from easytrader import grid_strategies
        import pywinauto.clipboard
    except ImportError as exc:
        raise SyncError("同花顺自动化依赖不完整，请重新安装 requirements-holdings.txt",
                        EXIT_DEPENDENCY_MISSING) from exc

    class SilentWMCopy(grid_strategies.WMCopy):
        def get(self, control_id: int) -> Any:
            saved = None
            try:
                saved = pywinauto.clipboard.get_data()
            except Exception:
                pass
            try:
                return super().get(control_id)
            finally:
                if saved is not None:
                    try:
                        pywinauto.clipboard.set_data(saved)
                    except Exception:
                        pass

    last_error = None
    for mode in ("universal_client", "ths"):
        try:
            user = easytrader.use(mode)
            user.connect(exe_path)
            grid_strategies.Copy._need_captcha_reg = False
            user.grid_strategy = SilentWMCopy()
            user._switch_left_menus = _silent_switch.__get__(user, type(user))
            return user, mode
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    raise SyncError("同花顺客户端连接失败：%s" % last_error, EXIT_FETCH_FAILED)

def _activate(exe_path: str) -> None:
    try:
        import win32con
        import win32gui
        from pywinauto import Application
        app = Application().connect(path=exe_path, timeout=4)
        win = _pick_window(app)
        if win is not None and (not win.is_visible() or win.is_minimized()):
            win32gui.ShowWindow(win.handle, win32con.SW_SHOWNOACTIVATE)
            time.sleep(0.5)
    except Exception:
        return

def _shape_ok(kind: str, raw: Any) -> bool:
    """检查当前网格是否真的是目标页面，防止验证码/切页失败时读到旧表。"""
    if not raw:
        return True
    if kind == "balance":
        row = _first_row(raw)
        return any(_clean_key(key) in {_clean_key(x) for x in KEYS_BALANCE["total"]} for key in row)
    rows = _rows(raw)
    if not rows:
        return False
    if kind == "position":
        return any(
            pick(row, KEYS_POSITION["code"]) is not None
            and pick(row, KEYS_POSITION["amount"]) is not None
            and pick(row, KEYS_TRADES["price"]) is None
            for row in rows
        )
    if kind == "trades":
        return any(
            (pick(row, KEYS_TRADES["entrust_no"]) is not None
             or pick(row, KEYS_TRADES["date"]) is not None
             or pick(row, KEYS_TRADES["time"]) is not None)
            and (pick(row, KEYS_TRADES["price"]) is not None
                 or pick(row, KEYS_TRADES["amount"]) is not None)
            and pick(row, KEYS_POSITION["amount"]) is None
            for row in rows
        )
    return True

def _fetch_with_retry(exe_path: str, getter: Callable[[], Any], name: str,
                      allow_empty: bool, log: Callable[[str], None], kind: str,
                      captcha_handler: Callable[[int, str], None] | None = None) -> Any:
    last_error = None
    for attempt in range(1, 3):
        if attempt > 1:
            time.sleep(5)
            _activate(exe_path)
        try:
            info = captcha_window_info()
            if info:
                if captcha_handler is None:
                    raise SyncError("检测到同花顺复制验证码：%s" % info[1], EXIT_FETCH_FAILED)
                captcha_handler(info[0], info[1])
            result = getter()
            info = wait_for_captcha_window(1.5)
            if info:
                if captcha_handler is None:
                    raise SyncError("检测到同花顺复制验证码：%s" % info[1], EXIT_FETCH_FAILED)
                captcha_handler(info[0], info[1])
                result = getter()
            if not _shape_ok(kind, result):
                info = wait_for_captcha_window(3.0)
                if info and captcha_handler is not None:
                    captcha_handler(info[0], info[1])
                    result = getter()
                if not _shape_ok(kind, result):
                    raise SyncError("页面结构不匹配，且未检测到可填写的验证码窗口",
                                    EXIT_FETCH_FAILED)
            if result or allow_empty:
                return result
            last_error = RuntimeError("返回空数据")
        except SyncError:
            raise
        except Exception as exc:  # noqa: BLE001
            last_error = exc
        log("[WARN] %s 第 %d 次尝试失败：%s" % (name, attempt, last_error))
        if attempt >= 2:
            break
    raise SyncError("%s 连续 2 次抓取失败：%s" % (name, last_error), EXIT_FETCH_FAILED)

def capture(exe_path: str | None = None, cache_path: str | None = None,
            log: Callable[[str], None] | None = None, include_trades: bool = True,
            captcha_handler: Callable[[int, str], None] | None = None) -> dict:
    """读取并归一化资金、持仓、**近 TRADE_WINDOW_DAYS 天成交**；不负责写入业务文件。

    成交口径（用户口径）：只读「历史成交」（近一周，含当日），按委托去重后写台账——
    不再单独复制「当日成交」页（少一次剪贴板复制 = 少一次验证码打断）；
    凌晨/非交易日也不会像只拉当日那样拿到空数据。
    """
    log = log or (lambda _text: None)
    exe = find_exe(exe_path, cache_path)
    if not exe:
        raise SyncError("未找到正在运行的 xiadan.exe，请先打开并登录同花顺交易界面",
                        EXIT_CLIENT_NOT_RUNNING)
    check_window(exe)
    stale_captcha = captcha_window_info()
    if stale_captcha:
        log("[WARN] 检测到同步前已存在的验证码窗口，先关闭旧窗口")
        if not close_captcha_window():
            raise SyncError("无法关闭遗留验证码窗口，请手动关闭后重试", EXIT_FETCH_FAILED)
        log("[OK] 遗留验证码窗口已关闭，继续发起本次同步")
    user, mode = connect(exe)
    log("[OK] 已连接同花顺：%s（模式 %s）" % (exe, mode))
    try:
        raw_balance = _fetch_with_retry(exe, lambda: user.balance, "资金", False, log, "balance", captcha_handler)
        raw_positions = _fetch_with_retry(exe, lambda: user.position, "持仓", True, log, "position", captcha_handler)
        if include_trades:
            # 只读「历史成交」（近一周）——用户口径：不再单独复制「当日成交」页，
            # 少一次剪贴板复制就少一次验证码打断；历史成交按委托去重后写台账。
            raw_history = read_history_trades(user, log)
        else:
            raw_history = []
            log("[OK] 本次跳过成交页，避免无意义的复制验证")
    except SyncError:
        active_now = captcha_windows()
        if active_now:
            raise SyncError("抓取被同花顺复制验证码中断：%s，请手动完成验证后重试"
                            % "；".join(active_now), EXIT_FETCH_FAILED)
        raise
    balance = normalize_balance(raw_balance, getattr(user, "main", None))
    positions = normalize_positions(raw_positions)
    trades, window = merge_trades([], normalize_trades(raw_history), code6=code6)
    apply_position_ratios(positions, balance)
    return {
        "客户端": {"路径": exe, "连接模式": mode},
        "资金": balance,
        "持仓": positions,
        "当日成交": trades,
        "成交窗口": window,
        "历史成交条数": len(normalize_trades(raw_history)),
        "抓取时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "跳过": {} if include_trades else {"当日成交": "已跳过"},
    }
