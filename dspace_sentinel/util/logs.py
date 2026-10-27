"""Log tailing and error counting. Files are only ever read from the tail (seek from end).

Understood line formats: DSpace's log4j2 ``dspace.log`` (7–10), Solr's ``solr.log``, Tomcat's JULI
``catalina.YYYY-MM-DD.log``, the ``catalina.out`` that packaged Ubuntu/Debian Tomcat (``tomcat9``,
``tomcat10``) writes through rsyslog (``[2026-09-26 10:41:02] [crit]message``) and nginx's
``error.log``.

Log lines carry wall-clock timestamps without a zone. By default they are read in the Sentinel
host's local time; a file can name its zone (``logs.files[].timezone``: an IANA name, ``UTC``
or ``local``). Solr stamps its log in UTC unless ``SOLR_TIMEZONE`` says otherwise, so a file
with no zone configured is read as UTC when its newest line matches the file's modification
time only that way (:func:`looks_like_utc`). Lines stamped more than :data:`FUTURE_SKEW_S`
after "now" are never counted: they come from a zone mismatch, not from the future.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_MAX_BYTES = 262144

FUTURE_SKEW_S = 300.0
"""A line stamped this much after "now" is ignored (clock skew allowance)."""

MTIME_TOLERANCE_S = 120.0
"""How close the newest line's time must be to the file's modification time to tell the zone."""

LOCAL_ZONE_NAMES = frozenset({"", "local", "auto"})
UTC_ZONE_NAMES = frozenset({"utc", "z", "gmt", "etc/utc"})

# 2024-01-01 10:41:02,123 ERROR   (DSpace / log4j2)   ·  2024-01-01 10:41:02.123 ERROR (Solr)
# [2026-09-26 10:41:02] [crit]msg  (catalina.out of packaged Ubuntu/Debian Tomcat, via rsyslog)
_TS_ISO = re.compile(
    r"^\[?(?P<date>\d{4}-\d{2}-\d{2})[ T](?P<time>\d{2}:\d{2}:\d{2})(?:[,.](?P<ms>\d{1,6}))?"
)
# 24-Sep-2026 10:41:02.123 SEVERE (Tomcat catalina.out, JULI)
_TS_JULI = re.compile(r"^(?P<day>\d{2})-(?P<mon>[A-Za-z]{3})-(?P<year>\d{4}) (?P<time>\d{2}:\d{2}:\d{2})")
# 2026/09/24 10:41:02 [error] (nginx error.log)
_TS_NGINX = re.compile(r"^(?P<date>\d{4}/\d{2}/\d{2}) (?P<time>\d{2}:\d{2}:\d{2})")

# Packaged Tomcat's catalina.out: rsyslog's TomcatFormat ("[%timegenerated%] [%syslogseverity-text%]%msg%")
# gets one journald entry per stdout line, and Debian's SystemdFormatter gives *every* line of a record
# the record's priority, its Throwable included: a 40-line trace arrives as 40 stamped "[crit]" lines.
_RSYSLOG = re.compile(
    r"^\[\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}\]\s*\[(?P<severity>[A-Za-z]+)\](?P<message>.*)$"
)
# the lines of a Throwable after its first: frames, "... 8 more", causes, suppressed ones
_TRACE_LINE = re.compile(
    r"^(?:\s+at\s|\s+\.\.\.\s*\d+\s+more|\s*Caused by:|\s+Suppressed:|\s+\[CIRCULAR REFERENCE)"
)
# a Throwable's first line, Throwable.toString(): "java.net.BindException: Address already in use"
_THROWABLE_HEAD = re.compile(r"^(?:[A-Za-z_$][\w$]*\.)+[A-Za-z_$][\w$]*(?:[:;]|$)")

RECORD_GAP_S = 1.0
"""How far (s) a Throwable's first line may trail the line above it and still belong to its record."""

ERROR_PATTERNS: tuple[str, ...] = (
    r"\bERROR\b",
    r"\bSEVERE\b",
    r"\bFATAL\b",
    r"\[(error|err|crit|alert|emerg)\]",
)
"""Level tokens of an error line; the bracketed ones are nginx's and rsyslog's (syslog severities)."""
WARN_PATTERNS: tuple[str, ...] = (r"\bWARN(?:ING)?\b", r"\[warn(?:ing)?\]")

_ERROR_RE = re.compile("|".join(ERROR_PATTERNS))
_WARN_RE = re.compile("|".join(WARN_PATTERNS))

_MONTHS = {
    m: i
    for i, m in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1
    )
}


def tail_lines(path: str | Path, max_bytes: int = DEFAULT_MAX_BYTES) -> list[str]:
    """Return the last lines of ``path`` reading at most ``max_bytes`` from the end.

    The first (possibly partial) line is dropped when the file is larger than
    ``max_bytes``. Raises ``FileNotFoundError``/``PermissionError`` like ``open`` does.
    """
    path = Path(path)
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        start = max(0, size - max_bytes)
        fh.seek(start)
        data = fh.read()
    text = data.decode("utf-8", "replace")
    lines = text.splitlines()
    if start > 0 and lines:
        lines = lines[1:]
    return lines


def resolve_daily(path: str | Path, day: _dt.date | None = None) -> Path:
    """For ``daily_suffix`` logs return ``<path>.YYYY-MM-DD`` when it exists, else ``path``."""
    path = Path(path)
    day = day or _dt.date.today()
    candidate = path.with_name(f"{path.name}.{day.isoformat()}")
    return candidate if candidate.exists() else path


def zone_for(name: str | None) -> _dt.tzinfo | None:
    """The zone a ``timezone`` setting names: ``None`` for local time (``""``, ``local``, ``auto``).

    ``UTC`` (any case) is always available; other values are IANA names (``Asia/Colombo``).
    Raises ``ValueError`` for an unknown zone.
    """
    key = (name or "").strip()
    if key.lower() in LOCAL_ZONE_NAMES:
        return None
    if key.lower() in UTC_ZONE_NAMES:
        return _dt.timezone.utc
    try:
        return ZoneInfo(key)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            f"unknown time zone {key!r}: use an IANA name such as Asia/Colombo, UTC or local"
        ) from exc


def _epoch(dt: _dt.datetime, tz: _dt.tzinfo | None) -> float:
    return (dt if tz is None else dt.replace(tzinfo=tz)).timestamp()


def parse_timestamp(line: str, tz: _dt.tzinfo | None = None) -> float | None:
    """Epoch seconds of the timestamp at the start of ``line``, or ``None``.

    The wall-clock time is read in ``tz`` (the host's local time when ``None``). Understands
    DSpace/log4j ``2024-01-01 10:41:02,123``, Solr ``2024-01-01 10:41:02.123``, Tomcat
    ``24-Sep-2026 10:41:02.123``, rsyslog's ``[2026-09-26 10:41:02]`` (packaged Tomcat's
    ``catalina.out``) and nginx ``2026/09/24 10:41:02``.
    """
    m = _TS_ISO.match(line)
    if m:
        try:
            dt = _dt.datetime.strptime(f"{m.group('date')} {m.group('time')}", "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
        ms = m.group("ms")
        frac = int(ms.ljust(6, "0")[:6]) / 1e6 if ms else 0.0
        return _epoch(dt, tz) + frac
    m = _TS_NGINX.match(line)
    if m:
        try:
            dt = _dt.datetime.strptime(f"{m.group('date')} {m.group('time')}", "%Y/%m/%d %H:%M:%S")
        except ValueError:
            return None
        return _epoch(dt, tz)
    m = _TS_JULI.match(line)
    if m:
        month = _MONTHS.get(m.group("mon").lower())
        if not month:
            return None
        try:
            dt = _dt.datetime.strptime(
                f"{m.group('year')}-{month:02d}-{m.group('day')} {m.group('time')}", "%Y-%m-%d %H:%M:%S"
            )
        except ValueError:
            return None
        return _epoch(dt, tz)
    return None


def looks_like_utc(lines: list[str], mtime: float, tolerance: float = MTIME_TOLERANCE_S) -> bool:
    """True when the newest stamped line matches the file's modification time only when read as UTC.

    A log's last line is written when the file was last modified, so its timestamp read in the
    right zone lies within seconds of ``mtime``. On a UTC host both readings agree (``False``).
    """
    for raw in reversed(lines):
        line = raw.rstrip()
        local = parse_timestamp(line)
        if local is None:
            continue
        utc = parse_timestamp(line, _dt.timezone.utc)
        return utc is not None and abs(mtime - utc) <= tolerance < abs(mtime - local)
    return False


@dataclass
class LogSummary:
    """Result of :func:`count_errors`."""

    errors: int
    warnings: int
    last_error: dict[str, Any] | None
    lines_scanned: int
    first_ts: float | None
    last_ts: float | None

    def to_dict(self) -> dict[str, Any]:
        """Plain dict form."""
        return asdict(self)


def count_errors(
    lines: list[str],
    since_ts: float,
    patterns: list[str] | tuple[str, ...] | None = None,
    warn_patterns: list[str] | tuple[str, ...] | None = None,
    *,
    tz: _dt.tzinfo | None = None,
    now: float | None = None,
) -> LogSummary:
    """Count error and warning lines with a timestamp ≥ ``since_ts``.

    Only the first line of an entry is counted, so one exception with a 40-line trace counts
    once and the last error is the entry's first line. In most formats the trace lines carry no
    timestamp and are skipped. Packaged Tomcat's ``catalina.out`` (rsyslog) stamps every line of
    a record with the record's severity, so there a stamped line continues the record above it
    when its message is empty, is a trace line (``    at …``, ``Caused by: …``, ``    ... 8
    more``, ``    Suppressed: …``) or is a Throwable's first line (``java.net.BindException: …``)
    with the same severity within :data:`RECORD_GAP_S` of that record (see
    :func:`_continues_record`). ``patterns`` override the default error regexes. Timestamps are
    read in ``tz`` (local time when ``None``); with ``now`` given, lines stamped more than
    :data:`FUTURE_SKEW_S` later are not counted.
    """
    until = None if now is None else now + FUTURE_SKEW_S
    err_re = re.compile("|".join(patterns)) if patterns else _ERROR_RE
    warn_re = re.compile("|".join(warn_patterns)) if warn_patterns else _WARN_RE
    errors = warnings = 0
    last_error: dict[str, Any] | None = None
    first_ts: float | None = None
    last_ts: float | None = None
    record: list[Any] | None = None  # [severity, time of its latest line] of the last rsyslog entry counted
    for raw in lines:
        line = raw.rstrip()
        if not line:
            continue
        ts = parse_timestamp(line, tz)
        if ts is None:
            continue  # continuation line (stack trace): never counted as a new entry
        if first_ts is None:
            first_ts = ts
        last_ts = ts
        level = _level(line[:200], err_re, warn_re)
        syslog = _RSYSLOG.match(line)
        if syslog is not None:
            severity = syslog.group("severity").lower()
            if _continues_record(syslog.group("message"), severity, ts, record):
                if record is not None and record[0] == severity:
                    record[1] = ts
                continue
            if level is not None:
                record = [severity, ts]
        if level is None or ts < since_ts or (until is not None and ts > until):
            continue
        if level == "error":
            errors += 1
            last_error = {"line": line[:500], "ts": ts}
        else:
            warnings += 1
    return LogSummary(errors, warnings, last_error, len(lines), first_ts, last_ts)


def _level(head: str, err_re: re.Pattern[str], warn_re: re.Pattern[str]) -> str | None:
    """``"error"``, ``"warning"`` or ``None`` for the start of a line (its level token)."""
    if err_re.search(head):
        return "error"
    if warn_re.search(head):
        return "warning"
    return None


def _continues_record(message: str, severity: str, ts: float, record: list[Any] | None) -> bool:
    """True when an rsyslog ``catalina.out`` line belongs to the record above it, not a new one.

    ``message`` is what follows ``[severity]`` (rsyslog's %msg% may keep one leading space).
    An empty message (the empty line that ends every Throwable) and a trace line always continue;
    a Throwable's first line continues ``record`` (``[severity, time of its latest line]``) when it
    has the same severity and follows within :data:`RECORD_GAP_S`. Two records written in the same
    second both count: only the lines a Throwable adds are folded into its record.
    """
    text = message[1:] if message.startswith(" ") else message
    if not text.strip() or _TRACE_LINE.match(text):
        return True
    return (
        record is not None
        and record[0] == severity
        and 0.0 <= ts - record[1] <= RECORD_GAP_S
        and _THROWABLE_HEAD.match(text) is not None
    )


def summarize_file(
    path: str | Path, since_ts: float, max_bytes: int = DEFAULT_MAX_BYTES, daily_suffix: bool = False
) -> LogSummary | None:
    """Tail ``path`` and count errors since ``since_ts``; ``None`` when the file is missing/unreadable."""
    target = resolve_daily(path) if daily_suffix else Path(path)
    try:
        lines = tail_lines(target, max_bytes=max_bytes)
    except (FileNotFoundError, PermissionError, IsADirectoryError, OSError):
        return None
    return count_errors(lines, since_ts)


__all__ = [
    "DEFAULT_MAX_BYTES",
    "ERROR_PATTERNS",
    "FUTURE_SKEW_S",
    "MTIME_TOLERANCE_S",
    "WARN_PATTERNS",
    "LogSummary",
    "count_errors",
    "looks_like_utc",
    "parse_timestamp",
    "resolve_daily",
    "summarize_file",
    "tail_lines",
    "zone_for",
]
