# -*- coding: utf-8 -*-
"""数据新鲜度：过期行情快照的自动清理（**直接删除**，不进回收站）。

用户要求：**事实包不许用过期数据**。aiplan / stock3d / pan 都是「扫目录取最新一份」，
所以只要把「早于最近一个交易日」的旧快照从数据目录搬走，事实包就不可能再引用它们；
真的没有新鲜数据时，事实包会在降级清单里写「没有 pan 快照」，而不是拿旧的冒充。

口径（按用户要求）：过期快照**直接删除**——它们没有保留价值（行情随时能重新抓），
所以不进回收站、不留 .origin；`data/ai/.trash/stale/` 里历史遗留的条目也会一并清掉。
"""

import os
import re
import shutil
import time
from datetime import date, timedelta

from .paths import DATA_DIR, TRASH_DIR, rel
from .plancheck import session_of

STALE_DIR = os.path.join(TRASH_DIR, "stale")     # 只用于清理历史遗留（新策略直接删）
PURGE_INTERVAL = 60.0
NO_PURGE_ENV = "AIPLAN_NO_PURGE"     # 测试用：置 1 时完全不清理（测试不该动用户数据）
_PURGE_TS = [0.0]


def purge_disabled():
    return os.environ.get(NO_PURGE_ENV, "").strip().lower() not in ("", "0", "false", "no")


def latest_trade_day(now=None):
    """最近一个交易日（YYYY-MM-DD）：优先本地交易日历，缺失就按周一至周五推算。"""
    sess = session_of(now)
    today = str(sess["现在"])[:10]
    if sess["是否交易日"]:
        return today
    day = date.fromisoformat(today)
    for _ in range(10):
        day -= timedelta(days=1)
        if day.weekday() < 5:
            return day.isoformat()
    return today


def _day_of(name):
    """目录名 → (类型, YYYY-MM-DD)；认不出返回 (None, None)。"""
    if re.match(r"^\d{8}$", name):
        return "day", "%s-%s-%s" % (name[:4], name[4:6], name[6:])
    m = re.match(r"^s(\d{8})$", name)
    if m:
        return "stock3d", "%s-%s-%s" % (m.group(1)[:4], m.group(1)[4:6], m.group(1)[6:])
    # stock3d.py 落的文件型快照 data/stock3d_<YYYYMMDD>.json（老实现只认目录，这批一直没被清过）
    m = re.match(r"^stock3d_(\d{8})\.json$", name)
    if m:
        return "stock3d文件", "%s-%s-%s" % (m.group(1)[:4], m.group(1)[4:6], m.group(1)[6:])
    return None, None


def _remove(path, tag, log=None):
    """直接删除一个过期目录/文件（不进回收站：过期行情快照没有保留价值）。"""
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=False)
        else:
            os.remove(path)
    except OSError as exc:
        if log:
            log("[WARN] 过期数据删除失败：%s（%s）" % (rel(path), exc))
        return None
    if log:
        log("[OK] 已删除过期数据：%s（%s）" % (rel(path), tag))
    return {"类型": tag, "路径": rel(path)}


def purge_stale(force=False, log=None):
    """删除过期快照（早于最近一个交易日），并清掉 stale 目录里的历史遗留。

    返回 {最近交易日, 删除[], 检查, 跳过?}。同进程 60 秒最多扫一次。
    """
    now = time.time()
    if purge_disabled():
        return {"跳过": True, "原因": "环境变量 %s 已禁用清理" % NO_PURGE_ENV}
    if not force and (now - _PURGE_TS[0]) < PURGE_INTERVAL:
        return {"跳过": True, "原因": "同进程 60 秒内已清理过"}
    _PURGE_TS[0] = now
    trade_day = latest_trade_day()
    deleted = []

    def scan(parent, kinds, tag_prefix):
        if not os.path.isdir(parent):
            return 0
        n = 0
        for name in sorted(os.listdir(parent)):
            path = os.path.join(parent, name)
            if not os.path.isdir(path):
                continue
            kind, day = _day_of(name)
            if kind not in kinds or day is None or day >= trade_day:
                continue
            hit = _remove(path, tag_prefix, log)
            if hit:
                deleted.append(hit)
            n += 1
        return n

    def scan_files(parent, kinds, tag_prefix):
        """文件型快照（data/stock3d_<日期>.json）：与目录型同一口径，过期直接删。"""
        if not os.path.isdir(parent):
            return 0
        n = 0
        for name in sorted(os.listdir(parent)):
            path = os.path.join(parent, name)
            if not os.path.isfile(path):
                continue
            kind, day = _day_of(name)
            if kind not in kinds or day is None or day >= trade_day:
                continue
            hit = _remove(path, tag_prefix, log)
            if hit:
                deleted.append(hit)
            n += 1
        return n

    checked = 0
    checked += scan(os.path.join(DATA_DIR, "pan"), {"day"}, "pan")
    checked += scan(os.path.join(DATA_DIR, "pan", "state"), {"day"}, "panstate")
    checked += scan(DATA_DIR, {"stock3d"}, "stock3d")
    checked += scan_files(DATA_DIR, {"stock3d文件"}, "stock3d文件")

    legacy = []
    if os.path.isdir(STALE_DIR):
        for name in sorted(os.listdir(STALE_DIR)):
            path = os.path.join(STALE_DIR, name)
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.remove(path)
            except OSError:
                continue
            legacy.append(name)
        if not os.listdir(STALE_DIR):
            try:
                os.rmdir(STALE_DIR)
            except OSError:
                pass
    if log and not deleted and not legacy:
        log("[..] 数据新鲜度：没有过期快照（最近交易日 %s）" % trade_day)
    return {"最近交易日": trade_day, "检查": checked, "删除": deleted,
            "历史遗留": legacy, "口径": "过期快照直接删除（不进回收站）"}


def stale_hint(result):
    """清理结果 → 一句人话（页面 toast 用）。没有清理动作返回 None。"""
    if not result or result.get("跳过"):
        return None
    gone = len(result.get("删除") or [])
    if not gone:
        return None
    return ("已删除 %d 份过期行情快照（早于最近交易日 %s）；"
            "想让事实包带上最新背景，先跑一次 python main.py pan post 或 stock3d pull。"
            % (gone, result.get("最近交易日")))
