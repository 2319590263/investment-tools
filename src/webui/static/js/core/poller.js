/* 自动刷新定时器：档位 / 启停 / 交易时段判定 / 失败退避 / localStorage 记忆。
 *
 * 设计要点（与后端限速口径配套）：
 *   · 只在交易时段自动跑；非交易时段即使开着也不发请求（手动刷新仍可用）。
 *   · 页面切到后台时暂停，回到前台由调用方 kick() 补一次。
 *   · 连续失败按 2 倍退避，上限 5 分钟；成功一次即回到所选档位。
 */

export const INTERVALS = [
  { ms: 10000, label: "10 秒" },
  { ms: 30000, label: "30 秒" },
  { ms: 60000, label: "1 分钟" },
  { ms: 180000, label: "3 分钟" },
  { ms: 600000, label: "10 分钟" },
];
export const DEFAULT_MS = 30000;
export const MAX_BACKOFF_MS = 300000;                 // 5 分钟
export const TRADING_SESSIONS = ["集合竞价", "盘中（上午）", "盘中（下午）"];
const PREF_KEY = "aiplan.console.auto";

export function loadPref() {
  try {
    const raw = JSON.parse(localStorage.getItem(PREF_KEY) || "{}");
    const ms = INTERVALS.some(i => i.ms === raw.ms) ? raw.ms : DEFAULT_MS;
    return { enabled: !!raw.enabled, ms };
  } catch (e) {
    return { enabled: false, ms: DEFAULT_MS };
  }
}

export function savePref(pref) {
  try { localStorage.setItem(PREF_KEY, JSON.stringify(pref)); } catch (e) { /* 隐私模式下忽略 */ }
}

export function isTradingSession(name, tradingDay) {
  return !!tradingDay && TRADING_SESSIONS.indexOf(String(name || "")) >= 0;
}

export function createPoller(opts) {
  const run = opts.run;
  const onState = opts.onState;
  const state = {
    enabled: false, ms: DEFAULT_MS, failures: 0, timer: null,
    sessionName: "", tradingDay: false,
    lastAt: null, nextAt: null, running: false,
  };

  function pauseReason() {
    if (!state.enabled) return "";
    if (!isTradingSession(state.sessionName, state.tradingDay)) {
      return "非交易时段（" + (state.sessionName || "未知") + "）";
    }
    if (typeof document !== "undefined" && document.hidden) return "页面在后台";
    return "";
  }

  function emit() {
    if (onState) onState({ ...state, pauseReason: pauseReason() });
  }

  function clearTimer() {
    if (state.timer) { clearTimeout(state.timer); state.timer = null; }
  }

  function delay() {
    return Math.min(state.ms * Math.pow(2, state.failures), MAX_BACKOFF_MS);
  }

  function schedule() {
    clearTimer();
    const reason = pauseReason();
    if (!state.enabled || reason) { state.nextAt = null; emit(); return; }
    const wait = delay();
    state.nextAt = new Date(Date.now() + wait);
    state.timer = setTimeout(() => tick(false), wait);
    emit();
  }

  async function tick(manual) {
    if (state.running) return;
    if (!manual && pauseReason()) { schedule(); return; }
    state.running = true;
    try {
      const ok = await run();
      state.failures = ok === false ? state.failures + 1 : 0;
      state.lastAt = new Date();
    } catch (e) {
      state.failures += 1;
    } finally {
      state.running = false;
      schedule();
    }
  }

  return {
    state,
    pauseReason,
    setSession(name, tradingDay) {
      const changed = state.sessionName !== name || state.tradingDay !== tradingDay;
      state.sessionName = name || "";
      state.tradingDay = !!tradingDay;
      if (changed) schedule();
    },
    setEnabled(on) {
      state.enabled = !!on;
      state.failures = 0;
      if (state.enabled) tick(true); else schedule();
    },
    setIntervalMs(ms) {
      state.ms = ms;
      state.failures = 0;
      schedule();
    },
    kick() { if (state.enabled) tick(true); },
    stop() { state.enabled = false; clearTimer(); emit(); },
  };
}
