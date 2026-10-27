"""Assetstore collector: is the bitstream directory readable, and how full is its disk (SPEC section 6).

One rung, ``assetstore.path`` "Assetstore readable", graded by free space against
``alerts.assetstore_free_gb`` (inverted thresholds). Growth per day comes from history:
Sentinel keeps a small ``[ts, used_bytes]`` series in the ``kv`` table (one point per hour,
30 days) and, until that spans an hour, falls back to the bitstream byte totals recorded by
the repository collector.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, ClassVar

from dspace_sentinel.collectors.base import (
    Collector,
    CollectorContext,
    CollectorResult,
    ProbeStalled,
    run_blocking,
)
from dspace_sentinel.collectors.services import rung
from dspace_sentinel.collectors.system import mount_point
from dspace_sentinel.health import threshold_status

log = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 10.0
"""A ``stat``/``statvfs`` of the assetstore taking longer means its filesystem is hung (dead NFS)."""

GIB = 1024**3
HISTORY_KEY = "assetstore.usage_history"
HISTORY_MIN_GAP_S = 3600
HISTORY_MAX_AGE_S = 30 * 86400
GROWTH_MIN_SPAN_S = 3600
FALLBACK_WINDOW_S = 7 * 86400


def disk_usage(path: str) -> dict[str, Any]:
    """``os.statvfs`` of ``path`` → ``{total, used, free, pct}`` (free = what non-root can use, like df)."""
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    used = (st.f_blocks - st.f_bfree) * st.f_frsize
    denom = used + free
    pct = round(100.0 * used / denom, 2) if denom > 0 else 0.0
    return {"total": int(total), "used": int(used), "free": int(free), "pct": pct}


def inspect_path(path: str) -> dict[str, Any]:
    """Blocking look at the assetstore directory: existence, readability, usage, mount point."""
    out: dict[str, Any] = {"exists": os.path.isdir(path), "readable": False, "usage": None, "mount": None}
    if not out["exists"]:
        return out
    out["readable"] = os.access(path, os.R_OK | os.X_OK)
    try:
        out["usage"] = disk_usage(path)
    except OSError as exc:
        out["error"] = (exc.strerror or str(exc)).lower()
    out["mount"] = mount_point(path)
    return out


def growth_per_day(history: list[list[float]], min_span_s: float = GROWTH_MIN_SPAN_S) -> float | None:
    """Bytes/day from a ``[[ts, used], ...]`` series (least squares; ``None`` below ``min_span_s``)."""
    points = [(float(t), float(u)) for t, u in history if t is not None and u is not None]
    if len(points) < 2:
        return None
    points.sort()
    span = points[-1][0] - points[0][0]
    if span < min_span_s:
        return None
    if len(points) == 2:
        return (points[1][1] - points[0][1]) / span * 86400.0
    n = len(points)
    mean_t = sum(t for t, _ in points) / n
    mean_u = sum(u for _, u in points) / n
    var = sum((t - mean_t) ** 2 for t, _ in points)
    if var == 0:
        return None
    slope = sum((t - mean_t) * (u - mean_u) for t, u in points) / var
    return slope * 86400.0


def load_history(store: Any) -> list[list[float]]:
    """The persisted usage series (``[]`` when absent or unreadable)."""
    try:
        raw = store.kv_get(HISTORY_KEY)
    except Exception:
        return []
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    return [[float(p[0]), float(p[1])] for p in data if isinstance(p, list | tuple) and len(p) == 2]


def record_usage(store: Any, now: float, used: int) -> list[list[float]]:
    """Append ``[now, used]`` (at most once an hour), drop points older than 30 days, persist."""
    history = load_history(store)
    if not history or now - history[-1][0] >= HISTORY_MIN_GAP_S:
        history.append([float(int(now)), float(used)])
    history = [p for p in history if now - p[0] <= HISTORY_MAX_AGE_S]
    try:
        store.kv_set(HISTORY_KEY, json.dumps(history))
    except Exception as exc:
        log.debug("could not persist assetstore history: %s", exc)
    return history


class AssetstoreCollector(Collector):
    """``assetstore`` 🗄️: path, mount, total/used/free, growth per day and days until full."""

    id: ClassVar[str] = "assetstore"
    interval_key: ClassVar[str] = "service_interval"
    label: ClassVar[str] = "Assetstore"
    emoji: ClassVar[str] = "🗄️"

    async def collect(self, ctx: CollectorContext) -> CollectorResult:
        """Inspect ``dspace.assetstore_dir`` in a thread and grade free space."""
        ts = ctx.now
        path = ctx.cfg.dspace.assetstore_dir
        rung_id, label = "assetstore.path", "Assetstore readable"
        try:
            # one stuck probe at a time: a dead NFS mount must not pile up blocked threads
            info = await run_blocking(f"assetstore:{path}", inspect_path, path, timeout=PROBE_TIMEOUT_S)
        except (ProbeStalled, asyncio.TimeoutError):
            info = None
        metrics: dict[str, Any] = {
            "path": path,
            "mount": info.get("mount") if info is not None else None,
            "total": None,
            "used": None,
            "free": None,
            "pct": None,
            "growth_bytes_per_day": None,
            "days_until_full": None,
        }
        if info is None:
            check = rung(rung_id, label, "crit", ts, message=f"filesystem not responding ({path})", path=path)
            return CollectorResult(checks=[check], metrics=metrics)
        if not info["exists"]:
            check = rung(rung_id, label, "crit", ts, message=f"directory not found ({path})", path=path)
            return CollectorResult(checks=[check], metrics=metrics)
        usage = info.get("usage")
        if usage is None:
            message = f"{info.get('error') or 'not accessible'} ({path})"
            check = rung(rung_id, label, "warn", ts, message=message, path=path)
            return CollectorResult(checks=[check], metrics=metrics)

        metrics.update(usage)
        free_gb = usage["free"] / GIB
        th = ctx.cfg.alerts.assetstore_free_gb
        status = threshold_status(free_gb, th.warning, th.critical, inverted=True)
        message = "" if status == "ok" else f"only {free_gb:.1f} GB free"
        if not info["readable"]:
            if status == "ok":
                status = "warn"
            message = f"not readable by Sentinel (permission denied){' · ' + message if message else ''}"

        started = time.perf_counter()
        history = await asyncio.to_thread(record_usage, ctx.store, ts, int(usage["used"]))
        growth = growth_per_day(history)
        growth_source = "mount" if growth is not None else None
        if growth is None:
            try:
                delta = await asyncio.to_thread(ctx.store.repository_delta, ts - FALLBACK_WINDOW_S, ts)
            except Exception:
                delta = {}
            days = float(delta.get("days") or 0.0)
            grown = delta.get("assetstore_bytes")
            if grown is not None and days >= 1.0:
                growth = float(grown) / days
                growth_source = "repository_stats"
        if growth is not None:
            metrics["growth_bytes_per_day"] = int(round(growth))
            metrics["growth_source"] = growth_source
            if growth > 0:
                metrics["days_until_full"] = round(usage["free"] / growth, 1)
        metrics["history_points"] = len(history)
        check = rung(
            rung_id,
            label,
            status,
            ts,
            latency_ms=round((time.perf_counter() - started) * 1000.0, 2),
            value=round(free_gb, 1),
            message=message,
            path=path,
            mount=info.get("mount"),
            readable=info["readable"],
        )
        return CollectorResult(checks=[check], metrics=metrics)


__all__ = [
    "FALLBACK_WINDOW_S",
    "GROWTH_MIN_SPAN_S",
    "HISTORY_KEY",
    "HISTORY_MAX_AGE_S",
    "HISTORY_MIN_GAP_S",
    "PROBE_TIMEOUT_S",
    "AssetstoreCollector",
    "disk_usage",
    "growth_per_day",
    "inspect_path",
    "load_history",
    "record_usage",
]
