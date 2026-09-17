# -*- coding: utf-8 -*-
"""大盘评分的取数层（只读 + 本地历史）：给《大盘评分逻辑.txt》的 6 模块 25 个分项供原始值。

约定（每个分项一个函数，统一返回证据 dict）：
  {值, 来源, 数据日期, 口径, 错误}；取不到就 值=None + 错误文本，绝不编造。

缓存：
  · 当天数据 → data/cache/mkt/<YYYYMMDD>/（同一天二次运行零请求）
  · 长期历史 → data/cache/mkt/hist/（中证 PE 十年序列、逐日积累的广度/成交额/中间价）
联网只有三类：中证指数官网（估值）、东财（指数日K/月K/资金流/宏观报表/全市场汇总）、
中国货币网（人民币中间价）。全部走 pan 的 HTTP 会话，失败返回 None。
"""

import json
import os
import time
from datetime import datetime, timedelta

from . import mechdata
from .paths import DATA_DIR, aiplan, atomic_write, num, pan
from .sources import newest_file, pan_files

CACHE_ROOT = os.path.join(DATA_DIR, "cache", "mkt")
HIST_DIR = os.path.join(CACHE_ROOT, "hist")
DAILY_HIST = os.path.join(HIST_DIR, "daily.json")
CSINDEX_CACHE_DAYS = 30             # 估值序列 30 天刷新一次（历史值不会变）
KLINE_GAP = 0.08

MARKET_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
CLIST_FIELDS = "f12,f14,f21,f6"
CSINDEX_URL = "https://www.csindex.com.cn/csindex-home/perf/index-perf"
CCPR_URL = "https://www.chinamoney.com.cn/r/cms/www/chinamoney/data/fx/ccpr.json"
SINA_FX_URL = "https://hq.sinajs.cn/list=fx_susdcny"
DC_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

INDEX_BARS = 300                    # 沪深300 要 250 日均线
INDEX_MONTH_BARS = 130
US_SECIDS = {"美元指数": "100.UDI", "标普500": "100.SPX"}


def _today():
    return datetime.now().strftime("%Y%m%d")


def _day_dir(day=None):
    return os.path.join(CACHE_ROOT, day or _today())


def _read(path, max_age=None):
    """读 JSON 缓存；超过 max_age 秒视为失效（None 表示永不过期）。"""
    if not os.path.exists(path):
        return None
    doc = aiplan.read_json(path)
    if not isinstance(doc, dict):
        return None
    if max_age is not None and time.time() - os.path.getmtime(path) > max_age:
        return None
    return doc


def _write(path, obj):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        atomic_write(path, json.dumps(obj, ensure_ascii=False), newline="\n")
    except OSError:
        pass
    return obj


def http_json(url, params=None, headers=None, timeout=15, tries=2):
    """GET → JSON（失败返回 None，不抛错）。"""
    raw = pan.http_get_bytes(url, params=params, headers=headers or pan.EM_HEADERS,
                             timeout=timeout, tries=tries)
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None


def ev(value, source, date=None, note=None, err=None):
    """统一的证据结构。"""
    return {"值": None if value is None else num(value), "来源": source,
            "数据日期": date, "口径": note, "错误": err}


# ---------------------------------------------------------------------------
# 1 估值：中证指数官网 PE 十年序列（按年分段抓，避免行数截断）
# ---------------------------------------------------------------------------

def csindex_pe_series(index_code, years=10, refresh=False, log=None):
    """中证指数官网的日度 PE 序列（字段 peg 即 PE-TTM）。返回 [{日期, PE}]。"""
    path = os.path.join(HIST_DIR, "csindex_%s.json" % index_code)
    cached = None if refresh else _read(path, max_age=CSINDEX_CACHE_DAYS * 86400)
    if cached and cached.get("序列"):
        return cached["序列"]
    head = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.csindex.com.cn/"}
    start = datetime.now() - timedelta(days=int(365.25 * years))
    series, cursor = [], start
    while cursor < datetime.now():
        stop = min(cursor + timedelta(days=365), datetime.now())
        doc = http_json(CSINDEX_URL, {"indexCode": index_code,
                                      "startDate": cursor.strftime("%Y-%m-%d"),
                                      "endDate": stop.strftime("%Y-%m-%d")},
                        headers=head, timeout=25, tries=2)
        for row in ((doc or {}).get("data") or []):
            pe = num(row.get("peg"))
            day = str(row.get("tradeDate") or "")[:10]
            if pe is not None and len(day) == 8:
                series.append([day, pe])
        cursor = stop + timedelta(days=1)
        time.sleep(0.2)
    dedup = {day: pe for day, pe in series}          # 分段抓可能重复，按日期去重
    series = [[day, dedup[day]] for day in sorted(dedup)]
    if series:
        _write(path, {"抓取时间": aiplan.iso_now(), "指数": index_code, "序列": series})
        if log:
            log("[OK] 中证 %s PE 序列 %d 条（%s ~ %s）"
                % (index_code, len(series), series[0][0], series[-1][0]))
    elif log:
        log("[WARN] 中证 %s PE 序列取不到（官网接口失败或限流）" % index_code)
    return series


def pe_percentile(index_code, years=10, refresh=False, log=None):
    """当前 PE 在近 10 年序列里的历史分位（%）。"""
    series = csindex_pe_series(index_code, years, refresh=refresh, log=log)
    if not series:
        return ev(None, "中证指数官网 index-perf", None, "PE-TTM 近 10 年分位",
                  "官网接口没返回数据（可能限流），下次重算会再试")
    values = [v for _d, v in series]
    cur = values[-1]
    below = sum(1 for v in values if v <= cur)
    pct = round(below / len(values) * 100.0, 1)
    out = ev(cur, "中证指数官网 index-perf（peg 字段）", series[-1][0],
             "%d 条日度 PE-TTM（%s ~ %s）中的分位" % (len(series), series[0][0], series[-1][0]))
    out.update({"分位": pct, "序列条数": len(series), "起": series[0][0], "止": series[-1][0]})
    return out


# ---------------------------------------------------------------------------
# 2/4/6 指数行情：日K / 月K / 资金流历史（东财，当天缓存）
# ---------------------------------------------------------------------------

def index_bars(secid, bars=INDEX_BARS, klt="101", refresh=False, log=None, need_amount=False):
    """指数日K/月K（东财；指数走 mechdata 的腾讯兜底）。

    need_amount=True 表示要成交额这一列：缓存里没有（腾讯兜底没有该列）就重取一次。
    """
    name = "k%s_%s_%s" % (klt, secid.replace(".", ""), _today())
    path = os.path.join(_day_dir(), "%s.json" % name)
    cached = None if refresh else _read(path)
    if cached and cached.get("bars") and not (need_amount and not cached.get("有成交额")):
        return cached
    rows = mechdata._kline_http(secid, bars) if klt == "101" else None
    if not rows:
        params = {"secid": secid, "fields1": mechdata.KLINE_FIELDS1,
                  "fields2": mechdata.KLINE_FIELDS2, "klt": klt, "fqt": "1",
                  "lmt": str(bars), "end": "20500101"}
        doc = http_json("https://push2his.eastmoney.com/api/qt/stock/kline/get", params,
                        timeout=20, tries=2)
        rows = []
        for line in (((doc or {}).get("data") or {}).get("klines") or []):
            p = str(line).split(",")
            if len(p) >= 6:
                rows.append({"date": p[0], "open": num(p[1]), "close": num(p[2]),
                             "high": num(p[3]), "low": num(p[4]), "vol": num(p[5]),
                             "额": num(p[6]) if len(p) > 6 else None})
    if not rows and klt == "101":
        rows = mechdata._kline_tx_symbol(mechdata.INDEX_TX_SYMBOL.get(secid, ""), bars) \
            if mechdata.INDEX_TX_SYMBOL.get(secid) else []
    out = {"bars": rows or [],
           "有成交额": bool(any(b.get("额") for b in (rows or []))),
           "来源": "东财前复权%sK（腾讯兜底）" % ("日" if klt == "101" else "月"),
           "数据日期": (rows[-1].get("date") if rows else None)}
    if log and klt == "101":
        log("[..] %s 日K %d 根（最新 %s）" % (secid, len(rows), out["数据日期"]))
    return _write(path, out) if rows else out


def index_fflow(secid, days=10, refresh=False, log=None):
    """指数资金流历史：近 N 日主力净流入合计（亿元）。"""
    path = os.path.join(_day_dir(), "fflow_%s.json" % secid.replace(".", ""))
    cached = None if refresh else _read(path)
    if cached and cached.get("明细"):
        return cached
    doc = http_json("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
                    {"secid": secid, "lmt": str(max(days, 10)), "klt": "101",
                     "fields1": "f1,f2,f3,f7",
                     "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"},
                    timeout=20, tries=2)
    rows = []
    for line in (((doc or {}).get("data") or {}).get("klines") or []):
        p = str(line).split(",")
        if len(p) >= 2 and num(p[1]) is not None:
            rows.append([p[0], round(num(p[1]) / 1e8, 2)])       # 主力净流入 → 亿元
    out = {"明细": rows[-days:], "来源": "东财指数资金流历史（fflow/daykline）",
           "数据日期": (rows[-1][0] if rows else None),
           "口径": "指数口径（上证/深证成指的资金流），不是逐股汇总"}
    if log:
        log("[..] %s 资金流历史 %d 天（最新 %s）" % (secid, len(out["明细"]), out["数据日期"]))
    return _write(path, out) if rows else out


# ---------------------------------------------------------------------------
# 2 宏观报表：M2 同比 / 3 个月拆借利率
# ---------------------------------------------------------------------------

def macro_report(report_name, filter_=None, sort="REPORT_DATE", page=1, size=1):
    params = {"reportName": report_name, "columns": "ALL", "pageNumber": str(page),
              "pageSize": str(size), "sortColumns": sort, "sortTypes": "-1",
              "source": "WEB", "client": "WEB"}
    if filter_:
        params["filter"] = filter_
    doc = http_json(DC_URL, params, timeout=15, tries=2)
    return ((doc or {}).get("result") or {}).get("data") or []


def m2_yoy(refresh=False, log=None):
    """M2 同比（东财宏观经济：货币供应量，月度口径）。"""
    path = os.path.join(_day_dir(), "macro_m2.json")
    cached = None if refresh else _read(path)
    if cached and cached.get("值") is not None:
        return cached
    rows = macro_report("RPT_ECONOMY_CURRENCY_SUPPLY", size=2)
    row = rows[0] if rows else {}
    out = ev(row.get("BASIC_CURRENCY_SAME"), "东财 RPT_ECONOMY_CURRENCY_SUPPLY",
             str(row.get("REPORT_DATE") or "")[:10] or None, "M2 同比增速（月度）")
    if out["值"] is None:
        out["错误"] = "东财货币供应量报表取不到"
    if log:
        log("[..] M2 同比 %s%%（%s）" % (out["值"], out["数据日期"]))
    return _write(path, out)


def shibor_3m(refresh=False, log=None):
    """3 个月上海银行间同业拆借利率（东财报表口径）。"""
    path = os.path.join(_day_dir(), "macro_shibor.json")
    cached = None if refresh else _read(path)
    if cached and cached.get("值") is not None:
        return cached
    rows = macro_report("RPT_IMP_INTRESTRATEN",
                        '(MARKET_CODE="001")(REPORT_PERIOD="3月(3M)")', "REPORT_DATE", 1, 1)
    row = rows[0] if rows else {}
    out = ev(row.get("IR_RATE"), "东财 RPT_IMP_INTRESTRATEN（上海银行同业拆借市场 3M）",
             str(row.get("REPORT_DATE") or "")[:10] or None,
             "3 个月期 Shibor（文件写的是同业拆借加权利率，取该口径）")
    if out["值"] is None:
        out["错误"] = "东财同业拆借利率报表取不到"
    return _write(path, out)


def bond10y(pandoc, refresh=False, log=None):
    """10 年期国债到期收益率：优先 pan 快照，其次东财报价。"""
    d = ((pandoc or {}).get("data") or {})
    node = (((d.get("overseas_evening") or {}).get("债券收益率") or {})
            .get("中债10年期收益率") or {})
    val = num(node.get("最新"))
    if val is not None:
        return ev(val, "pan 快照 · 中债 10 年期收益率", (pandoc or {}).get("trade_date"),
                  "中债 10 年期到期收益率")
    quote = pan.em_quote(mechdata._ctx(), "171.CN10Y", fields="f43,f170")
    val = num((quote or {}).get("f43")) or num((quote or {}).get("f170"))
    if val is not None and val > 20:
        val = round(val / 100.0, 3)
    out = ev(val, "东财 171.CN10Y", _today(), "中债 10 年期到期收益率")
    if val is None:
        out["错误"] = "pan 快照与东财都没有 10 年期国债收益率"
    return out


# ---------------------------------------------------------------------------
# 3 场内资金：全市场汇总（流通市值/成交额）、两融、主力资金
# ---------------------------------------------------------------------------

def _clist_page(pn, pz=100, fs=None, fields=CLIST_FIELDS, sort="f6"):
    doc = http_json("https://push2.eastmoney.com/api/qt/clist/get",
                    {"pn": str(pn), "pz": str(pz), "po": "1", "np": "1", "fltt": "2",
                     "invt": "2", "fs": fs or MARKET_FS, "fields": fields,
                     "fid": sort}, timeout=20, tries=2)
    data = (doc or {}).get("data") or {}
    return (data.get("diff") or []), num(data.get("total"))


def market_summary(refresh=False, log=None, full=True):
    """全市场汇总：流通市值合计、成交额合计、家数（东财 clist 分页）。"""
    path = os.path.join(_day_dir(), "market_sum.json")
    cached = None if refresh else _read(path)
    if cached and cached.get("流通市值_亿"):
        return cached
    if not full:
        return {"值": None, "来源": "东财 clist 全市场分页", "数据日期": None,
                "口径": "全市场流通市值/成交额合计", "错误": "快速版未跑全市场汇总（可点「重新计算」）"}
    cap = amt = 0.0
    seen, page = 0, 1
    while page <= 70:
        rows, total = _clist_page(page)
        if not rows:
            break
        for r in rows:
            cap += num(r.get("f21")) or 0.0
            amt += num(r.get("f6")) or 0.0
        seen += len(rows)
        if total and seen >= total:
            break
        page += 1
        time.sleep(KLINE_GAP)
    out = {"值": round(cap / 1e8, 2), "成交额_亿": round(amt / 1e8, 2),
           "家数": seen, "来源": "东财 clist 全市场分页（f21 流通市值 / f6 成交额）",
           "数据日期": _today(), "口径": "全市场合计（未做任何排除）"}
    if not cap:
        out["值"] = None
        out["错误"] = "全市场汇总取不到（东财 clist 拒绝或限流）"
    if log:
        log("[%s] 全市场汇总 %s 只 ｜ 流通市值 %s 亿 ｜ 成交额 %s 亿"
            % ("OK" if cap else "WARN", seen, out["值"], out.get("成交额_亿")))
    return _write(path, out) if cap else out          # 失败不落缓存，下次还能重试


def margin_balance(pandoc):
    """两融余额（融资余额 + 融券余额，亿元）。"""
    node = (((pandoc or {}).get("data") or {}).get("funds") or {}).get("两融") or {}
    fin, sec = num(node.get("融资余额_亿")), num(node.get("融券余额_亿"))
    if fin is None:
        return ev(None, "pan 快照 · 两融", None, "融资余额 + 融券余额",
                  "pan 快照里没有两融数据（该数据为交易所 T+1，可能还没更新）")
    out = ev((fin or 0) + (sec or 0), "pan 快照 · 两融", node.get("日期"),
             "融资余额 %s 亿 + 融券余额 %s 亿" % (fin, sec if sec is not None else "—"))
    return out


# ---------------------------------------------------------------------------
# 5 情绪：本地逐日历史 + 涨跌停池回抓
# ---------------------------------------------------------------------------

def load_daily_history():
    doc = aiplan.read_json(DAILY_HIST) if os.path.exists(DAILY_HIST) else None
    return dict(doc) if isinstance(doc, dict) else {}


def save_daily_history(hist):
    return _write(DAILY_HIST, hist)


def record_snapshot(pandoc):
    """把当天 pan 快照里的广度数据写进本地历史（同一天覆盖，保留最近 90 天）。"""
    d = ((pandoc or {}).get("data") or {})
    bd = ((d.get("sentiment") or {}).get("广度") or {})
    day = str((pandoc or {}).get("trade_date") or "")[:10]
    hist = load_daily_history()
    if day and bd:
        up, down = num(bd.get("上涨家数")), num(bd.get("下跌家数"))
        lu, ld = num(bd.get("涨停家数_阈值口径")), num(bd.get("跌停家数_阈值口径"))
        stu, std = num(bd.get("ST涨停家数_阈值口径")) or 0, num(bd.get("ST跌停家数_阈值口径")) or 0
        hist[day] = {
            "上涨家数": up, "下跌家数": down, "平盘家数": num(bd.get("平盘家数")),
            "涨停_非ST": None if lu is None else max(0, lu - stu),
            "跌停_非ST": None if ld is None else max(0, ld - std),
            "成交额_亿": num(bd.get("两市成交额_亿")),
            "主力净流入_亿": num(bd.get("主力净流入_亿")),
            "上涨占比_pct": num(bd.get("上涨占比_pct")),
        }
        for key in sorted(hist)[:-90]:
            hist.pop(key, None)
        save_daily_history(hist)
    return hist


def limit_counts_from_pan_dirs(days=20):
    """把还留在 data/pan/<日期>/ 里的历史快照补进本地历史（过期目录已被清理，能补多少算多少）。"""
    root = os.path.join(DATA_DIR, "pan")
    hist = load_daily_history()
    if not os.path.isdir(root):
        return hist
    changed = False
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name, "latest_post.json")
        if not os.path.isfile(path):
            continue
        doc = aiplan.read_json(path)
        day = str((doc or {}).get("trade_date") or "")[:10]
        if not day or day in hist:
            continue
        before = len(hist)
        merged = record_snapshot(doc)
        hist = merged
        changed = changed or len(hist) != before
    if changed:
        save_daily_history(hist)
    return hist


def window_mean(hist, field, days, trade_date):
    """历史里最近 days 个交易日（含当天）该字段的均值；不足 days 天返回 (None, 天数)。"""
    keys = [k for k in sorted(hist) if k <= str(trade_date or "9999-99-99")][-days:]
    vals = [num((hist.get(k) or {}).get(field)) for k in keys]
    vals = [v for v in vals if v is not None]
    if len(vals) < days or not vals:
        return None, len(vals)
    return round(sum(vals) / len(vals), 2), len(vals)


def window_sum_ratio(hist, days, trade_date):
    """最近 days 个交易日累计「上涨家数 / 下跌家数」比。"""
    keys = [k for k in sorted(hist) if k <= str(trade_date or "9999-99-99")][-days:]
    up = down = 0
    n = 0
    for k in keys:
        row = hist.get(k) or {}
        if row.get("上涨家数") is None or row.get("下跌家数") is None:
            continue
        up += num(row["上涨家数"]) or 0
        down += num(row["下跌家数"]) or 0
        n += 1
    if n < days or not down:
        return None, n
    return round(up / down, 3), n


def index_amount5(bars_sh, bars_sz, days=5):
    """近 N 日日均全市场成交额（亿）：上证综指 + 深证综指 每日成交额之和的均值。"""
    if len(bars_sh) < days or len(bars_sz) < days:
        return None, 0
    vals = []
    for a, b in zip(bars_sh[-days:], bars_sz[-days:]):
        ea, eb = num(a.get("额")), num(b.get("额"))
        if ea is None or eb is None:
            return None, 0
        vals.append((ea + eb) / 1e8)
    return round(sum(vals) / len(vals), 2), days


# ---------------------------------------------------------------------------
# 6 外围：汇率中间价（中国货币网当日 + 本地积累）
# ---------------------------------------------------------------------------

def csindex_midprice():
    """中国货币网「人民币汇率中间价」当日值（有公开 JSON 就给，没有返回 None）。"""
    doc = http_json(CCPR_URL, None, headers={"User-Agent": "Mozilla/5.0",
                                             "Referer": "https://www.chinamoney.com.cn/"})
    data = (doc or {}).get("data") or {}
    for key in ("usdCny", "USDCNY", "midPrice"):
        val = num(data.get(key))
        if val:
            return val
    for row in (data.get("exchangeRateList") or data.get("list") or []):
        if "美元" in str(row.get("pair") or row.get("ccyPair") or ""):
            val = num(row.get("price") or row.get("rate"))
            if val:
                return val
    return None


def sina_usdcny():
    """新浪在岸人民币报价（官方中间价没有公开接口时的兜底口径）。"""
    raw = pan.http_get_bytes(SINA_FX_URL, headers={"User-Agent": "Mozilla/5.0",
                                                   "Referer": "https://finance.sina.com.cn/"},
                             timeout=12, tries=2)
    text = (raw or b"").decode("gbk", "replace")
    if "=" not in text:
        return None, None
    body = text.split("=", 1)[1].strip().strip(";").strip('"')
    parts = body.split(",")
    if len(parts) < 2:
        return None, None
    return num(parts[1]), (parts[-1][:10] if parts else None)


def cnh_midprice(refresh=False, log=None):
    """人民币兑美元汇率（当日值写进本地历史）。返回 (当前值, 历史 dict)。"""
    path = os.path.join(HIST_DIR, "midprice.json")
    hist = _read(path) or {}
    day = datetime.now().strftime("%Y-%m-%d")
    if refresh or day not in hist:
        val = csindex_midprice()
        src = "中国货币网 · 人民币汇率中间价"
        if val is None:
            val, _d = sina_usdcny()
            src = "新浪财经 · 在岸人民币报价（官方中间价无公开接口，口径已标注）"
        if val is not None:
            hist[day] = val
            hist["_来源"] = src
            for key in sorted(hist)[:-120]:
                hist.pop(key, None)
            _write(path, hist)
        elif log:
            log("[WARN] 人民币汇率取不到（中国货币网中间价与新浪报价都失败）")
    return hist.get(day), hist


def midprice_change(hist, days=20):
    """最近 days 个自然日窗口的中间价变动（%）：升值幅度 = -(USDCNY 变动)。"""
    keys = [k for k in sorted(hist) if len(k) == 10 and k[4] == "-"][-days:]
    if len(keys) < days:
        return None, len(keys)
    first, last = num(hist[keys[0]]), num(hist[keys[-1]])
    if not first or not last:
        return None, len(keys)
    return round(-(last / first - 1.0) * 100.0, 2), len(keys)


# ---------------------------------------------------------------------------
# 4 宽度：收盘价高于 MA20 的个股占比（成交额前 N 只样本）
# ---------------------------------------------------------------------------

def width_above_ma20(refresh=False, sample=400, log=None, full=True):
    """当日全市场（按成交额降序前 N 只）收盘价 > MA20 的占比（%）。"""
    path = os.path.join(_day_dir(), "width_ma20.json")
    cached = None if refresh else _read(path)
    if cached and cached.get("值") is not None:
        return cached
    if not full:
        return {"值": None, "来源": "样本口径", "数据日期": None,
                "口径": "成交额前 %d 只" % sample,
                "错误": "快速版未算宽度（可点「重新计算」）"}
    rows, total = [], None
    page = 1
    while len(rows) < sample and page <= 10:
        got, total = _clist_page(page, pz=100, fields="f12,f14,f6")
        if not got:
            break
        rows += got
        page += 1
        time.sleep(KLINE_GAP)
    rows = rows[:sample]
    above = counted = 0
    for i, r in enumerate(rows, start=1):
        c6 = aiplan.code6(str(r.get("f12") or ""))
        bars = mechdata.kline(c6) if c6 else []
        if len(bars) >= 20:
            closes = [num(b.get("close")) for b in bars[-20:]]
            if all(c is not None for c in closes):
                counted += 1
                if closes[-1] >= sum(closes) / 20.0:
                    above += 1
        if log and i % 50 == 0:
            log("[..] 宽度样本 %d/%d（已判定 %d 只）" % (i, len(rows), counted))
    out = {"值": round(above / counted * 100.0, 1) if counted else None,
           "样本": counted, "口径": "成交额降序前 %d 只（未做排除）的样本口径" % sample,
           "来源": "东财全市场行情 + 逐股前复权日K", "数据日期": _today()}
    if not counted:
        out["错误"] = "宽度样本的逐股日K 取不到（东财限流时会发生）"
    if log:
        log("[%s] 宽度：%s%%（%d/%d 只站上 20 日均线）"
            % ("OK" if counted else "WARN", out["值"], above, counted))
    return _write(path, out) if counted else out      # 失败不落缓存，下次还能重试


# ---------------------------------------------------------------------------
# 汇总入口
# ---------------------------------------------------------------------------

def collect(refresh=False, full=True, log=None):
    """把 6 模块要用的原始证据一次性取齐（取不到的项留错误文本，不抛错）。"""
    pan_path = newest_file(pan_files())
    pandoc = aiplan.read_json(pan_path) if pan_path else None
    trade_date = (pandoc or {}).get("trade_date")
    hist = limit_counts_from_pan_dirs()
    hist = record_snapshot(pandoc)
    amount5_local, amount_days = window_mean(hist, "成交额_亿", 5, trade_date)
    limit_up5, lu_days = window_mean(hist, "涨停_非ST", 5, trade_date)
    limit_down5, ld_days = window_mean(hist, "跌停_非ST", 5, trade_date)
    ad_ratio5, ad_days = window_sum_ratio(hist, 5, trade_date)
    mid_hist = cnh_midprice(refresh=refresh, log=log)[1]
    mid_chg20, mid_days = midprice_change(mid_hist, 20)
    bars300 = index_bars("1.000300", INDEX_BARS, refresh=refresh, log=log)
    month300 = index_bars("1.000300", INDEX_MONTH_BARS, klt="103", refresh=refresh, log=log)
    sh = index_bars("1.000001", 40, refresh=refresh, need_amount=True)
    sz = index_bars("0.399106", 40, refresh=refresh, need_amount=True)
    amount5, amt_days = index_amount5(sh.get("bars") or [], sz.get("bars") or [])
    amount_src = "上证综指 + 深证综指 日K 的 5 日均值（东财，当天缓存）"
    if amount5 is None:
        amount5, amt_days, amount_src = amount5_local, amount_days, "pan 快照逐日积累（本地历史）"
    fft = {}
    for secid in ("1.000001", "0.399106"):
        got = index_fflow(secid, 10, refresh=refresh, log=log)
        if not got.get("明细") and secid == "0.399106":
            got = index_fflow("0.399001", 10, refresh=refresh, log=log)
        fft[secid] = got
    us = {name: index_bars(secid, 40, refresh=refresh) for name, secid in US_SECIDS.items()}
    summary = market_summary(refresh=refresh, full=full, log=log)
    return {
        "交易日": trade_date, "pan路径": pan_path, "pan": pandoc,
        "取数时间": aiplan.iso_now(),
        "估值": {"沪深300PE": pe_percentile("000300", refresh=refresh, log=log),
                "中证500PE": pe_percentile("000905", refresh=refresh, log=log)},
        "宏观": {"国债10Y": bond10y(pandoc),
                "M2同比": m2_yoy(refresh=refresh, log=log),
                "拆借3M": shibor_3m(refresh=refresh, log=log)},
        "行情": {"沪深300日K": bars300, "沪深300月K": month300,
                "上证日K": sh, "深证日K": sz, "美元指数": us["美元指数"],
                "标普500": us["标普500"]},
        "资金": {"资金流": fft, "全市场汇总": summary, "两融": margin_balance(pandoc),
                "成交额5日": {"值": amount5, "天数": amt_days, "来源": amount_src}},
        "情绪": {"历史": hist, "涨停5日": {"值": limit_up5, "天数": lu_days},
                "跌停5日": {"值": limit_down5, "天数": ld_days},
                "涨跌家数比5日": {"值": ad_ratio5, "天数": ad_days}},
        "外围": {"中间价变动20日": {"值": mid_chg20, "天数": mid_days,
                                "来源": mid_hist.get("_来源"),
                                "数据日期": (sorted([k for k in mid_hist if k[0].isdigit()])[-1]
                                          if mid_hist else None)}},
        "宽度": width_above_ma20(refresh=refresh, full=full, log=log),
    }
