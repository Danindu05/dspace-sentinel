/**
 * ui.js — the DSpace Sentinel UI kit (SPEC §12.4, §12.6).
 *
 * Every screen is built from these helpers so a new screen looks finished by default.
 * All text goes through `textContent`; nothing here uses innerHTML.
 *
 * DOM building
 *   el(tag, props?, ...children) -> HTMLElement
 *       props: string (className) or {class|className, id, text, title, role, href, type, disabled,
 *              value, tabIndex, hidden, attrs: {name: value}, dataset: {key: value}, style: {prop: value},
 *              aria: {label: 'x'} (→ aria-label), on: {click: fn}, onClick: fn (any on<Event>)}.
 *       children: string | number | Node | Array | null/false/undefined (skipped).
 *   svgEl(tag, attrs?) -> SVGElement
 *   replaceKeepingFocus(container, nodes, {fallback}) — swap a list and keep keyboard focus on the same
 *       [data-key] item/control, else the neighbouring item's same control, else `fallback`/the heading
 *
 * Tiles (the one recipe — surface fill, 1 px line, radius 22, emoji square left, title + subtitle, end slot)
 *   tile({emoji, title, subtitle, value, valueSub, status, pill, chevron, href, onClick, actions, ariaLabel,
 *         className, id, dataset}) -> <a class="tile tile--action"> | <button class="tile tile--action"> | <div class="tile">
 *   statTile({emoji, label, value, unit, sub, status, spark, href, onClick, ariaLabel, id}) -> tile.tile--stat
 *       (`spark`: number[] | [ts, value][] → 12-point sparkline under the value)
 *   emptyState(emoji, sentence, {action, className}) -> <div class="empty"> (tile-styled, one emoji + one sentence)
 *
 * Layout
 *   section({title, subtitle, actions, span=12, layout='stack'|'tiles'|'stat'|'grid'|'two', id, className, level=2})
 *       -> <section class="section span-N"> with `.head`, `.titleEl`, `.actionsEl` and `.body` expandos
 *   grid(...children) -> <div class="grid"> (12 columns, gap 16)   ·   column(...children) -> <div class="col">
 *   pageHeaderControls(...nodes) -> <div class="controls"> (row of controls, right-aligned in the page header)
 *   chartTile({title, value, chart, span=6}) -> <div class="tile chart-tile span-N"> with .setValue(text), .setNote(text)
 *   table({columns: [{key, label, align:'start'|'end', format(value,row)->string|Node, sortable, width}],
 *          rows, caption, sortKey, sortDir, ariaLabel, rowKey, onRowClick, emptyText}) -> <div class="table-wrap"> with .update(rows)
 *
 * Controls
 *   button({label, emoji, variant='ghost'|'filled'|'text'|'danger', size='md'|'sm', full, type='button',
 *           onClick, disabled, ariaLabel, busyLabel, title, id}) -> <button class="btn btn--variant"> with .setBusy(bool), .setLabel(text)
 *   chipRow({options: [{value, label, emoji, title}], value, onChange(value), ariaLabel, name, className})
 *       -> <div class="chips" role="radiogroup"> with .value (get/set) and .set(value, {silent})
 *   toggle({label, description, emoji, checked, onChange(checked), disabled, id, bare, name})
 *       -> <label class="tile toggle"> row with switch on the right (bare → just the <label class="switch">); .input, .set(checked)
 *   field({label, input, help, error, id}) -> <div class="field"> with .setError(text)
 *   input({type='text', id, name, value, placeholder, inputmode, required, min, max, step, autocomplete,
 *          onInput, onChange, ariaLabel, disabled}) -> <input class="input">
 *   select({options: [{value, label}], value, onChange, id, name, ariaLabel}) -> <select class="input">
 *   disclosure({summary, open, children, className}) -> <details class="disclosure">
 *   kbd(text) -> <kbd>
 *
 * Overlays
 *   panel({title, name, onClose, ariaLabel}) -> {el, body, foot, close(result), setTitle(text), setBusy(bool)}
 *       right-side <dialog class="panel" closedby="any"> (520 px; bottom sheet < 900 px). One at a time.
 *   confirm({title, message, confirmLabel='Confirm', cancelLabel='Cancel', danger=false}) -> Promise<boolean>
 *   snackbar(message, {action: {label, onClick}, duration=6000}) -> {dismiss()}   (aria-live polite, never steals focus)
 *   snackbar.undo(message, onUndo, {duration}) -> {dismiss()}
 *   shortcutsDialog(rows: [[keys, description]]) -> opens the "Keyboard shortcuts" dialog
 *
 * Status
 *   STATUS: {ok, warn, crit, unknown, off} → {label, mark, fill: css var, ink: css var}
 *   statusLabel(status) -> 'Healthy' | 'Warning' | 'Critical' | 'Unknown' | 'Off'
 *   statusMark(status) -> '✅' | '⚠️' | '❌' | '—'
 *   statusDot(status, {label}) -> <span class="dot dot--status"> (+ visually hidden label)
 *   statusPill(status, text?) -> <span class="pill pill--status">mark text</span>
 *   ackedPill() -> the "Acked" pill that takes an Ack button's place (data-action="ack", focusable from script)
 *   severityStatus(severity) -> 'warn' | 'crit'  (alert severity → status)
 *   staleWords(reason, lastTs) -> {title: 'Sentinel is unreachable', short, last: 'last check 11:34'} (store.stale)
 *   meter(...) -> re-exported from charts.js
 *
 * Formatting (`fmt`): number(v, decimals=0) · pct(v, decimals=0) · bytes(n) · gb(n) · ms(v) · duration(s) ·
 *   uptime(s) ("17 days") · relative(secondsAgo) ("8 s ago") · clock(ts) ("10:43") · dateTime(ts) ·
 *   date(ts) · rate(bps) ("1.2 MB/s") · plural(n, word, pluralWord?) · load(v) · kernelShort(kernel)
 *   Numbers are glued to their units with a no-break space (`NBSP`); `keepUnits(text)` does the same for
 *   text from the API (collector reasons, alert and event messages); `humanize(text)` also rewords status
 *   ids and module ids in older API text ("changed from ok to warn" → "Healthy → Warning").
 */

import { meter as chartMeter, sparkline } from './charts.js';

// ------------------------------------------------------------------ DOM helpers

const SVG_NS = 'http://www.w3.org/2000/svg';

function appendChildren(parent, children) {
  for (const child of children) {
    if (child === null || child === undefined || child === false || child === true) continue;
    if (Array.isArray(child)) {
      appendChildren(parent, child);
    } else if (child instanceof Node) {
      parent.appendChild(child);
    } else {
      parent.appendChild(document.createTextNode(String(child)));
    }
  }
}

/**
 * Create an element.
 * @param {string} tag
 * @param {string|object} [props]
 * @param {...any} children
 * @returns {HTMLElement}
 */
export function el(tag, props, ...children) {
  const node = document.createElement(tag);
  if (typeof props === 'string') {
    node.className = props;
  } else if (props && typeof props === 'object' && !(props instanceof Node) && !Array.isArray(props)) {
    for (const [key, value] of Object.entries(props)) {
      if (value === undefined || value === null) continue;
      if (key === 'class' || key === 'className') {
        node.className = value;
      } else if (key === 'text') {
        node.textContent = String(value);
      } else if (key === 'attrs') {
        for (const [k, v] of Object.entries(value)) {
          if (v !== null && v !== undefined) node.setAttribute(k, String(v));
        }
      } else if (key === 'dataset') {
        Object.assign(node.dataset, value);
      } else if (key === 'style') {
        Object.assign(node.style, value);
      } else if (key === 'aria') {
        for (const [k, v] of Object.entries(value)) {
          if (v !== null && v !== undefined) node.setAttribute(`aria-${k}`, String(v));
        }
      } else if (key === 'on') {
        for (const [evt, fn] of Object.entries(value)) node.addEventListener(evt, fn);
      } else if (/^on[A-Z]/.test(key) && typeof value === 'function') {
        node.addEventListener(key.slice(2).toLowerCase(), value);
      } else if (key === 'hidden') {
        node.hidden = Boolean(value);
      } else if (key === 'disabled') {
        node.disabled = Boolean(value);
      } else if (key === 'tabIndex') {
        node.tabIndex = value;
      } else if (key === 'htmlFor') {
        node.htmlFor = value;
      } else if (key === 'value') {
        node.value = value;
      } else if (key === 'checked') {
        node.checked = Boolean(value);
      } else {
        node.setAttribute(key, String(value));
      }
    }
  } else if (props !== undefined) {
    children.unshift(props);
  }
  appendChildren(node, children);
  return node;
}

/** Create an SVG element with attributes. */
export function svgEl(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value !== null && value !== undefined) node.setAttribute(key, String(value));
  }
  return node;
}

// ------------------------------------------------------------------ focus keeping

const FOCUSABLE = 'a[href], button, input, select, textarea, [tabindex]';

function canFocus(node) {
  return Boolean(node && node.isConnected && node.matches && node.matches(FOCUSABLE) && !node.disabled
    && !node.closest('[hidden], [inert]'));
}

/** The item's focusable controls in document order (the item itself first when it is one). */
function controlsOf(item) {
  const list = canFocus(item) ? [item] : [];
  for (const node of item.querySelectorAll(FOCUSABLE)) if (canFocus(node)) list.push(node);
  return list;
}

/** Top-level `[data-key]` items of a container, in order (keyed descendants of an item are skipped). */
function keyedItems(container) {
  return Array.from(container.querySelectorAll('[data-key]')).filter((node) => {
    const outer = node.parentElement ? node.parentElement.closest('[data-key]') : null;
    return !outer || !container.contains(outer);
  });
}

/** The same control inside `item`: by `data-action`, else by position among its controls. */
function sameControl(item, slot) {
  if (!item) return null;
  if (slot.action) {
    const node = item.querySelector(`[data-action="${CSS.escape(slot.action)}"]`);
    return canFocus(node) ? node : null;
  }
  const controls = controlsOf(item);
  return controls.length ? controls[Math.min(Math.max(0, slot.index), controls.length - 1)] : null;
}

/**
 * Replace a container's children and keep keyboard focus where the user was (SPEC §12.6 "focus
 * visible everywhere"). Focus goes, in order, to: the same control of the same `[data-key]` item
 * (matched by `data-action`, else by position) · the same control of the item now at that position,
 * then of the items after it and before it (a deleted report → the next report's Delete; an acked
 * alert → its "Acked" state) · `fallback` · the section heading around the container · the page
 * heading. Nothing moves when focus was not inside the container, so live repaints never steal it.
 * @param {HTMLElement} container
 * @param {Node[]} nodes
 * @param {{fallback?: HTMLElement|(() => HTMLElement|null)}} [opts]
 */
export function replaceKeepingFocus(container, nodes, { fallback } = {}) {
  const active = document.activeElement;
  const inside = Boolean(active && active !== document.body && container.contains(active));
  let key = null;
  let index = -1;
  let slot = null;
  if (inside) {
    const items = keyedItems(container);
    const item = items.find((node) => node === active || node.contains(active));
    if (item) {
      key = item.dataset.key;
      index = items.indexOf(item);
      slot = active.dataset && active.dataset.action ? { action: active.dataset.action } : { index: controlsOf(item).indexOf(active) };
    }
  }
  container.replaceChildren(...nodes);
  if (!inside) return;
  if (active.isConnected && container.contains(active) && canFocus(active)) {
    active.focus({ preventScroll: true });
    return;
  }
  let target = null;
  if (slot) {
    const items = keyedItems(container);
    target = sameControl(items.find((node) => node.dataset.key === key), slot);
    if (!target && items.length) {
      const start = Math.min(index, items.length - 1);
      const order = [...items.slice(start), ...items.slice(0, start).reverse()];
      for (const node of order) {
        target = sameControl(node, slot);
        if (target) break;
      }
    }
  }
  if (!target) target = typeof fallback === 'function' ? fallback() : fallback || null;
  if (!canFocus(target)) {
    const heading = container.closest('section') && container.closest('section').querySelector('.section-title');
    target = canFocus(heading) ? heading : document.getElementById('page-title');
  }
  if (target && typeof target.focus === 'function') {
    target.focus({ preventScroll: true });
    if (target.scrollIntoView) target.scrollIntoView({ block: 'nearest' });
  }
}

// ------------------------------------------------------------------ status

export const STATUS = {
  ok: { label: 'Healthy', mark: '✅', fill: 'var(--ok-fill)', ink: 'var(--ok-ink)' },
  warn: { label: 'Warning', mark: '⚠️', fill: 'var(--warn-fill)', ink: 'var(--warn-ink)' },
  crit: { label: 'Critical', mark: '❌', fill: 'var(--crit-fill)', ink: 'var(--crit-ink)' },
  unknown: { label: 'Unknown', mark: '—', fill: 'var(--unknown-fill)', ink: 'var(--ink-2)' },
  off: { label: 'Off', mark: '—', fill: 'var(--unknown-fill)', ink: 'var(--ink-3)' },
};

function normStatus(status) {
  return STATUS[status] ? status : 'unknown';
}

/** Human label for a status. */
export function statusLabel(status) {
  return STATUS[normStatus(status)].label;
}

/** Emoji mark for a status. */
export function statusMark(status) {
  return STATUS[normStatus(status)].mark;
}

/**
 * The words for a status that is out of date (store.stale): `title` for the Pulse headline and `last`
 * for "last check 11:34" (a date is added once it is a day old).
 * @param {'offline'|'overdue'} reason
 * @param {number|null} lastTs epoch seconds of the last completed check
 */
export function staleWords(reason, lastTs) {
  const title = reason === 'offline' ? 'Sentinel is unreachable' : 'Checks are overdue';
  const short = reason === 'offline' ? 'Sentinel unreachable' : 'Checks overdue';
  if (lastTs === null || lastTs === undefined) return { title, short, last: 'no check yet' };
  const old = Date.now() / 1000 - Number(lastTs) >= 86400;
  return { title, short, last: `last check ${old ? fmt.dateTime(lastTs) : fmt.clock(lastTs)}` };
}

/** Alert severity ("warning" | "critical") → status id. */
export function severityStatus(severity) {
  return severity === 'critical' ? 'crit' : 'warn';
}

/** A small coloured dot with a visually hidden label (status is never colour-alone). */
export function statusDot(status, { label } = {}) {
  const s = normStatus(status);
  return el('span', { class: `dot dot--${s}` }, el('span', { class: 'sr-only', text: label || STATUS[s].label }));
}

/** A rounded pill: mark + text. */
export function statusPill(status, text) {
  const s = normStatus(status);
  return el('span', { class: `pill pill--${s}` }, el('span', { class: 'pill-mark', 'aria-hidden': 'true', text: STATUS[s].mark }), ' ', text || STATUS[s].label);
}

/**
 * The "Acked" pill that replaces an alert's Ack button. It shares the button's `data-action` and is
 * focusable from script, so keyboard focus stays on the alert's action slot after Ack.
 */
export function ackedPill() {
  const node = statusPill('unknown', 'Acked');
  node.tabIndex = -1;
  node.dataset.action = 'ack';
  return node;
}

// ------------------------------------------------------------------ formatting

const nf = new Map();
function numberFormat(decimals) {
  const key = String(decimals);
  if (!nf.has(key)) nf.set(key, new Intl.NumberFormat(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals }));
  return nf.get(key);
}
const timeFmt = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit' });
const timeSecFmt = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
const dateFmt = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short' });
const dateTimeFmt = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
const UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
/** No-break space: every number is glued to its unit ("94 %", "2.6 s", "420.4 GB") so the pair never wraps apart. */
export const NBSP = '\u00a0';
const UNIT_RE = /(\d)[ \u202f](%|ms|s|m|h|d|min|B|KB|MB|GB|TB|PB)(?![\w])/g;

function isNil(value) {
  return value === null || value === undefined || Number.isNaN(value);
}

/**
 * Glue numbers to their units in text we did not format ourselves (collector reasons, alert and event
 * messages: "search took 2.6 s", "swap 38 %", "lasted 4 m 0 s") so a unit never wraps onto its own line.
 */
export function keepUnits(text) {
  return text === null || text === undefined ? text : String(text).replace(UNIT_RE, `$1${NBSP}$2`);
}

/** Module names as the More › Modules toggles say them (for events recorded before the API used them). */
const MODULE_WORDS = {
  ports: 'Ports', postgres: 'PostgreSQL', solr: 'Solr', backend: 'DSpace Backend', frontend: 'DSpace Frontend', nginx: 'nginx',
  ssl: 'SSL certificate', logs: 'Logs', repository: 'Repository stats', assetstore: 'Assetstore', incidents: 'Incidents',
};
/** How a dependency rung words the state of the component it mirrors (as health.DEPENDENCY_STATE does now). */
const DEPENDENCY_WORDS = {
  ok: 'is healthy', warn: 'is degraded (Warning)', crit: 'is down (Critical)', unknown: 'could not be checked (Unknown)', off: 'is switched off',
};
const STATUS_IDS = 'ok|warn|crit|unknown|off';

/**
 * Put text from the API into the UI's words (SPEC §12.6: an action keeps its name, no jargon). Rows
 * written before the backend used these words read "Solr changed from ok to warn: …", "Module solr
 * switched on", "DSpace Backend is warn: …" and "WARNING Solr search slow"; they become "Solr: Healthy →
 * Warning — …", "Solr turned on", "DSpace Backend is degraded (Warning): …" and "Warning: Solr search
 * slow". Text already in these words passes through unchanged. Units are glued (keepUnits).
 */
export function humanize(text) {
  if (text === null || text === undefined) return text;
  let out = String(text);
  const label = (id) => (id === 'new' ? null : statusLabel(id));
  out = out.replace(new RegExp(`^(.+?) changed from (new|${STATUS_IDS}) to (${STATUS_IDS})(?:: ([\\s\\S]*))?$`), (m, name, from, to, reason) => {
    const change = label(from) ? `${label(from)} → ${label(to)}` : label(to);
    return `${name}: ${change}${reason ? ` — ${reason}` : ''}`;
  });
  out = out.replace(/^Module (\S+) switched (on|off)$/, (m, id, state) => `${MODULE_WORDS[id] || id} turned ${state}`);
  out = out.replace(/^Modules changed: (.+)$/, (m, list) => list.split(/,\s*/)
    .map((part) => part.replace(/^(\S+) (on|off)$/, (x, id, state) => `${MODULE_WORDS[id] || id} turned ${state}`)).join(', '));
  out = out.replace(new RegExp(` is (${STATUS_IDS})(?=$|[\\s:,.;)])`, 'g'), (m, id) => ` ${DEPENDENCY_WORDS[id]}`);
  out = out.replace(/\b(CRITICAL|WARNING)\b/g, (m) => m.charAt(0) + m.slice(1).toLowerCase());
  // "Warning 35 errors …" → "Warning: 35 errors …" — only before a word, never before "→" or "—"
  out = out.replace(/(^|: |Reminder )(Critical|Warning|Resolved) (?=[A-Za-z0-9#])/g, '$1$2: ');
  return keepUnits(out);
}

export const fmt = {
  /** Thousands-separated number. */
  number(value, decimals = 0) {
    return isNil(value) ? '—' : numberFormat(decimals).format(Number(value));
  },
  /** "87 %" (no-break space before the sign). */
  pct(value, decimals = 0) {
    return isNil(value) ? '—' : `${numberFormat(decimals).format(Number(value))}${NBSP}%`;
  },
  /** Human bytes: 1536 → "1.5 KB"; GB and above with one decimal. */
  bytes(n, decimals = 1) {
    if (isNil(n)) return '—';
    let value = Number(n);
    let unit = 0;
    while (Math.abs(value) >= 1024 && unit < UNITS.length - 1) {
      value /= 1024;
      unit += 1;
    }
    return unit === 0 ? `${Math.round(value)}${NBSP}B` : `${numberFormat(decimals).format(value)}${NBSP}${UNITS[unit]}`;
  },
  /** Bytes as GB with one decimal — the one format for disk, mount, assetstore and memory sizes (SPEC §12.6). */
  gb(n, decimals = 1) {
    return isNil(n) ? '—' : `${numberFormat(decimals).format(Number(n) / 1024 ** 3)}${NBSP}GB`;
  },
  /** Latency: "91 ms" below one second, "2.4 s" above. */
  ms(value) {
    if (isNil(value)) return '—';
    const v = Number(value);
    return v < 1000 ? `${numberFormat(0).format(v)}${NBSP}ms` : `${numberFormat(1).format(v / 1000)}${NBSP}s`;
  },
  /** "6 m 12 s" / "17 d 3 h" / "45 s" (each number glued to its unit). */
  duration(seconds) {
    if (isNil(seconds)) return '—';
    let s = Math.round(Number(seconds));
    if (s < 60) return `${s}${NBSP}s`;
    let m = Math.floor(s / 60);
    s %= 60;
    if (m < 60) return `${m}${NBSP}m ${s}${NBSP}s`;
    let h = Math.floor(m / 60);
    m %= 60;
    if (h < 24) return `${h}${NBSP}h ${m}${NBSP}m`;
    const d = Math.floor(h / 24);
    h %= 24;
    return `${d}${NBSP}d ${h}${NBSP}h`;
  },
  /** Uptime for the footer: "17 days", "5 hours", "12 minutes". */
  uptime(seconds) {
    if (isNil(seconds)) return '—';
    const s = Number(seconds);
    if (s >= 86400) return fmt.plural(Math.floor(s / 86400), 'day');
    if (s >= 3600) return fmt.plural(Math.floor(s / 3600), 'hour');
    return fmt.plural(Math.max(1, Math.floor(s / 60)), 'minute');
  },
  /** "8 s ago" / "3 m ago" / "2 h ago" / "5 d ago". */
  relative(secondsAgo) {
    if (isNil(secondsAgo)) return 'never';
    const s = Math.max(0, Math.floor(Number(secondsAgo)));
    if (s < 60) return `${s}${NBSP}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)}${NBSP}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}${NBSP}h ago`;
    return `${Math.floor(s / 86400)}${NBSP}d ago`;
  },
  /** Local clock "10:43" (`withSeconds` → "10:43:07"). */
  clock(ts, withSeconds = false) {
    return isNil(ts) ? '—' : (withSeconds ? timeSecFmt : timeFmt).format(new Date(Number(ts) * 1000));
  },
  /** Local "24 Sep, 10:43". */
  dateTime(ts) {
    return isNil(ts) ? '—' : dateTimeFmt.format(new Date(Number(ts) * 1000));
  },
  /** Local "24 Sep". */
  date(ts) {
    return isNil(ts) ? '—' : dateFmt.format(new Date(Number(ts) * 1000));
  },
  /** Bytes per second → "1.2 MB/s". */
  rate(bps) {
    return isNil(bps) ? '—' : `${fmt.bytes(bps)}/s`;
  },
  /** "1 warning" / "2 warnings" (the count stays with its noun). */
  plural(n, word, pluralWord) {
    const count = Number(n) || 0;
    return `${numberFormat(0).format(count)}${NBSP}${count === 1 ? word : pluralWord || `${word}s`}`;
  },
  /** Load average with two decimals. */
  load(value) {
    return isNil(value) ? '—' : numberFormat(2).format(Number(value));
  },
  /** "6.8.0-45-generic" → "6.8". */
  kernelShort(kernel) {
    if (!kernel) return '—';
    const m = String(kernel).match(/^(\d+\.\d+)/);
    return m ? m[1] : String(kernel);
  },
};

// ------------------------------------------------------------------ tiles

function emojiBox(emoji, className = 'tile-icon') {
  return el('span', { class: className, 'aria-hidden': 'true', text: emoji || '' });
}

/**
 * The tile recipe. Renders `<a>` with `href`, `<button>` with `onClick`, else `<div>`.
 * @returns {HTMLElement}
 */
export function tile({
  emoji, title, subtitle, value, valueSub, status, pill, chevron, href, onClick, actions,
  ariaLabel, className = '', id, dataset, children,
} = {}) {
  const interactive = Boolean(href || onClick);
  const tag = href ? 'a' : onClick ? 'button' : 'div';
  const node = el(tag, { class: `tile${interactive ? ' tile--action' : ''}${className ? ` ${className}` : ''}`, id, dataset });
  if (href) node.href = href;
  if (tag === 'button') node.type = 'button';
  if (onClick) node.addEventListener('click', onClick);
  if (ariaLabel) node.setAttribute('aria-label', ariaLabel);

  if (emoji) node.appendChild(emojiBox(emoji));
  const body = el('span', 'tile-body');
  body.appendChild(el('span', { class: 'tile-title', text: title ?? '' }));
  if (subtitle !== undefined && subtitle !== null && subtitle !== '') {
    body.appendChild(subtitle instanceof Node ? subtitle : el('span', { class: 'tile-sub', text: String(subtitle) }));
  }
  if (children) appendChildren(body, Array.isArray(children) ? children : [children]);
  node.appendChild(body);

  const end = el('span', 'tile-end');
  if (value !== undefined && value !== null && value !== '') {
    const v = el('span', 'tile-value-wrap');
    v.appendChild(value instanceof Node ? value : el('span', { class: 'tile-value', text: String(value) }));
    if (valueSub) v.appendChild(el('span', { class: 'tile-value-sub', text: String(valueSub) }));
    end.appendChild(v);
  }
  if (pill) end.appendChild(pill instanceof Node ? pill : statusPill(pill.status || status, pill.text));
  else if (status && !pill && value === undefined) end.appendChild(statusPill(status));
  if (actions) appendChildren(end, Array.isArray(actions) ? actions : [actions]);
  if (chevron || (interactive && chevron !== false && !actions)) end.appendChild(el('span', { class: 'tile-chevron', 'aria-hidden': 'true', text: '›' }));
  if (end.childNodes.length) node.appendChild(end);
  return node;
}

/**
 * Stat tile: the same tile with a large value and a 12-point sparkline under it.
 * @returns {HTMLElement}
 */
export function statTile({ emoji, label, value, unit, sub, status, spark, href, onClick, ariaLabel, id, className = '' } = {}) {
  const interactive = Boolean(href || onClick);
  const tag = href ? 'a' : onClick ? 'button' : 'div';
  const node = el(tag, { class: `tile tile--stat${interactive ? ' tile--action' : ''}${className ? ` ${className}` : ''}`, id });
  if (href) node.href = href;
  if (tag === 'button') node.type = 'button';
  if (onClick) node.addEventListener('click', onClick);
  const s = normStatus(status);
  node.setAttribute('aria-label', ariaLabel || `${label}: ${value ?? '—'}${unit ? ` ${unit}` : ''}${sub ? `, ${sub}` : ''}, ${STATUS[s].label}`);

  const head = el('span', 'stat-head', emojiBox(emoji), el('span', { class: 'stat-label', text: label ?? '' }), statusDot(s),
    interactive ? el('span', { class: 'stat-chevron', 'aria-hidden': 'true', text: '›' }) : null);
  const valueRow = el('span', 'stat-value-row',
    el('span', { class: 'stat-value', text: value ?? '—' }),
    unit ? el('span', { class: 'stat-unit', text: unit }) : null,
  );
  node.appendChild(head);
  node.appendChild(valueRow);
  if (sub) node.appendChild(el('span', { class: 'stat-sub', text: sub }));
  if (spark && spark.length > 1) node.appendChild(sparkline(spark, { status: s }));
  else node.appendChild(el('span', { class: 'spark spark--empty', 'aria-hidden': 'true' }));
  return node;
}

/**
 * Empty state: one emoji and one sentence naming the next click.
 * @param {string} emoji
 * @param {string} sentence
 * @param {{action?: Node, className?: string}} [opts]
 */
export function emptyState(emoji, sentence, { action, className = '' } = {}) {
  return el('div', { class: `empty${className ? ` ${className}` : ''}` },
    el('span', { class: 'empty-emoji', 'aria-hidden': 'true', text: emoji }),
    el('p', { class: 'empty-text', text: sentence }),
    action || null,
  );
}

// ------------------------------------------------------------------ layout

/**
 * A titled section of the dashboard grid.
 * @returns {HTMLElement & {head: HTMLElement, titleEl: HTMLElement, actionsEl: HTMLElement, body: HTMLElement}}
 */
export function section({ title, subtitle, actions, span = 12, layout = 'stack', id, className = '', level = 2 } = {}) {
  const node = el('section', { class: `section span-${span}${className ? ` ${className}` : ''}`, id });
  const head = el('div', 'section-head');
  const titleWrap = el('div', 'section-title-wrap');
  // tabindex -1: the last-resort focus target when a list inside the section empties (replaceKeepingFocus)
  const titleEl = el(`h${level}`, { class: 'section-title', text: title ?? '', tabIndex: -1 });
  titleWrap.appendChild(titleEl);
  if (subtitle) titleWrap.appendChild(el('p', { class: 'section-sub', text: subtitle }));
  head.appendChild(titleWrap);
  const actionsEl = el('div', 'section-actions');
  if (actions) appendChildren(actionsEl, Array.isArray(actions) ? actions : [actions]);
  head.appendChild(actionsEl);
  if (title || actions) node.appendChild(head);
  const body = el('div', `section-body section-body--${layout}`);
  node.appendChild(body);
  if (id) node.setAttribute('aria-labelledby', `${id}-title`), titleEl.id = `${id}-title`;
  node.head = head;
  node.titleEl = titleEl;
  node.actionsEl = actionsEl;
  node.body = body;
  return node;
}

/** The 12-column dashboard grid. */
export function grid(...children) {
  return el('div', 'grid', ...children);
}

/** A vertical stack of sections (used for the two Home columns). */
export function column(...children) {
  return el('div', 'col', ...children);
}

/** A right-aligned row of controls for the page header. */
export function pageHeaderControls(...nodes) {
  return el('div', 'controls', ...nodes);
}

/**
 * A table inside a tile (desktop density). Sortable headers when `sortable` is set on a column.
 * @returns {HTMLElement & {update(rows: object[]): void}}
 */
export function table({ columns = [], rows = [], caption, sortKey = null, sortDir = 'asc', ariaLabel, rowKey, onRowClick, emptyText = 'Nothing here yet.' } = {}) {
  const wrap = el('div', 'table-wrap');
  const tableEl = el('table', 'table');
  if (ariaLabel) tableEl.setAttribute('aria-label', ariaLabel);
  if (caption) tableEl.appendChild(el('caption', { class: 'sr-only', text: caption }));
  const thead = el('thead');
  const tbody = el('tbody');
  const state = { rows: rows.slice(), sortKey, sortDir };

  function sorted() {
    if (!state.sortKey) return state.rows;
    const col = columns.find((c) => c.key === state.sortKey);
    const dir = state.sortDir === 'desc' ? -1 : 1;
    return state.rows.slice().sort((a, b) => {
      const va = col && col.sortValue ? col.sortValue(a) : a[state.sortKey];
      const vb = col && col.sortValue ? col.sortValue(b) : b[state.sortKey];
      if (va === vb) return 0;
      if (va === null || va === undefined) return 1;
      if (vb === null || vb === undefined) return -1;
      return (va < vb ? -1 : 1) * dir;
    });
  }

  function renderHead() {
    const tr = el('tr');
    for (const col of columns) {
      const th = el('th', { scope: 'col', class: `align-${col.align || 'start'}` });
      if (col.width) th.style.width = col.width;
      if (col.sortable) {
        const active = state.sortKey === col.key;
        th.setAttribute('aria-sort', active ? (state.sortDir === 'desc' ? 'descending' : 'ascending') : 'none');
        const btn = el('button', { class: 'th-sort', type: 'button', text: col.label });
        btn.appendChild(el('span', { class: 'th-arrow', 'aria-hidden': 'true', text: active ? (state.sortDir === 'desc' ? '↓' : '↑') : '↕' }));
        btn.addEventListener('click', () => {
          if (state.sortKey === col.key) state.sortDir = state.sortDir === 'asc' ? 'desc' : 'asc';
          else { state.sortKey = col.key; state.sortDir = 'asc'; }
          renderHead();
          renderBody();
        });
        th.appendChild(btn);
      } else {
        th.textContent = col.label;
      }
      tr.appendChild(th);
    }
    thead.replaceChildren(tr);
  }

  function renderBody() {
    const list = sorted();
    if (!list.length) {
      const td = el('td', { colspan: String(Math.max(1, columns.length)), class: 'table-empty', text: emptyText });
      tbody.replaceChildren(el('tr', td));
      return;
    }
    const frag = document.createDocumentFragment();
    list.forEach((row, i) => {
      const tr = el('tr');
      if (rowKey) tr.dataset.key = String(rowKey(row));
      if (onRowClick) {
        tr.tabIndex = 0;
        tr.classList.add('row--action');
        tr.addEventListener('click', () => onRowClick(row));
        tr.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onRowClick(row); } });
      }
      columns.forEach((col, j) => {
        const raw = row[col.key];
        const content = col.format ? col.format(raw, row, i) : raw;
        const cell = el(j === 0 && col.header !== false ? 'th' : 'td', { class: `align-${col.align || 'start'}${col.numeric ? ' numeric' : ''}` });
        if (j === 0) cell.setAttribute('scope', 'row');
        if (content instanceof Node) cell.appendChild(content);
        else cell.textContent = content === null || content === undefined ? '—' : String(content);
        tr.appendChild(cell);
      });
      frag.appendChild(tr);
    });
    tbody.replaceChildren(frag);
  }

  renderHead();
  renderBody();
  tableEl.appendChild(thead);
  tableEl.appendChild(tbody);
  wrap.appendChild(tableEl);
  wrap.update = (next) => { state.rows = (next || []).slice(); renderBody(); };
  return wrap;
}

// ------------------------------------------------------------------ controls

/**
 * A button. Variants: ghost (default, 1 px line), filled (accent), text, danger.
 * @returns {HTMLButtonElement & {setBusy(busy: boolean): void, setLabel(text: string): void}}
 */
export function button({ label, emoji, variant = 'ghost', size = 'md', full = false, type = 'button', onClick, disabled = false, ariaLabel, busyLabel, title, id, className = '' } = {}) {
  const node = el('button', { class: `btn btn--${variant}${size === 'sm' ? ' btn--sm' : ''}${full ? ' btn--full' : ''}${className ? ` ${className}` : ''}`, type, id, title });
  const labelEl = el('span', 'btn-label');
  if (emoji) node.appendChild(el('span', { class: 'btn-emoji', 'aria-hidden': 'true', text: emoji }));
  labelEl.textContent = label ?? '';
  node.appendChild(labelEl);
  node.disabled = Boolean(disabled);
  if (ariaLabel) node.setAttribute('aria-label', ariaLabel);
  if (onClick) node.addEventListener('click', onClick);
  let idle = label ?? '';
  node.setLabel = (text) => { idle = text; if (node.getAttribute('aria-busy') !== 'true') labelEl.textContent = text; };
  node.setBusy = (busy) => {
    if (busy) {
      node.setAttribute('aria-busy', 'true');
      node.disabled = true;
      labelEl.textContent = busyLabel || idle;
      node.classList.add('is-busy');
    } else {
      node.removeAttribute('aria-busy');
      node.disabled = Boolean(disabled);
      labelEl.textContent = idle;
      node.classList.remove('is-busy');
    }
  };
  return node;
}

/**
 * A horizontal strip of single-select chips (radio semantics; ←/→ move the selection).
 * @returns {HTMLElement & {value: string, set(value: string, opts?: {silent?: boolean}): void}}
 */
export function chipRow({ options = [], value, onChange, ariaLabel, name, className = '' } = {}) {
  const row = el('div', { class: `chips${className ? ` ${className}` : ''}`, role: 'radiogroup' });
  if (ariaLabel) row.setAttribute('aria-label', ariaLabel);
  if (name) row.dataset.name = name;
  let current = value;
  const chips = options.map((opt) => {
    const chip = el('button', { class: 'chip', type: 'button', role: 'radio', title: opt.title, dataset: { value: String(opt.value) } });
    if (opt.emoji) chip.appendChild(el('span', { class: 'chip-emoji', 'aria-hidden': 'true', text: opt.emoji }));
    chip.appendChild(el('span', { class: 'chip-label', text: opt.label }));
    chip.addEventListener('click', () => set(opt.value));
    chip.addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
      e.preventDefault();
      const idx = options.findIndex((o) => o.value === current);
      const next = options[(idx + (e.key === 'ArrowRight' ? 1 : options.length - 1)) % options.length];
      set(next.value);
      chips[options.indexOf(next)].focus();
    });
    row.appendChild(chip);
    return chip;
  });
  function paint() {
    chips.forEach((chip, i) => {
      const on = options[i].value === current;
      chip.setAttribute('aria-checked', on ? 'true' : 'false');
      chip.tabIndex = on || (current === undefined && i === 0) ? 0 : -1;
    });
  }
  function set(next, { silent = false } = {}) {
    const changed = next !== current;
    current = next;
    paint();
    if (changed && !silent && onChange) onChange(next);
  }
  paint();
  Object.defineProperty(row, 'value', { get: () => current, set: (v) => set(v, { silent: true }) });
  row.set = set;
  return row;
}

/**
 * A switch row in the tile recipe (emoji · label + one-line explainer · switch), or a bare switch.
 * @returns {HTMLLabelElement & {input: HTMLInputElement, set(checked: boolean): void}}
 */
export function toggle({ label, description, emoji, checked = false, onChange, disabled = false, id, bare = false, name } = {}) {
  const input = el('input', { type: 'checkbox', role: 'switch', class: 'switch-input', id, name });
  input.checked = Boolean(checked);
  input.disabled = Boolean(disabled);
  input.setAttribute('aria-checked', input.checked ? 'true' : 'false');
  input.addEventListener('change', () => {
    input.setAttribute('aria-checked', input.checked ? 'true' : 'false');
    if (onChange) onChange(input.checked, input);
  });
  const track = el('span', { class: 'switch-track', 'aria-hidden': 'true' }, el('span', 'switch-thumb'));
  const sw = el('span', 'switch', input, track);
  let node;
  if (bare) {
    node = el('label', { class: 'switch-wrap' }, sw, label ? el('span', { class: 'sr-only', text: label }) : null);
  } else {
    node = el('label', 'tile toggle');
    if (emoji) node.appendChild(emojiBox(emoji));
    const body = el('span', 'tile-body', el('span', { class: 'tile-title', text: label ?? '' }));
    if (description) body.appendChild(el('span', { class: 'tile-sub', text: description }));
    node.appendChild(body);
    node.appendChild(el('span', 'tile-end', sw));
  }
  node.input = input;
  node.set = (value) => { input.checked = Boolean(value); input.setAttribute('aria-checked', input.checked ? 'true' : 'false'); };
  return node;
}

let fieldSeq = 0;

/** A labelled form field with helper text and an error slot. */
export function field({ label, input: control, help, error, id } = {}) {
  const inputId = id || (control && control.id) || `f-${++fieldSeq}`;
  if (control && !control.id) control.id = inputId;
  const node = el('div', 'field');
  node.appendChild(el('label', { class: 'field-label', htmlFor: inputId, text: label ?? '' }));
  if (control) node.appendChild(control);
  const helpEl = el('p', { class: 'field-help', id: `${inputId}-help`, text: help || '' });
  helpEl.hidden = !help;
  const errorEl = el('p', { class: 'field-error', id: `${inputId}-error`, role: 'alert', text: error || '' });
  errorEl.hidden = !error;
  node.appendChild(helpEl);
  node.appendChild(errorEl);
  if (control) {
    const described = [help ? helpEl.id : null, error ? errorEl.id : null].filter(Boolean).join(' ');
    if (described) control.setAttribute('aria-describedby', described);
    if (error) control.setAttribute('aria-invalid', 'true');
  }
  node.setError = (text) => {
    errorEl.textContent = text || '';
    errorEl.hidden = !text;
    if (control) {
      if (text) control.setAttribute('aria-invalid', 'true');
      else control.removeAttribute('aria-invalid');
    }
  };
  return node;
}

/** A text-like input (height 44, radius 22). */
export function input({ type = 'text', id, name, value, placeholder, inputmode, required, min, max, step, autocomplete, onInput, onChange, ariaLabel, disabled } = {}) {
  const node = el('input', { class: 'input', type, id, name, placeholder, inputmode, min, max, step, autocomplete });
  if (value !== undefined && value !== null) node.value = String(value);
  if (required) node.required = true;
  if (disabled) node.disabled = true;
  if (ariaLabel) node.setAttribute('aria-label', ariaLabel);
  if (onInput) node.addEventListener('input', onInput);
  if (onChange) node.addEventListener('change', onChange);
  return node;
}

/** A native select styled like an input. */
export function select({ options = [], value, onChange, id, name, ariaLabel } = {}) {
  const node = el('select', { class: 'input input--select', id, name });
  if (ariaLabel) node.setAttribute('aria-label', ariaLabel);
  for (const opt of options) {
    const o = el('option', { value: String(opt.value), text: opt.label });
    if (opt.value === value) o.selected = true;
    node.appendChild(o);
  }
  if (onChange) node.addEventListener('change', onChange);
  return node;
}

/** A `<details>` disclosure ("More options"). */
export function disclosure({ summary, open = false, children, className = '' } = {}) {
  const node = el('details', { class: `disclosure${className ? ` ${className}` : ''}` });
  if (open) node.open = true;
  node.appendChild(el('summary', { text: summary ?? 'More options' }));
  const body = el('div', 'disclosure-body');
  if (children) appendChildren(body, Array.isArray(children) ? children : [children]);
  node.appendChild(body);
  return node;
}

/** A keyboard key. */
export function kbd(text) {
  return el('kbd', { text });
}

/**
 * A tile that hosts a chart: title left, current value right, chart underneath.
 * @returns {HTMLElement & {setValue(text: string): void, setNote(text: string): void, chart: object}}
 */
export function chartTile({ title, value, chart, span = 6, className = '' } = {}) {
  const node = el('div', { class: `tile chart-tile span-${span}${className ? ` ${className}` : ''}` });
  const titleEl = el('h3', { class: 'chart-tile-title', text: title ?? '' });
  const valueEl = el('span', { class: 'chart-tile-now numeric', text: value ?? '' });
  node.appendChild(el('div', 'chart-tile-head', titleEl, valueEl));
  if (chart && chart.el) node.appendChild(chart.el);
  const note = el('p', { class: 'muted chart-tile-note', text: '' });
  note.hidden = true;
  node.appendChild(note);
  node.chart = chart;
  node.setValue = (text) => { valueEl.textContent = text ?? ''; };
  node.setNote = (text) => { note.textContent = text || ''; note.hidden = !text; };
  return node;
}

// ------------------------------------------------------------------ overlays

let openPanel = null;

function supportsClosedBy() {
  return 'closedBy' in HTMLDialogElement.prototype;
}

function lightDismissFallback(dialog) {
  if (supportsClosedBy()) return;
  dialog.addEventListener('click', (event) => {
    if (event.target !== dialog) return;
    const rect = dialog.getBoundingClientRect();
    const inside = rect.top <= event.clientY && event.clientY <= rect.bottom && rect.left <= event.clientX && event.clientX <= rect.right;
    if (!inside) dialog.close();
  });
}

/**
 * Open a right-side panel (520 px drawer; bottom sheet below 900 px). Only one is open at a time.
 * @returns {{el: HTMLDialogElement, body: HTMLElement, foot: HTMLElement, close(result?: string): void, setTitle(text: string): void, setBusy(busy: boolean): void}}
 */
export function panel({ title = '', name = '', onClose, ariaLabel } = {}) {
  if (openPanel) openPanel.close('replaced');
  const dialog = el('dialog', { class: 'panel', closedby: 'any' });
  if (name) dialog.dataset.panel = name;
  const titleId = `panel-title-${Date.now().toString(36)}`;
  const titleEl = el('h2', { class: 'panel-title', id: titleId, text: title });
  dialog.setAttribute('aria-labelledby', titleId);
  if (ariaLabel) dialog.setAttribute('aria-label', ariaLabel);
  const closeBtn = el('button', { class: 'panel-close', type: 'button', 'aria-label': 'Close', text: '✕' });
  closeBtn.addEventListener('click', () => dialog.close('dismiss'));
  const head = el('div', 'panel-head', titleEl, closeBtn);
  const body = el('div', 'panel-body');
  const foot = el('div', 'panel-foot');
  dialog.appendChild(head);
  dialog.appendChild(body);
  dialog.appendChild(foot);
  lightDismissFallback(dialog);
  document.body.appendChild(dialog);
  const handle = {
    el: dialog,
    body,
    foot,
    close(result) {
      if (dialog.open) dialog.close(result || 'close');
    },
    setTitle(text) {
      titleEl.textContent = text;
    },
    setBusy(busy) {
      dialog.classList.toggle('is-busy', Boolean(busy));
      dialog.setAttribute('aria-busy', busy ? 'true' : 'false');
    },
  };
  dialog.addEventListener('close', () => {
    if (openPanel === handle) openPanel = null;
    const result = dialog.returnValue;
    dialog.remove();
    if (onClose) onClose(result);
  });
  openPanel = handle;
  dialog.showModal();
  return handle;
}

/** The currently open panel handle, or null. */
export function currentPanel() {
  return openPanel;
}

/**
 * A small centred confirm dialog. Resolves true when confirmed.
 * @returns {Promise<boolean>}
 */
export function confirm({ title = 'Are you sure?', message = '', confirmLabel = 'Confirm', cancelLabel = 'Cancel', danger = false } = {}) {
  return new Promise((resolve) => {
    const dialog = el('dialog', { class: 'confirm', closedby: 'any' });
    const titleId = `confirm-title-${Date.now().toString(36)}`;
    dialog.setAttribute('aria-labelledby', titleId);
    const form = el('form', { method: 'dialog' });
    form.appendChild(el('h2', { class: 'confirm-title', id: titleId, text: title }));
    if (message) form.appendChild(el('p', { class: 'confirm-text', text: message }));
    const cancel = button({ label: cancelLabel, variant: 'ghost' });
    cancel.value = 'cancel';
    cancel.type = 'submit';
    const ok = button({ label: confirmLabel, variant: danger ? 'danger' : 'filled' });
    ok.value = 'ok';
    ok.type = 'submit';
    form.appendChild(el('div', 'confirm-actions', cancel, ok));
    dialog.appendChild(form);
    lightDismissFallback(dialog);
    dialog.addEventListener('close', () => {
      const result = dialog.returnValue === 'ok';
      dialog.remove();
      resolve(result);
    });
    document.body.appendChild(dialog);
    dialog.showModal();
    cancel.focus();
  });
}

let snackTimer = null;

function snackRegion() {
  let region = document.getElementById('snackbar');
  if (!region) {
    region = el('div', { id: 'snackbar', class: 'snackbar-region', 'aria-live': 'polite', 'aria-atomic': 'true' });
    document.body.appendChild(region);
  }
  return region;
}

/**
 * Show a snackbar (one at a time, 6 s, polite live region; never steals focus).
 * The action may return (or resolve to) the element that should take focus afterwards — "Undo"
 * returns the restored item. Focus only moves when it was on the snackbar's own button, which
 * removes itself; otherwise it falls back to the page heading instead of <body>.
 * @param {string} message
 * @param {{action?: {label: string, onClick: () => (HTMLElement|void|Promise<HTMLElement|void>)}, duration?: number}} [opts]
 * @returns {{dismiss(): void}}
 */
export function snackbar(message, { action, duration = 6000 } = {}) {
  const region = snackRegion();
  clearTimeout(snackTimer);
  const node = el('div', { class: 'snackbar', role: 'status' }, el('span', { class: 'snackbar-text', text: message }));
  let timer = null;
  const dismiss = () => {
    // only this snackbar's own timer: dismissing one that was already replaced never touches the newer one
    clearTimeout(timer);
    if (snackTimer === timer) snackTimer = null;
    if (node.isConnected) {
      node.classList.add('is-leaving');
      const remove = () => node.remove();
      const ms = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--dur')) || 0;
      if (ms > 0) setTimeout(remove, ms + 20);
      else remove();
    }
  };
  if (action) {
    const btn = el('button', { class: 'snackbar-action', type: 'button', text: action.label });
    btn.addEventListener('click', () => {
      const hadFocus = node.contains(document.activeElement);
      dismiss();
      const result = action.onClick();
      if (!hadFocus) return;
      Promise.resolve(result).catch(() => null).then((target) => {
        const current = document.activeElement;
        // the user (or the action) already put focus somewhere real: leave it there
        if (current && current !== document.body && current.isConnected && !node.contains(current)) return;
        const next = target instanceof HTMLElement && target.isConnected ? target : document.getElementById('page-title');
        if (next) next.focus({ preventScroll: true });
      });
    });
    node.appendChild(btn);
  }
  region.replaceChildren(node);
  timer = setTimeout(dismiss, duration);
  snackTimer = timer;
  return { dismiss };
}

/** Undo snackbar shorthand. */
snackbar.undo = (message, onUndo, { duration = 6000 } = {}) => snackbar(message, { action: { label: 'Undo', onClick: onUndo }, duration });

/**
 * The keyboard-shortcuts dialog ("?").
 * @param {Array<[string|string[], string]>} rows
 */
export function shortcutsDialog(rows) {
  const dialog = el('dialog', { class: 'confirm shortcuts', closedby: 'any' });
  const titleId = 'shortcuts-title';
  dialog.setAttribute('aria-labelledby', titleId);
  dialog.appendChild(el('h2', { class: 'confirm-title', id: titleId, text: 'Keyboard shortcuts' }));
  const list = el('dl', 'shortcut-list');
  for (const [keys, desc] of rows) {
    const dt = el('dt');
    for (const key of Array.isArray(keys) ? keys : [keys]) dt.appendChild(kbd(key));
    list.appendChild(dt);
    list.appendChild(el('dd', { text: desc }));
  }
  dialog.appendChild(list);
  const close = button({ label: 'Close', variant: 'ghost', onClick: () => dialog.close() });
  dialog.appendChild(el('div', 'confirm-actions', close));
  lightDismissFallback(dialog);
  dialog.addEventListener('close', () => dialog.remove());
  document.body.appendChild(dialog);
  dialog.showModal();
  return dialog;
}

export const meter = chartMeter;
export { sparkline };

export default {
  el, svgEl, replaceKeepingFocus, keepUnits, humanize, tile, statTile, emptyState, section, grid, column, pageHeaderControls,
  table, chartTile,
  button, chipRow, toggle, field, input, select, disclosure, kbd,
  panel, currentPanel, confirm, snackbar, shortcutsDialog,
  STATUS, statusLabel, statusMark, statusDot, statusPill, ackedPill, severityStatus, staleWords, meter, sparkline, fmt,
};
