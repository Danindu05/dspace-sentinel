"""Alert engine (SPEC section 7): the per-rule state machine, ack, reminders, digest.

The scheduler calls ``await engine.evaluate(sample, components, now)`` after every
collector group. For every observation the engine keeps, per ``<rule>@<component>`` key,
a breach streak, an OK streak and the currently open alert:

* open after ``alerts.confirm_samples`` consecutive breaching observations,
* escalate warning → critical immediately when the critical threshold is crossed (which
  clears an acknowledgement: the alert is unacknowledged again),
* resolve after ``alerts.clear_samples`` consecutive OK observations (hysteresis: the
  rules only flag OK below warning − 2 % of range, or when the component is healthy),
* an observation inside the hysteresis band (or unmeasurable) resets both streaks.

Every transition writes an ``events`` row, publishes a bus ``alert`` event
``{type: opened|escalated|resolved|acked|unacked, alert}`` and, for opened/escalated/
resolved, notifies through ``notifiers.notify`` (Telegram / email / webhook; logged only in
demo mode). Deliveries are queued on a :class:`~dspace_sentinel.alerts.notifiers.Outbox` and
sent in the background, in order, so an unreachable Telegram API or mail server never holds up
the scheduler's cycle. Reminders go out every ``alerts.reminder_minutes`` while an alert is open
and unacknowledged. The daily digest goes out per channel: at ``notifiers.telegram.daily_digest``
to Telegram and the webhook, at ``notifiers.email.daily_digest`` by email (both read in the zone
of ``server.timezone``, like the quiet hours and the times in the messages). Open alerts are
restored from the store on start-up so a restart keeps them. Nothing here raises into the
scheduler.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from dspace_sentinel.alerts import messages, notifiers, rules
from dspace_sentinel.alerts.rules import INVERTED_RULES, Observation
from dspace_sentinel.models import Alert, ComponentHealth, SystemSample
from dspace_sentinel.util.fmt import duration
from dspace_sentinel.util.tz import zone_of

if TYPE_CHECKING:  # pragma: no cover
    from dspace_sentinel.bus import Bus
    from dspace_sentinel.config import Config
    from dspace_sentinel.store import Store

log = logging.getLogger(__name__)

DIGEST_KV_KEY = "last_digest_ts"
"""``kv`` key holding when the last Telegram/webhook daily digest went out (or was armed)."""

EMAIL_DIGEST_KV_KEY = "last_email_digest_ts"
"""The same for the email daily digest (its own time: ``notifiers.email.daily_digest``)."""


@dataclass
class RuleState:
    """Streak counters and the open alert for one ``<rule>@<component>`` key."""

    breach_count: int = 0
    ok_count: int = 0
    alert: Alert | None = None
    last_observed_ts: float | None = None
    last_notified_ts: float | None = None
    last_severity: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Diagnostics view."""
        return {
            "breach_count": self.breach_count,
            "ok_count": self.ok_count,
            "alert_id": self.alert.id if self.alert is not None else None,
            "severity": self.alert.severity if self.alert is not None else self.last_severity,
            "last_observed_ts": self.last_observed_ts,
            "last_notified_ts": self.last_notified_ts,
        }


@dataclass
class _Cycle:
    """Scratch space for one ``evaluate`` call."""

    now: float
    transitions: list[dict[str, Any]] = field(default_factory=list)
    jobs: list[Coroutine[Any, Any, Any]] = field(default_factory=list)
    observed_keys: set[str] = field(default_factory=set)


class AlertEngine:
    """Per rule+component breach/ok counters, open alerts, ack/unack, reminders, digest.

    Args:
        store, cfg, bus: shared runtime objects.
        dispatcher: replaces ``notifiers.dispatch`` (tests); resolved lazily so monkeypatching
            the module attribute also works.
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
        self.last_evaluated_ts: float | None = None
        self.evaluations = 0
        self._states: dict[str, RuleState] = {}
        self._restore()

    # ------------------------------------------------------------- persistence

    @staticmethod
    def key_for(alert: Alert) -> str:
        """The state key of an alert: ``<rule>@<component>``."""
        return f"{alert.rule}@{alert.component}"

    def _restore(self) -> None:
        """Adopt every open alert from the store (a restart must not forget them)."""
        try:
            for alert in self.store.open_alerts():
                st = self._states.setdefault(self.key_for(alert), RuleState())
                st.alert = alert
                st.last_notified_ts = alert.notified_ts
                st.last_severity = alert.severity
        except Exception:
            log.exception("could not restore open alerts")

    def _sync_with_store(self) -> None:
        """Forget alerts that were resolved or deleted behind our back (clear-history, edits)."""
        open_ids = {a.id for a in self.store.open_alerts()}
        for st in self._states.values():
            if st.alert is not None and st.alert.id not in open_ids:
                st.alert = None
                st.breach_count = 0
                st.ok_count = 0

    def states(self) -> dict[str, RuleState]:
        """The live state table (read-only by convention)."""
        return self._states

    def describe(self) -> dict[str, Any]:
        """Diagnostics: counters per key, last evaluation."""
        return {
            "last_evaluated_ts": self.last_evaluated_ts,
            "evaluations": self.evaluations,
            "states": {key: st.to_dict() for key, st in self._states.items()},
        }

    def reset(self) -> None:
        """Drop all in-memory state (after *Clear history*)."""
        self._states.clear()

    # ---------------------------------------------------------------- evaluate

    async def evaluate(
        self, sample: SystemSample | None, components: dict[str, ComponentHealth], now: float | None = None
    ) -> list[dict[str, Any]]:
        """Run the rules and drive the state machine. Returns the bus payloads emitted. Never raises.

        Notifications are queued on :attr:`outbox` and delivered in the background (the scheduler
        calls this while holding its cycle lock); :meth:`flush` waits for them.
        """
        now = now or time.time()
        self.last_evaluated_ts = now
        self.evaluations += 1
        cycle = _Cycle(now=now)
        try:
            self._sync_with_store()
            observations = rules.evaluate(sample, components, self.cfg)
        except Exception:
            log.exception("alert rules failed")
            observations = []
        for obs in observations:
            try:
                self._apply(obs, cycle)
            except Exception:
                log.exception("alert rule %s failed", obs.key)
        try:
            self._resolve_stale(sample, components, cycle)
            self._reminders(cycle)
            self._maybe_digest(cycle)
        except Exception:
            log.exception("alert housekeeping failed")
        for job in cycle.jobs:
            self.outbox.submit(job)
        return cycle.transitions

    async def flush(self, timeout: float | None = None) -> bool:
        """Wait until the queued notifications were attempted (``False`` on timeout)."""
        return await self.outbox.flush(timeout)

    def _apply(self, obs: Observation, cycle: _Cycle) -> None:
        st = self._states.setdefault(obs.key, RuleState())
        cycle.observed_keys.add(obs.key)
        if obs.observed_ts is not None and st.last_observed_ts == obs.observed_ts:
            return  # same measurement seen again (another group ran); count it once
        st.last_observed_ts = obs.observed_ts
        st.last_severity = obs.severity
        if obs.severity is not None:
            st.ok_count = 0
            st.breach_count += 1
            if st.alert is None:
                if st.breach_count >= self.cfg.alerts.confirm_samples:
                    self._open(st, obs, cycle)
            elif obs.severity == "critical" and st.alert.severity == "warning":
                self._escalate(st, obs, cycle)
            elif obs.severity == st.alert.severity and self._should_refresh(st.alert, obs):
                self._refresh(st, obs)
        elif obs.ok:
            st.breach_count = 0
            st.ok_count += 1
            if st.alert is not None and st.ok_count >= self.cfg.alerts.clear_samples:
                self._resolve(st, cycle, notify=True)
        else:
            st.breach_count = 0
            st.ok_count = 0

    @staticmethod
    def _should_refresh(alert: Alert, obs: Observation) -> bool:
        """Update the stored text when the situation got worse or the reason changed.

        ``component_down`` and the Security rules carry no value: their text follows the rung's
        message (a second new port, a different failing rung).
        """
        if obs.base_rule == "component_down" or obs.base_rule.startswith("security_"):
            return obs.title != alert.title or obs.message != alert.message
        if obs.value is None or alert.value is None:
            return False
        if obs.base_rule in INVERTED_RULES:
            return obs.value < alert.value
        return obs.value > alert.value

    # ------------------------------------------------------------ transitions

    def _publish(self, event_type: str, alert: Alert, cycle: _Cycle | None) -> dict[str, Any]:
        ts = cycle.now if cycle is not None else time.time()
        payload = {"type": event_type, "alert": alert.to_dict(), "ts": ts}
        self.bus.publish("alert", payload)
        if cycle is not None:
            cycle.transitions.append(payload)
        return payload

    def _event(self, alert: Alert, event_type: str, message: str, ts: float) -> None:
        self.store.add_event(
            "alert",
            alert.component,
            message,
            {"alert_id": alert.id, "type": event_type, "rule": alert.rule, "severity": alert.severity},
            ts=ts,
        )

    def _open(self, st: RuleState, obs: Observation, cycle: _Cycle) -> None:
        alert = Alert(
            id=None,
            rule=obs.rule,
            component=obs.component,
            severity=obs.severity or "warning",
            status="open",
            title=obs.title,
            message=obs.message,
            value=obs.value,
            threshold=obs.threshold,
            opened_ts=cycle.now,
        )
        alert_id = self.store.insert_alert(alert)
        alert = self.store.get_alert(alert_id) or alert
        st.alert = alert
        st.ok_count = 0
        self._event(alert, "opened", f"{alert.severity.upper()} {alert.title}", cycle.now)
        self._publish("opened", alert, cycle)
        cycle.jobs.append(self._notify_alert("opened", st, alert, cycle.now))

    def _escalate(self, st: RuleState, obs: Observation, cycle: _Cycle) -> None:
        """Warning → critical: a new situation, so an acknowledgement is cleared (Home, the
        reminders and ``doctor`` treat the alert as unacknowledged again)."""
        assert st.alert is not None
        alert = self.store.update_alert(
            st.alert.id,  # type: ignore[arg-type]
            severity="critical",
            title=obs.title,
            message=obs.message,
            value=obs.value,
            threshold=obs.threshold,
            acked_ts=None,
        )
        if alert is None:  # vanished from the store
            st.alert = None
            return
        st.alert = alert
        self._event(alert, "escalated", f"Escalated to CRITICAL: {alert.title}", cycle.now)
        self._publish("escalated", alert, cycle)
        cycle.jobs.append(self._notify_alert("escalated", st, alert, cycle.now))

    def _refresh(self, st: RuleState, obs: Observation) -> None:
        assert st.alert is not None
        alert = self.store.update_alert(
            st.alert.id,  # type: ignore[arg-type]
            title=obs.title,
            message=obs.message,
            value=obs.value,
            threshold=obs.threshold if obs.threshold is not None else st.alert.threshold,
        )
        if alert is not None:
            st.alert = alert

    def _resolve(self, st: RuleState, cycle: _Cycle, *, notify: bool, note: str | None = None) -> None:
        assert st.alert is not None
        alert = self.store.resolve_alert(st.alert.id, cycle.now)  # type: ignore[arg-type]
        st.alert = None
        st.breach_count = 0
        st.ok_count = 0
        if alert is None:
            return
        lasted = duration(max(0.0, (alert.resolved_ts or cycle.now) - (alert.opened_ts or cycle.now)))
        text = f"Resolved: {alert.title} · lasted {lasted}"
        if note:
            text += f" · {note}"
        self._event(alert, "resolved", text, cycle.now)
        self._publish("resolved", alert, cycle)
        if notify:
            cycle.jobs.append(self._notify_alert("resolved", st, alert, cycle.now))

    def _resolve_stale(
        self, sample: SystemSample | None, components: dict[str, ComponentHealth], cycle: _Cycle
    ) -> None:
        """Close alerts whose subject disappeared: module switched off, mount/log file removed.

        A component whose last check could not run (the collector timed out or raised) reports
        none of its subjects; that says nothing about them, so its alerts are left alone.
        """
        for key, st in list(self._states.items()):
            alert = st.alert
            if alert is None or key in cycle.observed_keys:
                continue
            component = alert.component
            if component == "server":
                if sample is None:
                    continue
                note = "no longer observed"
            elif not self.cfg.module_enabled(component):
                note = "module switched off"
            elif component in components:
                comp = components[component]
                if comp is not None and any(rules.is_collector_error(r) for r in comp.chain):
                    continue
                note = "no longer observed"
            else:
                continue  # module on but not collected yet (start-up): leave it alone
            self._resolve(st, cycle, notify=False, note=note)
            self._states.pop(key, None)

    # ----------------------------------------------------------- notifications

    async def _notify_alert(self, event_type: str, st: RuleState, alert: Alert, now: float) -> None:
        hostname = self.cfg.hostname
        if event_type == "resolved":
            html = messages.resolved_html(alert, hostname, now)
            summary = f"Resolved {alert.title}"
        elif event_type == "reminder":
            html = messages.reminder_html(alert, hostname, now)
            summary = f"Reminder {alert.severity.upper()} {alert.title}"
        else:
            html = messages.alert_html(
                alert, hostname, escalated=event_type == "escalated", tz=zone_of(self.cfg)
            )
            summary = f"{alert.severity.upper()} {alert.title}"
        event = messages.notification_event(
            "alert",
            event_type,
            hostname=hostname,
            severity=alert.severity,
            html=html,
            summary=summary,
            alert=alert,
            ts=now,
        )
        results = await notifiers.notify(
            self.store, self.cfg, event, component=alert.component, ts=now, dispatcher=self.dispatcher
        )
        if any(r.ok for r in results) and alert.id is not None:
            st.last_notified_ts = now
            try:
                updated = self.store.update_alert(alert.id, notified_ts=int(now))
                if updated is not None and st.alert is not None and st.alert.id == alert.id:
                    st.alert = updated
            except Exception:
                log.exception("could not record notified_ts")

    def _reminders(self, cycle: _Cycle) -> None:
        minutes = self.cfg.alerts.reminder_minutes
        if minutes <= 0:
            return
        for st in self._states.values():
            alert = st.alert
            if alert is None or alert.acked_ts:
                continue
            last = st.last_notified_ts or alert.notified_ts or alert.opened_ts or cycle.now
            if cycle.now - last >= minutes * 60:
                st.last_notified_ts = cycle.now  # claim the slot before the (async) send
                self._event(
                    alert,
                    "reminder",
                    f"Reminder: {alert.severity.upper()} {alert.title} · open for "
                    f"{duration(max(0.0, cycle.now - (alert.opened_ts or cycle.now)))}",
                    cycle.now,
                )
                cycle.jobs.append(self._notify_alert("reminder", st, alert, cycle.now))

    def _digest_slots(self) -> list[tuple[str, str, list[str], bool]]:
        """``(kv key, "HH:MM", channels, active)`` of each daily digest: Telegram's (which the
        webhook shares, as it always did) and email's."""
        n = self.cfg.notifiers
        return [
            (
                DIGEST_KV_KEY,
                n.telegram.daily_digest,
                ["telegram", "webhook"],
                bool(n.telegram.enabled or n.webhook.enabled or self.cfg.demo),
            ),
            (EMAIL_DIGEST_KV_KEY, n.email.daily_digest, ["email"], bool(n.email.enabled)),
        ]

    def _maybe_digest(self, cycle: _Cycle) -> None:
        for kv_key, spec, channels, active in self._digest_slots():
            if not spec or not active:
                continue
            due = notifiers.digest_due_ts(spec, cycle.now, zone_of(self.cfg))
            if due is None:
                continue
            last_raw = self.store.kv_get(kv_key)
            if last_raw is None:
                # First time we see a digest time: arm it, do not fire for a time already past today.
                self.store.kv_set(kv_key, str(int(cycle.now)))
                continue
            try:
                last = float(last_raw)
            except ValueError:
                last = 0.0
            if cycle.now >= due and last < due:
                self.store.kv_set(kv_key, str(int(cycle.now)))
                cycle.jobs.append(self.send_digest(cycle.now, channels=channels))

    async def send_digest(
        self, now: float | None = None, channels: list[str] | None = None
    ) -> list[notifiers.NotifyResult]:
        """Build and send the daily digest for the last 24 h (also usable on demand).

        ``channels`` limits where it goes (default: every channel).
        """
        now = now or time.time()
        hostname = self.cfg.hostname
        opened = self.store.alerts_between(now - 86400, now)
        incidents = self.store.incidents("all", limit=100, since=now - 86400)
        open_now = self.store.open_alerts()
        html = messages.digest_html(hostname, opened, incidents, open_now, now)
        severity = "critical" if any(a.severity == "critical" for a in open_now) else "warning"
        event = messages.notification_event(
            "digest",
            "digest",
            hostname=hostname,
            severity=severity,
            html=html,
            summary=f"Daily digest: {len(opened)} alerts, {len(incidents)} incidents in 24 h",
            ts=now,
        )
        if channels is not None:
            event["channels"] = list(channels)
        return await notifiers.notify(self.store, self.cfg, event, ts=now, dispatcher=self.dispatcher)

    # ---------------------------------------------------------------- ack / misc

    def open_alerts(self) -> list[Alert]:
        """Every currently open alert (newest first)."""
        return self.store.open_alerts()

    def _adopt(self, alert: Alert) -> None:
        st = self._states.get(self.key_for(alert))
        if st is not None and st.alert is not None and st.alert.id == alert.id:
            st.alert = alert

    def ack(self, alert_id: int, now: float | None = None) -> Alert | None:
        """Acknowledge an alert (silences reminders); emits a bus ``alert`` event of type ``acked``."""
        if self.store.get_alert(alert_id) is None:
            return None
        alert = self.store.ack_alert(alert_id, now or time.time())
        if alert is not None:
            self._adopt(alert)
            self.store.add_event(
                "alert",
                alert.component,
                f"Acknowledged: {alert.title}",
                {"alert_id": alert.id, "type": "acked"},
            )
            self._publish("acked", alert, None)
        return alert

    def unack(self, alert_id: int) -> Alert | None:
        """Remove an acknowledgement; emits a bus ``alert`` event of type ``unacked``."""
        if self.store.get_alert(alert_id) is None:
            return None
        alert = self.store.unack_alert(alert_id)
        if alert is not None:
            self._adopt(alert)
            self.store.add_event(
                "alert",
                alert.component,
                f"Unacknowledged: {alert.title}",
                {"alert_id": alert.id, "type": "unacked"},
            )
            self._publish("unacked", alert, None)
        return alert

    def reload(self, cfg: Config) -> None:
        """Hot-apply a new configuration (thresholds, confirm/clear counts, notifiers, modules).

        Alerts of modules that were switched off are resolved on the next evaluation.
        """
        self.cfg = cfg


__all__ = ["DIGEST_KV_KEY", "EMAIL_DIGEST_KV_KEY", "AlertEngine", "RuleState"]
