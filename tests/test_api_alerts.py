"""``/api/alerts`` and ``/api/incidents`` fed by the live demo, plus the Telegram/webhook transports."""

from __future__ import annotations

import json
import time
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from dspace_sentinel.alerts.notifiers import telegram, webhook
from dspace_sentinel.api.app import create_app
from dspace_sentinel.collectors import demo as demo_mod
from dspace_sentinel.collectors.demo import INCIDENT_OFFSET_S
from dspace_sentinel.config import Config, merge_patch


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


@pytest.fixture
def incident_client(
    demo_cfg: Config, data_dir: Path, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """The demo app with its live timeline shifted so "now" is 330 s into the scripted incident.

    At that point memory is critical, Solr is down, the backend search API fails and the
    DSpace log spikes; the start-up cycle is the first breach, one *check now* confirms.
    """
    shift = INCIDENT_OFFSET_S + 330
    real_time = time.time
    monkeypatch.setattr(demo_mod, "time", types.SimpleNamespace(time=lambda: real_time() - shift))
    with make_client(demo_cfg, data_dir, config_path) as c:
        yield c


def test_live_demo_incident_flows_through_the_api(incident_client: TestClient) -> None:
    c = incident_client
    status = c.post("/api/actions/check-now").json()
    assert status["mood"] == "crit" and status["alerts_open"]

    open_alerts = c.get("/api/alerts").json()
    rules = {a["rule"] for a in open_alerts}
    assert {"memory", "component_down:solr", "component_down:backend", "log_errors:dspace"} <= rules
    assert all(a["status"] == "open" and a["opened_ts"] for a in open_alerts)
    solr = next(a for a in open_alerts if a["rule"] == "component_down:solr")
    assert solr["title"] == "Solr unreachable" and solr["severity"] == "critical"
    assert solr["message"].startswith("service ✅ → TCP ❌\n") and solr["notified_ts"]
    memory = next(a for a in open_alerts if a["rule"] == "memory")
    assert memory["severity"] == "critical" and memory["title"] == "Memory 94 %"
    assert c.get(f"/api/alerts/{solr['id']}").json()["id"] == solr["id"]

    incidents = c.get("/api/incidents", params={"status": "open"}).json()
    assert len(incidents) == 1
    incident = incidents[0]
    assert incident["hypothesis"] == "Possible Solr resource exhaustion"
    assert incident["title"] == incident["hypothesis"]
    assert incident["affected"] == ["DSpace Search", "DSpace Backend"]
    assert solr["id"] in incident["alert_ids"] and solr["incident_id"] == incident["id"]
    detail = c.get(f"/api/incidents/{incident['id']}").json()
    assert {a["id"] for a in detail["alerts"]} == set(incident["alert_ids"])
    texts = [e["text"] for e in detail["sequence"]]
    assert "Solr DOWN" in texts and "DSpace search unavailable" in texts and "Memory 94 %" in texts

    acked = c.post(f"/api/alerts/{solr['id']}/ack").json()
    assert acked["acked_ts"] and c.get(f"/api/alerts/{solr['id']}").json()["acked_ts"] == acked["acked_ts"]
    assert c.post(f"/api/alerts/{solr['id']}/unack").json()["acked_ts"] is None

    demo_events = c.get("/api/events", params={"kind": "notify.demo", "limit": 50}).json()
    assert demo_events and all(e["kind"] == "notify.demo" for e in demo_events)
    assert any("INCIDENT #" in e["message"] or "Incident" in e["message"] for e in demo_events) or any(
        e["message"].startswith("Notification (demo, not sent): Incident #") for e in demo_events
    )
    alert_events = c.get("/api/events", params={"kind": "alert", "limit": 50}).json()
    assert any(e["message"] == "CRITICAL Solr unreachable" for e in alert_events)
    incident_events = c.get("/api/events", params={"kind": "incident", "limit": 50}).json()
    assert any(e["message"].startswith(f"Incident #{incident['id']}:") for e in incident_events)
    assert c.get("/api/events", params={"kind": "notify"}).json() == []  # nothing really sent in demo

    # another cycle changes nothing: one alert per rule, still one incident
    c.post("/api/actions/check-now")
    assert len(c.get("/api/alerts").json()) == len(open_alerts)
    assert len(c.get("/api/incidents", params={"status": "open"}).json()) == 1

    # the incidents module toggle hot-reloads the engine
    assert c.app.state.incident_engine.enabled is True
    c.patch("/api/modules", json={"incidents": False})
    assert c.app.state.incident_engine.enabled is False
    c.patch("/api/modules", json={"incidents": True})

    # Telegram test in demo mode is refused before any request is built (nothing leaves the box)
    assert c.post("/api/actions/telegram-test").json() == {
        "ok": False,
        "bot_name": None,
        "error": "demo mode: nothing is sent to Telegram",
        "skipped": "demo",
        "results": [],
    }


def test_calm_demo_has_no_open_alerts(client: TestClient) -> None:
    assert client.get("/api/alerts").json() == []
    assert client.get("/api/incidents", params={"status": "open"}).json() == []
    engine = client.app.state.alert_engine
    assert engine.evaluations >= 1 and all(st.alert is None for st in engine.states().values())


# ----------------------------------------------------------- Telegram transport


# a made-up token, split in two so that secret scanners do not report it
TOKEN = "123456789:" + "AAH-api-alerts-test-token-000000001"


def tg_cfg(token: str = TOKEN, chat_id: str = "42") -> Config:
    return merge_patch(
        Config(),
        {
            "server": {"hostname": "server-1"},
            "notifiers": {"telegram": {"enabled": True, "chats": [{"bot_token": token, "chat_id": chat_id}]}},
        },
    )


def tg_client(calls: list[httpx.Request], handler: Any = None) -> httpx.AsyncClient:
    def default(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"username": "sentinel_bot"}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 5}})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler or default))


async def test_send_message_posts_html_to_the_bot_endpoint() -> None:
    calls: list[httpx.Request] = []
    cfg = tg_cfg()
    async with tg_client(calls) as client:
        result = await telegram.send_message(cfg, "<b>hi</b>", client=client)
    bot_id = cfg.notifiers.telegram.chats[0].id
    assert result == {
        "ok": True,
        "message_id": 5,
        "error": None,
        "results": [{"id": bot_id, "label": "", "chat_id": "42", "ok": True, "error": None, "message_id": 5}],
    }
    assert calls[0].url == httpx.URL(f"https://api.telegram.org/bot{TOKEN}/sendMessage")
    body = json.loads(calls[0].content)
    assert body == {
        "chat_id": "42",
        "text": "<b>hi</b>",
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }


async def test_send_test_calls_get_me_then_sends_the_connected_message() -> None:
    calls: list[httpx.Request] = []
    async with tg_client(calls) as client:
        result = await telegram.send_test(tg_cfg(), client=client)
    assert (result["ok"], result["bot_name"], result["error"]) == (True, "@sentinel_bot", None)
    assert [(r["bot_name"], r["ok"]) for r in result["results"]] == [("@sentinel_bot", True)]
    assert [c.url.path.rsplit("/", 1)[-1] for c in calls] == ["getMe", "sendMessage"]
    assert json.loads(calls[1].content)["text"] == "👋 DSpace Sentinel is connected · server-1"


async def test_telegram_errors_are_returned_and_redacted() -> None:
    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"ok": False, "error_code": 400, "description": f"Bad Request: odd reply for bot{TOKEN}"},
        )

    async with tg_client([], failing) as client:
        result = await telegram.send_message(tg_cfg(), "x", client=client)
    assert result["ok"] is False and result["error"] == "Bad Request: odd reply for bot••• (error 400)"

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    async with tg_client([], refused) as client:
        result = await telegram.send_test(tg_cfg(), client=client)
    assert (result["ok"], result["bot_name"]) == (False, None)
    assert result["error"].startswith("api.telegram.org refused the connection: ")  # and what to check

    def html_page(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>bad gateway</html>")

    async with tg_client([], html_page) as client:
        result = await telegram.get_me(tg_cfg(), client=client)
    assert result["error"] == "HTTP 502 from Telegram"

    calls: list[httpx.Request] = []
    async with tg_client(calls) as client:
        assert (await telegram.send_message(tg_cfg(token=""), "x", client=client))[
            "error"
        ] == telegram.NO_BOTS
        assert (await telegram.send_message(tg_cfg(chat_id=""), "x", client=client))[
            "error"
        ] == telegram.NO_BOTS
    assert calls == []


async def test_send_document_uses_multipart(tmp_path: Path) -> None:
    report = tmp_path / "sentinel-report.html"
    report.write_text("<html>report</html>", encoding="utf-8")
    calls: list[httpx.Request] = []
    async with tg_client(calls) as client:
        result = await telegram.send_document(tg_cfg(), report, "Weekly report", client=client)
        missing = await telegram.send_document(tg_cfg(), tmp_path / "nope.html", client=client)
    assert (result["ok"], result["message_id"], result["error"]) == (True, 5, None)
    assert calls[0].url.path.endswith("/sendDocument")
    assert calls[0].headers["content-type"].startswith("multipart/form-data")
    assert b'filename="sentinel-report.html"' in calls[0].content and b"Weekly report" in calls[0].content
    assert missing["ok"] is False and "file not found" in missing["error"]


# ------------------------------------------------------------ webhook transport


async def test_webhook_posts_the_event_as_json() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(204)

    cfg = merge_patch(
        Config(), {"notifiers": {"webhook": {"enabled": True, "url": "https://hooks.example/x"}}}
    )
    event = {"kind": "alert", "type": "opened", "severity": "critical", "text": "CRITICAL CPU 96 %"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await webhook.send(cfg, event, client=client)
    assert result == {"ok": True, "status_code": 204, "error": None}
    body = json.loads(calls[0].content)
    assert body["kind"] == "alert" and body["source"] == "dspace-sentinel" and body["version"]

    def server_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(server_error)) as client:
        assert (await webhook.send(cfg, event, client=client))["error"] == "HTTP 500 from webhook"
    assert (await webhook.send(Config(), event))["error"] == "webhook URL is not configured"
