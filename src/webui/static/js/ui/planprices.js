/* 计划价位：把报告的「价格区间」收敛成**一个可执行价位**（报告页与控制台共用同一份规则）。
 *
 * 规则（沿用报告页原有口径）：
 *   · 整段在现价上方 → 取下沿（上破启动）
 *   · 整段在现价下方 → 取上沿（跌破启动）
 *   · 区间含现价     → 取下沿（通常是止损/失效线）
 *   拿不到现价时退回动作类型判断（卖出类取上沿，其余取下沿）。
 */
import { num } from "../core/util.js";


export function planTriggerPrice(rng, act, price) {
  let vals = [];
  if (Array.isArray(rng)) {
    vals = rng.map(num).filter(v => v !== null);
  } else {
    vals = (String(rng == null ? "" : rng).match(/-?\d+(?:\.\d+)?/g) || []).map(Number);
  }
  if (!vals.length) return null;
  if (vals.length === 1) return vals[0];
  const lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
  const cur = num(price);
  if (cur !== null) {
    if (hi < cur) return hi;
    if (lo > cur) return lo;
    return lo;
  }
  const isSell = /减|清|卖|止盈|止损/.test(String(act || ""));
  return isSell ? hi : lo;
}


export function basisFor(v, book) {
  const n = num(v);
  if (n === null || !book.length) return null;
  let best = null, bestD = Infinity;
  book.forEach(b => {
    const d = Math.abs(b.v - n) / Math.max(Math.abs(b.v), 1e-9);
    if (d < bestD) { bestD = d; best = b; }
  });
  return (best && bestD <= 0.02) ? best : null;
}

/* 计划节点（{下沿, 上沿, 动作}）→ 精确价位；总控台卡片用。 */
export function precisePrice(node, price) {
  if (!node) return null;
  const lo = num(node["下沿"]);
  const hi = num(node["上沿"]);
  if (lo === null && hi === null) return null;
  return planTriggerPrice([lo === null ? hi : lo, hi === null ? lo : hi], node["动作"], price);
}


/* 目标位（[{价位,依据}]）→ 现价上方最近的一个；现价高于全部时取最高的那个。 */
export function nearestTarget(goals, price) {
  const rows = (goals || []).map(g => ({ ...g, v: num(g["价位"]) }))
    .filter(g => g.v !== null).sort((a, b) => a.v - b.v);
  if (!rows.length) return null;
  const cur = num(price);
  if (cur === null) return rows[0];
  return rows.find(g => g.v > cur) || rows[rows.length - 1];
}
