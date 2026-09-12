/* 股票卡片：HTML + 分时小图（含计划价格线）。
 *
 * 只负责渲染与画图，不发请求；数据由 views/console.js 从 /api/overview 取。
 */
import { esc, fmt, fmtMoney, fmtPct, kindClass, num } from "../core/util.js";
import { nearestTarget, precisePrice } from "./planprices.js";

const COLOR = { buy: "#4c8dff", sell: "#c084fc", stop: "#2ec27e", target: "#e2b03c",
                flat: "#5b6b7f", up: "#f05a63", down: "#2ec27e" };

export function planLines(item) {
  const plan = item["计划"] || {};
  const price = num(item["现价"]);
  const out = [];
  const buy = precisePrice(plan["买点"], price);
  if (buy !== null) out.push({ v: buy, color: COLOR.buy, label: "买点" });
  if (num(plan["止损"]) !== null) out.push({ v: num(plan["止损"]), color: COLOR.stop, label: "止损" });
  const goal = nearestTarget(plan["目标"], price);
  if (goal) out.push({ v: goal.v, color: COLOR.target, label: "止盈点" });
  return out;
}

function markBadge(mark) {
  const cls = { buy: "accent", stop: "down", target: "warn", sell: "warn", up: "up", mute: "flat" }[mark["级别"]] || "flat";
  return '<span class="badge ' + cls + '">' + esc(mark["类型"]) + "</span>";
}

function planRows(item) {
  const plan = item["计划"];
  if (!plan) return "";
  const price = num(item["现价"]);
  const lots = plan["手数"] || {};
  const rows = [];
  const dot = color => '<span class="dot" style="background:' + color + '"></span>';
  const paren = parts => {
    const bits = parts.filter(x => x && String(x).trim());
    return bits.length ? "（" + bits.map(x => esc(x)).join(" ") + "）" : "";
  };
  const buy = precisePrice(plan["买点"], price);
  if (buy !== null) {
    rows.push(dot(COLOR.buy) + "买点 " + fmt(buy) +
      paren([((plan["买点"] || {})["动作"]), lots["买点"]]));
  }
  const sell = precisePrice(plan["减仓"], price);
  if (sell !== null) {
    const act = String(((plan["减仓"] || {})["动作"]) || "").trim();
    /* 卖出类直接用计划里的动作名（清仓 / 减仓 / 卖出），避免「减仓 1.19（清仓 61 手）」这种重复 */
    rows.push(dot(COLOR.sell) + (act || "减仓") + " " + fmt(sell) + paren([lots["减仓"]]));
  }
  if (num(plan["止损"]) !== null) {
    const holds = num(item["持有股数"]);
    rows.push(dot(COLOR.stop) + "止损 " + fmt(plan["止损"]) +
      paren([lots["止损"] ? "可用 " + lots["止损"] : (holds > 0 ? "T+1 今日不可卖" : "")]));
  }
  const goal = nearestTarget(plan["目标"], price);
  if (goal) {
    rows.push(dot(COLOR.target) + "止盈点 " + fmt(goal["v"]) +
      paren([lots["止盈"] ? "持仓 " + lots["止盈"] : ""]) +
      (goal["依据"] ? '<span class="muted"> · ' + esc(goal["依据"]) + "</span>" : ""));
  }
  (plan["支撑"] || []).forEach(g => rows.push(dot(COLOR.flat) + "支撑 " +
    fmt(g["价位"]) + (g["依据"] ? '<span class="muted"> · ' + esc(g["依据"]) + "</span>" : "")));
  (plan["压力"] || []).forEach(g => rows.push(dot(COLOR.flat) + "压力 " +
    fmt(g["价位"]) + (g["依据"] ? '<span class="muted"> · ' + esc(g["依据"]) + "</span>" : "")));
  if (!rows.length) return "";
  const src = plan["报告交易日"] ? '<span class="muted">报告基于 ' + esc(plan["报告交易日"]) + "</span>" : "";
  return '<div class="sc-plan">' + rows.join("") + (src ? '<div class="muted">' + src + "</div>" : "") + "</div>";
}

export function stockCard(item, kind) {
  const price = num(item["现价"]);
  const chg = num(item["涨跌幅_pct"]);
  const plan = item["计划"] || {};
  const marks = (item["触发"] || []).filter(m => m["级别"] !== "mute");
  const tip = (item["触发"] || []).find(m => m["级别"] === "mute") || null;
  const badges = [];
  if (item["是否ETF"]) badges.push('<span class="chip">ETF</span>');
  if (item["已持仓"]) badges.push('<span class="chip accent">已持仓</span>');
  marks.forEach(m => badges.push(markBadge(m)));
  if (num(item["冻结股数_当日买入不可卖"]) > 0) badges.push('<span class="chip warn">有冻结</span>');

  const meta = [];
  if (kind === "hold") {
    const pnl = num(item["浮动盈亏_元"]);
    meta.push('<div class="sc-row">成本 ' + fmt(item["成本价"], 3) + " ｜ 盈亏 " +
      '<span class="' + kindClass(pnl) + '">' + fmtMoney(pnl, 0) + " 元" +
      (num(item["盈亏比例_pct"]) === null ? "" : " (" + fmtPct(item["盈亏比例_pct"]) + ")") + "</span></div>");
    meta.push('<div class="sc-row">市值 ' + fmtMoney(item["持仓市值_元"], 0) + " 元" +
      (num(item["占总资金_pct"]) === null ? "" : " ｜ 占总资金 " + fmt(item["占总资金_pct"], 1) + "%") +
      "</div>");
    meta.push('<div class="sc-row muted">可用 ' + fmt(item["可用股数_可卖"], 0) + " / 冻结 " +
      fmt(item["冻结股数_当日买入不可卖"], 0) + " 股" +
      (num(item["持股天数"]) === null ? "" : ' ｜ <span title="持股天数">持有 ' + fmt(item["持股天数"], 0) + " 天</span>") +
      "</div>");
  } else if (item["备注"]) {
    meta.push('<div class="sc-row muted">备注 ' + esc(item["备注"]) + "</div>");
  }

  const lines = planLines(item);
  const legend = lines.length
    ? '<div class="sc-legend">' + lines.map(l => '<span><i style="background:' + l.color + '"></i>' +
        esc(l.label) + " " + fmt(l.v) + "</span>").join("") + "</div>"
    : "";
  const spark = item["分时"]
    ? '<canvas class="sc-spark" data-spark="' + esc(item["代码"]) + '"></canvas>'
    : '<div class="sc-hint">' + (price === null ? "未抓数" : "未取分时（点「刷新实盘价」或开自动刷新）") + "</div>";
  const note = tip ? '<div class="sc-note muted">' + esc(tip["文案"]) + "</div>" : "";
  const src = '<div class="sc-src muted">' + esc(item["价格来源"] || "—") +
    (item["价格时间"] ? " · " + esc(item["价格时间"]) : "") + "</div>";

  return '<div class="stock-card" data-code="' + esc(item["代码"]) + '" data-kind="' + kind + '">' +
    '<div class="sc-h"><b>' + esc(item["名称"] || item["代码"]) + '</b>' +
    '<span class="mono muted">' + esc(item["代码"]) + "</span>" + badges.join("") + "</div>" +
    '<div class="sc-price"><b class="' + kindClass(chg) + '">' +
    (price === null ? "—" : fmt(price, 3)) + '</b><span class="' + kindClass(chg) + '">' + fmtPct(chg) + "</span>" +
    src + "</div>" +
    '<div class="sc-sparkwrap">' + spark + "</div>" + legend +
    '<div class="sc-meta">' + meta.join("") + "</div>" + planRows(item) + note +
    '<div class="sc-act"><button class="btn sm" data-act="run">研判</button>' +
    '<button class="btn sm ghost" data-act="kline">K线</button></div></div>';
}

export function drawSpark(canvas, minute, lines, prevClose) {
  if (!canvas || !minute || !(minute["价格"] || []).length) return false;
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 220;
  const h = canvas.clientHeight || 56;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  const g = canvas.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);
  const series = (minute["价格"] || []).map(num).filter(v => v !== null);
  if (!series.length) return false;
  const avg = (minute["均价"] || []).map(num).filter(v => v !== null);
  const extras = lines.map(l => l.v).concat(num(prevClose) === null ? [] : [num(prevClose)]);
  const all = series.concat(avg, extras);
  const lo = Math.min.apply(null, all);
  const hi = Math.max.apply(null, all);
  const span = (hi - lo) || Math.max(0.01, hi * 0.01);
  const pad = 3;
  const y = v => h - pad - (v - lo) / span * (h - 2 * pad);
  const x = i => pad + i / Math.max(1, series.length - 1) * (w - 2 * pad);

  const prev = num(prevClose);
  if (prev !== null) {
    g.setLineDash([3, 3]);
    g.strokeStyle = "#5b6b7f";
    g.beginPath(); g.moveTo(0, y(prev)); g.lineTo(w, y(prev)); g.stroke();
  }
  lines.forEach(l => {
    g.setLineDash([5, 3]);
    g.strokeStyle = l.color;
    g.beginPath(); g.moveTo(0, y(l.v)); g.lineTo(w, y(l.v)); g.stroke();
  });
  g.setLineDash([]);
  if (avg.length > 1) {
    g.strokeStyle = "rgba(226,176,60,.75)";
    g.lineWidth = 1;
    g.beginPath();
    avg.forEach((v, i) => (i ? g.lineTo(x(i), y(v)) : g.moveTo(x(i), y(v))));
    g.stroke();
  }
  const last = series[series.length - 1];
  g.strokeStyle = (prev !== null && last < prev) ? COLOR.down : COLOR.up;
  g.lineWidth = 1.4;
  g.beginPath();
  series.forEach((v, i) => (i ? g.lineTo(x(i), y(v)) : g.moveTo(x(i), y(v))));
  g.stroke();
  return true;
}

export function bindStockCards(root, handlers) {
  if (!root) return;
  root.querySelectorAll(".stock-card").forEach(card => {
    const code = card.dataset.code;
    card.addEventListener("click", e => {
      const act = e.target.closest("[data-act]");
      if (act) {
        e.stopPropagation();
        if (act.dataset.act === "run" && handlers.onRun) handlers.onRun(code, card.dataset.kind);
        if (act.dataset.act === "kline" && handlers.onKline) handlers.onKline(code, card);
        return;
      }
      if (handlers.onOpen) handlers.onOpen(code, card.dataset.kind);
    });
  });
}
