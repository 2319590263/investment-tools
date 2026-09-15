/* 模型配置页。 */
import { api } from "../core/api.js";
import { State, refreshState, registerView } from "../core/app.js";
import { confirmMobileWrite, isMobileShell } from "../core/mobile.js";
import { $, chip, esc, toast } from "../core/util.js";
import { closeModal, confirmModal, openModal } from "../ui/modal.js";

function optionsHtml(profiles, current, followLabel) {
  const head = followLabel ? '<option value="">' + esc(followLabel) + "</option>" : "";
  return head + (profiles || []).map(p =>
    '<option value="' + esc(p["名称"]) + '"' + (p["名称"] === current ? " selected" : "") + ">" +
    esc(p["名称"]) + "</option>").join("");
}


async function saveDefaults() {
  /* 只写「默认_profile」：不再暴露按时间段下拉，profiles_by_phase 原样保留。 */
  const body = {"默认_profile": ($("#default-profile") || {}).value || ""};
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

/* ---------------- 新增 / 修改 / 删除：profile 与 provider（批注 8、9） ----------------
 * 只调 /api/models/profile 与 /api/models/provider；这两个接口写 config/模型配置.json，
 * 写前自动 .bak，校验全部在服务端做（重名、provider 不存在、被引用、JSON 非法）。
 */

function providerNames() {
  return ((State.models || {})["providers"] || []).map(p => p["名称"]).filter(Boolean);
}

function providerByName(name) {
  return ((State.models || {})["providers"] || []).find(p => p["名称"] === name) || {};
}

function roleFieldset(tag, title, role) {
  const r = role || null;
  const opts = providerNames().map(n =>
    '<option value="' + esc(n) + '"' + (r && r["provider"] === n ? " selected" : "") + ">" +
    esc(n) + "</option>").join("");
  return '<div class="ms-role" data-role="' + esc(tag) + '">' +
    '<div class="sec-title">' + esc(title) +
      ' <span class="muted">provider 与 model 都留空 = 这一档不配置（跳过）</span></div>' +
    '<div class="row" style="gap:8px;flex-wrap:wrap">' +
    '<div class="field" style="margin:0;flex:0 0 160px"><label>provider</label>' +
      '<select data-role-field="provider" class="inline-select">' +
      '<option value="">（不配置）</option>' + opts + "</select></div>" +
    '<div class="field" style="margin:0;flex:1 1 200px"><label>model</label>' +
      '<input data-role-field="model" list="ms-models-' + esc(tag) + '" value="' +
      esc((r && r["model"]) || "") + '" /></div>' +
    '<div class="field" style="margin:0;flex:0 0 110px"><label>temperature</label>' +
      '<input data-role-field="temperature" type="number" step="0.1" value="' +
      (r && r["temperature"] != null ? r["temperature"] : "") + '" /></div>' +
    '<div class="field" style="margin:0;flex:0 0 120px"><label>max_tokens</label>' +
      '<input data-role-field="max_tokens" type="number" value="' +
      (r && r["max_tokens"] != null ? r["max_tokens"] : "") + '" /></div>' +
    "</div>" +
    '<datalist id="ms-models-' + esc(tag) + '"></datalist>' +
    '<div class="field"><label>参数 <span class="muted">JSON 对象，可留空</span></label>' +
      '<input data-role-field="参数" value="' +
      esc(r && r["参数"] ? JSON.stringify(r["参数"]) : "") + '" /></div>' +
    "</div>";
}

function collectRole(root, tag) {
  const box = root.querySelector('[data-role="' + tag + '"]');
  const get = f => ((box.querySelector('[data-role-field="' + f + '"]') || {}).value || "").trim();
  return {provider: get("provider"), model: get("model"),
          temperature: get("temperature"), max_tokens: get("max_tokens"),
          参数: get("参数")};
}

function bindModelDatalists(root) {
  root.querySelectorAll("[data-role]").forEach(box => {
    const tag = box.dataset.role;
    const sel = box.querySelector('[data-role-field="provider"]');
    const list = root.querySelector("#ms-models-" + tag.replace(/[^A-Za-z0-9_-]/g, "_")) ||
      box.parentElement.querySelector("datalist");
    const fill = () => {
      const names = providerByName(sel.value)["模型可选"] || [];
      if (list) list.innerHTML = names.map(n => '<option value="' + esc(n) + '"></option>').join("");
    };
    if (sel) {
      if (list) list.id = "ms-models-" + tag.replace(/[^A-Za-z0-9_-]/g, "_");
      sel.addEventListener("change", fill);
      fill();
    }
  });
}

export function openProfileForm(mode, name) {
  const profiles = ((State.models || {})["profiles"]) || [];
  const src = profiles.find(p => p["名称"] === name) || null;
  const title = {新增: "新增 profile", 修改: "编辑 profile：" + name, 另存为: "另存为新的 profile（源：" + name + "）"}[mode];
  const body = '<div class="row" style="gap:8px;flex-wrap:wrap;align-items:flex-end">' +
    '<div class="field" style="margin:0;flex:1 1 220px"><label>profile 名称</label>' +
      '<input id="ms-profile-name" value="' +
      esc(mode === "新增" ? "" : (mode === "另存为" ? (name + "-副本") : name)) + '" /></div></div>' +
    roleFieldset("研判", "研判档（主模型）", src && src["研判"]) +
    roleFieldset("复核", "复核档（可选）", src && src["复核"]) +
    '<div class="err" id="ms-form-msg"></div>' +
    '<div class="row" style="margin-top:10px"><button class="btn primary" id="ms-profile-save">保存</button>' +
    '<span class="muted">写 config/模型配置.json，写入前自动 .bak</span></div>';
  openModal(title, body);
  bindModelDatalists(document);
  $("#ms-profile-save").addEventListener("click", async () => {
    const data = {名称: $("#ms-profile-name").value.trim(),
                  研判: collectRole(document, "研判"), 复核: collectRole(document, "复核")};
    if (mode !== "新增") data["原名"] = name;
    const action = mode === "修改" ? "修改" : "新增";
    const res = await api("/api/models/profile", {
      method: "POST", body: JSON.stringify({动作: action, 数据: data}),
    });
    if (!res.ok) { $("#ms-form-msg").textContent = res.error; return; }
    toast((res["改动"] || []).join("，") || "已写入", "ok");
    closeModal();
    await loadModels();
    refreshState();
  });
}

function deleteProfile(name) {
  confirmModal("删除 profile「" + name + "」？",
    "只从 config/模型配置.json 里删掉这个 profile（会先备份 .bak）。默认 profile 不能删。",
    async () => {
      const res = await api("/api/models/profile", {
        method: "POST", body: JSON.stringify({动作: "删除", 数据: {名称: name}}),
      });
      if (!res.ok) { toast(res.error, "bad"); return; }
      toast("已删除 " + name, "ok");
      await loadModels();
      refreshState();
    });
}

export function openProviderForm(mode, name) {
  const src = name ? providerByName(name) : {};
  const body = '<div class="row" style="gap:8px;flex-wrap:wrap;align-items:flex-end">' +
    '<div class="field" style="margin:0;flex:1 1 180px"><label>名称</label>' +
      '<input id="ms-prov-name" value="' + esc(mode === "新增" ? "" : name) + '" /></div>' +
    '<div class="field" style="margin:0;flex:0 0 180px"><label>协议</label>' +
      '<select id="ms-prov-proto" class="inline-select">' +
      '<option value="openai-chat"' + (src["协议"] === "anthropic-messages" ? "" : " selected") +
        ">openai-chat</option>" +
      '<option value="anthropic-messages"' + (src["协议"] === "anthropic-messages" ? " selected" : "") +
        ">anthropic-messages</option></select></div></div>" +
    '<div class="field"><label>base_url</label><input id="ms-prov-base" placeholder="https://api.example.com" value="' +
      esc(src["base_url"] || "") + '" /></div>' +
    '<div class="row" style="gap:8px;flex-wrap:wrap">' +
    '<div class="field" style="margin:0;flex:1 1 200px"><label>路径 <span class="muted">留空按协议默认</span></label>' +
      '<input id="ms-prov-path" value="' + esc(src["路径"] || "") + '" /></div>' +
    '<div class="field" style="margin:0;flex:0 0 200px"><label>key_env <span class="muted">环境变量名</span></label>' +
      '<input id="ms-prov-env" value="' + esc(src["key_env"] || "") + '" /></div></div>' +
    '<div class="field"><label>api_key <span class="muted">' +
      (mode === "新增" ? "可留空" : "留空 = 不改动现有 key") + '</span></label>' +
      '<input id="ms-prov-key" type="password" placeholder="' +
      (src["已配置"] ? esc(src["key掩码"] || "已配置") : "留空则该服务商显示「未配置」") + '" /></div>' +
    '<div class="row" style="gap:8px;flex-wrap:wrap">' +
    '<label class="switch sm"><input type="checkbox" id="ms-prov-json"' +
      (src["json_object"] ? " checked" : "") + ' /><span class="track"></span>' +
      '<span class="sw-label">支持 response_format=json_object</span></label></div>' +
    '<div class="field"><label>模型可选 <span class="muted">逗号分隔</span></label>' +
      '<input id="ms-prov-models" value="' + esc((src["模型可选"] || []).join(", ")) + '" /></div>' +
    '<div class="field"><label>备注</label><input id="ms-prov-note" value="' +
      esc(src["备注"] || "") + '" /></div>' +
    '<div class="err" id="ms-form-msg"></div>' +
    '<div class="row" style="margin-top:10px"><button class="btn primary" id="ms-prov-save">保存</button>' +
    '<span class="muted">写 config/模型配置.json，写入前自动 .bak</span></div>';
  openModal(mode === "新增" ? "新增服务商" : "编辑服务商：" + name, body);
  $("#ms-prov-save").addEventListener("click", async () => {
    const data = {名称: $("#ms-prov-name").value.trim(),
                  协议: $("#ms-prov-proto").value,
                  base_url: $("#ms-prov-base").value.trim(),
                  路径: $("#ms-prov-path").value.trim(),
                  key_env: $("#ms-prov-env").value.trim(),
                  api_key: $("#ms-prov-key").value,
                  json_object: $("#ms-prov-json").checked,
                  模型可选: $("#ms-prov-models").value,
                  备注: $("#ms-prov-note").value.trim()};
    if (mode !== "新增") data["原名"] = name;
    const res = await api("/api/models/provider", {
      method: "POST", body: JSON.stringify({动作: mode === "新增" ? "新增" : "修改", 数据: data}),
    });
    if (!res.ok) { $("#ms-form-msg").textContent = res.error; return; }
    toast((res["改动"] || []).join("，") || "已写入", "ok");
    closeModal();
    await loadModels();
    refreshState();
  });
}

function deleteProvider(name) {
  confirmModal("删除服务商「" + name + "」？",
    "只从 config/模型配置.json 里删掉这个 provider（会先备份 .bak）。还有 profile 在用它时会拒绝。",
    async () => {
      const res = await api("/api/models/provider", {
        method: "POST", body: JSON.stringify({动作: "删除", 数据: {名称: name}}),
      });
      if (!res.ok) { toast(res.error, "bad"); return; }
      toast("已删除 " + name, "ok");
      await loadModels();
      refreshState();
    });
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
  const grid = $("#profile-grid");
  grid.innerHTML = profiles.map(p => {
    const isDef = p["名称"] === m["默认_profile"];
    const n = esc(p["名称"]);
    return '<div class="profile' + (isDef ? " is-default" : "") + '">' +
      "<h4>" + esc(p["名称"]) + (isDef ? chip("默认", "accent") : "") + "</h4>" +
      roleLine("研判", p["研判"]) + roleLine("复核", p["复核"]) +
      '<div class="fl-act">' +
        '<button class="btn sm ghost" data-edit-profile="' + n + '">编辑</button>' +
        '<button class="btn sm ghost" data-copy-profile="' + n + '">另存为</button>' +
        (isDef ? "" : '<button class="btn sm" data-default="' + n + '">设为默认</button>') +
        (isDef ? "" : '<button class="btn sm danger" data-del-profile="' + n + '">删除</button>') +
      "</div>" +
      "</div>";
  }).join("") + (phaseNote ? '<div class="profile"><h4>profiles_by_phase</h4><div class="role">' + esc(phaseNote) + "</div></div>" : "");
  if (grid.dataset.bound !== "1") {
    grid.dataset.bound = "1";
    grid.addEventListener("click", e => {
      const t = e.target;
      const def = t.closest("[data-default]");
      if (def) { setDefaultProfile(def.dataset.default); return; }
      const edit = t.closest("[data-edit-profile]");
      if (edit) { openProfileForm("修改", edit.dataset.editProfile); return; }
      const copy = t.closest("[data-copy-profile]");
      if (copy) { openProfileForm("另存为", copy.dataset.copyProfile); return; }
      const del = t.closest("[data-del-profile]");
      if (del) { deleteProfile(del.dataset.delProfile); }
    });
  }
  const addProfile = $("#btn-profile-add");
  if (addProfile && addProfile.dataset.bound !== "1") {
    addProfile.dataset.bound = "1";
    addProfile.addEventListener("click", () => openProfileForm("新增", ""));
  }
  const addProvider = $("#btn-provider-add");
  if (addProvider && addProvider.dataset.bound !== "1") {
    addProvider.dataset.bound = "1";
    addProvider.addEventListener("click", () => openProviderForm("新增", ""));
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

  let html = "<thead><tr><th>名称</th><th>协议</th><th>base_url</th><th>JSON模式</th><th>Key</th>" +
    "<th>模型可选</th><th>操作</th></tr></thead><tbody>";
  (m["providers"] || []).forEach(p => {
    const ok = !!p["已配置"];
    const n = esc(p["名称"]);
    const fallback = p["回退key来源"]
      ? '<div class="muted">运行时会回退 ' + esc(p["回退key来源"]) + "</div>" : "";
    html += "<tr>" +
      "<td><b>" + esc(p["名称"]) + "</b></td>" +
      "<td>" + chip(p["协议"] || "—") + "</td>" +
      '<td class="mono muted wrap">' + esc(p["base_url"] || "—") + "</td>" +
      "<td>" + (p["json_object"] ? chip("支持", "ok") : chip("否", "flat")) + "</td>" +
      "<td>" + (ok ? chip(p["key掩码"], "ok") + '<div class="muted">' + esc(p["key来源"]) + "</div>"
                    : chip("未配置", "bad") + fallback) + "</td>" +
      '<td class="muted wrap">' + esc((p["模型可选"] || []).join(", ")) + "</td>" +
      '<td class="fl-row-act">' +
        '<button class="btn sm ghost" data-edit-provider="' + n + '">编辑</button>' +
        '<button class="btn sm danger" data-del-provider="' + n + '">删除</button>' +
      "</td>" +
      "</tr>";
  });
  const table = $("#provider-table");
  table.innerHTML = html + "</tbody>";
  if (table.dataset.bound !== "1") {
    table.dataset.bound = "1";
    table.addEventListener("click", e => {
      const edit = e.target.closest("[data-edit-provider]");
      if (edit) { openProviderForm("修改", edit.dataset.editProvider); return; }
      const del = e.target.closest("[data-del-provider]");
      if (del) deleteProvider(del.dataset.delProvider);
    });
  }
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
