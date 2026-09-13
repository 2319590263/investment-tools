# -*- coding: utf-8 -*-
"""交易流的页面数据：报价 → 盈亏 → 到价提醒（一次批量报价覆盖所有在跑的流）。

机械检查（check_pass）是总控台与交易流页共用的入口：它也会写消息队列，所以两个页面轮询
同一时刻只提醒一次（去重状态存在流 JSON 里）。这里不调模型。
"""

from . import alerts as alerts_store
from . import flow
from .paths import aiplan, num, rel
from .plancheck import session_of
from .track import quote_brief

CONSOLE_ROWS = 8                # 总控台最多展示多少条流
CLOSED_ROWS = 20                # 页面「已结束」区最多展示多少条


def _price(doc, quote, refresh):
    """现价与来源：东财实时 → 流内最近成交价。"""
    brief = quote_brief(None, quote, None)
    if num(brief.get("价格")) is not None:
        return brief.get("价格"), brief.get("来源"), brief.get("时间")
    last = None
    for f in reversed(doc.get("成交") or []):
        last = num(f.get("价格"))
        if last is not None:
            break
    if last is not None:
        return last, "最近成交价（本次没取到实时价）", None
    return None, "未取到", None


def card(doc, fresh=None):
    """一条流的卡片数据（纯展示，不改文件）。"""
    pnl = doc.get("盈亏") or {}
    plan = doc.get("计划") or {}
    checks = doc.get("体检") or []
    return {
        "流编号": doc.get("流编号"), "标的": doc.get("标的"), "状态": doc.get("状态"),
        "创建时间": doc.get("创建时间"), "起始日": doc.get("起始日"),
        "结束时间": doc.get("结束时间"), "结束原因": doc.get("结束原因"),
        "参数": doc.get("参数"), "持仓": doc.get("持仓"), "盈亏": pnl,
        "现价": pnl.get("现价"), "价格来源": pnl.get("价格来源"),
        "价格时间": pnl.get("价格时间"),
        "计划": {"产物路径": plan.get("产物路径"), "md路径": plan.get("md路径"),
                 "生成时间": plan.get("生成时间"), "适用交易日": plan.get("适用交易日"),
                 "方向": plan.get("方向"), "置信度": plan.get("置信度"),
                 "一句话结论": plan.get("一句话结论"), "错误": plan.get("错误"),
                 "关键价位": plan.get("关键价位") or {}, "条目": plan.get("条目") or []},
        "下一步": flow.next_action(doc),
        "到价": flow.marks(doc, pnl.get("现价")),
        "本次提醒": [m.get("文案") for m in (fresh or [])],
        "待重算": flow.plan_stale(doc),
        "最近体检": (checks[-1] if checks else None),
        "体检次数": len(checks),
        "成交笔数": len(doc.get("成交") or []),
        "事件数": len(doc.get("事件") or []),
        "台账": doc.get("台账") or {}, "提示": doc.get("提示") or [],
        "期初": doc.get("期初") or {},
    }


def check_pass(active, quote_map=None, refresh=False, today=None):
    """把报价套到每条流上：重算盈亏 → 生成并去重提醒 → 写消息队列 → 有变化就落盘。

    同一（类型, 价位）当天只提醒一次；价格回落后再次进入会重新提醒（与总控台同一语义）。
    """
    cards, fresh_all, dirty = [], [], []
    for doc in active or []:
        code = aiplan.code6((doc.get("标的") or {}).get("代码") or "")
        price, source, when = _price(doc, (quote_map or {}).get(code), refresh)
        before = dict(doc.get("盈亏") or {})
        if price is not None:
            flow.apply_price(doc, price, source, when, today=today)
        fresh = flow.due_alerts(doc, price, today=today)
        if fresh:
            flow.mark_alerted(doc, fresh, today=today)
            for m in fresh:
                flow.events_append(doc, "到价", m.get("文案") or "", m.get("级别") or "info",
                                   price=m.get("价位"))
            fresh_all.extend(flow.alert_items(doc, fresh))
        if (doc.get("盈亏") or {}) != before or fresh:
            dirty.append(doc)
        cards.append(card(doc, fresh))
    if fresh_all:
        alerts_store.append(fresh_all)
    for doc in dirty:
        flow.save_flow(doc)
    return cards, fresh_all


def overview(quote_map=None, refresh=False, with_closed=True):
    """交易流页 / 总控台共用的数据组装。quote_map 由调用方用一次批量报价取好。"""
    active = flow.list_flows(active_only=True)
    cards, fresh = check_pass(active, quote_map, refresh=refresh) if active else ([], [])
    out = {"流": cards, "设置": flow.settings(), "时段": session_of(),
           "资金": {"已占用": flow.active_capital()},
           "待重算": [c["流编号"] for c in cards if (c.get("待重算") or {}).get("待重算")],
           "提醒": fresh, "目录": rel(flow.FLOW_DIR),
           "计数": {"进行中": len(cards)}}
    if with_closed:
        closed = [d for d in flow.list_flows() if d.get("状态") != flow.ACTIVE_STATE]
        out["已结束"] = [card(d) for d in closed[:CLOSED_ROWS]]
    return out


def detail(fid, quote_map=None, refresh=False, today=None):
    """单条流详情：卡片 + 成交 + 事件 + 体检 + 计划（可选先刷新一次报价）。"""
    doc, err = flow.load_flow(fid)
    if err:
        return None, err
    if quote_map is not None:
        check_pass([doc], quote_map, refresh=refresh, today=today)
    code = aiplan.code6((doc.get("标的") or {}).get("代码") or "")
    plan = doc.get("计划") or {}
    return {"卡": card(doc), "流": doc, "成交": doc.get("成交") or [],
            "事件": list(reversed(doc.get("事件") or []))[:80],
            "体检": doc.get("体检") or [],
            "计划": plan, "计划条目": plan.get("条目") or [],
            "关键价位": plan.get("关键价位") or {},
            "持仓": doc.get("持仓") or {}, "盈亏": doc.get("盈亏") or {},
            "台账": doc.get("台账") or {}, "设置": flow.settings(),
            "报价": (quote_map or {}).get(code),
            "手数": lots_of(code, plan.get("关键价位") or {})}, None


def lots_of(code, levels):
    """买点 / 减仓 / 止损 / 止盈的手数文案（与卡片、报告页同一口径）。"""
    from .planlines import lots_text
    from .store import load_holdings_bundle
    hold = {}
    try:
        for row in (load_holdings_bundle().get("持仓") or []):
            if row.get("代码") == aiplan.code6(code or ""):
                hold = row
                break
    except Exception:            # noqa: BLE001  持仓文件坏了不该拖垮详情页
        hold = {}
    buy = (levels or {}).get("买点") or {}
    sell = (levels or {}).get("减仓") or {}
    return {"买点": lots_text(buy.get("股数")), "减仓": lots_text(sell.get("股数")),
            "止损": lots_text(hold.get("可用股数_可卖")),
            "止盈": lots_text(hold.get("持有股数")),
            "持有股数": hold.get("持有股数"), "可用股数_可卖": hold.get("可用股数_可卖"),
            "是否持仓": bool(hold)}


def console_block(quote_map=None, refresh=False):
    """总控台用的紧凑卡区（最多 CONSOLE_ROWS 条 + 计数 + 待重算数）。"""
    data = overview(quote_map, refresh=refresh, with_closed=False)
    rows = data["流"][:CONSOLE_ROWS]
    return {"行": rows, "计数": {"进行中": len(data["流"]), "显示": len(rows)},
            "待重算": data["待重算"], "设置": data["设置"], "时段": data["时段"],
            "合计资金": data["资金"]["已占用"],
            "提示": ["一次东财批量报价覆盖所有在跑的流（不调模型）"] if refresh else [],
            "口径": "接近带 %s%% ｜ 轮询 %s 秒（在「交易流」页可调）"
                    % (data["设置"].get("接近带_pct"), data["设置"].get("轮询间隔_秒"))}
