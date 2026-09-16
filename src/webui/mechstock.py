# -*- coding: utf-8 -*-
"""单只标的的机械打分：交易流「计算交易计划」的量化底座。

口径只有一份：
  · 阈值 / 一票否决 → mech.py（照抄根目录《机器打分逻辑.txt》）；
  · 取数 → mechdata.py（东财 + 巨潮，当天缓存 / 逐股日K 缓存）；
  · 本模块：把**一只**标的组装成 mech.score_stock 需要的输入，并跑一票否决。

`fund_inputs / tech_inputs / mech_data` 这批纯函数是从 pick_run 迁过来的：荐股（全池）与
交易流（单只）都要用，放在这里保证两处的原始值与口径不会分叉。
本模块不解析计划、不调模型。
"""

from . import mech, mechdata
from .paths import aiplan, num

# 荐股主流程用的默认排除规则（只在「没有当天横截面」时补跑一次全市场用）
DEFAULT_EXCLUDE = {"st": True, "new": True, "low_price": True,
                   "low_amount": True, "skip_688_bj": False}


def market_row(code, refresh=False, log=None):
    """**单只标的**的原始行（1 次东财 ulist）→ 与 mechdata.pool_row 同构的 dict。

    为什么单独一条取数：荐股扫全市场是为了选股（14 页 clist），交易流只需要给**一只**
    标的打分，用 `em_ulist` 取同一套字段（mechdata.STOCK_FIELDS）即可，不必拉全市场。
    实测 ulist 返回 f100（东财行业）、ROE、资产负债率、营收/净利同比、主力净流入、
    流通市值、总股本等，字段与 clist 一致，所以 pool_row 可以直接复用（口径唯一）。
    当日内按代码缓存（data/cache/mech/row_<日期>.json），二次运行零请求。
    返回 (row, 错误文本)。
    """
    c6 = aiplan.code6(str(code or ""))
    if not c6:
        return None, "代码不是 6 位数字：%r" % (code,)
    name = "row_%s" % c6
    if not refresh:
        cached = mechdata.cache_read(name)
        if cached and cached.get("row"):
            return cached["row"], None
    secid = mechdata._secid(c6)
    if not secid:
        return None, "认不出 %s 的市场（无法转 secid）" % c6
    rows = mechdata.pan.em_ulist(mechdata._ctx(), [secid], fields=mechdata.STOCK_FIELDS)
    hit = None
    for r in rows or []:
        if aiplan.code6(str(r.get("f12") or "")) == c6:
            hit = r
            break
    if hit is None:
        return None, "东财 ulist 没有返回 %s（停牌 / 代码异常 / 限流）" % c6
    row = mechdata.pool_row(hit)
    mechdata.cache_write(name, {"抓取时间": aiplan.iso_now(), "row": row})
    if log:
        log("[OK] 单股行情 %s %s ｜ 现价 %s ｜ 行业 %s"
            % (c6, row.get("名称") or "", row.get("现价"), row.get("行业") or "—"))
    return row, None


# ---------------------------------------------------------------------------
# 纯函数（荐股 / 交易流共用）：原始值 → 机械打分的输入
# ---------------------------------------------------------------------------

QUARTERS_OF = {"03-31": 1, "06-30": 2, "09-30": 3, "12-31": 4}


def pct(cur, base):
    """(cur/base - 1) * 100；缺一侧或 base<=0 返回 None。"""
    a, b = num(cur), num(base)
    if a is None or b is None or b <= 0:
        return None
    return round((a / b - 1.0) * 100.0, 2)


def diff_pct(a, b):
    return None if a is None or b is None else round(a - b, 2)


def quarter_scale(period):
    return QUARTERS_OF.get(str(period or "")[-5:], 4)


def prev_of_same(period):
    """去年同期的上一期（算去年同期单季用）。'2025-06-30' → '2025-03-31'。"""
    text = str(period or "")[:10]
    if len(text) < 10:
        return None
    y, m, _d = (int(x) for x in text.split("-"))
    prev = mechdata.prev_periods(text)[0]
    return prev if prev else ("%04d-12-31" % (y - 1) if m == 3 else text)


def fund_inputs(code, lico, balance, latest, prev, same, row):
    """一只标的基本面原始值（扣非/营收用单季口径，缺上期就退回累计口径并标注）。"""
    cur = (lico.get(latest) or {}).get(code) or {}
    prv = (lico.get(prev) or {}).get(code) or {}
    last = (lico.get(same) or {}).get(code) or {}
    bal = (balance.get(latest) or {}).get(code) or {}
    bal_last = (balance.get(same) or {}).get(code) or {}
    scale = quarter_scale(latest)
    roe = num(cur.get("WEIGHTAVG_ROE"))
    income = num(cur.get("TOTAL_OPERATE_INCOME"))
    profit = num(cur.get("PARENT_NETPROFIT"))
    out = {
        "roe": None if roe is None else round(roe * 4.0 / scale, 2),
        "gross_margin": num(cur.get("XSMLL")),
        "net_margin": None if (income is None or profit is None or income <= 0)
                              else round(profit / income * 100.0, 2),
        "rev_yoy_q": num(cur.get("YSTZ")),
        "deduct_yoy": num(cur.get("SJLTZ")),
        "debt_ratio": row.get("资产负债率_pct"),
        "应收": num(bal.get("ACCOUNTS_RECE")), "净资产": num(bal.get("TOTAL_EQUITY")),
        "应收_去年": num(bal_last.get("ACCOUNTS_RECE")),
    }
    # 单季口径：有上期累计就相减，否则退回累计同比（口径写进产物）
    if income is not None and num(prv.get("TOTAL_OPERATE_INCOME")) is not None:
        q_now = income - num(prv.get("TOTAL_OPERATE_INCOME"))
        q_last = num(last.get("TOTAL_OPERATE_INCOME"))
        q_last_prev = num((lico.get(prev_of_same(same)) or {}).get(code, {})
                          .get("TOTAL_OPERATE_INCOME"))
        if q_last is not None and q_last_prev is not None:
            out["rev_yoy_q"] = pct(q_now, q_last - q_last_prev)
    eps = num(cur.get("DEDUCT_BASIC_EPS"))
    eps_prev = num(prv.get("DEDUCT_BASIC_EPS"))
    eps_last = num(last.get("DEDUCT_BASIC_EPS"))
    if eps is not None and eps_prev is not None and eps_last is not None:
        q_now = eps - eps_prev
        q_last = eps_last - num((lico.get(prev_of_same(same)) or {}).get(code, {})
                                .get("DEDUCT_BASIC_EPS") or 0.0)
        if q_last > 0:
            out["deduct_yoy"] = pct(q_now, q_last)
    ocf = num(cur.get("MGJYXJJE"))
    shares = num(row.get("总股本"))
    if ocf is not None and shares is not None and profit not in (None, 0):
        out["ocf_to_profit"] = round(ocf * shares / profit * 100.0, 2)
    if out["应收"] is not None and out["应收_去年"] is not None:
        ar_yoy = pct(out["应收"], out["应收_去年"])
        out["ar_vs_rev"] = diff_pct(ar_yoy, out["rev_yoy_q"])
    out["goodwill_ratio"] = None          # 商誉没有数据源 → 该小项记缺失
    out["两年亏损且营收不足1亿"] = bool(
        profit is not None and profit < 0
        and num(last.get("PARENT_NETPROFIT")) is not None
        and num(last.get("PARENT_NETPROFIT")) < 0
        and income is not None and income < 1e8)
    return out


def macd_code(dif, dea):
    """MACD 状态编码：3=DIF>DEA 且双线在 0 轴上方；1=DIF>DEA 但双线在 0 轴下方；0=DIF<DEA。"""
    if dif is None or dea is None:
        return None
    if dif > dea:
        return 3 if (dif > 0 and dea > 0) else 1
    return 0


def chip_focus(bars, holder_value):
    """90% 筹码集中度：优先东财 F10「股东户数集中度」，它不是数字时退回
    「60 日价格区间集中度」——公式与文件一致（(高-低)/(高+低)×100%）。"""
    v = num(holder_value)
    if v is not None:
        return v, "东财 F10 股东户数集中度"
    rows = (bars or [])[-60:]
    highs = [x for x in (num(b.get("high")) for b in rows) if x is not None]
    lows = [x for x in (num(b.get("low")) for b in rows) if x is not None]
    if not highs or not lows:
        return None, None
    hi, lo = max(highs), min(lows)
    if hi + lo <= 0:
        return None, None
    return round((hi - lo) / (hi + lo) * 100.0, 2), "60 日价格区间集中度（公式与文件一致）"


def tech_inputs(bars, bench, row):
    """技术面原始指标（同时给机械打分与事实包）。"""
    from . import mechtech
    closes_ = mechtech.closes(bars)
    close = closes_[-1] if closes_ else None
    out = {"MACD": None, "RSI14": None, "量能比_20_60": None, "量价比_5": None,
           "超额20_pct": None, "换手10日_pct": None}
    for n in (20, 60, 120, 250):
        ma = mechtech.sma(closes_, n)
        dist = None if (ma is None or close is None or not ma) else round((close / ma - 1) * 100, 2)
        out["MA%s距离_pct" % n] = dist
        if n == 20 and dist is not None and row is not None:
            row["最新收盘"] = close
    out["量能比_20_60"] = mechtech.vol_ratio(bars)
    out["量价比_5"] = mechtech.volume_price_ratio(bars)
    dif, dea = mechtech.macd(closes_)
    out["MACD"] = macd_code(dif, dea)
    out["MACD_DIF"], out["MACD_DEA"] = dif, dea
    out["RSI14"] = mechtech.rsi(closes_)
    out["超额20_pct"] = mechtech.excess_return_pct(bars, bench, 20)
    turns = [num(b.get("换手_pct")) for b in (bars or [])[-10:]]
    turns = [t for t in turns if t is not None]
    if not turns and row:
        # 腾讯兜底日K 没有换手列：用 成交量(手) / 流通股本(股) 自算（口径一致，页面标注）
        shares = num(row.get("流通股本"))
        if shares:
            turns = [num(b.get("vol")) * 100.0 / shares * 100.0
                     for b in (bars or [])[-10:] if num(b.get("vol")) is not None]
    if turns:
        out["换手10日_pct"] = round(sum(turns) / len(turns), 2)
    return out


def mech_data(row, fin, tech, env, ind_agg, ind, extras=None):
    """机械打分的输入字典（键与 mech.MODULES 一一对应）。"""
    extras = extras or {}
    ind_row = (ind_agg or {}).get(ind) or {}
    return {
        "行业": ind,
        "hs300_pe_pct": None, "north20": None, "ind_pe_pct": None,   # 无数据源，见 mech.MISSING_ITEMS
        "bond10y": env.get("bond10y"), "amount5": env.get("amount5"),
        "limitup5": env.get("limitup5"),
        "ind_profit_yoy": ind_row.get("净利同比_pct"), "ind_rev_yoy": ind_row.get("营收同比_pct"),
        "ind_etf_flow": extras.get("行业ETF资金率"), "ind_north_change": extras.get("北向行业变动"),
        "roe": fin.get("roe"), "gross_margin": fin.get("gross_margin"),
        "net_margin": fin.get("net_margin"), "deduct_yoy": fin.get("deduct_yoy"),
        "rev_yoy_q": fin.get("rev_yoy_q"), "debt_ratio": fin.get("debt_ratio"),
        "ocf_to_profit": fin.get("ocf_to_profit"), "goodwill_ratio": fin.get("goodwill_ratio"),
        "ar_vs_rev": fin.get("ar_vs_rev"),
        "ma20": tech.get("MA20距离_pct"), "ma60": tech.get("MA60距离_pct"),
        "ma120": tech.get("MA120距离_pct"), "ma250": tech.get("MA250距离_pct"),
        "vol_trend": tech.get("量能比_20_60"), "vol_price": tech.get("量价比_5"),
        "macd": tech.get("MACD"), "rsi14": tech.get("RSI14"),
        "excess20": tech.get("超额20_pct"), "chip_focus": extras.get("筹码集中度"),
        "main_flow_ratio": extras.get("主力净流入率"),
        "lhb_inst": extras.get("龙虎榜机构"),
        "top10_free": extras.get("十大流通占比"),
        "turnover10": tech.get("换手10日_pct"),
        "no_penalty": extras.get("无监管处罚"), "no_profit_cut": extras.get("无业绩下修"),
        "no_reduction": extras.get("无减持计划"),
    }


def flow_rate(row):
    """当日主力净流入率（%）= 主力净流入 / 流通市值（初筛用的近似口径）。"""
    flow, size = row.get("主力净流入_万"), row.get("流通市值_亿")
    if flow is None or not size:
        return None
    return round(flow / 1e4 / size * 100.0, 2)


def fmt_money(v, nd=0):
    return "—" if v is None else ("%.*f" % (nd, v))


def module_totals(score):
    """机械分明细 → 模块小计（页面与事实包共用这一份）。"""
    out = []
    for name, total, _items in mech.MODULES:
        got = full = 0.0
        for row in (score or {}).get("明细") or []:
            if row.get("模块") != name or row.get("得分") is None:
                continue
            got += float(row["得分"])
            full += float(row.get("满分") or 0)
        out.append({"模块": name, "满分": total, "得分": round(got, 1), "可得": round(full, 1)})
    return out


# ---------------------------------------------------------------------------
# 单只标的：取数 → 打分 → 一票否决
# ---------------------------------------------------------------------------

def score_one(code, refresh=False, log=None):
    """一只标的的机械打分（含一票否决）。返回 (结果, 错误文本)；取不到行情时结果为 None。

    结果 = {代码, 名称, 行业, 原始行, 技术, 基本面, 资金, 模块, 机械分, 缺失, 否决,
            否决汇总, 降级, 口径提示, 口径, 取数时间}
    """
    def say(text):
        if log:
            log(text)

    row, err = market_row(code, refresh=refresh, log=log)
    if row is None:
        return None, err or "取不到该标的的行情"
    c6 = row["代码"]
    degrade, hints = [], []
    lico, latest, prev, same = mechdata.finance_by_period(refresh=refresh, log=log)
    if not latest:
        degrade.append("业绩报表取不到：模块3 的小项会记缺失")
    balance = mechdata.balance_by_period(refresh=refresh, log=log)
    code_ind = mechdata.code_industry_map((balance or {}).get(latest) or {})
    ind = code_ind.get(c6) or row.get("行业") or "未归类"
    row["行业"] = ind
    fin = fund_inputs(c6, lico or {}, balance or {}, latest, prev, same, row)
    sample_ind = dict(mechdata.scanned_industry_map())
    sample_ind.update(code_ind)
    if not sample_ind:
        # 行业景气（模块2）要有横截面样本：没有当天全市场缓存就按需补一次
        say("[..] 没有当天全市场横截面：为行业景气补跑一次全市场扫描（当天只跑一次）…")
        try:
            mechdata.market_scan(dict(DEFAULT_EXCLUDE), log=log)
        except Exception as exc:            # noqa: BLE001  补横截面失败不该打断打分
            degrade.append("全市场横截面取数异常：%s" % str(exc)[:80])
        sample_ind = dict(mechdata.scanned_industry_map())
        sample_ind.update(code_ind)
    if not sample_ind:
        degrade.append("没有全市场横截面：行业景气（模块2）记缺失")
    ind_agg = mechdata.industry_agg(sample_ind, (lico or {}).get(latest) or {}, latest)
    env = mechdata.macro_inputs(refresh=refresh, log=log)
    bars = mechdata.kline(c6, refresh=refresh)
    if not bars:
        degrade.append("日K 取不到：技术面（模块4）会大面积记缺失")
    bench = mechdata.bench_kline(refresh=refresh)
    if not bench:
        degrade.append("沪深300 日K 取不到：20 日超额收益记缺失")
    tech = tech_inputs(bars, bench, row)
    lhb = mechdata.lhb_state([c6], refresh=refresh, log=log)
    etf = mechdata.etf_flow_ratio(ind, refresh=refresh)
    pledge = mechdata.pledge_map(refresh=refresh, log=log) or {}
    north = mechdata.north_change([c6], refresh=refresh)
    flags = mechdata.announce_flags(c6)
    warn = mechdata.profit_warning([c6], refresh=refresh)
    flow = mechdata.fund_flow(c6, days=10, refresh=refresh)
    rate = None
    if flow and row.get("流通市值_亿"):
        rate = round(flow["净额_万"] / 1e4 / row["流通市值_亿"] * 100.0, 2)
    chip_value, chip_note = chip_focus(bars, mechdata.holder_focus(c6))
    if chip_note:
        hints.append("90%% 筹码集中度口径：%s" % chip_note)
    extras = {
        "行业ETF资金率": etf, "北向行业变动": north.get(c6),
        "筹码集中度": chip_value, "筹码集中度_口径": chip_note,
        "十大流通占比": mechdata.top10_free_ratio(c6),
        "主力净流入率": rate if rate is not None else flow_rate(row),
        "主力10日_万": (flow or {}).get("净额_万"),
        "龙虎榜机构": lhb.get(c6), "质押比例_pct": pledge.get(c6),
        "无监管处罚": 0 if flags.get("立案或谴责") else 1,
        "无业绩下修": 0 if warn.get(c6) else 1,
        "无减持计划": 0 if (flags.get("有减持计划")
                            and (flags.get("减持比例_pct") is None
                                 or flags["减持比例_pct"] >= 1)) else 1,
    }
    data = mech_data(row, fin, tech, env, ind_agg, ind, extras)
    score = mech.score_stock(data)
    veto = []
    why = mech.veto_batch(dict(row, **{"两年亏损且营收不足1亿": fin["两年亏损且营收不足1亿"]}))
    if why:
        veto.append({"范围": "批量", "原因": why})
    why2 = mech.veto_announce(dict(flags, 质押比例_pct=pledge.get(c6)))
    if why2:
        veto.append({"范围": "公告", "原因": why2})
    say("[OK] 机械分 %s（实得 %s / 可得 %s）｜ 缺失 %d 项 ｜ 一票否决 %s"
        % (fmt_money(score.get("机械分"), 1), fmt_money(score.get("实得"), 1),
           fmt_money(score.get("可得"), 1), len(score.get("缺失") or []),
           "、".join(v["原因"] for v in veto) if veto else "无"))
    return {
        "代码": c6, "名称": row.get("名称") or c6, "行业": ind,
        "原始行": row, "技术": tech, "基本面": fin, "资金": extras,
        "模块": module_totals(score), "机械分": score,
        # 事实包数据日期（批注 3：页面要把「用的是哪天的数据」打出来）
        "日K最新交易日": ((bars[-1].get("date") if bars else None)),
        "日K根数": len(bars), "日K来源": "东财前复权（腾讯兜底）",
        "缺失": score.get("缺失") or [], "否决": veto,
        "否决汇总": mech.veto_summary(), "降级": degrade, "口径提示": hints,
        "口径": "机械分 = 实得 ÷ 可得 × 100（可得上限 %d 分）；阈值照抄《机器打分逻辑.txt》；"
                "缺失项只从分母里去掉，不记 0、不摊权" % mech.COVERED_TOTAL,
        "取数时间": aiplan.iso_now(),
    }, None
