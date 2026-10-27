/**
 * screens/stack.js — the Stack tab (SPEC §12.5) and the shared ladder primitive.
 *
 * Every service is drawn as its dependency chain: a header tile (emoji · label · reason or
 * "91 ms · GET /server/api ok" · status pill · ›) and the rungs under it, joined by a 1 px vertical rail.
 * Each rung shows its label, an optional message, the latency and ✅/⚠️/❌/—; the first failing
 * rung's message is the reason, rungs below it read "not reached". Groups are balanced over two
 * columns on desktop, the Storage group carries the assetstore meter, and the page header shows
 * "N of M healthy". Status frames patch text and classes in place — the DOM is rebuilt only when
 * the set of components or rungs changes.
 *
 * Exports for other screens:
 *   ladder(comp, {href, storage}) -> HTMLElement & {update(comp), comp}
 *   componentSubtitle(comp) -> string          ("91 ms · GET /server/api ok" | reason | "Not checked yet")
 *   latencyText(ms) -> string                  ("0.4 ms" | "91 ms" | "2.4 s" | "")
 *   rungMessage(rung) -> string                (message, "not available", "known issue · …", or a derived one:
 *                                               logs/cores/ports/HTTP code)
 *   isInformational(rung) -> boolean           (unknown only because the endpoint is not available here, or a known issue)
 *   isKnownIssue(rung) -> boolean              (informational because the answer is a known upstream bug: detail.known_issue)
 */
import { store } from '../store.js';
import {
  NBSP, el, emptyState, fmt, grid, humanize, keepUnits, meter, section, staleWords, statusLabel, statusMark, statusPill, tile,
} from '../ui.js';

const STORAGE_IDS = new Set(['assetstore', 'repository']);
const STATUS_ORDER = ['ok', 'warn', 'crit', 'unknown', 'off'];
/** The rung whose timing is the component's headline latency (SPEC §6); falls back to the last rung. */
const HEADLINE_RUNG = {
  postgres: 'postgres.query', solr: 'solr.search', backend: 'backend.rest', frontend: 'frontend.home',
  nginx: 'nginx.http', ssl: 'ssl.cert', assetstore: 'assetstore.path',
};

function normStatus(status) {
  return STATUS_ORDER.includes(status) ? status : 'unknown';
}

/** Latency for a rung: one decimal below 10 ms, then the shared ms/s formatting; empty for null. */
export function latencyText(ms) {
  if (ms === null || ms === undefined || Number.isNaN(Number(ms))) return '';
  const v = Number(ms);
  if (v < 10) return `${fmt.number(v, 1)}${NBSP}ms`;
  return fmt.ms(v);
}

/**
 * Header subtitle. The status pill next to it already says "Healthy", so a healthy component
 * reports what was measured — "1.1 ms · SELECT 1 ok", "7 checks passed", "27,576 items · 81,584
 * bitstreams" — and an unhealthy one its reason.
 */
export function componentSubtitle(comp) {
  if (!comp) return '';
  if (comp.status === 'ok') {
    const chain = comp.chain || [];
    const m = comp.metrics || {};
    const lat = latencyText(comp.latency_ms);
    const headline = chain.find((r) => r.id === HEADLINE_RUNG[comp.id]) || (lat ? chain[chain.length - 1] : null);
    if (lat && headline) return `${lat} · ${headline.label || headline.id} ok`;
    if (lat) return lat;
    if (chain.length) {
      const passed = chain.filter((r) => r.status === 'ok').length;
      const known = chain.filter(isKnownIssue).length;
      const info = chain.filter(isInformational).length - known;
      return `${fmt.plural(passed, 'check')} passed${info ? ` · ${info} not available` : ''}${known ? ` · ${fmt.plural(known, 'known issue')}` : ''}`;
    }
    if (m.items !== undefined && m.items !== null) return `${fmt.number(m.items)} items · ${fmt.number(m.bitstreams)} bitstreams`;
    return 'Up to date';
  }
  if (comp.reason) return humanize(comp.reason);
  if (comp.status === 'unknown') return 'Not checked yet';
  if (comp.status === 'off') return 'Switched off';
  return '';
}

/** An `unknown` rung that only says "this endpoint is not available here" (SPEC §6: `detail.informational`). */
export function isInformational(rung) {
  return Boolean(rung && rung.status === 'unknown' && rung.detail && rung.detail.informational);
}

/**
 * An informational rung whose answer is a known upstream bug rather than a missing endpoint:
 * `detail.known_issue` ("seo": DSpace 7.6.4–7.6.x, 8.2–8.4, 9.0–9.3 and 10.0 report DOWN while healthy).
 */
export function isKnownIssue(rung) {
  return isInformational(rung) && Boolean(rung.detail.known_issue);
}

/**
 * The line under a rung label: its message, or something useful derived from its detail
 * (error counts, core sizes, the listening address, the HTTP status code). Informational
 * rungs read "not available" (or "known issue") so they never look like a failure; the Security rungs word
 * their informational states themselves ("not available · sshd_config is not readable by Sentinel's user",
 * "no SSH server found", "no public address set"), so theirs read as they are.
 */
export function rungMessage(rung) {
  if (!rung) return '';
  if (isInformational(rung)) {
    // "—  not available", with the collector's reason when it gives one ("not exposed (HTTP 404)")
    const reason = rung.message ? String(rung.message).trim() : '';
    // the endpoint answered, but with a known upstream bug: "known issue · DSpace 9.2 reports DOWN while healthy (…)"
    if (isKnownIssue(rung)) return keepUnits(reason ? `known issue · ${reason}` : 'known issue');
    if (!reason || reason === 'not available' || reason === 'not monitored') return reason || 'not available';
    if (reason.startsWith('not available') || String(rung.id || '').startsWith('security.')) return keepUnits(reason);
    return keepUnits(`not available · ${reason}`);
  }
  if (rung.message) return humanize(String(rung.message));
  const d = rung.detail || {};
  const id = String(rung.id || '');
  if (id.startsWith('logs.') && d.errors !== undefined) {
    return `${fmt.plural(d.errors, 'error')} · ${fmt.plural(d.warnings || 0, 'warning')}`;
  }
  if (id.startsWith('solr.core.') && d.docs !== undefined && d.docs !== null) return `${fmt.number(d.docs)} docs`;
  if (id.startsWith('ports.') && d.addr) return String(d.addr);
  if (id.endsWith('.rest') && d.dspaceVersion) return String(d.dspaceVersion);
  if (d.status_code !== undefined && d.status_code !== null) return `HTTP ${d.status_code}`;
  return '';
}

function subtitleClass(status) {
  return `tile-sub${status === 'crit' ? ' tile-sub--crit' : status === 'warn' ? ' tile-sub--warn' : ''}`;
}

// ------------------------------------------------------------------ rungs

function rungClass(rung, s) {
  return `rung rung--${s}${isInformational(rung) ? ' rung--info' : ''}`;
}

function rungSrLabel(rung, s) {
  if (isKnownIssue(rung)) return 'Known issue';
  return isInformational(rung) ? 'Not available' : statusLabel(s);
}

function buildRung(rung) {
  const s = normStatus(rung.status);
  const li = el('li', { class: rungClass(rung, s), dataset: { rung: rung.id } });
  const node = el('span', { class: 'rung-node', 'aria-hidden': 'true' });
  const label = el('span', { class: 'rung-label', text: rung.label || rung.id });
  const msg = el('span', { class: 'rung-msg', text: rungMessage(rung) });
  msg.hidden = !msg.textContent;
  const main = el('span', 'rung-main', label, msg);
  const latency = el('span', { class: 'rung-latency numeric', text: latencyText(rung.latency_ms) });
  const mark = el('span', { class: 'rung-mark', 'aria-hidden': 'true', text: statusMark(s) });
  const sr = el('span', { class: 'sr-only', text: rungSrLabel(rung, s) });
  const end = el('span', 'rung-end', latency, mark, sr);
  li.append(node, main, end);
  return li;
}

function patchRung(li, rung) {
  const s = normStatus(rung.status);
  li.className = rungClass(rung, s);
  const label = li.querySelector('.rung-label');
  const text = rung.label || rung.id;
  if (label.textContent !== text) label.textContent = text;
  const msg = li.querySelector('.rung-msg');
  const message = rungMessage(rung);
  if (msg.textContent !== message) msg.textContent = message;
  msg.hidden = !message;
  const latency = li.querySelector('.rung-latency');
  const lat = latencyText(rung.latency_ms);
  if (latency.textContent !== lat) latency.textContent = lat;
  li.querySelector('.rung-mark').textContent = statusMark(s);
  li.querySelector('.sr-only').textContent = rungSrLabel(rung, s);
}

function chainKey(comp) {
  return ((comp && comp.chain) || []).map((r) => r.id).join(',');
}

// ------------------------------------------------------------------ assetstore meter

function storageCaption(m) {
  if (!m || m.total === undefined || m.total === null) return 'Waiting for the first check';
  // storage sizes read in GB with one decimal everywhere (SPEC §12.6), as in Server › Mounts
  const parts = [`${fmt.gb(m.used)} used of ${fmt.gb(m.total)}`, `${fmt.gb(m.free)} free`];
  const growth = Number(m.growth_bytes_per_day) || 0;
  if (growth > 0) {
    parts.push(`+${fmt.bytes(growth)}/day`);
    const days = Math.floor(Number(m.free) / growth);
    if (Number.isFinite(days) && days < 3650) parts.push(`full in about ${fmt.plural(days, 'day')}`);
  }
  return parts.join(' · ');
}

function buildMeterBlock(comp) {
  const m = comp.metrics || {};
  const bar = meter({ value: Number(m.used) || 0, max: Number(m.total) || 100, status: normStatus(comp.status), label: fmt.pct(m.pct), ariaLabel: 'Assetstore used' });
  const caption = el('p', { class: 'ladder-caption', text: storageCaption(m) });
  const block = el('div', 'ladder-meter', bar, caption);
  block.update = (next) => {
    const nm = next.metrics || {};
    bar.update({ value: Number(nm.used) || 0, max: Number(nm.total) || 100, status: normStatus(next.status), label: fmt.pct(nm.pct) });
    const text = storageCaption(nm);
    if (caption.textContent !== text) caption.textContent = text;
  };
  return block;
}

// ------------------------------------------------------------------ ladder group

/**
 * One ladder: header tile + (optional assetstore meter) + rungs. Patched in place by `.update()`.
 * @param {object} comp ComponentHealth payload
 * @param {{href?: string|null, storage?: boolean}} [opts] `href` makes the header a link (null = static header);
 *   `storage` adds the assetstore meter block under the header.
 * @returns {HTMLElement & {update(comp: object): void, comp: object}}
 */
export function ladder(comp, { href = `#/stack/${encodeURIComponent(comp.id)}`, storage = false, pillText = null } = {}) {
  const status = normStatus(comp.status);
  const pillLabel = (c) => (pillText ? pillText(c) : statusLabel(normStatus(c.status)));
  const wrap = el('div', { class: `ladder ladder--${status}`, role: 'group', 'aria-label': `${comp.label} checks`, dataset: { component: comp.id, status } });
  const subText = componentSubtitle(comp);
  const head = tile({
    emoji: comp.emoji, title: comp.label,
    subtitle: el('span', { class: subtitleClass(status), text: subText }),
    pill: { status, text: pillLabel(comp) }, chevron: Boolean(href), href: href || undefined,
    ariaLabel: href ? `${comp.label}: ${subText || statusLabel(status)}` : undefined,
    className: 'ladder-head',
  });
  head.dataset.key = comp.id;
  const headPill = head.querySelector('.pill');
  if (headPill) headPill.dataset.label = pillLabel(comp);
  // the group wrapper carries the name; naming the list too made readers announce it twice
  const list = el('ol', { class: 'rungs' });
  const meterBlock = storage ? buildMeterBlock(comp) : null;
  const empty = el('li', { class: 'rung rung--empty', text: '' });

  function paintRungs(next) {
    const chain = next.chain || [];
    if (!chain.length) {
      empty.textContent = next.status === 'ok' && Object.keys(next.metrics || {}).length ? 'Metrics only — no checks to climb.' : 'Checks appear after the first run.';
      list.replaceChildren(empty);
      return;
    }
    if (chainKey(next) !== chainKey(wrap.comp) || list.children.length !== chain.length || list.contains(empty)) {
      list.replaceChildren(...chain.map(buildRung));
    } else {
      chain.forEach((rung, i) => patchRung(list.children[i], rung));
    }
  }

  wrap.comp = null;
  wrap.append(head, ...(meterBlock ? [meterBlock] : []), list);
  paintRungs(comp);
  wrap.comp = comp;

  wrap.update = (next) => {
    const s = normStatus(next.status);
    wrap.className = `ladder ladder--${s}`;
    wrap.dataset.status = s;
    const sub = head.querySelector('.tile-sub');
    const text = componentSubtitle(next);
    if (sub) {
      if (sub.textContent !== text) sub.textContent = text;
      sub.className = subtitleClass(s);
    }
    const title = head.querySelector('.tile-title');
    if (title && title.textContent !== next.label) title.textContent = next.label;
    const pill = head.querySelector('.pill');
    const label = pillLabel(next);
    if (pill && (!pill.classList.contains(`pill--${s}`) || pill.dataset.label !== label)) {
      const fresh = statusPill(s, label);
      fresh.dataset.label = label;
      pill.replaceWith(fresh);
    }
    if (href) head.setAttribute('aria-label', `${next.label}: ${text || statusLabel(s)}`);
    if (meterBlock) meterBlock.update(next);
    paintRungs(next);
    wrap.comp = next;
  };
  return wrap;
}

// ------------------------------------------------------------------ the tab

function weight(comp) {
  return 2 + ((comp.chain && comp.chain.length) || 0);
}

/** Split ladder components over two columns, keeping order and balancing height. */
function balance(comps) {
  const cols = [[], []];
  const heights = [0, 0];
  for (const comp of comps) {
    const i = heights[0] <= heights[1] ? 0 : 1;
    cols[i].push(comp);
    heights[i] += weight(comp);
  }
  return cols;
}

function repositoryTiles(repoComp, status, components) {
  const m = (repoComp && repoComp.metrics && Object.keys(repoComp.metrics).length ? repoComp.metrics : status && status.repository) || null;
  const postgres = components.find((c) => c.id === 'postgres');
  const nodes = [];
  const repoSub = m
    ? `${fmt.number(m.items)} items · ${fmt.number(m.bitstreams)} bitstreams${m.estimated ? ' (estimated)' : ''}`
    : 'Counting items and bitstreams — numbers appear after the first repository check.';
  const repoTile = tile({
    emoji: repoComp ? repoComp.emoji : '📚', title: 'Repository', subtitle: repoSub,
    value: m ? fmt.gb(m.assetstore_bytes) : '—', valueSub: m ? 'in bitstreams' : undefined,
    pill: repoComp ? { status: repoComp.status } : undefined, // like the Database tile beside it
    href: '#/stack/repository', ariaLabel: `Repository: ${repoSub}${repoComp ? `, ${statusLabel(repoComp.status)}` : ''}`,
  });
  repoTile.dataset.key = 'repository';
  nodes.push(repoTile);
  const dbSize = m && m.db_size !== undefined ? m.db_size : postgres && postgres.metrics ? postgres.metrics.db_size_bytes : null;
  const dbTile = tile({
    emoji: '🐘', title: 'Database', subtitle: postgres ? `PostgreSQL · ${postgres.status === 'ok' ? `answers in ${latencyText(postgres.latency_ms) || '—'}` : componentSubtitle(postgres)}` : 'PostgreSQL on disk',
    value: fmt.gb(dbSize), valueSub: 'on disk',
    href: postgres ? '#/stack/postgres' : undefined, pill: postgres ? { status: postgres.status } : undefined,
    ariaLabel: postgres ? `Database: ${fmt.gb(dbSize)} on disk, ${statusLabel(postgres.status)}` : undefined,
  });
  dbTile.dataset.key = 'database';
  nodes.push(dbTile);
  return nodes;
}

/**
 * Render the Stack tab.
 * @param {HTMLElement} root
 * @param {object} params
 * @param {object} ctx
 * @returns {() => void} destroy
 */
export function render(root, params, ctx) {
  const view = grid();
  const servicesSection = section({ title: 'DSpace stack', subtitle: 'Every service as its dependency chain — the first failing rung is the reason.', layout: 'grid', id: 'stack-services' });
  const leftCol = el('div', 'col');
  const rightCol = el('div', 'col');
  const left = el('div', 'span-6', leftCol);
  const right = el('div', 'span-6', rightCol);
  servicesSection.body.append(left, right);
  const storageSection = section({ title: 'Storage', subtitle: 'Where the files and the database live.', layout: 'two', id: 'stack-storage' });
  const storageLeft = el('div', 'col');
  const storageRight = el('div', 'col');
  storageSection.body.append(storageLeft, storageRight);
  const emptyWrap = el('div', 'span-12');
  view.append(servicesSection, storageSection, emptyWrap);
  root.replaceChildren(view);

  const summary = el('span', { class: 'muted', id: 'stack-summary', text: '' });
  ctx.setControls(summary);

  /** @type {Map<string, HTMLElement>} component id → ladder */
  const ladders = new Map();
  let layoutKey = '';

  function keepFocus(fn) {
    const active = document.activeElement;
    const keyed = active && root.contains(active) ? active.closest('[data-key]') : null;
    const key = keyed ? keyed.dataset.key : null;
    fn();
    if (key) {
      // the same ladder's header, else the page heading (a module switched off took the ladder away)
      const next = root.querySelector(`[data-key="${CSS.escape(key)}"]`) || document.getElementById('page-title');
      if (next) next.focus({ preventScroll: true });
    }
  }

  function paint() {
    const status = store.get('status');
    const comps = (status && status.components) || [];
    const dspace = store.dspaceModulesEnabled();
    servicesSection.titleEl.textContent = dspace ? 'DSpace stack' : 'Services on this server';

    if (!status) {
      servicesSection.hidden = true;
      storageSection.hidden = true;
      emptyWrap.hidden = false;
      emptyWrap.replaceChildren(emptyState('📚', store.get('connection') === 'offline'
        ? 'Sentinel is unreachable — the ladders come back with the connection.'
        : 'Connecting to Sentinel — the ladders appear after the first check.'));
      summary.textContent = '';
      return;
    }
    if (!comps.length) {
      // no module is on: the server is still watched — say where, without a "turn me on" prompt or a 0-of-0 count
      servicesSection.hidden = true;
      storageSection.hidden = true;
      emptyWrap.hidden = false;
      emptyWrap.replaceChildren(emptyState('💻', 'Only the server is watched — its vitals are on Home.'));
      summary.textContent = '';
      return;
    }
    emptyWrap.hidden = true;
    emptyWrap.replaceChildren();

    const healthy = comps.filter((c) => c.status === 'ok').length;
    const stale = store.get('stale');
    const count = `${healthy} of ${comps.length} healthy`;
    // out of date: the count is the last known one, and the header says why
    const text = stale ? `${staleWords(stale, store.lastCheckTs()).short} · last known: ${count}` : count;
    if (summary.textContent !== text) summary.textContent = text;

    const services = comps.filter((c) => !STORAGE_IDS.has(c.id));
    const assetstore = comps.find((c) => c.id === 'assetstore');
    const repository = comps.find((c) => c.id === 'repository');

    const key = comps.map((c) => `${c.id}:${(c.chain || []).length}`).join('|');
    if (key !== layoutKey) {
      layoutKey = key;
      keepFocus(() => {
        ladders.clear();
        const [a, b] = balance(services);
        leftCol.replaceChildren(...a.map((c) => { const l = ladder(c); ladders.set(c.id, l); return l; }));
        rightCol.replaceChildren(...b.map((c) => { const l = ladder(c); ladders.set(c.id, l); return l; }));
        right.hidden = b.length === 0;
        left.hidden = a.length === 0;
        servicesSection.hidden = services.length === 0;
        if (assetstore) {
          const l = ladder(assetstore, { storage: true });
          ladders.set('assetstore', l);
          storageLeft.replaceChildren(l);
        } else {
          storageLeft.replaceChildren();
        }
        storageLeft.hidden = !assetstore;
        storageRight.hidden = !repository;
        storageRight.replaceChildren(...(repository ? repositoryTiles(repository, status, comps) : []));
        storageSection.hidden = !assetstore && !repository;
      });
      return;
    }
    for (const comp of comps) {
      const l = ladders.get(comp.id);
      if (l) l.update(comp);
    }
    if (repository) keepFocus(() => storageRight.replaceChildren(...repositoryTiles(repository, status, comps)));
  }

  paint();
  const unsubscribe = store.subscribe(['status', 'connection', 'stale'], paint);
  return () => {
    unsubscribe();
  };
}
