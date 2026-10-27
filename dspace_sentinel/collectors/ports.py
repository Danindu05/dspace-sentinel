"""Ports collector: are the required ports open, and do the private ones stay local (SPEC section 6).

One rung per configured :class:`~dspace_sentinel.config.PortRule`:

* ``expect: open`` — listening ⇒ ok, closed ⇒ warn;
* ``expect: local_only`` — listening on ``0.0.0.0``/``::``/a public address ⇒ warn
  "exposed publicly", listening on loopback ⇒ ok, closed ⇒ unknown.

An exposed ``local_only`` port also carries ``detail.hint`` (:func:`exposed_hint`): why it matters
for Solr, PostgreSQL or any other service, how to close it, and how to accept it.

The socket table comes from ``util.net.listening_sockets`` (psutil). When that table is
not readable at all, each port is probed with a TCP connect to ``127.0.0.1`` instead, which
tells open/closed but not the bind address.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, ClassVar

from dspace_sentinel.collectors.base import Collector, CollectorContext, CollectorResult
from dspace_sentinel.collectors.services import rung, url_parts
from dspace_sentinel.util.net import (
    ListeningSocket,
    is_public_address,
    listening_sockets,
    port_state,
    tcp_connect,
)

if TYPE_CHECKING:  # pragma: no cover
    from dspace_sentinel.config import Config

log = logging.getLogger(__name__)

_ACCEPT_IT = (
    'To accept it, change this rule to "open" in config/sentinel.yaml (ports: port {port}, '
    "expect: open) and restart Sentinel."
)
EXPOSED_HINTS: dict[str, str] = {
    "solr": (
        "Solr has no login: anyone who can reach this port can read and change the search index. "
        "Make Solr listen on localhost only, or block the port in the firewall (Sentinel sees where "
        "it listens, not your firewall). " + _ACCEPT_IT
    ),
    "postgres": (
        "PostgreSQL holds DSpace's database: anyone who can reach this port can try to log in to it. "
        "Make PostgreSQL listen on localhost only (listen_addresses in postgresql.conf), or block the "
        "port in the firewall (Sentinel sees where it listens, not your firewall). " + _ACCEPT_IT
    ),
    "other": (
        "This port should only be reachable from this machine: anyone who can reach it can connect "
        "to the service behind it. Make that service listen on localhost only, or block the port in "
        "the firewall (Sentinel sees where it listens, not your firewall). " + _ACCEPT_IT
    ),
}
"""``detail.hint`` of an exposed ``local_only`` port, by service (``{port}`` is filled in)."""


def exposed_service(port: int, label: str, cfg: Config | None = None) -> str:
    """``"solr"``, ``"postgres"`` or ``"other"``: what a port rule guards, by its label, else its port."""
    name = (label or "").lower()
    if "solr" in name:
        return "solr"
    if "postgres" in name:
        return "postgres"
    solr_port = url_parts(cfg.solr.url).port if cfg is not None else 8983
    pg_port = cfg.postgres.port if cfg is not None else 5432
    if port == solr_port:
        return "solr"
    if port == pg_port:
        return "postgres"
    return "other"


def exposed_hint(port: int, label: str, cfg: Config | None = None) -> str:
    """What to do about a ``local_only`` port that other machines can reach (``detail.hint``)."""
    return EXPOSED_HINTS[exposed_service(port, label, cfg)].format(port=port)


def evaluate_port(
    port: int,
    label: str,
    expect: str,
    sockets: list[ListeningSocket],
    ts: float,
    cfg: Config | None = None,
) -> tuple[Any, list[dict[str, Any]]]:
    """Grade one port rule against the socket table → ``(CheckResult, listening entries)``.

    ``cfg`` tells the Solr and PostgreSQL ports apart for the hint of an exposed port.
    """
    listening, public, addrs = port_state(port, sockets)
    entries = [
        {"port": s.port, "addr": s.addr, "public": s.public, "process": s.process}
        for s in sockets
        if s.port == port
    ]
    rung_id = f"ports.{port}"
    rung_label = f"{label or 'Port'} :{port}"
    detail: dict[str, Any] = {"expect": expect, "addrs": addrs or None}
    if not listening:
        status = "warn" if expect == "open" else "unknown"
        return rung(rung_id, rung_label, status, ts, message="not listening", **detail), entries
    if expect == "local_only" and public:
        exposed = ", ".join(a for a in addrs if is_public_address(a)) or addrs[0]
        message = f"exposed publicly ({exposed})"
        hint = exposed_hint(port, label, cfg)
        return rung(rung_id, rung_label, "warn", ts, message=message, **detail, hint=hint), entries
    message = "listening publicly" if public else "listening on localhost only"
    return rung(rung_id, rung_label, "ok", ts, message=message, **detail), entries


class PortsCollector(Collector):
    """``ports`` 🔌: one rung per configured port rule; metrics list what is listening."""

    id: ClassVar[str] = "ports"
    interval_key: ClassVar[str] = "service_interval"
    label: ClassVar[str] = "Ports"
    emoji: ClassVar[str] = "🔌"

    async def collect(self, ctx: CollectorContext) -> CollectorResult:
        """Read the socket table once (in a thread) and grade every rule against it."""
        ts = ctx.now
        rules = list(ctx.cfg.ports)
        try:
            sockets = await asyncio.to_thread(listening_sockets, True)
        except Exception as exc:
            log.debug("listening_sockets failed: %s", exc)
            sockets = []
        checks = []
        listening: list[dict[str, Any]] = []
        if sockets:
            for rule in rules:
                check, entries = evaluate_port(rule.port, rule.label, rule.expect, sockets, ts, ctx.cfg)
                checks.append(check)
                listening.extend(entries)
            wanted = {r.port for r in rules}
            other_public = sorted({s.port for s in sockets if s.public and s.port not in wanted})
            metrics: dict[str, Any] = {
                "listening": listening,
                "other_public": other_public,
                "source": "sockets",
            }
            return CollectorResult(checks=checks, metrics=metrics)

        # Fallback: no socket table (restricted container / hardened kernel) → probe loopback.
        for rule in rules:
            rung_id = f"ports.{rule.port}"
            rung_label = f"{rule.label or 'Port'} :{rule.port}"
            result = await tcp_connect("127.0.0.1", rule.port, timeout=1.0)
            if result.ok:
                listening.append({"port": rule.port, "addr": "127.0.0.1", "public": None})
                checks.append(
                    rung(
                        rung_id,
                        rung_label,
                        "ok",
                        ts,
                        latency_ms=result.latency_ms,
                        message="reachable on localhost (bind address unknown)",
                        expect=rule.expect,
                        source="tcp",
                    )
                )
            else:
                status = "warn" if rule.expect == "open" else "unknown"
                checks.append(
                    rung(
                        rung_id,
                        rung_label,
                        status,
                        ts,
                        message="not listening",
                        expect=rule.expect,
                        source="tcp",
                    )
                )
        return CollectorResult(
            checks=checks, metrics={"listening": listening, "other_public": [], "source": "tcp"}
        )


__all__ = ["EXPOSED_HINTS", "PortsCollector", "evaluate_port", "exposed_hint", "exposed_service"]
