"""CLI tests (SPEC section 14): doctor box and exit codes, report / telegram fallbacks, the
systemd unit renderer and installer, pid-file process management, update, and ``sentinel.sh``."""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from dspace_sentinel import __version__, cli, paths
from dspace_sentinel.auth import TOO_SHORT, USERNAME_RULE, hash_password, verify_password
from dspace_sentinel.models import Report

REPO = paths.REPO_DIR
SENTINEL_SH = REPO / "sentinel.sh"


# ---------------------------------------------------------------------------- fixtures


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Private data dir, config path and HOME; no system unit; pid-file mode."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SENTINEL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SENTINEL_CONFIG", str(tmp_path / "config" / "sentinel.yaml"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(cli, "SYSTEM_UNIT_PATH", tmp_path / "etc" / "dspace-sentinel.service")
    return tmp_path


@pytest.fixture
def fake_systemctl(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``systemctl``/``loginctl``/``journalctl`` stand-ins on PATH that log their arguments.

    The unit is inactive until ``start``/``restart``/``enable --now``, which lead to
    ``FAKE_SYSTEMCTL_STATE`` (default ``active``); ``FAKE_SYSTEMCTL_FAIL=<verb>`` makes that verb fail.
    """
    bin_dir = isolated / "bin"
    bin_dir.mkdir()
    log = isolated / "systemctl.log"
    state = isolated / "systemctl.state"
    script = (
        "#!/bin/sh\n"
        f'echo "$(basename "$0") $*" >> "{log}"\n'
        'if [ -n "$FAKE_SYSTEMCTL_FAIL" ]; then case " $* " in *" $FAKE_SYSTEMCTL_FAIL "*)\n'
        '  echo "Failed to $FAKE_SYSTEMCTL_FAIL unit" >&2; exit 1 ;; esac; fi\n'
        'case " $* " in\n'
        f'  *" is-active "*) cat "{state}" 2>/dev/null || echo inactive ;;\n'
        '  *" show "*) echo 4242 ;;\n'
        f'  *" disable "*|*" stop "*) echo inactive > "{state}" ;;\n'
        f'  *" start "*|*" restart "*|*" --now "*) echo "${{FAKE_SYSTEMCTL_STATE:-active}}" > "{state}" ;;\n'
        "esac\n"
        "exit 0\n"
    )
    for name in ("systemctl", "loginctl", "journalctl"):
        path = bin_dir / name
        path.write_text(script)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(cli, "UNIT_START_TIMEOUT", 0.3, raising=False)
    return log


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _status_fixture(demo: bool = True) -> dict:
    """A hand-built ``/api/status`` payload exercising every branch of the doctor box."""
    now = 1_800_000_000.0
    server = {
        "ts": now,
        "cpu_pct": 18.2,
        "mem_pct": 91.0,
        "disk_pct": 48.0,
        "swap_pct": 2.0,
        "load1": 0.83,
        "cores": 4,
        "mem_used": 10.2 * 1024**3,
        "mem_total": 16 * 1024**3,
        "net_rx_bps": 172_000.0,
        "net_tx_bps": 1_500_000.0,
        "inode_pct": 12.0,
        "os_name": "Ubuntu",
        "os_version": "24.04",
        "kernel": "6.8.0-45-generic",
        "uptime_s": 17 * 86400 + 3 * 3600,
        "disk_path": "/",
        "disks": [
            {"path": "/dspace", "total": 1024**4, "used": 420 * 1024**3, "free": 600 * 1024**3, "pct": 41.0}
        ],
        "per_metric": {
            "cpu": "ok",
            "memory": "warn",
            "disk": "ok",
            "load": "ok",
            "swap": "ok",
            "disk:/dspace": "ok",
        },
        "status": "warn",
    }
    postgres = {
        "id": "postgres",
        "label": "PostgreSQL",
        "emoji": "🐘",
        "status": "crit",
        "reason": "connection refused (localhost:5432)",
        "chain": [
            {
                "id": "postgres.service",
                "label": "systemd postgresql",
                "status": "ok",
                "ts": now,
                "latency_ms": None,
                "detail": {},
                "message": "active",
            },
            {
                "id": "postgres.tcp",
                "label": "TCP :5432",
                "status": "ok",
                "ts": now,
                "latency_ms": 0.4,
                "detail": {},
                "message": "",
            },
            {
                "id": "postgres.connect",
                "label": "Connect as dspace",
                "status": "crit",
                "ts": now,
                "latency_ms": None,
                "detail": {},
                "message": "connection refused (localhost:5432)",
            },
            {
                "id": "postgres.query",
                "label": "SELECT 1",
                "status": "unknown",
                "ts": now,
                "latency_ms": None,
                "detail": {},
                "message": "not reached",
            },
        ],
        "metrics": {},
        "depends_on": [],
        "latency_ms": None,
        "since": now - 300,
        "checked_at": now,
    }
    backend = {
        "id": "backend",
        "label": "DSpace Backend",
        "emoji": "☕",
        "status": "ok",
        "reason": None,
        "chain": [
            {
                "id": "backend.service",
                "label": "systemd tomcat9",
                "status": "ok",
                "ts": now,
                "latency_ms": None,
                "detail": {},
                "message": "active",
            },
            {
                "id": "backend.tcp",
                "label": "TCP :8080",
                "status": "ok",
                "ts": now,
                "latency_ms": 0.6,
                "detail": {},
                "message": "",
            },
            {
                "id": "backend.rest",
                "label": "GET /server/api",
                "status": "ok",
                "ts": now,
                "latency_ms": 91.0,
                "detail": {"status_code": 200},
                "message": "",
            },
            {
                "id": "backend.dep.postgres",
                "label": "Database dependency",
                "status": "warn",
                "ts": now,
                "latency_ms": None,
                "detail": {},
                "message": "PostgreSQL is crit",
            },
        ],
        "metrics": {"dspace_version": "8.1", "dspace_name": "Demo Repository"},
        "depends_on": ["postgres", "solr"],
        "latency_ms": 91.0,
        "since": now - 9000,
        "checked_at": now,
    }
    solr = {
        "id": "solr",
        "label": "Solr",
        "emoji": "☀️",
        "status": "ok",
        "reason": None,
        "chain": [
            {
                "id": "solr.service",
                "label": "systemd solr",
                "status": "ok",
                "ts": now,
                "latency_ms": None,
                "detail": {},
                "message": "",
            },
            {
                "id": "solr.tcp",
                "label": "TCP :8983",
                "status": "ok",
                "ts": now,
                "latency_ms": 0.3,
                "detail": {},
                "message": "",
            },
            {
                "id": "solr.admin",
                "label": "Admin cores",
                "status": "ok",
                "ts": now,
                "latency_ms": 12.0,
                "detail": {},
                "message": "",
            },
            {
                "id": "solr.core.search",
                "label": "Core search",
                "status": "ok",
                "ts": now,
                "latency_ms": 3.0,
                "detail": {},
                "message": "",
            },
            {
                "id": "solr.core.oai",
                "label": "Core oai",
                "status": "ok",
                "ts": now,
                "latency_ms": 3.0,
                "detail": {},
                "message": "",
            },
            {
                "id": "solr.search",
                "label": "Search query",
                "status": "ok",
                "ts": now,
                "latency_ms": 28.0,
                "detail": {},
                "message": "",
            },
        ],
        "metrics": {"cores": [{"name": "search"}, {"name": "oai"}], "search_docs": 27491, "version": "9.6.1"},
        "depends_on": [],
        "latency_ms": 28.0,
        "since": now - 9000,
        "checked_at": now,
    }
    ports = {
        "id": "ports",
        "label": "Ports",
        "emoji": "🔌",
        "status": "warn",
        "reason": "exposed publicly",
        "chain": [
            {
                "id": "ports.22",
                "label": "SSH :22",
                "status": "ok",
                "ts": now,
                "latency_ms": None,
                "detail": {"expect": "open"},
                "message": "listening publicly",
            },
            {
                "id": "ports.8983",
                "label": "Solr :8983",
                "status": "warn",
                "ts": now,
                "latency_ms": None,
                "detail": {"expect": "local_only"},
                "message": "exposed publicly",
            },
            {
                "id": "ports.5432",
                "label": "PostgreSQL :5432",
                "status": "ok",
                "ts": now,
                "latency_ms": None,
                "detail": {"expect": "local_only"},
                "message": "listening on localhost only",
            },
        ],
        "metrics": {"listening": []},
        "depends_on": [],
        "latency_ms": None,
        "since": now,
        "checked_at": now,
    }
    nginx = {
        "id": "nginx",
        "label": "nginx",
        "emoji": "🌐",
        "status": "crit",
        "reason": "HTTP 502 (http://localhost)",
        "chain": [
            {
                "id": "nginx.service",
                "label": "systemd nginx",
                "status": "ok",
                "ts": now,
                "latency_ms": None,
                "detail": {},
                "message": "active",
            },
            {
                "id": "nginx.http",
                "label": "HTTP",
                "status": "crit",
                "ts": now,
                "latency_ms": 4.0,
                "detail": {},
                "message": "HTTP 502 (http://localhost)",
            },
        ],
        "metrics": {"server_header": "nginx/1.24.0"},
        "depends_on": ["frontend"],
        "latency_ms": 4.0,
        "since": now,
        "checked_at": now,
    }
    assetstore = {
        "id": "assetstore",
        "label": "Assetstore",
        "emoji": "🗄️",
        "status": "ok",
        "reason": None,
        "chain": [
            {
                "id": "assetstore.path",
                "label": "Assetstore readable",
                "status": "ok",
                "ts": now,
                "latency_ms": 0.05,
                "detail": {},
                "message": "",
            }
        ],
        "metrics": {"path": "/dspace/assetstore", "total": 1024**4, "free": 600 * 1024**3, "pct": 41.0},
        "depends_on": [],
        "latency_ms": 0.05,
        "since": now,
        "checked_at": now,
    }
    alert = {
        "id": 7,
        "rule": "component_down:postgres",
        "component": "postgres",
        "severity": "critical",
        "status": "open",
        "title": "PostgreSQL unreachable",
        "message": "service ✅ → tcp ✅ → connect ❌",
        "opened_ts": now - 372,
        "acked_ts": None,
    }
    return {
        "ts": now,
        "mood": "crit",
        "headline": "Critical: PostgreSQL unreachable",
        "hostname": "server-1",
        "demo": demo,
        "checked_at": now - 8,
        "server": server,
        "components": [postgres, solr, backend, nginx, ports, assetstore],
        "alerts_open": [alert],
    }


# ------------------------------------------------------------------------- doctor box


def test_display_width_counts_cells() -> None:
    assert cli.display_width("hello") == 5
    assert cli.display_width("✅") == 2
    assert cli.display_width("⚠️") == 2  # emoji presentation sequence
    assert cli.display_width("—") == 1
    assert cli.display_width("") == 0


def test_box_rows_keep_the_border_aligned() -> None:
    box = cli.Box(60)
    box.top()
    box.row("✅ short", right="right")
    box.row("x" * 200)
    box.items([f"item {i} ✅" for i in range(12)])
    box.section("SERVER")
    box.bottom()
    lines = box.render().splitlines()
    assert {cli.display_width(line) for line in lines} == {60}
    assert lines[2].endswith("… │") or "…" in lines[2]


@pytest.mark.parametrize("ascii_only", [False, True])
def test_render_doctor_readme_style(ascii_only: bool) -> None:
    from dspace_sentinel.config import Config

    text = cli.render_doctor(_status_fixture(), Config(), ascii_only=ascii_only)
    lines = text.splitlines()
    widths = {cli.display_width(line) for line in lines}
    assert widths == {cli.BOX_WIDTH}, widths
    for title in ("SERVER", "DSPACE STACK", "NETWORK", "ALERTS"):
        assert title in text
    assert "server-1" in text and "demo data" in text
    assert "200 / 91 ms" in text  # HTTP code / latency on the backend headline
    assert "8983 Solr LOCAL ONLY" in text and "5432 PostgreSQL LOCAL ONLY" in text
    assert "connection refused (localhost:5432)" in text
    assert "cores 2/2" in text  # Solr core rungs collapsed
    sep = "-" if ascii_only else "·"
    assert f"PostgreSQL unreachable {sep} postgres {sep} since 6 m ago" in text
    assert "Memory 91 %" in text
    if ascii_only:
        assert "✅" not in text and "┌" not in text
        assert lines[0].startswith("+--")
        assert "XX PostgreSQL" in text and "!! Memory" in text
    else:
        assert "❌ PostgreSQL" in text and "⚠️ Memory" in text and "✅ Solr" in text
        assert lines[0].startswith("┌─") and lines[-1].startswith("└─")
        assert "service ✅ → TCP ✅ → connect ❌ → query —" in text
    # sub-millisecond path check shows no latency; nginx shows the HTTP code from the message
    assetstore_line = next(line for line in lines if "Assetstore" in line)
    assert " ms" not in assetstore_line
    assert "502 / 4 ms" in text


@pytest.mark.parametrize("ascii_only", [False, True])
def test_render_doctor_keeps_the_box_for_multi_line_alert_messages(ascii_only: bool) -> None:
    """Component alerts carry the chain on one line and the reason on the next (Telegram format)."""
    from dspace_sentinel.config import Config

    status = _status_fixture()
    message = "service ✅ → tcp ✅ → connect ❌\nconnection refused (localhost:5432)"
    status["alerts_open"][0]["message"] = message
    lines = cli.render_doctor(status, Config(), ascii_only=ascii_only).splitlines()
    border = "|" if ascii_only else "│"
    body = lines[1:-1]
    for line in body:
        if line[0] in "+├":  # section rules
            continue
        assert line.startswith(border) and line.endswith(border), line
    assert {cli.display_width(line) for line in lines} == {cli.BOX_WIDTH}
    assert any("connection refused (localhost:5432)" in line for line in body)


@pytest.mark.parametrize("ascii_only", [False, True])
def test_render_doctor_shows_a_security_warning_after_the_ports(ascii_only: bool) -> None:
    """A Security warning raises the headline, so the NETWORK section has a row that says why."""
    from dspace_sentinel.config import Config

    now = 1_800_000_000.0
    reason = "1 security update pending"
    names = ("ports", "firewall", "ssh", "web", "updates", "software", "files", "sentinel")
    chain = [
        {
            "id": f"security.{name}",
            "label": name,
            "status": "warn" if name == "updates" else "ok",
            "ts": now,
            "latency_ms": None,
            "detail": {},
            "message": reason if name == "updates" else "",
        }
        for name in names
    ]
    status = _status_fixture()
    status["components"].append(
        {
            "id": "security",
            "label": "Security",
            "emoji": "🛡️",
            "status": "warn",
            "reason": reason,
            "chain": chain,
            "metrics": {},
            "depends_on": [],
            "latency_ms": None,
            "since": now - 60,
            "checked_at": now,
        }
    )
    lines = cli.render_doctor(status, Config(), ascii_only=ascii_only).splitlines()
    assert {cli.display_width(line) for line in lines} == {cli.BOX_WIDTH}
    warn, ok, sep, border = ("!!", "OK", "-", " |") if ascii_only else ("⚠️", "✅", "·", " │")
    head = next(i for i, line in enumerate(lines) if f"{warn} Security" in line)
    assert head > max(i for i, line in enumerate(lines) if "LOCAL ONLY" in line)  # after the Ports rows
    assert lines[head].removesuffix(border).rstrip().endswith(reason)
    marks = [f"{name} {warn if name == 'updates' else ok}" for name in names]
    assert f" {sep} ".join(marks[:6]) in lines[head + 1]
    assert f" {sep} ".join(marks[6:]) in lines[head + 2]
    assert "ALERTS" in lines[head + 3]


def test_render_doctor_without_modules() -> None:
    from dspace_sentinel.config import Config

    status = _status_fixture(demo=False)
    status["components"] = []
    status["alerts_open"] = []
    text = cli.render_doctor(status, Config(), ascii_only=False)
    assert "no DSpace modules enabled" in text
    assert "nginx / SSL / ports / security modules are off" in text
    assert "No open alerts" in text
    assert "live" in text


def test_exit_code_for() -> None:
    assert cli.exit_code_for("ok") == 0
    assert cli.exit_code_for("warn") == 1
    assert cli.exit_code_for("unknown") == 1
    assert cli.exit_code_for("crit") == 2


def test_doctor_demo_prints_box_and_json(isolated: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["doctor", "--demo"])
    out = capsys.readouterr().out
    assert rc in (0, 1, 2)
    assert "DSPACE STACK" in out and "demo-dspace" in out and "LOCAL ONLY" in out
    rc = cli.main(["doctor", "--demo", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc in (0, 1, 2)
    assert payload["demo"] is True and {c["id"] for c in payload["components"]} >= {
        "postgres",
        "solr",
        "backend",
    }


def test_doctor_demo_shows_the_security_row(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The demo's Security component is calm (no security update) and has its row after Ports."""
    _forget_after_test(monkeypatch, "SENTINEL_DEMO")
    cli.main(["doctor", "--demo", "--json"])
    payload = json.loads(capsys.readouterr().out)
    security = next(c for c in payload["components"] if c["id"] == "security")
    assert (security["status"], security["reason"]) == ("ok", None)
    updates = next(r for r in security["chain"] if r["id"] == "security.updates")
    assert updates["message"] == "no pending security updates (3 other updates)"
    cli.main(["doctor", "--demo", "--ascii"])
    lines = capsys.readouterr().out.splitlines()
    head = next(i for i, line in enumerate(lines) if line.startswith("| OK Security "))
    assert next(i for i, line in enumerate(lines) if line.startswith("| OK Ports ")) < head
    checks = "ports OK - firewall OK - ssh OK - web OK - updates OK - software OK "
    assert lines[head + 1].startswith(f"|    {checks}")
    assert lines[head + 2].startswith("|    files OK - sentinel OK ")
    assert lines[head + 3].startswith("+- ALERTS ")


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["version"]) == 0
    assert capsys.readouterr().out.strip() == f"dspace-sentinel {__version__}"


@pytest.mark.parametrize("argv", [["version", "-v"], ["version", "--verbose"], ["-v", "version"]])
def test_version_verbose(isolated: Path, argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    """``./sentinel.sh version -v`` (documented) works as well as the global ``-v version``."""
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    for label in ("python", "repo", "data", "config", "service"):
        assert re.search(rf"^{label}\s", out, re.M), out


def test_demo_flag_is_documented_for_every_command_that_takes_it(capsys: pytest.CaptureFixture[str]) -> None:
    for command in ("start", "stop", "restart", "status", "logs"):
        with pytest.raises(SystemExit):
            cli.main([command, "--help"])
        assert "--demo" in capsys.readouterr().out, command


def test_plain_stop_and_status_point_at_a_background_demo(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``start --demo`` keeps its pid in data/demo; plain stop/status must not hide it."""
    monkeypatch.setattr(paths, "REPO_DIR", isolated / "repo")
    monkeypatch.delenv("SENTINEL_DATA_DIR")  # the demo only has its own data dir by default
    demo_dir = isolated / "repo" / "data" / "demo"
    demo_dir.mkdir(parents=True)
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "dspace_sentinel"])
    try:
        (demo_dir / "sentinel.pid").write_text(f"{sleeper.pid}\n")
        assert cli.main(["stop"]) == 0
        out = capsys.readouterr().out
        assert "Not running" in out and f"pid {sleeper.pid}" in out and "stop --demo" in out
        assert sleeper.poll() is None  # plain stop leaves the demo alone
        assert cli.main(["status"]) == cli.EXIT_WARN
        assert "stop --demo" in capsys.readouterr().out
        assert cli.main(["stop", "--demo"]) == 0
        assert f"Stopped (pid {sleeper.pid})" in capsys.readouterr().out
    finally:
        sleeper.kill()
        sleeper.wait()


def test_usage_errors_exit_64_not_a_health_code(capsys: pytest.CaptureFixture[str]) -> None:
    """0/1/2 are health results (doctor); a mistyped command or flag must not look like one."""
    for argv in (["frobnicate"], ["doctor", "--jsn"], [], ["report", "--range"]):
        with pytest.raises(SystemExit) as exc:
            cli.main(argv)
        assert exc.value.code == cli.EXIT_USAGE == 64, argv
        assert "error:" in capsys.readouterr().err


def test_an_unexpected_failure_exits_4_not_a_health_code(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(cfg: object) -> dict:
        raise RuntimeError("collector exploded")

    monkeypatch.setattr(cli, "_one_shot_status", boom)
    assert cli.main(["doctor"]) == cli.EXIT_ERROR
    err = capsys.readouterr().err
    assert "RuntimeError: collector exploded" in err


@pytest.mark.parametrize("argv", [["status"], ["-v", "version"], ["logs", "-n", "5"]])
def test_output_into_a_closed_pipe_keeps_the_command_result(isolated: Path, argv: list[str]) -> None:
    """``./sentinel.sh status | head -1``: the reader leaving early is not an error."""
    log_file = paths.log_path(paths.ensure_dirs(paths.data_dir()))
    log_file.write_text("".join(f"line {i}\n" for i in range(50)))
    read_end, write_end = os.pipe()
    os.close(read_end)  # the reader is gone before anything is written
    try:
        res = subprocess.run(
            [sys.executable, "-m", "dspace_sentinel", *argv],
            stdout=write_end,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(REPO),
            env=dict(os.environ),
            timeout=60,
        )
    finally:
        os.close(write_end)
    assert "BrokenPipe" not in res.stderr and "Exception ignored" not in res.stderr, res.stderr
    assert res.returncode == (cli.EXIT_WARN if argv == ["status"] else cli.EXIT_OK)


def test_python_m_with_a_broken_virtualenv_exits_4(tmp_path: Path) -> None:
    """A half-installed .venv (missing dependency) must not exit 1, the 'warnings' code."""
    blocker = tmp_path / "blocker"
    blocker.mkdir()
    (blocker / "yaml.py").write_text("raise ImportError('No module named yaml (simulated)')\n")
    env = {**os.environ, "PYTHONPATH": f"{blocker}{os.pathsep}{REPO}"}
    res = subprocess.run(
        [sys.executable, "-m", "dspace_sentinel", "doctor"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=env,
        timeout=60,
    )
    assert res.returncode == cli.EXIT_ERROR, res.stderr
    assert "./sentinel.sh install" in res.stderr


# ----------------------------------------------------------------------------- report


def _report(path: str, fmt: str = "html") -> Report:
    return Report(
        id=1, created_ts=time.time(), title="T", range_from=0, range_to=1, fmt=fmt, path=path, size=1234
    )


def test_report_when_module_not_built(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from dspace_sentinel.reports import generator

    def not_yet(*_a: object, **_k: object) -> Report:
        raise NotImplementedError("reports module not built yet")

    monkeypatch.setattr(generator, "generate_for_range", not_yet, raising=False)
    assert cli.main(["report"]) == cli.EXIT_NOT_IMPLEMENTED
    assert "not available in this build" in capsys.readouterr().out


def test_report_uses_generate_for_range(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from dspace_sentinel.reports import generator

    seen: dict = {}

    def fake(
        store: object,
        cfg: object,
        range_str: str,
        fmt: str,
        title: str | None = None,
        send_telegram: bool = False,
    ) -> Report:
        seen.update(range_str=range_str, fmt=fmt, title=title, send_telegram=send_telegram)
        return _report("/tmp/sentinel-report.md", fmt)

    delivered: list[Any] = []

    def fake_deliver(cfg: object, report: Report, store: object = None) -> dict:
        delivered.append(report)
        return {"ok": True, "error": None}

    monkeypatch.setattr(generator, "generate_for_range", fake, raising=False)
    monkeypatch.setattr(generator, "deliver_telegram", fake_deliver)
    rc = cli.main(["report", "--range", "7d", "--format", "md", "--title", "Weekly", "--telegram"])
    out = capsys.readouterr().out
    assert rc == 0
    # the CLI delivers the report itself so it can print what actually happened
    assert seen == {"range_str": "7d", "fmt": "md", "title": "Weekly", "send_telegram": False}
    assert len(delivered) == 1 and delivered[0].path == "/tmp/sentinel-report.md"
    assert "Report ready: /tmp/sentinel-report.md" in out and "1.2 KB" in out
    assert "Telegram: sent" in out


@pytest.mark.parametrize(
    ("delivery", "rc", "text"),
    [
        (
            {"ok": False, "error": "Telegram is not enabled", "skipped": True},
            4,
            "not sent: Telegram is not enabled",
        ),
        ({"ok": False, "error": "401 Unauthorized"}, 4, "not sent: 401 Unauthorized"),
        ({"ok": True, "error": None, "skipped": "demo"}, 0, "demo mode: logged, not sent"),
    ],
)
def test_report_telegram_prints_the_real_outcome(
    isolated: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    delivery: dict,
    rc: int,
    text: str,
) -> None:
    """'sent to Telegram (when the notifier is configured)' hid a failed or skipped delivery."""
    from dspace_sentinel.reports import generator

    fake_report = _report("/tmp/r.html")
    monkeypatch.setattr(generator, "generate_for_range", lambda *a, **k: fake_report, raising=False)
    monkeypatch.setattr(generator, "deliver_telegram", lambda cfg, report, store=None: delivery)
    assert cli.main(["report", "--telegram"]) == rc
    captured = capsys.readouterr()
    assert "Report ready: /tmp/r.html" in captured.out
    assert f"Telegram: {text}" in captured.out + captured.err
    assert cli.main(["report", "--telegram", "--json"]) == rc
    assert json.loads(capsys.readouterr().out)["telegram"] == delivery


def test_report_falls_back_to_generate(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from dspace_sentinel.reports import generator

    seen: dict = {}

    def fake_generate(
        store: object,
        cfg: object,
        t_from: float,
        t_to: float,
        fmt: str = "html",
        title: str | None = None,
        sections: list | None = None,
        send_telegram: bool = False,
    ) -> Report:
        seen.update(span=round(t_to - t_from), fmt=fmt)
        return _report("/tmp/r.json", fmt)

    monkeypatch.delattr(generator, "generate_for_range", raising=False)
    monkeypatch.setattr(generator, "generate", fake_generate)
    assert cli.main(["report", "--range", "30d", "--format", "json", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert seen == {"span": 30 * 86400, "fmt": "json"} and payload["path"] == "/tmp/r.json"


def test_report_rejects_bad_range_and_format(isolated: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["report", "--range", "2y"]) == cli.EXIT_ERROR
    assert "--range must be one of" in capsys.readouterr().err
    assert cli.main(["report", "--format", "pdf"]) == cli.EXIT_ERROR
    assert "--format must be one of" in capsys.readouterr().err


# ------------------------------------------------------------------------ telegram-test


def _install_fake_telegram(monkeypatch: pytest.MonkeyPatch, send_test: object) -> None:
    pkg = types.ModuleType("dspace_sentinel.alerts.notifiers")
    mod = types.ModuleType("dspace_sentinel.alerts.notifiers.telegram")
    mod.send_test = send_test  # type: ignore[attr-defined]
    pkg.telegram = mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "dspace_sentinel.alerts.notifiers", pkg)
    monkeypatch.setitem(sys.modules, "dspace_sentinel.alerts.notifiers.telegram", mod)


def test_telegram_test_without_notifier_module(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(sys.modules, "dspace_sentinel.alerts.notifiers", None)
    monkeypatch.setitem(sys.modules, "dspace_sentinel.alerts.notifiers.telegram", None)
    assert cli.main(["telegram-test"]) == cli.EXIT_NOT_IMPLEMENTED
    assert "not part of this build" in capsys.readouterr().out


def test_telegram_test_not_configured(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_fake_telegram(monkeypatch, lambda cfg: {"ok": True})
    assert cli.main(["telegram-test"]) == cli.EXIT_ERROR
    assert "not configured" in capsys.readouterr().err


# made-up tokens, split in two so that secret scanners do not report them
CLI_BOT = "123456789:" + "AAH-cli-test-token-00000000000000001"
CLI_BOT_B = "987654321:" + "AAH-cli-test-token-00000000000000002"


def _result(label: str, chat_id: str, bot: str | None, ok: bool, error: str | None = None) -> dict[str, Any]:
    return {
        "id": f"id-{chat_id}",
        "label": label,
        "chat_id": chat_id,
        "bot_name": bot,
        "ok": ok,
        "error": error,
    }


def test_telegram_test_sync_and_async(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SENTINEL_TELEGRAM_TOKEN", CLI_BOT)
    monkeypatch.setenv("SENTINEL_TELEGRAM_CHAT_ID", "42")

    async def async_ok(cfg: Any) -> dict:
        # telegram.get_me already puts the "@" in front of the username
        (bot,) = cfg.notifiers.telegram.chats  # the environment's bot
        result = _result(bot.label, bot.chat_id, "@sentinel_bot", True)
        return {"ok": True, "bot_name": "@sentinel_bot", "error": None, "results": [result]}

    _install_fake_telegram(monkeypatch, async_ok)
    assert cli.main(["telegram-test"]) == 0
    out = capsys.readouterr().out
    assert "✅ From environment · @sentinel_bot · delivered" in out
    assert "Connected to Telegram as @sentinel_bot —" in out and "@@" not in out

    # a bot without a username is shown by its first name, without an "@"
    _install_fake_telegram(monkeypatch, lambda cfg: {"ok": True, "bot_name": "Sentinel", "error": None})
    assert cli.main(["telegram-test"]) == 0
    assert "Connected to Telegram as Sentinel —" in capsys.readouterr().out

    refused = "Telegram refused the bot token (401 Unauthorized) — copy it again from @BotFather"
    answer = {
        "ok": False,
        "bot_name": None,
        "error": refused,
        "results": [_result("", "42", None, False, refused)],
    }
    _install_fake_telegram(monkeypatch, lambda cfg: answer)
    assert cli.main(["telegram-test", "--json"]) == cli.EXIT_ERROR
    assert json.loads(capsys.readouterr().out) == answer


def _two_bots_config() -> None:
    path = paths.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "notifiers": {
                    "telegram": {
                        "enabled": True,
                        "chats": [
                            {"id": "aaaa1111", "label": "Danindu", "bot_token": CLI_BOT, "chat_id": "111111"},
                            {"id": "bbbb2222", "label": "Team", "bot_token": CLI_BOT_B, "chat_id": "-100222"},
                        ],
                    }
                }
            }
        )
    )


def test_telegram_test_prints_one_line_per_bot_and_fails_on_any(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _two_bots_config()
    seen: list[Any] = []

    def fake_send_test(cfg: Any, destination_id: str | None = None) -> dict:
        seen.append(destination_id)
        results = [
            _result(c.label, c.chat_id, f"@{c.label.lower()}_bot", c.chat_id != "-100222")
            for c in cfg.notifiers.telegram.chats
            if destination_id in (None, c.id)
        ]
        for r in results:
            if not r["ok"]:
                r["error"] = "chat not found — add the bot to the group first"
        ok = all(r["ok"] for r in results)
        error = None if ok else "1 of 2 bots failed — Team: chat not found — add the bot to the group first"
        return {"ok": ok, "bot_name": results[0]["bot_name"], "error": error, "results": results}

    _install_fake_telegram(monkeypatch, fake_send_test)
    assert cli.main(["telegram-test"]) == cli.EXIT_ERROR
    captured = capsys.readouterr()
    assert "✅ Danindu · @danindu_bot · delivered" in captured.out
    assert "❌ Team · @team_bot · chat not found — add the bot to the group first" in captured.out
    assert "Telegram test failed: 1 of 2 bots failed" in captured.err
    # one bot, by its id or its chat id
    assert cli.main(["telegram-test", "--to", "aaaa1111"]) == 0
    assert cli.main(["telegram-test", "--to", "111111"]) == 0
    assert seen == [None, "aaaa1111", "aaaa1111"]
    out = capsys.readouterr().out
    assert out.count("✅ Danindu") == 2 and "Team" not in out
    assert cli.main(["telegram-test", "--to", "nobody"]) == cli.EXIT_ERROR
    err = capsys.readouterr().err
    assert "no Telegram bot has the id or chat id 'nobody'" in err and "aaaa1111 (Danindu)" in err
    assert CLI_BOT not in err


# ---------------------------------------------------------------------------- email-test


class _CliRelay:
    """smtplib.SMTP stand-in: checks the app password, refuses the addresses in ``refuse``."""

    sent: list[tuple[Any, list[str]]] = []
    refuse: set[str] = set()

    def __init__(self, host: str, port: int, **kwargs: Any) -> None:
        pass

    def ehlo(self) -> None:
        pass

    def starttls(self, context: Any) -> None:
        pass

    def login(self, user: str, password: str) -> None:
        import smtplib

        if password != "abcdefghijklmnop":
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")

    def send_message(self, msg: Any, from_addr: str, to_addrs: list[str]) -> dict[str, Any]:
        _CliRelay.sent.append((msg, to_addrs))
        return {a: (550, b"5.1.1 unknown user") for a in to_addrs if a in _CliRelay.refuse}

    def quit(self) -> None:
        pass


@pytest.fixture
def relay(monkeypatch: pytest.MonkeyPatch) -> type[_CliRelay]:
    import smtplib

    _CliRelay.sent, _CliRelay.refuse = [], set()
    monkeypatch.setattr(smtplib, "SMTP", _CliRelay)
    return _CliRelay


def _email_config(**email: Any) -> None:
    section = {
        "username": "sentinel@gmail.com",
        "password": "abcdefghijklmnop",
        "recipients": [{"address": "ops@example.org", "label": "On call"}, {"address": "boss@example.org"}],
        **email,
    }
    path = paths.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"server": {"hostname": "repo-1"}, "notifiers": {"email": section}}))


def test_email_test_needs_a_login_and_a_recipient(isolated: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["email-test"]) == cli.EXIT_ERROR
    err = capsys.readouterr().err
    assert "email is not set up: add the Gmail address, app password and at least one recipient" in err
    assert "SENTINEL_SMTP_PASSWORD" in err


def test_email_test_prints_one_line_per_recipient(
    isolated: Path, relay: type[_CliRelay], capsys: pytest.CaptureFixture[str]
) -> None:
    _email_config()
    assert cli.main(["email-test"]) == 0
    out = capsys.readouterr().out
    assert "✅ On call (ops@example.org) · sent" in out and "✅ boss@example.org · sent" in out
    assert "Test email sent through smtp.gmail.com:587 — check the inbox" in out
    ((msg, to),) = relay.sent
    assert (
        to == ["ops@example.org", "boss@example.org"] and msg["Subject"] == "[Sentinel] Test email · repo-1"
    )
    # one recipient
    assert cli.main(["email-test", "--to", "BOSS@example.org"]) == 0
    assert relay.sent[-1][1] == ["boss@example.org"] and "On call" not in capsys.readouterr().out
    assert cli.main(["email-test", "--to", "nobody@example.org"]) == cli.EXIT_ERROR
    err = capsys.readouterr().err
    assert "nobody@example.org is not one of the email recipients: ops@example.org, boss@example.org" in err
    # a refused recipient fails the test (exit 4) and says who
    relay.refuse = {"boss@example.org"}
    assert cli.main(["email-test"]) == cli.EXIT_ERROR
    captured = capsys.readouterr()
    assert "❌ boss@example.org · refused by the mail server: 550 5.1.1 unknown user" in captured.out
    assert "Email test failed: 1 of 2 recipients failed" in captured.err
    assert cli.main(["email-test", "--json"]) == cli.EXIT_ERROR
    answer = json.loads(capsys.readouterr().out)
    assert set(answer) == {"ok", "error", "results"} and [r["ok"] for r in answer["results"]] == [True, False]


def test_email_test_says_why_gmail_refused_the_login(
    isolated: Path, relay: type[_CliRelay], capsys: pytest.CaptureFixture[str]
) -> None:
    _email_config(password="my normal password")
    assert cli.main(["email-test"]) == cli.EXIT_ERROR
    captured = capsys.readouterr()
    assert (
        "Email test failed: Gmail refused the login for sentinel@gmail.com (535 5.7.8 Username and Password "
        "not accepted): use an app password made in that same Google account (Google Account"
    ) in captured.err
    assert "my normal password" not in captured.out + captured.err
    assert "❌" not in captured.out  # one sentence, not the same line for every recipient


def test_email_test_names_a_login_that_is_not_an_address(
    isolated: Path, relay: type[_CliRelay], capsys: pytest.CaptureFixture[str]
) -> None:
    """R1#3: an Amazon SES key id as the login and no from_address: a plain sentence, no traceback text.
    notify-validation#2: the settings file loads with it, so the command itself names the reason."""
    _email_config(smtp_host="127.0.0.1", smtp_port=9850, security="none", username="AKIAIOSFODNN7EXAMPLE")
    for argv in (["email-test"], ["email-test", "--json"], ["email-test", "--to", "ops@example.org"]):
        assert cli.main(argv) == cli.EXIT_ERROR
        captured = capsys.readouterr()
        assert captured.err.splitlines() == [
            "error: Email test failed: the SMTP username is not an email address: set from_address "
            "(the address alerts come from)"
        ]
        assert captured.out == "" and relay.sent == []
    _email_config(
        smtp_host="127.0.0.1",
        smtp_port=9850,
        security="none",
        username="AKIAIOSFODNN7EXAMPLE",
        password="abcdefghijklmnop",
        from_address="sentinel@example.org",
    )
    assert cli.main(["email-test"]) == 0
    assert relay.sent and relay.sent[-1][0]["From"] == "DSpace Sentinel <sentinel@example.org>"


def test_report_email_prints_the_real_outcome(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from dspace_sentinel.reports import generator

    fake_report = _report("/tmp/r.html")
    monkeypatch.setattr(generator, "generate_for_range", lambda *a, **k: fake_report, raising=False)
    sent = {"ok": True, "error": None, "results": []}
    monkeypatch.setattr(generator, "deliver_email", lambda cfg, report, store=None: sent)
    assert cli.main(["report", "--email"]) == 0
    out = capsys.readouterr().out
    assert "Report ready: /tmp/r.html" in out and "Email: sent" in out and "Telegram" not in out
    # both at once: each says what happened; one failing is exit 4
    refused = {"ok": False, "error": "Telegram is not enabled", "skipped": True}
    monkeypatch.setattr(generator, "deliver_telegram", lambda cfg, report, store=None: refused)
    assert cli.main(["report", "--telegram", "--email"]) == cli.EXIT_ERROR
    captured = capsys.readouterr()
    assert "Email: sent" in captured.out and "Telegram: not sent: Telegram is not enabled" in captured.err
    assert cli.main(["report", "--telegram", "--email", "--json"]) == cli.EXIT_ERROR
    payload = json.loads(capsys.readouterr().out)
    assert payload["telegram"] == refused and payload["email"] == sent
    failed = {"ok": False, "error": "email is not enabled", "skipped": True}
    monkeypatch.setattr(generator, "deliver_email", lambda cfg, report, store=None: failed)
    assert cli.main(["report", "--email"]) == cli.EXIT_ERROR
    assert "Email: not sent: email is not enabled" in capsys.readouterr().err


def test_sentinel_sh_runs_email_test(checkout: Path, tmp_path: Path) -> None:
    res = _exec([str(checkout / "sentinel.sh"), "email-test", "--json"], checkout, _sh_env(tmp_path))
    assert res.returncode == 4, res.stdout + res.stderr
    assert "email is not set up" in res.stderr and "unknown command" not in res.stderr


# ------------------------------------------------------------------- config/sentinel.env


def _forget_after_test(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    """Make monkeypatch remove ``names`` at teardown even when the code under test sets them."""
    for name in names:
        monkeypatch.setenv(name, "placeholder")
        monkeypatch.delenv(name)


def test_env_file_supplies_what_the_environment_does_not_set(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """config/sentinel.env gives every command (and a pid-file restart) the same secrets."""
    names = ("SENTINEL_TELEGRAM_TOKEN", "SENTINEL_TELEGRAM_CHAT_ID", "SENTINEL_PG_PASSWORD", "SENTINEL_PORT")
    _forget_after_test(monkeypatch, *names, "SENTINEL_EXTRA")
    env_file = paths.config_path().parent / "sentinel.env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(
        "# secrets for DSpace Sentinel\n"
        f"SENTINEL_TELEGRAM_TOKEN={CLI_BOT}\n"
        '  SENTINEL_TELEGRAM_CHAT_ID = "42"\n'
        "; another comment style\n"
        "SENTINEL_PG_PASSWORD='p#ss \"word\"'\n"
        "SENTINEL_PORT=9701\n"
        'SENTINEL_EXTRA="a \\"quoted\\" \\$value"\n'
        "this line is not an assignment\n"
    )
    env_file.chmod(0o600)
    monkeypatch.setenv("SENTINEL_PORT", "9702")  # the environment wins over the file
    seen: dict[str, Any] = {}

    def fake_send_test(cfg: Any) -> dict:
        seen["cfg"] = cfg
        return {"ok": True, "bot_name": "@bot", "error": None}

    _install_fake_telegram(monkeypatch, fake_send_test)
    assert cli.main(["telegram-test"]) == 0
    cfg = seen["cfg"]
    (bot,) = cfg.notifiers.telegram.chats  # both variables together: one bot of their own
    assert (bot.id, bot.bot_token, bot.chat_id) == ("env", CLI_BOT, "42")
    assert cfg.postgres.password == 'p#ss "word"'
    assert cfg.server.port == 9702
    assert os.environ["SENTINEL_EXTRA"] == 'a "quoted" $value'


def test_env_file_warns_when_other_users_can_read_it(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _forget_after_test(monkeypatch, "SENTINEL_AUTH_TOKEN")
    env_file = paths.config_path().parent / "sentinel.env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("SENTINEL_AUTH_TOKEN=abc\n")
    env_file.chmod(0o644)
    assert cli.load_env_file() == ["SENTINEL_AUTH_TOKEN"]
    assert "chmod 600" in caplog.text


def test_env_file_is_not_used_by_the_demo(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The demo must not pick up the real port, auth token or secrets."""
    _forget_after_test(monkeypatch, "SENTINEL_TELEGRAM_TOKEN", "SENTINEL_DEMO")
    env_file = paths.config_path().parent / "sentinel.env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("SENTINEL_TELEGRAM_TOKEN=123:real\n")
    env_file.chmod(0o600)
    monkeypatch.setattr(cli, "_one_shot_status", lambda cfg: _async_value({"mood": "ok"}))
    monkeypatch.setattr(cli, "render_doctor", lambda *a, **k: "box")
    assert cli.main(["doctor", "--demo"]) == 0
    assert "SENTINEL_TELEGRAM_TOKEN" not in os.environ


async def _async_value(value: Any) -> Any:
    return value


# ------------------------------------------------------------------------ systemd unit


def _body(unit_text: str) -> str:
    """Unit text without the comment header (which mentions @TOKENS@ and User=/Group=)."""
    return "\n".join(line for line in unit_text.splitlines() if not line.startswith("#"))


def test_render_unit_system(tmp_path: Path) -> None:
    text = cli.render_unit(
        "system",
        user="alice",
        group="staff",
        repo_dir=Path("/opt/sentinel"),
        python="/opt/sentinel/.venv/bin/python",
        data_dir=Path("/var/lib/sentinel"),
        config_path=Path("/etc/sentinel/sentinel.yaml"),
    )
    assert re.search(r"@[A-Z_]+@", _body(text)) is None
    assert "User=alice\nGroup=staff\n" in text
    assert "WorkingDirectory=/opt/sentinel\n" in text
    assert 'ExecStart="/opt/sentinel/.venv/bin/python" -m dspace_sentinel serve' in text
    assert 'Environment="SENTINEL_DATA_DIR=/var/lib/sentinel"' in text
    assert 'Environment="SENTINEL_CONFIG=/etc/sentinel/sentinel.yaml"' in text
    assert 'ReadWritePaths="/var/lib/sentinel" "/etc/sentinel"' in text
    assert "EnvironmentFile=-/etc/sentinel/sentinel.env\n" in text  # optional: the leading "-"
    # a config error ends in "failed" after five quick restarts instead of looping forever
    unit_section = text.split("[Service]")[0]
    assert "StartLimitIntervalSec=120" in unit_section and "StartLimitBurst=5" in unit_section
    for directive in (
        "Nice=10",
        "IOSchedulingClass=best-effort",
        "IOSchedulingPriority=7",
        "CPUQuota=25%",
        "MemoryMax=300M",
    ):
        assert directive in text
    for directive in (
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ProtectHome=read-only",
        "PrivateTmp=yes",
    ):
        assert directive in text
    assert "WantedBy=multi-user.target" in text


def _spaced_checkout(root: Path) -> dict[str, Any]:
    """render_unit() arguments for a checkout under a path with a space and a '%'."""
    repo = root / "dspace tools 50%" / "dspace-sentinel"
    for sub in ("data", "config", ".venv/bin"):
        (repo / sub).mkdir(parents=True, exist_ok=True)
    python = repo / ".venv" / "bin" / "python"
    python.symlink_to(sys.executable)
    return {
        "repo_dir": repo,
        "python": str(python),
        "data_dir": repo / "data",
        "config_path": repo / "config" / "sentinel.yaml",
    }


def test_render_unit_quotes_paths_with_spaces(tmp_path: Path) -> None:
    """systemd splits unquoted values on whitespace and expands '%' specifiers."""
    kw = _spaced_checkout(tmp_path)
    repo = str(kw["repo_dir"]).replace("%", "%%")
    text = cli.render_unit("system", user="alice", group="alice", **kw)
    assert f"WorkingDirectory={repo}\n" in text
    assert f'ExecStart="{repo}/.venv/bin/python" -m dspace_sentinel serve' in text
    assert f'Environment="SENTINEL_DATA_DIR={repo}/data"' in text
    assert f'Environment="SENTINEL_CONFIG={repo}/config/sentinel.yaml"' in text
    assert f'ReadWritePaths="{repo}/data" "{repo}/config"' in text


@pytest.mark.parametrize("scope", ["system", "user"])
def test_rendered_unit_passes_systemd_analyze_verify(tmp_path: Path, scope: str) -> None:
    analyze = shutil.which("systemd-analyze")
    if analyze is None:
        pytest.skip("systemd-analyze not installed")
    import getpass

    user = getpass.getuser()
    unit = tmp_path / "dspace-sentinel.service"
    unit.write_text(cli.render_unit(scope, user=user, group=user, **_spaced_checkout(tmp_path)))
    argv = [analyze, *(["--user"] if scope == "user" else []), "verify", str(unit)]
    res = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    problems = [
        line
        for line in (res.stdout + res.stderr).splitlines()
        if unit.name in line and ("not absolute" in line or "Invalid" in line or "not executable" in line)
    ]
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("bad", ['/srv/say "hi"/sentinel', "/srv/back\\slash/sentinel", "/srv/new\nline"])
def test_render_unit_refuses_paths_systemd_cannot_hold(bad: str) -> None:
    with pytest.raises(ValueError, match="systemd unit"):
        cli.render_unit("system", user="alice", group="alice", repo_dir=Path(bad), python="/usr/bin/python3")


def test_render_unit_user_drops_namespace_sandbox() -> None:
    text = _body(cli.render_unit("user", user="alice", group="alice"))
    assert "User=" not in text and "Group=" not in text
    for directive in cli.NAMESPACE_DIRECTIVES:
        assert f"{directive}=" not in text
    assert "NoNewPrivileges=yes" in text and "CPUQuota=25%" in text
    assert "WantedBy=default.target" in text


def test_install_service_print(isolated: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["install-service", "--print"]) == 0
    out = capsys.readouterr().out
    assert "[Service]" in out and "-m dspace_sentinel serve" in out


def test_install_service_refuses_demo(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SENTINEL_DEMO", "1")
    assert cli.main(["install-service", "--user"]) == cli.EXIT_ERROR
    assert "SENTINEL_DEMO" in capsys.readouterr().err


def test_install_manage_uninstall_user_unit(
    isolated: Path, fake_systemctl: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    port = _free_port()
    rc = cli.main(["install-service", "--user", "--port", str(port), "--host", "127.0.0.1"])
    out = capsys.readouterr().out
    assert rc == 0, out
    unit = cli.user_unit_path()
    assert unit.exists() and str(unit).startswith(str(isolated / "home"))
    log = fake_systemctl.read_text()
    assert "systemctl --user daemon-reload" in log
    assert "systemctl --user enable --now dspace-sentinel" in log
    assert "loginctl enable-linger" in log
    assert "Lingering enabled" in out
    assert f"port: {port}" in Path(os.environ["SENTINEL_CONFIG"]).read_text()
    assert cli.unit_scope() == "user"

    # idempotent re-run
    assert cli.main(["install-service", "--user"]) == 0
    assert "Unchanged" in capsys.readouterr().out

    # start/stop/restart/status/logs go through systemctl / journalctl
    assert cli.main(["start"]) == 0
    assert cli.main(["restart"]) == 0
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "running" in out and "pid 4242" in out and "systemd-user" in out
    assert cli.main(["status", "--json"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["running"] is True and info["mode"] == "systemd-user" and info["pid"] == 4242
    assert cli.main(["logs", "-n", "5"]) == 0
    assert cli.main(["stop"]) == 0
    log = fake_systemctl.read_text()
    assert "systemctl --user start dspace-sentinel" in log
    assert "systemctl --user restart dspace-sentinel" in log
    assert "systemctl --user stop dspace-sentinel" in log
    assert "journalctl --user -u dspace-sentinel -n 5 --no-pager" in log

    assert cli.main(["uninstall"]) == 0
    out = capsys.readouterr().out
    assert not unit.exists()
    assert "Removed" in out and "Kept data" in out
    assert "systemctl --user disable --now dspace-sentinel" in fake_systemctl.read_text()
    assert cli.unit_scope() is None


def _foreign_user_unit(isolated: Path) -> Path:
    """A dspace-sentinel user unit that runs another checkout (e.g. the Ansible one in /opt)."""
    other = isolated / "other checkout"
    unit = cli.user_unit_path()
    unit.parent.mkdir(parents=True, exist_ok=True)
    text = cli.render_unit("user", user="alice", group="alice", repo_dir=other, python="/usr/bin/python3")
    unit.write_text(text)
    return unit


def test_a_unit_for_another_checkout_is_left_alone(
    isolated: Path, fake_systemctl: Path, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """A test clone next to the production install must not stop, restart or rewrite its unit."""
    unit = _foreign_user_unit(isolated)
    before = unit.read_text()
    assert cli.unit_scope() is None
    assert "other checkout" in caplog.text and "leaving it alone" in caplog.text
    assert cli.main(["stop"]) == 0 and "Not running" in capsys.readouterr().out
    assert cli.main(["status", "--json"]) == cli.EXIT_WARN
    assert json.loads(capsys.readouterr().out)["mode"] == "pidfile"
    assert cli.main(["install-service", "--user"]) == cli.EXIT_ERROR
    assert "not replacing it" in capsys.readouterr().err and unit.read_text() == before
    assert not fake_systemctl.exists() or "stop" not in fake_systemctl.read_text()
    # --force takes it over deliberately
    assert cli.main(["install-service", "--user", "--force"]) == 0
    assert f"WorkingDirectory={paths.REPO_DIR}\n" in unit.read_text()
    assert cli.unit_scope() == "user"


def test_uninstall_leaves_a_foreign_unit_unless_forced(
    isolated: Path, fake_systemctl: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    unit = _foreign_user_unit(isolated)
    assert cli.main(["uninstall"]) == 0
    out = capsys.readouterr().out
    assert unit.exists() and "Not touching" in out and "other checkout" in out
    assert not fake_systemctl.exists() or "disable" not in fake_systemctl.read_text()
    assert cli.main(["uninstall", "--force"]) == 0
    assert not unit.exists() and "Removed" in capsys.readouterr().out


def test_uninstall_keeps_the_unit_when_disable_fails(
    isolated: Path, fake_systemctl: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["install-service", "--user", "--no-start"]) == 0
    capsys.readouterr()
    monkeypatch.setenv("FAKE_SYSTEMCTL_FAIL", "disable")
    assert cli.main(["uninstall"]) == cli.EXIT_ERROR
    captured = capsys.readouterr()
    assert "disable" in captured.err and "Removed" not in captured.out
    assert cli.user_unit_path().exists()


@pytest.fixture
def background_process(isolated: Path) -> Iterator[subprocess.Popen[bytes]]:
    """A live stand-in for a `./sentinel.sh start` process, recorded in the pid file."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", "dspace_sentinel"])
    pid_file = paths.pid_path(paths.ensure_dirs(paths.data_dir()))
    pid_file.write_text(f"{proc.pid}\n")
    yield proc
    proc.kill()
    proc.wait()


def test_install_service_takes_over_from_the_background_process(
    isolated: Path,
    fake_systemctl: Path,
    background_process: subprocess.Popen[bytes],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Otherwise the new unit crash-loops on the busy port and stop/status lose the old process."""
    monkeypatch.setenv("SENTINEL_PORT", str(_free_port()))
    assert cli.main(["install-service", "--user"]) == 0
    out = capsys.readouterr().out
    assert f"Stopped the background process (pid {background_process.pid})" in out
    assert background_process.wait(timeout=10) is not None
    assert not paths.pid_path(paths.data_dir()).exists()


def test_systemd_mode_stop_and_status_handle_a_background_process(
    isolated: Path,
    fake_systemctl: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SENTINEL_PORT", str(_free_port()))
    assert cli.main(["install-service", "--user", "--no-start"]) == 0
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", "dspace_sentinel"])
    try:
        paths.pid_path(paths.data_dir()).write_text(f"{proc.pid}\n")
        capsys.readouterr()
        cli.main(["status"])
        out = capsys.readouterr().out
        assert f"background process from `./sentinel.sh start` runs as pid {proc.pid}" in out
        assert cli.main(["stop"]) == 0
        assert f"Stopped the background process (pid {proc.pid})" in capsys.readouterr().out
        assert proc.wait(timeout=10) is not None
    finally:
        proc.kill()
        proc.wait()


def test_install_service_fails_loudly_when_the_unit_does_not_start(
    isolated: Path, fake_systemctl: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SENTINEL_PORT", str(_free_port()))
    monkeypatch.setenv("FAKE_SYSTEMCTL_STATE", "failed")
    assert cli.main(["install-service", "--user"]) == cli.EXIT_ERROR
    assert "did not start (state: failed)" in capsys.readouterr().err
    assert "journalctl --user -u dspace-sentinel" in fake_systemctl.read_text()


def test_install_service_restarts_an_active_unit_to_apply_a_new_port(
    isolated: Path, fake_systemctl: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`enable --now` is a no-op on an active unit, so a changed port was never applied."""
    monkeypatch.setenv("SENTINEL_CONFIG", str(isolated / "config" / "sentinel.yaml"))
    assert cli.main(["install-service", "--user", "--port", str(_free_port())]) == 0
    fake_systemctl.write_text("")
    new_port = _free_port()
    assert cli.main(["install-service", "--user", "--port", str(new_port)]) == 0
    log = fake_systemctl.read_text()
    assert "systemctl --user restart dspace-sentinel" in log
    assert f"127.0.0.1:{new_port}" in capsys.readouterr().out


def test_install_service_refuses_a_busy_port(
    isolated: Path, fake_systemctl: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    port = _free_port()
    monkeypatch.setenv("SENTINEL_PORT", str(port))
    other = _FakeSentinel(port)
    try:
        assert cli.main(["install-service", "--user"]) == cli.EXIT_ERROR
    finally:
        other.close()
    assert f"port {port} is already in use by another Sentinel" in capsys.readouterr().err
    assert "enable" not in (fake_systemctl.read_text() if fake_systemctl.exists() else "")


def test_install_service_as_root_checks_the_unit_user_can_use_the_files(
    isolated: Path, fake_systemctl: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """sudo-created, root-owned data/config would make a unit running as $SUDO_USER crash-loop."""
    import getpass

    monkeypatch.setenv("SENTINEL_PORT", str(_free_port()))
    monkeypatch.setattr(cli, "_is_root", lambda: True)
    monkeypatch.setattr(cli, "_invoking_user", lambda: getpass.getuser())
    monkeypatch.setattr(cli, "SYSTEM_UNIT_PATH", isolated / "etc" / "dspace-sentinel.service")
    monkeypatch.setattr(cli, "_as_user", lambda user, argv: str(paths.data_dir()) not in argv)
    assert cli.main(["install-service"]) == cli.EXIT_ERROR
    err = capsys.readouterr().err
    assert f"cannot write {paths.data_dir()}" in err and "chown -R" in err
    assert not fake_systemctl.exists() or "enable" not in fake_systemctl.read_text()


def test_install_service_from_a_root_shell_says_the_unit_runs_as_root(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(cli, "_invoking_user", lambda: "root")
    assert cli.main(["install-service", "--print"]) == 0
    assert "run as root" in caplog.text


def test_start_says_when_the_running_process_does_not_answer_the_configured_url(
    isolated: Path,
    background_process: subprocess.Popen[bytes],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """After `install --port`, the old process still listens on the old port."""
    monkeypatch.setenv("SENTINEL_PORT", str(_free_port()))
    monkeypatch.setattr(cli, "ALREADY_RUNNING_GRACE", 0.2, raising=False)
    assert cli.main(["start"]) == cli.EXIT_WARN
    out = capsys.readouterr().out
    assert f"Already running (pid {background_process.pid})" in out
    assert "does not answer" in out and "./sentinel.sh restart" in out


def test_uninstall_with_nothing_installed(isolated: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["uninstall"]) == 0
    assert "Nothing installed" in capsys.readouterr().out


# --------------------------------------------------------------------- pid-file mode


@pytest.fixture
def background(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    """A free port; whatever ``start`` spawned is killed afterwards."""
    port = _free_port()
    monkeypatch.setenv("SENTINEL_PORT", str(port))
    yield port
    pid_file = paths.pid_path(paths.data_dir())
    pid = cli._read_pid(pid_file)
    if pid and cli._pid_running(pid):
        cli._stop_pid(pid, timeout=5.0)


def test_start_status_logs_stop_with_pidfile(background: int, capsys: pytest.CaptureFixture[str]) -> None:
    port = background
    rc = cli.main(["start", "--timeout", "45"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert f"http://127.0.0.1:{port}" in out
    pid = cli._read_pid(paths.pid_path(paths.data_dir()))
    assert pid and cli._pid_running(pid)

    assert cli.main(["start"]) == 0
    assert "Already running" in capsys.readouterr().out

    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "running" in out and f"pid {pid}" in out and "pidfile" in out
    assert cli.main(["status", "--json"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["running"] is True and info["responding"] is True and info["url"].endswith(f":{port}")
    assert info["mood"] in ("ok", "warn", "crit", "unknown")

    assert cli.main(["logs", "-n", "20"]) == 0
    assert "DSpace Sentinel" in capsys.readouterr().out

    assert cli.main(["stop"]) == 0
    assert f"Stopped (pid {pid})" in capsys.readouterr().out
    assert not paths.pid_path(paths.data_dir()).exists()
    assert not cli._pid_running(pid)

    assert cli.main(["stop"]) == 0
    assert "Not running" in capsys.readouterr().out
    assert cli.main(["status"]) == cli.EXIT_WARN


UNREADABLE_QUIET = (
    "he quiet hours '11pm-7am' in notifiers.telegram.quiet_hours were ignored: "
    "quiet hours must look like 23:00-07:00"
)
"""The notice without its first letter: the log says "the …", the events log "The …"."""


def _config_with_an_unreadable_time(port: int) -> None:
    """A settings file as the code before the times were checked saved or accepted it."""
    path = paths.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "server": {"port": port},
                "setup": {"completed": True},
                "notifiers": {"telegram": {"quiet_hours": "11pm-7am"}},
            }
        )
    )


def test_doctor_and_status_run_with_a_time_that_cannot_be_read(
    isolated: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """notify-validation#1: doctor ended with exit 4, and status fell back to the defaults (so it
    looked at port 9765 whatever the file said). Both run now and show what was left out."""
    port = _free_port()
    _config_with_an_unreadable_time(port)
    monkeypatch.setattr(
        cli, "_one_shot_status", lambda cfg: _async_value({"mood": "ok", "port": cfg.server.port})
    )
    with caplog.at_level(logging.WARNING, logger="dspace_sentinel"):
        assert cli.main(["doctor", "--json"]) == cli.EXIT_OK
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"mood": "ok", "port": port} and "error:" not in captured.err
    assert f"t{UNREADABLE_QUIET}" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="dspace_sentinel"):
        assert cli.main(["status"]) == cli.EXIT_WARN  # nothing was started
    out = capsys.readouterr().out
    assert "not running" in out and f"http://127.0.0.1:{port}" in out  # the file's port, not 9765
    assert f"t{UNREADABLE_QUIET}" in caplog.text and "using defaults" not in caplog.text


def test_start_and_restart_work_with_a_time_that_cannot_be_read(
    background: int, capsys: pytest.CaptureFixture[str]
) -> None:
    """notify-validation#1: after an update, `restart` stopped the running Sentinel and then failed
    on a file the old code had accepted, so nothing monitored the server. It starts, and every
    start writes one config row that names the key and the value."""
    port = background
    _config_with_an_unreadable_time(port)

    def rows() -> list[str]:
        events = cli._http_get(f"http://127.0.0.1:{port}/api/events?kind=config") or []
        return [e["message"] for e in events if "ignored" in e["message"]]

    rc = cli.main(["start", "--timeout", "45"])
    out = capsys.readouterr().out
    assert rc == 0, out
    first_pid = cli._read_pid(paths.pid_path(paths.data_dir()))
    assert rows() == [f"T{UNREADABLE_QUIET}"]
    shown = cli._http_get(f"http://127.0.0.1:{port}/api/config")
    assert shown["notifiers"]["telegram"]["quiet_hours"] == ""
    rc = cli.main(["restart", "--timeout", "45"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert f"Stopped (pid {first_pid})" in out and "Started (pid " in out
    pid = cli._read_pid(paths.pid_path(paths.data_dir()))
    assert pid and pid != first_pid and cli._pid_running(pid)
    assert rows() == [f"T{UNREADABLE_QUIET}"] * 2  # one row per start
    assert cli.main(["status"]) == 0
    assert "running" in capsys.readouterr().out
    assert cli.main(["stop"]) == 0


class _FakeProc:
    """Stands in for the ``serve`` child that ``start`` spawns."""

    pid = 999_999
    returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


def test_start_gives_the_child_absolute_paths(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Relative SENTINEL_DATA_DIR / SENTINEL_CONFIG mean the caller's directory for the server too."""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("SENTINEL_DATA_DIR", "rel/data")
    monkeypatch.setenv("SENTINEL_CONFIG", "rel/config/sentinel.yaml")
    monkeypatch.setenv("SENTINEL_PORT", str(_free_port()))
    seen: dict[str, Any] = {}

    def fake_popen(cmd: list[str], **kwargs: Any) -> _FakeProc:
        seen.update(cmd=cmd, **kwargs)
        return _FakeProc()

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cli, "_wait_healthz", lambda *a, **k: True)
    monkeypatch.setattr(cli, "_serves_port", lambda pid, port: True, raising=False)
    assert cli.main(["start"]) == 0
    assert seen["cwd"] == str(paths.REPO_DIR)
    assert seen["env"]["SENTINEL_DATA_DIR"] == str(work / "rel" / "data")
    assert seen["env"]["SENTINEL_CONFIG"] == str(work / "rel" / "config" / "sentinel.yaml")
    assert (work / "rel" / "data" / "sentinel.pid").read_text().strip() == str(_FakeProc.pid)


def test_start_reports_a_crash(
    background: int, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # a uvicorn that cannot be imported makes the server exit right after it was spawned
    broken = tmp_path / "broken-modules"
    broken.mkdir()
    (broken / "uvicorn.py").write_text("raise SystemExit('uvicorn is broken on purpose')\n")
    monkeypatch.setenv("PYTHONPATH", str(broken))
    rc = cli.main(["start", "--timeout", "20"])
    captured = capsys.readouterr()
    assert rc == cli.EXIT_ERROR
    assert "exited with code" in captured.err and "broken on purpose" in captured.out
    assert not paths.pid_path(paths.data_dir()).exists()


class _FakeSentinel:
    """Another Sentinel on ``port``: answers /healthz and /api/* like a running instance."""

    def __init__(self, port: int) -> None:
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                body = {"/healthz": {"ok": True}, "/api/meta": {"started_ts": 1.0, "modules": {"solr": True}}}
                data = json.dumps(body.get(self.path, {"mood": "ok", "headline": "All systems healthy"}))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data.encode())

            def log_message(self, *args: object) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def test_start_refuses_a_port_another_sentinel_answers_on(
    background: int, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The demo (or another checkout) on the same port must not pass for our own start."""
    spawned: list[list[str]] = []
    monkeypatch.setattr(cli.subprocess, "Popen", lambda cmd, **kw: spawned.append(cmd) or _FakeProc())
    other = _FakeSentinel(background)
    try:
        rc = cli.main(["start", "--timeout", "5"])
    finally:
        other.close()
    err = capsys.readouterr().err
    assert rc == cli.EXIT_ERROR
    assert f"port {background} is already in use by another Sentinel" in err
    assert not spawned and not paths.pid_path(paths.data_dir()).exists()


def test_start_refuses_a_port_another_program_holds(
    background: int, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = socket.socket()
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", background))
    blocker.listen(1)
    try:
        rc = cli.main(["start", "--timeout", "5"])
    finally:
        blocker.close()
    assert rc == cli.EXIT_ERROR
    assert f"port {background} is already in use by another program" in capsys.readouterr().err


def test_start_does_not_take_another_process_answer_as_its_own(
    background: int, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Something else answered /healthz while our server was still starting (a race)."""
    monkeypatch.setattr(cli.subprocess, "Popen", lambda cmd, **kw: _FakeProc())
    monkeypatch.setattr(cli, "_wait_healthz", lambda *a, **k: True)
    monkeypatch.setattr(cli, "_serves_port", lambda pid, port: False)
    monkeypatch.setattr(cli, "_stop_pid", lambda pid, timeout=10.0: True)
    assert cli.main(["start"]) == cli.EXIT_ERROR
    assert "not from the process just started" in capsys.readouterr().err
    assert not paths.pid_path(paths.data_dir()).exists()


def test_status_labels_an_instance_it_does_not_manage(
    background: int, capsys: pytest.CaptureFixture[str]
) -> None:
    """No pid file of ours, yet something answers on the configured port: say so, do not adopt it."""
    other = _FakeSentinel(background)
    try:
        assert cli.main(["status"]) == cli.EXIT_WARN
        out = capsys.readouterr().out
        assert cli.main(["status", "--json"]) == cli.EXIT_WARN
        info = json.loads(capsys.readouterr().out)
    finally:
        other.close()
    assert "not running" in out and "another Sentinel answers on" in out
    assert "health:" not in out and "All systems healthy" not in out
    assert info["running"] is False and info["other_instance"] is True and "mood" not in info


# ------------------------------------------------------------------------------ update


def test_update_runs_git_pull_and_pip(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if "--is-inside-work-tree" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "true\n", "")
        if "--short" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "abc1234\n", "")
        if "pull" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "Already up to date.\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cli, "_run", fake_run)
    assert cli.main(["update", "--no-restart"]) == 0
    out = capsys.readouterr().out
    assert "Already up to date." in out and f"v{__version__}" in out
    assert any(cmd[:3] == ["git", "-C", str(REPO)] and cmd[3:] == ["pull", "--ff-only"] for cmd in calls)
    assert any("pip" in cmd and "-r" in cmd and str(REPO / "requirements.txt") in cmd for cmd in calls)


def test_update_stops_on_pull_failure(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "--is-inside-work-tree" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "true\n", "")
        if "pull" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "error: Your local changes would be overwritten")
        return subprocess.CompletedProcess(cmd, 0, "abc1234\n", "")

    monkeypatch.setattr(cli, "_run", fake_run)
    assert cli.main(["update"]) == cli.EXIT_ERROR
    assert "git pull failed" in capsys.readouterr().err


# ---------------------------------------------------------------------------- discover


def test_discover_json(isolated: Path, no_systemd: None, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["discover", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {"findings", "suggested_config", "detected_persona"} <= set(payload)


# --------------------------------------------------------------------------- sentinel.sh


def _sh(*args: str, env: dict | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SENTINEL_SH), *args],
        capture_output=True,
        text=True,
        cwd=str(REPO),
        env=env,
        timeout=60,
        stdin=subprocess.DEVNULL,  # never a terminal: install must not wait for a login
    )


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A private copy of the checkout whose ``.venv/bin/python`` runs this interpreter.

    ``-m pip`` is a no-op in the copy, so ``install`` never touches a real virtualenv or the network.
    """
    root = tmp_path / "checkout"
    root.mkdir()
    shutil.copy2(SENTINEL_SH, root / "sentinel.sh")
    shutil.copytree(
        REPO / "dspace_sentinel", root / "dspace_sentinel", ignore=shutil.ignore_patterns("__pycache__")
    )
    for rel in (
        "requirements.txt",
        "requirements-dev.txt",
        "config/sentinel.example.yaml",
        "deploy/systemd/dspace-sentinel.service",
    ):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, root / rel)
    venv_python = root / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text(
        f'#!/bin/sh\nif [ "$1" = "-m" ] && [ "$2" = "pip" ]; then exit 0; fi\nexec "{sys.executable}" "$@"\n'
    )
    venv_python.chmod(0o755)
    return root


def _sh_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    """The test process environment with a private HOME and no colours."""
    home = tmp_path / "sh-home"
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in ("XDG_CONFIG_HOME", "PYTHONPATH")}
    env.update(HOME=str(home), NO_COLOR="1", **extra)
    return env


def _exec(
    argv: list[str], cwd: Path, env: dict[str, str], timeout: float = 90, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` with ``input_text`` on stdin, or with no terminal at all (so install never prompts)."""
    stdin = {"input": input_text} if input_text is not None else {"stdin": subprocess.DEVNULL}
    return subprocess.run(
        argv, capture_output=True, text=True, cwd=str(cwd), env=env, timeout=timeout, **stdin
    )


def test_sentinel_sh_works_from_any_directory_and_through_a_symlink(checkout: Path, tmp_path: Path) -> None:
    """cron, `ansible -a /opt/dspace-sentinel/sentinel.sh doctor` and symlinks run from elsewhere."""
    env = _sh_env(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    res = _exec([str(checkout / "sentinel.sh"), "version"], elsewhere, env)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == f"dspace-sentinel {__version__}"
    res = _exec([str(checkout / "sentinel.sh"), "doctor", "--demo", "--ascii"], Path("/"), env)
    assert res.returncode in (0, 1, 2), res.stderr
    assert "DSPACE STACK" in res.stdout and "No module named" not in res.stderr
    link_dir = tmp_path / "bin"
    link_dir.mkdir()
    (link_dir / "sentinel").symlink_to(checkout / "sentinel.sh")
    res = _exec([str(link_dir / "sentinel"), "status"], Path("/"), env)
    assert "not installed yet" not in res.stderr and "No module named" not in res.stderr
    assert res.returncode == 1 and "not running" in res.stdout
    assert str(checkout / "data") in res.stdout  # the checkout's data dir, not the link's


def test_sentinel_sh_install_port_from_another_directory(checkout: Path, tmp_path: Path) -> None:
    """install writes --port through its embedded Python snippets and prints that port."""
    env = _sh_env(tmp_path)
    res = _exec([str(checkout / "sentinel.sh"), "install", "--port", "9793", "--no-discover"], Path("/"), env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "server.port=9793" in res.stdout
    assert "http://127.0.0.1:9793" in res.stdout
    assert yaml.safe_load((checkout / "config" / "sentinel.yaml").read_text())["server"]["port"] == 9793


def test_sentinel_sh_syntax_and_shellcheck() -> None:
    assert subprocess.run(["bash", "-n", str(SENTINEL_SH)], capture_output=True).returncode == 0
    assert SENTINEL_SH.stat().st_mode & stat.S_IXUSR
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck not installed")
    res = subprocess.run([shellcheck, "-s", "bash", str(SENTINEL_SH)], capture_output=True, text=True)
    assert res.returncode == 0, res.stdout


def test_sentinel_sh_help_version_and_usage_errors() -> None:
    res = _sh("help")
    assert res.returncode == 0 and "Usage: ./sentinel.sh <command>" in res.stdout
    for cmd in ("install", "start", "doctor", "report", "demo", "update", "uninstall"):
        assert cmd in res.stdout
    # every sub-command and flag the dispatcher accepts is in the help
    for text in (
        "install-service [--user] [--print] [--no-start]",
        "run | serve",
        "uninstall [--purge] [--yes]",
        "doctor [--json] [--ascii] [--width N] [--demo]",
        "start [--port N] [--host H] [--timeout S]",
        "[--title T]",
        "logs [-f] [-n N]",
        "update [--no-pip] [--no-restart]",
        "version [-v]",
        "data/demo/demo-sentinel.yaml",  # the demo runs with SENTINEL_DATA_DIR=data/demo
        "SENTINEL_ALLOW_OPEN=1",
        "SENTINEL_LOG_LEVEL",
        "SENTINEL_SMTP_PASSWORD",
        "telegram-test [--to ID_OR_CHAT_ID] [--json]",
        "email-test [--to ADDRESS] [--json]",
        "[--telegram] [--email]",
        "set-password [--user NAME] [--password-stdin] [--remove] [--yes]",
        "hash-password [--password-stdin]",
        "[--no-login]",
    ):
        assert text in res.stdout, text
    assert "data/demo-sentinel.yaml" not in res.stdout
    # and every flag argparse shows in its own help (hidden ones use argparse.SUPPRESS)
    parser = cli.build_parser()
    commands = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction)).choices
    missing = [
        f"{name} {action.option_strings[-1]}"
        for name, sub in commands.items()
        for action in sub._actions
        if action.option_strings and action.help != argparse.SUPPRESS and "-h" not in action.option_strings
        if not any(re.search(rf"\[{re.escape(opt)}[\] ]", res.stdout) for opt in action.option_strings)
    ]
    assert missing == [], missing
    res = _sh("version")
    assert res.returncode == 0 and res.stdout.startswith("dspace-sentinel ")
    res = _sh("no-such-command")
    assert res.returncode == 64 and "unknown command" in res.stderr
    res = _sh("install", "--port", "abc")
    assert res.returncode == 64 and "--port must be a number" in res.stderr
    res = _sh("install", "--port")
    assert res.returncode == 64 and "--port needs a value" in res.stderr
    res = _sh("install", "--bogus")
    assert res.returncode == 64 and "unknown install option" in res.stderr


def test_sentinel_sh_passes_the_cli_usage_error_through(checkout: Path, tmp_path: Path) -> None:
    """A mistyped flag exits 64 through the wrapper too, never 2 (critical)."""
    res = _exec([str(checkout / "sentinel.sh"), "doctor", "--jsn"], checkout, _sh_env(tmp_path))
    assert res.returncode == 64 and "unrecognized arguments: --jsn" in res.stderr


def test_sentinel_sh_install_prints_the_real_bind_address(checkout: Path, tmp_path: Path) -> None:
    env = _sh_env(tmp_path)
    res = _exec([str(checkout / "sentinel.sh"), "install", "--no-discover"], checkout, env)
    assert res.returncode == 0, res.stderr
    assert "binds to 127.0.0.1:9765 (this machine only)" in res.stdout
    assert "openssl rand" not in res.stdout + res.stderr


def test_sentinel_sh_install_warns_about_an_open_unauthenticated_bind(checkout: Path, tmp_path: Path) -> None:
    """--host 0.0.0.0 without a token: say so (serve refuses it) instead of 'binds to 127.0.0.1 only'."""
    env = _sh_env(tmp_path)
    argv = [str(checkout / "sentinel.sh"), "install", "--host", "0.0.0.0", "--no-discover"]
    res = _exec(argv, checkout, env)
    assert res.returncode == 0, res.stderr
    assert "127.0.0.1 only" not in res.stdout
    assert "0.0.0.0:9765" in res.stderr and "refuse to start" in res.stderr
    assert "openssl rand -hex 24" in res.stderr and "sentinel.env" in res.stderr
    # with a token the bind is fine: no warning
    env["SENTINEL_AUTH_TOKEN"] = "s3cret-token"
    res = _exec([str(checkout / "sentinel.sh"), "install", "--no-discover"], checkout, env)
    assert res.returncode == 0, res.stderr
    assert "0.0.0.0:9765" in res.stdout and "openssl rand" not in res.stderr


def _no_privileged_tools(bin_dir: Path, calls: Path) -> None:
    """Failing, logging stand-ins so a sentinel.sh test can never reach the real sudo or systemd."""
    bin_dir.mkdir(exist_ok=True)
    for name in ("sudo", "systemctl", "loginctl", "journalctl"):
        (bin_dir / name).write_text(f'#!/bin/sh\necho "{name} $*" >> "{calls}"\nexit 1\n')
        (bin_dir / name).chmod(0o755)


def test_sentinel_sh_install_service_stops_before_a_unit_that_cannot_start(
    checkout: Path, tmp_path: Path
) -> None:
    fake = tmp_path / "fakebin"
    calls = tmp_path / "calls.log"
    _no_privileged_tools(fake, calls)
    env = _sh_env(tmp_path, PATH=f"{fake}{os.pathsep}{os.environ['PATH']}")
    argv = [str(checkout / "sentinel.sh"), "install", "--service", "--host", "0.0.0.0", "--no-discover"]
    res = _exec(argv, checkout, env)
    assert res.returncode == 4 and "refuse to start" in res.stderr
    assert not calls.exists()  # nothing was installed


def test_sentinel_sh_install_refuses_sudo(checkout: Path, tmp_path: Path) -> None:
    """Under sudo every file would belong to root while the unit runs as $SUDO_USER."""
    fake = tmp_path / "fakebin"
    calls = tmp_path / "calls.log"
    _no_privileged_tools(fake, calls)
    (fake / "id").write_text('#!/bin/sh\n[ "$1" = "-u" ] && { echo 0; exit 0; }\nexec /usr/bin/id "$@"\n')
    (fake / "id").chmod(0o755)
    env = _sh_env(tmp_path, PATH=f"{fake}{os.pathsep}{os.environ['PATH']}", SUDO_USER="alice")
    res = _exec([str(checkout / "sentinel.sh"), "install", "--service"], checkout, env)
    assert res.returncode == 4
    assert "as alice" in res.stderr and "./sentinel.sh install --service" in res.stderr
    assert not (checkout / "config" / "sentinel.yaml").exists() and not (checkout / "data").exists()
    assert not calls.exists()


def test_sentinel_sh_install_says_a_running_sentinel_needs_a_restart(checkout: Path, tmp_path: Path) -> None:
    """install --port rewrites the config, but the running process keeps the old port."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", "dspace_sentinel"])
    try:
        (checkout / "data").mkdir(exist_ok=True)
        (checkout / "data" / "sentinel.pid").write_text(f"{proc.pid}\n")
        argv = [str(checkout / "sentinel.sh"), "install", "--port", str(_free_port()), "--no-discover"]
        res = _exec(argv, checkout, _sh_env(tmp_path))
    finally:
        proc.kill()
        proc.wait()
    assert res.returncode == 0, res.stderr
    assert "./sentinel.sh restart" in res.stdout and "Start it:" not in res.stdout


def test_sentinel_sh_install_does_not_claim_success_with_a_broken_config(
    checkout: Path, tmp_path: Path
) -> None:
    (checkout / "config" / "sentinel.yaml").write_text("server: [this is: not valid\n")
    res = _exec([str(checkout / "sentinel.sh"), "install", "--no-discover"], checkout, _sh_env(tmp_path))
    assert res.returncode == 4
    assert "is installed" not in res.stdout and "sentinel.yaml" in res.stderr


def _fake_pythons(bin_dir: Path, broken: tuple[str, ...], working: tuple[str, ...] = ()) -> None:
    """Interpreters on PATH: ``broken`` ones lack venv/ensurepip, ``working`` ones are complete."""
    bin_dir.mkdir(exist_ok=True)
    for name in broken + working:
        guard = 'case "$*" in *ensurepip*) exit 1 ;; esac\n' if name in broken else ""
        path = bin_dir / name
        path.write_text(f'#!/bin/sh\n{guard}exec "{sys.executable}" "$@"\n')
        path.chmod(0o755)


VERSIONED = ("python3.13", "python3.12", "python3.11", "python3.10")


def test_sentinel_sh_install_skips_a_python_without_venv(checkout: Path, tmp_path: Path) -> None:
    """A newer python3.X without its -venv package must not block python3 + python3-venv."""
    fake = tmp_path / "fakebin"
    _fake_pythons(fake, broken=VERSIONED, working=("python3",))
    env = _sh_env(tmp_path, PATH=f"{fake}{os.pathsep}{os.environ['PATH']}")
    res = _exec([str(checkout / "sentinel.sh"), "install", "--no-discover"], checkout, env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "(python3)" in res.stdout and "venv module is missing" not in res.stderr


def test_sentinel_sh_install_names_the_interpreter_choice(checkout: Path, tmp_path: Path) -> None:
    fake = tmp_path / "fakebin"
    _fake_pythons(fake, broken=(*VERSIONED, "python3", "python"))
    env = _sh_env(tmp_path, PATH=f"{fake}{os.pathsep}{os.environ['PATH']}")
    res = _exec([str(checkout / "sentinel.sh"), "install", "--no-discover"], checkout, env)
    assert res.returncode == 4
    assert "venv module is missing for python3.13" in res.stderr
    assert "PYTHON=python3 ./sentinel.sh install" in res.stderr
    # an explicit PYTHON is used as given, never silently replaced
    _fake_pythons(fake, broken=VERSIONED, working=("python3",))
    env["PYTHON"] = "python3.12"
    res = _exec([str(checkout / "sentinel.sh"), "install", "--no-discover"], checkout, env)
    assert res.returncode == 4 and "venv module is missing for python3.12" in res.stderr


def test_sentinel_sh_requires_install_for_runtime_commands(tmp_path: Path) -> None:
    """A copy without .venv refuses runtime commands with a hint instead of a traceback."""
    copy = tmp_path / "copy"
    copy.mkdir()
    (copy / "dspace_sentinel").mkdir()
    shutil.copy(REPO / "dspace_sentinel" / "__init__.py", copy / "dspace_sentinel" / "__init__.py")
    shutil.copy(SENTINEL_SH, copy / "sentinel.sh")
    res = subprocess.run(
        ["bash", str(copy / "sentinel.sh"), "start"], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 4 and "./sentinel.sh install" in res.stderr  # "could not run", not critical
    res = subprocess.run(
        ["bash", str(copy / "sentinel.sh"), "doctor"], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 4 and "not installed yet" in res.stderr
    res = subprocess.run(
        ["bash", str(copy / "sentinel.sh"), "version"], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0 and "not installed yet" in res.stdout


# ------------------------------------------------------------------------ the dashboard login

LOGIN_PASSWORD = "correct horse battery"


class _Terminal(io.StringIO):
    """Standard input that says it is a terminal."""

    def isatty(self) -> bool:
        return True


def _saved_login() -> dict[str, str]:
    return yaml.safe_load(paths.config_path().read_text())["server"]["login"]


def _config_with_login(**server: Any) -> None:
    path = paths.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    login = {"username": "admin", "password_hash": hash_password(LOGIN_PASSWORD)}
    path.write_text(yaml.safe_dump({"server": {"login": login, **server}}))


def test_set_password_from_stdin_saves_a_hash_through_the_file_layer(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SENTINEL_AUTH_TOKEN", "EnvOnlyToken")
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(LOGIN_PASSWORD + "\n"))
    assert cli.main(["set-password", "--password-stdin"]) == 0
    captured = capsys.readouterr()
    assert (
        captured.out.strip() == "Login saved for 'admin'. Sessions that were signed in have been signed out."
    )
    login = _saved_login()
    assert login["username"] == "admin" and verify_password(LOGIN_PASSWORD, login["password_hash"])
    text = paths.config_path().read_text()
    assert LOGIN_PASSWORD not in text + captured.out + captured.err and "EnvOnlyToken" not in text
    assert stat.S_IMODE(paths.config_path().stat().st_mode) == 0o600
    # --user changes the name; the current one is the default afterwards
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("another long password\n"))
    assert cli.main(["set-password", "--user", "ops.admin", "--password-stdin"]) == 0
    assert "Login saved for 'ops.admin'." in capsys.readouterr().out
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("a third long password\n"))
    assert cli.main(["set-password", "--password-stdin"]) == 0
    login = _saved_login()
    assert login["username"] == "ops.admin" and verify_password(
        "a third long password", login["password_hash"]
    )


@pytest.mark.parametrize(
    ("argv", "typed", "message"),
    [
        (["set-password", "--password-stdin"], "short\n", TOO_SHORT),
        (["set-password", "--user", "John Smith", "--password-stdin"], LOGIN_PASSWORD + "\n", USERNAME_RULE),
        (["set-password"], "", "use --password-stdin"),  # no terminal to ask on
    ],
)
def test_set_password_refusals_leave_the_config_alone(
    isolated: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    typed: str,
    message: str,
) -> None:
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(typed))
    assert cli.main(argv) == cli.EXIT_ERROR
    assert message in capsys.readouterr().err
    assert not paths.config_path().exists()


def test_set_password_asks_twice_on_a_terminal_and_never_echoes(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli.sys, "stdin", _Terminal(""))
    names = iter([""])  # Enter keeps the default, admin
    monkeypatch.setattr("builtins.input", lambda prompt="": next(names))
    typed = iter(["short", LOGIN_PASSWORD, LOGIN_PASSWORD + "x", LOGIN_PASSWORD, LOGIN_PASSWORD])
    prompts: list[str] = []

    def getpass(prompt: str = "") -> str:
        prompts.append(prompt)
        return next(typed)

    monkeypatch.setattr(cli.getpass, "getpass", getpass)
    assert cli.main(["set-password"]) == 0
    captured = capsys.readouterr()
    assert prompts == [
        "Password: ",
        "Password: ",
        "Repeat the password: ",
        "Password: ",
        "Repeat the password: ",
    ]
    assert TOO_SHORT in captured.err and "The passwords do not match." in captured.err
    assert "Login saved for 'admin'." in captured.out
    assert LOGIN_PASSWORD not in captured.out + captured.err
    assert verify_password(LOGIN_PASSWORD, _saved_login()["password_hash"])


def test_set_password_remove(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SENTINEL_ALLOW_OPEN", raising=False)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(""))
    _config_with_login(host="0.0.0.0")
    # listening on the network with nothing else to protect it, Sentinel would refuse to start
    assert cli.main(["set-password", "--remove", "--yes"]) == cli.EXIT_ERROR
    assert "not removing the login" in capsys.readouterr().err
    assert _saved_login()["password_hash"]
    monkeypatch.setenv("SENTINEL_AUTH_TOKEN", "s3cret-token")
    assert cli.main(["set-password", "--remove"]) == cli.EXIT_ERROR  # asks first, but cannot here
    assert "--remove needs --yes" in capsys.readouterr().err
    monkeypatch.setattr(cli.sys, "stdin", _Terminal(""))
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert cli.main(["set-password", "--remove"]) == 0
    assert "Kept the login." in capsys.readouterr().out and _saved_login()["password_hash"]
    assert cli.main(["set-password", "--remove", "--yes"]) == 0
    assert capsys.readouterr().out.strip() == "Login removed: the dashboard no longer asks for a password."
    assert _saved_login() == {"username": "", "password_hash": ""}
    assert "s3cret-token" not in paths.config_path().read_text()
    assert cli.main(["set-password", "--remove", "--yes"]) == 0
    assert "No login is set" in capsys.readouterr().out


def test_hash_password_prints_only_the_hash(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(LOGIN_PASSWORD + "\n"))
    assert cli.main(["hash-password", "--password-stdin"]) == 0
    out = capsys.readouterr().out
    (line,) = out.splitlines()
    assert verify_password(LOGIN_PASSWORD, line) and LOGIN_PASSWORD not in out
    assert not paths.config_path().exists()  # only printed
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("short\n"))
    assert cli.main(["hash-password", "--password-stdin"]) == cli.EXIT_ERROR
    captured = capsys.readouterr()
    assert TOO_SHORT in captured.err and captured.out == ""
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(""))
    assert cli.main(["hash-password"]) == cli.EXIT_ERROR  # no terminal to ask on
    assert "use --password-stdin" in capsys.readouterr().err


def test_status_asks_with_the_cli_token_then_the_auth_token(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str, str]] = []
    answers: list[Any] = []

    def fake_get(url: str, token: str = "", timeout: float = 3.0, *, bearer: str = "") -> Any:
        calls.append((url, token, bearer))
        return answers.pop(0)

    monkeypatch.setattr(cli, "_http_get", fake_get)
    cfg = cli.Config()
    cfg.server.auth_token = "script-token"
    answers[:] = [{"ok": 1}]
    assert cli._api_get(cfg, "http://x/api/meta") == {"ok": 1}
    assert calls == [("http://x/api/meta", "script-token", "")]  # no CLI token yet
    token_file = paths.data_dir() / "api-token"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text("cli-token-" + "a" * 30 + "\n")
    calls.clear()
    answers[:] = [None, {"ok": 2}]  # an older server that wrote no token answers the auth token
    assert cli._api_get(cfg, "http://x/api/meta") == {"ok": 2}
    assert calls == [
        ("http://x/api/meta", "", "cli-token-" + "a" * 30),
        ("http://x/api/meta", "script-token", ""),
    ]


def test_status_shows_health_with_a_login_set(background: int, capsys: pytest.CaptureFixture[str]) -> None:
    port = background
    _config_with_login()
    rc = cli.main(["start", "--timeout", "45"])
    out = capsys.readouterr().out
    assert rc == 0, out
    try:
        base = f"http://127.0.0.1:{port}"
        assert cli._http_get(f"{base}/api/meta") is None  # without credentials: 401
        assert cli._http_get(f"{base}/api/session")["login_required"] is True
        assert cli.main(["status", "--json"]) == 0
        info = json.loads(capsys.readouterr().out)
        assert info["responding"] is True and info["mood"] in ("ok", "warn", "crit", "unknown")
        assert cli.main(["status"]) == 0
        assert "health:" in capsys.readouterr().out
    finally:
        assert cli.main(["stop"]) == 0


def test_doctor_needs_no_login(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _config_with_login()
    seen: list[Any] = []

    def one_shot(cfg: Any) -> Any:
        seen.append(cfg.server.login.username)
        return _async_value({"mood": "ok"})

    monkeypatch.setattr(cli, "_one_shot_status", one_shot)
    assert cli.main(["doctor", "--json"]) == cli.EXIT_OK
    assert seen == ["admin"]  # in-process: nothing to sign in to


def test_sentinel_sh_install_never_prompts_without_a_terminal(checkout: Path, tmp_path: Path) -> None:
    hint = "No dashboard login yet: run ./sentinel.sh set-password"
    env = _sh_env(tmp_path)
    res = _exec([str(checkout / "sentinel.sh"), "install", "--no-discover"], checkout, env)
    assert res.returncode == 0, res.stderr
    assert hint in res.stderr and "Create the dashboard login" not in res.stdout
    res = _exec([str(checkout / "sentinel.sh"), "install", "--no-discover", "--no-login"], checkout, env)
    assert res.returncode == 0 and hint not in res.stdout + res.stderr
    # set-password and hash-password pass through to the CLI
    res = _exec(
        [str(checkout / "sentinel.sh"), "set-password", "--password-stdin"],
        checkout,
        env,
        input_text=LOGIN_PASSWORD + "\n",
    )
    assert res.returncode == 0, res.stderr
    assert "Login saved for 'admin'." in res.stdout
    login = yaml.safe_load((checkout / "config" / "sentinel.yaml").read_text())["server"]["login"]
    assert verify_password(LOGIN_PASSWORD, login["password_hash"])
    res = _exec(
        [str(checkout / "sentinel.sh"), "hash-password", "--password-stdin"],
        checkout,
        env,
        input_text=LOGIN_PASSWORD + "\n",
    )
    assert res.returncode == 0 and verify_password(LOGIN_PASSWORD, res.stdout.strip())
    # with a login, listening on the network is fine and nothing asks for a login again
    argv = [str(checkout / "sentinel.sh"), "install", "--host", "0.0.0.0", "--no-discover"]
    res = _exec(argv, checkout, env)
    assert res.returncode == 0, res.stderr
    assert hint not in res.stderr and "refuse to start" not in res.stderr
    assert "0.0.0.0:9765 and asks for the dashboard login" in res.stdout


# ------------------------------------------------------------------------ demo settings


def test_demo_loads_and_saves_its_own_config(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Demo mode never touches config/sentinel.yaml: its settings live in data/demo/demo-sentinel.yaml."""
    real = paths.config_path()
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_text("server: {hostname: prod-box}\nsetup: {completed: true, persona: server}\n")
    monkeypatch.setenv("SENTINEL_DEMO", "1")
    args = types.SimpleNamespace(demo=True, host=None, port=None)
    cfg = cli._load(args)
    assert cfg.demo is True and cfg.hostname == "demo-dspace" and cfg.setup.completed is False
    demo_file = paths.demo_config_path(paths.data_dir())
    demo_file.parent.mkdir(parents=True, exist_ok=True)
    demo_file.write_text("setup: {completed: true, persona: full}\nappearance: {theme: dark}\n")
    again = cli._load(args)
    assert again.setup.completed is True and again.appearance.theme == "dark" and again.demo is True
    # serve hands the demo file (not the real config) to the app
    seen: dict[str, Any] = {}

    def fake_create_app(cfg: Any, *, config_path: Any, data_dir: Any) -> object:
        seen["config_path"] = config_path
        return object()

    import uvicorn

    monkeypatch.setattr("dspace_sentinel.api.app.create_app", fake_create_app)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    assert cli.main(["demo"]) == 0
    assert seen["config_path"] == demo_file
    assert yaml.safe_load(real.read_text())["server"]["hostname"] == "prod-box"  # untouched


@pytest.mark.parametrize(
    ("version", "end_of_life"),
    [("7.6.7", True), ("7.6.8-SNAPSHOT", True), ("8.5", False), ("9.4", False), ("10.1", False)],
)
def test_doctor_marks_an_end_of_life_dspace(version: str, end_of_life: bool) -> None:
    summary = cli._summary({"id": "backend", "metrics": {"dspace_version": version, "dspace_name": "Repo"}})
    assert summary.startswith(f"DSpace {version}")
    assert ("(end-of-life)" in summary) is end_of_life
    assert summary.endswith("Repo")


def test_doctor_calms_down_when_every_open_alert_is_acknowledged(mem_store: Any) -> None:
    """F7 #6: doctor's headline and exit code follow Home; the box still lists the alert as acked."""
    from dspace_sentinel import health
    from dspace_sentinel.api.routes_status import build_status
    from dspace_sentinel.config import Config, merge_patch
    from dspace_sentinel.models import Alert, CheckResult
    from dspace_sentinel.state import State

    cfg = merge_patch(Config(), {"server": {"hostname": "server-1", "timezone": "UTC"}})
    state = State()
    check = CheckResult("ports.8983", "Solr :8983", "warn", 1.0, message="exposed publicly (::)")
    state.set_component(health.rollup("ports", [check], now=1.0))
    alert_id = mem_store.insert_alert(
        Alert(None, "component_down:ports", "ports", "warning", "open", "Ports degraded", "", opened_ts=1.0)
    )
    mem_store.ack_alert(alert_id, 2.0)
    status = build_status(cfg, state, mem_store, now=100.0)
    assert (status["mood"], status["headline"]) == ("ok", "All systems healthy · 1 acknowledged")
    assert next(c for c in status["components"] if c["id"] == "ports")["status"] == "warn"
    assert status["alerts_open"][0]["acked_ts"] == 2
    assert cli.exit_code_for(status["mood"]) == cli.EXIT_OK == 0
    text = cli.render_doctor(status, cfg, ascii_only=False)
    assert "All systems healthy · 1 acknowledged" in text
    assert "Ports degraded · ports · since 1 m ago · acked" in text
    assert "server-1 · 1970-01-01 00:01 UTC" in text  # the box's time names its zone
