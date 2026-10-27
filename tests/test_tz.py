"""server.timezone (F7 #5, T18): the zone of the times Sentinel shows people.

Messages and quiet hours in Asia/Colombo and in UTC, a window across midnight in the chosen zone,
the digest hour, the weekly report hour, report times and chart ticks, the API (PATCH and
``/api/meta``) and a bad name (refused by the API, ignored with a notice in the file).
"""

from __future__ import annotations

import datetime as _dt
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml
from fastapi.testclient import TestClient

from dspace_sentinel.alerts import notifiers
from dspace_sentinel.alerts.engine import AlertEngine
from dspace_sentinel.api.app import create_app
from dspace_sentinel.bus import Bus
from dspace_sentinel.config import Config, load_config, merge_patch, save_config, startup_notices
from dspace_sentinel.reports import generator, schedule, svg
from dspace_sentinel.store import Store
from dspace_sentinel.util import tz
from tests.test_engine import BOT, Recorder, feed
from tests.test_rules import make_sample

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

UTC = _dt.timezone.utc
COLOMBO = ZoneInfo("Asia/Colombo")
BAD_ZONE = (
    "unknown time zone 'Mars/Base': use an IANA name such as Asia/Colombo, or leave it empty for the "
    "server's zone"
)


def at(*parts: int) -> float:
    """Epoch seconds of a UTC wall-clock time."""
    return _dt.datetime(*parts, tzinfo=UTC).timestamp()


def colombo(**changes: Any) -> Config:
    return merge_patch(Config(), {"server": {"timezone": "Asia/Colombo"}, **changes})


# ------------------------------------------------------------------------------- util.tz


def test_names_labels_and_the_server_zone() -> None:
    assert tz.resolve("") is None and tz.resolve("  ") is None
    assert tz.resolve("UTC") is UTC and tz.resolve("etc/utc") is UTC
    assert tz.resolve(" Asia/Colombo ") == COLOMBO
    assert tz.zone_label(at(2026, 10, 7, 15, 59), COLOMBO) == "+0530"
    assert tz.zone_label(at(2026, 10, 7, 15, 59), UTC) == "UTC"
    assert tz.zone_label(at(2026, 1, 7, 15, 59), ZoneInfo("Europe/Berlin")) == "CET"
    assert tz.zone_name(COLOMBO) == "Asia/Colombo" and tz.zone_name(UTC) == "UTC"
    assert tz.zone_of(colombo()) == COLOMBO and tz.zone_of(Config()) is None
    assert tz.server_zone_name()  # some name, whatever this machine uses
    for bad in ("Mars/Base", "Asia", "../etc/passwd", "/etc/localtime", "x" * 65):
        with pytest.raises(ValueError, match="unknown time zone"):
            tz.resolve(bad)
    with pytest.raises(ValueError, match="must be text"):
        tz.check_zone(530)


def test_describe_for_api_meta(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tz, "server_zone_name", lambda: "Etc/UTC")
    monkeypatch.setattr(tz, "zone_label", lambda ts, zone: "+0530" if zone is COLOMBO else "UTC")
    now = at(2026, 10, 7, 12, 0)
    assert tz.describe(colombo(), now) == {
        "name": "Asia/Colombo",
        "abbreviation": "+0530",
        "server_name": "Etc/UTC",
        "server_abbreviation": "UTC",
    }
    assert tz.describe(Config(), now) == {
        "name": "Etc/UTC",
        "abbreviation": "UTC",
        "server_name": "Etc/UTC",
        "server_abbreviation": "UTC",
    }


# ----------------------------------------------------------------------------- the config


def test_server_timezone_is_checked_and_saved(tmp_path: Path) -> None:
    assert Config().server.timezone == ""
    assert colombo().server.timezone == "Asia/Colombo"
    assert merge_patch(Config(), {"server": {"timezone": "  UTC "}}).server.timezone == "UTC"
    with pytest.raises(ValueError) as refused:
        merge_patch(Config(), {"server": {"timezone": "Mars/Base"}})
    assert str(refused.value).startswith(f"invalid configuration: server.timezone: {BAD_ZONE}")
    path = tmp_path / "sentinel.yaml"
    path.write_text("server:\n  port: 9765\n")
    save_config(merge_patch(load_config(path), {"server": {"timezone": "Asia/Colombo"}}), path)
    assert yaml.safe_load(path.read_text())["server"]["timezone"] == "Asia/Colombo"
    assert load_config(path).server.timezone == "Asia/Colombo"


def test_a_bad_name_in_the_file_never_stops_the_start(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "sentinel.yaml"
    path.write_text(yaml.safe_dump({"server": {"timezone": "Mars/Base"}, "appearance": {"theme": "dark"}}))
    with caplog.at_level(logging.WARNING, logger="dspace_sentinel.config"):
        cfg = load_config(path)
    assert cfg.server.timezone == "" and cfg.appearance.theme == "dark"
    (notice,) = startup_notices(cfg)
    assert notice.startswith(
        f"The time zone in server.timezone was ignored, so times use the server's zone: {BAD_ZONE}"
    )
    assert any("server.timezone" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------- quiet hours and the digest


def test_quiet_hours_across_midnight_in_the_chosen_zone() -> None:
    cfg = colombo(notifiers={"telegram": {"quiet_hours": "23:00-07:00"}})
    warning = {"kind": "alert", "severity": "warning"}
    late = at(2024, 3, 5, 18, 0)  # 23:30 in Colombo
    after_midnight = at(2024, 3, 5, 19, 0)  # 00:30 in Colombo
    morning = at(2024, 3, 6, 2, 0)  # 07:30 in Colombo
    assert notifiers.suppressed_by_quiet_hours(cfg, warning, late, "telegram")
    assert notifiers.suppressed_by_quiet_hours(cfg, warning, after_midnight, "telegram")
    assert not notifiers.suppressed_by_quiet_hours(cfg, warning, morning, "telegram")
    # the same instants in UTC: 18:00 and 19:00 are outside the window, 02:00 inside
    in_utc = merge_patch(cfg, {"server": {"timezone": "UTC"}})
    assert not notifiers.suppressed_by_quiet_hours(in_utc, warning, late, "telegram")
    assert notifiers.suppressed_by_quiet_hours(in_utc, warning, morning, "telegram")
    assert notifiers.in_quiet_hours("23:00-07:00", late, COLOMBO)
    assert not notifiers.in_quiet_hours("23:00-07:00", late, UTC)


def test_the_digest_hour_is_read_in_the_chosen_zone() -> None:
    morning = at(2024, 3, 6, 2, 0)  # 07:30 in Colombo
    assert notifiers.digest_due_ts("08:00", morning, COLOMBO) == at(2024, 3, 6, 2, 30)
    assert notifiers.digest_due_ts("08:00", morning, UTC) == at(2024, 3, 6, 8, 0)


async def test_the_engine_sends_the_digest_at_the_zones_hour(mem_store: Store) -> None:
    recorder = Recorder()
    cfg = colombo(
        notifiers={
            "telegram": {
                "enabled": True,
                "chats": [{"bot_token": BOT, "chat_id": "9"}],
                "daily_digest": "08:00",
            }
        }
    )
    engine = AlertEngine(mem_store, cfg, Bus(), dispatcher=recorder)
    for now in (at(2024, 3, 5, 12, 0), at(2024, 3, 6, 2, 29)):  # armed at 17:30, then 07:59 in Colombo
        await engine.evaluate(make_sample(ts=now), {}, now)
        await engine.flush()
    assert [e for e in recorder.events if e["kind"] == "digest"] == []
    eight = at(2024, 3, 6, 2, 31)  # 08:01 in Colombo
    await engine.evaluate(make_sample(ts=eight), {}, eight)
    await engine.flush()
    assert len([e for e in recorder.events if e["kind"] == "digest"]) == 1


async def test_alert_messages_name_the_zone(mem_store: Store) -> None:
    recorder = Recorder()
    engine = AlertEngine(mem_store, colombo(), Bus(), dispatcher=recorder)
    await feed(engine, [85.0, 85.0], start=at(2026, 10, 7, 15, 58, 45))  # opens at 15:59:00 UTC
    assert recorder.events[0]["html"].endswith("Started 21:29 +0530 · DSpace Sentinel")
    engine.reload(merge_patch(Config(), {"server": {"timezone": "UTC"}}))
    await feed(engine, [96.0], start=at(2026, 10, 7, 16, 0))
    assert recorder.types() == ["opened", "escalated"]
    assert recorder.events[1]["html"].endswith("Started 15:59 UTC · DSpace Sentinel")


# ------------------------------------------------------------------------------- reports


def test_weekly_report_hour_and_title_in_the_chosen_zone() -> None:
    cfg = colombo(reports={"weekly": {"enabled": True, "weekday": 1, "hour": 8}})
    # Monday 2026-09-21 08:00 in Colombo is 02:30 UTC
    assert schedule.weekly_due(cfg, at(2026, 9, 21, 2, 29), None) is None
    assert schedule.weekly_due(cfg, at(2026, 9, 21, 2, 31), None) == "2026-09-21"
    # Sunday 23:00 UTC is already Monday 04:30 in Colombo: the right day, before the hour
    assert schedule.weekly_due(cfg, at(2026, 9, 20, 23, 0), None) is None
    # Monday 20:00 UTC is Tuesday 01:30 in Colombo: the day is over there
    assert schedule.weekly_due(cfg, at(2026, 9, 21, 20, 0), None) is None


def test_report_times_and_chart_ticks_in_the_chosen_zone() -> None:
    env = generator.jinja_env()
    stamp = env.from_string("{{ ts|datetime }} · {{ ts|date }} · {{ ts|clock }}")
    ts = at(2026, 10, 7, 15, 59)
    assert stamp.render(ts=ts, tz=COLOMBO) == "07 Oct 2026 21:29 +0530 · 07 Oct 2026 · 21:29 +0530"
    assert stamp.render(ts=ts, tz=UTC) == "07 Oct 2026 15:59 UTC · 07 Oct 2026 · 15:59 UTC"
    assert generator.report_filename(ts, "24h", "html", COLOMBO) == "sentinel-report-20261007-2129-24h.html"
    assert (
        generator.telegram_caption(
            generator.Report(
                id=1,
                created_ts=ts,
                title="T",
                range_from=ts - 86400,
                range_to=ts,
                fmt="html",
                path="/x",
                size=1,
            ),
            UTC,
        )
        == "📄 T\nLast 24 hours · 06 Oct 2026 15:59 UTC → 07 Oct 2026 15:59 UTC"
    )
    hours = svg.time_ticks(at(2026, 10, 7, 0, 0), at(2026, 10, 7, 6, 0), tz=COLOMBO)
    assert hours[0][1] == "05:30"
    days = svg.time_ticks(at(2026, 9, 30, 12, 0), at(2026, 10, 7, 12, 0), tz=COLOMBO)
    assert days and all(_dt.datetime.fromtimestamp(t, COLOMBO).strftime("%H:%M") == "00:00" for t, _ in days)
    chart = svg.line_chart(
        [svg.Series("CPU %", [[at(2026, 10, 7, 0, 0), 10], [at(2026, 10, 7, 6, 0), 20]])],
        t_from=at(2026, 10, 7, 0, 0),
        t_to=at(2026, 10, 7, 6, 0),
        tz=COLOMBO,
    )
    assert ">05:30</text>" in chart


# ----------------------------------------------------------------------------------- API


@pytest.fixture
def api(demo_cfg: Config, data_dir: Path, config_path: Path) -> Iterator[TestClient]:
    app = create_app(
        demo_cfg, config_path=config_path, data_dir=data_dir, start_scheduler=False, demo_backfill_days=0
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client


def test_the_zone_is_chosen_over_the_api(api: TestClient) -> None:
    meta = api.get("/api/meta").json()["timezone"]
    assert set(meta) == {"name", "abbreviation", "server_name", "server_abbreviation"}
    assert meta["name"] == meta["server_name"]
    r = api.patch("/api/config", json={"server": {"timezone": "Asia/Colombo"}})
    assert r.status_code == 200 and r.json()["server"]["timezone"] == "Asia/Colombo"
    meta = api.get("/api/meta").json()["timezone"]
    assert (meta["name"], meta["abbreviation"]) == ("Asia/Colombo", "+0530")
    bad = api.patch("/api/config", json={"server": {"timezone": "Mars/Base"}})
    assert bad.status_code == 400
    assert bad.json()["error"].startswith(f"invalid configuration: server.timezone: {BAD_ZONE}")
    # the zone is the only server setting the API may change
    port = api.patch("/api/config", json={"server": {"timezone": "UTC", "port": 9999}})
    assert port.status_code == 400 and "server settings" in port.json()["error"]
    assert api.get("/api/config").json()["server"]["timezone"] == "Asia/Colombo"
    cleared = api.patch("/api/config", json={"server": {"timezone": ""}})
    assert cleared.status_code == 200
    assert api.get("/api/meta").json()["timezone"]["name"] == meta["server_name"]
