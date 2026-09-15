# -*- coding: utf-8 -*-
"""K 线技术指标（纯函数）：机械打分「模块4 技术面」的算法部分。

输入统一是日K列表（新→旧或旧→新都可以，本模块内部按升序处理），每根 =
{"date","open","high","low","close","vol"}。只算数值，不下结论（阈值在 mech.py）。
"""


def _num(v):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    if n != n:                    # NaN
        return None
    return n


def bars_sorted(bars):
    """按日期升序（最早在前）。缺日期或重复都不报错，只按顺序排。"""
    rows = [b for b in (bars or []) if isinstance(b, dict) and _num(b.get("close")) is not None]
    try:
        rows.sort(key=lambda b: str(b.get("date") or ""))
    except TypeError:
        pass
    return rows


def closes(bars):
    return [_num(b.get("close")) for b in bars_sorted(bars)]


def vols(bars):
    return [_num(b.get("vol")) for b in bars_sorted(bars)]


def sma(vals, n, offset=0):
    """最近 n 根的简单均值（offset=0 表示到最后一根为止）。样本不足返回 None。"""
    xs = [v for v in vals if v is not None]
    if len(xs) < n:
        return None
    end = len(xs) - offset
    window = xs[max(0, end - n):end]
    if len(window) < n:
        return None
    return sum(window) / float(n)


def ema_series(vals, n):
    """标准 EMA（首值用第一个样本）。"""
    k = 2.0 / (n + 1)
    out, prev = [], None
    for v in vals:
        if v is None:
            out.append(prev)
            continue
        prev = v if prev is None else (v * k + prev * (1 - k))
        out.append(prev)
    return out


def macd(closes_, fast=12, slow=26, signal=9):
    """返回最后一根的 (DIF, DEA)；样本不足返回 (None, None)。"""
    xs = [v for v in (closes_ or []) if v is not None]
    if len(xs) < slow + signal:
        return None, None
    ef, es = ema_series(xs, fast), ema_series(xs, slow)
    dif = [(a - b) if (a is not None and b is not None) else None for a, b in zip(ef, es)]
    dea = ema_series([d for d in dif], signal)
    return dif[-1], dea[-1]


def rsi(closes_, n=14):
    """标准 RSI(14)：用 n 根涨跌幅的简单均值。样本不足返回 None。"""
    xs = [v for v in (closes_ or []) if v is not None]
    if len(xs) < n + 1:
        return None
    gains, losses = [], []
    for i in range(len(xs) - n, len(xs)):
        diff = xs[i] - xs[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains) / float(n)
    avg_loss = sum(losses) / float(n)
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def return_pct(vals, n):
    """最近 n 个交易日的区间涨幅（%）。"""
    xs = [v for v in (vals or []) if v is not None]
    if len(xs) < n + 1:
        return None
    start, end = xs[-(n + 1)], xs[-1]
    if not start:
        return None
    return (end / start - 1.0) * 100.0


def vol_ratio(bars, short=20, long=60):
    """短均量 / 长均量；样本不足返回 None。"""
    v = vols(bars)
    a, b = sma(v, short), sma(v, long)
    if not a or not b:
        return None
    return a / b


def volume_price_ratio(bars, days=5):
    """近 days 日「阳线均量 / 阴线均量」；缺一侧返回 None。"""
    rows = bars_sorted(bars)[-days:]
    up, dn = [], []
    prev = None
    for b in rows:
        c, v = _num(b.get("close")), _num(b.get("vol"))
        o = _num(b.get("open"))
        base = o if o is not None else prev
        if c is None or v is None or base is None:
            prev = c if c is not None else prev
            continue
        (up if c >= base else dn).append(v)
        prev = c
    if not up or not dn:
        return None
    up_avg = sum(up) / float(len(up))
    dn_avg = sum(dn) / float(len(dn))
    if dn_avg == 0:
        return None
    return up_avg / dn_avg


def range_pos_pct(bars, n=20):
    """现价 n 日区间位置（0=最低，100=最高）；样本不足返回 None。"""
    rows = bars_sorted(bars)[-n:]
    highs = [_num(b.get("high")) for b in rows]
    lows = [_num(b.get("low")) for b in rows]
    highs = [x for x in highs if x is not None]
    lows = [x for x in lows if x is not None]
    close = _num(rows[-1].get("close")) if rows else None
    if not highs or not lows or close is None:
        return None
    hi, lo = max(highs), min(lows)
    if hi == lo:
        return 50.0
    return max(0.0, min(100.0, (close - lo) / (hi - lo) * 100.0))


def excess_return_pct(bars, bench_bars, n=20):
    """近 n 日相对基准（沪深300）的超额收益（百分点）。"""
    a = return_pct(closes(bars), n)
    b = return_pct(closes(bench_bars), n)
    if a is None or b is None:
        return None
    return a - b
