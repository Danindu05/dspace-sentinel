"""Component detail pages in states the demo never shows (SPEC section 12.5, Playwright sync API).

A demo server of this module's own (a free port, a temporary data directory, onboarding completed
through the API) serves the real UI. Each case opens a detail page (``#/stack/<id>``) with the
browser's requests rewritten by ``page.route``: the real ``/api/components/<id>``, ``/api/status``
and one SSE ``status`` frame of ``/api/stream`` come back with one component changed the way its
collector reports the state: an SEO known issue, an actuator that is not exposed, a Solr core that
failed to load or to reload, a client-side shell, a public URL that is down … What a collector
grades is graded here by the collector's own functions (``grade_health``, ``csr_note``,
``failure_reason``, ``health.rollup``), and ``tests/test_collectors.py`` runs the real collector in
every scenario's situation and compares the two, so a state reads as it does on a real server. The
checks read the rendered "Right now" tiles and the ladder, and every case fails on a console error,
an uncaught page error or a horizontal scroll bar. The key/value lists of the block tiles (the
component page's Rendering and Connection used, Server's Host, About, a report's facts) are measured
in the same way: each sits right under its tile's title row.

The states, by page:

* backend: the demo's anonymous ``UP_WITH_ISSUES``; the SEO known issue (anonymous, and with the
  components visible); a ``DOWN`` that is a warning (anonymous, and an administrator's view);
  ``UP_WITH_ISSUES`` with components; an actuator that is not exposed or answers HTTP 500; a REST API
  that is down; DSpace 7.x, a 7.x snapshot with DSpace-CRIS, 8.2 with DSpace-CRIS; a Solr that the
  backend depends on and that answers correctly, only too slowly;
* Stack tab: the SEO known issue in the backend's ladder;
* frontend: the demo's server-side render; the client-side shell with each of its notes; a homepage
  without DSpace markup; a homepage that does not answer; a public URL that is ok, down or not set, and
  one that is set but not checked (before the first check, after a failed frontend check);
* Solr: the demo's six cores; a core that failed to load (with the fix); an optional core that failed
  to load; a core whose reload failed, and one whose ping fails as well; the failed reload of a core
  Sentinel does not ping;
* Ports: the demo's ports; the test server's, which listen on every interface (the addresses in the
  Listening table);
* rungs with a hint (``detail.hint``, a collector's advice, shown under the rung's message): nginx on a
  server without a web server, a Solr port that listens on every interface, a log Sentinel may not read;
* Security (SECURITY-CONTRACT): a healthy server, one with every warning, one where most checks are not
  available, a long list of ports, and the test server's Tailscale ports; its tile on Home and its ladder on
  the Stack tab. The demo may not
  have the component yet, so its states add it to the status (after Ports) and answer its detail payload
  themselves (``ADDED``).

``tests/test_collectors.py`` replays every state of ``SCENARIOS`` through the real collector and checks
that the two are the same situations; the states of ``UI_STATES`` are built the same way from the
collectors' words but have no such twin (yet), so they sit in a registry of their own.

Like ``tests/e2e/test_ui.py`` (whose helpers it imports) the module runs only with
``SENTINEL_E2E=1`` and skips without Playwright, its Chromium or a built UI. Playwright's sync API
allows one running instance per thread and ``test_ui.py`` keeps one for the whole session, so this
module drives its Chromium from a worker thread of its own (:class:`_Chromium`). Screenshots of the
states the latest fixes changed go to ``build/screenshots/detail-*.png``, always: they are evidence
for a review, not the README's images, so ``SENTINEL_E2E_SHOTS`` (which points ``test_ui.py`` at
``docs/screenshots``) does not move them.
"""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import pytest

from dspace_sentinel import dspace_version, health
from dspace_sentinel.collectors.backend import (
    ACTUATOR_MIN_INTERVAL_S,
    actuator_components,
    grade_health,
    version_number,
)
from dspace_sentinel.collectors.frontend import NO_MARKUP, csr_note
from dspace_sentinel.collectors.services import slow_message
from dspace_sentinel.collectors.solr import RELOAD_NOTE, failure_hint, failure_reason
from dspace_sentinel.models import CheckResult, ComponentHealth
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

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not E2E_ENABLED, reason="set SENTINEL_E2E=1 to run the Playwright suite"),
    pytest.mark.skipif(sync_playwright is None, reason="playwright is not installed"),
]

DESKTOP = {"width": 1440, "height": 900}
NBSP = "\u00a0"
DETAIL_SHOT_DIR = REPO_DIR / "build" / "screenshots"
"""Where ``detail-<case>.png`` goes: the git-ignored build tree (CI uploads it), whatever
``SENTINEL_E2E_SHOTS`` says, so refreshing the README images never adds these to ``docs/screenshots``."""

# ------------------------------------------------------------------ scenarios (each changes one component)


def _rung(comp: dict[str, Any], rung_id: str) -> dict[str, Any]:
    found = next((r for r in comp.get("chain", []) if r["id"] == rung_id), None)
    assert found is not None, f"{comp.get('id')} has no rung {rung_id}"
    return found


def _rollup(comp: dict[str, Any]) -> None:
    """Status, reason and headline latency of the changed chain, by the real ``health.rollup``."""
    rolled = health.rollup(
        comp["id"], [CheckResult.from_dict(r) for r in comp["chain"]], comp.get("metrics") or {}
    )
    comp.update(status=rolled.status, reason=rolled.reason, latency_ms=rolled.latency_ms)


# ---- backend: what GET /server/api and /server/actuator/health answered


def _rest_root(comp: dict[str, Any], version: str, cris: str | None = None) -> None:
    """The REST root names ``version`` ("DSpace 9.2") and, on DSpace-CRIS, ``crisVersion``."""
    root = {"dspaceVersion": version, **({"crisVersion": cris} if cris else {})}
    rest = _rung(comp, "backend.rest")
    rest["message"] = version
    rest["detail"] = {**rest["detail"], "dspaceVersion": version}
    comp["metrics"].update(dspace_version=version_number(version), **dspace_version.version_metrics(root))


def _actuator(
    comp: dict[str, Any],
    body: dict[str, Any] | None,
    *,
    status_code: int = 200,
    reused_after_s: int | None = None,
) -> None:
    """The actuator rung for an answer of ``/actuator/health``, as ``BackendCollector`` builds it.

    ``body`` is graded by the collector's ``grade_health`` against the version the REST rung names.
    ``reused_after_s`` makes it an answer the collector kept (``detail.cached_age_s``): it keeps only
    an ok or informational answer, and for less than five minutes, so anything else is refused here.
    """
    rung = _rung(comp, "backend.actuator")
    version = dspace_version.parse(_rung(comp, "backend.rest")["detail"].get("dspaceVersion"))
    components = actuator_components(body)
    metrics = comp["metrics"]
    metrics.update(actuator_status=None, actuator_components=components)
    detail: dict[str, Any] = {"status_code": status_code}
    if reused_after_s is not None:
        detail["cached_age_s"] = reused_after_s
    grade = grade_health(body, version)
    if grade is not None and body is not None:
        metrics["actuator_status"] = str(body["status"]).strip().upper()
        detail.update(components=components, health_status=metrics["actuator_status"])
        if grade.informational:
            detail["informational"] = True
        if grade.known_issue:
            detail["known_issue"] = grade.known_issue
        rung.update(status=grade.status, message=grade.message)
    elif status_code in (401, 403, 404):
        detail["informational"] = True
        rung.update(status="unknown", message=f"not exposed (HTTP {status_code})")
    else:
        rung.update(status="warn", message=f"HTTP {status_code}")
    rung["detail"] = detail
    if reused_after_s is not None:
        assert rung["status"] == "ok" or detail.get("informational"), "only a healthy answer is reused"
        assert 0 <= reused_after_s < ACTUATOR_MIN_INTERVAL_S, "an answer is reused for five minutes"
    _rollup(comp)


SITEMAP_MISSING = (
    "Sitemaps are missing or inaccessible. Please see the DSpace Documentation on Search Engine "
    "Optimization for how to enable Sitemaps."
)
SSR_OFF = (
    "Server-side rendering (SSR) appears to be disabled. Most search engines require enabling SSR "
    "for proper indexing. Please see the DSpace Documentation on Search Engine Optimization for more details."
)


def _admin_health(status: str, **not_up: str) -> dict[str, Any]:
    """``/actuator/health`` with its components: an administrator's view, or show-details=always."""
    solr_core = {"status": "UP", "details": {"status": 0, "detectedPathType": "particular core"}}
    components: dict[str, Any] = {
        "db": {"status": "UP", "details": {"database": "PostgreSQL", "validationQuery": "isValid()"}},
        "geoIp": {
            "status": "UP_WITH_ISSUES",
            "details": {"reason": "The required 'dbfile' configuration is missing in usage-statistics.cfg!"},
        },
        "solrSearchCore": copy.deepcopy(solr_core),
        "solrStatisticsCore": copy.deepcopy(solr_core),
        "solrOaiCore": copy.deepcopy(solr_core),
        "solrAuthorityCore": copy.deepcopy(solr_core),
        "seo": {"status": "UP", "details": {"sitemap": "OK", "robots.txt": "OK", "ssr": "OK"}},
    }
    for name, state in not_up.items():
        components[name]["status"] = state
    if components["seo"]["status"] != "UP":
        components["seo"]["details"].update(sitemap=SITEMAP_MISSING, ssr=SSR_OFF)
    return {"status": status, "components": components}


def backend_seo(comp: dict[str, Any]) -> None:
    """9.2 answers an anonymous DOWN while healthy (its SEO check): a known issue, kept for five minutes."""
    _rest_root(comp, "DSpace 9.2")
    _actuator(comp, {"status": "DOWN"}, reused_after_s=190)


def backend_seo_components(comp: dict[str, Any]) -> None:
    """9.2 with its components visible, and ``seo`` the only one down: the same known issue."""
    _rest_root(comp, "DSpace 9.2")
    _actuator(comp, _admin_health("DOWN", seo="DOWN"), reused_after_s=70)


def backend_down(comp: dict[str, Any]) -> None:
    """9.4 answers an anonymous DOWN: a warning, which the collector asks again at the next check."""
    _actuator(comp, {"status": "DOWN"})


def backend_admin_down(comp: dict[str, Any]) -> None:
    """DOWN with the components visible: the search core's indicator is the one that is down."""
    _actuator(comp, _admin_health("DOWN", solrSearchCore="DOWN"))


def backend_admin_up(comp: dict[str, Any]) -> None:
    """UP_WITH_ISSUES with the components visible (no GeoLite database), a kept answer."""
    _actuator(comp, _admin_health("UP_WITH_ISSUES"), reused_after_s=130)


def backend_hidden(comp: dict[str, Any]) -> None:
    """A backend that does not expose its actuator: informational, a kept answer."""
    _actuator(comp, None, status_code=404, reused_after_s=250)


def backend_http_500(comp: dict[str, Any]) -> None:
    _actuator(comp, None, status_code=500)


def backend_rest_down(comp: dict[str, Any]) -> None:
    """The REST API answers 503: actuator and search are not reached, and nothing names a version."""
    _rung(comp, "backend.rest").update(
        status="crit", message="HTTP 503", latency_ms=12.0, detail={"status_code": 503}
    )
    for rung_id in ("backend.actuator", "backend.search"):
        _rung(comp, rung_id).update(
            status="unknown", message="not reached", latency_ms=None, value=None, detail={}
        )
    comp["metrics"].update(
        dspace_version=None,
        dspace_name=None,
        **dspace_version.version_metrics(None),
        rest_latency_ms=None,
        search_latency_ms=None,
        actuator_status=None,
        actuator_components={},
    )
    _rollup(comp)


def backend_7x(comp: dict[str, Any]) -> None:
    _rest_root(comp, "DSpace 7.6.6")
    _actuator(comp, {"status": "UP"})


def backend_7x_snapshot_cris(comp: dict[str, Any]) -> None:
    _rest_root(comp, "DSpace 7.6.8-SNAPSHOT", cris="cris-2023.02.07")
    _actuator(comp, {"status": "UP"})


def backend_8_cris(comp: dict[str, Any]) -> None:
    """DSpace-CRIS on 8.2, which is inside the SEO window too."""
    _rest_root(comp, "DSpace 8.2", cris="cris-2024.02.04")
    _actuator(comp, {"status": "DOWN"})


SLOW_SEARCH_MS = 4300.0


def slow_solr() -> ComponentHealth:
    """Solr as the backend collector finds it when the search query answers correctly, 4.3 s later:
    critical by its latency alone (the rung carries ``detail.slow``)."""
    search = CheckResult(
        id="solr.search",
        label="Search query",
        status="crit",
        ts=time.time(),
        latency_ms=SLOW_SEARCH_MS,
        value=27687.0,
        message=slow_message("search", SLOW_SEARCH_MS),
        detail={"slow": True, "query": "q=*:*&rows=0", "core": "search", "status_code": 200},
    )
    return health.rollup("solr", [search])


def backend_solr_slow(comp: dict[str, Any]) -> None:
    """The Solr the backend depends on is only too slow: its rung says slow, where a failure says down."""
    old = _rung(comp, "backend.dep.solr")
    rung = health.dependency_rung(old["id"], old["label"], slow_solr(), old["ts"])
    comp["chain"][comp["chain"].index(old)] = rung.to_dict()
    _rollup(comp)


# ---- frontend: what the homepage and the public URL answered

PUBLIC_URL = "https://repository.example.edu"
PUBLIC_HOST = "repository.example.edu"
BOT_CHECK_TITLE = "Making sure you're not a bot!"


def _settings(comp: dict[str, Any], **dspace: str) -> None:
    """The dspace settings a state implies, for the detail payload's "Connection used" tile (a status
    frame carries no settings)."""
    if "config_used" in comp:
        comp["config_used"].setdefault("dspace", {}).update(dspace)


def _shell(comp: dict[str, Any], note: str) -> None:
    """The homepage came back as the client-side shell (``<ds-app></ds-app>``), with the collector's note."""
    home = _rung(comp, "frontend.home")
    home["message"] = note
    home["detail"].update(rendering="csr", note=note, title="DSpace")
    home["detail"].pop("host_header", None)
    comp["metrics"].update(rendering="csr", generator=None, angular=None, title="DSpace", host_header=None)
    _rollup(comp)


def frontend_shell(comp: dict[str, Any]) -> None:
    """The shell, and nothing says which host ui.baseUrl names: server-side rendering is off or failing."""
    _shell(comp, csr_note("9.4", "localhost", None))


def frontend_shell_host_rule(comp: dict[str, Any]) -> None:
    """9.4 probed on localhost:4000 while its ui.baseUrl names the public site: the Host rule."""
    _shell(comp, csr_note("9.4", "localhost", PUBLIC_HOST))


def frontend_shell_public_host(comp: dict[str, Any]) -> None:
    """9.4 probed at its public host (dspace.ui_url is the public site): SSR itself is off or failing."""
    _shell(comp, csr_note("9.4", PUBLIC_HOST, PUBLIC_HOST))
    _rung(comp, "frontend.tcp").update(label="TCP :443", detail={"host": PUBLIC_HOST, "port": 443})
    _rung(comp, "frontend.home")["detail"]["url"] = PUBLIC_URL
    comp["metrics"]["ui_url"] = PUBLIC_URL
    _settings(comp, ui_url=PUBLIC_URL)


def _public_rung(comp: dict[str, Any], status: str, message: str, latency: float | None) -> None:
    """Put a ``frontend.public`` rung after the homepage (dspace.public_url is set)."""
    home = _rung(comp, "frontend.home")
    public = {
        "id": "frontend.public",
        "label": "Public URL",
        "status": status,
        "ts": home["ts"],
        "latency_ms": latency,
        "value": None,
        "message": message,
        "detail": {"url": PUBLIC_URL, **({"status_code": 200, "rendering": "ssr"} if status == "ok" else {})},
    }
    comp["chain"] = [r for r in comp["chain"] if r["id"] != "frontend.public"]
    comp["chain"].insert(comp["chain"].index(home) + 1, public)
    home["detail"]["host_header"] = PUBLIC_HOST
    comp["metrics"].update(public_url=PUBLIC_URL, public_latency_ms=latency, host_header=PUBLIC_HOST)
    _settings(comp, public_url=PUBLIC_URL)
    _rollup(comp)


def frontend_public_down(comp: dict[str, Any]) -> None:
    """dspace.public_url is set and nginx refuses: the collector leaves public_latency_ms null."""
    _public_rung(comp, "crit", f"connection refused ({PUBLIC_HOST}:443)", None)


def frontend_public_ok(comp: dict[str, Any]) -> None:
    _public_rung(comp, "ok", "", 871.4)


def frontend_no_markup(comp: dict[str, Any]) -> None:
    """HTTP 200 without the DSpace UI in it: a bot check in front of the site, say."""
    home = _rung(comp, "frontend.home")
    home.update(status="warn", message=NO_MARKUP)
    home["detail"] = {"title": BOT_CHECK_TITLE, "url": home["detail"]["url"], "status_code": 200}
    comp["metrics"].update(rendering=None, generator=None, angular=None, title=BOT_CHECK_TITLE)
    _rollup(comp)


def frontend_home_down(comp: dict[str, Any]) -> None:
    """The UI accepts the connection and never answers: no page, so nothing was read from one."""
    home = _rung(comp, "frontend.home")
    home.update(status="crit", message="timed out after 5 s (localhost:4000)", latency_ms=None)
    home["detail"] = {"url": home["detail"]["url"]}
    comp["metrics"].update(rendering=None, generator=None, angular=None, title=None, home_latency_ms=None)
    _rollup(comp)


def frontend_public_pending(comp: dict[str, Any]) -> None:
    """dspace.public_url is set and the frontend was not checked yet: the placeholder that
    GET /api/components/frontend answers before the first check (no rungs, no metrics)."""
    comp.update(
        status="unknown",
        reason="not checked yet",
        chain=[],
        metrics={},
        latency_ms=None,
        since=None,
        checked_at=None,
    )
    _settings(comp, public_url=PUBLIC_URL)


FRONTEND_TIMEOUT_S = 29
"""The frontend check's time-out with the default intervals (``collector.http_interval`` - 1 s)."""


def frontend_public_check_failed(comp: dict[str, Any]) -> None:
    """dspace.public_url is set and the last frontend check itself failed (it timed out): the scheduler
    keeps only its error rung, and no metrics."""
    error = CheckResult(
        id="frontend.error",
        label="DSpace Frontend check",
        status="unknown",
        ts=_rung(comp, "frontend.home")["ts"],
        message=f"check timed out after {FRONTEND_TIMEOUT_S} s",
        detail={"collector_error": True},
    )
    comp["chain"] = [error.to_dict()]
    comp["metrics"] = {}
    _settings(comp, public_url=PUBLIC_URL)
    _rollup(comp)


# ---- Solr: what admin/cores?action=STATUS listed

LIB_DISABLED = (
    "org.apache.solr.common.SolrException: Configset references one or more <lib/> directives, but "
    "<lib/> usage is disabled on this Solr node. Either remove all <lib/> tags from the configset, or "
    "enable use of the tag by setting the solr.config.lib.enabled system property to true"
)
"""Solr 9.8+ without ``solr.config.lib.enabled`` on a core whose configuration has ``<lib/>`` lines."""
NEW_SEARCHER = "org.apache.solr.common.SolrException: Error opening new searcher"
RELOAD_FAILED = (
    "org.apache.solr.common.SolrException: Could not load conf for core search: Can't load schema "
    "managed-schema.xml: [schema.xml] Duplicate field definition for 'dc.title'"
)


def _core_not_loaded(comp: dict[str, Any], core: str, raw: str) -> None:
    """``core`` is only in STATUS's initFailures (``raw`` is Solr's message): it did not load."""
    reason, hint = failure_reason(raw), failure_hint(raw)
    metrics = comp["metrics"]
    row = next(r for r in metrics["cores"] if r["name"] == core)
    optional = bool(row.get("optional"))
    loaded = sorted(r["name"] for r in metrics["cores"] if r["name"] != core)
    admin = _rung(comp, "solr.admin")
    admin["message"] = f"{len(loaded)} cores, 1 failed to load"
    admin["detail"].update(cores=loaded, failed=[core])
    detail = {key: value for key, value in (("optional", optional), ("hint", hint)) if value}
    _rung(comp, f"solr.core.{core}").update(
        status="warn" if optional else "crit",
        message=f"failed to load: {reason}",
        latency_ms=None,
        value=None,
        detail=detail,
    )
    row.update(docs=None, size_bytes=None, status="warn" if optional else "crit")
    metrics["init_failures"] = {core: reason}
    if core == metrics["search_core"]:  # a core that did not load is never searched
        _rung(comp, "solr.search").update(
            status="crit",
            message=f"core {core} failed to load",
            latency_ms=None,
            value=None,
            detail={"core": core},
        )
        metrics.update(search_docs=None, search_latency_ms=None)
    _rollup(comp)


def solr_failed(comp: dict[str, Any]) -> None:
    """The search core did not load, for a reason Sentinel knows the fix of."""
    _core_not_loaded(comp, "search", LIB_DISABLED)


def solr_failed_optional(comp: dict[str, Any]) -> None:
    """An optional core did not load: a warning, and no known fix."""
    _core_not_loaded(comp, "qaevent", NEW_SEARCHER)


def solr_reload_failed(comp: dict[str, Any]) -> None:
    """The search core's RELOAD failed: Solr keeps serving the old core (it is pinged and searched)."""
    reason = failure_reason(RELOAD_FAILED)
    admin = _rung(comp, "solr.admin")
    admin["message"] = f"{len(comp['metrics']['cores'])} cores, 1 failed to reload"
    admin["detail"]["reload_failed"] = ["search"]
    core = _rung(comp, "solr.core.search")
    core.update(status="warn", message=f"reload failed: {reason} ({RELOAD_NOTE})")
    core["detail"] = {**core.get("detail", {}), "reload_failed": reason}
    metrics = comp["metrics"]
    metrics["init_failures"] = {}
    metrics["reload_failures"] = {"search": reason}
    for row in metrics["cores"]:
        if row["name"] == "search":
            row["status"] = "warn"
    _rollup(comp)


def solr_reload_ping_failed(comp: dict[str, Any]) -> None:
    """The search core's reload failed and its ping answers HTTP 500: the rung is the ping's own problem
    (critical), and only its detail.reload_failed keeps the reload's reason."""
    reason = failure_reason(RELOAD_FAILED)
    admin = _rung(comp, "solr.admin")
    admin["message"] = f"{len(comp['metrics']['cores'])} cores, 1 failed to reload"
    admin["detail"]["reload_failed"] = ["search"]
    core = _rung(comp, "solr.core.search")
    core.update(status="crit", message="HTTP 500")
    core["detail"] = {**core.get("detail", {}), "reload_failed": reason}
    metrics = comp["metrics"]
    metrics["init_failures"] = {}
    metrics["reload_failures"] = {"search": reason}
    for row in metrics["cores"]:
        if row["name"] == "search":
            row["status"] = "crit"
    _rollup(comp)


OTHER_APP_RELOAD = (
    "org.apache.solr.common.SolrException: Could not load conf for core other_app: Error loading solr "
    "config from solrconfig.xml"
)
"""The failed reload of a core that is not DSpace's: another application's, on the same Solr."""


def solr_reload_unlisted(comp: dict[str, Any]) -> None:
    """Solr also serves another application's core, whose reload failed: STATUS lists it under status and
    initFailures, so the admin rung and metrics.reload_failures name it, but it has no row and no rung
    (Sentinel pings DSpace's cores only)."""
    admin = _rung(comp, "solr.admin")
    loaded = sorted([*admin["detail"].get("cores", []), "other_app"])
    admin["message"] = f"{len(loaded)} cores, 1 failed to reload"
    admin["detail"].update(cores=loaded, reload_failed=["other_app"])
    comp["metrics"]["init_failures"] = {}
    comp["metrics"]["reload_failures"] = {"other_app": failure_reason(OTHER_APP_RELOAD)}
    _rollup(comp)


# ---- rungs with a hint (detail.hint): what the situation is and what to do, in the collector's words

NGINX_HINT = (
    "No web server was found on this machine: there is no nginx unit and nothing answers on localhost:80. "
    "If DSpace is opened directly on its own port (for example :4000), switch this check off in More › "
    "Modules. If it runs behind Apache or another unit, set its name and address in More › DSpace connection."
)
SOLR_PORT_HINT = (
    "Solr has no login: anyone who can reach this port can read and change the search index. Make Solr "
    "listen on localhost only, or block the port in the firewall (Sentinel sees where it listens, not your "
    'firewall). To accept it, change this rule to "open" in More › DSpace connection.'
)
LOG_PATH = "/dspace/log/dspace.log"
LOG_HINT = (
    "The file belongs to tomcat:tomcat (mode 640): add sentinel to the group tomcat, log out and in again, "
    "then restart Sentinel."
)
OWNER_PORTS: dict[int, list[str]] = {
    22: ["0.0.0.0", "::"],
    4000: ["0.0.0.0"],
    8080: ["::"],
    8983: ["::"],
    5432: ["127.0.0.1"],
}
"""Where the test server's ports listen (SERVER-TEST T4): 22, 4000, 8080 and 8983 on every interface,
PostgreSQL on localhost, nothing on 80 or 443."""
LOOPBACK = ("127.0.0.1", "::1")


def nginx_no_web_server(comp: dict[str, Any]) -> None:
    """No nginx unit and nothing on port 80: the refused request carries the hint."""
    _rung(comp, "nginx.service").update(
        status="unknown",
        message="unit nginx not found",
        latency_ms=None,
        detail={"unit": "nginx", "state": "missing", "informational": True},
    )
    _rung(comp, "nginx.http").update(
        status="crit",
        message="connection refused (localhost:80)",
        latency_ms=None,
        detail={"url": "http://localhost", "hint": NGINX_HINT},
    )
    for rung in comp["chain"]:
        if rung["id"] not in ("nginx.service", "nginx.http"):
            rung.update(status="unknown", message="not reached", latency_ms=None, value=None, detail={})
    comp["metrics"].update(http_latency_ms=None, https_latency_ms=None, server_header=None)
    _rollup(comp)


def ports_every_interface(comp: dict[str, Any]) -> None:
    """The test server's ports (``OWNER_PORTS``), each rung worded as ``collectors/ports.py`` words it:
    ``detail.addrs`` lists the addresses, and the Solr rule (local only, but listening on every interface)
    carries the hint."""
    listening: list[dict[str, Any]] = []
    for rung in comp["chain"]:
        port = int(rung["id"].removeprefix("ports."))
        expect_kind = rung["detail"]["expect"]
        addrs = OWNER_PORTS.get(port, [])
        public = [addr for addr in addrs if addr not in LOOPBACK]
        listening += [{"port": port, "addr": a, "public": a in public, "process": None} for a in addrs]
        if not addrs:
            status, message = ("warn" if expect_kind == "open" else "unknown"), "not listening"
        elif expect_kind == "local_only" and public:
            status, message = "warn", f"exposed publicly ({', '.join(public)})"
        else:
            status, message = "ok", "listening publicly" if public else "listening on localhost only"
        detail = {"expect": expect_kind, **({"addrs": addrs} if addrs else {})}
        rung.update(status=status, message=message, latency_ms=None, value=None, detail=detail)
    _rung(comp, "ports.8983")["detail"]["hint"] = SOLR_PORT_HINT
    comp["metrics"] = {"listening": listening, "other_public": [], "source": "sockets"}
    _rollup(comp)


def logs_permission_denied(comp: dict[str, Any]) -> None:
    """Sentinel may not read DSpace's log (the file is tomcat:tomcat, mode 640): the rung carries the hint."""
    _rung(comp, "logs.dspace").update(
        status="unknown",
        message=f"permission denied ({LOG_PATH})",
        latency_ms=None,
        value=None,
        detail={"path": LOG_PATH, "hint": LOG_HINT},
    )
    comp["metrics"]["files"]["dspace"] = {
        "errors": None,
        "warnings": None,
        "last_error": None,
        "path": LOG_PATH,
        "label": "DSpace",
        "error": "permission denied",
    }
    _rollup(comp)


# ---- Security: built whole from the collector's words (SECURITY-CONTRACT), whatever the demo has

SECURITY_LABELS = {
    "security.ports": "Open ports",
    "security.firewall": "Firewall",
    "security.ssh": "SSH",
    "security.web": "Web",
    "security.updates": "Updates",
    "security.software": "Software",
    "security.files": "DSpace files",
    "security.sentinel": "Sentinel",
}
"""The eight rungs in their order, with the collector's labels."""
SECURITY_TILES = ["Open ports", "Firewall", "SSH", "Web", "Updates", "Software", "Files", "Sentinel"]
SSH_KEYS_HINT = "Keys instead of passwords: set PasswordAuthentication no once every admin has a key."
HSTS_HINT = (
    "Missing response headers: Strict-Transport-Security. They are set in the web server in front of "
    "DSpace (nginx add_header)."
)
EXPOSED_HINT = (
    "Ports other machines can reach that no rule expects: 3306 (mysqld), 9100. If a port is not meant for "
    "other machines, make its program listen on 127.0.0.1 or block it in the firewall; if it is, add a rule "
    "for it under ports in config/sentinel.yaml and restart Sentinel."
)
FIREWALL_HINT = (
    "Switch a host firewall on: for example `sudo ufw allow OpenSSH`, then `sudo ufw enable`; or make those "
    "programs listen on localhost. Sentinel cannot see the network's firewall."
)
ROOT_LOGIN_HINT = "Set PermitRootLogin prohibit-password (or no) in /etc/ssh/sshd_config and reload sshd."
UPDATES_HINT = "sudo apt update && sudo apt upgrade, then reboot when it says so."
LOCAL_CFG_HINT = "chmod 640 /dspace/config/local.cfg and keep it owned by the Tomcat user and its group."
LOG4SHELL = "affected by Log4Shell (CVE-2021-44228): upgrade to 8.11.1 or later"


def _sec(
    rung_id: str, status: str, message: str, *, hint: str = "", alert: bool = False, note: str = ""
) -> dict[str, Any]:
    """A Security rung as the collector reports it; ``status="info"`` is an informational ``unknown``."""
    detail: dict[str, Any] = {}
    if status == "info":
        status, detail["informational"] = "unknown", True
    if hint:
        detail["hint"] = hint
    if alert:
        detail["alert"] = True
    if note:
        detail["note"] = note
    return {
        "id": rung_id,
        "label": SECURITY_LABELS[rung_id],
        "status": status,
        "ts": time.time(),
        "latency_ms": None,
        "value": None,
        "message": message,
        "detail": detail,
    }


def _port(
    port: int,
    addrs: list[str],
    verdict: str,
    expected: str | None = None,
    process: str | None = None,
    proto: str = "tcp",
    note: str | None = None,
) -> dict[str, Any]:
    """One entry of ``metrics.ports``; a process is named only when Sentinel's own user runs it, and the
    collector adds a ``note`` to some verdicts (Tailscale, WireGuard, Sentinel's own port)."""
    public = any(not a.startswith("127.") and a != "::1" for a in addrs)
    pid = None if process is None else 1000 + port
    entry = {
        "port": port,
        "proto": proto,
        "addrs": addrs,
        "public": public,
        "process": process,
        "pid": pid,
        "expected": expected,
        "verdict": verdict,
    }
    if note:
        entry["note"] = note
    return entry


def _software(
    name: str, version: str, until: str | None, status: str, note: str | None = None
) -> dict[str, Any]:
    return {"name": name, "version": version, "until": until, "status": status, "note": note}


def _file(path: str, mode: str, owner: str, world_readable: bool, readable: bool = True) -> dict[str, Any]:
    return {
        "path": path,
        "mode": mode,
        "owner": owner,
        "group": owner,
        "world_readable": world_readable,
        "readable": readable,
    }


EVERY_INTERFACE = ["0.0.0.0", "::"]
HEALTHY_PORTS = [
    _port(22, EVERY_INTERFACE, "expected", "open"),
    _port(80, EVERY_INTERFACE, "expected", "open"),
    _port(443, EVERY_INTERFACE, "expected", "open"),
    _port(4000, ["0.0.0.0"], "expected", "open"),
    _port(8080, ["::"], "expected", "open"),
    _port(8983, ["127.0.0.1"], "local", "local_only"),
    _port(5432, ["127.0.0.1"], "local", "local_only"),
    _port(9765, ["127.0.0.1"], "local", None, "python3"),
]
"""The demo's server (DSpace 9.4, Ubuntu 24.04): 22, 80, 443, 4000 and 8080 open, Solr, PostgreSQL and
Sentinel itself on localhost."""


def _security(comp: dict[str, Any], chain: list[dict[str, Any]], metrics: dict[str, Any]) -> None:
    """Make ``comp`` (the demo's Security component, or the placeholder of ``ADDED``) this state."""
    now = time.time()
    comp.update(
        id="security",
        label="Security",
        emoji="🛡️",
        chain=chain,
        metrics=metrics,
        depends_on=[],
        since=now - 3600,
        checked_at=now,
    )
    _rollup(comp)


def security_healthy(comp: dict[str, Any]) -> None:
    """Nothing to warn about; SSH still accepts passwords and the answer lacks HSTS (hints only)."""
    reachable = "5 reachable from the network (22, 80, 443, 4000, 8080)"
    chain = [
        _sec("security.ports", "ok", f"8 listening · {reachable} · none unexpected"),
        _sec("security.firewall", "ok", "ufw active"),
        _sec(
            "security.ssh",
            "ok",
            "root login: prohibit-password · passwords: yes · port 22",
            hint=SSH_KEYS_HINT,
        ),
        _sec("security.web", "ok", f"HTTPS on {PUBLIC_HOST}", hint=HSTS_HINT),
        _sec("security.updates", "ok", "no pending security updates (3 other updates)"),
        _sec(
            "security.software", "ok", "DSpace 9.4 · Solr 9.8.1 · PostgreSQL 16 · Ubuntu 24.04: all supported"
        ),
        _sec("security.files", "ok", "local.cfg and sentinel.yaml are not readable by other users"),
        _sec("security.sentinel", "ok", "listens on 127.0.0.1 only"),
    ]
    metrics = {
        "ports": copy.deepcopy(HEALTHY_PORTS),
        "listening": 8,
        "reachable": 5,
        "exposed": 0,
        "firewall": {"active": True, "name": "ufw"},
        "ssh": {
            "port": 22,
            "permit_root_login": "prohibit-password",
            "password_authentication": "yes",
            "readable": True,
        },
        "web": {
            "public_url": PUBLIC_URL,
            "scheme": "https",
            "headers_missing": ["Strict-Transport-Security"],
        },
        "updates": {"pending": 3, "security": 0, "reboot_required": False, "source": "update-notifier"},
        "software": [
            _software("DSpace", "9.4", None, "ok"),
            _software("Solr", "9.8.1", None, "ok"),
            _software("PostgreSQL", "16", "2028-11-09", "ok"),
            _software("Ubuntu", "24.04", "2029-04-30", "ok"),
        ],
        "files": [
            _file("/dspace/config/local.cfg", "640", "tomcat", False, readable=False),
            _file("/dspace/config/dspace.cfg", "644", "tomcat", True),
            _file("/opt/dspace-sentinel/config/sentinel.yaml", "600", "sentinel", False),
            _file("/opt/dspace-sentinel/config/sentinel.env", "600", "sentinel", False),
        ],
    }
    _security(comp, chain, metrics)


def security_warnings(comp: dict[str, Any]) -> None:
    """Every rung warns: a new port and an unexpected one, no firewall, root login, plain HTTP, pending
    security updates and a reboot, an affected Solr and an ended Ubuntu, a world-readable local.cfg, and
    Sentinel open to the network without a password."""
    ports = [
        _port(3306, ["0.0.0.0"], "new", None, "mysqld"),
        _port(9100, ["::"], "exposed"),
        *(copy.deepcopy(p) for p in HEALTHY_PORTS[:5]),
        _port(8983, ["::"], "expected", "local_only"),
        _port(5432, ["127.0.0.1"], "local", "local_only"),
        _port(53, ["127.0.0.53"], "local", None, "systemd-resolve", proto="udp"),
    ]
    chain = [
        _sec(
            "security.ports",
            "warn",
            "10 listening · 8 reachable from the network · 2 unexpected: 3306 (mysqld), 9100",
            hint=EXPOSED_HINT,
            alert=True,
        ),
        _sec(
            "security.firewall",
            "warn",
            "no host firewall is active and 3 ports are reachable from the network without a rule",
            hint=FIREWALL_HINT,
            alert=True,
        ),
        _sec("security.ssh", "warn", "root may log in with a password", hint=ROOT_LOGIN_HINT, alert=True),
        _sec(
            "security.web", "warn", "the public address is plain HTTP: logins and sessions travel unencrypted"
        ),
        _sec(
            "security.updates",
            "warn",
            "5 security updates pending · reboot required (linux-image-6.8.0-45-generic)",
            hint=UPDATES_HINT,
        ),
        _sec("security.software", "warn", f"Solr 8.8.2 is {LOG4SHELL} · +1 more"),
        _sec(
            "security.files",
            "warn",
            "local.cfg is readable by every user on this server and holds the database password "
            "(mode 644, tomcat:tomcat)",
            hint=LOCAL_CFG_HINT,
        ),
        _sec("security.sentinel", "warn", "listens on 0.0.0.0 without a password (SENTINEL_ALLOW_OPEN)"),
    ]
    metrics = {
        "ports": ports,
        "listening": 10,
        "reachable": 8,
        "exposed": 2,
        "firewall": {"active": False, "name": None},
        "ssh": {"port": 22, "permit_root_login": "yes", "password_authentication": "yes", "readable": True},
        "web": {
            "public_url": f"http://{PUBLIC_HOST}",
            "scheme": "http",
            "headers_missing": ["X-Content-Type-Options", "X-Frame-Options or Content-Security-Policy"],
        },
        "updates": {"pending": 12, "security": 5, "reboot_required": True, "source": "update-notifier"},
        "software": [
            _software("DSpace", "9.4", None, "ok"),
            _software("Solr", "8.8.2", None, "affected", LOG4SHELL),
            _software("PostgreSQL", "14", "2026-11-12", "soon", "support ends 2026-11-12"),
            _software("Ubuntu", "20.04", "2025-05-31", "ended", "end of standard support on 2025-05-31"),
        ],
        "files": [
            _file("/dspace/config/local.cfg", "644", "tomcat", True),
            _file("/opt/dspace-sentinel/config/sentinel.yaml", "600", "sentinel", False),
        ],
    }
    _security(comp, chain, metrics)


def security_not_available(comp: dict[str, Any]) -> None:
    """Most checks have nothing to read here (no socket table, no systemd firewall, an unreadable
    sshd_config, no public URL, no update-notifier on Debian): informational, so Security stays healthy."""
    chain = [
        _sec("security.ports", "info", "not available · the socket table could not be read"),
        _sec(
            "security.firewall",
            "info",
            "no host firewall active (this server relies on the network's firewall, "
            "which Sentinel cannot see)",
        ),
        _sec("security.ssh", "info", "not available · sshd_config is not readable by Sentinel's user"),
        _sec("security.web", "info", "no public address set"),
        _sec(
            "security.updates",
            "info",
            "not available · no update information on this system (Ubuntu's update-notifier)",
        ),
        _sec("security.software", "ok", "DSpace 8.1 · Debian 12: all supported"),
        _sec("security.files", "ok", "sentinel.yaml is not readable by other users · local.cfg not found"),
        _sec("security.sentinel", "ok", "listens on 0.0.0.0 with a password"),
    ]
    metrics = {
        "ports": [],
        "firewall": {"active": False, "name": None},
        "ssh": {
            "port": 22,
            "permit_root_login": "prohibit-password",
            "password_authentication": "yes",
            "readable": False,
        },
        "web": {"public_url": None, "scheme": None, "headers_missing": []},
        "updates": {"pending": None, "security": None, "reboot_required": False, "source": None},
        "software": [_software("DSpace", "8.1", None, "ok"), _software("Debian", "12", "2028-06-30", "ok")],
        "files": [_file("/opt/dspace-sentinel/config/sentinel.yaml", "600", "sentinel", False)],
    }
    _security(comp, chain, metrics)


MANY_PORTS = 40
IPV6 = "2001:db8:85a3::8a2e:370:7334"


def security_many_ports(comp: dict[str, Any]) -> None:
    """The healthy server with a long list of ports, some on long IPv6 addresses."""
    security_healthy(comp)
    extra = [
        _port(20000 + i, [IPV6], "expected", "open", proto="udp" if i % 4 == 0 else "tcp")
        if i % 3
        else _port(20000 + i, ["127.0.0.1", "::1"], "local", None, "python3")
        for i in range(MANY_PORTS - len(HEALTHY_PORTS))
    ]
    comp["metrics"]["ports"] += extra
    comp["metrics"]["listening"] = MANY_PORTS


TAILSCALE_V4 = "100.101.102.103"  # made up, inside Tailscale's 100.64.0.0/10
TAILSCALE_V6 = "fd7a:115c:a1e0::53"  # made up, inside fd7a:115c:a1e0::/48
TAILSCALE_NOTE = "reachable through Tailscale only"
WIREGUARD_NOTE = "Tailscale's own port (WireGuard)"
PROGRAM_NAMES_NOTE = (
    "Program names are known only for Sentinel's own user; "
    "run `sudo ss -ltnp` on the server to see the others."
)
TAILSCALE_PORTS_MSG = (
    "10 listening · 6 reachable from the network · 2 through Tailscale only · 1 unexpected: 19999"
)
TAILSCALE_FIREWALL_MSG = (
    "no host firewall is active and 1 port is reachable from the network without a rule, "
    "and 1 local-only port is public"
)


def security_tailscale(comp: dict[str, Any]) -> None:
    """T21, the test server: tailscaled listens on its Tailscale addresses and on UDP 41641 (WireGuard), Solr
    on every interface under a local-only rule, Netdata (19999) without a rule; Sentinel is not root, so no
    program is named and the ports rung says why (``detail.note``). No host firewall."""
    security_healthy(comp)
    comp["chain"][0] = _sec(
        "security.ports",
        "warn",
        TAILSCALE_PORTS_MSG,
        hint=EXPOSED_HINT.replace("3306 (mysqld), 9100", "19999"),
        note=PROGRAM_NAMES_NOTE,
    )
    comp["chain"][1] = _sec(
        "security.firewall", "warn", TAILSCALE_FIREWALL_MSG, hint=FIREWALL_HINT, alert=True
    )
    comp["metrics"].update(
        ports=[
            _port(22, EVERY_INTERFACE, "expected", "open"),
            _port(4000, ["0.0.0.0"], "expected", "open"),
            _port(8080, ["::"], "expected", "open"),
            _port(8983, ["::"], "expected", "local_only"),
            _port(5432, ["127.0.0.1"], "local", "local_only"),
            _port(19999, ["0.0.0.0"], "exposed"),
            _port(40341, [TAILSCALE_V4], "tailscale", note=TAILSCALE_NOTE),
            _port(41246, [TAILSCALE_V6], "tailscale", note=TAILSCALE_NOTE),
            _port(41641, EVERY_INTERFACE, "expected", proto="udp", note=WIREGUARD_NOTE),
            _port(9765, ["127.0.0.1"], "local", None, "python3"),
        ],
        listening=10,
        reachable=6,
        exposed=1,
        new=0,
        tailscale=2,
        firewall={"active": False, "name": None},
    )
    _rollup(comp)


SCENARIOS: dict[str, tuple[str, Callable[[dict[str, Any]], None]]] = {
    "seo": ("backend", backend_seo),
    "seo-components": ("backend", backend_seo_components),
    "down": ("backend", backend_down),
    "admin-down": ("backend", backend_admin_down),
    "admin-up": ("backend", backend_admin_up),
    "hidden": ("backend", backend_hidden),
    "http-500": ("backend", backend_http_500),
    "rest-down": ("backend", backend_rest_down),
    "7x": ("backend", backend_7x),
    "7x-snapshot-cris": ("backend", backend_7x_snapshot_cris),
    "8-cris": ("backend", backend_8_cris),
    "dep-solr-slow": ("backend", backend_solr_slow),
    "shell": ("frontend", frontend_shell),
    "shell-host-rule": ("frontend", frontend_shell_host_rule),
    "shell-public-host": ("frontend", frontend_shell_public_host),
    "public-down": ("frontend", frontend_public_down),
    "public-ok": ("frontend", frontend_public_ok),
    "no-markup": ("frontend", frontend_no_markup),
    "home-down": ("frontend", frontend_home_down),
    "solr-failed": ("solr", solr_failed),
    "solr-failed-optional": ("solr", solr_failed_optional),
    "solr-reload": ("solr", solr_reload_failed),
}
"""Scenario name → (component id, the change)."""

UI_STATES: dict[str, tuple[str, Callable[[dict[str, Any]], None]]] = {
    "solr-reload-ping-failed": ("solr", solr_reload_ping_failed),
    "solr-reload-unlisted": ("solr", solr_reload_unlisted),
    "public-pending": ("frontend", frontend_public_pending),
    "public-check-failed": ("frontend", frontend_public_check_failed),
    "nginx-no-web-server": ("nginx", nginx_no_web_server),
    "ports-every-interface": ("ports", ports_every_interface),
    "logs-permission-denied": ("logs", logs_permission_denied),
    "security-healthy": ("security", security_healthy),
    "security-warnings": ("security", security_warnings),
    "security-not-available": ("security", security_not_available),
    "security-many-ports": ("security", security_many_ports),
    "security-tailscale": ("security", security_tailscale),
}
"""States without a twin in ``tests/test_collectors.py``: name → (component id, the change)."""

ADDED = {"security": ("Security", "🛡️")}
"""Components the demo may not have yet (their collector is newer than this test): a state of one adds it to
the status, after Ports, and answers its detail payload without asking the server."""


def _scenario(name: str) -> tuple[str, Callable[[dict[str, Any]], None]]:
    return SCENARIOS[name] if name in SCENARIOS else UI_STATES[name]


def _placeholder(cid: str) -> dict[str, Any]:
    """An ``ADDED`` component before its first check, as ``ComponentHealth.to_dict`` shapes it."""
    label, emoji = ADDED[cid]
    return {
        "id": cid,
        "label": label,
        "emoji": emoji,
        "status": "unknown",
        "reason": "not checked yet",
        "chain": [],
        "metrics": {},
        "depends_on": [],
        "latency_ms": None,
        "since": None,
        "checked_at": None,
    }


def _mutated(status: dict[str, Any], names: list[str]) -> dict[str, Any]:
    status = copy.deepcopy(status)
    comps = status.get("components", [])
    for name in names:
        cid, change = _scenario(name)
        if cid in ADDED and not any(comp["id"] == cid for comp in comps):
            after = next((i + 1 for i, comp in enumerate(comps) if comp["id"] == "ports"), len(comps))
            comps.insert(after, _placeholder(cid))
            status.setdefault("modules", {})[cid] = True
        for comp in comps:
            if comp["id"] == cid:
                change(comp)
    return status


def _install_mocks(page: Any, base: str, names: list[str]) -> None:
    """Rewrite /api/status, the SSE stream and /api/components/<id> for the scenarios ``names``."""
    if not names:
        return

    def status_handler(route: Any) -> None:
        resp = route.fetch()
        route.fulfill(response=resp, json=_mutated(resp.json(), names))

    def stream_handler(route: Any) -> None:
        with urllib.request.urlopen(f"{base}/api/status", timeout=10) as r:  # noqa: S310 - local test server
            status = json.loads(r.read())
        body = "retry: 600000\nevent: status\ndata: " + json.dumps(_mutated(status, names)) + "\n\n"
        route.fulfill(
            status=200, headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"}, body=body
        )

    def component_handler(route: Any) -> None:
        resp = route.fetch()
        data = resp.json()
        for name in names:
            cid, change = _scenario(name)
            if data.get("id") == cid:
                change(data)
        route.fulfill(response=resp, json=data)

    def added_handler(route: Any) -> None:
        cid = route.request.url.split("/api/components/", 1)[1].split("?", 1)[0]
        data = _placeholder(cid)
        data.update(
            latency_series_1h={"check": None, "points": []}, recent_events=[], config_used={}, affected=[]
        )
        for name in names:
            sid, change = _scenario(name)
            if sid == cid:
                change(data)
        route.fulfill(status=200, json=data)

    page.route(re.compile(r".*/api/status(\?.*)?$"), status_handler)
    page.route(re.compile(r".*/api/stream(\?.*)?$"), stream_handler)
    for cid in {_scenario(name)[0] for name in names}:
        handler = added_handler if cid in ADDED else component_handler
        page.route(re.compile(rf".*/api/components/{cid}(\?.*)?$"), handler)


# ------------------------------------------------------------------ reading the page


def _norm(text: str) -> str:
    return text.replace(NBSP, " ")


def _tile(view: Any, title: str) -> Any:
    """The "Right now" tile whose title is exactly ``title``."""
    return view.locator("#component-now .tile").filter(
        has=view.page.locator(".tile-title", has_text=re.compile(rf"^{re.escape(title)}$"))
    )


def _tile_text(view: Any, title: str) -> str:
    loc = _tile(view, title)
    return _norm(loc.first.inner_text()) if loc.count() else ""


def _lines(view: Any, title: str) -> list[str]:
    """The lines under a tile's title, top to bottom: its words, a message, how old the answer is."""
    loc = _tile(view, title)
    if not loc.count():
        return []
    return [_norm(text).strip() for text in loc.first.locator(".tile-body > .tile-sub").all_inner_texts()]


def _toned(view: Any, title: str) -> int:
    """How many lines of the tile are coloured like a warning or a failure."""
    return int(_tile(view, title).first.locator(".tile-sub--warn, .tile-sub--crit").count())


def _value(view: Any, title: str) -> str:
    """The number on the tile's right ("871 ms", or "—")."""
    return _norm(_tile(view, title).first.locator(".tile-value").first.inner_text()).strip()


def _kv(view: Any, title: str) -> dict[str, str]:
    """The key/value rows of a tile (``<dl class="kv">``)."""
    loc = _tile(view, title)
    if not loc.count():
        return {}
    keys = loc.first.locator("dt").all_inner_texts()
    values = loc.first.locator("dd").all_inner_texts()
    return {_norm(k): _norm(v) for k, v in zip(keys, values, strict=True)}


def _rung_text(scope: Any, rung_id: str) -> str:
    loc = scope.locator(f'.rung[data-rung="{rung_id}"]')
    return _norm(loc.first.inner_text()) if loc.count() else ""


def _rung_class(scope: Any, rung_id: str) -> str:
    return scope.locator(f'.rung[data-rung="{rung_id}"]').first.get_attribute("class") or ""


def _table_rows(view: Any, label: str) -> dict[str, str]:
    """The rows of the table ``label`` in the "Right now" section, by the first word of their first cell."""
    rows = view.locator(f'#component-now table[aria-label="{label}"] tbody tr')
    return {
        _norm(row.locator("th").first.inner_text()).split()[0]: _norm(row.inner_text()) for row in rows.all()
    }


def _cells(view: Any, label: str) -> dict[str, list[str]]:
    """The cells of the table ``label`` in the "Right now" section, by the first word of their row."""
    out: dict[str, list[str]] = {}
    for row in view.locator(f'#component-now table[aria-label="{label}"] tbody tr').all():
        cells = [_norm(text).strip() for text in row.locator("th, td").all_inner_texts()]
        out[cells[0].split()[0]] = cells
    return out


def _expect_setting(view: Any, key: str, value: str) -> None:
    """The "Connection used" tile, painted from the detail payload, shows the setting ``key`` as ``value``."""
    assert expect is not None
    name = view.locator("#component-connection dt", has_text=re.compile(rf"^{re.escape(key)}$"))
    expect(name.locator("xpath=following-sibling::dd[1]")).to_have_text(value, timeout=UI_TIMEOUT_MS)


REPAINT_JS = """() => {
  if (document.visibilityState !== 'visible') {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' });
  }
  document.dispatchEvent(new Event('visibilitychange'));
}"""
"""What the app hears when its tab is shown again: it asks for the status at once (a poll of /api/status,
or the stream opened again), both mocked like the rest."""


def _repaint_from_status(view: Any) -> None:
    """Have the page apply one more (mocked) status frame and wait until the frame painted the "Right now"
    tiles again: they are rebuilt, so the tile seen before leaves the page."""
    before = view.locator("#component-now .tile").first.element_handle()
    view.page.evaluate(REPAINT_JS)
    view.page.wait_for_function("(node) => !node.isConnected", arg=before, timeout=UI_TIMEOUT_MS)


def _wait_for(view: Any, title: str, text: str) -> None:
    """Wait until the tile ``title`` shows ``text`` (the mocked state has been painted)."""
    assert expect is not None
    expect(_tile(view, title).first).to_contain_text(text, timeout=UI_TIMEOUT_MS)


# ------------------------------------------------------------------ the browser, in a thread of its own


class _Chromium:
    """A headless Chromium driven from one worker thread (Playwright's sync API: one instance per thread)."""

    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="detail-states")
        self._manager: Any = None
        self._playwright: Any = None
        self.browser: Any = None
        try:
            self._pool.submit(self._start).result()
        except BaseException:
            self._pool.shutdown(wait=False)
            raise

    def _start(self) -> None:
        assert sync_playwright is not None
        self._manager = sync_playwright()
        self._playwright = self._manager.start()
        self.browser = self._playwright.chromium.launch(headless=HEADLESS)

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
def detail_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A demo server for this module (free port, private data dir, setup completed); yields its URL."""
    if not APP_JS.is_file():
        pytest.skip(f"web UI not built yet ({APP_JS.relative_to(REPO_DIR)} is missing)")
    root = tmp_path_factory.mktemp("sentinel-e2e-detail")
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
        _http(f"{base_url}/api/actions/setup", "POST", {"persona": "full"})
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@pytest.fixture(scope="module")
def chromium(detail_server: str) -> Iterator[_Chromium]:
    try:
        browser = _Chromium()
    except PlaywrightError as exc:
        pytest.skip(f"Playwright Chromium is not installed (run `playwright install chromium`): {exc}")
    try:
        yield browser
    finally:
        browser.close()


# ------------------------------------------------------------------ checks: the backend page

SEO_MSG = "DSpace 9.2 reports DOWN while healthy (a known SEO health check issue fixed in 8.5 / 9.4 / 10.1)"
ADMIN_LOGIN_NOTE = "details need a DSpace admin login"
ASKED = "asked at most every 5 minutes"
"""Under an answer the backend collector keeps: a healthy or informational one, for five minutes."""
ASKED_AGAIN = "asked again at the next check"
"""Under a warning (a DOWN, an error, a time-out): the collector never keeps one."""
AGE = r"checked \d+ (s|m|h) ago"
EOL_NOTE = "7.x is end of life — 7.6.7 was the final release"


def check_backend_demo(view: Any) -> None:
    """The demo's 9.4 as an anonymous client sees it: UP_WITH_ISSUES, and no components."""
    lines = _lines(view, "Actuator health")
    assert lines[:2] == ["Up, with non-blocking issues", ADMIN_LOGIN_NOTE], lines
    assert len(lines) == 3 and re.fullmatch(rf"{AGE} · {ASKED}", lines[2]), lines
    assert not _toned(view, "Actuator health")
    assert not view.locator('#component-now table[aria-label="Actuator components"]').count()
    assert _value(view, "DSpace") == "9.4"
    text = _tile_text(view, "DSpace")
    assert "DSpace 9.x" in text, text
    assert not any(word in text for word in ("end of life", "snapshot", "CRIS")), text
    # this demo was set up with a persona only, so its settings keep the default backend unit
    # (tomcat9): the ladder names the unit a DSpace 9.4 on Ubuntu 24.04 really runs under
    service = _rung_text(view, "backend.service")
    assert service.startswith("systemd tomcat10") and "active" in service, service
    # the answer's age moves with the page header, not only when a status frame arrives
    age = view.locator("#component-now [data-asked-at]").first
    before = age.inner_text()
    if " s ago" in _norm(before):
        assert expect is not None
        expect(age).not_to_have_text(before, timeout=5_000)


def check_backend_seo(view: Any) -> None:
    """The SEO known issue: muted in the ladder and in the tile, never a failure."""
    rung = _rung_text(view, "backend.actuator")
    assert f"known issue · {SEO_MSG}" in rung, rung
    assert "not available" not in rung.lower(), rung
    assert "rung--info" in _rung_class(view, "backend.actuator")
    spoken = view.locator('.rung[data-rung="backend.actuator"] .sr-only').first.inner_text()
    assert spoken == "Known issue", spoken
    lines = _lines(view, "Actuator health")
    assert lines[:2] == ["Down · known issue", SEO_MSG], lines
    assert len(lines) == 3 and re.fullmatch(rf"checked [34] m ago · {ASKED}", lines[2]), lines
    assert not _toned(view, "Actuator health")
    assert "9.2" in _tile_text(view, "DSpace")


def check_backend_seo_components(view: Any) -> None:
    """The same known issue when DSpace shows its components: the tile names what the SEO check flags."""
    flagged = f"{SEO_MSG}: the SEO check flags sitemap, ssr"
    assert f"known issue · {flagged}" in _rung_text(view, "backend.actuator")
    assert "rung--info" in _rung_class(view, "backend.actuator")
    lines = _lines(view, "Actuator health")
    assert lines[:2] == ["Down · known issue", flagged], lines
    assert len(lines) == 3 and re.fullmatch(rf"checked [12] m ago · {ASKED}", lines[2]), lines
    assert not _toned(view, "Actuator health")
    rows = _table_rows(view, "Actuator components")
    assert len(rows) == 7, rows
    assert "Down" in rows["seo"] and "Up" in rows["db"], rows
    # the component as a whole stays healthy: a known issue does not count
    assert "Healthy" in _norm(view.locator(".ladder-head .pill").first.inner_text())


def check_stack_seo(view: Any) -> None:
    """The Stack tab's backend ladder words the known issue like the detail page does."""
    assert expect is not None
    rung = view.page.locator('.view:not([hidden]) .rung[data-rung="backend.actuator"]').first
    expect(rung).to_contain_text("known issue", timeout=UI_TIMEOUT_MS)
    text = _norm(rung.inner_text())
    assert text.startswith("Actuator health") and f"known issue · {SEO_MSG}" in text, text
    assert "rung--info" in (rung.get_attribute("class") or "")


def check_backend_down(view: Any) -> None:
    """An anonymous DOWN outside the SEO window: a warning that says where the details are, once."""
    lines = _lines(view, "Actuator health")
    assert lines[:2] == ["Down", f"status DOWN ({ADMIN_LOGIN_NOTE})"], lines
    assert len(lines) == 3 and re.fullmatch(rf"{AGE} · {ASKED_AGAIN}", lines[2]), lines
    assert _toned(view, "Actuator health") == 2
    assert _tile(view, "Actuator health").first.locator(".tile-sub--warn").count() == 2
    assert "rung--warn" in _rung_class(view, "backend.actuator")


def check_backend_admin_down(view: Any) -> None:
    """DOWN with the components visible: the table names them, so the message is not repeated."""
    rows = _table_rows(view, "Actuator components")
    assert len(rows) == 7, rows
    assert "Up, with non-blocking issues" in rows["geoIp"] and "⚠️" in rows["geoIp"], rows
    assert "Down" in rows["solrSearchCore"] and "❌" in rows["solrSearchCore"], rows
    lines = _lines(view, "Actuator health")
    assert lines[0] == "Down" and len(lines) == 2, lines
    assert re.fullmatch(rf"{AGE} · {ASKED_AGAIN}", lines[1]), lines
    text = _tile_text(view, "Actuator health")
    assert "status DOWN" not in text and "admin login" not in text, text
    assert "status DOWN (solrSearchCore)" in _rung_text(view, "backend.actuator")


def check_backend_admin_up(view: Any) -> None:
    """UP_WITH_ISSUES with the components visible: healthy, and the table says which one has issues."""
    lines = _lines(view, "Actuator health")
    assert lines[0] == "Up, with non-blocking issues", lines
    assert len(lines) == 2 and re.fullmatch(rf"checked [23] m ago · {ASKED}", lines[1]), lines
    text = _tile_text(view, "Actuator health")
    assert "(geoIp)" not in text and "admin login" not in text, text
    rows = _table_rows(view, "Actuator components")
    assert len(rows) == 7 and "Up, with non-blocking issues" in rows["geoIp"], rows
    assert not _toned(view, "Actuator health")


def check_backend_hidden(view: Any) -> None:
    """An actuator that is not exposed says so, and nothing about an admin login."""
    lines = _lines(view, "Actuator health")
    assert lines[0] == "Not exposed (HTTP 404)", lines
    assert len(lines) == 2 and re.fullmatch(rf"checked [45] m ago · {ASKED}", lines[1]), lines
    assert "admin login" not in _tile_text(view, "Actuator health")
    assert not _toned(view, "Actuator health")
    assert "not available · not exposed (HTTP 404)" in _rung_text(view, "backend.actuator")


def check_backend_http_500(view: Any) -> None:
    lines = _lines(view, "Actuator health")
    assert lines[0] == "HTTP 500" and len(lines) == 2, lines
    assert re.fullmatch(rf"{AGE} · {ASKED_AGAIN}", lines[1]), lines
    assert "not exposed" not in _tile_text(view, "Actuator health").lower()
    assert _toned(view, "Actuator health") == 1


def check_backend_rest_down(view: Any) -> None:
    """REST is down: the actuator was never asked (no age), and the DSpace tile names no version."""
    assert _lines(view, "Actuator health") == ["Not reached"]
    assert _lines(view, "DSpace") == ["version reported by the REST API"]
    assert _value(view, "DSpace") == "—"
    assert not _tile(view, "DSpace").first.locator(".tile-value-sub").count()
    assert "HTTP 503" in _rung_text(view, "backend.rest") and "rung--crit" in _rung_class(
        view, "backend.rest"
    )
    assert "not reached" in _rung_text(view, "backend.actuator")
    assert "not reached" in _rung_text(view, "backend.search")


def check_backend_7x(view: Any) -> None:
    """7.x gets its end-of-life note as a quiet fact, and a plain UP needs no admin login."""
    assert _value(view, "DSpace") == "7.6.6"
    text = _tile_text(view, "DSpace")
    assert "DSpace 7.x" in text and EOL_NOTE in text and "snapshot" not in text, text
    assert not _toned(view, "DSpace")
    lines = _lines(view, "Actuator health")
    assert lines[0] == "Up" and len(lines) == 2, lines
    assert "admin login" not in _tile_text(view, "Actuator health")


def check_backend_7x_snapshot_cris(view: Any) -> None:
    assert _value(view, "DSpace") == "7.6.8-SNAPSHOT"
    text = _tile_text(view, "DSpace")
    assert "DSpace 7.x · snapshot build" in text, text
    assert "DSpace-CRIS cris-2023.02.07" in text and EOL_NOTE in text, text


def check_backend_8_cris(view: Any) -> None:
    assert _value(view, "DSpace") == "8.2"
    text = _tile_text(view, "DSpace")
    assert "DSpace 8.x" in text and "DSpace-CRIS cris-2024.02.04" in text, text
    assert "end of life" not in text and "snapshot" not in text, text
    assert _lines(view, "Actuator health")[0] == "Down · known issue"


SOLR_SLOW = "Solr is slow (Critical): search took 4.3 s"


def check_backend_solr_slow(view: Any) -> None:
    """A dependency that answers correctly but too slowly is called slow in the ladder, never down."""
    assert expect is not None
    rung = view.locator('.rung[data-rung="backend.dep.solr"]').first
    expect(rung).to_contain_text("Solr is slow", timeout=UI_TIMEOUT_MS)
    text = _rung_text(view, "backend.dep.solr")
    assert text.startswith("Solr dependency") and SOLR_SLOW in text, text
    assert "is down" not in text, text
    assert "rung--warn" in _rung_class(view, "backend.dep.solr")  # a dependency is at most a warning
    head = _norm(view.locator(".ladder-head").first.inner_text())
    assert SOLR_SLOW in head and "Warning" in head, head
    # the backend's own rungs are untouched: it answered, and its actuator was asked as usual
    assert "rung--ok" in _rung_class(view, "backend.rest")
    assert _lines(view, "Actuator health")[0] == "Up, with non-blocking issues"


# ------------------------------------------------------------------ checks: the frontend page

SSR_WORDS = "Server-side (SSR): the homepage arrives already rendered"
SHELL_NOTE = "client-side rendering only (server-side rendering is switched off or failing)"
"""CSR_NOTE as the UI words it: humanize() reads " is off" as " is switched off", here and in the ladder."""
HOST_RULE = "renders on the server only for the host in its ui.baseUrl (set dspace.public_url)"
UI_HOST = "localhost:4000 (the UI URL's host)"


def check_frontend_demo(view: Any) -> None:
    """The demo's server-side render: what the page names, and the host it was asked for."""
    assert _lines(view, "Rendering") == [SSR_WORDS]
    assert _kv(view, "Rendering") == {"Generator": "DSpace 9.4", "Angular": "20.3.30", "Host header": UI_HOST}


def check_shell(view: Any) -> None:
    """The client-side shell: the rung's note in the tile too, and what a shell cannot name."""
    assert _lines(view, "Rendering") == ["Client-side shell", SHELL_NOTE]
    assert SHELL_NOTE in _rung_text(view, "frontend.home")
    assert _kv(view, "Rendering") == {
        "Generator": "not in the page",
        "Angular": "not in the page (only a server render names it)",
        "Host header": UI_HOST,
    }
    assert "rung--ok" in _rung_class(view, "frontend.home")  # a shell is a note, not a failure


def check_shell_host_rule(view: Any) -> None:
    lines = _lines(view, "Rendering")
    assert lines[0] == "Client-side shell" and len(lines) == 2, lines
    assert lines[1] == f"client-side rendering only: DSpace 9.4 {HOST_RULE}", lines
    assert _kv(view, "Rendering")["Host header"] == UI_HOST


def check_shell_public_host(view: Any) -> None:
    """The shell served to ui.baseUrl's own host: SSR is off or failing, and the Host rule is not named."""
    assert _lines(view, "Rendering") == ["Client-side shell", SHELL_NOTE]
    text = _tile_text(view, "Rendering")
    assert "ui.baseUrl" not in text and "set dspace.public_url" not in text, text
    assert SHELL_NOTE in _rung_text(view, "frontend.home")
    assert _kv(view, "Rendering")["Host header"] == f"{PUBLIC_HOST} (the UI URL's host)"
    _expect_setting(view, "UI URL", PUBLIC_URL)


def check_no_markup(view: Any) -> None:
    """A page without the DSpace UI in it: a warning, and the tile says nothing was recognised."""
    assert _lines(view, "Rendering") == ["Not recognised in the homepage"]
    kv = _kv(view, "Rendering")
    assert kv["Generator"] == "not in the page" and kv["Angular"] == "not in the page", kv
    assert NO_MARKUP in _rung_text(view, "frontend.home")
    assert "rung--warn" in _rung_class(view, "frontend.home")
    assert _lines(view, "Page title") == [BOT_CHECK_TITLE]


def check_home_down(view: Any) -> None:
    """No page came back: nothing was read from it, and no latency is claimed."""
    assert _lines(view, "Rendering") == ["Read once the homepage answers"]
    kv = _kv(view, "Rendering")
    assert kv["Generator"] == "—" and kv["Angular"] == "—", kv
    assert _value(view, "Homepage") == "—"
    assert not _tile(view, "Page title").count()
    assert "timed out after 5 s (localhost:4000)" in _rung_text(view, "frontend.home")
    assert "rung--crit" in _rung_class(view, "frontend.home")


def check_public_down(view: Any) -> None:
    """A configured public URL that fails says why, in the rung's tone; it never reads 'not set'."""
    text = _tile_text(view, "Public URL")
    assert f"connection refused ({PUBLIC_HOST}:443)" in text, text
    assert "not set" not in text and "add it in More" not in text, text
    assert _value(view, "Public URL") == "—"
    crit = _tile(view, "Public URL").first.locator(".tile-sub--crit")
    assert crit.count() == 1 and "connection refused" in crit.inner_text()
    assert f"connection refused ({PUBLIC_HOST}:443)" in _rung_text(view, "frontend.public")
    _expect_setting(view, "Public URL", PUBLIC_URL)


def check_public_ok(view: Any) -> None:
    """The public URL answers: its latency, and the homepage is asked for as the public host."""
    assert _lines(view, "Public URL") == ["through nginx and TLS, as visitors see it"]
    assert _value(view, "Public URL") == "871 ms"
    assert _kv(view, "Rendering")["Host header"] == f"{PUBLIC_HOST} (the public URL's host)"


def check_public_not_set(view: Any) -> None:
    """The demo has no dspace.public_url: the tile says where to add one."""
    assert _lines(view, "Public URL") == ["not set — add it in More › DSpace connection"]
    assert not view.locator('.rung[data-rung="frontend.public"]').count()
    _expect_setting(view, "Public URL", "not set")


def check_public_pending(view: Any) -> None:
    """A public URL is set but was never checked: "not checked yet", never "not set" (the setting decides)."""
    assert _lines(view, "Public URL") == ["not checked yet"]
    assert _value(view, "Public URL") == "—"
    _expect_setting(view, "Public URL", PUBLIC_URL)


def check_public_check_failed(view: Any) -> None:
    """After a failed frontend check a set public URL is "not checked yet", also once a status frame
    (which carries no settings) has painted the tiles again."""
    assert _lines(view, "Public URL") == ["not checked yet"]
    assert f"check timed out after {FRONTEND_TIMEOUT_S} s" in _rung_text(view, "frontend.error")
    _repaint_from_status(view)
    assert _lines(view, "Public URL") == ["not checked yet"]
    _expect_setting(view, "Public URL", PUBLIC_URL)


# ------------------------------------------------------------------ checks: the Solr page

LIB_REASON = (
    "Configset references one or more <lib/> directives, but <lib/> usage is disabled on this Solr node. "
    "Either remove all <lib/> tags from the configset, or enable use of the tag by setting the solr.conf…"
)
LIB_HINT = 'start Solr with SOLR_OPTS="-Dsolr.config.lib.enabled=true" (Solr 9.8+)'
LIB_HINT_LINE = 'Start Solr with SOLR_OPTS="-Dsolr.config.lib.enabled=true" (Solr 9.8+)'
"""The same fix as the core rung's hint in the ladder, where it starts a sentence of its own."""
RELOAD_REASON = (
    "Could not load conf for core search: Can't load schema managed-schema.xml: [schema.xml] "
    "Duplicate field definition for 'dc.title'"
)
REQUIRED_CORES = ("search", "statistics", "oai", "authority")


def _notes(view: Any, label: str) -> list[list[str]]:
    """The notes under the cores table (the list ``label``): each one's lines."""
    items = view.locator(f'#component-now ul[aria-label="{label}"] li')
    return [[_norm(text) for text in item.locator(".tile-sub").all_inner_texts()] for item in items.all()]


def check_solr_demo(view: Any) -> None:
    """The demo's six cores: the two DSpace 9 installs but does not need are tagged optional."""
    rows = _table_rows(view, "Solr cores")
    assert list(rows) == [*REQUIRED_CORES, "qaevent", "suggestion"], rows
    assert "optional" in rows["qaevent"] and "optional" in rows["suggestion"], rows
    assert not any("optional" in rows[core] for core in REQUIRED_CORES), rows
    assert _lines(view, "Cores") == ["6 cores pinged every check"]
    assert not view.locator("#component-now .note-list").count()


def check_solr_failed(view: Any) -> None:
    """A core that did not load: "Failed" in the table, why under it, and the fix Sentinel knows."""
    rows = _table_rows(view, "Solr cores")
    assert re.search(r"❌\s*Failed$", rows["search"].strip()), rows
    assert _lines(view, "Cores") == ["5 cores pinged every check · 1 failed to load"]
    assert _notes(view, "Cores that failed to load") == [
        [f"search failed to load: {LIB_REASON}", f"Fix: {LIB_HINT}"]
    ]
    assert _value(view, "Search query") == "—" and _value(view, "Search core") == "—"
    assert "5 cores, 1 failed to load" in _rung_text(view, "solr.admin")
    assert "core search failed to load" in _rung_text(view, "solr.search")
    assert "Critical" in _norm(view.locator(".ladder-head .pill").first.inner_text())
    # the core's rung carries the same fix as its hint, under its message in the ladder
    _expect_hint(view, "solr.core.search", LIB_HINT_LINE)


def check_solr_failed_optional(view: Any) -> None:
    """An optional core that did not load is a warning, and an unknown reason gets no "Fix:" line."""
    rows = _table_rows(view, "Solr cores")
    assert re.search(r"⚠️\s*Failed$", rows["qaevent"].strip()) and "optional" in rows["qaevent"], rows
    assert _lines(view, "Cores") == ["5 cores pinged every check · 1 failed to load"]
    assert _notes(view, "Cores that failed to load") == [
        ["qaevent failed to load: Error opening new searcher"]
    ]
    assert "Fix:" not in _tile_text(view, "Cores")
    assert "Warning" in _norm(view.locator(".ladder-head .pill").first.inner_text())


def check_solr_reload_failed(view: Any) -> None:
    """A core whose reload failed keeps its row and its numbers: it still serves until a restart."""
    rows = _table_rows(view, "Solr cores")
    assert rows["search"].strip().endswith("Warning") and "Failed" not in rows["search"], rows
    assert _lines(view, "Cores") == ["6 cores pinged every check · 1 failed to reload"]
    assert _notes(view, "Cores that failed to reload") == [
        [
            f"search failed to reload: {RELOAD_REASON}",
            "Still serving with its previous configuration; a restart would not load it.",
        ]
    ]
    assert "failed to load" not in _tile_text(view, "Cores")
    assert "failed to reload" in _rung_text(view, "solr.admin")
    assert "reload failed: " in _rung_text(view, "solr.core.search")
    assert _value(view, "Search query") != "—"  # still searched


STILL_SERVING = "Still serving with its previous configuration; a restart would not load it."
OTHER_APP_REASON = "Could not load conf for core other_app: Error loading solr config from solrconfig.xml"


def check_solr_reload_unlisted(view: Any) -> None:
    """The failed reload of a core without a row is noted under the table, but neither counted in the
    subtitle nor coloured: none of the pinged cores failed."""
    assert _lines(view, "Cores") == ["6 cores pinged every check"]
    assert _notes(view, "Cores that failed to reload") == [
        [f"other_app failed to reload: {OTHER_APP_REASON}", STILL_SERVING]
    ]
    assert not _toned(view, "Cores")
    assert "other_app" not in _table_rows(view, "Solr cores")
    assert "7 cores, 1 failed to reload" in _rung_text(view, "solr.admin")


def check_solr_reload_ping_failed(view: Any) -> None:
    """A core whose reload failed and whose ping fails is not "still serving": the note gives the reason in
    the row's tone and says only that a restart would not load it either."""
    rows = _table_rows(view, "Solr cores")
    assert rows["search"].strip().endswith("Critical") and "Failed" not in rows["search"], rows
    assert _lines(view, "Cores") == ["6 cores pinged every check · 1 failed to reload"]
    assert _notes(view, "Cores that failed to reload") == [
        [f"search failed to reload: {RELOAD_REASON}", "A restart would not load it either."]
    ]
    assert "Still serving" not in _tile_text(view, "Cores")
    notes = _tile(view, "Cores").first.locator(".note-list")
    assert notes.locator(".tile-sub--crit").count() == 1 and not notes.locator(".tile-sub--warn").count()
    assert "HTTP 500" in _rung_text(view, "solr.core.search")


# ------------------------------------------------------------------ checks: a rung's hint


def _hint(view: Any, rung_id: str) -> Any:
    """The hint line of the rung ``rung_id`` (none when the rung has no ``detail.hint``)."""
    return view.locator(f'.rung[data-rung="{rung_id}"] .rung-hint')


def _expect_hint(view: Any, rung_id: str, text: str) -> None:
    """The rung ``rung_id`` shows ``text`` as its hint: one line right under its message, in the muted ink
    of the page header's "Checked …" whatever the rung's own tone."""
    assert expect is not None
    hint = _hint(view, rung_id)
    expect(hint).to_have_count(1, timeout=UI_TIMEOUT_MS)
    assert _norm(hint.inner_text()) == text
    assert view.locator(f'.rung[data-rung="{rung_id}"] .rung-main > .rung-msg + .rung-hint').count() == 1
    colour = "(node) => getComputedStyle(node).color"
    assert hint.evaluate(colour) == view.page.locator("#component-checked").first.evaluate(colour)


def check_nginx_no_web_server(view: Any) -> None:
    """SERVER-TEST T7: no web server here. Under the refused request, what that means and what to do."""
    _expect_hint(view, "nginx.http", NGINX_HINT)
    assert "connection refused (localhost:80)" in _rung_text(view, "nginx.http")
    assert "rung--crit" in _rung_class(view, "nginx.http")
    assert "not available · unit nginx not found" in _rung_text(view, "nginx.service")
    assert not _hint(view, "nginx.service").count()


def check_ports_every_interface(view: Any) -> None:
    """T14: the Solr port listens on every interface. Why that matters, how to fix it, how to accept it.
    T4: the Listening table's Address column names where each port listens (the rungs' ``detail.addrs``)."""
    _expect_hint(view, "ports.8983", SOLR_PORT_HINT)
    assert "exposed publicly (::)" in _rung_text(view, "ports.8983")
    assert "rung--warn" in _rung_class(view, "ports.8983")
    assert not _hint(view, "ports.5432").count()
    addresses = {name: cells[1] for name, cells in _cells(view, "Ports").items()}
    assert addresses == {
        "SSH": "0.0.0.0, ::",
        "HTTP": "—",
        "HTTPS": "—",
        "Angular": "0.0.0.0",
        "Backend": "::",
        "Solr": "::",
        "PostgreSQL": "127.0.0.1",
    }, addresses


def check_ports_demo(view: Any) -> None:
    """The demo names one address per port (``detail.addr``): the Address column shows it too."""
    assert expect is not None
    table = view.locator('#component-now table[aria-label="Ports"]')
    expect(table.locator("tbody tr").first).to_contain_text("SSH", timeout=UI_TIMEOUT_MS)
    cells = _cells(view, "Ports")
    assert cells["SSH"][1] == "0.0.0.0" and cells["PostgreSQL"][1] == "127.0.0.1", cells


def check_logs_permission_denied(view: Any) -> None:
    """T5: a log Sentinel may not read. Under "permission denied", who owns it and what to do."""
    _expect_hint(view, "logs.dspace", LOG_HINT)
    assert f"permission denied ({LOG_PATH})" in _rung_text(view, "logs.dspace")
    assert not _hint(view, "logs.tomcat").count()


# ------------------------------------------------------------------ checks: the Security page

SECURITY = "#/stack/security"
SETTINGS_NOTE = "Reads the port rules, dspace.public_url and dspace.install_dir from sentinel.yaml."
TABLES_SCROLL_JS = """() => [...document.querySelectorAll('.view--sub #component-now .table-wrap')]
  .map((wrap) => wrap.scrollWidth - wrap.clientWidth)"""
"""How far each table of the Security tiles scrolls sideways inside its tile (0: every column fits)."""


def _rung_ids(view: Any) -> list[str]:
    return view.locator(".ladder .rung[data-rung]").evaluate_all(
        "(rungs) => rungs.map((r) => r.dataset.rung)"
    )


def _rung_msg(view: Any, rung_id: str) -> str:
    return _norm(view.locator(f'.rung[data-rung="{rung_id}"] .rung-msg').first.inner_text()).strip()


def _head(view: Any, title: str) -> Any:
    """The first line under a tile's title: its rung's message."""
    return _tile(view, title).first.locator(".tile-body > .tile-sub").first


def _expect_security_page(view: Any, status: str) -> None:
    """The ladder lists the eight rungs in order and its head reads ``status``; the tiles follow in the
    contract's order; ladder and tiles both run across the page, and no table scrolls sideways."""
    assert _rung_ids(view) == list(SECURITY_LABELS), _rung_ids(view)
    titles = [_norm(text).strip() for text in view.locator("#component-now .tile-title").all_inner_texts()]
    assert titles == SECURITY_TILES, titles
    pill = _norm(view.locator(".ladder-head .pill").first.inner_text())
    assert pill.split("·")[0].strip().endswith(status), pill
    ladder_box = view.locator(".ladder").first.bounding_box()
    now_box = view.locator("#component-now").bounding_box()
    assert ladder_box and now_box and abs(ladder_box["width"] - now_box["width"]) <= 1, (ladder_box, now_box)
    scrolls = view.page.evaluate(TABLES_SCROLL_JS)
    assert len(scrolls) == 2 and all(px <= 1 for px in scrolls), scrolls


def check_security_healthy(view: Any) -> None:
    """The demo's server: every rung ok, the ports expected or on localhost (plain pills), the hints of
    SSH (passwords) and Web (HSTS), and nothing in the tiles toned."""
    assert expect is not None
    _expect_security_page(view, "Healthy")
    for rung_id in SECURITY_LABELS:
        assert "rung--ok" in _rung_class(view, rung_id), rung_id
    _expect_hint(view, "security.ssh", SSH_KEYS_HINT)
    _expect_hint(view, "security.web", HSTS_HINT)
    assert not _hint(view, "security.ports").count()
    cells = _cells(view, "Open ports")
    assert list(cells) == ["22", "80", "443", "4000", "8080", "5432", "8983", "9765"], list(cells)
    assert cells["22"] == ["22", "—", "0.0.0.0, ::", "yes", "open", "Expected"], cells["22"]
    assert cells["5432"] == ["5432", "—", "127.0.0.1", "no", "local only", "Local"], cells["5432"]
    assert cells["9765"] == ["9765", "python3", "127.0.0.1", "no", "no rule", "Local"], cells["9765"]
    assert not view.locator('#component-now table[aria-label="Open ports"] .pill--warn').count()
    assert not view.locator("#component-now .tile-sub--warn, #component-now .tile-sub--crit").count()
    assert _kv(view, "Firewall") == {"Host firewall": "ufw (active)", "Reachable without a rule": "none"}
    assert _kv(view, "SSH") == {"Root login": "prohibit-password", "Passwords": "yes", "Port": "22"}
    web = {
        "Public address": PUBLIC_URL,
        "Encryption": "HTTPS",
        "Missing headers": "Strict-Transport-Security",
    }
    assert _kv(view, "Web") == web, _kv(view, "Web")
    updates = {
        "Security updates": "0",
        "Other updates": "3",
        "Reboot required": "no",
        "Read from": "update-notifier",
    }
    assert _kv(view, "Updates") == updates, _kv(view, "Updates")
    assert _kv(view, "Files") == {
        "local.cfg": "mode 640 · tomcat:tomcat",
        "dspace.cfg": "mode 644 · tomcat:tomcat · readable by every user",
        "sentinel.yaml": "mode 600 · sentinel:sentinel",
        "sentinel.env": "mode 600 · sentinel:sentinel",
    }, _kv(view, "Files")
    assert _kv(view, "Sentinel") == {"Listens on": "127.0.0.1"}
    software = _cells(view, "Software")
    assert software["Ubuntu"][:3] == ["Ubuntu", "24.04", "2029-04-30"], software
    assert software["DSpace"][2] == "—", software
    assert all(row[3].endswith("Supported") for row in software.values()), software
    assert not view.locator('#component-now ul[aria-label="Support notes"]').count()
    expect(view.locator("#component-connection .section-title")).to_have_text("Settings used")
    expect(view.locator("#component-connection")).to_contain_text(SETTINGS_NOTE)


def check_security_warnings(view: Any) -> None:
    """Every rung warns, each tile's first line in the warning tone; the new and the unexpected port lead
    the table with warning pills; a command in a hint keeps its first letter, and one between backticks
    reads in the monospace face."""
    _expect_security_page(view, "Warning")
    for rung_id in SECURITY_LABELS:
        assert "rung--warn" in _rung_class(view, rung_id), rung_id
    for title in SECURITY_TILES:
        assert "tile-sub--warn" in (_head(view, title).get_attribute("class") or ""), title
    _expect_hint(view, "security.ports", EXPOSED_HINT)
    _expect_hint(view, "security.firewall", FIREWALL_HINT.replace("`", ""))
    commands = _hint(view, "security.firewall").locator(".mono").all_inner_texts()
    assert commands == ["sudo ufw allow OpenSSH", "sudo ufw enable"], commands
    _expect_hint(view, "security.ssh", ROOT_LOGIN_HINT)
    _expect_hint(view, "security.updates", UPDATES_HINT)
    _expect_hint(view, "security.files", LOCAL_CFG_HINT)
    cells = _cells(view, "Open ports")
    assert list(cells) == ["3306", "9100", "22", "80", "443", "4000", "8080", "8983", "53", "5432"], list(
        cells
    )
    assert cells["3306"][:5] == ["3306", "mysqld", "0.0.0.0", "yes", "no rule"], cells["3306"]
    assert cells["3306"][5].endswith("New") and cells["9100"][5].endswith("Exposed"), cells
    assert cells["8983"] == ["8983", "—", "::", "yes", "local only", "Expected"], cells["8983"]
    assert cells["53"][0].split() == ["53", "udp"], cells["53"]
    assert cells["53"][1:] == ["systemd-resolve", "127.0.0.53", "no", "no rule", "Local"], cells["53"]
    assert view.locator('#component-now table[aria-label="Open ports"] .pill--warn').count() == 2
    assert _kv(view, "Firewall") == {"Host firewall": "none active", "Reachable without a rule": "2 ports"}
    assert _kv(view, "SSH")["Root login"] == "yes"
    assert _kv(view, "Web") == {
        "Public address": f"http://{PUBLIC_HOST}",
        "Encryption": "none (plain HTTP)",
        "Missing headers": "X-Content-Type-Options, X-Frame-Options or Content-Security-Policy",
    }, _kv(view, "Web")
    updates = {
        "Security updates": "5",
        "Other updates": "7",
        "Reboot required": "yes",
        "Read from": "update-notifier",
    }
    assert _kv(view, "Updates") == updates, _kv(view, "Updates")
    assert _kv(view, "Files")["local.cfg"] == "mode 644 · tomcat:tomcat · readable by every user"
    assert _kv(view, "Sentinel") == {"Listens on": "0.0.0.0", "Login": "not set", "Token": "not set"}
    software = _cells(view, "Software")
    assert software["Solr"][3].endswith("Affected") and software["Ubuntu"][3].endswith("Ended"), software
    assert software["PostgreSQL"][2:] == ["2026-11-12", "Ends soon"], software
    notes = view.locator('#component-now ul[aria-label="Support notes"] > li')
    assert [_norm(text).strip() for text in notes.all_inner_texts()] == [
        f"Solr 8.8.2: {LOG4SHELL}",
        "PostgreSQL 14: support ends 2026-11-12",
        "Ubuntu 20.04: end of standard support on 2025-05-31",
    ]
    assert notes.locator(".tile-sub--warn").count() == 2


def check_security_not_available(view: Any) -> None:
    """Five checks have nothing to read here: muted rungs worded by the collector (no second "not
    available"), Security stays healthy, and the tiles leave out what they do not know."""
    assert expect is not None
    _expect_security_page(view, "Healthy")
    info = ("security.ports", "security.firewall", "security.ssh", "security.web", "security.updates")
    for rung_id in SECURITY_LABELS:
        assert ("rung--info" in _rung_class(view, rung_id)) == (rung_id in info), rung_id
    assert _rung_msg(view, "security.ports") == "not available · the socket table could not be read"
    firewall = (
        "no host firewall active (this server relies on the network's firewall, which Sentinel cannot see)"
    )
    assert _rung_msg(view, "security.firewall") == firewall
    assert _rung_msg(view, "security.web") == "no public address set"
    head = _norm(view.locator(".ladder-head .tile-sub").first.inner_text()).strip()
    assert head == "3 checks passed · 5 not available", head
    assert not view.locator("#component-now .tile-sub--warn, #component-now .tile-sub--crit").count()
    ssh = _norm(_head(view, "SSH").inner_text()).strip()
    assert ssh == "not available · sshd_config is not readable by Sentinel's user", ssh
    empty = view.locator('#component-now table[aria-label="Open ports"] .table-empty')
    expect(empty).to_have_text("No ports to show.")
    assert _kv(view, "SSH") == {}
    assert _kv(view, "Firewall") == {"Host firewall": "none active"}
    assert _kv(view, "Web") == {"Public address": "not set"}
    assert _kv(view, "Updates") == {"Reboot required": "no"}
    assert _kv(view, "Sentinel") == {"Listens on": "0.0.0.0", "Login": "not set", "Token": "set"}


def check_security_many_ports(view: Any) -> None:
    """Forty ports, some on long IPv6 addresses: the table grows down the page, never sideways."""
    _expect_security_page(view, "Healthy")
    rows = view.locator('#component-now table[aria-label="Open ports"] tbody tr')
    assert rows.count() == MANY_PORTS
    assert rows.filter(has_text=IPV6).count() == len(
        [i for i in range(MANY_PORTS - len(HEALTHY_PORTS)) if i % 3]
    )


def _spaced(text: str) -> str:
    """Every run of white space as one space (a pill and the note beside it may sit on two lines)."""
    return " ".join(text.split())


def check_security_tailscale(view: Any) -> None:
    """T21: tailscaled's ports read "Tailscale" (a plain pill, with the collector's note beside it) right
    after the one unexpected port, WireGuard's UDP port is expected with its note, and only Netdata warns;
    the Firewall tile shows the backend's count; the note on program names sits under the table, its
    command in the monospace face."""
    _expect_security_page(view, "Warning")
    assert _rung_msg(view, "security.ports") == TAILSCALE_PORTS_MSG
    assert _rung_msg(view, "security.firewall") == TAILSCALE_FIREWALL_MSG
    cells = _cells(view, "Open ports")
    order = ["19999", "40341", "41246", "22", "4000", "8080", "8983", "41641", "5432", "9765"]
    assert list(cells) == order, list(cells)
    assert cells["19999"][5].endswith("Exposed"), cells["19999"]
    assert cells["40341"][:5] == ["40341", "—", TAILSCALE_V4, "yes", "no rule"], cells["40341"]
    for port in ("40341", "41246"):
        assert _spaced(cells[port][5]) == f"Tailscale {TAILSCALE_NOTE}", cells[port]
    assert cells["41641"][0].split() == ["41641", "udp"], cells["41641"]
    assert _spaced(cells["41641"][5]) == f"Expected {WIREGUARD_NOTE}", cells["41641"]
    assert cells["22"][5] == "Expected", cells["22"]
    assert view.locator('#component-now table[aria-label="Open ports"] .pill--warn').count() == 1
    assert _kv(view, "Firewall") == {"Host firewall": "none active", "Reachable without a rule": "1 port"}
    note = view.locator('#component-now ul[aria-label="Port notes"] > li')
    assert note.count() == 1
    assert _spaced(_norm(note.inner_text())) == PROGRAM_NAMES_NOTE.replace("`", "")
    assert note.locator(".mono").all_inner_texts() == ["sudo ss -ltnp"]


def check_security_home(view: Any) -> None:
    """Home: Security's tile like any other component's, right after Ports, with its reason."""
    assert expect is not None
    tile = view.locator('#home-stack [data-key="security"]')
    expect(tile).to_contain_text("2 unexpected: 3306 (mysqld), 9100", timeout=UI_TIMEOUT_MS)
    assert _norm(tile.locator(".tile-title").inner_text()) == "Security"
    assert tile.get_attribute("href") == "#/stack/security"
    keys = view.locator("#home-stack .section-body > [data-key]").evaluate_all(
        "(tiles) => tiles.map((tile) => tile.dataset.key)"
    )
    assert keys.index("security") == keys.index("ports") + 1, keys


def check_security_stack(view: Any) -> None:
    """The Stack tab: Security's ladder, its eight rungs short (hints are for its page), linking there."""
    assert expect is not None
    ladder_el = view.locator('.ladder[data-component="security"]')
    expect(ladder_el.locator(".rung[data-rung]")).to_have_count(len(SECURITY_LABELS), timeout=UI_TIMEOUT_MS)
    assert not ladder_el.locator(".rung-hint").count()
    assert ladder_el.locator(".ladder-head").get_attribute("href") == "#/stack/security"


# ------------------------------------------------------------------ checks: key/value lists

KV_TILES_JS = """(titles) => {
  const view = [...document.querySelectorAll('#views .view:not([hidden])')].pop();
  if (!view) return null;
  const px = (value) => Math.round(parseFloat(value) * 10) / 10 || 0;
  const tiles = [...view.querySelectorAll('.tile--block')].map((tile) => {
    const body = tile.querySelector(':scope > .tile-body');
    const content = [...tile.children].filter((node) => !node.matches('.tile-icon, .tile-body, .tile-end'));
    if (!body || !content.length) return null;
    const list = tile.querySelector('dl.kv');
    const first = list || content[0];
    const last = list || content[content.length - 1];
    const style = getComputedStyle(tile);
    return {
      title: tile.querySelector('.tile-title').textContent,
      kind: list ? 'kv' : content[0].className.split(' ')[0],
      rows: list ? list.querySelectorAll('dd').length : 0,
      margins: list ? [getComputedStyle(list).marginTop, getComputedStyle(list).marginBottom] : null,
      above: px(first.getBoundingClientRect().top - body.getBoundingClientRect().bottom),
      below: px(tile.getBoundingClientRect().bottom - last.getBoundingClientRect().bottom),
      spacing: px(style.rowGap) + px(getComputedStyle(content[0]).marginTop),
      edge: px(style.paddingBottom) + px(style.borderBottomWidth),
    };
  }).filter(Boolean);
  const filled = tiles.filter((t) => t.kind === 'kv' && t.rows > 0).map((t) => t.title);
  return titles.every((title) => filled.includes(title)) ? tiles : null;
}"""
"""The block tiles of the visible view (content stacked under the title row) once the key/value lists of
the tiles ``titles`` are filled: each one's distance from the title row to its content (``above``) and from
the content to the tile's edge (``below``), beside what the tile's own spacing accounts for (``spacing``:
its row gap and the content's top margin; ``edge``: its padding and border)."""


def check_kv_lists(*titles: str) -> Callable[[Any], None]:
    """The key/value lists (``<dl class="kv">``) of the tiles ``titles`` sit right under their title row.

    A list keeps no browser margin, so only the tile's own spacing separates it from the title row and
    from the tile's edge, which puts it where a chip list, a meter list or a table sits in the tiles
    beside it.
    """

    def check(view: Any) -> None:
        handle = view.page.wait_for_function(KV_TILES_JS, arg=list(titles), timeout=UI_TIMEOUT_MS)
        tiles = handle.json_value()
        lists = [tile for tile in tiles if tile["kind"] == "kv"]
        assert sorted(tile["title"] for tile in lists) == sorted(titles), tiles
        for tile in lists:
            assert tile["margins"] == ["0px", "0px"], tile
            assert abs(tile["above"] - tile["spacing"]) <= 0.5, tile
            assert abs(tile["below"] - tile["edge"]) <= 0.5, tile
        for other in (tile for tile in tiles if tile["kind"] in ("chip-list", "meter-list", "table-wrap")):
            assert abs(other["above"] - lists[0]["above"]) <= 0.5, (other, lists[0])

    return check


# ------------------------------------------------------------------ cases


@dataclass(frozen=True)
class Case:
    name: str
    route: str
    scenarios: tuple[str, ...]
    check: Callable[[Any], None]
    ready: tuple[str, str] | None = None
    """``(tile title, text)`` the page shows once the (mocked) state is painted."""
    shot: bool = False
    """Save ``detail-<name>.png`` (a state the latest fixes changed)."""


BACKEND = "#/stack/backend"
FRONTEND = "#/stack/frontend"
SOLR = "#/stack/solr"
ACTUATOR = "Actuator health"

CASES: list[Case] = [
    # ---- the backend page
    Case("backend-demo", BACKEND, (), check_backend_demo, ready=(ACTUATOR, "non-blocking")),
    Case("backend-seo-known-issue", BACKEND, ("seo",), check_backend_seo, ready=(ACTUATOR, "known issue")),
    Case(
        "backend-seo-known-issue-components",
        BACKEND,
        ("seo-components",),
        check_backend_seo_components,
        ready=(ACTUATOR, "the SEO check flags"),
        shot=True,
    ),
    Case("stack-seo-known-issue", "#/stack", ("seo",), check_stack_seo),
    Case("backend-down", BACKEND, ("down",), check_backend_down, ready=(ACTUATOR, "status DOWN"), shot=True),
    Case(
        "backend-admin-down",
        BACKEND,
        ("admin-down",),
        check_backend_admin_down,
        ready=(ACTUATOR, "solrSearchCore"),
    ),
    Case(
        "backend-admin-up-with-issues",
        BACKEND,
        ("admin-up",),
        check_backend_admin_up,
        ready=(ACTUATOR, "geoIp"),
    ),
    Case(
        "backend-actuator-not-exposed",
        BACKEND,
        ("hidden",),
        check_backend_hidden,
        ready=(ACTUATOR, "Not exposed"),
    ),
    Case(
        "backend-actuator-http-500",
        BACKEND,
        ("http-500",),
        check_backend_http_500,
        ready=(ACTUATOR, "HTTP 500"),
    ),
    Case(
        "backend-rest-down", BACKEND, ("rest-down",), check_backend_rest_down, ready=(ACTUATOR, "Not reached")
    ),
    Case("backend-7x", BACKEND, ("7x",), check_backend_7x, ready=("DSpace", "7.6.6")),
    Case(
        "backend-7x-snapshot-cris",
        BACKEND,
        ("7x-snapshot-cris",),
        check_backend_7x_snapshot_cris,
        ready=("DSpace", "7.6.8-SNAPSHOT"),
    ),
    Case("backend-8-cris", BACKEND, ("8-cris",), check_backend_8_cris, ready=("DSpace", "cris-2024.02.04")),
    Case("backend-solr-slow", BACKEND, ("dep-solr-slow",), check_backend_solr_slow, shot=True),
    # ---- the frontend page
    Case("frontend-demo-ssr", FRONTEND, (), check_frontend_demo, ready=("Rendering", "Server-side")),
    Case("frontend-shell", FRONTEND, ("shell",), check_shell, ready=("Rendering", "Client-side shell")),
    Case(
        "frontend-shell-host-rule",
        FRONTEND,
        ("shell-host-rule",),
        check_shell_host_rule,
        ready=("Rendering", "Client-side shell"),
    ),
    Case(
        "frontend-shell-public-host",
        FRONTEND,
        ("shell-public-host",),
        check_shell_public_host,
        ready=("Rendering", "Client-side shell"),
        shot=True,
    ),
    Case(
        "frontend-no-markup", FRONTEND, ("no-markup",), check_no_markup, ready=("Rendering", "Not recognised")
    ),
    Case(
        "frontend-homepage-down", FRONTEND, ("home-down",), check_home_down, ready=("Rendering", "Read once")
    ),
    Case(
        "frontend-public-url-down",
        FRONTEND,
        ("public-down",),
        check_public_down,
        ready=("Public URL", "connection refused"),
        shot=True,
    ),
    Case("frontend-public-url-ok", FRONTEND, ("public-ok",), check_public_ok, ready=("Public URL", "871")),
    Case("frontend-public-url-not-set", FRONTEND, (), check_public_not_set, ready=("Public URL", "not set")),
    Case(
        "frontend-public-url-pending",
        FRONTEND,
        ("public-pending",),
        check_public_pending,
        ready=("Public URL", "not checked yet"),
    ),
    Case(
        "frontend-public-url-check-failed",
        FRONTEND,
        ("public-check-failed",),
        check_public_check_failed,
        ready=("Public URL", "not checked yet"),
        shot=True,
    ),
    # ---- the Solr page
    Case("solr-demo", SOLR, (), check_solr_demo, ready=("Cores", "6 cores pinged")),
    Case("solr-core-failed", SOLR, ("solr-failed",), check_solr_failed, ready=("Cores", "failed to load")),
    Case(
        "solr-optional-core-failed",
        SOLR,
        ("solr-failed-optional",),
        check_solr_failed_optional,
        ready=("Cores", "qaevent failed to load"),
    ),
    Case(
        "solr-reload-failed",
        SOLR,
        ("solr-reload",),
        check_solr_reload_failed,
        ready=("Cores", "failed to reload"),
        shot=True,
    ),
    Case(
        "solr-reload-failed-unlisted-core",
        SOLR,
        ("solr-reload-unlisted",),
        check_solr_reload_unlisted,
        ready=("Cores", "other_app failed to reload"),
    ),
    Case(
        "solr-reload-failed-ping-failed",
        SOLR,
        ("solr-reload-ping-failed",),
        check_solr_reload_ping_failed,
        ready=("Cores", "failed to reload"),
        shot=True,
    ),
    # ---- the Ports page
    Case("ports-demo", "#/stack/ports", (), check_ports_demo),
    # ---- a rung's hint
    Case(
        "nginx-no-web-server", "#/stack/nginx", ("nginx-no-web-server",), check_nginx_no_web_server, shot=True
    ),
    Case(
        "ports-every-interface",
        "#/stack/ports",
        ("ports-every-interface",),
        check_ports_every_interface,
        shot=True,
    ),
    Case(
        "logs-permission-denied",
        "#/stack/logs",
        ("logs-permission-denied",),
        check_logs_permission_denied,
        shot=True,
    ),
    # ---- the Security page, its Home tile and its Stack ladder (the demo may not have it yet: ADDED)
    Case(
        "security-healthy",
        SECURITY,
        ("security-healthy",),
        check_security_healthy,
        ready=("Open ports", "none unexpected"),
        shot=True,
    ),
    Case(
        "security-warnings",
        SECURITY,
        ("security-warnings",),
        check_security_warnings,
        ready=("Open ports", "2 unexpected"),
        shot=True,
    ),
    Case(
        "security-not-available",
        SECURITY,
        ("security-not-available",),
        check_security_not_available,
        ready=("Open ports", "socket table"),
        shot=True,
    ),
    Case(
        "security-many-ports",
        SECURITY,
        ("security-many-ports",),
        check_security_many_ports,
        ready=("Open ports", "none unexpected"),
        shot=True,
    ),
    Case(
        "security-tailscale",
        SECURITY,
        ("security-tailscale",),
        check_security_tailscale,
        ready=("Open ports", "through Tailscale only"),
        shot=True,
    ),
    Case("security-home", "#/home", ("security-warnings",), check_security_home),
    Case("security-stack", "#/stack", ("security-warnings",), check_security_stack),
    # ---- key/value lists
    Case(
        "kv-lists-component",
        FRONTEND,
        (),
        check_kv_lists("Rendering", "Connection used"),
        ready=("Rendering", "Server-side"),
        shot=True,
    ),
    Case("kv-list-server", "#/server", (), check_kv_lists("Host"), shot=True),
    Case("kv-list-about", "#/more/about", (), check_kv_lists("DSpace Sentinel"), shot=True),
]


def _run_case(base: str, browser: Any, case: Case) -> list[str]:
    """Open ``case.route`` with its scenarios, run its check; return console and page errors."""
    ctx = browser.new_context(viewport=DESKTOP, color_scheme="light", reduced_motion="reduce", locale="en-US")
    ctx.set_default_timeout(UI_TIMEOUT_MS)
    ctx.add_init_script("try { localStorage.setItem('sentinel.theme', 'light'); } catch (e) {}")
    errors: list[str] = []
    try:
        page = ctx.new_page()
        page.on("pageerror", lambda err: errors.append(f"pageerror: {err}"))
        page.on(
            "console",
            lambda msg: (
                errors.append(f"console.{msg.type}: {msg.text}") if msg.type in ("error", "warning") else None
            ),
        )
        _install_mocks(page, base, list(case.scenarios))
        page.goto(f"{base}/{case.route}", wait_until="domcontentloaded")
        page.wait_for_selector("#views .view:not([hidden])", timeout=UI_TIMEOUT_MS)
        view = page.locator(".view--sub").last if "/stack/" in case.route else page.locator("body")
        if case.ready is not None:
            _wait_for(view, *case.ready)
        case.check(view)
        overflow = page.evaluate(
            "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        assert overflow <= 1, f"the page scrolls sideways by {overflow} px"
        if case.shot:
            DETAIL_SHOT_DIR.mkdir(parents=True, exist_ok=True)
            path = DETAIL_SHOT_DIR / f"detail-{case.name}.png"
            page.wait_for_timeout(300)
            page.screenshot(path=str(path), full_page=True, animations="disabled")
    finally:
        ctx.close()
    return errors


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_detail_state(detail_server: str, chromium: _Chromium, case: Case) -> None:
    errors = chromium.run(_run_case, detail_server, chromium.browser, case)
    assert errors == [], errors


def test_report_facts_list_sits_under_its_title(detail_server: str, chromium: _Chromium) -> None:
    """The "About this report" tile of a report generated for this test (its title is the tile's)."""
    report = _http(f"{detail_server}/api/reports", "POST", {"range": "24h", "fmt": "html"}, timeout=60.0)
    case = Case(
        "kv-list-report",
        f"#/history/reports/{report['id']}",
        (),
        check_kv_lists(report["title"]),
        shot=True,
    )
    errors = chromium.run(_run_case, detail_server, chromium.browser, case)
    assert errors == [], errors
