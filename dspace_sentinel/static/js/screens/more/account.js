/**
 * screens/more/account.js — #/more/account: the dashboard login (server.login).
 *
 * The route exists only while a login is set (app.js). "Signed in as admin" with Sign out (the
 * sidebar's is hidden in the icon rail and the phone bar), then Change password: Current password,
 * New password, Repeat new password. The page checks 12 characters (256 at most) and the repeat
 * before it posts POST /api/account/password; success shows the snackbar "Password changed. Other
 * sessions were signed out." and empties the fields. The server's 400s go under the field they
 * concern ("The current password is wrong." under Current password, the password rules and "The new
 * password is the same as the current one." under New password).
 */
import api from '../../api.js';
import { store } from '../../store.js';
import { button, el, snackbar, tile } from '../../ui.js';
import { formEl, group, textField } from './form.js';

export const title = 'Account';

export const PASSWORD_CHANGED = 'Password changed. Other sessions were signed out.';
const MIN_LENGTH = 12;
const MAX_LENGTH = 256;

/**
 * Render the account page.
 * @returns {() => void} destroy
 */
export function render(root, params, ctx) {
  const session = store.get('session') || {};
  const name = session.username || '';
  const signOut = button({ label: 'Sign out', variant: 'ghost', size: 'sm', onClick: () => ctx.signOut() });
  const who = tile({
    emoji: '👤',
    title: name ? `Signed in as ${name}` : 'Signed in',
    subtitle: 'Sessions end after 12 hours without use, or after 30 days with Keep me signed in.',
    actions: signOut,
  });

  const current = textField({ label: 'Current password', id: 'acct-current', type: 'password', autocomplete: 'current-password' });
  const fresh = textField({ label: 'New password', id: 'acct-new', type: 'password', autocomplete: 'new-password', help: 'At least 12 characters, and not the username.' });
  const repeat = textField({ label: 'Repeat new password', id: 'acct-repeat', type: 'password', autocomplete: 'new-password' });
  const save = button({ label: 'Change password', variant: 'filled', type: 'submit', busyLabel: 'Changing…' });
  const form = formEl({ id: 'account-form' });
  // for password managers: the login this password belongs to
  form.appendChild(el('input', { type: 'text', name: 'username', autocomplete: 'username', value: name, hidden: true, tabIndex: -1 }));
  form.append(
    group('Change password', 'Other browsers signed in with this login are signed out; this one stays signed in.', current.node, fresh.node, repeat.node),
    el('div', 'form-actions', save),
  );
  root.replaceChildren(el('div', 'form', who, form));

  const fields = [current, fresh, repeat];

  /** The client checks: a current password, 12 to 256 characters, the same new password twice. */
  function validate() {
    for (const f of fields) f.node.setError('');
    let bad = null;
    if (!current.input.value) bad = [current, 'Enter the current password.'];
    else if (fresh.input.value.length < MIN_LENGTH) bad = [fresh, 'Use at least 12 characters.'];
    else if (fresh.input.value.length > MAX_LENGTH) bad = [fresh, 'Use at most 256 characters.'];
    else if (repeat.input.value !== fresh.input.value) bad = [repeat, 'The new passwords do not match.'];
    if (!bad) return true;
    bad[0].node.setError(bad[1]);
    bad[0].input.focus();
    return false;
  }

  /** The field a refusal concerns: the current password (wrong, or too many attempts), else the new one. */
  function fieldFor(err) {
    if (!err) return null;
    if (err.status === 429 || (err.status === 400 && /current password is wrong/i.test(err.message || ''))) return current;
    return err.status === 400 ? fresh : null;
  }

  async function submit(event) {
    event.preventDefault();
    if (!validate()) return;
    save.setBusy(true);
    try {
      await api.changePassword({ current: current.input.value, new: fresh.input.value }, { signal: ctx.signal });
    } catch (err) {
      if (err && err.name === 'AbortError') return;
      // 401: the session ended; the sign-in screen is already up
      if (err && err.status === 401) return;
      const target = fieldFor(err);
      if (target) {
        target.node.setError(err.message);
        target.input.focus();
      } else {
        snackbar(`Password not changed — ${err ? err.message : 'unknown error'}`, { duration: 8000 });
      }
      return;
    } finally {
      save.setBusy(false);
    }
    for (const f of fields) f.input.value = '';
    snackbar(PASSWORD_CHANGED);
  }

  form.addEventListener('submit', submit);
  return () => form.removeEventListener('submit', submit);
}
