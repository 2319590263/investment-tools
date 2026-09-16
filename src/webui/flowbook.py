# -*- coding: utf-8 -*-
"""交易流里「一只标的的账本」：成交 / 持仓 / 盈亏 / 到价提醒 / 台账 / 计划与体检记录。

分工：`flow.py` 管「流」（资金参数、标的清单、聚合、开流 / 加标的 / 结束 / 删除、
文件读写），本模块只管「一只标的」——所有函数收到的都是**标的节点**（dict），节点里带
`代码 / 名称 / 是否ETF / 打法 / 分配资金 / 加入日 / 期初 / 成交 / 持仓 / 盈亏 / 计划 /
体检 / 事件 / 提醒状态 / 台账`。这里不取数、不调模型（取数与模型在 flow_run.py）。

口径固定：
* 收益率分母 = 该标的的「分配资金」（页面另显示「按已投入成本」的收益率作参考，判定只用前者）；
* 手续费复用 aiplan.trade_cost（A 股卖出含印花税、双边过户费；ETF 免）；
* 提醒按「同一（类型, 价位）当天只提醒一次」去重，状态存在节点里，跨页面刷新不重复；
* 盘中不改计划：成交只重算持仓与盈亏，计划只在开流 / 盘后过点提醒一键 / 手动时重算。
"""

import json
import os
import re
from datetime import datetime

from .paths import FLOW_SETTINGS, LEDGER_PATH, aiplan, atomic_write, num, now_str, rel
from .plancheck import session_of
from .planlines import report_levels

ACTIVE_STATE = "进行中"
STATES = ("进行中", "已达标", "已止损", "未达标清仓", "已结束", "已终止")
DIRECTIONS = ("买入", "卖出")
BANDS = (0.2, 0.5, 1.0)                  # 接近带档位（百分比）
INTERVALS = (10, 30, 60, 180, 600)       # 轮询档位（秒）
DEFAULT_SETTINGS = {"接近带_pct": 0.3, "轮询间隔_秒": 30,
                    "盘后重算时间": "15:10", "打开页面自动补跑": False}
EVENT_KEEP = 200                         # 事件时间线保留条数
CHECK_KEEP = 20                          # 体检记录保留条数
FILL_KEEP = 2000                         # 单只标的的成交上限（防手滑灌爆）
TOL = 0.01                               # 触及容差 = 1 个最小变动价位

CHECK_LEVELS = {"继续执行": "ok", "提高警惕": "warn", "暂停新动作": "warn", "建议作废重算": "bad"}


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


def _f(v, nd=2):
    return "—" if v is None else (("%." + str(int(nd)) + "f") % v)


def is_etf(node):
    """节点自带 是否ETF；老结构（标的 里挂）也认。"""
    node = node or {}
    return bool(node.get("是否ETF") or (node.get("标的") or {}).get("是否ETF"))


# ---------------------------------------------------------------------------
# 成交、持仓与盈亏（平均成本法）
# ---------------------------------------------------------------------------

def _account():
    from .store import load_account_bundle
    try:
        return load_account_bundle().get("配置") or {}
    except Exception:              # noqa: BLE001  账户文件坏了不该拖垮流计算
        return {}


def trade_fee(node, side, amount, account=None):
    """按账户费率算一笔费用；费率缺失时按 0 计并标注。"""
    acc = account if account is not None else _account()
    if not acc:
        return {"佣金": 0.0, "印花税": 0.0, "过户费": 0.0, "合计": 0.0, "费率缺失": True}
    out = aiplan.trade_cost(is_etf(node), side, amount or 0.0, acc)
    out["费率缺失"] = not any(num(acc.get(k)) for k in ("佣金费率_pct", "佣金最低_元"))
    return out


def _available(node, fills, qty, today):
    """可用股数：T+1 口径 —— 当日买入的部分不可卖；期初底仓沿用持仓文件里的可用。

    期初那笔是开流前就持有的底仓（不是当天买入），所以不按 T+1 冻结；
    但持仓文件里本来就冻结的部分（期初可用 < 期初股数）继续冻结。
    """
    today_buy = sum((num(f.get("数量")) or 0.0) for f in fills
                    if f.get("方向") == "买入" and str(f.get("日期") or "") == today
                    and str(f.get("来源")) != "期初")
    avail = qty - today_buy
    init = node.get("期初") or {}
    init_qty = num(init.get("股数")) or 0.0
    init_avail = num(init.get("可用"))
    frozen = init_qty - (init_qty if init_avail is None else init_avail)
    if frozen > 0:
        avail -= min(frozen, qty)
    return round(max(0.0, avail), 2)


def recompute(node, account=None, today=None):
    """按成交明细重算持仓与已实现盈亏（平均成本法）；就地写回节点并返回它。"""
    today = today or datetime.now().strftime("%Y-%m-%d")
    fills = sorted([f for f in (node.get("成交") or []) if isinstance(f, dict)],
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
            f["手续费"] = trade_fee(node, side, amount, account)["合计"]
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
    node["成交"] = fills
    node["持仓"] = {"股数": round(qty, 2), "可用": _available(node, fills, qty, today),
                    "平均成本": (round(cost_total / qty, 4) if qty > 1e-9 else None)}
    pnl = dict(node.get("盈亏") or {})
    pnl["已实现_元"] = round(realized, 2)
    pnl["投入成本_元"] = round(invested, 2)
    node["盈亏"] = pnl
    node["提示"] = warnings
    _settle_if_flat(node, today)
    return node


def _capital(node):
    """这只标的的资金分母：分配资金（老结构回退到 本流资金）。"""
    params = node.get("参数") or {}
    cap = num(params.get("分配资金"))
    if cap is None:
        cap = num(params.get("本流资金"))
    return cap or 0.0


def apply_price(node, price, source="东财实时行情", when=None, today=None):
    """现价 → 浮动 / 合计 / 收益率 / 目标进度 / 达标与止损标记（就地写回节点的盈亏）。"""
    today = today or datetime.now().strftime("%Y-%m-%d")
    qty = num((node.get("持仓") or {}).get("股数")) or 0.0
    avg = num((node.get("持仓") or {}).get("平均成本"))
    pnl = dict(node.get("盈亏") or {})
    realized = num(pnl.get("已实现_元")) or 0.0
    cap = _capital(node)
    invested = num(pnl.get("投入成本_元")) or 0.0
    # 目标 / 止损金额按这只标的的分配资金算（_sync 写在 参数 里），老数据回退到盈亏里的旧值
    params = node.get("参数") or {}
    target = num(params.get("目标盈利_元"))
    if target is None:
        target = num(pnl.get("目标盈利_元"))
    loss = num(params.get("最大亏损_元"))
    if loss is None:
        loss = num(pnl.get("最大亏损_元"))
    target, loss = target or 0.0, loss or 0.0
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
    node["盈亏"] = pnl
    _settle_if_flat(node, today)
    return pnl


def _settle_if_flat(node, today=None):
    """持仓归零且已有卖出 → 自动结算这只标的的状态（达标 / 未达标 / 止损）。"""
    if node.get("状态") != ACTIVE_STATE:
        return False
    qty = num((node.get("持仓") or {}).get("股数")) or 0.0
    if qty > 1e-9:
        return False
    if not any(str(f.get("方向")) == "卖出" for f in (node.get("成交") or [])):
        return False
    pnl = node.get("盈亏") or {}
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
    node["状态"] = state
    node["结束时间"] = now_str()
    node["结束原因"] = "持仓归零自动结算"
    events_append(node, "状态", "持仓已归零 → 结算为「%s」（合计 %s 元，收益率 %s%%）"
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


def marks(node, price=None, band_pct=None):
    """现价 vs 当前计划价位：接近（info）与触及（按类型分级别）。"""
    band = _clamp(band_pct if band_pct is not None else settings()["接近带_pct"],
                  0.05, 5.0, DEFAULT_SETTINGS["接近带_pct"]) / 100.0
    p = num(price)
    if p is None:
        p = num((node.get("盈亏") or {}).get("现价"))
    levels = (node.get("计划") or {}).get("关键价位") or {}
    if p is None or not levels:
        return []
    out = []

    def push(kind, item, hit_level, near_level, lots=None, basis=""):
        lo = num(item.get("下沿") if isinstance(item, dict) else item)
        hi = num(item.get("上沿") if isinstance(item, dict) else item)
        if lo is None and hi is None:
            return
        gap, side = _gap(p, lo, hi)
        if gap is None:
            return
        hit = gap <= (TOL / max(abs(p), 1e-9)) or side == "区间内" or gap <= 0.0001
        near = gap <= band
        act = (item.get("动作") if isinstance(item, dict) else "") or ""
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


def due_alerts(node, price=None, band_pct=None, today=None):
    """今天还没提醒过的标记（同类型+同价位只提醒一次，价格回落后再进入会重新提醒）。"""
    today = today or datetime.now().strftime("%Y-%m-%d")
    state = node.get("提醒状态") or {}
    if str(state.get("日期") or "") != today:
        state = {"日期": today, "已提醒": {}}
    done = state.get("已提醒") or {}
    fresh = [m for m in marks(node, price, band_pct) if m["键"] not in done]
    if fresh:
        node["提醒状态"] = state
    return fresh


def mark_alerted(node, items, today=None):
    today = today or datetime.now().strftime("%Y-%m-%d")
    state = node.get("提醒状态") or {}
    if str(state.get("日期") or "") != today:
        state = {"日期": today, "已提醒": {}}
    state.setdefault("已提醒", {})
    stamp = datetime.now().strftime("%H:%M:%S")
    for m in items or []:
        state["已提醒"][m.get("键")] = stamp
    node["提醒状态"] = state
    return node


def alert_items(node, items, flow_id=None):
    """把标记转成消息队列条目（字段与总控台到价提醒完全一致，另加流编号）。"""
    plan_path = (node.get("计划") or {}).get("产物路径")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for m in items or []:
        out.append({"时间": now, "代码": aiplan.code6(node.get("代码") or ""),
                    "名称": node.get("名称"), "点位类型": m.get("类型"),
                    "价位": m.get("价位"), "手数文本": "", "文案": m.get("文案"),
                    "报告路径": plan_path, "级别": m.get("级别"),
                    "流编号": flow_id})
    return out


def events_append(node, kind, text, level="info", price=None, source="系统"):
    rows = list(node.get("事件") or [])
    rows.append({"时间": now_str(), "类型": kind, "级别": level, "文案": text,
                 "价位": num(price), "来源": source})
    node["事件"] = rows[-EVENT_KEEP:]
    return node


# ---------------------------------------------------------------------------
# 手工补录 / 删除成交
# ---------------------------------------------------------------------------

def _next_seq(node):
    return max([int(num(f.get("序号")) or 0) for f in (node.get("成交") or [])] or [0]) + 1


def add_fill(node, side, price, qty, day=None, when=None, note="", source="手工",
             委托序号="", account=None):
    """记一笔成交（手工补录）。返回 (成交条目, 错误)。"""
    if node.get("状态") != ACTIVE_STATE:
        return None, "这只标的的流已经结束（%s），如需继续请另开一条" % node.get("状态")
    direction = "卖出" if str(side) == "卖出" else "买入"
    p, q = num(price), num(qty)
    if p is None or p <= 0:
        return None, "成交价必须大于 0"
    if q is None or q <= 0:
        return None, "成交数量必须大于 0"
    if len(node.get("成交") or []) >= FILL_KEEP:
        return None, "成交笔数过多（上限 %d 笔），先清理再记" % FILL_KEEP
    day = str(day or datetime.now().strftime("%Y-%m-%d"))
    fill = {"序号": _next_seq(node), "日期": day,
            "时间": str(when or datetime.now().strftime("%H:%M:%S"))[0:8],
            "方向": direction, "价格": round(float(p), 4), "数量": round(float(q), 2),
            "金额": round(float(p) * float(q), 2), "手续费": None, "来源": source,
            "委托序号": str(委托序号 or ""), "备注": str(note or "").strip(), "去重键": ""}
    fill["手续费"] = trade_fee(node, direction, fill["金额"], account)["合计"]
    node["成交"] = list(node.get("成交") or []) + [fill]
    recompute(node, account=account, today=day)
    events_append(node, "成交", "%s %s 股 @ %s（手续费 %s 元，来源：%s）"
                  % (direction, _f(q, 0), _f(p, 3), _f(fill["手续费"], 2), source),
                  "ok" if direction == "买入" else "warn", price=p, source=source)
    return fill, None


def delete_fill(node, seq, account=None):
    rows = list(node.get("成交") or [])
    target = [f for f in rows if int(num(f.get("序号")) or 0) == int(num(seq) or -1)]
    if not target:
        return None, "没有序号为 %s 的成交" % seq
    if str(target[0].get("来源")) == "期初":
        return None, "期初持仓不支持删除（它代表开流时的底仓）"
    node["成交"] = [f for f in rows if f not in target]
    recompute(node, account=account)
    events_append(node, "成交", "删除成交 #%s（手工纠错）" % seq, "warn")
    return node, None


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


def sync_ledger(node, account=None, log=None):
    """把台账里该标的新成交并进来（幂等）。返回新增条数。"""
    rows, skipped, mtime = ledger_rows(node.get("代码"), node.get("加入日"))
    have = {str(f.get("去重键") or "") for f in (node.get("成交") or [])}
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
                  for f in (node.get("成交") or []))
        if dup:                                   # 手工已经录过同一笔，不再重复
            have.add(key)
            continue
        fill, err = add_fill(node, direction, p, q, day=day,
                             when=str(row.get("时间") or "")[0:8], source="台账",
                             委托序号=row.get("委托序号"), account=account)
        if err:
            if log:
                log("[WARN] 台账里的一笔成交没并入：%s（%s）" % (row.get("委托序号") or "?", err))
            continue
        fill["去重键"] = key
        have.add(key)
        added += 1
    node["台账"] = {"游标行数": len(rows), "mtime": mtime, "未并入": skipped,
                    "最近同步": now_str()}
    if added:
        events_append(node, "同步", "从交易台账并入 %d 笔成交" % added, "ok", source="台账")
    return added


# ---------------------------------------------------------------------------
# 计划：挂到标的上 / 盘后重算判定 / 卡片上的「下一步」
# ---------------------------------------------------------------------------

def set_plan(node, payload, path, kind="首份计划", error=None):
    """把一份计划产物挂到这只标的上（关键价位用 planlines 的统一口径）。"""
    from . import track
    plan = ((payload or {}).get("研判") or {}).get("json") or {}
    md = re.sub(r"\.json$", ".md", path)
    node["计划"] = {
        "产物路径": rel(path), "md路径": rel(md) if os.path.exists(md) else None,
        "生成时间": (payload or {}).get("generated_at") or now_str(),
        "适用交易日": (payload or {}).get("适用交易日"),
        "数据交易日": (payload or {}).get("trade_date"),
        "打法": (payload or {}).get("打法") or node.get("打法"),
        "方向": plan.get("方向"), "置信度": plan.get("置信度"),
        "一句话结论": plan.get("一句话结论"),
        "条目": track.plan_items(payload), "关键价位": report_levels(path),
        "错误": error or ((payload or {}).get("研判") or {}).get("error"),
    }
    re_meta = dict(node.get("计划重算") or {})
    re_meta["上次生成日"] = datetime.now().strftime("%Y-%m-%d")
    re_meta["上次生成时间"] = now_str()
    node["计划重算"] = re_meta
    head = "%s / 置信度 %s" % (plan.get("方向") or "—", plan.get("置信度") or "—")
    events_append(node, "重算", "%s 已生成：%s" % (kind, head),
                  "warn" if node["计划"]["错误"] else "ok")
    return node


def plan_stale(node, now=None):
    """盘后重算提醒：交易日、过了设定时间、且今天这个点之后还没重算过。"""
    s = session_of(now)
    cfg = settings()
    if not s["是否交易日"]:
        return {"待重算": False, "说明": "非交易日不重算"}
    today = str(s["现在"])[:10]
    hhmm = str(s["现在"])[11:16]
    if hhmm < str(cfg["盘后重算时间"]):
        return {"待重算": False, "说明": "还没到 %s" % cfg["盘后重算时间"]}
    stamp = str((node.get("计划") or {}).get("生成时间") or "")
    if stamp >= "%s %s" % (today, cfg["盘后重算时间"]):
        return {"待重算": False, "说明": "今天已按最新数据重算过"}
    if not node.get("计划"):
        return {"待重算": True, "说明": "还没有计划，先出一份"}
    return {"待重算": True, "说明": "今天的盘后计划还没重算"}


def next_action(node, price=None):
    """卡片上的「操作」：优先到价标记，其次第一条可执行计划条目（**只给精确价，不给区间**）。"""
    ms = marks(node)
    if ms:
        rank = {"bad": 0, "accent": 1, "ok": 2, "warn": 3, "info": 4}
        top = sorted(ms, key=lambda m: rank.get(m.get("级别"), 9))[0]
        return top.get("文案") or ""
    items = [i for i in ((node.get("计划") or {}).get("条目") or []) if not i.get("无动作")]
    if not items:
        return "还没有计划：点「计算」生成一份"
    r = items[0]
    from .planlines import trigger_price
    px = num(r.get("精确价"))
    if px is None:
        px = trigger_price(r.get("价格区间"), r.get("动作"),
                           price if price is not None else (node.get("盈亏") or {}).get("现价"))
    txt = _f(px, 3) if px is not None else "—"
    qty = num(r.get("股数"))
    return "%s %s%s" % (r.get("动作") or "—", txt, ("（%s 股）" % _f(qty, 0)) if qty else "")


def exec_record(node):
    """把流内成交折成 track 事实包认识的「执行情况」结构（成交就是执行情况）。"""
    rows = []
    for i, f in enumerate(node.get("成交") or []):
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
    return {"录入时间": (node.get("台账") or {}).get("最近同步") or node.get("加入时间"),
            "条目": rows, "总体备注": "流内成交明细共 %d 笔" % len(rows)}


def add_check(node, result, note="", model=None, usage=None, cost=None, error=None,
              fact_chars=None):
    """记录一次体检（结论 / 依据 / 处理建议）到这只标的上。"""
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
    node["体检"] = (list(node.get("体检") or []) + [rec])[-CHECK_KEEP:]
    level = CHECK_LEVELS.get(rec["结论"], "info")
    if error and rec["结论"] == "未判定":
        level = "warn"
    events_append(node, "体检", "体检：%s —— %s" % (rec["结论"], rec["一句话"] or "（无结论）"), level)
    return rec


# ---------------------------------------------------------------------------
# 交易台账（只读）：持仓页的「交易明细」卡
# ---------------------------------------------------------------------------

LEDGER_VIEW_LIMIT = 500


def ledger_view(code=None, limit=LEDGER_VIEW_LIMIT):
    """台账 → 页面用的成交明细（只读，不写盘）。

    台账由同花顺「同步持仓」写入 data/user/交易台账.md（列口径见 holdings_sync），
    这里只解析展示：不做去重合并、不改文件。
    """
    import time
    from .paths import read_text
    c6 = aiplan.code6(code) if code else None
    try:
        mtime = os.path.getmtime(LEDGER_PATH)
    except OSError:
        mtime = 0.0
    rows, summary = [], {}
    for row in parse_ledger(read_text(LEDGER_PATH)):
        row_code = aiplan.code6(row.get("代码"))
        price, qty = num(row.get("成交价格")), num(row.get("成交数量"))
        amount = num(row.get("成交金额"))
        if amount is None and price is not None and qty is not None:
            amount = round(price * qty, 2)
        item = {"日期": row.get("日期"), "时间": row.get("时间"),
                "代码": row_code, "名称": row.get("名称"), "方向": row.get("方向"),
                "价格": price, "数量": qty, "金额": amount,
                "市场": row.get("交易市场"), "委托序号": row.get("委托序号")}
        rows.append(item)
        hit = summary.get(row_code)
        if hit is None:
            summary[row_code] = {"代码": row_code, "名称": row.get("名称"), "笔数": 1}
        else:
            hit["笔数"] += 1
    if c6:
        rows = [r for r in rows if r["代码"] == c6]
    rows.reverse()                      # 最新成交排在最前
    try:
        limit = max(1, min(int(limit or LEDGER_VIEW_LIMIT), 2000))
    except (TypeError, ValueError):
        limit = LEDGER_VIEW_LIMIT
    total = len(rows)
    days = sorted({str(r.get("日期") or "") for r in rows if str(r.get("日期") or "")})
    return {"行": rows[:limit], "总数": total, "显示": min(limit, total),
            "台账": rel(LEDGER_PATH), "存在": os.path.exists(LEDGER_PATH),
            "更新时间": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)) if mtime else None,
            "标的数": len(summary),
            "标的": [summary[k] for k in sorted(summary, key=lambda x: (-summary[x]["笔数"], x or ""))],
            "覆盖": {"起": days[0], "止": days[-1]} if days else None,
            "口径": "台账由「同步同花顺」写入 data/user/交易台账.md：每次同步拉**近一周成交**"
                    "（当日成交 + 历史成交，按委托序号自动去重）；这里只读不写"}
