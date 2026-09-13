/* 荐股页：筛选树、运行、矩阵与候选表。 */
import { api } from "../core/api.js";
import { State, registerView, showView, viewApi } from "../core/app.js";
import { $, $$, badge, chip, dirColor, esc, fmt, fmtPct, has, num, pctClass, toast } from "../core/util.js";
import { drawKline } from "../ui/kline.js";
import { closeModal, confirmModal, openModal } from "../ui/modal.js";
import { PICK_MODULE_CYCLE, pickApplyModules, pickLoadModules, pickModules,
         pickSaveModules, pickPrefill } from "../ui/pickfilter.js";

export function pickLoadBoards(force) {
  Pick.boardsMsg = "板块数据加载中…";
  pickRenderLists();
  return api("/api/pick/boards" + (force ? "?refresh=1" : ""))
    .then(res => {
      Pick.boards = res;
      Pick.boardsMsg = (res["错误"] && res["错误"].length) ? ("部分板块数据降级：" + res["错误"].join("；")) : "";
      pickRenderLists();
      return res;
    })
    .catch(e => {
      Pick.boardsMsg = "板块数据加载失败：" + e.message;
      pickRenderLists();
      return null;
    });
}/* =========================================================================
   荐股（先筛选板块 → 本地机械打分 + 一次模型点评）
   ========================================================================= */

export const Pick = {
  jobId: null, timer: null, from: 0, data: null, kline: null,
  tab: "industry", boards: null, boardsMsg: "",
  subs: {}, industry: {}, concepts: {}, filter: "", themeOpen: {},
};

export { PICK_MODULE_CYCLE };

export const PICK_CONCEPT_CAP = 120;

export function pickMatch(name) {
  if (!Pick.filter) return true;
  return String(name || "").toLowerCase().includes(Pick.filter);
}

export function pickCounts() {
  const ind = Object.keys(Pick.industry);
  let subs = 0, unknown = 0;
  ind.forEach(code => {
    const it = Pick.industry[code];
    if (it.subs === null) {
      const st = Pick.subs[code];
      const n = st && st["数据"] ? (st["数据"]["细分"] || []).length : 0;
      if (n) subs += n; else unknown += 1;
    } else {
      subs += it.subs.size;
    }
  });
  return { ind: ind.length, subs: subs, unknown: unknown,
           con: Object.keys(Pick.concepts).length };
}

export function pickSyncHint() {
  const c = pickCounts();
  const per = num($("#pick-per-board").value) || 5;
  const cap = num($("#pick-max-candidates").value) || 400;
  const boards = c.subs + c.con;
  const sel = $("#pick-sel-count");
  if (sel) {
    sel.textContent = "已选：行业 " + c.ind + " 个（已知细分 " + c.subs +
      (c.unknown ? "，另有 " + c.unknown + " 个行业未展开细分" : "") + "）｜ 概念 " + c.con + " 个";
  }
  const hint = $("#pick-hint");
  if (hint) {
    if (!boards && !c.unknown) {
      hint.innerHTML = "<b>还没选板块</b>：在左边按「行业 → 细分」或「概念 → 主题」勾选，" +
        "也可以点「热度前 10」或「全选本页」。默认不选是故意的——只抓你选的，未选的不拉取。";
    } else {
      const known = Math.max(boards, c.ind);
      const sec = Math.max(10, Math.round(known * 1.6 + 60));
      const text = c.unknown
        ? "≥ " + known + " 个板块（含 " + c.unknown + " 个行业未展开细分，按实际细分展开）"
        : known + " 个板块";
      const mods = pickModules();
      hint.innerHTML = "<b>将抓取 " + text + "</b>（每板块前 " + per + " 只，候选上限 " + cap +
        "）→ <b>1 次模型调用</b>（profile 的研判档）<br>模块：" +
        esc(mods.join(" / ") || "—") + "（" + mods.length + " 个，各自一套权重、各出一张候选表）<br>粗略预计 " +
        (sec < 90 ? "约 " + sec + " 秒" : "约 " + Math.round(sec / 60) + " 分钟") +
        "；机械榜一定落盘，模型失败会如实标注。";
    }
  }
}

export function pickRenderLists() {
  const panelI = $("#pick-panel-industry"), panelC = $("#pick-panel-concept");
  if (!panelI || !panelC) return;
  $$("#pick-tabs button").forEach(b => b.classList.toggle("on", b.dataset.tab === Pick.tab));
  panelI.hidden = Pick.tab !== "industry";
  panelC.hidden = Pick.tab !== "concept";
  const msg = $("#pick-filter-msg");
  if (msg) msg.textContent = Pick.boardsMsg || "";
  if (!Pick.boards) {
    panelI.innerHTML = '<div class="empty">板块数据加载中…</div>';
    panelC.innerHTML = '<div class="empty">板块数据加载中…</div>';
    pickSyncHint();
    return;
  }
  if (Pick.tab === "industry") {
    panelI.innerHTML = pickIndustryHtml();
  } else {
    panelC.innerHTML = pickConceptHtml();
  }
  pickBindListEvents();
  pickSyncHint();
}

export function pickIndustryHtml() {
  const rows = (Pick.boards || {})["一级行业"] || [];
  const shown = rows.filter(it => {
    if (pickMatch(it["名称"])) return true;
    const st = Pick.subs[it["代码"]] || {};
    return ((st["数据"] || {})["细分"] || []).some(s => pickMatch(s["名称"]));
  });
  if (!shown.length) return '<div class="empty">没有匹配的行业。</div>';
  let html = "";
  shown.forEach(it => {
    if (!it["可用"]) {
      html += '<div class="pick-row"><span></span><div><b>' + esc(it["名称"]) +
        '</b><div class="hint">东财板块表里暂时找不到（可能改名）</div></div><span></span></div>';
      return;
    }
    const code = it["代码"], sel = Pick.industry[code], st = Pick.subs[code] || {};
    let tag = "";
    if (sel) tag = sel.subs === null ? chip("全部细分", "accent") : chip("细分 " + sel.subs.size + " 个", "accent");
    const subInfo = st["载入中"] ? "细分加载中…"
      : (st["数据"] ? ((st["数据"]["细分"] || []).length + " 个细分 ｜ " + (st["数据"]["股票数"] || 0) + " 只成分")
        : (st["错误"] ? ("细分加载失败：" + st["错误"]) : "点「细分」加载二级行业"));
    html += '<div class="pick-row">' +
      '<input type="checkbox" data-ind="' + esc(code) + '"' + (sel ? " checked" : "") + " />" +
      "<div><b>" + esc(it["名称"]) + "</b> " + tag +
      '<div class="hint pick-num">' + fmtPct(it["涨跌幅_pct"]) + " ｜ 净占比 " +
      fmt(it["主力净占比_pct"], 2) + "% ｜ 热度 " + fmt(it["热度"], 1) + "</div>" +
      '<div class="hint">' + esc(subInfo) + "</div></div>" +
      '<button class="btn sm ghost" data-exp="' + esc(code) + '">' +
      (st["展开"] ? "收起" : "细分") + "</button></div>";
    if (st["展开"]) {
      const subs = ((st["数据"] || {})["细分"] || []).filter(s => pickMatch(s["名称"]));
      if (!subs.length) {
        html += '<div class="pick-sub"><span></span><span class="muted">没有匹配的细分。</span><span></span></div>';
      }
      subs.forEach(s => {
        const on = sel ? (sel.subs === null || sel.subs.has(s["名称"])) : false;
        html += '<div class="pick-sub">' +
          '<input type="checkbox" data-sub="' + esc(code) + '" data-subname="' + esc(s["名称"]) + '"' +
          (on ? " checked" : "") + " />" +
          "<span>" + esc(s["名称"]) + ' <span class="muted">' + (s["股票数"] || 0) + " 只</span></span>" +
          '<span class="muted pick-num">' + fmtPct(s["涨跌幅_pct"]) + " ｜ " +
          esc(s["行情口径"] || "") + "</span></div>";
      });
    }
  });
  return html;
}

export function pickConceptHtml() {
  const items = (Pick.boards || {})["概念"] || [];
  const themes = ((Pick.boards || {})["主题"] || []).map(t => t["主题"]);
  const byTheme = {};
  items.forEach(c => {
    const t = c["主题"] || "其他";
    (byTheme[t] = byTheme[t] || []).push(c);
  });
  let html = "";
  themes.forEach(theme => {
    const all = byTheme[theme] || [];
    const list = all.filter(c => pickMatch(c["名称"]));
    if (!list.length) return;
    const open = Pick.themeOpen[theme] === true || (!!Pick.filter && list.length > 0);
    const picked = list.filter(c => Pick.concepts[c["代码"]]).length;
    html += '<div class="pick-theme">' +
      '<input type="checkbox" data-theme="' + esc(theme) + '"' +
      (picked && picked === list.length ? " checked" : "") + " />" +
      "<span>" + esc(theme) + ' <span class="muted">' + list.length + " 个" +
      (picked ? " ｜ 已选 " + picked : "") + "</span></span>" +
      '<div class="spacer"></div>' +
      '<button class="btn sm ghost" data-theme-toggle="' + esc(theme) + '">' + (open ? "收起" : "展开") + "</button></div>";
    if (!open) return;
    list.forEach(c => {
      html += '<div class="pick-sub">' +
        '<input type="checkbox" data-con="' + esc(c["代码"]) + '"' +
        (Pick.concepts[c["代码"]] ? " checked" : "") + " />" +
        "<span>" + esc(c["名称"]) + "</span>" +
        '<span class="muted pick-num">' + fmtPct(c["涨跌幅_pct"]) + " ｜ 净占比 " +
        fmt(c["主力净占比_pct"], 2) + "% ｜ 热度 " + fmt(c["热度"], 1) + "</span></div>";
    });
  });
  return html || '<div class="empty">没有匹配的概念。</div>';
}

export function pickBindListEvents() {
  $$("#pick-panel-industry [data-ind]").forEach(cb => cb.addEventListener("change", () => {
    const code = cb.dataset.ind;
    if (cb.checked) {
      const it = pickFindL1(code) || {};
      Pick.industry[code] = {名称: it["名称"] || code, subs: null};
      const st = Pick.subs[code];
      if (!st || !st["数据"]) pickLoadSubs(code);
    } else {
      delete Pick.industry[code];
    }
    pickRenderLists();
  }));
  $$("#pick-panel-industry [data-exp]").forEach(btn => btn.addEventListener("click", () => {
    const code = btn.dataset.exp;
    const st = Pick.subs[code] || {};
    st["展开"] = !st["展开"];
    Pick.subs[code] = st;
    if (st["展开"] && !st["数据"] && !st["载入中"]) { pickLoadSubs(code); return; }
    pickRenderLists();
  }));
  $$("#pick-panel-industry [data-sub]").forEach(cb => cb.addEventListener("change", () => {
    const code = cb.dataset.sub, name = cb.dataset.subname;
    if (!Pick.industry[code]) Pick.industry[code] = {名称: (pickFindL1(code) || {})["名称"] || code, subs: new Set()};
    const sel = Pick.industry[code];
    if (sel.subs === null) {
      const all = (((Pick.subs[code] || {})["数据"] || {})["细分"] || []).map(s => s["名称"]);
      sel.subs = new Set(all);
    }
    if (cb.checked) sel.subs.add(name); else sel.subs.delete(name);
    if (!sel.subs.size) delete Pick.industry[code];
    pickRenderLists();
  }));
  $$("#pick-panel-concept [data-con]").forEach(cb => cb.addEventListener("change", () => {
    const code = cb.dataset.con;
    if (cb.checked) Pick.concepts[code] = true; else delete Pick.concepts[code];
    pickRenderLists();
  }));
  $$("#pick-panel-concept [data-theme]").forEach(cb => cb.addEventListener("change", () => {
    const theme = cb.dataset.theme;
    const list = ((Pick.boards || {})["概念"] || []).filter(c => (c["主题"] || "其他") === theme);
    list.forEach(c => { if (cb.checked) Pick.concepts[c["代码"]] = true; else delete Pick.concepts[c["代码"]]; });
    pickRenderLists();
  }));
  $$("#pick-panel-concept [data-theme-toggle]").forEach(btn => btn.addEventListener("click", () => {
    const theme = btn.dataset.themeToggle;
    Pick.themeOpen[theme] = !(Pick.themeOpen[theme] === true || !!Pick.filter);
    pickRenderLists();
  }));
}

export function pickFindL1(code) {
  return ((Pick.boards || {})["一级行业"] || []).find(x => x["代码"] === code);
}

export async function pickLoadSubs(code, force) {
  const st = Pick.subs[code] || {};
  st["载入中"] = true;
  st["错误"] = "";
  st["展开"] = true;
  Pick.subs[code] = st;
  pickRenderLists();
  try {
    const res = await api("/api/pick/industry?code=" + encodeURIComponent(code) + (force ? "&refresh=1" : ""));
    Pick.subs[code] = {载入中: false, 错误: "", 展开: true, 数据: res};
  } catch (e) {
    Pick.subs[code] = {载入中: false, 错误: e.message, 展开: true, 数据: null};
  }
  pickRenderLists();
}

export function pickSelectHot() {
  if (!Pick.boards) { toast("板块数据还没加载完", "warn"); return; }
  const byHot = (arr) => arr.slice().sort((a, b) => (num(b["热度"]) || -1) - (num(a["热度"]) || -1));
  const inds = byHot(((Pick.boards || {})["一级行业"] || []).filter(x => x["可用"])).slice(0, 5);
  inds.forEach(x => { Pick.industry[x["代码"]] = {名称: x["名称"], subs: null}; });
  const cons = byHot((Pick.boards || {})["概念"] || []).slice(0, 5);
  cons.forEach(c => { Pick.concepts[c["代码"]] = true; });
  const note = $("#pick-selectall-note");
  if (note) note.textContent = "";
  toast("已选热度前 5 行业（全部细分）+ 热度前 5 概念", "ok");
  pickRenderLists();
}

export function pickClearSel() {
  Pick.industry = {};
  Pick.concepts = {};
  const note = $("#pick-selectall-note");
  if (note) note.textContent = "";
  pickRenderLists();
}

export function pickSelectAll() {
  if (!Pick.boards) { toast("板块数据还没加载完", "warn"); return; }
  const note = $("#pick-selectall-note");
  if (Pick.tab === "industry") {
    const rows = ((Pick.boards || {})["一级行业"] || [])
      .filter(x => x["可用"] && pickMatch(x["名称"]));
    if (!rows.length) { toast("没有匹配的行业", "warn"); return; }
    if (rows.length > 6 && !Pick._allConfirmed) {
      confirmModal("全选 " + rows.length + " 个行业？",
        "选中一级行业会连带它的全部二级细分（31 个行业合计约 128 个细分）。" +
        "这会让抓取时间和候选池明显变大：建议先把「候选上限」设小一点，" +
        "或者全选后再展开某个行业、取消个别细分。",
        () => { Pick._allConfirmed = true; pickSelectAll(); }, "仍然全选");
      return;
    }
    rows.forEach(x => { Pick.industry[x["代码"]] = {名称: x["名称"], subs: null}; });
    if (note) note.textContent = "已全选 " + rows.length + " 个行业（全部细分；展开后可取消个别细分）";
    toast("已全选 " + rows.length + " 个行业", "ok");
  } else {
    const list = ((Pick.boards || {})["概念"] || []).filter(c => pickMatch(c["名称"]));
    if (!list.length) { toast("没有匹配的概念", "warn"); return; }
    if (list.length > PICK_CONCEPT_CAP) {
      toast("概念最多选 " + PICK_CONCEPT_CAP + " 个（当前匹配 " + list.length +
            " 个）：先用搜索缩小范围再全选", "bad");
      return;
    }
    list.forEach(c => { Pick.concepts[c["代码"]] = true; });
    if (note) note.textContent = "已全选本页 " + list.length + " 个概念";
    toast("已全选 " + list.length + " 个概念", "ok");
  }
  Pick._allConfirmed = false;
  pickRenderLists();
}

export function pickConsoleClear(msg) {
  const pre = $("#pick-console");
  if (!pre) return;
  pre.dataset.clean = "1";
  pre.innerHTML = msg ? '<span class="l-dim">' + esc(msg) + "</span>" : "";
}

export function pickConsoleAppend(line) {
  const pre = $("#pick-console");
  if (!pre) return;
  if (pre.dataset.clean !== "1") { pre.innerHTML = ""; pre.dataset.clean = "1"; }
  const t = (line.text || "").trim();
  let cls = "";
  if (t.startsWith("[OK]")) cls = "l-ok";
  else if (t.startsWith("[WARN]")) cls = "l-warn";
  else if (t.startsWith("[FAIL]")) cls = "l-fail";
  else if (t.startsWith("[..]")) cls = "l-stage";
  const span = document.createElement("div");
  span.innerHTML = '<span class="l-ts">' + esc(line.t || "") + "</span>" +
    '<span class="' + cls + '">' + esc(line.text) + "</span>";
  pre.appendChild(span);
  pre.scrollTop = pre.scrollHeight;
}

export function setPickRunning(on) {
  const run = $("#btn-pick-run"), stop = $("#btn-pick-stop");
  if (run) { run.disabled = on; run.textContent = on ? "荐股中…" : "开始荐股"; }
  if (stop) stop.disabled = !on;
}

export async function startPick() {
  const mods = $$("#pick-modules button.on").map(b => b.dataset.module);
  if (!mods.length) { toast("至少选一个模块", "bad"); return; }
  const industry = Object.keys(Pick.industry).map(code => {
    const it = Pick.industry[code];
    return { code: code, name: it["名称"] || code, subs: it.subs === null ? [] : Array.from(it.subs) };
  });
  const concepts = Object.keys(Pick.concepts);
  if (!industry.length && !concepts.length) { toast("请先筛选行业或概念：至少勾选一个细分或一个概念", "bad"); return; }
  if (concepts.length > PICK_CONCEPT_CAP) {
    toast("概念最多选 " + PICK_CONCEPT_CAP + " 个，当前 " + concepts.length + " 个", "bad");
    return;
  }
  const body = {
    kind: "pick",
    modules: mods,
    industry: industry,
    concepts: concepts,
    per_board: num($("#pick-per-board").value) || 5,
    max_candidates: num($("#pick-max-candidates").value) || 400,
    max_chars: num($("#pick-max-chars").value) || 40000,
    profile: ($("#pick-profile").value || "").trim(),
    model_pro: ($("#pick-model").value || "").trim(),
    api_base: ($("#pick-api-base").value || "").trim(),
    api_key: ($("#pick-api-key").value || "").trim(),
    exclude: {
      st: $("#pick-ex-st").checked, "new": $("#pick-ex-new").checked,
      low_price: $("#pick-ex-price").checked, low_amount: $("#pick-ex-amount").checked,
      skip_688_bj: $("#pick-ex-688").checked,
    },
  };
  try {
    const res = await api("/api/jobs", { method: "POST", body: JSON.stringify(body) });
    if (!res.ok) { toast(res.error, "bad"); return; }
    Pick.jobId = res.id;
    Pick.from = 0;
    pickConsoleClear("任务已启动：" + res["命令"]);
    $("#pick-cmd").textContent = res["命令"] || "";
    $("#pick-job-state").textContent = "运行中…";
    setPickRunning(true);
    pollPick();
  } catch (e) { toast(e.message, "bad"); }
}

export async function pollPick() {
  if (!Pick.jobId) return;
  let j;
  try {
    j = await api("/api/jobs/" + Pick.jobId + "?from=" + Pick.from);
  } catch (e) {
    clearTimeout(Pick.timer);
    Pick.timer = setTimeout(pollPick, 2500);
    return;
  }
  (j.lines || []).forEach(pickConsoleAppend);
  Pick.from = j.next == null ? Pick.from : j.next;
  const label = { running: "运行中", done: "完成", failed: "失败", canceled: "已中断" };
  $("#pick-job-state").textContent = (label[j.status] || j.status) + " · " + j.elapsed + "s";
  if (j.status === "running") {
    clearTimeout(Pick.timer);
    Pick.timer = setTimeout(pollPick, 2000);
    return;
  }
  setPickRunning(false);
  const result = j.result || {};
  if (j.status === "canceled") {
    toast("已中断（未写入产物）", "warn");
  } else if (j.status === "failed") {
    toast("荐股失败：看执行日志里的 [FAIL] 一行" + (result["诊断"] ? "；" + result["诊断"] : ""), "bad");
  } else if (result["模型错误"]) {
    toast("模型点评失败，已保存机械榜：" + result["模型错误"], "warn");
  } else {
    toast("荐股完成", "ok");
  }
  if (result.pick) await loadPick(result.pick);
}

export async function stopPick() {
  if (!Pick.jobId) return;
  try {
    const res = await api("/api/jobs/" + Pick.jobId + "/cancel", { method: "POST", body: "{}" });
    if (!res.ok) { toast("中断请求未生效（任务可能已结束）", "warn"); return; }
    toast("已请求中断，当前步骤结束后停止", "warn");
  } catch (e) { toast(e.message, "bad"); }
}

export async function loadPick(path) {
  const box = $("#pick-result");
  if (!box) return;
  let res;
  try {
    res = await api("/api/pick" + (path ? "?path=" + encodeURIComponent(path) : ""));
  } catch (e) { toast("读取荐股结果失败：" + e.message, "bad"); return; }
  Pick.data = res;
  renderPick(res);
}

export function pickCell(v, d, pct) {
  const n = num(v);
  if (n === null) return '<td class="num muted">—</td>';
  return '<td class="num' + (pct ? " " + pctClass(n) : "") + '">' +
    (pct ? fmtPct(n, d) : fmt(n, d)) + "</td>";
}

export function pickGradeChip(g) {
  const cls = g === "强" ? "ok" : (g === "偏强" ? "accent" : (g === "弱" ? "bad" : "flat"));
  return badge(g || "—", cls);
}

export function pickMatrix(p) {
  const par = p["参数"] || {};
  const boards = p["板块"] || {};
  const types = Object.keys(boards).filter(k => (boards[k] || []).length);
  const cols = types.length ? types : (par["板块类型"] || []);
  const mods = par["模块"] || [];
  if (!mods.length || !cols.length) return "";
  let html = '<div class="card"><div class="card-h"><span>模块 × 板块矩阵</span>' +
    '<span class="muted">每格 = 该模块在该类板块下的首选（按机械分），点板块名跳到候选表</span></div>' +
    '<div class="card-b table-wrap"><table class="tbl"><thead><tr><th>模块</th>';
  cols.forEach(t => { html += "<th>" + esc(t) + "</th>"; });
  html += "</tr></thead><tbody>";
  mods.forEach(m => {
    html += "<tr><td><b>" + esc(m) + '</b><div class="hint">' +
      esc(PICK_MODULE_CYCLE[m] || "") + "</div></td>";
    cols.forEach(t => {
      const rows = ((p["候选"] || {})[m] || []).filter(r => r["板块类型"] === t)
        .sort((a, b) => (num(b["板块分"]) == null ? -1 : num(b["板块分"])) -
                        (num(a["板块分"]) == null ? -1 : num(a["板块分"])));
      if (!rows.length) { html += '<td class="muted">—</td>'; return; }
      const hit = rows[0];
      html += '<td><a href="#" data-jump="' + esc(m) + '">' + esc(hit["来源板块"] || "—") + "</a>" +
        '<div class="hint">板块分 ' + fmt(hit["板块分"], 1) + " ｜ 首位候选 " +
        esc(hit["名称"] || "") + "</div></td>";
    });
    html += "</tr>";
  });
  return html + "</tbody></table></div></div>";
}

export function pickBoardCards(p) {
  const evals = {};
  (p["板块评估"] || []).forEach(e => { evals[String(e["名称"] || "")] = e; });
  const boards = p["板块"] || {};
  let html = "";
  Object.keys(boards).forEach(type => {
    const rows = boards[type] || [];
    if (!rows.length) return;
    html += '<div class="card"><div class="card-h"><span>' +
      esc(type === "行业" ? "行业细分评估" : type + "板块评估") + "</span>" +
      '<span class="muted">共 ' + rows.length + " 个，按机械分排序</span></div>" +
      '<div class="card-b table-wrap"><table class="tbl"><thead><tr>' +
      "<th>名称</th><th>一级行业</th><th>主题</th><th class='num'>涨跌幅</th><th class='num'>5日</th>" +
      "<th class='num'>20日</th><th class='num'>主力净流入(亿)</th><th class='num'>净占比</th>" +
      "<th class='num'>涨/跌</th><th class='num'>成交额(亿)</th><th class='num'>机械分</th>" +
      "<th>评级</th><th>行情口径</th><th class='num'>入池</th><th>模型评估</th>" +
      "</tr></thead><tbody>";
    rows.forEach(b => {
      const e = evals[String(b["名称"] || "")] || {};
      html += "<tr><td><b>" + esc(b["名称"]) + '</b><div class="hint mono">' + esc(b["代码"]) +
        "</div></td>" +
        '<td class="muted">' + esc(b["一级行业"] || "—") + "</td>" +
        '<td class="muted">' + esc(b["主题"] || "—") + "</td>" +
        pickCell(b["涨跌幅_pct"], 2, true) + pickCell(b["5日_pct"], 2, true) +
        pickCell(b["20日_pct"], 2, true) + pickCell(b["主力净流入_亿"], 3, false) +
        pickCell(b["主力净占比_pct"], 2, true) +
        '<td class="num">' + (b["上涨家数"] == null ? "—" : esc(b["上涨家数"])) + "/" +
        (b["下跌家数"] == null ? "—" : esc(b["下跌家数"])) + "</td>" +
        pickCell(b["成交额_亿"], 1, false) + pickCell(b["机械分"], 1, false) +
        "<td>" + pickGradeChip(b["评级"]) + "</td>" +
        '<td class="muted">' + esc(b["行情口径"] || "—") + "</td>" +
        '<td class="num">' + (b["入池"] == null ? "—" : esc(b["入池"])) + "</td>" +
        '<td class="wrap">' + (e["驱动"] ? esc(e["驱动"]) +
          (e["风险"] ? '<div class="hint">风险：' + esc(e["风险"]) + "</div>" : "")
          : '<span class="muted">—</span>') + "</td></tr>";
    });
    html += "</tbody></table></div></div>";
  });
  return html;
}

export function pickPrices(f) {
  if (!f || !f["代码"]) return '<span class="muted">—</span>';
  const buy = f["关注买点"] || {}, stop = f["止损"] || {}, tg = f["目标位"] || [];
  let out = "";
  if (buy["价位"]) out += "<div><b>买点</b> " + esc(buy["价位"]) +
    '<div class="hint">' + esc(buy["依据"] || "") + "</div></div>";
  if (stop["价位"]) out += "<div><b>止损</b> " + esc(stop["价位"]) +
    '<div class="hint">' + esc(stop["依据"] || "") + "</div></div>";
  tg.forEach(t => {
    if (t["价位"]) out += "<div><b>目标</b> " + esc(t["价位"]) +
      '<div class="hint">' + esc(t["依据"] || "") + "</div></div>";
  });
  if (f["风险"]) out += '<div class="hint">风险：' + esc(f["风险"]) + "</div>";
  return out || '<span class="muted">—</span>';
}

export function pickModuleCards(p) {
  const par = p["参数"] || {};
  const modelMods = {};
  (p["模块"] || []).forEach(m => { modelMods[String(m["模块"] || "")] = m; });
  const mods = par["模块"] || Object.keys(p["候选"] || {});
  let html = "";
  mods.forEach(m => {
    const mm = modelMods[m] || {};
    const first = {};
    (mm["首推"] || []).forEach(c => { first[String(c["代码"] || "")] = c; });
    const rows = (p["候选"] || {})[m] || [];
    html += '<div class="card" id="pick-mod-' + esc(m) + '"><div class="card-h"><span>' +
      esc(m) + " · 候选</span>" + chip(PICK_MODULE_CYCLE[m] || "", "flat") +
      (mm["评分"] == null ? "" : chip("模型评分 " + mm["评分"], "accent")) +
      '<div class="spacer"></div><span class="muted">机械分前 ' + rows.length + " 只</span></div>";
    if (mm["逻辑"] || mm["介入节奏"] || mm["失效条件"]) {
      html += '<div class="card-b note">' +
        (mm["逻辑"] ? "<div><b>逻辑</b> " + esc(mm["逻辑"]) + "</div>" : "") +
        (mm["介入节奏"] ? "<div><b>介入节奏</b> " + esc(mm["介入节奏"]) + "</div>" : "") +
        (mm["失效条件"] ? "<div><b>失效条件</b> " + esc(mm["失效条件"]) + "</div>" : "") +
        "</div>";
    }
    html += '<div class="card-b table-wrap"><table class="tbl"><thead><tr>' +
      "<th>代码</th><th>名称</th><th>来源板块</th><th class='num'>现价</th><th class='num'>涨跌幅</th>" +
      "<th class='num'>5日</th><th class='num'>20日</th><th class='num'>换手</th><th class='num'>量比</th>" +
      "<th class='num'>主力净流入(亿)</th><th class='num'>机械分</th><th>模型结论</th>" +
      "<th>精确价位（含依据）</th><th>操作</th></tr></thead><tbody>";
    rows.forEach(r => {
      const f = first[String(r["代码"])] || {};
      html += "<tr><td><b>" + esc(r["代码"]) + "</b></td><td>" + esc(r["名称"]) + "</td>" +
        '<td class="muted">' + esc(r["来源板块"] || "") + "</td>" +
        pickCell(r["现价"], 2, false) + pickCell(r["涨跌幅_pct"], 2, true) +
        pickCell(r["5日_pct"], 2, true) + pickCell(r["20日_pct"], 2, true) +
        pickCell(r["换手率_pct"], 2, false) + pickCell(r["量比"], 2, false) +
        pickCell(r["主力净流入_亿"], 3, false) + pickCell(r["机械分"], 1, false) +
        "<td>" + (f["评级"] ? badge(f["评级"],
          f["评级"] === "关注" ? "ok" : (f["评级"] === "回避" ? "bad" : "flat"))
          : '<span class="muted">—</span>') +
        (f["理由"] ? '<div class="hint wrap">' + esc(f["理由"]) + "</div>" : "") + "</td>" +
        '<td class="wrap">' + pickPrices(f) + "</td>" +
        '<td><a href="#" data-pick-run="' + esc(r["代码"]) + '">研判</a> ' +
        '<a href="#" data-pick-kline="' + esc(r["代码"]) + '">K线</a> ' +
        '<button class="btn sm" data-pick-watch="' + esc(r["代码"]) + '" data-pick-watch-name="' +
        esc(r["名称"] || "") + '">加入自选</button></td></tr>';
    });
    html += "</tbody></table></div></div>";
  });
  return html;
}

export function renderPick(bundle) {
  const box = $("#pick-result");
  if (!box) return;
  const p = (bundle || {})["json"];
  if (!p) {
    box.innerHTML = '<div class="card"><div class="card-b"><div class="empty">' +
      "还没有荐股结果：在上面按行业细分或概念主题筛选，再点「开始荐股」。</div></div></div>";
    return;
  }
  const sum = p["总评"] || {}, model = p["模型层"] || {}, cfg = p["配置"] || {};
  const par = p["参数"] || {}, sel = par["筛选"] || {};
  const cost = (p["成本"] || {})["人民币_估算"];
  let html = '<div class="card"><div class="card-h"><span>总评</span>' +
    (sum["多空倾向"] ? chip("倾向 " + sum["多空倾向"], dirColor(sum["多空倾向"])) : "") +
    chip("最强模块 " + (sum["最强模块"] || "—")) +
    chip("最强板块 " + (sum["最强板块"] || "—")) +
    '<div class="spacer"></div>' +
    (bundle["md路径"] ? '<a class="btn ghost" href="/api/blob?path=' +
      encodeURIComponent(bundle["md路径"]) + '&download=1">下载 Markdown</a>' : "") +
    '<button class="btn ghost danger" id="btn-pick-del">删除本份</button></div><div class="card-b">' +
    '<div class="note">' + (sum["一句话"] ? esc(sum["一句话"])
      : '<span class="muted">本次没有模型总评（原因见下方），以下是本地机械打分榜。</span>') + "</div>" +
    (sum["操作节奏"] ? '<div class="hint">操作节奏：' + esc(sum["操作节奏"]) + "</div>" : "") +
    '<div class="hint">' + chip("生成 " + (p["生成时间"] || "—")) +
    chip("交易日 " + (p["交易日"] || "—")) +
    chip("候选池 " + (p["候选池数量"] || 0) + " 只") +
    chip("每板块 " + (par["每板块候选"] == null ? "—" : par["每板块候选"]) +
         " 只 ｜ 上限 " + (par["候选上限"] == null ? "—" : par["候选上限"])) +
    chip("模型 " + (cfg["model"] || "未点评"), cfg["model"] ? "" : "warn") +
    chip("费用 " + (cost == null ? "未配置单价" : "¥" + cost)) +
    chip("事实包 " + (p["factpack_chars"] || 0) + " 字符") +
    (p["latency_ms"] ? chip("模型耗时 " + Math.round(p["latency_ms"] / 1000) + "s") : "") + "</div>";
  if ((sel["行业"] || []).length || (sel["概念"] || []).length) {
    html += '<div class="hint">筛选：' +
      (sel["行业"] || []).map(x => "行业 " + esc(x["名称"]) +
        "（" + esc(Array.isArray(x["细分"]) ? (x["细分"].join("、") || "全部") : (x["细分"] || "全部")) +
        "）").join(" ｜ ") +
      ((sel["概念"] || []).length ? " ｜ 概念 " + (sel["概念"] || []).map(x => esc(x["名称"])).join("、") : "") +
      "</div>";
  }
  html += '<div class="hint mono">' + esc(bundle["json路径"] || "") + "</div></div></div>";
  if (model["error"]) {
    html += '<div class="card"><div class="card-b"><div class="err">[WARN] 本次没有模型点评：' +
      esc(model["error"]) + "（机械榜仍然有效；改好 Key/网络后可重跑）</div></div></div>";
  }
  html += pickMatrix(p) + pickBoardCards(p) + pickModuleCards(p);
  const deg = (p["降级"] || []).concat(p["降级与不确定性"] || []);
  if (deg.length) {
    html += '<div class="card"><div class="card-h"><span>数据依赖与降级</span>' +
      '<span class="muted">机械层与模型层如实呈现，不掩饰</span></div><div class="card-b">' +
      deg.map(x => '<div class="hint">· ' + esc(x) + "</div>").join("") + "</div></div>";
  }
  const ex = (p["排除统计"] && Object.keys(p["排除统计"]).length)
    ? "排除：" + Object.keys(p["排除统计"]).map(k => k + " " + p["排除统计"][k] + " 只").join("，") : "";
  html += '<div class="card"><div class="card-b"><div class="hint">' +
    esc(p["免责声明"] || p["note"] || "本结论由机械打分与模型点评生成，不构成投资建议。") +
    (ex ? " ｜ " + esc(ex) : "") + "</div></div></div>";
  box.innerHTML = html;

  $$("#pick-result [data-jump]").forEach(a => a.addEventListener("click", e => {
    e.preventDefault();
    const el = document.getElementById("pick-mod-" + a.dataset.jump);
    if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
  }));
  $$("#pick-result [data-pick-run]").forEach(a => a.addEventListener("click", e => {
    e.preventDefault();
    $("#code").value = a.dataset.pickRun;
    viewApi("run").syncCmd();
    viewApi("run").checkReadiness();
    showView("run");
    toast("已把 " + a.dataset.pickRun + " 填入运行页", "ok");
  }));
  $$("#pick-result [data-pick-kline]").forEach(a => a.addEventListener("click", e => {
    e.preventDefault();
    const code = a.dataset.pickKline;
    const hit = pickFindFirst(p, code);
    showPickKline(code, hit && hit["名称"], pickLevelsFrom(hit));
  }));
  $$("#pick-result [data-pick-watch]").forEach(b => b.addEventListener("click", async () => {
    try {
      const res = await api("/api/watchlist/add", {
        method: "POST",
        body: JSON.stringify({ code: b.dataset.pickWatch, name: b.dataset.pickWatchName || "", note: "荐股" }),
      });
      if (!res.ok) { toast(res.error, "bad"); return; }
      toast("已加入自选：" + b.dataset.pickWatch, "ok");
      if (State.state) {
        State.state["自选股"] = res["自选股"] || State.state["自选股"];
        State.state["代码候选"] = res["代码候选"] || State.state["代码候选"];
      }
    } catch (e) { toast(e.message, "bad"); }
  }));
  const del = $("#btn-pick-del");
  if (del) del.addEventListener("click", () => {
    const path = bundle["json路径"];
    confirmModal("删除这份荐股结果？",
      "会连同 .md 一起移入回收站（超过 7 天后自动真删），未过期前可恢复。",
      async () => {
        try {
          const res = await api("/api/pick/delete", { method: "POST", body: JSON.stringify({ path: path }) });
          if (!res.ok) { toast(res.error, "bad"); return; }
          toast("已移入回收站：" + path, "ok");
          await loadPick();
        } catch (e) { toast(e.message, "bad"); }
      }, "确认删除");
  });
}

export function pickFindFirst(p, code) {
  for (const m of Object.keys(p["候选"] || {})) {
    const hit = (p["候选"][m] || []).find(r => r["代码"] === code);
    if (hit) return hit;
  }
  return null;
}

export function pickLevelsFrom(hit) {
  const code = hit && hit["代码"];
  if (!code) return [];
  let f = null;
  const p = (Pick.data || {})["json"] || {};
  (p["模块"] || []).forEach(m => (m["首推"] || []).forEach(c => {
    if (String(c["代码"]) === String(code)) f = c;
  }));
  if (!f) return [];
  const out = [];
  const buy = f["关注买点"] || {}, stop = f["止损"] || {};
  const buyV = num(String(buy["价位"] || "").split("-")[0]);
  if (buyV !== null) out.push({ v: buyV, color: "#4c8dff", dash: [4, 3], text: "买点 " + fmt(buyV) });
  const sv = num(stop["价位"]);
  if (sv !== null) out.push({ v: sv, color: "#f05a63", dash: [4, 3], text: "止损 " + fmt(sv) });
  (f["目标位"] || []).forEach(t => {
    const v = num(t["价位"]);
    if (v !== null) out.push({ v: v, color: "#2ec27e", dash: [4, 3], text: "目标 " + fmt(v) });
  });
  return out;
}

export async function showPickHistory() {
  let res;
  try { res = await api("/api/pick/list"); } catch (e) { toast(e.message, "bad"); return; }
  const items = res["items"] || [];
  if (!items.length) { openModal("荐股历史", '<div class="empty">还没有荐股结果。</div>'); return; }
  let html = '<div class="table-wrap"><table class="tbl"><thead><tr><th>时间</th><th>模块</th>' +
    "<th class='num'>板块</th><th class='num'>候选</th><th>模型</th><th class='num'>费用</th>" +
    "<th>倾向</th><th>结论</th><th>操作</th></tr></thead><tbody>";
  items.forEach((it, i) => {
    html += "<tr><td>" + esc(it["时间"]) + "</td>" +
      '<td class="muted">' + esc((it["模块"] || []).join("/")) + "</td>" +
      '<td class="num">' + (it["板块数"] || 0) + "</td>" +
      '<td class="num">' + (it["候选数"] || 0) + "</td>" +
      "<td>" + (it["已点评"] ? chip(it["模型"] || "已点评")
        : chip("未点评", "warn")) + "</td>" +
      '<td class="num">' + (it["费用"] == null ? "—" : "¥" + it["费用"]) + "</td>" +
      "<td>" + (it["评级"] ? chip(it["评级"], dirColor(it["评级"])) : "—") + "</td>" +
      '<td class="wrap muted">' + esc((it["结论"] || "").slice(0, 60)) + "</td>" +
      '<td><a href="#" data-open="' + i + '">打开</a> ' +
      '<button class="btn sm danger" data-del="' + i + '">删除</button></td></tr>';
  });
  openModal("荐股历史（" + items.length + " 份）", html + "</tbody></table></div>");
  $$("#overlay [data-open]").forEach(a => a.addEventListener("click", async e => {
    e.preventDefault();
    closeModal();
    await loadPick(items[Number(a.dataset.open)]["json路径"]);
  }));
  $$("#overlay [data-del]").forEach(b => b.addEventListener("click", () => {
    const it = items[Number(b.dataset.del)];
    confirmModal("删除这份荐股结果？", "会连同 .md 一起移入回收站（超过 7 天后自动真删）。",
      async () => {
        try {
          const r = await api("/api/pick/delete", {
            method: "POST", body: JSON.stringify({ path: it["json路径"] }) });
          if (!r.ok) { toast(r.error, "bad"); return; }
          toast("已移入回收站", "ok");
          closeModal();
          await loadPick();
        } catch (e) { toast(e.message, "bad"); }
      }, "确认删除");
  }));
}

export function initPickView() {
  pickLoadModules();
  $$("#pick-modules button").forEach(b => b.addEventListener("click", () => {
    const on = !b.classList.contains("on");
    if (!on && pickModules().length <= 1) { toast("至少保留一个模块", "warn"); return; }
    b.classList.toggle("on", on);
    pickSaveModules();
    pickSyncHint();
  }));
  $$("#pick-tabs button").forEach(b => b.addEventListener("click", () => {
    Pick.tab = b.dataset.tab;
    const note = $("#pick-selectall-note");
    if (note) note.textContent = "";
    pickRenderLists();
  }));
  const search = $("#pick-search");
  if (search) search.addEventListener("input", () => {
    Pick.filter = (search.value || "").trim().toLowerCase();
    const note = $("#pick-selectall-note");
    if (note) note.textContent = "";
    pickRenderLists();
  });
  ["#pick-per-board", "#pick-max-candidates"].forEach(s => {
    const el = $(s);
    if (el) el.addEventListener("change", pickSyncHint);
  });
  const run = $("#btn-pick-run");
  if (run) run.addEventListener("click", startPick);
  const stop = $("#btn-pick-stop");
  if (stop) stop.addEventListener("click", stopPick);
  const his = $("#btn-pick-history");
  if (his) his.addEventListener("click", showPickHistory);
  const hot = $("#btn-pick-hot");
  if (hot) hot.addEventListener("click", pickSelectHot);
  const all = $("#btn-pick-selectall");
  if (all) all.addEventListener("click", pickSelectAll);
  const clr = $("#btn-pick-clear");
  if (clr) clr.addEventListener("click", pickClearSel);
  const rf = $("#btn-pick-refresh");
  if (rf) rf.addEventListener("click", () => {
    Pick.subs = {};
    pickLoadBoards(true);
    toast("已请求刷新板块行情与成分缓存", "ok");
  });
  pickSyncHint();
  pickLoadBoards(false);
}

/* =========================================================================
   模型配置
   ========================================================================= */

export const PICK_KLINE_ZOOM = [60, 120, 180, 260, 400];

export async function showPickKline(code, name, levels) {
  openModal("日K线 · " + code + " " + (name || ""),
    '<div class="row" style="margin-bottom:8px;align-items:center">' +
    '<span class="muted">价位标注来自本次模型的「买点 / 止损 / 目标位」</span>' +
    '<div class="spacer"></div>' +
    '<button class="btn sm ghost" data-kz="-1">－ 缩</button>' +
    '<span class="muted" id="pick-kline-zoom"></span>' +
    '<button class="btn sm ghost" data-kz="1">＋ 放</button></div>' +
    '<canvas class="kline-canvas" id="pick-kline-canvas" height="420"></canvas>' +
    '<div class="hint" id="pick-kline-msg"></div>');
  Pick.kline = { code: code, name: name, levels: levels || [], idx: 2 };
  await drawPickKline();
  $$("#overlay [data-kz]").forEach(b => b.addEventListener("click", async () => {
    Pick.kline.idx = Math.max(0, Math.min(PICK_KLINE_ZOOM.length - 1,
      Pick.kline.idx + Number(b.dataset.kz)));
    await drawPickKline();
  }));
}

export async function drawPickKline() {
  const st = Pick.kline;
  if (!st) return;
  const canvas = $("#pick-kline-canvas"), msg = $("#pick-kline-msg");
  const limit = PICK_KLINE_ZOOM[st.idx];
  const zl = $("#pick-kline-zoom");
  if (zl) zl.textContent = limit + " 根";
  let data;
  try {
    data = await api("/api/kline?code=" + encodeURIComponent(st.code) + "&limit=" + limit);
  } catch (e) {
    if (msg) msg.textContent = "没有该标的的日K缓存（data/history/）：先在「运行研判」跑一次它，" +
      "stock3d.py 会把日K落盘，这里就能画了。";
    return;
  }
  if (msg) msg.textContent = "";
  const bars = (data && data.bars) || [];
  const price = bars.length ? bars[bars.length - 1].close : null;
  drawKline(canvas, data, st.levels || [], price, st.name || "");
}

/* 注册给 core/app.js：切到本页时按需加载 / 对外暴露的动作。 */
registerView("pick", {
  onShow: loadPick,
  prefill: payload => pickPrefill(payload, { state: Pick, loadBoards: pickLoadBoards,
                                             renderLists: pickRenderLists, syncHint: pickSyncHint }),
});
