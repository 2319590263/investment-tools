# -*- coding: utf-8 -*-
"""只读原始数据：data/pan 快照与 data/stock3d_*.json 快照的枚举与读取。"""

import os
import re

from .paths import DATA_DIR, PAN_DIR, aiplan


def newest_file(paths):
    best = None
    for p in paths:
        try:
            mt = os.stat(p).st_mtime
        except OSError:
            continue
        if best is None or mt > best[0]:
            best = (mt, p)
    return best[1] if best else None


def pan_files():
    out = []
    if not os.path.isdir(PAN_DIR):
        return out
    for date_dir in os.listdir(PAN_DIR):
        d = os.path.join(PAN_DIR, date_dir)
        if not re.match(r"^\d{8}$", date_dir) or not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if re.match(r"^latest_(prep|live|post|all)\.json$", name):
                out.append(os.path.join(d, name))
    return out


def stock3d_files():
    if not os.path.isdir(DATA_DIR):
        return []
    return [os.path.join(DATA_DIR, n) for n in os.listdir(DATA_DIR)
            if re.match(r"^stock3d_\d{8}\.json$", n)]


def stock3d_snapshot():
    """最新 stock3d 快照：返回 (路径, 文档, 已含 6 位代码列表)。"""
    path = newest_file(stock3d_files())
    doc = aiplan.read_json(path) if path else None
    codes = []
    for sym in (doc or {}).get("symbols") or []:
        c6 = aiplan.code6(sym.get("code"))
        if c6 and c6 not in codes:
            codes.append(c6)
    return path, doc, codes
