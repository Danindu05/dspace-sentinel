"""Security collector: the read-only security status of this server (the Security module).

Sentinel only *shows* security status: it never opens or closes a port, never changes a
firewall, never runs sudo. It reads files its user may read, the socket table,
``systemctl is-active`` and, once per run, the response headers of the public address.

Eight rungs, in this order (each may carry ``detail.hint``; ``detail.alert`` marks the three
warnings that also open an alert, see ``alerts.rules.SECURITY_RULES``):

* ``security.ports`` — TCP and UDP sockets that wait for connections, graded against the
  ``ports`` rules: ``expected`` (a rule covers it), ``local`` (loopback only), ``tailscale``
  (reachable only on Tailscale's addresses), ``exposed`` (reachable from the network, no rule) or
  ``new`` (exposed and not open when Sentinel started: the first readable socket table is the
  baseline, kept in memory for the life of the process);
* ``security.firewall`` — ufw / nftables / firewalld, and whether exposed ports make a missing
  host firewall a warning;
* ``security.ssh`` — ``PermitRootLogin``, ``PasswordAuthentication`` and ``Port`` from
  ``/etc/ssh/sshd_config`` and the files its ``Include`` lines name (first value wins);
* ``security.web`` — HTTPS on the public address and its security headers;
* ``security.updates`` — Ubuntu's update-notifier counts and ``/var/run/reboot-required``;
* ``security.software`` — end of support of DSpace, Solr, PostgreSQL and the OS, from the
  versions the other components already report (:mod:`dspace_sentinel.util.support_dates`);
* ``security.files`` — the modes of DSpace's ``local.cfg``/``dspace.cfg`` and Sentinel's
  ``sentinel.yaml``/``sentinel.env``;
* ``security.sentinel`` — how Sentinel itself listens (and whether it asks for the login or a token).

A rung that cannot be checked (no permission, no systemd, not this kind of system) is
*informational* (``unknown`` with ``detail.informational``) and also carries
``detail.unavailable``, so the component is never Unknown just because one file is unreadable,
and the alert rules neither open nor clear an alert on it. Every source is reached through
:class:`HostSources`, which tests replace with fakes; every read is guarded, so a failing source
never raises out of :meth:`SecurityCollector.collect`.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import glob as _glob
import grp
import ipaddress
import logging
import os
import pwd
import re
import stat
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlsplit

import psutil

from dspace_sentinel import paths
from dspace_sentinel.auth import login_active
from dspace_sentinel.collectors.base import Collector, CollectorContext, CollectorResult, run_blocking
from dspace_sentinel.collectors.services import rung
from dspace_sentinel.config import dspace_modules_enabled
from dspace_sentinel.models import CheckResult
from dspace_sentinel.util import support_dates, systemd
from dspace_sentinel.util.httpc import describe_error
from dspace_sentinel.util.net import (
    ListeningSocket,
    ip_of,
    is_public_address,
    is_wildcard_address,
    listening_sockets,
)

if TYPE_CHECKING:  # pragma: no cover
    import httpx

    from dspace_sentinel.config import Config

log = logging.getLogger(__name__)

SSHD_CONFIG = "/etc/ssh/sshd_config"
SSHD_DIR = "/etc/ssh"
UFW_CONF = "/etc/ufw/ufw.conf"
UPDATES_AVAILABLE = "/var/lib/update-notifier/updates-available"
REBOOT_REQUIRED = "/var/run/reboot-required"
REBOOT_REQUIRED_PKGS = "/var/run/reboot-required.pkgs"
OS_RELEASE = "/etc/os-release"

FIREWALL_UNITS: tuple[str, ...] = ("ufw", "nftables", "firewalld")
"""Host firewalls asked with ``systemctl is-active``, in the order their name is reported."""

PLAIN_HTTP_PORTS: tuple[int, ...] = (4000, 8080)
"""DSpace's own ports (Angular, Tomcat): reachable from the network, they serve plain HTTP."""

CLIENT_UDP_PORTS: dict[int, str] = {68: "DHCP client", 546: "DHCPv6 client"}
"""UDP ports a networked server keeps open as a *client* (answers to its own requests): expected."""

TAILSCALE_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
)
"""The addresses Tailscale gives its machines: a port listening only there is reachable only
from the tailnet (verdict ``tailscale``)."""

TAILSCALE_INTERFACE = "tailscale0"
"""Tailscale's network interface on Linux."""

TAILSCALE_UDP_PORT = 41641
"""tailscaled's WireGuard port on every interface: expected while :data:`TAILSCALE_INTERFACE` exists."""

TAILSCALE_NOTE = "reachable through Tailscale only"
"""``note`` of a port with the verdict ``tailscale``."""

WIREGUARD_NOTE = "Tailscale's own port (WireGuard)"
"""``note`` of :data:`TAILSCALE_UDP_PORT` when Tailscale runs here."""

PROGRAM_NAMES_NOTE = (
    "Program names are known only for Sentinel's own user; run `sudo ss -ltnp` on the server to see "
    "the others."
)
"""``detail.note`` of the ports rung when Sentinel is not root and a port's program is unknown."""

WEB_TIMEOUT_S = 10.0
"""The public address is asked once per run: 10 s, redirects followed; only the response
headers are used, so the body is never read."""

READ_TIMEOUT_S = 15.0
"""How long the file and socket reads may take together (a dead NFS mount must not hang a run)."""

FRAME_HEADERS = "X-Frame-Options or Content-Security-Policy"
"""The ``headers_missing`` entry when the answer has neither: either one stops framing."""

MAX_LISTED = 8
"""Ports named in a message before the rest becomes ``+N more``."""

RUNGS: tuple[tuple[str, str], ...] = (
    ("security.ports", "Open ports"),
    ("security.firewall", "Firewall"),
    ("security.ssh", "SSH"),
    ("security.web", "Web"),
    ("security.updates", "Updates"),
    ("security.software", "Software"),
    ("security.files", "DSpace files"),
    ("security.sentinel", "Sentinel"),
)
"""Rung id → label, in ladder order."""

LABELS: dict[str, str] = dict(RUNGS)

HINTS: dict[str, str] = {
    "ports": (
        "Ports other machines can reach that no rule expects: {ports}. If a port is not meant for "
        "other machines, make its program listen on 127.0.0.1 or block it in the firewall; if it is, "
        "add a rule for it under ports in config/sentinel.yaml and restart Sentinel."
    ),
    "firewall": (
        "Switch a host firewall on: for example `sudo ufw allow OpenSSH`, then `sudo ufw enable`; "
        "or make those programs listen on localhost. Sentinel cannot see the network's firewall."
    ),
    "ssh_root": "Set PermitRootLogin prohibit-password (or no) in /etc/ssh/sshd_config and reload sshd.",
    "ssh_passwords": "Keys instead of passwords: set PasswordAuthentication no once every admin has a key.",
    "web_http": (
        "Serve DSpace over HTTPS (for example with a Let's Encrypt certificate in nginx) and set "
        "dspace.ui.url and dspace.server.url to the https:// addresses."
    ),
    "web_redirect": (
        "The address redirects to HTTPS, but DSpace's own address starts with http://: set "
        "dspace.ui.url and dspace.server.url to the https:// addresses."
    ),
    "web_plain_port": (
        "Put nginx with HTTPS in front of DSpace, make Angular (4000) and Tomcat (8080) listen on "
        "127.0.0.1, and set dspace.public_url in config/sentinel.yaml."
    ),
    "web_headers": (
        "Missing response headers: {headers}. They are set in the web server in front of DSpace "
        "(nginx add_header)."
    ),
    "updates": "sudo apt update && sudo apt upgrade, then reboot when it says so.",
    "local_cfg": "chmod 640 {path} and keep it owned by the Tomcat user and its group.",
    "sentinel_file": "chmod 600 {path}: only Sentinel's user needs to read it.",
    "sentinel_open": (
        "Create the dashboard login with ./sentinel.sh set-password (or set server.auth_token), or "
        "listen on 127.0.0.1 and reach Sentinel through the SSH tunnel or the nginx HTTPS recipe."
    ),
    "sentinel_login": (
        "Put the nginx HTTPS recipe in front of Sentinel so the password never travels in plain text."
    ),
}
"""``detail.hint`` texts (``{…}`` filled in)."""


# ----------------------------------------------------------------------------- sources


@dataclass
class FileFacts:
    """What ``stat`` says about one file: mode bits, owner, group, and whether Sentinel may read it."""

    path: str
    mode: int
    owner: str
    group: str
    readable: bool

    @property
    def world_readable(self) -> bool:
        return bool(self.mode & stat.S_IROTH)

    @property
    def mode_text(self) -> str:
        """``"644"``."""
        return f"{stat.S_IMODE(self.mode):03o}"

    def to_metric(self) -> dict[str, Any]:
        """The ``metrics.files`` entry."""
        return {
            "path": self.path,
            "mode": self.mode_text,
            "owner": self.owner,
            "group": self.group,
            "world_readable": self.world_readable,
            "readable": self.readable,
        }


@dataclass
class WebAnswer:
    """The public address's answer: where the redirects ended, the status and the headers."""

    url: str
    status_code: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    error: str | None = None


def _user_name(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def _group_name(gid: int) -> str:
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return str(gid)


class HostSources:
    """Everything the Security collector reads, on the real host; tests pass a fake with the same methods.

    Blocking methods (called in a worker thread): :meth:`sockets`, :meth:`read_text`,
    :meth:`glob`, :meth:`stat`, :meth:`exists`, :meth:`sentinel_config`, :meth:`interfaces`,
    :meth:`is_root`. Async methods:
    :meth:`unit_state` and :meth:`fetch_headers`. Nothing here writes anything.
    """

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        self._env = env

    @property
    def env(self) -> Mapping[str, str]:
        """The process environment (for ``SENTINEL_ALLOW_OPEN``)."""
        return os.environ if self._env is None else self._env

    def sockets(self) -> list[ListeningSocket]:
        """TCP and UDP sockets waiting for connections (``[]`` when the table cannot be read)."""
        return listening_sockets(with_process=True, udp=True)

    def read_text(self, path: str) -> str:
        """The file's text (at most 256 KB); raises ``OSError`` (missing, not permitted …)."""
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(262144)

    def glob(self, pattern: str) -> list[str]:
        """Paths matching ``pattern``, sorted the way OpenSSH reads an ``Include`` glob."""
        return sorted(_glob.glob(pattern))

    def stat(self, path: str) -> FileFacts | None:
        """``stat`` of ``path``; ``None`` when it is not a file or Sentinel may not look at it."""
        try:
            st = os.stat(path)
        except OSError:
            return None
        if not stat.S_ISREG(st.st_mode):
            return None
        return FileFacts(
            path=path,
            mode=st.st_mode,
            owner=_user_name(st.st_uid),
            group=_group_name(st.st_gid),
            readable=os.access(path, os.R_OK),
        )

    def exists(self, path: str) -> bool:
        return os.path.exists(path)

    def sentinel_config(self) -> Path:
        """Sentinel's own ``config/sentinel.yaml`` (``config/sentinel.env`` sits next to it)."""
        return paths.config_path()

    def interfaces(self) -> set[str]:
        """The names of this machine's network interfaces (``tailscale0`` while Tailscale runs)."""
        return set(psutil.net_if_addrs())

    def is_root(self) -> bool:
        """True when Sentinel runs as root (it then knows every listening program's name)."""
        return os.geteuid() == 0

    def has_systemd(self) -> bool:
        return systemd.systemctl_path() is not None

    async def unit_state(self, unit: str) -> str:
        """``systemctl is-active``: ``active`` | ``inactive`` | ``failed`` | ``missing`` | ``unknown``."""
        return await systemd.is_active(unit)

    async def fetch_headers(self, client: httpx.AsyncClient, url: str) -> WebAnswer:
        """One GET of ``url`` (redirects followed, :data:`WEB_TIMEOUT_S`): the headers, never the body."""
        try:
            async with client.stream("GET", url, follow_redirects=True, timeout=WEB_TIMEOUT_S) as resp:
                headers = {k.lower(): v for k, v in resp.headers.items()}
                return WebAnswer(str(resp.url), resp.status_code, headers)
        except Exception as exc:
            return WebAnswer(url, error=describe_error(exc))


# ------------------------------------------------------------------------------ parsing


@dataclass
class SshFacts:
    """``sshd_config`` as OpenSSH reads it, for the three keywords the SSH rung looks at.

    ``found``: the main file exists; ``readable``: Sentinel may read it. ``certain[keyword]`` is
    false when an included file Sentinel may not read comes before the value (or, for a
    default, anywhere): that file could set it differently.
    """

    found: bool = False
    readable: bool = False
    port: int = 22
    permit_root_login: str = "prohibit-password"
    password_authentication: str = "yes"
    certain: dict[str, bool] = field(default_factory=dict)
    unreadable: list[str] = field(default_factory=list)

    def is_certain(self, keyword: str) -> bool:
        return self.certain.get(keyword, not self.unreadable)


SSH_KEYWORDS: tuple[str, ...] = ("port", "permitrootlogin", "passwordauthentication")
_KEY_VALUE = re.compile(r"^(\S+?)(?:\s*=\s*|\s+)(.*)$")


def _first_token(value: str) -> str:
    words = value.strip().split()
    return words[0].strip("\"'") if words else ""


def parse_sshd_config(
    read: Callable[[str], str], glob: Callable[[str], list[str]], path: str = SSHD_CONFIG
) -> SshFacts:
    """Read ``path`` and every file its ``Include`` lines name, in the order OpenSSH reads them.

    The first value of a keyword wins (an ``Include`` at the top of the file therefore wins over
    the lines below it); lines inside a ``Match`` block (up to ``Match all`` or the end of the
    file) are conditional and skipped. Keywords found nowhere keep OpenSSH's defaults
    (``Port 22``, ``PermitRootLogin prohibit-password``, ``PasswordAuthentication yes``).
    ``without-password`` reads as ``prohibit-password``. Never raises.
    """
    facts = SshFacts()
    values: dict[str, str] = {}
    seen: set[str] = set()
    blind = False

    def lines_of(text: str, depth: int) -> None:
        in_match = False
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            m = _KEY_VALUE.match(line)
            if not m:
                continue
            keyword, value = m.group(1).lower(), m.group(2)
            if keyword == "match":
                in_match = value.strip().lower() != "all"
            elif in_match:
                continue
            elif keyword == "include":
                for pattern in value.split():
                    pattern = pattern.strip("\"'")
                    full = pattern if pattern.startswith("/") else os.path.join(SSHD_DIR, pattern)
                    targets = glob(full) if any(ch in full for ch in "*?[") else [full]
                    for target in targets:
                        include(target, depth + 1)
            elif keyword in SSH_KEYWORDS and keyword not in values:
                values[keyword] = _first_token(value)
                facts.certain[keyword] = not blind

    def include(file_path: str, depth: int) -> None:
        nonlocal blind
        if depth > 8 or file_path in seen:
            return
        seen.add(file_path)
        try:
            text = read(file_path)
        except FileNotFoundError:
            return
        except OSError:
            facts.unreadable.append(file_path)
            blind = True
            return
        lines_of(text, depth)

    try:
        main = read(path)
    except FileNotFoundError:
        return facts
    except OSError:
        facts.found = True
        return facts
    facts.found = facts.readable = True
    seen.add(path)
    try:
        lines_of(main, 0)
    except Exception as exc:  # a parser slip must not take the rung down
        log.debug("sshd_config parse failed: %s", exc)
    for keyword in SSH_KEYWORDS:
        facts.certain.setdefault(keyword, not blind)
    port = values.get("port", "")
    if port.isdigit() and 0 < int(port) < 65536:
        facts.port = int(port)
    if values.get("permitrootlogin"):
        prl = values["permitrootlogin"].lower()
        facts.permit_root_login = "prohibit-password" if prl == "without-password" else prl
    if values.get("passwordauthentication"):
        facts.password_authentication = values["passwordauthentication"].lower()
    return facts


_PENDING = re.compile(r"(\d+)\s+(?:updates?|packages?)\s+can be (?:applied immediately|updated)", re.I)
_SECURITY = re.compile(
    r"(\d+)\s+(?:of these updates|updates?)\s+(?:are|is)\s+(?:a\s+)?(?:standard\s+)?security updates?",
    re.I,
)


def parse_updates(text: str) -> tuple[int, int]:
    """``(pending, security)`` from update-notifier's ``updates-available`` (``0`` for what it omits).

    Reads both wordings: "14 updates can be applied immediately. / 6 of these updates are standard
    security updates." (Ubuntu 20.04 and later) and "14 packages can be updated. / 6 updates are
    security updates." (older releases); ESM lines ("… can be applied with ESM Apps") are left out.
    """
    pending = _PENDING.search(text)
    security = _SECURITY.search(text)
    return (int(pending.group(1)) if pending else 0, int(security.group(1)) if security else 0)


def parse_os_release(text: str) -> dict[str, str]:
    """``/etc/os-release`` → ``{"ID": "ubuntu", "VERSION_ID": "24.04", …}`` (quotes removed)."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def parse_ufw_enabled(text: str) -> bool:
    """True when ``/etc/ufw/ufw.conf`` says ``ENABLED=yes``."""
    for raw in text.splitlines():
        line = raw.strip()
        if line.upper().startswith("ENABLED="):
            return line.split("=", 1)[1].strip().strip("\"'").lower() == "yes"
    return False


# ------------------------------------------------------------------------------- facts


@dataclass
class UpdatesFacts:
    """Pending updates (Ubuntu's update-notifier) and whether a reboot is required."""

    available: bool = False
    pending: int | None = None
    security: int | None = None
    reboot_required: bool = False
    reboot_pkgs: list[str] = field(default_factory=list)
    source: str | None = None


@dataclass
class HostFacts:
    """What the blocking reads of one run found (``sockets=None``: the table could not be read;
    ``interfaces``/``root`` ``None``: not known)."""

    sockets: list[ListeningSocket] | None = None
    interfaces: set[str] | None = None
    root: bool | None = None
    ufw_enabled: bool | None = None
    ssh: SshFacts = field(default_factory=SshFacts)
    updates: UpdatesFacts = field(default_factory=UpdatesFacts)
    os_release: dict[str, str] | None = None
    files: list[FileFacts] = field(default_factory=list)
    local_cfg: str = ""
    sentinel_yaml: str = ""
    sentinel_env: str = ""

    @property
    def local_cfg_found(self) -> bool:
        return any(f.path == self.local_cfg for f in self.files)


@dataclass
class FirewallState:
    """``active``: True / False / ``None`` (nothing could tell); ``name`` of the active firewall."""

    active: bool | None = None
    name: str | None = None
    units: dict[str, str] = field(default_factory=dict)


def _guard(what: str, fn: Callable[[], Any], default: Any = None) -> Any:
    try:
        return fn()
    except Exception as exc:
        log.debug("security: %s failed: %s", what, exc)
        return default


def _read_or_none(src: HostSources, path: str) -> str | None:
    try:
        return src.read_text(path)
    except OSError:
        return None


def _read_updates(src: HostSources) -> UpdatesFacts:
    out = UpdatesFacts()
    text = _read_or_none(src, UPDATES_AVAILABLE)
    if text is not None:
        out.pending, out.security = parse_updates(text)
        out.available, out.source = True, UPDATES_AVAILABLE
    out.reboot_required = bool(_guard("reboot-required", lambda: src.exists(REBOOT_REQUIRED), False))
    if out.reboot_required:
        pkgs = _read_or_none(src, REBOOT_REQUIRED_PKGS) or ""
        out.reboot_pkgs = list(dict.fromkeys(p.strip() for p in pkgs.splitlines() if p.strip()))
    return out


def read_host(src: HostSources, cfg: Config) -> HostFacts:
    """Every blocking read of one run, each guarded on its own (called in a worker thread)."""
    facts = HostFacts()
    sockets = _guard("socket table", src.sockets, [])
    facts.sockets = list(sockets) if sockets else None
    names = _guard("network interfaces", src.interfaces)
    facts.interfaces = {str(n) for n in names} if names is not None else None
    root = _guard("user id", src.is_root)
    facts.root = root if isinstance(root, bool) else None
    ufw_text = _guard("ufw.conf", lambda: _read_or_none(src, UFW_CONF))
    facts.ufw_enabled = parse_ufw_enabled(ufw_text) if ufw_text is not None else None
    facts.ssh = _guard("sshd_config", lambda: parse_sshd_config(src.read_text, src.glob), SshFacts())
    facts.updates = _guard("updates", lambda: _read_updates(src), UpdatesFacts())
    os_text = _guard("os-release", lambda: _read_or_none(src, OS_RELEASE))
    facts.os_release = (parse_os_release(os_text) or None) if os_text is not None else None

    config_dir = Path(cfg.dspace.install_dir or "/dspace") / "config"
    facts.local_cfg = str(config_dir / "local.cfg")
    sentinel_yaml = Path(_guard("config path", src.sentinel_config, None) or paths.config_path())
    facts.sentinel_yaml = str(sentinel_yaml)
    facts.sentinel_env = str(sentinel_yaml.parent / "sentinel.env")
    for path in (facts.local_cfg, str(config_dir / "dspace.cfg"), facts.sentinel_yaml, facts.sentinel_env):
        found = _guard(f"stat {path}", lambda p=path: src.stat(p))
        if found is not None:
            facts.files.append(found)
    return facts


async def firewall_state(src: HostSources, ufw_enabled: bool | None) -> FirewallState:
    """Which host firewall is active: ``systemctl is-active`` plus ``ufw.conf``'s ``ENABLED=yes``.

    Ubuntu keeps ``ufw.service`` active even while ufw is disabled, so ufw counts only when its
    configuration says ``ENABLED=yes`` (and its unit is not stopped); without systemd that line
    alone decides. ``active=None`` when nothing can tell.
    """
    if not src.has_systemd():
        return FirewallState(True, "ufw") if ufw_enabled else FirewallState(None, None)
    answers = await asyncio.gather(*(src.unit_state(u) for u in FIREWALL_UNITS), return_exceptions=True)
    units = {u: a if isinstance(a, str) else "unknown" for u, a in zip(FIREWALL_UNITS, answers, strict=True)}
    for unit in FIREWALL_UNITS:
        state = units[unit]
        if unit == "ufw":
            if ufw_enabled is True and state not in ("inactive", "failed"):
                return FirewallState(True, "ufw", units)
            if ufw_enabled is None and state == "active":
                return FirewallState(True, "ufw", units)
        elif state == "active":
            return FirewallState(True, unit, units)
    if all(state == "unknown" for state in units.values()):
        return FirewallState(None, None, units)
    return FirewallState(False, None, units)


# ------------------------------------------------------------------------------- rungs


def _informational(rung_id: str, ts: float, message: str, *, unavailable: bool) -> CheckResult:
    """An ``unknown`` rung that does not count: ``unavailable`` when it could not be checked at all."""
    return rung(
        rung_id,
        LABELS[rung_id],
        "unknown",
        ts,
        message=message,
        informational=True,
        unavailable=True if unavailable else None,
    )


def _plural(n: int, word: str) -> str:
    return f"{n} {word if n == 1 else word + 's'}"


def _capped(items: list[str]) -> str:
    if len(items) <= MAX_LISTED:
        return ", ".join(items)
    return ", ".join(items[:MAX_LISTED]) + f" +{len(items) - MAX_LISTED} more"


def _port_text(entry: dict[str, Any]) -> str:
    return f"{entry['port']}" if entry["proto"] == "tcp" else f"{entry['port']}/udp"


def _port_with_program(entry: dict[str, Any]) -> str:
    text = _port_text(entry)
    return f"{text} ({entry['process']})" if entry.get("process") else text


def group_ports(sockets: Iterable[ListeningSocket]) -> list[dict[str, Any]]:
    """One entry per (port, protocol): every address it listens on, public when any address is."""
    groups: dict[tuple[int, str], dict[str, Any]] = {}
    for s in sockets:
        proto = getattr(s, "proto", "tcp") or "tcp"
        entry = groups.setdefault(
            (s.port, proto),
            {"port": s.port, "proto": proto, "addrs": [], "public": False, "process": None, "pid": None},
        )
        if s.addr not in entry["addrs"]:
            entry["addrs"].append(s.addr)
        entry["public"] = entry["public"] or bool(s.public)
        if entry["process"] is None and s.process:
            entry["process"] = s.process
        if entry["pid"] is None and s.pid:
            entry["pid"] = s.pid
    return [groups[key] for key in sorted(groups)]


def is_tailscale_address(addr: str) -> bool:
    """True for an address in Tailscale's ranges (:data:`TAILSCALE_NETWORKS`)."""
    ip = ip_of(addr)
    return ip is not None and any(ip.version == net.version and ip in net for net in TAILSCALE_NETWORKS)


def _tailscale_only(entry: dict[str, Any]) -> bool:
    """Every address other machines can reach is a Tailscale address."""
    reachable = [a for a in entry["addrs"] if is_public_address(a)]
    return bool(reachable) and all(is_tailscale_address(a) for a in reachable)


def _wireguard_port(entry: dict[str, Any], interfaces: set[str] | None) -> bool:
    """tailscaled's UDP port on every interface while Tailscale's interface exists."""
    return (
        entry["proto"] == "udp"
        and entry["port"] == TAILSCALE_UDP_PORT
        and TAILSCALE_INTERFACE in (interfaces or ())
        and all(is_wildcard_address(a) for a in entry["addrs"])
    )


def grade_ports(
    entries: list[dict[str, Any]],
    cfg: Config,
    baseline: set[tuple[int, str]] | None,
    interfaces: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Set ``expected`` (the ``ports`` rule's ``expect``, or ``None``) and ``verdict`` on every entry.

    ``local`` when it listens on loopback only. Reachable from the network: ``expected`` when a
    rule covers it (a public ``local_only`` port is the Ports component's warning, not this
    one's), and also for Sentinel's own port, the UDP ports a networked server keeps open as a
    client (DHCP) and Tailscale's WireGuard port while ``interfaces`` holds ``tailscale0`` (those
    carry a ``note``); ``tailscale`` when every reachable address is one of Tailscale's
    (:data:`TAILSCALE_NETWORKS`); otherwise ``new`` when it is not in ``baseline``, else
    ``exposed``.
    """
    rules = {rule.port: rule for rule in cfg.ports}
    for entry in entries:
        rule = rules.get(entry["port"])
        entry["expected"] = rule.expect if rule is not None else None
        note: str | None = None
        if not entry["public"]:
            verdict = "local"
        elif rule is not None:
            verdict = "expected"
        elif entry["proto"] == "tcp" and entry["port"] == cfg.server.port:
            verdict, note = "expected", "Sentinel"
        elif entry["proto"] == "udp" and entry["port"] in CLIENT_UDP_PORTS:
            verdict, note = "expected", CLIENT_UDP_PORTS[entry["port"]]
        elif _wireguard_port(entry, interfaces):
            verdict, note = "expected", WIREGUARD_NOTE
        elif _tailscale_only(entry):
            verdict, note = "tailscale", TAILSCALE_NOTE
        elif baseline is not None and (entry["port"], entry["proto"]) not in baseline:
            verdict = "new"
        else:
            verdict = "exposed"
        entry["verdict"] = verdict
        if note:
            entry["note"] = note
    return entries


def unexpected_ports(entries: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """The ports other machines can reach without a rule (verdict ``exposed`` or ``new``): the
    ports rung's "unexpected", the firewall rung's count and ``metrics.exposed``."""
    return [e for e in entries or [] if e["verdict"] in ("exposed", "new")]


def ports_rung(
    entries: list[dict[str, Any]] | None, ts: float, *, root: bool | None = None
) -> tuple[CheckResult, dict[str, Any]]:
    """``security.ports`` and its metrics (``ports``, ``listening``, ``reachable``, ``tailscale``,
    ``exposed``, ``new``).

    ``reachable`` leaves out the ports reachable through Tailscale only (counted in ``tailscale``).
    ``root=False`` (Sentinel is known not to run as root) adds :data:`PROGRAM_NAMES_NOTE` as
    ``detail.note`` while a port's program is unknown.
    """
    rid = "security.ports"
    if entries is None:
        message = "not available · the socket table could not be read"
        check = _informational(rid, ts, message, unavailable=True)
        empty = {"ports": [], "listening": None, "reachable": None, "tailscale": None}
        return check, {**empty, "exposed": None, "new": None}
    reachable = [e for e in entries if e["public"] and e["verdict"] != "tailscale"]
    tailnet = [e for e in entries if e["verdict"] == "tailscale"]
    unexpected = unexpected_ports(entries)
    new = [e for e in unexpected if e["verdict"] == "new"]
    metrics = {
        "ports": entries,
        "listening": len(entries),
        "reachable": len(reachable),
        "tailscale": len(tailnet),
        "exposed": len(unexpected),
        "new": len(new),
    }
    reach = f"{len(reachable)} reachable from the network" if reachable else "none reachable from the network"
    if not unexpected:
        listed = list(dict.fromkeys(_port_text(e) for e in reachable))
        reach += f" ({_capped(listed)})" if listed else ""
    parts = [f"{len(entries)} listening", reach]
    if tailnet:
        parts.append(f"{len(tailnet)} through Tailscale only")
    note = PROGRAM_NAMES_NOTE if root is False and any(not e.get("process") for e in entries) else None
    if not unexpected:
        message = " · ".join([*parts, "none unexpected"])
        return rung(rid, LABELS[rid], "ok", ts, message=message, note=note), metrics
    named = _capped([_port_with_program(e) for e in unexpected])
    message = " · ".join([*parts, f"{len(unexpected)} unexpected: {named}"])
    hint = HINTS["ports"].format(ports=named)
    alert = True if new else None
    check = rung(rid, LABELS[rid], "warn", ts, message=message, hint=hint, alert=alert, note=note)
    return check, metrics


def firewall_rung(
    state: FirewallState, entries: list[dict[str, Any]] | None, ts: float
) -> tuple[CheckResult, dict[str, Any]]:
    """``security.firewall``: ok when a host firewall is active; a warning only while ports are exposed.

    "N ports are reachable from the network without a rule" counts the ports rung's unexpected
    ones (:func:`unexpected_ports`); a public ``local_only`` port is named after them.
    """
    rid = "security.firewall"
    metrics = {"active": state.active, "name": state.name, "units": dict(state.units) or None}
    if state.active:
        return rung(rid, LABELS[rid], "ok", ts, message=f"{state.name} active"), metrics
    if state.active is None:
        why = "systemctl did not answer" if state.units else "systemctl is not available"
        return _informational(rid, ts, f"not available · {why}", unavailable=True), metrics
    unexpected = unexpected_ports(entries)
    local_public = [e for e in entries or [] if e.get("expected") == "local_only" and e["public"]]
    if unexpected or local_public:
        found: list[str] = []
        if unexpected:
            n = len(unexpected)
            found.append(
                f"{_plural(n, 'port')} {'is' if n == 1 else 'are'} reachable from the network without a rule"
            )
        if local_public:
            n = len(local_public)
            found.append(f"{_plural(n, 'local-only port')} {'is' if n == 1 else 'are'} public")
        message = "no host firewall is active and " + ", and ".join(found)
        check = rung(rid, LABELS[rid], "warn", ts, message=message, hint=HINTS["firewall"], alert=True)
        return check, metrics
    message = (
        "no host firewall active (this server relies on the network's firewall, which Sentinel cannot see)"
    )
    return _informational(rid, ts, message, unavailable=False), metrics


def ssh_rung(
    facts: SshFacts, entries: list[dict[str, Any]] | None, ts: float
) -> tuple[CheckResult, dict[str, Any]]:
    """``security.ssh`` from the parsed ``sshd_config``."""
    rid = "security.ssh"
    unknown = {"port": None, "permit_root_login": None, "password_authentication": None, "readable": False}
    if not facts.found:
        if any(e["port"] == 22 and e["proto"] == "tcp" for e in entries or []):
            message = "not available · port 22 listens, but /etc/ssh/sshd_config was not found"
            return _informational(rid, ts, message, unavailable=True), unknown
        return _informational(rid, ts, "no SSH server found", unavailable=False), unknown
    if not facts.readable:
        message = "not available · sshd_config is not readable by Sentinel's user"
        return _informational(rid, ts, message, unavailable=True), unknown
    metrics = {
        "port": facts.port,
        "permit_root_login": facts.permit_root_login,
        "password_authentication": facts.password_authentication,
        "readable": True,
        "unreadable": list(facts.unreadable) or None,
    }
    passwords = facts.password_authentication != "no"
    if facts.permit_root_login == "yes":
        certain = facts.is_certain("permitrootlogin") and facts.is_certain("passwordauthentication")
        if passwords:
            message = "root may log in with a password"
        else:
            message = "root may log in with a key (passwords are off)"
        if not certain:
            message += " (an included file Sentinel cannot read may change this)"
        alert = True if passwords and certain else None
        check = rung(rid, LABELS[rid], "warn", ts, message=message, hint=HINTS["ssh_root"], alert=alert)
        return check, metrics
    message = (
        f"root login: {facts.permit_root_login} · passwords: {facts.password_authentication} "
        f"· port {facts.port}"
    )
    if facts.unreadable:
        message += f" · {_plural(len(facts.unreadable), 'included file')} not readable"
    hint = HINTS["ssh_passwords"] if passwords else None
    return rung(rid, LABELS[rid], "ok", ts, message=message, hint=hint), metrics


def missing_headers(headers: Mapping[str, str], https: bool) -> list[str]:
    """The security headers the answer lacks: HSTS (over HTTPS only), X-Content-Type-Options, and
    X-Frame-Options or a Content-Security-Policy (:data:`FRAME_HEADERS` when it has neither)."""
    have = {k.lower() for k in headers}
    missing: list[str] = []
    if https and "strict-transport-security" not in have:
        missing.append("Strict-Transport-Security")
    if "x-content-type-options" not in have:
        missing.append("X-Content-Type-Options")
    if "x-frame-options" not in have and "content-security-policy" not in have:
        missing.append(FRAME_HEADERS)
    return missing


def web_rung(
    public_url: str, answer: WebAnswer | None, entries: list[dict[str, Any]] | None, ts: float
) -> tuple[CheckResult, dict[str, Any]]:
    """``security.web``: HTTPS on the public address and its security headers."""
    rid = "security.web"
    url = (public_url or "").strip()
    metrics: dict[str, Any] = {
        "public_url": url or None,
        "scheme": None,
        "headers_missing": [],
        "headers_checked": False,
        "final_url": None,
        "status_code": None,
        "error": None,
    }
    if not url:
        plain = [p for p in PLAIN_HTTP_PORTS if any(e["port"] == p and e["public"] for e in entries or [])]
        if plain:
            message = f"DSpace is served over plain HTTP on port {plain[0]} to the network"
            return rung(rid, LABELS[rid], "warn", ts, message=message, hint=HINTS["web_plain_port"]), metrics
        return _informational(rid, ts, "no public address set", unavailable=False), metrics
    try:
        parts = urlsplit(url)
        scheme, host = (parts.scheme or "").lower(), parts.hostname or url
    except ValueError:
        scheme, host = "", url
    metrics["scheme"] = scheme or None
    if scheme not in ("http", "https"):
        message = "not available · the public address is not an http:// or https:// address"
        return _informational(rid, ts, message, unavailable=True), metrics
    final_scheme = scheme
    if answer is not None:
        metrics["status_code"] = answer.status_code
        metrics["error"] = answer.error
        if answer.error is None:
            metrics["final_url"] = answer.url
            try:
                final_scheme = (urlsplit(answer.url).scheme or scheme).lower()
            except ValueError:
                final_scheme = scheme
            metrics["headers_missing"] = missing_headers(answer.headers, https=final_scheme == "https")
            metrics["headers_checked"] = True
    if scheme == "http":
        message = "the public address is plain HTTP: logins and sessions travel unencrypted"
        hint = HINTS["web_redirect"] if final_scheme == "https" else HINTS["web_http"]
        return rung(rid, LABELS[rid], "warn", ts, message=message, hint=hint), metrics
    message = f"HTTPS on {host}"
    if answer is not None and answer.error is not None:
        message += f" · response headers not checked ({answer.error})"
    missing = metrics["headers_missing"]
    hint = HINTS["web_headers"].format(headers=", ".join(missing)) if missing else None
    return rung(rid, LABELS[rid], "ok", ts, message=message, hint=hint), metrics


def updates_rung(facts: UpdatesFacts, ts: float) -> tuple[CheckResult, dict[str, Any]]:
    """``security.updates``: pending security updates and a required reboot."""
    rid = "security.updates"
    metrics = {
        "pending": facts.pending,
        "security": facts.security,
        "reboot_required": facts.reboot_required,
        "reboot_pkgs": list(facts.reboot_pkgs) or None,
        "source": facts.source,
    }
    parts: list[str] = []
    if facts.security:
        parts.append(f"{_plural(facts.security, 'security update')} pending")
    if facts.reboot_required:
        text = "reboot required"
        if facts.reboot_pkgs:
            more = len(facts.reboot_pkgs) - 2
            text += f" ({', '.join(facts.reboot_pkgs[:2])}{f' +{more} more' if more > 0 else ''})"
        parts.append(text)
    if parts:
        return rung(rid, LABELS[rid], "warn", ts, message=" · ".join(parts), hint=HINTS["updates"]), metrics
    if not facts.available:
        message = "not available · no update information on this system (Ubuntu's update-notifier)"
        return _informational(rid, ts, message, unavailable=True), metrics
    other = max(0, (facts.pending or 0) - (facts.security or 0))
    message = "no pending updates"
    if other:
        message = f"no pending security updates ({_plural(other, 'other update')})"
    return rung(rid, LABELS[rid], "ok", ts, message=message), metrics


def software_entries(
    versions: Mapping[str, Any], os_release: dict[str, str] | None, today: _dt.date
) -> list[dict[str, Any]]:
    """The ``metrics.software`` list: DSpace, Solr, PostgreSQL, the OS (unknown versions left out)."""
    found = [
        support_dates.dspace_support(versions.get("dspace"), today),
        support_dates.solr_support(versions.get("solr"), today),
        support_dates.postgres_support(versions.get("postgres"), today),
        support_dates.os_support(os_release, today),
    ]
    return [e for e in found if e is not None]


def software_rung(entries: list[dict[str, Any]], ts: float) -> tuple[CheckResult, dict[str, Any]]:
    """``security.software``: a warning for software past its end of support or affected by a known flaw."""
    rid = "security.software"
    metrics: dict[str, Any] = {"software": entries}
    if not entries:
        message = "not available · no software versions known yet"
        return _informational(rid, ts, message, unavailable=True), metrics
    problems = [e for e in entries if e["status"] in ("ended", "affected")]
    if problems:
        message = support_dates.problem_sentence(problems[0])
        hint = None
        if len(problems) > 1:
            message += f" (+{len(problems) - 1} more)"
            hint = " ".join(support_dates.problem_sentence(e) + "." for e in problems)
        return rung(rid, LABELS[rid], "warn", ts, message=message, hint=hint), metrics
    known = [e for e in entries if e["status"] in ("ok", "soon")]
    unknown = [e for e in entries if e["status"] == "unknown"]
    parts: list[str] = []
    if known:
        parts.append(" · ".join(f"{e['name']} {e['version']}" for e in known) + ": all supported")
    parts.extend(support_dates.soon_sentence(e) for e in known if e["status"] == "soon")
    if unknown:
        names = ", ".join(f"{e['name']} {e['version']}".strip() for e in unknown)
        parts.append(f"no support date known for {names}")
    message = " · ".join(parts)
    if not known:
        return _informational(rid, ts, message, unavailable=False), metrics
    return rung(rid, LABELS[rid], "ok", ts, message=message), metrics


def files_rung(facts: HostFacts, dspace_on: bool, ts: float) -> tuple[CheckResult, dict[str, Any]]:
    """``security.files``: DSpace's ``local.cfg`` and Sentinel's own configuration, readable by others?"""
    rid = "security.files"
    metrics: dict[str, Any] = {"files": [f.to_metric() for f in facts.files]}
    by_path = {f.path: f for f in facts.files}
    problems: list[tuple[str, str]] = []
    local = by_path.get(facts.local_cfg)
    if local is not None and local.world_readable:
        message = (
            "local.cfg is readable by every user on this server and holds the database password "
            f"(mode {local.mode_text}, {local.owner}:{local.group})"
        )
        problems.append((message, HINTS["local_cfg"].format(path=local.path)))
    secrets = (
        (facts.sentinel_yaml, "sentinel.yaml", "it holds the Telegram tokens and the email password"),
        (facts.sentinel_env, "sentinel.env", "it holds the secrets Sentinel reads when it starts"),
    )
    for path, name, holds in secrets:
        found = by_path.get(path)
        if found is not None and found.world_readable:
            message = f"{name} is readable by other users (mode {found.mode_text}): {holds}"
            problems.append((message, HINTS["sentinel_file"].format(path=found.path)))
    if problems:
        message = problems[0][0]
        if len(problems) > 1:
            message += f" (+{len(problems) - 1} more)"
        hint = " ".join(h for _, h in problems)
        return rung(rid, LABELS[rid], "warn", ts, message=message, hint=hint), metrics
    sensitive = (
        (facts.local_cfg, "local.cfg"),
        (facts.sentinel_yaml, "sentinel.yaml"),
        (facts.sentinel_env, "sentinel.env"),
    )
    names = [name for path, name in sensitive if path in by_path]
    missing_note = "local.cfg not found" if dspace_on and not facts.local_cfg_found else ""
    if not names:
        message = "not available · " + (missing_note or "no configuration file could be checked")
        return _informational(rid, ts, message, unavailable=True), metrics
    if len(names) == 1:
        message = f"{names[0]} is not readable by other users"
    else:
        message = f"{', '.join(names[:-1])} and {names[-1]} are not readable by other users"
    if missing_note:
        message += f" · {missing_note}"
    return rung(rid, LABELS[rid], "ok", ts, message=message), metrics


def is_loopback_host(host: str) -> bool:
    """True for ``localhost`` and loopback addresses (``127.0.0.0/8``, ``::1``)."""
    name = (host or "").strip().strip("[]")
    if name.lower() == "localhost":
        return True
    ip = ip_of(name)
    return ip is not None and ip.is_loopback


def sentinel_rung(cfg: Config, env: Mapping[str, str], ts: float) -> tuple[CheckResult, dict[str, Any]]:
    """``security.sentinel``: how Sentinel's own dashboard listens, and whether it asks for the
    login (``server.login``) or the token (``server.auth_token``)."""
    rid = "security.sentinel"
    host = cfg.server.host or "0.0.0.0"
    auth = bool(cfg.server.auth_token)
    login = login_active(cfg)
    local = is_loopback_host(host)
    metrics = {
        "host": host,
        "port": cfg.server.port,
        "auth": auth,
        "login": login,
        "open": not local and not auth and not login,
    }
    if local:
        return rung(rid, LABELS[rid], "ok", ts, message=f"listens on {host} only"), metrics
    if login:
        message = f"listens on {host} with a login"
        return rung(rid, LABELS[rid], "ok", ts, message=message, hint=HINTS["sentinel_login"]), metrics
    if auth:
        return rung(rid, LABELS[rid], "ok", ts, message=f"listens on {host} with a password"), metrics
    allow_open = str(env.get("SENTINEL_ALLOW_OPEN", "")).strip() == "1"
    why = "SENTINEL_ALLOW_OPEN" if allow_open else "server.allow_unauthenticated"
    message = f"listens on {host} without a password ({why})"
    return rung(rid, LABELS[rid], "warn", ts, message=message, hint=HINTS["sentinel_open"]), metrics


METRIC_KEYS: dict[str, str | None] = {
    "security.ports": None,
    "security.firewall": "firewall",
    "security.ssh": "ssh",
    "security.web": "web",
    "security.updates": "updates",
    "security.software": None,
    "security.files": None,
    "security.sentinel": "sentinel",
}
"""Where each rung's metrics go: under a key, or (``None``) merged into the top level
(``ports``/``listening``/``reachable``/``tailscale``/``exposed``/``new``, ``software``, ``files``)."""


def build_ladder(
    cfg: Config,
    facts: HostFacts,
    firewall: FirewallState,
    web: WebAnswer | None,
    versions: Mapping[str, Any],
    ts: float,
    *,
    baseline: set[tuple[int, str]] | None = None,
    env: Mapping[str, str] | None = None,
    today: _dt.date | None = None,
    public_url: str | None = None,
) -> tuple[list[CheckResult], dict[str, Any]]:
    """The eight rungs and the component's metrics from what one run found (no I/O here).

    ``baseline`` is the set of ``(port, proto)`` that were public when Sentinel started;
    ``versions`` holds ``dspace``, ``solr`` and ``postgres`` as the other components report them;
    ``public_url`` replaces ``dspace.public_url`` (the demo's address). The demo builds its
    Security component through this function too.
    """
    today = today or _dt.date.fromtimestamp(ts)
    address = cfg.dspace.public_url if public_url is None else public_url
    entries = None
    if facts.sockets is not None:
        entries = grade_ports(group_ports(facts.sockets), cfg, baseline, facts.interfaces)
    steps: dict[str, Callable[[], tuple[CheckResult, dict[str, Any]]]] = {
        "security.ports": lambda: ports_rung(entries, ts, root=facts.root),
        "security.firewall": lambda: firewall_rung(firewall, entries, ts),
        "security.ssh": lambda: ssh_rung(facts.ssh, entries, ts),
        "security.web": lambda: web_rung(address, web, entries, ts),
        "security.updates": lambda: updates_rung(facts.updates, ts),
        "security.software": lambda: software_rung(software_entries(versions, facts.os_release, today), ts),
        "security.files": lambda: files_rung(facts, dspace_modules_enabled(cfg), ts),
        "security.sentinel": lambda: sentinel_rung(cfg, env if env is not None else {}, ts),
    }
    checks: list[CheckResult] = []
    metrics: dict[str, Any] = {}
    for rid, _label in RUNGS:
        try:
            check, part = steps[rid]()
        except Exception as exc:  # one broken rung is informational, never the whole component
            log.warning("security rung %s failed: %s", rid, exc)
            check = _informational(rid, ts, f"not available · {exc.__class__.__name__}", unavailable=True)
            part = {}
        checks.append(check)
        key = METRIC_KEYS[rid]
        if key is None:
            metrics.update(part)
        else:
            metrics[key] = part
    return checks, metrics


# ---------------------------------------------------------------------------- collector


class PortBaseline:
    """The ports that were reachable from the network when Sentinel started (``(port, proto)``).

    Kept in memory for the life of the process: the collector is created again on every
    configuration change, the baseline is not. A port that opens later is ``new``.
    """

    def __init__(self) -> None:
        self.ports: set[tuple[int, str]] | None = None
        self.since: float | None = None

    def remember(self, sockets: Iterable[ListeningSocket], ts: float) -> set[tuple[int, str]]:
        """The baseline, taken from ``sockets`` the first time a socket table could be read."""
        if self.ports is None:
            self.ports = {(s.port, getattr(s, "proto", "tcp") or "tcp") for s in sockets if s.public}
            self.since = ts
        return self.ports


BASELINE = PortBaseline()
"""The process-wide baseline the real collector uses (tests pass their own)."""


def dependency_versions(ctx: CollectorContext) -> dict[str, Any]:
    """The versions the other components already report: nothing is probed again."""

    def metric(component_id: str, key: str) -> Any:
        metrics = getattr(ctx.dependency(component_id), "metrics", None)
        return metrics.get(key) if isinstance(metrics, dict) else None

    return {
        "dspace": metric("backend", "dspace_version"),
        "solr": metric("solr", "version"),
        "postgres": metric("postgres", "version"),
    }


class SecurityCollector(Collector):
    """``security`` 🛡️: the read-only security status of this server (eight rungs)."""

    id: ClassVar[str] = "security"
    interval_key: ClassVar[str] = "security_interval"
    label: ClassVar[str] = "Security"
    emoji: ClassVar[str] = "\U0001f6e1️"
    budget_s: ClassVar[float] = READ_TIMEOUT_S + 2 * systemd.SUBPROCESS_TIMEOUT + WEB_TIMEOUT_S + 5.0
    """The reads (15 s), the firewall units asked together (systemctl twice, 3 s each) and the GET."""

    def __init__(self, sources: HostSources | None = None, baseline: PortBaseline | None = None) -> None:
        self.sources = sources or HostSources()
        self.baseline = baseline or BASELINE

    async def collect(self, ctx: CollectorContext) -> CollectorResult:
        """Read the host once (in a worker thread), ask systemd and the public address, build the rungs."""
        ts = ctx.now
        cfg = ctx.cfg
        src = self.sources
        try:
            facts = await run_blocking("security.read", read_host, src, cfg, timeout=READ_TIMEOUT_S)
        except Exception as exc:  # a stuck filesystem or a broken source: those rungs are informational
            log.debug("security reads failed: %s", exc)
            facts = HostFacts()
        try:
            firewall = await firewall_state(src, facts.ufw_enabled)
        except Exception as exc:
            log.debug("firewall state failed: %s", exc)
            firewall = FirewallState()
        web: WebAnswer | None = None
        public_url = (cfg.dspace.public_url or "").strip()
        if public_url.lower().startswith(("http://", "https://")):
            try:
                web = await src.fetch_headers(ctx.http, public_url)
            except Exception as exc:
                web = WebAnswer(public_url, error=describe_error(exc))
        baseline = self.baseline.remember(facts.sockets, ts) if facts.sockets else self.baseline.ports
        env = _guard("environment", lambda: src.env, None) or {}
        checks, metrics = build_ladder(
            cfg, facts, firewall, web, dependency_versions(ctx), ts, baseline=baseline, env=env
        )
        return CollectorResult(checks=checks, metrics=metrics)


__all__ = [
    "BASELINE",
    "CLIENT_UDP_PORTS",
    "FIREWALL_UNITS",
    "FRAME_HEADERS",
    "HINTS",
    "METRIC_KEYS",
    "PROGRAM_NAMES_NOTE",
    "RUNGS",
    "TAILSCALE_INTERFACE",
    "TAILSCALE_NETWORKS",
    "TAILSCALE_NOTE",
    "TAILSCALE_UDP_PORT",
    "WIREGUARD_NOTE",
    "FileFacts",
    "FirewallState",
    "HostFacts",
    "HostSources",
    "PortBaseline",
    "SecurityCollector",
    "SshFacts",
    "UpdatesFacts",
    "WebAnswer",
    "build_ladder",
    "dependency_versions",
    "files_rung",
    "firewall_rung",
    "firewall_state",
    "grade_ports",
    "group_ports",
    "is_loopback_host",
    "is_tailscale_address",
    "missing_headers",
    "parse_os_release",
    "parse_sshd_config",
    "parse_ufw_enabled",
    "parse_updates",
    "ports_rung",
    "read_host",
    "sentinel_rung",
    "software_entries",
    "software_rung",
    "ssh_rung",
    "unexpected_ports",
    "updates_rung",
    "web_rung",
]
