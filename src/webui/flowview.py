# -*- coding: utf-8 -*-
"""交易流的页面数据：报价 → 每只标的的盈亏 → 到价提醒（一次批量报价覆盖所有在跑的标的）。

机械检查（check_pass）是总控台与交易流页共用的入口：它也会写消息队列，所以两个页面轮询
同一时刻只提醒一次（去重状态存在流 JSON 里）。这里不调模型。
"""

from . import alerts as alerts_store
from . import flow
from . import track
from .paths import aiplan, num, rel
from .plancheck import session_of
from .track import quote_brief
from .flowbook import resolve_name

CONSOLE_ROWS = 12               # 总控台最多展示多少行（一行 = 一只标的）
CLOSED_ROWS = 20                # 页面「已结束」区最多展示多少条


def _price(node, quote, refresh):
    """现价与来源：东财实时 → 流内最近成交价。"""
    brief = quote_brief(None, quote, None)
    if num(brief.get("价格")) is not None:
        return brief.get("价格"), brief.get("来源"), brief.get("时间")
    last = None
    for f in reversed(node.get("成交") or []):
        last = num(f.get("价格"))
        if last is not None:
            break
    if last is not None:
        return last, "最近成交价（本次没取到实时价）", None
    return None, "未取到", None


def _plan_rows(node):
    """这只标的的计划条目：直接读计划产物（流里存的只是快照，不吃硬约束的作废/改股数）。

    读不到产物（还没算过 / 文件被删）就返回 None，调用方退回快照。
    """
    path = ((node.get("计划") or {}).get("产物路径"))
    if not path:
        return None
    try:
        payload = aiplan.read_json(track.abs_path(path))
    except Exception:              # noqa: BLE001  产物坏了不该拖垮详情页
        return None
    rows = track.plan_items(payload) if payload else None
    return rows or None


def target_card(flow_doc, node, fresh=None, quote_map=None):
    """一只标的的卡片数据（纯展示，不改文件）。名称按「行情 → 名称簿 → 存的 → 代码」解析。"""
    pnl = node.get("盈亏") or {}
    plan = node.get("计划") or {}
    checks = node.get("体检") or []
    return {
        "流编号": flow_doc.get("流编号"), "代码": node.get("代码"),
        "名称": resolve_name(node, quote_map),
        "存的名字": node.get("名称"),
        "是否ETF": node.get("是否ETF"), "打法": node.get("打法") or flow.DEFAULT_STYLE,
        "分配资金": node.get("分配资金"), "参数": node.get("参数"),
        "状态": node.get("状态"), "加入日": node.get("加入日"), "加入时间": node.get("加入时间"),
        "结束时间": node.get("结束时间"), "结束原因": node.get("结束原因"),
        "持仓": node.get("持仓"), "盈亏": pnl,
        "现价": pnl.get("现价"), "价格来源": pnl.get("价格来源"),
        "价格时间": pnl.get("价格时间"),
        "计划": {"产物路径": plan.get("产物路径"), "md路径": plan.get("md路径"),
                 "生成时间": plan.get("生成时间"), "适用交易日": plan.get("适用交易日"),
                 "打法": plan.get("打法") or node.get("打法"),
                 "方向": plan.get("方向"), "置信度": plan.get("置信度"),
                 "一句话结论": plan.get("一句话结论"), "错误": plan.get("错误"),
                 "关键价位": plan.get("关键价位") or {}, "条目": plan.get("条目") or []},
        # 机械打分（量化底座）+ 硬约束 + 校正记录：与计划产物同一份
        "机械打分": plan.get("机械打分"), "硬约束": plan.get("硬约束"),
        "约束校正": plan.get("约束校正") or [], "约束提示": plan.get("约束提示") or [],
        "下一步": flow.next_action(node, price=pnl.get("现价")),
        "到价": flow.marks(node, pnl.get("现价")),
        "本次提醒": [m.get("文案") for m in (fresh or [])],
        "待重算": flow.plan_stale(node),
        "最近体检": (checks[-1] if checks else None),
        "体检次数": len(checks),
        "成交笔数": len(node.get("成交") or []),
        "事件数": len(node.get("事件") or []),
        "台账": node.get("台账") or {}, "提示": node.get("提示") or [],
        "期初": node.get("期初") or {},
    }


def flow_card(doc, fresh_map=None, quote_map=None):
    """一条流的卡片：流参数 + 汇总 + 每只标的的卡片。"""
    params = doc.get("参数") or {}
    return {
        "流编号": doc.get("流编号"), "状态": doc.get("状态"),
        "创建时间": doc.get("创建时间"), "起始日": doc.get("起始日"),
        "结束时间": doc.get("结束时间"), "结束原因": doc.get("结束原因"),
        "参数": params,
        "汇总": doc.get("汇总") or flow.aggregate(doc),
        "标的": [target_card(doc, n, (fresh_map or {}).get(aiplan.code6(n.get("代码") or "")),
                             quote_map=quote_map)
                 for n in flow.targets(doc)],
        "事件": list(reversed(doc.get("事件") or []))[:20],
    }


def check_pass(active, quote_map=None, refresh=False, today=None):
    """把报价套到每只标的上：重算盈亏 → 生成并去重提醒 → 写消息队列 → 有变化就落盘。

    同一（类型, 价位）当天只提醒一次；价格回落后再次进入会重新提醒（与总控台同一语义）。
    """
    cards, fresh_all, dirty = [], [], []
    for doc in active or []:
        fresh_map, changed = {}, False
        for node in flow.targets(doc):
            code = aiplan.code6(node.get("代码") or "")
            price, source, when = _price(node, (quote_map or {}).get(code), refresh)
            before = dict(node.get("盈亏") or {})
            if price is not None:
                flow.apply_price(node, price, source, when, today=today)
            fresh = flow.due_alerts(node, price, today=today)
            if fresh:
                flow.mark_alerted(node, fresh, today=today)
                for m in fresh:
                    flow.events_append(node, "到价", m.get("文案") or "",
                                       m.get("级别") or "info", price=m.get("价位"))
                fresh_all.extend(flow.alert_items(node, fresh, doc.get("流编号")))
                fresh_map[code] = fresh
            if (node.get("盈亏") or {}) != before or fresh:
                changed = True
        if changed:
            flow.aggregate(doc)
            dirty.append(doc)
        cards.append(flow_card(doc, fresh_map, quote_map=quote_map))
    if fresh_all:
        alerts_store.append(fresh_all)
    for doc in dirty:
        flow.save_flow(doc)
    return cards, fresh_all


def overview(quote_map=None, refresh=False, with_closed=True):
    """交易流页 / 总控台共用的数据组装。quote_map 由调用方用一次批量报价取好。"""
    active = flow.list_flows(active_only=True)
    cards, fresh = check_pass(active, quote_map, refresh=refresh)
    pending = []
    for card in cards:
        codes = [t["代码"] for t in card["标的"] if (t.get("待重算") or {}).get("待重算")]
        if codes:
            names = {t["代码"]: t["名称"] for t in card["标的"]}
            pending.append({"流编号": card["流编号"], "代码": codes,
                            "标签": "、".join("%s %s" % (c, names.get(c) or "") for c in codes)})
    used = flow.active_capital()
    alloc = round(sum((c["汇总"] or {}).get("分配合计") or 0.0 for c in cards), 2)
    out = {"流": cards, "设置": flow.settings(), "时段": session_of(),
           "资金": {"已占用": used, "分配合计": alloc},
           "标的数": sum(len(c["标的"]) for c in cards),
           "待重算": pending, "提醒": fresh, "目录": rel(flow.FLOW_DIR),
           "打法口径": flow.STYLE_HINT,
           "计数": {"进行中": len(cards), "标的": sum(len(c["标的"]) for c in cards)}}
    if with_closed:
        closed = [d for d in flow.list_flows() if d.get("状态") != flow.ACTIVE_STATE]
        out["已结束"] = [flow_card(d) for d in closed[:CLOSED_ROWS]]
    return out


def lots_of(node):
    """买点 / 减仓 / 止损 / 止盈的手数文案（与卡片、报告页同一口径）。"""
    from .planlines import lots_text
    from .store import load_holdings_bundle
    hold = {}
    try:
        for row in (load_holdings_bundle().get("持仓") or []):
            if row.get("代码") == aiplan.code6(node.get("代码") or ""):
                hold = row
                break
    except Exception:            # noqa: BLE001  持仓文件坏了不该拖垮详情页
        hold = {}
    levels = (node.get("计划") or {}).get("关键价位") or {}
    buy = (levels or {}).get("买点") or {}
    sell = (levels or {}).get("减仓") or {}
    return {"买点": lots_text(buy.get("股数")), "减仓": lots_text(sell.get("股数")),
            "止损": lots_text(hold.get("可用股数_可卖")),
            "止盈": lots_text(hold.get("持有股数")),
            "持有股数": hold.get("持有股数"), "可用股数_可卖": hold.get("可用股数_可卖"),
            "是否持仓": bool(hold)}


def detail(fid, quote_map=None, refresh=False, today=None, code=None):
    """一条流的详情：流卡片 + 每只标的的成交 / 事件 / 体检 / 计划 / 手数（一次给全）。"""
    doc, err = flow.load_flow(fid)
    if err:
        return None, err
    if quote_map is not None:
        check_pass([doc], quote_map, refresh=refresh, today=today)
    blocks = []
    for node in flow.targets(doc):
        card = target_card(doc, node, quote_map=quote_map)
        from .planlines import meaningful_failure, trigger_price
        live = (node.get("盈亏") or {}).get("现价")
        # 计划条目以**产物**为准（流里存的只是快照）：硬约束做的作废 / 改股数写的是产物
        rows = _plan_rows(node) or (card["计划"].get("条目") or [])
        items = []
        for it in rows:
            row = dict(it)
            row["精确价"] = trigger_price(row.get("价格区间"), row.get("动作"), live)
            row["失效条件"] = meaningful_failure(row.get("失效条件") or row.get("失效条件原文"),
                                                 row.get("价格区间"), row.get("动作"))
            items.append(row)
        card["计划"]["条目"] = items
        plan = card["计划"]
        blocks.append({
            "卡": card, "代码": node.get("代码"), "名称": node.get("名称"),
            "打法": node.get("打法") or flow.DEFAULT_STYLE, "分配资金": node.get("分配资金"),
            "成交": node.get("成交") or [],
            "事件": list(reversed(node.get("事件") or []))[:80],
            "体检": node.get("体检") or [],
            "计划": plan, "计划条目": items,
            "关键价位": plan.get("关键价位") or {},
            # 机械打分（量化底座）+ 硬约束 + 校正记录：与产物同一份（卡片上就有）
            "机械打分": card.get("机械打分"), "硬约束": card.get("硬约束"),
            "约束校正": card.get("约束校正") or [], "约束提示": card.get("约束提示") or [],
            "持仓": node.get("持仓") or {}, "盈亏": node.get("盈亏") or {},
            "台账": node.get("台账") or {}, "期初": node.get("期初") or {},
            "手数": lots_of(node),
            "报价": (quote_map or {}).get(aiplan.code6(node.get("代码") or "")),
        })
    selected = aiplan.code6(code or "") or (blocks[0]["代码"] if blocks else None)
    return {"流": doc, "卡": flow_card(doc, quote_map=quote_map), "标的": blocks, "选中": selected,
            "汇总": doc.get("汇总") or flow.aggregate(doc), "设置": flow.settings(),
            "打法口径": flow.STYLE_HINT,
            "事件": list(reversed(doc.get("事件") or []))[:40]}, None


def minutes(fid, code, refresh=False, mode="minute"):
    """单只标的的行内图：分时（mode=minute）或日K（mode=day）+ 计划操作线（只读，不写盘）。

    分时走腾讯（quotes.fetch_minutes：60 秒缓存 + 300ms 串行限速），日K 走 market.kline_bundle
    （本地缓存优先，没有就按需拉取，当天落 data/cache/mech/kline_<日期>/），报价走一次东财批量
    （5 秒缓存）。两种模式的计划线都用同一份「关键价位」结构，前端用同一份 planprices.js
    收敛成精确价位，避免第二套解析口径。
    """
    from . import quotes as quotes_mod
    from .market import kline_bundle
    doc, err = flow.load_flow(fid)
    if err:
        return None, err
    node, terr = flow.find_target(doc, code)
    if node is None:
        return None, terr
    c6 = aiplan.code6(node.get("代码") or "")
    day = str(mode or "minute").lower() == "day"
    quote, hints = quotes_mod.fetch_quotes([c6], refresh=refresh)
    q = (quote or {}).get(c6) or {}
    hints = list(hints or [])
    bundle, minute = None, None
    if day:
        bundle = kline_bundle(c6, 250)
        if not bundle or not (bundle.get("bars") or []):
            hints.append("日K 取不到（停牌 / 网络）：只显示报价与计划线")
    else:
        minute = quotes_mod.fetch_minutes(c6, refresh=refresh)
        if minute is None:
            hints.append("分时取不到（北交所 / 停牌 / 网络）：只显示报价与计划线")
    fills = []
    for f in (node.get("成交") or []):
        price = num(f.get("价格"))
        if price is None:
            continue
        fills.append({"序号": f.get("序号"), "日期": f.get("日期"), "时间": f.get("时间"),
                      "方向": f.get("方向"), "价格": price, "数量": num(f.get("数量")),
                      "来源": f.get("来源"), "备注": f.get("备注")})
    out = {
        "流编号": fid, "代码": c6, "名称": resolve_name(node, {c6: q}),
        "打法": node.get("打法") or flow.DEFAULT_STYLE, "模式": "day" if day else "minute",
        "分时": minute, "分时日期": str(session_of()["现在"])[:10],
        "报价": q, "昨收": q.get("昨收"),
        "关键价位": ((node.get("计划") or {}).get("关键价位")) or {},
        "成交": fills, "提示": hints,
        "口径": "分时=腾讯当日分钟线（60 秒缓存）｜报价=东财批量（5 秒缓存）"
                "｜买卖线=交易计划的操作价位（买点/减仓/止损/止盈点）",
    }
    if day:
        out["日K"] = {"bars": (bundle or {}).get("bars") or [],
                      "来源": (bundle or {}).get("来源"),
                      "复权": (bundle or {}).get("复权") or "前复权",
                      "根数": len((bundle or {}).get("bars") or []),
                      "抓取时间": (bundle or {}).get("抓取时间")}
        out["口径"] = "日K=东财/腾讯前复权日线（当天缓存）｜报价=东财批量（5 秒缓存）" \
                      "｜买卖线=交易计划的操作价位（买点/减仓/止损/止盈点）"
    return out, None


def console_block(quote_map=None, refresh=False):
    """总控台用的紧凑卡区（一行 = 一只标的，最多 CONSOLE_ROWS 行）。"""
    data = overview(quote_map, refresh=refresh, with_closed=False)
    data = overview(quote_map, refresh=refresh, with_closed=False)
    rows = []
    for card in data["流"]:
        for t in card["标的"]:
            rows.append({"流编号": card["流编号"], "流状态": card["状态"],
                         "标的": {"代码": t["代码"], "名称": t["名称"]},
                         "打法": t["打法"], "状态": t["状态"], "分配资金": t["分配资金"],
                         "盈亏": t["盈亏"], "现价": t["现价"], "下一步": t["下一步"],
                         "到价": t["到价"], "待重算": t["待重算"]})
    shown = rows[:CONSOLE_ROWS]
    return {"行": shown, "计数": {"进行中": len(data["流"]), "标的": len(rows),
                             "显示": len(shown)},
            "待重算": data["待重算"], "设置": data["设置"], "时段": data["时段"],
            "合计资金": data["资金"]["已占用"], "分配合计": data["资金"]["分配合计"],
            "提示": ["一次东财批量报价覆盖所有在跑的标的（不调模型）"] if refresh else [],
            "口径": "接近带 %s%% ｜ 轮询 %s 秒（在「交易流」页可调）"
                    % (data["设置"].get("接近带_pct"), data["设置"].get("轮询间隔_秒"))}
