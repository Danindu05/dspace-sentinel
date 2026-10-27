/**
 * charts.js — SVG charts for DSpace Sentinel (SPEC §12.7, built on the dataviz skill).
 *
 * No dependencies. Colours come from tokens (`--chart-1..3`, status fills, `--ink-3`, `--chart-grid`).
 * Every chart has a text twin or summary so nothing is colour-only or tooltip-only.
 *
 *   sparkline(points, {status='ok', width=120, height=28, ariaHidden=true}) -> <span class="spark">
 *       points: number[] or [ts, value][] (last 12 used). 1.5 px line in --ink-3, last-point dot in the status fill.
 *
 *   lineChart({series, unit='', thresholds, height=180, yMin=0, yMax, ariaLabel, title, formatValue,
 *              from, to, points}) -> {el, update(series | {series, thresholds, from, to}), setBusy(busy), destroy(), series}
 *       series: [{id, label, points: [[ts, value(, max)]...], color?}] — colours assigned in fixed order chart-1..3.
 *       Single series → 10 % area wash; ≥ 2 series → legend (line keys). thresholds: {warning, critical}
 *       → hairlines in --warn-fill / --crit-fill with a 12 px label in --warn-ink / --crit-ink
 *       ("Warning 80 %"). Crosshair + one tooltip listing every
 *       series at that x; keyboard: focus the plot, ←/→ move, Home/End, Esc hides. "Show as table" renders
 *       a <table> twin. update() redraws in place; setBusy(true) holds the render at 50 % opacity while
 *       refetching. Width follows the container (ResizeObserver).
 *
 *   availabilityStrip(buckets, {from, to, bucketS, label, uptimePct}) -> <div class="strip" role="img">
 *       buckets: status[] ('ok' | 'warn' | 'crit' | 'unknown' | null). One 6 px-radius bar per bucket,
 *       2 px gaps, unknown → --surface-2; tooltip (hover/keyboard) with time + status. When the row is
 *       too narrow for one cell per bucket, neighbouring buckets merge (worst status wins) so cells
 *       stay >= 4 px; the aria summary always describes the raw buckets.
 *
 *   meter({value, max=100, status='ok', label, formatValue, ariaLabel}) -> <div class="meter" role="meter"> with .update({value, max, status, label})
 *       8 px track in --surface-2, fill in the status fill, label on the right.
 *
 *   niceTicks(min, max, count=4) -> number[]   ·   formatTick(value, unit) -> string
 *   Byte axes ('B/s', 'bytes') pick one 1024-based unit from the top of the domain first and step in round
 *   numbers of it ("0 · 0.5 · 1 · 1.5 · 2 MB/s"); tooltips and table twins still pick a unit per value.
 */

const SVG_NS = 'http://www.w3.org/2000/svg';
const SERIES_COLORS = ['var(--chart-1)', 'var(--chart-2)', 'var(--chart-3)'];
const STATUS_FILL = {
  ok: 'var(--ok-fill)',
  warn: 'var(--warn-fill)',
  crit: 'var(--crit-fill)',
  unknown: 'var(--unknown-fill)',
  off: 'var(--unknown-fill)',
};
const STATUS_LABEL = { ok: 'Healthy', warn: 'Warning', crit: 'Critical', unknown: 'Unknown', off: 'Off' };

function svg(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value !== null && value !== undefined) node.setAttribute(key, String(value));
  }
  return node;
}

function html(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

const numberFormats = new Map();
function nf(decimals) {
  if (!numberFormats.has(decimals)) {
    numberFormats.set(decimals, new Intl.NumberFormat(undefined, { minimumFractionDigits: 0, maximumFractionDigits: decimals }));
  }
  return numberFormats.get(decimals);
}

const clockFmt = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit' });
// multi-day axes name the day and month ("Sep 3" / "3 Sep"): a weekday alone repeats and hides month changes
const dayFmt = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short' });
/** Time-axis steps: clock steps (seconds) up to two days, whole local days beyond. */
const CLOCK_STEPS = [300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400];
const DAY_STEPS = [1, 2, 3, 7, 14];
/** At least this many pixels per time label; the step is chosen from the plot width. */
const MIN_TICK_PX = 90;
const dayClockFmt = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });

/** No-break space between a number and its unit (tooltips and table twins wrap; the pair must not). */
const NBSP = '\u00a0';

/** Format an axis tick / tooltip value for a unit ('%', 'ms', 'B/s', 'load', 'bytes', ''). */
export function formatTick(value, unit = '') {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  const v = Number(value);
  switch (unit) {
    case '%':
      return `${nf(v < 10 ? 1 : 0).format(v)}${NBSP}%`;
    case 'ms':
      return v >= 1000 ? `${nf(1).format(v / 1000)}${NBSP}s` : `${nf(0).format(v)}${NBSP}ms`;
    case 'B/s':
    case 'bps':
      return `${formatBytes(v)}/s`;
    case 'bytes':
    case 'B':
      return formatBytes(v);
    case 'load':
      return nf(2).format(v);
    default:
      return nf(Math.abs(v) < 10 ? 2 : 0).format(v);
  }
}

const BYTE_UNITS = ['B', 'KB', 'MB', 'GB', 'TB'];
const BYTE_AXIS = new Set(['B/s', 'bps', 'bytes', 'B']);

function formatBytes(n) {
  let value = Number(n);
  let i = 0;
  while (Math.abs(value) >= 1024 && i < BYTE_UNITS.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${nf(i === 0 ? 0 : 1).format(value)}${NBSP}${BYTE_UNITS[i]}`;
}

/** The 1024-power unit an axis whose top is `max` bytes is labelled in: 1 (B), 1024 (KB), 1024² (MB) … */
function byteBase(max) {
  let base = 1;
  for (let i = 0; i < BYTE_UNITS.length - 1 && Math.abs(max) / base >= 1024; i += 1) base *= 1024;
  return base;
}

/** Fraction digits that print every multiple of `step` exactly (0.25 → 2, 0.5 → 1, 2.5 → 1, 25 → 0). */
function stepDecimals(step) {
  if (!(step > 0)) return 0;
  let d = Math.max(0, -Math.floor(Math.log10(step) + 1e-9));
  while (d < 3 && Math.abs(step * 10 ** d - Math.round(step * 10 ** d)) > 1e-6) d += 1;
  return d;
}

/**
 * The y-axis label of one axis: every tick in the same unit, with the decimals its step needs
 * ("0 · 0.25 · 0.5 ms", "0 · 0.5 · 1 · 1.5 s", "0 · 0.5 · 1 MB/s") — no "500 ms · 1 s" mixes, no "0.3" for 0.25.
 */
function axisLabel(unit, ticks, byteBaseValue) {
  const step = ticks.length > 1 ? ticks[1] - ticks[0] : 0;
  const top = ticks.length ? ticks[ticks.length - 1] : 0;
  if (byteBaseValue) {
    const name = BYTE_UNITS[Math.round(Math.log(byteBaseValue) / Math.log(1024))] || 'B';
    const d = stepDecimals(step / byteBaseValue);
    const suffix = unit === 'B/s' || unit === 'bps' ? '/s' : '';
    return (v) => `${nf(d).format(Number(v) / byteBaseValue)}${NBSP}${name}${suffix}`;
  }
  if (unit === 'ms' && top >= 1000) {
    const d = stepDecimals(step / 1000);
    return (v) => `${nf(d).format(Number(v) / 1000)}${NBSP}s`;
  }
  const d = stepDecimals(step);
  if (unit === 'ms') return (v) => `${nf(d).format(Number(v))}${NBSP}ms`;
  if (unit === '%') return (v) => `${nf(d).format(Number(v))}${NBSP}%`;
  return (v) => nf(d).format(Number(v));
}

/** Clean axis ticks between min and max. */
export function niceTicks(min, max, count = 4) {
  if (!(max > min)) return [min];
  const span = max - min;
  const rough = span / count;
  const mag = 10 ** Math.floor(Math.log10(rough));
  const candidates = [1, 2, 2.5, 5, 10].map((m) => m * mag);
  const step = candidates.find((c) => span / c <= count + 0.5) || candidates[candidates.length - 1];
  const ticks = [];
  const start = Math.ceil(min / step) * step;
  for (let v = start; v <= max + step * 1e-6; v += step) ticks.push(Number(v.toFixed(10)));
  return ticks;
}

function niceMax(value) {
  if (!(value > 0)) return 1;
  const mag = 10 ** Math.floor(Math.log10(value));
  const n = value / mag;
  const m = n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10;
  return m * mag;
}

function toPairs(points) {
  if (!Array.isArray(points)) return [];
  return points.map((p, i) => (Array.isArray(p) ? [Number(p[0]), p[1] === null || p[1] === undefined ? null : Number(p[1])] : [i, p === null || p === undefined ? null : Number(p)]));
}

// ------------------------------------------------------------------ sparkline

/**
 * A 12-point sparkline for stat tiles.
 * @param {number[]|Array<[number, number]>} points
 * @param {{status?: string, width?: number, height?: number, ariaHidden?: boolean}} [opts]
 * @returns {HTMLElement}
 */
export function sparkline(points, { status = 'ok', width = 120, height = 28, ariaHidden = true } = {}) {
  const pairs = toPairs(points).slice(-12);
  const wrap = html('span', 'spark');
  if (ariaHidden) wrap.setAttribute('aria-hidden', 'true');
  const values = pairs.map((p) => p[1]).filter((v) => v !== null && !Number.isNaN(v));
  if (values.length < 2) {
    wrap.classList.add('spark--empty');
    return wrap;
  }
  const min = Math.min(...values);
  const max = Math.max(...values);
  const pad = 3;
  const span = max - min || 1;
  const n = pairs.length;
  const xs = pairs.map((_, i) => pad + (i / (n - 1)) * (width - pad * 2));
  const ys = pairs.map((p) => (p[1] === null ? null : height - pad - ((p[1] - min) / span) * (height - pad * 2)));
  let d = '';
  let pen = false;
  for (let i = 0; i < n; i += 1) {
    if (ys[i] === null) {
      pen = false;
      continue;
    }
    d += `${pen ? 'L' : 'M'}${xs[i].toFixed(1)} ${ys[i].toFixed(1)} `;
    pen = true;
  }
  const node = svg('svg', { viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: 'none', class: 'spark-svg', focusable: 'false' });
  node.appendChild(svg('path', { d: d.trim(), fill: 'none', stroke: 'var(--ink-3)', 'stroke-width': 1.5, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'vector-effect': 'non-scaling-stroke' }));
  wrap.appendChild(node);
  const lastIdx = ys.length - 1;
  if (ys[lastIdx] !== null) {
    const dot = html('span', 'spark-dot');
    dot.style.left = `${((xs[lastIdx] / width) * 100).toFixed(2)}%`;
    dot.style.top = `${((ys[lastIdx] / height) * 100).toFixed(2)}%`;
    dot.style.background = STATUS_FILL[status] || STATUS_FILL.unknown;
    wrap.appendChild(dot);
  }
  return wrap;
}

// ------------------------------------------------------------------ line chart

const MARGIN = { top: 10, right: 14, bottom: 24 };

/**
 * A multi-series SVG line chart with crosshair, tooltip, keyboard, legend, thresholds and a table twin.
 * @returns {{el: HTMLElement, update: Function, setBusy: Function, destroy: Function, series: object[]}}
 */
export function lineChart({ series = [], unit = '', thresholds = null, height = 180, yMin = 0, yMax, ariaLabel, title, formatValue, from, to } = {}) {
  const fig = html('figure', 'chart');
  const head = html('div', 'chart-head');
  const legend = html('div', 'chart-legend');
  legend.setAttribute('aria-label', 'Legend');
  const tableToggle = html('button', 'chart-table-toggle', 'Show as table');
  tableToggle.type = 'button';
  tableToggle.setAttribute('aria-pressed', 'false');
  head.appendChild(legend);
  head.appendChild(tableToggle);
  const plot = html('div', 'chart-plot');
  plot.setAttribute('role', 'img');
  plot.tabIndex = 0;
  plot.style.height = `${height}px`;
  const svgRoot = svg('svg', { class: 'chart-svg', width: '100%', height, focusable: 'false', 'aria-hidden': 'true' });
  const gGrid = svg('g', { class: 'chart-grid' });
  const gThresholds = svg('g', { class: 'chart-thresholds' });
  const gSeries = svg('g', { class: 'chart-series' });
  // threshold labels sit above the data (their surface halo keeps them legible where a spike
  // crosses them); the hairlines stay below it
  const gThresholdLabels = svg('g', { class: 'chart-threshold-labels' });
  const gAxis = svg('g', { class: 'chart-axis' });
  const gCross = svg('g', { class: 'chart-crosshair-g' });
  svgRoot.appendChild(gGrid);
  svgRoot.appendChild(gThresholds);
  svgRoot.appendChild(gSeries);
  svgRoot.appendChild(gThresholdLabels);
  svgRoot.appendChild(gAxis);
  svgRoot.appendChild(gCross);
  const tooltip = html('div', 'chart-tooltip');
  tooltip.hidden = true;
  const empty = html('div', 'chart-empty');
  empty.appendChild(html('span', 'empty-emoji', '📈'));
  empty.appendChild(html('span', 'chart-empty-text', 'Collecting the first samples — charts appear after a minute.'));
  empty.hidden = true;
  plot.appendChild(svgRoot);
  plot.appendChild(tooltip);
  plot.appendChild(empty);
  const tableWrap = html('div', 'chart-table');
  tableWrap.hidden = true;
  fig.appendChild(head);
  fig.appendChild(plot);
  fig.appendChild(tableWrap);

  const state = {
    series: [],
    thresholds,
    from,
    to,
    width: 0,
    xs: [],
    cursor: -1,
    layout: null,
    destroyed: false,
    tableDirty: true,
  };
  const fmtValue = formatValue || ((v) => formatTick(v, unit));
  const bytesAxis = !formatValue && BYTE_AXIS.has(unit);

  function normalise(list) {
    return (list || []).map((s, i) => ({
      id: s.id || `s${i}`,
      label: s.label || s.id || `Series ${i + 1}`,
      color: s.color || SERIES_COLORS[i % SERIES_COLORS.length],
      points: toPairs(s.points).filter((p) => !Number.isNaN(p[0])),
    }));
  }

  function domainX() {
    let lo = Number.isFinite(state.from) ? state.from : Infinity;
    let hi = Number.isFinite(state.to) ? state.to : -Infinity;
    for (const s of state.series) {
      if (s.points.length) {
        lo = Math.min(lo, s.points[0][0]);
        hi = Math.max(hi, s.points[s.points.length - 1][0]);
      }
    }
    if (!Number.isFinite(lo) || !Number.isFinite(hi)) {
      const now = Date.now() / 1000;
      return [now - 3600, now];
    }
    if (hi <= lo) hi = lo + 60;
    return [lo, hi];
  }

  function domainY() {
    let dataMax = -Infinity;
    for (const s of state.series) for (const p of s.points) if (p[1] !== null) dataMax = Math.max(dataMax, p[1]);
    if (!Number.isFinite(dataMax)) dataMax = 0;
    if (yMax !== undefined && yMax !== null) return [yMin, yMax];
    if (unit === '%') return [yMin, 100];
    let top = dataMax * 1.1;
    const th = state.thresholds || {};
    if (Number.isFinite(th.warning) && th.warning <= Math.max(dataMax, 1) * 2.5) top = Math.max(top, th.warning * 1.05);
    if (Number.isFinite(th.critical) && th.critical <= Math.max(dataMax, 1) * 2.5) top = Math.max(top, th.critical * 1.05);
    // a flat zero series still gets a readable axis (0 … 1) instead of five "0" ticks
    if (!(top > yMin)) top = yMin + (bytesAxis ? 1024 : 1);
    if (bytesAxis) {
      // pick the unit first (KB, MB …), then round in that unit: the top is 2 MB, not 1.9 MB,
      // and a top that would read "1,000 KB" moves up to "1 MB"
      let base = byteBase(top);
      if (niceMax(top / base) >= 1000 && base < 1024 ** (BYTE_UNITS.length - 1)) base *= 1024;
      return [yMin, niceMax(top / base) * base];
    }
    return [yMin, niceMax(top)];
  }

  /** Y-axis ticks and their label: byte axes step in round numbers of one 1024-based unit (0 · 0.5 · 1 · 1.5 · 2 MB/s). */
  function yAxis(yLo, yHi) {
    if (formatValue) return { ticks: niceTicks(yLo, yHi, 4), label: fmtValue };
    if (!bytesAxis) {
      const ticks = niceTicks(yLo, yHi, 4);
      return { ticks, label: axisLabel(unit, ticks, 0) };
    }
    const base = byteBase(yHi);
    const ticks = niceTicks(yLo / base, yHi / base, 4).map((v) => v * base);
    return { ticks, label: axisLabel(unit, ticks, base) };
  }

  function timeLabel(ts, spanS) {
    const d = new Date(ts * 1000);
    return spanS > 2 * 86400 ? dayFmt.format(d) : clockFmt.format(d);
  }

  /**
   * Time ticks for [lo, hi] on a plot `plotW` px wide: as many as fit at MIN_TICK_PX each. Spans over two
   * days step in whole days from local midnight (a 7 d chart gets a label per day when it is wide enough);
   * shorter spans step on the local clock (every 3 h, 30 min …).
   */
  function xTicks(lo, hi, plotW) {
    const span = hi - lo;
    // the smallest step whose labels stay MIN_TICK_PX apart on this plot
    const minStep = (MIN_TICK_PX * span) / Math.max(1, plotW);
    const ticks = [];
    if (span > 2 * 86400) {
      const days = DAY_STEPS.find((n) => n * 86400 >= minStep) || DAY_STEPS[DAY_STEPS.length - 1];
      const day = new Date(lo * 1000);
      day.setHours(0, 0, 0, 0);
      if (day.getTime() / 1000 < lo) day.setDate(day.getDate() + 1);
      for (let n = 0; day.getTime() / 1000 <= hi && n < 400; n += 1) {
        ticks.push(day.getTime() / 1000);
        day.setDate(day.getDate() + days); // calendar days, so DST changes keep midnight
      }
      return ticks;
    }
    const step = CLOCK_STEPS.find((sec) => sec >= minStep) || CLOCK_STEPS[CLOCK_STEPS.length - 1];
    const offset = new Date().getTimezoneOffset() * 60;
    const start = Math.ceil((lo - offset) / step) * step + offset;
    for (let t = start; t <= hi; t += step) ticks.push(t);
    return ticks;
  }

  function measureLeft(ticks, label = fmtValue) {
    const longest = ticks.reduce((acc, t) => Math.max(acc, label(t).length), 0);
    return Math.max(34, Math.min(72, 10 + longest * 7));
  }

  function draw() {
    if (state.destroyed) return;
    const width = state.width || plot.clientWidth || 600;
    state.width = width;
    const hasData = state.series.some((s) => s.points.some((p) => p[1] !== null));
    empty.hidden = hasData;
    svgRoot.setAttribute('viewBox', `0 0 ${width} ${height}`);
    const [xLo, xHi] = domainX();
    const [yLo, yHi] = domainY();
    const { ticks: yTicks, label: yLabel } = yAxis(yLo, yHi);
    const left = measureLeft(yTicks, yLabel);
    const plotW = Math.max(10, width - left - MARGIN.right);
    const plotH = Math.max(10, height - MARGIN.top - MARGIN.bottom);
    const x = (t) => left + ((t - xLo) / (xHi - xLo)) * plotW;
    const y = (v) => MARGIN.top + plotH - ((v - yLo) / (yHi - yLo || 1)) * plotH;
    state.layout = { left, plotW, plotH, x, y, xLo, xHi, yLo, yHi };

    // grid + y axis
    gGrid.replaceChildren();
    gAxis.replaceChildren();
    for (const t of yTicks) {
      gGrid.appendChild(svg('line', { x1: left, x2: left + plotW, y1: y(t), y2: y(t), stroke: 'var(--chart-grid)', 'stroke-width': 1, 'shape-rendering': 'crispEdges' }));
      const label = svg('text', { x: left - 6, y: y(t) + 4, 'text-anchor': 'end', class: 'chart-tick' });
      label.textContent = yLabel(t);
      gAxis.appendChild(label);
    }
    for (const t of xTicks(xLo, xHi, plotW)) {
      // a label near either end of the plot hangs inward instead of past the edge
      const px = x(t);
      const anchor = px - left < 28 ? 'start' : left + plotW - px < 28 ? 'end' : 'middle';
      const label = svg('text', { x: px, y: height - 6, 'text-anchor': anchor, class: 'chart-tick' });
      label.textContent = timeLabel(t, xHi - xLo);
      gAxis.appendChild(label);
    }

    // thresholds
    gThresholds.replaceChildren();
    gThresholdLabels.replaceChildren();
    const th = state.thresholds || {};
    // hairline in the status fill (a mark, ≥ 3:1); its label is text, so it takes the status ink (≥ 4.5:1)
    for (const [key, line, ink, name] of [['warning', 'var(--warn-fill)', 'var(--warn-ink)', 'Warning'], ['critical', 'var(--crit-fill)', 'var(--crit-ink)', 'Critical']]) {
      const v = th[key];
      if (!Number.isFinite(v) || v < yLo || v > yHi) continue;
      gThresholds.appendChild(svg('line', { x1: left, x2: left + plotW, y1: y(v), y2: y(v), stroke: line, 'stroke-width': 1, 'shape-rendering': 'crispEdges' }));
      // label at the left edge: the latest samples (and their spikes) live at the right, where the reader looks
      const label = svg('text', { x: left + 4, y: y(v) - 3, 'text-anchor': 'start', class: 'chart-threshold-label' });
      label.setAttribute('fill', ink);
      label.textContent = `${name} ${fmtValue(v)}`;
      gThresholdLabels.appendChild(label);
    }

    // series
    gSeries.replaceChildren();
    const single = state.series.length === 1;
    for (const s of state.series) {
      let d = '';
      let pen = false;
      let areaD = '';
      let segmentStart = null;
      const closeArea = (lastX) => {
        if (segmentStart !== null && lastX !== null) areaD += `L${lastX.toFixed(1)} ${(MARGIN.top + plotH).toFixed(1)} L${segmentStart.toFixed(1)} ${(MARGIN.top + plotH).toFixed(1)} Z `;
        segmentStart = null;
      };
      let lastX = null;
      for (const [t, v] of s.points) {
        if (v === null || Number.isNaN(v)) {
          pen = false;
          closeArea(lastX);
          continue;
        }
        const px = x(t);
        const py = y(Math.min(Math.max(v, yLo), yHi));
        if (!pen) {
          segmentStart = px;
          areaD += `M${px.toFixed(1)} ${py.toFixed(1)} `;
        } else {
          areaD += `L${px.toFixed(1)} ${py.toFixed(1)} `;
        }
        d += `${pen ? 'L' : 'M'}${px.toFixed(1)} ${py.toFixed(1)} `;
        pen = true;
        lastX = px;
      }
      closeArea(lastX);
      if (single && areaD) gSeries.appendChild(svg('path', { d: areaD.trim(), fill: s.color, 'fill-opacity': 0.1, stroke: 'none' }));
      gSeries.appendChild(svg('path', { d: d.trim(), fill: 'none', stroke: s.color, 'stroke-width': 2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }));
    }

    // crosshair positions: union of timestamps
    const set = new Set();
    for (const s of state.series) for (const p of s.points) set.add(p[0]);
    state.xs = Array.from(set).sort((a, b) => a - b);
    if (state.cursor >= state.xs.length) state.cursor = state.xs.length - 1;

    // legend
    legend.replaceChildren();
    if (state.series.length >= 2) {
      for (const s of state.series) {
        const item = html('span', 'legend-item');
        const key = html('span', 'legend-key');
        key.style.background = s.color;
        item.appendChild(key);
        item.appendChild(html('span', 'legend-label', s.label));
        legend.appendChild(item);
      }
    }
    legend.hidden = state.series.length < 2;

    plot.setAttribute('aria-label', summary());
    state.tableDirty = true;
    if (!tableWrap.hidden) renderTable();
    paintCursor();
  }

  function summary() {
    if (ariaLabel) return ariaLabel;
    const parts = [];
    for (const s of state.series) {
      const values = s.points.map((p) => p[1]).filter((v) => v !== null && !Number.isNaN(v));
      if (!values.length) {
        parts.push(`${s.label}: no data yet`);
        continue;
      }
      const min = Math.min(...values);
      const max = Math.max(...values);
      const last = values[values.length - 1];
      parts.push(`${s.label}: latest ${fmtValue(last)}, low ${fmtValue(min)}, high ${fmtValue(max)}`);
    }
    return `${title ? `${title}. ` : ''}${parts.join('; ')}. Line chart; press Show as table for the values.`;
  }

  function valueAt(s, t) {
    if (!s.points.length) return null;
    let lo = 0;
    let hi = s.points.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (s.points[mid][0] < t) lo = mid + 1;
      else hi = mid;
    }
    const cand = [s.points[lo], s.points[lo - 1]].filter(Boolean);
    cand.sort((a, b) => Math.abs(a[0] - t) - Math.abs(b[0] - t));
    const best = cand[0];
    if (!best) return null;
    const tolerance = state.xs.length > 1 ? (state.xs[state.xs.length - 1] - state.xs[0]) / state.xs.length * 1.5 : Infinity;
    return Math.abs(best[0] - t) <= tolerance ? best[1] : null;
  }

  function paintCursor() {
    gCross.replaceChildren();
    if (state.cursor < 0 || !state.layout || !state.xs.length) {
      tooltip.hidden = true;
      return;
    }
    const t = state.xs[state.cursor];
    const { x, y, left, plotW, plotH } = state.layout;
    const px = x(t);
    gCross.appendChild(svg('line', { x1: px, x2: px, y1: MARGIN.top, y2: MARGIN.top + plotH, stroke: 'var(--ink-3)', 'stroke-width': 1, 'shape-rendering': 'crispEdges' }));
    tooltip.replaceChildren();
    tooltip.appendChild(html('div', 'chart-tooltip-time', dayClockFmt.format(new Date(t * 1000))));
    for (const s of state.series) {
      const v = valueAt(s, t);
      if (v !== null) {
        gCross.appendChild(svg('circle', { cx: px, cy: y(Math.min(Math.max(v, state.layout.yLo), state.layout.yHi)), r: 4, fill: s.color, stroke: 'var(--surface)', 'stroke-width': 2 }));
      }
      const row = html('div', 'chart-tooltip-row');
      const key = html('span', 'legend-key');
      key.style.background = s.color;
      row.appendChild(key);
      row.appendChild(html('b', 'chart-tooltip-value', v === null ? '—' : fmtValue(v)));
      row.appendChild(html('span', 'chart-tooltip-label', s.label));
      tooltip.appendChild(row);
    }
    tooltip.hidden = false;
    const tw = tooltip.offsetWidth || 160;
    const flip = px + 12 + tw > left + plotW;
    tooltip.style.left = `${flip ? px - 12 - tw : px + 12}px`;
    tooltip.style.top = `${MARGIN.top}px`;
  }

  function indexAt(clientX) {
    if (!state.layout || !state.xs.length) return -1;
    const rect = plot.getBoundingClientRect();
    const px = clientX - rect.left;
    const { xLo, xHi, left, plotW } = state.layout;
    const t = xLo + ((px - left) / plotW) * (xHi - xLo);
    let lo = 0;
    let hi = state.xs.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (state.xs[mid] < t) lo = mid + 1;
      else hi = mid;
    }
    if (lo > 0 && Math.abs(state.xs[lo - 1] - t) < Math.abs(state.xs[lo] - t)) lo -= 1;
    return lo;
  }

  function setCursor(idx) {
    const next = Math.max(-1, Math.min(idx, state.xs.length - 1));
    if (next === state.cursor) return;
    state.cursor = next;
    paintCursor();
  }

  plot.addEventListener('pointermove', (e) => setCursor(indexAt(e.clientX)));
  plot.addEventListener('pointerleave', () => { if (document.activeElement !== plot) setCursor(-1); });
  plot.addEventListener('keydown', (e) => {
    if (!state.xs.length) return;
    const step = e.shiftKey ? Math.max(1, Math.round(state.xs.length / 12)) : 1;
    if (e.key === 'ArrowRight') { e.preventDefault(); setCursor(state.cursor < 0 ? state.xs.length - 1 : state.cursor + step); }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); setCursor(state.cursor < 0 ? state.xs.length - 1 : state.cursor - step); }
    else if (e.key === 'Home') { e.preventDefault(); setCursor(0); }
    else if (e.key === 'End') { e.preventDefault(); setCursor(state.xs.length - 1); }
    else if (e.key === 'Escape' && state.cursor >= 0) { e.stopPropagation(); setCursor(-1); }
  });
  plot.addEventListener('focus', () => { tooltip.setAttribute('aria-live', 'polite'); if (state.cursor < 0 && state.xs.length) setCursor(state.xs.length - 1); });
  plot.addEventListener('blur', () => { tooltip.removeAttribute('aria-live'); setCursor(-1); });

  function renderTable() {
    if (!state.tableDirty) return;
    state.tableDirty = false;
    const tbl = document.createElement('table');
    tbl.className = 'table table--chart';
    const cap = document.createElement('caption');
    cap.className = 'sr-only';
    cap.textContent = title ? `${title} as a table` : 'Chart values';
    tbl.appendChild(cap);
    const thead = document.createElement('thead');
    const hr = document.createElement('tr');
    const th0 = document.createElement('th');
    th0.scope = 'col';
    th0.textContent = 'Time';
    hr.appendChild(th0);
    for (const s of state.series) {
      const th = document.createElement('th');
      th.scope = 'col';
      th.className = 'align-end';
      th.textContent = s.label;
      hr.appendChild(th);
    }
    thead.appendChild(hr);
    tbl.appendChild(thead);
    const tbody = document.createElement('tbody');
    const rows = state.xs.slice().reverse();
    for (const t of rows) {
      const tr = document.createElement('tr');
      const th = document.createElement('th');
      th.scope = 'row';
      th.textContent = dayClockFmt.format(new Date(t * 1000));
      tr.appendChild(th);
      for (const s of state.series) {
        const td = document.createElement('td');
        td.className = 'align-end numeric';
        const v = valueAt(s, t);
        td.textContent = v === null ? '—' : fmtValue(v);
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
    tbl.appendChild(tbody);
    tableWrap.replaceChildren(tbl);
  }

  tableToggle.addEventListener('click', () => {
    const show = tableWrap.hidden;
    tableWrap.hidden = !show;
    tableToggle.setAttribute('aria-pressed', show ? 'true' : 'false');
    tableToggle.textContent = show ? 'Hide table' : 'Show as table';
    if (show) renderTable();
  });

  let ro = null;
  if (typeof ResizeObserver !== 'undefined') {
    ro = new ResizeObserver((entries) => {
      const w = Math.floor(entries[0].contentRect.width);
      if (w > 0 && w !== state.width) {
        state.width = w;
        draw();
      }
    });
    ro.observe(plot);
  }

  const handle = {
    el: fig,
    get series() {
      return state.series;
    },
    /** Redraw with new data (`series[]` or `{series, thresholds, from, to}`), no flash. */
    update(next) {
      if (Array.isArray(next)) state.series = normalise(next);
      else if (next && typeof next === 'object') {
        if (next.series) state.series = normalise(next.series);
        if ('thresholds' in next) state.thresholds = next.thresholds;
        if ('from' in next) state.from = next.from;
        if ('to' in next) state.to = next.to;
      }
      draw();
    },
    /** Hold the previous render at 50 % while refetching. */
    setBusy(busy) {
      fig.classList.toggle('is-busy', Boolean(busy));
      fig.setAttribute('aria-busy', busy ? 'true' : 'false');
    },
    destroy() {
      state.destroyed = true;
      if (ro) ro.disconnect();
    },
  };

  state.series = normalise(series);
  // First draw once the element has a width (next frame when attached).
  requestAnimationFrame(() => { if (!state.destroyed) draw(); });
  return handle;
}

// ------------------------------------------------------------------ availability strip

/**
 * One bar per availability bucket.
 * @param {Array<string|null>} buckets
 * @param {{from?: number, to?: number, bucketS?: number, label?: string, uptimePct?: number}} [opts]
 * @returns {HTMLElement}
 */
export function availabilityStrip(buckets = [], { from, to, bucketS, label = '', uptimePct } = {}) {
  const wrap = html('div', 'strip-wrap');
  const strip = html('div', 'strip');
  strip.setAttribute('role', 'img');
  strip.tabIndex = 0;
  const raw = buckets.map((status) => (status && STATUS_FILL[status] ? (status === 'off' ? 'unknown' : status) : 'unknown'));
  const n = raw.length;
  const span = Number.isFinite(from) && Number.isFinite(to) ? (to - from) / Math.max(1, n) : bucketS || 0;
  const counts = { ok: 0, warn: 0, crit: 0, unknown: 0 };
  for (const s of raw) counts[s] += 1;
  const summary = `${label ? `${label}: ` : ''}${Number.isFinite(uptimePct) ? `${nf(1).format(uptimePct)} % uptime, ` : ''}${counts.ok} healthy, ${counts.warn} warning, ${counts.crit} critical, ${counts.unknown} unknown of ${n} intervals`;
  strip.setAttribute('aria-label', summary);
  const tip = html('div', 'chart-tooltip strip-tooltip');
  tip.hidden = true;
  wrap.appendChild(strip);
  wrap.appendChild(tip);

  // Narrow rows merge neighbouring buckets (worst status wins) so a cell is never thinner than
  // 4 px + the 2 px gap; `group` buckets make one cell. Rebuilt when the width changes.
  const MIN_CELL = 6;
  const RANK = { unknown: 0, ok: 1, warn: 2, crit: 3 };
  let group = 1;
  let cells = [];
  let cursor = -1;

  function worst(start, end) {
    let best = 'unknown';
    for (let i = start; i < end && i < n; i += 1) if (RANK[raw[i]] > RANK[best]) best = raw[i];
    return best;
  }

  function build() {
    strip.replaceChildren();
    cells = [];
    for (let start = 0; start < n; start += group) {
      const s = worst(start, start + group);
      const cell = html('span', `strip-cell strip-cell--${s}`);
      cell.dataset.index = String(start);
      strip.appendChild(cell);
      cells.push(cell);
    }
    cursor = -1;
    tip.hidden = true;
  }

  function fit(width) {
    if (!n || !(width > 0)) return;
    const maxCells = Math.max(8, Math.floor((width + 2) / MIN_CELL));
    const next = Math.max(1, Math.ceil(n / maxCells));
    if (next !== group || !cells.length) {
      group = next;
      build();
    }
  }

  function timeOf(i) {
    if (!Number.isFinite(from) || !span) return '';
    const start = from + i * span;
    const end = start + span * Math.min(group, n - i);
    return `${dayClockFmt.format(new Date(start * 1000))} – ${clockFmt.format(new Date(end * 1000))}`;
  }

  function show(ci) {
    cells.forEach((c, j) => c.classList.toggle('is-active', j === ci));
    if (ci < 0 || ci >= cells.length) {
      tip.hidden = true;
      cursor = -1;
      return;
    }
    cursor = ci;
    const start = ci * group;
    const s = worst(start, start + group);
    tip.replaceChildren();
    const t = timeOf(start);
    if (t) tip.appendChild(html('div', 'chart-tooltip-time', t));
    const row = html('div', 'chart-tooltip-row');
    const key = html('span', 'legend-key');
    key.style.background = STATUS_FILL[s];
    row.appendChild(key);
    row.appendChild(html('b', 'chart-tooltip-value', STATUS_LABEL[s]));
    if (group > 1) row.appendChild(html('span', 'chart-tooltip-label', `worst of ${Math.min(group, n - start)} intervals`));
    tip.appendChild(row);
    tip.hidden = false;
    const rect = strip.getBoundingClientRect();
    const cellRect = cells[ci].getBoundingClientRect();
    const x = cellRect.left - rect.left + cellRect.width / 2;
    const tw = tip.offsetWidth || 140;
    tip.style.left = `${Math.max(0, Math.min(x - tw / 2, rect.width - tw))}px`;
  }

  strip.addEventListener('pointermove', (e) => {
    if (!cells.length) return;
    const rect = strip.getBoundingClientRect();
    const i = Math.floor(((e.clientX - rect.left) / rect.width) * cells.length);
    show(Math.max(0, Math.min(cells.length - 1, i)));
  });
  strip.addEventListener('pointerleave', () => { if (document.activeElement !== strip) show(-1); });
  strip.addEventListener('focus', () => { tip.setAttribute('aria-live', 'polite'); show(cells.length - 1); });
  strip.addEventListener('blur', () => { tip.removeAttribute('aria-live'); show(-1); });
  strip.addEventListener('keydown', (e) => {
    const last = cells.length - 1;
    if (last < 0) return;
    if (e.key === 'ArrowRight') { e.preventDefault(); show(Math.min(last, (cursor < 0 ? last : cursor) + 1)); }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); show(Math.max(0, (cursor < 0 ? last : cursor) - 1)); }
    else if (e.key === 'Home') { e.preventDefault(); show(0); }
    else if (e.key === 'End') { e.preventDefault(); show(last); }
  });

  build();
  if (typeof ResizeObserver !== 'undefined') {
    const ro = new ResizeObserver((entries) => fit(Math.floor(entries[0].contentRect.width)));
    ro.observe(strip);
  } else {
    requestAnimationFrame(() => fit(strip.clientWidth));
  }
  return wrap;
}

// ------------------------------------------------------------------ meter

/**
 * A slim meter: 8 px track, fill in the status colour, label on the right.
 * @returns {HTMLElement & {update(next: {value?: number, max?: number, status?: string, label?: string}): void}}
 */
export function meter({ value = 0, max = 100, status = 'ok', label, formatValue, ariaLabel } = {}) {
  const node = html('div', 'meter');
  node.setAttribute('role', 'meter');
  const track = html('span', 'meter-track');
  const fill = html('span', 'meter-fill');
  track.appendChild(fill);
  const labelEl = html('span', 'meter-label');
  node.appendChild(track);
  node.appendChild(labelEl);
  const state = { value, max, status, label };
  function paint() {
    const maxV = state.max > 0 ? state.max : 100;
    const pct = Math.max(0, Math.min(100, (Number(state.value) / maxV) * 100));
    fill.style.width = `${pct.toFixed(2)}%`;
    fill.style.background = STATUS_FILL[state.status] || STATUS_FILL.unknown;
    const text = state.label !== undefined && state.label !== null
      ? state.label
      : formatValue ? formatValue(state.value, state.max) : `${nf(0).format(pct)}${NBSP}%`;
    labelEl.textContent = text;
    node.setAttribute('aria-valuemin', '0');
    node.setAttribute('aria-valuemax', String(maxV));
    node.setAttribute('aria-valuenow', String(Number(state.value) || 0));
    node.setAttribute('aria-valuetext', `${text}, ${STATUS_LABEL[state.status] || 'Unknown'}`);
    if (ariaLabel) node.setAttribute('aria-label', ariaLabel);
  }
  node.update = (next = {}) => {
    Object.assign(state, next);
    paint();
  };
  paint();
  return node;
}

export default { sparkline, lineChart, availabilityStrip, meter, niceTicks, formatTick };
