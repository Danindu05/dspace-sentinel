"""SQLite storage layer (SPEC section 5). The only module that touches the database.

``Store`` is synchronous ``sqlite3`` (WAL, one connection, one lock); the async
application calls bulk queries through ``asyncio.to_thread``. Every public method
is safe to call from any thread.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import statistics
import threading
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from dspace_sentinel.auth import IDLE_S, REMEMBER_S
from dspace_sentinel.models import Alert, CheckResult, ComponentHealth, Event, Incident, Report, SystemSample

SCHEMA_VERSION = 1

HOURLY_RETENTION_DAYS = 365
REPORT_PURGE_AFTER_S = 24 * 3600
EVENTS_KEEP = 5000

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples(
    ts INTEGER PRIMARY KEY, cpu REAL, mem_pct REAL, mem_used INTEGER, mem_total INTEGER, swap_pct REAL,
    disk_pct REAL, disk_used INTEGER, disk_total INTEGER, load1 REAL, load5 REAL, load15 REAL,
    net_rx REAL, net_tx REAL, extra TEXT);
CREATE TABLE IF NOT EXISTS samples_hourly(
    ts INTEGER PRIMARY KEY, cpu_avg REAL, cpu_max REAL, mem_avg REAL, mem_max REAL, disk_avg REAL,
    load_avg REAL, net_rx_avg REAL, net_tx_avg REAL);
CREATE TABLE IF NOT EXISTS checks(
    ts INTEGER, check_id TEXT, status TEXT, latency_ms REAL, value REAL, message TEXT,
    PRIMARY KEY(ts, check_id));
CREATE INDEX IF NOT EXISTS checks_id_ts ON checks(check_id, ts);
CREATE TABLE IF NOT EXISTS component_state(
    component_id TEXT PRIMARY KEY, status TEXT, since_ts INTEGER, checked_ts INTEGER, reason TEXT,
    latency_ms REAL, metrics TEXT, chain TEXT);
CREATE TABLE IF NOT EXISTS component_history(
    ts INTEGER, component_id TEXT, status TEXT, latency_ms REAL, PRIMARY KEY(ts, component_id));
CREATE INDEX IF NOT EXISTS component_history_id_ts ON component_history(component_id, ts);
CREATE TABLE IF NOT EXISTS alerts(
    id INTEGER PRIMARY KEY AUTOINCREMENT, rule TEXT, component TEXT, severity TEXT, status TEXT, title TEXT,
    message TEXT, value REAL, threshold REAL, opened_ts INTEGER, resolved_ts INTEGER, acked_ts INTEGER,
    incident_id INTEGER, notified_ts INTEGER);
CREATE INDEX IF NOT EXISTS alerts_status ON alerts(status, opened_ts);
CREATE TABLE IF NOT EXISTS incidents(
    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, hypothesis TEXT, status TEXT, started_ts INTEGER,
    ended_ts INTEGER, affected TEXT, sequence TEXT);
CREATE TABLE IF NOT EXISTS reports(
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_ts INTEGER, title TEXT, range_from INTEGER,
    range_to INTEGER,
    fmt TEXT, path TEXT, size INTEGER, deleted_ts INTEGER);
CREATE TABLE IF NOT EXISTS repository_stats(
    ts INTEGER PRIMARY KEY, items INTEGER, bitstreams INTEGER, communities INTEGER, collections INTEGER,
    db_size INTEGER, assetstore_bytes INTEGER, extra TEXT);
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, kind TEXT, component TEXT, message TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sessions(
    token_hash TEXT PRIMARY KEY, username TEXT, created_ts REAL, last_seen_ts REAL, expires_ts REAL,
    remember INTEGER, ip TEXT, user_agent TEXT, fingerprint TEXT);
"""

METRIC_COLUMNS: dict[str, str] = {
    "cpu": "cpu",
    "mem": "mem_pct",
    "disk": "disk_pct",
    "load": "load1",
    "swap": "swap_pct",
}
"""``series()`` metric → column in ``samples``; ``net`` is handled separately."""

HOURLY_COLUMNS: dict[str, tuple[str, str]] = {
    "cpu": ("cpu_avg", "cpu_max"),
    "mem": ("mem_avg", "mem_max"),
    "disk": ("disk_avg", "disk_avg"),
    "load": ("load_avg", "load_avg"),
}
"""Metrics available in ``samples_hourly`` → (avg column, max column). ``swap`` is not rolled up
(the SPEC schema has no swap column), so long swap ranges are served from raw samples."""

HOURLY_ROW_COLUMNS: tuple[str, ...] = (
    "cpu_avg",
    "cpu_max",
    "mem_avg",
    "mem_max",
    "disk_avg",
    "load_avg",
    "net_rx_avg",
    "net_tx_avg",
)

METRIC_UNITS: dict[str, str] = {"cpu": "%", "mem": "%", "disk": "%", "load": "", "swap": "%", "net": "B/s"}

REPOSITORY_FIELDS: tuple[str, ...] = (
    "items",
    "bitstreams",
    "communities",
    "collections",
    "db_size",
    "assetstore_bytes",
)

SEVEN_DAYS = 7 * 86400


def bucket_for_range(range_s: float) -> int:
    """Return the bucket width (seconds) so a series has ≤ 360 points.

    1h→15 s, 6h→60 s, 24h→300 s, 7d→1800 s, 14d→3600 s, 30d→7200 s (SPEC section 5).
    Longer ranges get a whole number of hours large enough to stay under 360 points.
    """
    if range_s <= 3600:
        return 15
    if range_s <= 6 * 3600:
        return 60
    if range_s <= 86400:
        return 300
    if range_s <= SEVEN_DAYS:
        return 1800
    if range_s <= 14 * 86400:
        return 3600
    if range_s <= 30 * 86400:
        return 7200
    return int(math.ceil(range_s / 360.0 / 3600.0)) * 3600


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


def _loads(text: str | None, default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


class Store:
    """Thread-safe synchronous SQLite store.

    Args:
        path: database file, or ``":memory:"`` for an ephemeral store (tests, ``doctor``).
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path) if str(path) != ":memory:" else None
        self._lock = threading.RLock()
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._make_private()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=10.0, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._init()

    # ------------------------------------------------------------------ setup

    def _make_private(self) -> None:
        """Create the database file as 0600 before SQLite opens it, and tighten what already exists.

        SQLite gives the ``-wal`` and ``-shm`` files the database file's mode, so the file must be
        private before WAL mode creates them (the WAL holds the most recent writes).
        """
        assert self.path is not None
        try:
            os.close(os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600))
        except OSError:
            return
        for suffix in ("", "-wal", "-shm"):
            try:
                os.chmod(f"{self.path}{suffix}", 0o600)
            except OSError:
                pass

    def _init(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            if self.path is not None:
                cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA temp_store=MEMORY")
            cur.execute("PRAGMA busy_timeout=10000")
            cur.executescript(SCHEMA)
            existing = self.kv_get("schema_version")
            if existing is None:
                self.kv_set("schema_version", str(SCHEMA_VERSION))
            elif int(existing) < SCHEMA_VERSION:
                self._migrate(int(existing))
                self.kv_set("schema_version", str(SCHEMA_VERSION))
            if self.path is not None:
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass

    def _migrate(self, from_version: int) -> None:
        """Placeholder for future schema migrations (v1 is the first schema)."""
        return None

    def close(self) -> None:
        """Checkpoint the WAL and close the connection."""
        with self._lock:
            try:
                if self.path is not None:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self._conn.close()

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def _executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.executemany(sql, rows)
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return len(rows)

    def _fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _fetchone(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    # --------------------------------------------------------------------- kv

    def kv_get(self, key: str) -> str | None:
        """Read a value from the ``kv`` table (``None`` when missing)."""
        row = self._fetchone("SELECT value FROM kv WHERE key=?", (key,))
        return None if row is None else row["value"]

    def kv_set(self, key: str, value: str) -> None:
        """Upsert a value in the ``kv`` table."""
        self._execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def kv_delete(self, key: str) -> None:
        """Delete a key from the ``kv`` table (no error when missing)."""
        self._execute("DELETE FROM kv WHERE key=?", (key,))

    # ---------------------------------------------------------------- samples

    @staticmethod
    def _sample_row(sample: SystemSample) -> tuple[Any, ...]:
        extra = {
            "cpu_per_core": sample.cpu_per_core,
            "disks": sample.disks,
            "mem_available": sample.mem_available,
            "mem_cached": sample.mem_cached,
            "swap_total": sample.swap_total,
            "swap_used": sample.swap_used,
            "disk_free": sample.disk_free,
            "disk_path": sample.disk_path,
            "inode_pct": sample.inode_pct,
            "cores": sample.cores,
            "uptime_s": sample.uptime_s,
        }
        return (
            int(sample.ts),
            sample.cpu_pct,
            sample.mem_pct,
            sample.mem_used,
            sample.mem_total,
            sample.swap_pct,
            sample.disk_pct,
            sample.disk_used,
            sample.disk_total,
            sample.load1,
            sample.load5,
            sample.load15,
            sample.net_rx_bps,
            sample.net_tx_bps,
            _json(extra),
        )

    _SAMPLE_SQL = (
        "INSERT OR REPLACE INTO samples(ts, cpu, mem_pct, mem_used, mem_total, swap_pct, disk_pct, "
        "disk_used, "
        "disk_total, load1, load5, load15, net_rx, net_tx, extra) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    )

    def insert_sample(self, sample: SystemSample) -> None:
        """Insert (or replace, same second) one system sample."""
        self._execute(self._SAMPLE_SQL, self._sample_row(sample))

    def insert_samples_bulk(self, samples: Iterable[SystemSample]) -> int:
        """Insert many samples in one transaction (demo backfill). Returns the row count."""
        return self._executemany(self._SAMPLE_SQL, (self._sample_row(s) for s in samples))

    def insert_sample_rows(self, rows: Iterable[Sequence[Any]]) -> int:
        """Insert raw sample rows ``(ts, cpu, mem_pct, mem_used, mem_total, swap_pct, disk_pct, disk_used,
        disk_total, load1, load5, load15, net_rx, net_tx, extra_json)`` in one transaction."""
        return self._executemany(self._SAMPLE_SQL, rows)

    def latest_sample_row(self) -> dict[str, Any] | None:
        """Return the newest ``samples`` row as a dict (``extra`` decoded), or ``None``."""
        row = self._fetchone("SELECT * FROM samples ORDER BY ts DESC LIMIT 1")
        if row is None:
            return None
        out = dict(row)
        out["extra"] = _loads(out.get("extra"), {})
        return out

    def count_samples(self) -> int:
        """Number of raw sample rows."""
        row = self._fetchone("SELECT count(*) AS n FROM samples")
        return int(row["n"]) if row else 0

    def sample_time_range(self) -> tuple[int, int] | None:
        """``(oldest_ts, newest_ts)`` of raw samples, or ``None`` when empty."""
        row = self._fetchone("SELECT min(ts) AS lo, max(ts) AS hi FROM samples")
        if row is None or row["lo"] is None:
            return None
        return int(row["lo"]), int(row["hi"])

    # ----------------------------------------------------------------- checks

    _CHECK_SQL = (
        "INSERT OR REPLACE INTO checks(ts, check_id, status, latency_ms, value, message) VALUES(?,?,?,?,?,?)"
    )

    def insert_checks(self, checks: Iterable[CheckResult]) -> int:
        """Insert rung results (one transaction). Returns the row count."""
        return self._executemany(
            self._CHECK_SQL,
            ((int(c.ts), c.id, c.status, c.latency_ms, c.value, (c.message or "")[:500]) for c in checks),
        )

    def insert_check_rows(self, rows: Iterable[Sequence[Any]]) -> int:
        """Insert raw check rows ``(ts, check_id, status, latency_ms, value, message)``."""
        return self._executemany(self._CHECK_SQL, rows)

    def latest_check(self, check_id: str) -> dict[str, Any] | None:
        """Return the most recent row for ``check_id`` as a dict, or ``None``."""
        row = self._fetchone("SELECT * FROM checks WHERE check_id=? ORDER BY ts DESC LIMIT 1", (check_id,))
        return None if row is None else dict(row)

    def checks_between(
        self, t_from: float, t_to: float, check_id: str | None = None, limit: int = 100000
    ) -> list[dict[str, Any]]:
        """Raw check rows in ``[t_from, t_to]`` (optionally one check id), oldest first."""
        if check_id:
            rows = self._fetchall(
                "SELECT * FROM checks WHERE check_id=? AND ts BETWEEN ? AND ? ORDER BY ts LIMIT ?",
                (check_id, int(t_from), int(t_to), limit),
            )
        else:
            rows = self._fetchall(
                "SELECT * FROM checks WHERE ts BETWEEN ? AND ? ORDER BY ts LIMIT ?",
                (int(t_from), int(t_to), limit),
            )
        return [dict(r) for r in rows]

    def latency_series(
        self, check_id: str, t_from: float, t_to: float, bucket_s: int | None = None
    ) -> dict[str, Any]:
        """Bucketed latency of one check: ``{check, bucket_s, points: [[ts, avg_ms, max_ms]]}``."""
        bucket = bucket_s or bucket_for_range(t_to - t_from)
        rows = self._fetchall(
            "SELECT (ts / ?) * ? AS b, avg(latency_ms) AS a, max(latency_ms) AS m FROM checks "
            "WHERE check_id=? AND ts BETWEEN ? AND ? AND latency_ms IS NOT NULL GROUP BY b ORDER BY b",
            (bucket, bucket, check_id, int(t_from), int(t_to)),
        )
        return {
            "check": check_id,
            "bucket_s": bucket,
            "points": [[int(r["b"]), round(float(r["a"]), 2), round(float(r["m"]), 2)] for r in rows],
        }

    # ------------------------------------------------------------- components

    def upsert_component_state(self, comp: ComponentHealth) -> None:
        """Persist the current state of a component (survives restarts)."""
        self._execute(
            "INSERT INTO component_state(component_id, status, since_ts, checked_ts, reason, latency_ms,"
            " metrics, chain)"
            " VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(component_id) DO UPDATE SET status=excluded.status,"
            " since_ts=excluded.since_ts, checked_ts=excluded.checked_ts, reason=excluded.reason,"
            " latency_ms=excluded.latency_ms, metrics=excluded.metrics, chain=excluded.chain",
            (
                comp.id,
                comp.status,
                None if comp.since is None else int(comp.since),
                None if comp.checked_at is None else int(comp.checked_at),
                comp.reason,
                comp.latency_ms,
                _json(comp.metrics),
                _json([c.to_dict() for c in comp.chain]),
            ),
        )

    def component_state(self, component_id: str) -> dict[str, Any] | None:
        """Return the persisted state row for one component (JSON fields decoded), or ``None``."""
        row = self._fetchone("SELECT * FROM component_state WHERE component_id=?", (component_id,))
        return None if row is None else self._decode_state(row)

    def component_states(self) -> dict[str, dict[str, Any]]:
        """Return every persisted component state keyed by component id."""
        return {
            r["component_id"]: self._decode_state(r) for r in self._fetchall("SELECT * FROM component_state")
        }

    @staticmethod
    def _decode_state(row: sqlite3.Row) -> dict[str, Any]:
        out = dict(row)
        out["metrics"] = _loads(out.get("metrics"), {})
        out["chain"] = _loads(out.get("chain"), [])
        return out

    def delete_component_state(self, component_id: str) -> None:
        """Forget the persisted state of a component (used when its module is switched off)."""
        self._execute("DELETE FROM component_state WHERE component_id=?", (component_id,))

    _HISTORY_SQL = (
        "INSERT OR REPLACE INTO component_history(ts, component_id, status, latency_ms) VALUES(?,?,?,?)"
    )

    def insert_component_history(
        self, ts: float, component_id: str, status: str, latency_ms: float | None
    ) -> None:
        """Append one status point for the availability strips."""
        self._execute(self._HISTORY_SQL, (int(ts), component_id, status, latency_ms))

    def insert_component_history_rows(self, rows: Iterable[Sequence[Any]]) -> int:
        """Bulk insert of ``(ts, component_id, status, latency_ms)`` rows."""
        return self._executemany(self._HISTORY_SQL, rows)

    def component_history(
        self, component_id: str, t_from: float, t_to: float, limit: int = 100000
    ) -> list[dict[str, Any]]:
        """Raw history rows for one component, oldest first."""
        rows = self._fetchall(
            "SELECT ts, status, latency_ms FROM component_history WHERE component_id=? AND ts BETWEEN ? AND ?"
            " ORDER BY ts LIMIT ?",
            (component_id, int(t_from), int(t_to), limit),
        )
        return [dict(r) for r in rows]

    def availability(
        self,
        component_id: str,
        t_from: float,
        t_to: float,
        buckets: int = 48,
        interval_s: float | None = None,
    ) -> dict[str, Any]:
        """Availability strip for one component.

        Returns ``{component_id, bucket_s, uptime_pct, samples, downtime_s, covered_s, outages,
        buckets: [...]}`` where ``buckets`` holds the worst status seen in each bucket.
        ``uptime_pct`` counts ok/warn as up and crit as down; unknown points are
        excluded. Each row stands for the time until the next one, capped at twice the typical
        spacing (a gap in the history is not an outage): ``downtime_s`` sums that over the crit
        rows, ``covered_s`` over all rows, so a range only partly covered by history is not
        charged for the part without data.

        The strip follows the same rule: a bucket without a row of its own takes the status of
        the last check before it while that check still stands, so a component that is checked
        every 15 minutes is not "no data" for the 14 minutes in between, and the last row before
        the range covers its start. A bucket is ``None`` only where there is no data: before the
        first check, or more than twice the usual spacing after the last one (Sentinel was not
        running). ``interval_s`` is how often the component is checked, when the caller knows
        it; it is used when it is longer than the spacing the rows show (or when there are too
        few rows in the range to tell).
        """
        buckets = max(1, int(buckets))
        span = max(1.0, float(t_to - t_from))
        bucket_s = max(1, int(round(span / buckets)))
        rows = self._fetchall(
            "SELECT ts, status FROM component_history WHERE component_id=? AND ts >= ? AND ts < ?"
            " ORDER BY ts",
            (component_id, int(t_from), int(t_to)),
        )
        rank = {"ok": 0, "unknown": 1, "warn": 2, "crit": 3}
        strip: list[str | None] = [None] * buckets
        up = down = 0
        outages = 0
        prev_crit = False
        stamps = [int(r["ts"]) for r in rows]
        gaps = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
        typical = float(statistics.median(gaps)) if gaps else 0.0
        downtime = covered = 0.0
        for i, r in enumerate(rows):
            idx = min(buckets - 1, int((r["ts"] - t_from) / span * buckets))
            status = r["status"]
            cur = strip[idx]
            if cur is None or rank.get(status, 1) > rank.get(cur, 1):
                strip[idx] = status
            step = float(gaps[i]) if i < len(gaps) else max(0.0, min(typical, float(t_to) - stamps[i]))
            step = min(step, 2.0 * typical)
            covered += step
            if status == "crit":
                down += 1
                downtime += step
                if not prev_crit:
                    outages += 1
                prev_crit = True
            elif status in ("ok", "warn"):
                up += 1
                prev_crit = False
        spacing = max(typical, float(interval_s or 0.0))
        if spacing > 0:
            before = self._fetchone(
                "SELECT ts, status FROM component_history WHERE component_id=? AND ts < ?"
                " ORDER BY ts DESC LIMIT 1",
                (component_id, int(t_from)),
            )
            marks = [(int(r["ts"]), str(r["status"])) for r in rows]
            if before is not None:
                marks.insert(0, (int(before["ts"]), str(before["status"])))
            for i, (ts, status) in enumerate(marks):
                following = float(marks[i + 1][0]) if i + 1 < len(marks) else float(t_to)
                end = min(following, ts + 2.0 * spacing, float(t_to))
                first = int((ts - t_from) / span * buckets) + 1 if ts >= t_from else 0
                last = min(buckets - 1, math.ceil((end - t_from) / span * buckets) - 1)
                for idx in range(max(0, first), last + 1):
                    if strip[idx] is None:
                        strip[idx] = status
        total = up + down
        uptime = round(100.0 * up / total, 2) if total else None
        return {
            "component_id": component_id,
            "bucket_s": bucket_s,
            "uptime_pct": uptime,
            "samples": len(rows),
            "downtime_s": int(round(downtime)),
            "covered_s": int(round(covered)),
            "outages": outages,
            "buckets": strip,
        }

    # ----------------------------------------------------------------- series

    def series(self, metric: str, t_from: float, t_to: float, bucket_s: int | None = None) -> dict[str, Any]:
        """Bucketed history of one server metric.

        ``metric`` ∈ cpu|mem|disk|load|swap|net. Returns
        ``{metric, unit, bucket_s, points: [[ts, avg, max]]}``; ``net`` returns
        ``series: {rx: [[ts, avg, max]], tx: [...]}`` instead of ``points``.
        The source is ``samples`` when the range is ≤ 7 days, else ``samples_hourly``
        (``swap`` always comes from raw samples: it has no hourly column). The roll-up lags a
        few hours behind, so the hours after the newest hourly row are aggregated per hour
        from the raw samples and merged in: a long range still reaches the newest sample.
        """
        if metric not in METRIC_UNITS:
            raise ValueError(f"unknown metric {metric!r}")
        span = t_to - t_from
        bucket = bucket_s or bucket_for_range(span)
        use_hourly = span > SEVEN_DAYS and metric != "swap"
        out: dict[str, Any] = {"metric": metric, "unit": METRIC_UNITS[metric], "bucket_s": bucket}
        # (from, to, first hour not rolled up yet): hourly rows before it, raw samples per hour after
        window = (int(t_from), int(t_to), self._rolled_up_until() if use_hourly else 0)
        if metric == "net":
            if use_hourly:
                rows = self._fetchall(
                    "SELECT (h / ?) * ? AS b, avg(rx) AS rx, max(rxm) AS rxm, avg(tx) AS tx,"
                    " max(txm) AS txm FROM ("
                    " SELECT ts AS h, net_rx_avg AS rx, net_rx_avg AS rxm, net_tx_avg AS tx,"
                    " net_tx_avg AS txm FROM samples_hourly WHERE ts BETWEEN ? AND ? AND ts < ?"
                    " UNION ALL"
                    " SELECT (ts / 3600) * 3600 AS h, avg(net_rx) AS rx, avg(net_rx) AS rxm,"
                    " avg(net_tx) AS tx, avg(net_tx) AS txm FROM samples"
                    " WHERE ts BETWEEN ? AND ? AND ts >= ? GROUP BY h"
                    ") GROUP BY b ORDER BY b",
                    (bucket, bucket, *window, *window),
                )
            else:
                rows = self._fetchall(
                    "SELECT (ts / ?) * ? AS b, avg(net_rx) AS rx, max(net_rx) AS rxm, avg(net_tx) AS tx,"
                    " max(net_tx) AS txm FROM samples WHERE ts BETWEEN ? AND ? GROUP BY b ORDER BY b",
                    (bucket, bucket, int(t_from), int(t_to)),
                )
            out["series"] = {
                "rx": [[int(r["b"]), _r(r["rx"]), _r(r["rxm"])] for r in rows],
                "tx": [[int(r["b"]), _r(r["tx"]), _r(r["txm"])] for r in rows],
            }
            return out
        if use_hourly:
            col_avg, col_max = HOURLY_COLUMNS[metric]
            raw = METRIC_COLUMNS[metric]
            raw_max = f"max({raw})" if col_max != col_avg else f"avg({raw})"
            rows = self._fetchall(
                "SELECT (h / ?) * ? AS b, avg(a) AS a, max(m) AS m FROM ("
                f" SELECT ts AS h, {col_avg} AS a, {col_max} AS m FROM samples_hourly"
                " WHERE ts BETWEEN ? AND ? AND ts < ?"
                " UNION ALL"
                f" SELECT (ts / 3600) * 3600 AS h, avg({raw}) AS a, {raw_max} AS m FROM samples"
                " WHERE ts BETWEEN ? AND ? AND ts >= ? GROUP BY h"
                ") GROUP BY b ORDER BY b",
                (bucket, bucket, *window, *window),
            )
        else:
            col = METRIC_COLUMNS[metric]
            rows = self._fetchall(
                f"SELECT (ts / ?) * ? AS b, avg({col}) AS a, max({col}) AS m FROM samples"
                " WHERE ts BETWEEN ? AND ? GROUP BY b ORDER BY b",
                (bucket, bucket, int(t_from), int(t_to)),
            )
        out["points"] = [[int(r["b"]), _r(r["a"]), _r(r["m"])] for r in rows]
        return out

    def _rolled_up_until(self) -> int:
        """Start of the first hour after the newest ``samples_hourly`` row (0 when there is none)."""
        row = self._fetchone("SELECT max(ts) AS h FROM samples_hourly")
        return 0 if row is None or row["h"] is None else int(row["h"]) + 3600

    def sample_stats(self, t_from: float, t_to: float) -> dict[str, Any]:
        """Aggregate avg/max of cpu, mem, disk, load, net over a range (from raw samples)."""
        row = self._fetchone(
            "SELECT count(*) AS n, avg(cpu) AS cpu_avg, max(cpu) AS cpu_max, avg(mem_pct) AS mem_avg,"
            " max(mem_pct) AS mem_max, avg(disk_pct) AS disk_avg, max(disk_pct) AS disk_max,"
            " avg(load1) AS load_avg, max(load1) AS load_max, avg(net_rx) AS rx_avg, avg(net_tx) AS tx_avg,"
            " avg(swap_pct) AS swap_avg, max(swap_pct) AS swap_max FROM samples WHERE ts BETWEEN ? AND ?",
            (int(t_from), int(t_to)),
        )
        return {k: (row[k] if k == "n" else _r(row[k])) for k in row.keys()} if row else {"n": 0}

    # ----------------------------------------------------------------- alerts

    def insert_alert(self, alert: Alert) -> int:
        """Insert an alert; returns its id (also set on ``alert``)."""
        cur = self._execute(
            "INSERT INTO alerts(rule, component, severity, status, title, message, value, threshold,"
            " opened_ts,"
            " resolved_ts, acked_ts, incident_id, notified_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                alert.rule,
                alert.component,
                alert.severity,
                alert.status,
                alert.title,
                alert.message,
                alert.value,
                alert.threshold,
                _ts(alert.opened_ts),
                _ts(alert.resolved_ts),
                _ts(alert.acked_ts),
                alert.incident_id,
                _ts(alert.notified_ts),
            ),
        )
        alert.id = int(cur.lastrowid)
        return alert.id

    _ALERT_FIELDS = frozenset(
        {
            "rule",
            "component",
            "severity",
            "status",
            "title",
            "message",
            "value",
            "threshold",
            "opened_ts",
            "resolved_ts",
            "acked_ts",
            "incident_id",
            "notified_ts",
        }
    )

    def update_alert(self, alert_id: int, **fields: Any) -> Alert | None:
        """Update columns of one alert; returns the updated alert (``None`` when missing)."""
        cols = {k: v for k, v in fields.items() if k in self._ALERT_FIELDS}
        if cols:
            assignments = ", ".join(f"{k}=?" for k in cols)
            self._execute(f"UPDATE alerts SET {assignments} WHERE id=?", (*cols.values(), alert_id))
        return self.get_alert(alert_id)

    def get_alert(self, alert_id: int) -> Alert | None:
        """Fetch one alert by id."""
        row = self._fetchone("SELECT * FROM alerts WHERE id=?", (alert_id,))
        return None if row is None else Alert.from_row(row)

    def alerts(self, status: str = "open", limit: int = 100, since: float | None = None) -> list[Alert]:
        """List alerts by status (``open`` | ``resolved`` | ``all``), newest first."""
        clauses: list[str] = []
        params: list[Any] = []
        if status in ("open", "resolved"):
            clauses.append("status=?")
            params.append(status)
        if since is not None:
            clauses.append("opened_ts >= ?")
            params.append(int(since))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._fetchall(
            f"SELECT * FROM alerts {where} ORDER BY opened_ts DESC, id DESC LIMIT ?", (*params, int(limit))
        )
        return [Alert.from_row(r) for r in rows]

    def open_alerts(self) -> list[Alert]:
        """Every open alert, newest first."""
        return self.alerts("open", limit=1000)

    def alerts_between(self, t_from: float, t_to: float) -> list[Alert]:
        """Alerts opened within ``[t_from, t_to]``, oldest first."""
        rows = self._fetchall(
            "SELECT * FROM alerts WHERE opened_ts BETWEEN ? AND ? ORDER BY opened_ts, id",
            (int(t_from), int(t_to)),
        )
        return [Alert.from_row(r) for r in rows]

    def ack_alert(self, alert_id: int, ts: float | None = None) -> Alert | None:
        """Mark an alert acknowledged."""
        return self.update_alert(alert_id, acked_ts=int(ts or time.time()))

    def unack_alert(self, alert_id: int) -> Alert | None:
        """Remove the acknowledgement of an alert."""
        return self.update_alert(alert_id, acked_ts=None)

    def resolve_alert(self, alert_id: int, ts: float | None = None) -> Alert | None:
        """Mark an alert resolved at ``ts``."""
        return self.update_alert(alert_id, status="resolved", resolved_ts=int(ts or time.time()))

    def alerts_for_incident(self, incident_id: int) -> list[Alert]:
        """Alerts grouped into one incident, oldest first."""
        rows = self._fetchall(
            "SELECT * FROM alerts WHERE incident_id=? ORDER BY opened_ts, id", (incident_id,)
        )
        return [Alert.from_row(r) for r in rows]

    # -------------------------------------------------------------- incidents

    def insert_incident(self, incident: Incident) -> int:
        """Insert an incident; returns its id (also set on ``incident``)."""
        cur = self._execute(
            "INSERT INTO incidents(title, hypothesis, status, started_ts, ended_ts, affected, sequence)"
            " VALUES(?,?,?,?,?,?,?)",
            (
                incident.title,
                incident.hypothesis,
                incident.status,
                int(incident.started_ts),
                _ts(incident.ended_ts),
                _json(incident.affected),
                _json(incident.sequence),
            ),
        )
        incident.id = int(cur.lastrowid)
        for alert_id in incident.alert_ids:
            self._execute("UPDATE alerts SET incident_id=? WHERE id=?", (incident.id, alert_id))
        return incident.id

    def update_incident(self, incident_id: int, **fields: Any) -> Incident | None:
        """Update columns of one incident (``affected``/``sequence`` accept lists)."""
        allowed = {"title", "hypothesis", "status", "started_ts", "ended_ts", "affected", "sequence"}
        cols: dict[str, Any] = {}
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key in ("affected", "sequence"):
                value = _json(value)
            elif key in ("started_ts", "ended_ts"):
                value = _ts(value)
            cols[key] = value
        if cols:
            assignments = ", ".join(f"{k}=?" for k in cols)
            self._execute(f"UPDATE incidents SET {assignments} WHERE id=?", (*cols.values(), incident_id))
        return self.get_incident(incident_id)

    def _incident_from_row(self, row: sqlite3.Row) -> Incident:
        ids = [
            int(r["id"])
            for r in self._fetchall(
                "SELECT id FROM alerts WHERE incident_id=? ORDER BY opened_ts, id", (row["id"],)
            )
        ]
        return Incident(
            id=row["id"],
            title=row["title"] or "",
            hypothesis=row["hypothesis"] or "",
            status=row["status"],
            started_ts=row["started_ts"],
            ended_ts=row["ended_ts"],
            affected=_loads(row["affected"], []),
            sequence=_loads(row["sequence"], []),
            alert_ids=ids,
        )

    def get_incident(self, incident_id: int) -> Incident | None:
        """Fetch one incident (with its alert ids)."""
        row = self._fetchone("SELECT * FROM incidents WHERE id=?", (incident_id,))
        return None if row is None else self._incident_from_row(row)

    def incidents(self, status: str = "all", limit: int = 100, since: float | None = None) -> list[Incident]:
        """List incidents (``open`` | ``resolved`` | ``all``), newest first."""
        clauses: list[str] = []
        params: list[Any] = []
        if status in ("open", "resolved"):
            clauses.append("status=?")
            params.append(status)
        if since is not None:
            clauses.append("started_ts >= ?")
            params.append(int(since))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._fetchall(
            f"SELECT * FROM incidents {where} ORDER BY started_ts DESC, id DESC LIMIT ?",
            (*params, int(limit)),
        )
        return [self._incident_from_row(r) for r in rows]

    def open_incidents(self) -> list[Incident]:
        """Every open incident."""
        return self.incidents("open", limit=1000)

    def attach_alert_to_incident(self, alert_id: int, incident_id: int) -> None:
        """Link an alert to an incident."""
        self._execute("UPDATE alerts SET incident_id=? WHERE id=?", (incident_id, alert_id))

    # ---------------------------------------------------------------- reports

    def insert_report(self, report: Report) -> int:
        """Insert a report row; returns its id (also set on ``report``)."""
        cur = self._execute(
            "INSERT INTO reports(created_ts, title, range_from, range_to, fmt, path, size, deleted_ts)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (
                int(report.created_ts),
                report.title,
                int(report.range_from),
                int(report.range_to),
                report.fmt,
                report.path,
                int(report.size),
                _ts(report.deleted_ts),
            ),
        )
        report.id = int(cur.lastrowid)
        return report.id

    def get_report(self, report_id: int, include_deleted: bool = False) -> Report | None:
        """Fetch one report (soft-deleted ones only when ``include_deleted``)."""
        row = self._fetchone("SELECT * FROM reports WHERE id=?", (report_id,))
        if row is None:
            return None
        rep = Report.from_row(row)
        if rep.deleted_ts is not None and not include_deleted:
            return None
        return rep

    def reports(self, include_deleted: bool = False, limit: int = 200) -> list[Report]:
        """List reports, newest first (soft-deleted excluded unless ``include_deleted``)."""
        where = "" if include_deleted else "WHERE deleted_ts IS NULL"
        rows = self._fetchall(
            f"SELECT * FROM reports {where} ORDER BY created_ts DESC, id DESC LIMIT ?", (limit,)
        )
        return [Report.from_row(r) for r in rows]

    def soft_delete_report(self, report_id: int, ts: float | None = None) -> Report | None:
        """Mark a report deleted (purged for real after 24 h by :meth:`purge`)."""
        self._execute("UPDATE reports SET deleted_ts=? WHERE id=?", (int(ts or time.time()), report_id))
        return self.get_report(report_id, include_deleted=True)

    def restore_report(self, report_id: int) -> Report | None:
        """Undo a soft delete."""
        self._execute("UPDATE reports SET deleted_ts=NULL WHERE id=?", (report_id,))
        return self.get_report(report_id)

    def prune_reports(self, keep: int, max_bytes: int) -> int:
        """Permanently remove the oldest reports (rows + files, soft-deleted ones included) beyond the
        newest ``keep`` or beyond ``max_bytes`` in total; the newest report always stays. Returns count."""
        rows = self._fetchall("SELECT id, path, size FROM reports ORDER BY created_ts DESC, id DESC")
        total = 0
        doomed: list[sqlite3.Row] = []
        for index, r in enumerate(rows):
            total += int(r["size"] or 0)
            if index > 0 and (index >= keep or total > max_bytes):
                doomed.append(r)
        for r in doomed:
            try:
                Path(r["path"]).unlink(missing_ok=True)
            except OSError:
                pass
            self._execute("DELETE FROM reports WHERE id=?", (r["id"],))
        return len(doomed)

    def purge_reports(self, deleted_before: float) -> int:
        """Permanently remove reports soft-deleted before ``deleted_before`` (rows + files). Returns count."""
        rows = self._fetchall(
            "SELECT id, path FROM reports WHERE deleted_ts IS NOT NULL AND deleted_ts < ?",
            (int(deleted_before),),
        )
        for r in rows:
            try:
                Path(r["path"]).unlink(missing_ok=True)
            except OSError:
                pass
            self._execute("DELETE FROM reports WHERE id=?", (r["id"],))
        return len(rows)

    # ------------------------------------------------------------- repository

    _REPO_SQL = (
        "INSERT OR REPLACE INTO repository_stats(ts, items, bitstreams, communities, collections, db_size,"
        " assetstore_bytes, extra) VALUES(?,?,?,?,?,?,?,?)"
    )

    @staticmethod
    def _repo_row(ts: float, stats: dict[str, Any]) -> tuple[Any, ...]:
        extra = {k: v for k, v in stats.items() if k not in REPOSITORY_FIELDS}
        return (
            int(ts),
            stats.get("items"),
            stats.get("bitstreams"),
            stats.get("communities"),
            stats.get("collections"),
            stats.get("db_size"),
            stats.get("assetstore_bytes"),
            _json(extra),
        )

    def insert_repository_stats(self, ts: float, stats: dict[str, Any]) -> None:
        """Store one repository snapshot; unknown keys go into ``extra``."""
        self._execute(self._REPO_SQL, self._repo_row(ts, stats))

    def insert_repository_stats_bulk(self, rows: Iterable[tuple[float, dict[str, Any]]]) -> int:
        """Bulk insert of ``(ts, stats)`` pairs (demo backfill)."""
        return self._executemany(self._REPO_SQL, (self._repo_row(ts, s) for ts, s in rows))

    @staticmethod
    def _decode_repo(row: sqlite3.Row) -> dict[str, Any]:
        out = {k: row[k] for k in REPOSITORY_FIELDS}
        out["ts"] = row["ts"]
        out.update(_loads(row["extra"], {}))
        return out

    def latest_repository_stats(self) -> dict[str, Any] | None:
        """Newest repository snapshot (flattened), or ``None``."""
        row = self._fetchone("SELECT * FROM repository_stats ORDER BY ts DESC LIMIT 1")
        return None if row is None else self._decode_repo(row)

    def repository_delta(self, t_from: float, t_to: float) -> dict[str, Any]:
        """Change of every repository counter between the first and last snapshot in the range.

        Returns ``{items: +n, ..., first_ts, last_ts, days}`` (``{}`` when fewer than one snapshot).
        """
        first = self._fetchone(
            "SELECT * FROM repository_stats WHERE ts BETWEEN ? AND ? ORDER BY ts LIMIT 1",
            (int(t_from), int(t_to)),
        )
        last = self._fetchone(
            "SELECT * FROM repository_stats WHERE ts BETWEEN ? AND ? ORDER BY ts DESC LIMIT 1",
            (int(t_from), int(t_to)),
        )
        if first is None or last is None:
            return {}
        out: dict[str, Any] = {}
        for key in REPOSITORY_FIELDS:
            a, b = first[key], last[key]
            out[key] = (b - a) if (a is not None and b is not None) else None
        out["first_ts"] = first["ts"]
        out["last_ts"] = last["ts"]
        out["days"] = round(max(0.0, (last["ts"] - first["ts"]) / 86400.0), 3)
        return out

    def repository_series(
        self, t_from: float, t_to: float, bucket_s: int = 3600
    ) -> dict[str, list[list[float]]]:
        """Per-counter series ``{items: [[ts, value]], ...}`` bucketed by ``bucket_s`` (last value wins)."""
        rows = self._fetchall(
            "SELECT (ts / ?) * ? AS b, max(ts) AS ts, items, bitstreams, communities, collections, db_size,"
            " assetstore_bytes FROM repository_stats WHERE ts BETWEEN ? AND ? GROUP BY b ORDER BY b",
            (bucket_s, bucket_s, int(t_from), int(t_to)),
        )
        out: dict[str, list[list[float]]] = {k: [] for k in REPOSITORY_FIELDS}
        for r in rows:
            for key in REPOSITORY_FIELDS:
                if r[key] is not None:
                    out[key].append([int(r["b"]), r[key]])
        return out

    # ----------------------------------------------------------------- events

    def add_event(
        self,
        kind: str,
        component: str | None,
        message: str,
        data: dict[str, Any] | None = None,
        ts: float | None = None,
    ) -> int:
        """Append an audit event; returns its id."""
        cur = self._execute(
            "INSERT INTO events(ts, kind, component, message, data) VALUES(?,?,?,?,?)",
            (int(ts or time.time()), kind, component, message[:1000], _json(data or {})),
        )
        return int(cur.lastrowid)

    def events(
        self,
        limit: int = 100,
        component: str | None = None,
        since: float | None = None,
        kind: str | None = None,
    ) -> list[Event]:
        """Recent events, newest first."""
        clauses: list[str] = []
        params: list[Any] = []
        if component:
            clauses.append("component=?")
            params.append(component)
        if since is not None:
            clauses.append("ts >= ?")
            params.append(int(since))
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._fetchall(
            f"SELECT * FROM events {where} ORDER BY ts DESC, id DESC LIMIT ?", (*params, int(limit))
        )
        return [
            Event(
                id=r["id"],
                ts=r["ts"],
                kind=r["kind"],
                component=r["component"],
                message=r["message"],
                data=_loads(r["data"], {}),
            )
            for r in rows
        ]

    # --------------------------------------------------------------- sessions

    def add_session(
        self,
        *,
        token_hash: str,
        username: str,
        fingerprint: str,
        created_ts: float,
        expires_ts: float,
        remember: bool,
        ip: str = "",
        user_agent: str = "",
    ) -> None:
        """Keep a new sign-in session: only the SHA-256 of its token, never the token itself."""
        self._execute(
            "INSERT OR REPLACE INTO sessions(token_hash, username, created_ts, last_seen_ts, expires_ts,"
            " remember, ip, user_agent, fingerprint) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                token_hash,
                username,
                float(created_ts),
                float(created_ts),
                float(expires_ts),
                1 if remember else 0,
                ip,
                user_agent,
                fingerprint,
            ),
        )

    def get_session(self, token_hash: str) -> dict[str, Any] | None:
        """The ``sessions`` row of ``token_hash`` as a dict, or ``None``."""
        row = self._fetchone("SELECT * FROM sessions WHERE token_hash=?", (token_hash,))
        return None if row is None else dict(row)

    def sessions(self) -> list[dict[str, Any]]:
        """Every session row, oldest sign-in first."""
        return [dict(r) for r in self._fetchall("SELECT * FROM sessions ORDER BY created_ts, token_hash")]

    def touch_session(self, token_hash: str, ts: float) -> None:
        """Record that the session was used at ``ts``."""
        self._execute("UPDATE sessions SET last_seen_ts=? WHERE token_hash=?", (float(ts), token_hash))

    def set_session_fingerprint(self, token_hash: str, fingerprint: str) -> None:
        """Move a session to a new login (the caller's own session after a password change)."""
        self._execute("UPDATE sessions SET fingerprint=? WHERE token_hash=?", (fingerprint, token_hash))

    def delete_session(self, token_hash: str) -> dict[str, Any] | None:
        """Delete one session; the row it had, or ``None`` when there was none."""
        with self._lock:
            row = self._conn.execute("SELECT * FROM sessions WHERE token_hash=?", (token_hash,)).fetchone()
            if row is None:
                return None
            self._conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
            return dict(row)

    def delete_sessions(self, keep: str | None = None) -> int:
        """Delete every session (but ``keep``, a token hash); the number deleted."""
        if keep is None:
            return int(self._execute("DELETE FROM sessions").rowcount)
        return int(self._execute("DELETE FROM sessions WHERE token_hash<>?", (keep,)).rowcount)

    def purge_sessions(
        self, now: float | None = None, idle_s: float = IDLE_S, remember_idle_s: float = REMEMBER_S
    ) -> int:
        """Delete sessions past their lifetime or idle limit; the number deleted."""
        now = time.time() if now is None else now
        return int(
            self._execute(
                "DELETE FROM sessions WHERE expires_ts <= ?"
                " OR last_seen_ts + (CASE WHEN remember THEN ? ELSE ? END) <= ?",
                (float(now), float(remember_idle_s), float(idle_s), float(now)),
            ).rowcount
        )

    # -------------------------------------------------------- rollup / purge

    def rollup_hourly(self, before_ts: float) -> int:
        """Aggregate raw samples of every full hour before ``before_ts`` into ``samples_hourly``.

        Idempotent and late-insert safe: every hour that still has raw samples is
        re-aggregated, but only hours whose aggregate is new or changed are written, so a
        second call returns 0 and a sample inserted later into an old hour updates just that
        hour. Raw samples live ``retention_days`` (30 by default), so the scan stays small.
        Records the cutoff in ``kv.last_rollup_ts``. Returns the number of hourly rows written.
        """
        cutoff = int(before_ts) // 3600 * 3600
        rows = self._fetchall(
            "SELECT (ts / 3600) * 3600 AS h, avg(cpu) AS cpu_avg, max(cpu) AS cpu_max,"
            " avg(mem_pct) AS mem_avg, max(mem_pct) AS mem_max, avg(disk_pct) AS disk_avg,"
            " avg(load1) AS load_avg, avg(net_rx) AS net_rx_avg, avg(net_tx) AS net_tx_avg"
            " FROM samples WHERE ts < ? GROUP BY h ORDER BY h",
            (cutoff,),
        )
        if not rows:
            self.kv_set("last_rollup_ts", str(cutoff))
            return 0
        existing = {
            int(r["ts"]): r
            for r in self._fetchall(
                "SELECT * FROM samples_hourly WHERE ts BETWEEN ? AND ?",
                (int(rows[0]["h"]), int(rows[-1]["h"])),
            )
        }
        to_write: list[tuple[Any, ...]] = []
        for r in rows:
            values = tuple(_r6(r[col]) for col in HOURLY_ROW_COLUMNS)
            old = existing.get(int(r["h"]))
            if old is not None and tuple(_r6(old[col]) for col in HOURLY_ROW_COLUMNS) == values:
                continue
            to_write.append((int(r["h"]), *values))
        written = self._executemany(
            "INSERT OR REPLACE INTO samples_hourly(ts, cpu_avg, cpu_max, mem_avg, mem_max, disk_avg,"
            " load_avg, net_rx_avg, net_tx_avg) VALUES(?,?,?,?,?,?,?,?,?)",
            to_write,
        )
        self.kv_set("last_rollup_ts", str(cutoff))
        return written

    def purge(self, retention_days: int, now: float | None = None) -> dict[str, int]:
        """Delete raw data older than ``retention_days`` (hourly rollups kept 365 d), trim events,
        purge reports soft-deleted more than 24 h ago and expired sign-in sessions. Returns
        per-table deleted counts.

        Raw samples go by whole hours (the cutoff is rounded down to the hour): an hour that
        kept only part of its samples would be re-aggregated by :meth:`rollup_hourly` from that
        part, overwriting its complete roll-up and losing its peaks.
        """
        now = now or time.time()
        cutoff = int(now - retention_days * 86400)
        samples_cutoff = cutoff // 3600 * 3600
        hourly_cutoff = int(now - HOURLY_RETENTION_DAYS * 86400)
        out: dict[str, int] = {}
        with self._lock:
            out["samples"] = self._conn.execute(
                "DELETE FROM samples WHERE ts < ?", (samples_cutoff,)
            ).rowcount
            out["checks"] = self._conn.execute("DELETE FROM checks WHERE ts < ?", (cutoff,)).rowcount
            out["component_history"] = self._conn.execute(
                "DELETE FROM component_history WHERE ts < ?", (cutoff,)
            ).rowcount
            out["repository_stats"] = self._conn.execute(
                "DELETE FROM repository_stats WHERE ts < ?", (hourly_cutoff,)
            ).rowcount
            out["samples_hourly"] = self._conn.execute(
                "DELETE FROM samples_hourly WHERE ts < ?", (hourly_cutoff,)
            ).rowcount
            out["alerts"] = self._conn.execute(
                "DELETE FROM alerts WHERE status='resolved' AND resolved_ts < ?", (hourly_cutoff,)
            ).rowcount
            out["incidents"] = self._conn.execute(
                "DELETE FROM incidents WHERE status='resolved' AND ended_ts < ?", (hourly_cutoff,)
            ).rowcount
            out["events"] = self._conn.execute(
                "DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT ?)",
                (EVENTS_KEEP,),
            ).rowcount
        out["reports"] = self.purge_reports(now - REPORT_PURGE_AFTER_S)
        out["sessions"] = self.purge_sessions(now)
        return out

    def clear_history(self) -> None:
        """Wipe samples, rollups, checks, component history/state, alerts, incidents, events and
        repository stats. Reports, kv, sign-in sessions and the config are kept."""
        with self._lock:
            for table in (
                "samples",
                "samples_hourly",
                "checks",
                "component_history",
                "component_state",
                "alerts",
                "incidents",
                "events",
                "repository_stats",
            ):
                self._conn.execute(f"DELETE FROM {table}")
            self._conn.execute("DELETE FROM kv WHERE key IN ('last_rollup_ts', 'demo_backfilled')")
        self.vacuum()

    def vacuum(self) -> None:
        """Reclaim space (best effort)."""
        try:
            self._execute("VACUUM")
        except sqlite3.Error:
            pass

    def db_size_bytes(self) -> int:
        """Size of the database file (plus WAL) in bytes; page-based estimate for in-memory stores."""
        if self.path is None:
            row = self._fetchone(
                "SELECT page_count * page_size AS b FROM pragma_page_count(), pragma_page_size()"
            )
            return int(row["b"]) if row else 0
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += os.path.getsize(f"{self.path}{suffix}")
            except OSError:
                pass
        return total

    def table_counts(self) -> dict[str, int]:
        """Row counts of every table (diagnostics, tests)."""
        out: dict[str, int] = {}
        for table in (
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
        ):
            row = self._fetchone(f"SELECT count(*) AS n FROM {table}")
            out[table] = int(row["n"]) if row else 0
        return out

    # ----------------------------------------------------------------- export

    def export(
        self,
        t_from: float,
        t_to: float,
        *,
        stream: bool = False,
        meta: dict[str, Any] | None = None,
        batch: int = 2000,
    ) -> dict[str, Any] | Iterable[bytes]:
        """Every sample, check, component history row, alert, incident, repository stat and event
        within ``[t_from, t_to]``, oldest first (used by ``GET /api/export``); nothing is truncated.

        By default one JSON-ready dict (``meta`` first when given). ``stream=True`` returns an
        iterator of UTF-8 ``bytes`` that together form the same JSON object, read table by table in
        batches of ``batch`` rows through a separate read-only connection: memory stays flat
        whatever the range and (WAL) the collectors are never blocked. An in-memory store is read
        through its own connection instead.
        """
        lo, hi = int(t_from), int(t_to)
        big: tuple[tuple[str, str, Any], ...] = (
            (
                "samples",
                "SELECT * FROM samples WHERE ts BETWEEN ? AND ? ORDER BY ts",
                self._decode_sample_row,
            ),
            ("checks", "SELECT * FROM checks WHERE ts BETWEEN ? AND ? ORDER BY ts, check_id", dict),
            (
                "component_history",
                "SELECT * FROM component_history WHERE ts BETWEEN ? AND ? ORDER BY ts, component_id",
                dict,
            ),
        )

        def small() -> list[tuple[str, list[dict[str, Any]]]]:
            repo = self._fetchall(
                "SELECT * FROM repository_stats WHERE ts BETWEEN ? AND ? ORDER BY ts", (lo, hi)
            )
            return [
                ("alerts", [a.to_dict() for a in self.alerts_between(t_from, t_to)]),
                ("incidents", [i.to_dict() for i in self.incidents("all", limit=10000, since=t_from)]),
                ("repository", [self._decode_repo(r) for r in repo]),
                ("events", [e.to_dict() for e in self.events(limit=5000, since=t_from)]),
            ]

        head: dict[str, Any] = {} if meta is None else {"meta": meta}
        head["range"] = {"from": lo, "to": hi}
        if not stream:
            data = dict(head)
            for name, sql, decode in big:
                data[name] = [decode(r) for r in self._fetchall(sql, (lo, hi))]
            data.update(small())
            return data

        def dump(value: Any) -> str:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)

        def chunks() -> Iterable[bytes]:
            yield dump(head)[:-1].encode()  # the object stays open for the tables
            conn: sqlite3.Connection | None = None
            if self.path is not None:
                uri = self.path.resolve().as_uri() + "?mode=ro"
                conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=10.0)
                conn.row_factory = sqlite3.Row
            try:
                for name, sql, decode in big:
                    yield f',"{name}":['.encode()
                    if conn is not None:
                        cursor = conn.execute(sql, (lo, hi))
                        batches: Iterable[list[Any]] = iter(lambda c=cursor: c.fetchmany(batch), [])
                    else:
                        rows = self._fetchall(sql, (lo, hi))
                        batches = (rows[i : i + batch] for i in range(0, len(rows), batch))
                    separator = ""
                    for rows_batch in batches:
                        yield (separator + ",".join(dump(decode(r)) for r in rows_batch)).encode()
                        separator = ","
                    yield b"]"
                for name, rows_small in small():
                    yield f',"{name}":{dump(rows_small)}'.encode()
                yield b"}"
            finally:
                if conn is not None:
                    conn.close()

        return chunks()

    @staticmethod
    def _decode_sample_row(row: sqlite3.Row) -> dict[str, Any]:
        out = dict(row)
        out["extra"] = _loads(out.get("extra"), {})
        return out


def _r(value: Any) -> float | None:
    return None if value is None else round(float(value), 3)


def _r6(value: Any) -> float | None:
    return None if value is None else round(float(value), 6)


def _ts(value: float | None) -> int | None:
    return None if value is None else int(value)


__all__ = [
    "HOURLY_COLUMNS",
    "HOURLY_RETENTION_DAYS",
    "HOURLY_ROW_COLUMNS",
    "METRIC_UNITS",
    "REPORT_PURGE_AFTER_S",
    "REPOSITORY_FIELDS",
    "SCHEMA_VERSION",
    "Store",
    "bucket_for_range",
]
