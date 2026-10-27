"""Logs collector: errors and warnings in the last ``logs.window_minutes`` of each log (SPEC section 6).

Files are read from the tail only (``util.logs.tail_lines`` seeks from the end). A missing
or unreadable file is ``unknown``, never a warning. ``daily_suffix`` logs follow DSpace's daily
files: DSpace 7–10 write ``dspace.log`` and roll it over to ``dspace.log-YYYY-MM-DD`` (log4j2),
DSpace 6 wrote ``dspace.log.YYYY-MM-DD`` directly. Today's dotted file wins when it exists;
otherwise the plain file or the newest dated one (either spelling), whichever was written last.

A file Sentinel may not read (or one behind a folder it may not enter) is "permission denied" with
``detail.hint`` saying what to do, built from the owner, group and mode (:func:`permission_hint`).
The ``nginx`` entry belongs to the nginx module: while that module is off it is left out.
"""

from __future__ import annotations

import datetime as _dt
import grp
import logging
import os
import pwd
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from dspace_sentinel.collectors.base import Collector, CollectorContext, CollectorResult, run_blocking
from dspace_sentinel.collectors.services import rung
from dspace_sentinel.config import LogFileConfig
from dspace_sentinel.health import threshold_status
from dspace_sentinel.util.logs import (
    DEFAULT_MAX_BYTES,
    LogSummary,
    count_errors,
    looks_like_utc,
    tail_lines,
    zone_for,
)

log = logging.getLogger(__name__)

_DATE_SUFFIX = re.compile(r"^[.-]\d{4}-\d{2}-\d{2}$")
"""``.2026-09-26`` (DSpace 6, log4j1) or ``-2026-09-26`` (DSpace 7–10, log4j2) after the file name."""

NGINX_LOG_ID = "nginx"
"""The log entry of the nginx module: not read while that module is switched off."""

PERMISSION_HINTS: dict[str, str] = {
    "add": (
        "{subject} belongs to {owner}:{group} (mode {mode}): add {user} to the group {group}, "
        "log out and in again, then restart Sentinel."
    ),
    "later": (
        "The user {user} was added to the group {group}, but this Sentinel process does not have it "
        "yet: log out, log in again, then restart Sentinel."
    ),
    "owner_only": (
        "{subject} belongs to {owner}:{group} (mode {mode}), so only {owner} can read it: make it "
        "readable by the group {group}, add {user} to that group, log out and in again, then "
        "restart Sentinel."
    ),
    "general": (
        "Sentinel runs as {user}, who may not read this file: give {user} read access (usually by "
        "adding {user} to the file's group), log out and in again, then restart Sentinel."
    ),
}
"""``detail.hint`` of a "permission denied" log rung (see :func:`access_hint`). Each starts with a
word, never with a user name: the ladder writes a hint's first letter in capitals."""


def resolve_log_path(path: str, daily_suffix: bool = False, today: _dt.date | None = None) -> Path | None:
    """The file to tail for a log entry, or ``None`` when nothing exists.

    Plain logs: the path itself. ``daily_suffix`` logs: DSpace 6's ``<path>.<today>`` when
    present, else the most recently modified of the plain path and its dated siblings
    (``<path>-YYYY-MM-DD`` as DSpace 7–10 roll it, or ``<path>.YYYY-MM-DD``); on a tie the plain
    path (the file DSpace 7–10 write to) wins.
    """
    base = Path(path)
    if not daily_suffix:
        return base if base.is_file() else None
    today = today or _dt.date.today()
    todays = base.with_name(f"{base.name}.{today.isoformat()}")
    if todays.is_file():
        return todays
    candidates: list[Path] = []
    try:
        for sibling in base.parent.iterdir():
            name = sibling.name
            if (
                name.startswith(base.name)
                and _DATE_SUFFIX.match(name[len(base.name) :])
                and sibling.is_file()
            ):
                candidates.append(sibling)
    except OSError:
        candidates = []
    if base.is_file():
        candidates.append(base)
    if not candidates:
        return None

    def _newest(p: Path) -> tuple[float, bool]:
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0.0
        return mtime, p == base

    return max(candidates, key=_newest)


def access_hint(
    subject: str,
    mode: int,
    owner: str,
    group: str,
    user: str,
    *,
    in_group: bool,
    has_group: bool,
    folder: bool = False,
) -> str:
    """What to do about a file (or a folder on its way) that Sentinel's ``user`` may not read.

    ``subject`` is "The file" or "The folder <path>", ``mode`` its ``st_mode``; ``in_group`` says
    the user and group databases put ``user`` in ``group``, ``has_group`` that this process runs
    with it. A group that may not read (a folder: enter) it cannot help; a user listed in the group
    whose process lacks it was added after the login Sentinel was started from. When the group or
    everyone may read and it still fails, something else stops it: the general hint.
    """
    group_bit, other_bit = (stat.S_IXGRP, stat.S_IXOTH) if folder else (stat.S_IRGRP, stat.S_IROTH)
    if mode & other_bit or has_group:
        return PERMISSION_HINTS["general"].format(user=user)
    facts = {"subject": subject, "owner": owner, "group": group, "user": user}
    facts["mode"] = f"{stat.S_IMODE(mode):03o}"
    if not mode & group_bit:
        return PERMISSION_HINTS["owner_only"].format(**facts)
    if in_group:
        return PERMISSION_HINTS["later"].format(**facts)
    return PERMISSION_HINTS["add"].format(**facts)


def sentinel_user() -> str:
    """The name of the user this process runs as (its uid when the user database has no name)."""
    uid = os.geteuid()
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def listed_in_group(user: str, gid: int) -> bool:
    """True when the user and group databases put ``user`` in group ``gid`` (primary or member)."""
    try:
        if pwd.getpwnam(user).pw_gid == gid:
            return True
    except KeyError:
        pass
    try:
        return user in grp.getgrgid(gid).gr_mem
    except KeyError:
        return False


def process_has_group(gid: int) -> bool:
    """True when this process runs with group ``gid`` (its own or a supplementary one)."""
    return gid == os.getegid() or gid in os.getgroups()


def _owner_name(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def _group_name(gid: int) -> str:
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return str(gid)


def closed_folder(path: Path) -> Path | None:
    """The first folder on the way to ``path`` that exists but this process may not enter, or ``None``."""
    try:
        for folder in reversed(path.parents):
            if not os.access(folder, os.X_OK):
                return folder if folder.is_dir() else None
    except (OSError, ValueError):
        return None
    return None


def permission_hint(path: Path) -> str:
    """``detail.hint`` for a log this process may not read, from what can be read about it.

    The file's owner, group and mode, or, when even they are hidden, those of the folder on the
    way that this process may not enter (:func:`access_hint`). Never raises: without facts it is
    the general hint.
    """
    user = "Sentinel's user"
    try:
        user = sentinel_user()
        subject, folder = "The file", False
        try:
            st = path.stat()
        except OSError:
            closed = closed_folder(path)
            if closed is None:
                return PERMISSION_HINTS["general"].format(user=user)
            subject, folder = f"The folder {closed}", True
            st = closed.stat()
        return access_hint(
            subject,
            st.st_mode,
            _owner_name(st.st_uid),
            _group_name(st.st_gid),
            user,
            in_group=listed_in_group(user, st.st_gid),
            has_group=process_has_group(st.st_gid),
            folder=folder,
        )
    except Exception as exc:  # the hint is advice: it never fails the read
        log.debug("no permission hint for %s: %s", path, exc)
        return PERMISSION_HINTS["general"].format(user=user)


@dataclass
class LogRead:
    """Outcome of :func:`summarize_log` for one configured file."""

    path: Path | None
    summary: LogSummary | None
    error: str | None = None
    truncated: bool = False
    """True when the file is bigger than the tail budget, so the window may not be fully covered."""
    timezone: str = "local"
    """The zone its timestamps were read in: ``local``, a configured name, or ``UTC (detected)``."""
    hint: str | None = None
    """What to do about it, for a file Sentinel may not read (:func:`permission_hint`)."""

    def window_covered(self, since_ts: float) -> bool:
        """True when the whole window was scanned: the file was read entirely, or the tail reaches past it."""
        if self.summary is None:
            return False
        if not self.truncated:
            return True
        return self.summary.first_ts is not None and self.summary.first_ts <= since_ts


def summarize_log(
    entry: LogFileConfig,
    since_ts: float,
    max_bytes: int = DEFAULT_MAX_BYTES,
    today: _dt.date | None = None,
    now: float | None = None,
) -> LogRead:
    """Tail one configured log and count its errors since ``since_ts``; blocking (run in a thread).

    Timestamps are read in the entry's ``timezone``. Without one they are local time, unless the
    newest line matches the file's modification time only when read as UTC (Solr's default).
    Lines stamped more than a few minutes after ``now`` are not counted.
    """
    configured = Path(entry.path)
    try:
        target = resolve_log_path(entry.path, entry.daily_suffix, today)
    except PermissionError:  # a folder on the way may not be entered
        return LogRead(configured, None, "permission denied", hint=permission_hint(configured))
    except OSError as exc:
        return LogRead(configured, None, (exc.strerror or str(exc)).lower())
    if target is None:
        if closed_folder(configured) is not None:  # it may well be there, behind that folder
            return LogRead(configured, None, "permission denied", hint=permission_hint(configured))
        return LogRead(None, None, "file not found")
    try:
        st = target.stat()
        lines = tail_lines(target, max_bytes=max_bytes)
    except PermissionError:
        return LogRead(target, None, "permission denied", hint=permission_hint(target))
    except (IsADirectoryError, FileNotFoundError):
        return LogRead(target, None, "file not found")
    except OSError as exc:
        return LogRead(target, None, (exc.strerror or str(exc)).lower())
    tz = zone_for(entry.timezone)
    label = entry.timezone.strip() if tz is not None else "local"
    if tz is None and entry.timezone.strip().lower() != "local" and looks_like_utc(lines, st.st_mtime):
        tz, label = _dt.timezone.utc, "UTC (detected)"
    summary = count_errors(lines, since_ts, tz=tz, now=now)
    return LogRead(target, summary, None, truncated=st.st_size > max_bytes, timezone=label)


class LogsCollector(Collector):
    """``logs`` 📜: one rung per configured file, graded by errors in the window."""

    id: ClassVar[str] = "logs"
    interval_key: ClassVar[str] = "logs_interval"
    label: ClassVar[str] = "Logs"
    emoji: ClassVar[str] = "📜"

    async def collect(self, ctx: CollectorContext) -> CollectorResult:
        """Tail every configured file in one worker thread and grade the error counts."""
        ts = ctx.now
        cfg = ctx.cfg.logs
        window_s = cfg.window_minutes * 60
        since = ts - window_s
        th = ctx.cfg.alerts.log_errors_per_window
        nginx_on = ctx.cfg.module_enabled("nginx")
        entries = [entry for entry in cfg.files if nginx_on or entry.id != NGINX_LOG_ID]

        def _all() -> list[LogRead]:
            return [summarize_log(entry, since, now=ts) for entry in entries]

        # never more than one read in flight: a log dir on a dead NFS mount must not pile up threads
        results = await run_blocking("logs.read", _all)
        checks = []
        files: dict[str, Any] = {}
        total_errors = 0
        total_warnings = 0
        for entry, read in zip(entries, results, strict=True):
            target, summary, error = read.path, read.summary, read.error
            rung_id = f"logs.{entry.id}"
            label = f"{entry.label or entry.id} log"
            display_path = str(target) if target is not None else entry.path
            if summary is None:
                # A file that is not there is informational (nothing to observe); an unreadable
                # one is a counted unknown: the operator can fix the permissions.
                checks.append(
                    rung(
                        rung_id,
                        label,
                        "unknown",
                        ts,
                        message=f"{error} ({entry.path})",
                        path=display_path,
                        informational=True if error == "file not found" else None,
                        hint=read.hint,
                    )
                )
                files[entry.id] = {
                    "errors": None,
                    "warnings": None,
                    "last_error": None,
                    "path": display_path,
                    "label": entry.label or entry.id,
                    "error": error,
                }
                continue
            errors = summary.errors
            warnings = summary.warnings
            total_errors += errors
            total_warnings += warnings
            status = threshold_status(float(errors), th.warning, th.critical)
            message = "" if status == "ok" else f"{errors} errors in {cfg.window_minutes} min"
            covered = read.window_covered(since)
            checks.append(
                rung(
                    rung_id,
                    label,
                    status,
                    ts,
                    value=float(errors),
                    message=message,
                    errors=errors,
                    warnings=warnings,
                    path=display_path,
                    lines_scanned=summary.lines_scanned,
                    window_covered=covered,
                    timezone=read.timezone if read.timezone != "local" else None,
                )
            )
            files[entry.id] = {
                "errors": errors,
                "warnings": warnings,
                "last_error": summary.last_error,
                "path": display_path,
                "label": entry.label or entry.id,
                "window_covered": covered,
            }
        metrics = {
            "files": files,
            "window_minutes": cfg.window_minutes,
            "total_errors": total_errors,
            "total_warnings": total_warnings,
        }
        return CollectorResult(checks=checks, metrics=metrics)


__all__ = [
    "NGINX_LOG_ID",
    "PERMISSION_HINTS",
    "LogRead",
    "LogsCollector",
    "access_hint",
    "closed_folder",
    "listed_in_group",
    "permission_hint",
    "process_has_group",
    "resolve_log_path",
    "sentinel_user",
    "summarize_log",
]
