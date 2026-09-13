/* 荐股页筛选区的「模块多选」与「从总控台荐股榜预填」。
 *
 * 状态对象 Pick 与渲染函数由 views/pick.js 注入，避免 ui ← views 的反向依赖；
 * 模块勾选记在 localStorage，切页/重启服务都不丢（无记忆时默认「短线」）。
 */
import { $$ } from "../core/util.js";

export const PICK_MODULE_CYCLE = {
  "短线": "1-5 个交易日", "波段": "2-6 周", "中线": "1-3 个月", "长线": "6 个月以上",
};

const PICK_MOD_KEY = "aiplan.pick.modules";

export function pickModules() {
  return $$("#pick-modules button.on").map(b => b.dataset.module);
}

export function pickSaveModules() {
  try {
    localStorage.setItem(PICK_MOD_KEY, JSON.stringify(pickModules()));
  } catch (e) { /* 隐私模式忽略 */ }
}

/** 按名单勾选模块；空名单或全部非法时回退到第一个模块（短线），保证至少一个。 */
export function pickApplyModules(list) {
  const btns = $$("#pick-modules button");
  if (!btns.length) return;
  const want = (list || []).filter(m => PICK_MODULE_CYCLE[m]);
  btns.forEach(b => b.classList.toggle("on", want.indexOf(b.dataset.module) >= 0));
  if (!btns.some(b => b.classList.contains("on"))) btns[0].classList.add("on");
  pickSaveModules();
}

/** 进入页面时恢复上次勾选。 */
export function pickLoadModules() {
  let saved = null;
  try {
    saved = JSON.parse(localStorage.getItem(PICK_MOD_KEY) || "null");
  } catch (e) { saved = null; }
  if (Array.isArray(saved) && saved.length) pickApplyModules(saved);
  else pickApplyModules(["短线"]);
}

/** 把榜单筛选（模块 / 一级行业+细分 / 概念名）填进筛选区，名字→板块代码靠 /api/pick/boards。
 *  ctx = {state: Pick, loadBoards, renderLists, syncHint}；返回 {行业, 概念} 计数或 null。 */
export function pickPrefill(payload, ctx) {
  const p = payload || {};
  pickApplyModules(p["模块"] || []);
  return ctx.loadBoards(false).then(boards => {
    const l1ByName = {}, conByName = {};
    ((boards || {})["一级行业"] || []).forEach(x => {
      if (x["名称"]) l1ByName[x["名称"]] = x["代码"];
    });
    ((boards || {})["概念"] || []).forEach(x => {
      if (x["名称"]) conByName[x["名称"]] = x["代码"];
    });
    const state = ctx.state;
    state.industry = {};
    (p["行业"] || []).forEach(item => {
      const code = l1ByName[item["名称"]];
      if (!code) return;
      const subs = (item["细分"] || []).filter(Boolean);
      state.industry[code] = { 名称: item["名称"], subs: subs.length ? new Set(subs) : null };
    });
    state.concepts = {};
    (p["概念"] || []).forEach(name => {
      const code = conByName[name];
      if (code) state.concepts[code] = { 名称: name };
    });
    ctx.renderLists();
    ctx.syncHint();
    const ind = Object.keys(state.industry).length, con = Object.keys(state.concepts).length;
    return (ind || con) ? { 行业: ind, 概念: con } : null;
  });
}
