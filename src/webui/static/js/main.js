/* 入口：装配各视图初始化与导航。 */
import { refreshState, showView } from "./core/app.js";
import { bindMobileNav, closeMobileMore, initMobileShell } from "./core/mobile.js";
import { $, $$ } from "./core/util.js";
import { closeModal } from "./ui/modal.js";
import { initConsoleView } from "./views/console.js";
import { initHoldingsView } from "./views/holdings.js";
import { loadMarket } from "./views/market.js";
import { checkModel, saveModels } from "./views/models.js";
import { initPickView } from "./views/pick.js";
import { initReportView, loadLatestReport, refreshReports } from "./views/report.js";
import { initRunView } from "./views/run.js";
import { initFlowView } from "./views/flow.js";
import { initWatchView } from "./views/watch.js";

export function initNav() {
  $$("#nav .nav-item").forEach(b => b.addEventListener("click", () => showView(b.dataset.view)));
  bindMobileNav(showView);
  $("#modal-close").addEventListener("click", closeModal);
  $("#overlay").addEventListener("click", e => { if (e.target.id === "overlay") closeModal(); });
  document.addEventListener("keydown", e => {
    if (e.key !== "Escape") return;
    closeMobileMore();
    closeModal();
  });
  $("#btn-market-reload").addEventListener("click", loadMarket);
  $("#btn-model-check").addEventListener("click", checkModel);
  $("#btn-models-save").addEventListener("click", saveModels);
}

export async function init() {
  initMobileShell();
  initNav();
  initConsoleView();
  initFlowView();
  initRunView();
  initReportView();
  initHoldingsView();
  initWatchView();
  initPickView();
  await refreshState();
  const initialView = showView("console");
  await refreshReports();
  await loadLatestReport();
  await initialView;
}

/* 模块脚本本身就是 defer，正常会在 DOMContentLoaded 之前执行；
   若被动态加载而 DOM 已就绪，就直接初始化。 */
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
else init();
