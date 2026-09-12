/* 确认对话框 / 模态框。 */
import { $, esc } from "../core/util.js";

/* ---------------- 大盘走势预测 ---------------- */
export function confirmModal(title, text, onOk, okLabel) {
  openModal(title, '<div class="muted" style="line-height:1.9">' + esc(text) + "</div>" +
    '<div class="row" style="margin-top:18px;justify-content:flex-end">' +
    '<button class="btn ghost" id="cf-cancel">取消</button>' +
    '<button class="btn danger" id="cf-ok">' + esc(okLabel || "确认") + "</button></div>");
  $("#cf-cancel").addEventListener("click", closeModal);
  $("#cf-ok").addEventListener("click", () => { closeModal(); onOk(); });
}

export function openModal(title, html) {
  $("#modal-title").textContent = title;
  $("#modal-body").innerHTML = html;
  $("#overlay").hidden = false;
}

export function closeModal() { $("#overlay").hidden = true; $("#modal-body").innerHTML = ""; }
