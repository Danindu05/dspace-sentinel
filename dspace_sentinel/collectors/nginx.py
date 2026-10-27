"""nginx collector: service → HTTP → HTTPS (SPEC section 6).

The HTTP/HTTPS rungs do **not** follow redirects: a ``301 → https://…`` from nginx proves the
web server answers, which is what this component is about. 5xx means the upstream behind
nginx is broken (``crit``); 4xx means nginx answers but serves nothing useful (``warn``),
except 401/403 which just mean the site is access-restricted (``ok``).

When systemd knows no such unit *and* nothing answers on the HTTP address, there is most likely
no web server on the machine at all (DSpace opened directly on :4000): the refused HTTP rung stays
critical and carries ``detail.hint`` (:data:`NO_WEB_SERVER_HINT`) saying how to switch the check
off or point it at the real web server.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from dspace_sentinel.collectors.base import Collector, CollectorContext, CollectorResult
from dspace_sentinel.collectors.services import Ladder, http_message, rung, systemd_rung, url_parts
from dspace_sentinel.models import CheckResult, Status
from dspace_sentinel.util.httpc import HttpProbe, probe

log = logging.getLogger(__name__)

NO_WEB_SERVER_HINT = (
    "No web server was found on this machine: there is no {unit} unit and nothing answers on {where}. "
    "If DSpace is opened directly on its own port (for example :4000), switch this check off in "
    "More › Modules. If it runs behind Apache or another unit, set its name and address in "
    "More › DSpace connection."
)
"""``detail.hint`` of the HTTP rung when the unit is not found and the connection is refused."""


def classify(p: HttpProbe) -> tuple[Status, str]:
    """``(status, message)`` for a web-server probe made without following redirects."""
    code = p.status_code
    if code is None:
        return "crit", http_message(p)
    if 300 <= code < 400:
        location = p.headers.get("location", "")
        return "ok", f"HTTP {code} → {location}"[:160] if location else f"HTTP {code}"
    if 200 <= code < 300:
        return "ok", ""
    if code in (401, 403):
        return "ok", f"HTTP {code} (access restricted)"
    if 400 <= code < 500:
        return "warn", f"HTTP {code}"
    return "crit", f"HTTP {code}"


def no_web_server_hint(service: CheckResult | None, message: str, url: str) -> str | None:
    """:data:`NO_WEB_SERVER_HINT` when systemd knows no such unit and ``url`` refused the connection.

    ``service`` is the systemd rung (``None`` when no unit is configured), ``message`` the HTTP
    rung's message. A machine without ``systemctl`` says nothing about the unit: no hint.
    """
    if service is None or service.detail.get("state") != "missing":
        return None
    unit = service.detail.get("unit")
    if not unit or service.message != f"unit {unit} not found":
        return None
    if not message.startswith("connection refused"):
        return None
    return NO_WEB_SERVER_HINT.format(unit=unit, where=url_parts(url).hostport)


class NginxCollector(Collector):
    """``nginx`` 🌐: service → HTTP → HTTPS (only when ``nginx.https_url`` is set)."""

    id: ClassVar[str] = "nginx"
    interval_key: ClassVar[str] = "http_interval"
    label: ClassVar[str] = "nginx"
    emoji: ClassVar[str] = "🌐"
    budget_s: ClassVar[float] = 18.0  # systemd 3 + HTTP 5 + HTTPS 5 + slack
    depends_on: ClassVar[list[str]] = ["frontend"]

    async def collect(self, ctx: CollectorContext) -> CollectorResult:
        """Probe ``http_url`` and, when configured, ``https_url``; never raises."""
        n = ctx.cfg.nginx
        ts = ctx.now
        http_url = n.http_url.strip()
        https_url = n.https_url.strip()
        http_rung = ("nginx.http", "HTTP")
        https_rung = ("nginx.https", "HTTPS")
        ladder = Ladder(ts)
        metrics: dict[str, Any] = {
            "http_latency_ms": None,
            "https_latency_ms": None,
            "server_header": None,
            "http_url": http_url,
            "https_url": https_url or None,
        }

        service = ladder.add(await systemd_rung("nginx.service", n.service, ts))
        if ladder.blocked:
            ladder.skip(*http_rung)
            if https_url:
                ladder.skip(*https_rung)
            return CollectorResult(checks=ladder.checks, metrics=metrics)

        p = await probe(ctx.http, http_url, follow_redirects=False)
        status, message = classify(p)
        server = p.headers.get("server")
        if server:
            metrics["server_header"] = server
        ladder.add(
            rung(
                http_rung[0],
                http_rung[1],
                status,
                ts,
                latency_ms=p.latency_ms,
                message=message,
                url=http_url,
                status_code=p.status_code,
                server=server,
                hint=no_web_server_hint(service, message, http_url) if status == "crit" else None,
            )
        )
        if p.status_code is not None:
            metrics["http_latency_ms"] = p.latency_ms

        if https_url:
            if ladder.blocked:
                ladder.skip(*https_rung)
            else:
                ps = await probe(ctx.http, https_url, follow_redirects=False)
                status, message = classify(ps)
                server_s = ps.headers.get("server")
                if server_s and not metrics["server_header"]:
                    metrics["server_header"] = server_s
                ladder.add(
                    rung(
                        https_rung[0],
                        https_rung[1],
                        status,
                        ts,
                        latency_ms=ps.latency_ms,
                        message=message,
                        url=https_url,
                        status_code=ps.status_code,
                        server=server_s,
                    )
                )
                if ps.status_code is not None:
                    metrics["https_latency_ms"] = ps.latency_ms
        return CollectorResult(checks=ladder.checks, metrics=metrics)


__all__ = ["NO_WEB_SERVER_HINT", "NginxCollector", "classify", "no_web_server_hint"]
