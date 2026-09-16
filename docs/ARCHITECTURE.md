# 架构与模块边界

> 面向「改代码的人」：想加一个接口、加一个页面、加一个数据源时，先看这里。

## 一、全局约束

1. **核心零第三方依赖**：后端只用 Python 标准库，前端只用原生 ES 模块（无 npm、无构建步骤）；同花顺同步是隔离在 `.venv-holdings` 的可选扩展。
2. **三个 CLI 只读复用**：`aiplan.py` / `pan.py` / `stock3d.py` 留在项目根目录，网页端通过
   `importlib` 按文件路径加载它们（`webui/paths.py` 是唯一入口），不修改其逻辑。
   `python main.py check` 用 `scripts/cli_baseline.json` 的 sha256 守住这条线：
   只有走查过才允许 `--update-baseline` 重新登记。
3. **默认本地自用**：默认只监听 `127.0.0.1`；显式启用非回环监听时强制密码登录（口令按 `--password` → `AIPLAN_WEBUI_PASSWORD` → `config/webui配置.json` → 随机生成的顺序解析，源码内不存默认口令），局域网防火墙规则只应限定本地子网，所有写回仍留 `.bak`。

## 二、目录职责

```
投资/
├── aiplan.py pan.py stock3d.py   根锚点：CLI（以自身目录定位 data/ 与 config/）
├── main.py                       统一入口：webui | aiplan | pan | stock3d | test | check
├── src/webui/                    控制台后端（包，python -m webui）
├── src/webui/static/             前端（index.html + 桌面/手机样式 + js/ 模块树）
├── config/                       账户配置.json / 模型配置.json（+ 脱敏模板）
├── data/user/                    持仓数据.md / 自选股.md / 交易台账.md（个人数据）
├── data/                         运行产物：ai 报告、ai/pick 荐股、pan 快照、history、.trash
├── docs/                         说明文档（本文件 + 四份 CLI/控制台手册）
├── scripts/                      check.py（自检）/ api_snapshot.py（对拍）/ build.py（打包）
└── tests/                        unit（纯逻辑）/ integration（真起服务）/ ui（浏览器）
```

## 三、后端模块图

依赖方向自上而下，**没有环**（`python main.py check` 会验证前端那部分，后端靠分层约定）：

```
webserver  ← __main__（python -m webui）
   │  路由分派、静态文件、/m 手机入口、密码登录、本地服务
   ├── jobs        子进程任务：启动 / 增量日志 / 中断 / 诊断 / 批量
   ├── market      页面数据组装：快照、代码候选、K 线、大盘走势预测
   │     └── store ──┐
   ├── pick_run    荐股编排（全大盘）：扫描 → 机械打分 → 前 150 只交模型 → 落盘
   │     ├── pick        荐股纯逻辑：常量 / 五档打法 / 排除规则 / 参数收敛 / 提示词 / 事实包 / Markdown
   │     ├── mech        机械打分（严格照根目录《机器打分逻辑.txt》）：6 模块阈值 + 一票否决 + 缺失折算
   │     ├── mechdata    机械打分的取数层：全市场扫描 / 逐股日K（东财→腾讯兜底）/ 财报 / 资金流 /
   │     │               行业聚合 / ETF / 质押 / 北向 / 股东 / 龙虎榜 / 公告 / 板块热度（按自然日缓存）
   │     ├── mechtech    技术指标纯函数：SMA / MACD / RSI / 量能比 / 量价比 / 区间位置 / 超额收益
   │     └── mechstock   单只标的打分：1 次 ulist 取行 + 复用 mechdata 取数 → mech.score_stock
   │                     （荐股与交易流共用同一套原始值纯函数，口径不分叉）
   ├── plancheck   报告实盘复核：实盘价 × 交易计划（机械判定 + 当前时段策略点评）
   ├── track       标的跟踪纯逻辑：适用交易日 / 执行记录 / 事实包 / Markdown / 产物读写
   │     ├── track_run   跟踪执行层：取数 → 一次研判档调用（可选复核档）→ 落盘
   │     └── trackview   跟踪页数据组装：清单卡片 + 详情（最新计划 / 执行 / 历史）
   ├── flow        交易流容器：流参数（流资金 / 目标 / 最大亏损）、标的清单与资金上限、打法、
   │               开流 / 加标的 / 改参数 / 结束 / 删除、聚合与 v1→v2 升级
   │     ├── flowbook    一只标的的账本：成交 / 平均成本与盈亏 / 接近带与提醒去重 / 台账合并 /
   │     │               计划挂载与盘后重算判定 / 体检记录（纯函数，可单测）
   │     ├── flowplan    交易流硬约束（纯函数）：机械分档位 → 仓位上限 / 亏损预算 / 打法止损幅度上限，
   │     │               以及模型越界时的自动校正（股数 / 止损 / 区间 / 作废）与逐条标注
   │     ├── flow_run    交易流执行层：机械打分定约束 + 模型出价位 + 校正落盘（复用 track 事实包与模型层）
   │     │               + 手动体检（消息面 + 四档结论）
   │     ├── flowview    交易流页面数据：流卡片 / 标的行 / 详情 / 机械检查（报价 → 盈亏 → 提醒 → 消息队列）
   │     │               / 行内图（分时 minute 或日K day，计划买卖线同一份口径）
   │     └── flowapi     交易流的 HTTP 入口（webserver 只做分派，避免它继续膨胀）
   ├── pickrank    荐股榜 → 页面行（模型评分降序）+ **四档打法推荐**（超短/短/中/长各一只，大盘快照页用）
   ├── quotes      行情取数：批量报价（东财，1 次请求）+ 当日分时（腾讯，60 秒缓存 + 串行限速）
   ├── freshness   数据新鲜度：早于最近交易日的 pan / stock3d 快照**直接删除**（不进回收站），
   │               保证事实包不会引用过期数据（测试用 AIPLAN_NO_PURGE=1 关闭）
   ├── overview    总控台数据：持仓 / 自选卡片、计划线、到价提醒（只读）
   │     └── pickrank  荐股榜：最新产物 → 行表（模型评分降序 + 打法筛选；兼容旧产物）
   ├── planlines   计划线口径：关键价位 + 计划 → 买点 / 减仓 / 止损 / 目标（报告页 / 总控台 / 跟踪页共用）
   ├── alerts      到价消息队列：data/ai/alerts.jsonl（保留最近 500 条）
   │     └── background  把 stock3d / pan 快照裁成「个股量价 + 大盘 + 板块」背景数据
   ├── archive     报告 / plan_log / 荐股产物的读取、列举、删除
   ├── store       账户 / 模型 / 持仓 / 自选的读取与写回 ──┐
   ├── holdings_sync 同花顺同步编排：校验 / 账户 / 台账 / 快照 ─┤
   │     └── holdings_ths  可选依赖的 xiadan.exe 只读适配       │
   ├── sources     data/ 下原始快照的枚举与读取 ───────────┤
   └── trash       回收站：列表 / 7 天过期真删 / 恢复        │
        paths     路径真源 + 文件读写工具（含加载 aiplan.py / pan.py）←──┘
```

改代码时的落点：

| 要加的东西 | 放哪 |
|---|---|
| 新接口 | `webserver.py` 的 `Handler.do_GET` / `_api_post`，数据组装放 `market.py` 或对应模块 |
| 新数据源读取 | `sources.py`（纯读盘）；涉及页面组装再放 `market.py` |
| 新配置 / 新用户文件 | `paths.py` 加常量 + 同步 `.gitignore` 与 `store.py` |
| 新后台任务 | `jobs.py`（命令行式）或 `pick_run.py`（内置 callable 式） |
| 同花顺客户端适配 | `holdings_ths.py`；第三方依赖只在 `.venv-holdings` 中导入 |
| 人工验证码流转 | 抓取子进程写本地图片/会话文件，WebUI `GET/POST /api/holdings/captcha` 读取与回填 |
| 新的荐股评分维度 | `pick.py`（纯函数，必须可单测） |
| 标的跟踪的新规则（适用交易日 / 执行记录 / 事实包裁剪） | `track.py`（纯函数，必须可单测） |
| 标的跟踪的取数与编排 | `track_run.py`；页面数据在 `trackview.py` |
| 交易流的容器规则（流资金上限 / 一只标的只属于一条流 / 打法 / 聚合） | `flow.py`（纯函数，必须可单测） |
| 一只标的的账本规则（盈亏 / 接近带 / 台账 / 达标止损 / 计划挂载） | `flowbook.py`（纯函数，必须可单测） |
| 交易流的计划与体检 | `flow_run.py`（计划按打法出，复用 track 的取数与模型层；体检读 stock3d 消息面） |
| 交易流的新接口 | `flowapi.py` + `webserver.py` 的一行分派 |
| 新的计划线口径 | `planlines.py`（报告页 / 总控台 / 跟踪页共用，改一处三处生效） |

## 四、前端模块图

```
main.js                入口：initNav + 各视图 init + 首屏刷新
  ├── core/util.js     DOM 选择、转义、格式化、toast、chip/badge
  ├── core/api.js      唯一 fetch 封装
  ├── core/app.js      State / VIEW_TITLE / showView / refreshState / openReport + 视图注册表
  ├── core/mobile.js   /m 路径识别、底部导航、更多面板、移动端写入确认
  ├── ui/markdown.js   轻量 Markdown 渲染
  ├── ui/jsontree.js   原始 JSON 树
  ├── ui/modal.js      模态框与确认框
  ├── ui/cards.js      报告 / 大盘共用的卡片原语
   ├── ui/kline.js      K 线绘制与缩放
   ├── ui/pickcards.js  荐股推荐榜 / 一票否决 / 机械口径 / 候选池（只拼 HTML）
   ├── ui/trackcards.js 跟踪清单 / 计划摘要 / 执行录入表 / 历史时间线（只拼 HTML）
   ├── ui/flowcards.js  交易流卡片 / 开流表单 / 详情（成交 · 体检 · 事件 · 计划）
   ├── ui/flowchart.js  交易流当日分时图（分时 + 均价 + 昨收 + 计划线 + 买卖点标记）
   └── views/*.js       九个页面，各自渲染 + 注册（console 总控台、flow 交易流；历史页已下线）
core/poller.js 自动刷新定时器（档位 / 交易时段 / 退避 / localStorage）；ui/stockcard.js 股票卡片与分时小图；
mobile.css 仅作用于 body[data-shell="mobile"]，桌面版和 /m 共用同一份视图 DOM。
```

**跨页动作一律走注册表**，视图之间禁止互相 import：

```js
// views/report.js 末尾：声明「切到本页要干什么」与对外动作
registerView("report", {
  onShow: () => { if (!State.report) loadLatestReport(); },
  render: renderReport,
  refreshReports: refreshReports,
});

// 别的模块要跳报告：不 import，而是查表
import { viewApi } from "../core/app.js";
viewApi("report").refreshReports();
```

好处：文件之间没有环、页面可以单独替换；代价是调用目标必须已经注册（各视图在模块求值时注册，
`main.js` 静态 import 全部视图，所以调用点一定晚于注册）。

## 五、数据流

1. **研判**：`main.py webui` → 运行页 → `POST /api/jobs` → `jobs.JobManager` 起子进程
   `python src/webui/run_aiplan.py <phase> --code …` → aiplan 抓数（可调 pan/stock3d）→
   报告与事实包落 `data/ai/<YYYYMMDD>/` → 前端轮询 `/api/jobs/<id>` 拿增量日志 →
   完成后 `jobs` 定位最新产物 → 前端跳报告页 → `market/archive` 读取并渲染。
2. **荐股**：`pick_run.run_pick` 走东财 HTTP 取板块与成分（磁盘缓存 `data/cache/pick_*.json`，
   6 小时 TTL）→ `pick.py` 机械打分与筛选 → 一次模型点评 → 落 `data/ai/pick/<日期>/`。
3. **实盘复核**：`plancheck.build_plan_check` 读报告 + 取一次东财实时快照（失败按
   stock3d 缓存 → 报告内现价降级并标注来源），机械判定每条计划是否触发；点「生成当前时段点评」
   再走一次模型（研判档），产物落 `data/ai/plancheck/<日期>/`，不写回报告与配置。
4. **总控台轮询**：前端按所选档位（10s ~ 10min）请求 `/api/overview?refresh=1`——每档 1 次东财批量报价，分时每标的最多 60 秒一次（分钟线每分钟才变）、同轮串行间隔 300ms；非交易时段不发请求，连续失败按 2 倍退避（上限 5 分钟）。同一份响应里 `pickrank` 把**最新一份**荐股产物组装成榜单：推荐度 = **模型评分**（模型没点评的行按机械分排序并标注），打法筛选始终给全 5 档；旧版产物（按模块组织的 `候选`）仍能读，推荐度回退机械分；榜单代码并进**同一次**批量报价（只报价、不取分时，失败按产物快照价并标注）。
5. **报告页卡片顺序**：`renderStruct()` 用槽位（slots）收集各卡片，最后一行决定顺序——K 线与关键价位 / 交易计划 / 关键价位 置顶，其余按原优先级排后。
6. **到价消息队列**：每次「新触发」由 `overview` 追加一行到 `data/ai/alerts.jsonl`（保留最近 500 条），顶栏铃铛与控制台队列卡都读它；未读游标存 localStorage。
7. **写回**：`store.py` 只写三个固定文件，写前 `paths.save_like()` 比较内容、必要时留 `.bak`
  并按原文件的 BOM / 换行风格写回。
8. **删除**：一律移动进 `data/ai/.trash/<日期>/`（`archive.delete_*`），回收站
   `trash.py` 负责列出、恢复、以及超过 7 天的真删。
8.5 **一键抓大盘快照**：`POST /api/jobs {kind:"pan"}` → `jobs.build_pan_argv()` 起 `pan.py post`（只抓行情、不调模型）→ 落 `data/pan/<今天>/`，页面空态卡上直接可点。

9. **标的跟踪（每日计划 → 执行 → 次日计划）**：`POST /api/jobs {kind:"track"}` →
   `track_run.run_track` 逐只标的：`quotes.fetch_quotes` 一次批量报价（1 次请求）+
   `background.latest_pan/stock3d_tech` 背景 → `track.factpack_sections` 拼事实包（超上限先砍板块 →
  大盘 → 个股形态）→ 一次研判档调用（可选复核档）→ `track.save_plan` 落
  `data/ai/track/<日期>/`。执行情况由人工录入到同名 `*_track_exec.json`；上一份计划的适用交易日
  早于本次时没填执行记录就跳过该标的（日志 `[WARN]`）。跟踪产物与报告同结构，报告页 / 实盘复核
  可直接打开，总控台计划线取「报告 ∪ 跟踪」里每只标的最新一份。
10. **交易流（一笔资金 + 多只标的，从建仓盯到清仓）**：`data/ai/flows/F-<YYYYMMDD>-<NN>.json` 存
   一条流（流参数 / 标的清单 / 流级事件），每只标的带 打法（超短线 / 短线 / 波段）、分配资金、
   期初、成交、持仓、盈亏、计划引用、体检、事件与提醒状态；**资金是流的属性**，流内所有标的的分配
   资金之和 ≤ 流资金，且**一只标的只属于一条流**（`flow._check_capital` / `flow_id_of_code` 拦），
   老结构 `<代码>-<起始日>.json` 读入时由 `flow.normalize` 升成 v2。**盯盘是页面轮询**：
   `GET /api/flows?refresh=1` 用一次批量报价覆盖所有在跑的标的，`flowview.check_pass` 机械判定
   盈亏、接近带与触及、达标/止损，把新提醒写进 `data/ai/alerts.jsonl`（同类型同价位当天只一次）；
   `overview` 把流卡区（一行 = 一只标的）并进总控台，所以人在别的页面（开着自动刷新）也在盯。
   `jobs.start_pan_job` 把「抓大盘快照」和「顺带荐股」串成同一个任务（pan 子进程 → `pick_run.run_pick`），
大盘快照页读最新荐股产物给四档打法各推一只。
模型只在两处出手：`flow_run.run_plan`（开流首份 / 盘后过点一键 / 手动，按这只标的的打法出计划；
   事实包最前面是「交易流状态」，成交与盈亏是权威口径）与 `flow_run.run_check`
   （手动体检：stock3d 消息面 + 量价 + 板块大盘 + 流状态 → 四档结论；数据太旧可先跑
   `stock3d.py pull|news` 子进程）。成交来自 `data/user/交易台账.md`（同花顺同步写入，按委托去重）
   与手工补录，二者合并去重；补录/删除成交只重算盈亏，**不改计划**。

## 六、HTTP 接口

路由集中在 `src/webui/webserver.py`；字段名与旧版完全一致
（重构时用 `scripts/api_snapshot.py` 逐字段对拍过）。清单：

```
GET  /api/state /holdings /account /models /reports /report /history /symbols
     /market /market/forecast /plancheck /overview /alerts /watchlist /trash /pick /pick/list
     /kline（先读 data/history 缓存，没有就自动拉取：东财 push2his → 腾讯前复权兜底）
     /pick/boards /pick/industry /tracklist /track/all /track /blob
     /models/default /models/profile /models/provider
     /ledger（交易台账只读视图：持仓页「交易明细」，含覆盖日期区间；台账由同步写入近一周成交）
     /flows /flow（?id=&code=）/flow/minutes（?id=&code=）
POST /api/holdings /account /models /alerts/clear /trash/restore /trash/purge /pick/delete
     /models/default /models/profile /models/provider
     /report/delete /watchlist/add /watchlist/remove /watchlist
     /tracklist /tracklist/add /tracklist/remove /tracklist/import-watchlist
     /track/exec /track/delete
     /flows /flow/target/add /flow/target/remove /flow/target/set /flow/params
     /flow/fill /flow/fill/delete /flow/sync /flow/close /flow/delete
     /flow/settings /flow/plan /flow/check
     /jobs /jobs/<id>/cancel
```

## 七、验证手段

| 目的 | 命令 |
|---|---|
| 静态自检（结构 / 语法 / 模块图 / 密钥 / CLI 基线 / 机械打分口径文件 sha256） | `python main.py check` |
| 过期快照与回收站清理（用户数据维护动作，测试里用 AIPLAN_NO_PURGE=1 关掉） | `python -c "import sys;sys.path.insert(0,'src');from webui import freshness;print(freshness.purge_stale(force=True))"` |
| 单元 + 接口回归（标准库 unittest） | `python main.py test` |
| 重构前后接口对拍 | `python scripts/api_snapshot.py --out tmp/a.json` / `--compare tmp/a.json tmp/b.json` |
| 桌面浏览器冒烟（九页渲染 + 重交互 + 0 报错） | `node tests/ui/ui_smoke.mjs` |
| 手机浏览器冒烟（三视口 + 九页 + 无横向溢出 + 0 报错） | `node tests/ui/mobile_smoke.mjs` |
| 打包源码 | `python scripts/build.py` |
