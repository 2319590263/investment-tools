/* `/m` 手机专用外壳：路径识别、底部导航、更多面板与移动端写入确认。 */
import { $, $$ } from "./util.js";
import { confirmModal } from "../ui/modal.js";

const MORE_VIEWS = new Set(["history", "watch", "pick", "models", "market"]);

export function initMobileShell() {
  const mobile = /^\/m\/?$/.test(window.location.pathname);
  document.body.dataset.shell = mobile ? "mobile" : "desktop";
  return mobile;
}

export function isMobileShell() {
  return document.body.dataset.shell === "mobile";
}

export function openMobileMore() {
  const sheet = $("#mobile-more");
  const backdrop = $("#mobile-sheet-backdrop");
  const toggle = $("#mobile-more-toggle");
  if (!sheet || !backdrop || !toggle) return;
  sheet.hidden = false;
  backdrop.hidden = false;
  toggle.setAttribute("aria-expanded", "true");
}

export function closeMobileMore() {
  const sheet = $("#mobile-more");
  const backdrop = $("#mobile-sheet-backdrop");
  const toggle = $("#mobile-more-toggle");
  if (!sheet || !backdrop || !toggle) return;
  sheet.hidden = true;
  backdrop.hidden = true;
  toggle.setAttribute("aria-expanded", "false");
}

export function bindMobileNav(showView) {
  $$("#mobile-nav [data-view], #mobile-more [data-view]").forEach(button => {
    button.addEventListener("click", () => showView(button.dataset.view));
  });
  const toggle = $("#mobile-more-toggle");
  if (toggle) toggle.addEventListener("click", openMobileMore);
  const close = $("#mobile-more-close");
  if (close) close.addEventListener("click", closeMobileMore);
  const backdrop = $("#mobile-sheet-backdrop");
  if (backdrop) backdrop.addEventListener("click", closeMobileMore);
}

export function syncMobileNav(name) {
  $$("#mobile-nav [data-view], #mobile-more [data-view]").forEach(button => {
    const active = button.dataset.view === name;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  const more = $("#mobile-more-toggle");
  if (more) more.classList.toggle("active", MORE_VIEWS.has(name));
  closeMobileMore();
}

export function confirmMobileWrite(title, text, onOk, okLabel) {
  if (!isMobileShell()) {
    onOk();
    return;
  }
  confirmModal(title, text, onOk, okLabel || "确认保存");
}
