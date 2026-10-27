"""AlertEngine: confirm/clear counts, escalation, hysteresis, ack, reminders, quiet hours, digest."""

from __future__ import annotations

import datetime as _dt
from typing import Any

import pytest

from dspace_sentinel.alerts import notifiers, rules
from dspace_sentinel.alerts.engine import DIGEST_KV_KEY, AlertEngine
from dspace_sentinel.alerts.notifiers import NotifyResult
from dspace_sentinel.alerts.notifiers import telegram as telegram_mod
from dspace_sentinel.bus import Bus
from dspace_sentinel.config import Config, merge_patch
from dspace_sentinel.health import rollup
from dspace_sentinel.models import CheckResult, ComponentHealth
from dspace_sentinel.store import Store
from tests.test_rules import make_sample

T0 = 1_700_000_000.0
# a made-up token, split in two so that secret scanners do not report it
BOT = "123456789:" + "AAH-engine-test-token-0000000000001"


class Recorder:
    """A dispatcher stand-in that records every notification event."""

    def __init__(self, result: NotifyResult | None = None) -> None:
        self.events: list[dict[str, Any]] = []
        self.result = result or NotifyResult("telegram", True)

    async def __call__(
        self, cfg: Config, event: dict[str, Any], now: float | None = None
    ) -> list[NotifyResult]:
        self.events.append(event)
        return [self.result]

    def types(self) -> list[str]:
        return [e["type"] for e in self.events]


@pytest.fixture
def cfg() -> Config:
    return Config()


@pytest.fixture
def bus() -> Bus:
    return Bus()


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def engine(mem_store: Store, cfg: Config, bus: Bus, recorder: Recorder) -> AlertEngine:
    return AlertEngine(mem_store, cfg, bus, dispatcher=recorder)


async def feed(engine: AlertEngine, values: list[float], start: float = T0, step: float = 15.0) -> list[dict]:
    """Evaluate one CPU sample per value (each with its own timestamp); returns every transition.

    Notifications go out in the background; between two samples they have long been delivered.
    """
    out: list[dict] = []
    for i, cpu in enumerate(values):
        now = start + i * step
        out.extend(await engine.evaluate(make_sample(ts=now, cpu_pct=cpu), {}, now))
        await engine.flush()
    return out


def postgres_down(ts: float, down: bool = True) -> ComponentHealth:
    chain = [
        CheckResult("postgres.service", "systemd postgresql", "ok", ts),
        CheckResult("postgres.tcp", "TCP :5432", "ok", ts, latency_ms=0.4),
    ]
    if down:
        chain.append(
            CheckResult("postgres.connect", "Connect as dspace", "crit", ts, message="connection refused")
        )
        chain.append(CheckResult.not_reached("postgres.query", "SELECT 1", ts))
    else:
        chain.append(CheckResult("postgres.connect", "Connect as dspace", "ok", ts, latency_ms=5.0))
        chain.append(CheckResult("postgres.query", "SELECT 1", "ok", ts, latency_ms=1.1))
    return rollup("postgres", chain, {"connections": 10, "max_connections": 100}, now=ts)


# ------------------------------------------------------------- open / escalate


async def test_opens_after_confirm_samples_and_notifies(
    engine: AlertEngine, bus: Bus, recorder: Recorder
) -> None:
    sub = bus.subscribe()
    transitions = await feed(engine, [85.0])
    assert transitions == [] and engine.open_alerts() == []
    transitions = await feed(engine, [86.0], start=T0 + 15)
    assert [t["type"] for t in transitions] == ["opened"]
    alert = engine.open_alerts()[0]
    assert (alert.rule, alert.component, alert.severity) == ("cpu", "server", "warning")
    assert alert.title == "CPU 86 %" and alert.threshold == 80.0 and alert.opened_ts == int(T0 + 15)
    assert alert.notified_ts == int(T0 + 15)
    evt = sub.queue.get_nowait()
    assert evt.event == "alert" and evt.data["type"] == "opened" and evt.data["alert"]["id"] == alert.id
    assert evt.data["ts"] == T0 + 15
    events = engine.store.events(kind="alert")
    assert events[0].message == "WARNING CPU 86 %" and events[0].data["type"] == "opened"
    assert recorder.types() == ["opened"]
    assert recorder.events[0]["html"].startswith("🟡 <b>WARNING</b> ·")
    assert engine.store.events(kind="notify")[0].message.startswith("telegram sent: WARNING CPU 86 %")


async def test_same_measurement_is_counted_once(engine: AlertEngine) -> None:
    sample = make_sample(ts=T0, cpu_pct=99.0)
    for i in range(5):  # several groups finishing before the next system sample
        await engine.evaluate(sample, {}, T0 + i)
    assert engine.open_alerts() == []
    await engine.evaluate(make_sample(ts=T0 + 15, cpu_pct=99.0), {}, T0 + 15)
    assert len(engine.open_alerts()) == 1


async def test_opens_as_critical_and_escalates_immediately(engine: AlertEngine, recorder: Recorder) -> None:
    await feed(engine, [96.0, 97.0])
    assert engine.open_alerts()[0].severity == "critical"
    engine.reset()
    engine.store.clear_history()
    recorder.events.clear()
    transitions = await feed(engine, [85.0, 85.0, 96.0], start=T0 + 1000)
    assert [t["type"] for t in transitions] == ["opened", "escalated"]
    alert = engine.open_alerts()[0]
    assert alert.severity == "critical" and alert.title == "CPU 96 %" and alert.threshold == 95.0
    assert recorder.types() == ["opened", "escalated"]
    assert "Escalated from warning" in recorder.events[1]["html"]
    assert engine.store.events(kind="alert")[0].message == "Escalated to CRITICAL: CPU 96 %"


async def test_title_refreshes_when_it_gets_worse_only(engine: AlertEngine, recorder: Recorder) -> None:
    await feed(engine, [85.0, 88.0, 84.0, 90.0])
    alert = engine.open_alerts()[0]
    assert alert.title == "CPU 90 %" and alert.value == 90.0
    assert recorder.types() == ["opened"]  # refreshes are silent


# ---------------------------------------------------------- clear / hysteresis


async def test_resolves_after_clear_samples_with_hysteresis(
    engine: AlertEngine, recorder: Recorder, bus: Bus
) -> None:
    sub = bus.subscribe()
    await feed(engine, [90.0, 90.0])
    # 79 is inside the band (warning − 2): neither breach nor OK → resets the OK streak
    values = [70.0, 70.0, 79.0, 70.0, 70.0]
    transitions = await feed(engine, values, start=T0 + 30)
    assert transitions == [] and len(engine.open_alerts()) == 1
    transitions = await feed(engine, [70.0], start=T0 + 30 + 5 * 15)
    assert [t["type"] for t in transitions] == ["resolved"]
    assert engine.open_alerts() == []
    resolved = engine.store.alerts("resolved")[0]
    assert resolved.status == "resolved" and resolved.resolved_ts == int(T0 + 30 + 5 * 15)
    assert recorder.types() == ["opened", "resolved"]
    assert recorder.events[-1]["html"].startswith("✅ <b>Resolved</b> ·")
    kinds = [sub.queue.get_nowait().data["type"] for _ in range(sub.queue.qsize())]
    assert kinds == ["opened", "resolved"]
    assert engine.store.events(kind="alert")[0].message.startswith("Resolved: CPU 90 % · lasted ")


async def test_component_down_alert_lifecycle(engine: AlertEngine, recorder: Recorder) -> None:
    for i in range(2):
        ts = T0 + i * 30
        await engine.evaluate(None, {"postgres": postgres_down(ts)}, ts)
    await engine.flush()
    alert = engine.open_alerts()[0]
    assert alert.rule == "component_down:postgres" and alert.title == "PostgreSQL unreachable"
    assert alert.message == "service ✅ → TCP ✅ → connect ❌\nconnection refused"
    html = recorder.events[0]["html"]
    assert html.splitlines()[1:4] == [
        "<b>PostgreSQL unreachable</b>",
        "service ✅ → TCP ✅ → connect ❌",
        "connection refused",
    ]
    for i in range(3):
        ts = T0 + 60 + i * 30
        await engine.evaluate(None, {"postgres": postgres_down(ts, down=False)}, ts)
    await engine.flush()
    assert engine.open_alerts() == [] and recorder.types() == ["opened", "resolved"]


def security_ports(ts: float, message: str) -> ComponentHealth:
    """The Security component whose ports rung warns with ``detail.alert`` (a port opened after start)."""
    chain = [
        CheckResult("security.ports", "Open ports", "warn", ts, message=message, detail={"alert": True}),
        CheckResult("security.firewall", "Firewall", "ok", ts, message="ufw active"),
        CheckResult("security.ssh", "SSH", "ok", ts, message="root login: no · passwords: no · port 22"),
    ]
    return rollup("security", chain, {"new": 1}, now=ts)


async def test_an_open_security_alert_follows_its_rung_message(
    engine: AlertEngine, recorder: Recorder
) -> None:
    """A second new port changes the ports rung's message: the open alert says so too, silently."""
    engine.reload(merge_patch(Config(), {"modules": {"security": True}}))
    title = "New port open to the network"
    first = "9 listening · 6 reachable from the network · 1 unexpected: 3306 (mysqld)"
    second = "10 listening · 7 reachable from the network · 2 unexpected: 3306 (mysqld), 6379 (redis-server)"
    for i in range(2):
        ts = T0 + i * 900
        await engine.evaluate(None, {"security": security_ports(ts, first)}, ts)
    [alert] = engine.open_alerts()
    assert (alert.rule, alert.title, alert.message) == ("security_ports", title, first)
    ts = T0 + 2 * 900
    await engine.evaluate(None, {"security": security_ports(ts, second)}, ts)
    await engine.flush()
    [alert] = engine.open_alerts()
    assert (alert.rule, alert.title, alert.message) == ("security_ports", title, second)
    assert recorder.types() == ["opened"]  # refreshes are silent


# ----------------------------------------------------------- ack / reminders


async def test_reminders_until_acked(mem_store: Store, bus: Bus, recorder: Recorder) -> None:
    cfg = merge_patch(Config(), {"alerts": {"reminder_minutes": 1}})
    engine = AlertEngine(mem_store, cfg, bus, dispatcher=recorder)
    await feed(engine, [90.0, 90.0])
    alert = engine.open_alerts()[0]
    await feed(engine, [90.0], start=T0 + 45)
    assert recorder.types() == ["opened"]  # 30 s: too early
    await feed(engine, [90.0], start=T0 + 15 + 61)
    assert recorder.types() == ["opened", "reminder"]
    assert recorder.events[-1]["html"].startswith("🔔 <b>Still open</b> ·")
    assert mem_store.events(kind="alert")[0].data["type"] == "reminder"
    await feed(engine, [90.0], start=T0 + 15 + 61 + 30)
    assert recorder.types() == ["opened", "reminder"]  # next slot not reached yet
    acked = engine.ack(alert.id, T0 + 200)
    assert acked is not None and acked.acked_ts == int(T0 + 200)
    await feed(engine, [90.0], start=T0 + 15 + 61 + 120)
    assert recorder.types() == ["opened", "reminder"]  # silenced
    unacked = engine.unack(alert.id)
    assert unacked is not None and unacked.acked_ts is None
    await feed(engine, [90.0], start=T0 + 15 + 61 + 240)
    assert recorder.types() == ["opened", "reminder", "reminder"]
    assert engine.ack(9999) is None and engine.unack(9999) is None
    kinds = [e.data.get("type") for e in mem_store.events(kind="alert")]
    assert "acked" in kinds and "unacked" in kinds


async def test_ack_publishes_bus_event(engine: AlertEngine, bus: Bus) -> None:
    await feed(engine, [90.0, 90.0])
    alert = engine.open_alerts()[0]
    sub = bus.subscribe()
    engine.ack(alert.id)
    evt = sub.queue.get_nowait()
    assert evt.event == "alert" and evt.data["type"] == "acked" and evt.data["alert"]["acked_ts"]


# -------------------------------------------------------------- notifications


async def test_quiet_hours_suppress_warnings_but_not_critical(
    mem_store: Store, bus: Bus, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = _dt.datetime(2024, 3, 5, 2, 30).timestamp()  # 02:30 local: inside 23:00-07:00
    cfg = merge_patch(
        Config(),
        {
            "notifiers": {
                "telegram": {
                    "enabled": True,
                    "chats": [{"bot_token": BOT, "chat_id": "42"}],
                    "quiet_hours": "23:00-07:00",
                }
            }
        },
    )
    sent: list[str] = []

    async def fake_send(cfg: Config, html: str, **kwargs: Any) -> dict[str, Any]:
        sent.append(html)
        return {"ok": True, "message_id": 1, "error": None}

    monkeypatch.setattr(telegram_mod, "send_message", fake_send)
    engine = AlertEngine(mem_store, cfg, bus)  # real dispatch path
    await feed(engine, [85.0, 85.0], start=now)
    assert sent == []
    skipped = mem_store.events(kind="notify.skipped")
    assert skipped and "quiet hours" in skipped[0].message
    assert engine.open_alerts()[0].notified_ts is None
    await feed(engine, [97.0], start=now + 30)
    assert len(sent) == 1 and sent[0].startswith("🔴 <b>CRITICAL</b> · ")
    assert engine.open_alerts()[0].notified_ts == int(now + 30)
    assert notifiers.in_quiet_hours("23:00-07:00", now) and not notifiers.in_quiet_hours(
        "23:00-07:00", now + 6 * 3600
    )
    assert not notifiers.in_quiet_hours("", now) and not notifiers.in_quiet_hours("garbage", now)
    assert notifiers.in_quiet_hours("02:00-03:00", now) and not notifiers.in_quiet_hours("03:00-03:00", now)


async def test_demo_mode_logs_instead_of_sending(mem_store: Store, bus: Bus) -> None:
    cfg = Config()
    cfg.demo = True
    engine = AlertEngine(mem_store, cfg, bus)
    await feed(engine, [96.0, 96.0])
    demo = mem_store.events(kind="notify.demo")
    assert len(demo) == 1 and demo[0].data["channels"] == ["telegram"]
    assert demo[0].data["html"].startswith("🔴 <b>CRITICAL</b> · ") and demo[0].data["type"] == "opened"
    assert demo[0].message.startswith("Notification (demo, not sent): CRITICAL CPU 96 %")
    assert engine.open_alerts()[0].notified_ts == int(T0 + 15)
    assert mem_store.events(kind="notify") == []


async def test_notification_failure_is_logged_never_raised(mem_store: Store, bus: Bus) -> None:
    failing = Recorder(NotifyResult("telegram", False, error="HTTP 401 from Telegram"))
    engine = AlertEngine(mem_store, Config(), bus, dispatcher=failing)
    await feed(engine, [96.0, 96.0])
    errors = mem_store.events(kind="notify.error")
    assert errors and errors[0].message == "telegram failed: HTTP 401 from Telegram"
    assert engine.open_alerts()[0].notified_ts is None

    async def boom(cfg: Config, event: dict[str, Any], now: float | None = None) -> list[NotifyResult]:
        raise RuntimeError("boom")

    engine.dispatcher = boom
    transitions = await feed(engine, [10.0, 10.0, 10.0], start=T0 + 100)
    assert [t["type"] for t in transitions] == ["resolved"]


async def test_engine_survives_broken_rules(engine: AlertEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args: Any, **kwargs: Any) -> list:
        raise RuntimeError("rules broke")

    monkeypatch.setattr(rules, "evaluate", explode)
    assert await engine.evaluate(make_sample(), {}, T0) == []
    assert engine.last_evaluated_ts == T0 and engine.describe()["evaluations"] == 1


# ---------------------------------------------------------------- housekeeping


async def test_restart_restores_open_alerts(mem_store: Store, bus: Bus, recorder: Recorder) -> None:
    first = AlertEngine(mem_store, Config(), bus, dispatcher=recorder)
    await feed(first, [90.0, 90.0])
    alert_id = first.open_alerts()[0].id
    second = AlertEngine(mem_store, Config(), bus, dispatcher=recorder)
    assert second.states()["cpu@server"].alert is not None
    assert second.states()["cpu@server"].alert.id == alert_id
    await feed(second, [10.0, 10.0], start=T0 + 100)
    assert len(second.open_alerts()) == 1
    transitions = await feed(second, [10.0], start=T0 + 130)
    assert [t["type"] for t in transitions] == ["resolved"]


async def test_alert_of_a_switched_off_module_is_resolved_silently(
    engine: AlertEngine, recorder: Recorder
) -> None:
    cfg_on = merge_patch(Config(), {"modules": {"postgres": True}})
    engine.reload(cfg_on)
    for i in range(2):
        await engine.evaluate(None, {"postgres": postgres_down(T0 + i * 30)}, T0 + i * 30)
    assert len(engine.open_alerts()) == 1
    engine.reload(Config())  # postgres module off again → component vanishes from the state
    transitions = await engine.evaluate(make_sample(ts=T0 + 90), {}, T0 + 90)
    await engine.flush()
    assert [t["type"] for t in transitions] == ["resolved"]
    assert recorder.types() == ["opened"]  # no "resolved" message for an operator action
    assert engine.store.events(kind="alert")[0].message.endswith("· module switched off")
    assert "component_down:postgres@postgres" not in engine.states()


def errored(cid: str, ts: float) -> ComponentHealth:
    """What the scheduler stores when a collector timed out or raised."""
    rung = CheckResult(
        f"{cid}.error",
        f"{cid} check",
        "unknown",
        ts,
        message="check timed out after 29 s",
        detail={"collector_error": True},
    )
    return rollup(cid, [rung], None, now=ts)


async def test_a_check_that_could_not_run_keeps_the_alerts_open(
    engine: AlertEngine, recorder: Recorder
) -> None:
    """A time-out is not a recovery: no "Resolved" for an unreachable Solr that became slow."""
    engine.reload(merge_patch(Config(), {"modules": {"postgres": True, "logs": True}}))
    for i in range(2):
        ts = T0 + i * 30
        logs = rollup(
            "logs",
            [CheckResult("logs.dspace", "DSpace log", "crit", ts, value=150.0, detail={"errors": 150})],
            {"files": {"dspace": {"label": "DSpace"}}, "window_minutes": 5},
            now=ts,
        )
        await engine.evaluate(None, {"postgres": postgres_down(ts), "logs": logs}, ts)
    assert sorted(a.rule for a in engine.open_alerts()) == ["component_down:postgres", "log_errors:dspace"]
    for i in range(4):
        ts = T0 + 60 + i * 30
        await engine.evaluate(None, {"postgres": errored("postgres", ts), "logs": errored("logs", ts)}, ts)
    await engine.flush()
    assert sorted(a.rule for a in engine.open_alerts()) == ["component_down:postgres", "log_errors:dspace"]
    assert recorder.types() == ["opened", "opened"]


async def test_alert_of_a_vanished_mount_is_resolved(engine: AlertEngine) -> None:
    mount = {"path": "/data", "total": 100, "used": 95, "free": 5, "pct": 95.0}
    await engine.evaluate(make_sample(ts=T0, disks=[mount]), {}, T0)
    await engine.evaluate(make_sample(ts=T0 + 15, disks=[mount]), {}, T0 + 15)
    assert engine.open_alerts()[0].rule == "disk:/data"
    transitions = await engine.evaluate(make_sample(ts=T0 + 30, disks=[]), {}, T0 + 30)
    assert [t["type"] for t in transitions] == ["resolved"]
    assert engine.store.events(kind="alert")[0].message.endswith("· no longer observed")


async def test_externally_cleared_alerts_are_forgotten(engine: AlertEngine) -> None:
    await feed(engine, [90.0, 90.0])
    engine.store.clear_history()
    transitions = await feed(engine, [90.0], start=T0 + 60)
    assert transitions == []  # state re-synced, no attempt to resolve a deleted row
    transitions = await feed(engine, [90.0, 90.0], start=T0 + 75)
    assert [t["type"] for t in transitions] == ["opened"]


# --------------------------------------------------------------------- digest


async def test_daily_digest_arms_then_fires_once(mem_store: Store, bus: Bus, recorder: Recorder) -> None:
    cfg = merge_patch(
        Config(),
        {
            "notifiers": {
                "telegram": {
                    "enabled": True,
                    "chats": [{"bot_token": BOT, "chat_id": "9"}],
                    "daily_digest": "08:00",
                }
            }
        },
    )
    engine = AlertEngine(mem_store, cfg, bus, dispatcher=recorder)
    noon = _dt.datetime(2024, 3, 5, 12, 0).timestamp()
    await engine.evaluate(make_sample(ts=noon), {}, noon)
    assert recorder.types() == [] and mem_store.kv_get(DIGEST_KV_KEY) == str(int(noon))  # armed only
    await engine.evaluate(make_sample(ts=noon + 60), {}, noon + 60)
    assert recorder.types() == []
    next_morning = _dt.datetime(2024, 3, 6, 8, 0, 30).timestamp()
    await feed(engine, [96.0, 96.0], start=next_morning - 3600)  # one critical alert inside the 24 h
    await engine.evaluate(make_sample(ts=next_morning), {}, next_morning)
    await engine.flush()
    digests = [e for e in recorder.events if e["kind"] == "digest"]
    assert len(digests) == 1
    assert digests[0]["html"].splitlines()[1] == "Last 24 h: 1 alert (1 critical) · 0 incidents"
    assert "Open now: 1 alert" in digests[0]["html"]
    assert mem_store.kv_get(DIGEST_KV_KEY) == str(int(next_morning))
    await engine.evaluate(make_sample(ts=next_morning + 600), {}, next_morning + 600)
    await engine.flush()
    assert len([e for e in recorder.events if e["kind"] == "digest"]) == 1  # not again today
    assert mem_store.events(kind="notify")[0].message.startswith("telegram sent: Daily digest")


async def test_digest_not_scheduled_without_channels(engine: AlertEngine, recorder: Recorder) -> None:
    engine.reload(merge_patch(Config(), {"notifiers": {"telegram": {"daily_digest": "08:00"}}}))
    await engine.evaluate(make_sample(), {}, T0)
    assert engine.store.kv_get(DIGEST_KV_KEY) is None and recorder.events == []
    assert notifiers.digest_due_ts("nope", T0) is None and notifiers.parse_hhmm("25:00") is None


# ------------------------------------------------------------------ dispatch


async def test_dispatch_skips_disabled_and_unconfigured_channels() -> None:
    event = {
        "kind": "alert",
        "type": "opened",
        "severity": "warning",
        "html": "x",
        "text": "x",
        "summary": "x",
    }
    results = await notifiers.dispatch(Config(), event)
    assert [(r.channel, r.ok, r.skipped) for r in results] == [
        ("telegram", False, "disabled"),
        ("email", False, "disabled"),
        ("webhook", False, "disabled"),
    ]
    on = {"enabled": True}
    cfg = merge_patch(Config(), {"notifiers": {"telegram": on, "email": on, "webhook": on}})
    results = await notifiers.dispatch(cfg, event)
    # Telegram has no bot with a chat id; email has no login; the webhook has no URL
    assert [(r.channel, r.skipped) for r in results] == [
        ("telegram", "no chats"),
        ("email", "not configured"),
        ("webhook", "not configured"),
    ]
    login = {"enabled": True, "username": "s@example.org", "password": "abcdefghijklmnop"}
    results = await notifiers.dispatch(merge_patch(cfg, {"notifiers": {"email": login}}), event)
    assert [(r.channel, r.skipped) for r in results][1] == ("email", "no recipients")
    demo = Config()
    demo.demo = True
    results = await notifiers.dispatch(demo, event)
    assert [(r.channel, r.ok, r.skipped) for r in results] == [("telegram", True, "demo")]
    assert results[0].to_dict()["channel"] == "telegram"


async def test_escalation_clears_the_acknowledgement(engine: AlertEngine, recorder: Recorder) -> None:
    """F7 #6: warning → critical re-opens the alert as unacknowledged (Home is coloured again)."""
    await feed(engine, [85.0, 85.0])
    alert = engine.open_alerts()[0]
    acked = engine.ack(alert.id, T0 + 20)  # type: ignore[arg-type]
    assert acked is not None and acked.acked_ts == int(T0 + 20)
    await feed(engine, [86.0], start=T0 + 30)  # still a warning: it stays acknowledged
    assert engine.open_alerts()[0].acked_ts == int(T0 + 20)
    await feed(engine, [97.0], start=T0 + 45)
    escalated = engine.open_alerts()[0]
    assert escalated.id == alert.id and escalated.severity == "critical" and escalated.acked_ts is None
    assert recorder.types() == ["opened", "escalated"]
