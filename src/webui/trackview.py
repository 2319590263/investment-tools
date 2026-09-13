# -*- coding: utf-8 -*-
"""标的跟踪的页面数据组装（/api/track/all 与 /api/track）。

只做「读产物 + 拼卡片」：取数与模型调用在 track_run.py，纯逻辑与产物读写都在 track.py。
把页面组装单独分出来的原因与 pickrank.py 一样——视图数据变化最频繁，别把纯逻辑搅在一起。
"""

import time

from . import quotes
from .paths import aiplan, rel
from .planlines import lots_text, report_levels
from .store import load_holdings_bundle, load_tracklist
from .track import (MAX_CHARS_DEFAULT, MAX_CHARS_MAX, MAX_CHARS_MIN, STALE_DAYS,
                    TRACK_DIR, TRACKLIST_PATH, abs_path, apply_trade_date, exec_summary,
                    latest_plan_item, list_plans, load_exec, mech_reference, plan_items,
                    quote_brief, reference_prev)

# ---------------------------------------------------------------------------
# 页面数据（/api/track/all 与 /api/track）
# ---------------------------------------------------------------------------

def _lots(code, levels):
    """买点 / 减仓 / 止损 / 止盈各自的手数文案（与总控台卡片同一口径）。"""
    hold = {}
    try:
        for row in (load_holdings_bundle().get("持仓") or []):
            if row.get("代码") == aiplan.code6(code or ""):
                hold = row
                break
    except Exception:            # noqa: BLE001  持仓文件坏了不该拖垮跟踪页
        hold = {}
    buy = (levels or {}).get("买点") or {}
    sell = (levels or {}).get("减仓") or {}
    return {"买点": lots_text(buy.get("股数")), "减仓": lots_text(sell.get("股数")),
            "止损": lots_text(hold.get("可用股数_可卖")), "止盈": lots_text(hold.get("持有股数")),
            "持有股数": hold.get("持有股数"), "可用股数_可卖": hold.get("可用股数_可卖"),
            "是否持仓": bool(hold)}


def _stale(item):
    """最新计划是否已经超过 STALE_DAYS 天没更新。"""
    try:
        age = time.time() - float((item or {}).get("mtime") or 0)
    except (TypeError, ValueError):
        return False
    return age > STALE_DAYS * 86400.0


def _card(listed, item, quote, apply):
    """清单里的一行：清单信息 + 最新计划摘要 + 执行摘要 + 状态标记。"""
    listed = listed or {}
    code = aiplan.code6(listed.get("代码") or (item or {}).get("代码") or "")
    doc = aiplan.read_json(abs_path(item["json路径"])) if item else None
    brief = quote_brief(listed, quote, doc)
    apply_date = (item or {}).get("适用交易日")
    exec_doc = (item or {}).get("执行") or exec_summary(None)
    return {
        "代码": code,
        "名称": listed.get("名称") or (item or {}).get("名称") or code,
        "备注": listed.get("备注") or "", "在缓存": listed.get("在缓存"),
        "现价": brief.get("价格"), "涨跌幅_pct": brief.get("涨跌幅_pct"),
        "价格来源": brief.get("来源"), "价格时间": brief.get("时间"),
        "价格口径": brief.get("口径"),
        "计划路径": (item or {}).get("json路径"), "md路径": (item or {}).get("md路径"),
        "生成时间": (item or {}).get("时间"), "适用交易日": apply_date,
        "数据交易日": (item or {}).get("数据交易日"),
        "方向": (item or {}).get("方向"), "置信度": (item or {}).get("置信度"),
        "一句话结论": (item or {}).get("一句话结论"), "计划条数": (item or {}).get("计划条数") or 0,
        "执行": exec_doc, "历史条数": len(list_plans(code)),
        "待生成": (not item) or (apply_date != apply["适用交易日"]),
        "需要执行记录": bool((not exec_doc.get("是否录入")) and apply_date
                            and str(apply_date) < apply["适用交易日"]),
        "计划已过期": _stale(item) if item else False,
    }


def build_overview(refresh=False):
    """跟踪页首屏：清单 + 每标的的最新计划与执行情况 + 待生成清单。"""
    listed = load_tracklist()
    apply = apply_trade_date()
    codes = [it["代码"] for it in listed]
    quote_map, hints = {}, []
    if refresh and codes:
        quote_map, hints = quotes.fetch_quotes(codes, refresh=True)
    cards, pending, need_exec = [], [], []
    for it in listed:
        card = _card(it, latest_plan_item(it["代码"]), quote_map.get(it["代码"]), apply)
        cards.append(card)
        if card["待生成"]:
            pending.append(card["代码"])
        if card["需要执行记录"]:
            need_exec.append(card["代码"])
    if not refresh:
        hints.append("当前是本地口径价格（清单缓存 / 上一份计划）：点「刷新实盘价」"
                     "或用总控台的自动刷新取实时价")
    return {"时段": apply["时段"], "适用交易日": apply["适用交易日"],
            "交易日口径": apply["口径"], "清单": cards,
            "待生成": pending, "待录入执行": need_exec,
            "刷新": {"模式": "实时" if refresh else "本地", "时间": time.strftime("%H:%M:%S"),
                     "来源": "东财批量报价" if refresh else "本地缓存",
                     "报价条数": len(quote_map), "提示": hints},
            "路径": rel(TRACKLIST_PATH), "目录": rel(TRACK_DIR),
            "最大字符默认": MAX_CHARS_DEFAULT, "最大字符下限": MAX_CHARS_MIN,
            "最大字符上限": MAX_CHARS_MAX}


def _history_rows(plans, limit=30):
    out = []
    for it in plans[:limit]:
        out.append({"时间": it["时间"], "适用交易日": it.get("适用交易日"),
                    "方向": it.get("方向"), "置信度": it.get("置信度"),
                    "一句话结论": it.get("一句话结论"), "计划条数": it.get("计划条数"),
                    "执行": it.get("执行"), "json路径": it["json路径"],
                    "md路径": it.get("md路径")})
    return out


def build_detail(code, refresh=False):
    """单个标的的跟踪详情：最新计划 + 执行记录 + 机械参考 + 历史时间线。"""
    c6 = aiplan.code6(code or "")
    if not c6:
        return None, "请填 6 位证券代码"
    listed = None
    for it in load_tracklist():
        if it["代码"] == c6:
            listed = it
            break
    plans = list_plans(c6)
    latest = plans[0] if plans else None
    apply = apply_trade_date()
    quote_map, hints = {}, []
    if refresh:
        quote_map, hints = quotes.fetch_quotes([c6], refresh=True)
    path = abs_path(latest["json路径"]) if latest else None
    doc = aiplan.read_json(path) if path else None
    exec_doc, exec_p = load_exec(path) if path else (None, None)
    brief = quote_brief(listed, quote_map.get(c6), doc)
    levels = report_levels(path) if path else None
    prev = reference_prev(c6, apply["适用交易日"])
    plan = ((doc or {}).get("研判") or {}).get("json") or {}
    latest_block = None
    if latest:
        latest_block = {"json路径": latest["json路径"], "md路径": latest.get("md路径"),
                        "生成时间": latest.get("时间"), "数据交易日": latest.get("数据交易日"),
                        "适用交易日": latest.get("适用交易日"),
                        "方向": plan.get("方向"), "置信度": plan.get("置信度"),
                        "一句话结论": plan.get("一句话结论"),
                        "时间窗": plan.get("时间窗"), "情景树": plan.get("情景树") or [],
                        "风险": plan.get("风险") or [], "数据依赖": plan.get("数据依赖"),
                        "仓位": plan.get("仓位"), "计划校正": plan.get("计划校正"),
                        "计划": plan_items(doc), "关键价位": levels,
                        "手数": _lots(c6, levels), "降级": (doc or {}).get("降级") or []}
    return {"代码": c6, "名称": (listed or {}).get("名称") or (latest or {}).get("名称") or c6,
            "备注": (listed or {}).get("备注") or "", "在清单": bool(listed),
            "时段": apply["时段"], "适用交易日": apply["适用交易日"],
            "交易日口径": apply["口径"], "现价": brief, "最新": latest_block,
            "执行记录": exec_doc, "执行摘要": exec_summary(exec_doc),
            "执行路径": rel(exec_p) if path else None,
            "机械参考": mech_reference(doc, quote_map.get(c6)) if doc else None,
            "参考": {"来源": prev.get("来源"), "路径": prev.get("路径"),
                     "适用交易日": prev.get("适用交易日"), "说明": prev.get("说明"),
                     "需要执行记录": prev.get("需要执行记录"),
                     "执行摘要": prev.get("执行摘要")},
            "需录入执行记录": bool(prev.get("需要执行记录")),
            "待生成": (not latest) or (latest.get("适用交易日") != apply["适用交易日"]),
            "历史": _history_rows(plans), "提示": hints}, None
