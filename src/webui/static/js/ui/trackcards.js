/* 标的跟踪的卡片渲染：清单表、计划摘要、执行录入表、历史时间线。
 *
 * 只负责拼 HTML，不发请求；数据与交互都在 views/track.js。
 * 价位口径复用 ui/planprices.js（与报告页、总控台同一份规则）。
 */
import { chip, esc, fmt, fmtPct, kindClass, num } from "../core/util.js";
import { nearestTarget, precisePrice } from "./planprices.js";

const EXEC_STATES = ["已执行", "部分执行", "未执行", "已作废"];


function execBadge(ex) {
  if (!ex || !ex["是否录入"]) return chip("未录执行", "flat");
  const c = ex["计数"] || {};
  const done = (c["已执行"] || 0) + (c["部分执行"] || 0);
  if (done > 0) return chip("已执行 " + done, "ok");
  if ((c["未执行"] || 0) > 0) return chip("未执行 " + c["未执行"], "warn");
  return chip("已录执行", "ok");
}


function dirCls(dir) {
  const d = String(dir || "");
  if (d.indexOf("多") >= 0) return "up";
  if (d.indexOf("空") >= 0) return "down";
  return "";
}


export function trackListHtml(cards, checked) {
  if (!cards || !cards.length) {
    return '<tbody><tr><td class="empty">跟踪清单还是空的：上面填 6 位代码点「加入跟踪」，' +
      "或点「从自选股导入」。加入后每只标的每天生成一份交易计划。</td></tr></tbody>";
  }
  let html = "<thead><tr><th></th><th>代码</th><th>名称</th><th class='num'>现价</th>" +
    "<th class='num'>涨跌幅</th><th>最新计划</th><th>方向</th><th>执行情况</th>" +
    "<th class='num'>历史</th><th>操作</th></tr></thead><tbody>";
  cards.forEach(c => {
    const on = checked.has(c["代码"]);
    const flags = [];
    if (c["待生成"]) flags.push(chip(c["计划路径"] ? "待生成新计划" : "还没有计划", "warn"));
    if (c["需要执行记录"]) flags.push(chip("待录执行", "bad"));
    if (c["计划已过期"]) flags.push(chip("计划已过期", "warn"));
    const plan = c["计划路径"]
      ? '<a href="#" data-open="' + esc(c["代码"]) + '">' + esc(c["适用交易日"] || "—") + "</a>" +
        '<div class="muted">' + esc(c["生成时间"] || "") + "</div>"
      : '<span class="muted">—</span>';
    html += "<tr>" +
      '<td><input type="checkbox" data-check="' + esc(c["代码"]) + '"' + (on ? " checked" : "") + " /></td>" +
      "<td><b>" + esc(c["代码"]) + "</b></td>" +
      "<td>" + esc(c["名称"] || "—") +
        (c["备注"] ? '<div class="muted">' + esc(c["备注"]) + "</div>" : "") + "</td>" +
      '<td class="num">' + (c["现价"] == null ? "—" : fmt(c["现价"], 3)) + "</td>" +
      '<td class="num ' + kindClass(c["涨跌幅_pct"]) + '">' +
        (c["涨跌幅_pct"] == null ? "—" : fmtPct(c["涨跌幅_pct"])) + "</td>" +
      "<td>" + plan + " " + flags.join(" ") + "</td>" +
      '<td class="' + dirCls(c["方向"]) + '">' + esc(c["方向"] || "—") +
        (c["置信度"] == null ? "" : '<span class="muted"> ' + esc(c["置信度"]) + "</span>") + "</td>" +
      "<td>" + execBadge(c["执行"]) + "</td>" +
      '<td class="num">' + esc(c["历史条数"] == null ? "—" : c["历史条数"]) + "</td>" +
      '<td><a href="#" data-detail="' + esc(c["代码"]) + '">详情</a>&nbsp;&nbsp;' +
        '<a href="#" data-gen="' + esc(c["代码"]) + '">生成</a>&nbsp;&nbsp;' +
        '<button class="btn sm danger" data-untrack="' + esc(c["代码"]) + '">移除</button></td>' +
      "</tr>";
  });
  return html + "</tbody>";
}


export function trackLevelsHtml(brief, levels, lots) {
  if (!levels) return '<div class="muted">这份计划里没有关键价位。</div>';
  const price = num((brief || {})["价格"]);
  const rows = [];
  const paren = parts => {
    const bits = (parts || []).filter(x => x && String(x).trim());
    return bits.length ? "（" + bits.map(x => esc(x)).join(" ") + "）" : "";
  };
  const buy = precisePrice(levels["买点"], price);
  if (buy !== null) {
    rows.push("买点 " + fmt(buy) + paren([(levels["买点"] || {})["动作"], (lots || {})["买点"]]));
  }
  const sell = precisePrice(levels["减仓"], price);
  if (sell !== null) {
    rows.push(((levels["减仓"] || {})["动作"] || "减仓") + " " + fmt(sell) +
      paren([(lots || {})["减仓"]]));
  }
  if (levels["止损"] != null) {
    rows.push("止损 " + fmt(levels["止损"]) +
      paren([(lots || {})["止损"] ? "可用 " + lots["止损"] : ""]));
  }
  const goal = nearestTarget(levels["目标"], price);
  if (goal) {
    rows.push("止盈点 " + fmt(goal["v"]) +
      paren([(lots || {})["止盈"] ? "持仓 " + lots["止盈"] : ""]) +
      (goal["依据"] ? '<span class="muted"> · ' + esc(goal["依据"]) + "</span>" : ""));
  }
  (levels["支撑"] || []).forEach(g => rows.push("支撑 " + fmt(g["价位"]) +
    (g["依据"] ? '<span class="muted"> · ' + esc(g["依据"]) + "</span>" : "")));
  (levels["压力"] || []).forEach(g => rows.push("压力 " + fmt(g["价位"]) +
    (g["依据"] ? '<span class="muted"> · ' + esc(g["依据"]) + "</span>" : "")));
  return '<div class="tk-levels">' + rows.map(r => "<div>" + r + "</div>").join("") + "</div>";
}


export function trackPlanTableHtml(plan) {
  if (!plan || !plan.length) return '<div class="muted">这份计划里没有可执行条目。</div>';
  let html = '<div class="table-wrap" style="max-height:none"><table class="tbl"><thead><tr>' +
    "<th>#</th><th>动作</th><th>触发条件</th><th>价格区间</th><th class='num'>股数</th>" +
    "<th class='num'>金额</th><th>失效条件</th></tr></thead><tbody>";
  plan.forEach(r => {
    html += "<tr>" +
      "<td>" + esc(r["编号"]) + "</td>" +
      "<td><b>" + esc(r["动作"] || "—") + "</b>" + (r["无动作"] ? chip("无动作", "flat") : "") + "</td>" +
      '<td class="wrap muted">' + esc(r["触发条件"] || "—") + "</td>" +
      '<td class="mono">' +
        esc(Array.isArray(r["价格区间"]) ? r["价格区间"].join(" ~ ") : (r["价格区间"] || "—")) + "</td>" +
      '<td class="num">' + (r["股数"] == null ? "—" : fmt(r["股数"], 0)) + "</td>" +
      '<td class="num">' + (r["金额_元"] == null ? "—" : fmt(r["金额_元"], 0)) + "</td>" +
      '<td class="wrap muted">' + esc(r["失效条件"] || "—") + "</td></tr>";
  });
  return html + "</tbody></table></div>";
}


export function trackExecFormHtml(detail) {
  const latest = detail["最新"];
  if (!latest) {
    return '<div class="muted">这个标的还没有跟踪计划，先生成一份再录入执行情况。</div>';
  }
  const saved = {};
  ((detail["执行记录"] || {})["条目"] || []).forEach(r => { saved[r["编号"]] = r; });
  let html = '<div class="table-wrap" style="max-height:none"><table class="tbl tk-exec">' +
    "<thead><tr><th>#</th><th>动作</th><th>计划区间</th><th>执行状态</th>" +
    "<th class='num'>成交价</th><th class='num'>成交股数</th><th>备注</th></tr></thead><tbody>";
  (latest["计划"] || []).forEach(p => {
    const old = saved[p["编号"]] || {};
    const state = old["执行状态"] || (p["无动作"] ? "已作废" : "未执行");
    html += '<tr data-no="' + esc(p["编号"]) + '">' +
      "<td>" + esc(p["编号"]) + "</td>" +
      "<td><b>" + esc(p["动作"] || "—") + "</b><div class='muted'>计划 " +
        esc(p["股数"] == null ? "—" : fmt(p["股数"], 0)) + " 股</div></td>" +
      '<td class="mono">' +
        esc(Array.isArray(p["价格区间"]) ? p["价格区间"].join(" ~ ") : (p["价格区间"] || "—")) + "</td>" +
      '<td><select class="inline-select" data-state>' +
        EXEC_STATES.map(s => '<option value="' + s + '"' + (state === s ? " selected" : "") +
          ">" + s + "</option>").join("") + "</select></td>" +
      '<td class="num"><input class="inline-input" type="number" step="0.001" data-price value="' +
        esc(old["成交价"] == null ? "" : old["成交价"]) + '" /></td>' +
      '<td class="num"><input class="inline-input" type="number" step="100" data-shares value="' +
        esc(old["成交股数"] == null ? "" : old["成交股数"]) + '" /></td>' +
      '<td><input class="inline-input" data-note value="' + esc(old["备注"] || "") + '" /></td></tr>';
  });
  html += "</tbody></table></div>" +
    '<div class="row" style="margin-top:8px;gap:8px;align-items:center">' +
    '<input class="inline-input" id="track-exec-note" style="flex:1 1 240px" placeholder="总体备注（可选）" value="' +
      esc((detail["执行记录"] || {})["总体备注"] || "") + '" />' +
    '<button class="btn ghost" id="btn-track-exec-none">全部标为未执行</button>' +
    '<button class="btn primary" id="btn-track-exec-save">保存执行记录</button>' +
    '<span class="muted" id="track-exec-state">' + execBadge(detail["执行摘要"]) +
      (((detail["执行记录"] || {})["录入时间"]) ? " " + esc(detail["执行记录"]["录入时间"]) : "") +
    "</span></div>";
  return html;
}


export function trackDetailHtml(detail) {
  if (!detail) return '<div class="muted">在清单里点「详情」看某只标的的计划与执行记录。</div>';
  const latest = detail["最新"];
  const brief = detail["现价"] || {};
  let html = '<div class="tk-detail-head"><b>' + esc(detail["名称"] || detail["代码"]) + "</b> " +
    '<span class="mono muted">' + esc(detail["代码"]) + "</span> " +
    chip("适用交易日 " + (detail["适用交易日"] || "—"), "accent") +
    chip(brief["来源"] || "—", brief["实时"] ? "ok" : "warn") +
    (detail["需录入执行记录"] ? chip("待录执行记录", "bad") : "") + "</div>" +
    '<div class="muted">' + esc(detail["交易日口径"] || "") + "</div>";
  if (!latest) {
    return html + '<div class="empty" style="margin-top:10px">还没有计划产物：勾选这只标的后点' +
      "「生成选中标的的计划」。</div>";
  }
  html += '<div class="tk-sum">' +
    '<div class="gauge"><span class="k">方向</span><span class="v ' + dirCls(latest["方向"]) + '">' +
      esc(latest["方向"] || "—") + "</span></div>" +
    '<div class="gauge"><span class="k">置信度</span><span class="v">' +
      esc(latest["置信度"] == null ? "—" : latest["置信度"]) + "</span></div>" +
    '<div class="gauge"><span class="k">现价</span><span class="v">' +
      (brief["价格"] == null ? "—" : fmt(brief["价格"], 3)) + "</span></div>" +
    '<div class="gauge"><span class="k">涨跌幅</span><span class="v ' + kindClass(brief["涨跌幅_pct"]) + '">' +
      fmtPct(brief["涨跌幅_pct"]) + "</span></div>" +
    '<div class="gauge"><span class="k">数据交易日</span><span class="v" style="font-size:14px">' +
      esc(latest["数据交易日"] || "—") + "</span></div></div>";
  html += '<div class="hero-quote" style="margin:10px 0">' +
    esc(latest["一句话结论"] || "（本次没有模型计划）") + "</div>";
  (detail["提示"] || []).forEach(h => { html += '<div class="muted">· ' + esc(h) + "</div>"; });
  (latest["降级"] || []).forEach(h => { html += '<div class="muted">· ' + esc(h) + "</div>"; });
  html += '<div class="sec-title">关键价位（精确价 + 手数）</div>' +
    trackLevelsHtml(brief, latest["关键价位"], latest["手数"]);
  html += '<div class="sec-title">交易计划</div>' + trackPlanTableHtml(latest["计划"]);
  const mech = detail["机械参考"] || {};
  if ((mech["计划"] || []).length) {
    html += '<div class="sec-title">机械参考（只读，不代表是否成交）</div>' +
      '<div class="muted">' + esc(mech["说明"] || "") + "</div>";
    (mech["计划"] || []).forEach(r => {
      html += '<div class="muted">· ' + esc(r["编号"]) + ") " + esc(r["动作"] || "—") + " ｜ " +
        esc(r["状态"] || "—") + " ｜ " + esc(r["说明"] || "") + "</div>";
    });
    (mech["关键价位"] || []).forEach(r => {
      html += '<div class="muted">· ' + esc(r["类型"]) + " " + fmt(r["价位"]) + " ｜ " +
        esc(r["状态"]) + " ｜ " + esc(r["说明"]) + "</div>";
    });
  }
  html += '<div class="sec-title">执行情况录入（以你填的为准）</div>' +
    '<div id="track-exec">' + trackExecFormHtml(detail) + "</div>";
  html += '<div class="sec-title">历史计划</div>' +
    '<div class="table-wrap" style="max-height:280px"><table class="tbl"><thead><tr>' +
    "<th>生成时间</th><th>适用交易日</th><th>方向</th><th>结论</th><th>执行</th><th>操作</th>" +
    "</tr></thead><tbody>" +
    (detail["历史"] || []).map(h => "<tr><td>" + esc(h["时间"]) + "</td><td>" +
      esc(h["适用交易日"] || "—") + "</td><td class='" + dirCls(h["方向"]) + "'>" +
      esc(h["方向"] || "—") + "</td><td class='wrap muted'>" +
      esc((h["一句话结论"] || "").slice(0, 60)) + "</td><td>" + execBadge(h["执行"]) + "</td>" +
      "<td><a href='#' data-plan='" + esc(h["json路径"]) + "'>打开</a>&nbsp;&nbsp;" +
      "<button class='btn sm danger' data-del='" + esc(h["json路径"]) + "'>删除</button></td></tr>").join("") +
    "</tbody></table></div>";
  return html;
}
