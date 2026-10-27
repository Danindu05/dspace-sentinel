"""Demo mode (SPEC section 13): a synthetic, realistic DSpace stack with a scripted incident.

``DemoSuite`` replaces every real collector when ``SENTINEL_DEMO=1``. All numbers come
from ``DemoWorld``, a deterministic function of time, so the live view, the seven-day
backfill and the alert engine all see one consistent story:

* calm operation with gentle noise and a diurnal curve,
* every ~20 minutes: memory climbs → Solr gets slow → Solr goes down and the backend
  search API fails → recovery.

The stack is a DSpace 9.4 as an anonymous client sees it: the actuator answers
``UP_WITH_ISSUES`` (no GeoLite database) without components, and ``DOWN`` with HTTP 200 while
Solr is down; Solr lists the six DSpace 9 cores; the UI renders on the server.

``SENTINEL_DEMO_FAST=1`` shortens the *live* cycle to three minutes (the same script,
compressed: calm 30 s → two-minute incident → calm) and makes the scheduler run every
collector every :data:`FAST_INTERVAL_S` seconds, so every alert type, the incident and the
recovery reliably appear within a few minutes; the back-filled history always uses the real
20-minute timings.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import logging
import math
import os
import time
from typing import TYPE_CHECKING, Any, ClassVar

from dspace_sentinel import dspace_version
from dspace_sentinel.alerts.incidents import sequence_entry
from dspace_sentinel.collectors.backend import grade_health
from dspace_sentinel.collectors.base import Collector, CollectorContext, CollectorResult
from dspace_sentinel.collectors.frontend import public_host_headers
from dspace_sentinel.collectors.postgres import CONNECTIONS_CHECK_ID, connections_row
from dspace_sentinel.collectors.security import (
    UPDATES_AVAILABLE,
    FileFacts,
    FirewallState,
    HostFacts,
    SshFacts,
    UpdatesFacts,
    WebAnswer,
    build_ladder,
)
from dspace_sentinel.collectors.solr import core_prefix
from dspace_sentinel.config import Config
from dspace_sentinel.health import COMPONENTS, dependency_rung, rollup, threshold_status
from dspace_sentinel.models import Alert, CheckResult, Incident, SystemSample
from dspace_sentinel.util.fmt import duration
from dspace_sentinel.util.net import ListeningSocket

if TYPE_CHECKING:  # pragma: no cover
    from dspace_sentinel.store import Store

log = logging.getLogger(__name__)

GIB = 1024**3
MIB = 1024**2

DEMO_HOSTNAME = "demo-dspace"
DEMO_DSPACE_VERSION = "9.4"
DEMO_SOLR_VERSION = "9.6.1"
DEMO_ANGULAR_VERSION = "20.3.30"
"""The Angular release the dspace-angular 9.4 lock file pins (``ng-version`` of a server render)."""
DEMO_OPTIONAL_CORES: tuple[str, ...] = ("qaevent", "suggestion")
"""The optional Solr cores DSpace 9 installs (their features are off: the cores stay empty)."""
DEMO_BACKEND_UNIT = "tomcat10"
"""The demo stack's backend unit: DSpace 9.4 (Jakarta, Tomcat 10.1) on Ubuntu 24.04, whose package is
tomcat10; there is no tomcat9 package there (the unit discovery's ``DEMO_UNITS`` lists)."""
DEMO_TOMCAT_LOG = "/var/log/tomcat10/catalina.out"
"""The catalina.out that packaged tomcat10 writes (through rsyslog)."""
_DEFAULTS = Config()
DEFAULT_BACKEND_UNIT = _DEFAULTS.dspace.backend_service
DEFAULT_TOMCAT_LOG = next((f.path for f in _DEFAULTS.logs.files if f.id == "tomcat"), "")
DEMO_DSPACE_NAME = "Demo Institutional Repository"
DEMO_ITEMS = 27_491
DEMO_BITSTREAMS = 81_337
DEMO_COMMUNITIES = 14
DEMO_COLLECTIONS = 96
DEMO_EPERSONS = 1_842
DEMO_WORKFLOW_ITEMS = 7
DEMO_CORES = 4
DEMO_MEM_TOTAL = 16 * GIB
DEMO_SWAP_TOTAL = 4 * GIB
DEMO_DISK_TOTAL = 200 * GIB
DEMO_ASSETSTORE_TOTAL = 1024 * GIB
DEMO_ASSETSTORE_USED = 412 * GIB
DEMO_ASSETSTORE_GROWTH_PER_DAY = 1.2 * GIB
DEMO_DB_SIZE = int(3.1 * GIB)
DEMO_SSL_DAYS_LEFT = 61
DEMO_POSTGRES_VERSION = "PostgreSQL 15.8 (Ubuntu 15.8-1.pgdg24.04+1)"
DEMO_PUBLIC_URL = "https://repo.demo-dspace.org"
"""The demo server's public address while ``dspace.public_url`` is not set."""
DEMO_OS_RELEASE: dict[str, str] = {
    "NAME": "Ubuntu",
    "ID": "ubuntu",
    "VERSION_ID": "24.04",
    "PRETTY_NAME": "Ubuntu 24.04.3 LTS",
}
DEMO_UPDATES: tuple[int, int] = (3, 0)
"""Pending updates on the demo server, of which security updates: none, so the demo starts calm."""
DEMO_SENTINEL_CONFIG = "/opt/dspace-sentinel/config/sentinel.yaml"

CYCLE_S = 1200
"""Length of one live scenario cycle (20 minutes)."""

INCIDENT_OFFSET_S = 600
"""Seconds into each live cycle at which the incident starts (first 10 minutes are calm)."""

INCIDENT_LENGTH_S = 600
"""Length of the scripted incident; :data:`PHASES` are expressed on this time base."""

FAST_CYCLE_S = 180
"""Live cycle length with ``SENTINEL_DEMO_FAST=1`` (three minutes)."""

FAST_INCIDENT_OFFSET_S = 30
FAST_INCIDENT_LENGTH_S = 120

FAST_INTERVAL_S = 5
"""Collector interval the scheduler uses for every group in fast mode (repository stats excepted).

With ``confirm_samples = 2`` / ``clear_samples = 3`` every phase of the compressed incident is
sampled often enough for its alert to open and resolve.
"""

# Phase boundaries (seconds since incident start, on the INCIDENT_LENGTH_S time base) → (name, start, end)
PHASES: tuple[tuple[str, int, int], ...] = (
    ("memory", 0, 180),
    ("solr_slow", 180, 300),
    ("solr_down", 300, 420),
    ("recovery", 420, 600),
)


def demo_fast_enabled(env: dict[str, str] | None = None) -> bool:
    """True when ``SENTINEL_DEMO_FAST`` is set to 1/true/yes/on."""
    source = os.environ if env is None else env
    return source.get("SENTINEL_DEMO_FAST", "").strip().lower() in {"1", "true", "yes", "on"}


BACKFILL_INCIDENTS_DAYS_AGO: tuple[float, ...] = (6.1, 4.55, 2.3, 0.9)
"""Where the backfill places incidents (days before start). Each runs the same 10-minute script."""

BACKFILL_NGINX_BLIP_DAYS_AGO = 1.4
BACKFILL_NGINX_BLIP_S = 240

INTERVAL_KEYS: dict[str, str] = {
    "server": "system_interval",
    "postgres": "db_interval",
    "solr": "http_interval",
    "backend": "http_interval",
    "frontend": "http_interval",
    "nginx": "http_interval",
    "ssl": "http_interval",
    "ports": "service_interval",
    "security": "security_interval",
    "logs": "logs_interval",
    "repository": "repository_interval",
    "assetstore": "service_interval",
}

HEADLINE_CHECKS: tuple[tuple[str, str], ...] = (
    ("postgres", "postgres.query"),
    ("solr", "solr.search"),
    ("backend", "backend.rest"),
    ("backend", "backend.search"),
    ("frontend", "frontend.home"),
    ("nginx", "nginx.http"),
)


def _phase_hash(key: str) -> float:
    digest = hashlib.md5(key.encode("utf-8")).digest()  # noqa: S324 - not security related
    return int.from_bytes(digest[:4], "big") / 2**32 * 2 * math.pi


def noise(ts: float, key: str, period: float = 600.0) -> float:
    """Smooth deterministic noise in ``[-1, 1]`` (sum of three sines with key-derived phases)."""
    p = _phase_hash(key)
    a = math.sin(2 * math.pi * ts / period + p)
    b = math.sin(2 * math.pi * ts / (period * 0.37) + p * 1.7)
    c = math.sin(2 * math.pi * ts / (period * 2.9) + p * 0.4)
    return 0.55 * a + 0.3 * b + 0.15 * c


def diurnal(ts: float) -> float:
    """0..1 curve peaking around 14:00 local time (office-hours load)."""
    local = _dt.datetime.fromtimestamp(ts)
    hour = local.hour + local.minute / 60.0
    return 0.5 - 0.5 * math.cos(2 * math.pi * (hour - 4) / 24.0)


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * max(0.0, min(1.0, t))


def demo_backend_unit(cfg: Config) -> str:
    """The backend unit the demo shows: the demo stack's tomcat10 while the setting is the default.

    The configuration keeps Sentinel's defaults (tomcat9) until onboarding applies the demo's
    discovery ("Start monitoring"); ``doctor --demo`` and a skipped onboarding never do, and a
    DSpace 9.4 on Ubuntu 24.04 cannot run under tomcat9. A unit someone set is shown as it is.
    """
    unit = cfg.dspace.backend_service
    return DEMO_BACKEND_UNIT if unit == DEFAULT_BACKEND_UNIT else unit


def demo_log_path(path: str) -> str:
    """A log file's path in the demo: the default packaged tomcat9 catalina.out reads as tomcat10's."""
    return DEMO_TOMCAT_LOG if DEFAULT_TOMCAT_LOG and path == DEFAULT_TOMCAT_LOG else path


def demo_log_line(log_id: str, err_ts: float) -> str:
    """A realistic "last error" line for ``log_id`` stamped at ``err_ts`` in each log's own date format."""
    when = _dt.datetime.fromtimestamp(err_ts)
    ms = int((err_ts % 1.0) * 1000)
    if log_id == "dspace":
        return (
            f"{when:%Y-%m-%d %H:%M:%S},{ms:03d} ERROR org.dspace.discovery.SolrSearchCore @ "
            "Solr search core is unavailable: Connection refused"
        )
    if log_id == "tomcat":  # tomcat10's catalina.out on Ubuntu 24.04, written through rsyslog
        return (
            f"[{when:%Y-%m-%d %H:%M:%S}] [crit]Servlet.service() for servlet [dispatcherServlet] in context "
            "with path [/server] threw exception"
        )
    if log_id == "solr":
        return (
            f"{when:%Y-%m-%d %H:%M:%S}.{ms:03d} ERROR (qtp1234-19) [ x:search] o.a.s.h.RequestHandlerBase "
            "java.lang.OutOfMemoryError: Java heap space"
        )
    if log_id == "nginx":
        return (
            f"{when:%Y/%m/%d %H:%M:%S} [error] 1183#1183: *4821 upstream prematurely closed connection "
            "while reading response header from upstream"
        )
    return f"{when:%Y-%m-%d %H:%M:%S} ERROR demo error"


class Scenario:
    """The scripted state at one instant (what phase we are in and the key derived numbers)."""

    __slots__ = (
        "phase",
        "progress",
        "mem_pct",
        "solr_up",
        "solr_latency_ms",
        "search_latency_ms",
        "cpu_extra",
        "log_spike",
        "nginx_down",
        "in_incident",
        "incident_start",
        "incident_length",
    )

    def __init__(self) -> None:
        self.phase = "calm"
        self.progress = 0.0
        self.mem_pct = 61.0
        self.solr_up = True
        self.solr_latency_ms = 28.0
        self.search_latency_ms = 140.0
        self.cpu_extra = 0.0
        self.log_spike = 0.0
        self.nginx_down = False
        self.in_incident = False
        self.incident_start: float | None = None
        self.incident_length: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Plain dict (exposed in metrics for debugging the demo)."""
        return {k: getattr(self, k) for k in self.__slots__}


class DemoWorld:
    """Deterministic generator of samples and component chains for the demo stack.

    Args:
        start_ts: when the live timeline starts (defaults to now); backfill lies before it.
        cfg: the config (ports, cores, thresholds); ``Config()`` defaults when ``None``.
        fast: compress the live incident cycle to :data:`FAST_CYCLE_S`; defaults to
            :func:`demo_fast_enabled` (``SENTINEL_DEMO_FAST=1``).
    """

    def __init__(
        self, start_ts: float | None = None, cfg: Config | None = None, fast: bool | None = None
    ) -> None:
        self.start_ts = float(start_ts or time.time())
        self.cfg = cfg
        self.fast = demo_fast_enabled() if fast is None else bool(fast)
        self.cycle_s = FAST_CYCLE_S if self.fast else CYCLE_S
        self.incident_offset_s = FAST_INCIDENT_OFFSET_S if self.fast else INCIDENT_OFFSET_S
        self.incident_length_s = FAST_INCIDENT_LENGTH_S if self.fast else INCIDENT_LENGTH_S
        self.boot_ts = self.start_ts - 17 * 86400 - 3 * 3600
        self.anchor_ts = self.start_ts - 7 * 86400  # growth counters are relative to this
        self.ssl_not_after = self.start_ts + DEMO_SSL_DAYS_LEFT * 86400

    # ----------------------------------------------------------------- script

    def incident_window_for(self, ts: float) -> tuple[float, float] | None:
        """``(start, length)`` of the incident active at ``ts``, else ``None``.

        Live incidents (``ts >= start_ts``) follow the cycle (compressed in fast mode);
        back-filled incidents always run the full :data:`INCIDENT_LENGTH_S` script.
        """
        if ts >= self.start_ts:
            since = ts - self.start_ts
            cycle_start = self.start_ts + math.floor(since / self.cycle_s) * self.cycle_s
            inc = cycle_start + self.incident_offset_s
            return (inc, float(self.incident_length_s)) if inc <= ts < inc + self.incident_length_s else None
        for days in BACKFILL_INCIDENTS_DAYS_AGO:
            inc = self.start_ts - days * 86400
            if inc <= ts < inc + INCIDENT_LENGTH_S:
                return inc, float(INCIDENT_LENGTH_S)
        return None

    def incident_start_for(self, ts: float) -> float | None:
        """Start time of the incident active at ``ts``, else ``None``."""
        window = self.incident_window_for(ts)
        return None if window is None else window[0]

    def next_incident_start(self, ts: float) -> float:
        """Start time of the first live incident at or after ``ts``."""
        base = max(ts, self.start_ts)
        since = base - self.start_ts
        cycle_start = self.start_ts + math.floor(since / self.cycle_s) * self.cycle_s
        inc = cycle_start + self.incident_offset_s
        return inc if inc >= base else inc + self.cycle_s

    def scenario(self, ts: float) -> Scenario:
        """Compute the scripted state at ``ts``."""
        s = Scenario()
        base_mem = 61.0 + 1.6 * noise(ts, "mem", 1800) + 2.0 * diurnal(ts)
        s.mem_pct = base_mem
        blip = self.start_ts - BACKFILL_NGINX_BLIP_DAYS_AGO * 86400
        s.nginx_down = blip <= ts < blip + BACKFILL_NGINX_BLIP_S
        window = self.incident_window_for(ts)
        if window is None:
            return s
        inc, length = window
        s.in_incident = True
        s.incident_start = inc
        s.incident_length = length
        # phases are scripted on the INCIDENT_LENGTH_S time base; stretch/compress to this window
        offset = (ts - inc) / length * INCIDENT_LENGTH_S
        for name, lo, hi in PHASES:
            if lo <= offset < hi:
                s.phase = name
                s.progress = (offset - lo) / float(hi - lo)
                break
        p = s.progress
        if s.phase == "memory":
            s.mem_pct = _lerp(base_mem, 93.0, p)
            s.solr_latency_ms = _lerp(28.0, 320.0, p)
            s.search_latency_ms = _lerp(140.0, 700.0, p)
            s.cpu_extra = 6.0 * p
        elif s.phase == "solr_slow":
            s.mem_pct = 93.0 + 0.8 * p
            s.solr_latency_ms = _lerp(320.0, 4800.0, p)
            s.search_latency_ms = _lerp(700.0, 4300.0, p)
            s.cpu_extra = 6.0 + 22.0 * p
            s.log_spike = 0.6 * p
        elif s.phase == "solr_down":
            s.mem_pct = 94.2
            s.solr_up = False
            s.solr_latency_ms = 0.0
            s.search_latency_ms = 0.0
            s.cpu_extra = 30.0 - 12.0 * p
            s.log_spike = 1.0
        elif s.phase == "recovery":
            s.mem_pct = _lerp(94.0, base_mem, p)
            s.solr_latency_ms = _lerp(410.0, 28.0, p)
            s.search_latency_ms = _lerp(900.0, 140.0, p)
            s.cpu_extra = 18.0 * (1.0 - p)
            s.log_spike = max(0.0, 0.5 - p)
        return s

    # ---------------------------------------------------------------- samples

    def sample_at(self, ts: float, sc: Scenario | None = None) -> SystemSample:
        """Host vitals at ``ts``."""
        sc = sc or self.scenario(ts)
        cpu = 9.0 + 7.0 * diurnal(ts) + 4.0 * noise(ts, "cpu", 300) + sc.cpu_extra
        cpu = max(1.0, min(99.0, cpu))
        per_core = []
        for i in range(DEMO_CORES):
            per_core.append(round(max(0.0, min(100.0, cpu + 6.0 * noise(ts, f"core{i}", 120))), 1))
        mem_pct = max(20.0, min(99.0, sc.mem_pct))
        mem_used = int(DEMO_MEM_TOTAL * mem_pct / 100.0)
        swap_pct = 2.0 + 0.4 * noise(ts, "swap", 3600)
        if mem_pct > 85:
            swap_pct += (mem_pct - 85) * 4.2
        swap_pct = max(0.0, min(95.0, swap_pct))
        days = (ts - self.anchor_ts) / 86400.0
        disk_pct = 47.3 + 0.035 * days + 0.15 * noise(ts, "disk", 7200)
        disk_used = int(DEMO_DISK_TOTAL * disk_pct / 100.0)
        asset_used = int(DEMO_ASSETSTORE_USED + DEMO_ASSETSTORE_GROWTH_PER_DAY * days)
        asset_pct = round(100.0 * asset_used / DEMO_ASSETSTORE_TOTAL, 2)
        load1 = max(0.02, DEMO_CORES * cpu / 100.0 * 1.05 + 0.25 * noise(ts, "load", 240))
        load5 = max(0.02, load1 * 0.92 + 0.05)
        load15 = max(0.02, load1 * 0.85 + 0.08)
        rx = max(5_000.0, 120_000.0 + 90_000.0 * diurnal(ts) + 40_000.0 * noise(ts, "rx", 200))
        tx = max(20_000.0, 900_000.0 + 600_000.0 * diurnal(ts) + 250_000.0 * noise(ts, "tx", 260))
        return SystemSample(
            ts=ts,
            cpu_pct=round(cpu, 1),
            cpu_per_core=per_core,
            mem_total=DEMO_MEM_TOTAL,
            mem_used=mem_used,
            mem_available=DEMO_MEM_TOTAL - mem_used,
            mem_pct=round(mem_pct, 1),
            mem_cached=int(3.2 * GIB),
            swap_total=DEMO_SWAP_TOTAL,
            swap_used=int(DEMO_SWAP_TOTAL * swap_pct / 100.0),
            swap_pct=round(swap_pct, 1),
            disk_total=DEMO_DISK_TOTAL,
            disk_used=disk_used,
            disk_free=DEMO_DISK_TOTAL - disk_used,
            disk_pct=round(disk_pct, 1),
            disk_path="/",
            inode_pct=12.4,
            load1=round(load1, 2),
            load5=round(load5, 2),
            load15=round(load15, 2),
            cores=DEMO_CORES,
            net_rx_bps=round(rx, 0),
            net_tx_bps=round(tx, 0),
            uptime_s=round(max(0.0, ts - self.boot_ts), 0),
            os_name="Ubuntu",
            os_version="24.04",
            kernel="6.8.0-45-generic",
            hostname=DEMO_HOSTNAME,
            disks=[
                {
                    "path": "/dspace",
                    "total": DEMO_ASSETSTORE_TOTAL,
                    "used": asset_used,
                    "free": DEMO_ASSETSTORE_TOTAL - asset_used,
                    "pct": asset_pct,
                }
            ],
        )

    # ------------------------------------------------------------- repository

    def repository_at(self, ts: float) -> dict[str, Any]:
        """Repository counters at ``ts`` (slow, steady growth)."""
        days = (ts - self.anchor_ts) / 86400.0
        hour_bump = int(2 * diurnal(ts))
        asset = int(DEMO_ASSETSTORE_USED + DEMO_ASSETSTORE_GROWTH_PER_DAY * days)
        return {
            "items": DEMO_ITEMS + int(12 * days) + hour_bump,
            "bitstreams": DEMO_BITSTREAMS + int(35 * days) + 2 * hour_bump,
            "communities": DEMO_COMMUNITIES,
            "collections": DEMO_COLLECTIONS + (1 if days > 3.5 else 0),
            "db_size": DEMO_DB_SIZE + int(2.4 * MIB * days),
            "assetstore_bytes": asset,
            "eperson_count": DEMO_EPERSONS + int(1.5 * days),
            "workflow_items": DEMO_WORKFLOW_ITEMS + int(3 * noise(ts, "wf", 86400)),
            "estimated": False,
        }

    # ------------------------------------------------------------- components

    def _cfg(self) -> Config:
        return self.cfg or Config()

    def component(
        self, component_id: str, ts: float, sc: Scenario | None = None, deps: dict[str, Any] | None = None
    ) -> tuple[list[CheckResult], dict[str, Any]]:
        """Chain (rungs in ladder order) and metrics of one component at ``ts``.

        ``deps`` may hold already rolled-up :class:`ComponentHealth` of dependencies; when
        missing they are rolled up from this world so dependency rungs are always consistent.
        """
        sc = sc or self.scenario(ts)
        builder = getattr(self, f"_c_{component_id}", None)
        if builder is None:
            raise KeyError(component_id)
        return builder(ts, sc, deps or {})

    def _dep(
        self, dep_id: str, ts: float, sc: Scenario, deps: dict[str, Any], rung_id: str, label: str
    ) -> CheckResult | None:
        """Dependency rung mirroring ``dep_id``.

        ``deps[dep_id]`` may hold the dependency's current :class:`ComponentHealth`; when the key
        is absent the dependency is rolled up from this world (backfill, first cycle). An explicit
        ``None`` means *the module is switched off*: no rung at all, because a module that is off
        leaves no trace (SPEC section 0).
        """
        if dep_id in deps and deps[dep_id] is None:
            return None
        health = deps.get(dep_id)
        if health is None:
            checks, metrics = self.component(dep_id, ts, sc, deps)
            health = rollup(dep_id, checks, metrics, now=ts)
        return dependency_rung(rung_id, label, health, ts)

    @staticmethod
    def _ok(
        id: str,
        label: str,
        ts: float,
        latency: float | None = None,
        message: str = "",
        value: float | None = None,
        **detail: Any,
    ) -> CheckResult:
        return CheckResult(
            id=id,
            label=label,
            status="ok",
            ts=ts,
            latency_ms=latency,
            value=value,
            message=message,
            detail=detail,
        )

    @staticmethod
    def pg_connections(ts: float, sc: Scenario) -> int:
        """PostgreSQL connections in use at ``ts`` (of 100, so also the percentage)."""
        return int(round(18 + 4 * noise(ts, "pgconn", 400) + 6 * diurnal(ts) + (9 if sc.in_incident else 0)))

    def _c_postgres(
        self, ts: float, sc: Scenario, deps: dict[str, Any]
    ) -> tuple[list[CheckResult], dict[str, Any]]:
        cfg = self._cfg()
        conns = self.pg_connections(ts, sc)
        q_lat = round(max(0.4, 1.2 + 0.5 * noise(ts, "pgq", 90) + (1.5 if sc.in_incident else 0)), 2)
        db_size = self.repository_at(ts)["db_size"]
        checks = [
            self._ok(
                "postgres.service", "systemd postgresql", ts, message="active", unit=cfg.postgres.service
            ),
            self._ok(
                "postgres.tcp", f"TCP :{cfg.postgres.port}", ts, round(0.4 + 0.1 * abs(noise(ts, "pgtcp")), 2)
            ),
            self._ok(
                "postgres.connect",
                "Connect as dspace",
                ts,
                round(5.8 + 1.4 * abs(noise(ts, "pgc", 120)), 2),
                message=f"{cfg.postgres.user}@{cfg.postgres.host}:{cfg.postgres.port}/{cfg.postgres.database}",
            ),
            self._ok("postgres.query", "SELECT 1", ts, q_lat),
        ]
        metrics = {
            "connections": conns,
            "max_connections": 100,
            "connections_pct": conns,
            "db_size_bytes": db_size,
            "version": DEMO_POSTGRES_VERSION,
            "active_queries": 1 + int(2 * abs(noise(ts, "pgact", 60))) + (3 if sc.in_incident else 0),
            "longest_query_s": round(0.3 + 0.4 * abs(noise(ts, "pglong", 200)), 2),
        }
        return checks, metrics

    def _c_solr(
        self, ts: float, sc: Scenario, deps: dict[str, Any]
    ) -> tuple[list[CheckResult], dict[str, Any]]:
        cfg = self._cfg()
        repo = self.repository_at(ts)
        core_docs = {
            "search": repo["items"] + repo["communities"] + repo["collections"],
            "statistics": 1_842_331 + int(1400 * (ts - self.anchor_ts) / 86400.0),
            "oai": repo["items"],
            "authority": 5_210,
        }
        core_size = {
            "search": int(1.9 * GIB),
            "statistics": int(6.4 * GIB),
            "oai": int(410 * MIB),
            "authority": int(38 * MIB),
        }
        checks = [
            self._ok("solr.service", "systemd solr", ts, message="active", unit=cfg.solr.service),
        ]
        cores_metric: list[dict[str, Any]] = []
        prefix = core_prefix(list(cfg.solr.cores))
        optional = [prefix + core for core in DEMO_OPTIONAL_CORES if prefix + core not in cfg.solr.cores]
        server_cores = (
            {prefix + core for core in dspace_version.REQUIRED_CORES} | set(cfg.solr.cores) | set(optional)
        )
        if not sc.solr_up:
            checks.append(
                CheckResult(
                    id="solr.tcp",
                    label="TCP :8983",
                    status="crit",
                    ts=ts,
                    message="connection refused (localhost:8983)",
                )
            )
            checks.append(CheckResult.not_reached("solr.admin", "Admin cores", ts))
            for core in cfg.solr.cores:
                checks.append(CheckResult.not_reached(f"solr.core.{core}", f"Core {core}", ts))
                cores_metric.append(
                    {
                        "name": core,
                        "docs": core_docs.get(core),
                        "size_bytes": core_size.get(core),
                        "status": "unknown",
                    }
                )
            checks.append(CheckResult.not_reached("solr.search", "Search query", ts))
            metrics = {
                "cores": cores_metric,
                "version": DEMO_SOLR_VERSION,
                "search_docs": core_docs["search"],
                "search_latency_ms": None,
                "search_core": "search",
                "init_failures": {},
                "reload_failures": {},
            }
            return checks, metrics
        slow = sc.solr_latency_ms / 28.0 if sc.solr_latency_ms > 28 else 1.0
        checks.append(self._ok("solr.tcp", "TCP :8983", ts, round(0.5 + 0.2 * abs(noise(ts, "solrtcp")), 2)))
        checks.append(
            self._ok(
                "solr.admin",
                "Admin cores",
                ts,
                round(min(4800.0, 11.0 * slow**0.6 + 2 * abs(noise(ts, "solradm", 80))), 1),
                message=f"{len(server_cores)} cores",
                cores=sorted(server_cores),
                status_code=200,
            )
        )
        for i, core in enumerate(cfg.solr.cores):
            lat = round(min(4800.0, (4.0 + 1.5 * i) * slow**0.7 + 2 * abs(noise(ts, f"core-{core}", 70))), 1)
            checks.append(
                self._ok(
                    f"solr.core.{core}",
                    f"Core {core}",
                    ts,
                    lat,
                    value=float(core_docs.get(core, 0)),
                    docs=core_docs.get(core),
                )
            )
            cores_metric.append(
                {"name": core, "docs": core_docs.get(core), "size_bytes": core_size.get(core), "status": "ok"}
            )
        for i, core in enumerate(optional):
            lat = round(min(4800.0, (3.0 + i) * slow**0.7 + 2 * abs(noise(ts, f"core-{core}", 70))), 1)
            checks.append(
                self._ok(
                    f"solr.core.{core}",
                    f"Core {core}",
                    ts,
                    lat,
                    value=0.0,
                    docs=0,
                    size_bytes=101,
                    optional=True,
                )
            )
            cores_metric.append(
                {"name": core, "docs": 0, "size_bytes": 101, "status": "ok", "optional": True}
            )
        search_lat = round(sc.solr_latency_ms + 4.0 * abs(noise(ts, "solrsearch", 60)), 1)
        th = cfg.alerts.search_latency_ms
        status = threshold_status(search_lat, th.warning, th.critical)
        msg = "" if status == "ok" else f"search took {search_lat / 1000:.1f} s"
        checks.append(
            CheckResult(
                id="solr.search",
                label="Search query",
                status=status,
                ts=ts,
                latency_ms=search_lat,
                value=float(core_docs["search"]),
                message=msg,
                detail={
                    "query": "q=*:*&rows=0",
                    "core": "search",
                    "status_code": 200,
                    **({"slow": True} if status != "ok" else {}),
                },
            )
        )
        metrics = {
            "cores": cores_metric,
            "version": DEMO_SOLR_VERSION,
            "search_docs": core_docs["search"],
            "search_latency_ms": search_lat,
            "search_core": "search",
            "init_failures": {},
            "reload_failures": {},
        }
        return checks, metrics

    def _c_backend(
        self, ts: float, sc: Scenario, deps: dict[str, Any]
    ) -> tuple[list[CheckResult], dict[str, Any]]:
        cfg = self._cfg()
        rest_lat = round(
            91.0 + 14.0 * noise(ts, "rest", 150) + 25.0 * diurnal(ts) + (0.06 * sc.cpu_extra * 10), 1
        )
        checks = [
            self._ok(
                "backend.service",
                f"systemd {demo_backend_unit(cfg)}",
                ts,
                message="active",
                unit=demo_backend_unit(cfg),
            ),
            self._ok("backend.tcp", "TCP :8080", ts, round(0.6 + 0.2 * abs(noise(ts, "betcp")), 2)),
            self._ok(
                "backend.rest",
                "GET /server/api",
                ts,
                rest_lat,
                message=f"DSpace {DEMO_DSPACE_VERSION}",
                dspaceVersion=f"DSpace {DEMO_DSPACE_VERSION}",
                status_code=200,
            ),
        ]
        # anonymous: the aggregate status only (no GeoLite database → UP_WITH_ISSUES), DOWN with HTTP 200
        health_status = "UP_WITH_ISSUES" if sc.solr_up else "DOWN"
        grade = grade_health({"status": health_status}, dspace_version.parse(DEMO_DSPACE_VERSION))
        actuator_status, actuator_message = (grade.status, grade.message) if grade else ("unknown", "")
        solr_wait = min(sc.solr_latency_ms, 4800.0) if sc.solr_up else 0.0  # the Solr indicators wait on Solr
        checks.append(
            CheckResult(
                id="backend.actuator",
                label="Actuator health",
                status=actuator_status,
                ts=ts,
                latency_ms=round(rest_lat * (0.4 if sc.solr_up else 0.6) + solr_wait * 0.5, 1),
                message=actuator_message,
                detail={"components": {}, "status_code": 200, "health_status": health_status},
            )
        )
        if not sc.solr_up:
            checks.append(
                CheckResult(
                    id="backend.search",
                    label="Search API",
                    status="crit",
                    ts=ts,
                    latency_ms=round(2100 + 300 * abs(noise(ts, "besearchfail", 50)), 1),
                    message="HTTP 500 (discover/search/objects): Solr unreachable",
                    detail={"status_code": 500},
                )
            )
            search_lat: float | None = None
        else:
            search_lat = round(sc.search_latency_ms + 12.0 * abs(noise(ts, "besearch", 90)), 1)
            th = cfg.alerts.search_latency_ms
            status = threshold_status(search_lat, th.warning, th.critical)
            msg = "" if status == "ok" else f"search took {search_lat / 1000:.1f} s"
            checks.append(
                CheckResult(
                    id="backend.search",
                    label="Search API",
                    status=status,
                    ts=ts,
                    latency_ms=search_lat,
                    message=msg,
                    detail={"size": 1, "status_code": 200, **({"slow": True} if status != "ok" else {})},
                )
            )
        for dep_id, rung_id, label in (
            ("postgres", "backend.dep.postgres", "Database dependency"),
            ("solr", "backend.dep.solr", "Solr dependency"),
        ):
            rung = self._dep(dep_id, ts, sc, deps, rung_id, label)
            if rung is not None:
                checks.append(rung)
        metrics = {
            "dspace_version": DEMO_DSPACE_VERSION,
            "dspace_name": DEMO_DSPACE_NAME,
            **dspace_version.version_metrics({"dspaceVersion": f"DSpace {DEMO_DSPACE_VERSION}"}),
            "rest_latency_ms": rest_lat,
            "search_latency_ms": search_lat,
            "actuator_status": health_status,
            "actuator_components": {},
            "server_url": cfg.dspace.server_url.rstrip("/"),
        }
        return checks, metrics

    def _c_frontend(
        self, ts: float, sc: Scenario, deps: dict[str, Any]
    ) -> tuple[list[CheckResult], dict[str, Any]]:
        cfg = self._cfg()
        home_lat = round(640.0 + 90.0 * noise(ts, "home", 170) + 120.0 * diurnal(ts) + (sc.cpu_extra * 12), 1)
        th = cfg.alerts.home_latency_ms
        home_status = threshold_status(home_lat, th.warning, th.critical)
        host_header = public_host_headers(cfg.dspace.ui_url, cfg.dspace.public_url.strip()).get("Host")
        checks = [
            self._ok(
                "frontend.service",
                f"systemd {cfg.dspace.frontend_service}",
                ts,
                message="active",
                unit=cfg.dspace.frontend_service,
            ),
            self._ok("frontend.tcp", "TCP :4000", ts, round(0.5 + 0.2 * abs(noise(ts, "fetcp")), 2)),
            CheckResult(
                id="frontend.home",
                label="Homepage",
                status=home_status,
                ts=ts,
                latency_ms=home_lat,
                message="" if home_status == "ok" else f"homepage took {home_lat / 1000:.1f} s",
                detail={
                    "title": DEMO_DSPACE_NAME,
                    "url": cfg.dspace.ui_url,
                    "status_code": 200,
                    "rendering": "ssr",
                    **({"host_header": host_header} if host_header else {}),
                    **({"slow": True} if home_status != "ok" else {}),
                },
            ),
        ]
        public_lat: float | None = None
        if cfg.dspace.public_url:
            public_lat = round(home_lat + 70 + 20 * abs(noise(ts, "pub", 130)), 1)
            checks.append(
                self._ok(
                    "frontend.public",
                    "Public URL",
                    ts,
                    public_lat,
                    url=cfg.dspace.public_url,
                    status_code=200,
                    rendering="ssr",
                )
            )
        backend_rung = self._dep("backend", ts, sc, deps, "frontend.dep.backend", "Backend dependency")
        if backend_rung is not None:
            checks.append(backend_rung)
        metrics = {
            "home_latency_ms": home_lat,
            "public_latency_ms": public_lat,
            "title": DEMO_DSPACE_NAME,
            "rendering": "ssr",
            "generator": f"DSpace {DEMO_DSPACE_VERSION}",
            "angular": DEMO_ANGULAR_VERSION,
            "host_header": host_header,
            "ui_url": cfg.dspace.ui_url.rstrip("/"),
            "public_url": cfg.dspace.public_url.strip().rstrip("/") or None,
        }
        return checks, metrics

    def _c_nginx(
        self, ts: float, sc: Scenario, deps: dict[str, Any]
    ) -> tuple[list[CheckResult], dict[str, Any]]:
        cfg = self._cfg()
        checks = [self._ok("nginx.service", "systemd nginx", ts, message="active", unit=cfg.nginx.service)]
        http_lat = round(3.8 + 1.2 * abs(noise(ts, "nghttp", 100)), 1)
        https_lat = round(9.1 + 2.0 * abs(noise(ts, "nghttps", 110)), 1)
        if sc.nginx_down:
            checks.append(
                CheckResult(
                    id="nginx.http",
                    label="HTTP",
                    status="crit",
                    ts=ts,
                    message=f"HTTP 502 ({cfg.nginx.http_url})",
                    detail={"status_code": 502, "url": cfg.nginx.http_url},
                )
            )
            if cfg.nginx.https_url:
                checks.append(CheckResult.not_reached("nginx.https", "HTTPS", ts))
            return checks, {
                "http_latency_ms": None,
                "https_latency_ms": None,
                "server_header": "nginx/1.24.0",
            }
        checks.append(self._ok("nginx.http", "HTTP", ts, http_lat, server="nginx/1.24.0", status_code=200))
        if cfg.nginx.https_url:
            checks.append(
                self._ok("nginx.https", "HTTPS", ts, https_lat, server="nginx/1.24.0", status_code=200)
            )
        return checks, {
            "http_latency_ms": http_lat,
            "https_latency_ms": https_lat if cfg.nginx.https_url else None,
            "server_header": "nginx/1.24.0",
        }

    def _c_ssl(
        self, ts: float, sc: Scenario, deps: dict[str, Any]
    ) -> tuple[list[CheckResult], dict[str, Any]]:
        cfg = self._cfg()
        days_left = round((self.ssl_not_after - ts) / 86400.0, 1)
        th = cfg.alerts.ssl_expiry_days
        status = threshold_status(days_left, th.warning, th.critical, inverted=True)
        not_after = _dt.datetime.fromtimestamp(self.ssl_not_after, tz=_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        host = cfg.ssl.host or "repo.demo-dspace.org"
        checks = [
            CheckResult(
                id="ssl.cert",
                label="Certificate",
                status=status,
                ts=ts,
                latency_ms=round(38 + 6 * abs(noise(ts, "ssl", 500)), 1),
                value=days_left,
                message="" if status == "ok" else f"expires in {days_left:.0f} days",
                detail={"host": host, "port": cfg.ssl.port},
            )
        ]
        metrics = {
            "days_left": days_left,
            "not_after": not_after,
            "issuer": "R11 (Let's Encrypt)",
            "subject": host,
            "sans": [host, f"www.{host}"],
        }
        return checks, metrics

    def _c_ports(
        self, ts: float, sc: Scenario, deps: dict[str, Any]
    ) -> tuple[list[CheckResult], dict[str, Any]]:
        cfg = self._cfg()
        checks: list[CheckResult] = []
        listening: list[dict[str, Any]] = []
        for rule in cfg.ports:
            public = rule.expect == "open"
            if rule.port == 8983 and not sc.solr_up:
                checks.append(
                    CheckResult(
                        id=f"ports.{rule.port}",
                        label=f"{rule.label} :{rule.port}",
                        status="unknown",
                        ts=ts,
                        message="not listening",
                        detail={"expect": rule.expect},
                    )
                )
                continue
            addr = "0.0.0.0" if public else "127.0.0.1"
            listening.append({"port": rule.port, "addr": addr, "public": public})
            msg = "listening publicly" if public else "listening on localhost only"
            checks.append(
                CheckResult(
                    id=f"ports.{rule.port}",
                    label=f"{rule.label} :{rule.port}",
                    status="ok",
                    ts=ts,
                    message=msg,
                    detail={"expect": rule.expect, "addr": addr},
                )
            )
        return checks, {"listening": listening}

    def _c_security(
        self, ts: float, sc: Scenario, deps: dict[str, Any]
    ) -> tuple[list[CheckResult], dict[str, Any]]:
        """The demo server's Security component, built like the real collector builds it.

        The ports the ``ports`` rules expect (open ones public, local-only ones on loopback; Solr's
        while Solr is down is gone), ufw active, SSH with prohibit-password and passwords on, HTTPS
        without HSTS, 3 pending updates and no security update, every version supported, local.cfg
        640 tomcat:tomcat and Sentinel on 127.0.0.1. Every rung is ok, so the demo starts calm; the
        SSH and web hints still give the Security page its advice lines. Program names stay empty:
        on a real server Sentinel only sees those of its own user's sockets.
        """
        cfg = self._cfg()
        sockets: list[ListeningSocket] = []
        for rule in cfg.ports:
            if rule.port == 8983 and not sc.solr_up:
                continue
            public = rule.expect == "open"
            sockets.append(ListeningSocket(rule.port, "0.0.0.0" if public else "127.0.0.1", public, "ipv4"))
        local_cfg = f"{(cfg.dspace.install_dir or '/dspace').rstrip('/')}/config/local.cfg"
        dspace_cfg = local_cfg[: -len("local.cfg")] + "dspace.cfg"
        pending, security_updates = DEMO_UPDATES
        facts = HostFacts(
            sockets=sockets,
            ufw_enabled=True,
            ssh=SshFacts(found=True, readable=True),
            updates=UpdatesFacts(
                available=True, pending=pending, security=security_updates, source=UPDATES_AVAILABLE
            ),
            os_release=dict(DEMO_OS_RELEASE),
            files=[
                FileFacts(local_cfg, 0o100640, "tomcat", "tomcat", True),
                FileFacts(dspace_cfg, 0o100644, "tomcat", "tomcat", True),
                FileFacts(DEMO_SENTINEL_CONFIG, 0o100600, "sentinel", "sentinel", True),
            ],
            local_cfg=local_cfg,
            sentinel_yaml=DEMO_SENTINEL_CONFIG,
            sentinel_env=DEMO_SENTINEL_CONFIG.rsplit("/", 1)[0] + "/sentinel.env",
        )
        firewall = FirewallState(
            True, "ufw", {"ufw": "active", "nftables": "inactive", "firewalld": "missing"}
        )
        public_url = cfg.dspace.public_url.strip() or DEMO_PUBLIC_URL
        headers = {
            "server": "nginx/1.24.0",
            "content-type": "text/html; charset=utf-8",
            "x-content-type-options": "nosniff",
            "x-frame-options": "SAMEORIGIN",
        }
        web = WebAnswer(public_url.rstrip("/") + "/", 200, headers)
        versions = {
            "dspace": DEMO_DSPACE_VERSION if cfg.module_enabled("backend") else None,
            "solr": DEMO_SOLR_VERSION if cfg.module_enabled("solr") else None,
            "postgres": DEMO_POSTGRES_VERSION if cfg.module_enabled("postgres") else None,
        }
        baseline = {(s.port, s.proto) for s in sockets if s.public}
        return build_ladder(
            cfg, facts, firewall, web, versions, ts, baseline=baseline, env={}, public_url=public_url
        )

    def _c_logs(
        self, ts: float, sc: Scenario, deps: dict[str, Any]
    ) -> tuple[list[CheckResult], dict[str, Any]]:
        cfg = self._cfg()
        th = cfg.alerts.log_errors_per_window
        base = {"dspace": (1.2, 6.0), "tomcat": (0.2, 2.0), "solr": (0.3, 3.0), "nginx": (0.4, 1.5)}
        spike = {"dspace": 118.0, "tomcat": 9.0, "solr": 64.0, "nginx": 22.0}
        checks: list[CheckResult] = []
        per_file: dict[str, Any] = {}
        for lf in cfg.logs.files:
            b_err, b_warn = base.get(lf.id, (0.5, 2.0))
            errors = int(
                round(
                    b_err
                    + b_err * abs(noise(ts, f"log-{lf.id}", 700))
                    + spike.get(lf.id, 20.0) * sc.log_spike
                )
            )
            warnings = int(
                round(
                    b_warn
                    + b_warn * abs(noise(ts, f"logw-{lf.id}", 500))
                    + 3 * spike.get(lf.id, 20.0) * sc.log_spike / 10
                )
            )
            status = threshold_status(float(errors), th.warning, th.critical)
            msg = "" if status == "ok" else f"{errors} errors in {cfg.logs.window_minutes} min"
            last_error = None
            if errors:
                err_ts = ts - 15 - 40 * abs(noise(ts, f"logts-{lf.id}", 90))
                last_error = {"line": demo_log_line(lf.id, err_ts), "ts": err_ts}
            checks.append(
                CheckResult(
                    id=f"logs.{lf.id}",
                    label=f"{lf.label or lf.id} log",
                    status=status,
                    ts=ts,
                    value=float(errors),
                    message=msg,
                    detail={"errors": errors, "warnings": warnings, "path": demo_log_path(lf.path)},
                )
            )
            per_file[lf.id] = {
                "errors": errors,
                "warnings": warnings,
                "last_error": last_error,
                "path": demo_log_path(lf.path),
                "label": lf.label or lf.id,
            }
        return checks, {"files": per_file, "window_minutes": cfg.logs.window_minutes}

    def _c_repository(
        self, ts: float, sc: Scenario, deps: dict[str, Any]
    ) -> tuple[list[CheckResult], dict[str, Any]]:
        return [], self.repository_at(ts)

    def _c_assetstore(
        self, ts: float, sc: Scenario, deps: dict[str, Any]
    ) -> tuple[list[CheckResult], dict[str, Any]]:
        cfg = self._cfg()
        days = (ts - self.anchor_ts) / 86400.0
        used = int(DEMO_ASSETSTORE_USED + DEMO_ASSETSTORE_GROWTH_PER_DAY * days)
        free = DEMO_ASSETSTORE_TOTAL - used
        th = cfg.alerts.assetstore_free_gb
        status = threshold_status(free / GIB, th.warning, th.critical, inverted=True)
        checks = [
            CheckResult(
                id="assetstore.path",
                label="Assetstore readable",
                status=status,
                ts=ts,
                latency_ms=round(0.3 + 0.2 * abs(noise(ts, "asset", 300)), 2),
                value=round(free / GIB, 1),
                message="" if status == "ok" else f"only {free / GIB:.1f} GB free",
                detail={"path": cfg.dspace.assetstore_dir},
            )
        ]
        metrics = {
            "path": cfg.dspace.assetstore_dir,
            "mount": "/dspace",
            "total": DEMO_ASSETSTORE_TOTAL,
            "used": used,
            "free": free,
            "pct": round(100.0 * used / DEMO_ASSETSTORE_TOTAL, 2),
            "growth_bytes_per_day": int(DEMO_ASSETSTORE_GROWTH_PER_DAY),
        }
        return checks, metrics

    # --------------------------------------------------------------- backfill

    def headline_statuses(
        self, ts: float, sc: Scenario | None = None, cfg: Config | None = None
    ) -> dict[str, tuple[str, float | None]]:
        """Cheap per-component ``(status, latency_ms)`` used by the backfill (no chain objects)."""
        sc = sc or self.scenario(ts)
        cfg = cfg or self._cfg()
        th_s = cfg.alerts.search_latency_ms
        th_h = cfg.alerts.home_latency_ms
        out: dict[str, tuple[str, float | None]] = {}
        sample_status = "ok"
        if sc.mem_pct >= cfg.alerts.memory.critical:
            sample_status = "crit"
        elif sc.mem_pct >= cfg.alerts.memory.warning:
            sample_status = "warn"
        out["server"] = (sample_status, None)
        out["postgres"] = ("ok", round(1.2 + 0.5 * noise(ts, "pgq", 90) + (1.5 if sc.in_incident else 0), 2))
        if sc.solr_up:
            lat = round(sc.solr_latency_ms + 4.0 * abs(noise(ts, "solrsearch", 60)), 1)
            out["solr"] = (threshold_status(lat, th_s.warning, th_s.critical), lat)
            slat = round(sc.search_latency_ms + 12.0 * abs(noise(ts, "besearch", 90)), 1)
            b_status = threshold_status(slat, th_s.warning, th_s.critical)
        else:
            out["solr"] = ("crit", None)
            slat = None
            b_status = "crit"
        rest_lat = round(
            91.0 + 14.0 * noise(ts, "rest", 150) + 25.0 * diurnal(ts) + (0.06 * sc.cpu_extra * 10), 1
        )
        out["backend"] = (b_status, rest_lat)
        out["backend.search"] = (b_status, slat)
        home_lat = round(640.0 + 90.0 * noise(ts, "home", 170) + 120.0 * diurnal(ts) + (sc.cpu_extra * 12), 1)
        f_status = threshold_status(home_lat, th_h.warning, th_h.critical)
        if b_status == "crit" and f_status == "ok":
            f_status = "warn"  # dependency rung capped at warn
        out["frontend"] = (f_status, home_lat)
        out["nginx"] = (
            "crit" if sc.nginx_down else "ok",
            None if sc.nginx_down else round(3.8 + 1.2 * abs(noise(ts, "nghttp", 100)), 1),
        )
        out["ssl"] = ("ok", None)
        out["ports"] = ("ok", None)
        out["security"] = ("ok", None)  # every rung ok: no pending security update (DEMO_UPDATES)
        log_status = "ok"
        if sc.log_spike >= 0.8:
            log_status = "crit"
        elif sc.log_spike > 0.15:
            log_status = "warn"
        out["logs"] = (log_status, None)
        out["repository"] = ("ok", None)
        out["assetstore"] = ("ok", None)
        return out


# ----------------------------------------------------------------------------- collectors


class DemoSystemCollector(Collector):
    """Demo replacement for the system collector."""

    id: ClassVar[str] = "server"
    interval_key: ClassVar[str] = "system_interval"
    label: ClassVar[str] = "Server"
    emoji: ClassVar[str] = "💻"

    def __init__(self, world: DemoWorld) -> None:
        self.world = world

    async def collect(self, ctx: CollectorContext) -> CollectorResult:
        sc = self.world.scenario(ctx.now)
        sample = self.world.sample_at(ctx.now, sc)
        metrics = {
            "cpu_pct": sample.cpu_pct,
            "mem_pct": sample.mem_pct,
            "disk_pct": sample.disk_pct,
            "load1": sample.load1,
            "per_core_load": round(sample.load_per_core, 2),
            "uptime": sample.uptime_s,
            "os": f"{sample.os_name} {sample.os_version}",
            "kernel": sample.kernel,
            "demo_phase": sc.phase,
            "demo_fast": self.world.fast,
            "demo_cycle_s": self.world.cycle_s,
            "demo_next_incident_ts": self.world.next_incident_start(ctx.now),
        }
        return CollectorResult(checks=[], metrics=metrics, sample=sample)


class DemoComponentCollector(Collector):
    """Demo replacement for one stack component (postgres, solr, backend, …)."""

    def __init__(self, world: DemoWorld, component_id: str) -> None:
        meta = COMPONENTS[component_id]
        self.world = world
        self.id = component_id  # type: ignore[misc]
        self.interval_key = INTERVAL_KEYS.get(component_id, "service_interval")  # type: ignore[misc]
        self.label = meta.label  # type: ignore[misc]
        self.emoji = meta.emoji  # type: ignore[misc]
        self.depends_on = list(meta.depends_on)  # type: ignore[misc]

    async def collect(self, ctx: CollectorContext) -> CollectorResult:
        self.world.cfg = ctx.cfg
        sc = self.world.scenario(ctx.now)
        deps: dict[str, Any] = {}
        for dep in self.depends_on:
            if not ctx.cfg.module_enabled(dep):
                deps[dep] = None  # module off → no dependency rung (leaves no trace)
            elif dep in ctx.state.components:
                deps[dep] = ctx.state.components[dep]
        checks, metrics = self.world.component(self.id, ctx.now, sc, deps)
        if self.id != "server":
            metrics = {**metrics, "demo_phase": sc.phase}
        repo = metrics if self.id == "repository" else None
        history = connections_row(ctx.now, metrics, ctx.cfg) if self.id == "postgres" else []
        return CollectorResult(checks=checks, metrics=metrics, repository_stats=repo, history=history)


class DemoSuite:
    """Builds the demo collectors for the modules that are on and backfills history."""

    def __init__(self, cfg: Config, start_ts: float | None = None, world: DemoWorld | None = None) -> None:
        self.cfg = cfg
        self.world = world or DemoWorld(start_ts, cfg)

    def collectors(self) -> list[Collector]:
        """Demo collectors: server always, plus one per enabled stack module (menu order)."""
        out: list[Collector] = [DemoSystemCollector(self.world)]
        for cid in (
            "postgres",
            "solr",
            "backend",
            "frontend",
            "nginx",
            "ssl",
            "ports",
            "security",
            "logs",
            "repository",
            "assetstore",
        ):
            if self.cfg.module_enabled(cid):
                out.append(DemoComponentCollector(self.world, cid))
        return out

    # --------------------------------------------------------------- backfill

    def backfill(
        self, store: Store, days: int = 7, now: float | None = None, force: bool = False
    ) -> dict[str, int]:
        """Insert ``days`` of synthetic history on first start (idempotent via ``kv.demo_backfilled``).

        Samples every 60 s, headline checks (and ``postgres.connections``) every 60 s, component
        history every 120 s, repository stats hourly, plus the alerts/incidents/events of the
        scripted incidents.
        Uses ``executemany`` so seven days load in well under three seconds.
        """
        backfilled = store.kv_get("demo_backfilled")
        if not force and backfilled:
            # A demo restarted later continues the same history: keep the growth counters
            # anchored to the first start (seven days before it) so they never go backwards.
            try:
                self.world.anchor_ts = float(backfilled) - 7 * 86400
            except ValueError:
                pass
            return {}
        now = now or self.world.start_ts
        t0 = int(now - days * 86400)
        t_end = int(now) - 60
        world = self.world
        cfg = self.cfg

        sample_rows: list[tuple[Any, ...]] = []
        check_rows: list[tuple[Any, ...]] = []
        history_rows: list[tuple[Any, ...]] = []
        components = [
            cid
            for cid in (
                "server",
                "postgres",
                "solr",
                "backend",
                "frontend",
                "nginx",
                "ssl",
                "ports",
                "security",
                "logs",
                "repository",
                "assetstore",
            )
            if cfg.module_enabled(cid)
        ]
        pg_on = cfg.module_enabled("postgres")
        pg_th = cfg.alerts.postgres_connections_pct
        ts = t0 - (t0 % 60)
        while ts <= t_end:
            sc = world.scenario(ts)
            sample = world.sample_at(ts, sc)
            sample_rows.append(store._sample_row(sample))  # noqa: SLF001 - same package, hot loop
            heads = world.headline_statuses(ts, sc, cfg)
            for cid, check_id in HEADLINE_CHECKS:
                if not cfg.module_enabled(cid):
                    continue
                key = "backend.search" if check_id == "backend.search" else cid
                status, lat = heads[key]
                check_rows.append((ts, check_id, status, lat, None, ""))
            if pg_on:
                conns = world.pg_connections(ts, sc)
                status = threshold_status(float(conns), pg_th.warning, pg_th.critical)
                check_rows.append((ts, CONNECTIONS_CHECK_ID, status, None, float(conns), f"{conns} / 100"))
            if ts % 120 == 0:
                for cid in components:
                    status, lat = heads[cid]
                    history_rows.append((ts, cid, status, lat))
            ts += 60

        counts = {
            "samples": store.insert_sample_rows(sample_rows),
            "checks": store.insert_check_rows(check_rows),
            "component_history": store.insert_component_history_rows(history_rows),
        }
        if cfg.module_enabled("repository"):
            repo_rows = []
            ts = t0 - (t0 % 3600)
            while ts <= t_end:
                repo_rows.append((ts, world.repository_at(ts)))
                ts += 3600
            counts["repository_stats"] = store.insert_repository_stats_bulk(repo_rows)

        counts["alerts"], counts["incidents"] = self._backfill_incidents(store, t0, now)
        store.kv_set("demo_backfilled", str(int(now)))
        store.add_event("demo", None, f"Demo history backfilled: {days} days", {"counts": counts}, ts=now)
        return counts

    def _backfill_incidents(self, store: Store, t0: float, now: float) -> tuple[int, int]:
        """Insert resolved alerts + incidents (+ events) for each scripted incident inside the window."""
        n_alerts = n_incidents = 0
        cfg = self.cfg
        for days_ago in BACKFILL_INCIDENTS_DAYS_AGO:
            inc_start = int(self.world.start_ts - days_ago * 86400)
            if inc_start < t0 or inc_start + INCIDENT_LENGTH_S > now:
                continue
            mem_warn_ts = inc_start + 110
            mem_crit_ts = inc_start + 175
            solr_slow_ts = inc_start + 255
            solr_down_ts = inc_start + 320
            search_ts = inc_start + 335
            recovered = inc_start + INCIDENT_LENGTH_S - 40
            alerts = [
                Alert(
                    None,
                    "memory",
                    "server",
                    "critical",
                    "resolved",
                    "Memory 94 %",
                    "15.1 of 16 GB used, swap 40 %",
                    94.2,
                    cfg.alerts.memory.critical,
                    mem_warn_ts,
                    recovered,
                ),
                Alert(
                    None,
                    "solr_latency",
                    "solr",
                    "critical",
                    "resolved",
                    "Solr search slow: 4.8 s",
                    "select q=*:* rows=0 on core search",
                    4800.0,
                    cfg.alerts.search_latency_ms.critical,
                    solr_slow_ts,
                    recovered - 60,
                ),
                Alert(
                    None,
                    "search_latency",
                    "backend",
                    "critical",
                    "resolved",
                    "Search API slow: 4.3 s",
                    "GET /server/api/discover/search/objects",
                    4300.0,
                    cfg.alerts.search_latency_ms.critical,
                    solr_slow_ts,
                    recovered - 60,
                ),
                Alert(
                    None,
                    "component_down:solr",
                    "solr",
                    "critical",
                    "resolved",
                    "Solr unreachable",
                    "service ✅ → TCP ❌\nconnection refused (localhost:8983)",
                    None,
                    None,
                    solr_down_ts,
                    recovered - 120,
                ),
                Alert(
                    None,
                    "component_down:backend",
                    "backend",
                    "critical",
                    "resolved",
                    "DSpace Backend failing",
                    "service ✅ → TCP ✅ → rest ✅ → actuator ⚠️ → search ❌\n"
                    "HTTP 500 (discover/search/objects): Solr unreachable",
                    None,
                    None,
                    search_ts,
                    recovered - 100,
                ),
                Alert(
                    None,
                    "log_errors:dspace",
                    "logs",
                    "critical",
                    "resolved",
                    "118 errors in DSpace log (5 min)",
                    demo_log_line("dspace", search_ts + 25),
                    118.0,
                    cfg.alerts.log_errors_per_window.critical,
                    search_ts + 25,
                    recovered,
                ),
            ]
            ids = [store.insert_alert(a) for a in alerts]
            n_alerts += len(ids)
            sequence = [
                {"ts": float(mem_warn_ts), "text": "Memory 81 %", "severity": "warning", "alert_id": ids[0]},
                {"ts": float(mem_crit_ts), "text": "Memory 94 %", "severity": "critical", "alert_id": ids[0]},
                *(sequence_entry(a) for a in alerts[1:]),
            ]
            incident = Incident(
                None,
                "Possible Solr resource exhaustion",
                "Possible Solr resource exhaustion",
                "resolved",
                mem_warn_ts,
                recovered,
                ["DSpace Search", "DSpace Backend"],
                sequence,
                ids,
            )
            store.insert_incident(incident)
            n_incidents += 1
            for a in alerts:
                store.add_event(
                    "alert",
                    a.component,
                    f"{a.severity.upper()} {a.title}",
                    {"alert_id": a.id, "type": "opened", "rule": a.rule, "severity": a.severity},
                    ts=a.opened_ts,
                )
            store.add_event(
                "incident",
                "solr",
                f"Incident #{incident.id}: {incident.title}",
                {"incident_id": incident.id, "type": "opened", "alert_ids": ids},
                ts=mem_warn_ts,
            )
            store.add_event(
                "component",
                "solr",
                "Solr: Healthy → Critical — connection refused (localhost:8983)",
                {"from": "ok", "to": "crit"},
                ts=solr_down_ts,
            )
            store.add_event(
                "component",
                "solr",
                "Solr: Critical → Healthy",
                {"from": "crit", "to": "ok"},
                ts=recovered - 120,
            )
            store.add_event(
                "incident",
                "solr",
                f"Incident #{incident.id} resolved after {duration(recovered - mem_warn_ts)}",
                {"incident_id": incident.id, "type": "resolved"},
                ts=recovered,
            )
        return n_alerts, n_incidents


__all__ = [
    "BACKFILL_INCIDENTS_DAYS_AGO",
    "CYCLE_S",
    "DEFAULT_BACKEND_UNIT",
    "DEFAULT_TOMCAT_LOG",
    "DEMO_BACKEND_UNIT",
    "DEMO_HOSTNAME",
    "DEMO_TOMCAT_LOG",
    "FAST_CYCLE_S",
    "FAST_INCIDENT_LENGTH_S",
    "FAST_INCIDENT_OFFSET_S",
    "FAST_INTERVAL_S",
    "INCIDENT_LENGTH_S",
    "INCIDENT_OFFSET_S",
    "INTERVAL_KEYS",
    "PHASES",
    "DemoComponentCollector",
    "DemoSuite",
    "DemoSystemCollector",
    "DemoWorld",
    "Scenario",
    "demo_backend_unit",
    "demo_fast_enabled",
    "demo_log_line",
    "demo_log_path",
    "diurnal",
    "noise",
]
