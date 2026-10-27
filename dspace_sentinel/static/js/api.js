/**
 * api.js — fetch wrappers for the DSpace Sentinel JSON API.
 *
 * Every URL is built relative to `document.baseURI`, so the UI works under a
 * reverse-proxy prefix (`/sentinel/`). All helpers return parsed JSON and throw
 * `ApiError` (with `.status` and `.body`) for non-2xx answers and network errors.
 *
 *   import api, { ApiError, apiUrl } from './api.js';
 *   const status = await api.status();
 *   await api.patchConfig({ appearance: { theme: 'dark' } });
 *
 * Public surface (all async unless noted):
 *   apiUrl(path, params?) -> URL                       (sync) relative URL builder
 *   api.get(path, params?, opts?) / post(path, body?, opts?) / patch(path, body, opts?) / del(path, opts?)
 *   api.meta() · api.status() · api.system() · api.components() · api.component(id)
 *   api.history(metric, range) · api.latency(check, range) · api.availability(range)
 *   api.repository() · api.events({limit, component, kind})
 *   api.alerts(status='open', limit=100) · api.alert(id) · api.ackAlert(id) · api.unackAlert(id)
 *   api.incidents(status='all', limit=100) · api.incident(id)
 *   api.reports() · api.createReport(body) · api.deleteReport(id) · api.restoreReport(id)
 *   api.reportUrl(id) (sync) · api.exportUrl(range='30d') (sync) · api.streamUrl() (sync)
 *   api.config() · api.patchConfig(patch) · api.modules() · api.patchModules({id: bool})
 *   api.checkNow() · api.discover() · api.setup({persona, config_patch}) · api.clearHistory()
 *   api.telegramTest({id}?) · api.emailTest({address}?)   one destination, or every one when the body is omitted
 *   api.session() · api.login({username, password, remember}) · api.logout() · api.changePassword({current, new})
 *   setUnauthorizedHandler(fn)                          (sync) fn(err) runs for a 401 from any call except
 *                                                       api.session, api.login and api.logout (app.js: the sign-in screen)
 *
 * `opts.signal` (AbortSignal) is forwarded to fetch.
 */

/** Error raised for failed requests: `.status` (0 for network errors) and `.body` (parsed JSON or text). */
export class ApiError extends Error {
  constructor(message, status = 0, body = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }
}

/**
 * Build an absolute URL for an API path, relative to the page (reverse-proxy safe).
 * @param {string} path e.g. `'api/status'` (leading slash is stripped)
 * @param {Record<string, any>} [params] query parameters (null/undefined skipped)
 * @returns {URL}
 */
export function apiUrl(path, params) {
  const url = new URL(String(path).replace(/^\/+/, ''), document.baseURI);
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value === null || value === undefined) continue;
      url.searchParams.set(key, String(value));
    }
  }
  return url;
}

/** Calls that answer 401 as part of signing in and out: their callers read it themselves. */
const OWN_401 = new Set(['api/session', 'api/login', 'api/logout']);
let onUnauthorized = null;

/**
 * Run `fn(err)` whenever a call answers 401 (a login is set and this browser's session ended), before
 * the call's own promise rejects with that error. The sign-in calls (OWN_401) are left to their callers.
 * @param {((err: ApiError) => void)|null} fn
 */
export function setUnauthorizedHandler(fn) {
  onUnauthorized = typeof fn === 'function' ? fn : null;
}

async function parseBody(response) {
  const type = response.headers.get('content-type') || '';
  if (type.includes('application/json')) {
    try { return await response.json(); } catch { return null; }
  }
  try { return await response.text(); } catch { return null; }
}

async function request(method, path, { params, body, signal } = {}) {
  const init = { method, headers: { Accept: 'application/json' }, signal, credentials: 'same-origin' };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(apiUrl(path, params), init);
  } catch (err) {
    if (err && err.name === 'AbortError') throw err;
    throw new ApiError(`Sentinel is unreachable (${err && err.message ? err.message : 'network error'})`, 0, null);
  }
  const data = response.status === 204 ? null : await parseBody(response);
  if (!response.ok) {
    const message = data && typeof data === 'object' && data.error ? data.error : `${response.status} ${response.statusText}`;
    const error = new ApiError(String(message), response.status, data);
    if (response.status === 401 && onUnauthorized && !OWN_401.has(String(path).replace(/^\/+/, ''))) {
      try {
        onUnauthorized(error);
      } catch (err) {
        console.error('the 401 handler failed', err);
      }
    }
    throw error;
  }
  return data;
}

const api = {
  get: (path, params, opts = {}) => request('GET', path, { ...opts, params }),
  post: (path, body, opts = {}) => request('POST', path, { ...opts, body }),
  patch: (path, body, opts = {}) => request('PATCH', path, { ...opts, body }),
  del: (path, opts = {}) => request('DELETE', path, opts),

  // status
  meta: (opts) => request('GET', 'api/meta', opts),
  status: (opts) => request('GET', 'api/status', opts),
  system: (opts) => request('GET', 'api/system', opts),
  components: (opts) => request('GET', 'api/components', opts),
  component: (id, opts) => request('GET', `api/components/${encodeURIComponent(id)}`, opts),

  // history
  history: (metric, range = '24h', opts = {}) => request('GET', 'api/history', { ...opts, params: { metric, range } }),
  latency: (check, range = '24h', opts = {}) => request('GET', 'api/history/latency', { ...opts, params: { check, range } }),
  availability: (range = '24h', opts = {}) => request('GET', 'api/availability', { ...opts, params: { range } }),
  repository: (opts) => request('GET', 'api/repository', opts),
  events: ({ limit = 100, component, kind } = {}, opts = {}) =>
    request('GET', 'api/events', { ...opts, params: { limit, component, kind } }),

  // alerts + incidents
  alerts: (status = 'open', limit = 100, opts = {}) => request('GET', 'api/alerts', { ...opts, params: { status, limit } }),
  alert: (id, opts) => request('GET', `api/alerts/${id}`, opts),
  ackAlert: (id, opts) => request('POST', `api/alerts/${id}/ack`, opts),
  unackAlert: (id, opts) => request('POST', `api/alerts/${id}/unack`, opts),
  incidents: (status = 'all', limit = 100, opts = {}) => request('GET', 'api/incidents', { ...opts, params: { status, limit } }),
  incident: (id, opts) => request('GET', `api/incidents/${id}`, opts),

  // reports
  reports: (opts) => request('GET', 'api/reports', opts),
  createReport: (body, opts = {}) => request('POST', 'api/reports', { ...opts, body }),
  deleteReport: (id, opts) => request('DELETE', `api/reports/${id}`, opts),
  restoreReport: (id, opts) => request('POST', `api/reports/${id}/restore`, opts),
  reportUrl: (id) => apiUrl(`api/reports/${id}`),

  // config + modules
  config: (opts) => request('GET', 'api/config', opts),
  patchConfig: (patch, opts = {}) => request('PATCH', 'api/config', { ...opts, body: patch }),
  modules: (opts) => request('GET', 'api/modules', opts),
  patchModules: (body, opts = {}) => request('PATCH', 'api/modules', { ...opts, body }),

  // actions
  checkNow: (opts) => request('POST', 'api/actions/check-now', opts),
  // test messages: `{id}` (Telegram) / `{address}` (email) tests one destination; no body tests every one
  telegramTest: (body, opts = {}) => request('POST', 'api/actions/telegram-test', { ...opts, body }),
  emailTest: (body, opts = {}) => request('POST', 'api/actions/email-test', { ...opts, body }),
  discover: (opts) => request('POST', 'api/actions/discover', opts),
  setup: (body, opts = {}) => request('POST', 'api/actions/setup', { ...opts, body }),
  clearHistory: (opts) => request('POST', 'api/actions/clear-history', opts),

  // the dashboard login (server.login): GET /api/session is always open; an older server answers 404
  session: (opts) => request('GET', 'api/session', opts),
  login: (body, opts = {}) => request('POST', 'api/login', { ...opts, body }),
  logout: (opts) => request('POST', 'api/logout', opts),
  changePassword: (body, opts = {}) => request('POST', 'api/account/password', { ...opts, body }),

  // plain URLs
  exportUrl: (range = '30d') => apiUrl('api/export', { range }),
  streamUrl: () => apiUrl('api/stream'),
};

export { api };
export default api;
