/**
 * screens/home.js — the Home tab (SPEC §12.5).
 *
 * Pulse band (12) → Server vitals 4 × (3) → left column (6) DSpace stack / Services →
 * right column (6) Alerts + Repository → footer line. Re-renders section bodies from store
 * changes, keeps scroll and focus (tiles carry data-key).
 */
import api from '../api.js';
import { store } from '../store.js';
import {
  NBSP, ackedPill, button, column, el, emptyState, fmt, grid, humanize, keepUnits, replaceKeepingFocus, section, severityStatus,
  snackbar, staleWords, statTile, statusLabel, tile,
} from '../ui.js';
import { componentSubtitle } from './stack.js';

const DISK_KEYS = ['disk'];

function worstStatus(list) {
  const rank = { ok: 0, unknown: 1, warn: 2, crit: 3 };
  return list.reduce((acc, s) => (rank[s] > rank[acc] ? s : acc), 'ok');
}

function serverTiles(status) {
  const s = status && status.server ? status.server : null;
  const per = (s && s.per_metric) || {};
  const spark = (s && s.spark) || {};
  const has = s && s.cpu_pct !== undefined;
  const diskStatus = worstStatus(Object.entries(per).filter(([k]) => k === 'disk' || k.startsWith('disk:')).map(([, v]) => v).concat(DISK_KEYS.length ? [] : []));
  return [
    statTile({
      emoji: '💻', label: 'CPU', unit: '%', href: '#/server',
      value: has ? fmt.number(s.cpu_pct, s.cpu_pct < 10 ? 1 : 0) : '—',
      sub: has ? `${fmt.plural(s.cores, 'core')} · load ${fmt.load(s.load1)}` : 'Waiting for the first sample',
      status: per.cpu || 'unknown', spark: spark.cpu,
    }),
    statTile({
      emoji: '🧠', label: 'Memory', unit: '%', href: '#/server',
      value: has ? fmt.number(s.mem_pct, 0) : '—',
      sub: has ? `${fmt.gb(s.mem_used)} of ${fmt.gb(s.mem_total)}${s.swap_total ? ` · swap${NBSP}${fmt.pct(s.swap_pct)}` : ''}` : 'Waiting for the first sample',
      status: per.memory || 'unknown', spark: spark.mem,
    }),
    statTile({
      emoji: '💾', label: 'Disk', unit: '%', href: '#/server',
      value: has ? fmt.number(s.disk_pct, 0) : '—',
      sub: has ? `${fmt.gb(s.disk_free)} free on ${s.disk_path}${s.disks && s.disks.length ? ` · +${fmt.plural(s.disks.length, 'mount')}` : ''}` : 'Waiting for the first sample',
      status: has ? diskStatus : 'unknown', spark: spark.disk,
    }),
    statTile({
      emoji: '⚖️', label: 'Load', href: '#/server',
      value: has ? fmt.load(s.load1) : '—',
      // "5 min 0.61" stays one unit: at 1280 px the tile used to wrap it as "15 m / 1.33"
      sub: has ? `${fmt.number(s.load1 / Math.max(1, s.cores), 2)} per core · 5${NBSP}min${NBSP}${fmt.load(s.load5)} · 15${NBSP}min${NBSP}${fmt.load(s.load15)}` : 'Waiting for the first sample',
      status: per.load || 'unknown', spark: spark.load,
    }),
  ].map((node, i) => { node.dataset.key = `stat-${i}`; return node; });
}

function componentTiles(components) {
  return components.map((comp) => {
    const sub = el('span', { class: `tile-sub${comp.status === 'crit' ? ' tile-sub--crit' : comp.status === 'warn' ? ' tile-sub--warn' : ''}`, text: componentSubtitle(comp) });
    const node = tile({
      emoji: comp.emoji, title: comp.label, subtitle: sub, pill: { status: comp.status }, chevron: true,
      href: `#/stack/${encodeURIComponent(comp.id)}`,
      ariaLabel: `${comp.label}: ${statusLabel(comp.status)}, ${componentSubtitle(comp)}`,
    });
    node.dataset.key = comp.id;
    return node;
  });
}

/** Under an acknowledged alert: what Ack did (and did not do). */
export const ACKED_NOTE = 'Reminders are off. It stays listed until the cause is fixed.';

function alertTile(alert, onAck) {
  const status = severityStatus(alert.severity);
  const since = alert.opened_ts ? ` · since ${fmt.clock(alert.opened_ts)}` : '';
  const acked = Boolean(alert.acked_ts);
  const action = acked
    ? ackedPill()
    : button({ label: 'Ack', variant: 'ghost', size: 'sm', ariaLabel: `Acknowledge ${alert.title}`, onClick: (e) => { e.preventDefault(); e.stopPropagation(); onAck(alert); } });
  if (!acked) action.dataset.action = 'ack';
  const node = tile({
    emoji: status === 'crit' ? '🔴' : '🟡',
    title: keepUnits(alert.title),
    subtitle: el('span', { class: `tile-sub tile-sub--${status}`, text: humanize(`${alert.message || ''}${since}`) }),
    actions: action,
    chevron: false,
    children: acked ? el('span', { class: 'tile-sub', text: ACKED_NOTE }) : null,
  });
  node.dataset.key = `alert-${alert.id}`;
  return node;
}

function repositoryTiles(repo) {
  const items = [
    ['📚', 'Items', fmt.number(repo.items)],
    ['🧾', 'Bitstreams', fmt.number(repo.bitstreams)],
    ['🏛️', 'Communities', fmt.number(repo.communities)],
    ['🗂️', 'Collections', fmt.number(repo.collections)],
    ['🐘', 'DB size', fmt.gb(repo.db_size)],
    ['🗄️', 'Assetstore', fmt.gb(repo.assetstore_bytes)],
  ];
  return items.map(([emoji, title, value]) => {
    const node = tile({ emoji, title, value, subtitle: repo.estimated && (title === 'Items' || title === 'Bitstreams') ? 'estimated' : undefined });
    node.dataset.key = `repo-${title}`;
    return node;
  });
}

/**
 * What needs a look right now: server metrics past a threshold and components that are not healthy,
 * as {name, value, status, server, key} ("Memory", "83 %") — the subject of a warning before (or
 * without) an alert, which the band and the Alerts card name instead of leaving it to a 10 px dot.
 * `key` is what an alert about it carries (alertKey): "server:memory", or the component id.
 */
export function attentionSources(status) {
  const out = [];
  const server = (status && status.server) || {};
  for (const [key, st] of Object.entries(server.per_metric || {})) {
    if (st !== 'warn' && st !== 'crit') continue;
    let name = key;
    let value = '';
    if (key === 'cpu') { name = 'CPU'; value = fmt.pct(server.cpu_pct); }
    else if (key === 'memory') { name = 'Memory'; value = fmt.pct(server.mem_pct); }
    else if (key === 'swap') { name = 'Swap'; value = fmt.pct(server.swap_pct); }
    else if (key === 'disk') { name = 'Disk'; value = fmt.pct(server.disk_pct); }
    else if (key === 'load') { name = 'Load'; value = `${fmt.number((server.load1 || 0) / Math.max(1, server.cores || 1), 1)} per core`; }
    else if (key.startsWith('disk:')) {
      const path = key.slice(5);
      const mount = (server.disks || []).find((d) => d.path === path);
      name = `Disk ${path}`;
      value = mount ? fmt.pct(mount.pct) : '';
    }
    out.push({ name, value, status: st, server: true, key: `server:${key}` });
  }
  for (const comp of (status && status.components) || []) {
    if (comp.status !== 'warn' && comp.status !== 'crit') continue;
    out.push({ name: comp.label, value: comp.reason ? humanize(comp.reason) : '', status: comp.status, server: false, key: comp.id });
  }
  return out;
}

/** The components whose check could not tell anything (Unknown). */
function unchecked(status) {
  return ((status && status.components) || []).filter((c) => c.status === 'unknown');
}

/**
 * Why a component could not be checked, for the Alerts card: "Logs cannot be checked: permission
 * denied (/dspace/log/dspace.log)" while the cause lasts; "… could not be checked this time: check
 * timed out after 29 s" only for a check that failed once (it timed out or raised: the scheduler's
 * `<id>.error` rung, detail.collector_error); "… has not been checked yet" before its first check.
 */
export function uncheckedClause(comp) {
  if (!(comp.chain || []).length && !comp.reason) return `${comp.label} has not been checked yet`;
  const once = (comp.chain || []).some((rung) => Boolean(rung.detail && rung.detail.collector_error));
  const reason = comp.reason ? humanize(comp.reason).replace(/\.$/, '') : '';
  return `${comp.label} ${once ? 'could not be checked this time' : 'cannot be checked'}${reason ? `: ${reason}` : ''}`;
}

/** "Open Logs": the way to a component's page, whose ladder says more (a small ghost button). */
function componentLink(comp) {
  return el('a', { class: 'btn btn--ghost btn--sm', href: `#/stack/${encodeURIComponent(comp.id)}`, dataset: { action: 'open' } },
    el('span', { class: 'btn-label', text: `Open ${comp.label}` }));
}

/** "Memory 83 %" / "Solr" — a source as the band's short subject. */
function sourceShort(src) {
  return src.server && src.value ? `${src.name} ${src.value}` : src.name;
}

/** "A", "A and B", "A, B and C". */
function andList(items) {
  if (items.length < 2) return items.join('');
  return `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`;
}

/** The Alerts card sentence while something needs a look but no alert is open (confirm window, or no rule). */
function attentionSentence(sources) {
  if (sources.length === 1) {
    const [src] = sources;
    if (src.server) return `${src.name} is above its ${src.status === 'crit' ? 'critical' : 'warning'} threshold${src.value ? ` (${src.value})` : ''} — an alert opens if it stays there.`;
    return `${src.name} needs a look${src.value ? ` (${src.value})` : ''} — an alert opens if it stays that way.`;
  }
  const names = sources.map((src) => (src.value ? `${src.name} (${src.value})` : src.name));
  return `${andList(names)} need a look — an alert opens for each one that stays that way.`;
}

/** What an alert is about, as attentionSources keys it: "server:memory" for a server metric, else the component id. */
function alertKey(alert) {
  return alert.component === 'server' ? `server:${alert.rule}` : alert.component;
}

/**
 * True when the subject has open alerts and every one of them is acknowledged. The server then counts
 * it as healthy for the mood and the headline ("All systems healthy · 1 acknowledged"), while the
 * component keeps its own status on its tile, its page, the Stack and History.
 */
function calmed(src, alerts) {
  const own = alerts.filter((a) => alertKey(a) === src.key);
  return own.length > 0 && own.every((a) => Boolean(a.acked_ts));
}

/**
 * The band's headline, as the server words it (it leaves acknowledged alerts out of the mood). "1
 * warning" / "2 warnings" gain their subject ("1 warning · Memory 83 %"), never one the server counted
 * as healthy because all its alerts are acknowledged; "1 check could not run" gains its component ("1
 * check could not run · Logs"; the Alerts card says why); the server already names a critical
 * ("Critical: Memory 94 %").
 */
function headlineText(status) {
  const raw = String(status.headline || '').trim();
  let text = keepUnits(status.headline || '');
  let subjects = [];
  // the alerts as the server saw them: an Ack clicked here counts once the server's answer says so
  const alerts = status.alerts_open || [];
  if (status.mood === 'warn' && /^\d+\s+warnings?$/.test(raw)) subjects = attentionSources(status).filter((src) => !calmed(src, alerts)).map(sourceShort);
  else if (status.mood === 'unknown' && /^\d+\s+checks?\s+could not run$/.test(raw)) subjects = unchecked(status).map((c) => c.label);
  if (subjects.length) text = `${text} · ${subjects.slice(0, 2).join(', ')}${subjects.length > 2 ? ` +${subjects.length - 2} more` : ''}`;
  return text;
}

function footerText(status, meta) {
  const s = status && status.server;
  const parts = [];
  if (s && s.os_name) parts.push(`${s.os_name} ${s.os_version || ''}`.trim());
  if (s && s.kernel) parts.push(`kernel ${fmt.kernelShort(s.kernel)}`);
  if (s && s.uptime_s) parts.push(`up ${fmt.uptime(s.uptime_s)}`);
  parts.push(`Sentinel v${(meta && meta.version) || (status && status.version) || ''}`);
  return parts.join(' · ');
}

/**
 * Render the Home tab.
 * @param {HTMLElement} root
 * @param {object} params
 * @param {object} ctx
 * @returns {() => void} destroy
 */
export function render(root, params, ctx) {
  const view = grid();

  // Pulse band
  const headline = el('h2', { class: 'pulse-headline', id: 'pulse-headline', 'aria-live': 'polite', text: 'Connecting…' });
  const subText = el('span', { class: 'pulse-subtext', text: '' });
  const demoPill = el('span', { class: 'pulse-demo', text: 'Demo data' });
  demoPill.hidden = true;
  const checkBtn = button({ label: '↻ Check now', variant: 'ghost', className: 'btn--pill', busyLabel: 'Checking…', onClick: () => ctx.checkNow() });
  const pulse = el('section', { class: 'pulse', 'aria-labelledby': 'pulse-headline' },
    el('div', 'pulse-main', headline, el('p', 'pulse-sub', subText, demoPill)),
    el('div', 'pulse-actions', checkBtn),
  );

  // Sections
  const serverSection = section({ title: 'Server', layout: 'stat', id: 'home-server' });
  const stackSection = section({ title: 'DSpace stack', layout: 'stack', id: 'home-stack' });
  const stackCount = el('span', { class: 'muted', text: '' });
  stackSection.actionsEl.appendChild(stackCount);
  const alertsSection = section({ title: 'Alerts', layout: 'stack', id: 'home-alerts' });
  const alertsCount = el('span', { class: 'muted', text: '' });
  alertsSection.actionsEl.appendChild(alertsCount);
  const repoSection = section({ title: 'Repository', layout: 'two', id: 'home-repository' });
  const repoNote = el('span', { class: 'muted', text: '' });
  repoSection.actionsEl.appendChild(repoNote);
  const footer = el('p', { class: 'home-footer span-12 muted', text: '' });

  const left = el('div', 'span-6', column(stackSection));
  const right = el('div', 'span-6', column(alertsSection, repoSection));
  view.append(pulse, serverSection, left, right, footer);
  root.replaceChildren(view);

  // Header controls: the next check. The age is already in the band ("checked 8 s ago") and the sidebar.
  const checkedText = el('span', { class: 'muted', id: 'home-checked', text: '' });
  ctx.setControls(checkedText);

  const localAcks = new Map(); // alert id → acked_ts override until the next status frame confirms

  /** The open alerts as shown here: an Ack clicked on this page counts until a status frame confirms it. */
  function openAlerts(status) {
    const alerts = (status && status.alerts_open) || [];
    for (const a of alerts) if (a.acked_ts) localAcks.delete(a.id);
    return alerts.map((a) => (localAcks.has(a.id) ? { ...a, acked_ts: localAcks.get(a.id) } : a));
  }

  /** The Alerts card, which shows an Ack clicked here at once (the Acked pill and what it did). */
  function paintAcks() {
    paintAlerts(store.get('status'));
  }

  /**
   * Ack and its undo: the card changes at once; the band, the mood colour and the sidebar follow the
   * server's next answer (an acknowledged alert no longer colours them), asked for right away.
   */
  async function ack(alert) {
    localAcks.set(alert.id, Date.now() / 1000);
    paintAcks();
    try {
      await api.ackAlert(alert.id);
      ctx.refreshStatus();
      snackbar.undo('Alert acknowledged', async () => {
        localAcks.delete(alert.id);
        try {
          await api.unackAlert(alert.id);
        } catch (err) {
          snackbar(`Could not undo: ${err.message}`);
        }
        paintAcks();
        await ctx.refreshStatus();
        // focus returns to the restored Ack button
        return alertsSection.body.querySelector(`[data-key="alert-${alert.id}"] [data-action="ack"]`);
      });
    } catch (err) {
      localAcks.delete(alert.id);
      paintAcks();
      snackbar(`Could not acknowledge: ${err.message}`);
    }
  }

  function paintPulse(status) {
    if (!status) {
      headline.textContent = store.get('connection') === 'offline' ? 'Sentinel is unreachable' : 'Waiting for the first check';
      subText.textContent = (store.get('meta') && store.get('meta').hostname) || '';
      return;
    }
    const stale = store.get('stale');
    // out of date: the band stops saying "All systems healthy" and names what is known and when
    headline.textContent = stale
      ? (({ title, last }) => `${title} — ${last}`)(staleWords(stale, store.lastCheckTs()))
      : headlineText(status);
    demoPill.hidden = !status.demo;
    paintTick(status);
  }

  function paintTick(status = store.get('status')) {
    if (!status) return;
    const ago = store.checkedAgo();
    const agoText = ago === null ? 'first check pending' : `checked ${fmt.relative(ago)}`;
    const stale = store.get('stale');
    subText.textContent = `${status.hostname || ''} · ${stale ? 'showing the last known state' : agoText}`;
    if (stale) {
      checkedText.textContent = ''; // the band already says what is out of date and since when
      return;
    }
    let next = '';
    if (store.get('checking')) next = 'Checking…';
    else if (status.next_check_in_s) {
      const remaining = Math.round(status.next_check_in_s - (Date.now() / 1000 - store.get('statusAt')));
      if (remaining > 0) next = `Next check in ${remaining}${NBSP}s`;
    }
    checkedText.textContent = next;
  }

  function paintServer(status) {
    replaceKeepingFocus(serverSection.body, serverTiles(status));
  }

  function paintStack(status) {
    const comps = (status && status.components) || [];
    const dspace = store.dspaceModulesEnabled();
    stackSection.titleEl.textContent = dspace ? 'DSpace stack' : 'Services';
    // no component column (no module on): Alerts and Repository take the whole row instead of half of it
    left.hidden = comps.length === 0;
    right.className = comps.length ? 'span-6' : 'span-12';
    if (!comps.length) return;
    const healthy = comps.filter((c) => c.status === 'ok').length;
    stackCount.textContent = `${store.get('stale') ? 'Last known: ' : ''}${healthy} of ${comps.length} healthy`;
    replaceKeepingFocus(stackSection.body, componentTiles(comps));
  }

  function paintAlerts(status) {
    const alerts = openAlerts(status);
    alertsCount.textContent = alerts.length ? fmt.plural(alerts.length, 'open alert') : '';
    if (!alerts.length && store.get('stale')) {
      // no fresh check: an empty list only says nothing was open last time
      replaceKeepingFocus(alertsSection.body, [emptyState('🕓', store.get('stale') === 'offline'
        ? 'No open alerts at the last check — new ones show here once Sentinel answers again.'
        : 'No open alerts at the last check — new ones show here once checks run again.')]);
      return;
    }
    if (!alerts.length && status && status.mood !== 'ok') {
      // a warning without an alert (yet): name it here instead of a ✅ that contradicts the band
      const sources = attentionSources(status);
      if (sources.length) {
        replaceKeepingFocus(alertsSection.body, [emptyState('⚠️', attentionSentence(sources), { className: 'empty--attention' })]);
        return;
      }
      // checks that could not run: each says why, and a lasting cause does not read as a passing hiccup
      const unknown = unchecked(status);
      if (unknown.length) {
        const action = unknown.length === 1 ? componentLink(unknown[0]) : undefined;
        const card = emptyState('❔', `No open alerts — ${unknown.map(uncheckedClause).join('; ')}.`, { action });
        card.dataset.key = 'unchecked'; // a focused "Open Logs" keeps its focus through the next repaint
        replaceKeepingFocus(alertsSection.body, [card]);
        return;
      }
    }
    if (!alerts.length) {
      const interval = (store.get('meta') && store.get('meta').system_interval) || 15;
      // worded around alerts: the Incidents module may be off, and an empty list says nothing crossed a threshold
      replaceKeepingFocus(alertsSection.body, [emptyState('✅', `No open alerts — checks run every ${interval} seconds.`)]);
      return;
    }
    replaceKeepingFocus(alertsSection.body, alerts.map((a) => alertTile(a, ack)));
  }

  function paintRepository(status) {
    const enabled = store.moduleEnabled('repository');
    const repo = status && status.repository;
    repoSection.hidden = !enabled;
    if (!enabled) return;
    if (!repo) {
      repoNote.textContent = '';
      repoSection.body.replaceChildren(emptyState('📚', 'Counting items and bitstreams — numbers appear after the first repository check.'));
      return;
    }
    repoNote.textContent = repo.ts ? `as of ${fmt.clock(repo.ts)}` : '';
    replaceKeepingFocus(repoSection.body, repositoryTiles(repo));
  }

  function paintFooter(status) {
    footer.textContent = footerText(status, store.get('meta'));
  }

  function paintAll() {
    const status = store.get('status');
    paintPulse(status);
    paintServer(status);
    paintStack(status);
    paintAlerts(status);
    paintRepository(status);
    paintFooter(status);
  }

  const unsubscribe = store.subscribe(['status', 'meta', 'connection', 'stale'], paintAll);
  const unsubscribeChecking = store.subscribe('checking', (state) => { checkBtn.setBusy(state.checking); paintTick(); });
  const timer = setInterval(() => paintTick(), 1000);
  checkBtn.setBusy(Boolean(store.get('checking')));
  paintAll();

  return () => {
    unsubscribe();
    unsubscribeChecking();
    clearInterval(timer);
  };
}
