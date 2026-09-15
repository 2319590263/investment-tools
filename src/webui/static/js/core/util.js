/* 通用工具：DOM 选择、转义、数字与金额格式化、toast、chip/badge。 */

export const $ = (s, r) => (r || document).querySelector(s);

export const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

export const sleep = ms => new Promise(r => setTimeout(r, ms));

export const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export const num = v => { if (v == null || v === "" || v === true || v === false) return null; const n = Number(v); return isNaN(n) ? null : n; };

export const has = v => v !== null && v !== undefined && v !== "";

export function fmt(v, d) {
  d = d === undefined ? 2 : d;
  const n = num(v);
  return n === null ? "—" : n.toFixed(d);
}

export function fmtPct(v, d) {
  const n = num(v);
  if (n === null) return "—";
  return (n > 0 ? "+" : "") + n.toFixed(d === undefined ? 2 : d) + "%";
}

export function pctClass(v) {
  const n = num(v);
  if (n === null || n === 0) return "";
  return n > 0 ? "up" : "down";
}

export function fmtMoney(v, d) {
  const n = num(v);
  if (n === null) return "—";
  return n.toLocaleString("zh-CN", { minimumFractionDigits: d === undefined ? 2 : d, maximumFractionDigits: d === undefined ? 2 : d });
}

export function fmtSize(bytes) {
  if (!bytes) return "—";
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / 1024 / 1024).toFixed(2) + " MB";
}

export function dirColor(v) {
  if (v === "偏多") return "up";
  if (v === "偏空") return "down";
  return "";
}

export function kindClass(v) {
  const n = num(v);
  if (n === null) return "";
  return n > 0 ? "up" : (n < 0 ? "down" : "");
}

export function toast(msg, kind) {
  const el = document.createElement("div");
  el.className = "toast " + (kind || "");
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .3s"; }, 3200);
  setTimeout(() => el.remove(), 3700);
}

export const EMPTY = '<div class="empty">暂无数据</div>';

export function chip(text, cls) { return '<span class="chip ' + (cls || "") + '">' + esc(text) + "</span>"; }

/* 任务启动时服务端会把「过期快照清理」结果一起返回（批注：事实包不许用过期数据）。 */
export function freshNote(res) {
  const clear = (res || {})["清理"] || {};
  const gone = (clear["删除"] || []).length;
  if (!gone) return "";
  return "已删除 " + gone + " 份过期行情快照（早于最近交易日 " + clear["最近交易日"] +
    "）；想看最新背景先跑一次大盘 / 个股抓数（pan post / stock3d pull）。";
}

export function badge(text, cls) { return '<span class="badge ' + (cls || "flat") + '">' + esc(text) + "</span>"; }
