/* 模型配置页。 */
import { api } from "../core/api.js";
import { State, refreshState, registerView } from "../core/app.js";
import { confirmMobileWrite, isMobileShell } from "../core/mobile.js";
import { $, chip, esc, toast } from "../core/util.js";

export const PROFILE_PHASES = [["prep", "盘前"], ["live", "盘中"], ["post", "盘后"], ["all", "全时段"]];


function optionsHtml(profiles, current, followLabel) {
  const head = followLabel ? '<option value="">' + esc(followLabel) + "</option>" : "";
  return head + (profiles || []).map(p =>
    '<option value="' + esc(p["名称"]) + '"' + (p["名称"] === current ? " selected" : "") + ">" +
    esc(p["名称"]) + "</option>").join("");
}


async function saveDefaults() {
  const body = {"默认_profile": ($("#default-profile") || {}).value || ""};
  const byPhase = {};
  PROFILE_PHASES.forEach(([key]) => { byPhase[key] = (($("#default-phase-" + key) || {}).value) || ""; });
  body["profiles_by_phase"] = byPhase;
  const msg = $("#models-default-msg");
  if (msg) msg.textContent = "";
  try {
    const res = await api("/api/models/default", {method: "POST", body: JSON.stringify(body)});
    if (!res.ok) { if (msg) msg.textContent = res.error; toast(res.error, "bad"); return; }
    const changes = res["改动"] || [];
    toast(changes.length ? ("已切换：" + changes.join("，")) : "配置没变化",
          changes.length ? "ok" : "warn");
    if (msg) msg.textContent = changes.length
      ? ("已写入 " + (res["备份"] ? "（备份 " + res["备份"] + "）" : "")) : "内容与文件一致，未写入";
    await loadModels();
    refreshState();
  } catch (e) { toast(e.message, "bad"); }
}


export async function setDefaultProfile(name) {
  try {
    const res = await api("/api/models/default", {
      method: "POST", body: JSON.stringify({"默认_profile": name}),
    });
    if (!res.ok) { toast(res.error, "bad"); return; }
    toast("默认 profile 已切换为 " + name + (res["备份"] ? "（旧配置备份为 " + res["备份"] + "）" : ""), "ok");
    await loadModels();
    refreshState();
  } catch (e) { toast(e.message, "bad"); }
}

export async function loadModels() {
  const m = await api("/api/models");
  State.models = m;
  $("#models-default").textContent = "默认 profile：" + (m["默认_profile"] || "—");
  $("#models-editor").value = m["原文"] || "";
  const byPhase = m["profiles_by_phase"] || {};
  const phaseNote = Object.keys(byPhase).filter(k => byPhase[k]).map(k => k + "→" + byPhase[k]).join("，");
  const profiles = m["profiles"] || [];
  if ($("#default-profile")) $("#default-profile").innerHTML =
    optionsHtml(profiles, m["默认_profile"]);
  PROFILE_PHASES.forEach(([key]) => {
    const el = $("#default-phase-" + key);
    if (el) el.innerHTML = optionsHtml(profiles, byPhase[key] || "", "跟随默认");
  });
  const grid = $("#profile-grid");
  grid.innerHTML = profiles.map(p => {
    const isDef = p["名称"] === m["默认_profile"];
    return '<div class="profile' + (isDef ? " is-default" : "") + '">' +
      "<h4>" + esc(p["名称"]) + (isDef ? chip("默认", "accent") : "") + "</h4>" +
      roleLine("研判", p["研判"]) + roleLine("复核", p["复核"]) +
      '<div class="fl-act">' + (isDef ? "" :
        '<button class="btn sm" data-default="' + esc(p["名称"]) + '">设为默认</button>') + "</div>" +
      "</div>";
  }).join("") + (phaseNote ? '<div class="profile"><h4>profiles_by_phase</h4><div class="role">' + esc(phaseNote) + "</div></div>" : "");
  if (grid.dataset.bound !== "1") {
    grid.dataset.bound = "1";
    grid.addEventListener("click", e => {
      const btn = e.target.closest("[data-default]");
      if (!btn) return;
      setDefaultProfile(btn.dataset.default);
    });
  }
  const saveBtn = $("#btn-default-save");
  if (saveBtn && saveBtn.dataset.bound !== "1") {
    saveBtn.dataset.bound = "1";
    saveBtn.addEventListener("click", saveDefaults);
  }

  const cpsel = $("#check-profile");
  const prev = cpsel.value;
  cpsel.innerHTML = (m["profiles"] || []).map(p =>
    '<option value="' + esc(p["名称"]) + '">' + esc(p["名称"]) + "</option>").join("");
  cpsel.value = prev && (m["profiles"] || []).some(p => p["名称"] === prev)
    ? prev : (m["默认_profile"] || "");

  let html = "<thead><tr><th>名称</th><th>协议</th><th>base_url</th><th>JSON模式</th><th>Key</th><th>模型可选</th></tr></thead><tbody>";
  (m["providers"] || []).forEach(p => {
    const keyCls = p["key来源"] ? "ok" : "bad";
    html += "<tr>" +
      "<td><b>" + esc(p["名称"]) + "</b></td>" +
      "<td>" + chip(p["协议"] || "—") + "</td>" +
      '<td class="mono muted wrap">' + esc(p["base_url"] || "—") + "</td>" +
      "<td>" + (p["json_object"] ? chip("支持", "ok") : chip("否", "flat")) + "</td>" +
      "<td>" + chip(p["key掩码"] || "(none)", keyCls) + '<div class="muted">' + esc(p["key来源"] || "未找到") + "</div></td>" +
      '<td class="muted wrap">' + esc((p["模型可选"] || []).join(", ")) + "</td>" +
      "</tr>";
  });
  $("#provider-table").innerHTML = html + "</tbody>";
}

export function roleLine(role, c) {
  if (!c) return '<div class="role"><b>' + role + '</b> <span class="muted">未配置（跳过）</span></div>';
  const params = c["参数"] ? " · " + JSON.stringify(c["参数"]) : "";
  return '<div class="role"><b>' + role + "</b> " + esc(c.provider || "?") + " / " + esc(c.model || "?") +
    ' <span class="muted">temp ' + (c.temperature == null ? "—" : c.temperature) +
    " · max_tokens " + (c.max_tokens == null ? "—" : c.max_tokens) + esc(params) + "</span></div>";
}

async function persistModels(text) {
  const res = await api("/api/models", { method: "POST", body: JSON.stringify({ text: text }) });
  if (res.ok) {
    toast(res["已写入"] ? "模型配置已保存" : "模型配置无变化，未写入", res["已写入"] ? "ok" : "warn");
    $("#models-msg").textContent = res["已写入"] ? "已保存，备份为 模型配置.json.bak" : "内容与文件一致，未写入、未产生备份";
    loadModels(); refreshState();
  }
  else { toast(res.error, "bad"); $("#models-msg").textContent = res.error; }
}

export async function saveModels() {
  const text = $("#models-editor").value;
  if (!isMobileShell()) return persistModels(text);
  const lines = text ? text.split(/\r?\n/).length : 0;
  let shape = "当前 JSON 可由服务端继续校验";
  try {
    const parsed = JSON.parse(text);
    const providers = Array.isArray(parsed && parsed.providers) ? parsed.providers.length : 0;
    const profiles = parsed && parsed.profiles && typeof parsed.profiles === "object"
      ? Object.keys(parsed.profiles).length : 0;
    shape = "JSON 格式有效，包含 " + providers + " 个 provider / " + profiles + " 个 profile";
  } catch (e) {
    shape = "当前内容不是合法 JSON，服务端会拒绝写入";
  }
  confirmMobileWrite("确认保存模型配置",
    "将覆盖 config/模型配置.json（" + lines + " 行；" + shape + "）。服务端会在写入前自动保留 .bak 备份。",
    () => persistModels(text));
}

export async function checkModel() {
  const el = $("#model-console");
  el.hidden = false;
  el.dataset.clean = "1";
  el.innerHTML = '<span class="l-dim">正在自检 …</span>';
  const body = { kind: "check", phase: "post", profile: $("#check-profile").value || (State.models || {})["默认_profile"] };
  const res = await api("/api/jobs", { method: "POST", body: JSON.stringify(body) });
  if (!res.ok) { toast(res.error, "bad"); return; }
  let from = 0;
  const tick = async () => {
    const j = await api("/api/jobs/" + res.id + "?from=" + from);
    (j.lines || []).forEach(l => {
      const d = document.createElement("div");
      const t = (l.text || "").trim();
      let cls = "";
      if (t.startsWith("[OK]")) cls = "l-ok";
      else if (t.startsWith("[WARN]")) cls = "l-warn";
      else if (t.startsWith("[FAIL]")) cls = "l-fail";
      else if (t.startsWith("[..]")) cls = "l-stage";
      else if (t.startsWith("  ")) cls = "l-dim";
      d.innerHTML = '<span class="' + cls + '">' + esc(l.text) + "</span>";
      el.appendChild(d);
    });
    el.scrollTop = el.scrollHeight;
    from = j.next;
    if (j.status === "running") setTimeout(tick, 700);
    else toast("自检结束（退出码 " + j.exit_code + "）", j.exit_code === 0 ? "ok" : "bad");
  };
  tick();
}

/* 注册给 core/app.js：切到本页时按需加载 / 对外暴露的动作。 */
registerView("models", { onShow: loadModels });
