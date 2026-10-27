"""The dashboard login in the browser (LOGIN-CONTRACT, Web UI; Playwright sync API).

The demo never asks for a login, so this module starts a server of its own that is NOT a demo: a free
port, a private data directory and a config file whose ``server.login`` holds a hash made by
``dspace_sentinel.auth.hash_password``, with setup completed and every module off (only the server is
watched, so it needs no DSpace). The cases:

* the sign-in screen shows and nothing else loads (no /api/meta, no stream);
* a wrong password shows "Wrong username or password.";
* the right one lands on the app ("Keep me signed in" makes the cookie last beyond a week);
* Sign out returns to the sign-in screen and the API refuses the browser again;
* a cookie cleared mid-session shows "Your session ended. Sign in again." at the next request, and
  signing in renders the route that was open (More) again;
* a 429 disables Sign in with a countdown "Try again in … s" (answered by ``page.route``);
* More › Account changes the password: the page's own checks first, then the change, after which the
  old password stops working and the new one signs in.

A wrong password counts against the brute-force limits (5 failures per address in 15 minutes); the
module stays well under them. Like ``tests/e2e/test_onboarding.py`` (whose way of starting a server it
follows) it runs only with ``SENTINEL_E2E=1``, skips without Playwright, its Chromium or a built UI,
and drives its Chromium from a worker thread of its own. It also skips while
``dspace_sentinel.auth`` does not exist yet.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import urllib.error
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlsplit

import pytest
import yaml

from tests.e2e.test_ui import (
    APP_JS,
    BOOT_TIMEOUT_S,
    E2E_ENABLED,
    HEADLESS,
    REPO_DIR,
    UI_TIMEOUT_MS,
    PlaywrightError,
    _free_port,
    _http,
    _tail,
    sync_playwright,
)

try:
    from playwright.sync_api import expect
except ImportError:  # pragma: no cover - exercised only when the dev extra is not installed
    expect = None  # type: ignore[assignment]

auth = pytest.importorskip("dspace_sentinel.auth")

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not E2E_ENABLED, reason="set SENTINEL_E2E=1 to run the Playwright suite"),
    pytest.mark.skipif(sync_playwright is None, reason="playwright is not installed"),
]

DESKTOP = {"width": 1440, "height": 900}
USERNAME = "admin"
PASSWORD = "correct horse battery staple"  # made up, for this test server only
WRONG_PASSWORD = "not the password at all"
NEW_PASSWORD = "a different long passphrase"  # made up too

# ------------------------------------------------------------------ DOM contract
SIGN_IN_HEADING = "Sign in to DSpace Sentinel"
SIGN_IN_BUTTON = "Sign in"
REMEMBER_LABEL = "Keep me signed in for 30 days"
WRONG_TEXT = "Wrong username or password."
SESSION_ENDED = "Your session ended. Sign in again."
GATE = "#gate"
SIGN_IN_ERROR = "#signin-error"  # the server's sentence under the form
SUBMIT = "#gate form button[type=submit]"
SIGN_OUT = "#sign-out"  # the sidebar's
ACCOUNT_NAME = "#account-name"
MORE_SETTINGS = "#more-settings"
ACCOUNT_ROW = '#more-settings [data-key="account"]'
PASSWORD_CHANGED = "Password changed. Other sessions were signed out."
LOGIN_URL = re.compile(r".*/api/login(\?.*)?$")
IGNORED_CONSOLE = ("Failed to load resource", "EventSource")
"""The browser logs every 401 and 429 answer (and a stream it refused) as a console error: here they are
the point. Uncaught page errors and every other console error still fail a case."""


# ------------------------------------------------------------------ the browser, in a thread of its own


class _Chromium:
    """A headless Chromium driven from one worker thread (Playwright's sync API: one instance per thread)."""

    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="login")
        self._manager: Any = None
        self.browser: Any = None
        try:
            self._pool.submit(self._start).result()
        except BaseException:
            self._pool.shutdown(wait=False)
            raise

    def _start(self) -> None:
        assert sync_playwright is not None
        self._manager = sync_playwright()
        self.browser = self._manager.start().chromium.launch(headless=HEADLESS)

    def run(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run ``fn(*args)`` on the browser's thread; its exception (a failed assertion) is re-raised here."""
        return self._pool.submit(fn, *args).result()

    def close(self) -> None:
        def stop() -> None:
            try:
                if self.browser is not None:
                    self.browser.close()
            finally:
                if self._manager is not None:
                    self._manager.__exit__(None, None, None)

        try:
            self._pool.submit(stop).result()
        finally:
            self._pool.shutdown(wait=True)


# ------------------------------------------------------------------ fixtures


@pytest.fixture(scope="module")
def login_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A live (not demo) server with a login, setup completed and every module off; yields its URL."""
    if not APP_JS.is_file():
        pytest.skip(f"web UI not built yet ({APP_JS.relative_to(REPO_DIR)} is missing)")
    root = tmp_path_factory.mktemp("sentinel-e2e-login")
    data_dir = root / "data"
    data_dir.mkdir()
    config_path = root / "sentinel.yaml"
    config = {
        "setup": {"completed": True, "persona": "server"},
        "server": {"login": {"username": USERNAME, "password_hash": auth.hash_password(PASSWORD)}},
    }
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    config_path.chmod(0o600)
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("SENTINEL_")}
    env.update(
        {
            "SENTINEL_HOST": "127.0.0.1",
            "SENTINEL_PORT": str(port),
            "SENTINEL_DATA_DIR": str(data_dir),
            "SENTINEL_CONFIG": str(config_path),
            "SENTINEL_LOG_LEVEL": "INFO",
            "PYTHONUNBUFFERED": "1",
        }
    )
    log_path = root / "server.log"
    with log_path.open("w") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "dspace_sentinel", "serve"],
            cwd=str(REPO_DIR),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + BOOT_TIMEOUT_S
    try:
        while True:
            if proc.poll() is not None:
                raise RuntimeError(f"sentinel exited with {proc.returncode} during boot:\n{_tail(log_path)}")
            try:
                if _http(f"{base_url}/healthz", timeout=2.0) == {"ok": True}:
                    break
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
                pass
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"sentinel did not answer /healthz in {BOOT_TIMEOUT_S:.0f} s:\n{_tail(log_path)}"
                )
            time.sleep(0.25)
        session = _http(f"{base_url}/api/session")
        assert session["login_required"] is True, f"{session}\n{_tail(log_path)}"
        assert session["authenticated"] is False, session
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@pytest.fixture(scope="module")
def chromium(login_server: str) -> Iterator[_Chromium]:
    try:
        browser = _Chromium()
    except PlaywrightError as exc:
        pytest.skip(f"Playwright Chromium is not installed (run `playwright install chromium`): {exc}")
    try:
        yield browser
    finally:
        browser.close()


# ------------------------------------------------------------------ helpers (run on the browser's thread)


def _open(browser: Any, base: str) -> tuple[Any, Any, list[str], list[str]]:
    """A fresh context at ``base``: (context, page, errors, paths of the /api/ requests made)."""
    ctx = browser.new_context(viewport=DESKTOP, color_scheme="light", reduced_motion="reduce", locale="en-US")
    ctx.set_default_timeout(UI_TIMEOUT_MS)
    errors: list[str] = []
    api_paths: list[str] = []
    page = ctx.new_page()
    page.on("pageerror", lambda err: errors.append(f"pageerror: {err}"))
    page.on(
        "console",
        lambda msg: (
            errors.append(f"console.{msg.type}: {msg.text}")
            if msg.type == "error" and not any(word in msg.text for word in IGNORED_CONSOLE)
            else None
        ),
    )

    def record(request: Any) -> None:
        path = urlsplit(request.url).path
        if path.startswith("/api/"):
            api_paths.append(path)

    page.on("request", record)
    page.goto(f"{base}/", wait_until="domcontentloaded")
    return ctx, page, errors, api_paths


def _sign_in_screen(page: Any) -> None:
    """Wait for the sign-in screen: its heading shows and the app's navigation does not."""
    assert expect is not None
    expect(page.get_by_role("heading", name=SIGN_IN_HEADING)).to_be_visible()
    expect(page.get_by_role("navigation", name="Main")).to_be_hidden()


def _sign_in(page: Any, password: str = PASSWORD, *, remember: bool = False) -> None:
    """Fill the sign-in form and press Sign in."""
    gate = page.locator(GATE)
    gate.get_by_label("Username").fill(USERNAME)
    gate.get_by_label("Password", exact=True).fill(password)
    if remember:
        gate.get_by_label(REMEMBER_LABEL).check()
    gate.get_by_role("button", name=SIGN_IN_BUTTON, exact=True).click()


def _in_app(page: Any) -> None:
    """Wait for the app: the navigation shows, with the username and Sign out at the sidebar's foot."""
    assert expect is not None
    page.get_by_role("navigation", name="Main").wait_for(state="visible", timeout=UI_TIMEOUT_MS)
    expect(page.locator(ACCOUNT_NAME)).to_have_text(USERNAME)
    expect(page.locator(SIGN_OUT)).to_be_visible()


# ------------------------------------------------------------------ the cases


def _nothing_else_loads(base: str, browser: Any) -> list[str]:
    assert expect is not None
    ctx, page, errors, api_paths = _open(browser, base)
    try:
        _sign_in_screen(page)
        gate = page.locator(GATE)
        expect(gate.get_by_label("Username")).to_be_focused()
        expect(gate.get_by_label("Password", exact=True)).to_have_attribute("type", "password")
        expect(gate.get_by_label(REMEMBER_LABEL)).not_to_be_checked()
        expect(gate.get_by_role("button", name=SIGN_IN_BUTTON, exact=True)).to_be_enabled()
        expect(page.get_by_text(SESSION_ENDED)).to_have_count(0)
        page.wait_for_timeout(1000)  # room for a stray request
        assert set(api_paths) == {"/api/session"}, api_paths
    finally:
        ctx.close()
    return errors


def test_the_sign_in_screen_shows_and_nothing_else_loads(login_server: str, chromium: _Chromium) -> None:
    errors = chromium.run(_nothing_else_loads, login_server, chromium.browser)
    assert errors == [], errors


def _wrong_password(base: str, browser: Any) -> list[str]:
    assert expect is not None
    ctx, page, errors, _paths = _open(browser, base)
    try:
        _sign_in_screen(page)
        _sign_in(page, WRONG_PASSWORD)
        expect(page.locator(SIGN_IN_ERROR)).to_have_text(WRONG_TEXT)
        _sign_in_screen(page)
        # the password is emptied for the next try, the username stays
        expect(page.locator(GATE).get_by_label("Password", exact=True)).to_have_value("")
        expect(page.locator(GATE).get_by_label("Username")).to_have_value(USERNAME)
    finally:
        ctx.close()
    return errors


def test_a_wrong_password_says_so(login_server: str, chromium: _Chromium) -> None:
    errors = chromium.run(_wrong_password, login_server, chromium.browser)
    assert errors == [], errors


def _right_password(base: str, browser: Any) -> list[str]:
    assert expect is not None
    ctx, page, errors, _paths = _open(browser, base)
    try:
        _sign_in_screen(page)
        _sign_in(page, remember=True)
        _in_app(page)
        page.wait_for_url(re.compile(r"#/home$"))
        heading = page.get_by_role("main").get_by_role("heading", level=1).first
        expect(heading).to_have_text(re.compile(r"Home"))
        # "Keep me signed in for 30 days": the session cookie outlives the 7 days of a plain sign-in
        cookie = next(c for c in ctx.cookies() if c["name"] == "sentinel_session")
        assert cookie["expires"] > time.time() + 8 * 86400, cookie
    finally:
        ctx.close()
    return errors


def test_the_right_password_lands_on_the_app(login_server: str, chromium: _Chromium) -> None:
    errors = chromium.run(_right_password, login_server, chromium.browser)
    assert errors == [], errors


def _sign_out(base: str, browser: Any) -> list[str]:
    assert expect is not None
    ctx, page, errors, _paths = _open(browser, base)
    try:
        _sign_in_screen(page)
        _sign_in(page)
        _in_app(page)
        page.locator(SIGN_OUT).click()
        _sign_in_screen(page)
        expect(page.get_by_text(SESSION_ENDED)).to_have_count(0)
        # the session is gone on the server too, and the API refuses this browser again
        assert page.request.get(f"{base}/api/session").json()["authenticated"] is False
        assert page.request.get(f"{base}/api/meta").status == 401
    finally:
        ctx.close()
    return errors


def test_sign_out_returns_to_the_sign_in_screen(login_server: str, chromium: _Chromium) -> None:
    errors = chromium.run(_sign_out, login_server, chromium.browser)
    assert errors == [], errors


def _session_ends(base: str, browser: Any) -> list[str]:
    assert expect is not None
    ctx, page, errors, _paths = _open(browser, base)
    try:
        _sign_in_screen(page)
        _sign_in(page)
        _in_app(page)
        page.get_by_role("navigation", name="Main").get_by_role("link", name=re.compile(r"^More$")).click()
        page.wait_for_url(re.compile(r"#/more$"))
        expect(page.locator(MORE_SETTINGS)).to_be_visible()
        expect(page.locator(ACCOUNT_ROW)).to_be_visible()

        # the session ends behind the page's back; the next request finds out (a click on the sidebar's
        # Check now, sent from script so that it cannot wait on a button the sign-in screen hid already)
        ctx.clear_cookies()
        page.evaluate("() => document.getElementById('check-now').click()")
        _sign_in_screen(page)
        expect(page.get_by_text(SESSION_ENDED)).to_be_visible()

        _sign_in(page)
        _in_app(page)
        assert re.search(r"#/more$", page.url), page.url
        expect(page.locator(MORE_SETTINGS)).to_be_visible()
        heading = page.get_by_role("main").get_by_role("heading", level=1).first
        expect(heading).to_have_text(re.compile(r"More"))
    finally:
        ctx.close()
    return errors


def test_a_session_that_ended_says_so_and_signing_in_reloads_the_route(
    login_server: str, chromium: _Chromium
) -> None:
    errors = chromium.run(_session_ends, login_server, chromium.browser)
    assert errors == [], errors


def _countdown(base: str, browser: Any) -> list[str]:
    assert expect is not None
    ctx, page, errors, _paths = _open(browser, base)
    try:
        _sign_in_screen(page)

        def too_many(route: Any) -> None:
            route.fulfill(
                status=429,
                headers={"Retry-After": "3"},
                json={"error": "Too many attempts. Try again in 3 s.", "retry_after": 3},
            )

        page.route(LOGIN_URL, too_many)
        _sign_in(page)
        button = page.locator(SUBMIT)
        expect(button).to_be_disabled()
        expect(button).to_have_text(re.compile(r"^Try again in \d+\ss$"))
        expect(page.locator(SIGN_IN_ERROR)).to_contain_text("Too many attempts.")
        expect(button).to_be_enabled(timeout=10_000)
        expect(button).to_have_text(SIGN_IN_BUTTON)
    finally:
        ctx.close()
    return errors


def test_too_many_attempts_count_down_on_the_button(login_server: str, chromium: _Chromium) -> None:
    errors = chromium.run(_countdown, login_server, chromium.browser)
    assert errors == [], errors


def _change_password(base: str, browser: Any) -> list[str]:
    assert expect is not None
    ctx, page, errors, _paths = _open(browser, base)
    try:
        _sign_in_screen(page)
        _sign_in(page)
        _in_app(page)
        page.get_by_role("navigation", name="Main").get_by_role("link", name=re.compile(r"^More$")).click()
        page.locator(ACCOUNT_ROW).click()
        page.wait_for_url(re.compile(r"#/more/account$"))
        current = page.get_by_label("Current password")
        new = page.get_by_label("New password", exact=True)
        repeat = page.get_by_label("Repeat new password")
        change = page.get_by_role("button", name="Change password")

        # the page's own checks come first and post nothing
        current.fill(PASSWORD)
        new.fill("too short")
        repeat.fill("too short")
        change.click()
        expect(page.locator("#acct-new-error")).to_have_text("Use at least 12 characters.")
        new.fill(NEW_PASSWORD)
        repeat.fill(f"{NEW_PASSWORD}!")
        change.click()
        expect(page.locator("#acct-repeat-error")).to_have_text("The new passwords do not match.")

        repeat.fill(NEW_PASSWORD)
        change.click()
        expect(page.locator("#snackbar")).to_contain_text(PASSWORD_CHANGED)
        expect(current).to_have_value("")
        # this browser stays signed in
        assert page.request.get(f"{base}/api/session").json()["authenticated"] is True

        # the old password stops working, the new one signs in
        page.locator(SIGN_OUT).click()
        _sign_in_screen(page)
        _sign_in(page, PASSWORD)
        expect(page.locator(SIGN_IN_ERROR)).to_have_text(WRONG_TEXT)
        _sign_in(page, NEW_PASSWORD)
        _in_app(page)

        # back to the module's password
        answer = page.request.post(
            f"{base}/api/account/password", data={"current": NEW_PASSWORD, "new": PASSWORD}
        )
        assert answer.status == 200, answer.text()
    finally:
        ctx.close()
    return errors


def test_more_account_changes_the_password_and_the_old_one_stops_working(
    login_server: str, chromium: _Chromium
) -> None:
    errors = chromium.run(_change_password, login_server, chromium.browser)
    assert errors == [], errors
