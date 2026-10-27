/**
 * screens/history.js — the History tab (SPEC §12.5).
 *
 * One range chip row in the page header (1 h · 6 h · 24 h · 7 d · 30 d, persisted in the store)
 * scopes everything below: Server charts (CPU, Memory, Disk, Load; Network RX/TX two-series with a
 * legend) · Response times (REST, Search, Homepage with warning/critical hairlines) · Availability
 * (a strip per component + uptime %) · Incidents (🧭 tiles → detail) · Alert history (a table,
 * collapsed with "Show more") · Reports (📄 tiles → open / download / delete with Undo).
 * Charts hold the previous render at 50 % while refetching; lists re-render in place keeping focus.
 */
import api from '../api.js';
import { availabilityStrip, lineChart } from '../charts.js';
import { RANGES, store } from '../store.js';
import {
  button, chartTile, chipRow, el, emptyState, fmt, grid, humanize, keepUnits, replaceKeepingFocus, section, severityStatus, snackbar,
  statusPill, table, tile,
} from '../ui.js';
import { reportTitles } from './detail/report.js';

const RANGE_LABEL = { '1h': '1 h', '6h': '6 h', '24h': '24 h', '7d': '7 d', '30d': '30 d' };
const RANGE_TEXT = { '1h': 'the last hour', '6h': 'the last 6 hours', '24h': 'the last 24 hours', '7d': 'the last 7 days', '30d': 'the last 30 days' };
const RANGE_S = { '1h': 3600, '6h': 6 * 3600, '24h': 86400, '7d': 7 * 86400, '30d': 30 * 86400 };
const SERVER_CHARTS = [
  { metric: 'cpu', title: 'CPU', unit: '%', span: 6 },
  { metric: 'mem', title: 'Memory', unit: '%', span: 6 },
  { metric: 'disk', title: 'Disk', unit: '%', span: 6 },
  { metric: 'load', title: 'Load (1 min)', unit: 'load', span: 6 },
  { metric: 'net', title: 'Network', unit: 'B/s', span: 12 },
];
const LATENCY_CHARTS = [
  { check: 'backend.rest', title: 'REST API', module: 'backend' },
  { check: 'backend.search', title: 'Search', module: 'backend' },
  { check: 'frontend.home', title: 'Homepage', module: 'frontend' },
];
const ALERTS_COLLAPSED = 5;
const REFRESH_MS = 60000;

// ------------------------------------------------------------------ helpers

function bucketText(bucketS) {
  if (!bucketS) return '';
  if (bucketS < 60) return `${bucketS}-second samples`;
  if (bucketS < 3600) return `${Math.round(bucketS / 60)}-minute averages`;
  if (bucketS === 3600) return 'hourly averages';
  return `${Math.round(bucketS / 3600)}-hour averages`;
}

/** "avg 23 % · peak 61 %" for one series of [ts, avg, max] points. */
function summarise(points, unit) {
  if (!points || !points.length) return '';
  let sum = 0;
  let peak = -Infinity;
  let n = 0;
  for (const p of points) {
    if (p[1] === null || p[1] === undefined) continue;
    sum += p[1];
    n += 1;
    peak = Math.max(peak, p[2] !== null && p[2] !== undefined ? p[2] : p[1]);
  }
  if (!n) return '';
  const avg = sum / n;
  const f = (v) => (unit === '%' ? fmt.pct(v, v < 10 ? 1 : 0) : unit === 'ms' ? fmt.ms(v) : unit === 'B/s' ? fmt.rate(v) : fmt.load(v));
  return `avg ${f(avg)} · peak ${f(peak)}`;
}

/** Component labels as the backend names them (health.COMPONENTS), for alerts of a component not in the status. */
const COMPONENT_LABELS = {
  server: 'Server', postgres: 'PostgreSQL', solr: 'Solr', backend: 'DSpace Backend', frontend: 'DSpace Frontend',
  nginx: 'nginx', ssl: 'SSL certificate', ports: 'Ports', logs: 'Logs', repository: 'Repository', assetstore: 'Assetstore',
  security: 'Security',
};

function componentLabel(id, status) {
  const comp = ((status && status.components) || []).find((c) => c.id === id);
  if (comp) return comp.label;
  if (COMPONENT_LABELS[id]) return COMPONENT_LABELS[id];
  return id ? id.charAt(0).toUpperCase() + id.slice(1) : '—';
}

/** A module that is off leaves no trace (DESIGN principle 2): its old alerts leave the history too. Server alerts always stay. */
function alertVisible(alert) {
  const id = alert && alert.component;
  if (!id || id === 'server' || !(id in COMPONENT_LABELS)) return true;
  return store.moduleEnabled(id);
}

// ------------------------------------------------------------------ builders

function availabilityRow(comp, meta) {
  const strip = availabilityStrip(comp.buckets || [], { from: meta.from, to: meta.to, bucketS: meta.bucket_s, label: comp.label, uptimePct: comp.uptime_pct });
  const sub = comp.outages
    ? `${fmt.plural(comp.outages, 'outage')} · ${fmt.duration(comp.downtime_s)} down`
    : comp.samples ? 'No outages' : 'No data yet';
  const pct = comp.samples ? comp.uptime_pct : null;
  const tone = pct === null ? '' : pct < 99 ? ' avail-pct--crit' : pct < 99.9 ? ' avail-pct--warn' : '';
  return el('div', { class: 'avail-row', dataset: { key: `avail-${comp.id}` } },
    el('span', { class: 'tile-icon', 'aria-hidden': 'true', text: comp.emoji || '' }),
    el('span', 'avail-name', el('span', { class: 'avail-label', text: comp.label }), el('span', { class: 'avail-sub', text: sub })),
    strip,
    el('span', { class: `avail-pct${tone}`, text: pct === null ? '—' : fmt.pct(pct, pct === 100 ? 0 : 2) }),
  );
}

function incidentTile(inc) {
  const open = inc.status === 'open';
  // the start time leads the line, so an open incident just reads "· ongoing"
  const lasted = open ? 'ongoing' : `lasted ${fmt.duration((inc.ended_ts || inc.started_ts) - inc.started_ts)}`;
  const affected = inc.affected && inc.affected.length ? ` · ${inc.affected.join(', ')}` : '';
  return tile({
    emoji: '🧭',
    title: inc.hypothesis || inc.title || `Incident #${inc.id}`,
    subtitle: `${fmt.dateTime(inc.started_ts)} · ${lasted}${affected}`,
    pill: open ? statusPill('crit', 'Open') : statusPill('ok', 'Resolved'),
    chevron: true,
    href: `#/history/incidents/${inc.id}`,
    dataset: { key: `incident-${inc.id}` },
  });
}

function reportTile(rep, { onOpen, onDelete }) {
  const url = String(api.reportUrl(rep.id));
  const isHtml = rep.fmt === 'html';
  // the range is the title ("Last 24 hours"); a title the operator typed wins and the range moves to the subtitle
  const names = reportTitles(rep);
  const name = `${names.title} report`;
  const open = button({ label: isHtml ? 'Open' : 'View', variant: 'ghost', size: 'sm', ariaLabel: `${isHtml ? 'Open' : 'View'} ${name}`, onClick: () => onOpen(rep) });
  open.dataset.action = 'open';
  const download = el('a', { class: 'btn btn--ghost btn--sm', href: `${url}?download=true`, download: rep.filename || '', 'aria-label': `Download ${name}`, dataset: { action: 'download' } }, el('span', { class: 'btn-label', text: 'Download' }));
  const del = button({ label: 'Delete', variant: 'text', size: 'sm', ariaLabel: `Delete ${name}`, onClick: () => onDelete(rep) });
  del.dataset.action = 'delete';
  const node = tile({
    emoji: '📄', title: names.title || `Report #${rep.id}`, subtitle: names.subtitle, chevron: false,
    children: el('span', 'tile-actions tile-actions--body', open, download, del),
    dataset: { key: `report-${rep.id}` },
  });
  const titleEl = node.querySelector('.tile-title');
  titleEl.replaceChildren(el('a', { href: `#/history/reports/${rep.id}`, text: names.title || `Report #${rep.id}`, title: 'Report details', dataset: { action: 'details' } }));
  return node;
}

// ------------------------------------------------------------------ screen

/**
 * Render the History tab.
 * @param {HTMLElement} root
 * @param {object} params
 * @param {object} ctx
 * @returns {() => void} destroy
 */
export function render(root, params, ctx) {
  const view = grid();

  // Header: the range chips scope everything
  const chips = chipRow({
    options: RANGES.map((r) => ({ value: r, label: RANGE_LABEL[r], title: `Show ${RANGE_TEXT[r]}` })),
    value: store.get('range'), ariaLabel: 'Range', name: 'history-range',
    onChange: (value) => store.setRange(value),
  });
  ctx.setControls(chips);

  // Sections
  const serverSection = section({ title: 'Server', layout: 'grid', id: 'history-server' });
  const latencySection = section({ title: 'Response times', layout: 'grid', id: 'history-latency' });
  const availSection = section({ title: 'Availability', layout: 'stack', id: 'history-availability' });
  const availTile = tile({ emoji: '🟢', title: 'Uptime per service', subtitle: 'Loading…', className: 'tile--block' });
  const availList = el('div', 'avail-list');
  availTile.appendChild(availList);
  availSection.body.appendChild(availTile);
  const incidentsSection = section({ title: 'Incidents', layout: 'stack', id: 'history-incidents' });
  const incidentsCount = el('span', { class: 'muted', text: '' });
  incidentsSection.actionsEl.appendChild(incidentsCount);
  const alertsSection = section({ title: 'Alert history', layout: 'stack', id: 'history-alerts' });
  const alertsCount = el('span', { class: 'muted', text: '' });
  const showMore = button({ label: 'Show more', variant: 'ghost', size: 'sm' });
  showMore.hidden = true;
  alertsSection.actionsEl.append(alertsCount, showMore);
  const reportsSection = section({ title: 'Reports', layout: 'stack', id: 'history-reports' });
  const newReport = button({ label: '＋ New report', variant: 'ghost', size: 'sm', onClick: () => ctx.openPanel('report') });
  reportsSection.actionsEl.appendChild(newReport);

  const left = el('div', 'span-6', el('div', 'col', incidentsSection));
  const right = el('div', 'span-6', el('div', 'col', reportsSection));

  /** With the Incidents module off its half-row goes too: Reports takes the full width, its tiles in two columns. */
  function layoutIncidents(enabled) {
    incidentsSection.hidden = !enabled;
    left.hidden = !enabled;
    right.className = enabled ? 'span-6' : 'span-12';
    reportsSection.body.className = `section-body section-body--${enabled ? 'stack' : 'two'}`;
  }
  view.append(serverSection, latencySection, availSection, left, right, alertsSection);
  root.replaceChildren(view);

  // Charts (created once; update() redraws without flashing)
  const serverCharts = new Map();
  for (const spec of SERVER_CHARTS) {
    const chart = lineChart({ series: [], unit: spec.unit, height: 180, title: spec.title });
    const wrap = chartTile({ title: spec.title, chart, span: spec.span });
    serverSection.body.appendChild(wrap);
    serverCharts.set(spec.metric, { spec, chart, wrap });
  }
  const latencyCharts = new Map();
  for (const spec of LATENCY_CHARTS) {
    const chart = lineChart({ series: [], unit: 'ms', height: 180, title: spec.title });
    const wrap = chartTile({ title: spec.title, chart, span: 4 });
    latencySection.body.appendChild(wrap);
    latencyCharts.set(spec.check, { spec, chart, wrap });
  }

  const state = {
    seq: 0,
    alerts: [],
    alertsExpanded: false,
    reports: [],
    reportsLoaded: false,
    incidents: [],
  };

  function range() {
    return store.get('range');
  }

  function rangeFrom() {
    return Date.now() / 1000 - (RANGE_S[range()] || 86400);
  }

  function fresh(seq) {
    return seq === state.seq && !ctx.signal.aborted;
  }

  // ---------------------------------------------------------------- server charts

  async function loadServer(seq) {
    for (const { chart } of serverCharts.values()) chart.setBusy(true);
    const results = await Promise.allSettled(SERVER_CHARTS.map((spec) => api.history(spec.metric, range(), { signal: ctx.signal })));
    if (!fresh(seq)) return;
    let bucket = 0;
    results.forEach((res, i) => {
      const spec = SERVER_CHARTS[i];
      const entry = serverCharts.get(spec.metric);
      entry.chart.setBusy(false);
      if (res.status !== 'fulfilled') {
        entry.wrap.setNote(`Not loaded — ${res.reason && res.reason.message ? res.reason.message : 'error'}`);
        return;
      }
      entry.wrap.setNote('');
      const data = res.value;
      bucket = data.bucket_s || bucket;
      if (spec.metric === 'net') {
        const s = data.series || {};
        const rx = (s.rx || []).map((p) => [p[0], p[1]]);
        const tx = (s.tx || []).map((p) => [p[0], p[1]]);
        entry.chart.update({ series: [{ id: 'rx', label: 'Received', points: rx }, { id: 'tx', label: 'Sent', points: tx }], thresholds: null, from: data.from, to: data.to });
        const avg = (pts) => (pts.length ? pts.reduce((a, p) => a + (p[1] || 0), 0) / pts.length : null);
        entry.wrap.setValue(rx.length ? `↓ ${fmt.rate(avg(rx))} · ↑ ${fmt.rate(avg(tx))}` : '');
      } else {
        const points = data.points || [];
        entry.chart.update({ series: [{ id: spec.metric, label: spec.title, points: points.map((p) => [p[0], p[1]]) }], thresholds: data.thresholds || null, from: data.from, to: data.to });
        entry.wrap.setValue(summarise(points, spec.unit));
      }
    });
    serverSection.titleEl.nextElementSibling?.remove();
    if (bucket) serverSection.titleEl.insertAdjacentElement('afterend', el('p', { class: 'section-sub', text: `${bucketText(bucket)} over ${RANGE_TEXT[range()]}` }));
  }

  // ---------------------------------------------------------------- response times

  function activeLatency() {
    return LATENCY_CHARTS.filter((spec) => store.moduleEnabled(spec.module));
  }

  async function loadLatency(seq) {
    const active = activeLatency();
    latencySection.hidden = active.length === 0;
    for (const [check, entry] of latencyCharts) entry.wrap.hidden = !active.some((s) => s.check === check);
    if (!active.length) return;
    for (const spec of active) latencyCharts.get(spec.check).chart.setBusy(true);
    const results = await Promise.allSettled(active.map((spec) => api.latency(spec.check, range(), { signal: ctx.signal })));
    if (!fresh(seq)) return;
    results.forEach((res, i) => {
      const spec = active[i];
      const entry = latencyCharts.get(spec.check);
      entry.chart.setBusy(false);
      if (res.status !== 'fulfilled') {
        entry.wrap.setNote(`Not loaded — ${res.reason && res.reason.message ? res.reason.message : 'error'}`);
        return;
      }
      entry.wrap.setNote('');
      const data = res.value;
      const points = data.points || [];
      entry.chart.update({ series: [{ id: spec.check, label: spec.title, points: points.map((p) => [p[0], p[1]]) }], thresholds: data.thresholds || null, from: data.from, to: data.to });
      entry.wrap.setValue(summarise(points, 'ms'));
    });
  }

  // ---------------------------------------------------------------- availability

  async function loadAvailability(seq) {
    let data;
    try {
      data = await api.availability(range(), { signal: ctx.signal });
    } catch (err) {
      if (!fresh(seq)) return;
      availList.replaceChildren(el('p', { class: 'muted', text: `Not loaded — ${err.message}` }));
      return;
    }
    if (!fresh(seq)) return;
    const comps = data.components || [];
    const sub = availTile.querySelector('.tile-sub');
    if (sub) sub.textContent = `${fmt.plural(comps.length, 'service')} over ${RANGE_TEXT[range()]} · ${bucketText(data.bucket_s).replace('averages', 'intervals').replace('samples', 'intervals')}`;
    if (!comps.length) {
      availList.replaceChildren(el('p', { class: 'muted', text: 'Availability appears after the first checks.' }));
      return;
    }
    replaceKeepingFocus(availList, comps.map((c) => availabilityRow(c, data)));
  }

  // ---------------------------------------------------------------- incidents

  function paintIncidents() {
    const enabled = store.moduleEnabled('incidents');
    layoutIncidents(enabled);
    if (!enabled) return;
    const from = rangeFrom();
    const list = state.incidents.filter((i) => i.status === 'open' || (i.ended_ts || i.started_ts) >= from);
    incidentsCount.textContent = list.length ? fmt.plural(list.length, 'incident') : '';
    if (!list.length) {
      replaceKeepingFocus(incidentsSection.body, [emptyState('🧭', state.incidents.length
        ? `No incidents in ${RANGE_TEXT[range()]} — pick a longer range to see earlier ones.`
        : 'No incidents yet — related alerts will be grouped here.')]);
      return;
    }
    replaceKeepingFocus(incidentsSection.body, list.map(incidentTile));
  }

  async function loadIncidents(seq) {
    if (!store.moduleEnabled('incidents')) {
      layoutIncidents(false);
      return;
    }
    try {
      const list = await api.incidents('all', 200, { signal: ctx.signal });
      if (!fresh(seq)) return;
      state.incidents = list;
    } catch (err) {
      if (!fresh(seq)) return;
      incidentsSection.body.replaceChildren(el('p', { class: 'muted', text: `Not loaded — ${err.message}` }));
      return;
    }
    paintIncidents();
  }

  // ---------------------------------------------------------------- alert history

  const status = () => store.get('status');
  const alertsTable = table({
    ariaLabel: 'Alert history',
    columns: [
      { key: 'title', label: 'Alert', format: (v, row) => el('span', 'kv-cell', el('span', { text: keepUnits(v) }), row.message ? el('span', { class: 'muted', text: humanize(` — ${row.message}`) }) : null) },
      { key: 'component', label: 'Service', format: (v) => componentLabel(v, status()) },
      { key: 'severity', label: 'Severity', format: (v) => statusPill(severityStatus(v), v === 'critical' ? 'Critical' : 'Warning') },
      { key: 'opened_ts', label: 'Opened', align: 'end', numeric: true, format: (v) => el('span', { class: 'nowrap', text: fmt.dateTime(v) }) },
      { key: 'lasted', label: 'Lasted', align: 'end', numeric: true, format: (v, row) => el('span', { class: 'nowrap', text: row.status === 'open' ? 'open' : fmt.duration((row.resolved_ts || row.opened_ts) - row.opened_ts) }) },
      { key: 'status', label: 'Status', format: (v, row) => (v === 'open' ? statusPill(row.acked_ts ? 'unknown' : severityStatus(row.severity), row.acked_ts ? 'Acked' : 'Open') : statusPill('ok', 'Resolved')) },
    ],
    rows: [],
    emptyText: 'No alerts in this range.',
  });
  alertsSection.body.appendChild(alertsTable);
  const alertsEmpty = emptyState('✅', 'No alerts in this range — nothing crossed a threshold.');
  alertsEmpty.hidden = true;
  alertsSection.body.appendChild(alertsEmpty);

  function paintAlerts() {
    const from = rangeFrom();
    const kept = state.alerts.filter(alertVisible);
    const list = kept
      .filter((a) => a.status === 'open' || (a.opened_ts || 0) >= from)
      .sort((a, b) => (b.opened_ts || 0) - (a.opened_ts || 0));
    alertsCount.textContent = list.length ? fmt.plural(list.length, 'alert') : '';
    const hidden = Math.max(0, list.length - ALERTS_COLLAPSED);
    showMore.hidden = hidden === 0 && !state.alertsExpanded;
    showMore.setLabel(state.alertsExpanded ? 'Show less' : `Show more (${hidden})`);
    showMore.setAttribute('aria-expanded', state.alertsExpanded ? 'true' : 'false');
    const rows = state.alertsExpanded ? list : list.slice(0, ALERTS_COLLAPSED);
    alertsTable.hidden = list.length === 0;
    alertsEmpty.hidden = list.length !== 0;
    alertsEmpty.querySelector('.empty-text').textContent = kept.length
      ? `No alerts in ${RANGE_TEXT[range()]} — pick a longer range to see earlier ones.`
      : 'No alerts yet — nothing has crossed a threshold.';
    alertsTable.update(rows);
  }

  showMore.addEventListener('click', () => {
    state.alertsExpanded = !state.alertsExpanded;
    paintAlerts();
  });

  async function loadAlerts(seq) {
    try {
      const list = await api.alerts('all', 500, { signal: ctx.signal });
      if (!fresh(seq)) return;
      state.alerts = list;
    } catch (err) {
      if (!fresh(seq)) return;
      alertsCount.textContent = `Not loaded — ${err.message}`;
      return;
    }
    paintAlerts();
  }

  // ---------------------------------------------------------------- reports

  function openReport(rep) {
    const url = String(api.reportUrl(rep.id));
    if (rep.fmt === 'html') window.open(url, '_blank', 'noopener');
    else ctx.navigate(`#/history/reports/${rep.id}`);
  }

  function upsertReport(rep) {
    if (!rep || rep.id === undefined) return;
    const idx = state.reports.findIndex((r) => r.id === rep.id);
    if (rep.deleted_ts) {
      if (idx >= 0) state.reports.splice(idx, 1);
    } else if (idx >= 0) state.reports[idx] = rep;
    else state.reports.push(rep);
    state.reports.sort((a, b) => b.created_ts - a.created_ts);
  }

  function removeReport(id) {
    state.reports = state.reports.filter((r) => r.id !== id);
  }

  async function deleteReport(rep) {
    removeReport(rep.id);
    paintReports();
    try {
      await api.deleteReport(rep.id);
    } catch (err) {
      upsertReport(rep);
      paintReports();
      snackbar(`Could not delete the report — ${err.message}`);
      return;
    }
    snackbar.undo('Report deleted', async () => {
      try {
        const restored = await api.restoreReport(rep.id);
        upsertReport(restored);
        paintReports();
      } catch (err) {
        snackbar(`Could not undo — ${err.message}`);
        return null;
      }
      // focus returns to the restored report
      return reportsSection.body.querySelector(`[data-key="report-${rep.id}"] [data-action="details"]`);
    });
  }

  function paintReports() {
    const from = rangeFrom();
    const list = state.reports.filter((r) => r.created_ts >= from);
    if (!list.length) {
      const text = !state.reportsLoaded
        ? 'Loading reports…'
        : state.reports.length
          ? `No reports generated in ${RANGE_TEXT[range()]} — pick a longer range, or click ＋ New report.`
          : 'No reports yet — click ＋ New report to generate your first one.';
      replaceKeepingFocus(reportsSection.body, [emptyState('📄', text)], { fallback: newReport });
      return;
    }
    // a deleted report hands focus to the next report's Delete, the last one to "＋ New report"
    replaceKeepingFocus(reportsSection.body, list.map((r) => reportTile(r, { onOpen: openReport, onDelete: deleteReport })), { fallback: newReport });
  }

  async function loadReports(seq) {
    try {
      const list = await api.reports({ signal: ctx.signal });
      if (!fresh(seq)) return;
      state.reports = list.slice().sort((a, b) => b.created_ts - a.created_ts);
      state.reportsLoaded = true;
    } catch (err) {
      if (!fresh(seq)) return;
      reportsSection.body.replaceChildren(el('p', { class: 'muted', text: `Not loaded — ${err.message}` }));
      return;
    }
    paintReports();
  }

  // ---------------------------------------------------------------- orchestration

  function loadAll() {
    const seq = ++state.seq;
    loadServer(seq);
    loadLatency(seq);
    loadAvailability(seq);
    loadIncidents(seq);
    loadAlerts(seq);
    loadReports(seq);
  }

  loadAll();
  const timer = setInterval(loadAll, REFRESH_MS);
  const offRange = store.subscribe('range', (s) => {
    chips.set(s.range, { silent: true });
    loadAll();
  });
  const offModules = store.on('modules', () => loadAll());
  let alertTimer = null;
  const offAlert = store.on('sse:alert', () => {
    clearTimeout(alertTimer);
    alertTimer = setTimeout(() => loadAlerts(state.seq), 500);
  });
  const offIncident = store.on('sse:incident', () => loadIncidents(state.seq));
  const offReport = store.on('sse:report', (evt) => {
    if (!evt || !evt.report) return;
    if (evt.type === 'deleted') removeReport(evt.report.id);
    else upsertReport(evt.report);
    paintReports();
  });

  return () => {
    clearInterval(timer);
    clearTimeout(alertTimer);
    offRange();
    offModules();
    offAlert();
    offIncident();
    offReport();
    for (const { chart } of serverCharts.values()) chart.destroy();
    for (const { chart } of latencyCharts.values()) chart.destroy();
  };
}
