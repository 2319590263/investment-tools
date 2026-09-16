# -*- coding: utf-8 -*-
"""同花顺成交的窗口与去重（当日成交 + 近一周历史成交）。

为什么单独一个模块：只拉「当日成交」在凌晨/非交易日会拿到空数据（用户实测），
所以同步改成「当日成交 + 历史成交（近一周）」合并去重后再写台账；这段纯逻辑
放在这里，holdings_ths.py 只负责驱动客户端，两边都不用顶着 800 行上限。
"""

import re
from datetime import datetime, timedelta
from typing import Any, Callable

HISTORY_TRADE_MENUS = (("查询[F4]", "历史成交"), ("查询[F4]", "成交查询"),
                       ("查询[F4]", "历史成交查询"), ("查询[F4]", "对账单"))
TRADE_WINDOW_DAYS = 7          # 成交窗口：近 7 个自然日（含当日）


def trade_date_of(text: Any, today: datetime | None = None) -> str:
    """成交日期文本 → YYYY-MM-DD；认不出返回空串。

    同花顺历史成交页的日期可能是 2026-09-16 / 20260916 / 09-16（当年省略年份）。
    """
    digits = re.sub(r"\D", "", str(text or ""))
    today = today or datetime.now()
    if len(digits) >= 8:
        year, month, day = digits[:4], digits[4:6], digits[6:8]
    elif len(digits) == 6:                     # YYMMDD
        year, month, day = ("20" + digits[:2]), digits[2:4], digits[4:6]
    elif len(digits) == 4:                     # MM-DD（当年）
        year, month, day = str(today.year), digits[:2], digits[2:4]
    else:
        return ""
    try:
        return datetime(int(year), int(month), int(day)).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _num(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def merge_trades(recent: list[dict], history: list[dict], days: int = TRADE_WINDOW_DAYS,
                 today: datetime | None = None, code6: Callable[[Any], str] | None = None
                 ) -> tuple[list[dict], dict]:
    """当日成交 + 历史成交 → 近 days 天**去重**后的成交列表。

    去重口径与台账一致（委托序号 + 日期 + 时间 + 代码 + 价格 + 数量）；超出窗口的剔除。
    返回 (成交列表, 窗口信息)。
    """
    code6 = code6 or (lambda v: str(v or "").strip())
    today = today or datetime.now()
    cutoff = (today - timedelta(days=max(1, int(days)) - 1)).strftime("%Y-%m-%d")
    merged, seen, dropped = [], set(), 0
    for trade in list(recent or []) + list(history or []):
        row = dict(trade or {})
        day = trade_date_of(row.get("成交日期"), today)
        if day:
            row["成交日期"] = day
        if day and day < cutoff:
            dropped += 1
            continue
        key = (row.get("成交日期"), str(row.get("成交时间") or "").strip(),
               str(row.get("委托序号") or "").strip(), code6(row.get("证券代码")),
               _num(row.get("成交价格")), _num(row.get("成交数量")))
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)
    merged.sort(key=lambda r: (str(r.get("成交日期") or ""), str(r.get("成交时间") or "")))
    return merged, {"天数": int(days), "起": cutoff, "止": today.strftime("%Y-%m-%d"),
                    "当日条数": len(recent or []), "历史条数": len(history or []),
                    "去重后": len(merged), "超窗口剔除": dropped}


def read_history_trades(user: Any, log: Callable[[str], None] | None = None) -> list:
    """读同花顺「历史成交」页的原始表格（近一周）。取不到返回 []，不抛异常。"""
    log = log or (lambda _text: None)
    for path in HISTORY_TRADE_MENUS:
        try:
            user._switch_left_menus(list(path))
            raw = user._get_grid_data(user._config.COMMON_GRID_CONTROL_ID)
        except Exception as exc:            # noqa: BLE001  菜单名不对/页面不存在都算降级
            log("[..] 历史成交页「%s」读取失败：%s" % ("/".join(path), str(exc)[:80]))
            continue
        if raw:
            log("[OK] 历史成交页「%s」读到 %d 行" % ("/".join(path), len(raw)))
            return raw
        log("[..] 历史成交页「%s」为空，换下一个候选菜单" % "/".join(path))
    log("[WARN] 历史成交页没取到数据（菜单名与客户端版本不符）：本次只用当日成交")
    return []
