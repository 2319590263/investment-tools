/* 历史与复盘页：合并表、删除与回收站。 */
import { api } from "../core/api.js";
import { State, openReport, registerView, viewApi } from "../core/app.js";
import { $, $$, chip, dirColor, esc, fmt, fmtSize, num, toast } from "../core/util.js";
import { closeModal, confirmModal, openModal } from "../ui/modal.js";

export async function loadHistory() {
  const res = await api("/api/history");
  State.reports = res.reports || [];
  renderHistory(res.plan_log || [], State.reports);
  await loadTrash();
}

/* 计划记录与报告归档本就是同一份产出，合并成一张表：
   有 plan_log 记录的按记录走；只剩下报告（记录被删过）的用报告摘要补一行。 */

export function mergeHistoryRows(logs, reports) {
  const rows = [], used = {};
  (logs || []).forEach(rec => {
    const p = rec["_报告相对"];
    if (p) used[p] = true;
    rows.push({ log: rec, report: (reports || []).find(r => r["json路径"] === p) || null });
  });
  (reports || []).forEach(r => {
    if (!used[r["json路径"]]) rows.push({ log: null, report: r });
  });
  const stamp = row => String(
    (row.log && row.log["时间"]) || (row.report && (row.report["摘要"] || {})["生成时间"])
    || (row.report && row.report["时间"]) || "");
  rows.sort((a, b) => stamp(b).localeCompare(stamp(a)));
  return rows;
}


/* 一行同时牵涉 plan_log 记录与报告文件，所以删除时给出明确的两个选择 */
export function renderHistory(logs, reports) {
  const tbl = $("#hist-table");
  const rows = mergeHistoryRows(logs, reports);
  const cnt = $("#hist-count");
  if (cnt) cnt.textContent = rows.length ? "共 " + rows.length + " 条" : "";
  if (!rows.length) {
    tbl.innerHTML = '<tbody><tr><td class="empty">还没有研判记录：先在「运行研判」里跑一次。</td></tr></tbody>';
    return;
  }
  let html = "<thead><tr><th>时间</th><th>时段</th><th>标的</th><th>方向</th><th class='num'>置信度</th>" +
    "<th>校验</th><th>模型</th><th class='num'>费用(¥)</th><th>复盘</th><th>报告</th><th>操作</th></tr></thead><tbody>";

  rows.forEach((row, i) => {
    const rec = row.log, rep = row.report, sum = (rep || {})["摘要"] || {};
    const code = (rec && (rec["标的"] || {})["代码"]) || sum["标的代码"] || "";
    const name = (rec && (rec["标的"] || {})["名称"]) || sum["标的名称"] || "";
    const time = (rec && rec["时间"]) || sum["生成时间"] || (rep ? rep["时间"] : "") || "—";
    const phase = (rec && rec.phase) || (rep && rep.phase) || "";
    const dir = (rec && rec["方向"]) || sum["方向"];
    const conf = (rec && rec["置信度"] != null) ? rec["置信度"] : sum["置信度"];
    const model = (rec && rec["研判模型"]) || sum["研判模型"];

    let checkHtml = '<span class="muted">—</span>';
    if (rep) {
      const c = sum["校验统计"] || {};
      checkHtml = (c["违规"] ? chip("违规 " + c["违规"], "bad") : chip("通过 " + (c["通过"] || 0), "ok")) +
        (c["提示"] ? " " + chip("提示 " + c["提示"], "warn") : "");
    }
    let fee = "—";
    const f = (rec && rec["费用"]) || {};
    if (f["研判_人民币_估算"] != null || f["复核_人民币_估算"] != null) {
      fee = fmt((num(f["研判_人民币_估算"]) || 0) + (num(f["复核_人民币_估算"]) || 0), 3);
    } else if (sum["费用_元"] != null) {
      fee = fmt(sum["费用_元"], 3);
    }
    let rvTxt = "—";
    const rv = rec && rec["复盘"];
    if (rv) {
      const hit = rv["方向命中"], parts = [];
      if (hit) parts.push("方向 " + (hit["结论"] || hit["命中"] || "—"));
      const p = rv["计划回检"] || [];
      if (Array.isArray(p) && p.length) parts.push("计划 " + p.length + " 条");
      rvTxt = parts.join(" · ") || "已回检";
    }
    const reportCell = rep
      ? '<a href="#" data-jump="' + esc(rep["json路径"]) + '">查看</a>'
      : (rec && rec["_报告相对"] ? '<span class="muted">报告已删除</span>' : '<span class="muted">—</span>');

    html += "<tr>" +
      "<td>" + esc(time) + "</td>" +
      "<td>" + (phase ? chip(phase) : '<span class="muted">—</span>') + "</td>" +
      "<td>" + esc(code) + " <span class='muted'>" + esc(name) + "</span>" +
      (rec ? "" : ' <span class="badge flat" title="plan_log 里已无对应记录">无记录</span>') + "</td>" +
      '<td class="' + dirColor(dir) + '"><b>' + esc(dir || "—") + "</b></td>" +
      '<td class="num">' + (conf == null ? "—" : conf) + "</td>" +
      "<td>" + checkHtml + "</td>" +
      '<td class="muted">' + esc(model || "—") + "</td>" +
      '<td class="num">' + fee + "</td>" +
      '<td class="wrap muted">' + esc(rvTxt) + "</td>" +
      "<td>" + reportCell + "</td>" +
      '<td><button class="btn sm danger" data-rowdel="' + i + '">删除</button></td>' +
      "</tr>";
  });
  tbl.innerHTML = html + "</tbody>";

  $$("#hist-table [data-jump]").forEach(a => a.addEventListener("click", e => {
    e.preventDefault(); openReport(a.dataset.jump);
  }));
  $$("#hist-table [data-rowdel]").forEach(btn => btn.addEventListener("click", () => {
    showHistoryDelete(rows[Number(btn.dataset.rowdel)]);
  }));
}

export function showHistoryDelete(row) {
  const rec = row.log, rep = row.report;
  const line = rec ? rec["_行号"] : null;
  const path = rep ? rep["json路径"] : null;
  let text = "";
  if (line != null && path) {
    text = "这一行对应 plan_log 记录 + 报告 " + path + "。你可以只删记录（报告留着），也可以两个一起删（报告移入回收站）。";
  } else if (line != null) {
    text = "只删除 plan_log 里的这条记录（对应报告已不在）。删除前会把整个日志快照存进回收站。";
  } else {
    text = "这一行只有报告（对应的计划记录已被删过）：报告会移入回收站。";
  }
  let btns = '<button class="btn ghost" id="hd-cancel">取消</button>';
  if (line != null) btns += '<button class="btn" id="hd-log">只删记录</button>';
  if (path) btns += '<button class="btn danger" id="hd-both">' + (line != null ? "记录和报告都删" : "把报告移入回收站") + "</button>";
  openModal("删除这一行？", '<div class="muted" style="line-height:1.9">' + esc(text) + "</div>" +
    '<div class="row" style="margin-top:18px;justify-content:flex-end;flex-wrap:wrap">' + btns + "</div>");

  $("#hd-cancel").addEventListener("click", closeModal);
  const logBtn = $("#hd-log");
  if (logBtn) logBtn.addEventListener("click", async () => {
    closeModal();
    await deleteHistoryLine(line);
    if (path) toast("已删除计划记录（报告保留）", "ok");
  });
  const bothBtn = $("#hd-both");
  if (bothBtn) bothBtn.addEventListener("click", async () => {
    closeModal();
    if (line != null) await deleteHistoryLine(line);
    if (path) await deleteReportFile(path);
  });
}

export async function deleteHistoryLine(line) {
  try {
    const res = await api("/api/history/delete", { method: "POST", body: JSON.stringify({ line: line }) });
    if (!res.ok) { toast(res.error, "bad"); return; }
    toast("计划记录已删除，日志快照存于回收站", "ok");
    State.reports = res.reports || State.reports;
    viewApi("report").renderPicker();
    renderHistory(res.plan_log || [], State.reports);
    await loadTrash();
  } catch (e) { toast(e.message, "bad"); }
}

export async function deleteReportFile(path) {
  try {
    const res = await api("/api/report/delete", { method: "POST", body: JSON.stringify({ path: path }) });
    if (!res.ok) { toast(res.error, "bad"); return; }
    toast("报告已移入回收站", "ok");
    State.reports = res.reports || [];
    viewApi("report").renderPicker();
    renderHistory(res.history || [], State.reports);
    await loadTrash();
    if (State.report && State.report["json路径"] === path) {
      State.report = null;
      viewApi("report").loadLatest();
    }
  } catch (e) { toast(e.message, "bad"); }
}

export async function loadTrash() {
  const tbl = $("#trash-table");
  if (!tbl) return;
  let res;
  try { res = await api("/api/trash"); } catch (e) { return; }
  const items = res.items || [];
  const cnt = $("#trash-count");
  if (cnt) cnt.textContent = items.length ? "共 " + items.length + " 个文件" : "";
  const note = $("#trash-purge-note");
  if (note) {
    const last = res["上次清理"] || {};
    const days = res["过期天数"] || 7;
    if (last["清理时间"]) {
      note.textContent = "保留 " + days + " 天 ｜ 上次清理 " + last["清理时间"] +
        "（删除 " + ((last["删除"] || []).length) + " 个）";
    } else {
      note.textContent = "保留 " + days + " 天";
    }
  }
  if (!items.length) {
    tbl.innerHTML = '<tbody><tr><td class="empty">回收站是空的。删除报告或计划记录时会先移到这里，随时可以恢复。</td></tr></tbody>';
    return;
  }
  let html = "<thead><tr><th>时间</th><th>类型</th><th>名称</th><th>原位置</th>" +
    "<th class='num'>大小</th><th class='num'>剩余天数</th><th>操作</th></tr></thead><tbody>";
  items.forEach((it, i) => {
    html += "<tr>" +
      "<td>" + esc(it["时间"]) + "</td>" +
      "<td>" + chip(it["类型"]) + "</td>" +
      "<td>" + esc(it["名称"]) + "</td>" +
      '<td class="muted">' + esc(it["原路径"]) + "</td>" +
      '<td class="num muted">' + fmtSize(it["大小"]) + "</td>" +
      '<td class="num ' + (it["即将过期"] ? "warn" : "muted") + '">' +
        (it["剩余天数"] == null ? "—" : esc(it["剩余天数"]) + " 天") + "</td>" +
      '<td><button class="btn sm" data-restore="' + i + '">恢复</button></td>' +
      "</tr>";
  });
  tbl.innerHTML = html + "</tbody>";
  $$("#trash-table [data-restore]").forEach(btn => btn.addEventListener("click", async () => {
    const it = items[Number(btn.dataset.restore)];
    const warn = it.kind === "history"
      ? "这是计划历史的整份快照，恢复会覆盖当前的 plan_log.jsonl（覆盖前会另存一份当前文件）。"
      : "文件会从回收站搬回 " + it["原路径"] + "。";
    confirmModal("恢复这个文件？", warn, async () => {
      try {
        const res = await api("/api/trash/restore", { method: "POST", body: JSON.stringify({ path: it["回收站路径"] }) });
        if (!res.ok) { toast(res.error, "bad"); return; }
        toast("已恢复：" + res["恢复"], "ok");
        State.reports = res.reports || State.reports;
        viewApi("report").renderPicker();
        renderHistory(res.history || [], State.reports);
        await loadTrash();
      } catch (e) { toast(e.message, "bad"); }
    }, "确认恢复");
  }));
}

export async function purgeTrashNow() {
  confirmModal("清理超过 7 天的回收站条目？",
    "这些文件会被**真正删除**，无法恢复；未超过 7 天的条目会保留，仍可随时恢复。",
    async () => {
      try {
        const res = await api("/api/trash/purge", { method: "POST", body: JSON.stringify({}) });
        if (!res.ok) { toast(res.error, "bad"); return; }
        const n = ((res["清理"] || {})["删除"] || []).length;
        toast(n ? "已真删 " + n + " 个过期条目" : "没有超过 7 天的条目", n ? "ok" : "warn");
        State.reports = res.reports || State.reports;
        await loadTrash();
      } catch (e) { toast(e.message, "bad"); }
    }, "确认真删");
}

/* 注册给 core/app.js：切到本页时按需加载 / 对外暴露的动作。 */
registerView("history", { onShow: loadHistory });
