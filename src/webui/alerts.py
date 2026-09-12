# -*- coding: utf-8 -*-
"""到价消息队列：落盘 data/ai/alerts.jsonl，页面刷新与服务重启都不丢。

只有 overview 在「新触发」时调用 append()；文件是运行产物（data/ 已被 .gitignore 忽略）。
保留最近 500 条，超过 600 行时裁一次，避免无限增长。
"""

import io
import json
import os

from .paths import AI_DIR, atomic_write, rel

ALERTS_PATH = os.path.join(AI_DIR, "alerts.jsonl")
KEEP = 500          # 保留最近多少条（超过就裁，不留到 600）
MAX_LIMIT = 200     # 单次最多返回多少条


def _read_lines():
    if not os.path.exists(ALERTS_PATH):
        return []
    try:
        with io.open(ALERTS_PATH, encoding="utf-8") as fh:
            return [line.rstrip("\n") for line in fh if line.strip()]
    except OSError:
        return []


def append(items):
    """追加消息（每条一个 JSON 行）。返回写入条数。"""
    rows = [it for it in (items or []) if isinstance(it, dict)]
    if not rows:
        return 0
    lines = _read_lines()
    for row in rows:
        lines.append(json.dumps(row, ensure_ascii=False))
    if len(lines) > KEEP:
        lines = lines[-KEEP:]
    os.makedirs(os.path.dirname(ALERTS_PATH), exist_ok=True)
    atomic_write(ALERTS_PATH, "\n".join(lines) + "\n", newline="\n")
    return len(rows)


def list_alerts(limit=50):
    """倒序返回最近的消息。"""
    if limit in (None, ""):
        want = 50
    else:
        try:
            want = int(limit)
        except (TypeError, ValueError):
            want = 50
        want = max(1, min(want, MAX_LIMIT))
    out = []
    for raw in _read_lines():
        try:
            doc = json.loads(raw)
        except ValueError:
            continue
        if isinstance(doc, dict):
            out.append(doc)
    return list(reversed(out))[:want]


def count():
    return len(_read_lines())


def clear():
    """清空队列（截断文件）。返回清空前的条数。"""
    before = count()
    os.makedirs(os.path.dirname(ALERTS_PATH), exist_ok=True)
    atomic_write(ALERTS_PATH, "", newline="\n")
    return before


def info():
    return {"路径": rel(ALERTS_PATH), "条数": count(), "保留上限": KEEP}
