/* 交易流页：一条流从建仓盯到清仓（人工体检 + 盘后重算），下方保留「跟踪清单 + 计划生成」。
 *
 * 设计要点：
 *   · 盯盘只是「按档位重新取值」：一次批量报价覆盖所有在跑的流，机械判定盈亏 / 接近带 /
 *     达标止损，写消息队列；**不调模型**（模型只在你点「重算计划 / 体检」时跑）。
 *   · 盘中不改计划：成交只重算持仓与盈亏，计划要重算得你点。
 *   · 页面切后台自动暂停，非交易时段不发请求（与总控台同一套 poller 口径与档位记忆）。
 */
import { api } from "../core/api.js";
import { createPoller, INTERVALS, loadPref, savePref } from "../core/poller.js";
import { openReport, registerView } from "../core/app.js";
import { $, $$, esc, toast } from "../core/util.js";
import { confirmModal, openModal } from "../ui/modal.js";
import { flowCardsHtml, flowDetailHtml, flowFormHtml } from "../ui/flowcards.js";
import { trackDetailHtml, trackListHtml } from "../ui/trackcards.js";

export const Flow = {list: null, detail: null, fid: null, settings: null, day: "",
                    jobId: null, timer: null, from: 0, running: false, runningKind: "",
                    poller: null, checked: null, lastJobResult: null};

export const Track = {data: null, detail: null, code: null};


/* ---------------- 日志与状态 ---------------- */

function logAppend(line) {
  const pre = $("#flow-console");
  if (!pre) return;
  if (pre.dataset.clean !== "1") { pre.innerHTML = ""; pre.dataset.clean = "1"; }
  const t = String((line && line.text) || "");
  let cls = "";
  if (t.startsWith("[OK]")) cls = "l-ok";
  else if (t.startsWith("[WARN]")) cls = "l-warn";
  else if (t.startsWith("[FAIL]")) cls = "l-fail";
  else if (t.startsWith("[..]")) cls = "l-stage";
  else if (t.startsWith("    ")) cls = "l-dim";
  const span = document.createElement("div");
  span.innerHTML = '<span class="l-ts">' + esc((line && line.t) || "") + "</span>" +
    '<span class="' + cls + '">' + esc(t) + "</span>";
  pre.appendChild(span);
  pre.scrollTop = pre.scrollHeight;
}

function logClear(msg) {
  const pre = $("#flow-console");
  if (!pre) return;
  pre.dataset.clean = "1";
  pre.innerHTML = msg ? '<span class="l-dim">' + esc(msg) + "</span>" : "";
}

function setRunning(on, kind) {
  Flow.running = on;
  Flow.runningKind = on ? kind : "";
  ["#btn-flow-create", "#btn-track-generate", "#btn-flow-plan", "#btn-flow-check",
   "#btn-flow-nudge"].forEach(sel => {
    const el = $(sel);
    if (el) el.disabled = on;
  });
  const stop = $("#btn-flow-stop");
  if (stop) stop.hidden = !on;
}


/* ---------------- 盯盘（页面轮询） ---------------- */

function renderPollState(st) {
  const box = $("#flow-poll-state");
  if (!box) return;
  if (!st.enabled) { box.textContent = "盯盘已关闭"; return; }
  if (st.pauseReason) { box.textContent = "已暂停：" + st.pauseReason; return; }
  const last = st.lastAt ? st.lastAt.toLocaleTimeString() : "—";
  const next = st.nextAt ? st.nextAt.toLocaleTimeString() : "—";
  box.textContent = "盯盘中 · 上次 " + last + " · 下次 " + next +
    (st.failures ? "（连续失败 " + st.failures + " 次，退避中）" : "");
}

export function initPoller() {
  const pref = loadPref();
  const sel = $("#flow-interval");
  if (sel) sel.innerHTML = INTERVALS.map(i =>
    '<option value="' + i.ms + '">' + i.label + "</option>").join("");
  const toggle = $("#btn-flow-poll");
  if (toggle) toggle.checked = pref.enabled;
  if (sel) sel.value = String(pref.ms);
  Flow.poller = createPoller({
    run: () => loadFlows(true).then(() => true).catch(() => false),
    onState: renderPollState,
  });
  if (toggle) toggle.addEventListener("change", () => {
    Flow.poller.setEnabled(toggle.checked);
    savePref({enabled: toggle.checked, ms: Number(sel.value || pref.ms)});
  });
  if (sel) sel.addEventListener("change", () => {
    Flow.poller.setIntervalMs(Number(sel.value));
    savePref({enabled: !!(toggle && toggle.checked), ms: Number(sel.value)});
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) Flow.poller.kick(); });
  Flow.poller.setEnabled(pref.enabled);
}


/* ---------------- 流列表 ---------------- */

function syncChecked(cards) {
  const codes = (cards || []).map(c => ((c["标的"] || {})["代码"] || "").slice(0, 6));
  if (Flow.checked === null) { Flow.checked = new Set(); return; }
  codes.forEach(c => { if (!c) return; });
}

export function renderFlows() {
  const data = Flow.list || {};
  Flow.settings = data["设置"] || Flow.settings;
  Flow.day = data["适用交易日"] || "";
  const cards = data["流"] || [];
  $("#flow-list").innerHTML = flowCardsHtml(cards);
  const closed = data["已结束"] || [];
  const closedBox = $("#flow-closed");
  if (closedBox) {
    closedBox.innerHTML = closed.length
      ? closed.map(c => '<div class="fl-closed" data-fid="' + esc(c["流编号"]) + '">' +
          stateText(c) + "</div>").join("")
      : '<span class="muted">还没有结束的流</span>';
  }
  const s = Flow.settings || {};
  const band = $("#flow-band");
  if (band) {
    const want = String(s["接近带_pct"] || 0.3);
    const has = Array.from(band.options).some(o => o.value === want);
    band.value = has ? want : "0.3";
  }
  const reGen = $("#flow-regen-time");
  if (reGen) reGen.value = s["盘后重算时间"] || "15:10";
  const auto = $("#flow-auto-catchup");
  if (auto) auto.checked = !!s["打开页面自动补跑"];
  const note = $("#flow-settings-note");
  if (note) {
    note.textContent = "接近带 " + (s["接近带_pct"] || 0.3) + "% ｜ 轮询 " +
      ((s["轮询间隔_秒"] || 30) + " 秒") + " ｜ 盘后重算 " + (s["盘后重算时间"] || "15:10") +
      " ｜ 已占用资金 " + (((data["资金"] || {})["已占用"] || 0).toLocaleString()) + " 元";
  }
  const nudge = $("#flow-nudge");
  const pending = data["待重算"] || [];
  if (pending.length) {
    nudge.hidden = false;
    $("#flow-nudge-text").textContent = "今日盘后还没重算计划：" + pending.join("、") +
      "（点右边一键重算；因为服务不常驻，不会无人值守自动跑）";
  } else {
    nudge.hidden = true;
  }
  setBadge(cards, pending);
  if (Flow.settings && Flow.settings["打开页面自动补跑"]) {
    pending.forEach(fid => { startFlowJob("flow_plan", {流编号: fid}); });
  }
  renderForm();
}

function stateText(c) {
  return "· " + esc((c["标的"] || {})["名称"] || c["流编号"]) + " " +
    esc(c["状态"] || "") + (c["结束原因"] ? "（" + esc(c["结束原因"]) + "）" : "");
}

function setBadge(cards, pending) {
  const el = $("#nav-flow-badge");
  if (!el) return;
  const hit = (cards || []).filter(c => (c["到价"] || []).some(m => m["级别"] === "bad"))
    .length;
  const n = (pending || []).length;
  if (hit) { el.textContent = "!"; el.hidden = false; el.title = hit + " 条流触及止损/风险位"; }
  else if (n) { el.textContent = String(n); el.hidden = false; el.title = n + " 条流待重算计划"; }
  else { el.hidden = true; el.textContent = ""; el.title = ""; }
}

function renderForm() {
  const box = $("#flow-new");
  if (!box || box.dataset.ready === "1") return;
  box.innerHTML = flowFormHtml("本流资金 = 这条流专门留给这只标的的钱；目标收益率与最大亏损都按它算。" +
    "开流后可以随时补录成交、体检、重算计划。");
  box.dataset.ready = "1";
  $("#btn-flow-create").addEventListener("click", createFlow);
}

export async function loadFlows(refresh) {
  try {
    const d = await api("/api/flows" + (refresh ? "?refresh=1" : ""));
    Flow.list = d;
    renderFlows();
    if (Flow.poller) {
      const s = d["时段"] || {};
      Flow.poller.setSession(s["名称"], s["是否交易日"]);
    }
    (d["刷新"] || {}).提示 && (d["刷新"].提示 || []).forEach(h => toast(h, "warn"));
    return d;
  } catch (e) {
    toast("读取交易流失败：" + e.message, "bad");
    throw e;
  }
}


/* ---------------- 开流 ---------------- */

export async function createFlow() {
  const code = ($("#flow-new-code").value || "").trim();
  const capital = $("#flow-new-capital").value;
  const target = $("#flow-new-target").value;
  const loss = $("#flow-new-loss").value;
  $("#flow-msg").textContent = "";
  if (!code) { $("#flow-msg").textContent = "先填 6 位证券代码"; return; }
  if (!capital || !target || !loss) {
    $("#flow-msg").textContent = "本流资金 / 目标收益率 / 最大亏损都要填";
    return;
  }
  const body = {code: code, name: ($("#flow-new-name").value || "").trim(),
                "本流资金": capital, "目标收益率_pct": target, "最大亏损_pct": loss,
                "最大加仓次数": $("#flow-new-adds").value || 2};
  if ($("#flow-new-bring").checked) {
    try {
      const hold = await api("/api/holdings");
      const row = (hold["持仓"] || []).find(r => String(r["代码"]) === code.replace(/[^0-9]/g, "").slice(0, 6));
      if (!row) { $("#flow-msg").textContent = "持仓文件里没有这只标的，去掉「按持仓文件带入」再开"; return; }
      body["起始持仓"] = {"股数": row["持有股数"], "成本价": row["成本价"],
                          "可用": row["可用股数_可卖"]};
    } catch (e) { $("#flow-msg").textContent = e.message; return; }
  }
  try {
    const res = await api("/api/flows", {method: "POST", body: JSON.stringify(body)});
    if (!res.ok) { $("#flow-msg").textContent = res.error; return; }
    toast("已开流：" + res["流编号"], "ok");
    $("#flow-new-code").value = ""; $("#flow-new-name").value = "";
    await loadFlows(false);
    openFlowDetail(res["流编号"], false);
    if ($("#flow-new-plan").checked) startFlowJob("flow_plan", {流编号: res["流编号"]});
  } catch (e) { $("#flow-msg").textContent = e.message; }
}


/* ---------------- 任务（重算计划 / 体检） ---------------- */

export async function startFlowJob(kind, extra) {
  if (Flow.running) { toast("已有任务在跑，等它结束", "warn"); return; }
  const body = Object.assign({kind: kind}, extra || {});
  logClear("正在启动 …");
  setRunning(true, kind);
  try {
    const res = await api("/api/jobs", {method: "POST", body: JSON.stringify(body)});
    if (!res.ok) throw new Error(res.error || "启动失败");
    Flow.jobId = res.id;
    Flow.from = 0;
    $("#flow-cmd").textContent = res["命令"] || "";
    $("#flow-status").className = "chip accent";
    $("#flow-status").innerHTML = '<span class="spinner"></span> 运行中';
    pollFlowJob();
  } catch (e) {
    toast(e.message, "bad");
    setRunning(false);
    $("#flow-status").className = "chip bad";
    $("#flow-status").textContent = "启动失败";
  }
}

export async function pollFlowJob() {
  if (!Flow.jobId) return;
  try {
    const j = await api("/api/jobs/" + Flow.jobId + "?from=" + Flow.from);
    (j.lines || []).forEach(logAppend);
    Flow.from = j.next;
    if (j.status === "running") {
      $("#flow-status").innerHTML = '<span class="spinner"></span> 运行中 ' + j.elapsed + "s";
      clearTimeout(Flow.timer);
      Flow.timer = setTimeout(pollFlowJob, 800);
      return;
    }
    const kind = Flow.runningKind;
    setRunning(false);
    const r = j.result || {};
    Flow.lastJobResult = r;
    if (j.status === "canceled") {
      $("#flow-status").className = "chip warn";
      $("#flow-status").textContent = "已中断";
      toast("已中断，本次没有写入结果", "warn");
    } else {
      $("#flow-status").className = "chip " + (r["error"] ? "warn" : "ok");
      $("#flow-status").textContent = kind === "flow_check"
        ? ("体检完成 · " + (r["结论"] || "—") + " · " + j.elapsed + "s")
        : ("计划完成 · " + (r["方向"] || "—") + " · " + j.elapsed + "s");
      if (kind === "flow_check") {
        toast("体检结论：" + (r["结论"] || "—") + (r["error"] ? "（模型失败：" + r["error"] + "）" : ""),
              r["error"] ? "bad" : "ok");
      } else if (r["error"]) {
        toast("计划生成失败：" + r["error"], "bad");
      } else {
        toast("计划已生成：" + (r["计划"] || ""), "ok");
      }
    }
    await loadFlows(false);
    if (Flow.fid) await openFlowDetail(Flow.fid, false);
  } catch (e) {
    setRunning(false);
    toast("轮询失败：" + e.message, "bad");
  }
}

export async function stopFlowJob() {
  if (!Flow.jobId) return;
  try { await api("/api/jobs/" + Flow.jobId + "/cancel", {method: "POST", body: "{}"}); }
  catch (e) { toast(e.message, "bad"); }
}


/* ---------------- 详情与成交 ---------------- */

export async function openFlowDetail(fid, refresh) {
  Flow.fid = fid;
  try {
    const d = await api("/api/flow?id=" + encodeURIComponent(fid) + (refresh ? "&refresh=1" : ""));
    Flow.detail = d;
    $("#flow-detail").innerHTML = flowDetailHtml(d);
    $("#flow-detail-head").textContent = ((d["卡"] || {})["标的"] || {})["名称"] || fid;
    bindDetail();
  } catch (e) {
    $("#flow-detail").innerHTML = '<div class="fail">读取详情失败：' + esc(e.message) + "</div>";
  }
}

function bindDetail() {
  $$("#flow-detail [data-delfill]").forEach(b => b.addEventListener("click", () => deleteFill(b.dataset.delfill)));
  const add = $("#btn-flow-fill");
  if (add) add.addEventListener("click", addFill);
  const sync = $("#btn-flow-sync");
  if (sync) sync.addEventListener("click", syncLedger);
  $$("#flow-detail [data-plan]").forEach(a => a.addEventListener("click", e => {
    e.preventDefault();
    openReport(a.dataset.plan);
  }));
}

export async function addFill() {
  const side = $("#flow-fill-side").value;
  const price = $("#flow-fill-price").value;
  const qty = $("#flow-fill-qty").value;
  if (!price || !qty) { toast("成交价与数量都要填", "bad"); return; }
  try {
    const res = await api("/api/flow/fill", {
      method: "POST",
      body: JSON.stringify({"流编号": Flow.fid, "方向": side, "价格": price, "数量": qty,
                            "日期": ($("#flow-fill-day").value || "").trim() || undefined,
                            "备注": ($("#flow-fill-note").value || "").trim()}),
    });
    if (!res.ok) { toast(res.error, "bad"); return; }
    toast("已记一笔：" + side + " " + qty + " 股", "ok");
    (res["提示"] || []).forEach(t => toast(t, "warn"));
    await openFlowDetail(Flow.fid, false);
    await loadFlows(false);
  } catch (e) { toast(e.message, "bad"); }
}

export async function deleteFill(seq) {
  confirmModal("删除这笔成交？", "删除后会按剩下的成交重算持仓与盈亏（不会改计划）。", async () => {
    try {
      const res = await api("/api/flow/fill/delete", {
        method: "POST", body: JSON.stringify({"流编号": Flow.fid, "序号": Number(seq)}),
      });
      if (!res.ok) { toast(res.error, "bad"); return; }
      toast("已删除成交 #" + seq, "ok");
      await openFlowDetail(Flow.fid, false);
      await loadFlows(false);
    } catch (e) { toast(e.message, "bad"); }
  }, "确认删除");
}

export async function syncLedger() {
  try {
    const res = await api("/api/flow/sync", {
      method: "POST", body: JSON.stringify({流编号: Flow.fid}),
    });
    if (!res.ok) { toast(res.error, "bad"); return; }
    toast(res["新增"] ? "从台账并入 " + res["新增"] + " 笔成交" : "台账里没有新成交", res["新增"] ? "ok" : "warn");
    await openFlowDetail(Flow.fid, false);
    await loadFlows(false);
  } catch (e) { toast(e.message, "bad"); }
}

export async function runCheck(fid, note, pre) {
  startFlowJob("flow_check", {"流编号": fid, "补充说明": note || "", "先抓": pre || ""});
}

export async function closeFlow(fid) {
  confirmModal("结束这条流？", "结束后不再盯盘、也不能再补录成交；已经记录的成交与盈亏都会保留。",
    async () => {
      try {
        const res = await api("/api/flow/close", {
          method: "POST", body: JSON.stringify({"流编号": fid, "原因": "人工结束"}),
        });
        if (!res.ok) { toast(res.error, "bad"); return; }
        toast("已结束 " + fid, "ok");
        await loadFlows(false);
        await openFlowDetail(fid, false);
      } catch (e) { toast(e.message, "bad"); }
    }, "确认结束");
}

export async function deleteFlow(fid) {
  confirmModal("删除这条流？", "整条流（含成交、体检、事件）会移入回收站，超过 7 天才真正删除。",
    async () => {
      try {
        const res = await api("/api/flow/delete", {
          method: "POST", body: JSON.stringify({流编号: fid}),
        });
        if (!res.ok) { toast(res.error, "bad"); return; }
        toast("已移入回收站", "ok");
        await loadFlows(false);
        if (Flow.fid === fid) { Flow.fid = null; $("#flow-detail").innerHTML = ""; }
      } catch (e) { toast(e.message, "bad"); }
    }, "确认删除");
}


/* ---------------- 跟踪清单（计划来源） ---------------- */

export function trackRender() {
  const d = Track.data || {};
  const table = $("#track-table");
  if (!table) return;
  if (Flow.checked === null) Flow.checked = new Set((d["清单"] || []).map(c => c["代码"]));
  table.innerHTML = trackListHtml(d["清单"] || [], Flow.checked);
  $("#track-path").textContent = d["路径"] || "";
  $("#track-day").textContent = "适用交易日 " + (d["适用交易日"] || "—");
}

export async function loadTrack(refresh) {
  try {
    Track.data = await api("/api/track/all" + (refresh ? "?refresh=1" : ""));
    trackRender();
  } catch (e) {
    toast("读取跟踪清单失败：" + e.message, "bad");
  }
}

export async function openTrackDetail(code, refresh) {
  Track.code = code;
  try {
    Track.detail = await api("/api/track?code=" + encodeURIComponent(code) + (refresh ? "&refresh=1" : ""));
    $("#track-detail").innerHTML = trackDetailHtml(Track.detail);
    $("#track-detail-head").textContent = (Track.detail["名称"] || code) + " · " +
      (Track.detail["适用交易日"] || "");
    const none = $("#btn-track-exec-none");
    if (none) none.addEventListener("click", () => {
      $$("#track-exec tbody tr[data-no] select[data-state]").forEach(s => { s.value = "未执行"; });
    });
    const save = $("#btn-track-exec-save");
    if (save) save.addEventListener("click", saveTrackExec);
    $$("#track-detail [data-plan]").forEach(a => a.addEventListener("click", e => {
      e.preventDefault(); openReport(a.dataset.plan);
    }));
    $$("#track-detail [data-del]").forEach(b => b.addEventListener("click", () => deleteTrackPlan(b.dataset.del)));
  } catch (e) {
    $("#track-detail").innerHTML = '<div class="fail">读取失败：' + esc(e.message) + "</div>";
  }
}

function collectExec() {
  return $$("#track-exec tbody tr[data-no]").map(tr => ({
    "编号": Number(tr.dataset.no),
    "执行状态": (tr.querySelector("select[data-state]") || {}).value || "",
    "成交价": (tr.querySelector("input[data-price]") || {}).value || null,
    "成交股数": (tr.querySelector("input[data-shares]") || {}).value || null,
    "备注": (tr.querySelector("input[data-note]") || {}).value || "",
  }));
}

export async function saveTrackExec() {
  if (!Track.detail || !Track.detail["最新"]) return;
  const note = $("#track-exec-note");
  try {
    const res = await api("/api/track/exec", {
      method: "POST",
      body: JSON.stringify({"计划路径": Track.detail["最新"]["json路径"],
                            "条目": collectExec(),
                            "总体备注": note ? note.value : ""}),
    });
    if (!res.ok) { toast(res.error, "bad"); return; }
    toast("执行记录已保存", "ok");
    await openTrackDetail(Track.code, false);
    await loadTrack(false);
  } catch (e) { toast("保存失败：" + e.message, "bad"); }
}

export async function addTrack() {
  const code = ($("#track-code").value || "").trim();
  if (!code) { $("#track-code").focus(); return; }
  $("#track-msg").textContent = "";
  try {
    const res = await api("/api/tracklist/add", {
      method: "POST",
      body: JSON.stringify({code: code, name: ($("#track-name").value || "").trim(),
                            note: ($("#track-note").value || "").trim()}),
    });
    if (!res.ok) { $("#track-msg").textContent = res.error; return; }
    toast("已加入跟踪：" + code, "ok");
    $("#track-code").value = ""; $("#track-name").value = ""; $("#track-note").value = "";
    await loadTrack(false);
  } catch (e) { $("#track-msg").textContent = e.message; }
}

export async function importWatchlist() {
  try {
    const res = await api("/api/tracklist/import-watchlist", {method: "POST", body: "{}"});
    if (!res.ok) { toast(res.error, "bad"); return; }
    toast(res["新增"] ? "已从自选股导入 " + res["新增"] + " 只" : "自选股里的标的都已在清单里",
          res["新增"] ? "ok" : "warn");
    await loadTrack(false);
  } catch (e) { toast(e.message, "bad"); }
}

export async function removeTrack(code) {
  confirmModal("从跟踪清单移除？", "只从清单里删掉，已生成的计划产物与交易流都会保留。", async () => {
    try {
      const res = await api("/api/tracklist/remove", {method: "POST", body: JSON.stringify({code: code})});
      if (!res.ok) { toast(res.error, "bad"); return; }
      toast("已移除 " + code, "ok");
      await loadTrack(false);
    } catch (e) { toast(e.message, "bad"); }
  }, "确认移除");
}

export async function deleteTrackPlan(path) {
  confirmModal("删除这份计划产物？", "会移入回收站，超过 7 天才真正删除。", async () => {
    try {
      const res = await api("/api/track/delete", {method: "POST", body: JSON.stringify({path: path})});
      if (!res.ok) { toast(res.error, "bad"); return; }
      toast("已移入回收站", "ok");
      if (Track.code) await openTrackDetail(Track.code, false);
      await loadTrack(false);
    } catch (e) { toast(e.message, "bad"); }
  }, "确认删除");
}


/* ---------------- 交互绑定 ---------------- */

export function initFlowView() {
  initPoller();
  renderForm();                               // 开流表单是静态的：进页面就渲染，绑定在 renderForm 里
  const band = $("#flow-band");
  if (band) band.addEventListener("change", async () => {
    try {
      const res = await api("/api/flow/settings", {
        method: "POST", body: JSON.stringify({"接近带_pct": Number(band.value)}),
      });
      Flow.settings = res["设置"];
      toast("接近带已设为 " + res["设置"]["接近带_pct"] + "%", "ok");
      await loadFlows(true);
    } catch (e) { toast(e.message, "bad"); }
  });
  const reGen = $("#flow-regen-time");
  if (reGen) reGen.addEventListener("change", async () => {
    try {
      const res = await api("/api/flow/settings", {
        method: "POST", body: JSON.stringify({"盘后重算时间": reGen.value}),
      });
      Flow.settings = res["设置"];
      toast("盘后重算时间已设为 " + res["设置"]["盘后重算时间"], "ok");
      await loadFlows(false);
    } catch (e) { toast(e.message, "bad"); }
  });
  const auto = $("#flow-auto-catchup");
  if (auto) auto.addEventListener("change", async () => {
    try {
      const res = await api("/api/flow/settings", {
        method: "POST", body: JSON.stringify({"打开页面自动补跑": auto.checked}),
      });
      Flow.settings = res["设置"];
      toast(auto.checked ? "已开启：打开本页时自动补跑盘后重算" : "已关闭自动补跑", "ok");
    } catch (e) { toast(e.message, "bad"); }
  });
  const nudge = $("#btn-flow-nudge");
  if (nudge) nudge.addEventListener("click", () => {
    const pending = (Flow.list && Flow.list["待重算"]) || [];
    pending.forEach(fid => startFlowJob("flow_plan", {流编号: fid}));
  });
  const list = $("#flow-list");
  if (list) list.addEventListener("click", e => {
    const card = e.target.closest(".flow-card");
    if (!card) return;
    const fid = card.dataset.fid;
    const act = e.target.dataset ? e.target.dataset.act : "";
    if (act === "detail") openFlowDetail(fid, true);
    else if (act === "plan") startFlowJob("flow_plan", {流编号: fid});
    else if (act === "close") closeFlow(fid);
    else if (act === "delete") deleteFlow(fid);
    else if (act === "sync") syncLedger();
    else if (act === "fill") {
      openFlowDetail(fid, false).then(() => {
        const box = $("#flow-fill-price");
        if (box) { box.focus(); box.scrollIntoView({block: "center"}); }
      });
    } else if (act === "check") openCheckDialog(fid);
  });
  const closedBox = $("#flow-closed");
  if (closedBox) closedBox.addEventListener("click", e => {
    const row = e.target.closest(".fl-closed");
    if (row) openFlowDetail(row.dataset.fid, false);
  });
  const stop = $("#btn-flow-stop");
  if (stop) stop.addEventListener("click", stopFlowJob);
  const plan = $("#btn-flow-plan");
  if (plan) plan.addEventListener("click", () => {
    if (Flow.fid) startFlowJob("flow_plan", {流编号: Flow.fid});
    else toast("先选一条流", "bad");
  });
  const check = $("#btn-flow-check");
  if (check) check.addEventListener("click", () => {
    if (Flow.fid) openCheckDialog(Flow.fid);
    else toast("先选一条流", "bad");
  });
  const addT = $("#btn-track-add");
  if (addT) addT.addEventListener("click", addTrack);
  ["#track-code", "#track-name", "#track-note"].forEach(sel => {
    const el = $(sel);
    if (el) el.addEventListener("keydown", e => { if (e.key === "Enter") addTrack(); });
  });
  const imp = $("#btn-track-import");
  if (imp) imp.addEventListener("click", importWatchlist);
  const quotes = $("#btn-track-quotes");
  if (quotes) quotes.addEventListener("click", () => loadTrack(true));
  const gen = $("#btn-track-generate");
  if (gen) gen.addEventListener("click", () => {
    const codes = ((Track.data && Track.data["清单"]) || []).map(c => c["代码"])
      .filter(c => Flow.checked.has(c));
    if (!codes.length) { toast("先勾选要生成计划的标的", "bad"); return; }
    startFlowJob("track", {codes: codes});
  });
  const table = $("#track-table");
  if (table) table.addEventListener("change", e => {
    const code = e.target && e.target.dataset ? e.target.dataset.check : null;
    if (!code) return;
    if (e.target.checked) Flow.checked.add(code); else Flow.checked.delete(code);
  });
  if (table) table.addEventListener("click", e => {
    const t = e.target;
    if (!t || !t.dataset) return;
    if (t.dataset.detail) { e.preventDefault(); openTrackDetail(t.dataset.detail, true); }
    else if (t.dataset.open) { e.preventDefault(); openTrackDetail(t.dataset.open, false); }
    else if (t.dataset.gen) { e.preventDefault(); startFlowJob("track", {codes: [t.dataset.gen]}); }
    else if (t.dataset.untrack) removeTrack(t.dataset.untrack);
  });
}


export function openCheckDialog(fid) {
  openModal("体检：排查意外情况",
    '<div class="field"><label>你看到的异常 <span class="muted">可选，例如「公告被立案」「董事长变更」</span></label>' +
    '<textarea class="editor" id="flow-check-note" style="min-height:70px"></textarea></div>' +
    '<div class="muted">体检会看：消息面（公告 / 风险公告 / 快讯 / 调研 / 榜单）+ 个股量价 + 板块与大盘 + 你的流内成交与盈亏。' +
    '数据太旧或缺失时，可以先抓一次数据（不花模型钱）。</div>' +
    '<div class="row" style="margin-top:10px;gap:8px;flex-wrap:wrap">' +
    '<button class="btn primary" id="flow-check-run">直接体检</button>' +
    '<button class="btn ghost" id="flow-check-pull">先抓三维数据再体检（较慢）</button>' +
    '<button class="btn ghost" id="flow-check-news">只抓消息面再体检（较快）</button></div>');
  const go = pre => {
    const note = ($("#flow-check-note") || {}).value || "";
    runCheck(fid, note, pre);
  };
  $("#flow-check-run").addEventListener("click", () => go(""));
  $("#flow-check-pull").addEventListener("click", () => go("pull"));
  $("#flow-check-news").addEventListener("click", () => go("news"));
}


/* 注册给 core/app.js：切到本页自动刷一次（拉一次批量报价 + 机械判定）。 */
registerView("flow", {
  onShow: async () => { await loadFlows(true); await loadTrack(false); },
  openDetail: openFlowDetail,
  refresh: loadFlows,
});
