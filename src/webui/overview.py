# -*- coding: utf-8 -*-
"""总控台数据：持仓 / 自选卡片 + 计划线 + 到价提醒（全程只读，不写盘）。

- 价格默认用本地口径（持仓文件的「市价」/ 最新 stock3d 缓存）；refresh=True 时用
  **一次** 东财批量报价覆盖，失败则保留本地价并在提示里写明。
- 计划线取该股最新一份报告里的「关键价位 + 计划」，按报告 mtime 缓存，不重复解析。
- 触发判定是「现价 vs 计划线」的状态比较；同一标记只在「未触发 → 已触发」时提醒一次，
  价格回到未触发后再触发会重新提醒。
"""

import copy
import os
import time

from . import alerts as alerts_store
from . import flow
from . import flowview
from . import pickrank
from . import quotes
from . import track
from .archive import list_reports
from .paths import ROOT, WATCHLIST_PATH, aiplan, num, rel
from .plancheck import is_noop, parse_range, plan_kind, session_of
from .planlines import _LEVELS_CACHE, lots_text, report_levels
from .store import load_holdings_bundle, load_watchlist

TOL = 0.01                 # 与实盘复核一致的 1 个最小变动价位
ALERT_MAX = 8              # 提醒条一次最多给几条
REPORT_MAP_TTL = 60.0      # 「每只股票的最新报告」缓存

_REPORT_MAP = {"ts": 0.0, "map": {}}
_ALERT_STATE = {}          # (代码, 类型) -> 已提醒过的价位
_STATE_SEEDED = {"done": False}


def _f(v):
    return "—" if v is None else "%.2f" % v


# ---------------------------------------------------------------------------
# 计划线
# ---------------------------------------------------------------------------

def newest_report_map(reports=None):
    """{6 位代码: 最新报告条目}；list_reports 已按时间倒序，按 TTL 缓存避免每档都扫报告。"""
    now = time.time()
    if reports is None and _REPORT_MAP["map"] and now - _REPORT_MAP["ts"] <= REPORT_MAP_TTL:
        return _REPORT_MAP["map"]
    out = {}
    for item in (reports if reports is not None else list_reports()):
        code = aiplan.code6(((item.get("摘要") or {}).get("标的代码")) or "")
        if code and code not in out:
            out[code] = item
    if reports is None:
        _REPORT_MAP["ts"] = now
        _REPORT_MAP["map"] = out
    return out


# ---------------------------------------------------------------------------
# 触发判定
# ---------------------------------------------------------------------------

def trigger_marks(price, levels):
    """现价 vs 计划线的状态标记（纯函数）。提醒=True 的才进提醒条。"""
    marks = []
    if price is None or not levels:
        return marks
    buy = levels.get("买点")
    if buy and buy.get("下沿") is not None:
        lo, hi = buy["下沿"], buy["上沿"]
        if lo - TOL <= price <= hi + TOL:
            marks.append({"类型": "已到买点", "价位": lo, "级别": "buy", "提醒": True,
                          "文案": "%s %s ~ %s（止损 %s）"
                                  % (buy.get("动作") or "买入", _f(lo), _f(hi), _f(levels.get("止损")))})
        elif price > hi + TOL:
            marks.append({"类型": "高于买点", "价位": hi, "级别": "mute", "提醒": False,
                          "文案": "现价高于买点区间 %.2f%%，不追高" % ((price - hi) / hi * 100)})
    sell = levels.get("减仓")
    if sell and sell.get("下沿") is not None and sell["下沿"] - TOL <= price <= sell["上沿"] + TOL:
        marks.append({"类型": "已到减仓位", "价位": sell["上沿"], "级别": "sell", "提醒": True,
                      "文案": "%s %s ~ %s" % (sell.get("动作") or "减仓", _f(sell["下沿"]), _f(sell["上沿"]))})
    stop = levels.get("止损")
    if stop is not None and price <= stop + TOL:
        marks.append({"类型": "已破止损", "价位": stop, "级别": "stop", "提醒": True,
                      "文案": "跌破 %s 按计划止损离场" % _f(stop)})
    goals = sorted([g for g in (levels.get("目标") or []) if g.get("价位") is not None],
                   key=lambda g: g["价位"])
    for goal in goals:
        if price >= goal["价位"] - TOL:
            marks.append({"类型": "已达目标", "价位": goal["价位"], "级别": "target", "提醒": True,
                          "文案": "已到目标位 %s%s" % (_f(goal["价位"]),
                                                   ("（%s）" % goal["依据"]) if goal.get("依据") else "")})
            break
    support = sorted([s["价位"] for s in (levels.get("支撑") or []) if s.get("价位") is not None])
    if support and price < support[0] - TOL:
        marks.append({"类型": "已跌破支撑", "价位": support[0], "级别": "stop", "提醒": False,
                      "文案": "跌破支撑 %s" % _f(support[0])})
    pressure = sorted([s["价位"] for s in (levels.get("压力") or []) if s.get("价位") is not None])
    if pressure and price > pressure[-1] + TOL:
        marks.append({"类型": "已上破压力", "价位": pressure[-1], "级别": "up", "提醒": False,
                      "文案": "上破压力 %s" % _f(pressure[-1])})
    return marks


def _alerts_for(code, name, marks, levels, lots=None):
    """把「刚从不触发变成触发」的标记变成提醒；同一标记不重复提醒。"""
    out = []
    for mark in marks:
        if not mark.get("提醒"):
            continue
        key = (code, mark["类型"])
        price = mark.get("价位") or 0
        prev = _ALERT_STATE.get(key)
        if prev is not None and abs(prev - price) < 1e-9:
            continue
        _ALERT_STATE[key] = price
        out.append({"代码": code, "名称": name, "类型": mark["类型"], "价位": mark.get("价位"),
                    "手数": (lots or {}).get(mark["类型"], ""),
                    "文案": mark.get("文案"), "时间": time.strftime("%H:%M:%S"),
                    "ts": round(time.time(), 3),
                    "报告路径": (levels or {}).get("报告路径")})
    return out


def _prune_alert_state(code, marks):
    """已经不成立的标记要从状态里删掉，这样价格回来再触发时能重新提醒。"""
    live = {mark["类型"] for mark in marks if mark.get("提醒")}
    for key in [k for k in list(_ALERT_STATE) if k[0] == code and k[1] not in live]:
        _ALERT_STATE.pop(key, None)


def reset_alert_state():
    """测试与调试用：清空提醒去重状态。"""
    _ALERT_STATE.clear()
    _STATE_SEEDED["done"] = False


def _seed_alert_state():
    """用队列里已有的消息播种去重状态：服务重启后不会把同一条提醒再发一遍。

    只看「代码 + 类型 + 价位」——价格变了就是新情况，照常提醒。
    """
    if _STATE_SEEDED["done"]:
        return
    _STATE_SEEDED["done"] = True
    try:
        for row in alerts_store.list_alerts(alerts_store.MAX_LIMIT):
            code, kind = row.get("代码"), row.get("类型")
            if code and kind:
                _ALERT_STATE.setdefault((code, kind), row.get("价位") or 0)
    except Exception:              # noqa: BLE001  队列读不到不影响判定
        pass


# ---------------------------------------------------------------------------
# 卡片
# ---------------------------------------------------------------------------

def _price_of(row, quote, kind):
    """返回 (现价, 涨跌幅, 来源, 时间)。实时优先，其次本地口径。"""
    if quote and quote.get("价格") is not None:
        return (num(quote.get("价格")), num(quote.get("涨跌幅_pct")),
                "东财实时行情", time.strftime("%H:%M:%S"), num(quote.get("昨收")))
    price = num(row.get("现价"))
    chg = num(row.get("涨跌幅_pct"))
    if kind == "hold":
        src = "持仓文件市价"
    else:
        src = "stock3d 缓存" if row.get("在缓存") else "未抓数"
    return price, chg, src, None, None


def build_overview(refresh=False):
    """总控台数据。refresh=True 时用一次批量报价 + 每标的最多一次分时。"""
    _seed_alert_state()
    started = time.time()
    hold = load_holdings_bundle()
    watch = load_watchlist() or []
    hold_rows = hold.get("持仓") or []
    hints = []
    rank = pickrank.build_rank({aiplan.code6(r.get("代码")) for r in hold_rows},
                               {aiplan.code6(w.get("代码")) for w in watch})
    active_flows = flow.list_flows(active_only=True)
    flow_codes = [aiplan.code6((d.get("标的") or {}).get("代码") or "") for d in active_flows]
    codes = ([r.get("代码") for r in hold_rows] + [w.get("代码") for w in watch] +
             [row.get("代码") for row in rank.get("行") or []] + flow_codes)
    quote_map = {}
    if refresh:
        quote_map, qhints = quotes.fetch_quotes(codes, refresh=True)
        hints += qhints
    pickrank.apply_quotes(rank.get("行") or [], quote_map, refresh)
    # 计划线取「报告 ∪ 跟踪产物」里每只标的最新一份：日常用标的跟踪生成计划时，
    # 总控台卡片与到价提醒要跟着跟踪计划走，而不是停在旧的手工报告上。
    reports = list_reports() + track.merged_plan_items()
    reports.sort(key=lambda x: x.get("mtime") or 0, reverse=True)
    by_code = newest_report_map(reports)
    held = {r.get("代码") for r in hold_rows}
    session = session_of()
    # 交易流：同一次批量报价，做机械判定（盈亏 / 接近带 / 达标止损）并写消息队列；不调模型。
    flows_block = flowview.console_block(quote_map, refresh=refresh) if active_flows else {
        "行": [], "计数": {"进行中": 0, "显示": 0}, "待重算": [], "设置": flow.settings(),
        "时段": session, "合计资金": 0.0, "提示": [], "口径": ""}
    alerts = []

    def shared(row, kind):
        code = row.get("代码")
        quote = quote_map.get(code)
        price, chg, src, when, prev_close = _price_of(row, quote, kind)
        item = by_code.get(code)
        levels = report_levels(item.get("json路径")) if item else None
        buy = (levels or {}).get("买点") or {}
        sell = (levels or {}).get("减仓") or {}
        lots = {"已到买点": lots_text(buy.get("股数")),
                "已到减仓位": lots_text(sell.get("股数")),
                "已破止损": lots_text(row.get("可用股数_可卖")) if kind == "hold" else "",
                "已达目标": lots_text(row.get("持有股数")) if kind == "hold" else ""}
        if levels:
            levels = dict(levels)
            levels["手数"] = {"买点": lots["已到买点"], "减仓": lots["已到减仓位"],
                              "止损": lots["已破止损"], "止盈": lots["已达目标"]}
        marks = trigger_marks(price, levels)
        if marks:
            fresh = _alerts_for(code, row.get("名称"), marks, levels, lots)
            if fresh:
                alerts.extend(fresh)
                alerts_store.append(fresh)      # 落盘：页面关掉也不丢
            _prune_alert_state(code, marks)
        minute = quotes.fetch_minutes(code) if (refresh and price is not None) else None
        return {"代码": code, "名称": row.get("名称"), "现价": price, "涨跌幅_pct": chg,
                "价格来源": src, "价格时间": when, "昨收": prev_close,
                "报告路径": (item or {}).get("json路径"), "计划": levels,
                "触发": marks, "分时": minute,
                "报告时间": (item or {}).get("时间")}

    hold_cards = []
    for row in hold_rows:
        card = shared(row, "hold")
        card.update({"是否ETF": row.get("是否ETF"), "成本价": row.get("成本价"),
                     "持有股数": row.get("持有股数"), "可用股数_可卖": row.get("可用股数_可卖"),
                     "冻结股数_当日买入不可卖": row.get("冻结股数_当日买入不可卖"),
                     "持仓市值_元": row.get("持仓市值_元"), "浮动盈亏_元": row.get("浮动盈亏_元"),
                     "盈亏比例_pct": row.get("盈亏比例_pct"), "占总资金_pct": row.get("占总资金_pct"),
                     "持股天数": row.get("持股天数")})
        hold_cards.append(card)
    watch_cards = []
    for row in watch:
        card = shared(row, "watch")
        card.update({"备注": row.get("备注"), "已持仓": row.get("代码") in held,
                     "在缓存": row.get("在缓存")})
        watch_cards.append(card)

    if not refresh:
        hints.append("本次用本地口径价格（持仓文件「市价」/ stock3d 缓存）："
                     "进入总控台会自动刷一次实时行情，也可点「刷新实盘价」手动再刷")
    return {
        "时段": session,
        "刷新": {"模式": "实时" if refresh else "本地", "时间": time.strftime("%H:%M:%S"),
                 "来源": "东财批量报价 + 腾讯分时" if refresh else "本地缓存",
                 "报价条数": len(quote_map), "耗时_s": round(time.time() - started, 2),
                 "提示": hints},
        "汇总": hold.get("汇总") or {},
        "持仓": hold_cards, "自选": watch_cards,
        "荐股": rank, "flows": flows_block,
        "提醒": alerts[:ALERT_MAX],
        "路径": {"持仓": hold.get("路径"), "自选": rel(WATCHLIST_PATH)},
    }
