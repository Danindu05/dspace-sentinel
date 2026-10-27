"""Scheduler: hot reload, module switches during a check, check-now coalescing, hooks off the lock."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from typing import Any, ClassVar

import pytest

from dspace_sentinel import collectors as collectors_mod
from dspace_sentinel import scheduler as scheduler_mod
from dspace_sentinel.alerts import notifiers
from dspace_sentinel.alerts.engine import AlertEngine
from dspace_sentinel.alerts.notifiers import NotifyResult
from dspace_sentinel.alerts.notifiers import email as mail
from dspace_sentinel.bus import Bus
from dspace_sentinel.collectors.base import Collector, CollectorContext, CollectorResult
from dspace_sentinel.config import Config, merge_patch
from dspace_sentinel.models import CheckResult
from dspace_sentinel.reports import schedule as report_schedule
from dspace_sentinel.scheduler import Scheduler
from dspace_sentinel.state import State
from dspace_sentinel.store import Store
from tests.test_rules import make_sample


class FakeSystem(Collector):
    id: ClassVar[str] = "server"
    interval_key: ClassVar[str] = "system_interval"
    label: ClassVar[str] = "Server"
    emoji: ClassVar[str] = "💻"
    calls: ClassVar[int] = 0
    delay: ClassVar[float] = 0.0

    async def collect(self, ctx: CollectorContext) -> CollectorResult:
        type(self).calls += 1
        await asyncio.sleep(type(self).delay)
        return CollectorResult(sample=make_sample(ts=ctx.now))


class FakeRepository(Collector):
    id: ClassVar[str] = "repository"
    interval_key: ClassVar[str] = "repository_interval"
    label: ClassVar[str] = "Repository"
    emoji: ClassVar[str] = "📚"
    calls: ClassVar[int] = 0

    async def collect(self, ctx: CollectorContext) -> CollectorResult:
        type(self).calls += 1
        return CollectorResult(metrics={"items": 12}, repository_stats={"items": 12})


class FailingPostgres(Collector):
    """PostgreSQL refusing connections, answering after ``delay`` seconds."""

    id: ClassVar[str] = "postgres"
    interval_key: ClassVar[str] = "db_interval"
    label: ClassVar[str] = "PostgreSQL"
    emoji: ClassVar[str] = "🐘"
    delay: ClassVar[float] = 0.0

    async def collect(self, ctx: CollectorContext) -> CollectorResult:
        await asyncio.sleep(type(self).delay)
        ts = ctx.now
        return CollectorResult(
            checks=[
                CheckResult("postgres.tcp", "TCP :5432", "ok", ts, latency_ms=0.4),
                CheckResult("postgres.connect", "Connect", "crit", ts, message="connection refused"),
                CheckResult.not_reached("postgres.query", "SELECT 1", ts),
            ]
        )


async def no_dispatch(cfg: Config, event: dict[str, Any], now: float | None = None) -> list[NotifyResult]:
    return [NotifyResult("telegram", True)]


@pytest.fixture
def store() -> Iterator[Store]:
    s = Store(":memory:")
    yield s
    s.close()


@pytest.fixture
def fake_system(monkeypatch: pytest.MonkeyPatch) -> type[FakeSystem]:
    monkeypatch.setitem(collectors_mod._overrides, "server", FakeSystem)
    monkeypatch.setattr(FakeSystem, "calls", 0)
    monkeypatch.setattr(FakeSystem, "delay", 0.0)
    return FakeSystem


async def test_reload_keeps_the_running_group_bookkeeping(
    store: Store, fake_system: type[FakeSystem], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PATCH that leaves a group alone must not freeze its countdown or its run count."""
    monkeypatch.setattr(Scheduler, "_group_interval", lambda self, cfg, key: 1)
    sched = Scheduler(Config(), store, State(), Bus())
    await sched.start(initial_cycle=False)
    try:
        await asyncio.sleep(0.2)
        runs = sched.groups["system_interval"].run_count
        assert runs == 1
        sched.reload(merge_patch(sched.cfg, {"appearance": {"theme": "dark"}}))
        await asyncio.sleep(1.4)
        grp = sched.groups["system_interval"]
        assert grp.run_count > runs  # the bookkeeping lives on the object the loop updates
        assert grp.collectors[0] in sched.collectors  # one set of collector instances
        assert sched.describe()["groups"]["system_interval"]["runs"] == grp.run_count
        assert 0.0 < sched.next_check_in() <= 1.0
    finally:
        await sched.stop()


async def test_module_switched_off_during_a_check_now_stays_off(
    store: Store, fake_system: type[FakeSystem], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A result that arrives after its module was switched off must not bring the component back."""
    monkeypatch.setitem(collectors_mod._overrides, "postgres", FailingPostgres)
    monkeypatch.setattr(FailingPostgres, "delay", 0.0)
    cfg = merge_patch(Config(), {"modules": {"postgres": True}})
    bus = Bus()
    engine = AlertEngine(store, cfg, bus, dispatcher=no_dispatch)
    state = State()
    sched = Scheduler(cfg, store, state, bus, alert_engine=engine)
    try:
        await sched.run_all_now()
        await sched.run_all_now()
        assert [a.rule for a in store.open_alerts()] == ["component_down:postgres"]
        monkeypatch.setattr(FailingPostgres, "delay", 0.5)
        check = asyncio.create_task(sched.run_all_now())
        await asyncio.sleep(0.1)  # the check is waiting on PostgreSQL's timeout
        sched.reload(merge_patch(cfg, {"modules": {"postgres": False}}))
        await check
        assert set(state.components) == {"server"}
        assert set(store.component_states()) == {"server"}
        await sched.run_all_now()
        assert set(state.components) == {"server"}
        assert store.open_alerts() == []  # resolved as "module switched off"
    finally:
        await sched.stop()


async def test_check_now_is_coalesced_while_the_service_runs(
    store: Store, fake_system: type[FakeSystem], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A loop of POST /api/actions/check-now must not turn into continuous load on the stack."""
    monkeypatch.setattr(scheduler_mod, "CHECK_NOW_MIN_INTERVAL_S", 0.5)
    sched = Scheduler(Config(), store, State(), Bus())
    await sched.start()  # the start-up cycle; the loops then wait a whole interval
    try:
        assert fake_system.calls == 1
        assert await sched.run_all_now() is True  # the first check-now always runs
        assert fake_system.calls == 2
        assert await sched.run_all_now() is False  # right after it: the current state is returned
        assert fake_system.calls == 2
        await asyncio.sleep(0.55)
        monkeypatch.setattr(fake_system, "delay", 0.2)
        ran = await asyncio.gather(sched.run_all_now(), sched.run_all_now(), sched.run_all_now())
        assert sorted(ran) == [False, False, True]  # the late callers joined the running check
        assert fake_system.calls == 3
        sched.reload(merge_patch(sched.cfg, {"appearance": {"theme": "dark"}}))
        assert await sched.run_all_now() is True  # a config change always allows a fresh check
        assert fake_system.calls == 4
    finally:
        await sched.stop()


async def test_check_now_leaves_the_repository_counts_to_their_interval(
    store: Store, fake_system: type[FakeSystem], monkeypatch: pytest.MonkeyPatch
) -> None:
    """count(*) over item/bitstream runs every repository_interval, not on every check-now."""
    monkeypatch.setitem(collectors_mod._overrides, "repository", FakeRepository)
    monkeypatch.setattr(FakeRepository, "calls", 0)
    cfg = merge_patch(Config(), {"modules": {"repository": True}})
    state = State()
    sched = Scheduler(cfg, store, state, Bus())
    try:
        assert await sched.run_all_now() is True  # one-shot callers (doctor) are never coalesced
        assert await sched.run_all_now() is True
        assert fake_system.calls == 2 and FakeRepository.calls == 1
        assert state.components["repository"].metrics == {"items": 12}  # the last counts stay
        sched.groups["repository_interval"].last_run -= cfg.collector.repository_interval
        await sched.run_all_now()
        assert FakeRepository.calls == 2
    finally:
        await sched.stop()


async def test_notifications_and_reports_run_outside_the_cycle_lock(
    store: Store, fake_system: type[FakeSystem], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Telegram API that never answers must not stop collection on every alert transition."""
    delivered: list[str] = []

    async def blackholed(cfg: Config, event: dict[str, Any], now: float | None = None) -> list[NotifyResult]:
        await asyncio.sleep(1.0)  # the request hangs until the HTTP timeout
        delivered.append(event["type"])
        return [NotifyResult("telegram", True)]

    report_runs: list[float] = []

    async def slow_weekly_report(store: Any, cfg: Any, now: float, notifier: Any = None) -> None:
        report_runs.append(now)
        await asyncio.sleep(1.0)  # a weekly report being generated

    monkeypatch.setattr(report_schedule, "maybe_run", slow_weekly_report)
    cfg = merge_patch(Config(), {"alerts": {"confirm_samples": 1, "cpu": {"warning": 0, "critical": 200}}})
    bus = Bus()
    engine = AlertEngine(store, cfg, bus, dispatcher=blackholed)
    sched = Scheduler(cfg, store, State(), bus, alert_engine=engine)
    try:
        started = asyncio.get_running_loop().time()
        await sched.run_group(sched.groups["system_interval"])
        assert asyncio.get_running_loop().time() - started < 0.5
        assert not sched._cycle_lock.locked()
        assert [a.rule for a in store.open_alerts()] == ["cpu"] and delivered == []
        await sched.run_group(sched.groups["system_interval"])  # the report hook is still busy
        assert len(report_runs) == 1
        await engine.flush()
        assert delivered == ["opened"] and store.open_alerts()[0].notified_ts is not None
    finally:
        await sched.stop()


async def test_the_shutdown_warning_counts_the_notifications_given_up(
    store: Store,
    fake_system: type[FakeSystem],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """channel-lanes#5: with a lane per channel every job starts at once and waits inside its lane,
    so the old count (jobs not started) read "(0 waiting)" while the emails behind a mail server
    that never answers were dropped. Here three jobs are under way (one sending, two waiting for
    their turn in the email lane) and two wait beyond the outbox's bound: five are given up."""

    async def silent_mail_server(cfg: Config, event: dict[str, Any], recipients: Any = None) -> Any:
        await asyncio.sleep(3600)

    monkeypatch.setattr(mail, "send_event", silent_mail_server)
    monkeypatch.setattr(scheduler_mod, "STOP_FLUSH_TIMEOUT_S", 0.3)
    email = {
        "enabled": True,
        "smtp_host": "127.0.0.1",
        "security": "none",
        "from_address": "sentinel@example.org",
        "recipients": [{"address": "ops@example.org"}],
    }
    cfg = merge_patch(Config(), {"notifiers": {"email": email}})
    bus = Bus()
    engine = AlertEngine(store, cfg, bus)
    engine.outbox = notifiers.Outbox(maxsize=3)  # (an empty Outbox is falsy: set it, do not pass it)
    sched = Scheduler(cfg, store, State(), bus, alert_engine=engine)
    for n in range(1, 6):
        event = {"kind": "alert", "type": "opened", "severity": "critical", "html": f"alert {n}"}
        engine.outbox.submit(notifiers.notify(store, cfg, {**event, "summary": f"alert {n}"}, ts=float(n)))
        await asyncio.sleep(0.02)  # the job starts and takes its turn in the email lane
    assert len(engine.outbox) == 2  # all the old warning counted
    with caplog.at_level(logging.WARNING, logger="dspace_sentinel.scheduler"):
        await sched.stop()
    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.name == "dspace_sentinel.scheduler" and r.levelno >= logging.WARNING
    ]
    assert warnings == ["5 notifications not delivered (Sentinel is stopping)"]
    assert len(engine.outbox) == 0
    for _ in range(5):
        await asyncio.sleep(0)  # the cancelled deliveries wind down


class BrokenPostgres(Collector):
    id: ClassVar[str] = "postgres"
    interval_key: ClassVar[str] = "db_interval"
    label: ClassVar[str] = "PostgreSQL"
    emoji: ClassVar[str] = "🐘"

    async def collect(self, ctx: CollectorContext) -> CollectorResult:
        raise RuntimeError("driver exploded")


async def test_a_failing_collector_leaves_a_flagged_unknown_rung(
    store: Store, fake_system: type[FakeSystem], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(collectors_mod._overrides, "postgres", BrokenPostgres)
    state = State()
    sched = Scheduler(merge_patch(Config(), {"modules": {"postgres": True}}), store, state, Bus())
    try:
        await sched.run_all_now()
    finally:
        await sched.stop()
    (rung,) = state.components["postgres"].chain
    assert (rung.id, rung.status) == ("postgres.error", "unknown")
    assert rung.message == "RuntimeError: driver exploded"
    assert rung.detail == {"collector_error": True}
    assert state.components["postgres"].status == "unknown"


class CountingPostgres(Collector):
    id: ClassVar[str] = "postgres"
    interval_key: ClassVar[str] = "db_interval"
    label: ClassVar[str] = "PostgreSQL"
    emoji: ClassVar[str] = "🐘"

    async def collect(self, ctx: CollectorContext) -> CollectorResult:
        ts = ctx.now
        return CollectorResult(
            checks=[CheckResult("postgres.query", "SELECT 1", "ok", ts, latency_ms=1.0)],
            metrics={"connections_pct": 42.0},
            history=[CheckResult("postgres.connections", "Connections", "ok", ts, value=42.0)],
        )


async def test_history_rows_are_stored_but_stay_out_of_the_ladder(
    store: Store, fake_system: type[FakeSystem], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(collectors_mod._overrides, "postgres", CountingPostgres)
    state = State()
    sched = Scheduler(merge_patch(Config(), {"modules": {"postgres": True}}), store, state, Bus())
    try:
        await sched.run_all_now()
    finally:
        await sched.stop()
    rows = store.checks_between(0, 2**31, "postgres.connections")
    assert [(r["value"], r["status"]) for r in rows] == [(42.0, "ok")]
    assert [r.id for r in state.components["postgres"].chain] == ["postgres.query"]


class FlappingSolr(Collector):
    """ok, then slow, then refused, then ok again."""

    id: ClassVar[str] = "solr"
    interval_key: ClassVar[str] = "http_interval"
    label: ClassVar[str] = "Solr"
    emoji: ClassVar[str] = "☀️"
    script: ClassVar[list[tuple[str, str]]] = []

    async def collect(self, ctx: CollectorContext) -> CollectorResult:
        status, message = type(self).script.pop(0)
        rung = CheckResult("solr.search", "Search query", status, ctx.now, message=message)
        return CollectorResult(checks=[rung])


async def test_status_changes_are_worded_like_the_ui(
    store: Store, fake_system: type[FakeSystem], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(collectors_mod._overrides, "solr", FlappingSolr)
    script = [("crit", "connection refused"), ("warn", "search took 1.7 s"), ("ok", ""), ("ok", "")]
    monkeypatch.setattr(FlappingSolr, "script", script)
    sched = Scheduler(merge_patch(Config(), {"modules": {"solr": True}}), store, State(), Bus())
    try:
        for _ in range(4):
            await sched.run_all_now()
    finally:
        await sched.stop()
    texts = [e.message for e in reversed(store.events(kind="component", component="solr"))]
    assert texts == [
        "Solr: Critical — connection refused",
        "Solr: Critical → Warning — search took 1.7 s",
        "Solr: Warning → Healthy",
    ]


async def test_reload_leaves_the_config_event_to_its_caller(
    store: Store, fake_system: type[FakeSystem], monkeypatch: pytest.MonkeyPatch
) -> None:
    """PATCH /api/modules logs "Solr turned on"; the scheduler must not add a second wording."""
    monkeypatch.setitem(collectors_mod._overrides, "postgres", CountingPostgres)
    sched = Scheduler(Config(), store, State(), Bus())
    try:
        sched.reload(merge_patch(Config(), {"modules": {"postgres": True}}))
        assert "postgres" in sched.enabled_component_ids()
        sched.reload(Config())
        assert "postgres" not in sched.enabled_component_ids()
    finally:
        await sched.stop()
    assert store.events(kind="config") == []
