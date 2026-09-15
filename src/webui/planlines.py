# -*- coding: utf-8 -*-
"""计划线口径：产物里的「关键价位 + 计划」→ 买点 / 减仓 / 止损 / 目标 / 支撑 / 压力。

报告页、总控台卡片、标的跟踪页共用这一份实现（谁都不许自己再解析一遍计划）。
单独成模块，是因为 plancheck.py 已经贴着 800 行的上限。
"""

import copy
import os
import re

from .paths import ROOT, aiplan, num, rel
from .plancheck import is_noop, parse_range, plan_kind


# ---------------------------------------------------------------------------
# 计划线（关键价位 + 计划 → 单一可执行价位与手数）
#
# 总控台卡片、标的跟踪页、报告页都从这里取同一份口径：谁都不许自己再解析一遍计划。
# ---------------------------------------------------------------------------

LEVELS_CACHE_MAX = 200

_LEVELS_CACHE = {}         # 产物路径 -> (mtime, levels)


def lots_text(shares):
    """股数 → 手数文本（1 手 = 100 股）。空 / 0 / 负数返回空串。"""
    n = num(shares)
    if n is None or n <= 0:
        return ""
    n = int(n)
    whole, rest = divmod(n, 100)
    if rest == 0:
        return "%d 手" % whole
    return ("%d 手 %d 股" % (whole, rest)) if whole else ("%d 股" % rest)


def trigger_price(rng, act=None, price=None):
    """价格区间 → **一个可执行价位**（与报告页 ui/planprices.js 同一口径）。

    规则：整段在现价上方取下沿、整段在现价下方取上沿、含现价取下沿；
    没有现价时按动作判断（卖出类取上沿，其余取下沿）。
    """
    pair = parse_range(rng)
    if not pair:
        vals = [num(x) for x in re.findall(r"\d+(?:\.\d+)?", str(rng or ""))]
        vals = [v for v in vals if v is not None]
        if not vals:
            return None
        if len(vals) == 1:
            return vals[0]
        pair = (min(vals), max(vals))
    lo, hi = pair
    cur = num(price)
    if cur is not None:
        if hi < cur:
            return hi
        if lo > cur:
            return lo
        return lo
    is_sell = plan_kind(act) == "卖出" or any(w in str(act or "") for w in ("减", "清", "卖", "止盈"))
    return hi if is_sell else lo


def meaningful_failure(text, rng=None, act=None):
    """失效条件过滤：只重复「触发价的反面」且没有别的信息 → 返回空串。

    模型经常写「价格<8.22」这种与触发条件互为反向、等于没说的废话（用户批注 6）。
    判定：文本里出现的价格与触发价相差 ≤0.5%（或 0.01 元）且没有其他价位 → 视为废话。
    """
    txt = str(text or "").strip()
    if not txt:
        return ""
    px = trigger_price(rng, act, None)
    if px is None:
        return txt
    nums = [num(x) for x in re.findall(r"\d+(?:\.\d+)?", txt)]
    nums = [v for v in nums if v is not None]
    if not nums:
        return txt
    near = [v for v in nums if abs(v - px) <= max(abs(px) * 0.005, 0.011)]
    if near and len(near) == len(nums):
        return ""
    return txt


def report_levels(path):
    """产物里的计划线（买点 / 减仓 / 止损 / 目标 / 支撑 / 压力），按 mtime 缓存。

    aiplan 报告与跟踪产物是同一套顶层结构，所以这一个函数同时服务两者。
    """
    if not path:
        return None
    p = path if os.path.isabs(path) else os.path.join(ROOT, path)
    p = os.path.abspath(p)
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        return None
    hit = _LEVELS_CACHE.get(p)
    if hit and hit[0] == mtime:
        return copy.deepcopy(hit[1])
    doc = aiplan.read_json(p) or {}
    plan = ((doc.get("研判") or {}).get("json") or {})
    levels = plan.get("关键价位") or {}

    def rows(key):
        out = []
        for x in (levels.get(key) or []):
            v = num(x.get("价位")) if isinstance(x, dict) else num(x)
            if v is not None:
                out.append({"价位": v, "依据": (x.get("依据") or "") if isinstance(x, dict) else ""})
        return out

    buy = sell = None
    for item in (plan.get("计划") or []):
        act = item.get("动作")
        if is_noop(act):
            continue
        rng = parse_range(item.get("价格区间"))
        if not rng:
            continue
        node = {"动作": act, "下沿": rng[0], "上沿": rng[1], "股数": item.get("股数"),
                "失效条件": item.get("失效条件")}
        if plan_kind(act) == "买入" and buy is None:
            buy = node
        elif plan_kind(act) == "卖出" and sell is None:
            sell = node
    out = {"报告路径": rel(p), "方向": plan.get("方向"), "置信度": plan.get("置信度"),
           "报告交易日": doc.get("trade_date"), "生成时间": doc.get("generated_at"),
           "买点": buy, "减仓": sell, "止损": num(levels.get("止损价")),
           "目标": rows("目标位"), "支撑": rows("支撑"), "压力": rows("压力")}
    if len(_LEVELS_CACHE) > LEVELS_CACHE_MAX:
        _LEVELS_CACHE.clear()
    _LEVELS_CACHE[p] = (mtime, copy.deepcopy(out))
    return out
