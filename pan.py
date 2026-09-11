# -*- coding: utf-8 -*-
"""pan.py — A 股盘前 / 盘中 / 盘后全流程数据抓取（单文件 CLI）。

与 stock3d.py 完全独立：不 import、不依赖它运行。
全部输出为**中性事实与数值**，不含买卖建议、不做评分、不构成投资建议。
stdout 只输出 ASCII 状态行（中文一律落 JSON 文件；PowerShell 管道会把中文按 GBK 吃掉）。

用法::

    python pan.py prep                     # 盘前（外围/汇率利率/大宗/宏观/供给/消息/竞价）
    python pan.py live                     # 盘中单次快照（指数量能/情绪/资金/板块）
    python pan.py post                     # 盘后复盘 + 次日前瞻
    python pan.py all                      # 三段合并写一个 JSON
    python pan.py post --pool 持仓数据.md   # 附加个股复盘

落盘：data/pan/<YYYYMMDD>/<HHMMSS>_<phase>.json  +  latest_<phase>.json
跨日状态：data/pan/state/<YYYYMMDD>/state.json（今日涨停池/炸板池/连板，供次日盘前引用）

依赖：**纯标准库**（urllib + subprocess 调 node），不需要 requests / pandas。
"""

# ===== [SEC-01] imports =====

import argparse
import datetime
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request


# ===== [SEC-02] 常量 =====

SCHEMA_VERSION = "1"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DEFAULT = os.path.join(SCRIPT_DIR, "data", "pan")

NODE_MAIN = (r"C:\Users\m1526\AppData\Roaming\npm\node_modules"
             r"\@hithink-tech\hithink-finance-cli\dist\cli\main.js")

TIMEOUT_S = 180                 # 单次 node 调用超时（秒）
RETRY_MAX = 2                   # 瞬断重试次数
HTTP_TIMEOUT = 15
UA_CHROME = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
BASE_HEADERS = {"User-Agent": UA_CHROME, "Accept": "application/json, text/plain, */*"}
_WEB_HEADERS = {"User-Agent": UA_CHROME, "Accept": "*/*"}

_RETRYABLE_HITS = ("timeout", "econn", "etimedout", "enotfound", "fetch failed",
                   "socket", "eai_again", "epipe", "network")
_PARAM_HITS = ("usage", "unknown option", "required", "invalid", "missing")

# ---- 指数（同花顺 thscode）----
INDEX_THSCODES = ("000001.SH", "399001.SZ", "399006.SZ", "000688.SH",
                  "000016.SH", "000300.SH", "000905.SH", "000852.SH")
INDEX_NAMES = {
    "000001.SH": "上证指数", "399001.SZ": "深证成指", "399006.SZ": "创业板指",
    "000688.SH": "科创50", "000016.SH": "上证50", "000300.SH": "沪深300",
    "000905.SH": "中证500", "000852.SH": "中证1000",
}
CORE_INDEXES = ("000001.SH", "399001.SZ", "399006.SZ", "000688.SH")
STYLE_INDEXES = ("000016.SH", "000300.SH", "000905.SH", "000852.SH")
TX_MIN_SYMBOLS = (("sh000001", "上证指数"), ("sz399006", "创业板指"))

# ---- 东方财富 ----
EM_CLIST_PATH = "/api/qt/clist/get"
EM_ULIST_PATH = "/api/qt/ulist.np/get"
EM_STOCKGET_PATH = "/api/qt/stock/get"
EM_KAMT_PATH = "/api/qt/kamt/get"
EM_DC_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
EM_HOSTS = ("https://push2delay.eastmoney.com", "https://push2.eastmoney.com",
            "https://82.push2.eastmoney.com")
EM_HEADERS = {**BASE_HEADERS, "Referer": "https://quote.eastmoney.com/"}
EM_DC_HEADERS = {**BASE_HEADERS, "Referer": "https://data.eastmoney.com/"}

# 全市场 A 股（沪主板 + 深主板 + 创业板 + 科创板 + 北交所），实测 total≈5900
EM_FS_ALL_A = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
EM_FS_INDUSTRY = "m:90+t:2"
EM_FS_CONCEPT = "m:90+t:3"
# f2最新 f3涨跌幅 f4涨跌额 f5成交量 f6成交额 f7振幅 f8换手 f10量比 f12代码 f14名称
# f15最高 f16最低 f17今开 f18昨收 f20总市值 f21流通市值 f62主力净流入 f184主力净占比
EM_F_ALL = "f2,f3,f4,f5,f6,f7,f8,f10,f12,f14,f15,f16,f17,f18,f20,f21,f62,f184"
EM_F_BOARD = "f3,f6,f8,f12,f14,f20,f62,f104,f105,f128,f136,f140"
EM_BOARD_FIELDS = "f2,f3,f12,f14,f62"
EM_CLIST_PZ = 100                 # 实测 pz>100 被服务端截断，固定 100
EM_CLIST_MAX_PAGES = 80           # 上限 100*80=8000，覆盖 ~5900 只

EM_GLOBAL_SECIDS = ("100.DJIA", "100.SPX", "100.NDX", "100.HSI",
                    "100.FTSE", "100.GDAXI", "100.FCHI", "100.UDI")
EM_US10Y = "171.US10Y"
EM_CN10Y = "171.CN10Y"
EM_INDEX_SECIDS = {
    "000001.SH": "1.000001", "399001.SZ": "0.399001", "399006.SZ": "0.399006",
    "000688.SH": "1.000688", "000016.SH": "1.000016", "000300.SH": "1.000300",
    "000905.SH": "1.000905", "000852.SH": "1.000852",
}

# ---- 新浪 ----
SINA_HQ_URL = "https://hq.sinajs.cn/list="
SINA_REF_HEADERS = {**BASE_HEADERS, "Referer": "https://finance.sina.com.cn/"}
SINA_GLOBAL_CODES = (
    ("gb_dji", "道琼斯", "权益"),
    ("gb_ixic", "纳斯达克", "权益"),
    ("gb_inx", "标普500", "权益"),
    ("gb_ndx", "纳斯达克100", "权益"),
    ("hf_CHA50CFD", "富时中国A50期货", "权益"),
    ("hf_HSI", "恒生指数期货", "权益"),
    ("fx_susdcnh", "美元兑离岸人民币", "汇率"),
    ("DINIW", "美元指数", "汇率"),
    ("hf_CL", "WTI原油", "大宗"),
    ("hf_OIL", "布伦特原油", "大宗"),
    ("hf_GC", "COMEX黄金", "大宗"),
    ("hf_SI", "COMEX白银", "大宗"),
    ("hf_CAD", "LME铜", "大宗"),
    ("hf_AHD", "LME铝", "大宗"),
    ("hf_ZSD", "LME锌", "大宗"),
    ("hf_S", "CBOT大豆", "大宗"),
    ("hf_C", "CBOT玉米", "大宗"),
)

# ---- 中国货币网 ----
CHINAMONEY_SHIBOR = "https://www.chinamoney.com.cn/ags/ms/cm-u-bk-shibor/ShiborHis"
CHINAMONEY_FRR = "https://www.chinamoney.com.cn/ags/ms/cm-u-bk-currency/FrrHis"
CHINAMONEY_HEADERS = {**BASE_HEADERS, "Referer": "https://www.chinamoney.com.cn/"}

# ---- 腾讯分时 ----
TX_MIN_URL = "https://ifzq.gtimg.cn/appstock/app/minute/query"
TX_REF_HEADERS = {"Referer": "https://gu.qq.com/"}

# ---- 巨潮公告 ----
CNINFO_QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_HEADERS = {**BASE_HEADERS, "Referer": "https://www.cninfo.com.cn/",
                  "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                  "X-Requested-With": "XMLHttpRequest"}

# ---- 快讯源（与 stock3d.py 同源，独立实现）----
CLS_ROLL_URL = "https://www.cls.cn/v1/roll/get_roll_list"
CLS_SIGN_APP = {"app": "CailianpressWeb", "os": "web", "sv": "7.7.5"}
CLS_RN_MAX = 50                  # 实测 rn>50 静默失败
CLS_HEADERS = {**BASE_HEADERS, "Referer": "https://www.cls.cn/"}
WALLSTCN_URLS = ["https://api-prod.wallstreetcn.com/apiv1/content/lives",
                 "https://api-one.wallstcn.com/apiv1/content/lives"]
WALLSTCN_CHANNELS = ("a-stock-channel", "global-channel")
WALLSTCN_HEADERS = {**BASE_HEADERS, "Referer": "https://wallstreetcn.com/live/global"}
JIN10_URL = "https://flash-api.jin10.com/get_flash_list"
JIN10_HEADERS = {**BASE_HEADERS, "x-app-id": "bVBF4FyRTn5NJF5n", "x-version": "1.0.0",
                 "Origin": "https://www.jin10.com", "Referer": "https://www.jin10.com/"}
THS_URL = "https://news.10jqka.com.cn/tapp/news/push/stock/"
THS_HEADERS = {**BASE_HEADERS, "Referer": "https://news.10jqka.com.cn/realtimenews.html"}
SINA_24H_URL = "https://zhibo.sina.com.cn/api/zhibo/feed"
EM_FLASH_URL = "https://np-listapi.eastmoney.com/comm/web/getFastNewsList"
EM_FLASH_PARAMS = {"client": "web", "biz": "web_724", "fastColumn": "102",
                   "sortEnd": "", "pageSize": "50", "req_trace": "1"}
EM_FLASH_HEADERS = {**BASE_HEADERS, "Referer": "https://kuaixun.eastmoney.com/"}

NEWS_SOURCES = (
    ("财联社电报", "cls-telegraph", "cls"),
    ("华尔街见闻", "wallstcn", "wallstcn"),
    ("金十数据", "jin10", "jin10"),
    ("同花顺", "10jqka", "ths"),
    ("新浪", "sina", "sina"),
    ("东财快讯", "em-flash", "em"),
)

# ---- 消息分层 / 宏观关键词 ----
POLICY_TOP = ("国常会", "国务院", "证监会", "央行", "中国人民银行", "财政部", "发改委",
              "政治局", "中央经济工作会议", "金融监管总局", "国资委", "货币政策", "降准",
              "降息", "LPR", "MLF", "逆回购", "公开市场操作", "印花税", "注册制")
POLICY_INDUSTRY = ("工信部", "能源局", "交通运输部", "住建部", "农业农村部", "商务部",
                   "科技部", "医保局", "市场监管总局", "数据局", "网信办", "补贴",
                   "规划", "试点", "准入", "行业标准")
MACRO_CALENDAR = ("CPI", "PPI", "PMI", "社融", "社会融资", "工业增加值", "固定资产投资",
                  "社会消费品零售", "GDP", "贸易帐", "M2", "信贷", "非农", "美联储",
                  "议息", "加息", "降息")

TAG_RULES = {
    "cnh_alert_bp": 300.0,
    "breadth_strong": 60.0,
    "breadth_weak": 40.0,
    "turnover_ratio_high": 1.2,
    "turnover_ratio_low": 0.8,
    "limit_break_rate_hot": 30.0,
    "conflict_pct": 3.0,
}

NORTH_NOTE = ("北向资金：沪深港通日度/实时净买入自 2024-08 起停止披露（交易所规则调整），"
              "本工具保留字段但不填充数值，以 南向资金 + 主力资金 + 龙虎榜席位 作为替代观察。")
SEWAN_NOTE = ("行业口径 = 东财行业板块 + 同花顺行业指数；严格申万一级/二级口径不在两个"
              "数据源内，如需申万口径需另接数据源。")


# ===== [SEC-03] 日志 / 上下文 =====


def say(msg):
    """stdout 只出 ASCII（中文会被 PowerShell 管道按 GBK 解码成乱码）。"""
    try:
        msg = re.sub(r"[^\x20-\x7e]", "", str(msg))
        msg = re.sub(r"\s{2,}", " ", msg).strip()
    except Exception:
        pass
    try:
        sys.stdout.write(msg + "\n")
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        sys.stdout.write(msg.encode(enc, "replace").decode(enc, "replace") + "\n")
    try:
        sys.stdout.flush()
    except Exception:
        pass


class Ctx(object):
    """运行上下文。"""

    def __init__(self, base, target, phase):
        self.base = base
        self.target = target                      # datetime.date
        self.phase = phase
        self.debug = False
        self.no_cache = False
        self.top = 20
        self.pool_path = None
        self.em_hosts = list(EM_HOSTS)
        self.calls = 0
        self.http_calls = 0
        self.marks = {}
        self.notes = []
        self.degrade = []
        self._calendar = None
        self._all_rows = None

    def mark(self, block, status, detail="", note_ascii=""):
        self.marks[block] = {"status": status, "detail": detail}
        if status == "DEGRADE":
            self.degrade.append("%s: %s" % (block, detail))
        tag = {"OK": "[OK]", "DEGRADE": "[DEGR]", "FAIL": "[FAIL]",
               "NA": "[NA]"}.get(status, "[??]")
        extra = ("  " + (note_ascii or detail)) if (note_ascii or detail) else ""
        say("%-8s %-24s%s" % (tag, block, extra))


def dbg(ctx, msg):
    if ctx.debug:
        say("[DBG] " + str(msg))


# ===== [SEC-04] hithink CLI 调用层（唯一 node 出入口） =====


def _is_retryable(err):
    e = (err or "").lower()
    return any(h in e for h in _RETRYABLE_HITS)


def _is_param_err(err):
    e = (err or "").lower()
    return any(h in e for h in _PARAM_HITS)


def _clean_cli_err(raw):
    s = (raw or "").strip()
    i = s.find("{")
    if i >= 0:
        try:
            d = json.loads(s[i:])
            e = d.get("error")
            if isinstance(e, dict):
                return "%s: %s" % (e.get("code") or "CLI_ERROR",
                                   re.sub(r"\s+", " ", str(e.get("message") or ""))[:220])
            if isinstance(e, str):
                return e[:220]
            if d.get("message"):
                return str(d["message"])[:220]
        except ValueError:
            pass
    return re.sub(r"\s+", " ", s)[:220]


def invoke(ctx, words, args=None, outfile=None):
    """调 hithink CLI。返回 {'ok':True,'payload':obj} 或 {'ok':False,'error':str}。"""
    argv = [NODE_MAIN] + list(words) + (list(args) if args else [])
    if outfile:
        os.makedirs(os.path.dirname(outfile), exist_ok=True)
        argv += ["--output", outfile]
    argv += ["--format", "json"]
    msg = "empty output"
    for attempt in range(1 + RETRY_MAX):
        if attempt:
            time.sleep(1.0)
        ctx.calls += 1
        try:
            p = subprocess.run(["node"] + argv, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=TIMEOUT_S)
            out = (p.stdout or "").strip()
            err = (p.stderr or "").strip()[-2000:]
        except subprocess.TimeoutExpired:
            out, err = "", "timeout"
        except OSError as ex:
            return {"ok": False, "error": "node 启动失败: %s" % ex}
        if outfile and os.path.exists(outfile):
            try:
                if os.path.getsize(outfile) > 0:
                    with io.open(outfile, "r", encoding="utf-8") as f:
                        return _judge(json.load(f), err)
            except Exception:
                pass
        if out.startswith("{"):
            try:
                return _judge(json.loads(out), err)
            except Exception:
                pass
        msg = _clean_cli_err(err or out[-400:] or
                             ("rc=%s" % getattr(p, "returncode", "?")))
        if _is_param_err(msg) or not _is_retryable(msg):
            return {"ok": False, "error": msg}
    return {"ok": False, "error": "still failing after retries: " + msg}


def _judge(payload, err):
    if isinstance(payload, dict):
        okv = payload.get("ok")
        if okv is False or okv == "false":
            e = payload.get("error") or err or "ok=false"
            if isinstance(e, dict):
                e = e.get("message") or json.dumps(e, ensure_ascii=False)
            return {"ok": False, "error": str(e)[:300]}
    return {"ok": True, "payload": payload}


def hk_rows(payload, key=None):
    """payload → 行列表（递归收集 dict 列表，去重按对象 id）。"""
    rows, seen = [], set()

    def walk(obj):
        if isinstance(obj, (dict, list)):
            if id(obj) in seen:
                return
            seen.add(id(obj))
        if isinstance(obj, dict):
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            if obj and any(isinstance(x, dict) for x in obj):
                for x in obj:
                    if isinstance(x, dict) and (key is None or key in x):
                        rows.append(x)
            for v in obj:
                walk(v)
    walk(payload)
    return rows


def probe_cli(ctx):
    if not os.path.exists(NODE_MAIN):
        say("[FAIL] cli: main.js not found: %s" % NODE_MAIN)
        say("       install: npm i -g @hithink-tech/hithink-finance-cli")
        return False
    r = invoke(ctx, ["capabilities"])
    if not r["ok"]:
        say("[FAIL] cli: capabilities failed: %s" % str(r.get("error"))[:200])
        return False
    say("[OK] cli: node + hithink CLI ready")
    return True


# ===== [SEC-05] 日期与时段 =====


def now_local():
    return datetime.datetime.now()


def parse_date(s):
    try:
        return datetime.datetime.strptime(str(s).strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def ms_to_date(ms, fmt="%Y-%m-%d"):
    try:
        return datetime.datetime.fromtimestamp(int(ms) / 1000.0).strftime(fmt)
    except (ValueError, TypeError, OSError, OverflowError):
        return ""


def date_to_ms(d):
    return int(time.mktime(datetime.datetime(d.year, d.month, d.day).timetuple())) * 1000


def ts_to_str(ts, fmt="%Y-%m-%d %H:%M"):
    try:
        return datetime.datetime.fromtimestamp(int(ts)).strftime(fmt)
    except (ValueError, TypeError, OSError, OverflowError):
        return "-"


def _parse_dt_ts(s):
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(time.mktime(time.strptime(str(s).strip(), f)))
        except (ValueError, TypeError):
            continue
    return None


def session_name(now=None):
    now = now or now_local()
    hm = now.hour * 60 + now.minute
    if now.weekday() >= 5:
        return "非交易日"
    if hm < 9 * 60 + 15:
        return "盘前"
    if hm < 9 * 60 + 25:
        return "集合竞价"
    if hm <= 11 * 60 + 30:
        return "盘中（上午）"
    if hm < 13 * 60:
        return "午间休市"
    if hm <= 15 * 60:
        return "盘中（下午）"
    return "盘后"


def fetch_calendar(ctx):
    """同花顺交易日历（一年窗口，缓存到 base/state/calendar.json）。"""
    if ctx._calendar is not None:
        return ctx._calendar
    path = os.path.join(ctx.base, "state", "calendar.json")
    today = datetime.date.today().strftime("%Y-%m-%d")
    cached = read_json(path) or {}
    if not ctx.no_cache and cached.get("fetched_on") == today and cached.get("days"):
        ctx._calendar = set(cached["days"])
        return ctx._calendar
    r = invoke(ctx, ["market", "calendar"])
    days = set()
    if r["ok"]:
        for row in hk_rows(r["payload"], "date"):
            d = str(row.get("date") or "")
            if re.fullmatch(r"\d{8}", d):
                days.add("%s-%s-%s" % (d[:4], d[4:6], d[6:]))
            elif parse_date(d):
                days.add(d)
    if days:
        write_json(path, {"fetched_on": today, "days": sorted(days)})
    else:
        say("[DEGR] calendar: 交易日历不可用，回退到「周一至周五」判定")
    ctx._calendar = days
    return days


def is_trading_day(ctx, d):
    days = fetch_calendar(ctx)
    if days:
        return d.strftime("%Y-%m-%d") in days
    return d.weekday() < 5


def resolve_target_date(ctx, want):
    """目标日解析。未来日期不做回退（同花顺日历窗口实测只覆盖到今日，
    拿未来日期去查会「查不到」而误判为非交易日）。"""
    d = want or datetime.date.today()
    if d > datetime.date.today():
        return d, False, ("%s 是未来日期；交易日历窗口只覆盖到今日，未做节假日校验，"
                          "实时类字段将取当前时刻值。" % d.strftime("%Y-%m-%d"))
    if is_trading_day(ctx, d):
        return d, False, ""
    back = d
    for _ in range(20):
        back -= datetime.timedelta(days=1)
        if is_trading_day(ctx, back):
            return back, True, "%s 非交易日，回退到最近交易日 %s" % (
                d.strftime("%Y-%m-%d"), back.strftime("%Y-%m-%d"))
    return d, False, "%s 非交易日，且回溯 20 天未找到交易日" % d.strftime("%Y-%m-%d")


def prev_trading_day(ctx, d):
    back = d
    for _ in range(20):
        back -= datetime.timedelta(days=1)
        if is_trading_day(ctx, back):
            return back
    return d - datetime.timedelta(days=1)


# ===== [SEC-06] 通用工具 =====


def num(v):
    """任意值 → float；缺失/非法 → None（缺失绝不置 0）。"""
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        if s in ("", "-", "--", "null", "None", "nan"):
            return None
        try:
            return float(s)
        except ValueError:
            return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _f(v):
    r = num(v)
    return 0.0 if r is None else r


def scale(v, div, nd=4):
    """原始值 → 换算值；缺失保持 None（绝不置 0）。"""
    x = num(v)
    return None if x is None else round(x / div, nd)


def clean_html(s):
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", "", str(s))
    return re.sub(r"\s+", " ", s).strip()


def make_item(src, id_, title, content, ts, url=""):
    return {"src": src, "id": str(id_), "title": title or "",
            "content": content or "", "ts": int(ts or 0), "url": url or ""}


def cls_sign(qs):
    return hashlib.md5(hashlib.sha1(qs.encode("utf-8")).hexdigest().encode("utf-8")).hexdigest()


def _cls_signed_url(path, params):
    qs = urllib.parse.urlencode(sorted(params.items()))
    return "%s?%s&sign=%s" % (path, qs, cls_sign(qs))


def http_get_bytes(url, params=None, headers=None, timeout=HTTP_TIMEOUT, tries=3):
    """标准库 GET → bytes 或 None（带退避重试，不抛异常）。"""
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    hdr = dict(_WEB_HEADERS)
    hdr.update(headers or {})
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=hdr)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception:
            if attempt < tries - 1:
                time.sleep(0.8 * (attempt + 1))
    return None


def http_post_json(url, data=None, headers=None, timeout=20, tries=2):
    """表单 POST → JSON 或 None。"""
    hdr = dict(_WEB_HEADERS)
    hdr.update(headers or {})
    body = urllib.parse.urlencode(data or {}).encode("utf-8")
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, data=body, headers=hdr)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:
            if attempt < tries - 1:
                time.sleep(0.8 * (attempt + 1))
    return None


def _http_get_json(url, params=None, headers=None, timeout=HTTP_TIMEOUT, tries=3):
    raw = http_get_bytes(url, params=params, headers=headers, timeout=timeout, tries=tries)
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None


# ===== [SEC-07] 东方财富（clist / ulist / datacenter） =====


def _em_json(ctx, path, params, tries=3, timeout=HTTP_TIMEOUT):
    """东财 push2 族：host 轮换 + 退避（push2 主域名实测时段性拒连）。"""
    for host in ctx.em_hosts:
        for attempt in range(tries):
            raw = http_get_bytes(host + path, params=params, headers=EM_HEADERS,
                                 timeout=timeout, tries=1)
            ctx.http_calls += 1
            if raw:
                try:
                    d = json.loads(raw.decode("utf-8", "replace"))
                    if isinstance(d, dict) and d.get("data") is not None:
                        return d
                except ValueError:
                    pass
            if attempt < tries - 1:
                time.sleep(0.8 * (attempt + 1))
    return None


def em_scan_all(ctx, fields=EM_F_ALL):
    """全市场 A 股扫描（按代码稳定分页，避免按涨跌幅排序时并列导致漏页）。
    实测：pz 上限 100；fid=f12&po=0 时 total=5913 且分页无重叠。"""
    if ctx._all_rows is not None and fields == EM_F_ALL:
        return ctx._all_rows
    rows, total = [], None
    for pn in range(1, EM_CLIST_MAX_PAGES + 1):
        d = _em_json(ctx, EM_CLIST_PATH, {
            "pn": str(pn), "pz": str(EM_CLIST_PZ), "po": "0", "np": "1",
            "fltt": "2", "invt": "2", "fid": "f12", "fs": EM_FS_ALL_A,
            "fields": fields})
        if not d:
            break
        node = d.get("data") or {}
        total = num(node.get("total"))
        batch = node.get("diff") or []
        if isinstance(batch, dict):
            batch = list(batch.values())
        if not batch:
            break
        rows.extend([x for x in batch if isinstance(x, dict)])
        if len(batch) < EM_CLIST_PZ:
            break
        if total is not None and len(rows) >= total:
            break
        time.sleep(0.05)
    if fields == EM_F_ALL:
        ctx._all_rows = rows
    dbg(ctx, "em_scan_all rows=%d total=%s" % (len(rows), total))
    return rows


def em_boards(ctx, fs, top=20):
    """东财行业/概念板块列表（按涨跌幅降序）。"""
    d = _em_json(ctx, EM_CLIST_PATH, {
        "pn": "1", "pz": str(max(1, min(100, top))), "po": "1", "np": "1",
        "fltt": "2", "invt": "2", "fid": "f3", "fs": fs, "fields": EM_F_BOARD})
    if not d:
        return None
    batch = (d.get("data") or {}).get("diff") or []
    if isinstance(batch, dict):
        batch = list(batch.values())
    return [x for x in batch if isinstance(x, dict)]


def em_board_constituents(ctx, board_code, limit=100):
    d = _em_json(ctx, EM_CLIST_PATH, {
        "pn": "1", "pz": str(limit), "po": "1", "np": "1", "fltt": "2", "invt": "2",
        "fid": "f3", "fs": "b:%s" % board_code, "fields": EM_BOARD_FIELDS})
    if not d:
        return None
    b = (d.get("data") or {}).get("diff") or []
    if isinstance(b, dict):
        b = list(b.values())
    return [x for x in b if isinstance(x, dict)]


def em_ulist(ctx, secids, fields="f12,f13,f14,f2,f3,f4"):
    if not secids:
        return None
    d = _em_json(ctx, EM_ULIST_PATH, {
        "fltt": "2", "invt": "2", "fields": fields, "secids": ",".join(secids)})
    if not d:
        return None
    b = (d.get("data") or {}).get("diff") or []
    if isinstance(b, dict):
        b = list(b.values())
    return [x for x in b if isinstance(x, dict)]


def em_quote(ctx, secid, fields="f43,f57,f58,f169,f170"):
    d = _em_json(ctx, EM_STOCKGET_PATH,
                 {"fltt": "2", "invt": "2", "fields": fields, "secid": secid})
    return (d or {}).get("data")


def em_dc(ctx, report, filter_=None, page_size=50, sort=None, sort_type="-1", page=1,
          tries=3):
    """东财 datacenter 报表。失败返回 None；报表不存在返回 {'__error__': ...}。"""
    params = {"reportName": report, "columns": "ALL", "pageSize": str(page_size),
              "pageNumber": str(page), "source": "WEB", "client": "WEB"}
    if filter_:
        params["filter"] = filter_
    if sort:
        params["sortColumns"] = sort
        params["sortTypes"] = sort_type
    for attempt in range(tries):
        raw = http_get_bytes(EM_DC_URL, params=params, headers=EM_DC_HEADERS,
                             timeout=20, tries=1)
        ctx.http_calls += 1
        if raw:
            try:
                d = json.loads(raw.decode("utf-8", "replace"))
                if d.get("success") is False:
                    return {"__error__": d.get("message") or "报表配置不存在"}
                return d
            except ValueError:
                pass
        if attempt < tries - 1:
            time.sleep(1.0 + attempt)
    return None


def em_dc_rows(ctx, report, **kw):
    d = em_dc(ctx, report, **kw)
    if d is None:
        return None, "东财报表请求失败"
    if isinstance(d, dict) and d.get("__error__"):
        return None, str(d["__error__"])
    return (d.get("result") or {}).get("data") or [], None


# ===== [SEC-08] 新浪国际行情 =====


def sina_quotes(ctx, codes):
    """新浪 hq.sinajs.cn → {code: [字段]}。响应为 GBK。"""
    raw = http_get_bytes(SINA_HQ_URL + ",".join(codes), headers=SINA_REF_HEADERS,
                         timeout=12, tries=2)
    ctx.http_calls += 1
    if not raw:
        return None
    try:
        text = raw.decode("gbk", "replace")
    except LookupError:                                 # pragma: no cover
        text = raw.decode("utf-8", "replace")
    out = {}
    for m in re.finditer(r'var hq_str_([A-Za-z0-9_]+)="([^"]*)"', text):
        code, body = m.group(1), m.group(2)
        if body:
            out[code] = body.split(",")
    return out


def _sina_row(code, f):
    out = {"sina_code": code, "name": "", "last": None, "pct": None, "chg": None,
           "open": None, "high": None, "low": None, "prev": None, "time": None}
    if not f:
        return out
    if code.startswith("gb_"):
        out["name"] = f[0] if f else ""
        out["last"] = num(f[1]) if len(f) > 1 else None
        out["pct"] = num(f[2]) if len(f) > 2 else None
        out["time"] = f[3] if len(f) > 3 else None
        out["chg"] = num(f[4]) if len(f) > 4 else None
        if len(f) > 5:
            out["open"] = num(f[5])
        if len(f) > 6:
            out["high"] = num(f[6])
        if len(f) > 7:
            out["low"] = num(f[7])
        if out["last"] is not None and out["chg"] is not None:
            out["prev"] = round(out["last"] - out["chg"], 6)
    elif code.startswith("hf_"):
        out["last"] = num(f[0]) if f else None
        if len(f) > 4:
            out["high"] = num(f[4])
        if len(f) > 5:
            out["low"] = num(f[5])
        out["time"] = f[6] if len(f) > 6 else None
        prev = num(f[7]) if len(f) > 7 else None
        out["prev"] = prev
        if len(f) > 8:
            out["open"] = num(f[8])
        out["name"] = f[13] if len(f) > 13 else ""
        if out["last"] is not None and prev:
            out["chg"] = round(out["last"] - prev, 6)
            out["pct"] = round((out["last"] - prev) / prev * 100.0, 4)
    elif code.startswith("fx_"):
        out["time"] = f[0] if f else None
        out["last"] = num(f[1]) if len(f) > 1 else None
        out["name"] = f[9] if len(f) > 9 else code
        out["pct"] = num(f[10]) if len(f) > 10 else None
        out["chg"] = num(f[11]) if len(f) > 11 else None
        if len(f) > 6:
            out["high"] = num(f[6])
        if len(f) > 7:
            out["low"] = num(f[7])
        if out["chg"] is not None and out["last"] is not None:
            out["prev"] = round(out["last"] - out["chg"], 6)
    else:                                                # DINIW 等
        out["time"] = f[0] if f else None
        out["last"] = num(f[1]) if len(f) > 1 else None
        out["name"] = f[9] if len(f) > 9 else code
        if len(f) > 6:
            out["high"] = num(f[6])
        if len(f) > 7:
            out["low"] = num(f[7])
    return out


def fetch_sina_global(ctx):
    """新浪国际行情：外围权益 / 汇率 / 大宗。返回 (rows, err)。"""
    codes = [c for c, _, _ in SINA_GLOBAL_CODES]
    data = sina_quotes(ctx, codes)
    if not data:
        return None, "新浪国际行情请求失败"
    rows = []
    for code, name, cls in SINA_GLOBAL_CODES:
        f = data.get(code)
        if not f:
            rows.append({"sina_code": code, "名称": name, "类别": cls, "state": "无数据",
                         "last": None, "pct": None})
            continue
        r = _sina_row(code, f)
        r.update({"名称": name, "类别": cls, "state": "OK"})
        rows.append(r)
    return rows, None


# ===== [SEC-09] 落盘 / 跨日状态 =====


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


def read_json(path):
    try:
        with io.open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def snapshot_dir(ctx, d=None):
    return os.path.join(ctx.base, (d or ctx.target).strftime("%Y%m%d"))


def state_dir(ctx, d):
    return os.path.join(ctx.base, "state", d.strftime("%Y%m%d"))


def save_snapshot(ctx, payload):
    d = snapshot_dir(ctx)
    stamp = now_local().strftime("%H%M%S")
    path = os.path.join(d, "%s_%s.json" % (stamp, ctx.phase))
    write_json(path, payload)
    latest = os.path.join(d, "latest_%s.json" % ctx.phase)
    write_json(latest, payload)
    return path, latest


# ===== [SEC-10] 快讯池（7 源，取一次供全流程复用） =====


def _cls_roll_items(data):
    out = []
    for n in (data or {}).get("roll_data") or []:
        if not isinstance(n, dict):
            continue
        title = clean_html(n.get("title") or "")
        content = clean_html(n.get("brief") or n.get("content") or "")
        if not title and not content:
            continue
        out.append(make_item("财联社电报", n.get("id"), title or content[:40], content,
                             n.get("ctime"),
                             n.get("shareurl") or "https://www.cls.cn/detail/%s" % n.get("id")))
    return out


def src_cls(ctx, pages=2):
    items, fail, last_time = [], None, None
    for _ in range(max(1, int(pages))):
        params = dict(CLS_SIGN_APP)
        params["rn"] = str(CLS_RN_MAX)
        if last_time:
            params["refresh_type"] = "1"
            params["last_time"] = str(last_time)
        raw = http_get_bytes(_cls_signed_url(CLS_ROLL_URL, params),
                             headers=CLS_HEADERS, timeout=15, tries=2)
        ctx.http_calls += 1
        if not raw:
            break
        try:
            d = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            break
        if str(d.get("errno")) not in ("0", "None"):
            fail = "财联社接口 errno=%s msg=%s" % (d.get("errno"), d.get("msg"))
            break
        batch = _cls_roll_items(d.get("data"))
        if not batch:
            break
        items.extend(batch)
        cts = [int(x["ts"]) for x in batch if x.get("ts")]
        last_time = min(cts) if cts else None
        if not last_time:
            break
        time.sleep(0.4)
    return items, (None if items else (fail or "财联社电报无数据"))


def src_wallstcn(ctx):
    items, seen, ok = [], set(), False
    for url in WALLSTCN_URLS:
        for ch in WALLSTCN_CHANNELS:
            d = _http_get_json(url, params={"channel": ch, "client": "pc",
                                            "cursor": 0, "limit": 40},
                               headers=WALLSTCN_HEADERS, timeout=12, tries=1)
            ctx.http_calls += 1
            if not d:
                continue
            lives = (d.get("data") or {}).get("items") or []
            if not lives:
                continue
            ok = True
            for n in lives:
                nid = n.get("id")
                if nid in seen:
                    continue
                seen.add(nid)
                title = clean_html(n.get("title", ""))
                content = clean_html(n.get("content_text", ""))
                items.append(make_item("华尔街见闻", nid, title or content[:40], content,
                                       n.get("display_time"),
                                       "https://wallstreetcn.com/live/%s" % nid))
        if ok:
            return items, None
    return items, (None if ok else "华尔街见闻主/备 host 均失败")


def src_jin10(ctx):
    items = []
    d = _http_get_json(JIN10_URL, params={"channel": "-8200", "vip": "1"},
                       headers=JIN10_HEADERS, timeout=12, tries=2)
    ctx.http_calls += 1
    if d is None:
        return items, "金十接口请求失败"
    for n in d.get("data") or []:
        content = clean_html((n.get("data") or {}).get("content", ""))
        if not content:
            continue
        items.append(make_item("金十数据", n.get("id"), content[:40], content,
                               _parse_dt_ts(n.get("time")) or int(time.time()),
                               "https://www.jin10.com/"))
    return items, (None if items else "金十接口返回空")


def src_ths(ctx):
    items = []
    d = _http_get_json(THS_URL, params={"page": 1, "tag": "", "track": "website",
                                        "pageSize": 50}, headers=THS_HEADERS,
                       timeout=15, tries=2)
    ctx.http_calls += 1
    if d is None:
        return items, "同花顺接口请求失败"
    if str(d.get("code")) != "200":
        return items, "同花顺接口返回异常 code=%s" % d.get("code")
    for n in (d.get("data") or {}).get("list") or []:
        items.append(make_item("同花顺", n.get("id"), n.get("title", ""),
                               n.get("digest", "") or "",
                               n.get("rtime") or n.get("ctime"), n.get("url", "")))
    return items, (None if items else "同花顺接口返回空列表")


def src_sina(ctx):
    items = []
    d = _http_get_json(SINA_24H_URL, params={"page": 1, "page_size": 30, "zhibo_id": 152,
                                             "tag_id": 0, "dire": "f", "dpc": 1},
                       timeout=12, tries=2)
    ctx.http_calls += 1
    if d is None:
        return items, "新浪直播接口请求失败"
    lst = ((((d.get("result") or {}).get("data") or {}).get("feed") or {}).get("list")) or []
    for n in lst:
        body = clean_html(n.get("rich_text") or "")
        if not body:
            continue
        url = ""
        try:
            url = (json.loads(n.get("ext") or "{}") or {}).get("docurl") or ""
        except ValueError:
            pass
        items.append(make_item("新浪", n.get("id"), body[:60], body,
                               _parse_dt_ts(n.get("create_time")), url))
    return items, (None if items else "新浪直播返回空列表")


def src_em(ctx):
    """东财 7×24 快讯。req_trace 必填；空数组一律报 FAIL（该接口的静默失败）。"""
    raw = http_get_bytes(EM_FLASH_URL, params=EM_FLASH_PARAMS,
                         headers=EM_FLASH_HEADERS, timeout=15, tries=2)
    ctx.http_calls += 1
    if not raw:
        return [], "东财快讯接口请求失败"
    try:
        d = json.loads(raw.decode("utf-8", "replace"))
    except ValueError as e:
        return [], "东财快讯响应非 JSON: %s" % e
    node = d.get("data") or {}
    rows = node.get("fastNewsList") or node.get("list") or []
    if not rows:
        return [], ("东财快讯 code=%s 但行数组为空（req_trace 缺失或被拒的静默失败）"
                    % d.get("code"))
    items = []
    for n in rows:
        title = clean_html(n.get("title") or "")
        content = clean_html(n.get("summary") or "")
        if not title and not content:
            continue
        items.append(make_item("东财快讯", n.get("code") or title[:24],
                               title or content[:40], content,
                               _parse_dt_ts(n.get("showTime")),
                               "https://kuaixun.eastmoney.com/"))
    return items, (None if items else "东财快讯全部条目为空")


_NEWS_FUNCS = {"cls": src_cls, "wallstcn": src_wallstcn, "jin10": src_jin10,
               "ths": src_ths, "sina": src_sina, "em": src_em}


def collect_news_pool(ctx):
    """全市场快讯池：每源取一次。返回 ({src: [items]}, {src: fail}, ok_count)。"""
    pool, fails, ok = {}, {}, 0
    for name, label, key in NEWS_SOURCES:
        try:
            items, err = _NEWS_FUNCS[key](ctx)
        except Exception as ex:
            items, err = [], "异常: %s" % ex
        pool[name] = items
        fails[name] = err
        if items:
            ok += 1
        detail = ("%d items" % len(items)) if items else str(err or "0 items")[:110]
        say("%-8s news-pool %-14s %s" % ("[OK]" if items else "[FAIL]", label, detail))
    return pool, fails, ok


def flatten_news(pool):
    items = []
    for v in pool.values():
        items.extend(v)
    items.sort(key=lambda x: x.get("ts") or 0, reverse=True)
    return items


def match_news(items, keywords, top):
    hits, seen = [], set()
    for it in items:
        text = "%s %s" % (it.get("title", ""), it.get("content", ""))
        if not any(k in text for k in keywords):
            continue
        sig = (it.get("title") or it.get("content") or "")[:40]
        if sig in seen:
            continue
        seen.add(sig)
        x = dict(it)
        x["时间"] = ts_to_str(it.get("ts"))
        hits.append(x)
        if len(hits) >= top:
            break
    return hits


# ===== [SEC-11] 全市场广度 / 情绪 / 资金聚合 =====


def limit_pct(code, name):
    """按板块与 ST 判定涨跌停幅度（%）。"""
    if "ST" in (name or "").upper():
        return 5.0
    c = code or ""
    if c[:2] in ("30", "68"):
        return 20.0
    if c[:2] in ("43", "83", "87", "88", "92") or c[:1] in ("4", "8"):
        return 30.0
    return 10.0


def is_st(name):
    return "ST" in (name or "").upper()


def breadth_stats(rows):
    """全市场行 → 广度/情绪聚合（缺失保持 None，绝不置 0）。"""
    up = down = flat = up5 = down5 = 0
    lu = ld = lu_st = ld_st = lu_yizi = lu_tzi = 0
    amt_sum = mf_sum = 0.0
    amt_ok = mf_ok = 0
    pcts, lu_stocks = [], []
    for r in rows:
        code, name = str(r.get("f12") or ""), str(r.get("f14") or "")
        pct = num(r.get("f3"))
        if pct is None:
            continue
        pcts.append(pct)
        if pct > 0:
            up += 1
        elif pct < 0:
            down += 1
        else:
            flat += 1
        if pct >= 5:
            up5 += 1
        if pct <= -5:
            down5 += 1
        lp = limit_pct(code, name)
        if pct >= lp - 0.6:
            lu += 1
            if is_st(name):
                lu_st += 1
            last, high, low = num(r.get("f2")), num(r.get("f15")), num(r.get("f16"))
            if None not in (last, high, low):
                if high == low == last:
                    lu_yizi += 1
                elif low < last and high == last:
                    lu_tzi += 1
            lu_stocks.append({"代码": code, "名称": name, "涨跌幅_pct": pct})
        elif pct <= -(lp - 0.6):
            ld += 1
            if is_st(name):
                ld_st += 1
        a, m = num(r.get("f6")), num(r.get("f62"))
        if a is not None:
            amt_sum += a
            amt_ok += 1
        if m is not None:
            mf_sum += m
            mf_ok += 1
    total = len(pcts)
    return {
        "样本数": total,
        "无行情家数": max(0, len(rows) - total),
        "样本说明": "样本数 = 全市场扫描中「有涨跌幅」的标的；停牌/无报价计入「无行情家数」。",
        "上涨家数": up, "下跌家数": down, "平盘家数": flat,
        "上涨占比_pct": round(up * 100.0 / total, 2) if total else None,
        "涨幅超5%家数": up5, "跌幅超5%家数": down5,
        "涨停家数_阈值口径": lu, "跌停家数_阈值口径": ld,
        "ST涨停家数_阈值口径": lu_st, "ST跌停家数_阈值口径": ld_st,
        "一字涨停家数_阈值口径": lu_yizi, "T字涨停家数_阈值口径": lu_tzi,
        "两市成交额_亿": round(amt_sum / 1e8, 2) if amt_ok else None,
        "主力净流入_亿": round(mf_sum / 1e8, 2) if mf_ok else None,
        "平均涨幅_pct": round(sum(pcts) / total, 4) if total else None,
        "中位数涨幅_pct": round(sorted(pcts)[total // 2], 4) if total else None,
        "涨停股名单_阈值口径": lu_stocks,
    }


def _pack_row(r, extra):
    d = {"代码": str(r.get("f12") or ""), "名称": str(r.get("f14") or ""),
         "涨跌幅_pct": num(r.get("f3")), "最新价": num(r.get("f2"))}
    for label, field, div in extra:
        d[label] = scale(r.get(field), div) if div else num(r.get(field))
    return d


def top_lists(rows, top=20):
    """成交额 / 换手率 / 振幅 / 主力净流入（流入、流出）TOP 榜。"""
    def topn(field, extra):
        rs = [r for r in rows if num(r.get(field)) is not None]
        rs.sort(key=lambda r: num(r.get(field)), reverse=True)
        return [_pack_row(r, extra) for r in rs[:top]]

    def botn(field, extra):
        rs = [r for r in rows if num(r.get(field)) is not None]
        rs.sort(key=lambda r: num(r.get(field)))
        return [_pack_row(r, extra) for r in rs[:top]]

    return {
        "成交额TOP": topn("f6", [("成交额_亿", "f6", 1e8)]),
        "换手率TOP": topn("f8", [("换手率_pct", "f8", None)]),
        "振幅TOP": topn("f7", [("振幅_pct", "f7", None)]),
        "主力净流入TOP": topn("f62", [("主力净流入_亿", "f62", 1e8)]),
        "主力净流出TOP": botn("f62", [("主力净流入_亿", "f62", 1e8)]),
    }


def board_summary(ctx, fs, top=5):
    """板块列表 → 领涨/领跌（含板块内上涨/下跌家数与领涨股）。"""
    rows = em_boards(ctx, fs, top=max(top, 20))
    if rows is None:
        return None, "东财板块列表请求失败"
    good = [r for r in rows if num(r.get("f3")) is not None]
    if not good:
        return None, "板块列表返回空"
    good.sort(key=lambda r: num(r.get("f3")), reverse=True)

    def pack(r):
        return {"代码": str(r.get("f12") or ""), "名称": str(r.get("f14") or ""),
                "涨跌幅_pct": num(r.get("f3")), "成交额_亿": scale(r.get("f6"), 1e8, 2),
                "主力净流入_亿": scale(r.get("f62"), 1e8, 2),
                "板块内上涨家数": num(r.get("f104")),
                "板块内下跌家数": num(r.get("f105")),
                "领涨股": str(r.get("f128") or ""),
                "领涨股涨跌幅_pct": num(r.get("f136"))}

    return {"领涨": [pack(r) for r in good[:top]],
            "领跌": [pack(r) for r in good[-top:]][::-1]}, None


def board_structure(ctx, board_code, top=5):
    """板块成分 → 板块内涨停数 / 涨幅超5%数 / 龙头。"""
    rows = em_board_constituents(ctx, board_code)
    if rows is None:
        return None, "板块成分请求失败"
    lu = up5 = 0
    best = None
    for r in rows:
        code, name = str(r.get("f12") or ""), str(r.get("f14") or "")
        pct = num(r.get("f3"))
        if pct is None:
            continue
        if pct >= limit_pct(code, name) - 0.6:
            lu += 1
        if pct >= 5:
            up5 += 1
        if best is None or pct > best["涨跌幅_pct"]:
            best = {"代码": code, "名称": name, "涨跌幅_pct": pct}
    return ({"成分数_取前100": len(rows), "板块内涨停家数": lu,
             "板块内涨幅超5%家数": up5, "板块龙头": best}, None)


# ===== [SEC-12] 指数与分时 =====


def hk_index_snapshot(ctx, codes=INDEX_THSCODES):
    r = invoke(ctx, ["index", "snapshot"], ["--thscodes", ",".join(codes)])
    if not r["ok"]:
        return None, str(r.get("error"))[:200]
    out = []
    for x in hk_rows(r["payload"], "thscode"):
        code = str(x.get("thscode") or "")
        if code not in codes:
            continue
        prev = num(x.get("prev_price"))
        hi, lo = num(x.get("high_price")), num(x.get("low_price"))
        out.append({
            "代码": code, "名称": INDEX_NAMES.get(code, code),
            "最新价": num(x.get("last_price")),
            "涨跌幅_pct": num(x.get("price_change_ratio_pct")),
            "涨跌额": num(x.get("price_change")),
            "今开": num(x.get("open_price")), "最高": hi, "最低": lo, "昨收": prev,
            "成交量": num(x.get("volume")),
            "成交额_亿": scale(x.get("turnover"), 1e8, 2),
            "振幅_pct": (round((hi - lo) / prev * 100.0, 4)
                         if None not in (hi, lo, prev) and prev else None),
        })
    return (out or None), (None if out else "指数快照返回空")


def em_index_snapshot(ctx, secids):
    rows = em_ulist(ctx, secids, "f12,f13,f14,f2,f3,f4,f15,f16,f17,f18,f5,f6,f7")
    if not rows:
        return None
    want = {s.split(".")[-1] for s in secids}        # secid '1.000001' → '000001'
    out = []
    for x in rows:
        code = str(x.get("f12") or "")
        if code not in want:
            continue
        out.append({"代码": code, "名称": str(x.get("f14") or ""),
                    "secid": "%s.%s" % (x.get("f13"), code),
                    "最新价": num(x.get("f2")), "涨跌幅_pct": num(x.get("f3")),
                    "涨跌额": num(x.get("f4")), "最高": num(x.get("f15")),
                    "最低": num(x.get("f16")), "今开": num(x.get("f17")),
                    "昨收": num(x.get("f18")), "成交量": num(x.get("f5")),
                    "成交额_亿": scale(x.get("f6"), 1e8, 2),
                    "振幅_pct": num(x.get("f7"))})
    return out or None


def fetch_tx_minute(ctx, sym):
    """腾讯分时：返回 (分钟序列, 最新价, 分时均价, err)。
    行格式 'HHMM 价格 累计量 累计额'；均价用分钟增量做 VWAP（指数口径）。"""
    d = _http_get_json(TX_MIN_URL, params={"code": sym}, headers=TX_REF_HEADERS,
                       timeout=12, tries=2)
    ctx.http_calls += 1
    if not d:
        return None, None, None, "腾讯分时请求失败"
    node = (d.get("data") or {}).get(sym) or {}
    inner = node.get("data")
    blob = (inner or {}).get("data") if isinstance(inner, dict) else inner
    if not blob:
        return None, None, None, "腾讯分时返回空"
    lines = blob if isinstance(blob, list) else str(blob).split(";")
    rows = []
    for line in lines:
        p = line.strip().split()
        if len(p) < 3:
            continue
        rows.append({"时间": p[0], "价格": num(p[1]), "累计量": num(p[2]),
                     "累计额": num(p[3]) if len(p) > 3 else None})
    if not rows:
        return None, None, None, "腾讯分时无有效行"
    prev_vol, cum_pv, cum_v = 0.0, 0.0, 0.0
    for r in rows:
        v = r.get("累计量")
        p = r.get("价格")
        if v is None or p is None:
            r["分钟量"] = None
            r["分时均价"] = (round(cum_pv / cum_v, 4) if cum_v else None)
            continue
        dv = v - prev_vol
        prev_vol = v
        r["分钟量"] = round(dv, 2)
        if dv > 0:
            cum_pv += p * dv
            cum_v += dv
        r["分时均价"] = (round(cum_pv / cum_v, 4) if cum_v else None)
    vwap = rows[-1].get("分时均价")
    return rows, rows[-1].get("价格"), vwap, None


# ===== [SEC-13] hithink 特色数据 =====


def hk_pool(ctx, which, date_ms=None, size=200, max_pages=6):
    """special limit-up-pool / limit-down-pool / limit-break-pool 分页拉取。"""
    cmd = {"up": ["special", "limit-up-pool"], "down": ["special", "limit-down-pool"],
           "break": ["special", "limit-break-pool"]}[which]
    items, err, pages = [], None, None
    for pn in range(1, max_pages + 1):
        args = ["--page", str(pn), "--size", str(size)]
        if date_ms:
            args += ["--date-ms", str(date_ms)]
        r = invoke(ctx, cmd, args)
        if not r["ok"]:
            err = str(r.get("error"))[:180]
            break
        rows = hk_rows(r["payload"], "thscode")
        if not rows:
            break
        items.extend(rows)
        data = r["payload"].get("data") or {}
        pag = None
        if isinstance(data.get("pagination"), dict):
            pag = data["pagination"]
        elif isinstance(data.get("page"), dict):
            pag = data["page"]
        if pag:
            pages = num(pag.get("pages"))
        if pages and pn >= pages:
            break
        time.sleep(0.2)
    return items, err


def pack_pool(items, kind):
    out = []
    for x in items or []:
        d = {"代码": str(x.get("thscode") or ""), "名称": str(x.get("name") or ""),
             "最新价": num(x.get("last_price")),
             "涨跌幅_pct": num(x.get("price_change_ratio_pct")),
             "是否ST": bool(x.get("is_st")), "是否次新": bool(x.get("is_new"))}
        if kind == "up":
            d.update({"首次封板时间": x.get("limit_up_time"),
                      "连续板数": num(x.get("continue_day_cnt")),
                      "连板文本": x.get("continue_day_text"),
                      "封单额_亿": scale(x.get("seal_money"), 1e8, 4),
                      "最大封单额_亿": scale(x.get("max_seal_money"), 1e8, 4),
                      "涨停原因": x.get("limit_up_reason")})
        elif kind == "break":
            d.update({"炸板次数": num(x.get("open_times")),
                      "换手率_pct": num(x.get("turnover_ratio_pct")),
                      "成交额_亿": scale(x.get("turnover"), 1e8, 4)})
        elif kind == "down":
            d.update({"首次跌停时间": x.get("first_limit_time"),
                      "最后跌停时间": x.get("last_limit_time"),
                      "换手率_pct": num(x.get("turnover_ratio_pct"))})
        out.append(d)
    return out


def hk_ladder(ctx):
    """连板天梯（30 日窗口）→ (最新一日, 窗口日期列表, err)。"""
    r = invoke(ctx, ["special", "limit-up-ladder"])
    if not r["ok"]:
        return None, None, str(r.get("error"))[:180]
    rows = hk_rows(r["payload"], "boards")
    if not rows:
        return None, None, "连板天梯返回空"
    data = r["payload"].get("data") or {}
    win = (data.get("window") or {}).get("date_list")
    rows.sort(key=lambda x: str(x.get("date") or ""), reverse=True)
    return rows[0], win, None


def ladder_stats(day_row):
    boards = (day_row or {}).get("boards") or {}
    tiers = (("2连板", "two_board"), ("3连板", "three_board"), ("4连板", "four_board"),
             ("5连板", "five_board"), ("6连板", "six_board"), ("7连板及以上", "seven_over"))
    out, names, total, top = {}, [], 0, 0
    for label, key in tiers:
        arr = boards.get(key) or []
        out[label] = len(arr)
        total += len(arr)
        for s in arr:
            n = num(s.get("board_num"))
            if n is not None:
                top = max(top, int(n))
            names.append({"代码": str(s.get("thscode") or ""),
                          "名称": str(s.get("name") or ""), "连板数": n, "梯队": label,
                          "次日是否封板": s.get("seal_nextday")})
    return {"日期": (day_row or {}).get("date"), "连板股总数": total,
            "最高连板高度": top or None, "各梯队数量": out, "连板股名单": names}


def hk_dragon_tiger(ctx, board_type, date_str):
    r = invoke(ctx, ["special", "dragon-tiger"],
               ["--board-type", board_type, "--date", date_str])
    if not r["ok"]:
        return None, str(r.get("error"))[:180]
    return hk_rows(r["payload"]), None


def pack_dragon_tiger(rows, top=50):
    """同花顺龙虎榜字段（实测）：net_value / buy_value / sell_value / change(小数) /
    net_rate(小数) / hot_rank / hot_money_net_value。"""
    out = []
    for x in rows or []:
        chg = num(x.get("change"))
        rate = num(x.get("net_rate"))
        d = {"代码": str(x.get("thscode") or ""), "名称": str(x.get("name") or ""),
             "净买额_亿": scale(x.get("net_value"), 1e8, 4),
             "买入额_亿": scale(x.get("buy_value"), 1e8, 4),
             "卖出额_亿": scale(x.get("sell_value"), 1e8, 4),
             "涨跌幅_pct": (round(chg * 100, 4) if chg is not None else None),
             "净占比_pct": (round(rate * 100, 4) if rate is not None else None),
             "热度排名": num(x.get("hot_rank")),
             "游资净额_亿": scale(x.get("hot_money_net_value"), 1e8, 4),
             "统计天数": num(x.get("range_days"))}
        if x.get("reason"):
            d["上榜原因"] = x.get("reason")
        out.append(d)
    return out[:top]


def hk_auction_snapshot(ctx, codes, stage="final"):
    r = invoke(ctx, ["market", "auction-snapshot"],
               ["--thscodes", ",".join(codes), "--stage", stage])
    if not r["ok"]:
        return None, str(r.get("error"))[:180]
    return hk_rows(r["payload"]), None


def hk_auction_benchmark(ctx, date_str=None):
    args = ["--date", date_str] if date_str else []
    r = invoke(ctx, ["market", "auction-benchmark"], args)
    if not r["ok"]:
        return None, str(r.get("error"))[:180]
    return r["payload"], None


# ===== [SEC-14] 盘前：宏观流动性 / 政策 / 供给 =====


def fetch_shibor(ctx, target):
    """中国货币网 Shibor（ON / 1W 及各期限）。"""
    ed = target.strftime("%Y-%m-%d")
    sd = (target - datetime.timedelta(days=20)).strftime("%Y-%m-%d")
    d = _http_get_json(CHINAMONEY_SHIBOR,
                       params={"lang": "CN", "startDate": sd, "endDate": ed,
                               "pageNum": 1, "pageSize": 10},
                       headers=CHINAMONEY_HEADERS, timeout=20, tries=2)
    ctx.http_calls += 1
    if not d:
        return None, "中国货币网 Shibor 请求失败"
    recs = d.get("records") or []
    if not recs:
        return None, "中国货币网 Shibor 返回空（或接口结构变化）"
    recs.sort(key=lambda x: str(x.get("showDateCN") or ""), reverse=True)
    latest, prev = recs[0], (recs[1] if len(recs) > 1 else None)
    keys = ("ON", "1W", "2W", "1M", "3M", "6M", "9M", "1Y")
    out = {"日期": latest.get("showDateCN"), "隔夜_ON": num(latest.get("ON")),
           "7天_1W": num(latest.get("1W")),
           "各期限": {k: num(latest.get(k)) for k in keys},
           "前一日": ({k: num(prev.get(k)) for k in keys} if prev else None)}
    if out["隔夜_ON"] is not None and prev and num(prev.get("ON")) is not None:
        out["隔夜_变动_bp"] = round((out["隔夜_ON"] - num(prev.get("ON"))) * 100, 2)
    if out["7天_1W"] is not None and prev and num(prev.get("1W")) is not None:
        out["7天_变动_bp"] = round((out["7天_1W"] - num(prev.get("1W"))) * 100, 2)
    return out, None


def fetch_dr007(ctx, target):
    """中国货币网 FRR 定盘利率 → FDR007（即 DR007）。"""
    ed = target.strftime("%Y-%m-%d")
    sd = (target - datetime.timedelta(days=20)).strftime("%Y-%m-%d")
    d = _http_get_json(CHINAMONEY_FRR,
                       params={"lang": "CN", "startDate": sd, "endDate": ed},
                       headers=CHINAMONEY_HEADERS, timeout=20, tries=2)
    ctx.http_calls += 1
    if not d:
        return None, "中国货币网 FRR 请求失败"
    recs = d.get("records")
    if not isinstance(recs, list):
        recs = (d.get("data") or {}).get("records") if isinstance(d.get("data"), dict) else None
    if not recs:
        return None, "中国货币网 FRR 返回空（或接口结构变化）"
    # 实测结构：每条记录的值在 frValueMap 内（date / FDR007 / FR007 / FDR001 / FR001 …）
    rows = []
    for rec in recs:
        vm = rec.get("frValueMap") or {}
        day = vm.get("date") or rec.get("lfiProducDate")
        if not day:
            continue
        rows.append({"date": str(day), "map": vm})
    if not rows:
        return None, "中国货币网 FRR 记录缺少 frValueMap"
    rows.sort(key=lambda x: x["date"], reverse=True)
    latest, prev = rows[0], (rows[1] if len(rows) > 1 else None)
    lm = latest["map"]
    out = {"日期": latest["date"],
           "FDR007": num(lm.get("FDR007")), "FR007": num(lm.get("FR007")),
           "FDR001": num(lm.get("FDR001")), "FR001": num(lm.get("FR001")),
           "FDR014": num(lm.get("FDR014")),
           "note": "FDR007 = 存款类机构 7 天质押式回购定盘利率（即 DR007）"}
    if out["FDR007"] is not None and prev and num(prev["map"].get("FDR007")) is not None:
        out["FDR007_变动_bp"] = round(
            (out["FDR007"] - num(prev["map"].get("FDR007"))) * 100, 2)
    return out, None


def fetch_policy_calendar(ctx, items, target, top):
    """央行操作 / LPR / 宏观日历 → 快讯关键词提取（新闻派生口径，标 DEGRADE）。"""
    day0 = target.strftime("%Y-%m-%d")
    day_scope = [it for it in items
                 if ts_to_str(it.get("ts"), "%Y-%m-%d") == day0]
    scope = day_scope or items[:400]
    policy = match_news([it for it in items if any(
        k in (it.get("title", "") + it.get("content", "")) for k in POLICY_TOP)],
        POLICY_TOP, top)
    industry = match_news([it for it in items if any(
        k in (it.get("title", "") + it.get("content", "")) for k in POLICY_INDUSTRY)],
        POLICY_INDUSTRY, top)
    cal = match_news([it for it in scope if any(
        k in (it.get("title", "") + it.get("content", "")) for k in MACRO_CALENDAR)],
        MACRO_CALENDAR, top)
    return {"当日快讯条数": len(day_scope), "顶层政策": policy, "行业政策": industry,
            "宏观日历_数据发布": cal,
            "note": "央行公开市场操作 / LPR / 宏观日历无稳定公开接口，"
                    "此处为快讯关键词派生口径（DEGRADE），非官方日历。"}


def fetch_cninfo_market(ctx, target, page_size=50):
    """巨潮市场级公告（晚间公告汇总用）。"""
    day = target.strftime("%Y-%m-%d")
    rows = []
    for col in ("szse", "sse"):
        form = {"pageNum": "1", "pageSize": str(page_size), "column": col,
                "tabName": "fulltext", "plate": "", "stock": "", "searchkey": "",
                "secid": "", "category": "", "trade": "", "seDate": day,
                "sortName": "time", "sortType": "desc", "isHLtitle": "true"}
        d = http_post_json(CNINFO_QUERY_URL, data=form, headers=CNINFO_HEADERS,
                           timeout=20, tries=2)
        ctx.http_calls += 1
        for a in (d or {}).get("announcements") or []:
            rows.append({"时间": ms_to_date(a.get("announcementTime")),
                         "代码": str(a.get("secCode") or ""),
                         "名称": clean_html(a.get("secName") or ""),
                         "标题": clean_html(a.get("announcementTitle") or ""),
                         "类型": a.get("announcementTypeName") or "",
                         "url": "http://static.cninfo.com.cn/" + (a.get("adjunctUrl") or "")})
        time.sleep(0.2)
    seen, uniq = set(), []
    for r in rows:
        sig = (r["代码"], r["标题"], r["时间"])
        if sig in seen:
            continue
        seen.add(sig)
        uniq.append(r)
    uniq.sort(key=lambda x: x.get("时间") or "", reverse=True)
    return uniq


def _cap(v, div, nd=4):
    """可空换算：num 为 None 时保持 None。"""
    return scale(v, div, nd)


def fetch_supply(ctx, target, top):
    """市场供给：新股申购/上市、限售解禁、可转债（口径说明）。"""
    out = {}
    day = target.strftime("%Y-%m-%d")
    rows, err = em_dc_rows(ctx, "RPTA_APP_IPOAPPLY", page_size=100, sort="APPLY_DATE",
                           sort_type="1", filter_="(APPLY_DATE>='%s')" % day)
    if rows is None:
        out["新股"] = {"error": err}
    else:
        def pack(r):
            price = num(r.get("ISSUE_PRICE"))
            wan_shares = num(r.get("ISSUE_NUM"))          # 实测单位：万股
            return {"代码": r.get("SECURITY_CODE"), "申购代码": r.get("APPLY_CODE"),
                    "申购日": str(r.get("APPLY_DATE") or "")[:10],
                    "上市日": str(r.get("LISTING_DATE") or "")[:10],
                    "市场": r.get("TRADE_MARKET"), "发行价": price,
                    "发行数量_万股": wan_shares,
                    "网上发行_万股": _cap(r.get("ONLINE_ISSUE_NUM"), 1e4, 2),
                    "募资_亿": (round(price * wan_shares / 1e4, 4)
                              if None not in (price, wan_shares) else None)}
        listing, lerr = em_dc_rows(ctx, "RPTA_APP_IPOAPPLY", page_size=100,
                                   sort="LISTING_DATE", sort_type="1",
                                   filter_="(LISTING_DATE>='%s')" % day)
        out["新股"] = {
            "当日申购": [pack(r) for r in rows if str(r.get("APPLY_DATE") or "")[:10] == day],
            "当日上市": [{"代码": r.get("SECURITY_CODE"),
                          "上市日": str(r.get("LISTING_DATE") or "")[:10],
                          "市场": r.get("TRADE_MARKET")}
                         for r in (listing or [])
                         if str(r.get("LISTING_DATE") or "")[:10] == day],
            "后续待申购": [pack(r) for r in rows
                           if str(r.get("APPLY_DATE") or "")[:10] > day][:top],
            "当日上市_error": lerr}
    filt = "(FREE_DATE>='%s')(FREE_DATE<='%s')" % (day, day)
    rows2, err2 = em_dc_rows(ctx, "RPT_LIFT_STAGE", page_size=200, sort="FREE_DATE",
                             sort_type="1", filter_=filt)
    if rows2 is None:
        out["限售解禁"] = {"error": err2, "filter": filt}
    else:
        # 实测单位：LIFT_MARKET_CAP=万元，CURRENT_FREE_SHARES=万股，TOTAL_RATIO=小数
        items = [{"代码": r.get("SECURITY_CODE"), "名称": r.get("SECURITY_NAME_ABBR"),
                  "解禁日": str(r.get("FREE_DATE") or "")[:10],
                  "解禁市值_亿": _cap(r.get("LIFT_MARKET_CAP"), 1e4, 4),
                  "解禁股数_万股": num(r.get("CURRENT_FREE_SHARES")),
                  "解禁类型": r.get("FREE_SHARES_TYPE"),
                  "占总股本_pct": (round(_f(r.get("TOTAL_RATIO")) * 100, 4)
                                 if num(r.get("TOTAL_RATIO")) is not None else None)}
                 for r in rows2]
        items.sort(key=lambda x: (x.get("解禁市值_亿") or 0), reverse=True)
        out["限售解禁"] = {"解禁总市值_亿": round(sum(_f(x.get("解禁市值_亿")) for x in items), 4),
                           "解禁个股数": len(items),
                           "大额解禁名单": items[:top], "全部": items, "filter": filt,
                           "单位说明": "解禁市值单位亿元（源为万元）；解禁股数单位万股"}
    out["可转债"] = {"note": "东财无可稳定的可转债申购/上市报表口径；"
                             "改由消息面快讯关键词提取，见 data.news_layers"}
    return out


# ===== [SEC-15] 盘前：外围 / 竞价 =====


def fetch_overseas(ctx, target):
    """隔夜外围：新浪（权益/汇率/大宗） + 东财（欧股/债券收益率）。"""
    sina_rows, sina_err = fetch_sina_global(ctx)
    em_rows = em_ulist(ctx, list(EM_GLOBAL_SECIDS))
    bonds = {}
    for label, secid in (("美债10年期收益率", EM_US10Y), ("中债10年期收益率", EM_CN10Y)):
        q = em_quote(ctx, secid)
        bonds[label] = ({"最新": num(q.get("f43")), "涨跌": num(q.get("f169")),
                         "涨跌幅_pct": num(q.get("f170"))} if q else None)
    if sina_rows is None and em_rows is None:
        return None, "外围行情：新浪与东财均失败"
    em_map = {}
    for x in em_rows or []:
        em_map["%s.%s" % (x.get("f13"), x.get("f12"))] = {
            "名称": x.get("f14"), "最新": num(x.get("f2")),
            "涨跌幅_pct": num(x.get("f3")), "涨跌额": num(x.get("f4"))}
    groups = {"权益": [], "汇率": [], "大宗": []}
    for r in sina_rows or []:
        groups.setdefault(r.get("类别", "其他"), []).append(r)
    return {"权益_隔夜": groups.get("权益", []), "汇率": groups.get("汇率", []),
            "大宗商品": groups.get("大宗", []), "东财国际指数": em_map,
            "债券收益率": bonds, "sina_error": sina_err}, None


def fetch_auction(ctx, target):
    """集合竞价：指数竞价 + 全市场竞价聚合 + 前一日连板/炸板竞价表现。"""
    out = {}
    out["指数竞价"] = em_index_snapshot(ctx, list(EM_INDEX_SECIDS.values()))
    rows = em_scan_all(ctx)
    if not rows:
        return out, "东财全市场扫描失败，竞价聚合不可用"
    st = breadth_stats(rows)
    out["两市竞价成交额_亿"] = st.get("两市成交额_亿")
    out["竞价涨跌家数"] = {"上涨": st.get("上涨家数"), "下跌": st.get("下跌家数"),
                           "平盘": st.get("平盘家数"), "上涨占比_pct": st.get("上涨占比_pct")}
    out["竞价涨停家数"] = {"涨停_阈值口径": st.get("涨停家数_阈值口径"),
                           "跌停_阈值口径": st.get("跌停家数_阈值口径"),
                           "ST涨停_阈值口径": st.get("ST涨停家数_阈值口径")}
    bs, bs_err = board_summary(ctx, EM_FS_CONCEPT, top=5)
    out["竞价板块强弱"] = bs
    out["竞价板块强弱_error"] = bs_err
    snap, snap_err = hk_auction_snapshot(ctx, list(CORE_INDEXES), "final")
    out["同花顺竞价快照"] = {"items": snap, "error": snap_err,
                             "note": "today-only：仅当日集合竞价时段可用"}
    bench, bench_err = hk_auction_benchmark(ctx)
    out["同花顺竞价基准"] = {"data": bench, "error": bench_err}
    out["口径说明"] = (
        "竞价聚合（涨跌家数/成交额/板块/涨停家数）取自「运行时刻的全市场实时现值」："
        "在 9:15-9:25 运行即竞价数据；在盘中或盘后运行则等于该时刻的实时/收盘快照。"
        "本次运行 session=%s。" % session_name())
    prev = prev_trading_day(ctx, target)
    sdir = state_dir(ctx, prev)
    state = read_json(os.path.join(sdir, "state.json"))
    if not state:
        out["前一日连板股竞价表现"] = None
        out["前一日炸板股竞价表现"] = None
        out["state_note"] = ("缺少 %s 的本地 state（需在前一交易日收盘后跑过 post）；"
                             "该子块标 N/A，不伪造。" % sdir)
    else:
        row_map = {str(r.get("f12")): r for r in rows}

        def quote_of(code):
            r = row_map.get((code or "")[:6])
            if not r:
                return None
            return {"代码": (code or "")[:6], "名称": r.get("f14"),
                    "竞价涨跌幅_pct": num(r.get("f3")),
                    "竞价成交额_亿": scale(r.get("f6"), 1e8, 4),
                    "量比": num(r.get("f10"))}

        names = ((state.get("连板天梯") or {}).get("连板股名单")) or []
        out["前一日连板股竞价表现"] = [
            dict(quote_of(n.get("代码")), 连板数=n.get("连板数"), 名称=n.get("名称"))
            for n in names if quote_of(n.get("代码"))]
        brk = state.get("炸板池") or []
        out["前一日炸板股竞价表现"] = [quote_of(b.get("代码")) for b in brk
                                       if quote_of(b.get("代码"))]
        out["state_source"] = sdir
    return out, None


# ===== [SEC-16] 盘中：指数 / 情绪 / 资金 / 板块 =====


def fetch_indices_volume(ctx):
    out = {}
    idx, err = hk_index_snapshot(ctx, INDEX_THSCODES)
    out["指数_同花顺"] = idx
    out["指数_同花顺_error"] = err
    out["指数_东财"] = em_index_snapshot(ctx, list(EM_INDEX_SECIDS.values()))
    rows = em_scan_all(ctx)
    if rows:
        def amt_of(prefixes):
            return sum(_f(r.get("f6")) for r in rows
                       if str(r.get("f12") or "").startswith(prefixes))
        sh, sz = amt_of(("6",)), amt_of(("0", "3"))
        # 北交所：43/83/87/88/92 开头（口径已在 notes 说明）
        bj = amt_of(("43", "83", "87", "88", "92"))
        out["两市成交额"] = {"沪市_亿": round(sh / 1e8, 2), "深市_亿": round(sz / 1e8, 2),
                             "北交所_亿": round(bj / 1e8, 2),
                             "合计_亿": round((sh + sz + bj) / 1e8, 2)}
    intraday = {}
    for sym, name in TX_MIN_SYMBOLS:
        rows_m, last, avg, e = fetch_tx_minute(ctx, sym)
        if not rows_m:
            intraday[name] = {"error": e}
            continue
        intraday[name] = {"最新价": last, "分时均价": avg,
                          "现价相对均价_pct": (round((last - avg) / avg * 100.0, 4)
                                             if last and avg else None),
                          "分钟数": len(rows_m), "分钟序列_尾部30": rows_m[-30:]}
    out["分时均价线"] = intraday
    return out


def fetch_sentiment(ctx):
    rows = em_scan_all(ctx)
    if not rows:
        return None, "东财全市场扫描失败"
    st = breadth_stats(rows)
    up_pool, up_err = hk_pool(ctx, "up", size=200, max_pages=6)
    down_pool, down_err = hk_pool(ctx, "down", size=200, max_pages=6)
    brk_pool, brk_err = hk_pool(ctx, "break", size=200, max_pages=6)
    up_pack = pack_pool(up_pool, "up")
    down_pack = pack_pool(down_pool, "down")
    brk_pack = pack_pool(brk_pool, "break")
    lu, ld, brk = len(up_pack), len(down_pack), len(brk_pack)
    cross = {"东财_涨停家数_阈值口径": st.get("涨停家数_阈值口径"),
             "同花顺_涨停家数": (lu if not up_err else None),
             "东财_跌停家数_阈值口径": st.get("跌停家数_阈值口径"),
             "同花顺_跌停家数": (ld if not down_err else None),
             "同花顺_涨停池口径_含ST": (sum(1 for x in up_pack if x.get("是否ST"))
                                     if not up_err else None),
             "同花顺_炸板家数": (brk if not brk_err else None),
             "炸板率_pct": (round(brk * 100.0 / (lu + brk), 2) if (lu + brk) else None)}
    conflicts = []
    a, b = st.get("涨停家数_阈值口径"), cross.get("同花顺_涨停家数")
    if a and b and abs(a - b) / max(a, b) * 100 > TAG_RULES["conflict_pct"] * 5:
        conflicts.append("涨停家数 双源差异 东财阈值=%s vs 同花顺=%s" % (a, b))
    day_row, win, lad_err = hk_ladder(ctx)
    return {"广度": st, "双源交叉": cross, "双源冲突提示": conflicts or None,
            "涨停池": up_pack, "跌停池": down_pack, "炸板池": brk_pack,
            "连板梯队": (ladder_stats(day_row) if day_row else None),
            "个股人气榜": top_lists(rows, ctx.top),
            "errors": {"涨停池": up_err, "跌停池": down_err, "炸板池": brk_err,
                       "连板天梯": lad_err}}, None


def fetch_funds(ctx, target):
    out = {}
    day = target.strftime("%Y-%m-%d")
    out["北向资金"] = {"status": "NOT_DISCLOSED", "value": None, "note": NORTH_NOTE}
    rows, err = em_dc_rows(ctx, "RPT_MUTUAL_DEAL_HISTORY", page_size=50,
                           filter_="(TRADE_DATE='%s')" % day, sort="TRADE_DATE",
                           sort_type="-1")
    if rows is None:
        out["南向资金"] = {"date": day, "error": err}
    else:
        m = {str(r.get("MUTUAL_TYPE")): r for r in rows}

        def g(t, k):
            return num((m.get(t) or {}).get(k))

        # 单位推导：006 = 002 + 004（南向合计 = 沪市港股通 + 深市港股通）；
        # 用 2023 年北向（005 = 001 + 003）真实量级校准 → 原始单位为「百万元」，
        # 故 亿元 = 原值 / 100。原值一并保留，便于自行复核。
        def yi(t, k):
            v = g(t, k)
            return None if v is None else round(v / 100.0, 4)

        out["南向资金"] = {
            "日期": day,
            "南向合计_净买入_亿": yi("006", "NET_DEAL_AMT"),
            "沪市港股通_净买入_亿": yi("002", "NET_DEAL_AMT"),
            "深市港股通_净买入_亿": yi("004", "NET_DEAL_AMT"),
            "南向合计_买入_亿": yi("006", "BUY_AMT"),
            "南向合计_卖出_亿": yi("006", "SELL_AMT"),
            "北向合计_净买入_亿": yi("005", "NET_DEAL_AMT"),
            "原始值_百万元": {t: {"NET_DEAL_AMT": g(t, "NET_DEAL_AMT"),
                                 "BUY_AMT": g(t, "BUY_AMT"),
                                 "SELL_AMT": g(t, "SELL_AMT")} for t in sorted(m)},
            "单位说明": "东财 RPT_MUTUAL_DEAL_HISTORY 原始单位为百万元（亿元=原值/100），"
                        "由 006=002+004、005=001+003 的恒等式与历史量级校准得出；"
                        "北向（001/003/005）自 2024-08 起为空。"}
    kamt = _em_json(ctx, EM_KAMT_PATH, {"fields1": "f1,f2,f3,f4",
                                        "fields2": "f51,f52,f54,f56"})
    if kamt:
        out["沪深港通额度"] = {
            "口径": "该接口返回的是「额度余额」而非净流入（实测 dayNetAmtIn 恒等于日额度；"
                    "北向 hk2sh/hk2sz 自 2024-08 起为 0，status=3 表示关闭）。",
            "数据": {k: {"状态码": v.get("status"), "余额": num(v.get("dayNetAmtIn")),
                        "日额度": num(v.get("dayAmtThreshold")), "日期": v.get("date2")}
                    for k, v in (kamt.get("data") or {}).items() if isinstance(v, dict)},
            "单位": "万元（4200000 万元 = 420 亿元日额度）"}
    rows_all = em_scan_all(ctx)
    if rows_all:
        mf = sum(_f(r.get("f62")) for r in rows_all if num(r.get("f62")) is not None)
        out["主力资金_两市净流入_亿"] = round(mf / 1e8, 2)
    ind = em_boards(ctx, EM_FS_INDUSTRY, top=100)
    if ind:
        good = [r for r in ind if num(r.get("f62")) is not None]
        good.sort(key=lambda r: num(r.get("f62")), reverse=True)
        pk = lambda r: {"名称": r.get("f14"), "主力净流入_亿": scale(r.get("f62"), 1e8, 4),
                        "涨跌幅_pct": num(r.get("f3"))}
        out["行业主力资金"] = {"净流入TOP10": [pk(r) for r in good[:10]],
                               "净流出TOP10": [pk(r) for r in good[-10:]][::-1]}
    rz, err3 = em_dc_rows(ctx, "RPTA_RZRQ_LSHJ", page_size=10, sort="DIM_DATE",
                          sort_type="-1")
    if rz is None:
        out["两融"] = {"error": err3}
    else:
        latest = next((r for r in rz if str(r.get("DIM_DATE") or "")[:10] <= day), None)
        out["两融"] = None if not latest else {
            "日期": str(latest.get("DIM_DATE") or "")[:10],
            "融资余额_亿": scale(latest.get("RZYE"), 1e8, 2),
            "融资买入额_亿": scale(latest.get("RZMRE"), 1e8, 2),
            "融券余额_亿": scale(latest.get("RQYE"), 1e8, 2),
            "融券余量": scale(latest.get("RQYL"), 1e4, 2),
            "原始字段": {k: v for k, v in latest.items()
                       if not isinstance(v, (list, dict))}}
    return out, None


def fetch_sectors(ctx, target):
    out = {}
    ind, e1 = board_summary(ctx, EM_FS_INDUSTRY, top=10)
    con, e2 = board_summary(ctx, EM_FS_CONCEPT, top=10)
    out["行业板块"] = ind
    out["行业板块_error"] = e1
    out["概念板块"] = con
    out["概念板块_error"] = e2
    out["口径说明"] = SEWAN_NOTE
    struct = {}
    for label, pack in (("行业", ind), ("概念", con)):
        for b in (pack or {}).get("领涨", [])[:5]:
            if not b.get("代码"):
                continue
            s, e = board_structure(ctx, b["代码"])
            if s:
                s["板块名称"] = b.get("名称")
                struct["%s:%s" % (label, b.get("名称"))] = s
            time.sleep(0.15)
    out["板块内部结构"] = struct or None
    prev = prev_trading_day(ctx, target)
    state = read_json(os.path.join(state_dir(ctx, prev), "state.json"))
    if state and state.get("领涨板块"):
        today_leaders = {b.get("名称") for b in (ind or {}).get("领涨", [])[:5]}
        prev_leaders = set(state["领涨板块"])
        inter = today_leaders & prev_leaders
        out["板块轮动速度"] = {"前一日": sorted(prev_leaders), "今日": sorted(today_leaders),
                               "重合数": len(inter),
                               "重合度_pct": round(len(inter) * 100.0 /
                                                max(1, len(today_leaders)), 2)}
    else:
        out["板块轮动速度"] = None
        out["板块轮动速度_note"] = "缺少前一日 state，无法计算重合度"
    return out, None


# ===== [SEC-17] 盘后：个股复盘 / 次日前瞻 =====


def read_pool(path):
    """持仓/自选文件 → token 列表（自动识别「证券代码」列，兼容多列/表格）。"""
    if not os.path.exists(path):
        return [], "文件不存在: %s" % path
    try:
        with io.open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError as e:
        return [], "读取失败: %s" % e
    tokens, code_col = [], None
    for line in lines:
        raw = line.strip().strip("|").strip()
        if not raw:
            continue
        cells = [c.strip() for c in re.split(r"[\t|]", raw) if c.strip()]
        if not cells:
            continue
        if code_col is None:
            hit = None
            for i, c in enumerate(cells):
                if "证券代码" in c or c in ("代码", "股票代码", "ticker"):
                    hit = i
                    break
            if hit is not None:
                code_col = hit
                continue
            if any("证券名称" in c or c in ("名称", "股票名称") for c in cells):
                continue
        if code_col is not None and len(cells) > code_col:
            code = re.sub(r"\D", "", cells[code_col])
            if len(code) == 6:
                tokens.append(code)
                continue
        m = re.search(r"(?<!\d)(\d{6})(?!\d)", raw)
        if m:
            tokens.append(m.group(1))
    seen, uniq = set(), []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    if not uniq:
        return [], "未从文件中解析出任何标的"
    return uniq, None


def resolve_thscode(ctx, token):
    """名称/裸代码 → thscode。同一代码可能同时命中「场外基金(.OF)」与「A股/ETF」，
    此时优先交易所上市的品种（a-share / fund-etf / fund-lof / fund-reits），
    只有仍然并列时才报错不猜；名称必须精确匹配。"""
    t = (token or "").strip()
    if re.fullmatch(r"\d{6}\.[A-Za-z]{2}", t):
        return t, None
    r = invoke(ctx, ["symbol", "search"], ["--q", t, "--limit", "10"])
    if not r["ok"]:
        return None, str(r.get("error"))[:160]
    rows = hk_rows(r["payload"], "thscode")
    EXCHANGE_TRADED = ("a-share", "a-share-index", "fund-etf", "fund-lof", "fund-reits")

    def prefer(cands):
        listed = [x for x in cands if str(x.get("asset_type") or "") in EXCHANGE_TRADED]
        return listed or cands

    exact = [x for x in rows if str(x.get("thscode") or "") == t]
    if exact:
        return exact[0]["thscode"], None
    if re.fullmatch(r"\d{6}", t):
        hit = [x for x in rows if str(x.get("thscode") or "").startswith(t + ".")]
        if not hit:
            return None, "代码 %s 未找到标的" % t
        picked = prefer(hit)
        if len(picked) == 1:
            return picked[0]["thscode"], None
        return None, "代码 %s 对应多个标的: %s" % (
            t, ",".join("%s(%s)" % (x.get("thscode"), x.get("asset_type")) for x in picked))
    named = [x for x in rows if str(x.get("name") or "") == t]
    if named:
        picked = prefer(named)
        if len(picked) == 1:
            return picked[0]["thscode"], None
    if len(named) > 1:
        return None, "名称 %s 对应多个标的: %s" % (
            t, ",".join("%s(%s)" % (x.get("thscode"), x.get("asset_type")) for x in named))
    return None, "未解析出唯一标的: %s" % t


def _em_secid_of(thscode):
    """thscode → 东财 secid（.SH→1.，.SZ/.BJ→0.，.OF 无行情）。"""
    parts = (thscode or "").split(".")
    if len(parts) != 2:
        return None
    base, suf = parts[0], parts[1].upper()
    if suf == "SH":
        return "1." + base
    if suf in ("SZ", "BJ"):
        return "0." + base
    return None


def em_quote_detail(ctx, thscode):
    """东财单标的行情兜底（ETF/指数等不在 A 股扫描内的品种）。"""
    secid = _em_secid_of(thscode)
    if not secid:
        return None, "无东财 secid（场外基金无行情）"
    q = em_quote(ctx, secid,
                 "f43,f44,f45,f46,f47,f48,f50,f57,f58,f60,f168,f169,f170,f184")
    if not q:
        return None, "东财行情返回空"
    return {"代码": thscode, "名称": q.get("f58"),
            "最新价": num(q.get("f43")), "涨跌幅_pct": num(q.get("f170")),
            "涨跌额": num(q.get("f169")), "今开": num(q.get("f46")),
            "最高": num(q.get("f44")), "最低": num(q.get("f45")),
            "昨收": num(q.get("f60")), "成交量": num(q.get("f47")),
            "成交额_亿": scale(q.get("f48"), 1e8, 4),
            "换手率_pct": num(q.get("f168")), "量比": num(q.get("f50")),
            "main_net_pct": num(q.get("f184")),
            "数据源": "东财单标的行情（不在A股全市场扫描内）"}, None


def fetch_pool_review(ctx):
    """个股复盘：--pool 标的的涨跌幅/量比/换手/资金流 + 全市场 TOP 榜。"""
    if not ctx.pool_path:
        return None, "未提供 --pool，个股复盘子块标 N/A（不是失败）", True
    tokens, err = read_pool(ctx.pool_path)
    if err:
        return None, err, False
    out = {"标的数": len(tokens), "个股": []}
    rows = em_scan_all(ctx)
    row_map = {str(r.get("f12")): r for r in rows}
    for tk in tokens:
        code, e = resolve_thscode(ctx, tk)
        if not code:
            out["个股"].append({"输入": tk, "error": e})
            continue
        r = row_map.get(code[:6])
        if r:
            out["个股"].append({
                "输入": tk, "代码": code, "名称": r.get("f14"),
                "涨跌幅_pct": num(r.get("f3")), "最新价": num(r.get("f2")),
                "量比": num(r.get("f10")), "换手率_pct": num(r.get("f8")),
                "振幅_pct": num(r.get("f7")), "成交额_亿": scale(r.get("f6"), 1e8, 4),
                "主力净流入_亿": scale(r.get("f62"), 1e8, 4),
                "主力净占比_pct": num(r.get("f184"))})
        else:
            q, qe = em_quote_detail(ctx, code)
            if q:
                q["输入"] = tk
                out["个股"].append(q)
            else:
                out["个股"].append({"输入": tk, "代码": code,
                                    "error": "不在全市场扫描内且东财兜底失败: %s" % qe})
        time.sleep(0.1)
    if rows:
        out.update(top_lists(rows, ctx.top))
    return out, None, False


def fetch_next_day(ctx, target, items):
    """次日前瞻：次日新股/解禁 + 次日事件 + 晚间公告。"""
    days = fetch_calendar(ctx)
    cands = [d for d in (parse_date(x) for x in days) if d and d > target]
    if cands:
        nxt, estimated = min(cands), False
    else:
        # 交易日历窗口到今日为止（实测），没有未来日期 → 用「跳过周末」估算并标注
        nxt = target + datetime.timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += datetime.timedelta(days=1)
        estimated = True
    out = {"次日": nxt.strftime("%Y-%m-%d"), "次日是否来自交易日历": not estimated}
    if estimated:
        out["次日估算说明"] = ("交易日历未含未来日期，次日按「跳过周末的下一自然日」估算，"
                               "未排除法定节假日，请以交易所公告为准。")
    rows, err = em_dc_rows(ctx, "RPTA_APP_IPOAPPLY", page_size=100, sort="APPLY_DATE",
                           sort_type="1")
    if rows is not None:
        nd = nxt.strftime("%Y-%m-%d")
        out["新股_申购"] = [{"代码": r.get("SECURITY_CODE"),
                             "申购日": str(r.get("APPLY_DATE") or "")[:10],
                             "发行价": num(r.get("ISSUE_PRICE"))}
                            for r in rows if str(r.get("APPLY_DATE") or "")[:10] == nd]
        out["新股_上市"] = [{"代码": r.get("SECURITY_CODE"),
                             "上市日": str(r.get("LISTING_DATE") or "")[:10]}
                            for r in rows if str(r.get("LISTING_DATE") or "")[:10] == nd]
    else:
        out["新股_error"] = err
    nd = nxt.strftime("%Y-%m-%d")
    rows2, err2 = em_dc_rows(ctx, "RPT_LIFT_STAGE", page_size=200, sort="FREE_DATE",
                             sort_type="1",
                             filter_="(FREE_DATE>='%s')(FREE_DATE<='%s')" % (nd, nd))
    if rows2 is not None:
        items2 = [{"代码": r.get("SECURITY_CODE"), "名称": r.get("SECURITY_NAME_ABBR"),
                   "解禁市值_亿": _cap(r.get("LIFT_MARKET_CAP"), 1e4, 4),
                   "解禁股数_万股": num(r.get("CURRENT_FREE_SHARES")),
                   "解禁类型": r.get("FREE_SHARES_TYPE")} for r in rows2]
        items2.sort(key=lambda x: (x.get("解禁市值_亿") or 0), reverse=True)
        out["限售解禁"] = {
            "解禁总市值_亿": round(sum(_f(x.get("解禁市值_亿")) for x in items2), 4),
            "个股数": len(items2), "名单": items2[:ctx.top]}
    else:
        out["限售解禁_error"] = err2
    out["次日事件"] = match_news(
        [it for it in items if any(k in (it.get("title", "") + it.get("content", ""))
                                   for k in MACRO_CALENDAR)], MACRO_CALENDAR, ctx.top)
    out["晚间公告"] = fetch_cninfo_market(ctx, target, page_size=50)[:ctx.top * 2]
    out["note"] = "晚间公告为巨潮当日公告按时间倒序前 N 条（市场级，非全量）。"
    return out, None


# ===== [SEC-18] 组装 =====


def build_base(ctx, phase):
    future = ctx.target > datetime.date.today()
    return {
        "schema_version": SCHEMA_VERSION,
        "phase": phase,
        "generated_at": now_local().strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "trade_date": ctx.target.strftime("%Y-%m-%d"),
        "is_trading_day": is_trading_day(ctx, ctx.target),
        "trading_day_verified": (not future),
        "trading_day_note": ("未来日期：交易日历窗口只覆盖到今日，is_trading_day 仅为"
                             "「周一至周五」粗判，未经节假日校验。" if future else None),
        "session": session_name(),
        "sources": {},
        "data": {},
        "degrade": [],
        "notes": [],
    }


def put(ctx, rep, block, value, status="OK", detail=""):
    rep["data"][block] = value
    rep["sources"][block] = {"status": status, "detail": detail or None}
    ctx.mark(block, status, detail)


def time_point_note():
    """关键时点观察窗标注（纯事实：当前时刻落在哪个窗口）。"""
    now = now_local()
    hm = now.hour * 60 + now.minute
    windows = (("开盘定调期", 9 * 60 + 30, 10 * 60),
               ("趋势验证期", 10 * 60, 11 * 60 + 30),
               ("午后验证期", 13 * 60, 14 * 60 + 30),
               ("尾盘定调期", 14 * 60 + 30, 15 * 60))
    cur = [n for n, a, b in windows if a <= hm <= b]
    return {"采集时刻": now.strftime("%H:%M:%S"), "当前观察窗": cur or ["非关键时点"],
            "观察窗定义": {n: "%02d:%02d-%02d:%02d" % (a // 60, a % 60, b // 60, b % 60)
                          for n, a, b in windows},
            "note": "跨时点对比需同日多次运行 live：读取当日已落盘快照做差值，"
                    "单次运行只给当下事实。"}


def run_prep(ctx, rep, pool):
    say("[..] prep: overseas / macro / supply / news / auction")
    try:
        v, e = fetch_overseas(ctx, ctx.target)
        put(ctx, rep, "overseas", v, "OK" if v else "FAIL", e or "")
    except Exception as ex:
        put(ctx, rep, "overseas", None, "FAIL", "异常: %s" % ex)
    try:
        v, e = fetch_shibor(ctx, ctx.target)
        put(ctx, rep, "shibor", v, "OK" if v else "FAIL", e or "")
    except Exception as ex:
        put(ctx, rep, "shibor", None, "FAIL", "异常: %s" % ex)
    try:
        v, e = fetch_dr007(ctx, ctx.target)
        put(ctx, rep, "dr007", v, "OK" if v else "DEGRADE", e or "")
    except Exception as ex:
        put(ctx, rep, "dr007", None, "FAIL", "异常: %s" % ex)
    items = flatten_news(pool)
    try:
        v = fetch_policy_calendar(ctx, items, ctx.target, ctx.top)
        ok = bool(v.get("顶层政策") or v.get("行业政策") or v.get("宏观日历_数据发布"))
        put(ctx, rep, "policy_calendar", v, "DEGRADE" if ok else "NA",
            "快讯关键词派生口径（非官方日历接口）")
    except Exception as ex:
        put(ctx, rep, "policy_calendar", None, "FAIL", "异常: %s" % ex)
    try:
        v = fetch_supply(ctx, ctx.target, ctx.top)
        bad = [k for k, x in v.items() if isinstance(x, dict) and x.get("error")]
        put(ctx, rep, "supply", v, "DEGRADE" if bad else "OK",
            ("部分子块失败: %s" % ",".join(bad)) if bad else "")
    except Exception as ex:
        put(ctx, rep, "supply", None, "FAIL", "异常: %s" % ex)
    try:
        prev = prev_trading_day(ctx, ctx.target)
        lhb, e = hk_dragon_tiger(ctx, "all", prev.strftime("%Y-%m-%d"))
        layers = {
            "顶层政策": match_news(items, POLICY_TOP, ctx.top * 2),
            "行业政策": match_news(items, POLICY_INDUSTRY, ctx.top * 2),
            "宏观数据": match_news(items, MACRO_CALENDAR, ctx.top * 2),
            "个股公告": fetch_cninfo_market(ctx, prev, page_size=30)[:ctx.top],
            "前一日龙虎榜": (pack_dragon_tiger(lhb, 60) if lhb is not None else None),
            "前一日龙虎榜_error": e,
            "快讯源条数": {k: len(v) for k, v in pool.items()},
        }
        put(ctx, rep, "news_layers", layers, "OK" if items else "FAIL")
    except Exception as ex:
        put(ctx, rep, "news_layers", None, "FAIL", "异常: %s" % ex)
    try:
        v, e = fetch_auction(ctx, ctx.target)
        put(ctx, rep, "auction", v, "OK" if v else "FAIL", e or "")
    except Exception as ex:
        put(ctx, rep, "auction", None, "FAIL", "异常: %s" % ex)


def run_live(ctx, rep, pool):
    say("[..] live: indices / sentiment / funds / sectors")
    ctx._all_rows = None                       # 盘中每次运行重扫，避免复用缓存
    try:
        put(ctx, rep, "indices_volume", fetch_indices_volume(ctx), "OK")
    except Exception as ex:
        put(ctx, rep, "indices_volume", None, "FAIL", "异常: %s" % ex)
    try:
        v, e = fetch_sentiment(ctx)
        put(ctx, rep, "sentiment", v, "OK" if v else "FAIL", e or "")
    except Exception as ex:
        put(ctx, rep, "sentiment", None, "FAIL", "异常: %s" % ex)
    try:
        v, e = fetch_funds(ctx, ctx.target)
        put(ctx, rep, "funds", v, "OK" if v else "FAIL", e or "")
    except Exception as ex:
        put(ctx, rep, "funds", None, "FAIL", "异常: %s" % ex)
    try:
        v, e = fetch_sectors(ctx, ctx.target)
        put(ctx, rep, "sectors", v, "OK" if v else "FAIL", e or "")
    except Exception as ex:
        put(ctx, rep, "sectors", None, "FAIL", "异常: %s" % ex)
    rep["data"]["关键时点"] = time_point_note()
    ctx.mark("关键时点", "OK")


def save_state(ctx, rep):
    """把当日 today-only 特色数据落 state，供次日盘前引用。"""
    sent = (rep["data"].get("sentiment") or {})
    sec = (rep["data"].get("sectors") or {})
    state = {
        "date": ctx.target.strftime("%Y-%m-%d"),
        "连板天梯": sent.get("连板梯队"), "涨停池": sent.get("涨停池"),
        "跌停池": sent.get("跌停池"), "炸板池": sent.get("炸板池"),
        "领涨板块": [b.get("名称") for b in ((sec.get("行业板块") or {}).get("领涨") or [])[:5]],
        "领涨概念": [b.get("名称") for b in ((sec.get("概念板块") or {}).get("领涨") or [])[:5]],
        "广度": sent.get("广度"),
        "saved_at": now_local().strftime("%Y-%m-%dT%H:%M:%S"),
    }
    path = os.path.join(state_dir(ctx, ctx.target), "state.json")
    write_json(path, state)
    return path


def run_post(ctx, rep, pool):
    say("[..] post: review / next-day")
    ctx._all_rows = None
    try:
        put(ctx, rep, "indices_volume", fetch_indices_volume(ctx), "OK")
    except Exception as ex:
        put(ctx, rep, "indices_volume", None, "FAIL", "异常: %s" % ex)
    try:
        v, e = fetch_sentiment(ctx)
        put(ctx, rep, "sentiment", v, "OK" if v else "FAIL", e or "")
    except Exception as ex:
        put(ctx, rep, "sentiment", None, "FAIL", "异常: %s" % ex)
    try:
        v, e = fetch_funds(ctx, ctx.target)
        put(ctx, rep, "funds", v, "OK" if v else "FAIL", e or "")
    except Exception as ex:
        put(ctx, rep, "funds", None, "FAIL", "异常: %s" % ex)
    try:
        v, e = fetch_sectors(ctx, ctx.target)
        put(ctx, rep, "sectors", v, "OK" if v else "FAIL", e or "")
    except Exception as ex:
        put(ctx, rep, "sectors", None, "FAIL", "异常: %s" % ex)
    try:
        out = {}
        for label, dt in (("当日", ctx.target), ("前一日", prev_trading_day(ctx, ctx.target))):
            ds = dt.strftime("%Y-%m-%d")
            rows, e = hk_dragon_tiger(ctx, "all", ds)
            org, e2 = hk_dragon_tiger(ctx, "org", ds)
            hmoney, e3 = hk_dragon_tiger(ctx, "hot_money", ds)
            out[label] = {
                "榜单": (pack_dragon_tiger(rows, 80) if rows is not None else None),
                "机构席位": (pack_dragon_tiger(org, 40) if org is not None else None),
                "游资席位": (pack_dragon_tiger(hmoney, 40) if hmoney is not None else None),
                "error": e or e2 or e3}
        put(ctx, rep, "dragon_tiger", out, "OK")
    except Exception as ex:
        put(ctx, rep, "dragon_tiger", None, "FAIL", "异常: %s" % ex)
    try:
        rows, e = em_dc_rows(ctx, "RPT_DAILYBILLBOARD_DETAILSNEW", page_size=200,
                             filter_="(TRADE_DATE='%s')" % ctx.target.strftime("%Y-%m-%d"),
                             sort="BILLBOARD_NET_AMT", sort_type="-1")
        if rows is None:
            put(ctx, rep, "dragon_tiger_em", None, "FAIL", e or "")
        else:
            pack = [{"代码": r.get("SECURITY_CODE"), "名称": r.get("SECURITY_NAME_ABBR"),
                     "净买_亿": _cap(r.get("BILLBOARD_NET_AMT"), 1e8, 4),
                     "买入_亿": _cap(r.get("BILLBOARD_BUY_AMT"), 1e8, 4),
                     "卖出_亿": _cap(r.get("BILLBOARD_SELL_AMT"), 1e8, 4),
                     "涨跌幅_pct": num(r.get("CHANGE_RATE")),
                     "上榜原因": str(r.get("EXPLANATION") or "")[:40]} for r in rows]
            put(ctx, rep, "dragon_tiger_em", pack, "OK", "%d 条" % len(pack))
    except Exception as ex:
        put(ctx, rep, "dragon_tiger_em", None, "FAIL", "异常: %s" % ex)
    try:
        v, e, na = fetch_pool_review(ctx)
        put(ctx, rep, "stock_review", v, "NA" if na else ("OK" if v else "FAIL"), e or "")
    except Exception as ex:
        put(ctx, rep, "stock_review", None, "FAIL", "异常: %s" % ex)
    try:
        v, e = fetch_next_day(ctx, ctx.target, flatten_news(pool))
        put(ctx, rep, "next_day", v, "OK" if v else "FAIL", e or "")
    except Exception as ex:
        put(ctx, rep, "next_day", None, "FAIL", "异常: %s" % ex)
    try:
        v, e = fetch_overseas(ctx, ctx.target)
        put(ctx, rep, "overseas_evening", v, "OK" if v else "FAIL", e or "")
    except Exception as ex:
        put(ctx, rep, "overseas_evening", None, "FAIL", "异常: %s" % ex)
    try:
        path = save_state(ctx, rep)
        rep["data"]["state_saved"] = path
        ctx.mark("state", "OK", os.path.basename(os.path.dirname(path)) + "/state.json")
    except Exception as ex:
        ctx.mark("state", "FAIL", "异常: %s" % ex)


def assemble_notes(ctx, rep):
    rep["notes"].append(NORTH_NOTE)
    rep["notes"].append(SEWAN_NOTE)
    rep["notes"].append("全部输出为中性事实与数值，不含买卖建议，不构成投资建议。")
    rep["notes"].append(
        "涨停/跌停的「阈值口径」= 按板块与 ST 规则（10%/20%/30%/5%）对全市场涨跌幅做阈值"
        "判定，与同花顺官方涨跌停池口径会有差异；两者已并列，不自动裁决。")
    rep["notes"].append(
        "两市成交额按代码前缀粗分沪/深/北（6=沪，0/3=深，4/8=北交所），"
        "与交易所口径可能有极小差异。")
    if not ctx.pool_path:
        rep["notes"].append("未提供 --pool，个股复盘子块标 N/A（不是失败）。")
    rep["degrade"] = sorted(set(ctx.degrade))
    return rep


# ===== [SEC-19] CLI =====


USAGE_SAMPLES = """\
示例:
  python pan.py prep                        盘前（外围/汇率利率/大宗/宏观/供给/消息/竞价）
  python pan.py live                        盘中单次快照
  python pan.py post                        盘后复盘 + 次日前瞻
  python pan.py all                         三段合并
  python pan.py post --pool 持仓数据.md       附加个股复盘
  python pan.py prep --date 2026-09-11      指定交易日
  python pan.py post --em-host https://127.0.0.1:1   东财降级演练

落盘: data/pan/<YYYYMMDD>/<HHMMSS>_<phase>.json  +  latest_<phase>.json
"""


def _add_common(p):
    p.add_argument("--date", help="目标交易日 YYYY-MM-DD（默认今天，非交易日回退最近交易日）")
    p.add_argument("--out", metavar="DIR", help="落盘基目录（默认 <脚本目录>/data/pan）")
    p.add_argument("--pool", metavar="FILE", help="持仓/自选文件（个股复盘用）")
    p.add_argument("--top", type=int, default=20, help="各榜单保留条数（默认 20）")
    p.add_argument("--em-host", action="append", metavar="URL",
                   help="覆盖东财 host（可重复；降级演练用）")
    p.add_argument("--no-cache", action="store_true", help="忽略本地缓存（交易日历）")
    p.add_argument("--debug", action="store_true", help="打印调试信息")


def build_parser():
    p = argparse.ArgumentParser(
        prog="pan.py", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="A股盘前/盘中/盘后全流程数据抓取（中性事实与数值，不含买卖建议）。",
        epilog=USAGE_SAMPLES)
    sub = p.add_subparsers(dest="cmd")
    for name, help_txt in (("prep", "盘前：外围/汇率利率/大宗/宏观/供给/消息/竞价"),
                           ("live", "盘中单次快照：指数量能/情绪/资金/板块"),
                           ("post", "盘后复盘 + 次日前瞻"),
                           ("all", "三段合并")):
        s = sub.add_parser(name, help=help_txt, epilog=USAGE_SAMPLES,
                           formatter_class=argparse.RawDescriptionHelpFormatter)
        _add_common(s)
    return p


SUBCMDS = ("prep", "live", "post", "all")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        argv = ["--help"]
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 0
    base = args.out or BASE_DEFAULT
    want = parse_date(args.date) if args.date else None
    if args.date and want is None:
        say("[FAIL] --date 需为 YYYY-MM-DD")
        return 2
    ctx = Ctx(base, want or datetime.date.today(), args.cmd)
    ctx.debug = bool(args.debug)
    ctx.no_cache = bool(args.no_cache)
    ctx.top = max(1, int(args.top or 20))
    ctx.pool_path = args.pool
    if args.em_host:
        ctx.em_hosts = list(args.em_host)
    try:
        if not probe_cli(ctx):
            return 1
        target, rolled, note = resolve_target_date(ctx, ctx.target)
        ctx.target = target
        say("[OK] run: phase=%s target=%s session=%s" % (
            ctx.phase, ctx.target.strftime("%Y-%m-%d"), session_name()))
        if note:
            say("[NOTE] " + note)
        pool, pool_fails, ok = collect_news_pool(ctx)
        if not ok:
            say("[FAIL] news-pool: 全部快讯源失败，政策/消息层将只有公告与官方端点")
        rep = build_base(ctx, ctx.phase)
        if ctx.phase in ("prep", "all"):
            run_prep(ctx, rep, pool)
        if ctx.phase in ("live", "all"):
            run_live(ctx, rep, pool)
        if ctx.phase in ("post", "all"):
            run_post(ctx, rep, pool)
        if note:
            rep["notes"].append(note)
        rep = assemble_notes(ctx, rep)
        rep["meta"] = {"node_calls": ctx.calls, "http_calls": ctx.http_calls,
                       "pool_sizes": {k: len(v) for k, v in pool.items()},
                       "pool_fails": {k: v for k, v in pool_fails.items() if v}}
        path, latest = save_snapshot(ctx, rep)
        say("[OK] wrote: %s" % os.path.basename(path))
        say("[OK] latest: %s" % os.path.basename(latest))
        fails = [k for k, v in rep["sources"].items() if v.get("status") == "FAIL"]
        say("[SUM] blocks=%d ok=%d degrade=%d fail=%d na=%d" % (
            len(rep["sources"]),
            sum(1 for v in rep["sources"].values() if v.get("status") == "OK"),
            sum(1 for v in rep["sources"].values() if v.get("status") == "DEGRADE"),
            len(fails),
            sum(1 for v in rep["sources"].values() if v.get("status") == "NA")))
        if fails:
            say("[SUM] failed blocks: " + ", ".join(sorted(fails)))
        return 0
    except KeyboardInterrupt:
        say("[FAIL] interrupted")
        return 130
    except Exception as ex:
        say("[FAIL] unexpected: %s" % str(ex)[:300])
        if ctx.debug:
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
