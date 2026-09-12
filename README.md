# 投资工具集：aiplan + pan + stock3d + Web 控制台

一套本地运行的 A 股「数据采集 → 事实包 → 大模型研判」工具，外加一个浏览器控制台。

| 组件 | 作用 | 说明文档 |
|---|---|---|
| `aiplan.py` | 单标的走势研判与交易计划（可插拔模型） | [docs/README_aiplan.md](docs/README_aiplan.md) |
| `pan.py` | 大盘快照：指数 / 情绪 / 资金 / 板块轮动 | [docs/README_pan.md](docs/README_pan.md) |
| `stock3d.py` | 个股历史 K 线与盘口数据采集 | [docs/README_stock3d.md](docs/README_stock3d.md) |
| `src/webui/` | 浏览器控制台（9 个页面，含总控台、荐股、自选、回收站） | [docs/README_webui.md](docs/README_webui.md) |
| `holdings sync` | 从已登录同花顺客户端只读同步资金、持仓、当日成交 | 本文「同花顺持仓同步」 |

> 所有结论由大模型生成，**不构成投资建议**。控制台只监听 `127.0.0.1`，无鉴权，不要暴露到公网。

## 一、环境要求

- **Python 3.9+**（开发机为 3.11.9，`python --version` 可查）。
- **零第三方依赖**：三个 CLI 与控制台只用 Python 标准库（`http.server`、`urllib`、`json`…），
  `pip install` 这一步可以完全跳过，见 [requirements.txt](requirements.txt)。
- 可选：Node.js 18+，只有 `tests/ui_*.mjs` 的浏览器人工用例需要（见「测试」）。
- 可选：同花顺持仓同步需要 Windows、已登录的 `xiadan.exe` 与独立 `.venv-holdings`；主程序不依赖它。
- Windows / macOS / Linux 均可，一键启动脚本 `.cmd` 仅 Windows 有；其它平台用 `python main.py`。

## 二、安装

```powershell
git clone <仓库地址> 投资
cd 投资

# 生成两份配置模板（只做一次），然后把「总资金」改成真实可用资金
python main.py aiplan init-account
python main.py aiplan init-models
```

同花顺持仓同步只安装到隔离环境（安装后主项目仍保持零第三方依赖）：

```powershell
py -3.11 -m venv .venv-holdings
.\.venv-holdings\Scripts\python.exe -m pip install -r requirements-holdings.txt
```

仓库里不带任何密钥与个人资金数据：`config/账户配置.json`、`config/模型配置.json`、
`data/user/持仓数据.md`、`data/user/自选股.md` 都已在 `.gitignore` 中忽略，
`config/*.example.json` 是可入库的脱敏模板。

## 三、启动

### Web 控制台（推荐）

```powershell
python main.py                        # 默认 http://127.0.0.1:8765，自动开浏览器
python main.py webui --port 9000      # 换端口
python main.py webui --no-browser     # 不自动开浏览器
cd src; python -m webui --port 9000   # 等价写法：直接跑包
```

Windows 也可以直接**双击根目录的 `启动WebUI.cmd`**（内含 Python 缺失提示与端口占用提示）。
控制台是 `src/webui/` 这个包（`webserver.py` 路由 + `static/js/` 原生 ES 模块），
没有构建步骤，改完刷新页面即生效；模块边界见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

### 命令行

```powershell
python main.py aiplan post --code 002463.SZ --no-fetch     # 等价 python aiplan.py ...
python main.py pan post
python main.py stock3d pull 002463.SZ
python main.py --help                                      # 全部子命令
```

`main.py` 只做转发：参数原样交给对应脚本，任何 CLI 用法都与直接跑脚本一致；
运行目录固定为项目根，所以 `data/` 与配置文件的相对路径不会错位。

### 同花顺持仓同步

先启动并登录同花顺《网上股票交易系统》，再执行 `python main.py holdings sync --preview`。
预览只读取资金、持仓、当日成交并校验，不修改文件；确认无误后执行 `python main.py holdings sync`。
成功后：`data/user/持仓数据.md` 会更新并留 `.bak`，账户“总资金”同步为总资产，当日成交追加到
`data/user/交易台账.md`（按委托去重），完整原始数据保存到 `data/holdings/ths/snapshots/YYYY-MM-DD/`。
脚本不登录、不保存密码、不调用交易接口；非交易日只取资金和持仓，直接跳过“当日成交”页以避免无意义的复制验证。同花顺未启动、出现验证码或校验失败时不会覆盖任何业务文件。

## 四、目录结构

```
投资/
├── main.py                统一入口（webui / aiplan / pan / stock3d / test / check）
├── 启动WebUI.cmd          Windows 一键启动
├── README.md              本文件（总入口文档）
├── requirements.txt       运行依赖清单（本项目为空：零第三方）
├── requirements-dev.txt   可选开发依赖（不装也能跑）
├── .editorconfig          缩进 / 编码 / 换行风格约定
├── .gitignore             忽略密钥、运行产物、缓存、编辑器配置
│
├── aiplan.py               ┐
├── pan.py                  ├ 三个 CLI：留在根目录当锚点（以「自身所在目录」定位 data/）
├── stock3d.py              ┘ 只读复用，功能一行没改；改动会被哈希基线拦住
│
├── src/webui/              Web 控制台（包，python -m webui）
│   ├── paths.py              路径真源 + 文件读写工具（含加载根目录 CLI）
│   ├── sources.py            原始快照读取（pan / stock3d / K 线）
│   ├── store.py              账户 / 模型 / 持仓 / 自选读写
│   ├── archive.py            报告 / plan_log / 荐股产物的读取与删除
│   ├── market.py             页面数据组装 + 大盘走势预测
│   ├── jobs.py               后台任务（子进程）编排
│   ├── pick.py pick_run.py   荐股：纯逻辑 / 取数与编排
│   ├── trash.py              回收站与 7 天过期真删
│   ├── webserver.py          路由分派 + 静态文件 + 本地服务
│   ├── run_aiplan.py         aiplan 的 UTF-8 输出包装器（不改 aiplan.py）
│   ├── holdings_ths.py       同花顺 xiadan.exe 只读适配（可选依赖延迟导入）
│   ├── holdings_sync.py      持仓同步校验、账户/台账/快照写盘与 CLI
│   └── static/               index.html + styles.css + js/{core,ui,views} 原生 ES 模块
├── config/                 个人配置（账户 / 模型）+ 脱敏模板
├── data/user/              个人数据（持仓 / 自选）
├── docs/                   说明文档、数据字段清单、架构说明
├── scripts/                自检 / 接口对拍 / 打包 / 启动脚本
├── tests/                  unit（纯逻辑）/ integration（真起服务）/ ui（浏览器）
└── data/                   运行产物：报告、缓存、历史、大盘快照（可整目录删除重建）
```

**为什么三个 CLI 不进 `src/`？** 它们用 `SCRIPT_DIR = os.path.dirname(__file__)`
定位 `data/`；挪窝等于换数据目录，还会让 `python aiplan.py …` 这类直跑示例全部失效。
所以它们留在根目录当锚点，只把 `aiplan.py` 的三个默认路径常量指向 `config/` 与
`data/user/`（`scripts/cli_baseline.json` 记录 sha256，改没改一眼可查）。

## 五、参数速查

| 场景 | 命令 |
|---|---|
| 跑一次盘后研判 | `python main.py aiplan post --code 002463.SZ` |
| 不调模型只看事实包 | `python main.py aiplan post --code 002463.SZ --no-fetch --dry-run` |
| 自检模型连通性 | `python main.py aiplan check-model` |
| 抓大盘快照 | `python main.py pan post` |
| 抓个股 K 线 | `python main.py stock3d pull 002463.SZ` |
| 同花顺同步预览 | `python main.py holdings sync --preview` |
| 同花顺正式同步 | `python main.py holdings sync` |
| 同花顺只同步持仓 | `python main.py holdings sync --no-trades` |

各 CLI 的完整参数、字段口径、输出落盘位置见 `docs/` 下对应文档；
控制台各页面的操作见 [docs/README_webui.md](docs/README_webui.md)。

## 六、配置与密钥

- 端口、资金、费率、模型与 Key 全部走独立配置文件或环境变量，**代码里没有硬编码的密钥或账号**。
- 密钥查找优先级：命令行 `--api-key` → 环境变量 → `模型配置.json` 的 `api_key` → `Desktop\token.txt`。
- `data/` 整目录被忽略；控制台写回配置文件前一律生成同名 `.bak` 备份，改坏了直接改名覆盖即可。
- 同花顺同步成功后会把账户“总资金”更新为券商总资产；同时写 `data/user/交易台账.md` 与 `data/holdings/ths/snapshots/`。
- 页面上的 Key 只显示掩码与来源，不会回显明文。

## 七、测试

```powershell
python main.py test        # 或 scripts\run_tests.cmd（等价 python -m unittest discover -s tests -t tests -v）
python main.py check       # 结构 / 语法 / 前端模块图 / 敏感信息 / CLI 哈希基线
```

`tests/unit/`：目录与路径约定、前端模块与 id 一致性、荐股纯函数（筛选参数、排除规则、打分单调性）、
写回校验与备份。`tests/integration/`：HTTP 接口回归（真实起服务逐个打接口）、入口脚本可用性。
全部用标准库 `unittest`，不需要 pytest。

可选：浏览器冒烟测试（需要 Node 18+ 与 playwright，服务已启动时执行）：
`node tests/ui/ui_smoke.mjs` —— 点开 8 个页面、断言各自渲染出真实内容、检查报告页三个页签与
K 线画布、荐股筛选联动，并截图到 `build/`，全程要求 0 个 console error。

## 八、打包与部署

```powershell
python scripts/build.py            # 生成 build/投资工具集-YYYYMMDD.zip（只含源码与文档，不含密钥与数据）
```

本工具定位是本机自用：没有数据库、没有外部服务，部署 = 复制目录 + 装 Python。
需要单文件 exe 时用 PyInstaller 自行打包（第三方工具，非本项目依赖）：
`pip install pyinstaller && pyinstaller --noconfirm --onefile --name 投资控制台 main.py`。

## 九、常见报错

| 现象 | 处理 |
|---|---|
| 页面提示「账户配置不存在」 | `python main.py aiplan init-account`，并填真实「总资金」 |
| 模型报 401 / 403 | Key 失效或未配置：检查 `模型配置.json` 或环境变量，页面「模型配置」可一键自检 |
| 端口被占用 | 启动信息会给出实际端口（8765 起自动顺延）；也可 `--port 8766` |
| 大盘快照空白 | 先跑 `python main.py pan post`，页面读 `data/pan/<日期>/latest_post.json` |
| 改了前端但页面没变 | 按 Ctrl+F5 强刷：模块化后入口变成 `/js/main.js`，旧缓存会 404 |
| 控制台中文乱码 | 用 `python main.py …`（已强制 UTF-8）；直接跑 CLI 时 aiplan 会把中文转成 `?`，属既有设计 |
| 想找回删掉的报告 | 「历史与复盘」页的回收站可恢复；回收站超过 7 天会被自动真删 |

## 十、约束

1. `aiplan.py` / `pan.py` / `stock3d.py` 只读复用：除 `aiplan.py` 的三个默认路径常量外不碰，
   `python main.py check` 用 sha256 基线校验（要改就先走查再用 `--update-baseline` 重登记）。
2. 控制台只写回三个固定文件（`data/user/持仓数据.md`、`config/账户配置.json`、
   `config/模型配置.json`），写前必留 `.bak`。
3. 核心程序不新增第三方依赖；同花顺同步依赖只装在 `.venv-holdings` 并通过 `requirements-holdings.txt` 管理。前端不使用框架与构建步骤。
4. 后端包内单文件 ≤800 行、前端模块 ≤900 行，由 `python main.py check` 守住，防止再长回大文件。







