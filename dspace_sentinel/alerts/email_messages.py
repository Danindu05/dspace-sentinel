"""Email formatting: the subject and the two bodies (plain text + HTML) of every notification.

An email carries the same facts as the Telegram message (``messages.py``): it is built from the
event's Telegram HTML line by line, so the two never drift apart — the header line becomes a
coloured badge (``🔴 CRITICAL``), the bold title a heading, the chain summary a monospace line,
an incident's timeline a small table and "Started 10:43 UTC" a footnote. The HTML uses inline styles
only, loads nothing (no images, no web fonts) and sets no background colour outside the badge,
so light and dark mail clients both render it readably. The plain-text part is the Telegram
message's own plain twin.

Subjects (``host`` = the configured hostname):

* ``[Sentinel] CRITICAL · host · title`` / ``[Sentinel] Warning · host · title`` (opened)
* ``[Sentinel] CRITICAL (escalated) · host · title``
* ``[Sentinel] Still open · host · title`` (reminder)
* ``[Sentinel] Resolved · host · title (lasted 6 m 12 s)``
* ``[Sentinel] Incident #24 · host · hypothesis`` / ``[Sentinel] Incident #24 resolved · host ·
  hypothesis (lasted 7 m 30 s)``
* ``[Sentinel] Daily digest · host``, ``[Sentinel] Test email · host``,
  ``[Sentinel] Report · host · report title``
"""

from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass
from typing import Any

from dspace_sentinel.alerts.messages import BRAND, esc, to_plain
from dspace_sentinel.util.fmt import duration

SUBJECT_PREFIX = "[Sentinel]"
MAX_SUBJECT = 200
"""Subjects are cut (with "…") beyond this many characters."""

ACCENTS: dict[str, str] = {"critical": "#b42318", "warning": "#b54708", "ok": "#067647", "info": "#175cd3"}
"""Badge and border colours (white badge text stays above 5:1 on each)."""

FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace"

_TABLE_STYLE = "border-collapse:collapse;margin:4px 0 12px;"
_TITLE_STYLE = "margin:0 0 12px;font-size:20px;line-height:1.3;font-weight:700;"

_HEADER = re.compile(r"^(?P<icon>\S+) <b>(?P<word>.+?)</b> · (?P<host>.+)$")
_BOLD_LINE = re.compile(r"^<b>(?P<text>.*)</b>$")
_SEQUENCE = re.compile(r"^(?P<time>\d{1,2}:\d{2}(?: (?:[A-Za-z]{1,6}|[+-]\d{4}))?)  (?P<text>.+)$")
"""An incident timeline row: "10:41 UTC  Memory 94 %" (the zone is a word or a UTC offset)."""
_CHAIN_MARKS = ("✅", "⚠️", "❌", "—")
_SPACES = re.compile(r"\s+")


@dataclass(frozen=True)
class EmailContent:
    """What one email says: ``subject``, ``text`` (text/plain) and ``html`` (text/html)."""

    subject: str
    text: str
    html: str


def subject(*parts: Any) -> str:
    """``[Sentinel] a · b · c`` on one line (no CR/LF can reach the header), at most
    :data:`MAX_SUBJECT` characters."""
    words = [_SPACES.sub(" ", str(p)).strip() for p in parts if p is not None and str(p).strip()]
    line = f"{SUBJECT_PREFIX} {' · '.join(words)}" if words else SUBJECT_PREFIX
    return line if len(line) <= MAX_SUBJECT else line[: MAX_SUBJECT - 1].rstrip() + "…"


def _lasted(start: Any, end: Any, fallback: Any = None) -> str | None:
    try:
        begin = float(start)
        finish = float(end if end is not None else fallback)
    except (TypeError, ValueError):
        return None
    return duration(max(0.0, finish - begin))


def subject_for(event: dict[str, Any]) -> str:
    """The subject of a notification event (see the module docstring)."""
    host = event.get("hostname") or ""
    kind, etype, severity = event.get("kind"), event.get("type"), event.get("severity")
    if kind == "alert":
        alert = event.get("alert") or {}
        title = alert.get("title") or event.get("summary") or ""
        if etype == "escalated":
            return subject("CRITICAL (escalated)", host, title)
        if etype == "reminder":
            return subject("Still open", host, title)
        if etype == "resolved":
            lasted = _lasted(alert.get("opened_ts"), alert.get("resolved_ts"), event.get("ts"))
            return subject("Resolved", host, f"{title} (lasted {lasted})" if lasted else title)
        return subject("CRITICAL" if severity == "critical" else "Warning", host, title)
    if kind == "incident":
        incident = event.get("incident") or {}
        number = incident.get("id")
        hypothesis = incident.get("hypothesis") or incident.get("title") or ""
        if etype == "resolved":
            lasted = _lasted(incident.get("started_ts"), incident.get("ended_ts"), event.get("ts"))
            text = f"{hypothesis} (lasted {lasted})" if lasted else hypothesis
            return subject(f"Incident #{number} resolved", host, text)
        return subject(f"Incident #{number}", host, hypothesis)
    if kind == "digest":
        return subject("Daily digest", host)
    if kind == "test":
        return subject("Test email", host)
    if kind == "report":
        return subject("Report", host, event.get("title") or "")
    return subject(event.get("summary") or "Notification", host)


def _accent(event: dict[str, Any]) -> str:
    if event.get("type") == "resolved":
        return ACCENTS["ok"]
    if event.get("kind") in ("digest", "test", "report"):
        return ACCENTS["info"]
    return ACCENTS["critical"] if event.get("severity") == "critical" else ACCENTS["warning"]


def _blocks(telegram_html: str) -> tuple[tuple[str, str, str] | None, list[tuple[str, ...]]]:
    """Split a Telegram message into ``(badge, blocks)``; every text is plain (unescaped)."""
    lines = telegram_html.split("\n")
    badge: tuple[str, str, str] | None = None
    blocks: list[tuple[str, ...]] = []
    match = _HEADER.match(lines[0].rstrip()) if lines else None
    if match:
        badge = (to_plain(match["icon"]), to_plain(match["word"]), to_plain(match["host"]))
        lines = lines[1:]
    has_title = False
    for raw in lines:
        line = raw.rstrip()
        plain = to_plain(line).strip()
        if not plain or plain == BRAND:
            continue
        bold = _BOLD_LINE.match(line)
        if bold and not has_title:
            blocks.append(("title", to_plain(bold["text"])))
            has_title = True
        elif plain.endswith(f" · {BRAND}"):
            blocks.append(("meta", plain[: -len(f" · {BRAND}")]))
        elif (seq := _SEQUENCE.match(plain)) is not None:
            blocks.append(("row", seq["time"], seq["text"]))
        elif " → " in plain and plain.endswith(_CHAIN_MARKS):
            blocks.append(("mono", plain))
        else:
            blocks.append(("p", plain))
    if not has_title:  # resolved, reminder, digest: the first line after the header says it all
        for index, block in enumerate(blocks):
            if block[0] == "p":
                blocks[index] = ("title", block[1])
                break
    return badge, blocks


def _html_blocks(blocks: list[tuple[str, ...]]) -> str:
    out: list[str] = []
    rows: list[tuple[str, ...]] = []

    def flush_rows() -> None:
        if not rows:
            return
        cells = "".join(
            f'<tr><td style="padding:2px 16px 2px 0;font-family:{MONO};font-size:14px;'
            f'white-space:nowrap;vertical-align:top;">{_html.escape(time)}</td>'
            f'<td style="padding:2px 0;vertical-align:top;">{_html.escape(text)}</td></tr>'
            for _, time, text in rows
        )
        out.append(f'<table role="presentation" style="{_TABLE_STYLE}">{cells}</table>')
        rows.clear()

    for block in blocks:
        kind = block[0]
        if kind == "row":
            rows.append(block)
            continue
        flush_rows()
        text = _html.escape(block[1])
        if kind == "title":
            out.append(f'<h1 style="{_TITLE_STYLE}">{text}</h1>')
        elif kind == "mono":
            out.append(f'<p style="margin:0 0 8px;font-family:{MONO};font-size:14px;">{text}</p>')
        elif kind == "meta":
            out.append(f'<p style="margin:12px 0 0;font-size:13px;">{text}</p>')
        else:
            out.append(f'<p style="margin:0 0 8px;">{text}</p>')
    flush_rows()
    return "\n".join(out)


def _page(title: str, accent: str, badge: tuple[str, str, str] | None, body: str, host: str) -> str:
    badge_html = ""
    if badge is not None:
        icon, word, badge_host = (_html.escape(part) for part in badge)
        badge_html = (
            '<p style="margin:0 0 10px;font-size:13px;">'
            f'<span style="display:inline-block;padding:2px 8px;border-radius:4px;background-color:{accent};'
            f'color:#ffffff;font-weight:700;letter-spacing:0.02em;">{icon} {word}</span>'
            f"&nbsp; {badge_host}</p>"
        )
    footer = f"Sent by {BRAND} on {_html.escape(host)}. Who gets these emails is set in More › Email."
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="color-scheme" content="light dark">\n'
        '<meta name="supported-color-schemes" content="light dark">\n'
        f"<title>{_html.escape(title)}</title>\n</head>\n"
        '<body style="margin:0;padding:0;">\n'
        f'<div style="max-width:600px;margin:0 auto;padding:24px 16px;font-family:{FONT};'
        'font-size:15px;line-height:1.5;">\n'
        f'<div style="border-left:4px solid {accent};padding:2px 0 2px 16px;">\n'
        f"{badge_html}\n{body}\n</div>\n"
        f'<p style="margin:24px 0 0;font-size:12px;">{footer}</p>\n'
        "</div>\n</body>\n</html>\n"
    )


def _text_footer(host: str) -> str:
    return f"\n\n-- \nSent by {BRAND} on {host}. Who gets these emails is set in More › Email.\n"


def render_event(event: dict[str, Any]) -> EmailContent:
    """The email for a notification event (``messages.notification_event``)."""
    telegram_html = str(event.get("html") or esc(event.get("text") or ""))
    host = str(event.get("hostname") or "")
    title = subject_for(event)
    badge, blocks = _blocks(telegram_html)
    text = str(event.get("text") or to_plain(telegram_html)).strip() + _text_footer(host)
    return EmailContent(title, text, _page(title, _accent(event), badge, _html_blocks(blocks), host))


def connected_event(hostname: str) -> dict[str, Any]:
    """The event behind "Send test email" (the same facts as the Telegram test message)."""
    html = (
        f"✉️ <b>Test email</b> · {esc(hostname)}\n<b>{BRAND} is connected</b>\n"
        f"This test from {esc(hostname)} reached you, so its alerts will too."
    )
    event = {"kind": "test", "type": "test", "severity": "warning", "hostname": hostname}
    return {**event, "html": html, "text": to_plain(html)}


def connected_email(hostname: str) -> EmailContent:
    """The "Send test email" message."""
    return render_event(connected_event(hostname))


def report_email(title: str, hostname: str, period: str, filename: str, size: str) -> EmailContent:
    """The email a report file is attached to."""
    html = (
        f"📄 <b>Report</b> · {esc(hostname)}\n<b>{esc(title)}</b>\n{esc(period)}\n"
        f"The report is attached: {esc(filename)} ({esc(size)})."
    )
    event = {"kind": "report", "type": "report", "severity": "warning", "hostname": hostname, "title": title}
    return render_event({**event, "html": html, "text": to_plain(html)})


__all__ = [
    "ACCENTS",
    "MAX_SUBJECT",
    "SUBJECT_PREFIX",
    "EmailContent",
    "connected_email",
    "connected_event",
    "render_event",
    "report_email",
    "subject",
    "subject_for",
]
