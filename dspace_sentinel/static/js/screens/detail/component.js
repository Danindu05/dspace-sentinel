/**
 * screens/detail/component.js — #/stack/:id, one service in depth (SPEC §12.5).
 *
 * Ladder (static header; a rung's `detail.hint` as a muted line under its message) beside the "Right now"
 * metric tiles (connections meter, DB size, version, Solr cores table …) → response-time chart for the last
 * 24 hours (/api/history/latency) beside the "Last error" and "If this fails" tiles → recent events table
 * beside the "Connection used" tile (secrets masked). Status frames patch the ladder and the metrics in
 * place (the tiles keep the detail payload's settings, which a frame does not carry); the detail payload
 * (events, config) reloads on component/alert events for this service and every minute with the chart.
 * Security (detail/security.js) lays its ladder and its tiles out at the page's full width, one above the
 * other: two wide tables (open ports, software) and the rungs' long hints need the room.
 */
import api from '../../api.js';
import { lineChart } from '../../charts.js';
import { store } from '../../store.js';
import {
  chartTile, el, emptyState, fmt, grid, humanize, keepUnits, meter, section, staleWords, statusLabel, statusPill, table,
  tile,
} from '../../ui.js';
import { isInformational, isKnownIssue, ladder, latencyText } from '../stack.js';
import { securityTiles } from './security.js';

const REFRESH_MS = 60000;
const HEADLINE_CHECK = { postgres: 'postgres.query', solr: 'solr.search', backend: 'backend.rest', frontend: 'frontend.home', nginx: 'nginx.http' };
const CHECK_TITLE = {
  'postgres.query': 'SELECT 1', 'solr.search': 'Search query', 'backend.rest': 'GET /server/api',
  'backend.search': 'Search API', 'frontend.home': 'Homepage', 'nginx.http': 'HTTP',
};
const SECRET_KEYS = new Set(['password', 'auth_token', 'bot_token']);
const KEY_LABELS = {
  host: 'Host', port: 'Port', database: 'Database', user: 'User', password: 'Password', sslmode: 'SSL mode',
  service: 'systemd unit', url: 'URL', cores: 'Cores', server_url: 'REST API URL', ui_url: 'UI URL',
  public_url: 'Public URL', install_dir: 'Install directory', assetstore_dir: 'Assetstore directory',
  log_dir: 'Log directory', backend_service: 'Backend unit', frontend_service: 'Frontend unit',
  verify_tls: 'Verify TLS', http_url: 'HTTP URL', https_url: 'HTTPS URL', window_minutes: 'Window',
};
/** Which config keys the "Connection used" tile shows per component (null = every key of that slice). */
const CONFIG_KEYS = {
  postgres: [['postgres', ['host', 'port', 'database', 'user', 'password', 'sslmode', 'service']]],
  solr: [['solr', ['url', 'cores', 'service']]],
  backend: [['dspace', ['server_url', 'backend_service', 'install_dir', 'verify_tls']]],
  frontend: [['dspace', ['ui_url', 'public_url', 'frontend_service']]],
  nginx: [['nginx', ['service', 'http_url', 'https_url']]],
  ssl: [['ssl', ['host', 'port']], ['dspace', ['public_url']]],
  ports: [['ports', null]],
  logs: [['logs', null]],
  repository: [['postgres', ['host', 'port', 'database', 'user']], ['dspace', ['server_url']]],
  assetstore: [['dspace', ['assetstore_dir', 'install_dir']]],
  security: [['ports', null], ['dspace', ['public_url', 'install_dir']]],
};
const CONNECTION_TITLE = { ports: 'Rules used', logs: 'Files watched', assetstore: 'Settings used', security: 'Settings used' };
/** What a component reads from sentinel.yaml when the API sends none of it (instead of "Nothing configured"). */
const SETTINGS_NOTE = {
  security: 'Reads the port rules, dspace.public_url and dspace.install_dir from sentinel.yaml.',
};
/** A hint that starts with a command ("sudo apt update && …") keeps its first letter as it is. */
const COMMAND_START = /^(sudo|chmod|chown|chgrp|systemctl|apt|ufw)\s/;
/** DSpace's health statuses (/server/actuator/health) in the operator's words. */
const ACTUATOR_WORDS = {
  UP: 'Up', UP_WITH_ISSUES: 'Up, with non-blocking issues', DOWN: 'Down', OUT_OF_SERVICE: 'Out of service', UNKNOWN: 'Unknown',
};
/** How one visible actuator component reads in its table (DSpace's own health page marks UP_WITH_ISSUES as a warning). */
const ACTUATOR_STATUS = { UP: 'ok', UP_WITH_ISSUES: 'warn', DOWN: 'crit', OUT_OF_SERVICE: 'crit', UNKNOWN: 'unknown' };
/** Anonymously DSpace shows the overall status only (the backend's ADMIN_LOGIN_NOTE). */
const ADMIN_LOGIN_NOTE = 'details need a DSpace admin login';
/** The backend reuses a healthy actuator answer for ACTUATOR_MIN_INTERVAL_S (300 s): every call runs all of DSpace's health checks. */
const ACTUATOR_ASKED = 'asked at most every 5 minutes';
/** It never reuses a warning (a DOWN, an error, a time-out): the next check asks again. */
const ACTUATOR_ASKED_AGAIN = 'asked again at the next check';
/** Event kinds in the operator's words (the raw ids — notify.demo, component, config — are internal). */
const KIND_WORDS = {
  alert: 'Alert', component: 'Status change', incident: 'Incident', config: 'Setting', report: 'Report',
  demo: 'Demo', error: 'Error',
};
/** A delivery row names its channel: "Telegram", "Email (demo)", "Telegram and email (demo)", "Webhook (failed)". */
const NOTIFY_SUFFIX = { notify: '', 'notify.demo': ' (demo)', 'notify.error': ' (failed)', 'notify.skipped': ' (held back)' };
const CHANNEL_WORDS = { telegram: 'Telegram', email: 'email', webhook: 'webhook' };

function kindWord(kind, data) {
  const k = String(kind || '');
  if (k in NOTIFY_SUFFIX) {
    // data.channel on sent/failed/held-back rows, data.channels on demo rows; rows from before email were Telegram
    const ids = data && data.channel ? [data.channel] : (data && Array.isArray(data.channels) && data.channels.length ? data.channels : ['telegram']);
    const names = ids.map((id) => CHANNEL_WORDS[id] || String(id));
    const joined = names.length > 1 ? `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}` : names[0];
    return `${joined.charAt(0).toUpperCase()}${joined.slice(1)}${NOTIFY_SUFFIX[k]}`;
  }
  return KIND_WORDS[k] || (k ? (k.charAt(0).toUpperCase() + k.slice(1)).replace(/[._]/g, ' ') : '—');
}

function thresholdStatus(value, th, inverted = false) {
  if (!th || value === null || value === undefined) return 'ok';
  const v = Number(value);
  if (inverted) return v <= th.critical ? 'crit' : v <= th.warning ? 'warn' : 'ok';
  return v >= th.critical ? 'crit' : v >= th.warning ? 'warn' : 'ok';
}

function sinceText(ts) {
  if (ts === null || ts === undefined) return '';
  const age = Date.now() / 1000 - Number(ts);
  return age < 86400 ? fmt.clock(ts) : fmt.dateTime(ts);
}

function humanKey(key) {
  return KEY_LABELS[key] || String(key).replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase());
}

function configValue(key, value) {
  if (SECRET_KEYS.has(key)) return value ? '••••••' : 'not set';
  if (value === '' || value === null || value === undefined) return 'not set';
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (Array.isArray(value)) return value.length ? value.map(String).join(', ') : 'none';
  return String(value);
}

function kv(pairs) {
  const dl = el('dl', 'kv');
  for (const [k, v, numeric] of pairs) {
    dl.appendChild(el('dt', { text: k }));
    dl.appendChild(el('dd', { class: numeric ? 'numeric' : '', text: v }));
  }
  return dl;
}

/** The rows of the "Connection used" tile from the masked `config_used` slices. */
function configPairs(id, configUsed) {
  const spec = CONFIG_KEYS[id];
  const pairs = [];
  const slices = spec || Object.keys(configUsed || {}).map((k) => [k, null]);
  for (const [sliceName, keys] of slices) {
    const slice = configUsed ? configUsed[sliceName] : undefined;
    if (slice === undefined || slice === null) continue;
    if (sliceName === 'ports' && Array.isArray(slice)) {
      for (const rule of slice) pairs.push([`${rule.label || 'Port'} :${rule.port}`, rule.expect === 'local_only' ? 'local only' : 'should be open']);
      continue;
    }
    if (sliceName === 'logs' && slice && typeof slice === 'object') {
      for (const f of slice.files || []) pairs.push([f.label || f.id, `${f.path}${f.daily_suffix ? ' (daily files)' : ''}`]);
      if (slice.window_minutes) pairs.push(['Window', `${fmt.plural(slice.window_minutes, 'minute')}`, true]);
      continue;
    }
    if (typeof slice !== 'object') {
      pairs.push([humanKey(sliceName), configValue(sliceName, slice)]);
      continue;
    }
    for (const key of keys || Object.keys(slice)) {
      if (!(key in slice)) continue;
      const value = slice[key];
      if (value && typeof value === 'object' && !Array.isArray(value)) continue;
      pairs.push([humanKey(key), configValue(key, value), typeof value === 'number']);
    }
  }
  return pairs;
}

/** A tile with content stacked under its title row; `lines` are extra subtitle lines inside the text column. */
function blockTile({ emoji, title, subtitle, lines, className = '' }, ...children) {
  const node = tile({ emoji, title, subtitle, children: (lines || []).filter(Boolean), className: `tile--block ${className}`.trim() });
  node.append(...children.filter(Boolean));
  return node;
}

/** The tone class of a subtitle line: warn/crit colour it like the ladder does. */
function toneClass(status) {
  return status === 'crit' ? ' tile-sub--crit' : status === 'warn' ? ' tile-sub--warn' : '';
}

/** One subtitle line; `status` warn/crit colours it like the ladder does. */
function subLine(text, status) {
  return el('span', { class: `tile-sub${toneClass(status)}`, text });
}

/**
 * `text` as nodes with a line-break opportunity (<wbr>) after every dot, slash and colon, so a host name or a
 * path in a narrow tile wraps at its parts ("repository.example.|edu:|443") rather than inside a word.
 */
function breakable(text) {
  const out = [];
  let chunk = '';
  for (const ch of String(text ?? '')) {
    chunk += ch;
    if (ch === '.' || ch === '/' || ch === ':') {
      out.push(chunk, el('wbr'));
      chunk = '';
    }
  }
  if (chunk) out.push(chunk);
  return out;
}

function capitalize(text) {
  const s = String(text ?? '');
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

function meterTile({ emoji, title, subtitle, value, max, status, label, ariaLabel }) {
  const bar = meter({ value: Number(value) || 0, max: Number(max) || 100, status, label, ariaLabel: ariaLabel || title });
  return blockTile({ emoji, title, subtitle, className: 'span-full' }, el('div', 'meter-list', bar));
}

function pillCell(status, text) {
  return statusPill(status, text);
}

// ------------------------------------------------------------------ ladder: hints

/**
 * A hint's text as nodes: a command between backticks ("`sudo ufw enable`") in the monospace face, without
 * the backticks.
 */
function hintNodes(text) {
  return text.split(/`([^`]+)`/).map((part, i) => (i % 2 ? el('span', { class: 'mono', text: part }) : part)).filter((part) => part !== '');
}

/**
 * A rung's hint (`detail.hint`: one or two plain sentences on what the situation is and what to do, such
 * as which group lets Sentinel read a log file) as a muted line under the rung's message, in sentence case
 * (a Solr core's fix is worded to follow "Fix: " in the Cores tile), unless it starts with a command
 * ("sudo apt update && sudo apt upgrade, …"); a command between backticks reads in the monospace face. The
 * detail page alone shows it (the Stack tab's ladders stay short); the shared ladder patches its rungs in
 * place or rebuilds them, so this runs after every paint of the ladder.
 */
function paintHints(ladderEl, comp) {
  const hints = new Map();
  for (const r of comp.chain || []) {
    const hint = r && r.detail && typeof r.detail.hint === 'string' ? r.detail.hint.trim() : '';
    if (hint) hints.set(String(r.id), keepUnits(COMMAND_START.test(hint) ? hint : capitalize(hint)));
  }
  for (const li of ladderEl.querySelectorAll('.rung[data-rung]')) {
    const main = li.querySelector('.rung-main');
    if (!main) continue;
    const text = hints.get(li.dataset.rung) || '';
    let line = main.querySelector('.rung-hint');
    if (!text) {
      if (line) line.remove();
      continue;
    }
    if (!line) {
      line = el('span', { class: 'rung-hint' });
      main.append(line);
    }
    if (line.dataset.hint !== text) {
      line.replaceChildren(...hintNodes(text));
      line.dataset.hint = text;
    }
  }
}

// ------------------------------------------------------------------ backend: the version

/**
 * The backend's DSpace tile: the version the REST API reports ("9.4"), its major line and a snapshot build
 * under it ("DSpace 9.x · snapshot build"), DSpace-CRIS when the root names it, and a quiet note on 7.x,
 * which gets no more releases (a fact to plan with, not a warning).
 */
function dspaceTile(m) {
  const major = m.dspace_major === null || m.dspace_major === undefined || m.dspace_major === '' ? null : Number(m.dspace_major);
  const known = major !== null && Number.isFinite(major);
  const under = [known ? `DSpace ${major}.x` : '', m.dspace_snapshot ? 'snapshot build' : ''].filter(Boolean).join(' · ');
  const lines = [];
  if (m.cris_version) lines.push(subLine(`DSpace-CRIS ${m.cris_version}`));
  if (known && major === 7) lines.push(subLine('7.x is end of life — 7.6.7 was the final release'));
  return tile({
    emoji: '🏷️', title: 'DSpace', subtitle: m.dspace_name || 'version reported by the REST API',
    value: m.dspace_version || '—', valueSub: under || undefined, children: lines, className: 'span-full',
  });
}

// ------------------------------------------------------------------ backend: the actuator

/** "UP_WITH_ISSUES" → "Up, with non-blocking issues"; a status DSpace adds later reads "Some status". */
function actuatorWords(raw) {
  const key = String(raw ?? '').trim().toUpperCase();
  if (!key) return '';
  return ACTUATOR_WORDS[key] || capitalize(key.replace(/_/g, ' ').toLowerCase());
}

/**
 * True when the rung's message only repeats the status words: "UP", "up, with non-blocking issues", or,
 * beside the components table that names them, "status DOWN (solrSearchCore)".
 */
function repeatsStatus(message, words, withTable) {
  const flat = (s) => String(s ?? '').trim().toLowerCase().replace(/_/g, ' ').replace(/^status\s+/, '');
  const msg = flat(message);
  const said = flat(words);
  return !msg || msg === said || (withTable && msg.startsWith(said));
}

/**
 * "checked 3 m ago · asked at most every 5 minutes" for an actuator answer given at `ts` (epoch seconds);
 * `note` is when it is asked again (ACTUATOR_ASKED, or ACTUATOR_ASKED_AGAIN under a warning).
 */
function askedText(ts, note) {
  return `checked ${fmt.relative(Date.now() / 1000 - Number(ts))} · ${note || ACTUATOR_ASKED}`;
}

/**
 * The backend's "Actuator health" tile. Anonymously DSpace answers with its overall status only, so the tile
 * says that status in words (metrics.actuator_status, toned like the rung; a known issue stays muted), the
 * rung's message when it says more ("status DOWN (details need a DSpace admin login)", the SEO known issue),
 * how old the answer is and when it is asked again (the backend reuses a healthy or informational one for
 * five minutes, detail.cached_age_s, and asks again at the next check after a warning), and the components
 * table when DSpace shows them (an admin view). "Not exposed (HTTP 404)" only when the rung says so.
 */
function actuatorTile(comp, m) {
  const rung = (comp.chain || []).find((r) => r.id === 'backend.actuator') || null;
  const d = (rung && rung.detail) || {};
  const visible = (obj) => (obj && typeof obj === 'object' && !Array.isArray(obj) ? obj : {});
  const comps = Object.entries(Object.keys(visible(m.actuator_components)).length ? visible(m.actuator_components) : visible(d.components));
  const raw = String(m.actuator_status || d.health_status || '').trim().toUpperCase();
  const words = actuatorWords(raw);
  const known = isKnownIssue(rung);
  const tone = rung && !known ? rung.status : 'ok';
  const message = rung && rung.message ? humanize(String(rung.message)) : '';
  let head;
  if (words) head = known ? `${words} · known issue` : words;
  else if (message) head = capitalize(message); // "Not exposed (HTTP 404)", "HTTP 500", "Not reached"
  else head = rung ? statusLabel(rung.status) : 'Not checked yet';
  const lines = [];
  if (words && !repeatsStatus(rung ? rung.message : '', words, comps.length > 0)) lines.push(subLine(message, tone));
  else if (words && !comps.length && raw !== 'UP' && !known) lines.push(subLine(ADMIN_LOGIN_NOTE, tone));
  // only an answer that was asked for has an age ("not reached" was never sent); paintTick keeps it current
  const asked = rung && rung.ts && String(rung.message || '').trim() !== 'not reached'
    ? Number(rung.ts) - (Number(d.cached_age_s) || 0) : null;
  // the collector keeps an answer only while its rung reads ok or informational
  const note = rung && (rung.status === 'ok' || isInformational(rung)) ? ACTUATOR_ASKED : ACTUATOR_ASKED_AGAIN;
  if (asked !== null) lines.push(el('span', { class: 'tile-sub', dataset: { askedAt: String(asked), askedNote: note }, text: askedText(asked, note) }));
  return blockTile({ emoji: '❤️', title: 'Actuator health', subtitle: subLine(head, tone), lines, className: 'span-full' },
    comps.length ? table({
      ariaLabel: 'Actuator components',
      columns: [
        { key: 'name', label: 'Component' },
        { key: 'status', label: 'Status', format: (v) => pillCell(ACTUATOR_STATUS[String(v).toUpperCase()] || 'unknown', actuatorWords(v) || 'Unknown') },
      ],
      rows: comps.map(([name, status]) => ({ name, status: typeof status === 'object' && status ? status.status || 'UNKNOWN' : status })),
    }) : null);
}

// ------------------------------------------------------------------ solr: cores

/**
 * The Solr detail's Cores tile: a row per core, the optional DSpace cores (qaevent, suggestion, audit; a
 * problem with one is at most a warning) tagged "optional", a core Solr could not load (metrics.init_failures)
 * reading "Failed", and under the table why ("search failed to load: …"), with the fix when the core rung
 * knows it (detail.hint). A core whose reload failed (metrics.reload_failures) still serves with its previous
 * configuration while its ping answers (its rung reads "reload failed: …"): its row keeps its own status, and
 * the note under the table says a restart would not load it. When its ping fails too (the rung is the ping's
 * problem), the note says only that a restart would not load it either. The subtitle counts, and the notes
 * colour, only the table's cores: Solr may also hold cores Sentinel does not ping (another application's),
 * whose notes stay plain.
 */
function coresTile(comp, m) {
  const cores = Array.isArray(m.cores) ? m.cores : [];
  const asMap = (obj) => (obj && typeof obj === 'object' && !Array.isArray(obj) ? obj : {});
  const failures = asMap(m.init_failures);
  const reloads = asMap(m.reload_failures);
  const failed = (name) => Object.prototype.hasOwnProperty.call(failures, name);
  const rungs = new Map((comp.chain || []).map((r) => [r.id, r]));
  const listed = new Set(cores.map((c) => c.name));
  const failedHere = cores.filter((c) => failed(c.name));
  const reloadNames = Object.keys(reloads);
  const reloadedHere = reloadNames.filter((name) => listed.has(name));
  const counts = [
    failedHere.length ? `${failedHere.length} failed to load` : '',
    reloadedHere.length ? `${reloadedHere.length} failed to reload` : '',
  ].filter(Boolean).map((text) => ` · ${text}`).join('');
  const subtitle = `${fmt.plural(cores.length - failedHere.length, 'core')} pinged every check${counts}`;
  const hintFor = (name) => ((rungs.get(`solr.core.${name}`) || {}).detail || {}).hint;
  // the old core serves only while its ping answers (a core without a rung is not pinged: Solr keeps it)
  const serving = (name) => {
    const rung = rungs.get(`solr.core.${name}`);
    return !rung || String(rung.message || '').startsWith('reload failed');
  };
  // the table's failed cores first (a failed optional core is a warning), then any other core Solr could not load
  const notes = [...failedHere.map((c) => c.name), ...Object.keys(failures).filter((name) => !listed.has(name))].map((name) => {
    const row = cores.find((c) => c.name === name);
    const hint = hintFor(name);
    return el('li', null,
      subLine(`${name} failed to load: ${failures[name]}`, row ? (row.status === 'warn' ? 'warn' : 'crit') : ''),
      hint ? subLine(`Fix: ${hint}`) : null);
  });
  // then the cores whose reload failed, the table's first: Solr kept the old core, so they still answer until
  // the next restart
  for (const name of [...reloadedHere, ...reloadNames.filter((other) => !listed.has(other))]) {
    const row = cores.find((c) => c.name === name);
    const hint = hintFor(name);
    notes.push(el('li', null,
      subLine(`${name} failed to reload: ${reloads[name]}`, row ? (row.status === 'crit' ? 'crit' : 'warn') : ''),
      subLine(serving(name) ? 'Still serving with its previous configuration; a restart would not load it.' : 'A restart would not load it either.'),
      hint ? subLine(`Fix: ${hint}`) : null));
  }
  return blockTile({ emoji: '🗂️', title: 'Cores', subtitle, className: 'span-full' },
    table({
      ariaLabel: 'Solr cores',
      columns: [
        // the tag sits beside the name, or under it when the column is narrow (1280 px)
        { key: 'name', label: 'Core', format: (v, row) => (row.optional ? el('span', 'name-tag', el('span', { text: String(v) }), el('span', { class: 'tag', text: 'optional' })) : String(v)) },
        { key: 'docs', label: 'Docs', align: 'end', numeric: true, format: (v) => fmt.number(v) },
        { key: 'size_bytes', label: 'Size', align: 'end', numeric: true, format: (v) => fmt.bytes(v) },
        // "Failed" (why is under the table): "Failed to load" would make the table scroll at 1280 px
        { key: 'status', label: 'Status', format: (v, row) => (failed(row.name) ? pillCell(v === 'warn' ? 'warn' : 'crit', 'Failed') : pillCell(v)) },
      ],
      rows: cores,
      emptyText: 'Cores appear after the first check.',
    }),
    notes.length ? el('ul', { class: 'note-list', 'aria-label': notesLabel(notes.length - reloadNames.length, reloadNames.length) }, ...notes) : null);
}

/** The accessible name of the list under the cores table: which kind of failure it lists. */
function notesLabel(notLoaded, notReloaded) {
  if (notLoaded && notReloaded) return 'Cores that failed to load or reload';
  return notReloaded ? 'Cores that failed to reload' : 'Cores that failed to load';
}

// ------------------------------------------------------------------ frontend: the public URL

/** The configured `dspace.public_url` from the detail payload's settings ('' when empty); null without them. */
function publicUrlSetting(comp) {
  const dspace = comp.config_used && comp.config_used.dspace;
  if (!dspace || typeof dspace !== 'object' || !('public_url' in dspace)) return null;
  return String(dspace.public_url ?? '').trim();
}

/**
 * The frontend's Public URL tile. "not set — add it in More › DSpace connection" only when the setting is
 * empty (`config_used.dspace.public_url`); a configured URL without a public rung (before the first check,
 * after a failed frontend check, until a check has run with a newly saved URL) reads "not checked yet"; a
 * public rung that is not ok says why, in its tone ("connection refused (repository.example.edu:443)"),
 * since the collector reports no latency for a failed request. Without the settings (a status frame painted
 * before the detail payload arrived) the last check decides, as it did before.
 */
function publicUrlTile(comp, m) {
  const rung = (comp.chain || []).find((r) => r.id === 'frontend.public') || null;
  const setting = publicUrlSetting(comp);
  const configured = setting === null ? Boolean(m.public_url || rung) : setting !== '';
  if (!configured) {
    return tile({ emoji: '🌍', title: 'Public URL', subtitle: 'not set — add it in More › DSpace connection', value: '—' });
  }
  const value = latencyText(m.public_latency_ms) || '—';
  let subtitle = 'through nginx and TLS, as visitors see it';
  if (rung && rung.status !== 'ok') {
    const why = humanize(String(rung.message || statusLabel(rung.status)));
    subtitle = el('span', { class: `tile-sub${toneClass(rung.status)}` }, ...breakable(why));
  }
  else if (!rung) subtitle = 'not checked yet';
  return tile({ emoji: '🌍', title: 'Public URL', subtitle, value });
}

// ------------------------------------------------------------------ frontend: rendering

/** "localhost:4000" from "http://localhost:4000/" (null when it is not a URL). */
function urlHost(url) {
  try {
    return url ? new URL(String(url)).host : null;
  } catch {
    return null;
  }
}

/**
 * The frontend's Rendering tile: did the homepage arrive rendered on the server (SSR) or as the client-side
 * shell (with the rung's note on why), which Generator and Angular versions the page names, and the Host
 * header Sentinel sent (the public URL's host when it differs from the UI URL's: DSpace 9.3+ renders on the
 * server only for the host of its ui.baseUrl).
 */
function renderingTile(comp, m) {
  const home = (comp.chain || []).find((r) => r.id === 'frontend.home') || null;
  const d = (home && home.detail) || {};
  const mode = m.rendering || d.rendering || null;
  // a page came back (2xx/3xx): what it names, or does not, was read from it
  const code = Number(d.status_code);
  const read = Boolean(home) && code >= 200 && code < 400;
  let head;
  if (mode === 'ssr') head = 'Server-side (SSR): the homepage arrives already rendered';
  else if (mode === 'csr') head = 'Client-side shell';
  else head = read ? 'Not recognised in the homepage' : 'Read once the homepage answers';
  const note = mode === 'csr' ? d.note || (home && home.message) || '' : '';
  const inPage = (value, missing) => (value ? String(value) : read ? missing : '—');
  const uiHost = urlHost(m.ui_url);
  const pairs = [
    ['Generator', inPage(m.generator, 'not in the page')],
    ['Angular', inPage(m.angular, mode === 'csr' ? 'not in the page (only a server render names it)' : 'not in the page')],
    ['Host header', m.host_header ? `${m.host_header} (the public URL's host)` : uiHost ? `${uiHost} (the UI URL's host)` : "the UI URL's host"],
  ];
  return blockTile({ emoji: '🖼️', title: 'Rendering', subtitle: subLine(head), lines: [note ? subLine(humanize(String(note))) : null], className: 'span-full' },
    el('div', 'kv-wrap', kv(pairs)));
}

// ------------------------------------------------------------------ ports: where each one listens

/**
 * The addresses a port listens on, as the socket table names them ("::", "127.0.0.1", "0.0.0.0, ::"): the
 * rung's `detail.addrs` (the demo's `detail.addr`), else what `metrics.listening` lists for that port. "—"
 * when it does not listen, and when only a TCP probe of localhost answered (`source: tcp`, the socket table
 * could not be read: where it listens is not known).
 */
function listenAddrs(rung, m) {
  const d = (rung && rung.detail) || {};
  let addrs = Array.isArray(d.addrs) ? d.addrs : d.addr ? [d.addr] : [];
  const port = Number(String((rung && rung.id) || '').replace(/^ports\./, ''));
  if (!addrs.length && d.source !== 'tcp' && m.source !== 'tcp' && Array.isArray(m.listening)) {
    addrs = m.listening.filter((e) => e && Number(e.port) === port).map((e) => e.addr);
  }
  const shown = [...new Set(addrs.filter((a) => typeof a === 'string' && a !== ''))];
  return shown.length ? shown.join(', ') : '—';
}

// ------------------------------------------------------------------ per-component "Right now"

function metricTiles(comp, alerts) {
  const m = comp.metrics || {};
  const id = comp.id;
  const out = [];
  if (id === 'postgres') {
    const pct = m.connections_pct ?? (m.max_connections ? (100 * m.connections) / m.max_connections : null);
    out.push(meterTile({
      emoji: '🔗', title: 'Connections', subtitle: `${fmt.pct(pct)} of max_connections in use`,
      value: m.connections, max: m.max_connections || 100, status: thresholdStatus(pct, alerts.postgres_connections_pct),
      label: `${fmt.number(m.connections)} of ${fmt.number(m.max_connections)}`, ariaLabel: 'PostgreSQL connections',
    }));
    out.push(tile({ emoji: '🐘', title: 'DB size', subtitle: 'on disk', value: fmt.gb(m.db_size_bytes) }));
    out.push(tile({ emoji: '⚡', title: 'Active queries', subtitle: m.longest_query_s !== undefined && m.longest_query_s !== null ? `longest ${fmt.number(m.longest_query_s, 1)}\u00a0s` : '', value: fmt.number(m.active_queries) }));
    const short = (String(m.version || '').match(/PostgreSQL\s+([\d.]+)/) || [])[1];
    out.push(tile({ emoji: '🏷️', title: 'Version', subtitle: m.version || 'not read yet', value: short || (m.version ? '' : '—'), className: 'span-full' }));
  } else if (id === 'solr') {
    out.push(tile({ emoji: '🏷️', title: 'Version', subtitle: 'Solr', value: m.version || '—' }));
    out.push(tile({ emoji: '🔎', title: 'Search core', subtitle: 'documents indexed', value: fmt.number(m.search_docs) }));
    out.push(tile({ emoji: '⏱️', title: 'Search query', subtitle: 'q=*:* rows=0', value: latencyText(m.search_latency_ms) || '—' }));
    out.push(coresTile(comp, m));
  } else if (id === 'backend') {
    // the version across the top (it carries the extra lines), then the two timings side by side
    out.push(dspaceTile(m));
    out.push(tile({ emoji: '⏱️', title: 'REST API', subtitle: 'GET /server/api', value: latencyText(m.rest_latency_ms) || '—' }));
    // <wbr>: the path may wrap at its slashes instead of inside a word
    const searchPath = el('span', { class: 'tile-sub' }, 'discover/', el('wbr'), 'search/', el('wbr'), 'objects');
    out.push(tile({ emoji: '🔎', title: 'Search API', subtitle: searchPath, value: latencyText(m.search_latency_ms) || '—' }));
    out.push(actuatorTile(comp, m));
  } else if (id === 'frontend') {
    out.push(tile({ emoji: '⏱️', title: 'Homepage', subtitle: m.title || 'Angular UI', value: latencyText(m.home_latency_ms) || '—' }));
    out.push(publicUrlTile(comp, m));
    out.push(renderingTile(comp, m));
    if (m.title) out.push(tile({ emoji: '🏷️', title: 'Page title', subtitle: String(m.title), className: 'span-full' }));
  } else if (id === 'nginx') {
    out.push(tile({ emoji: '⏱️', title: 'HTTP', subtitle: 'plain request through the web server', value: latencyText(m.http_latency_ms) || '—' }));
    out.push(tile({ emoji: '🔒', title: 'HTTPS', subtitle: m.https_latency_ms === null || m.https_latency_ms === undefined ? 'not configured' : 'TLS request', value: latencyText(m.https_latency_ms) || '—' }));
    out.push(tile({ emoji: '🏷️', title: 'Server header', subtitle: m.server_header || 'not sent', className: 'span-full' }));
  } else if (id === 'ssl') {
    const expires = m.not_after ? new Date(m.not_after) : null;
    out.push(tile({ emoji: '📅', title: 'Days left', subtitle: expires && !Number.isNaN(expires.getTime()) ? `expires ${fmt.dateTime(expires.getTime() / 1000)}` : '', value: m.days_left === undefined || m.days_left === null ? '—' : fmt.number(Math.floor(m.days_left)), valueSub: statusLabel(comp.status) }));
    out.push(tile({ emoji: '🏢', title: 'Issuer', subtitle: m.issuer || 'not read yet' }));
    out.push(tile({ emoji: '🌐', title: 'Subject', subtitle: m.subject || 'not read yet', className: 'span-full' }));
    if (Array.isArray(m.sans) && m.sans.length) out.push(tile({ emoji: '🔖', title: 'Also valid for', subtitle: m.sans.join(', '), className: 'span-full' }));
  } else if (id === 'ports') {
    const rows = (comp.chain || []).map((r) => ({
      label: r.label, addr: listenAddrs(r, m),
      expect: r.detail && r.detail.expect === 'local_only' ? 'local only' : 'open', status: r.status, message: r.message || '',
    }));
    out.push(blockTile({ emoji: '🔌', title: 'Listening', subtitle: 'what each port does right now', className: 'span-full' }, table({
      ariaLabel: 'Ports',
      columns: [
        { key: 'label', label: 'Port', format: (v) => el('span', { style: { whiteSpace: 'nowrap' }, text: v }) },
        { key: 'addr', label: 'Address' },
        { key: 'expect', label: 'Expected', format: (v) => el('span', { style: { whiteSpace: 'nowrap' }, text: v }) },
        { key: 'status', label: 'Result', format: (v, row) => pillCell(v, v === 'ok' ? 'listening' : row.message || statusLabel(v)) },
      ],
      rows,
      emptyText: 'Ports appear after the first check.',
    })));
  } else if (id === 'logs') {
    const files = m.files && typeof m.files === 'object' ? Object.entries(m.files).map(([fid, f]) => ({ id: fid, ...f })) : [];
    const rungs = new Map((comp.chain || []).map((r) => [r.id, r]));
    out.push(blockTile({ emoji: '📜', title: 'Log files', subtitle: m.window_minutes ? `errors counted over the last ${fmt.plural(m.window_minutes, 'minute')}` : '', className: 'span-full' }, table({
      ariaLabel: 'Log files',
      columns: [
        { key: 'label', label: 'Log', format: (v, row) => v || row.id },
        { key: 'errors', label: 'Errors', align: 'end', numeric: true, format: (v) => fmt.number(v) },
        { key: 'warnings', label: 'Warnings', align: 'end', numeric: true, format: (v) => fmt.number(v) },
        { key: 'last_error', label: 'Last error', format: (v) => (v && v.ts ? fmt.relative(Date.now() / 1000 - v.ts) : '—') },
        { key: 'status', label: 'Status', format: (v, row) => pillCell((rungs.get(`logs.${row.id}`) || {}).status || 'unknown') },
      ],
      rows: files,
      emptyText: 'Log files appear after the first check.',
    })));
  } else if (id === 'repository') {
    const items = [
      ['📚', 'Items', fmt.number(m.items), m.estimated ? 'estimated' : 'in the archive'],
      ['🧾', 'Bitstreams', fmt.number(m.bitstreams), m.estimated ? 'estimated' : 'files'],
      ['🏛️', 'Communities', fmt.number(m.communities), ''],
      ['🗂️', 'Collections', fmt.number(m.collections), ''],
      ['👤', 'E-people', fmt.number(m.eperson_count), 'registered accounts'],
      ['📝', 'Workflow items', fmt.number(m.workflow_items), 'waiting for review'],
      ['🐘', 'DB size', fmt.gb(m.db_size), 'PostgreSQL on disk'],
      ['🗄️', 'Bitstream bytes', fmt.gb(m.assetstore_bytes), 'sum of file sizes'],
    ];
    for (const [emoji, title, value, subtitle] of items) out.push(tile({ emoji, title, value, subtitle }));
  } else if (id === 'security') {
    out.push(...securityTiles(comp, { blockTile, kv, subLine, breakable, hintNodes }));
  } else if (id === 'assetstore') {
    out.push(meterTile({ emoji: '🗄️', title: 'Space used', subtitle: `${fmt.gb(m.used)} of ${fmt.gb(m.total)} on ${m.mount || m.path || 'the assetstore mount'}`, value: m.used, max: m.total, status: comp.status, label: fmt.pct(m.pct), ariaLabel: 'Assetstore space used' }));
    out.push(tile({ emoji: '💾', title: 'Free', subtitle: m.mount ? `on ${m.mount}` : '', value: fmt.gb(m.free) }));
    const growth = Number(m.growth_bytes_per_day) || 0;
    const days = growth > 0 && m.free ? Math.floor(Number(m.free) / growth) : null;
    out.push(tile({ emoji: '📈', title: 'Growth', subtitle: days !== null && days < 3650 ? `full in about ${fmt.plural(days, 'day')} at this rate` : 'from the last days of history', value: growth > 0 ? `+${fmt.bytes(growth)}/day` : '—' }));
    out.push(tile({ emoji: '📁', title: 'Path', subtitle: m.path || 'not set', className: 'span-full' }));
  } else {
    const scalars = Object.entries(m).filter(([, v]) => v === null || ['string', 'number', 'boolean'].includes(typeof v));
    out.push(blockTile({ emoji: comp.emoji || '🧩', title: 'Metrics', subtitle: scalars.length ? '' : 'nothing reported yet', className: 'span-full' },
      scalars.length ? el('div', 'kv-wrap', kv(scalars.map(([k, v]) => [humanKey(k), typeof v === 'number' ? fmt.number(v, Number.isInteger(v) ? 0 : 2) : String(v ?? '—'), typeof v === 'number']))) : null));
  }
  return out;
}

// ------------------------------------------------------------------ last error

/** The most recent thing that went wrong for this component: an event, or a log line for the Logs component. */
function lastError(detail) {
  const events = (detail.recent_events || []).filter((e) => {
    if (e.kind === 'error') return true;
    if (e.kind === 'alert') return e.data && ['opened', 'escalated'].includes(e.data.type);
    if (e.kind === 'component') return e.data && (e.data.to === 'crit' || e.data.to === 'warn');
    return false;
  });
  let best = events.length ? { ts: events[0].ts, text: events[0].message } : null;
  const files = detail.metrics && detail.metrics.files && typeof detail.metrics.files === 'object' ? Object.values(detail.metrics.files) : [];
  for (const f of files) {
    if (f && f.last_error && f.last_error.ts && (!best || f.last_error.ts > best.ts)) best = { ts: f.last_error.ts, text: f.last_error.line };
  }
  const failing = (detail.chain || []).find((r) => r.status === 'crit' || r.status === 'warn');
  if (failing && (!best || (failing.ts && failing.ts >= best.ts))) best = { ts: failing.ts, text: `${failing.label}: ${failing.message || statusLabel(failing.status)}` };
  return best;
}

// ------------------------------------------------------------------ screen

/**
 * Render one component's detail.
 * @param {HTMLElement} root
 * @param {object} params `{id}`
 * @param {object} ctx
 * @returns {() => void} destroy
 */
export function render(root, params, ctx) {
  const id = String(params.id || '');
  const view = grid();
  root.replaceChildren(view);

  const ladderCol = el('div', 'span-6', el('div', 'col'));
  const nowSection = section({ title: 'Right now', layout: 'two', span: 6, id: 'component-now' });
  const chartWrap = el('div', 'span-8');
  const sideCol = el('div', 'span-4', el('div', 'col'));
  const eventsSection = section({ title: 'Recent events', layout: 'stack', span: 6, id: 'component-events' });
  const connectionSection = section({ title: 'Connection used', layout: 'stack', span: 6, id: 'component-connection' });
  view.append(ladderCol, nowSection, chartWrap, sideCol, eventsSection, connectionSection);
  if (id === 'security') {
    // the ladder above its tiles, both across the page: the open-ports and software tables keep every column
    ladderCol.className = 'span-12';
    nowSection.classList.replace('span-6', 'span-12');
  }

  // status and "since" are on the ladder head beside it; the header keeps only how fresh the check is
  const checked = el('span', { class: 'muted', id: 'component-checked', text: '' });
  ctx.setControls(checked);

  let detail = null;
  let ladderEl = null;
  let chart = null;
  let chartTileEl = null;
  let check = null;
  let alertsCfg = {};
  let loading = null;
  let gone = false;

  function paintHeader(comp) {
    paintTick(comp);
  }

  /** The ladder head's pill: "Healthy · since 01:34 PM". */
  function pillText(comp) {
    const s = comp.status || 'unknown';
    const since = sinceText(comp.since);
    return since ? `${statusLabel(s)} · since ${since}` : statusLabel(s);
  }

  function paintTick(comp = detail) {
    if (!comp) return;
    // ages inside the tiles ("checked 3 m ago" on the actuator) move with the header's
    for (const node of nowSection.body.querySelectorAll('[data-asked-at]')) {
      const text = askedText(node.dataset.askedAt, node.dataset.askedNote);
      if (node.textContent !== text) node.textContent = text;
    }
    const ago = store.checkedAgo();
    const stale = store.get('stale');
    if (stale) {
      const words = staleWords(stale, store.lastCheckTs());
      checked.textContent = `${words.short} · ${words.last}`;
      return;
    }
    checked.textContent = ago === null ? 'First check pending' : `Checked ${fmt.relative(ago)}`;
  }

  function paintLadder(comp) {
    if (!ladderEl) {
      ladderEl = ladder(comp, { href: null, storage: comp.id === 'assetstore', pillText });
      ladderCol.firstChild.replaceChildren(ladderEl);
    } else {
      ladderEl.update(comp);
    }
    paintHints(ladderEl, comp);
  }

  function paintNow(comp) {
    const active = document.activeElement;
    const focusedInside = active && nowSection.contains(active);
    nowSection.body.replaceChildren(...metricTiles(comp, alertsCfg));
    if (focusedInside) nowSection.body.querySelector('button, a, [tabindex]')?.focus({ preventScroll: true });
  }

  function paintSide(data) {
    const col = sideCol.firstChild;
    const err = lastError(data);
    const errTile = tile({
      emoji: '🧯', title: 'Last error',
      subtitle: el('span', { class: `tile-sub ${err ? 'tile-sub--wrap' : ''}`, text: err ? humanize(String(err.text).split('\n').join(' · ')) : 'No errors recorded — nothing has gone wrong in the kept history.' }),
      value: err ? fmt.relative(Date.now() / 1000 - err.ts) : undefined,
      valueSub: err ? fmt.dateTime(err.ts) : undefined,
      // the one tile recipe: message wraps in the subtitle, when on the right (not a block under the emoji)
      className: 'last-error',
    });
    const affected = Array.isArray(data.affected) ? data.affected : [];
    const affectsTile = blockTile({ emoji: '🧭', title: 'If this fails', subtitle: affected.length ? `${fmt.plural(affected.length, 'service')} would be affected` : 'Nothing else depends on it.' },
      affected.length ? el('div', { class: 'chip-list', role: 'list', 'aria-label': 'Affected services' }, ...affected.map((label) => el('span', { class: 'pill', role: 'listitem', text: label }))) : null);
    col.replaceChildren(errTile, affectsTile);
  }

  function paintEvents(data) {
    const rows = (data.recent_events || []).slice(0, 20);
    eventsSection.body.replaceChildren(table({
      ariaLabel: `Recent events for ${data.label}`,
      columns: [
        { key: 'ts', label: 'When', width: '160px', format: (v) => el('time', { datetime: new Date(Number(v) * 1000).toISOString(), style: { whiteSpace: 'nowrap' }, text: fmt.dateTime(v) }) },
        { key: 'message', label: 'What happened', format: (v) => humanize(v) },
        { key: 'kind', label: 'Kind', width: '150px', format: (v, row) => el('span', { class: 'pill', text: kindWord(v, row.data) }) },
      ],
      rows,
      emptyText: `No events for ${data.label} yet — status changes and alerts show up here.`,
    }));
  }

  function paintConnection(data) {
    connectionSection.titleEl.textContent = CONNECTION_TITLE[data.id] || 'Connection used';
    const pairs = configPairs(data.id, data.config_used);
    const node = blockTile({ emoji: '🔗', title: CONNECTION_TITLE[data.id] || 'Connection used', subtitle: 'from sentinel.yaml · secrets are never shown' },
      pairs.length ? el('div', 'kv-wrap', kv(pairs)) : el('p', { class: 'muted', text: SETTINGS_NOTE[data.id] || 'Nothing configured for this service.' }));
    connectionSection.body.replaceChildren(node);
  }

  function ensureChart(data) {
    const series = data.latency_series_1h || {};
    check = series.check || HEADLINE_CHECK[data.id] || null;
    if (!check) {
      chartWrap.hidden = true;
      sideCol.className = 'span-12';
      sideCol.firstChild.className = 'section-body section-body--two';
      return;
    }
    if (chart) return;
    chart = lineChart({ series: [], unit: 'ms', height: 200, title: `${data.label} response time` });
    chartTileEl = chartTile({ title: `Response time · ${CHECK_TITLE[check] || check} · last 24 hours`, chart, span: 8, className: 'span-8' });
    chartTileEl.classList.remove('span-6');
    chartWrap.replaceChildren(chartTileEl);
    chartWrap.hidden = false;
  }

  async function loadChart() {
    if (!chart || !check) return;
    chart.setBusy(true);
    try {
      const data = await api.latency(check, '24h', { signal: ctx.signal });
      if (ctx.signal.aborted) return;
      chart.update({
        series: [{ id: check, label: CHECK_TITLE[check] || check, points: (data.points || []).map((p) => [p[0], p[1], p[2]]) }],
        thresholds: data.thresholds || null, from: data.from, to: data.to,
      });
      chartTileEl.setNote('');
    } catch (err) {
      if (err && err.name === 'AbortError') return;
      chartTileEl.setNote(`Not loaded: ${err.message}`);
    } finally {
      if (chart) chart.setBusy(false);
    }
  }

  function paintCurrentValue(comp) {
    if (!chartTileEl) return;
    const rung = (comp.chain || []).find((r) => r.id === check);
    chartTileEl.setValue(rung ? latencyText(rung.latency_ms) || '—' : latencyText(comp.latency_ms) || '—');
  }

  function paintAll(data) {
    detail = data;
    ctx.setTitle(data.label);
    paintHeader(data);
    paintLadder(data);
    paintNow(data);
    paintSide(data);
    paintEvents(data);
    paintConnection(data);
    ensureChart(data);
    paintCurrentValue(data);
  }

  function showGone(message) {
    gone = true;
    view.replaceChildren(el('div', 'span-12', emptyState('🧩', message)));
    ctx.setControls();
  }

  async function load({ withChart = false } = {}) {
    if (loading) return loading;
    loading = (async () => {
      try {
        const data = await api.component(id, { signal: ctx.signal });
        if (ctx.signal.aborted) return;
        gone = false;
        paintAll(data);
        if (withChart) await loadChart();
      } catch (err) {
        if (err && err.name === 'AbortError') return;
        if (err && err.status === 404) showGone(`This service is not monitored — turn it on in More to see its checks.`);
        else if (!detail) showGone(`Could not load this service — ${err.message}`);
      } finally {
        loading = null;
      }
    })();
    return loading;
  }

  async function loadThresholds() {
    try {
      const cfg = await api.config({ signal: ctx.signal });
      alertsCfg = (cfg && cfg.alerts) || {};
      if (detail) paintNow(detail);
    } catch { /* defaults stay */ }
  }

  // live: status frames patch the ladder + metrics; component/alert events reload the detail payload.
  const unsubscribe = store.subscribe('status', () => {
    if (gone) return;
    const comp = store.components().find((c) => c.id === id);
    if (!comp) return;
    // a frame has no settings: the tiles read them from the detail payload merged under it (the Public URL)
    detail = { ...(detail || {}), ...comp };
    paintHeader(comp);
    paintLadder(comp);
    paintNow(detail);
    paintCurrentValue(comp);
  });
  const offComponent = store.on('sse:component', (data) => {
    const cid = data && (data.id || (data.component && data.component.id) || data.component);
    if (cid === id) load();
  });
  const offAlert = store.on('sse:alert', (data) => {
    if (data && data.alert && data.alert.component === id) load();
  });
  const timer = setInterval(() => load({ withChart: true }), REFRESH_MS);
  const tick = setInterval(() => paintTick(), 1000);

  load({ withChart: true });
  loadThresholds();

  return () => {
    unsubscribe();
    offComponent();
    offAlert();
    clearInterval(timer);
    clearInterval(tick);
    if (chart) chart.destroy();
  };
}
