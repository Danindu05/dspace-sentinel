"""End-to-end smoke test of the web UI in demo mode (SPEC section 14, Playwright sync API).

A session fixture starts ``SENTINEL_DEMO=1 python -m dspace_sentinel serve`` on a free port with
a temporary data directory, waits for ``/healthz`` and drives the real browser UI:

* onboarding (choose a persona, Continue, Start monitoring),
* Home shows the Pulse band and the four server vitals,
* every tab navigates through the hash router and the URL follows,
* the "New report" panel generates an HTML report,
* the theme switch turns dark mode on and persists it,
* the PostgreSQL component detail opens from its deep link,
* More › Email keeps two added recipients across a reload, More › Telegram keeps a second bot, and
  their Test buttons say "Demo: logged, not sent",
* the wizard pre-selects Security for every option ("this server"), and More › Modules lists it
  right after Ports,
* screenshots are written to ``build/screenshots/`` (git-ignored) so a local run never touches
  the curated README images; set ``SENTINEL_E2E_SHOTS=docs/screenshots`` to refresh them:
  ``home-light, home-dark, stack, history, more, report-panel, component, more-email, more-telegram``
  at 1440x900,
  ``home-wide`` at 1920x1080 and ``home-narrow`` at 390x844. (``incident.png`` in the README is
  captured by hand during a ``SENTINEL_DEMO_FAST=1`` incident; see CONTRIBUTING.md.)

The suite runs only when ``SENTINEL_E2E=1`` is set (``SENTINEL_E2E=1 pytest tests/e2e``): its
session-scoped ``sync_playwright()`` cannot share a process with the pytest-asyncio tests, so a
plain ``pytest`` skips it. It also skips cleanly when ``playwright`` or its Chromium build is
missing, and when the web UI has not been built yet (``dspace_sentinel/static/js/app.js`` absent).

INTEGRATOR NOTE - DOM contract. Selectors are semantic (ARIA roles + visible text) and follow
SPEC section 12 / docs/DESIGN.md; if the front-end names something differently, adjust the
constants in the ``DOM contract`` block below rather than the tests:

* ``<nav aria-label="Main">`` with links named "Home", "Stack" (or "Services" when no DSpace
  module is on), "History", "More"; the active link carries ``aria-current="page"``.
* A sidebar ``<button>`` whose accessible name contains "New report".
* The hero band on Home is an element with class ``pulse``.
* The report panel is a ``<dialog>`` with a heading "Generate report" and a button
  "Generate report"; success shows a ``role="status"`` snackbar reading "Report ready".
* Theme chips are radios (or buttons) named "System", "Light", "Dark"; the chosen theme is
  reflected in ``<html data-theme>`` and persisted in ``appearance.theme``.
* Onboarding: a persona tile whose text contains "Full DSpace stack", then buttons
  "Continue" and "Start monitoring".
* More › Email (``#/more/email``) and More › Telegram (``#/more/telegram``): one ``role="group"``
  card per destination named "Recipient 1" … / "Bot 1" … with labelled inputs ("Address",
  "Label (optional)" / "Name (optional)", "Bot token", "Chat id"), radios "All alerts" /
  "Critical only" and a button whose name starts with "Test"; buttons "＋ Add recipient" /
  "＋ Add a bot" and "Save changes"; saving shows a ``role="status"`` snackbar starting "Saved".
"""

from __future__ import annotations

import copy
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

try:
    from playwright.sync_api import Browser, BrowserContext, Locator, Page, sync_playwright
    from playwright.sync_api import Error as PlaywrightError
except ImportError:  # pragma: no cover - exercised only when the dev extra is not installed
    sync_playwright = None  # type: ignore[assignment]
    PlaywrightError = Exception  # type: ignore[assignment,misc]

E2E_ENABLED = os.environ.get("SENTINEL_E2E", "").strip().lower() in {"1", "true", "yes", "on"}

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not E2E_ENABLED, reason="set SENTINEL_E2E=1 to run the Playwright suite"),
    pytest.mark.skipif(sync_playwright is None, reason="playwright is not installed"),
]

REPO_DIR = Path(__file__).resolve().parents[2]
STATIC_DIR = REPO_DIR / "dspace_sentinel" / "static"
APP_JS = STATIC_DIR / "js" / "app.js"
# Local runs and CI write to the git-ignored build/ tree; point SENTINEL_E2E_SHOTS at
# docs/screenshots (relative to the repo, or absolute) to refresh the README images on purpose.
SCREENSHOT_DIR = (REPO_DIR / os.environ.get("SENTINEL_E2E_SHOTS", "build/screenshots")).resolve()

DESKTOP = {"width": 1440, "height": 900}
WIDE = {"width": 1920, "height": 1080}
NARROW = {"width": 390, "height": 844}

BOOT_TIMEOUT_S = 90.0
UI_TIMEOUT_MS = 20_000
REPORT_TIMEOUT_S = 45.0
HEADLESS = os.environ.get("SENTINEL_E2E_HEADLESS", "1").strip().lower() not in {"0", "false", "no"}

# ------------------------------------------------------------------ DOM contract
NAV_LABEL = "Main"
TABS: dict[str, str] = {
    "home": r"^Home$",
    "stack": r"^(Stack|Services)$",
    "history": r"^History$",
    "more": r"^More$",
}
NEW_REPORT_BUTTON = r"New report"
PULSE_SELECTOR = ".pulse"
REPORT_PANEL_TITLE = r"^Generate report$"
GENERATE_BUTTON = r"^Generate report$"
REPORT_READY_TEXT = r"Report ready"
THEME_CHIP: dict[str, str] = {"system": r"^System$", "light": r"^Light$", "dark": r"^Dark$"}
PERSONA_TILE = r"Full DSpace stack"
CONTINUE_BUTTON = r"^Continue$"
START_BUTTON = r"^Start monitoring$"
SERVER_TILES = ("CPU", "Memory", "Disk", "Load")
EMAIL_ROUTE = "#/more/email"
TELEGRAM_ROUTE = "#/more/telegram"
CONNECTION_ROUTE = "#/more/connection"
ALERTS_ROUTE = "#/more/alerts"
RECIPIENT_CARD = "Recipient {n}"
BOT_CARD = "Bot {n}"
ADD_RECIPIENT_BUTTON = r"Add recipient"
ADD_BOT_BUTTON = r"Add a bot"
SAVE_BUTTON = r"^Save changes$"
SAVED_TEXT = r"^Saved"
TEST_BUTTON = r"^Test\b"
CRITICAL_ONLY = r"^Critical only$"
DEMO_NOT_SENT = "Demo: logged, not sent"
MASK = "••••••"
GMAIL_CHIP = r"^Gmail$"
OTHER_SERVER_CHIP = r"^Other SMTP server$"
EMAIL_HOST_INPUT = "#em-host"
EMAIL_USER_INPUT = "#em-user"
EMAIL_PASSWORD_INPUT = "#em-pass"
PG_PASSWORD_INPUT = "#c-pg-pass"
PASSWORD_AGAIN_HELP = r"Enter the password again"
PASSWORD_FROM_ENV_HELP = r"Set in config/sentinel\.env \(SENTINEL_SMTP_PASSWORD\)"
REPORTS_ROW_SUBTITLE = '[data-key="reports"] .tile-sub'
TELEGRAM_ROW_SUBTITLE = '[data-key="telegram"] .tile-sub'
TEST_STATUS_LINE = ".dest-status"
GMAIL_LOGIN: dict[str, Any] = {
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 587,
    "security": "starttls",
    "username": "sentinel.alerts@gmail.com",
    "password": "abcdefghijklmnop",  # made up: the demo never sends and keeps it in memory only
    "from_address": "",
}
SMTP_CLOSED = (
    "smtp.gmail.com:587 closed the connection: check the port and security "
    "(Gmail: starttls on port 587, ssl on port 465)"
)
SAVED_SECRET_NOTE = "Saved. Hidden for safety — type a new one to replace it."
ACKED_NOTE = "Reminders are off. It stays listed until the cause is fixed."
CALM_HEADLINE = "All systems healthy · 1 acknowledged"  # health.headline: every open alert acknowledged
MOOD_JS = "() => document.documentElement.dataset.mood"
META_URL = re.compile(r".*/api/meta(\?.*)?$")
CONFIG_URL = re.compile(r".*/api/config(\?.*)?$")
COLOMBO = {
    "name": "Asia/Colombo",
    "abbreviation": "+0530",
    "server_name": "Etc/UTC",
    "server_abbreviation": "UTC",
}
"""/api/meta's ``timezone``: the zone Sentinel uses for people, and the server's own zone."""
BAD_ZONE = (
    "invalid configuration: server.timezone: unknown time zone 'Mars/Base': use an IANA name such as "
    "Asia/Colombo, or leave it empty for the server's zone"
)
"""PATCH /api/config's 400 for a time zone zoneinfo does not know."""
PULSE_HEADLINE = "#pulse-headline"
STATUS_URL = re.compile(r".*/api/status(\?.*)?$")
STREAM_URL = re.compile(r".*/api/stream(\?.*)?$")
QUIET_EQUAL_ERROR = "Quiet hours must start and end at different times."  # config.QUIET_HOURS_EQUAL_ERROR
DISCOVER_URL = re.compile(r".*/api/actions/discover(\?.*)?$")
SECURITY_EXPLAINER = "Open ports, firewall, SSH, updates and software support: read-only."
PERSONAS = ("Just the server", "Frontend only", "Backend only", "Full DSpace stack")
"""The wizard's options, ``PERSONA_TILE`` last: the test that walks through them finishes setup with it."""


# ------------------------------------------------------------------ helpers


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _http(url: str, method: str = "GET", body: dict[str, Any] | None = None, timeout: float = 10.0) -> Any:
    """Tiny JSON client for the API (no auth in demo mode)."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local test server
        raw = resp.read()
        ctype = resp.headers.get("content-type", "")
    return json.loads(raw) if "json" in ctype else raw


def _wait_until(predicate: Any, timeout_s: float, interval_s: float = 0.5, what: str = "condition") -> Any:
    deadline = time.monotonic() + timeout_s
    last: Any = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval_s)
    raise AssertionError(f"timed out after {timeout_s:.0f} s waiting for {what}")


def _tail(path: Path, lines: int = 40) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return "(no log)"


def _first_visible(*locators: Locator, timeout_ms: int = UI_TIMEOUT_MS, scan: int = 8) -> Locator:
    """Return the first visible match among ``locators`` (tries them in turn, sharing the budget).

    The first DOM match of a text locator is often a visually hidden label, so the first ``scan``
    matches of every locator are inspected rather than only ``.first``.
    """
    deadline = time.monotonic() + timeout_ms / 1000.0
    while True:
        for loc in locators:
            try:
                for index in range(min(loc.count(), scan)):
                    candidate = loc.nth(index)
                    if candidate.is_visible():
                        return candidate
            except PlaywrightError:
                continue
        if time.monotonic() > deadline:
            descriptions = ", ".join(str(loc) for loc in locators)
            raise AssertionError(f"none of these became visible within {timeout_ms} ms: {descriptions}")
        time.sleep(0.1)


def _words(text: str) -> str:
    """Letters and spaces only, so ``"📚Stack"`` compares as ``"Stack"``."""
    return re.sub(r"[^A-Za-z ]", "", text).strip()


def _nav(page: Page) -> Locator:
    return page.get_by_role("navigation", name=NAV_LABEL)


def _tab_link(page: Page, tab: str) -> Locator:
    return _nav(page).get_by_role("link", name=re.compile(TABS[tab]))


def _theme_chip(page: Page, theme: str) -> Locator:
    pattern = re.compile(THEME_CHIP[theme])
    return _first_visible(
        page.get_by_role("radio", name=pattern),
        page.get_by_role("button", name=pattern),
        page.get_by_label(pattern),
    )


def _pulse(page: Page) -> Locator:
    pulse = page.locator(PULSE_SELECTOR).first
    pulse.wait_for(state="visible", timeout=UI_TIMEOUT_MS)
    # The headline is filled from /api/status or the first SSE frame.
    page.wait_for_function(
        "sel => { const el = document.querySelector(sel); const t = el && el.textContent.trim();"
        " return !!t && !/connecting|loading/i.test(t); }",
        arg=PULSE_SELECTOR,
        timeout=UI_TIMEOUT_MS,
    )
    return pulse


def _onboarding_visible(page: Page, timeout_ms: int = 4000) -> bool:
    try:
        page.get_by_text(re.compile(PERSONA_TILE)).first.wait_for(state="visible", timeout=timeout_ms)
        return True
    except PlaywrightError:
        return False


def _complete_onboarding(page: Page) -> None:
    """Step 1: pick the full-stack persona and continue. Step 2: start monitoring."""
    persona = _first_visible(
        page.get_by_role("button", name=re.compile(PERSONA_TILE)),
        page.get_by_role("radio", name=re.compile(PERSONA_TILE)),
        page.get_by_text(re.compile(PERSONA_TILE)),
    )
    persona.click()
    page.get_by_role("button", name=re.compile(CONTINUE_BUTTON)).first.click()
    start = page.get_by_role("button", name=re.compile(START_BUTTON)).first
    start.wait_for(state="visible", timeout=UI_TIMEOUT_MS)
    start.click()
    _nav(page).wait_for(state="visible", timeout=UI_TIMEOUT_MS)


def _ensure_home(page: Page, base_url: str) -> None:
    """Open Home, completing onboarding first if the gate is showing."""
    page.goto(f"{base_url}/#/home", wait_until="domcontentloaded")
    if _onboarding_visible(page):
        _complete_onboarding(page)
    _nav(page).wait_for(state="visible", timeout=UI_TIMEOUT_MS)
    if not re.search(r"#/home(\b|$)", page.url):
        _tab_link(page, "home").click()
    _pulse(page)


def _screenshot(page: Page, name: str) -> Path:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SCREENSHOT_DIR / f"{name}.png"
    page.wait_for_timeout(300)  # let fonts and the last SSE frame settle
    page.screenshot(path=str(path), full_page=False, animations="disabled")
    assert path.stat().st_size > 0
    return path


def _quiet(status: dict[str, Any]) -> dict[str, Any]:
    """The demo's status with nothing to report: every component and server metric healthy, no alert."""
    status = copy.deepcopy(status)
    server = status.get("server") or {}
    server["per_metric"] = {key: "ok" for key in server.get("per_metric") or {}}
    for comp in status.get("components") or []:
        comp.update(status="ok", reason=None)
    status.update(mood="ok", headline="All systems healthy", alerts_open=[])
    return status


def _serve_home(page: Page, base_url: str, change: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
    """Home sees the demo's status as ``change`` leaves a quiet copy of it, from /api/status and from a
    one-frame SSE stream (the page is reloaded, so the stream is asked again). Returns the undo."""

    def changed(status: dict[str, Any]) -> dict[str, Any]:
        status = _quiet(status)
        change(status)
        return status

    # A request still being answered when undo() unroutes the handlers goes on without them (or is
    # cancelled by the reload): its late fulfil fails with "Route is already handled", which is no error.
    def status_handler(route: Any) -> None:
        try:
            response = route.fetch()
            route.fulfill(response=response, json=changed(response.json()))
        except PlaywrightError:
            return

    def stream_handler(route: Any) -> None:
        frame = json.dumps(changed(_http(f"{base_url}/api/status")))
        try:
            route.fulfill(
                status=200,
                headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
                body=f"retry: 600000\nevent: status\ndata: {frame}\n\n",
            )
        except PlaywrightError:
            return

    page.route(STATUS_URL, status_handler)
    page.route(STREAM_URL, stream_handler)
    page.reload(wait_until="domcontentloaded")

    def undo() -> None:
        page.unroute(STATUS_URL, status_handler)
        page.unroute(STREAM_URL, stream_handler)
        page.reload(wait_until="domcontentloaded")

    return undo


def _text_is(page: Page, selector: str, text: str) -> None:
    """Wait until the element ``selector`` reads ``text`` (no-break spaces read as spaces)."""
    page.wait_for_function(
        "([sel, text]) => (document.querySelector(sel)?.textContent || '').replace(/\\u00a0/g, ' ') === text",
        arg=[selector, text],
        timeout=UI_TIMEOUT_MS,
    )


# ------------------------------------------------------------------ fixtures


@pytest.fixture(scope="session")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Boot the demo server on a free port with a private data dir; yield its base URL."""
    root = tmp_path_factory.mktemp("sentinel-e2e")
    data_dir = root / "data"
    data_dir.mkdir()
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("SENTINEL_")}
    env.update(
        {
            "SENTINEL_DEMO": "1",
            "SENTINEL_HOST": "127.0.0.1",
            "SENTINEL_PORT": str(port),
            "SENTINEL_DATA_DIR": str(data_dir),
            "SENTINEL_CONFIG": str(root / "sentinel.yaml"),
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
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@pytest.fixture(scope="session")
def ui_available() -> None:
    """Skip the browser tests while the web UI has not been built."""
    if not APP_JS.is_file():
        pytest.skip(f"web UI not built yet ({APP_JS.relative_to(REPO_DIR)} is missing)")


@pytest.fixture(scope="session")
def browser(ui_available: None) -> Iterator[Browser]:
    """A headless Chromium; skips when Playwright's browser build is not installed."""
    assert sync_playwright is not None
    manager = sync_playwright()
    playwright = manager.start()
    try:
        try:
            chromium = playwright.chromium.launch(headless=HEADLESS)
        except PlaywrightError as exc:
            pytest.skip(f"Playwright Chromium is not installed (run `playwright install chromium`): {exc}")
        try:
            yield chromium
        finally:
            chromium.close()
    finally:
        manager.__exit__(None, None, None)


@pytest.fixture(scope="session")
def context(browser: Browser) -> Iterator[BrowserContext]:
    """One 1440x900 desktop context shared by the ordered smoke tests."""
    ctx = browser.new_context(
        viewport=DESKTOP,
        color_scheme="light",
        reduced_motion="reduce",
        locale="en-US",
        device_scale_factor=1,
    )
    ctx.set_default_timeout(UI_TIMEOUT_MS)
    try:
        yield ctx
    finally:
        ctx.close()


@pytest.fixture(scope="session")
def page_errors() -> list[str]:
    return []


@pytest.fixture(scope="session")
def page(context: BrowserContext, page_errors: list[str]) -> Iterator[Page]:
    pg = context.new_page()
    pg.on("pageerror", lambda err: page_errors.append(str(err)))
    try:
        yield pg
    finally:
        pg.close()


# ------------------------------------------------------------------ tests


def test_server_boots_and_serves_the_ui(server: str) -> None:
    """The fixture works even without a browser: healthz, meta and the index page answer."""
    assert _http(f"{server}/healthz") == {"ok": True}
    meta = _http(f"{server}/api/meta")
    assert meta["demo"] is True
    assert meta["hostname"] == "demo-dspace"
    assert set(meta["modules"]) >= {"postgres", "solr", "backend", "frontend"}
    index = _http(f"{server}/")
    assert b"DSpace Sentinel" in index


def test_onboarding_flow(server: str, page: Page) -> None:
    """Persona → Continue → Start monitoring completes setup and lands on Home."""
    # Force the gate even if another test (or a re-run) already finished onboarding.
    _http(f"{server}/api/config", "PATCH", {"setup": {"completed": False, "persona": ""}})
    assert _http(f"{server}/api/meta")["setup_completed"] is False

    page.goto(f"{server}/", wait_until="domcontentloaded")
    assert _onboarding_visible(page, timeout_ms=UI_TIMEOUT_MS), "onboarding gate did not appear"
    _complete_onboarding(page)

    def _setup_done() -> Any:
        meta = _http(f"{server}/api/meta")
        return meta if meta["setup_completed"] else None

    meta = _wait_until(_setup_done, timeout_s=15, what="setup.completed to become true")
    assert meta["persona"] == "full"
    assert _tab_link(page, "home").is_visible()
    assert re.search(r"#/home(\b|$)", page.url), page.url


def test_home_shows_pulse_band_and_server_tiles(server: str, page: Page) -> None:
    _ensure_home(page, server)
    pulse = _pulse(page)
    headline = pulse.inner_text()
    assert re.search(r"healthy|warning|critical|check", headline, re.I), headline
    assert "demo-dspace" in headline
    main = page.get_by_role("main")
    for label in SERVER_TILES:
        pattern = re.compile(rf"^\s*{label}\b")
        _first_visible(
            main.get_by_role("link", name=pattern),
            main.get_by_role("button", name=pattern),
            main.get_by_text(re.compile(rf"^{label}$")),
        )
    # In demo mode every module is on, so the stack list renders one tile per component.
    postgres = re.compile(r"PostgreSQL")
    _first_visible(
        main.get_by_role("link", name=postgres),
        main.get_by_role("button", name=postgres),
        main.get_by_text(postgres),
    )
    _screenshot(page, "home-light")


@pytest.mark.parametrize("tab", ["stack", "history", "more", "home"])
def test_tabs_navigate_via_hash_and_update_url(server: str, page: Page, tab: str) -> None:
    _ensure_home(page, server)
    link = _tab_link(page, tab)
    link.click()
    page.wait_for_url(re.compile(rf"#/{tab}(\b|$)"), timeout=UI_TIMEOUT_MS)
    page.wait_for_function(
        "([label, name]) => { const nav = document.querySelector(`nav[aria-label='${label}']`);"
        " if (!nav) return false; const cur = nav.querySelector(\"a[aria-current='page']\");"
        " if (!cur) return false; const words = cur.textContent.replace(/[^A-Za-z ]/g, '').trim();"
        " return new RegExp(name).test(words); }",
        arg=[NAV_LABEL, TABS[tab]],
        timeout=UI_TIMEOUT_MS,
    )
    heading = page.get_by_role("main").get_by_role("heading", level=1).first
    heading.wait_for(state="visible", timeout=UI_TIMEOUT_MS)
    assert re.search(TABS[tab], _words(heading.inner_text())), heading.inner_text()
    if tab != "home":
        _screenshot(page, tab)
    # Deep links work too: reloading on the hash keeps the tab.
    page.reload(wait_until="domcontentloaded")
    _nav(page).wait_for(state="visible", timeout=UI_TIMEOUT_MS)
    assert re.search(rf"#/{tab}(\b|$)", page.url), page.url


def test_component_detail_opens_from_its_deep_link(server: str, page: Page) -> None:
    _ensure_home(page, server)
    page.goto(f"{server}/#/stack/postgres", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => /PostgreSQL/.test(document.querySelector('main h1')?.textContent || '')",
        timeout=UI_TIMEOUT_MS,
    )
    # The ladder lists the database rungs and ends with the real query. Scope to the pushed
    # sub-view: the Stack tab underneath keeps its own (hidden, inert) PostgreSQL ladder.
    sub_view = page.locator(".view--sub").last
    sub_view.get_by_text(re.compile(r"^SELECT 1$")).first.wait_for(state="visible", timeout=UI_TIMEOUT_MS)
    _screenshot(page, "component")


def test_report_panel_generates_html_report(server: str, page: Page) -> None:
    _ensure_home(page, server)
    before = {r["id"] for r in _http(f"{server}/api/reports")}

    page.get_by_role("button", name=re.compile(NEW_REPORT_BUTTON)).first.click()
    dialog = page.get_by_role("dialog").filter(
        has=page.get_by_role("heading", name=re.compile(REPORT_PANEL_TITLE))
    )
    dialog.first.wait_for(state="visible", timeout=UI_TIMEOUT_MS)
    assert "panel=report" in page.url, page.url
    _screenshot(page, "report-panel")

    generate = dialog.first.get_by_role("button", name=re.compile(GENERATE_BUTTON))
    if generate.count() == 0:
        pytest.fail(
            "the report panel has no 'Generate report' button (panel body: "
            f"{_words(dialog.first.inner_text())[:160]!r})"
        )
    generate.first.click()

    report = _wait_until(
        lambda: next((r for r in _http(f"{server}/api/reports") if r["id"] not in before), None),
        timeout_s=REPORT_TIMEOUT_S,
        what="a new report to appear in /api/reports",
    )
    assert report["fmt"] == "html"
    assert report["size"] > 0
    html = _http(f"{server}/api/reports/{report['id']}")
    assert isinstance(html, bytes) and b"<html" in html.lower()

    snackbar = page.get_by_role("status").filter(has_text=re.compile(REPORT_READY_TEXT))
    snackbar.first.wait_for(state="visible", timeout=UI_TIMEOUT_MS)
    dialog.first.wait_for(state="hidden", timeout=UI_TIMEOUT_MS)
    assert "panel=report" not in page.url, page.url


def test_dark_theme_toggle_persists(server: str, page: Page) -> None:
    _ensure_home(page, server)
    _theme_chip(page, "dark").click()
    page.wait_for_function(
        "() => document.documentElement.getAttribute('data-theme') === 'dark'", timeout=UI_TIMEOUT_MS
    )
    background = page.evaluate("() => getComputedStyle(document.body).backgroundColor")
    rgb = [int(v) for v in re.findall(r"\d+", background)[:3]]
    assert rgb and sum(rgb) / 3 < 80, f"body background is not dark: {background}"

    def _theme_saved() -> Any:
        cfg = _http(f"{server}/api/config")
        return cfg if cfg["appearance"]["theme"] == "dark" else None

    cfg = _wait_until(_theme_saved, timeout_s=15, what="appearance.theme to be saved as dark")
    assert cfg["appearance"]["theme"] == "dark"
    _pulse(page)
    # The previous test's "Report ready" snackbar stays for 6 s; keep it out of the capture.
    page.wait_for_function("() => !document.querySelector('.snackbar')", timeout=UI_TIMEOUT_MS)
    _screenshot(page, "home-dark")

    _theme_chip(page, "light").click()
    page.wait_for_function(
        "() => document.documentElement.getAttribute('data-theme') === 'light'", timeout=UI_TIMEOUT_MS
    )


def _open_settings(page: Page, base_url: str, route: str) -> Locator:
    """Open a More sub-view by its deep link; returns ``<main>`` once the form's Save button shows."""
    _ensure_home(page, base_url)
    page.goto(f"{base_url}/{route}", wait_until="domcontentloaded")
    main = page.get_by_role("main")
    main.get_by_role("button", name=re.compile(SAVE_BUTTON)).wait_for(state="visible", timeout=UI_TIMEOUT_MS)
    return main


def _save(page: Page, main: Locator) -> None:
    main.get_by_role("button", name=re.compile(SAVE_BUTTON)).click()
    saved = page.get_by_role("status").filter(has_text=re.compile(SAVED_TEXT))
    saved.first.wait_for(state="visible", timeout=UI_TIMEOUT_MS)


def test_email_recipients_survive_a_reload(server: str, page: Page) -> None:
    """More › Email: add two recipients, save, reload — both are still there, and in the API."""
    main = _open_settings(page, server, EMAIL_ROUTE)
    first = main.get_by_role("group", name=RECIPIENT_CARD.format(n=1), exact=True)
    first.get_by_label("Address").fill("ops@example.org")
    first.get_by_label("Label (optional)").fill("Ops team")
    main.get_by_role("button", name=re.compile(ADD_RECIPIENT_BUTTON)).click()
    second = main.get_by_role("group", name=RECIPIENT_CARD.format(n=2), exact=True)
    second.get_by_label("Address").fill("dean@example.org")
    second.get_by_role("radio", name=re.compile(CRITICAL_ONLY)).click()
    _save(page, main)

    recipients = _http(f"{server}/api/config")["notifiers"]["email"]["recipients"]
    assert [(r["address"], r["label"], r["min_severity"]) for r in recipients] == [
        ("ops@example.org", "Ops team", "warning"),
        ("dean@example.org", "", "critical"),
    ]

    page.reload(wait_until="domcontentloaded")
    main = page.get_by_role("main")
    first = main.get_by_role("group", name=RECIPIENT_CARD.format(n=1), exact=True)
    second = main.get_by_role("group", name=RECIPIENT_CARD.format(n=2), exact=True)
    second.wait_for(state="visible", timeout=UI_TIMEOUT_MS)
    assert first.get_by_label("Address").input_value() == "ops@example.org"
    assert first.get_by_label("Label (optional)").input_value() == "Ops team"
    assert second.get_by_label("Address").input_value() == "dean@example.org"
    assert second.get_by_role("radio", name=re.compile(CRITICAL_ONLY)).get_attribute("aria-checked") == "true"

    # the demo never sends anything: the card's Test says so instead of contacting a mail server
    second.get_by_role("button", name=re.compile(TEST_BUTTON)).click()
    second.get_by_text(DEMO_NOT_SENT).wait_for(state="visible", timeout=UI_TIMEOUT_MS)
    _screenshot(page, "more-email")


def test_email_password_is_asked_again_for_another_server(server: str, page: Page) -> None:
    """R1#1: the saved app password only goes to the server and login it was saved for. Changing
    the provider, the server or the login empties the field and says why; going back to the saved
    server brings the saved password (the mask) back."""
    login = {
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "security": "starttls",
        "username": "sentinel.alerts@gmail.com",
        "password": "abcdefghijklmnop",  # the demo keeps it in memory only
    }
    _http(f"{server}/api/config", "PATCH", {"notifiers": {"email": login}})
    main = _open_settings(page, server, EMAIL_ROUTE)
    password = main.locator(EMAIL_PASSWORD_INPUT)
    assert password.input_value() == MASK

    main.get_by_role("radio", name=re.compile(OTHER_SERVER_CHIP)).click()
    assert password.input_value() == ""
    main.get_by_text(re.compile(PASSWORD_AGAIN_HELP)).first.wait_for(state="visible", timeout=UI_TIMEOUT_MS)
    main.locator(EMAIL_HOST_INPUT).fill("mail.example.org")
    assert password.input_value() == ""
    main.get_by_role("radio", name=re.compile(GMAIL_CHIP)).click()
    assert password.input_value() == MASK  # the saved server again: the saved password again

    username = main.locator(EMAIL_USER_INPUT)
    username.fill("someone.else@gmail.com")
    assert password.input_value() == ""
    username.fill("sentinel.alerts@gmail.com")
    assert password.input_value() == MASK

    # whatever the page shows, the API refuses the mask for another server
    body = json.dumps({"notifiers": {"email": {**login, "smtp_host": "mail.example.org", "password": MASK}}})
    req = urllib.request.Request(f"{server}/api/config", data=body.encode(), method="PATCH")
    req.add_header("Content-Type", "application/json")
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(req, timeout=10)  # noqa: S310 - local test server
    assert refused.value.code == 400
    assert "enter the email password again" in refused.value.read().decode()


def test_email_password_from_the_environment_is_read_only(server: str, page: Page) -> None:
    """R1#6: with SENTINEL_SMTP_PASSWORD set (simulated: the API's password_from_env flag), the
    field is read-only, says where the password lives, and a save never sends a password."""
    patches: list[dict[str, Any]] = []
    login = {
        "smtp_host": "smtp.gmail.com",
        "username": "sentinel.alerts@gmail.com",
        "password": "abcdefghijklmnop",
    }
    _http(f"{server}/api/config", "PATCH", {"notifiers": {"email": login}})

    def from_env(route: Any) -> None:
        if route.request.method == "PATCH":
            patches.append(route.request.post_data_json)
        response = route.fetch()
        body = response.json()
        if isinstance(body, dict) and isinstance(body.get("notifiers"), dict):
            body["notifiers"]["email"]["password_from_env"] = True
        route.fulfill(response=response, json=body)

    page.route("**/api/config", from_env)
    try:
        main = _open_settings(page, server, EMAIL_ROUTE)
        password = main.locator(EMAIL_PASSWORD_INPUT)
        assert password.input_value() == MASK and not password.is_editable()
        # email-password#1: a keystroke leaves it as it is (the first key used to empty it) ...
        password.focus()
        page.keyboard.press("a")
        assert password.input_value() == MASK
        # ... and it looks locked, unlike the login next to it
        background = "el => getComputedStyle(el).backgroundColor"
        assert password.evaluate(background) != main.locator(EMAIL_USER_INPUT).evaluate(background)
        # T9: "type a new one to replace it" is not said where nothing can be typed
        assert not main.get_by_text(SAVED_SECRET_NOTE).is_visible()
        main.get_by_text(re.compile(PASSWORD_FROM_ENV_HELP)).first.wait_for(
            state="visible", timeout=UI_TIMEOUT_MS
        )
        main.locator("#em-from-name").fill("Repository monitor")
        _save(page, main)
        assert patches and all("password" not in p["notifiers"]["email"] for p in patches), patches
        # another login would need the password set for it, which lives in the files
        main.locator(EMAIL_USER_INPUT).fill("someone.else@gmail.com")
        assert password.input_value() == MASK
        main.get_by_text(re.compile(r"config/sentinel\.yaml")).first.wait_for(
            state="visible", timeout=UI_TIMEOUT_MS
        )
    finally:
        page.unroute("**/api/config", from_env)


def test_quiet_hours_pasted_with_an_en_dash_are_saved(server: str, page: Page) -> None:
    """R1#5: "23:00 – 07:00" (as pasted from the docs) is saved as the window the notifiers read."""
    for route, field, channel in (
        (EMAIL_ROUTE, "#em-quiet", "email"),
        (TELEGRAM_ROUTE, "#tg-quiet", "telegram"),
    ):
        main = _open_settings(page, server, route)
        main.locator(field).fill("23:00 – 07:00")
        _save(page, main)
        assert _http(f"{server}/api/config")["notifiers"][channel]["quiet_hours"] == "23:00-07:00"
        page.get_by_role("status").filter(has_text=re.compile(SAVED_TEXT)).first.wait_for(state="hidden")


def test_gmail_saves_no_from_address(server: str, page: Page) -> None:
    """R1#extra4: Gmail sends as the account that logs in, so switching from "Other SMTP server"
    to Gmail drops the (now hidden) From address instead of keeping a stale one."""
    relay = {
        "smtp_host": "mail.example.org",
        "smtp_port": 587,
        "security": "starttls",
        "username": "relay-user",
        "password": "relay-password-123",
        "from_address": "noreply@example.org",
    }
    _http(f"{server}/api/config", "PATCH", {"notifiers": {"email": {**relay, "enabled": False}}})
    main = _open_settings(page, server, EMAIL_ROUTE)
    assert main.locator("#em-from").input_value() == "noreply@example.org"
    main.get_by_role("radio", name=re.compile(GMAIL_CHIP)).click()
    main.locator(EMAIL_USER_INPUT).fill("sentinel.alerts@gmail.com")
    main.locator(EMAIL_PASSWORD_INPUT).fill("abcd efgh ijkl mnop")
    _save(page, main)
    email = _http(f"{server}/api/config")["notifiers"]["email"]
    assert (email["smtp_host"], email["username"], email["from_address"]) == (
        "smtp.gmail.com",
        "sentinel.alerts@gmail.com",
        "",
    )


def test_more_says_only_what_the_weekly_report_really_does(server: str, page: Page) -> None:
    """R1#extra5: the Reports row under More › Settings named channels that were switched off."""
    weekly = {"enabled": True, "weekday": 1, "hour": 8, "send_telegram": True, "send_email": True}
    _http(
        f"{server}/api/config",
        "PATCH",
        {
            "reports": {"weekly": weekly},
            "notifiers": {"telegram": {"enabled": False}, "email": {"enabled": False}},
        },
    )
    _ensure_home(page, server)
    page.goto(f"{server}/#/more", wait_until="domcontentloaded")
    sub = page.locator(REPORTS_ROW_SUBTITLE)
    sub.filter(has_text="Weekly report every Monday at 08:00").wait_for(
        state="visible", timeout=UI_TIMEOUT_MS
    )
    page.wait_for_timeout(500)  # both answers are in
    assert sub.inner_text() == "Weekly report every Monday at 08:00"
    # email on and set up: now it really goes by email (Telegram is still off)
    login = {
        "enabled": True,
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "security": "starttls",
        "username": "sentinel.alerts@gmail.com",
        "password": "abcdefghijklmnop",
        "from_address": "",
        "recipients": [{"address": "ops@example.org"}],
    }
    _http(f"{server}/api/config", "PATCH", {"notifiers": {"email": login}})
    page.reload(wait_until="domcontentloaded")
    sub = page.locator(REPORTS_ROW_SUBTITLE)
    sub.filter(has_text="sent by email").wait_for(state="visible", timeout=UI_TIMEOUT_MS)
    assert sub.inner_text() == "Weekly report every Monday at 08:00 · sent by email"
    _http(f"{server}/api/config", "PATCH", {"reports": {"weekly": {"enabled": False}}})


def test_telegram_keeps_a_second_bot(server: str, page: Page) -> None:
    """More › Telegram: one bot per person — a second bot is saved, survives a reload, token masked."""
    before = _http(f"{server}/api/config")["notifiers"]["telegram"]["chats"]
    assert [c for c in before if not c.get("from_env")] == [], f"the demo starts without bots: {before}"
    main = _open_settings(page, server, TELEGRAM_ROUTE)
    first = main.get_by_role("group", name=BOT_CARD.format(n=1), exact=True)
    first.get_by_label("Name (optional)").fill("Alex")
    first.get_by_label("Bot token").fill("123456789:" + "A" * 35)  # fake: the demo never calls Telegram
    first.get_by_label("Chat id").fill("111111111")
    main.get_by_role("button", name=re.compile(ADD_BOT_BUTTON)).click()
    second = main.get_by_role("group", name=BOT_CARD.format(n=2), exact=True)
    second.get_by_label("Name (optional)").fill("Sam")
    second.get_by_label("Bot token").fill("987654321:" + "B" * 35)
    second.get_by_label("Chat id").fill("-1002222222")
    second.get_by_role("radio", name=re.compile(CRITICAL_ONLY)).click()
    _save(page, main)

    chats = [
        c for c in _http(f"{server}/api/config")["notifiers"]["telegram"]["chats"] if not c.get("from_env")
    ]
    assert [(c["label"], c["chat_id"], c["min_severity"]) for c in chats] == [
        ("Alex", "111111111", "warning"),
        ("Sam", "-1002222222", "critical"),
    ]
    assert all(c["id"] and c["bot_token"] == MASK for c in chats), "tokens must come back masked, with ids"

    page.reload(wait_until="domcontentloaded")
    main = page.get_by_role("main")
    second = main.get_by_role("group", name=BOT_CARD.format(n=2), exact=True)
    second.wait_for(state="visible", timeout=UI_TIMEOUT_MS)
    assert second.get_by_label("Name (optional)").input_value() == "Sam"
    assert second.get_by_label("Chat id").input_value() == "-1002222222"
    assert second.get_by_label("Bot token").input_value() == MASK

    second.get_by_role("button", name=re.compile(TEST_BUTTON)).click()
    second.get_by_text(DEMO_NOT_SENT).wait_for(state="visible", timeout=UI_TIMEOUT_MS)
    _screenshot(page, "more-telegram")


def test_email_test_line_keeps_the_mail_server_name(server: str, page: Page) -> None:
    """T10: a test outcome that starts with the mail server and its port keeps both; the line read
    "587 closed the connection: …". The demo never sends, so a real server's answer is given here."""
    recipients = [{"address": "ops@example.org", "label": "Ops team"}]
    _http(
        f"{server}/api/config",
        "PATCH",
        {"notifiers": {"email": {**GMAIL_LOGIN, "enabled": False, "recipients": recipients}}},
    )

    def closed(route: Any) -> None:
        result = {"address": "ops@example.org", "label": "Ops team", "ok": False, "error": SMTP_CLOSED}
        route.fulfill(json={"ok": False, "error": SMTP_CLOSED, "results": [result]})

    page.route("**/api/actions/email-test", closed)
    try:
        main = _open_settings(page, server, EMAIL_ROUTE)
        card = main.get_by_role("group", name=RECIPIENT_CARD.format(n=1), exact=True)
        card.get_by_role("button", name=re.compile(TEST_BUTTON)).click()
        card.get_by_text(SMTP_CLOSED).wait_for(state="visible", timeout=UI_TIMEOUT_MS)
        assert card.locator(TEST_STATUS_LINE).inner_text() == f"❌ Ops team · {SMTP_CLOSED}"
    finally:
        page.unroute("**/api/actions/email-test", closed)


def test_a_saved_secret_says_why_it_shows_dots(server: str, page: Page) -> None:
    """T9: a saved app password or bot token shows as dots, read as "it did not save my token"; the
    line under it says it is saved and hidden, until a new one is typed."""
    bot = {"label": "Alex", "bot_token": "123456789:" + "A" * 35, "chat_id": "111111111"}  # made up
    notifiers = {"email": GMAIL_LOGIN, "telegram": {"chats": [bot]}}
    _http(f"{server}/api/config", "PATCH", {"notifiers": notifiers})

    main = _open_settings(page, server, EMAIL_ROUTE)
    password = main.locator(EMAIL_PASSWORD_INPUT)
    assert password.input_value() == MASK
    note = main.get_by_text(SAVED_SECRET_NOTE)
    note.wait_for(state="visible", timeout=UI_TIMEOUT_MS)
    password.press("x")  # typing replaces the saved one: the line goes
    note.wait_for(state="hidden", timeout=UI_TIMEOUT_MS)

    main = _open_settings(page, server, TELEGRAM_ROUTE)
    card = main.get_by_role("group", name=BOT_CARD.format(n=1), exact=True)
    token = card.get_by_label("Bot token")
    assert token.input_value() == MASK
    note = card.get_by_text(SAVED_SECRET_NOTE)
    note.wait_for(state="visible", timeout=UI_TIMEOUT_MS)
    token.fill("987654321:" + "B" * 35)
    note.wait_for(state="hidden", timeout=UI_TIMEOUT_MS)


def test_gmail_on_another_port_keeps_its_password(server: str, page: Page) -> None:
    """email-password#2: Gmail saved on a port other than 587 or 465 (by hand, by Ansible) made the
    page count as "moved to another server" on load: the saved password was emptied, and saving any
    other change deleted it and rewrote the port. The page keeps the saved port and security now."""
    login = {**GMAIL_LOGIN, "smtp_port": 25, "enabled": False}
    _http(f"{server}/api/config", "PATCH", {"notifiers": {"email": login}})
    patches: list[dict[str, Any]] = []

    def record(route: Any) -> None:
        if route.request.method == "PATCH":
            patches.append(route.request.post_data_json)
        route.continue_()

    page.route("**/api/config", record)
    try:
        main = _open_settings(page, server, EMAIL_ROUTE)
        assert main.locator(EMAIL_PASSWORD_INPUT).input_value() == MASK
        main.get_by_text("smtp.gmail.com, port 25, STARTTLS").wait_for(state="visible", timeout=UI_TIMEOUT_MS)
        assert not main.get_by_text(re.compile(PASSWORD_AGAIN_HELP)).is_visible()
        main.locator("#em-from-name").fill("Repository monitor")
        _save(page, main)
        sent = patches[-1]["notifiers"]["email"]
        assert (sent["smtp_port"], sent["security"], sent["password"]) == (25, "starttls", MASK)
        saved = _http(f"{server}/api/config")["notifiers"]["email"]
        assert (saved["smtp_host"], saved["smtp_port"], saved["password"]) == ("smtp.gmail.com", 25, MASK)
    finally:
        page.unroute("**/api/config", record)
        _http(f"{server}/api/config", "PATCH", {"notifiers": {"email": GMAIL_LOGIN}})  # 587 again


def test_quiet_hours_that_start_and_end_together_are_refused(server: str, page: Page) -> None:
    """notify-validation#4, the forms: "23:00-23:00" would never be quiet. Both forms say so under the
    field, in the API's words, and send nothing."""
    patches: list[dict[str, Any]] = []

    def record(route: Any) -> None:
        if route.request.method == "PATCH":
            patches.append(route.request.post_data_json)
        route.continue_()

    page.route("**/api/config", record)
    try:
        for settings, quiet in ((EMAIL_ROUTE, "#em-quiet"), (TELEGRAM_ROUTE, "#tg-quiet")):
            main = _open_settings(page, server, settings)
            main.locator(quiet).fill("23:00 – 23:00")
            main.get_by_role("button", name=re.compile(SAVE_BUTTON)).click()
            main.get_by_text(QUIET_EQUAL_ERROR).wait_for(state="visible", timeout=UI_TIMEOUT_MS)
    finally:
        page.unroute("**/api/config", record)
    assert patches == [], patches


def test_more_rows_follow_a_saved_settings_page(server: str, page: Page) -> None:
    """reports-and-forms#1: More › Telegram › switch off › Save changes › Back left the rows as they
    were ("On · 1 chat", "… · sent to Telegram and email") until a reload."""
    bot = {"label": "Alex", "bot_token": "123456789:" + "A" * 35, "chat_id": "111111111"}  # made up
    email = {**GMAIL_LOGIN, "enabled": True, "recipients": [{"address": "ops@example.org"}]}
    weekly = {"enabled": True, "weekday": 1, "hour": 8, "send_telegram": True, "send_email": True}
    notifiers = {"telegram": {"enabled": True, "chats": [bot]}, "email": email}
    _http(f"{server}/api/config", "PATCH", {"notifiers": notifiers, "reports": {"weekly": weekly}})
    try:
        _ensure_home(page, server)
        page.goto(f"{server}/#/more", wait_until="domcontentloaded")
        telegram_row = page.locator(f"#more-settings {TELEGRAM_ROW_SUBTITLE}")
        reports_row = page.locator(f"#more-settings {REPORTS_ROW_SUBTITLE}")
        both = reports_row.filter(has_text="sent to Telegram and email")
        both.wait_for(state="visible", timeout=UI_TIMEOUT_MS)
        assert telegram_row.inner_text().replace(" ", " ") == "On · 1 chat"

        page.locator('#more-settings [data-key="telegram"]').click()
        main = page.get_by_role("main")
        main.get_by_role("button", name=re.compile(SAVE_BUTTON)).wait_for(state="visible")
        page.locator("#tg-enabled").set_checked(False)
        _save(page, main)
        page.locator("#back-btn").click()  # the page's own Back, no reload

        telegram_row.filter(has_text=re.compile(r"^Off$")).wait_for(state="visible", timeout=UI_TIMEOUT_MS)
        reports_row.filter(has_text="sent by email").wait_for(state="visible", timeout=UI_TIMEOUT_MS)
        assert reports_row.inner_text() == "Weekly report every Monday at 08:00 · sent by email"
    finally:
        _http(f"{server}/api/config", "PATCH", {"reports": {"weekly": {"enabled": False}}})


def test_connection_says_what_the_nginx_url_is(server: str, page: Page) -> None:
    """T7, the form: with no web server on the machine, the owner tried to make the nginx check pass
    by pointing its HTTP URL at the DSpace UI (:4000). The section says what the URL is, and where
    the check is switched off instead."""
    _open_settings(page, server, CONNECTION_ROUTE)
    sub = page.locator(".view--sub").last
    help_text = sub.locator("#c-nx-http-help").text_content() or ""
    assert "not the DSpace UI's own port (:4000)" in help_text, help_text
    assert sub.locator('a[href="#/more/modules"]').count() == 1


def test_a_saved_postgres_password_says_why_it_shows_dots(server: str, page: Page) -> None:
    """F7#7 (T9 for PostgreSQL): the saved database password in More › DSpace connection is one unit
    like the other secrets (the first key replaces the whole mask), with the line that says it is
    saved and hidden on purpose."""
    _http(f"{server}/api/config", "PATCH", {"postgres": {"password": "pg-example-password"}})  # made up
    try:
        _open_settings(page, server, CONNECTION_ROUTE)
        sub = page.locator(".view--sub").last
        password = sub.locator(PG_PASSWORD_INPUT)
        password.evaluate("el => { el.closest('details').open = true; }")  # the PostgreSQL group
        assert password.input_value() == MASK
        note = sub.get_by_text(SAVED_SECRET_NOTE)
        note.wait_for(state="visible", timeout=UI_TIMEOUT_MS)
        password.press("x")
        assert password.input_value() == "x"
        note.wait_for(state="hidden", timeout=UI_TIMEOUT_MS)
    finally:
        _http(f"{server}/api/config", "PATCH", {"postgres": {"password": ""}})


def test_an_acknowledged_warning_calms_home(server: str, page: Page) -> None:
    """T13 and F7#10: after Ack, Home still read "1 warning · Ports" and the owner took it for "Ack
    did not work". Now an acknowledged alert no longer colours Home: the server leaves it out of the
    mood (mocked as it answers then: "All systems healthy · 1 acknowledged", mood ok) and Home asks
    for that answer right after the Ack, so the headline, the mood colour and the sidebar follow at
    once. The alert stays listed with its Acked pill and what Ack did; the Ports tile and the Stack
    keep the real warning."""
    assert "ports" in {c["id"] for c in _http(f"{server}/api/status")["components"]}
    acked: list[float] = []
    alert = {
        "id": 990001,
        "rule": "component_down:ports",
        "component": "ports",
        "severity": "warning",
        "status": "open",
        "title": "Ports degraded",
        "message": "exposed publicly (::)",
        "value": None,
        "threshold": None,
        "opened_ts": time.time() - 600,
        "resolved_ts": None,
        "acked_ts": None,
        "incident_id": None,
        "notified_ts": None,
    }

    def ports_warning(status: dict[str, Any]) -> None:
        for comp in status["components"]:
            if comp["id"] == "ports":
                comp.update(status="warn", reason="exposed publicly (::)")
        if acked:  # the server's answer once the alert is acknowledged (the component keeps its status)
            status.update(mood="ok", headline=CALM_HEADLINE, alerts_open=[{**alert, "acked_ts": acked[0]}])
        else:
            status.update(mood="warn", headline="1 warning", alerts_open=[dict(alert)])

    def ack(route: Any) -> None:
        acked.append(time.time())
        route.fulfill(json={**alert, "acked_ts": acked[0]})

    _ensure_home(page, server)
    undo = _serve_home(page, server, ports_warning)
    page.route("**/api/alerts/990001/ack", ack)
    try:
        _text_is(page, PULSE_HEADLINE, "1 warning · Ports")
        assert page.evaluate(MOOD_JS) == "warn"
        tile = page.locator('#home-alerts [data-key="alert-990001"]')
        tile.get_by_role("button", name=re.compile(r"^Acknowledge")).click()
        tile.get_by_text(ACKED_NOTE).wait_for(state="visible", timeout=UI_TIMEOUT_MS)
        _text_is(page, PULSE_HEADLINE, CALM_HEADLINE)
        page.wait_for_function(f"() => ({MOOD_JS})() === 'ok'", timeout=UI_TIMEOUT_MS)
        assert (page.locator("#mood-line .mood-text").text_content() or "").startswith("All healthy")
        assert tile.locator(".pill").filter(has_text="Acked").count() == 1
        assert page.locator('#home-stack [data-key="ports"] .pill--warn').count() == 1
        page.goto(f"{server}/#/stack", wait_until="domcontentloaded")
        ladder = page.locator('.ladder[data-component="ports"]')
        ladder.wait_for(state="attached", timeout=UI_TIMEOUT_MS)
        assert ladder.get_attribute("data-status") == "warn"
    finally:
        page.unroute("**/api/alerts/990001/ack", ack)
        undo()


def test_a_check_that_cannot_run_says_why(server: str, page: Page) -> None:
    """T15: Home read "1 check could not run" and "No open alerts — Logs could not be checked this
    time." while Sentinel could not read DSpace's log at all. Home names the component and its
    reason, and keeps "this time" for a check that failed once (it timed out or raised)."""
    assert "logs" in {c["id"] for c in _http(f"{server}/api/status")["components"]}
    rung = {"id": "logs.dspace", "label": "dspace.log", "status": "unknown", "ts": time.time()}
    denied = {**rung, "message": "permission denied (/dspace/log/dspace.log)", "detail": {}}
    once = {"collector_error": True}  # the scheduler's rung for a check that timed out or raised
    timed_out = {**rung, "id": "logs.error", "message": "check timed out after 29 s", "detail": once}
    shown = [denied]

    def logs_unknown(status: dict[str, Any]) -> None:
        for comp in status["components"]:
            if comp["id"] == "logs":
                comp.update(status="unknown", reason=shown[0]["message"], chain=[shown[0]])
        status.update(mood="unknown", headline="1 check could not run")

    _ensure_home(page, server)
    undo = _serve_home(page, server, logs_unknown)
    card = "#home-alerts .empty-text"
    try:
        _text_is(page, PULSE_HEADLINE, "1 check could not run · Logs")
        why = "Logs cannot be checked: permission denied (/dspace/log/dspace.log)"
        _text_is(page, card, f"No open alerts — {why}.")
        link = page.locator("#home-alerts").get_by_role("link", name="Open Logs")
        assert link.get_attribute("href") == "#/stack/logs"
        shown[0] = timed_out
        page.reload(wait_until="domcontentloaded")
        why = "Logs could not be checked this time: check timed out after 29 s"
        _text_is(page, card, f"No open alerts — {why}.")
    finally:
        undo()


def test_time_zone_is_chosen_in_more_alerts(server: str, page: Page) -> None:
    """F7#9 (T18): an alert read "Started 15:59", the server's UTC clock, for 21:29 in Colombo. More ›
    Alert thresholds sets the zone of the times Sentinel sends: the help names the server's own zone
    (from /api/meta), a bad name is refused under the field in the server's words, a good one is saved
    as server.timezone; the quiet-hours and digest fields of Telegram and Email then name the zone.
    /api/meta's zone and the PATCH are answered here as the server answers them."""
    patches: list[dict[str, Any]] = []

    def meta(route: Any) -> None:
        response = route.fetch()
        route.fulfill(response=response, json={**response.json(), "timezone": COLOMBO})

    def config(route: Any) -> None:
        body = route.request.post_data_json if route.request.method == "PATCH" else None
        if not isinstance(body, dict) or "server" not in body:
            route.continue_()
            return
        patches.append(body)
        timezone = body["server"].get("timezone")
        if timezone == "Mars/Base":
            route.fulfill(status=400, json={"error": BAD_ZONE})
            return
        saved = _http(f"{server}/api/config")
        saved.setdefault("server", {})["timezone"] = timezone
        route.fulfill(json=saved)

    page.route(META_URL, meta)
    page.route(CONFIG_URL, config)
    try:
        main = _open_settings(page, server, ALERTS_ROUTE)
        page.reload(wait_until="domcontentloaded")  # /api/meta again, now with the zone
        zone = main.locator("#th-timezone")
        zone.wait_for(state="visible", timeout=UI_TIMEOUT_MS)
        help_text = main.locator("#th-timezone-help").text_content()
        assert help_text == "IANA name such as Asia/Colombo; empty = the server's zone (UTC).", help_text

        zone.fill("Mars/Base")
        main.get_by_role("button", name=re.compile(SAVE_BUTTON)).click()
        refused = "Unknown time zone 'Mars/Base': use an IANA name such as Asia/Colombo"
        _text_is(page, "#th-timezone-error", f"{refused}, or leave it empty for the server's zone")
        zone.fill("Asia/Colombo")
        _save(page, main)
        assert patches[-1]["server"] == {"timezone": "Asia/Colombo"}, patches

        in_colombo = "in Asia/Colombo (+0530)"
        main = _open_settings(page, server, TELEGRAM_ROUTE)
        assert main.locator("#tg-quiet-help").text_content() == (
            f"Only critical alerts during this window, {in_colombo}, for every bot. "
            "Leave empty to always notify."
        )
        assert main.locator("#tg-digest-help").text_content() == (
            f"One summary message per day at this time, {in_colombo}. Leave empty for none."
        )
        main = _open_settings(page, server, EMAIL_ROUTE)
        assert main.locator("#em-quiet-help").text_content() == (
            f"Only critical alerts by email during this window, {in_colombo}. Leave empty to always send."
        )
        assert main.locator("#em-digest-help").text_content() == (
            f"One summary email per day at this time, {in_colombo}. Leave empty for none."
        )
    finally:
        page.unroute(META_URL, meta)
        page.unroute(CONFIG_URL, config)
        page.reload(wait_until="domcontentloaded")


def test_wizard_preselects_security_for_every_option(server: str, page: Page) -> None:
    """SECURITY-CONTRACT (UI): step 2 lists Security switched on, with "this server" as its evidence,
    whichever option was chosen, even when discovery found nothing on the machine."""
    nothing_found = {"findings": [], "suggested_config": {}, "ports": [], "services": {}}

    def discover(route: Any) -> None:
        route.fulfill(status=200, json=nothing_found)

    _http(f"{server}/api/config", "PATCH", {"setup": {"completed": False, "persona": ""}})
    page.route(DISCOVER_URL, discover)
    try:
        page.goto(f"{server}/", wait_until="domcontentloaded")
        gate = page.locator("#gate")
        for persona in PERSONAS:
            gate.get_by_role("radio", name=re.compile(rf"^{re.escape(persona)}")).click()
            gate.get_by_role("button", name=re.compile(CONTINUE_BUTTON)).click()
            switch = gate.locator("#mod-security")
            switch.wait_for(state="attached", timeout=UI_TIMEOUT_MS)
            assert switch.is_checked(), persona
            row = switch.locator("xpath=ancestor::label[1]")
            assert row.locator(".tile-title").inner_text() == "Security"
            assert row.locator(".tile-sub").inner_text() == f"this server — {SECURITY_EXPLAINER}", persona
            if persona != PERSONAS[-1]:
                gate.get_by_role("button", name=re.compile(r"Back$")).click()
        gate.get_by_role("button", name=re.compile(START_BUTTON)).click()
        _nav(page).wait_for(state="visible", timeout=UI_TIMEOUT_MS)
    finally:
        page.unroute(DISCOVER_URL, discover)
        if not _http(f"{server}/api/meta")["setup_completed"]:
            # failed half-way: leave the next tests a finished setup, not the wizard
            _http(f"{server}/api/config", "PATCH", {"setup": {"completed": True, "persona": "full"}})
            page.reload(wait_until="domcontentloaded")

    def _setup_done() -> Any:
        meta = _http(f"{server}/api/meta")
        return meta if meta["setup_completed"] else None

    assert _wait_until(_setup_done, timeout_s=15, what="setup.completed to become true")["persona"] == "full"


def test_more_modules_lists_security_after_ports(server: str, page: Page) -> None:
    """More › Modules: the Security switch, with its explainer, sits right after Ports."""
    _ensure_home(page, server)
    page.goto(f"{server}/#/more/modules", wait_until="domcontentloaded")
    sub = page.locator(".view--sub").last
    row = sub.locator('[data-key="module-security"]')
    row.wait_for(state="visible", timeout=UI_TIMEOUT_MS)
    assert row.locator(".tile-title").inner_text() == "Security"
    assert row.locator(".tile-sub").inner_text() == SECURITY_EXPLAINER
    keys = sub.locator('[role="group"][aria-label="Modules"] > [data-key]').evaluate_all(
        "(rows) => rows.map((row) => row.dataset.key)"
    )
    assert keys.index("module-security") == keys.index("module-ports") + 1, keys


def test_report_panel_offers_security_only_while_its_module_is_on(server: str, page: Page) -> None:
    """The New report panel lists a module's report section only while the module is on, Security too
    (SECTION_MODULES in panels/report.js)."""
    _ensure_home(page, server)
    for on in (False, True):
        undo = _serve_home(
            page, server, lambda status, on=on: status.setdefault("modules", {}).update(security=on)
        )
        try:
            _pulse(page)  # the status frame, and with it the module map, has arrived
            page.get_by_role("button", name=re.compile(NEW_REPORT_BUTTON)).first.click()
            dialog = page.get_by_role("dialog").filter(
                has=page.get_by_role("heading", name=re.compile(REPORT_PANEL_TITLE))
            )
            boxes = dialog.first.locator('input[name="sections"]')
            boxes.first.wait_for(state="attached", timeout=UI_TIMEOUT_MS)
            offered = boxes.evaluate_all("(boxes) => boxes.map((box) => box.value)")
            assert ("security" in offered) is on, offered
            page.keyboard.press("Escape")
            dialog.first.wait_for(state="hidden", timeout=UI_TIMEOUT_MS)
        finally:
            undo()


def test_home_wide_and_narrow_fallback(server: str, browser: Browser) -> None:
    """1920x1080 uses the width; 390x844 (the fallback) renders without horizontal overflow."""
    for name, viewport in (("home-wide", WIDE), ("home-narrow", NARROW)):
        ctx = browser.new_context(
            viewport=viewport, color_scheme="light", reduced_motion="reduce", locale="en-US"
        )
        ctx.set_default_timeout(UI_TIMEOUT_MS)
        try:
            pg = ctx.new_page()
            _ensure_home(pg, server)
            # The header's countdown comes with the first status frame; capture it, not an empty header.
            pg.wait_for_function(
                "() => /Next check in/.test(document.getElementById('home-checked')?.textContent || '')",
                timeout=UI_TIMEOUT_MS,
            )
            overflow = pg.evaluate(
                "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
            )
            assert overflow <= 1, f"{name}: page scrolls horizontally by {overflow}px"
            _screenshot(pg, name)
        finally:
            ctx.close()


def test_no_uncaught_page_errors(page_errors: list[str]) -> None:
    """Collected across the session: the UI must not throw uncaught exceptions."""
    assert page_errors == [], "\n".join(page_errors)
