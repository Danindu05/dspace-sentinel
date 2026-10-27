"""PostgreSQL collector: systemd → TCP → connect → ``SELECT 1``, plus session metrics (SPEC section 6).

Every session is read-only and short-lived: ``connect_timeout=5``,
``options="-c statement_timeout=3000 -c default_transaction_read_only=on"``,
``application_name='dspace-sentinel'``, autocommit (no idle transactions) and closed at
the end of every cycle — never pooled. Nothing here writes to the database.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, ClassVar

import psycopg

from dspace_sentinel.collectors.base import Collector, CollectorContext, CollectorResult
from dspace_sentinel.collectors.services import Ladder, rung, systemd_rung, tcp_rung
from dspace_sentinel.config import Config, PostgresConfig
from dspace_sentinel.health import threshold_status
from dspace_sentinel.models import CheckResult

log = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 5.0
QUERY_TIMEOUT_S = 5.0
STATEMENT_TIMEOUT_MS = 3000
APPLICATION_NAME = "dspace-sentinel"
SESSION_OPTIONS = f"-c statement_timeout={STATEMENT_TIMEOUT_MS} -c default_transaction_read_only=on"

AsyncConnection = psycopg.AsyncConnection
"""The connection class used by :func:`open_connection` (module attribute so tests can swap it)."""

METRIC_QUERIES: tuple[tuple[str, str], ...] = (
    ("connections", "SELECT count(*) FROM pg_stat_activity"),
    ("max_connections", "SHOW max_connections"),
    ("db_size_bytes", "SELECT pg_database_size(current_database())"),
    ("version", "SELECT version()"),
    (
        "active_queries",
        "SELECT count(*) FROM pg_stat_activity WHERE state = 'active' AND pid <> pg_backend_pid()",
    ),
    (
        "longest_query_s",
        "SELECT coalesce(max(extract(epoch FROM (now() - query_start))), 0) FROM pg_stat_activity"
        " WHERE state = 'active' AND pid <> pg_backend_pid()",
    ),
)
"""Metric key → the read-only query that produces it (one row, one column)."""


def connection_kwargs(pg: PostgresConfig) -> dict[str, Any]:
    """Keyword arguments for ``AsyncConnection.connect`` implementing the SPEC session rules."""
    kwargs: dict[str, Any] = {
        "host": pg.host,
        "port": pg.port,
        "dbname": pg.database,
        "user": pg.user,
        "sslmode": pg.sslmode or "prefer",
        "connect_timeout": int(CONNECT_TIMEOUT_S),
        "application_name": APPLICATION_NAME,
        "options": SESSION_OPTIONS,
        "autocommit": True,
    }
    if pg.password:
        kwargs["password"] = pg.password
    return kwargs


async def open_connection(pg: PostgresConfig, timeout: float | None = None) -> Any:
    """Open a read-only, short-timeout session. Raises ``psycopg.Error`` / ``asyncio.TimeoutError``.

    ``timeout`` defaults to :data:`CONNECT_TIMEOUT_S` (resolved at call time).
    """
    timeout = CONNECT_TIMEOUT_S if timeout is None else timeout
    return await asyncio.wait_for(AsyncConnection.connect(**connection_kwargs(pg)), timeout=timeout)


async def close_quietly(conn: Any) -> None:
    """Close a connection, swallowing every error (the cycle is over either way)."""
    if conn is None:
        return
    try:
        await asyncio.wait_for(conn.close(), timeout=2.0)
    except Exception:
        pass


async def fetch_one(
    conn: Any, sql: str, params: Any = None, timeout: float = QUERY_TIMEOUT_S
) -> tuple[Any, ...] | None:
    """Run one query under ``timeout`` and return its first row (``None`` when empty)."""
    cursor = await asyncio.wait_for(conn.execute(sql, params), timeout=timeout)
    row = await asyncio.wait_for(cursor.fetchone(), timeout=timeout)
    return None if row is None else tuple(row)


def describe_pg_error(exc: BaseException, pg: PostgresConfig, timeout: float | None = None) -> str:
    """Short human text for a psycopg failure, e.g. ``"connection refused (localhost:5432)"``."""
    timeout = CONNECT_TIMEOUT_S if timeout is None else timeout
    where = f"{pg.host}:{pg.port}"
    if isinstance(exc, asyncio.TimeoutError):
        return f"timed out after {timeout:g} s ({where})"
    if isinstance(exc, psycopg.errors.QueryCanceled):
        return f"statement timed out after {STATEMENT_TIMEOUT_MS / 1000:g} s"
    text = str(exc).strip()
    low = text.lower()
    if "refused" in low:
        return f"connection refused ({where})"
    if "timeout expired" in low or "timed out" in low:
        return f"timed out after {timeout:g} s ({where})"
    if "password authentication failed" in low:
        return f"password authentication failed for user {pg.user}"
    if "no password supplied" in low or "password required" in low:
        return f"password required for user {pg.user}"
    if "pg_hba.conf" in low:
        return f"rejected by pg_hba.conf ({pg.user}@{where}/{pg.database})"
    if "does not exist" in low and "database" in low:
        return f'database "{pg.database}" does not exist'
    if "does not exist" in low and "role" in low:
        return f'role "{pg.user}" does not exist'
    if "could not translate host name" in low or "name or service not known" in low or "getaddrinfo" in low:
        return f"DNS lookup failed ({pg.host})"
    if "permission denied" in low:
        return f"permission denied for user {pg.user}"
    if "read-only" in low or "read only" in low:
        return "write attempted in a read-only session (refused)"
    first = text.splitlines()[0] if text else exc.__class__.__name__
    return first[:160]


def short_version(raw: str | None) -> str | None:
    """``"PostgreSQL 15.8 (Ubuntu ...) on x86_64..."`` → ``"PostgreSQL 15.8 (Ubuntu ...)"``."""
    if not raw:
        return None
    return raw.split(" on ", 1)[0].strip()


def _coerce(key: str, value: Any) -> Any:
    if value is None:
        return None
    if key == "version":
        return short_version(str(value))
    if key == "longest_query_s":
        return round(float(value), 2)
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


async def read_metrics(conn: Any, pg: PostgresConfig) -> dict[str, Any]:
    """Run :data:`METRIC_QUERIES`; a failing query leaves ``None`` (and a note) instead of raising."""
    metrics: dict[str, Any] = {}
    problems: list[str] = []
    for key, sql in METRIC_QUERIES:
        try:
            row = await fetch_one(conn, sql)
            metrics[key] = _coerce(key, row[0]) if row else None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            metrics[key] = None
            problems.append(f"{key}: {describe_pg_error(exc, pg, QUERY_TIMEOUT_S)}")
    conns, max_conns = metrics.get("connections"), metrics.get("max_connections")
    if isinstance(conns, int) and isinstance(max_conns, int) and max_conns > 0:
        metrics["connections_pct"] = round(100.0 * conns / max_conns, 1)
    else:
        metrics["connections_pct"] = None
    if problems:
        metrics["problems"] = problems
    return metrics


CONNECTIONS_CHECK_ID = "postgres.connections"
"""History-only ``checks`` row: connection usage in percent of ``max_connections`` (report p95)."""


def connections_row(ts: float, metrics: dict[str, Any], cfg: Config) -> list[CheckResult]:
    """The ``postgres.connections`` history row for ``metrics`` (``[]`` without a percentage)."""
    pct = metrics.get("connections_pct")
    if pct is None:
        return []
    th = cfg.alerts.postgres_connections_pct
    conns, max_conns = metrics.get("connections"), metrics.get("max_connections")
    return [
        CheckResult(
            CONNECTIONS_CHECK_ID,
            "Connections",
            threshold_status(float(pct), th.warning, th.critical),
            ts,
            value=float(pct),
            message=f"{conns} / {max_conns}" if conns is not None and max_conns else "",
        )
    ]


class PostgresCollector(Collector):
    """``postgres`` 🐘: service → TCP → connect → SELECT 1; connections, size, version, activity."""

    id: ClassVar[str] = "postgres"
    interval_key: ClassVar[str] = "db_interval"
    label: ClassVar[str] = "PostgreSQL"
    emoji: ClassVar[str] = "🐘"
    budget_s: ClassVar[float] = 35.0  # systemd 3 + TCP 2 + connect 5 + SELECT 1 + six 3 s metric queries

    async def collect(self, ctx: CollectorContext) -> CollectorResult:
        """Climb the ladder; the connection is closed before returning, whatever happened."""
        pg = ctx.cfg.postgres
        ts = ctx.now
        ladder = Ladder(ts)
        tcp = ("postgres.tcp", f"TCP :{pg.port}")
        connect = ("postgres.connect", f"Connect as {pg.user}")
        query = ("postgres.query", "SELECT 1")

        ladder.add(await systemd_rung("postgres.service", pg.service, ts))
        if ladder.blocked:
            ladder.skip_all([tcp, connect, query])
            return CollectorResult(checks=ladder.checks)

        ladder.add(await tcp_rung(tcp[0], pg.host, pg.port, ts, label=tcp[1]))
        if ladder.blocked:
            ladder.skip_all([connect, query])
            return CollectorResult(checks=ladder.checks)

        conn: Any = None
        metrics: dict[str, Any] = {}
        started = time.perf_counter()
        try:
            conn = await open_connection(pg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            ladder.add(
                rung(
                    connect[0],
                    connect[1],
                    "crit",
                    ts,
                    message=describe_pg_error(exc, pg),
                    target=f"{pg.user}@{pg.host}:{pg.port}/{pg.database}",
                )
            )
            ladder.skip(*query)
            return CollectorResult(checks=ladder.checks)
        try:
            ladder.add(
                rung(
                    connect[0],
                    connect[1],
                    "ok",
                    ts,
                    latency_ms=round((time.perf_counter() - started) * 1000.0, 2),
                    message=f"{pg.user}@{pg.host}:{pg.port}/{pg.database}",
                    sslmode=pg.sslmode,
                )
            )
            started = time.perf_counter()
            try:
                row = await fetch_one(conn, "SELECT 1")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                ladder.add(
                    rung(query[0], query[1], "crit", ts, message=describe_pg_error(exc, pg, QUERY_TIMEOUT_S))
                )
                return CollectorResult(checks=ladder.checks)
            latency = round((time.perf_counter() - started) * 1000.0, 2)
            if not row or row[0] != 1:
                ladder.add(
                    rung(
                        query[0],
                        query[1],
                        "crit",
                        ts,
                        latency_ms=latency,
                        message=f"unexpected result {row!r}",
                    )
                )
                return CollectorResult(checks=ladder.checks)
            ladder.add(rung(query[0], query[1], "ok", ts, latency_ms=latency, value=1.0))
            metrics = await read_metrics(conn, pg)
        finally:
            await close_quietly(conn)
        history = connections_row(ts, metrics, ctx.cfg)
        return CollectorResult(checks=ladder.checks, metrics=metrics, history=history)


__all__ = [
    "APPLICATION_NAME",
    "CONNECTIONS_CHECK_ID",
    "CONNECT_TIMEOUT_S",
    "METRIC_QUERIES",
    "QUERY_TIMEOUT_S",
    "SESSION_OPTIONS",
    "STATEMENT_TIMEOUT_MS",
    "PostgresCollector",
    "close_quietly",
    "connection_kwargs",
    "connections_row",
    "describe_pg_error",
    "fetch_one",
    "open_connection",
    "read_metrics",
    "short_version",
]
