"""Shared helpers for the API routers: range parsing, app context access, error responses."""

from __future__ import annotations

import math
import threading
import time
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

if TYPE_CHECKING:  # pragma: no cover
    from dspace_sentinel.bus import Bus
    from dspace_sentinel.config import Config
    from dspace_sentinel.scheduler import Scheduler
    from dspace_sentinel.state import State
    from dspace_sentinel.store import Store

RANGES: dict[str, int] = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
"""Accepted ``range=`` values and their length in seconds."""

MAX_CUSTOM_RANGE_S = 366 * 86400
"""A custom ``{from, to}`` range starts at most this long ago (the CLI caps ``--range`` the same)."""

FUTURE_SKEW_S = 300.0
"""How far past "now" a custom range may end (clock skew between browser and server)."""


def parse_range(value: str | None, default: str = "24h") -> int:
    """Length in seconds of a ``range`` query value; 400 for anything unknown."""
    key = (value or default).strip().lower()
    if key not in RANGES:
        raise HTTPException(
            status_code=400, detail=f"invalid range {value!r}; use one of {', '.join(RANGES)}"
        )
    return RANGES[key]


def range_bounds(
    value: str | None, now: float | None = None, default: str = "24h"
) -> tuple[float, float, str]:
    """``(t_from, t_to, label)`` for a ``range`` query value."""
    now = now or time.time()
    key = (value or default).strip().lower()
    span = parse_range(key)
    return now - span, now, key


def parse_custom_range(body: Any, now: float | None = None) -> tuple[float, float, str]:
    """Range from a report request body: ``"24h"`` style or ``{"from": ts, "to": ts}``."""
    now = now or time.time()
    if isinstance(body, str):
        return range_bounds(body, now)
    if isinstance(body, dict):
        try:
            t_from = float(body["from"])
            t_to = float(body.get("to", now))
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=400, detail="range must be '24h'|'7d'|'30d' or {from, to}"
            ) from exc
        if not (math.isfinite(t_from) and math.isfinite(t_to)):
            raise HTTPException(status_code=400, detail="range 'from' and 'to' must be finite epoch seconds")
        if t_to <= t_from:
            raise HTTPException(status_code=400, detail="range 'to' must be after 'from'")
        if t_from < now - MAX_CUSTOM_RANGE_S or t_to - t_from > MAX_CUSTOM_RANGE_S:
            raise HTTPException(status_code=400, detail="range must lie within the last 366 days")
        if t_to > now + FUTURE_SKEW_S:
            raise HTTPException(status_code=400, detail="range 'to' must not be in the future")
        return t_from, t_to, "custom"
    raise HTTPException(status_code=400, detail="range must be '24h'|'7d'|'30d' or {from, to}")


class SingleFlight:
    """At most one job of a kind at a time (report generation, export).

    ``try_start()`` returns a token (or ``None`` while another job runs); ``finish(token)`` ends
    that job and is safe to call more than once.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token: object | None = None

    def try_start(self) -> object | None:
        with self._lock:
            if self._token is not None:
                return None
            self._token = object()
            return self._token

    def finish(self, token: object) -> None:
        with self._lock:
            if self._token is token:
                self._token = None

    @property
    def busy(self) -> bool:
        return self._token is not None


class AppContext:
    """Typed view over ``request.app.state`` so routers do not repeat ``request.app.state.x``."""

    def __init__(self, request: Request) -> None:
        self._state = request.app.state
        self.request = request

    @property
    def cfg(self) -> Config:
        return self._state.cfg

    @cfg.setter
    def cfg(self, value: Config) -> None:
        self._state.cfg = value

    @property
    def store(self) -> Store:
        return self._state.store

    @property
    def state(self) -> State:
        return self._state.state

    @property
    def bus(self) -> Bus:
        return self._state.bus

    @property
    def scheduler(self) -> Scheduler:
        return self._state.scheduler

    @property
    def alert_engine(self) -> Any:
        return self._state.alert_engine

    @property
    def incident_engine(self) -> Any:
        return self._state.incident_engine

    @property
    def config_path(self) -> Any:
        return self._state.config_path

    @property
    def started_ts(self) -> float:
        return self._state.started_ts

    @property
    def report_flight(self) -> SingleFlight:
        """One ``POST /api/reports`` generation at a time."""
        return self._state.report_flight

    @property
    def export_flight(self) -> SingleFlight:
        """One ``GET /api/export`` at a time."""
        return self._state.export_flight

    @property
    def shutdown_event(self) -> Any:
        """``threading.Event`` set when the server begins to shut down (SSE streams then end)."""
        return self._state.shutdown_event


def get_ctx(request: Request) -> AppContext:
    """FastAPI dependency returning the :class:`AppContext`."""
    return AppContext(request)


def error_response(status_code: int, message: str) -> JSONResponse:
    """``{"error": message}`` with the given status."""
    return JSONResponse({"error": message}, status_code=status_code)


__all__ = [
    "FUTURE_SKEW_S",
    "MAX_CUSTOM_RANGE_S",
    "RANGES",
    "AppContext",
    "SingleFlight",
    "error_response",
    "get_ctx",
    "parse_custom_range",
    "parse_range",
    "range_bounds",
]
