"""Shared rung helpers for the stack collectors: systemd, TCP, HTTP messages and the ladder.

Every DSpace collector builds a *ladder* of rungs (SPEC section 6). The helpers here keep
the semantics identical across components:

* a systemd rung is skipped entirely when the unit name is empty (``""`` in the config); a
  unit that systemd does not know, or a host without ``systemctl``, gives an *informational*
  ``unknown`` rung (``detail.informational``) that does not count towards the component;
* a rung after a **failed** (``crit``) rung is *not reached* (``unknown`` / "not reached");
  ``warn`` and ``unknown`` rungs do not block the rest of the ladder, and neither does a rung
  that answered correctly but slowly (``detail.slow``: its status is only a latency grade);
* human messages look like ``"connection refused (localhost:5432)"``, ``"HTTP 502"`` or
  ``"timed out after 5 s"``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from dspace_sentinel.models import CheckResult, Status
from dspace_sentinel.util import systemd
from dspace_sentinel.util.httpc import DEFAULT_TIMEOUT, HttpProbe
from dspace_sentinel.util.net import DEFAULT_TCP_TIMEOUT, tcp_connect

UNIT_STATE_STATUS: dict[str, tuple[Status, str]] = {
    "active": ("ok", "active"),
    "inactive": ("crit", "inactive"),
    "failed": ("crit", "failed"),
    "missing": ("unknown", "unit not found"),
    "unknown": ("unknown", "systemctl did not answer"),
}
"""systemd unit state → (rung status, message)."""


def rung(
    rung_id: str,
    label: str,
    status: Status,
    ts: float,
    *,
    latency_ms: float | None = None,
    value: float | None = None,
    message: str = "",
    **detail: Any,
) -> CheckResult:
    """Build a :class:`CheckResult` with keyword ``detail`` entries (``None`` values dropped)."""
    return CheckResult(
        id=rung_id,
        label=label,
        status=status,
        ts=ts,
        latency_ms=latency_ms,
        value=value,
        message=message,
        detail={k: v for k, v in detail.items() if v is not None},
    )


@dataclass
class Ladder:
    """The rungs of one component in ladder order, with the *not reached* rule built in."""

    ts: float
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        """True once a rung failed (``crit`` and not merely slow): everything below it is not reached."""
        return self.failed is not None

    @property
    def failed(self) -> CheckResult | None:
        """The first failed rung (a slow but correct answer is not a failure), or ``None``."""
        return next((c for c in self.checks if c.status == "crit" and not c.detail.get("slow")), None)

    def add(self, check: CheckResult | None) -> CheckResult | None:
        """Append a rung (``None`` = a skipped optional rung such as an empty systemd unit)."""
        if check is not None:
            self.checks.append(check)
        return check

    def skip(self, rung_id: str, label: str) -> CheckResult:
        """Append a *not reached* rung."""
        check = CheckResult.not_reached(rung_id, label, self.ts)
        self.checks.append(check)
        return check

    def skip_all(self, rungs: list[tuple[str, str]]) -> None:
        """Append *not reached* rungs for every ``(id, label)`` pair."""
        for rung_id, label in rungs:
            self.skip(rung_id, label)


# --------------------------------------------------------------------------- systemd


async def systemd_rung(
    rung_id: str,
    unit: str,
    ts: float,
    *,
    label: str | None = None,
    timeout: float = systemd.SUBPROCESS_TIMEOUT,
) -> CheckResult | None:
    """The ``<component>.service`` rung for a systemd unit; ``None`` when ``unit`` is empty.

    ``active`` → ok; ``inactive``/``failed`` → crit; an unknown unit or a machine without
    systemd → informational ``unknown`` (the rest of the ladder still runs and decides the
    component's status); ``systemctl`` not answering → a counted ``unknown``.
    """
    unit = (unit or "").strip()
    if not unit:
        return None
    label = label or f"systemd {unit}"
    if systemd.systemctl_path() is None:
        return rung(
            rung_id,
            label,
            "unknown",
            ts,
            message="systemctl not available",
            unit=unit,
            state="missing",
            informational=True,
        )
    state = await systemd.is_active(unit, timeout=timeout)
    status, message = UNIT_STATE_STATUS.get(state, ("unknown", state))
    if state == "missing":
        return rung(
            rung_id,
            label,
            "unknown",
            ts,
            message=f"unit {unit} not found",
            unit=unit,
            state=state,
            informational=True,
        )
    return rung(rung_id, label, status, ts, message=message, unit=unit, state=state)


# ------------------------------------------------------------------------------- TCP


async def tcp_rung(
    rung_id: str,
    host: str,
    port: int,
    ts: float,
    *,
    label: str | None = None,
    timeout: float = DEFAULT_TCP_TIMEOUT,
) -> CheckResult:
    """The ``<component>.tcp`` rung: a timed TCP handshake (2 s timeout)."""
    label = label or f"TCP :{port}"
    result = await tcp_connect(host, port, timeout=timeout)
    if result.ok:
        return rung(rung_id, label, "ok", ts, latency_ms=result.latency_ms, host=host, port=port)
    error = result.error or f"unreachable ({host}:{port})"
    if error.startswith("timeout after"):
        error = f"timed out after {timeout:g} s ({host}:{port})"
    return rung(rung_id, label, "crit", ts, message=error, host=host, port=port)


# ------------------------------------------------------------------------------ URLs


@dataclass(frozen=True)
class UrlParts:
    """Host, port, scheme and path of a URL with the scheme's default port filled in."""

    scheme: str
    host: str
    port: int
    path: str

    @property
    def hostport(self) -> str:
        return f"{self.host}:{self.port}"


def url_parts(url: str) -> UrlParts:
    """Split ``url`` into :class:`UrlParts` (``http`` → 80, ``https`` → 443 when no port is given)."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return UrlParts("http", url, 80, "")
    scheme = (parts.scheme or "http").lower()
    host = parts.hostname or "localhost"
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is None:
        port = 443 if scheme == "https" else 80
    return UrlParts(scheme, host, int(port), parts.path.rstrip("/"))


_REFUSED_PHRASES = frozenset(
    {"all connection attempts failed", "connection refused", "[errno 111] connection refused"}
)
"""httpx wordings for "nothing listens there" (dual-stack ``localhost`` yields the first one)."""


def http_message(probe: HttpProbe, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Short human reason for a failed :class:`HttpProbe` (``""`` when it succeeded).

    ``"HTTP 502"``, ``"timed out after 5 s (localhost:8080)"``, ``"connection refused (localhost:8080)"``.
    """
    if probe.ok:
        return ""
    if probe.status_code is not None:
        return f"HTTP {probe.status_code}"
    error = probe.error or "request failed"
    reason = error.rsplit(" (", 1)[0] if error.endswith(")") and " (" in error else error
    where = url_parts(probe.url).hostport
    if reason == "timeout":
        return f"timed out after {timeout:g} s ({where})"
    if reason.lower() in _REFUSED_PHRASES:
        reason = "connection refused"
    return f"{reason} ({where})"


def slow_message(what: str, latency_ms: float) -> str:
    """``"search took 4.8 s"`` / ``"homepage took 640 ms"`` for latency threshold breaches."""
    if latency_ms >= 1000:
        return f"{what} took {latency_ms / 1000:.1f} s"
    return f"{what} took {latency_ms:.0f} ms"


__all__ = [
    "UNIT_STATE_STATUS",
    "Ladder",
    "UrlParts",
    "http_message",
    "rung",
    "slow_message",
    "systemd_rung",
    "tcp_rung",
    "url_parts",
]
