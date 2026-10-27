"""Exact Telegram strings (SPEC section 7) and the helpers around them."""

from __future__ import annotations

import datetime as _dt
from zoneinfo import ZoneInfo

from dspace_sentinel.alerts import email_messages, messages
from dspace_sentinel.models import Alert, CheckResult, Incident
from dspace_sentinel.util.fmt import clock

HOST = "server-1"
UTC = _dt.timezone.utc
COLOMBO = ZoneInfo("Asia/Colombo")


def _local(hour: int, minute: int, second: int = 0) -> float:
    """Epoch seconds of a fixed UTC wall-clock time (the messages below are built with ``tz=UTC``)."""
    return _dt.datetime(2024, 1, 1, hour, minute, second, tzinfo=UTC).timestamp()


def _alert(**overrides: object) -> Alert:
    data: dict = {
        "id": 7,
        "rule": "component_down:postgres",
        "component": "postgres",
        "severity": "critical",
        "status": "open",
        "title": "PostgreSQL unreachable",
        "message": "service ✅ → TCP ✅ → connect ❌\nconnection refused (localhost:5432)",
        "opened_ts": _local(10, 43),
    }
    data.update(overrides)
    return Alert(**data)


# --------------------------------------------------------------------- SPEC formats


def test_critical_alert_matches_spec_byte_for_byte() -> None:
    expected = (
        "🔴 <b>CRITICAL</b> · server-1\n"
        "<b>PostgreSQL unreachable</b>\n"
        "service ✅ → TCP ✅ → connect ❌\n"
        "connection refused (localhost:5432)\n"
        "\n"
        "Started 10:43 UTC · DSpace Sentinel"
    )
    assert messages.alert_html(_alert(), HOST, tz=UTC) == expected


def test_warning_alert_matches_spec_byte_for_byte() -> None:
    alert = _alert(
        rule="disk:/dspace",
        component="server",
        severity="warning",
        title="Disk 87 % on /dspace",
        message="free 53.7 GB · growing 2.1 GB/day",
        opened_ts=_local(10, 41),
    )
    expected = (
        "🟡 <b>WARNING</b> · server-1\n"
        "<b>Disk 87 % on /dspace</b>\n"
        "free 53.7 GB · growing 2.1 GB/day\n"
        "\n"
        "Started 10:41 UTC · DSpace Sentinel"
    )
    assert messages.alert_html(alert, HOST, tz=UTC) == expected


def test_resolved_matches_spec_byte_for_byte() -> None:
    opened = _local(10, 43)
    alert = _alert(status="resolved", opened_ts=opened, resolved_ts=opened + 6 * 60 + 12)
    assert messages.resolved_html(alert, HOST) == (
        "✅ <b>Resolved</b> · server-1\nPostgreSQL unreachable · lasted 6 m 12 s"
    )


def test_incident_matches_spec_byte_for_byte() -> None:
    incident = Incident(
        id=24,
        title="Possible Solr resource exhaustion",
        hypothesis="Possible Solr resource exhaustion",
        status="open",
        started_ts=_local(10, 41),
        affected=["DSpace Search", "DSpace Backend"],
        sequence=[
            {"ts": _local(10, 41), "text": "Memory 94 %", "severity": "critical"},
            {"ts": _local(10, 42), "text": "Solr response 4.8 s", "severity": "critical"},
            {"ts": _local(10, 43, 5), "text": "Solr DOWN", "severity": "critical"},
            {"ts": _local(10, 43, 20), "text": "DSpace search unavailable", "severity": "critical"},
        ],
    )
    expected = (
        "🧭 <b>INCIDENT #24</b> · server-1\n"
        "<b>Possible Solr resource exhaustion</b>\n"
        "\n"
        "10:41 UTC  Memory 94 %\n"
        "10:42 UTC  Solr response 4.8 s\n"
        "10:43 UTC  Solr DOWN\n"
        "10:43 UTC  DSpace search unavailable\n"
        "\n"
        "Affected: DSpace Search, DSpace Backend"
    )
    assert messages.incident_html(incident, HOST, tz=UTC) == expected


def test_incident_without_affected_omits_the_line_and_sorts_sequence() -> None:
    incident = Incident(
        id=3,
        title="Multiple alerts within 5 minutes",
        hypothesis="Multiple alerts within 5 minutes",
        status="open",
        started_ts=_local(9, 0),
        sequence=[
            {"ts": _local(9, 2), "text": "CPU 97 %", "severity": "critical"},
            {"ts": _local(9, 0), "text": "Load 3.2 per core", "severity": "critical"},
        ],
    )
    html = messages.incident_html(incident, HOST, tz=UTC)
    assert html.endswith("09:00 UTC  Load 3.2 per core\n09:02 UTC  CPU 97 %")
    assert "Affected" not in html


def test_incident_resolved_and_reminder_and_test_message() -> None:
    incident = Incident(
        id=24,
        title="Possible Solr resource exhaustion",
        hypothesis="Possible Solr resource exhaustion",
        status="resolved",
        started_ts=_local(10, 41),
        ended_ts=_local(10, 48, 30),
    )
    assert messages.incident_resolved_html(incident, HOST) == (
        "✅ <b>Incident #24 resolved</b> · server-1\nPossible Solr resource exhaustion · lasted 7 m 30 s"
    )
    alert = _alert(opened_ts=_local(10, 0))
    assert messages.reminder_html(alert, HOST, _local(10, 32, 10)) == (
        "🔔 <b>Still open</b> · server-1\n🔴 PostgreSQL unreachable · open for 32 m 10 s"
    )
    assert messages.test_html(HOST) == "👋 DSpace Sentinel is connected · server-1"


def test_escalated_adds_a_line_and_keeps_started_time() -> None:
    alert = _alert(
        rule="memory", component="server", title="Memory 93 %", message="14.9 of 16 GB used, swap 40 %"
    )
    html = messages.alert_html(alert, HOST, escalated=True)
    assert html.splitlines()[3] == "Escalated from warning"
    # without a zone: the server's own, named the same way
    assert html.endswith(f"Started {clock(alert.opened_ts, zone=True)} · DSpace Sentinel")


def test_times_name_their_zone_in_utc_and_colombo() -> None:
    """T18: the owner's server runs on UTC, the owner lives in Asia/Colombo (+05:30)."""
    alert = _alert(opened_ts=_dt.datetime(2026, 10, 7, 15, 59, tzinfo=UTC).timestamp())
    assert messages.alert_html(alert, HOST, tz=UTC).endswith("Started 15:59 UTC · DSpace Sentinel")
    assert messages.alert_html(alert, HOST, tz=COLOMBO).endswith("Started 21:29 +0530 · DSpace Sentinel")
    assert clock(alert.opened_ts, tz=ZoneInfo("Europe/Berlin"), zone=True) == "17:59 CEST"
    assert clock(alert.opened_ts, tz=ZoneInfo("Asia/Dubai"), zone=True) == "19:59 +0400"
    incident = Incident(
        id=3,
        title="Multiple alerts within 5 minutes",
        hypothesis="Multiple alerts within 5 minutes",
        status="open",
        started_ts=alert.opened_ts,
        sequence=[{"ts": alert.opened_ts, "text": "Solr DOWN", "severity": "critical"}],
    )
    html = messages.incident_html(incident, HOST, tz=COLOMBO)
    assert html.endswith("21:29 +0530  Solr DOWN")
    # the email keeps the timeline a table, the zone in the time cell
    event = messages.notification_event(
        "incident", "opened", hostname=HOST, severity="critical", html=html, summary="s", incident=incident
    )
    body = email_messages.render_event(event).html
    assert "<table" in body and ">21:29 +0530</td>" in body and ">Solr DOWN</td>" in body
    started = messages.alert_html(alert, HOST, tz=UTC)
    page = email_messages.render_event(
        messages.notification_event(
            "alert", "opened", hostname=HOST, severity="critical", html=started, summary="s", alert=alert
        )
    ).html
    assert "Started 15:59 UTC</p>" in page


def test_digest_lists_counts_and_open_alerts() -> None:
    now = _local(8, 0)
    opened = [
        _alert(id=1, opened_ts=now - 3600),
        _alert(id=2, severity="warning", title="Disk 81 % on /", opened_ts=now - 7200),
    ]
    html = messages.digest_html(HOST, opened, [], [opened[0]], now)
    assert html.splitlines()[:3] == [
        "📋 <b>Daily digest</b> · server-1",
        "Last 24 h: 2 alerts (1 critical) · 0 incidents",
        "Open now: 1 alert",
    ]
    assert "🔴 PostgreSQL unreachable · 1 h 0 m" in html
    assert html.endswith("\n\nDSpace Sentinel")
    quiet = messages.digest_html(HOST, [], [], [], now)
    assert "Open now: nothing — all clear" in quiet


def test_digest_lists_the_oldest_open_alerts_first() -> None:
    """``store.open_alerts()`` is newest first; the digest shows the five longest-running ones."""
    now = _local(8, 0)
    # "Alert n" opened n × 10 minutes ago; listed newest first like the store returns them
    newest_first = [_alert(id=20 - n, title=f"Alert {n}", opened_ts=now - n * 600) for n in range(1, 8)]
    html = messages.digest_html(HOST, [], [], newest_first, now)
    listed = [line.split(" · ")[0] for line in html.splitlines() if line.startswith("🔴 ")]
    assert listed == ["🔴 Alert 7", "🔴 Alert 6", "🔴 Alert 5", "🔴 Alert 4", "🔴 Alert 3"]
    assert "Open now: 7 alerts" in html and "… and 2 more" in html


# -------------------------------------------------------------------------- helpers


def test_html_is_escaped_everywhere() -> None:
    alert = _alert(title="Log <error> & stuff", message="line with <tag>")
    html = messages.alert_html(alert, "host<1>")
    assert "<b>Log &lt;error&gt; &amp; stuff</b>" in html
    assert "line with &lt;tag&gt;" in html
    assert "host&lt;1&gt;" in html
    assert messages.to_plain(html).splitlines()[1] == "Log <error> & stuff"


def test_chain_text_stops_at_failing_rung_and_spells_tcp() -> None:
    chain = [
        CheckResult("postgres.service", "systemd postgresql", "ok", 1.0),
        CheckResult("postgres.tcp", "TCP :5432", "ok", 1.0),
        CheckResult("postgres.connect", "Connect as dspace", "crit", 1.0, message="connection refused"),
        CheckResult.not_reached("postgres.query", "SELECT 1", 1.0),
    ]
    assert messages.chain_text(chain, stop_at="postgres.connect") == "service ✅ → TCP ✅ → connect ❌"
    assert messages.chain_text(chain) == "service ✅ → TCP ✅ → connect ❌ → query —"
    assert messages.rung_short("backend.dep.solr") == "dep.solr"
    assert messages.rung_short("ports.8983") == "8983"


def test_failing_rung_parses_the_chain_line() -> None:
    assert messages.failing_rung("service ✅ → TCP ✅ → connect ❌\nconnection refused") == "connect"
    assert messages.failing_rung("service ✅ → TCP ❌") == "tcp"
    assert messages.failing_rung("service ✅ → rest ✅ → actuator ⚠️ → search ❌\nHTTP 500") == "search"
    assert messages.failing_rung("cert ⚠️\nexpires in 12 days") == "cert"
    assert messages.failing_rung("free 53.7 GB") is None
    assert messages.failing_rung("") is None


def test_sequence_text_mapping() -> None:
    assert messages.sequence_text(_alert()) == "PostgreSQL DOWN"
    assert messages.sequence_text(
        _alert(severity="warning", component="solr", rule="component_down:solr")
    ) == ("Solr degraded")
    backend = _alert(
        rule="component_down:backend",
        component="backend",
        title="DSpace Backend failing",
        message="service ✅ → TCP ✅ → rest ✅ → search ❌\nHTTP 500",
    )
    assert messages.sequence_text(backend) == "DSpace search unavailable"
    solr = _alert(rule="solr_latency", component="solr", title="Solr search slow: 4.8 s", value=4800.0)
    assert messages.sequence_text(solr) == "Solr response 4.8 s"
    memory = _alert(rule="memory", component="server", title="Memory 94 %")
    assert messages.sequence_text(memory) == "Memory 94 %"


def test_notification_event_shape() -> None:
    alert = _alert()
    event = messages.notification_event(
        "alert",
        "opened",
        hostname=HOST,
        severity="critical",
        html="<b>x</b> &amp; y",
        summary="CRITICAL x",
        alert=alert,
        ts=123.0,
    )
    assert event["kind"] == "alert" and event["type"] == "opened" and event["severity"] == "critical"
    assert event["text"] == "x & y" and event["alert"]["id"] == 7 and event["incident"] is None
    assert event["hostname"] == HOST and event["ts"] == 123.0 and event["sentinel_version"]
