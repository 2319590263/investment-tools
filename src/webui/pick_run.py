# -*- coding: utf-8 -*-
"""荐股取数与编排：东财板块/成分抓取、缓存、模型点评、run_pick。"""

from datetime import datetime
import copy
import json
import os
import time

from .archive import save_pick
from .paths import MODELS_PATH, PAN_DIR, aiplan, atomic_write, pan, rel
from .pick import PICK_BOARD_FIELDS, PICK_BOARD_TYPES, PICK_CACHE_DIR, PICK_CACHE_TTL, PICK_CAND_SCAN, PICK_CONCEPT_THEMES, PICK_L1_INDUSTRIES, PICK_MAX_CANDIDATES, PICK_MAX_CONCEPTS, PICK_MEM_TTL, PICK_PAGE_TOP, PICK_PER_BOARD, PICK_PROMPT, PICK_STOCK_FIELDS, PICK_SYSTEM, PICK_THEME_OTHER, pick_board_view, pick_concept_theme, pick_factpack, pick_filter, pick_markdown, pick_pct, pick_score_boards, pick_score_stocks, pick_module_picks, pick_stock_view, pick_wsum
from .sources import newest_file, pan_files


def pick_em_list(ctx, fs, fields, limit, sort="f3"):
    """东财 clist 首页（按 sort 降序取前 limit 条）。失败返回 None。"""
    return pick_em_page(ctx, fs, fields, pn=1, pz=limit, sort=sort)


def pick_em_page(ctx, fs, fields, pn=1, pz=100, sort="f12"):
    """东财 clist 单页（失败返回 None）。"""
    d = pan._em_json(ctx, pan.EM_CLIST_PATH, {
        "pn": str(max(1, int(pn))), "pz": str(max(1, min(100, int(pz)))), "po": "1",
        "np": "1", "fltt": "2", "invt": "2", "fid": sort, "fs": fs, "fields": fields})
    if not d:
        return None
    batch = (d.get("data") or {}).get("diff") or []
    if isinstance(batch, dict):
        batch = list(batch.values())
    return [x for x in batch if isinstance(x, dict)]


_PICK_MEM = {}


def pick_cache_get(key, refresh=False, ttl=PICK_MEM_TTL):
    """进程内缓存（refresh=True 时无视缓存）。"""
    if refresh:
        return None
    hit = _PICK_MEM.get(key)
    if hit and (time.time() - hit[0]) < ttl:
        return hit[1]
    return None


def pick_cache_put(key, value):
    _PICK_MEM[key] = (time.time(), value)
    return value


def pick_disk_read(name, ttl=PICK_CACHE_TTL):
    p = os.path.join(PICK_CACHE_DIR, name)
    if not os.path.exists(p):
        return None
    try:
        if (time.time() - os.stat(p).st_mtime) > ttl:
            return None
    except OSError:
        return None
    doc = aiplan.read_json(p)
    return doc if isinstance(doc, dict) else None


def pick_disk_write(name, obj):
    try:
        os.makedirs(PICK_CACHE_DIR, exist_ok=True)
        atomic_write(os.path.join(PICK_CACHE_DIR, name),
                     json.dumps(obj, ensure_ascii=False), newline="\n")
    except OSError:
        pass


def pick_quote_from_members(members):
    """同名板块行情缺失时的兜底：用成分股聚合出板块口径行情。"""
    def med(key):
        vals = sorted(v for v in (pan.num(m.get(key)) for m in members) if v is not None)
        return None if not vals else vals[len(vals) // 2]

    def avg(key):
        vals = [v for v in (pan.num(m.get(key)) for m in members) if v is not None]
        return None if not vals else round(sum(vals) / len(vals), 3)

    amts = [v for v in (pan.num(m.get("f6")) for m in members) if v is not None]
    up = sum(1 for m in members if (pan.num(m.get("f3")) or 0) > 0)
    dn = sum(1 for m in members if (pan.num(m.get("f3")) or 0) < 0)
    return {"f12": None, "f14": None,
            "f3": med("f3"), "f109": med("f109"), "f160": med("f160"), "f110": med("f110"),
            "f24": med("f24"), "f25": med("f25"), "f8": avg("f8"),
            "f184": avg("f184"), "f62": avg("f62"), "f6": sum(amts) if amts else None,
            "f104": up, "f105": dn, "f128": None, "f140": None, "f136": None}


def pick_new_ctx():
    """荐股用的 pan.py 上下文（只借它的 HTTP 取数层，不跑 pan.py 主流程）。"""
    return pan.Ctx(PAN_DIR, datetime.now().date(), "post")


def pick_hot_rank(pool):
    """板块池综合热度：涨跌幅 0.5 + 主力净占比 0.5（池内分位）。"""
    c3 = [pan.num(b.get("f3")) for b in pool]
    c184 = [pan.num(b.get("f184")) for b in pool]
    for b in pool:
        hot = pick_wsum([(pick_pct(c3, pan.num(b.get("f3"))), 0.5),
                         (pick_pct(c184, pan.num(b.get("f184"))), 0.5)])
        b["热度"] = None if hot is None else round(100.0 * hot, 1)
    pool.sort(key=lambda b: (b["热度"] if b["热度"] is not None else -1.0), reverse=True)
    return pool


def pick_board_page_all(ctx, fs, cap_pages=8):
    """把某一类板块全量翻页取回（东财 pz 上限 100）。失败返回 None。"""
    rows = []
    for pn in range(1, cap_pages + 1):
        batch = pick_em_page(ctx, fs, PICK_BOARD_FIELDS, pn=pn, pz=100)
        if batch is None:
            return None
        rows.extend(batch)
        if len(batch) < 100:
            break
    uniq = {}
    for r in rows:
        code = str(r.get("f12") or "")
        if code and code not in uniq:
            uniq[code] = r
    return pick_hot_rank(list(uniq.values()))


def pick_industry_boards(refresh=False):
    """东财全部行业板块行情（496 个，含一级/二级/三级），按名称索引。返回 (byname, 元信息)。"""
    doc = pick_cache_get("industry_boards", refresh)
    if doc is None:
        doc = pick_disk_read("pick_industry_boards.json")
    if doc is None:
        rows = pick_board_page_all(pick_new_ctx(), pan.EM_FS_INDUSTRY)
        if not rows:
            return None, {"错误": "东财行业板块行情取数失败"}
        doc = {"生成时间": aiplan.iso_now(), "板块": rows}
        pick_disk_write("pick_industry_boards.json", doc)
    pick_cache_put("industry_boards", doc)
    byname = {}
    for r in doc.get("板块") or []:
        byname[str(r.get("f14"))] = r
    return byname, {"生成时间": doc.get("生成时间"), "数量": len(byname)}


def pick_concept_boards(refresh=False):
    """东财全部概念板块行情（504 个），每行附「主题」。返回 (列表, 元信息)。"""
    doc = pick_cache_get("concept_boards", refresh)
    if doc is None:
        doc = pick_disk_read("pick_concept_boards.json")
    if doc is None:
        rows = pick_board_page_all(pick_new_ctx(), pan.EM_FS_CONCEPT, cap_pages=8)
        if not rows:
            return None, {"错误": "东财概念板块行情取数失败"}
        doc = {"生成时间": aiplan.iso_now(), "板块": rows}
        pick_disk_write("pick_concept_boards.json", doc)
    pick_cache_put("concept_boards", doc)
    out = []
    for r in doc.get("板块") or []:
        row = dict(r)
        row["主题"] = pick_concept_theme(row.get("f14"))
        out.append(row)
    return out, {"生成时间": doc.get("生成时间"), "数量": len(out)}


def pick_l1_members(code, refresh=False):
    """某个一级行业的成分股（含 f100 细分行业名）。返回 (列表, 元信息)。"""
    key = "l1_%s" % code
    doc = pick_cache_get(key, refresh)
    if doc is None:
        doc = pick_disk_read("pick_l1_%s.json" % code)
    if doc is None:
        rows = []
        for pn in range(1, 12):
            batch = pick_em_page(pick_new_ctx(), "b:%s" % code, PICK_STOCK_FIELDS,
                                 pn=pn, pz=100)
            if batch is None:
                break
            rows.extend(batch)
            if len(batch) < 100:
                break
        if not rows:
            return None, {"错误": "东财成分取数失败"}
        doc = {"生成时间": aiplan.iso_now(), "股票": rows}
        pick_disk_write("pick_l1_%s.json" % code, doc)
    pick_cache_put(key, doc)
    return doc.get("股票") or [], {"生成时间": doc.get("生成时间"),
                                   "数量": len(doc.get("股票") or [])}


def pick_l1_subs(code, refresh=False):
    """一级行业的二级细分：按成分股 f100 聚合，附同名东财板块行情。返回 (数据, 错误)。"""
    rows, meta = pick_l1_members(code, refresh)
    if rows is None:
        return None, (meta or {}).get("错误") or "成分取数失败"
    byname, imeta = pick_industry_boards(refresh)
    cnt, flow_sum, amt_sum, up, dn = {}, {}, {}, {}, {}
    for r in rows:
        sub = str(r.get("f100") or "").strip() or "未分类"
        cnt[sub] = cnt.get(sub, 0) + 1
        f184, f6, f3 = pan.num(r.get("f184")), pan.num(r.get("f6")), pan.num(r.get("f3"))
        if f184 is not None:
            flow_sum[sub] = flow_sum.get(sub, 0.0) + f184
        if f6 is not None:
            amt_sum[sub] = amt_sum.get(sub, 0.0) + f6
        if f3 is not None:
            if f3 > 0:
                up[sub] = up.get(sub, 0) + 1
            elif f3 < 0:
                dn[sub] = dn.get(sub, 0) + 1
    subs = []
    for name, n in sorted(cnt.items(), key=lambda kv: -kv[1]):
        b = (byname or {}).get(name) or {}
        subs.append({
            "名称": name, "股票数": n, "代码": b.get("f12"),
            "涨跌幅_pct": pan.num(b.get("f3")) if b else None,
            "主力净占比_pct": pan.num(b.get("f184")) if b else None,
            "热度": b.get("热度") if b else None,
            "成分聚合": {
                "主力净占比_pct": None if name not in flow_sum else round(flow_sum[name] / n, 2),
                "成交额_亿": None if name not in amt_sum else round(amt_sum[name] / 1e8, 2),
                "上涨家数": up.get(name), "下跌家数": dn.get(name)},
            "行情口径": "东财板块" if b.get("f12") else "成分聚合",
        })
    return {"一级代码": code, "股票数": len(rows), "细分": subs,
            "缓存时间": (meta or {}).get("生成时间"),
            "板块行情缓存": (imeta or {}).get("生成时间")}, None


def pick_boards_bundle(refresh=False):
    """荐股筛选区数据：31 个一级行业 + 全部概念（含主题）。"""
    byname, imeta = pick_industry_boards(refresh)
    concepts, cmeta = pick_concept_boards(refresh)
    l1, missing = [], []
    for name in PICK_L1_INDUSTRIES:
        b = (byname or {}).get(name)
        if not b:
            missing.append(name)
            l1.append({"名称": name, "代码": None, "可用": False})
            continue
        f62, f6 = pan.num(b.get("f62")), pan.num(b.get("f6"))
        l1.append({
            "名称": name, "代码": b.get("f12"), "可用": True,
            "涨跌幅_pct": pan.num(b.get("f3")),
            "主力净占比_pct": pan.num(b.get("f184")),
            "主力净流入_亿": None if f62 is None else round(f62 / 1e8, 3),
            "成交额_亿": None if f6 is None else round(f6 / 1e8, 2),
            "热度": b.get("热度")})
    items, theme_cnt = [], {}
    for r in concepts or []:
        theme = r.get("主题") or PICK_THEME_OTHER
        theme_cnt[theme] = theme_cnt.get(theme, 0) + 1
        f62, f6 = pan.num(r.get("f62")), pan.num(r.get("f6"))
        items.append({
            "代码": r.get("f12"), "名称": r.get("f14"), "主题": theme,
            "涨跌幅_pct": pan.num(r.get("f3")),
            "主力净占比_pct": pan.num(r.get("f184")),
            "主力净流入_亿": None if f62 is None else round(f62 / 1e8, 3),
            "成交额_亿": None if f6 is None else round(f6 / 1e8, 2),
            "热度": r.get("热度")})
    order = [t for t, _kw in PICK_CONCEPT_THEMES] + [PICK_THEME_OTHER]
    themes = [{"主题": t, "数量": theme_cnt[t]} for t in order if t in theme_cnt]
    errors = [x for x in [(imeta or {}).get("错误"), (cmeta or {}).get("错误")] if x]
    return {"一级行业": l1, "概念": items, "主题": themes, "缺失行业": missing,
            "缓存": {"行业": (imeta or {}).get("生成时间"), "概念": (cmeta or {}).get("生成时间")},
            "每板块候选默认": PICK_PER_BOARD, "候选上限默认": PICK_MAX_CANDIDATES,
            "概念上限": PICK_MAX_CONCEPTS, "错误": errors}


def pick_target_boards(log, ctl, ctx, opts, degrade):
    """按用户筛选构建板块池：行业细分（成分已随一级行业取回）+ 概念。

    返回 (板块列表, 取消标记)。板块行 = 东财板块行情（或成分聚合兜底）+ 类型/名称/成员。
    """
    out = []
    byname, imeta = pick_industry_boards(opts.get("refresh"))
    if byname is None:
        degrade.append("行业板块行情取数失败：%s" % (imeta or {}).get("错误"))
    for item in opts["industry"]:
        if ctl.get("cancel"):
            return out, "canceled"
        code, subs = item["code"], item["subs"]
        rows, meta = pick_l1_members(code, opts.get("refresh"))
        if rows is None:
            degrade.append("一级行业「%s」成分取数失败：%s"
                           % (item.get("name") or code, (meta or {}).get("错误")))
            continue
        groups = {}
        for r in rows:
            sub = str(r.get("f100") or "").strip() or "未分类"
            if subs and sub not in subs:
                continue
            groups.setdefault(sub, []).append(r)
        if not groups:
            degrade.append("一级行业「%s」在所选细分下没有成分股（细分名可能已变）"
                           % (item.get("name") or code))
            continue
        for sub, members in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            b = (byname or {}).get(sub)
            row = dict(b) if b else pick_quote_from_members(members)
            row["类型"] = "行业"
            row["f14"] = sub
            row["细分"] = sub
            row["一级行业"] = item.get("name") or code
            row["成员"] = members
            row["行情口径"] = "东财板块" if b else "成分聚合"
            out.append(row)
        log("[OK] 行业「%s」→ %d 个细分，成分 %d 只（缓存 %s）"
            % (item.get("name") or code, len(groups), len(rows),
               (meta or {}).get("生成时间") or "—"))
    if opts["concepts"]:
        concepts, cmeta = pick_concept_boards(opts.get("refresh"))
        concept_failed = concepts is None
        if concepts is None:
            degrade.append("概念板块行情取数失败（%s），本次跳过 %d 个概念"
                           % ((cmeta or {}).get("错误") or "东财", len(opts["concepts"])))
            concepts = []
        cmap = {str(r.get("f12")): r for r in (concepts or [])}
        for code in opts["concepts"]:
            if ctl.get("cancel"):
                return out, "canceled"
            r = cmap.get(code)
            if not r:
                if not concept_failed:
                    degrade.append("概念板块 %s 不在东财概念列表里，已跳过" % code)
                continue
            row = dict(r)
            row["类型"] = "概念"
            row["成员"] = None
            row["行情口径"] = "东财板块"
            out.append(row)
        log("[OK] 概念 %d 个（缓存 %s）"
            % (len(out) - sum(1 for b in out if b.get("类型") == "行业"),
               (cmeta or {}).get("生成时间") or "—"))
    return out, None


def pick_collect_members(log, ctl, ctx, boards, per_board, exclude, degrade,
                         max_candidates):
    """逐板块取成分（行业用已抓的成员，概念现抓）→ 筛前 per_board 只 → 全池去重。

    超过 max_candidates 时按板块热度从高到低截断。返回 (池, 排除统计, 取消标记)。
    """
    stat, fails, packs = {}, [], []
    for i, b in enumerate(boards, 1):
        if ctl.get("cancel"):
            return None, stat, "canceled"
        code = str(b.get("代码") or b.get("f12") or "")
        bname = str(b.get("名称") or b.get("f14") or code)
        raw = b.get("成员")
        if raw is None:
            raw = pick_em_list(ctx, "b:%s" % code, PICK_STOCK_FIELDS,
                               max(per_board * PICK_CAND_SCAN, per_board), "f3")
            if raw is None:
                fails.append(bname)
                raw = []
        raw = [x for x in raw if aiplan.code6(str(x.get("f12") or ""))]
        raw.sort(key=lambda x: (pan.num(x.get("f3")) if pan.num(x.get("f3")) is not None
                                else -999.0), reverse=True)
        kept, dropped = pick_filter(raw, exclude)
        for k, v in dropped.items():
            stat[k] = stat.get(k, 0) + v
        packs.append((b, kept[:max(1, per_board)], bname))
        if i % 5 == 0 or i == len(boards):
            log("[..] 板块成分 %d/%d（%s）｜ 已备 %d 只"
                % (i, len(boards), bname, sum(len(x[1]) for x in packs)))
    if fails:
        degrade.append("成分股取数失败 %d 个板块（东财）：%s%s"
                       % (len(fails), "、".join(fails[:5]),
                          "…" if len(fails) > 5 else ""))
    cap = max(20, int(max_candidates))
    total = sum(len(x[1]) for x in packs)
    truncate = total > cap
    order = sorted(packs, key=lambda x: (x[0].get("热度")
                                         if x[0].get("热度") is not None else -1.0),
                   reverse=True)
    pool, seen, used = [], {}, 0
    for b, kept, bname in order:
        if used >= cap:
            break
        take = kept if (used + len(kept)) <= cap else kept[:max(0, cap - used)]
        added = 0
        for m in take:
            c6 = aiplan.code6(str(m.get("f12") or ""))
            if c6 in seen:
                continue
            m["代码"] = c6
            m["名称"] = str(m.get("f14") or "").strip() or c6
            m["来源板块"] = bname
            m["板块类型"] = b.get("类型")
            m["板块分"] = b.get("机械分")
            seen[c6] = m
            pool.append(m)
            added += 1
        b["入池"] = added
        used = len(pool)
    for b, _kept, _n in packs:
        b.setdefault("入池", 0)
    if truncate:
        degrade.append("候选池按上限 %d 只截断（原本 %d 只，低热度板块被裁到后面）"
                       % (cap, total))
    return pool, stat, None


def pick_cap_degrade(degrade, limit=40):
    """降级清单太长就折叠尾部，页面与事实包都不至于被刷屏。"""
    if len(degrade) <= limit:
        return degrade
    keep = list(degrade[:limit])
    keep.append("…另有 %d 条同类降级信息（详见任务日志）" % (len(degrade) - limit))
    return keep


def pick_call_model(log, opts, cfg, pandoc, fact):
    """一次模型调用（profile 的研判档）。失败抛异常，由调用方降级保存机械层。"""
    prof_name, prof, src = aiplan.resolve_profile(
        cfg, (pandoc or {}).get("phase") or "post", opts["profile"], None)
    if not prof:
        raise RuntimeError(src)
    conf = prof.get("研判")
    if not isinstance(conf, dict):
        raise RuntimeError("profile「%s」没有研判档" % prof_name)
    provider = aiplan.find_provider(cfg, conf.get("provider"))
    if provider is None:
        raise RuntimeError("provider 不存在：%r" % conf.get("provider"))
    provider = copy.deepcopy(provider)
    if opts["api_base"]:
        provider["base_url"] = opts["api_base"]
    model = opts["model_pro"] or conf.get("model")
    if not model:
        raise RuntimeError("研判档未指定 model")
    key, _ksrc = aiplan.resolve_key(provider, opts["api_key"])
    if not key and provider.get("鉴权") != "none":
        raise RuntimeError("未找到 API key（provider=%s）" % provider.get("名称"))
    log("[OK] profile %s（%s）｜ 研判 %s / %s ｜ key %s"
        % (prof_name, src, provider.get("名称"), model, aiplan.mask_key(key)))
    log("[..] call %s (%s) ..." % (model, provider.get("协议")))
    r = aiplan.call_model(
        provider, model, PICK_SYSTEM, PICK_PROMPT + "\n\n" + fact,
        temperature=conf.get("temperature") if conf.get("temperature") is not None else 0.3,
        max_tokens=conf.get("max_tokens") or 8000,
        json_mode=bool(provider.get("json_object")), key=key, extra=conf.get("参数"))
    if not r.get("ok"):
        raise RuntimeError("模型调用失败：%s" % r.get("error"))
    obj, perr = aiplan.extract_json(r.get("text"))
    usage = r.get("usage") or {}
    cost = aiplan.compute_cost(provider, model, usage, cfg.get("汇率") or {})
    log("[..] 模型返回 %d 字符 ｜ latency %sms"
        % (len(r.get("text") or ""), r.get("latency_ms")))
    log("[OK] usage in %s / out %s ｜ 费用 %s"
        % (usage.get("输入"), usage.get("输出"),
           cost.get("人民币_估算") if cost.get("人民币_估算") is not None
           else "未配置单价（仅记录 token）"))
    if obj is None:
        log("[WARN] 返回不是合法 JSON：%s（原文已保留在产物里）" % perr)
    return {"profile": prof_name, "profile来源": src, "provider": provider.get("名称"),
            "model": model, "usage": usage, "cost": cost, "latency_ms": r.get("latency_ms"),
            "json": obj, "raw_text": None if obj is not None else r.get("text"),
            "error": None if obj is not None else ("模型返回不是合法 JSON：%s" % perr)}


def run_pick(log, ctl, opts):
    """荐股主流程：抓板块 → 抓成分 → 机械打分 → 一次模型点评 → 落盘。"""
    t0 = time.time()
    degrade, exclude = [], opts["exclude"]
    pan_path = newest_file(pan_files())
    pandoc = aiplan.read_json(pan_path) if pan_path else None
    if pan_path:
        log("[..] pan 快照 %s（交易日 %s）"
            % (rel(pan_path), (pandoc or {}).get("trade_date")))
    else:
        degrade.append("没有 pan 快照（data/pan/）：产物的「交易日」参考为空，"
                       "其余数据仍来自东财实时行情")
    ctx = pick_new_ctx()
    log("[..] 筛选：行业 %d 个（细分 %d 个）/ 概念 %d 个 ｜ 每板块 %d 只 ｜ 候选上限 %d"
        % (len(opts["industry"]),
           sum(len(x["subs"]) or 1 for x in opts["industry"]),
           len(opts["concepts"]), opts["per_board"], opts["max_candidates"]))
    targets, canceled = pick_target_boards(log, ctl, ctx, opts, degrade)
    if canceled:
        log("[WARN] 已取消，未落盘")
        return {"__canceled__": True}
    if not targets:
        raise RuntimeError("所选板块没有取到任何数据：检查筛选条件与网络（日志降级项有明细）")
    if ctl.get("cancel"):
        return {"__canceled__": True}
    all_boards = []
    for label in PICK_BOARD_TYPES:
        pool = [b for b in targets if b.get("类型") == label]
        if pool:
            all_boards.extend(pick_score_boards(pool))
    log("[OK] 板块池 %d 个（行业 %d / 概念 %d）"
        % (len(all_boards),
           sum(1 for b in all_boards if b.get("类型") == "行业"),
           sum(1 for b in all_boards if b.get("类型") == "概念")))
    log("[..] 开始取成分股（每板块前 %d 只，候选上限 %d）…"
        % (opts["per_board"], opts["max_candidates"]))
    pool, stat, canceled = pick_collect_members(
        log, ctl, ctx, all_boards, opts["per_board"], exclude, degrade,
        opts["max_candidates"])
    if canceled:
        log("[WARN] 已取消，未落盘")
        return {"__canceled__": True}
    if not pool:
        degrade.append("候选池为空：成分股取数失败或全部被排除规则过滤，"
                       "本次只输出板块评估（仍会调一次模型点评板块）")
        log("[WARN] 候选池为空：本次只输出板块评估")
    else:
        log("[OK] 候选池 %d 只（排除：%s）"
            % (len(pool), "，".join("%s %d 只" % (k, v) for k, v in stat.items()) or "无"))
    degrade = pick_cap_degrade(degrade)
    cands = {}
    scored = {}
    for m in opts["modules"]:
        rows = pick_score_stocks([dict(r) for r in pool], m)
        rows.sort(key=lambda r: (r["机械分"] if r["机械分"] is not None else -1.0), reverse=True)
        scored[m] = rows
    assign = pick_module_picks(scored, opts["modules"])
    for m in opts["modules"]:
        rows = [r for r in scored[m] if assign.get(r.get("代码")) == m]
        cands[m] = [pick_stock_view(r, m) for r in rows]
        log(("[OK] 模块 %s：入选 %d 只（合并榜共 %d 只，机械分最高 %s）"
             % (m, len(rows), len(assign), rows[0]["机械分"] if rows else "—")) if rows
            else "[WARN] 模块 %s：没有进入合并榜的候选（见降级清单）" % m)
    payload = {
        "tool": "aiplan-webui", "kind": "pick", "schema_version": "1",
        "生成时间": aiplan.iso_now(), "日期": aiplan.now_local().strftime("%Y%m%d"),
        "交易日": (pandoc or {}).get("trade_date"),
        "pan路径": rel(pan_path) if pan_path else None,
        "参数": {"筛选": {
                     "行业": [{"代码": x["code"], "名称": x.get("name") or x["code"],
                              "细分": x["subs"] or "全部"} for x in opts["industry"]],
                     "概念": [{"代码": b.get("代码") or b.get("f12"),
                              "名称": b.get("名称") or b.get("f14"),
                              "主题": b.get("主题")}
                              for b in all_boards if b.get("类型") == "概念"]},
                 "模块": opts["modules"], "每板块候选": opts["per_board"],
                 "候选上限": opts["max_candidates"], "事实包上限": opts["max_chars"],
                 "排除规则": exclude},
        "候选池数量": len(pool), "排除统计": stat,
        "合并候选数": len(assign),          # 四个模块合并去重后的候选总数（= PICK_PAGE_TOP）
        "板块": {label: [pick_board_view(b) for b in all_boards
                        if b.get("类型") == label] for label in PICK_BOARD_TYPES},
        "候选": cands, "板块评估": [], "模块": [], "总评": {},
        "降级": degrade, "降级与不确定性": [], "免责声明": "",
        "配置": {}, "成本": {}, "用量": {}, "latency_ms": None,
        "factpack_chars": 0, "模型层": {},
        "note": "本结果由本地机械打分与一次模型点评生成，不构成投资建议。",
    }
    fact = pick_factpack(payload, opts["max_chars"])
    payload["factpack_chars"] = len(fact)
    log("[..] 事实包 %d 字符（上限 %d）" % (len(fact), opts["max_chars"]))
    if len(fact) > opts["max_chars"]:
        degrade.append("事实包超预算（%d > %d 字符），已按机械分截断候选表"
                       % (len(fact), opts["max_chars"]))
    cfg, cfg_err = aiplan.load_models(MODELS_PATH)
    if cfg_err:
        model_layer = {"error": cfg_err}
        log("[WARN] 模型层跳过：%s" % cfg_err)
    elif ctl.get("cancel"):
        log("[WARN] 已取消，未落盘")
        return {"__canceled__": True}
    else:
        try:
            model_layer = pick_call_model(log, opts, cfg, pandoc, fact)
        except Exception as e:  # noqa: BLE001
            model_layer = {"error": str(e)[:600]}
            log("[WARN] 模型点评失败：%s（机械榜仍会保存）" % str(e)[:300])
    if ctl.get("cancel"):
        log("[WARN] 已取消：模型返回后不再落盘")
        return {"__canceled__": True}
    payload["模型层"] = model_layer
    obj = model_layer.get("json") or {}
    payload["总评"] = obj.get("总评") or {}
    payload["模块"] = obj.get("模块") or []
    payload["板块评估"] = obj.get("板块评估") or []
    payload["降级与不确定性"] = obj.get("降级与不确定性") or []
    payload["免责声明"] = obj.get("免责声明") or "本结论由机械打分与模型点评生成，不构成投资建议。"
    payload["配置"] = {k: model_layer.get(k)
                       for k in ("profile", "profile来源", "provider", "model")}
    payload["用量"] = model_layer.get("usage") or {}
    payload["成本"] = model_layer.get("cost") or {}
    payload["latency_ms"] = model_layer.get("latency_ms")
    json_path, md_path = save_pick(payload, pick_markdown(payload))
    payload["产物"] = {"json": rel(json_path), "md": rel(md_path)}
    body = json.dumps(payload, ensure_ascii=False, indent=1)
    atomic_write(json_path, body, newline="\n")
    atomic_write(os.path.join(os.path.dirname(json_path), "latest_pick.json"), body, newline="\n")
    log("[OK] 已保存 %s（用时 %.1f 秒）" % (rel(json_path), time.time() - t0))
    return {"pick": rel(json_path), "pick_path": json_path, "md": rel(md_path),
            "候选池": len(pool), "模块": opts["modules"],
            "模型": model_layer.get("model"), "模型错误": model_layer.get("error")}
