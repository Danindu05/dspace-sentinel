"""``/api/alerts`` and ``/api/incidents`` (thin: the store and the engines do the work)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from dspace_sentinel.api.deps import AppContext, get_ctx

router = APIRouter(prefix="/api", tags=["alerts"])


@router.get("/alerts")
async def list_alerts(
    status: str = Query("open", pattern="^(open|resolved|all)$"),
    limit: int = Query(100, ge=1, le=1000),
    ctx: AppContext = Depends(get_ctx),
) -> list[dict[str, Any]]:
    """Alerts by status, newest first."""
    return [a.to_dict() for a in ctx.store.alerts(status, limit)]


@router.get("/alerts/{alert_id}")
async def get_alert(alert_id: int, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """One alert."""
    alert = ctx.store.get_alert(alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail=f"alert {alert_id} not found")
    return alert.to_dict()


@router.post("/alerts/{alert_id}/ack")
async def ack_alert(alert_id: int, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """Acknowledge an alert (silences reminders)."""
    engine = ctx.alert_engine
    alert = engine.ack(alert_id) if engine is not None else ctx.store.ack_alert(alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail=f"alert {alert_id} not found")
    return alert.to_dict()


@router.post("/alerts/{alert_id}/unack")
async def unack_alert(alert_id: int, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """Undo an acknowledgement."""
    engine = ctx.alert_engine
    alert = engine.unack(alert_id) if engine is not None else ctx.store.unack_alert(alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail=f"alert {alert_id} not found")
    return alert.to_dict()


@router.get("/incidents")
async def list_incidents(
    status: str = Query("all", pattern="^(open|resolved|all)$"),
    limit: int = Query(100, ge=1, le=1000),
    ctx: AppContext = Depends(get_ctx),
) -> list[dict[str, Any]]:
    """Incidents, newest first."""
    return [i.to_dict() for i in ctx.store.incidents(status, limit)]


@router.get("/incidents/{incident_id}")
async def get_incident(incident_id: int, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """One incident with its alerts expanded."""
    incident = ctx.store.get_incident(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail=f"incident {incident_id} not found")
    data = incident.to_dict()
    data["alerts"] = [a.to_dict() for a in ctx.store.alerts_for_incident(incident_id)]
    return data


__all__ = ["router"]
