"""The dashboard login: password hashing and rules, sessions, brute-force limits, the local CLI
token, and the login over HTTP (``/api/session``, ``/api/login``, ``/api/logout``,
``/api/account/password`` and the middleware that asks for it)."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import stat
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from starlette.requests import Request

from dspace_sentinel.api import routes_auth
from dspace_sentinel.api.app import create_app
from dspace_sentinel.api.routes_config import SERVER_READ_ONLY
from dspace_sentinel.auth import (
    IDLE_S,
    LOGIN_UNREADABLE,
    REMEMBER_S,
    SAME_AS_USERNAME,
    TOO_LONG,
    TOO_SHORT,
    USER_LOCK_S,
    USERNAME_RULE,
    WINDOW_S,
    CliToken,
    Sessions,
    Throttle,
    api_token_path,
    dummy_hash,
    hash_password,
    login_active,
    login_problem,
    parse_hash,
    password_problem,
    read_cli_token,
    same_username,
    scrypt_available,
    token_hash,
    username_problem,
    verify_password,
)
from dspace_sentinel.config import (
    MASK,
    Config,
    apply_demo_overrides,
    apply_login,
    load_config,
    merge_patch,
    save_config,
    startup_notices,
)
from dspace_sentinel.store import Store

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

LOCAL = "http://127.0.0.1"
NOW = 1_800_000_000.0
USERNAME = "admin"
PASSWORD = "correct horse battery"
NEW_PASSWORD = "a brand new passphrase"
HASH = hash_password(PASSWORD)
SALT = base64.urlsafe_b64encode(bytes(16)).decode().rstrip("=")
DIGEST = "A" * 43


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def login_cfg(**server: Any) -> Config:
    """A live (not demo) configuration with the admin login set."""
    return merge_patch(
        Config(), {"server": {"login": {"username": USERNAME, "password_hash": HASH}, **server}}
    )


def make_client(cfg: Config, data_dir: Path, config_path: Path, **kwargs: Any) -> TestClient:
    kwargs.setdefault("start_scheduler", False)
    kwargs.setdefault("demo_backfill_days", 0)
    return TestClient(create_app(cfg, config_path=config_path, data_dir=data_dir, **kwargs), base_url=LOCAL)


@pytest.fixture
def live(data_dir: Path, config_path: Path) -> Iterator[TestClient]:
    with make_client(login_cfg(), data_dir, config_path) as c:
        yield c


def sign_in(c: TestClient, password: str = PASSWORD, username: str = USERNAME, remember: bool = False) -> Any:
    return c.post("/api/login", json={"username": username, "password": password, "remember": remember})


def auth_events(c: TestClient) -> list[str]:
    """The ``auth`` events, newest first."""
    return [e.message for e in c.app.state.store.events(kind="auth")]


# ------------------------------------------------------------------------------ hashing


def test_a_hash_is_scrypt_with_a_random_salt_and_verifies() -> None:
    if not scrypt_available():
        pytest.skip("this Python has no hashlib.scrypt")
    scheme, n, r, p, salt, digest = HASH.split("$")
    assert (scheme, n, r, p) == ("scrypt", "16384", "8", "1")
    assert "=" not in salt + digest and len(_unb64(salt)) == 16 and len(_unb64(digest)) == 32
    assert verify_password(PASSWORD, HASH)
    assert not verify_password(PASSWORD + "!", HASH) and not verify_password("", HASH)
    again = hash_password(PASSWORD)
    assert again != HASH and verify_password(PASSWORD, again)  # a new salt every time
    assert PASSWORD not in HASH


def test_without_scrypt_the_hash_is_pbkdf2_and_both_kinds_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(hashlib, "scrypt", raising=False)
    encoded = hash_password(PASSWORD)
    scheme, iterations, salt, digest = encoded.split("$")
    assert (scheme, iterations) == ("pbkdf2_sha256", "600000")
    assert len(_unb64(salt)) == 16 and len(_unb64(digest)) == 32
    assert verify_password(PASSWORD, encoded) and not verify_password("not the password", encoded)
    # a scrypt hash cannot be checked here: the login is then unusable, never open
    assert parse_hash(f"scrypt$16384$8$1${SALT}${DIGEST}") is None
    assert login_problem(USERNAME, f"scrypt$16384$8$1${SALT}${DIGEST}") == LOGIN_UNREADABLE
    monkeypatch.undo()
    assert verify_password(PASSWORD, encoded)  # a Python with scrypt reads PBKDF2 hashes too


def test_unicode_passwords_hash_and_verify() -> None:
    password = "pässwörd ünïcode 密码 🔑"
    encoded = hash_password(password)
    assert verify_password(password, encoded)
    assert not verify_password("passwort unicode", encoded)
    lone = "lone \ud800 surrogate from JSON"
    assert verify_password(lone, hash_password(lone))


@pytest.mark.parametrize(
    "encoded",
    [
        "",
        PASSWORD,
        f"scrypt$16384$8$1${SALT}",
        f"scrypt$16383$8$1${SALT}${DIGEST}",  # n is not a power of two
        f"scrypt$1048576$64$1${SALT}${DIGEST}",  # far too much memory
        f"scrypt$-16384$8$1${SALT}${DIGEST}",
        f"scrypt$16384$8$1$AAAA${DIGEST}",  # a 3-byte salt
        f"scrypt$16384$8$1${SALT}$not*base64",
        f"pbkdf2_sha256$10${SALT}${DIGEST}",  # too few iterations
        f"bcrypt$12${SALT}${DIGEST}",
        "x" * 600,
    ],
)
def test_a_hash_that_does_not_parse_is_refused(encoded: str) -> None:
    assert parse_hash(encoded) is None
    assert not verify_password("anything at all", encoded)
    assert login_problem(USERNAME, encoded) == (LOGIN_UNREADABLE if encoded else None)


def test_the_dummy_hash_costs_the_same_and_matches_nothing() -> None:
    dummy = dummy_hash(HASH)
    parsed, real = parse_hash(dummy), parse_hash(HASH)
    assert parsed is not None and real is not None
    assert (parsed.scheme, parsed.params, len(parsed.digest)) == (real.scheme, real.params, len(real.digest))
    assert dummy != HASH and not verify_password(PASSWORD, dummy)
    assert parse_hash(dummy_hash("")) is not None  # with nothing to copy: the default scheme


# ------------------------------------------------------------------------------ rules


def test_the_rule_messages_are_the_contracts() -> None:
    assert TOO_SHORT == "Use at least 12 characters."
    assert TOO_LONG == "Use at most 256 characters."
    assert SAME_AS_USERNAME == "The password must differ from the username."
    assert LOGIN_UNREADABLE == "The login in server.login could not be read: run ./sentinel.sh set-password"


@pytest.mark.parametrize(
    ("password", "username", "problem"),
    [
        ("short pw", "admin", TOO_SHORT),
        ("x" * 11, "", TOO_SHORT),
        ("x" * 12, "", None),
        ("x" * 256, "", None),
        ("x" * 257, "", TOO_LONG),
        ("administrator", "administrator", SAME_AS_USERNAME),
        ("ADMINISTRATOR", "administrator", SAME_AS_USERNAME),
        ("administrator1", "administrator", None),
        ("ünïcödé passwört", "admin", None),
    ],
)
def test_password_rules(password: str, username: str, problem: str | None) -> None:
    assert password_problem(password, username) == problem


@pytest.mark.parametrize("name", ["admin", "a", "ops.admin", "first_last-2", "me@example.org", "x" * 64])
def test_usernames_the_rules_allow(name: str) -> None:
    assert username_problem(name) is None


@pytest.mark.parametrize("name", ["", "John Smith", "x" * 65, "admin\n", "ädmin", "admin;drop", " admin"])
def test_usernames_the_rules_refuse(name: str) -> None:
    assert username_problem(name) == USERNAME_RULE
    assert login_problem(name, HASH) == LOGIN_UNREADABLE  # such a login stays on and unusable


def test_the_typed_username_ignores_case_and_surrounding_spaces() -> None:
    assert same_username(" Admin ", "admin") and same_username("ADMIN", "admin")
    assert not same_username("admin2", "admin") and not same_username("", "admin")


def test_the_login_is_off_in_the_demo_and_without_a_hash() -> None:
    assert login_active(login_cfg())
    assert not login_active(Config())
    assert not login_active(apply_demo_overrides(login_cfg()))


# ------------------------------------------------------------------------------ sessions


def test_a_session_keeps_only_the_hash_of_its_token(mem_store: Store) -> None:
    sessions = Sessions(mem_store)
    token, session = sessions.create(USERNAME, HASH, ip="192.0.2.10", user_agent="pytest", now=NOW)
    (row,) = mem_store.sessions()
    assert row["token_hash"] == token_hash(token) == session.token_hash and token not in json.dumps(row)
    assert (row["username"], row["ip"], row["user_agent"], row["remember"]) == (
        USERNAME,
        "192.0.2.10",
        "pytest",
        0,
    )
    assert session.expires_ts == NOW + 7 * 86400 and not session.remember
    checked = sessions.check(token, USERNAME, HASH, now=NOW + 10)
    assert checked is not None and checked.username == USERNAME and checked.expires_ts == session.expires_ts
    assert sessions.check("not-a-token", USERNAME, HASH, now=NOW + 10) is None


def test_a_session_ends_after_12_idle_hours_and_at_most_after_7_days(mem_store: Store) -> None:
    sessions = Sessions(mem_store)
    token, _ = sessions.create(USERNAME, HASH, now=NOW)
    used = NOW
    while used + 11 * 3600 < NOW + 7 * 86400:  # used every 11 hours, it lives…
        used += 11 * 3600
        assert sessions.check(token, USERNAME, HASH, now=used) is not None, (used - NOW) / 3600
    assert sessions.check(token, USERNAME, HASH, now=NOW + 7 * 86400) is None  # …but not past 7 days
    assert mem_store.sessions() == []
    idle, _ = sessions.create(USERNAME, HASH, now=NOW)
    assert sessions.check(idle, USERNAME, HASH, now=NOW + IDLE_S - 1) is not None
    assert sessions.check(idle, USERNAME, HASH, now=NOW + IDLE_S - 1 + IDLE_S) is None
    assert mem_store.sessions() == []


def test_keep_me_signed_in_lasts_30_days_used_or_not(mem_store: Store) -> None:
    sessions = Sessions(mem_store)
    token, session = sessions.create(USERNAME, HASH, remember=True, now=NOW)
    assert session.remember and session.expires_ts == NOW + 30 * 86400
    assert mem_store.sessions()[0]["remember"] == 1
    assert sessions.check(token, USERNAME, HASH, now=NOW + 29 * 86400) is not None  # 29 idle days
    assert sessions.check(token, USERNAME, HASH, now=NOW + 30 * 86400) is None


def test_last_seen_is_written_at_most_once_a_minute(mem_store: Store) -> None:
    sessions = Sessions(mem_store)
    token, _ = sessions.create(USERNAME, HASH, now=NOW)
    sessions.check(token, USERNAME, HASH, now=NOW + 30)
    assert mem_store.sessions()[0]["last_seen_ts"] == NOW
    sessions.check(token, USERNAME, HASH, now=NOW + 61)
    assert mem_store.sessions()[0]["last_seen_ts"] == NOW + 61


def test_a_new_password_another_username_or_no_login_ends_a_session(mem_store: Store) -> None:
    sessions = Sessions(mem_store)
    other = hash_password(NEW_PASSWORD)
    for username, password_hash in ((USERNAME, other), ("root", HASH), (USERNAME, "")):
        token, _ = sessions.create(USERNAME, HASH, now=NOW)
        assert sessions.check(token, username, password_hash, now=NOW + 1) is None
        assert mem_store.sessions() == []


def test_a_password_change_ends_every_other_session(mem_store: Store) -> None:
    sessions = Sessions(mem_store)
    mine, me = sessions.create(USERNAME, HASH, now=NOW)
    theirs, _ = sessions.create(USERNAME, HASH, now=NOW)
    new = hash_password(NEW_PASSWORD)
    assert sessions.end_others(me.token_hash, USERNAME, new) == 1
    assert sessions.check(mine, USERNAME, new, now=NOW + 1) is not None
    assert sessions.check(theirs, USERNAME, new, now=NOW + 1) is None


def test_signing_out_deletes_the_session(mem_store: Store) -> None:
    sessions = Sessions(mem_store)
    token, _ = sessions.create(USERNAME, HASH, now=NOW)
    assert sessions.end(token) == USERNAME
    assert sessions.end(token) is None and sessions.end("") is None
    assert sessions.check(token, USERNAME, HASH, now=NOW + 1) is None


def test_maintenance_purges_expired_sessions(mem_store: Store) -> None:
    sessions = Sessions(mem_store)
    sessions.create(USERNAME, HASH, now=NOW)  # idles out after 12 hours
    kept, _ = sessions.create(USERNAME, HASH, remember=True, now=NOW)
    assert mem_store.purge(30, now=NOW + IDLE_S + 1)["sessions"] == 1
    assert [r["token_hash"] for r in mem_store.sessions()] == [token_hash(kept)]
    assert mem_store.purge(30, now=NOW + REMEMBER_S)["sessions"] == 1
    assert mem_store.table_counts()["sessions"] == 0


# ------------------------------------------------------------------------------ brute force


def test_five_failures_from_one_ip_start_a_growing_pause() -> None:
    t = Throttle()
    ip = "192.0.2.10"
    for i in range(4):
        assert t.failed(ip, USERNAME, now=NOW + i) is False
        assert t.retry_after(ip, USERNAME, now=NOW + i) == 0
    assert t.failed(ip, USERNAME, now=NOW + 4) is True  # the fifth pauses the IP
    assert t.retry_after(ip, USERNAME, now=NOW + 4) == 1  # 2 ** 0 s after the last failure
    assert t.retry_after(ip, USERNAME, now=NOW + 5) == 0
    assert t.failed(ip, USERNAME, now=NOW + 5) is False
    assert t.retry_after(ip, USERNAME, now=NOW + 5) == 2  # 2 ** 1
    assert t.retry_after("198.51.100.7", USERNAME, now=NOW + 5) == 0  # another IP may try
    for k in range(8):
        t.failed(ip, "someone", now=NOW + 6 + k)
    assert t.retry_after(ip, USERNAME, now=NOW + 13) == 300  # 2 ** 9, at most 300 s
    assert t.retry_after(ip, USERNAME, now=NOW + 13 + WINDOW_S) == 0  # older than 15 minutes
    t.failed(ip, USERNAME, now=NOW + 14)
    t.succeeded(ip)  # a success clears the IP's count
    assert t.retry_after(ip, USERNAME, now=NOW + 14) == 0


def test_twenty_failures_for_one_username_lock_it_for_15_minutes() -> None:
    t = Throttle()
    for i in range(20):
        assert t.failed(f"203.0.113.{i}", USERNAME, now=NOW + i) is False  # one failure per IP
    assert t.retry_after("198.51.100.1", USERNAME, now=NOW + 20) == USER_LOCK_S - 1
    assert t.retry_after("198.51.100.1", "ADMIN", now=NOW + 20) == USER_LOCK_S - 1
    assert t.retry_after("198.51.100.1", "operator", now=NOW + 20) == 0
    assert t.retry_after("198.51.100.1", USERNAME, now=NOW + 19 + USER_LOCK_S) == 0


# ------------------------------------------------------------------------------ CLI token


def test_the_cli_token_is_private_and_new_on_every_start(tmp_path: Path) -> None:
    token = CliToken(api_token_path(tmp_path))
    first = token.write()
    path = tmp_path / "api-token"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert read_cli_token(tmp_path) == first and token.matches(first)
    second = CliToken(api_token_path(tmp_path)).write()
    assert second != first and read_cli_token(tmp_path) == second
    assert token.matches(second)  # the file's token too: a second serve that could not bind
    assert not token.matches("") and not token.matches("x" * 43)
    assert [p.name for p in tmp_path.iterdir()] == ["api-token"]
    path.write_text("not a token\n")
    assert read_cli_token(tmp_path) == "" and read_cli_token(tmp_path / "missing") == ""


# ------------------------------------------------------------------------------ the middleware


def test_with_a_login_the_api_asks_for_it_without_a_browser_pop_up(live: TestClient) -> None:
    r = live.get("/api/meta")
    assert r.status_code == 401 and r.json() == {"error": "login required"}
    assert "www-authenticate" not in r.headers
    for path in ("/api/status", "/api/config", "/api/stream", "/api/events", "/api/openapi.json"):
        assert live.get(path).status_code == 401, path
    assert live.post("/api/actions/clear-history").status_code == 401
    assert live.get("/api/session").json() == {
        "login_required": True,
        "authenticated": False,
        "username": None,
        "expires_ts": None,
    }
    # the page, its files and the health probe stay open: the page then asks /api/session
    for path in ("/", "/static/js/app.js", "/static/css/tokens.css", "/favicon.svg", "/healthz"):
        assert live.get(path).status_code == 200, path


def test_with_a_login_scripts_use_the_auth_token_or_the_cli_token(data_dir: Path, config_path: Path) -> None:
    with make_client(login_cfg(auth_token="script-token-1234"), data_dir, config_path) as c:
        assert c.get("/api/meta", auth=("anyone", "script-token-1234")).status_code == 200
        assert c.get("/api/meta", headers={"Authorization": "Bearer script-token-1234"}).status_code == 200
        cli_token = read_cli_token(data_dir)
        assert cli_token
        assert c.get("/api/meta", headers={"Authorization": f"Bearer {cli_token}"}).status_code == 200
        wrong = c.get("/api/meta", auth=("anyone", "wrong-token"))
        assert wrong.status_code == 401 and wrong.json() == {"error": "login required"}
        assert "www-authenticate" not in wrong.headers
        assert c.get("/api/meta", headers={"Authorization": "Bearer wrong-token"}).status_code == 401
        session = c.get("/api/session", headers={"Authorization": f"Bearer {cli_token}"}).json()
        assert session["login_required"] is True and session["authenticated"] is True
        assert session["username"] is None


def test_without_a_login_the_token_keeps_the_basic_challenge(data_dir: Path, config_path: Path) -> None:
    cfg = merge_patch(Config(), {"server": {"auth_token": "only-a-token-1234"}})
    with make_client(cfg, data_dir, config_path) as c:
        r = c.get("/api/meta")
        assert r.status_code == 401 and r.json() == {"error": "authentication required"}
        assert r.headers["www-authenticate"].startswith("Basic realm=")
        assert c.get("/").status_code == 401
        assert c.get("/api/meta", auth=("x", "only-a-token-1234")).status_code == 200
        assert c.get("/api/meta", headers={"Authorization": "Bearer only-a-token-1234"}).status_code == 200
        cli_token = read_cli_token(data_dir)
        assert c.get("/api/meta", headers={"Authorization": f"Bearer {cli_token}"}).status_code == 200
        assert c.get("/api/session").json()["login_required"] is False  # always open
        assert sign_in(c).status_code == 400


def test_without_a_login_nothing_changes(data_dir: Path, config_path: Path) -> None:
    with make_client(Config(), data_dir, config_path) as c:
        assert c.get("/api/meta").status_code == 200
        assert c.get("/api/session").json() == {
            "login_required": False,
            "authenticated": False,
            "username": None,
            "expires_ts": None,
        }
        r = sign_in(c)
        assert r.status_code == 400 and r.json() == {"error": "No login is set up on this Sentinel."}


def test_the_demo_never_asks_for_a_login(data_dir: Path, config_path: Path) -> None:
    with make_client(apply_demo_overrides(login_cfg()), data_dir, config_path) as c:
        assert c.get("/api/meta").status_code == 200
        assert c.get("/api/session").json()["login_required"] is False
        assert sign_in(c).status_code == 400


def test_the_sign_in_refuses_other_sites_and_unknown_hosts(live: TestClient) -> None:
    body = {"username": USERNAME, "password": PASSWORD}
    cross = live.post("/api/login", json=body, headers={"Origin": "http://evil.example"})
    assert cross.status_code == 403 and "cross-site" in cross.json()["error"]
    form = live.post(
        "/api/login",
        content=b"username=admin&password=x",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert form.status_code == 415
    assert live.post("/api/login", json=body, headers={"Host": "rebind.attacker.example"}).status_code == 400
    assert live.app.state.store.sessions() == []


# ------------------------------------------------------------------------------ the routes


def test_signing_in_sets_a_strict_http_only_cookie(live: TestClient) -> None:
    r = sign_in(live)
    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] is True and body["username"] == USERNAME
    assert body["expires_ts"] == pytest.approx(time.time() + 7 * 86400, abs=120)
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("sentinel_session=")
    lowered = cookie.lower()
    assert "httponly" in lowered and "samesite=strict" in lowered and "path=/" in lowered
    assert "secure" not in lowered  # plain HTTP (the SSH tunnel)
    max_age = int(re.search(r"max-age=(\d+)", lowered).group(1))  # type: ignore[union-attr]
    assert 7 * 86400 - 120 <= max_age <= 7 * 86400
    assert r.headers["cache-control"] == "no-store"
    assert live.get("/api/meta").status_code == 200  # the client sends the cookie now
    session = live.get("/api/session").json()
    assert session["authenticated"] is True and session["username"] == USERNAME
    assert session["expires_ts"] == pytest.approx(body["expires_ts"])
    token = cookie.split(";", 1)[0].split("=", 1)[1]
    (row,) = live.app.state.store.sessions()
    assert row["token_hash"] == token_hash(token) and token not in json.dumps(row)
    assert auth_events(live) == ["Signed in: admin (testclient)"]


def test_the_username_is_not_case_sensitive(live: TestClient) -> None:
    r = sign_in(live, username=" Admin ")
    assert r.status_code == 200 and r.json()["username"] == USERNAME


def test_keep_me_signed_in_makes_a_30_day_session(live: TestClient) -> None:
    r = sign_in(live, remember=True)
    assert r.json()["expires_ts"] == pytest.approx(time.time() + 30 * 86400, abs=120)
    max_age = int(re.search(r"max-age=(\d+)", r.headers["set-cookie"].lower()).group(1))  # type: ignore[union-attr]
    assert max_age > 29 * 86400
    assert live.app.state.store.sessions()[0]["remember"] == 1


def test_a_wrong_password_and_an_unknown_username_get_the_same_answer(
    live: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    checked: list[str] = []
    real = routes_auth.verify_password

    def spy(password: str, encoded: str) -> bool:
        checked.append(encoded)
        return real(password, encoded)

    monkeypatch.setattr(routes_auth, "verify_password", spy)
    wrong = sign_in(live, password="not the password")
    unknown = sign_in(live, username="root")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == {"error": "Wrong username or password."}
    assert "set-cookie" not in wrong.headers and "www-authenticate" not in wrong.headers
    assert checked == [HASH, dummy_hash(HASH)]  # the unknown name is checked against a dummy hash
    assert auth_events(live) == [
        "Sign-in failed for 'root' (testclient)",
        "Sign-in failed for 'admin' (testclient)",
    ]
    assert live.app.state.store.sessions() == []


def test_a_password_typed_as_the_username_is_not_logged(live: TestClient) -> None:
    assert sign_in(live, username="my secret pass!", password="x").status_code == 401
    assert auth_events(live) == ["Sign-in failed for an invalid username (testclient)"]


def test_after_five_failures_the_ip_waits_and_gets_retry_after(live: TestClient) -> None:
    for _ in range(5):
        assert sign_in(live, password="wrong password!").status_code == 401
    assert auth_events(live)[0] == "Sign-in paused for testclient after 5 failed attempts"
    for _ in range(5):  # ten failures: the next attempt waits 2 ** 5 = 32 s
        live.app.state.auth.throttle.failed("testclient", "someone")
    r = sign_in(live)  # the right password waits too
    assert r.status_code == 429
    body = r.json()
    assert 25 <= body["retry_after"] <= 32
    assert body["error"] == f"Too many attempts. Try again in {body['retry_after']} s."
    assert r.headers["retry-after"] == str(body["retry_after"]) and "set-cookie" not in r.headers


def test_a_success_clears_the_ips_failures(live: TestClient) -> None:
    for _ in range(4):
        assert sign_in(live, password="wrong password!").status_code == 401
    assert sign_in(live).status_code == 200
    for _ in range(4):  # counted again from zero: never paused
        assert sign_in(live, password="wrong password!").status_code == 401


def test_twenty_failures_lock_the_username(live: TestClient) -> None:
    for i in range(20):
        live.app.state.auth.throttle.failed(f"203.0.113.{i}", USERNAME)
    r = sign_in(live)
    assert r.status_code == 429 and USER_LOCK_S - 30 <= r.json()["retry_after"] <= USER_LOCK_S
    assert sign_in(live, username="nobody").status_code == 401  # other names are not locked


def test_signing_out_deletes_the_session_and_clears_the_cookie(live: TestClient) -> None:
    assert sign_in(live).status_code == 200
    r = live.post("/api/logout")
    assert r.status_code == 200 and r.json() == {"authenticated": False}
    cookie = r.headers["set-cookie"].lower()
    assert cookie.startswith("sentinel_session=") and "max-age=0" in cookie
    assert live.app.state.store.sessions() == []
    assert live.get("/api/meta").status_code == 401
    assert live.post("/api/logout").status_code == 401  # needs a session
    assert auth_events(live)[0] == "Signed out: admin"


def test_an_idle_session_ends(live: TestClient) -> None:
    assert sign_in(live).status_code == 200
    store = live.app.state.store
    (row,) = store.sessions()
    store.touch_session(row["token_hash"], time.time() - IDLE_S - 1)
    assert live.get("/api/meta").status_code == 401
    assert store.sessions() == []


def test_changing_the_password_ends_every_other_session(live: TestClient, config_path: Path) -> None:
    other = TestClient(live.app, base_url=LOCAL)  # a second browser
    assert sign_in(other).status_code == 200
    assert sign_in(live).status_code == 200
    r = live.post("/api/account/password", json={"current": PASSWORD, "new": NEW_PASSWORD})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert live.get("/api/meta").status_code == 200  # the caller stays signed in
    assert other.get("/api/meta").status_code == 401  # every other session ended
    assert len(live.app.state.store.sessions()) == 1
    saved = yaml.safe_load(config_path.read_text())["server"]["login"]
    assert saved["username"] == USERNAME and verify_password(NEW_PASSWORD, saved["password_hash"])
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert NEW_PASSWORD not in config_path.read_text()
    assert sign_in(other).status_code == 401
    assert sign_in(other, password=NEW_PASSWORD).status_code == 200
    assert "Password changed" in auth_events(live)
    assert not any(NEW_PASSWORD in m or PASSWORD in m for m in auth_events(live))


def test_password_change_refusals(live: TestClient) -> None:
    assert sign_in(live).status_code == 200

    def change(current: Any, new: Any) -> Any:
        return live.post("/api/account/password", json={"current": current, "new": new})

    for current, new, message in (
        ("not the password", NEW_PASSWORD, "The current password is wrong."),
        (PASSWORD, PASSWORD, "The new password is the same as the current one."),
        (PASSWORD, "too short", TOO_SHORT),
        (PASSWORD, "x" * 257, TOO_LONG),
        (PASSWORD, 123456789012, "send the fields as text"),
    ):
        r = change(current, new)
        assert (r.status_code, r.json()) == (400, {"error": message}), (current, new)
    assert verify_password(PASSWORD, live.app.state.cfg.server.login.password_hash)  # unchanged
    for _ in range(4):  # a wrong current password counts as a failed attempt
        change("not the password", NEW_PASSWORD)
    assert auth_events(live)[0] == "Sign-in paused for testclient after 5 failed attempts"
    for _ in range(5):
        live.app.state.auth.throttle.failed("testclient", "someone")
    assert change(PASSWORD, NEW_PASSWORD).status_code == 429
    live.cookies.clear()
    assert change(PASSWORD, NEW_PASSWORD).status_code == 401  # needs a session


def test_the_cookie_is_secure_over_https(data_dir: Path, config_path: Path) -> None:
    app = create_app(login_cfg(), config_path=config_path, data_dir=data_dir, start_scheduler=False)
    with TestClient(app, base_url="https://127.0.0.1") as c:
        r = sign_in(c)
        assert r.status_code == 200 and "secure" in r.headers["set-cookie"].lower()
        assert c.get("/api/meta").status_code == 200


def test_the_cookie_path_is_the_apps_root_path() -> None:
    def request(root_path: str) -> Request:
        return Request({"type": "http", "method": "GET", "path": "/", "headers": [], "root_path": root_path})

    assert routes_auth._cookie_path(request("")) == "/"
    assert routes_auth._cookie_path(request("/sentinel")) == "/sentinel"
    assert routes_auth._cookie_path(request("/sentinel/")) == "/sentinel"


def test_the_password_hash_is_never_shown(live: TestClient) -> None:
    assert sign_in(live).status_code == 200
    shown = live.get("/api/config").json()
    assert shown["server"]["login"] == {"username": USERNAME, "password_hash": MASK}
    texts = [
        json.dumps(shown),
        live.get("/api/export", params={"range": "1h"}).text,
        live.get("/api/events").text,
        live.get("/api/components/server").text,
    ]
    secret_part = HASH.split("$")[-1]
    for text in texts:
        assert secret_part not in text and PASSWORD not in text


def test_the_login_cannot_be_changed_with_patch_config(live: TestClient) -> None:
    assert sign_in(live).status_code == 200
    for login in (
        {"password_hash": hash_password("attacker password")},
        {"username": "root"},
        {"username": "", "password_hash": ""},
    ):
        r = live.patch("/api/config", json={"server": {"login": login}})
        assert r.status_code == 400 and r.json()["error"] == SERVER_READ_ONLY, login
    current = live.get("/api/config").json()
    r = live.patch("/api/config", json={"server": current["server"], "appearance": {"theme": "dark"}})
    assert r.status_code == 200
    assert live.app.state.cfg.server.login.password_hash == HASH


# ------------------------------------------------------------------------------ the config file


def test_a_login_that_cannot_be_read_stays_on_and_answers_503(tmp_path: Path, data_dir: Path) -> None:
    path = tmp_path / "sentinel.yaml"
    path.write_text(
        yaml.safe_dump({"server": {"login": {"username": USERNAME, "password_hash": "scrypt$x"}}})
    )
    cfg = load_config(path, demo=False)
    assert LOGIN_UNREADABLE in startup_notices(cfg)
    with make_client(cfg, data_dir, path) as c:
        assert c.get("/api/meta").status_code == 401  # never open
        assert c.get("/api/session").json()["login_required"] is True
        r = sign_in(c)
        assert r.status_code == 503 and r.json() == {"error": LOGIN_UNREADABLE}
        assert LOGIN_UNREADABLE in [e.message for e in c.app.state.store.events(kind="config")]
    assert LOGIN_UNREADABLE not in startup_notices(load_config(path, demo=True))  # the demo has no login


def test_a_password_set_with_the_cli_applies_without_a_restart(tmp_path: Path, data_dir: Path) -> None:
    path = tmp_path / "sentinel.yaml"
    save_config(login_cfg(), path)
    with make_client(load_config(path, demo=False), data_dir, path) as c:
        c.app.state.auth.reload_every_s = 0.0
        assert sign_in(c).status_code == 200 and c.get("/api/meta").status_code == 200
        cli = load_config(path, demo=False)  # what set-password does: through the file layer
        apply_login(cli, USERNAME, hash_password(NEW_PASSWORD))
        save_config(cli, path)
        assert c.get("/api/meta").status_code == 401  # signed out at the next request
        assert c.app.state.store.sessions() == []
        assert sign_in(c).status_code == 401
        assert sign_in(c, password=NEW_PASSWORD).status_code == 200
        assert "Password changed" in auth_events(c)
        # a later UI save keeps the new login (it is in the file layer now)
        assert c.patch("/api/config", json={"appearance": {"theme": "dark"}}).status_code == 200
    assert verify_password(NEW_PASSWORD, load_config(path).server.login.password_hash)


def test_a_login_removed_by_hand_never_opens_a_dashboard_on_the_network(
    tmp_path: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SENTINEL_ALLOW_OPEN", raising=False)
    for host, still_asks in (("0.0.0.0", True), ("127.0.0.1", False)):
        path = tmp_path / f"{host}.yaml"
        save_config(login_cfg(host=host), path)
        with make_client(load_config(path, demo=False), data_dir, path) as c:
            c.app.state.auth.reload_every_s = 0.0
            raw = yaml.safe_load(path.read_text())
            raw["server"]["login"] = {"username": "", "password_hash": ""}
            path.write_text(yaml.safe_dump(raw))
            assert (c.get("/api/meta").status_code == 401) is still_asks, host
            assert bool(c.app.state.cfg.server.login.password_hash) is still_asks
            if not still_asks:
                assert "Login removed" in auth_events(c)
