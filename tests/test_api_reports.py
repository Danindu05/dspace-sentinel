"""``/api/reports`` in demo mode: options, create (201) in every format, download headers, validation,
custom ranges and sections, soft delete + restore + purge, events, Telegram degradation."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dspace_sentinel.api.app import create_app
from dspace_sentinel.config import Config
from dspace_sentinel.reports import generator
from dspace_sentinel.reports.analytics import ALL_SECTIONS
from dspace_sentinel.store import REPORT_PURGE_AFTER_S

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


def _create(client: TestClient, **body: Any) -> dict[str, Any]:
    r = client.post("/api/reports", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_options(client: TestClient) -> None:
    opts = client.get("/api/reports/options").json()
    assert [s["id"] for s in opts["sections"]] == list(ALL_SECTIONS)
    assert all(s["label"] and s["emoji"] for s in opts["sections"])
    assert opts["default_sections"] == client.get("/api/config").json()["reports"]["default_sections"]
    assert opts["formats"] == ["html", "md", "json"]
    assert [r["id"] for r in opts["ranges"]] == ["24h", "7d", "30d"]
    assert opts["telegram_available"] is False and opts["email_available"] is False
    assert opts["weekly"]["enabled"] is False and opts["purge_after_s"] == REPORT_PURGE_AFTER_S
    assert opts["weekly"]["send_email"] is False


def test_create_each_format_and_download(client: TestClient) -> None:
    assert client.get("/api/reports").json() == []
    created = {fmt: _create(client, range="24h", fmt=fmt) for fmt in ("html", "md", "json")}
    for fmt, rep in created.items():
        assert rep["fmt"] == fmt and rep["size"] > 0 and rep["exists"] is True and rep["deleted_ts"] is None
        assert rep["filename"].startswith("sentinel-report-") and rep["filename"].endswith(f"-24h.{fmt}")
        assert rep["title"] == "DSpace Sentinel report · demo-dspace · Last 24 hours"
        assert rep["range_to"] - rep["range_from"] == pytest.approx(86400, abs=2)
        assert "telegram" not in rep
    listed = client.get("/api/reports").json()
    assert [r["fmt"] for r in listed] == ["json", "md", "html"]  # newest first
    reports_dir = Path(client.app.state.data_dir) / "reports"
    assert all(Path(r["path"]).parent == reports_dir for r in listed)  # this app's data dir, not a global one

    html = client.get(f"/api/reports/{created['html']['id']}")
    assert html.status_code == 200
    assert html.headers["content-type"].startswith("text/html")
    assert html.headers["content-disposition"] == f'inline; filename="{created["html"]["filename"]}"'
    assert html.headers["cache-control"] == "no-cache"
    assert "<svg" in html.text and "demo-dspace" in html.text and "Last 24 hours" in html.text
    assert 'id="services"' in html.text and "☀️ Solr" in html.text

    md = client.get(f"/api/reports/{created['md']['id']}")
    assert md.headers["content-type"].startswith("text/markdown")
    assert md.headers["content-disposition"].startswith("attachment;")
    assert md.text.startswith("# DSpace Sentinel report · demo-dspace")

    js = client.get(f"/api/reports/{created['json']['id']}")
    assert js.headers["content-type"].startswith("application/json")
    payload = js.json()
    assert payload["meta"]["hostname"] == "demo-dspace" and payload["meta"]["demo"] is True
    assert {s["id"] for s in payload["services"]} >= {"server", "postgres", "solr", "backend", "frontend"}
    assert payload["repository"]["now"]["items"] > 27_000
    assert payload["latency"]["rest"]["p95"] is not None

    meta = client.get(f"/api/reports/{created['html']['id']}?meta=1").json()
    assert meta["id"] == created["html"]["id"] and meta["fmt"] == "html"
    forced = client.get(f"/api/reports/{created['html']['id']}?download=1")
    assert forced.headers["content-disposition"].startswith("attachment;")


def test_create_validation(client: TestClient) -> None:
    def bad(body: Any, needle: str) -> None:
        r = client.post("/api/reports", json=body)
        assert r.status_code == 400, r.text
        assert needle in r.json()["error"]

    bad({"range": "24h", "fmt": "pdf"}, "fmt must be one of")
    bad({"range": {"from": 10, "to": 5}}, "after")
    bad({"range": "yesterday"}, "invalid range")
    bad({"range": 42}, "range must be")
    bad({"sections": "summary"}, "sections must be a list")
    bad({"sections": ["summary", "weather"]}, "unknown sections: weather")
    bad({"title": 7}, "title must be a string")
    assert client.post("/api/reports", json=[1, 2]).status_code == 400
    assert client.get("/api/reports").json() == []


def test_create_refused_when_the_data_disk_is_full(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a free-space floor no real disk can meet makes the generator refuse to write
    monkeypatch.setattr(generator, "MIN_FREE_BYTES", 10**30)
    r = client.post("/api/reports", json={"range": "24h", "fmt": "html"})
    assert r.status_code == 507, r.text
    assert "not enough free disk space" in r.json()["error"]
    assert client.get("/api/reports").json() == []


def test_custom_range_title_and_sections(client: TestClient) -> None:
    now = time.time()
    rep = _create(
        client,
        range={"from": now - 3 * 3600, "to": now},
        fmt="html",
        title="  Three hours  ",
        sections=["recommendations", "summary"],
    )
    assert rep["title"] == "Three hours" and rep["filename"].endswith("-3h.html")
    body = client.get(f"/api/reports/{rep['id']}").text
    assert "<title>Three hours</title>" in body
    assert 'id="summary"' in body and 'id="recommendations"' in body and 'id="server"' not in body
    default = _create(client, range="7d", fmt="json")
    data = client.get(f"/api/reports/{default['id']}").json()
    assert data["meta"]["sections"] == client.get("/api/config").json()["reports"]["default_sections"]
    assert data["meta"]["range"]["label"] == "7d"


def test_security_section_in_every_format(client: TestClient) -> None:
    """``sections: ["security"]`` renders the Security section, whether the module is on or not."""
    on = bool(client.get("/api/config").json()["modules"].get("security"))
    reps = {fmt: _create(client, fmt=fmt, sections=["security"]) for fmt in ("html", "md", "json")}
    html = client.get(f"/api/reports/{reps['html']['id']}").text
    assert 'id="security"' in html and 'id="summary"' not in html
    md = client.get(f"/api/reports/{reps['md']['id']}").text
    assert "## 🛡️ Security" in md and "## 📋 Summary" not in md
    payload = client.get(f"/api/reports/{reps['json']['id']}").json()
    assert payload["meta"]["sections"] == ["security"]
    security = payload["security"]
    assert security["enabled"] is on
    if not on:
        assert security["verdict"] == "off" and "The Security module is off." in html
    elif security["verdict"] != "no_data":
        assert security["rungs"] and security["verdict"] in ("findings", "clear", "unchecked")
        assert f"<td>{security['rungs'][0]['label']}</td>" in html


def test_soft_delete_restore_and_purge(client: TestClient) -> None:
    rep = _create(client, range="24h", fmt="md")
    rid = rep["id"]
    path = Path(rep["path"])
    assert path.is_file()
    deleted = client.delete(f"/api/reports/{rid}").json()
    assert deleted["deleted_ts"] is not None
    assert deleted["purge_after_ts"] == pytest.approx(deleted["deleted_ts"] + REPORT_PURGE_AFTER_S)
    assert client.get("/api/reports").json() == []
    assert [r["id"] for r in client.get("/api/reports?include_deleted=true").json()] == [rid]
    assert client.get(f"/api/reports/{rid}").status_code == 404
    assert client.get(f"/api/reports/{rid}?meta=1").json()["deleted_ts"] is not None
    assert path.is_file()  # the file survives the undo window
    restored = client.post(f"/api/reports/{rid}/restore").json()
    assert restored["deleted_ts"] is None and restored["id"] == rid
    assert [r["id"] for r in client.get("/api/reports").json()] == [rid]
    assert client.get(f"/api/reports/{rid}").status_code == 200
    # delete again, then let the store's maintenance purge run 25 h later
    client.delete(f"/api/reports/{rid}")
    store = client.app.state.store
    assert store.purge(30, now=time.time() + 3600)["reports"] == 0 and path.is_file()
    assert store.purge(30, now=time.time() + REPORT_PURGE_AFTER_S + 60)["reports"] == 1
    assert not path.exists()
    assert client.get(f"/api/reports/{rid}?meta=1").status_code == 404
    assert client.post(f"/api/reports/{rid}/restore").status_code == 404
    assert client.get("/api/reports?include_deleted=true").json() == []
    kinds = [e["message"] for e in client.get("/api/events?kind=report").json()]
    assert any(m.startswith("Report deleted") for m in kinds)
    assert any(m.startswith("Report restored") for m in kinds)
    assert any(m.startswith("Report generated") for m in kinds)


def test_missing_report_and_gone_file(client: TestClient) -> None:
    assert client.get("/api/reports/999").status_code == 404
    assert client.delete("/api/reports/999").status_code == 404
    assert client.post("/api/reports/999/restore").status_code == 404
    rep = _create(client, range="24h", fmt="json")
    Path(rep["path"]).unlink()
    assert client.get(f"/api/reports/{rep['id']}").status_code == 410
    client.delete(f"/api/reports/{rep['id']}")
    assert client.post(f"/api/reports/{rep['id']}/restore").status_code == 409


def test_report_event_on_bus(client: TestClient) -> None:
    bus = client.app.state.bus
    sub = bus.subscribe()
    try:
        rep = _create(client, range="24h", fmt="json")
        events = []
        while not sub.queue.empty():
            events.append(sub.queue.get_nowait())
        created = [e for e in events if e.event == "report" and e.data.get("type") == "created"]
        assert created and created[-1].data["report"]["id"] == rep["id"]
    finally:
        sub.close()


def test_send_telegram_degrades_when_off(client: TestClient) -> None:
    rep = _create(client, range="24h", fmt="md", send_telegram=True)
    assert rep["telegram"] == {"ok": False, "error": "Telegram is not enabled", "skipped": True}
    notes = client.get("/api/events?kind=notify.error").json()  # a failed delivery, like an alert's
    assert notes and notes[0]["message"].startswith("Report not sent to Telegram: Telegram is not enabled")
    assert client.get("/api/events?kind=notify").json() == []


# a made-up token, split in two so that secret scanners do not report it
BOT = "123456789:" + "AAH-api-reports-test-token-000000001"


def test_send_telegram_in_demo_is_logged_not_sent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Demo mode never calls sendDocument: the delivery becomes a ``notify.demo`` event instead."""
    calls: list[tuple[Any, ...]] = []

    async def fake_send_document(cfg: Any, path: str, caption: str = "", **kwargs: Any) -> dict[str, Any]:
        calls.append((cfg.notifiers.telegram.chats, path, caption))
        return {"ok": True, "message_id": 5, "error": None}

    monkeypatch.setattr(generator, "_resolve_send_document", lambda notifier=None: (fake_send_document, None))
    patched = client.patch(
        "/api/config",
        json={"notifiers": {"telegram": {"enabled": True, "chats": [{"bot_token": BOT, "chat_id": "99"}]}}},
    )
    assert patched.status_code == 200
    assert client.get("/api/reports/options").json()["telegram_available"] is True
    rep = _create(client, range="24h", fmt="html", send_telegram=True)
    assert rep["telegram"] == {"ok": True, "error": None, "skipped": "demo"}
    assert calls == []
    assert client.get("/api/events?kind=notify").json() == []
    demo_events = client.get("/api/events?kind=notify.demo").json()
    assert demo_events and demo_events[0]["message"] == f"Report (demo, not sent): {rep['title']}"
    data = demo_events[0]["data"]
    assert data["report_id"] == rep["id"] and data["channels"] == ["telegram"]


def test_send_email_degrades_when_off_and_is_logged_in_demo(client: TestClient) -> None:
    rep = _create(client, range="24h", fmt="md", send_email=True)
    assert rep["email"] == {"ok": False, "error": "email is not enabled", "skipped": True}
    assert "telegram" not in rep
    (note,) = client.get("/api/events?kind=notify.error").json()
    assert (
        note["message"] == "Report not sent by email: email is not enabled"
        and note["data"]["channel"] == "email"
    )
    email = {
        "enabled": True,
        "username": "sentinel@gmail.com",
        "password": "abcdefghijklmnop",
        "recipients": [{"address": "ops@example.org"}],
    }
    assert client.patch("/api/config", json={"notifiers": {"email": email}}).status_code == 200
    assert client.get("/api/reports/options").json()["email_available"] is True
    both = _create(client, range="24h", fmt="html", send_email=True, send_telegram=True)
    assert both["email"] == {"ok": True, "error": None, "skipped": "demo"}
    assert both["telegram"]["ok"] is False  # Telegram is off
    logged = [
        e
        for e in client.get("/api/events?kind=notify.demo").json()
        if e["data"].get("report_id") == both["id"]
    ]
    assert [e["message"] for e in logged] == [f"Report by email (demo, not sent): {both['title']}"]
    assert logged[0]["data"]["subject"] == f"[Sentinel] Report · demo-dspace · {both['title']}"
    # email_available needs a recipient and the login
    client.patch("/api/config", json={"notifiers": {"email": {"recipients": []}}})
    assert client.get("/api/reports/options").json()["email_available"] is False


def test_openapi_lists_report_routes(client: TestClient) -> None:
    paths = client.get("/api/openapi.json").json()["paths"]
    assert set(paths) >= {
        "/api/reports",
        "/api/reports/options",
        "/api/reports/{report_id}",
        "/api/reports/{report_id}/restore",
    }
    assert "post" in paths["/api/reports"] and "delete" in paths["/api/reports/{report_id}"]
    body = json.dumps(paths["/api/reports"]["post"])
    assert "201" in body
