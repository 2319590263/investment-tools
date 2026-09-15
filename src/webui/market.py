# -*- coding: utf-8 -*-
"""页面数据组装：大盘快照、代码候选、K 线 + 大盘走势预测（复用 aiplan 模型层）。"""

import copy
import json
import os
import sys
from .plancheck import cost_of

from .archive import list_reports, plan_log_entries
from .paths import (HISTORY_DIR, MARKET_DIR, MODELS_PATH, PHASES, PHASE_LABEL, POOL_PATH,
                    PYTHON, ROOT, aiplan, atomic_write, now_str, num, rel)
from .sources import newest_file, pan_files, stock3d_files, stock3d_snapshot
from .store import load_account_bundle, load_holdings_bundle, load_models_bundle, load_watchlist


def build_market():
    pan_path = newest_file(pan_files())
    s3_path = newest_file(stock3d_files())
    pandoc = aiplan.read_json(pan_path) if pan_path else None
    s3doc = aiplan.read_json(s3_path) if s3_path else None
    symbols = []
    for sym in (s3doc or {}).get("symbols") or []:
        snap = ((sym.get("tech") or {}).get("snapshot") or {})
        symbols.append({
            "代码": sym.get("code"),
            "名称": sym.get("name"),
            "类型": sym.get("type"),
            "现价": snap.get("现价"),
            "涨跌幅_pct": snap.get("涨跌幅_pct"),
        })
    return {
        "pan": {
            "路径": rel(pan_path) if pan_path else None,
            "mtime": os.path.getmtime(pan_path) if pan_path else None,
            "doc": pandoc,
        },
        "stock3d": {
            "路径": rel(s3_path) if s3_path else None,
            "mtime": os.path.getmtime(s3_path) if s3_path else None,
            "target_date": (s3doc or {}).get("target_date"),
            "generated_at": (s3doc or {}).get("generated_at"),
            "symbols": symbols,
        },
    }


def build_symbols():
    """运行页的代码候选：持仓 + 最新 stock3d + 历史报告 + pan 状态里出现过的标的。"""
    seen, out = set(), []

    s3, doc, fresh_list = stock3d_snapshot()
    fresh = set(fresh_list)

    def add(code, name, source):
        c6 = aiplan.code6(code or "")
        if not c6 or c6 in seen:
            return
        seen.add(c6)
        out.append({"代码": c6, "thscode": code, "名称": name, "来源": source})

    _, rows = aiplan.parse_pool_rows(POOL_PATH)
    for r in rows:
        add(r.get("证券代码"), r.get("证券名称"), "持仓")
    for it in load_watchlist():
        add(it["代码"], it["名称"], "自选")
    for sym in (doc or {}).get("symbols") or []:
        add(sym.get("code"), sym.get("name"), "stock3d")
    for rec in plan_log_entries():
        plan = rec.get("标的") or {}
        add(plan.get("代码"), plan.get("名称"), "历史")
    # 最新 stock3d 快照里有的排前面：默认选中它就能直接 --no-fetch 零成本试跑
    out.sort(key=lambda x: 0 if x["代码"] in fresh else 1)
    for it in out:
        it["在最新stock3d"] = it["代码"] in fresh
    return out


def build_state():
    account = load_account_bundle()
    models = load_models_bundle()
    holdings = load_holdings_bundle()
    reports = list_reports()
    _s3p, _s3doc, _s3codes = stock3d_snapshot()
    return {
        "根目录": ROOT,
        "python": PYTHON,
        "python版本": sys.version.split()[0],
        "时间": now_str(),
        "账户": account,
        "模型": models,
        "持仓": holdings,
        "报告数": len(reports),
        "最新报告": reports[0] if reports else None,
        "代码候选": build_symbols(),
        "自选股": load_watchlist(),
        "stock3d": {
            "路径": rel(_s3p) if _s3p else None,
            "target_date": (_s3doc or {}).get("target_date"),
            "generated_at": (_s3doc or {}).get("generated_at"),
            "代码": _s3codes,
        },
        "phases": [{"值": p, "标签": PHASE_LABEL[p]} for p in PHASES],
        "aiplan": {
            "schema_version": aiplan.SCHEMA_VERSION,
            "prompt_version": aiplan.PROMPT_VERSION,
            "默认最大输入字符": aiplan.DEFAULT_MAX_INPUT_CHARS,
            "默认榜单条数": aiplan.DEFAULT_TOP,
        },
    }


MARKET_SYSTEM = "你是 A 股大盘研判助手，只依据给定事实数据做走势推演，不编造数值，不给出具体买卖指令。"


MARKET_PROMPT = """下面是 pan.py 采集的中性事实数据（只有数值与口径，没有买卖建议）。
请只输出一个合法 JSON 对象：不要解释文字、不要 markdown 代码围栏、不要注释、不要尾随逗号。

JSON 结构：
{
  "方向": "偏多|偏空|中性",
  "置信度": 0-100 的整数,
  "多空评分": -100 到 100 的整数（正=偏多，负=偏空）,
  "时间窗": "如 1-3 个交易日",
  "一句话结论": "40-80 字，点明主要矛盾与应对思路",
  "情景树": [
    {"情形": "乐观|中性|悲观", "概率_pct": 数字, "路径": "…",
     "触发信号": ["…"], "失效条件": "…"}
  ],
  "关键价位": {
    "支撑": [{"价位": 数字, "依据": "…"}],
    "压力": [{"价位": 数字, "依据": "…"}],
    "止损价": 数字,
    "目标位": [{"价位": 数字, "依据": "…"}]
  },
  "关注板块": [{"板块": "…", "方向": "偏多|偏空", "依据": "…"}],
  "风险": [{"风险": "…", "监控指标": "…", "应对": "…"}],
  "数据依赖": {"降级项": ["…"], "缺失导致的不确定性": "…"}
}

要求：
1) 关键价位请落在上证指数等真实点位上，依据写清均线／前高前低／缺口／整数关口，不要编造事实包里没有的数据；
2) 三个情景概率之和应约等于 100；
3) 数据缺失一律写进「数据依赖」，不要用占位值；
4) 本结论不构成投资建议。
"""


def _tbl(rows, cols, headers=None):
    """简易 markdown 表格（给模型看，不进报告）。"""
    if not rows:
        return "（无数据）"
    headers = headers or cols
    out = ["| " + " | ".join(str(h) for h in headers) + " |", "|" + "---|" * len(headers)]
    for r in rows:
        cells = []
        for c in cols:
            v = r.get(c)
            if isinstance(v, float):
                v = ("%.4f" % v).rstrip("0").rstrip(".")
            cells.append("—" if v is None else str(v))
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def market_factpack(pandoc, cap=22000):
    dd = (pandoc or {}).get("data") or {}
    L = ["# 大盘事实包（pan.py 采集，中性数值）",
         "- 交易日：%s ｜ 时段：%s ｜ 是否交易日：%s ｜ 生成时间：%s"
         % (pandoc.get("trade_date"), pandoc.get("phase"),
            pandoc.get("is_trading_day"), pandoc.get("generated_at"))]

    iv = dd.get("indices_volume") or {}
    idx = iv.get("指数_同花顺") or iv.get("指数_东财") or []
    if idx:
        L.append("\n## 1 指数与量能")
        L.append(_tbl(idx, ["名称", "最新价", "涨跌幅_pct", "成交额_亿", "振幅_pct", "最高", "最低"],
                      ["名称", "最新价", "涨跌%", "成交额(亿)", "振幅%", "最高", "最低"]))
        vol = iv.get("两市成交额") or {}
        if vol:
            L.append("- 两市成交额：" + "，".join("%s %s 亿" % (k, v) for k, v in vol.items()))
        for k, v in (iv.get("分时均价线") or {}).items():
            if isinstance(v, dict):
                L.append("- %s 分时：最新 %s，均价 %s，现价相对均价 %s%%"
                         % (k, v.get("最新价"), v.get("分时均价"), v.get("现价相对均价_pct")))

    sent = dd.get("sentiment") or {}
    bd = sent.get("广度")
    if bd:
        L.append("\n## 2 情绪与广度")
        L.append("- 上涨 %s 家 / 下跌 %s 家 / 平盘 %s 家（上涨占比 %s%%）；样本 %s 只；无行情 %s 只"
                 % (bd.get("上涨家数"), bd.get("下跌家数"), bd.get("平盘家数"),
                    bd.get("上涨占比_pct"), bd.get("样本数"), bd.get("无行情家数")))
        L.append("- 涨停 %s 家 / 跌停 %s 家（阈值口径）；涨幅超5%% %s 家 / 跌幅超5%% %s 家"
                 % (bd.get("涨停家数_阈值口径"), bd.get("跌停家数_阈值口径"),
                    bd.get("涨幅超5%家数"), bd.get("跌幅超5%家数")))
        cx = sent.get("双源交叉") or {}
        if cx:
            L.append("- 双源交叉：%s" % json.dumps(cx, ensure_ascii=False))
    lian = sent.get("连板梯队") or {}
    if lian.get("连板股总数") is not None:
        L.append("- 连板股 %s 只，最高 %s 连板；梯队 %s"
                 % (lian.get("连板股总数"), lian.get("最高连板高度"),
                    json.dumps(lian.get("各梯队数量") or {}, ensure_ascii=False)))
        names = lian.get("连板股名单") or []
        if names:
            L.append(_tbl(names[:15], ["代码", "名称", "连板数", "梯队"]))

    fu = dd.get("funds") or {}
    L.append("\n## 3 资金")
    L.append("- 两市主力净流入：%s 亿" % fu.get("主力资金_两市净流入_亿"))
    nb = fu.get("北向资金") or {}
    if nb:
        L.append("- 北向资金：%s（%s）" % (nb.get("status"), nb.get("note") or ""))
    so = fu.get("南向资金") or {}
    if so.get("南向合计_净买入_亿") is not None:
        L.append("- 南向资金（%s）：净买入 %s 亿（沪 %s / 深 %s）"
                 % (so.get("日期"), so.get("南向合计_净买入_亿"),
                    so.get("沪市港股通_净买入_亿"), so.get("深市港股通_净买入_亿")))
    mg = fu.get("两融") or {}
    if mg.get("融资余额_亿") is not None:
        L.append("- 两融（%s）：融资余额 %s 亿，融资买入 %s 亿，融券余额 %s 亿"
                 % (mg.get("日期"), mg.get("融资余额_亿"), mg.get("融资买入额_亿"), mg.get("融券余额_亿")))
    ind = fu.get("行业主力资金") or {}
    if ind.get("净流入TOP10"):
        L.append("\n主力净流入 TOP10：\n" + _tbl(ind["净流入TOP10"], ["名称", "主力净流入_亿", "涨跌幅_pct"],
                                                ["名称", "主力净流入(亿)", "涨跌%"]))
    if ind.get("净流出TOP10"):
        L.append("\n主力净流出 TOP10：\n" + _tbl(ind["净流出TOP10"], ["名称", "主力净流入_亿", "涨跌幅_pct"],
                                                ["名称", "主力净流入(亿)", "涨跌%"]))

    sec = dd.get("sectors") or {}
    sec_lines = []
    for k in ("行业板块", "概念板块"):
        g = sec.get(k) or {}
        for side in ("领涨", "领跌"):
            rows = g.get(side) or []
            if rows:
                sec_lines.append("\n%s %s：\n%s" % (k, side, _tbl(
                    rows[:10], ["名称", "涨跌幅_pct", "成交额_亿", "主力净流入_亿", "领涨股"],
                    ["名称", "涨跌%", "成交额(亿)", "主力净流入(亿)", "领涨股"])))
    if sec_lines:
        L.append("\n## 4 板块")
        L.extend(sec_lines)
    if sec.get("板块轮动速度"):
        L.append("- 板块轮动速度：%s" % json.dumps(sec.get("板块轮动速度"), ensure_ascii=False))

    ov = dd.get("overseas_evening") or {}
    if ov:
        L.append("\n## 5 海外隔夜与其他市场")
        for k in ("权益_隔夜", "汇率", "大宗商品", "债券收益率", "东财国际指数"):
            v = ov.get(k)
            if isinstance(v, list) and v:
                L.append("\n%s：\n%s" % (k, _tbl(v[:8], list(v[0].keys()))))
            elif isinstance(v, dict) and v:
                L.append("- %s：%s" % (k, json.dumps(v, ensure_ascii=False)[:400]))

    nd = dd.get("next_day") or {}
    if nd:
        L.append("\n## 6 次一日与事件")
        L.append("- 次一交易日：%s（来自交易日历：%s）"
                 % (nd.get("次日"), nd.get("次日是否来自交易日历")))
        for k in ("新股_申购", "新股_上市", "限售解禁", "次日事件", "晚间公告"):
            v = nd.get(k)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                L.append("\n%s（前 %d 条）：\n%s" % (k, min(len(v), 8), _tbl(v[:8], list(v[0].keys()))))

    sr = dd.get("stock_review") or {}
    if sr.get("个股"):
        L.append("\n## 7 个股复盘（节选）")
        L.append(_tbl((sr.get("个股") or [])[:15], list((sr.get("个股") or [{}])[0].keys())[:8]))

    L.append("\n## 8 口径与降级")
    for note in (pandoc.get("notes") or [])[:12]:
        L.append("- " + str(note))
    deg = pandoc.get("degrade")
    if isinstance(deg, list):
        for d in deg[:20]:
            L.append("- 降级：" + (d if isinstance(d, str) else json.dumps(d, ensure_ascii=False)))
    elif isinstance(deg, dict):
        for k, v in list(deg.items())[:20]:
            L.append("- 降级 %s：%s" % (k, v))

    txt = "\n".join(L)
    if len(txt) > cap:
        txt = txt[:cap] + "\n\n（事实包超出 %d 字符预算，已截断）" % cap
    return txt


def save_market_forecast(payload):
    day = str(payload.get("trade_date") or aiplan.now_local().strftime("%Y-%m-%d")).replace("-", "")
    d = os.path.join(MARKET_DIR, day)
    os.makedirs(d, exist_ok=True)
    body = json.dumps(payload, ensure_ascii=False, indent=1)
    stamp = aiplan.now_local().strftime("%H%M%S")
    path = os.path.join(d, "%s.json" % stamp)
    atomic_write(path, body, newline="\n")
    atomic_write(os.path.join(d, "latest.json"), body, newline="\n")
    return path


def latest_market_forecast():
    if not os.path.isdir(MARKET_DIR):
        return None, None
    cands = []
    for day in os.listdir(MARKET_DIR):
        p = os.path.join(MARKET_DIR, day, "latest.json")
        if os.path.exists(p):
            cands.append(p)
    p = newest_file(cands)
    if not p:
        return None, None
    return aiplan.read_json(p), p


def run_market_forecast(log, profile=None, model_pro=None, api_base=None, api_key=None):
    """调模型生成大盘走势预测。日志通过 log() 回传，产物落 data/ai/market/。"""
    cfg, err = aiplan.load_models(MODELS_PATH)
    if err:
        raise RuntimeError(err)
    pan_path = newest_file(pan_files())
    pandoc = aiplan.read_json(pan_path) if pan_path else None
    if not pandoc:
        raise RuntimeError("没有可用的 pan 快照：先跑 python pan.py post")
    prof_name, prof, src = aiplan.resolve_profile(cfg, pandoc.get("phase") or "post", profile or None, None)
    if not prof:
        raise RuntimeError(src)
    conf = prof.get("研判")
    if not isinstance(conf, dict):
        raise RuntimeError("profile「%s」没有研判档" % prof_name)
    provider = aiplan.find_provider(cfg, conf.get("provider"))
    if provider is None:
        raise RuntimeError("provider 不存在：%r" % conf.get("provider"))
    provider = copy.deepcopy(provider)
    if api_base:
        provider["base_url"] = api_base
    model = model_pro or conf.get("model")
    if not model:
        raise RuntimeError("研判档未指定 model")
    key, ksrc = aiplan.resolve_key(provider, api_key)
    if not key and provider.get("鉴权") != "none":
        raise RuntimeError("未找到 API key（provider=%s）" % provider.get("名称"))

    fact = market_factpack(pandoc)
    log("[..] pan 快照 %s（交易日 %s）" % (rel(pan_path), pandoc.get("trade_date")))
    log("[..] 事实包 %d 字符" % len(fact))
    log("[OK] profile %s（%s）｜ 研判 %s / %s ｜ key %s"
        % (prof_name, src, provider.get("名称"), model, aiplan.mask_key(key)))
    log("[..] call %s (%s) ..." % (model, provider.get("协议")))
    r = aiplan.call_model(provider, model, MARKET_SYSTEM, MARKET_PROMPT + "\n\n" + fact,
                          temperature=conf.get("temperature") if conf.get("temperature") is not None else 0.3,
                          max_tokens=conf.get("max_tokens") or 8000,
                          json_mode=bool(provider.get("json_object")),
                          key=key, extra=conf.get("参数"))
    if not r.get("ok"):
        raise RuntimeError("模型调用失败：%s" % r.get("error"))
    obj, perr = aiplan.extract_json(r.get("text"))
    log("[..] 模型返回 %d 字符 ｜ latency %sms"
        % (len(r.get("text") or ""), r.get("latency_ms")))
    usage = r.get("usage") or {}
    cost = cost_of(provider, model, usage, cfg)
    log("[OK] usage in %s / out %s ｜ 费用 %s"
        % (usage.get("输入"), usage.get("输出"),
           cost.get("人民币_估算") if cost.get("人民币_估算") is not None else "未配置单价（仅记录 token）"))
    if obj is None:
        log("[WARN] 返回不是合法 JSON：%s（原文已保留在产物里）" % perr)
    payload = {
        "tool": "aiplan-webui", "kind": "market_forecast", "schema_version": "1",
        "generated_at": aiplan.iso_now(),
        "trade_date": pandoc.get("trade_date"), "phase": pandoc.get("phase"),
        "pan_path": pan_path, "pan_sha1": aiplan.sha1_file(pan_path),
        "profile": prof_name, "profile来源": src,
        "provider": provider.get("名称"), "model": model,
        "usage": usage, "cost": cost, "latency_ms": r.get("latency_ms"),
        "factpack_chars": len(fact),
        "json": obj, "raw_text": None if obj is not None else r.get("text"),
        "note": "本段由大模型基于 pan.py 中性事实生成，不构成投资建议。",
    }
    path = save_market_forecast(payload)
    log("[OK] 已保存 %s" % rel(path))
    return {"forecast": rel(path), "forecast_path": path,
            "trade_date": pandoc.get("trade_date"), "model": model}


def _symbol_name(c6):
    """从运行页的代码候选里取名称（候选含持仓 / 自选 / stock3d / 历史报告）。"""
    for it in build_symbols():
        if it["代码"] == c6:
            return it.get("名称")
    return None


def load_kline(code, limit=180):
    """读 data/history/<thscode>.json 的日K缓存（pan.py / stock3d.py 抓数时落盘）。"""
    c6 = aiplan.code6(code or "")
    if not c6 or not os.path.isdir(HISTORY_DIR):
        return None
    target = None
    for name in sorted(os.listdir(HISTORY_DIR)):
        if aiplan.code6(name) == c6:
            target = os.path.join(HISTORY_DIR, name)
            break
    if not target:
        return None
    doc = aiplan.read_json(target)
    if not isinstance(doc, dict):
        return None
    try:
        n = max(20, min(int(limit or 180), 800))
    except (TypeError, ValueError):
        n = 180
    rows = [r for r in (doc.get("rows") or []) if isinstance(r, dict)][-n:]
    bars = []
    for r in rows:
        bar = {"date": r.get("date"), "open": num(r.get("open")), "high": num(r.get("high")),
               "low": num(r.get("low")), "close": num(r.get("close")), "vol": num(r.get("vol"))}
        if bar["date"] and bar["close"] is not None:
            bars.append(bar)
    return {
        "代码": c6, "名称": _symbol_name(c6), "文件": rel(target),
        "来源": doc.get("source"), "复权": doc.get("adjust"),
        "总根数": doc.get("bars"), "抓取时间": doc.get("fetched_at"), "bars": bars,
    }
