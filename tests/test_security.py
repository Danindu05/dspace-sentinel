"""Security module (SECURITY-CONTRACT): the eight rungs, the port baseline, the support dates, the sources.

Nothing here reads this machine: every source of the collector (socket table, files, systemd, the
public address) is a fake (:class:`FakeSources`), and the real :class:`HostSources` is only pointed at
temporary files and an ``httpx.MockTransport``.
"""

from __future__ import annotations

import datetime as _dt
import fnmatch
import os
import pwd
from collections import namedtuple
from pathlib import Path
from typing import Any

import httpx
import psutil
import pytest

from dspace_sentinel.alerts import rules
from dspace_sentinel.auth import hash_password
from dspace_sentinel.collectors import security
from dspace_sentinel.collectors.base import CollectorContext, CollectorResult
from dspace_sentinel.collectors.security import (
    REBOOT_REQUIRED,
    REBOOT_REQUIRED_PKGS,
    FileFacts,
    HostFacts,
    PortBaseline,
    SecurityCollector,
    WebAnswer,
)
from dspace_sentinel.config import Config, apply_demo_overrides, merge_patch
from dspace_sentinel.health import rollup
from dspace_sentinel.models import CheckResult
from dspace_sentinel.reports import generator
from dspace_sentinel.state import State
from dspace_sentinel.util import net, support_dates
from dspace_sentinel.util.net import ListeningSocket

NOW = 1_800_000_000.0  # 2027-01-15
SSHD = security.SSHD_CONFIG
CLOUD_INIT = "/etc/ssh/sshd_config.d/50-cloud-init.conf"
LOCAL_CFG = "/dspace/config/local.cfg"
DSPACE_CFG = "/dspace/config/dspace.cfg"
SENTINEL_YAML = "/opt/dspace-sentinel/config/sentinel.yaml"
SENTINEL_ENV = "/opt/dspace-sentinel/config/sentinel.env"
PUBLIC = "https://repo.example.org"


def sock(
    port: int, addr: str, public: bool, process: str | None = None, proto: str = "tcp"
) -> ListeningSocket:
    family = "ipv6" if ":" in addr else "ipv4"
    return ListeningSocket(port, addr, public, family, 4242 if process else None, process, proto)


def real_sock(port: int, addr: str, proto: str = "tcp") -> ListeningSocket:
    """A socket as ``listening_sockets`` reports it: ``public`` from ``net.is_public_address``."""
    return sock(port, addr, net.is_public_address(addr), proto=proto)


HEALTHY_SOCKETS = [
    sock(22, "0.0.0.0", True),
    sock(22, "::", True),
    sock(80, "0.0.0.0", True),
    sock(443, "0.0.0.0", True),
    sock(4000, "0.0.0.0", True),
    sock(8080, "::", True),
    sock(8983, "127.0.0.1", False),
    sock(5432, "127.0.0.1", False),
    sock(9765, "127.0.0.1", False, "python3"),
    sock(53, "127.0.0.53", False, proto="udp"),
    sock(68, "10.0.0.5%eth0", True, "systemd-networkd", proto="udp"),
]
"""A DSpace 9 server: the default rules' ports, Sentinel and the resolver on loopback, DHCP on eth0."""

UBUNTU_2404 = 'PRETTY_NAME="Ubuntu 24.04.1 LTS"\nNAME="Ubuntu"\nVERSION_ID="24.04"\nID=ubuntu\n'
HEALTHY_FILES = {
    SSHD: "Include /etc/ssh/sshd_config.d/*.conf\nKbdInteractiveAuthentication no\nUsePAM yes\n",
    CLOUD_INIT: "PasswordAuthentication no\n",
    security.UFW_CONF: "# /etc/ufw/ufw.conf\nENABLED=yes\nLOGLEVEL=low\n",
    security.UPDATES_AVAILABLE: (
        "\n3 updates can be applied immediately.\n"
        "To see these additional updates run: apt list --upgradable\n\n"
    ),
    security.OS_RELEASE: UBUNTU_2404,
}


def facts_of(path: str, mode: int, owner: str = "tomcat") -> FileFacts:
    return FileFacts(path, 0o100000 | mode, owner, owner, True)


HEALTHY_STATS = {
    LOCAL_CFG: facts_of(LOCAL_CFG, 0o640),
    DSPACE_CFG: facts_of(DSPACE_CFG, 0o644),
    SENTINEL_YAML: facts_of(SENTINEL_YAML, 0o600, "sentinel"),
}
FIREWALLS = {"ufw": "active", "nftables": "inactive", "firewalld": "missing"}
GOOD_HEADERS = {
    "strict-transport-security": "max-age=63072000",
    "x-content-type-options": "nosniff",
    "content-security-policy": "frame-ancestors 'self'",
}


class FakeSources:
    """:class:`security.HostSources` with everything in memory (paths → texts, stats, unit states)."""

    def __init__(
        self,
        *,
        sockets: list[ListeningSocket] | None = None,
        files: dict[str, str] | None = None,
        unreadable: tuple[str, ...] = (),
        stats: dict[str, FileFacts] | None = None,
        units: dict[str, str] | None = None,
        systemd: bool = True,
        answer: WebAnswer | None = None,
        env: dict[str, str] | None = None,
        interfaces: set[str] | None = None,
        root: bool = False,
    ) -> None:
        self.socket_list = list(sockets or [])
        self.interface_names = set(interfaces) if interfaces is not None else {"lo", "eth0"}
        self.root = root
        self.files = dict(files or {})
        self.unreadable = set(unreadable)
        self.stats = dict(stats or {})
        self.units = dict(units or {})
        self.systemd = systemd
        self.answer = answer
        self.env = dict(env or {})
        self.fetched: list[str] = []
        self.asked: list[str] = []

    def sockets(self) -> list[ListeningSocket]:
        return list(self.socket_list)

    def read_text(self, path: str) -> str:
        if path in self.unreadable:
            raise PermissionError(13, "Permission denied", path)
        if path not in self.files:
            raise FileNotFoundError(2, "No such file or directory", path)
        return self.files[path]

    def glob(self, pattern: str) -> list[str]:
        return sorted(p for p in set(self.files) | self.unreadable if fnmatch.fnmatch(p, pattern))

    def stat(self, path: str) -> FileFacts | None:
        return self.stats.get(path)

    def exists(self, path: str) -> bool:
        return path in self.files

    def sentinel_config(self) -> Path:
        return Path(SENTINEL_YAML)

    def interfaces(self) -> set[str]:
        return set(self.interface_names)

    def is_root(self) -> bool:
        return self.root

    def has_systemd(self) -> bool:
        return self.systemd

    async def unit_state(self, unit: str) -> str:
        self.asked.append(unit)
        return self.units.get(unit, "missing")

    async def fetch_headers(self, client: Any, url: str) -> WebAnswer:
        self.fetched.append(url)
        return self.answer or WebAnswer(url, error="connection refused")


def healthy(**changes: Any) -> FakeSources:
    base: dict[str, Any] = {
        "sockets": HEALTHY_SOCKETS,
        "files": HEALTHY_FILES,
        "stats": HEALTHY_STATS,
        "units": FIREWALLS,
        "answer": WebAnswer(f"{PUBLIC}/home", 200, GOOD_HEADERS),
    }
    base.update(changes)
    return FakeSources(**base)


def config(patch: dict[str, Any] | None = None) -> Config:
    cfg = merge_patch(Config(), {"modules": {"security": True}, "dspace": {"public_url": PUBLIC}})
    return merge_patch(cfg, patch) if patch else cfg


def stack_state(
    dspace: str = "9.4", solr: str = "9.8.1", pg: str = "PostgreSQL 16.4 (Ubuntu 16.4-1)"
) -> State:
    """The other components as the stack collectors report them (only their version metrics)."""
    state = State()
    for cid, metrics in (
        ("backend", {"dspace_version": dspace}),
        ("solr", {"version": solr}),
        ("postgres", {"version": pg}),
    ):
        state.set_component(rollup(cid, [], metrics, now=NOW))
    return state


async def collect(
    src: FakeSources,
    cfg: Config | None = None,
    *,
    state: State | None = None,
    baseline: PortBaseline | None = None,
    now: float = NOW,
) -> CollectorResult:
    ctx = CollectorContext(cfg=cfg or config(), http=None, store=None, state=state or State(), now=now)  # type: ignore[arg-type]
    return await SecurityCollector(src, baseline or PortBaseline()).collect(ctx)  # type: ignore[arg-type]


def rungs(result: CollectorResult) -> dict[str, CheckResult]:
    return {r.id: r for r in result.checks}


def informational(r: CheckResult, *, unavailable: bool) -> bool:
    return (
        r.status == "unknown"
        and r.detail.get("informational") is True
        and bool(r.detail.get("unavailable")) is unavailable
    )


# ------------------------------------------------------------------------- whole component


async def test_a_healthy_server() -> None:
    src = healthy()
    result = await collect(src, state=stack_state())
    chain = rungs(result)
    assert list(chain) == [rid for rid, _ in security.RUNGS]
    assert [r.label for r in result.checks] == [
        "Open ports",
        "Firewall",
        "SSH",
        "Web",
        "Updates",
        "Software",
        "DSpace files",
        "Sentinel",
    ]
    assert {rid: r.status for rid, r in chain.items()} == {rid: "ok" for rid in chain}
    assert {rid: r.message for rid, r in chain.items()} == {
        "security.ports": (
            "10 listening · 6 reachable from the network (22, 68/udp, 80, 443, 4000, 8080) · none unexpected"
        ),
        "security.firewall": "ufw active",
        "security.ssh": "root login: prohibit-password · passwords: no · port 22",
        "security.web": "HTTPS on repo.example.org",
        "security.updates": "no pending security updates (3 other updates)",
        "security.software": "DSpace 9.4 · Solr 9.8.1 · PostgreSQL 16.4 · Ubuntu 24.04: all supported",
        "security.files": "local.cfg and sentinel.yaml are not readable by other users",
        "security.sentinel": "listens on 127.0.0.1 only",
    }
    assert not any("hint" in r.detail or "alert" in r.detail for r in result.checks)
    m = result.metrics
    assert (m["listening"], m["reachable"], m["exposed"], m["new"]) == (10, 6, 0, 0)
    by_port = {(p["port"], p["proto"]): p for p in m["ports"]}
    assert by_port[(22, "tcp")] == {
        "port": 22,
        "proto": "tcp",
        "addrs": ["0.0.0.0", "::"],
        "public": True,
        "process": None,
        "pid": None,
        "expected": "open",
        "verdict": "expected",
    }
    assert (by_port[(8983, "tcp")]["verdict"], by_port[(8983, "tcp")]["expected"]) == ("local", "local_only")
    assert by_port[(9765, "tcp")]["verdict"] == "local" and by_port[(9765, "tcp")]["process"] == "python3"
    assert by_port[(53, "udp")]["verdict"] == "local"
    assert (by_port[(68, "udp")]["verdict"], by_port[(68, "udp")]["note"]) == ("expected", "DHCP client")
    assert m["firewall"] == {"active": True, "name": "ufw", "units": FIREWALLS}
    assert m["ssh"] == {
        "port": 22,
        "permit_root_login": "prohibit-password",
        "password_authentication": "no",
        "readable": True,
        "unreadable": None,
    }
    assert m["web"]["public_url"] == PUBLIC and m["web"]["scheme"] == "https"
    assert m["web"]["headers_missing"] == [] and m["web"]["headers_checked"] is True
    assert m["updates"] == {
        "pending": 3,
        "security": 0,
        "reboot_required": False,
        "reboot_pkgs": None,
        "source": security.UPDATES_AVAILABLE,
    }
    assert [(s["name"], s["version"], s["until"], s["status"]) for s in m["software"]] == [
        ("DSpace", "9.4", None, "ok"),
        ("Solr", "9.8.1", None, "ok"),
        ("PostgreSQL", "16.4", "2028-11-09", "ok"),
        ("Ubuntu", "24.04", "2029-04-30", "ok"),
    ]
    assert [(f["path"], f["mode"], f["world_readable"]) for f in m["files"]] == [
        (LOCAL_CFG, "640", False),
        (DSPACE_CFG, "644", True),
        (SENTINEL_YAML, "600", False),
    ]
    assert m["sentinel"] == {"host": "127.0.0.1", "port": 9765, "auth": False, "login": False, "open": False}
    assert src.fetched == [PUBLIC] and src.asked == ["ufw", "nftables", "firewalld"]
    assert rollup("security", result.checks, m, now=NOW).status == "ok"


async def test_every_warning_and_the_three_alerts() -> None:
    sockets = [
        *HEALTHY_SOCKETS[:6],
        sock(8983, "::", True),  # Solr open to the network: the Ports component's warning, named apart
        sock(3306, "0.0.0.0", True, "mysqld"),
        sock(9100, "::", True),
    ]
    files = {
        **HEALTHY_FILES,
        SSHD: "PermitRootLogin yes\nPasswordAuthentication yes\n",
        security.UFW_CONF: "ENABLED=no\n",
        security.UPDATES_AVAILABLE: (
            "12 updates can be applied immediately.\n5 of these updates are standard security updates.\n"
        ),
        REBOOT_REQUIRED: "*** System restart required ***\n",
        REBOOT_REQUIRED_PKGS: "linux-image-6.8.0-45-generic\nlinux-base\nlinux-image-6.8.0-45-generic\n",
        security.OS_RELEASE: 'NAME="Ubuntu"\nVERSION_ID="20.04"\nID=ubuntu\n',
    }
    stats = {**HEALTHY_STATS, LOCAL_CFG: facts_of(LOCAL_CFG, 0o644)}
    src = healthy(
        sockets=sockets,
        files=files,
        stats=stats,
        answer=WebAnswer("http://repo.example.org/", 200, {}),
        env={"SENTINEL_ALLOW_OPEN": "1"},
    )
    baseline = PortBaseline()
    baseline.ports = {(22, "tcp"), (80, "tcp"), (443, "tcp"), (4000, "tcp"), (8080, "tcp"), (8983, "tcp")}
    baseline.ports.add((9100, "tcp"))  # open when Sentinel started: exposed, not new
    cfg = config({"dspace": {"public_url": "http://repo.example.org"}, "server": {"host": "0.0.0.0"}})
    result = await collect(src, cfg, state=stack_state(solr="8.8.2"), baseline=baseline)
    chain = rungs(result)
    assert {r.status for r in result.checks} == {"warn"}
    ports = chain["security.ports"]
    assert ports.message == "8 listening · 8 reachable from the network · 2 unexpected: 3306 (mysqld), 9100"
    assert ports.detail["alert"] is True
    assert ports.detail["hint"] == (
        "Ports other machines can reach that no rule expects: 3306 (mysqld), 9100. If a port is not meant "
        "for other machines, make its program listen on 127.0.0.1 or block it in the firewall; if it is, "
        "add a rule for it under ports in config/sentinel.yaml and restart Sentinel."
    )
    verdicts = {p["port"]: p["verdict"] for p in result.metrics["ports"]}
    assert (verdicts[3306], verdicts[9100], verdicts[8983]) == ("new", "exposed", "expected")
    assert (result.metrics["exposed"], result.metrics["new"]) == (2, 1)
    firewall = chain["security.firewall"]
    assert firewall.message == (
        "no host firewall is active and 2 ports are reachable from the network without a rule, "
        "and 1 local-only port is public"
    )
    assert firewall.detail["alert"] is True and firewall.detail["hint"] == security.HINTS["firewall"]
    assert result.metrics["firewall"]["active"] is False and result.metrics["firewall"]["name"] is None
    ssh = chain["security.ssh"]
    assert (ssh.message, ssh.detail["alert"]) == ("root may log in with a password", True)
    assert ssh.detail["hint"] == (
        "Set PermitRootLogin prohibit-password (or no) in /etc/ssh/sshd_config and reload sshd."
    )
    web = chain["security.web"]
    assert web.message == "the public address is plain HTTP: logins and sessions travel unencrypted"
    assert web.detail["hint"] == security.HINTS["web_http"] and "alert" not in web.detail
    updates = chain["security.updates"]
    assert (
        updates.message
        == "5 security updates pending · reboot required (linux-image-6.8.0-45-generic, linux-base)"
    )
    assert updates.detail["hint"] == "sudo apt update && sudo apt upgrade, then reboot when it says so."
    assert result.metrics["updates"]["reboot_pkgs"] == ["linux-image-6.8.0-45-generic", "linux-base"]
    software = chain["security.software"]
    assert software.message == (
        "Solr 8.8.2 is affected by Log4Shell (CVE-2021-44228): upgrade to 8.11.1 or later (+1 more)"
    )
    assert "Ubuntu 20.04 reached end of standard support on 2025-05-31." in software.detail["hint"]
    files = chain["security.files"]
    assert files.message == (
        "local.cfg is readable by every user on this server and holds the database password "
        "(mode 644, tomcat:tomcat)"
    )
    assert files.detail["hint"] == (
        "chmod 640 /dspace/config/local.cfg and keep it owned by the Tomcat user and its group."
    )
    sentinel = chain["security.sentinel"]
    assert sentinel.message == "listens on 0.0.0.0 without a password (SENTINEL_ALLOW_OPEN)"
    assert result.metrics["sentinel"]["open"] is True
    # only the ports, firewall and SSH warnings carry detail.alert, and those three alert
    assert {rid for rid, r in chain.items() if r.detail.get("alert")} == {
        "security.ports",
        "security.firewall",
        "security.ssh",
    }
    comp = rollup("security", result.checks, result.metrics, now=NOW)
    assert comp.status == "warn" and comp.reason == ports.message
    obs = {o.rule: o for o in rules.component_rules(comp, cfg)}
    assert set(obs) == {"security_ports", "security_firewall", "security_ssh"}
    assert all(o.severity == "warning" and o.component == "security" for o in obs.values())
    assert (
        obs["security_ports"].key == "security_ports@security"
        and obs["security_ports"].message == ports.message
    )


async def test_nothing_readable_is_informational_never_unknown() -> None:
    """No socket table, no systemd, an unreadable sshd_config, no update-notifier (Debian): Security
    stays healthy on the rungs it could check."""
    src = FakeSources(
        sockets=[],
        files={
            security.OS_RELEASE: 'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\nVERSION_ID="12"\nID=debian\n'
        },
        unreadable=(SSHD,),
        stats={SENTINEL_YAML: facts_of(SENTINEL_YAML, 0o600, "sentinel")},
        systemd=False,
    )
    cfg = config(
        {
            "dspace": {"public_url": ""},
            "modules": {"backend": True},
            "server": {"host": "0.0.0.0", "auth_token": "abcdefghijklmnop"},
        }
    )
    result = await collect(src, cfg, state=stack_state(dspace="8.1", solr="", pg=""))
    chain = rungs(result)
    assert informational(chain["security.ports"], unavailable=True)
    assert chain["security.ports"].message == "not available · the socket table could not be read"
    assert informational(chain["security.firewall"], unavailable=True)
    assert chain["security.firewall"].message == "not available · systemctl is not available"
    assert informational(chain["security.ssh"], unavailable=True)
    assert chain["security.ssh"].message == "not available · sshd_config is not readable by Sentinel's user"
    assert result.metrics["ssh"]["readable"] is False and result.metrics["ssh"]["port"] is None
    assert informational(chain["security.web"], unavailable=False)
    assert chain["security.web"].message == "no public address set"
    assert informational(chain["security.updates"], unavailable=True)
    assert chain["security.updates"].message == (
        "not available · no update information on this system (Ubuntu's update-notifier)"
    )
    assert chain["security.software"].message == "DSpace 8.1 · Debian 12: all supported"
    assert (
        chain["security.files"].message
        == "sentinel.yaml is not readable by other users · local.cfg not found"
    )
    assert chain["security.sentinel"].message == "listens on 0.0.0.0 with a password"
    assert result.metrics["ports"] == [] and result.metrics["listening"] is None
    assert src.fetched == [] and src.asked == []
    comp = rollup("security", result.checks, result.metrics, now=NOW)
    assert comp.status == "ok"
    obs = rules.component_rules(comp, cfg)
    neutral = {o.rule for o in obs if not o.ok and o.severity is None}
    assert neutral == {"security_ports", "security_firewall", "security_ssh"}


async def test_a_failing_source_never_raises_out_of_collect() -> None:
    class Broken(FakeSources):
        def sockets(self) -> list[ListeningSocket]:
            raise RuntimeError("psutil exploded")

        def read_text(self, path: str) -> str:
            raise ValueError("bad bytes")

        def stat(self, path: str) -> FileFacts | None:
            raise RuntimeError("stat exploded")

        def sentinel_config(self) -> Path:
            raise RuntimeError("no config path")

        async def unit_state(self, unit: str) -> str:
            raise RuntimeError("dbus exploded")

        async def fetch_headers(self, client: Any, url: str) -> WebAnswer:
            raise httpx.ConnectTimeout("too slow")

    result = await collect(Broken())
    chain = rungs(result)
    assert len(chain) == 8 and not any(rid.endswith(".error") for rid in chain)
    assert chain["security.firewall"].message == "not available · systemctl did not answer"
    assert (
        chain["security.web"].message == "HTTPS on repo.example.org · response headers not checked (timeout)"
    )
    assert result.metrics["web"]["headers_checked"] is False and result.metrics["web"]["error"] == "timeout"
    assert chain["security.sentinel"].status == "ok"
    assert rollup("security", result.checks, result.metrics, now=NOW).status == "ok"


# ------------------------------------------------------------------------------- ports


async def test_the_baseline_makes_a_later_port_new_and_survives_a_new_collector() -> None:
    baseline = PortBaseline()
    first = await collect(healthy(sockets=[*HEALTHY_SOCKETS, sock(3306, "0.0.0.0", True)]), baseline=baseline)
    ports = rungs(first)["security.ports"]
    assert ports.status == "warn" and "alert" not in ports.detail  # open since the start: exposed
    assert ports.message.endswith("· 1 unexpected: 3306")
    assert baseline.ports is not None and (3306, "tcp") in baseline.ports and baseline.since == NOW
    later = [*HEALTHY_SOCKETS, sock(3306, "0.0.0.0", True), sock(6379, "::", True, "redis-server")]
    second = await collect(healthy(sockets=later), baseline=baseline, now=NOW + 900)
    ports = rungs(second)["security.ports"]
    assert ports.detail["alert"] is True and ports.message.endswith("2 unexpected: 3306, 6379 (redis-server)")
    assert {p["port"]: p["verdict"] for p in second.metrics["ports"]}[6379] == "new"
    assert baseline.since == NOW  # taken once
    # a rule for it makes it expected again, and the alert clears
    cfg = config({"ports": [*Config().model_dump(mode="json")["ports"], {"port": 6379, "expect": "open"}]})
    third = await collect(healthy(sockets=later), cfg, baseline=baseline, now=NOW + 1800)
    assert "alert" not in rungs(third)["security.ports"].detail


async def test_no_baseline_from_an_unreadable_socket_table() -> None:
    baseline = PortBaseline()
    await collect(healthy(sockets=[]), baseline=baseline)
    assert baseline.ports is None
    await collect(healthy(sockets=[*HEALTHY_SOCKETS, sock(3306, "0.0.0.0", True)]), baseline=baseline)
    assert baseline.ports is not None and (3306, "tcp") in baseline.ports


async def test_sentinel_on_every_interface_is_expected_not_new() -> None:
    """Sentinel binds its port after the first check: that is not a new port."""
    baseline = PortBaseline()
    await collect(healthy(), baseline=baseline)
    result = await collect(
        healthy(sockets=[*HEALTHY_SOCKETS[:8], sock(9765, "0.0.0.0", True)]), baseline=baseline
    )
    entry = next(p for p in result.metrics["ports"] if p["port"] == 9765)
    assert (entry["verdict"], entry["note"]) == ("expected", "Sentinel")
    assert rungs(result)["security.ports"].status == "ok"


def test_a_long_port_list_is_capped_in_the_message() -> None:
    entries = security.grade_ports(
        security.group_ports([sock(20000 + i, "0.0.0.0", True) for i in range(11)]), Config(), set()
    )
    check, metrics = security.ports_rung(entries, NOW)
    assert check.message.endswith(
        "11 unexpected: 20000, 20001, 20002, 20003, 20004, 20005, 20006, 20007 +3 more"
    )
    assert metrics["new"] == 11 and check.detail["alert"] is True


OWNER_SOCKETS = [
    real_sock(22, "0.0.0.0"),
    real_sock(22, "::"),
    real_sock(4000, "::"),
    real_sock(8080, "::"),
    real_sock(8983, "::"),  # Solr on every interface: its local_only rule makes it expected here
    real_sock(7983, "::ffff:127.0.0.1"),  # Solr's stop port, IPv4-mapped loopback (T20)
    real_sock(5432, "127.0.0.1"),
    real_sock(19999, "0.0.0.0"),  # Netdata without a login: the one true finding
    real_sock(40341, "100.101.102.103"),  # tailscaled (T21)
    real_sock(41246, "fd7a:115c:a1e0::a1b2:c3d4"),
    real_sock(41641, "0.0.0.0", "udp"),  # WireGuard
    real_sock(41641, "::", "udp"),
]
"""The owner's test server (SERVER-TEST.md, 2026-10-07): DSpace 8.1, ufw off, Tailscale, Netdata."""


async def test_the_owners_socket_table() -> None:
    """T20, T21 and F7 #3/#4: loopback stays local, Tailscale's ports are not exposed, the counts
    agree, and the page says why the programs have no names."""
    files = {**HEALTHY_FILES, security.UFW_CONF: "ENABLED=no\n"}
    src = healthy(sockets=OWNER_SOCKETS, files=files, interfaces={"lo", "enp1s0", "tailscale0"})
    result = await collect(src)
    chain = rungs(result)
    m = result.metrics
    verdicts = {(p["port"], p["proto"]): p["verdict"] for p in m["ports"]}
    assert verdicts == {
        (22, "tcp"): "expected",
        (4000, "tcp"): "expected",
        (5432, "tcp"): "local",
        (7983, "tcp"): "local",
        (8080, "tcp"): "expected",
        (8983, "tcp"): "expected",
        (19999, "tcp"): "exposed",
        (40341, "tcp"): "tailscale",
        (41246, "tcp"): "tailscale",
        (41641, "udp"): "expected",
    }
    notes = {(p["port"], p["proto"]): p.get("note") for p in m["ports"] if p.get("note")}
    assert notes == {
        (40341, "tcp"): "reachable through Tailscale only",
        (41246, "tcp"): "reachable through Tailscale only",
        (41641, "udp"): "Tailscale's own port (WireGuard)",
    }
    assert (m["listening"], m["reachable"], m["tailscale"], m["exposed"], m["new"]) == (10, 6, 2, 1, 0)
    ports = chain["security.ports"]
    assert ports.status == "warn" and "alert" not in ports.detail  # open since the start: exposed
    assert ports.message == (
        "10 listening · 6 reachable from the network · 2 through Tailscale only · 1 unexpected: 19999"
    )
    assert ports.detail["note"] == (
        "Program names are known only for Sentinel's own user; run `sudo ss -ltnp` on the server to "
        "see the others."
    )
    # the firewall rung counts what the ports rung calls unexpected (the Firewall tile shows
    # metrics.exposed); the public local_only Solr is named on its own
    firewall = chain["security.firewall"]
    assert firewall.message == (
        "no host firewall is active and 1 port is reachable from the network without a rule, "
        "and 1 local-only port is public"
    )
    assert m["exposed"] == 1 and firewall.detail["alert"] is True
    # without Tailscale's interface the WireGuard port is just another port on every interface
    plain = await collect(healthy(sockets=OWNER_SOCKETS, files=files))
    entry = next(p for p in plain.metrics["ports"] if (p["port"], p["proto"]) == (41641, "udp"))
    assert entry["verdict"] == "exposed" and "note" not in entry
    assert rungs(plain)["security.ports"].message.endswith("2 unexpected: 19999, 41641/udp")
    # as root Sentinel knows every program: no note
    as_root = await collect(healthy(sockets=OWNER_SOCKETS, files=files, root=True))
    assert "note" not in rungs(as_root)["security.ports"].detail


def test_tailscale_only_when_every_reachable_address_is_tailscales() -> None:
    entries = security.grade_ports(
        security.group_ports(
            [
                real_sock(5000, "100.101.102.103"),
                real_sock(5000, "127.0.0.1"),  # loopback beside it changes nothing
                real_sock(6000, "100.101.102.103"),
                real_sock(6000, "10.0.2.15"),  # also on the LAN: exposed
                real_sock(7000, "::ffff:100.100.1.2"),
                real_sock(41641, "100.101.102.103", "udp"),  # not a wildcard: graded by its address
                real_sock(41641, "0.0.0.0", "tcp"),  # the WireGuard port is UDP
            ]
        ),
        Config(),
        None,
        {"tailscale0"},
    )
    assert {(e["port"], e["proto"]): e["verdict"] for e in entries} == {
        (5000, "tcp"): "tailscale",
        (6000, "tcp"): "exposed",
        (7000, "tcp"): "tailscale",
        (41641, "tcp"): "exposed",
        (41641, "udp"): "tailscale",
    }
    check, metrics = security.ports_rung(entries, NOW)
    assert (metrics["reachable"], metrics["tailscale"], metrics["exposed"]) == (2, 3, 2)
    assert check.message == (
        "5 listening · 2 reachable from the network · 3 through Tailscale only · 2 unexpected: 6000, 41641"
    )
    wireguard = security.grade_ports(
        security.group_ports([real_sock(41641, "0.0.0.0", "udp"), real_sock(41641, "::", "udp")]),
        Config(),
        set(),
        {"lo", "tailscale0"},
    )
    assert (wireguard[0]["verdict"], wireguard[0]["note"]) == ("expected", "Tailscale's own port (WireGuard)")
    assert generator.PORT_VERDICT_LABELS["tailscale"] == ("Tailscale", "plain")  # the report's pill
    assert security.is_tailscale_address("fd7a:115c:a1e0:ab12::1")
    assert not security.is_tailscale_address("100.128.0.1") and not security.is_tailscale_address("fd7a::1")


def test_program_names_note_only_without_root_and_a_name_missing() -> None:
    entries = security.grade_ports(
        security.group_ports([sock(22, "0.0.0.0", True), sock(9765, "127.0.0.1", False, "python3")]),
        Config(),
        None,
    )
    note = security.PROGRAM_NAMES_NOTE
    assert security.ports_rung(entries, NOW, root=False)[0].detail["note"] == note
    assert "note" not in security.ports_rung(entries, NOW, root=True)[0].detail
    assert "note" not in security.ports_rung(entries, NOW)[0].detail  # not known (the demo)
    named = security.grade_ports(security.group_ports([sock(22, "0.0.0.0", True, "sshd")]), Config(), None)
    assert "note" not in security.ports_rung(named, NOW, root=False)[0].detail


# ---------------------------------------------------------------------------- firewall


@pytest.mark.parametrize(
    "units,ufw_conf,expected",
    [
        ({"ufw": "active"}, "ENABLED=yes\n", (True, "ufw")),
        ({"ufw": "active"}, "ENABLED=no\n", (False, None)),  # Ubuntu keeps the unit active when disabled
        ({"ufw": "inactive"}, "ENABLED=yes\n", (False, None)),  # stopped: its rules are flushed
        ({"ufw": "active"}, None, (True, "ufw")),  # no ufw.conf to read: the unit alone
        ({"nftables": "active"}, None, (True, "nftables")),
        ({"firewalld": "active"}, "ENABLED=no\n", (True, "firewalld")),
        ({"ufw": "unknown", "nftables": "unknown", "firewalld": "unknown"}, None, (None, None)),
    ],
)
async def test_firewall_state(units: dict[str, str], ufw_conf: str | None, expected: tuple[Any, Any]) -> None:
    src = FakeSources(units=units)
    enabled = security.parse_ufw_enabled(ufw_conf) if ufw_conf is not None else None
    state = await security.firewall_state(src, enabled)  # type: ignore[arg-type]
    assert (state.active, state.name) == expected


async def test_firewall_without_systemd() -> None:
    assert (await security.firewall_state(FakeSources(systemd=False), True)).name == "ufw"  # type: ignore[arg-type]
    off = await security.firewall_state(FakeSources(systemd=False), None)  # type: ignore[arg-type]
    assert off.active is None


async def test_no_firewall_and_nothing_exposed_is_informational() -> None:
    files = {**HEALTHY_FILES, security.UFW_CONF: "ENABLED=no\n"}
    result = await collect(healthy(files=files))
    firewall = rungs(result)["security.firewall"]
    assert informational(firewall, unavailable=False)
    assert firewall.message == (
        "no host firewall active (this server relies on the network's firewall, which Sentinel cannot see)"
    )
    # one exposed port is enough for the warning (and its alert)
    exposed = await collect(healthy(files=files, sockets=[*HEALTHY_SOCKETS, sock(3306, "0.0.0.0", True)]))
    firewall = rungs(exposed)["security.firewall"]
    assert firewall.status == "warn" and firewall.detail["alert"] is True
    assert (
        firewall.message
        == "no host firewall is active and 1 port is reachable from the network without a rule"
    )
    assert exposed.metrics["exposed"] == 1  # the number the Firewall tile shows
    # a public local_only port alone: the Ports component's warning, named here on its own
    solr = await collect(healthy(files=files, sockets=[*HEALTHY_SOCKETS, sock(8983, "::", True)]))
    firewall = rungs(solr)["security.firewall"]
    assert firewall.status == "warn" and solr.metrics["exposed"] == 0
    assert firewall.message == "no host firewall is active and 1 local-only port is public"


# ---------------------------------------------------------------------------------- SSH


def parse(files: dict[str, str], unreadable: tuple[str, ...] = ()) -> security.SshFacts:
    src = FakeSources(files=files, unreadable=unreadable)
    return security.parse_sshd_config(src.read_text, src.glob)


def test_sshd_config_first_value_wins_and_includes_come_first() -> None:
    facts = parse(
        {
            SSHD: "Include /etc/ssh/sshd_config.d/*.conf\nPermitRootLogin yes\nPort 2222\nPort 2200\n",
            "/etc/ssh/sshd_config.d/10-hardening.conf": "PermitRootLogin no\n",
            "/etc/ssh/sshd_config.d/60-late.conf": "PermitRootLogin yes\npasswordauthentication NO\n",
        }
    )
    assert (facts.permit_root_login, facts.password_authentication, facts.port) == ("no", "no", 2222)
    assert facts.found and facts.readable and facts.unreadable == []


def test_sshd_config_relative_include_match_blocks_and_spellings() -> None:
    facts = parse(
        {
            SSHD: (
                "# comment\nInclude sshd_config.d/*.conf\nMatch User backup\n  PermitRootLogin yes\n"
                "  PasswordAuthentication yes\nMatch all\nPort=2201\n"
            ),
            "/etc/ssh/sshd_config.d/a.conf": (
                "PermitRootLogin without-password\nInclude /etc/ssh/sshd_config\n"
            ),
        }
    )
    assert (facts.permit_root_login, facts.password_authentication, facts.port) == (
        "prohibit-password",
        "yes",
        2201,
    )


def test_sshd_config_defaults_missing_and_unreadable() -> None:
    defaults = parse({SSHD: "UsePAM yes\n"})
    assert (defaults.permit_root_login, defaults.password_authentication, defaults.port) == (
        "prohibit-password",
        "yes",
        22,
    )
    missing = parse({})
    assert missing.found is False and missing.readable is False
    locked = parse({}, unreadable=(SSHD,))
    assert locked.found is True and locked.readable is False


def test_ssh_rung_states() -> None:
    ok, _ = security.ssh_rung(parse({SSHD: "PasswordAuthentication yes\n"}), [], NOW)
    assert (ok.status, ok.message) == ("ok", "root login: prohibit-password · passwords: yes · port 22")
    assert (
        ok.detail["hint"]
        == "Keys instead of passwords: set PasswordAuthentication no once every admin has a key."
    )
    keys_only, _ = security.ssh_rung(
        parse({SSHD: "PermitRootLogin yes\nPasswordAuthentication no\n"}), [], NOW
    )
    assert (keys_only.status, keys_only.message) == ("warn", "root may log in with a key (passwords are off)")
    assert "alert" not in keys_only.detail
    doubtful = parse(
        {SSHD: "Include /etc/ssh/sshd_config.d/*.conf\nPermitRootLogin yes\n"},
        unreadable=("/etc/ssh/sshd_config.d/50-cloud-init.conf",),
    )
    assert doubtful.unreadable == ["/etc/ssh/sshd_config.d/50-cloud-init.conf"]
    unsure, metrics = security.ssh_rung(doubtful, [], NOW)
    assert unsure.status == "warn" and "alert" not in unsure.detail
    assert (
        unsure.message
        == "root may log in with a password (an included file Sentinel cannot read may change this)"
    )
    assert metrics["unreadable"] == ["/etc/ssh/sshd_config.d/50-cloud-init.conf"]
    partly, _ = security.ssh_rung(
        parse(
            {SSHD: "PermitRootLogin no\nInclude /etc/ssh/sshd_config.d/*.conf\n"}, unreadable=(CLOUD_INIT,)
        ),
        [],
        NOW,
    )
    assert partly.status == "ok" and partly.message.endswith("· port 22 · 1 included file not readable")


def test_no_ssh_server() -> None:
    nothing, metrics = security.ssh_rung(security.SshFacts(), [], NOW)
    assert informational(nothing, unavailable=False) and nothing.message == "no SSH server found"
    assert metrics == {
        "port": None,
        "permit_root_login": None,
        "password_authentication": None,
        "readable": False,
    }
    port_22 = security.grade_ports(security.group_ports([sock(22, "0.0.0.0", True)]), Config(), None)
    other, _ = security.ssh_rung(security.SshFacts(), port_22, NOW)
    assert informational(other, unavailable=True)
    assert other.message == "not available · port 22 listens, but /etc/ssh/sshd_config was not found"


# ---------------------------------------------------------------------------------- web


def test_web_rung_headers_redirects_and_plain_ports() -> None:
    check, metrics = security.web_rung(PUBLIC, WebAnswer(f"{PUBLIC}/", 200, {"server": "nginx"}), [], NOW)
    assert (check.status, check.message) == ("ok", "HTTPS on repo.example.org")
    assert metrics["headers_missing"] == [
        "Strict-Transport-Security",
        "X-Content-Type-Options",
        "X-Frame-Options or Content-Security-Policy",
    ]
    assert check.detail["hint"] == (
        "Missing response headers: Strict-Transport-Security, X-Content-Type-Options, X-Frame-Options or "
        "Content-Security-Policy. They are set in the web server in front of DSpace (nginx add_header)."
    )
    framed = security.missing_headers({"X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff"}, True)
    assert framed == ["Strict-Transport-Security"]
    redirected, metrics = security.web_rung(
        "http://repo.example.org", WebAnswer(f"{PUBLIC}/", 200, GOOD_HEADERS), [], NOW
    )
    assert redirected.status == "warn" and redirected.detail["hint"] == security.HINTS["web_redirect"]
    assert metrics["final_url"] == f"{PUBLIC}/" and metrics["headers_missing"] == []
    plain_http, metrics = security.web_rung(
        "http://repo.example.org", WebAnswer("http://repo.example.org/"), [], NOW
    )
    assert (
        "Strict-Transport-Security" not in metrics["headers_missing"]
    )  # HSTS only means something over HTTPS
    assert plain_http.detail["hint"] == security.HINTS["web_http"]
    public_8080 = security.grade_ports(security.group_ports([sock(8080, "::", True)]), Config(), None)
    served, _ = security.web_rung("", None, public_8080, NOW)
    assert (served.status, served.message) == (
        "warn",
        "DSpace is served over plain HTTP on port 8080 to the network",
    )
    assert served.detail["hint"] == security.HINTS["web_plain_port"]
    odd, _ = security.web_rung("ftp://repo.example.org", None, [], NOW)
    assert informational(odd, unavailable=True)


async def test_one_get_of_the_public_address_follows_redirects_and_reads_no_body() -> None:
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(f"{request.method} {request.url}")
        if request.url.scheme == "http":
            return httpx.Response(301, headers={"Location": f"{PUBLIC}/home"})
        return httpx.Response(200, headers={"Strict-Transport-Security": "max-age=1"}, content=b"x" * 200_000)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        answer = await security.HostSources().fetch_headers(client, "http://repo.example.org/")
    assert asked == ["GET http://repo.example.org/", f"GET {PUBLIC}/home"]
    assert answer.url == f"{PUBLIC}/home" and answer.status_code == 200 and answer.error is None
    assert answer.headers["strict-transport-security"] == "max-age=1"

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as client:
        failed = await security.HostSources().fetch_headers(client, PUBLIC)
    assert failed.error == "connection refused" and failed.url == PUBLIC


async def test_no_get_without_an_http_public_address() -> None:
    src = healthy()
    await collect(src, config({"dspace": {"public_url": ""}}))
    assert src.fetched == []


# ------------------------------------------------------------------------------ updates


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            "14 updates can be applied immediately.\n6 of these updates are standard security updates.\n",
            (14, 6),
        ),
        ("1 update can be applied immediately.\n1 of these updates is a standard security update.\n", (1, 1)),
        ("14 packages can be updated.\n6 updates are security updates.\n", (14, 6)),
        (
            "Expanded Security Maintenance for Applications is not enabled.\n\n2 updates can be applied "
            "immediately.\n\n5 additional security updates can be applied with ESM Apps.\n",
            (2, 0),
        ),
        ("0 updates can be applied immediately.\n", (0, 0)),
        ("\n", (0, 0)),
    ],
)
def test_parse_updates(text: str, expected: tuple[int, int]) -> None:
    assert security.parse_updates(text) == expected


def test_updates_rung_states() -> None:
    U = security.UpdatesFacts
    none, _ = security.updates_rung(U(available=True, pending=0, security=0), NOW)
    assert (none.status, none.message) == ("ok", "no pending updates")
    one, _ = security.updates_rung(U(available=True, pending=2, security=1), NOW)
    assert (one.status, one.message) == ("warn", "1 security update pending")
    other, _ = security.updates_rung(U(available=True, pending=1, security=0), NOW)
    assert other.message == "no pending security updates (1 other update)"
    reboot, _ = security.updates_rung(
        U(reboot_required=True, reboot_pkgs=["linux-image-6.8.0-45-generic", "linux-base", "systemd"]), NOW
    )
    assert (reboot.status, reboot.message) == (
        "warn",
        "reboot required (linux-image-6.8.0-45-generic, linux-base +1 more)",
    )
    bare, _ = security.updates_rung(U(reboot_required=True), NOW)
    assert bare.message == "reboot required"
    unknown, metrics = security.updates_rung(U(), NOW)
    assert informational(unknown, unavailable=True) and metrics["pending"] is None


# ----------------------------------------------------------------------------- software


async def test_software_comes_from_the_other_components_and_os_release() -> None:
    result = await collect(healthy(), state=State())  # the stack is off or not checked yet
    software = rungs(result)["security.software"]
    assert software.message == "Ubuntu 24.04: all supported"
    soon = await collect(healthy(), state=stack_state(pg="PostgreSQL 15.8"), now=NOW + 250 * 86400)
    assert rungs(soon)["security.software"].message == (
        "DSpace 9.4 · Solr 9.8.1 · PostgreSQL 15.8 · Ubuntu 24.04: all supported "
        "· PostgreSQL 15.8 support ends 2027-11-11"
    )
    files = {**HEALTHY_FILES, security.OS_RELEASE: 'NAME="Rocky Linux"\nVERSION_ID="9.4"\nID="rocky"\n'}
    rocky = await collect(healthy(files=files), state=State())
    check = rungs(rocky)["security.software"]
    assert (
        informational(check, unavailable=False)
        and check.message == "no support date known for Rocky Linux 9.4"
    )
    nothing = await collect(healthy(files={**HEALTHY_FILES, security.OS_RELEASE: ""}), state=State())
    assert informational(rungs(nothing)["security.software"], unavailable=True)


def test_software_rung_one_problem_is_the_message() -> None:
    today = _dt.date(2027, 1, 15)
    entries = security.software_entries({"dspace": "7.6.5"}, {"ID": "ubuntu", "VERSION_ID": "24.04"}, today)
    check, metrics = security.software_rung(entries, NOW)
    assert check.status == "warn" and check.message == "DSpace 7.6.5 reached end of life on 2026-05-28"
    assert "hint" not in check.detail
    assert metrics["software"][0] == {
        "name": "DSpace",
        "version": "7.6.5",
        "until": "2026-05-28",
        "status": "ended",
        "note": "end of life on 2026-05-28",
    }


@pytest.mark.parametrize("major,until", sorted(support_dates.POSTGRES_FINAL_RELEASE.items()))
def test_postgres_support_boundaries(major: int, until: str) -> None:
    end = _dt.date.fromisoformat(until)

    def status(day: _dt.date) -> str:
        entry = support_dates.postgres_support(f"PostgreSQL {major}.3 (Ubuntu {major}.3-1)", day)
        assert entry is not None and entry["version"] == f"{major}.3" and entry["until"] == until
        return entry["status"]

    assert status(end - _dt.timedelta(days=91)) == "ok"
    assert status(end - _dt.timedelta(days=90)) == "soon"
    assert status(end) == "soon"  # the last supported day
    assert status(end + _dt.timedelta(days=1)) == "ended"


@pytest.mark.parametrize(
    "os_id,version,until",
    [("ubuntu", v, d) for v, d in support_dates.UBUNTU_END_OF_STANDARD_SUPPORT.items()]
    + [("debian", v, d) for v, d in support_dates.DEBIAN_END_OF_LTS.items()],
)
def test_os_support_boundaries(os_id: str, version: str, until: str) -> None:
    end = _dt.date.fromisoformat(until)

    def status(day: _dt.date) -> str:
        entry = support_dates.os_support({"ID": os_id, "VERSION_ID": version}, day)
        assert entry is not None and entry["until"] == until
        return entry["status"]

    assert status(end - _dt.timedelta(days=91)) == "ok"
    assert status(end - _dt.timedelta(days=89)) == "soon"
    assert status(end + _dt.timedelta(days=1)) == "ended"
    entry = support_dates.os_support({"ID": os_id, "VERSION_ID": version}, end + _dt.timedelta(days=1))
    assert entry is not None
    kind = "standard support" if os_id == "ubuntu" else "LTS support"
    assert (
        support_dates.problem_sentence(entry) == f"{entry['name']} {version} reached end of {kind} on {until}"
    )


def test_support_verdicts_without_dates() -> None:
    today = _dt.date(2026, 10, 5)
    status = {
        "DSpace 6.4": support_dates.dspace_support("6.4", today),
        "DSpace 7.6": support_dates.dspace_support("DSpace 7.6", today),
        "DSpace 9.4": support_dates.dspace_support("9.4", today),
        "DSpace 10.1-SNAPSHOT": support_dates.dspace_support("10.1-SNAPSHOT", today),
        "Solr 7.7.3": support_dates.solr_support("7.7.3", today),
        "Solr 8.0.0": support_dates.solr_support("8.0.0", today),
        "Solr 8.11.0": support_dates.solr_support("8.11.0", today),
        "Solr 8.11.1": support_dates.solr_support("8.11.1", today),
        "Solr 9.6.1": support_dates.solr_support("9.6.1", today),
        "PostgreSQL 10": support_dates.postgres_support("PostgreSQL 10.23", today),
        "PostgreSQL 18": support_dates.postgres_support("18.0", today),
        "Ubuntu 26.04": support_dates.os_support({"ID": "ubuntu", "VERSION_ID": "26.04"}, today),
        "Debian 13": support_dates.os_support({"ID": "debian", "VERSION_ID": "13"}, today),
        "Rocky": support_dates.os_support({"ID": "rocky", "VERSION_ID": "9.4", "NAME": "Rocky Linux"}, today),
    }
    assert {name: e["status"] for name, e in status.items() if e is not None} == {
        "DSpace 6.4": "ended",
        "DSpace 7.6": "ended",
        "DSpace 9.4": "ok",
        "DSpace 10.1-SNAPSHOT": "ok",
        "Solr 7.7.3": "ended",
        "Solr 8.0.0": "affected",
        "Solr 8.11.0": "affected",
        "Solr 8.11.1": "ok",
        "Solr 9.6.1": "ok",
        "PostgreSQL 10": "ended",
        "PostgreSQL 18": "ok",
        "Ubuntu 26.04": "unknown",
        "Debian 13": "unknown",
        "Rocky": "unknown",
    }
    assert status["Solr 8.11.0"]["note"] == support_dates.LOG4SHELL_NOTE
    assert support_dates.problem_sentence(status["Solr 7.7.3"]) == "Solr 7.7.3 is no longer supported"
    assert (
        support_dates.problem_sentence(status["DSpace 7.6"]) == "DSpace 7.6 reached end of life on 2026-05-28"
    )
    for missing in (None, "", "unknown", 7):
        assert support_dates.dspace_support(missing, today) is None
        assert support_dates.solr_support(missing, today) is None
        assert support_dates.postgres_support(missing, today) is None
    assert support_dates.os_support(None, today) is None and support_dates.CHECKED_ON == "2026-10-05"


# -------------------------------------------------------------------------------- files


def test_files_rung_states() -> None:
    def run(stats: list[FileFacts], dspace_on: bool = True) -> CheckResult:
        facts = HostFacts(
            files=stats, local_cfg=LOCAL_CFG, sentinel_yaml=SENTINEL_YAML, sentinel_env=SENTINEL_ENV
        )
        return security.files_rung(facts, dspace_on, NOW)[0]

    open_yaml = run([facts_of(LOCAL_CFG, 0o640), facts_of(SENTINEL_YAML, 0o644, "sentinel")])
    assert open_yaml.status == "warn" and open_yaml.message == (
        "sentinel.yaml is readable by other users (mode 644): "
        "it holds the Telegram tokens and the email password"
    )
    assert open_yaml.detail["hint"] == f"chmod 600 {SENTINEL_YAML}: only Sentinel's user needs to read it."
    both = run([facts_of(LOCAL_CFG, 0o604), facts_of(SENTINEL_ENV, 0o644, "sentinel")])
    assert both.message.startswith("local.cfg is readable by every user") and both.message.endswith(
        "(+1 more)"
    )
    assert (
        both.detail["hint"].startswith("chmod 640 /dspace/config/local.cfg")
        and SENTINEL_ENV in both.detail["hint"]
    )
    owners = run([facts_of(SENTINEL_YAML, 0o600, "sentinel"), facts_of(SENTINEL_ENV, 0o640, "sentinel")])
    assert owners.message == (
        "sentinel.yaml and sentinel.env are not readable by other users · local.cfg not found"
    )
    server_only = run([facts_of(SENTINEL_YAML, 0o600, "sentinel")], dspace_on=False)
    assert server_only.message == "sentinel.yaml is not readable by other users"
    nothing = run([], dspace_on=False)
    assert informational(nothing, unavailable=True)
    assert nothing.message == "not available · no configuration file could be checked"
    assert run([facts_of(DSPACE_CFG, 0o644)]).message == "not available · local.cfg not found"


def test_read_host_looks_where_the_configuration_says() -> None:
    src = healthy(stats={"/opt/dspace/config/local.cfg": facts_of("/opt/dspace/config/local.cfg", 0o640)})
    facts = security.read_host(src, config({"dspace": {"install_dir": "/opt/dspace"}}))  # type: ignore[arg-type]
    assert facts.local_cfg == "/opt/dspace/config/local.cfg" and facts.local_cfg_found
    assert (facts.sentinel_yaml, facts.sentinel_env) == (SENTINEL_YAML, SENTINEL_ENV)
    assert (
        facts.ufw_enabled is True
        and facts.os_release is not None
        and facts.os_release["VERSION_ID"] == "24.04"
    )
    assert facts.updates.available and facts.updates.pending == 3 and facts.updates.reboot_required is False


def test_host_sources_stat_and_read(tmp_path: Path) -> None:
    secret = tmp_path / "local.cfg"
    secret.write_text("db.password = abcdefghijklmnop\n")
    secret.chmod(0o640)
    src = security.HostSources(env={})
    found = src.stat(str(secret))
    assert found is not None and found.mode_text == "640" and found.world_readable is False
    assert found.readable is True and found.owner == pwd.getpwuid(os.getuid()).pw_name
    assert found.to_metric()["mode"] == "640"
    secret.chmod(0o644)
    assert src.stat(str(secret)).world_readable is True  # type: ignore[union-attr]
    assert src.stat(str(tmp_path / "nope.cfg")) is None and src.stat(str(tmp_path)) is None
    assert src.read_text(str(secret)).startswith("db.password")
    (tmp_path / "b.conf").write_text("")
    (tmp_path / "a.conf").write_text("")
    assert [Path(p).name for p in src.glob(str(tmp_path / "*.conf"))] == ["a.conf", "b.conf"]
    assert src.exists(str(secret)) and not src.exists(str(tmp_path / "nope"))
    assert src.env == {}


# ------------------------------------------------------------------------------ sentinel


def test_sentinel_rung_states() -> None:
    local, metrics = security.sentinel_rung(config({"server": {"host": "localhost"}}), {}, NOW)
    assert (local.status, local.message, metrics["open"]) == ("ok", "listens on localhost only", False)
    v6, _ = security.sentinel_rung(config({"server": {"host": "::1"}}), {}, NOW)
    assert v6.message == "listens on ::1 only"
    opted, _ = security.sentinel_rung(
        config({"server": {"host": "0.0.0.0", "allow_unauthenticated": True}}), {}, NOW
    )
    assert opted.status == "warn"
    assert opted.message == "listens on 0.0.0.0 without a password (server.allow_unauthenticated)"
    assert opted.detail["hint"] == security.HINTS["sentinel_open"]
    assert security.is_loopback_host("127.0.0.2") and not security.is_loopback_host("10.0.0.5")


LOGIN = {"username": "admin", "password_hash": hash_password("correct horse battery")}


def test_sentinel_rung_knows_the_login() -> None:
    with_login, metrics = security.sentinel_rung(
        config({"server": {"host": "0.0.0.0", "login": LOGIN}}), {}, NOW
    )
    assert (with_login.status, with_login.message) == ("ok", "listens on 0.0.0.0 with a login")
    assert with_login.detail["hint"] == (
        "Put the nginx HTTPS recipe in front of Sentinel so the password never travels in plain text."
    )
    assert metrics == {"host": "0.0.0.0", "port": 9765, "auth": False, "login": True, "open": False}
    local, metrics = security.sentinel_rung(config({"server": {"login": LOGIN}}), {}, NOW)
    assert (local.status, local.message, metrics["login"]) == ("ok", "listens on 127.0.0.1 only", True)
    token, metrics = security.sentinel_rung(
        config({"server": {"host": "0.0.0.0", "auth_token": "abcdefghijklmnop"}}), {}, NOW
    )
    assert (token.message, metrics["login"], metrics["auth"]) == (
        "listens on 0.0.0.0 with a password",
        False,
        True,
    )
    assert "hint" not in token.detail
    # neither: the warning as before
    opted, metrics = security.sentinel_rung(
        config({"server": {"host": "0.0.0.0", "allow_unauthenticated": True}}),
        {"SENTINEL_ALLOW_OPEN": "1"},
        NOW,
    )
    assert (opted.status, opted.message) == (
        "warn",
        "listens on 0.0.0.0 without a password (SENTINEL_ALLOW_OPEN)",
    )
    assert metrics["open"] is True and metrics["login"] is False
    # the demo never asks for a login, so it never counts as one
    demo, metrics = security.sentinel_rung(
        apply_demo_overrides(config({"server": {"host": "0.0.0.0", "login": LOGIN}})), {}, NOW
    )
    assert demo.status == "warn" and metrics["login"] is False


# ---------------------------------------------------------------------------- socket table


Addr = namedtuple("Addr", "ip port")
Conn = namedtuple("Conn", "fd family type laddr raddr status pid")


def test_listening_sockets_adds_waiting_udp_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    table = {
        "tcp": [
            Conn(3, 2, 1, Addr("0.0.0.0", 22), (), psutil.CONN_LISTEN, 100),
            Conn(4, 10, 1, Addr("::", 22), (), psutil.CONN_LISTEN, 100),
            Conn(5, 2, 1, Addr("10.0.0.5", 51000), Addr("192.0.2.1", 443), psutil.CONN_ESTABLISHED, 100),
        ],
        "udp": [
            Conn(6, 2, 2, Addr("127.0.0.53", 53), (), psutil.CONN_NONE, None),
            Conn(7, 2, 2, Addr("10.0.0.5", 40000), Addr("192.0.2.53", 53), psutil.CONN_NONE, 200),
            Conn(8, 2, 2, Addr("0.0.0.0", 5353), (), psutil.CONN_NONE, 100),
        ],
    }

    class FakeProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def name(self) -> str:
            return {100: "sshd"}.get(self.pid, "other")

    monkeypatch.setattr(net.psutil, "net_connections", lambda kind="inet": list(table[kind]))
    monkeypatch.setattr(net.psutil, "Process", FakeProcess)
    tcp_only = net.listening_sockets()
    assert [(s.port, s.addr, s.proto) for s in tcp_only] == [(22, "0.0.0.0", "tcp"), (22, "::", "tcp")]
    both = net.listening_sockets(with_process=True, udp=True)
    assert [(s.port, s.addr, s.proto, s.public, s.process) for s in both] == [
        (22, "0.0.0.0", "tcp", True, "sshd"),
        (22, "::", "tcp", True, "sshd"),
        (53, "127.0.0.53", "udp", False, None),
        (5353, "0.0.0.0", "udp", True, "sshd"),
    ]

    def no_udp(kind: str = "inet") -> list[Conn]:
        if kind == "udp":
            raise psutil.AccessDenied()
        return list(table[kind])

    monkeypatch.setattr(net.psutil, "net_connections", no_udp)
    assert [s.proto for s in net.listening_sockets(udp=True)] == ["tcp", "tcp"]


def test_ipv4_mapped_addresses_count_as_their_ipv4_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """T20: Java reports Solr's stop port as [::ffff:127.0.0.1]:7983, which is loopback."""
    table = {
        "tcp": [
            Conn(3, 10, 1, Addr("::ffff:127.0.0.1", 7983), (), psutil.CONN_LISTEN, None),
            Conn(4, 2, 1, Addr("127.0.0.1", 7983), (), psutil.CONN_LISTEN, None),
            Conn(5, 10, 1, Addr("::ffff:10.0.0.5", 8983), (), psutil.CONN_LISTEN, None),
            Conn(6, 10, 1, Addr("::ffff:0.0.0.0", 8984), (), psutil.CONN_LISTEN, None),
        ],
        "udp": [],
    }
    monkeypatch.setattr(net.psutil, "net_connections", lambda kind="inet": list(table[kind]))
    assert [(s.port, s.addr, s.public) for s in net.listening_sockets()] == [
        (7983, "127.0.0.1", False),
        (7983, "::ffff:127.0.0.1", False),
        (8983, "::ffff:10.0.0.5", True),
        (8984, "::ffff:0.0.0.0", True),
    ]
    assert net.is_wildcard_address("::ffff:0.0.0.0") and not net.is_wildcard_address("::ffff:127.0.0.1")
    assert security.is_loopback_host("::ffff:127.0.0.1")
    entries = security.grade_ports(
        security.group_ports([real_sock(7983, "::ffff:127.0.0.1")]), Config(), set()
    )
    assert entries[0]["verdict"] == "local"
    # the Ports component names only the addresses other machines can reach
    from dspace_sentinel.collectors.ports import evaluate_port

    mapped = [real_sock(8983, "::ffff:127.0.0.1"), real_sock(8983, "::")]
    exposed, _ = evaluate_port(8983, "Solr", "local_only", mapped, NOW)
    assert (exposed.status, exposed.message) == ("warn", "exposed publicly (::)")
    stop, _ = evaluate_port(7983, "Solr stop", "local_only", [real_sock(7983, "::ffff:127.0.0.1")], NOW)
    assert (stop.status, stop.message) == ("ok", "listening on localhost only")


def test_host_sources_ask_for_tcp_and_udp(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[tuple[bool, bool]] = []

    def fake(with_process: bool = False, udp: bool = False) -> list[ListeningSocket]:
        asked.append((with_process, udp))
        return []

    monkeypatch.setattr(security, "listening_sockets", fake)
    assert security.HostSources().sockets() == [] and asked == [(True, True)]
    assert security.HostSources().env is os.environ
    monkeypatch.setattr(security.psutil, "net_if_addrs", lambda: {"lo": [], "tailscale0": []})
    assert security.HostSources().interfaces() == {"lo", "tailscale0"}
    assert security.HostSources().is_root() is (os.geteuid() == 0)
