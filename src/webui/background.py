# -*- coding: utf-8 -*-
"""背景数据：把 stock3d 的个股量价形态与 pan 的大盘/板块裁剪成紧凑结构。

报告实盘复核要判「放量滞涨 / 缩量企稳 / 板块资金转净流出」这类文字条件，
判断交给模型，数据由这里提供：只做裁剪与转存，不新增任何计算口径。
"""

from .paths import aiplan
from .sources import newest_file, pan_files, stock3d_snapshot


# ---------------------------------------------------------------------------
# 背景数据：个股量价形态（stock3d）+ 大盘与板块（pan）
#
# 目的是让模型自己把「放量滞涨 / 缩量企稳 / 板块资金转净流出」这类文字条件
# 用现成数值判出来，而不是丢回给人工确认。
# ---------------------------------------------------------------------------

ANALYSIS_KEYS = ("date", "last", "ma5", "ma10", "ma20", "ma30", "ma60",
                 "hi20", "lo20", "hi60", "lo60", "pos60", "vol_ratio",
                 "macd_dif", "macd_dea", "macd_hist")
LEVELS_KEYS = ("数据起点", "数据终点", "数据根数", "区间最高", "区间最低",
               "近1年最高", "近1年最低", "近60日平台高", "近60日平台低",
               "近20日高", "近20日低", "MA120", "MA250", "换手率_pct", "振幅_pct")
BREADTH_KEYS = ("样本数", "上涨家数", "下跌家数", "平盘家数", "上涨占比_pct",
                "涨幅超5%家数", "跌幅超5%家数", "涨停家数_阈值口径", "跌停家数_阈值口径",
                "两市成交额_亿", "主力净流入_亿", "平均涨幅_pct", "中位数涨幅_pct")


def _pick(node, keys):
    if not isinstance(node, dict):
        return {}
    return {k: node[k] for k in keys if node.get(k) is not None}


def market_context(tech=None, pan_doc=None, name=None, pan_rel=None):
    """把 stock3d 的个股量价与 pan 的大盘/板块压成一段紧凑背景数据（纯函数，便于单测）。"""
    tech = tech or {}
    pan_doc = pan_doc or {}
    stock = {}
    for label, key, keys in (
            ("均线与分位", "analysis", ANALYSIS_KEYS),
            ("区间与长期均线", "levels", LEVELS_KEYS)):
        picked = _pick(tech.get(key), keys)
        if picked:
            stock[label] = picked
    for label, key in (("量价（近5/10/20日）", "volume_price"), ("换手率", "turnover"),
                       ("量价背离", "divergence"), ("缺口", "gaps"),
                       ("均线交叉状态", "cross_state"), ("涨跌停与连板", "limit_board"),
                       ("集合竞价", "auction"), ("周线月线", "weekly"), ("技术标签", "tags")):
        v = tech.get(key)
        if v:
            stock[label] = v

    pdata = pan_doc.get("data") or {}
    breadth = ((pdata.get("sentiment") or {}).get("广度")) or {}
    market = _pick(breadth, BREADTH_KEYS)
    funds = pdata.get("funds") or {}
    south = _pick(funds.get("南向资金") or {}, ("日期", "南向合计_净买入_亿"))
    if south:
        market["南向资金净买入_亿"] = south.get("南向合计_净买入_亿")
        market["南向资金日期"] = south.get("日期")
    nxt = pdata.get("next_day") or {}
    if nxt:
        market["次日"] = nxt.get("次日")
        unlock = nxt.get("限售解禁") or {}
        if unlock:
            market["次日解禁总市值_亿"] = unlock.get("解禁总市值_亿")
    if pan_doc:
        market["pan快照"] = {"路径": pan_rel, "phase": pan_doc.get("phase"),
                             "交易日": pan_doc.get("trade_date"),
                             "是否交易日": pan_doc.get("is_trading_day")}

    sectors = pdata.get("sectors") or {}

    def rows(items, n=6):
        out = []
        for r in (items or [])[:n]:
            if not isinstance(r, dict):
                continue
            up, down = r.get("板块内上涨家数"), r.get("板块内下跌家数")
            out.append({"名称": r.get("名称"), "涨跌幅_pct": r.get("涨跌幅_pct"),
                        "主力净流入_亿": r.get("主力净流入_亿"),
                        "板块内涨跌家数": ("%s涨/%s跌" % (up, down)) if up is not None or down is not None else None,
                        "领涨股": r.get("领涨股")})
        return out

    blocks = {"行业领涨": rows((sectors.get("行业板块") or {}).get("领涨")),
              "行业领跌": rows((sectors.get("行业板块") or {}).get("领跌")),
              "概念领涨": rows((sectors.get("概念板块") or {}).get("领涨")),
              "概念领跌": rows((sectors.get("概念板块") or {}).get("领跌"))}
    blocks = {k: v for k, v in blocks.items() if v}
    hit = []
    if name:
        for label, items in (("行业", (sectors.get("行业板块") or {}).get("领涨")),
                             ("概念", (sectors.get("概念板块") or {}).get("领涨"))):
            for r in (items or []):
                if isinstance(r, dict) and r.get("领涨股") == name:
                    hit.append({"类型": label, "板块": r.get("名称"),
                                "涨跌幅_pct": r.get("涨跌幅_pct"),
                                "主力净流入_亿": r.get("主力净流入_亿")})
    if hit:
        blocks["本股领涨的板块"] = hit
    return {"个股量价与形态": stock, "大盘": market, "板块": blocks}


def stock3d_tech(code):
    """从最新 stock3d 快照里取该标的的 tech 段（没有就返回 {}）。"""
    c6 = aiplan.code6(code)
    if not c6:
        return {}, None
    path, doc, _codes = stock3d_snapshot()
    for sym in (doc or {}).get("symbols") or []:
        if aiplan.code6(sym.get("code")) == c6:
            return (sym.get("tech") or {}), path
    return {}, path


def latest_pan():
    path = newest_file(pan_files())
    return (aiplan.read_json(path) or {}), path
