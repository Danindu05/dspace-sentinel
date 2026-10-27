"""DSpace backend collector: service → TCP → ``/server/api`` → actuator → search → dependencies.

All requests are anonymous GETs against ``dspace.server_url``. The actuator rung is graded
the way DSpace defines its statuses (identical in 7, 8, 9 and 10): anonymously DSpace shows the
aggregate status only (the components need an admin login) and serves ``DOWN`` with HTTP 200.
``UP`` and ``UP_WITH_ISSUES`` (a missing GeoLite database is enough for it) are ok. An
anonymous ``DOWN`` is an *informational* ``unknown`` on the versions whose SEO health indicator
reports sitemap, robots.txt and SSR problems as ``DOWN`` (7.6.4–7.6.x, 8.2–8.4, 9.0–9.3, 10.0),
as long as the REST API answers, and a warning otherwise. A backend that does not expose
``/actuator/health`` (401/403/404) reads ``unknown`` with ``detail.informational`` set, so it
never fails or greys out the component. The actuator is asked at most every
:data:`ACTUATOR_MIN_INTERVAL_S` seconds, because each call runs every DSpace health indicator,
but only a healthy or informational answer is reused, and never across a restart or another
DSpace version (see :meth:`BackendCollector._actuator_probe`).

Every HTTP rung carries ``detail.status_code`` (``doctor`` prints "200 / 91 ms"). Dependency
rungs mirror PostgreSQL and Solr, capped at ``warn``, and are omitted entirely when that module
is switched off (a module that is off leaves no trace).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, ClassVar

from dspace_sentinel import dspace_version
from dspace_sentinel.collectors.base import Collector, CollectorContext, CollectorResult
from dspace_sentinel.collectors.services import (
    Ladder,
    http_message,
    rung,
    slow_message,
    systemd_rung,
    tcp_rung,
    url_parts,
)
from dspace_sentinel.dspace_version import DSpaceVersion
from dspace_sentinel.health import dependency_rung, is_informational, threshold_status
from dspace_sentinel.models import CheckResult, Status
from dspace_sentinel.util.httpc import HttpProbe, probe

log = logging.getLogger(__name__)

DEPENDENCY_RUNGS: tuple[tuple[str, str, str], ...] = (
    ("postgres", "backend.dep.postgres", "Database dependency"),
    ("solr", "backend.dep.solr", "Solr dependency"),
)
"""``(dependency component id, rung id, rung label)``."""

_NOT_EXPOSED = (401, 403, 404)

ACTUATOR_MIN_INTERVAL_S = 300.0
"""How long one actuator answer is reused. Every ``/actuator/health`` call runs all of DSpace's
health indicators, including (7.6.4+, 8.2+, 9.x, 10.x) the SEO check's requests to the UI and a
server-side render of its homepage; the REST and search rungs are still measured every cycle.
Only an answer whose rung reads ok or informational is reused: after a warning (a DOWN, an
error, a time-out) the next cycle asks again, so the alert rules count real answers."""

ADMIN_LOGIN_NOTE = "details need a DSpace admin login"
UP_WITH_ISSUES_TEXT = "up, with non-blocking issues"
SEO_COMPONENT = "seo"
"""The actuator component of DSpace's SEO health indicator (sitemaps, robots.txt, SSR; 7.6.4+, 8.2+)."""


@dataclass(frozen=True)
class ActuatorGrade:
    """How the actuator rung reads for one ``/actuator/health`` answer."""

    status: Status
    message: str
    informational: bool = False
    known_issue: str | None = None
    """``"seo"`` when the answer is the SEO health indicator's known ``DOWN``."""


def grade_health(body: Any, version: DSpaceVersion | None, rest_ok: bool = True) -> ActuatorGrade | None:
    """Grade an actuator answer; ``None`` when ``body`` carries no status at all.

    * ``UP`` → ok; ``UP_WITH_ISSUES`` → ok "up, with non-blocking issues" (naming the components
      when they are visible);
    * visible components (an admin view, or ``show-details=always``) → warn naming what is down,
      except a ``DOWN`` whose only down component is ``seo`` on a version inside the SEO window:
      that is the known issue below, with the SEO checks it flags ("…: the SEO check flags
      sitemap, ssr"), as 8.5 / 9.4 / 10.1 report the same condition as ``UP_WITH_ISSUES``;
    * anonymous ``DOWN`` on a version inside the SEO window whose REST API answers (``rest_ok``)
      → informational unknown "DSpace 9.2 reports DOWN while healthy (a known SEO health check
      issue fixed in 8.5 / 9.4 / 10.1)";
    * any other status → warn "status DOWN (details need a DSpace admin login)".
    """
    if not isinstance(body, dict):
        return None
    raw = body.get("status")
    if not isinstance(raw, str) or not raw.strip():
        return None
    status = raw.strip().upper()
    components = {name: str(st).upper() for name, st in actuator_components(body).items()}
    not_up = [name for name, st in components.items() if st not in ("UP", "UNKNOWN")]
    if status == "UP":
        return ActuatorGrade("ok", "UP")
    if status == "UP_WITH_ISSUES":
        return ActuatorGrade("ok", UP_WITH_ISSUES_TEXT + (f" ({', '.join(not_up)})" if not_up else ""))
    in_seo_window = version is not None and dspace_version.seo_reports_down(version)
    if components:
        down = [name for name, st in components.items() if st in ("DOWN", "OUT_OF_SERVICE")]
        if status == "DOWN" and down == [SEO_COMPONENT] and version is not None and in_seo_window:
            return _seo_known_issue(version, seo_flags(body))
        failing = down or not_up
        return ActuatorGrade("warn", f"status {status}" + (f" ({', '.join(failing)})" if failing else ""))
    if status == "DOWN" and rest_ok and version is not None and in_seo_window:
        return _seo_known_issue(version, [])
    return ActuatorGrade("warn", f"status {status} ({ADMIN_LOGIN_NOTE})")


def _seo_known_issue(version: DSpaceVersion, flagged: list[str]) -> ActuatorGrade:
    """The informational rung for the SEO health indicator's known ``DOWN`` (naming what it flags)."""
    message = (
        f"DSpace {version.text} reports DOWN while healthy "
        f"(a known SEO health check issue fixed in {dspace_version.SEO_FIXED_IN})"
    )
    if flagged:
        message += f": the SEO check flags {', '.join(flagged)}"
    return ActuatorGrade("unknown", message, informational=True, known_issue=SEO_COMPONENT)


def seo_flags(body: Any) -> list[str]:
    """The checks a visible ``seo`` component flags: its detail keys whose value is not ``"OK"``.

    DSpace's SEO indicator reports ``sitemap``, ``robots.txt`` and ``ssr``, each ``"OK"`` or a
    sentence saying what is wrong; ``[]`` when the component or its details are not shown.
    """
    comps = body.get("components") if isinstance(body, dict) else None
    seo = comps.get(SEO_COMPONENT) if isinstance(comps, dict) else None
    details = seo.get("details") if isinstance(seo, dict) else None
    if not isinstance(details, dict):
        return []
    return [str(key) for key, value in details.items() if str(value).strip().upper() != "OK"]


@dataclass(frozen=True)
class _ActuatorAnswer:
    url: str
    version: str
    """The REST root's raw ``dspaceVersion`` when the answer came: another one means an upgrade."""
    ts: float
    probe: HttpProbe


def version_number(raw: Any) -> str | None:
    """``"DSpace 8.1"`` → ``"8.1"`` (the REST root reports ``dspaceVersion`` with the product name)."""
    if raw is None:
        return None
    text = str(raw).strip()
    if text.lower().startswith("dspace"):
        text = text[6:].strip()
    return text or None


def actuator_components(body: Any) -> dict[str, Any]:
    """``{"components": {"db": {"status": "UP"}, ...}}`` → ``{"db": "UP", ...}`` (nested details dropped)."""
    if not isinstance(body, dict):
        return {}
    comps = body.get("components")
    if not isinstance(comps, dict):
        return {}
    out: dict[str, Any] = {}
    for name, comp in comps.items():
        out[str(name)] = comp.get("status") if isinstance(comp, dict) else comp
    return out


def search_total(body: Any) -> int | None:
    """``_embedded.searchResult.page.totalElements`` of a discover response, when present."""
    try:
        total = body["_embedded"]["searchResult"]["page"]["totalElements"]
    except (KeyError, TypeError):
        return None
    return int(total) if isinstance(total, int | float) else None


def _answered(rest_rung: CheckResult | None) -> bool:
    """True when the REST rung got the DSpace root back: ok, or correct but slow (``detail.slow``)."""
    if rest_rung is None or rest_rung.id != "backend.rest":
        return False
    return rest_rung.status == "ok" or bool(rest_rung.detail.get("slow"))


class BackendCollector(Collector):
    """``backend`` ☕: service → TCP → GET /server/api → actuator → search API → dependency rungs."""

    id: ClassVar[str] = "backend"
    interval_key: ClassVar[str] = "http_interval"
    label: ClassVar[str] = "DSpace Backend"
    emoji: ClassVar[str] = "☕"
    depends_on: ClassVar[list[str]] = ["postgres", "solr"]
    budget_s: ClassVar[float] = 25.0  # systemd 3 + TCP 2 + REST 5 + actuator 5 + search 5 + slack

    def __init__(self) -> None:
        self._actuator: _ActuatorAnswer | None = None

    async def _actuator_probe(
        self, ctx: CollectorContext, url: str, raw_version: str
    ) -> tuple[HttpProbe, float | None]:
        """``(answer, age in s)``: the last answer while it still applies, else a new one.

        The last answer applies while it is younger than :data:`ACTUATOR_MIN_INTERVAL_S` and came
        from the same URL and the same DSpace version (``raw_version``, the REST root's
        ``dspaceVersion``). :meth:`_actuator_rung` forgets an answer that read as a warning, and
        :meth:`collect` forgets it whenever the ladder stops before the actuator (a stopped unit, a
        closed port, a REST API that fails: the backend restarts), so neither a failure nor the
        process before a restart is ever replayed.
        """
        last = self._actuator
        if (
            last is not None
            and last.url == url
            and last.version == raw_version
            and 0.0 <= ctx.now - last.ts < ACTUATOR_MIN_INTERVAL_S
        ):
            return last.probe, ctx.now - last.ts
        answer = await probe(ctx.http, url, expect_json=True)
        self._actuator = _ActuatorAnswer(url, raw_version, ctx.now, answer)
        return answer, None

    async def _actuator_rung(
        self,
        ctx: CollectorContext,
        base: str,
        version: DSpaceVersion | None,
        raw_version: str,
        rest_rung: CheckResult,
        metrics: dict[str, Any],
    ) -> CheckResult:
        """The ``backend.actuator`` rung (sets ``actuator_status`` / ``actuator_components``).

        Only an answer whose rung reads ok or informational is kept for the next cycles.
        """
        check = await self._grade_actuator(ctx, base, version, raw_version, rest_rung, metrics)
        if check.status != "ok" and not is_informational(check):
            self._actuator = None
        return check

    async def _grade_actuator(
        self,
        ctx: CollectorContext,
        base: str,
        version: DSpaceVersion | None,
        raw_version: str,
        rest_rung: CheckResult,
        metrics: dict[str, Any],
    ) -> CheckResult:
        rung_id, label = "backend.actuator", "Actuator health"
        a, age = await self._actuator_probe(ctx, f"{base}/actuator/health", raw_version)
        body = a.json if isinstance(a.json, dict) else {}
        components = actuator_components(body)
        metrics["actuator_components"] = components
        common: dict[str, Any] = {
            "latency_ms": a.latency_ms,
            "status_code": a.status_code,
            "cached_age_s": round(age) if age is not None else None,
        }
        grade = grade_health(body, version, rest_ok=_answered(rest_rung))
        if grade is not None:
            health_status = str(body["status"]).strip().upper()
            metrics["actuator_status"] = health_status
            return rung(
                rung_id,
                label,
                grade.status,
                ctx.now,
                message=grade.message,
                components=components,
                health_status=health_status,
                informational=True if grade.informational else None,
                known_issue=grade.known_issue,
                **common,
            )
        if a.status_code in _NOT_EXPOSED:
            return rung(
                rung_id,
                label,
                "unknown",
                ctx.now,
                message=f"not exposed (HTTP {a.status_code})",
                informational=True,
                **common,
            )
        return rung(rung_id, label, "warn", ctx.now, message=http_message(a), **common)

    async def collect(self, ctx: CollectorContext) -> CollectorResult:
        """Climb the ladder against ``dspace.server_url``; dependency rungs are always appended."""
        d = ctx.cfg.dspace
        ts = ctx.now
        base = d.server_url.rstrip("/")
        parts = url_parts(base)
        tcp = ("backend.tcp", f"TCP :{parts.port}")
        rest = ("backend.rest", f"GET {parts.path}/api")
        actuator = ("backend.actuator", "Actuator health")
        search = ("backend.search", "Search API")
        ladder = Ladder(ts)
        metrics: dict[str, Any] = {
            "dspace_version": None,
            "dspace_name": None,
            **dspace_version.version_metrics(None),
            "rest_latency_ms": None,
            "search_latency_ms": None,
            "actuator_status": None,
            "actuator_components": {},
            "server_url": base,
        }
        version: DSpaceVersion | None = None

        ladder.add(await systemd_rung("backend.service", d.backend_service, ts))
        if ladder.blocked:
            return self._stopped(ctx, ladder, metrics, [tcp, rest, actuator, search])

        ladder.add(await tcp_rung(tcp[0], parts.host, parts.port, ts, label=tcp[1]))
        if ladder.blocked:
            return self._stopped(ctx, ladder, metrics, [rest, actuator, search])

        # --- GET /server/api: 200 + JSON with dspaceVersion --------------------------------
        p = await probe(ctx.http, f"{base}/api", expect_json=True)
        info = p.json if isinstance(p.json, dict) else {}
        raw_version = ""
        if p.ok and "dspaceVersion" in info:
            latency = float(p.latency_ms or 0.0)
            th = ctx.cfg.alerts.rest_latency_ms
            status = threshold_status(latency, th.warning, th.critical)
            raw_version = str(info.get("dspaceVersion"))
            ladder.add(
                rung(
                    rest[0],
                    rest[1],
                    status,
                    ts,
                    latency_ms=p.latency_ms,
                    message=raw_version if status == "ok" else slow_message("REST API", latency),
                    slow=True if status != "ok" else None,
                    status_code=p.status_code,
                    dspaceVersion=raw_version,
                    dspaceName=info.get("dspaceName"),
                    dspaceUI=info.get("dspaceUI"),
                    dspaceServer=info.get("dspaceServer"),
                )
            )
            metrics["dspace_version"] = version_number(raw_version)
            metrics["dspace_name"] = info.get("dspaceName")
            metrics.update(dspace_version.version_metrics(info))
            metrics["rest_latency_ms"] = p.latency_ms
            version = dspace_version.parse(raw_version)
        elif p.ok:
            ladder.add(
                rung(
                    rest[0],
                    rest[1],
                    "crit",
                    ts,
                    latency_ms=p.latency_ms,
                    message="unexpected response (not the DSpace REST API)",
                    url=f"{base}/api",
                    status_code=p.status_code,
                )
            )
        else:
            ladder.add(
                rung(
                    rest[0],
                    rest[1],
                    "crit",
                    ts,
                    latency_ms=p.latency_ms,
                    message=http_message(p),
                    status_code=p.status_code,
                )
            )
        if ladder.blocked:
            return self._stopped(ctx, ladder, metrics, [actuator, search])

        # --- actuator health: graded the DSpace way, asked at most every 5 minutes --------------
        ladder.add(await self._actuator_rung(ctx, base, version, raw_version, ladder.checks[-1], metrics))

        # --- search API ---------------------------------------------------------------------
        s = await probe(
            ctx.http, f"{base}/api/discover/search/objects", params={"size": "1"}, expect_json=True
        )
        if s.ok and isinstance(s.json, dict):
            latency = float(s.latency_ms or 0.0)
            th = ctx.cfg.alerts.search_latency_ms
            status = threshold_status(latency, th.warning, th.critical)
            total = search_total(s.json)
            ladder.add(
                rung(
                    search[0],
                    search[1],
                    status,
                    ts,
                    latency_ms=s.latency_ms,
                    value=float(total) if total is not None else None,
                    message="" if status == "ok" else slow_message("search", latency),
                    slow=True if status != "ok" else None,
                    size=1,
                    total=total,
                    status_code=s.status_code,
                )
            )
            metrics["search_latency_ms"] = s.latency_ms
        else:
            message = http_message(s) if not s.ok else "unexpected response (not JSON)"
            if s.status_code is not None:
                message += " (discover/search/objects)"
            ladder.add(
                rung(
                    search[0],
                    search[1],
                    "crit",
                    ts,
                    latency_ms=s.latency_ms,
                    message=message,
                    status_code=s.status_code,
                )
            )
        return self._finish(ctx, ladder, metrics)

    def _stopped(
        self,
        ctx: CollectorContext,
        ladder: Ladder,
        metrics: dict[str, Any],
        not_reached: list[tuple[str, str]],
    ) -> CollectorResult:
        """The ladder stopped before the actuator: the backend is down or restarting, so its last
        actuator answer (from the process before the restart) is forgotten."""
        self._actuator = None
        ladder.skip_all(not_reached)
        return self._finish(ctx, ladder, metrics)

    @staticmethod
    def _finish(ctx: CollectorContext, ladder: Ladder, metrics: dict[str, Any]) -> CollectorResult:
        """Append the dependency rungs (only for modules that are on) and wrap up."""
        for dep_id, rung_id, label in DEPENDENCY_RUNGS:
            if ctx.cfg.module_enabled(dep_id):
                ladder.add(dependency_rung(rung_id, label, ctx.dependency(dep_id), ladder.ts))
        return CollectorResult(checks=ladder.checks, metrics=metrics)


__all__ = [
    "ACTUATOR_MIN_INTERVAL_S",
    "ADMIN_LOGIN_NOTE",
    "DEPENDENCY_RUNGS",
    "SEO_COMPONENT",
    "UP_WITH_ISSUES_TEXT",
    "ActuatorGrade",
    "BackendCollector",
    "actuator_components",
    "grade_health",
    "search_total",
    "seo_flags",
    "version_number",
]
