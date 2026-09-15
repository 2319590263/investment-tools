/* 荐股结果卡：推荐榜（模型分降序）、一票否决、机械口径、候选池。
 *
 * 只渲染，不发请求；数据来自 /api/pick 的产物 JSON。
 * 口径：排名看模型评分；机械分是按《机器打分逻辑.txt》算的辅助列与初筛依据。
 */
import { badge, chip, esc, fmt, fmtMoney, fmtPct, num, pctClass } from "../core/util.js";

function cell(v, d, pct) {
  const n = num(v);
  if (n === null) return '<td class="num muted">—</td>';
  return '<td class="num' + (pct ? " " + pctClass(n) : "") + '">' +
    (pct ? fmtPct(n, d) : fmt(n, d)) + "</td>";
}

export function pickGradeChip(g) {
  const cls = g === "关注" ? "ok" : (g === "观察" ? "flat" : (g === "回避" ? "bad" : "flat"));
  return badge(g || "—", cls);
}

function scoreCell(v) {
  const n = num(v);
  if (n === null) return '<td class="num muted">机械分补位</td>';
  const cls = n >= 80 ? "up" : (n >= 60 ? "" : "muted");
  return '<td class="num ' + cls + '"><b>' + fmt(n, 1) + "</b></td>";
}


/** 推荐榜：模型评分降序的 30 只（模型没有的用机械分补位，明确标注）。 */
export function pickRankCard(p) {
  const rows = p["推荐榜"] || [];
  if (!rows.length) return "";
  let html = '<div class="card" id="pick-rank"><div class="card-h"><span>推荐榜</span>' +
    chip("模型评分 " + rows.length + " 只", "accent") +
    '<div class="spacer"></div><span class="muted">' +
    esc((p["配置"] || {}).model || "—") + " / " +
    esc((p["配置"] || {}).profile || "") +
    " ｜ 机械分只作辅助</span></div>" +
    '<div class="card-b table-wrap"><table class="tbl"><thead><tr>' +
    "<th class='num'>#</th><th>标的</th><th>打法</th><th>板块</th>" +
    "<th class='num'>现价</th><th class='num'>涨跌幅</th><th class='num'>模型评分</th>" +
    "<th class='num'>机械分</th><th>评级</th><th>理由</th><th>操作</th></tr></thead><tbody>";
  rows.forEach(r => {
    const code = r["代码"] || "";
    html += '<tr><td class="num muted">' + (r["排名"] || "") + "</td>" +
      "<td><b>" + esc(r["名称"] || code) + '</b><div class="mono muted">' + esc(code) +
        (r["来源"] === "机械分补位" ? " · 模型未点评" : "") + "</div></td>" +
      "<td>" + chip(r["打法"] || "—", "accent") + "</td>" +
      '<td class="muted wrap">' + esc(r["所属板块"] || r["行业"] || "—") + "</td>" +
      cell(r["现价"], 3) + cell(r["涨跌幅_pct"], 2, true) + scoreCell(r["评分"]) +
      cell(r["机械分"], 1) + "<td>" + pickGradeChip(r["评级"]) + "</td>" +
      '<td class="wrap muted">' + esc(r["理由"] || "—") + "</td>" +
      '<td><a href="#" data-pk-run="' + esc(code) + '">研判</a> ' +
      '<a href="#" data-pk-kline="' + esc(code) + '" data-pk-name="' + esc(r["名称"] || "") +
      '">K线</a> ' +
      '<button class="btn sm" data-pk-watch="' + esc(code) + '" data-pk-name="' +
      esc(r["名称"] || "") + '">加自选</button></td></tr>';
  });
  return html + "</tbody></table></div></div>";
}


/** 一票否决：命中的标的（分全池 / 前150 两阶段）。 */
export function pickVetoCard(p) {
  const rows = p["被否决"] || [];
  const summary = p["一票否决"] || {};
  if (!rows.length && !summary["口径"]) return "";
  let html = '<div class="card"><div class="card-h"><span>一票否决</span>' +
    chip("本轮淘汰 " + rows.length + " 只", rows.length ? "warn" : "flat") +
    '<div class="spacer"></div><span class="muted">' +
    esc(summary["口径"] || "") + "</span></div>";
  html += '<div class="card-b">' +
    '<div class="hint">全池执行：' + esc((summary["全池"] || []).join("、")) + "</div>" +
    '<div class="hint">前 150 只执行：' + esc((summary["前150"] || []).join("、")) + "</div>" +
    ((summary["未覆盖"] || []).length
      ? '<div class="hint warn">未覆盖：' +
        (summary["未覆盖"] || []).map(x => esc(x["项"] + "（" + x["原因"] + "）")).join("；") +
        "</div>" : "") +
    "</div>";
  if (rows.length) {
    html += '<div class="card-b table-wrap" style="max-height:260px"><table class="tbl">' +
      "<thead><tr><th>阶段</th><th>代码</th><th>名称</th><th>原因</th></tr></thead><tbody>" +
      rows.map(v => "<tr><td>" + esc(v["阶段"]) + '</td><td class="mono">' + esc(v["代码"]) +
        "</td><td>" + esc(v["名称"] || "") + '</td><td class="muted wrap">' +
        esc(v["原因"]) + "</td></tr>").join("") + "</tbody></table></div>";
  }
  return html + "</div>";
}


/** 机械口径：可得满分、缺失项、未覆盖项 + 机械层前 40 名。 */
export function pickMechanicCard(p) {
  const cal = p["机械口径"] || {};
  const rows = p["机械层"] || [];
  if (!cal["说明"] && !rows.length) return "";
  let html = '<div class="card"><div class="card-h"><span>机械分（辅助）</span>' +
    chip("可得满分 " + (cal["可得总分"] == null ? "—" : cal["可得总分"]) + " / 100", "flat") +
    '<div class="spacer"></div><span class="muted">严格照《机器打分逻辑.txt》，不参与排名</span></div>' +
    '<div class="card-b"><div class="hint">' + esc(cal["说明"] || "") + "</div>";
  if ((cal["缺失"] || []).length) {
    html += '<div class="hint">缺失项（只从分母去掉，不记 0）：' +
      cal["缺失"].map(x => esc(x["指标"] + "（" + x["满分"] + " 分：" + x["原因"] + "）"))
        .join("；") + "</div>";
  }
  html += "</div>";
  if (rows.length) {
    html += '<div class="card-b table-wrap" style="max-height:320px"><table class="tbl">' +
      "<thead><tr><th>代码</th><th>名称</th><th class='num'>机械分</th>" +
      "<th class='num'>实得</th><th class='num'>可得</th></tr></thead><tbody>" +
      rows.slice(0, 60).map(r => '<tr><td class="mono">' + esc(r["代码"]) + "</td><td>" +
        esc(r["名称"] || "") + "</td>" + cell(r["机械分"], 1) + cell(r["实得"], 1) +
        cell(r["可得"], 1) + "</tr>").join("") + "</tbody></table></div>";
  }
  return html + "</div>";
}


/** 候选池：默认只显示前 30（避免页面被 150 行刷屏），可展开。 */
export function pickPoolCard(p, showAll) {
  const rows = p["候选池"] || [];
  if (!rows.length) return "";
  const show = showAll ? rows : rows.slice(0, 30);
  let html = '<div class="card"><div class="card-h"><span>送模型的候选池</span>' +
    chip(rows.length + " 只", "flat") +
    '<div class="spacer"></div><span class="muted">' +
    "扫描 " + ((p["扫描"] || {})["全市场总数"] || "—") + " 只 → 候选池 " +
    ((p["扫描"] || {})["候选池数量"] || 0) + " 只 → 送模型 " + rows.length + " 只</span></div>" +
    '<div class="card-b table-wrap"><table class="tbl"><thead><tr>' +
    "<th>代码</th><th>名称</th><th>行业</th><th class='num'>现价</th>" +
    "<th class='num'>涨跌幅</th><th class='num'>成交额</th><th class='num'>换手</th>" +
    "<th class='num'>主力净流入(万)</th></tr></thead><tbody>";
  show.forEach(r => {
    html += '<tr><td class="mono">' + esc(r["代码"]) + "</td><td>" + esc(r["名称"] || "") +
      '</td><td class="muted">' + esc(r["行业"] || "") + "</td>" +
      cell(r["现价"], 3) + cell(r["涨跌幅_pct"], 2, true) +
      cell(r["成交额_亿"], 1) + cell(r["换手率_pct"], 2) + cell(r["主力净流入_万"], 0) +
      "</tr>";
  });
  html += "</tbody></table></div>";
  html += '<div class="card-b"><button class="btn ghost sm" id="btn-pick-pool-more">' +
    (showAll ? "只看前 30 只" : ("显示全部 " + rows.length + " 只")) + "</button></div>";
  return html + "</div>";
}
