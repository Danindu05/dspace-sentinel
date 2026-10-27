"""The dashboard login over HTTP: ``/api/session``, ``/api/login``, ``/api/logout`` and
``/api/account/password``, plus :class:`AuthService`, which :class:`~dspace_sentinel.api.app.
SentinelMiddleware` asks whether a request may reach the API.

With a login set (``server.login.password_hash``, never in the demo), every other ``/api/*``
request needs a session cookie, ``server.auth_token`` (``Authorization: Basic`` with any user name,
or ``Bearer``) or the local CLI token (``Bearer``, from ``<data dir>/api-token``).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import logging
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from dspace_sentinel.api.deps import AppContext, get_ctx
from dspace_sentinel.auth import (
    LOGIN_UNREADABLE,
    PASSWORD_MAX,
    SESSION_COOKIE,
    CliToken,
    Session,
    Sessions,
    Throttle,
    api_token_path,
    dummy_hash,
    hash_password,
    login_active,
    login_problem,
    password_problem,
    same_username,
    username_problem,
    verify_password,
)
from dspace_sentinel.config import Config, LoginConfig, apply_login, read_yaml, save_config

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["auth"])

NO_LOGIN = "No login is set up on this Sentinel."
WRONG_LOGIN = "Wrong username or password."
CURRENT_WRONG = "The current password is wrong."
SAME_PASSWORD = "The new password is the same as the current one."
LOGIN_REQUIRED = "login required"
"""The middleware's 401 answer while a login is set (no ``WWW-Authenticate``: no browser pop-up)."""
TEXT_FIELDS = "send the fields as text"

RELOAD_EVERY_S = 2.0
"""How often a request may look at the config file's mtime for a login set with the CLI."""

NO_STORE = {"Cache-Control": "no-store"}


def too_many(seconds: int) -> JSONResponse:
    """429 ``{"error": "Too many attempts. Try again in 40 s.", "retry_after": 40}`` + ``Retry-After``."""
    return JSONResponse(
        {"error": f"Too many attempts. Try again in {seconds} s.", "retry_after": seconds},
        status_code=429,
        headers={"Retry-After": str(seconds), **NO_STORE},
    )


def client_ip(request: Request) -> str:
    """The client's address as uvicorn reports it (from a trusted proxy's headers behind nginx)."""
    host = request.client.host if request.client else ""
    return "".join(ch for ch in host if ch.isprintable())[:64] or "unknown"


def _shown_name(typed: str) -> str:
    """The typed username for the events log: only a name the username rules allow is repeated,
    so a password typed into the username field never lands in the log."""
    name = typed.strip()
    return f"'{name}'" if username_problem(name) is None else "an invalid username"


def authorization(headers: list[tuple[bytes, bytes]]) -> tuple[str, str]:
    """The first ``Authorization`` header: ``("basic", password)`` (any user name),
    ``("bearer", token)`` or ``("", "")`` (none, another scheme or a broken value)."""
    for name, raw in headers:
        if name.lower() != b"authorization":
            continue
        scheme, _, value = raw.decode("latin-1").partition(" ")
        scheme = scheme.lower()
        if scheme == "bearer":
            return "bearer", value.strip()
        if scheme == "basic":
            try:
                decoded = base64.b64decode(value.strip(), validate=True).decode("utf-8", "replace")
            except (ValueError, binascii.Error):
                return "", ""
            return "basic", decoded.partition(":")[2]
        return "", ""
    return "", ""


def token_matches(given: str, token: str) -> bool:
    """True when ``token`` is set and ``given`` is it (constant-time)."""
    return bool(token) and hmac.compare_digest(
        given.encode("utf-8", "surrogatepass"), token.encode("utf-8", "surrogatepass")
    )


def _file_signature(path: Path) -> tuple[int, int, int] | None:
    try:
        st = Path(path).stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


class AuthService:
    """The login state of one running app: sessions, brute-force limits, the CLI token and the
    watch on the config file (a password set with ``set-password`` applies without a restart).

    ``state`` is ``app.state``: ``cfg``, ``store``, ``config_path`` and ``data_dir`` are read from
    it at every use, so a configuration swapped in by ``PATCH /api/config`` is the one used.
    """

    reload_every_s = RELOAD_EVERY_S

    def __init__(self, state: Any) -> None:
        self.state = state
        self.sessions = Sessions(state.store)
        self.throttle = Throttle()
        self.cli_token = CliToken(api_token_path(state.data_dir))
        self._signature = _file_signature(state.config_path)
        self._checked = time.monotonic()

    @property
    def cfg(self) -> Config:
        return self.state.cfg

    # ---------------------------------------------------------------------------- the file

    def refresh(self, force: bool = False) -> None:
        """Re-read ``server.login`` when the config file changed (at most every couple of seconds)."""
        cfg = self.cfg
        if cfg.demo:
            return
        now = time.monotonic()
        if not force and now - self._checked < self.reload_every_s:
            return
        self._checked = now
        signature = _file_signature(self.state.config_path)
        if signature is None or signature == self._signature:
            return  # a file that went away changes nothing: the login never drops by itself
        self._signature = signature
        self._reload_login(cfg)

    def note_saved(self) -> None:
        """The app wrote the config file itself: nothing to re-read."""
        self._signature = _file_signature(self.state.config_path)

    def _reload_login(self, cfg: Config) -> None:
        path = self.state.config_path
        try:
            raw = read_yaml(path)
            server = raw.get("server")
            login = LoginConfig.model_validate(
                (server if isinstance(server, dict) else {}).get("login") or {}
            )
        except (OSError, ValueError, ValidationError) as exc:
            log.warning("server.login in %s could not be read again; the login is unchanged: %s", path, exc)
            return
        current = cfg.server.login
        if (login.username, login.password_hash) == (current.username, current.password_hash):
            return
        if not login.password_hash and not self._may_drop_login(cfg):
            log.warning(
                "server.login was removed from %s, but Sentinel listens on %s and would then be open: "
                "the login stays until a restart",
                path,
                cfg.server.host or "*",
            )
            return
        apply_login(cfg, login.username, login.password_hash)
        ended = self.sessions.end_all()
        log.info("server.login changed in %s; %d session(s) signed out", path, ended)
        problem = login_problem(login.username, login.password_hash)
        if problem:
            log.warning("%s", problem)
        self.event("Password changed" if login.password_hash else "Login removed")

    @staticmethod
    def _may_drop_login(cfg: Config) -> bool:
        from dspace_sentinel.api.app import UnsafeBindError, check_bind

        kept = cfg.server.login
        cfg.server.login = LoginConfig()
        try:
            check_bind(cfg)
        except UnsafeBindError:
            return False
        finally:
            cfg.server.login = kept
        return True

    # ---------------------------------------------------------------------------- requests

    def session_for(self, token: str | None, cfg: Config | None = None) -> Session | None:
        """The valid session of a cookie value under the current login, else ``None``."""
        cfg = cfg or self.cfg
        login = cfg.server.login
        if not token or not login_active(cfg) or login_problem(login.username, login.password_hash):
            return None
        return self.sessions.check(token, login.username, login.password_hash)

    def session_of(self, request: Request) -> Session | None:
        return self.session_for(request.cookies.get(SESSION_COOKIE))

    def token_ok(self, scheme: str, value: str, cfg: Config | None = None) -> bool:
        """True for ``server.auth_token`` (Basic password or Bearer) or the CLI token (Bearer)."""
        cfg = cfg or self.cfg
        if scheme in ("basic", "bearer") and token_matches(value, cfg.server.auth_token):
            return True
        return scheme == "bearer" and self.cli_token.matches(value)

    def event(self, message: str) -> None:
        """Record an ``auth`` event (never a password)."""
        try:
            self.state.store.add_event("auth", None, message)
        except Exception:
            log.exception("could not record an auth event")


def _auth(request: Request) -> AuthService:
    return request.app.state.auth


def _set_cookie(response: JSONResponse, request: Request, token: str, session: Session) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max(0, int(session.expires_ts - time.time())),
        path=_cookie_path(request),
        secure=request.url.scheme == "https",
        httponly=True,
        samesite="strict",
    )


def _cookie_path(request: Request) -> str:
    return (request.scope.get("root_path") or "").rstrip("/") or "/"


def _text_fields(body: Mapping[str, Any], *names: str) -> list[str]:
    values = [body.get(name, "") for name in names]
    if not all(isinstance(value, str) for value in values):
        raise HTTPException(status_code=400, detail=TEXT_FIELDS)
    return values


def _usable_login(cfg: Config) -> LoginConfig:
    """The login, or 400 (none set, or the demo) / 503 (it cannot be read)."""
    if not login_active(cfg):
        raise HTTPException(status_code=400, detail=NO_LOGIN)
    login = cfg.server.login
    if login_problem(login.username, login.password_hash):
        raise HTTPException(status_code=503, detail=LOGIN_UNREADABLE)
    return login


@router.get("/session")
async def get_session(request: Request, ctx: AppContext = Depends(get_ctx)) -> JSONResponse:
    """Always open: ``{login_required, authenticated, username, expires_ts}`` (the demo: no login)."""
    body: dict[str, Any] = {
        "login_required": False,
        "authenticated": False,
        "username": None,
        "expires_ts": None,
    }
    if login_active(ctx.cfg):
        body["login_required"] = True
        auth = _auth(request)
        session = auth.session_of(request)
        if session is not None:
            body.update(authenticated=True, username=session.username, expires_ts=session.expires_ts)
        else:
            scheme, value = authorization(request.headers.raw)
            body["authenticated"] = auth.token_ok(scheme, value, ctx.cfg)
    return JSONResponse(body, headers=NO_STORE)


@router.post("/login")
async def post_login(
    request: Request, body: dict[str, Any] = Body(default_factory=dict), ctx: AppContext = Depends(get_ctx)
) -> JSONResponse:
    """Sign in with ``{username, password, remember}``: 200 and the session cookie, 401 for a wrong
    username or password (the same answer and the same time for both), 429 while paused."""
    login = _usable_login(ctx.cfg)
    username, password = _text_fields(body, "username", "password")
    remember = body.get("remember") is True
    auth = _auth(request)
    ip = client_ip(request)
    wait = auth.throttle.retry_after(ip, username)
    if wait:
        return too_many(wait)
    name_ok = same_username(username, login.username)
    target = login.password_hash if name_ok else dummy_hash(login.password_hash)
    matched = len(password) <= PASSWORD_MAX and await asyncio.to_thread(verify_password, password, target)
    if not (name_ok and matched):
        paused = auth.throttle.failed(ip, username)
        auth.event(f"Sign-in failed for {_shown_name(username)} ({ip})")
        if paused:
            auth.event(f"Sign-in paused for {ip} after 5 failed attempts")
        raise HTTPException(status_code=401, detail=WRONG_LOGIN)
    auth.throttle.succeeded(ip)
    token, session = auth.sessions.create(
        login.username,
        login.password_hash,
        remember=remember,
        ip=ip,
        user_agent=request.headers.get("user-agent", ""),
    )
    auth.event(f"Signed in: {login.username} ({ip})")
    response = JSONResponse(
        {"authenticated": True, "username": login.username, "expires_ts": session.expires_ts},
        headers=NO_STORE,
    )
    _set_cookie(response, request, token, session)
    return response


@router.post("/logout")
async def post_logout(request: Request) -> JSONResponse:
    """End this browser's session (the row is deleted) and clear the cookie."""
    auth = _auth(request)
    username = auth.sessions.end(request.cookies.get(SESSION_COOKIE) or "")
    if username is not None:
        auth.event(f"Signed out: {username}")
    response = JSONResponse({"authenticated": False}, headers=NO_STORE)
    response.delete_cookie(
        SESSION_COOKIE,
        path=_cookie_path(request),
        secure=request.url.scheme == "https",
        httponly=True,
        samesite="strict",
    )
    return response


@router.post("/account/password")
async def post_password(
    request: Request, body: dict[str, Any] = Body(default_factory=dict), ctx: AppContext = Depends(get_ctx)
) -> JSONResponse:
    """The signed-in admin changes the password with ``{current, new}``; every other session ends."""
    login = _usable_login(ctx.cfg)
    auth = _auth(request)
    session = auth.session_of(request)
    if session is None:
        raise HTTPException(status_code=401, detail=LOGIN_REQUIRED)
    current, new = _text_fields(body, "current", "new")
    ip = client_ip(request)
    wait = auth.throttle.retry_after(ip, login.username)
    if wait:
        return too_many(wait)
    matched = len(current) <= PASSWORD_MAX and await asyncio.to_thread(
        verify_password, current, login.password_hash
    )
    if not matched:
        if auth.throttle.failed(ip, login.username):
            auth.event(f"Sign-in paused for {ip} after 5 failed attempts")
        raise HTTPException(status_code=400, detail=CURRENT_WRONG)
    if new == current:
        raise HTTPException(status_code=400, detail=SAME_PASSWORD)
    problem = password_problem(new, login.username)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    encoded = await asyncio.to_thread(hash_password, new)
    cfg = ctx.cfg
    if (cfg.server.login.username, cfg.server.login.password_hash) != (login.username, login.password_hash):
        raise HTTPException(status_code=401, detail=LOGIN_REQUIRED)  # changed meanwhile (CLI)
    apply_login(cfg, login.username, encoded)
    try:
        save_config(cfg, ctx.config_path)
    except Exception:
        apply_login(cfg, login.username, login.password_hash)
        raise
    auth.note_saved()
    auth.throttle.succeeded(ip)
    auth.sessions.end_others(session.token_hash, login.username, encoded)
    auth.event("Password changed")
    return JSONResponse({"ok": True}, headers=NO_STORE)


__all__ = [
    "CURRENT_WRONG",
    "LOGIN_REQUIRED",
    "NO_LOGIN",
    "RELOAD_EVERY_S",
    "SAME_PASSWORD",
    "WRONG_LOGIN",
    "AuthService",
    "authorization",
    "client_ip",
    "router",
    "token_matches",
    "too_many",
]
