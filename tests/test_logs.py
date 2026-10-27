"""Log tailing and counting (``util.logs``) plus the logs collector's file resolution and grading."""

from __future__ import annotations

import datetime as _dt
import grp
import os
import pwd
import re
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from dspace_sentinel.collectors import logs as logs_collector
from dspace_sentinel.collectors.base import CollectorContext
from dspace_sentinel.config import Config, LogFileConfig, merge_patch
from dspace_sentinel.state import State
from dspace_sentinel.store import Store
from dspace_sentinel.util import logs


def stamp(ts: float, fmt: str = "%Y-%m-%d %H:%M:%S,000") -> str:
    """Local-time timestamp string the way the log writers format it (round-trips parse_timestamp)."""
    return _dt.datetime.fromtimestamp(ts).strftime(fmt)


def dspace_lines(now: float, errors_recent: int, errors_old: int = 0, warnings: int = 0) -> list[str]:
    lines: list[str] = []
    for i in range(errors_old):
        lines.append(f"{stamp(now - 3600 - i)} ERROR org.dspace.old @ old error {i}")
    for i in range(warnings):
        lines.append(f"{stamp(now - 100 - i)} WARN  org.dspace.x @ careful {i}")
    for i in range(errors_recent):
        lines.append(f"{stamp(now - 60 - i)} ERROR org.dspace.discovery.SolrSearchCore @ Solr down {i}")
        lines.append("java.lang.RuntimeException: boom")
        lines.append("\tat org.dspace.Something.method(Something.java:42)")
    lines.append(f"{stamp(now - 5)} INFO  org.dspace.app @ all fine")
    return lines


# ------------------------------------------------------------------------------ util


def test_tail_lines_reads_from_the_end(tmp_path: Path) -> None:
    path = tmp_path / "big.log"
    content = "".join(f"line {i:05d} {'x' * 40}\n" for i in range(2000))
    path.write_text(content)
    tail = logs.tail_lines(path, max_bytes=1000)
    assert 0 < len(tail) < 40
    assert tail[-1] == "line 01999 " + "x" * 40
    assert all(line.startswith("line ") for line in tail)  # the partial first line was dropped
    small = tmp_path / "small.log"
    small.write_text("a\nb\nc\n")
    assert logs.tail_lines(small) == ["a", "b", "c"]
    with pytest.raises(FileNotFoundError):
        logs.tail_lines(tmp_path / "missing.log")


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("2024-01-01 10:41:02,123 ERROR x", _dt.datetime(2024, 1, 1, 10, 41, 2, 123000).timestamp()),
        ("2024-01-01 10:41:02.5 ERROR solr", _dt.datetime(2024, 1, 1, 10, 41, 2, 500000).timestamp()),
        ("2024-01-01T10:41:02 INFO", _dt.datetime(2024, 1, 1, 10, 41, 2).timestamp()),
        ("24-Sep-2026 10:41:02.123 SEVERE [x]", _dt.datetime(2026, 9, 24, 10, 41, 2).timestamp()),
        ("2026/09/24 10:41:02 [error] 1183#1183", _dt.datetime(2026, 9, 24, 10, 41, 2).timestamp()),
        ("\tat org.dspace.Foo.bar(Foo.java:1)", None),
        ("random text", None),
        ("99-Foo-2026 10:41:02 x", None),
    ],
)
def test_parse_timestamp(line: str, expected: float | None) -> None:
    assert logs.parse_timestamp(line) == expected


LIVE_LINES = (Path(__file__).parent / "fixtures" / "dspace-live" / "log-lines.txt").read_text().splitlines()
"""dspace.log (7-10), Tomcat JULI, Ubuntu/Debian rsyslog catalina.out (crit, warning) and Solr lines."""
LIVE_TS = _dt.datetime(2026, 9, 26, 10, 41, 2).timestamp()


def test_every_live_log_format_carries_a_timestamp() -> None:
    dspace, juli, rsyslog_crit, rsyslog_warning, solr = LIVE_LINES
    assert logs.parse_timestamp(dspace) == pytest.approx(LIVE_TS + 0.123)
    assert logs.parse_timestamp(juli) == LIVE_TS
    # packaged tomcat9/tomcat10 write catalina.out through rsyslog: "[YYYY-MM-DD HH:MM:SS] [crit]msg"
    assert logs.parse_timestamp(rsyslog_crit) == LIVE_TS
    assert logs.parse_timestamp(rsyslog_warning) == LIVE_TS
    assert logs.parse_timestamp(solr) == pytest.approx(LIVE_TS + 0.123)


def test_rsyslog_catalina_out_lines_are_counted() -> None:
    summary = logs.count_errors(LIVE_LINES, since_ts=LIVE_TS - 60, now=LIVE_TS + 1)
    assert (summary.errors, summary.warnings) == (4, 1)  # ERROR, SEVERE, [crit], ERROR · [warning]
    catalina = [
        "[2026-09-26 10:41:02] [crit]One or more listeners failed to start.",
        "[2026-09-26 10:41:03]  [err] spacing after the bracket varies",
        "[2026-09-26 10:41:04][alert]Out of memory",
        "[2026-09-26 10:41:05] [warning]The web application [server] appears to have started a thread",
        "[2026-09-26 10:41:06] [info]Server startup in [12345] milliseconds",
        "[2026-09-26 10:41:07] [notice]Deploying web application directory",
        "\tat org.apache.catalina.core.StandardContext.startInternal(StandardContext.java:4412)",
    ]
    summary = logs.count_errors(catalina, since_ts=LIVE_TS - 60, now=LIVE_TS + 60)
    assert (summary.errors, summary.warnings) == (3, 1)
    assert summary.last_error is not None and "Out of memory" in summary.last_error["line"]
    assert summary.first_ts == LIVE_TS and summary.last_ts == LIVE_TS + 5
    # outside the window: nothing counted, even though the lines are errors
    assert logs.count_errors(catalina, since_ts=LIVE_TS + 600, now=LIVE_TS + 700).errors == 0


TOMCAT_STDOUT = Path(__file__).parent / "fixtures" / "dspace-live" / "tomcat10-systemd-stdout.txt"
"""Real stdout of packaged Tomcat under systemd, in sections headed ``# <name>: …``. Debian's
``org.apache.juli.SystemdFormatter`` (the ConsoleHandler formatter of the tomcat9/tomcat10 packages)
prefixes every line of a record, its Throwable included, with the record's syslog priority ("<2>"
SEVERE, "<4>" WARNING, "<6>" INFO) and ends a Throwable with an empty "<2>" line; the lines of a
multi-line *message* carry no prefix. Made with the jars of Ubuntu 24.04's tomcat10 10.1.55."""

SYSLOG_SEVERITY = ("emerg", "alert", "crit", "err", "warning", "notice", "info", "debug")


def tomcat_stdout(name: str) -> list[str]:
    """The lines of one section of :data:`TOMCAT_STDOUT`."""
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in TOMCAT_STDOUT.read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            current = sections.setdefault(line[2:].split(":", 1)[0], [])
        elif current is not None:
            current.append(line)
    return sections[name]


def catalina_out(stdout: list[str], when: float, lead: str = "") -> list[str]:
    """``stdout`` the way the package's /etc/rsyslog.d/tomcat10.conf writes it to catalina.out.

    journald reads a leading "<N>" as the line's priority (a line without one gets the unit's
    default, info), and rsyslog's TomcatFormat template writes "[%timegenerated%]
    [%syslogseverity-text%]%msg%". ``lead`` is the space %msg% keeps when the message reaches
    rsyslog through the syslog socket.
    """
    stamp_ = _dt.datetime.fromtimestamp(when).strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for line in stdout:
        m = re.match(r"<([0-7])>(.*)", line)
        severity, message = (SYSLOG_SEVERITY[int(m.group(1))], m.group(2)) if m else ("info", line)
        out.append(f"[{stamp_}] [{severity}]{lead}{message}")
    return out


@pytest.mark.parametrize("lead", ["", " "])
def test_packaged_tomcat_counts_a_stack_trace_once(lead: str) -> None:
    """One SEVERE record is one error however long its Throwable, and the last error is its first line."""
    lines = catalina_out(tomcat_stdout("port-taken"), LIVE_TS, lead)
    assert len(lines) == 32 and sum("] [crit]" in line for line in lines) == 25
    summary = logs.count_errors(lines, since_ts=LIVE_TS - 900, now=LIVE_TS + 60)
    assert (summary.errors, summary.warnings) == (1, 0)
    assert summary.last_error == {"line": lines[1], "ts": LIVE_TS}
    assert lines[1].endswith(f'[crit]{lead}Failed to initialize component [Connector["http-nio-9865"]]')
    # R2's reproduction: 21 lines of a start failure, all "[crit]" and the last one empty
    lines = catalina_out(tomcat_stdout("start-failed"), LIVE_TS, lead)
    assert len(lines) == 21 and lines[-1].endswith("[crit]" + lead)
    summary = logs.count_errors(lines, since_ts=LIVE_TS - 900, now=LIVE_TS + 60)
    assert summary.errors == 1 and summary.last_error is not None
    assert summary.last_error["line"] == lines[0] and "Failed to start component" in lines[0]


@pytest.mark.parametrize("lead", ["", " "])
def test_packaged_tomcat_counts_a_warning_with_its_throwable_once(lead: str) -> None:
    scan = catalina_out(tomcat_stdout("jar-scan"), LIVE_TS, lead)
    assert len(scan) == 18 and all("] [warning]" in line for line in scan)
    summary = logs.count_errors(scan, since_ts=LIVE_TS - 900, now=LIVE_TS + 60)
    assert (summary.errors, summary.warnings) == (0, 1)
    # a multi-line message (the thread's stack) prefixes only its first line: the rest reads [info]
    leak = catalina_out(tomcat_stdout("thread-leak"), LIVE_TS, lead)
    assert [line.split("]")[1] for line in leak] == [" [warning", " [info", " [info", " [info"]
    assert logs.count_errors(leak, since_ts=LIVE_TS - 900, now=LIVE_TS + 60).warnings == 1


def test_packaged_tomcat_counts_every_record() -> None:
    """Records keep counting one by one: in later seconds, and several at once in the same second."""
    failed = catalina_out(tomcat_stdout("port-taken"), LIVE_TS)
    again = catalina_out(tomcat_stdout("start-failed"), LIVE_TS + 1)
    summary = logs.count_errors(failed + again, since_ts=LIVE_TS - 900, now=LIVE_TS + 60)
    assert summary.errors == 2 and summary.last_error is not None
    assert summary.last_error["line"] == again[0]
    # an undeploy warns about every thread it finds, all in one second: each one is a warning
    leaks = catalina_out(tomcat_stdout("thread-leak") * 5 + tomcat_stdout("jar-scan"), LIVE_TS + 2)
    summary = logs.count_errors(failed + leaks, since_ts=LIVE_TS - 900, now=LIVE_TS + 60)
    assert (summary.errors, summary.warnings) == (1, 6)
    # a tail that starts inside a trace counts none of it
    tail = catalina_out(tomcat_stdout("port-taken")[5:], LIVE_TS)
    assert logs.count_errors(tail, since_ts=LIVE_TS - 900, now=LIVE_TS + 60).errors == 0


def test_juli_catalina_log_counts_a_stack_trace_once() -> None:
    """The same record in the JULI file (catalina.YYYY-MM-DD.log): its trace lines carry no timestamp."""
    record = tomcat_stdout("port-taken")[1:26]
    source = "org.apache.catalina.util.LifecycleBase.handleSubClassException"
    juli = [f"26-Sep-2026 10:41:02.123 SEVERE [main] {source} {record[0][3:]}"]
    juli += [line[3:].replace("    ", "\t", 1) for line in record[1:]]
    summary = logs.count_errors(juli, since_ts=LIVE_TS - 900, now=LIVE_TS + 60)
    assert (summary.errors, summary.warnings) == (1, 0) and summary.last_error["line"] == juli[0]


def test_count_errors_window_and_continuations() -> None:
    now = time.time()
    lines = dspace_lines(now, errors_recent=3, errors_old=5, warnings=2)
    summary = logs.count_errors(lines, since_ts=now - 300)
    assert summary.errors == 3  # traces are continuation lines, old errors are outside the window
    assert summary.warnings == 2
    # last_error = the last matching line in file order (written last), not the newest timestamp
    assert summary.last_error is not None and "Solr down 2" in summary.last_error["line"]
    assert summary.last_error["ts"] == pytest.approx(now - 62, abs=1.0)
    assert summary.lines_scanned == len(lines)
    assert summary.first_ts is not None and summary.first_ts < now - 3000
    assert summary.last_ts == pytest.approx(now - 5, abs=1.0)
    everything = logs.count_errors(lines, since_ts=0)
    assert everything.errors == 8
    assert logs.count_errors([], since_ts=0).errors == 0


def test_count_errors_other_formats() -> None:
    now = time.time()
    lines = [
        f"{stamp(now - 10, '%d-%b-%Y %H:%M:%S.000')} SEVERE [main] o.a.catalina.core.StandardContext start",
        f"{stamp(now - 9, '%Y/%m/%d %H:%M:%S')} [error] 1183#1183: *4821 upstream closed connection",
        f"{stamp(now - 8, '%Y/%m/%d %H:%M:%S')} [warn] 1183#1183: something",
        f"{stamp(now - 7, '%Y-%m-%d %H:%M:%S.123')} INFO  (qtp-19) [ x:search] o.a.s.c.S.Request ok",
        f"{stamp(now - 6, '%Y-%m-%d %H:%M:%S.123')} ERROR (qtp-19) [ x:search] o.a.s.h.RequestHandler OOM",
    ]
    summary = logs.count_errors(lines, since_ts=now - 60)
    assert summary.errors == 3 and summary.warnings == 1
    custom = logs.count_errors(lines, since_ts=now - 60, patterns=[r"OOM"], warn_patterns=[r"upstream"])
    assert custom.errors == 1 and custom.warnings == 1


def test_resolve_daily_and_summarize_file(tmp_path: Path) -> None:
    base = tmp_path / "dspace.log"
    assert logs.resolve_daily(base) == base
    today = base.with_name(f"dspace.log.{_dt.date.today().isoformat()}")
    today.write_text(f"{stamp(time.time() - 3)} ERROR x\n")
    assert logs.resolve_daily(base) == today
    summary = logs.summarize_file(base, since_ts=time.time() - 60, daily_suffix=True)
    assert summary is not None and summary.errors == 1
    assert logs.summarize_file(tmp_path / "nope.log", since_ts=0) is None
    assert logs.summarize_file(tmp_path, since_ts=0) is None  # a directory is not a log


# -------------------------------------------------------------- collector: resolution


def test_resolve_log_path_plain(tmp_path: Path) -> None:
    path = tmp_path / "catalina.out"
    assert logs_collector.resolve_log_path(str(path)) is None
    path.write_text("x\n")
    assert logs_collector.resolve_log_path(str(path)) == path
    assert logs_collector.resolve_log_path(str(tmp_path)) is None


def test_resolve_log_path_daily_suffix(tmp_path: Path) -> None:
    base = tmp_path / "dspace.log"
    today = _dt.date(2026, 9, 24)
    assert logs_collector.resolve_log_path(str(base), True, today) is None
    old = base.with_name("dspace.log.2026-09-20")
    older = base.with_name("dspace.log.2026-09-19")
    noise = base.with_name("dspace.log.bak")
    for p in (older, old, noise):
        p.write_text("x\n")
    os.utime(older, (1_000_000, 1_000_000))
    os.utime(old, (2_000_000, 2_000_000))
    os.utime(noise, (9_000_000, 9_000_000))
    assert logs_collector.resolve_log_path(str(base), True, today) == old  # newest *dated* sibling
    base.write_text("y\n")
    os.utime(base, (3_000_000, 3_000_000))
    assert logs_collector.resolve_log_path(str(base), True, today) == base  # plain file is newer
    todays = base.with_name("dspace.log.2026-09-24")
    todays.write_text("z\n")
    os.utime(todays, (10, 10))
    assert logs_collector.resolve_log_path(str(base), True, today) == todays  # today's always wins
    assert logs_collector.resolve_log_path(str(tmp_path / "missing" / "dspace.log"), True, today) is None


def test_resolve_log_path_follows_the_dspace_7_rotation(tmp_path: Path) -> None:
    """DSpace 7-10 write dspace.log and roll it daily to dspace.log-YYYY-MM-DD (log4j2, hyphen)."""
    base = tmp_path / "dspace.log"
    today = _dt.date(2026, 9, 26)
    rotated = base.with_name("dspace.log-2026-09-25")
    rotated.write_text("yesterday\n")
    os.utime(rotated, (1_000_000, 1_000_000))
    for noise in ("dspace.log-2026-09-24.gz", "dspace.log-old", "dspace-cli.log-2026-09-26"):
        base.with_name(noise).write_text("x\n")
        os.utime(base.with_name(noise), (9_000_000, 9_000_000))
    assert logs_collector.resolve_log_path(str(base), True, today) == rotated  # only a rolled file
    base.write_text("now\n")
    os.utime(base, (2_000_000, 2_000_000))
    assert logs_collector.resolve_log_path(str(base), True, today) == base  # the current file
    os.utime(base, (1_000_000, 1_000_000))  # rolled over within the same second
    assert logs_collector.resolve_log_path(str(base), True, today) == base
    # only dspace.log-<today> there: that file
    other = tmp_path / "other"
    other.mkdir()
    only = other / "dspace.log-2026-09-26"
    only.write_text("x\n")
    assert logs_collector.resolve_log_path(str(other / "dspace.log"), True, today) == only
    # DSpace 6 (dspace.log.YYYY-MM-DD, dot) keeps working next to the new names
    six = tmp_path / "six"
    six.mkdir()
    dotted = six / "dspace.log.2026-09-26"
    dotted.write_text("x\n")
    hyphen = six / "dspace.log-2026-09-25"
    hyphen.write_text("x\n")
    os.utime(dotted, (10, 10))
    assert logs_collector.resolve_log_path(str(six / "dspace.log"), True, today) == dotted
    # without daily_suffix only the plain path counts
    assert logs_collector.resolve_log_path(str(other / "dspace.log"), False, today) is None


async def test_logs_collector_reads_the_current_dspace_log(tmp_path: Path) -> None:
    """The default dspace entry (daily_suffix) counts dspace.log, not yesterday's rolled file."""
    now = time.time()
    current = tmp_path / "dspace.log"
    current.write_text("\n".join(dspace_lines(now, errors_recent=2)) + "\n")
    rolled = tmp_path / f"dspace.log-{_dt.date.fromtimestamp(now - 86400).isoformat()}"
    rolled.write_text("\n".join(dspace_lines(now - 86400, errors_recent=50)) + "\n")
    os.utime(rolled, (now - 86400, now - 86400))
    cfg = merge_patch(
        Config(),
        {
            "logs": {
                "files": [{"id": "dspace", "label": "DSpace", "path": str(current), "daily_suffix": True}]
            }
        },
    )
    ctx = make_ctx(cfg, now)
    try:
        result = await logs_collector.LogsCollector().collect(ctx)
    finally:
        await ctx.http.aclose()
    (rung,) = result.checks
    assert rung.detail["path"] == str(current) and rung.detail["errors"] == 2 and rung.status == "ok"


# --------------------------------------------------------------- collector: grading


def make_ctx(cfg: Config, now: float) -> CollectorContext:
    transport = httpx.MockTransport(lambda request: httpx.Response(404))
    return CollectorContext(
        cfg=cfg, http=httpx.AsyncClient(transport=transport), store=Store(":memory:"), state=State(), now=now
    )


async def test_logs_collector_grades_each_file(tmp_path: Path) -> None:
    now = time.time()
    dspace = tmp_path / "dspace.log"
    dspace.with_name(f"dspace.log.{_dt.date.today().isoformat()}").write_text(
        "\n".join(dspace_lines(now, errors_recent=25, errors_old=40, warnings=3)) + "\n"
    )
    tomcat = tmp_path / "catalina.out"
    tomcat.write_text(f"{stamp(now - 20, '%d-%b-%Y %H:%M:%S.000')} INFO [main] fine\n")
    solr = tmp_path / "solr.log"
    solr.write_text(
        "\n".join(
            f"{stamp(now - 30 - i, '%Y-%m-%d %H:%M:%S.000')} ERROR (x) [ y] boom {i}" for i in range(120)
        )
        + "\n"
    )
    cfg = merge_patch(
        Config(),
        {
            "modules": {"logs": True, "nginx": True},
            "logs": {
                "window_minutes": 5,
                "files": [
                    {"id": "dspace", "label": "DSpace", "path": str(dspace), "daily_suffix": True},
                    {"id": "tomcat", "label": "Tomcat", "path": str(tomcat)},
                    {"id": "solr", "label": "Solr", "path": str(solr)},
                    {"id": "nginx", "label": "nginx", "path": str(tmp_path / "error.log")},
                ],
            },
        },
    )
    ctx = make_ctx(cfg, now)
    result = await logs_collector.LogsCollector().collect(ctx)
    await ctx.http.aclose()
    assert [c.id for c in result.checks] == ["logs.dspace", "logs.tomcat", "logs.solr", "logs.nginx"]
    rungs = {c.id: c for c in result.checks}
    assert rungs["logs.dspace"].label == "DSpace log"
    assert rungs["logs.dspace"].status == "warn" and rungs["logs.dspace"].message == "25 errors in 5 min"
    assert rungs["logs.dspace"].value == 25.0 and rungs["logs.dspace"].detail["warnings"] == 3
    assert rungs["logs.dspace"].detail["path"].endswith(_dt.date.today().isoformat())
    assert rungs["logs.tomcat"].status == "ok" and rungs["logs.tomcat"].message == ""
    assert rungs["logs.solr"].status == "crit" and rungs["logs.solr"].message == "120 errors in 5 min"
    assert rungs["logs.nginx"].status == "unknown"
    assert rungs["logs.nginx"].message == f"file not found ({tmp_path / 'error.log'})"
    assert "hint" not in rungs["logs.nginx"].detail
    m = result.metrics
    assert m["window_minutes"] == 5 and m["total_errors"] == 145 and m["total_warnings"] == 3
    assert m["files"]["dspace"]["errors"] == 25 and m["files"]["dspace"]["last_error"]["line"].startswith(
        stamp(now - 60)[:10]
    )
    assert m["files"]["nginx"] == {
        "errors": None,
        "warnings": None,
        "last_error": None,
        "path": str(tmp_path / "error.log"),
        "label": "nginx",
        "error": "file not found",
    }
    assert m["files"]["tomcat"]["window_covered"] is True


async def collect_logs(files: list[dict[str, Any]], modules: dict[str, bool] | None = None) -> Any:
    """One run of the logs collector over ``files`` (with ``modules`` switched on or off)."""
    cfg = merge_patch(Config(), {"modules": {"logs": True, **(modules or {})}, "logs": {"files": files}})
    ctx = make_ctx(cfg, time.time())
    try:
        return await logs_collector.LogsCollector().collect(ctx)
    finally:
        await ctx.http.aclose()


def owner_group(path: Path) -> tuple[str, str]:
    """``(owner, group)`` of ``path`` by name (their ids when the databases have no name)."""
    st = path.stat()
    try:
        owner = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        owner = str(st.st_uid)
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        group = str(st.st_gid)
    return owner, group


@pytest.fixture
def not_in_the_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sentinel's user is in no group by the databases, and this process has none of them."""
    monkeypatch.setattr(logs_collector, "listed_in_group", lambda user, gid: False)
    monkeypatch.setattr(logs_collector, "process_has_group", lambda gid: False)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
async def test_logs_collector_permission_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, not_in_the_group: None
) -> None:
    """The message stays; the hint names the file's owner, group and mode and what to do."""
    secret = tmp_path / "secret.log"
    secret.write_text("2024-01-01 10:41:02,123 ERROR x\n")
    secret.chmod(0o040)  # its group may read it, its owner (this user) may not
    owner, group = owner_group(secret)
    user = logs_collector.sentinel_user()
    try:
        result = await collect_logs([{"id": "s", "label": "Secret", "path": str(secret)}])
        (rung,) = result.checks
        assert rung.status == "unknown" and rung.message == f"permission denied ({secret})"
        assert rung.detail["hint"] == (
            f"The file belongs to {owner}:{group} (mode 040): add {user} to the group {group}, "
            "log out and in again, then restart Sentinel."
        )
        # usermod -aG, then a restart from the same login: the database lists the user, the process lacks it
        monkeypatch.setattr(logs_collector, "listed_in_group", lambda user, gid: True)
        (rung,) = (await collect_logs([{"id": "s", "label": "Secret", "path": str(secret)}])).checks
        assert rung.detail["hint"] == (
            f"The user {user} was added to the group {group}, but this Sentinel process does not have it "
            "yet: log out, log in again, then restart Sentinel."
        )

        # the hint is advice: when it cannot be worked out the read still reports, with the general one
        def broken(user: str, gid: int) -> bool:
            raise RuntimeError("group database unavailable")

        monkeypatch.setattr(logs_collector, "listed_in_group", broken)
        (rung,) = (await collect_logs([{"id": "s", "label": "Secret", "path": str(secret)}])).checks
        assert rung.message == f"permission denied ({secret})"
        assert rung.detail["hint"] == logs_collector.PERMISSION_HINTS["general"].format(user=user)
    finally:
        secret.chmod(0o600)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
async def test_logs_collector_names_the_folder_it_may_not_enter(
    tmp_path: Path, not_in_the_group: None
) -> None:
    """A log behind a folder Sentinel may not enter (/var/log/tomcat10 is tomcat:adm 2750 on Ubuntu) is
    "permission denied", not a failed collector or "file not found", and the hint names that folder."""
    locked = tmp_path / "tomcat10"
    locked.mkdir()
    (locked / "catalina.out").write_text("2024-01-01 10:41:02,123 ERROR x\n")
    locked.chmod(0o650)  # its group may enter it, its owner (this user) may not
    owner, group = owner_group(locked)
    user = logs_collector.sentinel_user()
    files = [
        {"id": "tomcat", "label": "Tomcat", "path": str(locked / "catalina.out")},
        {"id": "daily", "label": "Daily", "path": str(locked / "dspace.log"), "daily_suffix": True},
        {"id": "gone", "label": "Gone", "path": str(tmp_path / "tomcat9" / "catalina.out")},
    ]
    try:
        result = await collect_logs(files)
    finally:
        locked.chmod(0o755)
    rungs = {c.id: c for c in result.checks}
    hint = (
        f"The folder {locked} belongs to {owner}:{group} (mode 650): add {user} to the group {group}, "
        "log out and in again, then restart Sentinel."
    )
    for rung_id, name in (("logs.tomcat", "catalina.out"), ("logs.daily", "dspace.log")):
        rung = rungs[rung_id]
        assert rung.status == "unknown" and rung.message == f"permission denied ({locked / name})"
        assert rung.detail["hint"] == hint and "informational" not in rung.detail
    # a folder that is not there at all: the file is simply not found
    gone = rungs["logs.gone"]
    assert gone.message == f"file not found ({tmp_path / 'tomcat9' / 'catalina.out'})"
    assert gone.detail["informational"] is True and "hint" not in gone.detail


def test_permission_hint_texts() -> None:
    """What stat and the user and group databases say decides the advice (the owner's test server:
    dspace.log is tomcat:tomcat 640, Sentinel runs as the login user)."""
    hint = logs_collector.access_hint
    who = ("tomcat", "tomcat", "dspace-admin")
    assert hint("The file", 0o100640, *who, in_group=False, has_group=False) == (
        "The file belongs to tomcat:tomcat (mode 640): add dspace-admin to the group tomcat, log out and "
        "in again, then restart Sentinel."
    )
    assert hint("The file", 0o100640, *who, in_group=True, has_group=False) == (
        "The user dspace-admin was added to the group tomcat, but this Sentinel process does not have it "
        "yet: log out, log in again, then restart Sentinel."
    )
    # every text starts with a capitalised word ({subject} is "The file" or "The folder …"), never with
    # a login name: the ladder writes a hint's first letter in capitals, and "Dspace-admin" is another user
    for name, text in logs_collector.PERMISSION_HINTS.items():
        assert text[0].isupper() or text.startswith("{subject} "), name
    # the group may not read it: joining the group alone would not help
    assert hint("The file", 0o100600, *who, in_group=True, has_group=False) == (
        "The file belongs to tomcat:tomcat (mode 600), so only tomcat can read it: make it readable by "
        "the group tomcat, add dspace-admin to that group, log out and in again, then restart Sentinel."
    )
    folder = "The folder /var/log/tomcat10"
    assert hint(
        folder, 0o42750, "tomcat", "adm", "dspace-admin", in_group=False, has_group=False, folder=True
    ) == (
        "The folder /var/log/tomcat10 belongs to tomcat:adm (mode 2750): add dspace-admin to the group "
        "adm, log out and in again, then restart Sentinel."
    )
    # the process has the group, or everyone may read it: something else stops it
    general = (
        "Sentinel runs as dspace-admin, who may not read this file: give dspace-admin read access "
        "(usually by adding dspace-admin to the file's group), log out and in again, then restart Sentinel."
    )
    assert hint("The file", 0o100640, *who, in_group=True, has_group=True) == general
    assert hint("The file", 0o100644, *who, in_group=False, has_group=False) == general


async def test_logs_leave_out_the_nginx_log_while_the_nginx_module_is_off(tmp_path: Path) -> None:
    """A server without nginx has the nginx module off: its log gets no row and no rung."""
    files = [
        {"id": "dspace", "label": "DSpace", "path": str(tmp_path / "dspace.log")},
        {"id": "nginx", "label": "nginx", "path": str(tmp_path / "error.log")},
    ]
    off = await collect_logs(files, {"nginx": False})
    assert [c.id for c in off.checks] == ["logs.dspace"] and list(off.metrics["files"]) == ["dspace"]
    on = await collect_logs(files, {"nginx": True})
    assert [c.id for c in on.checks] == ["logs.dspace", "logs.nginx"]
    assert list(on.metrics["files"]) == ["dspace", "nginx"]


# ------------------------------------------------------------------------ time zones


@pytest.fixture
def host_zone() -> Any:
    """Switch the process's local time zone (``TZ`` + ``tzset``) for one test, then restore it."""
    saved = os.environ.get("TZ")

    def set_zone(name: str) -> None:
        os.environ["TZ"] = name
        time.tzset()

    yield set_zone
    if saved is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = saved
    time.tzset()


def utc_stamp(ts: float) -> str:
    """How Solr stamps its lines with its default ``-Duser.timezone=UTC``."""
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S.000")


def solr_utc_log(path: Path, now: float) -> None:
    """150 ERROR lines from 3 h to 30 min ago plus one a minute ago, all stamped in UTC."""
    lines = [f"{utc_stamp(now - 10800 + i * 60)} ERROR (qtp-1) [ x:search] OldError {i}" for i in range(150)]
    lines.append(f"{utc_stamp(now - 60)} ERROR (qtp-1) [ x:search] o.a.s.RecentError")
    path.write_text("\n".join(lines) + "\n")
    os.utime(path, (now - 60, now - 60))  # written when its newest line was


@pytest.mark.parametrize("zone", ["UTC", "Asia/Colombo", "America/New_York"])
def test_solr_errors_in_utc_are_counted_right_in_every_host_zone(
    tmp_path: Path, host_zone: Any, zone: str
) -> None:
    host_zone(zone)
    now = time.time()
    path = tmp_path / "solr.log"
    solr_utc_log(path, now)
    since = now - 300
    for timezone in ("UTC", ""):  # configured, and detected from the file's modification time
        entry = LogFileConfig(id="solr", label="Solr", path=str(path), timezone=timezone)
        read = logs_collector.summarize_log(entry, since, now=now)
        assert read.summary is not None and read.summary.errors == 1, (zone, timezone)


def test_lines_stamped_in_the_future_are_not_counted(host_zone: Any) -> None:
    host_zone("America/New_York")
    now = time.time()
    # UTC stamps read as New York time land hours in the future: never "in the last 5 minutes"
    lines = [f"{utc_stamp(now - 600 + i)} ERROR old {i}" for i in range(50)]
    assert logs.count_errors(lines, now - 300, now=now).errors == 0
    assert logs.count_errors(lines, now - 300, tz=_dt.timezone.utc, now=now).errors == 0
    fresh = [f"{utc_stamp(now - 30)} ERROR fresh"]
    assert logs.count_errors(fresh, now - 300, tz=_dt.timezone.utc, now=now).errors == 1


def test_parse_timestamp_in_a_given_zone() -> None:
    line = "2026-09-24 10:41:02.500 ERROR (qtp-1) [ x:search] boom"
    utc = _dt.datetime(2026, 9, 24, 10, 41, 2, 500000, tzinfo=_dt.timezone.utc).timestamp()
    assert logs.parse_timestamp(line, _dt.timezone.utc) == utc
    colombo = logs.zone_for("Asia/Colombo")
    assert logs.parse_timestamp(line, colombo) == utc - 5.5 * 3600
    nginx = "2026/09/24 10:41:02 [error] 1183#1183: x"
    assert logs.parse_timestamp(nginx, _dt.timezone.utc) == utc - 0.5
    assert logs.zone_for("") is None and logs.zone_for("local") is None and logs.zone_for("auto") is None
    assert logs.zone_for("utc") is _dt.timezone.utc and logs.zone_for("UTC") is _dt.timezone.utc
    with pytest.raises(ValueError, match="unknown time zone"):
        logs.zone_for("Mars/Olympus_Mons")


def test_log_file_timezone_setting() -> None:
    # "" detects the zone per file: Solr's UTC log is found out, a SOLR_TIMEZONE=local one too
    files = {f.id: f.timezone for f in Config().logs.files}
    assert files == {"dspace": "", "tomcat": "", "solr": "", "nginx": ""}
    assert LogFileConfig(id="x", path="/x", timezone=" Asia/Colombo ").timezone == "Asia/Colombo"
    with pytest.raises(ValidationError, match="unknown time zone"):
        LogFileConfig(id="x", path="/x", timezone="Mars/Olympus_Mons")
