/* 报告与大盘页共用的卡片级渲染原语（card/kv/列表/胶囊/情景汇总）。 */
import { esc, fmt, num } from "../core/util.js";

export function card(title, body, extra) {
  return '<div class="card"><div class="card-h"><span>' + esc(title) + "</span>" +
    (extra ? '<div class="spacer"></div>' + extra : "") + '</div><div class="card-b">' + body + "</div></div>";
}

export function kv(k, v) { return '<div class="kv"><b>' + esc(k) + "</b><span>" + v + "</span></div>"; }

export function listOrEmpty(arr) {
  if (!Array.isArray(arr) || !arr.length) return '<div class="empty">无</div>';
  return "<ul style='margin:6px 0 0;padding-left:20px'>" +
    arr.map(x => "<li>" + esc(typeof x === "string" ? x : JSON.stringify(x)) + "</li>").join("") + "</ul>";
}


/* ---------------- 关键价位索引 ---------------- */
export function pill(v, note, cls) {
  return '<span class="price-pill ' + (cls || "") + '"><b>' + fmt(v) + "</b>" +
    (note ? "<small>" + esc(note) + "</small>" : "") + "</span>";
}


/* ---------------- K 线图 ---------------- */
export function renderScenarioSummary(plan) {
  const scenes = Array.isArray((plan || {})["情景树"]) ? plan["情景树"] : [];
  if (!scenes.length) return "";
  let bull = 0, bear = 0, neut = 0;
  const rows = scenes.map(s => {
    const p = num(s["概率_pct"]) || 0;
    const name = String(s["情形"] || "");
    let tag = "neut";
    if (/乐观|看多|上涨|反弹|强势/.test(name)) tag = "bull";
    else if (/悲观|看空|下跌|回调|弱势/.test(name)) tag = "bear";
    if (tag === "bull") bull += p; else if (tag === "bear") bear += p; else neut += p;
    return { name: name, p: p, tag: tag, fail: s["失效条件"] || "", path: s["路径"] || "" };
  });
  const raw = num(plan["多空评分"]);
  const score = raw === null ? Math.round(bull - bear) : Math.round(raw);
  const label = score >= 40 ? "强偏多" : score >= 15 ? "偏多"
    : score > -15 ? "中性震荡" : score > -40 ? "偏空" : "强偏空";
  const cls = score >= 15 ? "up" : (score <= -15 ? "down" : "");
  const top = rows.slice().sort((a, b) => b.p - a.p)[0];
  const total = (bull + neut + bear) || 1;
  const w = v => (v / total * 100);
  const r1 = v => Math.round(v * 10) / 10;

  let html = '<div class="mini-card" style="margin-bottom:14px">' +
    '<div class="sec-title">综合评价 <span class="muted">（按情景概率机械汇总，非二次判断）</span></div>' +
    '<div class="gauge-row">' +
    '<div class="gauge"><span class="k">综合倾向</span><span class="v ' + cls + '">' + label + "</span></div>" +
    '<div class="gauge"><span class="k">多空评分</span><span class="v ' + cls + '">' + score + "</span></div>" +
    '<div class="gauge"><span class="k">最可能情景</span><span class="v" style="font-size:14px">' +
    esc(top.name || "—") + " " + r1(top.p) + "%</span></div>" +
    "</div>" +
    '<div class="seg-bar">' +
    '<span style="width:' + w(bull) + '%;background:linear-gradient(90deg,#d94b53,var(--up))"></span>' +
    '<span style="width:' + w(neut) + '%;background:#5a6b80"></span>' +
    '<span style="width:' + w(bear) + '%;background:linear-gradient(90deg,#1f9e66,var(--down))"></span>' +
    "</div>" +
    '<div class="seg-legend">' +
    '<span><i style="background:var(--up)"></i>偏多 ' + r1(bull) + "%</span>" +
    '<span><i style="background:#5a6b80"></i>震荡 ' + r1(neut) + "%</span>" +
    '<span><i style="background:var(--down)"></i>偏空 ' + r1(bear) + "%</span>" +
    "</div>";
  if (top.path) html += '<div class="muted" style="margin-top:8px">最可能路径：' + esc(top.path) + "</div>";
  if (top.fail) html += '<div class="fail" style="margin-top:8px">该情景失效条件：' + esc(top.fail) + "</div>";
  return html + "</div>";
}
