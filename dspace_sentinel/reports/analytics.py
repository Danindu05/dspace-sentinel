"""Report analytics (SPEC section 8): turn the store's history into one plain data dict.

:func:`compute_report_data` is the single entry point. Everything it returns is JSON
serialisable (the JSON report *is* this dict) and every number the HTML and Markdown
templates show comes from here, so the three formats can never disagree.

The module only reads the store. It never raises on missing data: an empty period yields
``None`` values and empty lists, and the recommendations say so.
"""

from __future__ import annotations

import logging
import math
import re
import time
from collections import Counter
from typing import TYPE_CHECKING, Any

from dspace_sentinel import __version__, health
from dspace_sentinel.models import STATUS_RANK, Alert, Incident, worst
from dspace_sentinel.util.tz import zone_label, zone_name, zone_of

if TYPE_CHECKING:  # pragma: no cover
    from dspace_sentinel.config import Config
    from dspace_sentinel.store import Store

log = logging.getLogger(__name__)

HEALTH_WEIGHTS: dict[str, int] = {"backend": 30, "postgres": 25, "solr": 15, "frontend": 15, "nginx": 10}
"""Weight of each component's availability in the health score (SPEC section 8)."""

DEFAULT_WEIGHT = 5
"""Weight of every other component (server, ssl, ports, logs, repository, assetstore)."""

AVAILABILITY_TARGET_PCT = 99.5
"""Below this availability a service earns a recommendation."""

DISK_FULL_HORIZON_DAYS = 60
"""A disk predicted to fill within this many days earns a recommendation."""

PG_CONNECTIONS_WARN_PCT = 70.0
"""PostgreSQL connection usage (p95, percent of max_connections) above which we recommend action."""

CURRENT_VALUE_SLACK_S = 600
"""Only a range ending this close to "now" may stand in the current value for missing history."""

LATENCY_CHECKS: tuple[tuple[str, str, str, str, str], ...] = (
    ("rest", "backend.rest", "REST API", "backend", "rest_latency_ms"),
    ("search", "backend.search", "Search API", "backend", "search_latency_ms"),
    ("home", "frontend.home", "Homepage", "frontend", "home_latency_ms"),
    ("postgres", "postgres.query", "PostgreSQL query", "postgres", ""),
    ("solr", "solr.search", "Solr search", "solr", "search_latency_ms"),
)
"""``(key, check id, label, module, alerts threshold key)`` of every latency the report analyses."""

RANGE_SECONDS: dict[str, int] = {
    "1h": 3600,
    "6h": 6 * 3600,
    "24h": 86400,
    "7d": 7 * 86400,
    "14d": 14 * 86400,
    "30d": 30 * 86400,
}
"""Named ranges and their length in seconds (the report file name carries the label)."""

RANGE_TITLES: dict[str, str] = {
    "1h": "Last hour",
    "6h": "Last 6 hours",
    "24h": "Last 24 hours",
    "7d": "Last 7 days",
    "14d": "Last 14 days",
    "30d": "Last 30 days",
}

ALL_SECTIONS: tuple[str, ...] = (
    "summary",
    "server",
    "services",
    "latency",
    "alerts",
    "incidents",
    "repository",
    "logs",
    "security",
    "recommendations",
)
"""Every section a report can contain, in document order."""

SECTION_LABELS: dict[str, tuple[str, str]] = {
    "summary": ("📋", "Summary"),
    "server": ("💻", "Server"),
    "services": ("📚", "Services"),
    "latency": ("⏱️", "Response times"),
    "alerts": ("⚠️", "Alerts"),
    "incidents": ("🧭", "Incidents"),
    "repository": ("🗄️", "Repository and storage"),
    "logs": ("📜", "Logs"),
    "security": ("🛡️", "Security"),
    "recommendations": ("💡", "Recommendations"),
}
"""Section id → (emoji, heading) used by the templates and the report panel."""

SEVERITY_RANK: dict[str, int] = {"critical": 3, "warning": 2, "info": 1, "ok": 0}

SECURITY_RUNGS: tuple[tuple[str, str], ...] = (
    ("security.ports", "Open ports"),
    ("security.firewall", "Firewall"),
    ("security.ssh", "SSH"),
    ("security.web", "Web"),
    ("security.updates", "Updates"),
    ("security.software", "Software"),
    ("security.files", "DSpace files"),
    ("security.sentinel", "Sentinel"),
)
"""``(rung id, label)`` of the Security component's ladder, in order (its rung history is read by id)."""

PORT_VERDICT_RANK: dict[str, int] = {"new": 0, "exposed": 1, "expected": 2, "local": 3}
"""Order of the open-ports table: what needs a look first."""

_MAX_CHECK_ROWS = 200_000
_TS_PREFIXES = (
    re.compile(r"^\d{4}[-/]\d{2}[-/]\d{2}[ T]\d{2}:\d{2}:\d{2}[.,]?\d*\s*"),
    re.compile(r"^\d{2}-[A-Za-z]{3}-\d{4} \d{2}:\d{2}:\d{2}\.\d+\s*"),
    re.compile(r"^\[[^\]]{0,40}\]\s*"),
)
_THREAD_RE = re.compile(r"\((?:qtp|http-nio|pool|Thread)[^)]*\)\s*|\[(?:http-nio|pool|Thread)[^\]]*\]\s*")
_PIDS_RE = re.compile(r"\b\d{3,}#\d{3,}:\s*\*?\d*\s*")


# ------------------------------------------------------------------------------ small maths


def percentile(values: list[float], p: float) -> float | None:
    """Linear-interpolated percentile ``p`` (0-100) of ``values``; ``None`` for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    k = (len(ordered) - 1) * (max(0.0, min(100.0, p)) / 100.0)
    lo = int(math.floor(k))
    hi = min(lo + 1, len(ordered) - 1)
    return float(ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo))


def mean(values: list[float]) -> float | None:
    """Arithmetic mean, ``None`` for an empty list."""
    return (sum(values) / len(values)) if values else None


def linear_slope(points: list[tuple[float, float]]) -> float | None:
    """Least-squares slope (value units per x unit) of ``(x, y)`` points; ``None`` below two distinct x."""
    if len(points) < 2:
        return None
    n = float(len(points))
    mean_x = sum(p[0] for p in points) / n
    mean_y = sum(p[1] for p in points) / n
    sxx = sum((p[0] - mean_x) ** 2 for p in points)
    if sxx <= 0:
        return None
    sxy = sum((p[0] - mean_x) * (p[1] - mean_y) for p in points)
    return sxy / sxx


def growth_per_day(points: list[tuple[float, float]]) -> float | None:
    """Slope of ``(epoch seconds, value)`` points expressed per day."""
    slope = linear_slope(points)
    return None if slope is None else slope * 86400.0


def days_until_full(free: float | None, per_day: float | None) -> float | None:
    """Days until ``free`` bytes are consumed at ``per_day`` bytes/day (``None`` when not growing)."""
    if free is None or per_day is None or per_day <= 0:
        return None
    return max(0.0, float(free) / float(per_day))


def health_score(services: list[dict[str, Any]]) -> float | None:
    """Weighted mean availability (0-100): backend 30, postgres 25, solr 15, frontend 15, nginx 10, rest 5."""
    total_w = 0.0
    acc = 0.0
    for svc in services:
        avail = svc.get("availability_pct")
        if avail is None:
            continue
        w = float(HEALTH_WEIGHTS.get(svc["id"], DEFAULT_WEIGHT))
        total_w += w
        acc += w * float(avail)
    if total_w <= 0:
        return None
    return round(acc / total_w, 2)


# ------------------------------------------------------------------------------ range labels


def range_label(t_from: float, t_to: float, hint: str | None = None) -> str:
    """Short label for a range: a named one (``24h``, ``7d``…) when the span matches, else ``36h``/``3d``."""
    span = max(1.0, float(t_to) - float(t_from))
    if hint and hint in RANGE_SECONDS and abs(span - RANGE_SECONDS[hint]) <= max(120.0, span * 0.02):
        return hint
    for key, seconds in RANGE_SECONDS.items():
        if abs(span - seconds) <= max(60.0, seconds * 0.01):
            return key
    hours = span / 3600.0
    if hours < 48:
        return f"{max(1, int(round(hours)))}h"
    return f"{max(1, int(round(hours / 24.0)))}d"


def range_title(label: str, t_from: float, t_to: float, tz: Any = None) -> str:
    """Human heading for a range: ``"Last 7 days"`` or ``"2026-09-17 10:00 → 2026-09-24 10:00 UTC"``
    (in ``tz``, ``server.timezone``; ``None``: the server's zone)."""
    if label in RANGE_TITLES:
        return RANGE_TITLES[label]
    from dspace_sentinel.util.fmt import date_time

    return f"{date_time(t_from, tz)} → {date_time(t_to, tz, zone=True)}"


def resolve_sections(cfg: Config, sections: list[str] | None) -> list[str]:
    """The sections a report renders, in document order (unknown names dropped, defaults from config)."""
    wanted = [str(s).strip().lower() for s in (sections or []) if str(s).strip()]
    if not wanted:
        wanted = list(cfg.reports.default_sections)
    chosen = [s for s in ALL_SECTIONS if s in wanted]
    return chosen or ["summary"]


# ------------------------------------------------------------------------------ helpers


def _buckets_for(span: float) -> int:
    if span <= 6 * 3600:
        return 36
    if span <= 86400:
        return 48
    if span <= 7 * 86400:
        return 84
    return 60


def _series_points(store: Store, metric: str, t_from: float, t_to: float, bucket_s: int | None = None) -> Any:
    """``store.series`` with a raw-sample fallback for long ranges whose hourly roll-up is not built yet."""
    data = store.series(metric, t_from, t_to, bucket_s)
    empty = not data.get("points") if metric != "net" else not (data.get("series") or {}).get("rx")
    if empty and (t_to - t_from) > 7 * 86400:
        bucket = data["bucket_s"]
        if metric == "net":
            rx: list[list[Any]] = []
            tx: list[list[Any]] = []
        merged: list[list[Any]] = []
        chunk_from = t_from
        while chunk_from < t_to:
            chunk_to = min(t_to, chunk_from + 7 * 86400 - 1)
            part = store.series(metric, chunk_from, chunk_to, bucket)
            if metric == "net":
                rx.extend(part["series"]["rx"])
                tx.extend(part["series"]["tx"])
            else:
                merged.extend(part["points"])
            chunk_from = chunk_to + 1
        if metric == "net":
            data["series"] = {"rx": rx, "tx": tx}
        else:
            data["points"] = merged
    return data


def _fine_bucket(span: float) -> int:
    """A finer bucket than the chart bucket, for percentiles (≤ ~1800 points)."""
    return int(max(60, math.ceil(span / 1800.0 / 60.0) * 60))


def _round(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(float(value), digits)


def _status_of(value: float | None, warning: float, critical: float) -> str | None:
    if value is None:
        return None
    return health.threshold_status(value, warning, critical)


def _alert_dict(alert: Alert, now: float) -> dict[str, Any]:
    data = alert.to_dict()
    end = alert.resolved_ts if alert.resolved_ts is not None else now
    data["duration_s"] = max(0, int(end - alert.opened_ts)) if alert.opened_ts is not None else None
    data["component_label"] = health.component_label(alert.component)
    data["component_emoji"] = health.meta_for(alert.component).emoji
    return data


def _incident_dict(incident: Incident, alerts: list[Alert], now: float) -> dict[str, Any]:
    data = incident.to_dict()
    end = incident.ended_ts if incident.ended_ts is not None else now
    data["duration_s"] = max(0, int(end - incident.started_ts))
    data["alerts"] = [_alert_dict(a, now) for a in alerts]
    data["alert_count"] = len(alerts)
    return data


def normalise_log_line(line: str) -> str:
    """Strip timestamps, thread names and pids from a log line so equal errors count together."""
    text = str(line or "").strip()
    for pattern in _TS_PREFIXES:
        text = pattern.sub("", text, count=1)
    text = _THREAD_RE.sub("", text)
    text = _PIDS_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:200]


# ------------------------------------------------------------------------------ sections


def _server(store: Store, cfg: Config, t_from: float, t_to: float) -> dict[str, Any]:
    span = t_to - t_from
    stats = store.sample_stats(t_from, t_to)
    n = int(stats.get("n") or 0)
    fine = _fine_bucket(span)
    fine_series: dict[str, list[float]] = {}
    fine_max: dict[str, list[float]] = {}
    for metric in ("cpu", "mem", "disk", "load", "swap"):
        try:
            data = _series_points(store, metric, t_from, t_to, fine)
        except Exception:
            log.exception("series %s failed", metric)
            data = {"points": []}
        fine_series[metric] = [float(p[1]) for p in data.get("points", []) if p[1] is not None]
        fine_max[metric] = [float(p[2]) for p in data.get("points", []) if len(p) > 2 and p[2] is not None]

    series: dict[str, Any] = {}
    for metric in ("cpu", "mem", "disk", "load", "swap"):
        try:
            data = _series_points(store, metric, t_from, t_to)
            series[metric] = [[int(p[0]), p[1]] for p in data.get("points", [])]
        except Exception:
            series[metric] = []
    try:
        net = _series_points(store, "net", t_from, t_to)
        series["net"] = {
            "rx": [[int(p[0]), p[1]] for p in net["series"]["rx"]],
            "tx": [[int(p[0]), p[1]] for p in net["series"]["tx"]],
        }
    except Exception:
        series["net"] = {"rx": [], "tx": []}

    latest = store.latest_sample_row() or {}
    extra = latest.get("extra") or {}
    cores = int(extra.get("cores") or 0) or None
    disk_total = latest.get("disk_total")
    disk_used = latest.get("disk_used")
    disk_free = extra.get("disk_free")
    if disk_free is None and disk_total is not None and disk_used is not None:
        disk_free = int(disk_total) - int(disk_used)

    def block(metric: str, avg_key: str, max_key: str) -> dict[str, Any]:
        values = fine_series.get(metric, [])
        peak = fine_max.get(metric, [])
        return {
            "avg": _round(stats.get(avg_key)) if n else _round(mean(values)),
            "p95": _round(percentile(values, 95)),
            "max": _round(stats.get(max_key)) if n else _round(max(peak) if peak else None),
            "min": _round(min(values) if values else None),
        }

    cpu = block("cpu", "cpu_avg", "cpu_max")
    mem = block("mem", "mem_avg", "mem_max")
    disk = block("disk", "disk_avg", "disk_max")
    load = block("load", "load_avg", "load_max")
    swap = block("swap", "swap_avg", "swap_max")
    swap_values = fine_series.get("swap", [])
    swap["used_fraction"] = (
        round(sum(1 for v in swap_values if v > 0.0) / len(swap_values), 3) if swap_values else None
    )
    load["cores"] = cores
    load["per_core_p95"] = _round(load["p95"] / cores, 2) if (cores and load["p95"] is not None) else None

    disk_points = [(float(p[0]), float(p[1])) for p in series.get("disk", []) if p[1] is not None]
    disk_growth_pct = growth_per_day(disk_points)
    disk_growth = (disk_growth_pct / 100.0 * float(disk_total)) if (disk_growth_pct and disk_total) else None
    disk.update(
        {
            "total": disk_total,
            "used_now": disk_used,
            "free_now": disk_free,
            "path": extra.get("disk_path") or "/",
            "growth_per_day": None if disk_growth is None else int(disk_growth),
            "days_until_full": _round(days_until_full(disk_free, disk_growth), 1),
        }
    )

    comp = store.component_state("server") or {}
    metrics = comp.get("metrics") or {}
    net_block = {"rx_avg": _round(stats.get("rx_avg"), 0), "tx_avg": _round(stats.get("tx_avg"), 0)}
    expected_buckets = max(1, int(math.ceil(span / float(fine))))
    coverage = min(100.0, 100.0 * len(fine_series.get("cpu", [])) / expected_buckets) if n else 0.0
    th = cfg.alerts
    n_cores = cores or 1
    thresholds = {
        "cpu": {"warning": th.cpu.warning, "critical": th.cpu.critical},
        "mem": {"warning": th.memory.warning, "critical": th.memory.critical},
        "disk": {"warning": th.disk.warning, "critical": th.disk.critical},
        "swap": {"warning": th.swap.warning, "critical": th.swap.critical},
        "load": {
            "warning": th.load_per_core.warning * n_cores,
            "critical": th.load_per_core.critical * n_cores,
            "per_core": True,
        },
    }
    return {
        "samples": n,
        "coverage_pct": round(coverage, 1),
        "thresholds": thresholds,
        "cpu": cpu,
        "mem": mem,
        "swap": swap,
        "disk": disk,
        "load": load,
        "net": net_block,
        "mounts": list(extra.get("disks") or []),
        "uptime_s": extra.get("uptime_s"),
        "os": metrics.get("os"),
        "kernel": metrics.get("kernel"),
        "cores": cores,
        "mem_total": latest.get("mem_total"),
        "series": series,
    }


def _services(
    store: Store, cfg: Config, t_from: float, t_to: float, enabled: list[str]
) -> list[dict[str, Any]]:
    span = t_to - t_from
    buckets = _buckets_for(span)
    out: list[dict[str, Any]] = []
    for cid in enabled:
        meta = health.meta_for(cid)
        try:
            avail = store.availability(cid, t_from, t_to, buckets)
        except Exception:
            log.exception("availability of %s failed", cid)
            continue
        strip = avail.get("buckets") or []
        seen = [s for s in strip if s]
        worst_status = worst(seen) if seen else None
        avg_latency: float | None = None
        max_latency: float | None = None
        headline = health.HEADLINE_LATENCY.get(cid)
        if headline:
            try:
                lat = store.latency_series(headline, t_from, t_to)
                avgs = [float(p[1]) for p in lat["points"] if p[1] is not None]
                maxes = [float(p[2]) for p in lat["points"] if p[2] is not None]
                avg_latency = _round(mean(avgs))
                max_latency = _round(max(maxes) if maxes else None)
            except Exception:
                log.exception("latency of %s failed", cid)
        out.append(
            {
                "id": cid,
                "label": meta.label,
                "emoji": meta.emoji,
                "availability_pct": avail.get("uptime_pct"),
                "downtime_s": int(avail.get("downtime_s") or 0),
                "outages": int(avail.get("outages") or 0),
                "samples": int(avail.get("samples") or 0),
                "worst_status": worst_status,
                "avg_latency_ms": avg_latency,
                "max_latency_ms": max_latency,
                "weight": HEALTH_WEIGHTS.get(cid, DEFAULT_WEIGHT),
                "bucket_s": avail.get("bucket_s"),
                "buckets": strip,
            }
        )
    return out


def _latency(store: Store, cfg: Config, t_from: float, t_to: float) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, check_id, label, module, th_key in LATENCY_CHECKS:
        if not cfg.module_enabled(module):
            continue
        try:
            rows = store.checks_between(t_from, t_to, check_id, limit=_MAX_CHECK_ROWS)
        except Exception:
            log.exception("checks for %s failed", check_id)
            rows = []
        values = [float(r["latency_ms"]) for r in rows if r.get("latency_ms") is not None]
        failures = sum(1 for r in rows if r.get("status") == "crit")
        try:
            series = store.latency_series(check_id, t_from, t_to)["points"]
        except Exception:
            series = []
        thresholds: dict[str, float] | None = None
        if th_key:
            th = getattr(cfg.alerts, th_key)
            thresholds = {"warning": float(th.warning), "critical": float(th.critical)}
        p95 = percentile(values, 95)
        out[key] = {
            "check": check_id,
            "label": label,
            "component": module,
            "samples": len(rows),
            "failures": failures,
            "p50": _round(percentile(values, 50)),
            "p95": _round(p95),
            "avg": _round(mean(values)),
            "max": _round(max(values) if values else None),
            "thresholds": thresholds,
            "status": (
                _status_of(p95, thresholds["warning"], thresholds["critical"]) if thresholds else None
            ),
            "series": series,
        }
    return out


def _alerts(
    store: Store, t_from: float, t_to: float, now: float
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        alerts = store.alerts_between(t_from, t_to)
    except Exception:
        log.exception("alerts query failed")
        alerts = []
    items = [_alert_dict(a, now) for a in alerts]
    items.sort(key=lambda a: (a.get("opened_ts") or 0, a.get("id") or 0), reverse=True)
    by_component: Counter[str] = Counter(a["component_label"] for a in items)
    by_rule: Counter[str] = Counter(a["rule"].split(":", 1)[0] for a in items)
    durations = [a["duration_s"] for a in items if a.get("resolved_ts") is not None and a.get("duration_s")]
    stats = {
        "opened": len(items),
        "critical": sum(1 for a in items if a["severity"] == "critical"),
        "warning": sum(1 for a in items if a["severity"] == "warning"),
        "still_open": sum(1 for a in items if a["status"] == "open"),
        "acked": sum(1 for a in items if a.get("acked_ts")),
        "mean_time_to_resolve_s": int(mean(durations)) if durations else None,
        "longest_s": max(durations) if durations else None,
        "by_component": dict(by_component.most_common()),
        "by_rule": dict(by_rule.most_common()),
    }
    return items, stats


def _incidents(store: Store, t_from: float, t_to: float, now: float) -> list[dict[str, Any]]:
    try:
        incidents = [i for i in store.incidents("all", limit=1000, since=t_from) if i.started_ts <= t_to]
    except Exception:
        log.exception("incidents query failed")
        incidents = []
    out: list[dict[str, Any]] = []
    for inc in incidents:
        try:
            alerts = store.alerts_for_incident(inc.id) if inc.id is not None else []
        except Exception:
            alerts = []
        out.append(_incident_dict(inc, alerts, now))
    return out


def _repository(
    store: Store, cfg: Config, t_from: float, t_to: float, server: dict[str, Any]
) -> dict[str, Any]:
    enabled = cfg.module_enabled("repository")
    latest: dict[str, Any] | None = None
    delta: dict[str, Any] = {}
    series: dict[str, list[list[float]]] = {}
    if enabled:
        try:
            latest = store.latest_repository_stats()
            delta = store.repository_delta(t_from, t_to)
            bucket = 3600 if (t_to - t_from) <= 7 * 86400 else 6 * 3600
            series = store.repository_series(t_from, t_to, bucket)
        except Exception:
            log.exception("repository queries failed")
    days = float(delta.get("days") or 0.0)
    growth: dict[str, float | None] = {}
    for key in ("items", "bitstreams", "communities", "collections", "db_size", "assetstore_bytes"):
        value = delta.get(key)
        growth[key] = round(value / days, 2) if (value is not None and days >= 0.5) else None

    forecasts: list[dict[str, Any]] = []
    disk = server.get("disk") or {}
    if disk.get("total"):
        forecasts.append(
            {
                "id": "root",
                "label": f"Disk {disk.get('path') or '/'}",
                "total": disk.get("total"),
                "free": disk.get("free_now"),
                "pct": disk.get("max"),
                "growth_per_day": disk.get("growth_per_day"),
                "days_until_full": disk.get("days_until_full"),
            }
        )
    asset_state = store.component_state("assetstore") if cfg.module_enabled("assetstore") else None
    asset_metrics = (asset_state or {}).get("metrics") or {}
    asset_growth = asset_metrics.get("growth_bytes_per_day")
    if asset_growth is None:
        asset_growth = growth.get("assetstore_bytes")
    asset_total = asset_metrics.get("total")
    asset_free = asset_metrics.get("free")
    if asset_total is None:
        for mount in server.get("mounts") or []:
            if mount.get("path") and cfg.dspace.assetstore_dir.startswith(str(mount["path"])):
                asset_total, asset_free = mount.get("total"), mount.get("free")
                break
    if asset_total is not None and asset_free is not None:
        forecasts.append(
            {
                "id": "assetstore",
                "label": f"Assetstore {asset_metrics.get('mount') or cfg.dspace.assetstore_dir}",
                "total": asset_total,
                "free": asset_free,
                "pct": asset_metrics.get("pct"),
                "growth_per_day": None if asset_growth is None else int(asset_growth),
                "days_until_full": _round(days_until_full(asset_free, asset_growth), 1),
            }
        )
    horizons = [f["days_until_full"] for f in forecasts if f.get("days_until_full") is not None]
    return {
        "enabled": enabled,
        "now": latest,
        "delta": {
            k: delta.get(k)
            for k in ("items", "bitstreams", "communities", "collections", "db_size", "assetstore_bytes")
        },
        "delta_days": round(days, 2),
        "growth_per_day": growth,
        "forecasts": forecasts,
        "days_until_disk_full": min(horizons) if horizons else None,
        "series": {k: series.get(k, []) for k in ("items", "bitstreams", "db_size", "assetstore_bytes")},
    }


def _logs(
    store: Store, cfg: Config, t_from: float, t_to: float, alerts: list[dict[str, Any]]
) -> dict[str, Any]:
    enabled = cfg.module_enabled("logs")
    window_s = float(cfg.logs.window_minutes * 60)
    interval_s = float(cfg.collector.logs_interval)
    factor = min(1.0, interval_s / window_s) if window_s > 0 else 1.0
    th = cfg.alerts.log_errors_per_window
    mid = (t_from + t_to) / 2.0
    files: list[dict[str, Any]] = []
    first_half = second_half = 0.0
    if enabled:
        for lf in cfg.logs.files:
            check_id = f"logs.{lf.id}"
            try:
                rows = store.checks_between(t_from, t_to, check_id, limit=_MAX_CHECK_ROWS)
            except Exception:
                log.exception("log checks for %s failed", check_id)
                rows = []
            values = [(float(r["ts"]), float(r["value"])) for r in rows if r.get("value") is not None]
            total = sum(v for _, v in values) * factor
            first = sum(v for ts, v in values if ts < mid) * factor
            second = sum(v for ts, v in values if ts >= mid) * factor
            first_half += first
            second_half += second
            peak = max((v for _, v in values), default=None)
            files.append(
                {
                    "id": lf.id,
                    "label": lf.label or lf.id,
                    "path": lf.path,
                    "samples": len(rows),
                    "missing": bool(rows) and all(r.get("status") == "unknown" for r in rows),
                    "errors_estimate": int(round(total)),
                    "peak_per_window": None if peak is None else int(peak),
                    "windows_over_warning": sum(1 for _, v in values if v >= th.warning),
                    "windows_over_critical": sum(1 for _, v in values if v >= th.critical),
                    "first_half": int(round(first)),
                    "second_half": int(round(second)),
                }
            )
    counter: Counter[str] = Counter()
    sources: dict[str, str] = {}
    for alert in alerts:
        base_rule = str(alert.get("rule") or "").split(":", 1)[0]  # log_errors:<file id>
        if base_rule == "log_errors" and alert.get("message"):
            key = normalise_log_line(alert["message"])
            if key:
                counter[key] += 1
                sources.setdefault(key, alert.get("component_label") or "Logs")
    state = store.component_state("logs") if enabled else None
    for fid, info in ((state or {}).get("metrics") or {}).get("files", {}).items():
        last = (info or {}).get("last_error") or {}
        line = last.get("line")
        ts = last.get("ts")
        if line and (ts is None or t_from <= float(ts) <= t_to):
            key = normalise_log_line(line)
            if key:
                counter[key] += 1
                sources.setdefault(key, str((info or {}).get("label") or fid))
    top_errors = [
        {"text": text, "count": n, "source": sources.get(text, "")} for text, n in counter.most_common(10)
    ]
    trend_up = second_half >= 10 and second_half > first_half * 1.5
    return {
        "enabled": enabled,
        "window_minutes": cfg.logs.window_minutes,
        "files": files,
        "totals": {
            "errors_estimate": int(round(sum(f["errors_estimate"] for f in files))),
            "peak_per_window": max((f["peak_per_window"] or 0) for f in files) if files else None,
            "windows_over_warning": sum(f["windows_over_warning"] for f in files),
            "windows_over_critical": sum(f["windows_over_critical"] for f in files),
        },
        "trend": {
            "first_half": int(round(first_half)),
            "second_half": int(round(second_half)),
            "up": trend_up,
        },
        "top_errors": top_errors,
    }


def _pg_connections(
    store: Store, cfg: Config, t_from: float, t_to: float, now: float
) -> dict[str, Any] | None:
    """p95/max of the ``postgres.connections`` history in the range (percent of max_connections).

    Without history the current value stands in, but only for a range that ends now
    (``source: "current"``); a past period without history is not judged at all.
    """
    if not cfg.module_enabled("postgres"):
        return None
    values: list[float] = []
    try:
        rows = store.checks_between(t_from, t_to, "postgres.connections", limit=_MAX_CHECK_ROWS)
        values = [float(r["value"]) for r in rows if r.get("value") is not None]
    except Exception:
        log.exception("postgres.connections history failed")
    source = "history"
    if not values:
        if t_to < now - CURRENT_VALUE_SLACK_S:
            return None
        state = store.component_state("postgres") or {}
        pct = (state.get("metrics") or {}).get("connections_pct")
        if pct is None:
            return None
        values = [float(pct)]
        source = "current"
    return {
        "p95": _round(percentile(values, 95)),
        "max": _round(max(values)),
        "samples": len(values),
        "source": source,
    }


def _ssl(store: Store, cfg: Config) -> dict[str, Any] | None:
    if not cfg.module_enabled("ssl"):
        return None
    state = store.component_state("ssl") or {}
    metrics = state.get("metrics") or {}
    days_left = metrics.get("days_left")
    if days_left is None:
        latest = store.latest_check("ssl.cert")
        days_left = latest.get("value") if latest else None
    if days_left is None:
        return None
    return {
        "days_left": float(days_left),
        "not_after": metrics.get("not_after"),
        "subject": metrics.get("subject"),
    }


def _security_ports(rows: Any) -> list[dict[str, Any]]:
    """``metrics.ports`` of the Security component, cleaned up and sorted (new and exposed first)."""
    out: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        try:
            port = int(row["port"])
        except (KeyError, TypeError, ValueError):
            continue
        addrs = row.get("addrs") or []
        out.append(
            {
                **row,
                "port": port,
                "proto": str(row.get("proto") or "tcp"),
                "addrs": [str(a) for a in ([addrs] if isinstance(addrs, str) else addrs)],
                "public": bool(row.get("public")),
                "process": row.get("process") or None,
                "expected": row.get("expected") or None,
                "verdict": row.get("verdict") or None,
            }
        )
    out.sort(key=lambda p: (PORT_VERDICT_RANK.get(p["verdict"] or "", 4), p["port"], p["proto"]))
    return out


def _security_software(rows: Any) -> list[dict[str, Any]]:
    """``metrics.software`` of the Security component: one row per product, in the collector's order."""
    out: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not row.get("name"):
            continue
        out.append(
            {
                **row,
                "name": str(row["name"]),
                "version": None if row.get("version") is None else str(row["version"]),
                "until": row.get("until") or None,
                "status": row.get("status") or "unknown",
                "note": row.get("note") or "",
            }
        )
    return out


def _security(store: Store, cfg: Config, t_from: float, t_to: float) -> dict[str, Any]:
    """The Security section: the current rungs and the warning ones among them (``findings``), the
    rungs that warned earlier in the range (their history), the open-ports and software tables.

    ``verdict`` is ``off`` (module off), ``no_data`` (no check has run yet), ``findings`` (a rung
    warns now), ``clear`` (nothing warns and at least one rung was checked) or ``unchecked`` (the
    last check gave no result: the collector failed, or nothing could be read on this server).
    """
    out: dict[str, Any] = {
        "enabled": cfg.module_enabled("security"),
        "verdict": "off",
        "status": None,
        "checked_ts": None,
        "reason": None,
        "findings": [],
        "rungs": [],
        "earlier": [],
        "ports": [],
        "port_counts": {"listening": None, "reachable": None, "exposed": None},
        "software": [],
    }
    if not out["enabled"]:
        return out
    try:
        state = store.component_state("security")
    except Exception:
        log.exception("security state failed")
        state = None
    if not state:
        out["verdict"] = "no_data"
        return out
    metrics = state.get("metrics") if isinstance(state.get("metrics"), dict) else {}
    chain = [c for c in state.get("chain") or [] if isinstance(c, dict) and c.get("id")]
    labels = dict(SECURITY_RUNGS)
    labels.update({str(c["id"]): str(c.get("label") or c["id"]) for c in chain})
    history: dict[str, list[dict[str, Any]]] = {}
    for rung_id in dict.fromkeys([rid for rid, _ in SECURITY_RUNGS] + [str(c["id"]) for c in chain]):
        try:
            history[rung_id] = store.checks_between(t_from, t_to, rung_id, limit=_MAX_CHECK_ROWS)
        except Exception:
            log.exception("history of %s failed", rung_id)
            history[rung_id] = []

    for rung in chain:
        rung_id = str(rung["id"])
        detail = rung.get("detail") if isinstance(rung.get("detail"), dict) else {}
        rows = history.get(rung_id, [])
        out["rungs"].append(
            {
                "id": rung_id,
                "label": labels[rung_id],
                "status": rung.get("status") or "unknown",
                "message": str(rung.get("message") or ""),
                "hint": str(detail.get("hint") or ""),
                "alert": bool(detail.get("alert")),
                "informational": bool(detail.get("informational")),
                "period_checks": len(rows),
                "period_warnings": sum(1 for r in rows if r.get("status") in ("warn", "crit")),
            }
        )
    out["findings"] = [r for r in out["rungs"] if r["status"] in ("warn", "crit")]
    now_warning = {r["id"] for r in out["findings"]}
    for rung_id, rows in history.items():
        warned = [r for r in rows if r.get("status") in ("warn", "crit")]
        if not warned or rung_id in now_warning:
            continue
        out["earlier"].append(
            {
                "id": rung_id,
                "label": labels.get(rung_id, rung_id),
                "message": str(warned[-1].get("message") or ""),
                "warnings": len(warned),
                "checks": len(rows),
                "first_ts": float(warned[0]["ts"]),
                "last_ts": float(warned[-1]["ts"]),
            }
        )

    if out["findings"]:
        out["verdict"] = "findings"
    elif any(r["status"] == "ok" for r in out["rungs"]):
        out["verdict"] = "clear"
    else:
        out["verdict"] = "unchecked"
        failed = [r for r in out["rungs"] if r["message"] and not r["informational"]]
        out["reason"] = (failed or [{"message": "nothing could be checked on this server"}])[0]["message"]

    ports = _security_ports(metrics.get("ports"))
    counts = {
        "listening": len(ports),
        "reachable": sum(1 for p in ports if p["public"]),
        "exposed": sum(1 for p in ports if p["verdict"] in ("exposed", "new")),
    }
    for key in counts:
        value = metrics.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            counts[key] = int(value)
    out.update(
        {
            "status": state.get("status"),
            "checked_ts": state.get("checked_ts"),
            "ports": ports,
            "port_counts": counts if ports or any(key in metrics for key in counts) else out["port_counts"],
            "software": _security_software(metrics.get("software")),
        }
    )
    return out


def _summary(
    services: list[dict[str, Any]],
    alert_stats: dict[str, Any],
    incidents: list[dict[str, Any]],
    server: dict[str, Any],
) -> dict[str, Any]:
    avails = [float(s["availability_pct"]) for s in services if s.get("availability_pct") is not None]
    score = health_score(services)
    worst_svc: dict[str, Any] | None = None
    ranked = [s for s in services if s.get("availability_pct") is not None]
    if ranked:
        worst_svc = min(
            ranked,
            key=lambda s: (float(s["availability_pct"]), -STATUS_RANK.get(s.get("worst_status") or "ok", 0)),
        )
    statuses = [s.get("worst_status") for s in services if s.get("worst_status")]
    if score is None and not server.get("samples"):
        mood = "unknown"
    elif score is not None and score < 95.0:
        mood = "crit"
    elif alert_stats["opened"] or incidents or (score is not None and score < AVAILABILITY_TARGET_PCT):
        mood = "warn"
    elif "crit" in statuses:
        mood = "warn"
    else:
        mood = "ok"
    return {
        "health_score": score,
        "availability_pct": _round(mean(avails), 2),
        "incidents": len(incidents),
        "alerts_opened": alert_stats["opened"],
        "alerts_critical": alert_stats["critical"],
        "alerts_warning": alert_stats["warning"],
        "worst_component": (
            None
            if worst_svc is None
            else {
                "id": worst_svc["id"],
                "label": worst_svc["label"],
                "emoji": worst_svc["emoji"],
                "availability_pct": worst_svc["availability_pct"],
                "worst_status": worst_svc["worst_status"],
                "outages": worst_svc["outages"],
            }
        ),
        "uptime_s": server.get("uptime_s"),
        "samples": server.get("samples"),
        "coverage_pct": server.get("coverage_pct"),
        "mood": mood,
    }


# ------------------------------------------------------------------------------ recommendations


def recommendations(data: dict[str, Any], cfg: Config) -> list[dict[str, Any]]:
    """Apply the SPEC's recommendation rules to a computed report and return ``[{severity, text, rule}]``.

    Severity is ``critical`` | ``warning`` | ``info`` | ``ok``; the list is sorted worst first.
    """
    from dspace_sentinel.util.fmt import bytes_, duration, ms, pct

    out: list[dict[str, Any]] = []

    def add(severity: str, rule: str, text: str) -> None:
        out.append({"severity": severity, "rule": rule, "text": text})

    server = data.get("server") or {}
    if not server.get("samples"):
        add(
            "info",
            "no_data",
            "Not enough data for this period yet — Sentinel needs at least a few minutes of samples.",
        )

    for fc in (data.get("repository") or {}).get("forecasts") or []:
        days = fc.get("days_until_full")
        if days is None or days > DISK_FULL_HORIZON_DAYS:
            continue
        severity = "critical" if days < 14 else "warning"
        add(
            severity,
            "disk_full",
            f"{fc['label']} is growing {bytes_(fc.get('growth_per_day'))}/day; at this rate it is full in "
            f"{days:.0f} days ({bytes_(fc.get('free'))} free).",
        )

    ssl = data.get("ssl")
    if ssl and ssl.get("days_left") is not None:
        days_left = float(ssl["days_left"])
        th = cfg.alerts.ssl_expiry_days
        if days_left <= th.critical:
            add(
                "critical",
                "ssl_expiry",
                f"The SSL certificate expires in {days_left:.0f} days — renew it now.",
            )
        elif days_left <= max(th.warning, 30.0):
            add(
                "warning",
                "ssl_expiry",
                f"The SSL certificate expires in {days_left:.0f} days — plan the renewal.",
            )

    pg = data.get("postgres_connections")
    if pg and pg.get("p95") is not None and float(pg["p95"]) > PG_CONNECTIONS_WARN_PCT:
        qualifier = "is currently at" if pg.get("source") == "current" else "p95 reached"
        add(
            "warning",
            "postgres_connections",
            f"PostgreSQL connection usage {qualifier} {pct(pg['p95'])} of max_connections — "
            "check the DSpace pool size or raise max_connections.",
        )

    for svc in data.get("services") or []:
        avail = svc.get("availability_pct")
        if avail is None or avail >= AVAILABILITY_TARGET_PCT:
            continue
        severity = "critical" if avail < 95.0 else "warning"
        outages = svc.get("outages") or 0
        outage_text = f"{outages} outage" if outages == 1 else f"{outages} outages"
        add(
            severity,
            "availability",
            f"{svc['label']} was available {avail:.2f} % of the time ({outage_text}, "
            f"{duration(svc.get('downtime_s'))} down) — below the {AVAILABILITY_TARGET_PCT} % target.",
        )

    rest = (data.get("latency") or {}).get("rest")
    if rest and rest.get("p95") is not None and rest.get("thresholds"):
        p95 = float(rest["p95"])
        th = rest["thresholds"]
        if p95 >= th["critical"]:
            add(
                "critical",
                "rest_latency",
                f"REST API p95 latency {ms(p95)} is above the {ms(th['critical'])} critical threshold.",
            )
        elif p95 >= th["warning"]:
            add(
                "warning",
                "rest_latency",
                f"REST API p95 latency {ms(p95)} is above the {ms(th['warning'])} warning threshold.",
            )

    swap = server.get("swap") or {}
    used_fraction = swap.get("used_fraction")
    if used_fraction is not None and used_fraction >= 0.5 and (swap.get("max") or 0) > 0:
        severity = "warning" if (swap.get("max") or 0) >= cfg.alerts.swap.warning else "info"
        add(
            severity,
            "swap",
            f"Swap was in use for {used_fraction * 100:.0f} % of the period (avg {pct(swap.get('avg'))}, "
            f"max {pct(swap.get('max'))}) — check JVM heap sizes against the RAM available.",
        )

    logs = data.get("logs") or {}
    trend = logs.get("trend") or {}
    if logs.get("enabled") and trend.get("up"):
        add(
            "warning",
            "log_errors_trend",
            f"Log errors trended up: about {trend['second_half']} in the second half of the period against "
            f"{trend['first_half']} in the first.",
        )

    incidents = data.get("incidents") or []
    if incidents:
        longest = max(incidents, key=lambda i: i.get("duration_s") or 0)
        count_text = "1 incident" if len(incidents) == 1 else f"{len(incidents)} incidents"
        add(
            "info",
            "incidents",
            f"{count_text} this period; the longest lasted {duration(longest.get('duration_s'))} "
            f"({longest.get('title') or longest.get('hypothesis')}).",
        )
    elif cfg.module_enabled("incidents"):
        add("ok", "incidents", "No incidents this period.")
    else:
        add(
            "info",
            "incidents",
            "Incident correlation is off — turn on the Incidents module to group related alerts.",
        )

    out.sort(key=lambda r: -SEVERITY_RANK.get(r["severity"], 0))
    return out


# ------------------------------------------------------------------------------ entry point


def compute_report_data(
    store: Store,
    cfg: Config,
    t_from: float,
    t_to: float,
    *,
    title: str | None = None,
    sections: list[str] | None = None,
    now: float | None = None,
    range_hint: str | None = None,
) -> dict[str, Any]:
    """Compute everything a report shows for ``[t_from, t_to]`` (SPEC section 8 dict, JSON-ready).

    Args:
        store, cfg: the runtime store and effective config (only enabled modules are analysed).
        t_from, t_to: epoch seconds; ``t_to`` must be after ``t_from``.
        title: report title (default ``"DSpace Sentinel report · <hostname> · <range>"``).
        sections: section ids to render; ``None`` uses ``cfg.reports.default_sections``.
        now: "now" for durations of still-open alerts (defaults to the wall clock).
        range_hint: the range label the caller used (``"24h"``…) so the file name matches it.

    Raises:
        ValueError: when the range is empty.
    """
    t_from, t_to = float(t_from), float(t_to)
    if t_to <= t_from:
        raise ValueError("report range is empty")
    now = float(now or time.time())
    label = range_label(t_from, t_to, range_hint)
    enabled = [cid for cid in health.COMPONENT_ORDER if cfg.module_enabled(cid)]

    server = _server(store, cfg, t_from, t_to)
    services = _services(store, cfg, t_from, t_to, enabled)
    latency = _latency(store, cfg, t_from, t_to)
    alerts, alert_stats = _alerts(store, t_from, t_to, now)
    incidents = _incidents(store, t_from, t_to, now)
    repository = _repository(store, cfg, t_from, t_to, server)
    logs = _logs(store, cfg, t_from, t_to, alerts)
    summary = _summary(services, alert_stats, incidents, server)
    chosen = resolve_sections(cfg, sections)
    hostname = cfg.hostname
    zone = zone_of(cfg)
    period = range_title(label, t_from, t_to, zone)
    data: dict[str, Any] = {
        "meta": {
            "title": title or f"DSpace Sentinel report · {hostname} · {period}",
            "hostname": hostname,
            "generated_ts": now,
            "timezone": {"name": zone_name(zone), "abbreviation": zone_label(now, zone)},
            "range": {
                "from": t_from,
                "to": t_to,
                "label": label,
                "title": period,
                "span_s": int(t_to - t_from),
            },
            "sentinel_version": __version__,
            "sections": chosen,
            "demo": bool(cfg.demo),
            "modules": [cid for cid in enabled if cid != "server"],
        },
        "summary": summary,
        "server": server,
        "services": services,
        "latency": latency,
        "alerts": alerts,
        "alert_stats": alert_stats,
        "incidents": incidents,
        "repository": repository,
        "logs": logs,
        "ssl": _ssl(store, cfg),
        "postgres_connections": _pg_connections(store, cfg, t_from, t_to, now),
        "security": _security(store, cfg, t_from, t_to),
        "recommendations": [],
    }
    data["recommendations"] = recommendations(data, cfg)
    return data


__all__ = [
    "ALL_SECTIONS",
    "AVAILABILITY_TARGET_PCT",
    "DEFAULT_WEIGHT",
    "DISK_FULL_HORIZON_DAYS",
    "HEALTH_WEIGHTS",
    "LATENCY_CHECKS",
    "PG_CONNECTIONS_WARN_PCT",
    "RANGE_SECONDS",
    "RANGE_TITLES",
    "SECTION_LABELS",
    "SECURITY_RUNGS",
    "compute_report_data",
    "days_until_full",
    "growth_per_day",
    "health_score",
    "linear_slope",
    "mean",
    "normalise_log_line",
    "percentile",
    "range_label",
    "range_title",
    "recommendations",
    "resolve_sections",
]
