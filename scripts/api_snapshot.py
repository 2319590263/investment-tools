# -*- coding: utf-8 -*-
"""HTTP 接口快照与前后对拍：重构时证明「接口零变化」。

用法（需要服务已在跑）：
    python scripts/api_snapshot.py --out tmp/api_before.json
    ... 改代码 ...
    python scripts/api_snapshot.py --out tmp/api_after.json
    python scripts/api_snapshot.py --compare tmp/api_before.json tmp/api_after.json

只打只读 GET 接口；易变字段（mtime / 时间 / 缓存 / 上次清理 等）在快照里统一归一化，
所以对拍结果只看结构与业务字段。退出码 0 = 一致，1 = 有差异。
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

ENDPOINTS = [
    "/api/state", "/api/holdings", "/api/account", "/api/models", "/api/reports",
    "/api/report", "/api/history", "/api/symbols", "/api/market", "/api/market/forecast",
    "/api/kline?code=002463.SZ&limit=60", "/api/watchlist", "/api/trash",
    "/api/pick", "/api/pick/list", "/api/pick/boards",
    "/api/plancheck",
    "/api/overview",
    "/api/alerts",
    "/api/tracklist",
    "/api/track/all",
    "/api/track?code=600967",
    "/api/flows",
    "/api/flow?id=__missing__",
    "/api/ledger?limit=5",
    "/api/flow/minutes?id=__missing__&code=600967",
]

VOLATILE_KEY = re.compile(r"(mtime|时间|生成|缓存|上次清理|扫描|用时|耗时|日期|现在|剩余天数|即将过期)")
VOLATILE_VALUE = re.compile(r"\d{4}-\d{2}-\d{2}T?\d{0,2}:?\d{0,2}:?\d{0,2}")
VOLATILE_TIME = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")


def normalize(node, key=""):
    if isinstance(node, dict):
        return {k: normalize(v, k) for k, v in node.items()}
    if isinstance(node, list):
        return [normalize(v, key) for v in node]
    if VOLATILE_KEY.search(key) and isinstance(node, (int, float)) and not isinstance(node, bool):
        return "<volatile>"          # 剩余天数这类随时间自然衰减的数值
    if isinstance(node, str) and VOLATILE_KEY.search(key) and VOLATILE_VALUE.search(node):
        return "<volatile>"
    if isinstance(node, str) and VOLATILE_KEY.search(key) and VOLATILE_TIME.match(node):
        return "<volatile>"
    return node


def fetch(base, path, timeout=300):
    url = base + path
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return {"状态": resp.status, "体": normalize(json.loads(raw))}
    except urllib.error.HTTPError as e:
        return {"状态": e.code, "体": e.read().decode("utf-8", "replace")[:400]}
    except Exception as e:  # noqa: BLE001
        return {"状态": None, "错误": "%s" % (e,)}


def diff(a, b, path="", out=None, limit=40):
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if type(a) is not type(b):
        out.append("%s: 类型 %s -> %s" % (path or "/", type(a).__name__, type(b).__name__))
    elif isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append("%s/%s: 新增" % (path, k))
            elif k not in b:
                out.append("%s/%s: 缺失" % (path, k))
            else:
                diff(a[k], b[k], "%s/%s" % (path, k), out, limit)
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append("%s: 长度 %d -> %d" % (path, len(a), len(b)))
        else:
            for i, (x, y) in enumerate(zip(a, b)):
                diff(x, y, "%s[%d]" % (path, i), out, limit)
    elif a != b:
        out.append("%s: %r -> %r" % (path, a, b))
    return out


def main():
    ap = argparse.ArgumentParser(description="接口快照 / 对拍")
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--out", help="把快照写到这个文件")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"), help="对拍两个快照文件")
    args = ap.parse_args()

    if args.compare:
        with open(args.compare[0], encoding="utf-8") as fh:
            a = json.load(fh)
        with open(args.compare[1], encoding="utf-8") as fh:
            b = json.load(fh)
        problems = diff(a, b)
        if problems:
            print("发现 %d 处差异（最多显示 40 条）：" % len(problems))
            for line in problems:
                print("  " + line)
            return 1
        print("一致：%d 个接口逐字段相同（易变字段已归一化）" % len(ENDPOINTS))
        return 0

    snap = {}
    for path in ENDPOINTS:
        snap[path] = fetch(args.base, path)
        state = snap[path].get("状态")
        print("  %-40s %s" % (path, state))
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(snap, fh, ensure_ascii=False, indent=1, sort_keys=True)
        print("已写入 %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
