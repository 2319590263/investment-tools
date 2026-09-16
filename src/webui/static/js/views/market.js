/* 大盘快照页与走势预测。 */
import { api } from "../core/api.js";
import { State, registerView } from "../core/app.js";
import { $, EMPTY, chip, dirColor, esc, fmt, fmtPct, freshNote, has, kindClass, num, sleep, toast } from "../core/util.js";
import { card, kv, listOrEmpty, pill, renderScenarioSummary } from "../ui/cards.js";
import { sceneCards } from "../ui/kline.js";

export function fcProfilePicker() {
  const profiles = (((State.state || {})["模型"]) || {}).profiles || [];
  const def = (((State.state || {})["模型"]) || {})["默认_profile"] || "";
  return '<select id="fc-profile" class="inline-select">' + profiles.map(p =>
    '<option value="' + esc(p["名称"]) + '"' + (p["名称"] === def ? " selected" : "") + ">" +
    esc(p["名称"]) + "</option>").join("") + "</select>";
}

export function marketForecastCard(res) {
  const fc = (res || {})["forecast"];
  const j = ((fc || {}).json) || {};
  const has = fc && Object.keys(j).length > 0;
  const actions = '<div class="fc-actions" style="margin-top:14px">' + fcProfilePicker() +
    '<button class="btn primary" id="fc-run">' + (has ? "重新生成预测" : "生成大盘走势预测") + "</button>" +
    '<span class="muted" id="fc-status"></span></div>' +
    '<pre class="console" id="fc-console" hidden></pre>' +
    '<div class="fc-note">这是一次真实的模型调用（产生 token 费用），默认用所选 profile 的「研判档」；' +
    '结果落在 data/ai/market/，不会写进报告的 plan_log。模型生成，不构成投资建议。</div>';

  if (!has) {
    return card("大盘走势预测",
      '<div class="muted">还没有生成。点下面的按钮，用当前 pan 快照调一次模型，给出方向、情景树、关键点位与风险。</div>' + actions);
  }

  const score = num(j["多空评分"]);
  const scoreCls = score === null ? dirColor(j["方向"]) : (score > 0 ? "up" : (score < 0 ? "down" : ""));
  let html = '<div class="gauge-row">' +
    '<div class="gauge"><span class="k">方向</span><span class="v ' + dirColor(j["方向"]) + '">' + esc(j["方向"] || "—") + "</span></div>" +
    '<div class="gauge"><span class="k">置信度</span><span class="v">' + (j["置信度"] == null ? "—" : j["置信度"]) + "</span></div>" +
    '<div class="gauge"><span class="k">多空评分</span><span class="v ' + scoreCls + '">' + (score === null ? "—" : score) + "</span></div>" +
    '<div class="gauge"><span class="k">时间窗</span><span class="v" style="font-size:14px">' + esc(j["时间窗"] || "—") + "</span></div>" +
    '<div class="gauge"><span class="k">生成时间</span><span class="v" style="font-size:12.5px">' + esc(fc.generated_at || "—") + "</span></div>" +
    '<div class="gauge"><span class="k">模型</span><span class="v" style="font-size:12.5px">' + esc((fc.provider || "") + " / " + (fc.model || "")) + "</span></div>" +
    "</div>" +
    '<div class="hero-quote" style="margin-top:12px">' + esc(j["一句话结论"] || "（模型未给出结论）") + "</div>";

  const scenes = j["情景树"];
  if (Array.isArray(scenes) && scenes.length) {
    html += '<div style="margin-top:14px">' +
      renderScenarioSummary({ "情景树": scenes, "多空评分": j["多空评分"] }) + sceneCards(scenes) + "</div>";
  }

  const kp = j["关键价位"] || {};
  const pills = [];
  (kp["支撑"] || []).forEach(x => pills.push(pill(x["价位"], x["依据"], "up")));
  if (kp["止损价"] != null) pills.push(pill(kp["止损价"], "止损价", "stop"));
  (kp["压力"] || []).forEach(x => pills.push(pill(x["价位"], x["依据"], "down")));
  (kp["目标位"] || []).forEach(x => pills.push(pill(x["价位"], x["依据"], "target")));
  if (pills.length) html += '<div class="sec-title" style="margin-top:16px">关键点位</div><div>' + pills.join("") + "</div>";

  const secs = j["关注板块"];
  if (Array.isArray(secs) && secs.length) {
    html += '<div class="sec-title" style="margin-top:14px">关注板块</div>' +
      '<div class="table-wrap" style="max-height:none"><table class="tbl"><thead><tr><th>板块</th><th>方向</th><th>依据</th></tr></thead><tbody>' +
      secs.map(x => "<tr><td><b>" + esc(x["板块"] || "") + '</b></td><td class="' +
        (String(x["方向"] || "").indexOf("多") >= 0 ? "up" : "down") + '">' + esc(x["方向"] || "—") +
        '</td><td class="wrap muted">' + esc(x["依据"] || "") + "</td></tr>").join("") +
      "</tbody></table></div>";
  }

  const risks = j["风险"];
  if (Array.isArray(risks) && risks.length) {
    html += '<div class="sec-title" style="margin-top:14px">风险与应对</div>' +
      '<div class="table-wrap" style="max-height:none"><table class="tbl"><thead><tr><th>风险</th><th>监控指标</th><th>应对</th></tr></thead><tbody>' +
      risks.map(x => '<tr><td class="wrap"><b>' + esc(x["风险"] || "") + '</b></td><td class="wrap muted">' +
        esc(x["监控指标"] || "—") + '</td><td class="wrap">' + esc(x["应对"] || "—") + "</td></tr>").join("") +
      "</tbody></table></div>";
  }

  const dep = j["数据依赖"];
  if (dep) {
    html += '<div class="sec-title" style="margin-top:14px">数据依赖</div>' + listOrEmpty(dep["降级项"]) +
      (dep["缺失导致的不确定性"] ? '<div class="muted" style="margin-top:6px">' + esc(dep["缺失导致的不确定性"]) + "</div>" : "");
  }
  html += actions;
  return card("大盘走势预测", html);
}

/* 批注 5：四档打法推荐（超短线 / 短线 / 中线 / 长线 各一只）。
 * 数据来自最新一份荐股产物（点「一键抓取大盘快照」时勾上「同时荐股」就会顺带跑一轮）。 */
export function stylePicksCard(block) {
  const rows = (block || {})["行"] || [];
  let html = "";
  if (!rows.length) {
    html = '<div class="muted">还没有荐股产物：在下面点「一键抓取大盘快照」时勾上' +
      "「同时荐股」，或者去「荐股」页跑一次全大盘扫描。</div>";
    return card("打法推荐（四档各一只）", html);
  }
  html = '<div class="table-wrap" style="max-height:none"><table class="tbl" id="style-picks">' +
    "<thead><tr><th>打法</th><th>标的</th><th class='num'>现价</th><th class='num'>涨跌幅</th>" +
    "<th class='num'>模型评分</th><th class='num'>机械分</th><th>评级</th><th>理由</th></tr></thead><tbody>";
  rows.forEach(r => {
    const t = r["行"];
    html += "<tr><td>" + chip(r["打法"], "accent") + "</td>";
    if (!t) {
      html += '<td colspan="7" class="muted">' + esc(r["说明"] || "本次没有该档推荐") + "</td>";
    } else {
      html += "<td><b>" + esc(t["名称"] || "") + '</b> <span class="mono muted">' +
        esc(t["代码"] || "") + "</span></td>" +
        '<td class="num">' + (t["现价"] == null ? "—" : fmt(t["现价"], 3)) + "</td>" +
        '<td class="num ' + kindClass(t["涨跌幅_pct"]) + '">' + fmtPct(t["涨跌幅_pct"]) + "</td>" +
        '<td class="num"><b>' + (t["模型分"] == null ? "—" : fmt(t["模型分"], 0)) + "</b></td>" +
        '<td class="num muted">' + (t["机械分"] == null ? "—" : fmt(t["机械分"], 1)) + "</td>" +
        "<td>" + esc(t["评级"] || "—") + "</td>" +
        '<td class="wrap muted">' + esc(t["理由"] || "—") + "</td>";
    }
    html += "</tr>";
  });
  html += "</tbody></table></div>";
  html += '<div class="muted" style="margin-top:8px">' + esc((block || {})["口径"] || "") +
    "（价格是荐股产物里的快照价，要看实时价去「荐股」页）</div>";
  if ((block || {})["错误"]) html += '<div class="fail">' + esc(block["错误"]) + "</div>";
  return card("打法推荐（四档各一只）", html);
}


/* 一键抓取大盘快照（批注 4）：跑一次 pan.py post，落 data/pan/<今天>/；
 * 勾上「同时荐股」就顺带跑一轮全大盘荐股（批注 5，会花模型钱）。 */
export async function runPanJob() {
  const btn = $("#btn-pan-run");
  const st = $("#pan-run-state");
  if (btn) btn.disabled = true;
  if (st) st.textContent = "正在抓取 …";
  try {
    const withPick = !!($("#pan-with-pick") || {}).checked;
    const res = await api("/api/jobs", {
      method: "POST", body: JSON.stringify({kind: "pan", pick: withPick})});
    if (!res.ok) throw new Error(res.error || "启动失败");
    const _fresh = freshNote(res);
    if (_fresh) toast(_fresh, "warn");
    /* 勾了「同时荐股」这轮要 5-10 分钟（全市场扫描 + 一次模型调用）：轮询放宽到 30 分钟并报进度 */
    let guard = 0;
    while (guard++ < 2000) {
      const j = await api("/api/jobs/" + res.id + "?from=0");
      if (st && j.status === "running") {
        const last = (j.lines || [])[(j.lines || []).length - 1];
        st.textContent = (withPick ? "抓大盘 + 荐股中 " : "抓取中 ") + j.elapsed + "s" +
          (last ? " ｜ " + String(last.text || "").slice(0, 60) : "");
      }
      if (j.status !== "running") {
        if (btn) btn.disabled = false;
        if (j.status === "done") {
          const r = j.result || {};
          if (st) {
            st.textContent = (withPick ? "大盘快照 + 荐股完成 " : "已抓取 ") + "用时 " +
              j.elapsed + "s";
          }
          if (r["error"]) toast("大盘快照已更新，但荐股失败：" + r["error"], "warn");
          else toast(withPick ? "大盘快照已更新，四档推荐见「打法推荐」卡" : "大盘快照已更新", "ok");
          await loadMarket();
        } else {
          if (st) st.textContent = "抓取失败（退出码 " + j.exit_code + "）";
          toast("抓取失败：看控制台里的 [FAIL] 一行", "bad");
        }
        return;
      }
      await sleep(900);
    }
    if (btn) btn.disabled = false;
    if (st) st.textContent = "超时";
  } catch (e) {
    if (btn) btn.disabled = false;
    if (st) st.textContent = "失败：" + e.message;
    toast(e.message, "bad");
  }
}


export async function runMarketForecast() {
  const btn = $("#fc-run");
  const st = $("#fc-status");
  const con = $("#fc-console");
  if (!btn) return;
  btn.disabled = true;
  if (con) { con.hidden = false; con.dataset.clean = "1"; con.innerHTML = '<span class="l-dim">正在准备 …</span>'; }
  if (st) st.textContent = "运行中 …";
  try {
    const res = await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({ kind: "market", profile: ($("#fc-profile") || {}).value || "" }),
    });
    if (!res.ok) throw new Error(res.error || "启动失败");
    let from = 0, guard = 0;
    while (guard++ < 600) {
      const j = await api("/api/jobs/" + res.id + "?from=" + from);
      (j.lines || []).forEach(l => {
        const d = document.createElement("div");
        const t = (l.text || "").trim();
        let cls = "";
        if (t.indexOf("[OK]") === 0) cls = "l-ok";
        else if (t.indexOf("[WARN]") === 0) cls = "l-warn";
        else if (t.indexOf("[FAIL]") === 0) cls = "l-fail";
        else if (t.indexOf("[..]") === 0) cls = "l-stage";
        d.innerHTML = '<span class="' + cls + '">' + esc(l.text) + "</span>";
        con.appendChild(d);
        con.scrollTop = con.scrollHeight;
      });
      from = j.next;
      if (j.status !== "running") {
        btn.disabled = false;
        if (j.status === "done") {
          if (st) st.textContent = "完成，用时 " + j.elapsed + "s";
          toast("大盘预测已生成", "ok");
          loadMarket();
        } else {
          if (st) st.textContent = "失败（退出码 " + j.exit_code + "）";
          toast("大盘预测失败", "bad");
        }
        return;
      }
      await sleep(900);
    }
    btn.disabled = false;
    if (st) st.textContent = "超时";
  } catch (e) {
    btn.disabled = false;
    if (st) st.textContent = "失败：" + e.message;
    toast(e.message, "bad");
  }
}

export async function loadMarket() {
  const m = await api("/api/market");
  const doc = (m.pan || {}).doc;
  $("#market-src").textContent = doc
    ? "来源 " + (m.pan["路径"] || "") + " · phase=" + doc.phase + " · 交易日 " + (doc.trade_date || "—") +
      " · 生成 " + (doc.generated_at || "—")
    : "没有找到 pan 快照（先跑一次 pan.py）";
  const body = $("#market-body");
  if (!doc) {
    /* 过期快照会被自动清理（事实包不许用过期数据），所以「没有快照」是正常状态：
       这里给一张带操作指引的卡，而不是一片空白。 */
    body.innerHTML = '<div class="card"><div class="card-h"><span>没有行情快照</span>' +
      '<span class="muted">data/pan/ 下没有可用的最新快照</span></div>' +
      '<div class="card-b"><div class="row" style="gap:8px;align-items:center;flex-wrap:wrap">' +
      "右上角「抓大盘快照」按钮可以直接跑一次" +
      "（勾上「同时荐股」就顺带出一份四档推荐）。</div>" +
      '<div class="hint">过期的快照已按「事实包不许用过期数据」自动清理' +
      '（移入 data/ai/.trash/stale/，7 天后真删）。想恢复大盘背景与事实包里的量能数据，' +
      '先跑一次：<code>python main.py pan post</code>；个股消息面用 ' +
      '<code>python main.py stock3d pull &lt;代码&gt;</code>（或直接在「运行研判」里跑）。' +
      '</div></div></div>';
    return;
  }
  const dd = doc.data || {};
  const parts = [];
  const fcRes = await api("/api/market/forecast").catch(() => null);
  parts.push(marketForecastCard(fcRes));
  parts.push(stylePicksCard(m["荐股四档"]));

  const idx = (dd.indices_volume || {})["指数_同花顺"] || (dd.indices_volume || {})["指数_东财"] || [];
  if (idx.length) {
    parts.push(card("指数与量能",
      '<div class="stat-row">' + idx.map(i => {
        const p = num(i["涨跌幅_pct"]);
        return '<div class="stat"><div class="k">' + esc(i["名称"]) + '</div><div class="v ' + kindClass(p) + '">' +
          fmt(i["最新价"], 2) + '</div><div class="s ' + kindClass(p) + '">' + fmtPct(p) +
          ' · 成交额 ' + fmt((i["成交额_亿"]), 0) + "亿</div></div>";
      }).join("") + "</div>" +
      (function () {
        const v = (dd.indices_volume || {})["两市成交额"] || {};
        const keys = Object.keys(v);
        if (!keys.length) return "";
        return '<div class="kv-list" style="margin-top:6px">' + keys.map(k => kv(k, esc(fmt(v[k], 2)) + " 亿")).join("") + "</div>";
      })()
    ));
  }

  const sent = dd.sentiment || {};
  const bd = sent["广度"];
  if (bd) {
    const total = num(bd["上涨家数"]) + num(bd["下跌家数"]) + num(bd["平盘家数"]);
    const up = num(bd["上涨家数"]) || 0, down = num(bd["下跌家数"]) || 0, flat = num(bd["平盘家数"]) || 0;
    const bar = t => '<span class="bar-track"><span class="bar-fill ' + t;
    let html = '<div class="stat-row">' +
      '<div class="stat"><div class="k">上涨家数</div><div class="v up">' + up + '</div><div class="s">占比 ' + fmt(bd["上涨占比_pct"]) + '%</div></div>' +
      '<div class="stat"><div class="k">下跌家数</div><div class="v down">' + down + '</div><div class="s">平盘 ' + flat + '</div></div>' +
      '<div class="stat"><div class="k">涨停家数</div><div class="v up">' + (bd["涨停家数_阈值口径"] == null ? "—" : bd["涨停家数_阈值口径"]) + '</div><div class="s">跌停 ' + (bd["跌停家数_阈值口径"] == null ? "—" : bd["跌停家数_阈值口径"]) + "</div></div>" +
      '<div class="stat"><div class="k">涨幅超5%</div><div class="v">' + (bd["涨幅超5%家数"] == null ? "—" : bd["涨幅超5%家数"]) + '</div><div class="s">跌幅超5% ' + (bd["跌幅超5%家数"] == null ? "—" : bd["跌幅超5%家数"]) + "</div></div>" +
      "</div>";
    if (total) {
      html += '<div class="bar-row"><span class="muted">涨/跌分布</span><span class="bar-track" style="display:flex">' +
        '<span class="bar-fill up" style="width:' + (up / total * 100) + '%"></span>' +
        '<span class="bar-fill" style="width:' + (flat / total * 100) + '%;background:#5a6b80"></span>' +
        '<span class="bar-fill down" style="width:' + (down / total * 100) + '%"></span></span>' +
        '<span class="bar-val">' + bd["样本数"] + " 只</span></div>";
    }
    const lian = sent["连板梯队"];
    if (lian && lian["连板股总数"] != null) {
      html += '<div style="margin-top:10px">' + chip("连板股 " + lian["连板股总数"] + " 只") +
        chip("最高 " + lian["最高连板高度"] + " 连板", "accent") +
        Object.keys(lian["各梯队数量"] || {}).filter(k => lian["各梯队数量"][k]).map(k => chip(k + "：" + lian["各梯队数量"][k])).join("") +
        "</div>";
      if ((lian["连板股名单"] || []).length) {
        html += '<div class="table-wrap" style="margin-top:10px;max-height:240px"><table class="tbl"><thead><tr><th>代码</th><th>名称</th><th class="num">连板</th><th>梯队</th></tr></thead><tbody>' +
          lian["连板股名单"].map(s => "<tr><td>" + esc(s["代码"]) + "</td><td>" + esc(s["名称"]) +
            '</td><td class="num">' + fmt(s["连板数"], 0) + "</td><td>" + esc(s["梯队"] || "") + "</td></tr>").join("") +
          "</tbody></table></div>";
      }
    }
    parts.push(card("情绪与广度", html));
  }

  const funds = dd.funds || {};
  let fhtml = '<div class="stat-row">' +
    '<div class="stat"><div class="k">两市主力净流入</div><div class="v ' + kindClass(funds["主力资金_两市净流入_亿"]) + '">' +
    fmt(funds["主力资金_两市净流入_亿"], 1) + ' 亿</div></div>' +
    (funds["南向资金"] ? '<div class="stat"><div class="k">南向净买入</div><div class="v ' + kindClass(funds["南向资金"]["南向合计_净买入_亿"]) + '">' +
      fmt(funds["南向资金"]["南向合计_净买入_亿"], 2) + ' 亿</div><div class="s">' + esc(funds["南向资金"]["日期"] || "") + "</div></div>" : "") +
    (funds["两融"] ? '<div class="stat"><div class="k">融资余额</div><div class="v">' + fmt(funds["两融"]["融资余额_亿"], 0) +
      ' 亿</div><div class="s">' + esc(funds["两融"]["日期"] || "") + "</div></div>" : "") +
    (funds["北向资金"] ? '<div class="stat"><div class="k">北向资金</div><div class="v muted">未披露</div><div class="s">' +
      esc((funds["北向资金"]["status"] || "")) + "</div></div>" : "") +
    "</div>";
  const ind = (funds["行业主力资金"] || {});
  if ((ind["净流入TOP10"] || []).length || (ind["净流出TOP10"] || []).length) {
    fhtml += '<div class="two-col">' + flowTable("主力净流入 TOP", ind["净流入TOP10"], "up") +
      flowTable("主力净流出 TOP", ind["净流出TOP10"], "down") + "</div>";
  }
  if (fhtml) parts.push(card("资金", fhtml));

  const sec = dd.sectors || {};
  const secTables = [];
  ["行业板块", "概念板块"].forEach(k => {
    const g = sec[k] || {};
    if (!(g["领涨"] || []).length && !(g["领跌"] || []).length) return;
    secTables.push('<div class="sec-title">' + k + ' · 领涨</div>' + sectorTable(g["领涨"]) +
      '<div class="sec-title" style="margin-top:12px">' + k + ' · 领跌</div>' + sectorTable(g["领跌"]));
  });
  if (secTables.length) parts.push(card("板块轮动", secTables.join("")));

  const pop = (dd.sentiment || {})["个股人气榜"] || {};
  const popKeys = ["成交额TOP", "主力净流入TOP", "换手率TOP"];
  const popTables = popKeys.filter(k => (pop[k] || []).length).map(k => {
    const rows = pop[k].slice(0, 10);
    const cols = Object.keys(rows[0]);
    return '<div class="sec-title">' + k + '</div><div class="table-wrap" style="max-height:300px"><table class="tbl"><thead><tr>' +
      cols.map(c => "<th>" + esc(c) + "</th>").join("") + "</tr></thead><tbody>" +
      rows.map(r => "<tr>" + cols.map(c => {
        const v = r[c];
        const isNum = typeof v === "number";
        const cls = /涨跌|净流入/.test(c) ? kindClass(v) : "";
        return '<td class="' + (isNum ? "num " : "") + cls + '">' + esc(isNum ? fmt(v, 2) : (v == null ? "—" : v)) + "</td>";
      }).join("") + "</tr>").join("") + "</tbody></table></div>";
  });
  if (popTables.length) parts.push(card("个股榜", popTables.join('<div style="height:12px"></div>')));

  const src = doc.sources || {};
  const sx = Object.keys(src);
  if (sx.length) {
    let html = '<div class="table-wrap" style="max-height:320px"><table class="tbl"><thead><tr><th>来源</th><th>状态</th><th>说明</th></tr></thead><tbody>';
    sx.forEach(k => {
      const v = src[k];
      const st = (v && (v.status || v.状态)) || v;
      html += "<tr><td>" + esc(k) + "</td><td>" +
        chip(String(st || "—"), /OK|ok/.test(String(st)) ? "ok" : "bad") +
        '</td><td class="wrap muted">' + esc((v && (v.说明 || v.note || v.error)) || "") + "</td></tr>";
    });
    parts.push(card("数据源状态", html + "</tbody></table></div>"));
  }

  const deg = doc.degrade;
  const degList = Array.isArray(deg) ? deg : (deg && typeof deg === "object" ? Object.keys(deg).map(k => k + "：" + deg[k]) : []);
  if (degList.length) {
    parts.push(card("降级 / 失败清单", degList.map(d => '<div class="muted">· ' + esc(typeof d === "string" ? d : JSON.stringify(d)) + "</div>").join("")));
  }

  body.innerHTML = parts.join("") || EMPTY;
  const fcBtn = $("#fc-run");
  if (fcBtn) fcBtn.addEventListener("click", runMarketForecast);
}

export function flowTable(title, rows, cls) {
  if (!Array.isArray(rows) || !rows.length) return "";
  let html = '<div class="mini-card"><div class="sec-title">' + esc(title) + "</div>" +
    '<div class="table-wrap" style="max-height:300px"><table class="tbl"><thead><tr><th>板块</th><th class="num">净流入(亿)</th><th class="num">涨跌%</th></tr></thead><tbody>';
  rows.slice(0, 10).forEach(r => {
    html += "<tr><td>" + esc(r["名称"]) + '</td><td class="num ' + cls + '">' + fmt(r["主力净流入_亿"], 2) +
      '</td><td class="num ' + kindClass(r["涨跌幅_pct"]) + '">' + fmtPct(r["涨跌幅_pct"]) + "</td></tr>";
  });
  return html + "</tbody></table></div></div>";
}

export function sectorTable(rows) {
  if (!Array.isArray(rows) || !rows.length) return '<div class="empty">无</div>';
  const cols = Object.keys(rows[0]);
  let html = '<div class="table-wrap" style="max-height:300px"><table class="tbl"><thead><tr>' +
    cols.map(c => "<th>" + esc(c) + "</th>").join("") + "</tr></thead><tbody>";
  rows.slice(0, 10).forEach(r => {
    html += "<tr>" + cols.map(c => {
      const v = r[c];
      const isNum = typeof v === "number";
      const cls = /涨跌|净流入/.test(c) ? kindClass(v) : "";
      return '<td class="' + (isNum ? "num " : "") + cls + '">' + esc(isNum ? fmt(v, 2) : (v == null ? "—" : v)) + "</td>";
    }).join("") + "</tr>";
  });
  return html + "</tbody></table></div>";
}

/* 注册给 core/app.js：切到本页时按需加载 / 对外暴露的动作。 */
/* 「抓大盘快照」（+ 可选同时荐股）按钮在工具栏里：只绑一次，别在每次渲染时重复绑 */
function bindPanButton() {
  const btn = $("#btn-pan-run");
  if (!btn || btn.dataset.bound === "1") return;
  btn.dataset.bound = "1";
  btn.addEventListener("click", runPanJob);
}


registerView("market", { onShow: () => { bindPanButton(); return loadMarket(); } });
