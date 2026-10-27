"""Email over SMTP: stdlib ``smtplib`` + ``email.message.EmailMessage``, run in a daemon worker thread.

One SMTP session per event and ONE message addressed (``To:``) to every recipient that takes the
event, as ``multipart/alternative`` (text/plain + text/html, see
:mod:`dspace_sentinel.alerts.email_messages`), with ``Auto-Submitted: auto-generated`` and
``X-Auto-Response-Suppress: All`` so vacation responders stay quiet. A report travels as an
attachment of its own message.

``notifiers.email.security``: ``starttls`` (port 587, Gmail's default), ``ssl`` (implicit TLS,
port 465) or ``none`` (a relay that needs no password, or one on this machine: a password is
never sent over an unencrypted connection to another host). TLS certificates are verified with
the system CA store (``ssl.create_default_context()``); every socket operation times out after
:data:`TIMEOUT_S` seconds. SMTP failures come back as plain sentences, and the password never
appears in any error or log line.

Every function returns a dict and never raises. In demo mode (``cfg.demo``) nothing is sent and
no connection is opened: the answer is ``{ok: False, skipped: "demo", error: "demo mode: nothing
is sent by email"}``.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import logging
import re
import smtplib
import socket
import ssl
import threading
from collections.abc import Sequence
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dspace_sentinel.alerts import email_messages
from dspace_sentinel.alerts.email_messages import EmailContent
from dspace_sentinel.alerts.notifiers import summarize
from dspace_sentinel.config import (
    PASSWORD_NOT_SENDABLE,
    SENDER_NOT_AN_ADDRESS,
    USERNAME_NOT_SENDABLE,
    is_address,
)
from dspace_sentinel.util import fmt

if TYPE_CHECKING:  # pragma: no cover
    from dspace_sentinel.config import Config, EmailRecipient

log = logging.getLogger(__name__)

TIMEOUT_S = 20.0
"""Timeout of every SMTP socket operation (connect, TLS, login, data)."""

DEMO_ERROR = "demo mode: nothing is sent by email"
NOT_SET_UP = "email is not set up: add the Gmail address, app password and at least one recipient"
GMAIL_APP_PASSWORD = (
    "use an app password made in that same Google account (Google Account › Security › "
    "2-Step Verification › App passwords), not the normal password"
)
"""What to do when Gmail refuses the login; the sentence names the login and Gmail's own answer
first: "Gmail refused the login for <username> (535 5.7.8 Username and Password not accepted): …"."""
GMAIL_CLOSED_HINT = (
    "Gmail does this for a few minutes after a refused login: check the address and the app "
    "password, wait, then test again"
)
"""Why smtp.gmail.com may close the connection although the port and security are right."""
# PASSWORD_NOT_SENDABLE and USERNAME_NOT_SENDABLE come from the configuration, which refuses such
# a login when it is saved and names it when the settings file or the environment brings one.
MAX_ATTACHMENT_BYTES = 17 * 1024 * 1024
"""Largest report attached: 17 MB as Sentinel counts sizes (``util.fmt.bytes_``, 1024-based), so
17,825,792 bytes. base64 makes it a third larger plus a line break every 76 characters, about
24.4 million bytes on the wire with the message around it, which still fits Gmail's 25 MB."""

GMAIL_HOSTS = frozenset({"smtp.gmail.com", "smtp.googlemail.com"})

ATTACHMENT_TYPES: dict[str, tuple[str, str]] = {
    ".html": ("text", "html"),
    ".md": ("text", "markdown"),
    ".json": ("application", "json"),
}


class PlainPasswordRefused(Exception):
    """``security: none`` with a login to another host: the password would travel in clear."""


class SenderNotAnAddress(ValueError):
    """The From address (``from_address``, else the login) is not an email address."""


class _Connection:
    """The SMTP connection of one delivery, so a delivery that was given up on can be cut off.

    The session runs in a daemon thread that a cancelled coroutine cannot stop. Without this, a
    mail server that answers slowly could still take the message seconds after the delivery was
    recorded as "timed out"; cut off, the thread's next read or write fails and nothing more is
    sent (a message whose end the server already had may still arrive: SMTP cannot take it back).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._server: smtplib.SMTP | None = None
        self.cut = False

    def attach(self, server: smtplib.SMTP) -> None:
        """The worker thread is connected; raises when the delivery was given up on meanwhile.

        Called again after the STARTTLS handshake: while it runs, ``server.sock`` is still the
        old socket object, which ``ssl`` has detached, so a cut in that window shuts nothing."""
        with self._lock:
            self._server = server
            cut = self.cut
        if cut:
            _shut(server)
            raise ConnectionAbortedError("the delivery was given up on")

    def cut_off(self) -> None:
        """Called from the event loop when the delivery is cancelled (its time limit, a shutdown)."""
        with self._lock:
            self.cut = True
            server = self._server
        if server is not None:
            _shut(server)


def _shut(server: smtplib.SMTP) -> None:
    """Shut the session's socket down: a read or write blocked in the worker thread ends at once."""
    sock = getattr(server, "sock", None)
    if sock is None:
        return
    try:
        socket.socket.shutdown(sock, socket.SHUT_RDWR)  # the plain method: works under TLS too
    except OSError:
        pass


def is_gmail(host: str) -> bool:
    """True for Google's SMTP servers."""
    return host.strip().lower().rstrip(".") in GMAIL_HOSTS


def _is_loopback(host: str) -> bool:
    name = host.strip().lower().rstrip(".")
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def _ehlo_name() -> str:
    """This machine's name for EHLO (no DNS lookup; ``socket.getfqdn()`` can stall)."""
    name = re.sub(r"[^A-Za-z0-9.-]", "-", socket.gethostname()).strip("-.")
    return name or "localhost"


def _domain(address: str) -> str:
    return address.rpartition("@")[2] or "localhost"


def scrub(text: str, cfg: Config) -> str:
    """``text`` without the SMTP password (as typed, and as AUTH would have encoded it)."""
    email_cfg = cfg.notifiers.email
    password = email_cfg.password
    if not password or not text:
        return text
    user = email_cfg.username
    variants = {
        password,
        base64.b64encode(password.encode()).decode(),
        base64.b64encode(f"\0{user}\0{password}".encode()).decode(),
    }
    for variant in sorted(variants, key=len, reverse=True):
        text = re.sub(re.escape(variant), "•••", text, flags=re.IGNORECASE)
    return text


def _unsendable_login(cfg: Config) -> str | None:
    """The sentence for a login that SMTP AUTH cannot carry (a character outside ASCII in the
    password or the login name); ``None`` for one it can, and for a relay that needs no login."""
    email_cfg = cfg.notifiers.email
    if not email_cfg.username:
        return None
    if not email_cfg.password.isascii():
        return PASSWORD_NOT_SENDABLE
    if not email_cfg.username.isascii():
        return USERNAME_NOT_SENDABLE
    return None


def unready(cfg: Config) -> str | None:
    """Why no email can be sent (``None`` when the server, the sender, a password for a login
    and at least one recipient are set). A sender that is not an email address (a login such as
    "apikey" and no ``from_address``) is named as such, and so is a login that cannot be sent
    (see :data:`PASSWORD_NOT_SENDABLE`): no connection is opened for either."""
    email_cfg = cfg.notifiers.email
    unsendable = _unsendable_login(cfg)
    if unsendable:
        return unsendable
    if email_cfg.ready():
        return None
    if email_cfg.sender and not is_address(email_cfg.sender):
        return SENDER_NOT_AN_ADDRESS
    return NOT_SET_UP


def build_message(
    cfg: Config,
    content: EmailContent,
    to: Sequence[str],
    *,
    attachment: tuple[str, bytes] | None = None,
) -> EmailMessage:
    """The message: From "<from_name> <sender>", To, Subject, Date, Message-ID, the two
    auto-reply headers, text/plain + text/html and an optional ``(filename, bytes)`` attachment."""
    email_cfg = cfg.notifiers.email
    message = EmailMessage()
    try:
        message["From"] = Address(display_name=email_cfg.from_name, addr_spec=email_cfg.sender)
    except ValueError as exc:  # InvalidHeaderDefect "addr-spec local part with no domain" and friends
        raise SenderNotAnAddress(email_cfg.sender) from exc
    message["To"] = ", ".join(to)
    message["Subject"] = content.subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(idstring="dspace-sentinel", domain=_domain(email_cfg.sender))
    message["Auto-Submitted"] = "auto-generated"
    message["X-Auto-Response-Suppress"] = "All"
    # quoted-printable keeps both bodies 7-bit, so a relay without 8BITMIME takes them too
    message.set_content(content.text, cte="quoted-printable")
    message.add_alternative(content.html, subtype="html", cte="quoted-printable")
    if attachment is not None:
        filename, data = attachment
        maintype, subtype = ATTACHMENT_TYPES.get(
            Path(filename).suffix.lower(), ("application", "octet-stream")
        )
        # reports are written as UTF-8; without the parameter a text/* part means US-ASCII
        params = {"charset": "utf-8"} if maintype == "text" else None
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename, params=params)
    return message


def _session(
    cfg: Config, message: EmailMessage, to: Sequence[str], connection: _Connection | None = None
) -> dict[str, Any]:
    """One SMTP session (blocking; runs in a worker thread). Returns the refused recipients.

    ``connection`` learns the socket as soon as there is one, so the delivery can be cut off."""
    email_cfg = cfg.notifiers.email
    host, port, security = email_cfg.smtp_host, email_cfg.smtp_port, email_cfg.security
    if email_cfg.username and security == "none" and not _is_loopback(host):
        raise PlainPasswordRefused()
    context = ssl.create_default_context()
    if security == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(
            host, port, local_hostname=_ehlo_name(), timeout=TIMEOUT_S, context=context
        )
    else:
        server = smtplib.SMTP(host, port, local_hostname=_ehlo_name(), timeout=TIMEOUT_S)
    try:
        if connection is not None:
            connection.attach(server)
        server.ehlo()
        if security == "starttls":
            server.starttls(context=context)
            if connection is not None:
                connection.attach(server)  # a cut during the handshake could not shut the socket
            server.ehlo()
        if email_cfg.username:
            server.login(email_cfg.username, email_cfg.password)
        return dict(server.send_message(message, from_addr=email_cfg.sender, to_addrs=list(to)))
    finally:
        try:
            server.quit()
        except Exception:
            server.close()


async def _in_daemon_thread(func: Any, *args: Any, on_cancel: Any = None) -> Any:
    """Run the blocking ``func(*args)`` in a daemon thread and await its result.

    Unlike ``asyncio.to_thread`` (whose pool threads are joined when Python exits), a mail
    server that hangs can never hold up a shutdown: cancelling returns at once and the thread
    ends by itself when its socket timeout fires; its late answer is dropped. ``on_cancel`` is
    called when the awaiting coroutine is cancelled (to cut the session off).
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()

    def settle(result: Any, error: BaseException | None) -> None:
        if future.done():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(result)

    def run() -> None:
        try:
            result, error = func(*args), None
        except BaseException as exc:  # handed to the awaiting coroutine
            result, error = None, exc
        try:
            loop.call_soon_threadsafe(settle, result, error)
        except RuntimeError:  # the event loop is gone (shutdown): nobody is waiting any more
            pass

    threading.Thread(target=run, name="sentinel-smtp", daemon=True).start()
    try:
        return await future
    except asyncio.CancelledError:
        if on_cancel is not None:
            try:
                on_cancel()
            except Exception:  # pragma: no cover - cutting off is best effort
                log.debug("could not cut the SMTP session off", exc_info=True)
        raise


def _reply(code: Any, text: Any) -> str:
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    text = " ".join(str(text or "").split())
    return f"{code} {text}".strip() if code else text


def _first_answer(code: Any, text: Any, cfg: Config, limit: int = 80) -> str:
    """``"535 5.7.8 Username and Password not accepted"``: the code and the first sentence of the
    first line of a server's answer, at most ``limit`` characters of it (Gmail's goes on with
    "For more information, go to" and a link on the next line). The password is taken out
    (:func:`scrub`) before the answer is cut, so no part of it can be left behind."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    lines = scrub(str(text or ""), cfg).strip().splitlines()
    first = " ".join(lines[0].split()) if lines else ""
    first = first.split(". ", 1)[0].rstrip(".")
    if len(first) > limit:  # cut after a whole word
        cut = first[: limit - 1]
        first = (cut.rsplit(" ", 1)[0] if " " in cut else cut).rstrip() + "…"
    return f"{code} {first}".strip() if code else first


def _timed_out(exc: BaseException) -> bool:
    """True when ``exc`` is a socket time-out, also one that ``smtplib`` reported as a disconnect:
    a read that times out becomes "Connection unexpectedly closed: timed out", a write (the DATA
    of a large report over a slow uplink) plain "Server not connected" with the ``TimeoutError``
    left in ``__context__``."""
    if isinstance(exc, TimeoutError):
        return True
    if not isinstance(exc, smtplib.SMTPServerDisconnected):
        return False
    return "timed out" in str(exc).lower() or any(
        isinstance(reason, TimeoutError) for reason in (exc.__cause__, exc.__context__)
    )


def _unexpected(exc: BaseException) -> str:
    """A failure nothing above knows: its class only, never its own text (which can hold a
    character of the password, where it stands, a path …)."""
    return f"unexpected error ({exc.__class__.__name__})"


def describe_error(exc: BaseException, cfg: Config) -> str:
    """An SMTP failure as a plain sentence (never with the password, or a part of it, in it)."""
    email_cfg = cfg.notifiers.email
    host, port = email_cfg.smtp_host, email_cfg.smtp_port
    where = f"{host}:{port}"
    tls_hint = "Gmail: starttls on port 587, ssl on port 465"
    if isinstance(exc, SenderNotAnAddress):
        text = SENDER_NOT_AN_ADDRESS
    elif isinstance(exc, PlainPasswordRefused):
        text = (
            'security "none" would send the password unencrypted: use starttls or ssl, or leave the '
            "username empty for a relay that needs no login"
        )
    elif isinstance(exc, smtplib.SMTPAuthenticationError):
        if is_gmail(host):
            answer = _first_answer(exc.smtp_code, exc.smtp_error, cfg)
            who = f" for {email_cfg.username}" if email_cfg.username else ""
            text = f"Gmail refused the login{who} ({answer}): {GMAIL_APP_PASSWORD}"
        else:
            text = f"the mail server refused the login ({exc.smtp_code}): check the username and password"
    elif isinstance(exc, smtplib.SMTPNotSupportedError):
        if "starttls" in str(exc).lower():
            text = f'{where} does not offer STARTTLS: use security "ssl" (port 465) or the server\'s TLS port'
        else:
            text = f"{where} does not accept a login: leave the username empty or use its submission port"
    elif isinstance(exc, smtplib.SMTPSenderRefused):
        text = f"the mail server refused the sender {exc.sender}: {_reply(exc.smtp_code, exc.smtp_error)}"
    elif isinstance(exc, smtplib.SMTPRecipientsRefused):
        text = "the mail server refused every recipient"
    elif isinstance(exc, smtplib.SMTPDataError):
        text = f"the mail server refused the message: {_reply(exc.smtp_code, exc.smtp_error)}"
    elif _timed_out(exc):
        text = f"{where} did not answer within {TIMEOUT_S:.0f} s"
        if port == 465 and email_cfg.security != "ssl":
            text += ' (port 465 needs security "ssl")'
    elif isinstance(exc, smtplib.SMTPServerDisconnected):
        if is_gmail(host):  # the port and security may well be right
            text = (
                f"{where} closed the connection. {GMAIL_CLOSED_HINT}. "
                f"Otherwise check the port and security ({tls_hint})"
            )
        else:
            text = f"{where} closed the connection: check the port and security ({tls_hint})"
    elif isinstance(exc, smtplib.SMTPConnectError | smtplib.SMTPHeloError):
        text = f"{where} refused the session: {_reply(exc.smtp_code, exc.smtp_error)}"
    elif isinstance(exc, smtplib.SMTPResponseException):
        text = f"the mail server answered {_reply(exc.smtp_code, exc.smtp_error)}"
    elif isinstance(exc, ssl.SSLCertVerificationError):
        reason = getattr(exc, "verify_message", None) or "not trusted"
        text = f"the TLS certificate of {host} could not be verified ({reason}): check the server name"
    elif isinstance(exc, ssl.SSLError):
        text = f"TLS with {where} failed: check the security setting ({tls_hint})"
    elif isinstance(exc, socket.gaierror):
        text = f"cannot find the mail server {host}: check its name"
    elif isinstance(exc, ConnectionRefusedError):
        text = f"{where} refused the connection: check the server name and port"
    elif isinstance(exc, OSError):
        text = f"cannot reach {where}: {exc.strerror or exc}"
    elif isinstance(exc, smtplib.SMTPException):
        text = f"the mail server failed: {exc}"
    elif isinstance(exc, UnicodeEncodeError):
        # smtplib could not put the login into ASCII: its text names the character and its position
        text = _unsendable_login(cfg) or _unexpected(exc)
    else:
        text = _unexpected(exc)
    return scrub(text, cfg)


def _results(
    recipients: Sequence[EmailRecipient], error: str | None, refused: dict[str, Any]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for recipient in recipients:
        entry = {"address": recipient.address, "label": recipient.label, "ok": error is None, "error": error}
        answer = refused.get(recipient.address)
        if answer is not None:
            code, text = answer if isinstance(answer, tuple) and len(answer) == 2 else (None, answer)
            entry["ok"], entry["error"] = False, f"refused by the mail server: {_reply(code, text)}"
        out.append(entry)
    return out


async def send(
    cfg: Config,
    content: EmailContent,
    recipients: Sequence[EmailRecipient] | None = None,
    *,
    attachment: tuple[str, bytes] | None = None,
) -> dict[str, Any]:
    """Send ``content`` as one message to ``recipients`` (default: every configured recipient).

    Returns ``{ok, error, results: [{address, label, ok, error}]}``: ``ok`` when the server took
    it for every recipient; a failed session gives every recipient its error and ``error`` that
    one sentence; refused recipients are named in ``error``.
    """
    if getattr(cfg, "demo", False):
        return {"ok": False, "error": DEMO_ERROR, "skipped": "demo", "results": []}
    reason = unready(cfg)
    targets = list(cfg.notifiers.email.recipients if recipients is None else recipients)
    if reason or not targets:
        return {"ok": False, "error": reason or NOT_SET_UP, "results": []}
    try:
        message = build_message(cfg, content, [r.address for r in targets], attachment=attachment)
        connection = _Connection()
        refused = await _in_daemon_thread(
            _session, cfg, message, [r.address for r in targets], connection, on_cancel=connection.cut_off
        )
    except smtplib.SMTPRecipientsRefused as exc:
        results = _results(targets, None, dict(exc.recipients))
    except Exception as exc:  # every failure becomes a sentence; nothing raises into the caller
        error = describe_error(exc, cfg)
        log.warning("email to %d recipient(s) not sent: %s", len(targets), error)
        return {"ok": False, "error": error, "results": _results(targets, error, {})}
    else:
        results = _results(targets, None, refused)
    ok = all(r["ok"] for r in results)
    return {"ok": ok, "error": summarize(results, "recipients", key="address"), "results": results}


async def send_event(
    cfg: Config, event: dict[str, Any], recipients: Sequence[EmailRecipient] | None = None
) -> dict[str, Any]:
    """Send a notification event (``messages.notification_event``) as an email; see :func:`send`."""
    result = await send(cfg, email_messages.render_event(event), recipients)
    result["subject"] = email_messages.subject_for(event)
    return result


async def send_test(cfg: Config, *, address: str | None = None) -> dict[str, Any]:
    """The "Send test email" message to every recipient (or the one ``address``, which the caller
    checked is configured), whether or not email is switched on and whatever their severity."""
    if getattr(cfg, "demo", False):
        return {"ok": False, "error": DEMO_ERROR, "skipped": "demo", "results": []}
    recipients = list(cfg.notifiers.email.recipients)
    if address is not None:
        recipients = [r for r in recipients if r.address.lower() == address.strip().lower()]
    return await send(cfg, email_messages.connected_email(cfg.hostname), recipients)


def _rounded_up(size: int) -> str:
    """``size`` as Sentinel shows sizes (``util.fmt.bytes_``: 1024-based, one decimal) but rounded
    up, so a file over the limit never reads like the limit: 17 MB and one byte is "17.1 MB"."""
    tenths_of_mb = -(-size * 10 // 1024**2)
    return fmt.bytes_(tenths_of_mb * 1024**2 / 10)


async def send_file(cfg: Config, content: EmailContent, path: str | Path) -> dict[str, Any]:
    """Send ``content`` with the file at ``path`` attached to every recipient (reports)."""
    if getattr(cfg, "demo", False):
        return {"ok": False, "error": DEMO_ERROR, "skipped": "demo", "results": []}
    file_path = Path(path)
    try:
        size = file_path.stat().st_size
        if size > MAX_ATTACHMENT_BYTES:
            shown, limit = _rounded_up(size), fmt.bytes_(MAX_ATTACHMENT_BYTES, 0)
            return {
                "ok": False,
                "error": f"{file_path.name} is too large to email ({shown}; the limit is {limit})",
                "results": [],
            }
        data = file_path.read_bytes()
    except OSError as exc:
        return {"ok": False, "error": f"cannot read {file_path.name}: {exc.strerror or exc}", "results": []}
    return await send(cfg, content, attachment=(file_path.name, data))


__all__ = [
    "ATTACHMENT_TYPES",
    "DEMO_ERROR",
    "GMAIL_APP_PASSWORD",
    "GMAIL_CLOSED_HINT",
    "MAX_ATTACHMENT_BYTES",
    "NOT_SET_UP",
    "PASSWORD_NOT_SENDABLE",
    "SENDER_NOT_AN_ADDRESS",
    "TIMEOUT_S",
    "USERNAME_NOT_SENDABLE",
    "SenderNotAnAddress",
    "build_message",
    "describe_error",
    "is_gmail",
    "scrub",
    "send",
    "send_event",
    "send_file",
    "send_test",
    "unready",
]
