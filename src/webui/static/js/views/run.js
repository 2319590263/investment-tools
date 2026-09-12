/* 运行研判页：参数收集、任务轮询、日志、批量与预检。 */
import { api } from "../core/api.js";
import { State, openReport, registerView, viewApi } from "../core/app.js";
import { $, $$, esc, toast } from "../core/util.js";
import { closeModal, openModal } from "../ui/modal.js";

export const Run = { phase: "post", jobId: null, timer: null, from: 0 };

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
    code: $("#code").value.trim(),
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
  const body = collectRunBody(kind);
  if (kind !== "check" && !body.code) { toast("请先填写标的代码", "bad"); $("#code").focus(); return; }
  consoleClear("正在启动 …");
  showDiagnose(null);
  setRunning(true);
  try {
    const res = await api("/api/jobs", { method: "POST", body: JSON.stringify(body) });
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
  const items = batchTargets(kind);
  const codes = items.map(x => x["代码"]).filter(Boolean);
  if (!codes.length) { toast(kind === "watch" ? "自选股是空的" : "持仓文件里没有可研判的标的", "bad"); return; }
  const body = {
    kind: "batch", phase: Run.phase, codes: codes,
    no_fetch: $("#opt-no-fetch").checked,
    dry_run: $("#opt-dry-run").checked,
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
  const el = $("#code-hint");
  if (!el) return;
  const raw = ($("#code").value || "").trim();
  const m = raw.match(/\d{6}/);
  const s3 = ((State.state || {})["stock3d"]) || {};
  const cached = s3["代码"] || [];
  const noFetch = $("#opt-no-fetch").checked;
  if (!m) {
    el.className = "hint";
    el.textContent = "一次只深度研判一只标的。";
    return;
  }
  if (noFetch && cached.length && cached.indexOf(m[0]) < 0) {
    el.className = "hint warn-hint";
    el.innerHTML = "⚠ 最新 stock3d 缓存（" + esc(s3["target_date"] || "—") + "）里没有 " + esc(m[0]) +
      "，只有 " + esc(cached.join("、")) + "。现在开着「复用已有数据」，跑下去会直接失败（退出码 4）；" +
      "取消该开关再跑，程序会先抓这个标的的数据。";
    return;
  }
  el.className = "hint";
  el.textContent = "一次只深度研判一只标的。" + (cached.length ? "当前数据缓存含：" + cached.join("、") : "");
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
  ["#code", "#opt-no-fetch"].forEach(sel => {
    const el = $(sel);
    if (el) { el.addEventListener("input", checkRunReadiness); el.addEventListener("change", checkRunReadiness); }
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
  const parts = ["python", "aiplan.py", Run.phase, "--code", $("#code").value.trim() || "<代码>"];
  if ($("#date").value) parts.push("--date", $("#date").value);
  if ($("#profile").value) parts.push("--profile", $("#profile").value);
  if ($("#opt-no-fetch").checked) parts.push("--no-fetch");
  if ($("#opt-no-review").checked) parts.push("--no-review");
  if ($("#opt-dry-run").checked) parts.push("--dry-run");
  if ($("#opt-debug").checked) parts.push("--debug");
  if ($("#model-pro").value.trim()) parts.push("--model-pro", $("#model-pro").value.trim());
  if ($("#model-review").value.trim()) parts.push("--model-review", $("#model-review").value.trim());
  $("#run-cmd").textContent = parts.join(" ");
}

export function showBatchDialog(kind) {
  const items = batchTargets(kind);
  const codes = items.map(x => x["代码"] + " " + (x["名称"] || "")).filter(x => x.trim());
  if (!codes.length) { toast(kind === "watch" ? "自选股是空的，先去「自选股」页添加" : "持仓文件里没有可研判的标的", "bad"); return; }
  const noFetch = $("#opt-no-fetch").checked, dryRun = $("#opt-dry-run").checked;
  const cached = (((State.state || {})["stock3d"]) || {})["代码"] || [];
  const missing = items.map(x => x["代码"]).filter(c => cached.indexOf(c) < 0);
  let warn = "";
  if (dryRun) warn = '<div class="fail">当前勾着「只看不花钱」，批量只会生成事实包，不会调模型。</div>';
  else warn = '<div class="muted">每只标的都会各调一次模型（研判档 + 复核档），费用随只数线性增加。</div>';
  if (noFetch && missing.length) {
    warn += '<div class="fail">注意：当前开着「复用已有数据」，但缓存里没有 ' + esc(missing.join("、")) +
      "，这几只会直接失败（退出码 4）。</div>";
  }
  openModal("批量研判" + (kind === "watch" ? "自选股" : "持仓") + "（" + items.length + " 只）",
    '<div class="muted">将按顺序对以下标的依次研判，单只失败不会中断后面的：</div>' +
    '<div class="cards-3" style="margin:10px 0">' + codes.map(c =>
      '<div class="mini-card"><b>' + esc(c) + "</b></div>").join("") + "</div>" + warn +
    '<div class="row" style="margin-top:16px;justify-content:flex-end;flex-wrap:wrap">' +
    '<button class="btn ghost" id="ba-cancel">取消</button>' +
    '<button class="btn ghost" id="ba-noreview" title="跳过复核档，省一半调用">开始（跳过复核，省一半）</button>' +
    '<button class="btn primary" id="ba-go">开始（含复核）</button></div>');
  $("#ba-cancel").addEventListener("click", closeModal);
  $("#ba-noreview").addEventListener("click", () => { closeModal(); startBatch(true, kind); });
  $("#ba-go").addEventListener("click", () => { closeModal(); startBatch(false, kind); });
}

export function showSymbolPicker() {
  const items = (State.state && State.state["代码候选"]) || [];
  if (!items.length) { toast("没有可用的标的候选", "warn"); return; }
  openModal("选择标的",
    '<div class="muted" style="margin-bottom:10px">来源包括 持仓 / 自选 / 行情缓存 / 历史记录；带 <span class="chip ok">已缓存</span> 的可以直接配合「复用已有数据」零成本试跑。</div>' +
    '<div class="cards-3">' + items.map(it =>
      '<button class="btn" data-code="' + esc(it["thscode"] || it["代码"]) + '" style="justify-content:flex-start">' +
      "<b>" + esc(it["代码"]) + "</b>&nbsp;" + esc(it["名称"] || "") +
      (it["在最新stock3d"] ? ' <span class="chip ok">已缓存</span>' : "") +
      '<span class="muted" style="margin-left:auto">' + esc(it["来源"]) + "</span></button>").join("") + "</div>");
  $$("#modal-body [data-code]").forEach(b => b.addEventListener("click", () => {
    $("#code").value = b.dataset.code;
    closeModal();
    syncRunCmd();
  }));
}

/* 注册给 core/app.js：切到本页时按需加载 / 对外暴露的动作。 */
registerView("run", {
  syncCmd: syncRunCmd,
  checkReadiness: checkRunReadiness,
  showBatchDialog: showBatchDialog,
});
