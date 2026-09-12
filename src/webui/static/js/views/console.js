/* 总控台：持仓股 / 自选股卡片 + 到价提醒 + 自动刷新 + 系统状态。
 *
 * 数据来自 /api/overview（卡片、计划线、触发、分时、提醒）与既有的 /api/state、/api/market、
 * /api/trash（系统状态）。默认用本地口径价格，点「刷新实盘价」或打开自动刷新才联网取实时行情；
 * 所有刷新只改页面显示，不写盘。
 */
import { api } from "../core/api.js";
import { State, openReport, registerView, showView, viewApi } from "../core/app.js";
import { $, $$, esc, fmt, fmtMoney, num, toast } from "../core/util.js";
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
    const input = $("#code");
    if (input) input.value = code;
    viewApi("run").syncCmd();
    viewApi("run").checkReadiness();
    showView("run");
    toast("已把 " + code + " 填入运行页", "ok");
  },
  onKline: (code, card) => {
    const name = card.querySelector(".sc-h b");
    showKlineModal(code, name ? name.textContent.trim() : "");
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
