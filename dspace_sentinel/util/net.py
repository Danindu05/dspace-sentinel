"""TCP reachability and listening-socket inspection."""

from __future__ import annotations

import asyncio
import ipaddress
import time
from dataclasses import asdict, dataclass
from typing import Any

import psutil

DEFAULT_TCP_TIMEOUT = 2.0


@dataclass
class TcpResult:
    """Outcome of :func:`tcp_connect`."""

    ok: bool
    latency_ms: float | None
    error: str | None
    host: str
    port: int

    def to_dict(self) -> dict[str, Any]:
        """Plain dict form."""
        return asdict(self)


async def tcp_connect(host: str, port: int, timeout: float = DEFAULT_TCP_TIMEOUT) -> TcpResult:
    """Open and immediately close a TCP connection, measuring the handshake time.

    Never raises: failures come back as ``ok=False`` with a short human ``error``
    such as ``"connection refused (localhost:5432)"`` or ``"timeout after 2.0 s"``.
    """
    started = time.perf_counter()
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
    except asyncio.TimeoutError:
        return TcpResult(False, None, f"timeout after {timeout:g} s ({host}:{port})", host, port)
    except ConnectionRefusedError:
        return TcpResult(False, None, f"connection refused ({host}:{port})", host, port)
    except OSError as exc:
        reason = exc.strerror or str(exc) or exc.__class__.__name__
        return TcpResult(False, None, f"{reason.lower()} ({host}:{port})", host, port)
    except Exception as exc:  # pragma: no cover - defensive
        return TcpResult(False, None, f"{exc.__class__.__name__}: {exc} ({host}:{port})", host, port)
    latency = (time.perf_counter() - started) * 1000.0
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
    except Exception:
        pass
    return TcpResult(True, round(latency, 2), None, host, port)


WILDCARD_ADDRESSES: frozenset[str] = frozenset({"", "*", "0.0.0.0", "::", "::0", "::ffff:0.0.0.0"})
"""Bind addresses that mean "every interface"."""


def ip_of(addr: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The IP address in ``addr`` (a ``%zone`` and ``[]`` removed), ``None`` when it is not one.

    An IPv4-mapped IPv6 address (``::ffff:127.0.0.1``, as Java reports an IPv4 socket on a dual
    stack) is given back as the IPv4 address it maps, so it is judged like that address.
    """
    try:
        ip = ipaddress.ip_address(addr.strip().strip("[]").split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def is_wildcard_address(addr: str) -> bool:
    """True for a bind address meaning every interface (``0.0.0.0``, ``::``, ``*``, ``::ffff:0.0.0.0``)."""
    if addr.strip() in WILDCARD_ADDRESSES:
        return True
    ip = ip_of(addr)
    return ip is not None and ip.is_unspecified


def is_public_address(addr: str) -> bool:
    """True when ``addr`` is a wildcard or non-loopback address (reachable from outside).

    ``::ffff:127.0.0.1`` (IPv4-mapped loopback) is loopback, so it is not public.
    """
    if is_wildcard_address(addr):
        return True
    ip = ip_of(addr)
    if ip is None:
        return True
    return not ip.is_loopback


@dataclass
class ListeningSocket:
    """A listening socket: TCP in LISTEN state, or (``proto="udp"``) a bound, unconnected UDP socket."""

    port: int
    addr: str
    public: bool
    family: str
    pid: int | None = None
    process: str | None = None
    proto: str = "tcp"

    def to_dict(self) -> dict[str, Any]:
        """Plain dict form."""
        return asdict(self)


def _connections(kind: str) -> list[Any] | None:
    """``psutil.net_connections(kind)``, or ``None`` when the table is not accessible."""
    try:
        return list(psutil.net_connections(kind=kind))
    except (psutil.AccessDenied, PermissionError, OSError):
        if kind != "tcp":
            return None
        try:
            return list(psutil.net_connections(kind="inet"))  # UDP rows are never in LISTEN state
        except Exception:
            return None
    except Exception:
        return None


def listening_sockets(with_process: bool = False, udp: bool = False) -> list[ListeningSocket]:
    """Every TCP socket in LISTEN state, classified ``public`` (0.0.0.0 / :: / non-loopback) or local.

    ``udp=True`` adds the UDP sockets that wait for datagrams (bound, no remote address), with
    ``proto="udp"``. Process names (``with_process``) are known for Sentinel's own user's sockets;
    other users' sockets carry ``pid``/``process`` ``None`` unless Sentinel runs as root.

    Sorted by port (TCP before UDP); duplicates per (port, addr, proto) are collapsed. Returns
    ``[]`` when the information is not accessible instead of raising.
    """
    tcp = _connections("tcp")
    if tcp is None:
        return []
    udp_conns = (_connections("udp") or []) if udp else []
    names: dict[int, str | None] = {}
    seen: dict[tuple[int, str, str], ListeningSocket] = {}
    for proto, conns in (("tcp", tcp), ("udp", udp_conns)):
        for conn in conns:
            if not conn.laddr:
                continue
            if proto == "tcp" and conn.status != psutil.CONN_LISTEN:
                continue
            if proto == "udp" and conn.raddr:
                continue  # a connected UDP socket is a client talking to one peer
            addr = conn.laddr.ip
            port = int(conn.laddr.port)
            key = (port, addr, proto)
            if key in seen:
                continue
            family = "ipv6" if ":" in addr else "ipv4"
            name: str | None = None
            if with_process and conn.pid:
                if conn.pid not in names:
                    try:
                        names[conn.pid] = psutil.Process(conn.pid).name()
                    except Exception:
                        names[conn.pid] = None
                name = names[conn.pid]
            seen[key] = ListeningSocket(
                port=port,
                addr=addr,
                public=is_public_address(addr),
                family=family,
                pid=conn.pid,
                process=name,
                proto=proto,
            )
    return sorted(seen.values(), key=lambda s: (s.port, s.proto, s.addr))


def port_state(port: int, sockets: list[ListeningSocket] | None = None) -> tuple[bool, bool, list[str]]:
    """``(listening, public, addrs)`` for one port given a socket list (fetched when ``None``)."""
    sockets = listening_sockets() if sockets is None else sockets
    matching = [s for s in sockets if s.port == port]
    return bool(matching), any(s.public for s in matching), [s.addr for s in matching]


__all__ = [
    "DEFAULT_TCP_TIMEOUT",
    "WILDCARD_ADDRESSES",
    "ListeningSocket",
    "TcpResult",
    "ip_of",
    "is_public_address",
    "is_wildcard_address",
    "listening_sockets",
    "port_state",
    "tcp_connect",
]
