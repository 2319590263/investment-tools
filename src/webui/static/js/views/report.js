/* 报告页：结构化面板、价位、情景、K 线与完整原文。 */
import { api } from "../core/api.js";
import { State, openReport, registerView, showView, viewApi } from "../core/app.js";
import { $, $$, badge, chip, dirColor, esc, fmt, fmtMoney, fmtPct, kindClass, num, sleep, toast } from "../core/util.js";
import { card, kv, listOrEmpty, pill, renderScenarioSummary } from "../ui/cards.js";
import { loadStructKline, sceneCards } from "../ui/kline.js";
import { mdToHtml, withToc } from "../ui/markdown.js";
import { basisFor, planTriggerPrice } from "../ui/planprices.js";

export async function refreshReports() {
  const res = await api("/api/reports");
  State.reports = res.items || [];
  renderReportPicker();
}

export function renderReportPicker() {
  const sel = $("#report-pick");
  const cur = sel.value;
  const items = State.reports || [];
  let html = "";
  /* 每个标的只保留最新一份（服务端 prune_reports 维护），所以优先按标的列。 */
  const latest = {}, order = [];
  items.forEach(r => {
    const code = ((r["摘要"] || {})["标的代码"]) || "（未知标的）";
    if (!latest[code]) { latest[code] = r; order.push(code); }
  });
  if (order.length) {
    html += '<optgroup label="按标的（每个标的只留最新一份）">';
    order.forEach(code => {
      const r = latest[code], s = r["摘要"] || {};
      html += '<option value="' + esc(r["json路径"]) + '">' +
        esc(s["标的名称"] || code) + " · " + esc(code) +
        " · " + esc(r["phase标签"] || "") +
        (s["方向"] ? " · " + esc(s["方向"]) : "") +
        (s["交易日"] ? " · " + esc(s["交易日"]) : "") +
        (r["过期"] ? "（已过期）" : "") + "</option>";
    });
    html += "</optgroup>";
  }
  const older = items.filter(r =>
    latest[((r["摘要"] || {})["标的代码"]) || "（未知标的）"] !== r);
  const byDate = {};
  older.forEach(r => { (byDate[r["日期"]] = byDate[r["日期"]] || []).push(r); });
  Object.keys(byDate).sort().reverse().forEach(d => {
    html += '<optgroup label="更早（待清理）' + esc(d) + '">';
    byDate[d].forEach(r => {
      const s = r["摘要"] || {};
      html += '<option value="' + esc(r["json路径"]) + '">' +
        esc(r["时间戳"].slice(0, 2) + ":" + r["时间戳"].slice(2, 4) + ":" + r["时间戳"].slice(4)) + " · " +
        esc(r["phase标签"]) + " · " + esc(s["标的名称"] || s["标的代码"] || "") + "</option>";
    });
    html += "</optgroup>";
  });
  if (!items.length) html = '<option value="">（还没有报告）</option>';
  sel.innerHTML = html;
  if (cur && items.some(r => r["json路径"] === cur)) sel.value = cur;
}

/* 过期提醒（批注 3）：每个标的只保留最新一份报告，过期就在顶部提醒重新生成。 */
export function renderStaleBar(b) {
  const bar = $("#report-stale");
  if (!bar) return;
  const hit = (State.reports || []).find(r => r["json路径"] === b["json路径"]);
  if (!hit || !hit["过期"]) {
    bar.hidden = true;
    bar.textContent = "";
    return;
  }
  const code = ((b["摘要"] || {})["标的代码"]) || "";
  bar.hidden = false;
  bar.innerHTML = "<b>这份报告已过期</b>：" + esc(hit["过期说明"] || "") +
    "。过时的报告会自动移入回收站（回收站保留 7 天），请重新研判这只标的。" +
    (code ? '<button class="btn sm primary" id="btn-report-regen">重新研判 ' + esc(code) +
      "</button>" : "");
  const btn = $("#btn-report-regen");
  if (btn) btn.addEventListener("click", () => {
    const run = viewApi("run");
    if (run.pickCode) run.pickCode(code);
    showView("run");
  });
}


export async function loadLatestReport(phase) {
  try {
    const q = phase ? "/api/report?phase=" + phase : "/api/report";
    const b = await api(q);
    State.report = b;
    renderReport();
    if ($("#report-pick")) $("#report-pick").value = b["json路径"];
  } catch (e) {
    $("#report-struct").innerHTML = '<div class="empty">还没有报告：先在「运行研判」里跑一次，或用 --dry-run 生成事实包。</div>';
  }
}

export function renderReport() {
  const b = State.report;
  if (!b) return;
  renderStaleBar(b);
  $("#report-md").innerHTML = withToc(mdToHtml(b.md || ""));
  $("#json-pre").textContent = JSON.stringify(b.json, null, 1);
  $("#report-struct").innerHTML = renderStruct(b);
  bindStruct();
  loadStructKline(b);
  loadPlanCheck(false);
  const s = b["摘要"] || {};
  $("#btn-download-md").disabled = !b.md;
}

export function renderStruct(b) {
  const j = b.json || {};
  const s = b["摘要"] || {};
  const plan = (j["研判"] || {}).json || {};
  const review = (j["复核"] || {}).json || {};
  const checks = j["机械校验"] || [];
  const pos = plan["仓位"] || {};
  const prices = plan["关键价位"] || {};
  const book = priceBook(plan);
  const slots = {};
  /* put(key, html)：各卡片先入槽位，最后按新顺序拼装（顺序调整只改最后一行） */
  const put = (key, html) => { if (html) slots[key] = html; };

  /* --- 头部 --- */
  const dir = s["方向"];
  put("hero", '<div class="rep-head">' +
    "<h1>" + esc(s["标的名称"] || s["标的代码"] || "报告") + " " +
    '<span class="mono muted">' + esc(s["标的代码"] || "") + "</span></h1>" +
    '<div class="rep-sub">' +
    chip((j.phase || "?") + " · " + (j.trade_date || ""), "accent") +
    chip("交易日 " + (j.trade_date || "—")) +
    chip("生成 " + (j.generated_at || "—")) +
    chip("profile " + ((j.profile || {})["名称"] || "—"), "flat") +
    chip("研判 " + (s["研判模型"] || "—"), s["研判ok"] ? "" : "bad") +
    (s["复核模型"] ? chip("复核 " + s["复核模型"], s["复核ok"] ? "" : "warn") : chip("复核 已跳过", "flat")) +
    (j["账户余额"] ? chip("余额 " + fmtMoney((j["账户余额"] || {})["余额"]) + " " + ((j["账户余额"] || {})["币种"] || "")) : "") +
    "</div>" +
    '<div class="rep-hero">' +
    '<div class="hero-main">' +
    '<div class="hero-quote">' + esc(plan["一句话结论"] || "（模型未给出结论）") + "</div>" +
    '<div class="gauge-row">' +
    '<div class="gauge"><span class="k">方向</span><span class="v ' + dirColor(dir) + '">' + esc(dir || "—") + "</span></div>" +
    '<div class="gauge"><span class="k">置信度</span><span class="v">' + (s["置信度"] == null ? "—" : s["置信度"]) + "</span></div>" +
    '<div class="gauge"><span class="k">时间窗</span><span class="v" style="font-size:14px">' + esc(s["时间窗"] || "—") + "</span></div>" +
    '<div class="gauge"><span class="k">现价</span><span class="v">' + fmt(s["现价"]) + "</span></div>" +
    '<div class="gauge"><span class="k">本次费用</span><span class="v" style="font-size:14px">' +
    (s["费用_元"] == null ? "未配置单价" : "¥" + fmt(s["费用_元"], 3)) + "</span></div>" +
    "</div></div>" +
    '<div class="hero-main" style="flex:0 0 380px;min-width:320px">' + renderPositionBars(pos) + "</div>" +
    "</div></div>");

  /* --- 实盘复核（报告计划 × 当前实盘价） --- */
  if (s["标的代码"]) {
    put("plancheck", '<div class="card" id="plan-card"><div class="card-h">' +
      "<span>实盘复核</span>" +
      '<span class="muted" id="plan-head">按这份报告的交易计划核对当前实盘价</span>' +
      '<div class="spacer"></div>' +
      '<button class="btn ghost" id="btn-plan-refresh">刷新实盘价</button></div>' +
      '<div class="card-b" id="plan-body"><div class="muted">正在取实盘价 …</div></div></div>');
  }

  /* --- 情景树 + 综合评价 --- */
  const scenes = plan["情景树"];
  if (Array.isArray(scenes) && scenes.length) {
    put("scene", card("情景树与综合评价", renderScenarioSummary(plan) + sceneCards(scenes)));
  }

  /* --- 关键价位 --- */
  const lv = [];
  (prices["支撑"] || []).forEach(x => lv.push(pill(x["价位"], x["依据"], "up")));
  if (prices["止损价"] != null) lv.push(pill(prices["止损价"], "止损价", "stop"));
  (prices["压力"] || []).forEach(x => lv.push(pill(x["价位"], x["依据"], "down")));
  (prices["目标位"] || []).forEach(x => lv.push(pill(x["价位"], x["依据"], "target")));
  if (lv.length) put("levels", card("关键价位", '<div>' + lv.join("") + "</div>"));

  /* --- K 线与关键价位 --- */
  if (s["标的代码"]) {
    put("kline", '<div class="card" id="kline-card"><div class="card-h"><span>K 线与关键价位</span>' +
      '<span class="muted">' + esc(s["标的名称"] || s["标的代码"]) + ' · 日K（前复权）</span>' +
      '<div class="spacer"></div>' +
      '<div class="kline-zoom">' +
      '<button class="btn sm" id="k-zoom-out" title="缩小：显示更多K线">－</button>' +
      '<span class="muted" id="k-zoom-label">—</span>' +
      '<button class="btn sm" id="k-zoom-in" title="放大：少画几根并加高画布，看得更清">＋</button>' +
      '<button class="btn sm ghost" id="k-zoom-reset">复位</button>' +
      '</div>' +
      '<span class="muted" id="kline-src"></span></div>' +
      '<div class="card-b"><div class="kline-loading" id="kline-loading">正在加载日K缓存 …</div>' +
      '<canvas class="kline-canvas" id="kline-canvas" style="display:none"></canvas>' +
      '<div class="kline-legend" id="kline-legend" style="display:none"></div></div></div>');
  }

  /* --- 计划 --- */
  const allEntries = Array.isArray(plan["计划"]) ? plan["计划"] : [];
  const entries = allEntries.filter(e => !planIsNoop(e["动作"]));
  const noopCount = allEntries.length - entries.length;
  if (entries.length) {
    let html = '<div class="table-wrap" style="max-height:none"><table class="tbl"><thead><tr>' +
      "<th>优先级</th><th>动作</th><th>触发条件</th><th>触发价（含依据）</th><th class='num'>股数</th>" +
      "<th class='num'>金额(元)</th><th>失效条件</th></tr></thead><tbody>";
    entries.slice().sort((a, c) => (num(a["优先级"]) || 9) - (num(c["优先级"]) || 9)).forEach(e => {
      const act = String(e["动作"] || "");
      const isSell = /减|清|卖|止盈|止损/.test(act);
      const isBuy = /买|加|建|补|定投/.test(act);
      const cls = isSell ? "down" : (isBuy ? "up" : "");
      const rng = renderPlanPrices(e["价格区间"], book, e["动作"], s["现价"]);
      html += "<tr>" + "<td>" + (e["优先级"] == null ? "—" : e["优先级"]) + "</td>" +
        '<td class="' + cls + '"><b>' + esc(act || "—") + "</b>" + (e["作废"] ? ' <span class="badge bad">作废</span>' : "") + "</td>" +
        '<td class="wrap">' + esc(e["触发条件"] || "—") + "</td>" +
        '<td class="mono">' + rng + "</td>" +
        '<td class="num">' + (e["股数"] == null ? "—" : fmt(e["股数"], 0)) + "</td>" +
        '<td class="num">' + (e["金额_元"] == null ? "—" : fmtMoney(e["金额_元"], 0)) + "</td>" +
        '<td class="wrap muted">' + esc(e["失效条件"] || "—") + "</td></tr>";
    });
    html += "</tbody></table></div>";
    if (noopCount) {
      html += '<div class="muted" style="margin-top:8px">· 已省略 ' + noopCount +
        " 条观望/持有类条目（没有执行动作；加入自选或持仓本身就等于在观望）</div>";
    }
    const corr = plan["计划校正"] || {};
    if ((corr["作废条目"] || []).length || (corr["提示"] || []).length) {
      html += '<div style="margin-top:10px" class="mini-card"><div class="sec-title">脚本计划校正</div>' +
        (corr["作废条目"] || []).map(x => '<div class="fail">作废：' + esc(typeof x === "string" ? x : JSON.stringify(x)) + "</div>").join("") +
        (corr["提示"] || []).map(x => '<div class="muted">提示：' + esc(typeof x === "string" ? x : JSON.stringify(x)) + "</div>").join("") +
        "</div>";
    }
    put("plan", card("交易计划", html));
  } else if (noopCount) {
    put("plan", card("交易计划",
      '<div class="muted">这份报告只有观望/持有类条目（已省略），没有需要执行的动作——' +
      "加入自选或持仓本身就等于在观望。</div>"));
  }

  /* --- 风险 --- */
  const risks = plan["风险"];
  if (Array.isArray(risks) && risks.length) {
    let html = '<div class="table-wrap" style="max-height:none"><table class="tbl"><thead><tr><th>风险</th><th>监控指标</th><th>应对</th></tr></thead><tbody>';
    risks.forEach(r => {
      html += '<tr><td class="wrap"><b>' + esc(r["风险"] || "") + "</b></td><td class=\"wrap muted\">" +
        esc(r["监控指标"] || "—") + '</td><td class="wrap">' + esc(r["应对"] || "—") + "</td></tr>";
    });
    put("risk", card("风险与应对", html + "</tbody></table></div>"));
  }

  /* --- 机械校验 --- */
  if (checks.length) {
    const cnt = { "通过": 0, "违规": 0, "提示": 0 };
    checks.forEach(c => { if (cnt[c["结果"]] !== undefined) cnt[c["结果"]]++; });
    const cls = { "通过": "ok", "违规": "bad", "提示": "warn" };
    const dot = { "通过": "ok", "违规": "bad", "提示": "warn" };
    let html = '<div class="check-sum">' +
      chip("通过 " + cnt["通过"], "ok") + chip("违规 " + cnt["违规"], cnt["违规"] ? "bad" : "") +
      chip("提示 " + cnt["提示"], cnt["提示"] ? "warn" : "") +
      '<span class="muted">规则由脚本确定性判定，不交给模型</span></div>';
    checks.forEach(c => {
      html += '<div class="check-item"><span class="dot ' + (dot[c["结果"]] || "") + '"></span>' +
        "<span>" + badge(c["结果"] || "?", cls[c["结果"]] || "flat") + " " + esc(c["项"] || "") + "</span>" +
        '<span class="note">' + esc(c["说明"] || "") + "</span></div>";
    });
    put("check", card("机械风控校验", html));
  }

  /* --- 复核 --- */
  const rv = j["复核"] || {};
  if (rv.called) {
    let html = "";
    if (!rv.ok || !review || !Object.keys(review).length) {
      html = '<div class="fail">复核档未产出可解析结果：' + esc(rv.error || rv.parse_error || "未知") + "</div>";
      if (rv.raw_text) html += '<details class="adv"><summary>模型原文</summary><pre class="console">' + esc(rv.raw_text) + "</pre></details>";
    } else {
      const overturn = review["是否推翻结论"];
      html += '<div class="check-sum">' +
        chip("是否推翻结论：" + (overturn === true ? "是" : overturn === false ? "否" : "—"), overturn ? "bad" : "ok") +
        chip("质询 " + ((review["质询"] || []).length) + " 条") +
        chip("模型 " + (rv.model || "—"), "flat") + "</div>";
      const qs = review["质询"];
      if (Array.isArray(qs) && qs.length) {
        html += '<div class="table-wrap" style="max-height:none"><table class="tbl"><thead><tr><th>严重度</th><th>质询点</th><th>理由</th><th>建议</th></tr></thead><tbody>';
        qs.forEach(q => {
          html += "<tr><td>" + badge(q["严重度"] || "—", q["严重度"] || "flat") + '</td><td class="wrap"><b>' +
            esc(q["点"] || "") + '</b></td><td class="wrap muted">' + esc(q["理由"] || "") +
            '</td><td class="wrap">' + esc(q["建议"] || "") + "</td></tr>";
        });
        html += "</tbody></table></div>";
      }
      const fixes = review["修正要点"], miss = review["遗漏的关键数据"];
      if ((fixes || []).length || (miss || []).length) {
        html += '<div class="two-col" style="margin-top:12px">';
        html += '<div class="mini-card"><div class="sec-title">修正要点</div>' + listOrEmpty(fixes) + "</div>";
        html += '<div class="mini-card"><div class="sec-title">遗漏的关键数据</div>' + listOrEmpty(miss) + "</div></div>";
      }
    }
    put("review", card("复核档质询", html));
  } else if (rv.error) {
    put("review", card("复核档质询", '<div class="muted">' + esc(rv.error) + "</div>"));
  }

  /* --- 上次计划复盘 + 数据依赖 --- */
  const cols = [];
  const prevr = j["上次计划复盘"];
  if (prevr) cols.push('<div class="mini-card"><div class="sec-title">上次计划机械回检</div>' + renderPrevReview(prevr) + "</div>");
  const dep = plan["数据依赖"];
  if (dep) {
    cols.push('<div class="mini-card"><div class="sec-title">数据依赖与不确定性</div>' +
      "<div class='sec-title' style='margin:0;color:var(--muted)'>降级项</div>" + listOrEmpty(dep["降级项"]) +
      (dep["缺失导致的不确定性"] ? '<div class="sec-title" style="margin:10px 0 0;color:var(--muted)">不确定性</div><div class="path">' +
        esc(dep["缺失导致的不确定性"]) + "</div>" : "") + "</div>");
  }
  if (cols.length) put("prev", card("复盘与数据依赖", '<div class="two-col">' + cols.join("") + "</div>"));

  /* --- 运行元信息 --- */
  const meta = [];
  const dd = j["数据"] || {};
  meta.push(["pan 文件", dd.pan_path, dd.pan_sha1]);
  meta.push(["stock3d 文件", dd.stock3d_path, dd.stock3d_sha1]);
  meta.push(["事实包", (j["事实包"] || {})["字符数"] + " 字符", (j["事实包"] || {})["sha1"]]);
  let mhtml = '<div class="table-wrap" style="max-height:none"><table class="tbl"><thead><tr><th>项</th><th>路径 / 值</th><th>sha1</th></tr></thead><tbody>';
  meta.forEach(m => {
    mhtml += "<tr><td>" + esc(m[0]) + '</td><td class="wrap mono muted">' + esc(m[1] || "—") +
      '</td><td class="mono muted">' + esc((m[2] || "—").slice(0, 16)) + "</td></tr>";
  });
  mhtml += "</tbody></table></div>";
  const secs = (j["事实包"] || {})["章节"] || [];
  if (secs.length) {
    mhtml += '<div style="margin-top:12px" class="cards-3">' + secs.map(x =>
      '<div class="mini-card"><div class="k muted">' + esc(x["标题"]) + '</div><div class="mono">' +
      esc(x["字符数"]) + " 字符</div></div>").join("") + "</div>";
  }
  const trimmed = dd["裁剪记录"] || [];
  if (trimmed.length) {
    mhtml += '<div class="sec-title" style="margin-top:14px">预算裁剪记录</div>' +
      trimmed.map(t => '<div class="muted">· ' + esc(typeof t === "string" ? t : JSON.stringify(t)) + "</div>").join("");
  }
  mhtml += '<div class="sec-title" style="margin-top:14px">token 与费用</div><div class="kv-list">' +
    kv("研判 usage", "in " + fmtInt((j["研判"] || {}).usage, "输入") + " / out " + fmtInt((j["研判"] || {}).usage, "输出") +
      " / 缓存 " + fmtInt((j["研判"] || {}).usage, "缓存读取")) +
    kv("复核 usage", "in " + fmtInt((j["复核"] || {}).usage, "输入") + " / out " + fmtInt((j["复核"] || {}).usage, "输出")) +
    kv("费用", ((j["研判"] || {}).cost || {})["人民币_估算"] == null
      ? esc((((j["研判"] || {}).cost || {})["说明"]) || "—")
      : "研判 ¥" + fmt(((j["研判"] || {}).cost || {})["人民币_估算"], 4) +
        " ｜ 复核 ¥" + fmt((((j["复核"] || {}).cost || {})["人民币_估算"]), 4)) +
    "</div>";
  put("meta", card("运行元信息", mhtml));

  /* 卡片顺序：K 线与关键价位、交易计划、关键价位明细 置顶，其余按原优先级排在后面 */
  return [slots.hero, slots.kline, slots.plan, slots.levels, slots.plancheck, slots.scene,
          slots.risk, slots.check, slots.review, slots.prev, slots.meta]
    .filter(Boolean).join("");
}

export function fmtInt(obj, key) { return obj && obj[key] != null ? obj[key] : "—"; }

export function priceBook(plan) {
  const p = (plan || {})["关键价位"] || {};
  const out = [];
  const push = (arr, type) => (Array.isArray(arr) ? arr : []).forEach(x => {
    const v = num(x && x["价位"]);
    if (v !== null) out.push({ v: v, note: x["依据"] || "", type: type });
  });
  push(p["支撑"], "支撑");
  push(p["压力"], "压力");
  push(p["目标位"], "目标");
  const stop = num(p["止损价"]);
  if (stop !== null) out.push({ v: stop, note: "破位即止损的确认位", type: "止损" });
  return out;
}


/* 计划只给「一个」可执行价位。判断依据是这个区间落在现价的哪一侧：
   整段在现价上方 → 上破启动，取区间下沿；整段在现价下方 → 跌破启动，取区间上沿；
   区间含现价 → 取下沿（通常是止损/失效线）。拿不到现价时退回动作类型判断。 */


/* 一个价位 + 这个价位的依据；原计划区间放进 hover 提示，不占版面 */


/* 情景树综合评价：脚本按情景概率机械汇总，不替模型加戏 */
export function renderPlanPrices(rng, book, act, price) {
  const v = planTriggerPrice(rng, act, price);
  if (v === null) return '<span class="muted">—</span>';
  const b = basisFor(v, book);
  const tip = (Array.isArray(rng) && rng.length > 1)
    ? ' title="原计划价格区间 ' + rng.map(x => fmt(x)).join(" ~ ") + '"' : "";
  return '<div class="price-line"' + tip + "><b>" + fmt(v) + "</b>" +
    (b ? "<small>" + esc(b.type + "：" + (b.note || "—")) + "</small>" : "") + "</div>";
}

export function renderPositionBars(pos) {
  const rows = [
    ["当前仓位", pos["当前_pct"], "up"],
    ["建议仓位", pos["建议_pct"], ""],
    ["单票上限", pos["上限_pct"], "warn"],
    ["现金保留", pos["现金保留_pct"], "down"],
  ];
  return '<div class="sec-title">仓位</div><div class="bars">' + rows.map(r => {
    const v = num(r[1]);
    const w = v == null ? 0 : Math.max(0, Math.min(100, v));
    return '<div class="bar-row"><span class="muted">' + r[0] + '</span>' +
      '<div class="bar-track"><div class="bar-fill ' + r[2] + '" style="width:' + w + '%"></div></div>' +
      '<span class="bar-val">' + (v == null ? "—" : v + "%") + "</span></div>";
  }).join("") + "</div>";
}

export function renderPrevReview(prev) {
  if (!prev || prev["是否有上次计划"] === false) {
    return '<div class="empty">' + esc((prev || {})["说明"] || "首份报告，无上次计划。") + "</div>";
  }
  let html = "";
  if (prev["说明"]) html += '<div class="muted">' + esc(prev["说明"]) + "</div>";
  const dir = prev["方向命中"];
  if (dir) html += '<div style="margin:6px 0">' + chip("方向命中：" + (dir["结论"] || dir["命中"] || JSON.stringify(dir))) + "</div>";
  const plans = prev["计划回检"] || prev["计划"] || [];
  if (Array.isArray(plans) && plans.length) {
    html += '<div class="table-wrap" style="max-height:none"><table class="tbl"><thead><tr><th>动作</th><th>价格区间</th><th>结果</th></tr></thead><tbody>';
    plans.forEach(p => {
      html += "<tr><td>" + esc(p["动作"] || "—") + '</td><td class="mono">' +
        esc(Array.isArray(p["价格区间"]) ? p["价格区间"].join(" ~ ") : (p["价格区间"] || "—")) +
        '</td><td class="wrap">' + esc(p["结论"] || p["结果"] || p["备注"] || "—") + "</td></tr>";
    });
    html += "</tbody></table></div>";
  }
  return html || '<pre class="console" style="max-height:220px">' + esc(JSON.stringify(prev, null, 1)) + "</pre>";
}

export function bindStruct() {
  $$("#report-struct [data-jump]").forEach(el => el.addEventListener("click", () => openReport(el.dataset.jump)));
  bindPlanCheck();
}

export function initReportView() {
  $("#report-pick").addEventListener("change", e => { if (e.target.value) openReport(e.target.value); });
  $("#btn-report-reload").addEventListener("click", async () => {
    await refreshReports(); await loadLatestReport(); toast("已刷新", "ok");
  });
  $$("#report-tabs button").forEach(b => b.addEventListener("click", () => {
    State.reportTab = b.dataset.tab;
    $$("#report-tabs button").forEach(x => x.classList.toggle("on", x === b));
    $("#report-struct").hidden = b.dataset.tab !== "struct";
    $("#report-md").hidden = b.dataset.tab !== "md";
    $("#report-json").hidden = b.dataset.tab !== "json";
  }));
  $("#btn-download-md").addEventListener("click", () => {
    if (!State.report || !State.report.md) { toast("该报告没有 Markdown", "warn"); return; }
    const blob = new Blob([State.report.md], { type: "text/markdown;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = (State.report["md路径"] || "report.md").split("/").pop();
    a.click(); URL.revokeObjectURL(a.href);
  });
}

/* =========================================================================
   实盘复核：报告交易计划 × 当前实盘价（机械判定 + 当前时段策略的模型点评）
   ========================================================================= */

const PlanCheck = { data: null, jobId: null };

function planStatusCls(s) {
  return { "已触发": "ok", "未触发": "warn", "已失效": "bad", "已作废": "flat", "无法判定": "flat" }[s] || "flat";
}

/* 与后端 plancheck.plan_kind 同一口径：没有买卖字样的动作（观望/持有/等待）视为无执行动作。
   这类条目不再占计划表，只在表下留一行「已省略 N 条」。 */
function planIsNoop(act) {
  return !/建仓|买入|加仓|补仓|开仓|吸纳|减仓|清仓|卖出|止盈|止损|减|卖|清/.test(String(act || ""));
}

function planItemsHtml(items) {
  if (!items.length) {
    return '<div class="muted" style="margin-top:10px">这份报告没有需要执行的动作（观望/持有类条目已省略）。</div>';
  }
  let html = '<div class="table-wrap" style="max-height:none;margin-top:12px"><table class="tbl"><thead><tr>' +
    "<th>优先级</th><th>动作</th><th>计划区间</th><th>状态</th><th>距触发</th><th>股数</th><th>说明</th>" +
    "</tr></thead><tbody>";
  items.forEach(it => {
    const dist = num(it["距离_pct"]);
    const shares = num(it["股数"]);
    html += "<tr><td>" + esc(it["优先级"] == null ? "—" : it["优先级"]) + "</td>" +
      '<td class="wrap"><b>' + esc(it["动作"]) + "</b>" +
      (it["类别"] && it["类别"] !== "观察" ? ' <small class="muted">' + esc(it["类别"]) + "</small>" : "") + "</td>" +
      '<td class="mono">' + esc(it["区间文本"]) + "</td>" +
      "<td>" + badge(it["状态"], planStatusCls(it["状态"])) + "</td>" +
      '<td class="mono">' + (dist === null ? "—" : fmt(dist) + "%") + "</td>" +
      '<td class="mono">' + (shares === null ? "—" : fmt(shares, 0)) + "</td>" +
      '<td class="wrap muted" title="' + esc(it["失效条件"] || "") + '">' + esc(it["说明"] || "—") + "</td></tr>";
  });
  return html + "</tbody></table></div>";
}

function planLevelsHtml(lv) {
  const bits = [];
  const rows = [["止损", lv["止损"]], ["支撑", lv["支撑"]], ["压力", lv["压力"]]];
  rows.forEach(([name, x]) => {
    if (!x) return;
    const dist = num(x["距离_pct"]);
    const cls = x["状态"] === "已破位" || x["状态"] === "已跌破" ? " down" : "";
    bits.push('<div class="price-line"><b class="' + (cls ? "down" : "") + '">' + fmt(x["价位"]) + "</b><small>" +
      esc(name + "：" + (x["状态"] || "—")) + (dist === null ? "" : "（距现价 " + fmt(dist) + "%）") +
      (x["依据"] ? " · " + esc(x["依据"]) : "") + "</small></div>");
  });
  (lv["目标"] || []).forEach(t => bits.push('<div class="price-line"><b>' + fmt(t["价位"]) + "</b><small>" +
    esc("目标：" + (t["已达标"] ? "已达标" : "未达标")) + (t["依据"] ? " · " + esc(t["依据"]) : "") + "</small></div>"));
  return bits.length ? '<div class="sec-title" style="margin-top:12px">关键价位</div><div>' + bits.join("") + "</div>" : "";
}

function planReviewBlock(d) {
  const doc = d["点评"];
  const j = ((doc || {})["点评"]) || {};
  const has = !!(doc && Object.keys(j).length);
  const profiles = (((State.state || {})["模型"]) || {}).profiles || [];
  const def = (((State.state || {})["模型"]) || {})["默认_profile"] || "";
  const picker = '<select id="plan-profile" class="inline-select">' + profiles.map(p =>
    '<option value="' + esc(p["名称"]) + '"' + (p["名称"] === def ? " selected" : "") + ">" +
    esc(p["名称"]) + "</option>").join("") + "</select>";
  const actions = '<div class="fc-actions" style="margin-top:12px">' + picker +
    '<button class="btn primary" id="btn-plan-review">' + (has ? "重新生成点评" : "生成当前时段点评") + "</button>" +
    '<button class="btn ghost" id="btn-plan-stop" hidden>中断</button>' +
    '<span class="muted" id="plan-status"></span></div>' +
    '<pre class="console" id="plan-log" hidden></pre>';
  const note = '<div class="fc-note">这是一次真实的模型调用（产生 token 费用），用所选 profile 的「研判档」；' +
    "结果落在 data/ai/plancheck/，不写回报告与配置。模型生成，不构成投资建议。</div>";

  if (!has) {
    const why = (doc && doc.error) ? '<div class="fail">上次点评失败：' + esc(doc.error) + "</div>" : "";
    return '<div class="muted">还没有针对这份报告的点评。点下面的按钮，用当前实盘价与时段调一次模型，' +
      "给出该时段该执行哪一条、哪些条件还没满足。</div>" + why + actions + note;
  }

  let html = '<div class="gauge-row">' +
    '<div class="gauge" style="flex:2"><span class="k">点评结论</span><span class="v" style="font-size:13px">' +
    esc(j["一句话结论"] || "—") + "</span></div>" +
    '<div class="gauge"><span class="k">点评时段</span><span class="v">' + esc(j["当前时段"] || "—") + "</span></div>" +
    '<div class="gauge"><span class="k">生成时间</span><span class="v" style="font-size:12.5px">' +
    esc(String(doc.generated_at || "").slice(0, 19)) + "</span></div>" +
    '<div class="gauge"><span class="k">模型</span><span class="v" style="font-size:12.5px">' +
    esc((doc.provider || "") + " / " + (doc.model || "")) + "</span></div>" +
    '<div class="gauge"><span class="k">费用</span><span class="v" style="font-size:12.5px">' +
    (((doc.cost || {})["人民币_估算"] == null) ? "未配置单价" : "¥" + fmt(doc.cost["人民币_估算"], 3)) + "</span></div>" +
    "</div>";
  if (doc.error) html += '<div class="fail">模型返回异常：' + esc(doc.error) + "</div>";
  const allActs = j["动作清单"] || [];
  const acts = allActs.filter(a => !planIsNoop(a["动作"]));
  const actNoop = allActs.length - acts.length;
  if (acts.length) {
    html += '<div class="table-wrap" style="max-height:none;margin-top:12px"><table class="tbl"><thead><tr>' +
      "<th>优先级</th><th>动作</th><th>执行</th><th>价位</th><th>理由</th></tr></thead><tbody>";
    acts.forEach(a => {
      const cls = a["执行"] === "执行" ? "ok" : (a["执行"] === "放弃" ? "bad" : "warn");
      html += "<tr><td>" + esc(a["优先级"] == null ? "—" : a["优先级"]) + "</td>" +
        '<td class="wrap"><b>' + esc(a["动作"] || "—") + "</b></td>" +
        "<td>" + badge(a["执行"] || "—", cls) + "</td>" +
        '<td class="mono">' + esc(a["价位"] || "—") + "</td>" +
        '<td class="wrap muted">' + esc(a["理由"] || "—") + "</td></tr>";
    });
    html += "</tbody></table></div>";
  }
  if (actNoop) {
    html += '<div class="muted" style="margin-top:6px">· 点评里 ' + actNoop +
      " 条观望/持有类条目已省略（没有执行动作）；想让它完全按新口径重写，点一次「重新生成点评」</div>";
  }
  const conf = j["触发确认"] || [];
  if (conf.length) {
    html += '<div class="sec-title" style="margin-top:12px">触发确认</div>' +
      '<div class="table-wrap" style="max-height:none"><table class="tbl"><thead><tr>' +
      "<th>条件</th><th>状态</th><th>依据</th></tr></thead><tbody>";
    conf.forEach(c => {
      const cls = c["状态"] === "已满足" ? "ok" : (c["状态"] === "未满足" ? "warn" : "flat");
      html += '<tr><td class="wrap">' + esc(c["条件"] || "—") + "</td>" +
        "<td>" + badge(c["状态"] || "—", cls) + "</td>" +
        '<td class="wrap muted">' + esc(c["依据"] || "—") + "</td></tr>";
    });
    html += "</tbody></table></div>";
  }
  const cols = [];
  if ((j["风险与失效"] || []).length) {
    cols.push("<div><div class=\"sec-title\">风险与失效</div>" +
      listOrEmpty(j["风险与失效"], "fail") + "</div>");
  }
  if ((j["数据依赖与不确定性"] || []).length) {
    cols.push("<div><div class=\"sec-title\">数据依赖与不确定性</div>" +
      listOrEmpty(j["数据依赖与不确定性"], "muted") + "</div>");
  }
  if (cols.length) html += '<div class="two-col" style="margin-top:12px">' + cols.join("") + "</div>";
  return html + actions + note;
}

export function renderPlanCheck(d) {
  const body = $("#plan-body");
  if (!body) return;
  const q = d["实盘"] || {};
  const sess = d["时段"] || {};
  const mech = d["机械"] || {};
  const rpt = d["报告"] || {};
  const price = num(q["价格"]);
  const chg = num(q["涨跌幅_pct"]);
  const head = $("#plan-head");
  if (head) head.textContent = "按这份报告的交易计划核对当前实盘价";

  let html = '<div class="gauge-row">' +
    '<div class="gauge"><span class="k">当前时段</span><span class="v">' + esc(sess["名称"] || "—") + "</span></div>" +
    '<div class="gauge"><span class="k">实盘价</span><span class="v ' + kindClass(chg) + '">' +
    (price === null ? "取不到" : fmt(price)) + "</span></div>" +
    '<div class="gauge"><span class="k">涨跌幅</span><span class="v ' + kindClass(chg) + '">' + fmtPct(chg) + "</span></div>" +
    '<div class="gauge"><span class="k">行情口径</span><span class="v" style="font-size:12.5px">' + esc(q["口径"] || "—") + "</span></div>" +
    '<div class="gauge"><span class="k">抓取时间</span><span class="v" style="font-size:12.5px">' + esc(q["抓取时间"] || "—") + "</span></div>" +
    '<div class="gauge"><span class="k">报告现价</span><span class="v" style="font-size:12.5px">' + fmt(rpt["报告现价"]) + "</span></div>" +
    "</div>";
  html += '<div class="rep-sub">' +
    chip("来源 " + (q["来源"] || "取不到"), q["来源"] && q["来源"].indexOf("降级") >= 0 ? "warn" : "") +
    chip(sess["是否交易日"] ? "交易日" : "非交易日", sess["是否交易日"] ? "" : "warn") +
    chip((d["关系"] || {})["文本"] || "—") +
    (q["量比"] == null ? "" : chip("量比 " + fmt(q["量比"]))) +
    (q["换手率_pct"] == null ? "" : chip("换手 " + fmt(q["换手率_pct"]) + "%")) +
    (q["主力净流入"] == null ? "" : chip("主力净流入 " + fmtMoney(q["主力净流入"], 0))) +
    "</div>";
  html += '<div class="hero-quote" style="margin-top:10px">' + esc(mech["一句话"] || "—") + "</div>";
  /* 把模型判定文字条件时用到的关键数据也摊在卡片上，便于核对它的依据 */
  const bg = d["背景数据"] || {};
  const stockBg = bg["个股量价与形态"] || {};
  const vp5 = ((stockBg["量价（近5/10/20日）"] || {})["近5日"]) || {};
  const an = stockBg["均线与分位"] || {};
  const ev = [];
  if (num(vp5["涨跌量比"]) !== null) ev.push("近5日涨跌量比 " + fmt(vp5["涨跌量比"]));
  if (num(an["pos60"]) !== null) ev.push("60日分位 " + fmt(an["pos60"], 1) + "%");
  if (num(an["ma5"]) !== null) ev.push("MA5 " + fmt(an["ma5"]) + " / MA20 " + fmt(an["ma20"]));
  if (num(vp5["上涨天数"]) !== null) ev.push("近5日涨" + fmt(vp5["上涨天数"], 0) + "跌" + fmt(vp5["下跌天数"], 0));
  if (ev.length) {
    html += '<div class="rep-sub" style="margin-top:8px">' +
      ev.map(x => chip(x)).join("") + "</div>";
  }
  (d["提示"] || []).forEach(h => { html += '<div class="muted" style="margin-top:6px">· ' + esc(h) + "</div>"; });
  html += planItemsHtml(mech["计划"] || []);
  if (mech["省略"]) {
    html += '<div class="muted" style="margin-top:6px">· ' + esc(mech["省略"]["说明"] || "") + "</div>";
  }
  html += planLevelsHtml(mech["关键价位"] || {});
  html += '<div class="sec-title" style="margin-top:16px">当前时段策略（模型点评）</div>';
  body.innerHTML = html + planReviewBlock(d);
  bindPlanCheck();
}

export async function loadPlanCheck(refresh) {
  const b = State.report;
  const body = $("#plan-body");
  if (!b || !body) return;
  if (refresh) body.innerHTML = '<div class="muted">正在取实盘价 …</div>';
  try {
    const url = "/api/plancheck?report=" + encodeURIComponent(b["json路径"]) + (refresh ? "&refresh=1" : "");
    const d = await api(url);
    PlanCheck.data = d;
    renderPlanCheck(d);
  } catch (e) {
    body.innerHTML = '<div class="fail">实盘复核失败：' + esc(e.message) + "</div>";
  }
}

export function bindPlanCheck() {
  const refresh = $("#btn-plan-refresh");
  if (refresh && !refresh.dataset.bound) {
    refresh.dataset.bound = "1";
    refresh.addEventListener("click", () => loadPlanCheck(true));
  }
  const run = $("#btn-plan-review");
  if (run) run.addEventListener("click", startPlanReview);
  const stop = $("#btn-plan-stop");
  if (stop) {
    stop.addEventListener("click", async () => {
      if (!PlanCheck.jobId) return;
      try { await api("/api/jobs/" + PlanCheck.jobId + "/cancel", { method: "POST", body: "{}" }); }
      catch (e) { toast(e.message, "bad"); }
    });
  }
}

export async function startPlanReview() {
  const b = State.report;
  if (!b) return;
  const btn = $("#btn-plan-review"), stop = $("#btn-plan-stop");
  const con = $("#plan-log"), st = $("#plan-status");
  if (btn) btn.disabled = true;
  if (stop) stop.hidden = false;
  if (con) { con.hidden = false; con.innerHTML = '<span class="l-dim">正在准备 …</span>'; }
  if (st) st.textContent = "运行中 …（1 次模型调用）";
  try {
    const res = await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({ kind: "plancheck", report: b["json路径"], refresh: 1,
                             profile: ($("#plan-profile") || {}).value || "" }),
    });
    if (!res.ok) throw new Error(res.error || "启动失败");
    PlanCheck.jobId = res.id;
    let from = 0, guard = 0;
    while (guard++ < 600) {
      const j = await api("/api/jobs/" + res.id + "?from=" + from);
      (j.lines || []).forEach(l => {
        if (!con) return;
        const t = (l.text || "").trim();
        const cls = t.indexOf("[OK]") === 0 ? "l-ok" : (t.indexOf("[WARN]") === 0 ? "l-warn"
          : (t.indexOf("[FAIL]") === 0 ? "l-fail" : (t.indexOf("[..]") === 0 ? "l-stage" : "")));
        const div = document.createElement("div");
        div.innerHTML = '<span class="' + cls + '">' + esc(l.text) + "</span>";
        con.appendChild(div);
        con.scrollTop = con.scrollHeight;
      });
      from = j.next;
      if (j.status !== "running") {
        PlanCheck.jobId = null;
        if (btn) btn.disabled = false;
        if (stop) stop.hidden = true;
        if (j.status === "done") {
          const warn = !!(j.result && j.result.error);
          if (st) st.textContent = "完成，用时 " + j.elapsed + "s";
          toast(warn ? "点评未成功（机械结论已保留）" : "当前时段点评已生成", warn ? "warn" : "ok");
          await loadPlanCheck(false);
        } else if (j.status === "canceled") {
          if (st) st.textContent = "已中断";
          toast("已中断", "warn");
        } else {
          if (st) st.textContent = "失败（退出码 " + j.exit_code + "）";
          toast("点评失败", "bad");
        }
        return;
      }
      await sleep(900);
    }
    if (btn) btn.disabled = false;
    if (stop) stop.hidden = true;
    if (st) st.textContent = "超时";
  } catch (e) {
    PlanCheck.jobId = null;
    if (btn) btn.disabled = false;
    if (stop) stop.hidden = true;
    if (st) st.textContent = "失败：" + e.message;
    toast(e.message, "bad");
  }
}

/* 注册给 core/app.js：切到本页时按需加载 / 对外暴露的动作。 */
registerView("report", {
  onShow: () => { if (!State.report) loadLatestReport(); },
  render: renderReport,
  refreshReports: refreshReports,
  renderPicker: renderReportPicker,
  loadLatest: loadLatestReport,
});
