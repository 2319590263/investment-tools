# -*- coding: utf-8 -*-
"""交易流的执行层：生成/重算下一步计划 + 手动体检（意外排查）。

* 计划：复用 track 的取数与模型层，事实包最前面加「交易流状态」（成交与盈亏是权威口径）；
* 体检：读 stock3d 的消息面（公告 / 风险公告 / 快讯 / 调研 / 榜单）+ 量价 + 板块大盘 + 流状态，
  让模型给四档结论；必要时先跑一次 stock3d 抓数（子进程，不花模型钱）；
* 盘中不改计划：本模块只在你点「重算计划 / 体检」的时候跑。
"""

import os
import subprocess

from . import background
from . import alerts as alerts_store
from . import flow as flow_store
from . import quotes as quotes_mod
from . import track
from . import track_run
from .jobs import child_env
from .paths import PYTHON, ROOT, aiplan, now_str, num, rel
from .sources import newest_file, stock3d_snapshot
from .store import load_account_bundle, load_holdings_bundle

FLOW_SYSTEM = (track.TRACK_SYSTEM + "这是「交易流」里的一只标的：从建仓到清仓全程跟踪，"
               "成交明细与盈亏由用户当场记录，是唯一权威的执行口径。同一条流里可能有多只标的，"
               "每只标的各选一种打法，计划只针对当前这一只。")


def plan_prompt(style):
    """按打法拼提示词：打法决定计划的时间尺度、止损宽窄与加减仓节奏。"""
    style = style or flow_store.DEFAULT_STYLE
    hint = flow_store.STYLE_HINT.get(style, "")
    return (track.TRACK_PROMPT + "\n\n【交易流补充要求】\n"
            "1. 本次没有单独的「执行情况」录入表：成交明细就是执行情况，写在【交易流状态】里，"
            "一律以它为准，禁止自行推断是否成交。\n"
            "2. 这只标的的打法是「%s」：%s 计划的持有周期、买卖点间距、止损宽窄、加减仓节奏"
            "都要匹配这个打法，不要给跨打法的建议。\n"
            "3. 计划要服务于这条流的目标收益率与最大亏损（流级参数与这只标的的分配资金都在"
            "【交易流状态】里）：合计盈亏已经达到目标盈利时，明确建议减仓/清仓落袋；"
            "触及最大亏损时明确建议止损离场。\n"
            "4. 已经成交过的价位不要再重复建议建仓；未成交的条目可以继续沿用，或按新数据微调。\n"
            "5. 没有新数据支持时不要为了改动而改动——计划要尽量稳定。" % (style, hint))

CHECK_SYSTEM = ("你是 A 股持仓的「意外排查」助手。用户已经有一条交易计划和一条持仓流，"
                "你要只依据给定数据判断：有没有出现会让计划失效的意外情况"
                "（消息面暴雷、技术面崩溃、基本面出事），以及还要不要接着按计划进行。"
                "禁止编造公告或数值；数据缺失就写进不确定性，不要假装没有坏事。")

CHECK_PROMPT = (
    "下面是本地程序采集的中性数据：一条在跟踪的交易流（参数 / 成交 / 持仓 / 盈亏）、"
    "当前交易计划原文、消息面（巨潮公告与风险公告分类、当日快讯、机构调研、龙虎榜与热度）、"
    "个股量价形态、板块与大盘背景、当前实时快照，以及用户手填的观察。\n\n"
    "请只依据这些数据，输出**一个 JSON 对象**（不要解释文字、不要代码块围栏），字段固定为：\n"
    "{\n"
    ' "是否失效": true 或 false,\n'
    ' "结论": "继续执行|提高警惕|暂停新动作|建议作废重算",\n'
    ' "一句话结论": "一句话说清还能不能按原计划做",\n'
    ' "逐条依据": ["引用具体公告标题/日期/分类或量价数值，并说明它改变了什么"],\n'
    ' "对当前计划的处理建议": "哪条继续、哪条改价、哪条作废",\n'
    ' "风险与失效条件": ["接下来什么情况必须离场或停手"],\n'
    ' "数据依赖与不确定性": {"降级项": ["消息面缺失等"], "缺失导致的不确定性": "..."}\n'
    "}\n\n"
    "要求：\n"
    "1. 「结论」只能四选一：继续执行（没有影响计划的意外）/ 提高警惕（有苗头但计划仍可执行）/ "
    "暂停新动作（不再加仓或开新仓，已持仓按原止损管理）/ 建议作废重算（原计划前提已不成立）。\n"
    "2. 「逐条依据」必须引用给定数据里的具体条目与数值（公告标题、日期、事件分类、量价、板块资金），"
    "不许只给结论；确实没有依据就写「无」并把不确定性写清楚。\n"
    "3. 只判断「意外是否让计划失效」，不新增计划外的买卖建议、不预测涨跌、不构成投资建议。\n"
    "4. 消息面缺失（sources 里 FAIL / 降级、公告为空）必须写明「消息面缺失」，不能当作没有坏事。")


def _num(v, nd=2):
    return "—" if v is None else (("%." + str(int(nd)) + "f") % v)


def flow_state_lines(doc, node, price=None, source=None, when=None):
    """给模型的「交易流状态」章节：流的资金口径 + 这只标的的持仓 / 盈亏 / 成交 / 事件 / 最近体检。"""
    params = doc.get("参数") or {}
    summary = doc.get("汇总") or {}
    pos = node.get("持仓") or {}
    pnl = node.get("盈亏") or {}
    style = node.get("打法") or flow_store.DEFAULT_STYLE
    others = [n for n in flow_store.targets(doc) if n is not node]
    out = ["- 流编号：%s ｜ 流状态：%s ｜ 创建：%s（起始日 %s）｜ 流内标的 %d 只"
           % (doc.get("流编号"), doc.get("状态"), doc.get("创建时间"), doc.get("起始日"),
              len(flow_store.targets(doc))),
           "- 流级参数（资金是流的属性）：流资金 %s 元 ｜ 目标收益率 %s%%（整条流目标盈利 %s 元）"
           "｜ 最大亏损 %s%%（整条流最大亏损 %s 元）｜ 流内分配资金合计 %s 元"
           % (_num(params.get("流资金")), _num(params.get("目标收益率_pct")),
              _num(summary.get("目标盈利_元")), _num(params.get("最大亏损_pct")),
              _num(summary.get("最大亏损_元")), _num(summary.get("分配合计"))),
           "- 本标的：%s %s ｜ 打法：%s（%s）｜ 分配资金 %s 元（这只标的的收益率与最大亏损按它算）"
           "｜ 加入日 %s"
           % (node.get("名称") or "—", node.get("代码") or "—", style,
              flow_store.STYLE_HINT.get(style, ""), _num(node.get("分配资金")),
              node.get("加入日") or "—"),
           "- 持仓：%s 股（可用 %s）｜ 平均成本 %s ｜ 期初：%s"
           % (_num(pos.get("股数"), 0), _num(pos.get("可用"), 0),
              _num(pos.get("平均成本"), 4), (node.get("期初") or {}).get("说明") or "—"),
           "- 本标的盈亏：已实现 %s ｜ 浮动 %s ｜ 合计 %s 元 ｜ 收益率 %s%%（按投入成本 %s%%）"
           " ｜ 目标进度 %s%%"
           % (_num(pnl.get("已实现_元")), _num(pnl.get("浮动_元")), _num(pnl.get("合计_元")),
              _num(pnl.get("收益率_pct")), _num(pnl.get("按成本收益率_pct")),
              _num(pnl.get("进度_pct"))),
           "- 现价：%s（%s ｜ %s）"
           % (_num(price if price is not None else pnl.get("现价"), 3),
              source or pnl.get("价格来源") or "—", when or pnl.get("价格时间") or "—"),
           "- 成交明细（权威口径，按时间升序）："]
    fills = node.get("成交") or []
    if not fills:
        out.append("  （还没有成交：这是空仓开流，先出建仓计划）")
    for f in fills:
        out.append("  %s) %s %s %s 股 @ %s ｜ 金额 %s 元 ｜ 手续费 %s 元 ｜ 来源：%s%s"
                   % (f.get("序号"), f.get("日期"), f.get("方向"), _num(f.get("数量"), 0),
                      _num(f.get("价格"), 4), _num(f.get("金额")), _num(f.get("手续费")),
                      f.get("来源"),
                      (" ｜ 备注：%s" % f.get("备注")) if f.get("备注") else ""))
    if others:
        out.append("- 同一条流里的其他标的（只看不改，各自独立判定）：")
        for o in others:
            o_pnl = o.get("盈亏") or {}
            out.append("  · %s %s ｜ 打法 %s ｜ 分配资金 %s 元 ｜ 持仓 %s 股 ｜ 合计盈亏 %s 元"
                       % (o.get("名称") or "—", o.get("代码") or "—",
                          o.get("打法") or flow_store.DEFAULT_STYLE, _num(o.get("分配资金")),
                          _num((o.get("持仓") or {}).get("股数"), 0), _num(o_pnl.get("合计_元"))))
    events = list(node.get("事件") or [])[-5:]
    if events:
        out.append("- 最近事件：")
        for e in events:
            out.append("  · %s %s：%s" % (e.get("时间"), e.get("类型"), e.get("文案")))
    checks = node.get("体检") or []
    if checks:
        last = checks[-1]
        out.append("- 最近一次体检：%s —— %s（%s）"
                   % (last.get("结论"), last.get("一句话") or "—", last.get("时间")))
    return "\n".join(out)


def _holding_from_flow(node, c6, name, price, account):
    """把这只标的的持仓折成 aiplan 的持仓口径（机械校验要用）。"""
    pos = node.get("持仓") or {}
    qty = num(pos.get("股数")) or 0.0
    if qty <= 0:
        return aiplan.holdings_metrics(None, c6, name, price, account)
    avail = num(pos.get("可用")) or 0.0
    row = {"股票余额": qty, "可用余额": avail, "冻结数量": max(0.0, qty - avail),
           "成本价": num(pos.get("平均成本")), "市价": price}
    return aiplan.holdings_metrics(row, c6, name, price, account)


def _prev_payload(node):
    """这只标的上挂着的上一份计划产物（读回 json），没有就返回 (None, None)。"""
    path = (node.get("计划") or {}).get("产物路径")
    if not path:
        return None, None
    full = path if os.path.isabs(path) else os.path.join(ROOT, path)
    doc_json = aiplan.read_json(full)
    return doc_json, full


def run_plan(log, ctl, opts):
    """生成 / 重算这条流里标的的计划（每只标的 1 次研判档调用，可选复核档）。"""
    fid = str((opts or {}).get("流编号") or "").strip()
    doc, err = flow_store.load_flow(fid)
    if err:
        raise RuntimeError(err)
    if doc.get("状态") != flow_store.ACTIVE_STATE:
        raise RuntimeError("这条流已经结束（%s），不再重算计划" % doc.get("状态"))
    codes = [aiplan.code6(c) for c in ((opts or {}).get("代码") or []) if aiplan.code6(c)]
    nodes = []
    for c6 in codes:
        node, err = flow_store.find_target(doc, c6)
        if err:
            raise RuntimeError(err)
        nodes.append(node)
    if not codes:
        nodes = flow_store.active_targets(doc)
    if not nodes:
        raise RuntimeError("这条流里还没有标的：先在「交易流」页加一只标的")
    log("[..] 交易流 %s ｜ 本次 %d 只标的：%s"
        % (fid, len(nodes), "、".join("%s %s" % (n.get("代码"), n.get("名称")) for n in nodes)))
    out = []
    for node in nodes:
        if ctl.get("cancel"):
            raise RuntimeError("已取消")
        out.append(_plan_one(log, ctl, doc, node, opts))
    bad = [r for r in out if r.get("error")]
    return {"流编号": fid, "计划": out, "计划数": len(out), "成功": len(out) - len(bad),
            "方向": out[0].get("方向") if len(out) == 1 else None,
            "置信度": out[0].get("置信度") if len(out) == 1 else None,
            "error": ("%d 只标的的计划没出来：%s"
                      % (len(bad), "；".join("%s %s" % (r.get("代码"), str(r.get("error"))[:80])
                                            for r in bad))) if bad else None}


def _plan_one(log, ctl, doc, node, opts):
    """给一只标的生成 / 重算计划：事实包最前面是「交易流状态」（成交与盈亏是权威口径）。"""
    fid = doc.get("流编号")
    c6 = aiplan.code6(node.get("代码") or "")
    name = node.get("名称") or c6
    style = node.get("打法") or flow_store.DEFAULT_STYLE
    kind = "重算计划" if node.get("计划") else "首份计划"
    apply = track.apply_trade_date()
    log("[..] %s %s ｜ 打法 %s ｜ %s" % (c6, name, style, kind))
    log("[..] 适用交易日 %s（%s）" % (apply["适用交易日"], apply["口径"]))
    s = track_run._settings(opts or {})
    log("[OK] profile %s（%s）｜ 研判 %s / %s ｜ key %s"
        % (s["名称"], s["来源"], s["provider"].get("名称"), s["model"],
           aiplan.mask_key(s["key"])))
    account = load_account_bundle().get("配置") or {}
    holdings = load_holdings_bundle()
    pan_doc, pan_path = background.latest_pan()
    log("[..] pan 快照 %s（交易日 %s）"
        % (rel(pan_path) if pan_path else "—", (pan_doc or {}).get("trade_date") or "—"))
    quote_map, qhints = quotes_mod.fetch_quotes([c6], refresh=True)
    quote = quote_map.get(c6)
    for h in qhints:
        log("[WARN] %s" % h)
    added = flow_store.sync_ledger(node, account=account, log=log)
    if added:
        log("[OK] 交易台账并入 %d 笔成交" % added)
    brief = track.quote_brief(None, quote, None)
    price = num(brief.get("价格"))
    flow_store.recompute(node, account=account)
    flow_store.apply_price(node, price, brief.get("来源"), brief.get("时间"))
    log("[OK] 持仓 %s 股 ｜ 合计盈亏 %s 元 ｜ 目标进度 %s%%"
        % (_num((node.get("持仓") or {}).get("股数"), 0), _num((node.get("盈亏") or {}).get("合计_元")),
           _num((node.get("盈亏") or {}).get("进度_pct"))))
    prev_doc, prev_path = _prev_payload(node)
    mech = track.mech_reference(prev_doc, quote)
    tech, s3_path = background.stock3d_tech(c6)
    bg = background.market_context(tech, pan_doc, name=name,
                                   pan_rel=rel(pan_path) if pan_path else None)
    hints = []
    if not (bg.get("个股量价与形态") or {}):
        hints.append("本地 stock3d 快照里没有 %s 的量价细节（先跑 python stock3d.py pull %s 更准）"
                     % (c6, c6))
    if price is None:
        hints.append("本次没取到实时价：现值口径用最近成交价")
    if not pan_doc:
        hints.append("没有 pan 快照：大盘与板块背景缺失")
    data = {"标的": {"代码": quotes_mod.thscode_of(c6) or c6, "名称": name},
            "适用交易日": apply["适用交易日"], "交易日口径": apply["口径"],
            "打法": style, "打法规格": flow_store.STYLE_HINT.get(style),
            "账户": account,
            "持仓": _holding_from_flow(node, c6, name, price, account),
            "实盘": brief,
            "上一份计划": {"来源": "跟踪产物" if prev_doc else None, "路径": rel(prev_path) if prev_path else None,
                       "适用交易日": ((prev_doc or {}).get("适用交易日")),
                       "文档": prev_doc, "执行记录": flow_store.exec_record(node),
                       "执行摘要": track.exec_summary(flow_store.exec_record(node)),
                       "需要执行记录": False, "说明": None},
            "机械参考": mech, "背景": bg}
    fact = track.factpack_sections(data, track_run.clamp_chars((opts or {}).get("max_chars")),
                                   front=[("交易流状态（成交与盈亏，权威口径）",
                                           flow_state_lines(doc, node, price, brief.get("来源"),
                                                            brief.get("时间")))])
    log("    事实包 %s 字符（%d 章节%s）"
        % (fact["字符数"], len(fact["章节"]),
           "，裁剪 " + "、".join(fact["裁剪"]) if fact["裁剪"] else ""))
    if ctl.get("cancel"):
        raise RuntimeError("已取消")
    plan_obj, research = track_run.call_plan(log, s, fact["文本"], ctl,
                                             system=FLOW_SYSTEM, prompt=plan_prompt(style))
    if ctl.get("cancel"):
        raise RuntimeError("已取消")
    is_etf = bool(node.get("是否ETF"))
    checks = aiplan.mechanical_checks(account, data["持仓"], price, plan_obj or {}, is_etf,
                                      track_run._other_mv(holdings, c6))
    review = {"called": False, "role": "复核", "model": None, "ok": False, "usage": {},
              "cost": {}, "json": None, "error": None, "raw_text": ""}
    if (opts or {}).get("review"):
        review = track_run.call_review(log, s, fact["文本"], plan_obj,
                                       research.get("raw_text"), ctl)
    payload = {
        "tool": "webui-track", "schema_version": track.SCHEMA_VERSION, "phase": "track",
        "generated_at": now_str(),
        "trade_date": ((pan_doc or {}).get("trade_date") or apply["现在"][:10]),
        "适用交易日": apply["适用交易日"], "交易日口径": apply["口径"],
        "打法": style, "打法规格": flow_store.STYLE_HINT.get(style),
        "标的": {"代码": quotes_mod.thscode_of(c6) or c6, "名称": name,
                 "类型": "etf" if is_etf else "stock"},
        "profile": {"名称": s["名称"], "来源": s["来源"], "研判": s["研判"],
                    "复核": s.get("复核档")},
        "数据": {"pan_path": rel(pan_path) if pan_path else None,
                 "pan_sha1": aiplan.sha1_file(pan_path) if pan_path else None,
                 "stock3d_path": rel(s3_path) if s3_path else None,
                 "stock3d_sha1": aiplan.sha1_file(s3_path) if s3_path else None,
                 "裁剪记录": fact["裁剪"], "降级明细": hints},
        "事实包": {"字符数": fact["字符数"], "sha1": aiplan.sha1_text(fact["文本"]),
                   "章节": fact["章节"], "上限": fact["上限"], "说明": fact["说明"]},
        "交易流": {"流编号": fid, "流资金": (doc.get("参数") or {}).get("流资金"),
                   "目标收益率_pct": (doc.get("参数") or {}).get("目标收益率_pct"),
                   "最大亏损_pct": (doc.get("参数") or {}).get("最大亏损_pct"),
                   "流内标的数": len(flow_store.targets(doc)),
                   "本标的": {"代码": c6, "名称": name, "打法": style,
                            "分配资金": node.get("分配资金")},
                   "流内其他标的": [s for s in flow_store.symbols(doc)
                                if aiplan.code6(s.get("代码") or "") != c6],
                   "持仓": node.get("持仓"), "盈亏": node.get("盈亏"),
                   "成交笔数": len(node.get("成交") or [])},
        "上次计划": {"来源": "跟踪产物" if prev_doc else None,
                     "路径": rel(prev_path) if prev_path else None,
                     "适用交易日": (prev_doc or {}).get("适用交易日"),
                     "方向": ((prev_doc or {}).get("研判") or {}).get("json", {}).get("方向"),
                     "计划": track.plan_items(prev_doc),
                     "执行摘要": track.exec_summary(flow_store.exec_record(node))},
        "上次计划复盘": {"是否有上次计划": bool(prev_doc),
                         "说明": "执行情况以流内成交明细为准（见【交易流状态】）",
                         "计划": [{"动作": r.get("动作"), "价格区间": r.get("区间"),
                                   "结论": "%s ｜ %s" % (r.get("状态") or "—", r.get("说明") or "")}
                                  for r in (mech.get("计划") or [])]},
        "机械参考": mech, "事实包文本": fact["文本"],
        "研判": research, "复核": review, "机械校验": checks,
        "账户": account, "持仓": data["持仓"], "现价": price, "降级": hints,
        "note": "本计划由本地程序采集的数据 + 大模型生成，不构成投资建议。",
    }
    path = track.save_plan(payload, track.render_md(payload))
    flow_store.set_plan(node, payload, path, kind=kind)
    flow_store.save_flow(doc)
    log("[OK] %s 已落盘 %s" % (kind, rel(path)))
    if research.get("error"):
        log("[WARN] 本次没有模型计划：%s" % str(research["error"])[:160])
    return {"流编号": fid, "代码": c6, "名称": name, "打法": style,
            "计划": rel(path), "方向": (plan_obj or {}).get("方向"),
            "置信度": (plan_obj or {}).get("置信度"),
            "一句话结论": (plan_obj or {}).get("一句话结论"),
            "error": research.get("error"), "usage": research.get("usage") or {},
            "cost": (research.get("cost") or {}).get("人民币_估算")}


# ---------------------------------------------------------------------------
# 体检：意外排查（唯一在你点的时候才发生的模型调用）
# ---------------------------------------------------------------------------

def _stock3d_symbol(code):
    """最新 stock3d 快照里该标的的整块节点（tech + news）→ (节点, 路径, 快照日期)。"""
    c6 = aiplan.code6(code or "")
    path, doc, _codes = stock3d_snapshot()
    for sym in (doc or {}).get("symbols") or []:
        if aiplan.code6(sym.get("code")) == c6:
            return sym, path, (doc or {}).get("target_date")
    return None, path, (doc or {}).get("target_date")


def _pull(log, ctl, code, mode="pull"):
    """跑一次 stock3d 抓数（pull = 三维全出；news = 只抓消息面）。不花模型钱。"""
    argv = [PYTHON, os.path.join(ROOT, "stock3d.py"), mode, code]
    log("[..] 先抓数据：python stock3d.py %s %s" % (mode, code))
    proc = subprocess.Popen(argv, cwd=ROOT, env=child_env(), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", bufsize=1)
    ctl["proc"] = proc
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if line.strip():
                log("    " + line)
    except Exception as e:                 # noqa: BLE001  读输出被打断也算抓数失败
        log("[WARN] 抓数输出读取中断：%s" % e)
    finally:
        rc = proc.wait()
        ctl["proc"] = None
    log("[%s] stock3d %s 结束（退出码 %s）" % ("OK" if rc == 0 else "WARN", mode, rc))
    return rc


def _json_dump(node):
    import json
    return json.dumps(node, ensure_ascii=False)


def check_factpack(doc, node, news, bg, brief, note, s3_path, s3_date, price, cap=30000):
    """体检事实包：流与标的的状态 + 计划 + 消息面 + 量价 + 板块大盘 + 用户补充。"""
    plan = node.get("计划") or {}
    sections = [("体检对象与交易流状态", flow_state_lines(
        doc, node, (brief or {}).get("价格") if brief else price,
        (brief or {}).get("来源"), (brief or {}).get("时间")))]
    rows = plan.get("条目") or []
    plan_lines = ["- 计划产物：%s（生成于 %s ｜ 适用交易日 %s）"
                  % (plan.get("产物路径") or "—", plan.get("生成时间") or "—",
                     plan.get("适用交易日") or "—"),
                  "- 方向：%s ｜ 置信度：%s" % (plan.get("方向") or "—", plan.get("置信度") or "—"),
                  "- 一句话结论：%s" % (plan.get("一句话结论") or "—")]
    for r in rows:
        plan_lines.append("  %s) %s ｜ 触发条件：%s ｜ 价格区间：%s ｜ 股数：%s ｜ 失效条件：%s"
                          % (r.get("编号"), r.get("动作") or "—", r.get("触发条件") or "—",
                             r.get("价格区间") or "—", r.get("股数") or "—",
                             r.get("失效条件") or "—"))
    levels = plan.get("关键价位") or {}
    plan_lines.append("- 关键价位：止损 %s ｜ 目标 %s ｜ 支撑 %s ｜ 压力 %s"
                      % (_num(levels.get("止损")),
                         "、".join(_num(g.get("价位")) for g in (levels.get("目标") or [])) or "—",
                         "、".join(_num(g.get("价位")) for g in (levels.get("支撑") or [])) or "—",
                         "、".join(_num(g.get("价位")) for g in (levels.get("压力") or [])) or "—"))
    if plan.get("错误"):
        plan_lines.append("- 上次生成计划时的错误：%s" % plan["错误"])
    sections.append(("当前交易计划原文", "\n".join(plan_lines)))
    sections.append(("消息面（stock3d）", _news_text(news, s3_path, s3_date)))
    bg = bg or {}
    if bg.get("个股量价与形态"):
        sections.append(("个股量价与形态", _json_dump(bg["个股量价与形态"])))
    if bg.get("板块"):
        sections.append(("板块", _json_dump(bg["板块"])))
    if bg.get("大盘"):
        sections.append(("大盘", _json_dump(bg["大盘"])))
    if (note or "").strip():
        sections.append(("用户手填的观察与异常", str(note).strip()))
    drop_order = ("板块", "大盘", "个股量价与形态")
    dropped = []

    def total():
        return sum(len(t) + len(c) + 8 for t, c in sections)

    while total() > int(cap) and any(t in drop_order for t, _c in sections):
        for title in drop_order:
            hit = [i for i, (t, _c) in enumerate(sections) if t == title]
            if hit:
                dropped.append(sections.pop(hit[0])[0])
                break
    text = "\n\n".join("【%s】\n%s" % (t, c) for t, c in sections)
    if dropped:
        text += "\n\n【预算裁剪】\n- 因 %d 字符上限被裁掉：%s" % (int(cap), "、".join(dropped))
    return text


def _news_text(news, s3_path, s3_date):
    news = news or {}
    out = ["- 快照：%s（数据日期 %s ｜ 消息面抓取于 %s）"
           % (rel(s3_path) if s3_path else "—", s3_date or "—", news.get("fetched_at") or "—")]
    sources = news.get("sources") or {}
    if sources:
        out.append("- 各源状态：" + " ｜ ".join("%s %s" % (k, v) for k, v in sources.items()))
    notices = news.get("notices") or {}
    items = notices.get("items") or []
    out.append("- 公告（%s，共 %d 条）：%s"
               % (notices.get("window") or "近 30 天", len(items), notices.get("note") or ""))
    for n in items[:20]:
        out.append("  · %s ｜ %s ｜ %s%s"
                   % (n.get("时间") or "—", n.get("事件分类") or "其他",
                      (n.get("标题") or "")[:80], "（近 7 天）" if n.get("近7天") else ""))
    if not items:
        out.append("  （近 30 天没有公告；若 sources.公告 是 FAIL 或降级，按「消息面缺失」处理）")
    risk = news.get("risk_notices") or {}
    risk_items = risk.get("列表") or []
    out.append("- 风险公告：%s 条" % (risk.get("条数") if risk.get("条数") is not None
                                else len(risk_items)))
    for n in risk_items[:10]:
        out.append("  · %s" % (n.get("标题") if isinstance(n, dict) else n))
    flash = news.get("flash") or []
    if flash:
        out.append("- 当日快讯命中（%d 条，最多列 10 条）：" % len(flash))
        for f in flash[:10]:
            body = str(f.get("content") or "").replace("\n", " ")[:120]
            out.append("  · %s ｜ %s ｜ %s"
                       % (f.get("时间") or "—", (f.get("title") or "")[:60], body))
    tags = news.get("tags") or {}
    if tags:
        out.append("- 消息面摘要：" + " ｜ ".join("%s %s" % (k, v) for k, v in tags.items()))
    lhb = ((news.get("lhb") or {}).get("items") or [])
    if lhb:
        last = lhb[0]
        out.append("- 最近龙虎榜：%s 净买 %s 亿（%s）"
                   % (last.get("日期"), _num(last.get("净买_亿"), 4), last.get("上榜原因")))
    contracts = news.get("contracts") or {}
    if contracts.get("列表"):
        out.append("- 合同/订单类公告标题：%d 条" % len(contracts["列表"]))
        for c in contracts["列表"][:5]:
            out.append("  · %s" % (c.get("标题") if isinstance(c, dict) else c))
    degrade = news.get("degrade") or []
    if degrade:
        out.append("- 消息面降级：" + "；".join(str(x)[:80] for x in degrade[:6]))
    if news.get("north_note"):
        out.append("- 口径说明：%s" % news["north_note"])
    return "\n".join(out)


CHECK_LEVEL_SET = ("继续执行", "提高警惕", "暂停新动作", "建议作废重算")


def normalize_check(obj):
    """体检输出的兜底：结论必须落在四档里，缺字段补空。"""
    if not isinstance(obj, dict):
        return None
    out = dict(obj)
    level = str(out.get("结论") or "").strip()
    if level not in CHECK_LEVEL_SET:
        level = "提高警惕" if out.get("是否失效") else "继续执行"
    out["结论"] = level
    out.setdefault("是否失效", bool(out.get("是否失效")))
    if not isinstance(out.get("逐条依据"), list):
        out["逐条依据"] = [out["逐条依据"]] if out.get("逐条依据") else []
    if not isinstance(out.get("风险与失效条件"), list):
        out["风险与失效条件"] = ([out["风险与失效条件"]]
                             if out.get("风险与失效条件") else [])
    out.setdefault("一句话结论", None)
    out.setdefault("对当前计划的处理建议", None)
    if not isinstance(out.get("数据依赖与不确定性"), dict):
        out["数据依赖与不确定性"] = {}
    return out


def _call_check(log, s, fact, ctl):
    """体检用的模型调用（研判档；输出是四档结论，不走计划规范化）。"""
    conf = s["研判"]
    provider = s["provider"]
    block = track_run._fail_block("体检", s["model"], provider.get("名称"), None)
    log("    调用 %s（%s）…" % (s["model"], provider.get("协议")))
    try:
        res = aiplan.call_model(
            provider, s["model"], CHECK_SYSTEM, CHECK_PROMPT + "\n\n" + fact,
            temperature=(conf.get("temperature") if conf.get("temperature") is not None else 0.2),
            max_tokens=conf.get("max_tokens") or 8000,
            json_mode=bool(provider.get("json_object")), key=s["key"], extra=conf.get("参数"))
    except Exception as e:                 # noqa: BLE001
        block["error"] = "模型调用异常：%s" % str(e)[:400]
        log("[WARN] 模型调用异常：%s" % str(e)[:200])
        return None, block
    usage = res.get("usage") or {}
    block["usage"] = usage
    block["cost"] = track_run.cost_of(provider, s["model"], usage, s["cfg"])
    block["latency_ms"] = res.get("latency_ms")
    block["ok"] = bool(res.get("ok"))
    obj, perr = aiplan.extract_json(res.get("text") or "") if block["ok"] else (None, None)
    if isinstance(obj, dict):
        obj = normalize_check(obj)
        block["json"] = obj
        log("[OK] 体检结论：%s ｜ 是否失效 %s" % (obj.get("结论"), obj.get("是否失效")))
        for line in (obj.get("逐条依据") or [])[:5]:
            log("    · %s" % str(line)[:120])
    else:
        block["error"] = ("模型调用失败：%s" % (res.get("error") or "未知")
                          if not block["ok"] else
                          "模型返回不是合法 JSON：%s" % (perr or "未识别"))
        block["raw_text"] = (res.get("text") or "")[:20000]
        log("[WARN] %s" % block["error"])
    cost = (block.get("cost") or {}).get("人民币_估算")
    log("    usage in %s / out %s ｜ 费用 %s"
        % (usage.get("输入"), usage.get("输出"),
           cost if cost is not None else "未配置单价（仅记录 token）"))
    return block.get("json"), block


def run_check(log, ctl, opts):
    """体检：先（可选）抓数，再把「标的与流状态 + 计划 + 消息面 + 量价 + 板块大盘」交给模型判定。"""
    fid = str((opts or {}).get("流编号") or "").strip()
    doc, err = flow_store.load_flow(fid)
    if err:
        raise RuntimeError(err)
    codes = [aiplan.code6(c) for c in ((opts or {}).get("代码") or []) if aiplan.code6(c)]
    node = None
    if codes:
        node, err = flow_store.find_target(doc, codes[0])
        if err:
            raise RuntimeError(err)
    else:
        pool = flow_store.active_targets(doc) or flow_store.targets(doc)
        if len(pool) != 1:
            raise RuntimeError("这条流里有 %d 只标的，体检要指明「代码」" % len(pool))
        node = pool[0]
    c6 = aiplan.code6(node.get("代码") or "")
    name = node.get("名称") or c6
    note = str((opts or {}).get("补充说明") or "").strip()
    pre = str((opts or {}).get("先抓") or "").strip()
    log("[..] 体检：%s %s（流 %s ｜ 打法 %s）"
        % (c6, name, fid, node.get("打法") or flow_store.DEFAULT_STYLE))
    if pre in ("pull", "news"):
        _pull(log, ctl, c6, pre)
        if ctl.get("cancel"):
            raise RuntimeError("已取消")
    s = track_run._settings(opts or {})
    log("[OK] profile %s（%s）｜ 研判 %s / %s ｜ key %s"
        % (s["名称"], s["来源"], s["provider"].get("名称"), s["model"],
           aiplan.mask_key(s["key"])))
    s3_node, s3_path, s3_date = _stock3d_symbol(c6)
    news = (s3_node or {}).get("news") or {}
    tech = (s3_node or {}).get("tech") or {}
    if not news:
        log("[WARN] 本地快照里没有该标的的消息面：按「消息面缺失」处理（可先抓一次数据）")
    pan_doc, pan_path = background.latest_pan()
    quote_map, qhints = quotes_mod.fetch_quotes([c6], refresh=True)
    quote = quote_map.get(c6)
    for h in qhints:
        log("[WARN] %s" % h)
    account = load_account_bundle().get("配置") or {}
    flow_store.sync_ledger(node, account=account, log=log)
    brief = track.quote_brief(None, quote, None)
    price = num(brief.get("价格"))
    flow_store.recompute(node, account=account)
    flow_store.apply_price(node, price, brief.get("来源"), brief.get("时间"))
    bg = background.market_context(tech, pan_doc, name=name,
                                   pan_rel=rel(pan_path) if pan_path else None)
    fact = check_factpack(doc, node, news, bg, brief, note, s3_path, s3_date, price)
    log("    事实包 %s 字符（消息面 %s 条公告 / %s 条快讯）"
        % (len(fact), len(((news.get("notices") or {}).get("items") or [])),
           len(news.get("flash") or [])))
    if ctl.get("cancel"):
        raise RuntimeError("已取消")
    result, block = _call_check(log, s, fact, ctl)
    rec = flow_store.add_check(node, result or {}, note=note, model=s["model"],
                               usage=block.get("usage"),
                               cost=(block.get("cost") or {}).get("人民币_估算"),
                               error=block.get("error"), fact_chars=len(fact))
    if rec["结论"] != "继续执行" or block.get("error"):
        alerts_store.append([{
            "时间": now_str(), "代码": c6, "名称": name, "点位类型": "体检",
            "价位": price, "手数文本": "",
            "文案": "体检：%s —— %s" % (rec["结论"],
                                    rec.get("一句话") or block.get("error") or "（无结论）"),
            "报告路径": (node.get("计划") or {}).get("产物路径"),
            "级别": flow_store.CHECK_LEVELS.get(rec["结论"], "warn"), "流编号": fid}])
    flow_store.save_flow(doc)
    return {"流编号": fid, "代码": c6, "名称": name, "结论": rec["结论"],
            "是否失效": rec["是否失效"],
            "一句话结论": rec.get("一句话"), "依据": rec.get("依据") or [],
            "处理建议": rec.get("处理建议"), "风险": rec.get("风险") or [],
            "数据依赖": rec.get("数据依赖") or {}, "时间": rec.get("时间"),
            "error": block.get("error"), "usage": block.get("usage") or {},
            "cost": (block.get("cost") or {}).get("人民币_估算")}
