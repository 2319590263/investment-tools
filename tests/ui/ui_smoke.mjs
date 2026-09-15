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

const VIEWS = ["console", "flow", "run", "report", "holdings", "watch", "pick",
               "models", "market"];
// 每个视图必须渲染出的真实内容（空壳页面不算通过）
const CONTENT = {
  console: "#console-body .kv",
  flow: "#flow-list .flow-card, #flow-list .empty",
  run: "#console",
  report: "#report-struct .card",
  holdings: "#hold-table tbody tr",
  watch: "#watch-table tbody tr",
  pick: "#pick-result .card, #pick-scan-hint",
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
const facetStyles = await page.$$eval("#console-pick-mods button", els => els.map(e => e.dataset.rkStyle));
const fiveStyles = facetStyles.slice(0, 5).join("/") === "超短线/短线/波段/中线/长线";
console.log(`${fiveStyles ? "PASS" : "FAIL"}  打法筛选始终给全 5 档（${facetStyles.join("/")}）`);
if (!fiveStyles) failed++;
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
  const rkHead = await page.locator("#console-pick table.tbl thead").innerText().catch(() => "");
  const colOk = /模型评分/.test(rkHead) && /机械分/.test(rkHead) && /打法/.test(rkHead);
  console.log(`${colOk ? "PASS" : "FAIL"}  榜单列头含 模型评分 / 机械分 / 打法（${rkHead.replace(/\s+/g, " ").slice(0, 60)}）`);
  if (!colOk) failed++;
  const styleChips = await page.locator("#console-pick table.tbl tbody tr .chip").allInnerTexts().catch(() => []);
  const styleOk = styleChips.some(x => ["超短线", "短线", "波段", "中线", "长线", "未定"].includes(x.trim()));
  console.log(`${styleOk ? "PASS" : "FAIL"}  榜单行带打法标签`);
  if (!styleOk) failed++;
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
  /* 只在「真实自选清单里确实没有」的候选上做增删——只信页面标记会误动用户已有的自选股 */
  const watchNow = await page.evaluate(async () => {
    try { return ((await (await fetch("/api/watchlist")).json())["条目"] || []); } catch (e) { return []; }
  });
  const knownWatch = new Set(watchNow.map(x => String(x["代码"] || "").slice(0, 6)));
  const candidates = await page.evaluate(() => Array.from(
    document.querySelectorAll("#console-pick table.tbl tbody tr:not(.rk-plan)"))
    .filter(tr => Array.from(tr.querySelectorAll(".chip")).every(c => c.textContent.trim() !== "自选"))
    .map(tr => { const a = tr.querySelector(".rk-code"); return a ? a.dataset.code : null; })
    .filter(Boolean));
  const pickTarget = candidates.filter(c => !knownWatch.has(c))[0] || null;
  if (pickTarget) {
    const beforeCards = await page.locator("#console-watch .stock-card").count();
    try {
      await page.click(`#console-pick [data-rk-watch="${pickTarget}"]`);
      await page.waitForSelector(`#console-watch .stock-card[data-code="${pickTarget}"]`, { timeout: 15000 });
      const afterCards = await page.locator("#console-watch .stock-card").count();
      const okWatch = afterCards === beforeCards + 1;
      console.log(`${okWatch ? "PASS" : "FAIL"}  榜单加自选后上方卡区立刻出现（${beforeCards} -> ${afterCards}）`);
      if (!okWatch) failed++;
      /* 卡片上的「删除」按钮：点 → 确认 → 卡片立刻消失 */
      const rmBtn = page.locator(`#console-watch .stock-card[data-code="${pickTarget}"] [data-act="remove"]`);
      const hasRm = await rmBtn.count();
      if (!hasRm) {
        console.log("FAIL  自选卡片上没有删除按钮");
        failed++;
      } else {
        await rmBtn.click();
        await page.waitForSelector("#cf-ok", { timeout: 8000 });
        await page.click("#cf-ok");
        await page.waitForFunction(
          (code) => !document.querySelector(`#console-watch .stock-card[data-code="${code}"]`),
          pickTarget, { timeout: 15000 });
        const backCards = await page.locator("#console-watch .stock-card").count();
        const okRm = backCards === beforeCards;
        console.log(`${okRm ? "PASS" : "FAIL"}  自选卡片删除按钮生效（${afterCards} -> ${backCards}）`);
        if (!okRm) failed++;
      }
    } catch (e) {
      console.log("FAIL  榜单加自选后上方卡区没更新：" + String(e).slice(0, 80));
      failed++;
    } finally {
      await page.evaluate(async (code) => {
        try {                                          // 兜底：万一上面中断，别在自选里留残留
          const doc = await (await fetch("/api/watchlist")).json();
          const still = (doc["条目"] || []).some(x => String(x["代码"] || "").slice(0, 6) === code);
          if (!still) return;                          // 卡片按钮已经删掉了就别再删（会 400）
          await fetch("/api/watchlist/remove", { method: "POST",
            headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code }) });
        } catch (e) { /* 忽略：文件本身没被改坏 */ }
      }, pickTarget);
    }
  } else {
    console.log("INFO  榜单前 30 行都已在自选里，跳过「加自选刷新卡区」断言");
  }
  const watchCards = await page.locator("#console-watch .stock-card").count();
  const watchRm = await page.locator('#console-watch .stock-card [data-act="remove"]').count();
  const rmAll = watchCards > 0 && watchRm === watchCards;
  console.log(`${rmAll ? "PASS" : "FAIL"}  每张自选卡片都有删除按钮（${watchRm}/${watchCards}）`);
  if (!rmAll) failed++;
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
const hasPlan = cardTitles.some(t => /交易计划/.test(t));
if (hasPlan) {
  console.log(`${orderOk ? "PASS" : "FAIL"}  报告页前 3 张卡顺序（${top3}）`);
  if (!orderOk) failed++;
} else {
  console.log(`INFO  最新报告没有模型计划（可能上次模型调用失败），跳过卡片顺序断言（${top3}）`);
}
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

/* ---- 荐股页（全大盘）：扫描参数 + 推荐榜；已删掉行业/概念筛选与模块矩阵 ---- */
await gotoView("pick");
for (const sel of ["#pick-pool-size", "#pick-model-top", "#pick-scan-hint", "#btn-pick-run",
                   "#btn-pick-stop", "#btn-pick-history"]) {
  const found = await page.locator(sel).count();
  console.log(`${found > 0 ? "PASS" : "FAIL"}  荐股页扫描参数控件 ${sel}`);
  if (!found) failed++;
}
const legacyPick = await page.locator("#pick-modules, #pick-panel-industry, #pick-panel-concept, " +
  "#pick-tabs, [id^='pick-mod-']").count();
console.log(`${legacyPick === 0 ? "PASS" : "FAIL"}  荐股页已无模块 / 行业 / 概念筛选与矩阵（残留 ${legacyPick}）`);
if (legacyPick !== 0) failed++;
await page.waitForSelector("#pick-result .card", { timeout: 25000 }).catch(() => {});
const pickText = await page.locator("#pick-result").innerText().catch(() => "");
const rankRows = await page.locator("#pick-rank tbody tr").count();
console.log(`${rankRows > 0 ? "PASS" : "FAIL"}  推荐榜已渲染（${rankRows} 行）`);
if (!(rankRows > 0)) failed++;
const rankHeads = await page.locator("#pick-rank thead").innerText().catch(() => "");
const headOk = /模型评分/.test(rankHeads) && /机械分/.test(rankHeads) && /打法/.test(rankHeads);
console.log(`${headOk ? "PASS" : "FAIL"}  推荐榜列头含 模型评分 / 机械分 / 打法`);
if (!headOk) failed++;
const noPrice = !/买点|止损价|目标位/.test(pickText);
console.log(`${noPrice ? "PASS" : "FAIL"}  模型不给买卖价位（页面无买点/止损/目标位）`);
if (!noPrice) failed++;
const mechCard = /机械分（辅助）/.test(pickText) && /《机器打分逻辑.txt》/.test(pickText);
console.log(`${mechCard ? "PASS" : "FAIL"}  机械口径卡（可得满分 / 缺失项 / 口径来源）`);
if (!mechCard) failed++;
const vetoCard = /一票否决/.test(pickText);
console.log(`${vetoCard ? "PASS" : "FAIL"}  一票否决卡（执行范围与未覆盖项）`);
if (!vetoCard) failed++;
const styleRow = await page.locator("#pick-rank tbody tr .chip").allInnerTexts().catch(() => []);
const styleInRank = styleRow.some(x =>
  ["超短线", "短线", "波段", "中线", "长线", "未定"].includes(x.trim()));
console.log(`${styleInRank ? "PASS" : "FAIL"}  推荐榜每行带打法标签`);
if (!styleInRank) failed++;

/* ---- 荐股结果：推荐榜 30 只以内 + 候选池表 + 机械层 ---- */
const candRows = await page.locator("#pick-rank tbody tr").count();
const candOk = candRows > 0 && candRows <= 30;
console.log(`${candOk ? "PASS" : "FAIL"}  推荐榜长度 ≤ 30（${candRows} 行）`);
if (!candOk) failed++;
const poolRows = await page.locator("#pick-result .card:last-of-type tbody tr").count();
console.log(`${poolRows > 0 ? "PASS" : "FAIL"}  候选池表已渲染（${poolRows} 行）`);
if (!(poolRows > 0)) failed++;


/* ---- 交易流页：盯盘控件 + 流卡区 + 开流表单 + 详情（不点「体检 / 重算」，不打模型） ---- */
await gotoView("flow");
await page.waitForSelector("#btn-flow-create", { timeout: 20000 }).catch(() => {});
for (const sel of ["#flow-list", "#btn-flow-create", "#btn-flow-poll", "#flow-interval",
                   "#flow-band", "#flow-console", "#flow-detail", "#btn-flow-plan",
                   "#btn-flow-check", "#nav-flow-badge",
                   "#flow-new-capital", "#flow-new-target", "#flow-new-loss", "#flow-new-style",
                   "#flow-new-code", "#flow-new-alloc"]) {
  const found = await page.locator(sel).count();
  console.log(`${found > 0 ? "PASS" : "FAIL"}  交易流页控件 ${sel}`);
  if (!found) failed++;
}
/* 批注 5：跟踪清单卡整块删除（接口仍在服务端） */
const trackGone = await page.locator("#track-table, #btn-track-add, #btn-track-generate, #track-detail").count();
console.log(`${trackGone === 0 ? "PASS" : "FAIL"}  跟踪清单卡已删除（残留 ${trackGone}）`);
if (trackGone !== 0) failed++;
/* 开流表单：打法三选一、没有「最多加仓」栏（它已按需求删掉） */
const styleOpts = await page.locator("#flow-new-style option").allInnerTexts().catch(() => []);
const styleOk = ["超短线", "短线", "波段"].every(s => styleOpts.includes(s));
console.log(`${styleOk ? "PASS" : "FAIL"}  开流表单打法三选一（${styleOpts.join("/")}）`);
if (!styleOk) failed++;
const hasAdds = await page.locator("#flow-new-adds").count();
console.log(`${hasAdds === 0 ? "PASS" : "FAIL"}  开流表单已无「最多加仓」栏`);
if (hasAdds !== 0) failed++;
await page.waitForFunction(() => {
  const box = document.querySelector("#flow-list");
  return box && (box.querySelector(".flow-card") || box.querySelector(".empty"));
}, null, { timeout: 20000 }).catch(() => {});
const flowCards = await page.locator("#flow-list .flow-card").count();
const flowText = await page.locator("#flow-list").innerText().catch(() => "");
const flowOk = flowCards > 0 || /还没有交易流/.test(flowText);
console.log(`${flowOk ? "PASS" : "FAIL"}  交易流卡区渲染（${flowCards} 条流）`);
if (!flowOk) failed++;
if (flowCards > 0) {
  /* 卡片里要有标的表 + 加标的表单（打法下拉三档） */
  const addBoxes = await page.locator("#flow-list .flow-card details.fl-add").count();
  console.log(`${addBoxes === flowCards ? "PASS" : "FAIL"}  每条流都有「添加标的」表单`);
  if (addBoxes !== flowCards) failed++;
  const rowAdds = await page.locator("#flow-list .flow-card details.fl-add [data-add='style'] option")
    .count();
  console.log(`${rowAdds >= flowCards * 3 ? "PASS" : "FAIL"}  加标的打法下拉（${rowAdds} 个选项）`);
  if (rowAdds < flowCards * 3) failed++;
  const targetTables = await page.locator("#flow-list .flow-card table.fl-targets").count();
  console.log(`${targetTables >= 1 ? "PASS" : "FAIL"}  流卡片里有标的表`);
  if (targetTables < 1) failed++;
}
/* 批注 1/2/4：行内分时图（买卖线 + 计划线）与「计算」按钮 */
const calcBtns = await page.locator("#flow-list [data-act='plan']").allInnerTexts().catch(() => []);
const calcOk = calcBtns.some(x => x.trim() === "计算");
console.log(`${calcOk ? "PASS" : "FAIL"}  行内按钮已改名「计算」（${calcBtns.join("/")}）`);
if (!calcOk) failed++;
await page.waitForSelector("#flow-list canvas.fl-chart", { timeout: 25000 }).catch(() => {});
const rowCharts = await page.locator("#flow-list canvas.fl-chart").count();
console.log(`${rowCharts > 0 ? "PASS" : "FAIL"}  标的行内分时图已渲染（${rowCharts} 张）`);
if (!(rowCharts > 0)) failed++;
const chartPixels = await page.evaluate(() => {
  const c = document.querySelector("#flow-list canvas.fl-chart");
  if (!c) return 0;
  try { return c.getContext("2d").getImageData(0, 0, c.width, c.height).data.filter(v => v > 0).length; }
  catch (e) { return -1; }
});
console.log(`${chartPixels !== 0 ? "PASS" : "FAIL"}  行内分时图确实画了东西（非空像素 ${chartPixels}）`);
if (chartPixels === 0) failed++;
const bandOptions = await page.locator("#flow-band option").count();
console.log(`${bandOptions >= 3 ? "PASS" : "FAIL"}  接近带档位（${bandOptions} 档）`);
if (bandOptions < 3) failed++;
if (flowCards > 0) {
  await page.locator("#flow-list .flow-card [data-act='detail']").first().click();
  await page.waitForSelector("#flow-detail .fl-detail-head", { timeout: 15000 }).catch(() => {});
  const detail = await page.locator("#flow-detail").innerText().catch(() => "");
  const hasBlocks = /补录成交/.test(detail) && /体检记录/.test(detail) && /事件时间线/.test(detail);
  console.log(`${hasBlocks ? "PASS" : "FAIL"}  流详情含成交录入 / 体检 / 事件时间线`);
  if (!hasBlocks) failed++;
  const tabs = await page.locator("#flow-detail [data-tab]").count();
  console.log(`${tabs > 0 ? "PASS" : "FAIL"}  流详情有标的页签（${tabs} 只）`);
  if (!tabs) failed++;
} else {
  console.log("INFO  还没有交易流，跳过详情断言（开流表单已在上面断言）");
}
/* ---- 总控台交易流卡区 ---- */
await gotoView("console");
for (const sel of ["#console-flow", "#console-flow-sum", "#btn-flow-goto"]) {
  const found = await page.locator(sel).count();
  console.log(`${found > 0 ? "PASS" : "FAIL"}  总控台交易流控件 ${sel}`);
  if (!found) failed++;
}

/* ---- 模型配置：默认 profile 可切换（只看不点，避免改用户配置） ---- */
await gotoView("models");
await page.waitForFunction(() => {
  const el = document.querySelector("#default-profile");
  return el && el.options.length > 0;
}, null, { timeout: 20000 }).catch(() => {});
for (const sel of ["#default-profile", "#btn-default-save", "#btn-profile-add", "#btn-provider-add"]) {
  const found = await page.locator(sel).count();
  console.log(`${found > 0 ? "PASS" : "FAIL"}  模型配置控件 ${sel}`);
  if (!found) failed++;
}
/* 批注 10：盘前 / 盘中 / 盘后 / 全时段四个下拉已下线 */
const phaseSel = await page.locator("[id^='default-phase-']").count();
console.log(`${phaseSel === 0 ? "PASS" : "FAIL"}  时段下拉已删除（残留 ${phaseSel}）`);
if (phaseSel !== 0) failed++;
/* 批注 11：没配 key 的服务商显示「未配置」，不再显示借用 token.txt 的掩码 */
const provText = await page.locator("#provider-table").innerText().catch(() => "");
console.log(`${provText.includes("未配置") || provText.includes("已配置") ? "PASS" : "FAIL"}  provider 表显示 key 配置状态`);
if (!(provText.includes("未配置") || provText.includes("已配置"))) failed++;
const profileOptions = await page.locator("#default-profile option").count();
console.log(`${profileOptions >= 2 ? "PASS" : "FAIL"}  默认 profile 可选档位数（${profileOptions}）`);
if (profileOptions < 2) failed++;
/* profile 卡片上的增删改按钮（批注 8、9）：每档都有「编辑 / 另存为 / 删除」 */
const profBtns = await page.locator("#profile-grid [data-edit-profile]").count();
console.log(`${profBtns > 0 ? "PASS" : "FAIL"}  profile 卡片带编辑按钮（${profBtns}）`);
if (!(profBtns > 0)) failed++;

/* ---- 运行研判：标的代码多选（输入 / 加入 / 移除） ---- */
await gotoView("run");
await page.waitForSelector("#code-picked", { state: "attached", timeout: 20000 });
for (const sel of ["#btn-code-add", "#code-picked", "#btn-code-clear"]) {
  const found = await page.locator(sel).count();
  console.log(`${found > 0 ? "PASS" : "FAIL"}  运行页多选控件 ${sel}`);
  if (!found) failed++;
}
/* 覆盖 Key 的防呆提示：填了值要红字提醒 */
await page.evaluate(() => {
  document.querySelectorAll("#view-run details.adv").forEach(d => { d.open = true; });
});
await page.waitForTimeout(300);
await page.fill("#api-key", "test-only");
await page.waitForTimeout(200);
const hintShown = await page.locator("#override-hint").isVisible().catch(() => false);
console.log(`${hintShown ? "PASS" : "FAIL"}  填了 --api-key 会提示「覆盖配置」`);
if (!hintShown) failed++;
await page.fill("#api-key", "");
await page.waitForTimeout(200);
const hintHidden = await page.locator("#override-hint").isVisible().catch(() => true);
console.log(`${!hintHidden ? "PASS" : "FAIL"}  清空 --api-key 后提示消失`);
if (hintHidden) failed++;
await page.fill("#code", "512890");
await page.press("#code", "Enter");
await page.fill("#code", "002463");
await page.click("#btn-code-add");
await page.waitForTimeout(300);
const chips = await page.$$eval("#code-picked .chip", els =>
  els.map(e => e.textContent.replace("×", "").trim()));
const pickedOk = chips.length === 2 && chips.indexOf("512890") >= 0 && chips.indexOf("002463") >= 0;
console.log(`${pickedOk ? "PASS" : "FAIL"}  运行页多选标的（${chips.join("/")}）`);
if (!pickedOk) failed++;
const hintText = await page.textContent("#code-hint");
const hintOk = /已选 2 只/.test(hintText);
console.log(`${hintOk ? "PASS" : "FAIL"}  多选提示文案（${hintText.slice(0, 30)}…）`);
if (!hintOk) failed++;
await page.click("#code-picked .chip-x");
await page.waitForTimeout(200);
const left = await page.locator("#code-picked .chip").count();
console.log(`${left === 1 ? "PASS" : "FAIL"}  点 × 能移除单只（剩 ${left}）`);
if (left !== 1) failed++;
await page.click("#btn-code-clear");
await page.waitForTimeout(200);
const cleared = await page.locator("#code-picked .chip").count();
console.log(`${cleared === 0 ? "PASS" : "FAIL"}  清空已选标的`);
if (cleared !== 0) failed++;

await browser.close();

if (problems.length) {
  failed++;
  console.log("FAIL  页面报错：" + problems.join(" | "));
} else {
  console.log("PASS  无 console error / pageerror");
}
console.log(failed ? `结果：FAIL（${failed}）` : "结果：PASS");
process.exit(failed ? 1 : 0);
