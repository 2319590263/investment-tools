# -*- coding: utf-8 -*-
"""账户 / 模型 / 持仓 / 自选股的读取与写回（写回前必留 .bak）。"""

import json
import os

from .paths import ACCOUNT_PATH, MODELS_PATH, POOL_PATH, WATCHLIST_PATH, aiplan, atomic_write, num, read_bytes, read_text, rel
from .sources import stock3d_snapshot


def _account_fields(text):
    """账户表单字段：模板顺序 + 保留用户文件里实际在用的 ETF_/ETC_ 前缀字段。"""
    base = ["总资金", "单票仓位上限_pct", "单笔最大亏损_pct", "最低现金比例_pct",
            "最大加仓次数", "佣金费率_pct", "佣金最低_元"]
    tail = ["印花税率_pct", "过户费率_pct", "风险偏好", "api_key"]
    try:
        keys = list((json.loads(text) or {}).keys())
    except (ValueError, TypeError):
        keys = []
    etf = [k for k in keys if k.startswith(("ETF_", "ETC_"))]
    if not etf:
        etf = [k for k in aiplan.ACCOUNT_TEMPLATE.keys() if k.startswith("ETF_")]
    if not etf:
        etf = ["ETF_佣金费率_pct", "ETF_佣金最低_元"]
    return base + etf + tail


ACCOUNT_FIELD_HINT = {
    "总资金": "必填，真实可用资金（元）；缺失或为 0 时主流程直接报错退出",
    "单票仓位上限_pct": "单只标的市值占总资金上限",
    "单笔最大亏损_pct": "占总资金比例，用于机械风控校验",
    "佣金费率_pct": "A股佣金费率，按成交金额计；不足最低值按最低值收",
    "ETF_佣金费率_pct": "ETF 专用佣金费率（ETF 免印花税与过户费）",
    "ETF_佣金最低_元": "ETF 佣金最低值；旧前缀 ETC_ 同样被 aiplan 识别，两者同时存在时 ETF_ 优先",
    "ETC_佣金费率_pct": "旧前缀写法，等效于 ETF_佣金费率_pct",
    "ETC_佣金最低_元": "旧前缀写法，等效于 ETF_佣金最低_元",
    "印花税率_pct": "仅卖出 A 股收取，ETF 免",
    "过户费率_pct": "仅 A 股收取，ETF 免",
    "api_key": "可留空：优先级低于命令行与环境变量，高于 Desktop\\token.txt",
}


def load_account_bundle():
    text = read_text(ACCOUNT_PATH)
    parsed, err = aiplan.load_account(ACCOUNT_PATH)
    return {
        "路径": rel(ACCOUNT_PATH),
        "存在": os.path.exists(ACCOUNT_PATH),
        "配置": parsed,
        "解析错误": err,
        "原文": text,
        "模板字段": _account_fields(text),
        "字段说明": ACCOUNT_FIELD_HINT,
    }


WATCHLIST_HEADER = "| 证券代码 | 证券名称 | 备注 |\n|---|---|---|\n"


def load_watchlist():
    """返回 (是否新建, 条目列表)。条目：{代码, 名称, 备注, 现价, 涨跌幅_pct, 在缓存}"""
    if not os.path.exists(WATCHLIST_PATH):
        atomic_write(WATCHLIST_PATH, WATCHLIST_HEADER, newline="\n")
    _hdr, rows = aiplan.parse_pool_rows(WATCHLIST_PATH)
    s3_path, s3doc, cached = stock3d_snapshot()
    snap = {}
    for sym in (s3doc or {}).get("symbols") or []:
        c6 = aiplan.code6(sym.get("code"))
        if c6:
            tech = (sym.get("tech") or {}).get("snapshot") or {}
            snap[c6] = {"现价": num(tech.get("现价")), "涨跌幅_pct": num(tech.get("涨跌幅_pct")),
                        "名称": sym.get("name")}
    out = []
    for r in rows:
        c6 = aiplan.code6(r.get("证券代码") or r.get("代码") or "")
        if not c6:
            continue
        info = snap.get(c6) or {}
        out.append({
            "代码": c6,
            "名称": (r.get("证券名称") or r.get("名称") or info.get("名称") or "").strip(),
            "备注": (r.get("备注") or "").strip(),
            "现价": info.get("现价"),
            "涨跌幅_pct": info.get("涨跌幅_pct"),
            "在缓存": c6 in cached,
        })
    return out


def save_watchlist(items):
    """按统一表格格式整份写回自选股。"""
    lines = ["| 证券代码 | 证券名称 | 备注 |", "|---|---|---|"]
    for it in items:
        lines.append("| %s | %s | %s |" % (it.get("代码", ""), it.get("名称", ""), it.get("备注", "")))
    atomic_write(WATCHLIST_PATH, "\n".join(lines) + "\n", newline="\n")
    return items


def watchlist_add(code, name="", note=""):
    c6 = aiplan.code6(code or "")
    if not c6:
        return None, "请填 6 位证券代码"
    items = load_watchlist()
    for it in items:
        if it["代码"] == c6:
            if name and not it["名称"]:
                it["名称"] = name
            if note:
                it["备注"] = note
            save_watchlist(items)
            return items, None
    items.append({"代码": c6, "名称": (name or "").strip(), "备注": (note or "").strip()})
    save_watchlist(items)
    return items, None


def watchlist_remove(code):
    c6 = aiplan.code6(code or "")
    items = load_watchlist()
    keep = [it for it in items if it["代码"] != c6]
    if len(keep) == len(items):
        return None, "自选股里没有 %s" % (c6 or code)
    save_watchlist(keep)
    return keep, None


def load_models_bundle():
    text = read_text(MODELS_PATH)
    cfg, err = aiplan.load_models(MODELS_PATH)
    providers, profiles = [], []
    if cfg:
        for p in cfg.get("providers") or []:
            raw_key = str(p.get("api_key") or "")
            key, src = aiplan.resolve_key(p, None)
            envc = str(p.get("key_env") or "")
            env_hit = bool(os.environ.get(envc)) if envc else False
            providers.append({
                "名称": p.get("名称"),
                "协议": p.get("协议"),
                "base_url": p.get("base_url"),
                "路径": p.get("路径"),
                "key_env": envc,
                "环境变量已设置": env_hit,
                "配置内key": bool(raw_key.strip()),
                "key掩码": aiplan.mask_key(key),
                "key来源": src,
                "json_object": bool(p.get("json_object")),
                "余额端点": p.get("余额端点"),
                "模型可选": p.get("模型可选") or [],
                "备注": p.get("备注") or p.get("说明") or "",
            })
        for name, prof in (cfg.get("profiles") or {}).items():
            def role(r):
                c = (prof or {}).get(r)
                if not isinstance(c, dict):
                    return None
                return {
                    "provider": c.get("provider"),
                    "model": c.get("model"),
                    "temperature": c.get("temperature"),
                    "max_tokens": c.get("max_tokens"),
                    "参数": c.get("参数"),
                }
            profiles.append({"名称": name, "研判": role("研判"), "复核": role("复核")})
        providers_ok = True
    else:
        providers_ok = False
    return {
        "路径": rel(MODELS_PATH),
        "存在": os.path.exists(MODELS_PATH),
        "解析错误": err,
        "原文": text,
        "默认_profile": (cfg or {}).get("默认_profile"),
        "profiles_by_phase": (cfg or {}).get("profiles_by_phase") or {},
        "profiles": profiles,
        "providers": providers,
        "模型": (cfg or {}).get("模型") or {},
        "汇率": (cfg or {}).get("汇率") or {},
        "provider可用": providers_ok,
    }


def load_holdings_bundle():
    header, rows = aiplan.parse_pool_rows(POOL_PATH)
    account, acc_err = aiplan.load_account(ACCOUNT_PATH)
    total = num((account or {}).get("总资金"))
    items, sum_mv = [], 0.0
    for row in rows:
        c6 = aiplan.code6(row.get("证券代码") or row.get("代码") or "")
        name = row.get("证券名称") or row.get("名称") or c6
        price = num(row.get("市价"))
        metrics = aiplan.holdings_metrics(row, c6, name, price, account or {})
        mv = num(metrics.get("持仓市值_元"))
        if mv:
            sum_mv += mv
        items.append({
            "代码": c6,
            "名称": name,
            "是否ETF": metrics.get("是否ETF"),
            "持有股数": metrics.get("持有股数"),
            "可用股数_可卖": metrics.get("可用股数_可卖"),
            "冻结股数_当日买入不可卖": metrics.get("冻结股数_当日买入不可卖"),
            "可卖_计划执行日": metrics.get("可卖_计划执行日"),
            "成本价": metrics.get("成本价"),
            "现价": metrics.get("现价"),
            "持仓市值_元": metrics.get("持仓市值_元"),
            "浮动盈亏_元": metrics.get("浮动盈亏_元"),
            "盈亏比例_pct": metrics.get("盈亏比例_pct"),
            "占总资金_pct": metrics.get("占总资金_pct"),
            "回本需涨_pct": metrics.get("回本需涨_pct"),
            "持股天数": metrics.get("持股天数"),
            "当日买入": num(row.get("当日买入")),
            "当日卖出": num(row.get("当日卖出")),
            "原始行": row,
        })
    pos_pct = round(sum_mv / total * 100, 2) if total else None
    return {
        "路径": rel(POOL_PATH),
        "存在": os.path.exists(POOL_PATH),
        "列名": header,
        "原文": read_text(POOL_PATH),
        "行BOM": (read_bytes(POOL_PATH) or b"").startswith(b"\xef\xbb\xbf"),
        "持仓": items,
        "汇总": {
            "总资金": total,
            "持仓市值_元": round(sum_mv, 2),
            "持仓占比_pct": pos_pct,
            "现金_元": round(total - sum_mv, 2) if total else None,
            "现金占比_pct": round(100 - pos_pct, 2) if pos_pct is not None else None,
            "标的数": len(items),
        },
        "账户解析错误": acc_err,
    }
