"""Command line: ``python -m dspace_sentinel <command>`` (SPEC section 10).

``sentinel.sh`` is a thin wrapper around this module: everything except ``install`` ends up
here. Sub-commands: ``serve``, ``demo``, ``doctor``, ``report``, ``discover``,
``telegram-test``, ``email-test``, ``set-password``, ``hash-password``, ``install-service``,
``start``, ``stop``, ``restart``, ``status``, ``logs``, ``update``, ``uninstall``, ``version``.

Exit codes: 0 ok · 1 warnings / not running · 2 critical · 3 not available in this build ·
4 error (the command could not run) · 64 usage error · 130 interrupted. 0/1/2 are reserved for
health results so a monitoring wrapper can tell "Solr is down" from "the check did not run".
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import grp
import inspect
import json
import logging
import os
import pwd
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from base64 import b64encode
from pathlib import Path
from typing import Any, NoReturn

from dspace_sentinel import __version__, paths
from dspace_sentinel.config import Config, load_config, load_demo_state
from dspace_sentinel.util import fmt
from dspace_sentinel.util.tz import zone_of

log = logging.getLogger("dspace_sentinel")

EXIT_OK = 0
EXIT_WARN = 1
EXIT_CRIT = 2
EXIT_NOT_IMPLEMENTED = 3
EXIT_ERROR = 4
EXIT_USAGE = 64  # sysexits.h EX_USAGE: unknown command or option
EXIT_INTERRUPTED = 130

UNIT_NAME = "dspace-sentinel"
SYSTEM_UNIT_PATH = Path("/etc/systemd/system") / f"{UNIT_NAME}.service"
UNIT_TEMPLATE_PATH = paths.REPO_DIR / "deploy" / "systemd" / f"{UNIT_NAME}.service"
REPORT_RANGES: dict[str, int] = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
"""``report --range`` values and their length in seconds."""
UNIT_START_TIMEOUT = 15.0
"""Seconds install-service waits for a (re)started unit to answer ``/healthz``."""
ALREADY_RUNNING_GRACE = 3.0
"""Seconds ``start`` gives a running background process to answer before calling it stale."""

NAMESPACE_DIRECTIVES: frozenset[str] = frozenset(
    {
        "ProtectSystem",
        "ProtectHome",
        "ReadWritePaths",
        "ReadOnlyPaths",
        "InaccessiblePaths",
        "PrivateTmp",
        "PrivateDevices",
        "ProtectKernelTunables",
        "ProtectKernelModules",
        "ProtectKernelLogs",
        "ProtectControlGroups",
        "ProtectHostname",
        "ProtectClock",
        "ProtectProc",
        "RestrictNamespaces",
    }
)
"""Unit directives that need a mount namespace; dropped from ``systemd --user`` units."""

USER_ONLY_INVALID: frozenset[str] = frozenset({"User", "Group"})
"""Directives systemd rejects inside a user unit."""

_UNIT_UNSAFE = re.compile(r'["\\\x00-\x1f\x7f]')
"""Characters a unit file value cannot carry safely in both quoted and unquoted settings."""

BOX_WIDTH = 78
MARKS: dict[str, str] = {"ok": "✅", "warn": "⚠️", "crit": "❌", "unknown": "—", "off": "—"}
ASCII_MARKS: dict[str, str] = {"ok": "OK", "warn": "!!", "crit": "XX", "unknown": "--", "off": "--"}
TAG: dict[str, str] = {
    "ok": "[ OK ]",
    "warn": "[WARN]",
    "crit": "[CRIT]",
    "unknown": "[ ?? ]",
    "off": "[ -- ]",
}
STACK_IDS: tuple[str, ...] = ("postgres", "solr", "backend", "frontend", "repository", "assetstore", "logs")
NETWORK_IDS: tuple[str, ...] = ("nginx", "ssl", "ports", "security")

_ZERO_WIDTH = {0xFE0E, 0xFE0F, 0x200B, 0x200C, 0x200D}
_HTTP_CODE_RE = re.compile(r"\bHTTP[ /](\d{3})\b")
_ASCII_MAP: tuple[tuple[str, str], ...] = (
    ("✅", "OK"),
    ("⚠️", "!!"),
    ("⚠", "!!"),
    ("❌", "XX"),
    ("🛰️ ", ""),
    ("→", ">"),
    ("·", "-"),
    ("…", "..."),
    ("—", "--"),
    ("›", ">"),
    ("\u202f", " "),
    ("\u00a0", " "),
)
"""Free-text substitutions for ``--ascii`` (alert messages carry the same marks as Telegram)."""


# ------------------------------------------------------------------------------ helpers


def setup_logging(verbose: bool = False) -> None:
    """Console logging with timestamps; ``SENTINEL_LOG_LEVEL`` overrides the level."""
    level_name = os.environ.get("SENTINEL_LOG_LEVEL", "DEBUG" if verbose else "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _prepare_demo_env(args: argparse.Namespace) -> None:
    """Point a demo run at ``data/demo`` unless the operator chose explicit locations."""
    os.environ["SENTINEL_DEMO"] = "1"
    if not os.environ.get("SENTINEL_DATA_DIR"):
        os.environ["SENTINEL_DATA_DIR"] = str(paths.REPO_DIR / "data" / "demo")
    if not os.environ.get("SENTINEL_CONFIG"):
        os.environ["SENTINEL_CONFIG"] = str(paths.REPO_DIR / "data" / "demo" / "sentinel.yaml")


def _load(args: argparse.Namespace) -> Config:
    """Load the effective config, honouring ``--demo``, ``--host`` and ``--port``."""
    demo = bool(getattr(args, "demo", False)) or _env_truthy("SENTINEL_DEMO")
    if demo:
        _prepare_demo_env(args)
    cfg = load_config(paths.config_path(), demo=demo)
    if demo:
        # what the UI changed during an earlier demo session (onboarding, thresholds, theme)
        cfg = load_demo_state(cfg, paths.demo_config_path(paths.data_dir()))
    host = getattr(args, "host", None)
    port = getattr(args, "port", None)
    if host:
        cfg.server.host = host
    if port:
        cfg.server.port = int(port)
    return cfg


def _load_or_default(args: argparse.Namespace) -> Config:
    """Like :func:`_load` but a broken YAML only warns (used by stop/logs/uninstall)."""
    try:
        return _load(args)
    except ValueError as exc:
        log.warning("%s (using defaults)", exc)
        cfg = Config()
        cfg.demo = bool(getattr(args, "demo", False)) or _env_truthy("SENTINEL_DEMO")
        return cfg


ENV_FILE_NAME = "sentinel.env"
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def env_file_path() -> Path:
    """``<config dir>/sentinel.env``: environment for every command and the unit's ``EnvironmentFile=``."""
    return paths.config_path().parent / ENV_FILE_NAME


def _env_file_value(raw: str) -> str:
    """A ``KEY=VALUE`` value the way systemd reads it: quotes removed, backslash escapes resolved."""
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return re.sub(r'\\(["\\$`])', r"\1", value[1:-1])
    return re.sub(r"\\(.)", r"\1", value)


def load_env_file(path: Path | None = None) -> list[str]:
    """Export the ``KEY=VALUE`` lines of ``config/sentinel.env`` the environment does not set yet.

    The systemd units read the same file (``EnvironmentFile=``), so secrets such as
    ``SENTINEL_TELEGRAM_TOKEN`` or ``SENTINEL_PG_PASSWORD`` reach every CLI command and survive a
    pid-file ``restart``/``update`` from another shell. Blank lines and ``#``/``;`` comments are
    skipped and values may be quoted. Returns the names that were exported.
    """
    path = path or env_file_path()
    try:
        text = path.read_text(encoding="utf-8")
        mode = path.stat().st_mode
    except FileNotFoundError:
        return []
    except OSError as exc:
        log.warning("cannot read %s: %s", path, exc)
        return []
    if mode & 0o077:
        log.warning("%s is readable by other users; chmod 600 it (it may hold secrets)", path)
    exported: list[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line[0] in "#;":
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _ENV_KEY_RE.match(key):
            log.warning("%s:%d is not KEY=VALUE; ignored", path, number)
            continue
        if key not in os.environ:
            os.environ[key] = _env_file_value(value)
            exported.append(key)
    return exported


def _silence_stdout() -> None:
    """The reader went away (``status | head -1``): send further output to /dev/null."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        os.close(devnull)
    except (OSError, ValueError):
        pass


def _say(text: str = "") -> None:
    try:
        print(text, flush=True)
    except BrokenPipeError:
        _silence_stdout()  # the command still finishes and exits with its own result


def _err(text: str) -> None:
    print(f"error: {text}", file=sys.stderr, flush=True)


def _run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Run a command, capturing text output; never raises on a non-zero exit."""
    kwargs.setdefault("text", True)
    kwargs.setdefault("capture_output", True)
    log.debug("run: %s", " ".join(cmd))
    try:
        return subprocess.run(cmd, check=False, **kwargs)
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))


def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _invoking_user() -> str:
    """The real operator, even under ``sudo``."""
    return os.environ.get("SUDO_USER") or getpass.getuser()


def _primary_group(user: str) -> str:
    try:
        return grp.getgrgid(pwd.getpwnam(user).pw_gid).gr_name
    except (KeyError, OSError):
        return user


def _home_of(user: str) -> Path:
    try:
        return Path(pwd.getpwnam(user).pw_dir)
    except (KeyError, OSError):
        return Path.home()


def user_unit_path() -> Path:
    """Where ``install-service --user`` writes the unit (honours ``XDG_CONFIG_HOME``)."""
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "systemd" / "user" / f"{UNIT_NAME}.service"


def venv_python() -> str:
    """The interpreter a unit should run: ``<repo>/.venv/bin/python`` when present, else this one."""
    candidate = paths.REPO_DIR / ".venv" / "bin" / "python"
    return str(candidate) if candidate.exists() else sys.executable


def unit_checkout(path: Path) -> Path | None:
    """The checkout an installed unit runs: its ``WorkingDirectory=`` (``None`` when unreadable)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "WorkingDirectory":
            value = value.strip().lstrip("-").replace("%%", "%")
            return Path(value) if value else None
    return None


def is_our_unit(path: Path) -> bool:
    """True when the unit at ``path`` runs this checkout (the unit name is the same for every one)."""
    checkout = unit_checkout(path)
    return checkout is not None and checkout.resolve() == paths.REPO_DIR.resolve()


_FOREIGN_UNITS_REPORTED: set[Path] = set()


def unit_scope() -> str | None:
    """``"system"`` / ``"user"`` when this checkout's unit is installed and systemctl exists.

    A ``dspace-sentinel`` unit that runs another checkout (say the Ansible install in /opt next
    to a test clone) is never managed from here: this checkout then uses its pid file.
    """
    if shutil.which("systemctl") is None:
        return None
    for scope, path in (("system", SYSTEM_UNIT_PATH), ("user", user_unit_path())):
        if not path.exists():
            continue
        if is_our_unit(path):
            return scope
        if path not in _FOREIGN_UNITS_REPORTED:
            _FOREIGN_UNITS_REPORTED.add(path)
            log.warning(
                "%s runs %s, not this checkout (%s); leaving it alone",
                path,
                unit_checkout(path) or "another checkout",
                paths.REPO_DIR,
            )
    return None


def _systemctl(scope: str, *verbs: str, privileged: bool = False) -> list[str]:
    """Build a ``systemctl`` command for ``scope`` (``sudo`` for privileged system verbs)."""
    cmd = ["systemctl"]
    if scope == "user":
        cmd.append("--user")
    elif privileged and not _is_root():
        cmd = ["sudo", "systemctl"]
    cmd.extend(verbs)
    return cmd


def _sudo(cmd: list[str]) -> list[str]:
    return cmd if _is_root() else ["sudo", *cmd]


# ------------------------------------------------------------------------- HTTP probes


def _probe_host(host: str) -> str:
    if host in ("", "0.0.0.0"):
        return "127.0.0.1"
    if host == "::":
        return "[::1]"
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def base_url(cfg: Config) -> str:
    """The URL a browser on this machine uses to reach Sentinel."""
    return f"http://{_probe_host(cfg.server.host)}:{cfg.server.port}"


def _http_get(url: str, token: str = "", timeout: float = 3.0, *, bearer: str = "") -> Any:
    """GET ``url`` and decode JSON; ``None`` on any failure. ``Authorization: Bearer`` with
    ``bearer``, else Basic auth with ``token`` when set."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if bearer:
        req.add_header("Authorization", f"Bearer {bearer}")
    elif token:
        req.add_header("Authorization", "Basic " + b64encode(f"sentinel:{token}".encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local URL
            return json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _api_get(cfg: Config, url: str) -> Any:
    """GET an API URL the way this machine's CLI may: with the local CLI token the running server
    wrote to ``<data dir>/api-token`` (it works with a login set), else with ``server.auth_token``."""
    from dspace_sentinel.auth import read_cli_token

    cli_token = read_cli_token(paths.data_dir())
    if cli_token:
        answer = _http_get(url, bearer=cli_token)
        if answer is not None:
            return answer
    return _http_get(url, cfg.server.auth_token)


def _port_in_use(host: str, port: int, timeout: float = 0.5) -> bool:
    """True when something already accepts TCP connections where Sentinel would listen."""
    try:
        with socket.create_connection((_probe_host(host).strip("[]"), port), timeout=timeout):
            return True
    except OSError:
        return False


def _serves_port(pid: int, port: int) -> bool | None:
    """Whether process ``pid`` holds a listening TCP socket on ``port`` (``None`` when unknown).

    ``/healthz`` does not say *which* Sentinel answered; the socket owner does.
    """
    try:
        import psutil

        proc = psutil.Process(pid)
        lister = getattr(proc, "net_connections", None) or proc.connections
        conns = lister(kind="tcp")
    except Exception:
        return None
    return any(c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port for c in conns)


def _wait_healthz(url: str, timeout: float, proc: subprocess.Popen[bytes] | None = None) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            return False
        if _http_get(f"{url}/healthz", timeout=1.0):
            return True
        time.sleep(0.25)
    return False


# ------------------------------------------------------------------------------ pid file


def _read_pid(pid_file: Path) -> int | None:
    try:
        return int(pid_file.read_text().strip() or 0) or None
    except (OSError, ValueError):
        return None


def _pid_running(pid: int | None) -> bool:
    """True when ``pid`` is alive *and* looks like a Sentinel process (guards against pid reuse)."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    cmdline = Path(f"/proc/{pid}/cmdline")
    if cmdline.exists():
        try:
            return b"dspace_sentinel" in cmdline.read_bytes()
        except OSError:
            return True
    return True


def _tail(path: Path, lines: int) -> list[str]:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            block = 65536
            data = b""
            while size > 0 and data.count(b"\n") <= lines:
                step = min(block, size)
                size -= step
                fh.seek(size)
                data = fh.read(step) + data
        return data.decode("utf-8", "replace").splitlines()[-lines:]
    except OSError:
        return []


# ------------------------------------------------------------------------------ serve


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the web server (uvicorn) in the foreground."""
    import uvicorn

    from dspace_sentinel.api.app import UnsafeBindError, create_app

    cfg = _load(args)
    data_dir = paths.ensure_dirs(paths.data_dir())
    # A demo session saves its settings next to the demo database, never into the real config.
    config_path = paths.demo_config_path(data_dir) if cfg.demo else paths.config_path()
    try:
        app = create_app(cfg, config_path=config_path, data_dir=data_dir)
    except UnsafeBindError as exc:
        _err(str(exc))
        return EXIT_ERROR
    mode = "DEMO" if cfg.demo else "live"
    log.info(
        "DSpace Sentinel v%s (%s) listening on http://%s:%s  data=%s config=%s",
        __version__,
        mode,
        cfg.server.host,
        cfg.server.port,
        data_dir,
        config_path,
    )
    if cfg.demo:
        log.info(
            "demo settings (onboarding, thresholds, theme) are kept in %s; delete it to start over",
            config_path,
        )
    try:
        uvicorn.run(
            app,
            host=cfg.server.host,
            port=cfg.server.port,
            log_level="warning",
            access_log=bool(getattr(args, "verbose", False)),
            proxy_headers=True,
            server_header=False,
            # an open dashboard (SSE) ends on SIGTERM; never wait longer than this for any request
            timeout_graceful_shutdown=5,
        )
    except KeyboardInterrupt:  # pragma: no cover
        pass
    return EXIT_OK


def cmd_demo(args: argparse.Namespace) -> int:
    """``serve --demo`` on port 9765 (unless ``--port`` / ``SENTINEL_PORT`` say otherwise)."""
    args.demo = True
    if not getattr(args, "port", None) and not os.environ.get("SENTINEL_PORT"):
        args.port = 9765
    return cmd_serve(args)


# ----------------------------------------------------------------------------- doctor


async def _one_shot_status(cfg: Config) -> dict[str, Any]:
    """Run every enabled collector once (in-memory store) and return the ``/api/status`` payload."""
    from dspace_sentinel.alerts.engine import AlertEngine
    from dspace_sentinel.alerts.incidents import IncidentEngine
    from dspace_sentinel.api.routes_status import build_status
    from dspace_sentinel.bus import Bus
    from dspace_sentinel.scheduler import Scheduler
    from dspace_sentinel.state import State
    from dspace_sentinel.store import Store

    store = Store(":memory:")
    state = State()
    bus = Bus()
    scheduler = Scheduler(
        cfg,
        store,
        state,
        bus,
        alert_engine=AlertEngine(store, cfg, bus),
        incident_engine=IncidentEngine(store, cfg, bus),
        demo_backfill_days=0,
    )
    try:
        await scheduler.run_all_now()
        # a second system sample makes CPU % and network rates meaningful on real hosts
        if not cfg.demo:
            await asyncio.sleep(1.0)
            await scheduler.run_all_now()
        return build_status(cfg, state, store, scheduler, scheduler.alert_engine)
    finally:
        await scheduler.stop()
        store.close()


def display_width(text: str) -> int:
    """Terminal cells ``text`` occupies: wide/emoji glyphs count 2, variation selectors 0."""
    width = 0
    chars = list(text)
    for i, ch in enumerate(chars):
        cp = ord(ch)
        if cp in _ZERO_WIDTH or unicodedata.combining(ch):
            continue
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            width += 2
        elif i + 1 < len(chars) and ord(chars[i + 1]) == 0xFE0F:
            width += 2  # emoji presentation sequence such as ⚠️
        else:
            width += 1
    return width


def _clip(text: str, width: int, ellipsis: str = "…") -> str:
    """Cut ``text`` to ``width`` cells, ending with ``ellipsis`` when something was removed."""
    if display_width(text) <= width:
        return text
    out: list[str] = []
    used = 0
    limit = width - display_width(ellipsis)
    for ch in text:
        w = display_width(ch)
        if used + w > limit:
            break
        out.append(ch)
        used += w
    return "".join(out).rstrip() + ellipsis


def asciify(text: str) -> str:
    """Replace the marks and typographic characters Sentinel uses with plain ASCII."""
    for src, dst in _ASCII_MAP:
        text = text.replace(src, dst)
    return text.encode("ascii", "replace").decode("ascii")


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def _stdout_supports_unicode() -> bool:
    encoding = (getattr(sys.stdout, "encoding", None) or "").lower()
    if not encoding:
        return False
    try:
        "┌─┐│✅⚠️❌…".encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


class Box:
    """Boxed dashboard renderer with a fixed outer width (cells, emoji-aware).

    ``ascii_only`` swaps the box-drawing characters and emoji for plain ASCII so the output
    survives terminals and log files without UTF-8.
    """

    def __init__(self, width: int = BOX_WIDTH, ascii_only: bool = False) -> None:
        self.width = max(40, width)
        self.ascii = ascii_only
        self.inner = self.width - 4
        self.lines: list[str] = []
        if ascii_only:
            self.h, self.v = "-", "|"
            self.tl, self.tr, self.bl, self.br, self.ml, self.mr = "+", "+", "+", "+", "+", "+"
        else:
            self.h, self.v = "─", "│"
            self.tl, self.tr, self.bl, self.br, self.ml, self.mr = "┌", "┐", "└", "┘", "├", "┤"

    def mark(self, status: str | None) -> str:
        """The status glyph (✅ ⚠️ ❌ —), padded to two cells."""
        table = ASCII_MARKS if self.ascii else MARKS
        return _pad(table.get(status or "unknown", table["unknown"]), 2)

    def top(self) -> None:
        self.lines.append(self.tl + self.h * (self.width - 2) + self.tr)

    def bottom(self) -> None:
        self.lines.append(self.bl + self.h * (self.width - 2) + self.br)

    def section(self, title: str) -> None:
        head = f"{self.ml}{self.h} {title} "
        self.lines.append(head + self.h * (self.width - display_width(head) - 1) + self.mr)

    def row(self, text: str = "", right: str = "", indent: int = 0) -> None:
        """One line: ``text`` left (clipped), optional ``right`` flush right."""
        if self.ascii:
            text, right = asciify(text), asciify(right)
        ellipsis = "..." if self.ascii else "…"
        text = " " * indent + text
        if right:
            avail = self.inner - display_width(right) - 2
            text = _clip(text, max(0, avail), ellipsis)
            gap = self.inner - display_width(text) - display_width(right)
            self.lines.append(f"{self.v} {text}{' ' * gap}{right} {self.v}")
        else:
            text = _clip(text, self.inner, ellipsis)
            self.lines.append(f"{self.v} {_pad(text, self.inner)} {self.v}")

    def items(self, items: list[str], sep: str = " · ", indent: int = 3) -> None:
        """Pack short items into as many rows as needed (never splits an item)."""
        if self.ascii:
            items = [asciify(item) for item in items]
            sep = asciify(sep)
        line = ""
        for item in items:
            candidate = f"{line}{sep}{item}" if line else item
            if line and display_width(candidate) + indent > self.inner:
                self.row(line, indent=indent)
                line = item
            else:
                line = candidate
        if line or not items:
            self.row(line, indent=indent)

    def render(self) -> str:
        """The box as text."""
        return "\n".join(self.lines)


def _http_code(rung: dict[str, Any] | None) -> int | None:
    """HTTP status of a rung, from ``detail`` (``status_code``/``http_status``) or the message."""
    if not rung:
        return None
    detail = rung.get("detail") or {}
    for key in ("status_code", "http_status", "code"):
        value = detail.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    match = _HTTP_CODE_RE.search(rung.get("message") or "")
    return int(match.group(1)) if match else None


def _rung(component: dict[str, Any], rung_id: str) -> dict[str, Any] | None:
    for rung in component.get("chain", []):
        if rung.get("id") == rung_id:
            return rung
    return None


def _value_text(component: dict[str, Any]) -> str:
    """Headline value: ``"200 / 91 ms"`` for HTTP rungs, ``"2 ms"`` otherwise, ``""`` when unknown."""
    from dspace_sentinel.health import HEADLINE_LATENCY

    headline_id = HEADLINE_LATENCY.get(component.get("id", ""), "")
    head = _rung(component, headline_id)
    latency = component.get("latency_ms")
    if latency is None and head is not None:
        latency = head.get("latency_ms")
    if not headline_id and latency is not None and latency < 1.0:
        latency = None  # sub-millisecond path/stat checks carry no information
    code = _http_code(head)
    if code is not None and latency is not None:
        return f"{code} / {fmt.ms(latency)}"
    if code is not None:
        return str(code)
    if latency is not None:
        return fmt.ms(latency)
    return ""


def _short_name(rung_id: str) -> str:
    return rung_id.split(".", 1)[1] if "." in rung_id else rung_id


def _chain_items(component: dict[str, Any], box: Box) -> list[str]:
    """``["service ✅", "TCP ✅", "cores 4/4 ✅", ...]`` with Solr core rungs collapsed.

    Rung names are spelled as alerts and Telegram messages spell them (``rung_short``).
    """
    from dspace_sentinel.alerts.messages import rung_short
    from dspace_sentinel.models import worst

    items: list[str] = []
    cores: list[dict[str, Any]] = []
    for rung in component.get("chain", []):
        rid = rung.get("id", "")
        if ".core." in rid:
            cores.append(rung)
            continue
        if cores:
            items.append(_cores_item(cores, box, worst))
            cores = []
        items.append(f"{rung_short(rid)} {box.mark(rung.get('status')).rstrip()}")
    if cores:
        items.append(_cores_item(cores, box, worst))
    return items


def _cores_item(cores: list[dict[str, Any]], box: Box, worst: Any) -> str:
    ok = sum(1 for c in cores if c.get("status") == "ok")
    return f"cores {ok}/{len(cores)} {box.mark(worst(c.get('status') for c in cores)).rstrip()}"


def _summary(component: dict[str, Any]) -> str:
    """One-line context shown next to a healthy component."""
    m = component.get("metrics") or {}
    cid = component.get("id")
    if cid == "postgres":
        parts = []
        if m.get("connections") is not None and m.get("max_connections"):
            parts.append(f"{m['connections']} / {m['max_connections']} connections")
        if m.get("db_size_bytes"):
            parts.append(f"db {fmt.bytes_(m['db_size_bytes'])}")
        if m.get("version"):
            parts.append(" ".join(str(m["version"]).split()[:2]))
        return " · ".join(parts)
    if cid == "solr":
        parts = []
        if m.get("cores"):
            parts.append(f"{len(m['cores'])} cores")
        if m.get("search_docs") is not None:
            parts.append(f"{fmt.number(m['search_docs'])} docs")
        if m.get("version"):
            parts.append(f"Solr {m['version']}")
        return " · ".join(parts)
    if cid == "backend":
        from dspace_sentinel.dspace_version import end_of_life, parse

        parts = []
        if m.get("dspace_version"):
            eol = " (end-of-life)" if end_of_life(parse(str(m["dspace_version"]))) else ""
            parts.append(f"DSpace {m['dspace_version']}{eol}")
        if m.get("dspace_name"):
            parts.append(str(m["dspace_name"]))
        return " · ".join(parts)
    if cid == "frontend":
        return str(m.get("title") or "")
    if cid == "nginx":
        return str(m.get("server_header") or "")
    if cid == "ssl":
        days = m.get("days_left")
        return f"{days:.0f} days left" if isinstance(days, (int, float)) else ""
    return ""


def _component_rows(box: Box, comp: dict[str, Any]) -> None:
    """Header row (mark · label · value · reason/summary) plus the ladder or metrics row."""
    status = comp.get("status", "unknown")
    label = _pad(str(comp.get("label", comp.get("id"))), 17)
    value = _value_text(comp)
    tail = comp.get("reason") if status != "ok" and comp.get("reason") else _summary(comp)
    head = f"{box.mark(status)} {label}{_pad(value, 14) if tail else value}{tail or ''}"
    box.row(head.rstrip())
    cid = comp.get("id")
    m = comp.get("metrics") or {}
    if cid == "repository":
        box.items(
            [
                f"{fmt.number(m.get('items'))} items",
                f"{fmt.number(m.get('bitstreams'))} bitstreams",
                f"{fmt.number(m.get('communities'))} communities",
                f"{fmt.number(m.get('collections'))} collections",
                f"db {fmt.bytes_(m.get('db_size'))}",
                f"assetstore {fmt.bytes_(m.get('assetstore_bytes'))}",
            ]
        )
    elif cid == "assetstore":
        items = [str(m.get("path") or m.get("mount") or "")]
        if m.get("free") is not None:
            items.append(f"{fmt.gb(m.get('free'))} free of {fmt.gb(m.get('total'))}")
        if m.get("pct") is not None:
            items.append(f"{fmt.pct(m.get('pct'))} used")
        if m.get("growth_bytes_per_day"):
            items.append(f"growing {fmt.bytes_(m.get('growth_bytes_per_day'))}/day")
        box.items([i for i in items if i])
    elif cid == "logs":
        files = m.get("files") or {}
        items = []
        for rung in comp.get("chain", []):
            fid = _short_name(rung.get("id", ""))
            info = files.get(fid, {}) if isinstance(files, dict) else {}
            errors = info.get("errors", rung.get("value"))
            text = f"{fid} {int(errors or 0)} err" if errors is not None else f"{fid} n/a"
            items.append(f"{text} {box.mark(rung.get('status')).rstrip()}")
        if not items:
            items = ["no log files configured"]
        window = m.get("window_minutes")
        box.items(items + ([f"({window} min window)"] if window else []))
    elif cid == "ssl":
        items = [str(m.get("subject") or ""), str(m.get("issuer") or "")]
        if m.get("not_after"):
            items.append(f"expires {str(m['not_after'])[:10]}")
        box.items([i for i in items if i])
    elif cid == "ports":
        _ports_rows(box, comp)
    elif cid == "security":
        items = _chain_items(comp, box)
        if items:
            box.items(items)  # eight separate checks, not a chain: joined with " · " like the ports
    else:
        items = _chain_items(comp, box)
        if items:
            box.items(items, sep=" → " if not box.ascii else " > ")


def _ports_rows(box: Box, comp: dict[str, Any]) -> None:
    items = []
    for rung in comp.get("chain", []):
        detail = rung.get("detail") or {}
        port = rung.get("id", "").split(".")[-1]
        label = str(rung.get("label", "")).split(" :")[0]
        local = detail.get("expect") == "local_only"
        text = f"{port} {label}".strip()
        if local:
            text += " LOCAL ONLY"
        items.append(f"{text} {box.mark(rung.get('status')).rstrip()}")
    box.items(items or ["no port rules configured"])


def _server_rows(box: Box, server: dict[str, Any]) -> None:
    per = server.get("per_metric") or {}
    box.items(
        [
            f"{box.mark(per.get('cpu', 'ok'))} CPU {fmt.pct(server.get('cpu_pct'))}",
            f"{box.mark(per.get('memory', 'ok'))} Memory {fmt.pct(server.get('mem_pct'))}",
            f"{box.mark(per.get('disk', 'ok'))} Disk {fmt.pct(server.get('disk_pct'))}",
            f"{box.mark(per.get('load', 'ok'))} Load {server.get('load1')} / {server.get('cores')} cores",
        ],
        sep="   ",
        indent=0,
    )
    second = [
        f"{box.mark(per.get('swap', 'ok')).rstrip()} swap {fmt.pct(server.get('swap_pct'))}",
        f"{fmt.bytes_(server.get('mem_used'))} of {fmt.bytes_(server.get('mem_total'))} used",
        f"rx {fmt.rate_bps(server.get('net_rx_bps'))}",
        f"tx {fmt.rate_bps(server.get('net_tx_bps'))}",
    ]
    if server.get("inode_pct") is not None:
        second.append(f"inodes {fmt.pct(server.get('inode_pct'))}")
    box.items(second)
    box.items(
        [
            f"{server.get('os_name', '')} {server.get('os_version', '')}".strip(),
            f"kernel {server.get('kernel')}",
            f"up {fmt.duration(server.get('uptime_s'))}",
            f"{server.get('disk_path', '/')} {fmt.pct(server.get('disk_pct'))} used",
        ]
    )
    for mount in server.get("disks", []):
        status = per.get(f"disk:{mount.get('path')}", "ok")
        box.row(
            f"{box.mark(status).rstrip()} {mount.get('path')} {fmt.pct(mount.get('pct'))} used"
            f" · {fmt.gb(mount.get('free'))} free of {fmt.gb(mount.get('total'))}",
            indent=3,
        )


def render_doctor(
    status: dict[str, Any], cfg: Config, ascii_only: bool | None = None, width: int = BOX_WIDTH
) -> str:
    """The boxed dashboard: SERVER / DSPACE STACK / NETWORK / ALERTS (README style).

    ``ascii_only=None`` picks ASCII automatically when stdout cannot encode UTF-8.
    """
    if ascii_only is None:
        ascii_only = not _stdout_supports_unicode()
    box = Box(width, ascii_only)
    server = status.get("server") or {}
    comps = {c["id"]: c for c in status.get("components", [])}
    now = status.get("ts") or time.time()
    brand = "" if ascii_only else "🛰️ "

    box.top()
    stamp = fmt.date_time(now, zone_of(cfg), zone=True)
    box.row(f"{brand}DSpace Sentinel v{__version__}", f"{status.get('hostname')} · {stamp}")
    mood_value = status.get("mood", "unknown")
    right = "demo data" if status.get("demo") else "live"
    if status.get("checked_at"):
        right += f" · checked {fmt.relative(now - status['checked_at'])}"
    box.row(f"{box.mark(mood_value)} {status.get('headline', '')}", right)

    box.section("SERVER")
    if server.get("ts"):
        _server_rows(box, server)
    else:
        box.row(f"{box.mark('unknown')} no system sample yet")

    box.section("DSPACE STACK")
    stack = [comps[c] for c in STACK_IDS if c in comps]
    if not stack:
        box.row(f"{box.mark('off')} no DSpace modules enabled (More › Modules)")
    for comp in stack:
        _component_rows(box, comp)

    box.section("NETWORK")
    net = [comps[c] for c in NETWORK_IDS if c in comps]
    if not net:
        box.row(f"{box.mark('off')} nginx / SSL / ports / security modules are off")
    for comp in net:
        _component_rows(box, comp)

    box.section("ALERTS")
    alerts = status.get("alerts_open", [])
    if not alerts:
        box.row(f"{box.mark('ok')} No open alerts")
    for alert in alerts[:8]:
        severity = "crit" if alert.get("severity") == "critical" else "warn"
        since = fmt.relative(now - (alert.get("opened_ts") or now))
        acked = " · acked" if alert.get("acked_ts") else ""
        box.row(
            f"{box.mark(severity)} {alert.get('title')} · {alert.get('component')} · since {since}{acked}"
        )
        # component alerts are multi-line (chain, then reason): one row per line keeps the border
        for line in str(alert.get("message") or "").splitlines():
            if line.strip():
                box.row(line.strip(), indent=3)
    if len(alerts) > 8:
        box.row(f"… and {len(alerts) - 8} more", indent=3)
    box.bottom()
    return box.render()


def exit_code_for(mood: str) -> int:
    """0 ok · 1 warnings/unknown · 2 critical."""
    if mood == "crit":
        return EXIT_CRIT
    if mood == "ok":
        return EXIT_OK
    return EXIT_WARN


def cmd_doctor(args: argparse.Namespace) -> int:
    """One-shot check of everything enabled, printed as the boxed dashboard (``--json`` for the payload)."""
    cfg = _load(args)
    status = asyncio.run(_one_shot_status(cfg))
    if args.json:
        _say(json.dumps(status, indent=2, default=str))
    else:
        _say(render_doctor(status, cfg, ascii_only=True if args.ascii else None, width=args.width))
    return exit_code_for(status.get("mood", "unknown"))


# ----------------------------------------------------------------------------- report


def _generate_report(
    store: Any, cfg: Config, range_str: str, fmt_: str, title: str | None, send_telegram: bool
) -> Any:
    """Call ``reports.generator.generate_for_range`` (preferred) or ``generate`` with a computed range."""
    from dspace_sentinel.reports import generator

    range_str = range_str.strip().lower()
    if range_str not in REPORT_RANGES:
        raise ValueError(f"--range must be one of {', '.join(REPORT_RANGES)} (got {range_str!r})")
    valid = getattr(generator, "VALID_FORMATS", ("html", "md", "json"))
    if fmt_ not in valid:
        raise ValueError(f"--format must be one of {', '.join(valid)} (got {fmt_!r})")
    func = getattr(generator, "generate_for_range", None)
    if callable(func):
        kwargs: dict[str, Any] = {}
        try:
            params = inspect.signature(func).parameters
        except (TypeError, ValueError):
            params = {}
        var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
        for name, value in (("title", title), ("send_telegram", send_telegram)):
            if value not in (None, False) and (name in params or var_kw):
                kwargs[name] = value
        result = func(store, cfg, range_str, fmt_, **kwargs)
    else:
        now = time.time()
        result = generator.generate(
            store, cfg, now - REPORT_RANGES[range_str], now, fmt_, title, None, send_telegram
        )
    if inspect.isawaitable(result):
        result = asyncio.run(result)
    return result


def _delivery_text(delivery: dict[str, Any] | None) -> str:
    """One line for what happened to ``report --telegram`` / ``--email``."""
    if delivery is None:
        return "sent when the notifier is configured"
    if delivery.get("skipped") == "demo":
        return "demo mode: logged, not sent"
    if delivery.get("ok"):
        return "sent"
    if delivery.get("pending"):
        return "scheduled"
    return f"not sent: {delivery.get('error') or 'unknown error'}"


def cmd_report(args: argparse.Namespace) -> int:
    """Generate a report against the live database and print its path.

    ``--telegram`` / ``--email`` also send the file (both may be given); each prints what really
    happened, and any delivery that did not go out makes the exit code 4.
    """
    from dspace_sentinel.reports import generator
    from dspace_sentinel.store import Store

    cfg = _load(args)
    data_dir = paths.ensure_dirs(paths.data_dir())
    store = Store(paths.db_path(data_dir))
    # deliver here rather than inside generate(), which drops the outcome, so the result is printed
    deliver = getattr(generator, "deliver_telegram", None) if args.telegram else None
    deliver_email = getattr(generator, "deliver_email", None) if getattr(args, "email", False) else None
    delivery: dict[str, Any] | None = None
    email_delivery: dict[str, Any] | None = None
    try:
        report = _generate_report(
            store, cfg, args.range, args.format, args.title, args.telegram and deliver is None
        )
        if deliver is not None and report is not None:
            delivery = deliver(cfg, report, store=store)
        if deliver_email is not None and report is not None:
            email_delivery = deliver_email(cfg, report, store=store)
    except NotImplementedError as exc:
        _say(f"Reports are not available in this build yet ({exc}).")
        _say("The reports module is still being finished; try again after `./sentinel.sh update`.")
        return EXIT_NOT_IMPLEMENTED
    except ValueError as exc:
        _err(str(exc))
        return EXIT_ERROR
    finally:
        store.close()
    if report is None:
        _err("the report generator returned nothing")
        return EXIT_ERROR
    path = getattr(report, "path", None) or (report.get("path") if isinstance(report, dict) else None)
    size = getattr(report, "size", None) or (report.get("size") if isinstance(report, dict) else None)
    title = getattr(report, "title", None) or (report.get("title") if isinstance(report, dict) else None)
    telegram_failed = isinstance(delivery, dict) and delivery.get("ok") is False
    email_failed = isinstance(email_delivery, dict) and email_delivery.get("ok") is False
    failed = telegram_failed or email_failed
    if args.json:
        payload = report.to_dict() if hasattr(report, "to_dict") else report
        if delivery is not None:
            payload = {**payload, "telegram": delivery}
        if email_delivery is not None:
            payload = {**payload, "email": email_delivery}
        _say(json.dumps(payload, indent=2, default=str))
        return EXIT_ERROR if failed else EXIT_OK
    _say(f"Report ready: {path}")
    details = [f"{args.format}", f"range {args.range}"]
    if size:
        details.append(fmt.bytes_(size))
    if title:
        details.insert(0, str(title))
    _say("  " + " · ".join(details))
    for wanted, name, outcome, bad in (
        (args.telegram, "Telegram", delivery, telegram_failed),
        (getattr(args, "email", False), "Email", email_delivery, email_failed),
    ):
        if bad:
            _err(f"{name}: {_delivery_text(outcome)}")
        elif wanted:
            _say(f"  {name}: {_delivery_text(outcome)}")
    return EXIT_ERROR if failed else EXIT_OK


# --------------------------------------------------------------------------- discover


def cmd_discover(args: argparse.Namespace) -> int:
    """Print discovery findings and the suggested config patch; ``--apply`` writes it."""
    from dspace_sentinel.config import merge_patch, save_config
    from dspace_sentinel.discovery import discover

    cfg = _load(args)
    result = asyncio.run(discover(cfg))
    if args.json:
        _say(json.dumps(result.to_dict(), indent=2, default=str))
    else:
        _say("Findings:")
        if not result.findings:
            _say("  - nothing recognised on this machine (is dspace.install_dir right?)")
        for line in result.findings:
            _say(f"  - {line}")
        _say(f"\nDetected persona: {result.detected_persona or 'none'}")
        _say("\nSuggested config patch:")
        import yaml

        patch = result.to_dict().get("suggested_config") or {}
        _say(
            yaml.safe_dump(patch, sort_keys=False, allow_unicode=True).rstrip()
            if patch
            else "  (nothing to change)"
        )
    if args.apply:
        if cfg.demo:
            _say("Not applying in demo mode.")
            return EXIT_WARN
        if not result.suggested_config:
            _say("Nothing to apply.")
            return EXIT_OK
        new_cfg = merge_patch(cfg, result.suggested_config)
        save_config(new_cfg, paths.config_path())
        _say(f"Applied to {paths.config_path()}")
    return EXIT_OK


# ------------------------------------------------------------------------ telegram-test


def _outcome_line(ok: bool, *parts: str) -> str:
    """``✅ Danindu · @danindu_bot · delivered`` (``OK``/``XX`` and plain ASCII without UTF-8)."""
    text = " · ".join(part for part in parts if part)
    if _stdout_supports_unicode():
        return f"  {'✅' if ok else '❌'} {text}"
    return f"  {'OK' if ok else 'XX'} {asciify(text)}"


def _run_async(result: Any) -> Any:
    return asyncio.run(result) if inspect.isawaitable(result) else result


def cmd_telegram_test(args: argparse.Namespace) -> int:
    """Send the "DSpace Sentinel is connected" test message through every configured bot (or one).

    One line per destination with its bot's name; exit 0 when every tested bot delivered, 4
    otherwise (nothing configured, an unknown ``--to``, a refused token or chat).
    """
    cfg = _load(args)
    try:
        from dspace_sentinel.alerts.notifiers import telegram
    except ImportError:
        _say("The Telegram notifier is not part of this build yet; try again after `./sentinel.sh update`.")
        return EXIT_NOT_IMPLEMENTED
    chats = list(cfg.notifiers.telegram.chats)
    if not chats:
        _err(
            "Telegram is not configured: add a bot (its token from @BotFather and your chat id) in "
            "More › Telegram, under notifiers.telegram.chats in config/sentinel.yaml, or with "
            "SENTINEL_TELEGRAM_TOKEN + SENTINEL_TELEGRAM_CHAT_ID."
        )
        return EXIT_ERROR
    wanted: list[str] | None = None
    if args.to:
        key = args.to.strip()
        match = [c for c in chats if c.id == key] or [c for c in chats if c.chat_id.lower() == key.lower()]
        if not match:
            known = ", ".join(f"{c.id} ({c.name})" for c in chats)
            _err(f"no Telegram bot has the id or chat id {key!r}; configured: {known}")
            return EXIT_ERROR
        wanted = [c.id for c in match]
    send_test = getattr(telegram, "send_test", None)
    if not callable(send_test):
        _say("This build's Telegram notifier has no send_test(); nothing sent.")
        return EXIT_NOT_IMPLEMENTED
    try:
        if wanted is None:
            answers = [_run_async(send_test(cfg))]
        else:
            answers = [_run_async(send_test(cfg, destination_id=dest_id)) for dest_id in wanted]
    except Exception as exc:
        _err(f"Telegram test failed: {exc}")
        return EXIT_ERROR
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    for answer in answers:
        answer = answer if isinstance(answer, dict) else {"ok": bool(answer)}
        results.extend(answer.get("results") or [])
        if not answer.get("ok"):
            errors.append(str(answer.get("error") or "unknown error"))
    first = answers[0] if isinstance(answers[0], dict) else {}
    ok = not errors
    bot_name = next((r.get("bot_name") for r in results if r.get("bot_name")), first.get("bot_name"))
    error = None if ok else "; ".join(errors)
    if args.json:
        _say(json.dumps({"ok": ok, "bot_name": bot_name, "error": error, "results": results}))
        return EXIT_OK if ok else EXIT_ERROR
    for r in results:
        name = str(r.get("label") or r.get("chat_id") or r.get("id") or "bot")
        outcome = "delivered" if r.get("ok") else str(r.get("error") or "not delivered")
        _say(_outcome_line(bool(r.get("ok")), name, str(r.get("bot_name") or ""), outcome))
    if ok:
        chats_word = "chat" if len(results) <= 1 else "chats"
        who = f" as {bot_name}" if bot_name and len(results) <= 1 else ""  # "@username" or first name
        _say(f"Connected to Telegram{who} — check your {chats_word} for the test message.")
    else:
        _err(f"Telegram test failed: {error}")
    return EXIT_OK if ok else EXIT_ERROR


def cmd_email_test(args: argparse.Namespace) -> int:
    """Send the test email to every recipient (or the one ``--to`` names) as one message.

    One line per recipient; exit 0 when every one got it, 4 when any did not or email is not
    ready (never 3: email is always part of the build). Settings that are incomplete, or that
    loaded but can never send (a login that is not an address and no ``from_address``), end the
    command with that one sentence before anything is sent.
    """
    cfg = _load(args)
    from dspace_sentinel.alerts.email_messages import connected_email
    from dspace_sentinel.alerts.notifiers import email as mail

    email_cfg = cfg.notifiers.email
    reason = mail.unready(cfg)
    if reason == mail.NOT_SET_UP:
        _err(
            f"{mail.NOT_SET_UP} (More › Email in the UI or notifiers.email in config/sentinel.yaml; "
            "the app password can also come from SENTINEL_SMTP_PASSWORD in config/sentinel.env)."
        )
        return EXIT_ERROR
    if reason:
        _err(f"Email test failed: {reason}")
        return EXIT_ERROR
    address = None
    if args.to:
        match = [r for r in email_cfg.recipients if r.address.lower() == args.to.strip().lower()]
        if not match:
            known = ", ".join(r.address for r in email_cfg.recipients)
            _err(f"{args.to.strip()} is not one of the email recipients: {known}")
            return EXIT_ERROR
        address = match[0].address
    result = asyncio.run(mail.send_test(cfg, address=address))
    ok, error, results = bool(result.get("ok")), result.get("error"), list(result.get("results") or [])
    if args.json:
        _say(json.dumps({"ok": ok, "error": error, "results": results}))
        return EXIT_OK if ok else EXIT_ERROR
    session_failed = bool(results) and not any(r.get("ok") for r in results)
    session_failed = session_failed and len({r.get("error") for r in results}) == 1
    if not session_failed:  # a refused login or connection is one sentence, not one per recipient
        for r in results:
            address_text = str(r.get("address") or "")
            name = f"{r['label']} ({address_text})" if r.get("label") else address_text
            outcome = "sent" if r.get("ok") else str(r.get("error") or "not sent")
            _say(_outcome_line(bool(r.get("ok")), name, outcome))
    if ok:
        subject = connected_email(cfg.hostname).subject
        _say(
            f"Test email sent through {email_cfg.smtp_host}:{email_cfg.smtp_port} — check the inbox "
            f'for "{subject}".'
        )
        return EXIT_OK
    _err(f"Email test failed: {error or 'not sent'}")
    return EXIT_ERROR


# ---------------------------------------------------------------------- install-service


def _unit_value(value: str) -> str:
    """``value`` ready for the unit template: ``%`` doubled so systemd does not expand specifiers.

    The template quotes the settings that split on whitespace; quotes, backslashes and control
    characters cannot be represented the same way in every setting, so such paths are refused.
    """
    if _UNIT_UNSAFE.search(value):
        raise ValueError(
            f"cannot write a systemd unit for {value!r}: quotes, backslashes and control characters "
            "are not supported in its paths; move the checkout or use `./sentinel.sh start`"
        )
    return value.replace("%", "%%")


def render_unit(
    scope: str = "system",
    *,
    user: str | None = None,
    group: str | None = None,
    repo_dir: Path | None = None,
    python: str | None = None,
    data_dir: Path | None = None,
    config_path: Path | None = None,
    template: Path | None = None,
) -> str:
    """Render ``deploy/systemd/dspace-sentinel.service`` for ``scope`` (``system`` or ``user``).

    The template carries ``@TOKENS@``; a user unit additionally loses ``User=``/``Group=`` and
    every namespace-based sandbox directive (they need privileges a user manager lacks) and is
    wanted by ``default.target``.
    """
    repo_dir = repo_dir or paths.REPO_DIR
    user = user or _invoking_user()
    group = group or _primary_group(user)
    data_dir = data_dir or paths.data_dir()
    config_path = config_path or paths.config_path()
    text = (template or UNIT_TEMPLATE_PATH).read_text(encoding="utf-8")
    tokens = {
        "@USER@": user,
        "@GROUP@": group,
        "@REPO_DIR@": str(repo_dir),
        "@PYTHON@": python or venv_python(),
        "@DATA_DIR@": str(data_dir),
        "@CONFIG_PATH@": str(config_path),
        "@CONFIG_DIR@": str(config_path.parent),
        "@WANTED_BY@": "default.target" if scope == "user" else "multi-user.target",
    }
    for token, value in tokens.items():
        text = text.replace(token, _unit_value(value))
    if scope == "user":
        kept = []
        for line in text.splitlines():
            key = line.split("=", 1)[0].strip()
            if key in USER_ONLY_INVALID or key in NAMESPACE_DIRECTIVES:
                continue
            kept.append(line)
        text = "\n".join(kept) + "\n"
    return text


def _write_config_overrides(cfg: Config, host: str | None, port: int | None) -> None:
    """Persist ``--host`` / ``--port`` into the YAML so the unit and the CLI agree."""
    if not host and not port:
        return
    from dspace_sentinel.config import merge_patch, save_config

    patch: dict[str, Any] = {"server": {}}
    if host:
        patch["server"]["host"] = host
    if port:
        patch["server"]["port"] = int(port)
    save_config(merge_patch(cfg, patch), paths.config_path())


def _as_user(user: str, argv: list[str]) -> bool | None:
    """Run ``argv`` as ``user`` (needs root) and say whether it succeeded; ``None`` when it cannot."""
    try:
        pw = pwd.getpwnam(user)
        res = subprocess.run(  # noqa: S603 - fixed argv
            argv,
            user=pw.pw_uid,
            group=pw.pw_gid,
            extra_groups=os.getgrouplist(user, pw.pw_gid),
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (KeyError, OSError, ValueError, subprocess.SubprocessError):
        return None
    return res.returncode == 0


def _unit_user_problems(user: str) -> list[str]:
    """What the unit's ``User=`` cannot do that Sentinel needs, checked by running ``test`` as it."""
    data_dir, config_path = paths.data_dir(), paths.config_path()
    writable = 'test -w "$1" && test -x "$1"'
    wanted = [
        (f"write {data_dir}", ["sh", "-c", writable, "sh", str(data_dir)]),
        (f"write {config_path.parent}", ["sh", "-c", writable, "sh", str(config_path.parent)]),
    ]
    db = paths.db_path(data_dir)
    if db.exists():
        wanted.append((f"write {db}", ["test", "-w", str(db)]))
    if config_path.exists():
        wanted.append((f"read {config_path}", ["test", "-r", str(config_path)]))
    return [what for what, argv in wanted if _as_user(user, argv) is False]


def _background_pid() -> int | None:
    """Pid of a live ``./sentinel.sh start`` process of this data dir, if any."""
    pid = _read_pid(paths.pid_path(paths.data_dir()))
    return pid if _pid_running(pid) else None


def _retire_background_process(reason: str) -> bool:
    """Stop a live ``./sentinel.sh start`` process because systemd runs Sentinel now.

    Left running it keeps the port (the unit then crash-loops) and systemd-mode stop/status would
    not see it. Prints what it did; ``False`` when the process could not be stopped.
    """
    pid = _background_pid()
    if pid is None:
        return True
    if not _stop_pid(pid):
        _err(f"could not stop the background Sentinel (pid {pid}); stop it first:  kill {pid}")
        return False
    paths.pid_path(paths.data_dir()).unlink(missing_ok=True)
    _say(f"Stopped the background process (pid {pid}); {reason}.")
    return True


def _verify_unit_started(scope: str, url: str) -> bool:
    """Wait for the (re)started unit to answer ``/healthz``; show the journal tail when it failed."""
    deadline = time.monotonic() + UNIT_START_TIMEOUT
    while True:
        state = _run(_systemctl(scope, "is-active", UNIT_NAME)).stdout.strip() or "unknown"
        if state != "active":
            break
        if _http_get(f"{url}/healthz", timeout=1.0):
            _say(f"State: active · {url}")
            return True
        if time.monotonic() >= deadline:
            _say(f"State: active · {url} (not answering yet; see ./sentinel.sh status and logs)")
            return True
        time.sleep(0.5)
    _err(f"{UNIT_NAME} did not start (state: {state}); last journal lines:")
    journal = [
        "journalctl",
        *(["--user"] if scope == "user" else []),
        "-u",
        UNIT_NAME,
        "-n",
        "20",
        "--no-pager",
    ]
    res = _run(journal)
    for line in (res.stdout or res.stderr).strip().splitlines():
        _say(f"  {line}")
    return False


def cmd_install_service(args: argparse.Namespace) -> int:
    """Install the systemd unit (system unit via sudo, or ``--user`` unit with linger)."""
    scope = "user" if args.user else "system"
    cfg = _load(args)
    if cfg.demo:
        _err("install-service is for real monitoring; unset SENTINEL_DEMO first")
        return EXIT_ERROR
    user = _invoking_user()
    if user == "root" and scope == "system":
        log.warning(
            "the unit will run as root (install-service was started from a root shell, not with sudo); "
            "run it as the user that owns %s to keep Sentinel unprivileged",
            paths.REPO_DIR,
        )
    unit = render_unit(scope, user=user)
    if args.print:
        _say(unit.rstrip())
        return EXIT_OK
    if shutil.which("systemctl") is None:
        _err("systemd is not available on this machine; use `./sentinel.sh start` (pid file mode) instead")
        return EXIT_ERROR
    target = user_unit_path() if scope == "user" else SYSTEM_UNIT_PATH
    if target.exists() and not is_our_unit(target) and not args.force:
        _err(
            f"{target} runs {unit_checkout(target) or 'another checkout'}, not {paths.REPO_DIR}; "
            "not replacing it (install-service --force takes it over)"
        )
        return EXIT_ERROR
    paths.ensure_dirs(paths.data_dir())
    paths.config_path().parent.mkdir(parents=True, exist_ok=True)
    try:
        _write_config_overrides(cfg, args.host, args.port)
    except ValueError as exc:
        _err(str(exc))
        return EXIT_ERROR
    if scope == "system" and _is_root() and user != "root":
        # under sudo the unit runs as $SUDO_USER: root-owned data or config make it crash-loop
        if (args.host or args.port) and paths.config_path().exists():
            pw = pwd.getpwnam(user)
            os.chown(paths.config_path(), pw.pw_uid, pw.pw_gid)  # we just wrote it as root
        problems = _unit_user_problems(user)
        if problems:
            _err(
                f"the unit runs as {user}, who cannot {', cannot '.join(problems)} (created with sudo?). "
                f"Hand them over first:  sudo chown -R {user}: {paths.data_dir()} {paths.config_path().parent}"
                f"   or run ./sentinel.sh install-service as {user}; it uses sudo only for the unit"
            )
            return EXIT_ERROR
    url = base_url(cfg)
    # `enable --now` does nothing to an active unit: restart it so a new unit or port applies
    was_active = _run(_systemctl(scope, "is-active", UNIT_NAME)).stdout.strip() == "active"
    if not args.no_start:
        if not _retire_background_process("systemd runs Sentinel from now on"):
            return EXIT_ERROR
        if not was_active and _port_in_use(cfg.server.host, cfg.server.port):
            other = "another Sentinel" if _http_get(f"{url}/healthz", timeout=1.0) else "another program"
            _err(
                f"port {cfg.server.port} is already in use by {other} ({url}), so the unit could not start; "
                "stop it first or choose another port (--port N)"
            )
            return EXIT_ERROR
    elif _background_pid():
        _say(f"The background process (pid {_background_pid()}) keeps running until ./sentinel.sh stop.")
    start_now = not args.no_start and not was_active

    if scope == "user":
        target.parent.mkdir(parents=True, exist_ok=True)
        changed = not target.exists() or target.read_text(encoding="utf-8") != unit
        target.write_text(unit, encoding="utf-8")
        _say(f"{'Wrote' if changed else 'Unchanged'} {target}")
        steps = [["systemctl", "--user", "daemon-reload"]]
        steps.append(["systemctl", "--user", "enable", "--now" if start_now else "", UNIT_NAME])
        for step in steps:
            step = [s for s in step if s]
            res = _run(step)
            if res.returncode != 0:
                _err(f"{' '.join(step)} failed: {(res.stderr or res.stdout).strip()}")
                return EXIT_ERROR
        linger = _run(["loginctl", "enable-linger", user])
        if linger.returncode == 0:
            _say(f"Lingering enabled for {user}: the unit survives logout and starts at boot.")
        else:
            _say(f"Could not enable lingering ({(linger.stderr or linger.stdout).strip() or 'no loginctl'}).")
            _say(f"  Run once as root so the unit survives logout:  sudo loginctl enable-linger {user}")
    else:
        if not _is_root() and shutil.which("sudo") is None:
            _err("installing a system unit needs root or sudo; use --user for a user unit")
            return EXIT_ERROR
        current = SYSTEM_UNIT_PATH.read_text(encoding="utf-8") if SYSTEM_UNIT_PATH.exists() else None
        if current == unit:
            _say(f"Unchanged {SYSTEM_UNIT_PATH}")
        else:
            tee = _run(_sudo(["tee", str(SYSTEM_UNIT_PATH)]), input=unit)
            if tee.returncode != 0:
                _err(f"could not write {SYSTEM_UNIT_PATH}: {tee.stderr.strip()}")
                return EXIT_ERROR
            _run(_sudo(["chmod", "0644", str(SYSTEM_UNIT_PATH)]))
            _say(f"Wrote {SYSTEM_UNIT_PATH}")
        steps = [_sudo(["systemctl", "daemon-reload"])]
        enable = (
            ["systemctl", "enable", "--now", UNIT_NAME] if start_now else ["systemctl", "enable", UNIT_NAME]
        )
        steps.append(_sudo(enable))
        for step in steps:
            res = _run(step)
            if res.returncode != 0:
                _err(f"{' '.join(step)} failed: {(res.stderr or res.stdout).strip()}")
                return EXIT_ERROR
    if was_active and not args.no_start:
        res = _run(_systemctl(scope, "restart", UNIT_NAME, privileged=True))
        if res.returncode != 0:
            _err(f"systemctl restart {UNIT_NAME} failed: {(res.stderr or res.stdout).strip()}")
            return EXIT_ERROR
        _say(f"Restarted {UNIT_NAME} so the unit and {paths.config_path().name} take effect.")
    _say(f"Unit {UNIT_NAME}.service installed ({scope} scope) for user {user}.")
    if not args.no_start and not _verify_unit_started(scope, url):
        return EXIT_ERROR
    flag = "--user " if scope == "user" else ""
    _say(f"Logs:  journalctl {flag}-u {UNIT_NAME} -f      Manage: ./sentinel.sh start|stop|status|logs")
    return EXIT_OK


# ---------------------------------------------------------------- start/stop/status/logs


def _pid_paths() -> tuple[Path, Path, Path]:
    data_dir = paths.ensure_dirs(paths.data_dir())
    return data_dir, paths.pid_path(data_dir), paths.log_path(data_dir)


def _background_demo_pid(cfg: Config) -> int | None:
    """Pid of a live ``start --demo`` process that this (non-demo) command does not manage.

    The demo keeps its pid file in ``<repo>/data/demo`` unless ``SENTINEL_DATA_DIR`` is set, in
    which case it shares (and plain commands already see) the live pid file.
    """
    if cfg.demo or os.environ.get("SENTINEL_DATA_DIR", "").strip():
        return None
    pid = _read_pid(paths.pid_path(paths.REPO_DIR / "data" / "demo"))
    return pid if _pid_running(pid) else None


def cmd_start(args: argparse.Namespace) -> int:
    """Start Sentinel: through systemd when a unit is installed, else detached with a pid file."""
    cfg = _load(args)
    scope = unit_scope()
    if scope and not cfg.demo:
        if not _retire_background_process("systemd runs Sentinel from now on"):
            return EXIT_ERROR
        res = _run(_systemctl(scope, "start", UNIT_NAME, privileged=True))
        if res.returncode != 0:
            _err(f"systemctl start failed: {(res.stderr or res.stdout).strip()}")
            return EXIT_ERROR
        _say(f"Started {UNIT_NAME} ({scope} unit) · {base_url(cfg)}")
        return EXIT_OK
    data_dir, pid_file, log_file = _pid_paths()
    pid = _read_pid(pid_file)
    url = base_url(cfg)
    if _pid_running(pid):
        if _wait_healthz(url, timeout=ALREADY_RUNNING_GRACE):
            _say(f"Already running (pid {pid}) · {url}")
            return EXIT_OK
        _say(
            f"Already running (pid {pid}), but {url} does not answer: it is still starting, or it runs "
            f"with an older port/host than {paths.config_path()}. Apply the config with ./sentinel.sh restart"
        )
        return EXIT_WARN
    if _port_in_use(cfg.server.host, cfg.server.port):
        # a demo, `run`, another checkout or an unrelated program: our server could not bind
        other = "another Sentinel" if _http_get(f"{url}/healthz", timeout=1.0) else "another program"
        _err(
            f"port {cfg.server.port} is already in use by {other} ({url}); stop it first or choose "
            f"another port (--port N, or server.port in {paths.config_path()})"
        )
        return EXIT_ERROR
    cmd = [sys.executable, "-m", "dspace_sentinel", "serve"]
    if getattr(args, "host", None):
        cmd += ["--host", str(args.host)]
    if getattr(args, "port", None):
        cmd += ["--port", str(args.port)]
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    # the server runs in the checkout: relative locations must keep meaning the caller's directory
    for name in ("SENTINEL_DATA_DIR", "SENTINEL_CONFIG"):
        if env.get(name, "").strip():
            env[name] = str(Path(env[name].strip()).expanduser().absolute())
    with log_file.open("ab") as fh:
        try:
            proc = subprocess.Popen(  # noqa: S603 - our own interpreter
                cmd,
                cwd=str(paths.REPO_DIR),
                stdin=subprocess.DEVNULL,
                stdout=fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=env,
            )
        except OSError as exc:
            _err(f"could not start Sentinel: {exc}")
            return EXIT_ERROR
    pid_file.write_text(f"{proc.pid}\n")
    if _wait_healthz(url, timeout=float(args.timeout), proc=proc):
        if proc.poll() is None and _serves_port(proc.pid, cfg.server.port) is not False:
            mode = " (demo data)" if cfg.demo else ""
            _say(f"Started (pid {proc.pid}){mode} · {url}")
            _say(f"  log: {log_file}")
            return EXIT_OK
        _err(
            f"{url} answered, but not from the process just started (pid {proc.pid}); is another Sentinel there?"
        )
        if proc.poll() is None:
            _stop_pid(proc.pid)
        pid_file.unlink(missing_ok=True)
        return EXIT_ERROR
    if proc.poll() is not None:
        _err(f"Sentinel exited with code {proc.returncode} right after starting; last log lines:")
        for line in _tail(log_file, 15):
            _say(f"  {line}")
        pid_file.unlink(missing_ok=True)
        return EXIT_ERROR
    _say(f"Started (pid {proc.pid}) but {url}/healthz is not answering yet; check {log_file}")
    return EXIT_WARN


def _stop_pid(pid: int, timeout: float = 10.0) -> bool:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_running(pid):
            return True
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    time.sleep(0.5)
    return not _pid_running(pid)


def cmd_stop(args: argparse.Namespace) -> int:
    """Stop Sentinel (systemd unit or pid file); idempotent."""
    cfg = _load_or_default(args)
    scope = unit_scope()
    if scope and not cfg.demo:
        res = _run(_systemctl(scope, "stop", UNIT_NAME, privileged=True))
        if res.returncode != 0:
            _err(f"systemctl stop failed: {(res.stderr or res.stdout).strip()}")
            return EXIT_ERROR
        _say(f"Stopped {UNIT_NAME} ({scope} unit)")
        return EXIT_OK if _retire_background_process("it ran outside systemd") else EXIT_ERROR
    _data_dir, pid_file, _log = _pid_paths()
    pid = _read_pid(pid_file)
    if not _pid_running(pid):
        pid_file.unlink(missing_ok=True)
        demo_pid = _background_demo_pid(cfg)
        demo = f" (a background demo runs as pid {demo_pid}: ./sentinel.sh stop --demo)" if demo_pid else ""
        _say(f"Not running{demo}")
        return EXIT_OK
    if _stop_pid(int(pid or 0)):
        pid_file.unlink(missing_ok=True)
        _say(f"Stopped (pid {pid})")
        return EXIT_OK
    _err(f"could not stop pid {pid} (permission denied?)")
    return EXIT_ERROR


def cmd_restart(args: argparse.Namespace) -> int:
    """``stop`` then ``start`` (or ``systemctl restart``)."""
    cfg = _load_or_default(args)
    scope = unit_scope()
    if scope and not cfg.demo:
        if not _retire_background_process("systemd runs Sentinel from now on"):
            return EXIT_ERROR
        res = _run(_systemctl(scope, "restart", UNIT_NAME, privileged=True))
        if res.returncode != 0:
            _err(f"systemctl restart failed: {(res.stderr or res.stdout).strip()}")
            return EXIT_ERROR
        _say(f"Restarted {UNIT_NAME} ({scope} unit) · {base_url(cfg)}")
        return EXIT_OK
    rc = cmd_stop(args)
    if rc != EXIT_OK:
        return rc
    return cmd_start(args)


def status_info(cfg: Config) -> dict[str, Any]:
    """Everything ``status`` prints: mode, running, pid, url, mood, uptime, paths, db size."""
    scope = unit_scope()
    info: dict[str, Any] = {
        "version": __version__,
        "mode": f"systemd-{scope}" if scope and not cfg.demo else "pidfile",
        "running": False,
        "pid": None,
        "url": base_url(cfg),
        "demo": bool(cfg.demo),
        "data_dir": str(paths.data_dir()),
        "config": str(paths.config_path()),
        "unit": None,
    }
    if scope and not cfg.demo:
        info["unit"] = str(SYSTEM_UNIT_PATH if scope == "system" else user_unit_path())
        active = _run(_systemctl(scope, "is-active", UNIT_NAME)).stdout.strip()
        info["running"] = active == "active"
        info["unit_state"] = active or "unknown"
        pid_text = _run(_systemctl(scope, "show", "-p", "MainPID", "--value", UNIT_NAME)).stdout.strip()
        info["pid"] = int(pid_text) if pid_text.isdigit() and pid_text != "0" else None
        info["background_pid"] = _background_pid()  # a `start` process from before the unit
    else:
        pid = _read_pid(paths.pid_path(paths.data_dir()))
        info["running"] = _pid_running(pid)
        info["pid"] = pid if info["running"] else None
        info["log"] = str(paths.log_path(paths.data_dir()))
    db = paths.db_path(paths.data_dir())
    info["db_size"] = db.stat().st_size if db.exists() else 0
    if not info["running"]:
        # whatever answers there (the demo, `run`, another checkout) is not the instance we manage
        if _http_get(f"{info['url']}/healthz", timeout=1.0):
            info["other_instance"] = True
    else:
        meta = _api_get(cfg, f"{info['url']}/api/meta")
        status = _api_get(cfg, f"{info['url']}/api/status")
        if isinstance(meta, dict):
            info["responding"] = True
            info["started_ts"] = meta.get("started_ts")
            info["hostname"] = meta.get("hostname")
            info["setup_completed"] = meta.get("setup_completed")
            info["modules_on"] = [k for k, v in (meta.get("modules") or {}).items() if v]
        else:
            info["responding"] = False
        if isinstance(status, dict):
            info["mood"] = status.get("mood")
            info["headline"] = status.get("headline")
            info["checked_ago_s"] = status.get("checked_ago_s")
            info["alerts_open"] = len(status.get("alerts_open") or [])
    return info


def cmd_status(args: argparse.Namespace) -> int:
    """Show whether Sentinel runs, where, and how it feels (exit 0 running, 1 not)."""
    cfg = _load_or_default(args)
    info = status_info(cfg)
    if args.json:
        _say(json.dumps(info, indent=2, default=str))
        return EXIT_OK if info["running"] else EXIT_WARN
    state = "running" if info["running"] else "not running"
    if info.get("unit_state") and not info["running"]:
        state = f"not running ({info['unit_state']})"
    pid = f" · pid {info['pid']}" if info.get("pid") else ""
    _say(f"DSpace Sentinel v{__version__} — {state}{pid} · {info['mode']}")
    _say(f"  url:     {info['url']}" + ("  (demo data)" if info["demo"] else ""))
    if info.get("responding"):
        mood_value = info.get("mood") or "unknown"
        tag = TAG.get(mood_value, TAG["unknown"])
        started = info.get("started_ts")
        up = f" · up {fmt.duration(time.time() - started)}" if started else ""
        _say(f"  health:  {tag} {info.get('headline', '')}{up}")
        if info.get("alerts_open"):
            _say(f"  alerts:  {info['alerts_open']} open")
        mods = info.get("modules_on") or []
        _say(f"  modules: {', '.join(mods) if mods else 'server only'}")
    elif info["running"]:
        _say(
            f"  health:  the process is up but {info['url']} does not answer (still starting, or it "
            "listens on an older port/host: ./sentinel.sh restart applies the config)"
        )
    elif info.get("other_instance"):
        _say(f"  note:    another Sentinel answers on {info['url']} (a demo, `run` or another checkout?)")
    _say(f"  data:    {info['data_dir']} (db {fmt.bytes_(info['db_size'])})")
    _say(f"  config:  {info['config']}")
    if info.get("unit"):
        _say(f"  unit:    {info['unit']}")
    elif info.get("log"):
        _say(f"  log:     {info['log']}")
    if info.get("background_pid"):
        _say(
            f"  also:    a background process from `./sentinel.sh start` runs as pid {info['background_pid']}"
            " (./sentinel.sh stop ends it)"
        )
    demo_pid = _background_demo_pid(cfg)
    if demo_pid:
        _say(
            f"  demo:    a background demo runs as pid {demo_pid} (./sentinel.sh status --demo, stop --demo)"
        )
    return EXIT_OK if info["running"] else EXIT_WARN


def cmd_logs(args: argparse.Namespace) -> int:
    """Show the last lines of the Sentinel log (journal or ``data/sentinel.log``); ``-f`` follows."""
    cfg = _load_or_default(args)
    scope = unit_scope()
    if scope and not cfg.demo:
        cmd = ["journalctl"]
        if scope == "user":
            cmd.append("--user")
        cmd += ["-u", UNIT_NAME, "-n", str(args.lines), "--no-pager"]
        if args.follow:
            cmd.append("-f")
        try:
            return subprocess.call(cmd)  # noqa: S603 - fixed argv
        except FileNotFoundError:
            _err("journalctl not found")
            return EXIT_ERROR
        except KeyboardInterrupt:
            return EXIT_INTERRUPTED
    log_file = paths.log_path(paths.data_dir())
    if not log_file.exists():
        _say(f"No log yet at {log_file} (start Sentinel with `./sentinel.sh start`).")
        return EXIT_WARN
    for line in _tail(log_file, args.lines):
        _say(line)
    if not args.follow:
        return EXIT_OK
    try:
        with log_file.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            while True:
                chunk = fh.readline()
                if chunk:
                    sys.stdout.write(chunk.decode("utf-8", "replace"))
                    sys.stdout.flush()
                    continue
                time.sleep(0.5)
                try:
                    if log_file.stat().st_size < fh.tell():  # truncated / rotated
                        fh.seek(0)
                except OSError:
                    pass
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED


# ----------------------------------------------------------------------------- update


def _installed_version() -> str:
    """Version string read from disk (the imported module may predate a ``git pull``)."""
    init = paths.PACKAGE_DIR / "__init__.py"
    try:
        match = re.search(r'__version__\s*=\s*"([^"]+)"', init.read_text(encoding="utf-8"))
        return match.group(1) if match else __version__
    except OSError:
        return __version__


def cmd_update(args: argparse.Namespace) -> int:
    """``git pull --ff-only`` + ``pip install -r requirements.txt`` + restart if running."""
    repo = paths.REPO_DIR
    before = _installed_version()
    inside = _run(["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"])
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        _err(f"{repo} is not a git checkout; update it the way it was installed")
        return EXIT_ERROR
    head_before = _run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"]).stdout.strip()
    _say(f"Updating {repo} (currently v{before} @ {head_before or '?'})")
    pull = _run(["git", "-C", str(repo), "pull", "--ff-only"])
    if pull.returncode != 0:
        _err(f"git pull failed:\n{(pull.stderr or pull.stdout).strip()}")
        _say("  Local changes? Commit or stash them (git stash) and run update again.")
        return EXIT_ERROR
    _say("  " + (pull.stdout.strip().splitlines() or ["up to date"])[-1])
    head_after = _run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"]).stdout.strip()
    if not args.no_pip:
        _say("  Installing Python packages…")
        pip = _run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--quiet",
                "--disable-pip-version-check",
                "--progress-bar",
                "off",
                "-r",
                str(repo / "requirements.txt"),
            ]
        )
        if pip.returncode != 0:
            _err(f"pip install failed:\n{(pip.stderr or pip.stdout).strip()[-2000:]}")
            return EXIT_ERROR
    after = _installed_version()
    _say(f"Now v{after} @ {head_after or '?'}" + (" (no change)" if head_after == head_before else ""))
    if args.no_restart:
        return EXIT_OK
    cfg = _load_or_default(args)
    scope = unit_scope()
    if scope:
        if not _retire_background_process("systemd runs Sentinel from now on"):
            return EXIT_ERROR
        res = _run(_systemctl(scope, "restart", UNIT_NAME, privileged=True))
        _say(
            f"  restarted {UNIT_NAME} ({scope} unit)"
            if res.returncode == 0
            else f"  restart failed: {res.stderr.strip()}"
        )
        return EXIT_OK if res.returncode == 0 else EXIT_ERROR
    if _pid_running(_read_pid(paths.pid_path(paths.data_dir()))):
        _say("  restarting the background process")
        return cmd_restart(
            argparse.Namespace(demo=False, host=None, port=None, timeout=20.0, verbose=args.verbose)
        )
    _say("  Sentinel is not running; start it with ./sentinel.sh start")
    _ = cfg
    return EXIT_OK


# -------------------------------------------------------------------------- uninstall


def cmd_uninstall(args: argparse.Namespace) -> int:
    """Stop Sentinel and remove its systemd unit and pid file. Data and config are kept.

    ``--purge`` also deletes the data directory and ``config/sentinel.yaml`` (asks first
    unless ``--yes``). The virtualenv is never touched; ``rm -rf .venv`` removes it.
    """
    removed: list[str] = []
    systemctl = shutil.which("systemctl") is not None
    for scope, target in (("system", SYSTEM_UNIT_PATH), ("user", user_unit_path())):
        if not target.exists():
            continue
        if not is_our_unit(target) and not args.force:
            _say(
                f"Not touching {target}: it runs {unit_checkout(target) or 'another checkout'}, "
                f"not {paths.REPO_DIR} (uninstall --force removes it anyway)"
            )
            continue
        if systemctl:
            disable = _run(_systemctl(scope, "disable", "--now", UNIT_NAME, privileged=True))
            if disable.returncode != 0 and not args.force:
                _err(
                    f"systemctl disable --now {UNIT_NAME} failed: {(disable.stderr or disable.stdout).strip()}; "
                    f"{target} was left in place (uninstall --force removes it anyway)"
                )
                return EXIT_ERROR
        if scope == "system":
            res = _run(_sudo(["rm", "-f", str(target)]))
        else:
            try:
                target.unlink()
                res = subprocess.CompletedProcess([], 0, "", "")
            except OSError as exc:
                res = subprocess.CompletedProcess([], 1, "", str(exc))
        if res.returncode != 0:
            _err(f"could not remove {target}: {res.stderr.strip()}")
            return EXIT_ERROR
        removed.append(str(target))
        if systemctl:
            _run(_systemctl(scope, "daemon-reload", privileged=True))
            _run(_systemctl(scope, "reset-failed", UNIT_NAME, privileged=True))
        if scope == "user":
            user = _invoking_user()
            _say(
                f"Lingering stays enabled for {user} (other user services may rely on it); "
                f"turn it off with:  loginctl disable-linger {user}"
            )
    pid_file = paths.pid_path(paths.data_dir())
    pid = _read_pid(pid_file)
    if _pid_running(pid):
        if _stop_pid(int(pid or 0)):
            _say(f"Stopped background process (pid {pid})")
        else:
            _err(f"could not stop pid {pid}")
            return EXIT_ERROR
    if pid_file.exists():
        pid_file.unlink(missing_ok=True)
        removed.append(str(pid_file))
    for item in removed:
        _say(f"Removed {item}")
    if not removed:
        _say("Nothing installed to remove (no unit, no pid file).")
    if args.purge:
        data_dir = paths.data_dir()
        cfg_path = paths.config_path()
        if not args.yes:
            if not sys.stdin.isatty():
                _err("--purge needs --yes when not run interactively")
                return EXIT_ERROR
            answer = input(f"Delete {data_dir} and {cfg_path}? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                _say("Kept data and config.")
                return EXIT_OK
        if data_dir.exists():
            shutil.rmtree(data_dir, ignore_errors=True)
            _say(f"Removed {data_dir}")
        if cfg_path.exists():
            cfg_path.unlink(missing_ok=True)
            _say(f"Removed {cfg_path}")
    else:
        _say(
            f"Kept data in {paths.data_dir()} and config {paths.config_path()} (use --purge to delete them)."
        )
    _say(f"To remove the virtualenv:  rm -rf {paths.REPO_DIR / '.venv'}")
    return EXIT_OK


# ------------------------------------------------------------------------- set-password

LOGIN_SAVED = "Login saved for '{username}'. Sessions that were signed in have been signed out."
LOGIN_REMOVED = "Login removed: the dashboard no longer asks for a password."
NEEDS_A_TERMINAL = (
    "{command} asks for the password on a terminal; use --password-stdin to read it from standard input"
)


def _interactive() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _password_from_stdin() -> str:
    """One line of standard input without its line ending (``--password-stdin``)."""
    return sys.stdin.readline().rstrip("\r\n")


def _say_problem(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def _ask_username(default: str) -> str | None:
    """The username, asked on the terminal (``default`` on Enter); ``None`` after three misses."""
    from dspace_sentinel.auth import username_problem

    for _ in range(3):
        name = input(f"Username [{default}]: ").strip() or default
        problem = username_problem(name)
        if problem is None:
            return name
        _say_problem(problem)
    return None


def _ask_password(username: str) -> str | None:
    """A new password typed twice without echo; ``None`` after three misses. Never printed."""
    from dspace_sentinel.auth import password_problem

    for _ in range(3):
        first = getpass.getpass("Password: ")
        problem = password_problem(first, username)
        if problem:
            _say_problem(problem)
            continue
        if getpass.getpass("Repeat the password: ") != first:
            _say_problem("The passwords do not match.")
            continue
        return first
    return None


def cmd_set_password(args: argparse.Namespace) -> int:
    """Create or change the dashboard login (``server.login``), or ``--remove`` it.

    Interactive by default (the username, then the password twice, hidden); ``--password-stdin``
    reads one line for automation. The config is written through the file layer (mode 0600); a
    running Sentinel notices within seconds and signs out every session.
    """
    from dspace_sentinel import auth
    from dspace_sentinel.config import apply_login, save_config

    path = paths.config_path()
    cfg = load_config(path, demo=False)
    if args.remove:
        return _remove_login(cfg, path, yes=args.yes)
    if not args.password_stdin and not _interactive():
        _err(NEEDS_A_TERMINAL.format(command="set-password"))
        return EXIT_ERROR
    current = cfg.server.login.username
    default = current if current and auth.username_problem(current) is None else auth.DEFAULT_USERNAME
    try:
        if args.user is not None:
            username: str | None = args.user.strip()
        elif args.password_stdin:
            username = default
        else:
            username = _ask_username(default)
        if username is None:
            _err("no login saved")
            return EXIT_ERROR
        problem = auth.username_problem(username)
        if problem:
            _err(problem)
            return EXIT_ERROR
        if args.password_stdin:
            password: str | None = _password_from_stdin()
            problem = auth.password_problem(password, username)
            if problem:
                _err(problem)
                return EXIT_ERROR
        else:
            password = _ask_password(username)
    except EOFError:
        _err("no login saved (the input ended)")
        return EXIT_ERROR
    if password is None:
        _err("no login saved")
        return EXIT_ERROR
    apply_login(cfg, username, auth.hash_password(password))
    save_config(cfg, path)
    _say(LOGIN_SAVED.format(username=username))
    return EXIT_OK


def _remove_login(cfg: Config, path: Path, *, yes: bool) -> int:
    """``set-password --remove``: refused while Sentinel would then refuse to start."""
    from dspace_sentinel.api.app import UnsafeBindError, check_bind
    from dspace_sentinel.config import LoginConfig, apply_login, save_config

    login = cfg.server.login
    if not login.password_hash:
        _say("No login is set: the dashboard does not ask for one.")
        return EXIT_OK
    cfg.server.login = LoginConfig()
    try:
        check_bind(cfg)
    except UnsafeBindError:
        _err(
            f"not removing the login: Sentinel listens on {cfg.server.host or '0.0.0.0'} and without "
            "a login or server.auth_token it would refuse to start; set server.auth_token first, or "
            "listen on 127.0.0.1"
        )
        return EXIT_ERROR
    finally:
        cfg.server.login = login
    if not yes:
        if not _interactive():
            _err("--remove needs --yes when not run interactively")
            return EXIT_ERROR
        try:
            answer = input(
                f"Remove the login for '{login.username}'? The dashboard will not ask for a password. [y/N] "
            )
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            _say("Kept the login.")
            return EXIT_OK
    apply_login(cfg, "", "")
    save_config(cfg, path)
    _say(LOGIN_REMOVED)
    return EXIT_OK


def cmd_hash_password(args: argparse.Namespace) -> int:
    """Print only the hash of a password (``dspace_sentinel_login_password_hash`` of the Ansible role)."""
    from dspace_sentinel import auth

    if args.password_stdin:
        password: str | None = _password_from_stdin()
        problem = auth.password_problem(password or "")
        if problem:
            _err(problem)
            return EXIT_ERROR
    else:
        if not _interactive():
            _err(NEEDS_A_TERMINAL.format(command="hash-password"))
            return EXIT_ERROR
        try:
            password = _ask_password("")
        except EOFError:
            password = None
        if password is None:
            _err("no hash made")
            return EXIT_ERROR
    _say(auth.hash_password(password or ""))
    return EXIT_OK


# ------------------------------------------------------------------------------ version


def cmd_version(args: argparse.Namespace) -> int:
    """Print the version (``-v`` adds interpreter, paths and service state)."""
    _say(f"dspace-sentinel {__version__}")
    if args.verbose:
        scope = unit_scope()
        _say(f"python   {sys.version.split()[0]} ({sys.executable})")
        _say(f"repo     {paths.REPO_DIR}")
        _say(f"data     {paths.data_dir()}")
        _say(f"config   {paths.config_path()}")
        _say(f"service  {scope + ' unit installed' if scope else 'no systemd unit (pid file mode)'}")
    return EXIT_OK


# ------------------------------------------------------------------------------ parser


class _Parser(argparse.ArgumentParser):
    """argparse with usage errors on :data:`EXIT_USAGE` (argparse's own 2 means "critical" here)."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


DEMO_PROCESS_HELP = "the background demo (started with `start --demo`, pid in data/demo)"


def _add_runtime_flags(parser: argparse.ArgumentParser, demo_help: str | None = None) -> None:
    parser.add_argument("--host", help="bind address (default from config / SENTINEL_HOST)")
    parser.add_argument("--port", type=int, help="port (default from config / SENTINEL_PORT)")
    parser.add_argument("--demo", action="store_true", help=demo_help or argparse.SUPPRESS)


def build_parser() -> argparse.ArgumentParser:
    """The argparse parser with every sub-command."""
    parser = _Parser(
        prog="dspace-sentinel",
        description="DSpace Sentinel: observe-only health for DSpace servers.",
        epilog="exit codes: 0 ok · 1 warnings / not running · 2 critical · 3 not in this build · "
        "4 error (could not run) · 64 usage error",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--version", action="version", version=f"dspace-sentinel {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    p_serve = sub.add_parser("serve", help="run the web server in the foreground")
    _add_runtime_flags(p_serve, "demo mode with synthetic data")
    p_serve.set_defaults(func=cmd_serve)

    p_demo = sub.add_parser("demo", help="run in demo mode (synthetic DSpace stack) on port 9765")
    p_demo.add_argument("--host")
    p_demo.add_argument("--port", type=int)
    p_demo.set_defaults(func=cmd_demo)

    p_doctor = sub.add_parser("doctor", help="one-shot health check; exit 0 ok, 1 warnings, 2 critical")
    p_doctor.add_argument("--json", action="store_true", help="print the /api/status payload")
    p_doctor.add_argument("--ascii", action="store_true", help="plain ASCII box (no emoji, no box drawing)")
    p_doctor.add_argument(
        "--width", type=int, default=BOX_WIDTH, help=f"box width in columns (default {BOX_WIDTH})"
    )
    p_doctor.add_argument("--demo", action="store_true", help="check the demo stack instead of this host")
    p_doctor.set_defaults(func=cmd_doctor)

    p_report = sub.add_parser("report", help="generate a report and print its path")
    p_report.add_argument("--range", default="24h", help="24h | 7d | 30d (default 24h)")
    p_report.add_argument("--format", default="html", help="html | md | json (default html)")
    p_report.add_argument("--title", help="report title")
    p_report.add_argument("--telegram", action="store_true", help="also send the file to Telegram")
    p_report.add_argument("--email", action="store_true", help="also email the file to every recipient")
    p_report.add_argument("--json", action="store_true", help="print the report record as JSON")
    p_report.add_argument("--demo", action="store_true", help=argparse.SUPPRESS)
    p_report.set_defaults(func=cmd_report)

    p_disc = sub.add_parser("discover", help="detect DSpace configuration, ports and services")
    p_disc.add_argument("--apply", action="store_true", help="write the suggested config")
    p_disc.add_argument("--json", action="store_true")
    p_disc.add_argument("--demo", action="store_true", help=argparse.SUPPRESS)
    p_disc.set_defaults(func=cmd_discover)

    p_tg = sub.add_parser("telegram-test", help="send a Telegram test message through every bot (or one)")
    p_tg.add_argument("--to", metavar="ID_OR_CHAT_ID", help="test only the bot with this id or chat id")
    p_tg.add_argument("--json", action="store_true")
    p_tg.add_argument("--demo", action="store_true", help=argparse.SUPPRESS)
    p_tg.set_defaults(func=cmd_telegram_test)

    p_mail = sub.add_parser("email-test", help="send a test email to every recipient (or one)")
    p_mail.add_argument("--to", metavar="ADDRESS", help="test only this recipient")
    p_mail.add_argument("--json", action="store_true")
    p_mail.add_argument("--demo", action="store_true", help=argparse.SUPPRESS)
    p_mail.set_defaults(func=cmd_email_test)

    p_pw = sub.add_parser("set-password", help="create or change the dashboard login (or --remove it)")
    p_pw.add_argument("--user", metavar="NAME", help="the username (default: the current one, or admin)")
    p_pw.add_argument(
        "--password-stdin", action="store_true", help="read the password from one line of standard input"
    )
    p_pw.add_argument("--remove", action="store_true", help="remove the login: the dashboard stops asking")
    p_pw.add_argument("--yes", action="store_true", help="do not ask before removing the login")
    p_pw.set_defaults(func=cmd_set_password)

    p_hash = sub.add_parser("hash-password", help="print a password hash for server.login (Ansible)")
    p_hash.add_argument(
        "--password-stdin", action="store_true", help="read the password from one line of standard input"
    )
    p_hash.set_defaults(func=cmd_hash_password)

    p_unit = sub.add_parser("install-service", help="install the systemd unit (system, or --user)")
    p_unit.add_argument("--user", action="store_true", help="systemd --user unit (no sudo, enables linger)")
    p_unit.add_argument("--port", type=int, help="write this port into config before installing")
    p_unit.add_argument("--host", help="write this bind address into config before installing")
    p_unit.add_argument("--no-start", action="store_true", help="enable the unit but do not start it")
    p_unit.add_argument("--print", action="store_true", help="print the rendered unit and exit")
    p_unit.add_argument(
        "--force", action="store_true", help="replace a dspace-sentinel unit that runs another checkout"
    )
    p_unit.set_defaults(func=cmd_install_service)

    p_start = sub.add_parser(
        "start", help="start Sentinel (systemd unit, or background process with a pid file)"
    )
    _add_runtime_flags(p_start, "start the demo in the background")
    p_start.add_argument(
        "--timeout", type=float, default=20.0, help="seconds to wait for /healthz (default 20)"
    )
    p_start.set_defaults(func=cmd_start)

    p_stop = sub.add_parser("stop", help="stop Sentinel")
    p_stop.add_argument("--demo", action="store_true", help=DEMO_PROCESS_HELP)
    p_stop.set_defaults(func=cmd_stop)

    p_restart = sub.add_parser("restart", help="restart Sentinel")
    _add_runtime_flags(p_restart, DEMO_PROCESS_HELP)
    p_restart.add_argument("--timeout", type=float, default=20.0, help=argparse.SUPPRESS)
    p_restart.set_defaults(func=cmd_restart)

    p_status = sub.add_parser("status", help="show whether Sentinel is running (exit 0 yes, 1 no)")
    p_status.add_argument("--json", action="store_true")
    p_status.add_argument("--demo", action="store_true", help=DEMO_PROCESS_HELP)
    p_status.set_defaults(func=cmd_status)

    p_logs = sub.add_parser("logs", help="show the Sentinel log (journal or data/sentinel.log)")
    p_logs.add_argument("-n", "--lines", type=int, default=100, help="lines to show (default 100)")
    p_logs.add_argument("-f", "--follow", action="store_true", help="keep printing new lines")
    p_logs.add_argument("--demo", action="store_true", help=DEMO_PROCESS_HELP)
    p_logs.set_defaults(func=cmd_logs)

    p_update = sub.add_parser("update", help="git pull --ff-only, pip install, restart if running")
    p_update.add_argument("--no-pip", action="store_true", help="skip pip install")
    p_update.add_argument("--no-restart", action="store_true", help="do not restart after updating")
    p_update.set_defaults(func=cmd_update)

    p_un = sub.add_parser("uninstall", help="remove the systemd unit and pid file (keeps data and config)")
    p_un.add_argument(
        "--purge", action="store_true", help="also delete the data dir and config/sentinel.yaml"
    )
    p_un.add_argument("--yes", action="store_true", help="do not ask before purging")
    p_un.add_argument(
        "--force",
        action="store_true",
        help="also remove a unit that runs another checkout, even when systemctl disable fails",
    )
    p_un.set_defaults(func=cmd_uninstall)

    p_ver = sub.add_parser("version", help="print the version")
    p_ver.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="also print the interpreter, paths and service state",
    )
    p_ver.set_defaults(func=cmd_version)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m dspace_sentinel`` and the ``dspace-sentinel`` script."""
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    try:
        # the demo keeps its own port, auth and (unsent) Telegram settings: no real secrets
        if not (args.command == "demo" or getattr(args, "demo", False) or _env_truthy("SENTINEL_DEMO")):
            load_env_file()
        return int(args.func(args))
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    except BrokenPipeError:  # `logs -f | head`: the reader left; nothing more to say
        _silence_stdout()
        return EXIT_OK
    except ValueError as exc:
        _err(str(exc))
        return EXIT_ERROR
    except OSError as exc:
        if args.verbose:
            raise
        _err(f"{exc.__class__.__name__}: {exc}")
        return EXIT_ERROR
    except Exception as exc:
        # a bug must not surface as Python's exit 1, which means "warnings" here
        if args.verbose:
            raise
        _err(f"{exc.__class__.__name__}: {exc} (run with -v for the traceback)")
        return EXIT_ERROR


__all__ = [
    "BOX_WIDTH",
    "EXIT_CRIT",
    "EXIT_ERROR",
    "EXIT_NOT_IMPLEMENTED",
    "EXIT_OK",
    "EXIT_USAGE",
    "EXIT_WARN",
    "LOGIN_REMOVED",
    "LOGIN_SAVED",
    "REPORT_RANGES",
    "UNIT_NAME",
    "Box",
    "asciify",
    "base_url",
    "build_parser",
    "display_width",
    "exit_code_for",
    "main",
    "render_doctor",
    "render_unit",
    "status_info",
    "is_our_unit",
    "unit_checkout",
    "unit_scope",
    "user_unit_path",
]

if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
