"""``/api/meta``, ``/api/status``, ``/api/system``, ``/api/components``."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException

from dspace_sentinel import __version__, health
from dspace_sentinel.api.deps import AppContext, get_ctx
from dspace_sentinel.config import Config, dspace_modules_enabled, masked
from dspace_sentinel.models import ComponentHealth
from dspace_sentinel.util import tz

if TYPE_CHECKING:  # pragma: no cover
    from dspace_sentinel.scheduler import Scheduler
    from dspace_sentinel.state import State
    from dspace_sentinel.store import Store

router = APIRouter(prefix="/api", tags=["status"])

_ORDER = {cid: i for i, cid in enumerate(health.COMPONENT_ORDER)}

CONFIG_SLICES: dict[str, tuple[str, ...]] = {
    "server": ("server", "collector"),
    "postgres": ("postgres",),
    "solr": ("solr",),
    "backend": ("dspace",),
    "frontend": ("dspace",),
    "nginx": ("nginx",),
    "ssl": ("ssl", "dspace"),
    "ports": ("ports",),
    "security": ("ports", "dspace"),
    "logs": ("logs",),
    "repository": ("postgres", "dspace"),
    "assetstore": ("dspace",),
}


def ordered_components(state: State, include_server: bool = True) -> list[ComponentHealth]:
    """Current components in display order (server first, then the SPEC table order)."""
    comps = [c for c in state.components.values() if include_server or c.id != "server"]
    comps.sort(key=lambda c: _ORDER.get(c.id, 99))
    return comps


def build_meta(cfg: Config, started_ts: float) -> dict[str, Any]:
    """The ``/api/meta`` payload; ``timezone`` is :func:`util.tz.describe` (the zone of the times
    Sentinel sends, and the server's own)."""
    return {
        "version": __version__,
        "hostname": cfg.hostname,
        "setup_completed": cfg.setup.completed,
        "persona": cfg.setup.persona,
        "demo": bool(cfg.demo),
        "modules": cfg.modules.as_dict(),
        "dspace_modules_enabled": dspace_modules_enabled(cfg),
        "started_ts": started_ts,
        "theme": cfg.appearance.theme,
        "retention_days": cfg.collector.retention_days,
        "system_interval": cfg.collector.system_interval,
        "timezone": tz.describe(cfg),
    }


def build_status(
    cfg: Config,
    state: State,
    store: Store,
    scheduler: Scheduler | None = None,
    alert_engine: Any = None,
    now: float | None = None,
) -> dict[str, Any]:
    """The ``/api/status`` payload.

    Also sent as the SSE ``status`` event and printed by ``doctor --json``.
    """
    now = now or time.time()
    checked_ago = state.checked_ago_s(now)
    sample = state.sample
    server_comp = state.components.get("server") or health.server_health(sample, cfg, None, now)
    server: dict[str, Any] = sample.to_dict() if sample is not None else {}
    server.update(
        {
            "status": server_comp.status,
            "reason": server_comp.reason,
            "since": server_comp.since,
            "spark": state.spark_dict(),
            "per_metric": server_comp.metrics.get("per_metric", {}),
        }
    )
    if alert_engine is not None and hasattr(alert_engine, "open_alerts"):
        try:
            open_alerts = list(alert_engine.open_alerts())
        except Exception:
            open_alerts = list(store.open_alerts())
    else:
        open_alerts = list(store.open_alerts())
    alerts_open = [a.to_dict() for a in open_alerts]
    # The mood and headline take the open alerts into account: an alert may outlive the
    # condition that opened it (clear_samples hysteresis), and the band must not read
    # "All systems healthy" while it is open. An acknowledged alert no longer counts, and a
    # component whose open alerts are all acknowledged counts as healthy there ("All systems
    # healthy · 1 acknowledged"); the components keep their own status below.
    mood_value, headline = health.headline(state, cfg, open_alerts)
    repository = None
    if cfg.module_enabled("repository"):
        try:
            repository = store.latest_repository_stats()
        except Exception:
            repository = None
    return {
        "ts": now,
        "mood": mood_value,
        "headline": headline,
        "subline": health.subline(cfg.hostname, checked_ago),
        "hostname": cfg.hostname,
        "demo": bool(cfg.demo),
        "checking": state.checking,
        "checked_at": state.last_cycle_ts,
        "checked_ago_s": None if checked_ago is None else round(checked_ago, 1),
        "server": server,
        "components": [c.to_dict() for c in ordered_components(state, include_server=False)],
        "alerts_open": alerts_open,
        "repository": repository,
        "next_check_in_s": scheduler.next_check_in(now) if scheduler is not None else None,
        "modules": cfg.modules.as_dict(),
        "version": __version__,
    }


@router.get("/meta")
async def get_meta(ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """Version, hostname, onboarding state, modules, theme (plus the SQLite size for About)."""
    data = build_meta(ctx.cfg, ctx.started_ts)
    try:
        data["db_size_bytes"] = ctx.store.db_size_bytes()
    except Exception:  # the About page simply omits the row
        data["db_size_bytes"] = None
    return data


@router.get("/status")
async def get_status(ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """Everything the Home tab needs in one call."""
    return build_status(ctx.cfg, ctx.state, ctx.store, ctx.scheduler, ctx.alert_engine)


@router.get("/system")
async def get_system(ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """The latest system sample with server status and sparklines."""
    status = build_status(ctx.cfg, ctx.state, ctx.store, ctx.scheduler, ctx.alert_engine)
    return {"ts": status["ts"], "server": status["server"], "hostname": status["hostname"]}


@router.get("/components")
async def list_components(ctx: AppContext = Depends(get_ctx)) -> list[dict[str, Any]]:
    """Every current component (server included), display order."""
    return [c.to_dict() for c in ordered_components(ctx.state)]


@router.get("/components/{component_id}")
async def get_component(component_id: str, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """One component plus ``latency_series_1h``, ``recent_events`` and the masked ``config_used``."""
    comp = ctx.state.components.get(component_id)
    if comp is None:
        if component_id in health.COMPONENTS and ctx.cfg.module_enabled(component_id):
            meta = health.meta_for(component_id)
            comp = ComponentHealth(
                id=component_id,
                label=meta.label,
                emoji=meta.emoji,
                status="unknown",
                reason="not checked yet",
                chain=[],
                metrics={},
                depends_on=list(meta.depends_on),
                latency_ms=None,
                since=None,
                checked_at=None,
            )
        else:
            raise HTTPException(status_code=404, detail=f"unknown or disabled component {component_id!r}")
    now = time.time()
    check_id = health.HEADLINE_LATENCY.get(component_id)
    latency = (
        ctx.store.latency_series(check_id, now - 3600, now) if check_id else {"check": None, "points": []}
    )
    events = [e.to_dict() for e in ctx.store.events(limit=20, component=component_id)]
    cfg_masked = masked(ctx.cfg)
    config_used = {key: cfg_masked.get(key) for key in CONFIG_SLICES.get(component_id, ())}
    data = comp.to_dict()
    data.update(
        {
            "latency_series_1h": latency,
            "recent_events": events,
            "config_used": config_used,
            "affected": health.affected_closure(component_id, ctx.scheduler.enabled_component_ids()),
        }
    )
    return data


__all__ = ["CONFIG_SLICES", "build_meta", "build_status", "ordered_components", "router"]
