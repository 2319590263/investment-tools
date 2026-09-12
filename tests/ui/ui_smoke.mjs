/*
 * 浏览器冒烟测试（可选，不进 Python 测试套件）：
 *   node tests/ui/ui_smoke.mjs            # 默认打 http://127.0.0.1:8765
 *   node tests/ui/ui_smoke.mjs 9000       # 指定端口
 *
 * 需要 Node 18+ 与 playwright（npm i playwright）。没装时直接 SKIP（退出码 0）。
 * 跑之前先启动控制台（python main.py webui）。截图输出到 build/ui-smoke-<view>.png。
 *
 * 检查两层：① 八个视图切得动且各自渲染出真实内容（不是空壳）；
 *          ② 报告页三个页签 + K 线画布这类重交互不回归；全程 0 个 console error。
 */

import { mkdirSync } from "node:fs";

const PORT = process.argv[2] || process.env.AIPLAN_WEBUI_PORT || "8765";
const BASE = `http://127.0.0.1:${PORT}`;

const VIEWS = ["console", "run", "report", "history", "holdings", "watch", "pick", "models", "market"];
// 每个视图必须渲染出的真实内容（空壳页面不算通过）
const CONTENT = {
  console: "#console-body .kv",
  run: "#console",
  report: "#report-struct .card",
  history: "#hist-table tbody tr",
  holdings: "#hold-table tbody tr",
  watch: "#watch-table tbody tr",
  pick: "#pick-panel-industry .pick-row",
  models: "#provider-table tbody tr",
  market: "#market-body .card",
};

let chromium;
try {
  ({ chromium } = await import("playwright"));
} catch {
  console.log("SKIP  未安装 playwright：npm i playwright 之后可再跑这个用例");
  process.exit(0);
}

try {
  const probe = await fetch(BASE + "/api/state");
  if (!probe.ok) throw new Error("HTTP " + probe.status);
} catch (e) {
  console.log(`SKIP  服务没在 ${BASE} 上跑（先执行 python main.py webui）：${e.message}`);
  process.exit(0);
}

let browser;
for (const opts of [{ channel: "msedge" }, {}]) {
  try {
    browser = await chromium.launch(opts);
    break;
  } catch (e) {
    if (opts.channel) continue;
    throw e;
  }
}

const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const problems = [];
let failed = 0;
page.on("console", (m) => { if (m.type() === "error") problems.push("console: " + m.text()); });
page.on("pageerror", (e) => problems.push("pageerror: " + e.message));

const gotoView = async (view) => {
  await page.click(`nav#nav button[data-view="${view}"]`);
  await page.waitForFunction(
    (v) => document.querySelector(`#view-${v}`)?.classList.contains("active"), view, { timeout: 15000 });
};

await page.goto(BASE, { waitUntil: "domcontentloaded" });
await page.waitForSelector("#view-run", { timeout: 20000 });
mkdirSync("build", { recursive: true });

for (const view of VIEWS) {
  await gotoView(view);
  const sel = CONTENT[view];
  try {
    await page.waitForSelector(sel, { timeout: 30000 });
    console.log(`PASS  view-${view} 内容已渲染（${sel}）`);
  } catch {
    console.log(`FAIL  view-${view} 没有渲染出内容（等不到 ${sel}）`);
    failed++;
  }
  await page.screenshot({ path: `build/ui-smoke-${view}.png` });
}

/* ---- 报告页深度检查：三个页签 + K 线画布 ---- */
/* ---- 总控台深度检查：卡片区 + 自动刷新控件（不开启开关，避免测试打网络） ---- */
await gotoView("console");
for (const sel of ["#console-body .kv", "#console-holdings", "#console-watch",
                   "#console-auto", "#console-interval", "#btn-console-quotes"]) {
  const found = await page.locator(sel).count();
  console.log(`${found > 0 ? "PASS" : "FAIL"}  总控台控件 ${sel}`);
  if (!found) failed++;
}
const cards = await page.locator("#console-holdings .stock-card").count();
const emptyHold = await page.locator("#console-holdings").innerText();
const holdOk = cards > 0 || /还没有数据/.test(emptyHold);
console.log(`${holdOk ? "PASS" : "FAIL"}  持仓卡片区渲染（卡片 ${cards} 张）`);
if (!holdOk) failed++;
const intervalOptions = await page.locator("#console-interval option").count();
console.log(`${intervalOptions === 5 ? "PASS" : "FAIL"}  自动刷新档位（${intervalOptions} 档）`);
if (intervalOptions !== 5) failed++;
const bellFound = await page.locator("#btn-alerts").count();
console.log(`${bellFound > 0 ? "PASS" : "FAIL"}  顶栏消息队列铃铛存在`);
if (!bellFound) failed++;
const planText = await page.locator("#console-holdings .stock-card .sc-plan").first()
  .innerText({ timeout: 5000 }).catch(() => "");
if (planText) {
  const noRange = !/~/.test(planText);
  const hasLots = /手/.test(planText);
  const oneGoal = (planText.match(/止盈点/g) || []).length === 1 && !/(^|\s)目标/.test(planText);
  console.log(`${noRange && hasLots && oneGoal ? "PASS" : "FAIL"}  卡片计划行：无区间 / 带手数 / 单一止盈点`);
  if (!(noRange && hasLots && oneGoal)) failed++;
} else {
  console.log("INFO  持仓卡片没有计划行，跳过计划行断言");
}

await gotoView("report");
await page.waitForSelector("#report-struct .card", { timeout: 30000 });
/* 卡片顺序：K线 / 交易计划 / 关键价位 置顶（用户要求的排列） */
const cardTitles = await page.evaluate(() => Array.from(
  document.querySelectorAll("#report-struct > .card"),
  el => {
    const h = el.querySelector(".card-h span");
    return h ? h.textContent.trim() : "";
  }));
const top3 = cardTitles.slice(0, 3).join(" / ");
const orderOk = /K 线/.test(cardTitles[0] || "") && /交易计划/.test(cardTitles[1] || "") &&
  /关键价位/.test(cardTitles[2] || "");
console.log(`${orderOk ? "PASS" : "FAIL"}  报告页前 3 张卡顺序（${top3}）`);
if (!orderOk) failed++;
/* 实盘复核卡片：时段 + 实盘价（或明确的取不到提示）必须渲染出来 */
try {
  await page.waitForSelector("#plan-card #plan-body .gauge-row", { timeout: 30000 });
  const planText = await page.innerText("#plan-card");
  const okPlan = /当前时段/.test(planText) && /(实盘价|取不到)/.test(planText) &&
    /(计划|没有可解析的计划条目)/.test(planText);
  console.log(`${okPlan ? "PASS" : "FAIL"}  实盘复核卡片（时段 / 实盘价 / 计划判定）`);
  if (!okPlan) failed++;
  await page.screenshot({ path: "build/ui-smoke-plancheck.png" });
} catch (e) {
  console.log("FAIL  实盘复核卡片没有渲染：" + String(e).slice(0, 90));
  failed++;
}
for (const [tab, pane] of [["md", "#report-md"], ["json", "#report-json"], ["struct", "#report-struct"]]) {
  await page.click(`#report-tabs button[data-tab="${tab}"]`);
  const shown = await page.isVisible(pane);
  console.log(`${shown ? "PASS" : "FAIL"}  报告页签 ${tab}`);
  if (!shown) failed++;
}
const kline = await page.evaluate(() => {
  const card = document.querySelector("#kline-card");
  if (!card) return { card: false };
  const c = document.querySelector("#kline-canvas");
  return { card: true, canvas: c ? { w: c.width, h: c.height, shown: c.style.display !== "none" } : null };
});
if (!kline.card) {
  console.log("FAIL  报告页缺少 K 线卡片");
  failed++;
} else if (kline.canvas && kline.canvas.shown && kline.canvas.w > 0 && kline.canvas.h > 0) {
  console.log(`PASS  K 线已绘制（${kline.canvas.w}x${kline.canvas.h} 像素）`);
} else {
  console.log("INFO  K 线卡片存在，但没有可用的日K缓存（页面会显示提示文案，属正常）");
}

/* ---- 荐股页深度检查：勾选一个行业会更新预估文案 ---- */
await gotoView("pick");
const box = page.locator("#pick-panel-industry input[data-ind]").first();
if (await box.count()) {
  const before = await page.textContent("#pick-sel-count");
  await box.check();
  const after = await page.textContent("#pick-sel-count");
  const ok = before !== after && /[1-9]/.test(after || "");
  console.log(`${ok ? "PASS" : "FAIL"}  荐股筛选联动（${before} -> ${after}）`);
  if (!ok) failed++;
} else {
  console.log("FAIL  荐股页没有渲染出可勾选的行业");
  failed++;
}

await browser.close();

if (problems.length) {
  failed++;
  console.log("FAIL  页面报错：" + problems.join(" | "));
} else {
  console.log("PASS  无 console error / pageerror");
}
console.log(failed ? `结果：FAIL（${failed}）` : "结果：PASS");
process.exit(failed ? 1 : 0);
