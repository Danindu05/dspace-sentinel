"""Telegram Bot API: ``sendMessage``, ``sendDocument`` and ``getMe`` over httpx, one bot per destination.

Every destination in ``notifiers.telegram.chats`` is its own bot (``bot_token``) talking to one
chat (``chat_id``). :func:`send_message` and :func:`send_document` go through the destinations
one after the other (10 s timeout each) and report on each; :func:`send_test` first calls
``getMe`` with every destination's token. Every function returns a dict and never raises; every
bot token of the configuration is redacted from every error string (httpx puts the request URL,
which contains the token, into its messages). Pass ``client`` to reuse a client (tests use
``httpx.MockTransport``).

Well-known Bot API refusals are turned into plain advice ("chat not found — send the bot a
message first"); anything else keeps Telegram's own description. A request that never got an
answer (a time-out, a refused connection, a failed DNS lookup, a TLS error) says what happened
and what to check (:func:`transport_error`), naming the proxy from ``HTTPS_PROXY`` when the
requests go through one.

In demo mode (``cfg.demo``) nothing is ever sent: :func:`call` answers
``{ok: False, skipped: "demo", error: "demo mode: nothing is sent to Telegram"}`` before any
request is built, and :func:`send_message`, :func:`send_document` and :func:`send_test` answer
the same without looking at a destination.
"""

from __future__ import annotations

import logging
import re
import socket
import ssl
import urllib.request
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit

import httpx

from dspace_sentinel.alerts.messages import test_html
from dspace_sentinel.alerts.notifiers import summarize
from dspace_sentinel.util.httpc import describe_error, make_client

if TYPE_CHECKING:  # pragma: no cover
    from dspace_sentinel.config import Config, TelegramChat

log = logging.getLogger(__name__)

API_HOST = "api.telegram.org"
API_BASE = f"https://{API_HOST}"
TIMEOUT_S = 10.0
MAX_TEXT = 4096
"""Telegram's hard limit for one message; longer texts are cut with an ellipsis."""

MAX_CAPTION = 1024
"""Telegram's limit for a document caption."""

DEMO_ERROR = "demo mode: nothing is sent to Telegram"
"""The ``error`` every Bot API call answers with while ``cfg.demo`` is set."""

NO_BOTS = "Telegram has no bot yet: add a bot token and a chat id"
"""Nothing to send to: no destination has both a bot token and a chat id."""

NO_TOKEN = "no bot token: paste the token @BotFather gave you"
NO_CHAT = "no chat id: send the bot a message, then paste your chat id"


def api_url(token: str, method: str) -> str:
    """``https://api.telegram.org/bot<token>/<method>``."""
    return f"{API_BASE}/bot{token}/{method}"


def redact(text: str, tokens: str | Iterable[str]) -> str:
    """Replace every bot token in ``tokens`` wherever it appears in ``text``.

    Case-insensitive (an error text may have been case-folded on its way here) and also in
    its URL-encoded form (``123456%3AABC…``), so no copy of a token survives.
    """
    if not text:
        return text
    wanted = [tokens] if isinstance(tokens, str) else list(tokens)
    variants = {v for token in wanted if token for v in (token, quote(token, safe=""))}
    for variant in sorted(variants, key=len, reverse=True):
        text = re.sub(re.escape(variant), "•••", text, flags=re.IGNORECASE)
    return text


def all_tokens(cfg: Config) -> list[str]:
    """Every bot token in the configuration (for :func:`redact`)."""
    return [c.bot_token for c in cfg.notifiers.telegram.chats if c.bot_token]


def destinations(cfg: Config) -> list[TelegramChat]:
    """The destinations that can be sent to (bot token and chat id both set), in order."""
    return cfg.notifiers.telegram.destinations()


def _truncate(text: str, limit: int = MAX_TEXT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def friendly_error(description: str, code: Any = None, chat_id: str = "", parameters: Any = None) -> str:
    """A Bot API refusal as advice the person who set the destination up can act on."""
    low = description.lower()
    if "chat not found" in low:
        if chat_id.startswith("@"):
            return "chat not found — add the bot to the channel as an administrator first"
        if chat_id.startswith("-"):
            return "chat not found — add the bot to the group first"
        return "chat not found — send the bot a message first"
    if "can't initiate conversation" in low:
        return "the bot has not been started in this chat — open it in Telegram and press Start"
    if "bot was blocked by the user" in low:
        return "the bot was blocked in this chat — unblock it in Telegram"
    if "bot was kicked" in low:
        return "the bot was removed from this group — add it again"
    if "bot is not a member" in low:
        return "the bot is not a member of this chat — add it first"
    if "user is deactivated" in low:
        return "this Telegram account is deactivated"
    if "not enough rights" in low or "have no rights to send" in low:
        return "the bot may not post in this chat — allow it to send messages"
    migrated = parameters.get("migrate_to_chat_id") if isinstance(parameters, dict) else None
    if migrated:
        return f"this group became a supergroup: its chat id is now {migrated}"
    if code in (401, 404):
        reason = "401 Unauthorized" if code == 401 else "404 Not Found"
        return f"Telegram refused the bot token ({reason}) — copy it again from @BotFather"
    return f"{description} (error {code})" if code else description


PROXY_ADVICE = "set HTTPS_PROXY in config/sentinel.env if the network has a proxy"
"""What to try when a request that went to Telegram directly did not get through."""


def _proxy() -> str | None:
    """``host:port`` of the proxy the requests to Telegram go through (``HTTPS_PROXY``, else
    ``ALL_PROXY``, read from the environment as httpx reads them), never the login it may carry;
    ``None`` when they go directly (no proxy, or ``NO_PROXY`` names Telegram)."""
    proxies = urllib.request.getproxies()
    url = proxies.get("https") or proxies.get("all")
    if not url:
        return None
    for entry in str(proxies.get("no") or "").split(","):
        name = entry.strip().lower().lstrip("*.")
        if entry.strip() == "*" or (name and (API_HOST == name or API_HOST.endswith(f".{name}"))):
            return None
    parts = urlsplit(url if "://" in url else f"http://{url}")
    try:
        port = parts.port
    except ValueError:  # a port that is not a number
        port = None
    host = parts.hostname or "from HTTPS_PROXY"
    return f"{host}:{port}" if port else host


def _chain(exc: BaseException) -> list[BaseException]:
    """``exc`` and every exception behind it (``__cause__``, ``__context__``, the members of an
    exception group), each once: httpx wraps what the socket said two or three times."""
    found: list[BaseException] = []
    todo: list[BaseException] = [exc]
    while todo and len(found) < 32:
        current = todo.pop(0)
        if any(current is seen for seen in found):
            continue
        found.append(current)
        nested = [current.__cause__, current.__context__, *(getattr(current, "exceptions", None) or ())]
        todo.extend(e for e in nested if isinstance(e, BaseException))
    return found


_SSL_SAID = re.compile(r"\[SSL\b|_ssl\.c:", re.IGNORECASE)
"""How Python's ssl module marks its texts ("[SSL: WRONG_VERSION_NUMBER] … (_ssl.c:1006)")."""


def _ssl_text(text: str) -> str:
    """``"wrong version number"`` for ``"[SSL: WRONG_VERSION_NUMBER] wrong version number (_ssl.c:1006)"``."""
    text = re.sub(r"^\[SSL: [A-Z0-9_]+\]\s*", "", text.strip())
    return re.sub(r"\s*\(_ssl\.c:\d+\)$", "", text).strip()


def _tls_reason(exc: ssl.SSLError) -> str:
    """The short reason of a TLS failure: OpenSSL's ``reason`` ("UNEXPECTED_EOF_WHILE_READING"
    becomes "unexpected eof while reading"), else its text without the ``[SSL: …]`` wrapping."""
    reason = getattr(exc, "reason", None)
    if reason:
        return str(reason).replace("_", " ").lower()
    return _ssl_text(str(exc.strerror or exc)) or "TLS error"


def transport_error(exc: BaseException) -> str:
    """A request that got no answer from Telegram, as what happened and what to check.

    A time-out, a refused connection, a failed DNS lookup and a TLS error name api.telegram.org
    (or the proxy the requests go through, see :func:`_proxy`) and the likely cause; anything
    else keeps :func:`~dspace_sentinel.util.httpc.describe_error`'s text. The caller redacts the
    bot tokens: httpx puts the request URL into some of its texts.
    """
    proxy = _proxy()
    check = "check its address in HTTPS_PROXY (config/sentinel.env)"
    if isinstance(exc, httpx.TimeoutException):
        if proxy:
            return (
                f"{API_HOST} did not answer within {TIMEOUT_S:.0f} s through the proxy {proxy}: check "
                "HTTPS_PROXY in config/sentinel.env, and that the proxy lets this server reach Telegram"
            )
        return (
            f"{API_HOST} did not answer within {TIMEOUT_S:.0f} s: this server may not be able to reach "
            "Telegram (a firewall, or a proxy it has to use: set HTTPS_PROXY in config/sentinel.env)"
        )
    if isinstance(exc, httpx.ProxyError):
        return (
            f"the proxy{' ' + proxy if proxy else ''} did not connect to {API_HOST} ({describe_error(exc)}): "
            "check HTTPS_PROXY in config/sentinel.env (the address, and a login if the proxy needs one)"
        )
    if not isinstance(exc, httpx.ConnectError):
        return describe_error(exc)
    chain = _chain(exc)
    raw = str(exc)
    low = raw.lower()
    unverified = next((e for e in chain if isinstance(e, ssl.SSLCertVerificationError)), None)
    if unverified is not None or "certificate verify failed" in low:
        said = re.search(r"certificate verify failed: ([^()]+)", raw)
        reason = getattr(unverified, "verify_message", None) or (said.group(1).strip() if said else "")
        return (
            f"the TLS certificate of {API_HOST} could not be verified ({reason or 'not trusted'}): check "
            "this server's date and time, and whether a proxy or firewall on this network intercepts HTTPS"
        )
    tls = next((e for e in chain if isinstance(e, ssl.SSLError)), None)
    cut = any(isinstance(e, ConnectionResetError) or type(e).__name__ == "EndOfStream" for e in chain)
    said_tls = bool(_SSL_SAID.search(raw))
    if tls is not None or cut or said_tls:
        if tls is not None:
            reason = _tls_reason(tls)
        else:
            reason = _ssl_text(raw) if said_tls else "the connection was cut off"
        advice = "check HTTPS_PROXY in config/sentinel.env" if proxy else PROXY_ADVICE
        return (
            f"the secure connection to {API_HOST} failed ({reason or 'TLS error'}): a firewall or proxy "
            f"on this network may block or intercept Telegram ({advice})"
        )
    text = describe_error(exc)
    if any(isinstance(e, socket.gaierror) for e in chain) or text == "DNS lookup failed":
        if proxy:
            return f"this server cannot look up the proxy {proxy} (DNS): {check}"
        return (
            f"this server cannot look up {API_HOST} (DNS): check its name servers (/etc/resolv.conf), "
            f"or {PROXY_ADVICE}"
        )
    if any(isinstance(e, ConnectionRefusedError) for e in chain) or text == "connection refused":
        if proxy:
            return f"the proxy {proxy} refused the connection: {check}"
        return (
            f"{API_HOST} refused the connection: a firewall on this server or its network probably "
            f"blocks Telegram (allow outgoing HTTPS, port 443, or {PROXY_ADVICE})"
        )
    said = next((e.strerror for e in chain if isinstance(e, OSError) and e.strerror), None)
    reason = said[:1].lower() + said[1:] if said else text
    if proxy:
        return f"cannot connect to the proxy {proxy} ({reason}): {check}"
    return (
        f"cannot connect to {API_HOST} ({reason}): check this server's network and firewall, "
        f"or {PROXY_ADVICE}"
    )


async def call(
    cfg: Config,
    method: str,
    *,
    token: str | None = None,
    data: dict[str, Any] | None = None,
    files: dict[str, Any] | None = None,
    client: httpx.AsyncClient | None = None,
    require_chat: bool = True,
) -> dict[str, Any]:
    """POST one Bot API method with ``token`` (default: the first complete destination's).

    Returns ``{ok, result, error}``; never raises. ``data`` is sent as JSON unless ``files`` is
    given (multipart). ``require_chat`` makes a missing ``data["chat_id"]`` an error (``getMe``
    does not need one).
    """
    if getattr(cfg, "demo", False):
        return {"ok": False, "result": None, "error": DEMO_ERROR, "skipped": "demo"}
    if token is None:
        first = next(iter(destinations(cfg)), None)
        token = first.bot_token if first is not None else ""
    token = (token or "").strip()
    secrets = [token, *all_tokens(cfg)]
    if not token:
        return {"ok": False, "result": None, "error": "Telegram bot token is not configured"}
    chat_id = str((data or {}).get("chat_id") or "")
    if require_chat and not chat_id:
        return {"ok": False, "result": None, "error": "Telegram chat id is not configured"}
    own = client is None
    http = client or make_client(timeout=TIMEOUT_S)
    try:
        url = api_url(token, method)
        if files:
            response = await http.post(url, data=data or {}, files=files, timeout=TIMEOUT_S)
        else:
            response = await http.post(url, json=data or {}, timeout=TIMEOUT_S)
    except Exception as exc:
        return {"ok": False, "result": None, "error": redact(transport_error(exc), secrets)}
    finally:
        if own:
            try:
                await http.aclose()
            except Exception:  # pragma: no cover
                pass
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        return {"ok": False, "result": None, "error": f"HTTP {response.status_code} from Telegram"}
    if not payload.get("ok"):
        description = str(payload.get("description") or f"HTTP {response.status_code}")
        error = friendly_error(description, payload.get("error_code"), chat_id, payload.get("parameters"))
        return {"ok": False, "result": payload.get("result"), "error": redact(error, secrets)}
    return {"ok": True, "result": payload.get("result"), "error": None}


def _entry(chat: TelegramChat) -> dict[str, Any]:
    return {"id": chat.id, "label": chat.label, "chat_id": chat.chat_id, "ok": False, "error": None}


def _outcome(results: list[dict[str, Any]]) -> dict[str, Any]:
    delivered = [r for r in results if r["ok"]]
    return {
        "ok": bool(results) and len(delivered) == len(results),
        "message_id": delivered[0].get("message_id") if delivered else None,
        "error": summarize(results, "bots") if results else NO_BOTS,
        "results": results,
    }


async def send_message(
    cfg: Config,
    html: str,
    *,
    chats: Sequence[TelegramChat] | None = None,
    client: httpx.AsyncClient | None = None,
    disable_preview: bool = True,
) -> dict[str, Any]:
    """``sendMessage`` (``parse_mode=HTML``) through each destination of ``chats`` in turn
    (default: every complete destination), each with its own bot.

    Returns ``{ok, message_id, error, results: [{id, label, chat_id, ok, error, message_id}]}``;
    ``ok`` only when every destination got it, ``message_id`` of the first that did.
    """
    if getattr(cfg, "demo", False):
        return {"ok": False, "message_id": None, "error": DEMO_ERROR, "skipped": "demo", "results": []}
    targets = list(destinations(cfg) if chats is None else chats)
    results: list[dict[str, Any]] = []
    own = client is None
    http = client or make_client(timeout=TIMEOUT_S)
    try:
        for chat in targets:
            entry = _entry(chat)
            result = await call(
                cfg,
                "sendMessage",
                token=chat.bot_token,
                data={
                    "chat_id": chat.chat_id,
                    "text": _truncate(html),
                    "parse_mode": "HTML",
                    "disable_web_page_preview": disable_preview,
                },
                client=http,
            )
            entry["ok"], entry["error"] = bool(result["ok"]), result.get("error")
            entry["message_id"] = (result.get("result") or {}).get("message_id") if result["ok"] else None
            results.append(entry)
    finally:
        if own:
            await http.aclose()
    return _outcome(results)


async def send_document(
    cfg: Config,
    path: str | Path,
    caption: str = "",
    *,
    chats: Sequence[TelegramChat] | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """``sendDocument`` (multipart) of a report file through each destination's bot (default:
    every complete destination). Same answer as :func:`send_message`.

    ``caption`` is plain text (no ``parse_mode``): a report title may hold ``<`` or ``&``, which
    Telegram's HTML mode refuses unless escaped ("can't parse entities")."""
    if getattr(cfg, "demo", False):
        return {"ok": False, "message_id": None, "error": DEMO_ERROR, "skipped": "demo", "results": []}
    file_path = Path(path)
    secrets = all_tokens(cfg)
    if not file_path.is_file():
        error = redact(f"file not found: {file_path.name}", secrets)
        return {"ok": False, "message_id": None, "error": error, "results": []}
    try:
        content = file_path.read_bytes()
    except OSError as exc:
        error = redact(f"cannot read {file_path.name}: {exc}", secrets)
        return {"ok": False, "message_id": None, "error": error, "results": []}
    targets = list(destinations(cfg) if chats is None else chats)
    results: list[dict[str, Any]] = []
    own = client is None
    http = client or make_client(timeout=TIMEOUT_S)
    try:
        for chat in targets:
            entry = _entry(chat)
            result = await call(
                cfg,
                "sendDocument",
                token=chat.bot_token,
                data={"chat_id": chat.chat_id, "caption": _truncate(caption, MAX_CAPTION)},
                files={"document": (file_path.name, content)},
                client=http,
            )
            entry["ok"], entry["error"] = bool(result["ok"]), result.get("error")
            entry["message_id"] = (result.get("result") or {}).get("message_id") if result["ok"] else None
            results.append(entry)
    finally:
        if own:
            await http.aclose()
    return _outcome(results)


async def get_me(
    cfg: Config, *, token: str | None = None, client: httpx.AsyncClient | None = None
) -> dict[str, Any]:
    """``getMe`` with ``token`` (default: the first complete destination's):
    ``{ok, bot_name, error}`` (``bot_name`` is ``@username`` or the first name)."""
    result = await call(cfg, "getMe", token=token, client=client, require_chat=False)
    if not result["ok"]:
        return {"ok": False, "bot_name": None, "error": result["error"]}
    info = result.get("result") or {}
    username = info.get("username")
    bot_name = f"@{username}" if username else str(info.get("first_name") or "bot")
    return {"ok": True, "bot_name": bot_name, "error": None}


async def send_test(
    cfg: Config, *, destination_id: str | None = None, client: httpx.AsyncClient | None = None
) -> dict[str, Any]:
    """``getMe`` with each destination's token, then the "👋 DSpace Sentinel is connected" message.

    ``destination_id`` tests only that destination (the caller checks it exists). Sent whether or
    not Telegram is switched on, to every destination whatever its ``min_severity``. Returns
    ``{ok, bot_name, error, results: [{id, label, chat_id, bot_name, ok, error}]}``; ``ok`` when
    every tested destination got the message, ``bot_name`` the first bot that answered.
    """
    if getattr(cfg, "demo", False):
        return {"ok": False, "bot_name": None, "error": DEMO_ERROR, "skipped": "demo", "results": []}
    chats = list(cfg.notifiers.telegram.chats)
    if destination_id is not None:
        chats = [c for c in chats if c.id == destination_id]
    if not chats:
        return {"ok": False, "bot_name": None, "error": NO_BOTS, "results": []}
    results: list[dict[str, Any]] = []
    own = client is None
    http = client or make_client(timeout=TIMEOUT_S)
    try:
        for chat in chats:
            entry = {**_entry(chat), "bot_name": None}
            if not chat.bot_token:
                entry["error"] = NO_TOKEN
            else:
                me = await get_me(cfg, token=chat.bot_token, client=http)
                entry["bot_name"], entry["error"] = me["bot_name"], me["error"]
                if me["ok"] and not chat.chat_id:
                    entry["error"] = NO_CHAT
                elif me["ok"]:
                    sent = await send_message(cfg, test_html(cfg.hostname), chats=[chat], client=http)
                    entry["ok"], entry["error"] = sent["ok"], sent["error"]
            results.append(entry)
    finally:
        if own:
            await http.aclose()
    ok = all(r["ok"] for r in results)
    bot_name = next((r["bot_name"] for r in results if r["bot_name"]), None)
    return {"ok": ok, "bot_name": bot_name, "error": summarize(results, "bots"), "results": results}


__all__ = [
    "API_BASE",
    "API_HOST",
    "DEMO_ERROR",
    "MAX_CAPTION",
    "MAX_TEXT",
    "NO_BOTS",
    "NO_CHAT",
    "NO_TOKEN",
    "PROXY_ADVICE",
    "TIMEOUT_S",
    "all_tokens",
    "api_url",
    "call",
    "destinations",
    "friendly_error",
    "get_me",
    "redact",
    "send_document",
    "send_message",
    "send_test",
    "summarize",
    "transport_error",
]
