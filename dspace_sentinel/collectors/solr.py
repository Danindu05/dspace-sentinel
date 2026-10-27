"""Solr collector: systemd → TCP → ``admin/cores`` → one ping per core → ``rows=0`` search (SPEC section 6).

Every request is a read: core STATUS, per-core ``admin/ping`` and ``select?q=*:*&rows=0`` on the
``search`` core. The per-core rungs are siblings — a missing ``authority`` core does not hide the
search latency, which is the component's headline number.

The configured cores (``solr.cores``, the four every DSpace needs) always get a rung. The
optional cores DSpace 8 added (``qaevent``, ``suggestion``) and DSpace 10 added (``audit``) get
one too when STATUS lists them, behind the same ``solr.multicorePrefix`` as the configured cores
(a problem with one of them is a warning, never critical). A core that failed to load appears in
STATUS only under ``initFailures``: its rung says why ("failed to load: …"), and it is neither
pinged nor searched, which would only add errors to Solr's log. A core whose RELOAD failed (a
bad schema or solrconfig after an upgrade, say) is listed under ``initFailures`` *and* ``status``:
Solr keeps the old core serving, so it is pinged and searched as usual and its rung warns
"reload failed: … (still serving; a restart would not load it)".
"""

from __future__ import annotations

import asyncio
import logging
import re
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
from dspace_sentinel.health import threshold_status
from dspace_sentinel.models import CheckResult, Status
from dspace_sentinel.util.httpc import HttpProbe, probe

log = logging.getLogger(__name__)

SEARCH_CORE = "search"
SEARCH_PARAMS: dict[str, str] = {"q": "*:*", "rows": "0", "wt": "json"}
"""The only query Sentinel ever sends to a core: match all, return nothing."""

REASON_MAX = 200
_EXCEPTION_PREFIX = re.compile(r"^(?:[A-Za-z_$][\w$]*\.)+[\w$]*(?:Exception|Error)\s*:\s*")
LIB_HINT = 'start Solr with SOLR_OPTS="-Dsolr.config.lib.enabled=true" (Solr 9.8+)'
RELOAD_NOTE = "still serving; a restart would not load it"
"""Why a failed RELOAD is a warning: Solr keeps the old core, but its next start loads the new
configuration, which fails."""


def core_prefix(cores: list[str]) -> str:
    """The ``solr.multicorePrefix`` the configured cores share: ``"dspace_"`` for ``dspace_search`` …"""
    names = [c for c in cores if c]
    for required in (SEARCH_CORE, *dspace_version.REQUIRED_CORES):
        for name in names:
            if name.endswith(required):
                return name[: -len(required)]
    return ""


def search_core_for(cores: list[str]) -> str:
    """The core the search rung queries: ``search`` (or ``<prefix>search``) if configured, else the first."""
    if not cores or SEARCH_CORE in cores:
        return SEARCH_CORE
    prefixed = [c for c in cores if c.endswith(SEARCH_CORE)]
    return prefixed[0] if prefixed else cores[0]


def parse_core_status(payload: Any) -> dict[str, dict[str, Any]]:
    """``admin/cores?action=STATUS`` JSON → ``{core: {docs, size_bytes, uptime_s}}``."""
    out: dict[str, dict[str, Any]] = {}
    status = payload.get("status") if isinstance(payload, dict) else None
    if not isinstance(status, dict):
        return out
    for name, info in status.items():
        if not isinstance(info, dict):
            continue
        index = info.get("index") if isinstance(info.get("index"), dict) else {}
        docs = index.get("numDocs")
        size = index.get("sizeInBytes")
        uptime = info.get("uptime")
        out[str(name)] = {
            "docs": int(docs) if isinstance(docs, int | float) else None,
            "size_bytes": int(size) if isinstance(size, int | float) else None,
            "uptime_s": round(float(uptime) / 1000.0) if isinstance(uptime, int | float) else None,
        }
    return out


def failure_reason(message: Any) -> str:
    """The readable part of an ``initFailures`` message: its first line without exception class names."""
    lines = str(message).strip().splitlines() if message is not None else []
    text = lines[0].strip() if lines else ""
    while True:
        shorter = _EXCEPTION_PREFIX.sub("", text, count=1).strip()
        if shorter == text:
            break
        text = shorter
    text = text or "no reason given"
    return text if len(text) <= REASON_MAX else text[: REASON_MAX - 1].rstrip() + "…"


def failure_hint(message: Any) -> str | None:
    """What fixes a known load failure: Solr 9.8+ ignores ``<lib/>`` unless it is enabled."""
    text = str(message or "")
    return LIB_HINT if "<lib/>" in text and "disabled" in text else None


def parse_init_failures(payload: Any) -> dict[str, str]:
    """STATUS's ``initFailures`` → ``{core: reason}`` (:func:`failure_reason`)."""
    failures = payload.get("initFailures") if isinstance(payload, dict) else None
    if not isinstance(failures, dict):
        return {}
    return {str(name): failure_reason(message) for name, message in failures.items()}


def split_failures(
    failures: dict[str, str], loaded: dict[str, dict[str, Any]]
) -> tuple[dict[str, str], dict[str, str]]:
    """``initFailures`` → ``(not loaded, reload failed)``.

    Solr records a core that failed to start *and* a core whose RELOAD failed in ``initFailures``;
    only the second is still in STATUS's ``status`` map (``loaded``), serving with its previous
    configuration until Solr restarts (CoreContainer.reload keeps the old core registered).
    """
    not_loaded = {core: reason for core, reason in failures.items() if core not in loaded}
    reloads = {core: reason for core, reason in failures.items() if core in loaded}
    return not_loaded, reloads


def optional_cores_in(
    cores: list[str], present: set[str], version: dspace_version.DSpaceVersion | None
) -> list[str]:
    """The optional DSpace cores (behind the configured cores' prefix) that ``present`` lists."""
    prefix = core_prefix(cores)
    names = (prefix + core for core in dspace_version.optional_cores(version))
    return [name for name in names if name in present and name not in cores]


def _empty_cores(cores: list[str]) -> list[dict[str, Any]]:
    return [{"name": c, "docs": None, "size_bytes": None, "status": "unknown"} for c in cores]


def _core_rung(
    core: str,
    ts: float,
    info: dict[str, Any],
    ping: HttpProbe | None,
    failure: str | None,
    hint: str | None,
    optional: bool,
    reload_failure: str | None = None,
) -> CheckResult:
    """One ``solr.core.<name>`` rung: failed to load, or its ping (optional cores cap at warn).

    A core whose reload failed (``reload_failure``) still serves: a good ping reads as a warning
    "reload failed: <reason> (still serving; a restart would not load it)", a bad one as the ping's
    own problem; either way ``detail.reload_failed`` holds the reason.
    """
    docs = info.get("docs")
    status: Status
    if failure is not None:
        status, message = "crit", f"failed to load: {failure}"
    elif ping is None:  # pragma: no cover - every loaded core is pinged
        status, message = "unknown", "not pinged"
    else:
        ping_status = ping.json.get("status") if isinstance(ping.json, dict) else None
        if ping.ok and (ping_status is None or str(ping_status).upper() == "OK"):
            status, message = "ok", ""
        elif ping.status_code == 404:
            status, message = "crit", "core not found (HTTP 404)"
        elif ping.ok:
            status, message = "crit", f"ping status {ping_status}"
        else:
            status, message = "crit", http_message(ping)
    if reload_failure is not None and status == "ok":
        status, message = "warn", f"reload failed: {reload_failure} ({RELOAD_NOTE})"
    if optional and status == "crit":
        status = "warn"
    return rung(
        f"solr.core.{core}",
        f"Core {core}",
        status,
        ts,
        latency_ms=ping.latency_ms if ping is not None else None,
        value=float(docs) if docs is not None else None,
        message=message,
        docs=docs,
        size_bytes=info.get("size_bytes"),
        optional=True if optional else None,
        hint=hint,
        reload_failed=reload_failure,
    )


def _admin_rung(
    p: HttpProbe,
    ts: float,
    cores: list[str],
    server_cores: dict[str, dict[str, Any]],
    failures: dict[str, str],
    reloads: dict[str, str] | None = None,
) -> CheckResult:
    """The ``solr.admin`` rung for the CoreAdmin STATUS answer.

    ``failures`` are the cores that did not load, ``reloads`` the loaded ones whose reload failed
    (:func:`split_failures`): "6 cores, 1 failed to reload" counts every loaded core once.
    """
    rung_id, label = "solr.admin", "Admin cores"
    reloads = reloads or {}
    if p.ok and isinstance(p.json, dict):
        missing = [c for c in cores if c not in server_cores and c not in failures]
        message = f"{len(server_cores)} cores"
        if failures:
            message += f", {len(failures)} failed to load"
        if reloads:
            message += f", {len(reloads)} failed to reload"
        return rung(
            rung_id,
            label,
            "ok",
            ts,
            latency_ms=p.latency_ms,
            message=message,
            cores=sorted(server_cores),
            missing=missing or None,
            failed=sorted(failures) or None,
            reload_failed=sorted(reloads) or None,
            status_code=p.status_code,
        )
    status: Status = "crit"
    if p.status_code in (401, 403):
        status, message = "warn", f"authentication required (HTTP {p.status_code})"
    elif p.ok:
        message = "unexpected response (not Solr)"
    else:
        message = http_message(p)
    return rung(
        rung_id, label, status, ts, latency_ms=p.latency_ms, message=message, status_code=p.status_code
    )


def _search_rung(
    sp: HttpProbe, ts: float, search_core: str, ctx: CollectorContext, metrics: dict[str, Any]
) -> CheckResult:
    """The ``solr.search`` rung (``q=*:*&rows=0``); sets ``search_docs`` / ``search_latency_ms``."""
    rung_id, label = "solr.search", "Search query"
    response = sp.json.get("response") if isinstance(sp.json, dict) else None
    if sp.ok and isinstance(response, dict):
        num = response.get("numFound")
        latency = float(sp.latency_ms or 0.0)
        th = ctx.cfg.alerts.search_latency_ms
        status = threshold_status(latency, th.warning, th.critical)
        metrics["search_docs"] = int(num) if isinstance(num, int | float) else None
        metrics["search_latency_ms"] = sp.latency_ms
        return rung(
            rung_id,
            label,
            status,
            ts,
            latency_ms=sp.latency_ms,
            value=float(num) if isinstance(num, int | float) else None,
            message="" if status == "ok" else slow_message("search", latency),
            slow=True if status != "ok" else None,
            query="q=*:*&rows=0",
            core=search_core,
            status_code=sp.status_code,
        )
    if sp.ok:
        message = f"unexpected response ({search_core}/select)"
    else:
        message = http_message(sp)
        if sp.status_code is not None:
            message += f" ({search_core}/select)"
    return rung(
        rung_id,
        label,
        "crit",
        ts,
        latency_ms=sp.latency_ms,
        message=message,
        core=search_core,
        status_code=sp.status_code,
    )


class SolrCollector(Collector):
    """``solr`` ☀️: service → TCP → admin cores → core pings → search query."""

    id: ClassVar[str] = "solr"
    interval_key: ClassVar[str] = "http_interval"
    label: ClassVar[str] = "Solr"
    emoji: ClassVar[str] = "☀️"
    budget_s: ClassVar[float] = 25.0  # systemd 3 + TCP 2 + admin 5 + (version ‖ pings) 5 + search 5 + slack

    async def collect(self, ctx: CollectorContext) -> CollectorResult:
        """Climb the ladder against ``solr.url``; never raises for an unreachable Solr."""
        cfg = ctx.cfg.solr
        ts = ctx.now
        base = cfg.url.rstrip("/")
        parts = url_parts(base)
        cores = [c for c in cfg.cores if c]
        search_core = search_core_for(cores)
        tcp = ("solr.tcp", f"TCP :{parts.port}")
        admin = ("solr.admin", "Admin cores")
        core_rungs = [(f"solr.core.{c}", f"Core {c}") for c in cores]
        search = ("solr.search", "Search query")
        ladder = Ladder(ts)
        metrics: dict[str, Any] = {
            "cores": _empty_cores(cores),
            "version": None,
            "search_docs": None,
            "search_latency_ms": None,
            "search_core": search_core,
            "init_failures": {},
            "reload_failures": {},
        }

        ladder.add(await systemd_rung("solr.service", cfg.service, ts))
        if ladder.blocked:
            ladder.skip_all([tcp, admin, *core_rungs, search])
            return CollectorResult(checks=ladder.checks, metrics=metrics)

        ladder.add(await tcp_rung(tcp[0], parts.host, parts.port, ts, label=tcp[1]))
        if ladder.blocked:
            ladder.skip_all([admin, *core_rungs, search])
            return CollectorResult(checks=ladder.checks, metrics=metrics)

        # --- admin/cores ---------------------------------------------------------------
        p = await probe(
            ctx.http, f"{base}/admin/cores", params={"action": "STATUS", "wt": "json"}, expect_json=True
        )
        server_cores = parse_core_status(p.json) if p.ok else {}
        failures, reloads = split_failures(parse_init_failures(p.json) if p.ok else {}, server_cores)
        raw_failures = p.json.get("initFailures") if p.ok and isinstance(p.json, dict) else None
        ladder.add(_admin_rung(p, ts, cores, server_cores, failures, reloads))
        if ladder.blocked:
            ladder.skip_all([*core_rungs, search])
            return CollectorResult(checks=ladder.checks, metrics=metrics)
        metrics["init_failures"] = failures
        metrics["reload_failures"] = reloads

        # --- version (best effort, never a rung) and one ping per loaded core, all at once ---
        # Sequentially a slow Solr (4-5 s per request) would outlast the collector's timeout.
        optional = optional_cores_in(cores, set(server_cores) | set(failures), self._dspace_version(ctx))
        rung_cores = cores + optional
        pinged = [core for core in rung_cores if core not in failures]
        v, *pings = await asyncio.gather(
            probe(ctx.http, f"{base}/admin/info/system", params={"wt": "json"}, expect_json=True),
            *(
                probe(ctx.http, f"{base}/{core}/admin/ping", params={"wt": "json"}, expect_json=True)
                for core in pinged
            ),
        )
        if v.ok and isinstance(v.json, dict):
            lucene = v.json.get("lucene")
            if isinstance(lucene, dict) and lucene.get("solr-spec-version"):
                metrics["version"] = str(lucene["solr-spec-version"])
        ping_of = dict(zip(pinged, pings, strict=True))

        # --- the core rungs (siblings): configured first, then the optional ones -------------
        cores_metric: list[dict[str, Any]] = []
        for core in rung_cores:
            info = server_cores.get(core, {})
            is_optional = core in optional
            raw = raw_failures.get(core) if isinstance(raw_failures, dict) else None
            check = ladder.add(
                _core_rung(
                    core,
                    ts,
                    info,
                    ping_of.get(core),
                    failures.get(core),
                    failure_hint(raw),
                    is_optional,
                    reloads.get(core),
                )
            )
            entry: dict[str, Any] = {
                "name": core,
                "docs": info.get("docs"),
                "size_bytes": info.get("size_bytes"),
                "status": check.status if check is not None else "unknown",
            }
            if is_optional:
                entry["optional"] = True
            cores_metric.append(entry)
        metrics["cores"] = cores_metric

        # --- search: q=*:* rows=0 on the search core ---------------------------------------
        if search_core in failures:
            ladder.add(
                rung(
                    search[0],
                    search[1],
                    "crit",
                    ts,
                    message=f"core {search_core} failed to load",
                    core=search_core,
                )
            )
            return CollectorResult(checks=ladder.checks, metrics=metrics)
        sp = await probe(ctx.http, f"{base}/{search_core}/select", params=SEARCH_PARAMS, expect_json=True)
        ladder.add(_search_rung(sp, ts, search_core, ctx, metrics))
        return CollectorResult(checks=ladder.checks, metrics=metrics)

    @staticmethod
    def _dspace_version(ctx: CollectorContext) -> dspace_version.DSpaceVersion | None:
        """The DSpace version the backend collector last read (``None`` before it or when it is off)."""
        backend = ctx.dependency("backend")
        metrics = getattr(backend, "metrics", None)
        return dspace_version.parse(metrics.get("dspace_version")) if isinstance(metrics, dict) else None


__all__ = [
    "LIB_HINT",
    "RELOAD_NOTE",
    "SEARCH_CORE",
    "SEARCH_PARAMS",
    "SolrCollector",
    "core_prefix",
    "failure_hint",
    "failure_reason",
    "optional_cores_in",
    "parse_core_status",
    "parse_init_failures",
    "search_core_for",
    "split_failures",
]
