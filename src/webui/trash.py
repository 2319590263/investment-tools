# -*- coding: utf-8 -*-
"""回收站：列表、7 天过期真删、恢复。"""

import os
import re
import threading
import time

from .paths import AI_DIR, DATA_DIR, ROOT, TRASH_DIR, TRASH_PURGE_INTERVAL, TRASH_TTL_DAYS, aiplan, atomic_write, inside, now_str, read_bytes, read_text, rel


def trash_kind(day, name):
    """回收站条目的类型标签（报告 / 计划历史快照 / 荐股结果）。"""
    if day == "history":
        return "计划历史快照"
    base = str(name or "")
    if base.startswith("latest_pick."):
        return "荐股指针"
    if re.search(r"_pick\.(json|md)$", base):
        return "荐股结果"
    return "报告"


def trash_items():
    items = []
    if not os.path.isdir(TRASH_DIR):
        return items
    for day in sorted(os.listdir(TRASH_DIR), reverse=True):
        d = os.path.join(TRASH_DIR, day)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if name.endswith(".replaced"):
                continue
            if name.endswith(".origin"):
                continue
            p = os.path.join(d, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            is_hist = (day == "history")
            age_days = max(0.0, (time.time() - st.st_mtime) / 86400.0)
            origin = p + ".origin"
            origin_text = (read_text(origin) or "").strip() if os.path.exists(origin) else ""
            items.append({
                "类型": trash_kind(day, name),
                "名称": name, "日期": day, "大小": st.st_size,
                "时间": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
                "回收站路径": rel(p),
                "原路径": origin_text or (rel(aiplan.PLAN_LOG) if is_hist
                                        else "data/ai/%s/%s" % (day, name)),
                "kind": "history" if is_hist else "report",
                "剩余天数": round(max(0.0, TRASH_TTL_DAYS - age_days), 1),
                "即将过期": (TRASH_TTL_DAYS - age_days) <= 2,
            })
    items.sort(key=lambda x: x["时间"], reverse=True)
    return items


TRASH_LAST_PURGE = {"清理时间": None, "删除": [], "释放字节": 0, "错误": None,
                    "保留天数": TRASH_TTL_DAYS, "跳过": True}


_TRASH_PURGE_LOCK = threading.Lock()


_TRASH_PURGE_TS = 0.0


def purge_trash_expired(days=TRASH_TTL_DAYS, force=False, log=None):
    """回收站里超过 days 天的条目**真删**（不可恢复）。

    · force=False 时同进程内最多每 TRASH_PURGE_INTERVAL 秒扫一次盘（惰性清理）；
    · 返回 {清理时间, 删除, 释放字节, 错误, 保留天数, 跳过}。
    """
    global _TRASH_PURGE_TS
    now = time.time()
    with _TRASH_PURGE_LOCK:
        if not force and (now - _TRASH_PURGE_TS) < TRASH_PURGE_INTERVAL:
            return dict(TRASH_LAST_PURGE, 跳过=True)
        _TRASH_PURGE_TS = now
        result = {"清理时间": now_str(), "删除": [], "释放字节": 0, "错误": None,
                  "保留天数": days, "跳过": False}
        cutoff = now - days * 86400.0
        try:
            days_dirs = sorted(os.listdir(TRASH_DIR)) if os.path.isdir(TRASH_DIR) else []
        except OSError as e:
            result["错误"] = str(e)
            days_dirs = []
        for day in days_dirs:
            d = os.path.join(TRASH_DIR, day)
            if not os.path.isdir(d):
                continue
            try:
                names = sorted(os.listdir(d))
            except OSError:
                continue
            for name in names:
                p = os.path.join(d, name)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                if not os.path.isfile(p) or st.st_mtime >= cutoff:
                    continue
                try:
                    os.remove(p)
                except OSError as e:
                    result["错误"] = "删除失败：%s（%s）" % (rel(p), e)
                    continue
                result["删除"].append({
                    "类型": trash_kind(day, name),
                    "名称": name, "日期": day, "大小": st.st_size,
                    "时间": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
                })
                result["释放字节"] += st.st_size
            try:
                if not os.listdir(d):
                    os.rmdir(d)
            except OSError:
                pass
        TRASH_LAST_PURGE.clear()
        TRASH_LAST_PURGE.update(result)
        if log:
            if result["删除"]:
                log("[OK] 回收站清理：删除 %d 个超过 %d 天的条目，释放 %.1f KB"
                    % (len(result["删除"]), days, result["释放字节"] / 1024.0))
            else:
                log("[..] 回收站清理：没有超过 %d 天的条目（%s）" % (days, result["清理时间"]))
        return result


def restore_trash(path):
    """把回收站里的文件搬回原位；历史快照会覆盖 plan_log.jsonl（先备份当前文件）。"""
    if not path:
        return None, "缺少 path"
    p = path if os.path.isabs(path) else os.path.join(ROOT, path)
    p = os.path.abspath(p)
    if not inside(p, TRASH_DIR) or not os.path.exists(p):
        return None, "回收站里没有这个文件"
    parts = os.path.relpath(p, TRASH_DIR).split(os.sep)
    if len(parts) != 2:
        return None, "回收站结构异常"
    day, name = parts
    if day == "history":
        target = aiplan.PLAN_LOG
        if os.path.exists(target) and os.path.getsize(target) > 0:
            atomic_write(os.path.join(aiplan.AI_HISTORY, name + ".before-restore"),
                         read_bytes(target), newline="")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        os.replace(p, target)
        return {"恢复": rel(target), "类型": "计划历史快照"}, None
    target_dir = os.path.join(AI_DIR, day)
    os.makedirs(target_dir, exist_ok=True)
    target = os.path.join(target_dir, name)
    origin = p + ".origin"
    if os.path.exists(origin):
        raw = (read_text(origin) or "").strip()
        if raw:
            cand = os.path.abspath(os.path.join(ROOT, raw))
            if inside(cand, DATA_DIR):
                target = cand
                os.makedirs(os.path.dirname(target), exist_ok=True)
    if os.path.exists(target):
        os.replace(target, target + ".replaced")
    os.replace(p, target)
    if os.path.exists(origin):
        os.remove(origin)
    return {"恢复": rel(target), "类型": trash_kind(day, name)}, None
