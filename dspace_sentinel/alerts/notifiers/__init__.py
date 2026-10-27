"""Notification channels: ``dispatch(cfg, event)`` fans one event out to every notifier.

Rules applied here, once, for every channel:

* Quiet hours are per channel (``notifiers.telegram.quiet_hours``, ``notifiers.email.quiet_hours``
  = ``"23:00-07:00"``, read in the zone of ``server.timezone``): alert/incident notifications
  whose severity is not ``critical`` are held back on that channel; digests, test messages and
  the webhook are exempt.
* Every destination of a channel (a Telegram bot, an email recipient) has a ``min_severity``: a
  ``critical`` one only gets alert/incident events whose severity is critical. Digests, tests
  and reports go to every destination.
* An event may carry ``channels`` (e.g. the email digest: ``["email"]``); the other channels are
  left out entirely.
* In demo mode (``cfg.demo``) nothing is sent: every channel reports ``skipped="demo"`` and
  :func:`notify` writes a ``notify.demo`` event carrying the formatted message instead.
* A channel that is disabled, not configured or has nobody to send to is skipped with a reason;
  a failing one returns ``error`` – nothing here ever raises into the scheduler. One channel
  failing never keeps another from sending.
* Channels are independent in time too: an event's channels are sent side by side (each with its
  own time limit), and in the :class:`Outbox` every channel has its own lane, so a mail server
  that does not answer delays only the emails behind it, never a Telegram message, and an
  unreachable Telegram never delays an email. Each channel keeps its own order (an alert's
  "resolved" never overtakes its "opened"), and :func:`notify` records each channel's outcome as
  soon as that channel is done.
"""

from __future__ import annotations

import asyncio
import contextvars
import datetime as _dt
import logging
import time
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from dspace_sentinel.util.tz import wall, zone_of

if TYPE_CHECKING:  # pragma: no cover
    from dspace_sentinel.config import Config

log = logging.getLogger(__name__)

NOTIFY_TIMEOUT_S = 15.0
"""Hard cap per channel per event for channels without their own (the HTTP calls time out sooner)."""

OUTBOX_MAX = 500
"""Notifications waiting for delivery; beyond this the oldest waiting one is dropped (and logged)."""

QUIET_KINDS = frozenset({"alert", "incident"})
"""Event kinds subject to quiet hours and to a destination's ``min_severity`` (digests and tests are not)."""

QUIET_CHANNELS = ("telegram", "email")
"""Channels with their own ``quiet_hours`` (the webhook has none)."""


@dataclass
class NotifyResult:
    """Outcome of one channel for one event.

    ``ok`` is true when the message was delivered to every eligible destination (or, in demo
    mode, logged); ``skipped`` names the reason nothing was attempted (``disabled``, ``not
    configured``, ``no chats``, ``no recipients``, ``min severity``, ``quiet hours``, ``demo``);
    ``error`` is the failure text otherwise. ``detail["destinations"]`` lists every destination
    tried: ``{destination, label, ok, error}`` (``destination`` = chat id or address).
    """

    channel: str
    ok: bool
    skipped: str | None = None
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Plain dict form."""
        return asdict(self)


class Notifier(ABC):
    """One delivery channel."""

    id: ClassVar[str] = ""

    @abstractmethod
    def enabled(self, cfg: Config) -> bool:
        """True when the operator switched this channel on."""

    @abstractmethod
    def configured(self, cfg: Config) -> bool:
        """True when the channel has what it needs (token, login, URL …) and someone to send to."""

    def unready(self, cfg: Config) -> str | None:
        """Why nothing can be sent (the ``skipped`` reason), or ``None`` when the channel is ready."""
        return None if self.configured(cfg) else "not configured"

    def destinations(self, cfg: Config) -> list[Any] | None:
        """The channel's destinations (each with a ``min_severity``); ``None`` for a channel without."""
        return None

    def timeout(self, count: int) -> float:
        """Seconds the whole delivery to ``count`` destinations may take."""
        return NOTIFY_TIMEOUT_S

    @abstractmethod
    async def send(
        self, cfg: Config, event: dict[str, Any], destinations: Sequence[Any] | None = None
    ) -> NotifyResult:
        """Deliver ``event`` (see ``messages.notification_event``) to ``destinations`` (default:
        all). May raise; dispatch guards it."""


def summarize(results: Sequence[dict[str, Any]], noun: str, key: str = "chat_id") -> str | None:
    """``None`` when every result is ok; the one error for a single result; else
    ``"1 of 3 bots failed — Team: chat not found …"`` (names: label, else ``key``)."""
    failed = [r for r in results if not r.get("ok")]
    if not failed:
        return None
    if len(results) == 1:
        return str(failed[0].get("error") or "unknown error")
    parts = "; ".join(
        f"{r.get('label') or r.get(key) or r.get('id')}: {r.get('error') or 'unknown error'}" for r in failed
    )
    return f"{len(failed)} of {len(results)} {noun} failed — {parts}"


def destination_results(results: Sequence[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """Per-destination outcomes as ``{destination, id?, label, ok, error}`` (``destination`` = ``key``)."""
    out: list[dict[str, Any]] = []
    for r in results:
        entry = {
            "destination": r.get(key),
            "label": r.get("label") or "",
            "ok": bool(r.get("ok")),
            "error": r.get("error"),
        }
        if r.get("id"):
            entry["id"] = r["id"]
        out.append(entry)
    return out


class TelegramNotifier(Notifier):
    """``sendMessage`` with the event's HTML text, through each destination's own bot."""

    id: ClassVar[str] = "telegram"

    def enabled(self, cfg: Config) -> bool:
        return bool(cfg.notifiers.telegram.enabled)

    def configured(self, cfg: Config) -> bool:
        return bool(cfg.notifiers.telegram.destinations())

    def unready(self, cfg: Config) -> str | None:
        return None if self.configured(cfg) else "no chats"

    def destinations(self, cfg: Config) -> list[Any] | None:
        return cfg.notifiers.telegram.destinations()

    def timeout(self, count: int) -> float:
        from dspace_sentinel.alerts.notifiers import telegram

        return telegram.TIMEOUT_S * max(1, count) + 5.0

    async def send(
        self, cfg: Config, event: dict[str, Any], destinations: Sequence[Any] | None = None
    ) -> NotifyResult:
        from dspace_sentinel.alerts.notifiers import telegram

        html = str(event.get("html") or event.get("text") or "")
        result = await telegram.send_message(cfg, html, chats=destinations)
        return NotifyResult(
            self.id,
            bool(result.get("ok")),
            error=None if result.get("ok") else str(result.get("error") or "unknown error"),
            detail={
                "message_id": result.get("message_id"),
                "destinations": destination_results(result.get("results") or [], "chat_id"),
            },
        )


class EmailNotifier(Notifier):
    """One SMTP message per event, addressed to every recipient that takes it."""

    id: ClassVar[str] = "email"

    def enabled(self, cfg: Config) -> bool:
        return bool(cfg.notifiers.email.enabled)

    def configured(self, cfg: Config) -> bool:
        return self.unready(cfg) is None

    def unready(self, cfg: Config) -> str | None:
        email_cfg = cfg.notifiers.email
        if not email_cfg.server_ready():
            return "not configured"
        return None if email_cfg.recipients else "no recipients"

    def destinations(self, cfg: Config) -> list[Any] | None:
        return list(cfg.notifiers.email.recipients)

    def timeout(self, count: int) -> float:
        from dspace_sentinel.alerts.notifiers import email

        return email.TIMEOUT_S * 3 + 5.0  # connect + TLS, login, data: each bounded by the socket timeout

    async def send(
        self, cfg: Config, event: dict[str, Any], destinations: Sequence[Any] | None = None
    ) -> NotifyResult:
        from dspace_sentinel.alerts.notifiers import email

        result = await email.send_event(cfg, event, destinations)
        return NotifyResult(
            self.id,
            bool(result.get("ok")),
            error=None if result.get("ok") else str(result.get("error") or "unknown error"),
            detail={
                "subject": result.get("subject"),
                "destinations": destination_results(result.get("results") or [], "address"),
            },
        )


class WebhookNotifier(Notifier):
    """``POST`` the event as JSON."""

    id: ClassVar[str] = "webhook"

    def enabled(self, cfg: Config) -> bool:
        return bool(cfg.notifiers.webhook.enabled)

    def configured(self, cfg: Config) -> bool:
        return bool(cfg.notifiers.webhook.url)

    async def send(
        self, cfg: Config, event: dict[str, Any], destinations: Sequence[Any] | None = None
    ) -> NotifyResult:
        from dspace_sentinel.alerts.notifiers import webhook

        result = await webhook.send(cfg, event)
        return NotifyResult(
            self.id,
            bool(result.get("ok")),
            error=None if result.get("ok") else str(result.get("error") or "unknown error"),
            detail={"status_code": result.get("status_code")},
        )


NOTIFIERS: tuple[Notifier, ...] = (TelegramNotifier(), EmailNotifier(), WebhookNotifier())
"""Every channel, in dispatch order."""


def eligible(destinations: Sequence[Any], event: dict[str, Any]) -> list[Any]:
    """The destinations that take ``event``: a ``critical`` one only gets critical alert and
    incident events; digests, tests and reports go to everyone."""
    if event.get("kind") not in QUIET_KINDS or event.get("severity") == "critical":
        return list(destinations)
    return [d for d in destinations if getattr(d, "min_severity", "warning") != "critical"]


# ------------------------------------------------------------------ time windows


def parse_hhmm(text: str | None) -> int | None:
    """``"08:30"`` → 510 (minutes since midnight); ``None`` for anything else."""
    if not text:
        return None
    parts = text.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hours, minutes = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        return None
    return hours * 60 + minutes


def parse_quiet_hours(spec: str | None) -> tuple[int, int] | None:
    """``"23:00-07:00"`` → ``(1380, 420)`` in minutes; ``None`` when unset or malformed."""
    if not spec or "-" not in spec:
        return None
    start_text, _, end_text = spec.partition("-")
    start, end = parse_hhmm(start_text), parse_hhmm(end_text)
    if start is None or end is None:
        return None
    return start, end


def in_quiet_hours(spec: str | None, now: float | None = None, tz: _dt.tzinfo | None = None) -> bool:
    """True when ``now`` on the wall clock of ``tz`` (``None``: the server's zone) falls inside the
    quiet window (overnight windows wrap)."""
    window = parse_quiet_hours(spec)
    if window is None:
        return False
    start, end = window
    if start == end:
        return False
    local = wall(time.time() if now is None else now, tz)
    minute = local.hour * 60 + local.minute
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def quiet_hours_for(cfg: Config, channel: str) -> str:
    """The ``quiet_hours`` of ``channel`` (``""`` for a channel without)."""
    if channel not in QUIET_CHANNELS:
        return ""
    return str(getattr(getattr(cfg.notifiers, channel, None), "quiet_hours", "") or "")


def suppressed_by_quiet_hours(
    cfg: Config, event: dict[str, Any], now: float | None = None, channel: str = "telegram"
) -> bool:
    """Quiet hours of ``channel`` (in the zone of ``server.timezone``) hold back its non-critical
    alert/incident notifications."""
    if event.get("kind") not in QUIET_KINDS or event.get("severity") == "critical":
        return False
    return in_quiet_hours(quiet_hours_for(cfg, channel), now, zone_of(cfg))


def digest_due_ts(spec: str | None, now: float, tz: _dt.tzinfo | None = None) -> float | None:
    """Epoch seconds of today's ``HH:MM`` on the wall clock of ``tz`` (``None``: the server's zone)
    for the daily digest; ``None`` if unset."""
    minutes = parse_hhmm(spec)
    if minutes is None:
        return None
    local = wall(now, tz)
    due = local.replace(hour=minutes // 60, minute=minutes % 60, second=0, microsecond=0)
    return due.timestamp()


# ---------------------------------------------------------------------- outbox


class _Lane:
    """One channel's deliveries in an :class:`Outbox`: one at a time, in the order they were claimed."""

    __slots__ = ("_tail",)

    def __init__(self) -> None:
        self._tail: asyncio.Future[None] | None = None

    def claim(self) -> tuple[asyncio.Future[None] | None, asyncio.Future[None]]:
        """Take the next turn: ``(the turn before, which must end first; this turn's own end)``."""
        loop = asyncio.get_running_loop()
        done: asyncio.Future[None] = loop.create_future()
        previous, self._tail = self._tail, done
        if previous is not None and (previous.done() or previous.get_loop() is not loop):
            previous = None  # over already, or left behind by an event loop that is gone
        return previous, done


class _Job:
    """What :func:`dispatch` needs from the outbox job it runs in (see :data:`_CURRENT_JOB`)."""

    __slots__ = ("claimed", "outbox")

    def __init__(self, outbox: Outbox) -> None:
        self.outbox = outbox
        self.claimed: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def lane(self, channel: str) -> _Lane:
        return self.outbox.lane(channel)

    def mark_claimed(self) -> None:
        if not self.claimed.done():
            self.claimed.set_result(None)


_CURRENT_JOB: contextvars.ContextVar[_Job | None] = contextvars.ContextVar(
    "sentinel_outbox_job", default=None
)
"""The :class:`Outbox` job the running task belongs to (``None`` outside one)."""


class Outbox:
    """Delivers notification jobs in the background, one lane per channel.

    The engines run inside the scheduler's cycle lock: awaiting a Telegram API that does not
    answer (an egress firewall) or a hanging webhook there would stop every collector for the
    full HTTP timeout on each alert transition. They :meth:`submit` their notification coroutines
    here instead and return at once.

    Jobs start in the order they were submitted. A job that reaches :func:`dispatch` takes its
    turn in the lane of every channel it sends to, and the next job starts right away: each
    channel then delivers one event at a time, in order, and a channel that stalls (a mail server
    that never answers) holds up only its own lane. A job that never dispatches (a test's) runs
    alone, as before. :meth:`flush` waits until everything queued so far has been attempted;
    :meth:`close` drops what is still waiting, cancels what is under way and forgets the lanes,
    so the outbox can be used again afterwards.
    """

    def __init__(self, maxsize: int = OUTBOX_MAX) -> None:
        self.maxsize = maxsize
        self.dropped = 0
        self._jobs: deque[Coroutine[Any, Any, Any]] = deque()
        self._worker: asyncio.Task[None] | None = None
        self._running: set[asyncio.Task[Any]] = set()
        self._lanes: dict[str, _Lane] = {}

    def __len__(self) -> int:
        return len(self._jobs)

    def lane(self, channel: str) -> _Lane:
        """The delivery lane of ``channel``."""
        return self._lanes.setdefault(channel, _Lane())

    def submit(self, job: Coroutine[Any, Any, Any]) -> None:
        """Queue ``job`` for delivery (needs a running event loop)."""
        if len(self._jobs) >= self.maxsize:
            self._jobs.popleft().close()
            self.dropped += 1
            log.warning("notification outbox full: dropped the oldest waiting notification")
        self._jobs.append(job)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.get_running_loop().create_task(self._run(), name="sentinel-notify")

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while self._jobs:
            if len(self._running) >= self.maxsize:  # bounded: a stalled channel cannot pile up tasks
                await asyncio.wait(set(self._running), return_when=asyncio.FIRST_COMPLETED)
                continue
            job = _Job(self)
            token = _CURRENT_JOB.set(job)  # the task copies the context: dispatch() finds its job
            try:
                task = loop.create_task(self._jobs.popleft(), name="sentinel-notify-job")
            finally:
                _CURRENT_JOB.reset(token)
            self._running.add(task)
            task.add_done_callback(self._finished)
            # the next job starts once this one holds its place in every lane (or is done)
            await asyncio.wait({task, job.claimed}, return_when=asyncio.FIRST_COMPLETED)

    def _finished(self, task: asyncio.Task[Any]) -> None:
        self._running.discard(task)
        if not task.cancelled() and task.exception() is not None:
            # a broken job never stops the ones behind it
            log.warning("notification job failed: %s", task.exception())

    async def flush(self, timeout: float | None = None) -> bool:
        """Wait until every job queued so far has run; ``False`` when ``timeout`` expired first."""
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while True:
            pending = {t for t in (self._worker, *self._running) if t is not None and not t.done()}
            if not pending:
                return True
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                return False
            await asyncio.wait(pending, timeout=remaining)  # never cancels what it waits for

    def close(self) -> None:
        """Cancel the deliveries under way, drop every waiting job and forget the lanes.

        A job cancelled before its deliveries ran one step leaves nothing behind to end the lane
        turns it took: with the lanes forgotten, a job submitted later starts on free ones."""
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
        for task in list(self._running):
            task.cancel()
        while self._jobs:
            self._jobs.popleft().close()
        self._lanes.clear()


# ---------------------------------------------------------------------- dispatch


def _tell(on_result: Callable[[NotifyResult], Any] | None, result: NotifyResult) -> None:
    """Hand ``result`` to ``on_result`` (the audit trail); a failing callback never breaks delivery."""
    if on_result is None:
        return
    try:
        on_result(result)
    except Exception:  # pragma: no cover - notify()'s recorder guards itself
        log.exception("could not record the %s outcome", result.channel)


def _end_turn(done: asyncio.Future[None], previous: asyncio.Future[None] | None) -> None:
    """End a lane turn; one cancelled before it began ends when the turn before it does, so the
    next event on the channel still waits for the one that is actually sending."""
    if done.done():
        return
    if previous is None or previous.done():
        done.set_result(None)
        return

    def release(_: asyncio.Future[None]) -> None:
        if not done.done():
            done.set_result(None)

    previous.add_done_callback(release)


async def _send(
    notifier: Notifier, cfg: Config, event: dict[str, Any], targets: Any, limit: float
) -> NotifyResult:
    """One channel's delivery within its own time limit; never raises (except cancellation)."""
    try:
        return await asyncio.wait_for(notifier.send(cfg, event, targets), timeout=limit)
    except asyncio.TimeoutError:
        return NotifyResult(notifier.id, False, error=f"timed out after {limit:.0f} s")
    except Exception as exc:  # a broken channel must never break the others
        log.warning("notifier %s failed: %s", notifier.id, exc)
        return NotifyResult(notifier.id, False, error=f"{exc.__class__.__name__}: {exc}")


async def dispatch(
    cfg: Config,
    event: dict[str, Any],
    now: float | None = None,
    *,
    on_result: Callable[[NotifyResult], Any] | None = None,
) -> list[NotifyResult]:
    """Send ``event`` through every channel; one :class:`NotifyResult` per channel, never raises.

    The channels that send go side by side, each within its own time limit; inside an
    :class:`Outbox` job each also waits for its turn in that channel's lane (the events before it
    on the same channel). ``on_result`` is called with each channel's result as soon as that
    channel is done (skipped ones at once). The list comes back in channel order.

    In demo mode nothing leaves the machine: Telegram (always) and every other channel that is
    switched on report ``skipped="demo"`` with ``ok=True`` so callers log the message instead.
    ``event["channels"]``, when present, limits the channels considered.
    """
    only = event.get("channels")
    results: list[NotifyResult | None] = []
    plans: list[tuple[int, Notifier, list[Any] | None, float]] = []
    for notifier in NOTIFIERS:
        if only is not None and notifier.id not in only:
            continue
        if cfg.demo:
            if not (notifier.id == "telegram" or notifier.enabled(cfg)):
                continue
        elif not notifier.enabled(cfg):  # a switched-off channel is never "held back" either
            results.append(NotifyResult(notifier.id, False, skipped="disabled"))
            continue
        if suppressed_by_quiet_hours(cfg, event, now, notifier.id):
            results.append(NotifyResult(notifier.id, False, skipped="quiet hours"))
            continue
        if cfg.demo:
            results.append(NotifyResult(notifier.id, True, skipped="demo"))
            continue
        reason = notifier.unready(cfg)
        if reason:
            results.append(NotifyResult(notifier.id, False, skipped=reason))
            continue
        targets = notifier.destinations(cfg)
        if targets is not None:
            targets = eligible(targets, event)
            if not targets:
                results.append(NotifyResult(notifier.id, False, skipped="min severity"))
                continue
        plans.append((len(results), notifier, targets, notifier.timeout(len(targets or ()))))
        results.append(None)
    job = _CURRENT_JOB.get()
    turns = {index: job.lane(notifier.id).claim() for index, notifier, _, _ in plans} if job else {}
    if job is not None:
        job.mark_claimed()  # every lane holds this event's place: the outbox may start the next job
    for result in results:
        if result is not None:
            _tell(on_result, result)

    async def deliver(index: int, notifier: Notifier, targets: list[Any] | None, limit: float) -> None:
        previous, done = turns.get(index, (None, None))
        started = False
        try:
            if previous is not None and not previous.done():
                await asyncio.wait({previous})  # this channel's earlier events go first
            started = True
            result = await _send(notifier, cfg, event, targets, limit)
        finally:
            if done is not None:
                _end_turn(done, None if started else previous)
        results[index] = result
        _tell(on_result, result)

    if plans:
        await asyncio.gather(*(deliver(*plan) for plan in plans))
    return [r for r in results if r is not None]


dispatch.reports_each_channel = True  # type: ignore[attr-defined]
"""Marks the real dispatch: :func:`notify` passes it ``on_result`` (a replacement gets none)."""


async def notify(
    store: Any,
    cfg: Config,
    event: dict[str, Any],
    *,
    component: str | None = None,
    ts: float | None = None,
    dispatcher: Any = None,
) -> list[NotifyResult]:
    """:func:`dispatch` plus the audit trail in ``events``; never raises.

    Rows written: ``notify.demo`` (demo mode, carries ``html``/``text``), ``notify``
    (delivered), ``notify.skipped`` (quiet hours) and ``notify.error`` (delivery failed), each
    with ``data.channel`` and, for Telegram and email, ``data.destinations``. Each channel's row
    is written as soon as that channel is done, so a slow mail server never holds back the record
    of a Telegram message that already went out.
    ``dispatcher`` overrides :func:`dispatch` (tests); it is called as ``dispatcher(cfg, event, now)``
    where ``now`` is ``ts`` (else the event's ``ts``), the instant quiet hours are judged at.
    """
    send = dispatcher or dispatch
    now = ts if ts is not None else event.get("ts")
    summary = str(event.get("summary") or event.get("text") or "").split("\n", 1)[0]
    base = {
        "kind": event.get("kind"),
        "type": event.get("type"),
        "severity": event.get("severity"),
        "alert_id": (event.get("alert") or {}).get("id"),
        "incident_id": (event.get("incident") or {}).get("id"),
    }

    def record(result: NotifyResult) -> None:
        try:
            _record(store, result, base, summary, component, ts)
        except Exception:  # pragma: no cover - the audit row is best effort
            log.exception("could not record notification event")

    try:
        if getattr(send, "reports_each_channel", False):
            results = list(await send(cfg, event, now, on_result=record))
        else:
            results = list(await send(cfg, event, now))
            for result in results:
                record(result)
    except Exception as exc:  # pragma: no cover - dispatch guards itself; belt and braces
        log.exception("notification dispatch failed")
        results = [NotifyResult("dispatch", False, error=f"{exc.__class__.__name__}: {exc}")]
        record(results[0])
    try:
        demo_channels = [r.channel for r in results if r.skipped == "demo"]
        if demo_channels:
            data = {**base, "channels": demo_channels, "html": event.get("html"), "text": event.get("text")}
            if "email" in demo_channels:
                from dspace_sentinel.alerts.email_messages import subject_for

                data["subject"] = subject_for(event)
            store.add_event(
                "notify.demo", component, f"Notification (demo, not sent): {summary}", data, ts=ts
            )
    except Exception:  # pragma: no cover - the audit row is best effort
        log.exception("could not record notification event")
    return results


def _record(
    store: Any,
    result: NotifyResult,
    base: dict[str, Any],
    summary: str,
    component: str | None,
    ts: float | None,
) -> None:
    """The ``events`` row of one channel's outcome (none for a skip other than quiet hours, or demo)."""
    if result.error:
        data = {**base, "channel": result.channel}
        if "destinations" in result.detail:
            data["destinations"] = result.detail["destinations"]
        store.add_event("notify.error", component, f"{result.channel} failed: {result.error}", data, ts=ts)
    elif result.skipped == "quiet hours":
        store.add_event(
            "notify.skipped",
            component,
            f"{result.channel}: {summary} held back by quiet hours",
            {**base, "channel": result.channel},
            ts=ts,
        )
    elif result.ok and not result.skipped:
        store.add_event(
            "notify",
            component,
            f"{result.channel} sent: {summary}",
            {**base, "channel": result.channel, **result.detail},
            ts=ts,
        )


__all__ = [
    "NOTIFIERS",
    "NOTIFY_TIMEOUT_S",
    "OUTBOX_MAX",
    "QUIET_CHANNELS",
    "QUIET_KINDS",
    "Notifier",
    "NotifyResult",
    "Outbox",
    "EmailNotifier",
    "TelegramNotifier",
    "WebhookNotifier",
    "destination_results",
    "digest_due_ts",
    "dispatch",
    "eligible",
    "in_quiet_hours",
    "notify",
    "parse_hhmm",
    "parse_quiet_hours",
    "quiet_hours_for",
    "summarize",
    "suppressed_by_quiet_hours",
]
