/* 运行研判页：参数收集、任务轮询、日志、批量与预检。 */
import { api } from "../core/api.js";
import { State, openReport, registerView, viewApi } from "../core/app.js";
import { $, $$, esc, freshNote, toast } from "../core/util.js";
import { closeModal, openModal } from "../ui/modal.js";

export const Run = { phase: "post", jobId: null, timer: null, from: 0, codes: [] };

const CODE_MAX = 30;                       // 一次最多选多少只（防手滑）


/* 代码规范化：只接受 6 位代码或带 .SH/.SZ/.BJ 后缀的 thscode */
export function normCode(raw) {
  const s = String(raw == null ? "" : raw).trim().toUpperCase().replace(/\s+/g, "");
  const m = s.match(/^(\d{6})(?:\.(SH|SZ|BJ))?$/);
  return m ? (m[2] ? m[1] + "." + m[2] : m[1]) : "";
}


export function splitCodes(text) {
  return String(text == null ? "" : text).split(/[\s,，、;；]+/).map(normCode).filter(Boolean);
}


export function pickedCodes() {
  if (Run.codes.length) return Run.codes.slice();
  const raw = ($("#code") && $("#code").value || "").trim();
  return raw ? splitCodes(raw) : [];
}


export function renderPicked() {
  const box = $("#code-picked");
  if (box) {
    box.innerHTML = Run.codes.map(c =>
      '<span class="chip accent">' + esc(c) +
      '<button class="chip-x" data-code-x="' + esc(c) + '" title="移除">×</button></span>').join("");
  }
  const clear = $("#btn-code-clear");
  if (clear) clear.hidden = !Run.codes.length;
  if (Run.codes.length > 1) $("#run-cmd").dataset.batch = "1";
  else delete $("#run-cmd").dataset.batch;
  syncRunCmd();
  checkRunReadiness();
}


export function addCodes(list) {
  (list || []).forEach(c => {
    const code = normCode(c);
    if (!code || Run.codes.indexOf(code) >= 0 || Run.codes.length >= CODE_MAX) return;
    Run.codes.push(code);
  });
  renderPicked();
}


export function clearCodes() {
  Run.codes = [];
  const input = $("#code");
  if (input) input.value = "";
  renderPicked();
}


/* 别的页面（自选股 / 总控台 / 荐股）「研判」某只标的时用：清掉多选，只填这一只 */
export function pickCode(code) {
  const c = normCode(code) || String(code == null ? "" : code).trim();
  Run.codes = c ? [c] : [];
  const input = $("#code");
  if (input) input.value = "";
  renderPicked();
}


function addFromInput() {
  const input = $("#code");
  const codes = splitCodes(input ? input.value : "");
  if (!codes.length) { toast("请填 6 位代码（或 002463.SZ 这种形式）", "bad"); return; }
  addCodes(codes);
  input.value = "";
  renderPicked();
}

export function consoleAppend(line) {
  const pre = $("#console");
  if (pre.dataset.clean !== "1") { pre.innerHTML = ""; pre.dataset.clean = "1"; }
  const span = document.createElement("div");
  let cls = "";
  const t = (line.text || "").trim();
  if (t.startsWith("[OK]")) cls = "l-ok";
  else if (t.startsWith("[WARN]")) cls = "l-warn";
  else if (t.startsWith("[FAIL]")) cls = "l-fail";
  else if (t.startsWith("[..]")) cls = "l-stage";
  else if (t.startsWith("    ")) cls = "l-dim";
  span.innerHTML = '<span class="l-ts">' + esc(line.t || "") + "</span>" +
    '<span class="' + cls + '">' + esc(line.text) + "</span>";
  pre.appendChild(span);
  pre.scrollTop = pre.scrollHeight;
}

export function consoleClear(msg) {
  const pre = $("#console");
  pre.dataset.clean = "1";
  pre.innerHTML = msg ? '<span class="l-dim">' + esc(msg) + "</span>" : "";
}

export function collectRunBody(kind) {
  const body = {
    kind: kind,
    phase: Run.phase,
    code: pickedCodes()[0] || "",
    date: $("#date").value.trim(),
    profile: $("#profile").value,
    top: $("#top").value,
    max_input_chars: $("#maxchars").value,
    no_fetch: $("#opt-no-fetch").checked,
    no_review: $("#opt-no-review").checked,
    dry_run: $("#opt-dry-run").checked || kind === "dry",
    debug: $("#opt-debug").checked,
    model_pro: $("#model-pro").value.trim(),
    model_review: $("#model-review").value.trim(),
    api_key: $("#api-key").value.trim(),
    api_base: $("#api-base").value.trim(),
  };
  if (kind === "dry") { body.kind = "run"; body.dry_run = true; }
  if (kind === "check") { delete body.top; delete body.max_input_chars; }
  return body;
}

export async function startJob(kind) {
  const codes = pickedCodes();
  if (kind !== "check" && !codes.length) {
    toast("请先填写标的代码", "bad");
    $("#code").focus();
    return;
  }
  if (kind !== "check" && codes.length > 1) {     // 多选 → 走既有的顺序批量
    showPickedDialog(codes, kind);
    return;
  }
  const body = collectRunBody(kind);
  body.code = codes[0] || "";
  consoleClear("正在启动 …");
  showDiagnose(null);
  setRunning(true);
  try {
    const res = await api("/api/jobs", { method: "POST", body: JSON.stringify(body) });
    const _fresh = freshNote(res);
    if (_fresh) toast(_fresh, "warn");
    if (!res.ok) throw new Error(res.error || "启动失败");
    Run.jobId = res.id;
    $("#run-cmd").textContent = res["命令"] || "";
    $("#run-status").className = "chip accent";
    $("#run-status").innerHTML = '<span class="spinner"></span> 运行中';
    Run.from = 0;
    pollJob();
  } catch (e) {
    toast(e.message, "bad");
    setRunning(false);
    $("#run-status").className = "chip bad";
    $("#run-status").textContent = "启动失败";
  }
}

export function setRunning(on) {
  Run.running = on;
  $("#btn-run").disabled = on;
  $("#btn-check").disabled = on;
  $("#btn-make-prompt").disabled = on;
  const bb = $("#btn-batch");
  if (bb) bb.disabled = on;
  $("#btn-stop").hidden = !on;
}


/* 批量目标来源：holdings = 持仓文件，watch = 自选股 */
export async function pollJob() {
  if (!Run.jobId) return;
  try {
    const j = await api("/api/jobs/" + Run.jobId + "?from=" + Run.from);
    (j.lines || []).forEach(consoleAppend);
    Run.from = j.next;
    if (j.status === "running") {
      $("#run-status").innerHTML = '<span class="spinner"></span> 运行中 ' + j.elapsed + "s";
      clearTimeout(Run.timer);
      Run.timer = setTimeout(pollJob, 700);
      return;
    }
    setRunning(false);
    const code = j.exit_code;
    if (j.status === "canceled") {
      $("#run-status").className = "chip warn";
      $("#run-status").textContent = "已中断";
    } else if (code === 0) {
      $("#run-status").className = "chip ok";
      $("#run-status").textContent = "完成 · 退出码 0 · " + j.elapsed + "s";
      toast("运行完成", "ok");
    } else {
      $("#run-status").className = "chip bad";
      $("#run-status").textContent = "失败 · 退出码 " + code;
      toast("运行失败，退出码 " + code + "（报告通常仍已落盘）", "bad");
    }
    showDiagnose(j.result && j.result["诊断"]);
    if (j.kind === "batch") showBatchResult(j.result, j.status);
    if (j.result && j.result.report) {
      const ok = j.result.report_ok !== false;
      toast(ok ? "已生成报告，正在打开" : "报告已落盘（标注 FAIL），正在打开", ok ? "ok" : "warn");
      await viewApi("report").refreshReports();
      openReport(j.result.report);
    } else if (j.result && j.result.factpack) {
      toast("已落事实包：" + j.result.factpack, "ok");
      await viewApi("report").refreshReports();
    } else if (j.kind === "check") {
      toast("自检结束", code === 0 ? "ok" : "warn");
    } else if (j.result && j.result["诊断"]) {
      toast("运行失败，请看下方诊断", "bad");
    }
  } catch (e) {
    clearTimeout(Run.timer);
    Run.timer = setTimeout(pollJob, 1500);
  }
}

export function batchTargets(kind) {
  if (kind === "watch") {
    return (((State.state || {})["自选股"]) || []).map(x => ({ "代码": x["代码"], "名称": x["名称"], "备注": x["备注"] }));
  }
  return ((((State.state || {})["持仓"]) || {})["持仓"] || []).map(x => ({ "代码": x["代码"], "名称": x["名称"] }));
}

export async function startBatch(skipReview, kind) {
  const codes = batchTargets(kind).map(x => x["代码"]).filter(Boolean);
  if (!codes.length) { toast(kind === "watch" ? "自选股是空的" : "持仓文件里没有可研判的标的", "bad"); return; }
  return startBatchCodes(codes, skipReview);
}


/* 指定代码列表的顺序批量（运行页多选也走这里） */
export async function startBatchCodes(codes, skipReview, forceDryRun) {
  const list = (codes || []).filter(Boolean);
  if (!list.length) { toast("没有可研判的标的", "bad"); return; }
  const body = {
    kind: "batch", phase: Run.phase, codes: list,
    no_fetch: $("#opt-no-fetch").checked,
    dry_run: !!forceDryRun || $("#opt-dry-run").checked,
    no_review: !!skipReview || $("#opt-no-review").checked,
    debug: $("#opt-debug").checked,
    top: $("#top").value, max_input_chars: $("#maxchars").value,
    profile: $("#profile").value, date: $("#date").value.trim(),
    model_pro: $("#model-pro").value.trim(), model_review: $("#model-review").value.trim(),
    api_key: $("#api-key").value.trim(), api_base: $("#api-base").value.trim(),
  };
  consoleClear("批量研判启动中 …");
  showDiagnose(null);
  setRunning(true);
  const br = $("#batch-result");
  if (br) { br.hidden = true; br.innerHTML = ""; }
  try {
    const res = await api("/api/jobs", { method: "POST", body: JSON.stringify(body) });
    if (!res.ok) throw new Error(res.error || "启动失败");
    Run.jobId = res.id;
    Run.isBatch = true;
    $("#run-cmd").textContent = res["命令"] || "";
    $("#run-status").className = "chip accent";
    $("#run-status").innerHTML = '<span class="spinner"></span> 批量运行中';
    Run.from = 0;
    pollJob();
  } catch (e) {
    toast(e.message, "bad");
    setRunning(false);
  }
}

export function showBatchResult(result, status) {
  const el = $("#batch-result");
  if (!el) return;
  const rows = (result || {})["批量"];
  if (!Array.isArray(rows) || !rows.length) { el.hidden = true; return; }
  const ok = (result || {})["成功"] || 0;
  el.hidden = false;
  el.innerHTML = '<div class="sec-title" style="margin:14px 0 8px">批量结果：成功 ' + ok + " / 共 " + rows.length + "</div>" +
    '<div class="table-wrap" style="max-height:none"><table class="tbl"><thead><tr>' +
    "<th>标的</th><th>退出码</th><th class='num'>耗时(s)</th><th>报告</th></tr></thead><tbody>" +
    rows.map(r => "<tr><td><b>" + esc(r["代码"]) + "</b></td><td>" +
      (r["退出码"] === 0 ? '<span class="badge ok">0</span>' : '<span class="badge bad">' + esc(r["退出码"]) + "</span>") +
      '</td><td class="num">' + (r["耗时_秒"] == null ? "—" : r["耗时_秒"]) + "</td><td>" +
      (r["报告"] ? '<a href="#" data-jump="' + esc(r["报告"]) + '">查看</a>' : '<span class="muted">—</span>') +
      "</td></tr>").join("") + "</tbody></table></div>";
  $$("#batch-result [data-jump]").forEach(a => a.addEventListener("click", e => {
    e.preventDefault(); openReport(a.dataset.jump);
  }));
}


/* 运行前的就地预检：避免「复用已有数据 + 标的不在缓存里」这种必然失败 */
export async function stopJob() {
  if (!Run.jobId) return;
  await api("/api/jobs/" + Run.jobId + "/cancel", { method: "POST", body: "{}" });
  toast("已发送中断", "warn");
}

export function checkRunReadiness() {
  syncOverrideHint();
  const el = $("#code-hint");
  if (!el) return;
  const codes = pickedCodes();
  const s3 = ((State.state || {})["stock3d"]) || {};
  const cached = s3["代码"] || [];
  const noFetch = $("#opt-no-fetch").checked;
  if (!codes.length) {
    el.className = "hint";
    el.textContent = "一次只深度研判一只标的；也可以多选（输入后回车 / 点「多选」）。";
    return;
  }
  const missing = codes.map(c => c.replace(/\..*$/, "")).filter(c => cached.indexOf(c) < 0);
  if (noFetch && cached.length && missing.length) {
    el.className = "hint warn-hint";
    el.innerHTML = "⚠ 最新 stock3d 缓存（" + esc(s3["target_date"] || "—") + "）里没有 " +
      esc(missing.join("、")) + "，只有 " + esc(cached.join("、")) +
      "。现在开着「复用已有数据」，这几只跑下去会直接失败（退出码 4）；取消该开关再跑，程序会先抓数。";
    return;
  }
  el.className = "hint";
  el.textContent = (codes.length > 1
    ? ("已选 " + codes.length + " 只：按顺序依次研判，每只各出一份报告（单只失败不中断）。")
    : "一次只深度研判一只标的；也可以多选。") +
    (cached.length ? "当前数据缓存含：" + cached.join("、") : "");
}

/* 覆盖模型与 Key 的防呆：填了就会盖掉配置里的 Key（这是 401 最常见的来源） */
export function syncOverrideHint() {
  const el = $("#override-hint");
  if (!el) return;
  const key = (($("#api-key") || {}).value || "").trim();
  const base = (($("#api-base") || {}).value || "").trim();
  const bits = [];
  if (key) bits.push("API Key（尾号 " + key.slice(-4) + "）");
  if (base) bits.push("API Base（" + base + "）");
  if (!bits.length) { el.hidden = true; el.textContent = ""; return; }
  el.hidden = false;
  el.innerHTML = "⚠ 本次会用你填的 " + esc(bits.join(" 和 ")) +
    "：它**优先于**模型配置与环境变量里的 Key，填错就会直接 401 invalid api key。" +
    "想用配置里的 key 就把这里清空（「模型配置」页能看到当前 key 的来源与掩码）。";
}


export function showDiagnose(text) {
  const el = $("#run-diagnose");
  if (!el) return;
  if (!text) { el.hidden = true; el.innerHTML = ""; return; }
  el.hidden = false;
  el.innerHTML = "<b>诊断</b>" + esc(text);
}

export function initRunView() {
  $$("#phase-seg button").forEach(b => b.addEventListener("click", () => {
    Run.phase = b.dataset.phase;
    $$("#phase-seg button").forEach(x => x.classList.toggle("on", x === b));
    syncRunCmd();
  }));
  ["#code", "#date", "#top", "#maxchars", "#profile", "#model-pro", "#model-review"].forEach(sel => {
    const el = $(sel);
    if (el) el.addEventListener("input", syncRunCmd);
  });
  ["#api-key", "#api-base"].forEach(sel => {
    const el = $(sel);
    if (el) el.addEventListener("input", syncOverrideHint);
  });
  ["#opt-no-fetch"].forEach(sel => {
    const el = $(sel);
    if (el) { el.addEventListener("input", checkRunReadiness); el.addEventListener("change", checkRunReadiness); }
  });
  const codeInput = $("#code");
  if (codeInput) {
    codeInput.addEventListener("keydown", e => {
      if (e.key === "Enter") { e.preventDefault(); addFromInput(); }
    });
  }
  const addBtn = $("#btn-code-add");
  if (addBtn) addBtn.addEventListener("click", addFromInput);
  const clearBtn = $("#btn-code-clear");
  if (clearBtn) clearBtn.addEventListener("click", () => {
    clearCodes();
    toast("已清空已选标的", "ok");
  });
  const pickedBox = $("#code-picked");
  if (pickedBox) pickedBox.addEventListener("click", e => {
    const x = e.target.closest("[data-code-x]");
    if (!x) return;
    Run.codes = Run.codes.filter(c => c !== x.dataset.codeX);
    renderPicked();
  });
  $("#btn-run").addEventListener("click", () => startJob("run"));
  $("#btn-check").addEventListener("click", () => startJob("check"));
  $("#btn-make-prompt").addEventListener("click", () => startJob("dry"));
  $("#btn-stop").addEventListener("click", stopJob);
  $("#btn-batch").addEventListener("click", () => showBatchDialog("holdings"));
  $("#btn-holdings-pick").addEventListener("click", showSymbolPicker);
  document.addEventListener("keydown", e => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter" && State.currentView === "run") { e.preventDefault(); startJob("run"); }
  });
}

export function syncRunCmd() {
  const codes = pickedCodes();
  const parts = ["python", "aiplan.py", Run.phase, "--code", codes[0] || "<代码>"];
  if ($("#date").value) parts.push("--date", $("#date").value);
  if ($("#profile").value) parts.push("--profile", $("#profile").value);
  if ($("#opt-no-fetch").checked) parts.push("--no-fetch");
  if ($("#opt-no-review").checked) parts.push("--no-review");
  if ($("#opt-dry-run").checked) parts.push("--dry-run");
  if ($("#opt-debug").checked) parts.push("--debug");
  if ($("#model-pro").value.trim()) parts.push("--model-pro", $("#model-pro").value.trim());
  if ($("#model-review").value.trim()) parts.push("--model-review", $("#model-review").value.trim());
  if (codes.length > 1) parts.push("…（依次研判 " + codes.length + " 只：" + codes.join(" ") + "）");
  $("#run-cmd").textContent = parts.join(" ");
}

/* 顺序批量的确认框（持仓 / 自选 / 运行页多选共用） */
function batchModal(title, codes, onGo) {
  const noFetch = $("#opt-no-fetch").checked, dryRun = $("#opt-dry-run").checked;
  const cached = (((State.state || {})["stock3d"]) || {})["代码"] || [];
  const missing = codes.map(c => String(c).replace(/\..*$/, "")).filter(c => cached.indexOf(c) < 0);
  let warn = "";
  if (dryRun) warn = '<div class="fail">当前勾着「只看不花钱」，批量只会生成事实包，不会调模型。</div>';
  else warn = '<div class="muted">每只标的都会各调一次模型（研判档 + 复核档），费用随只数线性增加。</div>';
  if (noFetch && missing.length) {
    warn += '<div class="fail">注意：当前开着「复用已有数据」，但缓存里没有 ' + esc(missing.join("、")) +
      "，这几只会直接失败（退出码 4）。</div>";
  }
  openModal(title,
    '<div class="muted">将按顺序对以下标的依次研判，单只失败不会中断后面的：</div>' +
    '<div class="cards-3" style="margin:10px 0">' + codes.map(c =>
      '<div class="mini-card"><b>' + esc(c) + "</b></div>").join("") + "</div>" + warn +
    '<div class="row" style="margin-top:16px;justify-content:flex-end;flex-wrap:wrap">' +
    '<button class="btn ghost" id="ba-cancel">取消</button>' +
    '<button class="btn ghost" id="ba-noreview" title="跳过复核档，省一半调用">开始（跳过复核，省一半）</button>' +
    '<button class="btn primary" id="ba-go">开始（含复核）</button></div>');
  $("#ba-cancel").addEventListener("click", closeModal);
  $("#ba-noreview").addEventListener("click", () => { closeModal(); onGo(true); });
  $("#ba-go").addEventListener("click", () => { closeModal(); onGo(false); });
}


export function showBatchDialog(kind) {
  const codes = batchTargets(kind).map(x => x["代码"]).filter(Boolean);
  if (!codes.length) { toast(kind === "watch" ? "自选股是空的，先去「自选股」页添加" : "持仓文件里没有可研判的标的", "bad"); return; }
  batchModal("批量研判" + (kind === "watch" ? "自选股" : "持仓") + "（" + codes.length + " 只）",
             codes, skip => startBatchCodes(codes, skip));
}


/* 运行页多选：点「开始研判」时如果选了多只，先确认再顺序跑 */
export function showPickedDialog(codes, kind) {
  const list = (codes || []).filter(Boolean);
  if (!list.length) { toast("先选标的", "bad"); return; }
  if (kind === "dry") {
    openModal("批量只落事实包（" + list.length + " 只）",
      '<div class="muted">当前是「预览事实包」：会对这 ' + list.length +
      " 只依次抓数并落事实包，**不调用模型**（零成本）。</div>" +
      '<div class="cards-3" style="margin:10px 0">' + list.map(c =>
        '<div class="mini-card"><b>' + esc(c) + "</b></div>").join("") + "</div>" +
      '<div class="row" style="justify-content:flex-end"><button class="btn ghost" id="ba-cancel">取消</button>' +
      '<button class="btn primary" id="ba-go">开始</button></div>');
    $("#ba-cancel").addEventListener("click", closeModal);
    $("#ba-go").addEventListener("click", () => { closeModal(); startBatchCodes(list, true, true); });
    return;
  }
  batchModal("批量研判（已选 " + list.length + " 只）", list, skip => startBatchCodes(list, skip));
}

export function showSymbolPicker() {
  const items = (State.state && State.state["代码候选"]) || [];
  if (!items.length) { toast("没有可用的标的候选", "warn"); return; }
  const codeOf = it => normCode(it["thscode"] || it["代码"]) || String(it["代码"] || "");
  const chosen = new Set(Run.codes);
  openModal("多选标的（最多 " + CODE_MAX + " 只）",
    '<div class="muted" style="margin-bottom:10px">来源包括 持仓 / 自选 / 行情缓存 / 历史记录；带 <span class="chip ok">已缓存</span> 的可以直接配合「复用已有数据」零成本试跑。点一下选中 / 取消。</div>' +
    '<div class="row" style="gap:8px;margin-bottom:8px"><button class="btn ghost" id="pick-all">全选</button>' +
    '<button class="btn ghost" id="pick-none">全部取消</button><span class="muted" id="pick-count"></span></div>' +
    '<div class="cards-3" id="pick-list">' + items.map((it, i) =>
      '<button class="btn pick-item' + (chosen.has(codeOf(it)) ? " on" : "") + '" data-idx="' + i +
      '" style="justify-content:flex-start">' +
      "<b>" + esc(it["代码"]) + "</b>&nbsp;" + esc(it["名称"] || "") +
      (it["在最新stock3d"] ? ' <span class="chip ok">已缓存</span>' : "") +
      '<span class="muted" style="margin-left:auto">' + esc(it["来源"]) + "</span></button>").join("") + "</div>" +
    '<div class="row" style="margin-top:14px;justify-content:flex-end;gap:8px">' +
    '<button class="btn ghost" id="pick-cancel">取消</button>' +
    '<button class="btn primary" id="pick-ok">确定</button></div>');
  const boxes = $$("#pick-list .pick-item");
  const count = () => {
    const el = $("#pick-count");
    if (el) el.textContent = "已勾选 " + chosen.size + " 只（上限 " + CODE_MAX + "）";
  };
  boxes.forEach(b => b.addEventListener("click", () => {
    const code = codeOf(items[Number(b.dataset.idx)]);
    if (chosen.has(code)) { chosen.delete(code); b.classList.remove("on"); }
    else if (chosen.size >= CODE_MAX) { toast("最多选 " + CODE_MAX + " 只", "warn"); return; }
    else { chosen.add(code); b.classList.add("on"); }
    count();
  }));
  $("#pick-all").addEventListener("click", () => {
    boxes.forEach(b => {
      if (chosen.size >= CODE_MAX) return;
      chosen.add(codeOf(items[Number(b.dataset.idx)]));
      b.classList.add("on");
    });
    count();
  });
  $("#pick-none").addEventListener("click", () => {
    chosen.clear();
    boxes.forEach(b => b.classList.remove("on"));
    count();
  });
  $("#pick-cancel").addEventListener("click", closeModal);
  $("#pick-ok").addEventListener("click", () => {
    Run.codes = Array.from(chosen).slice(0, CODE_MAX);
    closeModal();
    renderPicked();
    toast("已选 " + Run.codes.length + " 只", "ok");
  });
  count();
}

/* 注册给 core/app.js：切到本页时按需加载 / 对外暴露的动作。 */
registerView("run", {
  syncCmd: syncRunCmd,
  checkReadiness: checkRunReadiness,
  showBatchDialog: showBatchDialog,
  pickedCodes: pickedCodes,
  pickCode: pickCode,
});
