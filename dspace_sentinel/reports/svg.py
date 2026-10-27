"""Server-side inline SVG for reports: line charts, availability strips, meters and sparklines.

The output follows the UI's chart language (SPEC section 12.7): 2 px lines with round joins,
a hairline solid grid, threshold hairlines with a small label, ticks in the ``--ink-3`` tone,
no dashed lines and no gradients. Colours are written as ``var(--token, #fallback)`` so the
SVG picks up the report's tokens (and dark mode) when inlined, and still looks right when the
markup is viewed on its own.

Every function returns a plain ``str`` of well-formed SVG; the generator wraps it in
``markupsafe.Markup`` before handing it to Jinja.
"""

from __future__ import annotations

import datetime as _dt
import math
from collections.abc import Sequence
from dataclasses import dataclass
from html import escape
from typing import Any

from dspace_sentinel.util.fmt import bytes_
from dspace_sentinel.util.tz import aware, wall

PALETTE: dict[str, str] = {
    "ink": "var(--ink, #17150F)",
    "ink-2": "var(--ink-2, #5B564A)",
    "ink-3": "var(--ink-3, #8A8477)",
    "line": "var(--line, rgba(23,21,15,.12))",
    "surface": "var(--surface, #FFFDF8)",
    "surface-2": "var(--surface-2, #F1ECDF)",
    "chart-1": "var(--chart-1, #2F6FD6)",
    "chart-2": "var(--chart-2, #E06B2D)",
    "chart-3": "var(--chart-3, #189B72)",
    "ok-fill": "var(--ok-fill, #178F58)",
    "warn-fill": "var(--warn-fill, #A86F00)",
    "crit-fill": "var(--crit-fill, #D4432E)",
    "unknown-fill": "var(--unknown-fill, #8A8477)",
}
"""Token name → CSS colour expression used inside the SVG."""

STATUS_FILL: dict[str, str] = {
    "ok": "ok-fill",
    "warn": "warn-fill",
    "crit": "crit-fill",
    "unknown": "unknown-fill",
}

FONT_BODY = "Inter, ui-sans-serif, system-ui, sans-serif"
FONT_DISPLAY = '"Space Grotesk", ui-sans-serif, system-ui, sans-serif'

TICK_STEPS: tuple[int, ...] = (
    300,
    900,
    1800,
    3600,
    7200,
    10800,
    21600,
    43200,
    86400,
    172800,
    259200,
    604800,
)

PAD_LEFT = 52
PAD_RIGHT = 14
PAD_TOP = 14
PAD_BOTTOM = 26


@dataclass
class Series:
    """One line of a chart: ``points`` are ``[ts, value]`` pairs (extra elements are ignored)."""

    label: str
    points: Sequence[Sequence[Any]]
    color: str = "chart-1"


def color(token: str) -> str:
    """CSS colour expression for a palette token (unknown tokens pass through unchanged)."""
    return PALETTE.get(token, token)


def _num(value: float) -> str:
    """Compact SVG number: ``12`` / ``12.5`` (never scientific notation)."""
    if abs(value - round(value)) < 1e-6:
        return str(int(round(value)))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def nice_ceiling(value: float) -> float:
    """Round ``value`` up to a "nice" axis maximum (1, 2, 2.5, 5 × 10^n); at least 1."""
    if value is None or value <= 0 or math.isnan(value) or math.isinf(value):
        return 1.0
    exponent = math.floor(math.log10(value))
    base = 10.0**exponent
    for mult in (1.0, 2.0, 2.5, 5.0, 10.0):
        if value <= base * mult * 1.0001:
            return base * mult
    return base * 10.0


def format_value(value: float | None, unit: str = "") -> str:
    """Axis / label text for a value in ``unit`` (``%``, ``ms``, ``B/s``, ``B``, or a bare number)."""
    if value is None:
        return "—"
    if unit == "B":
        return bytes_(value)
    if unit == "%":
        return f"{value:.0f} %"
    if unit == "ms":
        return f"{value / 1000:.1f} s" if value >= 1000 else f"{value:.0f} ms"
    if unit == "B/s":
        size = float(value)
        for suffix in ("B/s", "KB/s", "MB/s", "GB/s"):
            if size < 1024 or suffix == "GB/s":
                return f"{size:.0f} {suffix}" if suffix == "B/s" else f"{size:.1f} {suffix}"
            size /= 1024
    if abs(value) >= 100:
        return f"{value:.0f}"
    if abs(value) >= 10:
        return f"{value:.1f}".rstrip("0").rstrip(".")
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _local_offset(ts: float, tz: _dt.tzinfo | None = None) -> int:
    offset = aware(ts, tz).utcoffset()
    return int(offset.total_seconds()) if offset else 0


def time_ticks(
    t_from: float, t_to: float, max_ticks: int = 6, tz: _dt.tzinfo | None = None
) -> list[tuple[float, str]]:
    """Evenly spaced, nicely aligned ``(ts, label)`` ticks for the x axis, on the wall clock of
    ``tz`` (``server.timezone``; ``None``: the server's zone).

    Steps under a day give ``HH:MM`` labels; day steps are aligned to midnight in ``tz`` and
    labelled ``DD Mon``.
    """
    span = max(1.0, float(t_to) - float(t_from))
    step = TICK_STEPS[-1]
    for candidate in TICK_STEPS:
        if span / candidate <= max_ticks:
            step = candidate
            break
    else:
        step = int(math.ceil(span / max_ticks / 86400.0)) * 86400
    offset = _local_offset(t_from, tz) if step >= 86400 else 0
    first = math.ceil((t_from + offset) / step) * step - offset
    ticks: list[tuple[float, str]] = []
    ts = first
    fmt = "%H:%M" if step < 86400 else "%d %b"
    while ts <= t_to and len(ticks) < max_ticks + 1:
        ticks.append((float(ts), wall(ts, tz).strftime(fmt)))
        ts += step
    return ticks


def _text(
    x: float, y: float, text: str, *, anchor: str = "start", size: int = 12, fill: str = "ink-3"
) -> str:
    return (
        f'<text x="{_num(x)}" y="{_num(y)}" text-anchor="{anchor}" font-size="{size}" '
        f'fill="{color(fill)}">{escape(text)}</text>'
    )


def line_chart(
    series: Sequence[Series],
    *,
    width: int = 720,
    height: int = 200,
    unit: str = "",
    y_min: float = 0.0,
    y_max: float | None = None,
    thresholds: dict[str, float] | None = None,
    t_from: float | None = None,
    t_to: float | None = None,
    title: str = "",
    area: bool | None = None,
    tz: _dt.tzinfo | None = None,
) -> str:
    """Render one or more series as a line chart (time ticks in ``tz``, see :func:`time_ticks`).

    * y axis from ``y_min`` to ``y_max`` (a nice ceiling of the data when ``None``; a
      warning/critical threshold within twice the data range extends the axis so it stays visible),
    * hairline grid at four levels with tick labels, x ticks from :func:`time_ticks`,
    * 2 px lines, round joins, a 10 % area wash when there is a single series,
    * thresholds as 1 px hairlines in the warning/critical fills with a small label,
    * a legend when there are two or more series, ``role="img"`` + ``aria-label`` always.
    """
    pts_all: list[tuple[float, float]] = []
    for s in series:
        for p in s.points:
            if len(p) >= 2 and p[0] is not None and p[1] is not None:
                pts_all.append((float(p[0]), float(p[1])))
    if t_from is None:
        t_from = min((p[0] for p in pts_all), default=0.0)
    if t_to is None:
        t_to = max((p[0] for p in pts_all), default=t_from + 1.0)
    if t_to <= t_from:
        t_to = t_from + 1.0
    data_max = max((p[1] for p in pts_all), default=0.0)
    data_min = min((p[1] for p in pts_all), default=0.0)
    top = y_max
    if top is None:
        top = nice_ceiling(max(data_max * 1.05, y_min + 1e-9))
        for key in ("warning", "critical"):
            value = (thresholds or {}).get(key)
            if value is not None and value <= top * 2.0:
                top = max(top, nice_ceiling(float(value) * 1.1))
    if top <= y_min:
        top = y_min + 1.0

    plot_w = width - PAD_LEFT - PAD_RIGHT
    plot_h = height - PAD_TOP - PAD_BOTTOM
    x0, y0 = PAD_LEFT, PAD_TOP

    def sx(ts: float) -> float:
        return x0 + (ts - t_from) / (t_to - t_from) * plot_w

    def sy(value: float) -> float:
        clamped = max(y_min, min(top, value))
        return y0 + plot_h - (clamped - y_min) / (top - y_min) * plot_h

    parts: list[str] = []
    summary = (
        f"min {format_value(data_min, unit)}, max {format_value(data_max, unit)}" if pts_all else "no data"
    )
    label = f"{title}: {summary}" if title else summary
    clip_id = f"clip{abs(hash((title, width, height, len(pts_all)))) % 10_000_000}"
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" '
        f'height="{height}" role="img" aria-label="{escape(label)}" '
        f'style="font-family:{escape(FONT_BODY)};font-variant-numeric:tabular-nums">'
    )
    parts.append(f"<title>{escape(label)}</title>")
    parts.append(
        f'<defs><clipPath id="{clip_id}"><rect x="{x0}" y="{y0}" width="{_num(plot_w)}" '
        f'height="{_num(plot_h)}"/></clipPath></defs>'
    )
    # grid + y labels
    for i in range(4):
        frac = i / 3.0
        value = y_min + (top - y_min) * frac
        y = sy(value)
        parts.append(
            f'<line x1="{x0}" y1="{_num(y)}" x2="{x0 + plot_w}" y2="{_num(y)}" stroke="{color("line")}" '
            f'stroke-width="1" shape-rendering="crispEdges"/>'
        )
        parts.append(_text(x0 - 8, y + 4, format_value(value, unit), anchor="end", size=11))
    # x ticks
    for ts, text in time_ticks(t_from, t_to, tz=tz):
        x = sx(ts)
        parts.append(
            f'<line x1="{_num(x)}" y1="{y0 + plot_h}" x2="{_num(x)}" y2="{y0 + plot_h + 4}" '
            f'stroke="{color("ink-3")}" stroke-width="1" shape-rendering="crispEdges"/>'
        )
        parts.append(_text(x, height - 8, text, anchor="middle", size=11))
    if not pts_all:
        parts.append(
            _text(
                x0 + plot_w / 2, y0 + plot_h / 2 + 4, "No data for this period", anchor="middle", fill="ink-2"
            )
        )
        parts.append("</svg>")
        return "".join(parts)
    # thresholds
    for key, token in (("warning", "warn-fill"), ("critical", "crit-fill")):
        value = (thresholds or {}).get(key)
        if value is None or not (y_min <= float(value) <= top):
            continue
        y = sy(float(value))
        parts.append(
            f'<line x1="{x0}" y1="{_num(y)}" x2="{x0 + plot_w}" y2="{_num(y)}" stroke="{color(token)}" '
            f'stroke-width="1" shape-rendering="crispEdges"/>'
        )
        parts.append(
            _text(
                x0 + plot_w - 4,
                y - 4,
                f"{key} {format_value(float(value), unit)}",
                anchor="end",
                size=11,
                fill=token,
            )
        )
    # lines
    wash = (len(series) == 1) if area is None else bool(area)
    parts.append(f'<g clip-path="url(#{clip_id})">')
    for s in series:
        segments: list[list[tuple[float, float]]] = [[]]
        for p in s.points:
            if len(p) < 2 or p[0] is None or p[1] is None:
                if segments[-1]:
                    segments.append([])
                continue
            segments[-1].append((sx(float(p[0])), sy(float(p[1]))))
        stroke = color(s.color)
        base_y = y0 + plot_h
        for seg in segments:
            if not seg:
                continue
            if len(seg) == 1:
                x, y = seg[0]
                parts.append(f'<circle cx="{_num(x)}" cy="{_num(y)}" r="2" fill="{stroke}"/>')
                continue
            d = "M" + " L".join(f"{_num(x)} {_num(y)}" for x, y in seg)
            if wash:
                parts.append(
                    f'<path d="{d} L{_num(seg[-1][0])} {_num(base_y)} L{_num(seg[0][0])} {_num(base_y)} Z" '
                    f'fill="{stroke}" fill-opacity="0.1" stroke="none"/>'
                )
            parts.append(
                f'<path d="{d}" fill="none" stroke="{stroke}" stroke-width="2" stroke-linejoin="round" '
                f'stroke-linecap="round"/>'
            )
    parts.append("</g>")
    # legend
    if len(series) >= 2:
        x = x0 + 4
        for s in series:
            parts.append(
                f'<rect x="{_num(x)}" y="{y0 - 10}" width="14" height="3" rx="1.5" fill="{color(s.color)}"/>'
            )
            parts.append(_text(x + 20, y0 - 5, s.label, size=11, fill="ink-2"))
            x += 20 + 7 * len(s.label) + 18
    parts.append("</svg>")
    return "".join(parts)


def strip(
    buckets: Sequence[str | None],
    *,
    width: int = 720,
    height: int = 14,
    gap: int = 2,
    radius: int = 3,
    title: str = "",
) -> str:
    """Availability strip: one rounded bar per bucket in the status fill (``None`` = no data)."""
    n = max(1, len(buckets))
    bar_w = (width - gap * (n - 1)) / n
    counts = {"ok": 0, "warn": 0, "crit": 0, "unknown": 0}
    for b in buckets:
        if b in counts:
            counts[b] += 1
    summary = ", ".join(f"{v} {k}" for k, v in counts.items() if v)
    label = f"{title}: {summary or 'no data'}" if title else (summary or "no data")
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" '
        f'height="{height}" role="img" aria-label="{escape(label)}" preserveAspectRatio="none">',
        f"<title>{escape(label)}</title>",
    ]
    for i, status in enumerate(buckets):
        token = STATUS_FILL.get(status or "", "surface-2") if status else "surface-2"
        x = i * (bar_w + gap)
        parts.append(
            f'<rect x="{_num(x)}" y="0" width="{_num(bar_w)}" height="{height}" rx="{radius}" '
            f'fill="{color(token)}"/>'
        )
    if not buckets:
        parts.append(
            f'<rect x="0" y="0" width="{width}" height="{height}" rx="{radius}" fill="{color("surface-2")}"/>'
        )
    parts.append("</svg>")
    return "".join(parts)


def meter(
    value: float | None,
    maximum: float,
    *,
    status: str = "ok",
    width: int = 200,
    height: int = 8,
    title: str = "",
    unit: str = "",
) -> str:
    """Horizontal meter: 8 px track in ``--surface-2`` with the filled part in the status fill.

    With a ``title`` the accessible name reads "<title>: <value> of <maximum>" in ``unit``
    (see :func:`format_value`; ``"B"`` for sizes).
    """
    maximum = float(maximum) if maximum else 1.0
    fraction = 0.0 if value is None else max(0.0, min(1.0, float(value) / maximum))
    label = (
        f"{title}: {format_value(value, unit)} of {format_value(maximum, unit)}"
        if title
        else f"{fraction * 100:.0f} %"
    )
    r = height / 2
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" '
        f'height="{height}" role="img" aria-label="{escape(label)}"><title>{escape(label)}</title>'
        f'<rect x="0" y="0" width="{width}" height="{height}" rx="{_num(r)}" fill="{color("surface-2")}"/>'
        f'<rect x="0" y="0" width="{_num(max(height, width * fraction) if fraction > 0 else 0)}" '
        f'height="{height}" rx="{_num(r)}" fill="{color(STATUS_FILL.get(status, "ok-fill"))}"/></svg>'
    )


def sparkline(
    points: Sequence[float | None], *, width: int = 120, height: int = 32, status: str = "ok"
) -> str:
    """Tiny trend line (1.5 px in ``--ink-3``) with the last point dotted in the status fill."""
    values = [(i, float(v)) for i, v in enumerate(points) if v is not None]
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" '
        f'height="{height}" role="img" aria-label="trend">'
    ]
    if len(values) >= 2:
        lo = min(v for _, v in values)
        hi = max(v for _, v in values)
        span = (hi - lo) or 1.0
        n = max(1, len(points) - 1)
        coords = [(2 + i / n * (width - 4), height - 3 - (v - lo) / span * (height - 6)) for i, v in values]
        d = "M" + " L".join(f"{_num(x)} {_num(y)}" for x, y in coords)
        parts.append(
            f'<path d="{d}" fill="none" stroke="{color("ink-3")}" stroke-width="1.5" '
            f'stroke-linejoin="round" stroke-linecap="round"/>'
        )
        x, y = coords[-1]
        dot = color(STATUS_FILL.get(status, "ok-fill"))
        parts.append(f'<circle cx="{_num(x)}" cy="{_num(y)}" r="2.5" fill="{dot}"/>')
    parts.append("</svg>")
    return "".join(parts)


__all__ = [
    "FONT_BODY",
    "FONT_DISPLAY",
    "PALETTE",
    "STATUS_FILL",
    "Series",
    "color",
    "format_value",
    "line_chart",
    "meter",
    "nice_ceiling",
    "sparkline",
    "strip",
    "time_ticks",
]
