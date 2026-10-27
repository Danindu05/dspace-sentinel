/**
 * screens/more/email.js — #/more/email: alerts and reports by email (SPEC §12.5).
 *
 * Switch · Account: provider chips "Gmail" (smtp.gmail.com, 587, STARTTLS, or the port and security a
 * saved Gmail server has; the server fields stay hidden) or "Other SMTP server" (host, port, security
 * chips, from address), the login ("Gmail address" / "Username"), the password ("App password" for
 * Gmail — masked once saved, sending the mask keeps it), "Send as" and, for Gmail, the setup guide ·
 * Recipients: 1-5 cards (address, label, "All alerts" / "Critical only", Test, ✕) with "＋ Add
 * recipient" · quiet hours and the daily digest · Save changes / Send test email. A test saves the
 * form first, then POSTs /api/actions/email-test ({address} for one card, nothing for all); each
 * card's status line shows its outcome.
 *
 * Gmail sends as the account that logs in, so saving with the Gmail chip clears from_address.
 *
 * The saved password is only ever sent to the server and login it was saved for (the server refuses
 * the mask for any other): once the person changes the provider, server, port, security or login to
 * another one, the field empties and says why; going back to the saved server and login brings the
 * mask back. Opening the page changes nothing: the saved settings are shown as they are.
 */
import api from '../../api.js';
import { store } from '../../store.js';
import { button, chipRow, disclosure, el, fmt, snackbar, toggle } from '../../ui.js';
import { destinationList, everyone } from './destinations.js';
import {
  MASK, chipField, errorState, formEl, group, inZone, loadConfig, maskAsOne, numberField, plainError, quietHoursError,
  readNumber, relabel, saveButton, submitPatch, textField,
} from './form.js';

export const title = 'Email';

const MAX_RECIPIENTS = 5;
const GMAIL_HOST = 'smtp.gmail.com';
/** One plain address, local@domain.tld (no display name, spaces, commas or angle brackets) — the server's rule. */
const ADDRESS_RE = /^[A-Za-z0-9!#$%&'*+\/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+\/=?^_`{|}~-]+)*@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$/;
/** A Gmail app password: 16 letters (Google shows four groups of four; the spaces do not count). */
const APP_PASSWORD_RE = /^[A-Za-z]{16}$/;
const PROVIDERS = [
  { value: 'gmail', label: 'Gmail' },
  { value: 'other', label: 'Other SMTP server' },
];
const SECURITY = [
  { value: 'starttls', label: 'STARTTLS' },
  { value: 'ssl', label: 'SSL/TLS' },
  { value: 'none', label: 'None' },
];
const FIELDS = [
  { key: 'address', label: 'Address', type: 'email', inputmode: 'email', placeholder: 'ops@example.org', resetsStatus: true },
  { key: 'label', label: 'Label (optional)', placeholder: 'Ops team', maxLength: 40 },
];
const WORDS = {
  gmail: {
    user: 'Gmail address',
    userHelp: 'The account Sentinel logs in with; alerts come from this address.',
    pass: 'App password',
    passHelp: 'Make one at Google Account › Security › 2-Step Verification › App passwords: 16 letters (spaces are fine). Your normal Gmail password will not work.',
  },
  other: {
    user: 'Username',
    userHelp: 'Leave empty when the server needs no login.',
    pass: 'Password',
    passHelp: 'Stored in sentinel.yaml (mode 0600) and never shown again.',
    provider: 'Your organisation\'s mail server or a relay.',
  },
};

/** A dash between the two times of quiet hours, as typed or pasted ("23:00 – 07:00"): the server reads it as "-". */
const QUIET_DASH_RE = /\s*[-\u2010-\u2015\u2212]\s*/;

/** Help under the password once another server or login emptied it. */
const PASSWORD_AGAIN = 'Enter the password again — the saved one is only sent to the server and login it was saved for.';
/** Help under a password set in config/sentinel.env (read-only here: it wins at every start). */
const PASSWORD_FROM_ENV = 'Set in config/sentinel.env (SENTINEL_SMTP_PASSWORD) — change it there.';
const PASSWORD_FROM_ENV_MOVED = 'Set in config/sentinel.env (SENTINEL_SMTP_PASSWORD) and only sent to the server it was set for — change the server and login in config/sentinel.yaml.';

/** The server and login a password belongs to: {host, port, security, user} (the host in lower case). */
function targetOf(host, port, security, user) {
  return { host: String(host || '').trim().toLowerCase().replace(/\.$/, ''), port: Number.isFinite(port) ? port : null, security: security || 'starttls', user: String(user || '').trim() };
}

function sameTarget(a, b) {
  return a.host === b.host && a.port === b.port && a.security === b.security && a.user === b.user;
}

/**
 * Gmail's port and security on the page: a saved Gmail server keeps its own, whatever they are
 * (Gmail answers on 587 with STARTTLS and on 465 with SSL; a file may say otherwise), so opening the
 * page never moves the saved password to another port; any other saved server switches to Gmail at
 * 587 with STARTTLS.
 */
function gmailServerOf(em) {
  const port = Number(em.smtp_port);
  const gmail = String(em.smtp_host || GMAIL_HOST).trim().toLowerCase() === GMAIL_HOST;
  return gmail && Number.isInteger(port) && port > 0 ? { port, security: em.security || 'starttls' } : { port: 587, security: 'starttls' };
}

const SECURITY_WORDS = { starttls: 'STARTTLS', ssl: 'SSL/TLS', none: 'no encryption' };

/** "smtp.gmail.com, port 587, STARTTLS — nothing else to fill in." for the port and security Gmail is saved with. */
function gmailProviderHelp({ port, security }) {
  return `smtp.gmail.com, port ${port}, ${SECURITY_WORDS[security] || security} — nothing else to fill in.`;
}

/** One result line: "✅ Ops team · sent" / "❌ ops@example.org · mailbox unavailable". */
function resultLine(res) {
  const who = res.label || res.address || 'Recipient';
  const outcome = res.ok ? 'sent' : plainError(res.error).replace(/^\w/, (c) => c.toLowerCase()).replace(/\.$/, '') || 'not sent';
  return `${res.ok ? '✅' : '❌'} ${who} · ${outcome}`;
}

/**
 * Render the email settings.
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
  const em = (cfg.notifiers && cfg.notifiers.email) || {};
  const loadedHost = String(em.smtp_host || GMAIL_HOST).trim();
  const loadedGmail = loadedHost.toLowerCase() === GMAIL_HOST;
  let gmailServer = gmailServerOf(em);
  // true once the person changes the provider, server, port, security or login: until then the page
  // shows the saved settings as they are and never counts as "moved to another server"
  let touched = false;
  const touch = (paint) => (...args) => {
    touched = true;
    paint(...args);
  };

  const form = formEl({ id: 'email-form' });
  const enabled = toggle({ emoji: '✉️', label: 'Send alerts by email', description: 'Opened, escalated and resolved alerts, incidents and scheduled reports go to every recipient below.', checked: Boolean(em.enabled), id: 'em-enabled', onChange: () => paintHint() });

  // Account
  const provider = chipRow({ options: PROVIDERS, value: loadedGmail ? 'gmail' : 'other', name: 'provider', onChange: touch(() => paintProvider(true)) });
  const providerField = chipField('Provider', provider, '');
  const host = textField({ label: 'SMTP server', id: 'em-host', value: loadedGmail ? '' : loadedHost, placeholder: 'smtp.example.org', onInput: touch(() => paintPassword()) });
  const port = numberField({ label: 'Port', id: 'em-port', value: loadedGmail ? 587 : em.smtp_port ?? 587, min: 1, max: 65535 });
  port.input.addEventListener('input', touch(() => paintPassword()));
  const security = chipRow({ options: SECURITY, value: loadedGmail ? 'starttls' : em.security || 'starttls', name: 'security', onChange: touch(swapPort) });
  const securityField = chipField('Security', security, 'STARTTLS on port 587 or SSL/TLS on 465. None only for a relay on this machine or your own network.');
  const username = textField({ label: WORDS.gmail.user, id: 'em-user', type: 'email', inputmode: 'email', value: em.username || '', placeholder: 'sentinel@gmail.com', help: WORDS.gmail.userHelp, onInput: touch(() => paintPassword()) });
  const password = textField({ label: WORDS.gmail.pass, id: 'em-pass', type: 'password', value: em.password || '', placeholder: 'abcd efgh ijkl mnop', help: WORDS.gmail.passHelp, onInput: () => paintPassword() });
  // SENTINEL_SMTP_PASSWORD wins at every start: the field only shows that one is set, and is never sent
  const passwordFromEnv = Boolean(em.password_from_env);
  password.input.readOnly = passwordFromEnv;
  // "Saved. Hidden for safety — …" under the dots of a saved password (not under a locked one)
  const paintSaved = maskAsOne(password.input, password.node);
  // the server and login the stored password (the mask) belongs to; true once another one emptied the field
  let savedTarget = targetOf(em.smtp_host, Number(em.smtp_port), em.security, em.username);
  let maskCleared = false;
  const fromAddress = textField({ label: 'From address (optional)', id: 'em-from', type: 'email', inputmode: 'email', value: em.from_address || '', placeholder: 'sentinel@example.org', help: 'Leave empty to send from the username.' });
  const fromName = textField({ label: 'Send as', id: 'em-from-name', value: em.from_name ?? 'DSpace Sentinel', placeholder: 'DSpace Sentinel', help: 'The sender name people see in their inbox.' });
  fromName.input.maxLength = 60;
  const serverBox = el('div', 'form-group', el('div', 'pair', host.node, port.node), securityField);
  const guide = disclosure({
    summary: 'How to set it up',
    children: el('ol', 'steps',
      el('li', { text: 'Turn on 2-Step Verification for the Gmail account: Google Account › Security › 2-Step Verification.' }),
      el('li', { text: 'Create an app password: Google Account › Security › 2-Step Verification › App passwords. Name it "DSpace Sentinel"; Google shows 16 letters once.' }),
      el('li', { text: 'Paste the Gmail address and the app password here, add up to five recipients, switch email on and send a test email.' }),
    ),
  });

  // Recipients
  const hint = el('p', { class: 'form-note form-note--warn' });
  hint.hidden = true;
  const note = el('p', { class: 'form-note', role: 'status' });
  note.hidden = true;
  const live = el('p', { class: 'sr-only', role: 'status' });
  const list = destinationList({
    noun: 'recipient',
    nounPlural: 'recipients',
    fields: FIELDS,
    items: Array.isArray(em.recipients) ? em.recipients : [],
    max: MAX_RECIPIENTS,
    addLabel: '＋ Add recipient',
    caption: 'Up to 5 recipients',
    nameOf: (v) => v.label || v.address,
    onTest: (row) => testOne(row),
    onChange: () => paintHint(),
  });

  // both times are in the zone set in More › Alert thresholds (the server's own zone by default)
  const zone = inZone(store.get('meta'));
  const quiet = textField({ label: 'Quiet hours', id: 'em-quiet', value: em.quiet_hours || '', placeholder: '23:00-07:00', help: `Only critical alerts by email during this window${zone}. Leave empty to always send.` });
  const digest = textField({ label: 'Daily digest', type: 'time', id: 'em-digest', value: em.daily_digest || '', help: `One summary email per day at this time${zone}. Leave empty for none.` });

  const save = saveButton();
  const testAll = button({ label: 'Send test email', variant: 'ghost', busyLabel: 'Sending…', onClick: sendTestAll });

  form.append(
    enabled,
    group('Account', null, providerField, serverBox, el('div', 'pair', username.node, password.node), el('div', 'pair', fromAddress.node, fromName.node), guide),
    group('Recipients', 'One email goes to everyone an alert is for. Critical only skips warnings; tests, digests and reports reach every recipient.', list.el, hint, note, live),
    group('When', null, el('div', 'pair', quiet.node, digest.node)),
    el('div', 'form-actions', save, testAll),
  );
  root.replaceChildren(form);
  paintProvider(false);
  paintHint();

  function gmail() {
    return provider.value === 'gmail';
  }

  /** Gmail hides the server, the from address and uses the Gmail words; Other shows them. */
  function paintProvider(changed) {
    const words = WORDS[gmail() ? 'gmail' : 'other'];
    providerField.setHelp(gmail() ? gmailProviderHelp(gmailServer) : words.provider);
    serverBox.hidden = gmail();
    fromAddress.node.hidden = gmail();
    guide.hidden = !gmail();
    relabel(username.node, words.user, words.userHelp);
    // another server's login is not always an address ("apikey"): a plain text field there
    username.input.type = gmail() ? 'email' : 'text';
    username.input.placeholder = gmail() ? 'sentinel@gmail.com' : 'sentinel';
    if (changed && !gmail() && !host.input.value.trim()) host.input.focus();
    for (const f of [host, port, username, password, fromAddress]) f.node.setError('');
    paintPassword();
  }

  /** SSL/TLS and STARTTLS each have their usual port; switching one moves the other along. */
  function swapPort(value) {
    const current = readNumber(port.input);
    if (value === 'ssl' && current === 587) port.input.value = '465';
    else if (value === 'starttls' && current === 465) port.input.value = '587';
    paintPassword();
  }

  /** The server and login the form would save. */
  function formTarget() {
    const user = username.input.value;
    if (gmail()) return targetOf(GMAIL_HOST, gmailServer.port, gmailServer.security, user);
    return targetOf(host.input.value, readNumber(port.input), security.value, user);
  }

  /**
   * The saved password (the mask) only goes to the server and login it was saved for: once the person
   * changes them, another one empties the field and asks for the password again; the saved one brings
   * the mask back. A server that needs no login (no username) is never sent a password, so it keeps
   * the field as it is.
   */
  function paintPassword() {
    const target = formTarget();
    const moved = touched && Boolean(target.user) && !sameTarget(target, savedTarget);
    const words = WORDS[gmail() ? 'gmail' : 'other'];
    if (passwordFromEnv) {
      // it cannot be typed here, so another server or login is set in the files (the server says so too)
      relabel(password.node, words.pass, moved ? PASSWORD_FROM_ENV_MOVED : PASSWORD_FROM_ENV);
      return;
    }
    if (moved && password.input.value === MASK) {
      password.input.value = '';
      maskCleared = true;
    } else if (!moved && maskCleared && !password.input.value) {
      password.input.value = MASK;
      maskCleared = false;
    }
    relabel(password.node, words.pass, maskCleared && !password.input.value ? PASSWORD_AGAIN : words.passHelp);
    paintSaved();
  }

  /** Email on with nobody to write to: allowed, but say so. */
  function paintHint() {
    const ready = list.rows().some((row) => Boolean(row.values().address));
    hint.textContent = '⚠️ Email is on but has no recipients yet — add one so alerts have somewhere to go.';
    hint.hidden = !enabled.input.checked || ready;
  }

  function showNote(text, tone = 'muted') {
    note.textContent = text;
    note.className = `form-note form-note--${tone}`;
    note.hidden = !text;
  }

  /** Validate everything; returns {values, sent} or null after showing the errors. */
  function collect() {
    let first = null;
    const flag = (fld, text) => {
      fld.node.setError(text);
      first = first || fld.input;
    };
    for (const f of [host, port, username, password, fromAddress, fromName, quiet]) f.node.setError('');
    const on = enabled.input.checked;
    const user = username.input.value.trim();
    let pass = password.input.value;
    const values = { enabled: on, username: user, from_name: fromName.input.value.trim() };
    if (gmail()) {
      if (on && !user) flag(username, 'Enter the Gmail address Sentinel sends from.');
      else if (user && !ADDRESS_RE.test(user)) flag(username, 'Enter the whole address, like sentinel@gmail.com.');
      if (pass !== MASK) pass = pass.replace(/\s+/g, '');
      // a password set in config/sentinel.env is neither checked nor sent
      if (!passwordFromEnv && on && !pass) flag(password, 'Paste the app password Google made for Sentinel.');
      else if (!passwordFromEnv && pass && pass !== MASK && !APP_PASSWORD_RE.test(pass)) flag(password, 'An app password is 16 letters (Google shows four groups of four). Your normal Gmail password will not work.');
      // Gmail sends as the account that logs in: a From address left over from another server is dropped
      Object.assign(values, { smtp_host: GMAIL_HOST, smtp_port: gmailServer.port, security: gmailServer.security, from_address: '' });
    } else {
      const server = host.input.value.trim();
      const portNo = readNumber(port.input);
      const from = fromAddress.input.value.trim();
      if (on && !server) flag(host, 'Enter the mail server, like smtp.example.org.');
      else if (server && !/^[A-Za-z0-9.:[\]_-]+$/.test(server)) flag(host, 'A server name has only letters, digits, dots and dashes.');
      if (portNo === null || Number.isNaN(portNo) || portNo < 1 || portNo > 65535 || portNo % 1 !== 0) flag(port, 'The port is a number between 1 and 65535.');
      // messages come from this address, else from the login: a login such as "apikey" needs it (the server says so too)
      if (from && !ADDRESS_RE.test(from)) flag(fromAddress, 'Enter one plain address, like sentinel@example.org.');
      else if (!from && user && !ADDRESS_RE.test(user)) flag(fromAddress, 'Enter the address alerts come from — the username is not one.');
      else if (on && !from && !user) flag(fromAddress, 'Enter the address alerts come from.');
      if (/^\s+$/.test(pass)) pass = '';
      if (on && user && !pass && !passwordFromEnv) flag(password, 'Enter the password for this login, or leave the username empty.');
      Object.assign(values, { smtp_host: server, smtp_port: portNo, security: security.value, from_address: from });
    }
    // a password from config/sentinel.env is never sent: the server refuses to change it
    if (!passwordFromEnv) values.password = pass;

    const sent = [];
    const seen = new Set();
    for (const row of list.rows()) {
      row.clearErrors();
      if (row.isEmpty()) continue;
      const v = row.values();
      if (!v.address) {
        row.setError('address', 'Enter the address, or remove this row.');
        first = first || row.inputs.address;
      } else if (!ADDRESS_RE.test(v.address)) {
        row.setError('address', 'Enter one plain address, like ops@example.org.');
        first = first || row.inputs.address;
      } else if (seen.has(v.address.toLowerCase())) {
        row.setError('address', 'This address is already in another row.');
        first = first || row.inputs.address;
      }
      seen.add(v.address.toLowerCase());
      sent.push({ row, entry: { address: v.address, label: v.label, min_severity: v.min_severity } });
    }
    values.recipients = sent.map((s) => s.entry);
    values.quiet_hours = quiet.input.value.trim().replace(QUIET_DASH_RE, '-');
    values.daily_digest = digest.input.value.trim();
    const quietError = quietHoursError(values.quiet_hours);
    if (quietError) flag(quiet, quietError);
    if (first) {
      // an error in a hidden server field would be invisible: they are all shown in "Other" only
      first.focus();
      return null;
    }
    return { values, sent };
  }

  /** Put a 400 from the server under the field or card it is about; false leaves it to the snackbar. */
  function placeServerError(sent) {
    return (err) => {
      if (!err || err.status !== 400) return false;
      const msg = String(err.message || '');
      let m = msg.match(/recipients\.(\d+)\.(\w+)/);
      let hit = m && sent[Number(m[1])] ? sent[Number(m[1])] : null;
      const unquote = (text) => text.trim().replace(/^['"]|['"]$/g, '');
      if (!hit && !/from_address/.test(msg) && (m = msg.match(/not an email address: (.+)$/i))) hit = sent.find(({ entry }) => entry.address === unquote(m[1]));
      if (!hit && (m = msg.match(/(\S+@\S+) is listed twice/i))) {
        const t = unquote(m[1]).toLowerCase();
        hit = sent.filter(({ entry }) => entry.address.toLowerCase() === t).pop();
      }
      if (hit) {
        hit.row.setError('address', plainError(msg));
        hit.row.focus('address');
        return true;
      }
      // "enter the email password again: the SMTP server … changed" is about the password, whatever it names
      const fieldFor = [[/password again|SMTP_PASSWORD/i, password], [/smtp_host|server|host name/i, host], [/smtp_port|port/i, port], [/from_address/i, fromAddress], [/from_name/i, fromName], [/username/i, username], [/password/i, password], [/quiet/i, quiet], [/daily_digest|digest time/i, digest]];
      for (const [re, fld] of fieldFor) {
        if (!re.test(msg) || fld.node.hidden || (serverBox.hidden && serverBox.contains(fld.node))) continue;
        fld.node.setError(plainError(msg));
        fld.input.focus();
        return true;
      }
      return false;
    };
  }

  async function save_(label) {
    const collected = collect();
    if (!collected) return null;
    const saved = await submitPatch({ patch: { notifiers: { email: collected.values } }, button: save, label, signal: ctx.signal, onError: placeServerError(collected.sent) });
    if (saved) {
      cfg = saved;
      const fresh = (saved.notifiers && saved.notifiers.email) || {};
      savedTarget = targetOf(fresh.smtp_host, Number(fresh.smtp_port), fresh.security, fresh.username);
      gmailServer = gmailServerOf(fresh);
      maskCleared = false;
      touched = false;
      password.input.value = fresh.password || '';
      paintPassword();
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

  function paintResults(r, rows) {
    const results = Array.isArray(r.results) ? r.results : [];
    let sentOk = 0;
    let tested = 0;
    for (const row of rows) {
      const v = row.values();
      const res = results.find((x) => String(x.address || '').toLowerCase() === v.address.toLowerCase());
      if (!v.address || !res) continue;
      tested += 1;
      if (res.ok) sentOk += 1;
      row.setStatus(resultLine({ label: v.label, address: v.address, ...res }), res.ok ? 'ok' : 'crit');
    }
    return [sentOk, tested];
  }

  /** "Test" on one card: save, then send the test email to that address alone. */
  async function testOne(row) {
    showNote('');
    live.textContent = '';
    if (row.isEmpty()) {
      row.setError('address', 'Enter an address first, then test.');
      row.focus('address');
      return;
    }
    row.setStatus('');
    row.test.setBusy(true);
    let sending = null;
    try {
      const saved = await save_('');
      if (!saved) return;
      // the snackbar lasts as long as the send; the card's line carries the outcome
      sending = snackbar('Saved — sending the test email…', { duration: 60000 });
      const r = await api.emailTest({ address: row.values().address }, { signal: ctx.signal });
      if (isDemo(r)) {
        row.setStatus('Demo: logged, not sent', 'muted');
        live.textContent = 'Demo: logged, not sent';
        return;
      }
      const [, tested] = paintResults(r, [row]);
      if (!tested) row.setStatus(`❌ Not sent — ${plainError(r.error) || 'the mail server did not accept it.'}`, 'crit');
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

  /** "Send test email": save, then one test email to every recipient; a line per card and a summary. */
  async function sendTestAll() {
    showNote('');
    live.textContent = '';
    list.clearStatus();
    testAll.setBusy(true);
    let sending = null;
    try {
      const saved = await save_('');
      if (!saved) return;
      sending = snackbar('Saved — sending the test email…', { duration: 60000 });
      const r = await api.emailTest(undefined, { signal: ctx.signal });
      if (isDemo(r)) {
        showNote('Demo: logged, not sent — no email leaves Sentinel while the demo runs; the message is in the events log.', 'muted');
        return;
      }
      const [sentOk, tested] = paintResults(r, list.rows());
      if (!tested) showNote(`❌ Not sent — ${plainError(r.error) || 'there is no recipient yet.'}`, 'crit');
      else if (sentOk === tested) showNote(`✅ Test email sent to ${everyone(tested, 'recipient')} — check the inbox.`, 'ok');
      else showNote(`❌ ${sentOk} of ${fmt.plural(tested, 'recipient')} got the test email — each row says why.`, 'crit');
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
