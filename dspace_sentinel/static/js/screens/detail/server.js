/**
 * screens/detail/server.js — #/server: every vital of the host (SPEC §12.5).
 *
 * Stat row → 24 h charts (CPU, Memory, Disk, Load; Network RX/TX full width) → per-core bars +
 * memory breakdown → mounts table + system facts. Charts come from /api/history and refresh every
 * minute, holding the previous render at 50 % while refetching; vitals follow every status frame.
 */
import api from '../../api.js';
import { lineChart } from '../../charts.js';
import { store } from '../../store.js';
import { chartTile, el, fmt, grid, meter, section, statTile, table, tile } from '../../ui.js';

export const title = 'Server';

const REFRESH_MS = 60000;
const CHARTS = [
  { metric: 'cpu', title: 'CPU', unit: '%', span: 6 },
  { metric: 'mem', title: 'Memory', unit: '%', span: 6 },
  { metric: 'disk', title: 'Disk', unit: '%', span: 6 },
  { metric: 'load', title: 'Load (1 min)', unit: 'load', span: 6 },
  { metric: 'net', title: 'Network', unit: 'B/s', span: 12 },
];

function thresholdStatus(value, th) {
  if (!th || value === null || value === undefined) return 'ok';
  if (value >= th.critical) return 'crit';
  if (value >= th.warning) return 'warn';
  return 'ok';
}

function meterRow(label, node) {
  return el('div', 'meter-row', el('span', { class: 'meter-row-label', text: label }), node);
}

function kv(pairs) {
  const dl = el('dl', 'kv');
  for (const [k, v, numeric] of pairs) {
    dl.appendChild(el('dt', { text: k }));
    dl.appendChild(el('dd', { class: numeric ? 'numeric' : '', text: v }));
  }
  return dl;
}

/**
 * Render the server detail.
 * @returns {() => void} destroy
 */
export function render(root, params, ctx) {
  const view = grid();
  const statsSection = section({ title: 'Right now', layout: 'stat', id: 'server-stats' });
  const chartsSection = section({ title: 'Last 24 hours', layout: 'grid', id: 'server-charts' });
  const coresTile = tile({ emoji: '🧮', title: 'Cores', subtitle: 'Per-core CPU use', className: 'span-6 tile--block' });
  const coresBody = el('div', 'meter-list');
  coresTile.appendChild(coresBody);
  const memTile = tile({ emoji: '🧠', title: 'Memory', subtitle: 'Used, available, cached and swap', className: 'span-6 tile--block' });
  const memBody = el('div', 'meter-list');
  memTile.appendChild(memBody);
  const mountsSection = section({ title: 'Mounts', layout: 'stack', span: 6, id: 'server-mounts' });
  const systemSection = section({ title: 'System', layout: 'stack', span: 6, id: 'server-system' });
  const systemTile = tile({ emoji: '🖥️', title: 'Host', className: 'tile--block' });
  const systemBody = el('div', 'kv-wrap');
  systemTile.appendChild(systemBody);
  systemSection.body.appendChild(systemTile);
  const detailsGrid = el('div', 'grid span-12', coresTile, memTile);
  view.append(statsSection, chartsSection, detailsGrid, mountsSection, systemSection);
  root.replaceChildren(view);
  ctx.setControls(el('span', { class: 'muted', text: 'Charts refresh every minute' }));

  const charts = new Map();
  for (const spec of CHARTS) {
    const chart = lineChart({ series: [], unit: spec.unit, height: 180, title: spec.title });
    const wrap = chartTile({ title: spec.title, chart, span: spec.span });
    chartsSection.body.appendChild(wrap);
    charts.set(spec.metric, { spec, chart, wrap });
  }
  const thresholds = {};
  let firstLoad = true;

  async function loadCharts() {
    for (const { chart } of charts.values()) chart.setBusy(true);
    const results = await Promise.allSettled(CHARTS.map((spec) => api.history(spec.metric, '24h', { signal: ctx.signal })));
    if (ctx.signal.aborted) return;
    results.forEach((res, i) => {
      const spec = CHARTS[i];
      const entry = charts.get(spec.metric);
      entry.chart.setBusy(false);
      if (res.status !== 'fulfilled') {
        if (firstLoad) entry.wrap.setNote(`Not loaded: ${res.reason && res.reason.message ? res.reason.message : 'error'}`);
        return;
      }
      const data = res.value;
      thresholds[spec.metric] = data.thresholds || null;
      if (spec.metric === 'net') {
        const s = data.series || {};
        entry.chart.update({
          series: [
            { id: 'rx', label: 'Received', points: (s.rx || []).map((p) => [p[0], p[1]]) },
            { id: 'tx', label: 'Sent', points: (s.tx || []).map((p) => [p[0], p[1]]) },
          ],
          thresholds: null, from: data.from, to: data.to,
        });
      } else {
        entry.chart.update({
          series: [{ id: spec.metric, label: spec.title, points: (data.points || []).map((p) => [p[0], p[1]]) }],
          thresholds: data.thresholds || null, from: data.from, to: data.to,
        });
      }
    });
    firstLoad = false;
    paintVitals();
  }

  function paintVitals() {
    const status = store.get('status');
    const s = status && status.server && status.server.cpu_pct !== undefined ? status.server : null;
    const per = (s && s.per_metric) || {};
    const spark = (s && s.spark) || {};
    statsSection.body.replaceChildren(
      statTile({ emoji: '💻', label: 'CPU', unit: '%', value: s ? fmt.number(s.cpu_pct, s.cpu_pct < 10 ? 1 : 0) : '—', sub: s ? fmt.plural(s.cores, 'core') : '', status: per.cpu || 'unknown', spark: spark.cpu }),
      statTile({ emoji: '🧠', label: 'Memory', unit: '%', value: s ? fmt.number(s.mem_pct, 0) : '—', sub: s ? `${fmt.gb(s.mem_used)} of ${fmt.gb(s.mem_total)}` : '', status: per.memory || 'unknown', spark: spark.mem }),
      statTile({ emoji: '💾', label: 'Disk', unit: '%', value: s ? fmt.number(s.disk_pct, 0) : '—', sub: s ? `${fmt.gb(s.disk_free)} free on ${s.disk_path}` : '', status: per.disk || 'unknown', spark: spark.disk }),
      statTile({ emoji: '⚖️', label: 'Load', value: s ? fmt.load(s.load1) : '—', sub: s ? `${fmt.load(s.load5)} · ${fmt.load(s.load15)} over 5 and 15 min` : '', status: per.load || 'unknown', spark: spark.load }),
    );
    for (const [metric, entry] of charts) {
      if (!s) { entry.wrap.setValue('—'); continue; }
      if (metric === 'cpu') entry.wrap.setValue(fmt.pct(s.cpu_pct, s.cpu_pct < 10 ? 1 : 0));
      else if (metric === 'mem') entry.wrap.setValue(fmt.pct(s.mem_pct));
      else if (metric === 'disk') entry.wrap.setValue(fmt.pct(s.disk_pct));
      else if (metric === 'load') entry.wrap.setValue(fmt.load(s.load1));
      else if (metric === 'net') entry.wrap.setValue(`↓ ${fmt.rate(s.net_rx_bps)} · ↑ ${fmt.rate(s.net_tx_bps)}`);
    }

    // cores
    coresBody.replaceChildren();
    if (s && Array.isArray(s.cpu_per_core) && s.cpu_per_core.length) {
      s.cpu_per_core.forEach((pct, i) => {
        coresBody.appendChild(meterRow(`Core ${i}`, meter({ value: pct, max: 100, status: thresholdStatus(pct, thresholds.cpu), label: fmt.pct(pct), ariaLabel: `Core ${i}` })));
      });
    } else {
      coresBody.appendChild(el('p', { class: 'muted', text: 'Per-core figures appear after the first sample.' }));
    }

    // memory
    memBody.replaceChildren();
    if (s) {
      const total = s.mem_total || 1;
      memBody.append(
        meterRow('Used', meter({ value: s.mem_used, max: total, status: per.memory || 'ok', label: fmt.gb(s.mem_used), ariaLabel: 'Memory used' })),
        meterRow('Available', meter({ value: s.mem_available, max: total, status: 'ok', label: fmt.gb(s.mem_available), ariaLabel: 'Memory available' })),
        meterRow('Cached', meter({ value: s.mem_cached, max: total, status: 'ok', label: fmt.gb(s.mem_cached), ariaLabel: 'Memory cached' })),
        meterRow('Swap', s.swap_total
          ? meter({ value: s.swap_used, max: s.swap_total, status: per.swap || 'ok', label: `${fmt.gb(s.swap_used)} of ${fmt.gb(s.swap_total)}`, ariaLabel: 'Swap used' })
          : el('span', { class: 'muted', text: 'No swap configured' })),
      );
    }

    // mounts
    const mounts = [];
    if (s) {
      mounts.push({ path: s.disk_path || '/', total: s.disk_total, used: s.disk_used, free: s.disk_free, pct: s.disk_pct, status: per.disk || 'ok' });
      for (const m of s.disks || []) mounts.push({ ...m, status: per[`disk:${m.path}`] || 'ok' });
    }
    mountsSection.body.replaceChildren(table({
      ariaLabel: 'Mounted filesystems',
      columns: [
        { key: 'path', label: 'Mount' },
        { key: 'total', label: 'Size', align: 'end', numeric: true, format: (v) => fmt.gb(v) },
        { key: 'used', label: 'Used', align: 'end', numeric: true, format: (v) => fmt.gb(v) },
        { key: 'free', label: 'Free', align: 'end', numeric: true, format: (v) => fmt.gb(v) },
        { key: 'pct', label: 'Use', width: '180px', format: (v, row) => meter({ value: v, max: 100, status: row.status, label: fmt.pct(v), ariaLabel: `${row.path} used` }) },
      ],
      rows: mounts,
      emptyText: 'Mounts appear after the first sample.',
    }));

    // system facts
    systemBody.replaceChildren();
    if (s) {
      systemBody.appendChild(kv([
        ['Hostname', s.hostname || status.hostname || '—'],
        ['OS', `${s.os_name || ''} ${s.os_version || ''}`.trim() || '—'],
        ['Kernel', s.kernel || '—'],
        ['Uptime', fmt.duration(s.uptime_s), true],
        ['Cores', String(s.cores ?? '—'), true],
        ['Load 1 / 5 / 15', `${fmt.load(s.load1)} · ${fmt.load(s.load5)} · ${fmt.load(s.load15)}`, true],
        ['Network now', `↓ ${fmt.rate(s.net_rx_bps)} · ↑ ${fmt.rate(s.net_tx_bps)}`, true],
        ['Inodes used', s.inode_pct === null || s.inode_pct === undefined ? '—' : fmt.pct(s.inode_pct), true],
        ['Sampled', fmt.clock(s.ts, true), true],
      ]));
    } else {
      systemBody.appendChild(el('p', { class: 'muted', text: 'Waiting for the first sample.' }));
    }
  }

  paintVitals();
  loadCharts();
  const unsubscribe = store.subscribe('status', paintVitals);
  const timer = setInterval(loadCharts, REFRESH_MS);
  return () => {
    unsubscribe();
    clearInterval(timer);
    for (const { chart } of charts.values()) chart.destroy();
  };
}
