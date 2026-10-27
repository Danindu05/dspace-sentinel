/**
 * panels/report.js — the "＋ New report" right-side panel (SPEC §12.5, §12.6).
 *
 * Two clicks to a report: the range is preselected, "Generate report" does the rest.
 * Biggest field first: range chips (Last 24 hours ✓ · 7 days · 30 days · Custom → two date inputs),
 * format chips (HTML ✓ · Markdown · JSON), an optional title, then "More options" hiding the
 * section checkboxes, "Send to Telegram" and "Send by email" (each only while that channel can
 * deliver). One full-width filled button in the panel foot reads "Generating…" while busy; on
 * success the panel closes and a snackbar says "Report ready" — naming each delivery ("Report ready ·
 * sent to Telegram and email") — with an Open action (the HTML report opens in a new tab).
 *
 * `root` is the panel body, `ctx.panel` the ui.panel handle. Options come from
 * GET /api/reports/options; the form is usable before they arrive.
 */
import api from '../api.js';
import { store } from '../store.js';
import { button, chipRow, disclosure, el, field, input, snackbar, toggle } from '../ui.js';

export const title = 'Generate report';

const RANGE_OPTIONS = [
  { value: '24h', label: 'Last 24 hours' },
  { value: '7d', label: '7 days' },
  { value: '30d', label: '30 days' },
  { value: 'custom', label: 'Custom' },
];
const FORMAT_OPTIONS = [
  { value: 'html', label: 'HTML' },
  { value: 'md', label: 'Markdown' },
  { value: 'json', label: 'JSON' },
];
const FORMAT_HELP = {
  html: 'A self-contained page with charts. Opens in a new tab and prints to PDF.',
  md: 'Plain Markdown for wikis and tickets.',
  json: 'The raw numbers for your own tooling.',
};
const PRESET_RANGES = new Set(['24h', '7d', '30d']);
const DAY_S = 86400;
/** Report sections that belong to modules; a section shows only while one of them is on (others always show). */
export const SECTION_MODULES = {
  latency: ['backend', 'frontend', 'postgres', 'solr'],
  incidents: ['incidents'],
  repository: ['repository', 'assetstore'],
  logs: ['logs'],
  security: ['security'],
};

/** True when a report section can be offered: its module is on, or it is not tied to one. */
export function sectionOn(id) {
  const mods = SECTION_MODULES[id];
  return !mods || mods.some((m) => store.moduleEnabled(m));
}

/** Which preset the panel starts on: an explicit `range` param, else the History chip when it is a report range. */
function initialRange(params) {
  if (params && PRESET_RANGES.has(params.range)) return params.range;
  const current = store.get('range');
  return PRESET_RANGES.has(current) ? current : '24h';
}

/** `YYYY-MM-DD` in local time for a date input. */
function isoDay(ts) {
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** Local midnight (start) or the following midnight (end) of a `YYYY-MM-DD` value, in epoch seconds. */
function dayBound(value, end = false) {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  const [y, m, d] = value.split('-').map(Number);
  const date = new Date(y, m - 1, d + (end ? 1 : 0), 0, 0, 0, 0);
  return Number.isNaN(date.getTime()) ? null : date.getTime() / 1000;
}

/** A labelled group for a control that is not an <input> (chip rows). */
function chipField(labelText, chips, help) {
  const id = `rf-${labelText.toLowerCase().replace(/\W+/g, '-')}`;
  chips.setAttribute('aria-labelledby', id);
  const node = el('div', 'field', el('span', { class: 'field-label', id, text: labelText }), chips);
  if (help) node.appendChild(el('p', { class: 'field-help', text: help }));
  return node;
}

/**
 * Render the report panel.
 * @param {HTMLElement} root the panel body
 * @param {object} params route params (`range` preselects a preset)
 * @param {object} ctx screen context (uses ctx.panel, ctx.signal, ctx.navigate)
 * @returns {() => void} destroy
 */
export function render(root, params, ctx) {
  const state = { range: initialRange(params), fmt: 'html', busy: false, telegram: false, email: false, sections: null };
  const now = Date.now() / 1000;

  const form = el('form', { class: 'form panel-form', id: 'report-form', novalidate: 'novalidate' });

  // Range
  const fromInput = input({ type: 'date', id: 'report-from', value: isoDay(now - 7 * DAY_S), max: isoDay(now) });
  const toInput = input({ type: 'date', id: 'report-to', value: isoDay(now), max: isoDay(now) });
  const fromField = field({ label: 'From', input: fromInput });
  const toField = field({ label: 'To', input: toInput });
  const customRow = el('div', 'pair', fromField, toField);
  customRow.hidden = state.range !== 'custom';
  const rangeChips = chipRow({
    options: RANGE_OPTIONS, value: state.range, ariaLabel: 'Range', name: 'range',
    onChange: (value) => {
      state.range = value;
      customRow.hidden = value !== 'custom';
      if (value === 'custom') fromInput.focus();
      clearError();
    },
  });

  // Format
  const formatHelp = el('p', { class: 'field-help', text: FORMAT_HELP.html });
  const formatChips = chipRow({
    options: FORMAT_OPTIONS, value: state.fmt, ariaLabel: 'Format', name: 'format',
    onChange: (value) => { state.fmt = value; formatHelp.textContent = FORMAT_HELP[value] || ''; },
  });
  const formatField = chipField('Format', formatChips);
  formatField.appendChild(formatHelp);

  // Title
  const titleInput = input({ type: 'text', id: 'report-title', placeholder: 'Weekly health report', autocomplete: 'off' });
  titleInput.maxLength = 200;
  const titleField = field({ label: 'Title (optional)', input: titleInput, help: 'Leave empty for "DSpace Sentinel report · <host> · <range>"; the History list then shows the range.' });

  // More options
  const sectionsList = el('div', { class: 'check-list', role: 'group', 'aria-label': 'Sections' });
  sectionsList.appendChild(el('p', { class: 'form-note', text: 'Loading sections…' }));
  const sectionsWrap = el('div', 'field', el('span', { class: 'field-label', text: 'Sections' }), sectionsList);
  const deliverySlot = el('div', 'tile-list');
  const more = disclosure({ summary: 'More options', children: [sectionsWrap, deliverySlot] });

  const errorEl = el('p', { class: 'field-error', role: 'alert' });
  errorEl.hidden = true;

  form.append(chipField('Range', rangeChips), customRow, formatField, titleField, more, errorEl);
  root.replaceChildren(form);

  const generate = button({ label: 'Generate report', variant: 'filled', full: true, type: 'submit', busyLabel: 'Generating…' });
  generate.setAttribute('form', 'report-form');
  ctx.panel.foot.replaceChildren(generate);

  function clearError() {
    errorEl.hidden = true;
    errorEl.textContent = '';
  }

  function showError(text) {
    errorEl.textContent = text;
    errorEl.hidden = false;
  }

  function paintSections(options) {
    const defaults = new Set(options.default_sections || []);
    // sections of modules that are off are not offered (and never sent)
    const offered = (options.sections || []).filter((sec) => sectionOn(sec.id));
    sectionsList.replaceChildren();
    for (const sec of offered) {
      const box = el('input', { type: 'checkbox', name: 'sections', value: sec.id });
      box.checked = defaults.size ? defaults.has(sec.id) : true;
      const row = el('label', 'check', box, el('span', { class: 'check-emoji', 'aria-hidden': 'true', text: sec.emoji || '' }), el('span', { text: sec.label }));
      sectionsList.appendChild(row);
    }
    state.sections = offered.map((s) => s.id);
  }

  /** "Send to Telegram" / "Send by email", each only while that channel can deliver. */
  function paintDelivery(options) {
    deliverySlot.replaceChildren();
    if (options.telegram_available) {
      deliverySlot.appendChild(toggle({ emoji: '📨', label: 'Send to Telegram', description: 'Deliver the file to every Telegram bot as soon as it is ready.', name: 'send_telegram', checked: false, onChange: (on) => { state.telegram = on; } }));
    }
    if (options.email_available) {
      deliverySlot.appendChild(toggle({ emoji: '✉️', label: 'Send by email', description: 'Email the file to every recipient as an attachment.', name: 'send_email', checked: false, onChange: (on) => { state.email = on; } }));
    }
    if (deliverySlot.childNodes.length) return;
    deliverySlot.appendChild(el('p', { class: 'form-note' }, '📨 Telegram and email are off — set one up in ',
      el('a', { href: '#/more/telegram', text: 'More › Telegram' }), ' or ', el('a', { href: '#/more/email', text: 'More › Email' }), ' to get reports delivered.'));
  }

  api.get('api/reports/options', null, { signal: ctx.signal })
    .then((options) => {
      if (ctx.signal.aborted) return;
      paintSections(options);
      paintDelivery(options);
    })
    .catch((err) => {
      if (ctx.signal.aborted || (err && err.name === 'AbortError')) return;
      sectionsList.replaceChildren(el('p', { class: 'form-note', text: 'Sections use the defaults (options could not be loaded).' }));
      state.sections = null;
    });

  /** Validate the form and build the POST body, or return null after showing the error. */
  function buildBody() {
    const body = { fmt: state.fmt };
    if (state.range === 'custom') {
      const from = dayBound(fromInput.value);
      const to = dayBound(toInput.value, true);
      fromField.setError('');
      toField.setError('');
      if (from === null) { fromField.setError('Pick a start date.'); fromInput.focus(); return null; }
      if (to === null) { toField.setError('Pick an end date.'); toInput.focus(); return null; }
      if (to <= from) { toField.setError('The end date must be after the start date.'); toInput.focus(); return null; }
      if (from > Date.now() / 1000) { fromField.setError('The start date is in the future.'); fromInput.focus(); return null; }
      body.range = { from, to: Math.min(to, Date.now() / 1000) };
    } else {
      body.range = state.range;
    }
    const titleText = titleInput.value.trim();
    if (titleText) body.title = titleText;
    if (state.sections) {
      const picked = Array.from(sectionsList.querySelectorAll('input[name="sections"]:checked')).map((i) => i.value);
      if (!picked.length) {
        more.open = true;
        showError('Pick at least one section.');
        return null;
      }
      // always explicit: "every box ticked" means every offered section, not the server's defaults
      body.sections = picked;
    }
    if (state.telegram) body.send_telegram = true;
    if (state.email) body.send_email = true;
    return body;
  }

  /** "Report ready" plus what happened to each delivery: "Report ready · sent to Telegram and email". */
  function readyMessage(report) {
    const outcomes = [['telegram', report.telegram], ['email', report.email]].filter(([, r]) => r);
    if (!outcomes.length) return 'Report ready';
    if (outcomes.every(([, r]) => r.skipped === 'demo')) return 'Report ready · Demo: logged, not sent';
    const parts = [];
    const sent = outcomes.filter(([, r]) => r.ok).map(([channel]) => channel);
    if (sent.length === 2) parts.push('sent to Telegram and email');
    else if (sent[0] === 'telegram') parts.push('sent to Telegram');
    else if (sent[0] === 'email') parts.push('sent by email');
    for (const [channel, r] of outcomes) {
      if (r.ok) continue;
      const where = channel === 'email' ? 'by email' : 'to Telegram';
      parts.push(r.skipped === 'demo' ? `Demo: logged, not sent ${where}` : `not sent ${where}: ${r.error || 'unknown error'}`);
    }
    return `Report ready · ${parts.join(' · ')}`;
  }

  async function submit(event) {
    event.preventDefault();
    if (state.busy) return;
    clearError();
    const body = buildBody();
    if (!body) return;
    state.busy = true;
    generate.setBusy(true);
    ctx.panel.setBusy(true);
    try {
      const report = await api.createReport(body, { signal: ctx.signal });
      const url = String(api.reportUrl(report.id));
      const isHtml = report.fmt === 'html';
      ctx.panel.close('done');
      snackbar(readyMessage(report), {
        duration: 8000,
        action: {
          label: isHtml ? 'Open' : 'Download',
          onClick: () => window.open(isHtml ? url : `${url}?download=true`, '_blank', 'noopener'),
        },
      });
    } catch (err) {
      if (err && err.name === 'AbortError') return;
      state.busy = false;
      generate.setBusy(false);
      ctx.panel.setBusy(false);
      showError(`Could not generate the report — ${err.message}`);
    }
  }

  form.addEventListener('submit', submit);
  requestAnimationFrame(() => {
    const first = rangeChips.querySelector('.chip[aria-checked="true"]') || rangeChips.querySelector('.chip');
    if (first && !ctx.signal.aborted) first.focus({ preventScroll: true });
  });

  return () => {
    form.removeEventListener('submit', submit);
  };
}
