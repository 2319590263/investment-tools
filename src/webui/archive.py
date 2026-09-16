# -*- coding: utf-8 -*-
"""报告归档、plan_log、荐股产物的读取 / 列举 / 删除入口。"""

import json
import os
import re
import time

from .paths import AI_DIR, MAX_REPORTS, PHASE_LABEL, PICK_DIR, ROOT, TRASH_DIR, aiplan, atomic_write, inside, read_bytes, read_text, rel
from .sources import newest_file


def summarize_payload(payload):
    plan = (payload.get("研判") or {}).get("json") or {}
    review = (payload.get("复核") or {}).get("json") or {}
    checks = payload.get("机械校验") or []
    counters = {"通过": 0, "违规": 0, "提示": 0}
    for c in checks:
        r = c.get("结果") if isinstance(c, dict) else None
        if r in counters:
            counters[r] += 1
    cost_pro = ((payload.get("研判") or {}).get("cost") or {}).get("人民币_估算")
    cost_rev = ((payload.get("复核") or {}).get("cost") or {}).get("人民币_估算")
    total_cost = None
    if cost_pro is not None or cost_rev is not None:
        total_cost = round((cost_pro or 0) + (cost_rev or 0), 4)
    return {
        "标的代码": (payload.get("标的") or {}).get("代码"),
        "标的名称": (payload.get("标的") or {}).get("名称"),
        "类型": (payload.get("标的") or {}).get("类型"),
        "方向": plan.get("方向"),
        "置信度": plan.get("置信度"),
        "时间窗": plan.get("时间窗"),
        "一句话结论": plan.get("一句话结论"),
        "现价": payload.get("现价"),
        "研判模型": (payload.get("研判") or {}).get("model"),
        "复核模型": (payload.get("复核") or {}).get("model"),
        "研判ok": bool((payload.get("研判") or {}).get("ok")),
        "复核ok": bool((payload.get("复核") or {}).get("ok")),
        "复核推翻": review.get("是否推翻结论"),
        "校验统计": counters,
        "费用_元": total_cost,
        "usage": {
            "研判_输入": ((payload.get("研判") or {}).get("usage") or {}).get("输入"),
            "研判_输出": ((payload.get("研判") or {}).get("usage") or {}).get("输出"),
            "复核_输入": ((payload.get("复核") or {}).get("usage") or {}).get("输入"),
            "复核_输出": ((payload.get("复核") or {}).get("usage") or {}).get("输出"),
        },
        "profile": (payload.get("profile") or {}).get("名称"),
        "交易日": payload.get("trade_date"),
        "生成时间": payload.get("generated_at"),
        # 批注 2：报告头要显示「用的是哪天的数据」；批注 1：复核跳过的原因
        "数据日期": (payload.get("数据") or {}).get("事实包日期"),
        "复核跳过原因": ((payload.get("复核") or {}).get("跳过原因")
                    or (payload.get("复核") or {}).get("error")),
        "note": payload.get("note"),
    }


def list_reports():
    """扫描 data/ai/<YYYYMMDD>/ 下的带时间戳报告（HHMMSS_phase.json）。"""
    out = []
    if not os.path.isdir(AI_DIR):
        return out
    for date_dir in os.listdir(AI_DIR):
        if not re.match(r"^\d{8}$", date_dir):
            continue
        d = os.path.join(AI_DIR, date_dir)
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            m = re.match(r"^(\d{6})_([a-z]+)\.json$", name)
            if not m:
                continue
            stamp, phase = m.group(1), m.group(2)
            js = os.path.join(d, name)
            md = os.path.join(d, "%s_%s.md" % (stamp, phase))
            try:
                st = os.stat(js)
            except OSError:
                continue
            out.append({
                "日期": date_dir,
                "phase": phase,
                "phase标签": PHASE_LABEL.get(phase, phase),
                "时间戳": stamp,
                "时间": "%s-%s-%s %s:%s:%s" % (date_dir[0:4], date_dir[4:6], date_dir[6:8],
                                             stamp[0:2], stamp[2:4], stamp[4:6]),
                "mtime": st.st_mtime,
                "大小": st.st_size,
                "json路径": rel(js),
                "md路径": rel(md) if os.path.exists(md) else None,
                "有md": os.path.exists(md),
                "摘要": None,
            })
    out.sort(key=lambda x: x["mtime"], reverse=True)
    out = out[:MAX_REPORTS]
    for item in out:
        js = os.path.join(ROOT, item["json路径"])
        payload = aiplan.read_json(js)
        if isinstance(payload, dict):
            try:
                item["摘要"] = summarize_payload(payload)
            except Exception:
                item["摘要"] = None
    session = None
    try:
        from .plancheck import session_of
        session = session_of()
    except Exception:            # noqa: BLE001  日历读不到就只按天数判定
        session = None
    for item in out:
        item.update(report_staleness(item, session))
    return out


def ai_files(regex):
    """扫 data/ai/<YYYYMMDD>/ 下所有匹配 regex 的文件（不限日期）。"""
    out = []
    if not os.path.isdir(AI_DIR):
        return out
    rx = re.compile(regex)
    for date_dir in os.listdir(AI_DIR):
        d = os.path.join(AI_DIR, date_dir)
        if not re.match(r"^\d{8}$", date_dir) or not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if rx.match(name):
                out.append(os.path.join(d, name))
    return out


def latest_report(phase=None):
    """按 mtime 找最新一份报告。优先带时间戳的产物（页面下拉里也是这些），
    没有时才回落 data/ai/<date>/latest_<phase>.json。"""
    pat = (r"^\d{6}_%s\.json$" % phase) if phase else r"^\d{6}_[a-z]+\.json$"
    path = newest_file(ai_files(pat))
    if path:
        return path
    lpat = (r"^latest_%s\.json$" % phase) if phase else r"^latest_[a-z]+\.json$"
    return newest_file(ai_files(lpat))


STALE_DAYS = 3               # 报告超过几天算「过时」（页面提示重新生成）


def report_staleness(item, session=None):
    """报告是否过时：生成超过 STALE_DAYS 天，或交易日已经走到报告之后。"""
    mtime = item.get("mtime")
    days = None
    if mtime:
        days = int(max(0, (time.time() - mtime) // 86400))
    trade_day = str(((item.get("摘要") or {}).get("交易日") or "")).strip()
    today = str((session or {}).get("现在") or "")[:10]
    trading = bool((session or {}).get("是否交易日"))
    stale, why = False, ""
    if trade_day and today and trading and trade_day < today:
        stale, why = True, "报告交易日 %s 早于今天（今天开市）" % trade_day
    if days is not None and days >= STALE_DAYS:
        stale = True
        why = why or ("报告生成于 %d 天前" % days)
    return {"过期": stale, "过期说明": why, "天数": days}


PRUNE_INTERVAL = 60.0        # 同进程最多 60 秒扫一次盘

_PRUNE_TS = [0.0]


def prune_reports(keep_per_code=1, force=False, log=None):
    """每个标的只保留最新一份报告，更早的（json/md/prompt/factpack）移入回收站。

    属于维护动作：同进程 60 秒最多扫一次；只移动不删除，回收站 7 天后才真删，
    需要时可以从回收站搬回原位。返回 {检查, 标的, 移入, 明细}。
    """
    now = time.time()
    if not force and (now - _PRUNE_TS[0]) < PRUNE_INTERVAL:
        return {"跳过": True, "原因": "同进程 %d 秒内已清理过" % int(PRUNE_INTERVAL)}
    _PRUNE_TS[0] = now
    groups = {}
    for path in ai_files(r"^\d{6}_[a-z]+\.json$"):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        doc = aiplan.read_json(path) or {}
        code = aiplan.code6(((doc.get("标的") or {}).get("代码")) or "")
        key = code or ("__" + os.path.basename(path))
        groups.setdefault(key, []).append((mtime, path))
    moved, detail = [], []
    for key, rows in sorted(groups.items()):
        rows.sort(reverse=True)
        for _mtime, path in rows[keep_per_code:]:
            res, err = delete_report(path)
            if res:
                moved.extend(res.get("移入") or [])
                detail.append({"标的": key, "报告": rel(path)})
                if log:
                    log("[OK] 旧报告移入回收站：%s（每个标的只留最新一份）" % rel(path))
            elif log:
                log("[WARN] 旧报告清理失败：%s（%s）" % (rel(path), err))
    return {"检查": sum(len(v) for v in groups.values()), "标的": len(groups),
            "移入": moved, "明细": detail}


def delete_report(path):
    """把一份报告连同它的 md / prompt / factpack 一起移入 data/ai/.trash/<日期>/。
    只做移动，不做物理删除，随时可以手动搬回来。返回 (结果, 错误)。"""
    if not path:
        return None, "缺少 path"
    p = path if os.path.isabs(path) else os.path.join(ROOT, path)
    p = os.path.abspath(p)
    if not inside(p, AI_DIR) or not os.path.exists(p):
        return None, "报告不存在，或不在 data/ai 目录下"
    m = re.match(r"^(\d{6})_([a-z]+)\.json$", os.path.basename(p))
    if not m:
        return None, "只支持删除带时间戳的报告（HHMMSS_phase.json）"
    stamp, phase = m.group(1), m.group(2)
    base = re.sub(r"\.json$", "", p)
    date_dir = os.path.basename(os.path.dirname(p))
    dest_dir = os.path.join(TRASH_DIR, date_dir)
    os.makedirs(dest_dir, exist_ok=True)

    moved = []
    targets = [base + ".json", base + ".md", base + ".prompt.txt", base + ".factpack.md"]
    # 若 latest_<phase> 指针指向的就是这一份，一起收走，避免留下指向已删报告的指针
    for ext in ("json", "md"):
        latest = os.path.join(os.path.dirname(p), "latest_%s.%s" % (phase, ext))
        src_same = base + "." + ext
        if os.path.exists(latest) and os.path.exists(src_same):
            if read_bytes(latest) == read_bytes(src_same):
                targets.append(latest)
    for src in targets:
        if not os.path.exists(src):
            continue
        dst = os.path.join(dest_dir, os.path.basename(src))
        if os.path.exists(dst):
            os.replace(dst, dst + ".replaced")
        os.replace(src, dst)
        moved.append(rel(dst))
    if not moved:
        return None, "没有找到可移除的文件"
    return {"移入": moved, "回收站": rel(dest_dir),
            "说明": "文件已移入回收站，需要时可直接从该目录搬回原位。"}, None


def report_bundle(path):
    if not path:
        return None
    if not os.path.isabs(path):
        path = os.path.join(ROOT, path)
    path = os.path.abspath(path)
    if not inside(path, AI_DIR) or not os.path.exists(path):
        return None
    payload = aiplan.read_json(path)
    base = re.sub(r"\.json$", "", path)
    md_path = base + ".md"
    prompt_path = base + ".prompt.txt"
    factpack_path = base + ".factpack.md"
    md_text = read_text(md_path) if os.path.exists(md_path) else None
    return {
        "json路径": rel(path),
        "md路径": rel(md_path) if md_text is not None else None,
        "prompt路径": rel(prompt_path) if os.path.exists(prompt_path) else None,
        "factpack路径": rel(factpack_path) if os.path.exists(factpack_path) else None,
        "摘要": summarize_payload(payload) if isinstance(payload, dict) else None,
        "json": payload,
        "md": md_text,
        "mtime": os.path.getmtime(path),
    }


def plan_log_entries():
    path = aiplan.PLAN_LOG
    out = []
    if not os.path.exists(path):
        return out
    for idx, raw in enumerate(read_text(path).splitlines()):
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if isinstance(rec, dict):
            rec["_报告存在"] = bool(rec.get("报告") and os.path.exists(rec["报告"]))
            rec["_报告相对"] = rel(rec["报告"]) if rec.get("报告") else None
            rec["_行号"] = idx
            out.append(rec)
    out.reverse()
    return out


def delete_plan_log(line_no):
    """删除 plan_log.jsonl 的第 line_no 行（0 基，文件顺序）。先备份再重写。"""
    path = aiplan.PLAN_LOG
    if not os.path.exists(path):
        return False, "plan_log 不存在"
    try:
        line_no = int(line_no)
    except (TypeError, ValueError):
        return False, "行号必须是整数"
    lines = read_text(path).splitlines()
    if not (0 <= line_no < len(lines)):
        return False, "行号越界：%s（共 %d 行）" % (line_no, len(lines))
    # 删前的完整快照进回收站：累计保存，不会被下一次删除覆盖
    hist_dir = os.path.join(TRASH_DIR, "history")
    os.makedirs(hist_dir, exist_ok=True)
    stamp = aiplan.now_local().strftime("%Y%m%d%H%M%S")
    snapshot = os.path.join(hist_dir, "plan_log_%s.jsonl" % stamp)
    atomic_write(snapshot, read_bytes(path), newline="")
    keep = [l for i, l in enumerate(lines) if i != line_no]
    atomic_write(path, ("\n".join(keep) + "\n") if keep else "", newline="")
    return True, rel(snapshot)


def save_pick(payload, md_text):
    """落 data/ai/pick/<YYYYMMDD>/<HHMMSS>_pick.json + .md，并写 latest 指针。"""
    d = os.path.join(PICK_DIR, aiplan.now_local().strftime("%Y%m%d"))
    os.makedirs(d, exist_ok=True)
    body = json.dumps(payload, ensure_ascii=False, indent=1)
    base = os.path.join(d, "%s_pick" % aiplan.now_local().strftime("%H%M%S"))
    atomic_write(base + ".json", body, newline="\n")
    atomic_write(base + ".md", md_text, newline="\n")
    atomic_write(os.path.join(d, "latest_pick.json"), body, newline="\n")
    atomic_write(os.path.join(d, "latest_pick.md"), md_text, newline="\n")
    return base + ".json", base + ".md"


def pick_paths():
    out = []
    if not os.path.isdir(PICK_DIR):
        return out
    for day in os.listdir(PICK_DIR):
        d = os.path.join(PICK_DIR, day)
        if not re.match(r"^\d{8}$", day) or not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if re.match(r"^\d{6}_pick\.json$", name):
                out.append(os.path.join(d, name))
    return out


def latest_pick_path():
    return newest_file(pick_paths())


def pick_bundle(path):
    """读一份荐股结果（json + md）。"""
    if not path:
        return None
    p = path if os.path.isabs(path) else os.path.join(ROOT, path)
    p = os.path.abspath(p)
    if not inside(p, PICK_DIR) or not os.path.exists(p):
        return None
    md = re.sub(r"\.json$", ".md", p)
    return {
        "json路径": rel(p),
        "md路径": rel(md) if os.path.exists(md) else None,
        "md": read_text(md) if os.path.exists(md) else None,
        "mtime": os.path.getmtime(p),
        "json": aiplan.read_json(p),
    }


def latest_pick_bundle():
    p = latest_pick_path()
    if not p:
        return {"json路径": None, "md路径": None, "md": None, "mtime": None, "json": None}
    return pick_bundle(p) or {"json路径": None, "md路径": None, "md": None,
                              "mtime": None, "json": None}


def list_picks(limit=40):
    """荐股历史（按 mtime 倒序，最多扫描 limit 份）。"""
    paths = [p for p in pick_paths() if os.path.exists(p)]
    paths.sort(key=lambda p: os.stat(p).st_mtime, reverse=True)
    out = []
    for p in paths[:limit]:
        doc = aiplan.read_json(p) or {}
        model = doc.get("模型层") or {}
        summary = doc.get("总评") or {}
        boards = doc.get("板块") or {}
        cands = doc.get("候选") or {}
        cost = (doc.get("成本") or {}).get("人民币_估算")
        out.append({
            "json路径": rel(p),
            "md路径": rel(re.sub(r"\.json$", ".md", p)),
            "日期": doc.get("日期") or os.path.basename(os.path.dirname(p)),
            "时间": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(p))),
            "生成时间": doc.get("生成时间"),
            "模块": (doc.get("参数") or {}).get("模块") or [],
            "板块数": sum(len(v or []) for v in boards.values()),
            "候选数": sum(len(v or []) for v in cands.values()),
            "模型": model.get("model"), "费用": cost,
            "评级": summary.get("多空倾向"), "结论": summary.get("一句话"),
            "已点评": bool(model.get("json")), "模型错误": model.get("error"),
        })
    return out


def delete_pick(path):
    """把一份荐股结果移入回收站（受 7 天过期约束）。"""
    if not path:
        return None, "缺少 path"
    p = path if os.path.isabs(path) else os.path.join(ROOT, path)
    p = os.path.abspath(p)
    if not inside(p, PICK_DIR) or not os.path.exists(p):
        return None, "荐股结果不存在，或不在 data/ai/pick 下"
    if not re.match(r"^\d{6}_pick\.json$", os.path.basename(p)):
        return None, "只支持删除 HHMMSS_pick.json"
    day_dir = os.path.dirname(p)
    dest_dir = os.path.join(TRASH_DIR, os.path.basename(day_dir))
    os.makedirs(dest_dir, exist_ok=True)
    base = re.sub(r"\.json$", "", p)
    targets = [base + ".json", base + ".md"]
    for ext in ("json", "md"):
        latest = os.path.join(day_dir, "latest_pick.%s" % ext)
        src_same = base + "." + ext
        if os.path.exists(latest) and os.path.exists(src_same):
            if read_bytes(latest) == read_bytes(src_same):
                targets.append(latest)
    moved = []
    for src in targets:
        if not os.path.exists(src):
            continue
        dst = os.path.join(dest_dir, os.path.basename(src))
        if os.path.exists(dst):
            os.replace(dst, dst + ".replaced")
        os.replace(src, dst)
        # 记下原路径：荐股结果在 data/ai/pick/<日期>/ 下，恢复时要搬回原位
        atomic_write(dst + ".origin", rel(src), newline="\n")
        moved.append(rel(dst))
    if not moved:
        return None, "没有找到可移除的文件"
    return {"移入": moved, "回收站": rel(dest_dir)}, None
