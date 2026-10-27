"""The asyncio loop: runs collectors on their intervals and feeds state, store, alerts and the bus.

One task per *collector group* (all collectors sharing an ``interval_key``). Every
``collect`` runs under ``asyncio.wait_for`` with the collector's own timeout,
``max(interval - 1, budget_s)`` and at least 2 s (``Collector.timeout``), so a slow service
with a short interval still gets its full budget; any exception becomes a single ``unknown``
rung so a broken check can never crash the loop. After each group run
the alert engine, the incident engine and the report schedule hooks are called, each
guarded, so stub implementations and failures are both harmless. Nothing slow happens under
the cycle lock: the engines queue their notifications for background delivery and the weekly
report hook runs as its own task.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from dspace_sentinel import health
from dspace_sentinel.bus import Bus
from dspace_sentinel.collectors import COLLECTOR_ORDER, Collector, CollectorContext, CollectorResult
from dspace_sentinel.config import Config
from dspace_sentinel.models import CheckResult, ComponentHealth
from dspace_sentinel.state import State
from dspace_sentinel.store import Store
from dspace_sentinel.util.httpc import make_client

if TYPE_CHECKING:  # pragma: no cover
    from dspace_sentinel.collectors.demo import DemoSuite

log = logging.getLogger(__name__)

MAINTENANCE_INTERVAL_S = 3600
MAINTENANCE_FIRST_DELAY_S = 90
STATUS_EVENT_MIN_INTERVAL_S = 5.0

CHECK_NOW_MIN_INTERVAL_S = 10.0
"""While the service runs, a *Check now* this soon after the previous one returns the current
state instead of running every collector again (a config reload always allows a fresh one)."""

STOP_FLUSH_TIMEOUT_S = 10.0
"""On shutdown, how long queued notifications may still take before they are dropped."""

OWN_INTERVAL_ONLY: frozenset[str] = frozenset({"repository_interval"})
"""Groups a *Check now* leaves alone until their own interval is due (the repository counts are
``count(*)`` scans of the production database)."""


def _undelivered(outbox: Any) -> int:
    """The notifications a closing outbox gives up: those under way plus those not started.

    With a lane per channel every job starts at once and then waits for its turn inside the lane
    of each channel it sends to, so the jobs not started yet (``len(outbox)``) are only the few
    beyond the outbox's bound: an email waiting behind a mail server that does not answer is a job
    under way. A notification is one event, counted once however many of its channels are left.
    """
    under_way = sum(1 for task in list(getattr(outbox, "_running", ()) or ()) if not task.done())
    return under_way + len(outbox)


@dataclass
class Group:
    """Collectors that share one interval, and the task that runs them."""

    key: str
    interval: int
    collectors: list[Collector]
    task: asyncio.Task | None = None
    next_due: float = 0.0
    last_run: float | None = None
    run_count: int = 0

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(c.id for c in self.collectors)

    def signature(self) -> tuple[Any, ...]:
        return (self.interval, self.ids)


@dataclass
class _Hooks:
    """Optional engines; every call is guarded so missing methods and failures are harmless."""

    alert_engine: Any = None
    incident_engine: Any = None
    notifier: Any = None
    errors: dict[str, int] = field(default_factory=dict)


class Scheduler:
    """Owns the collector tasks. Construct, then ``await start()``; ``await stop()`` on shutdown.

    Args:
        cfg: effective configuration.
        store, state, bus: shared runtime objects.
        alert_engine: object with ``async evaluate(sample, components, now)`` and ``reload(cfg)``.
        incident_engine: object with ``async check_resolution(now)`` and ``reload(cfg)``.
        http: an ``httpx.AsyncClient`` to share (created from the config when ``None``).
        demo_backfill_days: how much history the demo suite inserts on first start.
        demo_suite: inject a prepared :class:`DemoSuite` (tests); built from ``cfg`` otherwise.
    """

    def __init__(
        self,
        cfg: Config,
        store: Store,
        state: State,
        bus: Bus,
        alert_engine: Any = None,
        incident_engine: Any = None,
        http: httpx.AsyncClient | None = None,
        demo_backfill_days: int = 7,
        demo_suite: DemoSuite | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.state = state
        self.bus = bus
        self.alert_engine = alert_engine
        self.incident_engine = incident_engine
        self.http = http
        self._own_http = http is None
        self.demo_backfill_days = demo_backfill_days
        self._demo_suite = demo_suite
        self.groups: dict[str, Group] = {}
        self.collectors: list[Collector] = []
        self._cycle_lock = asyncio.Lock()
        self._check_now: asyncio.Task | None = None
        self._last_check_now: float | None = None
        self._maintenance_task: asyncio.Task | None = None
        self._running = False
        self._last_status_event = 0.0
        self._hooks = _Hooks(alert_engine, incident_engine)
        self._report_task: asyncio.Task | None = None
        self.backfill_counts: dict[str, int] = {}
        # One delivery queue for both engines, so an incident's message follows its alerts'.
        shared_outbox = getattr(alert_engine, "outbox", None)
        if shared_outbox is not None and hasattr(incident_engine, "outbox"):
            incident_engine.outbox = shared_outbox
        self._build_groups(cfg)

    # ---------------------------------------------------------------- set-up

    def _demo(self, cfg: Config) -> DemoSuite | None:
        if not cfg.demo:
            return None
        if self._demo_suite is None:
            from dspace_sentinel.collectors.demo import DemoSuite

            self._demo_suite = DemoSuite(cfg)
        else:
            self._demo_suite.cfg = cfg
            self._demo_suite.world.cfg = cfg
        return self._demo_suite

    def _make_collectors(self, cfg: Config) -> list[Collector]:
        suite = self._demo(cfg)
        if suite is not None:
            return suite.collectors()
        from dspace_sentinel.collectors import enabled_collectors

        return enabled_collectors(cfg)

    def _group_interval(self, cfg: Config, interval_key: str) -> int:
        """The interval of one collector group; ``SENTINEL_DEMO_FAST`` compresses every group.

        Fast demo mode is a runtime thing: the config (and what ``PATCH /api/config`` saves)
        keeps the operator's intervals, only the running groups tick every
        :data:`~dspace_sentinel.collectors.demo.FAST_INTERVAL_S` seconds so the compressed
        incident is sampled often enough for every alert to confirm and clear.
        """
        interval = cfg.interval_for(interval_key)
        suite = self._demo_suite if cfg.demo else None
        if suite is not None and suite.world.fast and interval_key != "repository_interval":
            from dspace_sentinel.collectors.demo import FAST_INTERVAL_S

            interval = min(interval, FAST_INTERVAL_S)
        return interval

    def _build_groups(self, cfg: Config) -> dict[str, Group]:
        collectors = self._make_collectors(cfg)
        order = {cid: i for i, cid in enumerate(COLLECTOR_ORDER)}
        collectors.sort(key=lambda c: order.get(c.id, 99))
        groups: dict[str, Group] = {}
        for col in collectors:
            key = col.interval_key
            grp = groups.get(key)
            if grp is None:
                grp = Group(key=key, interval=self._group_interval(cfg, key), collectors=[])
                groups[key] = grp
            grp.collectors.append(col)
        self.collectors = collectors
        self.groups = groups
        return groups

    def _ensure_http(self) -> httpx.AsyncClient:
        if self.http is None:
            self.http = make_client(verify=self.cfg.dspace.verify_tls)
            self._own_http = True
        return self.http

    # ------------------------------------------------------------- lifecycle

    async def start(self, initial_cycle: bool = True) -> None:
        """Backfill demo data, restore persisted state, run one full cycle, then start the loops."""
        if self._running:
            return
        self._running = True
        self._ensure_http()
        suite = self._demo(self.cfg)
        if suite is not None:
            try:
                started = time.perf_counter()
                self.backfill_counts = await asyncio.to_thread(
                    suite.backfill, self.store, self.demo_backfill_days
                )
                if self.backfill_counts:
                    log.info(
                        "demo backfill done in %.2f s: %s",
                        time.perf_counter() - started,
                        self.backfill_counts,
                    )
            except Exception:
                log.exception("demo backfill failed")
        self._restore_state()
        if initial_cycle:
            try:
                await self._run_all_cycle()  # the start-up cycle is not a check-now: never coalesced
            except Exception:
                log.exception("initial cycle failed")
        now = time.time()
        for grp in self.groups.values():
            self._start_group(grp, first_delay=grp.interval if initial_cycle else 0.0, now=now)
        self._maintenance_task = asyncio.create_task(self._maintenance_loop(), name="sentinel-maintenance")

    async def stop(self) -> None:
        """Cancel every task, give queued notifications a moment, close the HTTP client (idempotent)."""
        self._running = False
        tasks = [g.task for g in self.groups.values() if g.task is not None]
        if self._maintenance_task is not None:
            tasks.append(self._maintenance_task)
        for extra in (self._check_now, self._report_task):
            if extra is not None and not extra.done():
                tasks.append(extra)
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        for grp in self.groups.values():
            grp.task = None
        self._maintenance_task = None
        self._report_task = None
        await self._close_outboxes()
        if self._own_http and self.http is not None:
            try:
                await self.http.aclose()
            except Exception:
                pass
            self.http = None

    async def _close_outboxes(self) -> None:
        """Let queued notifications go out (up to :data:`STOP_FLUSH_TIMEOUT_S`), then drop the rest
        and say how many were given up ("5 notifications not delivered")."""
        seen: set[int] = set()
        for engine in (self.alert_engine, self.incident_engine):
            outbox = getattr(engine, "outbox", None)
            if outbox is None or id(outbox) in seen:
                continue
            seen.add(id(outbox))
            try:
                if not await outbox.flush(STOP_FLUSH_TIMEOUT_S):
                    given_up = _undelivered(outbox)
                    if given_up:
                        noun = "notification" if given_up == 1 else "notifications"
                        log.warning("%d %s not delivered (Sentinel is stopping)", given_up, noun)
                outbox.close()
            except Exception:
                log.exception("could not close the notification outbox")

    @property
    def running(self) -> bool:
        """True between ``start()`` and ``stop()``."""
        return self._running

    def _restore_state(self) -> None:
        """Load persisted component states (so the UI is not empty after a restart) and seed sparklines."""
        try:
            enabled = {c.id for c in self.collectors}
            for cid, row in self.store.component_states().items():
                if cid not in enabled:
                    continue
                meta = health.meta_for(cid)
                comp = ComponentHealth(
                    id=cid,
                    label=meta.label,
                    emoji=meta.emoji,
                    status=row.get("status") or "unknown",
                    reason=row.get("reason"),
                    chain=[CheckResult.from_dict(c) for c in row.get("chain") or []],
                    metrics=row.get("metrics") or {},
                    depends_on=list(meta.depends_on),
                    latency_ms=row.get("latency_ms"),
                    since=row.get("since_ts"),
                    checked_at=row.get("checked_ts"),
                )
                self.state.set_component(comp)
            rng = self.store.sample_time_range()
            if rng is not None:
                series = self.store.series(
                    "cpu",
                    rng[1] - 12 * self.cfg.collector.system_interval,
                    rng[1],
                    bucket_s=self.cfg.collector.system_interval,
                )
                points = {"cpu": [p[1] for p in series["points"] if p[1] is not None]}
                for metric in ("mem", "disk", "load"):
                    s = self.store.series(
                        metric,
                        rng[1] - 12 * self.cfg.collector.system_interval,
                        rng[1],
                        bucket_s=self.cfg.collector.system_interval,
                    )
                    points[metric] = [p[1] for p in s["points"] if p[1] is not None]
                self.state.seed_sparks(points)
        except Exception:
            log.exception("could not restore persisted state")

    # ------------------------------------------------------------------ loops

    def _start_group(self, grp: Group, first_delay: float, now: float | None = None) -> None:
        now = now or time.time()
        grp.next_due = now + max(0.0, first_delay)
        grp.task = asyncio.create_task(self._group_loop(grp, first_delay), name=f"sentinel-{grp.key}")

    async def _group_loop(self, grp: Group, first_delay: float) -> None:
        try:
            if first_delay > 0:
                await asyncio.sleep(first_delay)
            while self._running:
                started = time.time()
                try:
                    await self.run_group(grp)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("group %s failed", grp.key)
                grp.next_due = started + grp.interval
                await asyncio.sleep(max(0.5, grp.next_due - time.time()))
        except asyncio.CancelledError:
            pass

    async def _maintenance_loop(self) -> None:
        try:
            await asyncio.sleep(MAINTENANCE_FIRST_DELAY_S)
            while self._running:
                await self.run_maintenance()
                await asyncio.sleep(MAINTENANCE_INTERVAL_S)
        except asyncio.CancelledError:
            pass

    async def run_maintenance(self) -> dict[str, Any]:
        """Hourly roll-up of raw samples into ``samples_hourly`` plus the retention purge.

        Runs from the maintenance task; safe to call manually.
        """
        out: dict[str, Any] = {}
        now = time.time()
        try:
            out["rolled_up"] = await asyncio.to_thread(self.store.rollup_hourly, now - 2 * 3600)
            out["purged"] = await asyncio.to_thread(self.store.purge, self.cfg.collector.retention_days, now)
            log.info("maintenance: %s", out)
        except Exception as exc:
            log.exception("maintenance failed")
            out["error"] = str(exc)
        return out

    # ------------------------------------------------------------------ cycles

    def _context(self, now: float) -> CollectorContext:
        return CollectorContext(
            cfg=self.cfg, http=self._ensure_http(), store=self.store, state=self.state, now=now
        )

    async def _collect_one(self, col: Collector, now: float) -> CollectorResult:
        """Run one collector under its timeout; exceptions become an ``unknown`` error rung."""
        timeout = col.timeout(self.cfg)
        try:
            result = await asyncio.wait_for(col.collect(self._context(now)), timeout=timeout)
            if not isinstance(result, CollectorResult):
                raise TypeError(f"{col.__class__.__name__}.collect returned {type(result).__name__}")
            return result
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            message = f"check timed out after {timeout:.0f} s"
        except Exception as exc:
            message = f"{exc.__class__.__name__}: {exc}" if str(exc) else exc.__class__.__name__
            log.warning("collector %s failed: %s", col.id, message)
        self.state.last_error = f"{col.id}: {message}"
        return CollectorResult(
            checks=[
                CheckResult(
                    id=f"{col.id}.error",
                    label=f"{col.label or col.id} check",
                    status="unknown",
                    ts=now,
                    message=message,
                    # nothing was measured: the alert rules treat this cycle as neutral
                    detail={"collector_error": True},
                )
            ]
        )

    def _process(self, col: Collector, result: CollectorResult, now: float) -> ComponentHealth | None:
        """Persist a collector result, update state and emit change events. Returns the new health.

        A result whose module was switched off while it was being collected (a *Check now*
        waiting on a timeout when the operator flips the toggle) is dropped: storing it would
        bring the component back as a ghost that nothing removes again. Returns ``None`` then.
        """
        if col.id not in {c.id for c in self.collectors}:
            log.info("dropping the %s result: its module was switched off during the check", col.id)
            return None
        previous = self.state.components.get(col.id)
        if result.sample is not None:
            self.state.set_sample(result.sample)
            self.store.insert_sample(result.sample)
            comp = health.server_health(result.sample, self.cfg, previous, now)
            if result.checks:
                comp.chain = list(result.checks)
        else:
            comp = health.rollup(
                col.id,
                result.checks,
                result.metrics,
                previous,
                now,
                label=col.label or None,
                emoji=col.emoji or None,
            )
            if result.checks:
                self.store.insert_checks(result.checks)
        self.state.set_component(comp)
        self.store.upsert_component_state(comp)
        self.store.insert_component_history(now, comp.id, comp.status, comp.latency_ms)
        if result.repository_stats:
            self.store.insert_repository_stats(now, result.repository_stats)
        if result.history:
            self.store.insert_checks(result.history)
        if previous is None or previous.status != comp.status:
            old = previous.status if previous is not None else None
            # worded like the UI: "Solr: Healthy → Warning — search took 1.7 s"
            change = health.status_label(comp.status)
            if old is not None:
                change = f"{health.status_label(old)} → {change}"
            text = f"{comp.label}: {change}"
            if comp.reason and comp.status != "ok":
                text += f" — {comp.reason}"
            if previous is not None or comp.status != "ok":
                self.store.add_event("component", comp.id, text, {"from": old, "to": comp.status}, ts=now)
            self.bus.publish(
                "component", {"id": comp.id, "from": old, "to": comp.status, "component": comp.to_dict()}
            )
        return comp

    async def _after_cycle(self, now: float) -> None:
        """Alert / incident / report hooks, each guarded."""
        engine = self.alert_engine
        if engine is not None and hasattr(engine, "evaluate"):
            try:
                await engine.evaluate(self.state.sample, dict(self.state.components), now)
            except Exception:
                self._hook_failed("alert_engine.evaluate")
        incidents = self.incident_engine
        if incidents is not None and hasattr(incidents, "check_resolution"):
            try:
                await incidents.check_resolution(now)
            except Exception:
                self._hook_failed("incident_engine.check_resolution")
        self._start_report_hook(now)
        self.state.last_cycle_ts = now
        self.state.cycle_count += 1
        # The SSE stream throttles "status" to one every STATUS_EVENT_MIN_INTERVAL_S; publish every tick.
        self._last_status_event = now
        self.bus.publish("status", {"ts": now})

    def _start_report_hook(self, now: float) -> None:
        """Run the weekly-report hook in the background, one run at a time, outside the cycle lock."""
        running = self._report_task
        if running is not None and not running.done():
            return  # still generating (or sending) the previous one
        self._report_task = asyncio.create_task(self._report_hook(now), name="sentinel-reports")

    async def _report_hook(self, now: float) -> None:
        try:
            from dspace_sentinel.reports import schedule as report_schedule

            await report_schedule.maybe_run(self.store, self.cfg, now, notifier=self._hooks.notifier)
        except Exception:
            self._hook_failed("reports.schedule.maybe_run")

    def _hook_failed(self, name: str) -> None:
        count = self._hooks.errors.get(name, 0) + 1
        self._hooks.errors[name] = count
        if count <= 3 or count % 100 == 0:
            log.exception("%s failed (%d times)", name, count)

    async def run_group(self, grp: Group) -> None:
        """Run every collector of one group (sequentially, in dependency order) and the hooks."""
        async with self._cycle_lock:
            now = time.time()
            for col in grp.collectors:
                result = await self._collect_one(col, now)
                try:
                    self._process(col, result, now)
                except Exception:
                    log.exception("processing %s failed", col.id)
            grp.last_run = now
            grp.run_count += 1
            await self._after_cycle(now)

    async def run_all_now(self) -> bool:
        """Run every enabled collector immediately (``POST /api/actions/check-now``, ``doctor``).

        While the service runs, requests are coalesced: a call made while a check-now is in
        flight waits for that one instead of starting another, and a call within
        :data:`CHECK_NOW_MIN_INTERVAL_S` of the previous check-now returns at once (a config
        reload or cleared history always allows a fresh run). One-shot callers such as
        ``doctor`` never started the scheduler and always get a cycle. Groups in
        :data:`OWN_INTERVAL_ONLY` only run when their own interval is due. Returns ``True`` when
        this call ran a cycle.
        """
        if not self._running:
            await self._run_all_cycle()
            return True
        inflight = self._check_now
        if inflight is not None and not inflight.done():
            try:
                await asyncio.shield(inflight)
            except Exception:
                pass  # the caller that started it reports the failure
            return False
        last = self._last_check_now
        if (
            last is not None
            and self.state.last_cycle_ts is not None
            and time.time() - last < CHECK_NOW_MIN_INTERVAL_S
        ):
            return False
        task = asyncio.create_task(self._run_all_cycle(), name="sentinel-check-now")
        self._check_now = task
        task.add_done_callback(self._check_now_done)
        await asyncio.shield(task)  # a caller that goes away does not abort the shared check
        return True

    def _check_now_done(self, task: asyncio.Task) -> None:
        if not task.cancelled():
            self._last_check_now = time.time()

    def _due_on_demand(self, grp: Group, now: float) -> bool:
        """False for an :data:`OWN_INTERVAL_ONLY` group that ran less than one interval ago."""
        if grp.key not in OWN_INTERVAL_ONLY or grp.last_run is None:
            return True
        return now - grp.last_run >= grp.interval

    async def _run_all_cycle(self) -> None:
        """One full cycle: every due collector, in order, then the hooks."""
        self.state.checking = True
        try:
            async with self._cycle_lock:
                now = time.time()
                groups = [grp for grp in self.groups.values() if self._due_on_demand(grp, now)]
                due = {grp.key for grp in groups}
                for col in list(self.collectors):
                    if col.interval_key not in due:
                        continue
                    result = await self._collect_one(col, now)
                    try:
                        self._process(col, result, now)
                    except Exception:
                        log.exception("processing %s failed", col.id)
                for grp in groups:
                    grp.last_run = now
                    grp.run_count += 1
                    if grp.task is not None:
                        grp.next_due = max(grp.next_due, now + 1.0)
                await self._after_cycle(now)
        finally:
            self.state.checking = False

    # ---------------------------------------------------------------- reload

    def reload(self, cfg: Config) -> None:
        """Hot-apply a new config: modules, intervals, thresholds, TLS verification.

        Groups whose collector set or interval changed are restarted; a group that gained a
        collector runs immediately so a freshly enabled module shows data at once. Components
        of modules switched off vanish from state and ``component_state`` (history is kept).
        The caller logs the change (``PATCH /api/modules`` writes "Solr turned on").
        """
        old_groups = self.groups
        old_ids = {c.id for c in self.collectors}
        verify_changed = cfg.dspace.verify_tls != self.cfg.dspace.verify_tls
        self.cfg = cfg
        self._last_check_now = None  # the next check-now runs against the new configuration
        self._build_groups(cfg)
        new_ids = {c.id for c in self.collectors}
        # The caller records the config event ("Solr turned on"); nothing is logged here twice.
        for removed in old_ids - new_ids:
            self.state.remove_component(removed)
            try:
                self.store.delete_component_state(removed)
            except Exception:
                log.exception("could not drop state of %s", removed)
        for hook in (self.alert_engine, self.incident_engine):
            if hook is not None and hasattr(hook, "reload"):
                try:
                    hook.reload(cfg)
                except Exception:
                    log.exception("%s.reload failed", hook.__class__.__name__)
        if verify_changed and self._own_http and self.http is not None:
            old_http = self.http
            self.http = None
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(old_http.aclose())
            except RuntimeError:
                pass
        if not self._running:
            return
        now = time.time()
        for key, old in old_groups.items():
            new = self.groups.get(key)
            if new is None or new.signature() != old.signature():
                if old.task is not None:
                    old.task.cancel()
                    old.task = None
        for key, grp in list(self.groups.items()):
            old = old_groups.get(key)
            if old is not None and grp.signature() == old.signature():
                # Keep the Group object its running task updates (next_due, last_run, run_count)
                # and hand it the new collector instances, so there is one set of each.
                old.collectors = grp.collectors
                self.groups[key] = old
                continue
            gained = old is None or set(grp.ids) - set(old.ids)
            if gained:
                delay = 0.0
            else:
                remaining = (old.next_due - now) if old is not None else 0.0
                delay = max(0.0, min(remaining, grp.interval))
            self._start_group(grp, first_delay=delay, now=now)
        self.bus.publish("status", {"ts": now, "reason": "reload"})

    # ------------------------------------------------------------------ misc

    def next_check_in(self, now: float | None = None) -> float:
        """Seconds until the next scheduled group run (0 when a run is due or in flight)."""
        now = now or time.time()
        dues = [g.next_due for g in self.groups.values() if g.task is not None]
        if not dues:
            return float(self.cfg.collector.system_interval)
        return round(max(0.0, min(dues) - now), 1)

    def enabled_component_ids(self) -> list[str]:
        """Component ids currently collected (server first)."""
        return [c.id for c in self.collectors]

    def describe(self) -> dict[str, Any]:
        """Diagnostics: groups, intervals, run counts, hook errors."""
        return {
            "running": self._running,
            "groups": {
                key: {
                    "interval": g.interval,
                    "collectors": list(g.ids),
                    "runs": g.run_count,
                    "last_run": g.last_run,
                    "next_due": g.next_due,
                }
                for key, g in self.groups.items()
            },
            "hook_errors": dict(self._hooks.errors),
            "backfill": self.backfill_counts,
        }


__all__ = [
    "CHECK_NOW_MIN_INTERVAL_S",
    "Group",
    "MAINTENANCE_INTERVAL_S",
    "OWN_INTERVAL_ONLY",
    "STATUS_EVENT_MIN_INTERVAL_S",
    "STOP_FLUSH_TIMEOUT_S",
    "Scheduler",
]
