/* 交易流页：一条流从建仓盯到清仓（人工体检 + 盘后计算），下方保留「跟踪清单 + 计划生成」。
 *
 * 设计要点：
 *   · 盯盘只是「按档位重新取值」：一次批量报价覆盖所有在跑的流，机械判定盈亏 / 接近带 /
 *     达标止损，写消息队列；**不调模型**（模型只在你点「计算计划 / 体检」时跑）。
 *   · 盘中不改计划：成交只重算持仓与盈亏，计划要重算得你点。
 *   · 页面切后台自动暂停，非交易时段不发请求（与总控台同一套 poller 口径与档位记忆）。
 */
import { api } from "../core/api.js";
import { createPoller, INTERVALS, loadPref, savePref } from "../core/poller.js";
import { openReport, registerView } from "../core/app.js";
import { $, $$, esc, freshNote, toast } from "../core/util.js";
import { closeModal, confirmModal, openModal } from "../ui/modal.js";
import { closedLine, flowCardsHtml, flowDetailHtml, flowFormHtml, paramsFormHtml,
         flowPickHtml, targetFormHtml } from "../ui/flowcards.js";
import { bindFlowChart, drawFlowDays, drawFlowMinutes, DAY_BARS } from "../ui/flowchart.js";

export const Flow = {list: null, detail: null, fid: null, settings: null, day: "",
                    jobId: null, timer: null, from: 0, running: false, runningKind: "",
                    poller: null, lastJobResult: null,
                    chartMode: {}, dayBars: {},       // 行内图：每只标的记住「分时 / 日K」与根数
                    pickFlow: "", pickCode: ""};      // 「计划与体检」卡选中的流与标的



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
  ["#btn-flow-create", "#btn-flow-plan", "#btn-flow-check",
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

export function renderFlows() {
  const data = Flow.list || {};
  Flow.settings = data["设置"] || Flow.settings;
  const cards = data["流"] || [];
  $("#flow-list").innerHTML = flowCardsHtml(cards);
  const closed = data["已结束"] || [];
  const closedBox = $("#flow-closed");
  if (closedBox) {
    closedBox.innerHTML = closed.length
      ? closed.map(closedLine).join("")
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
      " ｜ 在跑的流资金 " + (((data["资金"] || {})["已占用"] || 0).toLocaleString()) +
      " 元 ｜ 流内分配 " + (((data["资金"] || {})["分配合计"] || 0).toLocaleString()) +
      " 元 ｜ 标的 " + (data["标的数"] || 0) + " 只";
  }
  const nudge = $("#flow-nudge");
  const pending = data["待重算"] || [];
  if (pending.length) {
    nudge.hidden = false;
    $("#flow-nudge-text").textContent = "今日盘后还没重算计划：" +
      pending.map(x => x["标签"] || x["流编号"]).join("；") +
      "（点右边一键重算；因为服务不常驻，不会无人值守自动跑）";
  } else {
    nudge.hidden = true;
  }
  setBadge(cards, pending);
  drawRowCharts(cards);
  renderPick();
  if (Flow.settings && Flow.settings["打开页面自动补跑"]) {
    pending.forEach(x => startFlowJob("flow_plan", {流编号: x["流编号"], 代码: x["代码"]}));
  }
  renderForm();
}

/* 「计划与体检」卡的选流 / 选标的（批注 2）：下拉跟着流列表刷新，选了就记住。 */
export function renderPick() {
  const box = $("#flow-pick");
  if (!box) return;
  const cards = ((Flow.list || {})["流"]) || [];
  if (!cards.some(c => c["流编号"] === Flow.pickFlow && c["状态"] === "进行中")) {
    const first = cards.find(c => c["状态"] === "进行中");
    Flow.pickFlow = first ? first["流编号"] : "";
    Flow.pickCode = "";
  }
  box.innerHTML = flowPickHtml(cards, Flow.pickFlow, Flow.pickCode);
  const flowSel = $("#flow-pick-flow");
  const codeSel = $("#flow-pick-code");
  if (flowSel) flowSel.addEventListener("change", () => {
    Flow.pickFlow = flowSel.value;
    Flow.pickCode = "";
    renderPick();
  });
  if (codeSel) codeSel.addEventListener("change", () => { Flow.pickCode = codeSel.value; });
}


/* 每只标的行下面那张分时图：进入/刷新页面时按顺序补画（服务端有 60 秒缓存 + 限速）。 */
export function drawRowCharts(cards) {
  const list = $("#flow-list");
  if (!list) return;
  const jobs = [];
  (cards || []).forEach(card => (card["标的"] || []).forEach(t => {
    if (t["代码"]) jobs.push({fid: card["流编号"], code: t["代码"]});
  }));
  (async () => {
    for (const job of jobs) {
      if (!document.querySelector('[data-chart-code="' + job.code + '"]')) continue;
      await renderFlowChart(job.fid, job.code, false, list);
    }
  })();
}

function setBadge(cards, pending) {
  const el = $("#nav-flow-badge");
  if (!el) return;
  const hit = (cards || []).filter(c => (c["标的"] || [])
    .some(t => (t["到价"] || []).some(m => m["级别"] === "bad"))).length;
  const n = (pending || []).length;
  if (hit) { el.textContent = "!"; el.hidden = false; el.title = hit + " 条流里有标的触及止损/风险位"; }
  else if (n) { el.textContent = String(n); el.hidden = false; el.title = n + " 条流有待重算的计划"; }
  else { el.hidden = true; el.textContent = ""; el.title = ""; }
}

function renderForm() {
  const box = $("#flow-new");
  if (!box || box.dataset.ready === "1") return;
  box.innerHTML = flowFormHtml("流资金 = 这条流的总资金；目标收益率与最大亏损都按它算。" +
    "开流后可以随时往流里加标的（每只选一种打法），也可以改流参数。");
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
  const code = ($("#flow-new-code").value || "").trim().replace(/[^0-9]/g, "").slice(0, 6);
  const capital = $("#flow-new-capital").value;
  const target = $("#flow-new-target").value;
  const loss = $("#flow-new-loss").value;
  $("#flow-msg").textContent = "";
  if (!capital || !target || !loss) {
    $("#flow-msg").textContent = "流资金 / 目标收益率 / 最大亏损都要填";
    return;
  }
  const body = {"流资金": capital, "目标收益率_pct": target, "最大亏损_pct": loss,
                "备注": ($("#flow-new-note").value || "").trim()};
  if (code) {
    body.code = code;
    body.name = ($("#flow-new-name").value || "").trim();
    body["打法"] = $("#flow-new-style").value;
    body["分配资金"] = ($("#flow-new-alloc").value || "").trim() || capital;
  }
  if (code && $("#flow-new-bring").checked) {
    try {
      const hold = await api("/api/holdings");
      const row = (hold["持仓"] || []).find(r => String(r["代码"]) === code);
      if (!row) { $("#flow-msg").textContent = "持仓文件里没有这只标的，去掉「带入底仓」再开"; return; }
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
    if (code && $("#flow-new-plan").checked) {
      startFlowJob("flow_plan", {流编号: res["流编号"], 代码: [code]});
    }
  } catch (e) { $("#flow-msg").textContent = e.message; }
}


/* ---------------- 任务（计算计划 / 体检） ---------------- */

export async function startFlowJob(kind, extra) {
  if (Flow.running) { toast("已有任务在跑，等它结束", "warn"); return; }
  const body = Object.assign({kind: kind}, extra || {});
  logClear("正在启动 …");
  setRunning(true, kind);
  try {
    const res = await api("/api/jobs", {method: "POST", body: JSON.stringify(body)});
    const _fresh = freshNote(res);
    if (_fresh) toast(_fresh, "warn");
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
        ? ("体检完成（" + (r["代码"] || "—") + "）· " + (r["结论"] || "—") + " · " + j.elapsed + "s")
        : ("计划完成 " + (r["成功"] || 0) + "/" + (r["计划数"] || 0) + " 只 · " + j.elapsed + "s");
      if (kind === "flow_check") {
        toast("体检结论（" + (r["代码"] || "—") + "）：" + (r["结论"] || "—") +
              (r["error"] ? "（模型失败：" + r["error"] + "）" : ""),
              r["error"] ? "bad" : "ok");
      } else if (r["error"]) {
        toast("计划生成：" + r["error"], "bad");
      } else {
        toast("已生成 " + (r["计划数"] || 0) + " 只标的的计划", "ok");
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

export function curTarget() {
  const d = Flow.detail || {};
  const list = d["标的"] || [];
  return list.find(t => t["代码"] === Flow.code) || list[0] || null;
}

export async function openFlowDetail(fid, refresh, code) {
  Flow.fid = fid;
  if (code) Flow.code = code;
  try {
    const d = await api("/api/flow?id=" + encodeURIComponent(fid) +
      (refresh ? "&refresh=1" : "") +
      (Flow.code ? "&code=" + encodeURIComponent(Flow.code) : ""));
    Flow.detail = d;
    Flow.code = d["选中"] || Flow.code;
    $("#flow-detail").innerHTML = flowDetailHtml(d);
    $("#flow-detail-head").textContent = fid + " · " + (d["标的"] || []).length +
      " 只标的 ｜ 当前 " + ((curTarget() || {})["名称"] || "—");
    renderFlowChart(fid, Flow.code, false);
    /* 看哪只就默认算哪只（批注 2：点「详情」后上面的「计算计划 / 体检」直接可用） */
    Flow.pickFlow = fid;
    Flow.pickCode = Flow.code || "";
    renderPick();
  } catch (e) {
    $("#flow-detail").innerHTML = '<div class="fail">读取详情失败：' + esc(e.message) + "</div>";
  }
}

/* 行内图：流卡片的标的行内 + 详情卡共用这一条渲染路径（一个标的一次请求）。
 * 分时走 60 秒缓存；日K 走当天磁盘缓存（mode=day）。 */
export async function renderFlowChart(fid, code, refresh, root, mode) {
  const box = root || $("#flow-detail") || document;
  if (!box || !code) return;
  const canvas = box.querySelector('[data-chart-code="' + code + '"]');
  const note = box.querySelector('[data-chart-note="' + code + '"]');
  if (!canvas) return;
  const want = mode || Flow.chartMode[code] || "minute";
  Flow.chartMode[code] = want;
  if (!Flow.dayBars[code]) Flow.dayBars[code] = DAY_BARS[0];
  canvas.dataset.dayBars = String(Flow.dayBars[code]);
  if (note) note.textContent = want === "day" ? "正在取日K …" : "正在取分时 …";
  try {
    const d = await api("/api/flow/minutes?id=" + encodeURIComponent(fid) +
      "&code=" + encodeURIComponent(code) + "&mode=" + want +
      (refresh ? "&refresh=1" : ""));
    /* 买卖点只画在分时对应那一天：优先当天成交；当天没有就用最近一天的成交（非交易日也看得见） */
    const all = d["成交"] || [];
    const days = Array.from(new Set(all.map(f => String(f["日期"] || "")))).sort();
    const day = all.some(f => String(f["日期"] || "") === String(d["分时日期"] || ""))
      ? String(d["分时日期"] || "") : (days[days.length - 1] || "");
    const fills = all.filter(f => String(f["日期"] || "") === day);
    if (want === "day") drawFlowDays(canvas, d); else drawFlowMinutes(canvas, d);
    bindFlowChart(canvas);
    markChartMode(box, code, want);
    if (note) {
      const bits = [d["口径"] || ""];
      if (want === "day") bits.push("显示 " + canvas.dataset.dayBars + " 根");
      else bits.push("当日成交 " + fills.length + " 笔");
      if ((d["提示"] || []).length) bits.push(d["提示"].join("；"));
      note.textContent = bits.filter(Boolean).join(" ｜ ");
    }
  } catch (e) {
    if (note) note.textContent = (want === "day" ? "日K" : "分时") + "取数失败：" + e.message;
  }
}

/* 行内图上方那排「分时 / 日K（+ 根数）」按钮的选中态。 */
export function markChartMode(box, code, mode) {
  box.querySelectorAll('[data-act="chartmode"][data-code="' + code + '"]').forEach(b => {
    b.className = "btn sm" + (b.dataset.mode === mode ? "" : " ghost");
  });
  const barsBtn = box.querySelector('[data-act="daybars"][data-code="' + code + '"]');
  if (barsBtn) {
    barsBtn.hidden = mode !== "day";
    barsBtn.textContent = (Flow.dayBars[code] || DAY_BARS[0]) + " 根";
  }
}

/* 切分时 / 日K：只换图，不动计划与成交（日K 也不用重新请求行情）。 */
export async function switchChartMode(fid, code, mode, root) {
  const box = root || $("#flow-detail") || $("#flow-list") || document;
  Flow.chartMode[code] = mode;
  markChartMode(box, code, mode);
  await renderFlowChart(fid, code, false, box, mode);
  const canvas = box.querySelector('[data-chart-code="' + code + '"]');
  if (canvas && mode === "day") canvas.scrollIntoView({block: "center"});
}

/* 日K 根数：120 ↔ 250（与报告页 K 线档位同源）。 */
export async function cycleDayBars(fid, code, root) {
  const box = root || $("#flow-detail") || $("#flow-list") || document;
  const cur = Flow.dayBars[code] || DAY_BARS[0];
  const idx = DAY_BARS.indexOf(cur);
  Flow.dayBars[code] = DAY_BARS[(idx + 1) % DAY_BARS.length];
  await renderFlowChart(fid, code, false, box, "day");
}

/* 切标的页签：详情接口一次把整条流都给了，切换不发请求。 */
function switchTab(code) {
  Flow.code = code;
  $$("#flow-detail [data-body]").forEach(b => { b.hidden = b.dataset.body !== code; });
  $$("#flow-detail [data-tab]").forEach(b => {
    b.className = "btn sm" + (b.dataset.tab === code ? "" : " ghost");
  });
  $("#flow-detail-head").textContent = Flow.fid + " · 当前 " +
    ((curTarget() || {})["名称"] || code);
}

/* 取某只标的的操作面板：给了 code 就先切到它。 */
function targetBodyEl(code) {
  const box = $("#flow-detail");
  if (!box) return null;
  if (code && box.querySelector('[data-body="' + code + '"]')) {
    switchTab(code);
    return box.querySelector('[data-body="' + code + '"]');
  }
  return box.querySelector("[data-body]:not([hidden])");
}

function targetCardOf(fid, code) {
  const card = (((Flow.list || {})["流"]) || []).find(c => c["流编号"] === fid);
  return ((((card || {})["标的"]) || []).find(t => t["代码"] === code)) || {};
}

/* 行内图所在的容器：详情卡里是这只标的的 [data-body]，列表里是那张流卡片。
 * 必须认准容器（不能退到 #flow-detail），否则在列表上点「日K」会找不到画布而静默不动。 */
function chartBox(el, cardEl) {
  return el.closest("[data-body]") || (cardEl && cardEl.isConnected ? cardEl : null) ||
    $("#flow-list") || document;
}

export async function addFill(code) {
  const body = targetBodyEl(code);
  if (!body) { toast("先在详情里选一只标的", "bad"); return; }
  const get = k => (body.querySelector('[data-fill="' + k + '"]') || {}).value || "";
  const side = get("side"), price = get("price"), qty = get("qty");
  if (!price || !qty) { toast("成交价与数量都要填", "bad"); return; }
  try {
    const res = await api("/api/flow/fill", {
      method: "POST",
      body: JSON.stringify({"流编号": Flow.fid, "代码": code || Flow.code,
                            "方向": side, "价格": price, "数量": qty,
                            "日期": get("day").trim() || undefined,
                            "备注": get("note").trim()}),
    });
    if (!res.ok) { toast(res.error, "bad"); return; }
    toast("已记一笔：" + side + " " + qty + " 股", "ok");
    (res["提示"] || []).forEach(t => toast(t, "warn"));
    await openFlowDetail(Flow.fid, false, code || Flow.code);
    await loadFlows(false);
  } catch (e) { toast(e.message, "bad"); }
}

export async function deleteFill(seq, code) {
  confirmModal("删除这笔成交？", "删除后会按剩下的成交重算持仓与盈亏（不会改计划）。", async () => {
    try {
      const res = await api("/api/flow/fill/delete", {
        method: "POST",
        body: JSON.stringify({"流编号": Flow.fid, "代码": code || Flow.code, "序号": Number(seq)}),
      });
      if (!res.ok) { toast(res.error, "bad"); return; }
      toast("已删除成交 #" + seq, "ok");
      await openFlowDetail(Flow.fid, false, code || Flow.code);
      await loadFlows(false);
    } catch (e) { toast(e.message, "bad"); }
  }, "确认删除");
}

export async function syncLedger(code) {
  try {
    const res = await api("/api/flow/sync", {
      method: "POST", body: JSON.stringify({流编号: Flow.fid, 代码: code || Flow.code}),
    });
    if (!res.ok) { toast(res.error, "bad"); return; }
    toast(res["新增"] ? "从台账并入 " + res["新增"] + " 笔成交" : "台账里没有新成交", res["新增"] ? "ok" : "warn");
    await openFlowDetail(Flow.fid, false, code || Flow.code);
    await loadFlows(false);
  } catch (e) { toast(e.message, "bad"); }
}

export async function runCheck(fid, code, note, pre) {
  startFlowJob("flow_check", {"流编号": fid, "代码": code ? [code] : [],
                              "补充说明": note || "", "先抓": pre || ""});
}

/* ---------------- 加标的 / 改打法 / 改流参数 ---------------- */

export async function addTarget(card) {
  const box = card.querySelector("details.fl-add");
  if (!box) return;
  const msg = box.querySelector("[data-add-msg]");
  const val = k => (box.querySelector('[data-add="' + k + '"]') || {}).value || "";
  msg.textContent = "";
  const code = val("code").trim().replace(/[^0-9]/g, "").slice(0, 6);
  if (!code) { msg.textContent = "先填 6 位证券代码"; return; }
  const body = {"流编号": card.dataset.fid, "代码": code,
                "名称": val("name").trim(), "打法": val("style"),
                "分配资金": val("alloc").trim()};
  if (!body["分配资金"]) delete body["分配资金"];
  if (box.querySelector('[data-add="bring"]').checked) {
    try {
      const hold = await api("/api/holdings");
      const row = (hold["持仓"] || []).find(r => String(r["代码"]) === code);
      if (!row) { msg.textContent = "持仓文件里没有这只标的，去掉「带入底仓」再加"; return; }
      body["起始持仓"] = {"股数": row["持有股数"], "成本价": row["成本价"],
                          "可用": row["可用股数_可卖"]};
    } catch (e) { msg.textContent = e.message; return; }
  }
  try {
    const res = await api("/api/flow/target/add", {method: "POST", body: JSON.stringify(body)});
    if (!res.ok) { msg.textContent = res.error; return; }
    toast("已加入标的 " + code, "ok");
    await loadFlows(false);
    await openFlowDetail(card.dataset.fid, false, code);
  } catch (e) { msg.textContent = e.message; }
}

export function setTargetDialog(fid, code) {
  const t = targetCardOf(fid, code);
  openModal("改打法 / 分配资金", targetFormHtml(code, t));
  $("#btn-flow-set-save").addEventListener("click", async () => {
    $("#flow-set-msg").textContent = "";
    try {
      const res = await api("/api/flow/target/set", {
        method: "POST",
        body: JSON.stringify({"流编号": fid, "代码": code,
                              "打法": $("#flow-set-style").value,
                              "分配资金": ($("#flow-set-alloc").value || "").trim()}),
      });
      if (!res.ok) { $("#flow-set-msg").textContent = res.error; return; }
      closeModal();
      toast("已保存", "ok");
      await loadFlows(false);
      await openFlowDetail(fid, false, code);
    } catch (e) { $("#flow-set-msg").textContent = e.message; }
  });
}

export function paramsDialog(fid) {
  const card = (((Flow.list || {})["流"] || []).find(c => c["流编号"] === fid)) || {};
  openModal("改流参数（" + fid + "）", paramsFormHtml(card["参数"] || {}));
  $("#btn-flow-p-save").addEventListener("click", async () => {
    $("#flow-p-msg").textContent = "";
    try {
      const res = await api("/api/flow/params", {
        method: "POST",
        body: JSON.stringify({"流编号": fid, "流资金": $("#flow-p-capital").value,
                              "目标收益率_pct": $("#flow-p-target").value,
                              "最大亏损_pct": $("#flow-p-loss").value,
                              "备注": $("#flow-p-note").value}),
      });
      if (!res.ok) { $("#flow-p-msg").textContent = res.error; return; }
      closeModal();
      toast("流参数已保存", "ok");
      await loadFlows(false);
      await openFlowDetail(fid, false);
    } catch (e) { $("#flow-p-msg").textContent = e.message; }
  });
}

export function removeTarget(fid, code) {
  confirmModal("把这只标的移出这条流？",
    "它的成交明细、体检记录与计划引用会一起删除（计划产物文件仍在 data/ai/track 里可单独打开）；" +
    "分配资金会退回这条流。", async () => {
      try {
        const res = await api("/api/flow/target/remove", {
          method: "POST", body: JSON.stringify({"流编号": fid, "代码": code}),
        });
        if (!res.ok) { toast(res.error, "bad"); return; }
        toast("已移除 " + code, "ok");
        await loadFlows(false);
        await openFlowDetail(fid, false);
      } catch (e) { toast(e.message, "bad"); }
    }, "确认移除");
}

export async function closeFlow(fid, code) {
  const what = code ? (code + " 这只标的") : "这条流（流内所有标的）";
  confirmModal("结束" + what + "？", "结束后不再盯盘、也不能再补录成交；已经记录的成交与盈亏都会保留。",
    async () => {
      try {
        const res = await api("/api/flow/close", {
          method: "POST",
          body: JSON.stringify({"流编号": fid, "代码": code || "", "原因": "人工结束"}),
        });
        if (!res.ok) { toast(res.error, "bad"); return; }
        toast("已结束 " + (code || fid), "ok");
        await loadFlows(false);
        await openFlowDetail(fid, false, code || undefined);
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


/* ---------------- 交互绑定 ---------------- */

/* 卡片与详情里的按钮统一走这里：动作名在 data-act，标的小按钮带 data-code。 */
function onCardAction(e, root) {
  const t = e.target;
  if (!t || !t.closest) return;
  const planLink = t.closest("[data-plan]");
  if (planLink) { e.preventDefault(); openReport(planLink.dataset.plan); return; }
  const delLink = t.closest("[data-delfill]");
  if (delLink) { deleteFill(delLink.dataset.delfill, delLink.dataset.code); return; }
  const act = t.dataset ? t.dataset.act : "";
  if (!act) return;
  const cardEl = t.closest(".flow-card") || root.closest(".flow-card");
  const fid = (cardEl && cardEl.dataset.fid) || Flow.fid;
  const code = t.dataset.code || Flow.code;
  if (act === "detail") openFlowDetail(fid, true, code);
  else if (act === "minutes") {
    /* 「分时」= 强制刷新这只标的那张行内图（跳过缓存）并滚到它，图型沿用当前选择 */
    const box = t.closest("[data-body]") || cardEl || $("#flow-list") || document;
    renderFlowChart(fid, code, true, box).then(() => {
      const canvas = box.querySelector('[data-chart-code="' + code + '"]') ||
        document.querySelector('[data-chart-code="' + code + '"]');
      if (canvas) canvas.scrollIntoView({block: "center"});
    });
  }
  else if (act === "chartmode") {
    switchChartMode(fid, code, t.dataset.mode, chartBox(t, cardEl));
  }
  else if (act === "daybars") cycleDayBars(fid, code, chartBox(t, cardEl));
  else if (act === "plan") startFlowJob("flow_plan", {流编号: fid, 代码: code ? [code] : []});
  else if (act === "check") openCheckDialog(fid, code);
  else if (act === "add") { if (cardEl) addTarget(cardEl); }
  else if (act === "params") paramsDialog(fid);
  else if (act === "setstyle") setTargetDialog(fid, code);
  else if (act === "remove") removeTarget(fid, code);
  else if (act === "closetarget") closeFlow(fid, code);
  else if (act === "close") closeFlow(fid);
  else if (act === "delete") deleteFlow(fid);
  else if (act === "sync") syncLedger(code);
  else if (act === "fill") {
    openFlowDetail(fid, false, code).then(() => {
      const body = targetBodyEl(code);
      const box = body && body.querySelector('[data-fill="price"]');
      if (box) { box.focus(); box.scrollIntoView({block: "center"}); }
    });
  }
}

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
    ((Flow.list && Flow.list["待重算"]) || []).forEach(x =>
      startFlowJob("flow_plan", {流编号: x["流编号"], 代码: x["代码"]}));
  });
  const list = $("#flow-list");
  if (list) list.addEventListener("click", e => onCardAction(e, list));
  const detailBox = $("#flow-detail");
  if (detailBox) detailBox.addEventListener("click", e => {
    const tab = e.target.closest("[data-tab]");
    if (tab) { switchTab(tab.dataset.tab); return; }
    onCardAction(e, detailBox);
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
    const fid = Flow.pickFlow;
    if (!fid) { toast("还没有进行中的交易流：先开一条流再加标的", "bad"); return; }
    startFlowJob("flow_plan", {流编号: fid, 代码: Flow.pickCode ? [Flow.pickCode] : []});
  });
  const check = $("#btn-flow-check");
  if (check) check.addEventListener("click", () => {
    const fid = Flow.pickFlow;
    if (!fid) { toast("还没有进行中的交易流：先开一条流再加标的", "bad"); return; }
    if (!Flow.pickCode) {
      toast("体检要指明一只标的：在上面「选流与标的」里挑一只（体检是逐只排查意外）", "warn");
      return;
    }
    openCheckDialog(fid, Flow.pickCode);
  });
}


export function openCheckDialog(fid, code) {
  openModal("体检：排查意外情况",
    '<div class="muted">标的：<b>' + esc(code || "—") + "</b>" +
    ' <span class="muted">（体检只针对这一只标的，逐只排查）</span></div>' +
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
    runCheck(fid, code, note, pre);
  };
  $("#flow-check-run").addEventListener("click", () => go(""));
  $("#flow-check-pull").addEventListener("click", () => go("pull"));
  $("#flow-check-news").addEventListener("click", () => go("news"));
}


/* 注册给 core/app.js：切到本页自动刷一次（拉一次批量报价 + 机械判定）。 */
registerView("flow", {
  onShow: async () => { await loadFlows(true); },
  openDetail: openFlowDetail,
  refresh: loadFlows,
});
