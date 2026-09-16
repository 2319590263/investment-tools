/* 持仓 / 账户页。 */
import { api } from "../core/api.js";
import { refreshState, registerView } from "../core/app.js";
import { confirmMobileWrite, isMobileShell } from "../core/mobile.js";
import { $, $$, esc, fmt, fmtMoney, fmtPct, kindClass, num, toast } from "../core/util.js";
import { closeModal, openModal } from "../ui/modal.js";

const Sync = { jobId: null, timer: null, from: 0, running: false, last: "", captcha: null, logCount: 0 };
let loadedEditorText = "";

export async function loadHoldings() {
  const h = await api("/api/holdings");
  renderHoldings(h);
  const a = await api("/api/account");
  renderAccount(a);
  await loadLedger();
}

/* 交易明细（批注 4）：只读交易台账，按标的筛选。 */
export async function loadLedger() {
  const table = $("#ledger-table");
  if (!table) return;
  const sel = $("#ledger-code");
  const code = (sel && sel.value) || "";
  try {
    const d = await api("/api/ledger?limit=300" + (code ? "&code=" + encodeURIComponent(code) : ""));
    const opts = (d["标的"] || []).map(x =>
      '<option value="' + esc(x["代码"]) + '"' + (x["代码"] === code ? " selected" : "") + ">" +
      esc((x["名称"] || x["代码"]) + "（" + x["笔数"] + " 笔）") + "</option>").join("");
    if (sel) sel.innerHTML = '<option value="">全部标的</option>' + opts;
    const cover = d["覆盖"] ? ("覆盖 " + d["覆盖"]["起"] + " ~ " + d["覆盖"]["止"] + " ｜ ") : "";
    $("#ledger-meta").textContent = d["存在"]
      ? (cover + "共 " + d["总数"] + " 笔 ｜ " + d["标的数"] + " 只标的 ｜ 台账更新 " +
         (d["更新时间"] || "—") + " ｜ " + d["台账"])
      : ("还没有台账文件：" + d["台账"] +
         "（点右上「同步同花顺」拉近一周成交，自动去重）");
    const rows = d["行"] || [];
    table.innerHTML = "<thead><tr><th>日期</th><th>时间</th><th>代码</th><th>名称</th>" +
      "<th>方向</th><th class='num'>价格</th><th class='num'>数量</th><th class='num'>金额</th>" +
      "<th>市场</th></tr></thead><tbody>" +
      (rows.length ? rows.map(l => "<tr>" +
        "<td>" + esc(l["日期"] || "—") + "</td><td>" + esc(l["时间"] || "") + "</td>" +
        '<td class="mono">' + esc(l["代码"] || "") + "</td><td>" + esc(l["名称"] || "") + "</td>" +
        "<td>" + (l["方向"] === "卖出" ? '<span class="down">卖出</span>'
                                        : '<span class="up">买入</span>') + "</td>" +
        '<td class="num">' + fmt(l["价格"], 3) + "</td>" +
        '<td class="num">' + fmt(l["数量"], 0) + "</td>" +
        '<td class="num">' + fmtMoney(l["金额"], 2) + "</td>" +
        '<td class="muted">' + esc(l["市场"] || "") + "</td></tr>").join("")
        : '<tr><td class="empty" colspan="9">' + esc(d["口径"] || "") + "</td></tr>") +
      "</tbody>";
    if (d["显示"] < d["总数"]) {
      $("#ledger-meta").textContent += " ｜ 只显示最近 " + d["显示"] + " 笔";
    }
  } catch (e) {
    table.innerHTML = '<tbody><tr><td class="empty" colspan="9">读取台账失败：' +
      esc(e.message) + "</td></tr></tbody>";
  }
}

export function renderHoldings(h) {
  const s = h["汇总"] || {};
  $("#hold-path").textContent = h["路径"] || "";
  $("#hold-summary").innerHTML = [
    ["总资金", fmtMoney(s["总资金"], 0), "元"],
    ["持仓市值", fmtMoney(s["持仓市值_元"]), "元"],
    ["持仓占比", s["持仓占比_pct"] == null ? "—" : s["持仓占比_pct"] + "%", "含全部标的"],
    ["可估算现金", fmtMoney(s["现金_元"]), s["现金占比_pct"] == null ? "" : s["现金占比_pct"] + "%"],
    ["标的数", s["标的数"], ""],
  ].map(x => '<div class="stat"><div class="k">' + x[0] + '</div><div class="v">' + x[1] +
    '</div><div class="s">' + x[2] + "</div></div>").join("");

  const rows = h["持仓"] || [];
  let html = "<thead><tr><th>代码</th><th>名称</th><th class='num'>余额</th><th class='num'>可用</th><th class='num'>冻结</th>" +
    "<th class='num'>成本</th><th class='num'>现价</th><th class='num'>市值</th><th class='num'>盈亏</th><th class='num'>盈亏%</th>" +
    "<th class='num'>占总资金</th><th class='num'>回本需涨</th></tr></thead><tbody>";
  if (!rows.length) html += '<tr><td colspan="12" class="empty">持仓文件里没有解析出标的</td></tr>';
  rows.forEach(r => {
    html += "<tr>" +
      "<td>" + esc(r["代码"] || "") + "</td>" +
      "<td>" + esc(r["名称"] || "") + (r["是否ETF"] ? ' <span class="badge flat">ETF</span>' : "") + "</td>" +
      '<td class="num">' + (r["持有股数"] == null ? "—" : fmt(r["持有股数"], 0)) + "</td>" +
      '<td class="num">' + (r["可用股数_可卖"] == null ? "—" : fmt(r["可用股数_可卖"], 0)) + "</td>" +
      '<td class="num ' + (num(r["冻结股数_当日买入不可卖"]) ? "warn" : "") + '">' +
      (r["冻结股数_当日买入不可卖"] == null ? "—" : fmt(r["冻结股数_当日买入不可卖"], 0)) + "</td>" +
      '<td class="num">' + fmt(r["成本价"], 3) + "</td>" +
      '<td class="num">' + fmt(r["现价"], 3) + "</td>" +
      '<td class="num">' + fmtMoney(r["持仓市值_元"]) + "</td>" +
      '<td class="num ' + kindClass(r["浮动盈亏_元"]) + '">' + fmtMoney(r["浮动盈亏_元"]) + "</td>" +
      '<td class="num ' + kindClass(r["盈亏比例_pct"]) + '">' + fmtPct(r["盈亏比例_pct"]) + "</td>" +
      '<td class="num">' + (r["占总资金_pct"] == null ? "—" : r["占总资金_pct"] + "%") + "</td>" +
      '<td class="num muted">' + (r["回本需涨_pct"] == null ? "—" : fmtPct(r["回本需涨_pct"])) + "</td>" +
      "</tr>";
  });
  $("#hold-table").innerHTML = html + "</tbody>";
  $("#hold-editor").value = h["原文"] || "";
  loadedEditorText = $("#hold-editor").value;
}

export function renderAccount(a) {
  const cfg = a["配置"] || {};
  const fields = a["模板字段"] || [];
  const hints = a["字段说明"] || {};
  $("#account-form").innerHTML = fields.map(k => {
    const v = cfg[k];
    const isKey = k === "api_key";
    const type = (typeof v === "number") ? "number" : "text";
    const isEtf = k.indexOf("ETF_") === 0 || k.indexOf("ETC_") === 0;
    return '<div class="field"><label>' + esc(k) +
      (isEtf ? ' <span class="chip accent">ETF</span>' : "") + "</label>" +
      '<input data-acc="' + esc(k) + '" type="' + (isKey ? "password" : type) + '" value="' +
      esc(v === undefined || v === null ? "" : v) + '"' + (typeof v === "number" ? ' step="any"' : "") + " />" +
      (hints[k] ? '<div class="hint">' + esc(hints[k]) + "</div>" : "") + "</div>";
  }).join("");
  $("#account-msg").textContent = a["解析错误"] ? "解析错误：" + a["解析错误"] : "";
  $("#account-msg").className = a["解析错误"] ? "err" : "";
}

async function persistAccount(cfg) {
  const res = await api("/api/account", { method: "POST", body: JSON.stringify({ text: JSON.stringify(cfg, null, 1) }) });
  if (res.ok) {
    toast(res["已写入"] ? "账户配置已保存（原文件备份为 .bak）" : "账户配置无变化，未写入", res["已写入"] ? "ok" : "warn");
    renderAccount(res["配置"]); refreshState();
  }
  else { toast(res.error, "bad"); $("#account-msg").textContent = res.error; }
}

export async function saveAccount() {
  let cfg = {};
  try {
    const cur = await api("/api/account");
    // 以「文件原文」为基准覆盖，避免把模板默认值（如 ETF_*）写进用户文件、
    // 也避免丢掉表单未展示的字段（如用户实际在用的 ETC_* 前缀）。
    cfg = JSON.parse(cur["原文"] || "{}");
    if (!cfg || typeof cfg !== "object" || Array.isArray(cfg)) throw new Error("bad");
  } catch (e) {
    cfg = {};
  }
  const changes = [];
  $$("#account-form [data-acc]").forEach(inp => {
    const k = inp.dataset.acc;
    const oldValue = cfg[k];
    const v = inp.value.trim();
    if (k.endsWith("_pct") || k === "总资金" || k === "佣金最低_元" || k === "最大加仓次数") {
      const n = num(v);
      cfg[k] = n === null ? v : n;
    } else cfg[k] = v;
    if (String(oldValue == null ? "" : oldValue) !== String(cfg[k] == null ? "" : cfg[k])) changes.push(k);
  });
  if (!isMobileShell()) return persistAccount(cfg);
  const detail = changes.length
    ? "检测到 " + changes.length + " 项字段变化：" + changes.slice(0, 6).join("、") +
      (changes.length > 6 ? " 等" : "") + "。"
    : "未检测到字段变化。";
  confirmMobileWrite("确认保存账户配置",
    detail + "将写入 config/账户配置.json；服务端会在写入前自动保留 .bak 备份。",
    () => persistAccount(cfg));
}

export async function saveHoldings() {
  const res = await api("/api/holdings", { method: "POST", body: JSON.stringify({ text: $("#hold-editor").value }) });
  if (res.ok) {
    toast(res["已写入"] ? "持仓文件已保存" : "持仓文件无变化，未写入", res["已写入"] ? "ok" : "warn");
    renderHoldings(res["持仓"]);
    $("#hold-msg").textContent = res["已写入"] ? "已保存，原文件备份为 持仓数据.md.bak" : "内容与文件一致，未写入、未产生备份";
    $("#hold-msg").className = res["已写入"] ? "ok-msg" : "";
    refreshState();
  } else {
    $("#hold-msg").textContent = res.error; $("#hold-msg").className = "err";
  }
}

function setSyncRunning(on) {
  Sync.running = !!on;
  $("#btn-hold-sync").disabled = Sync.running;
  $("#btn-hold-sync-stop").hidden = !Sync.running;
  $("#btn-hold-sync").textContent = Sync.running ? "同步中…" : "同步同花顺";
}

function syncStatus(text, cls) {
  const el = $("#hold-sync-state");
  el.textContent = text || "";
  el.className = cls || "muted";
}

function syncFailure(job) {
  const lines = (job.lines || []).map(x => (x.text || "").trim()).filter(Boolean);
  return lines.reverse().find(x => x.indexOf("[FAIL]") === 0) ||
    Sync.last || (job.result && job.result["诊断"]) || "同步失败";
}

function syncLogClear(message) {
  const pre = $("#hold-sync-log");
  pre.innerHTML = '<span class="l-dim">' + esc(message || "等待开始 …") + "</span>";
  Sync.logCount = 0;
  $("#hold-sync-log-summary").textContent = message || "等待同步";
}

function syncLogAppend(line) {
  const pre = $("#hold-sync-log");
  if (Sync.logCount === 0) pre.innerHTML = "";
  const text = (line.text || "").trim();
  if (!text) return;
  const row = document.createElement("div");
  const time = document.createElement("span");
  time.className = "l-ts";
  time.textContent = line.t || "";
  const body = document.createElement("span");
  if (text.startsWith("[OK]")) body.className = "l-ok";
  else if (text.startsWith("[WARN]")) body.className = "l-warn";
  else if (text.startsWith("[FAIL]")) body.className = "l-fail";
  else if (text.startsWith("[..]")) body.className = "l-stage";
  else if (text.startsWith("[CAPTCHA]")) body.className = "l-warn";
  else body.className = "l-dim";
  body.textContent = text;
  row.appendChild(time);
  row.appendChild(body);
  pre.appendChild(row);
  pre.scrollTop = pre.scrollHeight;
  Sync.logCount += 1;
  $("#hold-sync-log-summary").textContent = Sync.running ? ("运行中 · " + Sync.logCount + " 行") : (Sync.logCount + " 行日志");
}

function setSyncLogOpen(open) {
  $("#hold-sync-log-card").open = !!open;
}
function setCaptcha(info) {
  Sync.captcha = info || null;
  const button = $("#btn-hold-captcha");
  button.hidden = !Sync.captcha;
  button.disabled = !Sync.captcha;
}

function showCaptchaModal(info) {
  if (!info || !info.id) return;
  const url = "/api/holdings/captcha?id=" + encodeURIComponent(info.id) + "&t=" + Date.now();
  openModal("同花顺复制验证码", '<div class="note">同花顺要求人工填写验证码。图片只保存在本机，提交后才能继续同步。</div>' +
    '<div style="text-align:center;margin:12px 0"><img src="' + esc(url) +
    '" alt="同花顺验证码" style="max-width:100%;border:1px solid var(--line);border-radius:8px" /></div>' +
    '<div class="row"><input id="hold-captcha-code" maxlength="12" autocomplete="off" placeholder="输入图片中的验证码" />' +
    '<button class="btn primary" id="hold-captcha-submit">提交验证码</button></div>' +
    '<div class="err" id="hold-captcha-msg"></div>');
  $("#hold-captcha-code").focus();
  $("#hold-captcha-submit").addEventListener("click", async () => {
    const code = $("#hold-captcha-code").value.trim();
    if (!code) { $("#hold-captcha-msg").textContent = "请先输入验证码"; return; }
    $("#hold-captcha-submit").disabled = true;
    try {
      const res = await api("/api/holdings/captcha", {
        method: "POST", body: JSON.stringify({ id: info.id, code }),
      });
      if (!res.ok) throw new Error(res.error || "提交失败");
      toast("验证码已提交，等待同花顺确认", "ok");
      setCaptcha(null);
      closeModal();
      syncStatus("验证码已提交，正在确认…", "muted");
    } catch (e) {
      $("#hold-captcha-msg").textContent = e.message;
      $("#hold-captcha-submit").disabled = false;
    }
  });
  $("#hold-captcha-code").addEventListener("keydown", e => {
    if (e.key === "Enter") $("#hold-captcha-submit").click();
  });
}

function handleSyncLine(text) {
  if (!text.startsWith("__HOLDINGS_CAPTCHA__ ")) return;
  try {
    const info = JSON.parse(text.slice("__HOLDINGS_CAPTCHA__ ".length));
    setCaptcha(info);
    showCaptchaModal(info);
  } catch (e) {
    syncStatus("验证码消息解析失败", "err");
  }
}
export async function pollHoldingsSync() {
  if (!Sync.jobId) return;
  try {
    const j = await api("/api/jobs/" + Sync.jobId + "?from=" + Sync.from);
    (j.lines || []).forEach(line => {
      const text = (line.text || "").trim();
      handleSyncLine(text);
      if (!text.startsWith("__HOLDINGS_CAPTCHA__ ")) syncLogAppend(line);
      if (text.indexOf("[FAIL]") === 0 || text.indexOf("[WARN]") === 0 || text.indexOf("[CAPTCHA]") === 0) {
        Sync.last = text;
      }
    });
    Sync.from = j.next;
    if (j.status === "running") {
      syncStatus((Sync.last || "正在读取同花顺…") + " · " + j.elapsed + "s", "muted");
      clearTimeout(Sync.timer);
      Sync.timer = setTimeout(pollHoldingsSync, 800);
      return;
    }
    setSyncRunning(false);
    setCaptcha(null);
    setSyncLogOpen(false);
    if (j.status === "canceled") {
      syncStatus("已中断，未继续等待写入", "warn");
      toast("同花顺同步已中断", "warn");
    } else if (j.exit_code === 0) {
      syncStatus("已同步 · " + j.elapsed + "s", "ok-msg");
      toast("同花顺持仓同步完成", "ok");
      await loadHoldings();
      refreshState();
    } else {
      const message = syncFailure(j);
      syncStatus(message, "err");
      $("#hold-msg").textContent = message;
      $("#hold-msg").className = "err";
      toast("同花顺同步失败", "bad");
    }
  } catch (e) {
    clearTimeout(Sync.timer);
    Sync.timer = setTimeout(pollHoldingsSync, 1500);
  }
}

export async function startHoldingsSync() {
  if (Sync.running) return;
  if ($("#hold-editor").value !== loadedEditorText &&
      !window.confirm("持仓编辑器里有未保存修改，同步会覆盖这些修改。是否继续？")) return;
  Sync.last = "";
  syncLogClear("正在启动同花顺同步…");
  setSyncLogOpen(true);
  syncStatus("正在启动…", "muted");
  setSyncRunning(true);
  try {
    const res = await api("/api/jobs", { method: "POST", body: JSON.stringify({ kind: "holdings_sync" }) });
    if (!res.ok) throw new Error(res.error || "启动失败");
    Sync.jobId = res.id;
    Sync.from = 0;
    pollHoldingsSync();
  } catch (e) {
    setSyncRunning(false);
    setSyncLogOpen(false);
    syncStatus(e.message, "err");
    toast(e.message, "bad");
  }
}

export async function stopHoldingsSync() {
  if (!Sync.jobId) return;
  await api("/api/jobs/" + Sync.jobId + "/cancel", { method: "POST", body: "{}" });
  syncLogAppend({ t: "", text: "[WARN] 已请求中断同步" });
  syncStatus("正在中断…", "warn");
}

export function initHoldingsView() {
  $("#btn-account-save").addEventListener("click", saveAccount);
  const ledgerBtn = $("#btn-ledger-reload");
  if (ledgerBtn) ledgerBtn.addEventListener("click", loadLedger);
  const ledgerSel = $("#ledger-code");
  if (ledgerSel) ledgerSel.addEventListener("change", loadLedger);
  $("#btn-hold-save").addEventListener("click", saveHoldings);
  $("#btn-hold-sync").addEventListener("click", startHoldingsSync);
  $("#btn-hold-sync-stop").addEventListener("click", stopHoldingsSync);
  $("#btn-hold-captcha").addEventListener("click", () => { if (Sync.captcha) showCaptchaModal(Sync.captcha); });
  $("#btn-account-raw").addEventListener("click", async () => {
    const a = await api("/api/account");
    openModal("账户配置.json 原文", '<textarea class="editor tall" id="acc-raw">' + esc(a["原文"]) + "</textarea>" +
      '<div class="row" style="margin-top:10px"><button class="btn primary" id="acc-raw-save">保存</button>' +
      '<span class="muted">保存前校验 JSON 合法性</span></div>');
    const saveRaw = async () => {
      const res = await api("/api/account", { method: "POST", body: JSON.stringify({ text: $("#acc-raw").value }) });
      if (res.ok) { toast("已保存", "ok"); closeModal(); loadHoldings(); } else toast(res.error, "bad");
    };
    $("#acc-raw-save").addEventListener("click", () => {
      const text = $("#acc-raw").value;
      const saveCurrent = async () => {
        const res = await api("/api/account", { method: "POST", body: JSON.stringify({ text: text }) });
        if (res.ok) { toast("已保存", "ok"); closeModal(); loadHoldings(); } else toast(res.error, "bad");
      };
      if (!isMobileShell()) return saveRaw();
      confirmMobileWrite("确认保存账户配置",
        "将以原文覆盖 config/账户配置.json；服务端会在写入前自动保留 .bak 备份。",
        saveCurrent);
    });
  });
}

/* 注册给 core/app.js：切到本页时按需加载 / 对外暴露的动作。 */
registerView("holdings", { onShow: loadHoldings });
