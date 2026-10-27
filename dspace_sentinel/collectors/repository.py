"""Repository statistics collector: items, bitstreams, communities, collections, sizes (SPEC section 6).

Metrics only, every ``repository_interval``. The database is asked first (read-only session,
``statement_timeout=3000``); a count that times out falls back to the planner's
``pg_class.reltuples`` estimate and the snapshot is marked ``estimated: true``. When the
database cannot be used at all, the anonymous REST discover endpoint supplies item,
community and collection totals instead (``source: "rest"``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, ClassVar

import httpx
import psycopg

from dspace_sentinel.collectors import postgres as pg_mod
from dspace_sentinel.collectors.base import Collector, CollectorContext, CollectorResult
from dspace_sentinel.collectors.services import http_message, rung
from dspace_sentinel.config import PostgresConfig
from dspace_sentinel.util.httpc import probe

log = logging.getLogger(__name__)

COUNT_QUERIES: tuple[tuple[str, str, str], ...] = (
    ("items", "item", "SELECT count(*) FROM item WHERE in_archive AND NOT withdrawn"),
    (
        "bitstreams",
        "bitstream",
        "SELECT count(*), coalesce(sum(size_bytes), 0) FROM bitstream WHERE NOT deleted",
    ),
    ("communities", "community", "SELECT count(*) FROM community"),
    ("collections", "collection", "SELECT count(*) FROM collection"),
    ("eperson_count", "eperson", "SELECT count(*) FROM eperson"),
    ("workflow_items", "cwf_workflowitem", "SELECT count(*) FROM cwf_workflowitem"),
)
"""``(metric key, table, exact query)``; the bitstream query also returns the byte total."""

ESTIMATE_SQL = (
    "SELECT relname, greatest(reltuples, 0)::bigint FROM pg_class"
    " WHERE relkind IN ('r', 'p') AND relname = ANY(%s)"
)
DB_SIZE_SQL = "SELECT pg_database_size(current_database())"

REST_COUNTS: tuple[tuple[str, str], ...] = (
    ("items", "ITEM"),
    ("communities", "COMMUNITY"),
    ("collections", "COLLECTION"),
)

STAT_KEYS: tuple[str, ...] = (
    "items",
    "bitstreams",
    "communities",
    "collections",
    "db_size",
    "assetstore_bytes",
    "eperson_count",
    "workflow_items",
)


def empty_stats() -> dict[str, Any]:
    """A snapshot with every counter unset."""
    return dict.fromkeys(STAT_KEYS)


async def collect_from_db(pg: PostgresConfig) -> dict[str, Any]:
    """Count through the database; raises when the connection cannot be opened.

    Individual counts that time out use ``reltuples`` estimates; a missing table
    (``cwf_workflowitem`` on unusual schemas) leaves that counter ``None``.
    """
    stats = empty_stats()
    estimated: list[str] = []
    problems: list[str] = []
    started = time.perf_counter()
    conn = await pg_mod.open_connection(pg)
    try:
        for key, table, sql in COUNT_QUERIES:
            try:
                row = await pg_mod.fetch_one(conn, sql)
            except asyncio.CancelledError:
                raise
            except (psycopg.errors.QueryCanceled, asyncio.TimeoutError):
                estimated.append(table)
                continue
            except psycopg.errors.UndefinedTable:
                stats[key] = None
                continue
            except psycopg.Error as exc:
                problems.append(f"{key}: {pg_mod.describe_pg_error(exc, pg)}")
                continue
            if not row:
                continue
            stats[key] = int(row[0]) if row[0] is not None else None
            if key == "bitstreams" and len(row) > 1 and row[1] is not None:
                stats["assetstore_bytes"] = int(row[1])
        if estimated:
            try:
                cursor = await asyncio.wait_for(
                    conn.execute(ESTIMATE_SQL, (estimated,)), timeout=pg_mod.QUERY_TIMEOUT_S
                )
                rows = await asyncio.wait_for(cursor.fetchall(), timeout=pg_mod.QUERY_TIMEOUT_S)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                rows = []
                problems.append(f"estimates: {pg_mod.describe_pg_error(exc, pg)}")
            by_table = {str(r[0]): int(r[1]) for r in rows or () if r and r[1] is not None}
            for key, table, _sql in COUNT_QUERIES:
                if table in estimated:
                    stats[key] = by_table.get(table)
        try:
            row = await pg_mod.fetch_one(conn, DB_SIZE_SQL)
            stats["db_size"] = int(row[0]) if row and row[0] is not None else None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            problems.append(f"db_size: {pg_mod.describe_pg_error(exc, pg)}")
    finally:
        await pg_mod.close_quietly(conn)
    stats["estimated"] = bool(estimated)
    stats["estimated_tables"] = estimated
    stats["source"] = "db"
    stats["latency_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
    if problems:
        stats["problems"] = problems
    return stats


async def collect_from_rest(http: httpx.AsyncClient, server_url: str) -> dict[str, Any]:
    """Item / community / collection totals from the anonymous discover endpoint; raises on failure."""
    base = server_url.rstrip("/")
    stats = empty_stats()
    started = time.perf_counter()
    for key, dso in REST_COUNTS:
        p = await probe(
            http,
            f"{base}/api/discover/search/objects",
            params={"dsoType": dso, "size": "1"},
            expect_json=True,
        )
        if not p.ok:
            raise RuntimeError(f"{http_message(p)} (discover/search/objects)")
        try:
            total = p.json["_embedded"]["searchResult"]["page"]["totalElements"]
        except (KeyError, TypeError):
            raise RuntimeError("unexpected discover response (no searchResult.page)") from None
        stats[key] = int(total)
    stats["estimated"] = False
    stats["source"] = "rest"
    stats["latency_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
    return stats


class RepositoryCollector(Collector):
    """``repository`` 📚: metrics only (DB first, REST fallback); persisted into ``repository_stats``."""

    id: ClassVar[str] = "repository"
    interval_key: ClassVar[str] = "repository_interval"
    label: ClassVar[str] = "Repository"
    emoji: ClassVar[str] = "📚"

    async def collect(self, ctx: CollectorContext) -> CollectorResult:
        """Counts as ``metrics`` + ``repository_stats``; a single ``unknown`` rung when no source works."""
        ts = ctx.now
        pg = ctx.cfg.postgres
        db_error: str | None = None
        stats: dict[str, Any] | None = None
        try:
            stats = await collect_from_db(pg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            db_error = pg_mod.describe_pg_error(exc, pg)
            log.debug("repository counts via database failed: %s", db_error)
        if stats is None:
            try:
                stats = await collect_from_rest(ctx.http, ctx.cfg.dspace.server_url)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                rest_error = str(exc) or exc.__class__.__name__
                check = rung(
                    "repository.stats",
                    "Repository counts",
                    "unknown",
                    ts,
                    message=f"database: {db_error}; REST: {rest_error}",
                    db_error=db_error,
                    rest_error=rest_error,
                )
                return CollectorResult(checks=[check], metrics={})
            stats["db_error"] = db_error
        return CollectorResult(checks=[], metrics=dict(stats), repository_stats=dict(stats))


__all__ = [
    "COUNT_QUERIES",
    "DB_SIZE_SQL",
    "ESTIMATE_SQL",
    "REST_COUNTS",
    "STAT_KEYS",
    "RepositoryCollector",
    "collect_from_db",
    "collect_from_rest",
    "empty_stats",
]
