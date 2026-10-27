"""Alert rules: thresholds, hysteresis, per-item keys, component_down classification, demo story."""

from __future__ import annotations

from typing import Any

from dspace_sentinel.alerts import rules
from dspace_sentinel.alerts.rules import Observation, evaluate, grade
from dspace_sentinel.collectors.demo import INCIDENT_OFFSET_S, DemoWorld
from dspace_sentinel.config import Config, apply_demo_overrides
from dspace_sentinel.health import rollup, server_health
from dspace_sentinel.models import CheckResult, ComponentHealth, SystemSample

GIB = 1024**3
CFG = Config()


def make_sample(**overrides: Any) -> SystemSample:
    data: dict[str, Any] = {
        "ts": 1000.0,
        "cpu_pct": 12.0,
        "cpu_per_core": [10.0, 14.0],
        "mem_total": 16 * GIB,
        "mem_used": int(12.9 * GIB),
        "mem_available": int(3.1 * GIB),
        "mem_pct": 60.0,
        "mem_cached": GIB,
        "swap_total": 4 * GIB,
        "swap_used": int(1.6 * GIB),
        "swap_pct": 40.0,
        "disk_total": 200 * GIB,
        "disk_used": int(146.3 * GIB),
        "disk_free": int(53.7 * GIB),
        "disk_pct": 50.0,
        "disk_path": "/",
        "inode_pct": 5.0,
        "load1": 0.8,
        "load5": 0.7,
        "load15": 0.6,
        "cores": 4,
        "net_rx_bps": 1.0,
        "net_tx_bps": 1.0,
        "uptime_s": 100.0,
        "os_name": "Ubuntu",
        "os_version": "24.04",
        "kernel": "6.8",
        "hostname": "server-1",
        "disks": [],
    }
    data.update(overrides)
    return SystemSample(**data)


def comp(
    cid: str, chain: list[CheckResult], metrics: dict | None = None, ts: float = 1000.0
) -> ComponentHealth:
    return rollup(cid, chain, metrics, now=ts)


def ok(rung_id: str, label: str = "", latency: float | None = None, **detail: Any) -> CheckResult:
    return CheckResult(rung_id, label or rung_id, "ok", 1000.0, latency_ms=latency, detail=detail)


def by_key(observations: list[Observation]) -> dict[str, Observation]:
    return {o.key: o for o in observations}


# --------------------------------------------------------------------- grading


def test_grade_thresholds_and_hysteresis_band() -> None:
    th = CFG.alerts.cpu  # 80 / 95, percent rule → 2 point margin
    assert grade(96.0, "cpu", th) == ("critical", False, 95.0)
    assert grade(80.0, "cpu", th) == ("warning", False, 80.0)
    assert grade(79.0, "cpu", th) == (None, False, None)  # inside the band: neither
    assert grade(77.9, "cpu", th) == (None, True, None)
    assert grade(None, "cpu", th) == (None, False, None)


def test_grade_inverted_and_non_percent_margin() -> None:
    ssl = CFG.alerts.ssl_expiry_days  # 30 / 7 inverted → margin 0.46 days
    assert grade(5.0, "ssl_expiry", ssl)[0] == "critical"
    assert grade(12.0, "ssl_expiry", ssl) == ("warning", False, 30.0)
    assert grade(30.2, "ssl_expiry", ssl) == (None, False, None)
    assert grade(31.0, "ssl_expiry", ssl) == (None, True, None)
    rest = CFG.alerts.rest_latency_ms  # 1000 / 3000 → margin 40 ms
    assert rules.hysteresis_margin("rest_latency", rest) == 40.0
    assert grade(970.0, "rest_latency", rest) == (None, False, None)
    assert grade(959.0, "rest_latency", rest) == (None, True, None)


# ---------------------------------------------------------------- server rules


def test_server_rules_titles_messages_and_keys() -> None:
    sample = make_sample(cpu_pct=96.0, load1=7.9, mem_pct=93.0, disk_pct=87.0, swap_pct=55.0)
    obs = by_key(evaluate(sample, {}, CFG))
    cpu = obs["cpu@server"]
    assert (cpu.severity, cpu.title, cpu.message) == ("critical", "CPU 96 %", "load 7.9 on 4 cores")
    mem = obs["memory@server"]
    assert (mem.severity, mem.title, mem.message) == (
        "critical",
        "Memory 93 %",
        "12.9 of 16 GB used, swap 55 %",
    )
    assert obs["swap@server"].severity == "warning" and obs["swap@server"].title == "Swap 55 %"
    disk = obs["disk@server"]
    assert (disk.severity, disk.title, disk.message) == ("warning", "Disk 87 % on /", "free 53.7 GB")
    load = obs["load@server"]
    assert load.severity == "warning" and load.title == "Load 2.0 per core" and load.value == 1.98
    assert all(o.observed_ts == sample.ts and o.component == "server" for o in obs.values())


def test_swap_rule_absent_without_swap_and_extra_mounts_get_their_own_key() -> None:
    sample = make_sample(
        swap_total=0,
        disks=[{"path": "/dspace", "total": 1024 * GIB, "used": 891 * GIB, "free": 53.7 * GIB, "pct": 87.0}],
    )
    asset = comp(
        "assetstore",
        [ok("assetstore.path", "Assetstore readable")],
        {
            "mount": "/dspace",
            "growth_bytes_per_day": 2.1 * GIB,
            "free": 53.7 * GIB,
            "path": "/dspace/assetstore",
        },
    )
    obs = by_key(evaluate(sample, {"assetstore": asset}, CFG))
    assert "swap@server" not in obs
    mount = obs["disk:/dspace@server"]
    assert mount.rule == "disk:/dspace" and mount.base_rule == "disk" and mount.severity == "warning"
    assert (mount.title, mount.message) == ("Disk 87 % on /dspace", "free 53.7 GB · growing 2.1 GB/day")
    assert obs["disk@server"].message == "free 53.7 GB"  # growth only on the assetstore mount


# ------------------------------------------------------------- latency rules


def _backend(rest_latency: float | None, rest_status: str = "ok") -> ComponentHealth:
    chain = [
        ok("backend.service"),
        ok("backend.tcp", "TCP :8080", 0.6),
        CheckResult("backend.rest", "GET /server/api", rest_status, 1000.0, latency_ms=rest_latency),
        CheckResult("backend.search", "Search API", "ok", 1000.0, latency_ms=300.0),
    ]
    return comp("backend", chain, {"rest_latency_ms": rest_latency})


def test_latency_rule_from_graded_rung() -> None:
    obs = by_key(evaluate(None, {"backend": _backend(2400.0, "warn")}, CFG))
    rest = obs["rest_latency@backend"]
    assert rest.severity == "warning" and rest.value == 2400.0 and rest.threshold == 1000.0
    assert (rest.title, rest.message) == ("REST API slow: 2.4 s", "GET /server/api")
    assert obs["search_latency@backend"].ok is True
    assert obs["component_down:backend@backend"].ok is True  # a graded slow rung is not "down"


def test_latency_rule_neutral_on_hard_failure_and_not_reached() -> None:
    failed = _backend(2100.0, "crit")  # crit with a latency the threshold would only call warn
    obs = by_key(evaluate(None, {"backend": failed}, CFG))
    rest = obs["rest_latency@backend"]
    assert rest.severity is None and rest.ok is False and rest.value is None
    down = obs["component_down:backend@backend"]
    assert down.severity == "critical" and down.title == "DSpace Backend unreachable"
    unreached = comp(
        "backend", [ok("backend.service"), CheckResult.not_reached("backend.rest", "GET", 1000.0)]
    )
    obs = by_key(evaluate(None, {"backend": unreached}, CFG))
    assert obs["rest_latency@backend"].severity is None and obs["rest_latency@backend"].ok is False


def test_latency_rule_falls_back_to_metrics_and_stays_neutral_without_source() -> None:
    metrics_only = comp("frontend", [ok("frontend.service")], {"home_latency_ms": 2300.0})
    obs = by_key(evaluate(None, {"frontend": metrics_only}, CFG))
    home = obs["home_latency@frontend"]
    assert home.severity == "warning" and home.title == "Homepage slow: 2.3 s"
    assert home.message == f"GET {CFG.dspace.ui_url}"
    nothing = comp("frontend", [ok("frontend.service")], {})
    silent = by_key(evaluate(None, {"frontend": nothing}, CFG))["home_latency@frontend"]
    assert silent.severity is None and silent.ok is False  # key stays alive, nothing opens or clears


def test_solr_latency_rule_is_separate_from_backend_search() -> None:
    solr = comp(
        "solr",
        [ok("solr.service"), CheckResult("solr.search", "Search query", "crit", 1000.0, latency_ms=4800.0)],
        {"search_latency_ms": 4800.0},
    )
    obs = by_key(evaluate(None, {"solr": solr}, CFG))
    assert obs["solr_latency@solr"].severity == "critical"
    assert obs["solr_latency@solr"].title == "Solr search slow: 4.8 s"
    assert obs["component_down:solr@solr"].ok is True


# -------------------------------------------------------------- other rules


def test_postgres_connections_rule() -> None:
    pg = comp(
        "postgres", [ok("postgres.query", "SELECT 1", 1.2)], {"connections": 86, "max_connections": 100}
    )
    obs = by_key(evaluate(None, {"postgres": pg}, CFG))
    conn = obs["postgres_connections@postgres"]
    assert conn.severity == "warning" and conn.value == 86.0
    assert (conn.title, conn.message) == ("PostgreSQL connections 86 / 100", "86 % of max_connections in use")
    pct_only = comp("postgres", [ok("postgres.query")], {"connections_pct": 97})
    assert by_key(evaluate(None, {"postgres": pct_only}, CFG))["postgres_connections@postgres"].severity == (
        "critical"
    )
    neutral = by_key(evaluate(None, {"postgres": comp("postgres", [ok("postgres.query")], {})}, CFG))
    assert (
        neutral["postgres_connections@postgres"].severity is None
        and not neutral["postgres_connections@postgres"].ok
    )


def test_ssl_expiry_rule_is_inverted() -> None:
    def ssl(days: float) -> ComponentHealth:
        status = "crit" if days <= 7 else "warn" if days <= 30 else "ok"
        rung = CheckResult("ssl.cert", "Certificate", status, 1000.0, value=days)
        return comp(
            "ssl", [rung], {"days_left": days, "subject": "repo.example.org", "not_after": "2030-01-01"}
        )

    obs = by_key(evaluate(None, {"ssl": ssl(12.0)}, CFG))
    cert = obs["ssl_expiry@ssl"]
    assert cert.severity == "warning" and cert.title == "Certificate expires in 12 days"
    assert cert.message == "repo.example.org · not after 2030-01-01"
    assert obs["component_down:ssl@ssl"].ok is True  # graded by ssl_expiry, not a hard failure
    assert by_key(evaluate(None, {"ssl": ssl(5.0)}, CFG))["ssl_expiry@ssl"].severity == "critical"
    assert (
        by_key(evaluate(None, {"ssl": ssl(1.0)}, CFG))["ssl_expiry@ssl"].title
        == "Certificate expires in 1 day"
    )
    assert by_key(evaluate(None, {"ssl": ssl(0.0)}, CFG))["ssl_expiry@ssl"].title == "Certificate expired"
    assert by_key(evaluate(None, {"ssl": ssl(61.0)}, CFG))["ssl_expiry@ssl"].ok is True


def test_log_errors_rule_per_file_with_last_error_line() -> None:
    chain = [
        CheckResult("logs.dspace", "DSpace log", "warn", 1000.0, value=48.0, detail={"errors": 48}),
        CheckResult("logs.tomcat", "Tomcat log", "ok", 1000.0, value=1.0),
        CheckResult("logs.nginx", "nginx log", "unknown", 1000.0, message="file missing"),
    ]
    metrics = {
        "window_minutes": 5,
        "files": {
            "dspace": {
                "label": "DSpace",
                "errors": 48,
                "last_error": {"line": "ERROR Solr unavailable", "ts": 990},
            },
            "tomcat": {"label": "Tomcat", "errors": 1, "last_error": None},
            "nginx": {"label": "nginx", "errors": 0},
        },
    }
    obs = by_key(evaluate(None, {"logs": comp("logs", chain, metrics)}, CFG))
    dspace = obs["log_errors:dspace@logs"]
    assert dspace.rule == "log_errors:dspace" and dspace.base_rule == "log_errors"
    assert dspace.severity == "warning" and dspace.value == 48.0
    assert (dspace.title, dspace.message) == ("48 errors in DSpace log (5 min)", "ERROR Solr unavailable")
    assert obs["log_errors:tomcat@logs"].ok is True
    nginx = obs["log_errors:nginx@logs"]
    assert nginx.severity is None and nginx.ok is False  # missing file: neutral, never warn
    assert obs["component_down:logs@logs"].ok is True


def test_assetstore_free_rule() -> None:
    rung = CheckResult("assetstore.path", "Assetstore readable", "crit", 1000.0, value=9.1)
    metrics = {"free": 9.1 * GIB, "path": "/dspace/assetstore", "pct": 99.1, "mount": "/dspace"}
    obs = by_key(evaluate(None, {"assetstore": comp("assetstore", [rung], metrics)}, CFG))
    free = obs["assetstore_free@assetstore"]
    assert free.severity == "critical" and free.value == 9.1 and free.threshold == 10.0
    assert free.title == "Assetstore free space 9.1 GB" and free.message == "/dspace/assetstore · 99 % used"
    assert obs["component_down:assetstore@assetstore"].ok is True


# ---------------------------------------------------------- component_down


def test_component_down_unreachable_with_chain_and_reason() -> None:
    chain = [
        ok("postgres.service", "systemd postgresql"),
        ok("postgres.tcp", "TCP :5432", 0.4),
        CheckResult(
            "postgres.connect",
            "Connect as dspace",
            "crit",
            1000.0,
            message="connection refused (localhost:5432)",
        ),
        CheckResult.not_reached("postgres.query", "SELECT 1", 1000.0),
    ]
    obs = by_key(evaluate(None, {"postgres": comp("postgres", chain)}, CFG))
    down = obs["component_down:postgres@postgres"]
    assert down.severity == "critical" and down.ok is False and down.value is None
    assert down.title == "PostgreSQL unreachable"
    assert down.message == "service ✅ → TCP ✅ → connect ❌\nconnection refused (localhost:5432)"


def test_component_down_failing_degraded_and_dependency_rungs_ignored() -> None:
    chain = [
        ok("backend.service"),
        ok("backend.tcp", "TCP :8080", 0.6),
        ok("backend.rest", "GET /server/api", 95.0),
        CheckResult(
            "backend.actuator", "Actuator health", "warn", 1000.0, message="status DOWN (solrSearchCore)"
        ),
        CheckResult(
            "backend.search",
            "Search API",
            "crit",
            1000.0,
            latency_ms=2100.0,
            message="HTTP 500: Solr unreachable",
        ),
        CheckResult("backend.dep.solr", "Solr dependency", "warn", 1000.0, message="Solr is down (Critical)"),
    ]
    obs = by_key(evaluate(None, {"backend": comp("backend", chain)}, CFG))
    down = obs["component_down:backend@backend"]
    assert down.severity == "critical" and down.title == "DSpace Backend failing"
    assert (
        down.message == "service ✅ → TCP ✅ → rest ✅ → actuator ⚠️ → search ❌\nHTTP 500: Solr unreachable"
    )
    assert obs["search_latency@backend"].severity is None  # hard failure: not a latency reading

    only_dep = comp("frontend", [ok("frontend.home", "Homepage", 500.0), chain[-1]])
    obs = by_key(evaluate(None, {"frontend": only_dep}, CFG))
    assert obs["component_down:frontend@frontend"].ok is True

    ports = comp(
        "ports",
        [ok("ports.22"), CheckResult("ports.8983", "Solr :8983", "warn", 1000.0, message="exposed publicly")],
    )
    obs = by_key(evaluate(None, {"ports": ports}, CFG))
    degraded = obs["component_down:ports@ports"]
    assert degraded.severity == "warning" and degraded.title == "Ports degraded"
    assert degraded.message == "22 ✅ → 8983 ⚠️\nexposed publicly"


def test_unknown_components_and_server_entry_produce_nothing_alarming() -> None:
    errored = comp("solr", [CheckResult("solr.error", "Solr check", "unknown", 1000.0, message="timed out")])
    server = server_health(make_sample(), CFG, None, 1000.0)
    obs = by_key(evaluate(None, {"server": server, "solr": errored}, CFG))
    assert set(obs) == {"solr_latency@solr", "component_down:solr@solr"}
    # a check that could not run says nothing either way: neutral, never "healthy"
    down = obs["component_down:solr@solr"]
    assert down.severity is None and down.ok is False and not obs["solr_latency@solr"].breaching
    assert obs["solr_latency@solr"].to_dict()["key"] == "solr_latency@solr"


def collector_error(cid: str, ts: float = 1000.0) -> CheckResult:
    """The rung the scheduler writes when ``collect`` timed out or raised."""
    return CheckResult(
        f"{cid}.error",
        f"{cid} check",
        "unknown",
        ts,
        message="check timed out after 29 s",
        detail={"collector_error": True},
    )


def test_component_down_is_neutral_when_nothing_was_measured() -> None:
    """A time-out must not resolve an open "unreachable" alert; one measured rung is enough for OK."""
    obs = by_key(evaluate(None, {"solr": comp("solr", [collector_error("solr")])}, CFG))
    down = obs["component_down:solr@solr"]
    assert down.severity is None and down.ok is False
    blind = [
        CheckResult("postgres.service", "systemd", "unknown", 1000.0, message="systemctl did not answer"),
        CheckResult.not_reached("postgres.tcp", "TCP :5432", 1000.0),
    ]
    obs = by_key(evaluate(None, {"postgres": comp("postgres", blind)}, CFG))
    assert obs["component_down:postgres@postgres"].ok is False
    measured = [blind[0], ok("postgres.tcp", "TCP :5432", 0.4)]
    obs = by_key(evaluate(None, {"postgres": comp("postgres", measured)}, CFG))
    assert obs["component_down:postgres@postgres"].ok is True


def test_log_error_rules_ignore_the_collector_error_rung() -> None:
    errored = comp("logs", [collector_error("logs")], {"files": {}, "window_minutes": 5})
    assert rules.log_error_rules(errored, CFG) == []
    # a configured log file that happens to be called "error" is still a log file
    real = CheckResult("logs.error", "Error log", "ok", 1000.0, value=3.0, detail={"errors": 3})
    obs = rules.log_error_rules(comp("logs", [real], {"files": {}, "window_minutes": 5}), CFG)
    assert [(o.rule, o.value, o.ok) for o in obs] == [("log_errors:error", 3.0, True)]


# ---------------------------------------------------------------- demo story


def _demo_observations(world: DemoWorld, cfg: Config, ts: float) -> dict[str, Observation]:
    sample = world.sample_at(ts)
    comps = {"server": server_health(sample, cfg, None, ts)}
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
        checks, metrics = world.component(cid, ts)
        comps[cid] = rollup(cid, checks, metrics, now=ts)
    return by_key(evaluate(sample, comps, cfg))


def test_demo_world_tells_the_scripted_story() -> None:
    cfg = apply_demo_overrides(Config())
    world = DemoWorld(start_ts=1_800_000_000.0, cfg=cfg)
    calm = _demo_observations(world, cfg, world.start_ts + 30)
    assert not any(o.breaching for o in calm.values())
    memory = _demo_observations(world, cfg, world.start_ts + INCIDENT_OFFSET_S + 150)
    assert memory["memory@server"].severity == "warning"
    slow = _demo_observations(world, cfg, world.start_ts + INCIDENT_OFFSET_S + 290)
    assert slow["memory@server"].severity == "critical"
    assert slow["solr_latency@solr"].severity == "critical"
    assert slow["search_latency@backend"].breaching
    assert slow["component_down:solr@solr"].ok is True
    down = _demo_observations(world, cfg, world.start_ts + INCIDENT_OFFSET_S + 330)
    assert down["component_down:solr@solr"].title == "Solr unreachable"
    assert down["component_down:solr@solr"].message.startswith("service ✅ → TCP ❌\n")
    assert down["component_down:backend@backend"].title == "DSpace Backend failing"
    assert down["component_down:backend@backend"].message.endswith(
        "search ❌\nHTTP 500 (discover/search/objects): Solr unreachable"
    )
    assert down["log_errors:dspace@logs"].severity == "critical"
    assert down["solr_latency@solr"].severity is None and down["search_latency@backend"].severity is None
    assert "component_down:ports@ports" in down and down["component_down:ports@ports"].ok is True
    recovered = _demo_observations(world, cfg, world.start_ts + INCIDENT_OFFSET_S + 595)
    assert all(o.ok for o in recovered.values())
    # the demo's Security component is calm and never alerts
    assert recovered["security_ports@security"].ok and "component_down:security@security" not in recovered


# ------------------------------------------------------------------ security rules


def sec(rung_id: str, status: str, message: str = "", **detail: Any) -> CheckResult:
    return CheckResult(rung_id, rung_id, status, 1000.0, message=message, detail=detail)  # type: ignore[arg-type]


def test_security_rules_alert_only_on_the_three_marked_warnings() -> None:
    assert {"security_ports", "security_firewall", "security_ssh"} <= set(rules.ALL_RULES)
    port_message = "9 listening · 6 reachable from the network · 1 unexpected: 3306 (mysqld)"
    chain = [
        sec("security.ports", "warn", port_message, alert=True, hint="…"),
        sec("security.firewall", "warn", "no host firewall is active and 1 port is reachable", alert=True),
        sec("security.ssh", "warn", "root may log in with a password", alert=True),
        sec(
            "security.web", "warn", "the public address is plain HTTP: logins and sessions travel unencrypted"
        ),
        sec("security.updates", "warn", "5 security updates pending"),
        sec("security.software", "warn", "Ubuntu 20.04 reached end of standard support on 2025-05-31"),
        sec("security.files", "warn", "local.cfg is readable by every user on this server"),
        sec("security.sentinel", "warn", "listens on 0.0.0.0 without a password (SENTINEL_ALLOW_OPEN)"),
    ]
    obs = by_key(evaluate(None, {"security": comp("security", chain)}, CFG))
    assert set(obs) == {"security_ports@security", "security_firewall@security", "security_ssh@security"}
    ports = obs["security_ports@security"]
    assert (ports.severity, ports.title, ports.message) == (
        "warning",
        "New port open to the network",
        port_message,
    )
    assert obs["security_firewall@security"].title == "No host firewall while ports are exposed"
    assert obs["security_ssh@security"].title == "Root may log in over SSH with a password"
    assert all(o.severity == "warning" and o.base_rule.startswith("security_") for o in obs.values())


def test_security_rules_clear_on_a_checked_rung_and_wait_on_an_unchecked_one() -> None:
    chain = [
        sec("security.ports", "warn", "1 unexpected: 9100"),  # exposed since the start: no alert
        sec("security.firewall", "unknown", "no host firewall active (…)", informational=True),
        sec("security.ssh", "unknown", "not available · …", informational=True, unavailable=True),
    ]
    obs = by_key(rules.component_rules(comp("security", chain), CFG))
    assert obs["security_ports@security"].ok is True and obs["security_ports@security"].severity is None
    assert obs["security_firewall@security"].ok is True
    unchecked = obs["security_ssh@security"]
    assert unchecked.ok is False and unchecked.severity is None  # neutral: neither opens nor clears
    errored = by_key(rules.component_rules(comp("security", [collector_error("security")]), CFG))
    assert all(not o.ok and o.severity is None for o in errored.values()) and len(errored) == 3
