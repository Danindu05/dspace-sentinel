"""The first-run wizard on a server without a web server (SPEC section 12.5, Playwright sync API).

A demo server of this module's own (a free port, a temporary data directory, setup not completed)
serves the real UI. ``POST /api/actions/discover`` is answered by ``page.route`` with what the real
``discovery.discover`` finds on a DSpace 8 server set up by hand: Tomcat 10 and PostgreSQL run as
units, Solr and the UI answer without one, and nothing listens on 80 or 443 (DSpace is opened
directly on :4000). After "Full DSpace stack" step 2 must show nginx and SSL switched off with "not
found on this machine" and the DSpace parts switched on; "Start monitoring" and "Skip for now" must
both leave nginx and SSL off, so the first minute brings no false "nginx unreachable".

Like ``tests/e2e/test_detail_states.py`` (whose way of starting a server it follows) the module runs
only with ``SENTINEL_E2E=1``, skips without Playwright, its Chromium or a built UI, and drives its
Chromium from a worker thread of its own: ``test_ui.py`` keeps the main thread's Playwright for the
whole session.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import time
import urllib.error
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from dspace_sentinel import discovery
from dspace_sentinel.config import Config
from dspace_sentinel.util.net import ListeningSocket
from tests.e2e.test_ui import (
    APP_JS,
    BOOT_TIMEOUT_S,
    CONTINUE_BUTTON,
    E2E_ENABLED,
    HEADLESS,
    PERSONA_TILE,
    REPO_DIR,
    START_BUTTON,
    UI_TIMEOUT_MS,
    PlaywrightError,
    _free_port,
    _http,
    _tail,
    _wait_until,
    sync_playwright,
)

try:
    from playwright.sync_api import expect
except ImportError:  # pragma: no cover - exercised only when the dev extra is not installed
    expect = None  # type: ignore[assignment]

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not E2E_ENABLED, reason="set SENTINEL_E2E=1 to run the Playwright suite"),
    pytest.mark.skipif(sync_playwright is None, reason="playwright is not installed"),
]

DESKTOP = {"width": 1440, "height": 900}
SKIP_BUTTON = r"^Skip for now$"
NOT_FOUND = "not found on this machine"
UNITLESS_SOLR = (
    "Solr answers on 8983 but no systemd unit was found for it: it may not start by itself after a reboot"
)
DSPACE_PARTS = ("postgres", "solr", "backend", "frontend")
"""What "Full DSpace stack" names: switched on even without evidence ("not found" may mean "down")."""


def hand_made_discovery() -> dict[str, Any]:
    """The discovery API's answer on a DSpace 8 server set up by hand, from the real ``discover``.

    Everything is injected (sockets, units, properties, files, versions, processes), so nothing on
    the machine running the tests is inspected or asked.
    """
    sockets = [
        ListeningSocket(22, "0.0.0.0", True, "ipv4"),
        ListeningSocket(4000, "0.0.0.0", True, "ipv4"),
        ListeningSocket(8080, "::", True, "ipv6"),
        ListeningSocket(8983, "::", True, "ipv6"),
        ListeningSocket(5432, "127.0.0.1", False, "ipv4"),
    ]
    props = {
        "dspace.dir": "/dspace",
        "dspace.name": "Test Repository",
        "dspace.server.url": "http://localhost:8080/server",
        "dspace.ui.url": "http://localhost:4000",
        "db.url": "jdbc:postgresql://localhost:5432/dspace",
        "db.username": "dspace",
        "solr.server": "http://localhost:8983/solr",
        "log.report.dir": "/dspace/log",
    }
    result = asyncio.run(
        discovery.discover(
            Config(),
            sockets=sockets,
            unit_states={"tomcat10": "active", "postgresql": "active"},
            properties=(props, ["/dspace/config/local.cfg"]),
            file_exists=lambda path: False,
            versions={"dspace": "DSpace 8.1", "cris": None, "solr": "8.11.4"},
            cmdlines=[],
        )
    )
    found = result.to_dict()
    modules = found["suggested_config"]["modules"]
    assert modules["nginx"] is False and modules["ssl"] is False  # the evidence the wizard reads
    return found


# ------------------------------------------------------------------ the browser, in a thread of its own


class _Chromium:
    """A headless Chromium driven from one worker thread (Playwright's sync API: one instance per thread)."""

    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="onboarding")
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
def onboarding_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A demo server for this module (free port, private data dir, setup NOT completed); yields its URL."""
    if not APP_JS.is_file():
        pytest.skip(f"web UI not built yet ({APP_JS.relative_to(REPO_DIR)} is missing)")
    root = tmp_path_factory.mktemp("sentinel-e2e-onboarding")
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


@pytest.fixture(scope="module")
def chromium(onboarding_server: str) -> Iterator[_Chromium]:
    try:
        browser = _Chromium()
    except PlaywrightError as exc:
        pytest.skip(f"Playwright Chromium is not installed (run `playwright install chromium`): {exc}")
    try:
        yield browser
    finally:
        browser.close()


# ------------------------------------------------------------------ the wizard


def _setup_pending(base: str) -> None:
    """Show the wizard again, with every module on (what the demo starts with)."""
    pending = {"setup": {"completed": False, "persona": ""}, "modules": {"nginx": True, "ssl": True}}
    _http(f"{base}/api/config", "PATCH", pending)
    meta = _http(f"{base}/api/meta")
    assert meta["setup_completed"] is False and meta["modules"]["nginx"] is True


def _setup_done(base: str) -> dict[str, Any]:
    def done() -> Any:
        meta = _http(f"{base}/api/meta")
        return meta if meta["setup_completed"] else None

    return _wait_until(done, timeout_s=15, what="setup.completed to become true")


def _wizard(base: str, browser: Any, found: dict[str, Any], finish: str) -> list[str]:
    """Choose "Full DSpace stack" with ``found`` as discovery's answer, check step 2, press ``finish``.

    Returns the page's uncaught errors.
    """
    assert expect is not None
    ctx = browser.new_context(viewport=DESKTOP, color_scheme="light", reduced_motion="reduce", locale="en-US")
    ctx.set_default_timeout(UI_TIMEOUT_MS)
    errors: list[str] = []
    asked: list[str] = []
    try:
        page = ctx.new_page()
        page.on("pageerror", lambda err: errors.append(f"pageerror: {err}"))

        def discover(route: Any) -> None:
            asked.append(route.request.method)
            route.fulfill(status=200, json=found)

        page.route(re.compile(r".*/api/actions/discover(\?.*)?$"), discover)
        page.goto(f"{base}/", wait_until="domcontentloaded")
        page.get_by_role("radio", name=re.compile(PERSONA_TILE)).click()
        page.get_by_role("button", name=re.compile(CONTINUE_BUTTON)).click()

        # step 2: nginx and SSL stay in the list, switched off, and say why
        for module in ("nginx", "ssl"):
            switch = page.locator(f"#mod-{module}")
            expect(switch).not_to_be_checked()
            expect(page.locator("label.toggle").filter(has=switch)).to_contain_text(NOT_FOUND)
        for module in DSPACE_PARTS:
            expect(page.locator(f"#mod-{module}")).to_be_checked()
        assert asked == ["POST"], asked
        # "What we found" names Solr's missing unit (the ladder gets no "unit not found" rung)
        expect(page.locator("ul.finding-list")).to_contain_text(UNITLESS_SOLR)

        page.get_by_role("button", name=re.compile(finish)).click()
        page.get_by_role("navigation", name="Main").wait_for(state="visible", timeout=UI_TIMEOUT_MS)
    finally:
        ctx.close()
    return errors


def test_start_monitoring_leaves_nginx_and_ssl_off_without_a_web_server(
    onboarding_server: str, chromium: _Chromium
) -> None:
    """T1/T3: no web server and no HTTPS found, so nginx and SSL start switched off."""
    _setup_pending(onboarding_server)
    errors = chromium.run(_wizard, onboarding_server, chromium.browser, hand_made_discovery(), START_BUTTON)
    assert errors == [], errors
    meta = _setup_done(onboarding_server)
    assert meta["persona"] == "full"
    assert meta["modules"]["nginx"] is False and meta["modules"]["ssl"] is False
    assert all(meta["modules"][module] for module in DSPACE_PARTS)
    config = _http(f"{onboarding_server}/api/config")
    assert config["solr"]["service"] == "" and config["dspace"]["frontend_service"] == ""


def test_skip_for_now_leaves_nginx_and_ssl_off_without_a_web_server(
    onboarding_server: str, chromium: _Chromium
) -> None:
    """Skipping takes the chosen option as it is, except nginx and SSL, which were not found."""
    _setup_pending(onboarding_server)
    errors = chromium.run(_wizard, onboarding_server, chromium.browser, hand_made_discovery(), SKIP_BUTTON)
    assert errors == [], errors
    meta = _setup_done(onboarding_server)
    assert meta["persona"] == "full"
    assert meta["modules"]["nginx"] is False and meta["modules"]["ssl"] is False
    assert all(meta["modules"][module] for module in DSPACE_PARTS)
