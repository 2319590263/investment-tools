# -*- coding: utf-8 -*-
"""路径真源与文件读写工具：唯一一处写死目录结构的地方。

同时是唯一一处「把根目录的 aiplan.py / pan.py 当模块加载」的地方：用 importlib
按文件路径加载，不动 sys.path；网页端只读复用它们的解析与取数函数，不触发抓数。
"""

import importlib.util
import os
import sys
from datetime import datetime


def _load_cli_module(name, path):
    """按文件路径加载项目根目录的 CLI 脚本模块（只读复用）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


WEBUI_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.dirname(WEBUI_DIR)
ROOT = os.path.dirname(SRC_DIR)

# 复用根目录 CLI 的解析/取数函数：解析函数是纯函数，取数函数只在荐股/快照里被调用。
aiplan = _load_cli_module("aiplan", os.path.join(ROOT, "aiplan.py"))
pan = _load_cli_module("pan", os.path.join(ROOT, "pan.py"))


STATIC_DIR = os.path.join(WEBUI_DIR, "static")


PYTHON = sys.executable


AIPLAN = os.path.join(ROOT, "aiplan.py")


RUNNER = os.path.join(WEBUI_DIR, "run_aiplan.py")


POOL_PATH = os.path.join(ROOT, "data", "user", "持仓数据.md")


ACCOUNT_PATH = os.path.join(ROOT, "config", "账户配置.json")


WATCHLIST_PATH = os.path.join(ROOT, "data", "user", "自选股.md")


TRACKLIST_PATH = os.path.join(ROOT, "data", "user", "跟踪标的.md")


MODELS_PATH = os.path.join(ROOT, "config", "模型配置.json")


AI_DIR = aiplan.AI_DIR


DATA_DIR = aiplan.DATA_DIR


PAN_DIR = aiplan.PAN_DIR


HISTORY_DIR = os.path.join(DATA_DIR, "history")


MARKET_DIR = os.path.join(AI_DIR, "market")


TRASH_DIR = os.path.join(AI_DIR, ".trash")


PICK_DIR = os.path.join(AI_DIR, "pick")
PLANCHECK_DIR = os.path.join(AI_DIR, "plancheck")     # 报告实盘复核的模型点评产物
TRACK_DIR = os.path.join(AI_DIR, "track")             # 标的跟踪：每日计划产物 + 执行记录
FLOW_DIR = os.path.join(AI_DIR, "flows")              # 交易流：一条流一个 JSON + settings.json
FLOW_SETTINGS = os.path.join(FLOW_DIR, "settings.json")
LEDGER_PATH = os.path.join(ROOT, "data", "user", "交易台账.md")


PHASES = ("prep", "live", "post", "all")


PHASE_LABEL = {"prep": "盘前", "live": "盘中", "post": "盘后", "all": "全时段"}


MAX_REPORTS = 160


JOB_KEEP = 60


TRASH_TTL_DAYS = 7


TRASH_PURGE_INTERVAL = 1800


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def read_bytes(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def read_text(path, keep_bom=False):
    raw = read_bytes(path)
    if raw is None:
        return ""
    text = raw.decode("utf-8", "replace")
    if not keep_bom:
        text = text.lstrip("\ufeff")
    return text


def file_style(path):
    """记录原文件的 BOM / 换行 / 结尾换行风格 —— 写回时保持一致，
    避免网页保存把用户的 CRLF 文件悄悄改成 LF（会污染 diff，也容易被误判为改动）。"""
    raw = read_bytes(path)
    if raw is None:
        return {"bom": False, "newline": "\n", "trailing": False}
    return {
        "bom": raw.startswith(b"\xef\xbb\xbf"),
        "newline": "\r\n" if b"\r\n" in raw else "\n",
        "trailing": raw.endswith(b"\n"),
    }


def write_like(path, text, style=None):
    """按原文件风格写回（BOM / CRLF 或 LF / 是否以换行结尾）。"""
    style = style or file_style(path)
    text = text.replace("\r\n", "\n")
    text = (text.rstrip("\n") + "\n") if style["trailing"] else text.rstrip("\n")
    if style["newline"] != "\n":
        text = text.replace("\n", style["newline"])
    data = text.encode("utf-8")
    if style["bom"]:
        data = b"\xef\xbb\xbf" + data
    atomic_write(path, data)


def save_like(path, text):
    """写回文本：内容没变就跳过（不写、不备份），变了才按原风格写并留 .bak。
    返回 (是否写入, 备份路径或 None)。"""
    style = file_style(path)
    old = read_text(path)
    def _norm(t):
        return t.replace("\r\n", "\n").rstrip("\n")
    if _norm(old) == _norm(text):
        return False, None
    backup(path)
    write_like(path, text, style)
    return True, path + ".bak"


def atomic_write(path, data, encoding="utf-8", newline=None):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    if isinstance(data, str):
        with open(tmp, "w", encoding=encoding, newline=newline) as f:
            f.write(data)
    else:
        with open(tmp, "wb") as f:
            f.write(data)
    os.replace(tmp, path)


def backup(path):
    """写回前留一份 .bak（覆盖同一个备份，不做无限堆积）。"""
    if os.path.exists(path):
        try:
            atomic_write(path + ".bak", read_bytes(path))
        except OSError:
            pass


def rel(path):
    try:
        return os.path.relpath(path, ROOT).replace("\\", "/")
    except ValueError:
        return path


def inside(child, parent):
    try:
        return os.path.commonpath([os.path.abspath(child), os.path.abspath(parent)]) == os.path.abspath(parent)
    except ValueError:
        return False


def num(v):
    return aiplan.num_or_none(v)
