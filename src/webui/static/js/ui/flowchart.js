/* 交易流分时图：分时价格线 + **交易计划里的操作买卖线**（买点 / 减仓 / 止损 / 止盈点）。
 * 用户口径：图上要的是「交易计划里的操作价位」，不是成交点，也不是均价/昨收这类均线。
 *
 * 只画图，不发请求；数据来自 /api/flow/minutes。计划线用与卡片同一份
 * planprices.js 收敛成精确价位，避免第二套口径。
 */
import { fmt, num } from "../core/util.js";
import { nearestTarget, precisePrice } from "./planprices.js";

const C = { price: "#4c8dff", avg: "#e2b03c", prev: "#5b6b7f",
            buy: "#f05a63", sell: "#2ec27e", target: "#c084fc",
            text: "#7f8fa6", line: "#26333f", bg: "#141a21", tip: "#0f151b" };

/* 计划的操作价位线（同价合并成一条，标签用「·」拼起来）。 */
export function flowLines(levels, price) {
  const lv = levels || {};
  const out = [];
  const add = (v, label, color) => { if (num(v) !== null) out.push({ v: num(v), label, color }); };
  add(precisePrice(lv["买点"], price), "买点", C.buy);
  add(precisePrice(lv["减仓"], price), "减仓", C.sell);
  add(num(lv["止损"]), "止损", C.sell);
  const goal = nearestTarget(lv["目标"], price);
  if (goal) out.push({ v: goal.v, label: "止盈点", color: C.target });
  const merged = [];                       // 同价（±0.1%）合并：买点与止盈点撞在一起时只画一条
  out.forEach(x => {
    const hit = merged.find(m => Math.abs(m.v - x.v) <= Math.max(m.v * 0.001, 0.005));
    if (hit) { hit.label += "·" + x.label; } else merged.push(Object.assign({}, x));
  });
  return merged.sort((a, b) => b.v - a.v);
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
  // 纵轴范围 = 分时价 + 计划的操作价位线（均价/昨收/成交点都不画）
  const points = prices.filter(v => v !== null);
  const planLines = flowLines(data["关键价位"], num((data["报价"] || {})["价格"]));
  planLines.forEach(l => points.push(l.v));
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
  /* 交易计划里的操作买卖线（用户口径）：买点 / 减仓 / 止损 / 止盈点各一条整宽水平线，
     标签放右端；同价的多条操作合并成一条（如「买点·止盈点 8.220」）。 */
  planLines.forEach(l => {
    const cy = y(l.v);
    ctx.save();
    ctx.strokeStyle = l.color;
    ctx.lineWidth = 1.3;
    ctx.beginPath(); ctx.moveTo(padL, cy); ctx.lineTo(padL + plotW, cy); ctx.stroke();
    ctx.fillStyle = l.color;
    ctx.font = "11px system-ui, sans-serif";
    ctx.fillText(l.label + " " + fmt(l.v, 3), padL + plotW + 6, cy + 4);
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
