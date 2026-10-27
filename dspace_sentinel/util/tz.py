"""The time zone Sentinel uses for people (``server.timezone``).

Alert and incident messages, emails, Telegram, report timestamps and chart ticks, the weekly
report hour, quiet hours, the daily digest and the ``doctor`` box all show and read times in
this zone. ``""`` means the server's own zone. Log files keep their own zones
(``logs.files[].timezone``, :mod:`dspace_sentinel.util.logs`): nothing here touches them.

A zone needs the IANA time zone data (``/usr/share/zoneinfo`` or the ``tzdata`` package); a
minimal system without it knows only ``UTC``. A name that cannot be loaded is refused by
``PATCH /api/config`` and, in the configuration file, ignored with a start-up notice: Sentinel
then uses the server's zone (see :func:`zone_of`).
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, available_timezones

if TYPE_CHECKING:  # pragma: no cover
    from dspace_sentinel.config import Config

log = logging.getLogger(__name__)

UTC_NAMES = frozenset({"utc", "etc/utc"})
"""Names that always mean UTC, even on a system without time zone data."""

ZONE_ERROR = (
    "unknown time zone {name}: use an IANA name such as Asia/Colombo, or leave it empty for the server's zone"
)
"""Why a ``server.timezone`` is refused (``{name}`` is the value, quoted)."""

NO_ZONE_DATA = " (this system has no time zone data: install the tzdata package)"
"""Added to :data:`ZONE_ERROR` when the system knows no zone at all."""

ZONE_TYPE_ERROR = "the time zone must be text, such as Asia/Colombo"

MAX_ZONE_LENGTH = 64
"""Longer values are refused before the zone data is asked (IANA names are far shorter)."""

_warned: set[str] = set()


def _refusal(name: str) -> str:
    shown = repr(name[:40]) + ("…" if len(name) > 40 else "")
    text = ZONE_ERROR.format(name=shown)
    try:
        has_data = bool(available_timezones())
    except Exception:
        has_data = False
    return text if has_data else text + NO_ZONE_DATA


def resolve(name: str | None) -> _dt.tzinfo | None:
    """The zone ``name`` names: ``None`` for ``""`` (the server's zone), :data:`datetime.timezone.utc`
    for ``UTC``, else the IANA zone (``Asia/Colombo``). Raises ``ValueError`` (:data:`ZONE_ERROR`)
    for a name that cannot be loaded.
    """
    key = (name or "").strip()
    if not key:
        return None
    if key.lower() in UTC_NAMES:
        return _dt.timezone.utc
    if len(key) > MAX_ZONE_LENGTH or key.startswith(("/", ".")) or ".." in key or "\\" in key:
        raise ValueError(_refusal(key))
    try:
        return ZoneInfo(key)
    except Exception as exc:  # unknown key, a directory such as "Asia", no zone data at all
        raise ValueError(_refusal(key)) from exc


def check_zone(value: Any) -> str:
    """``server.timezone`` as it is stored: the name without surrounding spaces, or ``""``.

    Raises ``ValueError`` for anything that is not text or names no loadable zone.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(ZONE_TYPE_ERROR)
    cleaned = value.strip()
    resolve(cleaned)
    return cleaned


def zone_of(cfg: Config | Any) -> _dt.tzinfo | None:
    """The zone of ``cfg.server.timezone`` (``None``: the server's zone).

    A name that cannot be loaded (time zone data removed after the start) falls back to the
    server's zone with one warning in the log.
    """
    name = str(getattr(getattr(cfg, "server", None), "timezone", "") or "").strip()
    try:
        return resolve(name)
    except ValueError as exc:
        if name not in _warned:
            _warned.add(name)
            log.warning("server.timezone %r cannot be used, so times use the server's zone: %s", name, exc)
        return None


def wall(ts: float, tz: _dt.tzinfo | None) -> _dt.datetime:
    """``ts`` on the wall clock of ``tz``: aware in ``tz``, or naive local time for ``None``."""
    return _dt.datetime.fromtimestamp(ts, tz) if tz is not None else _dt.datetime.fromtimestamp(ts)


def aware(ts: float, tz: _dt.tzinfo | None) -> _dt.datetime:
    """``ts`` as an aware datetime in ``tz`` (``None``: the server's zone, with its offset at ``ts``)."""
    if tz is not None:
        return _dt.datetime.fromtimestamp(ts, tz)
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).astimezone()


def label(moment: _dt.datetime) -> str:
    """The zone of an aware datetime as people read it: its abbreviation when that is a word
    (``UTC``, ``CEST``), else the UTC offset (``+0530`` for Asia/Colombo, ``+0400`` for a zone
    whose abbreviation is ``+04``)."""
    name = moment.tzname() or ""
    if name.isalpha():
        return name
    return moment.strftime("%z") or name or "UTC"


def zone_label(ts: float, tz: _dt.tzinfo | None) -> str:
    """:func:`label` of ``ts`` in ``tz`` (the abbreviation can change with summer time)."""
    return label(aware(ts, tz))


def zone_name(tz: _dt.tzinfo | None) -> str:
    """The IANA name of ``tz`` (``UTC`` for UTC), or the server's own zone's for ``None``."""
    if tz is None:
        return server_zone_name()
    key = getattr(tz, "key", None)
    if key:
        return str(key)
    return "UTC" if tz is _dt.timezone.utc else str(tz)


def server_zone_name() -> str:
    """The IANA name of the server's own zone: from ``TZ``, the ``/etc/localtime`` link or
    ``/etc/timezone``; its abbreviation (``UTC``) when none of them names one."""
    candidates: list[str] = []
    env = os.environ.get("TZ", "").strip().lstrip(":")
    if env:
        candidates.append(env)
    try:
        candidates.append(os.path.realpath("/etc/localtime"))
    except (OSError, ValueError):
        pass
    for candidate in candidates:
        if "/zoneinfo/" in candidate:
            return candidate.split("/zoneinfo/", 1)[1]
        if candidate and not candidate.startswith("/"):
            return candidate
    try:
        lines = Path("/etc/timezone").read_text(encoding="utf-8").split()
    except (OSError, ValueError):
        lines = []
    if lines:
        return lines[0]
    return zone_label(time.time(), None)


def describe(cfg: Config | Any, now: float | None = None) -> dict[str, str]:
    """``/api/meta``'s ``timezone``: the zone Sentinel uses now (``name``, ``abbreviation``) and the
    server's own (``server_name``, ``server_abbreviation``, for "empty = the server's zone")."""
    now = time.time() if now is None else now
    tz = zone_of(cfg)
    server_name = server_zone_name()
    server_abbreviation = zone_label(now, None)
    if tz is None:
        return {
            "name": server_name,
            "abbreviation": server_abbreviation,
            "server_name": server_name,
            "server_abbreviation": server_abbreviation,
        }
    return {
        "name": zone_name(tz),
        "abbreviation": zone_label(now, tz),
        "server_name": server_name,
        "server_abbreviation": server_abbreviation,
    }


__all__ = [
    "MAX_ZONE_LENGTH",
    "NO_ZONE_DATA",
    "UTC_NAMES",
    "ZONE_ERROR",
    "ZONE_TYPE_ERROR",
    "aware",
    "check_zone",
    "describe",
    "label",
    "resolve",
    "server_zone_name",
    "wall",
    "zone_label",
    "zone_name",
    "zone_of",
]
