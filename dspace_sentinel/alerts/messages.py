"""Telegram (HTML) and plain-text formatting of alerts, incidents, reminders and digests.

The four formats in SPEC section 7 are reproduced byte for byte (hostname and clock
aside); ``tests/test_messages.py`` pins them. Every clock time names its zone ("Started 10:43
UTC", "10:41 +0530  Memory 94 %"): the zone of ``server.timezone``, passed in as ``tz``
(``None``: the server's zone). Everything user-controlled (titles,
messages, log lines) is HTML-escaped so Telegram never rejects a message for a stray
``<``. The chain summary (``service ✅ → TCP ✅ → connect ❌``) is also built here because
it is stored in the alert message and later parsed back by the incident engine to find
the failing rung.
"""

from __future__ import annotations

import datetime as _dt
import html as _html
import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from dspace_sentinel import __version__
from dspace_sentinel.health import STATUS_MARK, component_label
from dspace_sentinel.util.fmt import clock, duration, ms

if TYPE_CHECKING:  # pragma: no cover
    from dspace_sentinel.models import Alert, CheckResult, Incident

BRAND = "DSpace Sentinel"

SEVERITY_ICON: dict[str, str] = {"critical": "🔴", "warning": "🟡"}
SEVERITY_WORD: dict[str, str] = {"critical": "CRITICAL", "warning": "WARNING"}

RUNG_NAMES: dict[str, str] = {"tcp": "TCP"}
"""Short rung ids that are spelled differently in messages (``postgres.tcp`` → ``TCP``)."""

_TAG_RE = re.compile(r"<[^>]+>")
_MARKS = tuple(STATUS_MARK.values())


def esc(text: Any) -> str:
    """HTML-escape ``text`` for Telegram's HTML parse mode (quotes are left alone)."""
    return _html.escape(str(text), quote=False)


def to_plain(html: str) -> str:
    """Strip tags and unescape entities: the plain-text twin of a Telegram message."""
    return _html.unescape(_TAG_RE.sub("", html))


def rung_short(rung_id: str) -> str:
    """``postgres.tcp`` → ``TCP``, ``backend.dep.solr`` → ``dep.solr``, ``ports.8983`` → ``8983``."""
    short = rung_id.split(".", 1)[1] if "." in rung_id else rung_id
    return RUNG_NAMES.get(short, short)


def chain_text(chain: Iterable[CheckResult], stop_at: str | None = None) -> str:
    """Ladder summary such as ``service ✅ → TCP ✅ → connect ❌``.

    Rungs are listed in order up to and including ``stop_at`` (a rung id, normally the
    failing rung), so the unreached rungs below it are not shown.
    """
    parts: list[str] = []
    for rung in chain:
        parts.append(f"{rung_short(rung.id)} {STATUS_MARK.get(rung.status, '—')}")
        if stop_at is not None and rung.id == stop_at:
            break
    return " → ".join(parts)


def failing_rung(message: str) -> str | None:
    """Recover the failing rung's short id from a ``component_down`` alert message.

    The first line of such a message is the chain summary produced by :func:`chain_text`,
    ending with the failing rung (``… → search ❌``); this returns ``"search"`` (lower case,
    so ``TCP`` comes back as ``"tcp"``). ``None`` when the line is not a chain summary.
    """
    first = (message or "").split("\n", 1)[0].strip()
    if not first or not first.endswith(_MARKS):
        return None
    last = first.split(" → ")[-1].strip()
    if " " not in last:
        return None
    return last.rsplit(" ", 1)[0].strip().lower()


def sequence_text(alert: Alert) -> str:
    """The short line an alert contributes to an incident sequence.

    ``component_down`` alerts read ``"Solr DOWN"`` / ``"Solr degraded"`` (a failing backend
    search rung reads ``"DSpace search unavailable"``); ``solr_latency`` reads
    ``"Solr response 4.8 s"``; every other rule uses the alert title (``"Memory 94 %"``).
    """
    base = alert.rule.split(":", 1)[0]
    if base == "component_down":
        label = component_label(alert.component)
        if alert.severity == "critical":
            if alert.component == "backend" and failing_rung(alert.message) == "search":
                return "DSpace search unavailable"
            return f"{label} DOWN"
        return f"{label} degraded"
    if base == "solr_latency" and alert.value is not None:
        return f"Solr response {ms(alert.value)}"
    return alert.title


# --------------------------------------------------------------------------- alerts


def _header(icon: str, word: str, hostname: str) -> str:
    return f"{icon} <b>{word}</b> · {esc(hostname)}"


def alert_html(alert: Alert, hostname: str, *, escalated: bool = False, tz: _dt.tzinfo | None = None) -> str:
    """The *opened* (or *escalated*) message (the start time in ``tz``, with its zone)::

    🔴 <b>CRITICAL</b> · server-1
    <b>PostgreSQL unreachable</b>
    service ✅ → TCP ✅ → connect ❌
    connection refused (localhost:5432)

    Started 10:43 UTC · DSpace Sentinel
    """
    icon = SEVERITY_ICON.get(alert.severity, "🔴")
    word = SEVERITY_WORD.get(alert.severity, alert.severity.upper())
    lines = [_header(icon, word, hostname), f"<b>{esc(alert.title)}</b>"]
    for line in (alert.message or "").splitlines():
        if line.strip():
            lines.append(esc(line.rstrip()))
    if escalated:
        lines.append("Escalated from warning")
    lines.append("")
    lines.append(f"Started {clock(alert.opened_ts, tz=tz, zone=True)} · {BRAND}")
    return "\n".join(lines)


def resolved_html(alert: Alert, hostname: str, now: float | None = None) -> str:
    """``✅ <b>Resolved</b> · server-1`` / ``PostgreSQL unreachable · lasted 6 m 12 s``."""
    ended = alert.resolved_ts if alert.resolved_ts is not None else now
    lasted = duration(max(0.0, (ended or 0) - (alert.opened_ts or 0))) if ended is not None else "—"
    return f"{_header('✅', 'Resolved', hostname)}\n{esc(alert.title)} · lasted {lasted}"


def reminder_html(alert: Alert, hostname: str, now: float) -> str:
    """``🔔 <b>Still open</b> · server-1`` / ``🔴 PostgreSQL unreachable · open for 32 m 10 s``."""
    icon = SEVERITY_ICON.get(alert.severity, "🔴")
    open_for = duration(max(0.0, now - (alert.opened_ts or now)))
    return f"{_header('🔔', 'Still open', hostname)}\n{icon} {esc(alert.title)} · open for {open_for}"


# ------------------------------------------------------------------------ incidents


def incident_html(incident: Incident, hostname: str, tz: _dt.tzinfo | None = None) -> str:
    """The incident *opened* message (each time in ``tz``, with its zone)::

    🧭 <b>INCIDENT #24</b> · server-1
    <b>Possible Solr resource exhaustion</b>

    10:41 UTC  Memory 94 %
    10:42 UTC  Solr response 4.8 s

    Affected: DSpace Search, DSpace Backend
    """
    lines = [_header("🧭", f"INCIDENT #{incident.id}", hostname), f"<b>{esc(incident.hypothesis)}</b>", ""]
    for entry in sorted(incident.sequence, key=lambda e: (float(e.get("ts") or 0), str(e.get("text")))):
        lines.append(f"{clock(entry.get('ts'), tz=tz, zone=True)}  {esc(entry.get('text', ''))}")
    if incident.affected:
        lines.append("")
        lines.append(f"Affected: {esc(', '.join(incident.affected))}")
    return "\n".join(lines)


def incident_resolved_html(incident: Incident, hostname: str, now: float | None = None) -> str:
    """``✅ <b>Incident #24 resolved</b> · server-1`` / ``<hypothesis> · lasted 7 m 30 s``."""
    ended = incident.ended_ts if incident.ended_ts is not None else now
    lasted = duration(max(0.0, (ended or 0) - incident.started_ts)) if ended is not None else "—"
    head = _header("✅", f"Incident #{incident.id} resolved", hostname)
    return f"{head}\n{esc(incident.hypothesis)} · lasted {lasted}"


# ------------------------------------------------------------------- digest / test


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def digest_html(
    hostname: str,
    opened: list[Alert],
    incidents: list[Incident],
    open_now: list[Alert],
    now: float | None = None,
) -> str:
    """The daily digest: counts for the last 24 h plus what is open right now.

    The open alerts are listed oldest first (at most five), so the longest-running problems are
    the ones that always make it into the message.
    """
    critical = sum(1 for a in opened if a.severity == "critical")
    lines = [
        _header("📋", "Daily digest", hostname),
        f"Last 24 h: {_plural(len(opened), 'alert')} ({critical} critical) · "
        f"{_plural(len(incidents), 'incident')}",
    ]
    if open_now:
        lines.append(f"Open now: {_plural(len(open_now), 'alert')}")
        oldest_first = sorted(open_now, key=lambda a: (a.opened_ts or 0, a.id or 0))
        for alert in oldest_first[:5]:
            icon = SEVERITY_ICON.get(alert.severity, "🔴")
            since = duration(max(0.0, (now or 0) - (alert.opened_ts or 0))) if now else "—"
            lines.append(f"{icon} {esc(alert.title)} · {since}")
        if len(open_now) > 5:
            lines.append(f"… and {len(open_now) - 5} more")
    else:
        lines.append("Open now: nothing — all clear")
    lines.extend(["", BRAND])
    return "\n".join(lines)


def test_html(hostname: str) -> str:
    """The message behind *Send test message*."""
    return f"👋 {BRAND} is connected · {esc(hostname)}"


# ---------------------------------------------------------------- event envelope


def notification_event(
    kind: str,
    event_type: str,
    *,
    hostname: str,
    severity: str,
    html: str,
    summary: str,
    alert: Alert | None = None,
    incident: Incident | None = None,
    ts: float | None = None,
) -> dict[str, Any]:
    """The dict handed to ``notifiers.dispatch``: one shape for every channel.

    ``kind`` is ``alert`` | ``incident`` | ``digest`` | ``test``; ``type`` is the transition
    (``opened`` | ``escalated`` | ``resolved`` | ``reminder`` | ``digest`` | ``test``).
    ``html`` is the Telegram text, ``text`` its plain twin, ``summary`` a one-liner for logs.
    """
    return {
        "kind": kind,
        "type": event_type,
        "severity": severity,
        "hostname": hostname,
        "ts": ts,
        "summary": summary,
        "html": html,
        "text": to_plain(html),
        "alert": alert.to_dict() if alert is not None else None,
        "incident": incident.to_dict() if incident is not None else None,
        "sentinel_version": __version__,
    }


__all__ = [
    "BRAND",
    "RUNG_NAMES",
    "SEVERITY_ICON",
    "SEVERITY_WORD",
    "alert_html",
    "chain_text",
    "digest_html",
    "esc",
    "failing_rung",
    "incident_html",
    "incident_resolved_html",
    "notification_event",
    "reminder_html",
    "resolved_html",
    "rung_short",
    "sequence_text",
    "test_html",
    "to_plain",
]
