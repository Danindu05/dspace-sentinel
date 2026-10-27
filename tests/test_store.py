"""Store: schema, series bucketing, availability, rollup, purge, alerts/incidents/reports, export."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from dspace_sentinel.models import Alert, CheckResult, ComponentHealth, Incident, Report, SystemSample
from dspace_sentinel.store import SCHEMA_VERSION, Store, bucket_for_range


def sample(
    ts: float, cpu: float = 10.0, mem: float = 50.0, disk: float = 40.0, load: float = 0.5
) -> SystemSample:
    return SystemSample(
        ts=ts,
        cpu_pct=cpu,
        cpu_per_core=[cpu, cpu],
        mem_total=16,
        mem_used=8,
        mem_available=8,
        mem_pct=mem,
        mem_cached=1,
        swap_total=4,
        swap_used=0,
        swap_pct=0.0,
        disk_total=100,
        disk_used=40,
        disk_free=60,
        disk_pct=disk,
        disk_path="/",
        inode_pct=5.0,
        load1=load,
        load5=load,
        load15=load,
        cores=2,
        net_rx_bps=1000.0,
        net_tx_bps=2000.0,
        uptime_s=100.0,
        os_name="Ubuntu",
        os_version="24.04",
        kernel="6.8",
        hostname="t",
        disks=[],
    )


def test_schema_and_pragmas(store: Store) -> None:
    counts = store.table_counts()
    assert set(counts) >= {
        "samples",
        "samples_hourly",
        "checks",
        "component_state",
        "component_history",
        "alerts",
        "incidents",
        "reports",
        "repository_stats",
        "events",
        "kv",
        "sessions",
    }
    assert store.kv_get("schema_version") == str(SCHEMA_VERSION)
    conn = sqlite3.connect(store.path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    conn.close()
    assert store.db_size_bytes() > 0
    # reopening keeps the schema version and data
    store.kv_set("x", "1")
    store.close()
    again = Store(store.path)
    assert again.kv_get("x") == "1"
    again.close()


def test_an_older_database_gets_the_sessions_table(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    Store(path).close()
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE sessions")
    conn.commit()
    conn.close()
    again = Store(path)
    try:
        assert again.table_counts()["sessions"] == 0
        assert again.kv_get("schema_version") == str(SCHEMA_VERSION)
    finally:
        again.close()


def test_database_wal_and_shm_are_private(tmp_path: Path) -> None:
    """The WAL holds recent writes (alerts, config-derived events): never world-readable."""
    path = tmp_path / "private.db"
    store = Store(path)
    try:
        store.kv_set("x", "1")
        for suffix in ("", "-wal", "-shm"):
            mode = Path(f"{path}{suffix}").stat().st_mode & 0o777
            assert mode == 0o600, (suffix, oct(mode))
    finally:
        store.close()
    # an older database created with the default umask is tightened on open
    loose = tmp_path / "loose.db"
    Store(loose).close()
    for suffix in ("", "-wal", "-shm"):
        target = Path(f"{loose}{suffix}")
        if target.exists():
            target.chmod(0o644)
    again = Store(loose)
    try:
        again.kv_set("y", "2")
        assert all(
            Path(f"{loose}{suffix}").stat().st_mode & 0o777 == 0o600 for suffix in ("", "-wal", "-shm")
        )
    finally:
        again.close()


def test_bucket_rule() -> None:
    assert bucket_for_range(3600) == 15
    assert bucket_for_range(6 * 3600) == 60
    assert bucket_for_range(86400) == 300
    assert bucket_for_range(7 * 86400) == 1800
    assert bucket_for_range(14 * 86400) == 3600
    assert bucket_for_range(30 * 86400) == 7200
    assert bucket_for_range(365 * 86400) % 3600 == 0
    for span in (3600, 6 * 3600, 86400, 7 * 86400, 10 * 86400, 14 * 86400, 30 * 86400, 365 * 86400):
        assert span / bucket_for_range(span) <= 360


def test_series_bucketing_and_net(mem_store: Store) -> None:
    now = int(time.time())
    for i in range(240):  # one hour at 15 s
        mem_store.insert_sample(sample(now - 3600 + i * 15, cpu=float(i % 100)))
    s = mem_store.series("cpu", now - 3600, now)
    assert s["unit"] == "%" and s["bucket_s"] == 15
    assert 230 <= len(s["points"]) <= 241
    ts, avg, mx = s["points"][0]
    assert isinstance(ts, int) and avg is not None and mx >= avg
    s6 = mem_store.series("cpu", now - 6 * 3600, now)
    assert s6["bucket_s"] == 60 and len(s6["points"]) <= 61
    net = mem_store.series("net", now - 3600, now)
    assert "series" in net and net["series"]["rx"][0][1] == 1000.0 and net["series"]["tx"][0][1] == 2000.0
    with pytest.raises(ValueError):
        mem_store.series("bogus", 0, 1)
    assert mem_store.count_samples() == 240
    lo, hi = mem_store.sample_time_range()
    assert hi - lo == 239 * 15


def test_series_uses_hourly_beyond_7d(mem_store: Store) -> None:
    now = int(time.time()) // 3600 * 3600 + 1800  # mid-hour: "now - 10" stays in the current hour
    for i in range(48):
        mem_store.insert_sample(sample(now - 10 * 86400 + i * 3600, cpu=20.0))
    assert mem_store.rollup_hourly(now) == 48
    s = mem_store.series("cpu", now - 30 * 86400, now)
    assert s["bucket_s"] == 7200
    assert 24 <= len(s["points"]) <= 25  # 48 hourly rows in 2 h buckets (alignment may add one)
    assert s["points"][0][1] == 20.0
    # 14 days still fits hourly buckets
    assert mem_store.series("cpu", now - 14 * 86400, now)["bucket_s"] == 3600
    # idempotent: nothing new to roll up
    assert mem_store.rollup_hourly(now) == 0
    # a sample that lands in an hour not rolled up yet produces exactly one new hourly row
    mem_store.insert_sample(sample(now - 3 * 3600, cpu=90.0))
    assert mem_store.rollup_hourly(now) == 1
    # a late sample inside an already rolled-up hour re-aggregates just that hour
    mem_store.insert_sample(sample(now - 10 * 86400 + 30, cpu=40.0))
    assert mem_store.rollup_hourly(now) == 1
    # read it back through the 14 d range (3600 s buckets = one hourly row per point)
    first = mem_store.series("cpu", now - 14 * 86400, now)["points"][0]
    assert first[1] == 30.0 and first[2] == 40.0
    # the 30 d range (7200 s buckets) averages the hourly averages and keeps the max
    wide = mem_store.series("cpu", now - 30 * 86400, now)["points"][0]
    assert 20.0 <= wide[1] <= 30.0 and wide[2] == 40.0
    # swap has no hourly column: long ranges come from raw samples
    swap = mem_store.series("swap", now - 30 * 86400, now)
    assert swap["bucket_s"] == 7200 and swap["points"] and swap["points"][0][1] == 0.0
    # the current (incomplete) hour is never rolled up
    mem_store.insert_sample(sample(now - 10, cpu=99.0))
    assert mem_store.rollup_hourly(now) == 0


def test_purge_never_leaves_a_half_purged_hour_to_the_rollup(mem_store: Store) -> None:
    """The retention cutoff falls inside an hour: the roll-up of that hour must keep its peak."""
    hour = 1_800_000_000 // 3600 * 3600
    for i in range(240):  # one hour of 15 s samples: CPU 95 % in the first half, 10 % in the second
        mem_store.insert_sample(sample(hour + i * 15, cpu=95.0 if i < 120 else 10.0))
    assert mem_store.rollup_hourly(hour + 3 * 3600) == 1

    def hourly() -> tuple[float, float]:
        row = mem_store._fetchone("SELECT cpu_avg, cpu_max FROM samples_hourly WHERE ts=?", (hour,))
        return row["cpu_avg"], row["cpu_max"]

    def raw_in_hour() -> int:
        return mem_store.sample_stats(hour, hour + 3599)["n"]

    assert hourly() == (52.5, 95.0)
    now = hour + 1800 + 30 * 86400  # 30 days of retention: the cutoff is half past that hour
    mem_store.purge(30, now)
    assert raw_in_hour() in (0, 240)  # the hour goes as a whole, never half of it
    assert mem_store.rollup_hourly(now - 2 * 3600) == 0
    assert hourly() == (52.5, 95.0)
    mem_store.purge(30, now + 3600)  # an hour later the whole hour is past the cutoff
    assert raw_in_hour() == 0 and hourly() == (52.5, 95.0)


def test_long_ranges_reach_the_newest_samples(mem_store: Store) -> None:
    """The hourly roll-up lags 2-4 h; a 30 d chart still ends at the newest raw sample."""
    now = 1_800_000_000 // 3600 * 3600 + 1234
    mem_store.insert_samples_bulk(
        sample(ts, cpu=80.0 if ts >= now - 3 * 3600 else 20.0) for ts in range(now - 10 * 86400, now, 60)
    )
    mem_store.rollup_hourly(now - 4 * 3600)
    for metric in ("cpu", "mem", "disk", "load"):
        s = mem_store.series(metric, now - 30 * 86400, now)
        assert s["bucket_s"] == 7200
        newest = s["points"][-1]
        assert now - newest[0] < 7200, (metric, now - newest[0])
    cpu = mem_store.series("cpu", now - 30 * 86400, now)["points"]
    assert cpu[-1][1] == 80.0 and cpu[-1][2] == 80.0  # read from the raw samples
    assert cpu[0][1] == 20.0  # the rolled-up part is unchanged
    assert len({p[0] for p in cpu}) == len(cpu)  # one point per bucket, hourly and raw merged
    net = mem_store.series("net", now - 30 * 86400, now)["series"]
    assert now - net["rx"][-1][0] < 7200 and now - net["tx"][-1][0] < 7200
    # nothing rolled up yet: the whole range comes from the raw samples
    fresh = Store(":memory:")
    fresh.insert_samples_bulk(sample(ts, cpu=30.0) for ts in range(now - 2 * 86400, now, 60))
    points = fresh.series("cpu", now - 30 * 86400, now)["points"]
    assert points and points[0][1] == 30.0 and now - points[-1][0] < 7200
    fresh.close()


def test_checks_and_latency_series(mem_store: Store) -> None:
    now = int(time.time())
    checks = [
        CheckResult("backend.rest", "GET /server/api", "ok", now - 60 + i, latency_ms=100 + i)
        for i in range(30)
    ]
    assert mem_store.insert_checks(checks) == 30
    ls = mem_store.latency_series("backend.rest", now - 3600, now)
    assert ls["check"] == "backend.rest" and ls["points"]
    assert ls["points"][-1][2] >= ls["points"][-1][1]
    latest = mem_store.latest_check("backend.rest")
    assert latest["latency_ms"] == 129
    assert len(mem_store.checks_between(now - 3600, now, "backend.rest")) == 30
    assert mem_store.insert_checks([]) == 0


def test_component_state_and_history_availability(mem_store: Store) -> None:
    now = int(time.time())
    comp = ComponentHealth(
        id="postgres",
        label="PostgreSQL",
        emoji="🐘",
        status="ok",
        reason=None,
        chain=[CheckResult("postgres.query", "SELECT 1", "ok", now, 1.2)],
        metrics={"connections": 3},
        depends_on=[],
        latency_ms=1.2,
        since=now - 100,
        checked_at=now,
    )
    mem_store.upsert_component_state(comp)
    comp.status = "crit"
    comp.reason = "connection refused"
    mem_store.upsert_component_state(comp)
    row = mem_store.component_state("postgres")
    assert (
        row["status"] == "crit"
        and row["metrics"] == {"connections": 3}
        and row["chain"][0]["id"] == "postgres.query"
    )
    assert set(mem_store.component_states()) == {"postgres"}
    mem_store.delete_component_state("postgres")
    assert mem_store.component_state("postgres") is None

    # 60 points over one hour: first 45 ok, then 10 crit, then 5 ok
    statuses = ["ok"] * 45 + ["crit"] * 10 + ["ok"] * 5
    for i, st in enumerate(statuses):
        mem_store.insert_component_history(now - 3600 + i * 60, "postgres", st, 1.0)
    av = mem_store.availability("postgres", now - 3600, now, buckets=12)
    assert av["samples"] == 60
    assert av["uptime_pct"] == pytest.approx(100 * 50 / 60, abs=0.01)
    assert av["outages"] == 1
    assert len(av["buckets"]) == 12
    assert "crit" in av["buckets"] and av["buckets"][0] == "ok"
    empty = mem_store.availability("solr", now - 3600, now, buckets=4)
    assert empty["uptime_pct"] is None and empty["buckets"] == [None] * 4
    assert len(mem_store.component_history("postgres", now - 3600, now)) == 60


def test_downtime_is_measured_from_the_rows_not_the_range(mem_store: Store) -> None:
    """One day of history (every 30 s) with a 10-minute outage is 600 s down in any range."""
    now = 1_800_000_000
    start = now - 86400
    outage = start + 43200
    rows = [
        (ts, "backend", "crit" if outage <= ts < outage + 600 else "ok", 90.0) for ts in range(start, now, 30)
    ]
    mem_store.insert_component_history_rows(rows)
    for days in (1, 7, 30):
        av = mem_store.availability("backend", now - days * 86400, now)
        assert (av["downtime_s"], av["outages"]) == (600, 1), days
        assert av["covered_s"] == pytest.approx(86400, abs=60), days  # the history covers one day
    # a gap in the history (Sentinel stopped) is not downtime, even right after a crit row
    later = now + 7200
    mem_store.insert_component_history_rows([(later, "backend", "crit", None)])
    mem_store.insert_component_history_rows([(later + 30 * k, "backend", "ok", 90.0) for k in range(1, 5)])
    av = mem_store.availability("backend", start, later + 150)
    assert av["downtime_s"] == 600 + 30 and av["outages"] == 2


def test_a_check_stands_until_the_next_one_in_the_strip(mem_store: Store) -> None:
    """A component that is checked every 15 minutes (Repository) is not "no data" in between:
    on the owner's server the one-hour strip showed four green minutes and 56 grey ones."""
    now = 1_800_000_000
    start = now - 3600
    stamps = [start + 330 + k * 900 for k in range(4)]  # minutes 5, 20, 35 and 50 of the hour
    mem_store.insert_component_history_rows([(ts, "repository", "ok", None) for ts in stamps])
    av = mem_store.availability("repository", start, now, buckets=60)
    assert av["samples"] == 4 and av["uptime_pct"] == 100.0
    assert av["buckets"][:5] == [None] * 5  # nothing is known before the first check
    assert av["buckets"][5:] == ["ok"] * 55  # from then on every minute has that check's status


def test_the_strip_has_no_data_only_where_nothing_was_checked(mem_store: Store) -> None:
    """The last check before the range covers its start, a status lasts until the next check,
    and a silence of more than twice the usual spacing (Sentinel stopped) stays "no data"."""
    now = 1_800_000_000
    start = now - 3600
    mem_store.insert_component_history_rows(
        [
            (start - 270, "logs", "ok", None),  # before the range: covers its first minutes
            (start + 630, "logs", "crit", None),  # minute 10
            (start + 1530, "logs", "ok", None),  # minute 25, then nothing for 34 minutes
        ]
    )
    av = mem_store.availability("logs", start, now, buckets=60)
    assert av["samples"] == 2  # the row before the range is not counted
    assert av["buckets"] == ["ok"] * 10 + ["crit"] * 15 + ["ok"] * 31 + [None] * 4


def test_one_check_stands_for_its_interval_when_the_caller_knows_it(mem_store: Store) -> None:
    now = 1_800_000_000
    start = now - 3600
    mem_store.insert_component_history_rows([(start + 90, "assetstore", "ok", None)])
    alone = mem_store.availability("assetstore", start, now, buckets=60)
    assert alone["buckets"].count("ok") == 1  # one row says nothing about how long it stands
    known = mem_store.availability("assetstore", start, now, buckets=60, interval_s=900)
    assert known["buckets"][0] is None
    assert known["buckets"][1:32] == ["ok"] * 31  # its own minute, then twice the interval
    assert known["buckets"][32:] == [None] * 28


def test_alert_crud(mem_store: Store) -> None:
    now = time.time()
    a = Alert(None, "cpu", "server", "warning", "open", "CPU 85 %", "load 3.1", 85.0, 80.0, now)
    aid = mem_store.insert_alert(a)
    assert aid == a.id == 1
    assert [x.id for x in mem_store.open_alerts()] == [1]
    assert mem_store.ack_alert(1, now + 5).acked_ts == int(now + 5)
    assert mem_store.unack_alert(1).acked_ts is None
    esc = mem_store.update_alert(1, severity="critical", value=97.0)
    assert esc.severity == "critical" and esc.value == 97.0
    assert mem_store.update_alert(1, bogus=1).id == 1  # unknown columns ignored
    res = mem_store.resolve_alert(1, now + 60)
    assert res.status == "resolved" and res.resolved_ts == int(now + 60)
    assert mem_store.open_alerts() == []
    assert len(mem_store.alerts("resolved")) == 1
    assert len(mem_store.alerts("all")) == 1
    assert mem_store.alerts_between(now - 1, now + 1)[0].id == 1
    assert mem_store.get_alert(99) is None
    assert mem_store.update_alert(99, status="open") is None


def test_incident_crud(mem_store: Store) -> None:
    now = time.time()
    ids = [
        mem_store.insert_alert(
            Alert(None, "memory", "server", "critical", "open", "Memory 94 %", "", 94, 92, now)
        ),
        mem_store.insert_alert(
            Alert(
                None,
                "component_down:solr",
                "solr",
                "critical",
                "open",
                "Solr unreachable",
                "",
                None,
                None,
                now + 30,
            )
        ),
    ]
    inc = Incident(
        None,
        "Possible Solr resource exhaustion",
        "Possible Solr resource exhaustion",
        "open",
        now,
        None,
        ["DSpace Search", "DSpace Backend"],
        [{"ts": now, "text": "Memory 94 %", "severity": "critical"}],
        ids,
    )
    iid = mem_store.insert_incident(inc)
    got = mem_store.get_incident(iid)
    assert got.alert_ids == ids and got.affected == ["DSpace Search", "DSpace Backend"]
    assert got.sequence[0]["text"] == "Memory 94 %"
    assert mem_store.get_alert(ids[0]).incident_id == iid
    assert [i.id for i in mem_store.open_incidents()] == [iid]
    assert [a.id for a in mem_store.alerts_for_incident(iid)] == ids
    third = mem_store.insert_alert(
        Alert(None, "search_latency", "backend", "warning", "open", "slow", "", 2, 1.5, now + 60)
    )
    mem_store.attach_alert_to_incident(third, iid)
    assert len(mem_store.get_incident(iid).alert_ids) == 3
    upd = mem_store.update_incident(iid, status="resolved", ended_ts=now + 600, affected=["x"])
    assert upd.status == "resolved" and upd.ended_ts == int(now + 600) and upd.affected == ["x"]
    assert mem_store.open_incidents() == []
    assert len(mem_store.incidents("resolved")) == 1
    assert mem_store.get_incident(42) is None


def test_reports_soft_delete_and_purge(mem_store: Store, tmp_path: Path) -> None:
    now = time.time()
    f = tmp_path / "r.html"
    f.write_text("<html></html>")
    rid = mem_store.insert_report(Report(None, now, "Weekly", now - 86400, now, "html", str(f), 13))
    assert [r.id for r in mem_store.reports()] == [rid]
    deleted = mem_store.soft_delete_report(rid, now)
    assert deleted.deleted_ts == int(now)
    assert mem_store.reports() == []
    assert mem_store.get_report(rid) is None
    assert mem_store.get_report(rid, include_deleted=True).id == rid
    assert len(mem_store.reports(include_deleted=True)) == 1
    restored = mem_store.restore_report(rid)
    assert restored.deleted_ts is None and mem_store.get_report(rid).id == rid
    mem_store.soft_delete_report(rid, now - 2 * 86400)
    assert mem_store.purge_reports(now - 86400) == 1
    assert not f.exists()
    assert mem_store.get_report(rid, include_deleted=True) is None


def test_repository_stats(mem_store: Store) -> None:
    now = int(time.time())
    mem_store.insert_repository_stats(
        now - 86400,
        {
            "items": 100,
            "bitstreams": 300,
            "communities": 2,
            "collections": 5,
            "db_size": 1000,
            "assetstore_bytes": 5000,
            "eperson_count": 9,
        },
    )
    mem_store.insert_repository_stats(
        now,
        {
            "items": 110,
            "bitstreams": 330,
            "communities": 2,
            "collections": 6,
            "db_size": 1200,
            "assetstore_bytes": 6000,
            "eperson_count": 10,
        },
    )
    latest = mem_store.latest_repository_stats()
    assert latest["items"] == 110 and latest["eperson_count"] == 10 and latest["ts"] == now
    delta = mem_store.repository_delta(now - 2 * 86400, now)
    assert delta["items"] == 10 and delta["collections"] == 1 and delta["assetstore_bytes"] == 1000
    assert delta["days"] == pytest.approx(1.0, abs=0.01)
    assert mem_store.repository_delta(now + 1, now + 2) == {}
    series = mem_store.repository_series(now - 2 * 86400, now)
    assert len(series["items"]) == 2 and series["items"][-1][1] == 110
    assert mem_store.insert_repository_stats_bulk([(now + 1, {"items": 1})]) == 1


def test_events_and_purge(mem_store: Store) -> None:
    now = time.time()
    mem_store.add_event("component", "solr", "Solr changed from ok to crit", {"from": "ok"}, ts=now - 10)
    mem_store.add_event("config", None, "Modules changed", ts=now)
    events = mem_store.events(limit=10)
    assert [e.kind for e in events] == ["config", "component"]
    assert mem_store.events(component="solr")[0].data == {"from": "ok"}
    assert mem_store.events(kind="config")[0].message == "Modules changed"

    old = now - 40 * 86400
    mem_store.insert_sample(sample(old))
    mem_store.insert_sample(sample(now))
    mem_store.insert_checks([CheckResult("x.y", "x", "ok", old), CheckResult("x.y", "x", "ok", now)])
    mem_store.insert_component_history(old, "solr", "ok", 1)
    mem_store.insert_component_history(now, "solr", "ok", 1)
    removed = mem_store.purge(30, now=now)
    assert removed["samples"] == 1 and removed["checks"] == 1 and removed["component_history"] == 1
    assert mem_store.count_samples() == 1
    stats = mem_store.sample_stats(now - 60, now + 60)
    assert stats["n"] == 1 and stats["cpu_avg"] == 10.0


def test_clear_history_and_export(mem_store: Store) -> None:
    now = time.time()
    mem_store.insert_sample(sample(now))
    mem_store.insert_alert(Alert(None, "cpu", "server", "warning", "open", "t", "", 1, 1, now))
    mem_store.insert_repository_stats(now, {"items": 1})
    mem_store.kv_set("demo_backfilled", "1")
    mem_store.add_session(
        token_hash="ab" * 32,
        username="admin",
        fingerprint="f",
        created_ts=now,
        expires_ts=now + 60,
        remember=False,
    )
    export = mem_store.export(now - 3600, now + 1)
    assert "sessions" not in export
    assert len(export["samples"]) == 1 and len(export["alerts"]) == 1 and len(export["repository"]) == 1
    assert export["samples"][0]["extra"]["cores"] == 2
    mem_store.clear_history()
    counts = mem_store.table_counts()
    assert counts["samples"] == 0 and counts["alerts"] == 0 and counts["repository_stats"] == 0
    assert counts["sessions"] == 1  # clearing the history signs nobody out
    assert mem_store.kv_get("demo_backfilled") is None
    assert mem_store.kv_get("schema_version") == str(SCHEMA_VERSION)


def test_bulk_inserts_fast(mem_store: Store) -> None:
    now = int(time.time())
    rows = [sample(now - 7 * 86400 + i * 60) for i in range(7 * 1440)]
    started = time.perf_counter()
    assert mem_store.insert_samples_bulk(rows) == 7 * 1440
    assert time.perf_counter() - started < 3.0
    assert mem_store.insert_check_rows((now - i, "a.b", "ok", 1.0, None, "") for i in range(1000)) == 1000
    assert mem_store.insert_component_history_rows([(now, "solr", "ok", 1.0)]) == 1
