/**
 * app.js — boot, hash router, tab shell, SSE, theme and mood (SPEC §12.3, §12.8).
 *
 * Boot: ask GET /api/session first (an older server answers 404: no login). With a login set and no
 * session, the sign-in screen (screens/signin.js) fills the gate and nothing else loads: no meta, no
 * stream. Otherwise (or once signed in) load /api/meta → onboarding gate (setup not completed) or the
 * shell. The shell opens `EventSource('api/stream')`, falls back to 15 s polling on error and retries
 * SSE when the tab becomes visible again. Every `status` frame updates the store and the mood accent.
 *
 * SIGN-IN: a 401 from any call but the sign-in ones (api.js setUnauthorizedHandler) closes the stream
 *   and shows the sign-in screen with "Your session ended. Sign in again."; signing in renders the
 *   current route again. The sidebar's foot shows the username and Sign out while a login is set.
 *
 * ROUTES (hash): #/home · #/server · #/stack · #/stack/:id · #/history · #/history/incidents/:id ·
 *   #/history/reports/:id · #/more · #/more/:page (alerts|telegram|email|connection|appearance|reports|about|modules,
 *   and account only while a login is set).
 *   A panel is a query on the current route: #/home?panel=report (extra keys are passed as params).
 *   The Report panel is the only right-side panel; the settings forms (alerts, telegram, email, connection …)
 *   are sub-views under #/more/<page>, and a legacy `?panel=alerts|telegram|connection` redirects there.
 *   Depth is capped at two: a tab root, then either one pushed sub-view (‹ Back header, `inert` on the
 *   covered root, focus restored on return) or one right-side panel. Each tab keeps its scroll position.
 *
 * CACHE BUSTING: static assets are cached for an hour, so module URLs carry `?v=<version>`. The
 *   import map in index.html rewrites every static `import` to its versioned URL; dynamic imports
 *   below append the same `v` (read from this module's own URL) so a fresh deploy is never stale.
 *
 * SCREEN CONTRACT — every screen/sub-view/panel module exports:
 *   export function render(root, params, ctx) → void | (() => void) | {destroy()} | Promise<same>
 *   export const title = '…'            (optional static title for the crumb / panel head)
 *   Modules are loaded lazily by path: screens/<tab>.js, screens/detail/server.js, screens/detail/component.js,
 *   screens/detail/incident.js, screens/detail/report.js, screens/more/<page>.js, panels/<name>.js.
 *
 * `params`: {tab, path, segments, id?, page?, query, ...query} — e.g. {tab:'stack', id:'postgres'} for #/stack/postgres.
 * `ctx`:
 *   store, api                      the singletons (also importable directly)
 *   navigate(hash, {replace})       go somewhere ('#/stack/postgres' or '/stack/postgres')
 *   back()                          leave the current sub-view (history.back when we pushed it)
 *   openPanel(name, params)         open panels/<name>.js as a right-side panel (adds ?panel=name to the URL)
 *   closePanel()                    close the open panel
 *   setTitle(text)                  sub-view crumb title ("Stack · PostgreSQL") / panel title
 *   setControls(...nodes)           right-hand page-header controls (root and sub-views)
 *   checkNow()                      run every collector now (shared; buttons read store.checking)
 *   setTheme(theme)                 'system' | 'light' | 'dark' — persisted server-side + localStorage
 *   refreshMeta()                   re-fetch /api/meta into the store (after module or setup changes)
 *   refreshStatus()                 re-fetch /api/status into the store (after an Ack, which changes the mood)
 *   signOut()                       POST /api/logout, then the sign-in screen (More › Account, the sidebar)
 *   route                           the resolved route object
 *   signal                          AbortSignal aborted when the screen is destroyed
 *   panel                           (panels only) the ui.panel handle {el, body, foot, close, setTitle, setBusy}
 *   finish(meta)                    (onboarding only) leave the gate and show the shell
 *   finish(session)                 (sign-in only) the POST /api/login answer: continue the boot
 *
 * Exports for direct use: navigate, back, openPanel, closePanel, checkNow, setTheme, refreshMeta, refreshStatus,
 * signOut, currentRoute, TABS.
 */

import api, { setUnauthorizedHandler } from './api.js';
import { store } from './store.js';
import {
  button, chipRow, el, emptyState, fmt, keepUnits, shortcutsDialog, snackbar, staleWords, panel as uiPanel, currentPanel,
} from './ui.js';

// ------------------------------------------------------------------ constants

export const TABS = [
  { id: 'home', label: 'Home', emoji: '🏠' },
  { id: 'stack', label: 'Stack', emoji: '📚', altLabel: 'Services', altEmoji: '⚙️' },
  { id: 'history', label: 'History', emoji: '📈' },
  { id: 'more', label: 'More', emoji: '⚙️' },
];
const TAB_IDS = TABS.map((t) => t.id);
const MORE_PAGES = ['alerts', 'telegram', 'email', 'connection', 'appearance', 'reports', 'about', 'modules'];
const PANEL_TITLES = { report: 'Generate report' };
/** Panel names that were once planned as drawers and now live as sub-views under More. */
const PANEL_REDIRECTS = new Set(['alerts', 'telegram', 'connection']);
const ASSET_VERSION = (() => {
  try {
    return new URL(import.meta.url).searchParams.get('v') || '';
  } catch {
    return '';
  }
})();
const MODULE_SUFFIX = ASSET_VERSION ? `?v=${encodeURIComponent(ASSET_VERSION)}` : '';
const ROUTES = [
  { match: ['home'], tab: 'home' },
  { match: ['server'], tab: 'home', sub: () => 'detail/server', title: 'Server' },
  { match: ['stack'], tab: 'stack' },
  { match: ['stack', ':id'], tab: 'stack', sub: () => 'detail/component' },
  { match: ['history'], tab: 'history' },
  { match: ['history', 'incidents', ':id'], tab: 'history', sub: () => 'detail/incident', title: (p) => `Incident #${p.id}` },
  { match: ['history', 'reports', ':id'], tab: 'history', sub: () => 'detail/report', title: (p) => `Report #${p.id}` },
  { match: ['more'], tab: 'more' },
  { match: ['more', ':page'], tab: 'more', sub: (p) => (morePage(p.page) ? `more/${p.page}` : null) },
];

/** True while the server has a dashboard login (server.login): sign-in screen, Sign out, More › Account. */
function loginRequired() {
  const session = store.get('session');
  return Boolean(session && session.login_required);
}

/** A page under More; Account exists only while a login is set (elsewhere #/more/account goes Home). */
function morePage(page) {
  return page === 'account' ? loginRequired() : MORE_PAGES.includes(page);
}
const MOOD_ACCENT = { ok: 'mint', warn: 'butter', crit: 'coral', unknown: 'sky' };
const THEME_KEY = 'sentinel.theme';
const POLL_MS = 15000;
const SSE_RETRY_MS = 60000;

// ------------------------------------------------------------------ dom handles

const root = document.documentElement;
const $ = (id) => document.getElementById(id);
const dom = {};

function bindDom() {
  dom.app = $('app');
  dom.gate = $('gate');
  dom.views = $('views');
  dom.pageHead = $('page-head');
  dom.pageTitle = $('page-title');
  dom.pageControls = $('page-controls');
  dom.backBtn = $('back-btn');
  dom.brandHost = $('brand-host');
  dom.version = $('version');
  dom.moodLine = $('mood-line');
  dom.checkNow = $('check-now');
  dom.newReport = $('new-report');
  dom.themeSwitch = $('theme-switch');
  dom.shortcutsBtn = $('shortcuts-btn');
  dom.account = $('account-line');
  dom.accountName = $('account-name');
  dom.signOut = $('sign-out');
  dom.navItems = Array.from(document.querySelectorAll('.nav-item[data-tab]'));
  dom.main = $('main');
  dom.skip = document.querySelector('.skip-link');
}

// ------------------------------------------------------------------ theme + mood

function applyTheme(theme) {
  const t = theme === 'light' || theme === 'dark' ? theme : 'system';
  if (t === 'system') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', t);
  const meta = document.querySelector('meta[name="color-scheme"]');
  if (meta) meta.setAttribute('content', t === 'system' ? 'light dark' : t);
  try {
    if (t === 'system') localStorage.removeItem(THEME_KEY);
    else localStorage.setItem(THEME_KEY, t);
  } catch { /* storage unavailable */ }
  store.set('theme', t);
}

/**
 * Change the theme: applied at once, mirrored in localStorage and persisted via PATCH /api/config.
 * @param {'system'|'light'|'dark'} theme
 */
export async function setTheme(theme) {
  applyTheme(theme);
  try {
    await api.patchConfig({ appearance: { theme } });
  } catch (err) {
    snackbar(`Theme not saved: ${err.message}`);
  }
}

/**
 * Tint the app by health. A status that is out of date (Sentinel unreachable, or no check for 3× the
 * interval) is not health: the accent goes to sky, as before the first check, and `data-stale` on
 * <html> mutes the status colours of the live views until a fresh status arrives.
 */
function setMood(mood) {
  const stale = store.get('stale');
  const name = stale ? 'sky' : MOOD_ACCENT[mood] || 'sky';
  root.style.setProperty('--accent', `var(--${name})`);
  root.dataset.mood = mood || 'unknown';
  if (stale) root.dataset.stale = stale;
  else delete root.dataset.stale;
  // the browser's theme colour follows the accent, read from the token (tokens.css is the one source)
  const meta = document.querySelector('meta[name="theme-color"]');
  const pastel = getComputedStyle(root).getPropertyValue(`--${name}`).trim();
  if (meta && pastel) meta.setAttribute('content', pastel);
}

/** A status older than this (or no answer at all) is shown as out of date. */
const STALE_MIN_S = 45;

function staleAfterS() {
  const meta = store.get('meta');
  const interval = (meta && Number(meta.system_interval)) || 15;
  return Math.max(STALE_MIN_S, 3 * interval);
}

/** Recompute store.stale: 'offline' | 'overdue' | null (before the first status the screens say so themselves). */
function updateStale() {
  let reason = null;
  if (store.get('status')) {
    const ago = store.checkedAgo();
    if (store.get('connection') === 'offline') reason = 'offline';
    else if (ago !== null && ago > staleAfterS()) reason = 'overdue';
  }
  if (store.get('stale') !== reason) store.set('stale', reason);
}

// ------------------------------------------------------------------ router

let currentRouteObj = null;

function parseHash(hash) {
  let raw = (hash || '').replace(/^#/, '');
  if (!raw.startsWith('/')) raw = `/${raw}`;
  const [pathPart, queryPart = ''] = raw.split('?');
  const query = {};
  for (const [k, v] of new URLSearchParams(queryPart)) query[k] = v;
  const segments = pathPart.split('/').filter(Boolean).map((s) => decodeURIComponent(s));
  return { path: `/${segments.join('/')}`, segments, query };
}

function matchRoute(segments) {
  for (const route of ROUTES) {
    if (route.match.length !== segments.length) continue;
    const params = {};
    let ok = true;
    for (let i = 0; i < route.match.length; i += 1) {
      const part = route.match[i];
      if (part.startsWith(':')) params[part.slice(1)] = segments[i];
      else if (part !== segments[i]) { ok = false; break; }
    }
    if (!ok) continue;
    const subModule = route.sub ? route.sub(params) : null;
    if (route.sub && !subModule) return null;
    return { route, params, subModule };
  }
  return null;
}

/** Resolve a hash into a route object (null when unknown). */
export function resolve(hash) {
  const parsed = parseHash(hash);
  const m = matchRoute(parsed.segments);
  if (!m) return null;
  const title = typeof m.route.title === 'function' ? m.route.title(m.params) : m.route.title;
  return {
    hash: `#${parsed.path}${Object.keys(parsed.query).length ? `?${new URLSearchParams(parsed.query)}` : ''}`,
    path: parsed.path,
    tab: m.route.tab,
    segments: parsed.segments,
    params: m.params,
    query: parsed.query,
    sub: m.subModule ? { module: m.subModule, title: title || null } : null,
  };
}

/** The current route object. */
export function currentRoute() {
  return currentRouteObj;
}

function stateDepth() {
  return (history.state && Number(history.state.depth)) || 0;
}

/**
 * Navigate to a hash. Pushes a history entry (or replaces) and renders.
 * @param {string} hash e.g. '#/stack/postgres' (a leading '/' is accepted)
 */
export function navigate(hash, { replace = false } = {}) {
  const normalised = hash.startsWith('#') ? hash : `#${hash.startsWith('/') ? '' : '/'}${hash}`;
  const target = resolve(normalised);
  if (!target) {
    if (normalised !== '#/home') navigate('#/home', { replace: true });
    return;
  }
  // depth counts pushed sub-views on top of the tab root; `panel` marks an entry we pushed for a panel.
  let depth = 0;
  if (target.sub && currentRouteObj && currentRouteObj.tab === target.tab) depth = currentRouteObj.sub ? stateDepth() : stateDepth() + 1;
  else if (target.sub && replace) depth = stateDepth();
  const state = { depth, panel: Boolean(target.query.panel) && !replace };
  if (replace) history.replaceState(state, '', target.hash);
  else history.pushState(state, '', target.hash);
  handleLocation();
}

/** Leave the current sub-view (or panel) — history.back when we pushed it, else a replace. */
export function back() {
  if (!currentRouteObj) return;
  if (currentRouteObj.sub && stateDepth() > 0) {
    history.back();
    return;
  }
  navigate(`#/${currentRouteObj.tab}`, { replace: true });
}

/** True while the onboarding gate (or the boot error) covers the shell: shortcuts and shell actions stay off. */
function gateShowing() {
  return Boolean(dom.gate && !dom.gate.hidden);
}

/** Open panels/<name>.js on top of the current route. */
export function openPanel(name, params = {}) {
  if (gateShowing()) return;
  const base = currentRouteObj ? currentRouteObj.path : '/home';
  const query = new URLSearchParams({ panel: name, ...params });
  navigate(`#${base}?${query}`);
}

/** Close the open panel. */
export function closePanel() {
  const open = currentPanel();
  if (open) open.close('close');
  else stripPanelFromUrl();
}

function stripPanelFromUrl() {
  if (!currentRouteObj || !currentRouteObj.query.panel) return;
  if (history.state && history.state.panel) history.back();
  else navigate(`#${currentRouteObj.path}`, { replace: true });
}

// ------------------------------------------------------------------ views

const scrollPositions = new Map();
const returnFocus = new WeakMap();
let rootView = null; // {tab, el, controller}
let subView = null; // {module, el, controller, params}
let panelView = null; // {name, handle, controller}

function makeController(extra = {}) {
  const abort = new AbortController();
  const ctx = {
    store,
    api,
    navigate,
    back,
    openPanel,
    closePanel,
    checkNow,
    setTheme,
    refreshMeta,
    refreshStatus,
    signOut,
    get route() {
      return currentRouteObj;
    },
    signal: abort.signal,
    setTitle: () => {},
    setControls: () => {},
    ...extra,
  };
  return { ctx, abort, destroyFn: null };
}

async function mount(controller, modulePath, rootEl, params) {
  let mod;
  try {
    mod = await import(`./${modulePath}.js${MODULE_SUFFIX}`);
  } catch (err) {
    console.error(`screen ${modulePath} failed to load`, err);
    if (!controller.abort.signal.aborted) rootEl.replaceChildren(emptyState('🚧', `This screen is not available yet (${modulePath}).`));
    return null;
  }
  if (controller.abort.signal.aborted) return mod;
  if (mod.title && typeof mod.title === 'string') controller.ctx.setTitle(mod.title);
  try {
    const result = await mod.render(rootEl, params, controller.ctx);
    if (typeof result === 'function') controller.destroyFn = result;
    else if (result && typeof result.destroy === 'function') controller.destroyFn = () => result.destroy();
  } catch (err) {
    console.error(`screen ${modulePath} failed to render`, err);
    if (!controller.abort.signal.aborted) rootEl.replaceChildren(emptyState('⚠️', `This screen failed to render — ${err.message}`));
  }
  return mod;
}

function unmount(view) {
  if (!view) return;
  view.controller.abort.abort();
  try {
    if (view.controller.destroyFn) view.controller.destroyFn();
  } catch (err) {
    console.error('destroy failed', err);
  }
  if (view.el) view.el.remove();
}

function tabLabel(tab) {
  const meta = TABS.find((t) => t.id === tab);
  if (!meta) return tab;
  if (tab === 'stack' && !store.dspaceModulesEnabled()) return meta.altLabel;
  return meta.label;
}

function setDocumentTitle(text) {
  document.title = text ? `${text} · DSpace Sentinel` : 'DSpace Sentinel';
}

function paintHeader(route) {
  const isSub = Boolean(route.sub);
  dom.backBtn.hidden = !isSub;
  dom.pageTitle.replaceChildren();
  if (isSub) {
    dom.pageTitle.appendChild(el('span', { class: 'crumb', text: tabLabel(route.tab) }));
    dom.pageTitle.appendChild(el('span', { class: 'crumb-sep', 'aria-hidden': 'true', text: '·' }));
    // the dot is decoration; screen readers hear "Stack: PostgreSQL", not "StackPostgreSQL"
    dom.pageTitle.appendChild(el('span', { class: 'sr-only', text: ': ' }));
    const fallback = route.sub.title || (route.params.id || route.params.page || '').replace(/^\w/, (c) => c.toUpperCase());
    dom.pageTitle.appendChild(el('span', { class: 'page-title-text', text: fallback }));
    setDocumentTitle(fallback);
  } else {
    dom.pageTitle.appendChild(el('span', { class: 'page-title-text', text: tabLabel(route.tab) }));
    setDocumentTitle(tabLabel(route.tab));
  }
}

function setSubTitle(text) {
  const span = dom.pageTitle.querySelector('.page-title-text');
  if (span) span.textContent = text;
  setDocumentTitle(text);
}

function setControls(...nodes) {
  dom.pageControls.replaceChildren(...nodes.flat().filter(Boolean));
}

function paintNav(route) {
  for (const item of dom.navItems) {
    const active = item.dataset.tab === route.tab;
    if (active) item.setAttribute('aria-current', 'page');
    else item.removeAttribute('aria-current');
  }
}

function paintNavLabels() {
  const stackItem = dom.navItems.find((i) => i.dataset.tab === 'stack');
  if (!stackItem) return;
  const dspace = store.dspaceModulesEnabled();
  const meta = TABS[1];
  stackItem.querySelector('.nav-label').textContent = dspace ? meta.label : meta.altLabel;
  stackItem.querySelector('.nav-emoji').textContent = dspace ? meta.emoji : meta.altEmoji;
  stackItem.title = `${dspace ? meta.label : meta.altLabel} (2)`;
  if (currentRouteObj) {
    const crumb = dom.pageTitle.querySelector('.crumb');
    if (crumb && currentRouteObj.tab === 'stack') crumb.textContent = tabLabel('stack');
    if (!currentRouteObj.sub && currentRouteObj.tab === 'stack') dom.pageTitle.querySelector('.page-title-text').textContent = tabLabel('stack');
  }
}

function saveScroll(tab) {
  if (tab && !subView) scrollPositions.set(tab, window.scrollY);
}

function restoreScroll(tab) {
  const y = scrollPositions.get(tab) || 0;
  requestAnimationFrame(() => window.scrollTo({ top: y, left: 0, behavior: 'instant' }));
}

function routeParams(route) {
  return { tab: route.tab, path: route.path, segments: route.segments, ...route.query, ...route.params, query: route.query };
}

async function renderRoot(route) {
  const controller = makeController({ setControls, setTitle: () => {} });
  const viewEl = el('div', { class: 'view view--root', dataset: { tab: route.tab } });
  dom.views.appendChild(viewEl);
  rootView = { tab: route.tab, el: viewEl, controller };
  await mount(controller, `screens/${route.tab}`, viewEl, routeParams(route));
}

async function renderSub(route) {
  const controller = makeController({ setControls, setTitle: setSubTitle });
  const viewEl = el('div', { class: 'view view--sub', dataset: { module: route.sub.module } });
  dom.views.appendChild(viewEl);
  subView = { module: route.sub.module, el: viewEl, controller, key: route.path };
  window.scrollTo({ top: 0, left: 0, behavior: 'instant' });
  await mount(controller, `screens/${route.sub.module}`, viewEl, routeParams(route));
}

function coverRoot() {
  if (!rootView) return;
  scrollPositions.set(rootView.tab, window.scrollY);
  rootView.el.hidden = true;
  rootView.el.inert = true;
}

function uncoverRoot() {
  if (!rootView) return;
  rootView.el.hidden = false;
  rootView.el.inert = false;
  restoreScroll(rootView.tab);
}

async function syncPanel(route) {
  const name = route.query.panel;
  if (!name) {
    if (panelView) {
      const view = panelView;
      panelView = null;
      view.handle.close('route');
    }
    return;
  }
  if (panelView && panelView.name === name) return;
  if (PANEL_REDIRECTS.has(name)) {
    navigate(`#/more/${name}`, { replace: true });
    return;
  }
  if (!PANEL_TITLES[name]) {
    stripPanelFromUrl();
    return;
  }
  const controller = makeController({ setControls: () => {} });
  const handle = uiPanel({
    title: PANEL_TITLES[name],
    name,
    onClose: () => {
      if (panelView && panelView.handle === handle) panelView = null;
      controller.abort.abort();
      try {
        if (controller.destroyFn) controller.destroyFn();
      } catch (err) {
        console.error('panel destroy failed', err);
      }
      if (currentRouteObj && currentRouteObj.query.panel === name) stripPanelFromUrl();
    },
  });
  controller.ctx.panel = handle;
  controller.ctx.setTitle = (text) => handle.setTitle(text);
  panelView = { name, handle, controller };
  const params = { ...routeParams(route), panel: name };
  await mount(controller, `panels/${name}`, handle.body, params);
}

let routing = Promise.resolve();

/** Route the current location; calls are serialized so rapid navigations never interleave. */
function handleLocation() {
  routing = routing.then(processLocation).catch((err) => console.error('routing failed', err));
  return routing;
}

async function processLocation() {
  const hash = location.hash || '#/home';
  const route = resolve(hash);
  if (!route) {
    history.replaceState({ depth: 0 }, '', '#/home');
    return processLocation();
  }
  const prev = currentRouteObj;
  currentRouteObj = route;
  // "home" only while the Home tab itself shows: its Pulse band has Check now, so the sidebar's hides (one per view)
  root.dataset.view = route.sub ? `${route.tab}-sub` : route.tab;
  store.set('route', route);
  paintNav(route);

  const tabChanged = !prev || !rootView || rootView.tab !== route.tab;
  if (tabChanged) {
    if (prev && rootView) saveScroll(prev.tab);
    unmount(subView);
    subView = null;
    unmount(rootView);
    rootView = null;
    paintHeader(route);
    setControls();
    await renderRoot(route);
    if (route.sub) {
      coverRoot();
      await renderSub(route);
    } else {
      restoreScroll(route.tab);
    }
  } else if (route.sub && (!subView || subView.key !== route.path)) {
    const pushing = !subView;
    if (pushing) {
      const active = document.activeElement;
      const keyed = active && active.closest ? active.closest('[data-key]') : null;
      returnFocus.set(rootView.el, { el: active, key: keyed ? keyed.dataset.key : null });
    }
    unmount(subView);
    subView = null;
    paintHeader(route);
    setControls();
    coverRoot();
    await renderSub(route);
    if (pushing) dom.backBtn.focus({ preventScroll: true });
  } else if (!route.sub && subView) {
    unmount(subView);
    subView = null;
    paintHeader(route);
    setControls();
    uncoverRoot();
    const stored = returnFocus.get(rootView.el);
    returnFocus.delete(rootView.el);
    let target = stored && stored.el && stored.el.isConnected ? stored.el : null;
    if (!target && stored && stored.key) target = rootView.el.querySelector(`[data-key="${CSS.escape(stored.key)}"]`);
    if (target && typeof target.focus === 'function') target.focus({ preventScroll: true });
    else dom.pageTitle.focus?.({ preventScroll: true });
  }
  await syncPanel(route);
}

// ------------------------------------------------------------------ status stream

let eventSource = null;
let pollTimer = null;
let retryTimer = null;
let lastModulesKey = '';

function applyStatus(status) {
  if (!status || typeof status !== 'object') return;
  store.patch({ status, statusAt: Date.now() / 1000 });
  updateStale();
  setMood(status.mood);
  paintMoodLine();
  paintNavLabels();
  if (status.hostname && dom.brandHost) dom.brandHost.textContent = status.hostname;
  const key = JSON.stringify(status.modules || {});
  if (lastModulesKey && key !== lastModulesKey) store.emit('modules', status.modules || {});
  lastModulesKey = key;
}

async function poll() {
  if (signInShowing()) return;
  let status;
  try {
    status = await api.status();
  } catch (err) {
    // 401: signed out, not unreachable (the sign-in screen is up and stopped the polling)
    if (err && err.status === 401) return;
    store.set('connection', 'offline');
    paintMoodLine();
    return;
  }
  // back online before the fresh status lands, so it is never painted as out of date
  if (store.get('connection') === 'offline') store.set('connection', 'polling');
  applyStatus(status);
}

function startPolling() {
  if (pollTimer || signInShowing()) return;
  poll();
  pollTimer = setInterval(poll, POLL_MS);
}

function stopPolling() {
  clearInterval(pollTimer);
  pollTimer = null;
}

/** Close the stream and stop polling: while the sign-in screen shows nothing talks to the API. */
function closeStream() {
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
  stopPolling();
  clearTimeout(retryTimer);
}

function safeParse(text) {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function connectStream() {
  if (signInShowing()) return;
  if (eventSource || typeof EventSource === 'undefined') {
    if (!eventSource) startPolling();
    return;
  }
  clearTimeout(retryTimer);
  let es;
  try {
    es = new EventSource(api.streamUrl());
  } catch {
    startPolling();
    return;
  }
  eventSource = es;
  es.addEventListener('status', (e) => {
    const data = safeParse(e.data);
    if (!data) return;
    if (store.get('connection') !== 'live') store.set('connection', 'live');
    stopPolling();
    applyStatus(data);
    store.emit('sse:status', data);
  });
  for (const name of ['alert', 'incident', 'report', 'component']) {
    es.addEventListener(name, (e) => store.emit(`sse:${name}`, safeParse(e.data)));
  }
  es.addEventListener('heartbeat', (e) => store.emit('sse:heartbeat', safeParse(e.data)));
  es.onerror = () => {
    es.close();
    if (eventSource === es) eventSource = null;
    store.set('connection', 'polling');
    startPolling();
    clearTimeout(retryTimer);
    retryTimer = setTimeout(connectStream, SSE_RETRY_MS);
  };
}

function watchVisibility() {
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible') return;
    if (!eventSource) connectStream();
    else poll();
  });
  window.addEventListener('online', () => { if (!eventSource) connectStream(); });
}

// ------------------------------------------------------------------ shared actions

/** Run every enabled collector now; buttons read `store.checking`. */
export async function checkNow() {
  if (store.get('checking') || gateShowing()) return;
  store.set('checking', true);
  try {
    applyStatus(await api.checkNow());
  } catch (err) {
    // no answer at all: say so everywhere now instead of waiting for the next poll
    if (err && err.status === 0) store.set('connection', 'offline');
    snackbar(`Check failed: ${err.message}`);
  } finally {
    store.set('checking', false);
  }
}

/**
 * Re-fetch /api/status into the store at once, after an action that changes what it says: an acknowledged
 * alert no longer colours the mood or the headline (the server decides), and the next check may be minutes
 * away. Failing quietly is fine: the stream or the next poll brings the same answer.
 */
export async function refreshStatus() {
  if (gateShowing()) return;
  try {
    applyStatus(await api.status());
  } catch { /* the next status frame or poll */ }
}

/** Re-fetch /api/meta into the store. */
export async function refreshMeta() {
  try {
    const meta = await api.meta();
    store.set('meta', meta);
    paintNavLabels();
    paintChrome(meta);
    return meta;
  } catch (err) {
    console.error('meta refresh failed', err);
    return store.get('meta');
  }
}

function paintChrome(meta) {
  if (!meta) return;
  if (dom.brandHost) dom.brandHost.textContent = meta.hostname || '';
  if (dom.version) dom.version.textContent = `Sentinel v${meta.version || ''}${meta.demo ? ' · demo' : ''}`;
}

function paintMoodLine() {
  if (!dom.moodLine) return;
  const status = store.get('status');
  const dot = dom.moodLine.querySelector('.dot');
  const mark = dom.moodLine.querySelector('.mood-mark');
  const text = dom.moodLine.querySelector('.mood-text');
  const connection = store.get('connection');
  const stale = store.get('stale');
  dom.moodLine.dataset.state = stale || (!status && connection === 'offline') ? 'stale' : 'live';
  // out of date: a ⚠️ in place of the dot (the only cue left in the 76 px rail, with the text as tooltip)
  dot.hidden = dom.moodLine.dataset.state === 'stale';
  mark.hidden = !dot.hidden;
  if (!status) {
    dot.className = 'dot dot--unknown';
    text.textContent = connection === 'offline' ? 'Sentinel unreachable' : 'Connecting…';
    dom.moodLine.title = text.textContent;
    return;
  }
  if (stale) {
    const words = staleWords(stale, store.lastCheckTs());
    text.textContent = `${words.short} · ${words.last}`;
    dom.moodLine.title = `${words.title} — ${words.last}; showing the last known state`;
    return;
  }
  const mood = status.mood || 'unknown';
  dot.className = `dot dot--${mood}`;
  const short = mood === 'ok' ? 'All healthy' : mood === 'crit' ? 'Critical' : mood === 'warn' ? keepUnits(status.headline) : 'Waiting for checks';
  const ago = store.checkedAgo();
  // short on purpose: "All healthy · 15 s ago" fits the 248 px sidebar on one line
  const agoText = ago === null ? 'first check pending' : formatAgo(ago);
  text.textContent = `${short} · ${agoText}`;
  // healthy with acknowledged alerts: the tooltip keeps the whole headline ("All systems healthy · 1 acknowledged")
  const full = mood === 'ok' && status.headline ? keepUnits(status.headline) : short;
  dom.moodLine.title = `${full} · ${status.subline || agoText}`;
}

function formatAgo(seconds) {
  return fmt.relative(seconds);
}

// ------------------------------------------------------------------ keyboard

function isTyping(target) {
  if (!target || !(target instanceof Element)) return false;
  const tag = target.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || target.isContentEditable;
}

const SHORTCUTS = [
  [['1', '2', '3', '4'], 'Switch tabs'],
  [['n'], 'New report'],
  [['r'], 'Check now'],
  [['Esc'], 'Close panel or go back'],
  [['?'], 'This list'],
  [['←', '→'], 'Move the crosshair on a focused chart'],
];

function bindKeyboard() {
  document.addEventListener('keydown', (e) => {
    if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return;
    // the first-run gate owns the page: no tab switches, panels or checks behind it
    if (gateShowing()) return;
    if (isTyping(e.target)) return;
    const dialogOpen = document.querySelector('dialog[open]');
    if (dialogOpen) return; // dialogs own their keys; Esc closes them natively
    if (e.key >= '1' && e.key <= '4') {
      e.preventDefault();
      navigate(`#/${TAB_IDS[Number(e.key) - 1]}`);
      dom.main.focus({ preventScroll: true });
    } else if (e.key === 'n') {
      e.preventDefault();
      openPanel('report');
    } else if (e.key === 'r') {
      e.preventDefault();
      checkNow();
    } else if (e.key === '?') {
      e.preventDefault();
      shortcutsDialog(SHORTCUTS);
    } else if (e.key === 'Escape' && currentRouteObj && currentRouteObj.sub) {
      e.preventDefault();
      back();
    }
  });
}

// ------------------------------------------------------------------ sign-in (server.login)

const NO_LOGIN = { login_required: false, authenticated: false, username: null, expires_ts: null };
let signInView = null; // the sign-in screen's controller while it fills the gate
let gateController = null; // the onboarding gate's, while it shows
let sessionAsked = null; // a 401 without a known login: the one GET /api/session in flight

function signInShowing() {
  return Boolean(signInView);
}

/** GET /api/session; an older server has no such route (404) and no login. Other failures propagate. */
async function loadSession() {
  try {
    return await api.session();
  } catch (err) {
    if (err && err.status === 404) return { ...NO_LOGIN };
    throw err;
  }
}

/** The username and Sign out at the bottom of the sidebar, while a login is set and this browser is signed in. */
function paintAccount() {
  if (!dom.account) return;
  const session = store.get('session');
  const on = Boolean(session && session.login_required && session.authenticated);
  dom.account.hidden = !on;
  dom.accountName.textContent = on ? session.username || '' : '';
}

/** While the sign-in screen shows, no snackbar: a screen's "Not saved — login required" is no news there. */
function muteSnackbars(muted) {
  const region = document.getElementById('snackbar');
  if (!region) return;
  region.replaceChildren();
  region.hidden = muted;
}

/** The report panel and any open dialog sit in the top layer and would cover the sign-in screen. */
function closeOverlays() {
  for (const dialog of document.querySelectorAll('dialog[open]')) dialog.close();
}

/** After signing in again the current route renders anew: its requests failed while signed out. */
function remountRoute() {
  unmount(subView);
  subView = null;
  unmount(rootView);
  rootView = null;
}

/**
 * Show the sign-in screen in the gate: the stream closes, the shell (or the wizard) hides, nothing else
 * loads. `ended` adds "Your session ended. Sign in again." (a 401 mid-session). Signing in continues the
 * boot: meta, then the onboarding gate or the shell with the current route rendered again.
 */
async function showSignIn({ ended = false } = {}) {
  if (signInView) return;
  const last = store.get('session');
  const controller = makeController({
    finish: async (answer) => {
      if (signInView !== controller) return;
      signInView = null;
      controller.abort.abort();
      // the POST /api/login answer; or a fresh /api/session saying the login was removed meanwhile
      const removed = Boolean(answer && answer.login_required === false);
      store.set('session', removed ? { ...NO_LOGIN, ...answer } : { ...NO_LOGIN, ...(answer || {}), login_required: true, authenticated: true });
      muteSnackbars(false);
      setDocumentTitle('');
      remountRoute();
      await continueBoot();
      if (!dom.app.hidden) requestAnimationFrame(() => dom.pageTitle.focus?.({ preventScroll: true }));
    },
  });
  signInView = controller;
  store.set('session', { ...NO_LOGIN, login_required: true });
  closeStream();
  clearTimeout(bootRetry);
  if (gateController) {
    gateController.abort.abort();
    gateController = null;
  }
  dom.app.hidden = true;
  dom.gate.hidden = false;
  closeOverlays();
  muteSnackbars(true);
  setDocumentTitle('Sign in');
  await mount(controller, 'screens/signin', dom.gate, { tab: 'signin', ended, username: (last && last.username) || '' });
}

/**
 * A call answered 401. With a login set: the session ended, so the sign-in screen says so. Without one
 * when the page loaded, a login may have been set since (./sentinel.sh set-password applies without a
 * restart): ask /api/session once more; a 401 of server.auth_token alone stays the browser's business.
 */
function onUnauthorized() {
  if (signInShowing()) return;
  if (loginRequired()) {
    showSignIn({ ended: true });
    return;
  }
  if (sessionAsked) return;
  sessionAsked = loadSession()
    .then((session) => {
      if (session && session.login_required && !session.authenticated) showSignIn({ ended: true });
    })
    .catch(() => {})
    .finally(() => { sessionAsked = null; });
}

/** Sign out (the sidebar's button, More › Account): POST /api/logout, then the sign-in screen. */
export async function signOut() {
  if (signInShowing()) return;
  if (dom.signOut) dom.signOut.disabled = true;
  try {
    await api.logout();
  } catch (err) {
    // a 401: the session had already ended, which is where Sign out leads anyway
    if (!err || err.status !== 401) {
      snackbar(`Could not sign out — ${err ? err.message : 'unknown error'}`);
      return;
    }
  } finally {
    if (dom.signOut) dom.signOut.disabled = false;
  }
  await showSignIn();
}

// ------------------------------------------------------------------ shell wiring

function bindShell() {
  document.addEventListener('click', (e) => {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const link = e.target instanceof Element ? e.target.closest('a[href^="#/"]') : null;
    if (!link || link.target === '_blank') return;
    e.preventDefault();
    navigate(link.getAttribute('href'));
  });
  window.addEventListener('popstate', () => { if (!gateShowing()) handleLocation(); });
  if (dom.skip) {
    dom.skip.addEventListener('click', (e) => {
      e.preventDefault();
      dom.main.focus({ preventScroll: true });
      dom.main.scrollIntoView({ block: 'start' });
    });
  }
  dom.backBtn.addEventListener('click', back);
  dom.newReport.addEventListener('click', () => openPanel('report'));
  dom.shortcutsBtn.addEventListener('click', () => shortcutsDialog(SHORTCUTS));
  if (dom.signOut) dom.signOut.addEventListener('click', () => signOut());
  store.subscribe('session', paintAccount);
  setUnauthorizedHandler(onUnauthorized);

  const check = dom.checkNow;
  check.addEventListener('click', checkNow);
  store.subscribe('checking', (state) => {
    check.disabled = state.checking;
    check.setAttribute('aria-busy', state.checking ? 'true' : 'false');
    check.textContent = state.checking ? 'Checking…' : '↻ Check now';
  });

  const chips = chipRow({
    options: [
      { value: 'system', label: 'System' },
      { value: 'light', label: 'Light' },
      { value: 'dark', label: 'Dark' },
    ],
    value: store.get('theme'),
    ariaLabel: 'Theme',
    onChange: (value) => setTheme(value),
  });
  dom.themeSwitch.replaceChildren(chips);
  store.subscribe('theme', (state) => chips.set(state.theme, { silent: true }));
  store.subscribe('connection', () => { updateStale(); paintMoodLine(); });
  store.subscribe('stale', () => {
    const status = store.get('status');
    setMood(status ? status.mood : 'unknown');
    paintMoodLine();
  });
  setInterval(() => { updateStale(); paintMoodLine(); }, 1000);
  bindKeyboard();
  watchVisibility();
}

function showShell() {
  dom.gate.hidden = true;
  dom.gate.replaceChildren();
  dom.app.hidden = false;
  paintChrome(store.get('meta'));
  paintNavLabels();
  paintAccount();
  connectStream();
  if (!location.hash) history.replaceState({ depth: 0 }, '', '#/home');
  handleLocation();
}

async function showGate() {
  dom.app.hidden = true;
  dom.gate.hidden = false;
  const controller = makeController({
    finish: async (meta) => {
      // the sign-in screen took the gate over (the session ended during the wizard)
      if (controller.abort.signal.aborted) return;
      if (meta) store.set('meta', meta);
      else await refreshMeta();
      controller.abort.abort();
      if (gateController === controller) gateController = null;
      history.replaceState({ depth: 0 }, '', '#/home');
      showShell();
      requestAnimationFrame(() => dom.pageTitle.focus?.({ preventScroll: true }));
    },
  });
  gateController = controller;
  await mount(controller, 'screens/onboarding', dom.gate, { tab: 'onboarding' });
}

function showBootError(err) {
  dom.app.hidden = true;
  dom.gate.hidden = false;
  const retry = button({ label: 'Try again', variant: 'filled', onClick: () => boot() });
  dom.gate.replaceChildren(emptyState('🛰️', `Sentinel is unreachable — ${err.message}. Retrying every few seconds.`, { action: retry }));
}

let booted = false;
let bootRetry = null;

/** Boot the app (idempotent for retries). */
export async function boot() {
  clearTimeout(bootRetry);
  if (!booted) {
    bindDom();
    bindShell();
    booted = true;
  }
  let stored = null;
  try { stored = localStorage.getItem(THEME_KEY); } catch { /* ignore */ }
  applyTheme(stored || 'system');
  setMood('unknown');
  if (signInShowing()) return;
  let session;
  try {
    session = await loadSession();
  } catch (err) {
    showBootError(err);
    bootRetry = setTimeout(boot, 5000);
    return;
  }
  store.set('session', session);
  // a login is set and this browser has no session: the sign-in screen, and nothing else loads
  if (session.login_required && !session.authenticated) {
    await showSignIn();
    return;
  }
  await continueBoot();
}

/** The rest of the boot, once the session allows it: /api/meta → the onboarding gate or the shell. */
async function continueBoot() {
  let meta;
  try {
    meta = await api.meta();
  } catch (err) {
    // the session ended meanwhile: the 401 handler already put the sign-in screen up
    if (err && err.status === 401 && signInShowing()) return;
    showBootError(err);
    bootRetry = setTimeout(boot, 5000);
    return;
  }
  store.set('meta', meta);
  if (meta.theme) applyTheme(meta.theme);
  if (meta.setup_completed) showShell();
  else await showGate();
}

boot();
