# -*- coding: utf-8 -*-
"""标的跟踪的执行层：取数 → 事实包 → 一次研判档调用（可选复核档）→ 落盘。

模型层写法与 market.py / plancheck.py 完全一致：复用 aiplan 的
load_models / resolve_profile / find_provider / resolve_key / call_model / extract_json /
compute_cost，不新增协议、不加依赖。取消在「每只标的前 / 模型调用前后」检查。
"""

import copy

from . import background
from . import quotes as quotes_mod
from . import track
from .plancheck import cost_of
from .paths import MODELS_PATH, aiplan, now_str, num, rel
from .store import load_account_bundle, load_holdings_bundle, load_tracklist


class _Canceled(Exception):
    """任务被用户中断：不落盘。"""


def clamp_chars(value):
    """事实包字符上限：默认 30000，最小 8000，最大 200000。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = track.MAX_CHARS_DEFAULT
    return max(track.MAX_CHARS_MIN, min(track.MAX_CHARS_MAX, n))


def _settings(opts):
    """解析 profile / provider / model / key（一次解析，全部标的复用）。"""
    cfg, err = aiplan.load_models(MODELS_PATH)
    if err:
        raise RuntimeError(err)
    prof_name, prof, src = aiplan.resolve_profile(cfg, "post", opts.get("profile") or None, None)
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
    return {"cfg": cfg, "名称": prof_name, "profile": prof, "来源": src, "研判": conf,
            "复核档": prof.get("复核"), "provider": provider, "model": model, "key": key,
            "api_base": opts.get("api_base"), "api_key": opts.get("api_key")}


def _holding(holdings, c6, name, price, account):
    """持仓段：直接复用 aiplan.holdings_metrics，口径与 CLI 报告完全一致。"""
    for row in holdings.get("持仓") or []:
        if row.get("代码") == c6:
            return aiplan.holdings_metrics(row.get("原始行") or {}, c6, name, price, account)
    return aiplan.holdings_metrics(None, c6, name, price, account)


def _other_mv(holdings, c6):
    """其它持仓的市值合计（机械校验里的「其它持仓」口径）。"""
    total = 0.0
    for row in holdings.get("持仓") or []:
        if row.get("代码") == c6:
            continue
        total += num(row.get("持仓市值_元")) or 0.0
    return total


def _fail_block(role, model, provider, error):
    return {"called": True, "role": role, "model": model, "ok": False, "usage": {}, "cost": {},
            "json": None, "parse_error": None, "raw_text": "", "error": error,
            "latency_ms": None, "provider": provider}


def call_plan(log, s, fact, ctl, system=None, prompt=None):
    """研判档：返回 (计划 JSON 或 None, 研究段)。失败不抛——产物仍要落盘并标注原因。

    system / prompt 默认用标的跟踪的那套；交易流会传入带「交易流状态」的变体。
    """
    conf = s["研判"]
    provider = s["provider"]
    block = _fail_block("研判", s["model"], provider.get("名称"), None)
    log("    调用 %s（%s）…" % (s["model"], provider.get("协议")))
    try:
        res = aiplan.call_model(
            provider, s["model"], system or track.TRACK_SYSTEM,
            (prompt or track.TRACK_PROMPT) + "\n\n" + fact,
            temperature=conf.get("temperature") if conf.get("temperature") is not None else 0.3,
            max_tokens=conf.get("max_tokens") or 8000,
            json_mode=bool(provider.get("json_object")), key=s["key"], extra=conf.get("参数"))
    except Exception as e:               # noqa: BLE001  网络/协议异常也不能吞掉产物
        block["error"] = "模型调用异常：%s" % str(e)[:400]
        log("[WARN] 模型调用异常：%s" % str(e)[:200])
        return None, block
    usage = res.get("usage") or {}
    block["usage"] = usage
    block["cost"] = cost_of(provider, s["model"], usage, s["cfg"])
    block["latency_ms"] = res.get("latency_ms")
    block["ok"] = bool(res.get("ok"))
    text = res.get("text") or ""
    obj, perr = aiplan.extract_json(text) if block["ok"] else (None, None)
    if isinstance(obj, dict):
        obj = track.normalize_plan(obj)
        block["json"] = obj
        log("[OK] 研判档返回：方向 %s ｜ 置信度 %s" % (obj.get("方向"), obj.get("置信度")))
    else:
        block["error"] = ("模型调用失败：%s" % (res.get("error") or "未知")
                          if not block["ok"] else
                          "模型返回不是合法 JSON：%s" % (perr or "未识别"))
        block["parse_error"] = perr
        block["raw_text"] = text[:20000]
        log("[WARN] %s（产物照常落盘，本次无模型计划）" % block["error"])
    cost = block["cost"].get("人民币_估算")
    log("    usage in %s / out %s ｜ 费用 %s"
        % (usage.get("输入"), usage.get("输出"),
           cost if cost is not None else "未配置单价（仅记录 token）"))
    return block["json"], block


def call_review(log, s, fact, plan_obj, raw_text, ctl):
    """复核档（可选）：质疑一轮，落 payload["复核"]，报告页会自动多出「复核档质询」卡。"""
    rconf = s.get("复核档") or {}
    if not isinstance(rconf, dict) or not rconf.get("provider"):
        log("[WARN] profile 没有可用的复核档，跳过复核")
        return _fail_block("复核", None, None, "profile 没有可用的复核档")
    provider = aiplan.find_provider(s["cfg"], rconf.get("provider"))
    if provider is None:
        log("[WARN] 复核档 provider 不存在：%r" % rconf.get("provider"))
        return _fail_block("复核", None, None, "复核档 provider 不存在：%r" % rconf.get("provider"))
    provider = copy.deepcopy(provider)
    if s.get("api_base"):
        provider["base_url"] = s["api_base"]
    model = rconf.get("model")
    if not model:
        log("[WARN] 复核档没写 model，跳过复核")
        return _fail_block("复核", None, provider.get("名称"), "复核档没写 model")
    sys_p, user_p = aiplan.build_review_messages(fact, plan_obj, raw_text or "")
    log("    调用复核档 %s（%s）…" % (model, provider.get("协议")))
    try:
        res = aiplan.call_model(
            provider, model, sys_p, user_p,
            temperature=rconf.get("temperature") if rconf.get("temperature") is not None else 0.2,
            max_tokens=rconf.get("max_tokens") or 8000,
            json_mode=bool(provider.get("json_object")), key=s["key"], extra=rconf.get("参数"))
    except Exception as e:               # noqa: BLE001
        log("[WARN] 复核档异常：%s" % str(e)[:200])
        return _fail_block("复核", model, provider.get("名称"), "复核档异常：%s" % str(e)[:400])
    usage = res.get("usage") or {}
    obj, perr = aiplan.extract_json(res.get("text") or "") if res.get("ok") else (None, None)
    if isinstance(obj, dict):
        obj = aiplan.normalize_review(obj)
    block = {"called": True, "role": "复核", "model": model, "ok": bool(res.get("ok")),
             "usage": usage,
             "cost": cost_of(provider, model, usage, s["cfg"]),
             "json": obj, "parse_error": perr,
             "raw_text": "" if obj is not None else (res.get("text") or "")[:20000],
             "error": None if obj is not None else ("复核档调用失败：%s" % (res.get("error") or "未知")
                                                    if not res.get("ok") else
                                                    "复核档返回不是合法 JSON：%s" % (perr or "未识别")),
             "latency_ms": res.get("latency_ms"), "provider": provider.get("名称")}
    if obj is None:
        log("[WARN] %s" % block["error"])
    else:
        log("[OK] 复核档返回：推翻=%s ｜ 质询 %d 条"
            % (obj.get("是否推翻结论"), len(obj.get("质询") or [])))
    return block


def _prev_review_block(prev, mech):
    """报告页「上次计划机械回检」卡的形状（执行情况以人工录入为准）。"""
    doc = prev.get("文档")
    if not doc:
        return {"是否有上次计划": False, "说明": "这是该标的的首份跟踪计划。"}
    ex = prev.get("执行摘要") or {}
    return {"是否有上次计划": True, "上次计划日": prev.get("适用交易日"),
            "上次计划来源": prev.get("来源"), "上次置信度": prev.get("置信度"),
            "说明": "执行情况以你录入的为准：%s"
                    % (("已录入（%s）" % ex.get("录入时间")) if ex.get("是否录入")
                       else "本次未录入执行记录"),
            "计划": [{"动作": r.get("动作"), "价格区间": r.get("区间"),
                      "结论": "%s ｜ %s" % (r.get("状态") or "—", r.get("说明") or "")}
                     for r in (mech.get("计划") or [])]}


def run_track(log, ctl, opts):
    """主流程：对选中的跟踪标的一只一只生成计划（顺序执行，可中断）。"""
    opts = opts or {}
    codes = []
    for raw in opts.get("codes") or []:
        c6 = aiplan.code6(raw)
        if c6 and c6 not in codes:
            codes.append(c6)
    if not codes:
        raise RuntimeError("没有可生成的标的：先在跟踪清单里勾选，或先在跟踪页加入标的")
    cap = clamp_chars(opts.get("max_chars"))
    review_on = bool(opts.get("review"))
    apply = track.apply_trade_date()
    log("[..] 适用交易日 %s（%s）" % (apply["适用交易日"], apply["口径"]))
    listed = {it["代码"]: it for it in load_tracklist()}
    s = _settings(opts)
    log("[OK] profile %s（%s）｜ 研判 %s / %s ｜ key %s"
        % (s["名称"], s["来源"], s["provider"].get("名称"), s["model"], aiplan.mask_key(s["key"])))
    log("[..] %s；事实包上限 %d 字符"
        % ("每标的 2 次模型调用（研判 + 复核）" if review_on else "每标的 1 次模型调用（研判档）",
           cap))
    holdings = load_holdings_bundle()
    account = load_account_bundle().get("配置") or {}
    pan_doc, pan_path = background.latest_pan()
    log("[..] pan 快照 %s（交易日 %s）"
        % (rel(pan_path) if pan_path else "—", (pan_doc or {}).get("trade_date") or "—"))
    quote_map, qhints = quotes_mod.fetch_quotes(codes, refresh=True)
    for h in qhints:
        log("[WARN] %s" % h)
    if quote_map:
        log("[OK] 东财批量报价 %d/%d 只（1 次请求）" % (len(quote_map), len(codes)))

    done, skipped, failed = [], [], []
    usage_in = usage_out = 0
    cost_total = 0.0
    priced = False
    for i, c6 in enumerate(codes, 1):
        if ctl.get("cancel"):
            log("[WARN] 已取消：剩余 %d 只未生成" % (len(codes) - i + 1))
            break
        item = listed.get(c6) or {}
        name = item.get("名称") or c6
        log("[..] (%d/%d) %s %s" % (i, len(codes), c6, name))
        prev = track.reference_prev(c6, apply["适用交易日"])
        if prev.get("需要执行记录"):
            log("[WARN] %s 还没录入 %s 计划的执行情况：先填执行记录再生成（本次跳过）"
                % (c6, prev.get("适用交易日")))
            skipped.append({"代码": c6, "名称": name, "原因": "未录入上一份计划的执行情况",
                            "上一份适用交易日": prev.get("适用交易日")})
            continue
        try:
            res = _one(log, ctl, c6, name, item, apply, prev, s, account, holdings,
                       pan_doc, pan_path, quote_map.get(c6), cap, review_on)
        except _Canceled:
            log("[WARN] 已取消：%s 未落盘" % c6)
            break
        except Exception as e:            # noqa: BLE001  单只失败不中断其余标的
            failed.append({"代码": c6, "名称": name, "原因": str(e)[:300]})
            log("[FAIL] %s 生成失败：%s" % (c6, str(e)[:300]))
            continue
        done.append(res)
        usage_in += int((res["usage"] or {}).get("输入") or 0)
        usage_out += int((res["usage"] or {}).get("输出") or 0)
        if res.get("cost") is not None:
            priced = True
            cost_total += float(res["cost"] or 0)
        log("[OK] %s 已落盘 %s" % (c6, res["路径"]))
        if res.get("方向") is not None:
            log("    方向 %s ｜ 置信度 %s ｜ %s"
                % (res.get("方向"), res.get("置信度"), (res.get("一句话结论") or "")[:60]))
        elif res.get("error"):
            log("    [WARN] 本次没有模型计划：%s" % str(res["error"])[:120])
    log("[..] 完成：生成 %d 只 ｜ 跳过 %d 只 ｜ 失败 %d 只"
        % (len(done), len(skipped), len(failed)))
    log("[..] 合计 usage in %d / out %d ｜ 费用 %s"
        % (usage_in, usage_out, ("~CNY %.4f" % cost_total) if priced
           else "未配置单价（仅记录 token）"))
    return {"生成": done, "跳过": skipped, "失败": failed,
            "适用交易日": apply["适用交易日"], "交易日口径": apply["口径"],
            "model": s["model"], "profile": s["名称"], "复核档": bool(review_on),
            "usage": {"输入": usage_in, "输出": usage_out},
            "cost": round(cost_total, 4) if priced else None}


def _one(log, ctl, c6, name, listed, apply, prev, s, account, holdings, pan_doc, pan_path,
         quote, cap, review_on):
    """单只标的：拼事实包 → 研判（→ 复核）→ 落盘。"""
    tech, s3_path = background.stock3d_tech(c6)
    bg = background.market_context(tech, pan_doc, name=name,
                                   pan_rel=rel(pan_path) if pan_path else None)
    brief = track.quote_brief(listed, quote, prev.get("文档"))
    price = brief.get("价格")
    holding = _holding(holdings, c6, name, price, account)
    hints = []
    if not (bg.get("个股量价与形态") or {}):
        hints.append("本地 stock3d 快照里没有 %s 的量价细节（先跑 python stock3d.py pull %s 更准）"
                     % (c6, c6))
    if not brief.get("实时"):
        hints.append("本次价格来源：%s（口径：%s）" % (brief.get("来源"), brief.get("口径")))
    if not pan_doc:
        hints.append("没有 pan 快照：大盘与板块背景缺失")
    if prev.get("说明"):
        hints.append(prev["说明"])
    data = {"标的": {"代码": quotes_mod.thscode_of(c6) or c6, "名称": name},
            "适用交易日": apply["适用交易日"], "交易日口径": apply["口径"],
            "账户": account, "持仓": holding, "实盘": brief, "上一份计划": prev,
            "机械参考": track.mech_reference(prev.get("文档"), quote), "背景": bg}
    fact = track.factpack_sections(data, cap)
    log("    事实包 %s 字符（%d 章节%s）"
        % (fact["字符数"], len(fact["章节"]),
           "，裁剪 " + "、".join(fact["裁剪"]) if fact["裁剪"] else ""))
    if ctl.get("cancel"):
        raise _Canceled()
    plan_obj, research = call_plan(log, s, fact["文本"], ctl)
    if ctl.get("cancel"):
        raise _Canceled()
    plan_for_check = plan_obj if isinstance(plan_obj, dict) else {}
    checks = aiplan.mechanical_checks(account, holding, price, plan_for_check,
                                      holding.get("是否ETF"), _other_mv(holdings, c6))
    review = {"called": False, "role": "复核", "model": None, "ok": False, "usage": {},
              "cost": {}, "json": None, "error": None, "raw_text": ""}
    if review_on:
        review = call_review(log, s, fact["文本"], plan_obj, research.get("raw_text"), ctl)
        if ctl.get("cancel"):
            raise _Canceled()
    prev_plan = ((prev.get("文档") or {}).get("研判") or {}).get("json") or {}
    trade_date = ((pan_doc or {}).get("trade_date") or apply["现在"][:10])
    payload = {
        "tool": "webui-track", "schema_version": track.SCHEMA_VERSION, "phase": "track",
        "generated_at": now_str(), "trade_date": trade_date,
        "适用交易日": apply["适用交易日"], "交易日口径": apply["口径"],
        "标的": {"代码": quotes_mod.thscode_of(c6) or c6, "名称": name,
                 "类型": "etf" if holding.get("是否ETF") else "stock"},
        "profile": {"名称": s["名称"], "来源": s["来源"], "研判": s["研判"], "复核": s["复核档"]},
        "数据": {"pan_path": rel(pan_path) if pan_path else None,
                 "pan_sha1": aiplan.sha1_file(pan_path) if pan_path else None,
                 "stock3d_path": rel(s3_path) if s3_path else None,
                 "stock3d_sha1": aiplan.sha1_file(s3_path) if s3_path else None,
                 "裁剪记录": fact["裁剪"], "降级明细": hints},
        "事实包": {"字符数": fact["字符数"], "sha1": aiplan.sha1_text(fact["文本"]),
                   "章节": fact["章节"], "上限": fact["上限"], "说明": fact["说明"]},
        "上次计划": {"来源": prev.get("来源"), "路径": prev.get("路径"),
                     "适用交易日": prev.get("适用交易日"), "方向": prev_plan.get("方向"),
                     "置信度": prev_plan.get("置信度"),
                     "一句话结论": prev_plan.get("一句话结论"),
                     "计划": track.plan_items(prev.get("文档")),
                     "执行记录": prev.get("执行记录"), "执行摘要": prev.get("执行摘要")},
        "上次计划复盘": _prev_review_block(prev, data["机械参考"]),
        "执行记录摘要": prev.get("执行摘要"),
        "机械参考": data["机械参考"],
        "事实包文本": fact["文本"],
        "研判": research, "复核": review, "机械校验": checks,
        "账户": account, "持仓": holding, "现价": price, "降级": hints,
        "note": "本计划由本地程序采集的数据 + 大模型生成，不构成投资建议。",
    }
    if ctl.get("cancel"):
        raise _Canceled()
    path = track.save_plan(payload, track.render_md(payload))
    return {"代码": c6, "名称": name, "路径": rel(path), "适用交易日": apply["适用交易日"],
            "方向": (plan_obj or {}).get("方向"), "置信度": (plan_obj or {}).get("置信度"),
            "一句话结论": (plan_obj or {}).get("一句话结论"),
            "error": research.get("error"), "usage": research.get("usage") or {},
            "cost": (research.get("cost") or {}).get("人民币_估算")}
