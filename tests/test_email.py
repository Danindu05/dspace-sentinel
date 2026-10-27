"""Email notifications: subjects and bodies, the message headers, one SMTP session per event
(a real ``smtplib`` session against a tiny local SMTP server, and fakes for the TLS modes),
error sentences without the password, demo mode, and email in dispatch and the daily digest.

Nothing here reaches a real mail server.
"""

from __future__ import annotations

import base64
import datetime as _dt
import email
import email.policy
import re
import smtplib
import socket
import socketserver
import ssl
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from dspace_sentinel.alerts import email_messages, messages, notifiers
from dspace_sentinel.alerts.engine import DIGEST_KV_KEY, EMAIL_DIGEST_KV_KEY, AlertEngine
from dspace_sentinel.alerts.notifiers import email as mail
from dspace_sentinel.bus import Bus
from dspace_sentinel.config import Config, merge_patch
from dspace_sentinel.models import Alert, Incident
from dspace_sentinel.store import Store
from dspace_sentinel.util.fmt import bytes_, clock
from tests.test_rules import make_sample

HOST = "server-1"
PASSWORD = "hunter2hunter2xyz"


def _local(hour: int, minute: int, second: int = 0) -> float:
    return _dt.datetime(2024, 1, 1, hour, minute, second).timestamp()


def _alert(**overrides: Any) -> Alert:
    data: dict[str, Any] = {
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


def _event(kind: str, event_type: str, html: str, severity: str = "critical", **kw: Any) -> dict[str, Any]:
    return messages.notification_event(
        kind, event_type, hostname=HOST, severity=severity, html=html, summary="s", ts=_local(10, 50), **kw
    )


def email_cfg(port: int = 587, **email_section: Any) -> Config:
    section = {
        "enabled": True,
        "smtp_host": "smtp.example.org",
        "smtp_port": port,
        "username": "sentinel@example.org",
        "password": PASSWORD,
        "recipients": [
            {"address": "ops@example.org", "label": "On call"},
            {"address": "boss@example.org", "label": "Boss", "min_severity": "critical"},
        ],
        **email_section,
    }
    return merge_patch(Config(), {"server": {"hostname": HOST}, "notifiers": {"email": section}})


# ------------------------------------------------------------------------------ subjects


def test_subjects_follow_the_contract() -> None:
    alert = _alert()
    opened = _event("alert", "opened", messages.alert_html(alert, HOST), alert=alert)
    assert email_messages.subject_for(opened) == "[Sentinel] CRITICAL · server-1 · PostgreSQL unreachable"
    warning = _alert(severity="warning", title="Disk 87 % on /dspace")
    assert (
        email_messages.subject_for(_event("alert", "opened", "x", "warning", alert=warning))
        == "[Sentinel] Warning · server-1 · Disk 87 % on /dspace"
    )
    assert (
        email_messages.subject_for(_event("alert", "escalated", "x", alert=alert))
        == "[Sentinel] CRITICAL (escalated) · server-1 · PostgreSQL unreachable"
    )
    assert (
        email_messages.subject_for(_event("alert", "reminder", "x", alert=alert))
        == "[Sentinel] Still open · server-1 · PostgreSQL unreachable"
    )
    resolved = _alert(status="resolved", resolved_ts=_local(10, 43) + 6 * 60 + 12)
    assert (
        email_messages.subject_for(_event("alert", "resolved", "x", alert=resolved))
        == "[Sentinel] Resolved · server-1 · PostgreSQL unreachable (lasted 6 m 12 s)"
    )
    incident = Incident(
        id=24,
        title="Possible Solr resource exhaustion",
        hypothesis="Possible Solr resource exhaustion",
        status="open",
        started_ts=_local(10, 41),
    )
    assert (
        email_messages.subject_for(_event("incident", "opened", "x", incident=incident))
        == "[Sentinel] Incident #24 · server-1 · Possible Solr resource exhaustion"
    )
    incident.status, incident.ended_ts = "resolved", _local(10, 48, 30)
    assert (
        email_messages.subject_for(_event("incident", "resolved", "x", incident=incident))
        == "[Sentinel] Incident #24 resolved · server-1 · Possible Solr resource exhaustion (lasted 7 m 30 s)"
    )
    assert email_messages.subject_for(_event("digest", "digest", "x")) == "[Sentinel] Daily digest · server-1"
    assert email_messages.connected_email(HOST).subject == "[Sentinel] Test email · server-1"
    report = email_messages.report_email("Weekly report · server-1", HOST, "Last 7 days", "r.html", "1.2 MB")
    assert report.subject == "[Sentinel] Report · server-1 · Weekly report · server-1"


def test_subjects_stay_on_one_line_and_short() -> None:
    evil = _alert(title="Log\r\nBcc: someone@example.org\nerrors")
    subject = email_messages.subject_for(_event("alert", "opened", "x", alert=evil))
    assert "\r" not in subject and "\n" not in subject and "Log Bcc: someone@example.org errors" in subject
    long = email_messages.subject("x" * 500)
    assert len(long) == email_messages.MAX_SUBJECT and long.endswith("…")


# -------------------------------------------------------------------------------- bodies


def test_alert_body_carries_the_telegram_facts() -> None:
    alert = _alert(title="Log <error> & stuff")
    content = email_messages.render_event(
        _event("alert", "opened", messages.alert_html(alert, HOST), alert=alert)
    )
    html = content.html
    assert "🔴 CRITICAL</span>" in html and "server-1" in html
    assert "<h1" in html and "Log &lt;error&gt; &amp; stuff</h1>" in html
    assert "service ✅ → TCP ✅ → connect ❌" in html and "monospace" in html
    assert "connection refused (localhost:5432)" in html and "Started 10:43" in html
    # self-contained: nothing is fetched, styles are inline, both colour schemes are declared
    for needle in ("<img", "<link", "<script", "<style", "src=", "href=", "url(", "@import", "http"):
        assert needle not in html.lower(), needle
    assert '<meta name="color-scheme" content="light dark">' in html
    # the text part is the Telegram message's plain twin
    assert content.text.startswith(messages.to_plain(messages.alert_html(alert, HOST)))
    assert "More › Email" in content.text


def test_incident_digest_resolved_and_test_bodies() -> None:
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
        ],
    )
    html = email_messages.render_event(
        _event("incident", "opened", messages.incident_html(incident, HOST), incident=incident)
    ).html
    assert "INCIDENT #24</span>" in html and "Possible Solr resource exhaustion</h1>" in html
    assert "<table" in html and f">{clock(_local(10, 41), zone=True)}</td>" in html
    assert ">Memory 94 %</td>" in html
    assert "Affected: DSpace Search, DSpace Backend" in html
    resolved = _alert(status="resolved", resolved_ts=_local(10, 43) + 372)
    html = email_messages.render_event(
        _event("alert", "resolved", messages.resolved_html(resolved, HOST), alert=resolved)
    ).html
    assert "#067647" in html and "PostgreSQL unreachable · lasted 6 m 12 s</h1>" in html
    digest = messages.digest_html(HOST, [], [], [], _local(8, 0))
    html = email_messages.render_event(_event("digest", "digest", digest, "warning")).html
    assert "Daily digest</span>" in html and "Open now: nothing — all clear" in html
    connected = email_messages.connected_email(HOST)
    assert "DSpace Sentinel is connected</h1>" in connected.html and "reached you" in connected.text


# ---------------------------------------------------------------------------- the message


def test_one_message_with_every_header() -> None:
    cfg = email_cfg()
    content = email_messages.connected_email(HOST)
    msg = mail.build_message(cfg, content, ["ops@example.org", "boss@example.org"])
    assert msg["From"] == "DSpace Sentinel <sentinel@example.org>"
    assert msg["To"] == "ops@example.org, boss@example.org"
    assert msg["Subject"] == "[Sentinel] Test email · server-1"
    assert msg["Auto-Submitted"] == "auto-generated" and msg["X-Auto-Response-Suppress"] == "All"
    assert msg["Date"] and msg["Message-ID"].endswith("@example.org>")
    assert msg.get_content_type() == "multipart/alternative"
    assert [p.get_content_type() for p in msg.iter_parts()] == ["text/plain", "text/html"]
    raw = msg.as_bytes(policy=email.policy.SMTP)
    assert all(byte < 128 for byte in raw)  # 7-bit clean: any relay takes it
    # the From address and name can be set; a report travels as an attachment
    cfg = merge_patch(
        cfg, {"notifiers": {"email": {"from_address": "noreply@example.org", "from_name": "Repo"}}}
    )
    msg = mail.build_message(cfg, content, ["ops@example.org"], attachment=("r.md", b"# Report"))
    assert msg["From"] == "Repo <noreply@example.org>"
    assert [p.get_content_type() for p in msg.iter_parts()] == ["multipart/alternative", "text/markdown"]
    assert next(msg.iter_attachments()).get_filename() == "r.md"


# ----------------------------------------------------------------------- a real SMTP session


class _FakeSMTP(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _SMTPHandler)
        self.commands: list[str] = []
        self.messages: list[bytes] = []
        self.password = PASSWORD
        self.refuse: set[str] = set()


class _SMTPHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server: _FakeSMTP = self.server  # type: ignore[assignment]
        self.wfile.write(b"220 fake.smtp ESMTP\r\n")
        body: list[bytes] | None = None
        while line := self.rfile.readline():
            if body is not None:
                if line == b".\r\n":
                    server.messages.append(b"".join(body))
                    body = None
                    self.wfile.write(b"250 2.0.0 queued\r\n")
                else:
                    body.append(line[1:] if line.startswith(b"..") else line)
                continue
            command = line.decode().strip()
            server.commands.append(command)
            verb = command.split(" ", 1)[0].upper()
            if verb == "EHLO":
                self.wfile.write(b"250-fake.smtp\r\n250-AUTH PLAIN\r\n250 SIZE 1000000\r\n")
            elif verb == "AUTH":
                user_pass = base64.b64decode(command.split()[2]).split(b"\0")
                ok = user_pass[2].decode() == server.password
                self.wfile.write(b"235 2.7.0 ok\r\n" if ok else b"535 5.7.8 bad credentials\r\n")
            elif verb == "MAIL":
                self.wfile.write(b"250 2.1.0 ok\r\n")
            elif verb == "RCPT":
                address = command.split("<", 1)[1].split(">", 1)[0]
                refused = address in server.refuse
                self.wfile.write(b"550 5.1.1 no such mailbox\r\n" if refused else b"250 2.1.5 ok\r\n")
            elif verb == "DATA":
                body = []
                self.wfile.write(b"354 go ahead\r\n")
            elif verb == "QUIT":
                self.wfile.write(b"221 bye\r\n")
                return
            else:
                self.wfile.write(b"250 ok\r\n")


@pytest.fixture
def smtp_server() -> Iterator[_FakeSMTP]:
    server = _FakeSMTP()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def local_cfg(server: _FakeSMTP, **email_section: Any) -> Config:
    port = server.server_address[1]
    return email_cfg(port=port, smtp_host="127.0.0.1", security="none", **email_section)


async def test_one_session_one_message_to_every_recipient(smtp_server: _FakeSMTP) -> None:
    smtp_server.refuse = {"boss@example.org"}
    cfg = local_cfg(smtp_server)
    result = await mail.send_test(cfg)
    assert result["ok"] is False
    assert result["results"] == [
        {"address": "ops@example.org", "label": "On call", "ok": True, "error": None},
        {
            "address": "boss@example.org",
            "label": "Boss",
            "ok": False,
            "error": "refused by the mail server: 550 5.1.1 no such mailbox",
        },
    ]
    assert result["error"].startswith("1 of 2 recipients failed — Boss: refused by the mail server")
    verbs = [c.split(" ", 1)[0].upper() for c in smtp_server.commands]
    assert verbs == ["EHLO", "AUTH", "MAIL", "RCPT", "RCPT", "DATA", "QUIT"]
    (raw,) = smtp_server.messages
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    assert (
        msg["To"] == "ops@example.org, boss@example.org"
        and msg["Subject"] == "[Sentinel] Test email · server-1"
    )
    assert "DSpace Sentinel is connected" in msg.get_body(("plain",)).get_content()
    # the test goes to one address when asked
    smtp_server.messages.clear()
    one = await mail.send_test(cfg, address="OPS@example.org")
    assert one["ok"] is True and [r["address"] for r in one["results"]] == ["ops@example.org"]
    assert (
        email.message_from_bytes(smtp_server.messages[0], policy=email.policy.default)["To"]
        == "ops@example.org"
    )


async def test_a_refused_login_is_a_sentence_without_the_password(smtp_server: _FakeSMTP) -> None:
    smtp_server.password = "something else"
    result = await mail.send_test(local_cfg(smtp_server))
    error = "the mail server refused the login (535): check the username and password"
    assert result["ok"] is False and result["error"] == error
    assert [r["error"] for r in result["results"]] == [error, error] and smtp_server.messages == []
    assert PASSWORD not in str(result)


class _StallsInData(socketserver.ThreadingTCPServer):
    """Takes the session up to ``354`` and then stops reading: a slow uplink, as the sender sees it."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _StallsInDataHandler)
        self.released = threading.Event()
        self.reached_data = False


class _StallsInDataHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server: _StallsInData = self.server  # type: ignore[assignment]
        self.wfile.write(b"220 stall.smtp ESMTP\r\n")
        while line := self.rfile.readline():
            verb = line.split(b" ", 1)[0].strip().upper()
            if verb == b"EHLO":
                self.wfile.write(b"250-stall.smtp\r\n250 8BITMIME\r\n")
            elif verb == b"DATA":
                self.wfile.write(b"354 go ahead\r\n")
                server.reached_data = True
                server.released.wait(10)  # not one byte of the message is read
                return
            else:
                self.wfile.write(b"250 ok\r\n")


async def test_a_message_that_cannot_be_handed_over_in_time_is_a_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A large report over a slow uplink: the socket timeout ends smtplib's send of the DATA,
    which smtplib reports as "Server not connected". The mail server closed nothing and the port
    and security are right, so the sentence is the time-out one."""

    class SmallSendBuffer(smtplib.SMTP):  # so that 2 MB are enough to fill the pipe, on any machine
        def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
            sock = super()._get_socket(host, port, timeout)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 32 * 1024)
            return sock

    monkeypatch.setattr(smtplib, "SMTP", SmallSendBuffer)
    monkeypatch.setattr(mail, "TIMEOUT_S", 1.0)
    report = tmp_path / "sentinel-report-20260926-0800-30d.html"
    with report.open("wb") as fh:
        fh.truncate(2_000_000)  # sparse: nothing is written
    content = email_messages.report_email("Monthly", HOST, "Last 30 days", report.name, "1.9 MB")
    server = _StallsInData()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        cfg = email_cfg(
            port=port,
            smtp_host="127.0.0.1",
            security="none",
            username="",
            password="",
            from_address="s@example.org",
        )
        result = await mail.send_file(cfg, content, report)
    finally:
        server.released.set()
        server.shutdown()
        server.server_close()
    assert server.reached_data is True
    expected = f"127.0.0.1:{port} did not answer within 1 s"
    assert result["ok"] is False and result["error"] == expected
    assert [r["error"] for r in result["results"]] == [expected, expected]


async def test_no_password_travels_unencrypted_to_another_host(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover - must not be reached
        raise AssertionError("connected")

    monkeypatch.setattr(smtplib, "SMTP", refuse)
    result = await mail.send_test(email_cfg(security="none", smtp_port=25))
    assert result["ok"] is False and result["error"].startswith(
        'security "none" would send the password unencrypted'
    )


# ---------------------------------------------------------------- TLS modes and errors (fakes)


class FakeServer:
    """Stands in for smtplib.SMTP / SMTP_SSL and records what Sentinel asked of it."""

    instances: list[FakeServer] = []
    fail: dict[str, BaseException] = {}

    def __init__(self, host: str, port: int, local_hostname: str | None = None, **kwargs: Any) -> None:
        self.host, self.port, self.kwargs = host, port, kwargs
        self.calls: list[str] = []
        FakeServer.instances.append(self)
        if "connect" in FakeServer.fail:
            raise FakeServer.fail["connect"]

    def _step(self, name: str) -> None:
        self.calls.append(name)
        if name in FakeServer.fail:
            raise FakeServer.fail[name]

    def ehlo(self) -> None:
        self._step("ehlo")

    def starttls(self, context: ssl.SSLContext) -> None:
        self.context = context
        self._step("starttls")

    def login(self, user: str, password: str) -> None:
        self.login_args = (user, password)
        self._step("login")

    def send_message(self, msg: Any, from_addr: str, to_addrs: list[str]) -> dict[str, Any]:
        self.sent = (msg, from_addr, to_addrs)
        self._step("send")
        return {}

    def quit(self) -> None:
        self.calls.append("quit")

    def close(self) -> None:  # pragma: no cover
        self.calls.append("close")


@pytest.fixture
def fake_smtp(monkeypatch: pytest.MonkeyPatch) -> type[FakeServer]:
    FakeServer.instances = []
    FakeServer.fail = {}
    monkeypatch.setattr(smtplib, "SMTP", FakeServer)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeServer)
    return FakeServer


async def test_starttls_on_587_verifies_the_certificate(fake_smtp: type[FakeServer]) -> None:
    result = await mail.send_test(email_cfg(smtp_host="smtp.gmail.com"))
    assert result["ok"] is True
    (server,) = fake_smtp.instances
    assert (server.host, server.port, server.kwargs["timeout"]) == ("smtp.gmail.com", 587, 20.0)
    assert server.calls == ["ehlo", "starttls", "ehlo", "login", "send", "quit"]
    assert server.context.verify_mode == ssl.CERT_REQUIRED and server.context.check_hostname is True
    assert server.login_args == ("sentinel@example.org", PASSWORD)
    assert server.sent[1:] == ("sentinel@example.org", ["ops@example.org", "boss@example.org"])


async def test_ssl_on_465_uses_implicit_tls(fake_smtp: type[FakeServer]) -> None:
    await mail.send_test(email_cfg(port=465, security="ssl"))
    (server,) = fake_smtp.instances
    assert server.port == 465 and server.kwargs["context"].verify_mode == ssl.CERT_REQUIRED
    assert server.calls == ["ehlo", "login", "send", "quit"]  # no STARTTLS inside TLS


def _disconnected(
    context: BaseException | None = None, cause: BaseException | None = None
) -> smtplib.SMTPServerDisconnected:
    """What ``smtplib.SMTP.send()`` raises when the socket fails under it: the real reason is
    only in ``__context__`` (or ``__cause__``)."""
    exc = smtplib.SMTPServerDisconnected("Server not connected")
    exc.__context__, exc.__cause__ = context, cause
    return exc


GMAIL_APP_PASSWORD = (
    "use an app password made in that same Google account (Google Account › Security › "
    "2-Step Verification › App passwords), not the normal password"
)
GMAIL_CLOSED = (
    "smtp.gmail.com:587 closed the connection. Gmail does this for a few minutes after a refused "
    "login: check the address and the app password, wait, then test again. Otherwise check the port "
    "and security (Gmail: starttls on port 587, ssl on port 465)"
)


@pytest.mark.parametrize(
    ("where", "exc", "section", "expected"),
    [
        (  # Gmail's answer as smtplib gives it: two lines, the second a link
            "login",
            smtplib.SMTPAuthenticationError(
                535,
                b"5.7.8 Username and Password not accepted. For more information, go to\n"
                b"5.7.8  https://support.google.com/mail/?p=BadCredentials d9443c01a7336-2e1f3b5 - gsmtp",
            ),
            {"smtp_host": "smtp.gmail.com"},
            "Gmail refused the login for sentinel@example.org (535 5.7.8 Username and Password not "
            f"accepted): {GMAIL_APP_PASSWORD}",
        ),
        (
            "login",
            smtplib.SMTPAuthenticationError(534, b"5.7.9 Application-specific password required"),
            {"smtp_host": "smtp.gmail.com"},
            "Gmail refused the login for sentinel@example.org (534 5.7.9 Application-specific password "
            f"required): {GMAIL_APP_PASSWORD}",
        ),
        (  # T12: a moment after a refused login Gmail drops new sessions; port and security are right
            "ehlo",
            smtplib.SMTPServerDisconnected("Connection unexpectedly closed"),
            {"smtp_host": "smtp.gmail.com"},
            GMAIL_CLOSED,
        ),
        (
            "send",
            _disconnected(context=BrokenPipeError(32, "Broken pipe")),
            {"smtp_host": "smtp.gmail.com"},
            GMAIL_CLOSED,
        ),
        (  # a time-out stays a time-out, on Gmail too
            "ehlo",
            smtplib.SMTPServerDisconnected("Connection unexpectedly closed: timed out"),
            {"smtp_host": "smtp.gmail.com"},
            "smtp.gmail.com:587 did not answer within 20 s",
        ),
        (
            "connect",
            socket.gaierror(-2, "Name or service not known"),
            {},
            "cannot find the mail server smtp.example.org: check its name",
        ),
        (
            "connect",
            ConnectionRefusedError(111, "Connection refused"),
            {},
            "smtp.example.org:587 refused the connection: check the server name and port",
        ),
        ("connect", TimeoutError("timed out"), {}, "smtp.example.org:587 did not answer within 20 s"),
        (
            "ehlo",
            smtplib.SMTPServerDisconnected("Connection unexpectedly closed: timed out"),
            {"smtp_port": 465},
            'smtp.example.org:465 did not answer within 20 s (port 465 needs security "ssl")',
        ),
        (
            "ehlo",
            smtplib.SMTPServerDisconnected("Connection unexpectedly closed"),
            {},
            "smtp.example.org:587 closed the connection: check the port and security"
            " (Gmail: starttls on port 587, ssl on port 465)",
        ),
        (  # smtplib's send() hides the time-out of a write behind "Server not connected"
            "send",
            _disconnected(context=TimeoutError("timed out")),
            {},
            "smtp.example.org:587 did not answer within 20 s",
        ),
        (
            "send",
            _disconnected(cause=TimeoutError("The write operation timed out")),
            {"smtp_port": 465},
            'smtp.example.org:465 did not answer within 20 s (port 465 needs security "ssl")',
        ),
        (  # the server really went away in the middle of the message
            "send",
            _disconnected(context=BrokenPipeError(32, "Broken pipe")),
            {},
            "smtp.example.org:587 closed the connection: check the port and security"
            " (Gmail: starttls on port 587, ssl on port 465)",
        ),
        (
            "starttls",
            smtplib.SMTPNotSupportedError("STARTTLS extension not supported by server."),
            {},
            'smtp.example.org:587 does not offer STARTTLS: use security "ssl" (port 465)'
            " or the server's TLS port",
        ),
        (
            "starttls",
            ssl.SSLCertVerificationError(1, "certificate verify failed"),
            {},
            "the TLS certificate of smtp.example.org could not be verified (not trusted):"
            " check the server name",
        ),
        (
            "starttls",
            ssl.SSLError(1, "wrong version number"),
            {},
            "TLS with smtp.example.org:587 failed: check the security setting"
            " (Gmail: starttls on port 587, ssl on port 465)",
        ),
        (
            "send",
            smtplib.SMTPDataError(552, b"5.3.4 Message size exceeds fixed limit"),
            {},
            "the mail server refused the message: 552 5.3.4 Message size exceeds fixed limit",
        ),
        (
            "send",
            smtplib.SMTPSenderRefused(553, b"5.7.1 not allowed", "sentinel@example.org"),
            {},
            "the mail server refused the sender sentinel@example.org: 553 5.7.1 not allowed",
        ),
    ],
)
async def test_smtp_failures_read_as_sentences(
    fake_smtp: type[FakeServer], where: str, exc: BaseException, section: dict[str, Any], expected: str
) -> None:
    fake_smtp.fail = {where: exc}
    result = await mail.send_test(email_cfg(**section))
    assert result["ok"] is False and result["error"] == expected
    assert [r["error"] for r in result["results"]] == [expected, expected]


async def test_the_password_never_appears_in_an_error(
    fake_smtp: type[FakeServer], caplog: pytest.LogCaptureFixture
) -> None:
    echoed = base64.b64encode(f"\0sentinel@example.org\0{PASSWORD}".encode()).decode()
    fake_smtp.fail = {
        "login": smtplib.SMTPResponseException(454, f"temporary failure for {PASSWORD} {echoed}".encode())
    }
    result = await mail.send_test(email_cfg())
    assert result["error"] == "the mail server answered 454 temporary failure for ••• •••"
    assert PASSWORD not in str(result) and echoed not in str(result)
    assert PASSWORD not in caplog.text and echoed not in caplog.text


async def test_gmail_names_the_login_and_its_first_line_never_the_password(
    fake_smtp: type[FakeServer], caplog: pytest.LogCaptureFixture
) -> None:
    """Gmail's refusal names the account it refused and the first line of its answer, kept short,
    with the password taken out before the answer is cut (no part of it is left)."""
    gmail = email_cfg(smtp_host="smtp.gmail.com")
    fake_smtp.fail = {
        "login": smtplib.SMTPAuthenticationError(535, f"5.7.8 {PASSWORD} not accepted".encode())
    }
    assert (await mail.send_test(gmail))["error"] == (
        f"Gmail refused the login for sentinel@example.org (535 5.7.8 ••• not accepted): {GMAIL_APP_PASSWORD}"
    )
    # a long first line is cut after a whole word; the password where the cut falls is gone first
    at_the_cut = f"5.7.8 {'y' * 70} {PASSWORD} was echoed".encode()
    fake_smtp.fail = {"login": smtplib.SMTPAuthenticationError(535, at_the_cut)}
    assert (await mail.send_test(gmail))["error"] == (
        f"Gmail refused the login for sentinel@example.org (535 5.7.8 {'y' * 70}…): {GMAIL_APP_PASSWORD}"
    )
    fake_smtp.fail = {"login": smtplib.SMTPAuthenticationError(535, b"")}
    assert (await mail.send_test(gmail))["error"] == (
        f"Gmail refused the login for sentinel@example.org (535): {GMAIL_APP_PASSWORD}"
    )
    assert PASSWORD not in caplog.text and PASSWORD[:4] not in caplog.text


def _past_validation(cfg: Config, **email_fields: Any) -> Config:
    """``cfg`` with email fields set as if they had got past validation (the settings file, the
    environment): assigning the section would validate it again."""
    cfg.notifiers.__dict__["email"] = cfg.notifiers.email.model_copy(update=email_fields)
    return cfg


NOT_SENDABLE = (
    "the email password has a character that cannot be sent: "
    "use letters, digits and symbols from the English keyboard"
)


async def test_a_password_that_cannot_be_sent_is_said_without_any_connection(
    fake_smtp: type[FakeServer], caplog: pytest.LogCaptureFixture
) -> None:
    """smtplib sends AUTH as ASCII, so a password with "ä" (or the mask "••••••" pasted into the
    file) can never log in: email is not ready, and says so in one sentence."""
    assert mail.PASSWORD_NOT_SENDABLE == NOT_SENDABLE
    for password in ("pässwort-123", "••••••", "hunter2 ✓"):
        cfg = _past_validation(email_cfg(), password=password)
        assert mail.unready(cfg) == NOT_SENDABLE
        content = email_messages.connected_email(HOST)
        for result in (await mail.send_test(cfg), await mail.send(cfg, content)):
            assert result == {"ok": False, "error": NOT_SENDABLE, "results": []}
    assert fake_smtp.instances == []
    # a login name that cannot be sent is the same kind of trouble, said about the right field
    named = _past_validation(email_cfg(), username="benutzer-ä", from_address="sentinel@example.org")
    assert mail.unready(named) == (
        "the SMTP username has a character that cannot be sent: "
        "use letters, digits and symbols from the English keyboard"
    )
    # a relay that needs no login sends no password: what is stored there does not matter
    relay = _past_validation(email_cfg(), username="", password="ä", from_address="sentinel@example.org")
    assert mail.unready(relay) is None
    assert "ä" not in caplog.text and "•" not in caplog.text


async def test_an_encoding_failure_never_shows_the_character_or_where_it_stands(
    smtp_server: _FakeSMTP, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Past the readiness check, smtplib's own failure is "UnicodeEncodeError: 'ascii' codec can't
    encode character '\\xe4' in position 20": one character of the password and where it stands.
    The sentence, the results and the log never carry it."""
    cfg = _past_validation(local_cfg(smtp_server), password="pässwort-123")
    monkeypatch.setattr(mail, "unready", lambda cfg: None)  # as if the check were not there
    result = await mail.send_test(cfg)
    assert result["ok"] is False and result["error"] == NOT_SENDABLE
    assert [r["error"] for r in result["results"]] == [NOT_SENDABLE, NOT_SENDABLE]
    assert [c.split(" ", 1)[0].upper() for c in smtp_server.commands] == ["EHLO", "QUIT"]  # no AUTH
    for text in (str(result), caplog.text):
        assert "UnicodeEncodeError" not in text and "position" not in text and "xe4" not in text
        assert "ä" not in text and "pässwort" not in text
    # the same failure for the login name names the login name
    user = _past_validation(local_cfg(smtp_server), username="benutzer-ä", from_address="s@example.org")
    assert (await mail.send_test(user))["error"].startswith("the SMTP username has a character")


def test_an_unknown_failure_is_named_by_its_class_only() -> None:
    """The last branch of describe_error never prints the exception's own text: whatever a
    library put there (a character of the password, a position, a path) stays out of the events
    log, the API answer and the server log."""
    cfg = email_cfg()
    boom = RuntimeError(f"cannot encode {PASSWORD[3]!r} at position 3 of {PASSWORD}")
    assert mail.describe_error(boom, cfg) == "unexpected error (RuntimeError)"
    assert mail.describe_error(KeyError("password"), cfg) == "unexpected error (KeyError)"
    # an encoding failure that is neither the password's nor the login's: no guess, no detail
    try:
        "sentinel@exämple.org".encode("ascii")
    except UnicodeEncodeError as exc:
        assert mail.describe_error(exc, cfg) == "unexpected error (UnicodeEncodeError)"


async def test_every_recipient_refused(fake_smtp: type[FakeServer]) -> None:
    refused = {a: (550, b"5.1.1 unknown") for a in ("ops@example.org", "boss@example.org")}
    fake_smtp.fail = {"send": smtplib.SMTPRecipientsRefused(refused)}
    result = await mail.send_test(email_cfg())
    assert result["ok"] is False
    assert [r["error"] for r in result["results"]] == ["refused by the mail server: 550 5.1.1 unknown"] * 2


async def test_nothing_is_sent_unless_set_up_and_never_in_demo(fake_smtp: type[FakeServer]) -> None:
    unset = merge_patch(
        Config(), {"notifiers": {"email": {"enabled": True, "recipients": [{"address": "a@b.org"}]}}}
    )
    assert await mail.send_test(unset) == {"ok": False, "error": mail.NOT_SET_UP, "results": []}
    no_recipient = email_cfg(recipients=[])
    assert (await mail.send_test(no_recipient))["error"] == mail.NOT_SET_UP
    demo = email_cfg()
    demo.demo = True
    content = email_messages.connected_email(HOST)
    for coro in (
        mail.send_test(demo),
        mail.send(demo, content),
        mail.send_event(demo, _event("digest", "digest", "x")),
        mail.send_file(demo, content, "/nope"),
    ):
        answer = await coro
        assert answer["ok"] is False and answer["skipped"] == "demo" and answer["error"] == mail.DEMO_ERROR
    assert fake_smtp.instances == []
    # a relay that needs no login only needs a sender
    relay = email_cfg(
        username="", password="", from_address="sentinel@example.org", security="none", smtp_port=25
    )
    assert (await mail.send_test(relay))["ok"] is True and fake_smtp.instances[-1].calls == [
        "ehlo",
        "send",
        "quit",
    ]


async def test_a_login_that_is_not_an_address_is_a_sentence_not_a_python_error(
    fake_smtp: type[FakeServer],
) -> None:
    """R1#3: the configuration refuses it; one that got past validation still reads as a sentence
    (never "InvalidHeaderDefect: addr-spec local part with no domain") and opens no connection."""
    cfg = email_cfg()
    # as if it had got past validation (assigning the section would validate it again)
    cfg.notifiers.__dict__["email"] = cfg.notifiers.email.model_copy(
        update={"username": "AKIAIOSFODNN7EXAMPLE"}
    )
    expected = "the SMTP username is not an email address: set from_address (the address alerts come from)"
    assert mail.SENDER_NOT_AN_ADDRESS == expected
    for result in (await mail.send_test(cfg), await mail.send(cfg, email_messages.connected_email(HOST))):
        assert result == {"ok": False, "error": expected, "results": []}
    assert fake_smtp.instances == []
    with pytest.raises(mail.SenderNotAnAddress):
        mail.build_message(cfg, email_messages.connected_email(HOST), ["ops@example.org"])
    assert mail.describe_error(mail.SenderNotAnAddress("AKIAIOSFODNN7EXAMPLE"), cfg) == expected


async def test_a_report_too_large_for_gmail_is_named_not_sent(
    fake_smtp: type[FakeServer], tmp_path: Path
) -> None:
    """The limit is 17 MB counted the way Sentinel shows every size (1024-based, as the report
    list does), and the refused file's size is rounded up, so it never reads like the limit."""
    assert mail.MAX_ATTACHMENT_BYTES == 17 * 1024 * 1024 == 17_825_792
    assert bytes_(mail.MAX_ATTACHMENT_BYTES) == "17.0 MB"
    big = tmp_path / "sentinel-report-20260926-0800-30d.html"
    content = email_messages.report_email("Monthly", HOST, "Last 30 days", big.name, "19.5 MB")
    for size, shown in (
        (17_825_793, "17.1 MB"),  # one byte over: never "17.0 MB; the limit is 17 MB"
        (17_930_649, "17.1 MB"),
        (17_930_650, "17.2 MB"),
        (18_000_000, "17.2 MB"),  # the report list shows this file as 17.2 MB too
        (20_447_232, "19.5 MB"),
        (1_073_741_825, "1.0 GB"),
    ):
        with big.open("wb") as fh:
            fh.truncate(size)  # sparse: nothing is written
        result = await mail.send_file(email_cfg(), content, big)
        assert result == {
            "ok": False,
            "error": f"{big.name} is too large to email ({shown}; the limit is 17 MB)",
            "results": [],
        }, size
    assert fake_smtp.instances == []
    with big.open("wb") as fh:
        fh.truncate(mail.MAX_ATTACHMENT_BYTES)  # a file of exactly the limit is sent
    assert (await mail.send_file(email_cfg(), content, big))["ok"] is True
    assert len(fake_smtp.instances) == 1


class _CountingSink(socketserver.ThreadingTCPServer):
    """Takes any message and keeps only its size: the one MAIL FROM declared and the DATA's bytes."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _CountingHandler)
        self.declared: int | None = None
        self.received: int | None = None


class _CountingHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server: _CountingSink = self.server  # type: ignore[assignment]
        self.wfile.write(b"220 sink.smtp ESMTP\r\n")
        while line := self.rfile.readline():
            verb = line.split(b" ", 1)[0].strip().upper()
            if verb == b"EHLO":
                self.wfile.write(b"250-sink.smtp\r\n250 SIZE 35882577\r\n")  # as smtp.gmail.com announces
            elif verb == b"MAIL":
                declared = re.search(rb"size=(\d+)", line, re.IGNORECASE)
                server.declared = int(declared.group(1)) if declared else None
                self.wfile.write(b"250 ok\r\n")
            elif verb == b"DATA":
                self.wfile.write(b"354 go ahead\r\n")
                count, tail = 0, b""
                while tail != b"\r\n.\r\n":
                    chunk = self.rfile.read1(65536)
                    if not chunk:
                        return
                    count, tail = count + len(chunk), (tail + chunk)[-5:]
                server.received = count - 3  # without the closing ".\r\n"
                self.wfile.write(b"250 queued\r\n")
            elif verb == b"QUIT":
                self.wfile.write(b"221 bye\r\n")
                return
            else:
                self.wfile.write(b"250 ok\r\n")


async def test_a_report_of_exactly_the_limit_fits_gmails_25_mb_on_the_wire(tmp_path: Path) -> None:
    """base64 makes the attachment a third larger and adds a line break every 76 characters: a
    file of exactly the limit is about 24.4 MB once it travels, bodies and headers included."""
    report = tmp_path / "sentinel-report-20260926-0800-30d.html"
    with report.open("wb") as fh:
        fh.truncate(mail.MAX_ATTACHMENT_BYTES)
    content = email_messages.report_email("Monthly", HOST, "Last 30 days", report.name, "17.0 MB")
    sink = _CountingSink()
    thread = threading.Thread(target=sink.serve_forever, daemon=True)
    thread.start()
    try:
        cfg = email_cfg(
            port=sink.server_address[1],
            smtp_host="127.0.0.1",
            security="none",
            username="",
            password="",
            from_address="sentinel@example.org",
        )
        result = await mail.send_file(cfg, content, report)
    finally:
        sink.shutdown()
        sink.server_close()
    assert result["ok"] is True, result
    assert sink.declared is not None and sink.received is not None
    assert 24_390_000 < sink.declared < 24_400_000 and abs(sink.received - sink.declared) < 1000
    assert max(sink.declared, sink.received) < 25_000_000  # Gmail's limit, read the strict way


async def test_reports_travel_as_attachments(fake_smtp: type[FakeServer], tmp_path: Path) -> None:
    report = tmp_path / "sentinel-report-20260926-0800-7d.html"
    report.write_text("<html>report</html>", encoding="utf-8")
    content = email_messages.report_email("Weekly report", HOST, "Last 7 days", report.name, "19 B")
    result = await mail.send_file(email_cfg(), content, report)
    assert result["ok"] is True
    msg = fake_smtp.instances[-1].sent[0]
    attachment = next(msg.iter_attachments())
    assert attachment.get_filename() == report.name and attachment.get_content_type() == "text/html"
    assert fake_smtp.instances[-1].sent[2] == [
        "ops@example.org",
        "boss@example.org",
    ]  # everyone, whatever severity
    missing = await mail.send_file(email_cfg(), content, tmp_path / "gone.html")
    assert missing["ok"] is False and "cannot read gone.html" in missing["error"]


@pytest.mark.parametrize(
    ("name", "content_type"),
    [
        ("sentinel-report-20260926-0800-24h.html", 'text/html; charset="utf-8"'),
        ("sentinel-report-20260926-0800-24h.md", 'text/markdown; charset="utf-8"'),
        ("sentinel-report-20260926-0800-24h.json", "application/json"),
    ],
)
async def test_a_text_report_says_it_is_utf8(
    fake_smtp: type[FakeServer], tmp_path: Path, name: str, content_type: str
) -> None:
    """Without a charset a text/* part is US-ASCII (RFC 2046) and text/markdown must name one
    (RFC 7763): a reader that shows the attachment from its header garbled every report's "·",
    "→" and status emoji."""
    line = '# Q3 <uptime> & "review" · 17 d → 😀'
    report = tmp_path / name
    report.write_text(line + "\n", encoding="utf-8")
    content = email_messages.report_email("Q3", HOST, "Last 24 hours", name, "42 B")
    assert (await mail.send_file(email_cfg(), content, report))["ok"] is True
    sent = fake_smtp.instances[-1].sent[0]
    raw = sent.as_bytes(policy=email.policy.SMTP)
    assert all(byte < 128 for byte in raw)  # still 7-bit clean on the wire
    # read back the way a mail client does: from the bytes, following the headers
    attachment = next(email.message_from_bytes(raw, policy=email.policy.default).iter_attachments())
    assert attachment["Content-Type"] == content_type
    assert attachment["Content-Transfer-Encoding"] == "base64" and attachment.get_filename() == name
    body = attachment.get_content()
    if isinstance(body, bytes):  # application/json: the bytes as they are
        body = body.decode("utf-8")
    assert body == line + "\n"


# ---------------------------------------------------------------- email in dispatch and digests


async def test_dispatch_sends_one_email_to_the_recipients_that_take_the_event(
    fake_smtp: type[FakeServer], mem_store: Store
) -> None:
    cfg = email_cfg()
    warning = _alert(severity="warning", title="Disk 87 % on /dspace", message="free 53.7 GB")
    event = _event("alert", "opened", messages.alert_html(warning, HOST), "warning", alert=warning)
    results = await notifiers.notify(mem_store, cfg, event, ts=100.0)
    email_result = next(r for r in results if r.channel == "email")
    assert (
        email_result.ok is True
        and email_result.detail["subject"] == "[Sentinel] Warning · server-1 · Disk 87 % on /dspace"
    )
    assert fake_smtp.instances[-1].sent[2] == ["ops@example.org"]  # Boss takes critical alerts only
    row = next(e for e in mem_store.events(kind="notify") if e.data["channel"] == "email")
    assert row.message == "email sent: s" and row.data["destinations"] == [
        {"destination": "ops@example.org", "label": "On call", "ok": True, "error": None}
    ]
    critical = _event("alert", "opened", messages.alert_html(_alert(), HOST), alert=_alert())
    await notifiers.dispatch(cfg, critical, now=100.0)
    assert fake_smtp.instances[-1].sent[2] == ["ops@example.org", "boss@example.org"]


async def test_email_quiet_hours_are_its_own(fake_smtp: type[FakeServer]) -> None:
    night = _dt.datetime(2024, 3, 5, 2, 30).timestamp()
    cfg = email_cfg(quiet_hours="23:00-07:00")
    event = _event("alert", "opened", "x", "warning", alert=_alert(severity="warning"))
    results = {r.channel: r for r in await notifiers.dispatch(cfg, event, now=night)}
    assert results["email"].skipped == "quiet hours" and results["telegram"].skipped == "disabled"
    assert fake_smtp.instances == []
    critical = _event("alert", "opened", "x", alert=_alert())
    results = {r.channel: r for r in await notifiers.dispatch(cfg, critical, now=night)}
    assert results["email"].ok is True


async def test_demo_logs_the_email_it_would_have_sent(mem_store: Store) -> None:
    cfg = email_cfg()
    cfg.demo = True
    event = _event("alert", "opened", messages.alert_html(_alert(), HOST), alert=_alert())
    await notifiers.notify(mem_store, cfg, event, ts=100.0)
    (row,) = mem_store.events(kind="notify.demo")
    assert row.data["channels"] == ["telegram", "email"]
    assert row.data["subject"] == "[Sentinel] CRITICAL · server-1 · PostgreSQL unreachable"


async def test_each_channel_has_its_own_daily_digest(mem_store: Store) -> None:
    seen: list[dict[str, Any]] = []

    async def recorder(cfg: Config, event: dict[str, Any], now: float | None = None) -> list[Any]:
        seen.append(event)
        return [notifiers.NotifyResult("email", True)]

    cfg = email_cfg(daily_digest="07:30")
    cfg = merge_patch(cfg, {"notifiers": {"telegram": {"enabled": True, "daily_digest": "08:00"}}})
    engine = AlertEngine(mem_store, cfg, Bus(), dispatcher=recorder)
    evening = _dt.datetime(2024, 3, 5, 20, 0).timestamp()
    await engine.evaluate(make_sample(ts=evening), {}, evening)  # arms both
    assert mem_store.kv_get(DIGEST_KV_KEY) and mem_store.kv_get(EMAIL_DIGEST_KV_KEY) and seen == []
    at_0745 = _dt.datetime(2024, 3, 6, 7, 45).timestamp()
    await engine.evaluate(make_sample(ts=at_0745), {}, at_0745)
    await engine.flush()
    assert [e["channels"] for e in seen] == [["email"]]
    at_0805 = _dt.datetime(2024, 3, 6, 8, 5).timestamp()
    await engine.evaluate(make_sample(ts=at_0805), {}, at_0805)
    await engine.flush()
    assert [e["channels"] for e in seen] == [["email"], ["telegram", "webhook"]]
    # an email digest time with email switched off never fires
    off = merge_patch(cfg, {"notifiers": {"email": {"enabled": False}}})
    assert [slot[3] for slot in AlertEngine(mem_store, off, Bus(), dispatcher=recorder)._digest_slots()] == [
        True,
        False,
    ]


async def test_a_hanging_mail_server_never_holds_up_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SMTP session runs in a daemon thread: cancelling the delivery returns at once and the
    interpreter never waits for the thread at exit."""
    import asyncio

    release = threading.Event()
    threads: list[threading.Thread] = []

    class Hanging(FakeServer):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            threads.append(threading.current_thread())
            release.wait(5)

    FakeServer.instances, FakeServer.fail = [], {}
    monkeypatch.setattr(smtplib, "SMTP", Hanging)
    task = asyncio.ensure_future(mail.send_test(email_cfg()))
    for _ in range(100):
        if threads:
            break
        await asyncio.sleep(0.01)
    assert threads and threads[0].daemon and threads[0] is not threading.main_thread()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()  # the thread finishes on its own; its late answer is dropped quietly
    threads[0].join(2)
    assert not threads[0].is_alive()


async def test_a_switched_off_channel_is_never_held_back(mem_store: Store) -> None:
    """Quiet hours only matter for a channel that would send: a switched-off one says "disabled"
    and leaves no "held back by quiet hours" row behind."""
    night = _dt.datetime(2024, 3, 5, 2, 30).timestamp()
    cfg = email_cfg(enabled=False, quiet_hours="23:00-07:00")
    cfg = merge_patch(cfg, {"notifiers": {"telegram": {"quiet_hours": "23:00-07:00"}}})
    event = _event("alert", "opened", "x", "warning", alert=_alert(severity="warning"))
    results = await notifiers.notify(mem_store, cfg, event, ts=night)
    assert [(r.channel, r.skipped) for r in results] == [
        ("telegram", "disabled"),
        ("email", "disabled"),
        ("webhook", "disabled"),
    ]
    assert mem_store.events(kind="notify.skipped") == []
