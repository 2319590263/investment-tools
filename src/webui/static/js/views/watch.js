/* 自选股页。 */
import { api } from "../core/api.js";
import { State, refreshState, registerView, showView, viewApi } from "../core/app.js";
import { $, $$, chip, esc, fmt, fmtPct, kindClass, toast } from "../core/util.js";
import { closeModal, confirmModal, openModal } from "../ui/modal.js";

export async function loadWatch() {
  let res;
  try { res = await api("/api/watchlist"); } catch (e) { toast(e.message, "bad"); return; }
  State.watch = res["条目"] || [];
  if (State.state) State.state["自选股"] = State.watch;
  $("#watch-path").textContent = res["路径"] || "";
  renderWatch(State.watch);
}

export function renderWatch(items) {
  const tbl = $("#watch-table");
  if (!tbl) return;
  if (!items.length) {
    tbl.innerHTML = '<tbody><tr><td class="empty">还没有自选股。上面填 6 位代码点「加入自选」即可；' +
      "自选股会出现在运行页的「选择」列表里，也可以一键批量研判。</td></tr></tbody>";
    return;
  }
  let html = "<thead><tr><th>代码</th><th>名称</th><th>备注</th><th class='num'>现价</th>" +
    "<th class='num'>涨跌幅</th><th>数据</th><th>操作</th></tr></thead><tbody>";
  items.forEach((it, i) => {
    const cached = it["在缓存"];
    html += "<tr>" +
      "<td><b>" + esc(it["代码"]) + "</b></td>" +
      "<td>" + esc(it["名称"] || "—") + "</td>" +
      '<td class="wrap muted">' + esc(it["备注"] || "") + "</td>" +
      '<td class="num">' + fmt(it["现价"], 3) + "</td>" +
      '<td class="num ' + kindClass(it["涨跌幅_pct"]) + '">' + (it["涨跌幅_pct"] == null ? "—" : fmtPct(it["涨跌幅_pct"])) + "</td>" +
      "<td>" + (cached ? chip("已缓存", "ok") : chip("未抓数", "warn")) + "</td>" +
      '<td><a href="#" data-run="' + i + '">研判</a>&nbsp;&nbsp;' +
      '<button class="btn sm danger" data-rm="' + i + '">删除</button></td>' +
      "</tr>";
  });
  tbl.innerHTML = html + "</tbody>";

  $$("#watch-table [data-run]").forEach(a => a.addEventListener("click", e => {
    e.preventDefault();
    const it = items[Number(a.dataset.run)];
    viewApi("run").pickCode(it["代码"]);
    showView("run");
    toast("已把 " + it["代码"] + " 填入运行页", "ok");
  }));
  $$("#watch-table [data-rm]").forEach(btn => btn.addEventListener("click", () => {
    const it = items[Number(btn.dataset.rm)];
    confirmModal("从自选股移除？", "将把 " + it["代码"] + " " + (it["名称"] || "") + " 从自选股.md 中删除（文件有 .bak 备份）。", async () => {
      try {
        const res = await api("/api/watchlist/remove", { method: "POST", body: JSON.stringify({ code: it["代码"] }) });
        if (!res.ok) { toast(res.error, "bad"); return; }
        toast("已移除 " + it["代码"], "ok");
        State.watch = res["自选股"] || [];
        if (State.state) { State.state["自选股"] = State.watch; State.state["代码候选"] = res["代码候选"] || State.state["代码候选"]; }
        renderWatch(State.watch);
        refreshState();
      } catch (e) { toast(e.message, "bad"); }
    }, "确认移除");
  }));
}

export async function addWatch() {
  const code = ($("#watch-code").value || "").trim();
  if (!code) { $("#watch-code").focus(); return; }
  $("#watch-msg").textContent = "";
  try {
    const res = await api("/api/watchlist/add", {
      method: "POST",
      body: JSON.stringify({ code: code, name: ($("#watch-name").value || "").trim(), note: ($("#watch-note").value || "").trim() }),
    });
    if (!res.ok) { $("#watch-msg").textContent = res.error; return; }
    toast("已加入自选：" + code, "ok");
    $("#watch-code").value = ""; $("#watch-name").value = ""; $("#watch-note").value = "";
    State.watch = res["自选股"] || [];
    if (State.state) { State.state["自选股"] = State.watch; State.state["代码候选"] = res["代码候选"] || State.state["代码候选"]; }
    renderWatch(State.watch);
    refreshState();
  } catch (e) { $("#watch-msg").textContent = e.message; }
}

export function initWatchView() {
  const add = $("#btn-watch-add");
  if (add) add.addEventListener("click", addWatch);
  ["#watch-code", "#watch-name", "#watch-note"].forEach(sel => {
    const el = $(sel);
    if (el) el.addEventListener("keydown", e => { if (e.key === "Enter") addWatch(); });
  });
  const bb = $("#btn-watch-batch");
  if (bb) bb.addEventListener("click", () => viewApi("run").showBatchDialog("watch"));
  const raw = $("#btn-watch-raw");
  if (raw) raw.addEventListener("click", async () => {
    const res = await api("/api/watchlist");
    openModal("自选股.md 原文",
      '<textarea class="editor" id="watch-raw">' + esc(res["原文"] || "") + "</textarea>" +
      '<div class="row" style="margin-top:10px"><button class="btn primary" id="watch-raw-save">保存</button>' +
      '<span class="muted">每行格式：| 代码 | 名称 | 备注 |</span></div>');
    $("#watch-raw-save").addEventListener("click", async () => {
      const r = await api("/api/watchlist", { method: "POST", body: JSON.stringify({ text: $("#watch-raw").value }) });
      if (!r.ok) { toast(r.error, "bad"); return; }
      toast(r["已写入"] ? "已保存" : "内容无变化，未写入", r["已写入"] ? "ok" : "warn");
      closeModal();
      State.watch = r["自选股"] || [];
      if (State.state) { State.state["自选股"] = State.watch; State.state["代码候选"] = r["代码候选"] || State.state["代码候选"]; }
      renderWatch(State.watch);
      refreshState();
    });
  });
}

/* 注册给 core/app.js：切到本页时按需加载 / 对外暴露的动作。 */
registerView("watch", { onShow: loadWatch });
