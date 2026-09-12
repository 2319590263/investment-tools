# -*- coding: utf-8 -*-
"""报告实盘复核：用当前实盘价核对某份报告的交易计划，并给出当前时段的策略点评。

机械层是纯函数（零成本、秒出、可单测）；模型层复用 aiplan 的模型层（一次研判档调用，
产物落 data/ai/plancheck/）。实盘价走东财实时快照（纯 HTTP，不调 node），
失败按 东财 → 最新 stock3d 缓存 → 报告内现价 降级，并在结果里显式标注来源与口径。
"""

import copy
import json
import os
import re
import time
from datetime import date, datetime

from .paths import (AI_DIR, MODELS_PATH, PAN_DIR, PHASE_LABEL, PLANCHECK_DIR, ROOT, aiplan,
                    atomic_write, inside, num, now_str, pan, rel)
from .background import latest_pan, market_context, stock3d_tech
from .sources import stock3d_snapshot

QUOTE_FIELDS = "f12,f13,f14,f2,f3,f4,f5,f6,f8,f10,f15,f16,f17,f18,f20,f21,f62"
QUOTE_TTL = 5.0        # 同一标的 5 秒内复用：一次渲染里多处取现价不重复请求
LEVEL_TOL = 0.01       # 触发容差 = 1 个最小变动价位（A 股 0.01 元）
KEEP_SCAN = 120        # 查历史点评时最多扫描多少份产物

_QUOTE_CACHE = {}      # 6 位代码 -> (取数时间戳, 行情 dict)
_CAL_CACHE = {"mtime": None, "days": None}

BUY_WORDS = ("建仓", "买入", "加仓", "补仓", "开仓", "吸纳")
SELL_WORDS = ("减仓", "清仓", "卖出", "止盈", "止损", "减", "卖", "清")


# ---------------------------------------------------------------------------
# 纯函数：日期 / 时段
# ---------------------------------------------------------------------------

def _to_date(s):
    m = re.match(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})", str(s or ""))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def calendar_days():
    """本地交易日历（pan.py 落的 data/pan/state/calendar.json）；没有返回 None。"""
    path = os.path.join(PAN_DIR, "state", "calendar.json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    if _CAL_CACHE["mtime"] == mtime:
        return _CAL_CACHE["days"]
    doc = aiplan.read_json(path) or {}
    days = {str(d) for d in (doc.get("days") or []) if re.match(r"^\d{4}-\d{2}-\d{2}$", str(d))}
    _CAL_CACHE["mtime"] = mtime
    _CAL_CACHE["days"] = days or None
    return _CAL_CACHE["days"]


def session_of(now=None):
    """当前时段：名称复用 pan.session_name()，是否交易日用本地日历校正。"""
    now = now or datetime.now()
    name = pan.session_name(now)
    days = calendar_days()
    if days:
        trading = now.strftime("%Y-%m-%d") in days
        basis = "本地交易日历（data/pan/state/calendar.json）"
    else:
        trading = now.weekday() < 5
        basis = "无本地交易日历，按周一至周五判定"
    if not trading:
        name = "非交易日"
    return {"名称": name, "是否交易日": bool(trading), "判定依据": basis,
            "现在": now.strftime("%Y-%m-%d %H:%M:%S")}


def report_relation(trade_date, today=None):
    """报告交易日与今天的差距（自然日）。"""
    today = today or date.today()
    d = _to_date(trade_date)
    if not d:
        return {"文本": "报告未记录交易日", "天数": None, "报告交易日": None}
    delta = (today - d).days
    if delta == 0:
        text = "报告交易日就是今天"
    elif delta > 0:
        text = "报告交易日已过 %d 天" % delta
    else:
        text = "报告指向未来 %d 天" % (-delta)
    return {"文本": text, "天数": delta, "报告交易日": d.isoformat()}


def phase_hint(session_name):
    """按时段的固定口径说明（与具体价格无关，便于单测）。"""
    return {
        "非交易日": "非交易日不开市：以下按最近收盘价核对，等下一交易日开盘再执行。",
        "盘前": "盘前：行情仍是上一交易日收盘口径，先把触发价与仓位想清楚，不在竞价追价。",
        "集合竞价": "集合竞价：开盘价还没形成，只做挂单准备，成交前不追价。",
        "盘中（上午）": "盘中：触发价到位才执行，没到位就等；跌破止损立即处理。",
        "午间休市": "午间休市：暂停交易，下午开盘再核对触发与失效。",
        "盘中（下午）": "盘中：触发价到位才执行；尾盘仍未触发就按未触发处理。",
        "盘后": "盘后：今日已收盘，核对触发结果，未执行的下个交易日按预案继续。",
    }.get(session_name, "按时段口径核对触发与失效。")


def quote_label(session_name):
    """实盘价在当前时段的口径（不许把非交易日说成实时价）。"""
    if session_name == "非交易日":
        return "最近收盘价（非交易日）"
    if session_name in ("盘前", "集合竞价"):
        return "昨收 / 竞价口径（未开盘）"
    if session_name == "午间休市":
        return "上午收盘价"
    if session_name == "盘后":
        return "今日收盘价"
    return "盘中实时价"


# ---------------------------------------------------------------------------
# 纯函数：计划判定
# ---------------------------------------------------------------------------

def plan_kind(act):
    """计划动作归类：买入 / 卖出 / 观察。"""
    a = str(act or "")
    if any(w in a for w in SELL_WORDS):
        return "卖出"
    if any(w in a for w in BUY_WORDS):
        return "买入"
    return "观察"


def is_noop(act):
    """无执行动作的条目（观望 / 持有 / 等待）——加入自选或持仓本身就等于在观望，
    这类条目不再占用计划表与模型输入，只在页面上留一行「已省略 N 条」的说明。"""
    return plan_kind(act) == "观察"


def parse_range(rng):
    """价格区间 → (下沿, 上沿)；支持 [lo, hi] 与 "13.4-14.0" 两种写法。

    字符串形态里 `-` 是区间连接符而不是负号（股价没有负数），所以不能匹配 `-?\\d`，
    否则 "13.40-14.00" 会被解析成 (-14.0, 13.4)。"""
    if isinstance(rng, (list, tuple)):
        vals = [num(v) for v in rng]
    else:
        vals = [num(x) for x in re.findall(r"\d+(?:\.\d+)?", str(rng or ""))]
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return min(vals), max(vals)


def verdict_of_item(item, price, stop=None, relation=None):
    """一条计划动作对当前价的机械判定。"""
    item = item or {}
    act = str(item.get("动作") or "").strip()
    kind = plan_kind(act)
    rng = parse_range(item.get("价格区间"))
    out = {"优先级": item.get("优先级"), "动作": act or "—", "类别": kind,
           "区间": list(rng) if rng else None,
           "区间文本": ("%.2f ~ %.2f" % rng) if rng else "—",
           "股数": item.get("股数"), "金额_元": item.get("金额_元"),
           "触发条件": item.get("触发条件"), "失效条件": item.get("失效条件"),
           "状态": "未触发", "距离_pct": None, "说明": ""}
    if item.get("_作废"):
        out["状态"] = "已作废"
        out["说明"] = "该条已被报告内「计划校正」作废，不作为执行依据"
        return out
    if price is None:
        out["状态"] = "无法判定"
        out["说明"] = "本次取不到实盘价"
        return out
    if stop is not None and price <= stop + 1e-9:
        out["状态"] = "已失效"
        out["说明"] = "现价 %.2f 已跌破止损 %.2f，该条按计划失效" % (price, stop)
        return out
    if not rng:
        out["状态"] = "无法判定"
        out["说明"] = "该条没有可解析的价格区间"
        return out
    lo, hi = rng
    if lo - LEVEL_TOL <= price <= hi + LEVEL_TOL:
        out["状态"] = "已触发"
        what = {"买入": "可按该条买入", "卖出": "可按该条减/清",
                "观察": "按该条观察，不追价"}.get(kind, "按该条执行")
        out["说明"] = "现价 %.2f 已进入区间 %s，%s" % (price, out["区间文本"], what)
        return out
    dist = (lo - price) / lo * 100 if price < lo and lo else (price - hi) / hi * 100 if hi else None
    out["距离_pct"] = round(dist, 2) if dist is not None else None
    if kind == "买入":
        out["说明"] = ("现价 %.2f 低于区间下沿，还差 %.2f%% 到买点" % (price, dist)) if price < lo else \
                      ("现价 %.2f 已高于区间上沿 %.2f%%，该买点已错过（不追高）" % (price, dist))
    elif kind == "卖出":
        out["说明"] = ("现价 %.2f 低于区间下沿，还差 %.2f%% 才到减仓区" % (price, dist)) if price < lo else \
                      ("现价 %.2f 已高于区间上沿 %.2f%%，已越过该减仓位" % (price, dist))
    else:
        out["说明"] = ("现价 %.2f 比区间下沿低 %.2f%%" % (price, dist)) if price < lo else \
                      ("现价 %.2f 比区间上沿高 %.2f%%" % (price, dist))
    if relation and relation.get("天数"):
        out["说明"] += "；计划基于 %s" % (relation.get("报告交易日") or "旧交易日")
    return out


def verdict_levels(levels, price):
    """关键价位（止损 / 目标 / 支撑 / 压力）对当前价的判定。"""
    levels = levels or {}

    def _v(x):
        return num(x.get("价位")) if isinstance(x, dict) else num(x)

    def _n(x):
        return (x.get("依据") or "") if isinstance(x, dict) else ""

    out = {"止损": None, "目标": [], "支撑": None, "压力": None}
    stop = num(levels.get("止损价"))
    if stop is not None:
        out["止损"] = {
            "价位": stop,
            "状态": ("已破位" if (price is not None and price <= stop + 1e-9) else "未破位"),
            "距离_pct": (round((price - stop) / stop * 100, 2) if (price is not None and stop) else None),
        }
    for x in (levels.get("目标位") or []):
        v = _v(x)
        if v is None:
            continue
        out["目标"].append({"价位": v, "依据": _n(x),
                            "已达标": bool(price is not None and price >= v - 1e-9)})
    sup = sorted(v for v in (_v(x) for x in (levels.get("支撑") or [])) if v is not None)
    pre = sorted(v for v in (_v(x) for x in (levels.get("压力") or [])) if v is not None)
    if price is not None and sup:
        broken = [v for v in sup if v > price + 1e-9]
        if broken:
            v = min(broken)
            out["支撑"] = {"价位": v, "状态": "已跌破", "依据": _n(_find(levels.get("支撑"), v)),
                           "距离_pct": round((price - v) / v * 100, 2)}
        else:
            v = max(x for x in sup if x <= price)
            out["支撑"] = {"价位": v, "状态": "未触及", "依据": _n(_find(levels.get("支撑"), v)),
                           "距离_pct": round((price - v) / v * 100, 2)}
    if price is not None and pre:
        above = [v for v in pre if v >= price - 1e-9]
        if above:
            v = min(above)
            out["压力"] = {"价位": v, "状态": "未触及", "依据": _n(_find(levels.get("压力"), v)),
                           "距离_pct": round((v - price) / price * 100, 2) if price else None}
        else:
            v = max(pre)
            out["压力"] = {"价位": v, "状态": "已上破", "依据": _n(_find(levels.get("压力"), v)),
                           "距离_pct": round((price - v) / v * 100, 2)}
    return out


def _find(rows, value):
    for x in (rows or []):
        v = num(x.get("价位")) if isinstance(x, dict) else num(x)
        if v is not None and abs(v - value) < 1e-9:
            return x
    return None


def mechanical_summary(items, levels, price, session, relation):
    """机械一句话结论：先给时段口径，再说触发状态与接下来看什么。"""
    sess = (session or {}).get("名称") if isinstance(session, dict) else str(session or "")
    bits = [phase_hint(sess)]
    if price is None:
        bits.append("本次取不到实盘价，无法给出触发结论。")
        return "".join(bits)
    if not items:
        bits.append("现价 %.2f，这份报告没有需要执行的动作（观望类条目已省略）。" % price)
        if relation and relation.get("天数"):
            bits.append("（%s）" % relation["文本"])
        return "".join(bits)
    trig = [it for it in items if it.get("状态") == "已触发"]
    stop = (levels or {}).get("止损") or {}
    if stop.get("状态") == "已破位":
        bits.append("现价 %.2f 已跌破止损 %.2f，按计划先处理风险。" % (price, stop["价位"]))
    elif trig:
        bits.append("现价 %.2f 已触发 %d 条：%s。" % (
            price, len(trig), "、".join("%s（%s）" % (it["动作"], it["区间文本"]) for it in trig[:3])))
        rest = [it for it in items if it.get("状态") == "未触发"]
        if rest:
            bits.append("其余 %d 条未触发。" % len(rest))
    else:
        near = [it for it in items if it.get("状态") == "未触发" and it.get("距离_pct") is not None]
        if near:
            n = min(near, key=lambda x: x["距离_pct"])
            bits.append("现价 %.2f，全部计划未触发，最近的一条「%s」还差 %.2f%%。"
                        % (price, n["动作"], n["距离_pct"]))
        else:
            bits.append("现价 %.2f，暂无可执行的计划条目。" % price)
    hit = [t for t in ((levels or {}).get("目标") or []) if t.get("已达标")]
    if hit:
        bits.append("已达目标位 %s。" % "、".join("%.2f" % t["价位"] for t in hit))
    if relation and relation.get("天数"):
        bits.append("（%s）" % relation["文本"])
    return "".join(bits)


# ---------------------------------------------------------------------------
# 实盘价：东财 → 最新 stock3d 缓存 → 报告内现价
# ---------------------------------------------------------------------------

def _quote_from_em(thscode):
    ctx = pan.Ctx(PAN_DIR, date.today(), "post")
    secid = pan._em_secid_of(thscode)
    if not secid:
        return None
    rows = pan.em_ulist(ctx, [secid], QUOTE_FIELDS)
    if not rows:
        return None
    r = rows[0]
    price = num(r.get("f2"))
    if price is None:
        return None
    return {"代码": aiplan.code6(thscode), "名称": r.get("f14"), "价格": price,
            "涨跌幅_pct": num(r.get("f3")), "涨跌额": num(r.get("f4")),
            "昨收": num(r.get("f18")), "今开": num(r.get("f17")),
            "最高": num(r.get("f15")), "最低": num(r.get("f16")),
            "成交量": num(r.get("f5")), "成交额": num(r.get("f6")),
            "换手率_pct": num(r.get("f8")), "量比": num(r.get("f10")),
            "主力净流入": num(r.get("f62")),
            "来源": "东财实时行情", "抓取时间": now_str()}


def _quote_from_stock3d(c6):
    path, doc, _codes = stock3d_snapshot()
    for sym in (doc or {}).get("symbols") or []:
        if aiplan.code6(sym.get("code")) != c6:
            continue
        tech = sym.get("tech") or {}
        snap = tech.get("snapshot") or {}
        price = num(snap.get("现价"))
        if price is None:
            continue
        return {"代码": c6, "名称": sym.get("name") or snap.get("名称"), "价格": price,
                "涨跌幅_pct": num(snap.get("涨跌幅_pct")), "涨跌额": num(snap.get("涨跌额")),
                "昨收": num(snap.get("昨收")), "今开": num(snap.get("今开")),
                "最高": num(snap.get("最高")), "最低": num(snap.get("最低")),
                "成交量": num(snap.get("成交量")), "成交额": num(snap.get("成交额")),
                "换手率_pct": None, "量比": None, "主力净流入": None,
                "来源": "stock3d 缓存（降级）",
                "抓取时间": tech.get("fetched_at") or (doc or {}).get("generated_at") or rel(path)}
    return None


def fetch_quote(thscode, fallback_price=None, refresh=False):
    """取当前实盘价。返回 (quote, 提示列表)；quote 带「来源 / 口径 / 抓取时间」。"""
    c6 = aiplan.code6(thscode)
    if not c6:
        return None, ["报告没有可用标的代码，取不到实盘价"]
    now = time.time()
    hit = _QUOTE_CACHE.get(c6)
    if not refresh and hit and now - hit[0] <= QUOTE_TTL:
        return copy.deepcopy(hit[1]), []
    hints, quote = [], None
    try:
        quote = _quote_from_em(thscode)
    except Exception:            # noqa: BLE001  取数层任何异常都按降级处理，不让整页失败
        quote = None
    if quote is None:
        quote = _quote_from_stock3d(c6)
        if quote:
            hints.append("东财实时行情不可用，已降级为最新 stock3d 缓存价")
    if quote is None and num(fallback_price) is not None:
        quote = {"代码": c6, "名称": None, "价格": num(fallback_price), "涨跌幅_pct": None,
                 "涨跌额": None, "昨收": None, "今开": None, "最高": None, "最低": None,
                 "成交量": None, "成交额": None, "换手率_pct": None, "量比": None,
                 "主力净流入": None, "来源": "报告内现价（降级）", "抓取时间": "报告生成时"}
        hints.append("东财与本地缓存都不可用，已降级为报告生成时的现价")
    if quote is None:
        return None, ["取不到实盘价：东财实时行情与本地缓存都不可用"]
    sess = session_of()
    quote["口径"] = quote_label(sess["名称"])
    quote["时段"] = sess["名称"]
    _QUOTE_CACHE[c6] = (now, copy.deepcopy(quote))
    return quote, hints




# ---------------------------------------------------------------------------
# 机械复核
# ---------------------------------------------------------------------------

def _report_path(report_rel):
    if not report_rel:
        return None, "缺少 report（报告相对路径）"
    p = report_rel if os.path.isabs(report_rel) else os.path.join(ROOT, report_rel)
    p = os.path.abspath(p)
    if not inside(p, AI_DIR) or not os.path.exists(p):
        return None, "报告不存在，或不在 data/ai 目录下"
    return p, None


def build_plan_check(report_rel, refresh=False):
    """机械复核：报告 + 实盘价 + 时段 + 逐条判定。返回 (data, err)。"""
    path, err = _report_path(report_rel)
    if err:
        return None, err
    doc = aiplan.read_json(path)
    if not isinstance(doc, dict):
        return None, "报告读不出内容：%s" % rel(path)
    plan = ((doc.get("研判") or {}).get("json") or {})
    review = ((doc.get("复核") or {}).get("json") or {})
    levels = plan.get("关键价位") or {}
    stop = num(levels.get("止损价"))
    session = session_of()
    relation = report_relation(doc.get("trade_date"))
    quote, hints = fetch_quote((doc.get("标的") or {}).get("代码"),
                               fallback_price=doc.get("现价"), refresh=refresh)
    price = num((quote or {}).get("价格"))
    name = (doc.get("标的") or {}).get("名称")
    tech, _s3_path = stock3d_tech((doc.get("标的") or {}).get("代码"))
    pan_doc, pan_path = latest_pan()
    background = market_context(tech, pan_doc, name=name,
                                pan_rel=rel(pan_path) if pan_path else None)
    if not (background.get("个股量价与形态") or {}):
        hints.append("本地 stock3d 快照里没有该标的的量价细节（先跑 python stock3d.py pull <代码>），"
                     "模型只能用实盘价与大盘数据判定条件")
    void = [str(x) for x in ((plan.get("计划校正") or {}).get("作废条目") or [])]
    items, skipped = [], 0
    for idx, raw in enumerate(plan.get("计划") or []):
        item = dict(raw or {})
        if item.get("优先级") is None:
            item["优先级"] = idx + 1
        if is_noop(item.get("动作")):
            skipped += 1
            continue
        if str(item) in void or str(item.get("动作")) in void:
            item["_作废"] = True
        items.append(verdict_of_item(item, price, stop=stop, relation=relation))
    lv = verdict_levels(levels, price)
    mech = {"一句话": mechanical_summary(items, lv, price, session, relation),
            "口径": phase_hint(session["名称"]),
            "计划": items, "关键价位": lv}
    if skipped:
        mech["省略"] = {
            "条数": skipped,
            "说明": "已省略 %d 条观望/持有类条目（无执行动作——加入自选或持仓本身就等于在观望）" % skipped,
        }
    if not session["是否交易日"]:
        hints.append("今天不是交易日（%s），价格是最近收盘口径" % session["判定依据"])
    if relation.get("天数"):
        hints.append(relation["文本"] + "：触发判断仅供参考，建议用当天生成的新报告")
    data = {
        "报告": {"路径": rel(path), "标的": doc.get("标的") or {},
                 "phase": doc.get("phase"),
                 "phase标签": PHASE_LABEL.get(doc.get("phase"), doc.get("phase")),
                 "trade_date": doc.get("trade_date"), "generated_at": doc.get("generated_at"),
                 "报告现价": num(doc.get("现价")), "方向": plan.get("方向"),
                 "置信度": plan.get("置信度"), "一句话结论": plan.get("一句话结论"),
                 "profile": (doc.get("profile") or {}).get("名称"),
                 "持仓": doc.get("持仓")},
        "实盘": quote, "时段": session, "关系": relation, "机械": mech,
        "背景数据": background, "复核方向": review.get("方向"), "提示": hints,
    }
    return data, None


def plancheck_bundle(report_rel, refresh=False):
    """机械复核 + 该报告最近一次模型点评。"""
    data, err = build_plan_check(report_rel, refresh=refresh)
    if err:
        return None, err
    doc, path = latest_plan_check(data["报告"]["路径"])
    data["点评"] = doc
    data["点评路径"] = rel(path) if path else None
    return data, None


# ---------------------------------------------------------------------------
# 产物读写
# ---------------------------------------------------------------------------

def save_plan_check(payload):
    day = datetime.now().strftime("%Y%m%d")
    d = os.path.join(PLANCHECK_DIR, day)
    os.makedirs(d, exist_ok=True)
    body = json.dumps(payload, ensure_ascii=False, indent=1)
    stamp = datetime.now().strftime("%H%M%S")
    path = os.path.join(d, stamp + "_plancheck.json")
    seq = 1
    while os.path.exists(path):          # 同一秒内第二次点评不能覆盖前一次
        path = os.path.join(d, "%s-%d_plancheck.json" % (stamp, seq))
        seq += 1
    atomic_write(path, body, newline="\n")
    atomic_write(os.path.join(d, "latest.json"), body, newline="\n")
    return path


def list_plan_checks(limit=KEEP_SCAN):
    """最近若干份点评产物（按 mtime 倒序）。"""
    out = []
    if not os.path.isdir(PLANCHECK_DIR):
        return out
    for day in os.listdir(PLANCHECK_DIR):
        d = os.path.join(PLANCHECK_DIR, day)
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if re.match(r"^\d{6}(-\d+)?_plancheck\.json$", name):
                out.append(os.path.join(d, name))
    out.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return out[:max(1, int(limit or KEEP_SCAN))]


def latest_plan_check(report_rel=None):
    """某份报告最近一次点评；report_rel 为空时取全局最新。"""
    for p in list_plan_checks():
        doc = aiplan.read_json(p)
        if isinstance(doc, dict) and (not report_rel or doc.get("报告路径") == report_rel):
            return doc, p
    return None, None


# ---------------------------------------------------------------------------
# 模型点评（复用 aiplan 的模型层，一次研判档调用）
# ---------------------------------------------------------------------------

PLANCHECK_SYSTEM = ("你是 A 股交易计划的执行助手，只依据给定事实数据判断当前时段该不该执行计划里的动作。"
                    "你要自己下结论：条件成没成立都必须明确表态，禁止用「需人工确认」「请自行判断」这类措辞推回给使用者；"
                    "判断必须引用给定数据里的具体数值，不编造数值，不新增计划外的买卖建议。")

PLANCHECK_PROMPT = """下面是本地程序采集的中性数据：一份已生成的交易计划、当前实盘价与当前时段、脚本按价格做的机械判定，
以及供你判断文字条件用的背景数据（个股均线/分位、涨跌日量能比、换手、量价背离、涨跌停与连板、大盘广度与资金、板块领涨领跌与资金）。

请只依据这些数据回答，并输出**一个 JSON 对象**（不要解释文字、不要代码块围栏），字段固定为：
{
 "当前时段": "盘前/集合竞价/盘中（上午）/午间休市/盘中（下午）/盘后/非交易日",
 "是否交易日": true,
 "一句话结论": "现在该做什么（一句话，带价位）",
 "动作清单": [{"优先级": 1, "动作": "观望", "执行": "执行|等待|放弃", "价位": "14.98 ~ 15.27", "理由": "…"}],
 "触发确认": [{"条件": "…", "状态": "已满足|未满足", "依据": "你引用了哪些具体数值"}],
 "风险与失效": ["…"],
 "数据依赖与不确定性": ["…"]
}

要求：
1. 「动作清单」覆盖计划里的每一条，沿用报告原来的优先级与动作名；「执行」只能是 执行 / 等待 / 放弃 三选一。
2. 「触发确认」覆盖每条的触发条件与失效条件，状态**只能是「已满足」或「未满足」**，禁止出现「需人工确认」。
   价格类条件用机械判定结果；「放量、缩量、企稳、板块资金、大盘上涨占比」这类文字条件，用背景数据的实际数值自己判
   （例如量比、近5/10/20日涨跌量比、换手率与5日均、均线位置与分位、量价背离、板块主力净流入、上涨占比、涨跌停家数）。
3. 「依据」必须写出你用的具体数据与数值（例如「量比 1.29、近5日涨跌量比 1.55 → 放量」）；只给结论不给数值不算完成。
4. 确实缺数据判不了时：状态按「未满足」处理，并在「数据依赖与不确定性」写清缺哪项、为什么影响判断——
   不要用「需人工确认」把结论推回给使用者。
5. 报告交易日与今天不同时，必须把「计划已过期、只能当参考」写进「数据依赖与不确定性」。
6. 不构成投资建议，不要新增计划外的买卖建议。"""


def _f(v, nd=2):
    return "—" if v is None else ("%." + str(int(nd)) + "f") % v


def plancheck_factpack(data):
    """给模型的中性事实包：报告计划 + 当前实盘价与时段 + 机械判定表。"""
    r = data.get("报告") or {}
    q = data.get("实盘") or {}
    s = data.get("时段") or {}
    mech = data.get("机械") or {}
    sym = r.get("标的") or {}
    out = []
    out.append("【报告】")
    out.append("- 标的：%s %s" % (sym.get("名称") or "—", sym.get("代码") or "—"))
    out.append("- 报告时段：%s ｜ 交易日：%s ｜ 生成：%s ｜ profile：%s"
               % (r.get("phase"), r.get("trade_date"), r.get("generated_at"), r.get("profile")))
    out.append("- 报告当时现价：%s ｜ 方向：%s ｜ 置信度：%s"
               % (_f(r.get("报告现价")), r.get("方向") or "—", r.get("置信度")))
    out.append("- 原报告一句话结论：%s" % (r.get("一句话结论") or "—"))
    hold = r.get("持仓") or {}
    if hold:
        out.append("- 该标的持仓：%s" % json.dumps(hold, ensure_ascii=False)[:300])
    out.append("")
    out.append("【当前】")
    out.append("- 现在：%s ｜ 时段：%s ｜ 是否交易日：%s（依据：%s）"
               % (s.get("现在"), s.get("名称"), s.get("是否交易日"), s.get("判定依据")))
    out.append("- 与报告交易日的关系：%s" % ((data.get("关系") or {}).get("文本") or "—"))
    if q:
        out.append("- 实盘价：%s（来源：%s ｜ 口径：%s ｜ 抓取：%s）"
                   % (_f(q.get("价格")), q.get("来源"), q.get("口径"), q.get("抓取时间")))
        out.append("- 涨跌：%s%% ｜ 昨收 %s ｜ 今开 %s ｜ 最高 %s ｜ 最低 %s"
                   % (_f(q.get("涨跌幅_pct")), _f(q.get("昨收")), _f(q.get("今开")),
                      _f(q.get("最高")), _f(q.get("最低"))))
        out.append("- 成交额 %s ｜ 换手 %s%% ｜ 量比 %s ｜ 主力净流入 %s"
                   % (_f(q.get("成交额"), 0), _f(q.get("换手率_pct")), _f(q.get("量比")),
                      _f(q.get("主力净流入"), 0)))
    else:
        out.append("- 实盘价：取不到（实时与本地缓存都不可用）")
    out.append("")
    out.append("【脚本机械判定】")
    out.append("- 结论：%s" % (mech.get("一句话") or "—"))
    out.append("- 计划逐条（「触发条件」就是要你判断的对象，别漏）：")
    for it in mech.get("计划") or []:
        out.append("  · 优先级 %s ｜ 动作 %s（%s）｜ 区间 %s ｜ 状态 %s ｜ 距离 %s%% ｜ 股数 %s ｜ 金额 %s ｜ 失效条件：%s"
                   % (it.get("优先级"), it.get("动作"), it.get("类别"), it.get("区间文本"),
                      it.get("状态"), _f(it.get("距离_pct")), _f(it.get("股数"), 0),
                      _f(it.get("金额_元"), 0), it.get("失效条件") or "—"))
        if it.get("触发条件"):
            out.append("      触发条件：%s" % it["触发条件"])
        if it.get("说明"):
            out.append("      说明：%s" % it["说明"])
    lv = mech.get("关键价位") or {}
    stop = lv.get("止损") or {}
    sup = lv.get("支撑") or {}
    pre = lv.get("压力") or {}
    out.append("- 止损：%s（%s）｜ 支撑：%s（%s）｜ 压力：%s（%s）"
               % (_f(stop.get("价位")), stop.get("状态") or "—",
                  _f(sup.get("价位")), sup.get("状态") or "—",
                  _f(pre.get("价位")), pre.get("状态") or "—"))
    goals = "、".join("%s%s" % (_f(t.get("价位")), "（已达标）" if t.get("已达标") else "")
                      for t in (lv.get("目标") or []))
    out.append("- 目标位：%s" % (goals or "—"))
    bg = data.get("背景数据") or {}
    if any(bg.values()):
        out.append("")
        out.append("【背景数据（判定「放量/缩量/企稳/板块资金」这类文字条件就用这些数）】")
        for label in ("个股量价与形态", "大盘", "板块"):
            val = bg.get(label)
            if val:
                out.append("- %s：%s" % (label, json.dumps(val, ensure_ascii=False)[:2600]))
    if data.get("提示"):
        out.append("")
        out.append("【降级与提示】")
        for h in data["提示"]:
            out.append("- %s" % h)
    return "\n".join(out)


def plancheck_call_model(log, opts, cfg, data, fact):
    """一次模型调用（profile 的研判档）。失败抛异常，由调用方降级保存机械层。"""
    prof_name, prof, src = aiplan.resolve_profile(
        cfg, (data.get("报告") or {}).get("phase") or "post", opts.get("profile") or None, None)
    if not prof:
        raise RuntimeError(src)
    conf = prof.get("研判")
    if not isinstance(conf, dict):
        raise RuntimeError("profile「%s」没有研判档" % prof_name)
    provider = aiplan.find_provider(cfg, conf.get("provider"))
    if provider is None:
        raise RuntimeError("provider 不存在：%r" % conf.get("provider"))
    provider = copy.deepcopy(provider)
    if opts.get("api_base"):
        provider["base_url"] = opts["api_base"]
    model = opts.get("model_pro") or conf.get("model")
    if not model:
        raise RuntimeError("研判档未指定 model")
    key, _ksrc = aiplan.resolve_key(provider, opts.get("api_key"))
    if not key and provider.get("鉴权") != "none":
        raise RuntimeError("未找到 API key（provider=%s）" % provider.get("名称"))
    log("[OK] profile %s（%s）｜ 研判 %s / %s ｜ key %s"
        % (prof_name, src, provider.get("名称"), model, aiplan.mask_key(key)))
    log("[..] call %s (%s) ..." % (model, provider.get("协议")))
    res = aiplan.call_model(
        provider, model, PLANCHECK_SYSTEM, PLANCHECK_PROMPT + "\n\n" + fact,
        temperature=conf.get("temperature") if conf.get("temperature") is not None else 0.3,
        max_tokens=conf.get("max_tokens") or 8000,
        json_mode=bool(provider.get("json_object")), key=key, extra=conf.get("参数"))
    if not res.get("ok"):
        raise RuntimeError("模型调用失败：%s" % res.get("error"))
    usage = res.get("usage") or {}
    cost = aiplan.compute_cost(provider, model, usage, cfg.get("汇率") or {})
    obj, perr = aiplan.extract_json(res.get("text"))
    log("[OK] 模型返回 %d 字符 ｜ latency %sms" % (len(res.get("text") or ""), res.get("latency_ms")))
    log("[OK] usage in %s / out %s ｜ 费用 %s"
        % (usage.get("输入"), usage.get("输出"),
           cost.get("人民币_估算") if cost.get("人民币_估算") is not None else "未配置单价（仅记录 token）"))
    return {"json": obj, "解析错误": perr, "原文": None if obj else (res.get("text") or "")[:20000],
            "profile": prof_name, "profile来源": src, "provider": provider.get("名称"),
            "model": model, "usage": usage, "cost": cost, "latency_ms": res.get("latency_ms")}


def run_plan_check(log, ctl, opts):
    """实盘复核主流程：机械判定 → 一次模型点评 → 落盘。"""
    opts = opts or {}
    data, err = build_plan_check((opts.get("report") or "").strip(), refresh=True)
    if err:
        raise RuntimeError(err)
    r, q, s = data["报告"], data.get("实盘") or {}, data.get("时段") or {}
    log("[OK] 报告 %s（%s · 交易日 %s）"
        % (r["路径"], r.get("phase标签") or r.get("phase"), r.get("trade_date")))
    log("[OK] 当前时段 %s（是否交易日 %s）｜ %s"
        % (s.get("名称"), s.get("是否交易日"), (data.get("关系") or {}).get("文本")))
    if q:
        log("[OK] 实盘价 %s（%s ｜ %s ｜ 抓取 %s）"
            % (q.get("价格"), q.get("来源"), q.get("口径"), q.get("抓取时间")))
    else:
        log("[WARN] 取不到实盘价：机械判定只能给出时段与计划原文")
    for hint in data.get("提示") or []:
        log("[WARN] %s" % hint)
    log("[..] 机械结论：%s" % (data["机械"].get("一句话") or ""))

    payload = {
        "tool": "webui-plancheck", "schema_version": aiplan.SCHEMA_VERSION,
        "generated_at": now_str(), "报告路径": r["路径"], "报告交易日": r.get("trade_date"),
        "标的": r.get("标的"), "时段": data.get("时段"), "关系": data.get("关系"),
        "实盘": data.get("实盘"), "机械": data["机械"], "提示": data.get("提示") or [],
        "点评": None, "error": None, "原文": None,
        "profile": None, "provider": None, "model": None,
        "usage": {}, "cost": {}, "latency_ms": None,
        "note": "本段由大模型基于报告计划与当前实盘价生成，不构成投资建议。",
    }
    cfg, cfg_err = aiplan.load_models(MODELS_PATH)
    if cfg_err:
        payload["error"] = cfg_err
        log("[WARN] 模型层跳过：%s" % cfg_err)
    elif ctl.get("cancel"):
        log("[WARN] 已取消，未落盘")
        return {"__canceled__": True}
    else:
        fact = plancheck_factpack(data)
        log("[..] 事实包 %d 字符" % len(fact))
        payload["事实包"] = fact            # 留档：事后能核对模型当时拿到了哪些数据
        payload["事实包字符数"] = len(fact)
        try:
            layer = plancheck_call_model(log, opts, cfg, data, fact)
        except Exception as e:            # noqa: BLE001  模型失败不静默，但机械结论照常落盘
            layer = {"error": str(e)[:600]}
            log("[WARN] 模型点评失败：%s" % str(e)[:300])
        for key in ("profile", "provider", "model", "usage", "cost", "latency_ms", "原文"):
            payload[key] = layer.get(key)
        payload["评分解析错误"] = layer.get("解析错误")
        if layer.get("error"):
            payload["error"] = layer["error"]
        elif layer.get("json") is None:
            payload["error"] = "模型返回不是合法 JSON：%s" % (layer.get("解析错误") or "未识别")
            log("[WARN] %s" % payload["error"])
        else:
            payload["点评"] = layer["json"]
    if ctl.get("cancel"):
        log("[WARN] 已取消：模型返回后不再落盘")
        return {"__canceled__": True}
    path = save_plan_check(payload)
    log("[OK] 已保存 %s" % rel(path))
    return {"plancheck": rel(path), "点评": payload.get("点评"), "error": payload.get("error"),
            "usage": payload.get("usage"), "cost": payload.get("cost")}
