/**
 * screens/more/form.js — shared helpers for the More sub-screens (single-column settings forms).
 *
 *   formEl({id, wide})                   <form class="form" novalidate> (Enter submits)
 *   group(title, sub, ...children)       a titled block of fields
 *   textField({label, value, …})         {node, input} — text-like input with visible label + help
 *   numberField({label, value, unit, …}) {node, input} — numeric input (inputmode decimal/numeric)
 *   chipField(label, chips, help)        a visible label over a ui.chipRow (+ help) with .setHelp(text)
 *   relabel(node, label, help)           change a field's visible label and help text in place
 *   maskAsOne(input, node?)              a saved secret's mask is one unit: typing, pasting or deleting replaces it all;
 *                                        with the field's node, "Saved. Hidden for safety — …" under it (→ repaint fn)
 *   readNumber(input)                    number | null (empty) | NaN (invalid)
 *   saveButton(label)                    filled "Save changes" button reading "Saving…" while busy
 *   loadConfig(ctx)                      GET /api/config (null once the screen is gone)
 *   submitPatch({patch, button, label})  PATCH /api/config with busy state and a "Saved" snackbar
 *   errorState(root, err)                replaces the screen with a one-sentence empty state
 *   plainError(message)                  a 400 from PATCH /api/config as one sentence ("Not a Telegram chat id: x.")
 *   QUIET_RE                             quiet hours "HH:MM-HH:MM"
 *   quietHoursError(value)               '' or the sentence under the field (not HH:MM-HH:MM, or two equal times)
 *   weekdayName(n) · hourLabel(h)        "Monday" for 1 … 7 (0 is Sunday too) · "08:00"
 *   zoneWords(meta) · inZone(meta)       the zone of the times Sentinel sends: "Asia/Colombo (+0530)" ·
 *                                        ", in Asia/Colombo (+0530)" for a help sentence ('' when the server does not say)
 *   MASK                                 the server's secret mask ("••••••"); sending it keeps the old value
 */
import api from '../../api.js';
import { button, el, emptyState, field, input, snackbar } from '../../ui.js';

export const MASK = '••••••';
/** Quiet hours as the notifiers read them: "23:00-07:00". */
export const QUIET_RE = /^([01]?\d|2[0-3]):[0-5]\d-([01]?\d|2[0-3]):[0-5]\d$/;
/** The server's sentence for a window whose two times are the same (config.QUIET_HOURS_EQUAL_ERROR). */
export const QUIET_EQUAL_ERROR = 'Quiet hours must start and end at different times.';

/**
 * What is wrong with quiet hours as they will be sent ("23:00-07:00"; '' when empty or fine): not
 * HH:MM-HH:MM, or two times that are the same ("23:00-23:00", "7:00-07:00"), which the server
 * refuses: no minute lies inside such a window, so it would never be quiet.
 */
export function quietHoursError(value) {
  if (!value) return '';
  if (!QUIET_RE.test(value)) return 'Use HH:MM-HH:MM, for example 23:00-07:00.';
  const [start, end] = value.split('-').map((time) => {
    const [hours, minutes] = time.split(':').map(Number);
    return hours * 60 + minutes;
  });
  return start === end ? QUIET_EQUAL_ERROR : '';
}

/** A settings form; Enter submits, native validation bubbles are off (errors render below fields). */
export function formEl({ id, wide = false } = {}) {
  return el('form', { class: `form${wide ? ' form--wide' : ''}`, id, novalidate: 'novalidate' });
}

/** A titled group of fields (an h2: the next level under the page's h1). */
export function group(title, sub, ...children) {
  const node = el('div', 'form-group');
  if (title) node.appendChild(el('h2', { class: 'form-group-title', text: title }));
  if (sub) node.appendChild(el('p', { class: 'form-group-sub', text: sub }));
  for (const child of children) if (child) node.appendChild(child);
  return node;
}

/** A labelled text-like input. */
export function textField({ label, value = '', placeholder, help, id, type = 'text', inputmode, autocomplete = 'off', required = false, disabled = false, onInput } = {}) {
  const control = input({ type, id, value, placeholder, inputmode, autocomplete, required, disabled, onInput });
  const node = field({ label, input: control, help, id });
  return { node, input: control };
}

/** A labelled numeric input (`inputmode` decimal when `step` is fractional). */
export function numberField({ label, value, help, id, step = 1, min, max, unit, disabled = false } = {}) {
  const fractional = Number(step) % 1 !== 0;
  const control = input({ type: 'number', id, value: value === null || value === undefined ? '' : value, step, min, max, inputmode: fractional ? 'decimal' : 'numeric', disabled });
  const node = field({ label: unit ? `${label} (${unit})` : label, input: control, help, id });
  return { node, input: control };
}

/** The line under a saved secret while its field shows the mask: the dots are on purpose. */
export const SAVED_SECRET_NOTE = 'Saved. Hidden for safety — type a new one to replace it.';

/** Add or remove one id in a control's aria-describedby. */
function describedBy(control, id, on) {
  const ids = (control.getAttribute('aria-describedby') || '').split(/\s+/).filter((x) => x && x !== id);
  if (on) ids.push(id);
  if (ids.length) control.setAttribute('aria-describedby', ids.join(' '));
  else control.removeAttribute('aria-describedby');
}

/**
 * A secret shown as the mask ("••••••") is one unit: the first keystroke, paste or delete replaces
 * the whole mask instead of editing it (typing after it would never be a valid token). A read-only or
 * disabled field (a password set in config/sentinel.env) is left alone: Chromium fires beforeinput on
 * a read-only input too, and emptying it would read as "no password saved".
 *
 * With the field's `node`, a muted line under the input (SAVED_SECRET_NOTE) says that the dots are
 * the saved secret, hidden on purpose, while the field shows the mask and can be typed in. The line
 * follows typing by itself; after setting the value from code, call the returned function.
 * @returns {() => void} repaint the line
 */
export function maskAsOne(control, node) {
  const locked = () => control.readOnly || control.disabled;
  control.addEventListener('beforeinput', (event) => {
    if (locked() || control.value !== MASK) return;
    control.value = '';
    if (String(event.inputType || '').startsWith('delete')) {
      event.preventDefault();
      control.dispatchEvent(new Event('input', { bubbles: true }));
    }
  });
  if (!node) return () => {};
  // muted like the help, but not the help: relabel() never writes into it
  const note = el('p', { class: 'field-help', id: `${control.id}-saved`, text: SAVED_SECRET_NOTE, dataset: { note: 'saved' } });
  control.insertAdjacentElement('afterend', note);
  const paint = () => {
    const shown = control.value === MASK && !locked();
    note.hidden = !shown;
    describedBy(control, note.id, shown);
  };
  control.addEventListener('input', paint);
  paint();
  return paint;
}

let chipSeq = 0;

/** A visible label over a chip row (radio semantics), with optional help; `.setHelp(text)` swaps the help. */
export function chipField(label, chips, help) {
  const id = `chip-field-${++chipSeq}`;
  chips.setAttribute('aria-labelledby', id);
  const helpEl = el('p', { class: 'field-help', text: help || '' });
  helpEl.hidden = !help;
  const node = el('div', 'field', el('span', { class: 'field-label', id, text: label }), chips, helpEl);
  node.setHelp = (text) => {
    helpEl.textContent = text || '';
    helpEl.hidden = !text;
  };
  return node;
}

/** Change a field's visible label (and its help text, when given) without rebuilding it. */
export function relabel(node, label, help) {
  const labelEl = node.querySelector('.field-label');
  if (labelEl) labelEl.textContent = label;
  if (help === undefined) return;
  // the field's own help, not a line added under the input (a saved secret's "Saved. Hidden …")
  const helpEl = node.querySelector('.field-help:not([data-note])');
  if (!helpEl) return;
  helpEl.textContent = help || '';
  helpEl.hidden = !help;
}

/**
 * The setting a validation error names ("notifiers.email.smtp_host: …", "dspace.ui_url: …"): a path
 * under one of the configuration's sections, then a colon and a space. A mail server and its port
 * ("smtp.gmail.com:587 closed the connection: …") is not one.
 */
const SETTING_PATH_RE = /^(?:server|setup|collector|dspace|postgres|solr|nginx|ssl|ports|logs|alerts|notifiers|reports|modules|appearance)(?:\.\w+)+:\s+/;

/**
 * A validation error from PATCH /api/config, or a test's outcome, as one plain sentence: the
 * "invalid configuration: notifiers.email.recipients.1.address: Value error, " wrapper goes and a
 * leading word is capitalised ("Not a Telegram chat id: x"); an address, a host or an id at the start
 * stays as it is ("smtp.gmail.com:587 closed the connection: …").
 */
export function plainError(message) {
  let text = String(message || '').trim()
    .replace(/^invalid configuration:\s*/i, '')
    .replace(SETTING_PATH_RE, '')
    .replace(/^value error,\s*/i, '');
  if (/^[a-z]+(?:[\s,;]|:\s|$)/.test(text)) text = text.charAt(0).toUpperCase() + text.slice(1);
  // a full stop only after words, never after a quoted value ("… address: bob@")
  if (/[A-Za-z)]$/.test(text) && !/:\s/.test(text)) text += '.';
  return text;
}

/** Parse a numeric input: null when empty, NaN when not a number. */
export function readNumber(control) {
  const raw = String(control.value).trim();
  if (!raw) return null;
  return Number(raw.replace(',', '.'));
}

/** The one filled button of a settings form. */
export function saveButton(label = 'Save changes') {
  return button({ label, variant: 'filled', type: 'submit', busyLabel: 'Saving…' });
}

/** Load the masked config; `null` when the screen was destroyed meanwhile. Other errors propagate. */
export async function loadConfig(ctx) {
  try {
    return await api.config({ signal: ctx.signal });
  } catch (err) {
    if (err && err.name === 'AbortError') return null;
    throw err;
  }
}

/**
 * PATCH /api/config with busy state on `button`; snackbar "Saved" (or the error). Resolves the
 * masked config on success, `null` on failure. `onError(err)` may place the error itself (under the
 * field it concerns) and return true, which skips the generic "Not saved" snackbar. An empty `label`
 * saves without the "Saved" snackbar (a test that saves first shows its own while it sends).
 */
export async function submitPatch({ patch, button: btn, label = 'Saved', signal, onError } = {}) {
  if (btn) btn.setBusy(true);
  try {
    const cfg = await api.patchConfig(patch, { signal });
    if (label) snackbar(label);
    return cfg;
  } catch (err) {
    if (err && err.name === 'AbortError') return null;
    if (!(onError && onError(err))) snackbar(`Not saved — ${err.message}`, { duration: 8000 });
    return null;
  } finally {
    if (btn) btn.setBusy(false);
  }
}

/** Replace the screen with a one-sentence error state. */
export function errorState(root, err) {
  root.replaceChildren(emptyState('⚠️', `Settings could not be loaded — ${err && err.message ? err.message : 'unknown error'}. Reload to try again.`));
}

const WEEKDAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];

/** Weekday name for the config convention 1 = Monday … 7 = Sunday (0 is also Sunday). */
export function weekdayName(n) {
  return WEEKDAYS[Number(n) % 8] || 'Monday';
}

/** "08:00" for an hour. */
export function hourLabel(h) {
  return `${String(Number(h) || 0).padStart(2, '0')}:00`;
}

/**
 * The zone Sentinel uses for the times it sends and for quiet hours, digests and the weekly report,
 * from /api/meta's `timezone` ({name, abbreviation}): "Asia/Colombo (+0530)", "Etc/UTC (UTC)", or ''
 * when the server does not say. The dashboard itself keeps the browser's zone.
 */
export function zoneWords(meta) {
  const tz = meta && meta.timezone;
  if (!tz || !tz.name) return '';
  return tz.abbreviation && tz.abbreviation !== tz.name ? `${tz.name} (${tz.abbreviation})` : String(tz.name);
}

/** ", in Asia/Colombo (+0530)" for a help sentence about a time of day ('' when the zone is not known). */
export function inZone(meta) {
  const words = zoneWords(meta);
  return words ? `, in ${words}` : '';
}
