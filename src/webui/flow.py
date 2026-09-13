# -*- coding: utf-8 -*-
"""交易流（纯逻辑）：从建仓到清仓的持仓跟踪。

一条流 = 一只标的 + 本流资金 / 目标收益率 / 最大亏损 / 最大加仓次数。流里记「每笔成交」，
持仓用平均成本法重算，盈亏、目标进度、到价提醒都是纯函数；**不取数、不调模型**
（取数与模型在 flow_run.py，页面组装在 flowview.py）。

口径固定：
* 收益率分母 = 本流资金（页面另显示「按已投入成本」的收益率作参考，但判定只用前者）；
* 手续费复用 aiplan.trade_cost（A 股卖出含印花税、双边过户费；ETF 免）；
* 提醒按「同一（类型, 价位）当天只提醒一次」去重，状态存流内，跨页面刷新不重复；
* 盘中不改计划：成交只重算持仓与盈亏，计划只在开流 / 盘后过点提醒一键 / 手动时重算。
"""

import json
import os
import re
from datetime import datetime, timedelta

from . import alerts as alerts_store
from .paths import (FLOW_DIR, FLOW_SETTINGS, LEDGER_PATH, ROOT, TRASH_DIR, aiplan,
                    atomic_write, num, now_str, rel)
from .plancheck import session_of
from .planlines import report_levels

FLOW_VERSION = 1
ACTIVE_STATE = "进行中"
STATES = ("进行中", "已达标", "已止损", "未达标清仓", "已结束", "已终止")
DIRECTIONS = ("买入", "卖出")
BANDS = (0.2, 0.5, 1.0)                  # 接近带档位（百分比）
INTERVALS = (10, 30, 60, 180, 600)       # 轮询档位（秒）
DEFAULT_SETTINGS = {"接近带_pct": 0.3, "轮询间隔_秒": 30,
                    "盘后重算时间": "15:10", "打开页面自动补跑": False}
EVENT_KEEP = 200                         # 事件时间线保留条数
CHECK_KEEP = 20                          # 体检记录保留条数
FILL_KEEP = 2000                         # 单条流的成交上限（防手滑灌爆）
TOL = 0.01                               # 触及容差 = 1 个最小变动价位

_FLOW_CACHE = {}                         # 路径 -> (mtime, doc)


# ---------------------------------------------------------------------------
# 设置
# ---------------------------------------------------------------------------

def _clamp(v, lo, hi, default):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, x))


def settings():
    """盯盘与重算设置（data/ai/flows/settings.json），缺失或损坏时回默认值。"""
    doc = aiplan.read_json(FLOW_SETTINGS)
    out = dict(DEFAULT_SETTINGS)
    if isinstance(doc, dict):
        out.update({k: v for k, v in doc.items() if k in DEFAULT_SETTINGS})
    out["接近带_pct"] = _clamp(out.get("接近带_pct"), 0.05, 5.0, DEFAULT_SETTINGS["接近带_pct"])
    iv = int(_clamp(out.get("轮询间隔_秒"), 5, 3600, DEFAULT_SETTINGS["轮询间隔_秒"]))
    out["轮询间隔_秒"] = iv
    if not re.match(r"^\d{1,2}:\d{2}$", str(out.get("盘后重算时间") or "")):
        out["盘后重算时间"] = DEFAULT_SETTINGS["盘后重算时间"]
    out["打开页面自动补跑"] = bool(out.get("打开页面自动补跑"))
    return out


def save_settings(patch):
    cur = settings()
    for key in DEFAULT_SETTINGS:
        if isinstance(patch, dict) and key in patch and patch[key] is not None:
            cur[key] = patch[key]
    cur["接近带_pct"] = _clamp(cur.get("接近带_pct"), 0.05, 5.0, DEFAULT_SETTINGS["接近带_pct"])
    cur["轮询间隔_秒"] = int(_clamp(cur.get("轮询间隔_秒"), 5, 3600,
                                   DEFAULT_SETTINGS["轮询间隔_秒"]))
    if not re.match(r"^\d{1,2}:\d{2}$", str(cur.get("盘后重算时间") or "")):
        cur["盘后重算时间"] = DEFAULT_SETTINGS["盘后重算时间"]
    cur["打开页面自动补跑"] = bool(cur.get("打开页面自动补跑"))
    atomic_write(FLOW_SETTINGS, json.dumps(cur, ensure_ascii=False, indent=1), newline="\n")
    return cur


# ---------------------------------------------------------------------------
# 流文件的读写与列举
# ---------------------------------------------------------------------------

def flow_id(code, day=None):
    c6 = aiplan.code6(code or "") or "000000"
    d = (day or datetime.now().strftime("%Y-%m-%d")).replace("-", "")
    return "%s-%s" % (c6, d)


def flow_path(fid):
    return os.path.join(FLOW_DIR, "%s.json" % str(fid or "").strip())


def save_flow(doc):
    path = flow_path(doc.get("流编号"))
    atomic_write(path, json.dumps(doc, ensure_ascii=False, indent=1), newline="\n")
    docs = _FLOW_CACHE.get(path)
    try:
        _FLOW_CACHE[path] = (os.path.getmtime(path), doc)
    except OSError:
        _FLOW_CACHE.pop(path, None)
    return path


def load_flow(fid):
    path = flow_path(fid)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None, "交易流不存在：%s" % fid
    hit = _FLOW_CACHE.get(path)
    if hit and hit[0] == mtime:
        return hit[1], None
    doc = aiplan.read_json(path)
    if not isinstance(doc, dict):
        return None, "交易流读不出内容：%s" % rel(path)
    _FLOW_CACHE[path] = (mtime, doc)
    return doc, None


def list_flows(active_only=False):
    """全部流（按创建时间倒序）；active_only 只返回「进行中」。"""
    out = []
    if not os.path.isdir(FLOW_DIR):
        return out
    for name in os.listdir(FLOW_DIR):
        if not re.match(r"^\d{6}-\d{8}\.json$", name):
            continue
        path = os.path.join(FLOW_DIR, name)
        doc, _err = load_flow(name[:-5])
        if not doc:
            continue
        if active_only and doc.get("状态") != ACTIVE_STATE:
            continue
        out.append(doc)
    out.sort(key=lambda d: str(d.get("创建时间") or ""), reverse=True)
    return out


def active_capital(exclude=None):
    """所有进行中的流已占用的本流资金合计（exclude 传流编号时排除自己，便于改参数）。"""
    total = 0.0
    for doc in list_flows(active_only=True):
        if exclude and doc.get("流编号") == exclude:
            continue
        total += num((doc.get("参数") or {}).get("本流资金")) or 0.0
    return round(total, 2)


def delete_flow(fid):
    """把一条流移入回收站（复用 7 天过期口径），返回 (结果, 错误)。"""
    doc, err = load_flow(fid)
    if err:
        return None, err
    src = flow_path(fid)
    day = datetime.now().strftime("%Y%m%d")
    dest = os.path.join(TRASH_DIR, day)
    os.makedirs(dest, exist_ok=True)
    dst = os.path.join(dest, os.path.basename(src))
    if os.path.exists(dst):
        os.replace(dst, dst + ".replaced")
    os.replace(src, dst)
    _FLOW_CACHE.pop(src, None)
    return {"移入": [rel(dst)], "回收站": rel(dest), "标的": doc.get("标的"),
            "说明": "交易流已移入回收站，超过 7 天会真正删除。"}, None


# ---------------------------------------------------------------------------
# 开流与校验
# ---------------------------------------------------------------------------

def create_flow(code, name="", capital=None, target_pct=None, loss_pct=None, max_adds=2,
                start=None, note="", account_total=None, today=None):
    """建一条流（写盘）。start = {股数, 成本价, 可用} 表示按持仓文件带入的期初持仓。"""
    c6 = aiplan.code6(code or "")
    if not c6:
        return None, "请填 6 位证券代码"
    cap = num(capital)
    if not cap or cap <= 0:
        return None, "本流资金必须大于 0"
    tgt = num(target_pct)
    if not tgt or tgt <= 0:
        return None, "目标收益率必须大于 0"
    los = num(loss_pct)
    if not los or los <= 0:
        return None, "最大亏损必须大于 0"
    adds = int(_clamp(max_adds, 0, 20, 2))
    day = today or datetime.now().strftime("%Y-%m-%d")
    fid = flow_id(c6, day)
    if os.path.exists(flow_path(fid)):
        return None, "该标的今天已经开过一条流：%s（先结束或删除它）" % fid
    for doc in list_flows(active_only=True):
        if aiplan.code6((doc.get("标的") or {}).get("代码") or "") == c6:
            return None, ("该标的有进行中的流 %s，同一标的只允许一条未结束的流"
                          % doc.get("流编号"))
    if num(account_total):
        used = active_capital()
        if used + cap > float(account_total) + 1e-6:
            return None, ("在跑的流资金合计 %s 元，再加 %s 元会超过账户总资金 %s 元"
                          % (_f(used, 2), _f(cap, 2), _f(account_total, 2)))
    is_etf = c6.startswith(("51", "52", "56", "58", "15", "16", "159"))
    fills, start_block = [], {"股数": 0.0, "可用": 0.0, "成本价": None,
                              "说明": "空仓开流（先出建仓计划）"}
    start_qty = num((start or {}).get("股数"))
    start_cost = num((start or {}).get("成本价"))
    if start_qty and start_qty > 0:
        if not start_cost or start_cost <= 0:
            return None, "按持仓文件带入时必须有成本价"
        avail = num((start or {}).get("可用"))
        start_block = {"股数": float(start_qty), "可用": float(avail if avail is not None
                                                            else start_qty),
                       "成本价": float(start_cost), "说明": "期初持仓（按持仓文件带入）"}
        fills.append({"序号": 1, "日期": day, "时间": datetime.now().strftime("%H:%M:%S"),
                      "方向": "买入", "价格": float(start_cost), "数量": float(start_qty),
                      "金额": round(float(start_cost) * float(start_qty), 2), "手续费": 0.0,
                      "来源": "期初", "委托序号": "", "备注": "期初持仓（不计手续费）",
                      "去重键": "期初-%s" % fid})
    doc = {
        "流编号": fid, "版本": FLOW_VERSION,
        "标的": {"代码": c6, "名称": (name or c6).strip(), "是否ETF": bool(is_etf)},
        "状态": ACTIVE_STATE, "创建时间": now_str(), "起始日": day,
        "结束时间": None, "结束原因": None,
        "参数": {"本流资金": round(float(cap), 2), "目标收益率_pct": round(float(tgt), 3),
                 "最大亏损_pct": round(float(los), 3), "最大加仓次数": adds,
                 "目标盈利_元": round(float(cap) * float(tgt) / 100.0, 2),
                 "最大亏损_元": round(float(cap) * float(los) / 100.0, 2),
                 "备注": str(note or "").strip()},
        "期初": start_block, "成交": fills,
        "持仓": {"股数": 0.0, "可用": 0.0, "平均成本": None},
        "盈亏": {}, "计划": {}, "体检": [], "事件": [], "提醒状态": {"日期": "", "已提醒": {}},
        "计划重算": {"上次生成日": None, "提醒过": None, "自动补跑": False},
        "台账": {"游标行数": 0, "mtime": 0.0, "未并入": 0, "最近同步": None},
        "提示": [],
    }
    events_append(doc, "开流", "开流：本流资金 %s 元｜目标 %s%%（%s 元）｜最大亏损 %s%%（%s 元）"
                  % (_f(cap, 2), _f(tgt, 3), _f(doc["参数"]["目标盈利_元"], 2),
                     _f(los, 3), _f(doc["参数"]["最大亏损_元"], 2)), "ok")
    recompute(doc)
    save_flow(doc)
    return doc, None


def _f(v, nd=2):
    return "—" if v is None else (("%." + str(int(nd)) + "f") % v)


# ---------------------------------------------------------------------------
# 成交、持仓与盈亏（平均成本法）
# ---------------------------------------------------------------------------

def _account():
    from .store import load_account_bundle
    try:
        return load_account_bundle().get("配置") or {}
    except Exception:              # noqa: BLE001  账户文件坏了不该拖垮流计算
        return {}


def trade_fee(doc, side, amount, account=None):
    """按账户费率算一笔费用；费率缺失时按 0 计并标注。"""
    acc = account if account is not None else _account()
    if not acc:
        return {"佣金": 0.0, "印花税": 0.0, "过户费": 0.0, "合计": 0.0, "费率缺失": True}
    out = aiplan.trade_cost(bool((doc.get("标的") or {}).get("是否ETF")), side,
                            amount or 0.0, acc)
    out["费率缺失"] = not any(num(acc.get(k)) for k in ("佣金费率_pct", "佣金最低_元"))
    return out


def _available(fills, qty, today):
    """可用股数：T+1 口径 —— 当日买入的部分不可卖。"""
    today_buy = sum((num(f.get("数量")) or 0.0) for f in fills
                    if f.get("方向") == "买入" and str(f.get("日期") or "") == today)
    return round(max(0.0, qty - today_buy), 2)


def recompute(doc, account=None, today=None):
    """按成交明细重算持仓与已实现盈亏（平均成本法）；就地写回 doc 并返回它。"""
    today = today or datetime.now().strftime("%Y-%m-%d")
    fills = sorted([f for f in (doc.get("成交") or []) if isinstance(f, dict)],
                   key=lambda f: (str(f.get("日期") or ""), str(f.get("时间") or ""),
                                  int(num(f.get("序号")) or 0)))
    qty = cost_total = realized = invested = 0.0
    warnings = []
    for f in fills:
        price = num(f.get("价格")) or 0.0
        q = num(f.get("数量")) or 0.0
        amount = round(price * q, 2)
        f["金额"] = amount
        side = "卖出" if str(f.get("方向")) == "卖出" else "买入"
        if num(f.get("手续费")) is None:
            f["手续费"] = trade_fee(doc, side, amount, account)["合计"]
        fee = num(f.get("手续费")) or 0.0
        if side == "买入":
            qty += q
            cost_total += amount + fee
            if str(f.get("来源")) != "期初":
                invested += amount + fee
        else:
            avg = (cost_total / qty) if qty > 1e-9 else price
            if q > qty + 1e-9:
                warnings.append("第 %s 笔卖出 %s 股超过流内持仓，只按 %s 股结算（请核对成交）"
                                % (f.get("序号"), _f(q, 0), _f(qty, 0)))
            sell = min(q, qty)
            realized += amount - fee - avg * sell
            qty -= sell
            cost_total -= avg * sell
            if qty <= 1e-9:
                qty, cost_total = 0.0, 0.0
    doc["成交"] = fills
    doc["持仓"] = {"股数": round(qty, 2), "可用": _available(fills, qty, today),
                   "平均成本": (round(cost_total / qty, 4) if qty > 1e-9 else None)}
    pnl = dict(doc.get("盈亏") or {})
    pnl["已实现_元"] = round(realized, 2)
    pnl["投入成本_元"] = round(invested, 2)
    doc["盈亏"] = pnl
    doc["提示"] = warnings
    _settle_if_flat(doc, today)
    return doc


def apply_price(doc, price, source="东财实时行情", when=None, today=None):
    """现价 → 浮动 / 合计 / 收益率 / 目标进度 / 达标与止损标记（就地写回 doc["盈亏"]）。"""
    today = today or datetime.now().strftime("%Y-%m-%d")
    qty = num((doc.get("持仓") or {}).get("股数")) or 0.0
    avg = num((doc.get("持仓") or {}).get("平均成本"))
    pnl = dict(doc.get("盈亏") or {})
    realized = num(pnl.get("已实现_元")) or 0.0
    cap = num((doc.get("参数") or {}).get("本流资金")) or 0.0
    invested = num(pnl.get("投入成本_元")) or 0.0
    target = num((doc.get("参数") or {}).get("目标盈利_元")) or 0.0
    loss = num((doc.get("参数") or {}).get("最大亏损_元")) or 0.0
    p = num(price)
    if p is None:
        floating = 0.0 if not qty else None
    else:
        floating = round((p - avg) * qty, 2) if (qty and avg) else 0.0
    total = round(realized + (floating or 0.0), 2)
    pnl.update({
        "现价": p, "价格来源": source,
        "价格时间": when or datetime.now().strftime("%H:%M:%S"),
        "浮动_元": floating, "合计_元": total,
        "收益率_pct": round(total / cap * 100, 3) if cap else None,
        "按成本收益率_pct": round(total / invested * 100, 3) if invested else None,
        "目标盈利_元": target, "最大亏损_元": loss,
        "进度_pct": round(total / target * 100, 2) if target else None,
        "达标": bool(target and total >= target - 1e-9),
        "触及最大亏损": bool(loss and total <= -loss + 1e-9),
    })
    doc["盈亏"] = pnl
    _settle_if_flat(doc, today)
    return pnl


def _settle_if_flat(doc, today=None):
    """持仓归零且已有卖出 → 自动结算状态（达标 / 未达标 / 止损）。"""
    if doc.get("状态") != ACTIVE_STATE:
        return False
    qty = num((doc.get("持仓") or {}).get("股数")) or 0.0
    if qty > 1e-9:
        return False
    if not any(str(f.get("方向")) == "卖出" for f in (doc.get("成交") or [])):
        return False
    pnl = doc.get("盈亏") or {}
    total = num(pnl.get("合计_元"))
    if total is None:
        total = num(pnl.get("已实现_元")) or 0.0
    target = num(pnl.get("目标盈利_元")) or 0.0
    loss = num(pnl.get("最大亏损_元")) or 0.0
    if target and total >= target - 1e-9:
        state, level = "已达标", "ok"
    elif loss and total <= -loss + 1e-9:
        state, level = "已止损", "bad"
    else:
        state, level = "未达标清仓", "warn"
    doc["状态"] = state
    doc["结束时间"] = now_str()
    doc["结束原因"] = "持仓归零自动结算"
    events_append(doc, "状态", "持仓已归零 → 结算为「%s」（合计 %s 元，收益率 %s%%）"
                  % (state, _f(total, 2), _f(pnl.get("收益率_pct"))), level)
    return True


# ---------------------------------------------------------------------------
# 到价与提醒（接近带 / 触及）
# ---------------------------------------------------------------------------

def _gap(p, lo, hi):
    """现价与区间的距离（百分比）；区间内含现价时为 0。"""
    if lo is None and hi is None:
        return None, None
    lo = lo if lo is not None else hi
    hi = hi if hi is not None else lo
    if p < lo:
        return (lo - p) / lo, "下方"
    if p > hi:
        return (p - hi) / hi, "上方"
    return 0.0, "区间内"


def marks(doc, price=None, band_pct=None):
    """现价 vs 当前计划价位：接近（info）与触及（按类型分级别）。"""
    band = _clamp(band_pct if band_pct is not None else settings()["接近带_pct"],
                  0.05, 5.0, DEFAULT_SETTINGS["接近带_pct"]) / 100.0
    p = num(price)
    if p is None:
        p = num((doc.get("盈亏") or {}).get("现价"))
    levels = (doc.get("计划") or {}).get("关键价位") or {}
    if p is None or not levels:
        return []
    out = []

    def push(kind, node, hit_level, near_level, lots=None, basis=""):
        lo = num(node.get("下沿") if isinstance(node, dict) else node)
        hi = num(node.get("上沿") if isinstance(node, dict) else node)
        if lo is None and hi is None:
            return
        gap, side = _gap(p, lo, hi)
        if gap is None:
            return
        hit = gap <= (TOL / max(abs(p), 1e-9)) or side == "区间内" or gap <= 0.0001
        near = gap <= band
        act = (node.get("动作") if isinstance(node, dict) else "") or ""
        label = kind if not act else "%s %s" % (kind, act)
        price_ref = lo if (hi is None or p <= hi) else hi
        lot_txt = ("（%s）" % lots) if lots else ""
        if hit:
            out.append({"类型": "触及" + kind, "级别": hit_level, "价位": price_ref,
                        "差距_pct": 0.0, "键": "触及%s:%s" % (kind, _f(price_ref, 3)),
                        "文案": "已触及%s %s%s%s" % (label, _f(price_ref, 3), lot_txt,
                                                  ("·" + basis) if basis else "")})
        elif near:
            out.append({"类型": "接近" + kind, "级别": near_level, "价位": price_ref,
                        "差距_pct": round(gap * 100, 3),
                        "键": "接近%s:%s" % (kind, _f(price_ref, 3)),
                        "文案": "接近%s %s（差 %s%%）%s%s"
                                % (label, _f(price_ref, 3), _f(gap * 100, 2), lot_txt,
                                   ("·" + basis) if basis else "")})
    buy = levels.get("买点")
    if buy:
        push("买点", buy, "accent", "info", plancheck_lots(buy.get("股数")))
    sell = levels.get("减仓")
    if sell:
        push("减仓", sell, "warn", "info", plancheck_lots(sell.get("股数")))
    stop = num(levels.get("止损"))
    if stop is not None:
        if p <= stop + TOL:
            out.append({"类型": "触及止损", "级别": "bad", "价位": stop, "差距_pct": 0.0,
                        "键": "触及止损:%s" % _f(stop, 3),
                        "文案": "已触及止损 %s：计划里跌破即离场" % _f(stop, 3)})
        elif (p - stop) / stop <= band:
            out.append({"类型": "接近止损", "级别": "info", "价位": stop,
                        "差距_pct": round((p - stop) / stop * 100, 3),
                        "键": "接近止损:%s" % _f(stop, 3),
                        "文案": "接近止损 %s（还差 %s%%）"
                                % (_f(stop, 3), _f((p - stop) / stop * 100, 2))})
    goals = [g for g in (levels.get("目标") or []) if num(g.get("价位")) is not None]
    goals.sort(key=lambda g: num(g.get("价位")))
    # 现价上方最近的止盈点；已经站上某个止盈点时，先报「已到」而不是跳到更远的目标
    above = [g for g in goals if num(g.get("价位")) >= p - TOL] or goals[-1:]
    for g in above:
        v = num(g.get("价位"))
        if v is None:
            continue
        if p >= v - TOL:
            out.append({"类型": "触及止盈点", "级别": "ok", "价位": v, "差距_pct": 0.0,
                        "键": "触及止盈点:%s" % _f(v, 3),
                        "文案": "已到止盈点 %s%s" % (_f(v, 3),
                                              ("·" + (g.get("依据") or "")) if g.get("依据") else "")})
        elif (v - p) / v <= band:
            out.append({"类型": "接近止盈点", "级别": "info", "价位": v,
                        "差距_pct": round((v - p) / v * 100, 3),
                        "键": "接近止盈点:%s" % _f(v, 3),
                        "文案": "接近止盈点 %s（还差 %s%%）%s"
                                % (_f(v, 3), _f((v - p) / v * 100, 2),
                                   ("·" + (g.get("依据") or "")) if g.get("依据") else "")})
        break
    return out


def plancheck_lots(shares):
    from .planlines import lots_text
    return lots_text(shares)


def due_alerts(doc, price=None, band_pct=None, today=None):
    """今天还没提醒过的标记（同类型+同价位只提醒一次，价格回落后再进入会重新提醒）。"""
    today = today or datetime.now().strftime("%Y-%m-%d")
    state = doc.get("提醒状态") or {}
    if str(state.get("日期") or "") != today:
        state = {"日期": today, "已提醒": {}}
    done = state.get("已提醒") or {}
    fresh = [m for m in marks(doc, price, band_pct) if m["键"] not in done]
    if fresh:
        doc["提醒状态"] = state
    return fresh


def mark_alerted(doc, items, today=None):
    today = today or datetime.now().strftime("%Y-%m-%d")
    state = doc.get("提醒状态") or {}
    if str(state.get("日期") or "") != today:
        state = {"日期": today, "已提醒": {}}
    state.setdefault("已提醒", {})
    stamp = datetime.now().strftime("%H:%M:%S")
    for m in items or []:
        state["已提醒"][m.get("键")] = stamp
    doc["提醒状态"] = state
    return doc


def alert_items(doc, items):
    """把标记转成消息队列条目（字段与总控台到价提醒完全一致，另加流编号）。"""
    sym = doc.get("标的") or {}
    plan_path = (doc.get("计划") or {}).get("产物路径")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for m in items or []:
        out.append({"时间": now, "代码": aiplan.code6(sym.get("代码") or ""),
                    "名称": sym.get("名称"), "点位类型": m.get("类型"),
                    "价位": m.get("价位"), "手数文本": "", "文案": m.get("文案"),
                    "报告路径": plan_path, "级别": m.get("级别"),
                    "流编号": doc.get("流编号")})
    return out


def events_append(doc, kind, text, level="info", price=None, source="系统"):
    rows = list(doc.get("事件") or [])
    rows.append({"时间": now_str(), "类型": kind, "级别": level, "文案": text,
                 "价位": num(price), "来源": source})
    doc["事件"] = rows[-EVENT_KEEP:]
    return doc


# ---------------------------------------------------------------------------
# 手工补录 / 删除成交
# ---------------------------------------------------------------------------

def _next_seq(doc):
    return max([int(num(f.get("序号")) or 0) for f in (doc.get("成交") or [])] or [0]) + 1


def add_fill(doc, side, price, qty, day=None, when=None, note="", source="手工",
             委托序号="", account=None):
    """记一笔成交（手工补录）。返回 (成交条目, 错误)。"""
    if doc.get("状态") != ACTIVE_STATE:
        return None, "这条流已经结束（%s），如需继续请另开一条流" % doc.get("状态")
    direction = "卖出" if str(side) == "卖出" else "买入"
    p, q = num(price), num(qty)
    if p is None or p <= 0:
        return None, "成交价必须大于 0"
    if q is None or q <= 0:
        return None, "成交数量必须大于 0"
    if len(doc.get("成交") or []) >= FILL_KEEP:
        return None, "成交笔数过多（上限 %d 笔），先清理再记" % FILL_KEEP
    day = str(day or datetime.now().strftime("%Y-%m-%d"))
    fill = {"序号": _next_seq(doc), "日期": day,
            "时间": str(when or datetime.now().strftime("%H:%M:%S"))[0:8],
            "方向": direction, "价格": round(float(p), 4), "数量": round(float(q), 2),
            "金额": round(float(p) * float(q), 2), "手续费": None, "来源": source,
            "委托序号": str(委托序号 or ""), "备注": str(note or "").strip(), "去重键": ""}
    fill["手续费"] = trade_fee(doc, direction, fill["金额"], account)["合计"]
    doc["成交"] = list(doc.get("成交") or []) + [fill]
    recompute(doc, account=account, today=day)
    events_append(doc, "成交", "%s %s 股 @ %s（手续费 %s 元，来源：%s）"
                  % (direction, _f(q, 0), _f(p, 3), _f(fill["手续费"], 2), source),
                  "ok" if direction == "买入" else "warn", price=p, source=source)
    return fill, None


def delete_fill(doc, seq, account=None):
    rows = list(doc.get("成交") or [])
    target = [f for f in rows if int(num(f.get("序号")) or 0) == int(num(seq) or -1)]
    if not target:
        return None, "没有序号为 %s 的成交" % seq
    if str(target[0].get("来源")) == "期初":
        return None, "期初持仓不支持删除（它代表开流时的底仓）"
    doc["成交"] = [f for f in rows if f not in target]
    recompute(doc, account=account)
    events_append(doc, "成交", "删除成交 #%s（手工纠错）" % seq, "warn")
    return doc, None


# ---------------------------------------------------------------------------
# 台账同步（同花顺「同步持仓」会写 data/user/交易台账.md）
# ---------------------------------------------------------------------------

LEDGER_COLS = ("日期", "时间", "委托序号", "方向", "代码", "名称", "成交价格", "成交数量",
               "成交金额", "交易市场")


def parse_ledger(text):
    """台账 Markdown → 行字典（列口径与 holdings_sync.LEDGER_COLUMNS 一致）。"""
    rows = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("|") or not line.endswith("|"):
            continue
        cells = [c.replace("\\|", "|").strip() for c in line[1:-1].split("|")]
        if len(cells) != len(LEDGER_COLS) or cells == list(LEDGER_COLS):
            continue
        if all(set(c.replace(":", "").replace("-", "").strip()) <= {" "} for c in cells):
            continue
        rows.append(dict(zip(LEDGER_COLS, cells)))
    return rows


def _ledger_key(row):
    """复用 holdings_sync 的去重口径（委托序号优先，否则内容哈希）。"""
    from .holdings_sync import _trade_key
    trade = {"成交日期": row.get("日期"), "成交时间": row.get("时间"),
             "委托序号": row.get("委托序号"), "买卖标志": row.get("方向"),
             "证券代码": row.get("代码"), "证券名称": row.get("名称"),
             "成交价格": row.get("成交价格"), "成交数量": row.get("成交数量"),
             "成交金额": row.get("成交金额"), "交易市场": row.get("交易市场")}
    return "台账:" + "|".join(str(x) for x in _trade_key(trade))


def ledger_rows(code, since_day=None):
    """台账里该标的的成交行；返回 (行, 早于 since_day 被跳过的条数, mtime)。"""
    from .paths import read_text
    c6 = aiplan.code6(code or "")
    try:
        mtime = os.path.getmtime(LEDGER_PATH)
    except OSError:
        return [], 0, 0.0
    rows, skipped = [], 0
    for row in parse_ledger(read_text(LEDGER_PATH)):
        if aiplan.code6(row.get("代码")) != c6:
            continue
        if since_day and str(row.get("日期") or "") < str(since_day):
            skipped += 1
            continue
        rows.append(row)
    return rows, skipped, mtime


def sync_ledger(doc, account=None, log=None):
    """把台账里该标的新成交并进来（幂等）。返回新增条数。"""
    rows, skipped, mtime = ledger_rows((doc.get("标的") or {}).get("代码"), doc.get("起始日"))
    have = {str(f.get("去重键") or "") for f in (doc.get("成交") or [])}
    added = 0
    for row in rows:
        key = _ledger_key(row)
        if key in have:
            continue
        p, q = num(row.get("成交价格")), num(row.get("成交数量"))
        day = str(row.get("日期") or "")
        direction = "卖出" if "卖" in str(row.get("方向") or "") else "买入"
        dup = any((str(f.get("日期")) == day and str(f.get("方向")) == direction
                   and num(f.get("价格")) == p and num(f.get("数量")) == q)
                  for f in (doc.get("成交") or []))
        if dup:                                   # 手工已经录过同一笔，不再重复
            have.add(key)
            continue
        fill, err = add_fill(doc, direction, p, q, day=day,
                             when=str(row.get("时间") or "")[0:8], source="台账",
                             委托序号=row.get("委托序号"), account=account)
        if err:
            if log:
                log("[WARN] 台账里的一笔成交没并入：%s（%s）" % (row.get("委托序号") or "?", err))
            continue
        fill["去重键"] = key
        have.add(key)
        added += 1
    doc["台账"] = {"游标行数": len(rows), "mtime": mtime, "未并入": skipped,
                   "最近同步": now_str()}
    if added:
        events_append(doc, "同步", "从交易台账并入 %d 笔成交" % added, "ok", source="台账")
    return added


# ---------------------------------------------------------------------------
# 计划：挂到流上 / 盘后重算判定 / 卡片上的「下一步」
# ---------------------------------------------------------------------------

def set_plan(doc, payload, path, kind="首份计划", error=None):
    """把一份计划产物挂到流上（关键价位用 planlines 的统一口径）。"""
    from . import track
    plan = ((payload or {}).get("研判") or {}).get("json") or {}
    md = re.sub(r"\.json$", ".md", path)
    doc["计划"] = {
        "产物路径": rel(path), "md路径": rel(md) if os.path.exists(md) else None,
        "生成时间": (payload or {}).get("generated_at") or now_str(),
        "适用交易日": (payload or {}).get("适用交易日"),
        "数据交易日": (payload or {}).get("trade_date"),
        "方向": plan.get("方向"), "置信度": plan.get("置信度"),
        "一句话结论": plan.get("一句话结论"),
        "条目": track.plan_items(payload), "关键价位": report_levels(path),
        "错误": error or ((payload or {}).get("研判") or {}).get("error"),
    }
    re_meta = dict(doc.get("计划重算") or {})
    re_meta["上次生成日"] = datetime.now().strftime("%Y-%m-%d")
    re_meta["上次生成时间"] = now_str()
    doc["计划重算"] = re_meta
    head = "%s / 置信度 %s" % (plan.get("方向") or "—", plan.get("置信度") or "—")
    events_append(doc, "重算", "%s 已生成：%s" % (kind, head),
                  "warn" if doc["计划"]["错误"] else "ok")
    return doc


def plan_stale(doc, now=None):
    """盘后重算提醒：交易日、过了设定时间、且今天这个点之后还没重算过。"""
    s = session_of(now)
    cfg = settings()
    if not s["是否交易日"]:
        return {"待重算": False, "说明": "非交易日不重算"}
    today = str(s["现在"])[:10]
    hhmm = str(s["现在"])[11:16]
    if hhmm < str(cfg["盘后重算时间"]):
        return {"待重算": False, "说明": "还没到 %s" % cfg["盘后重算时间"]}
    stamp = str((doc.get("计划") or {}).get("生成时间") or "")
    if stamp >= "%s %s" % (today, cfg["盘后重算时间"]):
        return {"待重算": False, "说明": "今天已按最新数据重算过"}
    if not doc.get("计划"):
        return {"待重算": True, "说明": "还没有计划，先出一份"}
    return {"待重算": True, "说明": "今天的盘后计划还没重算"}


def next_action(doc):
    """卡片上的「下一步」：优先到价标记，其次第一条可执行计划条目。"""
    ms = marks(doc)
    if ms:
        rank = {"bad": 0, "accent": 1, "ok": 2, "warn": 3, "info": 4}
        top = sorted(ms, key=lambda m: rank.get(m.get("级别"), 9))[0]
        return top.get("文案") or ""
    items = [i for i in ((doc.get("计划") or {}).get("条目") or []) if not i.get("无动作")]
    if not items:
        return "还没有计划：点「重算计划」生成一份"
    r = items[0]
    rng = r.get("价格区间")
    if isinstance(rng, (list, tuple)):
        txt = " ~ ".join(_f(num(x), 3) for x in rng)
    else:
        txt = str(rng or "")
    qty = num(r.get("股数"))
    return "%s %s%s" % (r.get("动作") or "—", txt, ("（%s 股）" % _f(qty, 0)) if qty else "")


CHECK_LEVELS = {"继续执行": "ok", "提高警惕": "warn", "暂停新动作": "warn", "建议作废重算": "bad"}


def exec_record(doc):
    """把流内成交折成 track 事实包认识的「执行情况」结构（成交就是执行情况）。"""
    rows = []
    for i, f in enumerate(doc.get("成交") or []):
        rows.append({"编号": i + 1,
                     "动作": "%s（%s）" % (f.get("方向") or "—", f.get("来源") or "—"),
                     "触发条件": "%s 股 @ %s" % (_f(f.get("数量"), 0), _f(f.get("价格"), 3)),
                     "价格区间": f.get("价格"),
                     "计划股数": f.get("数量"),
                     "执行状态": "已执行" if f.get("方向") == "买入" else "部分执行",
                     "成交价": f.get("价格"), "成交股数": f.get("数量"),
                     "备注": f.get("备注") or ""})
    if not rows:
        return None
    return {"录入时间": (doc.get("台账") or {}).get("最近同步") or doc.get("创建时间"),
            "条目": rows, "总体备注": "流内成交明细共 %d 笔" % len(rows)}


def add_check(doc, result, note="", model=None, usage=None, cost=None, error=None,
              fact_chars=None):
    """记录一次体检（结论 / 依据 / 处理建议）到流里。"""
    rec = {"时间": now_str(),
           "结论": (result or {}).get("结论") or "未判定",
           "是否失效": bool((result or {}).get("是否失效")),
           "一句话": (result or {}).get("一句话结论"),
           "依据": (result or {}).get("逐条依据") or [],
           "处理建议": (result or {}).get("对当前计划的处理建议") or (result or {}).get("处理建议"),
           "风险": (result or {}).get("风险与失效条件") or [],
           "数据依赖": (result or {}).get("数据依赖与不确定性"),
           "补充说明": str(note or "").strip(), "模型": model, "usage": usage or {},
           "费用": cost, "error": error, "事实包字符数": fact_chars}
    doc["体检"] = (list(doc.get("体检") or []) + [rec])[-CHECK_KEEP:]
    level = CHECK_LEVELS.get(rec["结论"], "info")
    if error and rec["结论"] == "未判定":
        level = "warn"
    events_append(doc, "体检", "体检：%s —— %s" % (rec["结论"], rec["一句话"] or "（无结论）"), level)
    return rec


def close_flow(doc, reason="人工结束", state=None):
    if doc.get("状态") != ACTIVE_STATE:
        return None, "这条流已经结束（%s）" % doc.get("状态")
    doc["状态"] = state or "已结束"
    doc["结束时间"] = now_str()
    doc["结束原因"] = str(reason or "人工结束").strip()
    events_append(doc, "状态", "结束这条流：%s" % doc["结束原因"], "warn")
    return doc, None
