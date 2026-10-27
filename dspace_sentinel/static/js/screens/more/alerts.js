/**
 * screens/more/alerts.js — #/more/alerts: alert thresholds (SPEC §12.5, §12.6).
 *
 * One block per rule with a Warning / Critical pair of numeric inputs (`inputmode` set), grouped
 * Server · Response times · Services · Behaviour; the blocks of a group flow in columns and the Save
 * row sticks to the bottom of the window. Rules of modules that are off are not shown. Then the time
 * zone of the times Sentinel sends (server.timezone: alert messages, emails, reports, quiet hours, the
 * daily digest). It sits here, with how alerts behave, rather than in Appearance: it changes what
 * Sentinel sends and when it stays quiet, never how this dashboard shows times (the browser's zone).
 * Saves via PATCH /api/config with a "Saved" snackbar; errors sit below the field that caused them.
 */
import { store } from '../../store.js';
import { el } from '../../ui.js';
import {
  errorState, formEl, group, loadConfig, numberField, plainError, readNumber, saveButton, submitPatch, textField,
} from './form.js';

export const title = 'Alert thresholds';

const GROUPS = [
  {
    title: 'Server',
    sub: 'Warning at or above the first value, critical at or above the second.',
    items: [
      { key: 'cpu', label: 'CPU', unit: '%', min: 1, max: 100 },
      { key: 'memory', label: 'Memory', unit: '%', min: 1, max: 100 },
      { key: 'swap', label: 'Swap', unit: '%', min: 1, max: 100 },
      { key: 'disk', label: 'Disk', unit: '%', min: 1, max: 100, help: 'Applies to the root filesystem and every extra mount.' },
      { key: 'load_per_core', label: 'Load per core', unit: 'load ÷ cores', step: 0.1, min: 0.1, max: 64 },
    ],
  },
  {
    title: 'Response times',
    sub: 'Milliseconds, averaged per check.',
    items: [
      { key: 'rest_latency_ms', label: 'REST API', unit: 'ms', module: ['backend'], step: 50, min: 50 },
      { key: 'search_latency_ms', label: 'Search', unit: 'ms', module: ['backend', 'solr'], step: 50, min: 50 },
      { key: 'home_latency_ms', label: 'Homepage', unit: 'ms', module: ['frontend'], step: 50, min: 50 },
    ],
  },
  {
    title: 'Services',
    items: [
      { key: 'postgres_connections_pct', label: 'PostgreSQL connections', unit: '% of max_connections', module: ['postgres'], min: 1, max: 100 },
      { key: 'ssl_expiry_days', label: 'Certificate expiry', unit: 'days left', module: ['ssl'], inverted: true, min: 1, max: 365 },
      { key: 'log_errors_per_window', label: 'Log errors', unit: 'per window', module: ['logs'], min: 1 },
      { key: 'assetstore_free_gb', label: 'Assetstore free space', unit: 'GB', module: ['assetstore'], inverted: true, min: 0 },
    ],
  },
];

const BEHAVIOUR = [
  { key: 'confirm_samples', label: 'Breaches before an alert opens', unit: 'consecutive checks', min: 1, max: 100, help: 'Keeps a single spike from paging you.' },
  { key: 'clear_samples', label: 'Healthy checks before it resolves', unit: 'consecutive checks', min: 1, max: 100 },
  { key: 'reminder_minutes', label: 'Remind about open, unacknowledged alerts', unit: 'minutes', min: 0, max: 10080, help: '0 never reminds; opened, escalated and resolved are always sent.' },
];

/** The time zone field's help: what to type, and what empty means on this server ("(UTC)" from /api/meta). */
function zoneHelp(meta) {
  const tz = (meta && meta.timezone) || {};
  const own = tz.server_abbreviation || tz.server_name || '';
  return `IANA name such as Asia/Colombo; empty = the server's zone${own ? ` (${own})` : ''}.`;
}

function moduleOn(item) {
  if (!item.module) return true;
  return item.module.some((id) => store.moduleEnabled(id));
}

function thresholdBlock(item, current, windowMinutes) {
  const unit = item.key === 'log_errors_per_window' ? `per ${windowMinutes} min` : item.unit;
  const warning = numberField({ label: 'Warning', value: current.warning, id: `th-${item.key}-warning`, step: item.step || 1, min: item.min, max: item.max });
  const critical = numberField({ label: 'Critical', value: current.critical, id: `th-${item.key}-critical`, step: item.step || 1, min: item.min, max: item.max });
  const help = item.inverted ? 'Lower is worse: warning below the first value, critical below the second.' : item.help || '';
  const node = el('div', { class: 'threshold', role: 'group', 'aria-label': `${item.label} thresholds` },
    el('div', 'threshold-head', el('span', { class: 'threshold-label', text: item.label }), unit ? el('span', { class: 'threshold-help', text: unit }) : null),
    help ? el('p', { class: 'threshold-help', text: help }) : null,
    el('div', 'pair', warning.node, critical.node),
  );
  return { item, node, warning, critical };
}

/**
 * Render the thresholds form.
 * @returns {() => void} destroy
 */
export async function render(root, params, ctx) {
  let cfg;
  try {
    cfg = await loadConfig(ctx);
  } catch (err) {
    errorState(root, err);
    return () => {};
  }
  if (!cfg) return () => {};
  const alerts = cfg.alerts || {};
  const windowMinutes = (cfg.logs && cfg.logs.window_minutes) || 5;

  const form = formEl({ id: 'alerts-form' });
  const blocks = [];
  for (const g of GROUPS) {
    const items = g.items.filter(moduleOn);
    if (!items.length) continue;
    const nodes = items.map((item) => {
      const block = thresholdBlock(item, alerts[item.key] || {}, windowMinutes);
      blocks.push(block);
      return block.node;
    });
    const block = group(g.title, g.sub, ...nodes);
    block.classList.add('form-group--grid'); // rule cards in columns: the page fits about one screen at 1440
    form.appendChild(block);
  }
  const behaviour = BEHAVIOUR.map((item) => ({ item, field: numberField({ label: item.label, value: alerts[item.key], id: `th-${item.key}`, unit: item.unit, min: item.min, max: item.max, help: item.help }) }));
  const behaviourGroup = group('Behaviour', 'How patient Sentinel is before it speaks up.', ...behaviour.map((b) => b.field.node));
  behaviourGroup.classList.add('form-group--grid');
  form.appendChild(behaviourGroup);

  // sent only when it changes: thresholds still save on a server that predates the setting
  let savedZone = String((cfg.server && cfg.server.timezone) || '');
  const zone = textField({ label: 'Time zone', id: 'th-timezone', value: savedZone, placeholder: 'Asia/Colombo', help: zoneHelp(store.get('meta')) });
  form.appendChild(group('Times in messages', 'Alert messages, emails, reports, quiet hours and the daily digest use this zone. The dashboard shows your browser\'s time.', zone.node));

  const save = saveButton();
  form.appendChild(el('div', 'form-actions', save));
  root.replaceChildren(form);

  function validate() {
    let first = null;
    const patch = {};
    for (const b of blocks) {
      b.warning.node.setError('');
      b.critical.node.setError('');
      const w = readNumber(b.warning.input);
      const c = readNumber(b.critical.input);
      let bad = null;
      if (w === null || Number.isNaN(w)) bad = [b.warning, 'Enter a number.'];
      else if (c === null || Number.isNaN(c)) bad = [b.critical, 'Enter a number.'];
      else if (!b.item.inverted && !(c > w)) bad = [b.critical, 'Critical must be higher than warning.'];
      else if (b.item.inverted && !(c < w)) bad = [b.critical, 'Critical must be lower than warning.'];
      if (bad) {
        bad[0].node.setError(bad[1]);
        first = first || bad[0].input;
        continue;
      }
      patch[b.item.key] = { warning: w, critical: c };
    }
    for (const b of behaviour) {
      b.field.node.setError('');
      const v = readNumber(b.field.input);
      if (v === null || Number.isNaN(v) || v < b.item.min || (b.item.max !== undefined && v > b.item.max) || v % 1 !== 0) {
        b.field.node.setError(`Enter a whole number between ${b.item.min} and ${b.item.max}.`);
        first = first || b.field.input;
        continue;
      }
      patch[b.item.key] = v;
    }
    if (first) {
      first.focus();
      return null;
    }
    return patch;
  }

  /** A zone the server refuses ("unknown time zone 'Mars/Base': use an IANA name …") goes under its field. */
  function zoneError(err) {
    if (!err || err.status !== 400 || !/timezone|time zone/i.test(err.message || '')) return false;
    zone.node.setError(plainError(err.message));
    zone.input.focus();
    return true;
  }

  async function submit(event) {
    event.preventDefault();
    zone.node.setError('');
    const patch = validate();
    if (!patch) return;
    const timezone = zone.input.value.trim();
    const body = { alerts: patch };
    if (timezone !== savedZone) body.server = { timezone };
    const saved = await submitPatch({ patch: body, button: save, label: 'Saved — new thresholds apply from the next check.', signal: ctx.signal, onError: zoneError });
    if (!saved) return;
    cfg = saved;
    if (body.server) {
      savedZone = String((saved.server && saved.server.timezone) ?? timezone);
      // the quiet-hours and digest fields of Telegram and Email name the zone from /api/meta
      await ctx.refreshMeta();
    }
  }
  form.addEventListener('submit', submit);
  return () => form.removeEventListener('submit', submit);
}
