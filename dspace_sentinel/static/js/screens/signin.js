/**
 * screens/signin.js — the sign-in screen (the dashboard login, server.login).
 *
 * A full page in the gate: "Sign in to DSpace Sentinel", Username, Password, the checkbox "Keep me
 * signed in for 30 days" and Sign in (POST /api/login). The server's sentence goes under the form
 * ("Wrong username or password."); a 429 disables the button with a countdown "Try again in 40 s"
 * from its `retry_after`. `params.ended` puts "Your session ended. Sign in again." above the form and
 * `params.username` fills in the last username. `ctx.finish(answer)` continues the boot. A 400 (no
 * login is set up any more) asks /api/session again and goes on when no login is required.
 */
import api from '../api.js';
import { NBSP, button, el, field, input, keepUnits } from '../ui.js';

export const SESSION_ENDED = 'Your session ended. Sign in again.';

/**
 * Render the sign-in screen.
 * @param {HTMLElement} root
 * @param {{ended?: boolean, username?: string}} params
 * @param {object} ctx — uses ctx.finish(answer), ctx.signal
 * @returns {() => void} destroy
 */
export function render(root, params, ctx) {
  const username = input({ id: 'signin-user', name: 'username', autocomplete: 'username', value: (params && params.username) || '' });
  username.setAttribute('autocapitalize', 'none');
  username.spellcheck = false;
  const password = input({ type: 'password', id: 'signin-pass', name: 'password', autocomplete: 'current-password' });
  const remember = el('input', { type: 'checkbox', id: 'signin-remember', name: 'remember' });
  const submit = button({ label: 'Sign in', variant: 'filled', type: 'submit', full: true, busyLabel: 'Signing in…' });
  const error = el('p', { class: 'field-error', id: 'signin-error', role: 'alert' });
  error.hidden = true;
  const form = el('form', { class: 'signin-form', novalidate: 'novalidate', 'aria-label': 'Sign in' },
    field({ label: 'Username', input: username }),
    field({ label: 'Password', input: password }),
    el('label', 'check', remember, el('span', { text: 'Keep me signed in for 30 days' })),
    submit,
    error,
  );
  const note = params && params.ended ? el('p', { class: 'signin-note', id: 'signin-note', role: 'status', text: SESSION_ENDED }) : null;
  root.replaceChildren(el('div', 'signin',
    el('span', { class: 'tile-icon', 'aria-hidden': 'true', text: '🛰️' }),
    el('h1', { text: 'Sign in to DSpace Sentinel', tabIndex: -1 }),
    note,
    form,
  ));
  (username.value ? password : username).focus({ preventScroll: true });

  let busy = false;
  let waitTimer = null;

  function showError(text) {
    error.textContent = text ? keepUnits(text) : '';
    error.hidden = !text;
  }

  /** A 429: the button stays off and counts down "Try again in 40 s", then reads Sign in again. */
  function wait(seconds) {
    clearInterval(waitTimer);
    let left = Math.max(1, Math.ceil(Number(seconds) || 1));
    const paint = () => submit.setLabel(`Try again in ${left}${NBSP}s`);
    submit.disabled = true;
    paint();
    waitTimer = setInterval(() => {
      left -= 1;
      if (left > 0) {
        paint();
        return;
      }
      clearInterval(waitTimer);
      waitTimer = null;
      submit.disabled = false;
      submit.setLabel('Sign in');
      showError('');
    }, 1000);
  }

  async function signIn(event) {
    event.preventDefault();
    if (busy || waitTimer) return;
    const user = username.value.trim();
    if (!user || !password.value) {
      showError(user ? 'Enter your password.' : 'Enter your username.');
      (user ? password : username).focus();
      return;
    }
    busy = true;
    showError('');
    submit.setBusy(true);
    let answer;
    try {
      answer = await api.login({ username: user, password: password.value, remember: remember.checked }, { signal: ctx.signal });
    } catch (err) {
      if (err && err.name === 'AbortError') return;
      busy = false;
      submit.setBusy(false);
      if (err && err.status === 400 && (await noLoginAnyMore())) return;
      showError(err ? err.message : 'Sign-in failed.');
      if (err && err.status === 429) wait(err.body && err.body.retry_after);
      password.value = '';
      password.focus();
      return;
    }
    await ctx.finish(answer);
  }

  /** "No login is set up on this Sentinel." — the login was removed meanwhile: go on without one. */
  async function noLoginAnyMore() {
    try {
      const session = await api.session({ signal: ctx.signal });
      if (session && session.login_required === false) {
        await ctx.finish(session);
        return true;
      }
    } catch { /* show the sign-in error */ }
    return false;
  }

  form.addEventListener('submit', signIn);
  return () => {
    clearInterval(waitTimer);
    form.removeEventListener('submit', signIn);
  };
}
