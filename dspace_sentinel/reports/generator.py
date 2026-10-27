"""Report generation entry point (SPEC section 8).

``generate()`` computes the analytics, renders HTML / Markdown / JSON through the Jinja
templates in ``reports/templates`` (HTML charts come from :mod:`dspace_sentinel.reports.svg`),
writes ``data/reports/sentinel-report-YYYYMMDD-HHMM-<range>.<ext>`` atomically and records a
:class:`~dspace_sentinel.models.Report` row. ``generate_for_range()`` is the CLI-friendly
variant that parses ``24h|7d|30d``. Telegram delivery goes through
``alerts.notifiers.telegram.send_document`` (every destination's own bot) when that module
exists and degrades to a clear ``{"ok": false, "error": ...}`` otherwise; email delivery
attaches the file to one message for every recipient (``alerts.notifiers.email.send_file``).
Each delivery is recorded as a ``notify`` (sent or scheduled), ``notify.error`` (not sent) or
``notify.demo`` event naming its channel.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import inspect
import json
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jinja2 import Environment, FileSystemLoader, pass_context
from jinja2.runtime import Context
from markupsafe import Markup

from dspace_sentinel import paths
from dspace_sentinel.models import Report
from dspace_sentinel.reports import svg
from dspace_sentinel.reports.analytics import (
    ALL_SECTIONS,
    RANGE_SECONDS,
    RANGE_TITLES,
    SECTION_LABELS,
    compute_report_data,
    range_title,
)
from dspace_sentinel.util import fmt as fmt_util
from dspace_sentinel.util.tz import aware, zone_of
from dspace_sentinel.util.tz import label as zone_label_of

if TYPE_CHECKING:  # pragma: no cover
    from dspace_sentinel.config import Config
    from dspace_sentinel.store import Store

log = logging.getLogger(__name__)

VALID_FORMATS: tuple[str, ...] = ("html", "md", "json")
"""Report formats: self-contained HTML, Markdown, or the raw analytics dict as JSON."""

MEDIA_TYPES: dict[str, str] = {
    "html": "text/html; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "json": "application/json",
}

TEMPLATES_DIR: Path = Path(__file__).resolve().parent / "templates"
TEMPLATE_NAMES: dict[str, str] = {"html": "report.html.j2", "md": "report.md.j2"}

STATUS_LABELS: dict[str, str] = {"ok": "Healthy", "warn": "Warning", "crit": "Critical", "unknown": "Unknown"}
SEVERITY_LABELS: dict[str, str] = {"critical": "Critical", "warning": "Warning", "info": "Note", "ok": "OK"}
SEVERITY_STATUS: dict[str, str] = {"critical": "crit", "warning": "warn", "info": "unknown", "ok": "ok"}
MOOD_ACCENT: dict[str, str] = {"ok": "mint", "warn": "butter", "crit": "coral", "unknown": "sky"}

PORT_VERDICT_LABELS: dict[str, tuple[str, str]] = {
    "expected": ("Expected", "plain"),
    "local": ("Local", "plain"),
    "tailscale": ("Tailscale", "plain"),
    "exposed": ("Exposed", "warn"),
    "new": ("New", "warn"),
}
"""Verdict of an open port → (pill text, pill class) in the Security section."""

PORT_EXPECT_LABELS: dict[str, str] = {"open": "open", "local_only": "local only"}
"""What a ``ports`` rule expects, as the open-ports table words it."""

SUPPORT_LABELS: dict[str, tuple[str, str]] = {
    "ok": ("Supported", "ok"),
    "soon": ("Ends soon", "plain"),
    "ended": ("Ended", "warn"),
    "affected": ("Affected", "warn"),
    "unknown": ("Unknown", "unknown"),
}
"""Support status of a software version → (pill text, pill class); ``soon`` alone never warns."""

MAX_ALERT_ROWS = 60
"""Alerts shown in the HTML/Markdown tables (the JSON always holds them all)."""

MAX_PORT_ROWS = 60
"""Open ports shown in the HTML/Markdown Security section (the JSON always holds them all)."""

MAX_REPORTS = 200
"""Report files kept on disk: generating one more removes the oldest (row and file)."""

MAX_REPORTS_BYTES = 500 * 1024**2
"""Total size the stored reports may take; the oldest are removed beyond it."""

MIN_FREE_BYTES = 1024**3
"""No report is written while the data filesystem (often DSpace's own disk) has less free space."""


TELEGRAM_NO_BOTS = "Telegram has no bot yet: add a bot token and a chat id"
"""Why a report is not sent to Telegram when no destination has both a bot token and a chat id."""


class ReportSpaceError(ValueError):
    """The data filesystem is too full to write a report."""


TILE_CHART_WIDTH, TILE_CHART_HEIGHT = 440, 180
"""SVG viewBox of a two-per-row chart tile (close to its rendered width so text keeps its size)."""

WIDE_CHART_WIDTH, WIDE_CHART_HEIGHT = 900, 220
"""SVG viewBox of the full-width network chart."""

_RANGE_RE = re.compile(r"^\s*(\d+)\s*([hdm])\s*$", re.IGNORECASE)
_env: Environment | None = None


# ------------------------------------------------------------------------------ jinja


def _delta(value: Any, unit: str | None = None) -> str:
    """``+12`` / ``−3`` / ``±0``; with ``unit="bytes"`` the number is human sized."""
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if unit == "bytes":
        body = fmt_util.bytes_(abs(number))
    elif abs(number - round(number)) < 1e-9:
        body = fmt_util.number(abs(int(round(number))))
    else:
        body = fmt_util.number(abs(number), 1)
    if number > 0:
        return f"+{body}"
    if number < 0:
        return f"−{body}"
    return f"±{body}"


def _date(ts: float | None, tz: _dt.tzinfo | None = None) -> str:
    """ "07 Oct 2026" in ``tz`` (``server.timezone``; ``None``: the server's zone)."""
    return fmt_util.day(ts, tz)


def _date_time(ts: float | None, tz: _dt.tzinfo | None = None) -> str:
    """ "07 Oct 2026 21:29 +0530": the time in ``tz`` and the zone's name."""
    if ts is None:
        return "—"
    moment = aware(ts, tz)
    return f"{moment.strftime('%d %b %Y %H:%M')} {zone_label_of(moment)}"


def _clock(ts: float | None, tz: _dt.tzinfo | None = None) -> str:
    """ "21:29 +0530" (an incident's timeline)."""
    return fmt_util.clock(ts, tz=tz, zone=True)


@pass_context
def _date_filter(ctx: Context, ts: float | None) -> str:
    return _date(ts, ctx.get("tz"))


@pass_context
def _date_time_filter(ctx: Context, ts: float | None) -> str:
    return _date_time(ts, ctx.get("tz"))


@pass_context
def _clock_filter(ctx: Context, ts: float | None) -> str:
    return _clock(ts, ctx.get("tz"))


def _pct(value: Any, decimals: int = 0) -> str:
    if value is None:
        return "—"
    return fmt_util.pct(float(value), decimals)


def _number(value: Any, decimals: int = 0) -> str:
    if value is None:
        return "—"
    return fmt_util.number(value, decimals)


def _mdcell(value: Any) -> str:
    text = "—" if value is None else str(value)
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def _score(value: Any) -> str:
    """Health score with up to two decimals and no trailing zeros (``100``, ``99.96``, ``99.5``)."""
    if value is None:
        return "—"
    return f"{float(value):.2f}".rstrip("0").rstrip(".")


def _days(value: Any) -> str:
    if value is None:
        return "—"
    number = float(value)
    if number >= 3650:
        return "10+ years"
    if number >= 730:
        return f"{number / 365:.1f} years"
    if number >= 60:
        return f"{number / 30.4:.0f} months"
    return f"{number:.0f} days"


def jinja_env() -> Environment:
    """The (cached) Jinja environment: HTML templates autoescape, Markdown/JSON do not."""
    global _env
    if _env is None:
        env = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            autoescape=lambda name: bool(name and name.endswith(".html.j2")),
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )
        env.filters.update(
            {
                "bytes": fmt_util.bytes_,
                "gb": fmt_util.gb,
                "duration": fmt_util.duration,
                "pct": _pct,
                "ms": fmt_util.ms,
                "clock": _clock_filter,
                "datetime": _date_time_filter,
                "date": _date_filter,
                "number": _number,
                "rate": fmt_util.rate_bps,
                "delta": _delta,
                "days": _days,
                "score": _score,
                "mdcell": _mdcell,
                "status_label": lambda s: STATUS_LABELS.get(s or "", "No data"),
                "severity_label": lambda s: SEVERITY_LABELS.get(s or "", str(s or "")),
                "severity_status": lambda s: SEVERITY_STATUS.get(s or "", "unknown"),
                "verdict_label": lambda v: PORT_VERDICT_LABELS.get(v or "", (str(v or "—"), "plain"))[0],
                "verdict_status": lambda v: PORT_VERDICT_LABELS.get(v or "", ("", "plain"))[1],
                "expect_label": lambda e: PORT_EXPECT_LABELS.get(e or "", str(e or "—")),
                "support_label": lambda s: SUPPORT_LABELS.get(s or "", (str(s or "—"), "unknown"))[0],
                "support_status": lambda s: SUPPORT_LABELS.get(s or "", ("", "unknown"))[1],
            }
        )
        env.globals.update({"SECTION_LABELS": SECTION_LABELS, "ALL_SECTIONS": ALL_SECTIONS})
        _env = env
    return _env


# ------------------------------------------------------------------------------ charts


def build_charts(data: dict[str, Any], tz: _dt.tzinfo | None = None) -> dict[str, Markup]:
    """Render every inline SVG the HTML template needs, keyed by name (``cpu``, ``latency:rest`` …);
    the time ticks are in ``tz`` (``server.timezone``; ``None``: the server's zone)."""
    charts: dict[str, Markup] = {}
    meta_range = data.get("meta", {}).get("range", {})
    t_from = float(meta_range.get("from", 0.0))
    t_to = float(meta_range.get("to", t_from + 1.0))
    server = data.get("server") or {}
    series = server.get("series") or {}
    thresholds = server.get("thresholds") or {}
    common = {
        "t_from": t_from,
        "t_to": t_to,
        "width": TILE_CHART_WIDTH,
        "height": TILE_CHART_HEIGHT,
        "tz": tz,
    }
    for metric, label in (("cpu", "CPU"), ("mem", "Memory"), ("disk", "Disk"), ("swap", "Swap")):
        charts[metric] = Markup(
            svg.line_chart(
                [svg.Series(f"{label} %", series.get(metric, []), "chart-1")],
                unit="%",
                y_max=100.0,
                thresholds=thresholds.get(metric),
                title=f"{label} usage",
                **common,
            )
        )
    charts["load"] = Markup(
        svg.line_chart(
            [svg.Series("Load (1 min)", series.get("load", []), "chart-3")],
            unit="",
            thresholds=thresholds.get("load"),
            title="Load average",
            **common,
        )
    )
    net = series.get("net") or {}
    charts["net"] = Markup(
        svg.line_chart(
            [
                svg.Series("Received", net.get("rx", []), "chart-1"),
                svg.Series("Sent", net.get("tx", []), "chart-2"),
            ],
            unit="B/s",
            title="Network throughput",
            t_from=t_from,
            t_to=t_to,
            width=WIDE_CHART_WIDTH,
            height=WIDE_CHART_HEIGHT,
            tz=tz,
        )
    )
    for key, lat in (data.get("latency") or {}).items():
        points = [[p[0], p[1]] for p in lat.get("series") or []]
        charts[f"latency:{key}"] = Markup(
            svg.line_chart(
                [svg.Series(lat.get("label", key), points, "chart-1")],
                unit="ms",
                thresholds=lat.get("thresholds"),
                title=f"{lat.get('label', key)} response time",
                **common,
            )
        )
    for service in data.get("services") or []:
        charts[f"strip:{service['id']}"] = Markup(
            svg.strip(service.get("buckets") or [], title=f"{service['label']} availability")
        )
    for forecast in (data.get("repository") or {}).get("forecasts") or []:
        total = forecast.get("total") or 0
        free = forecast.get("free")
        used = (total - free) if (total and free is not None) else None
        pct_used = (100.0 * used / total) if (used is not None and total) else None
        status = "ok"
        if pct_used is not None:
            status = "crit" if pct_used >= 90 else "warn" if pct_used >= 80 else "ok"
        charts[f"meter:{forecast['id']}"] = Markup(
            svg.meter(used, total or 1, status=status, width=320, title=forecast.get("label", ""), unit="B")
        )
    return charts


# ------------------------------------------------------------------------------ rendering


def render(
    data: dict[str, Any],
    fmt_name: str,
    charts: dict[str, Markup] | None = None,
    tz: _dt.tzinfo | None = None,
) -> str:
    """Render a computed report dict as ``html`` | ``md`` | ``json`` text.

    Times are shown in ``tz`` (``server.timezone``; ``None``: the server's zone), each with the
    zone's name ("07 Oct 2026 21:29 +0530").
    """
    fmt_name = (fmt_name or "html").lower()
    if fmt_name not in VALID_FORMATS:
        raise ValueError(f"unknown report format {fmt_name!r}; expected one of {', '.join(VALID_FORMATS)}")
    if fmt_name == "json":
        return json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n"
    template = jinja_env().get_template(TEMPLATE_NAMES[fmt_name])
    meta = data.get("meta", {})
    context: dict[str, Any] = {
        "data": data,
        "meta": meta,
        "summary": data.get("summary", {}),
        "server": data.get("server", {}),
        "services": data.get("services", []),
        "latency": data.get("latency", {}),
        "alerts": data.get("alerts", []),
        "alert_stats": data.get("alert_stats", {}),
        "incidents": data.get("incidents", []),
        "repository": data.get("repository", {}),
        "logs": data.get("logs", {}),
        "recommendations": data.get("recommendations", []),
        "ssl": data.get("ssl"),
        "security": data.get("security") or {},
        "sections": set(meta.get("sections") or ALL_SECTIONS),
        "accent": MOOD_ACCENT.get((data.get("summary") or {}).get("mood") or "unknown", "sky"),
        "max_alert_rows": MAX_ALERT_ROWS,
        "max_port_rows": MAX_PORT_ROWS,
        "tz": tz,
    }
    if fmt_name == "html":
        context["charts"] = charts if charts is not None else build_charts(data, tz)
    return template.render(**context)


def default_reports_dir(store: Store | None = None) -> Path:
    """``<data dir>/reports`` next to the store's database file; ``paths.reports_dir()`` otherwise.

    Keeps the API, the scheduler hook and the CLI writing to the same place as the database
    they read, whatever ``SENTINEL_DATA_DIR`` says in this process.
    """
    db_path = getattr(store, "path", None)
    if db_path:
        return paths.reports_dir(Path(db_path).parent)
    return paths.reports_dir()


def report_filename(created_ts: float, label: str, fmt_name: str, tz: _dt.tzinfo | None = None) -> str:
    """``sentinel-report-YYYYMMDD-HHMM-<range>.<ext>`` (the time in ``tz``; ``None``: the server's zone)."""
    stamp = aware(created_ts, tz).strftime("%Y%m%d-%H%M")
    safe_label = re.sub(r"[^A-Za-z0-9]+", "", label) or "custom"
    return f"sentinel-report-{stamp}-{safe_label}.{fmt_name}"


def _unique_path(directory: Path, filename: str) -> Path:
    path = directory / filename
    if not path.exists():
        return path
    stem, ext = os.path.splitext(filename)
    for n in range(2, 1000):
        candidate = directory / f"{stem}-{n}{ext}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"cannot find a free file name for {filename}")


def write_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` through a temp file in the same directory (mode 0600) and rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".report-", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


# ------------------------------------------------------------------------------ generate


def generate(
    store: Store,
    cfg: Config,
    t_from: float,
    t_to: float,
    fmt: str = "html",
    title: str | None = None,
    sections: list[str] | None = None,
    send_telegram: bool = False,
    *,
    reports_dir: Path | str | None = None,
    now: float | None = None,
    range_hint: str | None = None,
) -> Report:
    """Generate a report for ``[t_from, t_to]`` in ``fmt`` (html|md|json), store the file and its row.

    Args:
        store, cfg: runtime store and effective configuration.
        t_from, t_to: epoch seconds (``t_to`` must be after ``t_from``).
        fmt: ``"html"`` (self-contained, printable), ``"md"`` or ``"json"``.
        title: optional report title.
        sections: section ids to include (``None`` = ``cfg.reports.default_sections``).
        send_telegram: also send the file through Telegram (synchronously; failures are recorded
            as events and never raise).
        reports_dir: where to write (default: ``reports/`` next to the store's database file).
        now: creation timestamp (defaults to the wall clock).
        range_hint: the range label the caller used (``"24h"`` …) so the file name matches it.

    Returns:
        The stored :class:`Report` (``id`` set).

    At most :data:`MAX_REPORTS` files (and :data:`MAX_REPORTS_BYTES`) are kept: the oldest are
    removed after a new one is written.

    Raises:
        ValueError: unknown format or empty range.
        ReportSpaceError: the data filesystem has less than :data:`MIN_FREE_BYTES` free.
    """
    fmt_name = (fmt or "html").lower()
    if fmt_name not in VALID_FORMATS:
        raise ValueError(f"unknown report format {fmt!r}; expected one of {', '.join(VALID_FORMATS)}")
    t_from, t_to = float(t_from), float(t_to)
    if t_to <= t_from:
        raise ValueError("report range is empty")
    now = float(now or time.time())
    directory = Path(reports_dir) if reports_dir else default_reports_dir(store)
    directory.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(directory).free
    if free < MIN_FREE_BYTES:
        raise ReportSpaceError(
            f"not enough free disk space for a report: {fmt_util.bytes_(free)} free on {directory} "
            f"(Sentinel keeps at least {fmt_util.bytes_(MIN_FREE_BYTES)} free)"
        )
    data = compute_report_data(
        store, cfg, t_from, t_to, title=title, sections=sections, now=now, range_hint=range_hint
    )
    zone = zone_of(cfg)
    text = render(data, fmt_name, tz=zone)
    path = _unique_path(directory, report_filename(now, data["meta"]["range"]["label"], fmt_name, zone))
    write_atomic(path, text)
    report = Report(
        id=None,
        created_ts=now,
        title=str(data["meta"]["title"]),
        range_from=t_from,
        range_to=t_to,
        fmt=fmt_name,  # type: ignore[arg-type]
        path=str(path),
        size=path.stat().st_size,
    )
    store.insert_report(report)
    try:
        store.add_event(
            "report",
            None,
            f"Report generated: {report.title} ({fmt_name}, {fmt_util.bytes_(report.size)})",
            {"report_id": report.id, "fmt": fmt_name, "path": str(path), "range": data["meta"]["range"]},
            ts=now,
        )
    except Exception:
        log.exception("could not record report event")
    log.info("report %s written to %s (%d bytes)", report.id, path, report.size)
    try:
        removed = store.prune_reports(MAX_REPORTS, MAX_REPORTS_BYTES)
        if removed:
            store.add_event(
                "report",
                None,
                f"Removed {removed} old report{'s' if removed != 1 else ''} to stay within "
                f"{MAX_REPORTS} reports / {fmt_util.bytes_(MAX_REPORTS_BYTES)}",
                {"removed": removed},
                ts=now,
            )
    except Exception:
        log.exception("could not prune old reports")
    if send_telegram:
        deliver_telegram(cfg, report, store=store)
    return report


def parse_range_string(range_str: str, now: float | None = None) -> tuple[float, float, str]:
    """``"24h"`` / ``"7d"`` / ``"30d"`` (also ``"1h"``, ``"6h"``, ``"14d"``, or any ``<n>h|<n>d|<n>m``).

    Returns ``(t_from, t_to, label)``; raises ``ValueError`` for anything else.
    """
    now = float(now or time.time())
    key = (range_str or "").strip().lower()
    if key in RANGE_SECONDS:
        return now - RANGE_SECONDS[key], now, key
    match = _RANGE_RE.match(key)
    if not match:
        raise ValueError(f"invalid range {range_str!r}; use 24h, 7d or 30d (or <n>h / <n>d)")
    amount = int(match.group(1))
    unit = match.group(2).lower()
    if amount <= 0:
        raise ValueError("range must be positive")
    seconds = amount * {"m": 60, "h": 3600, "d": 86400}[unit]
    if seconds > 366 * 86400:
        raise ValueError("range must be one year or less")
    return now - seconds, now, f"{amount}{unit}"


def generate_for_range(
    store: Store,
    cfg: Config,
    range_str: str = "24h",
    fmt: str = "html",
    title: str | None = None,
    sections: list[str] | None = None,
    send_telegram: bool = False,
    *,
    reports_dir: Path | str | None = None,
    now: float | None = None,
) -> Report:
    """CLI-friendly :func:`generate`: ``range_str`` is ``"24h"`` | ``"7d"`` | ``"30d"`` (or ``<n>h|<n>d``)."""
    t_from, t_to, label = parse_range_string(range_str, now)
    return generate(
        store,
        cfg,
        t_from,
        t_to,
        fmt,
        title,
        sections,
        send_telegram,
        reports_dir=reports_dir,
        now=t_to,
        range_hint=label,
    )


# ------------------------------------------------------------------------------ telegram


def telegram_caption(report: Report, tz: _dt.tzinfo | None = None) -> str:
    """Plain-text caption for the Telegram document (≤ 1024 characters; times in ``tz``)."""
    from dspace_sentinel.reports.analytics import range_label

    label = range_label(report.range_from, report.range_to)
    period = RANGE_TITLES.get(label) or range_title(label, report.range_from, report.range_to, tz)
    when = f"{_date_time(report.range_from, tz)} → {_date_time(report.range_to, tz)}"
    text = f"📄 {report.title}\n{period} · {when}"
    return text[:1024]


def _resolve_send_document(notifier: Any = None) -> tuple[Any, str | None]:
    if notifier is not None and callable(getattr(notifier, "send_document", None)):
        return notifier.send_document, None
    try:
        from dspace_sentinel.alerts.notifiers import telegram as telegram_module
    except Exception as exc:  # ImportError, or a module that fails to import while being built
        return None, f"Telegram notifier is not available ({exc.__class__.__name__}: {exc})"
    func = getattr(telegram_module, "send_document", None)
    if not callable(func):
        return None, "Telegram notifier has no send_document()"
    return func, None


def _call_send_document(func: Any, cfg: Config, path: str, caption: str) -> Any:
    """Call ``send_document`` whichever of the two SPEC-compatible signatures it has."""
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return func(cfg, path, caption)
    params = list(signature.parameters.values())
    names = [p.name for p in params]
    if params:
        first = params[0]
        annotation = str(first.annotation) if first.annotation is not inspect.Parameter.empty else ""
        if first.name.lower().startswith(("cfg", "config", "settings")) or "Config" in annotation:
            return func(cfg, path, caption)
    if "cfg" in names:
        return func(path, caption, cfg=cfg)
    if "config" in names:
        return func(path, caption, config=cfg)
    return func(path, caption)


def _normalise_result(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        out = dict(result)
        out["ok"] = bool(out.get("ok", True))
        return out
    if isinstance(result, bool):
        return {"ok": result, "error": None if result else "Telegram send failed"}
    if result is None:
        return {"ok": True, "error": None}
    return {"ok": True, "error": None, "result": str(result)}


async def send_report_telegram(
    cfg: Config, report: Report, caption: str | None = None, notifier: Any = None
) -> dict[str, Any]:
    """Send a report file through Telegram; returns ``{ok, error, ...}`` and never raises.

    Uses ``notifier.send_document`` when a notifier object is given, else
    ``dspace_sentinel.alerts.notifiers.telegram.send_document`` (imported lazily), which sends
    the file through every destination's own bot. Returns ``ok=false`` with a readable ``error``
    when Telegram is off, has no bot with a chat id, or is unavailable.
    """
    telegram = cfg.notifiers.telegram
    if not telegram.enabled:
        return {"ok": False, "error": "Telegram is not enabled", "skipped": True}
    if not telegram.destinations():
        return {"ok": False, "error": TELEGRAM_NO_BOTS, "skipped": True}
    if getattr(cfg, "demo", False):
        # Demo mode never sends anything; the delivery is logged (a ``notify.demo`` event) instead,
        # the same convention the alert notifiers follow.
        return {"ok": True, "error": None, "skipped": "demo"}
    func, reason = _resolve_send_document(notifier)
    if func is None:
        return {"ok": False, "error": reason or "Telegram notifier unavailable"}
    text = caption or telegram_caption(report, zone_of(cfg))
    try:
        result = _call_send_document(func, cfg, report.path, text)
        if inspect.isawaitable(result):
            result = await result
    except Exception as exc:
        log.warning("telegram send_document failed: %s", exc)
        return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
    return _normalise_result(result)


_DELIVERY_WORDS: dict[str, tuple[str, str, str, str]] = {
    # channel: (demo, sent, scheduled, not sent)
    "telegram": (
        "Report (demo, not sent): {title}",
        "Report sent to Telegram: {title}",
        "Report Telegram delivery scheduled: {title}",
        "Report not sent to Telegram: {error}",
    ),
    "email": (
        "Report by email (demo, not sent): {title}",
        "Report sent by email: {title}",
        "Report email delivery scheduled: {title}",
        "Report not sent by email: {error}",
    ),
}


def _record_delivery(
    store: Store | None, report: Report, delivery: dict[str, Any], channel: str = "telegram", cfg: Any = None
) -> None:
    if store is None:
        return
    demo, sent, scheduled, failed = _DELIVERY_WORDS[channel]
    try:
        if delivery.get("skipped") == "demo":
            data: dict[str, Any] = {"report_id": report.id, "channels": [channel]}
            if channel == "telegram":
                data["caption"] = telegram_caption(report, zone_of(cfg) if cfg is not None else None)
            elif cfg is not None:
                data["subject"] = report_email_content(cfg, report).subject
            store.add_event("notify.demo", None, demo.format(title=report.title), data)
            return
        # like alert deliveries: "notify" for what went out (or is on its way), "notify.error" else
        kind = "notify"
        if delivery.get("ok"):
            message = sent.format(title=report.title)
        elif delivery.get("pending"):
            message = scheduled.format(title=report.title)
        else:
            kind, message = "notify.error", failed.format(error=delivery.get("error"))
        store.add_event(kind, None, message, {"report_id": report.id, "channel": channel, **delivery})
    except Exception:
        log.exception("could not record the %s delivery event", channel)


async def deliver_telegram_async(
    cfg: Config, report: Report, store: Store | None = None, notifier: Any = None
) -> dict[str, Any]:
    """:func:`send_report_telegram` plus an ``events`` row describing the outcome."""
    delivery = await send_report_telegram(cfg, report, notifier=notifier)
    _record_delivery(store, report, delivery, "telegram", cfg)
    return delivery


def deliver_telegram(
    cfg: Config, report: Report, store: Store | None = None, notifier: Any = None
) -> dict[str, Any]:
    """Synchronous :func:`deliver_telegram_async` for the CLI and ``generate(send_telegram=True)``.

    Runs the coroutine in a private event loop when none is running in this thread; when called
    from inside a running loop the send is scheduled as a task and ``{"ok": None, "pending": True}``
    is returned.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        try:
            return asyncio.run(deliver_telegram_async(cfg, report, store, notifier))
        except Exception as exc:
            delivery = {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
            _record_delivery(store, report, delivery, "telegram", cfg)
            return delivery
    loop.create_task(deliver_telegram_async(cfg, report, store, notifier))
    delivery = {"ok": None, "pending": True, "error": None}
    _record_delivery(store, report, delivery, "telegram", cfg)
    return delivery


# ------------------------------------------------------------------------------ email


def report_email_content(cfg: Config, report: Report) -> Any:
    """The email a report file is attached to (subject "[Sentinel] Report · host · title")."""
    from dspace_sentinel.alerts.email_messages import report_email
    from dspace_sentinel.reports.analytics import range_label

    zone = zone_of(cfg)
    label = range_label(report.range_from, report.range_to)
    period = RANGE_TITLES.get(label) or range_title(label, report.range_from, report.range_to, zone)
    when = f"{period} · {_date_time(report.range_from, zone)} → {_date_time(report.range_to, zone)}"
    return report_email(
        report.title, cfg.hostname, when, Path(report.path).name, fmt_util.bytes_(report.size)
    )


async def send_report_email(cfg: Config, report: Report) -> dict[str, Any]:
    """Email a report file to every recipient (whatever their ``min_severity``); never raises.

    Returns ``{ok, error, results}`` (``results``: ``{address, label, ok, error}`` per recipient),
    ``{ok: False, error, skipped: True}`` when email is off or not set up, and
    ``{ok: True, error: None, skipped: "demo"}`` in demo mode (nothing is sent).
    """
    email_cfg = cfg.notifiers.email
    if not email_cfg.enabled:
        return {"ok": False, "error": "email is not enabled", "skipped": True}
    try:
        from dspace_sentinel.alerts.notifiers import email as mail
    except Exception as exc:  # a module that fails to import while being built
        return {
            "ok": False,
            "error": f"the email notifier is not available ({exc.__class__.__name__}: {exc})",
        }
    reason = mail.unready(cfg)
    if reason:
        return {"ok": False, "error": reason, "skipped": True}
    if getattr(cfg, "demo", False):
        return {"ok": True, "error": None, "skipped": "demo"}
    try:
        result = await mail.send_file(cfg, report_email_content(cfg, report), report.path)
    except Exception as exc:  # send_file never raises, but a delivery must never fail the caller
        log.warning("report email failed: %s", exc)
        return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
    return {
        "ok": bool(result.get("ok")),
        "error": result.get("error"),
        "results": list(result.get("results") or []),
    }


async def deliver_email_async(cfg: Config, report: Report, store: Store | None = None) -> dict[str, Any]:
    """:func:`send_report_email` plus an ``events`` row describing the outcome."""
    delivery = await send_report_email(cfg, report)
    _record_delivery(store, report, delivery, "email", cfg)
    return delivery


def deliver_email(cfg: Config, report: Report, store: Store | None = None) -> dict[str, Any]:
    """Synchronous :func:`deliver_email_async` for the CLI (same rules as :func:`deliver_telegram`)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        try:
            return asyncio.run(deliver_email_async(cfg, report, store))
        except Exception as exc:
            delivery = {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
            _record_delivery(store, report, delivery, "email", cfg)
            return delivery
    loop.create_task(deliver_email_async(cfg, report, store))
    delivery = {"ok": None, "pending": True, "error": None}
    _record_delivery(store, report, delivery, "email", cfg)
    return delivery


__all__ = [
    "MAX_ALERT_ROWS",
    "MAX_PORT_ROWS",
    "MAX_REPORTS",
    "MAX_REPORTS_BYTES",
    "MEDIA_TYPES",
    "MIN_FREE_BYTES",
    "MOOD_ACCENT",
    "PORT_EXPECT_LABELS",
    "PORT_VERDICT_LABELS",
    "SEVERITY_LABELS",
    "STATUS_LABELS",
    "SUPPORT_LABELS",
    "TEMPLATES_DIR",
    "VALID_FORMATS",
    "ReportSpaceError",
    "build_charts",
    "default_reports_dir",
    "deliver_email",
    "deliver_email_async",
    "deliver_telegram",
    "deliver_telegram_async",
    "generate",
    "generate_for_range",
    "jinja_env",
    "parse_range_string",
    "render",
    "report_email_content",
    "report_filename",
    "send_report_email",
    "send_report_telegram",
    "telegram_caption",
    "write_atomic",
]
