# -*- coding: utf-8 -*-
"""大盘评分：6 模块 27 分项的阈值逐条照抄根目录《大盘评分逻辑.txt》，零主观、零模型。

· 机械分 = 实得 ÷ 可得 × 100（缺失项只从分母剔除，不记 0、不摊权）；
· 总分   = 机械分 × 0.7 + 模型分 × 0.3（模型分没生成时先按 100% 机械，页面标注）；
· 无数据源的分项固定标注（PB-LF 历史、ERP、DR007、社融存量、北向 20 日、股票型 ETF
  净申购率、CRB），阈值照抄不动，以后接上数据源就能自动得分。

本模块是纯函数：输入是 mktdata.collect() 的证据 dict，不联网、不读盘，便于单测。
"""

import os
import time

from . import mktdata

WEIGHTS = (0.7, 0.3)               # 机械 7 : 模型 3
SCORE_DOC = "大盘评分逻辑.txt"       # 口径来源（根目录，sha256 由 scripts/check.py 锁定）

# 无数据源的分项 → 固定原因（页面如实列出，不拿别的东西充数）
NO_SOURCE = {
    "沪深300 PB-LF 近10年历史分位": "无数据源：中证指数官网只提供 PE，东财只有当期 PB，没有 10 年历史序列",
    "A 股股权风险溢价(ERP) 近10年分位": "无数据源：ERP 需要沪深300 PE 与 10 年期国债的 10 年历史序列，国债历史无免费源",
    "DR007": "无数据源：中国货币网只开放 FR/FDR 定盘利率，DR007 无公开接口",
    "社融存量同比增速": "无数据源：东财宏观报表没有社融存量同比，央行只有网页发布",
    "近20日北向资金累计净流入": "无数据源：沪深港通日度净买入自 2024-08 起停止披露",
    "近20日股票型ETF累计净申购率": "无数据源：ETF 份额历史没有公开接口",
    "CRB 大宗商品指数近20日涨跌幅": "无数据源：东财/新浪都没有 CRB 指数序列",
}

NOTE = ("阈值逐条照抄《大盘评分逻辑.txt》，缺失项只从分母剔除（不记 0、不摊权）；"
        "机械分按可得分折算成百分制；总分 = 机械分 × 0.7 + 模型分 × 0.3；"
        "本分数只描述大盘系统性风险，不含个股判断，不构成投资建议。")


# ---------------------------------------------------------------------------
# 打分原语
# ---------------------------------------------------------------------------

def _asc(v, cuts, default):
    """区间下限判定：cuts = [(下限, 分值), ...] 升序，命中最后一条不超过 v 的；小于首条取下限值。"""
    if v is None:
        return None
    pts = default
    for lo, p in cuts:
        if v >= lo:
            pts = p
    return float(pts)


def _desc(v, cuts, default=0.0):
    """区间上限判定：cuts = [(上限, 分值), ...] 升序，命中第一条 v ≤ 上限的。"""
    if v is None:
        return None
    for hi, p in cuts:
        if v <= hi:
            return float(p)
    return float(default)


def _txt(v, nd=2, unit=""):
    return "—" if v is None else ("%.*f%s" % (nd, v, unit))


def _item(name, full, got, value, rule, source=None, date=None, err=None):
    out = {"指标": name, "满分": float(full), "得分": got,
           "观测值": value, "阈值": rule, "来源": source, "数据日期": date}
    if name in NO_SOURCE:
        out["无数据源"] = NO_SOURCE[name]
        out["得分"] = None
    elif got is None:
        out["缺失原因"] = err or "本次没取到数据"
    return out


def _ma(bars, n):
    if not bars or len(bars) < n:
        return None
    vals = [b.get("close") for b in bars[-n:]]
    if any(v is None for v in vals):
        return None
    return sum(vals) / n


def _chg_pct(bars, n):
    """最近 n 个周期的涨跌幅（%）：最后一个收盘 / n 根之前的收盘 − 1。"""
    if not bars or len(bars) <= n:
        return None
    last, base = bars[-1].get("close"), bars[-1 - n].get("close")
    if not last or not base:
        return None
    return round((last / base - 1.0) * 100.0, 2)


def macd(closes, fast=12, slow=26, signal=9):
    """标准 MACD（EMA 递推）：返回 (DIF, DEA)。"""
    if not closes or len(closes) < slow + signal:
        return None, None

    def ema(values, n):
        k = 2.0 / (n + 1.0)
        out = values[0]
        for v in values[1:]:
            out = v * k + out * (1 - k)
        return out

    dif_series = []
    for i in range(slow, len(closes) + 1):
        window = closes[:i]
        dif_series.append(ema(window, fast) - ema(window, slow))
    if len(dif_series) < signal:
        return None, None
    return round(dif_series[-1], 4), round(ema(dif_series, signal), 4)


# ---------------------------------------------------------------------------
# 六个模块（阈值逐条照抄文件）
# ---------------------------------------------------------------------------

def module1(ev):
    pe300 = ev["估值"]["沪深300PE"]
    pe500 = ev["估值"]["中证500PE"]
    items = [
        _item("沪深300 PE-TTM 近10年历史分位", 10,
              _asc(pe300.get("分位"), ((20, 7), (40, 4), (60, 2), (80, 0)), 10),
              "PE %s ｜ 分位 %s%%（%s ~ %s，%s 条）"
              % (_txt(pe300.get("值")), _txt(pe300.get("分位"), 1), pe300.get("起") or "—",
                 pe300.get("止") or "—", pe300.get("序列条数") or "—"),
              "分位 <20%：10 分；20~40%：7 分；40~60%：4 分；60~80%：2 分；≥80%：0 分",
              pe300.get("来源"), pe300.get("数据日期"), pe300.get("错误")),
        _item("沪深300 PB-LF 近10年历史分位", 5, None, "—",
              "分位 <20%：5 分；20~40%：3 分；40~60%：2 分；60~80%：1 分；≥80%：0 分"),
        _item("A 股股权风险溢价(ERP) 近10年分位", 7, None, "—",
              "分位 ≥80%：7 分；60~80%：5 分；40~60%：3 分；20~40%：1 分；<20%：0 分"),
        _item("中证500 PE-TTM 近10年历史分位", 3,
              _asc(pe500.get("分位"), ((20, 2), (40, 1), (60, 0)), 3),
              "PE %s ｜ 分位 %s%%（%s ~ %s）"
              % (_txt(pe500.get("值")), _txt(pe500.get("分位"), 1), pe500.get("起") or "—",
                 pe500.get("止") or "—"),
              "分位 <20%：3 分；20~40%：2 分；40~60%：1 分；≥60%：0 分",
              pe500.get("来源"), pe500.get("数据日期"), pe500.get("错误")),
    ]
    return "模块1 估值水平", 25.0, items


def module2(ev):
    mac = ev["宏观"]
    bond, m2, shibor = mac["国债10Y"], mac["M2同比"], mac["拆借3M"]
    items = [
        _item("DR007", 4, None, "—",
              "≤1.8%：4 分；1.8~2.0%：3 分；2.0~2.2%：2 分；2.2~2.5%：1 分；>2.5%：0 分"),
        _item("10 年期国债到期收益率", 5,
              _asc(bond.get("值"), ((2.6, 4), (2.9, 2), (3.2, 1), (3.5, 0)), 5),
              _txt(bond.get("值"), 3, "%"),
              "<2.6%：5 分；2.6~2.9%：4 分；2.9~3.2%：2 分；3.2~3.5%：1 分；≥3.5%：0 分",
              bond.get("来源"), bond.get("数据日期"), bond.get("错误")),
        _item("M2 同比增速", 4,
              _asc(m2.get("值"), ((6, 1), (8, 2), (10, 3), (12, 4)), 0),
              _txt(m2.get("值"), 1, "%"),
              "≥12%：4 分；10~12%：3 分；8~10%：2 分；6~8%：1 分；<6%：0 分",
              m2.get("来源"), m2.get("数据日期"), m2.get("错误")),
        _item("社融存量同比增速", 4, None, "—",
              "≥11%：4 分；9.5~11%：3 分；8~9.5%：2 分；6.5~8%：1 分；<6.5%：0 分"),
        _item("3 个月同业拆借加权利率", 3,
              _asc(shibor.get("值"), ((2.0, 2), (2.3, 1), (2.6, 0)), 3),
              _txt(shibor.get("值"), 2, "%"),
              "≤2.0%：3 分；2.0~2.3%：2 分；2.3~2.6%：1 分；>2.6%：0 分",
              shibor.get("来源"), shibor.get("数据日期"), shibor.get("错误")),
    ]
    return "模块2 货币与宏观流动性", 20.0, items


def module3(ev):
    fund = ev["资金"]
    summary, margin = fund["全市场汇总"], fund["两融"]
    fft = fund["资金流"]
    main10 = None
    rows = [r for sec in fft.values() for r in (sec.get("明细") or [])]
    if rows:
        main10 = round(sum(v for _d, v in rows), 2)
    margin_ratio = None
    cap = summary.get("值")
    if margin.get("值") is not None and cap:
        margin_ratio = round(margin["值"] / cap * 100.0, 2)
    amount5 = fund["成交额5日"]["值"]
    items = [
        _item("近20日北向资金累计净流入", 5, None, "—",
              "≥300 亿：5 分；100~300 亿：3 分；0~100 亿：1 分；净流出：0 分"),
        _item("两融余额占A股流通市值比例", 4,
              _asc(margin_ratio, ((2.0, 3), (2.5, 2), (3.0, 1), (3.5, 0)), 4),
              "%s%%（两融 %s 亿 / 流通市值 %s 亿）"
              % (_txt(margin_ratio), _txt(margin.get("值"), 0), _txt(cap, 0)),
              "<2.0%：4 分；2.0~2.5%：3 分；2.5~3.0%：2 分；3.0~3.5%：1 分；≥3.5%：0 分",
              "%s ｜ %s" % (margin.get("来源"), summary.get("来源")),
              margin.get("数据日期"),
              margin.get("错误") or summary.get("错误")),
        _item("近5日全市场日均成交额", 4,
              _asc(amount5, ((6000, 1), (8000, 2), (10000, 3), (12000, 4)), 0),
              _txt(amount5, 0, " 亿"),
              "≥12000 亿：4 分；10000~12000：3 分；8000~10000：2 分；6000~8000：1 分；<6000：0 分",
              "%s（%d 天）" % (fund["成交额5日"].get("来源") or "—", fund["成交额5日"]["天数"]),
              ev.get("交易日"), "本地历史不足 5 个交易日" if amount5 is None else None),
        _item("近20日股票型ETF累计净申购率", 4, None, "—",
              "≥3%：4 分；1~3%：2 分；-1~1%：1 分；<-1%：0 分"),
        _item("近10日全市场主力资金累计净流入", 3,
              _asc(main10, ((-100, 1), (100, 2), (500, 3)), 0),
              _txt(main10, 1, " 亿"),
              "≥500 亿：3 分；100~500 亿：2 分；-100~100 亿：1 分；<-100 亿：0 分",
              "东财指数资金流历史（上证综指 + 深证综指，指数口径）",
              (sorted([r[0] for r in rows])[-1] if rows else None),
              None if rows else "指数资金流历史取不到"),
    ]
    return "模块3 场内资金面", 20.0, items


def module4(ev):
    bars = ev["行情"]["沪深300日K"].get("bars") or []
    close = bars[-1].get("close") if bars else None
    dif, dea = macd([b.get("close") for b in (ev["行情"]["沪深300月K"].get("bars") or [])])
    if dif is None:
        macd_pts, macd_text = None, "—"
    else:
        above = dif > dea
        macd_pts = 4.0 if (above and dif > 0) else (2.0 if above else (1.0 if dif <= 0 else 0.0))
        macd_text = "DIF %s / DEA %s（%s）" % (dif, dea, "DIF>DEA" if above else "DIF<DEA")
    width = ev["宽度"]
    items = [
        _item("沪深300 收盘价 vs 250 日均线", 3,
              None if (close is None or _ma(bars, 250) is None)
              else (3.0 if close >= _ma(bars, 250) else 0.0),
              "%s vs MA250 %s" % (_txt(close), _txt(_ma(bars, 250))),
              "收盘价 ≥ 250 日均线：3 分；否则 0 分",
              ev["行情"]["沪深300日K"].get("来源"), ev["行情"]["沪深300日K"].get("数据日期"),
              None if bars else "沪深300 日K 取不到"),
        _item("沪深300 收盘价 vs 120 日均线", 2,
              None if (close is None or _ma(bars, 120) is None)
              else (2.0 if close >= _ma(bars, 120) else 0.0),
              "%s vs MA120 %s" % (_txt(close), _txt(_ma(bars, 120))),
              "收盘价 ≥ 120 日均线：2 分；否则 0 分",
              ev["行情"]["沪深300日K"].get("来源"), ev["行情"]["沪深300日K"].get("数据日期")),
        _item("沪深300 收盘价 vs 60 日均线", 2,
              None if (close is None or _ma(bars, 60) is None)
              else (2.0 if close >= _ma(bars, 60) else 0.0),
              "%s vs MA60 %s" % (_txt(close), _txt(_ma(bars, 60))),
              "收盘价 ≥ 60 日均线：2 分；否则 0 分",
              ev["行情"]["沪深300日K"].get("来源"), ev["行情"]["沪深300日K"].get("数据日期")),
        _item("市场宽度：收盘价高于 20 日均线个股占比", 4,
              _asc(width.get("值"), ((30, 1), (50, 2), (70, 4)), 0),
              "%s（样本 %s 只）" % (_txt(width.get("值"), 1, "%"), width.get("样本") or "—"),
              "≥70%：4 分；50~70%：2 分；30~50%：1 分；<30%：0 分",
              width.get("来源"), width.get("数据日期"), width.get("错误")),
        _item("沪深300 月线 MACD 状态", 4, macd_pts, macd_text,
              "DIF>DEA 且 DIF>0：4 分；DIF>DEA 且 DIF<0：2 分；DIF<DEA 且 DIF<0：1 分；"
              "DIF<DEA 且 DIF>0：0 分",
              ev["行情"]["沪深300月K"].get("来源"), ev["行情"]["沪深300月K"].get("数据日期"),
              None if dif is not None else "沪深300 月K 取不到（需要 ≥35 根月K）"),
    ]
    return "模块4 技术趋势结构", 15.0, items


def module5(ev):
    mood = ev["情绪"]
    turnover = None
    cap = ev["资金"]["全市场汇总"].get("值")
    amount5 = ev["资金"]["成交额5日"]["值"]
    if amount5 is not None and cap:
        turnover = round(amount5 / cap * 100.0, 2)
    items = [
        _item("近5日日均涨停家数（不含 ST）", 3,
              _asc(mood["涨停5日"]["值"], ((20, 1), (40, 2), (60, 3)), 0),
              "%s 家（%d 天）" % (_txt(mood["涨停5日"]["值"], 1), mood["涨停5日"]["天数"]),
              "≥60 家：3 分；40~60：2 分；20~40：1 分；<20：0 分",
              "pan 快照逐日积累", ev.get("交易日"),
              None if mood["涨停5日"]["值"] is not None else "本地历史不足 5 个交易日"),
        _item("近5日日均跌停家数（不含 ST）", 2,
              _desc(mood["跌停5日"]["值"], ((5, 2), (15, 1)), 0),
              "%s 家（%d 天）" % (_txt(mood["跌停5日"]["值"], 1), mood["跌停5日"]["天数"]),
              "≤5 家：2 分；5~15 家：1 分；>15 家：0 分",
              "pan 快照逐日积累", ev.get("交易日"),
              None if mood["跌停5日"]["值"] is not None else "本地历史不足 5 个交易日"),
        _item("近5日全市场日均换手率", 2,
              _asc(turnover, ((1, 1), (2, 1.5), (3, 2)), 0),
              "%s%%（5日均成交额 ÷ 流通市值）" % _txt(turnover),
              "≥3%：2 分；2~3%：1.5 分；1~2%：1 分；<1%：0 分",
              "两市成交额 ÷ 全市场流通市值（流通市值口径）", ev.get("交易日"),
              ev["资金"]["全市场汇总"].get("错误")),
        _item("近5日累计涨跌家数比", 3,
              _asc(mood["涨跌家数比5日"]["值"], ((0.9, 1), (1.1, 2), (1.5, 3)), 0),
              "%s（%d 天累计 上涨/下跌）"
              % (_txt(mood["涨跌家数比5日"]["值"], 2), mood["涨跌家数比5日"]["天数"]),
              "≥1.5：3 分；1.1~1.5：2 分；0.9~1.1：1 分；<0.9：0 分",
              "pan 快照逐日积累", ev.get("交易日"),
              None if mood["涨跌家数比5日"]["值"] is not None else "本地历史不足 5 个交易日"),
    ]
    return "模块5 市场情绪温度", 10.0, items


def module6(ev):
    quote = ev["行情"]
    udi = _chg_pct(quote["美元指数"].get("bars") or [], 20)
    spx = _chg_pct(quote["标普500"].get("bars") or [], 20)
    mid = ev["外围"]["中间价变动20日"]
    items = [
        _item("美元指数近20日涨跌幅", 2,
              _asc(udi, ((-2, 1), (2, 0)), 2) if udi is not None else None,
              _txt(udi, 2, "%"),
              "跌幅 ≥2%：2 分；-2%~+2%：1 分；涨幅 ≥2%：0 分",
              quote["美元指数"].get("来源"), quote["美元指数"].get("数据日期"),
              None if udi is not None else "美元指数日K 不足 21 根"),
        _item("人民币兑美元中间价近20日变动", 3,
              _asc(mid.get("值"), ((-1, 1), (-0.5, 2), (1, 3)), 0),
              "贬值/升值 %s%%（%d 天）" % (_txt(mid.get("值"), 2), mid.get("天数")),
              "人民币升值 ≥1%：3 分；-0.5~+1%：2 分；-1~-0.5%：1 分；贬值 ≥1%：0 分",
              mid.get("来源"), mid.get("数据日期"),
              None if mid.get("值") is not None else "本地历史不足 20 个交易日"),
        _item("标普500 近20日涨跌幅", 2,
              _asc(spx, ((0, 1), (5, 2)), 0),
              _txt(spx, 2, "%"),
              "涨幅 ≥5%：2 分；0~5%：1 分；下跌：0 分",
              quote["标普500"].get("来源"), quote["标普500"].get("数据日期"),
              None if spx is not None else "标普500 日K 不足 21 根"),
        _item("CRB 大宗商品指数近20日涨跌幅", 3, None, "—",
              "跌幅 ≥3%：3 分；-1~+3%：2 分；+1~+3%：1 分；涨幅 ≥3%：0 分"),
    ]
    return "模块6 外围环境传导", 10.0, items


MODULES = (module1, module2, module3, module4, module5, module6)


# ---------------------------------------------------------------------------
# 汇总：机械分 + 模型分 → 总分
# ---------------------------------------------------------------------------

def score(ev, model_score=None, model_meta=None):
    """证据 dict → 评分块（纯计算）。model_score=None 表示模型分还没生成。"""
    groups, missing, no_source = [], [], []
    got_all = avail_all = 0.0
    for build in MODULES:
        name, full, items = build(ev)
        got = sum(i["得分"] for i in items if i["得分"] is not None)
        avail = sum(i["满分"] for i in items if i["得分"] is not None)
        got_all += got
        avail_all += avail
        groups.append({"模块": name, "满分": full, "可得": round(avail, 1),
                       "得分": round(got, 1), "指标": items})
        for i in items:
            if i.get("无数据源"):
                no_source.append({"模块": name, "指标": i["指标"], "满分": i["满分"],
                                  "原因": i["无数据源"]})
            elif i["得分"] is None:
                missing.append({"模块": name, "指标": i["指标"], "满分": i["满分"],
                                "原因": i.get("缺失原因") or "本次没取到数据"})
    mech = round(got_all / avail_all * 100.0, 1) if avail_all else None
    model = None if model_score is None else round(float(model_score), 1)
    if mech is None:
        total, weight = None, "机械分没有可得分项"
    elif model is None:
        total, weight = mech, "暂 100% 机械（模型分未生成）"
    else:
        total, weight = round(mech * WEIGHTS[0] + model * WEIGHTS[1], 1), "机械 7 : 模型 3"
    diverge = None if (mech is None or model is None) else round(abs(mech - model), 1)
    idx_date = (ev["行情"]["沪深300日K"].get("数据日期") or "")
    pe300 = ev["估值"]["沪深300PE"]
    return {
        "总分": total, "机械分": mech, "机械实得": round(got_all, 1),
        "机械可得": round(avail_all, 1), "满分": 100.0,
        "模型分": model, "权重": weight, "分歧": diverge,
        "分歧提示": ("机械 %s 与模型 %s 相差 %s 分：两者分歧较大，建议看下方明细再定"
                 % (mech, model, diverge)) if (diverge is not None and diverge >= 15) else None,
        "模块": groups, "缺失": missing, "无数据源": no_source, "口径": NOTE,
        "模型解读": model_meta or None,
        "数据日期": {
            "pan 交易日": ev.get("交易日"),
            "指数日K 最新交易日": idx_date,
            "中证 PE 序列截止": pe300.get("止"),
            "宏观数据日期": "%s / %s" % (ev["宏观"]["M2同比"].get("数据日期") or "—",
                                    ev["宏观"]["拆借3M"].get("数据日期") or "—"),
            "本地历史天数": len([k for k in ev["情绪"]["历史"] if k[0].isdigit()]),
            "评分计算时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
        "取数时间": ev.get("取数时间"),
        "口径文件": SCORE_DOC,
    }


# ---------------------------------------------------------------------------
# 页面/事实包入口
# ---------------------------------------------------------------------------

CACHE_DIR = mktdata.CACHE_ROOT


def cache_path(day=None):
    return os.path.join(mktdata._day_dir(day), "score.json")


def load_cached(day=None):
    """读当天已算好的评分块（没有返回 None）。"""
    doc = mktdata._read(cache_path(day))
    return (doc or {}).get("评分") if isinstance(doc, dict) else None


def save_cached(block, day=None):
    mktdata._write(cache_path(day), {"算好时间": time.strftime("%Y-%m-%d %H:%M:%S"),
                                     "评分": block})
    return block


def latest_read():
    """最新一份模型解读（data/ai/market/read_*/latest_read.json）。"""
    from .paths import MARKET_DIR, aiplan
    from .sources import newest_file
    if not os.path.isdir(MARKET_DIR):
        return None, None
    cands = []
    for day in os.listdir(MARKET_DIR):
        p = os.path.join(MARKET_DIR, day, "latest_read.json")
        if os.path.exists(p):
            cands.append(p)
    path = newest_file(cands)
    if not path:
        return None, None
    return aiplan.read_json(path), path


READ_FIELDS = ("模型评分", "评分理由", "一句话结论", "风险与应对", "操作建议", "数据依赖")


def apply_read(block, read, path=None):
    """把「模型解读」产物合并进评分块（模型分参与 7:3，重算总分与分歧提示）。"""
    if not block:
        return block
    out = dict(block)
    doc = (read or {}).get("json") or {}
    ms = doc.get("模型评分")
    try:
        ms = None if ms is None else round(float(ms), 1)
    except (TypeError, ValueError):
        ms = None
    mech = out.get("机械分")
    out["模型分"] = ms
    if ms is None or mech is None:
        out["总分"] = mech
        out["权重"] = "暂 100% 机械（模型分未生成）"
        out["分歧"] = None
        out["分歧提示"] = None
        out["模型解读"] = None
        out["读取解读"] = None
        return out
    out["总分"] = round(mech * WEIGHTS[0] + ms * WEIGHTS[1], 1)
    out["权重"] = "机械 7 : 模型 3"
    out["分歧"] = round(abs(mech - ms), 1)
    out["分歧提示"] = ("机械 %s 与模型 %s 相差 %s 分：两者分歧较大，建议对照下方明细再定"
                   % (mech, ms, out["分歧"])) if out["分歧"] >= 15 else None
    out["模型解读"] = {k: doc.get(k) for k in READ_FIELDS}
    out["读取解读"] = {
        "生成时间": (read or {}).get("generated_at"),
        "模型": "%s / %s" % ((read or {}).get("provider") or "", (read or {}).get("model") or ""),
        "profile": (read or {}).get("profile"),
        "路径": path,
        "费用": (read or {}).get("cost"),
        "错误": (read or {}).get("error"),
    }
    return out


def evidence_text(ev, limit=1600):
    """给「模型盲评」用的原始证据（只有数值与来源，**不含任何得分、阈值与机械分**）。"""
    est = ev.get("估值", {})
    mac = ev.get("宏观", {})
    fund = ev.get("资金", {})
    mood = ev.get("情绪", {})
    out = ev.get("外围", {}).get("中间价变动20日", {})
    quotes = ev.get("行情", {})
    summary = fund.get("全市场汇总", {})
    margin = fund.get("两融", {})
    width = ev.get("宽度", {})
    rows = [r for sec in (fund.get("资金流") or {}).values() for r in (sec.get("明细") or [])]
    main10 = round(sum(v for _d, v in rows), 2) if rows else None
    pe300, pe500 = est.get("沪深300PE", {}), est.get("中证500PE", {})
    margin_ratio = (round(margin["值"] / summary["值"] * 100.0, 2)
                    if margin.get("值") is not None and summary.get("值") else None)
    L = ["## 大盘原始数据（供独立打分；不含任何机械分、得分与阈值）",
         "- 数据日期：pan 快照 %s ｜ 指数日K %s ｜ 中证 PE 序列截止 %s"
         % (ev.get("交易日") or "—", quotes.get("沪深300日K", {}).get("数据日期") or "—",
            pe300.get("止") or "—"),
         "- 估值：沪深300 PE %s（近10年分位 %s%%，序列 %s~%s）｜ 中证500 PE %s（分位 %s%%）"
         % (_txt(pe300.get("值")), _txt(pe300.get("分位"), 1), pe300.get("起") or "—",
            pe300.get("止") or "—", _txt(pe500.get("值")), _txt(pe500.get("分位"), 1)),
         "- 宏观：10 年期国债 %s%% ｜ M2 同比 %s%%（%s）｜ 3 个月拆借 %s%%（%s）"
         % (_txt(mac.get("国债10Y", {}).get("值"), 3), _txt(mac.get("M2同比", {}).get("值"), 1),
            mac.get("M2同比", {}).get("数据日期") or "—", _txt(mac.get("拆借3M", {}).get("值"), 2),
            mac.get("拆借3M", {}).get("数据日期") or "—"),
         "- 场内资金：两融余额 %s 亿 ｜ 全市场流通市值 %s 亿 ｜ 两融占比 %s%% ｜ "
         "近5日日均成交额 %s 亿（%s 天）｜ 近10日主力净流入 %s 亿"
         % (_txt(margin.get("值"), 0), _txt(summary.get("值"), 0), _txt(margin_ratio),
            _txt(fund.get("成交额5日", {}).get("值"), 0), fund.get("成交额5日", {}).get("天数"),
            _txt(main10, 1)),
         "- 情绪：近5日日均涨停 %s 家 ｜ 日均跌停 %s 家 ｜ 涨跌家数比 %s ｜ （各 %s 天）"
         % (_txt(mood.get("涨停5日", {}).get("值"), 1), _txt(mood.get("跌停5日", {}).get("值"), 1),
            _txt(mood.get("涨跌家数比5日", {}).get("值"), 2),
            mood.get("涨跌家数比5日", {}).get("天数")),
         "- 趋势：沪深300 收盘 %s ｜ 高于 20 日均线个股占比 %s%%（样本 %s 只）"
         % (_txt(((quotes.get("沪深300日K", {}).get("bars") or [{}])[-1]).get("close")),
            _txt(width.get("值"), 1), width.get("样本") or "—"),
         "- 均线：沪深300 收盘 %s ｜ MA60 %s ｜ MA120 %s ｜ MA250 %s ｜ 上证 5日 %s%% / 20日 %s%%"
         " ｜ 月线 MACD：DIF %s / DEA %s"
         % (_txt(((quotes.get("沪深300日K", {}).get("bars") or [{}])[-1]).get("close")),
            _txt(_ma(quotes.get("沪深300日K", {}).get("bars") or [], 60)),
            _txt(_ma(quotes.get("沪深300日K", {}).get("bars") or [], 120)),
            _txt(_ma(quotes.get("沪深300日K", {}).get("bars") or [], 250)),
            _txt(_chg_pct(quotes.get("上证日K", {}).get("bars") or [], 5), 2),
            _txt(_chg_pct(quotes.get("上证日K", {}).get("bars") or [], 20), 2),
            _txt((macd([b.get("close") for b in (quotes.get("沪深300月K", {}).get("bars") or [])]) or (None,))[0]),
            _txt((macd([b.get("close") for b in (quotes.get("沪深300月K", {}).get("bars") or [])]) or (None, None))[1])),
         "- 外围：美元指数近20日 %s%% ｜ 标普500 近20日 %s%% ｜ 人民币兑美元近20日 %s%%（%s 天）"
         % (_txt(_chg_pct(quotes.get("美元指数", {}).get("bars") or [], 20), 2),
            _txt(_chg_pct(quotes.get("标普500", {}).get("bars") or [], 20), 2),
            _txt(out.get("值"), 2), out.get("天数"))]
    pan_doc = ev.get("pan") or {}
    bd = (((pan_doc.get("data") or {}).get("sentiment") or {}).get("广度") or {})
    if bd:
        L.append("- 当日盘面：上涨 %s 家 / 下跌 %s 家（上涨占比 %s%%）｜ 涨停 %s / 跌停 %s ｜ "
                 "两市成交额 %s 亿 ｜ 主力净流入 %s 亿 ｜ 最高连板 %s"
                 % (bd.get("上涨家数"), bd.get("下跌家数"), bd.get("上涨占比_pct"),
                    bd.get("涨停家数_阈值口径"), bd.get("跌停家数_阈值口径"),
                    bd.get("两市成交额_亿"), bd.get("主力净流入_亿"),
                    (((pan_doc.get("data") or {}).get("sentiment") or {})
                     .get("连板梯队") or {}).get("最高连板高度")))
    return "\n".join(L)[:limit]


def build(refresh=False, full=True, log=None, model_score=None, model_meta=None):
    """取数 → 打分 → （可选）合并模型分 → 落当天缓存。"""
    ev = mktdata.collect(refresh=refresh, full=full, log=log)
    block = score(ev, model_score=model_score, model_meta=model_meta)
    if block.get("机械分") is not None:
        save_cached(block)
    return block


def fact_block(limit=400, compute=True):
    """给事实包用的一小段（≤limit 字，永不裁剪）；取不到就返回 None，绝不打断调用方。"""
    block = load_cached()
    if not block and compute:
        try:
            block = build(full=False)
        except Exception:                      # noqa: BLE001  评分失败不该拖垮荐股/计划
            block = None
    if not block:
        return None
    parts = ["## 大盘评分（机械 7 : 模型 3，口径文件 %s）" % SCORE_DOC,
             "- 总分 %s ｜ 机械分 %s（实得 %s / 可得 %s）｜ 模型分 %s（%s）"
             % (_txt(block.get("总分"), 1), _txt(block.get("机械分"), 1),
                block.get("机械实得"), block.get("机械可得"),
                _txt(block.get("模型分"), 1), block.get("权重")),
             "- 数据日期：pan %s ｜ 指数日K %s ｜ 评分算于 %s"
             % ((block.get("数据日期") or {}).get("pan 交易日") or "—",
                (block.get("数据日期") or {}).get("指数日K 最新交易日") or "—",
                (block.get("数据日期") or {}).get("评分计算时间") or "—")]
    for g in block.get("模块") or []:
        parts.append("- %s：%s / %s（满分 %s）" % (g["模块"], g["得分"], g["可得"], g["满分"]))
    if block.get("无数据源"):
        parts.append("- 无数据源分项（%d 项，%s 分已从分母剔除）：%s"
                     % (len(block["无数据源"]), sum(x["满分"] for x in block["无数据源"]),
                        "、".join(x["指标"] for x in block["无数据源"])))
    if block.get("缺失"):
        parts.append("- 本次缺失：%s" % "、".join(x["指标"] for x in block["缺失"]))
    read = block.get("模型解读") or {}
    if read.get("一句话结论"):
        parts.append("- 模型解读：%s" % read["一句话结论"])
    text = "\n".join(parts)
    return text[:limit]
