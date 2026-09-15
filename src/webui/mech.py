# -*- coding: utf-8 -*-
"""机械打分：严格照根目录《机器打分逻辑.txt》的 100 分制与一票否决规则。

分值：模块1 宏观 15 ｜ 模块2 行业景气 20 ｜ 模块3 基本面 25 ｜ 模块4 技术面 25 ｜
模块5 资金筹码 10 ｜ 模块6 风险合规 5。**阈值逐条照抄文件**，不新增、不改写、不做
主观修正；冲突时按分值加权求和（文件「歧义补充规则」第 1 条）。

拿不到数据的指标按「缺失」处理：不计入分子也不计入分母，机械分 = 实得 ÷ 可得 × 100，
缺失与未覆盖项在产物与页面逐条列出（MISSING_ITEMS / UNCOVERED）。

本文件只做「数值 → 分」的映射：不取数（mechdata.py）、不解析计划、不调模型。
"""


ERROR_TEXT = "本次没取到数据（接口失败或该标的缺该字段）"

MISSING_ITEMS = (
    ("模块1", "沪深300 PE-TTM 近10年历史分位", 4,
     "无数据源：东财估值报表只有当期快照，指数接口实测无 PE 历史序列"),
    ("模块1", "近20日北向资金累计净流入", 3,
     "沪深港通日度净买入自 2024-08 起停止披露（见 pan.NORTH_NOTE）"),
    ("模块2", "行业 PE-TTM 近10年历史分位", 4,
     "同上：行业只有当期 PE，没有 10 年历史序列"),
)

COVERED_TOTAL = 89          # 100 - 11（上面三项）

UNCOVERED = (
    ("一票否决 2", "最近一份财报被出具「非标准无保留意见」",
     "东财/巨潮都没有结构化的审计意见字段，本轮未执行"),
)

HALF_THRESHOLD_INDUSTRIES = ("银行", "非银金融", "证券", "保险")
CYCLE_INDUSTRIES = ("煤炭", "钢铁", "有色")


def _first(v, rules):
    """rules = [(判定, 分值)...]，第一条命中即返回；都不命中返回 None。"""
    if v is None:
        return None
    for cond, val in rules:
        if cond(v):
            return val
    return None


def s_hs300_pe_pct(v, _ctx):
    """4 分：分位 <30% 得 4；30~50% 得 2；50~70% 得 1；≥70% 得 0。"""
    return _first(v, [(lambda x: x >= 70, 0), (lambda x: x >= 50, 1),
                      (lambda x: x >= 30, 2), (lambda x: x < 30, 4)])


def s_bond10y(v, _ctx):
    """2 分：<2.8% 得 2；2.8~3.2% 得 1；≥3.2% 得 0。"""
    return _first(v, [(lambda x: x >= 3.2, 0), (lambda x: x >= 2.8, 1),
                      (lambda x: x < 2.8, 2)])


def s_north20(v, _ctx):
    """3 分：净流入 ≥200 亿得 3；0~200 亿得 1；净流出得 0。"""
    return _first(v, [(lambda x: x >= 200, 3), (lambda x: x >= 0, 1),
                      (lambda x: x < 0, 0)])


def s_amount5(v, _ctx):
    """3 分：近5日日均全市场成交额 ≥10000 亿得 3；8000~10000 亿得 1；<8000 亿得 0。"""
    return _first(v, [(lambda x: x >= 10000, 3), (lambda x: x >= 8000, 1),
                      (lambda x: x < 8000, 0)])


def s_limitup5(v, _ctx):
    """3 分：近5日日均涨停家数（不含 ST）≥50 得 3；30~50 得 1；<30 得 0。"""
    return _first(v, [(lambda x: x >= 50, 3), (lambda x: x >= 30, 1),
                      (lambda x: x < 30, 0)])


def s_ind_profit_yoy(v, ctx):
    """5 分：行业净利润同比 ≥30% 得 5；15~30% 得 3；0~15% 得 1；<0 得 0。

    银行/非银按文件「阈值减半」：≥15% 满分、7.5~15% 得 3、0~7.5% 得 1。
    煤炭/钢铁/有色按文件要换行业 PPI（无数据源）→ 记缺失。
    """
    industry = str((ctx or {}).get("行业") or "")
    if any(k in industry for k in CYCLE_INDUSTRIES):
        return None
    if any(k in industry for k in HALF_THRESHOLD_INDUSTRIES):
        return _first(v, [(lambda x: x >= 15, 5), (lambda x: x >= 7.5, 3),
                          (lambda x: x >= 0, 1), (lambda x: x < 0, 0)])
    return _first(v, [(lambda x: x >= 30, 5), (lambda x: x >= 15, 3),
                      (lambda x: x >= 0, 1), (lambda x: x < 0, 0)])


def s_ind_rev_yoy(v, _ctx):
    """3 分：行业营收同比 ≥20% 得 3；10~20% 得 2；0~10% 得 1；<0 得 0。"""
    return _first(v, [(lambda x: x >= 20, 3), (lambda x: x >= 10, 2),
                      (lambda x: x >= 0, 1), (lambda x: x < 0, 0)])


def s_ind_pe_pct(v, _ctx):
    """4 分：行业 PE-TTM 近10年分位 <30% 得 4；30~50% 得 2；50~70% 得 1；≥70% 得 0。"""
    return _first(v, [(lambda x: x >= 70, 0), (lambda x: x >= 50, 1),
                      (lambda x: x >= 30, 2), (lambda x: x < 30, 4)])


def s_ind_etf_flow(v, _ctx):
    """4 分：近20日行业 ETF 资金净流入率 ≥5% 得 4；0~5% 得 1；净流出得 0。"""
    return _first(v, [(lambda x: x >= 5, 4), (lambda x: x >= 0, 1),
                      (lambda x: x < 0, 0)])


def s_ind_north_change(v, _ctx):
    """4 分：北向资金行业持仓占比变动 ≥0.5 个百分点得 4；0~0.5 得 1；下降得 0。

    口径：北向日度净买入已停止披露，这里用「北向持股占流通比」季度快照环比近似。
    """
    return _first(v, [(lambda x: x >= 0.5, 4), (lambda x: x >= 0, 1),
                      (lambda x: x < 0, 0)])


def s_roe(v, _ctx):
    """4 分：ROE-TTM ≥15% 得 4；10~15% 得 2；5~10% 得 1；<5% 得 0。"""
    return _first(v, [(lambda x: x >= 15, 4), (lambda x: x >= 10, 2),
                      (lambda x: x >= 5, 1), (lambda x: x < 5, 0)])


def s_gross_margin(v, _ctx):
    """3 分：销售毛利率-TTM ≥40% 得 3；20~40% 得 2；<20% 得 0。"""
    return _first(v, [(lambda x: x >= 40, 3), (lambda x: x >= 20, 2),
                      (lambda x: x < 20, 0)])


def s_net_margin(v, _ctx):
    """3 分：销售净利率-TTM ≥15% 得 3；8~15% 得 2；3~8% 得 1；<3% 得 0。"""
    return _first(v, [(lambda x: x >= 15, 3), (lambda x: x >= 8, 2),
                      (lambda x: x >= 3, 1), (lambda x: x < 3, 0)])


def s_deduct_yoy(v, _ctx):
    """5 分：扣非净利润同比（单季）≥30% 得 5；15~30% 得 3；0~15% 得 1；<0 得 0。"""
    return _first(v, [(lambda x: x >= 30, 5), (lambda x: x >= 15, 3),
                      (lambda x: x >= 0, 1), (lambda x: x < 0, 0)])


def s_rev_yoy_q(v, _ctx):
    """3 分：营业收入同比（单季）≥30% 得 3；15~30% 得 2；0~15% 得 1；<0 得 0。

    文件在 10%~15% 之间没有定义档位（原文即如此），这里并入 0~15% 档。
    """
    return _first(v, [(lambda x: x >= 30, 3), (lambda x: x >= 15, 2),
                      (lambda x: x >= 0, 1), (lambda x: x < 0, 0)])


def s_debt_ratio(v, ctx):
    """2 分：一般行业 <40% 得 2、40~60% 得 1、>60% 得 0；银行 <92% 得 2；地产 <70% 得 2。"""
    industry = str((ctx or {}).get("行业") or "")
    if "银行" in industry:
        return _first(v, [(lambda x: x < 92, 2), (lambda x: x >= 92, 0)])
    if "地产" in industry:
        return _first(v, [(lambda x: x < 70, 2), (lambda x: x >= 70, 0)])
    return _first(v, [(lambda x: x < 40, 2), (lambda x: x < 60, 1), (lambda x: x >= 60, 0)])


def s_ocf_to_profit(v, _ctx):
    """2 分：经营现金流净额/净利润（TTM）≥100% 得 2；50~100% 得 1；<50% 得 0。"""
    return _first(v, [(lambda x: x >= 100, 2), (lambda x: x >= 50, 1),
                      (lambda x: x < 50, 0)])


def s_goodwill_ratio(v, _ctx):
    """2 分：商誉/净资产 <10% 得 2；10~30% 得 1；≥30% 得 0。"""
    return _first(v, [(lambda x: x < 10, 2), (lambda x: x < 30, 1), (lambda x: x >= 30, 0)])


def s_ar_vs_rev(v, _ctx):
    """1 分：应收增速 < 营收增速得 1；增速差 ≤10 个百分点得 0.5；超过得 0。"""
    return _first(v, [(lambda x: x < 0, 1), (lambda x: x <= 10, 0.5), (lambda x: x > 10, 0)])


def s_ma(v, _ctx):
    """2 分：收盘价 ≥ 均线得 2，否则 0。"""
    return _first(v, [(lambda x: x >= 0, 2), (lambda x: x < 0, 0)])


def s_vol_trend(v, _ctx):
    """4 分：20日均量 ≥ 60日均量×1.2 得 4；×0.9~×1.2 得 2；<×0.9 得 0。"""
    return _first(v, [(lambda x: x >= 1.2, 4), (lambda x: x >= 0.9, 2),
                      (lambda x: x < 0.9, 0)])


def s_vol_price(v, _ctx):
    """3 分：近5日阳线均量 ≥ 阴线均量×1.2 得 3；0.9~1.2 得 1；<0.9 得 0。"""
    return _first(v, [(lambda x: x >= 1.2, 3), (lambda x: x >= 0.9, 1),
                      (lambda x: x < 0.9, 0)])


def s_macd(v, _ctx):
    """3 分：DIF>DEA 且双线在 0 轴上方得 3；DIF>DEA 但双线在 0 轴下方得 1；DIF<DEA 得 0。

    调用方把状态编码成 3 / 1 / 0（见 pick_run 里的 macd_state_code）。
    """
    return _first(v, [(lambda x: x >= 3, 3), (lambda x: x >= 1, 1), (lambda x: x < 1, 0)])


def s_rsi14(v, _ctx):
    """2 分：40≤RSI≤60 得 2；30~40 或 60~70 得 1；<30 或 >70 得 0。"""
    return _first(v, [(lambda x: 40 <= x <= 60, 2),
                      (lambda x: 30 <= x < 40 or 60 < x <= 70, 1),
                      (lambda x: x < 30 or x > 70, 0)])


def s_excess20(v, _ctx):
    """3 分：20日超额收益（相对沪深300）≥5% 得 3；0~5% 得 1；<0 得 0。"""
    return _first(v, [(lambda x: x >= 5, 3), (lambda x: x >= 0, 1), (lambda x: x < 0, 0)])


def s_chip_focus(v, _ctx):
    """2 分：90% 筹码集中度 <10% 得 2；10~20% 得 1；≥20% 得 0。

    口径：没有公开的筹码分布数据源，用东财 F10「股东户数集中度」代理（页面标注）。
    """
    return _first(v, [(lambda x: x < 10, 2), (lambda x: x < 20, 1), (lambda x: x >= 20, 0)])


def s_main_flow_ratio(v, _ctx):
    """4 分：近10日主力资金净流入率 ≥2% 得 4；0~2% 得 1；净流出得 0。"""
    return _first(v, [(lambda x: x >= 2, 4), (lambda x: x >= 0, 1), (lambda x: x < 0, 0)])


def s_lhb_inst(v, _ctx):
    """2 分：近10日龙虎榜机构净买入为正得 2；有榜但净卖出得 0；无榜得 1。

    调用方把状态编码成 1 / 0 / -1（1=净买入、0=无榜、-1=净卖出）。
    """
    return _first(v, [(lambda x: x >= 1, 2), (lambda x: x == 0, 1), (lambda x: x < 0, 0)])


def s_top10_free(v, _ctx):
    """2 分：前十大流通股东持股占比 ≥60% 得 2；40~60% 得 1；<40% 得 0。"""
    return _first(v, [(lambda x: x >= 60, 2), (lambda x: x >= 40, 1),
                      (lambda x: x < 40, 0)])


def s_turnover10(v, _ctx):
    """2 分：近10日日均换手率 <5% 得 2；5~10% 得 1；≥10% 得 0。"""
    return _first(v, [(lambda x: x < 5, 2), (lambda x: x < 10, 1), (lambda x: x >= 10, 0)])


def s_no_penalty(v, _ctx):
    """2 分：近1年无监管处罚/立案记录得 2；有得 0。"""
    return _first(v, [(lambda x: x >= 1, 2), (lambda x: x < 1, 0)])


def s_no_profit_cut(v, _ctx):
    """2 分：近1年无业绩大幅下修（幅度>50%）得 2；有得 0。"""
    return _first(v, [(lambda x: x >= 1, 2), (lambda x: x < 1, 0)])


def s_no_reduction(v, _ctx):
    """1 分：近30日无大额减持计划（<1% 总股本）得 1；有得 0。"""
    return _first(v, [(lambda x: x >= 1, 1), (lambda x: x < 1, 0)])


MODULES = (
    ("模块1 宏观市场环境", 15, (
        ("hs300_pe_pct", "沪深300 PE-TTM 近10年历史分位", 4, s_hs300_pe_pct),
        ("bond10y", "10年期国债到期收益率", 2, s_bond10y),
        ("north20", "近20日北向资金累计净流入", 3, s_north20),
        ("amount5", "近5日日均全市场成交额", 3, s_amount5),
        ("limitup5", "近5日日均涨停家数（不含 ST）", 3, s_limitup5),
    )),
    ("模块2 行业中观景气", 20, (
        ("ind_profit_yoy", "行业净利润同比增速（最新单季）", 5, s_ind_profit_yoy),
        ("ind_rev_yoy", "行业营收同比增速（最新单季）", 3, s_ind_rev_yoy),
        ("ind_pe_pct", "行业 PE-TTM 近10年历史分位", 4, s_ind_pe_pct),
        ("ind_etf_flow", "近20日行业 ETF 资金净流入率", 4, s_ind_etf_flow),
        ("ind_north_change", "近20日北向资金行业持仓占比变动", 4, s_ind_north_change),
    )),
    ("模块3 公司基本面", 25, (
        ("roe", "ROE-TTM", 4, s_roe),
        ("gross_margin", "销售毛利率-TTM", 3, s_gross_margin),
        ("net_margin", "销售净利率-TTM", 3, s_net_margin),
        ("deduct_yoy", "扣非净利润同比增速（单季）", 5, s_deduct_yoy),
        ("rev_yoy_q", "营业收入同比增速（单季）", 3, s_rev_yoy_q),
        ("debt_ratio", "资产负债率", 2, s_debt_ratio),
        ("ocf_to_profit", "经营现金流净额/净利润（TTM）", 2, s_ocf_to_profit),
        ("goodwill_ratio", "商誉/净资产比例", 2, s_goodwill_ratio),
        ("ar_vs_rev", "应收账款增速 vs 营收增速", 1, s_ar_vs_rev),
    )),
    ("模块4 技术面趋势", 25, (
        ("ma250", "收盘价 ≥ 250 日均线（年线）", 2, s_ma),
        ("ma120", "收盘价 ≥ 120 日均线（半年线）", 2, s_ma),
        ("ma60", "收盘价 ≥ 60 日均线", 2, s_ma),
        ("ma20", "收盘价 ≥ 20 日均线", 2, s_ma),
        ("vol_trend", "量能趋势（20日均量/60日均量）", 4, s_vol_trend),
        ("vol_price", "量价配合度（近5日阳量/阴量）", 3, s_vol_price),
        ("macd", "MACD 状态（12,26,9）", 3, s_macd),
        ("rsi14", "RSI(14)", 2, s_rsi14),
        ("excess20", "20日超额收益（相对沪深300）", 3, s_excess20),
        ("chip_focus", "90% 筹码集中度（代理：股东户数集中度）", 2, s_chip_focus),
    )),
    ("模块5 资金与筹码", 10, (
        ("main_flow_ratio", "近10日主力资金净流入率", 4, s_main_flow_ratio),
        ("lhb_inst", "近10日龙虎榜机构净买入", 2, s_lhb_inst),
        ("top10_free", "前十大流通股东持股占比", 2, s_top10_free),
        ("turnover10", "近10日日均换手率", 2, s_turnover10),
    )),
    ("模块6 风险合规", 5, (
        ("no_penalty", "近1年无监管处罚/立案记录", 2, s_no_penalty),
        ("no_profit_cut", "近1年无业绩大幅下修（>50%）", 2, s_no_profit_cut),
        ("no_reduction", "近30日无大额减持计划（<1% 总股本）", 1, s_no_reduction),
    )),
)


def score_stock(data):
    """一只标的的机械分：{实得, 可得, 机械分, 明细[], 缺失[]}。

    机械分 = 实得 ÷ 可得 × 100（保留 1 位）；某项没数据时只从分母里去掉那一项，
    不做「缺项记 0」，也不把权重摊到别的项上（口径见模块注释）。
    """
    data = data or {}
    明细, 缺失 = [], []
    实得, 可得 = 0.0, 0.0
    for module_name, _total, items in MODULES:
        for key, label, full, fn in items:
            value = data.get(key)
            if isinstance(value, bool):
                value = int(value)
            score = fn(value, data) if value is not None else None
            明细.append({"模块": module_name, "指标": label, "键": key, "满分": full,
                         "值": value, "得分": score})
            if score is None:
                缺失.append({"指标": label, "满分": full,
                             "原因": static_reason(label) or ERROR_TEXT})
            else:
                实得 += float(score)
                可得 += float(full)
    return {"实得": round(实得, 1), "可得": round(可得, 1),
            "机械分": None if 可得 <= 0 else round(实得 / 可得 * 100.0, 1),
            "明细": 明细, "缺失": 缺失,
            "口径": "满分 100（可得 %d 分）：阈值照抄《机器打分逻辑.txt》；"
                    "缺失项只从分母里去掉，不记 0、不摊权" % int(可得)}


def static_reason(label):
    """确认「没有数据源」的指标 → 固定的缺失原因（页面照这个说）。"""
    for _module, name, _full, reason in MISSING_ITEMS:
        if name == label:
            return reason
    return None


# ---------------------------------------------------------------------------
# 一票否决（文件第一节「强制一票否决项」）
# ---------------------------------------------------------------------------

def veto_batch(row):
    """可在全池批量判断的否决项（400 只候选池全跑），命中返回原因文本。

    文件第 1/5/6/7 条：ST 与退市、连续两年亏损且营收 < 1 亿、当日一字跌停/停牌、
    自由流通市值 < 20 亿元。
    """
    row = row or {}
    name = str(row.get("名称") or "").upper()
    if "ST" in name or "退市" in name:
        return "ST / *ST / 退市整理或退市风险警示"
    if row.get("停牌") or row.get("一字跌停"):
        return "当日一字跌停 / 临时停牌"
    cap = row.get("自由流通市值_亿")
    if cap is not None and cap < 20:
        return "自由流通市值 %s 亿元 < 20 亿元" % cap
    if row.get("两年亏损且营收不足1亿"):
        return "连续两个会计年度净利润为负且最新年度营收 < 1 亿元"
    return None


def veto_announce(flags):
    """公告类否决项（只对送模型的前 150 只跑），命中返回原因文本。

    文件第 3/4/8 条：立案调查或公开谴责、控股股东质押 ≥85%、近30日减持计划 ≥2%。
    """
    flags = flags or {}
    if flags.get("立案或谴责"):
        return "公司或实控人被立案调查 / 被交易所公开谴责"
    pledge = flags.get("质押比例_pct")
    if pledge is not None and pledge >= 85:
        return "控股股东累计质押比例 %s%% ≥ 85%%" % pledge
    cut = flags.get("减持比例_pct")
    if cut is not None and cut >= 2:
        return "近30日大股东减持计划拟减持 %s%% ≥ 总股本 2%%" % cut
    return None


def veto_summary():
    """一票否决的执行范围、口径与未覆盖项（写进产物与页面）。"""
    return {
        "全池": ["ST / *ST / 退市整理或退市风险警示", "当日一字跌停 / 临时停牌",
                 "自由流通市值 < 20 亿元（口径：东财无该字段，用流通市值近似）",
                 "连续两个会计年度亏损且营收 < 1 亿元"],
        "前150": ["公司或实控人被立案调查 / 被交易所公开谴责",
                  "控股股东累计质押比例 ≥ 85%（口径：个股质押比例）",
                  "近30日大股东减持计划 ≥ 总股本 2%"],
        "未覆盖": [{"项": item[1], "原因": item[2]} for item in UNCOVERED],
        "口径": "命中任意一条直接淘汰、不参与打分；公告类只对送模型的前 150 只执行",
    }
