/* 交易流分时图：只画**分时价格线 + 买卖线**（用户批注 1：去掉所有多余的线，只要买卖线）。
 *
 * 只画图，不发请求；数据来自 /api/flow/minutes。计划线用与卡片同一份
 * planprices.js 收敛成精确价位，避免第二套口径。
 */
import { fmt, num } from "../core/util.js";
import { nearestTarget, precisePrice } from "./planprices.js";

const C = { price: "#4c8dff", avg: "#e2b03c", prev: "#5b6b7f",
            buy: "#f05a63", sell: "#2ec27e", target: "#c084fc",
            text: "#7f8fa6", line: "#26333f", bg: "#141a21", tip: "#0f151b" };

export function flowLines(levels, price) {
  const lv = levels || {};
  const out = [];
  const add = (v, label, color) => { if (num(v) !== null) out.push({ v: num(v), label, color }); };
  add(precisePrice(lv["买点"], price), "买点", C.buy);
  add(precisePrice(lv["减仓"], price), "减仓", C.sell);
  add(num(lv["止损"]), "止损", C.sell);
  const goal = nearestTarget(lv["目标"], price);
  if (goal) out.push({ v: goal.v, label: "止盈点", color: C.target });
  return out;
}

/* 成交点 → 分时序列下标（当天成交按时间就近吸附）。 */
function fillIndex(times, date, today, when) {
  if (!times.length || String(date || "") !== String(today || "")) return null;
  const hhmm = String(when || "").slice(0, 5);
  if (!hhmm) return null;
  let idx = null;
  times.forEach((t, i) => { if (String(t).slice(0, 5) <= hhmm) idx = i; });
  return idx === null ? 0 : idx;
}

function scale(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const w = Math.max(320, canvas.clientWidth || canvas.parentElement.clientWidth || 640);
  const h = 190;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  canvas.style.height = h + "px";
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return { ctx, w, h };
}

/* 数据 → 画布（把缩放后的映射关系挂回 canvas，供 hover 复用）。 */
export function drawFlowMinutes(canvas, data) {
  if (!canvas || !data) return null;
  const { ctx, w, h } = scale(canvas);
  const minute = data["分时"] || {};
  const prices = (minute["价格"] || []).map(num);
  const times = minute["时间"] || [];
  const padL = 52, padR = 96, padT = 16, padB = 22;
  const plotW = Math.max(60, w - padL - padR), plotH = Math.max(40, h - padT - padB);
  // 纵轴范围只看分时价与买卖线（均价/计划线/昨收都不画，用户批注 1：只要买卖线）
  const points = prices.filter(v => v !== null);
  (data["成交"] || []).forEach(f => {
    const v = num(f["价格"]);
    if (v !== null) points.push(v);
  });
  if (!points.length) {
    ctx.fillStyle = C.text;
    ctx.font = "12px system-ui, sans-serif";
    ctx.fillText("本次拿不到分时数据（北交所 / 停牌 / 网络）", padL, h / 2);
    return null;
  }
  let lo = Math.min.apply(null, points), hi = Math.max.apply(null, points);
  const pad = Math.max((hi - lo) * 0.08, hi * 0.001, 1e-6);
  lo -= pad; hi += pad;
  const n = Math.max(prices.length, 2);
  const x = i => padL + (plotW * i) / (n - 1 || 1);
  const y = v => padT + plotH * (1 - (v - lo) / (hi - lo));
  const seg = (vals, color, width) => {
    ctx.strokeStyle = color; ctx.lineWidth = width; ctx.beginPath();
    let started = false;
    vals.forEach((v, i) => {
      if (v === null) return;
      if (!started) { ctx.moveTo(x(i), y(v)); started = true; } else ctx.lineTo(x(i), y(v));
    });
    ctx.stroke();
  };
  ctx.fillStyle = C.bg; ctx.fillRect(padL, padT, plotW, plotH);
  /* 只保留分时线 + 买卖线（批注 1）：均价线、计划线、昨收虚线都不画，避免线条与文字重叠 */
  seg(prices, C.price, 1.6);
  /* 买卖线（批注 1/4）：每笔成交按成交价画一条**贯穿全宽**的水平线（买=红、卖=绿），
     标签放在右端，避免与左侧价格刻度、曲线重叠。 */
  const today = data["分时日期"];
  (data["成交"] || []).forEach(f => {
    const i = fillIndex(times, f["日期"], today, f["时间"]);
    const price = num(f["价格"]);
    if (i === null || price === null) return;
    const buy = String(f["方向"] || "") === "买入";
    const cy = y(price);
    ctx.save();
    ctx.strokeStyle = buy ? C.buy : C.sell;
    ctx.lineWidth = 1.4;
    ctx.beginPath(); ctx.moveTo(padL, cy); ctx.lineTo(padL + plotW, cy); ctx.stroke();  // 整宽买卖线
    ctx.fillStyle = buy ? C.buy : C.sell;
    ctx.font = "11px system-ui, sans-serif";
    ctx.fillText((buy ? "买 " : "卖 ") + fmt(price, 3), padL + plotW + 6, cy + 4);
    ctx.restore();
  });
  ctx.fillStyle = C.text; ctx.font = "11px system-ui, sans-serif";
  ctx.fillText(fmt(hi, 3), 6, padT + 8);
  ctx.fillText(fmt(lo, 3), 6, padT + plotH);
  if (times.length) {
    ctx.fillText(String(times[0]).slice(0, 5), padL, h - 6);
    ctx.fillText(String(times[times.length - 1]).slice(0, 5), padL + plotW - 28, h - 6);
  }
  canvas.dataset.map = JSON.stringify({ padL, plotW, padT, plotH, lo, hi, n, times, prices });
  return { times, prices, lo, hi };
}

/* hover：显示「时间 价格」。 */
export function bindFlowChart(canvas) {
  if (!canvas || canvas.dataset.bound === "1") return;
  canvas.dataset.bound = "1";
  const tip = document.createElement("div");
  tip.className = "muted fl-chart-tip";
  tip.textContent = "移动鼠标查看分时明细";
  canvas.parentElement.appendChild(tip);
  canvas.addEventListener("mousemove", e => {
    let map = null;
    try { map = JSON.parse(canvas.dataset.map || "null"); } catch (err) { map = null; }
    if (!map) return;
    const rect = canvas.getBoundingClientRect();
    const rel = e.clientX - rect.left - map.padL;
    if (rel < 0 || rel > map.plotW) return;
    const i = Math.max(0, Math.min(map.times.length - 1,
      Math.round((rel / map.plotW) * (map.n - 1))));
    const p = map.prices[i];
    tip.textContent = " " + String(map.times[i] || "").slice(0, 5) + " ｜ 价 " +
      (p === null || p === undefined ? "—" : fmt(p, 3));
  });
}
