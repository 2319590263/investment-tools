/*
 * `/m` 手机专用控制台冒烟测试（可选，不进 Python 测试套件）。
 *   node tests/ui/mobile_smoke.mjs
 *   node tests/ui/mobile_smoke.mjs 9000
 *
 * 覆盖三种常见手机视口、九个页面、底部导航/更多面板、整页横向溢出和手机写入确认。
 */

import { mkdirSync } from "node:fs";

const PORT = process.argv[2] || process.env.AIPLAN_WEBUI_PORT || "8765";
const BASE = `http://127.0.0.1:${PORT}`;
const VIEWPORTS = [
  { width: 360, height: 800 },
  { width: 390, height: 844 },
  { width: 430, height: 932 },
];
const PRIMARY = ["console", "run", "report", "holdings"];
const MORE = ["watch", "pick", "models", "market"];
const CONTENT = {
  console: "#console-body .kv",
  run: "#console",
  report: "#report-struct .card",
  holdings: "#hold-table tbody tr",
  watch: "#watch-table tbody tr",
  pick: "#pick-scan-hint, #pick-result .card",
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
  const probe = await fetch(BASE + "/m");
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

mkdirSync("build/mobile-smoke", { recursive: true });
let failed = 0;

for (const viewport of VIEWPORTS) {
  const page = await browser.newPage({ viewport });
  const problems = [];
  page.on("console", m => { if (m.type() === "error") problems.push("console: " + m.text()); });
  page.on("pageerror", e => problems.push("pageerror: " + e.message));
  page.on("response", r => {
    if (r.status() >= 500) problems.push("HTTP " + r.status() + " " + r.url());
  });

  await page.goto(BASE + "/m", { waitUntil: "domcontentloaded" });
  await page.waitForSelector('body[data-shell="mobile"]', { timeout: 20000 });
  const sideHidden = await page.locator(".side").isHidden();
  const navVisible = await page.locator("#mobile-nav").isVisible();
  console.log(`${sideHidden && navVisible ? "PASS" : "FAIL"}  ${viewport.width}px 移动外壳（侧栏隐藏 / 底部导航显示）`);
  if (!sideHidden || !navVisible) failed++;
  const defaultConsole = await page.locator("#view-console").evaluate(el => el.classList.contains("active"));
  console.log(`${defaultConsole ? "PASS" : "FAIL"}  ${viewport.width}px 手机默认首页为总控台`);
  if (!defaultConsole) failed++;

  const gotoView = async view => {
    if (PRIMARY.includes(view)) {
      await page.click(`#mobile-nav [data-view="${view}"]`);
    } else {
      await page.click("#mobile-more-toggle");
      await page.waitForSelector("#mobile-more:not([hidden])");
      if (view === MORE[0]) {
        const desktopLink = await page.locator(".mobile-desktop-link").getAttribute("href");
        console.log(`${desktopLink === "/" ? "PASS" : "FAIL"}  ${viewport.width}px 更多面板返回电脑版入口`);
        if (desktopLink !== "/") failed++;
      }
      await page.click(`#mobile-more [data-view="${view}"]`);
    }
    await page.waitForFunction(v => document.querySelector(`#view-${v}`)?.classList.contains("active"), view, { timeout: 15000 });
    await page.waitForSelector(CONTENT[view], { timeout: 30000 });
  };

  for (const view of [...PRIMARY, ...MORE]) {
    try {
      await gotoView(view);
      await page.screenshot({ path: `build/mobile-smoke/${viewport.width}-${view}.png` });
      const overflow = await page.evaluate(() => ({
        page: document.documentElement.scrollWidth,
        body: document.body.scrollWidth,
        viewport: window.innerWidth,
      }));
      const ok = overflow.page <= overflow.viewport + 1 && overflow.body <= overflow.viewport + 1;
      console.log(`${ok ? "PASS" : "FAIL"}  ${viewport.width}px view-${view} 无整页横向溢出（${overflow.page}/${overflow.body}/${overflow.viewport}）`);
      if (!ok) failed++;
    } catch (e) {
      console.log(`FAIL  ${viewport.width}px view-${view} 未完成：${String(e).slice(0, 100)}`);
      failed++;
    }
  }

  try {
    await gotoView("holdings");
    // 账户表单是接口回来后异步渲染的：先等它出现再量字号，否则会量到空
    await page.waitForSelector("#account-form input", { timeout: 15000 }).catch(() => {});
    const inputCount = await page.locator("#account-form input").count();
    const inputSize = inputCount
      ? await page.locator("#account-form input").first().evaluate(el => getComputedStyle(el).fontSize)
      : "0px";
    console.log(`${inputCount && parseFloat(inputSize) >= 16 ? "PASS" : "FAIL"}  ` +
      `${viewport.width}px 表单字号 ${inputSize}`);
    if (!inputCount || parseFloat(inputSize) < 16) failed++;
    await page.click("#btn-account-save");
    await page.waitForSelector("#overlay:not([hidden])", { timeout: 10000 });
    const accountConfirm = await page.locator("#modal-title").textContent();
    console.log(`${/账户配置/.test(accountConfirm || "") ? "PASS" : "FAIL"}  ${viewport.width}px 账户保存二次确认`);
    if (!/账户配置/.test(accountConfirm || "")) failed++;
    await page.click("#cf-cancel");

    await gotoView("models");
    await page.click("#btn-models-save");
    await page.waitForSelector("#overlay:not([hidden])", { timeout: 10000 });
    const modelConfirm = await page.locator("#modal-title").textContent();
    console.log(`${/模型配置/.test(modelConfirm || "") ? "PASS" : "FAIL"}  ${viewport.width}px 模型保存二次确认`);
    if (!/模型配置/.test(modelConfirm || "")) failed++;
    await page.click("#cf-cancel");
  } catch (e) {
    console.log(`FAIL  ${viewport.width}px 手机写入确认：${String(e).slice(0, 100)}`);
    failed++;
  }

  if (problems.length) {
    console.log(`FAIL  ${viewport.width}px 页面报错：${problems.join(" | ")}`);
    failed++;
  } else {
    console.log(`PASS  ${viewport.width}px 无 console error / pageerror`);
  }
  await page.close();
}

await browser.close();
console.log(failed ? `结果：FAIL（${failed}）` : "结果：PASS");
process.exit(failed ? 1 : 0);
