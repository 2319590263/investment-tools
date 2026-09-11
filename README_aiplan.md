# aiplan.py — 大盘 + 单股数据喂大模型，输出走势研判与交易计划

> 单文件 CLI。**不抓数**，只编排同目录的 `pan.py`（大盘）与 `stock3d.py`（个股三维），
> 把两份 JSON + 持仓文件 + 账户配置渲染成**精炼事实包**（原文附件按预算追加），
> 调**可插拔模型层**出结构化研判，再由复核档做第二轮质询，产出
> **Markdown 报告 + 结构化 JSON + 可复盘的历史记录**。
> 模型层不写死：`模型配置.json` 定义 provider 与 profile，支持
> **OpenAI 兼容 / Anthropic Messages / Gemini generateContent** 三种协议，换模型只改配置。
> 报告由大模型生成，**不构成投资建议**；数据部分仍是中性事实与数值。

```
python aiplan.py init-account                    # 生成 账户配置.json（必须先填「总资金」）
python aiplan.py init-models                     # 生成 模型配置.json
python aiplan.py check-model                     # 连通性自检（研判/复核各 ping 一次）

python aiplan.py post --code 002463.SZ           # 盘后：抓数→事实包→研判→复核→报告
python aiplan.py prep --code 002463.SZ           # 盘前
python aiplan.py live --code 002463.SZ           # 盘中（stock3d 带 --rt）
python aiplan.py all  --code 002463.SZ           # 三段合并

python aiplan.py post --code 002463.SZ --dry-run      # 只落事实包与提示词（零成本）
python aiplan.py post --code 002463.SZ --no-fetch     # 复用已有 data/ JSON
python aiplan.py post --code 002463.SZ --no-review    # 跳过复核档
```

**首次使用四步**：`init-account` → 填真实总资金 → `init-models` → `check-model` → 正式运行。

## 一、参数

| 参数 | 适用 | 说明 |
|---|---|---|
| `prep` / `live` / `post` / `all` | — | 四个时段子命令；省略子命令时默认 `post` |
| `--code CODE` | 运行类 | **必填**，一次只深度研判一只（6 位代码或 `002463.SZ`）。省略则报错并列出持仓文件可选代码 |
| `--name NAME` | 运行类 | 标的名称（可选，仅影响报告标题） |
| `--date YYYY-MM-DD` | 运行类 | 目标交易日，透传给 pan.py / stock3d.py |
| `--pool FILE` | 运行类 | 持仓文件（默认 `持仓数据.md`）；用于取成本/持仓/T+1 可卖与其它持仓市值 |
| `--account FILE` | 运行类 | 账户配置（默认 `账户配置.json`） |
| `--models FILE` | 运行类 | 模型配置（默认 `模型配置.json`） |
| `--profile NAME` | 运行类 | 临时指定 profile，覆盖 `profiles_by_phase` 与 `默认_profile` |
| `--phase-profile NAME` | 运行类 | 按 phase 指定 profile（优先级低于 `--profile`） |
| `--top N` | 运行类 | 透传给抓数脚本的榜单条数（默认 20） |
| `--max-input-chars N` | 运行类 | 事实包字符预算（默认 160000），超出按优先级裁剪并记录 |
| `--no-fetch` | 运行类 | 不抓数，复用已有 `data/` JSON |
| `--no-review` | 运行类 | 跳过复核档（省一次调用） |
| `--dry-run` | 运行类 | 只落事实包与提示词，**不调用模型（零成本）** |
| `--api-key KEY` | 运行类 / check-model | 临时覆盖 key（只生效本次） |
| `--api-base URL` | 运行类 / check-model | 临时覆盖 provider base_url（降级演练/排障） |
| `--model-pro` / `--model-review` | 运行类 / check-model | 临时覆盖模型名 |
| `--pan-out` / `--s3-out` / `--ai-out` | 运行类 | 三个落盘基目录（默认 `data/pan`、`data`、`data/ai`） |
| `--debug` | 运行类 | 透传抓数脚本的 `--debug` |
| `--force` | init-* | 覆盖已存在的配置文件 |

## 二、模型层（本轮核心：不顶死）

`模型配置.json` 由 `init-models` 生成，三段结构：

- **`providers[]`**：每个渠道一段。`名称` / `协议` / `base_url` / `路径` / `key_env` / `api_key` /
  `超时_秒` / `重试` / `json_object`（是否支持 `response_format=json_object`）/ `单价`（输入、输出、
  缓存读取、币种、单位）/ `模型单价`（按模型覆盖）/ `余额端点` / `模型可选` / `headers` / `鉴权`。
- **`profiles{}`**：每个 profile 由 `研判` 与 `复核` 两档组成（`复核` 可为 `null`），
  每档是 `{provider, model, temperature, max_tokens, 参数}`。
  `参数` 会**原样并入请求体**（OpenAI/Anthropic 顶层；Gemini 并入 `generationConfig`），
  用来传 provider 私有开关（推理档位、thinking 开关、top_p…），留空即不发。
- **`profiles_by_phase{}`**：让 `prep` / `live` / `post` / `all` 各用不同 profile；留空回落 `默认_profile`。

预置 provider：`deepseek`（默认，OpenAI 兼容）、`deepseek-anthropic`（同一 key 走 Anthropic 协议）、
`zhipu`、`siliconflow`、`modelscope`、`volces-ark`、`dashscope`、`moonshot`、`openrouter`、
`ollama`（本机）、`anthropic`、`gemini`。**除 DeepSeek 外都需要你自备 key**。

**key 解析顺序**：`--api-key` → 环境变量（`key_env` / `AI_PLAN_API_KEY` / `DEEPSEEK_API_KEY`）
→ provider 的 `api_key` → `C:\Users\m1526\Desktop\token.txt` 首行的 `sk-…`。日志只打印掩码。

**三种协议的实测差异**

| 协议 | 端点与认证 | JSON 约束 | 响应解析要点 |
|---|---|---|---|
| `openai-chat` | `{base}/chat/completions`，`Authorization: Bearer` | `response_format={"type":"json_object"}`（`json_object=true` 时发） | usage 取 `prompt_tokens` / `completion_tokens` / `prompt_tokens_details.cached_tokens`；**推理模型可能只有 `reasoning_content`，此时报"max_tokens 不足"** |
| `anthropic-messages` | `{base}/v1/messages`，`x-api-key` + `anthropic-version` | 无该开关，靠提示词 | `content[]` 里**只取 `type=text` 的块**（thinking 块要跳过）；usage 取 `input_tokens` / `output_tokens` / `cache_read_input_tokens` |
| `gemini-generate` | `{base}/v1beta/models/{model}:generateContent?key=…` | `generationConfig.responseMimeType=application/json` | 取 `candidates[0].content.parts[].text`；usage 取 `usageMetadata.*`。**协议已实现，但本机实测 `generativelanguage.googleapis.com` 15s 超时不可达，未经真实调用验证** |

**费用**：脚本按 provider 单价确定性计算（缓存读取单独计价，`计费输入 = 输入 − 缓存读取`），
报告与 `plan_log` 都记录 token、原币金额与人民币估算（汇率取配置里的 `汇率.USD_CNY`）。
声明了 `余额端点` 的 provider 会探测余额（DeepSeek 走 `/user/balance`），低于 5 元时告警。
**未配置任何非零单价时不编造金额**：报告与状态行写「not priced / 未配置单价，仅记录 token」。

**预置 profile**：`deepseek-双档`（pro 研判 + flash 复核）、`deepseek-单档`、`anthropic-双档`、
`zhipu-双档`（glm-5.3 + glm-5.3-flash）、`zhipu-全闪`。
`--profile` 可临时覆盖，`profiles_by_phase` 可按 prep/live/post 分别指定。

**「参数」是推理档位的唯一入口**：智谱 GLM-5.x 系列实测**始终思考、不能关闭**
（API 直接报错 `该模型始终思考，不支持关闭思考`），必须用 `reasoning_effort`；
不传会烧光 `max_tokens` 导致「只有 reasoning 没有正文」。实测同一份 8.6 万字符事实包：
`glm-5.3 + reasoning_effort=high` ≈ 81 秒、输出 3.5K 字符、JSON 完整；
`glm-5.3-flash + low` ≈ 33 秒。DeepSeek 侧同理（`max_tokens` 要给足，默认研判 12000 / 复核 16000）。

## 三、数据流与事实包

**抓数（默认自动，`--no-fetch` 可跳过）**

| 时段 | pan.py | stock3d.py |
|---|---|---|
| `prep` | `pan.py prep` | `stock3d.py pull <code>` |
| `live` | `pan.py live` | `stock3d.py pull <code> --rt` |
| `post` | `pan.py post --pool 持仓文件` | `stock3d.py pull <code>` |
| `all` | `pan.py all --pool 持仓文件` | `stock3d.py pull <code>` |

两个脚本**零改动**，仅被 subprocess 调用；抓数失败或块 `DEGRADE/FAIL` 一律透传进报告，绝不补值。
`pan.py` 的 `latest_<phase>.json` 按目录名+mtime 定位，`stock3d` 取 `stock3d_<YYYYMMDD>.json`。

**事实包（固定 12 节，脚本渲染，非模型生成）**

① 元信息与数据源状态（含全部 DEGRADE/FAIL/不可得清单）
② 指数与量能 ③ 情绪与广度 ④ 资金 ⑤ 板块轮动与关键时点
⑥ 盘前专项（外围/汇率/大宗/Shibor/DR007/政策/供给/消息分层/竞价，仅 `prep|all`）
⑦ 盘后专项（龙虎榜含东财上榜原因/个股复盘/次日前瞻/外围晚间，仅 `post|all`）
⑧ 个股技术面 ⑨ 个股基本面 ⑩ 个股消息面 ⑪ 持仓与账户 ⑫ 原始附件

- ②③④⑤ 在 `prep` 下显式标「不适用：本时段 pan.py 不产出该块」，不是失败。
- **确定性关联由脚本算，不交给模型**：用 stock3d 的东财/证监会行业名匹配 pan 的行业/概念板块与
  行业主力资金榜（给出「匹配词 / 匹配方式：完全一致 or 局部匹配」）、在成交额/换手/人气榜中的名次、
  是否在涨停/跌停/炸板池与连板名单中、该股持仓指标（浮盈率、T+1 可卖、占总资金）。
- 渲染器通用递归：扁平 dict 列表自动转 Markdown 表格，长文本截断，`null` 原样保留。
  英文键（`code`/`eps` 等）按源数据原样出现，不做翻译以免失真。
- **预算裁剪**：默认 160000 字符。超预算按固定顺序降级（股东明细 → 财报明细 → 龙虎榜长名单 →
  板块内部与池子 → 公告逐条 → 可比公司 → 基本面明细整块 → 消息面与盘后长名单整块 → 关闭附件内联），
  每步都记进报告与 JSON 的「裁剪记录」；仍有不可再裁的核心（实测约 5.2 万字符）时，事实包顶部打印
  一行超预算提示（**不隐藏、不伪造**）。
- **原始附件**：预算剩余 > 25% 时，把核心块（情绪/资金/板块/指数/技术/消息/基本面）以紧凑 JSON 内联到 ⑫，
  否则只列路径与 sha1。
- 提示词常量内置并打 `PROMPT_VERSION`，写入报告与 JSON；**稳定数据放前缀**（复核档复用同一事实包前缀，
  实测命中 prompt 缓存：复核调用 94188 输入 token 中 46976 为缓存读取）。

## 四、输出契约

**研判档**必须返回严格 JSON：`方向(偏多|中性|偏空)`、`置信度`、`时间窗`、`一句话结论`、
`情景树[{情形,概率_pct,路径,验证信号[],失效条件}]`、`关键价位{支撑[]/压力[]/止损价/目标位[]}`
（每项带依据）、`计划[{动作,触发条件,价格区间,股数,金额_元,失效条件,优先级}]`、
`仓位{当前_pct,建议_pct,上限_pct,现金保留_pct}`、`风险[{风险,监控指标,应对}]`、
`数据依赖{降级项[],缺失导致的不确定性}`。

**复核档**返回 `{质询[{点,严重度,理由,建议}], 是否推翻结论, 修正要点[], 遗漏的关键数据[]}`；
它复用同一事实包前缀以吃缓存。

**解析链**：整体 `json.loads` → ```json 围栏 → 花括号配对 → 仍失败则追加「只输出 JSON」提示重试 1 次
→ 仍失败则报告保留模型原文并标 `[FAIL] 结构化解析`（报告照出）。模型写歪的结构会被归一化
（缺键补 `null`、单条质询对象自动包成列表），避免"解析成功但字段为空"。

## 五、机械风控校验（脚本确定性规则，不由模型负责）

模型只做研判，**可执行性由脚本判定**，结果红黑分明地写进报告第 10 节：

1. 建议仓位 ≤ 单票仓位上限；2. 止损价 < 现价（多头自洽）；3. 预计亏损 ≤ 单笔最大亏损
（**双口径并列**：现价口径 = `(现价 − 止损价) × (持仓股数 + 计划买入股数)` 是真正的止损敞口，
超限即判违规，并给出「应减到多少股 / 把金额压到多少」；成本口径 = 相对成本的浮亏，仅作提示，
避免把浮亏当成新增风险）；4. 买入/加仓触发价 ≥ 止损价；5. 卖出股数 ≤ **计划执行日可卖**
（= 当前可用 + T+1 解冻；合计卖出超过可卖时给"互斥分支"提示而不是直接判违规）；
6. 股数为 100 的整数倍；7. 现金比例 ≥ 下限（并入其它持仓市值）；8. 费用口径
（佣金双边含最低值、印花税仅卖出 A 股、过户费仅 A 股，**ETF 免印花税与过户费**，
ETF 佣金可单列 `ETF_佣金费率_pct` / `ETF_佣金最低_元`）。

**计划校正（确定性，不改写模型数字）**：买入/加仓区间下沿低于止损价的条目会标 `作废`
（跌破止损即离场却在其下方接货，属逻辑互斥），在报告「交易计划」节单列并说明原因，**不替换成任何猜测价位**；
「持有 N 股」的市值占比与「建议_pct」相差 > 5 个百分点时给一致性提示；买入类与卖出类价位区间重叠时给动作歧义提示。

实测有效：抓出过「减仓 100 股但可用 0 股」的 T+1 问题、「加仓区间落在止损价下方」的互斥条目，
以及模型自认「按现价倒推止损、敞口刚好顶满 2%」这类无缓冲设计。

## 六、上次计划自动回检

每次生成报告前，读 `data/ai/history/plan_log.jsonl` 中**最近一条同标的、交易日严格早于当前交易日**
的记录（同日重跑不自我复盘），用本地日K缓存 `data/history/<thscode>.json` 覆盖
`(上次计划日, 当前交易日]` 区间，机械判定：

- 每条计划：期间 `[最低, 最高]` 与「价格区间」是否相交 → 已触发/未触发；买入类若期间最低已跌破止损，
  额外标注「原计划应已失效/止损」；
- 方向命中：区间最后收盘 vs 上次基准价，±1% 内记横盘，否则与上次「方向」比对命中/未命中。

结果写入报告第 1 节，并**回写**到 `plan_log` 中对应那条记录的 `复盘` 字段。

## 七、账户配置（`账户配置.json`）

`总资金`（元，**必填**）、`单票仓位上限_pct`、`单笔最大亏损_pct`（占总资金）、`最低现金比例_pct`、
`最大加仓次数`、`佣金费率_pct`、`佣金最低_元`、`ETF_佣金费率_pct`、`ETF_佣金最低_元`（可空，缺省回落个股费率，
兼容写成 `ETC_` 前缀）、`印花税率_pct`、`过户费率_pct`、`风险偏好`、`api_key`（可空）。

注意 `*_pct` 字段一律按**百分数**填（万分之一佣金写 `0.01`，不是 `0.0001`）；受「佣金最低_元」兜底时，
费率写小了实际会按最低值计，报告里的费用明细会体现真实取值。

**`总资金` 缺失或为 0 时主流程直接报错退出（exit 2）**，不会用占位值算股数。该文件只用于仓位与费用测算，
不改变数据工具的中性口径。

## 八、落盘

```
data/ai/<YYYYMMDD>/<HHMMSS>_<phase>.md|.json      报告 + 结构化结论（含事实包章节、复核、校验）
data/ai/<YYYYMMDD>/latest_<phase>.md|.json        当日指针
data/ai/<YYYYMMDD>/<HHMMSS>_<phase>.prompt.txt    --dry-run / 调试时的完整提示词
data/ai/<YYYYMMDD>/<HHMMSS>_<phase>.factpack.md   --dry-run 时的事实包
data/ai/history/plan_log.jsonl                    追加式历史（方向/计划/token/费用/复盘）
```

报告固定章节：免责声明与结论速览 → 上次计划复盘 → 数据完整性与降级 → 大盘环境 → 个股技术面 →
个股基本面 → 个股消息面 → 持仓与账户 → 走势研判（情景树）→ 交易计划 → 机械风控校验 →
复核档质询 → 原始附件与运行元信息。首行固定「本报告由大模型基于中性事实数据生成，不构成投资建议」。

## 九、铁律与已知边界（实测）

- **stdout 只出 ASCII**：中文一律进文件（PowerShell 管道会按 GBK 解码成乱码），路径里的中文显示为 `?`。
- **失败绝不伪装**：模型失败/解析失败时报告仍落盘并标 `[FAIL]`，退出码非 0；缺失一律 `null`。
- **退出码**：`0` 正常 ｜ `2` 参数或配置错误 ｜ `3` 模型调用失败 ｜ `4` 数据缺失/标的不在数据里。
- **持仓表解析**：`持仓数据.md` 是 **BOM + tab 分隔**且数据行首列为空，解析时不能 `strip()` 整行，
  否则列会整体错位（会把持仓股认成未持仓）。表格型（`|` 分隔）与纯代码清单也支持。
- **推理模型与 max_tokens**：DeepSeek 的 `deepseek-v4-pro` / `-flash` 都会先花 reasoning token，
  `max_tokens` 太小会出现「只有 reasoning_content 没有正文」。默认 profile 已给足
  （研判 12000、复核 16000），失败信息里也会直接说明原因。
- **JSON 模式**：DeepSeek OpenAI 端点实测支持 `response_format=json_object`；Anthropic 协议无该开关，
  只能靠提示词 + 解析链兜底。
- **模型名**：`deepseek-v4-flash[1m]` 是客户端别名，直接调 API 会 400；可用名为 `deepseek-v4-pro`、
  `deepseek-v4-flash`（后者在响应里回显为 `deepseek-flash`）。
- **免费渠道**：实测**没有可匿名调用的免费大模型接口**——智谱/硅基流动/魔搭/方舟/百炼/Kimi/MiniMax
  端点都通但都要注册取 key；Gemini 官方域名本机不可达；OpenRouter 有 `:free` 模型但也要 key。
  本脚本只预置骨架，不代注册；拿到任一 key 填进 `providers` 即可切换。
- **智谱（实测）**：`open.bigmodel.cn/api/paas/v4` 可用，OpenAI 兼容协议，`response_format=json_object` 有效；
  可用模型 glm-5.3 / glm-5.3-flash / glm-5.2 / glm-5 / glm-4.7 / glm-4.6 / glm-4.5-air。
  **prompt 缓存命中率极高**：实测 50648 输入 token 中 50624 命中缓存（复核档同一事实包前缀同样受益）。
  该系列不能关闭思考，必须传 `reasoning_effort`（见第二节）。
- **密钥绝不入库**：`.gitignore` 排除 `模型配置.json`、`账户配置.json`、`持仓数据.md`、`token.txt`、
  `credentials*`、`secrets*.json`、`data/`。推送前务必用
  `git grep -n -e '8d27…' -e 'sk-' HEAD` 自查一次历史里有没有明文 key。
- **DeepSeek 是有偿的**：实测余额接口返回充值余额（本机约 42 元）。一次 `post` 实测量级：
  事实包 8.5 万字符（约 3.6 万 token），研判档 pro 输入 4.6 万 / 输出 0.77 万 ≈ **¥0.65**，
  复核档 flash ≈ **¥0.18–0.26**，**单次合计约 ¥0.85**；每天三段约 ¥2–3。
  余额低于 5 元时脚本告警。
- **不适用 ≠ 失败**：`prep` 没有指数/情绪/资金/板块块；ETF/指数的基本面维度为「不适用」。
- **本脚本不做**：定时自动化、多标的组合优化、买卖建议的合规背书；也不改动 `pan.py` / `stock3d.py`。

## 十、维护须知

- 单文件，按 `# ===== [SEC-nn] 标题 =====` 分段；`Select-String -Path aiplan.py -Pattern '^# ====='`
  可列出骨架：常量 / 日志 / 工具 / 账户配置 / 模型配置 / 模型适配层 / JSON 提取 / 抓数编排 /
  持仓解析 / 事实包渲染器 / 事实包组装 / 提示词 / 机械校验 / 复盘 / 报告渲染 / 落盘 / 主流程 / CLI。
- 改了提示词或校验规则，记得同步 bump `PROMPT_VERSION`（写进报告与 plan JSON，便于跨版本对比效果）；
  模型/配置模板改了，要同步改 `MODELS_TEMPLATE` / `ACCOUNT_TEMPLATE` 两个常量，否则 `init-*` 生成的模板会落后。
- 改完必须跑：
  ```powershell
  python -c "import ast,io; ast.parse(io.open('aiplan.py',encoding='utf-8').read())"
  python aiplan.py --help
  python aiplan.py check-model                                  # 换模型后必跑
  python aiplan.py post --code 002463.SZ --no-fetch --dry-run   # 零成本验证事实包
  python aiplan.py post --code 002463.SZ --no-fetch --api-base https://127.0.0.1:1   # 降级演练（应 FAIL 且退出码 3、报告仍落盘）
  ```
- 新增数据块：在 `build_sections` 里加一节，并在 `CAPS` 给该路径设行数上限；
  新增裁剪档位：往 `PRUNE_STEPS` 追加 `(标签, 函数)`，函数只改 `CAPS` 或 `DROPPED`。
- 新增 provider：往 `providers` 加一段（含 `协议`/`base_url`/`单价`），再在 `profiles` 里组合，
  代码不用动；协议差异只需在 `_build_body` / `_parse_response` 里扩充分支。
