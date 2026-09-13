/* 标的跟踪页：跟踪清单、每日计划生成、执行记录录入、历史时间线。
 *
 * 运行方式与「运行研判 / 荐股」一致：POST /api/jobs {kind:"track"} + 轮询日志。
 * 生成只读市场数据、只写 data/ai/track/；执行记录是纯人工录入，不做自动判定。
 */
import { api } from "../core/api.js";
import { openReport, registerView, showView } from "../core/app.js";
import { $, $$, esc, toast } from "../core/util.js";
import { confirmModal } from "../ui/modal.js";
import { trackDetailHtml, trackListHtml } from "../ui/trackcards.js";

export const Track = {data: null, detail: null, code: null, jobId: null, timer: null,
                      from: 0, running: false, checked: null, lastRefresh: 0};


/* ---------------- 日志 ---------------- */

function logAppend(line) {
  const pre = $("#track-console");
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
  const pre = $("#track-console");
  if (!pre) return;
  pre.dataset.clean = "1";
  pre.innerHTML = msg ? '<span class="l-dim">' + esc(msg) + "</span>" : "";
}

function setRunning(on) {
  Track.running = on;
  const gen = $("#btn-track-generate");
  if (gen) gen.disabled = on;
  const nudge = $("#btn-track-nudge");
  if (nudge) nudge.disabled = on;
  const stop = $("#btn-track-stop");
  if (stop) stop.hidden = !on;
}


/* ---------------- 清单 ---------------- */

function setBadge(pending, needExec) {
  const el = $("#nav-track-badge");
  if (!el) return;
  const n = (pending || []).length;
  if (n > 0) {
    el.textContent = String(n);
    el.hidden = false;
    el.title = "有 " + n + " 只标的的今日计划还没生成";
  } else if ((needExec || []).length > 0) {
    el.textContent = "!";
    el.hidden = false;
    el.title = "有 " + needExec.length + " 只标的还没录入上一份计划的执行情况";
  } else {
    el.hidden = true;
    el.textContent = "";
    el.title = "";
  }
}

function syncChecked(cards) {
  const codes = cards.map(c => c["代码"]);
  if (Track.checked === null) {
    Track.checked = new Set(codes);
    return;
  }
  codes.forEach(c => { if (!Track.checked.has(c)) Track.checked.add(c); });
  for (const c of Array.from(Track.checked)) {
    if (codes.indexOf(c) < 0) Track.checked.delete(c);
  }
}

export function renderTrack() {
  const d = Track.data || {};
  const cards = d["清单"] || [];
  syncChecked(cards);
  $("#track-table").innerHTML = trackListHtml(cards, Track.checked);
  $("#track-path").textContent = d["路径"] || "";
  $("#track-day").textContent = "适用交易日 " + (d["适用交易日"] || "—");
  const note = d["刷新"] || {};
  const bits = [(note["来源"] || "—") + " · " + (note["时间"] || "")];
  if (d["目录"]) bits.push("产物 " + d["目录"]);
  $("#track-refresh-note").textContent = bits.join(" ｜ ");
  const pending = d["待生成"] || [];
  const needExec = d["待录入执行"] || [];
  const nudge = $("#track-nudge");
  if (pending.length) {
    nudge.hidden = false;
    $("#track-nudge-text").textContent =
      "今日计划未生成：" + pending.join("、") + "（" + (d["适用交易日"] || "") + "）";
  } else if (needExec.length) {
    nudge.hidden = false;
    $("#track-nudge-text").textContent =
      "还没录入上一份计划的执行情况：" + needExec.join("、") +
      "——先填执行记录，再生成下一份计划";
  } else {
    nudge.hidden = true;
  }
  const maxChars = $("#track-maxchars");
  if (maxChars && !maxChars.value) {
    maxChars.value = d["最大字符默认"] || 30000;
    maxChars.min = d["最大字符下限"] || 8000;
    maxChars.max = d["最大字符上限"] || 200000;
  }
  const ctl = $("#track-nudge-ctl");
  if (ctl) ctl.textContent = pending.length ? "一键生成" : "去填写执行记录";
  (note["提示"] || []).forEach(h => toast(h, "warn"));
  setBadge(pending, needExec);
}

export async function loadTrack(refresh) {
  try {
    const d = await api("/api/track/all" + (refresh ? "?refresh=1" : ""));
    Track.data = d;
    if (refresh) Track.lastRefresh = Date.now();
    renderTrack();
  } catch (e) {
    toast("读取跟踪清单失败：" + e.message, "bad");
  }
}


/* ---------------- 详情 ---------------- */

export async function openTrackDetail(code, refresh) {
  Track.code = code;
  try {
    const d = await api("/api/track?code=" + encodeURIComponent(code) + (refresh ? "&refresh=1" : ""));
    Track.detail = d;
    $("#track-detail").innerHTML = trackDetailHtml(d);
    $("#track-detail-head").textContent = (d["名称"] || code) + " · " + (d["适用交易日"] || "");
    const one = $("#btn-track-gen-one");
    if (one) one.hidden = false;
    bindDetail();
  } catch (e) {
    $("#track-detail").innerHTML = '<div class="fail">读取详情失败：' + esc(e.message) + "</div>";
  }
}

function bindDetail() {
  $$("#track-detail [data-plan]").forEach(a => a.addEventListener("click", e => {
    e.preventDefault();
    openReport(a.dataset.plan);
  }));
  $$("#track-detail [data-del]").forEach(b => b.addEventListener("click", () => deleteTrackPlan(b.dataset.del)));
  const none = $("#btn-track-exec-none");
  if (none) none.addEventListener("click", () => {
    $$("#track-exec tbody tr[data-no] select[data-state]").forEach(sel => { sel.value = "未执行"; });
  });
  const save = $("#btn-track-exec-save");
  if (save) save.addEventListener("click", saveTrackExec);
  const planBtn = $("#btn-track-gen-one");
  if (planBtn) planBtn.addEventListener("click", () => {
    if (Track.detail && Track.detail["需录入执行记录"]) {
      toast("先生成计划前请把上一份计划的执行情况填好并保存", "bad");
      return;
    }
    startTrack([Track.code]);
  });
}


/* ---------------- 执行记录 ---------------- */

export function collectExec() {
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
  } catch (e) {
    toast("保存失败：" + e.message, "bad");
  }
}


/* ---------------- 生成 ---------------- */

export async function startTrack(codes) {
  if (Track.running) return;
  const list = (codes || []).filter(Boolean);
  if (!list.length) { toast("先勾选要生成的标的", "bad"); return; }
  const body = {kind: "track", codes: list, profile: ($("#track-profile") || {}).value || "",
                review: !!($("#track-review") && $("#track-review").checked),
                max_chars: ($("#track-maxchars") || {}).value || ""};
  logClear("正在启动 …");
  setRunning(true);
  try {
    const res = await api("/api/jobs", {method: "POST", body: JSON.stringify(body)});
    if (!res.ok) throw new Error(res.error || "启动失败");
    Track.jobId = res.id;
    Track.from = 0;
    $("#track-cmd").textContent = res["命令"] || "";
    $("#track-status").className = "chip accent";
    $("#track-status").innerHTML = '<span class="spinner"></span> 运行中';
    pollTrack();
  } catch (e) {
    toast(e.message, "bad");
    setRunning(false);
    $("#track-status").className = "chip bad";
    $("#track-status").textContent = "启动失败";
  }
}

export async function pollTrack() {
  if (!Track.jobId) return;
  try {
    const j = await api("/api/jobs/" + Track.jobId + "?from=" + Track.from);
    (j.lines || []).forEach(logAppend);
    Track.from = j.next;
    if (j.status === "running") {
      $("#track-status").innerHTML = '<span class="spinner"></span> 运行中 ' + j.elapsed + "s";
      clearTimeout(Track.timer);
      Track.timer = setTimeout(pollTrack, 700);
      return;
    }
    setRunning(false);
    const r = j.result || {};
    if (j.status === "canceled") {
      $("#track-status").className = "chip warn";
      $("#track-status").textContent = "已中断";
      toast("已中断，未落盘的标的没有产物", "warn");
    } else {
      const done = (r["生成"] || []).length;
      const skip = (r["跳过"] || []).length;
      const fail = (r["失败"] || []).length;
      $("#track-status").className = "chip " + (fail ? "bad" : "ok");
      $("#track-status").textContent = "完成 · 生成 " + done + " / 跳过 " + skip + " / 失败 " + fail +
        " · " + j.elapsed + "s";
      if (fail) toast("有 " + fail + " 只失败，看日志里的 [FAIL] 一行", "bad");
      else if (skip) toast("有 " + skip + " 只因未录执行记录被跳过", "warn");
      else if (done) toast("已生成 " + done + " 份计划", "ok");
    }
    await loadTrack(false);
    if (Track.code) await openTrackDetail(Track.code, false);
    const first = (r["生成"] || []).map(x => x["代码"]);
    if (!Track.checked) Track.checked = new Set();
    first.forEach(c => Track.checked.add(c));
  } catch (e) {
    setRunning(false);
    toast("轮询失败：" + e.message, "bad");
  }
}

export async function stopTrack() {
  if (!Track.jobId) return;
  try { await api("/api/jobs/" + Track.jobId + "/cancel", {method: "POST", body: "{}"}); }
  catch (e) { toast(e.message, "bad"); }
}


/* ---------------- 清单增删 ---------------- */

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
    Track.checked.add(code.replace(/[^0-9]/g, "").slice(0, 6));
    await loadTrack(false);
  } catch (e) { $("#track-msg").textContent = e.message; }
}

export async function removeTrack(code) {
  confirmModal("从跟踪清单移除？", "将把 " + code + " 从跟踪标的.md 中删除（文件有 .bak 备份）。" +
    "已生成的计划产物保留在 data/ai/track/ 里，可在历史里单独删除。", async () => {
    try {
      const res = await api("/api/tracklist/remove", {method: "POST", body: JSON.stringify({code: code})});
      if (!res.ok) { toast(res.error, "bad"); return; }
      toast("已移除 " + code, "ok");
      await loadTrack(false);
    } catch (e) { toast(e.message, "bad"); }
  }, "确认移除");
}

export async function importWatchlist() {
  try {
    const res = await api("/api/tracklist/import-watchlist", {method: "POST", body: "{}"});
    if (!res.ok) { toast(res.error, "bad"); return; }
    toast(res["新增"] ? "已从自选股导入 " + res["新增"] + " 只" : "自选股里的标的都已在跟踪清单里",
          res["新增"] ? "ok" : "warn");
    await loadTrack(false);
  } catch (e) { toast(e.message, "bad"); }
}

export async function deleteTrackPlan(path) {
  confirmModal("删除这份计划产物？", "产物会移入回收站（data/ai/.trash/），超过 7 天才真正删除，" +
    "期间可以从「历史与复盘」页搬回。", async () => {
    try {
      const res = await api("/api/track/delete", {method: "POST", body: JSON.stringify({path: path})});
      if (!res.ok) { toast(res.error, "bad"); return; }
      toast("已移入回收站", "ok");
      await openTrackDetail(Track.code, false);
      await loadTrack(false);
    } catch (e) { toast(e.message, "bad"); }
  }, "确认删除");
}


/* ---------------- 交互绑定 ---------------- */

export function initTrackView() {
  const add = $("#btn-track-add");
  if (add) add.addEventListener("click", addTrack);
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
    const codes = (Track.data && Track.data["清单"] || []).map(c => c["代码"])
      .filter(c => Track.checked.has(c));
    const need = (Track.data && Track.data["清单"] || [])
      .filter(c => Track.checked.has(c["代码"]) && c["需要执行记录"]).map(c => c["代码"]);
    if (!codes.length) { toast("先勾选要生成的标的", "bad"); return; }
    const go = () => startTrack(codes);
    if (need.length) {
      confirmModal("有标的还没录执行记录",
        need.join("、") + " 还没录入上一份计划的执行情况，生成时会被跳过（日志里标 [WARN]）。仍要生成其余标的吗？",
        go, "继续生成");
      return;
    }
    go();
  });
  const nudge = $("#btn-track-nudge");
  if (nudge) nudge.addEventListener("click", () => {
    const pending = (Track.data && Track.data["待生成"]) || [];
    if (pending.length) { startTrack(pending); return; }
    const needFirst = ((Track.data && Track.data["待录入执行"]) || [])[0];
    if (needFirst) openTrackDetail(needFirst, false);
  });
  const stop = $("#btn-track-stop");
  if (stop) stop.addEventListener("click", stopTrack);
  const adv = $("#btn-track-advanced");
  if (adv) adv.addEventListener("click", () => {
    const box = $("#track-adv");
    box.hidden = !box.hidden;
  });
  const table = $("#track-table");
  if (table) table.addEventListener("change", e => {
    const code = e.target && e.target.dataset ? e.target.dataset.check : null;
    if (!code) return;
    if (e.target.checked) Track.checked.add(code); else Track.checked.delete(code);
  });
  if (table) table.addEventListener("click", e => {
    const t = e.target;
    if (!t || !t.dataset) return;
    if (t.dataset.detail) { e.preventDefault(); openTrackDetail(t.dataset.detail, true); }
    else if (t.dataset.open) { e.preventDefault(); openTrackDetail(t.dataset.open, false); }
    else if (t.dataset.gen) {
      e.preventDefault();
      const card = (Track.data["清单"] || []).find(c => c["代码"] === t.dataset.gen) || {};
      if (card["需要执行记录"]) {
        toast("先录入上一份计划的执行情况，再生成下一份", "bad");
        openTrackDetail(t.dataset.gen, false);
        return;
      }
      startTrack([t.dataset.gen]);
    } else if (t.dataset.untrack) { removeTrack(t.dataset.untrack); }
  });
}

/* 注册给 core/app.js：切到本页自动刷一次实盘价（与总控台同一套批量报价缓存）。 */
registerView("track", {
  onShow: () => loadTrack(true),
  openDetail: openTrackDetail,
  open: () => { showView("track"); },
});
