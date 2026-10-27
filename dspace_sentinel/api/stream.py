"""``/api/stream``: Server-Sent Events.

Events: ``status`` (same payload as ``/api/status``, at most every 5 s, one immediately on
connect), ``component``, ``alert``, ``incident``, ``report`` (forwarded from the bus) and
``heartbeat`` every 30 s of silence. The stream ends when the client disconnects or the server
shuts down (``app.state.shutdown_event``), so an open dashboard never holds up a restart.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from dspace_sentinel.api.deps import AppContext
from dspace_sentinel.api.routes_status import build_status

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["stream"])

HEARTBEAT_S = 30.0
STATUS_MIN_INTERVAL_S = 5.0
SHUTDOWN_POLL_S = 1.0
"""Longest wait between two looks at the shutdown flag."""
FORWARDED_EVENTS = frozenset({"component", "alert", "incident", "report"})


def format_sse(event: str, data: Any, event_id: str | None = None) -> bytes:
    """Encode one SSE frame."""
    payload = json.dumps(data, separators=(",", ":"), default=str)
    head = f"id: {event_id}\n" if event_id else ""
    return f"{head}event: {event}\ndata: {payload}\n\n".encode()


async def event_stream(ctx: AppContext, request: Request | None = None) -> AsyncIterator[bytes]:
    """Async generator yielding SSE frames until the client disconnects or the server shuts down."""
    bus = ctx.bus
    shutdown = getattr(ctx, "shutdown_event", None)
    counter = 0

    def status_frame() -> bytes:
        nonlocal counter
        counter += 1
        payload = build_status(ctx.cfg, ctx.state, ctx.store, ctx.scheduler, ctx.alert_engine)
        return format_sse("status", payload, str(counter))

    async with bus.listen(maxsize=128) as sub:
        yield b": connected\n\n"
        yield status_frame()
        last_status = time.monotonic()
        last_sent = last_status
        pending_status = False
        while True:
            if shutdown is not None and shutdown.is_set():
                return
            if request is not None:
                try:
                    if await request.is_disconnected():
                        return
                except Exception:
                    return
            now = time.monotonic()
            wait_status = (
                max(0.0, STATUS_MIN_INTERVAL_S - (now - last_status)) if pending_status else HEARTBEAT_S
            )
            wait_heartbeat = max(0.0, HEARTBEAT_S - (now - last_sent))
            timeout = max(0.05, min(wait_status, wait_heartbeat, SHUTDOWN_POLL_S))
            evt = await sub.get(timeout=timeout)
            now = time.monotonic()
            if evt is not None:
                if evt.event == "status":
                    pending_status = True
                elif evt.event in FORWARDED_EVENTS:
                    counter += 1
                    yield format_sse(evt.event, evt.data, str(counter))
                    last_sent = now
            if pending_status and now - last_status >= STATUS_MIN_INTERVAL_S:
                yield status_frame()
                pending_status = False
                last_status = now
                last_sent = now
            elif now - last_sent >= HEARTBEAT_S:
                counter += 1
                yield format_sse("heartbeat", {"ts": time.time()}, str(counter))
                last_sent = now


@router.get("/stream")
async def stream(request: Request) -> StreamingResponse:
    """SSE endpoint; ``text/event-stream`` with buffering disabled for reverse proxies."""
    ctx = AppContext(request)

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in event_stream(ctx, request):
                yield chunk
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("SSE stream failed")

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(body(), media_type="text/event-stream", headers=headers)


__all__ = [
    "FORWARDED_EVENTS",
    "HEARTBEAT_S",
    "SHUTDOWN_POLL_S",
    "STATUS_MIN_INTERVAL_S",
    "event_stream",
    "format_sse",
    "router",
]
