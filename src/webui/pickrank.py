# -*- coding: utf-8 -*-
"""总控台荐股榜：最新一份产物 → 「模型评分降序 + 打法筛选」的行表。

兼容两种产物：
  · 新版（全大盘）：读 推荐榜[]（模型评分 + 打法 + 理由），机械分只作辅助列；
  · 旧版（板块筛选版）：读 候选{模块: [...]}，推荐度回退机械分，打法显示「未定」。
行情不在这里取：overview 把榜单代码并进同一次东财批量请求，取不到就留产物快照价。
"""

import os
import time

from .archive import latest_pick_path
from .paths import aiplan, num, rel
from .pick import PICK_STYLES, pick_style_of

STALE_DAYS = 3
FACET_OTHER = "未定"
RANK_NOTE = ("排名看模型评分（0-100，模型给的推荐分）；机械分是按《机器打分逻辑.txt》"
             "算的辅助参考，不参与排名")


def build_rows(doc, held=None, watch=None):
    """产物 → 榜单行（模型分降序）。"""
    held, watch = set(held or ()), set(watch or ())
    rows = []
    rank = doc.get("推荐榜") or []
    if rank:
        for item in rank:
            code = aiplan.code6(str(item.get("代码") or ""))
            if code:
                rows.append(_new_row(item, code, held, watch))
    else:
        for _module, items in (doc.get("候选") or {}).items():
            for it in items or []:
                code = aiplan.code6(str(it.get("代码") or ""))
                if code:
                    rows.append(_old_row(it, code, held, watch))
    rows.sort(key=sort_key)
    return rows


def _new_row(item, code, held, watch):
    score = num(item.get("评分"))
    raw_style = str(item.get("打法") or "").strip()
    if not raw_style or raw_style == FACET_OTHER or score is None:
        style = FACET_OTHER
    else:
        style = pick_style_of(raw_style)
    return {
        "代码": code, "名称": item.get("名称"), "打法": style,
        "推荐度": score, "模型分": score, "机械分": num(item.get("机械分")),
        "评级": item.get("评级"), "理由": item.get("理由"),
        "所属板块": item.get("所属板块") or item.get("行业"), "行业": item.get("行业"),
        "排名": num(item.get("排名")),
        "产物现价": num(item.get("现价")), "现价": num(item.get("现价")),
        "涨跌幅_pct": num(item.get("涨跌幅_pct")),
        "换手率_pct": num(item.get("换手率_pct")), "量比": num(item.get("量比")),
        "主力净流入_万": num(item.get("主力净流入_万")),
        "主力净占比_pct": num(item.get("主力净占比_pct")),
        "价格来源": "产物快照", "价格时间": None,
        "是否持仓": code in held, "是否自选": code in watch,
        "行来源": item.get("来源") or "模型",
    }


def _old_row(it, code, held, watch):
    """旧产物（板块筛选版）：只有机械分，没有模型评分与打法。"""
    return {
        "代码": code, "名称": it.get("名称"), "打法": FACET_OTHER,
        "推荐度": num(it.get("机械分")), "模型分": None, "机械分": num(it.get("机械分")),
        "评级": it.get("评级"), "理由": None,
        "所属板块": it.get("来源板块") or it.get("所属行业"), "行业": it.get("所属行业"),
        "排名": None,
        "产物现价": num(it.get("现价")), "现价": num(it.get("现价")),
        "涨跌幅_pct": num(it.get("涨跌幅_pct")),
        "换手率_pct": num(it.get("换手率_pct")), "量比": num(it.get("量比")),
        "主力净流入_万": None if num(it.get("主力净流入_亿")) is None
                          else round(num(it.get("主力净流入_亿")) * 1e4, 2),
        "主力净占比_pct": num(it.get("主力净占比_pct")),
        "价格来源": "产物快照", "价格时间": None,
        "是否持仓": code in held, "是否自选": code in watch,
        "行来源": "旧产物",
    }


def sort_key(row):
    return (-(row.get("推荐度") if row.get("推荐度") is not None else -1.0),
            -(row.get("机械分") if row.get("机械分") is not None else -1.0),
            str(row.get("代码") or ""))


def facet_styles(rows):
    """打法筛选：五档固定顺序，只有真的有候选时才可点；另有「未定」。"""
    counts = {}
    for row in rows:
        name = row.get("打法") or FACET_OTHER
        counts[name] = counts.get(name, 0) + 1
    out = [{"名称": name, "周期": cycle, "候选数": counts.get(name, 0)}
           for name, cycle in PICK_STYLES]
    if counts.get(FACET_OTHER):
        out.append({"名称": FACET_OTHER, "周期": "", "候选数": counts[FACET_OTHER]})
    return out


def apply_quotes(rows, quote_map, refresh=False):
    """把批量报价贴到榜单行；没有实时价的行保留产物快照价。"""
    if not refresh:
        return rows
    for row in rows:
        quote = (quote_map or {}).get(row.get("代码"))
        if not quote or quote.get("价格") is None:
            continue
        row["现价"] = quote.get("价格")
        row["涨跌幅_pct"] = quote.get("涨跌幅_pct")
        row["价格来源"] = "东财实时行情"
        row["价格时间"] = time.strftime("%H:%M:%S")
    return rows


def empty_rank(note=None):
    return {"产物": None, "口径": RANK_NOTE, "行": [], "打法": facet_styles([]),
            "提示": [note or "还没有荐股结果（data/ai/pick/）：去「荐股」页跑一次全大盘扫描"]}


def build_rank(held=None, watch=None):
    """最新一份荐股产物 → 榜单结构；没有产物时返回空骨架（不抛错）。"""
    path = latest_pick_path()
    doc = aiplan.read_json(path) if path else None
    if not doc:
        return empty_rank()
    rows = build_rows(doc, held, watch)
    tips = []
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        mtime = None
    days = None if mtime is None else max(0, int((time.time() - mtime) // 86400))
    if days is not None and days >= STALE_DAYS:
        tips.append("产物是 %d 天前的（%s）：价格会实时刷新，但评分与理由仍是那天的"
                    % (days, time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))))
    if not (doc.get("推荐榜") or []):
        tips.append("这份产物没有模型推荐榜（旧版产物或模型失败）：榜单按机械分排序，"
                    "去「荐股」页重跑一次就能拿到模型评分")
    model = doc.get("模型层") or {}
    if model.get("error"):
        tips.append("本次模型失败：%s（已按机械分排序）" % str(model["error"])[:120])
    md = os.path.splitext(path)[0] + ".md"
    return {
        "产物": {
            "json路径": rel(path), "md路径": rel(md) if os.path.exists(md) else None,
            "生成时间": doc.get("生成时间"), "交易日": doc.get("交易日"),
            "扫描": doc.get("扫描") or {}, "扫描参数": doc.get("扫描参数") or {},
            "机械口径": doc.get("机械口径") or {},
            "被否决": len(doc.get("被否决") or []),
            "模型": {"profile": (doc.get("配置") or {}).get("profile"),
                     "model": (doc.get("配置") or {}).get("model")},
            "成本": doc.get("成本") or {}, "降级": doc.get("降级") or [],
            "产物时间": None if mtime is None else time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)),
            "天数": days,
        },
        "口径": RANK_NOTE, "行": rows, "打法": facet_styles(rows), "提示": tips,
    }
