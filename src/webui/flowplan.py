# -*- coding: utf-8 -*-
"""交易流的硬约束与自动校正（纯函数：不取数、不调模型、不写盘）。

用户口径：机械打分只定「能不能做、最大仓位多少、最多亏多少、这个打法允许多宽的止损」，
具体买卖价位仍由模型给；模型越界时程序**自动校正**，并把「原值 → 校正值 → 原因」
写进产物与页面（`约束校正`）。

档位（机械分百分制，见 ask 口径）：≥70 可满配 / 55-70 ≤2/3 / 40-55 ≤1/3 且只减不加 /
<40 或命中一票否决 → 只减不加（持仓按文件口径「次日开盘价全额清仓」）。
亏损预算 = 该标的分配资金 × 流最大亏损%；止损幅度上限按打法（超短线 3% / 短线 5% / 波段 8%）。
"""

from .paths import num

LOT = 100                                   # 一手 = 100 股
STYLE_STOP_PCT = {"超短线": 3.0, "短线": 5.0, "波段": 8.0}
DEFAULT_STOP_PCT = 5.0
TIERS = ({"低": 70.0, "档位": "可满配", "仓位上限_pct": 100.0},
         {"低": 55.0, "档位": "半配", "仓位上限_pct": 66.67},
         {"低": 40.0, "档位": "小仓", "仓位上限_pct": 33.33})
BUY_WORDS = ("建仓", "加仓", "买入", "补仓")
SELL_WORDS = ("减仓", "清仓", "卖出", "止损", "止盈")
TICK = 0.001                                # 价格最小变动（A 股 0.001 元）修正量


def _r(v, nd=3):
    return None if v is None else round(float(v), nd)


def kind_of(action):
    """动作 → 「买入 / 卖出 / 其它」（与 plancheck.plan_kind 同口径的轻量版）。"""
    text = str(action or "")
    if any(w in text for w in BUY_WORDS):
        return "买入"
    if any(w in text for w in SELL_WORDS):
        return "卖出"
    return "其它"


def stop_limit_pct(style):
    """这个打法允许的最大止损幅度（%）。"""
    return STYLE_STOP_PCT.get(str(style or ""), DEFAULT_STOP_PCT)


def tier_of(value, vetoes=None):
    """机械分 + 一票否决 → 仓位档位。"""
    if vetoes:
        reasons = "；".join(str(v.get("原因") or "") for v in vetoes if isinstance(v, dict))
        return {"档位": "一票否决", "仓位上限_pct": 0.0, "允许买入": False,
                "说明": "命中一票否决（%s）：不给买入条目；持仓按文件口径「次日开盘价全额清仓」"
                        % (reasons or "见否决明细")}
    v = num(value)
    if v is None:
        return {"档位": "小仓", "仓位上限_pct": TIERS[-1]["仓位上限_pct"], "允许买入": True,
                "说明": "机械分缺失：按最低可买档（小仓）处理"}
    for t in TIERS:
        if v >= t["低"]:
            return {"档位": t["档位"], "仓位上限_pct": t["仓位上限_pct"], "允许买入": True,
                    "说明": "机械分 %s ≥ %s：仓位上限 %.0f%% 分配资金"
                            % (_fmt(v, 1), _fmt(t["低"], 0), t["仓位上限_pct"])}
    return {"档位": "只减不加", "仓位上限_pct": 0.0, "允许买入": False,
            "说明": "机械分 %s < 40：不给买入条目，只做减仓 / 清仓" % _fmt(v, 1)}


def _fmt(v, nd=2):
    return "—" if v is None else ("%.*f" % (int(nd), v))


def constraints(score, doc, node, price):
    """机械分 + 流参数 + 打法 → 模型必须遵守的硬约束块（同一块也给页面显示）。"""
    score = score or {}
    params = doc.get("参数") or {}
    pnl = node.get("盈亏") or {}
    pos = node.get("持仓") or {}
    alloc = num(node.get("分配资金")) or 0.0
    loss_pct = num(params.get("最大亏损_pct")) or 0.0
    target_pct = num(params.get("目标收益率_pct")) or 0.0
    style = node.get("打法") or "短线"
    m = score.get("机械分") or {}
    veto = score.get("否决") or []
    tier = tier_of(m.get("机械分"), veto)
    held = num(pos.get("股数")) or 0.0
    cur = num(price)
    halt = num(pos.get("平均成本"))
    held_value = (held * cur) if (cur is not None and held) else 0.0
    amount_cap = round(alloc * tier["仓位上限_pct"] / 100.0, 2)
    out = {
        "机械分": num(m.get("机械分")), "实得": num(m.get("实得")), "可得": num(m.get("可得")),
        "模块": score.get("模块") or [], "缺失": score.get("缺失") or [],
        "否决": veto, "行业": score.get("行业"),
        "档位": tier["档位"], "仓位上限_pct": tier["仓位上限_pct"],
        "允许买入": tier["允许买入"], "档位说明": tier["说明"],
        "流资金": num(params.get("流资金")), "分配资金": alloc,
        "目标收益率_pct": target_pct, "最大亏损_pct": loss_pct,
        "目标盈利_元": round(alloc * target_pct / 100.0, 2),
        "亏损预算_元": round(alloc * loss_pct / 100.0, 2),
        "仓位金额上限_元": amount_cap,
        "允许新增金额_元": round(max(0.0, amount_cap - held_value), 2),
        "打法": style, "止损幅度上限_pct": stop_limit_pct(style),
        "持仓股数": held, "可用股数": num(pos.get("可用")), "平均成本": halt,
        "已实现_元": num(pnl.get("已实现_元")),
        "现价": cur,
        "口径": "机械分只定约束（可做性 / 仓位 / 亏损预算 / 止损幅度），具体价位仍由模型给；"
                "越界会被程序自动校正并标注",
    }
    if not tier["允许买入"]:
        out["禁止动作"] = ["建仓", "加仓"]
    return out


# ---------------------------------------------------------------------------
# 事实包章节（给模型的文字）
# ---------------------------------------------------------------------------

def score_lines(score, cons):
    """【机械打分（100分制）】章节。"""
    score = score or {}
    cons = cons or {}
    m = score.get("机械分") or {}
    if not m:
        return "- 本次没有取到机械打分（%s）：约束按缺失档处理，缺失项写进「数据依赖」。" \
               % (score.get("错误") or "取数失败")
    miss = score.get("缺失") or cons.get("缺失") or []
    veto = score.get("否决") or cons.get("否决") or []
    out = ["- 机械分：**%s / 100**（实得 %s ÷ 可得 %s）× 100 ｜ 行业：%s ｜ 取数：%s"
           % (_fmt(num(m.get("机械分")), 1), _fmt(num(m.get("实得")), 1),
              _fmt(num(m.get("可得")), 1),
              score.get("行业") or cons.get("行业") or "—", score.get("取数时间") or "—"),
           "- 模块小计（满分 → 实得）：" + " ｜ ".join(
               "%s %s/%s" % (r.get("模块"), _fmt(r.get("得分"), 1), r.get("满分"))
               for r in (score.get("模块") or []))]
    seen = []
    for row in miss:
        text = "%s（%s 分，%s）" % (row.get("指标"), row.get("满分"), row.get("原因"))
        if text not in seen:
            seen.append(text)
    out.append("- 缺失项（只从分母里去掉，不记 0、不摊权）：%s"
               % ("；".join(seen) if seen else "无"))
    out.append("- 一票否决：%s"
               % ("；".join("%s：%s" % (v.get("范围"), v.get("原因")) for v in veto)
                  if veto else "未命中"))
    out.append("- 原始指标（供你写依据，不要改算）：" + " ｜ ".join(
        "%s %s" % (k, _fmt(v, 3) if isinstance(v, (int, float)) else v)
        for k, v in (score.get("技术") or {}).items() if v is not None))
    if score.get("降级"):
        out.append("- 取数降级：" + "；".join(str(x)[:100] for x in score["降级"][:6]))
    return "\n".join(out)


def constraint_lines(cons):
    """【硬约束（不可越界）】章节：模型必须在这个框子里出价位与股数。"""
    out = ["- 仓位档位：%s（仓位上限 %.2f%% 分配资金 → %s 元）｜ 允许买入：%s"
           % (cons.get("档位"), cons.get("仓位上限_pct") or 0.0,
              _fmt(cons.get("仓位金额上限_元"), 0), "是" if cons.get("允许买入") else "否"),
           "- 亏损预算（硬约束）：本标的分配资金 %s 元 × 流最大亏损 %s%% = **%s 元**；"
           "即「Σ 计划买入股数 ×（买入价 − 止损价）≤ %s 元」，越界会被自动下调股数。"
           % (_fmt(cons.get("分配资金"), 0), _fmt(cons.get("最大亏损_pct"), 2),
              _fmt(cons.get("亏损预算_元"), 0), _fmt(cons.get("亏损预算_元"), 0)),
           "- 仓位金额上限：本次可新增金额 %s 元（仓位上限 %s 元 − 当前持仓市值）；"
           "加仓后总市值不得超过仓位上限。"
           % (_fmt(cons.get("允许新增金额_元"), 0), _fmt(cons.get("仓位金额上限_元"), 0)),
           "- 打法「%s」的止损幅度上限：**%s%%**（相对买入价）——止损价比买入价最多低这么多，"
           "超了会被自动收窄。" % (cons.get("打法"), _fmt(cons.get("止损幅度上限_pct"), 1)),
           "- 股数必须是 100 的整数倍；买入类条目可以参考："
           "股数上限 ≈ 亏损预算 ÷（买入价 − 止损价）。",
           "- 止盈目标：本标的分配资金 %s 元 × 流目标收益率 %s%% = %s 元；"
           "止盈价位至少要能覆盖这个盈利额，否则页面会标注完成度。"
           % (_fmt(cons.get("分配资金"), 0), _fmt(cons.get("目标收益率_pct"), 2),
              _fmt(cons.get("目标盈利_元"), 0)),
           "- 当前持仓 %s 股（可用 %s）｜ 平均成本 %s ｜ 现价 %s"
           % (_fmt(cons.get("持仓股数"), 0), _fmt(cons.get("可用股数"), 0),
              _fmt(cons.get("平均成本"), 3), _fmt(cons.get("现价"), 3))]
    if cons.get("禁止动作"):
        out.append("- 本次禁止：%s（档位 %s：%s）"
                   % ("、".join(cons["禁止动作"]), cons.get("档位"), cons.get("档位说明")))
    else:
        out.append("- 档位说明：%s" % cons.get("档位说明"))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 自动校正
# ---------------------------------------------------------------------------

def _entries(plan):
    rows = plan.get("计划") if isinstance(plan.get("计划"), list) else []
    return [e for e in rows if isinstance(e, dict)]


def _range_of(entry):
    """条目价格区间 → (下沿, 上沿)；区间缺失时返回 (None, None)。"""
    rng = entry.get("价格区间")
    if isinstance(rng, (list, tuple)) and rng:
        vals = [num(x) for x in rng]
        vals = [v for v in vals if v is not None]
        if vals:
            return min(vals), max(vals)
    v = num(rng)
    return (v, v) if v is not None else (None, None)


def enforce(plan, cons, price=None):
    """模型给的计划 → 按硬约束校正。返回 (校正后的计划副本, 校正记录[], 提示[])。"""
    out = dict(plan or {})
    entries = [dict(e) for e in _entries(out)]
    out["计划"] = entries
    prices = dict(out.get("关键价位") or {}) if isinstance(out.get("关键价位"), dict) else {}
    out["关键价位"] = prices
    corrections, notes = [], []
    cons = cons or {}
    cur = num(price) if price is not None else num(cons.get("现价"))
    limit = num(cons.get("止损幅度上限_pct")) or DEFAULT_STOP_PCT

    def note(item, old, new, why):
        corrections.append({"项": item, "原值": old, "校正值": new, "原因": why})

    buys = [e for e in entries if kind_of(e.get("动作")) == "买入" and not e.get("作废")]
    sells = [e for e in entries if kind_of(e.get("动作")) == "卖出" and not e.get("作废")]

    # 1) 档位不允许买入：直接作废所有买入条目
    if not cons.get("允许买入", True):
        for e in buys:
            e["作废"] = True
            e["作废原因"] = "档位「%s」不允许买入：%s" % (cons.get("档位"), cons.get("档位说明"))
            note("作废 %s 条目" % e.get("动作"), e.get("价格区间"), None, e["作废原因"])
        buys = []

    # 2) 止损价：这个打法允许的「最远止损」= 买入参考价 ×(1−幅度上限)；
    #    比它还低就是风险过大，收窄到该价位；止损落在现价上方则下调到现价下方（多头自洽）。
    stop = num(prices.get("止损价"))
    buys_hi = [v for v in (_range_of(e)[1] for e in buys) if v is not None]
    anchor = max(buys_hi) if buys_hi else (num(cons.get("平均成本")) or cur)
    floor_stop = round(anchor * (1.0 - limit / 100.0), 3) if anchor else None
    if stop is None:
        stop = floor_stop
        if stop is not None:
            note("止损价", None, stop, "模型没给止损价：按打法「%s」的 %.1f%% 幅度上限推算"
                 % (cons.get("打法"), limit))
    elif floor_stop is not None and stop < floor_stop - 1e-9:
        note("止损价", stop, floor_stop,
             "距买入价超过打法「%s」的 %.1f%% 幅度上限：收窄到上限价位" % (cons.get("打法"), limit))
        stop = floor_stop
    if stop is not None and cur is not None and stop >= cur:
        fixed = round(min(cur - TICK, floor_stop if floor_stop is not None else cur - TICK), 3)
        note("止损价", stop, fixed, "止损价 ≥ 现价 %s：多头自洽要求止损落在现价下方" % _fmt(cur))
        stop = fixed
    if stop is not None:
        prices["止损价"] = stop

    # 3) 买入区间不能落在止损下方（否则「跌破止损即离场」与买点互斥）
    if stop is not None:
        for e in buys:
            lo, hi = _range_of(e)
            if lo is None:
                continue
            floor = round(stop + TICK, 3)
            if lo < floor:
                hi = max(hi or floor, floor)
                e["价格区间"] = [floor, hi]
                note("买入区间", [lo, hi], e["价格区间"],
                     "区间下沿低于止损价 %s：为保持「止损即离场」自洽，上移到止损上方"
                     % _fmt(stop))

    # 4) 股数：按亏损预算 + 仓位金额上限逐条下调（优先级小的先占预算）
    order = sorted(range(len(buys)), key=lambda i: (num(buys[i].get("优先级")) or 3, i))
    budget_left = num(cons.get("亏损预算_元")) or 0.0
    amount_left = num(cons.get("允许新增金额_元"))
    if amount_left is None:
        amount_left = num(cons.get("仓位金额上限_元")) or 0.0
    held = num(cons.get("持仓股数")) or 0.0
    bought = 0.0
    for i in order:
        e = buys[i]
        lo, hi = _range_of(e)
        ref = hi if hi is not None else (lo if lo is not None else cur)
        qty = num(e.get("股数"))
        caps = []
        if ref is not None and stop is not None and ref > stop:
            caps.append(budget_left / (ref - stop))
        if ref:
            caps.append(amount_left / ref)
        cap = min(caps) if caps else None
        cap = None if cap is None else int(cap // LOT) * LOT
        if cap is not None and cap < LOT:
            e["作废"] = True
            e["作废原因"] = ("预算不足：亏损预算剩 %s 元、可新增金额剩 %s 元，"
                             "买不到 1 手（100 股）" % (_fmt(budget_left, 0), _fmt(amount_left, 0)))
            note("作废 %s 条目" % e.get("动作"), qty, None, e["作废原因"])
            continue
        if qty is None:
            if cap:
                e["股数"] = cap
                note("%s 股数" % e.get("动作"), None, cap, "模型没给股数：按预算与仓位上限填上限")
        elif cap is not None and qty > cap:
            e["股数"] = cap
            note("%s 股数" % e.get("动作"), qty, cap,
                 "超出亏损预算 %.2f 元或仓位上限：下调到可执行的最大整手数"
                 % (num(cons.get("亏损预算_元")) or 0.0))
            qty = cap
        q = num(e.get("股数")) or 0.0
        if ref and q:
            e["金额_元"] = round(q * ref, 2)
        if q and ref is not None and stop is not None:
            budget_left = max(0.0, budget_left - q * max(0.0, ref - stop))
            amount_left = max(0.0, amount_left - q * ref)
        bought += q

    # 5) 卖出条目：不能超过「当前持仓 + 本次计划买入」
    avail = held + bought
    for e in sells:
        lo, hi = _range_of(e)
        qty = num(e.get("股数"))
        cap = int(avail // LOT) * LOT if avail >= LOT else 0
        if cap <= 0:
            e["作废"] = True
            e["作废原因"] = "当前没有可卖持仓（持仓 %s 股），这条没有可执行数量" % _fmt(held, 0)
            note("作废 %s 条目" % e.get("动作"), qty, None, e["作废原因"])
            continue
        target = cap
        if any(w in str(e.get("动作") or "") for w in ("清仓", "止损")):
            target = cap
        elif qty is None or qty > cap:
            target = cap
        if num(e.get("股数")) != target:
            note("%s 股数" % e.get("动作"), qty, target,
                 "超过可卖数量（持仓 %s + 本次计划买入 %s = %s 股）：下调到可卖上限"
                 % (_fmt(held, 0), _fmt(bought, 0), _fmt(avail, 0)))
            e["股数"] = target
        ref = hi if hi is not None else lo
        if ref and num(e.get("股数")):
            e["金额_元"] = round(num(e["股数"]) * ref, 2)

    tips = target_tips(prices, cons, bought)
    notes.extend(risk_tip(stop, cons, cons.get("持仓股数")) + tips)
    return out, corrections, notes


def target_tips(prices, cons, bought=0.0):
    """止盈完成度提示（不硬改价位）：按最近的目标位算能完成多少目标盈利。"""
    goals = []
    for g in (prices or {}).get("目标位") or []:
        v = num(g.get("价位")) if isinstance(g, dict) else num(g)
        if v is not None:
            goals.append(v)
    want = num(cons.get("目标盈利_元"))
    cost = num(cons.get("平均成本"))
    held = (num(cons.get("持仓股数")) or 0.0) + (num(bought) or 0.0)
    done = num(cons.get("已实现_元")) or 0.0
    if not goals or want in (None, 0) or cost is None or held <= 0:
        return []
    cur = num(cons.get("现价"))
    above = sorted([v for v in goals if cur is None or v > cur])
    goal = above[0] if above else max(goals)
    pct = (done + (goal - cost) * held) / want * 100.0
    return ["止盈点 %s：按当前口径能完成本标的目标盈利的 %.0f%%（目标 %s 元）"
            % (_fmt(goal, 3), pct, _fmt(want, 0))]


def risk_tip(stop, cons, held):
    """现有持仓的止损敞口 vs 亏损预算：超了就提示（不自动砍仓——砍仓是你的决定）。"""
    budget = num(cons.get("亏损预算_元"))
    cost = num(cons.get("平均成本"))
    stop = num(stop)
    qty = num(held) or 0.0
    if not budget or cost is None or stop is None or qty <= 0:
        return []
    risk = max(0.0, (cost - stop) * qty)
    if risk <= budget + 0.5:
        return []
    return ["当前持仓 %s 股按止损 %s 计算的敞口 %s 元 > 本标的亏损预算 %s 元"
            "（多出 %s 元）：计划里应体现分批减仓，或把止损上移。"
            % (_fmt(qty, 0), _fmt(stop, 3), _fmt(risk, 0), _fmt(budget, 0),
               _fmt(risk - budget, 0))]


def summary(cons, corrections):
    """一行日志摘要。"""
    return ("机械分 %s ｜ 档位 %s（仓位上限 %s 元）｜ 亏损预算 %s 元 ｜ 打法止损上限 %s%% ｜ 校正 %d 条"
            % (_fmt((cons or {}).get("机械分"), 1), (cons or {}).get("档位"),
               _fmt((cons or {}).get("仓位金额上限_元"), 0),
               _fmt((cons or {}).get("亏损预算_元"), 0),
               _fmt((cons or {}).get("止损幅度上限_pct"), 1), len(corrections or [])))
