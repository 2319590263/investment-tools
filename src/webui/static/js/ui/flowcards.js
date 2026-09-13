/* 交易流：卡片、开流表单、详情（成交 / 事件 / 体检 / 计划）。
 *
 * 只拼 HTML，不发请求；数据与交互在 views/flow.js。
 * 关键价位与计划表复用 ui/trackcards.js（报告页 / 总控台 / 交易流同一口径）。
 */
import { chip, esc, fmt, fmtMoney, fmtPct, kindClass, num } from "../core/util.js";
import { nearestTarget, precisePrice } from "./planprices.js";
import { trackLevelsHtml, trackPlanTableHtml } from "./trackcards.js";

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
  const target = num(params["目标盈利_元"]) || 0;
  const loss = num(params["最大亏损_元"]) || 0;
  const span = (target || 0) + (loss || 0);
  if (!span) return "";
  const pct = Math.max(0, Math.min(100, (total + loss) / span * 100));
  const cls = total < 0 ? "down" : "up";
  return '<div class="fl-bar"><div class="fl-bar-fill ' + cls + '" style="width:' + pct.toFixed(1) + '%"></div>' +
    '<span class="fl-bar-zero" style="left:' + (loss / span * 100).toFixed(1) + '%"></span></div>' +
    '<div class="fl-bar-note muted">亏损线 -' + fmt(loss, 0) + " 元 ｜ 目标 +" + fmt(target, 0) + " 元" +
    " ｜ 进度 " + fmt(pnl["进度_pct"], 1) + "%</div>";
}


export function flowCard(c) {
  const p = c["参数"] || {};
  const pnl = c["盈亏"] || {};
  const pos = c["持仓"] || {};
  const plan = c["计划"] || {};
  const marks = c["到价"] || [];
  const stale = c["待重算"] || {};
  const chk = c["最近体检"];
  const total = num(pnl["合计_元"]);
  let html = '<div class="flow-card" data-fid="' + esc(c["流编号"]) + '">';
  html += '<div class="fl-h"><b>' + esc((c["标的"] || {})["名称"] || (c["标的"] || {})["代码"]) + "</b>" +
    '<span class="mono muted">' + esc((c["标的"] || {})["代码"]) + "</span>" +
    stateChip(c["状态"]) +
    chip("本流 " + fmtMoney(p["本流资金"], 0) + " 元", "flat") +
    chip("目标 " + fmt(p["目标收益率_pct"], 1) + "% / 亏 " + fmt(p["最大亏损_pct"], 1) + "%", "flat") +
    (chk ? chip("体检 " + (chk["结论"] || "—"), markCls(chk["结论"] === "建议作废重算" ? "bad" : (chk["结论"] === "继续执行" ? "ok" : "warn"))) : "") +
    (stale["待重算"] ? chip("待重算计划", "warn") : "") +
    '<div class="spacer"></div>' +
    '<span class="muted">' + esc(c["价格来源"] || "") + (c["价格时间"] ? " · " + esc(c["价格时间"]) : "") + "</span></div>";
  html += '<div class="fl-quote"><b class="' + kindClass(pnl["现价"] == null ? null : (pnl["收益率_pct"] || 0)) + '">' +
    (pnl["现价"] == null ? "—" : fmt(pnl["现价"], 3)) + "</b>" +
    '<span class="' + kindClass(pnl["收益率_pct"]) + '">合计 ' + fmtMoney(total, 0) + " 元（" +
    fmt(pnl["收益率_pct"], 2) + "%）</span>" +
    (pnl["按成本收益率_pct"] == null ? "" : '<span class="muted">按投入成本 ' + fmt(pnl["按成本收益率_pct"], 2) + "%</span>") + "</div>";
  html += '<div class="fl-meta">' +
    "<div>持仓 " + fmt(pos["股数"], 0) + " 股（可用 " + fmt(pos["可用"], 0) + "）｜ 平均成本 " +
    fmt(pos["平均成本"], 4) + "</div>" +
    "<div>已实现 " + fmtMoney(pnl["已实现_元"], 0) + " 元 ｜ 浮动 " +
    (pnl["浮动_元"] == null ? "—" : fmtMoney(pnl["浮动_元"], 0) + " 元") + "</div>" +
    "<div>" + esc(c["下一步"] || "") + "</div></div>";
  html += progressHtml(pnl, p);
  if (marks.length) {
    html += '<div class="fl-marks">' + marks.slice(0, 4).map(m =>
      chip(m["类型"], markCls(m["级别"])) + '<span class="muted">' + esc(m["文案"] || "") + "</span>").join("") + "</div>";
  }
  if ((c["提示"] || []).length) {
    html += (c["提示"] || []).map(t => '<div class="muted">· ' + esc(t) + "</div>").join("");
  }
  html += '<div class="fl-act">' +
    '<button class="btn sm" data-act="detail">详情</button>' +
    '<button class="btn sm ghost" data-act="plan">重算计划</button>' +
    '<button class="btn sm ghost" data-act="fill">补录成交</button>' +
    '<button class="btn sm ghost" data-act="check">体检</button>' +
    (c["状态"] === "进行中" ? '<button class="btn sm ghost" data-act="close">结束流</button>' : "") +
    '<button class="btn sm danger" data-act="delete">删除</button></div>';
  return html + "</div>";
}


export function flowCardsHtml(cards) {
  if (!cards || !cards.length) {
    return '<div class="empty">还没有交易流：在下面「跟踪清单」里挑一只点「开流」，' +
      "填好本流资金、目标收益率、最大亏损，就会生成首份计划并从建仓一路盯到清仓。</div>";
  }
  return cards.map(flowCard).join("");
}


export function flowFormHtml(hint) {
  return '<div class="row" style="gap:8px;flex-wrap:wrap;align-items:flex-end">' +
    '<div class="field" style="margin:0;flex:0 0 140px"><label>证券代码</label>' +
      '<input id="flow-new-code" placeholder="如 600967" /></div>' +
    '<div class="field" style="margin:0;flex:1 1 140px"><label>名称 <span class="muted">可选</span></label>' +
      '<input id="flow-new-name" /></div>' +
    '<div class="field" style="margin:0;flex:0 0 130px"><label>本流资金（元）</label>' +
      '<input id="flow-new-capital" type="number" min="1" step="1000" placeholder="30000" /></div>' +
    '<div class="field" style="margin:0;flex:0 0 110px"><label>目标收益率 %</label>' +
      '<input id="flow-new-target" type="number" min="0.1" step="0.5" placeholder="15" /></div>' +
    '<div class="field" style="margin:0;flex:0 0 110px"><label>最大亏损 %</label>' +
      '<input id="flow-new-loss" type="number" min="0.1" step="0.5" placeholder="8" /></div>' +
    '<div class="field" style="margin:0;flex:0 0 100px"><label>最多加仓</label>' +
      '<input id="flow-new-adds" type="number" min="0" max="20" step="1" value="2" /></div>' +
    '<label class="sw"><input type="checkbox" id="flow-new-bring" /> 按持仓文件带入底仓</label>' +
    '<label class="sw"><input type="checkbox" id="flow-new-plan" checked /> 开流即生成首份计划</label>' +
    '<button class="btn primary" id="btn-flow-create">开流</button></div>' +
    '<div class="err" id="flow-msg"></div>' +
    '<div class="muted">' + esc(hint || "") + "</div>";
}


function fillRow(f) {
  return "<tr><td>" + esc(f["序号"]) + "</td><td>" + esc(f["日期"]) + " " + esc(f["时间"] || "") +
    '</td><td class="' + (f["方向"] === "卖出" ? "down" : "up") + '">' + esc(f["方向"]) + "</td>" +
    '<td class="num">' + fmt(f["价格"], 4) + "</td>" +
    '<td class="num">' + fmt(f["数量"], 0) + "</td>" +
    '<td class="num">' + fmtMoney(f["金额"], 0) + "</td>" +
    '<td class="num">' + fmt(f["手续费"], 2) + "</td>" +
    "<td>" + esc(f["来源"] || "") + (f["备注"] ? '<div class="muted">' + esc(f["备注"]) + "</div>" : "") + "</td>" +
    '<td><button class="btn sm danger" data-delfill="' + esc(f["序号"]) + '">删除</button></td></tr>';
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


export function flowDetailHtml(d) {
  if (!d) return '<div class="muted">在卡片上点「详情」。</div>';
  const c = d["卡"] || {};
  const p = c["参数"] || {};
  const pnl = d["盈亏"] || {};
  const plan = d["计划"] || {};
  const lots = d["手数"] || {};
  const fills = d["成交"] || [];
  let html = '<div class="fl-detail-head"><b>' + esc((c["标的"] || {})["名称"] || "") + "</b>" +
    '<span class="mono muted">' + esc((c["标的"] || {})["代码"]) + "</span>" + stateChip(c["状态"]) +
    chip("本流 " + fmtMoney(p["本流资金"], 0) + " 元") +
    chip("目标 " + fmt(p["目标收益率_pct"], 1) + "% → +" + fmtMoney(p["目标盈利_元"], 0) + " 元") +
    chip("最大亏损 " + fmt(p["最大亏损_pct"], 1) + "% → -" + fmtMoney(p["最大亏损_元"], 0) + " 元") +
    (c["结束原因"] ? chip("结束：" + c["结束原因"], "flat") : "") + "</div>";
  html += '<div class="fl-meta"><div>现价 ' + (pnl["现价"] == null ? "—" : fmt(pnl["现价"], 3)) +
    "（" + esc(pnl["价格来源"] || "—") + " ｜ " + esc(pnl["价格时间"] || "—") + "）</div>" +
    "<div>持仓 " + fmt((d["持仓"] || {})["股数"], 0) + " 股 ｜ 平均成本 " +
    fmt((d["持仓"] || {})["平均成本"], 4) + " ｜ 期限初：" +
    esc((d["流"]["期初"] || {})["说明"] || "—") + "</div>" +
    "<div>已实现 " + fmtMoney(pnl["已实现_元"], 0) + " 元 ｜ 浮动 " +
    (pnl["浮动_元"] == null ? "—" : fmtMoney(pnl["浮动_元"], 0)) + " 元 ｜ 合计 " +
    fmtMoney(pnl["合计_元"], 0) + " 元（" + fmt(pnl["收益率_pct"], 2) + "%）</div></div>" +
    progressHtml(pnl, p);
  if (plan["一句话结论"]) {
    html += '<div class="hero-quote">' + esc(plan["一句话结论"]) + "</div>";
  }
  html += '<div class="sec-title">当前计划的关键价位（精确价 + 手数）</div>' +
    trackLevelsHtml({价格: pnl["现价"]}, d["关键价位"], lots);
  html += '<div class="sec-title">当前计划条目</div>' + trackPlanTableHtml(d["计划条目"]);
  html += '<div class="sec-title">补录成交（以你录的为准）</div>' +
    '<div class="row" style="gap:8px;flex-wrap:wrap;align-items:flex-end">' +
    '<div class="field" style="margin:0;flex:0 0 100px"><label>方向</label>' +
      '<select id="flow-fill-side" class="inline-select"><option>买入</option><option>卖出</option></select></div>' +
    '<div class="field" style="margin:0;flex:0 0 110px"><label>成交价</label>' +
      '<input id="flow-fill-price" type="number" step="0.001" /></div>' +
    '<div class="field" style="margin:0;flex:0 0 120px"><label>数量（股）</label>' +
      '<input id="flow-fill-qty" type="number" step="100" /></div>' +
    '<div class="field" style="margin:0;flex:0 0 150px"><label>日期</label>' +
      '<input id="flow-fill-day" placeholder="YYYY-MM-DD" /></div>' +
    '<div class="field" style="margin:0;flex:1 1 160px"><label>备注 <span class="muted">可选</span></label>' +
      '<input id="flow-fill-note" /></div>' +
    '<button class="btn primary" id="btn-flow-fill">记一笔</button>' +
    '<button class="btn ghost" id="btn-flow-sync">从台账同步</button></div>';
  html += '<div class="table-wrap" style="max-height:320px"><table class="tbl"><thead><tr>' +
    "<th>#</th><th>时间</th><th>方向</th><th class='num'>价格</th><th class='num'>数量</th>" +
    "<th class='num'>金额</th><th class='num'>手续费</th><th>来源 / 备注</th><th>操作</th></tr></thead><tbody>" +
    (fills.length ? fills.map(fillRow).join("") :
      '<tr><td class="empty" colspan="9">还没有成交记录：可以手工补录，或跑一次「同步同花顺」后点「从台账同步」。</td></tr>') +
    "</tbody></table></div>" +
    '<div class="muted">台账：' + esc((d["台账"] || {})["最近同步"] || "还没同步过") +
    " ｜ 更早（开流之前）的同标的成交未并入： " + esc((d["台账"] || {})["未并入"] || 0) + " 笔</div>";
  html += '<div class="sec-title">体检记录（意外排查）</div>';
  if ((d["体检"] || []).length) {
    html += (d["体检"] || []).slice().reverse().map(checkBlock).join("");
  } else {
    html += '<div class="muted">还没体检过：点卡片上的「体检」排查消息面 / 技术面 / 基本面是否出意外。</div>';
  }
  html += '<div class="sec-title">事件时间线</div><div class="fl-events">' +
    ((d["事件"] || []).length ? (d["事件"] || []).map(e =>
      '<div class="' + (e["级别"] === "bad" ? "down" : (e["级别"] === "ok" ? "up" : "muted")) + '">' +
      "· " + esc(e["时间"]) + " ｜ " + esc(e["类型"]) + " ｜ " + esc(e["文案"]) + "</div>").join("")
      : '<div class="muted">还没有事件</div>') + "</div>";
  return html;
}
