# -*- coding: utf-8 -*-
"""aiplan.py — A股「大盘 + 单股」数据喂大模型，输出走势研判与交易计划。

只做编排，不抓数（数据一律来自同目录的 pan.py 与 stock3d.py）：
  1) 按 phase 依次调 pan.py / stock3d.py 落盘 JSON（--no-fetch 可跳过）；
  2) 把两份 JSON + 持仓文件 + 账户配置渲染成「精炼事实包」，原文附件按输入预算追加；
  3) 调可插拔模型层（OpenAI 兼容 / Anthropic Messages / Gemini generateContent）
     出结构化研判，再由复核档做第二轮质询；
  4) 落 Markdown 报告 + 结构化 JSON + plan_log.jsonl（含上次计划的机械回检）。

铁律与 pan.py / stock3d.py 一致：
  - stdout 只输出 ASCII 状态行（中文一律进文件：PowerShell 管道会按 GBK 解码成乱码）；
  - 失败绝不伪装：缺失一律 null 并显式标注；模型失败时报告仍落盘并标 [FAIL]；
  - pan.py / stock3d.py 零改动，仅被 subprocess 调用；
  - 账户配置只用于仓位与费用测算，不改变数据工具的中性事实口径。

用法：
  python aiplan.py init-account
  python aiplan.py init-models
  python aiplan.py check-model
  python aiplan.py prep --code 002463.SZ
  python aiplan.py live --code 002463.SZ
  python aiplan.py post --code 002463.SZ
  python aiplan.py all  --code 002463.SZ
"""

# ===== [SEC-01] imports =====

import argparse
import copy
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
from datetime import datetime, timedelta


# ===== [SEC-02] 常量 =====

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "data")
PAN_DIR = os.path.join(DATA_DIR, "pan")
AI_DIR = os.path.join(DATA_DIR, "ai")
AI_HISTORY = os.path.join(AI_DIR, "history")
PLAN_LOG = os.path.join(AI_HISTORY, "plan_log.jsonl")

# [SEC-02b] 个人文件位置（2026-09 目录整理）：持仓/自选归 data/user/，配置归 config/。
# 只改了这三行的指向，其余逻辑与取值方式（命令行 --pool/--account/--models 覆盖）不变。
DEFAULT_POOL = os.path.join(DATA_DIR, "user", "持仓数据.md")
DEFAULT_ACCOUNT = os.path.join(SCRIPT_DIR, "config", "账户配置.json")
DEFAULT_MODELS = os.path.join(SCRIPT_DIR, "config", "模型配置.json")
TOKEN_FILE = (os.environ.get("AI_PLAN_TOKEN_FILE")
              or os.path.join(os.path.expanduser("~"), "Desktop", "token.txt"))

SCHEMA_VERSION = "1"
PROMPT_VERSION = "2"      # v2：止损双口径约束、计划自洽硬约束（买入≥止损/仓位一致）、T+1 可卖口径
DEFAULT_MAX_INPUT_CHARS = 160000
DEFAULT_TOP = 20
FETCH_TIMEOUT = 1800                 # 单个抓数脚本的超时（秒）
HTTP_TIMEOUT = 300                   # 单次模型调用超时（秒）
PING_MAX_TOKENS = 512                # check-model 的 ping 上限（推理模型会先花 reasoning token）

PHASES = ("prep", "live", "post", "all")
PHASE_LABEL = {"prep": "盘前", "live": "盘中", "post": "盘后", "all": "全时段"}
ROLE_LABEL = {"研判": "研判", "复核": "复核"}

EXIT_OK = 0
EXIT_BADARGS = 2
EXIT_MODEL = 3
EXIT_DATA = 4


# ===== [SEC-03] 日志（stdout 只出 ASCII） =====

def _ascii(text):
    """stdout 只出 ASCII：中文与路径里的非 ASCII 一律替换。"""
    try:
        return str(text).encode("ascii", "replace").decode("ascii")
    except Exception:
        return "?"


def say(msg):
    sys.stdout.write(_ascii(msg) + "\n")
    sys.stdout.flush()


def stage(msg):
    say("[..] " + msg)


def done(msg):
    say("[OK] " + msg)


def warn(msg):
    say("[WARN] " + msg)


def fail(msg):
    say("[FAIL] " + msg)


# ===== [SEC-04] 通用工具 =====

def now_local():
    return datetime.now()


def iso_now():
    return now_local().isoformat(timespec="seconds")


def read_json(path):
    try:
        with io.open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def read_text(path):
    try:
        with io.open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def write_json(path, payload):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, indent=1))
    os.replace(tmp, path)
    return path


def write_text(path, text):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)
    return path


def append_line(path, text):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with io.open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(text.rstrip("\n") + "\n")
    return path


def sha1_text(text):
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()


def sha1_file(path):
    h = hashlib.sha1()
    try:
        with io.open(path, "rb") as f:
            while True:
                blk = f.read(65536)
                if not blk:
                    break
                h.update(blk)
    except OSError:
        return None
    return h.hexdigest()


def mask_key(key):
    if not key:
        return "(none)"
    if len(key) <= 10:
        return key[:3] + "***"
    return key[:7] + "..." + key[-4:]


def num_or_none(v):
    """缺失 → None（数据口径，绝不置 0）。"""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        m = re.search(r"-?\d+(?:\.\d+)?", v.replace(",", ""))
        return float(m.group(0)) if m else None
    return None


def pct(a, b):
    a, b = num_or_none(a), num_or_none(b)
    if a is None or b in (None, 0):
        return None
    return (a / b - 1.0) * 100.0


def fmt_num(v, nd=4):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v != v:
            return "null"
        if v == int(v) and abs(v) < 1e15:
            return str(int(v))
        return ("%." + str(nd) + "f") % v
    return str(v)


def code6(token):
    m = re.search(r"(?<!\d)(\d{6})(?!\d)", str(token or ""))
    return m.group(1) if m else None


def is_etf_code(c6):
    if not c6:
        return False
    return c6[0] in ("5", "1") and c6[:2] in ("51", "50", "56", "58", "59", "15", "16", "17", "18")


# ===== [SEC-05] 账户配置 =====

ACCOUNT_TEMPLATE = {
    "版本": SCHEMA_VERSION,
    "总资金": 0,
    "单票仓位上限_pct": 30,
    "单笔最大亏损_pct": 2,
    "最低现金比例_pct": 20,
    "最大加仓次数": 3,
    "佣金费率_pct": 0.025,
    "佣金最低_元": 5,
    "ETF_佣金费率_pct": 0.00005,
    "ETF_佣金最低_元": 0.5,
    "印花税率_pct": 0.05,
    "过户费率_pct": 0.001,
    "风险偏好": "均衡",
    "api_key": "",
    "说明": ("本文件仅供仓位与费用测算，不改变数据工具的中性事实口径。"
             "「总资金」必须填真实可用资金（元），否则主流程直接报错退出；"
             "api_key 可留空（优先级低于命令行与环境变量，高于 Desktop\\token.txt）。"
             "印花税仅卖出A股收取（ETF 免）；过户费仅A股收取（ETF 免）；"
             "费率口径为「成交金额 × 费率」，佣金不足最低值按最低值收。"),
}


def init_account(path, force=False):
    if os.path.exists(path) and not force:
        return None, "文件已存在，未覆盖：%s" % path
    write_json(path, ACCOUNT_TEMPLATE)
    return path, None


def load_account(path):
    """返回 (配置, 错误)。总资金缺失或 <=0 视为错误（不静默用占位值算股数）。"""
    if not os.path.exists(path):
        return None, "账户配置不存在：%s（先跑 python aiplan.py init-account）" % path
    cfg = read_json(path)
    if not isinstance(cfg, dict):
        return None, "账户配置无法解析为 JSON：%s" % path
    total = num_or_none(cfg.get("总资金"))
    if total is None or total <= 0:
        return None, "账户配置的「总资金」未填或为 0：%s" % path
    out = copy.deepcopy(ACCOUNT_TEMPLATE)
    out.update(cfg)
    out["总资金"] = total
    for k, dv in (("单票仓位上限_pct", 30), ("单笔最大亏损_pct", 2), ("最低现金比例_pct", 20),
                  ("最大加仓次数", 3), ("佣金费率_pct", 0.025), ("佣金最低_元", 5),
                  ("印花税率_pct", 0.05), ("过户费率_pct", 0.001)):
        v = num_or_none(out.get(k))
        out[k] = dv if v is None else v
    return out, None


# ===== [SEC-06] 模型配置（可插拔 provider + profile） =====

PRESET_PROVIDERS = [
    {
        "名称": "deepseek", "协议": "openai-chat",
        "base_url": "https://api.deepseek.com", "路径": "/chat/completions",
        "key_env": "DEEPSEEK_API_KEY", "api_key": "",
        "超时_秒": 300, "重试": 2, "json_object": True,
        "单价": {"输入": 1.32, "输出": 3.96, "缓存读取": 0.044, "币种": "USD", "单位": "每百万token"},
        "模型单价": {
            "deepseek-v4-flash": {"输入": 0.44, "输出": 1.32, "缓存读取": 0.014},
            "deepseek-v4-pro": {"输入": 1.32, "输出": 3.96, "缓存读取": 0.044},
        },
        "余额端点": "/user/balance",
        "模型可选": ["deepseek-v4-pro", "deepseek-v4-flash"],
        "备注": "OpenAI 兼容协议实测可用；余额端点 /user/balance 实测可用。",
    },
    {
        "名称": "deepseek-anthropic", "协议": "anthropic-messages",
        "base_url": "https://api.deepseek.com/anthropic", "路径": "/v1/messages",
        "key_env": "DEEPSEEK_API_KEY", "api_key": "",
        "超时_秒": 300, "重试": 2, "json_object": False,
        "单价": {"输入": 1.32, "输出": 3.96, "缓存读取": 0.044, "币种": "USD", "单位": "每百万token"},
        "模型单价": {"deepseek-v4-flash": {"输入": 0.44, "输出": 1.32, "缓存读取": 0.014}},
        "余额端点": "",
        "模型可选": ["deepseek-v4-flash", "deepseek-v4-pro"],
        "备注": ("Anthropic Messages 协议实测可用（同一 DeepSeek key，x-api-key 认证）；"
                 "响应含 thinking 块，解析时只取 type=text 的块；无 response_format，靠提示词约束 JSON。"),
    },
    {
        "名称": "zhipu", "协议": "openai-chat",
        "base_url": "https://open.bigmodel.cn/api/paas/v4", "路径": "/chat/completions",
        "key_env": "ZHIPU_API_KEY", "api_key": "",
        "超时_秒": 300, "重试": 2, "json_object": True,
        "单价": {"输入": 0, "输出": 0, "缓存读取": 0, "币种": "CNY", "单位": "每百万token"},
        "余额端点": "", "模型可选": ["glm-4.5-flash", "glm-4-flash", "glm-4-plus"],
        "备注": "免费档（glm-4.5-flash / glm-4-flash）需注册取 key；端点本机可达（401 表示需鉴权）。",
    },
    {
        "名称": "siliconflow", "协议": "openai-chat",
        "base_url": "https://api.siliconflow.cn/v1", "路径": "/chat/completions",
        "key_env": "SILICONFLOW_API_KEY", "api_key": "",
        "超时_秒": 300, "重试": 2, "json_object": True,
        "单价": {"输入": 0, "输出": 0, "缓存读取": 0, "币种": "CNY", "单位": "每百万token"},
        "余额端点": "", "模型可选": ["Qwen/Qwen3-8B", "deepseek-ai/DeepSeek-V3"],
        "备注": "有免费模型档，需注册取 key；端点本机可达。",
    },
    {
        "名称": "modelscope", "协议": "openai-chat",
        "base_url": "https://api-inference.modelscope.cn/v1", "路径": "/chat/completions",
        "key_env": "MODELSCOPE_API_KEY", "api_key": "",
        "超时_秒": 300, "重试": 2, "json_object": False,
        "单价": {"输入": 0, "输出": 0, "缓存读取": 0, "币种": "CNY", "单位": "每百万token"},
        "余额端点": "", "模型可选": ["Qwen/Qwen3-8B"],
        "备注": "魔搭 API-Inference，每日免费额度，需注册 token；端点本机可达。",
    },
    {
        "名称": "volces-ark", "协议": "openai-chat",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3", "路径": "/chat/completions",
        "key_env": "ARK_API_KEY", "api_key": "",
        "超时_秒": 300, "重试": 2, "json_object": True,
        "单价": {"输入": 0, "输出": 0, "缓存读取": 0, "币种": "CNY", "单位": "每百万token"},
        "余额端点": "", "模型可选": ["doubao-seed-1-6", "doubao-1-5-pro-32k"],
        "备注": "火山方舟（豆包），新用户有免费额度，需注册 key；endpoint id 形如 ep-xxxx，按控制台填写。",
    },
    {
        "名称": "dashscope", "协议": "openai-chat",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "路径": "/chat/completions",
        "key_env": "DASHSCOPE_API_KEY", "api_key": "",
        "超时_秒": 300, "重试": 2, "json_object": True,
        "单价": {"输入": 0, "输出": 0, "缓存读取": 0, "币种": "CNY", "单位": "每百万token"},
        "余额端点": "", "模型可选": ["qwen-plus", "qwen-max", "qwen-turbo"],
        "备注": "阿里百炼（千问），新用户有免费额度，需注册 key。",
    },
    {
        "名称": "moonshot", "协议": "openai-chat",
        "base_url": "https://api.moonshot.cn/v1", "路径": "/chat/completions",
        "key_env": "MOONSHOT_API_KEY", "api_key": "",
        "超时_秒": 300, "重试": 2, "json_object": True,
        "单价": {"输入": 0, "输出": 0, "缓存读取": 0, "币种": "CNY", "单位": "每百万token"},
        "余额端点": "", "模型可选": ["kimi-k2-0905-preview", "moonshot-v1-32k"],
        "备注": "Kimi（月之暗面），需注册 key。",
    },
    {
        "名称": "openrouter", "协议": "openai-chat",
        "base_url": "https://openrouter.ai/api/v1", "路径": "/chat/completions",
        "key_env": "OPENROUTER_API_KEY", "api_key": "",
        "超时_秒": 300, "重试": 2, "json_object": True,
        "单价": {"输入": 0, "输出": 0, "缓存读取": 0, "币种": "USD", "单位": "每百万token"},
        "余额端点": "", "模型可选": ["deepseek/deepseek-chat:free", "qwen/qwen3-8b:free"],
        "备注": "有 :free 模型（限速），需注册 key；端点本机可达。",
    },
    {
        "名称": "ollama", "协议": "openai-chat",
        "base_url": "http://127.0.0.1:11434/v1", "路径": "/chat/completions",
        "key_env": "", "api_key": "ollama", "鉴权": "none",
        "超时_秒": 600, "重试": 1, "json_object": True,
        "单价": {"输入": 0, "输出": 0, "缓存读取": 0, "币种": "CNY", "单位": "每百万token"},
        "余额端点": "", "模型可选": ["qwen3:8b", "deepseek-r1:14b"],
        "备注": "本机 Ollama（需自行安装并 pull 模型）；当前机器未检测到 ollama。",
    },
    {
        "名称": "anthropic", "协议": "anthropic-messages",
        "base_url": "https://api.anthropic.com", "路径": "/v1/messages",
        "key_env": "ANTHROPIC_API_KEY", "api_key": "",
        "超时_秒": 300, "重试": 2, "json_object": False,
        "单价": {"输入": 0, "输出": 0, "缓存读取": 0, "币种": "USD", "单位": "每百万token"},
        "余额端点": "", "模型可选": ["claude-sonnet-4-5", "claude-opus-4-1"],
        "备注": "Claude 官方；本机需能直连 api.anthropic.com（未验证）。",
    },
    {
        "名称": "gemini", "协议": "gemini-generate",
        "base_url": "https://generativelanguage.googleapis.com", "路径": "/v1beta/models/{model}:generateContent",
        "key_env": "GEMINI_API_KEY", "api_key": "",
        "超时_秒": 180, "重试": 2, "json_object": True,
        "单价": {"输入": 0, "输出": 0, "缓存读取": 0, "币种": "USD", "单位": "每百万token"},
        "余额端点": "", "模型可选": ["gemini-2.5-pro", "gemini-2.5-flash"],
        "备注": "Gemini 官方；协议已实现但本机实测 15s 超时不可达，需自备网络条件与 key。",
    },
]

MODELS_TEMPLATE = {
    "版本": SCHEMA_VERSION,
    "默认_profile": "deepseek-双档",
    "汇率": {"USD_CNY": 7.1},
    "profiles": {
        "deepseek-双档": {
            "说明": "默认档：pro 主研判 + flash 复核（现有 DeepSeek key 可直接跑）",
            "研判": {"provider": "deepseek", "model": "deepseek-v4-pro",
                     "temperature": 0.3, "max_tokens": 12000, "参数": {}},
            "复核": {"provider": "deepseek", "model": "deepseek-v4-flash",
                     "temperature": 0.2, "max_tokens": 16000, "参数": {}},
        },
        "deepseek-单档": {
            "说明": "省钱档：只用 flash；复核留空即等价于 --no-review",
            "研判": {"provider": "deepseek", "model": "deepseek-v4-flash",
                     "temperature": 0.3, "max_tokens": 16000, "参数": {}},
            "复核": None,
        },
        "anthropic-双档": {
            "说明": "换协议的示例：Anthropic Messages 协议走同一 key",
            "研判": {"provider": "deepseek-anthropic", "model": "deepseek-v4-pro",
                     "temperature": 0.3, "max_tokens": 12000, "参数": {}},
            "复核": {"provider": "deepseek-anthropic", "model": "deepseek-v4-flash",
                     "temperature": 0.2, "max_tokens": 16000, "参数": {}},
        },
        "zhipu-双档": {
            "说明": ("智谱档：glm-5.3 主研判 + glm-5.3-flash 复核。GLM-5.x 系列始终思考、不能关闭，"
                     "必须传 reasoning_effort（low/high/max），不传会烧光 max_tokens 导致没有正文。"),
            "研判": {"provider": "zhipu", "model": "glm-5.3",
                     "temperature": 0.3, "max_tokens": 16000,
                     "参数": {"reasoning_effort": "high"}},
            "复核": {"provider": "zhipu", "model": "glm-5.3-flash",
                     "temperature": 0.2, "max_tokens": 8000,
                     "参数": {"reasoning_effort": "low"}},
        },
        "zhipu-全闪": {
            "说明": "全 flash 档：研判与复核都用 glm-5.3-flash（更便宜，适合试跑）",
            "研判": {"provider": "zhipu", "model": "glm-5.3-flash",
                     "temperature": 0.3, "max_tokens": 10000,
                     "参数": {"reasoning_effort": "low"}},
            "复核": {"provider": "zhipu", "model": "glm-5.3-flash",
                     "temperature": 0.2, "max_tokens": 8000,
                     "参数": {"reasoning_effort": "low"}},
        },
    },
    "profiles_by_phase": {"prep": "", "live": "", "post": "", "all": ""},
    "providers": PRESET_PROVIDERS,
    "说明": ("改模型不改代码：新增任意 OpenAI 兼容端点只需在 providers 里加一段，"
             "再用 profiles 组合出「研判/复核」两档；profiles_by_phase 可按 prep/live/post 分时段指定 profile。"
             "每个角色里的「参数」会原样并入请求体（OpenAI/Anthropic 顶层；Gemini 并入 generationConfig），"
             "可用来传 provider 私有开关，例如推理档位、thinking 开关、top_p 等；留空即不发。"
             "推理型模型若「只有 reasoning 没有正文」，调大该档 max_tokens 即可。"
             "key 优先级：--api-key > 环境变量(key_env / AI_PLAN_API_KEY / DEEPSEEK_API_KEY) "
             "> provider.api_key > Desktop\\token.txt 首行。"),
}


def init_models(path, force=False):
    if os.path.exists(path) and not force:
        return None, "文件已存在，未覆盖：%s" % path
    write_json(path, MODELS_TEMPLATE)
    return path, None


def load_models(path):
    if not os.path.exists(path):
        return None, "模型配置不存在：%s（先跑 python aiplan.py init-models）" % path
    cfg = read_json(path)
    if not isinstance(cfg, dict):
        return None, "模型配置无法解析为 JSON：%s" % path
    if not isinstance(cfg.get("providers"), list) or not cfg["providers"]:
        return None, "模型配置缺少 providers：%s" % path
    if not isinstance(cfg.get("profiles"), dict) or not cfg["profiles"]:
        return None, "模型配置缺少 profiles：%s" % path
    return cfg, None


def find_provider(models_cfg, name):
    for p in models_cfg.get("providers") or []:
        if str(p.get("名称")) == str(name):
            return p
    return None


def resolve_profile(models_cfg, phase, cli_profile=None, cli_phase_profile=None):
    """CLI > profiles_by_phase[phase] > 默认_profile。返回 (profile_name, profile, 来源说明)。"""
    if cli_profile:
        name, src = cli_profile, "命令行 --profile"
    elif cli_phase_profile and str(cli_phase_profile).strip():
        name, src = str(cli_phase_profile).strip(), "命令行 --phase-profile"
    else:
        by_phase = (models_cfg.get("profiles_by_phase") or {}).get(phase)
        if by_phase and str(by_phase).strip():
            name, src = str(by_phase).strip(), "profiles_by_phase.%s" % phase
        else:
            name, src = str(models_cfg.get("默认_profile") or "").strip(), "默认_profile"
    prof = (models_cfg.get("profiles") or {}).get(name)
    if not isinstance(prof, dict):
        return None, None, "profile 不存在：%r（来源：%s）" % (name, src)
    return name, prof, src


def resolve_key(provider, cli_key=None):
    """返回 (key, 来源)。优先级：CLI > 环境变量 > 配置 api_key > Desktop\\token.txt 首行。"""
    if cli_key:
        return cli_key.strip(), "命令行 --api-key"
    for var in (provider.get("key_env"), "AI_PLAN_API_KEY", "DEEPSEEK_API_KEY"):
        if var:
            v = (os.environ.get(var) or "").strip()
            if v:
                return v, "环境变量 %s" % var
    v = (provider.get("api_key") or "").strip()
    if v:
        return v, "模型配置 api_key"
    raw = read_text(TOKEN_FILE)
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        for tok in re.split(r"[\s,;]+", line):
            if tok.startswith("sk-") and len(tok) >= 12:
                return tok, "token 文件首行"
    return None, None


# ===== [SEC-07] 模型适配层（openai-chat / anthropic-messages / gemini-generate） =====

def _auth_headers(provider, key):
    proto = provider.get("协议") or "openai-chat"
    headers = dict(provider.get("headers") or {})
    if provider.get("鉴权") == "none":
        return headers
    if proto == "anthropic-messages":
        headers.setdefault("x-api-key", key)
        headers.setdefault("anthropic-version", provider.get("anthropic_version") or "2023-06-01")
    elif proto == "gemini-generate":
        pass                                     # key 走 query
    else:
        headers.setdefault("Authorization", "Bearer " + key)
    return headers


def _provider_url(provider, model, key):
    base = (provider.get("base_url") or "").rstrip("/")
    proto = provider.get("协议") or "openai-chat"
    if proto == "gemini-generate":
        path = provider.get("路径") or "/v1beta/models/{model}:generateContent"
        url = base + path.replace("{model}", urllib.parse.quote(str(model)))
        return url + ("&" if "?" in url else "?") + "key=" + urllib.parse.quote(key or "")
    path = provider.get("路径") or ("/v1/messages" if proto == "anthropic-messages" else "/chat/completions")
    return base + path


def _build_body(provider, model, system, user, temperature, max_tokens, json_mode, extra=None):
    proto = provider.get("协议") or "openai-chat"
    if proto == "anthropic-messages":
        body = {"model": model, "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": user}]}
        if system:
            body["system"] = system
        if temperature is not None:
            body["temperature"] = temperature
        if extra:
            body.update(extra)
        return body
    if proto == "gemini-generate":
        gen = {}
        if temperature is not None:
            gen["temperature"] = temperature
        if max_tokens:
            gen["maxOutputTokens"] = max_tokens
        if json_mode and provider.get("json_object"):
            gen["responseMimeType"] = "application/json"
        if extra:
            gen.update(extra)
        body = {"contents": [{"role": "user", "parts": [{"text": user}]}]}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if gen:
            body["generationConfig"] = gen
        return body
    body = {"model": model,
            "messages": ([{"role": "system", "content": system}] if system else [])
                        + [{"role": "user", "content": user}]}
    if max_tokens:
        body["max_tokens"] = max_tokens
    if temperature is not None:
        body["temperature"] = temperature
    if json_mode and provider.get("json_object"):
        body["response_format"] = {"type": "json_object"}
    if extra:
        body.update(extra)
    return body


def _normal_usage(input_tokens, output_tokens, cache_read):
    return {"输入": input_tokens, "输出": output_tokens, "缓存读取": cache_read or 0,
            "总": (input_tokens or 0) + (output_tokens or 0)}


def _parse_response(provider, payload):
    """返回 (text, usage, finish, 错误)。"""
    proto = provider.get("协议") or "openai-chat"
    if proto == "anthropic-messages":
        blocks = payload.get("content") or []
        texts = [b.get("text") for b in blocks
                 if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
        usage = payload.get("usage") or {}
        u = _normal_usage(usage.get("input_tokens"), usage.get("output_tokens"),
                          usage.get("cache_read_input_tokens"))
        finish = payload.get("stop_reason")
        text = "\n".join(t for t in texts if t)
        if not text:
            kinds = [b.get("type") for b in blocks if isinstance(b, dict)]
            return "", u, finish, "响应无 text 块（仅 %s），可能被 max_tokens 截断" % (kinds or "空")
        return text, u, finish, None
    if proto == "gemini-generate":
        cands = payload.get("candidates") or []
        if not cands:
            fb = payload.get("promptFeedback") or {}
            return "", _normal_usage(0, 0, 0), None, "无 candidates：%s" % json.dumps(fb, ensure_ascii=False)
        parts = ((cands[0].get("content") or {}).get("parts")) or []
        text = "\n".join(p.get("text") for p in parts if isinstance(p, dict) and p.get("text"))
        um = payload.get("usageMetadata") or {}
        u = _normal_usage(um.get("promptTokenCount"), um.get("candidatesTokenCount"),
                          um.get("cachedContentTokenCount"))
        finish = cands[0].get("finishReason")
        if not text:
            return "", u, finish, "响应无文本（finishReason=%s）" % finish
        return text, u, finish, None
    choices = payload.get("choices") or []
    if not choices:
        return "", _normal_usage(0, 0, 0), None, "响应无 choices"
    ch = choices[0] or {}
    msg = ch.get("message") or {}
    text = msg.get("content") or ""
    usage = payload.get("usage") or {}
    u = _normal_usage(usage.get("prompt_tokens"), usage.get("completion_tokens"),
                      (usage.get("prompt_tokens_details") or {}).get("cached_tokens"))
    finish = ch.get("finish_reason")
    if not text:
        if msg.get("reasoning_content"):
            return "", u, finish, "只有 reasoning_content 没有正文（finish_reason=%s，多为 max_tokens 不足）" % finish
        return "", u, finish, "响应 content 为空（finish_reason=%s）" % finish
    return text, u, finish, None


def price_of(provider, model):
    out = dict(provider.get("单价") or {})
    per_model = (provider.get("模型单价") or {}).get(model)
    if isinstance(per_model, dict):
        out.update(per_model)
    return out


def compute_cost(provider, model, usage, fx):
    """缓存读取按单独单价计，不重复计入输入（billable 输入 = 输入 - 缓存读取）。
    未配置任何非零单价时返回「未配置单价」，只报 token，不假装成本为 0。"""
    p = price_of(provider, model)
    if not any(float(p.get(k) or 0) for k in ("输入", "输出", "缓存读取")):
        return {"金额": None, "币种": str(p.get("币种") or ""), "人民币_估算": None,
                "说明": "该 provider/模型未配置单价，仅记录 token（免费额度或费率未知）"}
    inp = float(usage.get("输入") or 0)
    cache = float(usage.get("缓存读取") or 0)
    out = float(usage.get("输出") or 0)
    billable_in = max(0.0, inp - cache)
    amount = (billable_in / 1e6 * float(p.get("输入") or 0)
              + out / 1e6 * float(p.get("输出") or 0)
              + cache / 1e6 * float(p.get("缓存读取") or 0))
    cur = str(p.get("币种") or "CNY").upper()
    cny = amount * fx if cur == "USD" else amount
    return {"金额": round(amount, 6), "币种": cur, "人民币_估算": round(cny, 4)}


def call_model(provider, model, system, user, temperature=None, max_tokens=None,
               json_mode=True, key=None, timeout=None, retries=None, extra=None):
    """统一调用契约：{ok, text, usage, latency_ms, error, http_status, 请求字符数}。"""
    proto = provider.get("协议") or "openai-chat"
    timeout = int(timeout or provider.get("超时_秒") or HTTP_TIMEOUT)
    tries = int(retries if retries is not None else (provider.get("重试") or 0)) + 1
    url = _provider_url(provider, model, key)
    body = _build_body(provider, model, system, user, temperature, max_tokens, json_mode, extra)
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    headers.update(_auth_headers(provider, key))
    last_err, status = None, None
    started = time.time()
    for attempt in range(tries):
        req = urllib.request.Request(url, data=raw, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
                status = resp.status
            text, usage, finish, err = _parse_response(provider, payload)
            latency = int((time.time() - started) * 1000)
            if err:
                return {"ok": False, "text": text, "usage": usage, "latency_ms": latency,
                        "finish": finish, "error": err, "http_status": status,
                        "请求字符数": len(user) + len(system or ""), "协议": proto,
                        "url": url, "响应原文": json.dumps(payload, ensure_ascii=False)[:1200]}
            return {"ok": True, "text": text, "usage": usage, "latency_ms": latency,
                    "finish": finish, "error": None, "http_status": status,
                    "请求字符数": len(user) + len(system or ""), "协议": proto, "url": url}
        except urllib.error.HTTPError as e:
            status = e.code
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:600]
            except Exception:
                detail = ""
            last_err = "HTTP %s: %s" % (e.code, detail or e.reason)
            if e.code not in (408, 409, 425, 429, 500, 502, 503, 504) or attempt >= tries - 1:
                break
        except Exception as e:
            last_err = "%s: %s" % (type(e).__name__, e)
            if attempt >= tries - 1:
                break
        time.sleep(1.5 * (attempt + 1))
    return {"ok": False, "text": "", "usage": _normal_usage(0, 0, 0),
            "latency_ms": int((time.time() - started) * 1000), "finish": None,
            "error": last_err or "未知错误", "http_status": status,
            "请求字符数": len(user) + len(system or ""), "协议": proto, "url": url}


def probe_balance(provider, key, timeout=20):
    """只在 provider 声明了「余额端点」时探测；失败/不支持一律返回 None。"""
    ep = (provider.get("余额端点") or "").strip()
    if not ep:
        return None
    base = (provider.get("base_url") or "").rstrip("/")
    url = base + (ep if ep.startswith("/") else "/" + ep)
    headers = _auth_headers(provider, key)
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"探测失败": "%s: %s" % (type(e).__name__, e)}
    infos = payload.get("balance_infos")
    if isinstance(infos, list) and infos:
        it = infos[0] or {}
        return {"可用": payload.get("is_available"), "余额": it.get("total_balance"),
                "币种": it.get("currency"), "充值余额": it.get("topped_up_balance"),
                "赠送余额": it.get("granted_balance")}
    return {"原始": payload}


# ===== [SEC-08] 结构化 JSON 提取 =====

def extract_json(text):
    """返回 (对象, 错误)。依次尝试：整体解析 → 代码围栏 → 花括号配对。"""
    if not text or not text.strip():
        return None, "空文本"
    raw = text.strip()
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj, None
    except ValueError:
        pass
    for block in re.findall(r"```(?:json|JSON)?\s*(.+?)```", raw, re.S):
        b = block.strip()
        try:
            obj = json.loads(b)
            if isinstance(obj, dict):
                return obj, None
        except ValueError:
            continue
    start = raw.find("{")
    while start >= 0:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(raw)):
            ch = raw[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    frag = raw[start:i + 1]
                    try:
                        obj = json.loads(frag)
                        if isinstance(obj, dict):
                            return obj, None
                    except ValueError:
                        break
        start = raw.find("{", start + 1)
    return None, "无法从模型输出中提取 JSON 对象"


# ===== [SEC-09] 数据抓取编排（调 pan.py / stock3d.py） =====

def _run_subprocess(argv, cwd, timeout=FETCH_TIMEOUT):
    started = time.time()
    try:
        p = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=timeout)
        out = (p.stdout or b"").decode("utf-8", "replace")
        err = (p.stderr or b"").decode("utf-8", "replace")
        return {"returncode": p.returncode, "stdout": out, "stderr": err,
                "seconds": round(time.time() - started, 1), "argv": argv}
    except subprocess.TimeoutExpired:
        return {"returncode": None, "stdout": "", "stderr": "timeout after %ss" % timeout,
                "seconds": round(time.time() - started, 1), "argv": argv}
    except Exception as e:
        return {"returncode": None, "stdout": "", "stderr": "%s: %s" % (type(e).__name__, e),
                "seconds": round(time.time() - started, 1), "argv": argv}


def _tail_lines(text, limit=6):
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-limit:]


def newest_pan_file(phase, date_str=None):
    """优先取指定交易日目录下的 latest_<phase>.json，否则取最近修改的那个。"""
    if date_str:
        p = os.path.join(PAN_DIR, date_str.replace("-", ""), "latest_%s.json" % phase)
        if os.path.exists(p):
            return p
    best, best_m = None, -1.0
    if os.path.isdir(PAN_DIR):
        for name in os.listdir(PAN_DIR):
            p = os.path.join(PAN_DIR, name, "latest_%s.json" % phase)
            if os.path.exists(p):
                m = os.path.getmtime(p)
                if m > best_m:
                    best, best_m = p, m
    return best


def fetch_data(phase, code, date_str, pool, top, pan_out, s3_out, debug=False):
    """按 phase 调 pan.py 与 stock3d.py。返回结果 dict（含日志与落盘路径）。"""
    log = []
    pan_argv = [sys.executable, os.path.join(SCRIPT_DIR, "pan.py"), phase]
    if date_str:
        pan_argv += ["--date", date_str]
    if top:
        pan_argv += ["--top", str(top)]
    if pan_out:
        pan_argv += ["--out", pan_out]
    if phase in ("post", "all") and pool and os.path.exists(pool):
        pan_argv += ["--pool", pool]
    if debug:
        pan_argv += ["--debug"]
    stage("fetch pan.py %s" % phase)
    r1 = _run_subprocess(pan_argv, SCRIPT_DIR)
    log.append({"步骤": "pan.py %s" % phase, "返回码": r1["returncode"],
                "耗时_秒": r1["seconds"], "命令": " ".join(os.path.basename(a) if i == 1 else a
                                                        for i, a in enumerate(r1["argv"])),
                "尾部输出": _tail_lines(r1["stdout"])})
    for ln in _tail_lines(r1["stdout"], 3):
        say("    pan> " + ln)

    s3_argv = [sys.executable, os.path.join(SCRIPT_DIR, "stock3d.py"), "pull", code]
    if date_str:
        s3_argv += ["--date", date_str]
    if s3_out:
        s3_argv += ["--out", s3_out]
    if phase == "live":
        s3_argv += ["--rt"]
    if debug:
        s3_argv += ["--debug"]
    stage("fetch stock3d.py pull %s" % code)
    r2 = _run_subprocess(s3_argv, SCRIPT_DIR)
    log.append({"步骤": "stock3d.py pull", "返回码": r2["returncode"],
                "耗时_秒": r2["seconds"], "命令": "stock3d.py pull %s" % code,
                "尾部输出": _tail_lines(r2["stdout"])})
    for ln in _tail_lines(r2["stdout"], 3):
        say("    s3 > " + ln)

    pan_path = newest_pan_file(phase, None)
    s3_path = os.path.join(s3_out or DATA_DIR,
                           "stock3d_%s.json" % ((date_str or now_local().strftime("%Y-%m-%d")).replace("-", "")))
    if not os.path.exists(s3_path):
        cand = [os.path.join(s3_out or DATA_DIR, n) for n in os.listdir(s3_out or DATA_DIR)
                if re.match(r"^stock3d_\d{8}\.json$", n)]
        s3_path = max(cand, key=os.path.getmtime) if cand else s3_path
    errors = []
    if not pan_path or not os.path.exists(pan_path):
        errors.append("pan.py 未产出 latest_%s.json" % phase)
    if not os.path.exists(s3_path):
        errors.append("stock3d.py 未产出 stock3d_*.json")
    return {"pan_path": pan_path, "s3_path": s3_path, "日志": log, "错误": errors}


# ===== [SEC-10] 持仓文件解析 =====

POOL_NUM_FIELDS = ("股票余额", "可用余额", "冻结数量", "成本价", "市价", "盈亏",
                   "盈亏比例(%)", "当日盈亏", "市值", "仓位占比(%)", "持股天数",
                   "当日买入", "当日卖出")


def parse_pool_rows(path):
    """解析持仓/自选表格 → (列名列表, 行 list[dict])。兼容 | 或 tab 分隔、多列或纯代码。"""
    text = read_text(path).replace("\ufeff", "")
    if not text.strip():
        return [], []
    header, rows = None, []
    for line in text.splitlines():
        raw = line.rstrip("\r\n")
        if not raw.strip():
            continue
        probe = raw.strip()
        if probe.startswith("|") and set(probe.replace("|", "").strip()) <= set("-: "):
            continue                                  # markdown 分隔行
        # 只剥 markdown 的表格竖线，保留行首 tab：持仓表首列（操作）为空，strip 掉会整体错位
        core = raw
        if core.lstrip().startswith("|"):
            core = core.lstrip()[1:]
        if core.rstrip().endswith("|"):
            core = core.rstrip()[:-1]
        cells = [c.strip() for c in re.split(r"\s*\|\s*|\t", core)]
        while cells and cells[-1] == "":
            cells.pop()
        if not any(cells):
            continue
        if header is None and any("证券代码" in c for c in cells):
            header = cells
            continue
        if header is not None:
            row = {}
            for i, name in enumerate(header):
                row[name] = cells[i] if i < len(cells) else ""
            rows.append(row)
        else:
            m = re.search(r"(?<!\d)(\d{6})(?!\d)", core)
            if m:
                rows.append({"证券代码": m.group(1)})
    return header or [], rows


def find_pool_row(rows, c6):
    for row in rows:
        if code6(row.get("证券代码") or row.get("代码") or "") == c6:
            return row
    for row in rows:
        for v in row.values():
            if code6(v) == c6:
                return row
    return None


def holdings_metrics(row, c6, name, price, account):
    """持仓行 + 现价 → 交易计划需要的仓位指标。缺失一律 null。"""
    if not row:
        return {"是否持仓": False, "说明": "持仓文件中未找到该标的（按空仓/建仓情形研判）"}
    qty = num_or_none(row.get("股票余额"))
    avail = num_or_none(row.get("可用余额"))
    frozen = num_or_none(row.get("冻结数量"))
    cost = num_or_none(row.get("成本价"))
    last = num_or_none(price if price is not None else row.get("市价"))
    mv = num_or_none(row.get("市值"))
    if mv is None and qty is not None and last is not None:
        mv = qty * last
    total = num_or_none(account.get("总资金"))
    out = {
        "是否持仓": bool(qty),
        "证券代码": c6, "证券名称": name,
        "是否ETF": is_etf_code(c6),
        "持有股数": qty, "可用股数_可卖": avail, "冻结股数_当日买入不可卖": frozen,
        "可卖_计划执行日": (None if (avail is None and frozen is None)
                       else (avail or 0.0) + (frozen or 0.0)),
        "计划执行日口径": ("按 T+1 假设：冻结股份（当日买入/卖出未解冻）在下一交易日开盘后即可卖出，"
                     "故「可卖_计划执行日」= 可用股数 + 冻结股数；若冻结含质押/挂单等非 T+1 原因，"
                     "该口径会高估可卖数量，需人工核对券商持仓明细。"),
        "成本价": cost, "现价": last,
        "持仓市值_元": None if mv is None else round(mv, 2),
        "浮动盈亏_元": num_or_none(row.get("盈亏")),
        "盈亏比例_pct": num_or_none(row.get("盈亏比例(%)")),
        "持股天数": num_or_none(row.get("持股天数")),
        "回本需涨_pct": None if (cost is None or not last) else round((cost / last - 1) * 100, 3),
        "占总资金_pct": None if (mv is None or not total) else round(mv / total * 100, 3),
        "原始行": row,
    }
    return out


# ===== [SEC-11] 事实包渲染器（通用递归 + 预算裁剪） =====

DEFAULT_ROW_CAP = 12
DEFAULT_MAX_STR = 220
SKIP_KEYS = {"sina_code", "secid", "url", "id", "ts", "thscode", "filter", "state",
             "原始字段", "原始值_百万元", "name"}

CAPS = {
    "pan.sentiment.涨停池": 12,
    "pan.sentiment.跌停池": 12,
    "pan.sentiment.炸板池": 12,
    "pan.sentiment.连板梯队.连板股名单": 12,
    "pan.sentiment.个股人气榜.成交额TOP": 5,
    "pan.sentiment.个股人气榜.换手率TOP": 5,
    "pan.sentiment.个股人气榜.振幅TOP": 5,
    "pan.sentiment.个股人气榜.主力净流入TOP": 5,
    "pan.sentiment.个股人气榜.主力净流出TOP": 5,
    "pan.dragon_tiger.当日.榜单": 12,
    "pan.dragon_tiger.当日.机构席位": 8,
    "pan.dragon_tiger.当日.游资席位": 8,
    "pan.dragon_tiger.前一日.榜单": 12,
    "pan.dragon_tiger.前一日.机构席位": 8,
    "pan.dragon_tiger.前一日.游资席位": 8,
    "pan.dragon_tiger_em": 12,
    "pan.news_layers.顶层政策": 8,
    "pan.news_layers.行业政策": 8,
    "pan.news_layers.宏观数据": 8,
    "pan.news_layers.个股公告": 8,
    "pan.news_layers.前一日龙虎榜": 10,
    "pan.policy_calendar.顶层政策": 8,
    "pan.policy_calendar.行业政策": 8,
    "pan.policy_calendar.宏观日历_数据发布": 8,
    "pan.sectors.板块内部结构": 10,
    "pan.supply.限售解禁.大额解禁名单": 10,
    "pan.supply.新股.后续待申购": 10,
    "pan.next_day.限售解禁.名单": 8,
    "pan.next_day.次日事件": 8,
    "pan.auction.指数竞价": 8,
    "s3.fund.quarterly": 12,
    "s3.fund.annual": 5,
    "s3.fund.statements_detail.income_quarterly": 5,
    "s3.fund.statements_detail.income_annual": 5,
    "s3.fund.statements_detail.balance_quarterly": 5,
    "s3.fund.statements_detail.balance_annual": 5,
    "s3.fund.statements_detail.cash_quarterly": 5,
    "s3.fund.statements_detail.cash_annual": 5,
    "s3.fund.shareholders.holders": 10,
    "s3.fund.shareholders.freeholders": 10,
    "s3.fund.shareholders.holdernum": 4,
    "s3.fund.shareholders.orghold": 8,
    "s3.fund.shareholders.north": 4,
    "s3.fund.shareholders.pledge": 3,
    "s3.fund.shareholders.bonus": 4,
    "s3.fund.shareholders.predict": 3,
    "s3.fund.shareholders.lift": 6,
    "s3.fund.shareholders.margin": 8,
    "s3.fund.shareholders.survey": 8,
    "s3.fund.peers.公司": 10,
    "s3.news.notices.items": 15,
    "s3.news.hot.trend": 12,
    "s3.news.moneyflow.两融.序列": 10,
    "s3.news.moneyflow.机构调研": 8,
    "s3.news.policy_proxy.列表": 8,
    "s3.news.contracts.列表": 8,
    "s3.news.lhb.items": 8,
}

COLS = {
    "s3.fund.quarterly": ["report_date", "operating_income", "parent_holder_net_profit",
                          "单季营收", "单季净利", "营收同比_pct", "净利同比_pct",
                          "毛利率_pct", "净利率_pct", "ROE_pct_累计", "资产负债率_pct",
                          "经营现金流_净利润"],
    "s3.fund.annual": ["report_date", "operating_income", "parent_holder_net_profit",
                       "basic_eps", "毛利率_pct", "净利率_pct", "ROE_pct_累计",
                       "资产负债率_pct", "经营现金流_净利润"],
    "s3.fund.statements_detail.income_quarterly": ["报告期", "营业总收入", "营业利润",
                                                   "归母净利润", "扣非归母净利润",
                                                   "研发费用", "销售费用", "管理费用", "财务费用"],
    "s3.fund.statements_detail.income_annual": ["报告期", "营业总收入", "营业利润",
                                                "归母净利润", "扣非归母净利润", "研发费用"],
    "s3.fund.statements_detail.balance_quarterly": ["报告期", "货币资金", "应收票据及应收账款",
                                                    "存货", "固定资产", "在建工程", "短期借款",
                                                    "长期借款", "资产总计", "负债合计",
                                                    "归母股东权益合计"],
    "s3.fund.statements_detail.balance_annual": ["报告期", "货币资金", "存货", "固定资产",
                                                 "短期借款", "长期借款", "资产总计", "负债合计",
                                                 "归母股东权益合计"],
    "s3.fund.statements_detail.cash_quarterly": ["报告期", "销售商品提供劳务收到的现金",
                                                 "经营活动现金流净额", "投资活动现金流净额",
                                                 "筹资活动现金流净额"],
    "s3.fund.statements_detail.cash_annual": ["报告期", "经营活动现金流净额",
                                              "投资活动现金流净额", "筹资活动现金流净额"],
    "s3.fund.peers.公司": ["name", "总市值", "PE_TTM", "PB", "PS_TTM", "ROE", "毛利率",
                           "净利率", "营收同比_pct"],
    "s3.fund.shareholders.holders": ["期末", "排名", "股东名称", "持股数", "持股比例_pct", "较上期"],
    "s3.fund.shareholders.freeholders": ["期末", "排名", "股东名称", "持股数", "占流通_pct", "较上期"],
    "s3.fund.shareholders.holdernum": ["期末", "股东户数", "较上期_pct", "户均流通股",
                                       "户均持股金额", "集中度合计"],
    "s3.fund.shareholders.margin": ["日期", "融资余额", "融资买入额", "融券余量", "融券余额"],
    "s3.fund.shareholders.north": ["日期", "持股数", "持股市值", "占A股_pct", "占流通_pct", "较上期_pct"],
    "s3.fund.shareholders.predict": ["公告日", "报告期", "预告类型", "预告金额下限",
                                     "预告金额上限", "同比下限_pct", "同比上限_pct"],
    "s3.fund.shareholders.bonus": ["公告日", "除权除息日", "税前派息", "送股", "转增"],
    "s3.fund.shareholders.lift": ["解禁日", "解禁股东数", "解禁股数", "解禁市值", "解禁类型", "占流通_pct"],
    "s3.fund.shareholders.survey": ["公告日", "调研起", "接待对象", "接待对象类型"],
    "s3.news.notices.items": ["时间", "距今_天", "事件分类", "标题"],
    "s3.news.moneyflow.两融.序列": ["日期", "融资余额", "融资买入额", "融券余额", "融券余量"],
    "s3.news.lhb.items": ["日期", "名称", "涨跌幅_pct", "净买_亿", "买入_亿", "卖出_亿", "上榜原因"],
    "s3.news.policy_proxy.列表": ["时间", "来源", "标题"],
    "s3.news.contracts.列表": ["日期", "标题"],
    "s3.news.hot.trend": ["日期", "排名"],
    "pan.sentiment.涨停池": ["代码", "名称", "最新价", "涨跌幅_pct", "连续板数", "连板文本",
                             "封单额_亿", "首次封板时间", "涨停原因"],
    "pan.sentiment.跌停池": ["代码", "名称", "最新价", "涨跌幅_pct", "首次跌停时间", "最后跌停时间",
                             "换手率_pct"],
    "pan.sentiment.炸板池": ["代码", "名称", "最新价", "涨跌幅_pct", "炸板次数", "换手率_pct", "成交额_亿"],
    "pan.sentiment.连板梯队.连板股名单": ["代码", "名称", "连板数", "梯队"],
    "pan.dragon_tiger.当日.榜单": ["代码", "名称", "涨跌幅_pct", "净买额_亿", "买入额_亿",
                                   "卖出额_亿", "净占比_pct", "热度排名", "游资净额_亿"],
    "pan.dragon_tiger.前一日.榜单": ["代码", "名称", "涨跌幅_pct", "净买额_亿", "买入额_亿",
                                     "卖出额_亿", "净占比_pct", "热度排名"],
    "pan.dragon_tiger_em": ["代码", "名称", "涨跌幅_pct", "净买_亿", "买入_亿", "卖出_亿", "上榜原因"],
    "pan.news_layers.顶层政策": ["时间", "src", "title"],
    "pan.news_layers.行业政策": ["时间", "src", "title"],
    "pan.news_layers.宏观数据": ["时间", "src", "title"],
    "pan.news_layers.个股公告": ["时间", "代码", "名称", "标题"],
    "pan.news_layers.前一日龙虎榜": ["代码", "名称", "涨跌幅_pct", "净买额_亿", "净占比_pct"],
    "pan.sectors.板块内部结构": ["板块名称", "成分数_取前100", "板块内涨停家数",
                                 "板块内涨幅超5%家数", "板块龙头"],
}


def _cap_for(path):
    return CAPS.get(path, DEFAULT_ROW_CAP)


def _scalar_str(v):
    if isinstance(v, str):
        s = v.strip().replace("\r", " ").replace("\n", " ")
        return s if len(s) <= DEFAULT_MAX_STR else s[:DEFAULT_MAX_STR] + "…"
    return fmt_num(v)


def _is_flat_row(d):
    return isinstance(d, dict) and all(not isinstance(v, (dict, list)) for v in d.values())


def _row_keys(rows, path):
    cols = COLS.get(path)
    if cols:
        keys = [c for c in cols if any(c in r for r in rows)]
        if keys:
            return keys
    keys = []
    for r in rows:
        for k in r.keys():
            if k in SKIP_KEYS or k in keys:
                continue
            keys.append(k)
    return keys


def _table(rows, path, cap):
    shown = rows[:cap]
    keys = _row_keys(shown, path)
    if not keys:
        return ["（空表）"]
    out = ["| " + " | ".join(keys) + " |", "|" + "---|" * len(keys)]
    for r in shown:
        out.append("| " + " | ".join(_scalar_str(r.get(k)) for k in keys) + " |")
    if len(rows) > cap:
        out.append("（本块共 %d 行，已按输入预算截断至前 %d 行）" % (len(rows), cap))
    return out


def render(node, path, depth=0):
    """通用事实渲染：dict→项目符号，扁平 dict 列表→Markdown 表格，其余递归。"""
    if path in DROPPED:
        return ["（本块已按输入预算裁剪：%s）" % DROPPED[path]]
    if node is None:
        return ["null"]
    if isinstance(node, dict):
        if not node:
            return ["（空）"]
        out = []
        for k, v in node.items():
            if k in SKIP_KEYS:
                continue
            sub = path + "." + str(k)
            if isinstance(v, (dict, list)):
                if (isinstance(v, list) and not v) or (isinstance(v, dict) and not v):
                    out.append("- %s：（空）" % k)
                    continue
                out.append("- %s：" % k)
                out.extend("  " + ln for ln in render(v, sub, depth + 1))
            else:
                out.append("- %s：%s" % (k, _scalar_str(v)))
        return out
    if isinstance(node, list):
        if not node:
            return ["（空）"]
        if all(not isinstance(x, (dict, list)) for x in node):
            return ["、".join(_scalar_str(x) for x in node)]
        if all(_is_flat_row(x) for x in node):
            return _table(node, path, _cap_for(path))
        cap = _cap_for(path)
        out = []
        for i, x in enumerate(node[:cap]):
            out.append("- [%d]" % (i + 1))
            out.extend("  " + ln for ln in render(x, path, depth + 1))
        if len(node) > cap:
            out.append("（本块共 %d 项，已按输入预算截断至前 %d 项）" % (len(node), cap))
        return out
    return [_scalar_str(node)]


DROPPED = {}


def drop_block(path, reason):
    if path not in DROPPED:
        DROPPED[path] = reason


def set_caps(mapping):
    CAPS.update(mapping)


# ===== [SEC-12] 事实包组装（章节 + 确定性关联） =====

def _sec(lines, title):
    return {"标题": title, "内容": lines}


def _pan(pandoc):
    return (pandoc or {}).get("data") or {}


def _sources_table(pandoc, s3sym):
    rows = []
    for name, info in ((pandoc or {}).get("sources") or {}).items():
        rows.append({"来源": "pan.%s" % name, "状态": (info or {}).get("status"),
                     "说明": (info or {}).get("detail")})
    for dim in ("tech", "fund", "news"):
        st = ((s3sym or {}).get("status") or {}).get(dim) or {}
        if st:
            rows.append({"来源": "stock3d.%s" % dim, "状态": st.get("mark"), "说明": st.get("note")})
    for dim in ("tech", "fund", "news"):
        for name, mark in (((s3sym or {}).get(dim) or {}).get("sources") or {}).items():
            if str(mark).startswith("OK"):
                continue
            rows.append({"来源": "stock3d.%s.%s" % (dim, name), "状态": mark, "说明": None})
    return rows


def _issue_lines(pandoc, s3sym):
    out = []
    for d in (pandoc or {}).get("degrade") or []:
        out.append("DEGRADE(pan)：%s" % d)
    for n in (pandoc or {}).get("notes") or []:
        out.append("口径(pan)：%s" % n)
    for blk, info in ((pandoc or {}).get("sources") or {}).items():
        if (info or {}).get("status") == "FAIL":
            out.append("FAIL(pan.%s)：%s" % (blk, (info or {}).get("detail")))
    for dim in ("tech", "fund", "news"):
        blk = (s3sym or {}).get(dim) or {}
        for d in blk.get("degrade") or []:
            out.append("DEGRADE(stock3d.%s)：%s" % (dim, d))
        for u in blk.get("unavailable") or []:
            if isinstance(u, dict):
                out.append("不可得(stock3d.%s)：%s —— %s" % (dim, u.get("字段"), u.get("原因")))
            else:
                out.append("不可得(stock3d.%s)：%s" % (dim, u))
        for name, mark in (blk.get("sources") or {}).items():
            if str(mark).upper().startswith("FAIL"):
                out.append("FAIL(stock3d.%s.%s)：%s" % (dim, name, mark))
    return out


def join_industry(pan_data, s3fund):
    """确定性关联：该股所属行业在当日板块榜/行业资金榜中的位置。"""
    biz = ((s3fund or {}).get("business") or {}).get("行业") or {}
    names = []
    for key in ("东财行业", "证监会行业"):
        v = biz.get(key)
        if isinstance(v, str):
            for seg in re.split(r"[-—/、>]", v):
                seg = seg.strip()
                if len(seg) >= 2 and seg not in names:
                    names.append(seg)
    def matched(nm):
        for n in names:
            if n == nm:
                return n, "完全一致"
            if n in nm or nm in n:
                return n, "局部匹配"
        return None, None
    hits = []
    sec = (pan_data.get("sectors") or {})
    for grp in ("行业板块", "概念板块"):
        for side in ("领涨", "领跌"):
            for row in ((sec.get(grp) or {}).get(side) or []):
                nm = str(row.get("名称") or "")
                word, how = matched(nm)
                if word:
                    hits.append({"类别": "%s-%s" % (grp, side), "板块": nm,
                                 "匹配词": word, "匹配方式": how,
                                 "涨跌幅_pct": row.get("涨跌幅_pct"),
                                 "主力净流入_亿": row.get("主力净流入_亿"),
                                 "领涨股": row.get("领涨股")})
    for key, row in (sec.get("板块内部结构") or {}).items():
        nm = str((row or {}).get("板块名称") or key)
        word, how = matched(nm)
        if word:
            hits.append({"类别": "板块内部结构", "板块": nm, "匹配词": word, "匹配方式": how,
                         "板块内涨停家数": (row or {}).get("板块内涨停家数"),
                         "板块龙头": (row or {}).get("板块龙头")})
    funds = (pan_data.get("funds") or {}).get("行业主力资金") or {}
    for side in ("净流入TOP10", "净流出TOP10"):
        for row in (funds.get(side) or []):
            nm = str(row.get("名称") or "")
            word, how = matched(nm)
            if word:
                hits.append({"类别": "行业主力资金-%s" % side, "板块": nm,
                             "匹配词": word, "匹配方式": how,
                             "主力净流入_亿": row.get("主力净流入_亿"),
                             "涨跌幅_pct": row.get("涨跌幅_pct")})
    return {"行业关键词": names, "命中": hits,
            "口径": "用 stock3d 的东财/证监会行业名去匹配 pan 的东财行业板块与概念板块，非申万口径；未命中不代表无关。"}


def join_rank(pan_data, c6, name):
    """确定性关联：该股在当日各类榜单/池子里的名次与归属。"""
    out = {"榜单名次": [], "池子归属": []}
    def scan(title, rows):
        for i, row in enumerate(rows or [], 1):
            if code6(row.get("代码") or row.get("输入") or "") == c6 or row.get("名称") == name:
                out["榜单名次"].append({"榜单": title, "名次": i, "榜单长度": len(rows),
                                     "涨跌幅_pct": row.get("涨跌幅_pct"),
                                     "成交额_亿": row.get("成交额_亿")})
                return
    sr = pan_data.get("stock_review") or {}
    scan("盘后个股复盘.成交额TOP", sr.get("成交额TOP"))
    scan("盘后个股复盘.换手率TOP", sr.get("换手率TOP"))
    sen = pan_data.get("sentiment") or {}
    for key, rows in (sen.get("个股人气榜") or {}).items():
        scan("情绪.个股人气榜.%s" % key, rows)
    for pool in ("涨停池", "跌停池", "炸板池"):
        for row in sen.get(pool) or []:
            if code6(row.get("代码")) == c6:
                out["池子归属"].append({"池子": pool, "连板文本": row.get("连板文本"),
                                     "封单额_亿": row.get("封单额_亿"),
                                     "涨停原因": row.get("涨停原因")})
    for row in (sen.get("连板梯队") or {}).get("连板股名单") or []:
        if code6(row.get("代码")) == c6:
            out["池子归属"].append({"池子": "连板股名单", "梯队": row.get("梯队"),
                                 "连板数": row.get("连板数")})
    return out


def _account_lines(account):
    rows = [
        {"项": "总资金_元", "值": account.get("总资金")},
        {"项": "单票仓位上限_pct", "值": account.get("单票仓位上限_pct")},
        {"项": "单笔最大亏损_pct（占总资金）", "值": account.get("单笔最大亏损_pct")},
        {"项": "最低现金比例_pct", "值": account.get("最低现金比例_pct")},
        {"项": "最大加仓次数", "值": account.get("最大加仓次数")},
        {"项": "风险偏好", "值": account.get("风险偏好")},
        {"项": "A股佣金费率_pct（最低元）", "值": "%s / %s" % (account.get("佣金费率_pct"),
                                                          account.get("佣金最低_元"))},
        {"项": "印花税率_pct（仅卖出A股，ETF免）", "值": account.get("印花税率_pct")},
        {"项": "过户费率_pct（仅A股，ETF免）", "值": account.get("过户费率_pct")},
    ]
    etf_rate = num_or_none(account.get("ETF_佣金费率_pct"))
    if etf_rate is None:
        etf_rate = num_or_none(account.get("ETC_佣金费率_pct"))
    etf_floor = num_or_none(account.get("ETF_佣金最低_元"))
    if etf_floor is None:
        etf_floor = num_or_none(account.get("ETC_佣金最低_元"))
    if etf_rate is not None or etf_floor is not None:
        rows.append({"项": "ETF佣金费率_pct（最低元）", "值": "%s / %s" % (etf_rate, etf_floor)})
    return rows


def build_sections(ctx):
    """返回 (sections, meta)。sections 为 [{标题, 内容(list[str])}]。"""
    pandoc = ctx["pan"] or {}
    pan_data = _pan(pandoc)
    s3sym = ctx["s3sym"] or {}
    tech, fund, news = s3sym.get("tech") or {}, s3sym.get("fund") or {}, s3sym.get("news") or {}
    phase = ctx["phase"]
    secs = []

    # ① 元信息
    meta_lines = [
        {"项": "时段", "值": "%s(%s)" % (PHASE_LABEL.get(phase, phase), phase)},
        {"项": "pan 交易日", "值": pandoc.get("trade_date")},
        {"项": "pan session", "值": pandoc.get("session")},
        {"项": "pan 生成时间", "值": pandoc.get("generated_at")},
        {"项": "pan 文件", "值": ctx["pan_path"]},
        {"项": "pan sha1", "值": ctx["pan_sha1"]},
        {"项": "stock3d 目标日", "值": (ctx["s3doc"] or {}).get("target_date")},
        {"项": "stock3d 生成时间", "值": (ctx["s3doc"] or {}).get("generated_at")},
        {"项": "stock3d 文件", "值": ctx["s3_path"]},
        {"项": "stock3d sha1", "值": ctx["s3_sha1"]},
        {"项": "标的", "值": "%s %s" % (ctx["code"], ctx["name"])},
        {"项": "Prompt 版本", "值": PROMPT_VERSION},
    ]
    lines = render({"运行与数据来源": meta_lines}, "meta")
    src = _sources_table(pandoc, s3sym)
    lines.append("- 数据源状态：")
    lines.extend("  " + ln for ln in render(src, "meta.数据源状态"))
    issues = _issue_lines(pandoc, s3sym)
    lines.append("- 降级/失败/不可得清单（%d 条）：" % len(issues))
    if issues:
        lines.extend("  - " + ln for ln in issues)
    else:
        lines.append("  - （无）")
    secs.append(_sec(lines, "① 元信息与数据源状态"))

    # ②③④⑤ 大盘
    for title, path, key in (
        ("② 大盘：指数与量能", "pan.indices_volume", "indices_volume"),
        ("③ 大盘：情绪与广度", "pan.sentiment", "sentiment"),
        ("④ 大盘：资金", "pan.funds", "funds"),
    ):
        blk = pan_data.get(key)
        body = render(blk, path) if blk is not None else [
            "（不适用：%s 时段 pan.py 不产出 data.%s，这不是抓取失败；本时段口径见下文专项节）"
            % (PHASE_LABEL.get(phase, phase), key)]
        secs.append(_sec(body, title))
    if pan_data.get("sectors") is None and pan_data.get("关键时点") is None:
        secs.append(_sec(["（不适用：%s 时段无板块轮动/关键时点数据）" % PHASE_LABEL.get(phase, phase)],
                         "⑤ 大盘：板块轮动与关键时点"))
    else:
        secs.append(_sec(
            render({"板块轮动": pan_data.get("sectors"), "关键时点": pan_data.get("关键时点")},
                   "pan.sectors"),
            "⑤ 大盘：板块轮动与关键时点"))

    # ⑥ 盘前专项
    if phase in ("prep", "all"):
        ov = pan_data.get("overseas") or pan_data.get("overseas_evening")
        secs.append(_sec(
            render({"外围与前夜": ov, "Shibor": pan_data.get("shibor"),
                    "DR007": pan_data.get("dr007"),
                    "政策与宏观日历": pan_data.get("policy_calendar"),
                    "当日供给（新股/解禁）": pan_data.get("supply"),
                    "消息分层": pan_data.get("news_layers"),
                    "集合竞价": pan_data.get("auction")}, "pan.prep"),
            "⑥ 盘前专项：外围/利率/供给/消息/竞价"))

    # ⑦ 盘后专项
    if phase in ("post", "all"):
        secs.append(_sec(
            render({"龙虎榜_同花顺": pan_data.get("dragon_tiger"),
                    "龙虎榜_东财(含上榜原因)": pan_data.get("dragon_tiger_em"),
                    "盘后个股复盘": pan_data.get("stock_review"),
                    "次日前瞻": pan_data.get("next_day"),
                    "外围晚间": pan_data.get("overseas_evening")}, "pan.post"),
            "⑦ 盘后专项：龙虎榜/个股复盘/次日前瞻"))

    # ⑧ 个股技术面
    secs.append(_sec(render(tech, "s3.tech") if tech else ["null（技术面缺失）"],
                     "⑧ 个股技术面"))

    # ⑨ 个股基本面
    if fund.get("applicable") is False:
        secs.append(_sec(["不适用：%s" % (fund.get("reason") or "非个股（ETF/指数）"),
                          "口径：ETF/指数无财务报表口径数据，技术面与资金面为准。"],
                         "⑨ 个股基本面"))
    else:
        secs.append(_sec(render(fund, "s3.fund") if fund else ["null（基本面缺失）"],
                         "⑨ 个股基本面"))

    # ⑩ 个股消息面
    secs.append(_sec(render(news, "s3.news") if news else ["null（消息面缺失）"],
                     "⑩ 个股消息面"))

    # ⑪ 持仓与账户 + 确定性关联
    lines = render({"账户约束": _account_lines(ctx["account"]),
                    "本标的持仓": ctx["holding"]}, "holding")
    lines.append("- 确定性关联（由脚本计算，非模型推断）：")
    lines.extend("  " + ln for ln in render(
        {"所属行业在板块榜中的位置": ctx["industry"],
         "在当日榜单/池子中的归属": ctx["rank"]}, "holding.关联"))
    secs.append(_sec(lines, "⑪ 持仓与账户（含确定性关联）"))
    return secs


PRUNE_STEPS = [
    ("股东明细截断至5/3行", lambda: set_caps({
        "s3.fund.shareholders.holders": 5, "s3.fund.shareholders.freeholders": 5,
        "s3.fund.shareholders.orghold": 4, "s3.fund.shareholders.lift": 3,
        "s3.fund.shareholders.margin": 5, "s3.fund.shareholders.survey": 4})),
    ("财报明细截断至3期/去资产负债表明细", lambda: (set_caps({
        "s3.fund.quarterly": 6, "s3.fund.annual": 3,
        "s3.fund.statements_detail.income_quarterly": 3,
        "s3.fund.statements_detail.income_annual": 3}),
        drop_block("s3.fund.statements_detail.balance_quarterly", "输入预算不足"),
        drop_block("s3.fund.statements_detail.balance_annual", "输入预算不足"),
        drop_block("s3.fund.statements_detail.cash_quarterly", "输入预算不足"),
        drop_block("s3.fund.statements_detail.cash_annual", "输入预算不足"))[-1]),
    ("盘后龙虎榜长名单截断至8行", lambda: set_caps({
        "pan.dragon_tiger.当日.榜单": 8, "pan.dragon_tiger.前一日.榜单": 8,
        "pan.dragon_tiger_em": 8, "pan.news_layers.前一日龙虎榜": 6})),
    ("板块内部结构与池子截断", lambda: set_caps({
        "pan.sectors.板块内部结构": 5, "pan.sentiment.涨停池": 6, "pan.sentiment.跌停池": 6,
        "pan.sentiment.炸板池": 6, "pan.sentiment.连板梯队.连板股名单": 6})),
    ("公告与消息逐条截断", lambda: set_caps({
        "s3.news.notices.items": 8, "s3.news.hot.trend": 6,
        "pan.news_layers.顶层政策": 5, "pan.news_layers.行业政策": 5,
        "pan.news_layers.宏观数据": 5, "pan.news_layers.个股公告": 5,
        "s3.news.policy_proxy.列表": 4, "s3.news.contracts.列表": 4})),
    ("可比公司与预告明细截断", lambda: set_caps({
        "s3.fund.peers.公司": 6, "s3.fund.shareholders.predict": 2,
        "pan.next_day.次日事件": 4, "pan.next_day.限售解禁.名单": 4})),
    ("基本面明细整块裁剪", lambda: (drop_block("s3.fund.statements_detail", "输入预算不足"),
                                drop_block("s3.fund.shareholders", "输入预算不足"),
                                set_caps({"s3.fund.quarterly": 4, "s3.fund.annual": 2,
                                          "s3.fund.business.主营构成": 4,
                                          "s3.fund.peers.公司": 4,
                                          "s3.fund.valuation_history": 4}))[-1]),
    ("消息面与盘后长名单整块裁剪", lambda: (drop_block("s3.news.hot", "输入预算不足"),
                                     drop_block("s3.news.moneyflow", "输入预算不足"),
                                     drop_block("s3.news.policy_proxy", "输入预算不足"),
                                     drop_block("s3.news.contracts", "输入预算不足"),
                                     drop_block("pan.dragon_tiger_em", "输入预算不足"),
                                     drop_block("pan.overseas_evening", "输入预算不足"),
                                     set_caps({"s3.news.notices.items": 5,
                                               "s3.news.lhb.items": 4,
                                               "pan.sentiment.涨停池": 4,
                                               "pan.sentiment.跌停池": 4,
                                               "pan.sentiment.炸板池": 4,
                                               "pan.news_layers.顶层政策": 3,
                                               "pan.news_layers.行业政策": 3,
                                               "pan.news_layers.宏观数据": 3,
                                               "pan.news_layers.个股公告": 3}))[-1]),
    ("关闭原始附件内联", lambda: set_caps({})),
]

ATTACH_PATHS = [
    ("pan.data.sentiment", lambda d: _pan(d).get("sentiment")),
    ("pan.data.funds", lambda d: _pan(d).get("funds")),
    ("pan.data.sectors", lambda d: _pan(d).get("sectors")),
    ("pan.data.indices_volume", lambda d: _pan(d).get("indices_volume")),
    ("stock3d.tech", lambda d: (d.get("s3sym") or {}).get("tech")),
    ("stock3d.news", lambda d: (d.get("s3sym") or {}).get("news")),
    ("stock3d.fund", lambda d: (d.get("s3sym") or {}).get("fund")),
]


def assemble_factpack(ctx):
    secs = build_sections(ctx)
    parts = ["# A股事实包（中性事实与数值，脚本渲染，非模型生成）",
             "> 生成时间：%s ｜ 时段：%s ｜ 标的：%s %s ｜ Prompt 版本：%s"
             % (iso_now(), PHASE_LABEL.get(ctx["phase"], ctx["phase"]), ctx["code"], ctx["name"],
                PROMPT_VERSION),
             ""]
    if ctx.get("budget_note"):
        parts.append("> " + ctx["budget_note"])
        parts.append("")
    for s in secs:
        parts.append("## " + s["标题"])
        parts.extend(s["内容"])
        parts.append("")
    if ctx.get("attachment_text"):
        parts.append("## ⑫ 原始附件（按输入预算内联的核心块）")
        parts.append(ctx["attachment_text"])
        parts.append("")
    return "\n".join(parts), secs


def build_factpack(ctx, budget):
    """按预算裁剪事实包；返回 (markdown, sections, 裁剪记录, 附件记录)。"""
    applied, attach_note = [], []
    ctx["attachment_text"] = ""
    text, secs = assemble_factpack(ctx)
    for label, fn in PRUNE_STEPS:
        if len(text) <= budget:
            break
        before = len(text)
        fn()
        text, secs = assemble_factpack(ctx)
        applied.append({"裁剪": label, "裁剪前字符": before, "裁剪后字符": len(text)})
    overhead = len(text)
    if overhead > budget:
        ctx["budget_note"] = ("注意：事实包 %d 字符，仍超过预算 %d（全部裁剪步骤已执行），"
                              "内容因此更贵但不失真；可调大 --max-input-chars 或精简数据源。"
                              % (overhead, budget))
        text, secs = assemble_factpack(ctx)
    if overhead < budget * 0.75:
        chunks = []
        used = overhead
        for label, getter in ATTACH_PATHS:
            node = getter({"pan": ctx["pan"], "s3sym": ctx["s3sym"]})
            if node is None:
                continue
            blob = json.dumps(node, ensure_ascii=False, separators=(",", ":"))
            if used + len(blob) + 40 > budget * 0.95:
                attach_note.append({"附件": label, "状态": "超出预算，改为只列路径"})
                continue
            chunks.append("### %s\n```json\n%s\n```" % (label, blob))
            used += len(blob) + 40
            attach_note.append({"附件": label, "状态": "已内联", "字符": len(blob)})
        if chunks:
            ctx["attachment_text"] = "\n\n".join(chunks)
            text, secs = assemble_factpack(ctx)
    return text, secs, applied, attach_note


# ===== [SEC-13] 提示词 =====

SYSTEM_PROMPT = """你是A股中性数据研判助手，为个人投资者产出当日/次日的条件化操作预案。铁律：
1. 只能使用【事实包】中出现的数字与事实。禁止引入任何外部行情、传闻、目标价、个股记忆或联网知识。
2. 事实包中为 null、缺失、DEGRADE、FAIL、"不适用"的项，必须在结论里显式承认不确定，不得脑补补齐，更不得用 0 或估计值顶替。
3. 不得承诺涨跌或收益，不写"必涨/必跌/稳赚"。给出的是"若出现 X 则做 Y"的条件预案。
4. 交易计划必须可直接执行：A股 T+1（当日买入不可卖）、100股整数倍、遵守事实包给出的涨停价/跌停价；ETF 免印花税。
5. 价位必须有依据（均线/平台高低点/缺口/前高前低/分位），依据只能来自事实包。
6. 计划必须自洽（脚本会机械校验并作废违规条目）：
   - 买入/加仓的「价格区间」下沿必须 ≥ 止损价，跌破止损即离场，不得在止损下方接货；
   - 「仓位.建议_pct」必须与计划里的目标股数一致（目标股数 × 现价 ÷ 总资金 ≈ 建议_pct，误差 ≤ 5 个百分点）；
   - 同一价位段不得同时出现买入类与卖出类动作；
   - 卖出股数不得超过「计划执行日可卖」股数（事实包⑪已给出当前可用与 T+1 解冻口径）。
   - 止损敞口按「现价 − 止损价」×（持仓股数 + 计划买入股数）计算，必须 ≤ 总资金 × 单笔最大亏损_pct；
     若超限，请抬高止损价或减少买入股数，并在「数据依赖」里说明。
7. 只输出一个合法 JSON 对象：不要解释文字、不要 markdown 代码围栏、不要注释、不要尾随逗号。"""

SCHEMA_PROMPT = """输出 JSON 的字段与类型（全部必填，未知的用 null，不要省略键）：
{
  "方向": "偏多|中性|偏空",
  "置信度": 0-100 的整数,
  "时间窗": "如 3-10 个交易日",
  "一句话结论": "不超过 80 字",
  "情景树": [
    {"情形": "乐观|中性|悲观", "概率_pct": 0-100, "路径": "触发什么→怎么走",
     "验证信号": ["可在盘面观察到的信号"], "失效条件": "什么情况下该情景作废"}
  ],
  "关键价位": {
    "支撑": [{"价位": 数字, "依据": "如 MA20=xx / 近20日低点 / 向上缺口下沿"}],
    "压力": [{"价位": 数字, "依据": "..."}],
    "止损价": 数字,
    "目标位": [{"价位": 数字, "依据": "..."}]
  },
  "计划": [
    {"动作": "持有|加仓|减仓|清仓|建仓|观望",
     "触发条件": "价格或盘面条件（不要写时间点）",
     "价格区间": [下限, 上限],
     "股数": 100的整数倍整数（无法确定则 null）,
     "金额_元": 数字或 null,
     "失效条件": "...",
     "优先级": 1-5 的整数}
  ],
  "仓位": {"当前_pct": 数字, "建议_pct": 数字, "上限_pct": 数字, "现金保留_pct": 数字},
  "风险": [{"风险": "...", "监控指标": "事实包里的哪个数据能提前预警", "应对": "..."}],
  "数据依赖": {"降级项": ["事实包里 DEGRADE/FAIL/不可得 的项"], "缺失导致的不确定性": "..."}
}"""

TASK_PROMPT = """【任务】
基于上面的【事实包】，对 {code} {name} 做下一步走势研判，并给出可执行交易计划。
必须覆盖：
1) 大盘环境对该股的影响（指数/量能/情绪广度/资金/板块轮动/关键时点）；
2) 技术面（趋势与结构、关键价位、缺口、量价、指标状态、多周期是否矛盾）；
3) 基本面（成长与盈利质量、负债与现金流、估值分位、可比公司）；
4) 消息面（公告与事件类型、风险公告、热度与龙虎榜/两融/机构调研）；
5) 持仓与账户约束下的仓位建议（该股现有持仓、成本、可卖数量、单票上限、单笔最大亏损、最低现金比例）；
6) 机械化的触发条件与失效条件；止损价必须与"止损距现价的距离"和"单笔最大亏损"约束相容。
7) 止损价必须同时说明两个口径的亏损额：按持仓成本计算、按现价计算，并各自与「单笔最大亏损上限」比较。
注意：{code} 若为 ETF，基本面不适用，用技术面+资金面+板块代替。

{schema}"""

REVIEW_SYSTEM_PROMPT = """你是A股研判的复核（红队）档。你的职责是挑错，不是复述：
1. 只依据【事实包】核对研判档的每一条结论与数字是否被数据支持；指出无依据的推断、与数据矛盾的价位、漏用的关键风险。
2. 事实包里标注为缺失/DEGRADE 的项被当作确定事实使用，属严重问题。
3. 检查计划可执行性：T+1、100股整数倍、涨跌停、单票仓位上限、单笔最大亏损、最低现金比例；ETF 免印花税。
4. 不得引入事实包之外的任何数字或传闻。
5. 只输出一个合法 JSON 对象，不要解释文字、不要代码围栏。"""

REVIEW_TASK = """【复核任务】
下面是研判档基于同一份事实包给出的结论 JSON。请逐条挑错并给出修正建议。
输出要短：质询最多 6 条、每条理由不超过 120 字；不要复述事实包内容，不要重写研判结论。

输出 JSON（全部必填）：
{
  "质询": [
    {"点": "被质询的结论或数字", "严重度": "高|中|低",
     "理由": "事实包里的哪一项支持或反驳它", "建议": "怎么改"}
  ],
  "是否推翻结论": true 或 false,
  "修正要点": ["若采纳质询，计划应如何调整（含价位/仓位/止损）"],
  "遗漏的关键数据": ["事实包里有但研判档没用的关键项"]
}"""


def build_research_messages(factpack, code, name):
    user = ("【事实包】\n" + factpack + "\n\n"
            + TASK_PROMPT.format(code=code, name=name, schema=SCHEMA_PROMPT))
    return SYSTEM_PROMPT, user


def build_review_messages(factpack, research_obj, research_text):
    body = json.dumps(research_obj, ensure_ascii=False, indent=1) if research_obj else research_text
    user = ("【事实包】\n" + factpack + "\n\n【研判档输出】\n" + body + "\n\n" + REVIEW_TASK)
    return REVIEW_SYSTEM_PROMPT, user


def normalize_research(obj):
    """把模型可能写歪的结构拉回契约形状（缺键补 null，字典当列表则包一层）。"""
    if not isinstance(obj, dict):
        return obj
    out = dict(obj)
    for key in ("方向", "置信度", "时间窗", "一句话结论", "情景树", "关键价位", "计划",
                "仓位", "风险", "数据依赖"):
        out.setdefault(key, None)
    for key in ("情景树", "计划", "风险"):
        if isinstance(out.get(key), dict):
            out[key] = [out[key]]
        elif not isinstance(out.get(key), list):
            out[key] = [] if out.get(key) is None else [out[key]]
    if not isinstance(out.get("关键价位"), dict):
        out["关键价位"] = {}
    if not isinstance(out.get("仓位"), dict):
        out["仓位"] = {}
    if not isinstance(out.get("数据依赖"), dict):
        out["数据依赖"] = {}
    return out


def normalize_review(obj):
    """复核档偶尔会把单条质询直接当顶层对象返回，这里统一成 {质询:[...]}。"""
    if not isinstance(obj, dict):
        return obj
    if "质询" not in obj and any(k in obj for k in ("点", "严重度", "理由", "建议")):
        obj = {"质询": [obj]}
    out = dict(obj)
    out.setdefault("质询", [])
    out.setdefault("是否推翻结论", None)
    out.setdefault("修正要点", [])
    out.setdefault("遗漏的关键数据", [])
    if isinstance(out.get("质询"), dict):
        out["质询"] = [out["质询"]]
    elif not isinstance(out.get("质询"), list):
        out["质询"] = []
    for key in ("修正要点", "遗漏的关键数据"):
        if isinstance(out.get(key), str):
            out[key] = [out[key]]
        elif not isinstance(out.get(key), list):
            out[key] = []
    return out


# ===== [SEC-14] 机械风控校验（确定性规则，不由模型负责） =====

BUY_WORDS = ("买入", "加仓", "建仓", "补仓", "定投")
SELL_WORDS = ("减仓", "清仓", "卖出", "止盈", "止损")


def _price_range(entry):
    rng = entry.get("价格区间")
    if isinstance(rng, (list, tuple)) and rng:
        vals = [num_or_none(x) for x in rng]
        vals = [v for v in vals if v is not None]
        if vals:
            return min(vals), max(vals)
    if isinstance(rng, str):
        vals = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", rng)]
        if vals:
            return min(vals), max(vals)
    return None, None


def _action_kind(action):
    a = str(action or "")
    if any(w in a for w in SELL_WORDS):
        return "卖出"
    if any(w in a for w in BUY_WORDS):
        return "买入"
    return "观察"


def trade_cost(is_etf, side, amount, account):
    """成交金额 → 费用明细（佣金双边 + 印花税仅卖出A股 + 过户费仅A股）。
    ETF 可另配 ETF_佣金费率_pct / ETF_佣金最低_元（兼容写成 ETC_ 前缀），缺省回落个股费率。"""
    amount = float(amount or 0)
    rate = num_or_none(account.get("佣金费率_pct")) or 0.0
    floor = num_or_none(account.get("佣金最低_元")) or 0.0
    if is_etf:
        for key in ("ETF_佣金费率_pct", "ETC_佣金费率_pct"):
            v = num_or_none(account.get(key))
            if v is not None:
                rate = v
                break
        for key in ("ETF_佣金最低_元", "ETC_佣金最低_元"):
            v = num_or_none(account.get(key))
            if v is not None:
                floor = v
                break
    comm = max(amount * rate / 100.0, floor) if amount else 0.0
    stamp = amount * float(account.get("印花税率_pct") or 0) / 100.0 if (side == "卖出" and not is_etf) else 0.0
    transfer = amount * float(account.get("过户费率_pct") or 0) / 100.0 if not is_etf else 0.0
    return {"佣金": round(comm, 2), "印花税": round(stamp, 2), "过户费": round(transfer, 2),
            "合计": round(comm + stamp + transfer, 2)}


def normalize_plan(plan, account, holding, price):
    """确定性计划校正（不改写模型数字，只作废自相矛盾的条目并给一致性提示）。

    规则 1：买入/加仓的价格区间下沿 < 止损价 → 该条与「跌破止损即离场」互斥，标作废。
    规则 2：「持有 N 股」的市值占比与「建议_pct」相差 > 5 个百分点 → 提示以股数为准重算仓位。
    规则 3：同一价位段同时出现买入类与卖出类动作 → 提示执行歧义。
    """
    result = {"作废条目": [], "提示": []}
    if not isinstance(plan, dict):
        return result
    entries = plan.get("计划") if isinstance(plan.get("计划"), list) else []
    prices = plan.get("关键价位") or {}
    stop = num_or_none(prices.get("止损价"))
    total = float(account.get("总资金") or 0)
    suggest_pct = num_or_none((plan.get("仓位") or {}).get("建议_pct"))

    for e in entries:
        if not isinstance(e, dict):
            continue
        if _action_kind(e.get("动作")) == "买入":
            lo, hi = _price_range(e)
            if stop is not None and lo is not None and lo < stop - 1e-9:
                reason = ("买入/加仓区间下沿 %s 低于止损价 %s：跌破止损即离场，却在其下方加仓，两条件互斥。"
                          "本脚本按确定性规则作废该条，不替换为任何猜测价位。"
                          % (fmt_num(lo), fmt_num(stop)))
                e["作废"] = True
                e["作废原因"] = reason
                result["作废条目"].append({"动作": e.get("动作"), "价格区间": [lo, hi], "原因": reason})

    if total and suggest_pct is not None and price:
        for e in entries:
            act = str(e.get("动作") or "")
            if act in ("持有", "持仓", "继续持有"):
                q = num_or_none(e.get("股数"))
                if q:
                    pct = q * price / total * 100.0
                    if abs(pct - suggest_pct) > 5:
                        result["提示"].append(
                            "「%s %s 股」对应市值占比 %.2f%%，与「建议_pct %s%%」相差 %.1f 个百分点；"
                            "执行时以股数换算为准，建议让模型把两者对齐。"
                            % (act, fmt_num(q), pct, fmt_num(suggest_pct),
                               abs(pct - suggest_pct)))

    bands = []
    for e in entries:
        if e.get("作废"):
            continue
        lo, hi = _price_range(e)
        if lo is None:
            continue
        bands.append((_action_kind(e.get("动作")), lo, hi or lo))
    for i in range(len(bands)):
        for j in range(i + 1, len(bands)):
            k1, l1, h1 = bands[i]
            k2, l2, h2 = bands[j]
            if k1 == k2 or {k1, k2} != {"买入", "卖出"}:
                continue
            if min(h1, h2) >= max(l1, l2):
                result["提示"].append(
                    "买入类与卖出类的价格区间重叠（[%s, %s] 与 [%s, %s]）：同一价位段动作歧义，"
                    "执行前需明确先后顺序。" % (fmt_num(l1), fmt_num(h1), fmt_num(l2), fmt_num(h2)))
    seen, uniq = set(), []
    for t in result["提示"]:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    result["提示"] = uniq
    return result


def mechanical_checks(account, holding, price, plan, is_etf, other_mv):
    """返回校验明细列表。结果取值：通过 / 违规 / 提示。"""
    # 先做确定性校正：与止损互斥的买入条目标作废、仓位口径不一致给提示
    plan = plan if isinstance(plan, dict) else {}
    plan["计划校正"] = normalize_plan(plan, account, holding, price)
    checks = []
    total = float(account.get("总资金") or 0)
    cap_pct = float(account.get("单票仓位上限_pct") or 0)
    max_loss_pct = float(account.get("单笔最大亏损_pct") or 0)
    min_cash_pct = float(account.get("最低现金比例_pct") or 0)
    pos = plan.get("仓位") or {}
    prices = plan.get("关键价位") or {}
    entries = plan.get("计划") if isinstance(plan.get("计划"), list) else []
    stop = num_or_none(prices.get("止损价"))
    suggest_pct = num_or_none(pos.get("建议_pct"))
    cur_pct = num_or_none(pos.get("当前_pct"))
    if cur_pct is None:
        cur_pct = num_or_none(holding.get("占总资金_pct"))
    voided = [e for e in entries if isinstance(e, dict) and e.get("作废")]
    live_entries = [e for e in entries if not (isinstance(e, dict) and e.get("作废"))]

    def add(item, result, note):
        checks.append({"项": item, "结果": result, "说明": note})

    # 1) 单票仓位上限
    if suggest_pct is None:
        add("建议仓位 ≤ 单票上限", "提示", "模型未给「建议_pct」，无法校验（上限 %s%%）" % cap_pct)
    elif suggest_pct > cap_pct + 0.01:
        add("建议仓位 ≤ 单票上限", "违规",
            "建议 %s%% > 上限 %s%%，须下调；按上限计算的可投金额 %.2f 元"
            % (fmt_num(suggest_pct, 2), fmt_num(cap_pct, 2), total * cap_pct / 100.0))
    else:
        add("建议仓位 ≤ 单票上限", "通过",
            "建议 %s%% ≤ 上限 %s%%" % (fmt_num(suggest_pct, 2), fmt_num(cap_pct, 2)))

    # 2) 止损价方向自洽
    if stop is None or price is None:
        add("止损价 < 现价（多头自洽）", "提示", "止损价或现价缺失，无法校验")
    elif stop >= price:
        add("止损价 < 现价（多头自洽）", "违规",
            "止损 %s ≥ 现价 %s：多头计划的止损必须落在现价下方（若为做空或清仓条件，请改写为失效条件）"
            % (fmt_num(stop), fmt_num(price)))
    else:
        add("止损价 < 现价（多头自洽）", "通过",
            "止损 %s 距现价 %s = %.2f%%" % (fmt_num(stop), fmt_num(price),
                                          (price - stop) / price * 100.0))

    # 3) 单笔最大亏损约束
    # 3) 单笔最大亏损约束（成本口径为主，现价口径并列）
    buy_entries = [e for e in live_entries if _action_kind(e.get("动作")) == "买入"]
    buy_shares, buy_amount = 0.0, 0.0
    for e in buy_entries:
        q = num_or_none(e.get("股数")) or 0.0
        lo, hi = _price_range(e)
        ref = hi or lo or price
        amt = num_or_none(e.get("金额_元"))
        if amt is None and q and ref:
            amt = q * ref
        buy_shares += q
        buy_amount += float(amt or 0)
    held_qty = num_or_none(holding.get("持有股数")) or 0.0
    held_cost = num_or_none(holding.get("成本价"))
    buy_ref_price = (buy_amount / buy_shares) if (buy_shares and buy_amount) else None
    # 止损敞口按「未执行减仓/清仓」的保守口径：卖出是执行动作，不能用来抵消止损风险
    target_qty = held_qty + buy_shares
    cost_risk = 0.0
    if stop is not None and held_cost is not None:
        cost_risk += max(0.0, (held_cost - stop) * held_qty)
    if stop is not None and buy_ref_price is not None:
        cost_risk += max(0.0, (buy_ref_price - stop) * buy_shares)
    now_risk = max(0.0, (price - stop) * target_qty) if (price and stop) else None
    allow = total * max_loss_pct / 100.0
    if stop is None or price is None or (held_qty <= 0 and not buy_entries):
        add("预计亏损 ≤ 单笔最大亏损", "提示", "缺少止损价/持仓/买入金额，无法校验（需人工确认）")
    else:
        detail = ("成本口径：持仓 %s 股 ×(成本 %s−止损 %s) + 计划买入 %s 股 ×(均价 %s−止损 %s) = %.2f 元；"
                  "现价口径（止损敞口）：%s 股 ×(现价 %s−止损 %s) = %s 元；"
                  "上限 = 总资金 %.0f × %s%% = %.2f 元；敞口按未执行减仓/清仓的保守口径计"
                  % (fmt_num(held_qty), fmt_num(held_cost), fmt_num(stop),
                     fmt_num(buy_shares), fmt_num(buy_ref_price), fmt_num(stop), cost_risk,
                     fmt_num(target_qty), fmt_num(price), fmt_num(stop),
                     fmt_num(now_risk) if now_risk is not None else "null",
                     total, fmt_num(max_loss_pct, 2), allow))
        if now_risk is not None and now_risk > allow + 0.01:
            cut = allow * price / max(price - (stop or 0), 1e-9)
            add("预计亏损 ≤ 单笔最大亏损", "违规",
                "现价口径（真正的止损敞口）已超限 %.2f 元 > %.2f 元；%s；"
                "在现价 %s 与止损 %s 不变时，敞口应压到约 %.2f 元以内（约减仓到 %.0f 股）"
                % (now_risk, allow, detail, fmt_num(price), fmt_num(stop), cut,
                   max(0.0, cut / max(price - (stop or 0), 1e-9))))
        elif cost_risk > allow + 0.01:
            add("预计亏损 ≤ 单笔最大亏损", "提示",
                "现价口径未超限，但成本口径已超（%.2f 元 > %.2f 元，属浮亏状态下的既有风险，非新增敞口）：%s"
                % (cost_risk, allow, detail))
        else:
            add("预计亏损 ≤ 单笔最大亏损", "通过", detail)

    # 4) 买入触发价 ≥ 止损价
    bad = []
    for e in buy_entries:
        lo, _hi = _price_range(e)
        if lo is not None and stop is not None and lo < stop:
            bad.append("%s（区间下沿 %s < 止损 %s）" % (e.get("动作"), fmt_num(lo), fmt_num(stop)))
    tail = ("；已作废 %d 条与止损冲突的买入/加仓条目（见「计划校正」）" % len(voided)) if voided else ""
    if stop is None or not buy_entries:
        add("买入/加仓触发价 ≥ 止损价", "提示", "无买入类计划或缺止损价，跳过")
    elif bad:
        add("买入/加仓触发价 ≥ 止损价", "违规", "；".join(bad) + tail)
    else:
        add("买入/加仓触发价 ≥ 止损价", "通过", "买入类计划的价格区间均在止损价之上" + tail)

    # 5) 卖出股数 ≤ 计划执行日可卖（T+1 解冻后）
    avail_now = num_or_none(holding.get("可用股数_可卖"))
    avail_next = num_or_none(holding.get("可卖_计划执行日"))
    if avail_next is None:
        avail_next = avail_now
    frozen = num_or_none(holding.get("冻结股数_当日买入不可卖"))
    sell_entries = [e for e in live_entries if _action_kind(e.get("动作")) == "卖出"]
    sell_bad, sell_total = [], 0.0
    for e in sell_entries:
        q = num_or_none(e.get("股数"))
        if q is None:
            continue
        sell_total += q
        if avail_next is not None and q > avail_next:
            sell_bad.append("%s %s 股 > 计划执行日可卖 %s 股" % (e.get("动作"), fmt_num(q), fmt_num(avail_next)))
    if not sell_entries:
        add("卖出股数 ≤ 可用余额（T+1 可卖）", "提示", "无卖出类计划")
    elif sell_bad:
        add("卖出股数 ≤ 可用余额（T+1 可卖）", "违规", "；".join(sell_bad))
    else:
        note = ("计划执行日可卖 %s 股（当前可用 %s + T+1 解冻 %s）；各卖出条目合计 %s 股"
                % (fmt_num(avail_next), fmt_num(avail_now), fmt_num(frozen), fmt_num(sell_total)))
        if avail_next is not None and sell_total > avail_next + 0.5:
            add("卖出股数 ≤ 可用余额（T+1 可卖）", "提示",
                note + "：合计已超可卖，若这些条目可能同时触发，必须按可用数量排序执行；"
                       "若属互斥分支（如反弹减仓 / 破位清仓），则忽略本条。")
        else:
            add("卖出股数 ≤ 可用余额（T+1 可卖）", "通过", note)

    # 6) 100 股整数倍
    odd = [fmt_num(num_or_none(e.get("股数"))) for e in live_entries
           if (num_or_none(e.get("股数")) or 0) % 100 != 0]
    if not live_entries:
        add("股数为 100 的整数倍", "提示", "无计划条目")
    elif odd:
        add("股数为 100 的整数倍", "违规", "非整手：%s" % "、".join(odd))
    else:
        add("股数为 100 的整数倍", "通过", "全部计划股数均为整手")

    # 7) 现金比例下限（含其它持仓）
    target_pct = suggest_pct if suggest_pct is not None else cur_pct
    if target_pct is None:
        add("现金比例 ≥ 下限", "提示", "缺建议仓位，无法校验")
    else:
        other_pct = (float(other_mv) / total * 100.0) if total else 0.0
        cash_pct = 100.0 - target_pct - other_pct
        if cash_pct < min_cash_pct - 0.01:
            add("现金比例 ≥ 下限", "违规",
                "该股建议 %.2f%% + 其它持仓 %.2f%% → 现金仅 %.2f%% < 下限 %.2f%%"
                % (target_pct, other_pct, cash_pct, min_cash_pct))
        else:
            add("现金比例 ≥ 下限", "通过",
                "该股 %.2f%% + 其它持仓 %.2f%% → 现金 %.2f%%（下限 %.2f%%）"
                % (target_pct, other_pct, cash_pct, min_cash_pct))

    # 8) 费用口径（信息项）
    fee_rows = []
    for e in live_entries:
        kind = _action_kind(e.get("动作"))
        if kind == "观察":
            continue
        amt = num_or_none(e.get("金额_元"))
        if amt is None:
            q = num_or_none(e.get("股数"))
            lo, hi = _price_range(e)
            amt = (q * (hi or lo)) if (q and (hi or lo)) else None
        if amt:
            c = trade_cost(is_etf, kind, amt, account)
            fee_rows.append("%s %.2f 元 → 费用 %.2f 元（佣 %.2f / 印 %.2f / 过 %.2f）"
                            % (e.get("动作"), amt, c["合计"], c["佣金"], c["印花税"], c["过户费"]))
    add("交易费用口径", "提示",
        ("ETF：免印花税与过户费；" if is_etf else "A股：卖出收印花税、双边收过户费；")
        + ("；".join(fee_rows) if fee_rows else "无可测算的计划金额"))
    return checks


# ===== [SEC-15] 上次计划机械回检 =====

def plan_log_path(ai_base):
    return os.path.join(ai_base or AI_DIR, "history", "plan_log.jsonl")


def load_log_records(log_path=PLAN_LOG):
    out = []
    for line in read_text(log_path).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def find_prev_record(c6, trade_date, log_path=PLAN_LOG):
    """取最近一条「同一标的且交易日严格早于当前交易日」的记录（同日重跑不自我复盘）。"""
    prev = None
    for rec in load_log_records(log_path):
        if code6((rec.get("标的") or {}).get("代码")) != c6:
            continue
        d = str(rec.get("交易日") or "")
        if trade_date and d and d >= str(trade_date):
            continue
        prev = rec
    return prev


def load_kline(thscode):
    doc = read_json(os.path.join(DATA_DIR, "history", "%s.json" % thscode))
    rows = (doc or {}).get("rows")
    return rows if isinstance(rows, list) else []


def build_review(prev, thscode, trade_date):
    """按「计划日次一交易日 → 当前交易日」的日K极值，机械判定触发与方向命中。"""
    if not prev:
        return {"是否有上次计划": False, "说明": "plan_log 中没有更早的同标的记录，本次为首份。"}
    plan_date = str(prev.get("交易日") or "")
    rows = [r for r in load_kline(thscode)
            if plan_date < str(r.get("date")) <= str(trade_date or "9999-99-99")]
    out = {"是否有上次计划": True, "上次计划日": plan_date, "上次时段": prev.get("phase"),
           "上次方向": prev.get("方向"), "上次置信度": prev.get("置信度"),
           "上次报告": prev.get("报告"), "基准价_计划时点": prev.get("基准价")}
    if not rows:
        out["判定"] = "无法回检：本地日K缓存未覆盖（%s, %s] 区间" % (plan_date, trade_date)
        return out
    highs = [num_or_none(r.get("high")) for r in rows]
    lows = [num_or_none(r.get("low")) for r in rows]
    closes = [num_or_none(r.get("close")) for r in rows]
    highs = [v for v in highs if v is not None]
    lows = [v for v in lows if v is not None]
    closes = [v for v in closes if v is not None]
    if not highs or not lows or not closes:
        out["判定"] = "无法回检：日K极值或收盘缺失"
        return out
    base = num_or_none(prev.get("基准价"))
    out["区间交易日数"] = len(rows)
    out["期间最高"] = max(highs)
    out["期间最低"] = min(lows)
    out["期间最后收盘"] = closes[-1]
    chg = pct(closes[-1], base) if base else None
    out["区间涨跌幅_pct"] = None if chg is None else round(chg, 3)
    hits = []
    for e in (prev.get("计划") or []):
        kind = _action_kind(e.get("动作"))
        lo, hi = _price_range(e)
        stop = num_or_none((prev.get("关键价位") or {}).get("止损价"))
        if kind == "观察" or (lo is None and hi is None):
            hits.append({"动作": e.get("动作"), "触发条件": e.get("触发条件"),
                         "判定": "无价位条件，未机械判定"})
            continue
        lo = lo if lo is not None else hi
        hi = hi if hi is not None else lo
        touched = (min(lows) <= hi) and (max(highs) >= lo)
        note = "期间价格区间 [%s, %s] 与计划区间 [%s, %s] %s" % (
            fmt_num(min(lows)), fmt_num(max(highs)), fmt_num(lo), fmt_num(hi),
            "相交 → 已触发" if touched else "不相交 → 未触发")
        if stop is not None and kind == "买入" and min(lows) <= stop:
            note += "；期间最低 %s 已跌破止损 %s（原计划应已失效/止损）" % (fmt_num(min(lows)),
                                                                        fmt_num(stop))
        hits.append({"动作": e.get("动作"), "触发条件": e.get("触发条件"),
                     "计划区间": [lo, hi], "判定": "已触发" if touched else "未触发",
                     "依据": note})
    out["计划回检"] = hits
    direction = str(prev.get("方向") or "")
    if chg is None:
        out["方向判定"] = "无基准价，无法判定"
    elif abs(chg) < 1.0:
        out["方向判定"] = "横盘（|涨跌幅| < 1%），判为中性"
    else:
        actual = "偏多" if chg > 0 else "偏空"
        out["方向判定"] = ("命中（实际%s，判定%s）" % (actual, direction or "未记录")
                        if direction == actual else
                        "未命中（实际%s，判定%s）" % (actual, direction or "未记录"))
    return out


def update_log_review(c6, trade_date, review, log_path=PLAN_LOG):
    """把回检结果写回 plan_log 中对应的那条历史记录。"""
    lines = read_text(log_path).splitlines()
    target, best_idx = None, -1
    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if code6((obj.get("标的") or {}).get("代码")) != c6:
            continue
        d = str(obj.get("交易日") or "")
        if trade_date and d and d >= str(trade_date):
            continue
        target, best_idx = obj, i
    if best_idx < 0 or target is None:
        return None
    target["复盘"] = review
    lines[best_idx] = json.dumps(target, ensure_ascii=False)
    write_text(log_path, "\n".join(lines) + "\n")
    return best_idx


# ===== [SEC-16] 报告渲染（Markdown） =====

def _cell(v):
    if v is None:
        return "null"
    if isinstance(v, list):
        return "、".join(_cell(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, bool):
        return "是" if v else "否"
    return str(v)


def md_table(rows, cols=None):
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    if not rows:
        return ["（无）"]
    cols = cols or list(rows[0].keys())
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        out.append("| " + " | ".join(_cell(r.get(c)) for c in cols) + " |")
    return out


def _sec_by_title(secs, prefix):
    for s in secs:
        if str(s.get("标题") or "").startswith(prefix):
            return s.get("内容") or ["（空）"]
    return ["（本次未生成该节）"]


def render_report(ctx, secs, research, review, checks):
    plan = research.get("json") if isinstance(research.get("json"), dict) else {}
    prices = plan.get("关键价位") or {}
    pos = plan.get("仓位") or {}
    entries = plan.get("计划") if isinstance(plan.get("计划"), list) else []
    lines = []
    lines.append("# AI 研判报告 — %s %s — %s %s" % (ctx["name"], ctx["code"],
                                                PHASE_LABEL.get(ctx["phase"], ctx["phase"]),
                                                ctx["trade_date"]))
    lines.append("")
    lines.append("> 本报告由大模型基于中性事实数据生成，不构成投资建议。")
    lines.append("> 生成时间：%s ｜ 模型档：%s（%s）｜ 研判模型：%s ｜ 复核模型：%s ｜ Prompt 版本：%s"
                 % (iso_now(), ctx["profile_name"], ctx["profile_src"],
                    ctx["model_pro"], ctx["model_review"] or "未启用", PROMPT_VERSION))
    lines.append("> 事实包 sha1：%s ｜ 字符数：%d ｜ 数据文件：%s"
                 % (ctx["factpack_sha1"], ctx["factpack_chars"],
                    os.path.basename(ctx["pan_path"] or "") + " + " + os.path.basename(ctx["s3_path"] or "")))
    lines.append("")

    # 0 结论速览
    lines.append("## 0 结论速览")
    if plan:
        lines.append("- 方向：**%s** ｜ 置信度：%s ｜ 时间窗：%s"
                     % (_cell(plan.get("方向")), _cell(plan.get("置信度")), _cell(plan.get("时间窗"))))
        lines.append("- 一句话结论：%s" % _cell(plan.get("一句话结论")))
        lines.append("- 仓位：当前 %s%% ｜ 建议 %s%% ｜ 上限 %s%% ｜ 现金保留 %s%%"
                     % (_cell(pos.get("当前_pct")), _cell(pos.get("建议_pct")),
                        _cell(pos.get("上限_pct")), _cell(pos.get("现金保留_pct"))))
        lines.append("- 止损价：%s ｜ 目标位：%s"
                     % (_cell(prices.get("止损价")),
                        "、".join(_cell(t.get("价位")) for t in (prices.get("目标位") or [])
                                 if isinstance(t, dict))))
    else:
        lines.append("- 研判档未产出可解析的结构化结论（原因见第 11 节与第 12 节）。")
    lines.append("")

    # 1 上次计划复盘
    lines.append("## 1 上次计划复盘（机械判定，非策略评价）")
    rv = ctx["review_prev"] or {}
    if not rv.get("是否有上次计划"):
        lines.append("- %s" % _cell(rv.get("说明")))
    else:
        lines.append("- 上次计划日：%s（%s 时段）｜ 上次方向：%s ｜ 基准价：%s"
                     % (_cell(rv.get("上次计划日")), _cell(rv.get("上次时段")),
                        _cell(rv.get("上次方向")), _cell(rv.get("基准价_计划时点"))))
        lines.append("- 回检区间：%s 个交易日 ｜ 期间最高 %s / 最低 %s / 最后收盘 %s ｜ 区间涨跌幅 %s%%"
                     % (_cell(rv.get("区间交易日数")), _cell(rv.get("期间最高")),
                        _cell(rv.get("期间最低")), _cell(rv.get("期间最后收盘")),
                        _cell(rv.get("区间涨跌幅_pct"))))
        lines.append("- 方向判定：%s" % _cell(rv.get("方向判定") or rv.get("判定")))
        lines.extend(md_table(rv.get("计划回检"), ["动作", "触发条件", "计划区间", "判定", "依据"]))
    lines.append("")

    # 2 数据完整性与降级
    lines.append("## 2 数据完整性与降级")
    lines.extend(_sec_by_title(secs, "①"))
    if ctx.get("trimmed"):
        lines.append("- 输入预算裁剪：")
        lines.extend("  - %s（%s → %s 字符）" % (t.get("裁剪"), t.get("裁剪前字符"), t.get("裁剪后字符"))
                     for t in ctx["trimmed"])
    else:
        lines.append("- 输入预算裁剪：未触发（事实包在预算内）")
    if ctx.get("attachments"):
        lines.append("- 原始附件：")
        lines.extend("  - %s：%s" % (a.get("附件"), a.get("状态"))
                     for a in ctx["attachments"])
    lines.append("")

    # 3-7 事实包正文
    lines.append("## 3 大盘环境")
    for pfx in ("②", "③", "④", "⑤", "⑥", "⑦"):
        body = _sec_by_title(secs, pfx)
        if body and not str(body[0]).startswith("（本次未生成"):
            lines.extend(body)
            lines.append("")
    for pfx, title in (("⑧", "## 4 个股技术面"), ("⑨", "## 5 个股基本面"),
                       ("⑩", "## 6 个股消息面"), ("⑪", "## 7 持仓与账户")):
        lines.append(title)
        lines.extend(_sec_by_title(secs, pfx))
        lines.append("")

    # 8 走势研判
    lines.append("## 8 走势研判（情景树）")
    if plan:
        for sc in (plan.get("情景树") or []):
            if not isinstance(sc, dict):
                continue
            lines.append("### %s（概率 %s%%）" % (_cell(sc.get("情形")), _cell(sc.get("概率_pct"))))
            lines.append("- 路径：%s" % _cell(sc.get("路径")))
            lines.append("- 验证信号：%s" % "；".join(_cell(x) for x in (sc.get("验证信号") or [])))
            lines.append("- 失效条件：%s" % _cell(sc.get("失效条件")))
        lines.append("")
        lines.append("**关键价位**")
        rows = []
        for s in (prices.get("支撑") or []):
            if isinstance(s, dict):
                rows.append({"类型": "支撑", "价位": s.get("价位"), "依据": s.get("依据")})
        for s in (prices.get("压力") or []):
            if isinstance(s, dict):
                rows.append({"类型": "压力", "价位": s.get("价位"), "依据": s.get("依据")})
        for s in (prices.get("目标位") or []):
            if isinstance(s, dict):
                rows.append({"类型": "目标位", "价位": s.get("价位"), "依据": s.get("依据")})
        rows.append({"类型": "止损", "价位": prices.get("止损价"), "依据": "模型给定"})
        lines.extend(md_table(rows))
        lines.append("")
        lines.append("**风险**")
        lines.extend(md_table(plan.get("风险"), ["风险", "监控指标", "应对"]))
        lines.append("")
        dep = plan.get("数据依赖") or {}
        lines.append("- 数据依赖（模型自述）：降级项 %s ｜ 缺失导致的不确定性：%s"
                     % ("；".join(_cell(x) for x in (dep.get("降级项") or [])),
                        _cell(dep.get("缺失导致的不确定性"))))
    else:
        lines.append("- 未产出结构化研判。模型原始输出见第 12 节。")
    lines.append("")

    # 9 交易计划
    lines.append("## 9 交易计划")
    live_entries = [e for e in entries if not (isinstance(e, dict) and e.get("作废"))]
    voided_entries = [e for e in entries if isinstance(e, dict) and e.get("作废")]
    if live_entries:
        lines.extend(md_table(live_entries, ["优先级", "动作", "触发条件", "价格区间", "股数",
                                             "金额_元", "失效条件"]))
    else:
        lines.append("- 无可执行计划条目。")
    corr = plan.get("计划校正") or {}
    if voided_entries:
        lines.append("")
        lines.append("**已作废条目（与止损互斥，脚本确定性作废，未替换为任何猜测价位）**")
        lines.extend(md_table([{"动作": e.get("动作"), "价格区间": e.get("价格区间"),
                                "作废原因": e.get("作废原因")} for e in voided_entries]))
    if corr.get("提示"):
        lines.append("")
        lines.append("**计划一致性提示**")
        lines.extend("- " + str(t) for t in corr["提示"])
    lines.append("")
    lines.append("**风控参数**")
    lines.extend(md_table([
        {"项": "止损价", "值": prices.get("止损价")},
        {"项": "目标位", "值": "、".join(_cell(t.get("价位")) for t in (prices.get("目标位") or [])
                                     if isinstance(t, dict))},
        {"项": "建议仓位_pct", "值": pos.get("建议_pct")},
        {"项": "单票上限_pct", "值": pos.get("上限_pct")},
        {"项": "现金保留_pct", "值": pos.get("现金保留_pct")},
        {"项": "当前可用股数", "值": (ctx["holding"] or {}).get("可用股数_可卖")},
        {"项": "T+1 解冻股数", "值": (ctx["holding"] or {}).get("冻结股数_当日买入不可卖")},
        {"项": "计划执行日可卖", "值": (ctx["holding"] or {}).get("可卖_计划执行日")},
        {"项": "当前持仓股数", "值": (ctx["holding"] or {}).get("持有股数")},
        {"项": "成本价", "值": (ctx["holding"] or {}).get("成本价")},
    ]))
    lines.append("")
    lines.append("- 可卖口径：%s" % _cell((ctx["holding"] or {}).get("计划执行日口径")))
    lines.append("")

    # 10 机械风控校验
    lines.append("## 10 机械风控校验（脚本确定性规则）")
    lines.extend(md_table(checks, ["项", "结果", "说明"]))
    lines.append("")

    # 11 复核档
    lines.append("## 11 复核档质询（红队）")
    robj = review.get("json") if isinstance(review.get("json"), dict) else None
    if robj:
        lines.append("- 是否推翻研判结论：%s" % _cell(robj.get("是否推翻结论")))
        lines.extend(md_table(robj.get("质询"), ["严重度", "点", "理由", "建议"]))
        lines.append("")
        lines.append("- 修正要点：%s" % "；".join(_cell(x) for x in (robj.get("修正要点") or [])))
        lines.append("- 遗漏的关键数据：%s" % "；".join(_cell(x) for x in (robj.get("遗漏的关键数据") or [])))
    else:
        lines.append("- 未启用或未产出：%s" % _cell(review.get("error") or "已按 --no-review 关闭"))
    lines.append("")

    # 12 运行元信息
    lines.append("## 12 原始附件与运行元信息")
    lines.append("- pan 文件：`%s`（sha1 %s）" % (ctx["pan_path"], ctx["pan_sha1"]))
    lines.append("- stock3d 文件：`%s`（sha1 %s）" % (ctx["s3_path"], ctx["s3_sha1"]))
    lines.append("- 事实包：字符 %d ｜ sha1 %s ｜ 章节 %d"
                 % (ctx["factpack_chars"], ctx["factpack_sha1"], len(secs)))
    for role in ("研判", "复核"):
        r = research if role == "研判" else review
        if not r.get("called"):
            continue
        cost = r.get("cost") or {}
        lines.append("- %s调用：协议 %s ｜ 模型 %s ｜ 耗时 %s ms ｜ token 输入 %s / 输出 %s / 缓存读 %s "
                     "｜ 费用 %s %s（约 ¥%s）%s"
                     % (role, _cell(r.get("协议")), _cell(r.get("model")), _cell(r.get("latency_ms")),
                        _cell((r.get("usage") or {}).get("输入")),
                        _cell((r.get("usage") or {}).get("输出")),
                        _cell((r.get("usage") or {}).get("缓存读取")),
                        _cell(cost.get("金额")), _cell(cost.get("币种")),
                        _cell(cost.get("人民币_估算")),
                        ("（%s）" % cost.get("说明")) if cost.get("说明") else ""))
        if not r.get("ok"):
            lines.append("  - 状态：**FAIL** —— %s" % _cell(r.get("error")))
        elif r.get("parse_error"):
            lines.append("  - 状态：**结构化解析 FAIL** —— %s" % _cell(r.get("parse_error")))
    bal = ctx.get("balance") or {}
    if bal:
        lines.append("- 账户余额探测：%s" % json.dumps(bal, ensure_ascii=False))
    lines.append("- 抓数日志：")
    for row in ctx.get("fetch_log") or []:
        lines.append("  - %s 返回码 %s ｜ 耗时 %s s" % (row.get("步骤"), row.get("返回码"),
                                                   row.get("耗时_秒")))
    if research.get("raw_text") and not research.get("json"):
        lines.append("")
        lines.append("### 研判档原始输出（未能解析为 JSON）")
        lines.append("```text")
        lines.append(str(research.get("raw_text"))[:6000])
        lines.append("```")
    if review.get("raw_text") and not review.get("json"):
        lines.append("")
        lines.append("### 复核档原始输出（未能解析为 JSON）")
        lines.append("```text")
        lines.append(str(review.get("raw_text"))[:4000])
        lines.append("```")
    lines.append("")
    lines.append("---")
    lines.append("数据来源：pan.py（大盘）+ stock3d.py（个股）；全部为中性事实与数值。"
                 "本报告由大模型生成，仅供研究参考，不构成投资建议，据此操作风险自担。")
    return "\n".join(lines)


# ===== [SEC-17] 落盘与历史 =====

def ai_out_dir(base, trade_date):
    return os.path.join(base, str(trade_date or now_local().strftime("%Y-%m-%d")).replace("-", ""))


def save_outputs(ai_base, phase, trade_date, report_md, payload, prompt_text=None):
    d = ai_out_dir(ai_base, trade_date)
    stamp = now_local().strftime("%H%M%S")
    md_path = os.path.join(d, "%s_%s.md" % (stamp, phase))
    js_path = os.path.join(d, "%s_%s.json" % (stamp, phase))
    write_text(md_path, report_md)
    write_json(js_path, payload)
    write_text(os.path.join(d, "latest_%s.md" % phase), report_md)
    write_json(os.path.join(d, "latest_%s.json" % phase), payload)
    prompt_path = None
    if prompt_text:
        prompt_path = os.path.join(d, "%s_%s.prompt.txt" % (stamp, phase))
        write_text(prompt_path, prompt_text)
    return {"md": md_path, "json": js_path, "prompt": prompt_path, "dir": d}


def append_plan_log(record, log_path=PLAN_LOG):
    append_line(log_path, json.dumps(record, ensure_ascii=False))
    return log_path


def build_log_record(ctx, plan, research, review, paths):
    usage_in = (research.get("usage") or {}).get("输入") or 0
    usage_out = (research.get("usage") or {}).get("输出") or 0
    r_usage = review.get("usage") or {}
    cost = research.get("cost") or {}
    r_cost = review.get("cost") or {}
    return {
        "时间": iso_now(),
        "phase": ctx["phase"],
        "交易日": ctx["trade_date"],
        "标的": {"代码": ctx["code"], "名称": ctx["name"]},
        "profile": ctx["profile_name"],
        "研判模型": ctx["model_pro"],
        "复核模型": ctx["model_review"],
        "事实包字符数": ctx["factpack_chars"],
        "事实包sha1": ctx["factpack_sha1"],
        "usage": {"研判_输入": usage_in, "研判_输出": usage_out,
                  "复核_输入": r_usage.get("输入") or 0, "复核_输出": r_usage.get("输出") or 0},
        "费用": {"研判_人民币_估算": cost.get("人民币_估算"),
                 "复核_人民币_估算": r_cost.get("人民币_估算")},
        "方向": (plan or {}).get("方向"),
        "置信度": (plan or {}).get("置信度"),
        "时间窗": (plan or {}).get("时间窗"),
        "关键价位": (plan or {}).get("关键价位"),
        "仓位": (plan or {}).get("仓位"),
        "计划": (plan or {}).get("计划"),
        "基准价": ctx.get("price"),
        "报告": paths.get("md"),
        "复盘": None,
    }


# ===== [SEC-18] 主流程 =====

def call_and_parse(provider, model, system, user, temperature, max_tokens, key,
                   json_mode=True, parse_retry=1, extra=None):
    """调一次模型并解析 JSON；解析失败时追加"只输出JSON"提示再试一次。返回 (结果, 对象, 错误)。"""
    r = call_model(provider, model, system, user, temperature=temperature, max_tokens=max_tokens,
                   json_mode=json_mode, key=key, extra=extra)
    if not r.get("ok"):
        return r, None, r.get("error")
    obj, err = extract_json(r.get("text"))
    if obj is not None or parse_retry <= 0:
        return r, obj, err
    hint = ("\n\n【重要】你上一次的输出不是合法 JSON。请只输出一个合法 JSON 对象："
            "不要解释文字、不要 markdown 代码围栏、不要注释、不要尾随逗号。")
    r2 = call_model(provider, model, system, user + hint, temperature=temperature,
                    max_tokens=max_tokens, json_mode=json_mode, key=key, extra=extra)
    if not r2.get("ok"):
        r2["parse_retry_of"] = True
        return r2, None, r2.get("error")
    u1, u2 = r.get("usage") or {}, r2.get("usage") or {}
    merged = {k: (u1.get(k) or 0) + (u2.get(k) or 0) for k in ("输入", "输出", "缓存读取", "总")}
    r2["usage"] = merged
    r2["latency_ms"] = (r.get("latency_ms") or 0) + (r2.get("latency_ms") or 0)
    r2["重试解析"] = True
    obj2, err2 = extract_json(r2.get("text"))
    return r2, obj2, err2


def _role_setup(models_cfg, prof, role, args):
    """返回 (provider, model, key, 错误)。"""
    conf = prof.get(role)
    if not isinstance(conf, dict):
        return None, None, None, "profile 里没有「%s」档配置" % role
    name = conf.get("provider")
    provider = find_provider(models_cfg, name)
    if provider is None:
        return None, None, None, "profile.%s 指向的 provider 不存在：%r" % (role, name)
    provider = copy.deepcopy(provider)
    if args.api_base:
        provider["base_url"] = args.api_base
    model = conf.get("model")
    if role == "研判" and args.model_pro:
        model = args.model_pro
    if role == "复核" and args.model_review:
        model = args.model_review
    if not model:
        return None, None, None, "profile.%s 未指定 model" % role
    key, _src = resolve_key(provider, args.api_key)
    if not key and provider.get("鉴权") != "none":
        return None, None, None, ("未找到 %s 的 API key（试过 --api-key / 环境变量 %s / 模型配置 / %s）"
                                  % (name, provider.get("key_env") or "-", TOKEN_FILE))
    return provider, model, key, None


def _locate_inputs(args, phase):
    pan_path = newest_pan_file(phase, args.date)
    s3_dir = args.s3_out or DATA_DIR
    date_str = (args.date or now_local().strftime("%Y-%m-%d")).replace("-", "")
    s3_path = os.path.join(s3_dir, "stock3d_%s.json" % date_str)
    if not os.path.exists(s3_path):
        cand = [os.path.join(s3_dir, n) for n in os.listdir(s3_dir)
                if re.match(r"^stock3d_\d{8}\.json$", n)] if os.path.isdir(s3_dir) else []
        s3_path = max(cand, key=os.path.getmtime) if cand else s3_path
    return pan_path, s3_path


def _pick_symbol(s3doc, c6):
    for sym in (s3doc or {}).get("symbols") or []:
        if code6(sym.get("code")) == c6:
            return sym
    return None


def cmd_run(args):
    phase = args.cmd
    ai_base = args.ai_out or AI_DIR
    account, err = load_account(args.account)
    if err:
        fail(err)
        return EXIT_BADARGS
    models_cfg, err = load_models(args.models)
    if err:
        fail(err)
        return EXIT_BADARGS
    prof_name, prof, prof_src = resolve_profile(models_cfg, phase, args.profile, args.phase_profile)
    if not prof:
        fail(prof_src)
        return EXIT_BADARGS
    c6 = code6(args.code)
    pool_rows = parse_pool_rows(args.pool)[1] if (args.pool and os.path.exists(args.pool)) else []
    if not c6:
        codes = "、".join((r.get("证券代码") or "") for r in pool_rows[:20])
        fail("必须用 --code 指定一只标的（6 位代码或 thscode）"
             + ("；持仓文件可选：" + codes if codes else ""))
        return EXIT_BADARGS

    # 1) 抓数（除非 --no-fetch）
    fetch_log, fetch_err = [], []
    if args.no_fetch:
        pan_path, s3_path = _locate_inputs(args, phase)
        stage("skip fetch (--no-fetch): reuse existing JSON")
    else:
        res = fetch_data(phase, args.code, args.date, args.pool, args.top,
                         args.pan_out, args.s3_out, debug=args.debug)
        fetch_log, fetch_err = res["日志"], res["错误"]
        pan_path, s3_path = res["pan_path"], res["s3_path"]
    if not pan_path or not os.path.exists(pan_path):
        fail("找不到 pan 快照（latest_%s.json）；先跑 pan.py %s 或去掉 --no-fetch" % (phase, phase))
        return EXIT_DATA
    if not s3_path or not os.path.exists(s3_path):
        fail("找不到 stock3d_*.json；先跑 stock3d.py pull 或去掉 --no-fetch")
        return EXIT_DATA
    for e in fetch_err:
        warn(e)

    pandoc = read_json(pan_path) or {}
    s3doc = read_json(s3_path) or {}
    sym = _pick_symbol(s3doc, c6)
    if sym is None:
        avail = "、".join(code6(s.get("code")) or "" for s in (s3doc.get("symbols") or []))
        fail("stock3d 数据里没有 %s（当前文件含：%s）" % (c6, avail or "无"))
        return EXIT_DATA
    tech = sym.get("tech") or {}
    fund = sym.get("fund") or {}
    trade_date = pandoc.get("trade_date") or s3doc.get("target_date") or now_local().strftime("%Y-%m-%d")
    name = sym.get("name") or (find_pool_row(pool_rows, c6) or {}).get("证券名称") or c6
    snap_price = num_or_none((tech.get("snapshot") or {}).get("现价"))
    holding_row = find_pool_row(pool_rows, c6)
    holding = holdings_metrics(holding_row, c6, name, snap_price, account)
    price = snap_price if snap_price is not None else num_or_none(holding.get("现价"))
    other_mv = 0.0
    for row in pool_rows:
        if code6(row.get("证券代码") or "") == c6:
            continue
        other_mv += num_or_none(row.get("市值")) or 0.0

    pan_data = _pan(pandoc)
    log_path = plan_log_path(ai_base)
    prev = find_prev_record(c6, trade_date, log_path)
    review_prev = build_review(prev, sym.get("code"), trade_date)

    ctx = {
        "phase": phase, "code": sym.get("code") or c6, "name": name,
        "trade_date": trade_date, "price": price,
        "pan": pandoc, "s3doc": s3doc, "s3sym": sym,
        "pan_path": pan_path, "s3_path": s3_path,
        "pan_sha1": sha1_file(pan_path), "s3_sha1": sha1_file(s3_path),
        "account": account, "holding": holding, "pool_rows": pool_rows,
        "industry": join_industry(pan_data, fund),
        "rank": join_rank(pan_data, c6, name),
        "review_prev": review_prev,
        "profile_name": prof_name, "profile_src": prof_src,
    }
    factpack, secs, trimmed, attachments = build_factpack(ctx, args.max_input_chars)
    ctx["trimmed"], ctx["attachments"] = trimmed, attachments
    ctx["factpack_chars"] = len(factpack)
    ctx["factpack_sha1"] = sha1_text(factpack)

    sys_p, user_p = build_research_messages(factpack, ctx["code"], name)
    role_provider, role_model, pro_key, e1 = _role_setup(models_cfg, prof, "研判", args)
    role_review, rev_model, rev_key, e2 = _role_setup(models_cfg, prof, "复核", args)
    ctx["model_pro"] = role_model or "-"
    ctx["model_review"] = (rev_model if not args.no_review else None)
    if e1:
        fail("研判档配置错误：" + e1)
        return EXIT_BADARGS
    if e2 and not args.no_review:
        warn("复核档不可用，将跳过复核：" + e2)

    stage("factpack %d chars | sections %d | trimmed %d" % (ctx["factpack_chars"], len(secs), len(trimmed)))
    for s in secs:
        say("    - %s (%d chars)" % (_ascii(s["标题"]), len("\n".join(s["内容"]))))

    # 2) dry-run：只落事实包与提示词
    if args.dry_run:
        d = ai_out_dir(ai_base, trade_date)
        stamp = now_local().strftime("%H%M%S")
        prompt_path = os.path.join(d, "%s_%s.prompt.txt" % (stamp, phase))
        write_text(prompt_path, "===== SYSTEM =====\n%s\n\n===== USER =====\n%s\n" % (sys_p, user_p))
        pack_path = os.path.join(d, "%s_%s.factpack.md" % (stamp, phase))
        write_text(pack_path, factpack)
        done("dry-run：未调用模型（零成本）")
        say("    prompt: %s" % _ascii(prompt_path))
        say("    factpack: %s (%d chars)" % (_ascii(pack_path), ctx["factpack_chars"]))
        say("    预估输入 token 约 %d（按 2.4 字符/token 估算）" % int(ctx["factpack_chars"] / 2.4))
        return EXIT_OK

    # 3) 研判档
    stage("call %s (%s) ..." % (role_model, role_provider.get("协议")))
    r = call_and_parse(role_provider, role_model, sys_p, user_p,
                       prof["研判"].get("temperature"), prof["研判"].get("max_tokens"),
                       pro_key, json_mode=True, extra=prof["研判"].get("参数"))
    r_res, r_obj, r_err = r
    if r_obj is not None:
        r_obj = normalize_research(r_obj)
    fx = float((models_cfg.get("汇率") or {}).get("USD_CNY") or 7.1)
    research = {
        "called": True, "role": "研判", "model": role_model, "ok": bool(r_res.get("ok")),
        "协议": r_res.get("协议"),
        "usage": r_res.get("usage") or {}, "latency_ms": r_res.get("latency_ms"),
        "error": r_res.get("error"), "json": r_obj, "parse_error": (None if r_obj else r_err),
        "raw_text": r_res.get("text") or "",
        "cost": compute_cost(role_provider, role_model, r_res.get("usage") or {}, fx),
        "provider": role_provider.get("名称"),
    }
    if not research["ok"]:
        fail("研判档调用失败：%s" % research["error"])
    elif r_obj is None:
        fail("研判档输出无法解析为 JSON：%s" % r_err)
    else:
        done("研判档 OK：方向 %s / 置信度 %s" % (_ascii(r_obj.get("方向")), _ascii(r_obj.get("置信度"))))

    # 4) 复核档
    review = {"called": False, "role": "复核", "model": None, "ok": False, "usage": {},
              "cost": {}, "json": None, "error": None, "raw_text": ""}
    if r_obj is None and not args.no_review:
        review["error"] = "研判档未产出结构化结论，复核档无对象可复核（已跳过，省一次调用）"
    elif (not args.no_review) and not e2 and role_review is not None:
        rs_p, ru_p = build_review_messages(factpack, r_obj, r_res.get("text") or "")
        stage("call %s (%s) [review] ..." % (rev_model, role_review.get("协议")))
        rr = call_and_parse(role_review, rev_model, rs_p, ru_p,
                            prof["复核"].get("temperature"), prof["复核"].get("max_tokens"),
                            rev_key, json_mode=True, extra=prof["复核"].get("参数"))
        rr_res, rr_obj, rr_err = rr
        if rr_obj is not None:
            rr_obj = normalize_review(rr_obj)
        review = {
            "called": True, "role": "复核", "model": rev_model, "ok": bool(rr_res.get("ok")),
            "协议": rr_res.get("协议"),
            "usage": rr_res.get("usage") or {}, "latency_ms": rr_res.get("latency_ms"),
            "error": rr_res.get("error"), "json": rr_obj, "parse_error": (None if rr_obj else rr_err),
            "raw_text": rr_res.get("text") or "",
            "cost": compute_cost(role_review, rev_model, rr_res.get("usage") or {}, fx),
            "provider": role_review.get("名称"),
        }
        if rr_obj is None:
            warn("复核档未产出可解析 JSON：%s" % (rr_err or review["error"]))
        else:
            done("复核档 OK：推翻=%s / 质询 %d 条"
                 % (_ascii(rr_obj.get("是否推翻结论")), len(rr_obj.get("质询") or [])))

    # 5) 机械校验 + 余额探测
    checks = mechanical_checks(account, holding, price, r_obj, holding.get("是否ETF"), other_mv)
    balance = None
    try:
        balance = probe_balance(role_provider, pro_key)
    except Exception as e:
        balance = {"探测失败": str(e)}
    ctx["balance"] = balance
    if isinstance(balance, dict) and num_or_none(balance.get("余额")) is not None:
        if float(balance["余额"]) < 5:
            warn("模型账户余额偏低：%s %s" % (balance.get("余额"), balance.get("币种")))

    # 6) 落盘
    report_md = render_report(ctx, secs, research, review, checks)
    payload = {
        "tool": "aiplan", "schema_version": SCHEMA_VERSION, "prompt_version": PROMPT_VERSION,
        "phase": phase, "generated_at": iso_now(), "trade_date": trade_date,
        "标的": {"代码": ctx["code"], "名称": name, "类型": sym.get("type")},
        "profile": {"名称": prof_name, "来源": prof_src, "研判": prof.get("研判"),
                    "复核": prof.get("复核")},
        "数据": {"pan_path": pan_path, "pan_sha1": ctx["pan_sha1"],
                 "stock3d_path": s3_path, "stock3d_sha1": ctx["s3_sha1"],
                 "抓数日志": fetch_log, "裁剪记录": trimmed, "附件记录": attachments},
        "事实包": {"字符数": ctx["factpack_chars"], "sha1": ctx["factpack_sha1"],
                 "章节": [{"标题": s["标题"], "字符数": len("\n".join(s["内容"]))} for s in secs]},
        "上次计划复盘": review_prev,
        "研判": research, "复核": review, "机械校验": checks,
        "账户": account, "持仓": holding, "现价": price,
        "账户余额": balance,
        "note": "本报告由大模型基于中性事实数据生成，不构成投资建议。",
    }
    paths = save_outputs(ai_base, phase, trade_date, report_md, payload)
    rec = build_log_record(ctx, r_obj, research, review, paths)
    append_plan_log(rec, log_path)
    update_log_review(c6, trade_date, review_prev, log_path)
    done("report: %s" % _ascii(paths["md"]))
    say("    json  : %s" % _ascii(paths["json"]))
    total_cny = (research["cost"].get("人民币_估算") or 0) + (review.get("cost", {}).get("人民币_估算") or 0)
    if research["cost"].get("说明") or (review.get("cost") or {}).get("说明"):
        say("    tokens: in %s / out %s ｜ cost: not priced (token only)"
            % (_ascii(research["usage"].get("输入")), _ascii(research["usage"].get("输出"))))
    else:
        say("    tokens: in %s / out %s ｜ cost ~CNY %s"
            % (_ascii(research["usage"].get("输入")), _ascii(research["usage"].get("输出")),
               _ascii(round(total_cny, 4))))
    if not research["ok"]:
        return EXIT_MODEL
    return EXIT_OK


def cmd_check_model(args):
    models_cfg, err = load_models(args.models)
    if err:
        fail(err)
        return EXIT_BADARGS
    prof_name, prof, src = resolve_profile(models_cfg, args.phase or "post", args.profile, None)
    if not prof:
        fail(src)
        return EXIT_BADARGS
    done("profile %s (%s)" % (_ascii(prof_name), _ascii(src)))
    fx = float((models_cfg.get("汇率") or {}).get("USD_CNY") or 7.1)
    rc = EXIT_OK
    for role in ("研判", "复核"):
        if not isinstance(prof.get(role), dict):
            say("  - %s: (profile 未配置此档，跳过)" % role)
            continue
        provider, model, key, e = _role_setup(models_cfg, prof, role, args)
        if e:
            fail("%s: %s" % (role, e))
            rc = EXIT_BADARGS
            continue
        r = call_model(provider, model, "你是连通性测试。", "只回复 OK 两个字符。",
                       temperature=0, max_tokens=PING_MAX_TOKENS, json_mode=False, key=key)
        cost = compute_cost(provider, model, r.get("usage") or {}, fx)
        say("  - %s: provider=%s protocol=%s model=%s key=%s"
            % (role, _ascii(provider.get("名称")), _ascii(provider.get("协议")),
               _ascii(model), _ascii(mask_key(key))))
        say("       url=%s" % _ascii(r.get("url")))
        if r.get("ok"):
            say("       OK latency=%sms usage=in %s / out %s / cache %s cost=%s"
                % (r.get("latency_ms"), (r.get("usage") or {}).get("输入"),
                   (r.get("usage") or {}).get("输出"), (r.get("usage") or {}).get("缓存读取"),
                   cost.get("人民币_估算") if cost.get("人民币_估算") is not None
                   else "not priced (token only)"))
            say("       text=%s" % _ascii((r.get("text") or "").strip()[:60]))
        else:
            fail("%s 调用失败：%s" % (role, _ascii(r.get("error"))))
            rc = EXIT_MODEL
        bal = probe_balance(provider, key)
        if bal:
            say("       balance=%s" % _ascii(json.dumps(bal, ensure_ascii=False)))
    return rc


def cmd_init_account(args):
    path, err = init_account(args.account, force=args.force)
    if err:
        warn(err)
        return EXIT_BADARGS
    done("已生成账户配置模板：%s（请填写真实「总资金」后再跑主流程）" % _ascii(path))
    return EXIT_OK


def cmd_init_models(args):
    path, err = init_models(args.models, force=args.force)
    if err:
        warn(err)
        return EXIT_BADARGS
    done("已生成模型配置模板：%s" % _ascii(path))
    say("    默认 profile：deepseek-双档（deepseek-v4-pro 研判 + deepseek-v4-flash 复核）")
    say("    换模型：改 providers/profiles 即可；profiles_by_phase 可按 prep/live/post 分别指定")
    return EXIT_OK


# ===== [SEC-19] CLI =====

USAGE_SAMPLES = """示例:
  python aiplan.py init-account                    生成账户配置模板（填「总资金」）
  python aiplan.py init-models                     生成模型配置模板
  python aiplan.py check-model                     连通性自检（研判/复核各 ping 一次）
  python aiplan.py post --code 002463.SZ           盘后：抓数→事实包→研判→复核→报告
  python aiplan.py prep --code 002463.SZ           盘前
  python aiplan.py live --code 002463.SZ           盘中
  python aiplan.py all  --code 002463.SZ           三段合并
  python aiplan.py post --code 002463.SZ --dry-run 只落事实包与提示词（零成本）
  python aiplan.py post --code 002463.SZ --no-fetch    复用已有 data/ JSON
  python aiplan.py post --code 002463.SZ --no-review   跳过复核档

落盘: data/ai/<YYYYMMDD>/<HHMMSS>_<phase>.md|.json + latest_<phase>.* + data/ai/history/plan_log.jsonl
模型层: 模型配置.json（OpenAI 兼容 / Anthropic Messages / Gemini generateContent 三协议）"""


def _add_run_args(sp):
    sp.add_argument("--code", metavar="CODE", help="要研判的标的（6位代码或 thscode，必填，一次一只）")
    sp.add_argument("--name", metavar="NAME", help="标的名称（可选，仅用于报告标题）")
    sp.add_argument("--date", metavar="YYYY-MM-DD", help="目标交易日（透传给 pan.py / stock3d.py）")
    sp.add_argument("--pool", metavar="FILE", default=DEFAULT_POOL, help="持仓文件（默认 持仓数据.md）")
    sp.add_argument("--account", metavar="FILE", default=DEFAULT_ACCOUNT, help="账户配置（默认 账户配置.json）")
    sp.add_argument("--models", metavar="FILE", default=DEFAULT_MODELS, help="模型配置（默认 模型配置.json）")
    sp.add_argument("--profile", metavar="NAME", help="临时指定 profile（覆盖 profiles_by_phase 与默认）")
    sp.add_argument("--phase-profile", metavar="NAME", help="按 phase 指定 profile（优先级低于 --profile）")
    sp.add_argument("--top", type=int, default=DEFAULT_TOP, help="抓数时各榜单保留条数（默认 %d）" % DEFAULT_TOP)
    sp.add_argument("--max-input-chars", type=int, default=DEFAULT_MAX_INPUT_CHARS,
                    help="事实包字符预算（默认 %d，超出按优先级裁剪）" % DEFAULT_MAX_INPUT_CHARS)
    sp.add_argument("--no-fetch", action="store_true", help="不抓数，复用已有 data/ JSON")
    sp.add_argument("--no-review", action="store_true", help="跳过复核档")
    sp.add_argument("--dry-run", action="store_true", help="只落事实包与提示词，不调用模型（零成本）")
    sp.add_argument("--api-key", metavar="KEY", help="临时覆盖 API key（只影响本次运行）")
    sp.add_argument("--api-base", metavar="URL", help="临时覆盖 provider base_url（排障用）")
    sp.add_argument("--model-pro", metavar="MODEL", help="临时覆盖研判模型名")
    sp.add_argument("--model-review", metavar="MODEL", help="临时覆盖复核模型名")
    sp.add_argument("--pan-out", metavar="DIR", default=PAN_DIR, help="pan.py 落盘基目录")
    sp.add_argument("--s3-out", metavar="DIR", default=DATA_DIR, help="stock3d.py 落盘基目录")
    sp.add_argument("--ai-out", metavar="DIR", default=AI_DIR, help="本脚本落盘基目录")
    sp.add_argument("--debug", action="store_true", help="透传抓数脚本的 --debug")


def build_parser():
    p = argparse.ArgumentParser(
        prog="aiplan.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="A股「大盘+单股」数据喂大模型，输出走势研判与交易计划（模型层可插拔）。",
        epilog=USAGE_SAMPLES)
    sub = p.add_subparsers(dest="cmd")
    for cmd, help_txt in (("prep", "盘前：外围/利率/供给/消息/竞价 + 单股三维"),
                          ("live", "盘中：指数量能/情绪/资金/板块 + 单股（含分时）"),
                          ("post", "盘后：龙虎榜/个股复盘/次日前瞻 + 单股三维"),
                          ("all", "三段合并")):
        sp = sub.add_parser(cmd, help=help_txt, epilog=USAGE_SAMPLES,
                            formatter_class=argparse.RawDescriptionHelpFormatter)
        _add_run_args(sp)
    sp = sub.add_parser("init-account", help="生成账户配置模板")
    sp.add_argument("--account", metavar="FILE", default=DEFAULT_ACCOUNT)
    sp.add_argument("--force", action="store_true", help="覆盖已存在的文件")
    sp = sub.add_parser("init-models", help="生成模型配置模板")
    sp.add_argument("--models", metavar="FILE", default=DEFAULT_MODELS)
    sp.add_argument("--force", action="store_true", help="覆盖已存在的文件")
    sp = sub.add_parser("check-model", help="模型连通性自检")
    sp.add_argument("--models", metavar="FILE", default=DEFAULT_MODELS)
    sp.add_argument("--profile", metavar="NAME")
    sp.add_argument("--phase", metavar="PHASE", default="post")
    sp.add_argument("--api-key", metavar="KEY")
    sp.add_argument("--api-base", metavar="URL")
    sp.add_argument("--model-pro", metavar="MODEL")
    sp.add_argument("--model-review", metavar="MODEL")
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # 默认子命令：pull 语义与 stock3d 一致（这里默认 post）
    if not argv or (argv[0] not in ("prep", "live", "post", "all", "init-account",
                                    "init-models", "check-model", "-h", "--help")):
        argv = ["post"] + argv
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "init-account":
        return cmd_init_account(args)
    if args.cmd == "init-models":
        return cmd_init_models(args)
    if args.cmd == "check-model":
        return cmd_check_model(args)
    return cmd_run(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        fail("interrupted")
        sys.exit(130)
