# -*- coding: utf-8 -*-
"""标的跟踪：每日交易计划 + 执行记录（纯逻辑，不取数、不直接调模型）。

约定与理由
----------
* 产物 ``data/ai/track/<YYYYMMDD>/<HHMMSS>_<代码>_track.json|.md`` 的顶层字段与 aiplan
  报告一致（``研判.json`` 里还是同一套 方向 / 置信度 / 关键价位 / 计划），所以报告页的
  结构化卡片、K 线、机械校验、实盘复核卡不用改一行就能直接打开跟踪产物。
* 执行情况完全由人工录入（同目录 ``*_track_exec.json``）。生成下一份计划时它是**权威事实**；
  机械参考（当日最高/最低有没有碰到计划区间）只用于事后核对，不参与结论。
* 取数、模型调用与编排在 ``track_run.py``；本模块只有纯函数与文件读写，便于单测。
"""

import json
import os
import re
import time
from datetime import date, datetime, timedelta

from . import archive
from . import quotes
from .paths import (ROOT, TRACK_DIR, TRACKLIST_PATH, TRASH_DIR, aiplan, atomic_write, inside,
                    num, now_str, rel)
from .plancheck import calendar_days, is_noop, parse_range, quote_label, session_of
from .planlines import lots_text, report_levels
from .store import load_holdings_bundle, load_tracklist

PHASE = "track"
SCHEMA_VERSION = 1

PLAN_RE = re.compile(r"^(\d{6})_([0-9]{6})_track\.json$")
DAY_RE = re.compile(r"^\d{8}$")

EXEC_STATES = ("已执行", "部分执行", "未执行", "已作废")

MAX_CHARS_DEFAULT = 30000
MAX_CHARS_MIN = 8000
MAX_CHARS_MAX = 200000
STALE_DAYS = 3                  # 最新计划超过几天没更新就在页面上标「计划已过期」

_PLAN_CACHE = {}                # 产物路径 -> (mtime, exec mtime, item)
_EXEC_CACHE = {}                # 执行记录路径 -> (mtime, doc)
_REPORT_LOOKUP = {"ts": 0.0, "map": {}}


# ---------------------------------------------------------------------------
# 纯函数：日期与交易日口径
# ---------------------------------------------------------------------------

def _abs(path):
    return path if os.path.isabs(path) else os.path.join(ROOT, str(path or ""))


abs_path = _abs          # 供 trackview.py 用（相对路径 → 绝对路径）


def _date_of(value):
    m = re.match(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})", str(value or ""))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _next_weekday(day):
    nxt = day + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt


def next_trade_date(days, today):
    """本地交易日历里 today 之后的第一个交易日；没有更晚的日期时返回 None。"""
    if not days:
        return None
    later = sorted(d for d in days if d > today.isoformat())
    return later[0] if later else None


def apply_trade_date(now=None):
    """本次生成的计划「适用哪一个交易日」。

    盘后（≥15:00）或非交易日生成 → 下一交易日；盘前 / 集合竞价 / 盘中 / 午休 → 当天。
    交易日历缺失或没覆盖未来日期时，回退「跳过周末」推算，并把口径写清楚。
    """
    now = now or datetime.now()
    session = session_of(now)
    day = now.date()
    days = calendar_days()
    # 日历只覆盖到今天之前时，session_of 会把「今天」误判成非交易日；那种情况下用
    # 「周一至周五」把今天当交易日看，否则会把盘前生成的计划错指到下一个交易日。
    covered = bool(days) and max(days) >= day.isoformat()
    off_day = (not session["是否交易日"]) and (covered or day.weekday() >= 5)
    after_close = off_day or session["名称"] == "盘后"
    if after_close:
        target = next_trade_date(days, day)
        if target:
            basis = "本地交易日历：%s生成，顺延到下一交易日" % session["名称"]
        else:
            target = _next_weekday(day).isoformat()
            basis = ("本地交易日历不可用：按「跳过周末」推算下一交易日" if not days
                     else "本地交易日历没有更晚的日期：按「跳过周末」推算下一交易日")
    else:
        target = day.isoformat()
        if days and covered:
            basis = "本地交易日历判定今天是交易日：计划适用当日剩余时段"
        elif days:
            basis = ("本地交易日历未覆盖今天（最后一天 %s），按周一至周五判定今天是交易日："
                     "计划适用当日剩余时段" % max(days))
        else:
            basis = "无本地交易日历，按周一至周五判定今天是交易日：计划适用当日剩余时段"
    return {"适用交易日": target, "口径": basis, "时段": session, "现在": session["现在"],
            "盘后": bool(after_close)}


def plan_dir(day):
    return os.path.join(TRACK_DIR, str(day))


def plan_paths(day, stamp, code6):
    base = os.path.join(plan_dir(day), "%s_%s_track" % (stamp, code6))
    return base + ".json", base + ".md"


def exec_path_of(plan_path):
    """计划产物 → 同名执行记录（``..._track.json`` → ``..._track_exec.json``）。"""
    return re.sub(r"_track\.json$", "_track_exec.json", _abs(plan_path))


def latest_pointer_of(code6):
    return os.path.join(TRACK_DIR, "latest_%s.json" % aiplan.code6(code6 or ""))


# ---------------------------------------------------------------------------
# 产物读取与执行记录
# ---------------------------------------------------------------------------

def plan_items(payload):
    """计划条目（编号 = 产物里的下标 + 1，执行记录按编号对齐）。"""
    plan = ((payload or {}).get("研判") or {}).get("json") or {}
    out = []
    for i, raw in enumerate(plan.get("计划") or []):
        if not isinstance(raw, dict):
            continue
        out.append({"编号": i + 1, "优先级": raw.get("优先级"), "动作": raw.get("动作"),
                    "触发条件": raw.get("触发条件"), "价格区间": raw.get("价格区间"),
                    "股数": raw.get("股数"), "金额_元": raw.get("金额_元"),
                    "失效条件": raw.get("失效条件"), "无动作": is_noop(raw.get("动作"))})
    return out


def key_levels(payload, path):
    """计划线（与 /api/overview 里 report_levels 同一实现，避免两套口径）。"""
    return report_levels(path)


def load_exec(plan_path):
    """读该份计划的执行记录；没录入过返回 (None, 预期路径)。"""
    p = exec_path_of(plan_path)
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        return None, p
    hit = _EXEC_CACHE.get(p)
    if hit and hit[0] == mtime:
        return hit[1], p
    doc = aiplan.read_json(p)
    doc = doc if isinstance(doc, dict) else None
    _EXEC_CACHE[p] = (mtime, doc)
    return doc, p


def exec_summary(doc):
    """执行记录 → 页面/总控台用的摘要。"""
    if not isinstance(doc, dict):
        return {"是否录入": False, "说明": "还没有录入执行情况", "总数": 0, "计数": {},
                "完成": False, "录入时间": None, "总体备注": ""}
    rows = doc.get("条目") or []
    counts = {s: 0 for s in EXEC_STATES}
    for r in rows:
        st = str((r or {}).get("执行状态") or "").strip()
        if st in counts:
            counts[st] += 1
    filled = bool(rows) and all(str((r or {}).get("执行状态") or "").strip() in EXEC_STATES
                                for r in rows)
    return {"是否录入": True, "说明": "已录入执行情况", "总数": len(rows), "计数": counts,
            "完成": filled, "录入时间": doc.get("录入时间"),
            "总体备注": doc.get("总体备注") or ""}


def save_exec(plan_path, entries, note=""):
    """写入/更新执行记录。条目按编号与计划对齐，缺的按「未执行」补全。

    纯人工录入：状态只接受 EXEC_STATES 里的四种，成交价/成交股数可以为空。
    """
    p = _abs(plan_path)
    if not inside(p, TRACK_DIR) or not os.path.exists(p):
        return None, "计划产物不存在，或不在 data/ai/track 下"
    payload = aiplan.read_json(p) or {}
    plan_rows = plan_items(payload)
    if not plan_rows:
        return None, "这份计划里没有可录入的条目"
    by_no = {int(r["编号"]): r for r in plan_rows}
    rows, seen = [], set()
    for raw in entries or []:
        if not isinstance(raw, dict):
            continue
        try:
            no = int(raw.get("编号"))
        except (TypeError, ValueError):
            return None, "条目编号必须是整数"
        if no not in by_no:
            return None, "条目编号 %s 不在这份计划里" % no
        state = str(raw.get("执行状态") or "").strip()
        if state and state not in EXEC_STATES:
            return None, "执行状态只能是：%s" % " / ".join(EXEC_STATES)
        src = by_no[no]
        rows.append({"编号": no, "动作": src.get("动作"), "触发条件": src.get("触发条件"),
                     "价格区间": src.get("价格区间"), "计划股数": src.get("股数"),
                     "执行状态": state or "未执行", "成交价": num(raw.get("成交价")),
                     "成交股数": num(raw.get("成交股数")),
                     "备注": str(raw.get("备注") or "").strip()})
        seen.add(no)
    for no in sorted(set(by_no) - seen):
        src = by_no[no]
        rows.append({"编号": no, "动作": src.get("动作"), "触发条件": src.get("触发条件"),
                     "价格区间": src.get("价格区间"), "计划股数": src.get("股数"),
                     "执行状态": "已作废" if src.get("无动作") else "未执行",
                     "成交价": None, "成交股数": None, "备注": ""})
    rows.sort(key=lambda r: r["编号"])
    doc = {"代码": aiplan.code6((payload.get("标的") or {}).get("代码") or ""),
           "名称": (payload.get("标的") or {}).get("名称"),
           "计划路径": rel(p), "适用交易日": payload.get("适用交易日"),
           "录入时间": now_str(), "条目": rows, "总体备注": str(note or "").strip()}
    atomic_write(exec_path_of(p), json.dumps(doc, ensure_ascii=False, indent=1), newline="\n")
    _EXEC_CACHE.pop(exec_path_of(p), None)
    return doc, None


# ---------------------------------------------------------------------------
# 产物列举（清单页 / 总控台合并都用它）
# ---------------------------------------------------------------------------

def plan_item(payload, path):
    """一份跟踪产物 → 与 archive.list_reports() 同形状的「归档条目」。"""
    plan = ((payload.get("研判") or {}).get("json") or {})
    code = aiplan.code6((payload.get("标的") or {}).get("代码") or "")
    day = os.path.basename(os.path.dirname(path))
    stamp = os.path.basename(path)[:6]
    md = re.sub(r"\.json$", ".md", path)
    exec_doc, _exec_path = load_exec(path)
    return {
        "来源": "跟踪",
        "日期": day, "时间戳": stamp,
        "时间": "%s-%s-%s %s:%s:%s" % (day[0:4], day[4:6], day[6:8],
                                      stamp[0:2], stamp[2:4], stamp[4:6]),
        "mtime": os.path.getmtime(path),
        "json路径": rel(path), "md路径": rel(md) if os.path.exists(md) else None,
        "代码": code, "名称": (payload.get("标的") or {}).get("名称"),
        "适用交易日": payload.get("适用交易日"),
        "数据交易日": payload.get("trade_date"),
        "方向": plan.get("方向"), "置信度": plan.get("置信度"),
        "一句话结论": plan.get("一句话结论"),
        "计划条数": len(plan.get("计划") or []),
        "执行": exec_summary(exec_doc),
        "摘要": archive.summarize_payload(payload),
    }


def _cached_item(path):
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    try:
        exec_mtime = os.path.getmtime(exec_path_of(path))
    except OSError:
        exec_mtime = 0.0
    hit = _PLAN_CACHE.get(path)
    if hit and hit[0] == mtime and hit[1] == exec_mtime:
        return hit[2]
    payload = aiplan.read_json(path)
    if not isinstance(payload, dict):
        return None
    item = plan_item(payload, path)
    _PLAN_CACHE[path] = (mtime, exec_mtime, item)
    return item


def list_plans(code=None, limit=None):
    """跟踪产物列表（按 mtime 倒序）。code 为空时返回全部标的。"""
    c6 = aiplan.code6(code or "") if code else None
    out = []
    if not os.path.isdir(TRACK_DIR):
        return out
    for day in os.listdir(TRACK_DIR):
        d = os.path.join(TRACK_DIR, day)
        if not DAY_RE.match(str(day)) or not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            m = PLAN_RE.match(name)
            if not m or (c6 and m.group(2) != c6):
                continue
            item = _cached_item(os.path.join(d, name))
            if item:
                out.append(item)
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out[:int(limit)] if limit else out


def latest_plan_item(code):
    """该标的最新一份跟踪产物条目（没有则 None）。"""
    items = list_plans(code, limit=1)
    return items[0] if items else None


def latest_plan(code):
    item = latest_plan_item(code)
    if not item:
        return None, None
    p = _abs(item["json路径"])
    return aiplan.read_json(p), p


def merged_plan_items():
    """总控台用：跟踪产物的归档条目（与 list_reports() 同形状，可直接合并排序）。"""
    return list_plans()


# ---------------------------------------------------------------------------
# 价格口径与机械参考
# ---------------------------------------------------------------------------

def _fmt(v, nd=2):
    return "—" if v is None else (("%." + str(int(nd)) + "f") % v)


def quote_brief(listed=None, quote=None, doc=None, now=None):
    """现价与来源：东财实时 → 清单缓存价 → 上一份计划里的现价（每一档都标口径）。"""
    q = quote or {}
    label = quote_label((session_of(now) if now else session_of())["名称"])
    if num(q.get("价格")) is not None:
        return {"价格": num(q.get("价格")), "涨跌幅_pct": num(q.get("涨跌幅_pct")),
                "来源": "东财实时行情", "口径": label, "时间": time.strftime("%H:%M:%S"),
                "昨收": num(q.get("昨收")), "今开": num(q.get("今开")),
                "最高": num(q.get("最高")), "最低": num(q.get("最低")),
                "量比": num(q.get("量比")), "换手率_pct": num(q.get("换手率_pct")),
                "成交额": num(q.get("成交额")), "主力净流入": num(q.get("主力净流入")),
                "实时": True}
    price = num((listed or {}).get("现价"))
    if price is not None:
        return {"价格": price, "涨跌幅_pct": num((listed or {}).get("涨跌幅_pct")),
                "来源": "stock3d 缓存（本次没取实时价）", "口径": "最近缓存收盘价",
                "时间": None, "实时": False}
    price = num((doc or {}).get("现价"))
    if price is not None:
        return {"价格": price, "涨跌幅_pct": None, "来源": "上一份计划里的现价",
                "口径": "报告当时价格", "时间": None, "实时": False}
    return {"价格": None, "涨跌幅_pct": None, "来源": "未取到", "口径": "—",
            "时间": None, "实时": False}


def mech_reference(prev_doc, quote):
    """只读机械参考：当日最高/最低与上一份计划的区间有没有相交。

    它只回答「价格碰到没有」，回答不了「用户成交没有」——成交一律以人工录入为准。
    """
    if not prev_doc:
        return {"说明": "没有上一份计划，本次不做机械参考", "计划": [], "关键价位": []}
    hi, lo = num((quote or {}).get("最高")), num((quote or {}).get("最低"))
    rows = []
    for r in plan_items(prev_doc):
        rng = parse_range(r.get("价格区间"))
        if not rng or hi is None or lo is None:
            rows.append({"编号": r["编号"], "动作": r.get("动作"),
                         "区间": list(rng) if rng else None, "状态": "未判定",
                         "说明": "没有当日高低价，或这条没有价格区间"})
            continue
        top, bottom = max(hi, lo), min(hi, lo)
        touched = bottom <= rng[1] and top >= rng[0]
        rows.append({"编号": r["编号"], "动作": r.get("动作"), "区间": [rng[0], rng[1]],
                     "状态": "区间已触及" if touched else "区间未触及",
                     "说明": "当日 [%s, %s] 与计划 [%s, %s] %s"
                             % (_fmt(bottom), _fmt(top), _fmt(rng[0]), _fmt(rng[1]),
                                "相交" if touched else "不相交")})
    levels = ((prev_doc.get("研判") or {}).get("json") or {}).get("关键价位") or {}
    extra = []
    stop = num(levels.get("止损价"))
    if stop is not None and lo is not None and lo <= stop:
        extra.append({"类型": "止损", "价位": stop, "状态": "当日最低已触及",
                      "说明": "最低 %s ≤ 止损 %s" % (_fmt(lo), _fmt(stop))})
    for g in (levels.get("目标位") or []):
        v = num(g.get("价位")) if isinstance(g, dict) else num(g)
        if v is not None and hi is not None and hi >= v:
            extra.append({"类型": "目标", "价位": v, "状态": "当日最高已触及",
                          "说明": "最高 %s ≥ 目标 %s" % (_fmt(hi), _fmt(v))})
    return {"说明": "只读参考：只说明价格有没有触及计划价位，不代表用户是否成交",
            "计划": rows, "关键价位": extra}


# ---------------------------------------------------------------------------
# 事实包（给模型的中性数据；账户 / 上一份计划 / 执行记录永不裁剪）
# ---------------------------------------------------------------------------

def _account_lines(data):
    sym = data.get("标的") or {}
    acc = data.get("账户") or {}
    hold = data.get("持仓") or {}
    out = ["- 标的：%s %s" % (sym.get("名称") or "—", sym.get("代码") or "—"),
           "- 本次计划适用交易日：%s（%s）" % (data.get("适用交易日") or "—",
                                              data.get("交易日口径") or "—")]
    if acc:
        out.append("- 账户：总资金 %s 元 ｜ 单票上限 %s%% ｜ 单笔最大亏损 %s%% ｜ 最低现金比例 %s%%"
                   % (_fmt(acc.get("总资金")), _fmt(acc.get("单票仓位上限_pct")),
                      _fmt(acc.get("单笔最大亏损_pct")), _fmt(acc.get("最低现金比例_pct"))))
    else:
        out.append("- 账户：读不到账户配置，仓位与资金类建议只能写进「数据依赖」")
    if hold and hold.get("是否持仓"):
        out.append("- 持仓：持有 %s 股（可用 %s）｜ 成本 %s ｜ 现价 %s ｜ 市值 %s 元"
                   " ｜ 占总资金 %s%% ｜ 浮动盈亏 %s 元"
                   % (_fmt(hold.get("持有股数"), 0), _fmt(hold.get("可用股数_可卖"), 0),
                      _fmt(hold.get("成本价"), 3), _fmt(hold.get("现价"), 3),
                      _fmt(hold.get("持仓市值_元"), 0), _fmt(hold.get("占总资金_pct")),
                      _fmt(hold.get("浮动盈亏_元"), 0)))
    else:
        out.append("- 持仓：该标的当前不在持仓文件里（按空仓处理）")
    return out


def _quote_lines(data):
    q = data.get("实盘") or {}
    if num(q.get("价格")) is None:
        return ["- 本次没有取到价格（来源：%s）。只能用上一份计划的价格口径，"
                "缺的部分写进「数据依赖」。" % (q.get("来源") or "未取到")]
    out = ["- 现价 %s ｜ 涨跌幅 %s%% ｜ 来源：%s ｜ 口径：%s ｜ 抓取时间：%s"
           % (_fmt(q.get("价格"), 3), _fmt(q.get("涨跌幅_pct")), q.get("来源"), q.get("口径"),
              q.get("时间") or "—")]
    if q.get("实时"):
        out.append("- 昨收 %s ｜ 今开 %s ｜ 当日最高 %s ｜ 当日最低 %s"
                   % (_fmt(q.get("昨收"), 3), _fmt(q.get("今开"), 3), _fmt(q.get("最高"), 3),
                      _fmt(q.get("最低"), 3)))
        out.append("- 量比 %s ｜ 换手率 %s%% ｜ 成交额 %s 元 ｜ 主力净流入 %s 元"
                   % (_fmt(q.get("量比")), _fmt(q.get("换手率_pct")),
                      _fmt(q.get("成交额"), 0), _fmt(q.get("主力净流入"), 0)))
    return out


def _prev_lines(data):
    prev = data.get("上一份计划") or {}
    doc = prev.get("文档")
    if not doc:
        return ["- 这是该标的的首份跟踪计划（没有上一份计划可参考）。"]
    plan = ((doc.get("研判") or {}).get("json") or {})
    out = ["- 来源：%s（%s）｜ 适用交易日：%s ｜ 数据交易日：%s"
           % (prev.get("来源") or "—", prev.get("路径") or "—",
              prev.get("适用交易日") or "—", doc.get("trade_date") or "—"),
           "- 方向：%s ｜ 置信度：%s" % (plan.get("方向") or "—", plan.get("置信度") or "—"),
           "- 一句话结论：%s" % (plan.get("一句话结论") or "—")]
    if prev.get("说明"):
        out.append("- %s" % prev["说明"])
    rows = plan_items(doc)
    out.append("- 计划原文：")
    for r in rows:
        out.append("  %d) %s ｜ 触发条件：%s ｜ 价格区间：%s ｜ 股数：%s ｜ 失效条件：%s"
                   % (r["编号"], r.get("动作") or "—", r.get("触发条件") or "—",
                      r.get("价格区间") or "—", r.get("股数") or "—", r.get("失效条件") or "—"))
    if not rows:
        out.append("  （上一份计划里没有条目）")
    levels = plan.get("关键价位") or {}
    goals = "；".join("%s（%s）" % (_fmt(g.get("价位")), g.get("依据") or "—")
                     for g in (levels.get("目标位") or []) if isinstance(g, dict))
    sup = "、".join(_fmt(g.get("价位")) for g in (levels.get("支撑") or []) if isinstance(g, dict))
    pres = "、".join(_fmt(g.get("价位")) for g in (levels.get("压力") or []) if isinstance(g, dict))
    out.append("- 关键价位：止损 %s ｜ 目标位 %s ｜ 支撑 %s ｜ 压力 %s"
               % (_fmt(levels.get("止损价")), goals or "—", sup or "—", pres or "—"))
    return out


def _exec_lines(data):
    prev = data.get("上一份计划") or {}
    doc = prev.get("执行记录")
    if not isinstance(doc, dict):
        return ["- 本次没有执行记录（%s）——按「未记录」处理，并写进「数据依赖」。"
                % (prev.get("说明") or "新标的的首份计划")]
    out = ["- 录入时间：%s ｜ 总体备注：%s" % (doc.get("录入时间") or "—",
                                              doc.get("总体备注") or "（无）")]
    for r in (doc.get("条目") or []):
        bits = ("  %s) %s ｜ 计划 %s（%s 股）｜ 实际：%s"
                % (r.get("编号"), r.get("动作") or "—", r.get("价格区间") or "—",
                   _fmt(r.get("计划股数"), 0), r.get("执行状态") or "未执行"))
        if num(r.get("成交价")) is not None:
            bits += " ｜ 成交价 %s" % _fmt(r.get("成交价"), 3)
        if num(r.get("成交股数")) is not None:
            bits += " ｜ 成交 %s 股" % _fmt(r.get("成交股数"), 0)
        if (r.get("备注") or "").strip():
            bits += " ｜ 备注：%s" % r["备注"]
        out.append(bits)
    return out


def _mech_lines(data):
    mech = data.get("机械参考") or {}
    rows = mech.get("计划") or []
    if not rows:
        return ["- %s" % (mech.get("说明") or "本次没有机械参考")]
    out = ["- %s" % (mech.get("说明") or "")]
    for r in rows:
        out.append("  %s) %s ｜ 区间 %s ｜ %s ｜ %s"
                   % (r.get("编号"), r.get("动作") or "—", r.get("区间") or "—",
                      r.get("状态") or "—", r.get("说明") or ""))
    for r in (mech.get("关键价位") or []):
        out.append("  %s %s ｜ %s ｜ %s" % (r.get("类型"), _fmt(r.get("价位")),
                                           r.get("状态"), r.get("说明")))
    return out


def factpack_sections(data, cap=MAX_CHARS_DEFAULT, front=None):
    """拼事实包；超上限时先砍背景数据（板块 → 大盘 → 个股形态）。

    front 是调用方补充的高优先章节（如交易流状态），插在最前面、永不裁剪。
    """
    bg = data.get("背景") or {}
    keep = list(front or []) + [("标的与账户", "\n".join(_account_lines(data))),
            ("今日实盘", "\n".join(_quote_lines(data))),
            ("上一份计划（原文）", "\n".join(_prev_lines(data))),
            ("用户录入的执行情况（权威口径）", "\n".join(_exec_lines(data))),
            ("机械参考（只读，仅用于核对）", "\n".join(_mech_lines(data)))]
    tail = []
    for title, key in (("个股量价与形态", "个股量价与形态"), ("大盘", "大盘"), ("板块", "板块")):
        node = bg.get(key)
        if node:
            tail.append((title, json.dumps(node, ensure_ascii=False)))

    def total(secs):
        return sum(len(t) + len(c) + 8 for t, c in secs)

    dropped = []
    while tail and total(keep + tail) > int(cap):
        title, _text = tail.pop()
        dropped.append(title)
    sections = keep + tail
    text = "\n\n".join("【%s】\n%s" % (t, c) for t, c in sections)
    notes = ["- 标的与账户 / 今日实盘 / 上一份计划 / 执行情况 / 机械参考（优先保留）"]
    if dropped:
        notes.append("- 因 %d 字符上限被裁掉的背景数据：%s" % (int(cap), "、".join(dropped)))
    return {"章节": [{"标题": t, "字符数": len(c)} for t, c in sections],
            "保留": [t for t, _c in sections], "裁剪": dropped, "文本": text,
            "字符数": len(text), "上限": int(cap), "说明": notes}


TRACK_SYSTEM = (
    "你是 A 股单标的的每日交易计划员。只依据给定事实数据，为这一只标的写出「适用交易日」可执行的交易计划。"
    "执行情况一律以用户录入为准，不得自行推断是否成交；机械参考只是价格核对，不能当成交结果。"
    "数值只来自给定数据，不编造；缺失写进「数据依赖」。不针对其它标的给建议，不构成投资建议。")

TRACK_PROMPT = (
    "下面是本地程序采集的中性数据：一只标的的账户与持仓、今日实盘、上一份交易计划原文、"
    "用户录入的上一份计划执行情况、机械参考，以及个股量价形态与大盘/板块背景。\n\n"
    "请只依据这些数据，输出**一个 JSON 对象**（不要解释文字、不要代码块围栏），字段固定为：\n"
    "{\n"
    ' "方向": "偏多|中性|偏空",\n'
    ' "置信度": 0 到 100 的整数,\n'
    ' "时间窗": "如 3-10 个交易日",\n'
    ' "一句话结论": "不超过 80 字，带价位",\n'
    ' "情景树": [{"情形": "乐观|中性|悲观", "概率_pct": 0-100, "路径": "触发什么→怎么走",\n'
    '             "验证信号": ["可在盘面观察到的信号"], "失效条件": "什么情况下该情景作废"}],\n'
    ' "关键价位": {"支撑": [{"价位": 1.19, "依据": "如 MA20 / 近20日低点 / 缺口下沿"}],\n'
    '             "压力": [{"价位": 1.25, "依据": "..."}], "止损价": 1.19,\n'
    '             "目标位": [{"价位": 1.22, "依据": "..."}]},\n'
    ' "计划": [{"动作": "建仓|加仓|减仓|清仓|止损", "触发条件": "价格或盘面条件（不要写时间点）",\n'
    '          "价格区间": [下限, 上限], "股数": 100 的整数倍整数或 null,\n'
    '          "金额_元": 数字或 null, "失效条件": "...", "优先级": 1-5 的整数}],\n'
    ' "仓位": {"当前_pct": 数字, "建议_pct": 数字, "上限_pct": 数字, "现金保留_pct": 数字},\n'
    ' "风险": [{"风险": "...", "监控指标": "事实包里的哪个数据能提前预警", "应对": "..."}],\n'
    ' "数据依赖": {"降级项": ["事实包里 DEGRADE / FAIL / 缺失的项"], "缺失导致的不确定性": "..."},\n'
    ' "计划校正": {"作废条目": [], "提示": []}\n'
    "}\n\n"
    "要求：\n"
    "1. 「计划」只写这一只标的的动作，2 到 4 条；用 A 股常用动作名（建仓/加仓/减仓/清仓/止损），"
    "不要写「观望」「持有」这类没有执行动作的条目——标的已经在跟踪清单里，本身就代表在跟踪。\n"
    "2. 必须基于「用户录入的执行情况」决定下一步：已执行的按成交价与股数更新后续动作与仓位；"
    "未执行的写清继续等待、改价还是放弃；部分执行的写清剩余部分怎么办。"
    "禁止把「机械参考」当成成交结果——它只说明价格有没有触及。\n"
    "3. 每条都要有可核对的「触发条件」（价格 / 量比 / 均线 / 板块资金等）与明确的「失效条件」。\n"
    "4. 「止损价」是收盘口径的离场价，必须 ≤ 买入类计划的触发价；买入类计划的区间下沿要 ≥ 止损价；"
    "「仓位.建议_pct」要与计划股数自洽（计划股数 × 现价 ÷ 总资金，误差 ≤ 5 个百分点）。\n"
    "5. 数据缺失、取不到实时价、上一份计划已过期等情况，写进「数据依赖」，不要编造数值。\n"
    "6. 只针对这一只标的，不构成投资建议。")


def normalize_plan(obj):
    """把模型输出拉回契约形状（复用 aiplan.normalize_research，再补计划校正）。"""
    out = aiplan.normalize_research(obj) if isinstance(obj, dict) else obj
    if isinstance(out, dict):
        corr = out.get("计划校正")
        if not isinstance(corr, dict):
            corr = {}
        corr.setdefault("作废条目", [])
        corr.setdefault("提示", [])
        out["计划校正"] = corr
    return out


# ---------------------------------------------------------------------------
# Markdown 与落盘
# ---------------------------------------------------------------------------

def render_md(payload):
    """人读版 Markdown（只用事实与计划，不写投资建议）。"""
    sym = payload.get("标的") or {}
    plan = ((payload.get("研判") or {}).get("json") or {})
    prev = payload.get("上次计划") or {}
    ex = payload.get("执行记录摘要") or {}
    q = payload.get("实时") or {}
    lines = ["# 标的跟踪 · %s（%s）" % (sym.get("名称") or "—", sym.get("代码") or "—"), "",
             "- 适用交易日：%s ｜ 数据交易日：%s" % (payload.get("适用交易日") or "—",
                                                   payload.get("trade_date") or "—"),
             "- 生成时间：%s ｜ profile：%s" % (payload.get("generated_at") or "—",
                                               (payload.get("profile") or {}).get("名称") or "—"),
             "- 现价：%s ｜ 涨跌幅：%s%% ｜ 来源：%s（%s）"
             % (_fmt(q.get("价格"), 3), _fmt(q.get("涨跌幅_pct")), q.get("来源") or "—",
                q.get("口径") or "—"),
             "- 方向：%s ｜ 置信度：%s" % (plan.get("方向") or "—", plan.get("置信度") or "—"),
             "- 一句话结论：%s" % (plan.get("一句话结论") or "—"), ""]
    lines += ["## 1 上一份计划与执行情况", "",
              "- 上一份来源：%s（%s）｜ 适用交易日：%s"
              % (prev.get("来源") or "—", prev.get("路径") or "—", prev.get("适用交易日") or "—"),
              "- 执行记录：%s" % ("已录入（%s）" % (ex.get("录入时间") or "—")
                                  if ex.get("是否录入") else (prev.get("说明") or "未录入")), ""]
    for r in (prev.get("执行记录") or {}).get("条目") or []:
        lines.append("- %s) %s ｜ 计划 %s（%s 股）｜ 实际：%s%s"
                     % (r.get("编号"), r.get("动作") or "—", r.get("价格区间") or "—",
                        _fmt(r.get("计划股数"), 0), r.get("执行状态") or "未执行",
                        (" ｜ 成交价 %s ｜ 成交 %s 股" % (_fmt(r.get("成交价"), 3),
                                                      _fmt(r.get("成交股数"), 0)))
                        if num(r.get("成交价")) is not None or num(r.get("成交股数")) is not None
                        else ""))
    if (prev.get("执行记录") or {}).get("总体备注"):
        lines.append("- 总体备注：%s" % prev["执行记录"]["总体备注"])
    lines += ["", "## 2 本次关键价位", ""]
    levels = plan.get("关键价位") or {}
    lines.append("- 止损价：%s" % _fmt(levels.get("止损价")))
    for key in ("目标位", "支撑", "压力"):
        for g in (levels.get(key) or []):
            if isinstance(g, dict):
                lines.append("- %s：%s（%s）" % (key, _fmt(g.get("价位")), g.get("依据") or "—"))
    lines += ["", "## 3 本次交易计划", ""]
    for r in plan_items(payload):
        lines.append("### %s. %s" % (r["编号"], r.get("动作") or "—"))
        lines.append("- 触发条件：%s" % (r.get("触发条件") or "—"))
        lines.append("- 价格区间：%s ｜ 股数：%s ｜ 金额：%s 元"
                     % (r.get("价格区间") or "—", _fmt(r.get("股数"), 0), _fmt(r.get("金额_元"), 0)))
        lines.append("- 失效条件：%s" % (r.get("失效条件") or "—"))
        lines.append("")
    if not plan_items(payload):
        lines.append("（本次没有产出可执行的计划条目）")
    lines += ["", "## 4 情景树", ""]
    for s in (plan.get("情景树") or []):
        lines.append("- %s" % (s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)))
    lines += ["", "## 5 风险与数据依赖", ""]
    for s in (plan.get("风险") or []):
        lines.append("- 风险：%s" % (s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)))
    dep = plan.get("数据依赖")
    lines.append("- 数据依赖：%s" % (json.dumps(dep, ensure_ascii=False) if dep else "—"))
    mech = payload.get("机械参考") or {}
    if mech.get("计划"):
        lines += ["", "## 6 机械参考（只读）", "", "- %s" % (mech.get("说明") or "")]
        for r in mech["计划"]:
            lines.append("- %s) %s ｜ %s ｜ %s" % (r.get("编号"), r.get("动作") or "—",
                                                  r.get("状态") or "—", r.get("说明") or ""))
    cost = (payload.get("研判") or {}).get("cost") or {}
    usage = (payload.get("研判") or {}).get("usage") or {}
    lines += ["", "## 7 费用与降级", "",
              "- usage：in %s / out %s" % (usage.get("输入"), usage.get("输出")),
              "- 费用：%s" % (cost.get("人民币_估算") if cost.get("人民币_估算") is not None
                             else "未配置单价（仅记录 token）")]
    for h in payload.get("降级") or []:
        lines.append("- 降级：%s" % h)
    lines += ["", "---", "", "本页由本地程序采集数据 + 大模型生成，不构成投资建议。"]
    return "\n".join(lines)


def save_plan(payload, md_text):
    """落盘 JSON + Markdown + latest 指针，返回 JSON 路径。"""
    code6 = aiplan.code6((payload.get("标的") or {}).get("代码") or "")
    day = str(payload.get("trade_date") or datetime.now().strftime("%Y-%m-%d")).replace("-", "")
    stamp = datetime.now().strftime("%H%M%S")
    js, mdp = plan_paths(day, stamp, code6)
    while os.path.exists(js):        # 同一秒内第二次生成：时间戳顺延一秒，保持文件名可被正则识别
        stamp = "%06d" % ((int(stamp) + 1) % 1000000)
        js, mdp = plan_paths(day, stamp, code6)
    body = json.dumps(payload, ensure_ascii=False, indent=1)
    atomic_write(js, body, newline="\n")
    atomic_write(mdp, md_text, newline="\n")
    pointer = dict(payload)
    pointer["产物路径"] = rel(js)
    atomic_write(latest_pointer_of(code6), json.dumps(pointer, ensure_ascii=False, indent=1),
                 newline="\n")
    _PLAN_CACHE.pop(js, None)
    return js


def delete_plan(path):
    """把跟踪产物（JSON + MD + 执行记录）移入回收站：只移动，可恢复，7 天后才真删。"""
    p = _abs(path)
    if not inside(p, TRACK_DIR) or not os.path.exists(p):
        return None, "产物不存在，或不在 data/ai/track 下"
    day = os.path.basename(os.path.dirname(p))
    dest = os.path.join(TRASH_DIR, day)
    os.makedirs(dest, exist_ok=True)
    moved = []
    targets = [p, re.sub(r"\.json$", ".md", p), exec_path_of(p)]
    pointer = latest_pointer_of(aiplan.code6(os.path.basename(p).split("_")[1]))
    if os.path.exists(pointer):
        doc = aiplan.read_json(pointer) or {}
        if doc.get("产物路径") == rel(p):
            targets.append(pointer)
    for src in targets:
        if not os.path.exists(src):
            continue
        dst = os.path.join(dest, os.path.basename(src))
        if os.path.exists(dst):
            os.replace(dst, dst + ".replaced")
        os.replace(src, dst)
        moved.append(rel(dst))
    if not moved:
        return None, "没有找到可移除的文件"
    _PLAN_CACHE.pop(p, None)
    _EXEC_CACHE.pop(exec_path_of(p), None)
    return {"移入": moved, "回收站": rel(dest),
            "说明": "已移入回收站，可从「历史与复盘」页搬回；超过 7 天会被真正删除。"}, None


def _newest_report_for(code):
    """该标的最近一份手工研判报告（同一标的）；负结果也缓存 60 秒。"""
    c6 = aiplan.code6(code or "")
    if not c6:
        return None, None
    now = time.time()
    if now - _REPORT_LOOKUP["ts"] > 60.0:
        _REPORT_LOOKUP["map"].clear()
        _REPORT_LOOKUP["ts"] = now
    if c6 in _REPORT_LOOKUP["map"]:
        return _REPORT_LOOKUP["map"][c6]
    paths = [p for p in archive.ai_files(r"^\d{6}_[a-z]+\.json$") if os.path.exists(p)]
    paths.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    found = (None, None)
    for p in paths[:60]:
        doc = aiplan.read_json(p)
        if isinstance(doc, dict) and aiplan.code6((doc.get("标的") or {}).get("代码") or "") == c6:
            found = (doc, p)
            break
    _REPORT_LOOKUP["map"][c6] = found
    return found


def reference_prev(code, apply_date):
    """上一份参考计划：跟踪产物优先；没有就把最近一份手工研判报告当参考。

    「需要执行记录」= 上一份是跟踪产物 + 它的适用交易日早于本次适用交易日 + 还没有执行记录。
    同日重跑不要求（那天还没发生），手工报告也不要求（它本来就没有执行记录）。
    """
    item = latest_plan_item(code)
    if item:
        p = _abs(item["json路径"])
        doc = aiplan.read_json(p) or {}
        prev_date = str(item.get("适用交易日") or item.get("数据交易日") or "")
        exec_doc, exec_p = load_exec(p)
        return {"来源": "跟踪产物", "路径": rel(p), "文档": doc, "适用交易日": prev_date,
                "执行记录": exec_doc, "执行记录路径": rel(exec_p),
                "执行摘要": exec_summary(exec_doc),
                "需要执行记录": bool(prev_date) and prev_date < str(apply_date) and not exec_doc,
                "说明": None}
    doc, p = _newest_report_for(code)
    if doc:
        return {"来源": "手工研判报告", "路径": rel(p), "文档": doc,
                "适用交易日": doc.get("trade_date"), "执行记录": None,
                "执行记录路径": None, "执行摘要": exec_summary(None),
                "需要执行记录": False,
                "说明": "上一份计划来自手工研判报告，没有执行记录（本次不强制录入）"}
    return {"来源": None, "路径": None, "文档": None, "适用交易日": None,
            "执行记录": None, "执行记录路径": None, "执行摘要": exec_summary(None),
            "需要执行记录": False, "说明": "这是该标的的首份跟踪计划"}
