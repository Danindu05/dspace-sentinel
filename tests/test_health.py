"""Health: roll-up, not reached, dependency capping, server thresholds, mood, headline, closure."""

from __future__ import annotations

import time
from typing import Any

from dspace_sentinel import health
from dspace_sentinel.config import Config
from dspace_sentinel.models import CheckResult, ComponentHealth, SystemSample, worst
from dspace_sentinel.state import State


def rung(
    id: str, status: str, latency: float | None = None, message: str = "", ts: float = 1000.0
) -> CheckResult:
    return CheckResult(
        id=id,
        label=id,
        status=status,
        ts=ts,
        latency_ms=latency,
        message=message,  # type: ignore[arg-type]
    )


def sample(**over: object) -> SystemSample:
    base = dict(
        ts=1000.0,
        cpu_pct=10.0,
        cpu_per_core=[10.0],
        mem_total=16,
        mem_used=8,
        mem_available=8,
        mem_pct=50.0,
        mem_cached=0,
        swap_total=4,
        swap_used=0,
        swap_pct=0.0,
        disk_total=100,
        disk_used=40,
        disk_free=60,
        disk_pct=40.0,
        disk_path="/",
        inode_pct=1.0,
        load1=1.0,
        load5=1.0,
        load15=1.0,
        cores=4,
        net_rx_bps=0.0,
        net_tx_bps=0.0,
        uptime_s=1.0,
        os_name="Ubuntu",
        os_version="24.04",
        kernel="6.8",
        hostname="h",
        disks=[],
    )
    base.update(over)
    return SystemSample(**base)  # type: ignore[arg-type]


def test_worst() -> None:
    assert worst(["ok", "warn", "ok"]) == "warn"
    assert worst(["ok", "unknown"]) == "unknown"
    assert worst(["warn", "unknown"]) == "warn"
    assert worst(["crit", "warn"]) == "crit"
    assert worst(["off", "ok"]) == "ok"
    assert worst([]) == "unknown"
    assert worst(["off"]) == "unknown"


def test_rollup_worst_reason_latency_since() -> None:
    chain = [
        rung("postgres.service", "ok"),
        rung("postgres.tcp", "ok", 0.4),
        rung("postgres.connect", "crit", message="connection refused (localhost:5432)"),
        CheckResult.not_reached("postgres.query", "SELECT 1", 1000.0),
    ]
    comp = health.rollup("postgres", chain, {"x": 1}, now=1000.0)
    assert comp.status == "crit"
    assert comp.reason == "connection refused (localhost:5432)"
    assert comp.label == "PostgreSQL" and comp.emoji == "🐘"
    assert comp.latency_ms == 0.4  # headline rung unreachable → first ok rung with a latency
    assert comp.chain[-1].message == "not reached" and comp.chain[-1].status == "unknown"
    assert comp.since == 1000.0 and comp.checked_at == 1000.0
    # status unchanged keeps `since`
    again = health.rollup("postgres", chain, {}, previous=comp, now=2000.0)
    assert again.since == 1000.0
    # status change resets it
    ok_chain = [rung("postgres.service", "ok"), rung("postgres.query", "ok", 1.5)]
    back = health.rollup("postgres", ok_chain, {}, previous=again, now=3000.0)
    assert back.status == "ok" and back.since == 3000.0 and back.reason is None and back.latency_ms == 1.5


def test_rollup_unknown_only_beats_ok_and_metrics_only() -> None:
    comp = health.rollup(
        "solr", [rung("solr.service", "unknown", message="systemctl missing"), rung("solr.tcp", "ok")]
    )
    assert comp.status == "unknown" and comp.reason == "systemctl missing"
    warn = health.rollup("solr", [rung("solr.service", "unknown"), rung("solr.search", "warn", 2000, "slow")])
    assert warn.status == "warn"
    assert health.rollup("repository", [], {"items": 1}).status == "ok"
    assert health.rollup("repository", [], {}).status == "unknown"
    assert health.rollup("mystery", [rung("mystery.x", "ok")]).label == "Mystery"


def test_dependency_rung_capped_at_warn() -> None:
    pg = ComponentHealth("postgres", "PostgreSQL", "🐘", "crit", "connection refused", [], {}, [], None, 1, 1)
    dep = health.dependency_rung("backend.dep.postgres", "Database dependency", pg, ts=5.0)
    assert dep.status == "warn"
    assert dep.message == "PostgreSQL is down (Critical): connection refused"
    assert dep.detail["dependency_status"] == "crit"
    pg.status, pg.reason = "ok", None
    assert health.dependency_rung("backend.dep.postgres", "Database dependency", pg).status == "ok"
    assert health.dependency_rung("backend.dep.postgres", "Database dependency", None).status == "unknown"
    assert health.not_reached([("a.b", "B"), ("a.c", "C")], 1.0)[1].message == "not reached"


def test_dependency_rung_says_slow_when_only_latency_made_it_critical() -> None:
    """A dependency that answers correctly but past its critical latency is slow, not down."""
    slow = CheckResult(
        id="solr.search",
        label="Search query",
        status="crit",
        ts=1.0,
        latency_ms=4300.0,
        message="search took 4.3 s",
        detail={"slow": True},
    )
    chain = [rung("solr.tcp", "ok", 0.4), slow]
    solr = ComponentHealth("solr", "Solr", "☀️", "crit", "search took 4.3 s", chain, {}, [], 4300.0, 1, 1)
    dep = health.dependency_rung("backend.dep.solr", "Solr dependency", solr, ts=5.0)
    assert (dep.status, dep.message) == ("warn", "Solr is slow (Critical): search took 4.3 s")
    assert dep.detail["dependency_status"] == "crit"
    # a real failure beside the slow rung: down
    solr.chain = [rung("solr.core.oai", "crit", message="core not found (HTTP 404)"), slow]
    solr.reason = "core not found (HTTP 404)"
    dep = health.dependency_rung("backend.dep.solr", "Solr dependency", solr, ts=5.0)
    assert dep.message == "Solr is down (Critical): core not found (HTTP 404)"
    # without a chain nothing says it is only slow
    solr.chain = []
    assert health.dependency_rung("backend.dep.solr", "Solr dependency", solr).message.startswith(
        "Solr is down"
    )
    # slow at warning level keeps its words
    warn = ComponentHealth("solr", "Solr", "☀️", "warn", "search took 2.1 s", [slow], {}, [], 2100.0, 1, 1)
    assert health.dependency_rung("x", "y", warn).message == "Solr is degraded (Warning): search took 2.1 s"


def slow_rung(id: str, label: str, message: str, latency: float) -> CheckResult:
    """A rung that answered correctly, only past its critical latency (as the collectors mark it)."""
    return CheckResult(
        id=id,
        label=label,
        status="crit",
        ts=1000.0,
        latency_ms=latency,
        message=message,
        detail={"slow": True},
    )


def test_dependency_rung_takes_its_reason_from_the_rung_that_decides_the_state() -> None:
    """'is down' names the failed rung and 'is slow' the slow one, whichever rung is the first
    problem of the chain (the component's own reason stays its first problem)."""
    rest_slow = slow_rung("backend.rest", "GET /server/api", "REST API took 61 ms", 61.0)
    search_500 = rung("backend.search", "crit", 30.0, "HTTP 500")
    backend = health.rollup("backend", [rung("backend.tcp", "ok", 0.4), rest_slow, search_500], now=1000.0)
    assert (backend.status, backend.reason) == ("crit", "REST API took 61 ms")
    dep = health.dependency_rung("frontend.dep.backend", "Backend dependency", backend, ts=5.0)
    assert (dep.status, dep.message) == ("warn", "DSpace Backend is down (Critical): HTTP 500")

    # an anonymous actuator DOWN (a warning) above a critical-slow search: slow, because of the search
    actuator = rung("backend.actuator", "warn", message="status DOWN (details need a DSpace admin login)")
    search_slow = slow_rung("backend.search", "Search API", "search took 4.3 s", 4300.0)
    backend = health.rollup("backend", [rung("backend.rest", "ok", 91.0), actuator, search_slow], now=1000.0)
    assert backend.reason == "status DOWN (details need a DSpace admin login)"
    dep = health.dependency_rung("frontend.dep.backend", "Backend dependency", backend, ts=5.0)
    assert dep.message == "DSpace Backend is slow (Critical): search took 4.3 s"

    # a Solr core whose reload failed (a warning) above a critical-slow search: slow, because of the search
    reload = rung(
        "solr.core.search",
        "warn",
        12.0,
        "reload failed: Unable to reload core [search]: Could not load conf for core search "
        "(still serving; a restart would not load it)",
    )
    solr_slow = slow_rung("solr.search", "Search query", "search took 4.3 s", 4300.0)
    solr = health.rollup("solr", [rung("solr.admin", "ok", 3.0), reload, solr_slow], now=1000.0)
    assert solr.reason.startswith("reload failed")
    dep = health.dependency_rung("backend.dep.solr", "Solr dependency", solr, ts=5.0)
    assert (dep.status, dep.message) == ("warn", "Solr is slow (Critical): search took 4.3 s")

    # a warning names the warning, not a counted unknown above it
    stuck = rung("backend.service", "unknown", message="systemctl did not answer")
    backend = health.rollup("backend", [stuck, rung("backend.rest", "ok", 91.0), actuator], now=1000.0)
    assert (backend.status, backend.reason) == ("warn", "systemctl did not answer")
    assert health.dependency_rung("x", "y", backend).message == (
        "DSpace Backend is degraded (Warning): status DOWN (details need a DSpace admin login)"
    )
    # an informational rung never decides
    missing = CheckResult(
        id="solr.service",
        label="systemd solr",
        status="unknown",
        ts=1000.0,
        message="unit solr not found",
        detail={"informational": True},
    )
    solr = health.rollup("solr", [missing, rung("solr.error", "unknown", message="timed out after 5 s")])
    assert (
        health.dependency_rung("x", "y", solr).message
        == "Solr could not be checked (Unknown): timed out after 5 s"
    )


def test_threshold_status() -> None:
    assert health.threshold_status(50, 80, 95) == "ok"
    assert health.threshold_status(80, 80, 95) == "warn"
    assert health.threshold_status(96, 80, 95) == "crit"
    assert health.threshold_status(None, 80, 95) == "unknown"
    assert health.threshold_status(40, 30, 7, inverted=True) == "ok"
    assert health.threshold_status(12, 30, 7, inverted=True) == "warn"
    assert health.threshold_status(3, 30, 7, inverted=True) == "crit"


def test_server_status_and_health() -> None:
    cfg = Config()
    status, reason, per = health.server_status(sample(), cfg)
    assert status == "ok" and reason is None and per["load"] == "ok"
    status, reason, _ = health.server_status(sample(mem_pct=93.0), cfg)
    assert status == "crit" and reason == "Memory 93 %"
    status, reason, _ = health.server_status(sample(load1=8.0), cfg)
    assert status == "warn" and reason == "Load 2.0 per core"
    status, reason, per = health.server_status(sample(disks=[{"path": "/dspace", "pct": 91.0}]), cfg)
    assert status == "crit" and reason == "Disk 91 % on /dspace" and per["disk:/dspace"] == "crit"
    assert health.server_status(None, cfg)[0] == "unknown"
    comp = health.server_health(sample(cpu_pct=85.0), cfg, now=10.0)
    assert comp.id == "server" and comp.status == "warn" and comp.reason == "CPU 85 %"
    assert comp.metrics["per_core_load"] == 0.25 and comp.chain == []


def test_mood_headline_subline() -> None:
    cfg = Config()
    state = State()
    assert health.mood(state, cfg) == "unknown"
    assert health.headline(state, cfg) == ("unknown", "Waiting for the first check")
    state.set_component(health.server_health(sample(), cfg, now=1.0))
    state.set_component(health.rollup("postgres", [rung("postgres.query", "ok", 1.0)]))
    assert health.headline(state, cfg) == ("ok", "All systems healthy")
    state.set_component(health.rollup("solr", [rung("solr.search", "warn", 2000, "search took 2.0 s")]))
    assert health.headline(state, cfg) == ("warn", "1 warning")
    state.set_component(health.rollup("nginx", [rung("nginx.http", "warn", 1, "HTTP 502")]))
    assert health.headline(state, cfg)[1] == "2 warnings"
    state.set_component(
        health.rollup("postgres", [rung("postgres.tcp", "crit", message="connection refused")])
    )
    assert health.headline(state, cfg) == ("crit", "Critical: PostgreSQL unreachable")
    state.set_component(health.rollup("backend", [rung("backend.search", "crit", message="HTTP 500")]))
    assert health.headline(state, cfg)[1] == "Critical: PostgreSQL unreachable (+1 more)"
    state.components.clear()
    state.set_component(health.server_health(sample(mem_pct=95.0), cfg, now=1.0))
    assert health.headline(state, cfg) == ("crit", "Critical: Memory 95 %")
    state.components.clear()
    state.set_component(health.rollup("solr", [rung("solr.error", "unknown", message="timed out")]))
    assert health.headline(state, cfg) == ("unknown", "1 check could not run")
    assert health.subline("server-1", 8.0) == "server-1 · checked 8 s ago"
    assert health.subline("server-1", None) == "server-1 · first check pending"
    assert health.worst_component(state.components.values()).id == "solr"
    assert health.worst_component([]) is None


def test_dependency_graph_and_closure() -> None:
    assert health.DEPENDENCY_GRAPH["backend"] == ("postgres", "solr")
    assert health.DEPENDENCY_GRAPH["frontend"] == ("backend",)
    assert health.DEPENDENCY_GRAPH["nginx"] == ("frontend",)
    assert health.DEPENDENCY_GRAPH["search"] == ("backend", "solr")
    assert set(health.dependents_of("solr")) == {"backend", "search"}
    assert health.affected_closure("solr", enabled={"solr", "backend"}) == ["DSpace Search", "DSpace Backend"]
    full = health.affected_closure("postgres")
    assert full[:2] == ["DSpace Backend", "DSpace Search"] and "nginx" in full and "Repository site" in full
    assert health.affected_closure("ssl") == []
    assert health.HEADLINE_LATENCY["backend"] == "backend.rest"
    assert health.component_label("assetstore") == "Assetstore"


def test_security_component_meta_and_order() -> None:
    """SECURITY-CONTRACT: 🛡️ Security, no dependencies, no headline check, right after Ports."""
    meta = health.COMPONENTS["security"]
    assert (meta.label, meta.emoji, meta.depends_on, meta.headline_check) == ("Security", "🛡️", (), None)
    order = health.COMPONENT_ORDER
    assert order.index("security") == order.index("ports") + 1
    assert "security" not in health.HEADLINE_LATENCY and health.dependents_of("security") == []
    # informational rungs never make it Unknown: one counted ok rung is enough
    info = CheckResult("security.ssh", "SSH", "unknown", 1000.0, detail={"informational": True})
    comp = health.rollup("security", [info, rung("security.sentinel", "ok")])
    assert comp.status == "ok" and comp.label == "Security"


def test_state_sparks_and_reset() -> None:
    state = State()
    for i in range(20):
        state.set_sample(sample(ts=float(i), cpu_pct=float(i)))
    sparks = state.spark_dict()
    assert len(sparks["cpu"]) == 12 and sparks["cpu"][-1] == 19.0
    assert set(sparks) == {"cpu", "mem", "disk", "load"}
    state.seed_sparks({"cpu": list(range(30))})
    assert state.spark_dict()["cpu"] == [float(v) for v in range(18, 30)]
    state.last_cycle_ts = time.time() - 3
    assert 2.5 <= state.checked_ago_s() <= 4
    state.reset()
    assert state.sample is None and state.spark_dict()["cpu"] == [] and state.checked_ago_s() is None


def test_rollup_ignores_informational_unknown_rungs() -> None:
    info = CheckResult(
        id="backend.actuator",
        label="Actuator health",
        status="unknown",
        ts=1000.0,
        message="not exposed (HTTP 404)",
        detail={"informational": True},
    )
    chain = [rung("backend.rest", "ok", 91.0), info, rung("backend.search", "ok", 140.0)]
    comp = health.rollup("backend", chain, {"dspace_version": "8.1"}, now=1000.0)
    assert comp.status == "ok" and comp.reason is None and comp.latency_ms == 91.0
    assert [r.id for r in comp.chain] == ["backend.rest", "backend.actuator", "backend.search"]
    assert health.is_informational(info) and not health.is_informational(chain[0])
    # a real unknown still counts
    hard = rung("backend.service", "unknown", message="systemctl did not answer")
    assert health.rollup("backend", [hard, *chain]).status == "unknown"
    # a warn/crit rung wins regardless of informational neighbours
    broken = [info, rung("backend.search", "crit", message="HTTP 500")]
    assert health.rollup("backend", broken).status == "crit"
    # a chain made only of informational rungs stays unknown
    assert health.rollup("logs", [info]).status == "unknown"
    # the dependency rung of a not-yet-collected dependency is informational too
    dep = health.dependency_rung("frontend.dep.backend", "Backend dependency", None, ts=1.0)
    assert dep.status == "unknown" and dep.message == "not monitored" and health.is_informational(dep)
    frontend = health.rollup("frontend", [rung("frontend.home", "ok", 640.0), dep])
    assert frontend.status == "ok"


def _alert(severity: str, title: str, status: str = "open", id: int = 1) -> object:
    from dspace_sentinel.models import Alert

    return Alert(id, "log_errors:dspace", "logs", severity, status, title, "", 120.0, 100.0, 5.0)  # type: ignore[arg-type]


def test_mood_and_headline_consider_open_alerts() -> None:
    cfg = Config()
    state = State()
    state.set_component(health.server_health(sample(), cfg, now=1.0))
    state.set_component(health.rollup("logs", [rung("logs.dspace", "ok")]))
    assert health.headline(state, cfg) == ("ok", "All systems healthy")
    # an open critical alert whose component already reads ok again (clear_samples hysteresis)
    crit = _alert("critical", "120 errors in DSpace log (5 min)")
    assert health.alert_mood([crit]) == "crit"
    assert health.mood(state, cfg, [crit]) == "crit"
    assert health.headline(state, cfg, [crit]) == ("crit", "Critical: 120 errors in DSpace log (5 min)")
    two = [crit, _alert("critical", "Solr unreachable", id=2)]
    assert health.headline(state, cfg, two)[1] == "Critical: 120 errors in DSpace log (5 min) (+1 more)"
    # warnings only raise the mood to warn and are counted
    warn = _alert("warning", "Disk 87 % on /", id=3)
    assert health.headline(state, cfg, [warn]) == ("warn", "1 open alert")
    both = [warn, _alert("warning", "Memory 81 %", id=4)]
    assert health.headline(state, cfg, both) == ("warn", "2 open alerts")
    # resolved alerts and /api dicts are handled
    assert health.mood(state, cfg, [_alert("critical", "old", status="resolved")]) == "ok"
    assert health.headline(state, cfg, [{"severity": "critical", "title": "CPU 96 %", "status": "open"}]) == (
        "crit",
        "Critical: CPU 96 %",
    )
    # component-based wording wins when the components themselves are degraded
    down = [rung("postgres.tcp", "crit", message="connection refused")]
    state.set_component(health.rollup("postgres", down))
    assert health.headline(state, cfg, [crit]) == ("crit", "Critical: PostgreSQL unreachable")
    state.set_component(health.rollup("postgres", [rung("postgres.query", "warn", 2000, "slow")]))
    assert health.headline(state, cfg, [warn]) == ("warn", "1 warning")
    assert health.headline(state, cfg, [crit])[0] == "crit"
    # an open alert beats "unknown" from a component that could not be checked
    state.components.clear()
    state.set_component(health.rollup("solr", [rung("solr.error", "unknown", message="timed out")]))
    assert health.headline(state, cfg, [warn]) == ("warn", "1 open alert")
    assert health.headline(state, cfg) == ("unknown", "1 check could not run")
    assert health.component_mood(state, cfg) == "unknown"


def _open(id: int, component: str, severity: str, acked_ts: float | None = None) -> Any:
    from dspace_sentinel.models import Alert

    return Alert(
        id,
        f"component_down:{component}",
        component,
        severity,
        "open",
        f"{component} {severity}",
        "",
        None,
        None,
        5.0,
        acked_ts=acked_ts,
    )


def test_acknowledged_alerts_calm_the_mood_but_not_the_components() -> None:
    """F7 #6 (T13): Ack calms Home; the component keeps its real status everywhere else."""
    cfg = Config()
    state = State()
    state.set_component(health.server_health(sample(), cfg, now=1.0))
    state.set_component(health.rollup("ports", [rung("ports.8983", "warn", message="exposed publicly (::)")]))
    acked = _open(1, "ports", "warning", acked_ts=10.0)
    assert health.headline(state, cfg, [acked]) == ("ok", "All systems healthy · 1 acknowledged")
    assert health.mood(state, cfg, [acked]) == "ok" and state.components["ports"].status == "warn"
    assert health.headline(state, cfg, [acked.to_dict()]) == ("ok", "All systems healthy · 1 acknowledged")
    two = [acked, _open(2, "server", "warning", acked_ts=11.0)]
    assert health.headline(state, cfg, two) == ("ok", "All systems healthy · 2 acknowledged")
    assert health.acknowledged_components(two) == {"ports", "server"}
    # a second, unacknowledged alert of the same component keeps it coloured
    assert health.headline(state, cfg, [acked, _open(3, "ports", "warning")]) == ("warn", "1 warning")
    # a new alert elsewhere colours Home; the acknowledged one is not counted
    assert health.headline(state, cfg, [acked, _open(4, "logs", "warning")]) == ("warn", "1 open alert")
    # escalated to critical (the acknowledgement is cleared): Home is coloured again
    assert health.headline(state, cfg, [_open(1, "ports", "critical")])[0] == "crit"
    # an acknowledged critical alert calms it too
    assert health.headline(state, cfg, [_open(5, "ports", "critical", acked_ts=12.0)])[0] == "ok"
    # a component that warns without an alert still colours Home
    state.set_component(health.rollup("solr", [rung("solr.search", "warn", 2000, "search took 2.0 s")]))
    assert health.headline(state, cfg, [acked]) == ("warn", "1 warning")
    assert health.component_mood(state, cfg) == "warn"  # alerts aside, nothing changes
