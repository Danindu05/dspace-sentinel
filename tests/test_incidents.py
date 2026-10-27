"""IncidentEngine: every hypothesis, the affected closure, windowing, resolution, bus wiring."""

from __future__ import annotations

from typing import Any

import pytest

from dspace_sentinel.alerts import incidents as inc
from dspace_sentinel.alerts.incidents import IncidentEngine, affected_for, hypothesis_for, root_components
from dspace_sentinel.alerts.notifiers import NotifyResult
from dspace_sentinel.bus import Bus
from dspace_sentinel.config import Config, merge_patch
from dspace_sentinel.models import Alert
from dspace_sentinel.store import Store

T0 = 1_700_000_000.0


def A(rule: str, component: str, severity: str = "critical", message: str = "", **kw: Any) -> Alert:
    data: dict[str, Any] = {
        "id": None,
        "rule": rule,
        "component": component,
        "severity": severity,
        "status": "open",
        "title": rule,
        "message": message,
        "opened_ts": T0,
    }
    data.update(kw)
    return Alert(**data)


BACKEND_SEARCH_FAIL = "service ✅ → TCP ✅ → rest ✅ → search ❌\nHTTP 500"


# ------------------------------------------------------------------ hypotheses


@pytest.mark.parametrize(
    ("alerts", "expected"),
    [
        (
            [A("memory", "server"), A("solr_latency", "solr"), A("search_latency", "backend")],
            "Possible Solr resource exhaustion",
        ),
        (
            [
                A("swap", "server"),
                A("component_down:solr", "solr"),
                A("component_down:backend", "backend", message=BACKEND_SEARCH_FAIL),
            ],
            "Possible Solr resource exhaustion",
        ),
        (
            [A("disk", "server"), A("component_down:postgres", "postgres")],
            "Disk pressure affecting PostgreSQL",
        ),
        (
            [A("disk:/dspace", "server"), A("postgres_connections", "postgres")],
            "Disk pressure affecting PostgreSQL",
        ),
        (
            [A("component_down:postgres", "postgres"), A("rest_latency", "backend")],
            "Database outage cascading to the DSpace backend",
        ),
        (
            [A("component_down:postgres", "postgres"), A("component_down:backend", "backend")],
            "Database outage cascading to the DSpace backend",
        ),
        (
            [
                A("component_down:solr", "solr"),
                A("component_down:backend", "backend", message=BACKEND_SEARCH_FAIL),
            ],
            "Solr outage: DSpace search unavailable",
        ),
        (
            [A("component_down:solr", "solr"), A("search_latency", "backend")],
            "Solr outage: DSpace search unavailable",
        ),
        (
            [A("component_down:backend", "backend"), A("component_down:frontend", "frontend")],
            "Backend outage: the UI cannot reach the REST API",
        ),
        ([A("cpu", "server"), A("home_latency", "frontend")], "CPU saturation slowing responses"),
        ([A("load", "server"), A("rest_latency", "backend")], "CPU saturation slowing responses"),
        (
            [A("assetstore_free", "assetstore"), A("log_errors:dspace", "logs")],
            "Storage pressure causing application errors",
        ),
        (
            [A("disk", "server"), A("log_errors:tomcat", "logs")],
            "Storage pressure causing application errors",
        ),
        ([A("cpu", "server"), A("ssl_expiry", "ssl")], "Multiple alerts within 5 minutes"),
        (
            [A("component_down:solr", "solr", "warning"), A("memory", "server")],
            "Multiple alerts within 5 minutes",
        ),
    ],
)
def test_hypotheses_match_in_order(alerts: list[Alert], expected: str) -> None:
    assert hypothesis_for(alerts) == expected


def test_signals_and_order_preference() -> None:
    signals = inc.signals_for(
        [
            A("component_down:backend", "backend", message=BACKEND_SEARCH_FAIL),
            A("component_down:solr", "solr", "warning"),
        ]
    )
    assert signals == {"down:backend", "backend_search", "degraded:solr"}
    # memory + solr down + backend search matches rule 1 before rule 4
    both = [A("memory", "server"), A("component_down:solr", "solr"), A("search_latency", "backend")]
    assert hypothesis_for(both) == "Possible Solr resource exhaustion"
    assert hypothesis_for(both[1:]) == "Solr outage: DSpace search unavailable"


# ------------------------------------------------------------------- affected


def test_affected_closure_examples() -> None:
    assert affected_for([A("component_down:solr", "solr")]) == ["DSpace Search", "DSpace Backend"]
    assert affected_for(
        [A("memory", "server"), A("component_down:solr", "solr"), A("component_down:backend", "backend")]
    ) == [
        "DSpace Search",
        "DSpace Backend",
    ]
    assert affected_for([A("component_down:postgres", "postgres")]) == ["DSpace Backend"]
    assert affected_for(
        [A("component_down:backend", "backend"), A("component_down:frontend", "frontend")]
    ) == [
        "DSpace Search",
        "DSpace Frontend",
    ]
    assert affected_for([A("component_down:frontend", "frontend")]) == ["Repository site", "nginx"]
    assert affected_for([A("cpu", "server"), A("log_errors:dspace", "logs")]) == []


def test_affected_respects_enabled_modules() -> None:
    solr = [A("component_down:solr", "solr")]
    # the virtual "DSpace Search" capability is hit as soon as one of its dependencies is monitored
    assert affected_for(solr, enabled=["server", "solr"]) == ["DSpace Search"]
    assert affected_for(solr, enabled=["server", "solr", "backend"]) == ["DSpace Search", "DSpace Backend"]
    assert affected_for(solr, enabled=["server", "solr", "frontend"]) == ["DSpace Search"]
    assert affected_for([A("component_down:postgres", "postgres")], enabled=["server", "postgres"]) == []
    assert root_components(["backend", "solr", "server", "frontend", "solr"]) == ["solr", "server"]


# ------------------------------------------------------------------- engine


class Recorder:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def __call__(
        self, cfg: Config, event: dict[str, Any], now: float | None = None
    ) -> list[NotifyResult]:
        self.events.append(event)
        return [NotifyResult("telegram", True)]


@pytest.fixture
def cfg() -> Config:
    return merge_patch(
        Config(), {"modules": {"incidents": True, "solr": True, "backend": True, "logs": True}}
    )


@pytest.fixture
def bus() -> Bus:
    return Bus()


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def engine(mem_store: Store, cfg: Config, bus: Bus, recorder: Recorder) -> IncidentEngine:
    return IncidentEngine(mem_store, cfg, bus, dispatcher=recorder)


def open_alert(store: Store, alert: Alert) -> Alert:
    store.insert_alert(alert)
    return store.get_alert(alert.id)  # type: ignore[return-value]


def opened(alert: Alert) -> dict[str, Any]:
    return {"type": "opened", "alert": alert.to_dict(), "ts": alert.opened_ts}


async def test_groups_alerts_within_the_window(
    engine: IncidentEngine, mem_store: Store, bus: Bus, recorder: Recorder
) -> None:
    sub = bus.subscribe()
    memory = open_alert(mem_store, A("memory", "server", "warning", title="Memory 81 %", opened_ts=T0))
    assert await engine.on_alert_event(opened(memory)) is None  # a lone alert is not an incident
    solr = open_alert(
        mem_store,
        A("solr_latency", "solr", title="Solr search slow: 4.8 s", value=4800.0, opened_ts=T0 + 60),
    )
    incident = await engine.on_alert_event(opened(solr))
    await engine.flush()  # the notification is delivered in the background
    assert incident is not None and incident.status == "open" and incident.started_ts == int(T0)
    assert incident.hypothesis == "Multiple alerts within 5 minutes" and incident.title == incident.hypothesis
    assert [e["text"] for e in incident.sequence] == ["Memory 81 %", "Solr response 4.8 s"]
    assert incident.alert_ids == [memory.id, solr.id]
    assert mem_store.get_alert(memory.id).incident_id == incident.id
    evt = sub.queue.get_nowait()
    assert (
        evt.event == "incident" and evt.data["type"] == "opened" and evt.data["incident"]["id"] == incident.id
    )
    assert recorder.events[-1]["kind"] == "incident" and recorder.events[-1]["severity"] == "critical"
    assert recorder.events[-1]["html"].startswith(f"🧭 <b>INCIDENT #{incident.id}</b> · ")
    assert (
        mem_store.events(kind="incident")[0].message
        == f"Incident #{incident.id}: Multiple alerts within 5 minutes"
    )

    # a third alert inside the window attaches, re-runs the hypothesis and the affected list
    backend = open_alert(
        mem_store,
        A(
            "component_down:backend",
            "backend",
            title="DSpace Backend failing",
            message=BACKEND_SEARCH_FAIL,
            opened_ts=T0 + 120,
        ),
    )
    same = await engine.on_alert_event(opened(backend))
    await engine.flush()
    assert same is not None and same.id == incident.id
    assert same.hypothesis == "Possible Solr resource exhaustion"
    assert same.affected == ["DSpace Search", "DSpace Backend"]
    assert [e["text"] for e in same.sequence] == [
        "Memory 81 %",
        "Solr response 4.8 s",
        "DSpace search unavailable",
    ]
    assert sub.queue.get_nowait().data["type"] == "updated"
    assert len(recorder.events) == 1  # one message on open only

    # escalation of a grouped alert adds a timeline entry
    mem_store.update_alert(memory.id, severity="critical", title="Memory 94 %")
    crit = mem_store.get_alert(memory.id)
    esc = await engine.on_alert_event({"type": "escalated", "alert": crit.to_dict(), "ts": T0 + 130})
    assert [e["text"] for e in esc.sequence] == [
        "Memory 81 %",
        "Solr response 4.8 s",
        "DSpace search unavailable",
        "Memory 94 %",
    ]
    assert esc.sequence[-1]["severity"] == "critical" and esc.sequence[-1]["ts"] == T0 + 130

    # far outside the window: a new lone alert starts nothing
    late = open_alert(mem_store, A("cpu", "server", opened_ts=T0 + 2000))
    assert await engine.on_alert_event(opened(late)) is None
    assert len(mem_store.incidents()) == 1


async def test_resolution_when_every_alert_is_resolved(
    engine: IncidentEngine, mem_store: Store, bus: Bus, recorder: Recorder
) -> None:
    a1 = open_alert(
        mem_store, A("component_down:postgres", "postgres", title="PostgreSQL unreachable", opened_ts=T0)
    )
    a2 = open_alert(mem_store, A("rest_latency", "backend", title="REST API slow: 2.4 s", opened_ts=T0 + 30))
    await engine.on_alert_event(opened(a1))
    incident = await engine.on_alert_event(opened(a2))
    assert incident.hypothesis == "Database outage cascading to the DSpace backend"
    assert incident.affected == ["DSpace Backend"]
    assert await engine.check_resolution(T0 + 100) == []
    mem_store.resolve_alert(a1.id, T0 + 300)
    assert await engine.check_resolution(T0 + 301) == []
    mem_store.resolve_alert(a2.id, T0 + 450)
    sub = bus.subscribe()
    resolved = await engine.check_resolution(T0 + 460)
    await engine.flush()
    assert [i.id for i in resolved] == [incident.id]
    assert resolved[0].status == "resolved" and resolved[0].ended_ts == int(T0 + 450)
    assert sub.queue.get_nowait().data["type"] == "resolved"
    assert recorder.events[-1]["type"] == "resolved"
    assert recorder.events[-1]["html"].endswith(
        "Database outage cascading to the DSpace backend · lasted 7 m 30 s"
    )
    assert mem_store.events(kind="incident")[0].message == f"Incident #{incident.id} resolved after 7 m 30 s"
    assert engine.open_incidents() == [] and await engine.check_resolution(T0 + 500) == []


async def test_module_off_and_bad_events(mem_store: Store, bus: Bus) -> None:
    engine = IncidentEngine(mem_store, Config(), bus)
    assert engine.enabled is False
    a1 = open_alert(mem_store, A("cpu", "server", opened_ts=T0))
    a2 = open_alert(mem_store, A("load", "server", opened_ts=T0 + 10))
    assert await engine.on_alert_event(opened(a1)) is None
    assert await engine.on_alert_event(opened(a2)) is None
    assert mem_store.incidents() == []
    engine.reload(merge_patch(Config(), {"modules": {"incidents": True}}))
    assert engine.enabled is True
    assert await engine.on_alert_event({"type": "opened"}) is None
    assert await engine.on_alert_event({"type": "opened", "alert": {"id": 999}}) is None
    assert await engine.on_alert_event({"type": "acked", "alert": a2.to_dict()}) is None
    assert await engine.on_alert_event(opened(a2)) is not None  # a1 is within the window


async def test_events_arrive_through_the_bus(engine: IncidentEngine, mem_store: Store, bus: Bus) -> None:
    a1 = open_alert(mem_store, A("memory", "server", opened_ts=T0))
    a2 = open_alert(mem_store, A("component_down:solr", "solr", opened_ts=T0 + 20))
    bus.publish("status", {"ts": T0})
    bus.publish("alert", "not a dict")
    bus.publish("alert", opened(a1))
    bus.publish("alert", opened(a2))
    assert mem_store.incidents() == []
    await engine.check_resolution(T0 + 30)
    incidents = mem_store.incidents()
    assert len(incidents) == 1 and incidents[0].alert_ids == [a1.id, a2.id]
    assert await engine.drain() == []
    engine.close()
    assert bus.subscriber_count == 0
