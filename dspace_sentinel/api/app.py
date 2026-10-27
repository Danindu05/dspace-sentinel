"""FastAPI application factory: ``create_app(cfg)``.

The lifespan starts and stops the scheduler and writes the local CLI token; an ASGI middleware
checks the ``Host`` header against an allowlist (DNS rebinding), asks for the dashboard login
(``server.login``: a session cookie) or, without a login, for HTTP Basic auth (password =
``server.auth_token``), refuses cross-site writes and sets cache headers for static files.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import signal
import socket
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import cookie_parser
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from dspace_sentinel import __version__, paths
from dspace_sentinel.alerts.engine import AlertEngine
from dspace_sentinel.alerts.incidents import IncidentEngine
from dspace_sentinel.api import (
    routes_alerts,
    routes_auth,
    routes_config,
    routes_history,
    routes_reports,
    routes_status,
    stream,
)
from dspace_sentinel.api.deps import SingleFlight
from dspace_sentinel.api.routes_auth import LOGIN_REQUIRED, AuthService, authorization, token_matches
from dspace_sentinel.auth import SESSION_COOKIE, login_active
from dspace_sentinel.bus import Bus
from dspace_sentinel.config import Config, load_config, startup_notices
from dspace_sentinel.scheduler import Scheduler
from dspace_sentinel.state import State
from dspace_sentinel.store import Store

log = logging.getLogger(__name__)

REALM = "DSpace Sentinel"
STATIC_CACHE = "public, max-age=3600"
HTML_CACHE = "no-cache"
HEALTH_PATH = "/healthz"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
CROSS_SITE_FETCH = frozenset({"cross-site", "same-site"})
AUTH_PATHS = frozenset({"/api/session", "/api/login"})
"""Always open: the page asks ``/api/session`` first and signs in with ``/api/login``."""
OPEN_PATHS = frozenset({"", "/", "/index.html", "/favicon.svg", *AUTH_PATHS})
"""Open while a login is set (with ``/static/**`` and ``/healthz``): the page loads, then asks
``/api/session``."""


def is_open_path(local_path: str) -> bool:
    """True for the paths a login leaves open (``/healthz`` is exempt from every check anyway)."""
    return local_path in OPEN_PATHS or local_path.startswith("/static/")


SHUTDOWN_SIGNALS: tuple[signal.Signals, ...] = (signal.SIGTERM, signal.SIGINT)


def chain_shutdown_signals(event: threading.Event) -> Callable[[], None]:
    """Set ``event`` on SIGTERM/SIGINT, then run the handler installed before (uvicorn's own).

    uvicorn stops only once every response has finished; the SSE stream watches ``event`` and
    ends, so ``stop``/``restart`` neither wait for the graceful-shutdown timeout nor end in
    SIGKILL (which skipped the scheduler stop and the WAL checkpoint). Nothing is installed off
    the main thread (signals cannot be handled there) or when there is no Python handler to
    chain to. Returns a function that puts the previous handlers back.
    """
    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    installed: dict[signal.Signals, tuple[Callable[..., Any], Callable[..., Any]]] = {}
    for sig in SHUTDOWN_SIGNALS:
        previous = signal.getsignal(sig)
        if not callable(previous):
            continue

        def handler(signum: int, frame: Any, _previous: Callable[..., Any] = previous) -> None:
            event.set()
            _previous(signum, frame)

        signal.signal(sig, handler)
        installed[sig] = (previous, handler)

    def restore() -> None:
        for sig, (previous, handler) in installed.items():
            if signal.getsignal(sig) is handler:
                signal.signal(sig, previous)

    return restore


class UnsafeBindError(RuntimeError):
    """Refusal to serve the read/write API unauthenticated on a non-loopback address."""


def is_loopback_host(host: str) -> bool:
    """True for ``localhost`` and loopback IP addresses (``127.0.0.0/8``, ``::1``)."""
    name = host.strip().strip("[]").lower()
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def check_bind(cfg: Config) -> None:
    """Raise :class:`UnsafeBindError` when ``server.host`` is not loopback and nothing protects the API.

    Allowed: a loopback host, the dashboard login (``server.login``; not in the demo, which never
    asks for it), a non-empty ``server.auth_token``, ``server.allow_unauthenticated: true`` or
    ``SENTINEL_ALLOW_OPEN=1`` (e.g. behind a firewall or an authenticating proxy on another host).
    """
    server = cfg.server
    if (
        is_loopback_host(server.host)
        or server.auth_token
        or login_active(cfg)
        or server.allow_unauthenticated
    ):
        return
    if os.environ.get("SENTINEL_ALLOW_OPEN", "").strip() == "1":
        return
    raise UnsafeBindError(
        f"refusing to serve the API without authentication on {server.host or '*'}:{server.port}: "
        "create the dashboard login with ./sentinel.sh set-password, set server.auth_token (or "
        "SENTINEL_AUTH_TOKEN), bind to 127.0.0.1 and publish Sentinel through the nginx recipe, or "
        "opt in with server.allow_unauthenticated: true or SENTINEL_ALLOW_OPEN=1"
    )


def split_host_port(value: str) -> tuple[str, int | None]:
    """``"Repo.Example.org:8443"`` → ``("repo.example.org", 8443)``; ``"[::1]:9765"`` → ``("::1", 9765)``."""
    value = value.strip()
    port = ""
    if value.startswith("["):
        host, _, rest = value[1:].partition("]")
        if rest.startswith(":"):
            port = rest[1:]
    elif value.count(":") == 1:
        host, _, port = value.partition(":")
    else:  # a bare name, an IPv4 address or an unbracketed IPv6 address
        host = value
    return host.strip().lower().rstrip("."), int(port) if port.isdigit() else None


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    return True


def _url_host(url: str) -> str:
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


def allowed_hosts(cfg: Config) -> frozenset[str] | None:
    """Host names the API answers to (``None``: any, when ``server.allowed_hosts`` has ``"*"``).

    DNS rebinding reaches Sentinel with the attacker's DNS name in ``Host``, so only names that
    point at this machine are accepted: ``localhost``, the machine and display host names, the bind
    address, the hosts of the configured DSpace/nginx URLs (the nginx recipe forwards
    ``Host $host`` from the repository's own server block) and ``server.allowed_hosts``.
    IP literals are always accepted (a rebinding attack cannot produce one).
    """
    names = {"localhost", socket.gethostname(), cfg.server.hostname, cfg.server.host, cfg.ssl.host}
    for url in (
        cfg.dspace.public_url,
        cfg.dspace.ui_url,
        cfg.dspace.server_url,
        cfg.nginx.http_url,
        cfg.nginx.https_url,
    ):
        names.add(_url_host(url))
    for entry in cfg.server.allowed_hosts:
        if entry.strip() == "*":
            return None
        names.add(split_host_port(entry)[0])
    return frozenset(split_host_port(n)[0] for n in names if n and n.strip())


def host_allowed(host_header: str, allowed: frozenset[str] | None) -> bool:
    """True when the ``Host`` header names this machine (see :func:`allowed_hosts`)."""
    host, _port = split_host_port(host_header)
    if not host:
        return False
    return allowed is None or _is_ip_literal(host) or host in allowed


def _same_origin(origin: str, *authorities: str | None) -> bool:
    """True when ``origin`` (the ``Origin`` header) names one of ``authorities`` (``Host`` values).

    A ``Host`` without a port (a reverse proxy forwarding ``$host``) matches any port.
    """
    try:
        parts = urlsplit(origin.strip())
        origin_port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    origin_host = parts.hostname.lower().rstrip(".")
    for authority in authorities:
        if not authority:
            continue
        host, port = split_host_port(authority.split(",", 1)[0])
        if host == origin_host and (port is None or port == origin_port):
            return True
    return False


def _header(headers: list[tuple[bytes, bytes]], name: bytes) -> str | None:
    for key, value in headers:
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def _cookie(headers: list[tuple[bytes, bytes]], name: str) -> str | None:
    """The value of cookie ``name`` across every ``Cookie`` header, or ``None``."""
    joined = "; ".join(value.decode("latin-1") for key, value in headers if key.lower() == b"cookie")
    return cookie_parser(joined).get(name) if joined else None


async def _send_error(
    send: Send, status: int, message: str, extra: list[tuple[bytes, bytes]] | None = None
) -> None:
    body = JSONResponse({"error": message}).body
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"cache-control", b"no-store"),
                *(extra or []),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class SentinelMiddleware:
    """Pure ASGI middleware, in this order: ``Host`` allowlist, the login or Basic auth, refusal
    of cross-site writes, cache headers. ``/healthz`` (exactly) is exempt from the first three.

    With a login set (``server.login``, never in the demo): :data:`OPEN_PATHS` and ``/static/**``
    are open, every other request needs a valid session cookie, ``server.auth_token`` (Basic with
    any user name, or Bearer) or the local CLI token (Bearer); otherwise 401 ``{"error": "login
    required"}`` without ``WWW-Authenticate`` (no browser pop-up). Without a login but with
    ``server.auth_token``: everything but :data:`AUTH_PATHS` needs the token (Basic or Bearer) or
    the CLI token, and a 401 carries the Basic challenge as before.
    """

    def __init__(
        self,
        app: ASGIApp,
        get_cfg: Callable[[], Config],
        get_auth: Callable[[], AuthService | None] | None = None,
    ) -> None:
        self.app = app
        self.get_cfg = get_cfg
        self.get_auth = get_auth
        self._hosts_for: tuple[Config, frozenset[str] | None] | None = None

    def _allowed(self, cfg: Config) -> frozenset[str] | None:
        if self._hosts_for is None or self._hosts_for[0] is not cfg:
            self._hosts_for = (cfg, allowed_hosts(cfg))
        return self._hosts_for[1]

    @staticmethod
    def _has_access(
        headers: list[tuple[bytes, bytes]], cfg: Config, auth: AuthService | None, *, sessions: bool
    ) -> bool:
        scheme, value = authorization(headers)
        if auth is not None:
            if auth.token_ok(scheme, value, cfg):
                return True
        elif scheme in ("basic", "bearer") and token_matches(value, cfg.server.auth_token):
            return True
        if sessions and auth is not None:
            return auth.session_for(_cookie(headers, SESSION_COOKIE), cfg) is not None
        return False

    def _auth_refusal(
        self, local_path: str, headers: list[tuple[bytes, bytes]], cfg: Config, auth: AuthService | None
    ) -> tuple[int, str, list[tuple[bytes, bytes]]] | None:
        """Why the request may not go on (status, message, extra headers), or ``None``."""
        if login_active(cfg):
            if is_open_path(local_path) or self._has_access(headers, cfg, auth, sessions=True):
                return None
            return 401, LOGIN_REQUIRED, []
        if cfg.server.auth_token:
            if local_path in AUTH_PATHS or self._has_access(headers, cfg, auth, sessions=False):
                return None
            challenge = f'Basic realm="{REALM}", charset="UTF-8"'.encode()
            return 401, "authentication required", [(b"www-authenticate", challenge)]
        return None

    @staticmethod
    def _cross_site_refusal(headers: list[tuple[bytes, bytes]]) -> tuple[int, str] | None:
        """Why a state-changing request must be refused, or ``None``.

        Browsers mark cross-site requests (``Sec-Fetch-Site``, ``Origin``) and HTML forms can only
        send form or text bodies without a CORS preflight; curl and the CLI send none of these.
        """
        fetch_site = (_header(headers, b"sec-fetch-site") or "").strip().lower()
        origin = _header(headers, b"origin")
        if fetch_site in CROSS_SITE_FETCH or (
            origin is not None
            and not _same_origin(origin, _header(headers, b"host"), _header(headers, b"x-forwarded-host"))
        ):
            return 403, "cross-site request refused: open Sentinel's own page to make changes"
        content_type = (_header(headers, b"content-type") or "").split(";", 1)[0].strip().lower()
        if content_type and content_type != "application/json" and not content_type.endswith("+json"):
            return 415, "unsupported content type: send application/json (or no body)"
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        root_path: str = scope.get("root_path", "") or ""
        local_path = path[len(root_path) :] if root_path and path.startswith(root_path) else path
        headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
        cfg = self.get_cfg()
        if local_path != HEALTH_PATH:
            host = _header(headers, b"host")
            if host is not None and not host_allowed(host, self._allowed(cfg)):
                await _send_error(
                    send,
                    400,
                    f"unknown host {split_host_port(host)[0]!r}: add it to server.allowed_hosts "
                    "in config/sentinel.yaml if Sentinel is reached under that name",
                )
                return
            auth = self.get_auth() if self.get_auth is not None else None
            if auth is not None:
                try:
                    auth.refresh()  # a login set with the CLI applies without a restart
                except Exception:
                    log.exception("could not look for a changed server.login")
                cfg = self.get_cfg()
            refusal = self._auth_refusal(local_path, headers, cfg, auth)
            if refusal is not None:
                await _send_error(send, *refusal)
                return
            if str(scope.get("method", "GET")).upper() not in SAFE_METHODS:
                refusal = self._cross_site_refusal(headers)
                if refusal is not None:
                    await _send_error(send, *refusal)
                    return

        cache: bytes | None = None
        if "/static/" in path:
            cache = HTML_CACHE.encode() if path.endswith(".html") else STATIC_CACHE.encode()
        elif path in ("", "/") or path.endswith("/index.html"):
            cache = HTML_CACHE.encode()

        async def send_wrapper(message: Message) -> None:
            if cache is not None and message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"]
                headers.append((b"cache-control", cache))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)


def create_app(
    cfg: Config | None = None,
    *,
    config_path: Path | None = None,
    data_dir: Path | None = None,
    store: Store | None = None,
    demo_backfill_days: int = 7,
    start_scheduler: bool = True,
) -> FastAPI:
    """Build the FastAPI app.

    Args:
        cfg: effective config (``load_config()`` when ``None``).
        config_path: where ``PATCH /api/config`` saves (default :func:`paths.config_path`).
        data_dir: runtime data directory (default :func:`paths.data_dir`); holds ``sentinel.db``.
        store: inject a prepared store (tests); otherwise opened at ``<data_dir>/sentinel.db``.
        demo_backfill_days: history the demo suite backfills on first start.
        start_scheduler: set false to serve the API without collecting (tests of pure routes).

    Raises:
        UnsafeBindError: ``server.host`` is not loopback and neither the login,
            ``server.auth_token``, ``server.allow_unauthenticated`` nor ``SENTINEL_ALLOW_OPEN=1``
            is set.
    """
    cfg = cfg or load_config(config_path)
    check_bind(cfg)
    config_path = config_path or paths.config_path()
    data_dir = paths.ensure_dirs(data_dir or paths.data_dir())
    if store is None:
        store = Store(paths.db_path(data_dir))
    for notice in startup_notices(cfg):  # e.g. the environment's Telegram bot left out (a sixth)
        try:
            store.add_event("config", None, notice)
        except Exception:
            log.exception("could not record a start-up notice")
    bus = Bus()
    state = State()
    alert_engine = AlertEngine(store, cfg, bus)
    incident_engine = IncidentEngine(store, cfg, bus)
    scheduler = Scheduler(
        cfg,
        store,
        state,
        bus,
        alert_engine=alert_engine,
        incident_engine=incident_engine,
        demo_backfill_days=demo_backfill_days,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.started_ts = time.time()
        app.state.shutdown_event.clear()
        try:  # a new local CLI token on every start (status reads it)
            app.state.auth.cli_token.write()
        except OSError as exc:
            log.warning("could not write the local CLI token: %s", exc)
        restore_signals = chain_shutdown_signals(app.state.shutdown_event)
        if start_scheduler:
            await scheduler.start()
        try:
            yield
        finally:
            app.state.shutdown_event.set()  # any stream still open ends now
            restore_signals()
            await scheduler.stop()
            try:
                store.close()
            except Exception:
                log.exception("store close failed")

    app = FastAPI(
        title="DSpace Sentinel",
        version=__version__,
        description="Observe-only health and observability for DSpace 7, 8 and 9 servers.",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.cfg = cfg
    app.state.config_path = config_path
    app.state.data_dir = data_dir
    app.state.store = store
    app.state.bus = bus
    app.state.state = state
    app.state.scheduler = scheduler
    app.state.alert_engine = alert_engine
    app.state.incident_engine = incident_engine
    app.state.started_ts = time.time()
    app.state.shutdown_event = threading.Event()
    app.state.report_flight = SingleFlight()
    app.state.export_flight = SingleFlight()
    app.state.auth = AuthService(app.state)

    # ------------------------------------------------------------ errors → {"error": ...}

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return JSONResponse(
            {"error": detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None)
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err.get('loc', []))}: {err.get('msg')}" for err in exc.errors()
        )
        return JSONResponse({"error": f"invalid request: {problems}"}, status_code=400)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # the details (paths, SQL, library messages) go to the log, not to the client
        log.error("unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
        return JSONResponse({"error": "internal error; the Sentinel log has the details"}, status_code=500)

    # ------------------------------------------------------------------ routes

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, Any]:
        return {"ok": True}

    app.include_router(routes_auth.router)
    app.include_router(routes_status.router)
    app.include_router(routes_history.router)
    app.include_router(routes_alerts.router)
    app.include_router(routes_reports.router)
    app.include_router(routes_config.router)
    app.include_router(stream.router)

    static_dir = paths.STATIC_DIR
    index_file = static_dir / "index.html"

    @app.get("/", include_in_schema=False)
    async def index() -> Any:
        if index_file.is_file():
            return FileResponse(
                str(index_file), media_type="text/html", headers={"Cache-Control": HTML_CACHE}
            )
        return PlainTextResponse(
            "DSpace Sentinel API is running; the web UI is not installed.", status_code=200
        )

    @app.get("/favicon.svg", include_in_schema=False)
    async def favicon() -> Any:
        icon = static_dir / "favicon.svg"
        if icon.is_file():
            return FileResponse(
                str(icon), media_type="image/svg+xml", headers={"Cache-Control": STATIC_CACHE}
            )
        raise HTTPException(status_code=404, detail="no favicon")

    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    app.add_middleware(SentinelMiddleware, get_cfg=lambda: app.state.cfg, get_auth=lambda: app.state.auth)
    return app


__all__ = [
    "AUTH_PATHS",
    "OPEN_PATHS",
    "SentinelMiddleware",
    "UnsafeBindError",
    "allowed_hosts",
    "chain_shutdown_signals",
    "check_bind",
    "create_app",
    "host_allowed",
    "is_loopback_host",
    "is_open_path",
    "split_host_port",
]
