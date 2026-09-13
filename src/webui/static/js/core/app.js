/* 全局状态与跨页动作（切页、跳报告、刷新顶部状态）。
 *
 * 视图之间不互相 import：每个视图在文件末尾用 registerView() 注册自己的「切到本页时要
 * 干什么」与对外动作，这里只通过注册表调用，避免模块之间形成环。
 */
import { api } from "./api.js";
import { syncMobileNav } from "./mobile.js";
import { $, $$, chip, esc, fmt, fmtMoney, toast } from "./util.js";

export const State = {
  state: null,
  reports: [],
  report: null,
  reportTab: "struct",
  job: null,
  currentView: "console",
};

export const VIEW_TITLE = {
  console: "总控台",
  flow: "交易流",
  run: "运行研判", report: "报告", history: "历史与复盘",
  holdings: "持仓 / 账户", watch: "自选股", pick: "荐股",
  models: "模型配置", market: "大盘快照",
};

/* ---------------- 视图注册表 ---------------- */

export const registry = {};

export function registerView(name, api) {
  Object.assign(registry[name] || (registry[name] = {}), api);
}

export function viewApi(name) {
  return registry[name] || {};
}

/* =========================================================================
   导航
   ========================================================================= */

export function showView(name) {
  State.currentView = name;
  $$("#nav .nav-item").forEach(b => b.classList.toggle("active", b.dataset.view === name));
  $$(".view").forEach(v => v.classList.toggle("active", v.id === "view-" + name));
  $("#topbar-title").textContent = VIEW_TITLE[name] || name;
  syncMobileNav(name);
  const views = $(".views");
  if (views) views.scrollTop = 0;
  const onShow = viewApi(name).onShow;
  return onShow ? onShow() : undefined;
}

/* =========================================================================
   顶部状态
   ========================================================================= */

export async function refreshState() {
  try {
    const st = await api("/api/state");
    State.state = st;
    const total = ((st["账户"] || {})["配置"] || {})["总资金"];
    const ssum = (st["持仓"] || {})["汇总"] || {};
    $("#topbar-meta").innerHTML =
      chip("总资金 " + (total == null ? "未配置" : fmtMoney(total, 0) + " 元"), total == null ? "bad" : "") +
      chip("持仓 " + fmt(ssum["持仓市值_元"], 0) + " 元 · " + (ssum["标的数"] || 0) + " 只") +
      chip("profile " + ((st["模型"] || {})["默认_profile"] || "—"), "accent");
    $("#side-foot").innerHTML =
      "<div><b>模型</b> " + esc((st["模型"] || {})["默认_profile"] || "—") + "</div>" +
      "<div><b>报告</b> " + st["报告数"] + " 份</div>" +
      "<div><b>aiplan</b> schema " + esc((st["aiplan"] || {}).schema_version) +
      " / prompt " + esc((st["aiplan"] || {}).prompt_version) + "</div>";

    const profSel = $("#profile");
    profSel.innerHTML = '<option value="">（用配置默认）</option>' +
      ((st["模型"] || {}).profiles || []).map(p =>
        '<option value="' + esc(p["名称"]) + '"' + (p["名称"] === (st["模型"] || {})["默认_profile"] ? " selected" : "") + ">" +
        esc(p["名称"]) + "</option>").join("");
    const pickProf = $("#pick-profile");
    if (pickProf) {
      pickProf.innerHTML = '<option value="">（用配置默认）</option>' +
        ((st["模型"] || {}).profiles || []).map(p =>
          '<option value="' + esc(p["名称"]) + '"' + (p["名称"] === (st["模型"] || {})["默认_profile"] ? " selected" : "") + ">" +
          esc(p["名称"]) + "</option>").join("");
    }
    const dl = $("#code-list");
    dl.innerHTML = (st["代码候选"] || []).map(c =>
      '<option value="' + esc(c["thscode"] || c["代码"]) + '">' + esc(c["名称"] || "") + "</option>").join("");
    const runApi = viewApi("run");
    const picked = (runApi.pickedCodes && runApi.pickedCodes()) || [];
    if (!$("#code").value && !picked.length && (st["代码候选"] || []).length) {
      $("#code").value = (st["代码候选"][0]["thscode"] || st["代码候选"][0]["代码"]);
    }
    const run = viewApi("run");
    if (run.syncCmd) run.syncCmd();
    if (run.checkReadiness) run.checkReadiness();
  } catch (e) {
    toast("读取状态失败：" + e.message, "bad");
  }
}

export async function openReport(path) {
  showView("report");
  try {
    const b = await api("/api/report?path=" + encodeURIComponent(path));
    State.report = b;
    const render = viewApi("report").render;
    if (render) render();
    if ($("#report-pick")) $("#report-pick").value = b["json路径"];
  } catch (e) { toast(e.message, "bad"); }
}
