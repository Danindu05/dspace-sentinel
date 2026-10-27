"""Incident correlation (SPEC section 7, "Incidents"): related alerts become one story.

Only active when the ``incidents`` module is on. When an alert opens the engine looks for
an open incident with activity in the last :data:`WINDOW_MINUTES`; failing that, for other
alerts opened inside the window without an incident, and groups them. The hypothesis is
matched in order against the set of signals derived from the incident's alerts
(:data:`HYPOTHESES`), ``affected`` is the dependency closure of the incident's root
components and ``sequence`` lists the alerts (and escalations) in time order. An incident
resolves when all its alerts are resolved.

Alert transitions reach the engine through the bus: the engine subscribes on construction
and drains the queue inside ``check_resolution`` (called by the scheduler after every
cycle, right after ``AlertEngine.evaluate``), so no extra wiring is needed;
``on_alert_event`` can also be called directly. Incident notifications are queued on an
:class:`~dspace_sentinel.alerts.notifiers.Outbox` (the scheduler shares the alert engine's, so
they go out after the alerts they group) and delivered in the background.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from dspace_sentinel.alerts import messages, notifiers
from dspace_sentinel.health import COMPONENTS, DEPENDENCY_GRAPH, dependents_of
from dspace_sentinel.models import Alert, Incident
from dspace_sentinel.util.fmt import duration
from dspace_sentinel.util.tz import zone_of

if TYPE_CHECKING:  # pragma: no cover
    from dspace_sentinel.bus import Bus, Subscription
    from dspace_sentinel.config import Config
    from dspace_sentinel.store import Store

log = logging.getLogger(__name__)

WINDOW_MINUTES = 5
"""Correlation window: alerts opened within this many minutes of each other are related."""

DEFAULT_HYPOTHESIS = "Multiple alerts within 5 minutes"

SOLR_IMPAIRED = frozenset({"solr_latency", "down:solr", "degraded:solr"})
POSTGRES_IMPAIRED = frozenset({"down:postgres", "degraded:postgres", "postgres_connections"})
BACKEND_IMPAIRED = frozenset({"down:backend", "degraded:backend", "rest_latency", "search_latency"})
BACKEND_SEARCH = frozenset({"backend_search", "search_latency"})
FRONTEND_DOWN = frozenset({"down:frontend", "degraded:frontend"})


def _any(signals: set[str], wanted: Iterable[str]) -> bool:
    return any(s in signals for s in wanted)


HYPOTHESES: tuple[tuple[str, Callable[[set[str]], bool]], ...] = (
    (
        "Possible Solr resource exhaustion",
        lambda s: _any(s, ("memory", "swap")) and _any(s, SOLR_IMPAIRED) and _any(s, BACKEND_SEARCH),
    ),
    ("Disk pressure affecting PostgreSQL", lambda s: "disk" in s and _any(s, POSTGRES_IMPAIRED)),
    (
        "Database outage cascading to the DSpace backend",
        lambda s: "down:postgres" in s and _any(s, BACKEND_IMPAIRED),
    ),
    ("Solr outage: DSpace search unavailable", lambda s: "down:solr" in s and _any(s, BACKEND_SEARCH)),
    (
        "Backend outage: the UI cannot reach the REST API",
        lambda s: "down:backend" in s and _any(s, FRONTEND_DOWN),
    ),
    (
        "CPU saturation slowing responses",
        lambda s: _any(s, ("cpu", "load")) and _any(s, ("rest_latency", "home_latency")),
    ),
    (
        "Storage pressure causing application errors",
        lambda s: _any(s, ("assetstore_free", "disk")) and "log_errors" in s,
    ),
)
"""``(hypothesis, predicate over the signal set)`` in matching order (SPEC table)."""


def signals_for(alerts: Iterable[Alert]) -> set[str]:
    """Derive the signal set of a group of alerts.

    Base rules appear as themselves (``memory``, ``disk``, ``log_errors`` …);
    ``component_down`` alerts become ``down:<id>`` (critical) or ``degraded:<id>``
    (warning), plus ``backend_search`` when the backend's failing rung is its search API.
    """
    signals: set[str] = set()
    for alert in alerts:
        base = alert.rule.split(":", 1)[0]
        if base == "component_down":
            state = "down" if alert.severity == "critical" else "degraded"
            signals.add(f"{state}:{alert.component}")
            if alert.component == "backend" and messages.failing_rung(alert.message) == "search":
                signals.add("backend_search")
        else:
            signals.add(base)
    return signals


def hypothesis_for(alerts: Iterable[Alert]) -> str:
    """The first matching hypothesis for a group of alerts (or :data:`DEFAULT_HYPOTHESIS`)."""
    signals = signals_for(alerts)
    for text, predicate in HYPOTHESES:
        try:
            if predicate(signals):
                return text
        except Exception:  # pragma: no cover - predicates are pure set logic
            continue
    return DEFAULT_HYPOTHESIS


def _ancestors(component_id: str) -> set[str]:
    """Everything ``component_id`` depends on, transitively."""
    seen: set[str] = set()
    frontier = list(DEPENDENCY_GRAPH.get(component_id, ()))
    while frontier:
        dep = frontier.pop()
        if dep in seen:
            continue
        seen.add(dep)
        frontier.extend(DEPENDENCY_GRAPH.get(dep, ()))
    return seen


def root_components(component_ids: Iterable[str]) -> list[str]:
    """Components of the group that do not depend on another member (the likely causes)."""
    ids: list[str] = []
    for cid in component_ids:
        if cid not in ids:
            ids.append(cid)
    present = set(ids)
    return [cid for cid in ids if not (_ancestors(cid) & present)]


def affected_for(alerts: Iterable[Alert], enabled: Iterable[str] | None = None) -> list[str]:
    """Human labels of what depends on the incident's root components.

    Direct dependents of every root, virtual capabilities first (``solr →
    ["DSpace Search", "DSpace Backend"]``). Real components are listed only when their module
    is in ``enabled`` (all when ``None``); a virtual capability when one of its dependencies is.
    """
    enabled_set = set(enabled) if enabled is not None else None
    labels: list[str] = []
    for root in root_components(a.component for a in alerts):
        dependents = dependents_of(root)
        dependents.sort(key=lambda cid: not COMPONENTS[cid].virtual)
        for dep in dependents:
            meta = COMPONENTS[dep]
            if enabled_set is not None:
                if meta.virtual:
                    if not any(d in enabled_set for d in meta.depends_on):
                        continue
                elif dep not in enabled_set:
                    continue
            if meta.label not in labels:
                labels.append(meta.label)
    return labels


def sequence_entry(alert: Alert, ts: float | None = None, severity: str | None = None) -> dict[str, Any]:
    """``{ts, text, severity}`` for the incident timeline."""
    return {
        "ts": float(ts if ts is not None else (alert.opened_ts or 0)),
        "text": messages.sequence_text(alert),
        "severity": severity or alert.severity,
        "alert_id": alert.id,
    }


class IncidentEngine:
    """Groups alerts opened within :data:`WINDOW_MINUTES` into one incident with a hypothesis.

    Args:
        store, cfg, bus: shared runtime objects (the engine subscribes to ``bus``).
        dispatcher: replaces ``notifiers.dispatch`` (tests).
    """

    def __init__(
        self,
        store: Store,
        cfg: Config,
        bus: Bus,
        *,
        dispatcher: Any = None,
        outbox: notifiers.Outbox | None = None,
    ) -> None:
        self.store = store
        self.cfg = cfg
        self.bus = bus
        self.dispatcher = dispatcher
        self.outbox = outbox if outbox is not None else notifiers.Outbox()
        self.window_s = WINDOW_MINUTES * 60
        self._sub: Subscription = bus.subscribe(maxsize=1024)

    @property
    def enabled(self) -> bool:
        """Incidents are only produced when the ``incidents`` module is on."""
        return bool(self.cfg.modules.incidents)

    def close(self) -> None:
        """Unsubscribe from the bus."""
        self._sub.close()

    # ------------------------------------------------------------------ inputs

    async def drain(self) -> list[Incident]:
        """Process every alert event queued on the bus since the last call."""
        touched: list[Incident] = []
        while True:
            try:
                evt = self._sub.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if evt.event != "alert" or not isinstance(evt.data, dict):
                continue
            incident = await self.on_alert_event(evt.data)
            if incident is not None:
                touched.append(incident)
        return touched

    async def on_alert_event(self, event: dict[str, Any]) -> Incident | None:
        """React to an alert transition ``{type: opened|escalated|resolved|acked, alert: {...}}``.

        ``opened`` may create or extend an incident, ``escalated`` adds a timeline entry to
        the alert's incident; everything else is ignored. Returns the incident touched.
        Never raises.
        """
        try:
            if not self.enabled:
                return None
            data = event.get("alert") or {}
            alert_id = data.get("id")
            if alert_id is None:
                return None
            alert = self.store.get_alert(int(alert_id))
            if alert is None:
                return None
            etype = event.get("type")
            if etype == "opened":
                return await self._on_opened(alert)
            if etype == "escalated":
                return await self._on_escalated(alert, event.get("ts"))
            return None
        except Exception:
            log.exception("incident correlation failed")
            return None

    # ------------------------------------------------------------- correlation

    def _last_activity(self, incident: Incident) -> float:
        stamps = [float(incident.started_ts)]
        stamps.extend(float(e.get("ts") or 0) for e in incident.sequence)
        return max(stamps)

    def active_incident(self, ts: float) -> Incident | None:
        """The open incident with activity within the window before ``ts``, if any."""
        best: Incident | None = None
        best_ts = 0.0
        for incident in self.store.open_incidents():
            last = self._last_activity(incident)
            if ts - last <= self.window_s and last >= best_ts:
                best, best_ts = incident, last
        return best

    async def _on_opened(self, alert: Alert) -> Incident | None:
        if alert.incident_id is not None:
            return None
        ts = float(alert.opened_ts or time.time())
        incident = self.active_incident(ts)
        if incident is not None:
            return await self._attach(incident, alert)
        others = [
            a
            for a in self.store.alerts_between(ts - self.window_s, ts)
            if a.incident_id is None and a.id != alert.id
        ]
        if not others:
            return None
        group = sorted([*others, alert], key=lambda a: (a.opened_ts or 0, a.id or 0))
        return await self._create(group)

    def _enabled_ids(self) -> list[str]:
        return ["server", *self.cfg.modules.enabled_ids()]

    async def _create(self, group: list[Alert]) -> Incident:
        hypothesis = hypothesis_for(group)
        affected = affected_for(group, self._enabled_ids())
        sequence = sorted((sequence_entry(a) for a in group), key=lambda e: (e["ts"], e["alert_id"] or 0))
        started = min(float(a.opened_ts or time.time()) for a in group)
        incident = Incident(
            id=None,
            title=hypothesis,
            hypothesis=hypothesis,
            status="open",
            started_ts=started,
            affected=affected,
            sequence=sequence,
            alert_ids=[a.id for a in group if a.id is not None],
        )
        incident_id = self.store.insert_incident(incident)
        incident = self.store.get_incident(incident_id) or incident
        component = root_components(a.component for a in group)[0] if group else None
        self.store.add_event(
            "incident",
            component,
            f"Incident #{incident.id}: {incident.hypothesis}",
            {"incident_id": incident.id, "type": "opened", "alert_ids": incident.alert_ids},
            ts=started,
        )
        self.bus.publish("incident", {"type": "opened", "incident": incident.to_dict()})
        await self._notify(
            "opened",
            incident,
            messages.incident_html(incident, self.cfg.hostname, tz=zone_of(self.cfg)),
            f"Incident #{incident.id}: {incident.hypothesis}",
            group,
            component,
        )
        return incident

    async def _attach(self, incident: Incident, alert: Alert) -> Incident:
        assert incident.id is not None
        self.store.attach_alert_to_incident(alert.id, incident.id)  # type: ignore[arg-type]
        alerts = self.store.alerts_for_incident(incident.id)
        entry = sequence_entry(alert)
        sequence = sorted(
            [*incident.sequence, entry], key=lambda e: (float(e.get("ts") or 0), e.get("alert_id") or 0)
        )
        hypothesis = hypothesis_for(alerts)
        updated = self.store.update_incident(
            incident.id,
            title=hypothesis,
            hypothesis=hypothesis,
            affected=affected_for(alerts, self._enabled_ids()),
            sequence=sequence,
        )
        incident = updated or incident
        self.store.add_event(
            "incident",
            alert.component,
            f"Incident #{incident.id}: {entry['text']}",
            {"incident_id": incident.id, "type": "updated", "alert_id": alert.id},
            ts=entry["ts"],
        )
        self.bus.publish("incident", {"type": "updated", "incident": incident.to_dict()})
        return incident

    async def _on_escalated(self, alert: Alert, ts: float | None) -> Incident | None:
        if alert.incident_id is None:
            return None
        incident = self.store.get_incident(alert.incident_id)
        if incident is None or incident.status != "open":
            return None
        when = float(ts or time.time())
        entry = sequence_entry(alert, when, "critical")
        if incident.sequence and incident.sequence[-1].get("text") == entry["text"]:
            return incident
        alerts = self.store.alerts_for_incident(incident.id)  # type: ignore[arg-type]
        hypothesis = hypothesis_for(alerts)
        sequence = sorted(
            [*incident.sequence, entry], key=lambda e: (float(e.get("ts") or 0), e.get("alert_id") or 0)
        )
        updated = self.store.update_incident(
            incident.id,  # type: ignore[arg-type]
            title=hypothesis,
            hypothesis=hypothesis,
            affected=affected_for(alerts, self._enabled_ids()),
            sequence=sequence,
        )
        incident = updated or incident
        self.bus.publish("incident", {"type": "updated", "incident": incident.to_dict()})
        return incident

    # -------------------------------------------------------------- resolution

    async def check_resolution(self, now: float | None = None) -> list[Incident]:
        """Drain queued alert events, then resolve open incidents whose alerts are all resolved.

        Returns the incidents resolved in this call. Never raises.
        """
        now = now or time.time()
        try:
            await self.drain()
        except Exception:
            log.exception("draining alert events failed")
        resolved: list[Incident] = []
        try:
            for incident in self.store.open_incidents():
                alerts = self.store.alerts_for_incident(incident.id)  # type: ignore[arg-type]
                if not alerts or any(a.status != "resolved" for a in alerts):
                    continue
                ended = max(float(a.resolved_ts or now) for a in alerts)
                updated = self.store.update_incident(incident.id, status="resolved", ended_ts=ended)  # type: ignore[arg-type]
                incident = updated or incident
                lasted = duration(max(0.0, ended - float(incident.started_ts)))
                component = root_components(a.component for a in alerts)[0] if alerts else None
                self.store.add_event(
                    "incident",
                    component,
                    f"Incident #{incident.id} resolved after {lasted}",
                    {"incident_id": incident.id, "type": "resolved"},
                    ts=ended,
                )
                self.bus.publish("incident", {"type": "resolved", "incident": incident.to_dict()})
                await self._notify(
                    "resolved",
                    incident,
                    messages.incident_resolved_html(incident, self.cfg.hostname, now),
                    f"Incident #{incident.id} resolved after {lasted}",
                    alerts,
                    component,
                )
                resolved.append(incident)
        except Exception:
            log.exception("incident resolution failed")
        return resolved

    async def _notify(
        self,
        event_type: str,
        incident: Incident,
        html: str,
        summary: str,
        alerts: Iterable[Alert],
        component: str | None,
    ) -> None:
        """Queue the incident notification on :attr:`outbox` (delivered in the background)."""
        severity = "critical" if any(a.severity == "critical" for a in alerts) else "warning"
        now = time.time()
        event = messages.notification_event(
            "incident",
            event_type,
            hostname=self.cfg.hostname,
            severity=severity,
            html=html,
            summary=summary,
            incident=incident,
            ts=now,
        )
        job = notifiers.notify(
            self.store, self.cfg, event, component=component, ts=now, dispatcher=self.dispatcher
        )
        self.outbox.submit(job)

    async def flush(self, timeout: float | None = None) -> bool:
        """Wait until the queued notifications were attempted (``False`` on timeout)."""
        return await self.outbox.flush(timeout)

    # -------------------------------------------------------------------- misc

    def open_incidents(self) -> list[Incident]:
        """Every open incident."""
        return self.store.open_incidents()

    def reload(self, cfg: Config) -> None:
        """Hot-apply a new configuration (the ``incidents`` module switch, hostname)."""
        self.cfg = cfg


__all__ = [
    "DEFAULT_HYPOTHESIS",
    "HYPOTHESES",
    "WINDOW_MINUTES",
    "IncidentEngine",
    "affected_for",
    "hypothesis_for",
    "root_components",
    "sequence_entry",
    "signals_for",
]
