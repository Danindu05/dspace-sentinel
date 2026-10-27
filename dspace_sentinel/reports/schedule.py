"""Weekly report schedule (SPEC section 8) and the housekeeping of soft-deleted reports.

The scheduler calls ``await maybe_run(store, cfg, now, notifier)`` after every collector
cycle. When ``reports.weekly.enabled`` is on, a 7-day HTML report is generated once on the
configured weekday at (or after) the configured hour, both in the zone of ``server.timezone``
(the server's zone when empty); a run missed because Sentinel was down
still happens later the same day, never on another day. Delivery follows
``reports.weekly.send_telegram`` (every Telegram bot) and ``reports.weekly.send_email`` (every
email recipient); each outcome is recorded as a ``notify`` (sent), ``notify.error`` (not sent)
or ``notify.demo`` (demo mode, nothing sent) event. Nothing here ever raises into the scheduler.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from dspace_sentinel.store import REPORT_PURGE_AFTER_S
from dspace_sentinel.util import fmt
from dspace_sentinel.util.tz import wall, zone_of

if TYPE_CHECKING:  # pragma: no cover
    from dspace_sentinel.config import Config
    from dspace_sentinel.models import Report
    from dspace_sentinel.store import Store

log = logging.getLogger(__name__)

WEEKLY_KV_KEY = "weekly_report_last_day"
"""``kv`` key holding the date (``YYYY-MM-DD``, in ``server.timezone``) of the last weekly report produced."""

ATTEMPT_KV_KEY = "weekly_report_last_attempt_ts"
"""``kv`` key holding the epoch of the last failed attempt (retried after :data:`RETRY_S`)."""

RETRY_S = 1800
"""Seconds between retries after a failed weekly report."""

PURGE_INTERVAL_S = 600
"""How often soft-deleted reports older than 24 h are purged from this hook."""

_last_purge_ts: float = 0.0


def weekly_due(cfg: Config, now: float, last_day: str | None) -> str | None:
    """Return the due-day key (``YYYY-MM-DD``) when a weekly report should run now.

    ``reports.weekly.weekday`` follows the config convention 1 = Monday … 7 = Sunday (0 is also
    Sunday). The report is due from ``hour:00`` for the rest of that day, unless ``last_day``
    already equals the day. Day and hour are read in the zone of ``server.timezone``.
    """
    weekly = cfg.reports.weekly
    if not weekly.enabled:
        return None
    local = wall(now, zone_of(cfg))
    weekday = int(weekly.weekday) or 7
    if local.isoweekday() != weekday:
        return None
    if local.hour < int(weekly.hour):
        return None
    key = local.strftime("%Y-%m-%d")
    if last_day == key:
        return None
    return key


def _purge_deleted(store: Store, now: float) -> None:
    global _last_purge_ts
    if now - _last_purge_ts < PURGE_INTERVAL_S:
        return
    _last_purge_ts = now
    try:
        removed = store.purge_reports(now - REPORT_PURGE_AFTER_S)
        if removed:
            log.info("purged %d soft-deleted report(s)", removed)
    except Exception:
        log.exception("report purge failed")


async def maybe_run(store: Store, cfg: Config, now: float, notifier: Any = None) -> Report | None:
    """Generate the weekly report when it is due; returns the :class:`Report` produced, else ``None``.

    Also purges reports soft-deleted more than 24 h ago (at most every ten minutes). Never raises.
    """
    now = float(now or time.time())
    _purge_deleted(store, now)
    try:
        due_key = weekly_due(cfg, now, store.kv_get(WEEKLY_KV_KEY))
    except Exception:
        log.exception("weekly report schedule check failed")
        return None
    if due_key is None:
        return None
    try:
        last_attempt = float(store.kv_get(ATTEMPT_KV_KEY) or 0.0)
    except (TypeError, ValueError):
        last_attempt = 0.0
    if now - last_attempt < RETRY_S:
        return None
    store.kv_set(ATTEMPT_KV_KEY, str(int(now)))
    from dspace_sentinel.reports import generator

    weekly = cfg.reports.weekly
    title = f"Weekly report · {cfg.hostname} · week ending {fmt.day(now, zone_of(cfg))}"
    try:
        report = await asyncio.to_thread(
            generator.generate,
            store,
            cfg,
            now - 7 * 86400,
            now,
            "html",
            title,
            None,
            False,
            now=now,
            range_hint="7d",
        )
    except Exception:
        log.exception("weekly report generation failed")
        try:
            store.add_event("report", None, "Weekly report failed; will retry in 30 minutes", {}, ts=now)
        except Exception:
            pass
        return None
    store.kv_set(WEEKLY_KV_KEY, due_key)
    store.kv_delete(ATTEMPT_KV_KEY)
    log.info("weekly report generated: %s", report.path)
    if weekly.send_telegram:
        try:
            delivery = await generator.deliver_telegram_async(cfg, report, store=store, notifier=notifier)
            if not delivery.get("ok"):
                log.warning("weekly report not sent to Telegram: %s", delivery.get("error"))
        except Exception:
            log.exception("weekly report Telegram delivery failed")
    if weekly.send_email:
        try:
            delivery = await generator.deliver_email_async(cfg, report, store=store)
            if not delivery.get("ok"):
                log.warning("weekly report not sent by email: %s", delivery.get("error"))
        except Exception:
            log.exception("weekly report email delivery failed")
    return report


__all__ = ["ATTEMPT_KV_KEY", "PURGE_INTERVAL_S", "RETRY_S", "WEEKLY_KV_KEY", "maybe_run", "weekly_due"]
