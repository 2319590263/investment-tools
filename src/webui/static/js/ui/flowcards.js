/* 交易流：流卡片、开流表单、加标的表单、详情（标的切换 · 成交 · 体检 · 事件 · 计划）。
 *
 * 只拼 HTML，不发请求；数据与交互在 views/flow.js。
 * 一条流 = 一笔流资金 + 若干只标的，每只标的选一种打法；分配资金之和不能超过流资金。
 * 关键价位与计划表复用 ui/trackcards.js（报告页 / 总控台 / 交易流同一口径）。
 */
import { chip, esc, fmt, fmtMoney, kindClass, num } from "../core/util.js";
import { trackLevelsHtml, trackPlanTableHtml } from "./trackcards.js";

/* 打法口径与后端 flow.STYLES 一致（改了后端这里也要改）。 */
export const STYLES = ["超短线", "短线", "波段"];

const STATE_CLS = {"进行中": "accent", "已达标": "ok", "已止损": "bad",
                   "未达标清仓": "warn", "已结束": "flat", "已终止": "flat"};


export function stateChip(state) {
  return chip(state || "—", STATE_CLS[state] || "flat");
}


function markCls(level) {
  return {"bad": "bad", "ok": "ok", "accent": "accent", "warn": "warn"}[level] || "flat";
}


export function progressHtml(pnl, params) {
  const total = num(pnl["合计_元"]) || 0;
  const target = num(pnl["目标盈利_元"]) || num((params || {})["目标盈利_元"]) || 0;
  const loss = num(pnl["最大亏损_元"]) || num((params || {})["最大亏损_元"]) || 0;
  const span = (target || 0) + (loss || 0);
  if (!span) return "";
  const pct = Math.max(0, Math.min(100, (total + loss) / span * 100));
  const cls = total < 0 ? "down" : "up";
  return '<div class="fl-bar"><div class="fl-bar-fill ' + cls + '" style="width:' + pct.toFixed(1) + '%"></div>' +
    '<span class="fl-bar-zero" style="left:' + (loss / span * 100).toFixed(1) + '%"></span></div>' +
    '<div class="fl-bar-note muted">亏损线 -' + fmt(loss, 0) + " 元 ｜ 目标 +" + fmt(target, 0) + " 元" +
    " ｜ 进度 " + fmt(pnl["进度_pct"], 1) + "%</div>";
}


function styleOptions(pick) {
  return STYLES.map(s => '<option value="' + esc(s) + '"' +
    (s === pick ? " selected" : "") + ">" + esc(s) + "</option>").join("");
}


function planCell(t) {
  const plan = t["计划"] || {};
  if (!plan["产物路径"]) {
    return '<div class="muted">还没有计划：点「计算」出第一份</div>';
  }
  const bits = [plan["方向"] || "—",
                (plan["置信度"] == null ? "" : "置信度 " + plan["置信度"]),
                (plan["适用交易日"] ? "适用 " + plan["适用交易日"] : "")].filter(Boolean);
  return '<div class="muted">' + esc(bits.join(" ｜ ")) +
    (plan["错误"] ? ' · <span class="down">上次生成失败</span>' : "") + "</div>" +
    '<a href="#" data-plan="' + esc(plan["产物路径"]) + '">打开计划报告</a>';
}


function targetRow(t) {
  const pnl = t["盈亏"] || {};
  const pos = t["持仓"] || {};
  const marks = t["到价"] || [];
  const stale = (t["待重算"] || {})["待重算"];
  const code = t["代码"] || "";
  let html = '<tr data-code="' + esc(code) + '">';
  html += "<td><b>" + esc(t["名称"] || code) + "</b>" +
    '<div class="mono muted">' + esc(code) + (t["是否ETF"] ? " · ETF" : "") + "</div>" +
    planCell(t) + "</td>";
  html += "<td>" + chip(t["打法"] || "—", "accent") + stateChip(t["状态"]) +
    (stale ? chip("待重算", "warn") : "") + "</td>";
  html += '<td class="num">' + fmtMoney(t["分配资金"], 0) + "</td>";
  html += '<td class="num">' + (pnl["现价"] == null ? "—" : fmt(pnl["现价"], 3)) +
    '<div class="muted">' + esc(t["价格来源"] || "") + "</div></td>";
  html += '<td class="num">' + fmt(pos["股数"], 0) +
    '<div class="muted">平均成本 ' + fmt(pos["平均成本"], 3) + "</div></td>";
  html += '<td class="num ' + kindClass(pnl["合计_元"]) + '">' + fmtMoney(pnl["合计_元"], 0) + "</td>";
  html += '<td class="num ' + kindClass(pnl["收益率_pct"]) + '">' + fmt(pnl["收益率_pct"], 2) +
    '%<div class="muted">进度 ' + fmt(pnl["进度_pct"], 1) + "%</div></td>";
  html += '<td class="wrap muted">' + esc(t["下一步"] || "") + "</td>";
  html += "<td>" + (marks.length
    ? marks.slice(0, 2).map(m => chip(m["类型"], markCls(m["级别"]))).join("")
    : '<span class="muted">—</span>') + "</td>";
  html += "</tr>";
  /* 分时图紧贴标的那一行，**操作按钮放到分时图下方**（批注 2/3）：
     表格不再有按钮列，行高只由标的信息决定，中间不会再有空白。 */
  html += '<tr class="fl-chart-row"><td colspan="9">' +
    '<canvas class="fl-chart" data-chart-code="' + esc(code) + '"></canvas>' +
    '<div class="muted" data-chart-note="' + esc(code) + '">正在取分时 …</div>' +
    '<div class="row fl-row-act">' +
    '<button class="btn sm ghost" data-act="minutes" data-code="' + esc(code) + '">分时</button>' +
    '<button class="btn sm ghost" data-act="detail" data-code="' + esc(code) + '">详情</button>' +
    '<button class="btn sm ghost" data-act="plan" data-code="' + esc(code) + '">计算</button>' +
    '<button class="btn sm ghost" data-act="check" data-code="' + esc(code) + '">体检</button>' +
    '<button class="btn sm ghost" data-act="setstyle" data-code="' + esc(code) + '">改打法</button>' +
    (t["状态"] === "进行中"
      ? '<button class="btn sm ghost" data-act="closetarget" data-code="' + esc(code) + '">结束</button>'
      : "") +
    '<button class="btn sm danger" data-act="remove" data-code="' + esc(code) + '">移除</button>' +
    "</div></td></tr>";
  return html;
}


export function flowCard(c) {
  const p = c["参数"] || {};
  const sum = c["汇总"] || {};
  const targets = c["标的"] || [];
  const remain = num(sum["剩余资金"]);
  let html = '<div class="flow-card" data-fid="' + esc(c["流编号"]) + '">';
  html += '<div class="fl-h"><b class="mono">' + esc(c["流编号"]) + "</b>" + stateChip(c["状态"]) +
    chip("流资金 " + fmtMoney(p["流资金"], 0) + " 元", "flat") +
    chip("目标 " + fmt(p["目标收益率_pct"], 1) + "% → +" + fmtMoney(sum["目标盈利_元"], 0), "flat") +
    chip("最大亏损 " + fmt(p["最大亏损_pct"], 1) + "% → -" + fmtMoney(sum["最大亏损_元"], 0), "flat") +
    chip("分配 " + fmtMoney(sum["分配合计"], 0) + " / " + fmtMoney(p["流资金"], 0) + " 元",
         remain && remain > 0 ? "warn" : "flat") +
    chip("标的 " + targets.length + " 只", "flat") +
    (c["结束原因"] ? chip("结束：" + c["结束原因"], "flat") : "") +
    '<div class="spacer"></div>' +
    '<span class="muted">创建 ' + esc(c["创建时间"] || "—") + "</span></div>";
  html += '<div class="fl-quote"><b class="' + kindClass(sum["合计_元"]) + '">' +
    fmtMoney(sum["合计_元"], 0) + " 元</b>" +
    '<span class="' + kindClass(sum["收益率_pct"]) + '">按流资金 ' + fmt(sum["收益率_pct"], 2) + "%</span>" +
    (sum["按成本收益率_pct"] == null ? "" :
      '<span class="muted">按投入成本 ' + fmt(sum["按成本收益率_pct"], 2) + "%</span>") +
    '<span class="muted">已实现 ' + fmtMoney(sum["已实现_元"], 0) + " ｜ 浮动 " +
    (sum["浮动_元"] == null ? "—" : fmtMoney(sum["浮动_元"], 0)) + "</span></div>";
  html += progressHtml(sum, p);
  html += '<div class="table-wrap"><table class="tbl fl-targets"><thead><tr>' +
    "<th>标的 / 计划</th><th>打法 / 状态</th><th class='num'>分配资金</th><th class='num'>现价</th>" +
    "<th class='num'>持仓</th><th class='num'>合计盈亏</th><th class='num'>收益率</th>" +
    "<th>操作</th><th>到价</th></tr></thead><tbody>" +
    (targets.length ? targets.map(targetRow).join("")
      : '<tr><td class="empty" colspan="9">这条流还没有标的：在下面「添加标的」里加一只。</td></tr>') +
    "</tbody></table></div>";
  html += '<details class="fl-add"><summary>添加标的（还可以加 ' +
    (remain == null ? "—" : fmtMoney(remain, 0)) + " 元）</summary>" +
    '<div class="row" style="gap:8px;flex-wrap:wrap;align-items:flex-end;margin-top:8px">' +
    '<div class="field" style="margin:0;flex:0 0 140px"><label>证券代码</label>' +
      '<input data-add="code" placeholder="如 600967" /></div>' +
    '<div class="field" style="margin:0;flex:1 1 130px"><label>名称 <span class="muted">可选</span></label>' +
      '<input data-add="name" /></div>' +
    '<div class="field" style="margin:0;flex:0 0 120px"><label>打法</label>' +
      '<select data-add="style">' + styleOptions("短线") + "</select></div>" +
    '<div class="field" style="margin:0;flex:0 0 140px"><label>分配资金（元）</label>' +
      '<input data-add="alloc" type="number" min="1" step="1000" placeholder="剩余 ' +
      (remain == null ? "—" : fmt(remain, 0)) + '" /></div>' +
    '<label class="sw"><input type="checkbox" data-add="bring" /> 按持仓文件带入底仓</label>' +
    '<button class="btn primary" data-act="add">添加标的</button></div>' +
    '<div class="err" data-add-msg></div>' +
    '<div class="muted">一只标的只能属于一条流；流内所有标的的分配资金之和不能超过流资金。' +
    "打法决定计划的时间尺度：超短线 1-3 日 / 短线 1-5 日 / 波段 2-6 周。</div></details>";
  html += '<div class="fl-act">' +
    '<button class="btn sm ghost" data-act="params">改流参数</button>' +
    '<button class="btn sm" data-act="plan">全部计算计划</button>' +
    (c["状态"] === "进行中" ? '<button class="btn sm ghost" data-act="close">结束整条流</button>' : "") +
    '<button class="btn sm danger" data-act="delete">删除流</button></div>';
  return html + "</div>";
}


export function flowCardsHtml(cards) {
  if (!cards || !cards.length) {
    return '<div class="empty">还没有交易流：在下面「开新流」里填流资金、目标收益率与最大亏损，' +
      "开好之后往里面加标的（每只选一种打法），就会按打法生成计划并从建仓一路盯到清仓。</div>";
  }
  return cards.map(flowCard).join("");
}


export function closedLine(c) {
  const names = (c["标的"] || []).map(t => t["名称"] || t["代码"]).join("、");
  return '<div class="fl-closed" data-fid="' + esc(c["流编号"]) + '">· <b class="mono">' +
    esc(c["流编号"]) + "</b> " + esc(c["状态"] || "") + " " + esc(names) +
    (c["结束原因"] ? "（" + esc(c["结束原因"]) + "）" : "") + "</div>";
}


export function flowFormHtml(hint) {
  return '<div class="row" style="gap:8px;flex-wrap:wrap;align-items:flex-end">' +
    '<div class="field" style="margin:0;flex:0 0 150px"><label>流资金（元）</label>' +
      '<input id="flow-new-capital" type="number" min="1" step="1000" placeholder="50000" /></div>' +
    '<div class="field" style="margin:0;flex:0 0 110px"><label>目标收益率 %</label>' +
      '<input id="flow-new-target" type="number" min="0.1" step="0.5" placeholder="15" /></div>' +
    '<div class="field" style="margin:0;flex:0 0 110px"><label>最大亏损 %</label>' +
      '<input id="flow-new-loss" type="number" min="0.1" step="0.5" placeholder="8" /></div>' +
    '<div class="field" style="margin:0;flex:1 1 160px"><label>备注 <span class="muted">可选</span></label>' +
      '<input id="flow-new-note" /></div></div>' +
    '<div class="row" style="gap:8px;flex-wrap:wrap;align-items:flex-end;margin-top:8px">' +
    '<div class="field" style="margin:0;flex:0 0 140px"><label>第一只标的 <span class="muted">可选</span></label>' +
      '<input id="flow-new-code" placeholder="如 512890" /></div>' +
    '<div class="field" style="margin:0;flex:1 1 130px"><label>名称 <span class="muted">可选</span></label>' +
      '<input id="flow-new-name" /></div>' +
    '<div class="field" style="margin:0;flex:0 0 120px"><label>打法</label>' +
      '<select id="flow-new-style">' + styleOptions("短线") + "</select></div>" +
    '<div class="field" style="margin:0;flex:0 0 130px"><label>分配资金（元）</label>' +
      '<input id="flow-new-alloc" type="number" min="1" step="1000" placeholder="默认全部流资金" /></div>' +
    '<label class="sw"><input type="checkbox" id="flow-new-bring" /> 按持仓文件带入底仓</label>' +
    '<label class="sw"><input type="checkbox" id="flow-new-plan" checked /> 开流即生成首份计划</label>' +
    '<button class="btn primary" id="btn-flow-create">开流</button></div>' +
    '<div class="err" id="flow-msg"></div>' +
    '<div class="muted">' + esc(hint || "") + "</div>";
}


export function targetFormHtml(code, cur) {
  return '<div class="row" style="gap:8px;flex-wrap:wrap;align-items:flex-end">' +
    '<div class="field" style="margin:0;flex:0 0 130px"><label>标的</label>' +
      '<input id="flow-set-code" value="' + esc(code || "") + '" readonly /></div>' +
    '<div class="field" style="margin:0;flex:0 0 130px"><label>打法</label>' +
      '<select id="flow-set-style">' + styleOptions((cur || {}).打法) + "</select></div>" +
    '<div class="field" style="margin:0;flex:0 0 160px"><label>分配资金（元）</label>' +
      '<input id="flow-set-alloc" type="number" min="1" step="1000" value="' +
      esc((cur || {})["分配资金"] || "") + '" /></div>' +
    '<button class="btn primary" id="btn-flow-set-save">保存</button></div>' +
    '<div class="err" id="flow-set-msg"></div>' +
    '<div class="muted">改分配资金时会校验：流内所有标的之和不能超过流资金。</div>';
}


export function paramsFormHtml(p) {
  p = p || {};
  return '<div class="row" style="gap:8px;flex-wrap:wrap;align-items:flex-end">' +
    '<div class="field" style="margin:0;flex:0 0 150px"><label>流资金（元）</label>' +
      '<input id="flow-p-capital" type="number" min="1" step="1000" value="' +
      esc(p["流资金"] == null ? "" : p["流资金"]) + '" /></div>' +
    '<div class="field" style="margin:0;flex:0 0 120px"><label>目标收益率 %</label>' +
      '<input id="flow-p-target" type="number" min="0.1" step="0.5" value="' +
      esc(p["目标收益率_pct"] == null ? "" : p["目标收益率_pct"]) + '" /></div>' +
    '<div class="field" style="margin:0;flex:0 0 120px"><label>最大亏损 %</label>' +
      '<input id="flow-p-loss" type="number" min="0.1" step="0.5" value="' +
      esc(p["最大亏损_pct"] == null ? "" : p["最大亏损_pct"]) + '" /></div>' +
    '<div class="field" style="margin:0;flex:1 1 160px"><label>备注 <span class="muted">可选</span></label>' +
      '<input id="flow-p-note" value="' + esc(p["备注"] || "") + '" /></div>' +
    '<button class="btn primary" id="btn-flow-p-save">保存</button></div>' +
    '<div class="err" id="flow-p-msg"></div>' +
    '<div class="muted">流资金不能小于流内标的分配资金合计；改完目标收益率 / 最大亏损后，' +
    "每只标的的目标盈利与最大亏损金额会按各自的分配资金重算。</div>";
}


function fillRow(t, f) {
  return "<tr><td>" + esc(f["序号"]) + "</td><td>" + esc(f["日期"]) + " " + esc(f["时间"] || "") +
    '</td><td class="' + (f["方向"] === "卖出" ? "down" : "up") + '">' + esc(f["方向"]) + "</td>" +
    '<td class="num">' + fmt(f["价格"], 4) + "</td>" +
    '<td class="num">' + fmt(f["数量"], 0) + "</td>" +
    '<td class="num">' + fmtMoney(f["金额"], 0) + "</td>" +
    '<td class="num">' + fmt(f["手续费"], 2) + "</td>" +
    "<td>" + esc(f["来源"] || "") +
    (f["备注"] ? '<div class="muted">' + esc(f["备注"]) + "</div>" : "") + "</td>" +
    '<td><button class="btn sm danger" data-delfill="' + esc(f["序号"]) +
    '" data-code="' + esc(t["代码"]) + '">删除</button></td></tr>';
}


function checkBlock(rec) {
  if (!rec) return "";
  const cls = {"继续执行": "ok", "提高警惕": "warn", "暂停新动作": "warn", "建议作废重算": "bad"}[rec["结论"]] || "flat";
  let html = '<div class="fl-check"><div>' + chip("体检 " + esc(rec["结论"]), cls) +
    '<span class="muted">' + esc(rec["时间"]) + " ｜ 模型 " + esc(rec["模型"] || "—") +
    " ｜ 事实包 " + esc(rec["事实包字符数"] || "—") + " 字符</span></div>" +
    "<div>" + esc(rec["一句话"] || "") + "</div>";
  if (rec["补充说明"]) html += '<div class="muted">你补充的观察：' + esc(rec["补充说明"]) + "</div>";
  (rec["依据"] || []).forEach(x => { html += "<div>· " + esc(String(x)) + "</div>"; });
  if (rec["处理建议"]) html += '<div class="muted">处理建议：' + esc(rec["处理建议"]) + "</div>";
  (rec["风险"] || []).forEach(x => { html += '<div class="muted">· ' + esc(String(x)) + "</div>"; });
  if (rec["error"]) html += '<div class="fail">' + esc(rec["error"]) + "</div>";
  return html + "</div>";
}


function targetBody(t, tools) {
  const c = t["卡"] || {};
  const pnl = t["盈亏"] || {};
  const plan = t["计划"] || {};
  const lots = t["手数"] || {};
  const fills = t["成交"] || [];
  const code = t["代码"];
  let html = '<div class="fl-detail-head"><b>' + esc(t["名称"] || code) + "</b>" +
    '<span class="mono muted">' + esc(code) + "</span>" +
    chip("打法 " + (t["打法"] || "—"), "accent") + stateChip(c["状态"]) +
    chip("分配资金 " + fmtMoney(t["分配资金"], 0) + " 元") +
    (c["结束原因"] ? chip("结束：" + c["结束原因"], "flat") : "") +
    '<div class="spacer"></div>' + (tools || "") + "</div>";
  html += '<div class="fl-meta"><div>现价 ' + (pnl["现价"] == null ? "—" : fmt(pnl["现价"], 3)) +
    "（" + esc(pnl["价格来源"] || "—") + " ｜ " + esc(pnl["价格时间"] || "—") + "）</div>" +
    "<div>持仓 " + fmt((t["持仓"] || {})["股数"], 0) + " 股 ｜ 平均成本 " +
    fmt((t["持仓"] || {})["平均成本"], 4) + " ｜ 期初：" +
    esc((t["期初"] || {})["说明"] || "—") + "</div>" +
    "<div>已实现 " + fmtMoney(pnl["已实现_元"], 0) + " 元 ｜ 浮动 " +
    (pnl["浮动_元"] == null ? "—" : fmtMoney(pnl["浮动_元"], 0)) + " 元 ｜ 合计 " +
    fmtMoney(pnl["合计_元"], 0) + " 元（" + fmt(pnl["收益率_pct"], 2) + "%）</div></div>";
  html += progressHtml(pnl, c["参数"] || {});
  if (plan["一句话结论"]) html += '<div class="hero-quote">' + esc(plan["一句话结论"]) + "</div>";
  html += '<div class="sec-title">当前计划的关键价位（精确价 + 手数）</div>' +
    trackLevelsHtml({价格: pnl["现价"]}, t["关键价位"], lots);
  /* 分时图：买卖点标在成交价上，计划线用同一份 planprices.js 收敛（批注 1）。 */
  html += '<div class="sec-title">当日分时（买卖线）' +
    '<button class="btn sm ghost fl-min-refresh" data-act="minutes" data-code="' + esc(code) +
    '">刷新分时</button></div>' +
    '<div class="fl-chart-box"><canvas class="fl-chart" data-chart-code="' + esc(code) + '"></canvas>' +
    '<div class="muted" data-chart-note="' + esc(code) + '">正在取分时 …</div></div>';
  html += '<div class="sec-title">当前计划条目</div>' + trackPlanTableHtml(t["计划条目"]);
  html += '<div class="sec-title">补录成交（以你录的为准 · 只算盈亏不改计划）</div>' +
    '<div class="row" style="gap:8px;flex-wrap:wrap;align-items:flex-end">' +
    '<div class="field" style="margin:0;flex:0 0 100px"><label>方向</label>' +
      '<select data-fill="side" class="inline-select"><option>买入</option><option>卖出</option></select></div>' +
    '<div class="field" style="margin:0;flex:0 0 110px"><label>成交价</label>' +
      '<input data-fill="price" type="number" step="0.001" /></div>' +
    '<div class="field" style="margin:0;flex:0 0 120px"><label>数量（股）</label>' +
      '<input data-fill="qty" type="number" step="100" /></div>' +
    '<div class="field" style="margin:0;flex:0 0 150px"><label>日期</label>' +
      '<input data-fill="day" placeholder="YYYY-MM-DD" /></div>' +
    '<div class="field" style="margin:0;flex:1 1 160px"><label>备注 <span class="muted">可选</span></label>' +
      '<input data-fill="note" /></div>' +
    '<button class="btn primary" data-act="fill" data-code="' + esc(code) + '">记一笔</button>' +
    '<button class="btn ghost" data-act="sync" data-code="' + esc(code) + '">从台账同步</button></div>';
  html += '<div class="table-wrap" style="max-height:320px"><table class="tbl"><thead><tr>' +
    "<th>#</th><th>时间</th><th>方向</th><th class='num'>价格</th><th class='num'>数量</th>" +
    "<th class='num'>金额</th><th class='num'>手续费</th><th>来源 / 备注</th><th>操作</th></tr></thead><tbody>" +
    (fills.length ? fills.map(f => fillRow(t, f)).join("")
      : '<tr><td class="empty" colspan="9">还没有成交记录：可以手工补录，或跑一次「同步同花顺」后点「从台账同步」。</td></tr>') +
    "</tbody></table></div>" +
    '<div class="muted">台账：' + esc((t["台账"] || {})["最近同步"] || "还没同步过") +
    " ｜ 更早（加入之前）的同标的成交未并入： " + esc((t["台账"] || {})["未并入"] || 0) + " 笔</div>";
  html += '<div class="sec-title">体检记录（意外排查）</div>' +
    ((t["体检"] || []).length
      ? (t["体检"] || []).slice().reverse().map(checkBlock).join("")
      : '<div class="muted">还没体检过：点「体检」排查消息面 / 技术面 / 基本面是否出意外。</div>');
  html += '<div class="sec-title">这只标的的事件时间线</div><div class="fl-events">' +
    ((t["事件"] || []).length ? (t["事件"] || []).map(e =>
      '<div class="' + (e["级别"] === "bad" ? "down" : (e["级别"] === "ok" ? "up" : "muted")) + '">' +
      "· " + esc(e["时间"]) + " ｜ " + esc(e["类型"]) + " ｜ " + esc(e["文案"]) + "</div>").join("")
      : '<div class="muted">还没有事件</div>') + "</div>";
  return html;
}


export function flowDetailHtml(d) {
  if (!d) return '<div class="muted">在流卡片上点「详情」。</div>';
  const c = d["卡"] || {};
  const p = c["参数"] || {};
  const sum = d["汇总"] || c["汇总"] || {};
  const blocks = d["标的"] || [];
  if (!blocks.length) return '<div class="muted">这条流还没有标的：在卡片上「添加标的」。</div>';
  const sel = d["选中"] || blocks[0]["代码"];
  let html = '<div class="fl-detail-head"><b class="mono">' + esc(c["流编号"]) + "</b>" +
    stateChip(c["状态"]) +
    chip("流资金 " + fmtMoney(p["流资金"], 0) + " 元") +
    chip("目标 " + fmt(p["目标收益率_pct"], 1) + "% → +" + fmtMoney(sum["目标盈利_元"], 0) + " 元") +
    chip("最大亏损 " + fmt(p["最大亏损_pct"], 1) + "% → -" + fmtMoney(sum["最大亏损_元"], 0) + " 元") +
    chip("分配 " + fmtMoney(sum["分配合计"], 0) + " / " + fmtMoney(p["流资金"], 0) + " 元") +
    chip("标的 " + blocks.length + " 只", "flat") + "</div>";
  html += '<div class="fl-meta"><div>流级合计：已实现 ' + fmtMoney(sum["已实现_元"], 0) + " 元 ｜ 浮动 " +
    (sum["浮动_元"] == null ? "—" : fmtMoney(sum["浮动_元"], 0)) + " 元 ｜ 合计 " +
    fmtMoney(sum["合计_元"], 0) + " 元（按流资金 " + fmt(sum["收益率_pct"], 2) + "%）</div></div>" +
    progressHtml(sum, p);
  html += '<div class="fl-tabs">' + blocks.map(t => {
    const on = t["代码"] === sel;
    return '<button class="btn sm' + (on ? "" : " ghost") + '" data-tab="' + esc(t["代码"]) + '">' +
      esc(t["名称"] || t["代码"]) + " · " + esc(t["打法"] || "") + "</button>";
  }).join("") + "</div>";
  html += '<div class="fl-tab-bodies">' + blocks.map(t =>
    '<div data-body="' + esc(t["代码"]) + '"' + (t["代码"] === sel ? "" : " hidden") + ">" +
    targetBody(t) + "</div>").join("") + "</div>";
  html += '<div class="sec-title">流事件（加标的 / 改参数 / 结束）</div><div class="fl-events">' +
    ((d["事件"] || []).length ? (d["事件"] || []).map(e =>
      '<div class="' + (e["级别"] === "bad" ? "down" : (e["级别"] === "ok" ? "up" : "muted")) + '">' +
      "· " + esc(e["时间"]) + " ｜ " + esc(e["类型"]) + " ｜ " + esc(e["文案"]) + "</div>").join("")
      : '<div class="muted">还没有流事件</div>') + "</div>";
  return html;
}
