# -*- coding: utf-8 -*-
"""账户 / 模型 / 持仓 / 自选股的读取与写回（写回前必留 .bak）。"""

import json
import os

from .paths import ACCOUNT_PATH, MODELS_PATH, POOL_PATH, TRACKLIST_PATH, WATCHLIST_PATH, aiplan, atomic_write, num, read_bytes, read_text, rel, save_like
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


def _pool_list(path):
    """读「代码 / 名称 / 备注」三列清单（自选股与跟踪标的是同一种文件格式）。

    条目：{代码, 名称, 备注, 现价, 涨跌幅_pct, 在缓存}；名称与价格取自 stock3d 快照，
    只是显示用的缓存值，不写回文件。
    """
    if not os.path.exists(path):
        atomic_write(path, WATCHLIST_HEADER, newline="\n")
    _hdr, rows = aiplan.parse_pool_rows(path)
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


def _save_pool_list(path, items):
    """按统一表格格式整份写回自选股。"""
    lines = ["| 证券代码 | 证券名称 | 备注 |", "|---|---|---|"]
    for it in items:
        lines.append("| %s | %s | %s |" % (it.get("代码", ""), it.get("名称", ""), it.get("备注", "")))
    atomic_write(path, "\n".join(lines) + "\n", newline="\n")
    return items


def load_watchlist():
    """自选股条目（文件不存在时按标准表头建一个空文件）。"""
    return _pool_list(WATCHLIST_PATH)


def save_watchlist(items):
    return _save_pool_list(WATCHLIST_PATH, items)


def load_tracklist():
    """标的跟踪清单条目（与自选股同格式，互不影响）。"""
    return _pool_list(TRACKLIST_PATH)


def save_tracklist(items):
    return _save_pool_list(TRACKLIST_PATH, items)


def _pool_add(path, code, name="", note=""):
    c6 = aiplan.code6(code or "")
    if not c6:
        return None, "请填 6 位证券代码"
    items = _pool_list(path)
    for it in items:
        if it["代码"] == c6:
            if name and not it["名称"]:
                it["名称"] = name
            if note:
                it["备注"] = note
            _save_pool_list(path, items)
            return items, None
    items.append({"代码": c6, "名称": (name or "").strip(), "备注": (note or "").strip()})
    _save_pool_list(path, items)
    return items, None


def _pool_remove(path, code, label):
    c6 = aiplan.code6(code or "")
    items = _pool_list(path)
    keep = [it for it in items if it["代码"] != c6]
    if len(keep) == len(items):
        return None, "%s 里没有 %s" % (label, c6 or code)
    _save_pool_list(path, keep)
    return keep, None


def watchlist_add(code, name="", note=""):
    return _pool_add(WATCHLIST_PATH, code, name, note)


def watchlist_remove(code):
    return _pool_remove(WATCHLIST_PATH, code, "自选股")


def tracklist_add(code, name="", note=""):
    return _pool_add(TRACKLIST_PATH, code, name, note)


def tracklist_remove(code):
    return _pool_remove(TRACKLIST_PATH, code, "跟踪清单")


def tracklist_import_watchlist():
    """把自选股里还没进跟踪清单的标的补进来。返回 (新增数量, 跟踪清单)。"""
    if not os.path.exists(TRACKLIST_PATH):
        atomic_write(TRACKLIST_PATH, WATCHLIST_HEADER, newline="\n")
    items = _pool_list(TRACKLIST_PATH)
    have = {it["代码"] for it in items}
    added = 0
    for it in _pool_list(WATCHLIST_PATH):
        if it["代码"] in have:
            continue
        items.append({"代码": it["代码"], "名称": it.get("名称") or "", "备注": it.get("备注") or ""})
        have.add(it["代码"])
        added += 1
    if added:
        _save_pool_list(TRACKLIST_PATH, items)
    return added, items


def load_models_bundle():
    """读模型配置（含默认 profile、按时间段 profile、providers 掩码）。"""
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


PROFILE_PHASES = ("prep", "live", "post", "all")


def set_default_profile(name=None, by_phase=None):
    """切换默认 profile（可同时按 prep/live/post/all 分时段指定）。写回前留 .bak。

    只动「默认_profile」与「profiles_by_phase」两个键，providers / profiles 原样保留。
    返回 (模型配置 bundle, 错误)。
    """
    cfg, err = aiplan.load_models(MODELS_PATH)
    if err or not isinstance(cfg, dict):
        return None, err or "模型配置读不出内容"
    profiles = cfg.get("profiles") or {}
    merged = dict(cfg)
    changed = []
    target = str(name or "").strip()
    if target:
        if target not in profiles:
            return None, "profile 不存在：%s（现有：%s）" % (target, "、".join(profiles) or "无")
        if target != str(cfg.get("默认_profile") or "").strip():
            merged["默认_profile"] = target
            changed.append("默认_profile=%s" % target)
    if isinstance(by_phase, dict) and by_phase:
        phase_map = dict(merged.get("profiles_by_phase") or {})
        for phase in PROFILE_PHASES:
            if phase not in by_phase:
                continue
            value = str(by_phase.get(phase) or "").strip()
            if value and value not in profiles:
                return None, "%s 段指定的 profile 不存在：%s" % (phase, value)
            if value != str(phase_map.get(phase) or "").strip():
                phase_map[phase] = value
                changed.append("profiles_by_phase.%s=%s" % (phase, value or "跟随默认"))
        merged["profiles_by_phase"] = phase_map
    if not changed:
        bundle = load_models_bundle()
        bundle["已写入"] = False
        bundle["改动"] = []
        return bundle, None
    written, bkp = save_like(MODELS_PATH, json.dumps(merged, ensure_ascii=False, indent=1))
    bundle = load_models_bundle()
    bundle["已写入"] = written
    bundle["备份"] = rel(bkp) if bkp else None
    bundle["改动"] = changed
    return bundle, None


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
