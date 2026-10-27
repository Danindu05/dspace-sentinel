"""Stack collectors (SPEC section 6): every ladder against mocked httpx, psycopg, systemd and sockets.

No real service is needed: HTTP goes through ``httpx.MockTransport``, PostgreSQL through a
fake ``AsyncConnection``, systemd / TCP through monkeypatched helpers, the socket table is
injected and the TLS fetch is replaced by a canned :class:`CertInfo`.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import datetime as _dt
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import psutil
import psycopg
import pytest

from dspace_sentinel import dspace_version, health
from dspace_sentinel.alerts import rules
from dspace_sentinel.bus import Bus
from dspace_sentinel.collectors import (
    COLLECTOR_ORDER,
    assetstore,
    backend,
    enabled_collectors,
    frontend,
    nginx,
    ports,
    postgres,
    repository,
    security,
    services,
    solr,
    system,
)
from dspace_sentinel.collectors import base as base_mod
from dspace_sentinel.collectors import ssl as ssl_mod
from dspace_sentinel.collectors.base import CollectorContext, CollectorResult, ProbeStalled
from dspace_sentinel.config import MODULE_IDS, Config, merge_patch
from dspace_sentinel.models import CheckResult, ComponentHealth
from dspace_sentinel.scheduler import Scheduler
from dspace_sentinel.state import State
from dspace_sentinel.store import Store
from dspace_sentinel.util.net import ListeningSocket, TcpResult
from tests.e2e import test_detail_states as detail_states

NOW = 1_800_000_000.0
GIB = 1024**3

# Self-signed test certificate (CN=repo.example.org, O=Test Repository, C=LK; SANs repo.example.org,
# www.repo.example.org, 127.0.0.1; valid 2026-09-24T10:55:40Z → 2036-09-21T10:55:40Z), DER, base64.
FIXTURE_DER = base64.b64decode(
    "MIIDoDCCAoigAwIBAgIUAOb3WqcXD7iTdezdn4UmwxOABYAwDQYJKoZIhvcNAQELBQAwQjELMAkGA1UEBhMCTEsxGDAWBgNVBAoMD1Rl"
    "c3QgUmVwb3NpdG9yeTEZMBcGA1UEAwwQcmVwby5leGFtcGxlLm9yZzAeFw0yNjA5MjQxMDU1NDBaFw0zNjA5MjExMDU1NDBaMEIxCzAJ"
    "BgNVBAYTAkxLMRgwFgYDVQQKDA9UZXN0IFJlcG9zaXRvcnkxGTAXBgNVBAMMEHJlcG8uZXhhbXBsZS5vcmcwggEiMA0GCSqGSIb3DQEB"
    "AQUAA4IBDwAwggEKAoIBAQDNJvLEAxu1d2o+NAfte3rLEr4tVJ5slot+1BPzocWKtYn6dBfRxbHr3QbBfrrk93coqJ9V9dbnvx4ou+xh"
    "ifOZ+5mbfsMOkclXMPEJFlwPpHBH9UOqFNHZtXlmy87PgBL9fucfYEVlJfS3WE54z/vLGEB+zHcrLII7/ZT1IGeY14cGPirjQ6mYMk7p"
    "brVwLY4ZEJmXYCRCoLR4zMIGdUKDTKhFecpyL7nKqVO9FJGaugukXA47yMcicQpuYMxMrZOl39ipg013k1qgi+fJ1wzXpZkrzCwvhuJd"
    "gfS/BPeuVI7TE76DCqVtnDfhIe4mmjTrrEvpf5LY+x6UE41IX/7LAgMBAAGjgY0wgYowHQYDVR0OBBYEFA0Y680Z3TVLj/N07qiiMf78"
    "2ouLMB8GA1UdIwQYMBaAFA0Y680Z3TVLj/N07qiiMf782ouLMA8GA1UdEwEB/wQFMAMBAf8wNwYDVR0RBDAwLoIQcmVwby5leGFtcGxl"
    "Lm9yZ4IUd3d3LnJlcG8uZXhhbXBsZS5vcmeHBH8AAAEwDQYJKoZIhvcNAQELBQADggEBAEgWAf8OccnDAlaZFP2/xEhvTxmLE8jGamnG"
    "+3dhZoZyXn5mjDp4vrCCFl8VNHW8EUePLJOYrXJkah/Lzta6LS0lzFRyjX/VeH6uDEr9thWfN/2edqQOECG671M11E0qzngZWF3LeQcY"
    "qkJJGCC1z4KrUHLdT2QcjLVMlM84TZAMlzALml1hdXoCnsaCtQZKbxnuwz12HuAKOQQTs3o+PHfIloLbOyPX3YbebcNucmYtz5AZLjsH"
    "rEPI4iznQdxWS24zWFllvEkNzYdiMerSS9mSmDwbI3JxIxYM4FQPs9+yd4Zw3PgvzYMqF1MXeLLwcEm2VPpIoGlgs8SeW7zKkAU="
)

# ------------------------------------------------------------------------------ helpers


def all_on(patch: dict[str, Any] | None = None) -> Config:
    """Every module on (non-demo), optionally patched."""
    cfg = merge_patch(Config(), {"modules": {m: True for m in MODULE_IDS}})
    return merge_patch(cfg, patch) if patch else cfg


def ids(result: CollectorResult) -> list[str]:
    return [c.id for c in result.checks]


def by_id(result: CollectorResult) -> dict[str, CheckResult]:
    return {c.id: c for c in result.checks}


def statuses(result: CollectorResult) -> dict[str, str]:
    return {c.id: c.status for c in result.checks}


def component(
    cid: str, status: str, reason: str | None = None, latency: float | None = 12.0
) -> ComponentHealth:
    meta = health.COMPONENTS[cid]
    return ComponentHealth(
        id=cid,
        label=meta.label,
        emoji=meta.emoji,
        status=status,  # type: ignore[arg-type]
        reason=reason,
        chain=[],
        metrics={},
        depends_on=list(meta.depends_on),
        latency_ms=latency,
        since=NOW - 100,
        checked_at=NOW - 1,
    )


def refused(host: str, port: int) -> TcpResult:
    return TcpResult(False, None, f"connection refused ({host}:{port})", host, port)


@pytest.fixture
async def ctx_factory() -> Any:
    """Build a :class:`CollectorContext` around a MockTransport; clients are closed at teardown."""
    clients: list[httpx.AsyncClient] = []
    stores: list[Store] = []

    def _make(
        cfg: Config,
        handler: Callable[[httpx.Request], httpx.Response] | None = None,
        *,
        store: Store | None = None,
        state: State | None = None,
        now: float = NOW,
    ) -> CollectorContext:
        if handler is None:

            def handler(request: httpx.Request) -> httpx.Response:  # noqa: F811 - default stub
                raise httpx.ConnectError("Connection refused", request=request)

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
        clients.append(http)
        if store is None:
            store = Store(":memory:")
            stores.append(store)
        return CollectorContext(cfg=cfg, http=http, store=store, state=state or State(), now=now)

    yield _make
    for c in clients:
        await c.aclose()
    for s in stores:
        s.close()


@pytest.fixture
def stack_ok(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """systemd says every unit is active and every TCP port connects; records what was asked."""
    calls: dict[str, Any] = {"units": [], "tcp": []}
    monkeypatch.setattr(services.systemd, "systemctl_path", lambda: "/usr/bin/systemctl")

    async def _active(unit: str, timeout: float = 3.0) -> str:
        calls["units"].append(unit)
        return "active"

    async def _tcp(host: str, port: int, timeout: float = 2.0) -> TcpResult:
        calls["tcp"].append((host, port))
        return TcpResult(True, 0.42, None, host, port)

    monkeypatch.setattr(services.systemd, "is_active", _active)
    monkeypatch.setattr(services, "tcp_connect", _tcp)
    return calls


def set_unit_state(monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    async def _state(unit: str, timeout: float = 3.0) -> str:
        return state

    monkeypatch.setattr(services.systemd, "is_active", _state)


def set_tcp_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _tcp(host: str, port: int, timeout: float = 2.0) -> TcpResult:
        return refused(host, port)

    monkeypatch.setattr(services, "tcp_connect", _tcp)


# ------------------------------------------------------------------------- fake psycopg


class FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = list(rows)

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class FakeConn:
    """Answers SQL from a script ``{sql or substring: rows | exception}``; exact keys win."""

    def __init__(self, script: dict[str, Any]) -> None:
        self.script = script
        self.executed: list[tuple[str, Any]] = []
        self.closed = False

    async def execute(self, sql: str, params: Any = None) -> FakeCursor:
        self.executed.append((sql, params))
        value: Any = None
        found = False
        if sql in self.script:
            value, found = self.script[sql], True
        else:
            for key, candidate in self.script.items():
                if key in sql:
                    value, found = candidate, True
                    break
        if not found:
            raise psycopg.errors.UndefinedTable(f'relation for "{sql[:40]}" does not exist')
        if isinstance(value, BaseException):
            raise value
        return FakeCursor(value)

    async def close(self) -> None:
        self.closed = True


def install_fake_pg(
    monkeypatch: pytest.MonkeyPatch, script: dict[str, Any] | None = None, error: BaseException | None = None
) -> dict[str, Any]:
    """Replace ``postgres.AsyncConnection``; returns ``{"kwargs": [...], "conns": [...]}``."""
    record: dict[str, Any] = {"kwargs": [], "conns": []}

    class FakeAsyncConnection:
        @classmethod
        async def connect(cls, **kwargs: Any) -> FakeConn:
            record["kwargs"].append(kwargs)
            if error is not None:
                raise error
            conn = FakeConn(dict(script or {}))
            record["conns"].append(conn)
            return conn

    monkeypatch.setattr(postgres, "AsyncConnection", FakeAsyncConnection)
    return record


METRIC_SQL = dict(postgres.METRIC_QUERIES)
PG_VERSION = "PostgreSQL 15.8 (Ubuntu 15.8-1.pgdg24.04+1) on x86_64-pc-linux-gnu, compiled by gcc 13.2.0"


def pg_ok_script() -> dict[str, Any]:
    return {
        "SELECT 1": [(1,)],
        METRIC_SQL["connections"]: [(18,)],
        METRIC_SQL["max_connections"]: [("100",)],
        METRIC_SQL["db_size_bytes"]: [(3_328_599_654,)],
        METRIC_SQL["version"]: [(PG_VERSION,)],
        METRIC_SQL["active_queries"]: [(2,)],
        METRIC_SQL["longest_query_s"]: [(Decimal("0.4231"),)],
    }


def repo_ok_script() -> dict[str, Any]:
    sql = {key: q for key, _table, q in repository.COUNT_QUERIES}
    return {
        sql["items"]: [(27_491,)],
        sql["bitstreams"]: [(81_337, 442_381_631_488)],
        sql["communities"]: [(14,)],
        sql["collections"]: [(96,)],
        sql["eperson_count"]: [(1_842,)],
        sql["workflow_items"]: [(7,)],
        repository.DB_SIZE_SQL: [(3_328_599_654,)],
    }


# =============================================================================== postgres


async def test_postgres_full_ladder_and_metrics(
    stack_ok: dict, ctx_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = install_fake_pg(monkeypatch, pg_ok_script())
    ctx = ctx_factory(all_on())
    result = await postgres.PostgresCollector().collect(ctx)
    assert ids(result) == ["postgres.service", "postgres.tcp", "postgres.connect", "postgres.query"]
    assert set(statuses(result).values()) == {"ok"}
    rungs = by_id(result)
    assert rungs["postgres.service"].label == "systemd postgresql"
    assert rungs["postgres.tcp"].label == "TCP :5432"
    assert rungs["postgres.connect"].label == "Connect as dspace"
    assert rungs["postgres.query"].label == "SELECT 1"
    assert rungs["postgres.query"].latency_ms is not None
    assert stack_ok["units"] == ["postgresql"] and stack_ok["tcp"] == [("localhost", 5432)]
    # SPEC session rules
    kw = record["kwargs"][0]
    assert kw["connect_timeout"] == 5 and kw["autocommit"] is True
    assert kw["application_name"] == "dspace-sentinel"
    assert "statement_timeout=3000" in kw["options"] and "default_transaction_read_only=on" in kw["options"]
    assert kw["dbname"] == "dspace" and kw["user"] == "dspace" and "password" not in kw
    assert record["conns"][0].closed is True
    # metrics
    m = result.metrics
    assert m["connections"] == 18 and m["max_connections"] == 100 and m["connections_pct"] == 18.0
    assert m["db_size_bytes"] == 3_328_599_654
    assert m["version"] == "PostgreSQL 15.8 (Ubuntu 15.8-1.pgdg24.04+1)"
    assert m["active_queries"] == 2 and m["longest_query_s"] == 0.42
    assert "problems" not in m
    comp = health.rollup("postgres", result.checks, result.metrics, now=NOW)
    assert comp.status == "ok" and comp.latency_ms == rungs["postgres.query"].latency_ms
    # connection usage is kept as history (not a rung) for the report's p95
    (conn,) = result.history
    assert (conn.id, conn.status, conn.value) == ("postgres.connections", "ok", 18.0)
    assert conn.message == "18 / 100"
    assert "postgres.connections" not in ids(result)


async def test_postgres_password_and_sslmode_are_passed(
    stack_ok: dict, ctx_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = install_fake_pg(monkeypatch, pg_ok_script())
    cfg = all_on(
        {"postgres": {"password": "s3cret", "sslmode": "require", "host": "db.internal", "port": 5433}}
    )
    await postgres.PostgresCollector().collect(ctx_factory(cfg))
    kw = record["kwargs"][0]
    assert kw["password"] == "s3cret" and kw["sslmode"] == "require"
    assert kw["host"] == "db.internal" and kw["port"] == 5433
    assert stack_ok["tcp"] == [("db.internal", 5433)]


async def test_postgres_service_inactive_blocks_the_ladder(
    stack_ok: dict, ctx_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_unit_state(monkeypatch, "inactive")
    record = install_fake_pg(monkeypatch, pg_ok_script())
    result = await postgres.PostgresCollector().collect(ctx_factory(all_on()))
    assert statuses(result) == {
        "postgres.service": "crit",
        "postgres.tcp": "unknown",
        "postgres.connect": "unknown",
        "postgres.query": "unknown",
    }
    rungs = by_id(result)
    assert rungs["postgres.service"].message == "inactive"
    assert rungs["postgres.tcp"].message == "not reached"
    assert rungs["postgres.query"].message == "not reached"
    assert record["kwargs"] == []  # never tried to connect
    comp = health.rollup("postgres", result.checks, result.metrics, now=NOW)
    assert comp.status == "crit" and comp.reason == "inactive"


async def test_postgres_missing_unit_is_unknown_but_continues(
    stack_ok: dict, ctx_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_unit_state(monkeypatch, "missing")
    install_fake_pg(monkeypatch, pg_ok_script())
    result = await postgres.PostgresCollector().collect(ctx_factory(all_on()))
    rungs = by_id(result)
    assert rungs["postgres.service"].status == "unknown"
    assert rungs["postgres.service"].message == "unit postgresql not found"
    assert rungs["postgres.service"].detail["informational"] is True
    assert rungs["postgres.query"].status == "ok"
    # informational: the component is judged by the real checks, not by the missing unit
    comp = health.rollup("postgres", result.checks, result.metrics, now=NOW)
    assert comp.status == "ok" and comp.reason is None


async def test_postgres_empty_unit_skips_the_service_rung(
    stack_ok: dict, ctx_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_pg(monkeypatch, pg_ok_script())
    result = await postgres.PostgresCollector().collect(ctx_factory(all_on({"postgres": {"service": ""}})))
    assert ids(result) == ["postgres.tcp", "postgres.connect", "postgres.query"]
    assert stack_ok["units"] == []


async def test_postgres_without_systemd(
    stack_ok: dict, ctx_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(services.systemd, "systemctl_path", lambda: None)
    install_fake_pg(monkeypatch, pg_ok_script())
    result = await postgres.PostgresCollector().collect(ctx_factory(all_on()))
    rungs = by_id(result)
    assert rungs["postgres.service"].status == "unknown"
    assert rungs["postgres.service"].message == "systemctl not available"
    assert rungs["postgres.query"].status == "ok"


async def test_postgres_tcp_refused(
    stack_ok: dict, ctx_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_tcp_refused(monkeypatch)
    record = install_fake_pg(monkeypatch, pg_ok_script())
    result = await postgres.PostgresCollector().collect(ctx_factory(all_on()))
    rungs = by_id(result)
    assert rungs["postgres.tcp"].status == "crit"
    assert rungs["postgres.tcp"].message == "connection refused (localhost:5432)"
    assert (
        rungs["postgres.connect"].message == "not reached"
        and rungs["postgres.query"].message == "not reached"
    )
    assert record["kwargs"] == []


async def test_postgres_connect_failure(
    stack_ok: dict, ctx_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = psycopg.OperationalError(
        'connection failed: connection to server at "127.0.0.1", port 5432 failed: '
        'FATAL:  password authentication failed for user "dspace"'
    )
    install_fake_pg(monkeypatch, error=error)
    result = await postgres.PostgresCollector().collect(ctx_factory(all_on()))
    rungs = by_id(result)
    assert rungs["postgres.connect"].status == "crit"
    assert rungs["postgres.connect"].message == "password authentication failed for user dspace"
    assert rungs["postgres.query"].status == "unknown" and rungs["postgres.query"].message == "not reached"
    assert result.metrics == {}


async def test_postgres_connect_timeout(
    stack_ok: dict, ctx_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Slow:
        @classmethod
        async def connect(cls, **kwargs: Any) -> Any:
            await asyncio.sleep(30)

    monkeypatch.setattr(postgres, "AsyncConnection", Slow)
    monkeypatch.setattr(postgres, "CONNECT_TIMEOUT_S", 0.05)
    result = await postgres.PostgresCollector().collect(ctx_factory(all_on()))
    assert by_id(result)["postgres.connect"].message == "timed out after 0.05 s (localhost:5432)"


async def test_postgres_query_failure_closes_connection(
    stack_ok: dict, ctx_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = pg_ok_script()
    script["SELECT 1"] = psycopg.errors.QueryCanceled("canceling statement due to statement timeout")
    record = install_fake_pg(monkeypatch, script)
    result = await postgres.PostgresCollector().collect(ctx_factory(all_on()))
    rungs = by_id(result)
    assert rungs["postgres.connect"].status == "ok"
    assert rungs["postgres.query"].status == "crit"
    assert rungs["postgres.query"].message == "statement timed out after 3 s"
    assert record["conns"][0].closed is True
    assert result.metrics == {}


async def test_postgres_metrics_are_best_effort(
    stack_ok: dict, ctx_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = pg_ok_script()
    script[METRIC_SQL["connections"]] = psycopg.errors.InsufficientPrivilege(
        "permission denied for view pg_stat_activity"
    )
    install_fake_pg(monkeypatch, script)
    result = await postgres.PostgresCollector().collect(ctx_factory(all_on()))
    assert by_id(result)["postgres.query"].status == "ok"
    assert result.metrics["connections"] is None and result.metrics["connections_pct"] is None
    assert result.metrics["max_connections"] == 100
    assert result.metrics["problems"] == ["connections: permission denied for user dspace"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "connection to server at 127.0.0.1 failed: Connection refused",
            "connection refused (localhost:5432)",
        ),
        ("connection timeout expired", "timed out after 5 s (localhost:5432)"),
        (
            'FATAL:  no pg_hba.conf entry for host "10.0.0.2", user "dspace"',
            "rejected by pg_hba.conf (dspace@localhost:5432/dspace)",
        ),
        ('FATAL:  database "dspace" does not exist', 'database "dspace" does not exist'),
        ('FATAL:  role "dspace" does not exist', 'role "dspace" does not exist'),
        (
            'could not translate host name "db" to address: Name or service not known',
            "DNS lookup failed (localhost)",
        ),
        ("something odd\nsecond line", "something odd"),
    ],
)
def test_describe_pg_error(text: str, expected: str) -> None:
    assert postgres.describe_pg_error(psycopg.OperationalError(text), Config().postgres) == expected


def test_describe_pg_error_timeout_and_cancel() -> None:
    pg = Config().postgres
    assert postgres.describe_pg_error(asyncio.TimeoutError(), pg) == "timed out after 5 s (localhost:5432)"
    assert (
        postgres.describe_pg_error(psycopg.errors.QueryCanceled("x"), pg) == "statement timed out after 3 s"
    )


def test_http_message_wordings() -> None:
    from dspace_sentinel.util.httpc import HttpProbe

    def failed(error: str, url: str = "http://localhost:80/", code: int | None = None) -> HttpProbe:
        return HttpProbe(False, code, None, error, url, {}, "")

    assert services.http_message(failed("all connection attempts failed (http://localhost:80/)")) == (
        "connection refused (localhost:80)"
    )
    assert services.http_message(
        failed("connection refused (http://localhost:8080/server/api)", "http://localhost:8080/server/api")
    ) == ("connection refused (localhost:8080)")
    assert services.http_message(
        failed("timeout (https://repo.example.org/)", "https://repo.example.org/")
    ) == ("timed out after 5 s (repo.example.org:443)")
    assert services.http_message(failed("HTTP 502 (http://localhost/)", code=502)) == "HTTP 502"
    assert services.http_message(HttpProbe(True, 200, 1.0, None, "http://x/", {}, "")) == ""
    assert services.url_parts("https://repo.example.org/server/").hostport == "repo.example.org:443"
    assert services.url_parts("http://solr.internal:8984/solr").path == "/solr"


# ================================================================================== solr

SOLR_CORES = {
    "search": {
        "name": "search",
        "uptime": 3_600_000,
        "index": {"numDocs": 27_601, "sizeInBytes": 2_040_109_465},
    },
    "statistics": {"name": "statistics", "index": {"numDocs": 1_842_331, "sizeInBytes": 6_871_947_673}},
    "oai": {"name": "oai", "index": {"numDocs": 27_491, "sizeInBytes": 429_916_160}},
    "authority": {"name": "authority", "index": {"numDocs": 5_210, "sizeInBytes": 39_845_888}},
}


def solr_handler(opts: dict[str, Any] | None = None) -> Callable[[httpx.Request], httpx.Response]:
    opts = {} if opts is None else opts
    requests: list[httpx.Request] = opts.setdefault("requests", [])

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if opts.get("down"):
            raise httpx.ConnectError("Connection refused", request=request)
        if path.endswith("/admin/cores"):
            if opts.get("admin") == 401:
                return httpx.Response(401, text="require authentication")
            if opts.get("admin") == "html":
                return httpx.Response(200, text="<html>not solr</html>")
            if "status_payload" in opts:
                return httpx.Response(200, json=opts["status_payload"])
            return httpx.Response(200, json={"responseHeader": {"status": 0}, "status": SOLR_CORES})
        if path.endswith("/admin/info/system"):
            return httpx.Response(200, json={"lucene": {"solr-spec-version": "9.6.1"}})
        if path.endswith("/admin/ping"):
            core = path.rsplit("/", 3)[-3]
            if core in opts.get("missing", ()):
                return httpx.Response(404, text="Not Found")
            if core in opts.get("ping_error", ()):
                return httpx.Response(500, json={"error": {"msg": f"SolrCore '{core}' is not available"}})
            return httpx.Response(200, json={"status": "OK"})
        if path.endswith("/select"):
            if opts.get("search") == 500:
                return httpx.Response(500, json={"error": {"msg": "boom"}})
            return httpx.Response(200, json={"response": {"numFound": 27_601, "start": 0, "docs": []}})
        return httpx.Response(404, text="nope")

    return handler


async def test_solr_full_ladder(stack_ok: dict, ctx_factory: Any) -> None:
    opts: dict[str, Any] = {}
    result = await solr.SolrCollector().collect(ctx_factory(all_on(), solr_handler(opts)))
    assert ids(result) == [
        "solr.service",
        "solr.tcp",
        "solr.admin",
        "solr.core.search",
        "solr.core.statistics",
        "solr.core.oai",
        "solr.core.authority",
        "solr.search",
    ]
    assert set(statuses(result).values()) == {"ok"}
    rungs = by_id(result)
    assert rungs["solr.service"].label == "systemd solr" and rungs["solr.tcp"].label == "TCP :8983"
    assert rungs["solr.admin"].label == "Admin cores" and rungs["solr.admin"].message == "4 cores"
    assert rungs["solr.core.search"].label == "Core search" and rungs["solr.core.search"].value == 27_601.0
    assert rungs["solr.search"].label == "Search query"
    assert rungs["solr.search"].detail == {"query": "q=*:*&rows=0", "core": "search", "status_code": 200}
    assert rungs["solr.admin"].detail["status_code"] == 200
    select = [r for r in opts["requests"] if r.url.path.endswith("/select")]
    assert len(select) == 1 and select[0].url.path == "/solr/search/select"
    assert select[0].url.params["rows"] == "0" and select[0].url.params["q"] == "*:*"
    m = result.metrics
    assert m["version"] == "9.6.1" and m["search_docs"] == 27_601 and m["search_latency_ms"] is not None
    assert [c["name"] for c in m["cores"]] == ["search", "statistics", "oai", "authority"]
    assert m["cores"][1] == {
        "name": "statistics",
        "docs": 1_842_331,
        "size_bytes": 6_871_947_673,
        "status": "ok",
    }
    assert stack_ok["tcp"] == [("localhost", 8983)]
    comp = health.rollup("solr", result.checks, result.metrics, now=NOW)
    assert comp.latency_ms == rungs["solr.search"].latency_ms


async def test_solr_pings_the_cores_concurrently(stack_ok: dict, ctx_factory: Any) -> None:
    """A slow Solr (the "search slow" band) must fit the collector budget: one wait for all pings."""
    answer = solr_handler()

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.3)
        return answer(request)

    started = time.perf_counter()
    result = await solr.SolrCollector().collect(ctx_factory(all_on(), slow))
    elapsed = time.perf_counter() - started
    # admin/cores, then version + 4 pings together, then the search: ~0.9 s (one by one: 2.1 s)
    assert elapsed < 1.5, elapsed
    assert ids(result)[3:] == [
        "solr.core.search",
        "solr.core.statistics",
        "solr.core.oai",
        "solr.core.authority",
        "solr.search",
    ]
    assert set(statuses(result).values()) == {"ok"} and result.metrics["version"] == "9.6.1"


def test_collector_timeouts_fit_their_ladders() -> None:
    """``interval - 1``, but never less than the worst case of the rung timeouts in a row."""
    fast = merge_patch(Config(), {"collector": {"http_interval": 5, "db_interval": 5, "service_interval": 5}})
    assert solr.SolrCollector().timeout(fast) >= 20  # systemd 3 + TCP 2 + three HTTP waits of 5 s
    assert backend.BackendCollector().timeout(fast) >= 20
    assert frontend.FrontendCollector().timeout(fast) >= 15
    assert nginx.NginxCollector().timeout(fast) >= 13
    assert postgres.PostgresCollector().timeout(fast) >= 30
    assert solr.SolrCollector().timeout(Config()) == 29.0  # the interval rule when it is longer
    assert ports.PortsCollector().timeout(fast) == 4.0


async def test_solr_missing_core_does_not_hide_search(stack_ok: dict, ctx_factory: Any) -> None:
    result = await solr.SolrCollector().collect(
        ctx_factory(all_on(), solr_handler({"missing": {"authority"}}))
    )
    rungs = by_id(result)
    assert rungs["solr.core.authority"].status == "crit"
    assert rungs["solr.core.authority"].message == "core not found (HTTP 404)"
    assert rungs["solr.search"].status == "ok"
    assert [c["status"] for c in result.metrics["cores"]] == ["ok", "ok", "ok", "crit"]
    comp = health.rollup("solr", result.checks, result.metrics, now=NOW)
    assert comp.status == "crit" and comp.reason == "core not found (HTTP 404)"


async def test_solr_admin_unreachable_blocks_the_rest(stack_ok: dict, ctx_factory: Any) -> None:
    result = await solr.SolrCollector().collect(ctx_factory(all_on(), solr_handler({"down": True})))
    rungs = by_id(result)
    assert rungs["solr.admin"].status == "crit"
    assert rungs["solr.admin"].message == "connection refused (localhost:8983)"
    for rid in ("solr.core.search", "solr.core.oai", "solr.search"):
        assert rungs[rid].status == "unknown" and rungs[rid].message == "not reached"
    assert result.metrics["search_latency_ms"] is None
    assert all(c["status"] == "unknown" for c in result.metrics["cores"])


async def test_solr_admin_auth_required_is_warn_and_continues(stack_ok: dict, ctx_factory: Any) -> None:
    result = await solr.SolrCollector().collect(ctx_factory(all_on(), solr_handler({"admin": 401})))
    rungs = by_id(result)
    assert rungs["solr.admin"].status == "warn"
    assert rungs["solr.admin"].message == "authentication required (HTTP 401)"
    assert rungs["solr.search"].status == "ok"


async def test_solr_admin_not_solr(stack_ok: dict, ctx_factory: Any) -> None:
    result = await solr.SolrCollector().collect(ctx_factory(all_on(), solr_handler({"admin": "html"})))
    assert by_id(result)["solr.admin"].message == "unexpected response (not Solr)"
    assert by_id(result)["solr.search"].message == "not reached"


async def test_solr_search_error_and_slow(stack_ok: dict, ctx_factory: Any) -> None:
    result = await solr.SolrCollector().collect(ctx_factory(all_on(), solr_handler({"search": 500})))
    assert by_id(result)["solr.search"].status == "crit"
    assert by_id(result)["solr.search"].message == "HTTP 500 (search/select)"
    slow_cfg = all_on({"alerts": {"search_latency_ms": {"warning": 0, "critical": 100000}}})
    result = await solr.SolrCollector().collect(ctx_factory(slow_cfg, solr_handler()))
    rung = by_id(result)["solr.search"]
    assert rung.status == "warn" and rung.message.startswith("search took ")


async def test_solr_tcp_refused_and_custom_url(
    stack_ok: dict, ctx_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_tcp_refused(monkeypatch)
    cfg = all_on({"solr": {"url": "http://solr.internal:8984/solr/", "cores": ["search", "oai"]}})
    result = await solr.SolrCollector().collect(ctx_factory(cfg, solr_handler()))
    rungs = by_id(result)
    assert rungs["solr.tcp"].label == "TCP :8984"
    assert rungs["solr.tcp"].message == "connection refused (solr.internal:8984)"
    assert ids(result) == [
        "solr.service",
        "solr.tcp",
        "solr.admin",
        "solr.core.search",
        "solr.core.oai",
        "solr.search",
    ]
    assert all(rungs[r].message == "not reached" for r in ids(result)[2:])


def test_solr_search_core_choice() -> None:
    assert solr.search_core_for(["search", "oai"]) == "search"
    assert solr.search_core_for(["statistics", "oai"]) == "statistics"
    assert solr.search_core_for([]) == "search"
    assert solr.parse_core_status(
        {"status": {"x": {"index": {"numDocs": 3, "sizeInBytes": 10}, "uptime": 2500}}}
    ) == {"x": {"docs": 3, "size_bytes": 10, "uptime_s": 2}}
    assert solr.parse_core_status("garbage") == {}


SOLR_EMPTY_CORE = {"index": {"numDocs": 0, "sizeInBytes": 101}}


def solr_status(*names: str, prefix: str = "", **extra: Any) -> dict[str, Any]:
    """A CoreAdmin STATUS payload (StatusOp's shape) holding ``names`` (``SOLR_CORES`` values when known)."""
    status = {
        f"{prefix}{name}": {**SOLR_CORES.get(name, SOLR_EMPTY_CORE), "name": f"{prefix}{name}"}
        for name in names
    }
    return {"responseHeader": {"status": 0, "QTime": 2}, "initFailures": {}, "status": status, **extra}


def core_rungs(result: CollectorResult) -> list[str]:
    return [c.id.removeprefix("solr.core.") for c in result.checks if c.id.startswith("solr.core.")]


async def test_solr_explains_a_core_that_failed_to_load(stack_ok: dict, ctx_factory: Any) -> None:
    """A core that did not load is only in STATUS's initFailures (DSpace 9 on Solr 9.8+ without
    solr.config.lib.enabled, say): its rung says why, and nothing asks the dead core anything."""
    opts: dict[str, Any] = {"status_payload": live_json("solr-status-init-failure.json")}
    result = await solr.SolrCollector().collect(ctx_factory(all_on(), solr_handler(opts)))
    rungs = by_id(result)
    reason = (
        "Could not load conf for core search: Can't load schema managed-schema.xml: Plugin init failure for "
        "[schema.xml] fieldType \"...\": Error loading class 'solr.ICUCollationField'"
    )
    assert rungs["solr.core.search"].status == "crit"
    assert rungs["solr.core.search"].message == f"failed to load: {reason}"
    assert rungs["solr.admin"].status == "ok" and rungs["solr.admin"].message == "5 cores, 1 failed to load"
    assert rungs["solr.admin"].detail["failed"] == ["search"] and "missing" not in rungs["solr.admin"].detail
    # the optional DSpace 8+ cores that STATUS lists get their own rung, after the configured ones
    assert core_rungs(result) == ["search", "statistics", "oai", "authority", "qaevent", "suggestion"]
    assert rungs["solr.core.qaevent"].status == "ok" and rungs["solr.core.qaevent"].detail["optional"] is True
    assert (
        rungs["solr.search"].status == "crit" and rungs["solr.search"].message == "core search failed to load"
    )
    asked = [r.url.path for r in opts["requests"]]
    assert "/solr/search/admin/ping" not in asked and "/solr/search/select" not in asked
    assert "/solr/qaevent/admin/ping" in asked
    assert result.metrics["init_failures"] == {"search": reason}
    cores = {c["name"]: c for c in result.metrics["cores"]}
    assert cores["search"]["status"] == "crit" and cores["suggestion"]["optional"] is True
    comp = health.rollup("solr", result.checks, result.metrics, now=NOW)
    assert comp.status == "crit" and comp.reason == f"failed to load: {reason}"


RELOAD_REASON = (
    "Could not load conf for core search: Can't load schema managed-schema.xml: [schema.xml] "
    "Duplicate field definition for 'dc.title'"
)


async def test_solr_core_whose_reload_failed_still_serves(stack_ok: dict, ctx_factory: Any) -> None:
    """A failed RELOAD keeps the old core loaded: STATUS lists it in status *and* initFailures. It is
    pinged and searched as usual, and its rung warns that a restart would not load it."""
    opts: dict[str, Any] = {"status_payload": live_json("solr-status-reload-failure.json")}
    result = await solr.SolrCollector().collect(ctx_factory(all_on(), solr_handler(opts)))
    rungs = by_id(result)
    core = rungs["solr.core.search"]
    assert core.status == "warn"
    assert core.message == f"reload failed: {RELOAD_REASON} (still serving; a restart would not load it)"
    assert core.detail["reload_failed"] == RELOAD_REASON and core.value == 27_600.0
    assert core.latency_ms is not None  # pinged
    admin = rungs["solr.admin"]
    assert admin.status == "ok" and admin.message == "6 cores, 1 failed to reload"
    assert admin.detail["reload_failed"] == ["search"] and "failed" not in admin.detail
    assert rungs["solr.search"].status == "ok" and rungs["solr.search"].value == 27_601.0
    asked = [r.url.path for r in opts["requests"]]
    assert "/solr/search/admin/ping" in asked and "/solr/search/select" in asked
    assert result.metrics["init_failures"] == {}
    assert result.metrics["reload_failures"] == {"search": RELOAD_REASON}
    assert result.metrics["search_docs"] == 27_601
    cores = {c["name"]: c for c in result.metrics["cores"]}
    assert cores["search"]["status"] == "warn" and cores["search"]["docs"] == 27_600
    comp = health.rollup("solr", result.checks, result.metrics, now=NOW)
    assert comp.status == "warn" and comp.reason == core.message


async def test_solr_reload_failure_with_a_failing_ping_is_the_ping_problem(
    stack_ok: dict, ctx_factory: Any
) -> None:
    payload = live_json("solr-status-reload-failure.json")
    opts: dict[str, Any] = {"status_payload": payload, "ping_error": ("search",)}
    rung = by_id(await solr.SolrCollector().collect(ctx_factory(all_on(), solr_handler(opts))))[
        "solr.core.search"
    ]
    assert rung.status == "crit" and rung.message.startswith("HTTP 500")
    assert rung.detail["reload_failed"] == RELOAD_REASON
    # a reload that failed on Solr 9.8+'s <lib/> rule names the fix, like a core that failed to load
    lib = (
        "org.apache.solr.common.SolrException: Configset references one or more <lib/> directives, "
        "but <lib/> usage is disabled on this Solr node"
    )
    opts = {"status_payload": {**payload, "initFailures": {"search": lib}}}
    rung = by_id(await solr.SolrCollector().collect(ctx_factory(all_on(), solr_handler(opts))))[
        "solr.core.search"
    ]
    assert rung.status == "warn" and rung.detail["hint"] == solr.LIB_HINT


def test_solr_failure_reason_and_lib_hint() -> None:
    lib = (
        "org.apache.solr.common.SolrException: Configset references one or more <lib/> directives, but "
        "<lib/> usage is disabled on this Solr node. Either remove all <lib/> tags from the configset, or "
        "enable use of the tag by setting the solr.config.lib.enabled system property to true"
    )
    reason = solr.failure_reason(lib)
    assert reason.startswith("Configset references one or more <lib/> directives") and len(reason) <= 200
    assert solr.failure_hint(lib) == 'start Solr with SOLR_OPTS="-Dsolr.config.lib.enabled=true" (Solr 9.8+)'
    assert solr.failure_hint("java.io.IOException: disk full") is None
    assert solr.failure_reason("first line\nat x.y(Z.java:1)") == "first line"
    assert solr.failure_reason(None) == "no reason given" and solr.parse_init_failures("garbage") == {}


async def test_solr_lib_hint_on_the_rung(stack_ok: dict, ctx_factory: Any) -> None:
    payload = solr_status("statistics", "oai", "authority")
    payload["initFailures"] = {
        "search": "org.apache.solr.common.SolrException: <lib/> usage is disabled on this node"
    }
    result = await solr.SolrCollector().collect(
        ctx_factory(all_on(), solr_handler({"status_payload": payload}))
    )
    assert by_id(result)["solr.core.search"].detail["hint"].startswith('start Solr with SOLR_OPTS="')


@pytest.mark.parametrize(
    ("cores", "expected"),
    [
        (("search", "statistics", "oai", "authority"), ["search", "statistics", "oai", "authority"]),  # 7.x
        (
            ("search", "statistics", "oai", "authority", "qaevent", "suggestion"),  # 8.x and 9.x
            ["search", "statistics", "oai", "authority", "qaevent", "suggestion"],
        ),
        (
            ("search", "statistics", "oai", "authority", "qaevent", "suggestion", "audit"),  # 10.x
            ["search", "statistics", "oai", "authority", "qaevent", "suggestion", "audit"],
        ),
        # yearly statistics shards are neither missing nor rungs of their own
        (
            ("search", "statistics", "statistics-2024", "oai", "authority"),
            ["search", "statistics", "oai", "authority"],
        ),
    ],
)
async def test_solr_optional_cores_come_from_status(
    stack_ok: dict, ctx_factory: Any, cores: tuple[str, ...], expected: list[str]
) -> None:
    opts = {"status_payload": solr_status(*cores)}
    result = await solr.SolrCollector().collect(ctx_factory(all_on(), solr_handler(opts)))
    assert core_rungs(result) == expected
    assert set(statuses(result).values()) == {"ok"}
    assert "missing" not in by_id(result)["solr.admin"].detail
    assert [c["name"] for c in result.metrics["cores"]] == expected


async def test_solr_optional_cores_follow_the_dspace_version(stack_ok: dict, ctx_factory: Any) -> None:
    """With the backend's version known, a DSpace 7 site never grows a "suggestion" rung (8+ core)."""
    payload = solr_status("search", "statistics", "oai", "authority", "suggestion", "audit")
    for version, expected in (("7.6.6", []), ("9.4", ["suggestion"]), ("10.1", ["suggestion", "audit"])):
        backend_comp = component("backend", "ok")
        backend_comp.metrics = {"dspace_version": version}
        result = await solr.SolrCollector().collect(
            ctx_factory(
                all_on(), solr_handler({"status_payload": payload}), state=deps_state(backend=backend_comp)
            )
        )
        assert core_rungs(result)[4:] == expected, version


async def test_solr_optional_core_problems_are_warnings(stack_ok: dict, ctx_factory: Any) -> None:
    payload = solr_status("search", "statistics", "oai", "authority", "suggestion")
    payload["initFailures"] = {
        "qaevent": "org.apache.solr.common.SolrException: Could not load conf for core qaevent"
    }
    opts = {"status_payload": payload, "ping_error": {"suggestion"}}
    result = await solr.SolrCollector().collect(ctx_factory(all_on(), solr_handler(opts)))
    rungs = by_id(result)
    assert rungs["solr.core.qaevent"].status == "warn"
    assert rungs["solr.core.qaevent"].message == "failed to load: Could not load conf for core qaevent"
    assert (
        rungs["solr.core.suggestion"].status == "warn" and rungs["solr.core.suggestion"].message == "HTTP 500"
    )
    assert rungs["solr.search"].status == "ok"
    assert health.rollup("solr", result.checks, result.metrics, now=NOW).status == "warn"


async def test_solr_core_prefix(stack_ok: dict, ctx_factory: Any) -> None:
    """solr.multicorePrefix prefixes every DSpace core; other applications' cores are left alone."""
    payload = solr_status(
        "search", "statistics", "oai", "authority", "qaevent", "suggestion", prefix="dspace_"
    )
    payload["status"]["dspace_statistics-2024"] = {"name": "dspace_statistics-2024", **SOLR_EMPTY_CORE}
    payload["status"]["other_qaevent"] = {"name": "other_qaevent", **SOLR_EMPTY_CORE}
    cfg = all_on(
        {"solr": {"cores": ["dspace_search", "dspace_statistics", "dspace_oai", "dspace_authority"]}}
    )
    opts: dict[str, Any] = {"status_payload": payload}
    result = await solr.SolrCollector().collect(ctx_factory(cfg, solr_handler(opts)))
    assert core_rungs(result) == [
        "dspace_search",
        "dspace_statistics",
        "dspace_oai",
        "dspace_authority",
        "dspace_qaevent",
        "dspace_suggestion",
    ]
    assert set(statuses(result).values()) == {"ok"}
    select = [r for r in opts["requests"] if r.url.path.endswith("/select")]
    assert [r.url.path for r in select] == ["/solr/dspace_search/select"]
    assert result.metrics["search_core"] == "dspace_search"


def test_solr_prefix_helpers() -> None:
    assert solr.core_prefix(["dspace_search", "dspace_oai"]) == "dspace_"
    assert solr.core_prefix(["dspace_oai", "dspace_statistics"]) == "dspace_"
    assert solr.core_prefix(["search", "oai"]) == "" and solr.core_prefix([]) == ""
    assert solr.search_core_for(["dspace_statistics", "dspace_search"]) == "dspace_search"


# =============================================================================== backend

REST_ROOT = {
    "dspaceUI": "http://localhost:4000",
    "dspaceName": "Test Repository",
    "dspaceServer": "http://localhost:8080/server",
    "dspaceVersion": "DSpace 8.1",
}
SEARCH_OK = {"_embedded": {"searchResult": {"page": {"totalElements": 27_491, "size": 1}}}}
LIVE = Path(__file__).parent / "fixtures" / "dspace-live"


def live_json(name: str) -> dict[str, Any]:
    """A real payload from ``tests/fixtures/dspace-live`` (without the file's ``_source`` note)."""
    import json

    data = json.loads((LIVE / name).read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if k != "_source"}


LIVE_ROOTS = live_json("rest-roots.json")
LIVE_ACTUATOR = live_json("actuator.json")
DOWN_WITH_COMPONENTS = {  # what an admin (or show-details=always) sees while Solr is down
    "status": "DOWN",
    "components": {
        "db": {"status": "UP"},
        "solrSearchCore": {"status": "DOWN"},
        "geoIp": {"status": "UNKNOWN"},
    },
}


def backend_handler(opts: dict[str, Any] | None = None) -> Callable[[httpx.Request], httpx.Response]:
    """The DSpace REST API as an anonymous client sees it.

    ``opts``: ``root`` (the ``/server/api`` payload), ``rest`` ("html", or an HTTP status such as
    502 or 503 while the backend restarts), ``actuator`` (a body dict served with HTTP 200 like
    every real DSpace, "up" = the real anonymous ``{"status":"UP"}``, "admin" = the component view,
    "timeout" = no answer in time, or an HTTP status for "not exposed"), ``search`` (500); every
    request is appended to ``opts["requests"]``.
    """
    opts = {} if opts is None else opts
    requests: list[httpx.Request] = opts.setdefault("requests", [])

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/server/api":
            if opts.get("rest") == "html":
                return httpx.Response(200, text="<html>Tomcat default page</html>")
            if isinstance(opts.get("rest"), int):
                return httpx.Response(opts["rest"], text="Service Unavailable")
            return httpx.Response(200, json=opts.get("root", REST_ROOT))
        if path == "/server/actuator/health":
            mode = opts.get("actuator", "up")
            if isinstance(mode, dict):
                return httpx.Response(200, json=mode)  # DSpace maps DOWN and OUT_OF_SERVICE to 200
            if mode == "up":
                return httpx.Response(200, json=LIVE_ACTUATOR["up"])
            if mode == "admin":
                return httpx.Response(200, json=LIVE_ACTUATOR["admin_view"])
            if mode == "timeout":
                raise httpx.ReadTimeout("timed out", request=request)
            return httpx.Response(int(mode), text="nope")
        if path == "/server/api/discover/search/objects":
            assert request.url.params["size"] == "1"
            if opts.get("search") == 500:
                return httpx.Response(500, json={"message": "Solr unreachable"})
            return httpx.Response(200, json=opts.get("discover", SEARCH_OK))
        return httpx.Response(404, text="nope")

    return handler


def actuator_requests(opts: dict[str, Any]) -> int:
    return sum(1 for r in opts["requests"] if r.url.path.endswith("/actuator/health"))


def deps_state(**comps: ComponentHealth) -> State:
    state = State()
    for comp in comps.values():
        state.set_component(comp)
    return state


async def test_backend_full_ladder_with_dependencies(stack_ok: dict, ctx_factory: Any) -> None:
    state = deps_state(postgres=component("postgres", "ok"), solr=component("solr", "ok"))
    result = await backend.BackendCollector().collect(ctx_factory(all_on(), backend_handler(), state=state))
    assert ids(result) == [
        "backend.service",
        "backend.tcp",
        "backend.rest",
        "backend.actuator",
        "backend.search",
        "backend.dep.postgres",
        "backend.dep.solr",
    ]
    assert set(statuses(result).values()) == {"ok"}
    rungs = by_id(result)
    assert rungs["backend.service"].label == "systemd tomcat9"
    assert rungs["backend.tcp"].label == "TCP :8080"
    assert rungs["backend.rest"].label == "GET /server/api" and rungs["backend.rest"].message == "DSpace 8.1"
    assert rungs["backend.actuator"].label == "Actuator health" and rungs["backend.actuator"].message == "UP"
    assert rungs["backend.search"].label == "Search API" and rungs["backend.search"].value == 27_491.0
    assert rungs["backend.dep.postgres"].label == "Database dependency"
    assert rungs["backend.dep.solr"].label == "Solr dependency"
    assert rungs["backend.dep.postgres"].message == "PostgreSQL is healthy"
    m = result.metrics
    assert m["dspace_version"] == "8.1" and m["dspace_name"] == "Test Repository"
    assert m["dspace_major"] == 8 and m["dspace_minor"] == 1 and m["dspace_patch"] is None
    assert m["dspace_snapshot"] is False and m["cris_version"] is None
    assert m["rest_latency_ms"] is not None and m["search_latency_ms"] is not None
    # anonymous: the aggregate status only, never the components
    assert m["actuator_status"] == "UP" and m["actuator_components"] == {}
    assert rungs["backend.actuator"].detail["health_status"] == "UP"
    assert stack_ok["units"] == ["tomcat9"] and stack_ok["tcp"] == [("localhost", 8080)]
    comp = health.rollup("backend", result.checks, result.metrics, now=NOW)
    assert comp.latency_ms == rungs["backend.rest"].latency_ms


@pytest.mark.parametrize("key", ["7.6.6", "8.3", "8.4", "9.0", "9.2", "10.0", "8.2-cris"])
async def test_backend_reads_every_live_rest_root(stack_ok: dict, ctx_factory: Any, key: str) -> None:
    """The REST root is the same in 7, 8, 9 and 10 (10 adds ``uniqueType``, CRIS ``crisVersion``)."""
    root = LIVE_ROOTS[key]
    opts = {"root": root, "discover": live_json("discover-7.6.6.json")}
    result = await backend.BackendCollector().collect(ctx_factory(all_on(), backend_handler(opts)))
    rungs = by_id(result)
    assert rungs["backend.rest"].status == "ok" and rungs["backend.rest"].message == root["dspaceVersion"]
    assert rungs["backend.search"].value == 115_670.0
    m = result.metrics
    assert m["dspace_version"] == root["dspaceVersion"].removeprefix("DSpace ")
    assert m["dspace_major"] == int(key.split(".")[0]) and m["dspace_name"] == root["dspaceName"]
    assert m["cris_version"] == ("cris-2024.02.04" if key == "8.2-cris" else None)


async def test_backend_actuator_not_exposed_is_unknown(stack_ok: dict, ctx_factory: Any) -> None:
    for code in (401, 404):
        result = await backend.BackendCollector().collect(
            ctx_factory(all_on(), backend_handler({"actuator": str(code)}))
        )
        rungs = by_id(result)
        assert rungs["backend.actuator"].status == "unknown"
        assert rungs["backend.actuator"].message == f"not exposed (HTTP {code})"
        assert rungs["backend.actuator"].detail["informational"] is True
        assert rungs["backend.actuator"].detail["status_code"] == code
        assert rungs["backend.search"].status == "ok"  # not a failure: the ladder goes on
        assert result.metrics["actuator_components"] == {}
        # a backend without an actuator endpoint is still a healthy backend
        comp = health.rollup("backend", result.checks, result.metrics, now=NOW)
        assert comp.status == "ok" and comp.reason is None
        assert rungs["backend.rest"].detail["status_code"] == 200  # doctor prints "200 / 91 ms"
        assert rungs["backend.search"].detail["status_code"] == 200


async def test_backend_actuator_down_with_components_names_them(stack_ok: dict, ctx_factory: Any) -> None:
    """Visible components (an admin view, or show-details=always) name what is down, in any version."""
    opts = {"actuator": DOWN_WITH_COMPONENTS, "root": LIVE_ROOTS["8.4"]}  # 8.4: inside the SEO window
    result = await backend.BackendCollector().collect(ctx_factory(all_on(), backend_handler(opts)))
    rung = by_id(result)["backend.actuator"]
    assert rung.status == "warn" and rung.message == "status DOWN (solrSearchCore)"
    assert rung.detail["status_code"] == 200 and rung.detail["health_status"] == "DOWN"
    assert result.metrics["actuator_components"] == {"db": "UP", "solrSearchCore": "DOWN", "geoIp": "UNKNOWN"}
    assert result.metrics["actuator_status"] == "DOWN"
    result = await backend.BackendCollector().collect(
        ctx_factory(all_on(), backend_handler({"actuator": "admin"}))
    )
    rung = by_id(result)["backend.actuator"]
    assert rung.status == "ok" and rung.message == "up, with non-blocking issues (geoIp)"
    assert result.metrics["actuator_components"]["seo"] == "UP"


SEO_NOTE = "a known SEO health check issue fixed in 8.5 / 9.4 / 10.1"
ADMIN_NOTE = "details need a DSpace admin login"


@pytest.mark.parametrize(
    ("body", "version", "status", "message", "informational"),
    [
        (LIVE_ACTUATOR["up"], "DSpace 9.4", "ok", "UP", False),
        (LIVE_ACTUATOR["up_groups"], "DSpace 8.2", "ok", "UP", False),  # MIT: k8s probe groups
        (LIVE_ACTUATOR["up_with_issues"], "DSpace 9.0", "ok", "up, with non-blocking issues", False),
        (LIVE_ACTUATOR["up_with_issues"], "DSpace 8.3", "ok", "up, with non-blocking issues", False),
        # DOWN inside the SEO window (7.6.4-7.6.x, 8.2-8.4, 9.0-9.3, 10.0) on a working REST API
        (
            LIVE_ACTUATOR["down"],
            "DSpace 8.4",
            "unknown",
            f"DSpace 8.4 reports DOWN while healthy ({SEO_NOTE})",
            True,
        ),
        (
            LIVE_ACTUATOR["down"],
            "DSpace 9.2",
            "unknown",
            f"DSpace 9.2 reports DOWN while healthy ({SEO_NOTE})",
            True,
        ),
        (
            LIVE_ACTUATOR["down"],
            "DSpace 9.3",
            "unknown",
            f"DSpace 9.3 reports DOWN while healthy ({SEO_NOTE})",
            True,
        ),
        (
            LIVE_ACTUATOR["down"],
            "DSpace 7.6.6",
            "unknown",
            f"DSpace 7.6.6 reports DOWN while healthy ({SEO_NOTE})",
            True,
        ),
        (
            LIVE_ACTUATOR["down"],
            "DSpace 10.0",
            "unknown",
            f"DSpace 10.0 reports DOWN while healthy ({SEO_NOTE})",
            True,
        ),
        # DOWN outside it: a real problem that only an admin login can name
        (LIVE_ACTUATOR["down"], "DSpace 9.4", "warn", f"status DOWN ({ADMIN_NOTE})", False),
        (LIVE_ACTUATOR["down"], "DSpace 8.5", "warn", f"status DOWN ({ADMIN_NOTE})", False),
        (LIVE_ACTUATOR["down"], "DSpace 8.1", "warn", f"status DOWN ({ADMIN_NOTE})", False),
        (LIVE_ACTUATOR["down"], "DSpace 7.6.1", "warn", f"status DOWN ({ADMIN_NOTE})", False),
        (LIVE_ACTUATOR["down"], "DSpace 10.2-SNAPSHOT", "warn", f"status DOWN ({ADMIN_NOTE})", False),
        (LIVE_ACTUATOR["down"], "DSpace unknown", "warn", f"status DOWN ({ADMIN_NOTE})", False),
        # the SEO indicator never answers OUT_OF_SERVICE
        ({"status": "OUT_OF_SERVICE"}, "DSpace 9.2", "warn", f"status OUT_OF_SERVICE ({ADMIN_NOTE})", False),
        (DOWN_WITH_COMPONENTS, "DSpace 9.2", "warn", "status DOWN (solrSearchCore)", False),
        (
            {"status": "DOWN", "components": {"db": {"status": "DOWN"}}},
            "DSpace 9.4",
            "warn",
            "status DOWN (db)",
            False,
        ),
    ],
)
async def test_backend_actuator_is_graded_the_way_dspace_defines_it(
    stack_ok: dict,
    ctx_factory: Any,
    body: dict[str, Any],
    version: str,
    status: str,
    message: str,
    informational: bool,
) -> None:
    root = {**REST_ROOT, "dspaceVersion": version}
    result = await backend.BackendCollector().collect(
        ctx_factory(all_on(), backend_handler({"actuator": body, "root": root}))
    )
    rung = by_id(result)["backend.actuator"]
    assert (rung.status, rung.message) == (status, message)
    assert rung.detail["status_code"] == 200
    assert bool(rung.detail.get("informational")) is informational
    comp = health.rollup("backend", result.checks, result.metrics, now=NOW)
    # UP_WITH_ISSUES and the SEO window's DOWN leave a healthy backend healthy; a real DOWN is a warning
    assert comp.status == ("warn" if status == "warn" else "ok")
    obs = {o.rule: o for o in rules.evaluate(None, {"backend": comp}, all_on())}
    assert (obs["component_down:backend"].severity == "warning") is (status == "warn")


SITEMAP_MISSING = (
    "Sitemaps are missing or inaccessible. Please see the DSpace Documentation on Search Engine "
    "Optimization for how to enable Sitemaps."
)
SSR_OFF = (
    "Server-side rendering (SSR) appears to be disabled. Most search engines require enabling SSR "
    "for proper indexing. Please see the DSpace Documentation on Search Engine Optimization for more details."
)


def admin_health(status: str, seo: str, **others: str) -> dict[str, Any]:
    """What DSpace shows with component details (an admin, or show-details=always)."""
    ok = {"sitemap": "OK", "robots.txt": "OK", "ssr": "OK"}
    seo_details = (
        ok
        if seo in ("UP", "UP_WITH_ISSUES") and not others.get("seo_details")
        else {
            **ok,
            "sitemap": SITEMAP_MISSING,
            "ssr": SSR_OFF,
        }
    )
    components: dict[str, Any] = {
        "db": {"status": others.get("db", "UP")},
        "geoIp": {
            "status": "UP_WITH_ISSUES",
            "details": {"reason": "The required 'dbfile' configuration is missing"},
        },
        "solrSearchCore": {"status": others.get("solr", "UP")},
        "solrStatisticsCore": {"status": "UP"},
        "solrOaiCore": {"status": "UP"},
        "solrAuthorityCore": {"status": "UP"},
        "seo": {"status": seo, "details": seo_details},
    }
    return {"status": status, "components": components}


SEO_ONLY = "DSpace 9.2 reports DOWN while healthy (a known SEO health check issue fixed in 8.5 / 9.4 / 10.1)"


@pytest.mark.parametrize(
    ("body", "version", "status", "message", "known_issue"),
    [
        # inside the SEO window, a DOWN whose only DOWN component is seo is the known issue, not a warning
        (
            admin_health("DOWN", "DOWN"),
            "DSpace 9.2",
            "unknown",
            f"{SEO_ONLY}: the SEO check flags sitemap, ssr",
            "seo",
        ),
        (LIVE_ACTUATOR["down"], "DSpace 9.2", "unknown", SEO_ONLY, "seo"),
        # 8.5 / 9.4 / 10.1 report the same condition as UP_WITH_ISSUES
        (
            admin_health("UP_WITH_ISSUES", "UP_WITH_ISSUES"),
            "DSpace 9.4",
            "ok",
            "up, with non-blocking issues (geoIp, seo)",
            None,
        ),
        # anything else down, or seo DOWN on a release whose SEO check never answers DOWN: a warning
        (admin_health("DOWN", "DOWN", db="DOWN"), "DSpace 9.2", "warn", "status DOWN (db, seo)", None),
        (admin_health("DOWN", "UP", solr="DOWN"), "DSpace 9.2", "warn", "status DOWN (solrSearchCore)", None),
        (admin_health("DOWN", "DOWN"), "DSpace 9.4", "warn", "status DOWN (seo)", None),
        (admin_health("DOWN", "DOWN"), "DSpace 8.1", "warn", "status DOWN (seo)", None),
        (admin_health("DOWN", "DOWN"), "DSpace unknown", "warn", "status DOWN (seo)", None),
    ],
)
def test_grade_health_with_visible_components(
    body: dict[str, Any], version: str, status: str, message: str, known_issue: str | None
) -> None:
    grade = backend.grade_health(body, dspace_version.parse(version))
    assert grade is not None
    assert (grade.status, grade.message, grade.known_issue) == (status, message, known_issue)
    assert grade.informational is (status == "unknown")


async def test_backend_seo_only_down_with_visible_components_is_the_known_issue(
    stack_ok: dict, ctx_factory: Any
) -> None:
    """show-details=always on 9.2: an unmade sitemap must not be a permanent 'DSpace Backend degraded'."""
    opts = {"actuator": admin_health("DOWN", "DOWN"), "root": {**REST_ROOT, "dspaceVersion": "DSpace 9.2"}}
    result = await backend.BackendCollector().collect(ctx_factory(all_on(), backend_handler(opts)))
    rung = by_id(result)["backend.actuator"]
    assert rung.status == "unknown" and rung.detail["informational"] is True
    assert rung.detail["known_issue"] == "seo" and rung.detail["health_status"] == "DOWN"
    assert result.metrics["actuator_components"]["seo"] == "DOWN"
    comp = health.rollup("backend", result.checks, result.metrics, now=NOW)
    assert comp.status == "ok"
    obs = {o.rule: o for o in rules.evaluate(None, {"backend": comp}, all_on())}
    assert obs["component_down:backend"].severity is None


def test_grade_health_directly() -> None:
    down = backend.grade_health(LIVE_ACTUATOR["down"], dspace_version.parse("DSpace 9.2"))
    assert down is not None and down.status == "unknown" and down.informational and down.known_issue == "seo"
    # the SEO explanation needs a REST API that answers; otherwise DOWN stays a warning
    blind = backend.grade_health(LIVE_ACTUATOR["down"], dspace_version.parse("DSpace 9.2"), rest_ok=False)
    assert blind is not None and blind.status == "warn" and blind.message == f"status DOWN ({ADMIN_NOTE})"
    assert backend.grade_health({}, None) is None and backend.grade_health("DOWN", None) is None
    assert backend.grade_health({"status": " up "}, None).status == "ok"  # type: ignore[union-attr]


async def test_backend_seo_down_needs_a_rest_api_that_answers(stack_ok: dict, ctx_factory: Any) -> None:
    """A REST API that answers, even slowly, explains an SEO-window DOWN (slowness has its own rule)."""
    cfg = all_on({"alerts": {"rest_latency_ms": {"warning": 0, "critical": 100000}}})
    root = {**REST_ROOT, "dspaceVersion": "DSpace 9.2"}
    result = await backend.BackendCollector().collect(
        ctx_factory(cfg, backend_handler({"actuator": LIVE_ACTUATOR["down"], "root": root}))
    )
    rungs = by_id(result)
    assert rungs["backend.rest"].status == "warn" and rungs["backend.rest"].detail["slow"] is True
    assert rungs["backend.actuator"].status == "unknown" and rungs["backend.actuator"].detail["informational"]


async def test_backend_actuator_is_asked_at_most_every_five_minutes(stack_ok: dict, ctx_factory: Any) -> None:
    """Each actuator call runs every DSpace health indicator (the SEO one renders the homepage)."""
    collector = backend.BackendCollector()
    opts: dict[str, Any] = {"actuator": LIVE_ACTUATOR["up_with_issues"]}
    first = await collector.collect(ctx_factory(all_on(), backend_handler(opts), now=NOW))
    assert actuator_requests(opts) == 1 and "cached_age_s" not in by_id(first)["backend.actuator"].detail
    # 30 s later (the next http_interval): the answer is reused, and graded again
    opts["actuator"] = LIVE_ACTUATOR["down"]
    again = await collector.collect(ctx_factory(all_on(), backend_handler(opts), now=NOW + 30))
    rung = by_id(again)["backend.actuator"]
    assert actuator_requests(opts) == 1
    assert rung.status == "ok" and rung.message == "up, with non-blocking issues"
    assert rung.detail["cached_age_s"] == 30 and rung.detail["status_code"] == 200
    assert again.metrics["actuator_status"] == "UP_WITH_ISSUES"
    # the REST and search rungs are measured every time
    assert sum(1 for r in opts["requests"] if r.url.path == "/server/api") == 2
    # after ACTUATOR_MIN_INTERVAL_S the actuator is asked again
    assert backend.ACTUATOR_MIN_INTERVAL_S == 300
    later = await collector.collect(ctx_factory(all_on(), backend_handler(opts), now=NOW + 300))
    assert actuator_requests(opts) == 2 and by_id(later)["backend.actuator"].status == "warn"
    # another server URL, or a clock that went backwards, never reuses an answer
    moved = all_on({"dspace": {"server_url": "http://localhost:8081/server"}})
    await collector.collect(ctx_factory(moved, backend_handler(opts), now=NOW + 301))
    assert actuator_requests(opts) == 3
    await collector.collect(ctx_factory(moved, backend_handler(opts), now=NOW + 100))
    assert actuator_requests(opts) == 4
    # a fresh collector (doctor, a config reload) asks straight away
    await backend.BackendCollector().collect(ctx_factory(moved, backend_handler(opts), now=NOW + 101))
    assert actuator_requests(opts) == 5


ROOT_9_4 = {**REST_ROOT, "dspaceVersion": "DSpace 9.4"}


@pytest.mark.parametrize("blip", [LIVE_ACTUATOR["down"], "timeout", "500"])
async def test_backend_actuator_asks_again_after_a_bad_answer(
    stack_ok: dict, ctx_factory: Any, blip: Any
) -> None:
    """Only a healthy or informational answer is reused: a DOWN, a time-out or an error is asked again."""
    collector = backend.BackendCollector()
    opts: dict[str, Any] = {"root": ROOT_9_4, "actuator": blip}
    bad = by_id(await collector.collect(ctx_factory(all_on(), backend_handler(opts), now=NOW)))
    assert bad["backend.actuator"].status == "warn" and actuator_requests(opts) == 1
    # the next cycle asks again: the answer it gets is the one it shows
    opts["actuator"] = LIVE_ACTUATOR["up_with_issues"]
    good = by_id(await collector.collect(ctx_factory(all_on(), backend_handler(opts), now=NOW + 30)))
    assert actuator_requests(opts) == 2
    rung = good["backend.actuator"]
    assert (rung.status, rung.message) == ("ok", "up, with non-blocking issues")
    assert "cached_age_s" not in rung.detail
    # a healthy answer is reused as before
    await collector.collect(ctx_factory(all_on(), backend_handler(opts), now=NOW + 60))
    assert actuator_requests(opts) == 2


@pytest.mark.parametrize("informational", [LIVE_ACTUATOR["down"], "404"])
async def test_backend_actuator_reuses_an_informational_answer(
    stack_ok: dict, ctx_factory: Any, informational: Any
) -> None:
    """The SEO window's DOWN and a hidden actuator stay as they are for five minutes: reused."""
    collector = backend.BackendCollector()
    opts: dict[str, Any] = {"root": {**REST_ROOT, "dspaceVersion": "DSpace 9.2"}, "actuator": informational}
    for offset in (0, 30, 60):
        result = await collector.collect(ctx_factory(all_on(), backend_handler(opts), now=NOW + offset))
        rung = by_id(result)["backend.actuator"]
        assert rung.status == "unknown" and rung.detail["informational"] is True
    assert actuator_requests(opts) == 1


@pytest.mark.parametrize("blip", [LIVE_ACTUATOR["down"], "timeout"])
async def test_one_bad_actuator_answer_opens_no_alert(stack_ok: dict, ctx_factory: Any, blip: Any) -> None:
    """confirm_samples counts real answers: one transient DOWN or time-out among healthy ones is no alert."""
    from dspace_sentinel.alerts.engine import AlertEngine

    async def quiet(*args: Any, **kwargs: Any) -> list[Any]:
        return []

    cfg = all_on({"modules": {"postgres": False, "solr": False}})
    store = Store(":memory:")
    engine = AlertEngine(store, cfg, Bus(), dispatcher=quiet)
    collector = backend.BackendCollector()
    opts: dict[str, Any] = {"root": ROOT_9_4}
    seen: list[tuple[int, str]] = []
    emitted: list[dict[str, Any]] = []
    try:
        for i in range(28):  # 14 minutes at http_interval 30 s; the one bad answer comes at t+300
            now = NOW + 30 * i
            opts["actuator"] = blip if i == 10 else LIVE_ACTUATOR["up_with_issues"]
            result = await collector.collect(ctx_factory(cfg, backend_handler(opts), now=now))
            seen.append((30 * i, by_id(result)["backend.actuator"].status))
            comp = health.rollup("backend", result.checks, result.metrics, now=now)
            emitted += await engine.evaluate(None, {"backend": comp}, now=now)
    finally:
        await engine.flush(5)
        store.close()
    assert [t for t, status in seen if status != "ok"] == [300]  # t+330 asked again and got UP
    assert not [e for e in emitted if e.get("type") in ("opened", "escalated")], emitted


async def test_backend_actuator_answer_does_not_outlive_a_restart_or_an_upgrade(
    stack_ok: dict, ctx_factory: Any
) -> None:
    """9.3's SEO DOWN, a restart into 9.4: the new backend is asked, not graded on the old answer."""
    collector = backend.BackendCollector()
    opts: dict[str, Any] = {
        "root": {**REST_ROOT, "dspaceVersion": "DSpace 9.3"},
        "actuator": LIVE_ACTUATOR["down"],
    }
    before = by_id(await collector.collect(ctx_factory(all_on(), backend_handler(opts), now=NOW)))
    assert before["backend.actuator"].detail["known_issue"] == "seo"
    opts["rest"] = 503  # restarting
    during = by_id(await collector.collect(ctx_factory(all_on(), backend_handler(opts), now=NOW + 30)))
    assert during["backend.actuator"].message == "not reached"
    del opts["rest"]
    opts.update(root=ROOT_9_4, actuator=LIVE_ACTUATOR["up_with_issues"])
    after = by_id(await collector.collect(ctx_factory(all_on(), backend_handler(opts), now=NOW + 90)))
    rung = after["backend.actuator"]
    assert (rung.status, rung.message) == ("ok", "up, with non-blocking issues")
    assert actuator_requests(opts) == 2 and "cached_age_s" not in rung.detail
    comp = health.rollup("backend", list(after.values()), {}, now=NOW + 90)
    assert rules.component_down_rule(comp, all_on()).severity is None


async def test_backend_actuator_is_asked_again_when_the_version_changes(
    stack_ok: dict, ctx_factory: Any
) -> None:
    """An upgrade between two cycles (no failed cycle seen) still asks the new release."""
    collector = backend.BackendCollector()
    opts: dict[str, Any] = {
        "root": {**REST_ROOT, "dspaceVersion": "DSpace 9.3"},
        "actuator": LIVE_ACTUATOR["down"],
    }
    await collector.collect(ctx_factory(all_on(), backend_handler(opts), now=NOW))
    opts.update(root=ROOT_9_4, actuator=LIVE_ACTUATOR["up_with_issues"])
    result = await collector.collect(ctx_factory(all_on(), backend_handler(opts), now=NOW + 30))
    assert by_id(result)["backend.actuator"].status == "ok" and actuator_requests(opts) == 2


@pytest.mark.parametrize("stop", ["service", "tcp"])
async def test_backend_actuator_is_asked_again_after_the_ladder_stopped(
    stack_ok: dict, ctx_factory: Any, monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    """A stopped unit or a closed port means a restart: the answer from before it is dropped."""
    collector = backend.BackendCollector()
    opts: dict[str, Any] = {"root": ROOT_9_4, "actuator": LIVE_ACTUATOR["up_with_issues"]}
    await collector.collect(ctx_factory(all_on(), backend_handler(opts), now=NOW))
    with monkeypatch.context() as m:
        if stop == "service":
            set_unit_state(m, "inactive")
        else:
            set_tcp_refused(m)
        down = by_id(await collector.collect(ctx_factory(all_on(), backend_handler(opts), now=NOW + 30)))
        assert down["backend.actuator"].message == "not reached"
    await collector.collect(ctx_factory(all_on(), backend_handler(opts), now=NOW + 60))
    assert actuator_requests(opts) == 2


async def test_backend_search_failure(stack_ok: dict, ctx_factory: Any) -> None:
    result = await backend.BackendCollector().collect(ctx_factory(all_on(), backend_handler({"search": 500})))
    rung = by_id(result)["backend.search"]
    assert rung.status == "crit" and rung.message == "HTTP 500 (discover/search/objects)"
    assert rung.detail["status_code"] == 500
    comp = health.rollup("backend", result.checks, result.metrics, now=NOW)
    assert comp.status == "crit" and comp.chain_summary().startswith(
        "service ✅ → tcp ✅ → rest ✅ → actuator ✅ → search ❌"
    )


async def test_backend_rest_failures_block_and_keep_dependencies(stack_ok: dict, ctx_factory: Any) -> None:
    result = await backend.BackendCollector().collect(
        ctx_factory(all_on(), backend_handler({"rest": "html"}))
    )
    rungs = by_id(result)
    assert rungs["backend.rest"].status == "crit"
    assert rungs["backend.rest"].message == "unexpected response (not the DSpace REST API)"
    assert (
        rungs["backend.actuator"].message == "not reached"
        and rungs["backend.search"].message == "not reached"
    )
    assert "backend.dep.postgres" in rungs and rungs["backend.dep.postgres"].message == "not monitored"
    result = await backend.BackendCollector().collect(ctx_factory(all_on(), backend_handler({"rest": 502})))
    assert by_id(result)["backend.rest"].message == "HTTP 502"


async def test_backend_unreachable(stack_ok: dict, ctx_factory: Any) -> None:
    result = await backend.BackendCollector().collect(ctx_factory(all_on()))  # every request refused
    rungs = by_id(result)
    assert rungs["backend.rest"].status == "crit"
    assert rungs["backend.rest"].message == "connection refused (localhost:8080)"
    assert result.metrics["dspace_version"] is None


async def test_backend_dependency_rules(stack_ok: dict, ctx_factory: Any) -> None:
    # solr module off → no solr rung; postgres on but never collected → "not monitored"
    cfg = all_on({"modules": {"solr": False}})
    result = await backend.BackendCollector().collect(ctx_factory(cfg, backend_handler()))
    assert "backend.dep.solr" not in ids(result)
    dep = by_id(result)["backend.dep.postgres"]
    assert dep.status == "unknown" and dep.message == "not monitored"
    # a critical dependency is mirrored, capped at warn, with its reason
    state = deps_state(
        postgres=component("postgres", "crit", "connection refused (localhost:5432)"),
        solr=component("solr", "warn", "search took 2.1 s"),
    )
    result = await backend.BackendCollector().collect(ctx_factory(all_on(), backend_handler(), state=state))
    rungs = by_id(result)
    assert rungs["backend.dep.postgres"].status == "warn"
    assert rungs["backend.dep.postgres"].message == (
        "PostgreSQL is down (Critical): connection refused (localhost:5432)"
    )
    assert rungs["backend.dep.solr"].status == "warn"
    assert rungs["backend.dep.solr"].message == "Solr is degraded (Warning): search took 2.1 s"
    comp = health.rollup("backend", result.checks, result.metrics, now=NOW)
    assert comp.status == "warn"


async def test_backend_service_empty_and_slow_rest(stack_ok: dict, ctx_factory: Any) -> None:
    cfg = all_on(
        {"dspace": {"backend_service": ""}, "alerts": {"rest_latency_ms": {"warning": 0, "critical": 100000}}}
    )
    result = await backend.BackendCollector().collect(ctx_factory(cfg, backend_handler()))
    assert ids(result)[0] == "backend.tcp"
    rung = by_id(result)["backend.rest"]
    assert rung.status == "warn" and rung.message.startswith("REST API took ")


async def test_backend_slow_rest_keeps_climbing_and_reads_slow(stack_ok: dict, ctx_factory: Any) -> None:
    """REST answering correctly but past the critical latency is slow, not a failed rung."""
    cfg = all_on({"alerts": {"rest_latency_ms": {"warning": 0, "critical": 0}}})  # any answer is "slow"
    result = await backend.BackendCollector().collect(ctx_factory(cfg, backend_handler()))
    rest = by_id(result)["backend.rest"]
    assert rest.status == "crit" and rest.message.startswith("REST API took ") and rest.detail["slow"] is True
    st = statuses(result)
    assert st["backend.actuator"] == "ok" and st["backend.search"] == "ok"  # reached: search still probed
    assert result.metrics["search_latency_ms"] is not None
    comp = health.rollup("backend", result.checks, result.metrics, now=NOW)
    state = State()
    state.set_component(comp)
    assert health.headline(state, cfg) == ("crit", "Critical: DSpace Backend slow")
    obs = {o.rule: o for o in rules.evaluate(None, {"backend": comp}, cfg)}
    assert obs["rest_latency"].severity == "critical" and obs["component_down:backend"].ok is True


def test_backend_helpers() -> None:
    assert backend.version_number("DSpace 7.6.1") == "7.6.1" and backend.version_number("8.0") == "8.0"
    assert backend.version_number(None) is None
    assert backend.actuator_components({"components": {"db": {"status": "UP"}, "x": "UP"}}) == {
        "db": "UP",
        "x": "UP",
    }
    assert backend.search_total(SEARCH_OK) == 27_491 and backend.search_total({}) is None


# ============================================================================== frontend

HOME_HTML = (
    "<!doctype html><html><head><title>Test  Repository &amp; Archive</title></head>"
    "<body><ds-app></ds-app></body></html>"
)


def live_home(name: str) -> str:
    """A real homepage (``tests/fixtures/dspace-live/homepages``)."""
    return (LIVE / "homepages" / name).read_text(encoding="utf-8")


def frontend_handler(opts: dict[str, Any] | None = None) -> Callable[[httpx.Request], httpx.Response]:
    """The UI on :4000 and through the public URL; ``opts["home"]``: "ok", "plain", an HTML page or
    an HTTP status; ``opts["public"]``: 502. Every request is appended to ``opts["requests"]``."""
    opts = {} if opts is None else opts
    requests: list[httpx.Request] = opts.setdefault("requests", [])

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        host = request.url.host
        if host == "repo.example.org":
            if opts.get("public") == 502:
                return httpx.Response(502, text="Bad Gateway")
            return httpx.Response(200, text=HOME_HTML)
        mode = opts.get("home", "ok")
        if mode == "ok":
            return httpx.Response(200, text=HOME_HTML)
        if mode == "plain":
            return httpx.Response(200, text="<html><title>Welcome to nginx!</title></html>")
        if isinstance(mode, str) and mode.lstrip().startswith("<"):
            return httpx.Response(200, text=mode)
        return httpx.Response(int(mode), text="Bad Gateway")

    return handler


async def test_frontend_full_ladder(stack_ok: dict, ctx_factory: Any) -> None:
    state = deps_state(backend=component("backend", "ok"))
    result = await frontend.FrontendCollector().collect(
        ctx_factory(all_on(), frontend_handler(), state=state)
    )
    assert ids(result) == ["frontend.service", "frontend.tcp", "frontend.home", "frontend.dep.backend"]
    assert set(statuses(result).values()) == {"ok"}
    rungs = by_id(result)
    assert (
        rungs["frontend.service"].label == "systemd dspace-ui" and rungs["frontend.tcp"].label == "TCP :4000"
    )
    assert (
        rungs["frontend.home"].label == "Homepage"
        and rungs["frontend.home"].detail["title"] == "Test Repository & Archive"
    )
    assert rungs["frontend.dep.backend"].label == "Backend dependency"
    assert result.metrics["title"] == "Test Repository & Archive"
    assert result.metrics["home_latency_ms"] is not None and result.metrics["public_latency_ms"] is None
    assert stack_ok["tcp"] == [("localhost", 4000)]


async def test_frontend_public_url_rung(stack_ok: dict, ctx_factory: Any) -> None:
    cfg = all_on({"dspace": {"public_url": "https://repo.example.org/"}})
    result = await frontend.FrontendCollector().collect(ctx_factory(cfg, frontend_handler()))
    assert ids(result) == [
        "frontend.service",
        "frontend.tcp",
        "frontend.home",
        "frontend.public",
        "frontend.dep.backend",
    ]
    assert by_id(result)["frontend.public"].label == "Public URL"
    assert by_id(result)["frontend.public"].status == "ok" and result.metrics["public_latency_ms"] is not None
    result = await frontend.FrontendCollector().collect(ctx_factory(cfg, frontend_handler({"public": 502})))
    assert (
        by_id(result)["frontend.public"].status == "crit"
        and by_id(result)["frontend.public"].message == "HTTP 502"
    )
    # homepage down → public not reached
    result = await frontend.FrontendCollector().collect(ctx_factory(cfg, frontend_handler({"home": "502"})))
    assert by_id(result)["frontend.home"].message == "HTTP 502"
    assert by_id(result)["frontend.public"].message == "not reached"


async def test_frontend_wrong_page_is_warn(stack_ok: dict, ctx_factory: Any) -> None:
    result = await frontend.FrontendCollector().collect(
        ctx_factory(all_on(), frontend_handler({"home": "plain"}))
    )
    rung = by_id(result)["frontend.home"]
    assert rung.status == "warn" and rung.message == "no DSpace markup in the response"
    assert result.metrics["title"] == "Welcome to nginx!"


async def test_frontend_slow_home_and_backend_off(stack_ok: dict, ctx_factory: Any) -> None:
    cfg = all_on(
        {"modules": {"backend": False}, "alerts": {"home_latency_ms": {"warning": 0, "critical": 100000}}}
    )
    result = await frontend.FrontendCollector().collect(ctx_factory(cfg, frontend_handler()))
    assert "frontend.dep.backend" not in ids(result)
    rung = by_id(result)["frontend.home"]
    assert rung.status == "warn" and rung.message.startswith("homepage took ")


async def test_frontend_slow_home_still_checks_the_public_url(stack_ok: dict, ctx_factory: Any) -> None:
    cfg = all_on(
        {
            "dspace": {"public_url": "https://repo.example.org"},
            "alerts": {"home_latency_ms": {"warning": 0, "critical": 0}},
        }
    )
    result = await frontend.FrontendCollector().collect(ctx_factory(cfg, frontend_handler()))
    home = by_id(result)["frontend.home"]
    assert home.status == "crit" and home.detail["slow"] is True
    assert by_id(result)["frontend.public"].status == "ok"  # not "not reached"


def test_frontend_helpers() -> None:
    assert frontend.looks_like_dspace("<DS-APP>") and frontend.looks_like_dspace(
        "<ds-app ng-version=17.3.12>"
    )
    # the word alone is not the app: a bot check in front of dspace.example.edu mentions it too
    assert not frontend.looks_like_dspace("… DSpace …") and not frontend.looks_like_dspace("<ds-application>")
    assert not frontend.looks_like_dspace("<html>hello</html>") and not frontend.looks_like_dspace("")
    # a server-rendered page names DSpace in its <head>, long before a large inlined stylesheet ends
    assert frontend.looks_like_dspace('<head><meta content="DSpace 9.2" name="generator"><style>')
    assert frontend.page_title("<title>\n  A &lt;b&gt;\n</title>") == "A <b>"
    assert frontend.page_title("no title") is None
    assert frontend.rendering("") is None and frontend.rendering("<html></html>") is None
    assert frontend.generator("<meta name=generator content='DSpace 8.1'>") == "DSpace 8.1"
    assert frontend.generator('<meta name="description" content="x">') is None


@pytest.mark.parametrize(
    ("page", "rendering", "generator", "angular"),
    [
        ("7.6.5-ssr.html", "ssr", "DSpace 7.6.5", "15.2.10"),
        ("8.3-ssr.html", "ssr", "DSpace 8.3", "17.3.12"),
        ("9.0-ssr.html", "ssr", "DSpace 9.0", "18.2.13"),
        ("9.2-ssr.html", "ssr", "DSpace 9.2", "20.3.14"),
        ("8.1-minified.html", "ssr", None, "17.3.12"),  # a proxy dropped the attribute quotes
        ("csr-shell.html", "csr", None, None),  # src/index.html, identical in 7.6.7, 8.5, 9.4, 10.1
    ],
)
async def test_frontend_tells_server_rendering_from_the_client_shell(
    stack_ok: dict, ctx_factory: Any, page: str, rendering: str, generator: str | None, angular: str | None
) -> None:
    html = live_home(page)
    result = await frontend.FrontendCollector().collect(
        ctx_factory(all_on(), frontend_handler({"home": html}))
    )
    home = by_id(result)["frontend.home"]
    assert home.status == "ok"  # a client-side shell is a note, never a warning
    assert home.detail["rendering"] == rendering
    m = result.metrics
    assert (m["rendering"], m["generator"], m["angular"]) == (rendering, generator, angular)
    if rendering == "csr":
        assert home.message == frontend.CSR_NOTE and home.detail["note"] == frontend.CSR_NOTE
        assert m["title"] == "DSpace"
    else:
        assert home.message == "" and "note" not in home.detail


async def test_frontend_bot_check_page_is_no_dspace_markup(stack_ok: dict, ctx_factory: Any) -> None:
    page = live_home("constructed-bot-check.html")
    assert "dspace" in page.lower()  # the loose marker would have taken it for the UI
    result = await frontend.FrontendCollector().collect(
        ctx_factory(all_on(), frontend_handler({"home": page}))
    )
    home = by_id(result)["frontend.home"]
    assert home.status == "warn" and home.message == "no DSpace markup in the response"
    assert result.metrics["rendering"] is None and result.metrics["title"] == "Making sure you're not a bot!"


async def test_frontend_sends_the_public_host(stack_ok: dict, ctx_factory: Any) -> None:
    """9.3+ render on the server only for the host of ui.baseUrl: probe :4000 as the public site."""
    cfg = all_on({"dspace": {"public_url": "https://repo.example.org/"}})
    opts: dict[str, Any] = {}
    result = await frontend.FrontendCollector().collect(ctx_factory(cfg, frontend_handler(opts)))
    home_req, public_req = opts["requests"]
    assert home_req.url.host == "localhost" and home_req.url.port == 4000
    assert home_req.headers["host"] == "repo.example.org"
    assert home_req.headers["x-forwarded-proto"] == "https"
    assert public_req.url.host == "repo.example.org" and public_req.headers["host"] == "repo.example.org"
    assert "x-forwarded-proto" not in public_req.headers
    assert by_id(result)["frontend.home"].detail["host_header"] == "repo.example.org"
    assert result.metrics["host_header"] == "repo.example.org"
    # a plain-HTTP public URL: the host only
    opts = {}
    plain = all_on({"dspace": {"public_url": "http://repo.example.org:8081"}})
    await frontend.FrontendCollector().collect(ctx_factory(plain, frontend_handler(opts)))
    assert opts["requests"][0].headers["host"] == "repo.example.org"
    assert "x-forwarded-proto" not in opts["requests"][0].headers
    # no public URL, or one on the probed host itself: nothing is changed
    for patch in (
        {},
        {"dspace": {"ui_url": "https://repo.example.org", "public_url": "https://repo.example.org"}},
    ):
        opts = {}
        result = await frontend.FrontendCollector().collect(
            ctx_factory(all_on(patch), frontend_handler(opts))
        )
        first = opts["requests"][0]
        assert first.headers["host"] == first.url.netloc.decode()
        assert "x-forwarded-proto" not in first.headers
        assert "host_header" not in by_id(result)["frontend.home"].detail
        assert result.metrics["host_header"] is None


async def test_frontend_sends_an_idn_public_host_in_its_ascii_form(stack_ok: dict, ctx_factory: Any) -> None:
    """A public URL as users type it (https://bücher.example) goes into the Host header IDNA-encoded;
    as written, httpx could not encode the header and no request was sent (a false critical)."""
    cfg = all_on({"dspace": {"public_url": "https://bücher.example"}})
    opts: dict[str, Any] = {}
    state = deps_state(backend=component("backend", "ok"))
    result = await frontend.FrontendCollector().collect(ctx_factory(cfg, frontend_handler(opts), state=state))
    home_req, public_req = opts["requests"]
    assert home_req.url.host == "localhost" and home_req.headers["host"] == "xn--bcher-kva.example"
    assert home_req.headers["x-forwarded-proto"] == "https"
    assert public_req.url.raw_host == b"xn--bcher-kva.example"
    assert set(statuses(result).values()) == {"ok"}
    assert by_id(result)["frontend.home"].detail["host_header"] == "xn--bcher-kva.example"  # as sent
    assert result.metrics["host_header"] == "bücher.example"  # as the setting writes it
    assert health.rollup("frontend", result.checks, result.metrics, now=NOW).status == "ok"
    # either spelling names the same host; a name with no IDNA form sends no Host header
    assert frontend.wire_host("bücher.example") == "xn--bcher-kva.example"
    assert frontend.wire_host("repo.example.org") == "repo.example.org"
    assert frontend.public_host_headers("https://bücher.example", "https://xn--bcher-kva.example/") == {}
    assert frontend.wire_host("☃.example") is None
    assert frontend.public_host_headers("http://localhost:4000", "https://☃.example") == {}


def backend_seen(version: str, dspace_ui: str | None = "https://repo.example.org") -> ComponentHealth:
    """The backend as the frontend collector finds it: the version and the REST root's dspaceUI."""
    comp = component("backend", "ok")
    comp.metrics = {"dspace_version": version}
    detail = {"dspaceVersion": f"DSpace {version}"} | ({"dspaceUI": dspace_ui} if dspace_ui else {})
    comp.chain = [CheckResult(id="backend.rest", label="GET /server/api", status="ok", ts=NOW, detail=detail)]
    return comp


HOST_RULE_NOTE = (
    "client-side rendering only: DSpace 9.4 renders on the server only for the host in its ui.baseUrl "
    "(set dspace.public_url)"
)


@pytest.mark.parametrize(
    ("ui_url", "public_url", "dspace_ui", "note"),
    [
        # the probe goes to localhost while ui.baseUrl names the public site: the Host rule
        ("http://localhost:4000", "", "https://repo.example.org", HOST_RULE_NOTE),
        ("http://localhost:4000", "https://www.example.org", "https://repo.example.org", HOST_RULE_NOTE),
        # the request already carried ui.baseUrl's host: SSR is off or failing, not the Host rule
        ("https://repo.example.org", "https://repo.example.org", None, frontend.CSR_NOTE),
        ("https://repo.example.org", "", None, frontend.CSR_NOTE),
        ("https://repo.example.org", "", "https://repo.example.org", frontend.CSR_NOTE),
        ("http://localhost:4000", "https://repo.example.org", "https://REPO.example.org/", frontend.CSR_NOTE),
        ("http://localhost:4000", "", "http://localhost:4000", frontend.CSR_NOTE),
        # nothing says which host ui.baseUrl names: no guess
        ("http://localhost:4000", "", None, frontend.CSR_NOTE),
    ],
)
async def test_frontend_csr_note_blames_the_host_rule_only_for_another_host(
    stack_ok: dict, ctx_factory: Any, ui_url: str, public_url: str, dspace_ui: str | None, note: str
) -> None:
    """9.3+ serve the client-side shell to a Host other than ui.baseUrl's; to that host, a shell means
    SSR is off or failing, and 'set dspace.public_url' would be wrong advice."""
    cfg = all_on({"dspace": {"ui_url": ui_url, "public_url": public_url}})
    state = deps_state(backend=backend_seen("9.4", dspace_ui))
    handler = frontend_handler({"home": live_home("csr-shell.html")})
    result = await frontend.FrontendCollector().collect(ctx_factory(cfg, handler, state=state))
    home = by_id(result)["frontend.home"]
    assert (home.status, home.detail["rendering"]) == ("ok", "csr")
    assert home.message == note and home.detail["note"] == note


async def test_frontend_csr_note_names_the_fix_on_9_3_and_later(stack_ok: dict, ctx_factory: Any) -> None:
    shell = live_home("csr-shell.html")
    for version, restricted in (("9.4", True), ("10.1", True), ("9.2", False), ("8.5", False)):
        state = deps_state(backend=backend_seen(version))
        result = await frontend.FrontendCollector().collect(
            ctx_factory(all_on(), frontend_handler({"home": shell}), state=state)
        )
        home = by_id(result)["frontend.home"]
        assert home.status == "ok" and home.detail["rendering"] == "csr"
        if restricted:
            assert home.message == (
                f"client-side rendering only: DSpace {version} renders on the server only for the host "
                "in its ui.baseUrl (set dspace.public_url)"
            )
        else:
            assert home.message == frontend.CSR_NOTE
    # with the public host already sent, a shell means SSR is off or failing
    backend_comp = component("backend", "ok")
    backend_comp.metrics = {"dspace_version": "9.4"}
    cfg = all_on({"dspace": {"public_url": "https://repo.example.org"}})
    result = await frontend.FrontendCollector().collect(
        ctx_factory(cfg, frontend_handler({"home": shell}), state=deps_state(backend=backend_comp))
    )
    assert by_id(result)["frontend.home"].message == frontend.CSR_NOTE


# ======================================================= the detail pages' mocked states (tests/e2e)
#
# tests/e2e/test_detail_states.py shows the browser a component changed by a scenario function. Here
# the real collector meets the same situation over a mock transport, and both must say the same.

DEMO_NAME = "Demo Institutional Repository"
DEMO_HOME = (
    "<!DOCTYPE html><html><head><title>Demo Institutional Repository</title>"
    '<meta name="Generator" content="DSpace 9.4"></head>'
    '<body><ds-app ng-version="20.3.30" ng-server-context="ssr"></ds-app></body></html>'
)
SIX_CORES = ("search", "statistics", "oai", "authority", "qaevent", "suggestion")


def demo_root(version: str = "DSpace 9.4", cris: str | None = None) -> dict[str, Any]:
    return {"dspaceVersion": version, "dspaceName": DEMO_NAME, **({"crisVersion": cris} if cris else {})}


def detail_home(
    home: str = DEMO_HOME, public: str = DEMO_HOME, ui_is_public: bool = False
) -> Callable[[httpx.Request], httpx.Response]:
    """The UI on its own address and at the scenarios' public host: a page each, a homepage that
    never answers ("timeout") or a public URL that refuses ("refused")."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == detail_states.PUBLIC_HOST and not ui_is_public:
            if public == "refused":
                raise httpx.ConnectError("Connection refused", request=request)
            return httpx.Response(200, text=public)
        if home == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, text=home)

    return handler


def six_cores(missing: str = "", **failures: str) -> Callable[[httpx.Request], httpx.Response]:
    """Solr with the six DSpace 9 cores: ``missing`` did not load, ``failures`` are its initFailures."""
    names = [name for name in SIX_CORES if name != missing]
    return solr_handler({"status_payload": solr_status(*names, initFailures=failures)})


HEALTHY_BACKEND = {"root": demo_root(), "actuator": LIVE_ACTUATOR["up_with_issues"]}
DETAIL_BACKEND: dict[str, tuple[dict[str, Any], int]] = {
    "seo": ({"root": demo_root("DSpace 9.2"), "actuator": LIVE_ACTUATOR["down"]}, 190),
    "seo-components": (
        {"root": demo_root("DSpace 9.2"), "actuator": detail_states._admin_health("DOWN", seo="DOWN")},
        70,
    ),
    "down": ({"root": demo_root(), "actuator": LIVE_ACTUATOR["down"]}, 30),
    "admin-down": (
        {"root": demo_root(), "actuator": detail_states._admin_health("DOWN", solrSearchCore="DOWN")},
        30,
    ),
    "admin-up": ({"root": demo_root(), "actuator": detail_states._admin_health("UP_WITH_ISSUES")}, 130),
    "hidden": ({"root": demo_root(), "actuator": 404}, 250),
    "http-500": ({"root": demo_root(), "actuator": 500}, 30),
    "rest-down": ({"rest": 503}, 0),
    "7x": ({"root": demo_root("DSpace 7.6.6"), "actuator": "up"}, 0),
    "7x-snapshot-cris": (
        {"root": demo_root("DSpace 7.6.8-SNAPSHOT", "cris-2023.02.07"), "actuator": "up"},
        0,
    ),
    "8-cris": ({"root": demo_root("DSpace 8.2", "cris-2024.02.04"), "actuator": LIVE_ACTUATOR["down"]}, 0),
    "dep-solr-slow": (HEALTHY_BACKEND, 0),
}
"""Scenario → (what the backend answers, how many seconds ago the actuator was first asked: a kept
answer comes back with that age, a warning is asked again and has none; 0 = asked once)."""

DETAIL_SOLR_SEEN: dict[str, Callable[[], ComponentHealth]] = {"dep-solr-slow": detail_states.slow_solr}
"""Scenario → the Solr the backend collector finds in the state (a healthy one when not listed)."""

PUBLIC = detail_states.PUBLIC_URL
DETAIL_FRONTEND: dict[str, tuple[dict[str, str], Callable[[httpx.Request], httpx.Response], str | None]] = {
    "shell": ({}, detail_home(live_home("csr-shell.html")), None),
    "shell-host-rule": ({}, detail_home(live_home("csr-shell.html")), PUBLIC),
    "shell-public-host": (
        {"ui_url": PUBLIC},
        detail_home(live_home("csr-shell.html"), ui_is_public=True),
        PUBLIC,
    ),
    "public-down": ({"public_url": PUBLIC}, detail_home(public="refused"), None),
    "public-ok": ({"public_url": PUBLIC}, detail_home(), None),
    "no-markup": ({}, detail_home(live_home("constructed-bot-check.html")), None),
    "home-down": ({}, detail_home("timeout"), None),
}
"""Scenario → (the dspace settings that differ, what the UI answers, the backend's dspaceUI)."""

DETAIL_SOLR: dict[str, Callable[[httpx.Request], httpx.Response]] = {
    "solr-failed": six_cores("search", search=detail_states.LIB_DISABLED),
    "solr-failed-optional": six_cores("qaevent", qaevent=detail_states.NEW_SEARCHER),
    "solr-reload": six_cores(search=detail_states.RELOAD_FAILED),
}


async def collected(
    collector: Any,
    ctx_factory: Any,
    cfg: Config,
    handler: Callable[[httpx.Request], httpx.Response],
    state: State,
    times: tuple[float, ...] = (NOW,),
) -> dict[str, Any]:
    """The component payload after one collector instance ran at each of ``times``."""
    result = CollectorResult()
    for now in times:
        result = await collector.collect(ctx_factory(cfg, handler, state=state, now=now))
    return health.rollup(collector.id, result.checks, result.metrics, now=times[-1]).to_dict()


def comparable(comp: dict[str, Any]) -> dict[str, Any]:
    """A component payload without what differs from run to run: times and measured latencies."""

    def measured(value: Any) -> Any:
        return None if value is None else "measured"

    rungs = [
        {**{key: r[key] for key in ("id", "label", "status", "message", "value", "detail")}}
        | {"latency_ms": measured(r["latency_ms"])}
        for r in comp["chain"]
    ]
    metrics = {k: measured(v) if k.endswith("latency_ms") else v for k, v in comp["metrics"].items()}
    return {
        "status": comp["status"],
        "reason": comp["reason"],
        "latency_ms": measured(comp["latency_ms"]),
        "chain": rungs,
        "metrics": metrics,
    }


def test_every_detail_page_scenario_has_its_situation() -> None:
    assert set(detail_states.SCENARIOS) == {*DETAIL_BACKEND, *DETAIL_FRONTEND, *DETAIL_SOLR}


def test_detail_page_screenshots_stay_out_of_the_readme_images() -> None:
    """``SENTINEL_E2E_SHOTS=docs/screenshots`` refreshes the README's images from ``test_ui.py``. The
    detail pages' ``detail-*.png`` are evidence for a review: they stay in the git-ignored build tree."""
    code = (
        "import tests.e2e.test_detail_states as states, tests.e2e.test_ui as ui;"
        "print(ui.SCREENSHOT_DIR.relative_to(ui.REPO_DIR).as_posix(),"
        " states.DETAIL_SHOT_DIR.relative_to(ui.REPO_DIR).as_posix())"
    )
    env = {**os.environ, "SENTINEL_E2E_SHOTS": "docs/screenshots"}
    done = subprocess.run(  # noqa: S603 - this interpreter, a fixed script
        [sys.executable, "-c", code],
        cwd=str(detail_states.REPO_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["docs/screenshots", "build/screenshots"]


@pytest.mark.parametrize("name", sorted(detail_states.SCENARIOS))
async def test_detail_page_scenarios_are_what_the_collectors_report(
    stack_ok: dict, ctx_factory: Any, name: str
) -> None:
    """Applied to the healthy component, a scenario gives exactly what the real collector reports in
    that situation: every rung (label, status, message, detail), every metric, the status and the reason."""
    component_id, change = detail_states.SCENARIOS[name]
    cfg = all_on({"dspace": {"backend_service": "tomcat10"}})
    if component_id == "backend":
        opts, asked_ago = DETAIL_BACKEND[name]
        state = deps_state(postgres=component("postgres", "ok"), solr=component("solr", "ok"))
        times = (NOW - asked_ago, NOW) if asked_ago else (NOW,)
        healthy = await collected(
            backend.BackendCollector(), ctx_factory, cfg, backend_handler(dict(HEALTHY_BACKEND)), state
        )
        if name in DETAIL_SOLR_SEEN:
            state = deps_state(postgres=component("postgres", "ok"), solr=DETAIL_SOLR_SEEN[name]())
        real = await collected(
            backend.BackendCollector(), ctx_factory, cfg, backend_handler(dict(opts)), state, times
        )
    elif component_id == "frontend":
        patch, handler, dspace_ui = DETAIL_FRONTEND[name]
        healthy = await collected(
            frontend.FrontendCollector(),
            ctx_factory,
            cfg,
            detail_home(),
            deps_state(backend=backend_seen("9.4", None)),
        )
        state = deps_state(backend=backend_seen("9.4", dspace_ui))
        real = await collected(
            frontend.FrontendCollector(), ctx_factory, merge_patch(cfg, {"dspace": patch}), handler, state
        )
    else:
        state = deps_state(backend=backend_seen("9.4", None))
        healthy = await collected(solr.SolrCollector(), ctx_factory, cfg, six_cores(), state)
        real = await collected(solr.SolrCollector(), ctx_factory, cfg, DETAIL_SOLR[name], state)
    mocked = copy.deepcopy(healthy)
    change(mocked)
    assert comparable(mocked) == comparable(real)


# ================================================================================= nginx


def nginx_handler(opts: dict[str, Any] | None = None) -> Callable[[httpx.Request], httpx.Response]:
    opts = opts or {}

    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"server": "nginx/1.24.0"}
        if request.url.scheme == "https":
            code = opts.get("https", 200)
            return httpx.Response(code, headers=headers, text="ok")
        code = opts.get("http", 200)
        if code == 301:
            return httpx.Response(301, headers={**headers, "location": "https://repo.example.org/"})
        return httpx.Response(code, headers=headers, text="x")

    return handler


async def test_nginx_http_and_https(stack_ok: dict, ctx_factory: Any) -> None:
    result = await nginx.NginxCollector().collect(ctx_factory(all_on(), nginx_handler()))
    assert ids(result) == ["nginx.service", "nginx.http"]
    assert by_id(result)["nginx.http"].label == "HTTP" and by_id(result)["nginx.http"].status == "ok"
    assert result.metrics["server_header"] == "nginx/1.24.0" and result.metrics["https_latency_ms"] is None
    cfg = all_on({"nginx": {"https_url": "https://repo.example.org"}})
    result = await nginx.NginxCollector().collect(ctx_factory(cfg, nginx_handler({"http": 301})))
    assert ids(result) == ["nginx.service", "nginx.http", "nginx.https"]
    rungs = by_id(result)
    assert (
        rungs["nginx.http"].status == "ok"
        and rungs["nginx.http"].message == "HTTP 301 → https://repo.example.org/"
    )
    assert rungs["nginx.https"].label == "HTTPS" and rungs["nginx.https"].status == "ok"
    assert result.metrics["http_latency_ms"] is not None and result.metrics["https_latency_ms"] is not None
    assert stack_ok["units"] == ["nginx", "nginx"]


async def test_nginx_status_classes(stack_ok: dict, ctx_factory: Any) -> None:
    cfg = all_on({"nginx": {"https_url": "https://repo.example.org"}})
    result = await nginx.NginxCollector().collect(ctx_factory(cfg, nginx_handler({"http": 502})))
    rungs = by_id(result)
    assert rungs["nginx.http"].status == "crit" and rungs["nginx.http"].message == "HTTP 502"
    assert rungs["nginx.https"].message == "not reached"
    result = await nginx.NginxCollector().collect(
        ctx_factory(cfg, nginx_handler({"http": 401, "https": 404}))
    )
    rungs = by_id(result)
    assert (
        rungs["nginx.http"].status == "ok" and rungs["nginx.http"].message == "HTTP 401 (access restricted)"
    )
    assert rungs["nginx.https"].status == "warn" and rungs["nginx.https"].message == "HTTP 404"
    result = await nginx.NginxCollector().collect(ctx_factory(all_on()))
    assert by_id(result)["nginx.http"].status == "crit"
    assert by_id(result)["nginx.http"].message == "connection refused (localhost:80)"


async def test_nginx_service_down(stack_ok: dict, ctx_factory: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    set_unit_state(monkeypatch, "failed")
    cfg = all_on({"nginx": {"https_url": "https://repo.example.org"}})
    result = await nginx.NginxCollector().collect(ctx_factory(cfg, nginx_handler()))
    assert statuses(result) == {"nginx.service": "crit", "nginx.http": "unknown", "nginx.https": "unknown"}
    assert by_id(result)["nginx.service"].message == "failed"


NO_WEB_SERVER = (
    "No web server was found on this machine: there is no nginx unit and nothing answers on localhost:80. "
    "If DSpace is opened directly on its own port (for example :4000), switch this check off in More › "
    "Modules. If it runs behind Apache or another unit, set its name and address in More › DSpace connection."
)


async def test_nginx_without_a_web_server_says_how_to_switch_it_off(
    stack_ok: dict, ctx_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No nginx unit and nothing on :80 (DSpace opened directly on :4000): still critical, with a way out."""
    set_unit_state(monkeypatch, "missing")
    cfg = all_on({"nginx": {"https_url": "https://repo.example.org"}})
    result = await nginx.NginxCollector().collect(ctx_factory(cfg))
    rungs = by_id(result)
    assert (
        rungs["nginx.service"].message == "unit nginx not found"
        and "hint" not in rungs["nginx.service"].detail
    )
    http = rungs["nginx.http"]
    assert (http.status, http.message) == ("crit", "connection refused (localhost:80)")
    assert http.detail["hint"] == NO_WEB_SERVER
    assert rungs["nginx.https"].message == "not reached" and "hint" not in rungs["nginx.https"].detail
    comp = health.rollup("nginx", result.checks, result.metrics, now=NOW)
    assert (comp.status, comp.reason) == ("crit", "connection refused (localhost:80)")
    # another unit name and address are named as they are set
    cfg = all_on({"nginx": {"service": "apache2", "http_url": "http://127.0.0.1:8081/"}})
    result = await nginx.NginxCollector().collect(ctx_factory(cfg))
    named = NO_WEB_SERVER.replace("no nginx unit", "no apache2 unit").replace(
        "localhost:80", "127.0.0.1:8081"
    )
    assert by_id(result)["nginx.http"].detail["hint"] == named


async def test_nginx_hint_needs_a_missing_unit_and_a_refused_connection(
    stack_ok: dict, ctx_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the unit is there: nginx itself is down, no hint
    result = await nginx.NginxCollector().collect(ctx_factory(all_on()))
    assert by_id(result)["nginx.http"].status == "crit" and "hint" not in by_id(result)["nginx.http"].detail
    # something answers on :80, only badly: a web server exists
    set_unit_state(monkeypatch, "missing")
    result = await nginx.NginxCollector().collect(ctx_factory(all_on(), nginx_handler({"http": 502})))
    assert (
        by_id(result)["nginx.http"].message == "HTTP 502" and "hint" not in by_id(result)["nginx.http"].detail
    )
    # no unit configured, or no systemctl on this machine: nothing is known about the unit
    for patch in ({"nginx": {"service": ""}}, {}):
        if not patch:
            monkeypatch.setattr(services.systemd, "systemctl_path", lambda: None)
        result = await nginx.NginxCollector().collect(ctx_factory(all_on(patch)))
        http = by_id(result)["nginx.http"]
        assert http.message == "connection refused (localhost:80)" and "hint" not in http.detail


# =================================================================================== ssl


def test_parse_der_certificate_fixture() -> None:
    info = ssl_mod.parse_der_certificate(FIXTURE_DER)
    assert info["subject"] == {"C": "LK", "O": "Test Repository", "CN": "repo.example.org"}
    assert info["issuer"]["CN"] == "repo.example.org"
    assert info["sans"] == ["repo.example.org", "www.repo.example.org", "127.0.0.1"]
    expected_after = _dt.datetime(2036, 9, 21, 10, 55, 40, tzinfo=_dt.timezone.utc).timestamp()
    expected_before = _dt.datetime(2026, 9, 24, 10, 55, 40, tzinfo=_dt.timezone.utc).timestamp()
    assert info["not_after"] == expected_after and info["not_before"] == expected_before
    with pytest.raises(ValueError):
        ssl_mod.parse_der_certificate(b"\x02\x01\x01")
    assert ssl_mod.format_name(info["issuer"]) == "repo.example.org (Test Repository)"


def test_parse_peercert_dict() -> None:
    cert = {
        "subject": ((("commonName", "repo.example.org"),),),
        "issuer": (
            (("countryName", "US"),),
            (("organizationName", "Let's Encrypt"),),
            (("commonName", "R11"),),
        ),
        "notBefore": "Jul 26 00:00:00 2026 GMT",
        "notAfter": "Oct 24 23:59:59 2026 GMT",
        "subjectAltName": (
            ("DNS", "repo.example.org"),
            ("DNS", "www.repo.example.org"),
            ("IP Address", "10.0.0.1"),
        ),
    }
    info = ssl_mod.parse_peercert(cert)
    assert ssl_mod.format_name(info["issuer"]) == "R11 (Let's Encrypt)"
    assert ssl_mod.format_name(info["subject"]) == "repo.example.org"
    assert info["sans"] == ["repo.example.org", "www.repo.example.org", "10.0.0.1"]
    assert info["not_after"] == _dt.datetime(2026, 10, 24, 23, 59, 59, tzinfo=_dt.timezone.utc).timestamp()


def fake_cert(
    monkeypatch: pytest.MonkeyPatch,
    days: float,
    verify_error: str | None = None,
    error: BaseException | None = None,
) -> list[tuple]:
    calls: list[tuple] = []

    def _fetch(host: str, port: int = 443, timeout: float = 5.0, verify: bool = True) -> ssl_mod.CertInfo:
        calls.append((host, port, timeout, verify))
        if error is not None:
            raise error
        return ssl_mod.CertInfo(
            host=host,
            port=port,
            subject={"CN": "repo.example.org"},
            issuer={"CN": "R11", "O": "Let's Encrypt"},
            not_before=NOW - 30 * 86400,
            not_after=NOW + days * 86400,
            sans=["repo.example.org", "www.repo.example.org"],
            verified=verify and verify_error is None,
            verify_error=verify_error,
            tls_version="TLSv1.3",
            latency_ms=38.2,
        )

    monkeypatch.setattr(ssl_mod, "fetch_certificate", _fetch)
    return calls


async def test_ssl_ok_from_public_url(ctx_factory: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_cert(monkeypatch, 61)
    cfg = all_on({"dspace": {"public_url": "https://repo.example.org/"}})
    result = await ssl_mod.SslCollector().collect(ctx_factory(cfg))
    assert ids(result) == ["ssl.cert"]
    rung = by_id(result)["ssl.cert"]
    assert rung.label == "Certificate" and rung.status == "ok" and rung.message == "expires in 61 days"
    assert rung.value == 61.0 and rung.latency_ms == 38.2
    assert calls == [("repo.example.org", 443, 5.0, True)]
    m = result.metrics
    assert (
        m["days_left"] == 61.0 and m["issuer"] == "R11 (Let's Encrypt)" and m["subject"] == "repo.example.org"
    )
    assert m["sans"] == ["repo.example.org", "www.repo.example.org"] and m["verified"] is True
    assert m["not_after"] == _dt.datetime.fromtimestamp(NOW + 61 * 86400, tz=_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


async def test_ssl_thresholds(ctx_factory: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = all_on({"ssl": {"host": "repo.example.org", "port": 8443}})
    calls = fake_cert(monkeypatch, 20)
    result = await ssl_mod.SslCollector().collect(ctx_factory(cfg))
    assert by_id(result)["ssl.cert"].status == "warn" and calls[0][:2] == ("repo.example.org", 8443)
    fake_cert(monkeypatch, 5)
    result = await ssl_mod.SslCollector().collect(ctx_factory(cfg))
    assert (
        by_id(result)["ssl.cert"].status == "crit"
        and by_id(result)["ssl.cert"].message == "expires in 5 days"
    )
    fake_cert(monkeypatch, -3)
    result = await ssl_mod.SslCollector().collect(ctx_factory(cfg))
    assert (
        by_id(result)["ssl.cert"].status == "crit"
        and by_id(result)["ssl.cert"].message == "expired 3 days ago"
    )


async def test_ssl_verification_errors(ctx_factory: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = all_on({"ssl": {"host": "repo.example.org"}})
    fake_cert(monkeypatch, 200, verify_error="self-signed certificate")
    result = await ssl_mod.SslCollector().collect(ctx_factory(cfg))
    rung = by_id(result)["ssl.cert"]
    assert rung.status == "crit" and rung.message == "self-signed certificate · expires in 200 days"
    cfg = all_on({"ssl": {"host": "repo.example.org"}, "dspace": {"verify_tls": False}})
    calls = fake_cert(monkeypatch, 200, verify_error="self-signed certificate")
    result = await ssl_mod.SslCollector().collect(ctx_factory(cfg))
    rung = by_id(result)["ssl.cert"]
    assert (
        rung.status == "ok" and rung.message == "expires in 200 days (not verified: self-signed certificate)"
    )
    assert calls[0][3] is False


async def test_ssl_no_host_and_fetch_errors(ctx_factory: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    result = await ssl_mod.SslCollector().collect(ctx_factory(all_on()))
    rung = by_id(result)["ssl.cert"]
    assert (
        rung.status == "unknown" and rung.message == "no host configured (set ssl.host or dspace.public_url)"
    )
    cfg = all_on({"ssl": {"host": "repo.example.org"}})
    fake_cert(monkeypatch, 0, error=ConnectionRefusedError(111, "Connection refused"))
    result = await ssl_mod.SslCollector().collect(ctx_factory(cfg))
    assert by_id(result)["ssl.cert"].status == "crit"
    assert by_id(result)["ssl.cert"].message == "connection refused (repo.example.org:443)"
    fake_cert(monkeypatch, 0, error=TimeoutError("timed out"))
    result = await ssl_mod.SslCollector().collect(ctx_factory(cfg))
    assert by_id(result)["ssl.cert"].message == "timed out after 5 s (repo.example.org:443)"
    fake_cert(monkeypatch, 0, error=socket.gaierror(-2, "Name or service not known"))
    result = await ssl_mod.SslCollector().collect(ctx_factory(cfg))
    assert by_id(result)["ssl.cert"].message == "DNS lookup failed (repo.example.org)"
    assert result.metrics["days_left"] is None


# ================================================================================= ports

SOCKETS = [
    ListeningSocket(22, "0.0.0.0", True, "ipv4", 1, "sshd"),
    ListeningSocket(22, "::", True, "ipv6", 1, "sshd"),
    ListeningSocket(4000, "127.0.0.1", False, "ipv4", 2, "node"),
    ListeningSocket(8983, "0.0.0.0", True, "ipv4", 3, "java"),
    ListeningSocket(5432, "127.0.0.1", False, "ipv4", 4, "postgres"),
    ListeningSocket(5432, "::1", False, "ipv6", 4, "postgres"),
    ListeningSocket(3306, "0.0.0.0", True, "ipv4", 5, "mysqld"),
]


def install_sockets(monkeypatch: pytest.MonkeyPatch, sockets: list[ListeningSocket]) -> None:
    monkeypatch.setattr(ports, "listening_sockets", lambda with_process=False: list(sockets))


def missing_file(path: str) -> str:
    raise FileNotFoundError(path)


async def test_ports_rules(ctx_factory: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    install_sockets(monkeypatch, SOCKETS)
    cfg = all_on(
        {
            "ports": [
                {"port": 22, "label": "SSH", "expect": "open"},
                {"port": 80, "label": "HTTP", "expect": "open"},
                {"port": 4000, "label": "Angular", "expect": "open"},
                {"port": 8983, "label": "Solr", "expect": "local_only"},
                {"port": 5432, "label": "PostgreSQL", "expect": "local_only"},
                {"port": 6379, "label": "Redis", "expect": "local_only"},
            ]
        }
    )
    result = await ports.PortsCollector().collect(ctx_factory(cfg))
    assert ids(result) == ["ports.22", "ports.80", "ports.4000", "ports.8983", "ports.5432", "ports.6379"]
    rungs = by_id(result)
    assert rungs["ports.22"].label == "SSH :22" and rungs["ports.22"].status == "ok"
    assert rungs["ports.22"].message == "listening publicly"
    assert rungs["ports.80"].status == "warn" and rungs["ports.80"].message == "not listening"
    assert rungs["ports.4000"].status == "ok" and rungs["ports.4000"].message == "listening on localhost only"
    assert (
        rungs["ports.8983"].status == "warn" and rungs["ports.8983"].message == "exposed publicly (0.0.0.0)"
    )
    assert rungs["ports.5432"].status == "ok" and rungs["ports.5432"].message == "listening on localhost only"
    assert rungs["ports.6379"].status == "unknown" and rungs["ports.6379"].message == "not listening"
    m = result.metrics
    assert {"port": 8983, "addr": "0.0.0.0", "public": True, "process": "java"} in m["listening"]
    assert all(entry["port"] != 3306 for entry in m["listening"])
    assert m["other_public"] == [3306] and m["source"] == "sockets"
    comp = health.rollup("ports", result.checks, result.metrics, now=NOW)
    assert comp.status == "warn" and comp.reason == "not listening"


FIREWALL = "(Sentinel sees where it listens, not your firewall)"
EXPOSED_HINTS = {
    8983: (
        "Solr has no login: anyone who can reach this port can read and change the search index. Make Solr "
        f"listen on localhost only, or block the port in the firewall {FIREWALL}. To accept it, change this "
        'rule to "open" in config/sentinel.yaml (ports: port 8983, expect: open) and restart Sentinel.'
    ),
    5432: (
        "PostgreSQL holds DSpace's database: anyone who can reach this port can try to log in to it. Make "
        "PostgreSQL listen on localhost only (listen_addresses in postgresql.conf), or block the port in the "
        f'firewall {FIREWALL}. To accept it, change this rule to "open" in config/sentinel.yaml (ports: port '
        "5432, expect: open) and restart Sentinel."
    ),
    6379: (
        "This port should only be reachable from this machine: anyone who can reach it can connect to the "
        "service behind it. Make that service listen on localhost only, or block the port in the firewall "
        f'{FIREWALL}. To accept it, change this rule to "open" in config/sentinel.yaml (ports: port 6379, '
        "expect: open) and restart Sentinel."
    ),
}


async def test_ports_exposed_local_port_says_why_and_what_to_do(
    ctx_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Solr on every interface (its default before 9) keeps its warning and its words, and says why it
    matters, how to close it, and how to accept it; so do PostgreSQL and any other local-only port."""
    install_sockets(
        monkeypatch,
        [
            ListeningSocket(8983, "::", True, "ipv6", 3, "java"),
            ListeningSocket(5432, "0.0.0.0", True, "ipv4", 4, "postgres"),
            ListeningSocket(6379, "192.0.2.10", True, "ipv4", 5, "redis-server"),
            ListeningSocket(9765, "127.0.0.1", False, "ipv4", 6, "python3"),
        ],
    )
    rules = [
        {"port": 8983, "label": "Solr", "expect": "local_only"},
        {"port": 5432, "label": "PostgreSQL", "expect": "local_only"},
        {"port": 6379, "label": "Redis", "expect": "local_only"},
        {"port": 9765, "label": "Sentinel", "expect": "local_only"},
        {"port": 22, "label": "SSH", "expect": "open"},
    ]
    result = await ports.PortsCollector().collect(ctx_factory(all_on({"ports": rules})))
    rungs = by_id(result)
    assert (rungs["ports.8983"].status, rungs["ports.8983"].message) == ("warn", "exposed publicly (::)")
    assert rungs["ports.5432"].message == "exposed publicly (0.0.0.0)"
    assert rungs["ports.6379"].message == "exposed publicly (192.0.2.10)"
    for port, hint in EXPOSED_HINTS.items():
        assert rungs[f"ports.{port}"].detail["hint"] == hint, port
    # a port that stays local, or one that should be open, needs no advice
    assert rungs["ports.9765"].status == "ok" and "hint" not in rungs["ports.9765"].detail
    assert rungs["ports.22"].status == "warn" and "hint" not in rungs["ports.22"].detail


def test_ports_hint_knows_solr_and_postgresql_by_label_or_port() -> None:
    cfg = all_on({"solr": {"url": "http://localhost:8984/solr"}, "postgres": {"port": 5433}})
    assert (
        ports.exposed_service(8984, "", cfg) == "solr"
        and ports.exposed_service(5433, "DB", cfg) == "postgres"
    )
    assert ports.exposed_service(9999, "Solr replica", cfg) == "solr"
    assert ports.exposed_service(9998, "postgres-standby", cfg) == "postgres"
    assert ports.exposed_service(8983, "", cfg) == "other"  # Solr moved to 8984
    assert (
        ports.exposed_service(8983, "", None) == "solr"
        and ports.exposed_service(5432, "", None) == "postgres"
    )
    assert ports.exposed_hint(5433, "DB", cfg).endswith(
        "(ports: port 5433, expect: open) and restart Sentinel."
    )


async def test_ports_fallback_without_socket_table(ctx_factory: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    install_sockets(monkeypatch, [])

    async def _tcp(host: str, port: int, timeout: float = 2.0) -> TcpResult:
        return TcpResult(
            port == 22, 0.2 if port == 22 else None, None if port == 22 else "connection refused", host, port
        )

    monkeypatch.setattr(ports, "tcp_connect", _tcp)
    cfg = all_on(
        {
            "ports": [
                {"port": 22, "label": "SSH", "expect": "open"},
                {"port": 80, "label": "HTTP", "expect": "open"},
                {"port": 5432, "label": "PostgreSQL", "expect": "local_only"},
            ]
        }
    )
    result = await ports.PortsCollector().collect(ctx_factory(cfg))
    rungs = by_id(result)
    assert (
        rungs["ports.22"].status == "ok"
        and rungs["ports.22"].message == "reachable on localhost (bind address unknown)"
    )
    assert rungs["ports.80"].status == "warn" and rungs["ports.5432"].status == "unknown"
    assert result.metrics["source"] == "tcp" and result.metrics["listening"] == [
        {"port": 22, "addr": "127.0.0.1", "public": None}
    ]


# ============================================================================ repository


def repo_rest_handler(opts: dict[str, Any] | None = None) -> Callable[[httpx.Request], httpx.Response]:
    opts = opts or {}
    totals = {"ITEM": 27_491, "COMMUNITY": 14, "COLLECTION": 96}

    def handler(request: httpx.Request) -> httpx.Response:
        if opts.get("fail"):
            return httpx.Response(502, text="Bad Gateway")
        assert request.url.path == "/server/api/discover/search/objects"
        assert request.url.params["size"] == "1"
        dso = request.url.params["dsoType"]
        return httpx.Response(
            200, json={"_embedded": {"searchResult": {"page": {"totalElements": totals[dso]}}}}
        )

    return handler


async def test_repository_counts_from_database(ctx_factory: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    record = install_fake_pg(monkeypatch, repo_ok_script())
    result = await repository.RepositoryCollector().collect(ctx_factory(all_on()))
    assert result.checks == []
    m = result.metrics
    assert m["items"] == 27_491 and m["bitstreams"] == 81_337 and m["assetstore_bytes"] == 442_381_631_488
    assert (
        m["communities"] == 14
        and m["collections"] == 96
        and m["eperson_count"] == 1_842
        and m["workflow_items"] == 7
    )
    assert m["db_size"] == 3_328_599_654 and m["estimated"] is False and m["source"] == "db"
    assert result.repository_stats == m
    assert record["conns"][0].closed is True
    kw = record["kwargs"][0]
    assert "default_transaction_read_only=on" in kw["options"] and kw["autocommit"] is True
    comp = health.rollup("repository", result.checks, result.metrics, now=NOW)
    assert comp.status == "ok"


async def test_repository_timeout_falls_back_to_estimates(
    ctx_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = repo_ok_script()
    sql = {key: q for key, _t, q in repository.COUNT_QUERIES}
    script[sql["items"]] = psycopg.errors.QueryCanceled("canceling statement due to statement timeout")
    script[sql["bitstreams"]] = psycopg.errors.QueryCanceled("canceling statement due to statement timeout")
    script[repository.ESTIMATE_SQL] = [("item", 27_500), ("bitstream", 81_400)]
    record = install_fake_pg(monkeypatch, script)
    result = await repository.RepositoryCollector().collect(ctx_factory(all_on()))
    m = result.metrics
    assert m["items"] == 27_500 and m["bitstreams"] == 81_400 and m["assetstore_bytes"] is None
    assert m["estimated"] is True and m["estimated_tables"] == ["item", "bitstream"]
    assert m["communities"] == 14
    estimate_calls = [p for s, p in record["conns"][0].executed if s == repository.ESTIMATE_SQL]
    assert estimate_calls == [(["item", "bitstream"],)]


async def test_repository_missing_workflow_table(ctx_factory: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    script = repo_ok_script()
    sql = {key: q for key, _t, q in repository.COUNT_QUERIES}
    script[sql["workflow_items"]] = psycopg.errors.UndefinedTable(
        'relation "cwf_workflowitem" does not exist'
    )
    install_fake_pg(monkeypatch, script)
    result = await repository.RepositoryCollector().collect(ctx_factory(all_on()))
    assert result.metrics["workflow_items"] is None and result.metrics["items"] == 27_491
    assert "problems" not in result.metrics


async def test_repository_rest_fallback(ctx_factory: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_pg(monkeypatch, error=psycopg.OperationalError("connection refused"))
    result = await repository.RepositoryCollector().collect(ctx_factory(all_on(), repo_rest_handler()))
    assert result.checks == []
    m = result.metrics
    assert m["items"] == 27_491 and m["communities"] == 14 and m["collections"] == 96
    assert m["bitstreams"] is None and m["db_size"] is None and m["source"] == "rest"
    assert m["db_error"] == "connection refused (localhost:5432)"
    assert result.repository_stats["items"] == 27_491
    assert health.rollup("repository", result.checks, result.metrics, now=NOW).status == "ok"


async def test_repository_no_source_is_unknown(ctx_factory: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_pg(monkeypatch, error=psycopg.OperationalError("connection refused"))
    result = await repository.RepositoryCollector().collect(
        ctx_factory(all_on(), repo_rest_handler({"fail": True}))
    )
    assert ids(result) == ["repository.stats"]
    rung = by_id(result)["repository.stats"]
    assert rung.status == "unknown" and rung.label == "Repository counts"
    assert (
        rung.message
        == "database: connection refused (localhost:5432); REST: HTTP 502 (discover/search/objects)"
    )
    assert result.metrics == {} and result.repository_stats is None
    assert health.rollup("repository", result.checks, result.metrics, now=NOW).status == "unknown"


# ============================================================================ assetstore

LOW_FREE = {"alerts": {"assetstore_free_gb": {"warning": 0.001, "critical": 0.0001}}}


async def test_assetstore_ok_and_history(ctx_factory: Any, tmp_path: Path) -> None:
    store = Store(":memory:")
    cfg = all_on({"dspace": {"assetstore_dir": str(tmp_path)}, **LOW_FREE})
    ctx = ctx_factory(cfg, store=store)
    result = await assetstore.AssetstoreCollector().collect(ctx)
    assert ids(result) == ["assetstore.path"]
    rung = by_id(result)["assetstore.path"]
    assert rung.label == "Assetstore readable" and rung.status == "ok" and rung.message == ""
    m = result.metrics
    assert m["path"] == str(tmp_path) and m["total"] > 0 and m["free"] >= 0 and 0 <= m["pct"] <= 100
    assert m["mount"] is not None and Path(m["mount"]).is_absolute()
    assert m["growth_bytes_per_day"] is None and m["history_points"] == 1
    # a second run within the hour does not add a point; older points grow the series
    result = await assetstore.AssetstoreCollector().collect(ctx)
    assert result.metrics["history_points"] == 1
    used = m["used"]
    store.kv_set(
        assetstore.HISTORY_KEY,
        str([[NOW - 2 * 86400, used - 2 * GIB], [NOW - 86400, used - GIB]]).replace("'", '"'),
    )
    result = await assetstore.AssetstoreCollector().collect(ctx)
    assert result.metrics["history_points"] == 3
    assert abs(result.metrics["growth_bytes_per_day"] - GIB) < GIB * 0.01
    assert result.metrics["growth_source"] == "mount"
    assert result.metrics["days_until_full"] == round(
        result.metrics["free"] / result.metrics["growth_bytes_per_day"], 1
    )
    store.close()


async def test_assetstore_growth_fallback_from_repository_stats(ctx_factory: Any, tmp_path: Path) -> None:
    store = Store(":memory:")
    store.insert_repository_stats(NOW - 4 * 86400, {"items": 1, "assetstore_bytes": 100 * GIB})
    store.insert_repository_stats(NOW - 1 * 86400, {"items": 2, "assetstore_bytes": 106 * GIB})
    cfg = all_on({"dspace": {"assetstore_dir": str(tmp_path)}, **LOW_FREE})
    result = await assetstore.AssetstoreCollector().collect(ctx_factory(cfg, store=store))
    assert result.metrics["growth_bytes_per_day"] == 2 * GIB
    assert result.metrics["growth_source"] == "repository_stats"
    store.close()


async def test_assetstore_missing_and_low_space(
    ctx_factory: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = all_on({"dspace": {"assetstore_dir": str(tmp_path / "nope")}})
    result = await assetstore.AssetstoreCollector().collect(ctx_factory(cfg))
    rung = by_id(result)["assetstore.path"]
    assert rung.status == "crit" and rung.message == f"directory not found ({tmp_path / 'nope'})"
    assert result.metrics["total"] is None
    monkeypatch.setattr(
        assetstore,
        "disk_usage",
        lambda path: {"total": 1000 * GIB, "used": 991 * GIB, "free": 9 * GIB, "pct": 99.1},
    )
    cfg = all_on({"dspace": {"assetstore_dir": str(tmp_path)}})
    result = await assetstore.AssetstoreCollector().collect(ctx_factory(cfg))
    rung = by_id(result)["assetstore.path"]
    assert rung.status == "crit" and rung.message == "only 9.0 GB free" and rung.value == 9.0
    monkeypatch.setattr(
        assetstore,
        "disk_usage",
        lambda path: {"total": 1000 * GIB, "used": 960 * GIB, "free": 40 * GIB, "pct": 96.0},
    )
    result = await assetstore.AssetstoreCollector().collect(ctx_factory(cfg))
    assert by_id(result)["assetstore.path"].status == "warn"


async def test_assetstore_on_a_hung_mount_reports_it_without_piling_up_threads(
    ctx_factory: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """statvfs on a dead hard-mounted NFS share never returns: one stuck thread, a crit rung."""
    release = threading.Event()
    calls: list[str] = []

    def hung_inspect(path: str) -> dict[str, Any]:
        calls.append(path)
        release.wait(5)
        return {"exists": True, "readable": True, "usage": None, "mount": None, "error": "stale"}

    monkeypatch.setattr(assetstore, "inspect_path", hung_inspect)
    monkeypatch.setattr(assetstore, "PROBE_TIMEOUT_S", 0.2)
    cfg = all_on({"dspace": {"assetstore_dir": str(tmp_path / "hung-assetstore")}})
    try:
        first = await assetstore.AssetstoreCollector().collect(ctx_factory(cfg))
        second = await assetstore.AssetstoreCollector().collect(ctx_factory(cfg))
    finally:
        release.set()
    for result in (first, second):
        rung = by_id(result)["assetstore.path"]
        assert rung.status == "crit"
        assert rung.message == f"filesystem not responding ({tmp_path / 'hung-assetstore'})"
    assert calls == [str(tmp_path / "hung-assetstore")]  # the second cycle did not start another


# ============================================================================ system


async def test_system_collector_does_not_block_on_a_hung_assetstore_mount(
    ctx_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A statfs stuck on the assetstore mount leaves the loop free and the mount out of the sample."""
    release = threading.Event()
    calls: list[str] = []
    real_disk_usage = psutil.disk_usage

    def slow_disk_usage(path: str) -> Any:
        if path != "/":
            calls.append(path)
            release.wait(3)
        return real_disk_usage("/")

    monkeypatch.setattr(system.psutil, "disk_usage", slow_disk_usage)
    monkeypatch.setattr(system, "mount_point", lambda path: "/mnt/nfs")
    monkeypatch.setattr(system, "MOUNT_PROBE_TIMEOUT_S", 0.2)
    cfg = merge_patch(Config(), {"dspace": {"assetstore_dir": "/mnt/nfs/assetstore"}})
    col = system.SystemCollector()
    stalls: list[float] = []

    async def ticker() -> None:
        last = time.perf_counter()
        while True:
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            stalls.append(now - last)
            last = now

    tick = asyncio.create_task(ticker())
    try:
        started = time.perf_counter()
        first = await col.collect(ctx_factory(cfg))
        second = await col.collect(ctx_factory(cfg))
        elapsed = time.perf_counter() - started
        assert calls == ["/mnt/nfs"]  # one stuck statfs, not one more per cycle
    finally:
        release.set()
        tick.cancel()
    assert max(stalls) < 0.15, max(stalls)
    assert elapsed < 1.5
    for result in (first, second):
        assert result.sample is not None and result.sample.disks == []
        assert result.sample.disk_path == "/" and result.sample.disk_total > 0
    for _ in range(100):  # the stuck call returns: the next cycle measures the mount again
        third = await col.collect(ctx_factory(cfg))
        if third.sample is not None and third.sample.disks:
            break
        await asyncio.sleep(0.02)
    assert [d["path"] for d in third.sample.disks] == ["/mnt/nfs"]


async def test_system_sample_stuck_on_root_is_not_restarted_every_cycle(
    ctx_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()
    calls: list[str] = []

    def hung_disk_usage(path: str) -> Any:
        calls.append(path)
        release.wait(3)
        raise OSError("stale file handle")

    monkeypatch.setattr(system.psutil, "disk_usage", hung_disk_usage)
    cfg = merge_patch(Config(), {"dspace": {"assetstore_dir": ""}})
    col = system.SystemCollector()
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(col.collect(ctx_factory(cfg)), timeout=0.2)  # the scheduler's timeout
        with pytest.raises(ProbeStalled):
            await asyncio.wait_for(col.collect(ctx_factory(cfg)), timeout=0.2)
    finally:
        release.set()
        base_mod._probes["system.sample"].done.wait(5)  # later tests sample the host for real
    assert calls == ["/"]


def test_assetstore_growth_math() -> None:
    assert assetstore.growth_per_day([]) is None
    assert assetstore.growth_per_day([[0, 10]]) is None
    assert assetstore.growth_per_day([[0, 0], [1800, 5]]) is None  # spans less than an hour
    assert assetstore.growth_per_day([[0, 0], [86400, 1000]]) == 1000
    series = [[i * 3600, 100 + i * 50] for i in range(25)]  # +50 per hour, linear
    assert abs(assetstore.growth_per_day(series) - 1200) < 1e-6
    assert assetstore.growth_per_day([[0, 1], [0, 2], [0, 3]]) is None


# =========================================================================== registry


def test_registry_resolves_every_collector() -> None:
    cols = enabled_collectors(all_on())
    assert [c.id for c in cols] == list(COLLECTOR_ORDER)
    for col in cols:
        meta = health.COMPONENTS[col.id]
        assert (col.label, col.emoji) == (meta.label, meta.emoji)
        assert col.interval_key in Config().collector.model_dump()
    assert [c.id for c in enabled_collectors(Config())] == ["server"]
    only_pg = enabled_collectors(merge_patch(Config(), {"modules": {"postgres": True}}))
    assert [c.id for c in only_pg] == ["server", "postgres"]


async def test_scheduler_runs_real_collectors_without_exceptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All modules on, non-demo, nothing answers: statuses are crit/unknown, but no collector raises.

    Hermetic: TCP connects are refused and the HTTP client refuses every request, so the test
    never reaches whatever runs on this machine's ports (8080, 8983, 4000, 80, 5432).
    """
    monkeypatch.setattr(services.systemd, "systemctl_path", lambda: None)
    install_fake_pg(monkeypatch, error=psycopg.OperationalError("connection refused"))
    install_sockets(monkeypatch, SOCKETS)
    set_tcp_refused(monkeypatch)
    monkeypatch.setattr(ports, "tcp_connect", services.tcp_connect)
    # the Security collector reads nothing of this machine either: the same sockets, no files
    monkeypatch.setattr(security.HostSources, "sockets", lambda self: list(SOCKETS))
    monkeypatch.setattr(security.HostSources, "read_text", lambda self, path: missing_file(path))
    monkeypatch.setattr(security.HostSources, "stat", lambda self, path: None)
    monkeypatch.setattr(security.HostSources, "exists", lambda self, path: False)
    monkeypatch.setattr(security, "BASELINE", security.PortBaseline())
    asked: list[str] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        raise httpx.ConnectError("Connection refused", request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    cfg = all_on(
        {
            "dspace": {"assetstore_dir": str(tmp_path / "assetstore"), "public_url": ""},
            "ssl": {"host": ""},
            "logs": {
                "files": [
                    {
                        "id": "dspace",
                        "label": "DSpace",
                        "path": str(tmp_path / "dspace.log"),
                        "daily_suffix": True,
                    }
                ]
            },
        }
    )
    store = Store(tmp_path / "s.db")
    state = State()
    scheduler = Scheduler(cfg, store, state, Bus(), http=http)
    try:
        await asyncio.wait_for(scheduler.run_all_now(), timeout=60)
    finally:
        await scheduler.stop()
        await http.aclose()
        store.close()
    assert set(state.components) == set(COLLECTOR_ORDER)
    for comp in state.components.values():
        for rung in comp.chain:
            assert not rung.id.endswith(".error"), (comp.id, rung.message)
            if rung.status != "ok":
                assert rung.message, (comp.id, rung.id)
        assert comp.checked_at is not None
    assert state.components["postgres"].status == "crit"
    assert state.components["repository"].status == "unknown"
    assert state.components["ssl"].status == "unknown"
    assert state.components["assetstore"].status == "crit"
    assert state.components["logs"].status == "unknown"
    assert state.components["backend"].chain[-2].id == "backend.dep.postgres"
    # 3306 listens on every interface and no rule expects it; the unreadable rest is informational
    sec = state.components["security"]
    assert sec.status == "warn" and sec.reason.endswith("1 unexpected: 3306 (mysqld)")
    assert [r.id for r in sec.chain][0] == "security.ports" and len(sec.chain) == 8
