# -*- coding: utf-8 -*-
"""机械打分的取数层（只读）：全市场池、日K、财务、资金流、行业、ETF、质押、北向、公告、板块热度。

缓存与限速口径：
  · 全市场扫描 / 业绩报表 / 资产负债表 / 质押 / 龙虎榜 / 板块行情：按自然日落盘
    data/cache/mech/，同一天二次运行直接复用（refresh=1 才重取）；
  · 日K 逐股缓存 data/cache/mech/kline_<日期>/<代码>.json（320 根），串行 + 间隔；
  · 所有失败都返回 None / 空，由调用方记「缺失」并写降级清单，不抛给页面。
"""

import json
import os
import re
import time
from datetime import datetime, timedelta

from .paths import PAN_DIR, aiplan, atomic_write, num, pan
from .pick import PICK_CACHE_DIR, pick_filter
from .quotes import thscode_of, tx_symbol_of

MECH_CACHE = os.path.join(PICK_CACHE_DIR, "mech")
POOL_SIZE = 400                 # 候选池长度（按成交额降序）
MODEL_TOP = 150                 # 送模型的数量
KLINE_BARS = 320                # 需要 250 日均线，留足缓冲
KLINE_GAP = 0.12                # 同轮日K请求之间的串行间隔（秒）
DC_PAGE = 500                   # datacenter 单页上限
HS300_SECID = "1.000300"
FY_SYMBOL = "@"

STOCK_FIELDS = ("f2,f3,f5,f6,f8,f10,f12,f14,f15,f16,f17,f18,f20,f21,f23,f37,f38,f41,f46,"
                "f49,f55,f57,f62,f84,f100,f109,f110,f115,f160,f184,f24,f25")

# 行业 → 代表 ETF（文件要求「近20日行业ETF资金净流入率」；这张表人工维护）
INDUSTRY_ETF = {
    "电子": "512480", "半导体": "512480", "通信": "515880", "计算机": "512720",
    "传媒": "512980", "医药生物": "512010", "电力设备": "516160", "有色金属": "512400",
    "煤炭": "515220", "钢铁": "515210", "银行": "512800", "非银金融": "512880",
    "房地产": "512200", "食品饮料": "515170", "家用电器": "159996", "汽车": "516110",
    "机械设备": "159886", "国防军工": "512660", "农林牧渔": "159825", "公用事业": "159611",
    "交通运输": "159666", "建筑材料": "159745", "基础化工": "516020", "纺织服饰": "159729",
    "轻工制造": "159730", "商贸零售": "159766", "社会服务": "159766", "石油石化": "159930",
    "环保": "512580", "建筑装饰": "516970", "美容护理": "159766",
}


def _ctx():
    return pan.Ctx(PAN_DIR, datetime.now().date(), "post")


def _today():
    return datetime.now().strftime("%Y%m%d")


def _cache_file(name):
    return os.path.join(MECH_CACHE, "%s_%s.json" % (name, _today()))


def cache_read(name):
    path = _cache_file(name)
    if not os.path.exists(path):
        return None
    doc = aiplan.read_json(path)
    return doc if isinstance(doc, dict) else None


def cache_write(name, obj):
    try:
        os.makedirs(MECH_CACHE, exist_ok=True)
        atomic_write(_cache_file(name), json.dumps(obj, ensure_ascii=False), newline="\n")
    except OSError:
        pass
    return obj


def _secid(code):
    return pan._em_secid_of(thscode_of(code) or "")


def _diff(doc):
    data = (doc or {}).get("data") or {}
    rows = data.get("diff") or []
    if isinstance(rows, dict):
        rows = list(rows.values())
    return [r for r in rows if isinstance(r, dict)], data.get("total")


# ---------------------------------------------------------------------------
# 全市场扫描
# ---------------------------------------------------------------------------

def market_page(ctx, pn, pz=100, sort="f6"):
    """东财全 A clist 单页（按 sort 降序）。失败返回 (None, None)。"""
    d = pan._em_json(ctx, pan.EM_CLIST_PATH, {
        "pn": str(pn), "pz": str(pz), "po": "1", "np": "1", "fltt": "2", "invt": "2",
        "fid": sort, "fs": pan.EM_FS_ALL_A, "fields": STOCK_FIELDS})
    if not d:
        return None, None
    return _diff(d)


def is_yizhang(r):
    """一字跌停：最高 == 最低 且跌幅明显（-4.5% 以下，覆盖主板/创业板/ST 口径）。"""
    high, low, pct = num(r.get("f15")), num(r.get("f16")), num(r.get("f3"))
    return bool(high is not None and low is not None and high == low
                and pct is not None and pct <= -4.5)


def pool_row(r):
    """东财 clist 行 → 机械打分与事实包共用的结构（单位统一：亿 / 万元）。"""
    n = lambda k: num(r.get(k))          # noqa: E731
    price = n("f2")
    # 东财 clist 没有「自由流通市值」字段（f38/f39 是总股本/流通股本），
    # 所以用流通市值近似，并把口径写进字段里（页面与产物都会显示）。
    free_cap = n("f21")
    return {
        "代码": aiplan.code6(str(r.get("f12") or "")), "名称": str(r.get("f14") or ""),
        "现价": price, "涨跌幅_pct": n("f3"), "成交额_亿": None if n("f6") is None else n("f6") / 1e8,
        "成交量": n("f5"), "换手率_pct": n("f8"), "量比": n("f10"),
        "最高": n("f15"), "最低": n("f16"), "今开": n("f17"), "昨收": n("f18"),
        "总市值_亿": None if n("f20") is None else n("f20") / 1e8,
        "流通市值_亿": None if n("f21") is None else n("f21") / 1e8,
        "自由流通市值_亿": None if free_cap is None else free_cap / 1e8,
        "自由流通市值_口径": "用流通市值近似（东财 clist 无自由流通市值字段）",
        "PB": n("f23"), "PE_TTM": n("f115"), "净资产": n("f55"),
        "资产负债率_pct": n("f57"), "毛利率_pct": n("f49"), "ROE_pct": n("f37"),
        "营收同比_pct": n("f41"), "净利同比_pct": n("f46"),
        "主力净流入_万": None if n("f62") is None else n("f62") / 1e4,
        # 股本：东财只给 f38（总股本，股）；流通股本用「流通市值 / 现价」反推
        "主力净占比_pct": n("f184"), "总股本": n("f38"),
        "流通股本": (None if (free_cap is None or not price) else free_cap / price),
        "行业": str(r.get("f100") or ""),
        "5日_pct": n("f109"), "10日_pct": n("f160"), "20日_pct": n("f110"),
        "60日_pct": n("f24"), "年初至今_pct": n("f25"),
        "停牌": price is None or n("f3") is None, "一字跌停": is_yizhang(r),
    }


def market_scan(exclude, size=POOL_SIZE, refresh=False, log=None):
    """全市场 → 排除规则 → 按成交额取前 size 只。返回 (rows, 排除统计, 提示, 全市场总数)。"""
    want = max(int(size or POOL_SIZE), 60)
    hints, cached = [], None if refresh else cache_read("pool")
    raw, total = [], None
    if cached and cached.get("rows"):
        raw, total = cached["rows"], cached.get("total")
        hints.append("全市场扫描复用当日缓存 %s" % (cached.get("抓取时间") or ""))
    if not raw:
        ctx = _ctx()
        pages = max(2, min(14, (want + 199) // 100))       # 多取 2 页备着被排除的名额
        for pn in range(1, pages + 1):
            batch, tot = market_page(ctx, pn)
            if tot:
                total = tot
            if not batch:
                break
            raw.extend(batch)
            if len(raw) >= want + 200:
                break
            time.sleep(KLINE_GAP)
        if not raw:
            return [], {}, ["全市场扫描失败（东财 clist 取不到）：本轮没有候选池"], total
        cache_write("pool", {"抓取时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                             "total": total, "rows": raw})
        if log:
            log("[OK] 全市场扫描 %d 只（成交额降序前 %d 页）" % (total or len(raw), pages))
    kept, stats = pick_filter([dict(r) for r in raw], exclude)
    kept = kept[:want]
    if log:
        log("[OK] 候选池 %d 只（排除：%s）"
            % (len(kept), "，".join("%s %d" % (k, v) for k, v in stats.items()) or "无"))
    return kept, stats, hints, total


# ---------------------------------------------------------------------------
# 日K（东财 push2his；逐股缓存）
# ---------------------------------------------------------------------------

KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EM_KLINE_HOSTS = ("https://push2his.eastmoney.com", "https://62.push2his.eastmoney.com",
                  "https://63.push2his.eastmoney.com")
FFLOW_URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
KLINE_FIELDS1 = "f1,f2,f3,f4,f5,f6"
KLINE_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"


def _kline_http(secid, bars=KLINE_BARS):
    """东财日K（3 个 host 轮换）→ [{date,open,close,high,low,vol,额,换手_pct}]。"""
    if not secid:
        return []
    params = {"secid": secid, "fields1": KLINE_FIELDS1, "fields2": KLINE_FIELDS2,
              "klt": "101", "fqt": "1", "lmt": str(bars), "end": "20500101"}
    klines = []
    for host in EM_KLINE_HOSTS:
        raw = pan.http_get_bytes(host + "/api/qt/stock/kline/get", params=params,
                                 headers=pan.EM_HEADERS, timeout=20, tries=1)
        try:
            doc = json.loads(raw.decode("utf-8", "replace")) if raw else None
        except ValueError:
            doc = None
        klines = ((doc or {}).get("data") or {}).get("klines") or []
        if klines:
            break
    out = []
    for line in klines:
        parts = str(line).split(",")
        if len(parts) < 6:
            continue
        out.append({"date": parts[0], "open": num(parts[1]), "close": num(parts[2]),
                    "high": num(parts[3]), "low": num(parts[4]), "vol": num(parts[5]),
                    "额": num(parts[6]) if len(parts) > 6 else None,
                    "换手_pct": num(parts[10]) if len(parts) > 10 else None})
    return out


def _kline_tx(code, bars=KLINE_BARS):
    """腾讯前复权日K（东财不可用时的兜底）。只有 6 列：日期/开/收/高/低/量。"""
    sym = tx_symbol_of(code)
    if not sym:
        return []
    return _kline_tx_symbol(sym, bars)


def kline(code, refresh=False):
    """单只标的日K（当日缓存，二次运行零请求）。"""
    c6 = aiplan.code6(code)
    if not c6:
        return []
    folder = os.path.join(MECH_CACHE, "kline_%s" % _today())
    path = os.path.join(folder, "%s.json" % c6)
    if os.path.exists(path) and not refresh:
        doc = aiplan.read_json(path)
        if isinstance(doc, dict) and doc.get("bars"):
            return doc["bars"]
    bars, source = _kline_http(_secid(c6)), "东财"
    if not bars:
        bars, source = _kline_tx(c6), "腾讯（兜底）"
    if not bars:
        source = "取不到"
    try:
        os.makedirs(folder, exist_ok=True)
        atomic_write(path, json.dumps({"抓取时间": datetime.now().strftime("%H:%M:%S"),
                                       "来源": source, "bars": bars},
                                      ensure_ascii=False), newline="\n")
    except OSError:
        pass
    return bars


def klines(codes, refresh=False, log=None, gap=KLINE_GAP):
    """批量日K（串行 + 间隔）。返回 {代码: bars}。"""
    out, total = {}, len(list(codes or []))
    for i, c6 in enumerate(codes or [], start=1):
        out[c6] = kline(c6, refresh=refresh)
        if log and (i % 25 == 0 or i == total):
            log("[..] 日K %d/%d" % (i, total))
        if gap:
            time.sleep(gap)
    return out


def bench_kline(sort="f3", refresh=False):
    """沪深300 日K（算 20 日超额收益基准；当日缓存）。"""
    cached = None if refresh else cache_read("bench")
    if cached and cached.get("bars"):
        return cached["bars"]
    bars = index_kline(HS300_SECID, KLINE_BARS)
    cache_write("bench", {"bars": bars})
    return bars


INDEX_TX_SYMBOL = {"1.000001": "sh000001", "0.399106": "sz399106", "1.000300": "sh000300",
                   "1.000300.SH": "sh000300"}


def index_kline(secid, bars=KLINE_BARS):
    """指数日K：东财优先，腾讯兜底（指数代码不能用 tx_symbol_of 直接映射）。"""
    rows = _kline_http(secid, bars)
    if rows:
        return rows
    sym = INDEX_TX_SYMBOL.get(secid)
    if not sym:
        return []
    return _kline_tx_symbol(sym, bars)


def _kline_tx_symbol(sym, bars=KLINE_BARS):
    """腾讯前复权日K（按已拼好的 sh/sz 代码取，指数也走这里）。"""
    raw = pan.http_get_bytes("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                             params={"param": "%s,day,,,%d,qfq" % (sym, bars)},
                             headers={"Referer": "https://gu.qq.com/"}, timeout=20, tries=2)
    try:
        doc = json.loads(raw.decode("utf-8", "replace")) if raw else None
    except ValueError:
        return []
    node = ((doc or {}).get("data") or {}).get(sym) or {}
    rows = node.get("qfqday") or node.get("day") or []
    out = []
    for row in rows[-bars:]:
        if len(row) < 6:
            continue
        out.append({"date": row[0], "open": num(row[1]), "close": num(row[2]),
                    "high": num(row[3]), "low": num(row[4]), "vol": num(row[5]),
                    "额": None, "换手_pct": None})
    return out


# ---------------------------------------------------------------------------
# 财报（业绩报表 + 资产负债表，按报告期批量取全市场）
# ---------------------------------------------------------------------------

PERIOD_ORDER = ((3, 31), (6, 30), (9, 30), (12, 31))


def _period_str(y, m, d):
    return "%04d-%02d-%02d" % (y, m, d)


def prev_periods(latest):
    """'2026-06-30' → ('2026-03-31', '2025-06-30')（上一期、去年同期）。"""
    text = str(latest or "")[:10]
    if len(text) < 10:
        return None, None
    y, m, d = (int(x) for x in text.split("-"))
    idx = [i for i, (mm, _dd) in enumerate(PERIOD_ORDER) if mm == m]
    i = idx[0] if idx else 0
    prev = (y - 1, 12, 31) if i == 0 else (y,) + PERIOD_ORDER[i - 1]
    return _period_str(*prev), _period_str(y - 1, m, d)


def _dc_pages(report, filter_, page_size=DC_PAGE, sort=None, cap=20, gap=KLINE_GAP):
    """按报表 + 过滤条件翻页取全量（最多 cap 页）。失败返回已取到的部分。"""
    ctx, out = _ctx(), []
    for page in range(1, max(1, cap) + 1):
        rows, _err = pan.em_dc_rows(ctx, report, page_size=page_size, page=page,
                                    filter_=filter_, sort=sort)
        if not rows:
            break
        out.extend(rows)
        if len(rows) < page_size:
            break
        time.sleep(gap)
    return out


def latest_report_period(refresh=False):
    """最新报告期（东财业绩报表），并给出上一期与去年同期。"""
    cached = None if refresh else cache_read("period")
    if cached and cached.get("latest"):
        return cached["latest"], cached.get("prev"), cached.get("same")
    rows, _err = pan.em_dc_rows(_ctx(), "RPT_LICO_FN_CPD", page_size=1, sort="REPORTDATE")
    if not rows:
        return None, None, None
    latest = str(rows[0].get("REPORTDATE") or "")[:10]
    prev, same = prev_periods(latest)
    cache_write("period", {"latest": latest, "prev": prev, "same": same})
    return latest, prev, same


def _period_table(report, period):
    """按报告期取整表。不同报表的日期字段名不一样（业绩报表 REPORTDATE、
    资产负债表 REPORT_DATE），所以两个都试一遍。"""
    rows = []
    for field in ("REPORTDATE", "REPORT_DATE"):
        rows = _dc_pages(report, "(%s='%s')" % (field, period), sort="SECURITY_CODE", cap=16)
        if rows:
            break
    table = {}
    for r in rows:
        code = aiplan.code6(str(r.get("SECURITY_CODE") or ""))
        if code:
            table[code] = r
    return table


def finance_by_period(refresh=False, log=None):
    """业绩报表 {'最新期','上期','去年同期'} → {报告期: {代码: 行}}。"""
    latest, prev, same = latest_report_period(refresh=refresh)
    if not latest:
        return {}, None, None, None
    name = "lico_%s" % latest
    doc = (None if refresh else cache_read(name)) or {"数据": {}}
    for period in (latest, prev, same):
        if not period or doc["数据"].get(period):
            continue
        doc["数据"][period] = _period_table("RPT_LICO_FN_CPD", period)
        if log:
            log("[OK] 业绩报表 %s：%d 只" % (period, len(doc["数据"][period])))
    cache_write(name, doc)
    return doc["数据"], latest, prev, same


def balance_by_period(refresh=False, log=None):
    """资产负债表（应收 / 净资产）→ {报告期: {代码: 行}}。"""
    latest, _prev, same = latest_report_period(refresh=refresh)
    if not latest:
        return {}
    name = "balance_%s" % latest
    doc = (None if refresh else cache_read(name)) or {"数据": {}}
    for period in (latest, same):
        if not period or doc["数据"].get(period):
            continue
        doc["数据"][period] = _period_table("RPT_DMSK_FN_BALANCE", period)
        if log:
            log("[OK] 资产负债表 %s：%d 只" % (period, len(doc["数据"][period])))
    cache_write(name, doc)
    return doc["数据"]


def _median(vals):
    xs = sorted(v for v in vals if v is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def code_industry_map(table):
    """{代码: 行业名}（用资产负债表里的 INDUSTRY_NAME）。"""
    out = {}
    for code, row in (table or {}).items():
        name = str(row.get("INDUSTRY_NAME") or "").strip()
        if code and name:
            out[code] = name
    return out


def scanned_industry_map():
    """当日全市场扫描结果 → {代码: 东财行业（f100）}，用于行业景气聚合的样本。"""
    cached = cache_read("pool") or {}
    out = {}
    for row in cached.get("rows") or []:
        code = aiplan.code6(str(row.get("f12") or ""))
        name = str(row.get("f100") or "").strip()
        if code and name:
            out[code] = name
    return out


def industry_agg(code_industry, lico, period, min_n=6):
    """行业净利/营收同比：行业内个股中位数（自建聚合口径，页面标注）。"""
    buckets = {}
    for code, row in (lico or {}).items():
        ind = (code_industry or {}).get(code)
        if not ind:
            continue
        for key, field in (("净利同比", "SJLTZ"), ("营收同比", "YSTZ")):
            v = num(row.get(field))
            if v is None:
                continue
            buckets.setdefault(ind, {}).setdefault(key, []).append(v)
    out = {}
    for ind, cols in buckets.items():
        item = {}
        for key in ("净利同比", "营收同比"):
            vals = cols.get(key) or []
            if len(vals) >= min_n:
                item[key + "_pct"] = round(_median(vals), 2)
        if item:
            out[ind] = item
    return out


# ---------------------------------------------------------------------------
# 资金与筹码
# ---------------------------------------------------------------------------

def fund_flow(code, days=10, refresh=False):
    """近 N 个交易日主力资金净流入（东财 fflow 日线）。返回 {净额_万, 天数} 或 None。"""
    secid = _secid(code)
    if not secid:
        return None
    d = pan._http_get_json(FFLOW_URL, {"lmt": str(days + 1), "klt": "101", "secid": secid,
                                       "fields1": "f1,f2,f3,f7",
                                       "fields2": "f51,f52,f53,f54,f55,f56,f57"})
    klines = ((d or {}).get("data") or {}).get("klines") or []
    vals = []
    for line in klines[-days:]:
        parts = str(line).split(",")
        if len(parts) < 2:
            continue
        v = num(parts[1])
        if v is not None:
            vals.append(v)
    if not vals:
        return None
    return {"净额_万": round(sum(vals) / 1e4, 2), "天数": len(vals)}


def etf_size(code):
    """ETF 规模（流通市值，亿元）——算行业 ETF 资金净流入率的分母。"""
    secid = _secid(code)
    if not secid:
        return None
    rows = pan.em_ulist(_ctx(), [secid], fields="f12,f14,f21")
    for row in rows or []:
        if str(row.get("f12")) == aiplan.code6(code):
            v = num(row.get("f21"))
            return None if v is None else v / 1e8
    return None


def etf_flow_ratio(industry, refresh=False, days=20):
    """近 20 日行业 ETF 资金净流入率（%）：净流入额 / ETF 规模。"""
    code = None
    for key in sorted(INDUSTRY_ETF, key=len, reverse=True):
        if key and key in str(industry or ""):
            code = INDUSTRY_ETF[key]
            break
    if not code:
        return None
    flow = fund_flow(code, days=days, refresh=refresh)
    size = etf_size(code)
    if not flow or not size:
        return None
    return round(flow["净额_万"] / 1e4 / size * 100.0, 2)


def pledge_map(refresh=False, log=None):
    """最新一期股权质押比例（全市场）→ {代码: 比例%}。"""
    cached = None if refresh else cache_read("pledge")
    if cached and cached.get("数据"):
        return cached["数据"]
    rows, _err = pan.em_dc_rows(_ctx(), "RPT_CSDC_LIST", page_size=1, sort="TRADE_DATE")
    if not rows:
        return {}
    date = str(rows[0].get("TRADE_DATE") or "")[:10]
    out = {}
    for r in _dc_pages("RPT_CSDC_LIST", "(TRADE_DATE='%s')" % date, cap=16):
        code = aiplan.code6(str(r.get("SECURITY_CODE") or ""))
        v = num(r.get("PLEDGE_RATIO"))
        if code and v is not None:
            out[code] = v
    cache_write("pledge", {"日期": date, "数据": out})
    if log:
        log("[OK] 股权质押 %s：%d 只" % (date, len(out)))
    return out


def north_change(codes, refresh=False, log=None):
    """北向持股占流通比（季度快照）最近两期变动（百分点）→ {代码: 变动}。"""
    codes = [c for c in (codes or []) if c]
    if not codes:
        return {}
    cached = None if refresh else cache_read("north")
    if cached and cached.get("数据"):
        return {k: v for k, v in cached["数据"].items() if k in set(codes)}
    out = {}
    for i in range(0, len(codes), 50):
        chunk = codes[i:i + 50]
        filt = "(SECURITY_CODE in (%s))" % ",".join('"%s"' % c for c in chunk)
        per = {}
        for r in _dc_pages("RPT_MUTUAL_HOLDSTOCKNORTH_STA", filt, cap=4, sort="TRADE_DATE"):
            code = aiplan.code6(str(r.get("SECURITY_CODE") or ""))
            date = str(r.get("TRADE_DATE") or "")[:10]
            v = num(r.get("HOLD_SHARES_RATIO"))
            if code and date and v is not None:
                per.setdefault(code, {})[date] = v
        for code, dates in per.items():
            keys = sorted(dates)
            if len(keys) >= 2:
                out[code] = round(dates[keys[-1]] - dates[keys[-2]], 4)
    cache_write("north", {"数据": out})
    if log:
        log("[OK] 北向持股变动：%d 只（季度快照口径）" % len(out))
    return out


def holder_focus(code):
    """股东户数集中度（%）：东财 F10，代理 90% 筹码集中度（页面标注口径）。"""
    rows, _err = pan.em_dc_rows(_ctx(), "RPT_F10_EH_HOLDERNUM", page_size=1,
                                filter_='(SECURITY_CODE="%s")' % code, sort="END_DATE")
    if not rows:
        return None
    return num(rows[0].get("HOLD_FOCUS"))


def top10_free_ratio(code):
    """前十大流通股东持股占比（%）。"""
    rows, _err = pan.em_dc_rows(_ctx(), "RPT_F10_EH_FREEHOLDERS", page_size=30,
                                filter_='(SECURITY_CODE="%s")' % code, sort="END_DATE")
    if not rows:
        return None
    latest = str(rows[0].get("END_DATE") or "")[:10]
    total = 0.0
    seen = 0
    for r in rows:
        if str(r.get("END_DATE") or "")[:10] != latest:
            continue
        v = num(r.get("FREE_HOLDNUM_RATIO"))
        if v is None:
            continue
        total += v
        seen += 1
        if seen >= 10:
            break
    return round(total, 2) if seen else None


def lhb_state(codes, days=10, refresh=False, log=None):
    """近 N 日龙虎榜机构净买入 → {代码: 1=净买入 / 0=无榜 / -1=净卖出}。"""
    want = set(codes or [])
    if not want:
        return {}
    cached = None if refresh else cache_read("lhb")
    if cached and cached.get("数据"):
        return {k: v for k, v in cached["数据"].items() if k in want}
    since = (datetime.now() - timedelta(days=max(days * 2, 12))).strftime("%Y-%m-%d")
    agg = {}
    for r in _dc_pages("RPT_BILLBOARD_DAILYDETAILSBUY", "(TRADE_DATE>='%s')" % since,
                       page_size=500, cap=30, sort="TRADE_DATE"):
        code = aiplan.code6(str(r.get("SECURITY_CODE") or ""))
        if not code or code not in want:
            continue
        dept = str(r.get("OPERATEDEPT_NAME") or "")
        net = num(r.get("NET"))
        if net is None or "机构" not in dept:
            continue
        agg[code] = (agg.get(code) or 0.0) + net
    out = {code: 0 for code in want}
    for code, net in agg.items():
        out[code] = 1 if net > 0 else -1
    cache_write("lhb", {"数据": out, "起始": since})
    if log:
        log("[OK] 龙虎榜机构净买入：命中 %d 只（近 %d 天）" % (len(agg), days))
    return out


# ---------------------------------------------------------------------------
# 公告类（前 150 只才跑）：一票否决 + 模块6
# ---------------------------------------------------------------------------

ANN_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
REGULATE_WORDS = ("立案", "公开谴责", "处罚", "警示函", "监管函")
CUT_WORDS = ("减持",)


def announce_flags(code, days=30):
    """近 30 天公告 → {立案或谴责: bool, 有减持计划: bool, 减持比例_pct: num|None}。"""
    d = pan._http_get_json(ANN_URL, {"sr": "-1", "page_size": "50", "page_index": "1",
                                     "ann_type": "A", "client_source": "web",
                                     "stock_list": code, "f_node": "0", "s_node": "0"})
    items = ((d or {}).get("data") or {}).get("list") or []
    flags = {"立案或谴责": False, "有减持计划": False, "减持比例_pct": None}
    limit = datetime.now() - timedelta(days=days)
    for it in items:
        title = str(it.get("title") or "")
        raw = str(it.get("notice_date") or it.get("display_time") or "")[:10]
        try:
            if datetime.strptime(raw, "%Y-%m-%d") < limit:
                continue
        except ValueError:
            pass
        if any(w in title for w in REGULATE_WORDS):
            flags["立案或谴责"] = True
        if any(w in title for w in CUT_WORDS):
            flags["有减持计划"] = True
            m = re.search(r"(\d+(?:\.\d+)?)\s*%", title)
            if m:
                v = num(m.group(1))
                if v is not None:
                    flags["减持比例_pct"] = max(flags["减持比例_pct"] or 0.0, v)
    return flags


def profit_warning(codes, refresh=False):
    """业绩预告里「预减 / 首亏 / 增亏」且幅度 > 50% → {代码: True}（模块6 用）。"""
    want = [c for c in (codes or []) if c]
    if not want:
        return {}
    cached = None if refresh else cache_read("predict")
    if cached and cached.get("数据"):
        return {k: v for k, v in cached["数据"].items() if k in set(want)}
    out = {}
    for i in range(0, len(want), 50):
        chunk = want[i:i + 50]
        filt = "(SECURITY_CODE in (%s))" % ",".join('"%s"' % c for c in chunk)
        for r in _dc_pages("RPT_PUBLIC_OP_NEWPREDICT", filt, page_size=100, cap=4,
                           sort="NOTICE_DATE"):
            code = aiplan.code6(str(r.get("SECURITY_CODE") or ""))
            text = "%s%s" % (r.get("PREDICT_TYPE") or "", r.get("PREDICT_CONTENT") or "")
            low, high = num(r.get("ADD_AMP_LOWER")), num(r.get("ADD_AMP_UPPER"))
            amp = low if low is not None else high
            if not code or amp is None:
                continue
            if any(k in text for k in ("预减", "首亏", "增亏", "略减")) and amp <= -50:
                out[code] = True
    cache_write("predict", {"数据": out})
    return out


def limitup_counts(days=5, refresh=False):
    """最近 N 个交易日的涨停家数（东财涨停池按日期查；交易日取自沪深300 日K）。"""
    cached = None if refresh else cache_read("limitup")
    if cached and cached.get("家数"):
        return cached["家数"]
    dates = [str(b.get("date") or "")[:10] for b in bench_kline(refresh=refresh)][-days:]
    counts = []
    for day in dates:
        d = pan._http_get_json("https://push2ex.eastmoney.com/getTopicZTPool", {
            "ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt", "Pageindex": "0",
            "pagesize": "3", "sort": "fbt:asc", "date": day.replace("-", "")})
        total = ((d or {}).get("data") or {}).get("tc")
        if total is not None:
            counts.append(int(total))
        time.sleep(KLINE_GAP)
    cache_write("limitup", {"家数": counts, "日期": dates})
    return counts


def macro_inputs(refresh=False, log=None):
    """模块1 的可得输入：10 年国债、近5日日均全市场成交额、近5日日均涨停家数。"""
    out = {}
    d = pan.em_quote(_ctx(), "171.CN10Y", fields="f43,f57,f58,f169,f170")
    y10 = num((d or {}).get("f43"))
    if y10 is None:
        y10 = num((d or {}).get("f170"))
    out["bond10y"] = None if y10 is None else round(y10 / 100.0, 3) if y10 > 20 else y10
    # 近 5 日日均全市场成交额 = 上证综指 + 深证综指（两市全覆盖）的日成交额之和
    # 成交额只有东财日K 给（腾讯兜底没有这一列）→ 拿不到就记缺失，不编造
    legs = []
    for secid in ("1.000001", "0.399106"):
        bars = index_kline(secid, 8)
        legs.append([b.get("额") for b in bars[-5:]])
        time.sleep(KLINE_GAP)
    per_day = []
    if len(legs) == 2 and len(legs[0]) == 5 and len(legs[1]) == 5:
        pairs = list(zip(legs[0], legs[1]))
        if all(a is not None and b is not None for a, b in pairs):
            per_day = [a + b for a, b in pairs]
    out["amount5"] = round(sum(per_day) / len(per_day) / 1e8, 2) if per_day else None
    if out["amount5"] is None:
        hints = "东财日K 取不到成交额（腾讯兜底无该列）"
    else:
        hints = None
    counts = limitup_counts(days=5, refresh=refresh)
    out["limitup5"] = round(sum(counts) / len(counts), 1) if counts else None
    if log:
        log("[..] 宏观输入：10Y 国债 %s%% ｜ 5日日均成交额 %s 亿 ｜ 5日日均涨停 %s 家"
            % (out["bond10y"], out["amount5"], out["limitup5"]))
        if hints:
            log("[WARN] 近5日日均成交额缺失：%s（该项 3 分记缺失）" % hints)
    return out


def board_heat(refresh=False, top=10):
    """行业 + 概念热度榜（当日涨跌幅前 top / 后 top），只给模型当市场背景。"""
    cached = None if refresh else cache_read("heat")
    if cached and cached.get("行业"):
        return cached
    out = {}
    for label, fs in (("行业", pan.EM_FS_INDUSTRY), ("概念", pan.EM_FS_CONCEPT)):
        up = _board_page(fs, po="1")
        down = _board_page(fs, po="0")
        out[label] = {"前%d" % top: _trim(up, top), "后%d" % top: _trim(down, top)}
        time.sleep(KLINE_GAP)
    out["口径"] = "按当日涨跌幅排序（东财板块行情），只作市场背景，不参与打分"
    cache_write("heat", out)
    return out


def _board_page(fs, po="1"):
    d = pan._em_json(_ctx(), pan.EM_CLIST_PATH, {
        "pn": "1", "pz": "100", "po": po, "np": "1", "fltt": "2", "invt": "2",
        "fid": "f3", "fs": fs, "fields": "f12,f14,f3,f62,f184"})
    rows, _total = _diff(d)
    return rows


def _trim(rows, n):
    return [{"名称": r.get("f14"), "涨跌幅_pct": num(r.get("f3")),
             "主力净流入_亿": None if num(r.get("f62")) is None else round(num(r.get("f62")) / 1e8, 2)}
            for r in (rows or [])[:n]]
