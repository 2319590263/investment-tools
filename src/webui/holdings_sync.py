# -*- coding: utf-8 -*-
"""同花顺持仓同步编排与 CLI。

本模块只依赖标准库。真正访问同花顺的第三方依赖在 holdings_ths.capture() 内延迟导入，
因此主 WebUI 即使没有安装 .venv-holdings 也能正常启动。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import secrets
import sys
import time
import traceback
from datetime import datetime
from glob import glob
from typing import Any, Callable

from . import holdings_ths
from .paths import ACCOUNT_PATH, POOL_PATH, ROOT, atomic_write, read_text, rel, save_like


EXIT_OK = 0
EXIT_CLIENT_NOT_RUNNING = holdings_ths.EXIT_CLIENT_NOT_RUNNING
EXIT_WINDOW_UNAVAILABLE = holdings_ths.EXIT_WINDOW_UNAVAILABLE
EXIT_FETCH_FAILED = holdings_ths.EXIT_FETCH_FAILED
EXIT_VALIDATION_FAILED = 6
EXIT_WRITE_FAILED = 7
EXIT_DEPENDENCY_MISSING = holdings_ths.EXIT_DEPENDENCY_MISSING

VENV_PYTHON = os.path.join(ROOT, ".venv-holdings", "Scripts", "python.exe")
CLIENT_CACHE = os.path.join(ROOT, "data", "holdings", "ths", "client.json")
CALENDAR_PATH = os.path.join(ROOT, "data", "pan", "state", "calendar.json")
SNAPSHOT_ROOT = os.path.join(ROOT, "data", "holdings", "ths", "snapshots")
CAPTCHA_ROOT = os.path.join(ROOT, "data", "holdings", "ths", "captcha")
CAPTCHA_TIMEOUT = 180
LEDGER_PATH = os.path.join(ROOT, "data", "user", "交易台账.md")

HOLDINGS_COLUMNS = (
    "操作", "序号", "证券代码", "证券名称", "股票余额", "可用余额", "冻结数量",
    "成本价", "市价", "盈亏", "盈亏比例(%)", "当日盈亏", "当日盈亏比(%)", "市值",
    "仓位占比(%)", "当日买入", "当日卖出", "交易市场", "持股天数",
)
LEDGER_COLUMNS = (
    "日期", "时间", "委托序号", "方向", "代码", "名称", "成交价格", "成交数量",
    "成交金额", "交易市场",
)
LEDGER_HEADER = (
    "# 交易台账（同花顺自动同步，按委托去重）\n\n"
    "| " + " | ".join(LEDGER_COLUMNS) + " |\n"
    "|" + "|".join("---" for _ in LEDGER_COLUMNS) + "|\n"
)

REQUIRED_MODULES = ("easytrader", "pywinauto", "win32gui", "psutil")


def holdings_python() -> str | None:
    """返回可选虚拟环境解释器；可由环境变量覆盖，便于测试和迁移。"""
    override = os.environ.get("THS_HOLDINGS_PYTHON")
    candidates = [override, VENV_PYTHON]
    for path in candidates:
        if path and os.path.isfile(path):
            return os.path.abspath(path)
    return None


def install_hint() -> str:
    return ("缺少同花顺自动化环境。请运行：\n"
            "  py -3.11 -m venv .venv-holdings\n"
            "  .\\.venv-holdings\\Scripts\\python.exe -m pip install -r requirements-holdings.txt")


def ensure_dependencies() -> None:
    missing = [name for name in REQUIRED_MODULES if importlib.util.find_spec(name) is None]
    if missing:
        raise holdings_ths.SyncError(
            "缺少可选依赖：%s。\n%s" % ("、".join(missing), install_hint()),
            EXIT_DEPENDENCY_MISSING,
        )


def _context(root: str | None = None) -> dict:
    base = os.path.abspath(root or ROOT)
    return {
        "root": base,
        "pool": POOL_PATH if root is None else os.path.join(base, "data", "user", "持仓数据.md"),
        "account": ACCOUNT_PATH if root is None else os.path.join(base, "config", "账户配置.json"),
        "ledger": LEDGER_PATH if root is None else os.path.join(base, "data", "user", "交易台账.md"),
        "snapshot_root": SNAPSHOT_ROOT if root is None else os.path.join(base, "data", "holdings", "ths", "snapshots"),
        "cache": CLIENT_CACHE if root is None else os.path.join(base, "data", "holdings", "ths", "client.json"),
        "calendar": CALENDAR_PATH if root is None else os.path.join(base, "data", "pan", "state", "calendar.json"),
        "captcha_root": CAPTCHA_ROOT if root is None else os.path.join(base, "data", "holdings", "ths", "captcha"),
    }


def _cell(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r", " ").replace("\n", " ").replace("|", "\\|")
    return text.strip()


def _fmt(value: Any, digits: int | None = None) -> str:
    number = holdings_ths.to_num(value)
    if number is None:
        return ""
    if digits is None:
        return ("%.6f" % number).rstrip("0").rstrip(".")
    return ("%." + str(digits) + "f") % number


def render_holdings(positions: list[dict]) -> str:
    """渲染主持仓文件；格式可被 aiplan.parse_pool_rows 无歧义读回。"""
    lines = ["| " + " | ".join(HOLDINGS_COLUMNS) + " |",
             "|" + "|".join("---" for _ in HOLDINGS_COLUMNS) + "|"]
    rows = [row for row in positions
            if row.get("可纳入持仓文件", True) and holdings_ths.code6(row.get("证券代码"))]
    for index, row in enumerate(rows, 1):
        values = [
            "", str(index), row.get("证券代码", ""), row.get("证券名称", ""),
            _fmt(row.get("股票余额")), _fmt(row.get("可用余额")), _fmt(row.get("冻结数量")),
            _fmt(row.get("成本价"), 3), _fmt(row.get("市价"), 3), _fmt(row.get("盈亏"), 2),
            _fmt(row.get("盈亏比例(%)"), 3), _fmt(row.get("当日盈亏"), 2),
            _fmt(row.get("当日盈亏比(%)"), 3), _fmt(row.get("市值"), 2),
            _fmt(row.get("仓位占比(%)"), 3), _fmt(row.get("当日买入")),
            _fmt(row.get("当日卖出")), row.get("交易市场", ""), _fmt(row.get("持股天数")),
        ]
        lines.append("| " + " | ".join(_cell(value) for value in values) + " |")
    return "\n".join(lines) + "\n"


def render_account(account_path: str, total: float) -> tuple[str, bool]:
    """更新总资金，保留账户文件其他字段与原有字段顺序。"""
    if not os.path.isfile(account_path):
        raise holdings_ths.SyncError("账户配置不存在：%s。请先运行 python main.py aiplan init-account"
                                     % rel(account_path), EXIT_WRITE_FAILED)
    try:
        data = json.loads(read_text(account_path) or "{}")
    except ValueError as exc:
        raise holdings_ths.SyncError("账户配置不是合法 JSON：%s" % exc, EXIT_WRITE_FAILED) from exc
    if not isinstance(data, dict):
        raise holdings_ths.SyncError("账户配置顶层必须是对象", EXIT_WRITE_FAILED)
    changed = holdings_ths.to_num(data.get("总资金")) != round(total, 2)
    data["总资金"] = round(total, 2)
    return json.dumps(data, ensure_ascii=False, indent=1) + "\n", changed


def _trade_key(trade: dict) -> tuple:
    date = str(trade.get("成交日期") or "").strip()
    time_text = str(trade.get("成交时间") or "").strip()
    entrust_no = str(trade.get("委托序号") or "").strip()
    direction = str(trade.get("买卖标志") or "").strip()
    code = holdings_ths.code6(trade.get("证券代码"))
    price = holdings_ths.to_num(trade.get("成交价格"))
    amount = holdings_ths.to_num(trade.get("成交数量"))
    if entrust_no:
        return ("委托", date, entrust_no, direction, code)
    raw = json.dumps([date, time_text, direction, code, price, amount,
                      holdings_ths.to_num(trade.get("成交金额"))], ensure_ascii=False,
                     sort_keys=True, default=str)
    return ("内容", hashlib.sha256(raw.encode("utf-8")).hexdigest())


def _unescape_cell(value: str) -> str:
    return value.replace("\\|", "|").strip()


def _ledger_existing_keys(text: str) -> set[tuple]:
    keys = set()
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|") or not line.endswith("|"):
            continue
        cells = [_unescape_cell(x) for x in line[1:-1].split("|")]
        if len(cells) != len(LEDGER_COLUMNS) or cells == list(LEDGER_COLUMNS):
            continue
        if all(set(cell.replace(":", "").replace("-", "").strip()) <= {" "} for cell in cells):
            continue
        trade = dict(zip(LEDGER_COLUMNS, cells))
        trade = {
            "成交日期": trade["日期"], "成交时间": trade["时间"], "委托序号": trade["委托序号"],
            "买卖标志": trade["方向"], "证券代码": trade["代码"], "证券名称": trade["名称"],
            "成交价格": trade["成交价格"], "成交数量": trade["成交数量"],
            "成交金额": trade["成交金额"], "交易市场": trade["交易市场"],
        }
        keys.add(_trade_key(trade))
    return keys


def append_ledger(existing: str, trades: list[dict]) -> tuple[str, int]:
    """返回 (新台账文本, 新增笔数)，重复同步保持幂等。"""
    text = existing or LEDGER_HEADER
    if not text.strip():
        text = LEDGER_HEADER
    keys = _ledger_existing_keys(text)
    added = []
    for trade in trades:
        key = _trade_key(trade)
        if key in keys:
            continue
        keys.add(key)
        values = [
            trade.get("成交日期", ""), trade.get("成交时间", ""), trade.get("委托序号", ""),
            trade.get("买卖标志", ""), trade.get("证券代码", ""), trade.get("证券名称", ""),
            _fmt(trade.get("成交价格"), 3), _fmt(trade.get("成交数量")),
            _fmt(trade.get("成交金额"), 2), trade.get("交易市场", ""),
        ]
        added.append("| " + " | ".join(_cell(value) for value in values) + " |")
    if not added:
        return text, 0
    return text.rstrip() + "\n" + "\n".join(added) + "\n", len(added)


def is_trading_day(when: datetime, calendar_path: str) -> bool:
    """优先按本地同花顺交易日历判断，日历缺失时退回周一至周五。"""
    day = when.date().isoformat()
    if os.path.isfile(calendar_path):
        try:
            with open(calendar_path, "r", encoding="utf-8") as handle:
                doc = json.load(handle) or {}
            days = {str(x) for x in (doc.get("days") or [])}
            if day in days:
                return True
            covered_through = str(doc.get("fetched_on") or (max(days) if days else ""))
            if covered_through and day <= covered_through:
                return False
        except (OSError, ValueError, TypeError):
            pass
    return when.weekday() < 5

CAPTCHA_PREFIX = "__HOLDINGS_CAPTCHA__ "
_CAPTCHA_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def captcha_paths(root: str, captcha_id: str) -> dict:
    if not _CAPTCHA_ID_RE.fullmatch(str(captcha_id or "")):
        raise holdings_ths.SyncError("验证码会话 ID 无效", 400)
    base = os.path.join(root, captcha_id)
    return {
        "base": base,
        "request": os.path.join(base, "request.json"),
        "image": os.path.join(base, "captcha.png"),
        "answer": os.path.join(base, "answer.json"),
    }


def _cleanup_captcha(root: str, max_age_seconds: int = 86400) -> None:
    if not os.path.isdir(root):
        return
    cutoff = time.time() - max_age_seconds
    for path in glob(os.path.join(root, "*")):
        try:
            if os.path.getmtime(path) < cutoff:
                if os.path.isdir(path):
                    for child in glob(os.path.join(path, "*")):
                        os.remove(child)
                    os.rmdir(path)
                else:
                    os.remove(path)
        except OSError:
            continue


def create_captcha_request(root: str, image: bytes, ttl: int = CAPTCHA_TIMEOUT) -> dict:
    """保存验证码图片与等待会话；返回可安全发给 WebUI 的元数据。"""
    if not image:
        raise holdings_ths.SyncError("验证码截图为空", 400)
    _cleanup_captcha(root)
    captcha_id = secrets.token_hex(16)
    paths = captcha_paths(root, captcha_id)
    os.makedirs(paths["base"], exist_ok=True)
    atomic_write(paths["image"], image)
    request = {
        "id": captcha_id,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "expires_in": int(ttl),
        "image_url": "/api/holdings/captcha?id=" + captcha_id,
    }
    atomic_write(paths["request"], json.dumps(request, ensure_ascii=False, indent=1) + "\n",
                 encoding="utf-8", newline="\n")
    return request


def captcha_image(root: str, captcha_id: str) -> bytes:
    paths = captcha_paths(root, captcha_id)
    if not os.path.isfile(paths["request"]) or not os.path.isfile(paths["image"]):
        raise holdings_ths.SyncError("验证码会话不存在或已过期", 404)
    try:
        with open(paths["image"], "rb") as handle:
            return handle.read()
    except OSError as exc:
        raise holdings_ths.SyncError("读取验证码图片失败：%s" % exc, 404) from exc


def submit_captcha_answer(root: str, captcha_id: str, code: str) -> dict:
    text = str(code or "").strip()
    if not text or len(text) > 12 or not all(ch.isalnum() for ch in text):
        raise holdings_ths.SyncError("验证码格式无效", 400)
    paths = captcha_paths(root, captcha_id)
    if not os.path.isfile(paths["request"]):
        raise holdings_ths.SyncError("验证码会话不存在或已过期", 404)
    payload = {"id": captcha_id, "code": text, "submitted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    atomic_write(paths["answer"], json.dumps(payload, ensure_ascii=False) + "\n",
                 encoding="utf-8", newline="\n")
    return {"ok": True, "id": captcha_id, "等待同花顺确认": True}


def wait_captcha_answer(root: str, captcha_id: str, timeout: int,
                        poll_seconds: float = 0.5) -> str:
    paths = captcha_paths(root, captcha_id)
    deadline = time.monotonic() + max(5, int(timeout))
    while time.monotonic() < deadline:
        if os.path.isfile(paths["answer"]):
            try:
                with open(paths["answer"], "r", encoding="utf-8") as handle:
                    code = str((json.load(handle) or {}).get("code") or "").strip()
            except (OSError, ValueError, TypeError):
                code = ""
            try:
                os.remove(paths["answer"])
            except OSError:
                pass
            if code:
                return code
        time.sleep(max(0.1, poll_seconds))
    for key in ("answer", "image", "request"):
        try:
            if os.path.isfile(paths[key]):
                os.remove(paths[key])
        except OSError:
            pass
    raise holdings_ths.SyncError("等待人工填写验证码超时（%d 秒）" % timeout, EXIT_FETCH_FAILED)


def build_captcha_handler(root: str, timeout: int, log: Callable[[str], None],
                          prompt: bool = False) -> Callable[[int, str], None]:
    """构造抓取进程侧回调：截图→通知 WebUI→等待填写→提交同花顺。"""
    def handler(hwnd: int, title: str) -> None:
        for attempt in range(1, 4):
            image = holdings_ths.capture_captcha_image(hwnd)
            request = create_captcha_request(root, image, timeout)
            marker = dict(request)
            marker["attempt"] = attempt
            marker["message"] = "同花顺要求人工填写复制验证码"
            log("[CAPTCHA] 同花顺要求人工填写验证码，已发送到 WebUI（第 %d 次）" % attempt)
            log(CAPTCHA_PREFIX + json.dumps(marker, ensure_ascii=False))
            if prompt:
                log("[CAPTCHA] 请在终端输入图片中的验证码")
                try:
                    sys.stderr.write("验证码> ")
                    sys.stderr.flush()
                    code = sys.stdin.readline().strip()
                    if not code:
                        raise EOFError("empty")
                except (EOFError, KeyboardInterrupt) as exc:
                    raise holdings_ths.SyncError("终端验证码输入已取消", EXIT_FETCH_FAILED) from exc
            else:
                code = wait_captcha_answer(root, request["id"], timeout)
            holdings_ths.submit_captcha(hwnd, code)
            time.sleep(1.0)
            if not holdings_ths.captcha_window_info():
                log("[OK] 验证码已通过，继续读取同花顺数据")
                try:
                    for path in captcha_paths(root, request["id"]).values():
                        if path.endswith(("captcha.png", "answer.json", "request.json")) and os.path.isfile(path):
                            os.remove(path)
                except OSError:
                    pass
                return
            log("[WARN] 验证码未通过，重新获取验证码图片（第 %d 次）" % attempt)
            try:
                for path in captcha_paths(root, request["id"]).values():
                    if path.endswith(("captcha.png", "answer.json", "request.json")) and os.path.isfile(path):
                        os.remove(path)
            except OSError:
                pass
        raise holdings_ths.SyncError("验证码连续 3 次未通过，已停止同步", EXIT_FETCH_FAILED)
    return handler

def previous_total(snapshot_root: str, account_path: str) -> float | None:
    """优先读取最近一次成功快照中的总资产，否则退回账户配置。"""
    files = sorted(glob(os.path.join(snapshot_root, "*", "*.json")))
    for path in reversed(files):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                value = (json.load(handle or {}) or {}).get("资金", {}).get("总资产")
            number = holdings_ths.to_num(value)
            if number is not None:
                return number
        except (OSError, ValueError, TypeError):
            continue
    try:
        data = json.loads(read_text(account_path) or "{}")
        return holdings_ths.to_num(data.get("总资金"))
    except (OSError, ValueError, TypeError):
        return None


def _snapshot_path(snapshot_root: str, when: datetime) -> str:
    directory = os.path.join(snapshot_root, when.strftime("%Y-%m-%d"))
    base = when.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(directory, base + ".json")
    serial = 1
    while os.path.exists(path):
        path = os.path.join(directory, base + "-" + str(serial) + ".json")
        serial += 1
    return path


def run_sync(preview: bool = False, exe_path: str | None = None,
             log: Callable[[str], None] | None = None,
             capture_fn: Callable[..., dict] | None = None,
             root: str | None = None, now: datetime | None = None,
             trading_day: bool | None = None, skip_trades: bool = False,
             captcha_timeout: int = CAPTCHA_TIMEOUT, captcha_prompt: bool = False) -> dict:
    """执行一次同步；preview=True 时只抓取、校验和返回，不写任何业务文件。"""
    log = log or print
    context = _context(root)
    started = now or datetime.now()
    trading = is_trading_day(started, context["calendar"]) if trading_day is None else bool(trading_day)
    fetch_trades = trading and not skip_trades
    if not preview and not os.path.isfile(context["account"]):
        raise holdings_ths.SyncError(
            "账户配置不存在：%s。请先运行 python main.py aiplan init-account" % rel(context["account"]),
            EXIT_WRITE_FAILED)
    if not preview and not os.path.isdir(os.path.dirname(context["pool"])):
        raise holdings_ths.SyncError("持仓目录不存在，未访问同花顺客户端", EXIT_WRITE_FAILED)
    if not trading:
        log("[OK] 本地交易日历判定为非交易日：跳过“当日成交”页")
    elif skip_trades:
        log("[OK] --no-trades：本次跳过“当日成交”页")
    if capture_fn is None:
        ensure_dependencies()
        log("[..] 正在读取同花顺资金和持仓%s…" % ("、当日成交" if fetch_trades else ""))
        captcha_handler = build_captcha_handler(context["captcha_root"], captcha_timeout, log,
                                               prompt=captcha_prompt)
        captured = holdings_ths.capture(exe_path, context["cache"], log,
                                        include_trades=fetch_trades,
                                        captcha_handler=captcha_handler)
    else:
        captured = capture_fn(exe_path, context["cache"], log)

    balance = captured.get("资金") or {}
    positions = captured.get("持仓") or []
    trades = (captured.get("当日成交") or []) if fetch_trades else []
    prev_total = previous_total(context["snapshot_root"], context["account"])
    errors, warnings = holdings_ths.validate(balance, positions, trades, prev_total)
    for warning in warnings:
        log("[WARN] " + warning)
    if errors:
        for error in errors:
            log("[FAIL] " + error)
        raise holdings_ths.SyncError("数据校验失败，未写入任何文件", EXIT_VALIDATION_FAILED)

    total = holdings_ths.to_num(balance.get("总资产"))
    if total is None:
        raise holdings_ths.SyncError("总资产为空，未写入任何文件", EXIT_VALIDATION_FAILED)

    result = {
        "ok": True,
        "预览": bool(preview),
        "抓取时间": captured.get("抓取时间") or started.strftime("%Y-%m-%d %H:%M:%S"),
        "交易日": trading,
        "跳过": dict(captured.get("跳过") or {}, **({} if fetch_trades else {"当日成交": ("手动跳过" if skip_trades and trading else "非交易日")})),
        "持仓数": len(positions),
        "可分析持仓数": sum(1 for row in positions if row.get("可纳入持仓文件", True) and holdings_ths.code6(row.get("证券代码"))),
        "成交数": len(trades),
        "总资产": round(total, 2),
        "可用资金": holdings_ths.to_num(balance.get("可用资金")),
        "告警": warnings,
        "写入": {},
    }
    log("[OK] 抓取校验通过：持仓 %d 项（写入分析文件 %d 只），当日成交 %d 笔，总资产 %.2f 元"
        % (len(positions), result["可分析持仓数"], len(trades), total))
    if preview:
        log("[OK] 预览完成，未写入持仓文件、账户配置、快照或交易台账")
        return result

    captured_exe = ((captured.get("客户端") or {}).get("路径") or "").strip()
    if captured_exe:
        try:
            holdings_ths.remember_exe(captured_exe, context["cache"])
        except OSError as exc:
            log("[WARN] 无法缓存 xiadan.exe 路径：%s" % exc)

    holdings_text = render_holdings(positions)
    account_text, account_changed = render_account(context["account"], total)
    ledger_text, ledger_added = append_ledger(read_text(context["ledger"]), trades)
    snapshot = {
        "版本": 1,
        "来源": "同花顺 xiadan.exe（只读）",
        "抓取时间": result["抓取时间"],
        "客户端": captured.get("客户端") or {},
        "资金": balance,
        "持仓": positions,
        "当日成交": trades,
        "跳过": result["跳过"],
        "校验": {"通过": True, "错误": [], "告警": warnings},
        "预览": False,
    }
    snapshot_path = _snapshot_path(context["snapshot_root"], started)
    snapshot["写入路径"] = {
        "持仓": rel(context["pool"]), "账户": rel(context["account"]), "台账": rel(context["ledger"]),
    }
    try:
        # save_like 先比较内容，变化时备份原文件，再原子替换；不要提前 atomic_write。
        written, backup_path = save_like(context["pool"], holdings_text)
        result["写入"]["持仓"] = {"已写入": written, "备份": rel(backup_path) if backup_path else None}
        if account_changed:
            account_written, account_backup = save_like(context["account"], account_text)
            result["写入"]["账户"] = {
                "已写入": account_written,
                "备份": rel(account_backup) if account_backup else None,
                "总资金": round(total, 2),
            }
        else:
            result["写入"]["账户"] = {"已写入": False, "备份": None, "总资金": round(total, 2)}
        if ledger_added:
            ledger_written, ledger_backup = save_like(context["ledger"], ledger_text)
            result["写入"]["台账"] = {
                "已写入": ledger_written,
                "备份": rel(ledger_backup) if ledger_backup else None,
                "新增": ledger_added,
            }
        else:
            result["写入"]["台账"] = {"已写入": False, "备份": None, "新增": 0}
        atomic_write(snapshot_path, json.dumps(snapshot, ensure_ascii=False, indent=2, default=str) + "\n",
                     encoding="utf-8", newline="\n")
        result["写入"]["快照"] = rel(snapshot_path)
    except OSError as exc:
        raise holdings_ths.SyncError("写入同步结果失败：%s" % exc, EXIT_WRITE_FAILED) from exc

    if result["写入"]["持仓"]["已写入"]:
        log("[OK] 持仓文件已更新，旧文件保留为 .bak")
    else:
        log("[OK] 持仓文件无变化")
    if account_changed:
        log("[OK] 账户总资金已更新为 %.2f 元，旧配置保留为 .bak" % total)
    else:
        log("[OK] 账户总资金无变化：%.2f 元" % total)
    if ledger_added:
        log("[OK] 交易台账新增 %d 笔" % ledger_added)
    else:
        log("[OK] 交易台账无新增（已按委托去重）")
    log("[OK] 快照已保存：%s" % rel(snapshot_path))
    log("[OK] 同步完成")
    return result


def _emit_failure(message: str, code: int, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps({"ok": False, "退出码": code, "错误": message}, ensure_ascii=False))
    else:
        print("[FAIL] " + message, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从已登录的同花顺客户端只读同步资金、持仓与当日成交")
    parser.add_argument("command", nargs="?", choices=("sync",), default="sync")
    parser.add_argument("--exe", metavar="PATH", help="xiadan.exe 路径；默认自动定位")
    parser.add_argument("--preview", action="store_true", help="只抓取和校验，不写任何文件")
    parser.add_argument("--no-trades", action="store_true", help="本次跳过当日成交页，减少复制验证")
    parser.add_argument("--captcha-timeout", type=int, default=CAPTCHA_TIMEOUT,
                        help="WebUI 等待人工填写验证码的秒数（默认 %d）" % CAPTCHA_TIMEOUT)
    parser.add_argument("--no-captcha-prompt", action="store_true",
                        help="不读取终端输入，等待 WebUI 提交验证码")
    parser.add_argument("--json", action="store_true", help="最终结果输出 JSON")
    args = parser.parse_args(argv)

    logs: list[str] = []

    def collect(line: str) -> None:
        if args.json:
            logs.append(line)
        else:
            print(line, flush=True)

    try:
        result = run_sync(preview=args.preview, exe_path=args.exe, log=collect,
                          skip_trades=args.no_trades, captcha_timeout=args.captcha_timeout,
                          captcha_prompt=not args.no_captcha_prompt)
        if args.json:
            if logs and not args.preview:
                # JSON 模式保持 stdout 可机器解析，简要过程写 stderr.
                for line in logs:
                    print(line, file=sys.stderr)
            print(json.dumps(result, ensure_ascii=False, default=str))
        return EXIT_OK
    except holdings_ths.SyncError as exc:
        _emit_failure(str(exc), exc.code, args.json)
        return exc.code
    except Exception as exc:  # noqa: BLE001
        if args.json:
            _emit_failure("未捕获异常：%s\n%s" % (exc, traceback.format_exc()), EXIT_FETCH_FAILED, True)
        else:
            print("[FAIL] 未捕获异常：%s" % exc, file=sys.stderr)
            traceback.print_exc()
        return EXIT_FETCH_FAILED


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
