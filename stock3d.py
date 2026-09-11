#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""stock3d.py - A股三维数据拉取（技术面 / 基本面 / 消息面）

单文件 CLI，参考 caiwu/scripts/fin.py 的分段架构与踩坑结论，但完全独立、不 import 它。

用法
    python stock3d.py pull 002463.SZ 沪电股份     # 三维全出（默认子命令）
    python stock3d.py pull --pool <持仓文件>      # 从持仓/自选文件读标的
    python stock3d.py tech 002463.SZ              # 只跑技术面
    python stock3d.py fund 002463.SZ              # 只跑基本面
    python stock3d.py news 002463.SZ              # 只跑消息面

铁律（与 fin.py 同源）
  1. stdout 只输出 ASCII 状态行（中文一律落文件）——PowerShell 管道会把中文转 GBK 乱码。
  2. 任何源失败都必须显式落 [FAIL]/[DEGRADE] 标注，严禁用 0 或空数组伪装成「有数据」。
  3. 输出全部为中性事实与数值，不含任何买卖建议、不做综合评分。
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
import urllib.error
import urllib.parse
import urllib.request

try:
    import requests
except ImportError:
    # 惰性依赖：缺失时消息面报 [FAIL] 并给安装提示，其余维度照常可用
    requests = None

try:
    import pandas as pd
except ImportError:
    pd = None

# ===== [SEC-02] 常量 =====

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DEFAULT = os.path.join(SCRIPT_DIR, "data")

NODE_MAIN = (r"C:\Users\m1526\AppData\Roaming\npm\node_modules"
             r"\@hithink-tech\hithink-finance-cli\dist\cli\main.js")

TIMEOUT_S = 180             # 单次 node 调用超时（秒）
RETRY_MAX = 2               # 瞬断重试次数上限
KLINE_BARS = 130            # 目标日K根数（复盘手册近 ~120 根，取 130 富余）
KLINE_LOOKBACK_DAYS = 200   # 限长模式下的回溯自然日数（覆盖 130 个交易日）
SINA_MAX_BARS = 1023        # 新浪 datalen 实测上限（再大报错）
TX_MAX_BARS = 800           # 腾讯实测 800 根稳定（1500 起行为异常）
FIN_QUARTERS = 20           # 季报轨期数（hithink 上限 20；越长越能支撑估值历史分位）
F10_QUARTERS = 12           # 东财 F10 明细节的季报期数（单次 URL 不宜过长）
FIN_YEARS = 5               # 年报轨期数
DEFAULT_TOP = 30            # 每源快讯条数上限
NOTICE_DAYS = 30            # 公告窗口（自然日）。7 天太窄：实测 002463 最近一条公告在
                            # 9 天前，7 天窗口会把近 30 天的 15 条公告全部切掉 → 显示"无公告"。
NOTICE_PAGE_SIZE = 50       # 单次公告条数上限（沪深各请求一次）
BATCH_SLEEP = 1.2           # 多标的之间的间隔（防限流）

KIND_CN = {"stock": "个股", "fund": "基金", "index": "指数"}

# ---- hithink 瞬断特征（命中才重试；参数性错误不重试）----
_RETRYABLE_HITS = ("timeout", "econn", "etimedout", "enotfound", "fetch failed",
                   "socket", "eai_again", "epipe", "network")
_PARAM_HITS = ("usage", "unknown option", "required", "invalid", "missing")

# ---- HTTP 头（实测口径，勿随意改 UA）----
UA_CHROME = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
BASE_HEADERS = {"User-Agent": UA_CHROME, "Accept": "application/json, text/plain, */*"}
_WEB_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept": "*/*"}

# ---- 行情/技术面公开源 ----
EM_UT = "b2884a393a59ad64002292a3e90d46a5"
# 东财 K 线 host 轮换（push2his 族实测会出现 IP 级时段性拒连，多 host 提高命中率）
EM_KLINE_URLS = [
    "https://push2his.eastmoney.com/api/qt/stock/kline/get",
    "https://62.push2his.eastmoney.com/api/qt/stock/kline/get",
]
EM_DETAIL_URLS = [
    "https://push2his.eastmoney.com/api/qt/stock/details/get",
    "https://63.push2his.eastmoney.com/api/qt/stock/details/get",
]
TX_QT_URL = "https://qt.gtimg.cn/q="
TX_MIN_URL = "https://ifzq.gtimg.cn/appstock/app/minute/query"
TX_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
SINA_KLINE_URL = ("https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_x_120_1="
                  "/CN_MarketDataService.getKLineData")
SINA_HQ_URL = "https://hq.sinajs.cn/list="
EM_HEADERS = {**BASE_HEADERS, "Referer": "http://quote.eastmoney.com/"}
# 新浪必须带 Referer: finance.sina.com.cn（否则 403），且响应为 GBK
SINA_REF_HEADERS = {**BASE_HEADERS, "Referer": "https://finance.sina.com.cn/"}
TX_REF_HEADERS = {"Referer": "https://gu.qq.com/"}

# ---- 消息面公开源 ----
CLS_ROLL_URL = "https://www.cls.cn/v1/roll/get_roll_list"       # 真电报流（带签名）
CLS_CACHE_URL = "https://www.cls.cn/api/cache?name=telegraph"   # 零签名兜底（固定20条）
CLS_TG_SSR_URL = "https://m.cls.cn/telegraph"                   # 第三兜底（SSR）
CLS_HOT_URL = "https://m.cls.cn/"
CLS_SIGN_APP = {"app": "CailianpressWeb", "os": "web", "sv": "7.7.5"}
CLS_RN_MAX = 50     # 实测 rn>50 会「errno=0 但数组为空」地静默失败，不可调大
CLS_HEADERS = {**BASE_HEADERS, "Referer": "https://www.cls.cn/"}
WALLSTCN_URLS = ["https://api-prod.wallstreetcn.com/apiv1/content/lives",
                 "https://api-one.wallstcn.com/apiv1/content/lives"]
WALLSTCN_CHANNELS = ("a-stock-channel", "global-channel")   # 国内频道更贴 A 股
WALLSTCN_HEADERS = {**BASE_HEADERS, "Referer": "https://wallstreetcn.com/live/global"}
JIN10_URL = "https://flash-api.jin10.com/get_flash_list"
JIN10_HEADERS = {
    **BASE_HEADERS, "x-app-id": "bVBF4FyRTn5NJF5n", "x-version": "1.0.0",
    "Origin": "https://www.jin10.com", "Referer": "https://www.jin10.com/"}
THS_URL = "https://news.10jqka.com.cn/tapp/news/push/stock/"
THS_HEADERS = {**BASE_HEADERS, "Referer": "https://news.10jqka.com.cn/realtimenews.html"}
SINA_24H_URL = "https://zhibo.sina.com.cn/api/zhibo/feed"
EM_FLASH_URL = "https://np-listapi.eastmoney.com/comm/web/getFastNewsList"
EM_FLASH_COLUMN = "102"     # 7×24 全球直播（101/103/104 是其它栏目，内容不同）
# 东财快讯 req_trace 必填：漏掉会返回「code=0 + 空数组」的静默失败
EM_FLASH_PARAMS = {"client": "web", "biz": "web_724", "fastColumn": EM_FLASH_COLUMN,
                   "sortEnd": "", "pageSize": "50", "req_trace": "1"}
EM_FLASH_HEADERS = {**BASE_HEADERS, "Referer": "https://kuaixun.eastmoney.com/"}

# ---- 公告 / 榜单 ----
CNINFO_QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_TOPSEARCH_URL = "https://www.cninfo.com.cn/new/information/topSearch/query"
CNINFO_HEADERS = {**BASE_HEADERS, "Referer": "https://www.cninfo.com.cn/",
                  "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                  "X-Requested-With": "XMLHttpRequest"}
DC_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
LHB_MAIN_RPT = "RPT_DAILYBILLBOARD_DETAILSNEW"   # 龙虎榜主表（可按 SECURITY_CODE 过滤）
LHB_DAYS = 30                                    # 龙虎榜回溯自然日数
NORTH_NOTE = ("北向资金：沪深港通日度净买入自 2024-08 起停止披露（交易所规则调整），"
              "本层以龙虎榜机构/游资席位与热榜热度替代观察")

# ---- 东财 F10 完整三表（实测资产表 319 / 利润表 203 / 现金流 254 科目）----
EM_F10_BASE = "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/"
EM_F10_HEADERS = {**BASE_HEADERS, "Referer": "https://emweb.securities.eastmoney.com/"}
# 科目白名单（键=接口字段，值=中文名）。取不到的键自动跳过，不会伪造。
F10_BS = [("MONETARYFUNDS", "货币资金"), ("TRADE_FINASSET", "交易性金融资产"),
          ("NOTE_ACCOUNTS_RECE", "应收票据及应收账款"), ("ACCOUNTS_RECE", "应收账款"),
          ("INVENTORY", "存货"), ("FIXED_ASSET", "固定资产"), ("CIP", "在建工程"),
          ("GOODWILL", "商誉"), ("SHORT_LOAN", "短期借款"), ("LONG_LOAN", "长期借款"),
          ("BOND_PAYABLE", "应付债券"), ("NONCURRENT_LIAB_1YEAR", "一年内到期的非流动负债"),
          ("TOTAL_CURRENT_ASSETS", "流动资产合计"), ("TOTAL_CURRENT_LIAB", "流动负债合计"),
          ("TOTAL_ASSETS", "资产总计"), ("TOTAL_LIABILITIES", "负债合计"),
          ("TOTAL_PARENT_EQUITY", "归母股东权益合计")]
F10_IS = [("TOTAL_OPERATE_INCOME", "营业总收入"), ("OPERATE_INCOME", "营业收入"),
          ("OPERATE_COST", "营业成本"), ("TOTAL_OPERATE_COST", "营业总成本"),
          ("SALE_EXPENSE", "销售费用"), ("MANAGE_EXPENSE", "管理费用"),
          ("RESEARCH_EXPENSE", "研发费用"), ("FINANCE_EXPENSE", "财务费用"),
          ("OPERATE_PROFIT", "营业利润"), ("TOTAL_PROFIT", "利润总额"),
          ("INCOME_TAX", "所得税费用"), ("NETPROFIT", "净利润"),
          ("PARENT_NETPROFIT", "归母净利润"),
          ("DEDUCT_PARENT_NETPROFIT", "扣非归母净利润"), ("BASIC_EPS", "基本每股收益")]
F10_CF = [("SALES_SERVICES", "销售商品提供劳务收到的现金"), ("NETCASH_OPERATE", "经营活动现金流净额"),
          ("NETCASH_INVEST", "投资活动现金流净额"), ("NETCASH_FINANCE", "筹资活动现金流净额")]

# ---- 东财 datacenter 报表（2026-09-11 实测可用）----
DC_REPORTS = {
    "holders": "RPT_F10_EH_HOLDERS",                          # 十大股东
    "freeholders": "RPT_F10_EH_FREEHOLDERS",                  # 十大流通股东
    "holdernum": "RPT_F10_EH_HOLDERNUM",                      # 股东户数与集中度
    "orghold": "RPT_MAINDATA_MAIN_POSITIONDETAILS",           # 机构持仓明细
    "north": "RPT_MUTUAL_HOLDSTOCKNORTH_STA",                 # 北向持股
    "pledge": "RPT_CSDC_LIST",                                # 股权质押
    "lift": "RPT_LIFT_STAGE",                                 # 限售解禁
    "predict": "RPT_PUBLIC_OP_NEWPREDICT",                    # 业绩预告
    "mainop": "RPT_F10_FN_MAINOP",                            # 主营构成
    "bonus": "RPT_SHAREBONUS_DET",                            # 分红送转
    "margin": "RPTA_WEB_RZRQ_GGMX",                           # 两融个股明细（用 SCODE 过滤）
    "survey": "RPT_ORG_SURVEYNEW",                            # 机构调研
}
PEER_TOPN = 10            # 可比公司取同行业市值前 N（剔除自己）
PEER_BATCH = 50           # 同行估值批量查询单次上限
CACHE_DIRNAME = "cache"   # 重活缓存目录
SURVEY_DAYS = 30          # 机构调研统计窗口

# ---- 数据源确实拿不到的项：显式标注，绝不静默缺失 ----
UNAVAILABLE = {
    "fund": [
        {"字段": "产能/产量/销量/市场占有率", "原因": "无结构化数据源；偶见于公告正文，无法可靠提取数值"},
        {"字段": "行业景气度（行业PMI/销量增速/价格指数）", "原因": "hithink 与东财均无行业级景气度端点"},
        {"字段": "DCF估值/分部估值", "原因": "需自建估值模型，属分析结论而非可拉取数据"},
        {"字段": "公司行业地位梯队", "原因": "属主观判断，非数据"},
        {"字段": "大股东质押平仓线", "原因": "平仓线不对外披露；仅有质押比例与质押市值（RPT_CSDC_LIST）"},
    ],
    "news": [
        {"字段": "CPI/PPI/PMI/社融等宏观指标", "原因": "stock3d 未接宏观源（caiwu fin.py macro 有，需跨项目复用）"},
        {"字段": "行业高频数据（月销量/产量/库存/开工率）", "原因": "无结构化数据源"},
        {"字段": "地方行业政策结构化数据", "原因": "无结构化数据源，仅能用快讯关键词做弱代理"},
        {"字段": "相关大宗商品期货价格", "原因": "当前未接期货行情源"},
        {"字段": "美股同行业龙头映射", "原因": "个股到美股同业无现成映射表"},
    ],
    "tech": [
        {"字段": "筹码分布/筹码峰/集中度", "原因": "东财 cyq 接口返回 404 且 push2 拒连；改用股东户数集中度作代理"},
        {"字段": "获利盘/套牢盘比例", "原因": "依赖筹码分布，源不可达"},
        {"字段": "市场平均持仓成本", "原因": "依赖筹码分布，源不可达"},
        {"字段": "筹码迁移方向", "原因": "依赖筹码分布历史，源不可达"},
        {"字段": "日线+60分钟多周期共振", "原因": "腾讯 m60 与东财 klt=60 实测均不可达"},
        {"字段": "价格形态命名（底部/顶部/整理）", "原因": "属主观形态识别；本工具只输出事实性价位结构"},
    ],
}

# ---- 中性标注阈值（唯一真源，勿散落）----
TAG_RULES = {
    "bias_alert_pct": 8.0,      # |MA20 乖离| 警示阈值 %
    "vol_ratio_high": 1.5,      # 量比显著放量
    "vol_ratio_mild_high": 1.2,
    "vol_ratio_low": 0.7,       # 量比显著缩量
    "vol_ratio_mild_low": 0.85,
    "pos60_high": 80.0,
    "pos60_low": 20.0,
    "revenue_yoy_high": 20.0,   # 营收同比高增
    "revenue_yoy_down": -10.0,  # 营收同比下滑
    "debt_ratio_high": 60.0,    # 资产负债率偏高
    "debt_ratio_low": 40.0,
    "ocf_quality_good": 1.0,    # 经营现金流/净利润
    "ocf_quality_weak": 0.5,
}

# ===== [SEC-03] 日志与状态行（stdout 只出 ASCII） =====


def say(msg):
    """安全输出到 stdout。
    PowerShell 管道会把 UTF-8 中文按 GBK 解码成乱码（fin.py 同款坑），
    因此这里统一剥离非 ASCII 字符——中文内容一律落文件，不靠控制台传达。"""
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
    """运行上下文：目标日、输出目录、标的记录、成败日志。"""

    def __init__(self, base, target):
        self.base = base
        self.target = target                       # datetime.date
        self.debug = False
        self.no_cache = False
        self.entries = []                          # [{type,code,name,kind,...}]
        self.bad = []                              # 无法解析的 token
        self.resolves = []
        self.status = {}                           # code -> {dim: {"mark","note"}}
        self.failures = []                         # [(dim, code, why)]
        self.calls = 0                             # 实际启动 node 的次数
        self.kline_rows = {}                       # code -> 日K行（维间复用，不落盘）

    @property
    def history_dir(self):
        return os.path.join(self.base, "history")

    def mark(self, code, dim, mark, note="", note_ascii=""):
        self.status.setdefault(code, {})[dim] = {"mark": mark, "note": note}
        if mark in ("FAIL", "DEGRADE"):
            self.failures.append((dim, code, note))
        tag = {"OK": "[OK]", "FAIL": "[FAIL]", "SKIP": "[SKIP]", "DEGRADE": "[DEGRADE]"}.get(mark, "[??]")
        extra = ("  " + (note_ascii or note)) if (note_ascii or note) else ""
        say("%-8s %-6s %-11s%s" % (tag, dim, code, extra))

    def rel(self, path):
        try:
            return os.path.relpath(path, SCRIPT_DIR)
        except ValueError:
            return path


def dbg(ctx, msg):
    if ctx.debug:
        say("[DBG] " + str(msg))


def fail_msg(ctx, dim, code, why):
    """统一失败标注：先打状态行，再落 failure 记录。"""
    ctx.mark(code, dim, "FAIL", why)

# ===== [SEC-04] hithink CLI 调用层（唯一 node 出入口） =====


def _is_retryable(err):
    e = (err or "").lower()
    return any(h in e for h in _RETRYABLE_HITS)


def _is_param_err(err):
    e = (err or "").lower()
    return any(h in e for h in _PARAM_HITS)


def _clean_cli_err(raw):
    """CLI 失败信息 → 短摘要。
    CLI 会把 {'ok':false,'error':{code,message,hint}} 整个 JSON 打到 stderr，
    直接截断会带出一串 JSON 片段（含 hint 噪音），这里优先取 code/message。"""
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
    """调 hithink CLI。words=['market','snapshot']（不含 main.js）。
    返回 {'ok':True,'payload':obj} 或 {'ok':False,'error':str}。
    - 给 outfile 时大结果落盘，优先读回文件，stdout 兜底
    - 瞬断类失败重试 <=2 次退避 1s；参数性错误不重试；永不抛异常
    """
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
        # 1) 落盘文件优先（大结果时 stdout 可能只剩摘要）
        if outfile and os.path.exists(outfile):
            try:
                if os.path.getsize(outfile) > 0:
                    with io.open(outfile, "r", encoding="utf-8") as f:
                        return _judge(json.load(f), err)
            except Exception:
                pass
        # 2) stdout JSON
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
    """按顶层 ok 判定成败；失败描述统一取 error 字段。"""
    if isinstance(payload, dict):
        okv = payload.get("ok")
        if okv is False or okv == "false":
            e = payload.get("error") or err or "ok=false"
            if isinstance(e, dict):
                e = e.get("message") or json.dumps(e, ensure_ascii=False)
            return {"ok": False, "error": str(e)[:300]}
    return {"ok": True, "payload": payload}


def probe_cli(ctx):
    """启动预检：node + CLI 文件 + capabilities。失败 exit 1。"""
    if not os.path.exists(NODE_MAIN):
        say("[FAIL] cli: main.js not found: %s" % NODE_MAIN)
        say("       install: npm i -g @hithink-tech/hithink-finance-cli")
        sys.exit(1)
    r = invoke(ctx, ["capabilities"])
    if not r["ok"]:
        say("[FAIL] cli: capabilities failed: %s" % str(r.get("error"))[:200])
        say("       check: node in PATH / hithink auth status")
        sys.exit(1)
    say("[OK] cli: node + hithink CLI ready")

# ===== [SEC-05] 日期工具 =====


def now_local():
    return datetime.datetime.now()


def today_local():
    return datetime.date.today()


def parse_date(s):
    """'YYYY-MM-DD' → date；非法返回 None。"""
    try:
        return datetime.datetime.strptime(str(s).strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def date_to_ms(d):
    return int(time.mktime(datetime.datetime(d.year, d.month, d.day).timetuple())) * 1000


def ms_to_date(ms):
    try:
        return datetime.datetime.fromtimestamp(int(ms) / 1000.0).strftime("%Y-%m-%d")
    except (ValueError, TypeError, OSError, OverflowError):
        return ""


def ts_to_str(ts, fmt="%Y-%m-%d %H:%M"):
    try:
        return datetime.datetime.fromtimestamp(int(ts)).strftime(fmt)
    except (ValueError, TypeError, OSError, OverflowError):
        return "-"


def _parse_dt_ts(s):
    """'YYYY-MM-DD HH:MM:SS' → epoch 秒；失败返回 None。"""
    try:
        return int(time.mktime(time.strptime(str(s), "%Y-%m-%d %H:%M:%S")))
    except (ValueError, TypeError):
        return None


def report_guess(d):
    """最近已完整披露的报告期启发式：
    1-4月→上年Q4（年报窗）；5-8月→本年Q1；9-10月→本年Q2（中报8/31截止）；11-12月→本年Q3。"""
    m, y = d.month, d.year
    if m <= 4:
        return y - 1, 4
    if m <= 8:
        return y, 1
    if m <= 10:
        return y, 2
    return y, 3


def report_back(y, q):
    """回退一个报告期（guard 2010）。"""
    if q == 1:
        return (y - 1, 4) if y - 1 >= 2010 else (y, q)
    return y, q - 1


def session_note():
    """盘中/非交易时段标注（只描述事实）。"""
    now = now_local()
    hm = now.hour * 60 + now.minute
    if now.weekday() >= 5:
        return "非交易日（数据为最近交易日定格）"
    if 9 * 60 + 15 <= hm <= 11 * 60 + 30:
        return "盘中（上午）"
    if 11 * 60 + 30 < hm < 13 * 60:
        return "午间休市"
    if 13 * 60 <= hm <= 15 * 60:
        return "盘中（下午）"
    return "非交易时段（当日已定格）"

# ===== [SEC-06] 通用解析工具 =====


def num(v):
    """任意值 → float；缺失/非法 → None（数据口径：缺失绝不置 0）。"""
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        if s in ("", "-", "--", "null", "None"):
            return None
        try:
            return float(s)
        except ValueError:
            return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _f(v):
    """任意值 → float；缺失/非法 → 0.0（json 数值契约，勿改成 None）。"""
    r = num(v)
    return 0.0 if r is None else r


def _find_arrays(obj, out=None):
    """递归收集对象内所有「dict 列表」。"""
    if out is None:
        out = []
    if isinstance(obj, dict):
        for v in obj.values():
            _find_arrays(v, out)
    elif isinstance(obj, list):
        if obj and any(isinstance(x, dict) for x in obj):
            out.append(obj)
        for v in obj:
            _find_arrays(v, out)
    return out


def pick_rows(obj, key=None):
    """payload → 行列表；key 给定时只收含该字段的行。"""
    rows = []
    for arr in _find_arrays(obj):
        for r in arr:
            if isinstance(r, dict) and (key is None or key in r):
                rows.append(r)
    return rows


def is_thscode(s):
    """带后缀的 thscode 形态：6位数字 + .SH/.SZ/.BJ/.OF（大小写不敏感）。"""
    return bool(re.fullmatch(r"\d{6}\.[A-Za-z]{2}", (s or "").strip()))


def clean_html(s):
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", "", str(s))
    return re.sub(r"\s+", " ", s).strip()


def make_item(src, id_, title, content, ts, url=""):
    """统一快讯条目 schema。"""
    return {"src": src, "id": str(id_), "title": title or "",
            "content": content or "", "ts": int(ts or 0), "url": url or ""}


def _next_data_extract(html):
    """从页面抠出 __NEXT_DATA__ JSON：<script id=...> 或内联赋值（需大括号配对）。"""
    m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except ValueError:
            pass
    m = re.search(r"__NEXT_DATA__\s*=\s*\{", html)
    if not m:
        return None
    i = html.index("{", m.start())
    depth, instr, esc = 0, False, False
    for j in range(i, len(html)):
        ch = html[j]
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
            continue
        if ch == '"':
            instr = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[i:j + 1])
                except ValueError:
                    return None
    return None


def cls_sign(qs):
    """财联社签名 = md5(sha1(排序后的 query_string))。无密钥、纯标准库可算。
    关键：sign 必须对「实际发出的那串参数」计算，少发一个参数即 errno=10012。"""
    return hashlib.md5(hashlib.sha1(qs.encode("utf-8")).hexdigest().encode("utf-8")).hexdigest()


def _cls_signed_url(path, params):
    qs = urllib.parse.urlencode(sorted(params.items()))
    return "%s?%s&sign=%s" % (path, qs, cls_sign(qs))


def _cls_roll_items(data):
    """财联社 roll_data → 统一条目。title 约半数为空，必须用 brief 兜底，否则丢一半。"""
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
                             n.get("shareurl") or ("https://www.cls.cn/detail/%s" % n.get("id"))))
    return out


def _parse_sina_jsonp(text):
    """新浪 jsonp → list[dict]：剥注释 → 取 () 内 JSON。"""
    s = re.sub(r"/\*.*?\*/", "", text, flags=re.S).strip()
    if not s:
        raise ValueError("空响应")
    m = re.search(r"\((\[.*\])\)", s, re.S)
    if m:
        return json.loads(m.group(1))
    start, end = s.find("("), s.rfind(")")
    if start >= 0 and end > start:
        return json.loads(s[start + 1:end])
    return json.loads(s)


def http_get_bytes(url, params=None, headers=None, timeout=12, tries=3):
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


def http_get(url, params=None, headers=None, timeout=15, retries=1):
    """requests GET；失败重试后仍失败则向上抛（调用方负责标注 FAIL）。"""
    if requests is None:
        raise RuntimeError("requests 未安装：pip install requests")
    h = dict(BASE_HEADERS)
    h.update(headers or {})
    last = None
    for i in range(retries + 1):
        try:
            r = requests.get(url, params=params, headers=h, timeout=timeout)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last = e
            if i < retries:
                time.sleep(1)
    raise last


def http_post(url, data=None, headers=None, timeout=15, retries=1):
    """requests POST（表单编码）。"""
    if requests is None:
        raise RuntimeError("requests 未安装：pip install requests")
    h = dict(BASE_HEADERS)
    h.update(headers or {})
    last = None
    for i in range(retries + 1):
        try:
            r = requests.post(url, data=data, headers=h, timeout=timeout)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last = e
            if i < retries:
                time.sleep(1)
    raise last


def _eastmoney_get(urls, params, tries=4, base_sleep=1.5, timeout=12):
    """东财专用：host 列表轮换 + 指数退避。任一成功返回 dict，全败返回 None
    （push2 时段性拒连，故走 bytes 通道并轮换 host）。"""
    for url in urls:
        for attempt in range(tries):
            raw = http_get_bytes(url, params=params, headers=EM_HEADERS,
                                 timeout=timeout, tries=1)
            if raw:
                try:
                    return json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    pass
            if attempt < tries - 1:
                time.sleep(base_sleep * attempt + 1)
    return None


def _em_secid(thscode):
    """thscode → 东财 secid：.SH（含 5xx ETF / 000xxx 指数）→ 1.xxxxxx；.SZ/.BJ → 0.xxxxxx；
    .OF 无行情 secid → None。"""
    parts = (thscode or "").split(".")
    if len(parts) != 2:
        return None
    base, suf = parts[0], parts[1].upper()
    if suf == "OF":
        return None
    if suf == "SH":
        return "1." + base
    if suf in ("SZ", "BJ"):
        return "0." + base
    return None


def _tx_sym(thscode):
    """thscode → 腾讯符号 sh600519 / sz002463；.OF 返回 None。"""
    parts = (thscode or "").split(".")
    if len(parts) != 2:
        return None
    base, suf = parts[0], parts[1].upper()
    if suf == "SH":
        return "sh" + base
    if suf == "SZ":
        return "sz" + base
    if suf == "BJ":
        return "bj" + base
    return None


def fmt_n(v, nd=2):
    """数值 → 定长字符串；None / NaN → '-'（pandas 缺失值必须落到 '-' 而不是 'nan'）。"""
    if v is None:
        return "-"
    try:
        f = float(v)
        if f != f:
            return "-"
        return ("%." + str(nd) + "f") % f
    except (TypeError, ValueError):
        return "-"

# ===== [SEC-07] 标的解析与资产分流 =====


def classify_thscode(thscode):
    """带后缀 thscode → stock/fund/index；无法判道返回 None。
    SH: 6/9=个股, 5=基金, 其余(000/950..)=指数;
    SZ: 399=指数, 1=基金(15/16/18 ETF/LOF/REIT), 其余=个股; .OF=场外基金; .BJ=北交所个股。"""
    parts = (thscode or "").split(".")
    if len(parts) != 2:
        return None
    code, suf = parts[0], parts[1].upper()
    if not re.fullmatch(r"\d{6}", code):
        return None
    if suf == "OF":
        return "fund"
    if suf == "BJ":
        return "stock"
    if suf == "SH":
        if code[:1] in ("6", "9"):
            return "stock"
        if code[:1] == "5":
            return "fund"
        return "index"
    if suf == "SZ":
        if code.startswith("399"):
            return "index"
        if code[:1] == "1":
            return "fund"
        return "stock"
    return None


def asset_type_channel(at):
    """symbol.search 的 asset_type → (通道, 细分)。
    实测值：a-share / a-share-index / fund-etf / fund-lof / fund-otc。"""
    at = str(at or "")
    if at == "a-share":
        return "stock", ""
    if at in ("a-share-index", "index"):
        return "index", ""
    if at.startswith("fund"):
        return "fund", at.replace("fund-", "")
    return None, at


def search_hits(r):
    """symbol.search payload → 候选行（全部通道）。"""
    out = []
    for arr in _find_arrays(r.get("payload", {}) if isinstance(r, dict) else {}):
        for row in arr:
            if isinstance(row, dict) and row.get("thscode"):
                out.append(row)
    return out


def _resolve_name_hit(rows, token):
    """名称消歧 + 名称⇔代码双向一致校验（AGENTS.md 铁律）。
    返回 (hit, "exact"/"partial", "") 或 (None, "", ASCII 失败原因)。
    ASCII 原因用于 stdout（中文会被 GBK 管道吃掉），中文详情落进交付 JSON。"""
    named = [x for x in rows if str(x.get("name") or "").strip()]
    for x in named:
        if str(x.get("name")).strip() == token:
            return x, "exact", ""
    partial = [x for x in named
               if token in str(x.get("name")) or str(x.get("name")) in token]
    if len(partial) == 1:
        return partial[0], "partial", ""
    if len(partial) > 1:
        return None, "", "ambiguous name (%d candidates)" % len(partial)
    if rows:
        return None, "", "name/code mismatch: no same-name security"
    return None, "", "no security matched"


def _tok_label(t):
    """stdout 用的输入标签：非 ASCII 输入（中文名）折叠成占位符，避免乱码。"""
    return t if all(ord(c) < 128 for c in str(t)) else "<name>"


def _resolve_numeric_hit(rows, token):
    """裸 6 位码：按通道偏好取（个股 > 指数 > ETF/LOF > 场外）。"""
    prio = {"a-share": 0, "a-share-index": 1, "fund-etf": 2, "fund-lof": 2, "fund-otc": 3}
    return min(rows, key=lambda x: (prio.get(str(x.get("asset_type") or ""), 9), rows.index(x)))


def _quote_name(thscode):
    """带后缀代码 → 中文名（腾讯 qt 通道，失败返回 ''）。"""
    sym = _tx_sym(thscode)
    if not sym:
        return ""
    raw = http_get_bytes(TX_QT_URL + sym, headers=TX_REF_HEADERS, timeout=8, tries=1)
    if not raw:
        return ""
    try:
        for line in raw.decode("gbk", "replace").strip().split(";"):
            p = line.split("~")
            if len(p) > 3 and p[2] == thscode[:6]:
                return p[1].strip()
    except Exception:
        pass
    return ""


def resolve_entry(ctx, token):
    """单 token（带后缀 thscode / 裸6位码 / 中文名）→ entry dict 或 None。"""
    t = (token or "").strip()
    if not t:
        return None
    if is_thscode(t):
        code = t[:6] + "." + t[7:].upper()
        ch = classify_thscode(code)
        if not ch:
            ctx.bad.append("%s: 无法判道（仅支持 .SH/.SZ/.BJ/.OF）" % t)
            say("[FAIL] resolve %s: unsupported suffix (need .SH/.SZ/.BJ/.OF)" % t)
            return None
        # 名称回填：先腾讯报价通道，失败再退回 symbol.search（指数/ETF 在腾讯通道常无名）
        name = _quote_name(code)
        if not name:
            rs = invoke(ctx, ["symbol", "search"], ["--q", code[:6], "--limit", "12"])
            if rs["ok"]:
                for row in search_hits(rs):
                    if str(row.get("thscode")) == code:
                        name = str(row.get("name") or "")
                        break
        return {"type": ch, "code": code, "name": name or code, "kind": "",
                "asset_type": "", "match": "code"}
    r = invoke(ctx, ["symbol", "search"], ["--q", t, "--limit", "12"])
    if not r["ok"]:
        ctx.bad.append("%s: symbol.search 失败 %s" % (t, str(r.get("error"))[:120]))
        say("[FAIL] resolve %s: symbol.search %s" % (_tok_label(t), str(r.get("error"))[:120]))
        return None
    rows = search_hits(r)
    if re.fullmatch(r"\d{6}", t):
        if not rows:
            ctx.bad.append("%s: 无任何通道命中（指数裸码请带后缀，如 000300.SH）" % t)
            say("[FAIL] resolve %s: no match (index codes need suffix, e.g. 000300.SH)" % t)
            return None
        hit, match = _resolve_numeric_hit(rows, t), "code"
    else:
        hit, match, why = _resolve_name_hit(rows, t)
        if hit is None:
            ctx.bad.append("%s: %s" % (t, why))
            say("[FAIL] resolve %s: %s" % (_tok_label(t), why))
            return None
    ch, kind = asset_type_channel(hit.get("asset_type"))
    code = str(hit.get("thscode"))
    name = str(hit.get("name") or "")
    if ch is None:
        ctx.bad.append("%s: 不支持的 asset_type=%s" % (t, hit.get("asset_type")))
        say("[FAIL] resolve %s -> %s: unsupported asset_type=%s"
            % (_tok_label(t), code, hit.get("asset_type")))
        return None
    if match == "partial":
        say("[WARN] resolve %s -> %s (%s) via partial-name-match" % (t, code, name))
    ctx.resolves.append("%s -> %s (%s, %s, %s)" % (t, code, name, hit.get("asset_type"), match))
    say("[OK] resolve %s (%s)" % (code, ch))
    return {"type": ch, "code": code, "name": name, "kind": kind,
            "asset_type": str(hit.get("asset_type") or ""), "match": match}


def read_pool(path):
    """持仓/自选文件 → token 列表。
    兼容 caiwu\\持仓数据.md 的多列形态：自动识别「证券代码」列（缺表头则取首个 6 位数字）。"""
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
            hit_col = None
            for i, c in enumerate(cells):
                if "证券代码" in c or c in ("代码", "股票代码", "ticker"):
                    hit_col = i
                    break
            if hit_col is not None:
                code_col = hit_col
                continue
            if any("证券名称" in c or c in ("名称", "股票名称") for c in cells):
                continue    # 表头行但无代码列 → 忽略
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


def build_entries(ctx, tokens):
    """tokens → entries（去重保序）。"""
    seen = set()
    for t in tokens:
        e = resolve_entry(ctx, t)
        if not e or e["code"] in seen:
            continue
        seen.add(e["code"])
        ctx.entries.append(e)
    return ctx.entries

# ===== [SEC-08] 落盘 =====


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

# ===== [SEC-09] 技术指标与中性事实标注 =====


def _ema_series(seq, n):
    k = 2.0 / (n + 1)
    out = [seq[0]]
    for x in seq[1:]:
        out.append(x * k + out[-1] * (1 - k))
    return out


def kline_analysis(rows):
    """rows=[{date, open, close, high, low, vol}...]（时间升序）→ 指标 dict 或 None。
    输出（全中性数值）：MA5/10/20/30/60、60日高低与分位、20日高低、量比、
    MACD(dif/dea/hist)、KDJ、RSI14、BOLL(20,2)。窗口不足 → None，不伪造。"""
    rows = [r for r in rows if isinstance(r, dict) and num(r.get("close")) is not None]
    if not rows:
        return None
    closes = [num(r["close"]) for r in rows]
    vols = [num(r.get("vol")) for r in rows]
    n = len(closes)
    last = closes[-1]
    a = {"bars": n, "date": rows[-1].get("date", ""), "last": round(last, 3)}
    for w in (5, 10, 20, 30, 60):
        a["ma%d" % w] = round(sum(closes[-w:]) / w, 3) if n >= w else None
    win = closes[-60:]
    if len(win) >= 20:
        hi60, lo60 = max(win), min(win)
        a["hi60"], a["lo60"] = round(hi60, 3), round(lo60, 3)
        a["pos60"] = round(100.0 * sum(1 for c in win if c <= last) / len(win), 1)
    else:
        a["hi60"] = a["lo60"] = a["pos60"] = None
    if n >= 20:
        w20 = closes[-20:]
        a["hi20"], a["lo20"] = round(max(w20), 3), round(min(w20), 3)
    else:
        a["hi20"] = a["lo20"] = None
    if n >= 6 and all(v is not None for v in vols[-6:]):
        ma5v = sum(vols[-6:-1]) / 5.0
        a["vol_ratio"] = round(vols[-1] / ma5v, 3) if ma5v > 0 else None
    else:
        a["vol_ratio"] = None
    # MACD(12,26,9)
    if n >= 26:
        e12, e26 = _ema_series(closes, 12), _ema_series(closes, 26)
        dif = [x - y for x, y in zip(e12, e26)]
        dea = _ema_series(dif, 9)
        a["macd_dif"] = round(dif[-1], 4)
        a["macd_dea"] = round(dea[-1], 4)
        a["macd_hist"] = round((dif[-1] - dea[-1]) * 2, 4)
        a["_dif"] = dif
        a["_dea"] = dea
    # RSI14（Wilder）
    if n > 14:
        gains = losses = 0.0
        for i in range(1, 15):
            ch = closes[i] - closes[i - 1]
            gains += max(ch, 0)
            losses += max(-ch, 0)
        ag, al = gains / 14.0, losses / 14.0
        for i in range(15, n):
            ch = closes[i] - closes[i - 1]
            ag = (ag * 13 + max(ch, 0)) / 14.0
            al = (al * 13 + max(-ch, 0)) / 14.0
        a["rsi14"] = round(100.0 if al == 0 else 100 - 100 / (1 + ag / al), 2)
    else:
        a["rsi14"] = None
    # KDJ(9,3,3)
    if n >= 9:
        k = d = 50.0
        for i in range(8, n):
            lo9, hi9 = min(closes[i - 8:i + 1]), max(closes[i - 8:i + 1])
            rsv = 50.0 if hi9 == lo9 else (closes[i] - lo9) / (hi9 - lo9) * 100
            k = k * 2 / 3 + rsv / 3
            d = d * 2 / 3 + k / 3
        a["kdj_k"], a["kdj_d"], a["kdj_j"] = round(k, 2), round(d, 2), round(3 * k - 2 * d, 2)
    else:
        a["kdj_k"] = a["kdj_d"] = a["kdj_j"] = None
    # BOLL(20, 2σ)
    if n >= 20:
        w20 = closes[-20:]
        mid = sum(w20) / 20.0
        sd = (sum((c - mid) ** 2 for c in w20) / 20.0) ** 0.5
        a["boll_mid"], a["boll_up"], a["boll_lo"] = (round(mid, 3), round(mid + 2 * sd, 3),
                                                      round(mid - 2 * sd, 3))
        span = a["boll_up"] - a["boll_lo"]
        a["boll_pos"] = round(100.0 * (last - a["boll_lo"]) / span, 1) if span else None
    else:
        a["boll_mid"] = a["boll_up"] = a["boll_lo"] = a["boll_pos"] = None
    # 派生：近10日涨幅、MA 乖离
    base = closes[-11] if n >= 11 else closes[0]
    a["chg10_pct"] = round((last - base) / base * 100, 2) if base else None
    for w in (10, 20):
        m = a.get("ma%d" % w)
        a["bias%d_pct" % w] = round((last - m) / m * 100, 2) if m else None
    return a


def tech_tags(an):
    """指标 dict → 中性事实标注（事实陈述，不含建议、不打分）。"""
    if not an:
        return {}
    t = {}
    r = TAG_RULES
    ma5, ma10, ma20 = an.get("ma5"), an.get("ma10"), an.get("ma20")
    if None not in (ma5, ma10, ma20):
        if ma5 > ma10 > ma20:
            t["均线排列"] = "多头排列（MA5>MA10>MA20）"
        elif ma5 < ma10 < ma20:
            t["均线排列"] = "空头排列（MA5<MA10<MA20）"
        else:
            t["均线排列"] = "均线纠缠（无一致方向）"
    else:
        t["均线排列"] = "样本不足（需≥20根日K）"
    b = an.get("bias20_pct")
    if b is not None:
        if b >= r["bias_alert_pct"]:
            t["MA20乖离"] = "正乖离 %.2f%%（≥%.0f%% 阈值）" % (b, r["bias_alert_pct"])
        elif b <= -r["bias_alert_pct"]:
            t["MA20乖离"] = "负乖离 %.2f%%（≤-%.0f%% 阈值）" % (b, r["bias_alert_pct"])
        else:
            t["MA20乖离"] = "%.2f%%（阈值内）" % b
    dif, dea = an.get("_dif"), an.get("_dea")
    if dif and dea and len(dif) >= 6:
        cross = ""
        for i in range(len(dif) - 5, len(dif)):
            if i <= 0:
                continue
            if dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]:
                cross = "近5日 MACD 金叉（DIF 上穿 DEA）"
            elif dif[i - 1] >= dea[i - 1] and dif[i] < dea[i]:
                cross = "近5日 MACD 死叉（DIF 下穿 DEA）"
        t["MACD"] = cross or ("DIF %s DEA，近5日无交叉" % ("高于" if dif[-1] > dea[-1] else "低于"))
    else:
        t["MACD"] = "样本不足（需≥26根日K）"
    rsi = an.get("rsi14")
    if rsi is None:
        t["RSI14"] = "样本不足"
    elif rsi >= 70:
        t["RSI14"] = "%.2f（超买区间 ≥70）" % rsi
    elif rsi <= 30:
        t["RSI14"] = "%.2f（超卖区间 ≤30）" % rsi
    else:
        t["RSI14"] = "%.2f（中性区间 30-70）" % rsi
    bp = an.get("boll_pos")
    if bp is None:
        t["BOLL位置"] = "样本不足"
    elif bp > 100:
        t["BOLL位置"] = "%.1f%%（收盘价高于上轨）" % bp
    elif bp < 0:
        t["BOLL位置"] = "%.1f%%（收盘价低于下轨）" % bp
    else:
        t["BOLL位置"] = "%.1f%%（通道内）" % bp
    vr = an.get("vol_ratio")
    if vr is None:
        t["量能"] = "样本不足"
    elif vr >= r["vol_ratio_high"]:
        t["量能"] = "显著放量（量比 %.2f）" % vr
    elif vr >= r["vol_ratio_mild_high"]:
        t["量能"] = "温和放量（量比 %.2f）" % vr
    elif vr <= r["vol_ratio_low"]:
        t["量能"] = "显著缩量（量比 %.2f）" % vr
    elif vr <= r["vol_ratio_mild_low"]:
        t["量能"] = "温和缩量（量比 %.2f）" % vr
    else:
        t["量能"] = "平量（量比 %.2f）" % vr
    p60 = an.get("pos60")
    if p60 is None:
        t["60日分位"] = "样本不足"
    elif p60 >= r["pos60_high"]:
        t["60日分位"] = "%.1f%%（高位区）" % p60
    elif p60 <= r["pos60_low"]:
        t["60日分位"] = "%.1f%%（低位区）" % p60
    else:
        t["60日分位"] = "%.1f%%（中位区）" % p60
    return t


def public_analysis(an):
    """剥离内部临时字段（_dif/_dea）后的指标快照。"""
    if not an:
        return None
    return {k: v for k, v in an.items() if not k.startswith("_")}

# ===== [SEC-10] 快照 / K线降级链 / 盘中补充源 =====


def _snapshot_call(ctx, entry):
    """按资产类型选快照端点。
    实测形态：market.snapshot 与 index.snapshot 用复数 --thscodes；
    fund.snapshot 用单数 --thscode（传 --thscodes 会报 required option 缺失）。"""
    kind, code = entry["type"], entry["code"]
    if kind == "fund":
        return invoke(ctx, ["fund", "snapshot"], ["--thscode", code])
    if kind == "index":
        return invoke(ctx, ["index", "snapshot"], ["--thscodes", code])
    return invoke(ctx, ["market", "snapshot"], ["--thscodes", code])


def normalize_snapshot(row):
    """快照行 → 统一字段（缺失保持 None，绝不置 0）。"""
    return {
        "thscode": row.get("thscode"),
        "名称": row.get("name") or "",
        "现价": num(row.get("last_price")),
        "昨收": num(row.get("prev_price")),
        "今开": num(row.get("open_price")),
        "最高": num(row.get("high_price")),
        "最低": num(row.get("low_price")),
        "涨跌额": num(row.get("price_change")),
        "涨跌幅_pct": num(row.get("price_change_ratio_pct")),
        "成交量": num(row.get("volume")),
        "成交额": num(row.get("turnover")),
        "一手成本": None if num(row.get("last_price")) is None else round(num(row["last_price"]) * 100, 2),
    }


def fetch_snapshot(ctx, entry):
    """→ (snapshot dict, error)。"""
    r = _snapshot_call(ctx, entry)
    if not r["ok"]:
        return None, str(r.get("error"))[:200]
    rows = pick_rows(r["payload"], "thscode")
    row = next((x for x in rows if x.get("thscode") == entry["code"]),
               rows[0] if rows else None)
    if row is None:
        return None, "接口 ok 但返回空（停牌/退市/代码无效?）"
    return normalize_snapshot(row), None


def _kline_hithink(ctx, entry, start_ms, end_ms, outdir):
    """hithink 日K。stock 走 market.history（已知 raw_kline_daily 缺失故障，故三级阶梯）；
    fund/index 走各自 history（同样参数形态，但无已知故障）。"""
    kind, code = entry["type"], entry["code"]
    # 窗口上限实测（002463/512890/000300 三点采样）：
    #   基金：≥5 年直接报 CLI_BAD_ARGUMENT；指数：7 年「ok=true 但 0 条」的静默失败；
    #   指数/个股 10 年边界还会报 out of range。
    # 故统一夹到 1800 天（≈4.93 年）——这是三个端点都能稳定出数的最大跨度。
    start_ms = max(start_ms, date_to_ms(ctx.target) - 1800 * 86400000)
    words = {"stock": ["market", "history"], "fund": ["fund", "history"],
             "index": ["index", "history"]}[kind]
    args = ["--thscode", code, "--start-ms", str(start_ms), "--end-ms", str(end_ms)]
    if kind == "stock":
        args += ["--adjust", "forward"]
    r = invoke(ctx, words, args)
    if not r["ok"] and kind == "stock":
        e1 = r.get("error")
        time.sleep(2.0)
        r = invoke(ctx, words, args)
        if not r["ok"]:
            e2 = r.get("error")
            attempt = os.path.join(outdir, ".attempt_%s.json" % code)
            r = invoke(ctx, words, args, outfile=attempt)
            try:
                if os.path.exists(attempt):
                    os.remove(attempt)
            except OSError:
                pass
            if not r["ok"]:
                return None, "market.history 三级重试均失败 | 常规:%s | 2s重试:%s | 落盘重试:%s" % (
                    str(e1)[:90], str(e2)[:90], str(r.get("error"))[:90])
    if not r["ok"]:
        return None, str(r.get("error"))[:200]
    rows = []
    for x in pick_rows(r["payload"], "close_price"):
        c = num(x.get("close_price"))
        if c is None:
            continue
        rows.append({"date": ms_to_date(x.get("date_ms")), "open": num(x.get("open_price")),
                     "close": c, "high": num(x.get("high_price")),
                     "low": num(x.get("low_price")), "vol": num(x.get("volume")),
                     "amount": num(x.get("turnover"))})
    rows.sort(key=lambda x: x["date"])
    if not rows:
        return None, "接口 ok 但返回 0 根K"
    return rows, None


def _kline_em(ctx, entry, limit):
    """路径2：东财日K（前复权 fqt=1）。limit=0 → 不传 lmt，按东财能力取最长。"""
    secid = _em_secid(entry["code"])
    if not secid:
        return None, "无东财 secid"
    params = {"secid": secid, "klt": 101, "fqt": 1, "beg": 0, "end": "20500101",
              "fields1": "f1,f2,f3,f4,f5,f6",
              "fields2": "f51,f52,f53,f54,f55,f56,f57", "ut": EM_UT}
    if limit:
        params["lmt"] = limit
    d = _eastmoney_get(EM_KLINE_URLS, params, tries=3, base_sleep=1.0)
    if not d:
        return None, "东财 kline 请求失败"
    rows = []
    for s in ((d.get("data") or {}).get("klines") or []):
        p = str(s).split(",")
        if len(p) < 6:
            continue
        rows.append({"date": p[0], "open": num(p[1]), "close": num(p[2]),
                     "high": num(p[3]), "low": num(p[4]), "vol": num(p[5]),
                     "amount": num(p[6]) if len(p) > 6 else None})
    if not rows:
        return None, "东财 kline 返回空"
    return rows, None


def _kline_sina(ctx, entry, limit):
    """路径3：新浪日K（不复权；jsonp 剥壳 + Referer 必带）。实测 datalen 上限 1023 根。"""
    sym = _tx_sym(entry["code"])
    if not sym:
        return None, "无新浪符号"
    raw = http_get_bytes(SINA_KLINE_URL, params={
        "symbol": sym, "scale": 240, "ma": "no", "datalen": limit or SINA_MAX_BARS},
        headers=SINA_REF_HEADERS, timeout=12, tries=2)
    if not raw:
        return None, "新浪 kline 请求失败"
    try:
        lst = _parse_sina_jsonp(raw.decode("utf-8", "replace"))
    except ValueError as e:
        return None, "新浪 kline 非 JSONP: %s" % e
    rows = []
    for x in lst:
        if not isinstance(x, dict) or "day" not in x:
            continue
        rows.append({"date": x.get("day"), "open": num(x.get("open")), "close": num(x.get("close")),
                     "high": num(x.get("high")), "low": num(x.get("low")),
                     "vol": num(x.get("volume")), "amount": None})
    if not rows:
        return None, "新浪 kline 返回空"
    return rows, None


def _kline_tx(ctx, entry, limit):
    """路径4：腾讯日K（qfq 前复权；缺失降级不复权 day）。实测 800 根以内稳定。"""
    sym = _tx_sym(entry["code"])
    if not sym:
        return None, "无腾讯符号"
    limit = limit or TX_MAX_BARS
    for param in ("%s,day,,,%d,qfq" % (sym, limit), "%s,day,,,%d," % (sym, limit)):
        raw = http_get_bytes(TX_KLINE_URL, params={"param": param},
                             headers=TX_REF_HEADERS, timeout=10, tries=2)
        if not raw:
            continue
        try:
            d = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        node = (d.get("data") or {}).get(sym) or {}
        raw_rows = node.get("qfqday") or node.get("day") or []
        rows = []
        for r in raw_rows:
            if not isinstance(r, list) or len(r) < 6:
                continue
            rows.append({"date": r[0], "open": num(r[1]), "close": num(r[2]),
                         "high": num(r[3]), "low": num(r[4]), "vol": num(r[5]),
                         "amount": None})
        if rows:
            return rows, None
    return None, "腾讯 kline 返回空"


KLINE_SOURCE_CN = {"hithink": "hithink", "em": "东财", "sina": "新浪", "tx": "腾讯"}
# 成交量单位归一化系数（统一换算成「股」）。
# 实测 002463.SZ：hithink 快照 43,949,659 股；新浪 43,949,659（股）；腾讯 439,497（手）；
# 东财 f56 为「手」。不归一化会让换手率与量能均线整体差 100 倍。
VOL_UNIT = {"hithink": 1, "sina": 1, "em": 100, "tx": 100}


def fetch_kline(ctx, entry, force_source=None, kline_max=0):
    """日K 降级链：缓存 → hithink → 东财 → 腾讯 → 新浪。
    返回 dict：{rows, source, adjust, degrade, error, from_cache, coverage}。
    长度策略：按源能力取最长（kline_max=0 表示不限）。各源实测上限——
    东财不限、新浪 1023 根、腾讯约 800 根；降级时覆盖区间会缩短，如实记录。
    任何一级失败都记入 degrade，最终全败时 error 非空且 rows 为空（不伪造）。"""
    code = entry["code"]
    path = os.path.join(ctx.history_dir, code + ".json")
    key = ctx.target.strftime("%Y-%m-%d")
    if not ctx.no_cache and not force_source:
        cached = read_json(path)
        if (isinstance(cached, dict) and cached.get("target_date") == key
                and cached.get("rows")
                and cached.get("vol_normalized") is True
                and int(cached.get("kline_max") or 0) == int(kline_max or 0)):
            return {"rows": cached["rows"], "source": cached.get("source", ""),
                     "adjust": cached.get("adjust", ""), "degrade": cached.get("degrade") or [],
                     "error": None, "from_cache": True,
                     "coverage": cached.get("coverage") or {}}
    end_ms = date_to_ms(ctx.target) + 86400000 - 1
    # 起始时间按「不限长度」放宽到 30 年，让支持长历史的源尽量给全
    lookback_days = KLINE_LOOKBACK_DAYS if kline_max else 365 * 30
    start_ms = date_to_ms(ctx.target) - lookback_days * 86400000
    limit = int(kline_max) if kline_max else 0
    chain = []
    if force_source:
        chain = [force_source]
    else:
        # 顺序按「复权口径质量」排：前复权源优先，不复权的新浪放最后兜底。
        # 可靠性并不因此下降——任一源失败都会继续往下走，直到有数据为止。
        chain = ["hithink", "em", "tx", "sina"]
    runners = {
        "hithink": (lambda: _kline_hithink(ctx, entry, start_ms, end_ms, ctx.history_dir), "forward"),
        "em": (lambda: _kline_em(ctx, entry, limit), "forward"),
        "sina": (lambda: _kline_sina(ctx, entry, limit), "none"),
        "tx": (lambda: _kline_tx(ctx, entry, limit), "forward"),
    }
    degrade, rows, src, adjust = [], None, None, ""
    for name in chain:
        fn, adj = runners[name]
        try:
            got, err = fn()
        except Exception as ex:
            got, err = None, "异常: %s" % ex
        if got:
            f = VOL_UNIT.get(name, 1)
            if f != 1:      # 手 → 股
                for r in got:
                    v = num(r.get("vol"))
                    r["vol"] = (v * f) if v is not None else None
            rows, src, adjust = (got[-limit:] if limit else got), name, adj
            break
        degrade.append("%s: %s" % (KLINE_SOURCE_CN.get(name, name), err))
        dbg(ctx, "kline fallback %s -> %s" % (name, err))
    coverage = {}
    if rows:
        coverage = {"bars": len(rows), "first": rows[0].get("date"), "last": rows[-1].get("date")}
        if len(rows) < 1250:
            coverage["说明"] = ("该源历史上限 %d 根（约 %.1f 年），不足 1250 根时"
                              "MA60月/MA250 等长周期指标会样本不足" % (len(rows), len(rows) / 250.0))
    out = {"rows": rows or [], "source": src or "", "adjust": adjust,
           "degrade": degrade, "error": None if rows else "全链路失败: " + " | ".join(degrade),
           "from_cache": False, "coverage": coverage}
    if rows:
        write_json(path, {"code": code, "target_date": key, "source": src, "adjust": adjust,
                          "bars": len(rows), "rows": rows, "degrade": degrade,
                          "coverage": coverage, "kline_max": int(kline_max or 0),
                          "vol_normalized": True, "vol_unit": "股",
                          "fetched_at": now_local().strftime("%Y-%m-%dT%H:%M:%S")})
    return out


def fetch_corporate_actions(ctx, entry, years=2):
    """公司行动/复权事件（个股专属；ETF/指数不适用）。"""
    if entry["type"] != "stock":
        return [], None
    frm = (ctx.target - datetime.timedelta(days=365 * years)).strftime("%Y-%m-%d")
    r = invoke(ctx, ["market", "corporate-actions"],
               ["--thscode", entry["code"], "--from-date", frm,
                "--to-date", ctx.target.strftime("%Y-%m-%d")])
    if not r["ok"]:
        return [], str(r.get("error"))[:160]
    out = []
    for x in pick_rows(r["payload"], "ex_date_ms"):
        out.append({"除权除息日": ms_to_date(x.get("ex_date_ms")),
                    "每股分红": num(x.get("dividend_per_share")),
                    "每股送股": num(x.get("per_share_bonus"))})
    out.sort(key=lambda x: x["除权除息日"] or "")
    return out, None


def fetch_minute(ctx, entry):
    """腾讯当日分时（--rt 用）。行格式：'HHMM 价 累计量(手) 累计额(元)'。"""
    sym = _tx_sym(entry["code"])
    if not sym:
        return None, "无腾讯符号"
    raw = http_get_bytes(TX_MIN_URL, params={"code": sym}, headers=TX_REF_HEADERS,
                         timeout=10, tries=2)
    if not raw:
        return None, "腾讯分时请求失败"
    try:
        node = ((json.loads(raw.decode("utf-8", "replace")).get("data") or {})
                .get(sym, {}).get("data")) or {}
    except ValueError:
        return None, "腾讯分时响应非 JSON"
    rows = []
    for line in (node.get("data") or []):
        p = str(line).split()
        if len(p) < 4:
            continue
        rows.append({"t": p[0], "price": num(p[1]), "cumvol": num(p[2]), "cumamt": num(p[3])})
    if not rows:
        return None, "腾讯分时返回空（非交易时段属正常）"
    return {"date": node.get("date", ""), "rows": rows}, None


def fetch_depth(ctx, entry):
    """腾讯五档（--rt 用）。字段索引见 fin.py 注释：9..28 为买一价..卖五量。"""
    sym = _tx_sym(entry["code"])
    if not sym:
        return None, "无腾讯符号"
    raw = http_get_bytes(TX_QT_URL + sym, headers=TX_REF_HEADERS, timeout=10, tries=2)
    if not raw:
        return None, "腾讯五档请求失败"
    try:
        for line in raw.decode("gbk", "replace").strip().split(";"):
            p = line.split("~")
            if len(p) < 52:
                continue
            bids = [{"价": num(p[9 + i * 2]), "量_手": num(p[10 + i * 2])} for i in range(5)]
            asks = [{"价": num(p[19 + i * 2]), "量_手": num(p[20 + i * 2])} for i in range(5)]
            return {"买五档": bids, "卖五档": asks,
                    "量比": num(p[49]), "均价": num(p[51]),
                    "涨停价": num(p[47]), "跌停价": num(p[48])}, None
    except Exception as e:
        return None, "五档解析失败: %s" % e
    return None, "五档无数据"


def fetch_ticks(ctx, entry):
    """东财当日分笔（--rt 用）。该源偶发断连/空 → None 由调用方如实标注。"""
    secid = _em_secid(entry["code"])
    if not secid:
        return None, "无东财 secid"
    params = {"secid": secid, "ut": EM_UT, "fields1": "f1,f2,f3,f4,f5",
              "fields2": "f51,f52,f53,f54,f55", "pos": "-10000", "lmt": "10000"}
    d = _eastmoney_get(EM_DETAIL_URLS, params, tries=2, base_sleep=1.0, timeout=10)
    if not d:
        return None, "东财分笔请求失败"
    rows = (d.get("data") or {}).get("details") or []
    if not rows:
        return None, "东财分笔返回空（非交易时段属正常）"
    return {"n": len(rows), "head": rows[:20], "tail": rows[-20:]}, None

# ===== [SEC-11] 技术面维度 =====


def collect_tech(ctx, entry, want_rt=False, force_source=None, kline_max=0,
                 skip=frozenset()):
    """单标的完整技术面：快照 + 日K(降级链) + 指标 + 扩展块 + 竞价/盘口 + 公司行动。"""
    code = entry["code"]
    res = {"code": code, "name": entry["name"], "type": entry["type"],
           "target_date": ctx.target.strftime("%Y-%m-%d"),
           "fetched_at": now_local().strftime("%Y-%m-%dT%H:%M:%S"),
           "session": session_note(), "sources": {}, "degrade": []}
    snap, snap_err = fetch_snapshot(ctx, entry)
    res["snapshot"] = snap
    res["sources"]["snapshot"] = ("OK" if snap else "FAIL: " + str(snap_err))
    if snap is None:
        res["degrade"].append("快照: %s" % snap_err)
        fail_msg(ctx, "tech", code, "快照失败 %s" % str(snap_err)[:120])
    kl = fetch_kline(ctx, entry, force_source=force_source, kline_max=kline_max)
    res["kline"] = {"source": kl["source"], "adjust": kl["adjust"], "bars": len(kl["rows"]),
                     "from_cache": kl["from_cache"], "first": (kl["rows"][0]["date"] if kl["rows"] else None),
                     "last": (kl["rows"][-1]["date"] if kl["rows"] else None),
                     "coverage": kl.get("coverage") or {}}
    res["sources"]["kline"] = ("OK(%s, %s, %d bars%s)" % (
        KLINE_SOURCE_CN.get(kl["source"], kl["source"]), kl["adjust"], len(kl["rows"]),
        ", cache" if kl["from_cache"] else "")) if kl["rows"] else ("FAIL: " + str(kl["error"]))
    if kl["degrade"]:
        res["degrade"].extend(["K线降级: " + d for d in kl["degrade"]])
    if kl["rows"]:
        ctx.kline_rows[code] = kl["rows"]      # 供基本面算估值分位，不写进交付 JSON
    an = kline_analysis(kl["rows"]) if kl["rows"] else None
    txq, txq_err = fetch_tx_quote(entry["code"]) if kl["rows"] else (None, "无K线")
    if an and txq:
        an["rsi6"] = _rsi([num(r.get("close")) for r in kl["rows"]], 6)
        an["rsi12"] = _rsi([num(r.get("close")) for r in kl["rows"]], 12)
    res["analysis"] = public_analysis(an)
    res["tags"] = tech_tags(an)
    if kl["rows"]:
        ext = tech_extended(kl["rows"], an, txq)
        res["levels"] = ext.get("关键价位")
        res["gaps"] = ext.get("缺口")
        res["volume_price"] = ext.get("量价")
        res["divergence"] = ext.get("背离")
        res["cross_state"] = ext.get("交叉状态")
        res["weekly"] = ext.get("周线")
        res["monthly"] = ext.get("月线")
        if ext.get("换手率"):
            res["turnover"] = ext["换手率"]
        res["chips"] = ext.get("筹码代理")
    if txq:
        res["sources"]["tencent_quote"] = "OK"
    else:
        res["sources"]["tencent_quote"] = "FAIL: %s" % txq_err
        res["degrade"].append("腾讯报价(换手率/振幅/涨跌停): %s" % txq_err)
    # 筹码源不可达，代理数据从股东层补（由 collect_fund 填，此处先占位说明）
    actions, act_err = fetch_corporate_actions(ctx, entry)
    res["corporate_actions"] = actions
    res["sources"]["corporate_actions"] = ("OK(%d)" % len(actions)) if actions else (
        "N/A" if entry["type"] != "stock" else ("OK(0)" if act_err is None else "FAIL: " + str(act_err)))
    if want_rt:
        res["intraday"] = {}
        for key, fn in (("minute", fetch_minute), ("depth", fetch_depth), ("ticks", fetch_ticks)):
            val, err = fn(ctx, entry)
            res["intraday"][key] = val
            res["sources"][key] = "OK" if val else ("FAIL: " + str(err))
    if "auction" not in skip:
        auc, auc_err = fetch_auction(ctx, entry)
        res["auction"] = auc
        res["sources"]["auction"] = ("N/A" if (auc or {}).get("applicable") is False else
                                    ("OK" if auc and auc.get("有数据") else
                                     ("OK(0)" if auc else "FAIL: " + str(auc_err))))
        if auc_err:
            res["degrade"].append("集合竞价: %s" % auc_err)
        res["limit_board"] = fetch_limit_board(ctx, entry)
    res["unavailable"] = list(UNAVAILABLE["tech"])
    if kl["rows"] and len(kl["rows"]) < 1250:
        res["unavailable"].append(
            {"字段": "MA60月 / 长周期历史高低",
             "原因": "当前源仅覆盖 %d 根日K（%s~%s），不足 1250 根；换源或缩短口径可改善"
                     % (len(kl["rows"]), kl["rows"][0]["date"], kl["rows"][-1]["date"])})
    ok = bool(kl["rows"])
    note = []
    if kl["rows"] and kl["source"] != "hithink":
        note.append("K线源=%s(降级)" % KLINE_SOURCE_CN.get(kl["source"], kl["source"]))
    if not kl["rows"]:
        note.append("无K线")
    ascii_note = "kline=%s bars=%d" % (kl["source"] or "none", len(kl["rows"]))
    if kl["degrade"]:
        ascii_note += " fallback=%d" % len(kl["degrade"])
    ctx.mark(code, "tech", "OK" if ok else "FAIL", "; ".join(note), note_ascii=ascii_note)
    return res

# ===== [SEC-12] 基本面维度 =====


def _fin_stmt_rows(ctx, entry, stmt, period, limit):
    """三报表取数：--thscode + --period(annual|quarterly) + --limit。"""
    ability = "financials." + stmt
    r = invoke(ctx, ["financials", stmt],
               ["--thscode", entry["code"], "--period", period, "--limit", str(limit)])
    if not r["ok"]:
        return [], "%s: %s" % (ability, str(r.get("error"))[:150])
    rows = [x for x in pick_rows(r["payload"], "fiscal_year")]
    if not rows:
        return [], "%s(%s) 返回空" % (ability, period)
    return rows, None


def fetch_indicators(ctx, entry):
    """最近报告期指标，返回空则自动回退一期（最多回退 2 次）。"""
    y, q = report_guess(ctx.target)
    tried = []
    for _ in range(3):
        tried.append("%d-%d" % (y, q))
        r = invoke(ctx, ["financials", "indicators"],
                   ["--thscode", entry["code"], "--report", "%d-%d" % (y, q)])
        # 注意：indicators 的 payload 里没有 thscode 字段（结构是 abilities[].indicators[]），
        # 因此不能用 count_rows(...,"thscode") 判空，否则永远判成"无数据"。
        if r["ok"] and flatten_indicators(r["payload"]):
            return {"report": "%dQ%d" % (y, q), "tried": tried, "data": r["payload"]}, None
        y, q = report_back(y, q)
    return None, "报告期 %s 均无数据" % "+".join(tried)


def flatten_indicators(payload):
    """indicators payload → {能力分组: {指标: 数值}}（值统一转 float，缺失置 None）。"""
    out = {}
    rows = pick_rows(payload, "ability")
    if not rows:
        # 兼容 abilities:[{ability, indicators:[...]}] 形态
        for arr in _find_arrays(payload):
            for x in arr:
                if isinstance(x, dict) and x.get("ability"):
                    rows.append(x)
    for grp in rows:
        name = str(grp.get("ability") or "")
        vals = {}
        for ind in grp.get("indicators") or []:
            if isinstance(ind, dict) and ind.get("index_id"):
                vals[str(ind["index_id"])] = num(ind.get("value"))
        if name:
            out[name] = vals
    return out


def _stmt_frame(rows, cols):
    """报表行 → pandas 宽表（只保留报告期键与关注的列）。"""
    if not rows or pd is None:
        return None
    df = pd.DataFrame([r for r in rows if isinstance(r, dict)])
    if df.empty or "fiscal_year" not in df.columns or "fiscal_period" not in df.columns:
        return None
    df["report_date"] = (df["period_end_ms"].map(ms_to_date)
                         if "period_end_ms" in df.columns else "")
    keep = ["fiscal_year", "fiscal_period", "report_date"] + [c for c in cols if c in df.columns]
    df = df[keep].copy()
    df = df.sort_values(["fiscal_year", "fiscal_period"]).drop_duplicates(
        ["fiscal_year", "fiscal_period"], keep="last")
    return df.reset_index(drop=True)


def _safe_div(a, b):
    try:
        if a is None or b in (None, 0):
            return None
        return float(a) / float(b)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def build_fund_wide(inc_rows, bs_rows, cf_rows):
    """三报表 → 派生宽表（pandas）。返回 (records, error)。
    口径说明：季报为**年初至今累计值**（Q1=Q1, Q2=H1, Q3=9M, Q4=全年），
    因此「单季」由同财年内相邻期相减得到，同比与同一 fiscal_period 的上一财年比较。"""
    if pd is None:
        return None, "pandas 未安装（pip install pandas）"
    inc = _stmt_frame(inc_rows, ["operating_income", "operating_costs", "operating_profit",
                                 "net_profit", "parent_holder_net_profit", "basic_eps"])
    if inc is None:
        return None, "利润表无可用行"
    bs = _stmt_frame(bs_rows, ["assets_total", "total_current_assets", "total_debt",
                               "holder_equity_total", "cash", "accounts_receivable"])
    cf = _stmt_frame(cf_rows, ["act_cash_flow_net", "invest_cash_flow_net",
                               "financing_cash_flow_net", "cash_equivalents_net_addition"])
    df = inc
    for other in (bs, cf):
        if other is not None:
            df = df.merge(other, on=["fiscal_year", "fiscal_period", "report_date"], how="outer")
    df = df.sort_values(["fiscal_year", "fiscal_period"]).reset_index(drop=True)

    def col(name):
        return df[name] if name in df.columns else pd.Series([None] * len(df))

    rev = col("operating_income")
    cost = col("operating_costs")
    npp = col("parent_holder_net_profit")
    eq = col("holder_equity_total")
    debt = col("total_debt")
    assets = col("assets_total")
    ocf = col("act_cash_flow_net")
    df["毛利率_pct"] = [None if (r in (None, 0) or c is None) else (r - c) / r * 100
                        for r, c in zip(rev, cost)]
    df["净利率_pct"] = [None if (r in (None, 0) or p is None) else p / r * 100
                        for r, p in zip(rev, npp)]
    df["ROE_pct_累计"] = [None if (e in (None, 0) or p is None) else p / e * 100
                          for p, e in zip(npp, eq)]
    df["资产负债率_pct"] = [None if (a in (None, 0) or d is None) else d / a * 100
                            for d, a in zip(debt, assets)]
    df["经营现金流_净利润"] = [_safe_div(o, p) for o, p in zip(ocf, npp)]
    # 单季值：同财年内累计值做差
    order = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4}
    df["_seq"] = df["fiscal_period"].map(lambda x: order.get(str(x), 0))
    df["_prev_cum"] = df.groupby("fiscal_year")["operating_income"].shift(1)
    df["单季营收"] = [r if s <= 1 or p is None else (None if r is None else r - p)
                      for r, s, p in zip(rev, df["_seq"], df["_prev_cum"])]
    # 单季归母净利（同财年相邻累计期相减）——估值历史分位要用它算 TTM EPS
    df["_prev_cum_np"] = df.groupby("fiscal_year")["parent_holder_net_profit"].shift(1)
    df["单季净利"] = [p if s <= 1 or q is None else (None if p is None else p - q)
                      for p, s, q in zip(npp, df["_seq"], df["_prev_cum_np"])]
    # 同比：同 fiscal_period 的上一财年
    df["_rev_ly"] = df.groupby("fiscal_period")["operating_income"].shift(1)
    df["_np_ly"] = df.groupby("fiscal_period")["parent_holder_net_profit"].shift(1)
    df["_gm_ly"] = df.groupby("fiscal_period")["毛利率_pct"].shift(1)
    df["营收同比_pct"] = [None if (b in (None, 0) or a is None) else (a - b) / abs(b) * 100
                          for a, b in zip(rev, df["_rev_ly"])]
    df["净利同比_pct"] = [None if (b in (None, 0) or a is None) else (a - b) / abs(b) * 100
                          for a, b in zip(npp, df["_np_ly"])]
    df["毛利率同比_pp"] = [None if (a is None or b is None) else a - b
                           for a, b in zip(df["毛利率_pct"], df["_gm_ly"])]
    df = df.drop(columns=[c for c in df.columns if c.startswith("_")])
    # 派生列在样本不足处会产生 NaN；统一转 None，避免 json 里出现 NaN、md 里出现 "nan"
    df = df.astype(object).where(pd.notnull(df), None)
    return df.to_dict("records"), None


def fund_tags(wide, valuation):
    """派生宽表 + 估值 → 中性事实标注。"""
    t = {}
    if not wide:
        return t
    last = wide[-1]
    r = TAG_RULES
    yoy = last.get("营收同比_pct")
    if yoy is None:
        t["营收同比"] = "无同比口径（首期或缺失）"
    elif yoy >= r["revenue_yoy_high"]:
        t["营收同比"] = "高增 %.2f%%（≥%.0f%%）" % (yoy, r["revenue_yoy_high"])
    elif yoy <= r["revenue_yoy_down"]:
        t["营收同比"] = "下滑 %.2f%%（≤%.0f%%）" % (yoy, r["revenue_yoy_down"])
    else:
        t["营收同比"] = "%.2f%%（区间内）" % yoy
    np_yoy = last.get("净利同比_pct")
    t["净利同比"] = "-" if np_yoy is None else "%.2f%%" % np_yoy
    gm, gm_pp = last.get("毛利率_pct"), last.get("毛利率同比_pp")
    if gm is None:
        t["毛利率"] = "-"
    elif gm_pp is None:
        t["毛利率"] = "%.2f%%（无同比口径）" % gm
    else:
        t["毛利率"] = "%.2f%%（同比 %+.2fpp）" % (gm, gm_pp)
    dr = last.get("资产负债率_pct")
    if dr is None:
        t["资产负债率"] = "-"
    elif dr >= r["debt_ratio_high"]:
        t["资产负债率"] = "%.2f%%（偏高 ≥%.0f%%）" % (dr, r["debt_ratio_high"])
    elif dr <= r["debt_ratio_low"]:
        t["资产负债率"] = "%.2f%%（偏低 ≤%.0f%%）" % (dr, r["debt_ratio_low"])
    else:
        t["资产负债率"] = "%.2f%%（中等区间）" % dr
    q = last.get("经营现金流_净利润")
    if q is None:
        t["现金流质量"] = "-"
    elif q >= r["ocf_quality_good"]:
        t["现金流质量"] = "经营现金流/净利润 %.2f（≥1，覆盖充分）" % q
    elif q <= r["ocf_quality_weak"]:
        t["现金流质量"] = "经营现金流/净利润 %.2f（≤0.5，覆盖偏弱）" % q
    else:
        t["现金流质量"] = "经营现金流/净利润 %.2f（中间区间）" % q
    if valuation:
        pe, pb = valuation.get("pe_ttm"), valuation.get("pb_mrq")
        t["估值"] = "PE(TTM) %s / PB %s（当前快照，接口无历史序列，故不做分位）" % (
            fmt_n(pe), fmt_n(pb))
    return t


def build_fund_derived(wide_q, wide_a, detail):
    """由宽表 + F10 明细节算出清单要求的派生指标（可算的算，不可算的留 None）。"""
    out = {}
    if wide_q:
        last = wide_q[-1]
        rev = num(last.get("operating_income"))
        cost = num(last.get("operating_costs"))
        npp = num(last.get("parent_holder_net_profit"))
        eq = num(last.get("holder_equity_total"))
        debt = num(last.get("total_debt"))
        assets = num(last.get("assets_total"))

        def pct(a, b):
            r = _safe_div(a, b)
            return None if r is None else round(r * 100, 4)
        out["环比"] = {}
        if len(wide_q) >= 2:
            prev = wide_q[-2]
            # 环比用「单季值」对比（累计值直接相除会把 H1/Q1 这种口径差当成环比）
            sq, sq_prev = num(last.get("单季营收")), num(prev.get("单季营收"))
            sn, sn_prev = num(last.get("单季净利")), num(prev.get("单季净利"))
            out["环比"] = {
                "本期单季营收": sq, "上期单季营收": sq_prev,
                "营收环比_pct": pct(sq - sq_prev, abs(sq_prev) if sq_prev else None)
                                if (sq is not None and sq_prev is not None) else None,
                "本期单季净利": sn, "上期单季净利": sn_prev,
                "净利环比_pct": pct(sn - sn_prev, abs(sn_prev) if sn_prev else None)
                                if (sn is not None and sn_prev is not None) else None,
                "口径": "单季值环比（单季值由同财年相邻累计期相减得到）"}
        # 连续单季增速环比变化（最多 4 个点）
        seq = []
        for row in wide_q[-6:]:
            seq.append({"报告期": "%s%s" % (row.get("fiscal_year"), row.get("fiscal_period")),
                        "单季营收": num(row.get("单季营收")), "单季净利": num(row.get("单季净利"))})
        for i, x in enumerate(seq):
            if x["单季营收"] is not None and i >= 4:
                base = seq[i - 4]["单季营收"]
                x["单季营收同比_pct"] = pct(x["单季营收"] - base, abs(base) or None)
            if x["单季净利"] is not None and i >= 4:
                base = seq[i - 4]["单季净利"]
                x["单季净利同比_pct"] = pct(x["单季净利"] - base, abs(base) or None)
        out["单季序列"] = seq
        # 3 年 CAGR：年报轨回溯 3 期；不足则用季报轨回溯 12 期
        if wide_a and len(wide_a) >= 4:
            src, step, src_name = wide_a, 3, "年报"
        else:
            src, step, src_name = wide_q, 12, "季报"
        out["CAGR"] = {}
        for label, key in (("营收", "operating_income"), ("归母净利", "parent_holder_net_profit")):
            if len(src) > step and num(src[-1].get(key)) and num(src[-1 - step].get(key)):
                a, b = num(src[-1][key]), num(src[-1 - step][key])
                if a > 0 and b > 0:
                    out["CAGR"]["%s近3年CAGR_pct" % label] = round(((a / b) ** (1.0 / 3.0) - 1) * 100, 4)
                    out["CAGR"]["%s基期" % label] = "%s%s" % (src[-1 - step].get("fiscal_year"),
                                                            src[-1 - step].get("fiscal_period", ""))
        out["CAGR"]["口径"] = ("用%s轨 %s→%s 的 3 年跨度年化（(末/基)^(1/3)-1）"
                            % (src_name, "%s%s" % (src[-1 - step].get("fiscal_year"),
                                                   src[-1 - step].get("fiscal_period", "")),
                               "%s%s" % (src[-1].get("fiscal_year"),
                                         src[-1].get("fiscal_period", ""))))
        out["ROE序列"] = [{"报告期": "%s%s" % (r.get("fiscal_year"), r.get("fiscal_period")),
                           "ROE_累计_pct": num(r.get("ROE_pct_累计"))} for r in (wide_a or wide_q)[-6:]]
    # F10 明细节派生
    bs = {r.get("报告期"): r for r in (detail or {}).get("balance_quarterly", [])}
    iss = {r.get("报告期"): r for r in (detail or {}).get("income_quarterly", [])}
    cfs = {r.get("报告期"): r for r in (detail or {}).get("cash_quarterly", [])}
    if bs and iss:
        latest = max(set(bs) & set(iss))
        b, i = bs[latest], iss[latest]
        rev = num(i.get("营业总收入")) or num(i.get("营业收入"))
        ded = num(i.get("扣非归母净利润"))
        par = num(i.get("归母净利润"))
        cash = num(b.get("货币资金"))
        tfa = num(b.get("交易性金融资产"))
        interest_debt = sum(x for x in (num(b.get("短期借款")), num(b.get("长期借款")),
                                        num(b.get("应付债券")),
                                        num(b.get("一年内到期的非流动负债"))) if x)
        out["资产负债明细"] = {
            "报告期": latest, "类现金总额": (cash or 0) + (tfa or 0) if (cash or tfa) else None,
            "有息负债总额": round(interest_debt, 2) if interest_debt else None,
            "类现金_有息负债": round(((cash or 0) + (tfa or 0)) / interest_debt, 4)
                              if interest_debt and (cash or tfa) else None,
            "流动比率": None, "速动比率": None,
            "口径": "有息负债=短期借款+长期借款+应付债券+一年内到期的非流动负债"}
        if num(b.get("流动资产合计")) and num(b.get("流动负债合计")):
            out["资产负债明细"]["流动比率"] = round(num(b["流动资产合计"]) / num(b["流动负债合计"]), 4)
            inv = num(b.get("存货")) or 0
            out["资产负债明细"]["速动比率"] = round(
                (num(b["流动资产合计"]) - inv) / num(b["流动负债合计"]), 4)
        out["扣非"] = {
            "报告期": latest, "扣非归母净利润": ded, "归母净利润": par,
            "非经常性损益": (par - ded) if (par is not None and ded is not None) else None,
            "非经常性损益占净利_pct": round((par - ded) / abs(par) * 100, 4)
                                    if (par and ded is not None) else None,
            "扣非净利率_pct": round(ded / rev * 100, 4) if (ded is not None and rev) else None}
        fees = {}
        for label, key in (("销售费用率_pct", "销售费用"), ("管理费用率_pct", "管理费用"),
                           ("研发费用率_pct", "研发费用"), ("财务费用率_pct", "财务费用")):
            v = num(i.get(key))
            fees[label] = round(v / rev * 100, 4) if (v is not None and rev) else None
        fees["报告期"] = latest
        out["期间费用率"] = fees
        # 周转天数（按累计期折算：Q1 90天 / H1 180 / Q3 270 / FY 360）
        days = {"03-31": 90, "06-30": 180, "09-30": 270, "12-31": 360}.get(latest[5:], 360)
        ar = num(b.get("应收票据及应收账款")) or num(b.get("应收账款"))
        inv = num(b.get("存货"))
        if ar and rev:
            out["应收账款周转天数"] = round(ar / rev * days, 2)
        if inv and rev:
            out["存货周转天数"] = round(inv / rev * days, 2)
        if cfs:
            c = cfs.get(latest) or {}
            sales_cash = num(c.get("销售商品提供劳务收到的现金"))
            out["营收现金含量"] = round(sales_cash / rev, 4) if (sales_cash and rev) else None
        if num(i.get("营业利润")) and interest_debt:
            nopat = num(i.get("营业利润")) * (1 - 0.15)   # 简化税率 15%（高新技术企业常见）
            out["ROIC_pct"] = round(nopat / (interest_debt + (num(b.get("归母股东权益合计")) or 0)) * 100, 4)
            out["ROIC_口径"] = "简化口径：营业利润×(1-15%) ÷（有息负债+归母权益），未做税盾细分"
    return out


def collect_fund(ctx, entry, skip=frozenset(), kl_rows=None, kline_max=0):
    """单标的基本面：季报+年报双轨三报表 + 最近期指标 + 估值快照 + 派生宽表。
    ETF/指数显式标注不适用（不伪造数据）。"""
    code = entry["code"]
    res = {"code": code, "name": entry["name"], "type": entry["type"],
           "target_date": ctx.target.strftime("%Y-%m-%d"),
           "fetched_at": now_local().strftime("%Y-%m-%dT%H:%M:%S"),
           "applicable": True, "sources": {}, "degrade": []}
    if entry["type"] != "stock":
        res["applicable"] = False
        res["reason"] = ("%s 无基本面维度（%s/指数不适用财务报表口径）"
                         % (KIND_CN.get(entry["type"], entry["type"]),
                            "ETF" if entry["type"] == "fund" else "指数"))
        ctx.mark(code, "fund", "SKIP", "非个股，基本面不适用")
        return res
    if pd is None:
        res["applicable"] = False
        res["reason"] = "pandas 未安装：pip install pandas"
        fail_msg(ctx, "fund", code, "pandas 未安装（pip install pandas）")
        return res
    stmts, degrade = {}, []
    for stmt in ("income", "balance-sheet", "cash-flow"):
        q_rows, q_err = _fin_stmt_rows(ctx, entry, stmt, "quarterly", FIN_QUARTERS)
        a_rows, a_err = _fin_stmt_rows(ctx, entry, stmt, "annual", FIN_YEARS)
        stmts[stmt] = {"quarterly": q_rows, "annual": a_rows}
        if q_err:
            degrade.append(q_err)
        if a_err:
            degrade.append(a_err)
        res["sources"]["%s(季)" % stmt] = "OK(%d)" % len(q_rows) if q_rows else "FAIL: " + str(q_err)
        res["sources"]["%s(年)" % stmt] = "OK(%d)" % len(a_rows) if a_rows else "FAIL: " + str(a_err)
    inc = stmts["income"]
    wide_q, w_err = build_fund_wide(inc["quarterly"], stmts["balance-sheet"]["quarterly"],
                                    stmts["cash-flow"]["quarterly"])
    wide_a, wa_err = build_fund_wide(inc["annual"], stmts["balance-sheet"]["annual"],
                                     stmts["cash-flow"]["annual"])
    if w_err:
        degrade.append("季报宽表: " + w_err)
    if wa_err:
        degrade.append("年报宽表: " + wa_err)
    ind, ind_err = fetch_indicators(ctx, entry)
    res["sources"]["indicators"] = ("OK(%s)" % ind["report"]) if ind else "FAIL: " + str(ind_err)
    if ind_err:
        degrade.append("指标: " + ind_err)
    val_row = None
    r = invoke(ctx, ["valuation", "snapshot"], ["--thscodes", code])
    if r["ok"]:
        rows = pick_rows(r["payload"], "thscode")
        val_row = next((x for x in rows if x.get("thscode") == code), rows[0] if rows else None)
        res["sources"]["valuation"] = "OK" if val_row else "FAIL: 返回空"
    else:
        res["sources"]["valuation"] = "FAIL: " + str(r.get("error"))[:150]
        degrade.append("估值: " + str(r.get("error"))[:150])
    valuation = None
    if val_row:
        valuation = {k: num(val_row.get(k)) for k in
                     ("pe_ttm", "pe_mrq", "pb_mrq", "ps_ttm", "pcf_ttm")}
        valuation["name"] = val_row.get("name") or ""
    res["valuation"] = valuation
    res["valuation_note"] = "估值端点仅提供当前快照，无历史序列；故只给当期值，不做历史分位。"
    res["indicators"] = flatten_indicators(ind["data"]) if ind else {}
    res["indicators_report"] = ind["report"] if ind else None
    res["indicators_tried"] = ind["tried"] if ind else []
    res["quarterly"] = wide_q
    res["annual"] = wide_a
    res["period_note"] = ("季报为年初至今累计口径（Q1/Q2=H1/Q3=9M/Q4=全年）；"
                          "单季值由同财年内相邻累计期相减得到；同比取同一财季的上一财年。")
    # ---- 新增：F10 三表明细 + 派生指标 ----
    detail, d_errs, d_cache = fetch_statements_detail(ctx, entry)
    res["statements_detail"] = detail
    res["sources"]["F10明细"] = ("OK(%s)" % ("缓存" if d_cache else "新拉")) if detail else \
        "FAIL: " + "; ".join(d_errs)[:150]
    degrade.extend(d_errs)
    res["derived"] = build_fund_derived(wide_q, wide_a, detail)
    # ---- 新增：业务与行业 ----
    orginfo, o_err = fetch_orginfo(ctx, entry)
    res["business"] = {"行业": orginfo, "主营构成": []}
    res["sources"]["公司概况"] = "OK" if orginfo else "FAIL: " + str(o_err)
    if o_err:
        degrade.append("公司概况: %s" % o_err)
    # ---- 新增：股东与治理 ----
    if "shareholders" not in skip:
        hold_data, hold_src, hold_deg, hold_cache = fetch_shareholders(ctx, entry)
        res["shareholders"] = hold_data
        res["sources"].update(hold_src)
        degrade.extend(hold_deg)
        res["shareholders_from_cache"] = hold_cache
        mainop = hold_data.get("mainop") or []
        if mainop:
            latest_rep = max((x.get("报告期") or "") for x in mainop)
            res["business"]["主营构成"] = [x for x in mainop if x.get("报告期") == latest_rep]
            res["business"]["主营构成口径"] = "取最新报告期（%s）" % latest_rep
    else:
        res["shareholders"] = {"skipped": "由 --skip shareholders 跳过"}
        res["sources"]["股东与治理"] = "SKIPPED"
    # ---- 新增：可比公司 ----
    if "peers" not in skip:
        peers, p_deg, p_cache = fetch_peers(ctx, entry, orginfo,
                                            res["business"].get("主营构成") or [])
        res["peers"] = peers
        res["sources"]["可比公司"] = ("OK(%s, %d家)" % ("缓存" if p_cache else "新拉",
                                                    len(peers.get("公司") or []))
                                    if peers.get("applicable") else
                                    "N/A: " + str(peers.get("reason"))[:100])
        degrade.extend(p_deg)
    else:
        res["peers"] = {"skipped": "由 --skip peers 跳过"}
        res["sources"]["可比公司"] = "SKIPPED"
    # ---- 新增：估值历史分位 ----
    if "valuation-history" not in skip and not kl_rows and entry["type"] == "stock":
        # 只跑基本面时也要有价格序列：走同一降级链（命中缓存则很快）
        kl2 = fetch_kline(ctx, entry, kline_max=kline_max)
        kl_rows = kl2.get("rows")
        if not kl_rows:
            degrade.append("估值分位: 无K线（%s）" % str(kl2.get("error"))[:100])
    if "valuation-history" not in skip and kl_rows:
        vh, vh_err = compute_valuation_history(ctx, entry, kl_rows, wide_q)
        res["valuation_history"] = vh
        res["sources"]["估值分位"] = "OK" if vh else "不可用: " + str(vh_err)
        if vh_err:
            degrade.append("估值分位: %s" % vh_err)
        if vh:
            val = res.get("valuation") or {}
            pe, npp_last = val.get("pe_ttm"), num((wide_q or [{}])[-1].get("parent_holder_net_profit"))
            g = res["derived"].get("CAGR", {}).get("归母净利近3年CAGR_pct")
            if pe and g and g > 0:
                res["valuation"]["PEG"] = round(pe / g, 4)
                res["valuation"]["PEG_口径"] = "PE(TTM) ÷ 归母净利近3年CAGR(%)"
            res["valuation"]["PE静态"] = None
            if wide_a:
                eps_fy = num(wide_a[-1].get("basic_eps"))
                last_close = num(kl_rows[-1].get("close")) if kl_rows else None
                if eps_fy and last_close:
                    res["valuation"]["PE静态"] = round(last_close / eps_fy, 4)
                    res["valuation"]["PE静态_口径"] = "最新收盘价 ÷ %s年度基本每股收益" % wide_a[-1].get("fiscal_year")
    else:
        res["valuation_history"] = {"skipped": "由 --skip valuation-history 跳过或缺少K线"}
        res["sources"]["估值分位"] = "SKIPPED"
    res["unavailable"] = list(UNAVAILABLE["fund"])
    res["tags"] = fund_tags(wide_q, valuation)
    res["degrade"] = degrade
    ok = bool(wide_q or wide_a or res["indicators"])
    note = "降级 %d 项" % len(degrade) if degrade else ""
    ctx.mark(code, "fund", "OK" if ok else "FAIL", note,
             note_ascii="degrade=%d q=%d a=%d" % (len(degrade), len(wide_q or []), len(wide_a or [])))
    return res

# ===== [SEC-13] 消息面：快讯源（全市场池，取一次供全部标的过滤） =====


def src_cls_telegraph(ctx, pages=2):
    """财联社 7×24 电报流（三级降级）。rn>50 或签名不符都会「errno=0 + 空数组」静默失败，
    因此每级都显式校验条数。"""
    items, fail, last_time = [], None, None
    for _ in range(max(1, int(pages))):
        params = dict(CLS_SIGN_APP)
        params["rn"] = str(CLS_RN_MAX)
        if last_time:
            params["refresh_type"] = "1"
            params["last_time"] = str(last_time)
        raw = http_get_bytes(_cls_signed_url(CLS_ROLL_URL, params),
                             headers=CLS_HEADERS, timeout=15, tries=2)
        if not raw:
            break
        try:
            d = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            break
        if str(d.get("errno")) not in ("0", "None"):
            fail = "接口 errno=%s msg=%s" % (d.get("errno"), d.get("msg"))
            break
        batch = _cls_roll_items(d.get("data"))
        if not batch:
            break
        items.extend(batch)
        cts = [int(x["ts"]) for x in batch if x.get("ts")]
        last_time = min(cts) if cts else None
        if not last_time:
            break
        time.sleep(0.5)
    if not items:
        raw = http_get_bytes(CLS_CACHE_URL, headers=CLS_HEADERS, timeout=12, tries=2)
        if raw:
            try:
                items = _cls_roll_items(json.loads(raw.decode("utf-8", "replace")).get("data"))
            except ValueError:
                items = []
        if items:
            fail = "签名接口不可用，已降级 /api/cache（固定20条，不可翻页）"
    if not items:
        raw = http_get_bytes(CLS_TG_SSR_URL, headers=CLS_HEADERS, timeout=15, tries=2)
        if raw:
            nd = _next_data_extract(raw.decode("utf-8", "replace"))
            if nd:
                props = nd.get("props") or {}
                st = props.get("initialState") or props.get("pageProps") or {}
                items = _cls_roll_items(st)
        if items:
            fail = "签名与 cache 均不可用，已降级 m.cls.cn/telegraph SSR"
    if not items:
        return [], fail or "三级路径均无数据（接口改版?）"
    return items, fail


def src_cls_hot(ctx):
    """财联社热榜（m.cls.cn 首页 SSR 内嵌数据，零签名）。"""
    items = []
    try:
        raw = http_get_bytes(CLS_HOT_URL, headers={"Referer": "https://m.cls.cn/"},
                             timeout=12, tries=2)
        if not raw:
            return [], "页面请求失败"
        state = _next_data_extract(raw.decode("utf-8", "replace"))
        if not state:
            return [], "页面未包含内嵌数据"
        pp = (state.get("props") or {}).get("pageProps") or {}
        now = int(time.time())
        seen = set()

        def add(iid, title, content, ts):
            if iid in seen:
                return
            seen.add(iid)
            items.append(make_item("财联社热榜", iid, title, content, ts, ""))

        for b in pp.get("hotPlate") or []:
            up = "、".join(s.get("secu_name", "") for s in (b.get("up_stock") or [])
                           if s.get("secu_name"))
            add("plate:%s" % b.get("secu_code"), "【板块】%s" % b.get("secu_name", ""),
                "涨跌幅 %s%% | 领涨股: %s" % (b.get("change", "-"), up or "-"), now)
        for s in pp.get("hotSubject") or []:
            add("subject:%s" % s.get("id"), "【题材】%s" % s.get("name", ""),
                s.get("description", "") or s.get("newest_article_title", ""),
                s.get("create_time") or now)
        for c in (pp.get("recommendSubjectData") or {}).get("today_chances") or []:
            add("chance:%s" % c.get("subject_id"), "【机会】%s" % c.get("subject_name", ""),
                c.get("article_name", ""), c.get("article_time") or now)
        arts = list(pp.get("hotArticleData") or []) + list(pp.get("depth_list") or [])
        arts += list((pp.get("assembleData") or {}).get("top_article") or [])
        for a in arts:
            add("article:%s" % a.get("id"), a.get("title", ""),
                a.get("brief", "") or "", a.get("ctime") or now)
        return items, None
    except Exception as e:
        return items, "解析失败: %s" % e


def src_wallstcn(ctx):
    """华尔街见闻 7×24（a-stock 国内频道 + global 全球频道，按 id 去重）。"""
    items, seen, ok = [], set(), False
    for url in WALLSTCN_URLS:
        for ch in WALLSTCN_CHANNELS:
            try:
                r = http_get(url, params={"channel": ch, "client": "pc", "cursor": 0,
                                          "limit": 40}, headers=WALLSTCN_HEADERS, timeout=12)
                lives = (r.json().get("data") or {}).get("items") or []
            except Exception:
                continue
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
    return items, (None if ok else "主/备 host 均请求失败")


def src_jin10(ctx):
    """金十数据闪电快讯。"""
    items = []
    try:
        r = http_get(JIN10_URL, params={"channel": "-8200", "vip": "1"},
                     headers=JIN10_HEADERS, timeout=12)
        for n in r.json().get("data") or []:
            content = clean_html((n.get("data") or {}).get("content", ""))
            if not content:
                continue
            items.append(make_item("金十数据", n.get("id"), content[:40], content,
                                   _parse_dt_ts(n.get("time")) or int(time.time()),
                                   "https://www.jin10.com/"))
        if items:
            return items, None
        return items, "官方接口返回空"
    except Exception as e:
        return items, "请求失败: %s" % e


def src_ths(ctx):
    """同花顺 7×24 实时新闻。"""
    items = []
    try:
        r = http_get(THS_URL, params={"page": 1, "tag": "", "track": "website",
                                      "pageSize": 50}, headers=THS_HEADERS, timeout=15)
        data = r.json()
        if str(data.get("code")) != "200":
            return items, "接口返回异常 code=%s" % data.get("code")
        for n in (data.get("data") or {}).get("list") or []:
            items.append(make_item("同花顺", n.get("id"), n.get("title", ""),
                                   n.get("digest", "") or "",
                                   n.get("rtime") or n.get("ctime"), n.get("url", "")))
        return items, (None if items else "接口返回空列表")
    except Exception as e:
        return items, "请求失败: %s" % e


def src_sina(ctx):
    """新浪 7×24 直播（干净 JSON 接口 zhibo feed）。"""
    items = []
    try:
        r = http_get(SINA_24H_URL, params={"page": 1, "page_size": 30, "zhibo_id": 152,
                                           "tag_id": 0, "dire": "f", "dpc": 1}, timeout=12)
        lst = ((((r.json().get("result") or {}).get("data") or {}).get("feed") or {}
                ).get("list")) or []
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
        return items, (None if items else "接口返回空列表")
    except Exception as e:
        return items, "请求失败: %s" % e


def src_em_flash(ctx):
    """东方财富 7×24 快讯。
    req_trace 必填——实测漏掉会返回「code=0 + 空数组」的静默失败，
    故这里空结果一律报 FAIL，绝不假装该源无数据。"""
    raw = http_get_bytes(EM_FLASH_URL, params=EM_FLASH_PARAMS,
                         headers=EM_FLASH_HEADERS, timeout=15, tries=2)
    if not raw:
        return [], "接口请求失败（重试耗尽）"
    try:
        d = json.loads(raw.decode("utf-8", "replace"))
    except ValueError as e:
        return [], "响应非 JSON: %s" % e
    # 实测该接口的行键在 list / fastNewsList 之间变动（2026-09-11 实测为 fastNewsList），
    # 两个都收；且此处 code="1" 表示成功，不能按 code!=0 判失败。
    node = d.get("data") or {}
    rows = node.get("fastNewsList") or node.get("list") or []
    if not rows:
        return [], ("code=%s message=%s 但行数组为空（该接口的静默失败，多为 req_trace 缺失或被拒）"
                    % (d.get("code"), d.get("message")))
    items = []
    for n in rows:
        title = clean_html(n.get("title") or "")
        content = clean_html(n.get("summary") or "")
        if not title and not content:
            continue
        items.append(make_item("东财快讯", n.get("code") or title[:24], title or content[:40],
                               content, _parse_dt_ts(n.get("showTime")),
                               "https://kuaixun.eastmoney.com/"))
    return items, (None if items else "全部条目为空")


# (显示名, ASCII 标签, 取数函数) —— ASCII 标签用于 stdout（中文会被 GBK 管道吃掉）
NEWS_SOURCES = (
    ("财联社电报", "cls-telegraph", src_cls_telegraph),
    ("财联社热榜", "cls-hot", src_cls_hot),
    ("华尔街见闻", "wallstcn", src_wallstcn),
    ("金十数据", "jin10", src_jin10),
    ("同花顺", "10jqka", src_ths),
    ("新浪", "sina", src_sina),
    ("东财快讯", "em-flash", src_em_flash),
)


def collect_flash_pool(ctx):
    """全市场快讯池：每源取一次，返回 ({src: [items]}, {src: fail}, ok_count)。"""
    pool, fails, ok = {}, {}, 0
    for name, label, fn in NEWS_SOURCES:
        try:
            items, err = fn(ctx)
        except Exception as ex:
            items, err = [], "异常: %s" % ex
        pool[name] = items
        fails[name] = err
        if items:
            ok += 1
        detail = ("%d items" % len(items)) if items else str(err or "0 items")[:110]
        say("%-8s news-pool %-14s %s" % ("[OK]" if items else "[FAIL]", label, detail))
    return pool, fails, ok


def news_keys(entry):
    """标的 → 快讯匹配关键词（名称全称 / 名称前4字 / 裸代码）。"""
    keys = set()
    name = (entry.get("name") or "").strip()
    if name and name != entry["code"]:
        keys.add(name)
        if len(name) >= 5:
            keys.add(name[:4])
    keys.add(entry["code"][:6])
    return sorted(k for k in keys if k)


def filter_flash(pool, entry, top):
    """全市场池 → 该标的命中条目（附命中词），按时间倒序取 top 条。"""
    keys = news_keys(entry)
    hits = []
    for src, items in pool.items():
        for it in items:
            text = "%s %s" % (it.get("title", ""), it.get("content", ""))
            matched = [k for k in keys if k in text]
            if not matched:
                continue
            x = dict(it)
            x["命中词"] = matched
            x["时间"] = ts_to_str(it.get("ts"))
            hits.append(x)
    hits.sort(key=lambda x: x.get("ts") or 0, reverse=True)
    dedup, seen = [], set()
    for h in hits:
        sig = (h.get("title") or h.get("content") or "")[:40]
        if sig in seen:
            continue
        seen.add(sig)
        dedup.append(h)
    return dedup[:top], keys

# ===== [SEC-14] 消息面：公告与官方端点 =====


def cninfo_org_id(ctx, code):
    """巨潮 topSearch → orgId（公告按 stock=<code>,<orgId> 精确过滤必需）。"""
    try:
        r = http_post(CNINFO_TOPSEARCH_URL, data={"keyWord": code, "maxNum": "10"},
                      headers=CNINFO_HEADERS, timeout=15)
        hits = r.json()
        if isinstance(hits, list):
            for h in hits:
                if isinstance(h, dict) and str(h.get("code")) == code and h.get("orgId"):
                    return str(h["orgId"]), None
        return None, "topSearch 未命中 orgId"
    except Exception as e:
        return None, "topSearch 失败: %s" % e


def notice_category(title, atype=""):
    """公告 → 中性事件分类（规则驱动，只做归类，不含判断）。"""
    s = "%s %s" % (title or "", atype or "")
    rules = (
        ("风险公告", ("问询", "监管", "立案", "调查", "警示", "退市", "下修", "诉讼", "处罚",
                     "风险提示", "违规", "停牌", "计提减值", "商誉减值")),
        ("资本运作", ("并购", "重组", "收购", "定增", "非公开发行", "可转债", "募集资金",
                     "股权激励", "股票期权", "限制性股票", "行权", "激励计划")),
        ("股东动作", ("减持", "增持", "回购", "质押", "解除限售", "限售", "权益变动",
                     "实际控制人", "控股股东", "股东")),
        ("分红与权益", ("分红", "派息", "权益分派", "除权除息", "送股", "转增", "利润分配")),
        ("业绩与财报", ("业绩预告", "业绩快报", "年度报告", "半年度报告", "季度报告",
                        "年报", "半年报", "季报", "审计报告", "会计政策", "营业收入")),
        ("经营与合同", ("中标", "合同", "订单", "产能", "扩产", "新产品", "技术", "专利",
                        "对外投资", "项目")),
        ("公司治理", ("董事会", "监事会", "股东大会", "章程", "高级管理人员", "独立董事")),
    )
    for name, keys in rules:
        if any(k in s for k in keys):
            return name
    return "其他"


def fetch_notices(ctx, entry):
    """巨潮公告：单次拉近 NOTICE_DAYS 天（不做阶梯放宽，避免窄窗口把真实公告切掉）。
    窗口内另标注「近 7 天」条数，便于快速判断新鲜度。"""
    if entry["type"] != "stock":
        return {"window": None, "items": [], "fail": None, "applicable": False,
                "note": "%s 不走巨潮个股公告口径" % KIND_CN.get(entry["type"], entry["type"])}
    code = entry["code"][:6]
    org, org_err = cninfo_org_id(ctx, code)
    if not org:
        return {"window": None, "items": [], "fail": org_err, "applicable": True, "note": ""}
    frm = (ctx.target - datetime.timedelta(days=NOTICE_DAYS - 1)).strftime("%Y-%m-%d")
    win = "%s~%s" % (frm, ctx.target.strftime("%Y-%m-%d"))
    recent_from = (ctx.target - datetime.timedelta(days=6)).strftime("%Y-%m-%d")
    items, seen, fails = [], set(), []
    for col in ("szse", "sse"):
        form = {"pageNum": "1", "pageSize": str(NOTICE_PAGE_SIZE), "column": col,
                "tabName": "fulltext", "plate": "", "stock": "%s,%s" % (code, org),
                "searchkey": "", "secid": "", "category": "", "trade": "", "seDate": win,
                "sortName": "", "sortType": "", "isHLtitle": "true"}
        try:
            r = http_post(CNINFO_QUERY_URL, data=form, headers=CNINFO_HEADERS, timeout=20)
            for a in r.json().get("announcements") or []:
                if str(a.get("secCode")) != code:
                    continue
                iid = str(a.get("announcementId") or a.get("adjunctUrl") or "")
                if iid in seen:
                    continue
                seen.add(iid)
                day = ms_to_date(a.get("announcementTime"))
                atype = a.get("announcementTypeName") or a.get("typeName") or ""
                title = a.get("announcementTitle", "")
                age = None
                dd = parse_date(day)
                if dd:
                    age = (ctx.target - dd).days
                items.append({
                    "标题": title, "类型": atype, "时间": day,
                    "距今_天": age,
                    "事件分类": notice_category(title, atype),
                    "近7天": bool(day and day >= recent_from),
                    "url": ("http://static.cninfo.com.cn/" + (a.get("adjunctUrl") or "")),
                })
        except Exception as e:
            fails.append("巨潮 [%s] %s" % (col, e))
        time.sleep(0.3)
    items.sort(key=lambda x: x.get("时间") or "", reverse=True)
    fail = "; ".join(fails) if fails else None
    if not items:
        return {"window": win, "items": [], "fail": fail, "applicable": True,
                "note": "近 %d 天无公告" % NOTICE_DAYS}
    return {"window": win, "items": items, "fail": fail, "applicable": True,
            "note": "近 %d 天共 %d 条公告，其中近 7 天 %d 条"
                    % (NOTICE_DAYS, len(items), sum(1 for x in items if x.get("近7天")))}


def fetch_anomaly(ctx, entry):
    """当日异动原因（hithink special.anomaly-stock，复数 --thscodes）。
    当日无数据属正常（该端点 only-today），空结果按 OK(0) 记录。"""
    if entry["type"] != "stock":
        return None, None
    r = invoke(ctx, ["special", "anomaly-stock"], ["--thscodes", entry["code"]])
    if not r["ok"]:
        return None, str(r.get("error"))[:160]
    rows = pick_rows(r["payload"])
    out = []
    for x in rows:
        out.append({k: v for k, v in x.items() if not isinstance(v, (list, dict))})
    return out, None


def fetch_hot_rank(ctx, entry):
    """当前热榜 + 近 30 日排名走势。热榜为全市场榜，需按代码定位。"""
    out = {"current": None, "trend": [], "fail": None, "applicable": True, "note": ""}
    if entry["type"] != "stock":
        # 「不适用」不是失败：ETF/指数不进个股热榜，标 applicable=False，不计入降级
        out["applicable"] = False
        out["note"] = "%s 不适用个股热榜口径" % KIND_CN.get(entry["type"], entry["type"])
        return out
    # 命令名为 hot-stock（不是 hot-stock-list；端点 id 才叫 hot-stock-list）
    r = invoke(ctx, ["special", "hot-stock"], ["--period", "day"])
    if r["ok"]:
        rows = pick_rows(r["payload"], "thscode")
        hit = next((x for x in rows if x.get("thscode") == entry["code"]), None)
        if hit is None and rows:
            out["current"] = {"in_list": False, "list_size": len(rows),
                              "note": "未进入当前热榜"}
        elif hit:
            out["current"] = {k: v for k, v in hit.items() if not isinstance(v, (list, dict))}
            out["current"]["in_list"] = True
    else:
        out["fail"] = "热榜: " + str(r.get("error"))[:120]
    ed = ctx.target.strftime("%Y-%m-%d")
    sd = (ctx.target - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
    r2 = invoke(ctx, ["special", "hot-stock-trend"],
                ["--thscode", entry["code"], "--start-date", sd, "--end-date", ed])
    if r2["ok"]:
        tr = []
        for x in pick_rows(r2["payload"], "rank"):
            tr.append({"日期": ms_to_date(x.get("date_ms")) or str(x.get("date") or ""),
                       "排名": num(x.get("rank"))})
        tr.sort(key=lambda x: x["日期"])
        out["trend"] = tr
    else:
        out["fail"] = ((out["fail"] + " | ") if out["fail"] else "") + \
            "热度趋势: " + str(r2.get("error"))[:120]
    return out


def fetch_lhb_history(ctx, entry):
    """近 LHB_DAYS 天龙虎榜上榜记录（东财 datacenter 按 SECURITY_CODE 单次过滤，含上榜原因）。"""
    if entry["type"] != "stock":
        return {"items": [], "fail": None, "note": "非个股，不适用龙虎榜"}
    code = entry["code"][:6]
    frm = (ctx.target - datetime.timedelta(days=LHB_DAYS)).strftime("%Y-%m-%d")
    d = _eastmoney_get([DC_URL], {
        "reportName": LHB_MAIN_RPT, "columns": "ALL",
        "filter": '(SECURITY_CODE="%s")(TRADE_DATE>=\'%s\')' % (code, frm),
        "pageSize": "50", "pageNumber": "1", "sortColumns": "TRADE_DATE", "sortTypes": "-1",
        "source": "WEB", "client": "WEB"}, tries=3, base_sleep=1.0, timeout=15)
    if d is None:
        return {"items": [], "fail": "东财 datacenter 请求失败", "note": ""}
    rows = (d.get("result") or {}).get("data") or []
    items = []
    for x in rows:
        items.append({
            "日期": str(x.get("TRADE_DATE") or "")[:10],
            "名称": x.get("SECURITY_NAME_ABBR", ""),
            "净买_亿": round(_f(x.get("BILLBOARD_NET_AMT")) / 1e8, 4),
            "买入_亿": round(_f(x.get("BILLBOARD_BUY_AMT")) / 1e8, 4),
            "卖出_亿": round(_f(x.get("BILLBOARD_SELL_AMT")) / 1e8, 4),
            "涨跌幅_pct": _f(x.get("CHANGE_RATE")),
            "净占比_pct": _f(x.get("DEAL_NET_RATIO")),
            "上榜原因": str(x.get("EXPLANATION") or "")[:40],
        })
    return {"items": items, "fail": None, "note": ""}


def fetch_seat_boards(ctx, entry):
    """当日机构专用 / 游资席位（hithink dragon-tiger，按 board-type 拉全市场后按代码过滤）。"""
    out = {"org": [], "hot_money": [], "fail": [], "applicable": True, "note": ""}
    if entry["type"] != "stock":
        out["applicable"] = False
        out["note"] = "%s 不适用席位口径" % KIND_CN.get(entry["type"], entry["type"])
        return out
    for bt, key in (("org", "org"), ("hot_money", "hot_money")):
        r = invoke(ctx, ["special", "dragon-tiger"],
                   ["--board-type", bt, "--date", ctx.target.strftime("%Y-%m-%d")])
        if not r["ok"]:
            out["fail"].append("%s: %s" % (bt, str(r.get("error"))[:110]))
            continue
        rows = [x for x in pick_rows(r["payload"]) if str(x.get("thscode") or "") == entry["code"]]
        out[key] = [{k: v for k, v in x.items() if not isinstance(v, (list, dict))}
                    for x in rows]
    if not out["org"] and not out["hot_money"] and not out["fail"]:
        out["fail"].append("当日无席位记录（未上榜或非交易日）")
    return out


def news_tags(flash_hits, notices, hot, lhb, keys):
    """消息面中性事实标注（只陈述计数、分布与聚合值）。"""
    t = {}
    t["关键词"] = "、".join(keys)
    t["当日快讯命中"] = "%d 条" % len(flash_hits)
    if flash_hits:
        dist = {}
        for h in flash_hits:
            dist[h.get("src", "?")] = dist.get(h.get("src", "?"), 0) + 1
        t["来源分布"] = "、".join("%s %d" % (k, v) for k, v in
                                 sorted(dist.items(), key=lambda x: -x[1]))
        latest = flash_hits[0]
        t["最新一条"] = "%s %s" % (latest.get("时间", "-"), (latest.get("title") or
                                                          latest.get("content", ""))[:50])
    else:
        t["来源分布"] = "-"
    if not notices:
        t["公告"] = "-"
    elif notices.get("applicable") is False:
        t["公告"] = "不适用"
    else:
        items = notices.get("items") or []
        t["公告事件"] = "近 %d 天 %d 条（近 7 天 %d 条）" % (
            NOTICE_DAYS, len(items), sum(1 for x in items if x.get("近7天")))
        if items:
            cats = {}
            for x in items:
                cats[x.get("事件分类") or "其他"] = cats.get(x.get("事件分类") or "其他", 0) + 1
            t["公告分类"] = "、".join("%s %d" % (k, v) for k, v in
                                     sorted(cats.items(), key=lambda kv: -kv[1]))
    if hot:
        cur = hot.get("current")
        if isinstance(cur, dict) and cur.get("in_list"):
            t["当前热榜"] = "排名 %s" % fmt_n(cur.get("rank"), 0)
        elif isinstance(cur, dict):
            t["当前热榜"] = "未在榜（榜内 %s 只）" % cur.get("list_size")
        tr = hot.get("trend") or []
        if len(tr) >= 2:
            first, last_r = tr[0], tr[-1]
            t["30日热度趋势"] = "排名 %s → %s（%s）" % (
                fmt_n(first.get("排名"), 0), fmt_n(last_r.get("排名"), 0),
                "上升" if (last_r.get("排名") or 0) < (first.get("排名") or 0) else
                ("下降" if (last_r.get("排名") or 0) > (first.get("排名") or 0) else "持平"))
        elif tr:
            t["30日热度趋势"] = "仅 %d 天有记录" % len(tr)
        else:
            t["30日热度趋势"] = "无记录"
    if lhb is not None:
        items = lhb.get("items") or []
        t["30日龙虎榜"] = "上榜 %d 次" % len(items)
        if items:
            net = sum(_f(x.get("净买_亿")) for x in items)
            t["30日龙虎榜净买"] = "%.2f 亿元" % net
    return t

# ===== [SEC-15] 消息面维度组装 =====


def collect_news(ctx, entry, pool, pool_fails, top=DEFAULT_TOP, skip=frozenset()):
    """单标的消息面：快讯过滤 + 公告(含合同/政策代理) + 异动 + 热榜 + 龙虎榜 + 资金情绪。"""
    code = entry["code"]
    res = {"code": code, "name": entry["name"], "type": entry["type"],
           "target_date": ctx.target.strftime("%Y-%m-%d"),
           "fetched_at": now_local().strftime("%Y-%m-%dT%H:%M:%S"),
           "sources": {}, "degrade": []}
    hits, keys = filter_flash(pool, entry, top)
    res["flash"] = hits
    res["flash_keys"] = keys
    res["flash_pool_size"] = sum(len(v) for v in pool.values())
    res["sources"]["快讯池"] = ("OK(%d 源, %d 条, 命中 %d)"
                              % (sum(1 for v in pool.values() if v), res["flash_pool_size"],
                                 len(hits)))
    src_fails = {k: v for k, v in (pool_fails or {}).items() if v}
    if src_fails:
        res["degrade"].extend(["%s: %s" % (k, v) for k, v in src_fails.items()])
    notices = fetch_notices(ctx, entry)
    res["notices"] = notices
    if notices.get("fail"):
        res["degrade"].append("公告: %s" % notices["fail"])
    if notices.get("applicable") is False:
        res["sources"]["公告"] = "N/A"
    elif notices.get("items"):
        res["sources"]["公告"] = "OK(%d)" % len(notices["items"])
    else:
        res["sources"]["公告"] = "OK(0)" if not notices.get("fail") else "FAIL: " + str(notices["fail"])
    anomaly, an_err = fetch_anomaly(ctx, entry)
    res["anomaly"] = anomaly
    res["sources"]["异动原因"] = ("OK(%d)" % len(anomaly)) if anomaly is not None else \
        ("N/A" if entry["type"] != "stock" else "FAIL: " + str(an_err))
    if an_err:
        res["degrade"].append("异动原因: %s" % an_err)
    hot = fetch_hot_rank(ctx, entry)
    res["hot"] = hot
    if hot.get("applicable") is False:
        res["sources"]["热榜"] = "N/A"
    elif hot.get("fail"):
        res["sources"]["热榜"] = "FAIL: " + str(hot.get("fail"))
    else:
        res["sources"]["热榜"] = "OK(趋势%d天)" % len(hot.get("trend") or [])
    if hot.get("fail"):
        res["degrade"].append("热榜: %s" % hot["fail"])
    lhb = fetch_lhb_history(ctx, entry)
    res["lhb"] = lhb
    res["sources"]["龙虎榜"] = ("OK(%d)" % len(lhb.get("items") or [])) if not lhb.get("fail") \
        else "FAIL: " + str(lhb.get("fail"))
    if lhb.get("fail"):
        res["degrade"].append("龙虎榜: %s" % lhb["fail"])
    seats = fetch_seat_boards(ctx, entry)
    res["seats"] = seats
    if seats.get("applicable") is False:
        res["sources"]["席位"] = "N/A"
    else:
        res["sources"]["席位"] = "OK" if (seats.get("org") or seats.get("hot_money")) else "OK(0)"
    # ---- 新增：重大合同/订单/扩产/新产品（公告标题级，无结构化金额） ----
    n_items = notices.get("items") or []
    contracts = [x for x in n_items if x.get("事件分类") == "经营与合同"]
    res["contracts"] = {
        "口径": "从近 %d 天公告标题按「中标/合同/订单/产能/扩产/新产品/技术/专利/项目」"
                "关键词提取；**仅有标题，无合同金额与占营收比例等结构化字段**" % NOTICE_DAYS,
        "条数": len(contracts),
        "列表": [{"日期": x.get("时间"), "标题": x.get("标题"), "url": x.get("url")}
                 for x in contracts]}
    risk = [x for x in n_items if x.get("事件分类") == "风险公告"]
    res["risk_notices"] = {"条数": len(risk),
                           "列表": [{"日期": x.get("时间"), "标题": x.get("标题")}
                                    for x in risk]}
    # ---- 新增：行业政策代理（快讯关键词过滤，弱代理） ----
    ind_name = ""
    try:
        org, _ = fetch_orginfo(ctx, entry)
        ind_name = str(org.get("东财行业") or org.get("证监会行业") or "")
    except Exception:
        pass
    kw = [k for k in re.split(r"[-—/]", ind_name) if k] + ["政策", "规划", "补贴", "监管",
                                                          "指导意见", "通知", "方案", "部委"]
    pol = []
    for src, items in (pool or {}).items():
        for it in items:
            text = "%s %s" % (it.get("title", ""), it.get("content", ""))
            if any(k in text for k in kw[:6]) and any(k in text for k in kw[6:]):
                pol.append({"来源": src, "时间": ts_to_str(it.get("ts")),
                            "标题": (it.get("title") or it.get("content", ""))[:80],
                            "url": it.get("url")})
    res["policy_proxy"] = {
        "口径": "弱代理：用行业名 + 政策关键词在当日快讯池里过滤，非官方政策库，可能漏报误报",
        "关键词": kw, "条数": len(pol), "列表": pol[:top]}
    # ---- 新增：资金情绪 ----
    if "moneyflow" not in skip:
        hold_data, hold_src, hold_deg, _ = fetch_shareholders(ctx, entry)
        seat_detail, sd_err = fetch_seat_detail(ctx, entry, lhb.get("items") or [])
        margin = hold_data.get("margin") or []
        north = hold_data.get("north") or []
        survey = hold_data.get("survey") or []
        recent_survey = [x for x in survey
                         if (parse_date(x.get("调研起") or x.get("公告日")) or ctx.target)
                         >= ctx.target - datetime.timedelta(days=SURVEY_DAYS)]
        orgs = set()
        for x in recent_survey:
            for o in re.split(r"[,，、;；]", str(x.get("接待对象") or "")):
                if o.strip():
                    orgs.add(o.strip()[:40])
        res["moneyflow"] = {
            "两融": {"序列": margin,
                    "融资余额_亿": round(_f((margin[0] if margin else {}).get("融资余额")) / 1e8, 4)
                                  if margin else None,
                    "口径": "东财两融个股明细，序列按日期倒序"},
            "北向持股": {"序列": north, "最新": north[0] if north else None,
                      "口径": "沪深港通持股（净买入自 2024-08 停披露，此处为持股存量）"},
            "机构调研": {"近%d天次数" % SURVEY_DAYS: len(recent_survey),
                      "涉及机构数": len(orgs), "机构名单": sorted(orgs)[:30],
                      "记录": recent_survey[:10],
                      "口径": "接待对象为公告披露文本切分，机构数为主观上界估计"},
            "龙虎榜席位明细": {"条数": len(seat_detail), "列表": seat_detail[:20],
                          "口径": "东财营业部买卖明细，含机构专用席位标识"},
        }
        # 「无上榜日」不是故障（非个股/未上榜都会这样），只有真出错才计入降级
        if sd_err and (lhb.get("items") or []):
            res["degrade"].append("席位明细: %s" % sd_err)
        res["sources"]["两融"] = "OK(%d)" % len(margin) if margin else "FAIL/空"
        res["sources"]["北向持股"] = "OK(%d)" % len(north) if north else "FAIL/空"
        res["sources"]["机构调研"] = "OK(%d)" % len(survey)
        res["sources"]["席位明细"] = "OK(%d)" % len(seat_detail)
        res["moneyflow"]["关注度排名"] = fetch_attention_rank(ctx, entry)
    else:
        res["moneyflow"] = {"skipped": "由 --skip moneyflow 跳过"}
        res["sources"]["资金情绪"] = "SKIPPED"
    res["unavailable"] = list(UNAVAILABLE["news"])
    res["tags"] = news_tags(hits, notices, hot, lhb, keys)
    res["north_note"] = NORTH_NOTE
    ok = bool(hits or (notices.get("items") or notices.get("fail") is None))
    ctx.mark(code, "news", "OK" if ok else "FAIL",
             "命中 %d 条 / 降级 %d 项" % (len(hits), len(res["degrade"])),
             note_ascii="hits=%d pool=%d degrade=%d"
                        % (len(hits), res["flash_pool_size"], len(res["degrade"])))
    return res

# ===== [SEC-16] 交付打包（单 JSON） =====


def day_path(ctx):
    """当日唯一交付文件：<base>/stock3d_<YYYYMMDD>.json。
    不再产出 md，也不产出分维 json —— 交付物就是这一个文件。"""
    return os.path.join(ctx.base, "stock3d_%s.json" % ctx.target.strftime("%Y%m%d"))


def load_day(ctx):
    """读回当日交付文件：部分重跑（如只跑 tech）时用它做合并，
    避免把文件里其它标的、其它维度的既有结果覆盖掉。"""
    d = read_json(day_path(ctx))
    return d if isinstance(d, dict) else {}


def symbol_payload(ctx, entry, tech, fund, news):
    """单标的三维载荷。状态随标的一起存，部分重跑后仍能看清各维成败。"""
    return {"code": entry["code"], "name": entry["name"], "type": entry["type"],
            "asset_type": entry.get("asset_type", ""),
            "status": ctx.status.get(entry["code"], {}),
            "tech": tech, "fund": fund, "news": news}


def build_day_bundle(ctx, dims, symbols, prev, pool_sizes=None):
    """组装当日交付 JSON：运行元信息 + 全部标的 + 本次失败/降级明细。"""
    return {
        "tool": "stock3d",
        "schema_version": "1",
        "target_date": ctx.target.strftime("%Y-%m-%d"),
        "generated_at": now_local().strftime("%Y-%m-%dT%H:%M:%S"),
        "session": session_note(),
        "dims_run": dims,
        "node_calls": ctx.calls,
        "flash_pool_sizes": pool_sizes or {},
        "resolves": ctx.resolves,
        "unresolved": ctx.bad,
        "failures": [{"dim": d, "code": c, "reason": w} for d, c, w in ctx.failures],
        "prev_generated_at": (prev or {}).get("generated_at"),
        "symbols": symbols,
        "note": "全部输出为中性事实与数值，不含买卖建议，不构成投资建议。",
    }

# ===== [SEC-17] 主流程 =====

# ===== [SEC-18] CLI 入口 =====


USAGE_SAMPLES = """examples:
  python stock3d.py pull 002463.SZ 沪电股份      # 三维全出
  python stock3d.py pull --pool <持仓文件>       # 从持仓/自选文件读标的
  python stock3d.py tech 002463.SZ --rt          # 只跑技术面（含盘中分时/五档/分笔）
  python stock3d.py fund 002463.SZ               # 只跑基本面
  python stock3d.py news 002463.SZ --top 50      # 只跑消息面
"""


def _add_common(sp):
    sp.add_argument("tokens", nargs="*", help="标的：thscode / 裸6位码 / 中文名（可多个）")
    sp.add_argument("--pool", metavar="FILE", help="持仓/自选文件（每行一个代码，兼容多列格式）")
    sp.add_argument("--out", metavar="DIR", help="落盘基目录（默认 <脚本目录>/data）")
    sp.add_argument("--date", metavar="YYYY-MM-DD", help="目标交易日（默认今天）")
    sp.add_argument("--no-cache", action="store_true", help="忽略日K缓存，强制重拉")
    sp.add_argument("--debug", action="store_true", help="打印调试信息")
    sp.add_argument("--skip", metavar="A,B", default="",
                    help="跳过的重活模块，逗号分隔：peers, valuation-history, shareholders, "
                         "moneyflow, auction")
    sp.add_argument("--kline-max", type=int, default=0, metavar="N",
                    help="日K最多取 N 根；0=按源能力取最长（默认）")


def build_parser():
    p = argparse.ArgumentParser(
        prog="stock3d.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="A股三维数据拉取（技术面 / 基本面 / 消息面），输出中性事实与数值，不含买卖建议。",
        epilog=USAGE_SAMPLES)
    sub = p.add_subparsers(dest="cmd")
    sp = sub.add_parser("pull", help="三维全出（默认子命令）", epilog=USAGE_SAMPLES,
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    _add_common(sp)
    sp.add_argument("--rt", action="store_true", help="附加盘中分时/五档/分笔")
    sp.add_argument("--top", type=int, default=DEFAULT_TOP, help="每标的消息面保留条数")
    sp.add_argument("--kline-source", choices=["hithink", "em", "sina", "tx"],
                    help="强制指定日K来源（降级演练用，默认自动降级链）")
    for name, help_txt in (("tech", "只跑技术面"), ("fund", "只跑基本面"), ("news", "只跑消息面")):
        s = sub.add_parser(name, help=help_txt, epilog=USAGE_SAMPLES,
                           formatter_class=argparse.RawDescriptionHelpFormatter)
        _add_common(s)
        if name == "tech":
            s.add_argument("--rt", action="store_true", help="附加盘中分时/五档/分笔")
            s.add_argument("--kline-source", choices=["hithink", "em", "sina", "tx"],
                           help="强制指定日K来源（降级演练用）")
        if name == "news":
            s.add_argument("--top", type=int, default=DEFAULT_TOP, help="保留条数")
    return p


SUBCMDS = ("pull", "tech", "fund", "news")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        argv = ["--help"]
    # 便捷派发：首个位置参数不是子命令时，按 pull 处理（python stock3d.py 002463.SZ）
    if argv[0] not in SUBCMDS and not argv[0].startswith("-"):
        argv = ["pull"] + argv
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 0
    args.cmd = args.cmd
    try:
        return cmd_run(args)
    except KeyboardInterrupt:
        say("[FAIL] 已中断")
        return 130

# ===== [SEC-19] 通用缓存与东财底层（datacenter / F10） =====


def cache_path(ctx, kind, key):
    return os.path.join(ctx.base, CACHE_DIRNAME, "%s_%s.json" % (kind, key.replace(":", "_")))


def cached_payload(ctx, kind, key, target_key):
    """按 (kind,key) 读缓存；目标日不一致视为失效（跨日自动重拉）。"""
    if ctx.no_cache:
        return None
    d = read_json(cache_path(ctx, kind, key))
    if isinstance(d, dict) and d.get("target_date") == target_key:
        return d.get("payload")
    return None


def save_cache(ctx, kind, key, target_key, payload):
    write_json(cache_path(ctx, kind, key),
               {"target_date": target_key,
                "saved_at": now_local().strftime("%Y-%m-%dT%H:%M:%S"),
                "payload": payload})


def dc_query(ctx, report, filt=None, page_size=50, sort=None):
    """东财 datacenter 通用查询 → (rows, error)。code=0 才算成功。"""
    params = {"reportName": report, "columns": "ALL", "pageNumber": "1",
              "pageSize": str(page_size), "source": "WEB", "client": "WEB"}
    if filt:
        params["filter"] = filt
    if sort:
        params["sortColumns"], params["sortTypes"] = sort
    d = _eastmoney_get([DC_URL], params, tries=2, base_sleep=1.0, timeout=15)
    if d is None:
        return [], "datacenter 请求失败"
    if str(d.get("code")) not in ("0",):
        return [], "datacenter code=%s %s" % (d.get("code"), str(d.get("message") or "")[:60])
    return (d.get("result") or {}).get("data") or [], None


def f10_code(thscode):
    """thscode → 东财 F10 代码（SZ002463 / SH600519）。"""
    p = (thscode or "").split(".")
    if len(p) != 2 or p[1].upper() not in ("SH", "SZ", "BJ"):
        return None
    return p[1].upper() + p[0]


def period_ends_back(ctx, nq, ny):
    """按目标日回推报告期：季报 nq 期（YYYY-MM-DD）+ 年报 ny 期。"""
    ends = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}
    y, q = report_guess(ctx.target)
    qs, cy, cq = [], y, q
    for _ in range(nq):
        m, d = ends[cq]
        qs.append("%04d-%02d-%02d" % (cy, m, d))
        cy, cq = report_back(cy, cq)
    ys = ["%04d-12-31" % (y - (0 if q == 4 else 1) - i) for i in range(ny)]
    return qs, ys


def fetch_statements_detail(ctx, entry):
    """东财 F10 完整三表明细（季报 FIN_QUARTERS 期 + 年报 FIN_YEARS 期）。"""
    tkey = ctx.target.strftime("%Y-%m-%d")
    hit = cached_payload(ctx, "f10", entry["code"], tkey)
    if hit:
        return hit, [], True
    c6 = f10_code(entry["code"])
    if not c6:
        return None, ["无 F10 代码（仅支持 .SH/.SZ/.BJ）"], False
    qs, ys = period_ends_back(ctx, F10_QUARTERS, FIN_YEARS)
    out = {"季报期": qs, "年报期": ys, "单位": "元",
           "字段说明": "键为中文科目名；接口未返回该科目的记为缺失，不出现在行内"}
    degrade = []
    paths = {"income": "lrbAjaxNew", "balance": "zcfzbAjaxNew", "cash": "xjllbAjaxNew"}
    whitelists = {"income": F10_IS, "balance": F10_BS, "cash": F10_CF}
    for kind, path in paths.items():
        for label, dates in (("quarterly", qs), ("annual", ys)):
            url = "%s%s?companyType=4&reportDateType=0&reportType=1&dates=%s&code=%s" % (
                EM_F10_BASE, path, urllib.parse.quote(",".join(dates)), c6)
            raw = http_get_bytes(url, headers=EM_F10_HEADERS, timeout=20, tries=2)
            rows = []
            err = None
            if not raw:
                err = "请求失败"
            else:
                try:
                    data = json.loads(raw.decode("utf-8", "replace")).get("data") or []
                except ValueError as e:
                    data, err = [], "响应非 JSON: %s" % e
                for r in data:
                    if not isinstance(r, dict):
                        continue
                    item = {"报告期": str(r.get("REPORT_DATE") or "")[:10]}
                    for k, cn in whitelists[kind]:
                        if k in r:
                            v = num(r.get(k))
                            if v is not None:
                                item[cn] = v
                    rows.append(item)
                rows.sort(key=lambda x: x.get("报告期") or "")
            out["%s_%s" % (kind, label)] = rows
            if err or not rows:
                degrade.append("F10 %s(%s): %s" % (kind, label, err or "返回空"))
            time.sleep(0.3)
    save_cache(ctx, "f10", entry["code"], tkey, out)
    return out, degrade, False

# ===== [SEC-20] 股东与治理（fund.shareholders） =====


def _latest_period_rows(rows, date_key):
    """取 END_DATE 最新那一期的全部行（十大股东类报表需要按最期过滤）。"""
    dated = [r for r in rows if r.get(date_key)]
    if not dated:
        return rows
    latest = max(str(r[date_key]) for r in dated)
    return [r for r in dated if str(r[date_key]) == latest]


def fetch_shareholders(ctx, entry):
    """东财 datacenter：十大股东/流通股东/户数/机构持仓/质押/分红送转/业绩预告/解禁/两融/北向/调研。
    返回 (data, sources, degrade, from_cache)。"""
    tkey = ctx.target.strftime("%Y-%m-%d")
    hit = cached_payload(ctx, "holders", entry["code"], tkey)
    if hit:
        return hit["data"], hit["sources"], hit["degrade"], True
    c6 = entry["code"][:6]
    C = '(SECURITY_CODE="%s")' % c6
    data, sources, degrade = {}, {}, []

    def take(key, label, filt, sort, ps=50, mapper=None, limit=None):
        rows, err = dc_query(ctx, DC_REPORTS[key], filt, page_size=ps, sort=sort)
        if err:
            sources[label] = "FAIL: " + err
            degrade.append("%s: %s" % (label, err))
            data[key] = []
            return []
        if mapper:
            try:
                rows = [mapper(r) for r in rows]
            except Exception as e:
                sources[label] = "FAIL: 解析 %s" % e
                degrade.append("%s 解析失败: %s" % (label, e))
                data[key] = []
                return []
        if limit:
            rows = rows[:limit]
        data[key] = rows
        sources[label] = "OK(%d)" % len(rows)
        time.sleep(0.25)
        return rows

    take("holders", "十大股东", C, ("END_DATE,HOLDER_RANK", "-1,1"), mapper=lambda r: {
        "期末": str(r.get("END_DATE") or "")[:10], "排名": r.get("HOLDER_RANK"),
        "股东名称": r.get("HOLDER_NAME"), "持股数": num(r.get("HOLD_NUM")),
        "持股比例_pct": num(r.get("HOLD_NUM_RATIO")),
        "较上期": r.get("HOLD_NUM_CHANGE"), "变动比例_pct": num(r.get("CHANGE_RATIO"))})
    data["holders"] = _latest_period_rows(data.get("holders") or [], "期末")
    sources["十大股东"] = "OK(%d)" % len(data["holders"])
    take("freeholders", "十大流通股东", C, ("END_DATE,HOLDER_RANK", "-1,1"), mapper=lambda r: {
        "期末": str(r.get("END_DATE") or "")[:10], "排名": r.get("HOLDER_RANK"),
        "股东名称": r.get("HOLDER_NAME"), "持股数": num(r.get("HOLD_NUM")),
        "占流通_pct": num(r.get("FREE_HOLDNUM_RATIO")),
        "较上期": r.get("HOLD_NUM_CHANGE"), "变动比例_pct": num(r.get("CHANGE_RATIO"))})
    data["freeholders"] = _latest_period_rows(data.get("freeholders") or [], "期末")
    sources["十大流通股东"] = "OK(%d)" % len(data["freeholders"])
    take("holdernum", "股东户数", C, ("END_DATE", "-1"), ps=10, limit=8, mapper=lambda r: {
        "期末": str(r.get("END_DATE") or "")[:10], "股东户数": num(r.get("HOLDER_TOTAL_NUM")),
        "较上期_pct": num(r.get("TOTAL_NUM_RATIO")), "户均流通股": num(r.get("AVG_FREE_SHARES")),
        "户均持股金额": num(r.get("AVG_HOLD_AMT")),
        "持股集中度": num(r.get("HOLD_FOCUS")), "集中度合计": num(r.get("HOLD_RATIO_TOTAL"))})
    take("orghold", "机构持仓", C, ("REPORT_DATE", "-1"), ps=200, limit=None,
         mapper=lambda r: {
             "报告期": str(r.get("REPORT_DATE") or "")[:10], "机构名称": r.get("HOLDER_NAME"),
             "机构类型": r.get("ORG_TYPE"), "母公司": r.get("PARENT_ORG_NAME")})
    org_rows = data.get("orghold") or []
    if org_rows:
        latest = max((x.get("报告期") or "") for x in org_rows)
        cur = [x for x in org_rows if x.get("报告期") == latest]
        agg = {}
        for x in cur:
            t = x.get("机构类型") or "其他"
            agg[t] = agg.get(t, 0) + 1
        data["orghold_summary"] = {"报告期": latest, "机构家数": len(cur), "按类型": agg}
    take("north", "北向持股", C, ("TRADE_DATE", "-1"), ps=10, limit=10, mapper=lambda r: {
        "日期": str(r.get("TRADE_DATE") or "")[:10], "持股数": num(r.get("HOLD_SHARES")),
        "持股市值": num(r.get("HOLD_MARKET_CAP")), "占A股_pct": num(r.get("A_SHARES_RATIO")),
        "占流通_pct": num(r.get("HOLD_SHARES_RATIO")), "较上期_pct": num(r.get("CHANGE_RATE"))})
    take("pledge", "股权质押", C, ("TRADE_DATE", "-1"), ps=5, limit=5, mapper=lambda r: {
        "日期": str(r.get("TRADE_DATE") or "")[:10], "质押比例_pct": num(r.get("PLEDGE_RATIO")),
        "质押市值": num(r.get("PLEDGE_MARKET_CAP")), "质押笔数": num(r.get("PLEDGE_DEAL_NUM")),
        "质押股份": num(r.get("REPURCHASE_BALANCE"))})
    take("bonus", "分红送转", C, ("EX_DIVIDEND_DATE", "-1"), ps=10, limit=10, mapper=lambda r: {
        "公告日": str(r.get("PLAN_NOTICE_DATE") or "")[:10],
        "除权除息日": str(r.get("EX_DIVIDEND_DATE") or "")[:10],
        "送转比例": num(r.get("BONUS_IT_RATIO")), "送股": num(r.get("BONUS_RATIO")),
        "转增": num(r.get("IT_RATIO")), "税前派息": num(r.get("PRETAX_BONUS_RMB"))})
    take("predict", "业绩预告", C, ("NOTICE_DATE", "-1"), ps=10, limit=6, mapper=lambda r: {
        "公告日": str(r.get("NOTICE_DATE") or "")[:10],
        "报告期": str(r.get("REPORT_DATE") or "")[:10], "预告类型": r.get("PREDICT_FINANCE"),
        "预告金额下限": num(r.get("PREDICT_AMT_LOWER")), "预告金额上限": num(r.get("PREDICT_AMT_UPPER")),
        "同比下限_pct": num(r.get("ADD_AMP_LOWER")), "同比上限_pct": num(r.get("ADD_AMP_UPPER")),
        "变动原因": (r.get("CHANGE_REASON_EXPLAIN") or "")[:200]})
    take("mainop", "主营构成", C, ("REPORT_DATE", "-1"), ps=80, limit=60, mapper=lambda r: {
        "报告期": str(r.get("REPORT_DATE") or "")[:10], "口径": {"1": "按行业", "2": "按产品",
                                                              "3": "按地区"}.get(str(r.get("MAINOP_TYPE")), str(r.get("MAINOP_TYPE"))),
        "项目": r.get("ITEM_NAME"), "主营收入": num(r.get("MAIN_BUSINESS_INCOME")),
        # 东财 MBI_RATIO / GROSS_RPOFIT_RATIO 返回的是小数（0.949=94.9%），统一 ×100 成百分数
        "收入占比_pct": (None if num(r.get("MBI_RATIO")) is None
                       else round(num(r.get("MBI_RATIO")) * 100, 4)),
        "毛利率_pct": (None if num(r.get("GROSS_RPOFIT_RATIO")) is None
                     else round(num(r.get("GROSS_RPOFIT_RATIO")) * 100, 4)),
        "主营成本": num(r.get("MAIN_BUSINESS_COST")), "报表": r.get("REPORT_NAME")})
    take("lift", "限售解禁", C, ("FREE_DATE", "1"), ps=50, limit=20, mapper=lambda r: {
        "解禁日": str(r.get("FREE_DATE") or "")[:10], "解禁股东数": num(r.get("BATCH_HOLDER_NUM")),
        "解禁股数": num(r.get("CURRENT_FREE_SHARES")), "解禁市值": num(r.get("LIFT_MARKET_CAP")),
        "解禁类型": r.get("FREE_SHARES_TYPE"), "占流通_pct": num(r.get("FREE_RATIO"))})
    take("margin", "两融", '(SCODE="%s")' % c6, ("DATE", "-1"), ps=10, limit=10,
         mapper=lambda r: {
             "日期": str(r.get("DATE") or "")[:10], "融资余额": num(r.get("RZYE")),
             "融券余量": num(r.get("RQYL")), "融资融券余额": num(r.get("RZRQYE")),
             "融券余额": num(r.get("RQYE")), "融资买入额": num(r.get("RZMRE")),
             "融券卖出量": num(r.get("RQMCL"))})
    take("survey", "机构调研", C, ("NOTICE_DATE", "-1"), ps=30, limit=30, mapper=lambda r: {
        "公告日": str(r.get("NOTICE_DATE") or "")[:10],
        "调研起": str(r.get("RECEIVE_START_DATE") or "")[:10],
        "接待方式": r.get("RECEIVE_OBJECT_TYPE") or r.get("RECEIVE_PLACE"),
        "接待对象": (r.get("RECEIVE_OBJECT") or "")[:300],
        "接待对象类型": r.get("SOURCE")})
    save_cache(ctx, "holders", entry["code"], tkey,
               {"data": data, "sources": sources, "degrade": degrade})
    return data, sources, degrade, False


def fetch_orginfo(ctx, entry):
    """公司概况：行业分类（证监会口径 + 东财三级行业）、主营业务、上市日。"""
    rows, err = dc_query(ctx, "RPT_F10_BASIC_ORGINFO",
                         '(SECUCODE="%s")' % entry["code"], page_size=3)
    if err or not rows:
        return {}, err or "返回空"
    r = rows[0]
    return {"名称": r.get("SECURITY_NAME_ABBR"), "证监会行业": r.get("INDUSTRYCSRC1"),
            "东财行业": r.get("EM2016"), "地区": r.get("REGION"),
            "上市日期": str(r.get("LISTING_DATE") or "")[:10],
            "主营业务": (r.get("MAIN_BUSINESS") or "")[:300]}, None

# ===== [SEC-21] 可比公司（同行业市值 TopN） =====


def _industry_catalog(ctx):
    """同花顺行业指数目录（320 个）→ [(thscode, name)]，进程内缓存。"""
    cached = getattr(_industry_catalog, "_cache", None)
    if cached is not None:
        return cached
    r = invoke(ctx, ["index", "catalog"], ["--tag", "industry"])
    out = []
    if r["ok"]:
        for x in pick_rows(r["payload"], "thscode"):
            if x.get("name"):
                out.append((str(x["thscode"]), str(x["name"])))
    _industry_catalog._cache = out
    return out


def match_industry(ctx, orginfo, mainop):
    """个股 → 同花顺行业指数。优先公司概况的行业名末级，其次主营构成口径名。"""
    cata = _industry_catalog(ctx)
    if not cata:
        return None, "行业目录不可用"
    names = []
    for key in ("东财行业", "证监会行业"):
        v = str(orginfo.get(key) or "")
        if v:
            names += [x.strip() for x in re.split(r"[-—/]", v) if x.strip()]
    for row in mainop[:6]:
        v = str(row.get("项目") or "")
        m = re.match(r"^[^（(]*[（(]([^）)]+)[）)]", v)
        if m:
            names.append(m.group(1))
    for nm in names:
        for code, cname in cata:
            if cname == nm:
                return (code, cname), None
    for nm in names:
        for code, cname in cata:
            if cname and (cname in nm or nm in cname):
                return (code, cname), None
    return None, "行业名 %s 未能匹配同花顺行业目录" % "/".join(names[:4])


def tx_market_caps(codes):
    """腾讯批量取总市值/流通市值 → {6位代码: {'总市值':, '流通市值':, 'PE':, 'PB':}}。"""
    if not codes:
        return {}
    out = {}
    for chunk in [codes[i:i + 60] for i in range(0, len(codes), 60)]:
        syms = [_tx_sym(c) for c in chunk]
        syms = [s for s in syms if s]
        if not syms:
            continue
        raw = http_get_bytes(TX_QT_URL + ",".join(syms), headers=TX_REF_HEADERS,
                             timeout=10, tries=2)
        if not raw:
            continue
        try:
            for line in raw.decode("gbk", "replace").strip().split(";"):
                p = line.split("~")
                if len(p) > 46 and p[2]:
                    out[p[2]] = {"总市值": num(p[45]), "流通市值": num(p[44]),
                                 "PE": num(p[39]), "PB": num(p[46])}
        except Exception:
            continue
    return out


def fetch_peers(ctx, entry, orginfo, mainop, topn=PEER_TOPN):
    """同行业可比公司：行业匹配 → 成分股 → 市值排序 TopN → 估值与关键指标 + 行业均值。"""
    tkey = ctx.target.strftime("%Y-%m-%d")
    hit = cached_payload(ctx, "peers", entry["code"], tkey)
    if hit:
        return hit, [], True
    ind, err = match_industry(ctx, orginfo, mainop)
    if not ind:
        return {"applicable": False, "reason": err}, [err], False
    ind_code, ind_name = ind
    r = invoke(ctx, ["index", "constituents"], ["--thscode", ind_code])
    if not r["ok"]:
        return {"applicable": False, "reason": "成分股取数失败: %s" % str(r.get("error"))[:120]}, \
            ["成分股取数失败"], False
    members = [{"thscode": str(x["thscode"]), "name": x.get("name")}
               for x in pick_rows(r["payload"], "thscode")]
    me = entry["code"]
    members = [m for m in members if m["thscode"] != me]
    caps = tx_market_caps([m["thscode"] for m in members])
    for m in members:
        c = caps.get(m["thscode"][:6]) or {}
        m["总市值"] = c.get("总市值")
        m["流通市值"] = c.get("流通市值")
    ranked = [m for m in members if m.get("总市值") is not None]
    ranked.sort(key=lambda x: x["总市值"], reverse=True)
    top = ranked[:topn]
    degrade = []
    if not top:
        degrade.append("成分股市值取数为空，无法排序")
    vals = {}
    if top:
        r2 = invoke(ctx, ["valuation", "snapshot"], ["--thscodes", ",".join(m["thscode"] for m in top)])
        if r2["ok"]:
            for x in pick_rows(r2["payload"], "thscode"):
                vals[str(x.get("thscode"))] = x
        else:
            degrade.append("同行估值: " + str(r2.get("error"))[:100])
    for m in top:
        v = vals.get(m["thscode"]) or {}
        m["PE_TTM"] = num(v.get("pe_ttm"))
        m["PB"] = num(v.get("pb_mrq"))
        m["PS_TTM"] = num(v.get("ps_ttm"))
        m["ROE"] = m["毛利率"] = m["净利率"] = m["营收同比_pct"] = None
        try:
            r3 = invoke(ctx, ["financials", "indicators"],
                        ["--thscode", m["thscode"], "--report", "%d-%d" % report_guess(ctx.target)])
            if r3["ok"]:
                flat = flatten_indicators(r3["payload"])
                m["ROE"] = flat.get("profitability", {}).get("index_weighted_avg_roe")
                m["毛利率"] = flat.get("profitability", {}).get("sale_gross_margin")
                m["净利率"] = flat.get("profitability", {}).get("sale_net_interest_ratio")
                m["营收同比_pct"] = flat.get("growth", {}).get("calculate_operating_income_yoy_growth_ratio")
        except Exception:
            pass
        time.sleep(0.4)

    def avg(key):
        xs = [m.get(key) for m in top if m.get(key) is not None]
        return round(sum(xs) / len(xs), 4) if xs else None

    out = {"applicable": True, "行业指数": ind_code, "行业名称": ind_name,
           "成分股总数": len(members), "取数家数": len(top),
           "选取规则": "同行业按总市值降序取前 %d 家（已剔除本股）" % topn,
           "公司": top,
           "行业均值": {k: avg(k) for k in ("总市值", "PE_TTM", "PB", "PS_TTM", "ROE",
                                           "毛利率", "净利率", "营收同比_pct")},
           "口径说明": "行业均值=上表算术平均（含缺失值剔除）；PE/PB/PS 为东财快照口径"}
    save_cache(ctx, "peers", entry["code"], tkey, out)
    return out, degrade, False

# ===== [SEC-22] 估值历史分位（自建近似） =====


def compute_valuation_history(ctx, entry, kline_rows, wide_q):
    """自建近 3/5 年 PE-TTM 与 PB 分位（近似口径）。
    股本由「归母净利 ÷ 基本EPS」反推；TTM EPS = 最近 4 个单季归母净利 ÷ 股本；
    BPS = 归母权益 ÷ 股本。按报告期对齐到日K收盘价。"""
    if not kline_rows or not wide_q or len(wide_q) < 5:
        return None, "样本不足（需 K线 + ≥5 期财报）"
    rep = []
    for row in wide_q:
        npp = num(row.get("parent_holder_net_profit"))
        eps = num(row.get("basic_eps"))
        eq = num(row.get("holder_equity_total"))
        d = row.get("report_date")
        if not d or not npp or not eps or eps == 0:
            continue
        shares = npp / eps
        if shares <= 0:
            continue
        rep.append({"date": d, "shares": shares, "np": npp, "eq": eq,
                    "single": num(row.get("单季净利"))})
    if len(rep) < 5:
        return None, "财报期数不足（有效 %d 期）" % len(rep)
    rep.sort(key=lambda x: x["date"])
    for i, x in enumerate(rep):
        window = [r["single"] for r in rep[max(0, i - 3):i + 1] if r["single"] is not None]
        x["ttm_eps"] = (sum(window) / x["shares"]) if len(window) == 4 else None
        x["bps"] = (x["eq"] / x["shares"]) if x["eq"] else None
    rep = [x for x in rep if x["ttm_eps"]]
    if len(rep) < 2:
        return None, "TTM EPS 可算期数不足"
    series = []
    for bar in kline_rows:
        d, close = bar.get("date"), num(bar.get("close"))
        if not d or not close:
            continue
        last = None
        for x in rep:
            if x["date"] <= d:
                last = x
            else:
                break
        if not last:
            continue
        pe = close / last["ttm_eps"] if last["ttm_eps"] > 0 else None
        pb = close / last["bps"] if last.get("bps") else None
        series.append({"date": d, "close": close, "pe_ttm": pe, "pb": pb})
    if len(series) < 60:
        return None, "可比序列不足（%d 个交易日）" % len(series)

    def pct_rank(window):
        cur = window[-1]
        vals = [x for x in window if x is not None]
        if not vals or cur is None:
            return None, 0
        return round(100.0 * sum(1 for v in vals if v <= cur) / len(vals), 1), len(vals)

    out = {"口径说明": "近似口径：股本=归母净利/基本EPS；PE-TTM=收盘价/(近4个单季归母净利/股本)；"
                      "PB=收盘价/(归母权益/股本)。未考虑财报披露滞后与股本变动，仅供量级参考。"}
    for label, key in (("PE_TTM", "pe_ttm"), ("PB", "pb")):
        rec = {}
        for win_label, n in (("近3年", 750), ("近5年", 1250)):
            window = [x[key] for x in series[-n:]]
            p, cnt = pct_rank(window)
            rec[win_label + "分位_pct"] = p
            rec[win_label + "样本数"] = cnt
            if len(series) < n:
                rec[win_label + "覆盖不足"] = ("实际仅 %d 个交易日（约 %.1f 年），"
                                            "该分位为全序列分位" % (cnt, cnt / 250.0))
        rec["当前"] = round(series[-1][key], 4) if series[-1][key] is not None else None
        vals = [x[key] for x in series[-1250:] if x[key] is not None]
        rec["区间最高"] = round(max(vals), 4) if vals else None
        rec["区间最低"] = round(min(vals), 4) if vals else None
        out[label] = rec
    out["序列起点"] = series[0]["date"]
    out["序列终点"] = series[-1]["date"]
    out["序列长度"] = len(series)
    out["覆盖年限_约"] = round(len(series) / 250.0, 2)
    return out, None


def _dim_of(cmd):
    if cmd == "pull":
        return ["tech", "fund", "news"]
    return [cmd]


def run(ctx, entries, dims, args):
    """按维度采集，合并进当日单 JSON 交付文件。返回退出码。"""
    skip = set(x.strip() for x in str(getattr(args, "skip", "") or "").split(",") if x.strip())
    kline_max = int(getattr(args, "kline_max", 0) or 0)
    pool, pool_fails, pool_sizes = {}, {}, {}
    if "news" in dims:
        say("[..] news pool: fetching market-wide flash (one pass per source)")
        pool, pool_fails, ok = collect_flash_pool(ctx)
        pool_sizes = {k: len(v) for k, v in pool.items()}
        if ok == 0:
            say("[FAIL] news pool: all flash sources failed; news will keep official endpoints only")
    # 合并基线：部分重跑（只跑某一维）时保住文件里其它标的/其它维度的既有结果
    prev = load_day(ctx)
    prev_syms = {s.get("code"): s for s in (prev.get("symbols") or []) if isinstance(s, dict)}
    prev_order = [c for c in prev_syms]
    out, new_codes = {}, []
    for i, entry in enumerate(entries):
        if i:
            time.sleep(BATCH_SLEEP)
        code = entry["code"]
        old = prev_syms.get(code) or {}
        say("[..] %s %s" % (code, entry["type"]))
        tech = fund = news = None
        if "tech" in dims:
            tech = collect_tech(ctx, entry, want_rt=getattr(args, "rt", False),
                                force_source=getattr(args, "kline_source", None),
                                kline_max=kline_max, skip=skip)
        else:
            tech = old.get("tech")
        if "fund" in dims:
            fund = collect_fund(ctx, entry, skip=skip,
                                kl_rows=ctx.kline_rows.get(code), kline_max=kline_max)
        else:
            fund = old.get("fund")
        if "news" in dims:
            news = collect_news(ctx, entry, pool, pool_fails,
                                top=getattr(args, "top", DEFAULT_TOP), skip=skip)
        else:
            news = old.get("news")
        sym = symbol_payload(ctx, entry, tech, fund, news)
        for dim_name, st in (old.get("status") or {}).items():
            sym["status"].setdefault(dim_name, st)   # 保留该标的其它维度的历史状态
        out[code] = sym
        if code not in prev_syms:
            new_codes.append(code)
        say("[OK] %s dims=%s" % (code, ",".join(k for k in ("tech", "fund", "news")
                                                if sym.get(k))))
    # 顺序稳定：沿用上次文件里的顺序，新标的追加在末尾（避免重跑造成 diff 噪音）
    order = prev_order + new_codes
    for code, sym in prev_syms.items():              # 本次未涉及的标的原样保留
        if code not in out:
            out[code] = sym
    bundle = build_day_bundle(ctx, dims, [out[c] for c in order], prev, pool_sizes)
    write_json(day_path(ctx), bundle)
    say("[OK] bundle: %s (%d symbols)" % (ctx.rel(day_path(ctx)), len(order)))
    return 1 if ctx.failures else 0


def cmd_run(args):
    target = parse_date(args.date) if getattr(args, "date", None) else today_local()
    if getattr(args, "date", None) and target is None:
        say("[FAIL] --date 需为 YYYY-MM-DD")
        return 2
    base = os.path.abspath(args.out or BASE_DEFAULT)
    ctx = Ctx(base, target)
    ctx.debug = bool(getattr(args, "debug", False))
    ctx.no_cache = bool(getattr(args, "no_cache", False))
    # 只打印 ASCII 安全字段；base 含中文路径（投资）会乱码，故仅在 --debug 下落文件/打印相对值
    say("[OK] target=%s cmd=%s" % (target.strftime("%Y-%m-%d"), args.cmd))
    dbg(ctx, "base=%s" % base)
    probe_cli(ctx)
    tokens = list(getattr(args, "tokens", None) or [])
    if getattr(args, "pool", None):
        pt, err = read_pool(args.pool)
        if err:
            say("[FAIL] pool: %s" % err)
        else:
            # 路径文件名多为中文（持仓数据.md），会被 ASCII 过滤吃掉，故不打印路径
            say("[OK] pool: %d symbols (from --pool)" % len(pt))
            tokens += pt
    if not tokens:
        say("[FAIL] no symbols: pass codes/names, or use --pool <file>")
        return 2
    entries = build_entries(ctx, tokens)
    if not entries:
        say("[FAIL] no valid symbols (all inputs failed to resolve; see above)")
        return 2
    say("[OK] entries=%d -> %s" % (len(entries), ",".join(e["code"] for e in entries)))
    code = run(ctx, entries, _dim_of(args.cmd), args)
    if ctx.failures:
        say("[WARN] %d failure/degrade item(s); see 'failures' in the delivered json"
            % len(ctx.failures))
    return code

# ===== [SEC-23] 技术面扩展（多周期/价位/缺口/量价/背离/换手） =====


def _rsi(closes, n):
    """Wilder RSI。样本不足返回 None。"""
    if len(closes) <= n:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        ch = closes[i] - closes[i - 1]
        gains += max(ch, 0)
        losses += max(-ch, 0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(ch, 0)) / n
        al = (al * (n - 1) + max(-ch, 0)) / n
    return round(100.0 if al == 0 else 100 - 100 / (1 + ag / al), 2)


def _ma(seq, n):
    return round(sum(seq[-n:]) / n, 3) if len(seq) >= n else None


def kline_resample(rows, period):
    """日K → 周线('W')/月线('M')聚合（开=首、收=末、高=最高、低=最低、量=和）。"""
    groups, order = {}, []
    for r in rows:
        d, c = r.get("date"), num(r.get("close"))
        if not d or c is None:
            continue
        try:
            dt = datetime.datetime.strptime(d, "%Y-%m-%d").date()
        except ValueError:
            continue
        key = dt.isocalendar()[:2] if period == "W" else (dt.year, dt.month)
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"date": d, "open": num(r.get("open")), "close": c,
                               "high": num(r.get("high")), "low": num(r.get("low")), "vol": 0.0}
            order.append(key)
        else:
            g["date"], g["close"] = d, c
            for src in ("high", "low"):
                a, b = g[src], num(r.get(src))
                if a is None:
                    g[src] = b
                elif b is not None:
                    g[src] = max(a, b) if src == "high" else min(a, b)
        v = num(r.get("vol"))
        if v:
            g["vol"] = (g["vol"] or 0) + v
    return [groups[k] for k in order]


def compute_levels(rows):
    """关键价位：数据起点高低、近 1 年高低、近 60/20 日高低、MA120/250。"""
    if not rows:
        return {}
    closes = [num(r.get("close")) for r in rows]
    highs = [x for x in (num(r.get("high")) for r in rows) if x is not None]
    lows = [x for x in (num(r.get("low")) for r in rows) if x is not None]

    def hi_lo(win):
        h = [x for x in (num(r.get("high")) for r in rows[-win:]) if x is not None]
        l = [x for x in (num(r.get("low")) for r in rows[-win:]) if x is not None]
        return (round(max(h), 3) if h else None, round(min(l), 3) if l else None)

    out = {"数据起点": rows[0].get("date"), "数据终点": rows[-1].get("date"),
           "数据根数": len(rows)}
    out["区间最高"], out["区间最低"] = (round(max(highs), 3) if highs else None,
                                        round(min(lows), 3) if lows else None)
    out["近1年最高"], out["近1年最低"] = hi_lo(250)
    out["近60日平台高"], out["近60日平台低"] = hi_lo(60)
    out["近20日高"], out["近20日低"] = hi_lo(20)
    for label, n in (("MA120", 120), ("MA250", 250)):
        out[label] = _ma(closes, n)
        if out[label] is None:
            out[label + "_缺失原因"] = "样本不足（需 ≥%d 根，实有 %d 根）" % (n, len(rows))
    return out


def find_gaps(rows, lookback=250):
    """缺口识别（类型为规则判定）。仅保留近 lookback 根内、尚未回补的缺口。"""
    if len(rows) < 25:
        return [], "样本不足（需 ≥25 根）"
    seg = rows[-lookback:]
    out = []
    for i in range(1, len(seg)):
        prev, cur = seg[i - 1], seg[i]
        ph, pl = num(prev.get("high")), num(prev.get("low"))
        ch, cl = num(cur.get("high")), num(cur.get("low"))
        if None in (ph, pl, ch, cl):
            continue
        up, down = cl > ph, ch < pl
        if not (up or down):
            continue
        edge = ph if up else pl
        size = (cl - ph) if up else (pl - ch)
        pct = round(size / edge * 100, 2) if edge else None
        base = seg[max(0, i - 20):i]
        bh = [x for x in (num(x.get("high")) for x in base) if x is not None]
        bl = [x for x in (num(x.get("low")) for x in base) if x is not None]
        amp = round((max(bh) - min(bl)) / min(bl) * 100, 2) if bh and bl and min(bl) else None
        first = num(base[0].get("close")) if base else None
        lastc = num(prev.get("close"))
        run = round((lastc - first) / first * 100, 2) if first and lastc else None
        if run is not None and run > 20:
            gtype = "衰竭缺口（规则：缺口前20日涨幅>20%）"
        elif amp is not None and amp < 10:
            gtype = "突破缺口（规则：缺口前20日振幅<10%）"
        else:
            gtype = "普通缺口（规则：其余情形）"
        filled = False
        for x in seg[i + 1:]:
            lx, hx = num(x.get("low")), num(x.get("high"))
            if up and lx is not None and lx <= ph:
                filled = True
                break
            if down and hx is not None and hx >= pl:
                filled = True
                break
        out.append({"日期": cur.get("date"), "方向": "向上" if up else "向下",
                    "缺口下沿": round(edge, 3),
                    "缺口幅度_pct": pct, "类型": gtype, "是否回补": filled})
    return [g for g in out if not g["是否回补"]][-8:], None


def volume_price_stats(rows):
    """近 5/10/20 日量价配合统计与量能均线（事实统计，不做判断）。"""
    out = {}
    vols = [num(r.get("vol")) for r in rows]
    closes = [num(r.get("close")) for r in rows]
    for n in (5, 10, 20):
        if len(rows) < n + 1:
            out["近%d日" % n] = {"样本不足": "需 ≥%d 根" % (n + 1)}
            continue
        up_vol, dn_vol, up_days = [], [], 0
        for i in range(len(closes) - n, len(closes)):
            if closes[i] is None or closes[i - 1] is None:
                continue
            if closes[i] > closes[i - 1]:
                up_days += 1
                if vols[i]:
                    up_vol.append(vols[i])
            elif closes[i] < closes[i - 1] and vols[i]:
                dn_vol.append(vols[i])
        au = sum(up_vol) / len(up_vol) if up_vol else None
        ad = sum(dn_vol) / len(dn_vol) if dn_vol else None
        valid = [v for v in vols[-n - 1:-1] if v]
        out["近%d日" % n] = {
            "上涨天数": up_days, "下跌天数": n - up_days,
            "上涨日均量": round(au, 2) if au else None,
            "下跌日均量": round(ad, 2) if ad else None,
            "涨跌量比": round(au / ad, 3) if (au and ad) else None,
            "当期均量": round(sum(valid) / len(valid), 2) if valid else None}
    for n in (5, 10, 20, 60):
        good = [v for v in vols if v]
        out["量均线MA%d" % n] = _ma(good, n)
    return out


def macd_divergence(rows, an):
    """顶/底背离（规则判定：比较最近两个局部高低点与对应 DIF）。"""
    dif = (an or {}).get("_dif")
    if not dif or len(rows) < 40:
        return {"缺失原因": "样本不足或 MACD 不可用"}
    d, seg = dif[-60:], rows[-60:]

    def pivots(want_high):
        idx = []
        for i in range(3, len(seg) - 3):
            c = num(seg[i].get("close"))
            win = [x for x in (num(seg[j].get("close")) for j in range(i - 3, i + 4))
                   if x is not None]
            if c is None or not win:
                continue
            if (want_high and c == max(win)) or (not want_high and c == min(win)):
                idx.append(i)
        return idx[-2:] if len(idx) >= 2 else []

    out = {}
    for label, want_high in (("顶背离", True), ("底背离", False)):
        idx = pivots(want_high)
        if len(idx) < 2:
            out[label] = "近 60 日无两个可比的局部%s点" % ("高" if want_high else "低")
            continue
        i, j = idx
        pi, pj = num(seg[i].get("close")), num(seg[j].get("close"))
        di = d[i] if i < len(d) else None
        dj = d[j] if j < len(d) else None
        if None in (pi, pj, di, dj):
            out[label] = None
            continue
        hit = (pj > pi and dj < di) if want_high else (pj < pi and dj > di)
        out[label] = "%s（规则判定：价格 %.3f→%.3f，DIF %.4f→%.4f）" % (
            "存在" if hit else "未出现", pi, pj, di, dj)
    return out


def cross_state(rows, an):
    """均线与指标的金叉/死叉状态（近 5 根内）。"""
    out = {}
    closes = [num(r.get("close")) for r in rows]
    if len(closes) >= 25:
        ma5 = [_ma(closes[:i + 1], 5) for i in range(len(closes))]
        ma10 = [_ma(closes[:i + 1], 10) for i in range(len(closes))]
        st = "近5日无交叉"
        for i in range(max(1, len(closes) - 5), len(closes)):
            if None in (ma5[i - 1], ma10[i - 1], ma5[i], ma10[i]):
                continue
            if ma5[i - 1] <= ma10[i - 1] and ma5[i] > ma10[i]:
                st = "近5日 MA5 上穿 MA10（金叉）"
            elif ma5[i - 1] >= ma10[i - 1] and ma5[i] < ma10[i]:
                st = "近5日 MA5 下穿 MA10（死叉）"
        out["MA5/MA10"] = "%s，当前 MA5 %s MA10" % (
            st, "高于" if ma5[-1] > ma10[-1] else "低于")
    else:
        out["MA5/MA10"] = "样本不足"
    k, d = (an or {}).get("kdj_k"), (an or {}).get("kdj_d")
    if k is not None and d is not None:
        out["KDJ"] = "K %s / D %s（%s）" % (fmt_n(k), fmt_n(d), "金叉态" if k > d else "死叉态")
    dif = (an or {}).get("_dif")
    if dif:
        out["MACD零轴"] = "DIF 在零轴%s（%.4f）" % ("上方" if dif[-1] > 0 else "下方", dif[-1])
    return out


def fetch_tx_quote(thscode):
    """腾讯报价行 → 换手率/振幅/量比/涨跌停价/市值（字段索引见 fin.py 注释）。"""
    sym = _tx_sym(thscode)
    if not sym:
        return None, "无腾讯符号"
    raw = http_get_bytes(TX_QT_URL + sym, headers=TX_REF_HEADERS, timeout=10, tries=2)
    if not raw:
        return None, "腾讯报价请求失败"
    try:
        for line in raw.decode("gbk", "replace").strip().split(";"):
            p = line.split("~")
            if len(p) >= 52 and p[2] == thscode[:6]:
                return {"换手率_pct": num(p[38]), "振幅_pct": num(p[43]),
                        "流通市值_亿": num(p[44]), "总市值_亿": num(p[45]),
                        "PE": num(p[39]), "PB": num(p[46]),
                        "涨停价": num(p[47]), "跌停价": num(p[48]),
                        "量比": num(p[49]), "均价": num(p[51])}, None
    except Exception as e:
        return None, "腾讯报价解析失败: %s" % e
    return None, "腾讯报价无该股"


def tech_extended(rows, an, txq):
    """技术面扩展块：价位、缺口、量价、背离、交叉、多周期、换手。"""
    out = {"关键价位": compute_levels(rows)}
    if txq:
        out["关键价位"]["换手率_pct"] = txq.get("换手率_pct")
        out["关键价位"]["振幅_pct"] = txq.get("振幅_pct")
        out["关键价位"]["量比"] = txq.get("量比")
        out["关键价位"]["涨停价"] = txq.get("涨停价")
        out["关键价位"]["跌停价"] = txq.get("跌停价")
    gaps, gerr = find_gaps(rows)
    out["缺口"] = gaps
    if gerr:
        out["缺口_说明"] = gerr
    out["量价"] = volume_price_stats(rows)
    out["背离"] = macd_divergence(rows, an)
    out["交叉状态"] = cross_state(rows, an)
    wi, mo = kline_resample(rows, "W"), kline_resample(rows, "M")
    for label, k in (("周线", wi), ("月线", mo)):
        cl = [num(x.get("close")) for x in k]
        info = {"根数": len(k), "起": k[0]["date"] if k else None,
                "止": k[-1]["date"] if k else None}
        for n in (5, 10, 20, 60):
            info["MA%d%s" % (n, "周" if label == "周线" else "月")] = _ma(cl, n)
        miss = [n for n in ((5, 10, 20, 60) if label == "周线" else (20, 60))
                if _ma(cl, n) is None]
        if miss:
            info["样本不足"] = ("MA%s%s 缺失：需 ≥%d 根，实有 %d 根；日K覆盖自 %s"
                               % ("/".join(str(m) for m in miss), "周" if label == "周线" else "月",
                                  max(miss), len(k), rows[0]["date"] if rows else "-"))
        out[label] = info
    if txq and txq.get("流通市值_亿") and an and an.get("last"):
        shares = txq["流通市值_亿"] * 1e8 / an["last"]
        if shares > 0:
            tr = [round(v / shares * 100, 3) if v else None
                  for v in (num(r.get("vol")) for r in rows)]
            g5 = [x for x in tr[-5:] if x]
            g10 = [x for x in tr[-10:] if x]
            out["换手率"] = {
                "日": tr[-1] if tr else None,
                "5日均": round(sum(g5) / len(g5), 3) if g5 else None,
                "10日均": round(sum(g10) / len(g10), 3) if g10 else None,
                "流通股本_股": round(shares, 0)}
    out["筹码代理"] = {
        "说明": "筹码分布/获利盘/套牢盘/平均成本源不可达（东财 cyq 返回 404）；"
                "此处以股东户数、户均持股、集中度作代理，口径不同，不可直接等同",
        "数据": None}
    return out

# ===== [SEC-24] 集合竞价与涨停盘口 =====


def fetch_auction(ctx, entry):
    """集合竞价（hithink market.auction-snapshot，仅当日有效）。"""
    if entry["type"] != "stock":
        return {"applicable": False, "note": "%s 不适用个股集合竞价口径"
                % KIND_CN.get(entry["type"], entry["type"])}, None
    r = invoke(ctx, ["market", "auction-snapshot"],
               ["--thscodes", entry["code"], "--stage", "final"])
    if not r["ok"]:
        return None, str(r.get("error"))[:160]
    rows = pick_rows(r["payload"], "thscode")
    hit = next((x for x in rows if str(x.get("thscode")) == entry["code"]),
               rows[0] if rows else None)
    if not hit:
        return {"applicable": True, "有数据": False,
                "note": "当日无竞价记录（非交易日或已过窗口）"}, None
    return {"applicable": True, "有数据": True,
            "字段": {k: v for k, v in hit.items() if not isinstance(v, (list, dict))}}, None


def fetch_limit_board(ctx, entry):
    """当日涨停/炸板盘口（封单金额、首次封板时间、连板数）。"""
    if entry["type"] != "stock":
        return {"applicable": False}
    ms = date_to_ms(ctx.target)
    out = {"applicable": True, "涨停": None, "炸板": None}
    for key, ability in (("涨停", "limit-up-pool"), ("炸板", "limit-break-pool")):
        r = invoke(ctx, ["special", ability], ["--date-ms", str(ms), "--size", "200"])
        if not r["ok"]:
            out[key] = {"error": str(r.get("error"))[:110]}
            continue
        rows = pick_rows(r["payload"], "thscode")
        hit = next((x for x in rows if str(x.get("thscode")) == entry["code"]), None)
        out[key] = ({k: v for k, v in hit.items() if not isinstance(v, (list, dict))}
                    if hit else {"在池": False})
    return out

# ===== [SEC-25] 消息面增强（席位明细 / 资金情绪 / 关注度） =====


def fetch_seat_detail(ctx, entry, lhb_items):
    """龙虎榜买卖前五席位明细（东财 datacenter，按上榜日过滤）。"""
    if not lhb_items:
        return [], "近 %d 天无上榜日" % LHB_DAYS
    rows_all = []
    for day in [x.get("日期") for x in lhb_items[:3] if x.get("日期")]:
        rows, err = dc_query(ctx, "RPT_BILLBOARD_DAILYDETAILSBUY",
                             '(SECURITY_CODE="%s")(TRADE_DATE=\'%s\')' % (entry["code"][:6], day),
                             page_size=50)
        if err:
            return rows_all, err
        for r in rows:
            rows_all.append({
                "上榜日": day, "席位": r.get("OPERATEDEPT_NAME"),
                "买入_亿": round(_f(r.get("BUY")) / 1e8, 4),
                "卖出_亿": round(_f(r.get("SELL")) / 1e8, 4),
                "净额_亿": round(_f(r.get("NET")) / 1e8, 4),
                "是否机构专用": "机构专用" in str(r.get("OPERATEDEPT_NAME") or "")})
        time.sleep(0.4)
    return rows_all, None


def fetch_attention_rank(ctx, entry):
    """关注度：成交额 TOP100 与换手率 TOP100 中该股名次（东财 clist，一榜一次请求）。"""
    if entry["type"] != "stock":
        return {"applicable": False}
    code = entry["code"][:6]
    out = {"applicable": True}
    for key, fid in (("成交额排名", "f6"), ("换手率排名", "f8")):
        # push2 常被 IP 级限流，按 fin.py 做法加 push2delay 镜像轮换
        d = _eastmoney_get(["https://push2.eastmoney.com/api/qt/clist/get",
                            "https://push2delay.eastmoney.com/api/qt/clist/get"], {
            "pn": "1", "pz": "100", "po": "1", "np": "1", "fltt": "2", "invt": "2",
            "ut": EM_UT, "fid": fid,
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
            "fields": "f12,f14"}, tries=2, base_sleep=1.0, timeout=15)
        diff = ((d or {}).get("data") or {}).get("diff") or []
        if not diff:
            out[key] = "取数失败（东财 clist 拒连或返回空）"
            continue
        rank = next((i + 1 for i, row in enumerate(diff) if str(row.get("f12")) == code), None)
        out[key] = ("第 %d 名（前100榜内）" % rank) if rank else "未进入前100"
    return out

if __name__ == "__main__":
    sys.exit(main())
