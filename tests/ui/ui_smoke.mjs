/*
 * 浏览器冒烟测试（可选，不进 Python 测试套件）：
 *   node tests/ui/ui_smoke.mjs            # 默认打 http://127.0.0.1:8765
 *   node tests/ui/ui_smoke.mjs 9000       # 指定端口
 *
 * 需要 Node 18+ 与 playwright（npm i playwright）。没装时直接 SKIP（退出码 0）。
 * 跑之前先启动控制台（python main.py webui）。截图输出到 build/ui-smoke-<view>.png。
 *
 * 检查两层：① 九个视图切得动且各自渲染出真实内容（不是空壳）；
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
await page.waitForSelector("#view-console.active", { timeout: 20000 });
console.log("PASS  桌面默认首页为总控台");
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

/* ---- 总控台荐股榜：筛选控件 + 排行榜（没有产物时给空态） ---- */
for (const sel of ["#console-pick", "#console-pick-mods", "#btn-pick-goto"]) {
  const found = await page.locator(sel).count();
  console.log(`${found > 0 ? "PASS" : "FAIL"}  荐股榜控件 ${sel}`);
  if (!found) failed++;
}
const facetMods = await page.$$eval("#console-pick-mods button", els => els.map(e => e.dataset.rkMod));
const fourMods = facetMods.join("/") === "短线/波段/中线/长线";
console.log(`${fourMods ? "PASS" : "FAIL"}  模块始终给全 4 个（${facetMods.join("/")}）`);
if (!fourMods) failed++;
const noExtraFilter = await page.locator("#console-pick-l1, #console-pick-theme, #btn-pick-reset").count();
console.log(`${noExtraFilter === 0 ? "PASS" : "FAIL"}  榜单不再有行业/概念筛选（多余控件 ${noExtraFilter} 个）`);
if (noExtraFilter !== 0) failed++;
await page.waitForFunction(() => {
  const box = document.querySelector("#console-pick");
  return box && (box.querySelector("table.tbl tbody tr") || /还没有荐股结果/.test(box.textContent));
}, null, { timeout: 20000 }).catch(() => {});
const rkRows = await page.locator("#console-pick table.tbl tbody tr:not(.rk-plan)").count();
const rkText = await page.locator("#console-pick").innerText();
const rkOk = rkRows > 0 || /还没有荐股结果/.test(rkText);
console.log(`${rkOk ? "PASS" : "FAIL"}  荐股榜渲染（${rkRows} 行）`);
if (!rkOk) failed++;
if (rkRows > 0) {
  const planLine = await page.locator("#console-pick tr.rk-plan").first()
    .innerText({ timeout: 5000 }).catch(() => "");
  const hasPlan = /买点/.test(planLine) && /止损/.test(planLine) && /止盈点/.test(planLine) && !/~/.test(planLine);
  console.log(`${hasPlan ? "PASS" : "FAIL"}  首推行带买点/止损/止盈点且无区间（${planLine.slice(0, 40)}…）`);
  if (!hasPlan) failed++;
  const codes = await page.$$eval("#console-pick table.tbl tbody tr:not(.rk-plan) .rk-code",
    els => els.map(e => e.dataset.code));
  const dedupOk = codes.length > 0 && new Set(codes).size === codes.length;
  console.log(`${dedupOk ? "PASS" : "FAIL"}  榜单按股票去重（${codes.length} 行 / ${new Set(codes).size} 只）`);
  if (!dedupOk) failed++;
  const capped = codes.length <= 30 && await page.locator("#console-pick .rk-more").count() === 0;
  console.log(`${capped ? "PASS" : "FAIL"}  总榜固定 30 只、没有「显示全部」（${codes.length} 行）`);
  if (!capped) failed++;
  /* 榜单「自选」按钮：加完上方自选股卡区要立刻出现这只。
     会短暂写 data/user/自选股.md（备注“荐股榜”），无论成败都在 finally 里删回。 */
  const pickTarget = await page.evaluate(() => {
    const rows = Array.from(document.querySelectorAll("#console-pick table.tbl tbody tr:not(.rk-plan)"));
    for (const tr of rows) {
      const chips = Array.from(tr.querySelectorAll(".chip")).map(c => c.textContent.trim());
      if (chips.indexOf("自选") >= 0) continue;
      const a = tr.querySelector(".rk-code"), btn = tr.querySelector("[data-rk-watch]");
      if (a && btn) return a.dataset.code;
    }
    return null;
  });
  if (pickTarget) {
    const beforeCards = await page.locator("#console-watch .stock-card").count();
    try {
      await page.click(`#console-pick [data-rk-watch="${pickTarget}"]`);
      await page.waitForSelector(`#console-watch .stock-card[data-code="${pickTarget}"]`, { timeout: 15000 });
      const afterCards = await page.locator("#console-watch .stock-card").count();
      const okWatch = afterCards === beforeCards + 1;
      console.log(`${okWatch ? "PASS" : "FAIL"}  榜单加自选后上方卡区立刻出现（${beforeCards} -> ${afterCards}）`);
      if (!okWatch) failed++;
    } catch (e) {
      console.log("FAIL  榜单加自选后上方卡区没更新：" + String(e).slice(0, 80));
      failed++;
    } finally {
      await page.evaluate(async (code) => {
        await fetch("/api/watchlist/remove", { method: "POST",
          headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code }) });
      }, pickTarget);
    }
  } else {
    console.log("INFO  榜单前 30 行都已在自选里，跳过「加自选刷新卡区」断言");
  }
  const onMods = await page.$$eval("#console-pick-mods button.on", els => els.map(e => e.dataset.rkMod));
  const disabledMods = await page.$$eval("#console-pick-mods button[disabled]", els => els.map(e => e.dataset.rkMod));
  if (disabledMods.length) {
    await page.click(`#console-pick-mods button[data-rk-mod="${onMods[0]}"]`);
    await page.waitForTimeout(200);
    const filtered = await page.locator("#console-pick-more").innerText();
    const okFilter = /榜单前 \d+ 只/.test(filtered || "");
    console.log(`${okFilter ? "PASS" : "FAIL"}  模块筛选生效（${filtered}）`);
    if (!okFilter) failed++;
    await page.click(`#console-pick-mods button[data-rk-mod="${onMods[0]}"]`);
    await page.waitForTimeout(150);
  } else {
    console.log("INFO  产物覆盖 4 个模块，跳过「模块置灰」断言");
  }
  await page.screenshot({ path: "build/ui-smoke-console-pick.png" });
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

/* ---- 荐股页模块多选：可同时点亮、至少留一个、记忆到 localStorage ---- */
await page.evaluate(() => localStorage.removeItem("aiplan.pick.modules"));
await page.reload({ waitUntil: "domcontentloaded" });
await page.waitForSelector("#view-console.active", { timeout: 20000 });
await gotoView("pick");
const modsOn = () => page.$$eval("#pick-modules button.on", els => els.map(e => e.dataset.module));
const defMods = await modsOn();
console.log(`${defMods.length === 1 && defMods[0] === "短线" ? "PASS" : "FAIL"}  模块默认勾选（${defMods.join("/")}）`);
if (!(defMods.length === 1 && defMods[0] === "短线")) failed++;
await page.click('#pick-modules button[data-module="波段"]');
const twoMods = await modsOn();
const stored = await page.evaluate(() => localStorage.getItem("aiplan.pick.modules") || "");
const multiOk = twoMods.length === 2 && twoMods.includes("短线") && twoMods.includes("波段") && /波段/.test(stored);
console.log(`${multiOk ? "PASS" : "FAIL"}  模块可多选且写入 localStorage（${twoMods.join("/")}）`);
if (!multiOk) failed++;
await page.click('#pick-modules button[data-module="短线"]');
await page.click('#pick-modules button[data-module="波段"]');   // 取消最后一个应被拦下
const leftMods = await modsOn();
console.log(`${leftMods.length === 1 ? "PASS" : "FAIL"}  至少保留一个模块（${leftMods.join("/")}）`);
if (leftMods.length !== 1) failed++;

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
