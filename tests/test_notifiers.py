"""Notifier transports and dispatch: one bot per Telegram destination, token redaction, the
webhook's TLS/redirect policy, per-destination severity, per-channel quiet hours, the outbox."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt
import errno
import json
import os
import socket
import socketserver
import ssl
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from dspace_sentinel.alerts import notifiers
from dspace_sentinel.alerts.notifiers import NotifyResult, Outbox, telegram, webhook
from dspace_sentinel.config import Config, merge_patch
from dspace_sentinel.store import Store

# made-up tokens, split in two so that secret scanners do not report them
TOKEN = "123456:" + "SECRET-BOT-TOKEN-0123456789abcdefghij"
TOKEN_B = "987654321:" + "AAH-second-bot-token-0123456789abc"


def tg_cfg(**telegram_section: Any) -> Config:
    section = {"enabled": True, "chats": [{"bot_token": TOKEN, "chat_id": "42"}], **telegram_section}
    return merge_patch(Config(), {"server": {"hostname": "server-1"}, "notifiers": {"telegram": section}})


def two_bots(**extra: Any) -> Config:
    return tg_cfg(
        chats=[
            {"label": "Danindu", "bot_token": TOKEN, "chat_id": "111111"},
            {"label": "Team", "bot_token": TOKEN_B, "chat_id": "-100222", "min_severity": "critical"},
        ],
        **extra,
    )


def assert_no_token(text: str) -> None:
    low = text.lower()
    assert "secret-bot-token" not in low and "secret%2dbot" not in low, text
    assert "second-bot-token" not in low, text
    assert "•••" in text, text


def bot_api(calls: list[httpx.Request], answers: dict[str, Any] | None = None) -> httpx.AsyncClient:
    """A mock Bot API: ``answers[(token, method)]`` or ``answers[method]`` is the JSON reply."""
    answers = answers or {}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        token, method = request.url.path.split("/bot", 1)[1].split("/", 1)
        reply = answers.get((token, method), answers.get(method))
        if isinstance(reply, Exception):
            raise reply
        if reply is None:
            result: Any = {"message_id": 5}
            if method == "getMe":
                result = {"username": f"bot_{token.split(':')[0]}"}
            reply = {"ok": True, "result": result}
        return httpx.Response(200, json=reply)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


PROXY_VARIABLES = ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy")


@pytest.fixture(autouse=True)
def _no_proxy_in_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Telegram error says whether the requests went through a proxy: none unless a test sets one."""
    for name in PROXY_VARIABLES:
        monkeypatch.delenv(name, raising=False)


# ----------------------------------------------------------------------------- Telegram transport


async def test_connect_error_carrying_the_url_is_redacted() -> None:
    """A ConnectError whose text holds the request URL must not leak the (lower-cased) token."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed to reach {request.url}", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await telegram.send_message(tg_cfg(), "hi", client=client)
    assert result["ok"] is False
    assert_no_token(result["error"])
    assert result["error"] == (
        "cannot connect to api.telegram.org (failed to reach https://api.telegram.org/bot•••/sendMessage): "
        "check this server's network and firewall, or set HTTPS_PROXY in config/sentinel.env if the "
        "network has a proxy"
    )


def test_redact_is_case_insensitive_and_covers_the_encoded_token() -> None:
    low = TOKEN.lower()
    assert telegram.redact(f"x bot{low}/getMe", TOKEN) == "x bot•••/getMe"
    encoded = TOKEN.replace(":", "%3A")
    assert telegram.redact(f"x bot{encoded} y", TOKEN) == "x bot••• y"
    assert telegram.redact(f"{TOKEN} and {TOKEN_B}", [TOKEN, TOKEN_B]) == "••• and •••"
    assert telegram.redact("nothing to hide", TOKEN) == "nothing to hide"
    assert telegram.redact("anything", "") == "anything"


async def test_each_destination_is_its_own_bot() -> None:
    calls: list[httpx.Request] = []
    async with bot_api(calls) as client:
        result = await telegram.send_message(two_bots(), "<b>hi</b>", client=client)
    assert [c.url.path for c in calls] == [f"/bot{TOKEN}/sendMessage", f"/bot{TOKEN_B}/sendMessage"]
    assert [json.loads(c.content)["chat_id"] for c in calls] == ["111111", "-100222"]
    assert json.loads(calls[0].content) == {
        "chat_id": "111111",
        "text": "<b>hi</b>",
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    assert result["ok"] is True and result["message_id"] == 5 and result["error"] is None
    assert [(r["label"], r["chat_id"], r["ok"]) for r in result["results"]] == [
        ("Danindu", "111111", True),
        ("Team", "-100222", True),
    ]


async def test_one_failing_bot_never_stops_the_next_and_no_token_leaks() -> None:
    calls: list[httpx.Request] = []
    answers = {
        (TOKEN, "sendMessage"): {
            "ok": False,
            "error_code": 400,
            "description": "Bad Request: chat not found",
        },
        (TOKEN_B, "sendMessage"): httpx.ConnectError(f"cannot reach bot{TOKEN_B} or bot{TOKEN}"),
    }
    three = tg_cfg(
        chats=[
            {"label": "Danindu", "bot_token": TOKEN, "chat_id": "111111"},
            {"label": "Team", "bot_token": TOKEN_B, "chat_id": "-100222"},
            {"label": "Ops", "bot_token": TOKEN, "chat_id": "@sentinel_ops"},
        ]
    )
    async with bot_api(calls, answers) as client:
        result = await telegram.send_message(three, "x", client=client)
    assert len(calls) == 3 and result["ok"] is False
    errors = [r["error"] for r in result["results"]]
    assert errors[0] == "chat not found — send the bot a message first"
    assert_no_token(errors[1])
    assert errors[2] == "chat not found — add the bot to the channel as an administrator first"
    assert result["error"].startswith("3 of 3 bots failed — Danindu: chat not found")
    assert_no_token(result["error"])


@pytest.mark.parametrize(
    ("description", "code", "chat_id", "parameters", "expected"),
    [
        ("Bad Request: chat not found", 400, "123", None, "chat not found — send the bot a message first"),
        (
            "Bad Request: chat not found",
            400,
            "-100123",
            None,
            "chat not found — add the bot to the group first",
        ),
        (
            "Bad Request: chat not found",
            400,
            "@channel_x",
            None,
            "chat not found — add the bot to the channel as an administrator first",
        ),
        (
            "Forbidden: bot was blocked by the user",
            403,
            "1",
            None,
            "the bot was blocked in this chat — unblock it in Telegram",
        ),
        (
            "Forbidden: bot was kicked from the group chat",
            403,
            "-1",
            None,
            "the bot was removed from this group — add it again",
        ),
        (
            "Bad Request: group chat was upgraded to a supergroup chat",
            400,
            "-1",
            {"migrate_to_chat_id": -1001},
            "this group became a supergroup: its chat id is now -1001",
        ),
        (
            "Unauthorized",
            401,
            "1",
            None,
            "Telegram refused the bot token (401 Unauthorized) — copy it again from @BotFather",
        ),
        (
            "Forbidden: bot can't initiate conversation with a user",
            403,
            "123",
            None,
            "the bot has not been started in this chat — open it in Telegram and press Start",
        ),
        ("Too Many Requests: retry after 5", 429, "1", None, "Too Many Requests: retry after 5 (error 429)"),
    ],
)
def test_bot_api_refusals_become_advice(
    description: str, code: int, chat_id: str, parameters: Any, expected: str
) -> None:
    assert telegram.friendly_error(description, code, chat_id, parameters) == expected


async def test_send_test_asks_every_bot_its_name_then_sends() -> None:
    calls: list[httpx.Request] = []
    cfg = tg_cfg(
        chats=[
            {"label": "Danindu", "bot_token": TOKEN, "chat_id": "111111"},
            {"label": "Team", "bot_token": TOKEN_B, "chat_id": "-100222", "min_severity": "critical"},
            {"label": "No token yet", "chat_id": "333"},
            {"label": "No chat yet", "bot_token": TOKEN_B},
        ],
        enabled=False,  # the test goes out whether or not Telegram is switched on
    )
    async with bot_api(calls) as client:
        result = await telegram.send_test(cfg, client=client)
    methods = [c.url.path.rsplit("/", 1)[-1] for c in calls]
    assert methods == ["getMe", "sendMessage", "getMe", "sendMessage", "getMe"]
    assert json.loads(calls[1].content)["text"] == "👋 DSpace Sentinel is connected · server-1"
    assert [(r["label"], r["bot_name"], r["ok"], r["error"]) for r in result["results"]] == [
        ("Danindu", "@bot_123456", True, None),
        ("Team", "@bot_987654321", True, None),  # critical-only destinations get the test too
        ("No token yet", None, False, telegram.NO_TOKEN),
        ("No chat yet", "@bot_987654321", False, telegram.NO_CHAT),
    ]
    assert result["ok"] is False and result["bot_name"] == "@bot_123456"
    assert result["error"].startswith("2 of 4 bots failed — No token yet: no bot token")
    assert set(result["results"][0]) == {"id", "label", "chat_id", "bot_name", "ok", "error"}
    # one destination by id
    calls.clear()
    team_id = cfg.notifiers.telegram.chats[1].id
    async with bot_api(calls) as client:
        one = await telegram.send_test(cfg, destination_id=team_id, client=client)
    assert one["ok"] is True and [r["id"] for r in one["results"]] == [team_id] and len(calls) == 2
    # a refused token stops that destination at getMe
    calls.clear()
    refused = {"getMe": {"ok": False, "error_code": 401, "description": "Unauthorized"}}
    async with bot_api(calls, refused) as client:
        bad = await telegram.send_test(tg_cfg(), client=client)
    assert bad == {
        "ok": False,
        "bot_name": None,
        "error": "Telegram refused the bot token (401 Unauthorized) — copy it again from @BotFather",
        "results": [
            {
                "id": tg_cfg().notifiers.telegram.chats[0].id,
                "label": "",
                "chat_id": "42",
                "bot_name": None,
                "ok": False,
                "error": "Telegram refused the bot token (401 Unauthorized) — copy it again from @BotFather",
            }
        ],
    }
    assert [c.url.path.rsplit("/", 1)[-1] for c in calls] == ["getMe"]
    # nothing configured
    assert await telegram.send_test(merge_patch(Config(), {})) == {
        "ok": False,
        "bot_name": None,
        "error": telegram.NO_BOTS,
        "results": [],
    }


async def test_send_document_goes_through_every_bot(tmp_path: Path) -> None:
    report = tmp_path / "sentinel-report.html"
    report.write_text("<html>report</html>", encoding="utf-8")
    calls: list[httpx.Request] = []
    async with bot_api(calls) as client:
        result = await telegram.send_document(two_bots(), report, "Weekly report", client=client)
    assert result["ok"] is True and [r["ok"] for r in result["results"]] == [True, True]
    assert [c.url.path for c in calls] == [f"/bot{TOKEN}/sendDocument", f"/bot{TOKEN_B}/sendDocument"]
    for call in calls:
        assert call.headers["content-type"].startswith("multipart/form-data")
        assert b'filename="sentinel-report.html"' in call.content and b"<html>report</html>" in call.content


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
async def test_send_document_error_text_is_redacted(tmp_path: Path) -> None:
    folder = tmp_path / TOKEN
    folder.mkdir()
    report = folder / "report.html"
    report.write_text("<html></html>", encoding="utf-8")
    report.chmod(0)
    calls: list[httpx.Request] = []
    try:
        async with bot_api(calls) as client:
            result = await telegram.send_document(tg_cfg(), report, "caption", client=client)
    finally:
        report.chmod(0o600)
    assert result["ok"] is False and calls == []
    assert_no_token(result["error"])


async def test_nothing_is_sent_without_a_complete_bot() -> None:
    calls: list[httpx.Request] = []
    async with bot_api(calls) as client:
        result = await telegram.send_message(tg_cfg(chats=[{"chat_id": "42"}]), "x", client=client)
    assert result == {"ok": False, "message_id": None, "error": telegram.NO_BOTS, "results": []}
    assert calls == []


# ------------------------------------------------------------ requests that never get an answer

TIMED_OUT = (
    "api.telegram.org did not answer within 10 s: this server may not be able to reach Telegram "
    "(a firewall, or a proxy it has to use: set HTTPS_PROXY in config/sentinel.env)"
)
REFUSED = (
    "api.telegram.org refused the connection: a firewall on this server or its network probably blocks "
    "Telegram (allow outgoing HTTPS, port 443, or set HTTPS_PROXY in config/sentinel.env if the network "
    "has a proxy)"
)
NO_DNS = (
    "this server cannot look up api.telegram.org (DNS): check its name servers (/etc/resolv.conf), or "
    "set HTTPS_PROXY in config/sentinel.env if the network has a proxy"
)
UNVERIFIED = (
    "the TLS certificate of api.telegram.org could not be verified (unable to get local issuer "
    "certificate): check this server's date and time, and whether a proxy or firewall on this network "
    "intercepts HTTPS"
)
TLS_CUT = (
    "the secure connection to api.telegram.org failed (unexpected eof while reading): a firewall or proxy "
    "on this network may block or intercept Telegram (set HTTPS_PROXY in config/sentinel.env if the "
    "network has a proxy)"
)
NO_ROUTE = (
    "cannot connect to api.telegram.org (network is unreachable): check this server's network and "
    "firewall, or set HTTPS_PROXY in config/sentinel.env if the network has a proxy"
)


class _AllFailed(Exception):
    """anyio's ExceptionGroup when every address of the host failed (a stand-in that 3.10 has too)."""

    def __init__(self, exceptions: list[BaseException]) -> None:
        super().__init__("multiple connection attempts failed")
        self.exceptions = tuple(exceptions)


def _connect_failed(said: BaseException, text: str = "All connection attempts failed") -> httpx.ConnectError:
    """A ConnectError as httpx raises it: its text is httpcore's, whose cause is anyio's
    OSError("All connection attempts failed"), whose cause is what the socket said."""
    attempts = OSError("All connection attempts failed")
    attempts.__cause__ = said
    core = Exception(text)  # httpcore.ConnectError
    core.__cause__ = attempts
    error = httpx.ConnectError(text)
    error.__cause__ = core
    return error


def _tls_failed(said: BaseException) -> httpx.ConnectError:
    """A failed TLS handshake as httpx raises it (httpcore wraps the ssl module's error)."""
    core = Exception(str(said))  # httpcore.ConnectError
    core.__cause__ = said
    error = httpx.ConnectError(str(said))
    error.__cause__ = core
    return error


def _unverified() -> ssl.SSLCertVerificationError:
    text = (
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer "
        "certificate (_ssl.c:1006)"
    )
    error = ssl.SSLCertVerificationError(1, text)
    error.verify_message = "unable to get local issuer certificate"  # what the ssl module sets
    return error


def _cut_off() -> httpx.ConnectError:
    """A firewall that drops Telegram's TLS handshake: anyio raises EndOfStream "from None" while
    handling the ssl module's SSLEOFError, and httpx's own text is empty."""

    class EndOfStream(Exception):  # anyio.EndOfStream
        pass

    eof = ssl.SSLEOFError(8, "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol")
    eof.reason = "UNEXPECTED_EOF_WHILE_READING"  # what the ssl module sets
    end = EndOfStream()
    end.__context__, end.__suppress_context__ = eof, True
    core = Exception("")  # httpcore.ConnectError
    core.__cause__ = end
    error = httpx.ConnectError("")
    error.__cause__ = core
    return error


def _failing_api(fail: Callable[[httpx.Request], BaseException]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        raise fail(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    ("fail", "expected"),
    [
        (lambda request: httpx.ConnectTimeout("", request=request), TIMED_OUT),
        (lambda request: httpx.ReadTimeout("The read operation timed out", request=request), TIMED_OUT),
        (
            lambda request: _connect_failed(ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")),
            REFUSED,
        ),
        (  # the IPv6 route is dead, IPv4 is tried next and refused: the refusal is what counts
            lambda request: _connect_failed(
                _AllFailed(
                    [
                        OSError(errno.ENETUNREACH, "Network is unreachable"),
                        ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"),
                    ]
                )
            ),
            REFUSED,
        ),
        (lambda request: httpx.ConnectError("[Errno 111] Connection refused", request=request), REFUSED),
        (
            lambda request: _connect_failed(
                socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution"),
                "[Errno -3] Temporary failure in name resolution",
            ),
            NO_DNS,
        ),
        (lambda request: httpx.ConnectError("[Errno -2] Name or service not known"), NO_DNS),
        (lambda request: _tls_failed(_unverified()), UNVERIFIED),
        (lambda request: httpx.ConnectError(str(_unverified())), UNVERIFIED),
        (lambda request: _cut_off(), TLS_CUT),
        (
            lambda request: _connect_failed(
                _AllFailed([OSError(errno.ENETUNREACH, "Network is unreachable")] * 2)
            ),
            NO_ROUTE,
        ),
    ],
)
async def test_a_request_that_gets_no_answer_says_what_to_check(
    fail: Callable[[httpx.Request], BaseException], expected: str
) -> None:
    """T11: the test answered only "timeout". Each way a request to Telegram can fail on the way
    names api.telegram.org, the likely cause and what to check, in a test and in a delivery."""
    async with _failing_api(fail) as client:
        tested = await telegram.send_test(two_bots(), client=client)
        sent = await telegram.send_message(two_bots(), "x", client=client)
    assert [r["error"] for r in tested["results"]] == [expected, expected]
    assert [r["error"] for r in sent["results"]] == [expected, expected]
    assert tested["error"] == f"2 of 2 bots failed — Danindu: {expected}; Team: {expected}"
    for text in (str(tested), str(sent)):
        assert "SECRET-BOT-TOKEN" not in text and "second-bot-token" not in text


async def test_a_request_through_a_proxy_names_the_proxy_never_its_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After "set HTTPS_PROXY" the next failure must not say "set HTTPS_PROXY" again: it names the
    proxy the requests went through (never the user and password in its URL)."""
    monkeypatch.setenv("HTTPS_PROXY", "http://proxyuser:proxysecret@proxy.example:3128")
    failures: list[tuple[Callable[[httpx.Request], BaseException], str]] = [
        (
            lambda request: httpx.ConnectTimeout("", request=request),
            "api.telegram.org did not answer within 10 s through the proxy proxy.example:3128: check "
            "HTTPS_PROXY in config/sentinel.env, and that the proxy lets this server reach Telegram",
        ),
        (
            lambda request: _connect_failed(ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")),
            "the proxy proxy.example:3128 refused the connection: check its address in HTTPS_PROXY "
            "(config/sentinel.env)",
        ),
        (
            lambda request: _connect_failed(
                socket.gaierror(socket.EAI_NONAME, "Name or service not known"),
                "[Errno -2] Name or service not known",
            ),
            "this server cannot look up the proxy proxy.example:3128 (DNS): check its address in "
            "HTTPS_PROXY (config/sentinel.env)",
        ),
        (
            lambda request: httpx.ProxyError("407 Proxy Authentication Required", request=request),
            "the proxy proxy.example:3128 did not connect to api.telegram.org (407 Proxy Authentication "
            "Required): check HTTPS_PROXY in config/sentinel.env (the address, and a login if the proxy "
            "needs one)",
        ),
    ]
    for fail, expected in failures:
        async with _failing_api(fail) as client:
            result = await telegram.send_test(tg_cfg(), client=client)
        assert result["error"] == expected
        assert "proxyuser" not in str(result) and "proxysecret" not in str(result)
    # a proxy that NO_PROXY keeps Telegram away from is not the way the requests went
    monkeypatch.setenv("NO_PROXY", "localhost,.telegram.org")
    async with _failing_api(lambda request: httpx.ConnectTimeout("", request=request)) as client:
        assert (await telegram.send_test(tg_cfg(), client=client))["error"] == TIMED_OUT


# ------------------------------------------------------------------------------ webhook


def hook_cfg(**dspace: Any) -> Config:
    return merge_patch(
        Config(),
        {
            "dspace": dspace,
            "notifiers": {"webhook": {"enabled": True, "url": "https://hooks.example/services/T0/B0/X"}},
        },
    )


async def test_webhook_always_verifies_tls_and_never_follows_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``dspace.verify_tls: false`` is for DSpace's own self-signed URLs, never for the webhook."""
    seen: dict[str, Any] = {}
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(204)

    def fake_make_client(**kwargs: Any) -> httpx.AsyncClient:
        seen.update(kwargs)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(webhook, "make_client", fake_make_client)
    result = await webhook.send(hook_cfg(verify_tls=False), {"kind": "test"})
    assert result == {"ok": True, "status_code": 204, "error": None}
    assert seen["verify"] is True and seen["follow_redirects"] is False
    assert len(calls) == 1


async def test_webhook_redirect_is_reported_not_followed() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "hooks.example":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
        return httpx.Response(200)

    # even a shared client that follows redirects must not follow the webhook's
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        result = await webhook.send(hook_cfg(), {"kind": "test"}, client=client)
    assert result["ok"] is False and result["status_code"] == 302
    assert result["error"] == "HTTP 302 from webhook (redirects are not followed)"
    assert calls == ["https://hooks.example/services/T0/B0/X"]


# ------------------------------------------------------------------------------ dispatch


def alert_event(severity: str = "warning", kind: str = "alert", **extra: Any) -> dict[str, Any]:
    return {
        "kind": kind,
        "type": "opened",
        "severity": severity,
        "html": f"{severity} <b>x</b>",
        "text": f"{severity} x",
        "summary": f"{severity.upper()} x",
        **extra,
    }


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[str]]]:
    """Every Telegram delivery as ``(html, [chat ids])``; chat ``-100222`` refuses."""
    out: list[tuple[str, list[str]]] = []

    async def fake_send_message(
        cfg: Config, html: str, *, chats: Any = None, **kwargs: Any
    ) -> dict[str, Any]:
        targets = list(chats if chats is not None else cfg.notifiers.telegram.destinations())
        out.append((html, [c.chat_id for c in targets]))
        results = [
            {"id": c.id, "label": c.label, "chat_id": c.chat_id, "ok": c.chat_id != "-100222", "error": None}
            for c in targets
        ]
        for r in results:
            if not r["ok"]:
                r["error"] = "chat not found — add the bot to the group first"
        ok = all(r["ok"] for r in results)
        return {"ok": ok, "message_id": 5, "error": telegram.summarize(results, "bots"), "results": results}

    monkeypatch.setattr(telegram, "send_message", fake_send_message)
    return out


async def test_critical_only_destinations_get_only_critical_alerts(sent: list[tuple[str, list[str]]]) -> None:
    cfg = two_bots()
    warning, mail, hook = await notifiers.dispatch(cfg, alert_event("warning"), now=0)
    assert [(r.channel, r.skipped) for r in (mail, hook)] == [("email", "disabled"), ("webhook", "disabled")]
    assert warning.ok is True and sent[-1][1] == ["111111"]
    assert warning.detail["destinations"] == [
        {
            "destination": "111111",
            "label": "Danindu",
            "ok": True,
            "error": None,
            "id": cfg.notifiers.telegram.chats[0].id,
        }
    ]
    critical = (await notifiers.dispatch(cfg, alert_event("critical"), now=0))[0]
    assert sent[-1][1] == ["111111", "-100222"] and critical.ok is False  # the group refused
    assert critical.error.startswith("1 of 2 bots failed — Team: chat not found")
    assert [d["ok"] for d in critical.detail["destinations"]] == [True, False]
    # digests go to everyone whatever their severity; incidents follow the rule
    await notifiers.dispatch(cfg, alert_event("warning", kind="digest"), now=0)
    assert sent[-1][1] == ["111111", "-100222"]
    await notifiers.dispatch(cfg, alert_event("warning", kind="incident"), now=0)
    assert sent[-1][1] == ["111111"]
    # nobody takes warnings: skipped, nothing sent
    only_critical = tg_cfg(chats=[{"bot_token": TOKEN, "chat_id": "42", "min_severity": "critical"}])
    before = len(sent)
    skipped = (await notifiers.dispatch(only_critical, alert_event("warning"), now=0))[0]
    assert (skipped.ok, skipped.skipped) == (False, "min severity") and len(sent) == before


async def test_quiet_hours_are_per_channel(sent: list[tuple[str, list[str]]]) -> None:
    night = _dt.datetime(2024, 3, 5, 2, 30).timestamp()
    cfg = two_bots(quiet_hours="23:00-07:00")
    held = (await notifiers.dispatch(cfg, alert_event("warning"), now=night))[0]
    assert held.skipped == "quiet hours" and sent == []
    critical = (await notifiers.dispatch(cfg, alert_event("critical"), now=night))[0]
    assert critical.skipped is None and len(sent) == 1
    assert notifiers.quiet_hours_for(cfg, "telegram") == "23:00-07:00"
    assert notifiers.quiet_hours_for(cfg, "webhook") == ""
    assert not notifiers.suppressed_by_quiet_hours(cfg, alert_event("warning"), night, channel="webhook")


async def test_an_event_can_name_its_channels(sent: list[tuple[str, list[str]]]) -> None:
    cfg = merge_patch(two_bots(), {"notifiers": {"webhook": {"enabled": True}}})
    results = await notifiers.dispatch(cfg, alert_event("critical", channels=["webhook"]), now=0)
    assert [(r.channel, r.skipped) for r in results] == [("webhook", "not configured")] and sent == []


async def test_skip_reasons_name_what_is_missing() -> None:
    cfg = merge_patch(Config(), {"notifiers": {"telegram": {"enabled": True, "chats": [{"chat_id": "42"}]}}})
    results = await notifiers.dispatch(cfg, alert_event("critical"), now=0)
    assert [(r.channel, r.skipped) for r in results] == [
        ("telegram", "no chats"),
        ("email", "disabled"),
        ("webhook", "disabled"),
    ]


async def test_notify_rows_carry_the_destinations(
    sent: list[tuple[str, list[str]]], mem_store: Store
) -> None:
    await notifiers.notify(mem_store, two_bots(), alert_event("critical", ts=100.0), ts=100.0)
    (row,) = mem_store.events(kind="notify.error")
    assert row.message.startswith("telegram failed: 1 of 2 bots failed — Team:")
    assert row.data["channel"] == "telegram"
    assert [(d["destination"], d["ok"]) for d in row.data["destinations"]] == [
        ("111111", True),
        ("-100222", False),
    ]
    await notifiers.notify(mem_store, two_bots(), alert_event("warning", ts=200.0), ts=200.0)
    (ok_row,) = mem_store.events(kind="notify")
    assert ok_row.message == "telegram sent: WARNING x" and ok_row.data["message_id"] == 5
    assert [d["destination"] for d in ok_row.data["destinations"]] == ["111111"]


async def test_a_slow_channel_times_out_with_room_for_every_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    async def never(cfg: Config, html: str, **kwargs: Any) -> dict[str, Any]:
        await asyncio.sleep(3600)
        return {}

    monkeypatch.setattr(telegram, "send_message", never)
    monkeypatch.setattr(telegram, "TIMEOUT_S", 0.01)
    notifier = notifiers.TelegramNotifier()
    assert notifier.timeout(3) == pytest.approx(5.03)
    monkeypatch.setattr(notifiers.TelegramNotifier, "timeout", lambda self, count: 0.05 * count)
    result = (await notifiers.dispatch(two_bots(), alert_event("critical"), now=0))[0]
    assert result == NotifyResult("telegram", False, error="timed out after 0 s")


# --------------------------------------------------------- channels are independent in time (R1#2)


class _Clock:
    """Seconds since the test started, and a store that notes when each row was written."""

    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.rows: list[tuple[float, str, str]] = []

    def now(self) -> float:
        return time.monotonic() - self.t0

    def add_event(self, kind: str, component: Any, message: str, data: Any = None, ts: Any = None) -> None:
        self.rows.append((self.now(), kind, message))


class _SilentSMTP(socketserver.ThreadingTCPServer):
    """Accepts the connection and never says a word (a firewall that swallows port 587)."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _SilentHandler)
        self.connections = 0


class _SilentHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.server.connections += 1  # type: ignore[attr-defined]
        with contextlib.suppress(OSError):
            while self.rfile.read(1):
                pass


class _SlowSMTP(socketserver.ThreadingTCPServer):
    """Answers every command, each after ``delay`` seconds; records whether a message got through."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, delay: float) -> None:
        super().__init__(("127.0.0.1", 0), _SlowHandler)
        self.delay = delay
        self.delivered = 0


class _SlowHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server: _SlowSMTP = self.server  # type: ignore[assignment]
        with contextlib.suppress(OSError):
            time.sleep(server.delay)
            self.wfile.write(b"220 slow.smtp ESMTP\r\n")
            in_data = False
            while line := self.rfile.readline():
                if in_data:
                    if line == b".\r\n":
                        in_data = False
                        server.delivered += 1
                        time.sleep(server.delay)
                        self.wfile.write(b"250 queued\r\n")
                    continue
                time.sleep(server.delay)
                verb = line.split(b" ", 1)[0].strip().upper()
                if verb == b"EHLO":
                    self.wfile.write(b"250-slow.smtp\r\n250 8BITMIME\r\n")
                elif verb == b"DATA":
                    in_data = True
                    self.wfile.write(b"354 go ahead\r\n")
                elif verb == b"QUIT":
                    self.wfile.write(b"221 bye\r\n")
                    return
                else:
                    self.wfile.write(b"250 ok\r\n")


@contextlib.contextmanager
def _serving(server: socketserver.ThreadingTCPServer) -> Iterator[int]:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


def _both_channels(smtp_port: int) -> Config:
    return merge_patch(
        Config(),
        {
            "server": {"hostname": "repo-1"},
            "notifiers": {
                "telegram": {
                    "enabled": True,
                    "chats": [{"label": "Alex", "bot_token": TOKEN, "chat_id": "111"}],
                },
                "email": {
                    "enabled": True,
                    "smtp_host": "127.0.0.1",
                    "smtp_port": smtp_port,
                    "security": "none",
                    "from_address": "sentinel@example.org",
                    "recipients": [{"address": "ops@example.org"}],
                },
            },
        },
    )


def _outage(n: int) -> dict[str, Any]:
    return alert_event("critical", summary=f"CRITICAL alert {n}", html=f"alert {n}", alert={"id": n})


def _bot_that_records(clock: _Clock, arrivals: list[tuple[float, str]], delay: float = 0.0) -> Any:
    async def bot(request: httpx.Request) -> httpx.Response:
        if delay:
            await asyncio.sleep(delay)
        arrivals.append((clock.now(), json.loads(request.content)["text"]))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(arrivals)}})

    return lambda timeout=10.0, **_: httpx.AsyncClient(transport=httpx.MockTransport(bot), timeout=timeout)


async def test_a_mail_server_that_never_answers_holds_up_only_email(monkeypatch: pytest.MonkeyPatch) -> None:
    """R1#2 (a): three alerts queued at once while the mail server swallows every connection:
    Telegram gets all three at once, in order, and says so in the events log; email fails for
    each in its own time, in order, behind the one before."""
    from dspace_sentinel.alerts.notifiers import email as mail

    monkeypatch.setattr(mail, "TIMEOUT_S", 0.5)
    clock = _Clock()
    arrivals: list[tuple[float, str]] = []
    monkeypatch.setattr(telegram, "make_client", _bot_that_records(clock, arrivals))
    with _serving(_SilentSMTP()) as port:
        cfg = _both_channels(port)
        box = Outbox()
        for n in (1, 2, 3):
            box.submit(notifiers.notify(clock, cfg, _outage(n), ts=100.0 + n))
        assert await box.flush(timeout=10) is True
    assert [text for _, text in arrivals] == ["alert 1", "alert 2", "alert 3"]
    assert all(at < 0.4 for at, _ in arrivals), arrivals  # before the first email even timed out
    sent = [(at, message) for at, kind, message in clock.rows if kind == "notify"]
    failed = [(at, message) for at, kind, message in clock.rows if kind == "notify.error"]
    assert [m for _, m in sent] == [f"telegram sent: CRITICAL alert {n}" for n in (1, 2, 3)]
    assert all(at < 0.4 for at, _ in sent), sent  # each written as soon as Telegram was done
    assert [m.split(":", 1)[0] for _, m in failed] == ["email failed"] * 3
    # one after the other, each after its own 0.5 s wait: the mail server's queue, nobody else's
    times = [at for at, _ in failed]
    assert times[0] >= 0.45 and times[1] - times[0] >= 0.4 and times[2] - times[1] >= 0.4, times


async def test_an_unreachable_telegram_holds_up_only_telegram(monkeypatch: pytest.MonkeyPatch) -> None:
    """R1#2 (c): Telegram answers each call only after a long wait: the emails go out at once."""
    clock = _Clock()
    arrivals: list[tuple[float, str]] = []
    monkeypatch.setattr(telegram, "make_client", _bot_that_records(clock, arrivals, delay=0.4))
    sent_at: list[float] = []

    class Relay:
        def __init__(self, host: str, port: int, **kwargs: Any) -> None:
            pass

        def ehlo(self) -> None:
            pass

        def send_message(self, msg: Any, from_addr: str, to_addrs: list[str]) -> dict[str, Any]:
            sent_at.append(clock.now())
            return {}

        def quit(self) -> None:
            pass

    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", Relay)
    box = Outbox()
    cfg = _both_channels(25)
    for n in (1, 2, 3):
        box.submit(notifiers.notify(clock, cfg, _outage(n), ts=100.0 + n))
    assert await box.flush(timeout=10) is True
    assert len(sent_at) == 3 and all(at < 0.3 for at in sent_at), sent_at
    assert [text for _, text in arrivals] == ["alert 1", "alert 2", "alert 3"]
    assert arrivals[-1][0] >= 1.1  # Telegram's own queue: one call after the other


async def test_a_delivery_cut_short_never_hands_the_message_over_later(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1#2: once the channel's cap gave up on the mail server, the session is cut: the message is
    not handed over seconds after "email failed: timed out" was written."""
    monkeypatch.setattr(notifiers.EmailNotifier, "timeout", lambda self, count: 0.5)
    slow = _SlowSMTP(delay=0.3)
    with _serving(slow) as port:
        cfg = merge_patch(_both_channels(port), {"notifiers": {"telegram": {"enabled": False}}})
        results = await notifiers.dispatch(cfg, _outage(1), now=0)
        email_result = next(r for r in results if r.channel == "email")
        assert email_result.ok is False and email_result.error == "timed out after 0 s"
        await asyncio.sleep(3.0)  # the whole slow session takes about 2.4 s
        assert slow.delivered == 0


async def test_a_delivery_given_up_during_the_tls_handshake_is_not_handed_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While smtplib is inside starttls() its ``sock`` is still the old socket object, which ssl
    has detached, so the cut cannot shut it down: the session looks at the cut again when the
    handshake is over instead of logging in and handing the message over."""
    import smtplib
    import socket

    class Handshaking(smtplib.SMTP):
        def starttls(self, **kwargs: Any) -> Any:  # what ssl.wrap_socket does, minus the TLS
            taken_over = socket.socket(fileno=self.sock.detach())  # the old object is left without a socket
            time.sleep(0.8)  # the handshake: the channel's cap (0.3 s) fires meanwhile
            taken_over.settimeout(self.timeout)
            self.sock, self.file = taken_over, None
            return 220, b"ready"

    monkeypatch.setattr(smtplib, "SMTP", Handshaking)
    monkeypatch.setattr(notifiers.EmailNotifier, "timeout", lambda self, count: 0.3)
    server = _SlowSMTP(delay=0.0)
    with _serving(server) as port:
        cfg = merge_patch(
            _both_channels(port),
            {"notifiers": {"telegram": {"enabled": False}, "email": {"security": "starttls"}}},
        )
        results = await notifiers.dispatch(cfg, _outage(1), now=0)
        email_result = next(r for r in results if r.channel == "email")
        assert email_result.ok is False and email_result.error == "timed out after 0 s"
        await asyncio.sleep(1.2)  # the abandoned thread leaves starttls() after 0.8 s
        assert server.delivered == 0


async def test_a_turn_given_up_before_it_began_keeps_the_lane_in_order() -> None:
    """A delivery cancelled while it waited for its turn must not let the next one overtake the
    delivery still under way before it."""
    lane = notifiers._Lane()
    first, second, third = lane.claim(), lane.claim(), lane.claim()
    assert first[0] is None and second[0] is first[1] and third[0] is second[1]
    notifiers._end_turn(second[1], second[0])  # cancelled while waiting for the first
    await asyncio.sleep(0)
    assert not second[1].done()  # the third still waits: the first is sending
    notifiers._end_turn(first[1], None)
    await asyncio.sleep(0)
    assert second[1].done()
    assert lane.claim()[0] is third[1]


# ------------------------------------------------------------------------------ outbox


async def test_outbox_delivers_in_order_in_the_background() -> None:
    done: list[str] = []

    async def job(name: str, delay: float = 0.0, fail: bool = False) -> None:
        await asyncio.sleep(delay)
        if fail:
            raise RuntimeError(name)
        done.append(name)

    box = Outbox()
    box.submit(job("opened", 0.05))
    box.submit(job("broken", fail=True))  # a failing job never stops the ones behind it
    box.submit(job("resolved"))
    assert done == [] and len(box) == 3  # submit() returns at once
    assert await box.flush(timeout=2) is True
    assert done == ["opened", "resolved"]
    box.submit(job("later"))  # a new worker picks up after the queue ran dry
    assert await box.flush() is True and done[-1] == "later"


async def test_outbox_bounds_its_queue_and_can_be_closed() -> None:
    started: list[int] = []
    gate = asyncio.Event()

    async def job(n: int) -> None:
        started.append(n)
        await gate.wait()

    box = Outbox(maxsize=3)
    box.submit(job(0))
    for _ in range(5):  # the worker starts job 0, which waits on the gate (it never dispatches)
        await asyncio.sleep(0)
    for n in range(1, 5):
        box.submit(job(n))  # 1, 2, 3 wait; 4 pushes the oldest waiting one (1) out
    assert box.dropped == 1 and len(box) == 3 and started == [0]
    assert await box.flush(timeout=0.05) is False
    box.close()
    await asyncio.sleep(0)
    assert len(box) == 0 and started == [0]
    assert await box.flush() is True


async def test_a_closed_outbox_forgets_its_lanes(monkeypatch: pytest.MonkeyPatch) -> None:
    """close() can cancel a job in the very loop turn in which it took its lane turns, before
    its deliveries ran one step: nothing of that job is left to end those turns. The same outbox
    used again (a scheduler started again in the same process) must still deliver."""
    from dspace_sentinel.alerts.notifiers import email as mail

    state = {"mail_answers": False}
    emails: list[str] = []

    async def send_event(cfg: Config, event: dict[str, Any], recipients: Any = None) -> dict[str, Any]:
        if not state["mail_answers"]:
            await asyncio.sleep(3600)
        emails.append(str(event["summary"]))
        return {"ok": True, "error": None, "results": []}

    monkeypatch.setattr(mail, "send_event", send_event)
    clock = _Clock()
    arrivals: list[tuple[float, str]] = []
    monkeypatch.setattr(telegram, "make_client", _bot_that_records(clock, arrivals))
    cfg = _both_channels(25)
    stuck: list[int] = []
    for loop_turns in range(12):  # close() at every moment of the three jobs' first steps
        state["mail_answers"] = False
        emails.clear()
        arrivals.clear()
        box = Outbox()
        for n in (1, 2, 3):
            box.submit(notifiers.notify(clock, cfg, _outage(n), ts=100.0 + n))
        for _ in range(loop_turns):
            await asyncio.sleep(0)
        box.close()
        await asyncio.sleep(0.01)
        state["mail_answers"] = True
        box.submit(notifiers.notify(clock, cfg, _outage(9), ts=200.0))
        if not await box.flush(timeout=1):
            stuck.append(loop_turns)
            box.close()
            continue
        assert emails == ["CRITICAL alert 9"], (loop_turns, emails)
        assert arrivals[-1][1] == "alert 9", (loop_turns, arrivals)
    assert stuck == []


def test_the_webhook_body_leaves_out_internal_routing() -> None:
    body = webhook.payload_for({"kind": "digest", "channels": ["telegram", "webhook"], "text": "x"})
    assert "channels" not in body and body["kind"] == "digest" and body["source"] == "dspace-sentinel"
