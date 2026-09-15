/* 总控台：持仓股 / 自选股卡片 + 到价提醒 + 自动刷新 + 系统状态。
 *
 * 数据来自 /api/overview（卡片、计划线、触发、分时、提醒）与既有的 /api/state、/api/market、
 * /api/trash（系统状态）。默认用本地口径价格，点「刷新实盘价」或打开自动刷新才联网取实时行情；
 * 所有刷新只改页面显示，不写盘。
 */
import { api } from "../core/api.js";
import { State, openReport, registerView, showView, viewApi } from "../core/app.js";
import { $, $$, badge, chip, esc, fmt, fmtMoney, fmtPct, num, pctClass, toast } from "../core/util.js";
import { DEFAULT_MS, INTERVALS, createPoller, isTradingSession, loadPref, savePref } from "../core/poller.js";
import { bindStockCards, drawSpark, planLines, stockCard } from "../ui/stockcard.js";
import { showKlineModal } from "../ui/kline.js";
import { kv } from "../ui/cards.js";
import { confirmModal, openModal } from "../ui/modal.js";

const READ_KEY = "aiplan.alerts.readAt";   // 未读游标：上次打开消息队列的时间
let queue = [];                            // 服务端消息队列（data/ai/alerts.jsonl）
let readAt = 0;
let poller = null;
let hasOverview = false;
let lastLive = false;                      // 上一次总览是否取的是实时价（加自选后按同口径刷新）

function ago(ts) {
  const n = Number(ts);
  if (!n) return "—";
  const ms = n > 1e11 ? n : n * 1000;        // 秒 / 毫秒都接受
  const sec = Math.max(0, (Date.now() - ms) / 1000);
  if (sec < 90) return "刚刚";
  if (sec < 3600) return Math.round(sec / 60) + " 分钟前";
  if (sec < 86400) return (sec / 3600).toFixed(1) + " 小时前";
  return (sec / 86400).toFixed(1) + " 天前";
}

/* =========================================================================
   系统状态（沿用原有内容）
   ========================================================================= */

export function renderSystem(st, mk, tr) {
  const s = st || {};
  const acct = ((s["账户"] || {})["配置"]) || {};
  const sum = ((s["持仓"] || {})["汇总"]) || {};
  const models = s["模型"] || {};
  const pan = (mk || {})["pan"] || {};
  const s3 = (mk || {})["stock3d"] || {};
  const panDoc = pan["doc"] || {};
  const latest = s["最新报告"] || {};
  const trash = tr || {};
  const total = acct["总资金"];

  let html = '<div class="sec-title">账户与持仓</div><div class="stack-s">' +
    kv("总资金", total == null ? '<span class="muted">未配置</span>' : fmtMoney(total, 0) + " 元") +
    kv("持仓市值", fmtMoney(sum["持仓市值_元"], 0) + " 元 · " + (sum["标的数"] || 0) + " 只" +
      (sum["持仓占比_pct"] == null ? "" : ' <span class="muted">(' + fmt(sum["持仓占比_pct"], 1) + "%)</span>")) +
    kv("可用现金", fmtMoney(sum["现金_元"], 0) + " 元" +
      (sum["现金占比_pct"] == null ? "" : ' <span class="muted">(' + fmt(sum["现金占比_pct"], 1) + "%)</span>")) +
    kv("报告归档", (s["报告数"] == null ? "—" : s["报告数"]) + " 份") +
    "</div>";

  html += '<div class="sec-title" style="margin-top:14px">数据新鲜度</div><div class="stack-s">' +
    kv("pan 快照", pan["路径"]
      ? '<span class="mono">' + esc(pan["路径"]) + '</span> <span class="muted">· 交易日 ' +
        esc(panDoc["trade_date"] || "—") + " · " + ago(pan["mtime"]) + "</span>"
      : '<span class="muted">没有（先跑 python main.py pan post）</span>') +
    kv("stock3d 快照", s3["路径"]
      ? '<span class="mono">' + esc(s3["路径"]) + '</span> <span class="muted">· ' +
        esc(String(s3["generated_at"] || "").slice(0, 19)) + " · " +
        ((s3["symbols"] || []).length) + " 只标的</span>"
      : '<span class="muted">没有（先跑 python main.py stock3d pull <代码>）</span>') +
    kv("最新报告", latest["json路径"]
      ? '<span class="mono">' + esc(latest["json路径"]) + '</span> <span class="muted">· ' +
        esc(latest["phase标签"] || latest["phase"] || "") + " · " + esc(latest["时间"] || "") + "</span>"
      : '<span class="muted">还没有报告</span>') +
    kv("回收站", (trash["items"] || []).length + " 个文件 · 超过 " +
      (trash["过期天数"] == null ? 7 : trash["过期天数"]) + " 天自动真删") +
    "</div>";

  html += '<div class="sec-title" style="margin-top:14px">模型与环境</div><div class="stack-s">' +
    kv("默认 profile", esc(models["默认_profile"] || "—") +
      ' <span class="muted">· provider ' + ((models["providers"] || []).length) +
      " 个 / profile " + ((models["profiles"] || []).length) + " 个</span>") +
    kv("Python", esc(s["python版本"] || "—") + ' <span class="mono muted">' + esc(s["python"] || "") + "</span>") +
    kv("项目根目录", '<span class="mono">' + esc(s["根目录"] || "") + "</span>") +
    kv("服务器时间", esc(s["时间"] || "—")) +
    "</div>";
  return html;
}

/* =========================================================================
   卡片区
   ========================================================================= */

function cardsGrid(list, kind) {
  if (!list.length) {
    return '<div class="muted">' + (kind === "hold"
      ? "持仓文件里还没有数据（可在「持仓 / 账户」页编辑）"
      : "自选股为空（可在「自选股」页添加）") + "</div>";
  }
  return list.map(item => stockCard(item, kind)).join("");
}

function loadReadAt() {
  try { readAt = Number(localStorage.getItem(READ_KEY) || 0) || 0; } catch (e) { readAt = 0; }
  return readAt;
}

function saveReadAt(ts) {
  readAt = ts;
  try { localStorage.setItem(READ_KEY, String(ts)); } catch (e) { /* 隐私模式忽略 */ }
}

function isUnread(row) {
  return (num(row["ts"]) || 0) * 1000 > readAt;
}

function alertBadgeCls(type) {
  const map = { "已到买点": "accent", "已破止损": "bad", "已达目标": "ok",
                "已到减仓位": "warn", "已跌破支撑": "bad", "已上破压力": "warn" };
  return map[type] || "flat";
}

export function alertRowHtml(row) {
  return '<div class="alert-row' + (isUnread(row) ? " unread" : "") +
    '" data-report="' + esc(row["报告路径"] || "") + '">' +
    '<span class="badge ' + alertBadgeCls(row["类型"]) + '">' + esc(row["类型"]) + "</span>" +
    "<b>" + esc(row["名称"] || row["代码"]) + "</b>" +
    '<span class="mono muted">' + esc(row["代码"]) + "</span>" +
    (row["手数"] ? '<span class="chip">' + esc(row["手数"]) + "</span>" : "") +
    (row["价位"] == null ? "" : '<span class="mono muted">@' + fmt(row["价位"]) + "</span>") +
    '<span class="muted">' + esc(row["文案"] || "") + "</span>" +
    '<span class="spacer"></span><span class="muted">' + esc(row["时间"] || "") + "</span></div>";
}

export function bindAlertRows(root) {
  if (!root) return;
  root.querySelectorAll(".alert-row").forEach(row => row.addEventListener("click", () => {
    const path = row.dataset.report;
    if (path) openReport(path);
    else toast("这条消息没有关联报告", "warn");
  }));
}

function renderQueue() {
  const unread = queue.filter(isUnread).length;
  const card = $("#console-alert-card");
  if (card) card.hidden = !queue.length;
  const note = $("#console-alert-note");
  if (note) {
    note.textContent = queue.length
      ? "共 " + queue.length + " 条 · 未读 " + unread + " 条 · 落盘 data/ai/alerts.jsonl"
      : "";
  }
  const box = $("#console-alerts");
  if (box) {
    box.innerHTML = queue.map(alertRowHtml).join("");
    bindAlertRows(box);
  }
  const badge = $("#alert-count");
  if (badge) {
    badge.hidden = !unread;
    badge.textContent = unread > 99 ? "99+" : String(unread);
  }
}

export async function loadAlerts(limit) {
  try {
    const res = await api("/api/alerts?limit=" + (limit || 50));
    queue = res.items || [];
    renderQueue();
  } catch (e) {
    /* 队列读不到不影响主流程 */
  }
}

export async function openAlertDrawer() {
  let items = queue;
  try {
    const res = await api("/api/alerts?limit=100");
    items = res.items || [];
  } catch (e) { /* 用已有队列兜底 */ }
  const rows = items.map(alertRowHtml).join("");
  openModal("消息队列",
    '<div class="muted" style="margin-bottom:8px">最近 ' + items.length +
    " 条到价提醒 · 落盘 data/ai/alerts.jsonl · 点一条跳对应报告</div>" +
    (rows || '<div class="muted">还没有消息：打开自动刷新或点「刷新实盘价」后，到价才会入队。</div>'));
  bindAlertRows($("#modal-body"));
  queue = items;
  saveReadAt(Date.now());        // 打开即视为已读
  renderQueue();
}

function renderPollerState() {
  const el = $("#console-auto-state");
  if (!el || !poller) return;
  const st = poller.state;
  if (!st.enabled) { el.textContent = ""; return; }
  const reason = poller.pauseReason();
  if (reason) { el.textContent = "自动刷新已暂停：" + reason; return; }
  el.textContent = "下次 " + (st.nextAt ? st.nextAt.toLocaleTimeString() : "—") +
    (st.failures ? "（连续失败 " + st.failures + " 次，已退避）" : "");
}

const cardHandlers = {
  onOpen: code => openCodeReport(code),
  onRun: code => {
    viewApi("run").pickCode(code);
    showView("run");
    toast("已把 " + code + " 填入运行页", "ok");
  },
  onKline: (code, card) => {
    const name = card.querySelector(".sc-h b");
    showKlineModal(code, name ? name.textContent.trim() : "");
  },
  onRemove: (code, card) => {
    const nameEl = card.querySelector(".sc-h b");
    const name = nameEl ? nameEl.textContent.trim() : "";
    confirmModal("从自选股移除？",
      "将把 " + code + " " + name + " 从 自选股.md 里删除（文件有 .bak 备份，持仓不受影响）。",
      async () => {
        try {
          const res = await api("/api/watchlist/remove", { method: "POST",
            body: JSON.stringify({ code: code }) });
          if (!res.ok) { toast(res.error || "移除失败", "bad"); return; }
          toast("已移除 " + code, "ok");
          if (State.state) {
            State.state["自选股"] = res["自选股"] || State.state["自选股"];
            State.state["代码候选"] = res["代码候选"] || State.state["代码候选"];
          }
          await loadConsole(lastLive);      // 卡区当场少一张
        } catch (e) { toast(e.message, "bad"); }
      }, "确认移除");
  },
};

function openCodeReport(code) {
  const six = String(code || "").slice(0, 6);
  const hit = (State.reports || []).find(r =>
    String(((r["摘要"] || {})["标的代码"]) || "").slice(0, 6) === six);
  if (hit) { openReport(hit["json路径"]); return; }
  cardHandlers.onRun(code);
  toast("这只还没有报告，已填进运行页", "warn");
}

export function renderOverview(d) {
  hasOverview = true;
  const sess = d["时段"] || {};
  const rf = d["刷新"] || {};
  lastLive = rf["模式"] === "实时";
  const sum = d["汇总"] || {};
  const holdList = d["持仓"] || [];
  const watchList = d["自选"] || [];

  const holdSum = $("#console-hold-sum");
  if (holdSum) {
    holdSum.textContent = "市值 " + fmtMoney(sum["持仓市值_元"], 0) + " 元 · " + (sum["标的数"] || 0) + " 只" +
      (sum["持仓占比_pct"] == null ? "" : " · 占仓 " + fmt(sum["持仓占比_pct"], 1) + "%");
  }
  const watchSum = $("#console-watch-sum");
  if (watchSum) {
    const noPrice = watchList.filter(w => w["现价"] == null).length;
    watchSum.textContent = watchList.length + " 只" + (noPrice ? " · 未抓数 " + noPrice + " 只" : "");
  }
  const holdBox = $("#console-holdings");
  const watchBox = $("#console-watch");
  if (holdBox) holdBox.innerHTML = cardsGrid(holdList, "hold");
  if (watchBox) watchBox.innerHTML = cardsGrid(watchList, "watch");
  renderFlows(d["flows"] || {}, rf);

  $$("#view-console .stock-card").forEach(card => {
    const item = holdList.concat(watchList).find(x => x["代码"] === card.dataset.code);
    const canvas = card.querySelector("canvas[data-spark]");
    if (item && canvas) {
      if (!drawSpark(canvas, item["分时"], planLines(item), item["昨收"])) {
        canvas.replaceWith(Object.assign(document.createElement("div"),
          { className: "sc-hint", textContent: "分时暂时取不到" }));
      }
    }
  });
  bindStockCards($("#view-console"), cardHandlers);

  const last = $("#console-last");
  if (last) {
    last.textContent = "上次刷新 " + (rf["时间"] || "—") +
      (rf["模式"] === "实时" ? "（实时）" : "（本地）") + " · " + (rf["耗时_s"] == null ? "—" : rf["耗时_s"] + "s");
  }
  const hints = $("#console-hints");
  if (hints) hints.innerHTML = (rf["提示"] || []).map(h => '<div class="muted">· ' + esc(h) + "</div>").join("");

  if (poller) poller.setSession(sess["名称"], sess["是否交易日"]);
  const off = !(poller && poller.state.enabled);
  const trading = isTradingSession(sess["名称"], sess["是否交易日"]);
  const nudge = $("#console-nudge");
  if (nudge) {
    nudge.hidden = !(trading && off);
    if (trading && off) {
      const text = $("#console-nudge-text");
      if (text) {
        text.textContent = "现在是「" + sess["名称"] + "」，自动刷新未开启：到价提醒需要定期取数，建议至少用 10 秒一档。";
      }
    }
  }
  renderPollerState();
}

/* =========================================================================
   荐股榜单（读 /api/overview 的「荐股」段：最新一份产物 + 实时价）

   只读：不写盘、不额外发请求；价格随总控台自动刷新/手动刷新一起更新。
   榜单按股票去重合并——同一只在多个模块上榜也只占一行，模块收进「模块列表」；
   排名在前端按模块筛选后重算（服务端已按推荐度降序，筛选不改变相对顺序）。
   ========================================================================= */

const PICK_TOP = 30;                 // 总榜固定长度：短/波/中/长合并后只留推荐度最高的 30 只
const RANK_PLACE = ["①", "②", "③"];
const pickUI = { data: null, mods: new Set() };

function pickModuleRows(row) {
  return row["模块列表"] || (row["模块"] ? [row["模块"]] : []);
}

function pickFilteredRows() {
  return ((pickUI.data || {})["行"] || []).filter(row => {
    if (!pickUI.mods.size) return true;
    return pickModuleRows(row).some(m => pickUI.mods.has(m));
  });
}

function pickPlanText(row) {
  const parts = [];
  const push = (label, slot) => {
    if (!slot || slot["价位"] == null || slot["价位"] === "") return;
    const why = slot["依据"] ? ' <span class="muted">· ' + esc(slot["依据"]) + "</span>" : "";
    parts.push("<b>" + label + "</b> " + esc(slot["价位"]) + why);
  };
  push("买点", row["买点"]);
  push("止损", row["止损"]);
  push("止盈点", row["止盈点"]);
  if (num(row["止盈点数"]) > 1) {
    parts.push('<span class="muted">另 ' + (num(row["止盈点数"]) - 1) + " 个目标位见荐股页</span>");
  }
  return parts.join(" ｜ ");
}

function pickGradeCls(g) {
  return g === "强" ? "ok" : (g === "偏强" ? "accent" : (g === "弱" ? "bad" : "flat"));
}

function pickRankRow(row, i) {
  const code = row["代码"] || "", name = row["名称"] || "", place = num(row["首推名次"]);
  const tag = place ? chip("首推" + (RANK_PLACE[place - 1] || place) +
    (row["首推评级"] ? " " + row["首推评级"] : ""), "accent") : "";
  const stale = row["价格来源"] === "东财实时行情" ? "" : ' <span class="muted">产物价</span>';
  const mods = pickModuleRows(row);
  const modTags = mods.slice(0, 2).map(m => chip(m, "flat")).join(" ") +
    (mods.length > 2 ? ' <span class="muted">+' + (mods.length - 2) + "</span>" : "");
  let html = "<tr>" +
    '<td class="num muted">' + (i + 1) + "</td>" +
    '<td><a href="#" class="rk-code" data-code="' + esc(code) + '"><b>' + esc(name) + "</b> " +
      '<span class="mono muted">' + esc(code) + "</span></a>" +
      (row["是否持仓"] ? ' <span class="chip flat">持仓</span>' : "") +
      (row["是否自选"] ? ' <span class="chip flat">自选</span>' : "") + "</td>" +
    "<td>" + modTags + "</td>" +
    "<td>" + esc(row["来源板块"] || "—") +
      ' <span class="muted">' + esc(row["一级行业"] || "") + "</span></td>" +
    '<td class="num">' + (row["现价"] == null ? '<span class="muted">未抓数</span>'
      : fmt(row["现价"])) + stale + "</td>" +
    '<td class="num ' + pctClass(row["涨跌幅_pct"]) + '">' + fmtPct(row["涨跌幅_pct"]) + "</td>" +
    '<td class="num">' + fmt(row["机械分"], 1) + "</td>" +
    '<td class="num"><b>' + (row["推荐度"] == null ? "—" : fmt(row["推荐度"], 1)) + "</b>" +
      (num(row["加成"]) ? ' <span class="muted">+' + fmt(row["加成"], 0) + "</span>" : "") + "</td>" +
    "<td>" + badge(row["评级"] || "—", pickGradeCls(row["评级"])) + " " + tag + "</td>" +
    '<td class="num rk-ops">' +
      '<button class="btn sm ghost" data-rk-watch="' + esc(code) + '" data-rk-name="' + esc(name) + '">自选</button>' +
      '<button class="btn sm ghost" data-rk-run="' + esc(code) + '">研判</button>' +
      '<button class="btn sm ghost" data-rk-kline="' + esc(code) + '" data-rk-name="' + esc(name) + '">K线</button>' +
    "</td></tr>";
  const plan = pickPlanText(row);
  if (plan) html += '<tr class="rk-plan"><td></td><td colspan="9">' + plan + "</td></tr>";
  return html;
}

function bindPickRank(root) {
  if (!root) return;
  root.querySelectorAll(".rk-code").forEach(a => a.addEventListener("click", e => {
    e.preventDefault();
    openCodeReport(a.dataset.code);
  }));
  root.querySelectorAll("[data-rk-watch]").forEach(b => b.addEventListener("click", async () => {
    try {
      const res = await api("/api/watchlist/add", {
        method: "POST",
        body: JSON.stringify({ code: b.dataset.rkWatch, name: b.dataset.rkName || "", note: "荐股榜" }),
      });
      if (!res.ok) { toast(res.error || "加入自选失败", "bad"); return; }
      toast("已加入自选：" + b.dataset.rkWatch, "ok");
      if (State.state) {
        State.state["自选股"] = res["自选股"] || State.state["自选股"];
        State.state["代码候选"] = res["代码候选"] || State.state["代码候选"];
      }
      // 加完立刻重取一次总览：上方「自选股」卡区要当场出现这只（口径与上次一致）
      await loadConsole(lastLive);
    } catch (e) { toast(e.message, "bad"); }
  }));
  root.querySelectorAll("[data-rk-run]").forEach(b =>
    b.addEventListener("click", () => cardHandlers.onRun(b.dataset.rkRun)));
  root.querySelectorAll("[data-rk-kline]").forEach(b =>
    b.addEventListener("click", () => showKlineModal(b.dataset.rkKline, b.dataset.rkName || "")));
}

function pickModuleButtons(facets) {
  const box = $("#console-pick-mods");
  if (!box) return;
  box.innerHTML = (facets || []).map(f => {
    const n = f["候选数"] || 0;
    return '<button data-rk-mod="' + esc(f["名称"]) + '"' +
      (pickUI.mods.has(f["名称"]) ? ' class="on"' : "") + (n ? "" : " disabled") +
      ' title="' + esc(n ? (n + " 只候选") : "本次产物没跑这个模块") + '">' +
      esc(f["名称"]) + (n ? "" : "（无）") + "</button>";
  }).join("");
}

/* 总控台的交易流卡区：一行 = 一只标的（同一次批量报价 + 同一套机械判定，不调模型）。 */
export function renderFlows(block, rf) {
  const box = $("#console-flow");
  if (!box) return;
  const rows = block["行"] || [];
  const cnt = block["计数"] || {};
  const sum = $("#console-flow-sum");
  if (sum) {
    const n = cnt["进行中"] || 0;
    sum.textContent = n
      ? ("在跑 " + n + " 条流 · " + (cnt["标的"] || 0) + " 只标的 · 流资金 " +
         fmtMoney(block["合计资金"], 0) + " 元（分配 " + fmtMoney(block["分配合计"], 0) + " 元）")
      : "还没有在跑的交易流";
  }
  const src = $("#console-flow-src");
  if (src) src.textContent = (rf && rf["模式"] === "实时" ? "实时口径 ｜ " : "") + (block["口径"] || "");
  const hint = $("#console-flow-hint");
  if (hint) {
    const pending = block["待重算"] || [];
    hint.textContent = pending.length
      ? ("今日盘后还没重算计划：" + pending.map(x => x["标签"] || x["流编号"]).join("；") +
         "（去交易流页一键重算）")
      : "";
  }
  if (!rows.length) {
    box.innerHTML = '<div class="muted">还没有交易流：去「交易流」页开一条（填流资金、目标收益率、最大亏损），' +
      "再往流里加标的（每只选一种打法），就会按打法生成计划并从建仓一路盯到清仓。</div>";
    return;
  }
  let html = '<div class="table-wrap" style="max-height:320px"><table class="tbl"><thead><tr>' +
    "<th>流 / 标的</th><th>打法</th><th>状态</th><th class='num'>现价</th>" +
    "<th class='num'>合计盈亏</th><th class='num'>收益率</th><th>下一步</th><th>到价</th>" +
    "<th>操作</th></tr></thead><tbody>";
  rows.forEach(c => {
    const pnl = c["盈亏"] || {};
    const marks = c["到价"] || [];
    const stale = (c["待重算"] || {})["待重算"];
    html += "<tr>" +
      "<td>" + '<div class="mono muted">' + esc(c["流编号"] || "") + "</div>" +
        "<b>" + esc((c["标的"] || {})["名称"] || "") + "</b>" +
        '<span class="mono muted"> ' + esc((c["标的"] || {})["代码"] || "") + "</span></td>" +
      "<td>" + chip(c["打法"] || "—", "accent") +
        '<div class="muted">分配 ' + fmtMoney(c["分配资金"], 0) + "</div></td>" +
      "<td>" + chip(c["状态"] || "—", c["状态"] === "进行中" ? "accent" : "flat") +
        (stale ? chip("待重算", "warn") : "") + "</td>" +
      '<td class="num">' + (pnl["现价"] == null ? "—" : fmt(pnl["现价"], 3)) + "</td>" +
      '<td class="num ' + kindCls(pnl["合计_元"]) + '">' + fmtMoney(pnl["合计_元"], 0) + "</td>" +
      '<td class="num ' + kindCls(pnl["收益率_pct"]) + '">' + fmt(pnl["收益率_pct"], 2) + "%</td>" +
      '<td class="wrap muted">' + esc(c["下一步"] || "") + "</td>" +
      "<td>" + marks.slice(0, 2).map(m =>
        chip(m["类型"], m["级别"] === "bad" ? "bad" : (m["级别"] === "ok" ? "ok" : "warn"))).join("") + "</td>" +
      "<td><button class='btn sm ghost' data-flow-open='" + esc(c["流编号"]) +
        "' data-code='" + esc((c["标的"] || {})["代码"] || "") + "'>详情</button></td></tr>";
  });
  box.innerHTML = html + "</tbody></table></div>";
  $$("#console-flow [data-flow-open]").forEach(b => b.addEventListener("click", () => {
    const target = viewApi("flow");
    if (target.openDetail) target.openDetail(b.dataset.flowOpen, false, b.dataset.code);
    showView("flow");
  }));
}


function kindCls(v) {
  const n = num(v);
  if (n === null) return "";
  return n > 0 ? "up" : (n < 0 ? "down" : "");
}


function renderPickTable() {
  const box = $("#console-pick");
  const more = $("#console-pick-more");
  if (!box) return;
  const rows = pickFilteredRows();
  const total = ((pickUI.data || {})["行"] || []).length;
  if (!rows.length) {
    box.innerHTML = '<div class="empty">当前筛选下没有候选。</div>';
    if (more) more.textContent = "筛选后 0 只 / 共 " + total + " 只";
    return;
  }
  const show = rows.slice(0, PICK_TOP);
  box.innerHTML = '<div class="table-wrap"><table class="tbl"><thead><tr>' +
    "<th>#</th><th>标的</th><th>模块</th><th>来源板块</th>" +
    '<th class="num">现价</th><th class="num">涨跌幅</th><th class="num">机械分</th>' +
    '<th class="num">推荐度</th><th>机械评级</th><th></th></tr></thead><tbody>' +
    show.map((r, i) => pickRankRow(r, i)).join("") + "</tbody></table></div>";
  if (more) {
    more.textContent = "榜单前 " + show.length + " 只" + (total > PICK_TOP
      ? "（短/波/中/长合并候选 " + total + " 只" +
        (rows.length === total ? "" : "，当前筛选 " + rows.length + " 只") + "）" : "");
  }
  bindPickRank(box);
}

export function renderPickRank(rank) {
  pickUI.data = rank || null;
  const rows = ((rank || {})["行"]) || [];
  const prod = (rank || {})["产物"];
  const facets = ((rank || {})["模块"]) || [];
  const sum = $("#console-pick-sum");
  const hints = $("#console-pick-hints");
  const src = $("#console-pick-src");
  const more = $("#console-pick-more");
  const have = facets.filter(f => (f["候选数"] || 0) > 0).map(f => f["名称"]);
  const keep = Array.from(pickUI.mods).filter(m => have.indexOf(m) >= 0);
  pickUI.mods = new Set(rows.length ? (keep.length ? keep : have) : []);
  pickModuleButtons(facets);

  if (!rows.length) {
    if (sum) sum.textContent = "";
    if (src) src.textContent = "";
    if (more) more.textContent = "";
    const box = $("#console-pick");
    if (box) box.innerHTML = '<div class="empty">还没有荐股结果。点右上「一键去跑荐股」跳荐股页选板块跑一次（1 次模型调用）。</div>';
  } else {
    if (sum) {
      sum.textContent = Math.min(rows.length, PICK_TOP) + " 只（短/波/中/长合并）" +
        (prod ? " · 产物 " + (prod["产物时间"] || prod["生成时间"] || "") : "");
    }
    if (src) {
      const live = rows.filter(r => r["价格来源"] === "东财实时行情").length;
      src.textContent = live ? "实时价 " + live + "/" + rows.length + " 只"
        : "产物快照价（点「刷新实盘价」取实时）";
    }
    renderPickTable();
  }
  if (hints) {
    const list = (((rank || {})["提示"]) || []).slice();
    if ((rank || {})["口径"]) list.push(rank["口径"]);
    hints.innerHTML = list.map(h => "· " + esc(h)).join("<br>");
  }
}

/* 一键去跑：把榜单勾选的模块 + 产物当时的行业/概念筛选交给荐股页预填。 */
function pickGoto() {
  const rank = pickUI.data || {};
  const prod = rank["产物"] || {};
  const saved = prod["筛选"] || {};
  const mods = pickUI.mods.size ? Array.from(pickUI.mods) : (prod["模块"] || []);
  const industry = (saved["行业"] || []).map(x => ({
    名称: x["名称"], 细分: Array.isArray(x["细分"]) ? x["细分"] : [],
  })).filter(x => x["名称"]);
  const concepts = (saved["概念"] || []).map(x => x["名称"] || x).filter(Boolean);
  const api2 = viewApi("pick");
  const done = api2.prefill ? api2.prefill({ 模块: mods, 行业: industry, 概念: concepts }) : null;
  showView("pick");
  if (done && done.then) {
    done.then(r => {
      if (r) toast("已按榜单筛选预填（行业 " + r["行业"] + " 个 / 概念 " + r["概念"] + " 个），点「开始荐股」即可", "ok");
      else toast("已切到荐股页，请勾选要跑的板块", "warn");
    }).catch(() => {});
  }
}

export async function loadConsole(refresh) {
  const body = $("#console-body");
  if (body && !hasOverview) body.innerHTML = '<div class="muted">加载中 …</div>';
  try {
    const [ov, st, mk, tr] = await Promise.all([
      api("/api/overview" + (refresh ? "?refresh=1" : "")),
      api("/api/state"), api("/api/market"), api("/api/trash"),
    ]);
    State.state = st;
    renderOverview(ov);
    renderPickRank(ov["荐股"]);
    await loadAlerts();                 // 消息队列（服务端落盘的那份）
    if (body) body.innerHTML = renderSystem(st, mk, tr);
    const at = $("#console-at");
    if (at) at.textContent = "更新于 " + new Date().toLocaleTimeString();
    return true;
  } catch (e) {
    if (body) body.innerHTML = '<div class="fail">读取状态失败：' + esc(e.message) + "</div>";
    const hints = $("#console-hints");
    if (hints) hints.innerHTML = '<div class="fail">刷新失败：' + esc(e.message) + "（会自动退避重试）</div>";
    return false;
  }
}

export function initConsoleView() {
  const pref = loadPref();
  loadReadAt();
  const sel = $("#console-interval");
  if (sel) {
    sel.innerHTML = INTERVALS.map(i =>
      '<option value="' + i.ms + '"' + (i.ms === pref.ms ? " selected" : "") + ">" + i.label + "</option>").join("");
  }
  const auto = $("#console-auto");
  if (auto) auto.checked = pref.enabled;
  poller = createPoller({ run: () => loadConsole(true), onState: renderPollerState });
  poller.setIntervalMs(pref.ms);
  poller.setEnabled(pref.enabled);
  if (auto) {
    auto.addEventListener("change", () => {
      poller.setEnabled(auto.checked);
      savePref({ enabled: auto.checked, ms: poller.state.ms });
    });
  }
  if (sel) {
    sel.addEventListener("change", () => {
      poller.setIntervalMs(Number(sel.value));
      savePref({ enabled: poller.state.enabled, ms: poller.state.ms });
    });
  }
  const quotes = $("#btn-console-quotes");
  if (quotes) quotes.addEventListener("click", async () => { if (await loadConsole(true)) toast("已刷新实盘价", "ok"); });
  const flowGo = $("#btn-flow-goto");
  if (flowGo) flowGo.addEventListener("click", () => showView("flow"));
  const reload = $("#btn-console-reload");
  if (reload) reload.addEventListener("click", async () => { if (await loadConsole(false)) toast("已刷新", "ok"); });
  const nudgeBtn = $("#btn-console-nudge");
  if (nudgeBtn) {
    nudgeBtn.addEventListener("click", () => {
      const fast = INTERVALS[0].ms;
      poller.setIntervalMs(fast);
      if (sel) sel.value = String(fast);
      if (auto) auto.checked = true;
      poller.setEnabled(true);
      savePref({ enabled: true, ms: fast });
      toast("已按 " + INTERVALS[0].label + "开启自动刷新", "ok");
    });
  }
  const clear = $("#btn-alert-clear");
  if (clear) {
    clear.addEventListener("click", () => {
      confirmModal("清空消息队列",
        "将删除 data/ai/alerts.jsonl 里的全部到价消息（不可恢复）。确定清空？",
        async () => {
          try {
            await api("/api/alerts/clear", { method: "POST", body: "{}" });
            queue = [];
            renderQueue();
            toast("消息队列已清空", "ok");
          } catch (e) { toast(e.message, "bad"); }
        }, "清空");
    });
  }
  const bell = $("#btn-alerts");
  if (bell) bell.addEventListener("click", () => openAlertDrawer());

  /* 荐股榜单：模块按钮多选切换（产物没跑的模块置灰）+ 去跑荐股 */
  const modBox = $("#console-pick-mods");
  if (modBox) modBox.addEventListener("click", e => {
    const btn = e.target.closest("[data-rk-mod]");
    if (!btn || btn.disabled) return;
    const mod = btn.dataset.rkMod;
    if (pickUI.mods.has(mod)) {
      if (pickUI.mods.size <= 1) { toast("至少保留一个模块", "warn"); return; }
      pickUI.mods.delete(mod);
      btn.classList.remove("on");
    } else {
      pickUI.mods.add(mod);
      btn.classList.add("on");
    }
    renderPickTable();
  });
  const pickGo = $("#btn-pick-goto");
  if (pickGo) pickGo.addEventListener("click", pickGoto);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && poller) poller.kick();
  });
  renderQueue();
}

/* 注册给 core/app.js：进入本页就自动刷一次实盘价。
   8 秒内反复进出只读本地（避免来回切页连续打行情接口）；自动刷新的档位定时器不受影响。 */
const ENTRY_GAP_MS = 8000;
let lastEntryAt = 0;
registerView("console", {
  onShow: () => {
    const now = Date.now();
    const live = now - lastEntryAt >= ENTRY_GAP_MS;
    lastEntryAt = now;
    return loadConsole(live);
  },
});
