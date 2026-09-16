# -*- coding: utf-8 -*-
"""荐股编排（全大盘版）：全市场扫描 → 机械打分 → 前 150 只交模型 → 落盘。

两次打分（口径写进产物，页面可见）：
  1) 全池 400 只：用批量可得的字段打分，只作**初筛**（挑出送模型的前 150 只）；
  2) 前 150 只：补上公告类否决、股东户数集中度、十大流通股东占比、10 日主力资金后**重算**，
     这份分数才是推荐榜里的「机械分（辅助）」。
模型分是唯一排名依据；模型失败时回退机械分排序并在页面明确标注。
"""

import copy
import json
import os
import time

from . import mech, mechdata
from .archive import save_pick
from .mechstock import (chip_focus as _chip_focus, fmt_money as _fmt_money,
                        flow_rate as _flow_rate, fund_inputs as _fund_inputs,
                        mech_data as _mech_data, tech_inputs as _tech_inputs)
from .paths import MODELS_PATH, aiplan, atomic_write, num, pan, rel
from .pick import (PICK_MAX_CHARS, PICK_MODEL_TOP, PICK_PAGE_TOP, PICK_POOL_SIZE, PICK_PROMPT,
                   PICK_STYLE_CYCLE, PICK_SYSTEM, pick_factpack, pick_markdown, pick_style_of)
from .plancheck import cost_of
from .sources import newest_file, pan_files

def run_pick(log, ctl, opts):
    """荐股主流程（全大盘）。返回落盘摘要；取消返回 {'__canceled__': True}。"""
    t0 = time.time()
    degrade, vetoed = [], []
    pan_path = newest_file(pan_files())
    pandoc = aiplan.read_json(pan_path) if pan_path else None
    if pan_path:
        log("[..] pan 快照 %s（交易日 %s）" % (rel(pan_path), (pandoc or {}).get("trade_date")))
    else:
        degrade.append("没有 pan 快照（data/pan/）：产物的「交易日」参考为空")
    log("[..] 扫描参数：候选池 %d 只 ｜ 送模型 %d 只 ｜ 事实包上限 %d 字符"
        % (opts["pool_size"], opts["model_top"], opts["max_chars"]))
    raw_pool, stats, hints, market_total = mechdata.market_scan(
        opts["exclude"], size=opts["pool_size"], refresh=opts["refresh"], log=log)
    degrade += hints
    if ctl.get("cancel"):
        return {"__canceled__": True}
    if not raw_pool:
        raise RuntimeError("全市场扫描没有取到数据：检查网络（日志降级项有明细）")
    pool = [mechdata.pool_row(r) for r in raw_pool]
    log("[..] 取宏观 / 财务 / 板块背景 …")
    env = mechdata.macro_inputs(refresh=opts["refresh"], log=log)
    lico, latest, prev, same = mechdata.finance_by_period(refresh=opts["refresh"], log=log)
    if not latest:
        degrade.append("业绩报表取不到：模块2/3 的基本面小项会记缺失")
    balance = mechdata.balance_by_period(refresh=opts["refresh"], log=log)
    code_ind = mechdata.code_industry_map((balance or {}).get(latest) or {})
    scan_ind = mechdata.scanned_industry_map()      # 行业景气用全市场扫描样本
    if not scan_ind:
        degrade.append("没有全市场行业映射：行业景气（模块2）会记缺失")
    sample_ind = dict(scan_ind)
    sample_ind.update(code_ind)                     # 资产负债表口径更准，优先
    if not code_ind:
        degrade.append("资产负债表取不到：应收/净资产两项（商誉本来就无源）记缺失")
    for row in pool:
        row["行业"] = code_ind.get(row["代码"]) or row.get("行业") or "未归类"
    ind_agg = mechdata.industry_agg(sample_ind, (lico or {}).get(latest) or {}, latest)
    log("[OK] 行业景气覆盖 %d 个行业（样本：全市场扫描 %d 只）" % (len(ind_agg), len(sample_ind)))
    heat = mechdata.board_heat(refresh=opts["refresh"])
    if ctl.get("cancel"):
        return {"__canceled__": True}
    # 全池一票否决（可批量项）
    alive = []
    for row in pool:
        fin = _fund_inputs(row["代码"], lico or {}, balance or {}, latest, prev, same, row)
        row["_fin"] = fin
        why = mech.veto_batch(dict(row, **{"两年亏损且营收不足1亿": fin["两年亏损且营收不足1亿"]}))
        if why:
            vetoed.append({"阶段": "全池", "代码": row["代码"], "名称": row["名称"], "原因": why})
        else:
            alive.append(row)
    log("[OK] 一票否决（全池）：淘汰 %d 只，剩 %d 只" % (len(vetoed), len(alive)))
    # 日K + 技术指标 + 龙虎榜（全池可批量）
    bars_map = mechdata.klines([r["代码"] for r in alive], refresh=opts["refresh"], log=log)
    bench = mechdata.bench_kline(refresh=opts["refresh"])
    if not bench:
        degrade.append("沪深300 日K 取不到：20 日超额收益这一项记缺失")
    lhb = mechdata.lhb_state([r["代码"] for r in alive], refresh=opts["refresh"], log=log)
    if ctl.get("cancel"):
        return {"__canceled__": True}
    etf_cache = {}
    for row in alive:
        ind = row["行业"]
        if ind not in etf_cache:
            etf_cache[ind] = mechdata.etf_flow_ratio(ind, refresh=opts["refresh"])
    log("[..] 第一遍机械打分（全池 %d 只，只作初筛）…" % len(alive))
    scored = []
    for row in alive:
        code = row["代码"]
        tech = _tech_inputs(bars_map.get(code) or [], bench, row)
        tech["_bars"] = bars_map.get(code) or []
        data = _mech_data(row, row["_fin"], tech, env, ind_agg, row["行业"],
                          {"行业ETF资金率": etf_cache.get(row["行业"]),
                           "龙虎榜机构": lhb.get(code),
                           "主力净流入率": _flow_rate(row)})
        res = mech.score_stock(data)
        scored.append({"row": row, "tech": tech, "data": data, "分": res})
    scored.sort(key=lambda x: (x["分"]["机械分"] if x["分"]["机械分"] is not None else -1.0),
                reverse=True)
    top = scored[:opts["model_top"]]
    log("[OK] 初筛完成：机械分区间 %s ~ %s"
        % (_fmt_money(scored[-1]["分"]["机械分"], 1) if scored else "—",
           _fmt_money(scored[0]["分"]["机械分"], 1) if scored else "—"))
    # 前 150 只：补公告类否决 + 股东类 + 10 日主力资金，然后重算机械分
    log("[..] 前 %d 只补齐公告/股东/资金明细（这一步最慢）…" % len(top))
    pledge = mechdata.pledge_map(refresh=opts["refresh"], log=log)
    north = mechdata.north_change([x["row"]["代码"] for x in top], refresh=opts["refresh"], log=log)
    north_by_ind = _north_industry_avg(north, {x["row"]["代码"]: x["row"]["行业"] for x in top})
    warn = mechdata.profit_warning([x["row"]["代码"] for x in top], refresh=opts["refresh"])
    final, keep = [], []
    for i, item in enumerate(top, start=1):
        if ctl.get("cancel"):
            return {"__canceled__": True}
        row = item["row"]
        code = row["代码"]
        flags = mechdata.announce_flags(code)
        reason = mech.veto_announce(dict(flags, 质押比例_pct=pledge.get(code)))
        if reason:
            vetoed.append({"阶段": "前150", "代码": code, "名称": row["名称"], "原因": reason})
            continue
        flow = mechdata.fund_flow(code, days=10, refresh=opts["refresh"])
        rate = None
        if flow:
            size = row.get("流通市值_亿")
            if size:
                rate = round(flow["净额_万"] / 1e4 / size * 100.0, 2)
        chip_value, chip_note = _chip_focus(item["tech"].get("_bars") or [],
                                            mechdata.holder_focus(code))
        extras = {
            "行业ETF资金率": etf_cache.get(row["行业"]),
            "北向行业变动": north_by_ind.get(row["行业"]),
            "筹码集中度": chip_value, "筹码集中度_口径": chip_note,
            "十大流通占比": mechdata.top10_free_ratio(code),
            "主力净流入率": rate if rate is not None else _flow_rate(row),
            "主力10日_万": (flow or {}).get("净额_万"),
            "龙虎榜机构": lhb.get(code),
            "质押比例_pct": pledge.get(code),
            "无监管处罚": 0 if flags.get("立案或谴责") else 1,
            "无业绩下修": 0 if warn.get(code) else 1,
            "无减持计划": 0 if (flags.get("有减持计划")
                                and (flags.get("减持比例_pct") is None
                                     or flags["减持比例_pct"] >= 1)) else 1,
        }
        data = _mech_data(row, row["_fin"], item["tech"], env, ind_agg, row["行业"], extras)
        item["data"] = data
        item["分"] = mech.score_stock(data)
        item["extras"] = extras
        final.append(item)
        keep.append(code)
        if i % 25 == 0:
            log("[..] 明细 %d/%d" % (i, len(top)))
    final.sort(key=lambda x: (x["分"]["机械分"] if x["分"]["机械分"] is not None else -1.0),
               reverse=True)
    if not final:
        raise RuntimeError("前 %d 只全部被一票否决或取数失败：没有可送模型的候选" % len(top))
    log("[OK] 送模型 %d 只 ｜ 一票否决合计 %d 只（全池 %d / 前150 %d）"
        % (len(final), len(vetoed),
           len([v for v in vetoed if v["阶段"] == "全池"]),
           len([v for v in vetoed if v["阶段"] == "前150"])))
    payload = _payload(opts, pandoc, pan_path, market_total, pool, stats, env, heat, ind_agg,
                       final, vetoed, degrade)
    fact = pick_factpack(payload, opts["max_chars"])
    payload["factpack_chars"] = len(fact)
    log("[..] 事实包 %d 字符（上限 %d，不含机械分）" % (len(fact), opts["max_chars"]))
    if len(fact) > opts["max_chars"]:
        degrade.append("事实包超出预算（%d > %d 字符），已按条数截断"
                       % (len(fact), opts["max_chars"]))
    cfg, cfg_err = aiplan.load_models(MODELS_PATH)
    if cfg_err:
        model_layer = {"error": cfg_err}
        log("[WARN] 模型层跳过：%s" % cfg_err)
    elif ctl.get("cancel"):
        return {"__canceled__": True}
    else:
        log("[..] 调用模型（profile 的研判档，1 次）…")
        try:
            model_layer = _call_model(log, opts, cfg, pandoc, fact)
        except Exception as e:            # noqa: BLE001
            model_layer = {"error": str(e)[:600]}
            log("[WARN] 模型调用失败：%s（机械榜仍会保存）" % str(e)[:300])
    if ctl.get("cancel"):
        log("[WARN] 已取消：模型返回后不再落盘")
        return {"__canceled__": True}
    obj = model_layer.get("json") or {}
    payload["模型层"] = model_layer
    payload["市场风格"] = obj.get("市场风格") or {}
    payload["风险与不确定性"] = obj.get("风险与不确定性") or []
    payload["免责声明"] = obj.get("免责声明") or "本结论由模型评分与机械分共同生成，不构成投资建议。"
    payload["配置"] = {k: model_layer.get(k) for k in ("profile", "profile来源", "provider", "model")}
    payload["用量"] = model_layer.get("usage") or {}
    payload["成本"] = model_layer.get("cost") or {}
    payload["latency_ms"] = model_layer.get("latency_ms")
    payload["推荐榜"] = _rank_rows(obj.get("推荐榜") or [], final)
    payload["候选技术"] = {x["row"]["代码"]: _tech_view(x["tech"], x.get("extras"))
                           for x in final}
    json_path, md_path = save_pick(payload, pick_markdown(payload))
    payload["产物"] = {"json": rel(json_path), "md": rel(md_path)}
    body = json.dumps(payload, ensure_ascii=False, indent=1)
    atomic_write(json_path, body, newline="\n")
    atomic_write(os.path.join(os.path.dirname(json_path), "latest_pick.json"), body, newline="\n")
    log("[OK] 已保存 %s（用时 %.1f 秒）" % (rel(json_path), time.time() - t0))
    return {"pick": rel(json_path), "pick_path": json_path, "md": rel(md_path),
            "候选池": len(pool), "送模型": len(final), "推荐榜": len(payload["推荐榜"]),
            "被否决": len(vetoed),
            "模型": model_layer.get("model"), "模型错误": model_layer.get("error")}


def _north_industry_avg(per_code, code_ind):
    """个股北向持股变动 → 行业中位数（百分点）。"""
    buckets = {}
    for code, v in (per_code or {}).items():
        ind = code_ind.get(code)
        if ind and v is not None:
            buckets.setdefault(ind, []).append(v)
    return {ind: round(mechdata._median(vals), 4) for ind, vals in buckets.items() if vals}


def _tech_view(tech, extras):
    """技术面 + 资金/股东原始指标 → 事实包第 2 张表的列。"""
    out = {k: (tech or {}).get(k) for k in
           ("MA20距离_pct", "MA60距离_pct", "MA120距离_pct", "MA250距离_pct",
            "量能比_20_60", "量价比_5", "MACD", "RSI14", "超额20_pct", "换手10日_pct")}
    out.update({"主力10日_万": (extras or {}).get("主力10日_万"),
                "龙虎榜": (extras or {}).get("龙虎榜机构"),
                "质押比例_pct": (extras or {}).get("质押比例_pct"),
                "北向变动_pp": (extras or {}).get("北向行业变动"),
                "筹码集中度": (extras or {}).get("筹码集中度"),
                "十大流通占比_pct": (extras or {}).get("十大流通占比")})
    return out


def _rank_rows(model_rows, final, top=PICK_PAGE_TOP):
    """模型推荐榜 + 机械分 → 榜单行（模型分降序；模型没点评的用机械分补位）。"""
    by_code = {x["row"]["代码"]: x for x in final}
    out, used = [], set()
    for item in model_rows or []:
        code = aiplan.code6(str(item.get("代码") or ""))
        hit = by_code.get(code)
        if not code or hit is None or code in used:
            continue
        used.add(code)
        extras = hit.get("extras") or {}
        out.append({
            "代码": code, "名称": item.get("名称") or hit["row"]["名称"],
            "打法": pick_style_of(item.get("打法")), "评分": num(item.get("评分")),
            "评级": item.get("评级"), "理由": item.get("理由"),
            "所属板块": item.get("所属板块") or hit["row"]["行业"],
            "机械分": hit["分"]["机械分"], "现价": hit["row"]["现价"],
            "涨跌幅_pct": hit["row"]["涨跌幅_pct"], "行业": hit["row"]["行业"],
            "换手率_pct": hit["row"]["换手率_pct"], "量比": hit["row"]["量比"],
            "成交额_亿": hit["row"]["成交额_亿"], "总市值_亿": hit["row"]["总市值_亿"],
            "主力净流入_万": extras.get("主力10日_万") or hit["row"]["主力净流入_万"],
            "主力净占比_pct": hit["row"]["主力净占比_pct"],
            "来源": "模型",
        })
    if len(out) < min(top, len(final)):
        for hit in final:
            if len(out) >= top:
                break
            code = hit["row"]["代码"]
            if code in used:
                continue
            used.add(code)
            out.append({
                "代码": code, "名称": hit["row"]["名称"], "打法": "未定", "评分": None,
                "评级": None, "理由": "模型没有点评这只（按机械分补位）",
                "所属板块": hit["row"]["行业"], "机械分": hit["分"]["机械分"],
                "现价": hit["row"]["现价"], "涨跌幅_pct": hit["row"]["涨跌幅_pct"],
                "行业": hit["row"]["行业"], "换手率_pct": hit["row"]["换手率_pct"],
                "量比": hit["row"]["量比"], "成交额_亿": hit["row"]["成交额_亿"],
                "总市值_亿": hit["row"]["总市值_亿"],
                "主力净流入_万": hit["row"]["主力净流入_万"],
                "主力净占比_pct": hit["row"]["主力净占比_pct"],
                "来源": "机械分补位",
            })
    out.sort(key=lambda r: (-(r["评分"] if r["评分"] is not None else -1.0),
                            -(r["机械分"] if r["机械分"] is not None else -1.0),
                            str(r["代码"])))
    for i, r in enumerate(out, start=1):
        r["排名"] = i
    return out


def _call_model(log, opts, cfg, pandoc, fact):
    """一次模型调用（profile 的研判档）。失败抛异常，由调用方记降级。"""
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
        max_tokens=conf.get("max_tokens") or 16000,
        json_mode=bool(provider.get("json_object")), key=key, extra=conf.get("参数"))
    if not r.get("ok"):
        raise RuntimeError("模型调用失败：%s" % r.get("error"))
    obj, perr = aiplan.extract_json(r.get("text"))
    usage = r.get("usage") or {}
    cost = cost_of(provider, model, usage, cfg)
    log("[..] 模型返回 %d 字符 ｜ latency %sms" % (len(r.get("text") or ""), r.get("latency_ms")))
    log("[OK] usage in %s / out %s ｜ 费用 %s"
        % (usage.get("输入"), usage.get("输出"),
           cost.get("人民币_估算") if cost.get("人民币_估算") is not None else "未配置单价（仅记录 token）"))
    if obj is None:
        log("[WARN] 返回不是合法 JSON：%s（原文已保留在产物里）" % perr)
    rows = (obj or {}).get("推荐榜") or []
    styles = sorted({pick_style_of(x.get("打法")) for x in rows if isinstance(x, dict)})
    log("[OK] 推荐榜 %d 只（打法分布：%s）" % (len(rows), "、".join(styles) or "—"))
    return {"profile": prof_name, "profile来源": src, "provider": provider.get("名称"),
            "model": model, "usage": usage, "cost": cost, "latency_ms": r.get("latency_ms"),
            "json": obj, "raw_text": None if obj is not None else r.get("text"),
            "error": None if obj is not None else ("模型返回不是合法 JSON：%s" % perr)}


def _payload(opts, pandoc, pan_path, market_total, pool, stats, env, heat, ind_agg,
             final, vetoed, degrade):
    """产物主体（候选池 + 机械层 + 口径）。推荐榜与模型段由调用方补。"""
    mech_rows = [{"代码": x["row"]["代码"], "名称": x["row"]["名称"],
                  "机械分": x["分"]["机械分"], "实得": x["分"]["实得"],
                  "可得": x["分"]["可得"]} for x in final]
    missed = []
    seen = set()
    for x in final:
        for row in x["分"]["缺失"]:
            if row["指标"] in seen:
                continue
            seen.add(row["指标"])
            missed.append({"指标": row["指标"], "满分": row["满分"], "原因": row["原因"]})
    return {
        "tool": "aiplan-webui", "kind": "pick", "schema_version": "2",
        "生成时间": aiplan.iso_now(), "日期": aiplan.now_local().strftime("%Y%m%d"),
        "交易日": (pandoc or {}).get("trade_date"), "pan路径": rel(pan_path) if pan_path else None,
        "扫描参数": {"候选池上限": opts["pool_size"], "送模型数量": opts["model_top"],
                     "事实包上限": opts["max_chars"], "排除规则": opts["exclude"]},
        "扫描": {"全市场总数": market_total, "候选池数量": len(pool), "排除统计": stats},
        "市场环境": env, "板块背景": heat, "行业景气": ind_agg,
        "大盘": "全市场 %s 只 ｜ 10 年国债 %s%% ｜ 近5日日均成交额 %s 亿 ｜ 近5日日均涨停 %s 家"
                % (market_total or "—", env.get("bond10y"), env.get("amount5"),
                   env.get("limitup5")),
        "候选池": [x["row"] for x in final],
        "机械层": mech_rows, "推荐榜": [], "市场风格": {}, "候选技术": {},
        "机械口径": {"可得总分": mech.COVERED_TOTAL,
                     "缺失": missed,
                     "未覆盖": mech.veto_summary()["未覆盖"],
                     "说明": "机械分 = 实得 ÷ 可得 × 100；缺失项只从分母去掉，不记 0、不摊权；"
                             "阈值照抄《机器打分逻辑.txt》；机械分只作辅助与初筛，排名看模型分"},
        "一票否决": mech.veto_summary(),
        "被否决": vetoed,
        "降级": degrade, "风险与不确定性": [], "免责声明": "",
        "配置": {}, "成本": {}, "用量": {}, "latency_ms": None,
        "factpack_chars": 0, "模型层": {},
        "note": "全大盘扫描 + 模型评分（机械分只作辅助）；模型不给买卖价位，不构成投资建议。",
    }


# ---------------------------------------------------------------------------
# 板块接口（页面已不再调用，只为兼容保留）
# ---------------------------------------------------------------------------

def _board_list(fs, limit=200):
    d = pan._em_json(pan.Ctx(mechdata.PAN_DIR, aiplan.now_local().date(), "post"),
                     pan.EM_CLIST_PATH,
                     {"pn": "1", "pz": str(min(100, limit)), "po": "1", "np": "1",
                      "fltt": "2", "invt": "2", "fid": "f3", "fs": fs,
                      "fields": "f12,f14,f2,f3,f6,f62,f184"})
    rows = ((d or {}).get("data") or {}).get("diff") or []
    if isinstance(rows, dict):
        rows = list(rows.values())
    return [{"代码": str(r.get("f12") or ""), "名称": str(r.get("f14") or ""),
             "涨跌幅_pct": num(r.get("f3")),
             "成交额_亿": None if num(r.get("f6")) is None else round(num(r.get("f6")) / 1e8, 2),
             "主力净流入_亿": None if num(r.get("f62")) is None else round(num(r.get("f62")) / 1e8, 2),
             "主力净占比_pct": num(r.get("f184"))}
            for r in rows if isinstance(r, dict)]


def pick_boards_bundle(refresh=False):
    """兼容接口：板块行情列表（页面已改为全大盘扫描，不再需要筛选）。"""
    boards = {"行业": _board_list(pan.EM_FS_INDUSTRY), "概念": _board_list(pan.EM_FS_CONCEPT)}
    return {"一级行业": boards["行业"], "概念": boards["概念"],
            "主题": [], "缺失行业": [],
            "缓存": {"行业": None, "概念": None},
            "说明": "荐股已改为全大盘扫描，板块筛选停用；这里只返回板块行情供查看。",
            "错误": []}


def pick_l1_subs(code, refresh=False):
    """兼容接口：某板块的成分股（不含机械分）。"""
    d = pan._em_json(pan.Ctx(mechdata.PAN_DIR, aiplan.now_local().date(), "post"),
                     pan.EM_CLIST_PATH,
                     {"pn": "1", "pz": "100", "po": "1", "np": "1", "fltt": "2", "invt": "2",
                      "fid": "f3", "fs": "b:%s" % code,
                      "fields": "f12,f14,f2,f3,f6,f100"})
    rows = ((d or {}).get("data") or {}).get("diff") or []
    if isinstance(rows, dict):
        rows = list(rows.values())
    subs = {}
    for r in rows:
        name = str(r.get("f100") or "未归类")
        subs[name] = subs.get(name, 0) + 1
    return {"代码": code, "细分": [{"名称": k, "候选数": v} for k, v in subs.items()],
            "说明": "荐股已改为全大盘扫描；这里只返回该板块的成分分布。"}, None
