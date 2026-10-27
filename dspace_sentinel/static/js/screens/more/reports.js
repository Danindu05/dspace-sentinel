/**
 * screens/more/reports.js — #/more/reports: the weekly report schedule (SPEC §12.5).
 *
 * Weekly switch, weekday and hour, delivery ("Send to Telegram" / "Send by email", each shown only
 * while that channel can deliver; a hidden one keeps its saved value) and the default sections used
 * by the schedule and preselected in the "＋ New report" panel.
 */
import api from '../../api.js';
import { sectionOn } from '../../panels/report.js';
import { store } from '../../store.js';
import { button, el, select, toggle } from '../../ui.js';
import { errorState, formEl, group, hourLabel, loadConfig, saveButton, submitPatch, weekdayName, zoneWords } from './form.js';

export const title = 'Reports';

const WEEKDAY_OPTIONS = [1, 2, 3, 4, 5, 6, 7].map((n) => ({ value: n, label: weekdayName(n) }));
const HOUR_OPTIONS = Array.from({ length: 24 }, (_, h) => ({ value: h, label: hourLabel(h) }));

/**
 * "Monday 08:00 · in 3 days" for the next scheduled run, with the schedule's zone after the time when
 * the server names it ("Monday 08:00 Asia/Colombo (+0530) · in 3 days"); the days are counted here.
 */
function nextRun(weekday, hour, zone = '') {
  const now = new Date();
  const target = new Date(now);
  const iso = Number(weekday) || 7;
  const todayIso = now.getDay() === 0 ? 7 : now.getDay();
  let ahead = (iso - todayIso + 7) % 7;
  target.setHours(Number(hour) || 0, 0, 0, 0);
  if (ahead === 0 && target <= now) ahead = 7;
  target.setDate(now.getDate() + ahead);
  const days = Math.round((target - now) / 86400000);
  const when = days === 0 ? 'later today' : days === 1 ? 'tomorrow' : `in ${days} days`;
  return `${weekdayName(iso)} ${hourLabel(hour)}${zone ? ` ${zone}` : ''} · ${when}`;
}

/**
 * Render the report schedule.
 * @returns {() => void} destroy
 */
export async function render(root, params, ctx) {
  let cfg;
  let options = null;
  try {
    [cfg, options] = await Promise.all([loadConfig(ctx), api.get('api/reports/options', null, { signal: ctx.signal }).catch(() => null)]);
  } catch (err) {
    errorState(root, err);
    return () => {};
  }
  if (!cfg) return () => {};
  const weekly = (cfg.reports && cfg.reports.weekly) || {};
  const defaults = new Set((cfg.reports && cfg.reports.default_sections) || []);
  const telegramReady = Boolean(options && options.telegram_available);
  const emailReady = Boolean(options && options.email_available);

  const form = formEl({ id: 'reports-form' });
  const enabled = toggle({ emoji: '📄', label: 'Weekly report', description: 'Generated automatically and listed under History › Reports.', checked: Boolean(weekly.enabled), id: 'rp-enabled' });
  const weekday = select({ options: WEEKDAY_OPTIONS, value: Number(weekly.weekday) || 1, id: 'rp-weekday', ariaLabel: 'Weekday' });
  const hour = select({ options: HOUR_OPTIONS, value: Number(weekly.hour) || 0, id: 'rp-hour', ariaLabel: 'Hour' });
  const next = el('p', { class: 'form-note', text: '' });
  const sendTelegram = telegramReady ? toggle({ emoji: '📨', label: 'Send to Telegram', description: 'The file goes to every Telegram bot as a document.', checked: Boolean(weekly.send_telegram), id: 'rp-telegram' }) : null;
  const sendEmail = emailReady ? toggle({ emoji: '✉️', label: 'Send by email', description: 'The file goes to every email recipient as an attachment.', checked: Boolean(weekly.send_email), id: 'rp-email' }) : null;
  // neither channel can deliver yet: one sentence that names where to set one up
  const noDelivery = telegramReady || emailReady ? null : el('p', { class: 'form-note' },
    '📨 To have the weekly report delivered, set up ', el('a', { href: '#/more/telegram', text: 'More › Telegram' }), ' or ', el('a', { href: '#/more/email', text: 'More › Email' }), '.');

  const sections = el('div', { class: 'check-list', role: 'group', 'aria-label': 'Default sections' });
  const allSections = (options && options.sections) || Array.from(defaults).map((id) => ({ id, emoji: '', label: id }));
  // sections of modules that are off are not shown; saving keeps them as they were
  const sectionList = allSections.filter((sec) => sectionOn(sec.id));
  for (const sec of sectionList) {
    const box = el('input', { type: 'checkbox', name: 'sections', value: sec.id });
    box.checked = defaults.has(sec.id);
    sections.appendChild(el('label', 'check', box, el('span', { class: 'check-emoji', 'aria-hidden': 'true', text: sec.emoji || '' }), el('span', { text: sec.label })));
  }

  const save = saveButton();
  const newReport = button({ label: '＋ New report', variant: 'ghost', onClick: () => ctx.openPanel('report') });
  form.append(
    enabled,
    group('When', null,
      el('div', 'pair',
        el('div', 'field', el('label', { class: 'field-label', htmlFor: 'rp-weekday', text: 'Day' }), weekday),
        el('div', 'field', el('label', { class: 'field-label', htmlFor: 'rp-hour', text: 'Time' }), hour)),
      next,
      sendTelegram,
      sendEmail,
      noDelivery),
    group('Default sections', 'Used by the weekly report and preselected in the New report panel.', sections),
    el('div', 'form-actions', save, newReport),
  );
  root.replaceChildren(form);

  function paintNext() {
    // the schedule runs in the zone set in More › Alert thresholds (the server's own zone by default)
    const zone = zoneWords(store.get('meta'));
    next.textContent = enabled.input.checked ? `Next report: ${nextRun(weekday.value, hour.value, zone)}${zone ? '' : ' (server local time)'}.` : 'Off — reports are only generated from the ＋ New report panel.';
  }
  paintNext();
  enabled.input.addEventListener('change', paintNext);
  weekday.addEventListener('change', paintNext);
  hour.addEventListener('change', paintNext);

  async function submit(event) {
    event.preventDefault();
    const picked = Array.from(sections.querySelectorAll('input[name="sections"]:checked')).map((i) => i.value);
    if (!picked.length) {
      sections.querySelector('input').focus();
      next.textContent = 'Pick at least one section.';
      return;
    }
    const hiddenDefaults = allSections.filter((sec) => !sectionOn(sec.id) && defaults.has(sec.id)).map((sec) => sec.id);
    const patch = {
      reports: {
        weekly: {
          enabled: enabled.input.checked, weekday: Number(weekday.value), hour: Number(hour.value),
          send_telegram: sendTelegram ? sendTelegram.input.checked : Boolean(weekly.send_telegram),
          send_email: sendEmail ? sendEmail.input.checked : Boolean(weekly.send_email),
        },
        default_sections: allSections.map((sec) => sec.id).filter((id) => picked.includes(id) || hiddenDefaults.includes(id)),
      },
    };
    const saved = await submitPatch({ patch, button: save, label: 'Saved', signal: ctx.signal });
    if (saved) {
      cfg = saved;
      paintNext();
    }
  }
  form.addEventListener('submit', submit);
  return () => form.removeEventListener('submit', submit);
}
