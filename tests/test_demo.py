"""Demo mode details: now-relative log lines, status codes, fast mode timing, the Telegram guard."""

from __future__ import annotations

import datetime as _dt
import time
from pathlib import Path

import httpx
import pytest

from dspace_sentinel import dspace_version
from dspace_sentinel.alerts import rules
from dspace_sentinel.alerts.incidents import affected_for, hypothesis_for
from dspace_sentinel.alerts.messages import failing_rung, sequence_text
from dspace_sentinel.alerts.notifiers import telegram
from dspace_sentinel.bus import Bus
from dspace_sentinel.collectors import backend, demo
from dspace_sentinel.collectors.base import CollectorContext
from dspace_sentinel.config import Config, TelegramChat, apply_demo_overrides
from dspace_sentinel.health import rollup
from dspace_sentinel.scheduler import Scheduler
from dspace_sentinel.state import State
from dspace_sentinel.store import Store
from dspace_sentinel.util import logs


def test_demo_log_lines_are_stamped_relative_to_now() -> None:
    now = time.time()
    world = demo.DemoWorld(start_ts=now, cfg=apply_demo_overrides(Config()), fast=False)
    # solr_down phase: every log carries errors and a "last error" line
    ts = world.next_incident_start(now) + 330.0
    _checks, metrics = world.component("logs", ts)
    year = _dt.datetime.fromtimestamp(ts).strftime("%Y")
    for log_id, info in metrics["files"].items():
        last = info["last_error"]
        assert last is not None, log_id
        assert ts - 60 <= last["ts"] <= ts
        assert year in last["line"] and "2024-01-01" not in last["line"]
    when = _dt.datetime.fromtimestamp(ts)
    assert demo.demo_log_line("dspace", ts).startswith(f"{when:%Y-%m-%d %H:%M:%S},")
    assert demo.demo_log_line("nginx", ts).startswith(f"{when:%Y/%m/%d %H:%M:%S} [error]")
    # tomcat10 on Ubuntu 24.04 writes catalina.out through rsyslog
    tomcat = demo.demo_log_line("tomcat", ts)
    assert tomcat.startswith(f"[{when:%Y-%m-%d %H:%M:%S}] [crit]")
    assert logs.parse_timestamp(tomcat) == float(int(ts)) and logs.count_errors([tomcat], ts - 60).errors == 1
    solr_line = demo.demo_log_line("solr", ts)
    assert " ERROR " in solr_line and "OutOfMemoryError" in solr_line
    assert demo.demo_log_line("other", ts).endswith("ERROR demo error")


def test_demo_http_rungs_carry_status_codes() -> None:
    cfg = apply_demo_overrides(Config())
    cfg.dspace.public_url = "https://repo.example.org"
    cfg.nginx.https_url = "https://repo.example.org"
    world = demo.DemoWorld(start_ts=1_800_000_000.0, cfg=cfg, fast=False)
    calm = world.start_ts + 60.0
    codes = {}
    for cid in ("backend", "frontend", "nginx", "solr"):
        for rung in world.component(cid, calm)[0]:
            if "status_code" in rung.detail:
                codes[rung.id] = rung.detail["status_code"]
    assert codes == {
        "backend.rest": 200,
        "backend.actuator": 200,
        "backend.search": 200,
        "frontend.home": 200,
        "frontend.public": 200,
        "nginx.http": 200,
        "nginx.https": 200,
        "solr.admin": 200,
        "solr.search": 200,
    }
    down = world.next_incident_start(calm) + 330.0
    chain = {r.id: r for r in world.component("backend", down)[0]}
    search = chain["backend.search"]
    assert search.status == "crit" and search.detail["status_code"] == 500
    actuator = chain["backend.actuator"]
    assert actuator.status == "warn" and actuator.detail["status_code"] == 200  # DSpace serves DOWN with 200


def test_fast_mode_phases_and_intervals() -> None:
    assert demo.FAST_INCIDENT_OFFSET_S + demo.FAST_INCIDENT_LENGTH_S <= demo.FAST_CYCLE_S
    # every phase lasts long enough for confirm_samples=2 at FAST_INTERVAL_S (with margin)
    scale = demo.FAST_INCIDENT_LENGTH_S / demo.INCIDENT_LENGTH_S
    for name, lo, hi in demo.PHASES:
        assert (hi - lo) * scale >= 3 * demo.FAST_INTERVAL_S, name
    world = demo.DemoWorld(start_ts=1_800_000_000.0, cfg=apply_demo_overrides(Config()), fast=True)
    assert world.cycle_s == demo.FAST_CYCLE_S
    inc = world.next_incident_start(world.start_ts)
    assert inc == world.start_ts + demo.FAST_INCIDENT_OFFSET_S
    phases: list[str] = []
    t = inc
    while t < inc + demo.FAST_INCIDENT_LENGTH_S:
        sc = world.scenario(t)
        if not phases or phases[-1] != sc.phase:
            phases.append(sc.phase)
        t += demo.FAST_INTERVAL_S
    assert phases == ["memory", "solr_slow", "solr_down", "recovery"]
    assert world.scenario(inc + demo.FAST_INCIDENT_LENGTH_S + 1).phase == "calm"


async def test_scheduler_uses_fast_intervals_only_in_fast_demo() -> None:
    cfg = apply_demo_overrides(Config())
    for fast in (False, True):
        suite = demo.DemoSuite(cfg, world=demo.DemoWorld(cfg=cfg, fast=fast))
        store = Store(":memory:")
        sched = Scheduler(cfg, store, State(), Bus(), demo_suite=suite)
        intervals = {key: grp.interval for key, grp in sched.groups.items()}
        if fast:
            assert intervals["system_interval"] == demo.FAST_INTERVAL_S
            assert intervals["http_interval"] == demo.FAST_INTERVAL_S
            assert intervals["logs_interval"] == demo.FAST_INTERVAL_S
            assert intervals["repository_interval"] == cfg.collector.repository_interval
        else:
            assert intervals["system_interval"] == cfg.collector.system_interval
            assert intervals["http_interval"] == cfg.collector.http_interval
        assert cfg.collector.system_interval == 15  # the config itself is never touched
        store.close()


async def test_telegram_is_never_called_in_demo_mode() -> None:
    cfg = apply_demo_overrides(Config())
    cfg.notifiers.telegram.enabled = True
    cfg.notifiers.telegram.chats = [
        # a made-up token, split in two so that secret scanners do not report it
        TelegramChat(bot_token="123456789:" + "AAH-demo-test-token-00000000000001", chat_id="42")
    ]

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not be reached
        raise AssertionError(f"demo mode sent {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    expected = {"ok": False, "result": None, "error": telegram.DEMO_ERROR, "skipped": "demo"}
    assert await telegram.call(cfg, "getMe", client=client, require_chat=False) == expected
    assert (await telegram.send_message(cfg, "<b>hi</b>", client=client))["error"] == telegram.DEMO_ERROR
    assert (await telegram.send_test(cfg, client=client))["error"] == telegram.DEMO_ERROR
    assert (await telegram.get_me(cfg, client=client))["ok"] is False
    await client.aclose()


@pytest.mark.parametrize("value,expected", [("1", True), ("yes", True), ("0", False), ("", False)])
def test_demo_fast_env(value: str, expected: bool) -> None:
    assert demo.demo_fast_enabled({"SENTINEL_DEMO_FAST": value}) is expected


async def test_demo_keeps_postgres_connection_history() -> None:
    """Live cycles and the backfill both record ``postgres.connections`` for the report's p95."""
    cfg = apply_demo_overrides(Config())
    now = time.time()
    suite = demo.DemoSuite(cfg, world=demo.DemoWorld(start_ts=now, cfg=cfg, fast=False))
    store = Store(":memory:")
    try:
        suite.backfill(store, days=1, now=now)
        rows = store.checks_between(now - 86400, now, "postgres.connections")
        assert len(rows) > 1000 and all(r["value"] is not None for r in rows)
        pg = next(c for c in suite.collectors() if c.id == "postgres")
        ctx = CollectorContext(cfg=cfg, http=None, store=store, state=State(), now=now)  # type: ignore[arg-type]
        result = await pg.collect(ctx)
        (row,) = result.history
        assert row.id == "postgres.connections" and row.value == result.metrics["connections_pct"]
    finally:
        store.close()


def test_a_demo_restarted_days_later_keeps_its_counters_growing() -> None:
    """Growth counters stay anchored to the first start, so repository deltas never go negative."""
    cfg = apply_demo_overrides(Config())
    t0 = 1_800_000_000.0
    first = demo.DemoSuite(cfg, world=demo.DemoWorld(start_ts=t0, cfg=cfg, fast=False))
    store = Store(":memory:")
    try:
        first.backfill(store, days=1, now=t0)
        last_live = first.world.repository_at(t0 + 86400)  # that first run went on for a day
        later = demo.DemoSuite(cfg, world=demo.DemoWorld(start_ts=t0 + 3 * 86400, cfg=cfg, fast=False))
        assert later.backfill(store, days=1) == {}  # history is there already
        again = later.world.repository_at(t0 + 3 * 86400)
        for key in ("items", "bitstreams", "db_size", "assetstore_bytes", "eperson_count"):
            assert again[key] > last_live[key], key
    finally:
        store.close()


def test_backfilled_log_alerts_use_the_live_rule_ids() -> None:
    cfg = apply_demo_overrides(Config())
    now = 1_800_000_000.0
    suite = demo.DemoSuite(cfg, world=demo.DemoWorld(start_ts=now, cfg=cfg, fast=False))
    store = Store(":memory:")
    try:
        suite.backfill(store, days=7, now=now)
        rules = {a.rule for a in store.alerts("all", limit=100)}
        assert "log_errors:dspace" in rules and "log_errors" not in rules
    finally:
        store.close()


def test_backfilled_incidents_read_like_the_live_engine_wrote_them() -> None:
    """Messages, sequence lines, hypothesis, affected services and times follow the live rules."""
    cfg = apply_demo_overrides(Config())
    now = 1_800_000_000.0
    suite = demo.DemoSuite(cfg, world=demo.DemoWorld(start_ts=now, cfg=cfg, fast=False))
    store = Store(":memory:")
    try:
        suite.backfill(store, days=7, now=now)
        incidents = store.incidents("all", limit=10)
        assert len(incidents) == len(demo.BACKFILL_INCIDENTS_DAYS_AGO)
        for inc in incidents:
            alerts = store.alerts_for_incident(inc.id)
            by_id = {a.id: a for a in alerts}
            assert inc.hypothesis == hypothesis_for(alerts) == "Possible Solr resource exhaustion"
            assert inc.affected == affected_for(alerts)
            assert inc.started_ts == min(a.opened_ts for a in alerts)
            assert inc.ended_ts == max(a.resolved_ts for a in alerts)
            for entry in inc.sequence:  # the memory alert escalates: its warning line is older
                alert = by_id[entry["alert_id"]]
                if entry["severity"] == alert.severity:
                    assert entry["text"] == sequence_text(alert)
            assert "Solr response 4.8 s" in [e["text"] for e in inc.sequence]
            down = {a.component: a.message for a in alerts if a.rule.startswith("component_down:")}
            assert down["solr"].startswith("service ✅ → TCP ❌\n")
            assert failing_rung(down["solr"]) == "tcp" and failing_rung(down["backend"]) == "search"
    finally:
        store.close()


def test_demo_is_dspace_9_4_seen_anonymously() -> None:
    """The demo backend answers like a real DSpace 9.4 does to an anonymous client."""
    cfg = apply_demo_overrides(Config())
    world = demo.DemoWorld(start_ts=1_800_000_000.0, cfg=cfg, fast=False)
    assert demo.DEMO_DSPACE_VERSION == "9.4"
    calm = world.start_ts + 60.0
    checks, metrics = world.component("backend", calm)
    rungs = {r.id: r for r in checks}
    assert rungs["backend.rest"].message == "DSpace 9.4"
    actuator = rungs["backend.actuator"]
    # no GeoLite database: UP_WITH_ISSUES, and no components without an admin login
    assert (actuator.status, actuator.message) == ("ok", "up, with non-blocking issues")
    assert actuator.detail["status_code"] == 200 and actuator.detail["health_status"] == "UP_WITH_ISSUES"
    assert actuator.detail["components"] == {}
    assert metrics["actuator_components"] == {} and metrics["actuator_status"] == "UP_WITH_ISSUES"
    assert (
        metrics["dspace_version"] == "9.4" and metrics["dspace_major"] == 9 and metrics["dspace_minor"] == 4
    )
    assert metrics["dspace_snapshot"] is False and metrics["cris_version"] is None
    # Solr down: the anonymous answer is {"status": "DOWN"} with HTTP 200, graded as the collector does
    down = world.next_incident_start(calm) + 330.0
    checks, metrics = world.component("backend", down)
    actuator = {r.id: r for r in checks}["backend.actuator"]
    expected = backend.grade_health({"status": "DOWN"}, dspace_version.parse("DSpace 9.4"))
    assert expected is not None
    assert (actuator.status, actuator.message) == (expected.status, expected.message)
    assert actuator.message == "status DOWN (details need a DSpace admin login)"
    assert metrics["actuator_status"] == "DOWN" and metrics["actuator_components"] == {}


def test_demo_solr_has_the_dspace_9_cores() -> None:
    cfg = apply_demo_overrides(Config())
    world = demo.DemoWorld(start_ts=1_800_000_000.0, cfg=cfg, fast=False)
    calm = world.start_ts + 60.0
    checks, metrics = world.component("solr", calm)
    rungs = {r.id: r for r in checks}
    cores = [r.id.removeprefix("solr.core.") for r in checks if r.id.startswith("solr.core.")]
    assert cores == ["search", "statistics", "oai", "authority", "qaevent", "suggestion"]
    assert rungs["solr.admin"].message == "6 cores"
    assert rungs["solr.core.qaevent"].detail["optional"] is True and rungs["solr.core.qaevent"].status == "ok"
    assert [c["name"] for c in metrics["cores"]] == cores and metrics["cores"][4]["optional"] is True
    assert metrics["version"] == demo.DEMO_SOLR_VERSION and metrics["init_failures"] == {}
    assert metrics["reload_failures"] == {}  # the same keys as the real collector
    # while Solr is down nothing lists the optional cores: only the configured ones are not reached
    down = world.next_incident_start(calm) + 330.0
    checks, _ = world.component("solr", down)
    assert [r.id for r in checks if r.id.startswith("solr.core.")] == [
        "solr.core.search",
        "solr.core.statistics",
        "solr.core.oai",
        "solr.core.authority",
    ]


def test_demo_frontend_is_server_rendered() -> None:
    cfg = apply_demo_overrides(Config())
    world = demo.DemoWorld(start_ts=1_800_000_000.0, cfg=cfg, fast=False)
    checks, metrics = world.component("frontend", world.start_ts + 60.0)
    home = {r.id: r for r in checks}["frontend.home"]
    assert home.detail["rendering"] == "ssr" and "host_header" not in home.detail
    assert (metrics["rendering"], metrics["generator"], metrics["angular"]) == (
        "ssr",
        "DSpace 9.4",
        "20.3.30",
    )
    assert metrics["host_header"] is None
    cfg.dspace.public_url = "https://repo.example.org"
    checks, metrics = world.component("frontend", world.start_ts + 60.0)
    assert {r.id: r for r in checks}["frontend.home"].detail["host_header"] == "repo.example.org"
    assert metrics["host_header"] == "repo.example.org"


def test_demo_incident_reads_the_same_live_and_back_filled() -> None:
    """The live chain at "Solr down" is what the back-filled component_down:backend alert says."""
    cfg = apply_demo_overrides(Config())
    now = 1_800_000_000.0
    world = demo.DemoWorld(start_ts=now, cfg=cfg, fast=False)
    down = world.next_incident_start(now) + 330.0
    checks, metrics = world.component("backend", down)
    live = rules.component_down_rule(rollup("backend", checks, metrics, now=down), cfg)
    suite = demo.DemoSuite(cfg, world=world)
    store = Store(":memory:")
    try:
        suite.backfill(store, days=7, now=now)
        stored = {a.message for a in store.alerts("all", limit=100) if a.rule == "component_down:backend"}
    finally:
        store.close()
    assert stored == {live.message}
    assert live.message.startswith("service ✅ → TCP ✅ → rest ✅ → actuator ⚠️ → search ❌\n")
    # calm again: the backend is healthy, the actuator's UP_WITH_ISSUES never raises an alert
    for ts in (now + 30, down + 400):
        checks, metrics = world.component("backend", ts)
        assert rules.component_down_rule(rollup("backend", checks, metrics, now=ts), cfg).ok is True


# ------------------------------------------------------------------ the demo stack's Tomcat


def test_demo_backend_runs_the_demo_stacks_tomcat() -> None:
    """DSpace 9.4 on Ubuntu 24.04 runs packaged Tomcat 10: the untouched defaults (tomcat9 and its
    catalina.out) read as the demo stack's, with or without onboarding's discovery applied."""
    from dspace_sentinel import discovery

    assert demo.DEMO_BACKEND_UNIT in discovery.DEMO_UNITS and demo.DEMO_BACKEND_UNIT == "tomcat10"
    assert demo.DEMO_TOMCAT_LOG == "/var/log/tomcat10/catalina.out"
    world = demo.DemoWorld(start_ts=1_800_000_000.0, cfg=apply_demo_overrides(Config()), fast=False)
    calm = world.start_ts + 60.0
    service = {r.id: r for r in world.component("backend", calm)[0]}["backend.service"]
    assert service.label == "systemd tomcat10" and service.detail["unit"] == "tomcat10"
    checks, metrics = world.component("logs", calm)
    assert {r.id: r for r in checks}["logs.tomcat"].detail["path"] == demo.DEMO_TOMCAT_LOG
    assert metrics["files"]["tomcat"]["path"] == demo.DEMO_TOMCAT_LOG
    assert metrics["files"]["dspace"]["path"] == "/dspace/log/dspace.log"  # the others are kept


def test_demo_shows_a_changed_backend_unit_and_tomcat_log_as_they_are() -> None:
    from dspace_sentinel.config import merge_patch

    files = [f.model_dump(mode="json") for f in Config().logs.files]
    for entry in files:
        if entry["id"] == "tomcat":
            entry["path"] = "/opt/tomcat/logs/catalina.out"
    cfg = merge_patch(
        apply_demo_overrides(Config()),
        {"dspace": {"backend_service": "dspace-backend"}, "logs": {"files": files}},
    )
    world = demo.DemoWorld(start_ts=1_800_000_000.0, cfg=cfg, fast=False)
    calm = world.start_ts + 60.0
    service = {r.id: r for r in world.component("backend", calm)[0]}["backend.service"]
    assert service.label == "systemd dspace-backend" and service.detail["unit"] == "dspace-backend"
    assert world.component("logs", calm)[1]["files"]["tomcat"]["path"] == "/opt/tomcat/logs/catalina.out"


def test_doctor_demo_shows_the_demo_stacks_tomcat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``doctor --demo`` never runs onboarding: it must not show DSpace 9.4 under tomcat9."""
    import json

    from dspace_sentinel import cli

    monkeypatch.setenv("SENTINEL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SENTINEL_CONFIG", str(tmp_path / "config" / "sentinel.yaml"))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cli.main(["doctor", "--demo", "--json"]) in (0, 1, 2)
    payload = json.loads(capsys.readouterr().out)
    chains = {c["id"]: {r["id"]: r for r in c["chain"]} for c in payload["components"]}
    assert chains["backend"]["backend.service"]["label"] == "systemd tomcat10"
    assert chains["backend"]["backend.service"]["detail"]["unit"] == "tomcat10"
    assert chains["logs"]["logs.tomcat"]["detail"]["path"] == "/var/log/tomcat10/catalina.out"


def test_demo_security_component_models_the_demo_server() -> None:
    """SECURITY-CONTRACT demo: DSpace 9.4 / Ubuntu 24.04 / tomcat10, calm (no security update), with hints."""
    cfg = apply_demo_overrides(Config())
    world = demo.DemoWorld(start_ts=1_800_000_000.0, cfg=cfg, fast=False)
    calm = world.start_ts + 60.0
    checks, metrics = world.component("security", calm)
    chain = {r.id: r for r in checks}
    assert list(chain) == [
        "security.ports",
        "security.firewall",
        "security.ssh",
        "security.web",
        "security.updates",
        "security.software",
        "security.files",
        "security.sentinel",
    ]
    assert {rid: r.status for rid, r in chain.items() if r.status != "ok"} == {}  # the demo starts calm
    assert chain["security.ports"].message == (
        "7 listening · 5 reachable from the network (22, 80, 443, 4000, 8080) · none unexpected"
    )
    verdicts = {p["port"]: (p["verdict"], p["expected"], p["public"]) for p in metrics["ports"]}
    assert verdicts[22] == ("expected", "open", True) and verdicts[5432] == ("local", "local_only", False)
    assert chain["security.firewall"].message == "ufw active"
    assert chain["security.ssh"].message == "root login: prohibit-password · passwords: yes · port 22"
    assert chain["security.ssh"].detail["hint"].startswith("Keys instead of passwords")
    assert chain["security.web"].message == "HTTPS on repo.demo-dspace.org"
    assert metrics["web"]["headers_missing"] == ["Strict-Transport-Security"]
    web_hint = chain["security.web"].detail["hint"]
    assert web_hint.startswith("Missing response headers: Strict-Transport-Security.")
    assert chain["security.updates"].message == "no pending security updates (3 other updates)"
    assert metrics["updates"]["pending"] == 3 and metrics["updates"]["security"] == 0
    assert chain["security.software"].message == (
        "DSpace 9.4 · Solr 9.6.1 · PostgreSQL 15.8 · Ubuntu 24.04: all supported"
    )
    assert chain["security.files"].message == "local.cfg and sentinel.yaml are not readable by other users"
    local_cfg = next(f for f in metrics["files"] if f["path"].endswith("/local.cfg"))
    assert (local_cfg["mode"], local_cfg["owner"], local_cfg["group"]) == ("640", "tomcat", "tomcat")
    assert chain["security.sentinel"].message == "listens on 127.0.0.1 only"
    assert not any(r.detail.get("alert") for r in checks)
    comp = rollup("security", checks, metrics, now=calm)
    assert comp.status == "ok" and comp.reason is None
    # Solr down: its local port is gone, nothing else changes
    down = world.next_incident_start(calm) + 330.0
    ports = [p["port"] for p in world.component("security", down)[1]["ports"]]
    assert 8983 not in ports and 5432 in ports


async def test_demo_runs_security_in_its_own_group() -> None:
    cfg = apply_demo_overrides(Config())
    suite = demo.DemoSuite(cfg, world=demo.DemoWorld(cfg=cfg, fast=False))
    store = Store(":memory:")
    try:
        sched = Scheduler(cfg, store, State(), Bus(), demo_suite=suite)
        assert sched.groups["security_interval"].ids == ("security",)
        assert sched.groups["security_interval"].interval == cfg.collector.security_interval
        ids = [c.id for c in suite.collectors()]
        assert ids.index("security") == ids.index("ports") + 1
        now = 1_800_000_000.0
        assert suite.backfill(store, days=1, now=now)["component_history"] > 0
        strip = store.availability("security", now - 86400, now, 24)
        assert strip["samples"] > 0 and strip["uptime_pct"] == 100.0
    finally:
        store.close()
