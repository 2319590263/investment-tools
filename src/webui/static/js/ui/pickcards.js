/* 荐股结果页的候选表与模型点评卡片。
 *
 * 候选榜是「四模块合并去重后的一张表」（共 30 只）：同一只股票只占一行，主行取机械分最高的那条，
 * 模块列给出它在哪几个模块入选；per 模块的模型点评与首推明细单独成卡，矩阵点模块名仍能跳过去。
 */
import { badge, chip, esc, fmt, fmtPct, num, pctClass } from "../core/util.js";
import { PICK_MODULE_CYCLE } from "./pickfilter.js";


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


/** 候选合并：同一只股票在多个模块入选只占一行；主行取该股机械分最高的那条。 */
export function pickMergedRows(cands) {
  const by = {}, order = [];
  Object.keys(cands || {}).forEach(m => (cands[m] || []).forEach(r => {
    const code = String(r["代码"] || "");
    if (!code) return;
    if (!by[code]) {
      by[code] = Object.assign({}, r, { 模块列表: [m] });
      order.push(code);
      return;
    }
    if (by[code]["模块列表"].indexOf(m) < 0) by[code]["模块列表"].push(m);
    const now = num(r["机械分"]), keep = num(by[code]["机械分"]);
    if ((now === null ? -1 : now) > (keep === null ? -1 : keep)) {
      const mods = by[code]["模块列表"];
      Object.assign(by[code], r);
      by[code]["模块列表"] = mods;
    }
  }));
  return order.map(c => by[c]).sort((x, y) => {
    const a = num(x["机械分"]), b = num(y["机械分"]);
    return (b === null ? -1 : b) - (a === null ? -1 : a);
  });
}

function pickFirstMap(p) {
  const first = {};
  (p["模块"] || []).forEach(mm => (mm["首推"] || []).forEach(c => {
    first[String(c["代码"] || "")] = Object.assign({ 模块: mm["模块"] }, c);
  }));
  return first;
}

function pickModuleCell(row) {
  const mods = row["模块列表"] || (row["模块"] ? [row["模块"]] : []);
  return mods.map(m => chip(m, m === row["模块"] ? "accent" : "flat")).join(" ") ||
    '<span class="muted">—</span>';
}

/** 候选榜：四个模块合并去重后**一张表**（共 30 只），不再按模块各列一遍。 */
export function pickCandidateCard(p) {
  const rows = pickMergedRows(p["候选"] || {});
  if (!rows.length) return "";
  const first = pickFirstMap(p);
  let html = '<div class="card" id="pick-cand"><div class="card-h"><span>候选榜</span>' +
    chip("四模块合并 " + rows.length + " 只", "accent") +
    '<div class="spacer"></div><span class="muted">同一只股票只占一行，模块列给出它在哪几个模块入选</span></div>' +
    '<div class="card-b table-wrap"><table class="tbl"><thead><tr>' +
    "<th>代码</th><th>名称</th><th>模块</th><th>来源板块</th>" +
    "<th class='num'>现价</th><th class='num'>涨跌幅</th><th class='num'>5日</th><th class='num'>20日</th>" +
    "<th class='num'>换手</th><th class='num'>量比</th><th class='num'>主力净流入(亿)</th>" +
    "<th class='num'>机械分</th><th>模型结论</th><th>精确价位（含依据）</th><th>操作</th></tr></thead><tbody>";
  rows.forEach(r => {
    const f = first[String(r["代码"])] || {};
    html += "<tr><td><b>" + esc(r["代码"]) + "</b></td><td>" + esc(r["名称"]) + "</td>" +
      "<td>" + pickModuleCell(r) + "</td>" +
      '<td class="muted">' + esc(r["来源板块"] || "") + "</td>" +
      pickCell(r["现价"], 2, false) + pickCell(r["涨跌幅_pct"], 2, true) +
      pickCell(r["5日_pct"], 2, true) + pickCell(r["20日_pct"], 2, true) +
      pickCell(r["换手率_pct"], 2, false) + pickCell(r["量比"], 2, false) +
      pickCell(r["主力净流入_亿"], 3, false) + pickCell(r["机械分"], 1, false) +
      "<td>" + (f["评级"] ? chip("首推 " + (f["模块"] || ""), "accent") + " " +
        badge(f["评级"], f["评级"] === "关注" ? "ok" : (f["评级"] === "回避" ? "bad" : "flat"))
        : '<span class="muted">—</span>') +
      (f["理由"] ? '<div class="hint wrap">' + esc(f["理由"]) + "</div>" : "") + "</td>" +
      '<td class="wrap">' + pickPrices(f) + "</td>" +
      '<td><a href="#" data-pick-run="' + esc(r["代码"]) + '">研判</a> ' +
      '<a href="#" data-pick-kline="' + esc(r["代码"]) + '">K线</a> ' +
      '<button class="btn sm" data-pick-watch="' + esc(r["代码"]) + '" data-pick-watch-name="' +
      esc(r["名称"] || "") + '">加入自选</button></td></tr>';
  });
  return html + "</tbody></table></div></div>";
}

/** 模块级模型点评（评分 / 逻辑 / 介入节奏 / 失效条件 + 首推明细）；矩阵点模块名跳到这里。 */
export function pickModuleCards(p) {
  const mods = p["模块"] || [];
  if (!mods.length) return "";
  return mods.map(mm => {
    const name = String(mm["模块"] || "");
    const picks = mm["首推"] || [];
    let html = '<div class="card" id="pick-mod-' + esc(name) + '"><div class="card-h"><span>' +
      esc(name) + " · 模型点评</span>" + chip(PICK_MODULE_CYCLE[name] || "", "flat") +
      (mm["评分"] == null ? "" : chip("模型评分 " + mm["评分"], "accent")) +
      '<div class="spacer"></div><span class="muted">首推 ' + picks.length + " 只</span></div>";
    if (mm["逻辑"] || mm["介入节奏"] || mm["失效条件"]) {
      html += '<div class="card-b note">' +
        (mm["逻辑"] ? "<div><b>逻辑</b> " + esc(mm["逻辑"]) + "</div>" : "") +
        (mm["介入节奏"] ? "<div><b>介入节奏</b> " + esc(mm["介入节奏"]) + "</div>" : "") +
        (mm["失效条件"] ? "<div><b>失效条件</b> " + esc(mm["失效条件"]) + "</div>" : "") +
        "</div>";
    }
    if (picks.length) {
      html += '<div class="card-b">' + picks.map(c => '<div style="padding:7px 0;border-bottom:1px solid var(--line-soft)">' +
        "<b>" + esc(c["代码"]) + " " + esc(c["名称"] || "") + "</b> " +
        (c["所属板块"] ? chip(c["所属板块"], "flat") : "") + " " +
        (c["评级"] ? badge(c["评级"], c["评级"] === "关注" ? "ok" : (c["评级"] === "回避" ? "bad" : "flat")) : "") +
        (c["理由"] ? '<div class="hint">' + esc(c["理由"]) + "</div>" : "") +
        pickPrices(c) + "</div>").join("") + "</div>";
    }
    return html + "</div>";
  }).join("");
}

