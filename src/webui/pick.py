# -*- coding: utf-8 -*-
"""荐股的纯逻辑（全大盘版）：常量、排除规则、参数整理、提示词、事实包与 Markdown。

排序口径（本版重做）：
  · 候选池：东财全 A 按成交额降序 → 排除规则 → 前 400 只（mechdata.market_scan）；
  · 机械分：严格照《机器打分逻辑.txt》对全池打分，只用来「初筛 + 辅助列 + 挑出前 150 只」；
  · 模型分：模型对前 150 只给 0-100 推荐评分、一句话理由与适合打法（超短/短/波/中/长），
    **30 只总榜按模型分排序**，模型分缺失时才回退机械分；
  · 模型不给买卖价位（价位由用户自行研判），机械分也不进事实包（避免锚定）。
"""

import os
import re

from .paths import DATA_DIR, aiplan, pan

PICK_STYLES = (("超短线", "1-3 个交易日"), ("短线", "1-5 个交易日"), ("波段", "2-6 周"),
               ("中线", "1-3 个月"), ("长线", "6 个月以上"))

PICK_STYLE_CYCLE = dict(PICK_STYLES)

PICK_STYLE_SET = {name for name, _c in PICK_STYLES}

PICK_BOARD_TYPES = ("行业", "概念")

PICK_L1_INDUSTRIES = (
    "农林牧渔", "基础化工", "钢铁", "有色金属", "电子", "汽车", "家用电器", "食品饮料",
    "纺织服饰", "轻工制造", "医药生物", "公用事业", "交通运输", "房地产", "商贸零售",
    "社会服务", "综合", "建筑材料", "建筑装饰", "电力设备", "国防军工", "计算机", "传媒",
    "通信", "银行", "非银金融", "美容护理", "石油石化", "煤炭", "环保", "机械设备",
)

PICK_POOL_SIZE = 400          # 候选池上限（按成交额降序）
PICK_MODEL_TOP = 150          # 送模型的数量
PICK_PAGE_TOP = 30            # 总榜长度（模型推荐榜）
PICK_MAX_CHARS = 40000        # 事实包字符预算
PICK_MIN_PRICE = 2.0          # 排除低价股阈值（元）
PICK_MIN_AMOUNT = 5e7         # 排除低成交阈值（元）
PICK_CACHE_DIR = os.path.join(DATA_DIR, "cache")

PICK_BOARD_FIELDS = ("f2,f3,f5,f6,f7,f8,f12,f14,f20,f21,f24,f25,f62,f104,f105,"
                     "f109,f110,f128,f136,f140,f160,f184")

PICK_SYSTEM = ("你是 A 股选股助手，只依据给定的事实数据做评估与筛选，不编造数值，"
               "不给出具体买卖价位，结论不构成投资建议。")


def pick_list_meta():
    """荐股接口的元信息（页面「历史结果」与参数默认值用）。"""
    return {"打法": [{"值": name, "周期": cycle} for name, cycle in PICK_STYLES],
            "候选池上限默认": PICK_POOL_SIZE, "送模型数量默认": PICK_MODEL_TOP,
            "推荐榜长度": PICK_PAGE_TOP,
            "扫描口径": "东财全 A 按成交额降序 → 排除规则 → 候选池 → 机械打分 → 前 N 只送模型"}

PICK_PROMPT = """下面是本地程序采集的全市场中性行情与财务数据（只有数值与口径，没有买卖建议）。
请只输出一个合法 JSON 对象：不要解释文字、不要 markdown 代码围栏、不要注释、不要尾随逗号。

JSON 结构：
{
  "市场风格": {"一句话": "80-150 字，点明当前市场主要机会与风险",
              "多空倾向": "偏多|偏空|中性",
              "最强打法": "超短线|短线|波段|中线|长线",
              "操作节奏": "…"},
  "推荐榜": [{"代码": "6 位代码", "名称": "…", "打法": "超短线|短线|波段|中线|长线",
             "评分": 0-100 的整数, "评级": "关注|观察|回避",
             "理由": "60 字以内，必须引用事实包里的数值",
             "所属板块": "…"}],
  "风险与不确定性": ["…"],
  "免责声明": "…"
}

要求：
1) 只能从事实包「候选表」里出现的代码中挑选，一律写 6 位代码；事实包没有的一律不要编造；
2) 「推荐榜」按评分从高到低给出 30 只（候选不足 30 只就全部给出），跨行业、跨打法统一排名；
   评分要有区分度（不要大量同分，尽量用 1 分粒度拉开）；
3) 每只必须从「超短线 / 短线 / 波段 / 中线 / 长线」里选**一个**最合适的打法，写进「打法」；
4) **不要给任何买卖价位**（不给买点、止损、目标位），价格由使用者自行研判；
5) 「理由」只写事实包里的数值支撑（如主力净流入、均线位置、营收增速），写清为什么适合这个打法；
6) 把缺数据、结论可能不成立的地方写进「风险与不确定性」；本结论不构成投资建议。
"""


def pick_pe(r):
    """PE 取值：f115（TTM）优先，缺失回退 f9（动态），负值视为缺失。"""
    for key in ("f115", "f9"):
        v = pan.num(r.get(key))
        if v is not None and v > 0:
            return v
    return None


def pick_is_new(r):
    """次新/上市不足 60 日：f24 缺失，或 5/10/20/60 日与年初至今涨幅完全相等。"""
    if pan.num(r.get("f24")) is None:
        return True
    vals = [pan.num(r.get(k)) for k in ("f24", "f25", "f109", "f110", "f160")]
    if any(v is None for v in vals):
        return False
    return max(vals) == min(vals)


def pick_exclude_reason(r, exclude):
    """返回排除原因（不排除返回 None）。"""
    name = str(r.get("f14") or "")
    price, amount = pan.num(r.get("f2")), pan.num(r.get("f6"))
    if pan.num(r.get("f3")) is None or price is None or price <= 0:
        return "无行情/停牌"
    code6 = aiplan.code6(str(r.get("f12") or ""))
    if exclude.get("st", True) and ("ST" in name.upper()):
        return "ST"
    if exclude.get("low_amount", True) and (amount is None or amount < PICK_MIN_AMOUNT):
        return "成交额过低"
    if exclude.get("low_price", True) and price < PICK_MIN_PRICE:
        return "低价股"
    if exclude.get("new", True) and pick_is_new(r):
        return "次新/上市不足60日"
    if exclude.get("skip_688_bj", False) and code6 and re.match(r"^(688|8|4)", code6):
        return "科创板/北交所"
    return None


def pick_filter(rows, exclude):
    """按排除规则过滤，返回 (保留行, 排除统计)。"""
    kept, stats = [], {}
    for r in rows:
        why = pick_exclude_reason(r, exclude)
        if why:
            stats[why] = stats.get(why, 0) + 1
            continue
        kept.append(r)
    return kept, stats


def pick_param(body):
    """整理荐股参数（带默认与上限保护）。

    旧版参数（module / modules / industry / concepts / per_board）已废弃：传了也忽略，
    这样老的调用方与快捷键不会直接报错。
    """
    body = body or {}

    def clamp(v, lo, hi, dflt):
        try:
            v = int(v)
        except (TypeError, ValueError):
            return dflt
        return max(lo, min(hi, v))

    ex = body.get("exclude") or {}
    exclude = {
        "st": bool(ex.get("st", True)),
        "new": bool(ex.get("new", True)),
        "low_price": bool(ex.get("low_price", True)),
        "low_amount": bool(ex.get("low_amount", True)),
        "skip_688_bj": bool(ex.get("skip_688_bj", False)),
    }
    return {
        "pool_size": clamp(body.get("pool_size"), 40, 2000, PICK_POOL_SIZE),
        "model_top": clamp(body.get("model_top"), 20, 400, PICK_MODEL_TOP),
        "max_chars": clamp(body.get("max_chars"), 4000, 200000, PICK_MAX_CHARS),
        "refresh": bool(body.get("refresh")),
        "profile": (body.get("profile") or "").strip() or None,
        "model_pro": (body.get("model_pro") or "").strip() or None,
        "api_base": (body.get("api_base") or "").strip() or None,
        "api_key": (body.get("api_key") or "").strip() or None,
        "exclude": exclude,
    }


def pick_style_of(value, fallback="短线"):
    """模型给的打法 → 收敛到五档之一（不在表里就回退默认）。"""
    text = str(value or "").strip()
    if text in PICK_STYLE_SET:
        return text
    for name, _cycle in PICK_STYLES:
        if name and name in text:
            return name
    return fallback


def pick_table(rows, cols, headers):
    if not rows:
        return "（无数据）"
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for r in rows:
        cells = []
        for c in cols:
            v = r.get(c)
            if isinstance(v, float):
                v = ("%.4f" % v).rstrip("0").rstrip(".")
            cells.append("—" if v is None or v == "" else str(v))
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


# 事实包候选表：只给原始数据（不含机械分），列宽控制在 20 列以内
PICK_CAND_COLS = ["代码", "名称", "行业", "现价", "涨跌幅_pct", "成交额_亿", "换手率_pct",
                  "量比", "20日_pct", "60日_pct", "年初至今_pct", "PE_TTM", "PB",
                  "总市值_亿", "自由流通市值_亿", "主力净流入_万", "主力净占比_pct",
                  "ROE_pct", "毛利率_pct", "净利率_pct", "营收同比_pct", "净利同比_pct",
                  "资产负债率_pct"]

PICK_CAND_HEADS = ["代码", "名称", "行业", "现价", "涨跌%", "成交额亿", "换手%", "量比",
                   "20日%", "60日%", "年初%", "PE", "PB", "总市值亿", "自由流通亿",
                   "主力净流入万", "主力净占比%", "ROE%", "毛利率%", "净利率%",
                   "营收同比%", "净利同比%", "负债率%"]

PICK_TECH_COLS = ["代码", "名称", "MA20距离_pct", "MA60距离_pct", "MA120距离_pct",
                  "MA250距离_pct", "量能比_20_60", "量价比_5", "MACD", "RSI14",
                  "超额20_pct", "换手10日_pct", "主力10日_万", "龙虎榜", "质押比例_pct",
                  "北向变动_pp", "筹码集中度", "十大流通占比_pct"]

PICK_TECH_HEADS = ["代码", "名称", "距MA20%", "距MA60%", "距MA120%", "距MA250%",
                   "量能比", "量价比", "MACD", "RSI14", "超额20%", "10日换手%",
                   "10日主力万", "龙虎榜", "质押%", "北向变动pp", "筹码集中度", "十大流通%"]


def _pick_factpack(payload, keep):
    """渲染事实包文本（keep = 送模型的条数）。**不含机械分**，只给原始数据。"""
    scan = payload.get("扫描") or {}
    env = payload.get("市场环境") or {}
    heat = payload.get("板块背景") or {}
    cands = (payload.get("候选池") or [])[:keep]
    tech = (payload.get("候选技术") or {})
    L = ["# 荐股事实包（全市场扫描 + 东财行情/财务，中立数值，不含买卖建议）",
         "- 生成时间：%s ｜ 交易日参考：%s"
         % (payload.get("生成时间"), payload.get("交易日") or "—"),
         "- 扫描口径：东财全 A %s 只 → 排除规则后候选池 %d 只 → 本事实包取前 %d 只"
         % (scan.get("全市场总数") or "—", scan.get("候选池数量") or 0, keep),
         "- 已排除：" + ("，".join("%s %d 只" % (k, v)
                                  for k, v in (scan.get("排除统计") or {}).items()) or "无"),
         "- 说明：候选池按成交额降序取前 %s 只；本地打分结果不提供给模型，"
         "模型只依据下面的原始数据自行判断。" % (payload.get("扫描参数") or {}).get("候选池上限")]
    from . import mktscore                  # 放函数里：避免 pick → mktscore → mktdata → pick 的导入环
    market_line = mktscore.fact_block()
    if market_line:
        L.append("")
        L.append(market_line)
    L.append("\n## 0 市场环境（模块1 原始数据）")
    L.append("- 10 年期国债收益率：%s%% ｜ 近5日日均全市场成交额：%s 亿 ｜ 近5日日均涨停家数：%s 家"
             % (env.get("bond10y"), env.get("amount5"), env.get("limitup5")))
    if heat.get("行业"):
        L.append("- 行业涨跌幅前 10：%s"
                 % "、".join("%s %s%%" % (x.get("名称"), x.get("涨跌幅_pct"))
                             for x in heat["行业"].get("前10") or []))
        L.append("- 行业涨跌幅后 10：%s"
                 % "、".join("%s %s%%" % (x.get("名称"), x.get("涨跌幅_pct"))
                             for x in heat["行业"].get("后10") or []))
    if heat.get("概念"):
        L.append("- 概念涨跌幅前 10：%s"
                 % "、".join("%s %s%%" % (x.get("名称"), x.get("涨跌幅_pct"))
                             for x in heat["概念"].get("前10") or []))
    if payload.get("大盘"):
        L.append("- 大盘快照：%s" % payload["大盘"])
    L.append("\n## 1 候选表（原始行情与财务，共 %d 只）" % len(cands))
    L.append(pick_table(cands, PICK_CAND_COLS, PICK_CAND_HEADS))
    rows = []
    for c in cands:
        t = dict(tech.get(c.get("代码")) or {})
        t["代码"], t["名称"] = c.get("代码"), c.get("名称")
        rows.append(t)
    L.append("\n## 2 候选技术面与资金（同一批标的，原始指标）")
    L.append(pick_table(rows, PICK_TECH_COLS, PICK_TECH_HEADS))
    if payload.get("降级"):
        L.append("\n## 3 数据依赖与降级")
        L += ["- %s" % x for x in payload["降级"]]
    L.append("\n## 4 口径说明")
    L.append("- 现价/涨跌幅为采集时点快照；财务为最新披露报告期（见参数说明），非 TTM 的部分已注明；")
    L.append("- 主力资金为东财口径（近10日合计，单位万元）；北向为季度快照环比（百分点）；")
    L.append("- 筹码集中度：优先东财 F10 股东户数集中度，它不是数字时用「60 日价格区间"
             "集中度」（公式与文件一致）；质押比例是股权质押口径；")
    L.append("- 没有买卖价位、没有收益预测；模型输出 30 只推荐榜时只能引用上表数值。")
    return "\n".join(L)


def pick_factpack(payload, cap):
    """事实包；超出 cap 时逐步减少候选条数（最少 20 条）。"""
    keep = len((payload.get("候选池") or [])[:PICK_MODEL_TOP])
    keep = max(20, keep)
    txt = _pick_factpack(payload, keep)
    while len(txt) > cap and keep > 20:
        keep = max(20, keep - 5)
        txt = _pick_factpack(payload, keep)
    return txt


def pick_markdown(p):
    """人读版 Markdown（存档用）。"""
    par = p.get("扫描参数") or {}
    scan = p.get("扫描") or {}
    env = p.get("市场环境") or {}
    L = ["# 荐股结果（全大盘 · 模型为主） · aiplan Web UI", "",
         "- 生成时间：%s ｜ 交易日参考：%s ｜ 模型：%s ｜ 费用：%s"
         % (p.get("生成时间"), p.get("交易日") or "—",
            (p.get("配置") or {}).get("model") or "未点评",
            (p.get("成本") or {}).get("人民币_估算")
            if (p.get("成本") or {}).get("人民币_估算") is not None else "未配置单价"),
         "- 扫描：全市场 %s 只 → 候选池 %d 只（成交额降序前 %s）→ 送模型 %s 只 → 推荐榜 %d 只"
         % (scan.get("全市场总数") or "—", scan.get("候选池数量") or 0,
            par.get("候选池上限"), par.get("送模型数量"), len(p.get("推荐榜") or [])),
         "- 机械分口径：%s" % (p.get("机械口径") or {}).get("说明", "—"),
         "- 事实包 %d 字符 ｜ 用时见任务日志" % (p.get("factpack_chars") or 0), ""]
    style = p.get("市场风格") or {}
    if style:
        L += ["## 市场风格", "",
              "- 一句话：%s" % (style.get("一句话") or "—"),
              "- 多空倾向：%s ｜ 最强打法：%s ｜ 操作节奏：%s"
              % (style.get("多空倾向") or "—", style.get("最强打法") or "—",
                 style.get("操作节奏") or "—"), ""]
    if (p.get("模型层") or {}).get("error"):
        L += ["> **[WARN] 本次没有模型评分**：%s（下列按机械分排序）"
              % (p.get("模型层") or {}).get("error"), ""]
    rows = p.get("推荐榜") or []
    if rows:
        L += ["## 推荐榜（模型评分降序）", "",
              pick_table(rows, ["排名", "代码", "名称", "打法", "评分", "评级", "机械分",
                                "现价", "涨跌幅_pct", "所属板块", "理由"],
                         ["排名", "代码", "名称", "打法", "评分", "评级", "机械分",
                          "现价", "涨跌%", "所属板块", "理由"]), ""]
    veto = p.get("被否决") or []
    if veto:
        L += ["## 一票否决（直接被淘汰）", "",
              pick_table(veto, ["阶段", "代码", "名称", "原因"], ["阶段", "代码", "名称", "原因"]), ""]
    L += ["## 机械层（辅助参考，不参与排名）", ""]
    L.append("- 机械分按《机器打分逻辑.txt》对全池打分；缺失项：%s"
             % "、".join("%s（%d 分）" % (x["指标"], x["满分"])
                         for x in (p.get("机械口径") or {}).get("缺失") or []) or "无")
    L.append(pick_table((p.get("机械层") or [])[:40],
                        ["代码", "名称", "机械分", "实得", "可得"],
                        ["代码", "名称", "机械分", "实得", "可得"]))
    L.append("")
    L += ["## 市场环境", "",
          "- 10 年国债 %s%% ｜ 近5日日均成交额 %s 亿 ｜ 近5日日均涨停 %s 家"
          % (env.get("bond10y"), env.get("amount5"), env.get("limitup5")), ""]
    if p.get("降级"):
        L += ["## 数据依赖与降级", ""] + ["- %s" % x for x in p["降级"]] + [""]
    if p.get("风险与不确定性"):
        L += ["## 模型提示的不确定性", ""] + ["- %s" % x for x in p["风险与不确定性"]] + [""]
    L += ["## 说明", "",
          "- 推荐榜按模型评分排序，机械分只作辅助列；两者都不构成绩效承诺。",
          "- 模型不给买卖价位：价位请自行研判（可以再去「报告」或「交易流」里做计划）。",
          "- %s" % (p.get("免责声明") or "本结论由模型评分与机械分共同生成，不构成投资建议。"), ""]
    return "\n".join(L)
