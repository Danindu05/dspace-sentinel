/**
 * store.js — client state + subscribers (SPEC §12.8).
 *
 * One plain object of state, key-based subscriptions and a tiny event bus for
 * transient server-sent events. No framework.
 *
 *   import { store } from './store.js';
 *   store.get('status');                       // current /api/status payload (or null)
 *   const off = store.subscribe('status', (state, changed) => { ... });
 *   store.subscribe(['meta', 'status'], fn);   // several keys
 *   store.subscribe(null, fn);                 // every change
 *   store.patch({ checking: true });           // notifies synchronously
 *   store.on('sse:alert', (data) => { ... });  // transient events (see below)
 *
 * State keys:
 *   meta            /api/meta payload (version, hostname, setup_completed, demo, modules, theme, …)
 *   status          /api/status payload (mood, headline, subline, server, components, alerts_open, repository, …)
 *   statusAt        client clock (seconds) when `status` was received — for ticking "checked … ago"
 *   range           history range chip value: '1h' | '6h' | '24h' | '7d' | '30d' (persisted in localStorage)
 *   theme           'system' | 'light' | 'dark'
 *   connection      'connecting' | 'live' (SSE) | 'polling' | 'offline'
 *   stale           null, or why the shown status is out of date: 'offline' (Sentinel does not answer)
 *                   or 'overdue' (no completed check for 3× the check interval). app.js keeps it current;
 *                   screens show "Sentinel is unreachable — last check 11:34" and mute status colours.
 *   checking        true while POST /api/actions/check-now is in flight
 *   route           the current route object from the router ({tab, path, segments, query, …})
 *   session         /api/session payload {login_required, authenticated, username, expires_ts} (null before
 *                   boot asks); an older server without the route reads as {login_required: false}
 *
 * Events emitted by app.js (`store.emit(name, data)`):
 *   'sse:status' | 'sse:alert' | 'sse:incident' | 'sse:report' | 'sse:component' | 'sse:heartbeat'
 *   'modules'  — after the module set changed (payload: {id: bool})
 *
 * Derived helpers: store.moduleEnabled(id), store.modules(), store.dspaceModulesEnabled(),
 *   store.components(), store.checkedAgo(), store.lastCheckTs(), store.setRange(range).
 */

const RANGES = ['1h', '6h', '24h', '7d', '30d'];
const DSPACE_MODULES = ['postgres', 'solr', 'backend', 'frontend', 'repository', 'assetstore'];
const RANGE_KEY = 'sentinel.range';

function readRange() {
  try {
    const value = localStorage.getItem(RANGE_KEY);
    return RANGES.includes(value) ? value : '24h';
  } catch {
    return '24h';
  }
}

const state = {
  meta: null,
  status: null,
  statusAt: 0,
  range: readRange(),
  theme: 'system',
  connection: 'connecting',
  stale: null,
  checking: false,
  route: null,
  session: null,
};

/** @type {Set<{keys: Set<string>|null, fn: Function}>} */
const subscribers = new Set();
/** @type {Map<string, Set<Function>>} */
const listeners = new Map();

function notify(changed) {
  for (const sub of Array.from(subscribers)) {
    if (sub.keys && !changed.some((key) => sub.keys.has(key))) continue;
    try {
      sub.fn(state, changed);
    } catch (err) {
      console.error('store subscriber failed', err);
    }
  }
}

export const store = {
  /** Read-only view of the whole state object. */
  get state() {
    return state;
  },

  /** @param {string} key */
  get(key) {
    return state[key];
  },

  /** Set one key (no-op when the value is identical). */
  set(key, value) {
    this.patch({ [key]: value });
  },

  /**
   * Merge several keys and notify subscribers once (synchronously).
   * @param {Record<string, any>} partial
   */
  patch(partial) {
    const changed = [];
    for (const [key, value] of Object.entries(partial)) {
      if (state[key] === value) continue;
      state[key] = value;
      changed.push(key);
    }
    if (changed.length) notify(changed);
  },

  /**
   * Subscribe to changes of one key, several keys, or everything (`null`).
   * @param {string|string[]|null} keys
   * @param {(state: object, changed: string[]) => void} fn
   * @returns {() => void} unsubscribe
   */
  subscribe(keys, fn) {
    const set = keys === null || keys === undefined ? null : new Set(Array.isArray(keys) ? keys : [keys]);
    const sub = { keys: set, fn };
    subscribers.add(sub);
    return () => subscribers.delete(sub);
  },

  /**
   * Listen to a transient event (`'sse:alert'`, …).
   * @returns {() => void} unsubscribe
   */
  on(event, fn) {
    if (!listeners.has(event)) listeners.set(event, new Set());
    listeners.get(event).add(fn);
    return () => listeners.get(event)?.delete(fn);
  },

  /** Emit a transient event to `on()` listeners. */
  emit(event, data) {
    const set = listeners.get(event);
    if (!set) return;
    for (const fn of Array.from(set)) {
      try {
        fn(data);
      } catch (err) {
        console.error(`listener for ${event} failed`, err);
      }
    }
  },

  /** Change the history range chip and persist it. */
  setRange(range) {
    if (!RANGES.includes(range)) return;
    try { localStorage.setItem(RANGE_KEY, range); } catch { /* ignore */ }
    this.patch({ range });
  },

  /** The live module map (status frame first, meta as fallback). */
  modules() {
    return (state.status && state.status.modules) || (state.meta && state.meta.modules) || {};
  },

  /** @param {string} id module id */
  moduleEnabled(id) {
    return Boolean(this.modules()[id]);
  },

  /** True when any DSpace module is on (Stack tab) — false renames it to Services. */
  dspaceModulesEnabled() {
    const mods = this.modules();
    return DSPACE_MODULES.some((id) => mods[id]);
  },

  /** Current components (server excluded) from the last status frame. */
  components() {
    return (state.status && state.status.components) || [];
  },

  /** Epoch seconds of the last completed check (`null` before the first). */
  lastCheckTs() {
    const status = state.status;
    if (!status) return null;
    if (status.checked_at) return Number(status.checked_at);
    const ago = this.checkedAgo();
    return ago === null ? null : Date.now() / 1000 - ago;
  },

  /** Seconds since the last completed check, ticking on the client clock; `null` before the first. */
  checkedAgo() {
    const status = state.status;
    if (!status || status.checked_ago_s === null || status.checked_ago_s === undefined) return null;
    const elapsed = state.statusAt ? Date.now() / 1000 - state.statusAt : 0;
    return Math.max(0, status.checked_ago_s + elapsed);
  },
};

export { RANGES, DSPACE_MODULES };
export default store;
