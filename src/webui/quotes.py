# -*- coding: utf-8 -*-
"""行情取数（只读）：批量快照 + 当日分时，带缓存与限速。

为什么单独一个模块：
  · 总控台每一档刷新只要 **1 次** 批量请求（东财 ulist 支持多 secid），
    与报告页「单标的实盘复核」那条路径分开，互不影响；
  · 分时走腾讯（复用 pan.py 已有解析），分钟线每分钟才变，因此最多 60 秒取一次；
  · 所有请求串行并留间隔，避免触发对端限流。

限速口径：最快 10 秒档下，每个标的最多每 60 秒 1 次分时 + 每档 1 次批量报价；
6 只标的合计约 0.2 请求/秒。同轮多个分时请求之间强制间隔 300ms。
"""

import copy
import re
import time
from datetime import date

from .paths import PAN_DIR, aiplan, pan

QUOTE_TTL = 5.0          # 批量报价缓存（秒）
MINUTE_TTL = 60.0        # 分时缓存（秒）
MINUTE_GAP = 0.3         # 同轮分时请求之间的串行间隔（秒）
QUOTE_FIELDS = "f12,f13,f14,f2,f3,f4,f5,f6,f8,f10,f15,f16,f17,f18,f20,f21,f62"

_QUOTE_CACHE = {"ts": 0.0, "data": {}}
_MINUTE_CACHE = {}       # 6 位代码 -> (时间戳, 数据或 None)
_LAST_MINUTE_CALL = [0.0]
_CTX = {"ctx": None, "day": None}


def _ctx():
    """复用 pan.Ctx（只借它的 HTTP 取数层与 http_calls 计数）。"""
    today = date.today()
    if _CTX["ctx"] is None or _CTX["day"] != today:
        _CTX["ctx"] = pan.Ctx(PAN_DIR, today, "post")
        _CTX["day"] = today
    return _CTX["ctx"]


def tx_symbol_of(code):
    """thscode / 6 位代码 → 腾讯分时代码（sh600967 / sz002463）；北交所返回 None。"""
    thscode = thscode_of(code)
    secid = pan._em_secid_of(thscode) if thscode else None
    if not secid or "." not in secid:
        return None
    market, num6 = secid.split(".", 1)
    if market == "1":
        return "sh" + num6
    if market == "0":
        if num6[:1] in ("4", "8", "9"):      # 北交所：腾讯分时口径不同，跳过
            return None
        return "sz" + num6
    return None


def thscode_of(code):
    """6 位代码 → 带市场后缀的 thscode（.SH / .SZ / .BJ）；已带后缀的原样返回。

    pan._em_secid_of() 只接受 `600967.SH` 这种形态，而持仓/自选文件里只有 6 位代码，
    所以这里按代码前缀补市场（指数类如 000300 不在此范围，需要时请显式带后缀传入）。
    """
    text = str(code or "").strip().upper()
    m = re.match(r"^(\d{6})(?:\.(SH|SZ|BJ))?$", text)
    if not m:
        return None
    num6, suffix = m.group(1), m.group(2)
    if suffix:
        return num6 + "." + suffix
    head, two = num6[0], num6[:2]
    if head in ("6", "9") or two == "11":      # 沪市主板 / 科创板 / B股 / 沪市可转债
        return num6 + ".SH"
    if head in ("0", "3", "1"):                # 深市主板 / 创业板 / 深市基金与可转债
        return num6 + ".SZ"
    if head in ("4", "8"):                     # 北交所
        return num6 + ".BJ"
    if head == "5":                            # 沪市基金 / ETF
        return num6 + ".SH"
    return None


def _row_to_quote(row, code6):
    price = aiplan.num_or_none(row.get("f2"))
    if price is None:
        return None
    return {"代码": code6, "名称": row.get("f14"), "价格": price,
            "涨跌幅_pct": aiplan.num_or_none(row.get("f3")),
            "涨跌额": aiplan.num_or_none(row.get("f4")),
            "昨收": aiplan.num_or_none(row.get("f18")), "今开": aiplan.num_or_none(row.get("f17")),
            "最高": aiplan.num_or_none(row.get("f15")), "最低": aiplan.num_or_none(row.get("f16")),
            "成交量": aiplan.num_or_none(row.get("f5")), "成交额": aiplan.num_or_none(row.get("f6")),
            "换手率_pct": aiplan.num_or_none(row.get("f8")), "量比": aiplan.num_or_none(row.get("f10")),
            "主力净流入": aiplan.num_or_none(row.get("f62"))}


def fetch_quotes(codes, refresh=False):
    """批量报价：所有代码共用 1 次东财请求。返回 ({6 位代码: 行情}, 提示列表)。"""
    want = []
    for code in codes or []:
        c6 = aiplan.code6(code)
        if c6 and c6 not in want:
            want.append(c6)
    if not want:
        return {}, []
    now = time.time()
    cached = _QUOTE_CACHE["data"] or {}
    if not refresh and cached and now - _QUOTE_CACHE["ts"] <= QUOTE_TTL \
            and all(c6 in cached for c6 in want):
        return {c6: copy.deepcopy(cached[c6]) for c6 in want}, []

    mapping, secids = {}, []
    for c6 in want:
        secid = pan._em_secid_of(thscode_of(c6) or "")
        if secid:
            mapping[secid] = c6
            secids.append(secid)
    out, hints = {}, []
    rows = None
    if secids:
        try:
            rows = pan.em_ulist(_ctx(), secids, QUOTE_FIELDS)
        except Exception:            # noqa: BLE001  取数失败按降级处理，不抛给页面
            rows = None
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        c6 = mapping.get("%s.%s" % (row.get("f13"), row.get("f12")))
        if not c6:
            continue
        item = _row_to_quote(row, c6)
        if item:
            out[c6] = item
    if not out:
        hints.append("东财批量行情没取到（限流或断网）：继续用本地口径价格")
    else:
        missing = [c6 for c6 in want if c6 not in out]
        if missing:
            hints.append("%d 只标的没拿到实时价（停牌或代码异常）：继续用本地口径" % len(missing))
        _QUOTE_CACHE["ts"] = now
        _QUOTE_CACHE["data"] = {**cached, **out}
    return out, hints


def fetch_minutes(code, max_age=MINUTE_TTL, refresh=False):
    """当日分时（腾讯，复用 pan.fetch_tx_minute）。取不到返回 None。

    分钟线每分钟才更新，所以 max_age 默认 60 秒；同轮多标的按 MINUTE_GAP 串行。
    """
    c6 = aiplan.code6(code)
    if not c6:
        return None
    now = time.time()
    hit = _MINUTE_CACHE.get(c6)
    if hit and not refresh and now - hit[0] <= max_age:
        return copy.deepcopy(hit[1]) if hit[1] else None
    sym = tx_symbol_of(c6)
    if not sym:
        _MINUTE_CACHE[c6] = (now, None)
        return None
    gap = MINUTE_GAP - (now - _LAST_MINUTE_CALL[0])
    if gap > 0:
        time.sleep(gap)
    _LAST_MINUTE_CALL[0] = time.time()
    try:
        rows = pan.fetch_tx_minute(_ctx(), sym)[0]
    except Exception:                # noqa: BLE001
        rows = None
    if not rows:
        _MINUTE_CACHE[c6] = (now, None)
        return None
    data = {"代码": c6,
            "时间": [r.get("时间") for r in rows],
            "价格": [r.get("价格") for r in rows],
            "均价": [r.get("分时均价") for r in rows],
            "最新": rows[-1].get("价格"),
            "抓取时间": time.strftime("%H:%M:%S")}
    _MINUTE_CACHE[c6] = (now, data)
    return copy.deepcopy(data)
