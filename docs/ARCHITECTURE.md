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
   ├── pick_run    荐股取数与编排（东财抓取、缓存、模型点评）→ pick（纯逻辑）
   ├── plancheck   报告实盘复核：实盘价 × 交易计划（机械判定 + 当前时段策略点评）
   ├── quotes      行情取数：批量报价（东财，1 次请求）+ 当日分时（腾讯，60 秒缓存 + 串行限速）
   ├── overview    总控台数据：持仓 / 自选卡片、计划线、到价提醒（只读）
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
  └── views/*.js       九个页面，各自渲染 + 注册（console 总控台）
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
4. **总控台轮询**：前端按所选档位（10s ~ 10min）请求 `/api/overview?refresh=1`——每档 1 次东财批量报价，分时每标的最多 60 秒一次（分钟线每分钟才变）、同轮串行间隔 300ms；非交易时段不发请求，连续失败按 2 倍退避（上限 5 分钟）。
5. **报告页卡片顺序**：`renderStruct()` 用槽位（slots）收集各卡片，最后一行决定顺序——K 线与关键价位 / 交易计划 / 关键价位 置顶，其余按原优先级排后。
6. **到价消息队列**：每次「新触发」由 `overview` 追加一行到 `data/ai/alerts.jsonl`（保留最近 500 条），顶栏铃铛与控制台队列卡都读它；未读游标存 localStorage。
7. **写回**：`store.py` 只写三个固定文件，写前 `paths.save_like()` 比较内容、必要时留 `.bak`
  并按原文件的 BOM / 换行风格写回。
8. **删除**：一律移动进 `data/ai/.trash/<日期>/`（`archive.delete_*`），回收站
   `trash.py` 负责列出、恢复、以及超过 7 天的真删。

## 六、HTTP 接口

路由集中在 `src/webui/webserver.py`；GET 15 个、POST 12 个，字段名与旧版完全一致
（重构时用 `scripts/api_snapshot.py` 逐字段对拍过）。清单：

```
GET  /api/state /holdings /account /models /reports /report /history /symbols
     /market /market/forecast /plancheck /overview /alerts /kline /watchlist /trash /pick /pick/list
     /pick/boards /pick/industry /blob
POST /api/holdings /account /models /alerts/clear /trash/restore /trash/purge /pick/delete
     /report/delete /watchlist/add /watchlist/remove /watchlist
     /jobs /jobs/<id>/cancel
```

## 七、验证手段

| 目的 | 命令 |
|---|---|
| 静态自检（结构 / 语法 / 模块图 / 密钥 / CLI 基线） | `python main.py check` |
| 单元 + 接口回归（标准库 unittest） | `python main.py test` |
| 重构前后接口对拍 | `python scripts/api_snapshot.py --out tmp/a.json` / `--compare tmp/a.json tmp/b.json` |
| 桌面浏览器冒烟（九页渲染 + 重交互 + 0 报错） | `node tests/ui/ui_smoke.mjs` |
| 手机浏览器冒烟（三视口 + 九页 + 无横向溢出 + 0 报错） | `node tests/ui/mobile_smoke.mjs` |
| 打包源码 | `python scripts/build.py` |
