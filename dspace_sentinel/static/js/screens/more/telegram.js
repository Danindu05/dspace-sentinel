/**
 * screens/more/telegram.js — #/more/telegram: Telegram, one bot per person (SPEC §12.5).
 *
 * Switch · "Bots": 1-5 cards, one per person (name, their own bot token — masked once saved; the mask
 * plus the card's id keeps the stored token — chat id, "All alerts" / "Critical only", Test, ✕),
 * "＋ Add a bot" (stops at 5: "Up to 5 bots, one per person") and the setup guide · quiet hours and the
 * daily digest · Save changes / Send test message. A bot set in the environment (SENTINEL_TELEGRAM_TOKEN
 * + SENTINEL_TELEGRAM_CHAT_ID) is shown read-only and never sent back. A test saves the form first, then
 * POSTs /api/actions/telegram-test ({id} for one card, nothing for all); each card's status line then
 * names its bot: "✅ Alex · @alex_sentinel_bot · delivered".
 */
import api from '../../api.js';
import { store } from '../../store.js';
import { button, disclosure, el, fmt, snackbar, toggle } from '../../ui.js';
import { destinationList, everyone } from './destinations.js';
import {
  MASK, errorState, formEl, group, inZone, loadConfig, plainError, quietHoursError, saveButton, submitPatch, textField,
} from './form.js';

export const title = 'Telegram';

const MAX_BOTS = 5;
/** A token from @BotFather: "123456789:AAH4x…" (the server checks the same shape). */
const TOKEN_RE = /^\d{5,12}:[A-Za-z0-9_-]{30,50}$/;
/** A user or group id (groups are negative) or an @channel name. */
const CHAT_ID_RE = /^(-?\d{1,20}|@[A-Za-z0-9_]{5,32})$/;
const FIELDS = [
  { key: 'label', label: 'Name (optional)', placeholder: 'Alex', maxLength: 40 },
  { key: 'bot_token', label: 'Bot token', type: 'password', placeholder: '123456789:AAH4x…', resetsStatus: true },
  { key: 'chat_id', label: 'Chat id', placeholder: '987654321', resetsStatus: true },
];

/** A dash between the two times of quiet hours, as typed or pasted ("23:00 – 07:00"): the server reads it as "-". */
const QUIET_DASH_RE = /\s*[-\u2010-\u2015\u2212]\s*/;

/** The destinations of a config that predates per-person bots (one token + one chat id). */
function destinations(tg) {
  if (Array.isArray(tg.chats)) return tg.chats;
  return tg.chat_id || tg.bot_token ? [{ label: '', bot_token: tg.bot_token || '', chat_id: tg.chat_id || '', min_severity: 'warning' }] : [];
}

/** "@alex_sentinel_bot" from a getMe username with or without its "@". */
function botHandle(name) {
  const bare = String(name || '').trim().replace(/^@/, '');
  return bare ? `@${bare}` : '';
}

/** One result line: "✅ Alex · @alex_sentinel_bot · delivered" / "❌ Team · @team_bot · chat not found — …". */
function resultLine(res) {
  const who = res.label || res.chat_id || 'Bot';
  const bot = botHandle(res.bot_name);
  const outcome = res.ok ? 'delivered' : plainError(res.error).replace(/^\w/, (c) => c.toLowerCase()).replace(/\.$/, '') || 'not delivered';
  return `${res.ok ? '✅' : '❌'} ${who}${bot ? ` · ${bot}` : ''} · ${outcome}`;
}

/**
 * Render the Telegram settings.
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
  const tg = (cfg.notifiers && cfg.notifiers.telegram) || {};
  const chats = destinations(tg);

  const form = formEl({ id: 'telegram-form' });
  const enabled = toggle({ emoji: '📨', label: 'Send alerts to Telegram', description: 'Opened, escalated and resolved alerts, incidents and scheduled reports go to every bot below.', checked: Boolean(tg.enabled), id: 'tg-enabled', onChange: () => paintHint() });
  const hint = el('p', { class: 'form-note form-note--warn' });
  hint.hidden = true;
  const note = el('p', { class: 'form-note', role: 'status' });
  note.hidden = true;
  // one test result per card is read out from here (the cards' own lines are not live regions)
  const live = el('p', { class: 'sr-only', role: 'status' });

  const list = destinationList({
    noun: 'bot',
    nounPlural: 'bots',
    fields: FIELDS,
    items: chats.filter((c) => !c.from_env),
    fixed: chats.filter((c) => c.from_env),
    max: MAX_BOTS,
    addLabel: '＋ Add a bot',
    caption: 'Up to 5 bots, one per person',
    nameOf: (v) => v.label || v.chat_id,
    describeFixed: (c) => ({ emoji: '🔒', title: c.chat_id ? `Chat ${c.chat_id}` : c.label || 'From environment', subtitle: 'From environment · set in config/sentinel.env' }),
    onTest: (row) => testOne(row),
    onChange: () => paintHint(),
  });

  const guide = disclosure({
    summary: 'How to set it up',
    children: el('ol', 'steps',
      el('li', { text: 'Each person opens @BotFather in Telegram, sends /newbot, picks a name and copies the token it answers with.' }),
      el('li', { text: 'They open their new bot and send it any message, so the bot may write to them (for a group: add the bot and mention it once).' }),
      el('li', { text: 'They get their chat id, for example from @userinfobot (a group\'s id starts with a minus).' }),
      el('li', { text: 'Paste both into one row here, switch Telegram on and click Test.' }),
    ),
  });

  // both times are in the zone set in More › Alert thresholds (the server's own zone by default)
  const zone = inZone(store.get('meta'));
  const quiet = textField({ label: 'Quiet hours', id: 'tg-quiet', value: tg.quiet_hours || '', placeholder: '23:00-07:00', help: `Only critical alerts during this window${zone}, for every bot. Leave empty to always notify.` });
  const digest = textField({ label: 'Daily digest', type: 'time', id: 'tg-digest', value: tg.daily_digest || '', help: `One summary message per day at this time${zone}. Leave empty for none.` });

  const save = saveButton();
  const testAll = button({ label: 'Send test message', variant: 'ghost', busyLabel: 'Sending…', onClick: sendTestAll });

  form.append(
    enabled,
    group('Bots', 'One row per person: their own bot\'s token and their chat id. Critical only skips warnings; tests, digests and reports reach every bot.', list.el, hint, note, live, guide),
    group('When', null, el('div', 'pair', quiet.node, digest.node)),
    el('div', 'form-actions', save, testAll),
  );
  root.replaceChildren(form);
  paintHint();

  function showNote(text, tone = 'muted') {
    note.textContent = text;
    note.className = `form-note form-note--${tone}`;
    note.hidden = !text;
  }

  /** Telegram on with nowhere to send: allowed, but say so. */
  function paintHint() {
    const ready = list.allRows().some((row) => {
      if (row.fixed) return true;
      const v = row.values();
      return Boolean(v.bot_token && v.chat_id);
    });
    hint.textContent = '⚠️ Telegram is on but no bot is set up yet — add a bot token and a chat id so alerts have somewhere to go.';
    hint.hidden = !enabled.input.checked || ready;
  }

  /** Validate the cards and the fields; returns {values, sent} or null after showing the errors. */
  function collect() {
    let first = null;
    const flag = (row, key, text) => {
      row.setError(key, text);
      first = first || row.inputs[key];
    };
    const sent = [];
    const pairs = new Set();
    for (const row of list.rows()) {
      row.clearErrors();
      if (row.isEmpty()) continue;
      const v = row.values();
      if (!v.bot_token) flag(row, 'bot_token', 'Paste the token @BotFather gave for this bot.');
      else if (v.bot_token === MASK && !row.id) flag(row, 'bot_token', 'Paste the bot token again.');
      else if (v.bot_token !== MASK && !TOKEN_RE.test(v.bot_token)) flag(row, 'bot_token', 'This is not a bot token — it looks like 123456789:ABC…, from @BotFather.');
      if (!v.chat_id) flag(row, 'chat_id', 'Enter the chat id — @userinfobot tells each person theirs.');
      else if (!CHAT_ID_RE.test(v.chat_id)) flag(row, 'chat_id', 'A chat id is a number (negative for a group) or an @channel name.');
      else if (v.bot_token && v.bot_token !== MASK) {
        const pair = `${v.bot_token}\n${v.chat_id}`;
        if (pairs.has(pair)) flag(row, 'chat_id', 'This bot and chat id are already in another row.');
        pairs.add(pair);
      }
      const entry = { label: v.label, bot_token: v.bot_token, chat_id: v.chat_id, min_severity: v.min_severity };
      if (row.id) entry.id = row.id;
      sent.push({ row, entry });
    }
    quiet.node.setError('');
    const values = {
      enabled: enabled.input.checked,
      chats: sent.map((s) => s.entry),
      quiet_hours: quiet.input.value.trim().replace(QUIET_DASH_RE, '-'),
      daily_digest: digest.input.value.trim(),
    };
    const quietError = quietHoursError(values.quiet_hours);
    if (quietError) {
      quiet.node.setError(quietError);
      first = first || quiet.input;
    }
    if (first) {
      first.focus();
      return null;
    }
    return { values, sent };
  }

  /** Put a 400 from the server under the card and field it is about; false leaves it to the snackbar. */
  function placeServerError(sent) {
    return (err) => {
      if (!err || err.status !== 400) return false;
      const msg = String(err.message || '');
      // the channel's own fields: "notifiers.telegram.quiet_hours: quiet hours must look like 23:00-07:00"
      for (const [re, fld] of [[/quiet_hours|quiet hours/i, quiet], [/daily_digest|digest time/i, digest]]) {
        if (!re.test(msg)) continue;
        fld.node.setError(plainError(msg));
        fld.input.focus();
        return true;
      }
      const unquote = (text) => text.trim().replace(/^['"]|['"]$/g, '');
      const byText = (text) => {
        const t = unquote(text).toLowerCase();
        return sent.find(({ entry }) => (entry.label || '').toLowerCase() === t || entry.chat_id.toLowerCase() === t);
      };
      let hit = null;
      let key = 'chat_id';
      let m = msg.match(/chats\.(\d+)\.(\w+)/);
      if (m && sent[Number(m[1])]) {
        hit = sent[Number(m[1])];
        key = FIELDS.some((f) => f.key === m[2]) ? m[2] : 'chat_id';
      } else if ((m = msg.match(/bot token for (.+?) again/i))) {
        hit = byText(m[1]);
        key = 'bot_token';
      } else if ((m = msg.match(/not a Telegram chat id: (.+)$/i))) {
        hit = sent.find(({ entry }) => entry.chat_id === unquote(m[1]));
      } else if (/not a Telegram bot token/i.test(msg)) {
        hit = sent.find(({ entry }) => entry.bot_token !== MASK && !TOKEN_RE.test(entry.bot_token));
        key = 'bot_token';
      } else if ((m = msg.match(/(\S+) is listed twice/i))) {
        // "Telegram chat -100123 is listed twice with the same bot": the second row with that chat
        const t = unquote(m[1]).toLowerCase();
        hit = sent.filter(({ entry }) => entry.chat_id.toLowerCase() === t).pop();
      }
      if (!hit) return false;
      hit.row.setError(key, plainError(msg));
      hit.row.focus(key);
      return true;
    };
  }

  /** After a save: new cards learn their server id, and every stored token reads as the mask again. */
  function adopt(sent, saved) {
    const stored = destinations(((saved.notifiers || {}).telegram) || {}).filter((c) => !c.from_env);
    const used = new Set();
    sent.forEach(({ row, entry }, i) => {
      let j = stored[i] && stored[i].chat_id === entry.chat_id ? i : -1;
      if (j < 0 || used.has(j)) j = stored.findIndex((c, k) => !used.has(k) && c.chat_id === entry.chat_id && (c.label || '') === entry.label);
      if (j < 0) return;
      used.add(j);
      if (stored[j].id) row.id = stored[j].id;
      row.setValue('bot_token', stored[j].bot_token || '');
    });
  }

  async function save_(label) {
    const collected = collect();
    if (!collected) return null;
    const saved = await submitPatch({ patch: { notifiers: { telegram: collected.values } }, button: save, label, signal: ctx.signal, onError: placeServerError(collected.sent) });
    if (saved) {
      cfg = saved;
      adopt(collected.sent, saved);
      list.prune();
      paintHint();
    }
    return saved;
  }

  async function submit(event) {
    event.preventDefault();
    await save_('Saved');
  }

  function isDemo(r) {
    return r.skipped === 'demo' || /demo mode/i.test(r.error || '');
  }

  /** Write each tested card's line; returns [delivered, tested]. */
  function paintResults(r, rows) {
    const results = Array.isArray(r.results) ? r.results : [];
    let delivered = 0;
    let tested = 0;
    for (const row of rows) {
      const res = results.find((x) => x.id === row.id);
      if (!res) continue;
      tested += 1;
      if (res.ok) delivered += 1;
      row.setStatus(resultLine({ label: row.fixed ? row.name() : row.values().label, chat_id: row.values().chat_id, ...res }), res.ok ? 'ok' : 'crit');
    }
    return [delivered, tested];
  }

  /** "Test" on one card: save (for a card of the form), then test that bot alone. */
  async function testOne(row) {
    showNote('');
    live.textContent = '';
    if (!row.fixed && row.isEmpty()) {
      row.setError('bot_token', 'Fill in the bot token and the chat id first, then test.');
      row.focus('bot_token');
      return;
    }
    row.setStatus('');
    row.test.setBusy(true);
    let sending = null;
    try {
      if (!row.fixed) {
        const saved = await save_('');
        if (!saved) return;
        if (!row.id) {
          row.setStatus('❌ Saved, but this row has no id yet — reload the page and test again.', 'crit');
          return;
        }
      }
      // the snackbar lasts as long as the send; the card's line carries the outcome
      sending = snackbar(row.fixed ? 'Sending the test message…' : 'Saved — sending the test message…', { duration: 60000 });
      const r = await api.telegramTest({ id: row.id }, { signal: ctx.signal });
      if (isDemo(r)) {
        row.setStatus('Demo: logged, not sent', 'muted');
        live.textContent = 'Demo: logged, not sent';
        return;
      }
      const [, tested] = paintResults(r, [row]);
      if (!tested) row.setStatus(`❌ Not sent — ${plainError(r.error) || 'Telegram did not accept the message.'}`, 'crit');
      live.textContent = row.statusEl.textContent;
    } catch (err) {
      if (err && err.name === 'AbortError') return;
      row.setStatus(`❌ Could not send — ${err.message}`, 'crit');
      live.textContent = row.statusEl.textContent;
    } finally {
      if (sending) sending.dismiss();
      row.test.setBusy(false);
    }
  }

  /** "Send test message": save, then test every bot; one line per card and a summary under the list. */
  async function sendTestAll() {
    showNote('');
    live.textContent = '';
    list.clearStatus();
    testAll.setBusy(true);
    let sending = null;
    try {
      const saved = await save_('');
      if (!saved) return;
      sending = snackbar('Saved — sending the test message…', { duration: 60000 });
      const r = await api.telegramTest(undefined, { signal: ctx.signal });
      if (isDemo(r)) {
        showNote('Demo: logged, not sent — Telegram is never called while the demo runs; the message is in the events log.', 'muted');
        return;
      }
      const [delivered, tested] = paintResults(r, list.allRows());
      if (!tested) showNote(`❌ Not sent — ${plainError(r.error) || 'no bot is set up yet.'}`, 'crit');
      else if (delivered === tested) showNote(`✅ Test message delivered to ${everyone(tested, 'bot')}.`, 'ok');
      else showNote(`❌ ${delivered} of ${fmt.plural(tested, 'bot')} got the test message — each row says why.`, 'crit');
    } catch (err) {
      if (err && err.name === 'AbortError') return;
      showNote(`❌ Could not send — ${err.message}`, 'crit');
    } finally {
      if (sending) sending.dismiss();
      testAll.setBusy(false);
    }
  }

  form.addEventListener('submit', submit);
  return () => form.removeEventListener('submit', submit);
}

export { MASK };
