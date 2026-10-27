"""History endpoints.

``/api/history``, ``/api/history/latency``, ``/api/availability``, ``/api/repository``,
``/api/events`` and ``/api/export``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from dspace_sentinel import health
from dspace_sentinel.api.deps import AppContext, get_ctx, range_bounds
from dspace_sentinel.config import Config
from dspace_sentinel.store import METRIC_UNITS, bucket_for_range

router = APIRouter(prefix="/api", tags=["history"])

LATENCY_CHECKS: dict[str, str] = {
    "backend.rest": "rest_latency_ms",
    "backend.search": "search_latency_ms",
    "frontend.home": "home_latency_ms",
    "postgres.query": "",
    "solr.search": "search_latency_ms",
    "nginx.http": "",
}
"""Latency checks the history endpoint accepts → the alerts threshold key that applies ("" = none)."""

THRESHOLD_KEYS: dict[str, str] = {
    "cpu": "cpu",
    "mem": "memory",
    "disk": "disk",
    "swap": "swap",
    "load": "load_per_core",
}

AVAILABILITY_BUCKETS: dict[str, int] = {"1h": 60, "6h": 72, "24h": 96, "7d": 84, "30d": 60}


def metric_thresholds(cfg: Config, metric: str, cores: int | None) -> dict[str, float] | None:
    """Warning/critical thresholds for a history metric (load thresholds are scaled by ``cores``)."""
    key = THRESHOLD_KEYS.get(metric)
    if key is None:
        return None
    th = getattr(cfg.alerts, key)
    if metric == "load":
        n = max(1, int(cores or 1))
        return {"warning": th.warning * n, "critical": th.critical * n, "per_core": True, "cores": n}
    return {"warning": th.warning, "critical": th.critical}


@router.get("/history")
async def get_history(
    metric: str = Query("cpu"),
    range: str = Query("24h"),  # noqa: A002 - query parameter name fixed by the SPEC
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Bucketed server metric history: ``{metric, unit, bucket_s, points: [[ts, avg, max]], thresholds}``."""
    if metric not in METRIC_UNITS:
        raise HTTPException(
            status_code=400, detail=f"unknown metric {metric!r}; use one of {', '.join(METRIC_UNITS)}"
        )
    t_from, t_to, label = range_bounds(range)
    data = await asyncio.to_thread(ctx.store.series, metric, t_from, t_to)
    cores = ctx.state.sample.cores if ctx.state.sample else None
    data["thresholds"] = metric_thresholds(ctx.cfg, metric, cores)
    data["range"] = label
    data["from"] = t_from
    data["to"] = t_to
    return data


@router.get("/history/latency")
async def get_latency_history(
    check: str = Query("backend.rest"),
    range: str = Query("24h"),  # noqa: A002
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Bucketed latency of one check: ``{check, points: [[ts, avg_ms, max_ms]], thresholds}``."""
    if check not in LATENCY_CHECKS and not check.startswith(
        ("backend.", "frontend.", "postgres.", "solr.", "nginx.")
    ):
        raise HTTPException(
            status_code=400, detail=f"unknown check {check!r}; use one of {', '.join(LATENCY_CHECKS)}"
        )
    t_from, t_to, label = range_bounds(range)
    data = await asyncio.to_thread(ctx.store.latency_series, check, t_from, t_to)
    key = LATENCY_CHECKS.get(check, "")
    thresholds = None
    if key:
        th = getattr(ctx.cfg.alerts, key)
        thresholds = {"warning": th.warning, "critical": th.critical}
    data.update({"thresholds": thresholds, "unit": "ms", "range": label, "from": t_from, "to": t_to})
    return data


@router.get("/availability")
async def get_availability(
    range: str = Query("24h"),  # noqa: A002
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Availability strips for every enabled component (server included)."""
    t_from, t_to, label = range_bounds(range)
    buckets = AVAILABILITY_BUCKETS.get(label, 96)
    ids = ctx.scheduler.enabled_component_ids()
    # how often each component is checked: a check stands until the next one (Repository: 15 min)
    intervals: dict[str, float] = {}
    for col in getattr(ctx.scheduler, "collectors", ()):
        try:
            intervals[col.id] = float(col.interval(ctx.cfg))
        except Exception:  # the rows' own spacing decides
            continue
    components = []
    for cid in ids:
        meta = health.meta_for(cid)
        data = await asyncio.to_thread(ctx.store.availability, cid, t_from, t_to, buckets, intervals.get(cid))
        components.append(
            {
                "id": cid,
                "label": meta.label,
                "emoji": meta.emoji,
                "uptime_pct": data["uptime_pct"],
                "downtime_s": data["downtime_s"],
                "outages": data["outages"],
                "samples": data["samples"],
                "buckets": data["buckets"],
            }
        )
    bucket_s = int(max(1.0, (t_to - t_from) / buckets))
    return {"bucket_s": bucket_s, "range": label, "from": t_from, "to": t_to, "components": components}


@router.get("/repository")
async def get_repository(ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """Latest repository counters plus 30-day series and the 30-day delta."""
    now = time.time()
    t_from = now - 30 * 86400
    latest = await asyncio.to_thread(ctx.store.latest_repository_stats)
    series = await asyncio.to_thread(ctx.store.repository_series, t_from, now, 3600)
    delta = await asyncio.to_thread(ctx.store.repository_delta, t_from, now)
    growth: dict[str, float | None] = {}
    days = delta.get("days") or 0
    for key in ("items", "bitstreams", "assetstore_bytes", "db_size"):
        value = delta.get(key)
        growth[key] = round(value / days, 2) if (value is not None and days >= 0.5) else None
    return {
        "enabled": ctx.cfg.module_enabled("repository"),
        "latest": latest,
        "series": series,
        "delta": delta,
        "growth_per_day": growth,
        "from": t_from,
        "to": now,
    }


@router.get("/events")
async def get_events(
    limit: int = Query(100, ge=1, le=1000),
    component: str | None = Query(None),
    kind: str | None = Query(None),
    ctx: AppContext = Depends(get_ctx),
) -> list[dict[str, Any]]:
    """Recent audit events, newest first."""
    events = await asyncio.to_thread(ctx.store.events, limit, component, None, kind)
    return [e.to_dict() for e in events]


class ExportResponse(StreamingResponse):
    """A streamed export that frees the single-export slot however the response ends."""

    def __init__(self, chunks: Iterable[bytes], release: Callable[[], None], **kwargs: Any) -> None:
        super().__init__(chunks, **kwargs)
        self._chunks = chunks
        self._release = release

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._release()
            close = getattr(self._chunks, "close", None)
            if close is not None:
                try:
                    close()  # closes the export's read-only database connection
                except ValueError:  # still inside a worker thread; it closes when collected
                    pass


@router.get("/export")
async def get_export(
    range: str = Query("30d"),  # noqa: A002
    ctx: AppContext = Depends(get_ctx),
) -> Any:
    """JSON document with every sample, check, component history row, alert, incident, repository
    stat and event of ``range`` (default 30 days).

    Streamed table by table from a worker thread (flat memory, the event loop stays free); one
    export at a time, a second request meanwhile answers 429.
    """
    t_from, t_to, label = range_bounds(range, default="30d")
    flight = ctx.export_flight
    token = flight.try_start()
    if token is None:
        raise HTTPException(
            status_code=429,
            detail="an export is already running; try again when it has finished",
            headers={"Retry-After": "30"},
        )
    meta = {
        "hostname": ctx.cfg.hostname,
        "exported_ts": t_to,
        "range": label,
        "bucket_hint_s": bucket_for_range(t_to - t_from),
        "demo": bool(ctx.cfg.demo),
    }
    chunks = ctx.store.export(t_from, t_to, stream=True, meta=meta)
    return ExportResponse(
        chunks,  # type: ignore[arg-type]
        lambda: flight.finish(token),
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


__all__ = [
    "AVAILABILITY_BUCKETS",
    "LATENCY_CHECKS",
    "THRESHOLD_KEYS",
    "ExportResponse",
    "metric_thresholds",
    "router",
]
