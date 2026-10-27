"""Core HTTP API in demo mode (SPEC section 9).

Every foundation route is exercised through the TestClient: meta, status, components,
history, latency, availability, repository, events, alerts, incidents, reports (501 until
the reports module lands), config GET/PATCH with secret masking, modules PATCH with hot
reload, the actions (check-now, discover, setup, clear-history), export, SSE first event,
Basic auth on/off, error envelopes and static caching headers.
"""

from __future__ import annotations

import json
import os
import stat
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from dspace_sentinel import __version__
from dspace_sentinel.api.app import create_app
from dspace_sentinel.api.routes_config import MODULE_LABELS
from dspace_sentinel.api.stream import event_stream, format_sse
from dspace_sentinel.config import MASK, MODULE_IDS, PERSONAS, Config, apply_demo_overrides, merge_patch

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def make_client(cfg: Config, data_dir: Path, config_path: Path, **kwargs: object) -> TestClient:
    """``tests.conftest.make_client`` with a Host header the allowlist accepts (not ``testserver``)."""
    kwargs.setdefault("demo_backfill_days", 1)
    app = create_app(cfg, config_path=config_path, data_dir=data_dir, **kwargs)  # type: ignore[arg-type]
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture
def client(demo_cfg: Config, data_dir: Path, config_path: Path) -> Iterator[TestClient]:
    """The app in demo mode, scheduler started, one cycle already run."""
    with make_client(demo_cfg, data_dir, config_path) as c:
        yield c


# ------------------------------------------------------------------------------ meta / status


def test_healthz_and_index(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}
    page = client.get("/")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert page.headers["cache-control"] == "no-cache"
    assert "api/status" in page.text


def test_meta(client: TestClient) -> None:
    meta = client.get("/api/meta").json()
    assert meta["version"] == __version__
    assert meta["hostname"] == "demo-dspace"
    assert meta["demo"] is True
    assert meta["setup_completed"] is False
    assert meta["dspace_modules_enabled"] is True
    assert set(meta["modules"]) == set(MODULE_IDS)
    assert all(meta["modules"].values())
    assert meta["theme"] == "system"
    assert isinstance(meta["started_ts"], float)


def test_status_payload(client: TestClient) -> None:
    status = client.get("/api/status").json()
    assert status["mood"] in {"ok", "warn", "crit", "unknown"}
    assert status["headline"] and status["subline"].startswith("demo-dspace · checked")
    assert status["hostname"] == "demo-dspace" and status["demo"] is True
    server = status["server"]
    assert server["cpu_pct"] >= 0 and server["mem_total"] > 0 and server["cores"] == 4
    assert server["status"] in {"ok", "warn", "crit"}
    assert set(server["spark"]) == {"cpu", "mem", "disk", "load"}
    assert 1 <= len(server["spark"]["cpu"]) <= 12
    ids = [c["id"] for c in status["components"]]
    assert "server" not in ids
    assert ids[:4] == ["postgres", "solr", "backend", "frontend"]
    assert {"nginx", "ssl", "ports", "logs", "repository", "assetstore"} <= set(ids)
    postgres = next(c for c in status["components"] if c["id"] == "postgres")
    assert [r["id"] for r in postgres["chain"]] == [
        "postgres.service",
        "postgres.tcp",
        "postgres.connect",
        "postgres.query",
    ]
    assert postgres["metrics"]["max_connections"] == 100
    assert isinstance(status["alerts_open"], list)
    assert status["repository"]["items"] > 27_000
    assert status["next_check_in_s"] is not None and status["next_check_in_s"] >= 0
    assert status["checked_ago_s"] is not None


def test_components_list_and_detail(client: TestClient) -> None:
    comps = client.get("/api/components").json()
    assert comps[0]["id"] == "server"
    assert len(comps) == 12
    detail = client.get("/api/components/backend").json()
    assert detail["id"] == "backend" and detail["depends_on"] == ["postgres", "solr"]
    rung_ids = [r["id"] for r in detail["chain"]]
    assert rung_ids[-2:] == ["backend.dep.postgres", "backend.dep.solr"]
    assert detail["latency_series_1h"]["check"] == "backend.rest"
    assert isinstance(detail["latency_series_1h"]["points"], list)
    assert isinstance(detail["recent_events"], list)
    assert "dspace" in detail["config_used"]
    assert detail["affected"] == ["DSpace Search", "DSpace Frontend", "Repository site", "nginx"]
    missing = client.get("/api/components/nope")
    assert missing.status_code == 404 and "error" in missing.json()


def test_security_component_through_the_api(client: TestClient) -> None:
    """The demo's Security component reaches /api/status and /api/components/security like any other."""
    status = client.get("/api/status").json()
    ids = [c["id"] for c in status["components"]]
    assert ids.index("security") == ids.index("ports") + 1
    detail = client.get("/api/components/security").json()
    assert detail["label"] == "Security" and detail["depends_on"] == [] and detail["affected"] == []
    assert [r["id"] for r in detail["chain"]] == [
        "security.ports",
        "security.firewall",
        "security.ssh",
        "security.web",
        "security.updates",
        "security.software",
        "security.files",
        "security.sentinel",
    ]
    assert detail["status"] == "ok" and detail["reason"] is None  # the demo starts calm
    metrics = detail["metrics"]
    assert {"ports", "listening", "reachable", "exposed", "firewall", "ssh", "web", "updates"} <= set(metrics)
    assert {"software", "files", "sentinel"} <= set(metrics)
    assert set(metrics["sentinel"]) >= {"host", "port", "auth"}
    # "Settings used": the port rules and the dspace settings (public address, install folder)
    assert set(detail["config_used"]) == {"ports", "dspace"}
    assert "public_url" in detail["config_used"]["dspace"] and detail["config_used"]["ports"]
    assert all(alert["component"] != "security" for alert in status["alerts_open"])


def test_component_detail_masks_secrets(client: TestClient) -> None:
    client.patch("/api/config", json={"postgres": {"password": "hunter2"}})
    detail = client.get("/api/components/postgres").json()
    assert detail["config_used"]["postgres"]["password"] == MASK


# ------------------------------------------------------------------------------ history


def test_history_metrics(client: TestClient) -> None:
    cpu = client.get("/api/history", params={"metric": "cpu", "range": "24h"}).json()
    assert cpu["metric"] == "cpu" and cpu["unit"] == "%" and cpu["bucket_s"] == 300
    assert len(cpu["points"]) > 200  # one day of 60 s backfill in 300 s buckets
    ts, avg, mx = cpu["points"][0]
    assert isinstance(ts, int) and mx >= avg
    assert cpu["thresholds"] == {"warning": 80, "critical": 95}
    load = client.get("/api/history", params={"metric": "load", "range": "1h"}).json()
    assert load["bucket_s"] == 15 and load["thresholds"]["per_core"] is True
    assert load["thresholds"]["warning"] == pytest.approx(1.5 * 4)
    net = client.get("/api/history", params={"metric": "net", "range": "6h"}).json()
    assert net["bucket_s"] == 60 and set(net["series"]) == {"rx", "tx"} and net["series"]["rx"]
    assert "points" not in net
    for rng, bucket in (("7d", 1800), ("30d", 7200)):
        assert client.get("/api/history", params={"metric": "mem", "range": rng}).json()["bucket_s"] == bucket
    bad = client.get("/api/history", params={"metric": "bogus"})
    assert bad.status_code == 400 and "error" in bad.json()
    bad_range = client.get("/api/history", params={"metric": "cpu", "range": "2y"})
    assert bad_range.status_code == 400 and "invalid range" in bad_range.json()["error"]


def test_latency_history(client: TestClient) -> None:
    data = client.get("/api/history/latency", params={"check": "backend.rest", "range": "24h"}).json()
    assert data["check"] == "backend.rest" and data["unit"] == "ms"
    assert data["points"] and data["points"][0][2] >= data["points"][0][1]
    assert data["thresholds"] == {"warning": 1000, "critical": 3000}
    pg = client.get("/api/history/latency", params={"check": "postgres.query"}).json()
    assert pg["thresholds"] is None and pg["points"]
    bad = client.get("/api/history/latency", params={"check": "mail.smtp"})
    assert bad.status_code == 400


def test_availability(client: TestClient) -> None:
    data = client.get("/api/availability", params={"range": "24h"}).json()
    assert data["bucket_s"] == 900
    by_id = {c["id"]: c for c in data["components"]}
    assert set(by_id) == {
        "server",
        "postgres",
        "solr",
        "backend",
        "frontend",
        "nginx",
        "ssl",
        "ports",
        "security",
        "logs",
        "repository",
        "assetstore",
    }
    assert by_id["security"]["label"] == "Security" and by_id["security"]["emoji"] == "🛡️"
    pg = by_id["postgres"]
    assert pg["label"] == "PostgreSQL" and pg["emoji"] == "🐘"
    assert len(pg["buckets"]) == 96 and pg["uptime_pct"] == 100.0
    solr = by_id["solr"]
    assert solr["uptime_pct"] is not None and solr["uptime_pct"] < 100.0  # scripted outage 0.9 d ago
    assert solr["outages"] >= 1 and "crit" in solr["buckets"]


def test_repository_and_events(client: TestClient) -> None:
    repo = client.get("/api/repository").json()
    assert repo["enabled"] is True and repo["latest"]["bitstreams"] > 81_000
    assert repo["series"]["items"] and repo["delta"]["items"] >= 0
    assert set(repo["growth_per_day"]) == {"items", "bitstreams", "assetstore_bytes", "db_size"}
    events = client.get("/api/events", params={"limit": 50}).json()
    assert events and {"ts", "kind", "component", "message"} <= set(events[0])
    assert any(e["kind"] == "demo" for e in events)
    assert client.get("/api/events", params={"limit": 0}).status_code == 400
    incident_events = client.get("/api/events", params={"kind": "incident"}).json()
    assert incident_events and all(e["kind"] == "incident" for e in incident_events)


# ------------------------------------------------------------------------------ alerts


def test_alerts_and_incidents(client: TestClient) -> None:
    assert client.get("/api/alerts").json() == []  # nothing open in the calm phase
    resolved = client.get("/api/alerts", params={"status": "resolved"}).json()
    assert len(resolved) == 6  # one back-filled incident (six alerts) inside the one-day window
    every = client.get("/api/alerts", params={"status": "all", "limit": 3}).json()
    assert len(every) == 3
    alert_id = resolved[0]["id"]
    acked = client.post(f"/api/alerts/{alert_id}/ack").json()
    assert acked["id"] == alert_id and acked["acked_ts"] is not None
    assert client.get(f"/api/alerts/{alert_id}").json()["acked_ts"] == acked["acked_ts"]
    assert client.post(f"/api/alerts/{alert_id}/unack").json()["acked_ts"] is None
    assert client.post("/api/alerts/9999/ack").status_code == 404
    assert client.get("/api/alerts", params={"status": "weird"}).status_code == 400

    incidents = client.get("/api/incidents").json()
    assert len(incidents) == 1 and incidents[0]["hypothesis"] == "Possible Solr resource exhaustion"
    assert incidents[0]["affected"] == ["DSpace Search", "DSpace Backend"]
    detail = client.get(f"/api/incidents/{incidents[0]['id']}").json()
    assert len(detail["alerts"]) == 6 and detail["sequence"][0]["text"] == "Memory 81 %"
    assert client.get("/api/incidents/404").status_code == 404
    assert client.get("/api/incidents", params={"status": "open"}).json() == []


# ------------------------------------------------------------------------------ reports


def test_reports_routes_wired(client: TestClient) -> None:
    """The reports module is exercised in tests/test_api_reports.py; here only the wiring."""
    assert client.get("/api/reports").json() == []
    r = client.post("/api/reports", json={"range": "24h", "fmt": "html"})
    assert r.status_code == 201 and r.json()["fmt"] == "html"
    assert client.delete(f"/api/reports/{r.json()['id']}").status_code == 200
    assert client.post("/api/reports", json={"range": "24h", "fmt": "pdf"}).status_code == 400
    assert client.post("/api/reports", json={"range": {"from": 10, "to": 5}}).status_code == 400
    assert client.get("/api/reports/999").status_code == 404
    assert client.delete("/api/reports/999").status_code == 404
    assert client.post("/api/reports/999/restore").status_code == 404


# ------------------------------------------------------------------------------ config


def test_config_get_is_masked(client: TestClient) -> None:
    cfg = client.get("/api/config").json()
    assert cfg["demo"] is True
    assert cfg["postgres"]["password"] == ""  # empty secrets stay empty
    assert cfg["server"]["hostname"] == "demo-dspace"
    assert all(cfg["modules"].values())


def test_config_patch_masks_and_keeps_secrets(client: TestClient, config_path: Path) -> None:
    out = client.patch(
        "/api/config",
        json={"postgres": {"password": "s3cret"}, "notifiers": {"email": {"password": "app-pass"}}},
    ).json()
    assert out["postgres"]["password"] == MASK
    assert out["notifiers"]["email"]["password"] == MASK
    assert config_path.exists()
    assert stat.S_IMODE(os.stat(config_path).st_mode) == 0o600
    # the demo saves only what the UI changed and never a secret (the running app keeps them)
    assert "s3cret" not in config_path.read_text() and "app-pass" not in config_path.read_text()
    assert client.app.state.cfg.postgres.password == "s3cret"
    assert client.app.state.cfg.notifiers.email.password == "app-pass"
    # sending the mask back keeps the stored secret; other keys still change
    out = client.patch(
        "/api/config",
        json={"postgres": {"password": MASK, "sslmode": "require"}, "collector": {"system_interval": 20}},
    ).json()
    assert out["postgres"]["password"] == MASK and out["postgres"]["sslmode"] == "require"
    assert out["collector"]["system_interval"] == 20
    assert client.app.state.cfg.postgres.password == "s3cret"
    saved = yaml.safe_load(config_path.read_text())
    assert saved == {"postgres": {"sslmode": "require"}, "collector": {"system_interval": 20}}
    assert "demo" not in saved  # runtime flag never written
    # the running app uses the new values (hot reload) and remembers demo mode
    meta = client.get("/api/meta").json()
    assert meta["system_interval"] == 20 and meta["demo"] is True
    groups = client.app.state.scheduler.describe()["groups"]
    assert groups["system_interval"]["interval"] == 20
    events = client.get("/api/events", params={"kind": "config"}).json()
    assert events and events[0]["message"].startswith("Configuration updated")


# made-up tokens, split in two so that secret scanners do not report them
BOT_A = "123456789:" + "AAH-api-core-test-token-0000000001"
BOT_B = "987654321:" + "AAH-api-core-test-token-0000000002"


def test_telegram_bots_over_the_api(client: TestClient, config_path: Path) -> None:
    """Each destination is its own bot: tokens masked, kept by id, lists replaced wholesale."""
    bots = [
        {"label": "Danindu", "bot_token": BOT_A, "chat_id": "111111"},
        {"label": "Team", "bot_token": BOT_B, "chat_id": "-100222", "min_severity": "critical"},
    ]
    r = client.patch("/api/config", json={"notifiers": {"telegram": {"enabled": True, "chats": bots}}})
    assert r.status_code == 200, r.text
    shown = r.json()["notifiers"]["telegram"]["chats"]
    assert [(c["label"], c["bot_token"], c["chat_id"], c["from_env"]) for c in shown] == [
        ("Danindu", MASK, "111111", False),
        ("Team", MASK, "-100222", False),
    ]
    assert all(len(c["id"]) == 8 for c in shown) and BOT_A not in r.text and BOT_B not in r.text
    assert BOT_A not in config_path.read_text()  # the demo never saves a token
    # the UI sends the list back as it got it (masked tokens), minus one bot
    r = client.patch("/api/config", json={"notifiers": {"telegram": {"chats": [shown[1]]}}})
    assert r.status_code == 200 and [c["label"] for c in r.json()["notifiers"]["telegram"]["chats"]] == [
        "Team"
    ]
    assert [c.bot_token for c in client.app.state.cfg.notifiers.telegram.chats] == [BOT_B]
    # a masked token whose bot is gone must be typed again
    r = client.patch("/api/config", json={"notifiers": {"telegram": {"chats": [shown[0]]}}})
    assert r.status_code == 400 and r.json()["error"] == "enter the bot token for Danindu again"
    six = [{"bot_token": BOT_A, "chat_id": str(n)} for n in range(6)]
    r = client.patch("/api/config", json={"notifiers": {"telegram": {"chats": six}}})
    assert r.status_code == 400 and "Telegram can send to at most 5 bots" in r.json()["error"]
    r = client.patch(
        "/api/config", json={"notifiers": {"telegram": {"chats": [{"bot_token": "123:abc", "chat_id": "1"}]}}}
    )
    assert (
        r.status_code == 400 and "not a Telegram bot token (it looks like 123456789:ABC…" in r.json()["error"]
    )
    assert "123:abc" not in r.json()["error"]
    # an older client's single bot_token + chat_id still edits the (first) bot
    r = client.patch(
        "/api/config", json={"notifiers": {"telegram": {"bot_token": MASK, "chat_id": "-100333"}}}
    )
    assert r.status_code == 200
    assert [(c.label, c.bot_token, c.chat_id) for c in client.app.state.cfg.notifiers.telegram.chats] == [
        ("Team", BOT_B, "-100333")
    ]


def test_email_settings_over_the_api(client: TestClient) -> None:
    r = client.patch(
        "/api/config",
        json={
            "notifiers": {
                "email": {
                    "enabled": True,
                    "username": "sentinel@gmail.com",
                    "password": "abcd efgh ijkl mnop",
                    "recipients": [
                        {"address": "ops@example.org", "label": "On call", "min_severity": "critical"}
                    ],
                }
            }
        },
    )
    assert r.status_code == 200, r.text
    email = r.json()["notifiers"]["email"]
    assert (
        email["password"] == MASK
        and email["smtp_host"] == "smtp.gmail.com"
        and email["security"] == "starttls"
    )
    assert email["recipients"] == [
        {"address": "ops@example.org", "label": "On call", "min_severity": "critical"}
    ]
    assert client.app.state.cfg.notifiers.email.password == "abcdefghijklmnop"
    for bad, message in (
        ({"recipients": [{"address": "Ops <ops@example.org>"}]}, "not an email address"),
        (
            {"recipients": [{"address": f"r{n}@example.org"} for n in range(6)]},
            "email can go to at most 5 recipients",
        ),
        (
            {"recipients": [{"address": "a@example.org"}, {"address": "A@example.org"}]},
            "a@example.org is listed twice",
        ),
        ({"security": "tls"}, "security"),
        ({"from_name": "x\r\nBcc: y@z.org"}, "one line"),
    ):
        r = client.patch("/api/config", json={"notifiers": {"email": bad}})
        assert r.status_code == 400 and message in r.json()["error"], (bad, r.text)
    # lists are replaced wholesale
    r = client.patch("/api/config", json={"notifiers": {"email": {"recipients": []}}})
    assert r.json()["notifiers"]["email"]["recipients"] == []


@pytest.mark.parametrize("channel", ["telegram", "email"])
def test_quiet_hours_with_equal_times_are_refused_over_the_api(
    channel: str, client: TestClient, config_path: Path
) -> None:
    """notify-validation#4: '23:00-23:00' was saved (200) and then never quiet."""
    ok = client.patch("/api/config", json={"notifiers": {channel: {"quiet_hours": "22:00-06:00"}}})
    assert ok.status_code == 200 and ok.json()["notifiers"][channel]["quiet_hours"] == "22:00-06:00"
    before = config_path.read_text()
    for value in ("23:00-23:00", "00:00 – 0:00"):
        r = client.patch(
            "/api/config", json={"notifiers": {channel: {"quiet_hours": value, "daily_digest": "09:00"}}}
        )
        assert r.status_code == 400, r.text
        assert r.json() == {
            "error": f"invalid configuration: notifiers.{channel}.quiet_hours: "
            "quiet hours must start and end at different times"
        }
    # nothing of the refused patch was applied or saved
    shown = client.get("/api/config").json()["notifiers"][channel]
    assert (shown["quiet_hours"], shown["daily_digest"]) == ("22:00-06:00", "")
    assert config_path.read_text() == before


def test_config_patch_validation(client: TestClient) -> None:
    bad = client.patch("/api/config", json={"server": {"port": 70000}})
    assert bad.status_code == 400 and bad.json()["error"].startswith("invalid configuration")
    assert client.patch("/api/config", json={"appearance": {"theme": "neon"}}).status_code == 400
    not_object = client.patch("/api/config", content=b"[]", headers={"content-type": "application/json"})
    assert not_object.status_code in (400, 422)
    assert client.get("/api/config").json()["server"]["port"] == 9765


def test_modules_patch_hot_reloads(client: TestClient) -> None:
    scheduler = client.app.state.scheduler
    assert "solr" in scheduler.enabled_component_ids()
    out = client.patch("/api/modules", json={"solr": False, "ssl": False}).json()
    assert out["modules"]["solr"] is False and out["modules"]["ssl"] is False
    assert "solr" not in scheduler.enabled_component_ids()
    ids = {c["id"] for c in client.get("/api/components").json()}
    assert "solr" not in ids and "ssl" not in ids
    assert client.get("/api/components/solr").status_code == 404
    assert client.get("/api/meta").json()["modules"]["solr"] is False
    availability = {c["id"] for c in client.get("/api/availability").json()["components"]}
    assert "solr" not in availability
    # a fresh check leaves the disabled modules out of the status too
    status = client.post("/api/actions/check-now").json()
    assert "solr" not in {c["id"] for c in status["components"]}
    backend = next(c for c in status["components"] if c["id"] == "backend")
    rung_ids = [r["id"] for r in backend["chain"]]
    assert "backend.dep.postgres" in rung_ids and "backend.dep.solr" not in rung_ids  # no trace
    assert backend["status"] == "ok"
    # switching back on restores the component after the next check
    client.patch("/api/modules", json={"solr": True})
    assert "solr" in scheduler.enabled_component_ids()
    status = client.post("/api/actions/check-now").json()
    assert "solr" in {c["id"] for c in status["components"]}
    backend = next(c for c in status["components"] if c["id"] == "backend")
    assert "backend.dep.solr" in [r["id"] for r in backend["chain"]]
    # the events log uses the words of the More › Modules toggles
    messages = [e["message"] for e in client.get("/api/events", params={"kind": "config"}).json()]
    assert "Solr turned off, SSL certificate turned off" in messages
    assert "Solr turned on" in messages


def test_every_module_has_its_toggle_name_in_the_events_log(client: TestClient) -> None:
    """The events log says "Security turned off", in the words of More › Modules, never "security"."""
    assert list(MODULE_LABELS) == list(MODULE_IDS)
    client.patch("/api/modules", json={"security": False})
    messages = [e["message"] for e in client.get("/api/events", params={"kind": "config"}).json()]
    assert "Security turned off" in messages


def test_modules_patch_validation(client: TestClient) -> None:
    assert client.patch("/api/modules", json={"mail": True}).status_code == 400
    assert client.patch("/api/modules", json={"solr": "yes"}).status_code == 400
    assert client.get("/api/modules").json()["modules"]["solr"] is True


# ------------------------------------------------------------------------------ actions


def test_check_now_returns_status(client: TestClient) -> None:
    before = client.get("/api/status").json()["checked_at"]
    status = client.post("/api/actions/check-now").json()
    assert status["checked_at"] >= before
    assert status["checking"] is False and status["mood"] in {"ok", "warn", "crit", "unknown"}
    assert len(status["components"]) == 11


def test_discover(client: TestClient, no_systemd: None) -> None:
    result = client.post("/api/actions/discover").json()
    assert isinstance(result["findings"], list) and result["findings"]
    assert result["detected_persona"] in PERSONAS
    assert "modules" in result["suggested_config"]
    assert isinstance(result["ports"], list)


def test_setup_applies_persona_and_patch(client: TestClient, config_path: Path) -> None:
    meta = client.post(
        "/api/actions/setup",
        json={"persona": "server", "config_patch": {"appearance": {"theme": "dark"}}},
    ).json()
    assert meta["setup_completed"] is True and meta["persona"] == "server"
    assert meta["theme"] == "dark"
    expected = PERSONAS["server"]["modules"]
    assert meta["modules"] == expected
    assert meta["dspace_modules_enabled"] is False
    saved = yaml.safe_load(config_path.read_text())
    assert saved["setup"] == {"completed": True, "persona": "server"}
    assert saved["appearance"] == {"theme": "dark"}
    # demo: only the modules that differ from the demo (everything on) are written
    assert saved["modules"]["postgres"] is False and "nginx" not in saved["modules"]
    ids = {c["id"] for c in client.get("/api/components").json()}
    assert ids == {"server", "nginx", "ssl", "ports", "security", "logs"}
    assert client.post("/api/actions/setup", json={"persona": "wizard"}).status_code == 400
    assert client.post("/api/actions/setup", json={"config_patch": []}).status_code == 400


def test_setup_without_persona_only_completes(client: TestClient) -> None:
    meta = client.post("/api/actions/setup", json={}).json()
    assert meta["setup_completed"] is True and meta["persona"] == ""
    assert all(meta["modules"].values())


def test_demo_telegram_test_is_logged_not_sent(client: TestClient) -> None:
    """The UI says the demo test message "is in the events log": it must be."""
    from dspace_sentinel.alerts import messages

    result = client.post("/api/actions/telegram-test").json()
    assert result == {
        "ok": False,
        "bot_name": None,
        "error": "demo mode: nothing is sent to Telegram",
        "skipped": "demo",
        "results": [],
    }
    # an id that is not configured is refused first; a configured one is logged like the rest
    r = client.post("/api/actions/telegram-test", json={"id": "nope1234"})
    assert r.status_code == 400 and r.json()["error"] == "no Telegram bot with id 'nope1234' is configured"
    bots = {"chats": [{"label": "Me", "bot_token": BOT_A, "chat_id": "7"}]}
    bot_id = client.patch("/api/config", json={"notifiers": {"telegram": bots}}).json()["notifiers"][
        "telegram"
    ]["chats"][0]["id"]
    one = client.post("/api/actions/telegram-test", json={"id": bot_id}).json()
    assert one["skipped"] == "demo" and one["results"] == []
    events = client.get("/api/events", params={"kind": "notify.demo"}).json()
    tests = [e for e in events if e["data"].get("kind") == "test"]
    assert len(tests) == 2
    html = messages.test_html("demo-dspace")
    assert tests[0]["message"] == f"Test message (demo, not sent): {messages.to_plain(html)}"
    assert tests[0]["data"]["channels"] == ["telegram"]
    assert tests[0]["data"]["html"] == html and tests[0]["data"]["text"] == messages.to_plain(html)


def test_demo_email_test_is_logged_not_sent(client: TestClient) -> None:
    r = client.post("/api/actions/email-test", json={"address": "nobody@example.org"})
    assert (
        r.status_code == 400 and r.json()["error"] == "no email recipient 'nobody@example.org' is configured"
    )
    assert client.post("/api/actions/email-test").json() == {
        "ok": False,
        "error": "demo mode: nothing is sent by email",
        "skipped": "demo",
        "results": [],
    }
    (row,) = client.get("/api/events", params={"kind": "notify.demo"}).json()
    assert row["message"] == "Test email (demo, not sent): [Sentinel] Test email · demo-dspace"
    assert row["data"]["channels"] == ["email"] and row["data"]["kind"] == "test"
    assert "DSpace Sentinel is connected" in row["data"]["html"] and "reached you" in row["data"]["text"]


class _Relay:
    """smtplib.SMTP stand-in: takes every recipient except boss@example.org."""

    def __init__(self, host: str, port: int, **kwargs: Any) -> None:
        pass

    def ehlo(self) -> None:
        pass

    def starttls(self, context: Any) -> None:
        pass

    def login(self, user: str, password: str) -> None:
        pass

    def send_message(self, msg: Any, from_addr: str, to_addrs: list[str]) -> dict[str, Any]:
        return {a: (550, b"5.1.1 no such mailbox") for a in to_addrs if a == "boss@example.org"}

    def quit(self) -> None:
        pass


def test_email_test_answers_per_recipient(
    data_dir: Path, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", _Relay)
    email = {
        "username": "sentinel@gmail.com",
        "password": "abcdefghijklmnop",
        "recipients": [{"address": "ops@example.org", "label": "On call"}, {"address": "boss@example.org"}],
    }
    cfg = merge_patch(Config(), {"notifiers": {"email": email}})  # switched off: the test still goes out
    with make_client(cfg, data_dir, config_path) as c:
        r = c.post("/api/actions/email-test").json()
        assert r["ok"] is False and r["error"].startswith(
            "1 of 2 recipients failed — boss@example.org: refused"
        )
        assert [(x["address"], x["label"], x["ok"]) for x in r["results"]] == [
            ("ops@example.org", "On call", True),
            ("boss@example.org", "", False),
        ]
        one = c.post("/api/actions/email-test", json={"address": "OPS@example.org"}).json()
        assert one == {
            "ok": True,
            "error": None,
            "results": [{"address": "ops@example.org", "label": "On call", "ok": True, "error": None}],
        }
        c.patch("/api/config", json={"notifiers": {"email": {"password": ""}}})
        assert c.post("/api/actions/email-test").json() == {
            "ok": False,
            "error": "email is not set up: add the Gmail address, app password and at least one recipient",
            "results": [],
        }


class _Recorder(_Relay):
    """smtplib.SMTP stand-in that remembers where it connected and who logged in."""

    logins: list[tuple[str, int, str, str]] = []

    def __init__(self, host: str, port: int, **kwargs: Any) -> None:
        self.where = (host, port)

    def login(self, user: str, password: str) -> None:
        _Recorder.logins.append((*self.where, user, password))


def test_the_saved_email_password_never_follows_a_new_server(
    data_dir: Path, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1#1: re-pointing the SMTP server with the mask must not hand the app password to it."""
    import smtplib

    _Recorder.logins = []
    monkeypatch.setattr(smtplib, "SMTP", _Recorder)
    email = {
        "smtp_host": "smtp.gmail.com",
        "username": "sentinel.alerts@gmail.com",
        "password": "abcdefghijklmnop",
        "recipients": [{"address": "ops@example.org"}],
    }
    cfg = merge_patch(Config(), {"notifiers": {"email": email}, "postgres": {"password": "db-secret"}})
    with make_client(cfg, data_dir, config_path) as c:
        # the PostgreSQL rule, as a control
        r = c.patch("/api/config", json={"postgres": {"host": "127.0.0.1", "port": 9852, "password": MASK}})
        assert r.status_code == 400 and r.json()["error"].startswith("enter the PostgreSQL password again")
        repoint = {"smtp_host": "127.0.0.1", "smtp_port": 9850, "security": "none", "password": MASK}
        r = c.patch("/api/config", json={"notifiers": {"email": repoint}})
        assert r.status_code == 400
        assert r.json()["error"] == (
            "enter the email password again: the SMTP server, port, security or username changed and "
            "the saved password is only sent to the server it was saved for"
        )
        assert c.app.state.cfg.notifiers.email.smtp_host == "smtp.gmail.com"
        assert c.post("/api/actions/email-test").json()["ok"] is True
        assert _Recorder.logins == [("smtp.gmail.com", 587, "sentinel.alerts@gmail.com", "abcdefghijklmnop")]
        # typing a password for the new server is fine: that one goes there
        r = c.patch("/api/config", json={"notifiers": {"email": {**repoint, "password": "relay-password"}}})
        assert r.status_code == 200 and c.app.state.cfg.notifiers.email.password == "relay-password"


def test_an_smtp_password_from_the_environment_cannot_be_changed_over_the_api(
    data_dir: Path, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1#6: More › Email shows it read-only; a PATCH that changes it answers 400 (it would come
    back from config/sentinel.env at the next restart)."""
    from dspace_sentinel.config import load_config

    config_path.parent.mkdir(parents=True, exist_ok=True)
    email = {"username": "sentinel.alerts@gmail.com", "recipients": [{"address": "ops@example.org"}]}
    config_path.write_text(yaml.safe_dump({"notifiers": {"email": email}}))
    monkeypatch.setenv("SENTINEL_SMTP_PASSWORD", "envpassword")
    with make_client(load_config(config_path), data_dir, config_path) as c:
        shown = c.get("/api/config").json()["notifiers"]["email"]
        assert shown["password"] == MASK and shown["password_from_env"] is True
        r = c.patch("/api/config", json={"notifiers": {"email": {"password": "newrotatedpassword"}}})
        assert r.status_code == 400 and r.json()["error"] == (
            "the email password comes from SENTINEL_SMTP_PASSWORD in config/sentinel.env: change it there"
        )
        r = c.patch("/api/config", json={"notifiers": {"email": {**shown, "enabled": True}}})
        assert r.status_code == 200 and c.app.state.cfg.notifiers.email.password == "envpassword"
        assert "envpassword" not in config_path.read_text()


def test_an_environment_bot_left_out_is_one_row_in_the_events_log(
    data_dir: Path, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1#extra1: Sentinel starts with the file's five bots and says why the sixth is missing."""
    from dspace_sentinel.config import load_config

    config_path.parent.mkdir(parents=True, exist_ok=True)
    five = [{"label": f"P{n}", "bot_token": BOT_A, "chat_id": str(n)} for n in range(1, 6)]
    config_path.write_text(yaml.safe_dump({"notifiers": {"telegram": {"chats": five}}}))
    monkeypatch.setenv("SENTINEL_TELEGRAM_TOKEN", BOT_B)
    monkeypatch.setenv("SENTINEL_TELEGRAM_CHAT_ID", "-100999")
    with make_client(load_config(config_path), data_dir, config_path) as c:
        rows = c.get("/api/events", params={"kind": "config"}).json()
        notes = [r["message"] for r in rows if "SENTINEL_TELEGRAM_TOKEN" in r["message"]]
        assert notes == [
            "The Telegram bot from SENTINEL_TELEGRAM_TOKEN was left out: there are already 5 bots"
        ]
        assert len(c.get("/api/config").json()["notifiers"]["telegram"]["chats"]) == 5


def test_a_saved_login_that_is_not_an_address_starts_and_email_is_not_ready(
    data_dir: Path, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """notify-validation#2: {smtp.sendgrid.net, apikey, a password, email off} and no From address
    stopped the start. It starts; the events log says why email is not ready, the test email names
    the same sentence without opening a connection, and the API still refuses such settings."""
    import smtplib

    from dspace_sentinel.config import load_config

    _Recorder.logins = []
    monkeypatch.setattr(smtplib, "SMTP", _Recorder)
    sentence = "the SMTP username is not an email address: set from_address (the address alerts come from)"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    sendgrid = {
        "enabled": False,
        "smtp_host": "smtp.sendgrid.net",
        "username": "apikey",
        "password": "SG.secret-key",
        "recipients": [{"address": "ops@example.org"}],
    }
    config_path.write_text(yaml.safe_dump({"setup": {"completed": True}, "notifiers": {"email": sendgrid}}))
    before = config_path.read_text()
    with make_client(load_config(config_path), data_dir, config_path) as c:
        rows = c.get("/api/events", params={"kind": "config"}).json()
        assert [r["message"] for r in rows] == [f"Email is not ready: {sentence}"]
        assert c.post("/api/actions/email-test").json() == {"ok": False, "error": sentence, "results": []}
        assert _Recorder.logins == []
        shown = c.get("/api/config").json()["notifiers"]["email"]
        assert shown["username"] == "apikey" and shown["password"] == MASK and shown["from_address"] == ""
        # the form as it is, or the switch alone: refused, nothing saved
        for patch in (shown, {"enabled": True}):
            r = c.patch("/api/config", json={"notifiers": {"email": patch}})
            assert r.status_code == 400, r.text
            assert r.json() == {"error": f"invalid configuration: notifiers.email: {sentence}"}
        assert config_path.read_text() == before
        # other settings are not held up by it
        assert c.patch("/api/config", json={"appearance": {"theme": "dark"}}).status_code == 200
        assert c.patch("/api/modules", json={"ports": True}).status_code == 200
        # with the address alerts come from, the form saves and the test email goes out
        r = c.patch(
            "/api/config",
            json={"notifiers": {"email": {**shown, "enabled": True, "from_address": "alerts@example.org"}}},
        )
        assert r.status_code == 200, r.text
        assert c.post("/api/actions/email-test").json()["ok"] is True
        assert _Recorder.logins == [("smtp.sendgrid.net", 587, "apikey", "SG.secret-key")]
        rows = c.get("/api/events", params={"kind": "config"}).json()
        assert [r["message"] for r in rows].count(f"Email is not ready: {sentence}") == 1


def test_a_password_smtp_cannot_send_starts_not_ready_and_is_refused_over_the_api(
    data_dir: Path, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """email-password#3: a password with a character outside ASCII (smtplib sends the login as
    ASCII) in the settings file: Sentinel starts, the events log and the test email say why email
    is not ready without opening a connection, and saving another such password answers 400."""
    import smtplib

    from dspace_sentinel.config import load_config

    _Recorder.logins = []
    monkeypatch.setattr(smtplib, "SMTP", _Recorder)
    sentence = (
        "the email password has a character that cannot be sent: "
        "use letters, digits and symbols from the English keyboard"
    )
    config_path.parent.mkdir(parents=True, exist_ok=True)
    email = {
        "enabled": False,  # the test email goes out all the same; no alert can send one meanwhile
        "username": "sentinel@example.org",
        "password": "pässwort-123",
        "recipients": [{"address": "ops@example.org"}],
    }
    tree = {"setup": {"completed": True}, "notifiers": {"email": email}}
    config_path.write_text(yaml.safe_dump(tree, allow_unicode=True), encoding="utf-8")
    before = config_path.read_text(encoding="utf-8")
    with make_client(load_config(config_path), data_dir, config_path) as c:
        rows = c.get("/api/events", params={"kind": "config"}).json()
        assert [r["message"] for r in rows] == [f"Email is not ready: {sentence}"]
        assert c.post("/api/actions/email-test").json() == {"ok": False, "error": sentence, "results": []}
        assert _Recorder.logins == []
        for typed in ("nöch-ein-passwort", "hunter2 ✓"):
            r = c.patch("/api/config", json={"notifiers": {"email": {"password": typed}}})
            assert r.status_code == 400, r.text
            assert r.json() == {"error": f"invalid configuration: notifiers.email: {sentence}"}
        assert config_path.read_text(encoding="utf-8") == before
        # an app password that can be sent mends it, and the test email goes out
        r = c.patch("/api/config", json={"notifiers": {"email": {"password": "abcd efgh ijkl mnop"}}})
        assert r.status_code == 200, r.text
        assert c.post("/api/actions/email-test").json()["ok"] is True
        assert _Recorder.logins == [("smtp.gmail.com", 587, "sentinel@example.org", "abcdefghijklmnop")]


def test_an_unreadable_time_in_the_file_is_one_row_each_and_a_patch_is_still_refused(
    data_dir: Path, config_path: Path
) -> None:
    """notify-validation#1: a file the old code accepted ('11pm-7am', '8 am') starts, each value
    left out is one config row naming its key and its value, and the API still refuses them."""
    from dspace_sentinel.config import load_config

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.safe_dump(
            {
                "setup": {"completed": True},
                "notifiers": {
                    "telegram": {"quiet_hours": "11pm-7am", "daily_digest": "07:30"},
                    "email": {"quiet_hours": "23:00-23:00", "daily_digest": "8 am"},
                },
            }
        )
    )
    before = config_path.read_text()
    rows_wanted = [
        "The quiet hours '11pm-7am' in notifiers.telegram.quiet_hours were ignored: "
        "quiet hours must look like 23:00-07:00",
        "The quiet hours '23:00-23:00' in notifiers.email.quiet_hours were ignored: "
        "quiet hours must start and end at different times",
        "The daily digest time '8 am' in notifiers.email.daily_digest was ignored: "
        "the daily digest time must look like 08:00",
    ]
    with make_client(load_config(config_path), data_dir, config_path) as c:
        rows = c.get("/api/events", params={"kind": "config"}).json()
        assert sorted(r["message"] for r in rows if "ignored" in r["message"]) == sorted(rows_wanted)
        shown = c.get("/api/config").json()["notifiers"]
        assert (shown["telegram"]["quiet_hours"], shown["telegram"]["daily_digest"]) == ("", "07:30")
        assert (shown["email"]["quiet_hours"], shown["email"]["daily_digest"]) == ("", "")
        assert config_path.read_text() == before  # starting does not rewrite the file
        for channel, key, value, message in (
            ("telegram", "quiet_hours", "11pm-7am", "quiet hours must look like 23:00-07:00"),
            ("email", "quiet_hours", "23:00 to 07:00", "quiet hours must look like 23:00-07:00"),
            ("email", "daily_digest", "8 am", "the daily digest time must look like 08:00"),
            ("telegram", "daily_digest", "08.00", "the daily digest time must look like 08:00"),
        ):
            r = c.patch("/api/config", json={"notifiers": {channel: {key: value}}})
            assert r.status_code == 400, r.text
            assert r.json() == {"error": f"invalid configuration: notifiers.{channel}.{key}: {message}"}
        assert config_path.read_text() == before
        # several requests later there is still one row for each
        rows = c.get("/api/events", params={"kind": "config"}).json()
        assert sorted(r["message"] for r in rows if "ignored" in r["message"]) == sorted(rows_wanted)


def test_telegram_test_answers_per_bot(
    data_dir: Path, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    from dspace_sentinel.alerts.notifiers import telegram

    def bot_api(request: httpx.Request) -> httpx.Response:
        token, method = request.url.path.split("/bot", 1)[1].split("/", 1)
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"username": f"bot{token[:3]}"}})
        if token == BOT_B:
            return httpx.Response(
                400, json={"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
            )
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    monkeypatch.setattr(
        telegram, "make_client", lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(bot_api))
    )
    bots = [
        {"label": "Danindu", "bot_token": BOT_A, "chat_id": "111111"},
        {"label": "Team", "bot_token": BOT_B, "chat_id": "-100222", "min_severity": "critical"},
    ]
    cfg = merge_patch(Config(), {"notifiers": {"telegram": {"chats": bots}}})
    with make_client(cfg, data_dir, config_path) as c:
        r = c.post("/api/actions/telegram-test").json()
        assert r["ok"] is False and r["bot_name"] == "@bot123"
        assert [(x["label"], x["bot_name"], x["ok"], x["error"]) for x in r["results"]] == [
            ("Danindu", "@bot123", True, None),
            ("Team", "@bot987", False, "chat not found — add the bot to the group first"),
        ]
        assert BOT_A not in str(r) and BOT_B not in str(r)
        first = r["results"][0]["id"]
        one = c.post("/api/actions/telegram-test", json={"id": first}).json()
        assert one["ok"] is True and [x["id"] for x in one["results"]] == [first]
        assert set(one["results"][0]) == {"id", "label", "chat_id", "bot_name", "ok", "error"}


def test_clear_history(client: TestClient) -> None:
    assert client.get("/api/history", params={"metric": "cpu"}).json()["points"]
    out = client.post("/api/actions/clear-history").json()
    assert out["ok"] is True
    assert out["counts"]["samples"] == 0 and out["counts"]["alerts"] == 0
    assert out["counts"]["incidents"] == 0 and out["counts"]["component_history"] == 0
    assert client.get("/api/alerts", params={"status": "all"}).json() == []
    assert client.get("/api/incidents").json() == []
    assert len(client.get("/api/history", params={"metric": "cpu"}).json()["points"]) <= 1
    status = client.get("/api/status").json()
    assert status["mood"] in {"ok", "warn", "crit", "unknown"}
    # the config survives
    assert client.get("/api/meta").json()["hostname"] == "demo-dspace"
    # and the next check fills the state again
    status = client.post("/api/actions/check-now").json()
    assert status["server"]["cpu_pct"] >= 0 and status["components"]


def test_export(client: TestClient) -> None:
    data = client.get("/api/export").json()
    assert data["meta"]["hostname"] == "demo-dspace" and data["meta"]["range"] == "30d"
    assert data["range"]["to"] - data["range"]["from"] == pytest.approx(30 * 86400, abs=5)
    expected = {"samples", "checks", "component_history", "alerts", "incidents", "repository", "events"}
    assert expected <= set(data)
    assert len(data["samples"]) > 1000 and data["samples"][0]["extra"]["cores"] == 4
    assert len(data["alerts"]) == 6 and len(data["incidents"]) == 1
    assert data["repository"] and data["events"]
    day = client.get("/api/export", params={"range": "1h"}).json()
    assert len(day["samples"]) < len(data["samples"])


# ------------------------------------------------------------------------------ auth


def test_auth_off_by_default(client: TestClient) -> None:
    assert client.get("/api/meta").status_code == 200
    assert "www-authenticate" not in client.get("/api/meta").headers


def test_auth_on(data_dir: Path, config_path: Path) -> None:
    cfg = apply_demo_overrides(Config())
    cfg.server.auth_token = "open-sesame"
    with make_client(cfg, data_dir, config_path) as c:
        assert c.get("/healthz").status_code == 200  # never protected
        denied = c.get("/api/meta")
        assert denied.status_code == 401 and denied.json() == {"error": "authentication required"}
        assert denied.headers["www-authenticate"].startswith("Basic realm=")
        assert c.get("/").status_code == 401
        assert c.get("/api/meta", auth=("anyone", "wrong")).status_code == 401
        assert c.get("/api/meta", auth=("bob", "open-sesame")).status_code == 200
        assert c.get("/api/status", auth=("", "open-sesame")).status_code == 200
        assert c.get("/api/meta", headers={"Authorization": "Bearer open-sesame"}).status_code == 200
        assert c.get("/api/meta", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert c.get("/api/meta", headers={"Authorization": "Basic not-base64!"}).status_code == 401
        masked_cfg = c.get("/api/config", auth=("x", "open-sesame")).json()
        assert masked_cfg["server"]["auth_token"] == MASK
        # the token is set in the YAML (or SENTINEL_AUTH_TOKEN), never over the API
        for token in ("new-token", ""):
            r = c.patch("/api/config", json={"server": {"auth_token": token}}, auth=("x", "open-sesame"))
            assert r.status_code == 400 and "server settings" in r.json()["error"]
        assert c.get("/api/meta", auth=("x", "open-sesame")).status_code == 200
        assert c.get("/api/meta", auth=("x", "new-token")).status_code == 401
        assert c.get("/api/meta").status_code == 401


def test_auth_token_from_env(monkeypatch: pytest.MonkeyPatch, data_dir: Path, config_path: Path) -> None:
    from dspace_sentinel.config import load_config

    monkeypatch.setenv("SENTINEL_AUTH_TOKEN", "env-token")
    cfg = load_config(config_path, demo=True)
    with make_client(cfg, data_dir, config_path) as c:
        assert c.get("/api/meta").status_code == 401
        assert c.get("/api/meta", auth=("u", "env-token")).status_code == 200


# ------------------------------------------------------------------------------ SSE


def test_format_sse() -> None:
    frame = format_sse("status", {"a": 1}, "7")
    assert frame == b'id: 7\nevent: status\ndata: {"a":1}\n\n'
    assert format_sse("heartbeat", {"ts": 1}).startswith(b"event: heartbeat\n")


async def test_stream_first_event_is_status(data_dir: Path, config_path: Path) -> None:
    """The generator behind ``/api/stream`` sends a ``status`` frame immediately on connect."""
    cfg = apply_demo_overrides(Config())
    app = create_app(
        cfg, config_path=config_path, data_dir=data_dir, demo_backfill_days=0, start_scheduler=False
    )
    st = app.state
    ctx = SimpleNamespace(
        cfg=st.cfg,
        state=st.state,
        store=st.store,
        bus=st.bus,
        scheduler=st.scheduler,
        alert_engine=st.alert_engine,
    )
    try:
        await st.scheduler.run_all_now()
        gen = event_stream(ctx)  # type: ignore[arg-type]
        first = await gen.__anext__()
        assert first == b": connected\n\n"
        frame = (await gen.__anext__()).decode()
        lines = frame.strip().split("\n")
        assert lines[0] == "id: 1" and lines[1] == "event: status"
        payload = json.loads(lines[2][len("data: ") :])
        assert payload["hostname"] == "demo-dspace" and payload["mood"] in {"ok", "warn", "crit", "unknown"}
        assert {"headline", "server", "components", "alerts_open", "next_check_in_s"} <= set(payload)
        assert len(payload["components"]) == 11
        # a bus event that is not throttled is forwarded as its own frame
        st.bus.publish("alert", {"type": "opened", "alert": {"id": 1}})
        forwarded = (await gen.__anext__()).decode()
        assert "event: alert" in forwarded and '"type":"opened"' in forwarded
        await gen.aclose()
    finally:
        await st.scheduler.stop()
        st.store.close()


def test_stream_route_is_mounted(client: TestClient) -> None:
    paths = set(client.get("/api/openapi.json").json()["paths"])
    assert "/api/stream" in paths
    assert {"/api/meta", "/api/status", "/api/history", "/api/availability", "/api/config"} <= paths
    actions = {"/api/actions/check-now", "/api/actions/setup", "/api/actions/clear-history", "/api/export"}
    assert actions <= paths


# ------------------------------------------------------------------------------ errors / static


def test_error_envelope_and_static_cache(client: TestClient) -> None:
    missing = client.get("/api/does-not-exist")
    assert missing.status_code == 404 and missing.json() == {"error": "Not Found"}
    bad = client.get("/api/alerts", params={"limit": "many"})
    assert bad.status_code == 400 and bad.json()["error"].startswith("invalid request")
    html = client.get("/static/index.html")
    assert html.status_code == 200 and html.headers["cache-control"] == "no-cache"
    api = client.get("/api/meta")
    assert "cache-control" not in api.headers


def test_status_mood_follows_open_alerts(client: TestClient) -> None:
    """An open critical alert keeps the mood crit even while every component reads ok again."""
    from dspace_sentinel.models import Alert

    before = client.get("/api/status").json()
    assert all(c["status"] in {"ok", "warn", "crit", "unknown"} for c in before["components"])
    store = client.app.state.store
    alert_id = store.insert_alert(
        Alert(
            None,
            "log_errors:dspace",
            "logs",
            "critical",
            "open",
            "120 errors in DSpace log (5 min)",
            "Solr search core is unavailable",
            120.0,
            100.0,
            time.time() - 60,
        )
    )
    status = client.get("/api/status").json()
    assert status["mood"] == "crit"
    assert [a["id"] for a in status["alerts_open"]] == [alert_id]
    if all(c["status"] != "crit" for c in status["components"]) and status["server"]["status"] != "crit":
        assert status["headline"] == "Critical: 120 errors in DSpace log (5 min)"
    store.resolve_alert(alert_id)
    after = client.get("/api/status").json()
    assert after["alerts_open"] == [] and after["mood"] == before["mood"]


def test_next_check_in_is_a_real_countdown(client: TestClient) -> None:
    status = client.get("/api/status").json()
    meta = client.get("/api/meta").json()
    assert 0 < status["next_check_in_s"] <= meta["system_interval"]
