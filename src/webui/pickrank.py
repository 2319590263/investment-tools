# -*- coding: utf-8 -*-
"""总控台荐股榜：把最新一份荐股产物组装成「推荐度降序 + 两级筛选树」的行表。

纯函数为主，只有 build_rank() 读一次最新产物 JSON；行情不在这里取——
overview 把榜单代码并进同一次东财批量请求，取不到时回退产物快照价。
"""

import os
import time

from .archive import latest_pick_path
from .paths import aiplan, num, rel
from .pick import pick_concept_theme

RANK_BONUS = (8.0, 6.0, 4.0)        # 模型首推第 1/2/3 名的推荐度加成
RANK_CAP = 100.0                    # 推荐度上限
STALE_DAYS = 3                      # 产物超过几天就在页面上提醒
OTHER_L1 = "未归类"
RANK_NOTE = ("推荐度 = 机械分 + 模型首推加成（首推第 1/2/3 名 +8/+6/+4，上限 100）；"
             "机械分是候选池内分位打分，不是收益预测")


def rank_bonus(place):
    """首推名次 → 推荐度加成；非首推（None / 越界）返回 0。"""
    if not isinstance(place, int) or place < 1 or place > len(RANK_BONUS):
        return 0.0
    return RANK_BONUS[place - 1]


def recommend_score(mechanic, place):
    """推荐度 = min(100, 机械分 + 加成)；机械分缺失时给 None（排最后）。"""
    value = num(mechanic)
    if value is None:
        return None
    return round(min(RANK_CAP, value + rank_bonus(place)), 1)


def board_l1_map(doc):
    """产物板块表 → {细分板块名: 一级行业名}，用来把候选归到一级行业。"""
    out = {}
    for row in (doc.get("板块") or {}).get("行业") or []:
        name = str(row.get("名称") or "").strip()
        l1 = str(row.get("一级行业") or "").strip()
        if name and l1:
            out[name] = l1
    return out


def first_picks(doc):
    """模型层首推 → {(模块, 代码): (名次, 首推条目)}；没有模型点评时为空。"""
    model = ((doc.get("模型层") or {}).get("json") or {})
    mods = model.get("模块") or doc.get("模块") or []
    out = {}
    for item in mods or []:
        module = str(item.get("模块") or "").strip()
        if not module:
            continue
        for idx, pick in enumerate(item.get("首推") or [], start=1):
            code = aiplan.code6(pick.get("代码"))
            if code:
                out[(module, code)] = (idx, pick)
    return out


def price_slot(pick, key):
    """首推里的价位块：关注买点 / 止损 / 目标位（取第一个）。"""
    if not pick:
        return None
    if key == "目标位":
        goals = pick.get("目标位") or []
        return goals[0] if goals else None
    return pick.get(key) or None


def sort_key(row):
    return (-(row.get("推荐度") if row.get("推荐度") is not None else -1.0),
            -(row.get("机械分") if row.get("机械分") is not None else -1.0),
            -(row.get("产物涨跌幅_pct") if row.get("产物涨跌幅_pct") is not None else -1e9),
            str(row.get("代码") or ""))


def build_rows(doc, held=None, watch=None):
    """产物 → 榜单行（已按推荐度降序）。首推价位是模型给的文本，原样透传。"""
    l1_map = board_l1_map(doc)
    firsts = first_picks(doc)
    held, watch = set(held or ()), set(watch or ())
    rows = []
    for module, items in (doc.get("候选") or {}).items():
        for it in items or []:
            code = aiplan.code6(it.get("代码"))
            if not code:
                continue
            place, pick = firsts.get((module, code), (None, None))
            mechanic = num(it.get("机械分"))
            kind = str(it.get("板块类型") or "").strip()
            source = str(it.get("来源板块") or "").strip()
            own = str(it.get("所属行业") or "").strip()
            sub = source if kind == "行业" else (own or source)
            rows.append({
                "代码": code, "名称": it.get("名称"), "模块": module,
                "板块类型": kind or None, "来源板块": source or None,
                "细分": sub or None, "所属行业": own or None,
                "一级行业": l1_map.get(sub) or l1_map.get(source) or OTHER_L1,
                "概念主题": pick_concept_theme(source) if kind == "概念" else None,
                "机械分": mechanic, "评级": it.get("评级"),
                "首推名次": place, "加成": rank_bonus(place),
                "推荐度": recommend_score(mechanic, place),
                "产物现价": num(it.get("现价")),
                "产物涨跌幅_pct": num(it.get("涨跌幅_pct")),
                "换手率_pct": num(it.get("换手率_pct")), "量比": num(it.get("量比")),
                "主力净流入_亿": num(it.get("主力净流入_亿")),
                "现价": num(it.get("现价")), "涨跌幅_pct": num(it.get("涨跌幅_pct")),
                "价格来源": "产物快照", "价格时间": None,
                "买点": price_slot(pick, "关注买点"), "止损": price_slot(pick, "止损"),
                "止盈点": price_slot(pick, "目标位"),
                "止盈点数": len((pick or {}).get("目标位") or []),
                "首推理由": (pick or {}).get("理由"), "首推评级": (pick or {}).get("评级"),
                "是否持仓": code in held, "是否自选": code in watch,
            })
    rows.sort(key=sort_key)
    return rows


def facet_tree(rows):
    """行 → 两级筛选树（只保留有候选的节点）。"""
    ind, con, other = {}, {}, {}
    for row in rows:
        l1 = row.get("一级行业") or OTHER_L1
        sub = row.get("细分") or row.get("来源板块") or "未知板块"
        if l1 == OTHER_L1:
            other[sub] = other.get(sub, 0) + 1
        else:
            ind.setdefault(l1, {})
            ind[l1][sub] = ind[l1].get(sub, 0) + 1
        if row.get("板块类型") == "概念":
            theme = row.get("概念主题") or "其他"
            name = row.get("来源板块") or "未知概念"
            con.setdefault(theme, {})
            con[theme][name] = con[theme].get(name, 0) + 1
    return {"行业": _two_level(ind), "概念": _two_level(con),
            "未归类": [{"名称": k, "候选数": v} for k, v in sorted(other.items())]}


def _two_level(bucket):
    out = []
    for name in sorted(bucket, key=lambda x: (-sum(bucket[x].values()), x)):
        subs = [{"名称": k, "候选数": v} for k, v in
                sorted(bucket[name].items(), key=lambda kv: (-kv[1], kv[0]))]
        out.append({"名称": name, "候选数": sum(bucket[name].values()), "子项": subs})
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
    return {"产物": None, "口径": RANK_NOTE, "行": [],
            "筛选树": {"行业": [], "概念": [], "未归类": []},
            "提示": [note or "还没有荐股结果（data/ai/pick/）：去「荐股」页选板块跑一次"]}


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
        tips.append("产物是 %d 天前的（%s）：价格会实时刷新，但结论与价位仍是那天的"
                    % (days, time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))))
    modules = ((doc.get("参数") or {}).get("模块")) or []
    if len(modules) == 1:
        tips.append("本次产物只覆盖「%s」模块：在「荐股」页多选模块重跑，榜单就能按 4 个模块筛"
                    % modules[0])
    md = os.path.splitext(path)[0] + ".md"
    return {
        "产物": {
            "json路径": rel(path), "md路径": rel(md) if os.path.exists(md) else None,
            "生成时间": doc.get("生成时间"), "交易日": doc.get("交易日"),
            "模块": modules, "筛选": (doc.get("参数") or {}).get("筛选") or {},
            "模型": {"profile": (doc.get("配置") or {}).get("profile"),
                     "model": (doc.get("配置") or {}).get("model")},
            "成本": doc.get("成本") or {}, "降级": doc.get("降级") or [],
            "产物时间": None if mtime is None else time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)),
            "天数": days,
        },
        "口径": RANK_NOTE, "行": rows, "筛选树": facet_tree(rows), "提示": tips,
    }
