/**
 * screens/more.js — the More tab (SPEC §12.5).
 *
 * "Turn on only what you need. Sentinel stays simple." → module switches (left) · Settings rows
 * (right: ⚠️ Alert thresholds · 📨 Telegram · ✉️ Email · 🔗 DSpace connection · 🎨 Appearance ·
 * 📄 Reports · 👤 Account, only while a dashboard login is set · ℹ️ About) · Danger zone with the only two
 * confirm dialogs in the app (Clear history, Reset).
 * The rows are asked for again whenever a settings page on top of this screen closes.
 */
import api from '../api.js';
import { store } from '../store.js';
import { button, column, confirm, el, fmt, grid, keepUnits, replaceKeepingFocus, section, snackbar, tile } from '../ui.js';
import { hourLabel, weekdayName } from './more/form.js';
import { MODULES, moduleList } from './more/modules.js';

const THEME_LABEL = { system: 'System', light: 'Light', dark: 'Dark' };

/** "On · 3 chats" / "On · no chats yet" / "Off" — the state of a notifier in one line. */
function notifierSub(settings, count, noun) {
  if (!settings.enabled) return 'Off';
  return count ? `On · ${fmt.plural(count, noun)}` : `On · no ${noun}s yet`;
}

/** Telegram destinations, also from a config written before one bot per person (a single chat_id). */
function telegramCount(tg) {
  if (Array.isArray(tg.chats)) return tg.chats.length;
  return tg.chat_id ? 1 : 0;
}

/** "sent to Telegram and email" / "sent to Telegram" / "sent by email" / '' for the weekly report. */
function deliveryWords(toTelegram, byEmail) {
  if (toTelegram && byEmail) return 'sent to Telegram and email';
  if (toTelegram) return 'sent to Telegram';
  return byEmail ? 'sent by email' : '';
}

/**
 * How the weekly report really goes out: a channel counts only when the schedule asks for it and
 * GET /api/reports/options says it can take the file (on, with a bot or a login and a recipient).
 * Until that answer is in (or when it fails), no channel is claimed.
 */
function weeklyDelivery(weekly, available) {
  if (!available) return '';
  return deliveryWords(Boolean(weekly.send_telegram && available.telegram), Boolean(weekly.send_email && available.email));
}

function settingsRows(cfg, available) {
  const meta = store.get('meta') || {};
  const alerts = cfg && cfg.alerts;
  const tg = cfg && cfg.notifiers && cfg.notifiers.telegram;
  const em = cfg && cfg.notifiers && cfg.notifiers.email;
  const weekly = cfg && cfg.reports && cfg.reports.weekly;
  const dspace = cfg && cfg.dspace;
  const rows = [
    {
      page: 'alerts', emoji: '⚠️', title: 'Alert thresholds',
      sub: alerts ? keepUnits(`CPU ${fmt.number(alerts.cpu.warning)} / ${fmt.number(alerts.cpu.critical)} % · memory ${fmt.number(alerts.memory.warning)} / ${fmt.number(alerts.memory.critical)} % · disk ${fmt.number(alerts.disk.warning)} / ${fmt.number(alerts.disk.critical)} %`) : 'When Sentinel warns and when it escalates',
    },
    { page: 'telegram', emoji: '📨', title: 'Telegram', sub: tg ? notifierSub(tg, telegramCount(tg), 'chat') : 'One bot per person, up to 5' },
    { page: 'email', emoji: '✉️', title: 'Email', sub: em ? notifierSub(em, (em.recipients || []).length, 'recipient') : 'Gmail or your own mail server, up to 5 recipients' },
    { page: 'connection', emoji: '🔗', title: 'DSpace connection', sub: dspace ? `${dspace.server_url || '—'} · ${dspace.install_dir || '—'}` : 'URLs, directories, service names' },
    // the theme is a setting, not a metric: a small pill, not a big numeric value
    { page: 'appearance', emoji: '🎨', title: 'Appearance', sub: 'System, light or dark', pill: el('span', { class: 'pill', text: THEME_LABEL[store.get('theme')] || 'System' }) },
    { page: 'reports', emoji: '📄', title: 'Reports', sub: weekly ? (weekly.enabled ? [`Weekly report every ${weekdayName(weekly.weekday)} at ${hourLabel(weekly.hour)}`, weeklyDelivery(weekly, available)].filter(Boolean).join(' · ') : 'Weekly report off') : 'Weekly schedule and default sections' },
    { page: 'about', emoji: 'ℹ️', title: 'About', sub: `Sentinel v${meta.version || ''}${meta.demo ? ' · demo mode' : ''} · ${meta.hostname || ''}` },
  ];
  // the dashboard login (server.login): its page exists only while one is set
  const session = store.get('session');
  if (session && session.login_required) {
    rows.splice(rows.length - 1, 0, { page: 'account', emoji: '👤', title: 'Account', sub: session.username ? `Signed in as ${session.username}` : 'Password and sign out' });
  }
  return rows.map((r) => tile({
    emoji: r.emoji, title: r.title, subtitle: r.sub, value: r.value, pill: r.pill || undefined, chevron: true,
    href: `#/more/${r.page}`, dataset: { key: r.page },
  }));
}

/**
 * Render the More tab.
 * @returns {() => void} destroy
 */
export function render(root, params, ctx) {
  const view = grid();
  const lede = el('p', { class: 'lede', text: 'Turn on only what you need. Sentinel stays simple.' });
  const modulesSection = section({ title: 'Modules', subtitle: 'Switches apply at once; Undo is in the snackbar.', layout: 'stack', id: 'more-modules' });
  const modules = moduleList(ctx);
  modulesSection.body.appendChild(modules.el);
  const settingsSection = section({ title: 'Settings', layout: 'stack', id: 'more-settings' });
  const dangerSection = section({ title: 'Danger zone', subtitle: 'The only two actions that ask you to confirm.', layout: 'stack', id: 'more-danger', className: 'danger-zone' });
  view.append(lede, el('div', 'span-6', column(modulesSection)), el('div', 'span-6', column(settingsSection, dangerSection)));
  root.replaceChildren(view);

  let cfg = null;
  let available = null; // {telegram, email}: which channels can take a report file right now
  let asked = 0; // the newest request for the rows: an older answer never paints over a newer one
  function paintSettings() {
    replaceKeepingFocus(settingsSection.body, settingsRows(cfg, available));
  }
  /** Ask for the configuration and the report channels, then repaint the rows. */
  function loadSettings() {
    const mine = ++asked;
    const current = () => mine === asked && !ctx.signal.aborted;
    api.config({ signal: ctx.signal }).then((c) => {
      if (!current()) return;
      cfg = c;
      paintSettings();
    }).catch(() => {});
    api.get('api/reports/options', null, { signal: ctx.signal }).then((o) => {
      if (!current()) return;
      available = { telegram: Boolean(o && o.telegram_available), email: Boolean(o && o.email_available) };
      paintSettings();
    }).catch(() => {});
  }
  paintSettings();
  loadSettings();
  // A settings page opens on top of this screen, which stays mounted (hidden) underneath: when the page
  // closes (its Back, the browser's Back, the More tab) the rows are asked for again, so what was just
  // saved there ("Off", "Weekly report off", the channels a report really goes to) shows at once.
  let covered = Boolean(ctx.route && ctx.route.sub);
  const offRoute = store.subscribe('route', ({ route }) => {
    if (!route || route.tab !== 'more') return;
    if (route.sub) {
      covered = true;
    } else if (covered) {
      covered = false;
      loadSettings();
    }
  });

  // Danger zone
  const clearBtn = button({ label: 'Clear history…', variant: 'danger', size: 'sm', busyLabel: 'Clearing…', onClick: clearHistory });
  const resetBtn = button({ label: 'Reset Sentinel…', variant: 'danger', size: 'sm', busyLabel: 'Resetting…', onClick: resetSentinel });
  dangerSection.body.append(
    tile({ emoji: '🧹', title: 'Clear history', subtitle: 'Removes samples, checks, alerts and incidents. Settings and reports stay.', actions: clearBtn, chevron: false }),
    tile({ emoji: '♻️', title: 'Reset Sentinel', subtitle: 'Turns every module off, clears the history and shows the first-run setup again. Connection, Telegram and email settings stay.', actions: resetBtn, chevron: false }),
  );

  async function clearHistory() {
    const ok = await confirm({ title: 'Clear history?', message: 'Samples, checks, alerts and incidents are removed for good. Settings and generated reports are kept.', confirmLabel: 'Clear history', danger: true });
    if (!ok) return;
    clearBtn.setBusy(true);
    try {
      await api.clearHistory();
      snackbar('History cleared — the next check starts a fresh record.');
    } catch (err) {
      snackbar(`Could not clear the history — ${err.message}`, { duration: 8000 });
    } finally {
      clearBtn.setBusy(false);
    }
  }

  async function resetSentinel() {
    const ok = await confirm({ title: 'Reset Sentinel?', message: 'Every module is switched off, the history is cleared and the first-run setup opens again. Connection details, Telegram and email settings are kept.', confirmLabel: 'Reset Sentinel', danger: true });
    if (!ok) return;
    resetBtn.setBusy(true);
    try {
      await api.clearHistory();
      await api.patchConfig({ setup: { completed: false, persona: '' }, modules: Object.fromEntries(MODULES.map(([id]) => [id, false])) });
      location.hash = '#/home';
      location.reload();
    } catch (err) {
      resetBtn.setBusy(false);
      snackbar(`Could not reset — ${err.message}`, { duration: 8000 });
    }
  }

  const offTheme = store.subscribe(['theme', 'meta', 'session'], paintSettings);
  return () => {
    offTheme();
    offRoute();
    modules.destroy();
  };
}
