/**
 * screens/more/destinations.js — the 1-5 destinations of a notifier: More › Telegram (one bot per
 * person) and More › Email (recipients) share it.
 *
 * Every destination is a card: its fields side by side with visible labels, then the "All alerts" /
 * "Critical only" chips, a status line for the last test ("✅ Alex · @alex_sentinel_bot · delivered"),
 * "Test" and "✕". The list always shows at least one editable card; "＋ Add …" stops at `max`, and
 * read-only destinations (a Telegram bot set in the environment) count toward it. Removing a card is
 * instant with Undo in the snackbar; nothing is written until the form's "Save changes".
 *
 *   const list = destinationList({noun, nounPlural, fields, items, fixed, max, addLabel, caption,
 *                                 nameOf, describeFixed, onTest, onChange})
 *   list.el                   the cards and the "＋ Add …" row with its caption
 *   list.rows()               the editable rows in order · list.allRows() read-only rows first, then those
 *   list.prune()              drop empty cards (one always stays) — after a save
 *   list.clearStatus()        empty every status line — before a new test
 *   row.id                    the server id (null until saved) · row.fixed (read-only) · row.entry (read-only data)
 *   row.inputs[key]           the <input> of a field · row.values() {field: trimmed text, min_severity}
 *   row.setValue(key, value)  set a field from code (a saved token's "Saved. Hidden for safety" line follows)
 *   row.isEmpty() · row.setError(key, text) · row.clearErrors() · row.focus(key) · row.name()
 *   row.setStatus(text, tone) tone 'ok' | 'crit' | 'muted'; '' hides the line · row.test (its Test button)
 *
 * `fields`: [{key, label, type, placeholder, inputmode, maxLength, resetsStatus}] — `resetsStatus`
 * clears the card's last test result when that field is edited (a new token is a new bot).
 */
import { button, chipRow, el, field, input, snackbar, tile } from '../../ui.js';
import { maskAsOne } from './form.js';

/** The per-destination severity chips: a "critical" destination only hears about critical events. */
export const SEVERITY_OPTIONS = [
  { value: 'warning', label: 'All alerts' },
  { value: 'critical', label: 'Critical only' },
];
const SEVERITY_LABEL = { warning: 'All alerts', critical: 'Critical only' };

/** "the bot" / "both bots" / "all 3 bots" — who a test reached. */
export function everyone(count, noun) {
  if (count === 1) return `the ${noun}`;
  return count === 2 ? `both ${noun}s` : `all ${count}\u00a0${noun}s`;
}

let seq = 0;

/**
 * Build the destination list.
 * @returns {{el: HTMLElement, rows(): object[], allRows(): object[], prune(): void, clearStatus(): void}}
 */
export function destinationList({
  noun, nounPlural, fields, items = [], fixed = [], max = 5, addLabel, caption,
  nameOf = () => '', describeFixed, onTest, onChange = () => {},
} = {}) {
  const Noun = noun.charAt(0).toUpperCase() + noun.slice(1);
  const listEl = el('div', 'dest-list');
  const captionEl = el('p', { class: 'form-note', id: `dest-caption-${++seq}`, text: caption });
  // the ＋ is decoration (aria-hidden), so the button is announced as "Add a bot"
  const addBtn = button({ label: addLabel.replace(/^＋\s*/, ''), emoji: /^＋/.test(addLabel) ? '＋' : undefined, variant: 'ghost', onClick: add });
  addBtn.setAttribute('aria-describedby', captionEl.id);
  const wrap = el('div', 'dest-wrap', listEl, el('div', 'dest-add', addBtn, captionEl));
  /** Editable rows in order (read-only rows sit above them and never move). */
  const rows = [];
  const fixedRows = fixed.map(makeFixed);
  for (const row of fixedRows) listEl.appendChild(row.el);
  for (const item of items) insert(makeRow(item), rows.length);
  if (!rows.length) insert(makeRow({}), 0);
  paint();

  function total() {
    return fixedRows.length + rows.length;
  }

  function setStatus(row, text, tone = 'muted') {
    row.statusEl.textContent = text || '';
    row.statusEl.className = `dest-status${tone ? ` dest-status--${tone}` : ''}`;
    row.statusEl.hidden = !text;
  }

  function makeRow(entry) {
    const key = `dest-${++seq}`;
    const row = { key, id: entry.id || null, fixed: false, inputs: {}, nodes: {}, repaint: {}, n: 0 };
    const fieldNodes = fields.map((f) => {
      const control = input({ type: f.type || 'text', id: `${key}-${f.key}`, value: entry[f.key] ?? '', placeholder: f.placeholder, inputmode: f.inputmode, autocomplete: 'off' });
      if (f.maxLength) control.maxLength = f.maxLength;
      control.spellcheck = false;
      control.addEventListener('input', () => {
        row.nodes[f.key].setError('');
        if (f.resetsStatus) setStatus(row, '');
        paint();
        onChange();
      });
      const node = field({ label: f.label, input: control });
      // a saved token is the mask, one unit, with "Saved. Hidden for safety — …" under it
      if (f.type === 'password') row.repaint[f.key] = maskAsOne(control, node);
      row.inputs[f.key] = control;
      row.nodes[f.key] = node;
      return node;
    });
    row.chips = chipRow({ options: SEVERITY_OPTIONS, value: entry.min_severity === 'critical' ? 'critical' : 'warning', name: 'min_severity', ariaLabel: 'Alerts' });
    row.statusEl = el('p', { class: 'dest-status' });
    row.statusEl.hidden = true;
    row.test = button({ label: 'Test', variant: 'ghost', busyLabel: 'Testing…', onClick: () => onTest(row) });
    row.test.dataset.action = 'test';
    row.removeBtn = button({ label: '✕', variant: 'ghost', className: 'btn--icon', title: 'Remove', onClick: () => remove(row) });
    row.removeBtn.dataset.action = 'remove';
    row.el = el('div', { class: 'dest', role: 'group', dataset: { key } },
      el('div', `dest-fields dest-fields--${noun}`, fieldNodes),
      el('div', 'dest-foot', row.chips, row.statusEl, el('div', 'dest-actions', row.test, row.removeBtn)),
    );
    row.values = () => {
      const out = {};
      for (const f of fields) out[f.key] = row.inputs[f.key].value.trim();
      out.min_severity = row.chips.value;
      return out;
    };
    row.isEmpty = () => fields.every((f) => !row.inputs[f.key].value.trim());
    row.setValue = (k, value) => {
      row.inputs[k].value = value;
      if (row.repaint[k]) row.repaint[k]();
    };
    row.setError = (k, text) => row.nodes[k].setError(text);
    row.clearErrors = () => fields.forEach((f) => row.nodes[f.key].setError(''));
    row.focus = (k) => row.inputs[k || fields[0].key].focus();
    row.name = () => nameOf(row.values()) || `${noun} ${row.n}`;
    row.setStatus = (text, tone) => setStatus(row, text, tone);
    return row;
  }

  /** A destination the form cannot change (set in the environment): the tile recipe, a Test button, a status line. */
  function makeFixed(entry) {
    const key = `dest-${++seq}`;
    const d = describeFixed(entry);
    const row = { key, id: entry.id, fixed: true, entry };
    row.statusEl = el('p', { class: 'dest-status' });
    row.statusEl.hidden = true;
    row.test = button({ label: 'Test', variant: 'ghost', busyLabel: 'Testing…', ariaLabel: `Test ${d.title}`, onClick: () => onTest(row) });
    row.test.dataset.action = 'test';
    const severity = el('span', { class: 'pill', text: SEVERITY_LABEL[entry.min_severity] || SEVERITY_LABEL.warning });
    row.el = tile({ emoji: d.emoji, title: d.title, subtitle: d.subtitle, pill: severity, actions: row.test, className: 'dest-fixed', dataset: { key }, children: row.statusEl });
    row.el.setAttribute('role', 'group');
    row.el.setAttribute('aria-label', d.title);
    row.values = () => ({ ...entry });
    row.isEmpty = () => false;
    row.name = () => d.title;
    row.setStatus = (text, tone) => setStatus(row, text, tone);
    return row;
  }

  function insert(row, index) {
    const before = rows[index] ? rows[index].el : null;
    listEl.insertBefore(row.el, before);
    rows.splice(index, 0, row);
  }

  function detach(row) {
    const index = rows.indexOf(row);
    if (index >= 0) rows.splice(index, 1);
    row.el.remove();
    return index;
  }

  /** Numbering, accessible names, the one-empty-card rule and the Add limit. */
  function paint() {
    rows.forEach((row, i) => {
      row.n = i + 1; // the editable cards count from 1; a read-only card is named by its chat
      row.el.setAttribute('aria-label', `${Noun} ${row.n}`);
      const name = row.name();
      row.test.setAttribute('aria-label', `Test ${name}`);
      row.removeBtn.setAttribute('aria-label', `Remove ${name}`);
      // the last card only goes away once it holds something (an empty one is the place to start typing)
      row.removeBtn.disabled = rows.length === 1 && row.isEmpty();
    });
    addBtn.disabled = total() >= max;
  }

  function add() {
    if (total() >= max) return;
    const row = makeRow({});
    insert(row, rows.length);
    paint();
    onChange();
    row.focus();
  }

  function remove(row) {
    const hadFocus = row.el.contains(document.activeElement);
    const empty = row.isEmpty();
    const name = row.name();
    const index = detach(row);
    if (index < 0) return;
    // one card always stays: removing the last one leaves a fresh, empty card in its place
    const placeholder = rows.length ? null : makeRow({});
    if (placeholder) insert(placeholder, 0);
    paint();
    onChange();
    if (hadFocus) {
      // focus stays on a real control: the next card's ✕, else the previous card's, else the empty card
      const next = rows[index] || rows[index - 1];
      if (next && !next.removeBtn.disabled) next.removeBtn.focus({ preventScroll: true });
      else if (next) next.focus();
      else addBtn.focus({ preventScroll: true });
    }
    if (!empty) snackbar.undo(`Removed ${name} — Save changes to apply.`, () => restore(row, index, placeholder));
  }

  /** Undo: the card comes back where it was (with its id, values and severity); focus returns to its ✕. */
  function restore(row, index, placeholder) {
    if (row.el.isConnected) return row.removeBtn;
    if (placeholder && rows.includes(placeholder) && placeholder.isEmpty()) detach(placeholder);
    if (total() >= max) {
      snackbar(`No room to put ${row.name()} back — up to ${max} ${nounPlural}.`);
      return null;
    }
    insert(row, Math.min(index, rows.length));
    paint();
    onChange();
    return row.removeBtn;
  }

  return {
    el: wrap,
    rows: () => rows.slice(),
    allRows: () => [...fixedRows, ...rows],
    prune() {
      for (const row of rows.slice()) if (rows.length > 1 && row.isEmpty()) detach(row);
      paint();
      onChange();
    },
    clearStatus() {
      for (const row of [...fixedRows, ...rows]) setStatus(row, '');
    },
  };
}
