/* 荐股页（全大盘 · 模型为主）。
 *
 * 页面只剩三件事：设扫描参数 → 跑一次（全市场扫描 + 机械打分 + 1 次模型调用）→ 看推荐榜。
 * 行业/概念筛选与「模块 × 板块矩阵」已删除（批注 5、6）；打分机制重做（批注 7）：
 * 排名看模型评分，机械分只作辅助列与初筛，模型不给买卖价位。
 */
import { api } from "../core/api.js";
import { registerView, showView, viewApi } from "../core/app.js";
import { $, $$, esc, num, toast } from "../core/util.js";
import { drawKline } from "../ui/kline.js";
import { closeModal, confirmModal, openModal } from "../ui/modal.js";
import { pickMechanicCard, pickPoolCard, pickRankCard, pickVetoCard } from "../ui/pickcards.js";

export const Pick = {jobId: null, timer: null, from: 0, data: null, kline: null,
                    showPool: false};

export const PICK_KLINE_ZOOM = [60, 120, 180, 260, 400];


/* ---------------- 执行日志 ---------------- */

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
  if (run) { run.disabled = on; run.textContent = on ? "扫描中…" : "开始荐股"; }
  if (stop) stop.disabled = !on;
}

export function pickScanHint() {
  const box = $("#pick-scan-hint");
  if (!box) return;
  const pool = num($("#pick-pool-size").value) || 400;
  const top = num($("#pick-model-top").value) || 150;
  const heavy = Math.max(1, Math.round(pool / 400));
  box.innerHTML = "流程：东财全 A 扫描（约 5900 只）→ 排除规则 → 候选池 <b>" + pool +
    " 只</b> → 按《机器打分逻辑.txt》逐只打分（逐股日K，首次约 " +
    (3 * heavy) + "-" + (5 * heavy) + " 分钟）→ 机械分前 <b>" + top +
    " 只</b> 补公告/股东/资金明细 → <b>1 次模型调用</b>给出 30 只推荐榜。<br>" +
    "结果落 data/ai/pick/；只有「加入自选」会写配置，其余只读。";
}

export async function startPick() {
  const pool = num($("#pick-pool-size").value) || 400;
  const top = num($("#pick-model-top").value) || 150;
  if (top > pool) { toast("送模型的条数不能超过候选池上限", "bad"); return; }
  const body = {
    kind: "pick",
    pool_size: pool,
    model_top: top,
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
    pickConsoleClear("任务已启动：" + (res["命令"] || ""));
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
    toast("荐股失败：看执行日志里的 [FAIL] 一行" +
      (result["诊断"] ? "；" + result["诊断"] : ""), "bad");
  } else if (result["模型错误"]) {
    toast("模型评分失败，已按机械分保存：" + result["模型错误"], "warn");
  } else {
    toast("荐股完成：" + (result["推荐榜"] || 0) + " 只推荐榜", "ok");
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

/* ---------------- 结果渲染 ---------------- */

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

export function renderPick(bundle) {
  const box = $("#pick-result");
  if (!box) return;
  const p = (bundle || {})["json"] || {};
  if (!p || !p["生成时间"]) {
    box.innerHTML = '<div class="card"><div class="card-b"><div class="empty">' +
      "还没有荐股结果：设好上面的参数点「开始荐股」跑一次（全市场扫描 + 1 次模型调用）。" +
      "</div></div></div>";
    return;
  }
  let html = "";
  const style = p["市场风格"] || {};
  if (style["一句话"] || p["总评"]) {
    const s = style["一句话"] ? style : (p["总评"] || {});
    html += '<div class="card"><div class="card-h"><span>市场风格</span>' +
      '<span class="muted">' + esc((p["配置"] || {}).model || "—") + "</span></div>" +
      '<div class="card-b"><div class="hero-quote">' + esc(s["一句话"] || "") + "</div>" +
      '<div class="muted">多空倾向 ' + esc(s["多空倾向"] || "—") +
      " ｜ 最强打法 " + esc(s["最强打法"] || "—") +
      " ｜ 操作节奏 " + esc(s["操作节奏"] || "—") + "</div></div></div>";
  }
  if ((p["模型层"] || {})["error"]) {
    html += '<div class="card"><div class="card-b"><div class="fail">[WARN] 本次没有模型评分：' +
      esc(p["模型层"].error) + "（榜单按机械分排序）</div></div></div>";
  }
  html += pickRankCard(p);
  html += pickVetoCard(p);
  html += pickMechanicCard(p);
  html += pickPoolCard(p, Pick.showPool);
  box.innerHTML = html;
  bindResult(box);
}

function bindResult(root) {
  root.querySelectorAll("[data-pk-run]").forEach(a => a.addEventListener("click", e => {
    e.preventDefault();
    const code = a.dataset.pkRun;
    const run = viewApi("run");
    if (run.pickCode) run.pickCode(code);
    showView("run");
  }));
  root.querySelectorAll("[data-pk-kline]").forEach(a => a.addEventListener("click", e => {
    e.preventDefault();
    showPickKline(a.dataset.pkKline, a.dataset.pkName || "");
  }));
  root.querySelectorAll("[data-pk-watch]").forEach(b => b.addEventListener("click", async () => {
    try {
      const code = b.dataset.pkWatck || b.dataset.pkWatch;
      const res = await api("/api/watchlist/add", {
        method: "POST",
        body: JSON.stringify({code: code, name: b.dataset.pkName || "", note: "荐股榜"}),
      });
      if (!res.ok) { toast(res.error || "加入自选失败", "bad"); return; }
      toast("已加入自选：" + code, "ok");
    } catch (e) { toast(e.message, "bad"); }
  }));
  const more = root.querySelector("#btn-pick-pool-more");
  if (more) more.addEventListener("click", () => {
    Pick.showPool = !Pick.showPool;
    renderPick(Pick.data);
  });
  root.querySelectorAll("[data-pk-del]").forEach(b => b.addEventListener("click", () => {
    confirmModal("删除这份荐股结果？", "会连同 .md 一起移入回收站（超过 7 天后自动真删）。",
      async () => {
        const res = await api("/api/pick/delete", {
          method: "POST", body: JSON.stringify({path: b.dataset.pkDel}) });
        if (!res.ok) { toast(res.error, "bad"); return; }
        toast("已移入回收站", "ok");
        await loadPick();
      }, "确认删除");
  }));
}

export async function showPickHistory() {
  let res;
  try { res = await api("/api/pick/list"); } catch (e) { toast(e.message, "bad"); return; }
  const items = res["items"] || [];
  if (!items.length) { openModal("荐股历史", '<div class="empty">还没有荐股结果。</div>'); return; }
  let html = '<div class="table-wrap"><table class="tbl"><thead><tr><th>时间</th>' +
    "<th>打法</th><th class='num'>候选</th><th>模型</th><th class='num'>费用</th>" +
    "<th class='num'>推荐榜</th><th>操作</th></tr></thead><tbody>";
  items.forEach((it, i) => {
    html += "<tr><td>" + esc(it["时间"]) + "</td>" +
      '<td class="muted">' + esc((it["模块"] || []).join("/") || "—") + "</td>" +
      '<td class="num">' + (it["候选数"] || 0) + "</td>" +
      "<td>" + (it["已点评"] ? chipSafe(it["模型"] || "已点评") : chipSafe("未点评", "warn")) + "</td>" +
      '<td class="num">' + (it["费用"] == null ? "—" : "¥" + it["费用"]) + "</td>" +
      '<td class="num">' + (it["推荐数"] == null ? "—" : it["推荐数"]) + "</td>" +
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
        const r = await api("/api/pick/delete", {
          method: "POST", body: JSON.stringify({path: it["json路径"]}) });
        if (!r.ok) { toast(r.error, "bad"); return; }
        toast("已移入回收站", "ok");
        closeModal();
        await loadPick();
      }, "确认删除");
  }));
}

function chipSafe(text, cls) {
  return '<span class="chip ' + (cls || "") + '">' + esc(text) + "</span>";
}

/* ---------------- 日K（模型不给价位，所以图上只画K线） ---------------- */

export async function showPickKline(code, name) {
  openModal("日K线 · " + code + " " + (name || ""),
    '<div class="row" style="margin-bottom:8px;align-items:center">' +
    '<span class="muted">模型不给买卖价位：这里只画日K，价位请自行研判</span>' +
    '<div class="spacer"></div>' +
    '<button class="btn sm ghost" data-kz="-1">－ 缩</button>' +
    '<span class="muted" id="pick-kline-zoom"></span>' +
    '<button class="btn sm ghost" data-kz="1">＋ 放</button></div>' +
    '<canvas class="kline-canvas" id="pick-kline-canvas" height="420"></canvas>' +
    '<div class="hint" id="pick-kline-msg"></div>');
  Pick.kline = {code: code, name: name, idx: 2};
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
    if (msg) {
      msg.textContent = "日K 取不到（东财与腾讯都失败）：检查网络，或先跑 python main.py stock3d pull。";
    }
    return;
  }
  if (msg) msg.textContent = "";
  const bars = (data && data.bars) || [];
  const price = bars.length ? bars[bars.length - 1].close : null;
  drawKline(canvas, data, [], price, st.name || "");
}

export function initPickView() {
  const run = $("#btn-pick-run");
  if (run) run.addEventListener("click", startPick);
  const stop = $("#btn-pick-stop");
  if (stop) stop.addEventListener("click", stopPick);
  const his = $("#btn-pick-history");
  if (his) his.addEventListener("click", showPickHistory);
  ["#pick-pool-size", "#pick-model-top"].forEach(sel => {
    const el = $(sel);
    if (el) el.addEventListener("change", pickScanHint);
  });
  pickScanHint();
}

/* 注册给 core/app.js：切到本页时按需加载。 */
registerView("pick", {onShow: loadPick});
