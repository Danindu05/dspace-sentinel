"""Formatting helpers shared by the CLI, reports and messages."""

from __future__ import annotations

import datetime as _dt

from dspace_sentinel.util.tz import aware, label

_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def bytes_(n: float | int | None, decimals: int = 1) -> str:
    """Human size: 1536 → "1.5 KB", 412e9 → "412.0 GB" (GB and above with one decimal)."""
    if n is None:
        return "—"
    value = float(n)
    unit = 0
    while abs(value) >= 1024 and unit < len(_UNITS) - 1:
        value /= 1024
        unit += 1
    if unit == 0:
        return f"{int(value)} B"
    return f"{value:.{decimals}f} {_UNITS[unit]}"


def gb(n: float | int | None, decimals: int = 1) -> str:
    """Bytes as GB with one decimal ("53.7 GB")."""
    if n is None:
        return "—"
    return f"{float(n) / 1024**3:.{decimals}f} GB"


def duration(seconds: float | None, short: bool = True) -> str:
    """Seconds → "6 m 12 s" / "17 d 3 h" / "45 s"; ``short=False`` spells "6 min 12 sec"."""
    if seconds is None:
        return "—"
    s = int(round(seconds))
    if s < 60:
        return f"{s} s" if short else f"{s} sec"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m} m {s} s" if short else f"{m} min {s} sec"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h} h {m} m" if short else f"{h} h {m} min"
    d, h = divmod(h, 24)
    return f"{d} d {h} h"


def pct(value: float | None, decimals: int = 0) -> str:
    """Percent with a thin space before the sign: 87.4 → "87 %"."""
    if value is None:
        return "—"
    return f"{value:.{decimals}f} %"


def ms(value: float | None) -> str:
    """Latency: below 1 s in ms ("91 ms"), above in seconds ("2.4 s")."""
    if value is None:
        return "—"
    if value < 1000:
        return f"{value:.0f} ms"
    return f"{value / 1000:.1f} s"


def relative(seconds_ago: float | None) -> str:
    """ "8 s ago", "3 m ago", "2 h ago", "5 d ago"."""
    if seconds_ago is None:
        return "never"
    s = max(0, int(seconds_ago))
    if s < 60:
        return f"{s} s ago"
    if s < 3600:
        return f"{s // 60} m ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} d ago"


def clock(
    ts: float | None, with_seconds: bool = False, tz: _dt.tzinfo | None = None, *, zone: bool = False
) -> str:
    """Wall clock "10:43" (or "10:43:07") in ``tz`` (``None``: the server's zone).

    ``zone=True`` names the zone after it: "10:43 +0530", "15:59 UTC" (:func:`util.tz.label`).
    """
    if ts is None:
        return "—"
    moment = aware(ts, tz)
    text = moment.strftime("%H:%M:%S" if with_seconds else "%H:%M")
    return f"{text} {label(moment)}" if zone else text


def date_time(ts: float | None, tz: _dt.tzinfo | None = None, *, zone: bool = False) -> str:
    """ "2024-01-01 10:43" in ``tz`` (``None``: the server's zone); ``zone=True``: "2024-01-01 10:43 UTC"."""
    if ts is None:
        return "—"
    moment = aware(ts, tz)
    text = moment.strftime("%Y-%m-%d %H:%M")
    return f"{text} {label(moment)}" if zone else text


def day(ts: float | None, tz: _dt.tzinfo | None = None) -> str:
    """The date "07 Oct 2026" in ``tz`` (``None``: the server's zone)."""
    if ts is None:
        return "—"
    return aware(ts, tz).strftime("%d %b %Y")


def number(value: float | int | None, decimals: int = 0) -> str:
    """Thousands-separated number ("27 491") using a thin non-breaking space."""
    if value is None:
        return "—"
    text = f"{value:,.{decimals}f}"
    return text.replace(",", " ")


def rate_bps(value: float | None) -> str:
    """Bytes per second → "1.2 MB/s"."""
    if value is None:
        return "—"
    return f"{bytes_(value)}/s"


__all__ = [
    "bytes_",
    "clock",
    "date_time",
    "day",
    "duration",
    "gb",
    "ms",
    "number",
    "pct",
    "rate_bps",
    "relative",
]
