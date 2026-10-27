"""Discovery: read DSpace's configuration, list ports, probe systemd units (SPEC section 6).

Everything here is read-only. ``discover`` never raises for missing files or
unavailable systemd; it simply reports what it could find. In demo mode it describes the demo
stack (``collectors/demo.py``) instead of looking at the real machine.

It knows how DSpace 7, 8 and 9 (and 10) are really run: the backend under packaged Tomcat
(``tomcat9`` on Ubuntu 22.04 for 7.x; ``tomcat10`` on Ubuntu 24.04 and Debian 12/13 for 8+;
``tomcat11`` on Debian 13) or as the runnable JAR of 8+ under a unit of its own (``dspace``,
``dspace-backend``, ``dspace-server``); the UI under a unit of its own or PM2, whose
``pm2 startup`` unit is ``pm2-<user>``. ``assetstore.dir`` defaults in
``config/modules/assetstore.cfg``, and ``solr.multicorePrefix`` prefixes every Solr core.

It also knows servers set up by hand: Solr, the UI or a web server that answers without a
systemd unit gets an empty service name (its ladder then has no "unit not found" rung) and a
finding that says so. On a DSpace host the Solr and nginx log entries are suggested only where
that log is there or that service was found, Solr's following the ``-Dsolr.log.dir`` a running
Solr was started with.
"""

from __future__ import annotations

import asyncio
import copy
import fnmatch
import logging
import os
import re
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import psutil

from dspace_sentinel import dspace_version
from dspace_sentinel.config import DEFAULT_CONFIG, MASK, Config
from dspace_sentinel.util import systemd
from dspace_sentinel.util.httpc import make_client, probe
from dspace_sentinel.util.net import ListeningSocket, listening_sockets

log = logging.getLogger(__name__)

_VAR = re.compile(r"\$\{([A-Za-z0-9_.\-]+)\}")
_JDBC = re.compile(
    r"^jdbc:postgresql://(?P<host>[^:/?]+)(?::(?P<port>\d+))?/(?P<db>[^?;]+)(?:\?(?P<query>[^;]*))?",
    re.IGNORECASE,
)

BACKEND_UNITS: tuple[str, ...] = (
    "tomcat9",
    "tomcat10",
    "tomcat11",
    "tomcat",
    "dspace-backend",
    "dspace-server",
    "dspace",
)
"""The backend's unit, in pick order: packaged Tomcat, then the runnable JAR's own unit (8+)."""

FRONTEND_UNITS: tuple[str, ...] = ("dspace-ui", "dspace-angular", "dspace-frontend", "pm2-*")
"""The UI's unit, in pick order; ``pm2-<user>`` is what ``pm2 startup`` installs."""

UNIT_CANDIDATES: tuple[str, ...] = (
    "postgresql",
    "postgresql@*",
    "solr",
    *BACKEND_UNITS,
    *FRONTEND_UNITS,
    "*dspace*",
    "nginx",
    "apache2",
)
"""Units ``probe_units`` asks systemd about (``*`` = every loaded unit matching the glob)."""

SENTINEL_UNIT_PREFIX = "dspace-sentinel"
"""DSpace Sentinel's own unit, which the ``*dspace*`` glob would otherwise report."""

CONFIG_FILES: tuple[str, ...] = ("local.cfg", "dspace.cfg", "modules/assetstore.cfg")
"""DSpace's configuration files under ``[dspace]/config``, highest precedence first."""

VERSION_TIMEOUT_S = 3.0
"""How long discovery waits for the REST API's and Solr's version answers."""

_TOMCAT_UNIT = re.compile(r"^tomcat\d+$")
_PACKAGED_CATALINA = re.compile(r"^/var/log/tomcat\d*/catalina\.out$")
CLI_LOG_NAME = "dspace-cli.log"

SOLR_LOG = "/var/solr/logs/solr.log"
"""The default Solr log entry: where a Solr installed as a service (``install_solr_service.sh``) logs."""
NGINX_LOG = "/var/log/nginx/error.log"
"""The default nginx log entry: the packaged nginx's error log."""
SOLR_LOG_DIR_ARG = "-Dsolr.log.dir="
"""How Solr's start script (``bin/solr``) hands Solr its log folder."""

KNOWN_PORTS: dict[int, str] = {
    22: "SSH",
    80: "HTTP",
    443: "HTTPS",
    4000: "DSpace UI (Angular)",
    5432: "PostgreSQL",
    8080: "DSpace backend (Tomcat)",
    8983: "Solr",
}

DEMO_PROPERTIES: dict[str, str] = {
    "dspace.dir": "/dspace",
    "dspace.server.url": "http://localhost:8080/server",
    "dspace.ui.url": "http://localhost:4000",
    "db.url": "jdbc:postgresql://localhost:5432/dspace",
    "db.username": "dspace",
    "solr.server": "http://localhost:8983/solr",
    "assetstore.dir": "/dspace/assetstore",
    "log.report.dir": "/dspace/log",
}
"""What the demo's ``/dspace/config/local.cfg`` would say (no secrets); ``dspace.name`` is added
from ``collectors/demo.py``."""

DEMO_UNITS: dict[str, str] = {
    "postgresql": "active",
    "solr": "active",
    "tomcat10": "active",
    "dspace-ui": "active",
    "nginx": "active",
}
"""The demo stack's systemd units (DSpace 9.4 on Ubuntu 24.04: packaged Tomcat 10)."""

DEMO_SOCKETS: tuple[ListeningSocket, ...] = (
    ListeningSocket(22, "0.0.0.0", True, "ipv4"),
    ListeningSocket(80, "0.0.0.0", True, "ipv4"),
    ListeningSocket(443, "0.0.0.0", True, "ipv4"),
    ListeningSocket(4000, "0.0.0.0", True, "ipv4"),
    ListeningSocket(8080, "0.0.0.0", True, "ipv4"),
    ListeningSocket(8983, "127.0.0.1", False, "ipv4"),
    ListeningSocket(5432, "127.0.0.1", False, "ipv4"),
)
"""The demo stack's listening sockets (Solr and PostgreSQL local only)."""

DEMO_FILES: frozenset[str] = frozenset({SOLR_LOG, NGINX_LOG})
"""The files discovery finds on the demo stack: Solr's and nginx's logs (no ``dspace-cli.log``)."""


PROPERTY_KEYS: tuple[str, ...] = (
    "dspace.dir",
    "dspace.name",
    "dspace.server.url",
    "dspace.ui.url",
    "db.url",
    "db.username",
    "solr.server",
    "solr.multicorePrefix",
    "assetstore.dir",
    "assetstore.index.primary",
    "log.dir",
    "log.report.dir",
)
"""The only DSpace properties :meth:`DiscoveryResult.to_dict` returns (URLs redacted)."""

_USERINFO = re.compile(r"//[^/@\s]*@")
_URL_PASSWORD = re.compile(r"(//[^/:@\s]*:)[^/@\s]*@")


def redact_url(url: str) -> str:
    """``url`` without credentials or query: ``jdbc:postgresql://h:5432/db?password=x`` → ``…/db``."""
    return re.split(r"[?;]", _USERINFO.sub("//", url.strip()), maxsplit=1)[0]


def mask_url_password(url: str) -> str:
    """``http://user:pass@host/solr`` → ``http://user:••••••@host/solr`` (unchanged without a password)."""
    return _URL_PASSWORD.sub(lambda m: f"{m.group(1)}{MASK}@", url, count=1)


def masked_suggestion(patch: dict[str, Any]) -> dict[str, Any]:
    """A copy of a suggested config patch with the DB password and a Solr URL password masked."""
    out = copy.deepcopy(patch)
    pg = out.get("postgres")
    if isinstance(pg, dict) and pg.get("password"):
        pg["password"] = MASK
    solr = out.get("solr")
    if isinstance(solr, dict) and isinstance(solr.get("url"), str):
        solr["url"] = mask_url_password(solr["url"])
    return out


def _pg_target(pg: dict[str, Any]) -> tuple[str, int | None, str, str]:
    try:
        port: int | None = int(pg.get("port"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        port = None
    return (
        str(pg.get("host", "")).strip().lower(),
        port,
        str(pg.get("database", "")).strip(),
        str(pg.get("user", "")).strip(),
    )


def restore_secrets(patch: dict[str, Any], current: Config, discovered: dict[str, Any]) -> dict[str, Any]:
    """Put the secrets :meth:`DiscoveryResult.to_dict` masked back into a patch built from it.

    ``discovered`` is the raw ``suggested_config`` of a recent discovery. ``postgres.password``
    sent as :data:`MASK` becomes the discovered password only when the patch's PostgreSQL target
    (host, port, database, user; merged over ``current``) is the one discovery found, so that
    password can only ever go to the server DSpace itself uses it for. A Solr URL equal to the
    masked one discovery returned becomes the real URL.
    """
    out = copy.deepcopy(patch)
    found_pg = discovered.get("postgres") if isinstance(discovered.get("postgres"), dict) else {}
    pg = out.get("postgres")
    if isinstance(pg, dict) and pg.get("password") == MASK and found_pg.get("password"):
        base = current.postgres.model_dump()
        if _pg_target({**base, **pg}) == _pg_target({**base, **found_pg}):
            pg["password"] = found_pg["password"]
    found_solr = discovered.get("solr") if isinstance(discovered.get("solr"), dict) else {}
    found_url = found_solr.get("url")
    solr = out.get("solr")
    if isinstance(solr, dict) and found_url and solr.get("url") == mask_url_password(found_url):
        solr["url"] = found_url
    return out


@dataclass
class DiscoveryResult:
    """What ``discover`` found: human ``findings``, a config patch and the persona it suggests."""

    findings: list[str] = field(default_factory=list)
    suggested_config: dict[str, Any] = field(default_factory=dict)
    detected_persona: str = "server"
    sources: list[str] = field(default_factory=list)
    properties: dict[str, str] = field(default_factory=dict)
    ports: list[dict[str, Any]] = field(default_factory=list)
    services: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON-serialisable dict without secrets (what the API and the CLI show).

        ``suggested_config`` carries :data:`MASK` for the DB password and for a password inside
        the Solr URL (:func:`restore_secrets` puts them back server-side when the patch is
        applied); ``properties`` keeps only :data:`PROPERTY_KEYS`, URLs without credentials or
        query strings.
        """
        data = asdict(self)
        data["suggested_config"] = masked_suggestion(self.suggested_config)
        data["properties"] = {
            key: redact_url(value) if key.endswith((".url", ".server")) else value
            for key, value in self.properties.items()
            if key in PROPERTY_KEYS
        }
        return data


# ------------------------------------------------------------------ properties files


def parse_properties(text: str) -> dict[str, str]:
    """Parse a DSpace ``.cfg`` (Apache Commons Configuration flavour) into ``{key: value}``.

    Supports ``key = value`` / ``key: value``, ``\\`` line continuations, ``#``/``!`` comments
    and ``key += value`` (appended with a comma). Later duplicates of a key override earlier
    ones *within the same file*; :func:`load_dspace_config` handles precedence across files.
    """
    props: dict[str, str] = {}
    logical: list[str] = []
    pending = ""
    for raw in text.splitlines():
        line = raw.rstrip("\r\n")
        if pending:
            line = pending + line.lstrip()
            pending = ""
        stripped = line.strip()
        if stripped.endswith("\\") and not stripped.endswith("\\\\"):
            pending = line.rstrip()[:-1]
            continue
        logical.append(line)
    if pending:
        logical.append(pending)
    for line in logical:
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("!"):
            continue
        append = False
        m = re.match(r"^([^=:\s]+)\s*(\+=|=|:)\s*(.*)$", s)
        if not m:
            continue
        key, op, value = m.group(1).strip(), m.group(2), m.group(3).strip()
        append = op == "+="
        if value.startswith(("'", '"')) and value.endswith(value[0]) and len(value) >= 2:
            value = value[1:-1]
        if append and key in props and props[key]:
            props[key] = f"{props[key]},{value}"
        else:
            props[key] = value
    return props


def interpolate(props: dict[str, str], max_depth: int = 10) -> dict[str, str]:
    """Resolve ``${var}`` references (e.g. ``${dspace.dir}``) against ``props`` up to ``max_depth`` levels."""
    out = dict(props)
    for _ in range(max_depth):
        changed = False
        for key, value in out.items():
            if "${" not in value:
                continue

            def sub(m: re.Match[str]) -> str:
                ref = m.group(1)
                return out.get(ref, m.group(0))

            new = _VAR.sub(sub, value)
            if new != value:
                out[key] = new
                changed = True
        if not changed:
            break
    return out


def load_dspace_config(install_dir: str | Path) -> tuple[dict[str, str], list[str]]:
    """Read ``<install_dir>/config/`` :data:`CONFIG_FILES`; earlier files win on duplicate keys.

    ``local.cfg``, then ``dspace.cfg``, then ``modules/assetstore.cfg`` (where DSpace 7–10 keep
    ``assetstore.dir``'s default; ``dspace.cfg`` only mentions it in a comment). Returns
    ``(interpolated_properties, files_read)``. ``dspace.dir`` defaults to ``install_dir`` when no
    file sets it, so ``${dspace.dir}`` references still resolve.
    """
    install = Path(install_dir)
    merged: dict[str, str] = {}
    sources: list[str] = []
    for name in CONFIG_FILES:
        path = install / "config" / name
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeError):
            continue
        sources.append(str(path))
        for key, value in parse_properties(text).items():
            merged.setdefault(key, value)
    if merged and "dspace.dir" not in merged:
        merged["dspace.dir"] = str(install)
    return interpolate(merged), sources


def parse_jdbc_url(url: str) -> dict[str, Any] | None:
    """``jdbc:postgresql://host:port/db?sslmode=x`` → ``{host, port, database[, sslmode]}``.

    The port defaults to 5432; ``sslmode`` is included only when the JDBC query string sets it,
    so the PostgreSQL collector connects the way DSpace itself does.
    """
    m = _JDBC.match(url.strip())
    if not m:
        return None
    out: dict[str, Any] = {
        "host": m.group("host"),
        "port": int(m.group("port") or 5432),
        "database": m.group("db"),
    }
    for pair in (m.group("query") or "").split("&"):
        key, _, value = pair.partition("=")
        if key.strip().lower() == "sslmode" and value.strip():
            out["sslmode"] = value.strip()
    return out


def _first(props: dict[str, str], *keys: str) -> str | None:
    for key in keys:
        value = props.get(key)
        if value:
            return value.strip()
    return None


def _host_of(url: str | None) -> str:
    if not url:
        return ""
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


def assetstore_on_s3(props: dict[str, str]) -> bool:
    """True when DSpace stores new files in Amazon S3 (``assetstore.index.primary = 1``)."""
    return (props.get("assetstore.index.primary") or "").strip() == "1"


def cli_log_path(props: dict[str, str]) -> str | None:
    """Where DSpace 7.6.3+ / 8.1+ scripts log (``[dspace]/log/dspace-cli.log``), when the log dir is known."""
    log_dir = _first(props, "log.report.dir", "log.dir")
    return f"{log_dir.rstrip('/')}/{CLI_LOG_NAME}" if log_dir else None


def suggest_from_properties(
    props: dict[str, str],
    install_dir: str,
    log_files: list[dict[str, Any]] | None = None,
    *,
    cli_log: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    """Build the config patch and findings implied by DSpace's properties.

    ``log_files`` is the current ``logs.files`` (default: the built-in list); the suggestion only
    moves the ``dspace`` entry to DSpace's log directory and keeps every other entry, adding a
    ``dspace-cli`` entry after it when ``cli_log`` says that file exists. A
    ``solr.multicorePrefix`` prefixes the suggested ``solr.cores``; an S3 assetstore suggests no
    ``assetstore_dir``.
    """
    patch: dict[str, Any] = {}
    findings: list[str] = []
    dspace_dir = props.get("dspace.dir") or install_dir
    dspace_patch: dict[str, Any] = {"install_dir": dspace_dir}
    name = _first(props, "dspace.name")
    if name:
        findings.append(f'DSpace instance "{name}" configured in {dspace_dir}')
    else:
        findings.append(f"DSpace configuration found in {dspace_dir}")

    db_url = _first(props, "db.url")
    if db_url:
        parsed = parse_jdbc_url(db_url)
        if parsed:
            pg: dict[str, Any] = dict(parsed)
            user = _first(props, "db.username")
            if user:
                pg["user"] = user
            password = props.get("db.password")
            if password:
                pg["password"] = password
            patch["postgres"] = pg
            findings.append(
                f"Database {parsed['database']} on {parsed['host']}:{parsed['port']}"
                + (f" as {user}" if user else "")
            )
        else:
            findings.append(f"db.url is not a PostgreSQL JDBC URL: {redact_url(db_url)}")

    solr = _first(props, "solr.server")
    if solr:
        patch["solr"] = {"url": solr.rstrip("/")}
        findings.append(f"Solr at {redact_url(solr)}")
    prefix = (props.get("solr.multicorePrefix") or "").strip()
    if prefix:
        patch.setdefault("solr", {})["cores"] = [prefix + core for core in dspace_version.REQUIRED_CORES]
        findings.append(f"Solr core names start with {prefix} (solr.multicorePrefix)")

    server_url = _first(props, "dspace.server.url")
    if server_url:
        dspace_patch["server_url"] = server_url.rstrip("/")
        findings.append(f"REST API at {redact_url(server_url)}")
    ui_url = _first(props, "dspace.ui.url")
    if ui_url:
        dspace_patch["ui_url"] = ui_url.rstrip("/")
        findings.append(f"Angular UI at {redact_url(ui_url)}")
        host = _host_of(ui_url)
        if host and host not in ("localhost", "127.0.0.1") and ui_url.startswith("https://"):
            dspace_patch["public_url"] = ui_url.rstrip("/")
            patch.setdefault("ssl", {})["host"] = host
            patch.setdefault("nginx", {})["https_url"] = ui_url.rstrip("/")
    assetstore = _first(props, "assetstore.dir")
    if assetstore_on_s3(props):
        findings.append(
            "The assetstore is on Amazon S3 (assetstore.index.primary = 1): there is no local directory "
            "to watch, so the Assetstore module stays off"
        )
    elif assetstore:
        dspace_patch["assetstore_dir"] = assetstore
        findings.append(f"Assetstore in {assetstore}")
    log_dir = _first(props, "log.report.dir", "log.dir")
    if log_dir:
        dspace_patch["log_dir"] = log_dir
        dspace_log = f"{log_dir.rstrip('/')}/dspace.log"
        current = log_files if log_files is not None else DEFAULT_CONFIG["logs"]["files"]
        files = [dict(entry) for entry in current]
        for entry in files:
            if entry.get("id") == "dspace":
                entry["path"] = dspace_log
                break
        else:
            files.insert(
                0,
                {"id": "dspace", "label": "DSpace", "path": dspace_log, "daily_suffix": True, "timezone": ""},
            )
        cli_path = cli_log_path(props)
        known = {entry.get("id") for entry in files} | {entry.get("path") for entry in files}
        if cli_log and cli_path and "dspace-cli" not in known and cli_path not in known:
            at = next((i + 1 for i, entry in enumerate(files) if entry.get("id") == "dspace"), 0)
            files.insert(
                at,
                {
                    "id": "dspace-cli",
                    "label": "DSpace CLI",
                    "path": cli_path,
                    "daily_suffix": True,
                    "timezone": "",
                },
            )
            findings.append(f"DSpace scripts log to {cli_path}: added as the DSpace CLI log")
        patch["logs"] = {"files": files}
    patch["dspace"] = dspace_patch
    return patch, findings


# ------------------------------------------------------------------------- probes


def _is_sentinel(unit: str) -> bool:
    return unit.startswith(SENTINEL_UNIT_PREFIX)


async def probe_units(candidates: tuple[str, ...] = UNIT_CANDIDATES) -> dict[str, str]:
    """State of every candidate unit (a glob such as ``postgresql@*`` expands to the loaded units)."""
    states: dict[str, str] = {}
    if systemd.systemctl_path() is None:
        return states
    for unit in candidates:
        try:
            if unit.endswith("*"):
                for name, active in await systemd.list_units(unit):
                    if not _is_sentinel(name):
                        states[name] = active if active in ("active", "inactive", "failed") else "unknown"
                continue
            state = await systemd.is_active(unit)
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("probe %s failed: %s", unit, exc)
            state = "unknown"
        if state != "missing":
            states[unit] = state
    return states


def _pick_unit(states: dict[str, str], *names: str, prefer_active: bool = False) -> str | None:
    """The first unit named by ``names`` (in order; a name with ``*`` is a glob) that ``states`` has.

    With ``prefer_active`` an active unit wins over one named earlier that is not running (a
    ``tomcat9`` left behind by an upgrade to ``tomcat10``).
    """

    def first(pool: dict[str, str]) -> str | None:
        for name in names:
            if "*" not in name:
                if name in pool:
                    return name
                continue
            for unit in pool:
                if fnmatch.fnmatchcase(unit, name):
                    return unit
        return None

    if prefer_active:
        running = first({unit: state for unit, state in states.items() if state == "active"})
        if running:
            return running
    return first(states)


def _unitless(what: str, port: int) -> str:
    """The finding for a service that answers on ``port`` while systemd has no unit for it."""
    return (
        f"{what} answers on {port} but no systemd unit was found for it: it may not start by itself "
        "after a reboot"
    )


def _tomcat_logs(
    files: list[dict[str, Any]],
    backend_unit: str | None,
    states: dict[str, str],
    backend_seen: bool,
    exists: Callable[[str], bool],
) -> tuple[list[dict[str, Any]], str | None]:
    """``logs.files`` with the packaged Tomcat log following the runtime, plus a finding.

    ``tomcatN`` → ``/var/log/tomcatN/catalina.out`` (Ubuntu/Debian packages). No Tomcat unit at
    all while a backend is there (the runnable JAR of 8+, whose Tomcat messages go to
    ``dspace.log``) → packaged ``catalina.out`` entries that do not exist are left out. Custom
    paths are never touched.
    """
    packaged = [e for e in files if _PACKAGED_CATALINA.match(str(e.get("path", "")))]
    if not packaged:
        return files, None
    if backend_unit and _TOMCAT_UNIT.match(backend_unit):
        target = f"/var/log/{backend_unit}/catalina.out"
        return [{**e, "path": target} if e in packaged else e for e in files], None
    if backend_seen and not any(unit.startswith("tomcat") for unit in states):
        gone = [e for e in packaged if not exists(str(e["path"]))]
        if gone:
            paths = ", ".join(str(e["path"]) for e in gone)
            finding = (
                f"No Tomcat unit: the Tomcat log ({paths}) is left out; the runnable JAR of DSpace 8+ "
                "writes its Tomcat messages to dspace.log"
            )
            return [e for e in files if e not in gone], finding
    return files, None


def _file_exists(path: str) -> bool:
    try:
        return os.path.isfile(path)
    except OSError:  # pragma: no cover - defensive
        return False


def _may_exist(path: str) -> bool:
    """False only when ``path`` is certainly missing; in a folder Sentinel may not enter it may be there."""
    try:
        os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True
    return True


def solr_log_dir(cmdlines: Iterable[Sequence[str]]) -> str | None:
    """The folder a running Solr logs to: the first absolute ``-Dsolr.log.dir=<dir>`` in ``cmdlines``."""
    for args in cmdlines:
        for arg in args:
            if not arg.startswith(SOLR_LOG_DIR_ARG):
                continue
            folder = arg[len(SOLR_LOG_DIR_ARG) :].strip().rstrip("/")
            if folder.startswith("/"):
                return folder
    return None


def process_cmdlines() -> list[list[str]]:
    """The command lines of the running processes that name a Solr log folder (``[]`` when unreadable).

    psutil reads ``/proc/<pid>/cmdline``, which every user may read for every process (unless
    ``/proc`` is mounted with ``hidepid``); a process whose command line cannot be read is skipped.
    """
    found: list[list[str]] = []
    try:
        for proc in psutil.process_iter(["cmdline"]):
            args = [str(arg) for arg in (proc.info.get("cmdline") or [])]
            if any(arg.startswith(SOLR_LOG_DIR_ARG) for arg in args):
                found.append(args)
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("could not list the running processes: %s", exc)
    return found


async def _service_logs(
    files: list[dict[str, Any]],
    *,
    has_web: bool,
    there: Callable[[str], Awaitable[bool]],
    cmdlines: list[list[str]] | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """``logs.files`` with the default nginx and Solr entries only where this machine has them, plus findings.

    The nginx entry stays for a web server that was found or a log that is there. The Solr entry
    stays when its log is there, else follows the ``-Dsolr.log.dir`` of a running Solr (from
    ``cmdlines``; read from the process list when ``None``) or is left out. Custom paths are never
    touched.
    """
    out: list[dict[str, Any]] = []
    findings: list[str] = []
    for entry in files:
        path = str(entry.get("path", ""))
        if path == NGINX_LOG and not has_web and not await there(path):
            findings.append(f"No web server was found: the nginx log ({path}) is left out")
            continue
        if path == SOLR_LOG and not await there(path):
            if cmdlines is None:
                cmdlines = await asyncio.to_thread(process_cmdlines)
            folder = solr_log_dir(cmdlines)
            if folder is None:
                findings.append(f"The Solr log {path} does not exist: it is left out")
                continue
            moved = f"{folder}/solr.log"
            if moved != path:
                findings.append(f"Solr logs to {moved} (its -Dsolr.log.dir): used as the Solr log")
            out.append({**entry, "path": moved})
            continue
        out.append(entry)
    return out, findings


async def probe_versions(
    server_url: str | None,
    solr_url: str | None,
    *,
    verify: bool = True,
    timeout: float = VERSION_TIMEOUT_S,
    client: httpx.AsyncClient | None = None,
) -> dict[str, str | None]:
    """Ask the running services for their versions: ``{dspace, cris, solr}`` (``None`` = no answer).

    Two anonymous GETs, the ones the collectors send every cycle: ``<server_url>/api``
    (``dspaceVersion``, DSpace-CRIS's ``crisVersion``) and ``<solr_url>/admin/info/system``
    (``lucene.solr-spec-version``). A URL of ``None`` is not asked. Never raises.
    """
    out: dict[str, str | None] = {"dspace": None, "cris": None, "solr": None}
    if not server_url and not solr_url:
        return out
    own = client is None
    http = client if client is not None else make_client(verify=verify, timeout=timeout)
    try:
        rest, info = await asyncio.gather(
            probe(http, f"{server_url.rstrip('/')}/api", expect_json=True) if server_url else _nothing(),
            probe(http, f"{solr_url.rstrip('/')}/admin/info/system", params={"wt": "json"}, expect_json=True)
            if solr_url
            else _nothing(),
        )
    finally:
        if own:
            await http.aclose()
    root = rest.json if rest is not None and rest.ok and isinstance(rest.json, dict) else {}
    for key, field_name in (("dspace", "dspaceVersion"), ("cris", "crisVersion")):
        value = root.get(field_name)
        out[key] = value.strip() if isinstance(value, str) and value.strip() else None
    lucene = info.json.get("lucene") if info is not None and info.ok and isinstance(info.json, dict) else None
    spec = lucene.get("solr-spec-version") if isinstance(lucene, dict) else None
    out["solr"] = str(spec).strip() if spec else None
    return out


async def _nothing() -> None:
    return None


def version_findings(
    versions: dict[str, str | None], *, server_url: str | None = None, solr_url: str | None = None
) -> list[str]:
    """Findings from the versions the services report: what answers, 7.x end of life, a Solr mismatch.

    ``"DSpace 9.4 answers at …"``, ``"DSpace 7.x is end-of-life (7.6.7 was the final release;
    supported: 8.x, 9.x, 10.x)"``, ``"DSpace 9 expects Solr 9 but Solr 8.11.4 answers"``.
    """
    findings: list[str] = []
    v = dspace_version.parse(versions.get("dspace"))
    solr = versions.get("solr")
    if v is not None:
        cris = versions.get("cris")
        what = f"DSpace {v.text}" + (f" (DSpace-CRIS {cris})" if cris else "")
        findings.append(f"{what} answers at {redact_url(server_url)}" if server_url else f"{what} answers")
        if dspace_version.end_of_life(v):
            supported = ", ".join(f"{major}.x" for major in dspace_version.SUPPORTED_MAJORS)
            final = f"{dspace_version.FINAL_7X_RELEASE} was the final release; " if v.major == 7 else ""
            findings.append(f"DSpace {v.major}.x is end-of-life ({final}supported: {supported})")
    if solr:
        findings.append(
            f"Solr {solr} answers at {redact_url(solr_url)}" if solr_url else f"Solr {solr} answers"
        )
    expected = dspace_version.expected_solr_major(v)
    got = dspace_version.solr_major(solr)
    if expected is not None and got is not None and got != expected and v is not None:
        findings.append(f"DSpace {v.major} expects Solr {expected} but Solr {solr} answers")
    return findings


def describe_ports(sockets: list[ListeningSocket]) -> list[dict[str, Any]]:
    """Collapse sockets per port: ``[{port, label, public, addrs}]`` sorted by port."""
    by_port: dict[int, dict[str, Any]] = {}
    for s in sockets:
        entry = by_port.setdefault(
            s.port, {"port": s.port, "label": KNOWN_PORTS.get(s.port, ""), "public": False, "addrs": []}
        )
        entry["public"] = entry["public"] or s.public
        if s.addr not in entry["addrs"]:
            entry["addrs"].append(s.addr)
    return [by_port[p] for p in sorted(by_port)]


async def discover(
    cfg: Config,
    *,
    sockets: list[ListeningSocket] | None = None,
    unit_states: dict[str, str] | None = None,
    properties: tuple[dict[str, str], list[str]] | None = None,
    file_exists: Callable[[str], bool] | None = None,
    versions: dict[str, str | None] | None = None,
    cmdlines: list[list[str]] | None = None,
) -> DiscoveryResult:
    """Look around the machine and suggest a configuration.

    ``sockets``, ``unit_states``, ``properties`` (``(props, sources)``), ``file_exists`` (is
    there a file at this path?), ``versions`` (:func:`probe_versions`'s answer) and ``cmdlines``
    (:func:`process_cmdlines`'s answer) can be injected; otherwise the live system is inspected.
    The REST API and Solr are asked for their versions only where something runs (a port, a unit
    or DSpace's configuration says so), and the process list is read only to find a Solr log that
    is not at :data:`SOLR_LOG`. A demo config (``cfg.demo``) is answered from the demo stack and
    never inspects the host. Never raises for missing files / no systemd.
    """
    if cfg.demo:
        return await _discover_demo()
    result = DiscoveryResult()
    install_dir = cfg.dspace.install_dir
    systemd_seen = unit_states is not None or systemd.systemctl_path() is not None

    async def exists(path: str) -> bool:
        if file_exists is not None:
            return file_exists(path)
        return await asyncio.to_thread(_file_exists, path)

    async def there(path: str) -> bool:
        """A log that is there, or may be: only one that is certainly missing is left out."""
        if file_exists is not None:
            return file_exists(path)
        return await asyncio.to_thread(_may_exist, path)

    props: dict[str, str] = {}
    if properties is not None:
        props, result.sources = dict(properties[0]), list(properties[1])
    elif cfg.dspace.auto_discover:
        props, result.sources = await asyncio.to_thread(load_dspace_config, install_dir)
    result.properties = props
    patch: dict[str, Any] = {}
    if props:
        log_files = [f.model_dump(mode="json") for f in cfg.logs.files]
        cli_path = cli_log_path(props)
        cli_log = cli_path is not None and await exists(cli_path)
        patch, findings = suggest_from_properties(props, install_dir, log_files, cli_log=cli_log)
        result.findings.extend(findings)
    elif cfg.dspace.auto_discover:
        result.findings.append(
            f"No DSpace configuration found in {install_dir}/config (local.cfg, dspace.cfg)"
        )

    if sockets is None:
        try:
            sockets = await asyncio.to_thread(listening_sockets)
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("listening_sockets failed: %s", exc)
            sockets = []
    result.ports = describe_ports(sockets)
    ports_by_num = {p["port"]: p for p in result.ports}
    for port in (5432, 8983, 8080, 4000, 80, 443):
        info = ports_by_num.get(port)
        if not info:
            continue
        where = "exposed publicly" if info["public"] else "local only"
        label = KNOWN_PORTS.get(port, f"port {port}")
        result.findings.append(f"Found {label} on {info['addrs'][0]}:{port} ({where})")

    states = unit_states if unit_states is not None else await probe_units()
    states = {unit: state for unit, state in states.items() if not _is_sentinel(unit)}
    result.services = states
    active = {u for u, s in states.items() if s == "active"}
    for unit in sorted(states):
        result.findings.append(f"systemd unit {unit} is {states[unit]}")

    dspace_patch = patch.setdefault("dspace", {})
    backend_unit = _pick_unit(states, *BACKEND_UNITS, prefer_active=True)
    if backend_unit:
        dspace_patch["backend_service"] = backend_unit
    frontend_unit = _pick_unit(states, *FRONTEND_UNITS, prefer_active=True)
    if frontend_unit:
        dspace_patch["frontend_service"] = frontend_unit
        if frontend_unit.startswith("pm2-"):
            result.findings.append(
                f"{frontend_unit} is PM2's own unit: it only proves the PM2 daemon runs (the homepage "
                "check shows whether the DSpace UI answers)"
            )
    pg_unit = _pick_unit(states, "postgresql", "postgresql@*")
    if pg_unit:
        patch.setdefault("postgres", {})["service"] = pg_unit
    if "solr" in states:
        patch.setdefault("solr", {})["service"] = "solr"
    # an installed but stopped web server (apache2 next to nginx, say) is not the one to watch
    running = {unit: state for unit, state in states.items() if state in ("active", "failed")}
    web_unit = _pick_unit(running, "nginx", "apache2")
    if web_unit:
        patch.setdefault("nginx", {})["service"] = web_unit
    # a service that answers without a unit (started by hand, from cron, by PM2 without `pm2 startup`):
    # an empty service name, so its ladder has no "unit … not found" rung, and a finding that says so
    if systemd_seen:
        if 8983 in ports_by_num and "solr" not in states:
            patch.setdefault("solr", {})["service"] = ""
            result.findings.append(_unitless("Solr", 8983))
        if 4000 in ports_by_num and not frontend_unit:
            dspace_patch["frontend_service"] = ""
            result.findings.append(_unitless("The DSpace UI", 4000))
        web_ports = [str(port) for port in (80, 443) if port in ports_by_num]
        if web_ports and not web_unit:
            patch.setdefault("nginx", {})["service"] = ""
            result.findings.append(
                f"A web server answers on {' and '.join(web_ports)} but no nginx or apache2 unit runs: "
                "Sentinel checks only whether it answers"
            )

    # expect only the ports that listen now (the PostgreSQL rule follows the database's port)
    expected = {rule.port: rule.model_dump(mode="json") for rule in cfg.ports}
    pg_port = (patch.get("postgres") or {}).get("port")
    if isinstance(pg_port, int) and pg_port not in expected and 5432 in expected:
        expected[pg_port] = {**expected.pop(5432), "port": pg_port}
    suggested_ports = [rule for port, rule in expected.items() if port in ports_by_num]
    if suggested_ports:
        patch["ports"] = suggested_ports

    has_pg = 5432 in ports_by_num or "postgres" in patch or bool(pg_unit)
    has_solr = 8983 in ports_by_num or "solr" in patch or "solr" in active
    has_backend = 8080 in ports_by_num or bool(backend_unit) or "server_url" in dspace_patch
    has_frontend = 4000 in ports_by_num or bool(frontend_unit) or "ui_url" in dspace_patch
    has_web = 80 in ports_by_num or 443 in ports_by_num or bool(web_unit)
    has_tls = 443 in ports_by_num or bool(dspace_patch.get("public_url"))

    # what runs, in which version: DSpace 7.x end of life, the Solr major DSpace expects
    server_url = str(dspace_patch.get("server_url") or cfg.dspace.server_url)
    solr_url = str((patch.get("solr") or {}).get("url") or cfg.solr.url)
    if versions is None:
        versions = await probe_versions(
            server_url if has_backend else None,
            solr_url if has_solr else None,
            verify=cfg.dspace.verify_tls,
        )
    result.findings.extend(version_findings(versions, server_url=server_url, solr_url=solr_url))

    # the Tomcat log follows the runtime: tomcat9 / tomcat10 / tomcat11, or none for the JAR
    current_files = (patch.get("logs") or {}).get("files")
    files = (
        current_files if current_files is not None else [f.model_dump(mode="json") for f in cfg.logs.files]
    )
    packaged = [str(e.get("path", "")) for e in files if _PACKAGED_CATALINA.match(str(e.get("path", "")))]
    present = {path for path in packaged if await exists(path)}
    backend_seen = systemd_seen and (bool(backend_unit) or 8080 in ports_by_num)
    new_files, finding = _tomcat_logs(files, backend_unit, states, backend_seen, lambda path: path in present)
    if finding:
        result.findings.append(finding)
    # nginx's and Solr's logs only where they are, on a DSpace host (elsewhere the list stays the one
    # the chosen option sets, such as the frontend option's nginx-only list)
    if props or has_backend:
        new_files, found = await _service_logs(new_files, has_web=has_web, there=there, cmdlines=cmdlines)
        result.findings.extend(found)
    if new_files != files or current_files is not None:
        patch["logs"] = {"files": new_files}

    modules = {
        "postgres": has_pg,
        "solr": has_solr,
        "backend": has_backend,
        "frontend": has_frontend,
        "nginx": has_web,
        "ssl": has_tls,
        "ports": bool(suggested_ports),
        "security": True,  # it reads this server, whatever runs on it
        "logs": bool(props) or has_web,
        "repository": has_pg or has_backend,
        "assetstore": bool(props) and not assetstore_on_s3(props),
        "incidents": has_backend or has_frontend,
    }
    patch["modules"] = modules
    if not dspace_patch:
        patch.pop("dspace")

    if has_backend and has_frontend:
        result.detected_persona = "full"
    elif has_backend:
        result.detected_persona = "backend"
    elif has_frontend:
        result.detected_persona = "frontend"
    else:
        result.detected_persona = "server"
    result.suggested_config = patch
    if not result.findings:
        result.findings.append(
            "Nothing DSpace-specific found on this machine; the server module still works."
        )
    return result


async def _discover_demo() -> DiscoveryResult:
    """The discovery a demo session shows: the demo stack of ``collectors/demo.py``, not this host."""
    from dspace_sentinel.collectors.demo import DEMO_DSPACE_NAME, DEMO_DSPACE_VERSION, DEMO_SOLR_VERSION

    props = {**DEMO_PROPERTIES, "dspace.name": DEMO_DSPACE_NAME}
    install_dir = props["dspace.dir"]
    sources = [f"{install_dir}/config/local.cfg", f"{install_dir}/config/dspace.cfg"]
    result = await discover(
        Config(),
        sockets=list(DEMO_SOCKETS),
        unit_states=dict(DEMO_UNITS),
        properties=(props, sources),
        file_exists=lambda path: path in DEMO_FILES,
        versions={"dspace": f"DSpace {DEMO_DSPACE_VERSION}", "cris": None, "solr": DEMO_SOLR_VERSION},
        cmdlines=[],
    )
    result.findings.insert(0, "Demo mode: these findings describe the demo server, not this machine.")
    return result


__all__ = [
    "BACKEND_UNITS",
    "CONFIG_FILES",
    "DEMO_FILES",
    "DEMO_PROPERTIES",
    "DEMO_SOCKETS",
    "DEMO_UNITS",
    "FRONTEND_UNITS",
    "KNOWN_PORTS",
    "NGINX_LOG",
    "PROPERTY_KEYS",
    "SOLR_LOG",
    "UNIT_CANDIDATES",
    "DiscoveryResult",
    "assetstore_on_s3",
    "cli_log_path",
    "describe_ports",
    "discover",
    "interpolate",
    "load_dspace_config",
    "mask_url_password",
    "masked_suggestion",
    "parse_jdbc_url",
    "parse_properties",
    "probe_units",
    "probe_versions",
    "process_cmdlines",
    "redact_url",
    "restore_secrets",
    "solr_log_dir",
    "suggest_from_properties",
    "version_findings",
]
