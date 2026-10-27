/**
 * screens/detail/report.js — #/history/reports/:id (SPEC §12.5).
 *
 * The report's facts (created, range, format, size) with Open / Download / Delete in the page
 * header, and the report itself: HTML in a sandboxed frame, Markdown and JSON as text.
 * Delete is instant with Undo (POST /api/reports/{id}/restore) and returns to History.
 */
import api from '../../api.js';
import { button, el, emptyState, fmt, grid, section, snackbar, tile } from '../../ui.js';

export const title = 'Report';

export const FORMAT_LABEL = { html: 'HTML', md: 'Markdown', json: 'JSON' };
/** The generator's default title ("DSpace Sentinel report · <host> · Last 24 hours"); anything else is the operator's own. */
const DEFAULT_TITLE_RE = /^(DSpace )?Sentinel report\b/i;
const RANGE_TITLES = [[3600, 'Last hour'], [6 * 3600, 'Last 6 hours'], [86400, 'Last 24 hours'], [7 * 86400, 'Last 7 days'], [14 * 86400, 'Last 14 days'], [30 * 86400, 'Last 30 days']];

/** The preset label of a span ("Last 24 hours"), or null for a custom range. */
function presetTitle(seconds) {
  for (const [s, label] of RANGE_TITLES) if (Math.abs(seconds - s) <= Math.max(120, s * 0.02)) return label;
  return null;
}

/** "Last 24 hours" / "Last 7 days" for a preset span, else "3 days" / "5 hours". */
export function rangeTitle(seconds) {
  const preset = presetTitle(seconds);
  if (preset) return preset;
  if (seconds >= 86400 * 1.5) return `${Math.round(seconds / 86400)} days`;
  return `${Math.max(1, Math.round(seconds / 3600))} hours`;
}

const DAY_SPAN_FMT = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short' });
const TIME_SPAN_FMT = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });

function atMidnight(ts) {
  const d = new Date(Number(ts) * 1000);
  return d.getHours() === 0 && d.getMinutes() === 0 && d.getSeconds() === 0;
}

/**
 * The days or times a report covers: "Sep 20 – 24" for whole days (the range ends at the following
 * midnight, which the operator never picked, so the last day shown is the one before it), else
 * "Sep 24, 11:53 AM – Sep 25, 11:53 AM".
 */
export function coveredSpan(from, to) {
  if (!Number.isFinite(Number(from)) || !Number.isFinite(Number(to))) return '—';
  const days = atMidnight(from) && atMidnight(to) && to > from;
  const start = new Date(Number(from) * 1000);
  const end = new Date((Number(to) - (days ? 1 : 0)) * 1000);
  const f = days ? DAY_SPAN_FMT : TIME_SPAN_FMT;
  try {
    return f.formatRange(start, end);
  } catch {
    return `${f.format(start)} – ${f.format(end)}`;
  }
}

/**
 * What a report tile shows. The title is the operator's own, else the range: "Last 24 hours" for a
 * preset, the days it covers ("Sep 20 – 24") for a custom range. The subtitle names the covered span
 * when the title does not, the format, when it was generated (the list is in that order) and its size:
 * "Sep 20 – 24 · Markdown · generated Sep 25, 11:53 AM · 5.7 KB".
 * @returns {{title: string, subtitle: string, custom: boolean}}
 */
export function reportTitles(rep) {
  const from = rep.range_from || 0;
  const to = rep.range_to || 0;
  const covers = presetTitle(to - from) || coveredSpan(from, to);
  const custom = Boolean(rep.title) && !DEFAULT_TITLE_RE.test(rep.title);
  const made = rep.created_ts ? `generated ${fmt.dateTime(rep.created_ts)}` : null;
  const parts = [custom ? covers : null, FORMAT_LABEL[rep.fmt] || rep.fmt, made, fmt.bytes(rep.size)].filter(Boolean);
  return { title: custom ? rep.title : covers, subtitle: parts.join(' · '), custom };
}

/** Resolve the element matching `selector` once it is in the page (null after `timeoutMs`). */
function whenPresent(selector, timeoutMs = 2500) {
  return new Promise((resolve) => {
    const started = Date.now();
    const look = () => {
      const node = document.querySelector(selector);
      if (node || Date.now() - started > timeoutMs) resolve(node);
      else setTimeout(look, 100);
    };
    look();
  });
}

/**
 * Render one report.
 * @returns {() => void} destroy
 */
export async function render(root, params, ctx) {
  const id = Number(params.id);
  let rep;
  try {
    rep = await api.get(`api/reports/${id}`, { meta: true }, { signal: ctx.signal });
  } catch (err) {
    if (err && err.name === 'AbortError') return () => {};
    const back = button({ label: '‹ Back to History', variant: 'ghost', onClick: () => ctx.navigate('#/history') });
    root.replaceChildren(emptyState('📄', err.status === 404 ? 'This report is gone — deleted reports stay restorable for 24 hours, then they are purged.' : `Report not loaded — ${err.message}`, { action: back }));
    return () => {};
  }
  if (ctx.signal.aborted) return () => {};
  const names = reportTitles(rep);
  ctx.setTitle(names.title || `Report #${rep.id}`);

  const url = String(api.reportUrl(rep.id));
  const isHtml = rep.fmt === 'html';
  const open = button({ label: 'Open in new tab', variant: 'ghost', onClick: () => window.open(url, '_blank', 'noopener') });
  const download = el('a', { class: 'btn btn--ghost', href: `${url}?download=true`, download: rep.filename || '' }, el('span', { class: 'btn-label', text: 'Download' }));
  const del = button({ label: 'Delete', variant: 'danger', busyLabel: 'Deleting…', onClick: remove });
  ctx.setControls(isHtml ? open : null, download, del);

  const spanText = rangeTitle(rep.range_to - rep.range_from);
  const facts = section({ title: 'About this report', layout: 'stack', id: 'report-facts' });
  const factsTile = tile({ emoji: '🧾', title: rep.title || `Report #${rep.id}`, subtitle: `${FORMAT_LABEL[rep.fmt] || rep.fmt}${rep.filename ? ` · ${rep.filename}` : ''}`, className: 'tile--block' });
  const dl = el('dl', 'kv');
  for (const [k, v, numeric] of [
    ['Generated', fmt.dateTime(rep.created_ts), true],
    ['Covers', `${coveredSpan(rep.range_from, rep.range_to)} · ${spanText}`, true],
    ['Size', fmt.bytes(rep.size), true],
    ['File', rep.exists ? 'On this server — open, download or delete it above' : 'No longer on disk', false],
  ]) {
    dl.appendChild(el('dt', { text: k }));
    dl.appendChild(el('dd', { class: numeric ? 'numeric' : '', text: v }));
  }
  factsTile.appendChild(el('div', 'kv-wrap', dl));
  facts.body.appendChild(factsTile);

  const preview = section({ title: isHtml ? 'Preview' : 'Contents', layout: 'stack', id: 'report-preview' });
  const holder = tile({ emoji: '📄', title: names.title, subtitle: isHtml ? 'Rendered in a sandbox — open it in a new tab to print or save as PDF.' : 'Plain text', className: 'tile--block' });
  preview.body.appendChild(holder);
  const view = grid(facts, preview);
  root.replaceChildren(view);

  if (!rep.exists) {
    holder.appendChild(emptyState('📄', 'The file for this report is no longer on disk.'));
  } else if (isHtml) {
    const frame = el('iframe', { class: 'report-frame', src: url, title: `Report: ${rep.title}`, sandbox: '', loading: 'lazy' });
    holder.appendChild(frame);
  } else {
    const pre = el('pre', { class: 'report-pre', text: 'Loading…' });
    holder.appendChild(pre);
    fetch(url, { credentials: 'same-origin', signal: ctx.signal })
      .then((r) => (r.ok ? r.text() : Promise.reject(new Error(`${r.status} ${r.statusText}`))))
      .then((text) => {
        if (rep.fmt === 'json') {
          try { text = JSON.stringify(JSON.parse(text), null, 2); } catch { /* keep as served */ }
        }
        pre.textContent = text;
      })
      .catch((err) => { if (!(err && err.name === 'AbortError')) pre.textContent = `Could not load the file — ${err.message}`; });
  }

  async function remove() {
    del.setBusy(true);
    try {
      await api.deleteReport(rep.id);
    } catch (err) {
      del.setBusy(false);
      snackbar(`Could not delete the report — ${err.message}`, { duration: 8000 });
      return;
    }
    ctx.navigate('#/history', { replace: true });
    snackbar.undo('Report deleted', async () => {
      try {
        await api.restoreReport(rep.id);
      } catch (err) {
        snackbar(`Could not undo — ${err.message}`);
        return null;
      }
      // History re-adds the tile from the "restored" stream event; focus returns to it
      return whenPresent(`#history-reports [data-key="report-${rep.id}"] [data-action="details"]`);
    });
  }

  return () => {};
}
