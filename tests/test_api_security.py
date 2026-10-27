"""API hardening: the Host allowlist (DNS rebinding), refusal of cross-site writes, the exact
``/healthz`` auth exemption, the open-bind guard and read-only server settings."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dspace_sentinel.api.app import SentinelMiddleware, UnsafeBindError, create_app
from dspace_sentinel.auth import hash_password
from dspace_sentinel.config import MASK, Config, apply_demo_overrides, merge_patch

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

LOCAL = "http://127.0.0.1"
"""TestClient base URL: an address the Host allowlist accepts (Starlette's default is ``testserver``)."""

LOGIN = {"username": "admin", "password_hash": hash_password("correct horse battery")}


def make_client(cfg: Config, data_dir: Path, config_path: Path, **kwargs: Any) -> TestClient:
    """The app without the collectors running (these tests only exercise the HTTP layer)."""
    kwargs.setdefault("start_scheduler", False)
    kwargs.setdefault("demo_backfill_days", 0)
    app = create_app(cfg, config_path=config_path, data_dir=data_dir, **kwargs)
    return TestClient(app, base_url=LOCAL)


@pytest.fixture
def api(demo_cfg: Config, data_dir: Path, config_path: Path) -> Iterator[TestClient]:
    with make_client(demo_cfg, data_dir, config_path) as c:
        yield c


# ------------------------------------------------------------------------------ Host allowlist


def test_unknown_host_names_are_refused_before_routing(api: TestClient) -> None:
    """A DNS-rebinding page reaches Sentinel with its own name in Host: nothing is served or changed."""
    evil = {"Host": "rebind.attacker.example:9765"}
    r = api.get("/api/config", headers=evil)
    assert r.status_code == 400
    assert "server.allowed_hosts" in r.json()["error"]
    assert api.get("/", headers=evil).status_code == 400
    assert api.get("/api/stream", headers=evil).status_code == 400
    r = api.patch(
        "/api/config",
        json={"postgres": {"host": "203.0.113.9"}},
        headers={"Host": "rebind.attacker.example", "Origin": "http://rebind.attacker.example"},
    )
    assert r.status_code == 400
    assert api.get("/api/config").json()["postgres"]["host"] == "localhost"
    # the probe used by monitoring stays reachable whatever the Host says
    assert api.get("/healthz", headers=evil).json() == {"ok": True}


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.0.0.1:9765",
        "localhost:9765",
        "LOCALHOST",
        "[::1]:9765",
        "10.20.30.40:9765",  # any IP literal: rebinding needs a DNS name
        socket.gethostname(),
    ],
)
def test_local_names_and_ip_literals_are_allowed(api: TestClient, host: str) -> None:
    assert api.get("/api/meta", headers={"Host": host}).status_code == 200


def test_allowed_hosts_config_and_dspace_names(data_dir: Path, config_path: Path) -> None:
    cfg = merge_patch(
        apply_demo_overrides(Config()),
        {
            "server": {"allowed_hosts": ["Sentinel.Example.org", "ops.example.org:8443"]},
            "dspace": {"public_url": "https://repo.example.org"},
        },
    )
    with make_client(cfg, data_dir, config_path) as c:
        assert c.get("/api/meta", headers={"Host": "sentinel.example.org"}).status_code == 200
        assert c.get("/api/meta", headers={"Host": "ops.example.org"}).status_code == 200
        # the nginx recipe forwards `Host $host` from the repository's own server block
        assert c.get("/api/meta", headers={"Host": "repo.example.org"}).status_code == 200
        assert c.get("/api/meta", headers={"Host": "example.org"}).status_code == 400
    wildcard = merge_patch(apply_demo_overrides(Config()), {"server": {"allowed_hosts": ["*"]}})
    with make_client(wildcard, data_dir, config_path) as c:
        assert c.get("/api/meta", headers={"Host": "anything.example"}).status_code == 200


# ------------------------------------------------------------------------------ cross-site writes


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "http://evil.example"},
        {"Origin": "http://127.0.0.1:8000"},  # another local site on a different port
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
    ],
)
def test_cross_site_writes_are_refused(api: TestClient, headers: dict[str, str]) -> None:
    """A form or no-cors fetch from another site must not wipe history, rewrite setup or make reports."""
    host = {"Host": "127.0.0.1:9765"}
    for method, path, body in (
        ("POST", "/api/actions/clear-history", None),
        ("POST", "/api/actions/setup", None),
        ("POST", "/api/reports", None),
        ("POST", "/api/alerts/1/ack", None),
        ("DELETE", "/api/reports/1", None),
        ("PATCH", "/api/config", {"appearance": {"theme": "dark"}}),
    ):
        r = api.request(method, path, headers={**host, **headers}, json=body)
        assert r.status_code == 403, (method, path, r.text)
        assert "cross-site" in r.json()["error"]
    assert api.get("/api/meta").json()["setup_completed"] is False
    assert api.get("/api/config").json()["appearance"]["theme"] == "system"


@pytest.mark.parametrize(
    "content_type",
    ["application/x-www-form-urlencoded", "multipart/form-data; boundary=x", "text/plain;charset=UTF-8"],
)
def test_form_encoded_writes_are_refused(api: TestClient, content_type: str) -> None:
    """HTML forms need no CORS preflight; the API only takes JSON (or no body at all)."""
    r = api.post("/api/actions/clear-history", headers={"Content-Type": content_type}, content=b"")
    assert r.status_code == 415
    assert "application/json" in r.json()["error"]


def test_same_origin_and_non_browser_writes_still_work(api: TestClient) -> None:
    host = {"Host": "127.0.0.1:9765"}
    browser = {**host, "Origin": "http://127.0.0.1:9765", "Sec-Fetch-Site": "same-origin"}
    assert api.post("/api/actions/clear-history", headers=browser).status_code == 200
    r = api.patch("/api/config", json={"appearance": {"theme": "dark"}}, headers=browser)
    assert r.status_code == 200 and r.json()["appearance"]["theme"] == "dark"
    # curl and the CLI send neither Origin nor Sec-Fetch-Site
    assert api.post("/api/actions/clear-history").status_code == 200
    as_json = {"Content-Type": "application/json"}
    assert api.post("/api/actions/clear-history", headers=as_json).status_code == 200
    # behind nginx (`Host $host`, no port) the page's origin carries the public name
    proxied = {"Host": "127.0.0.1", "Origin": "https://127.0.0.1", "Sec-Fetch-Site": "same-origin"}
    assert api.post("/api/actions/clear-history", headers=proxied).status_code == 200
    # a proxy that rewrites Host but reports the original in X-Forwarded-Host (Apache ProxyPass)
    apache = {
        "Host": "127.0.0.1:9765",
        "X-Forwarded-Host": "localhost:8443",
        "Origin": "https://localhost:8443",
    }
    assert api.post("/api/actions/clear-history", headers=apache).status_code == 200
    # reading is never refused: CORS already stops another site from seeing the answer
    cross = {"Origin": "http://evil.example", "Sec-Fetch-Site": "cross-site"}
    assert api.get("/api/meta", headers=cross).status_code == 200


# ------------------------------------------------------------------------------ /healthz exemption


def test_only_the_exact_healthz_path_skips_auth(data_dir: Path, config_path: Path) -> None:
    cfg = merge_patch(apply_demo_overrides(Config()), {"server": {"auth_token": "T0k3n-for-review"}})
    with make_client(cfg, data_dir, config_path) as c:
        assert c.get("/healthz").status_code == 200
        for path in ("/api/components/healthz", "/api/reports/healthz", "/static/healthz", "/x/healthz"):
            assert c.get(path).status_code == 401, path
        assert c.get("/api/components/healthz", auth=("u", "T0k3n-for-review")).status_code == 404


async def test_healthz_exemption_honours_the_root_path() -> None:
    """Mounted under a prefix (``root_path``), ``<prefix>/healthz`` is the probe; nothing else is."""
    seen: list[str] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        seen.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    cfg = merge_patch(Config(), {"server": {"auth_token": "tok"}})
    mw = SentinelMiddleware(inner, get_cfg=lambda: cfg)

    async def call(path: str, root_path: str = "") -> int:
        sent: list[dict[str, Any]] = []

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        scope = {
            "type": "http",
            "method": "GET",
            "path": path,
            "root_path": root_path,
            "headers": [(b"host", b"127.0.0.1:9765")],
        }
        await mw(scope, receive, send)
        return int(sent[0]["status"])

    assert await call("/sentinel/healthz", "/sentinel") == 200
    assert await call("/healthz") == 200
    assert await call("/sentinel/api/healthz", "/sentinel") == 401
    assert await call("/healthz/extra") == 401
    assert seen == ["/sentinel/healthz", "/healthz"]


# ------------------------------------------------------------------------------ bind address guard


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "", "10.0.0.5", "sentinel-box.example.org"])
def test_open_bind_without_auth_is_refused(
    host: str, data_dir: Path, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SENTINEL_ALLOW_OPEN", raising=False)
    cfg = merge_patch(Config(), {"server": {"host": host}})
    with pytest.raises(UnsafeBindError, match="auth_token") as refused:
        create_app(cfg, config_path=config_path, data_dir=data_dir, start_scheduler=False)
    assert "./sentinel.sh set-password" in str(refused.value)
    assert not (data_dir / "sentinel.db").exists()  # refused before anything is opened


@pytest.mark.parametrize(
    "server",
    [
        {"host": "127.0.0.1"},
        {"host": "127.0.0.2"},
        {"host": "localhost"},
        {"host": "::1"},
        {"host": "0.0.0.0", "auth_token": "s3cret-token"},
        {"host": "0.0.0.0", "login": LOGIN},
        {"host": "0.0.0.0", "login": {"username": "admin", "password_hash": "unreadable"}},
        {"host": "0.0.0.0", "allow_unauthenticated": True},
    ],
)
def test_loopback_token_login_or_explicit_opt_in_may_bind(
    server: dict[str, Any], data_dir: Path, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SENTINEL_ALLOW_OPEN", raising=False)
    cfg = merge_patch(Config(), {"server": server})
    app = create_app(cfg, config_path=config_path, data_dir=data_dir, start_scheduler=False)
    app.state.store.close()


def test_the_demo_never_counts_a_login_as_protection(
    data_dir: Path, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The demo never asks for a login, so on the network it needs a token like before."""
    monkeypatch.delenv("SENTINEL_ALLOW_OPEN", raising=False)
    cfg = apply_demo_overrides(merge_patch(Config(), {"server": {"host": "0.0.0.0", "login": LOGIN}}))
    with pytest.raises(UnsafeBindError):
        create_app(cfg, config_path=config_path, data_dir=data_dir, start_scheduler=False)


def test_sentinel_allow_open_env_opts_in(
    data_dir: Path, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SENTINEL_ALLOW_OPEN", "1")
    cfg = merge_patch(Config(), {"server": {"host": "0.0.0.0"}})
    create_app(cfg, config_path=config_path, data_dir=data_dir, start_scheduler=False).state.store.close()


def test_serve_exits_4_on_an_open_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import uvicorn

    from dspace_sentinel import cli

    monkeypatch.setenv("SENTINEL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SENTINEL_CONFIG", str(tmp_path / "sentinel.yaml"))
    monkeypatch.setenv("SENTINEL_HOST", "0.0.0.0")
    monkeypatch.delenv("SENTINEL_ALLOW_OPEN", raising=False)
    runs: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: runs.append(k))
    assert cli.main(["serve"]) == 4
    assert runs == []
    err = capsys.readouterr().err
    assert "0.0.0.0" in err and "server.auth_token" in err and "SENTINEL_ALLOW_OPEN" in err
    monkeypatch.setenv("SENTINEL_AUTH_TOKEN", "s3cret-token")
    assert cli.main(["serve"]) == 0
    assert runs and runs[-1]["host"] == "0.0.0.0"


# ------------------------------------------------------------------------------ server.* is read-only


@pytest.mark.parametrize(
    "server",
    [
        {"host": "0.0.0.0"},
        {"port": 9999},
        {"auth_token": "attacker-chosen"},
        {"hostname": "renamed"},
        {"allowed_hosts": ["rebind.attacker.example"]},
        {"allow_unauthenticated": True},
        {"login": LOGIN},
    ],
)
def test_server_settings_cannot_be_changed_over_the_api(
    api: TestClient, config_path: Path, server: dict[str, Any]
) -> None:
    r = api.patch("/api/config", json={"server": server, "appearance": {"theme": "dark"}})
    assert r.status_code == 400
    assert r.json()["error"] == "server settings are set in config/sentinel.yaml; restart to apply"
    r = api.post("/api/actions/setup", json={"persona": "full", "config_patch": {"server": server}})
    assert r.status_code == 400
    meta = api.get("/api/meta").json()
    assert meta["setup_completed"] is False and meta["theme"] == "system"
    assert not config_path.exists()


def test_unchanged_server_settings_may_be_sent_back(api: TestClient) -> None:
    """GET → edit → PATCH of the whole document keeps working (the masked token is unchanged)."""
    current = api.get("/api/config").json()
    r = api.patch("/api/config", json={"server": current["server"], "appearance": {"theme": "dark"}})
    assert r.status_code == 200 and r.json()["appearance"]["theme"] == "dark"


# ------------------------------------------------------------------------------ environment values stay out


def test_ui_saves_never_write_environment_values(
    tmp_path: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dspace_sentinel.config import load_config

    path = tmp_path / "sentinel.yaml"
    path.write_text("server:\n  port: 9765\n")
    env = {
        "SENTINEL_AUTH_TOKEN": "EnvOnlyAuthToken",
        "SENTINEL_PG_PASSWORD": "EnvOnlyPgPassword",
        # a made-up token, split in two so that secret scanners do not report it
        "SENTINEL_TELEGRAM_TOKEN": "123456:" + "EnvOnlyBotToken-0123456789abcdefghij",
        "SENTINEL_TELEGRAM_CHAT_ID": "4242",
        "SENTINEL_SMTP_PASSWORD": "EnvOnlySmtpPassword",
        "SENTINEL_PORT": "9796",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    auth = ("admin", "EnvOnlyAuthToken")
    with make_client(load_config(path), data_dir, path) as c:
        assert c.patch("/api/config", json={"appearance": {"theme": "dark"}}, auth=auth).status_code == 200
        assert c.patch("/api/modules", json={"solr": True}, auth=auth).status_code == 200
        assert c.post("/api/actions/setup", json={"persona": "server"}, auth=auth).status_code == 200
        shown = c.get("/api/config", auth=auth).json()
        assert shown["server"]["port"] == 9796
        # the Telegram and email forms send back what they got: still nothing from the environment
        tg, email = shown["notifiers"]["telegram"], shown["notifiers"]["email"]
        assert [c["from_env"] for c in tg["chats"]] == [True] and email["password"] == MASK
        patch = {"notifiers": {"telegram": {"chats": tg["chats"], "enabled": True}, "email": email}}
        assert c.patch("/api/config", json=patch, auth=auth).status_code == 200
    text = path.read_text()
    assert "EnvOnly" not in text and "4242" not in text and "9796" not in text


# ------------------------------------------------------------------------------ PostgreSQL password


def test_repointing_postgres_over_the_api_needs_the_password(api: TestClient) -> None:
    saved = api.patch("/api/config", json={"postgres": {"password": "OperatorsRealDbPassword"}})
    assert saved.status_code == 200
    r = api.patch("/api/config", json={"postgres": {"host": "203.0.113.9", "port": 5432, "password": MASK}})
    assert r.status_code == 400 and "enter the PostgreSQL password again" in r.json()["error"]
    assert api.get("/api/config").json()["postgres"]["host"] == "localhost"
    ok = api.patch("/api/config", json={"postgres": {"host": "db.example.org", "password": "typed-again"}})
    assert ok.status_code == 200 and ok.json()["postgres"]["host"] == "db.example.org"


# ------------------------------------------------------------------------------ discovery secrets


def _leaky_install(root: Path) -> Path:
    from tests.test_discovery import LEAKY_LOCAL_CFG

    (root / "config").mkdir(parents=True)
    (root / "config" / "local.cfg").write_text(LEAKY_LOCAL_CFG)
    return root


def test_discovered_secrets_stay_on_the_server(
    tmp_path: Path, data_dir: Path, config_path: Path, no_systemd: None
) -> None:
    """discover masks the DB password; setup/PATCH with the mask apply the real one server-side."""
    import json

    from tests.test_discovery import LEAKED

    install = _leaky_install(tmp_path / "dspace")
    cfg = merge_patch(Config(), {"dspace": {"install_dir": str(install)}})
    with make_client(cfg, data_dir, config_path) as c:
        found = c.post("/api/actions/discover").json()
        assert not [s for s in LEAKED if s in json.dumps(found)]
        assert found["suggested_config"]["postgres"]["password"] == MASK
        patch = {k: v for k, v in found["suggested_config"].items() if k != "modules"}
        setup = c.post("/api/actions/setup", json={"persona": "full", "config_patch": patch})
        assert setup.status_code == 200
        live = c.app.state.cfg
        assert live.postgres.password == "S3cretDbPassw0rd" and live.postgres.port == 5433
        assert live.solr.url == "http://solradmin:SolrBasicPw@127.0.0.1:8983/solr"
        assert c.get("/api/config").json()["postgres"]["password"] == MASK
    assert "S3cretDbPassw0rd" in config_path.read_text()  # saved for the collectors, like a typed one


def test_discovered_password_only_goes_to_the_discovered_server(
    tmp_path: Path, data_dir: Path, config_path: Path, no_systemd: None
) -> None:
    install = _leaky_install(tmp_path / "dspace")
    cfg = merge_patch(Config(), {"dspace": {"install_dir": str(install)}})
    with make_client(cfg, data_dir, config_path) as c:
        assert c.post("/api/actions/discover").status_code == 200
        evil = {"host": "203.0.113.9", "port": 5433, "user": "dspace", "database": "dspace", "password": MASK}
        assert c.patch("/api/config", json={"postgres": evil}).status_code == 200
        assert c.app.state.cfg.postgres.host == "203.0.113.9"
        assert c.app.state.cfg.postgres.password == ""  # nothing stored, nothing discovered sent there
        # the connection screen's Apply (PATCH with the masked suggestion) does get the password
        found = c.post("/api/actions/discover").json()["suggested_config"]
        assert c.patch("/api/config", json={"postgres": found["postgres"]}).status_code == 200
        assert c.app.state.cfg.postgres.password == "S3cretDbPassw0rd"


def test_cli_discover_prints_no_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], no_systemd: None
) -> None:
    from dspace_sentinel import cli
    from tests.test_discovery import LEAKED

    install = _leaky_install(tmp_path / "dspace")
    config = tmp_path / "sentinel.yaml"
    config.write_text(f"dspace: {{install_dir: {install}}}\n")
    monkeypatch.setenv("SENTINEL_CONFIG", str(config))
    monkeypatch.setenv("SENTINEL_DATA_DIR", str(tmp_path / "data"))
    for argv in (["discover"], ["discover", "--json"]):
        assert cli.main(argv) == 0
        out = capsys.readouterr().out
        assert not [s for s in LEAKED if s in out], argv


# ------------------------------------------------------------------------------ graceful shutdown


async def test_stream_ends_when_the_server_shuts_down(data_dir: Path, config_path: Path) -> None:
    """An open dashboard (SSE) must not keep uvicorn from finishing its graceful shutdown."""
    import asyncio
    import time
    from types import SimpleNamespace

    from dspace_sentinel.api.stream import event_stream

    app = create_app(
        apply_demo_overrides(Config()),
        config_path=config_path,
        data_dir=data_dir,
        demo_backfill_days=0,
        start_scheduler=False,
    )
    st = app.state
    ctx = SimpleNamespace(
        cfg=st.cfg,
        state=st.state,
        store=st.store,
        bus=st.bus,
        scheduler=st.scheduler,
        alert_engine=st.alert_engine,
        shutdown_event=st.shutdown_event,
    )
    try:
        gen = event_stream(ctx)  # type: ignore[arg-type]
        assert await gen.__anext__() == b": connected\n\n"
        assert b"event: status" in await gen.__anext__()
        st.shutdown_event.set()
        started = time.monotonic()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(gen.__anext__(), timeout=5)
        assert time.monotonic() - started < 2.0
    finally:
        st.store.close()


def test_shutdown_signal_hook_chains_to_the_servers_handler() -> None:
    import signal
    import threading

    from dspace_sentinel.api.app import chain_shutdown_signals

    seen: list[int] = []

    def server_handler(signum: int, frame: object) -> None:
        seen.append(signum)

    original = signal.signal(signal.SIGTERM, server_handler)
    try:
        event = threading.Event()
        restore = chain_shutdown_signals(event)
        signal.raise_signal(signal.SIGTERM)
        assert event.is_set() and seen == [signal.SIGTERM]  # uvicorn still gets its signal
        restore()
        assert signal.getsignal(signal.SIGTERM) is server_handler
        # nothing to chain to (default disposition): leave the process behaviour alone
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        chain_shutdown_signals(threading.Event())()
        assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL
    finally:
        signal.signal(signal.SIGTERM, original)


def test_serve_bounds_the_graceful_shutdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    from dspace_sentinel import cli

    monkeypatch.setenv("SENTINEL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SENTINEL_CONFIG", str(tmp_path / "sentinel.yaml"))
    runs: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: runs.append(k))
    assert cli.main(["serve"]) == 0
    assert runs[-1]["timeout_graceful_shutdown"] == 5


# ------------------------------------------------------------------------------ export and reports


def _fill_checks(store: Any, now: int, n: int) -> None:
    store.insert_check_rows((now - i, f"demo.{i % 7}", "ok", 1.5, None, "") for i in range(n))
    store.insert_component_history_rows((now - i, f"c{i % 3}", "ok", 2.0) for i in range(n))


def test_export_streams_every_row_in_batches(tmp_path: Path) -> None:
    """No silent LIMIT: the newest rows are there too, read batch by batch (flat memory)."""
    import json
    import time

    from dspace_sentinel.store import Store

    store = Store(tmp_path / "sentinel.db")
    try:
        now = int(time.time())
        _fill_checks(store, now, 5000)
        chunks = list(store.export(now - 86400, now + 1, stream=True, batch=700, meta={"hostname": "h"}))
        assert len(chunks) > 14 and max(len(c) for c in chunks) < 200_000
        data = json.loads(b"".join(chunks))
        assert data["meta"] == {"hostname": "h"} and data["range"] == {"from": now - 86400, "to": now + 1}
        assert len(data["checks"]) == 5000 and len(data["component_history"]) == 5000
        assert data["checks"][-1]["ts"] == now and data["checks"][0]["ts"] == now - 4999
        assert data["samples"] == [] and {"alerts", "incidents", "repository", "events"} <= set(data)
        assert store.export(now - 86400, now + 1)["checks"] == data["checks"]  # the dict form agrees
    finally:
        store.close()


def test_export_is_streamed_and_one_at_a_time(demo_cfg: Config, data_dir: Path, config_path: Path) -> None:
    import time

    with make_client(demo_cfg, data_dir, config_path) as c:
        now = int(time.time())
        _fill_checks(c.app.state.store, now - 5, 3000)
        r = c.get("/api/export", params={"range": "1h"})
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
        assert r.headers["cache-control"] == "no-store"
        data = r.json()
        assert len(data["checks"]) == 3000 and data["meta"]["range"] == "1h"
        flight = c.app.state.export_flight
        token = flight.try_start()
        try:
            busy = c.get("/api/export")
            assert busy.status_code == 429 and "already running" in busy.json()["error"]
            assert busy.headers["retry-after"]
        finally:
            flight.finish(token)
        assert c.get("/api/export", params={"range": "1h"}).status_code == 200  # freed after each export
        assert not flight.busy


def test_custom_report_ranges_are_bounded(api: TestClient) -> None:
    import time

    now = time.time()

    def refused(body: bytes | dict[str, Any], needle: str) -> None:
        if isinstance(body, bytes):
            r = api.post("/api/reports", content=body, headers={"Content-Type": "application/json"})
        else:
            r = api.post("/api/reports", json=body)
        assert r.status_code == 400, r.text
        assert needle in r.json()["error"]

    refused({"range": {"from": 0, "to": 4102444800}, "fmt": "json"}, "366 days")
    refused({"range": {"from": now - 400 * 86400, "to": now}, "fmt": "json"}, "366 days")
    refused({"range": {"from": now - 3600, "to": now + 86400}, "fmt": "json"}, "future")
    # Python's JSON parser turns these into infinities
    refused(b'{"range": {"from": -Infinity, "to": Infinity}, "fmt": "json"}', "finite")
    refused(b'{"range": {"from": 1e400, "to": 2e400}, "fmt": "json"}', "finite")
    refused(b'{"range": {"from": NaN, "to": 5}, "fmt": "json"}', "finite")
    ok = api.post("/api/reports", json={"range": {"from": now - 365 * 86400, "to": now + 60}, "fmt": "json"})
    assert ok.status_code == 201, ok.text


def test_one_report_generation_at_a_time(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from dspace_sentinel.reports import generator

    flight = api.app.state.report_flight
    token = flight.try_start()
    try:
        busy = api.post("/api/reports", json={"range": "24h", "fmt": "json"})
        assert busy.status_code == 429 and "already being generated" in busy.json()["error"]
        assert busy.headers["retry-after"]
    finally:
        flight.finish(token)
    assert api.post("/api/reports", json={"range": "24h", "fmt": "json"}).status_code == 201
    assert not flight.busy

    def boom(*args: Any, **kwargs: Any) -> None:
        raise ValueError("no data")

    monkeypatch.setattr(generator, "generate", boom)
    assert api.post("/api/reports", json={"range": "24h", "fmt": "json"}).status_code == 400
    assert not flight.busy  # a failed generation frees it too


# ------------------------------------------------------------------------------ response hardening


def test_report_files_are_sandboxed(api: TestClient) -> None:
    """A report is inline HTML on Sentinel's origin: no scripts, no requests, no sniffing."""
    rep = api.post("/api/reports", json={"range": "24h", "fmt": "html"}).json()
    html = api.get(f"/api/reports/{rep['id']}")
    assert html.status_code == 200
    csp = html.headers["content-security-policy"]
    assert csp == "default-src 'none'; style-src 'unsafe-inline'; img-src data:"
    assert html.headers["x-content-type-options"] == "nosniff"
    md = api.post("/api/reports", json={"range": "24h", "fmt": "md"}).json()
    assert api.get(f"/api/reports/{md['id']}").headers["x-content-type-options"] == "nosniff"


def test_internal_errors_do_not_echo_details(
    demo_cfg: Config, data_dir: Path, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(demo_cfg, config_path=config_path, data_dir=data_dir, start_scheduler=False)

    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("database at /srv/secret/path.db is locked")

    monkeypatch.setattr(app.state.store, "table_counts", broken)
    with TestClient(app, base_url=LOCAL, raise_server_exceptions=False) as c:
        r = c.post("/api/actions/clear-history")
    assert r.status_code == 500
    assert r.json() == {"error": "internal error; the Sentinel log has the details"}
