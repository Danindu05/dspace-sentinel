"""Data models shared by collectors, store, API and CLI (SPEC section 4)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Status = Literal["ok", "warn", "crit", "unknown", "off"]
Severity = Literal["warning", "critical"]

STATUS_RANK: dict[str, int] = {"ok": 0, "unknown": 1, "warn": 2, "crit": 3}
"""Ordering used by ``worst()``; ``"off"`` never counts."""

STATUSES: tuple[str, ...] = ("ok", "warn", "crit", "unknown", "off")


def worst(statuses: Any) -> Status:
    """Return the worst status in ``statuses`` (per :data:`STATUS_RANK`); ``"off"`` is ignored.

    An empty input yields ``"unknown"``.
    """
    best: str | None = None
    for status in statuses:
        if status not in STATUS_RANK:
            continue
        if best is None or STATUS_RANK[status] > STATUS_RANK[best]:
            best = status
    return best or "unknown"  # type: ignore[return-value]


@dataclass
class CheckResult:
    """One rung of a component's health chain."""

    id: str
    label: str
    status: Status
    ts: float
    latency_ms: float | None = None
    value: float | None = None
    message: str = ""
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON-serialisable dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CheckResult:
        """Inverse of :meth:`to_dict` (tolerates missing optional keys)."""
        return cls(
            id=str(data["id"]),
            label=str(data.get("label", data["id"])),
            status=data.get("status", "unknown"),
            ts=float(data.get("ts", 0.0)),
            latency_ms=data.get("latency_ms"),
            value=data.get("value"),
            message=str(data.get("message", "") or ""),
            detail=dict(data.get("detail") or {}),
        )

    @classmethod
    def not_reached(cls, id: str, label: str, ts: float) -> CheckResult:
        """The rung after a failed rung: status ``unknown`` with message ``"not reached"``."""
        return cls(id=id, label=label, status="unknown", ts=ts, message="not reached")


@dataclass
class SystemSample:
    """One snapshot of the host, produced by the system collector every ``system_interval``."""

    ts: float
    cpu_pct: float
    cpu_per_core: list[float]
    mem_total: int
    mem_used: int
    mem_available: int
    mem_pct: float
    mem_cached: int
    swap_total: int
    swap_used: int
    swap_pct: float
    disk_total: int
    disk_used: int
    disk_free: int
    disk_pct: float
    disk_path: str
    inode_pct: float | None
    load1: float
    load5: float
    load15: float
    cores: int
    net_rx_bps: float
    net_tx_bps: float
    uptime_s: float
    os_name: str
    os_version: str
    kernel: str
    hostname: str
    disks: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON-serialisable dict."""
        return asdict(self)

    @property
    def load_per_core(self) -> float:
        """``load1`` divided by the number of cores (at least 1)."""
        return self.load1 / max(1, self.cores)


@dataclass
class ComponentHealth:
    """Rolled-up health of one component (a ladder in the UI)."""

    id: str
    label: str
    emoji: str
    status: Status
    reason: str | None
    chain: list[CheckResult]
    metrics: dict
    depends_on: list[str]
    latency_ms: float | None
    since: float | None
    checked_at: float | None

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON-serialisable dict (chain rungs expanded)."""
        return {
            "id": self.id,
            "label": self.label,
            "emoji": self.emoji,
            "status": self.status,
            "reason": self.reason,
            "chain": [c.to_dict() for c in self.chain],
            "metrics": dict(self.metrics),
            "depends_on": list(self.depends_on),
            "latency_ms": self.latency_ms,
            "since": self.since,
            "checked_at": self.checked_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ComponentHealth:
        """Inverse of :meth:`to_dict`."""
        return cls(
            id=str(data["id"]),
            label=str(data.get("label", data["id"])),
            emoji=str(data.get("emoji", "")),
            status=data.get("status", "unknown"),
            reason=data.get("reason"),
            chain=[CheckResult.from_dict(c) for c in data.get("chain") or []],
            metrics=dict(data.get("metrics") or {}),
            depends_on=list(data.get("depends_on") or []),
            latency_ms=data.get("latency_ms"),
            since=data.get("since"),
            checked_at=data.get("checked_at"),
        )

    def chain_summary(self) -> str:
        """Compact ladder text such as ``"service ✅ → tcp ✅ → connect ❌"``."""
        marks = {"ok": "✅", "warn": "⚠️", "crit": "❌", "unknown": "—", "off": "—"}
        parts = []
        for rung in self.chain:
            short = rung.id.split(".", 1)[1] if "." in rung.id else rung.id
            parts.append(f"{short} {marks.get(rung.status, '—')}")
        return " → ".join(parts)


@dataclass
class Alert:
    """A threshold or availability alert (row in the ``alerts`` table)."""

    id: int | None
    rule: str
    component: str
    severity: Severity
    status: Literal["open", "resolved"]
    title: str
    message: str
    value: float | None = None
    threshold: float | None = None
    opened_ts: float | None = None
    resolved_ts: float | None = None
    acked_ts: float | None = None
    incident_id: int | None = None
    notified_ts: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON-serialisable dict."""
        return asdict(self)

    @classmethod
    def from_row(cls, row: Any) -> Alert:
        """Build from a ``sqlite3.Row`` / mapping with the ``alerts`` columns."""
        return cls(
            id=row["id"],
            rule=row["rule"],
            component=row["component"],
            severity=row["severity"],
            status=row["status"],
            title=row["title"] or "",
            message=row["message"] or "",
            value=row["value"],
            threshold=row["threshold"],
            opened_ts=row["opened_ts"],
            resolved_ts=row["resolved_ts"],
            acked_ts=row["acked_ts"],
            incident_id=row["incident_id"],
            notified_ts=row["notified_ts"],
        )


@dataclass
class Incident:
    """A correlated group of alerts with a hypothesis (row in the ``incidents`` table)."""

    id: int | None
    title: str
    hypothesis: str
    status: Literal["open", "resolved"]
    started_ts: float
    ended_ts: float | None = None
    affected: list[str] = field(default_factory=list)
    sequence: list[dict] = field(default_factory=list)
    alert_ids: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON-serialisable dict."""
        return asdict(self)


@dataclass
class Report:
    """A generated report file (row in the ``reports`` table)."""

    id: int | None
    created_ts: float
    title: str
    range_from: float
    range_to: float
    fmt: Literal["html", "md", "json"]
    path: str
    size: int
    deleted_ts: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON-serialisable dict."""
        return asdict(self)

    @classmethod
    def from_row(cls, row: Any) -> Report:
        """Build from a ``sqlite3.Row`` / mapping with the ``reports`` columns."""
        return cls(
            id=row["id"],
            created_ts=row["created_ts"],
            title=row["title"] or "",
            range_from=row["range_from"],
            range_to=row["range_to"],
            fmt=row["fmt"],
            path=row["path"],
            size=row["size"] or 0,
            deleted_ts=row["deleted_ts"],
        )


@dataclass
class Event:
    """An audit-trail row in the ``events`` table (status changes, alerts, config edits, errors)."""

    id: int | None
    ts: float
    kind: str
    component: str | None
    message: str
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON-serialisable dict."""
        return asdict(self)


__all__ = [
    "STATUSES",
    "STATUS_RANK",
    "Alert",
    "CheckResult",
    "ComponentHealth",
    "Event",
    "Incident",
    "Report",
    "Severity",
    "Status",
    "SystemSample",
    "worst",
]
