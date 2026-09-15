/* K 线绘制与缩放（报告页、荐股页、总控台共用）。 */
import { api } from "../core/api.js";
import { $, chip, esc, fmt, num, toast } from "../core/util.js";
import { openModal } from "./modal.js";

/* 缩放档位：放大 = 少画几根、同时把画布撑高，看得更清；缩小 = 多看历史 */
export const KLINE_COLORS = { up: "#f05a63", down: "#2ec27e", ma5: "#e2b03c", ma10: "#4c8dff", ma20: "#c084fc" };

export const KLINE_ZOOM = [
  { bars: 40, h: 560 }, { bars: 60, h: 520 }, { bars: 90, h: 470 },
  { bars: 120, h: 430 }, { bars: 180, h: 400 }, { bars: 250, h: 370 },
  { bars: 400, h: 340 },
];

export const KLINE_ZOOM_DEFAULT = 3;

export function applyKlineZoom() {
  const k = window.__kline;
  if (!k) return;
  const idx = Math.max(0, Math.min(KLINE_ZOOM.length - 1, k.idx));
  k.idx = idx;
  const z = KLINE_ZOOM[idx];
  const bars = k.data.bars.slice(-Math.min(z.bars, k.data.bars.length));
  k.canvas.style.height = z.h + "px";
  drawKline(k.canvas, { bars: bars }, k.levels, k.cur);
  const label = $("#k-zoom-label");
  if (label) label.textContent = bars.length + " 根";
  const zin = $("#k-zoom-in"), zout = $("#k-zoom-out");
  if (zin) zin.disabled = idx <= 0;
  if (zout) zout.disabled = idx >= KLINE_ZOOM.length - 1;
}

export function klineZoom(delta) {
  const k = window.__kline;
  if (!k) return;
  k.idx = Math.max(0, Math.min(KLINE_ZOOM.length - 1, k.idx + delta));
  applyKlineZoom();
}

export function maOf(bars, n) {
  const out = [];
  let sum = 0;
  for (let i = 0; i < bars.length; i++) {
    sum += bars[i].close;
    if (i >= n) sum -= bars[i - n].close;
    out.push(i >= n - 1 ? sum / n : null);
  }
  return out;
}


/* ---------------- 情景卡片（报告与大盘预测共用） ---------------- */
export function drawKline(canvas, data, levels, price, planLabel) {
  const bars = (data && data.bars) || [];
  if (!bars.length) return false;
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || 900;
  const cssH = canvas.clientHeight || 430;
  canvas.width = Math.round(cssW * dpr);
  canvas.height = Math.round(cssH * dpr);
  const g = canvas.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, cssW, cssH);

  const padL = 8, padR = 78, padT = 16, padB = 26;
  const volH = 74, gap = 14;
  const priceH = cssH - padT - padB - volH - gap;
  const plotW = cssW - padL - padR;
  const bw = plotW / bars.length;

  let hi = -Infinity, lo = Infinity, vmax = 0;
  bars.forEach(b => {
    hi = Math.max(hi, b.high); lo = Math.min(lo, b.low);
    vmax = Math.max(vmax, b.vol || 0);
  });
  levels.forEach(l => { if (l.v > 0) { hi = Math.max(hi, l.v); lo = Math.min(lo, l.v); } });
  if (price) { hi = Math.max(hi, price); lo = Math.min(lo, price); }
  const span = (hi - lo) || 1;
  hi += span * 0.04; lo -= span * 0.04;
  const y = v => padT + (hi - v) / (hi - lo) * priceH;
  const x = i => padL + i * bw + bw / 2;

  // 网格
  g.strokeStyle = "#1b2532"; g.lineWidth = 1; g.font = "11px Consolas, monospace";
  g.fillStyle = "#7d8ea3"; g.textAlign = "left";
  for (let k = 0; k <= 4; k++) {
    const v = lo + (hi - lo) * k / 4, yy = Math.round(y(v)) + 0.5;
    g.beginPath(); g.moveTo(padL, yy); g.lineTo(padL + plotW, yy); g.stroke();
    g.fillText(v.toFixed(v > 100 ? 0 : 2), padL + plotW + 6, yy + 4);
  }

  // K 线
  bars.forEach((b, i) => {
    const up = b.close >= b.open;
    g.strokeStyle = up ? KLINE_COLORS.up : KLINE_COLORS.down;
    g.fillStyle = up ? KLINE_COLORS.up : KLINE_COLORS.down;
    const cx = x(i);
    g.beginPath(); g.moveTo(Math.round(cx) + 0.5, y(b.high)); g.lineTo(Math.round(cx) + 0.5, y(b.low)); g.stroke();
    const w = Math.max(1, bw * 0.62);
    const yo = y(b.open), yc = y(b.close);
    g.fillRect(cx - w / 2, Math.min(yo, yc), w, Math.max(1, Math.abs(yc - yo)));
  });

  // 均线
  [[5, KLINE_COLORS.ma5], [10, KLINE_COLORS.ma10], [20, KLINE_COLORS.ma20]].forEach(([n, color]) => {
    const ma = maOf(bars, n);
    g.strokeStyle = color; g.lineWidth = 1.2; g.beginPath();
    let started = false;
    ma.forEach((v, i) => {
      if (v == null) return;
      const px = x(i), py = y(v);
      if (!started) { g.moveTo(px, py); started = true; } else g.lineTo(px, py);
    });
    g.stroke();
  });

  // 成交量
  const vTop = padT + priceH + gap, vBot = vTop + volH;
  g.strokeStyle = "#1b2532";
  g.beginPath(); g.moveTo(padL, vBot + 0.5); g.lineTo(padL + plotW, vBot + 0.5); g.stroke();
  bars.forEach((b, i) => {
    if (!vmax) return;
    const up = b.close >= b.open;
    g.fillStyle = up ? "rgba(240,90,99,.55)" : "rgba(46,194,126,.55)";
    const h = (b.vol || 0) / vmax * (volH - 6);
    g.fillRect(x(i) - Math.max(1, bw * 0.62) / 2, vBot - h, Math.max(1, bw * 0.62), h);
  });

  // 关键价位线
  const labelRight = padL + plotW + 4;
  const used = [];
  levels.forEach(l => {
    if (!(l.v > 0) || l.v < lo || l.v > hi) return;
    const yy = Math.round(y(l.v)) + 0.5;
    if (used.some(u => Math.abs(u - yy) < 12)) return;
    used.push(yy);
    g.save();
    g.setLineDash(l.dash ? [5, 4] : []);
    g.strokeStyle = l.color; g.lineWidth = 1.2;
    g.beginPath(); g.moveTo(padL, yy); g.lineTo(padL + plotW, yy); g.stroke();
    g.restore();
    g.fillStyle = l.color; g.textAlign = "left";
    g.fillText(l.text, labelRight, yy + 4);
  });

  // 现价
  if (price) {
    const yy = Math.round(y(price)) + 0.5;
    g.save();
    g.strokeStyle = "#e7eef7"; g.setLineDash([3, 3]);
    g.beginPath(); g.moveTo(padL, yy); g.lineTo(padL + plotW, yy); g.stroke();
    g.restore();
    g.fillStyle = "#e7eef7";
    g.fillText("现价 " + price, labelRight, yy - 4);
  }

  // 日期刻度
  g.fillStyle = "#7d8ea3"; g.textAlign = "center";
  const step = Math.max(1, Math.floor(bars.length / 6));
  for (let i = 0; i < bars.length; i += step) {
    g.fillText(String(bars[i].date).slice(5), x(i), cssH - 8);
  }
  return true;
}


/* ---------------- 报告页 K 线 ---------------- */
export function sceneCards(scenes) {
  return '<div class="cards-3">' + scenes.map(sc => {
    const p = num(sc["概率_pct"]);
    return '<div class="scene">' +
      '<div class="scene-h"><b>' + esc(sc["情形"] || "") + "</b>" + chip((p == null ? "—" : p + "%")) + "</div>" +
      (p == null ? "" : '<div class="bar-track"><div class="bar-fill" style="width:' + Math.max(0, Math.min(100, p)) + '%"></div></div>') +
      '<div class="path">' + esc(sc["路径"] || "") + "</div>" +
      (Array.isArray(sc["验证信号"]) && sc["验证信号"].length
        ? '<div class="sec-title" style="margin:0">触发信号</div><ul>' +
          sc["验证信号"].map(x => "<li>" + esc(x) + "</li>").join("") + "</ul>" : "") +
      (sc["失效条件"] ? '<div class="fail">失效：' + esc(sc["失效条件"]) + "</div>" : "") +
      "</div>";
  }).join("") + "</div>";
}

export async function loadStructKline(b) {
  const canvas = $("#kline-canvas");
  if (!canvas) return;
  const loading = $("#kline-loading");
  const code = ((b || {})["摘要"] || {})["标的代码"];
  const plan = ((b || {}).json || {})["研判"] || {};
  const planJson = plan.json || {};
  const prices = planJson["关键价位"] || {};
  let data;
  try {
    data = await api("/api/kline?code=" + encodeURIComponent(code) + "&limit=400");
  } catch (e) {
    if (loading) loading.textContent = "日K 取不到（东财与腾讯都失败）：检查网络，或先用 python main.py stock3d pull " + code + " 落一份本地缓存";
    return;
  }
  const levels = [];
  const add = (arr, color, type, dash) => (Array.isArray(arr) ? arr : []).forEach(x => {
    const v = num(x && x["价位"]);
    if (v !== null) levels.push({ v: v, color: color, dash: dash, text: type + " " + fmt(v) });
  });
  add(prices["支撑"], "#4c8dff", "支撑");
  add(prices["压力"], "#e2b03c", "压力");
  add(prices["目标位"], "#2ec27e", "目标", true);
  const stop = num(prices["止损价"]);
  if (stop !== null) levels.push({ v: stop, color: "#f05a63", dash: true, text: "止损 " + fmt(stop) });
  const cur = num(((b || {})["摘要"] || {})["现价"]);
  window.__kline = { canvas: canvas, data: data, levels: levels, cur: cur, idx: KLINE_ZOOM_DEFAULT };
  if (!data.bars.length) {
    if (loading) loading.textContent = "缓存里没有足够的日K数据";
    return;
  }
  applyKlineZoom();
  if (loading) loading.style.display = "none";
  canvas.style.display = "block";
  const lg = $("#kline-legend");
  if (lg) {
    lg.style.display = "flex";
    lg.innerHTML = [
      ["#f05a63", "阳线"], ["#2ec27e", "阴线"],
      [KLINE_COLORS.ma5, "MA5"], [KLINE_COLORS.ma10, "MA10"], [KLINE_COLORS.ma20, "MA20"],
      ["#4c8dff", "支撑"], ["#e2b03c", "压力"], ["#f05a63", "止损"], ["#2ec27e", "目标"],
    ].map(x => '<span><i style="background:' + x[0] + '"></i>' + x[1] + "</span>").join("");
  }
  const src = $("#kline-src");
  if (src) src.textContent = (data["文件"] || "") + " · 缓存 " + data.bars.length + " 根 · 来源 " + (data["来源"] || "—") + " / " + (data["复权"] || "—");
  const zin = $("#k-zoom-in");
  if (zin) zin.addEventListener("click", () => klineZoom(-1));
  const zout = $("#k-zoom-out");
  if (zout) zout.addEventListener("click", () => klineZoom(1));
  const zreset = $("#k-zoom-reset");
  if (zreset) zreset.addEventListener("click", () => {
    if (window.__kline) { window.__kline.idx = KLINE_ZOOM_DEFAULT; applyKlineZoom(); }
  });
  window.removeEventListener("resize", redrawKline);
  window.addEventListener("resize", redrawKline);
}


/* ---------------- 确认对话框 ---------------- */
export function redrawKline() {
  applyKlineZoom();
}

/* 弹窗看日K（总控台卡片用）：没有 data/history 缓存就提示先抓数，不画空图。 */
export async function showKlineModal(code, name) {
  let data = null;
  try {
    data = await api("/api/kline?code=" + encodeURIComponent(code) + "&limit=120");
  } catch (e) {
    toast("日K 取不到（东财与腾讯都失败）：检查网络，或先跑 python main.py stock3d pull " + code, "warn");
    return;
  }
  const bars = (data || {}).bars || [];
  if (!bars.length) {
    toast("日K 返回为空：先跑 python main.py stock3d pull " + code + " 落一份本地缓存", "warn");
    return;
  }
  openModal((name ? name + " " : "") + code + " · 日K（前复权）",
    '<canvas class="kline-canvas" id="modal-kline" style="display:block;height:320px"></canvas>');
  const canvas = $("#modal-kline");
  if (canvas) drawKline(canvas, data, [], bars[bars.length - 1].close, name || "");
}
