"""Reports: analytics on synthetic data, SVG helpers, HTML/Markdown/JSON rendering, file generation,
range parsing, the weekly schedule and graceful Telegram delivery (SPEC section 8)."""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import re
import shutil
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from dspace_sentinel.config import Config, TelegramChat
from dspace_sentinel.models import Alert, CheckResult, ComponentHealth, Incident, SystemSample
from dspace_sentinel.reports import analytics, generator, schedule, svg
from dspace_sentinel.store import REPORT_PURGE_AFTER_S, Store

GIB = 1024**3
MIB = 1024**2
DAY = 86400
# A fixed, hour-aligned "now" keeps every synthetic series deterministic (a Thursday in 2026).
NOW = float(_dt.datetime(2026, 9, 24, 12, 0).timestamp())
T_FROM = NOW - DAY


def _sample(ts: float, i: int, n: int) -> SystemSample:
    frac = i / max(1, n - 1)
    cpu = 20.0 + 60.0 * frac
    disk_pct = 80.0 + 10.0 * frac
    disk_total = 100 * GIB
    disk_used = int(disk_total * disk_pct / 100.0)
    return SystemSample(
        ts=ts,
        cpu_pct=cpu,
        cpu_per_core=[cpu] * 4,
        mem_total=16 * GIB,
        mem_used=8 * GIB,
        mem_available=8 * GIB,
        mem_pct=50.0,
        mem_cached=GIB,
        swap_total=4 * GIB,
        swap_used=int(0.05 * 4 * GIB),
        swap_pct=5.0,
        disk_total=disk_total,
        disk_used=disk_used,
        disk_free=disk_total - disk_used,
        disk_pct=disk_pct,
        disk_path="/",
        inode_pct=3.0,
        load1=1.0,
        load5=1.0,
        load15=1.0,
        cores=4,
        net_rx_bps=1000.0,
        net_tx_bps=2000.0,
        uptime_s=1_000_000.0,
        os_name="Ubuntu",
        os_version="24.04",
        kernel="6.8",
        hostname="test-host",
        disks=[{"path": "/dspace", "total": 1000 * GIB, "used": 900 * GIB, "free": 100 * GIB, "pct": 90.0}],
    )


def _state(cid: str, status: str, metrics: dict[str, Any]) -> ComponentHealth:
    return ComponentHealth(
        id=cid,
        label=cid,
        emoji="",
        status=status,  # type: ignore[arg-type]
        reason=None,
        chain=[],
        metrics=metrics,
        depends_on=[],
        latency_ms=None,
        since=NOW - 3600,
        checked_at=NOW,
    )


def test_cfg() -> Config:
    """A full-stack config with a fixed hostname (not a pytest test: no fixture args)."""
    cfg = Config()
    cfg.server.hostname = "test-host"
    for mid in (
        "postgres",
        "solr",
        "backend",
        "frontend",
        "nginx",
        "ssl",
        "logs",
        "repository",
        "assetstore",
    ):
        setattr(cfg.modules, mid, True)
    cfg.modules.incidents = True
    return cfg


test_cfg.__test__ = False  # type: ignore[attr-defined]


@pytest.fixture
def synthetic(tmp_path: Path) -> tuple[Store, Config]:
    """24 h of synthetic history with one backend outage, alerts, an incident, growth and log spikes."""
    store = Store(tmp_path / "reports.db")
    cfg = test_cfg()
    n = DAY // 60
    store.insert_samples_bulk(_sample(T_FROM + i * 60, i, n) for i in range(n))
    check_rows: list[tuple[Any, ...]] = []
    for i in range(n):
        ts = int(T_FROM + i * 60)
        rest = 100.0 if i < int(n * 0.9) else 2000.0
        check_rows.append((ts, "backend.rest", "ok" if rest < 1000 else "warn", rest, None, ""))
        check_rows.append((ts, "backend.search", "ok", 200.0, None, ""))
        check_rows.append((ts, "frontend.home", "ok", 500.0, None, ""))
        check_rows.append((ts, "postgres.query", "ok", 2.0, None, ""))
        check_rows.append((ts, "solr.search", "ok", 30.0, None, ""))
        errors = 2.0 if ts < T_FROM + DAY / 2 else 30.0
        check_rows.append((ts, "logs.dspace", "ok" if errors < 20 else "warn", None, errors, ""))
    store.insert_check_rows(check_rows)
    history: list[tuple[Any, ...]] = []
    outage_from, outage_to = T_FROM + 3600, T_FROM + 4800
    for ts in range(int(T_FROM), int(NOW), 120):
        for cid in (
            "server",
            "postgres",
            "solr",
            "backend",
            "frontend",
            "nginx",
            "ssl",
            "logs",
            "repository",
            "assetstore",
        ):
            status = "crit" if (cid == "backend" and outage_from <= ts < outage_to) else "ok"
            history.append((ts, cid, status, 100.0 if cid == "backend" else None))
    store.insert_component_history_rows(history)
    a1 = Alert(
        None,
        "rest_latency",
        "backend",
        "critical",
        "resolved",
        "REST API slow: 2.4 s",
        "GET /server/api",
        2400.0,
        3000.0,
        outage_from,
        outage_to,
    )
    a2 = Alert(
        None,
        "memory",
        "server",
        "warning",
        "open",
        "Memory 83 %",
        "13 of 16 GB used",
        83.0,
        80.0,
        T_FROM + 7200,
        None,
    )
    a3 = Alert(
        None,
        "log_errors",
        "logs",
        "critical",
        "resolved",
        "118 errors in DSpace log (5 min)",
        "2024-01-01 10:41:02,123 ERROR org.dspace.X @ Boom happened",
        118.0,
        100.0,
        T_FROM + 9000,
        T_FROM + 9600,
    )
    for a in (a1, a2, a3):
        store.insert_alert(a)
    store.insert_incident(
        Incident(
            None,
            "Backend outage: the UI cannot reach the REST API",
            "Backend outage",
            "resolved",
            outage_from,
            outage_to,
            ["DSpace Frontend"],
            [
                {"ts": outage_from, "text": "REST API slow: 2.4 s", "severity": "critical"},
                {"ts": outage_from + 60, "text": "DSpace Backend unreachable", "severity": "critical"},
            ],
            [a1.id],
        )
    )
    store.insert_repository_stats(
        T_FROM,
        {
            "items": 1000,
            "bitstreams": 5000,
            "communities": 3,
            "collections": 10,
            "db_size": GIB,
            "assetstore_bytes": 900 * GIB,
        },
    )
    store.insert_repository_stats(
        NOW - 60,
        {
            "items": 1010,
            "bitstreams": 5030,
            "communities": 3,
            "collections": 11,
            "db_size": GIB + 10 * MIB,
            "assetstore_bytes": 901 * GIB,
        },
    )
    store.upsert_component_state(
        _state("ssl", "warn", {"days_left": 12.0, "not_after": "2026-10-06T00:00:00Z"})
    )
    store.upsert_component_state(_state("postgres", "ok", {"connections_pct": 86, "connections": 86}))
    store.upsert_component_state(
        _state(
            "assetstore",
            "ok",
            {
                "total": 1000 * GIB,
                "free": 100 * GIB,
                "growth_bytes_per_day": GIB,
                "mount": "/dspace",
                "pct": 90.0,
            },
        )
    )
    store.upsert_component_state(
        _state(
            "logs",
            "warn",
            {
                "files": {
                    "dspace": {
                        "label": "DSpace",
                        "last_error": {
                            "line": "2026-09-24 11:59:00,001 ERROR org.dspace.X @ Boom happened",
                            "ts": NOW - 60,
                        },
                    }
                }
            },
        )
    )
    store.upsert_component_state(_state("server", "ok", {"os": "Ubuntu 24.04", "kernel": "6.8"}))
    yield store, cfg
    store.close()


# ------------------------------------------------------------------------------ maths


def test_percentile_mean_slope() -> None:
    assert analytics.percentile([], 95) is None
    assert analytics.percentile([7.0], 50) == 7.0
    assert analytics.percentile([1, 2, 3, 4, 5], 50) == 3.0
    assert analytics.percentile([1, 2, 3, 4, 5], 100) == 5.0
    assert analytics.percentile([1, 2, 3, 4, 5], 95) == pytest.approx(4.8)
    assert analytics.mean([]) is None and analytics.mean([2, 4]) == 3.0
    assert analytics.linear_slope([(0, 0), (10, 20)]) == 2.0
    assert analytics.linear_slope([(5, 1), (5, 2)]) is None
    assert analytics.growth_per_day([(0, 0.0), (DAY, 100.0)]) == pytest.approx(100.0)
    assert analytics.days_until_full(50, 10) == 5.0
    assert analytics.days_until_full(50, 0) is None and analytics.days_until_full(50, -1) is None
    assert analytics.days_until_full(None, 10) is None


def test_health_score_weights() -> None:
    services = [
        {"id": "backend", "availability_pct": 90.0},
        {"id": "postgres", "availability_pct": 100.0},
        {"id": "solr", "availability_pct": 100.0},
        {"id": "ssl", "availability_pct": None},
        {"id": "ports", "availability_pct": 80.0},
    ]
    expected = (30 * 90 + 25 * 100 + 15 * 100 + 5 * 80) / (30 + 25 + 15 + 5)
    assert analytics.health_score(services) == pytest.approx(round(expected, 2))
    assert analytics.health_score([]) is None
    assert analytics.health_score([{"id": "x", "availability_pct": None}]) is None


def test_range_label_and_title() -> None:
    assert analytics.range_label(0, DAY) == "24h"
    assert analytics.range_label(0, 7 * DAY) == "7d"
    assert analytics.range_label(0, 30 * DAY + 30) == "30d"
    assert analytics.range_label(0, 3 * DAY) == "3d"
    assert analytics.range_label(0, 5400) == "2h"
    assert analytics.range_label(0, DAY + 100, hint="24h") == "24h"
    assert analytics.range_title("7d", 0, 7 * DAY) == "Last 7 days"
    assert "→" in analytics.range_title("3d", 0, 3 * DAY)


def test_resolve_sections() -> None:
    cfg = Config()
    assert analytics.resolve_sections(cfg, None) == list(cfg.reports.default_sections)
    assert analytics.resolve_sections(cfg, ["recommendations", "bogus", "summary"]) == [
        "summary",
        "recommendations",
    ]
    assert analytics.resolve_sections(cfg, ["bogus"]) == ["summary"]
    assert analytics.resolve_sections(cfg, ["LOGS", " incidents "]) == ["incidents", "logs"]
    assert analytics.resolve_sections(cfg, ["recommendations", "security", "logs"]) == [
        "logs",
        "security",
        "recommendations",
    ]


def test_normalise_log_line() -> None:
    a = analytics.normalise_log_line("2024-01-01 10:41:02,123 ERROR org.dspace.X @ Boom (qtp1-19) happened")
    b = analytics.normalise_log_line("2026-09-24 11:59:00,001 ERROR org.dspace.X @ Boom (qtp9-2) happened")
    assert a == b == "ERROR org.dspace.X @ Boom happened"
    assert (
        analytics.normalise_log_line("24-Sep-2026 10:41:02.412 SEVERE [http-nio-8080-exec-7] x") == "SEVERE x"
    )


# ------------------------------------------------------------------------------ analytics


def test_compute_report_data_on_synthetic_history(synthetic: tuple[Store, Config]) -> None:
    store, cfg = synthetic
    data = analytics.compute_report_data(store, cfg, T_FROM, NOW, now=NOW, range_hint="24h")
    meta = data["meta"]
    assert meta["hostname"] == "test-host" and meta["range"]["label"] == "24h"
    assert meta["title"] == "DSpace Sentinel report · test-host · Last 24 hours"
    assert meta["sections"] == list(cfg.reports.default_sections)
    json.dumps(data)  # JSON-ready

    server = data["server"]
    assert server["samples"] == DAY // 60 and server["coverage_pct"] == 100.0
    assert server["cpu"]["avg"] == pytest.approx(50.0, abs=0.1) and server["cpu"]["max"] == 80.0
    assert 75.0 < server["cpu"]["p95"] < 80.0
    assert server["swap"]["used_fraction"] == 1.0
    assert server["load"]["cores"] == 4 and server["thresholds"]["load"]["warning"] == 6.0
    disk = server["disk"]
    assert disk["total"] == 100 * GIB and disk["free_now"] == pytest.approx(10 * GIB, rel=0.01)
    assert disk["growth_per_day"] == pytest.approx(10 * GIB, rel=0.02)
    assert 0.9 < disk["days_until_full"] < 1.1
    assert len(server["series"]["cpu"]) > 100 and server["series"]["net"]["rx"]

    services = {s["id"]: s for s in data["services"]}
    assert set(services) == {
        "server",
        "postgres",
        "solr",
        "backend",
        "frontend",
        "nginx",
        "ssl",
        "logs",
        "repository",
        "assetstore",
    }
    backend = services["backend"]
    assert backend["availability_pct"] == pytest.approx(98.61, abs=0.01)
    assert backend["outages"] == 1 and backend["worst_status"] == "crit" and backend["weight"] == 30
    assert backend["avg_latency_ms"] == pytest.approx(290.0, abs=5)  # 90 % at 100 ms, 10 % at 2 s
    assert services["postgres"]["availability_pct"] == 100.0 and services["postgres"]["avg_latency_ms"] == 2.0
    assert len(backend["buckets"]) == 48

    latency = data["latency"]
    assert set(latency) == {"rest", "search", "home", "postgres", "solr"}
    rest = latency["rest"]
    assert (rest["p50"], rest["p95"], rest["max"]) == (100.0, 2000.0, 2000.0)
    assert rest["samples"] == DAY // 60 and rest["status"] == "warn"
    assert rest["thresholds"] == {"warning": 1000.0, "critical": 3000.0}
    assert latency["postgres"]["thresholds"] is None and latency["postgres"]["status"] is None

    assert len(data["alerts"]) == 3 and data["alerts"][0]["rule"] == "log_errors"  # newest first
    stats = data["alert_stats"]
    assert (
        stats["opened"] == 3 and stats["critical"] == 2 and stats["warning"] == 1 and stats["still_open"] == 1
    )
    assert stats["by_component"]["DSpace Backend"] == 1 and stats["longest_s"] == 1200
    assert (
        data["alerts"][2]["duration_s"] == 1200 and data["alerts"][2]["component_label"] == "DSpace Backend"
    )

    assert len(data["incidents"]) == 1
    inc = data["incidents"][0]
    assert (
        inc["duration_s"] == 1200 and inc["alert_count"] == 1 and inc["alerts"][0]["rule"] == "rest_latency"
    )

    repo = data["repository"]
    assert repo["enabled"] and repo["now"]["items"] == 1010
    assert repo["delta"]["items"] == 10 and repo["delta"]["collections"] == 1
    assert repo["growth_per_day"]["items"] == pytest.approx(10.0, rel=0.01)
    assert repo["growth_per_day"]["assetstore_bytes"] == pytest.approx(GIB, rel=0.01)
    forecasts = {f["id"]: f for f in repo["forecasts"]}
    assert forecasts["assetstore"]["days_until_full"] == pytest.approx(100.0, abs=0.5)
    assert repo["days_until_disk_full"] == forecasts["root"]["days_until_full"]

    logs = data["logs"]
    assert logs["enabled"] and logs["files"][0]["id"] == "dspace"
    assert logs["trend"]["up"] and logs["trend"]["second_half"] > logs["trend"]["first_half"] * 5
    assert logs["files"][0]["peak_per_window"] == 30 and logs["files"][0]["windows_over_warning"] == 720
    assert logs["top_errors"][0]["text"] == "ERROR org.dspace.X @ Boom happened"
    assert logs["top_errors"][0]["count"] == 2  # alert message + current last_error, normalised together

    assert data["ssl"]["days_left"] == 12.0
    assert data["postgres_connections"] == {"p95": 86.0, "max": 86.0, "samples": 1, "source": "current"}

    summary = data["summary"]
    assert summary["alerts_opened"] == 3 and summary["alerts_critical"] == 2 and summary["incidents"] == 1
    assert summary["worst_component"]["id"] == "backend"
    assert (
        98.0 < summary["health_score"] < 100.0 and summary["health_score"] < summary["availability_pct"] + 1
    )
    assert summary["uptime_s"] == 1_000_000.0 and summary["mood"] == "warn"

    rules = {r["rule"]: r["severity"] for r in data["recommendations"]}
    assert rules["disk_full"] == "critical"
    assert rules["ssl_expiry"] == "warning"
    assert rules["postgres_connections"] == "warning"
    assert rules["availability"] == "warning"
    assert rules["rest_latency"] == "warning"
    assert rules["swap"] == "info"
    assert rules["log_errors_trend"] == "warning"
    assert rules["incidents"] == "info"
    severities = [r["severity"] for r in data["recommendations"]]
    assert severities == sorted(severities, key=lambda s: -analytics.SEVERITY_RANK[s])


def test_postgres_connections_p95_comes_from_history(mem_store: Store) -> None:
    """The SPEC rule is p95 over the period; the current value only stands in for a range ending now."""
    cfg = test_cfg()
    mem_store.insert_check_rows(
        (NOW - DAY + i * 60, "postgres.connections", "ok", None, 90.0 if i % 10 == 0 else 20.0, "")
        for i in range(DAY // 60)
    )
    data = analytics.compute_report_data(mem_store, cfg, T_FROM, NOW, now=NOW)
    pg = data["postgres_connections"]
    assert pg["source"] == "history" and pg["samples"] == DAY // 60 and pg["p95"] == 90.0
    text = next(r["text"] for r in data["recommendations"] if r["rule"] == "postgres_connections")
    assert text.startswith("PostgreSQL connection usage p95 reached 90 % of max_connections")
    # a quiet period: no recommendation even though the connections are busy right now
    mem_store.upsert_component_state(_state("postgres", "ok", {"connections_pct": 86, "connections": 86}))
    old_from, old_to = NOW - 60 * DAY, NOW - 53 * DAY
    past = analytics.compute_report_data(mem_store, cfg, old_from, old_to, now=NOW)
    assert past["postgres_connections"] is None
    assert "postgres_connections" not in {r["rule"] for r in past["recommendations"]}
    # a range ending now without history: the current value, worded as such
    fresh = Store(":memory:")
    fresh.upsert_component_state(_state("postgres", "ok", {"connections_pct": 86, "connections": 86}))
    now_data = analytics.compute_report_data(fresh, cfg, T_FROM, NOW, now=NOW)
    assert now_data["postgres_connections"]["source"] == "current"
    text = next(r["text"] for r in now_data["recommendations"] if r["rule"] == "postgres_connections")
    assert text.startswith("PostgreSQL connection usage is currently at 86 % of max_connections")
    fresh.close()


def test_top_errors_read_the_live_log_alert_ids(mem_store: Store) -> None:
    """The engine files log alerts as ``log_errors:<file id>``; their lines count as top errors."""
    cfg = test_cfg()
    line = "2026-09-24 11:00:00,001 ERROR org.dspace.Y @ Disk quota exceeded"
    for i in range(2):
        mem_store.insert_alert(
            Alert(
                None,
                "log_errors:dspace",
                "logs",
                "warning",
                "resolved",
                "25 errors in DSpace log (5 min)",
                line,
                25.0,
                20.0,
                T_FROM + 600 * (i + 1),
                T_FROM + 600 * (i + 1) + 300,
            )
        )
    data = analytics.compute_report_data(mem_store, cfg, T_FROM, NOW, now=NOW, sections=["logs"])
    assert data["logs"]["top_errors"][0] == {
        "text": "ERROR org.dspace.Y @ Disk quota exceeded",
        "count": 2,
        "source": "Logs",
    }


def test_recommendations_without_incidents(mem_store: Store) -> None:
    cfg = Config()
    cfg.modules.incidents = True
    data = analytics.compute_report_data(mem_store, cfg, T_FROM, NOW, now=NOW)
    recs = {r["rule"]: r for r in data["recommendations"]}
    assert recs["incidents"]["severity"] == "ok" and recs["incidents"]["text"] == "No incidents this period."
    assert recs["no_data"]["severity"] == "info"
    assert data["summary"]["mood"] == "unknown" and data["summary"]["health_score"] is None
    cfg.modules.incidents = False
    data = analytics.compute_report_data(mem_store, cfg, T_FROM, NOW, now=NOW)
    assert "correlation is off" in next(
        r["text"] for r in data["recommendations"] if r["rule"] == "incidents"
    )


def test_off_modules_leave_no_trace(synthetic: tuple[Store, Config]) -> None:
    store, cfg = synthetic
    for mid in ("solr", "frontend", "logs", "repository", "ssl"):
        setattr(cfg.modules, mid, False)
    data = analytics.compute_report_data(store, cfg, T_FROM, NOW, now=NOW)
    assert {s["id"] for s in data["services"]} == {"server", "postgres", "backend", "nginx", "assetstore"}
    assert set(data["latency"]) == {"rest", "search", "postgres"}
    assert data["logs"]["enabled"] is False and data["logs"]["files"] == []
    assert data["repository"]["enabled"] is False and data["repository"]["now"] is None
    assert data["ssl"] is None
    assert "ssl_expiry" not in {r["rule"] for r in data["recommendations"]}
    assert data["meta"]["modules"] == ["postgres", "backend", "nginx", "assetstore"]


def test_empty_range_rejected(mem_store: Store) -> None:
    with pytest.raises(ValueError):
        analytics.compute_report_data(mem_store, Config(), NOW, NOW)


# ------------------------------------------------------------------------------ svg


def _svg_root(markup: str) -> ET.Element:
    return ET.fromstring(markup)


def test_svg_line_chart_shapes() -> None:
    points = [[T_FROM + i * 600, 10.0 + i] for i in range(144)]
    out = svg.line_chart(
        [svg.Series("CPU", points)],
        unit="%",
        y_max=100,
        thresholds={"warning": 80, "critical": 95},
        t_from=T_FROM,
        t_to=NOW,
        title="CPU usage",
    )
    root = _svg_root(out)
    assert root.get("role") == "img" and "CPU usage" in root.get("aria-label", "")
    ns = "{http://www.w3.org/2000/svg}"
    paths = root.findall(f".//{ns}path")
    widths = {p.get("stroke-width") for p in paths if p.get("stroke") != "none"}
    assert "2" in widths  # data line
    assert all(p.get("stroke-linejoin") == "round" for p in paths if p.get("stroke-width") == "2")
    assert any(p.get("fill-opacity") == "0.1" for p in paths)  # area wash for a single series
    assert "stroke-dasharray" not in out and "gradient" not in out.lower()
    texts = [t.text for t in root.findall(f".//{ns}text")]
    assert "warning 80 %" in texts and "critical 95 %" in texts and "100 %" in texts
    lines = root.findall(f".//{ns}line")
    assert all(line.get("stroke-width") == "1" for line in lines)  # hairlines only
    assert "var(--chart-1" in out and "var(--ink-3" in out


def test_svg_line_chart_legend_empty_and_axis() -> None:
    two = svg.line_chart(
        [svg.Series("rx", [[0, 10], [60, 20]], "chart-1"), svg.Series("tx", [[0, 5], [60, 7]], "chart-2")],
        unit="B/s",
        t_from=0,
        t_to=60,
        title="Network",
    )
    assert two.count("<rect") >= 3  # clip rect + two legend keys
    assert "fill-opacity" not in two  # no wash with two series
    empty = svg.line_chart([svg.Series("x", [])], t_from=0, t_to=3600, title="Nothing")
    assert "No data for this period" in empty and _svg_root(empty) is not None
    # a threshold far above the data is not allowed to squash the line
    tall = svg.line_chart(
        [svg.Series("ms", [[0, 100], [60, 120]])], unit="ms", thresholds={"warning": 5000}, t_from=0, t_to=60
    )
    assert "warning 5.0 s" not in tall
    near = svg.line_chart(
        [svg.Series("ms", [[0, 100], [60, 900]])], unit="ms", thresholds={"warning": 1000}, t_from=0, t_to=60
    )
    assert "warning 1.0 s" in near
    assert svg.nice_ceiling(87) == 100 and svg.nice_ceiling(1.2) == 2 and svg.nice_ceiling(0) == 1
    assert svg.format_value(1500, "ms") == "1.5 s" and svg.format_value(80, "%") == "80 %"
    assert svg.format_value(2048, "B/s") == "2.0 KB/s"
    ticks = svg.time_ticks(0, 6 * 3600)
    assert 3 <= len(ticks) <= 7 and all(re.match(r"\d\d:\d\d", label) for _, label in ticks)
    day_ticks = svg.time_ticks(NOW - 7 * DAY, NOW)
    assert 4 <= len(day_ticks) <= 8 and all(re.match(r"\d\d \w{3}", label) for _, label in day_ticks)


def test_svg_strip_meter_sparkline() -> None:
    out = svg.strip(["ok", "ok", "crit", None, "warn", "unknown"], title="Backend availability")
    root = _svg_root(out)
    rects = root.findall(".//{http://www.w3.org/2000/svg}rect")
    assert len(rects) == 6 and rects[2].get("fill", "").startswith("var(--crit-fill")
    assert rects[3].get("fill", "").startswith("var(--surface-2")
    assert "2 ok, 1 warn, 1 crit, 1 unknown" in root.get("aria-label", "")
    assert _svg_root(svg.strip([])) is not None
    meter = svg.meter(90, 100, status="crit", width=200)
    mroot = _svg_root(meter)
    fill = mroot.findall(".//{http://www.w3.org/2000/svg}rect")[1]
    assert fill.get("width") == "180" and "crit-fill" in fill.get("fill", "")
    assert _svg_root(svg.meter(None, 0)) is not None
    # a size meter names human sizes, not byte counts, to screen readers and in its tooltip
    sized = _svg_root(svg.meter(101_899_695_717, 214_748_364_800, title="Disk /", unit="B"))
    assert sized.get("aria-label") == "Disk /: 94.9 GB of 200.0 GB"
    spark = svg.sparkline([1, 2, 3, None, 5], status="warn")
    assert 'stroke-width="1.5"' in spark and "warn-fill" in spark and _svg_root(spark) is not None


# ------------------------------------------------------------------------------ rendering


def test_render_html_markdown_json(synthetic: tuple[Store, Config]) -> None:
    store, cfg = synthetic
    data = analytics.compute_report_data(store, cfg, T_FROM, NOW, now=NOW, range_hint="24h")
    html = generator.render(data, "html")
    assert (
        html.startswith("<!doctype html>")
        and "<title>DSpace Sentinel report · test-host · Last 24 hours</title>" in html
    )
    assert "#F6F2E8" in html and "--r-card: 22px" in html and "box-shadow" not in html
    # one file, no external requests: the font stack falls back to the system fonts
    assert "fonts.googleapis.com" not in html and "fonts.gstatic.com" not in html
    assert not re.search(r"""(?:href|src)=["']?(?:https?:)?//""", html)
    assert "Space Grotesk" in html and "Inter" in html and "system-ui" in html
    # the hero verdict pill keeps dark ink on the pastel accent in both themes
    hero_pill = ".hero .pill.ok, .hero .pill.warn, .hero .pill.crit, .hero .pill.unknown"
    assert f"{hero_pill} {{ color: var(--on-accent); }}" in html
    assert "@page" in html and "break-inside: avoid" in html and "print-color-adjust: exact" in html
    assert "stroke-dasharray" not in html
    for sid in cfg.reports.default_sections:
        assert f'id="{sid}"' in html
    assert 'id="logs"' not in html and 'id="incidents"' in html  # incidents ride along with alerts
    # a default section: here the Security module is off, and the section says so
    assert 'id="security"' in html and "🛡️ The Security module is off." in html
    assert html.count("<svg") >= 12  # 4 server charts + network + 5 latency + strips + meters
    assert "Backend outage: the UI cannot reach the REST API" in html
    assert "REST API slow: 2.4 s" in html and "Top errors" not in html  # logs section is off by default
    assert "<script" not in html
    for chunk in re.findall(r"<svg.*?</svg>", html, flags=re.S):
        ET.fromstring(chunk)

    md = generator.render(data, "md")
    assert md.startswith("# DSpace Sentinel report · test-host · Last 24 hours")
    for heading in (
        "## 📋 Summary",
        "## 💻 Server",
        "## 📚 Services",
        "## ⏱️ Response times",
        "## ⚠️ Alerts",
        "## 🧭 Incidents",
        "## 🗄️ Repository and storage",
        "## 💡 Recommendations",
    ):
        assert heading in md
    assert "## 📜 Logs" not in md
    assert "| ☕ DSpace Backend | 98.61 % | 1 |" in md
    assert "| REST API (`backend.rest`) | 100 ms | 2.0 s | 2.0 s |" in md
    assert "- **Critical** — Disk / is growing" in md

    js = generator.render(data, "json")
    assert json.loads(js)["summary"]["alerts_opened"] == 3
    assert json.loads(js) == json.loads(json.dumps(data, default=str))


def test_render_sections_filter_and_logs(synthetic: tuple[Store, Config]) -> None:
    store, cfg = synthetic
    data = analytics.compute_report_data(store, cfg, T_FROM, NOW, now=NOW, sections=["logs", "summary"])
    assert data["meta"]["sections"] == ["summary", "logs"]
    html = generator.render(data, "html")
    assert (
        'id="summary"' in html
        and 'id="logs"' in html
        and 'id="server"' not in html
        and 'id="alerts"' not in html
    )
    assert "Boom happened" in html and "Top errors" in html
    md = generator.render(data, "md")
    assert "## 📜 Logs" in md and "## 💻 Server" not in md
    with pytest.raises(ValueError):
        generator.render(data, "pdf")


def test_render_handles_empty_store(mem_store: Store) -> None:
    data = analytics.compute_report_data(mem_store, Config(), T_FROM, NOW, now=NOW)
    html = generator.render(data, "html")
    assert "No data for this period" in html and "no data yet" in html
    assert "💻 Server" in html and ">No data<" in html  # the server is always monitored, just empty
    assert 'class="pill unknown"' in html
    md = generator.render(data, "md")
    assert "| 💻 Server | — | 0 | — | No data | — |" in md
    assert "_No incidents in this period._" in md


# ------------------------------------------------------------------------------ security


@pytest.fixture
def security_on(monkeypatch: pytest.MonkeyPatch) -> Callable[[Config], Config]:
    """Switch the Security module on, also on a config.py that does not know the module yet."""

    def switch_on(cfg: Config) -> Config:
        if "security" in type(cfg.modules).model_fields:
            cfg.modules.security = True
        else:
            known = Config.module_enabled
            monkeypatch.setattr(
                Config, "module_enabled", lambda self, mid: mid == "security" or known(self, mid)
            )
        return cfg

    return switch_on


def _security_state(rungs: list[CheckResult], metrics: dict[str, Any]) -> ComponentHealth:
    """The Security component as the contract shapes it: its rungs, and metrics ports, software …"""
    state = _state("security", "ok", metrics)
    state.chain = rungs
    state.status = "warn" if any(r.status == "warn" for r in rungs) else "ok"
    return state


def _rung(rung_id: str, label: str, status: str, message: str, **detail: Any) -> CheckResult:
    return CheckResult(rung_id, label, status, NOW, message=message, detail=detail)  # type: ignore[arg-type]


PORTS_HINT = (
    "Ports other machines can reach that no rule expects: 3306 (mysqld). If a port is not meant for other "
    "machines, make its program listen on 127.0.0.1 or block it in the firewall; if it is, add a rule for "
    "it under ports in config/sentinel.yaml and restart Sentinel."
)
UPDATES_HINT = "sudo apt update && sudo apt upgrade, then reboot when it says so."


def _warning_rungs() -> list[CheckResult]:
    return [
        _rung(
            "security.ports",
            "Open ports",
            "warn",
            "4 listening · 3 reachable from the network · 2 unexpected: 3306 (mysqld), 161/udp",
            hint=PORTS_HINT,
            alert=True,
        ),
        _rung("security.firewall", "Firewall", "ok", "ufw active"),
        _rung(
            "security.ssh",
            "SSH",
            "ok",
            "root login: prohibit-password · passwords: yes · port 22",
            hint="Keys instead of passwords: set PasswordAuthentication no once every admin has a key.",
        ),
        _rung("security.web", "Web", "ok", "HTTPS on repo.example.org"),
        _rung("security.updates", "Updates", "warn", "5 security updates pending", hint=UPDATES_HINT),
        _rung(
            "security.software",
            "Software",
            "warn",
            "Ubuntu 20.04 reached end of standard support on 2025-05-31 (+1 more)",
        ),
        _rung(
            "security.files",
            "DSpace files",
            "ok",
            "local.cfg and sentinel.yaml are readable by their owners only",
        ),
        _rung("security.sentinel", "Sentinel", "ok", "listens on 127.0.0.1 only"),
    ]


def _port(port: int, verdict: str, public: bool, **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "port": port,
        "proto": "tcp",
        "addrs": ["0.0.0.0"] if public else ["127.0.0.1"],
        "public": public,
        "process": None,
        "pid": None,
        "expected": None,
        "verdict": verdict,
    }
    row.update(extra)
    return row


WARNING_METRICS: dict[str, Any] = {
    "ports": [
        _port(22, "expected", True, expected="open"),
        _port(5432, "local", False, expected="local_only"),
        _port(161, "exposed", True, proto="udp"),
        _port(3306, "new", True, process="mysqld", pid=812),
    ],
    "listening": 4,
    "reachable": 3,
    "exposed": 2,
    "firewall": {"active": True, "name": "ufw"},
    "updates": {"pending": 9, "security": 5, "reboot_required": False, "source": "update-notifier"},
    "software": [
        {"name": "DSpace", "version": "9.4", "until": None, "status": "ok", "note": ""},
        {
            "name": "Solr",
            "version": "8.8.2",
            "until": None,
            "status": "affected",
            "note": "affected by Log4Shell (CVE-2021-44228): upgrade to 8.11.1 or later",
        },
        {"name": "PostgreSQL", "version": "14", "until": "2026-11-12", "status": "soon", "note": ""},
        {"name": "Ubuntu", "version": "20.04", "until": "2025-05-31", "status": "ended", "note": ""},
    ],
}


def test_security_section_lists_findings_history_ports_and_software(
    mem_store: Store, security_on: Callable[[Config], Config]
) -> None:
    cfg = security_on(test_cfg())
    mem_store.upsert_component_state(_security_state(_warning_rungs(), WARNING_METRICS))
    # one check every 15 minutes: SSH warned twice early on, the ports warn in the last hour,
    # the updates since midday, the software all day
    rows: list[tuple[Any, ...]] = []
    for i in range(96):
        ts = int(T_FROM + i * 900)
        warnings = {
            "security.ssh": "root may log in with a password" if i in (4, 5) else "",
            "security.ports": "1 unexpected: 3306 (mysqld)" if i >= 92 else "",
            "security.updates": "5 security updates pending" if i >= 48 else "",
            "security.software": "Ubuntu 20.04 reached end of standard support on 2025-05-31",
        }
        for rung_id, _ in analytics.SECURITY_RUNGS:
            message = warnings.get(rung_id, "")
            rows.append((ts, rung_id, "warn" if message else "ok", None, None, message))
    mem_store.insert_check_rows(rows)

    data = analytics.compute_report_data(mem_store, cfg, T_FROM, NOW, now=NOW, sections=["security"])
    sec = data["security"]
    assert sec["enabled"] is True and sec["verdict"] == "findings" and sec["status"] == "warn"
    assert sec["checked_ts"] == NOW
    assert [r["id"] for r in sec["rungs"]] == [rung_id for rung_id, _ in analytics.SECURITY_RUNGS]
    assert [f["id"] for f in sec["findings"]] == ["security.ports", "security.updates", "security.software"]
    ports = sec["findings"][0]
    assert ports["label"] == "Open ports" and ports["hint"] == PORTS_HINT and ports["alert"] is True
    assert (ports["period_warnings"], ports["period_checks"]) == (4, 96)
    assert sec["findings"][1]["hint"] == UPDATES_HINT and sec["findings"][2]["hint"] == ""
    assert sec["earlier"] == [
        {
            "id": "security.ssh",
            "label": "SSH",
            "message": "root may log in with a password",
            "warnings": 2,
            "checks": 96,
            "first_ts": T_FROM + 4 * 900,
            "last_ts": T_FROM + 5 * 900,
        }
    ]
    assert [(p["port"], p["proto"], p["verdict"]) for p in sec["ports"]] == [
        (3306, "tcp", "new"),
        (161, "udp", "exposed"),
        (22, "tcp", "expected"),
        (5432, "tcp", "local"),
    ]
    assert sec["port_counts"] == {"listening": 4, "reachable": 3, "exposed": 2}
    assert [(s["name"], s["status"]) for s in sec["software"]] == [
        ("DSpace", "ok"),
        ("Solr", "affected"),
        ("PostgreSQL", "soon"),
        ("Ubuntu", "ended"),
    ]

    html = generator.render(data, "html")
    assert 'id="security"' in html and '<span class="mark">🛡️</span> Security' in html
    assert '<span class="pill warn">Open ports</span>' in html
    assert "3306 (mysqld). If a port is not meant for other machines" in html
    assert "sudo apt update &amp;&amp; sudo apt upgrade" in html
    assert "Earlier in this period" in html and "warned in 2 of 96 checks" in html
    assert "No security findings" not in html
    assert '<td class="num">161/udp</td>' in html and "<td>mysqld</td>" in html
    for pill in ('warn">New', 'warn">Exposed', 'plain">Expected', 'plain">Local'):
        assert f'<span class="pill {pill}</span>' in html
    assert "4 listening · 3 reachable from the network · 2 unexpected" in html
    for pill in ('ok">Supported', 'warn">Affected', 'plain">Ends soon', 'warn">Ended'):
        assert f'<span class="pill {pill}</span>' in html
    assert "affected by Log4Shell (CVE-2021-44228)" in html and "2026-11-12" in html
    assert html.index("Open ports</div>") < html.index("Software</div>")

    md = generator.render(data, "md")
    assert "## 🛡️ Security" in md and "## 💡 Recommendations" not in md
    assert "- **Open ports**: 4 listening · 3 reachable from the network · 2 unexpected" in md
    assert "\n\n  Ports other machines can reach that no rule expects: 3306 (mysqld)." in md
    assert "- **Updates**: 5 security updates pending\n\n  " + UPDATES_HINT in md
    assert "- **SSH**: root may log in with a password (warned in 2 of 96 checks, last " in md
    assert "| Open ports | Warning | 4 listening" in md
    assert "| Firewall | Healthy | ufw active | 0 of 96 |" in md
    assert "| 3306 | mysqld | 0.0.0.0 | from the network | — | New |" in md
    assert "| 161/udp | — | 0.0.0.0 | from the network | — | Exposed |" in md
    assert "| 5432 | — | 127.0.0.1 | this server only | local only | Local |" in md
    assert "| Solr | 8.8.2 | — | Affected | affected by Log4Shell (CVE-2021-44228): upgrade to 8.11.1" in md
    assert "| PostgreSQL | 14 | 2026-11-12 | Ends soon | — |" in md

    assert json.loads(generator.render(data, "json"))["security"] == json.loads(json.dumps(sec))


def test_security_section_says_no_findings_when_every_rung_is_ok(
    mem_store: Store, security_on: Callable[[Config], Config]
) -> None:
    cfg = security_on(test_cfg())
    updates = "not available · no update information on this system (Ubuntu's update-notifier)"
    rungs = [
        _rung(
            "security.ports",
            "Open ports",
            "ok",
            "3 listening · 2 reachable from the network (22, 443) · none unexpected",
        ),
        _rung("security.firewall", "Firewall", "ok", "ufw active"),
        _rung("security.updates", "Updates", "unknown", updates, informational=True),
    ]
    metrics = {
        "ports": [
            _port(22, "expected", True, expected="open"),
            _port(443, "expected", True, expected="open", process="nginx"),
            _port(8983, "local", False, expected="local_only"),
        ],
        "software": [
            {"name": "Ubuntu", "version": "24.04", "until": "2029-04-30", "status": "ok", "note": ""}
        ],
    }
    mem_store.upsert_component_state(_security_state(rungs, metrics))
    data = analytics.compute_report_data(mem_store, cfg, T_FROM, NOW, now=NOW, sections=["security"])
    sec = data["security"]
    assert sec["verdict"] == "clear" and sec["findings"] == [] and sec["earlier"] == []
    assert sec["rungs"][2]["informational"] is True and sec["rungs"][2]["period_checks"] == 0
    assert sec["port_counts"] == {"listening": 3, "reachable": 2, "exposed": 0}  # counted from the table

    html = generator.render(data, "html")
    assert "✅ No security findings." in html and "Earlier in this period" not in html
    assert '<span class="pill unknown">Note</span>' in html and '<td class="num">—</td>' in html
    md = generator.render(data, "md")
    assert "_No security findings._" in md
    assert f"| Updates | Note | {updates} | — |" in md
    assert "| Ubuntu | 24.04 | 2029-04-30 | Supported | — |" in md


def test_security_section_when_the_module_is_off_or_has_no_result(
    mem_store: Store, security_on: Callable[[Config], Config]
) -> None:
    off = analytics.compute_report_data(mem_store, test_cfg(), T_FROM, NOW, now=NOW, sections=["security"])
    assert off["security"]["enabled"] is False and off["security"]["verdict"] == "off"
    assert off["security"]["rungs"] == [] and off["security"]["ports"] == []
    assert "🛡️ The Security module is off." in generator.render(off, "html")
    assert "_The Security module is off._" in generator.render(off, "md")
    # a report dict without the section (an older one) still renders
    del off["security"]
    assert "The Security module is off." in generator.render(off, "html")

    cfg = security_on(test_cfg())
    fresh = analytics.compute_report_data(mem_store, cfg, T_FROM, NOW, now=NOW, sections=["security"])
    assert fresh["security"]["enabled"] is True and fresh["security"]["verdict"] == "no_data"
    assert "🛡️ No security check has run yet." in generator.render(fresh, "html")
    assert "_No security check has run yet._" in generator.render(fresh, "md")

    failed = CheckResult(
        "security.error",
        "Security check",
        "unknown",
        NOW,
        message="check timed out after 30 s",
        detail={"collector_error": True},
    )
    mem_store.upsert_component_state(_security_state([failed], {}))
    data = analytics.compute_report_data(mem_store, cfg, T_FROM, NOW, now=NOW, sections=["security"])
    sec = data["security"]
    assert sec["verdict"] == "unchecked" and sec["reason"] == "check timed out after 30 s"
    assert sec["ports"] == [] and set(sec["port_counts"].values()) == {None}
    html = generator.render(data, "html")
    assert "The last security check gave no result: check timed out after 30 s" in html
    assert "No security findings" not in html and "Open ports</div>" not in html
    md = generator.render(data, "md")
    assert "_The last security check gave no result: check timed out after 30 s_" in md


# ------------------------------------------------------------------------------ generate


def test_generate_keeps_the_newest_reports_only(
    synthetic: tuple[Store, Config], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every POST /api/reports writes a file: the oldest go once the count or size cap is reached."""
    store, cfg = synthetic
    out = tmp_path / "capped"
    monkeypatch.setattr(generator, "MAX_REPORTS", 3)

    def make(i: int) -> Any:
        return generator.generate(
            store, cfg, T_FROM, NOW, "json", reports_dir=out, now=NOW + i, sections=["summary"]
        )

    made = [make(i) for i in range(5)]
    kept = store.reports(include_deleted=True)
    assert [r.id for r in kept] == [r.id for r in reversed(made[2:])]
    assert sorted(p.name for p in out.iterdir()) == sorted(Path(r.path).name for r in kept)
    assert any("Removed 1 old report" in e.message for e in store.events(kind="report"))
    # the size cap: room for two of them
    monkeypatch.setattr(generator, "MAX_REPORTS", 200)
    monkeypatch.setattr(generator, "MAX_REPORTS_BYTES", int(made[-1].size * 2.5))
    newest = make(9)
    assert [r.id for r in store.reports(include_deleted=True)] == [newest.id, made[-1].id]
    assert len(list(out.iterdir())) == 2


def test_generate_refuses_when_the_data_disk_is_nearly_full(
    synthetic: tuple[Store, Config], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, cfg = synthetic
    out = tmp_path / "full"

    def nearly_full(path: Any) -> Any:
        return shutil._ntuple_diskusage(100 * GIB, 99.5 * GIB, 512 * MIB)  # type: ignore[attr-defined]

    monkeypatch.setattr(generator.shutil, "disk_usage", nearly_full)
    with pytest.raises(generator.ReportSpaceError, match=r"not enough free disk space .* 512.0 MB free"):
        generator.generate(store, cfg, T_FROM, NOW, "html", reports_dir=out, now=NOW)
    assert store.reports(include_deleted=True) == []
    assert not out.exists() or list(out.iterdir()) == []
    assert issubclass(generator.ReportSpaceError, ValueError)  # the CLI and the API report it cleanly


def test_generate_writes_file_row_and_event(synthetic: tuple[Store, Config], tmp_path: Path) -> None:
    store, cfg = synthetic
    out = tmp_path / "out"
    report = generator.generate(store, cfg, T_FROM, NOW, "html", reports_dir=out, now=NOW, range_hint="24h")
    assert report.id == 1 and report.fmt == "html" and report.deleted_ts is None
    path = Path(report.path)
    assert path.parent == out and re.fullmatch(r"sentinel-report-\d{8}-\d{4}-24h\.html", path.name)
    assert path.stat().st_size == report.size > 10_000
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert path.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert store.get_report(1).path == report.path and store.reports()[0].id == 1
    events = store.events(kind="report")
    assert events and events[0].message.startswith("Report generated: DSpace Sentinel report")
    assert events[0].data["report_id"] == 1 and events[0].data["fmt"] == "html"
    # same minute, same range and format → a distinct file name
    again = generator.generate(store, cfg, T_FROM, NOW, "html", reports_dir=out, now=NOW, range_hint="24h")
    assert Path(again.path).name == path.name.replace(".html", "-2.html")
    md = generator.generate(store, cfg, T_FROM, NOW, "md", title="Custom title", reports_dir=out, now=NOW)
    assert md.title == "Custom title" and Path(md.path).suffix == ".md"
    js = generator.generate(store, cfg, T_FROM, NOW, "json", reports_dir=out, now=NOW, sections=["summary"])
    payload = json.loads(Path(js.path).read_text(encoding="utf-8"))
    assert payload["meta"]["sections"] == ["summary"] and payload["meta"]["title"] == js.title
    assert len(store.reports()) == 4
    with pytest.raises(ValueError):
        generator.generate(store, cfg, T_FROM, NOW, "pdf", reports_dir=out)
    with pytest.raises(ValueError):
        generator.generate(store, cfg, NOW, T_FROM, "html", reports_dir=out)


def test_generate_default_directory(
    synthetic: tuple[Store, Config], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, cfg = synthetic
    monkeypatch.setenv("SENTINEL_DATA_DIR", str(tmp_path / "env-data"))
    # a file-backed store: reports live next to its database, whatever the environment says
    report = generator.generate(store, cfg, T_FROM, NOW, "md", now=NOW)
    assert Path(report.path).parent == Path(store.path).parent / "reports"
    assert generator.default_reports_dir(store) == Path(store.path).parent / "reports"
    # an in-memory store falls back to the configured data directory
    mem = Store(":memory:")
    try:
        assert generator.default_reports_dir(mem) == tmp_path / "env-data" / "reports"
        assert generator.default_reports_dir(None) == tmp_path / "env-data" / "reports"
    finally:
        mem.close()


def test_parse_range_string_and_generate_for_range(synthetic: tuple[Store, Config], tmp_path: Path) -> None:
    store, cfg = synthetic
    assert generator.parse_range_string("24h", NOW) == (NOW - DAY, NOW, "24h")
    assert generator.parse_range_string(" 7D ", NOW) == (NOW - 7 * DAY, NOW, "7d")
    assert generator.parse_range_string("30d", NOW)[2] == "30d"
    assert generator.parse_range_string("3d", NOW) == (NOW - 3 * DAY, NOW, "3d")
    assert generator.parse_range_string("90m", NOW) == (NOW - 5400, NOW, "90m")
    for bad in ("yesterday", "", "0d", "400d", "7"):
        with pytest.raises(ValueError):
            generator.parse_range_string(bad, NOW)
    report = generator.generate_for_range(store, cfg, "24h", "json", reports_dir=tmp_path, now=NOW)
    assert (
        Path(report.path).name.endswith("-24h.json")
        and report.range_to == NOW
        and report.range_from == T_FROM
    )
    payload = json.loads(Path(report.path).read_text(encoding="utf-8"))
    assert payload["meta"]["range"]["label"] == "24h" and payload["summary"]["alerts_opened"] == 3
    other = generator.generate_for_range(store, cfg, "3d", "md", reports_dir=tmp_path, now=NOW)
    assert Path(other.path).name.endswith("-3d.md")
    assert generator.report_filename(NOW, "custom range", "html").endswith("-customrange.html")


# ------------------------------------------------------------------------------ telegram


class _Notifier:
    def __init__(self, result: Any = None, raise_exc: Exception | None = None, sync: bool = False) -> None:
        self.result = result if result is not None else {"ok": True, "message_id": 7}
        self.raise_exc = raise_exc
        self.sync = sync
        self.calls: list[tuple[Any, ...]] = []

    def send_document(self, cfg: Config, path: str, caption: str = "") -> Any:
        self.calls.append((cfg, path, caption))
        if self.raise_exc:
            raise self.raise_exc
        if self.sync:
            return self.result

        async def _run() -> Any:
            return self.result

        return _run()


def _telegram_cfg() -> Config:
    cfg = test_cfg()
    cfg.notifiers.telegram.enabled = True
    cfg.notifiers.telegram.chats = [
        # a made-up token, split in two so that secret scanners do not report it
        TelegramChat(bot_token="123456789:" + "AAH-reports-test-token-000000001", chat_id="42")
    ]
    return cfg


async def test_send_report_telegram_paths(synthetic: tuple[Store, Config], tmp_path: Path) -> None:
    store, cfg = synthetic
    report = generator.generate(store, cfg, T_FROM, NOW, "html", reports_dir=tmp_path, now=NOW)
    off = await generator.send_report_telegram(cfg, report)
    assert off == {"ok": False, "error": "Telegram is not enabled", "skipped": True}
    tg = _telegram_cfg()
    tg.notifiers.telegram.chats[0].chat_id = ""
    assert await generator.send_report_telegram(tg, report) == {
        "ok": False,
        "error": "Telegram has no bot yet: add a bot token and a chat id",
        "skipped": True,
    }
    tg = _telegram_cfg()
    notifier = _Notifier()
    ok = await generator.send_report_telegram(tg, report, notifier=notifier)
    assert ok["ok"] is True and ok["message_id"] == 7
    assert notifier.calls[0][1] == report.path and notifier.calls[0][2].startswith("📄 ")
    assert "Last 24 hours" in notifier.calls[0][2]
    sync_false = await generator.send_report_telegram(tg, report, notifier=_Notifier(result=False, sync=True))
    assert sync_false == {"ok": False, "error": "Telegram send failed"}
    boom = await generator.send_report_telegram(
        tg, report, notifier=_Notifier(raise_exc=RuntimeError("net down"))
    )
    assert boom["ok"] is False and "net down" in boom["error"]
    delivered = await generator.deliver_telegram_async(tg, report, store=store, notifier=_Notifier())
    assert delivered["ok"] is True
    assert store.events(kind="notify")[0].message == f"Report sent to Telegram: {report.title}"


async def test_report_captions_reach_telegram_as_plain_text(tmp_path: Path) -> None:
    """R1#4: a title with '<' or '&' went out with parse_mode HTML and every bot refused it ("can't
    parse entities"); the caption carries no markup, so it travels as plain text."""
    import httpx

    from dspace_sentinel.alerts.notifiers import telegram
    from dspace_sentinel.models import Report

    path = tmp_path / "sentinel-report.html"
    path.write_text("<html></html>", encoding="utf-8")
    report = Report(
        id=1,
        created_ts=NOW,
        title="Solr <draft> & PostgreSQL",
        range_from=T_FROM,
        range_to=NOW,
        fmt="html",
        path=str(path),
        size=13,
    )
    caption = generator.telegram_caption(report)
    assert caption.startswith("📄 Solr <draft> & PostgreSQL\nLast 24 hours · ")
    seen: list[httpx.Request] = []

    def bot(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 3}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(bot)) as client:
        result = await telegram.send_document(_telegram_cfg(), path, caption, client=client)
    assert result["ok"] is True
    body = seen[0].content.decode()
    assert 'name="caption"\r\n\r\n📄 Solr <draft> & PostgreSQL\n' in body
    assert 'name="parse_mode"' not in body


def test_send_document_signature_adapter() -> None:
    cfg = Config()
    seen: list[tuple[Any, ...]] = []

    def two(path: str, caption: str) -> bool:
        seen.append(("two", path, caption))
        return True

    def three(cfg_: Config, path: str, caption: str = "") -> dict[str, Any]:
        seen.append(("three", cfg_, path))
        return {"ok": True}

    def kw(path: str, caption: str, *, cfg: Config) -> None:
        seen.append(("kw", cfg))

    assert generator._call_send_document(two, cfg, "/p", "c") is True
    assert generator._call_send_document(three, cfg, "/p", "c") == {"ok": True}
    assert generator._call_send_document(kw, cfg, "/p", "c") is None
    assert [s[0] for s in seen] == ["two", "three", "kw"] and seen[1][1] is cfg and seen[2][1] is cfg
    assert generator._normalise_result(None) == {"ok": True, "error": None}
    assert generator._normalise_result({"ok": 0, "error": "x"}) == {"ok": False, "error": "x"}


def test_deliver_telegram_sync_and_missing_notifier(
    synthetic: tuple[Store, Config], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, cfg = synthetic
    tg = _telegram_cfg()
    report = generator.generate(store, cfg, T_FROM, NOW, "md", reports_dir=tmp_path, now=NOW)
    result = generator.deliver_telegram(tg, report, store=store, notifier=_Notifier())
    assert result["ok"] is True
    monkeypatch.setattr(
        generator,
        "_resolve_send_document",
        lambda notifier=None: (None, "Telegram notifier is not available (ImportError)"),
    )
    missing = generator.deliver_telegram(tg, report, store=store)
    assert missing["ok"] is False and "not available" in missing["error"]
    assert store.events(kind="notify.error")[0].message.startswith("Report not sent to Telegram")
    # generate(send_telegram=True) never raises either
    again = generator.generate(
        store, tg, T_FROM, NOW, "md", send_telegram=True, reports_dir=tmp_path, now=NOW
    )
    assert again.id and store.events(kind="notify.error")[0].data["report_id"] == again.id


def test_resolve_send_document_uses_real_notifier_when_present() -> None:
    func, reason = generator._resolve_send_document()
    if func is None:
        assert reason and "not available" in reason
    else:
        assert callable(func) and reason is None


# ------------------------------------------------------------------------------ schedule


def _local(year: int, month: int, day: int, hour: int, minute: int = 0) -> float:
    return _dt.datetime(year, month, day, hour, minute).timestamp()


def test_weekly_due() -> None:
    cfg = Config()
    monday_9 = _local(2026, 9, 21, 9)  # 2026-09-21 is a Monday
    assert schedule.weekly_due(cfg, monday_9, None) is None  # disabled
    cfg.reports.weekly.enabled = True
    cfg.reports.weekly.weekday = 1
    cfg.reports.weekly.hour = 8
    assert schedule.weekly_due(cfg, monday_9, None) == "2026-09-21"
    assert (
        schedule.weekly_due(cfg, _local(2026, 9, 21, 23, 59), None) == "2026-09-21"
    )  # missed 08:00, same day
    assert schedule.weekly_due(cfg, _local(2026, 9, 21, 7, 59), None) is None
    assert schedule.weekly_due(cfg, _local(2026, 9, 22, 9), None) is None  # Tuesday
    assert schedule.weekly_due(cfg, monday_9, "2026-09-21") is None  # already done
    assert schedule.weekly_due(cfg, monday_9, "2026-09-14") == "2026-09-21"
    for sunday in (0, 7):
        cfg.reports.weekly.weekday = sunday
        assert schedule.weekly_due(cfg, _local(2026, 9, 27, 8), None) == "2026-09-27"


async def test_maybe_run_generates_once_and_retries_on_failure(
    synthetic: tuple[Store, Config], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, cfg = synthetic
    monkeypatch.setenv("SENTINEL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(schedule, "_last_purge_ts", 0.0)
    cfg.reports.weekly.enabled = True
    cfg.reports.weekly.weekday = 4  # NOW is Thursday 2026-09-24 12:00 local
    cfg.reports.weekly.hour = 8
    cfg.reports.weekly.send_telegram = True  # Telegram is off → skipped quietly
    report = await schedule.maybe_run(store, cfg, NOW)
    assert report is not None and report.fmt == "html"
    assert report.title.startswith("Weekly report · test-host · week ending")
    assert report.range_to == NOW and report.range_from == NOW - 7 * DAY
    assert Path(report.path).name.endswith("-7d.html")
    assert (
        store.kv_get(schedule.WEEKLY_KV_KEY) == "2026-09-24" and store.kv_get(schedule.ATTEMPT_KV_KEY) is None
    )
    # the weekly job records a delivery that did not go out as notify.error, never as notify
    assert [e.message for e in store.events(kind="notify.error")] == [
        "Report not sent to Telegram: Telegram is not enabled"
    ]
    assert store.events(kind="notify") == []
    assert await schedule.maybe_run(store, cfg, NOW + 600) is None  # once per due day
    assert len(store.reports()) == 1
    # next week, generation fails → retried after RETRY_S, not every cycle
    later = NOW + 7 * DAY
    calls: list[float] = []

    def boom(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs.get("now", 0.0))
        raise RuntimeError("disk full")

    monkeypatch.setattr(generator, "generate", boom)
    assert await schedule.maybe_run(store, cfg, later) is None
    assert store.kv_get(schedule.ATTEMPT_KV_KEY) == str(int(later))
    assert await schedule.maybe_run(store, cfg, later + 60) is None
    assert len(calls) == 1
    assert await schedule.maybe_run(store, cfg, later + schedule.RETRY_S + 1) is None
    assert len(calls) == 2
    assert store.events(kind="report")[0].message.startswith("Weekly report failed")
    # not due: disabled, wrong day
    cfg.reports.weekly.enabled = False
    assert await schedule.maybe_run(store, cfg, later + 2 * schedule.RETRY_S) is None


async def test_maybe_run_purges_soft_deleted_reports(
    synthetic: tuple[Store, Config], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, cfg = synthetic
    monkeypatch.setattr(schedule, "_last_purge_ts", 0.0)
    report = generator.generate(store, cfg, T_FROM, NOW, "json", reports_dir=tmp_path, now=NOW)
    store.soft_delete_report(report.id, NOW)
    await schedule.maybe_run(store, cfg, NOW + 3600)  # within 24 h → kept
    assert Path(report.path).exists() and store.get_report(report.id, include_deleted=True) is not None
    monkeypatch.setattr(schedule, "_last_purge_ts", 0.0)
    await schedule.maybe_run(store, cfg, NOW + REPORT_PURGE_AFTER_S + 1)
    assert not Path(report.path).exists() and store.get_report(report.id, include_deleted=True) is None


# ------------------------------------------------------------------------------ email


class _Mailbox:
    """Stands in for smtplib.SMTP and keeps every message it was handed."""

    sent: list[Any] = []

    def __init__(self, host: str, port: int, **kwargs: Any) -> None:
        pass

    def ehlo(self) -> None:
        pass

    def starttls(self, context: Any) -> None:
        pass

    def login(self, user: str, password: str) -> None:
        pass

    def send_message(self, msg: Any, from_addr: str, to_addrs: list[str]) -> dict[str, Any]:
        _Mailbox.sent.append((msg, to_addrs))
        return {}

    def quit(self) -> None:
        pass


@pytest.fixture
def mailbox(monkeypatch: pytest.MonkeyPatch) -> type[_Mailbox]:
    import smtplib

    _Mailbox.sent = []
    monkeypatch.setattr(smtplib, "SMTP", _Mailbox)
    return _Mailbox


def _email_cfg(enabled: bool = True) -> Config:
    from dspace_sentinel.config import merge_patch

    return merge_patch(
        test_cfg(),
        {
            "notifiers": {
                "email": {
                    "enabled": enabled,
                    "username": "sentinel@example.org",
                    "password": "abcdefghijklmnop",
                    "recipients": [
                        {"address": "ops@example.org"},
                        {"address": "boss@example.org", "min_severity": "critical"},
                    ],
                }
            }
        },
    )


async def test_send_report_email_paths(
    synthetic: tuple[Store, Config], tmp_path: Path, mailbox: type[_Mailbox]
) -> None:
    store, cfg = synthetic
    report = generator.generate(store, cfg, T_FROM, NOW, "md", reports_dir=tmp_path, now=NOW)
    assert await generator.send_report_email(cfg, report) == {
        "ok": False,
        "error": "email is not enabled",
        "skipped": True,
    }
    unset = _email_cfg()
    unset.notifiers.email.recipients = []
    assert await generator.send_report_email(unset, report) == {
        "ok": False,
        "error": "email is not set up: add the Gmail address, app password and at least one recipient",
        "skipped": True,
    }
    sent = await generator.send_report_email(_email_cfg(), report)
    assert sent["ok"] is True and sent["error"] is None
    assert [r["address"] for r in sent["results"]] == ["ops@example.org", "boss@example.org"]
    ((msg, to),) = mailbox.sent
    assert to == ["ops@example.org", "boss@example.org"]  # reports go to everyone, whatever their severity
    assert msg["Subject"] == f"[Sentinel] Report · test-host · {report.title}"
    attachment = next(msg.iter_attachments())
    assert (
        attachment.get_filename() == Path(report.path).name
        and attachment.get_content_type() == "text/markdown"
    )
    assert "Last 24 hours" in msg.get_body(("plain",)).get_content()
    demo = _email_cfg()
    demo.demo = True
    assert await generator.send_report_email(demo, report) == {"ok": True, "error": None, "skipped": "demo"}
    assert len(mailbox.sent) == 1


async def test_email_deliveries_are_recorded(
    synthetic: tuple[Store, Config], tmp_path: Path, mailbox: type[_Mailbox]
) -> None:
    store, cfg = synthetic
    report = generator.generate(store, cfg, T_FROM, NOW, "html", reports_dir=tmp_path, now=NOW)
    delivered = await generator.deliver_email_async(_email_cfg(), report, store=store)
    assert delivered["ok"] is True
    row = store.events(kind="notify")[0]
    assert row.message == f"Report sent by email: {report.title}"
    assert row.data["report_id"] == report.id and row.data["channel"] == "email"
    # the synchronous twin, as the CLI calls it (no running event loop in that thread)
    failed = await asyncio.to_thread(generator.deliver_email, _email_cfg(enabled=False), report, store=store)
    assert failed == {"ok": False, "error": "email is not enabled", "skipped": True}
    assert store.events(kind="notify.error")[0].message == "Report not sent by email: email is not enabled"
    demo = _email_cfg()
    demo.demo = True
    await generator.deliver_email_async(demo, report, store=store)
    (logged,) = store.events(kind="notify.demo")
    assert logged.message == f"Report by email (demo, not sent): {report.title}"
    assert logged.data["channels"] == ["email"] and logged.data["subject"].startswith("[Sentinel] Report · ")
    # Telegram's rows say which channel they are about too (switched off: not sent)
    await generator.deliver_telegram_async(cfg, report, store=store)
    assert store.events(kind="notify.error")[0].data["channel"] == "telegram"


async def test_failed_report_deliveries_are_recorded_as_errors(
    synthetic: tuple[Store, Config], tmp_path: Path, mailbox: type[_Mailbox]
) -> None:
    """R1#extra2: like alert deliveries, a report that did not go out is a ``notify.error`` row;
    ``notify`` stays for what was sent (or scheduled) and ``notify.demo`` for the demo."""
    store, cfg = synthetic
    report = generator.generate(store, cfg, T_FROM, NOW, "html", reports_dir=tmp_path, now=NOW)
    refused = _Notifier(result={"ok": False, "error": "chat not found — send the bot a message first"})
    await generator.deliver_telegram_async(_telegram_cfg(), report, store=store, notifier=refused)
    await generator.deliver_email_async(_email_cfg(enabled=False), report, store=store)
    errors = store.events(kind="notify.error")
    assert [(e.message, e.data["channel"]) for e in reversed(errors)] == [
        ("Report not sent to Telegram: chat not found — send the bot a message first", "telegram"),
        ("Report not sent by email: email is not enabled", "email"),
    ]
    assert store.events(kind="notify") == []
    await generator.deliver_email_async(_email_cfg(), report, store=store)
    assert [e.message for e in store.events(kind="notify")] == [f"Report sent by email: {report.title}"]


async def test_weekly_report_goes_by_email_too(
    synthetic: tuple[Store, Config], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mailbox: type[_Mailbox]
) -> None:
    store, _ = synthetic
    cfg = _email_cfg()
    monkeypatch.setattr(schedule, "_last_purge_ts", 0.0)
    monkeypatch.setattr(generator, "default_reports_dir", lambda store=None: tmp_path)
    cfg.reports.weekly.enabled = True
    cfg.reports.weekly.weekday = 4  # NOW is Thursday 2026-09-24 12:00 local
    cfg.reports.weekly.hour = 8
    cfg.reports.weekly.send_email = True
    report = await schedule.maybe_run(store, cfg, NOW)
    assert report is not None
    ((msg, to),) = mailbox.sent
    assert msg["Subject"] == f"[Sentinel] Report · test-host · {report.title}" and len(to) == 2
    assert next(msg.iter_attachments()).get_filename() == Path(report.path).name
    assert store.events(kind="notify")[0].message == f"Report sent by email: {report.title}"
