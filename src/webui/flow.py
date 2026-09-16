# -*- coding: utf-8 -*-
"""交易流（容器层）：一条流 = 一笔资金 + 若干只标的，每只标的选一种打法。

设计口径：
* **资金是「流」的属性**：目标收益率与最大亏损都按流派资金算；流内所有标的的
  「分配资金」之和不得超过流资金（创建 / 加标的 / 改参数三处都拦）。
* **一只标的只属于一条流**：同一标的在别的流里还没结束时，不允许再开一条。
* 每只标的选一种**打法**（超短线 / 短线 / 波段）：打法写进事实包与提示词，
  计划的时间尺度、止损宽窄与加减仓节奏都按它生成；判定口径不变。
* 一只标的的账本（成交 / 持仓 / 盈亏 / 到价 / 台账 / 计划 / 体检）在 flowbook.py，
  这里只管「流」：参数、标的清单、聚合、开流 / 加标的 / 结束 / 删除、文件读写。

文件：`data/ai/flows/F-<YYYYMMDD>-<NN>.json`（老结构 `<代码>-<YYYYMMDD>.json` 读入时自动升级）。
"""

import json
import os
import re
from datetime import datetime

from . import flowbook
from .flowbook import (BANDS, CHECK_LEVELS, DEFAULT_SETTINGS, INTERVALS,  # noqa: F401
                       add_check, add_fill, alert_items, apply_price, delete_fill,
                       due_alerts, events_append, exec_record, mark_alerted, marks,
                       next_action, plan_stale, plancheck_lots, recompute, save_settings,
                       set_plan, settings, sync_ledger, trade_fee,
                       name_book, resolve_name)
from .paths import FLOW_DIR, TRASH_DIR, aiplan, atomic_write, num, now_str, rel

FLOW_VERSION = 2
ACTIVE_STATE = flowbook.ACTIVE_STATE
STATES = flowbook.STATES
STYLES = ("超短线", "短线", "波段")
DEFAULT_STYLE = "短线"
# 打法口径：不只给页面显示，也直接进事实包与提示词（模型要按这个时间尺度出计划）。
STYLE_HINT = {
    "超短线": "持股 1-3 个交易日：只做放量突破 / 缩量回踩的当日或次日机会，"
              "止损要紧（通常 1%-3%），到小目标就落袋，时间优先于价格。",
    "短线": "持股 1-5 个交易日：按 3-5 日节奏给买点、止损与止盈，"
            "跌破关键均线或平台就离场，不参与回撤里的补仓摊薄。",
    "波段": "持股 2-6 周：以周线为背景做波段，容忍更宽的波动，"
            "用分批加减仓与趋势跟踪，止损放到结构位下方。",
}
CAP_TOL = 0.01                     # 资金校验容差（元）

_FLOW_CACHE = {}                   # 路径 -> (mtime, doc)


# ---------------------------------------------------------------------------
# 文件读写与列举
# ---------------------------------------------------------------------------

def flow_id(day=None):
    """新编号：F-<YYYYMMDD>-<NN>（一天内递增；与标的数量无关，可以随时加标的）。"""
    d = (day or datetime.now().strftime("%Y-%m-%d")).replace("-", "")
    n = 0
    if os.path.isdir(FLOW_DIR):
        for name in os.listdir(FLOW_DIR):
            m = re.match(r"^F-%s-(\d+)\.json$" % d, name)
            if m:
                n = max(n, int(m.group(1)))
    return "F-%s-%02d" % (d, n + 1)


def flow_path(fid):
    return os.path.join(FLOW_DIR, "%s.json" % str(fid or "").strip())


def is_flow_file(name):
    return bool(re.match(r"^F-\d{8}-\d+\.json$", name or "")
                or re.match(r"^\d{6}-\d{8}\.json$", name or ""))


def is_etf_code(c6):
    return str(c6 or "").startswith(("51", "52", "56", "58", "15", "16"))


def normalize(doc):
    """读入统一升级成 v2：老结构（一条流一只标的、参数.本流资金）自动折成标的清单。"""
    if not isinstance(doc, dict):
        return None
    if doc.get("版本") == FLOW_VERSION and isinstance(doc.get("标的"), list):
        _sync(doc)
        return doc
    params = dict(doc.get("参数") or {})
    cap = num(params.get("流资金")) or num(params.get("本流资金")) or 0.0
    sym = doc.get("标的") if isinstance(doc.get("标的"), dict) else {}
    sym = sym or {}
    fid = str(doc.get("流编号") or "")
    c6 = aiplan.code6(sym.get("代码") or "") or aiplan.code6(fid.split("-")[0]) or ""
    node = {"代码": c6, "名称": sym.get("名称") or c6 or fid,
            "是否ETF": bool(sym.get("是否ETF")) or is_etf_code(c6),
            "打法": params.get("打法") or DEFAULT_STYLE, "分配资金": round(cap, 2),
            "加入日": doc.get("起始日"), "加入时间": doc.get("创建时间"),
            "状态": doc.get("状态") or ACTIVE_STATE,
            "结束时间": doc.get("结束时间"), "结束原因": doc.get("结束原因")}
    for key, empty in (("期初", {}), ("成交", []), ("持仓", {}), ("盈亏", {}), ("计划", {}),
                       ("体检", []), ("事件", []), ("提醒状态", {}), ("计划重算", {}),
                       ("台账", {}), ("提示", [])):
        node[key] = doc.get(key) if doc.get(key) is not None else empty
    out = {"流编号": fid, "版本": FLOW_VERSION, "状态": doc.get("状态") or ACTIVE_STATE,
           "创建时间": doc.get("创建时间") or now_str(), "起始日": doc.get("起始日"),
           "结束时间": doc.get("结束时间"), "结束原因": doc.get("结束原因"),
           "参数": {"流资金": round(cap, 2),
                    "目标收益率_pct": num(params.get("目标收益率_pct")) or 0.0,
                    "最大亏损_pct": num(params.get("最大亏损_pct")) or 0.0,
                    "备注": params.get("备注") or ""},
           "标的": [node], "事件": doc.get("事件") or []}
    _sync(out)
    return out


def load_flow(fid):
    path = flow_path(fid)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None, "交易流不存在：%s" % fid
    hit = _FLOW_CACHE.get(path)
    if hit and hit[0] == mtime:
        return hit[1], None
    doc = normalize(aiplan.read_json(path))
    if not doc:
        return None, "交易流读不出内容：%s" % rel(path)
    _FLOW_CACHE[path] = (mtime, doc)
    return doc, None


def save_flow(doc):
    _sync(doc)
    aggregate(doc)
    _refresh_state(doc)
    path = flow_path(doc.get("流编号"))
    atomic_write(path, json.dumps(doc, ensure_ascii=False, indent=1), newline="\n")
    try:
        _FLOW_CACHE[path] = (os.path.getmtime(path), doc)
    except OSError:
        _FLOW_CACHE.pop(path, None)
    return path


def list_flows(active_only=False):
    """全部流（按创建时间倒序）；active_only 只返回「进行中」。"""
    out = []
    if not os.path.isdir(FLOW_DIR):
        return out
    for name in os.listdir(FLOW_DIR):
        if not is_flow_file(name):
            continue
        doc, _err = load_flow(name[:-5])
        if not doc:
            continue
        if active_only and doc.get("状态") != ACTIVE_STATE:
            continue
        out.append(doc)
    out.sort(key=lambda d: str(d.get("创建时间") or ""), reverse=True)
    return out


def targets(doc):
    rows = doc.get("标的")
    return [n for n in rows if isinstance(n, dict)] if isinstance(rows, list) else []


def active_targets(doc):
    return [n for n in targets(doc) if n.get("状态") == ACTIVE_STATE]


def find_target(doc, code):
    c6 = aiplan.code6(code or "")
    for node in targets(doc):
        if aiplan.code6(node.get("代码") or "") == c6:
            return node, None
    return None, "这条流里没有标的 %s" % (code or "")


def symbols(doc):
    """标的摘要（页面 / 提示词用）：代码 / 名称 / 打法 / 分配资金。"""
    return [{"代码": n.get("代码"), "名称": n.get("名称"), "打法": n.get("打法") or DEFAULT_STYLE,
             "分配资金": n.get("分配资金"), "状态": n.get("状态")} for n in targets(doc)]


def allocated(doc):
    return round(sum(num(n.get("分配资金")) or 0.0 for n in targets(doc)), 2)


def remaining(doc):
    cap = num((doc.get("参数") or {}).get("流资金")) or 0.0
    return round(cap - allocated(doc), 2)


def active_capital(exclude=None):
    """所有进行中的流占用的流资金合计（exclude 传流编号时排除自己）。"""
    total = 0.0
    for doc in list_flows(active_only=True):
        if exclude and doc.get("流编号") == exclude:
            continue
        total += num((doc.get("参数") or {}).get("流资金")) or 0.0
    return round(total, 2)


def flow_id_of_code(code, exclude=None):
    """这只标的落在哪条进行中的流里（一只标的只属于一条流）。"""
    c6 = aiplan.code6(code or "")
    for doc in list_flows(active_only=True):
        if exclude and doc.get("流编号") == exclude:
            continue
        for node in targets(doc):
            if aiplan.code6(node.get("代码") or "") == c6 and node.get("状态") == ACTIVE_STATE:
                return doc.get("流编号")
    return None


# ---------------------------------------------------------------------------
# 参数同步与聚合
# ---------------------------------------------------------------------------

def _sync(doc):
    """把流参数铺到每只标的上（分配资金 → 目标盈利 / 最大亏损）。"""
    params = doc.setdefault("参数", {})
    cap = num(params.get("流资金")) or 0.0
    tgt = num(params.get("目标收益率_pct")) or 0.0
    los = num(params.get("最大亏损_pct")) or 0.0
    for node in targets(doc):
        alloc = num(node.get("分配资金"))
        if alloc is None:
            alloc = num((node.get("参数") or {}).get("本流资金")) or 0.0
            node["分配资金"] = round(alloc, 2)
        c6 = aiplan.code6(node.get("代码") or "")
        if c6 and "是否ETF" not in node:
            node["是否ETF"] = is_etf_code(c6)
        if not node.get("打法"):
            node["打法"] = DEFAULT_STYLE
        node["参数"] = {"分配资金": round(float(alloc), 2), "目标收益率_pct": round(tgt, 3),
                        "最大亏损_pct": round(los, 3),
                        "目标盈利_元": round(float(alloc) * tgt / 100.0, 2),
                        "最大亏损_元": round(float(alloc) * los / 100.0, 2),
                        "流派资金": round(cap, 2)}
    return doc


def aggregate(doc):
    """流级汇总（写进 doc["汇总"] 供页面直接读）：盈亏 / 进度 / 资金占用。"""
    params = doc.get("参数") or {}
    cap = num(params.get("流资金")) or 0.0
    tgt_pct = num(params.get("目标收益率_pct")) or 0.0
    loss_pct = num(params.get("最大亏损_pct")) or 0.0
    nodes = targets(doc)
    realized = floating = invested = qty = 0.0
    floating_known = True
    for node in nodes:
        pnl = node.get("盈亏") or {}
        realized += num(pnl.get("已实现_元")) or 0.0
        invested += num(pnl.get("投入成本_元")) or 0.0
        f = num(pnl.get("浮动_元"))
        if f is None:
            floating_known = False
        else:
            floating += f
        qty += num((node.get("持仓") or {}).get("股数")) or 0.0
    floating_v = round(floating, 2) if floating_known else None
    total = round(realized + (floating or 0.0), 2)
    tgt_amt = round(cap * tgt_pct / 100.0, 2)
    loss_amt = round(cap * loss_pct / 100.0, 2)
    out = {"标的数": len(nodes), "进行中": len(active_targets(doc)),
           "流资金": round(cap, 2), "分配合计": allocated(doc), "剩余资金": remaining(doc),
           "已实现_元": round(realized, 2), "浮动_元": floating_v,
           "合计_元": total, "投入成本_元": round(invested, 2), "持仓_股": round(qty, 2),
           "收益率_pct": round(total / cap * 100, 3) if cap else None,
           "按成本收益率_pct": round(total / invested * 100, 3) if invested else None,
           "目标盈利_元": tgt_amt, "最大亏损_元": loss_amt,
           "进度_pct": round(total / tgt_amt * 100, 2) if tgt_amt else None,
           "达标": bool(tgt_amt and total >= tgt_amt - 1e-9),
           "触及最大亏损": bool(loss_amt and total <= -loss_amt + 1e-9)}
    doc["汇总"] = out
    return out


def _refresh_state(doc):
    """所有标的都结束了 → 流自动结束（还有标的在跑就不因为单只结算而结束）。"""
    if doc.get("状态") != ACTIVE_STATE:
        return doc
    nodes = targets(doc)
    if not nodes or active_targets(doc):
        return doc
    doc["状态"] = "已结束"
    doc["结束时间"] = doc.get("结束时间") or now_str()
    doc["结束原因"] = doc.get("结束原因") or "流内标的都已结束"
    events_append(doc, "状态", "流内 %d 只标的都已结束 → 整条流结束" % len(nodes), "warn")
    return doc


# ---------------------------------------------------------------------------
# 开流 / 加标的 / 改参数 / 结束 / 删除
# ---------------------------------------------------------------------------

def _start_block(start, day, node_key):
    """期初持仓块 + 那条「来源=期初」的虚拟成交（不计手续费）。"""
    qty = num((start or {}).get("股数"))
    cost = num((start or {}).get("成本价"))
    if not qty or qty <= 0:
        return {"股数": 0.0, "可用": 0.0, "成本价": None,
                "说明": "空仓开流（先出建仓计划）"}, []
    if not cost or cost <= 0:
        return None, "按持仓文件带入时必须有成本价"
    avail = num((start or {}).get("可用"))
    block = {"股数": float(qty), "可用": float(avail if avail is not None else qty),
             "成本价": float(cost), "说明": "期初持仓（按持仓文件带入）"}
    fill = {"序号": 1, "日期": day, "时间": datetime.now().strftime("%H:%M:%S"),
            "方向": "买入", "价格": float(cost), "数量": float(qty),
            "金额": round(float(cost) * float(qty), 2), "手续费": 0.0,
            "来源": "期初", "委托序号": "", "备注": "期初持仓（不计手续费）",
            "去重键": "期初-%s" % node_key}
    return block, [fill]


def _check_capital(doc, add_alloc, exclude_code=None):
    """流内所有标的的分配资金之和不得超过流资金。"""
    cap = num((doc.get("参数") or {}).get("流资金")) or 0.0
    used = 0.0
    for node in targets(doc):
        if exclude_code and aiplan.code6(node.get("代码") or "") == aiplan.code6(exclude_code):
            continue
        used += num(node.get("分配资金")) or 0.0
    if used + add_alloc > cap + CAP_TOL:
        return ("流内标的分配资金合计 %s 元 + 本次 %s 元 会超过流资金 %s 元"
                % ("%.2f" % used, "%.2f" % add_alloc, "%.2f" % cap))
    return None


def create_flow(capital=None, target_pct=None, loss_pct=None, note="", code=None, name="",
                style=None, alloc=None, start=None, account_total=None, today=None):
    """开一条流（写盘）；给了 code 就同时加第一只标的。返回 (doc, 错误)。"""
    cap = num(capital)
    if not cap or cap <= 0:
        return None, "流资金必须大于 0"
    tgt = num(target_pct)
    if not tgt or tgt <= 0:
        return None, "目标收益率必须大于 0"
    los = num(loss_pct)
    if not los or los <= 0:
        return None, "最大亏损必须大于 0"
    if num(account_total):
        used = active_capital()
        if used + cap > float(account_total) + 1e-6:
            return None, ("在跑的流资金合计 %s 元，再加 %s 元会超过账户总资金 %s 元"
                          % ("%.2f" % used, "%.2f" % cap, "%.2f" % float(account_total)))
    day = today or datetime.now().strftime("%Y-%m-%d")
    fid = flow_id(day)
    doc = {"流编号": fid, "版本": FLOW_VERSION, "状态": ACTIVE_STATE,
           "创建时间": now_str(), "起始日": day, "结束时间": None, "结束原因": None,
           "参数": {"流资金": round(float(cap), 2), "目标收益率_pct": round(float(tgt), 3),
                    "最大亏损_pct": round(float(los), 3), "备注": str(note or "").strip()},
           "标的": [], "事件": []}
    events_append(doc, "开流", "开流：流资金 %s 元 ｜ 目标 %s%%（%s 元）｜ 最大亏损 %s%%（%s 元）"
                  % ("%.2f" % cap, "%.3f" % tgt, "%.2f" % round(cap * tgt / 100.0, 2),
                     "%.3f" % los, "%.2f" % round(cap * los / 100.0, 2)), "ok")
    if code:
        _node, err = add_target(doc, code, name, style=style,
                                alloc=cap if alloc in (None, "") else alloc, start=start,
                                today=day, save=False)
        if err:
            return None, err
    save_flow(doc)
    return doc, None


def add_target(doc, code, name="", style=None, alloc=None, start=None, today=None, save=True):
    """给这条流加一只标的（分配资金默认取剩余）。返回 (标的节点, 错误)。"""
    if doc.get("状态") != ACTIVE_STATE:
        return None, "这条流已经结束（%s），不能再加标的" % doc.get("状态")
    c6 = aiplan.code6(code or "")
    if not c6:
        return None, "请填 6 位证券代码"
    if any(aiplan.code6(n.get("代码") or "") == c6 for n in targets(doc)):
        return None, "这只标的已经在这条流里了"
    other = flow_id_of_code(c6, exclude=doc.get("流编号"))
    if other:
        return None, "该标的在交易流 %s 里还没结束，一只标的只属于一条流" % other
    style = str(style or DEFAULT_STYLE).strip()
    if style not in STYLES:
        return None, "打法只能是：%s" % " / ".join(STYLES)
    rem = remaining(doc)
    if rem <= 0:
        return None, "流资金已经分完（剩余 0 元）：先调大流资金，或改小别的标的的分配资金"
    amount = rem if alloc in (None, "") else num(alloc)
    if amount is None or amount <= 0:
        return None, "分配资金必须大于 0（这条流还剩 %.2f 元）" % rem
    err = _check_capital(doc, amount)
    if err:
        return None, err
    day = today or datetime.now().strftime("%Y-%m-%d")
    block, fills = _start_block(start, day, doc.get("流编号") or c6)
    if block is None:
        return None, fills                       # 期初持仓缺成本价
    # 名称：页面上没填就用持仓 / 自选里的真名（批注 1：流里存的是代码时显示不出标的）
    name = (name or "").strip() or name_book().get(c6) or c6
    node = {"代码": c6, "名称": name, "是否ETF": is_etf_code(c6),
            "打法": style, "分配资金": round(float(amount), 2), "加入日": day,
            "加入时间": now_str(), "状态": ACTIVE_STATE, "结束时间": None, "结束原因": None,
            "期初": block, "成交": fills, "持仓": {}, "盈亏": {}, "计划": {}, "体检": [],
            "事件": [], "提醒状态": {"日期": "", "已提醒": {}},
            "计划重算": {"上次生成日": None, "提醒过": None, "自动补跑": False},
            "台账": {"游标行数": 0, "mtime": 0.0, "未并入": 0, "最近同步": None},
            "提示": []}
    doc["标的"] = targets(doc) + [node]
    events_append(doc, "加标的", "加入标的 %s %s（打法 %s，分配资金 %s 元）"
                  % (c6, node["名称"], style, "%.2f" % amount), "ok")
    events_append(node, "开流", "%s 加入交易流 %s（打法 %s，分配资金 %s 元）"
                  % (node["名称"], doc.get("流编号"), style, "%.2f" % amount), "ok")
    recompute(node)
    _sync(doc)
    aggregate(doc)
    if save:
        save_flow(doc)
    return node, None


def remove_target(doc, code, save=True):
    """把一只标的从流里拿掉（成交与计划引用一起移除；计划产物文件仍留在 data/ai/track）。"""
    node, err = find_target(doc, code)
    if err:
        return None, err
    doc["标的"] = [n for n in targets(doc) if n is not node]
    events_append(doc, "移除标的", "移除标的 %s %s（分配资金 %s 元退回流）"
                  % (node.get("代码"), node.get("名称"),
                     "%.2f" % (num(node.get("分配资金")) or 0.0)), "warn")
    _sync(doc)
    aggregate(doc)
    if save:
        save_flow(doc)
    return doc, None


def set_target(doc, code, style=None, alloc=None, save=True):
    """改一只标的的打法 / 分配资金。"""
    node, err = find_target(doc, code)
    if err:
        return None, err
    style = str(style).strip() if style not in (None, "") else None
    if style and style not in STYLES:
        return None, "打法只能是：%s" % " / ".join(STYLES)
    amount = None
    if alloc not in (None, ""):
        amount = num(alloc)
        if amount is None or amount <= 0:
            return None, "分配资金必须大于 0"
        err = _check_capital(doc, amount, exclude_code=node.get("代码"))
        if err:
            return None, err
    notes = []
    if style:
        notes.append("打法 %s → %s" % (node.get("打法"), style))
        node["打法"] = style
    if amount is not None:
        notes.append("分配资金 %s → %s 元" % ("%.2f" % (num(node.get("分配资金")) or 0.0),
                                        "%.2f" % amount))
        node["分配资金"] = round(float(amount), 2)
    if notes:
        events_append(node, "调整", "；".join(notes), "info")
        events_append(doc, "调整", "%s %s：%s"
                      % (node.get("代码"), node.get("名称"), "；".join(notes)), "info")
    _sync(doc)
    aggregate(doc)
    if save:
        save_flow(doc)
    return doc, None


def set_params(doc, capital=None, target_pct=None, loss_pct=None, note=None, save=True):
    """改流级参数（流资金 / 目标收益率 / 最大亏损 / 备注）。"""
    params = doc.setdefault("参数", {})
    if capital not in (None, ""):
        cap = num(capital)
        if cap is None or cap <= 0:
            return None, "流资金必须大于 0"
        used = allocated(doc)
        if used > cap + CAP_TOL:
            return None, ("流内标的分配资金合计 %s 元，流资金不能小于它"
                          "（可以先改小各标的的分配资金）" % ("%.2f" % used))
        params["流资金"] = round(float(cap), 2)
    if target_pct not in (None, ""):
        v = num(target_pct)
        if v is None or v <= 0:
            return None, "目标收益率必须大于 0"
        params["目标收益率_pct"] = round(float(v), 3)
    if loss_pct not in (None, ""):
        v = num(loss_pct)
        if v is None or v <= 0:
            return None, "最大亏损必须大于 0"
        params["最大亏损_pct"] = round(float(v), 3)
    if note is not None:
        params["备注"] = str(note).strip()
    events_append(doc, "调整", "流参数：资金 %s 元 ｜ 目标 %s%% ｜ 最大亏损 %s%%"
                  % ("%.2f" % (num(params.get("流资金")) or 0.0),
                     "%.3f" % (num(params.get("目标收益率_pct")) or 0.0),
                     "%.3f" % (num(params.get("最大亏损_pct")) or 0.0)), "info")
    _sync(doc)
    aggregate(doc)
    if save:
        save_flow(doc)
    return doc, None


def close_flow(doc, reason="人工结束", state=None, code=None, save=True):
    """结束整条流（默认）或其中一只标的（给了 code）。"""
    if code:
        node, err = find_target(doc, code)
        if err:
            return None, err
        if node.get("状态") != ACTIVE_STATE:
            return None, "%s 已经结束（%s）" % (node.get("名称"), node.get("状态"))
        node["状态"] = state or "已结束"
        node["结束时间"] = now_str()
        node["结束原因"] = str(reason or "人工结束").strip()
        events_append(node, "状态", "结束这只标的：%s" % node["结束原因"], "warn")
        events_append(doc, "状态", "结束标的 %s %s：%s"
                      % (node.get("代码"), node.get("名称"), node["结束原因"]), "warn")
    else:
        if doc.get("状态") != ACTIVE_STATE:
            return None, "这条流已经结束（%s）" % doc.get("状态")
        for node in active_targets(doc):
            node["状态"] = state or "已结束"
            node["结束时间"] = now_str()
            node["结束原因"] = str(reason or "人工结束").strip()
            events_append(node, "状态", "结束这只标的：%s" % node["结束原因"], "warn")
        doc["状态"] = state or "已结束"
        doc["结束时间"] = now_str()
        doc["结束原因"] = str(reason or "人工结束").strip()
        events_append(doc, "状态", "结束这条流：%s" % doc["结束原因"], "warn")
    _refresh_state(doc)
    aggregate(doc)
    if save:
        save_flow(doc)
    return doc, None


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
    return {"移入": [rel(dst)], "回收站": rel(dest),
            "标的": [n.get("名称") for n in targets(doc)],
            "说明": "交易流已移入回收站，超过 7 天会真正删除。"}, None
