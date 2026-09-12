/* 入口：装配各视图初始化与导航。 */
import { State, refreshState, showView } from "./core/app.js";
import { $, $$ } from "./core/util.js";
import { closeModal } from "./ui/modal.js";
import { initConsoleView } from "./views/console.js";
import { loadHistory, loadTrash, purgeTrashNow } from "./views/history.js";
import { initHoldingsView } from "./views/holdings.js";
import { loadMarket } from "./views/market.js";
import { checkModel, saveModels } from "./views/models.js";
import { initPickView } from "./views/pick.js";
import { initReportView, loadLatestReport, refreshReports } from "./views/report.js";
import { initRunView } from "./views/run.js";
import { initWatchView } from "./views/watch.js";

export function initNav() {
  $$("#nav .nav-item").forEach(b => b.addEventListener("click", () => showView(b.dataset.view)));
  $("#modal-close").addEventListener("click", closeModal);
  $("#overlay").addEventListener("click", e => { if (e.target.id === "overlay") closeModal(); });
  document.addEventListener("keydown", e => { if (e.key === "Escape") closeModal(); });
  $("#btn-hist-reload").addEventListener("click", loadHistory);
  const trashBtn = $("#btn-trash-reload");
  if (trashBtn) trashBtn.addEventListener("click", loadTrash);
  const trashPurge = $("#btn-trash-purge");
  if (trashPurge) trashPurge.addEventListener("click", purgeTrashNow);
  $("#btn-market-reload").addEventListener("click", loadMarket);
  $("#btn-model-check").addEventListener("click", checkModel);
  $("#btn-models-save").addEventListener("click", saveModels);
}

export async function init() {
  initNav();
  initConsoleView();
  initRunView();
  initReportView();
  initHoldingsView();
  initWatchView();
  initPickView();
  await refreshState();
  await refreshReports();
  await loadLatestReport();
  if (State.report) showView("run");
}

/* 模块脚本本身就是 defer，正常会在 DOMContentLoaded 之前执行；
   若被动态加载而 DOM 已就绪，就直接初始化。 */
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
else init();
