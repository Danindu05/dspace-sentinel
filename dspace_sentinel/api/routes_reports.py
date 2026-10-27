"""``/api/reports``: list, generate (201), download inline, soft delete + restore, options.

Generation runs in a worker thread (``asyncio.to_thread``) so a 30-day report never blocks
the event loop; the optional Telegram and email deliveries are awaited afterwards and reported
in the response as ``telegram: {ok, error}`` and ``email: {ok, error, results?}`` without ever
failing the request.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from dspace_sentinel import paths
from dspace_sentinel.api.deps import AppContext, get_ctx, parse_custom_range
from dspace_sentinel.reports import generator
from dspace_sentinel.reports.analytics import ALL_SECTIONS, RANGE_TITLES, SECTION_LABELS
from dspace_sentinel.store import REPORT_PURGE_AFTER_S

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["reports"])

MEDIA_TYPES = generator.MEDIA_TYPES
REPORT_RANGES: tuple[str, ...] = ("24h", "7d", "30d")
REPORT_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:"
"""Reports open inline on Sentinel's origin: no scripts, no outgoing requests, inline styles only."""


def _reports_dir(ctx: AppContext) -> Path:
    """Where this app instance writes report files: ``<data_dir>/reports``."""
    data_dir = getattr(ctx.request.app.state, "data_dir", None)
    return paths.reports_dir(Path(data_dir)) if data_dir else paths.reports_dir()


def _report_payload(report: Any) -> dict[str, Any]:
    data = report.to_dict()
    data["filename"] = Path(report.path).name
    data["exists"] = Path(report.path).is_file()
    if report.deleted_ts is not None:
        data["purge_after_ts"] = report.deleted_ts + REPORT_PURGE_AFTER_S
    return data


@router.get("/reports")
async def list_reports(
    include_deleted: bool = Query(False, description="also list soft-deleted reports (undo window)"),
    limit: int = Query(200, ge=1, le=1000),
    ctx: AppContext = Depends(get_ctx),
) -> list[dict[str, Any]]:
    """Reports newest first (soft-deleted ones only with ``include_deleted=true``)."""
    reports = await asyncio.to_thread(ctx.store.reports, include_deleted, limit)
    return [_report_payload(r) for r in reports]


@router.get("/reports/options")
async def report_options(ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """What the report panel can offer: sections (with labels), defaults, formats, ranges, and
    whether Telegram (on, with a bot that has a chat id) and email (on, with the login and at
    least one recipient) can take the file."""
    telegram = ctx.cfg.notifiers.telegram
    email = ctx.cfg.notifiers.email
    return {
        "sections": [
            {"id": sid, "emoji": SECTION_LABELS[sid][0], "label": SECTION_LABELS[sid][1]}
            for sid in ALL_SECTIONS
        ],
        "default_sections": list(ctx.cfg.reports.default_sections),
        "formats": list(generator.VALID_FORMATS),
        "ranges": [{"id": r, "label": RANGE_TITLES[r]} for r in REPORT_RANGES],
        "telegram_available": bool(telegram.enabled and telegram.destinations()),
        "email_available": bool(email.enabled and email.ready()),
        "weekly": ctx.cfg.reports.weekly.model_dump(mode="json"),
        "purge_after_s": REPORT_PURGE_AFTER_S,
    }


def _parse_create_body(body: Any) -> dict[str, Any]:
    """Validate a ``POST /api/reports`` body; raises ``HTTPException(400)`` with a readable message."""
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    t_from, t_to, label = parse_custom_range(body.get("range", "24h"))
    fmt = str(body.get("fmt") or body.get("format") or "html").lower()
    if fmt not in generator.VALID_FORMATS:
        raise HTTPException(
            status_code=400, detail=f"fmt must be one of {', '.join(generator.VALID_FORMATS)}"
        )
    sections = body.get("sections")
    if sections is not None:
        if not isinstance(sections, list) or not all(isinstance(s, str) for s in sections):
            raise HTTPException(status_code=400, detail="sections must be a list of section ids")
        unknown = [s for s in sections if s.lower() not in ALL_SECTIONS]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"unknown sections: {', '.join(unknown)}; use {', '.join(ALL_SECTIONS)}",
            )
    title = body.get("title")
    if title is not None and not isinstance(title, str):
        raise HTTPException(status_code=400, detail="title must be a string")
    return {
        "t_from": t_from,
        "t_to": t_to,
        "range_hint": None if label == "custom" else label,
        "fmt": fmt,
        "title": (title or "").strip()[:200] or None,
        "sections": sections,
        "send_telegram": bool(body.get("send_telegram", False)),
        "send_email": bool(body.get("send_email", False)),
    }


@router.post("/reports", status_code=201)
async def create_report(
    body: dict[str, Any] = Body(default_factory=dict), ctx: AppContext = Depends(get_ctx)
) -> Any:
    """Generate a report: ``{range: "24h"|"7d"|"30d"|{from,to}, fmt, title?, sections?,
    send_telegram?, send_email?}``.

    Returns the :class:`~dspace_sentinel.models.Report` (201) plus ``telegram: {ok, error}`` and
    ``email: {ok, error, results?}`` for the deliveries asked for. Validation problems answer 400
    with ``{"error": ...}``; a data filesystem with less than 1 GB free answers 507.
    """
    req = _parse_create_body(body)
    send_telegram = req["send_telegram"]
    flight = ctx.report_flight
    token = flight.try_start()
    if token is None:
        raise HTTPException(
            status_code=429,
            detail="a report is already being generated; try again in a moment",
            headers={"Retry-After": "5"},
        )
    started = threading.Event()
    cfg, store, reports_dir = ctx.cfg, ctx.store, _reports_dir(ctx)

    def generate() -> Any:
        # the worker frees the slot when it is done, even if this request was cancelled meanwhile
        started.set()
        try:
            return generator.generate(
                store,
                cfg,
                req["t_from"],
                req["t_to"],
                req["fmt"],
                req["title"],
                req["sections"],
                False,
                reports_dir=reports_dir,
                range_hint=req["range_hint"],
            )
        finally:
            flight.finish(token)

    try:
        report = await asyncio.to_thread(generate)
    except NotImplementedError as exc:
        return JSONResponse({"error": str(exc)}, status_code=501)
    except generator.ReportSpaceError as exc:
        # the data filesystem is too full: a storage condition, not a bad request
        raise HTTPException(status_code=507, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("report generation failed")
        raise HTTPException(
            status_code=500, detail="report generation failed; the Sentinel log has the details"
        ) from exc
    finally:
        if not started.is_set():  # cancelled before the worker ran
            flight.finish(token)
    payload = _report_payload(report)
    try:
        ctx.bus.publish("report", {"type": "created", "report": payload})
    except Exception:
        log.exception("could not publish report event")
    if send_telegram:
        try:
            payload["telegram"] = await generator.deliver_telegram_async(ctx.cfg, report, store=ctx.store)
        except Exception as exc:  # deliver_telegram_async never raises, but the response must not fail
            log.exception("telegram delivery failed")
            payload["telegram"] = {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
    if req["send_email"]:
        try:
            payload["email"] = await generator.deliver_email_async(ctx.cfg, report, store=ctx.store)
        except Exception as exc:  # deliver_email_async never raises, but the response must not fail
            log.exception("email delivery failed")
            payload["email"] = {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
    return JSONResponse(payload, status_code=201)


@router.get("/reports/{report_id}")
async def get_report_file(
    report_id: int,
    meta: bool = Query(False, description="return the report row instead of the file"),
    download: bool = Query(False, description="force Content-Disposition: attachment"),
    ctx: AppContext = Depends(get_ctx),
) -> Any:
    """The report file itself (inline for HTML so it opens in a tab; attachment for md/json)."""
    report = ctx.store.get_report(report_id, include_deleted=meta)
    if report is None:
        raise HTTPException(status_code=404, detail=f"report {report_id} not found")
    if meta:
        return _report_payload(report)
    path = Path(report.path)
    if not path.is_file():
        raise HTTPException(status_code=410, detail="report file is gone")
    disposition = "inline" if (report.fmt == "html" and not download) else "attachment"
    headers = {
        "Content-Disposition": f'{disposition}; filename="{path.name}"',
        "Cache-Control": "no-cache",
        "X-Content-Type-Options": "nosniff",
    }
    if report.fmt == "html":
        headers["Content-Security-Policy"] = REPORT_CSP
    return FileResponse(
        str(path),
        media_type=MEDIA_TYPES.get(report.fmt, "application/octet-stream"),
        headers=headers,
    )


@router.delete("/reports/{report_id}")
async def delete_report(report_id: int, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """Soft delete: the file stays for 24 h so *Undo* can restore it, then it is purged."""
    report = ctx.store.get_report(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"report {report_id} not found")
    deleted = ctx.store.soft_delete_report(report_id, time.time())
    payload = _report_payload(deleted) if deleted else {"id": report_id}
    try:
        ctx.store.add_event("report", None, f"Report deleted: {report.title}", {"report_id": report_id})
        ctx.bus.publish("report", {"type": "deleted", "report": payload})
    except Exception:
        log.exception("could not record report deletion")
    return payload


@router.post("/reports/{report_id}/restore")
async def restore_report(report_id: int, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """Undo a soft delete (409 once the file has already been purged)."""
    report = ctx.store.get_report(report_id, include_deleted=True)
    if report is None:
        raise HTTPException(status_code=404, detail=f"report {report_id} not found")
    if not Path(report.path).is_file():
        raise HTTPException(status_code=409, detail="report file has already been purged")
    restored = ctx.store.restore_report(report_id)
    payload = _report_payload(restored) if restored else _report_payload(report)
    try:
        ctx.store.add_event("report", None, f"Report restored: {report.title}", {"report_id": report_id})
        ctx.bus.publish("report", {"type": "restored", "report": payload})
    except Exception:
        log.exception("could not record report restore")
    return payload


__all__ = ["REPORT_CSP", "REPORT_RANGES", "router"]
