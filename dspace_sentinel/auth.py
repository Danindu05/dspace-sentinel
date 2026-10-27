"""The dashboard login: password hashing, the password rules, sessions and the brute-force limits.

One admin account lives in ``server.login`` (``username`` and ``password_hash``); the login is on
when ``password_hash`` is set. Passwords are hashed with scrypt from the standard library
(``scrypt$16384$8$1$<salt>$<hash>``, salt and hash base64url without padding), or with
PBKDF2-SHA256 (``pbkdf2_sha256$600000$<salt>$<hash>``) where ``hashlib.scrypt`` is missing;
:func:`verify_password` reads both. No password is ever logged, stored in clear or put into an
exception text.

A sign-in creates a random session token; the store keeps only its SHA-256, so a copy of the
database holds no usable session. Every session remembers a fingerprint of the login it signed
in with, so a changed password or a removed login ends it at its next request.

This module needs nothing but the standard library (the end-to-end tests import
:func:`hash_password` to write a login into a test configuration).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import math
import os
import re
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

# ------------------------------------------------------------------------------ hashing

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
HASH_BYTES = 32
SALT_BYTES = 16
PBKDF2_ITERATIONS = 600_000
SCRYPT_MAXMEM = 64 * 1024 * 1024
"""Memory scrypt may use while checking a stored hash (the default parameters need 16 MiB)."""

_B64 = re.compile(r"[A-Za-z0-9_-]+")
_DUMMY_SALT = b"dspace-sentinel!"


def scrypt_available() -> bool:
    """True when this Python's ``hashlib`` has scrypt (OpenSSL 1.1 or later)."""
    return hasattr(hashlib, "scrypt")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes | None:
    if not _B64.fullmatch(text) or len(text) % 4 == 1:
        return None
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        return None


def _number(text: str) -> int:
    if not (text.isascii() and text.isdigit()) or len(text) > 9:
        raise ValueError("not a small decimal number")
    return int(text)


def _secret(password: str) -> bytes:
    # a JSON body may carry a lone surrogate; it still hashes the same way every time
    return password.encode("utf-8", "surrogatepass")


@dataclass(frozen=True)
class PasswordHash:
    """A stored password hash taken apart: the scheme, its cost parameters, the salt and the hash."""

    scheme: str
    """``scrypt`` or ``pbkdf2_sha256``."""
    params: tuple[int, ...]
    """``(n, r, p)`` for scrypt, ``(iterations,)`` for PBKDF2."""
    salt: bytes
    digest: bytes

    def derive(self, password: str) -> bytes:
        """The hash of ``password`` with this salt and these parameters."""
        if self.scheme == "scrypt":
            n, r, p = self.params
            return hashlib.scrypt(
                _secret(password),
                salt=self.salt,
                n=n,
                r=r,
                p=p,
                maxmem=SCRYPT_MAXMEM,
                dklen=len(self.digest),
            )
        (iterations,) = self.params
        return hashlib.pbkdf2_hmac("sha256", _secret(password), self.salt, iterations, dklen=len(self.digest))

    def encode(self) -> str:
        """The ``scheme$params…$salt$hash`` text kept in ``server.login.password_hash``."""
        return "$".join([self.scheme, *(str(v) for v in self.params), _b64(self.salt), _b64(self.digest)])


def parse_hash(encoded: Any) -> PasswordHash | None:
    """``encoded`` taken apart, or ``None`` when it is not a hash this Sentinel can check.

    The cost parameters are bounded, so a hand-edited value can neither run for minutes nor
    exhaust memory; a scrypt hash on a Python without scrypt cannot be checked either.
    """
    if not isinstance(encoded, str) or len(encoded) > 512:
        return None
    parts = encoded.split("$")
    try:
        if parts[0] == "scrypt" and len(parts) == 6:
            n, r, p = (_number(value) for value in parts[1:4])
            fits = 2 <= n <= 2**20 and n & (n - 1) == 0 and 1 <= r <= 32 and 1 <= p <= 16
            usable = scrypt_available() and fits and 256 * r * n <= SCRYPT_MAXMEM
            params: tuple[int, ...] = (n, r, p)
        elif parts[0] == "pbkdf2_sha256" and len(parts) == 4:
            iterations = _number(parts[1])
            usable = 1000 <= iterations <= 10_000_000
            params = (iterations,)
        else:
            return None
    except ValueError:
        return None
    salt, digest = _unb64(parts[-2]), _unb64(parts[-1])
    if not usable or salt is None or digest is None:
        return None
    if not (8 <= len(salt) <= 64 and 16 <= len(digest) <= 64):
        return None
    return PasswordHash(parts[0], params, salt, digest)


def _default_scheme() -> tuple[str, tuple[int, ...]]:
    if scrypt_available():
        return "scrypt", (SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return "pbkdf2_sha256", (PBKDF2_ITERATIONS,)


def hash_password(password: str) -> str:
    """Hash ``password`` with 16 random salt bytes: scrypt (n=2**14, r=8, p=1, 32 bytes), or
    PBKDF2-SHA256 with 600,000 iterations where ``hashlib.scrypt`` is missing."""
    scheme, params = _default_scheme()
    template = PasswordHash(scheme, params, secrets.token_bytes(SALT_BYTES), bytes(HASH_BYTES))
    return replace(template, digest=template.derive(password)).encode()


def verify_password(password: str, encoded: str) -> bool:
    """True when ``password`` is the one ``encoded`` was made from (constant-time comparison)."""
    stored = parse_hash(encoded)
    if stored is None or not isinstance(password, str):
        return False
    try:
        candidate = stored.derive(password)
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(candidate, stored.digest)


def dummy_hash(like: str = "") -> str:
    """A well-formed hash no password matches, with the scheme and cost of ``like``.

    A sign-in with an unknown username is checked against it, so it takes as long as one with
    the right username and a wrong password.
    """
    stored = parse_hash(like)
    if stored is None:
        scheme, params = _default_scheme()
        stored = PasswordHash(scheme, params, _DUMMY_SALT, bytes(HASH_BYTES))
    return replace(stored, salt=_DUMMY_SALT, digest=bytes(len(stored.digest))).encode()


# ------------------------------------------------------------------------------ rules

PASSWORD_MIN = 12
PASSWORD_MAX = 256
TOO_SHORT = "Use at least 12 characters."
TOO_LONG = "Use at most 256 characters."
SAME_AS_USERNAME = "The password must differ from the username."
USERNAME_RULE = "The username must be 1 to 64 characters: letters, digits and . - _ @"
DEFAULT_USERNAME = "admin"
LOGIN_UNREADABLE = "The login in server.login could not be read: run ./sentinel.sh set-password"
"""Start-up notice and the answer (503) to every sign-in while the stored login cannot be used."""

_USERNAME = re.compile(r"[A-Za-z0-9._@-]{1,64}")


def password_problem(password: str, username: str = "") -> str | None:
    """Why ``password`` cannot be the login's password, or ``None``: 12 to 256 characters and not
    the username (whatever the case). There are no other rules."""
    if len(password) < PASSWORD_MIN:
        return TOO_SHORT
    if len(password) > PASSWORD_MAX:
        return TOO_LONG
    if username and password.casefold() == username.casefold():
        return SAME_AS_USERNAME
    return None


def username_problem(username: str) -> str | None:
    """Why ``username`` cannot be the login's username (1 to 64 letters, digits and ``.-_@``), or ``None``."""
    return None if isinstance(username, str) and _USERNAME.fullmatch(username) else USERNAME_RULE


def login_problem(username: str, password_hash: str) -> str | None:
    """:data:`LOGIN_UNREADABLE` when a login is set (``password_hash`` not empty) but cannot be
    used (a hash that does not parse, a username that breaks the rules); else ``None``.

    Such a login stays on: nobody can sign in, and nothing falls back to "no login".
    """
    if not password_hash:
        return None
    if username_problem(username) is not None or parse_hash(password_hash) is None:
        return LOGIN_UNREADABLE
    return None


def login_active(cfg: Any) -> bool:
    """True when ``cfg`` asks for the login: ``server.login.password_hash`` is set and this is
    not the demo (the demo never asks for a login)."""
    return bool(cfg.server.login.password_hash) and not bool(getattr(cfg, "demo", False))


def same_username(typed: str, username: str) -> bool:
    """True when the name typed at the sign-in is the login's (spaces around it and case ignored)."""
    return hmac.compare_digest(_secret(typed.strip().casefold()), _secret(username.casefold()))


# ------------------------------------------------------------------------------ sessions

SESSION_COOKIE = "sentinel_session"
IDLE_S = 12 * 3600
"""A session not used for this long ends."""
MAX_AGE_S = 7 * 86400
"""And every session ends this long after the sign-in…"""
REMEMBER_S = 30 * 86400
"""…except with "Keep me signed in": idle and absolute 30 days."""
TOUCH_EVERY_S = 60.0
"""``last_seen_ts`` is written at most this often."""


def idle_limit(remember: bool) -> float:
    return float(REMEMBER_S if remember else IDLE_S)


def lifetime(remember: bool) -> float:
    return float(REMEMBER_S if remember else MAX_AGE_S)


def token_hash(token: str) -> str:
    """What the store keeps of a session token: its SHA-256."""
    return hashlib.sha256(_secret(token)).hexdigest()


def fingerprint(username: str, password_hash: str) -> str:
    """A fingerprint of the login a session signed in with: a new password, another username or
    a removed login no longer matches it."""
    return hashlib.sha256(_secret(f"dspace-sentinel-session\0{username}\0{password_hash}")).hexdigest()


@dataclass(frozen=True)
class Session:
    token_hash: str
    username: str
    created_ts: float
    last_seen_ts: float
    expires_ts: float
    """The absolute end (sign-in + 7 days, or + 30 days with "Keep me signed in")."""
    remember: bool


class Sessions:
    """Sign-in sessions kept in the store's ``sessions`` table (only token hashes)."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def create(
        self,
        username: str,
        password_hash: str,
        *,
        remember: bool = False,
        ip: str = "",
        user_agent: str = "",
        now: float | None = None,
    ) -> tuple[str, Session]:
        """A new session for the login: ``(token for the cookie, the session)``."""
        now = time.time() if now is None else now
        token = secrets.token_urlsafe(32)
        session = Session(token_hash(token), username, now, now, now + lifetime(remember), remember)
        self.store.add_session(
            token_hash=session.token_hash,
            username=username,
            fingerprint=fingerprint(username, password_hash),
            created_ts=now,
            expires_ts=session.expires_ts,
            remember=remember,
            ip=ip[:64],
            user_agent=user_agent[:256],
        )
        return token, session

    def check(
        self, token: str, username: str, password_hash: str, now: float | None = None
    ) -> Session | None:
        """The session of ``token`` while it is valid for the current login, else ``None``.

        A session that idled too long, outlived its lifetime or signed in with another login
        (changed password, other username, removed login) is deleted. ``last_seen_ts`` is written
        at most once a minute.
        """
        if not token or len(token) > 256:
            return None
        now = time.time() if now is None else now
        key = token_hash(token)
        row = self.store.get_session(key)
        if row is None:
            return None
        remember = bool(row["remember"])
        last_seen = float(row["last_seen_ts"] or 0.0)
        expires = float(row["expires_ts"] or 0.0)
        stored = _secret(str(row["fingerprint"] or ""))
        same_login = hmac.compare_digest(stored, _secret(fingerprint(username, password_hash)))
        if (
            not password_hash
            or not same_login
            or row["username"] != username
            or now >= expires
            or now - last_seen >= idle_limit(remember)
        ):
            self.store.delete_session(key)
            return None
        if now - last_seen >= TOUCH_EVERY_S:
            self.store.touch_session(key, now)
            last_seen = now
        created = float(row["created_ts"] or 0.0)
        return Session(key, str(row["username"]), created, last_seen, expires, remember)

    def end(self, token: str) -> str | None:
        """Delete the session of ``token``; its username, or ``None`` when there was none."""
        if not token or len(token) > 256:
            return None
        row = self.store.delete_session(token_hash(token))
        return None if row is None else str(row["username"])

    def end_others(self, keep: str, username: str, password_hash: str) -> int:
        """End every session but ``keep`` (a token hash), which moves to the new login; the count ended."""
        ended = int(self.store.delete_sessions(keep=keep))
        self.store.set_session_fingerprint(keep, fingerprint(username, password_hash))
        return ended

    def end_all(self) -> int:
        return int(self.store.delete_sessions())


# ------------------------------------------------------------------------------ brute force

WINDOW_S = 15 * 60
FREE_FAILURES = 5
"""Failed sign-ins from one IP within :data:`WINDOW_S` before every attempt has to wait."""
MAX_WAIT_S = 300
USER_FAILURES = 20
"""Failed sign-ins for one username within :data:`WINDOW_S`, from any IPs, that lock it…"""
USER_LOCK_S = 15 * 60
"""…for this long."""
MAX_TRACKED = 10_000


def _user_key(username: str) -> str:
    return username.strip().casefold()[:128]


class Throttle:
    """In-memory brute-force limits (a restart forgets them).

    Per client IP: after :data:`FREE_FAILURES` failures within :data:`WINDOW_S`, the next attempt
    must wait ``2 ** (failures - 5)`` seconds (at most :data:`MAX_WAIT_S`) after the last failure;
    a success clears the IP's count. Per username: :data:`USER_FAILURES` failures within the window
    lock that username for :data:`USER_LOCK_S`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ips: dict[str, list[float]] = {}
        self._users: dict[str, list[float]] = {}
        self._locked: dict[str, float] = {}

    @staticmethod
    def _recent(times: list[float] | None, now: float) -> list[float]:
        return [t for t in times or [] if now - t < WINDOW_S]

    def retry_after(self, ip: str, username: str, now: float | None = None) -> int:
        """Seconds before ``ip`` may try ``username`` again (``0``: now)."""
        now = time.time() if now is None else now
        with self._lock:
            wait = 0.0
            failures = self._recent(self._ips.get(ip), now)
            if len(failures) >= FREE_FAILURES:
                pause = min(MAX_WAIT_S, 2 ** (len(failures) - FREE_FAILURES))
                wait = failures[-1] + pause - now
            wait = max(wait, self._locked.get(_user_key(username), 0.0) - now)
        return math.ceil(wait) if wait > 0 else 0

    def failed(self, ip: str, username: str, now: float | None = None) -> bool:
        """Count a failed attempt; True when it is the one that pauses ``ip`` (the fifth)."""
        now = time.time() if now is None else now
        with self._lock:
            failures = [*self._recent(self._ips.get(ip), now), now]
            self._ips[ip] = failures[-64:]
            key = _user_key(username)
            attempts = [*self._recent(self._users.get(key), now), now]
            if len(attempts) >= USER_FAILURES:
                self._locked[key] = now + USER_LOCK_S
                attempts = []
            self._users[key] = attempts
            self._prune(now)
        return len(failures) == FREE_FAILURES

    def succeeded(self, ip: str) -> None:
        """A successful sign-in clears the IP's count."""
        with self._lock:
            self._ips.pop(ip, None)

    def _prune(self, now: float) -> None:
        for table in (self._ips, self._users):
            _forget_oldest(table, now)
        for key in [k for k, until in self._locked.items() if until <= now]:
            del self._locked[key]


def _forget_oldest(table: dict[str, list[float]], now: float) -> None:
    """Keep at most :data:`MAX_TRACKED` entries: the quiet ones go first, then the oldest."""
    if len(table) <= MAX_TRACKED:
        return
    for key in [k for k, times in table.items() if not times or now - times[-1] >= WINDOW_S]:
        del table[key]
    newest = {key: (times[-1] if times else 0.0) for key, times in table.items()}
    for key in sorted(newest, key=newest.__getitem__)[: max(0, len(table) - MAX_TRACKED)]:
        del table[key]


# ------------------------------------------------------------------------------ CLI token

API_TOKEN_FILE = "api-token"
_TOKEN_TEXT = re.compile(r"[A-Za-z0-9_-]{20,128}")


def api_token_path(data_dir: Path) -> Path:
    """``<data dir>/api-token``: the local CLI token (mode 0600, new on every start)."""
    return Path(data_dir) / API_TOKEN_FILE


def read_cli_token(data_dir: Path) -> str:
    """The token in ``<data dir>/api-token``, or ``""`` when it is missing or unreadable."""
    try:
        text = api_token_path(data_dir).read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return ""
    return text if _TOKEN_TEXT.fullmatch(text) else ""


class CliToken:
    """The random token a starting server writes to ``<data dir>/api-token``.

    ``Authorization: Bearer <it>`` counts as signed in: commands such as ``status`` on this
    machine read it (only Sentinel's user can, mode 0600). The token in the file is accepted too,
    so a second ``serve`` that failed to bind the port and rewrote the file locks nobody out.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.token = ""
        self._seen: tuple[tuple[int, int, int] | None, str] = (None, "")

    def write(self) -> str:
        """Make a new token and write it atomically with mode 0600."""
        token = secrets.token_urlsafe(32)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".api-token-", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="ascii") as fh:
                fh.write(token + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        self.token = token
        return token

    def _from_file(self) -> str:
        try:
            st = self.path.stat()
        except OSError:
            return ""
        signature = (st.st_mtime_ns, st.st_size, st.st_ino)
        if self._seen[0] != signature:
            self._seen = (signature, read_cli_token(self.path.parent))
        return self._seen[1]

    def matches(self, candidate: str) -> bool:
        if not candidate or len(candidate) > 256:
            return False
        given = _secret(candidate)
        if self.token and hmac.compare_digest(given, _secret(self.token)):
            return True
        current = self._from_file()
        return bool(current) and hmac.compare_digest(given, _secret(current))


__all__ = [
    "API_TOKEN_FILE",
    "DEFAULT_USERNAME",
    "FREE_FAILURES",
    "IDLE_S",
    "LOGIN_UNREADABLE",
    "MAX_AGE_S",
    "MAX_WAIT_S",
    "PASSWORD_MAX",
    "PASSWORD_MIN",
    "REMEMBER_S",
    "SAME_AS_USERNAME",
    "SESSION_COOKIE",
    "TOO_LONG",
    "TOO_SHORT",
    "USERNAME_RULE",
    "USER_FAILURES",
    "USER_LOCK_S",
    "WINDOW_S",
    "CliToken",
    "PasswordHash",
    "Session",
    "Sessions",
    "Throttle",
    "api_token_path",
    "dummy_hash",
    "fingerprint",
    "hash_password",
    "login_active",
    "login_problem",
    "parse_hash",
    "password_problem",
    "read_cli_token",
    "same_username",
    "scrypt_available",
    "token_hash",
    "username_problem",
    "verify_password",
]
