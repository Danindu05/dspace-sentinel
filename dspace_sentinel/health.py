"""Health roll-up, dependency graph, mood and headline text (SPEC section 6)."""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from dspace_sentinel.models import (
    STATUS_RANK,
    Alert,
    CheckResult,
    ComponentHealth,
    Status,
    SystemSample,
    worst,
)

if TYPE_CHECKING:  # pragma: no cover
    from dspace_sentinel.config import Config
    from dspace_sentinel.state import State


@dataclass(frozen=True)
class ComponentMeta:
    """Static description of a component: label, emoji and what it depends on."""

    id: str
    label: str
    emoji: str
    depends_on: tuple[str, ...] = ()
    headline_check: str | None = None
    virtual: bool = False


COMPONENTS: dict[str, ComponentMeta] = {
    "server": ComponentMeta("server", "Server", "💻"),
    "postgres": ComponentMeta("postgres", "PostgreSQL", "🐘", headline_check="postgres.query"),
    "solr": ComponentMeta("solr", "Solr", "☀️", headline_check="solr.search"),
    "backend": ComponentMeta("backend", "DSpace Backend", "☕", ("postgres", "solr"), "backend.rest"),
    "frontend": ComponentMeta("frontend", "DSpace Frontend", "🖥️", ("backend",), "frontend.home"),
    "nginx": ComponentMeta("nginx", "nginx", "🌐", ("frontend",), "nginx.http"),
    "ssl": ComponentMeta("ssl", "SSL certificate", "🔒"),
    "ports": ComponentMeta("ports", "Ports", "🔌"),
    "security": ComponentMeta("security", "Security", "\U0001f6e1️"),
    "logs": ComponentMeta("logs", "Logs", "📜"),
    "repository": ComponentMeta("repository", "Repository", "📚"),
    "assetstore": ComponentMeta("assetstore", "Assetstore", "🗄️"),
    # virtual capabilities (never collected, used for the affected closure)
    "search": ComponentMeta("search", "DSpace Search", "🔎", ("backend", "solr"), virtual=True),
    "repository-site": ComponentMeta(
        "repository-site", "Repository site", "🏛️", ("nginx", "frontend"), virtual=True
    ),
}
"""Every known component in display order, plus the virtual capabilities."""

COMPONENT_ORDER: tuple[str, ...] = (
    "server",
    "postgres",
    "solr",
    "backend",
    "frontend",
    "nginx",
    "ssl",
    "ports",
    "security",
    "logs",
    "repository",
    "assetstore",
)

HEADLINE_LATENCY: dict[str, str] = {
    cid: meta.headline_check for cid, meta in COMPONENTS.items() if meta.headline_check
}
"""Component id → the rung whose latency is the component's headline latency."""

DEPENDENCY_GRAPH: dict[str, tuple[str, ...]] = {cid: meta.depends_on for cid, meta in COMPONENTS.items()}
"""Component id → the components it depends on (``backend → (postgres, solr)`` …)."""

ANY_OF_DEPENDENCIES: dict[str, tuple[str, ...]] = {"repository-site": ("nginx", "frontend")}
"""Virtual capabilities whose dependency is satisfied by *any one* of the listed components."""

STATUS_MARK: dict[str, str] = {"ok": "✅", "warn": "⚠️", "crit": "❌", "unknown": "—", "off": "—"}

STATUS_LABEL: dict[str, str] = {
    "ok": "Healthy",
    "warn": "Warning",
    "crit": "Critical",
    "unknown": "Unknown",
    "off": "Off",
}
"""Status id → the word the UI shows for it; generated texts use the same words."""

DEPENDENCY_STATE: dict[str, str] = {
    "ok": "is healthy",
    "warn": "is degraded (Warning)",
    "crit": "is down (Critical)",
    "unknown": "could not be checked (Unknown)",
    "off": "is switched off",
}
"""How a dependency rung describes the component it mirrors."""

SLOW_STATE = "is slow (Critical)"
"""A critical dependency whose every critical rung answered correctly, only too slowly (``detail.slow``)."""

_CONNECTIVITY_RUNGS = ("service", "tcp", "connect", "admin", "rest", "home", "http", "https", "cert", "path")


def meta_for(component_id: str) -> ComponentMeta:
    """Return the static metadata for ``component_id`` (a generic entry for unknown ids)."""
    meta = COMPONENTS.get(component_id)
    if meta is None:
        return ComponentMeta(component_id, component_id.replace("-", " ").title(), "🧩")
    return meta


def dependents_of(component_id: str) -> list[str]:
    """Components (including virtual capabilities) that directly depend on ``component_id``."""
    return [cid for cid, deps in DEPENDENCY_GRAPH.items() if component_id in deps]


def affected_closure(component_id: str, enabled: Iterable[str] | None = None) -> list[str]:
    """Human labels of everything that transitively depends on ``component_id``.

    Breadth-first, nearest first, e.g. ``solr → ["DSpace Search", "DSpace Backend", ...]``.
    When ``enabled`` (component ids) is given, real components not in it are skipped and a
    virtual capability is listed only when at least one of its dependencies is enabled.
    """
    enabled_set = set(enabled) if enabled is not None else None
    seen: set[str] = {component_id}
    order: list[str] = []
    frontier = [component_id]
    while frontier:
        level: list[str] = []
        nxt: list[str] = []
        for cid in frontier:
            for dep in dependents_of(cid):
                if dep in seen:
                    continue
                seen.add(dep)
                nxt.append(dep)  # always traverse so transitive dependents are found
                meta = COMPONENTS[dep]
                if enabled_set is not None:
                    if meta.virtual:
                        if not any(d in enabled_set for d in meta.depends_on):
                            continue
                    elif dep not in enabled_set:
                        continue
                level.append(dep)
        # within one level list virtual capabilities ("DSpace Search") before real components
        level.sort(key=lambda cid: not COMPONENTS[cid].virtual)
        order.extend(level)
        frontier = nxt
    return [COMPONENTS[cid].label for cid in order]


def component_label(component_id: str) -> str:
    """Human label for a component id."""
    return meta_for(component_id).label


def status_label(status: str | None) -> str:
    """``warn`` → ``Warning``: the UI's word for a status id."""
    return STATUS_LABEL.get(status or "unknown", str(status))


# --------------------------------------------------------------------------- roll-up


def is_informational(rung: CheckResult) -> bool:
    """True for an ``unknown`` rung that only says *this endpoint is not available here*.

    Collectors mark such rungs with ``detail["informational"] = True`` (an actuator that answers
    401/403/404, a systemd unit that does not exist, a log file that is not there, a dependency
    that has not been collected yet). They stay in the ladder ("—  not available") but never
    make the component, the mood or ``doctor`` read ``unknown``.
    """
    return rung.status == "unknown" and bool(rung.detail.get("informational"))


def rollup(
    component_id: str,
    checks: list[CheckResult],
    metrics: dict[str, Any] | None = None,
    previous: ComponentHealth | None = None,
    now: float | None = None,
    label: str | None = None,
    emoji: str | None = None,
) -> ComponentHealth:
    """Combine a component's rungs into one :class:`ComponentHealth`.

    * status = worst rung (``unknown`` only beats ``ok``); no rungs ⇒ ``ok`` when metrics were
      produced (metrics-only components such as ``repository``), else ``unknown``.
      Informational rungs (see :func:`is_informational`) are left out: a backend without an
      actuator endpoint is still ``ok``. A chain made only of them is ``unknown``.
    * reason = message of the first counted rung whose status ≠ ok (label when the message is empty).
    * latency = the headline rung's latency, else the first ok rung with a latency.
    * since = kept from ``previous`` while the status is unchanged.
    """
    now = now or time.time()
    meta = meta_for(component_id)
    counted = [c for c in checks if not is_informational(c)]
    if counted:
        status: Status = worst(c.status for c in counted)
    elif checks:
        status = "unknown"
    else:
        status = "ok" if metrics else "unknown"
    reason: str | None = None
    for rung in counted:
        if rung.status != "ok":
            reason = rung.message or f"{rung.label}: {rung.status}"
            break
    latency: float | None = None
    headline = HEADLINE_LATENCY.get(component_id)
    if headline:
        for rung in checks:
            if rung.id == headline and rung.latency_ms is not None:
                latency = rung.latency_ms
                break
    if latency is None:
        for rung in checks:
            if rung.status == "ok" and rung.latency_ms is not None:
                latency = rung.latency_ms
                break
    since = now
    if previous is not None and previous.status == status and previous.since is not None:
        since = previous.since
    checked_at = max((c.ts for c in checks), default=now)
    return ComponentHealth(
        id=component_id,
        label=label or meta.label,
        emoji=emoji or meta.emoji,
        status=status,
        reason=reason,
        chain=list(checks),
        metrics=dict(metrics or {}),
        depends_on=list(meta.depends_on),
        latency_ms=latency,
        since=since,
        checked_at=checked_at,
    )


def dependency_rung(
    rung_id: str,
    label: str,
    dependency: ComponentHealth | None,
    ts: float | None = None,
) -> CheckResult:
    """Build a dependency rung mirroring ``dependency``'s status, capped at ``warn``.

    Message: ``"PostgreSQL is down (Critical): connection refused"``, ``"Solr is degraded
    (Warning): search took 2.1 s"``; a dependency that is critical only because it answered too
    slowly reads ``"Solr is slow (Critical): search took 4.3 s"``; ``ok`` dependencies read
    ``"PostgreSQL is healthy"``. The reason after the colon is the message of the rung that
    decides the state (:func:`_deciding_rung`), so a slow REST API never explains "is down".
    A missing dependency (module off / not collected yet) yields an informational ``unknown``
    "not monitored" rung that does not count towards the component's status.
    """
    ts = ts or time.time()
    if dependency is None:
        return CheckResult(
            id=rung_id,
            label=label,
            status="unknown",
            ts=ts,
            message="not monitored",
            detail={"informational": True},
        )
    status: Status = dependency.status
    if status == "crit":
        status = "warn"
    if status == "off":
        status = "unknown"
    state = DEPENDENCY_STATE.get(dependency.status, f"is {status_label(dependency.status)}")
    decider = _deciding_rung(dependency)
    if dependency.status == "crit" and decider is not None and decider.detail.get("slow"):
        state = SLOW_STATE
    reason = dependency.reason
    if decider is not None:
        reason = decider.message or f"{decider.label}: {decider.status}"
    message = f"{dependency.label} {state}"
    if reason and dependency.status != "ok":
        message += f": {reason}"
    return CheckResult(
        id=rung_id,
        label=label,
        status=status,
        ts=ts,
        latency_ms=dependency.latency_ms,
        message=message,
        detail={"dependency": dependency.id, "dependency_status": dependency.status},
    )


def _deciding_rung(comp: ComponentHealth) -> CheckResult | None:
    """The rung whose status ``comp`` reports, or ``None`` without one (no chain).

    Critical: the first critical rung that failed ("is down"); when every critical rung only
    answered too slowly (``detail.slow``), the first of those ("is slow"). Any other status: the
    first counted rung with that status. Informational rungs never decide.
    """
    counted = [r for r in comp.chain or [] if not is_informational(r)]
    if comp.status == "crit":
        crit = [r for r in counted if r.status == "crit"]
        return next((r for r in crit if not r.detail.get("slow")), crit[0] if crit else None)
    return next((r for r in counted if r.status == comp.status), None)


def not_reached(rungs: list[tuple[str, str]], ts: float) -> list[CheckResult]:
    """Rungs after a failed one: each becomes ``unknown`` with message ``"not reached"``."""
    return [CheckResult.not_reached(rid, label, ts) for rid, label in rungs]


# ---------------------------------------------------------------------- server status


def threshold_status(value: float | None, warning: float, critical: float, inverted: bool = False) -> Status:
    """Classify ``value`` against warning/critical thresholds (``inverted`` = lower is worse)."""
    if value is None:
        return "unknown"
    if inverted:
        if value <= critical:
            return "crit"
        if value <= warning:
            return "warn"
        return "ok"
    if value >= critical:
        return "crit"
    if value >= warning:
        return "warn"
    return "ok"


def server_status(sample: SystemSample | None, cfg: Config) -> tuple[Status, str | None, dict[str, Status]]:
    """Evaluate the server thresholds on a sample.

    Returns ``(status, reason, per_metric)`` where ``per_metric`` maps
    ``cpu|memory|swap|disk|load`` (and ``disk:<path>`` for extra mounts) to a status.
    """
    if sample is None:
        return "unknown", None, {}
    th = cfg.alerts
    per: dict[str, Status] = {
        "cpu": threshold_status(sample.cpu_pct, th.cpu.warning, th.cpu.critical),
        "memory": threshold_status(sample.mem_pct, th.memory.warning, th.memory.critical),
        "swap": threshold_status(
            sample.swap_pct if sample.swap_total else 0.0, th.swap.warning, th.swap.critical
        ),
        "disk": threshold_status(sample.disk_pct, th.disk.warning, th.disk.critical),
        "load": threshold_status(sample.load_per_core, th.load_per_core.warning, th.load_per_core.critical),
    }
    for mount in sample.disks:
        pct = mount.get("pct")
        if pct is not None:
            per[f"disk:{mount.get('path', '?')}"] = threshold_status(
                float(pct), th.disk.warning, th.disk.critical
            )
    status = worst(per.values())
    reason: str | None = None
    if status != "ok":
        for key, st in per.items():
            if st == status:
                reason = _server_reason(key, sample)
                break
    return status, reason, per


def _server_reason(key: str, sample: SystemSample) -> str:
    if key == "cpu":
        return f"CPU {sample.cpu_pct:.0f} %"
    if key == "memory":
        return f"Memory {sample.mem_pct:.0f} %"
    if key == "swap":
        return f"Swap {sample.swap_pct:.0f} %"
    if key == "disk":
        return f"Disk {sample.disk_pct:.0f} % on {sample.disk_path}"
    if key == "load":
        return f"Load {sample.load_per_core:.1f} per core"
    if key.startswith("disk:"):
        path = key.split(":", 1)[1]
        pct = next((m.get("pct") for m in sample.disks if m.get("path") == path), 0.0)
        return f"Disk {float(pct or 0):.0f} % on {path}"
    return key


def server_health(
    sample: SystemSample | None,
    cfg: Config,
    previous: ComponentHealth | None = None,
    now: float | None = None,
) -> ComponentHealth:
    """The ``server`` component (no chain): status from the thresholds on ``sample``."""
    now = now or time.time()
    status, reason, per = server_status(sample, cfg)
    metrics: dict[str, Any] = {}
    if sample is not None:
        metrics = {
            "cpu_pct": sample.cpu_pct,
            "mem_pct": sample.mem_pct,
            "swap_pct": sample.swap_pct,
            "disk_pct": sample.disk_pct,
            "load1": sample.load1,
            "load5": sample.load5,
            "load15": sample.load15,
            "per_core_load": round(sample.load_per_core, 2),
            "cores": sample.cores,
            "uptime": sample.uptime_s,
            "os": f"{sample.os_name} {sample.os_version}".strip(),
            "kernel": sample.kernel,
            "hostname": sample.hostname,
            "per_metric": per,
        }
    since = now
    if previous is not None and previous.status == status and previous.since is not None:
        since = previous.since
    meta = COMPONENTS["server"]
    return ComponentHealth(
        id="server",
        label=meta.label,
        emoji=meta.emoji,
        status=status,
        reason=reason,
        chain=[],
        metrics=metrics,
        depends_on=[],
        latency_ms=None,
        since=since,
        checked_at=sample.ts if sample else None,
    )


# ----------------------------------------------------------------------------- mood


def _alert_field(alert: Any, name: str) -> Any:
    return alert.get(name) if isinstance(alert, dict) else getattr(alert, name, None)


def open_alert_list(alerts: Iterable[Alert | dict[str, Any]] | None) -> list[Any]:
    """The alerts of ``alerts`` that are still open (``Alert`` objects or ``/api`` dicts)."""
    if not alerts:
        return []
    return [a for a in alerts if _alert_field(a, "status") in (None, "open")]


def is_acknowledged(alert: Alert | dict[str, Any]) -> bool:
    """True when the alert was acknowledged (``acked_ts`` set)."""
    return bool(_alert_field(alert, "acked_ts"))


def unacknowledged(alerts: Iterable[Alert | dict[str, Any]] | None) -> list[Any]:
    """The open alerts of ``alerts`` nobody has acknowledged."""
    return [a for a in open_alert_list(alerts) if not is_acknowledged(a)]


def acknowledged_components(alerts: Iterable[Alert | dict[str, Any]] | None) -> set[str]:
    """Components with at least one open alert, every one of them acknowledged."""
    acked: dict[str, bool] = {}
    for alert in open_alert_list(alerts):
        component = _alert_field(alert, "component")
        if component:
            acked[str(component)] = acked.get(str(component), True) and is_acknowledged(alert)
    return {component for component, all_acked in acked.items() if all_acked}


def alert_mood(alerts: Iterable[Alert | dict[str, Any]] | None) -> Status:
    """``crit`` when any open alert is critical, ``warn`` when any is a warning, else ``ok``."""
    severities = {_alert_field(a, "severity") for a in open_alert_list(alerts)}
    if "critical" in severities:
        return "crit"
    if "warning" in severities:
        return "warn"
    return "ok"


def component_mood(state: State, cfg: Config | None = None, calm: Iterable[str] = ()) -> Status:
    """Worst status of every current component (the server component included), alerts aside.

    The components in ``calm`` (their open alerts are all acknowledged) count as ``ok``.
    """
    calm = set(calm)
    statuses = ["ok" if c.id in calm else c.status for c in state.components.values() if c.status != "off"]
    if cfg is not None and "server" not in state.components and state.sample is not None:
        statuses.append("ok" if "server" in calm else server_status(state.sample, cfg)[0])
    if not statuses:
        return "unknown"
    return worst(statuses)


def mood(
    state: State, cfg: Config | None = None, alerts: Iterable[Alert | dict[str, Any]] | None = None
) -> Status:
    """The overall mood: worst of the components *and* the open alerts nobody acknowledged.

    A component may already read ``ok`` again while its alert is still open (``clear_samples``
    hysteresis, an acked-but-unresolved incident): any open critical alert keeps the mood ``crit``
    and any open warning keeps it at least ``warn``, so the accent, the Pulse band and ``doctor``
    never say "All systems healthy" next to an open alert.

    Acknowledging calms it: an acknowledged alert counts as none, and a component whose open
    alerts are all acknowledged counts as ``ok`` here (its own status, on the Stack, its page and
    in History, stays what it is). A new alert, or an escalation (which clears the
    acknowledgement), colours the mood again. A component that warns without an alert still does.
    """
    open_alerts = open_alert_list(alerts)
    base = component_mood(state, cfg, acknowledged_components(open_alerts))
    from_alerts = alert_mood(unacknowledged(open_alerts))
    if from_alerts == "ok":
        return base
    return worst([base, from_alerts])


def worst_component(components: Iterable[ComponentHealth]) -> ComponentHealth | None:
    """The component with the worst status (``None`` when empty); ties keep display order."""
    best: ComponentHealth | None = None
    for comp in components:
        if comp.status == "off":
            continue
        if best is None or STATUS_RANK.get(comp.status, 1) > STATUS_RANK.get(best.status, 1):
            best = comp
    return best


def _crit_phrase(comp: ComponentHealth) -> str:
    if comp.id == "server":
        return comp.reason or "Server critical"
    failing = next((r for r in comp.chain if r.status == "crit" and not r.detail.get("slow")), None)
    if failing is not None:
        short = failing.id.split(".", 1)[-1]
        if short.split(".")[0] in _CONNECTIVITY_RUNGS:
            return f"{comp.label} unreachable"
        return f"{comp.label} failing"
    if any(r.status == "crit" for r in comp.chain):
        return f"{comp.label} slow"  # answered correctly, past the critical latency
    return f"{comp.label} critical"


def headline(
    state: State, cfg: Config | None = None, alerts: Iterable[Alert | dict[str, Any]] | None = None
) -> tuple[Status, str]:
    """Return ``(mood, headline)`` for the Pulse band.

    * ok → "All systems healthy", or "All systems healthy · 2 acknowledged" while acknowledged
      alerts are open and nothing else is wrong (see :func:`mood`)
    * warn → "1 warning" / "2 warnings" (components), or "1 open alert" / "3 open alerts" when
      only open warning alerts raise the mood
    * crit → "Critical: PostgreSQL unreachable" (or "Critical: Memory 96 %", or "Critical: DSpace
      Backend slow" when the rung only crossed its latency threshold); when only an open
      critical alert raises the mood, its title: "Critical: 120 errors in DSpace log (5 min)"
    * unknown → "Waiting for the first check" (nothing collected yet) or
      "<n> checks could not run".

    Acknowledged alerts and the components whose open alerts are all acknowledged are left out
    of the counts, as they are left out of the mood.
    """
    all_open = open_alert_list(alerts)
    calm = acknowledged_components(all_open)
    open_alerts = unacknowledged(all_open)
    base = component_mood(state, cfg, calm)
    current = mood(state, cfg, all_open)
    every = [c for c in state.components.values() if c.status != "off"]
    comps = [c for c in every if c.id not in calm]
    if current == "ok":
        acked = len(all_open) - len(open_alerts)
        return current, f"All systems healthy · {acked} acknowledged" if acked else "All systems healthy"
    if current == "warn":
        if base == "warn":
            n = sum(1 for c in comps if c.status == "warn")
            return current, f"{n} warning" if n == 1 else f"{n} warnings"
        n = len(open_alerts)
        return current, f"{n} open alert" if n == 1 else f"{n} open alerts"
    if current == "crit":
        if base == "crit":
            crit = [c for c in comps if c.status == "crit"]
            first = crit[0]
            text = f"Critical: {_crit_phrase(first)}"
            if len(crit) > 1:
                text += f" (+{len(crit) - 1} more)"
            return current, text
        critical = [a for a in open_alerts if _alert_field(a, "severity") == "critical"]
        title = str(_alert_field(critical[0], "title") or "open critical alert")
        text = f"Critical: {title}"
        if len(critical) > 1:
            text += f" (+{len(critical) - 1} more)"
        return current, text
    if not every and state.sample is None:
        return current, "Waiting for the first check"
    n = sum(1 for c in comps if c.status == "unknown")
    return current, f"{n} check could not run" if n == 1 else f"{n} checks could not run"


def subline(hostname: str, checked_ago_s: float | None) -> str:
    """ "server-1 · checked 8 s ago" (or "· first check pending")."""
    if checked_ago_s is None:
        return f"{hostname} · first check pending"
    from dspace_sentinel.util.fmt import relative

    return f"{hostname} · checked {relative(checked_ago_s)}"


__all__ = [
    "ANY_OF_DEPENDENCIES",
    "COMPONENTS",
    "COMPONENT_ORDER",
    "DEPENDENCY_GRAPH",
    "HEADLINE_LATENCY",
    "DEPENDENCY_STATE",
    "SLOW_STATE",
    "STATUS_LABEL",
    "STATUS_MARK",
    "ComponentMeta",
    "acknowledged_components",
    "affected_closure",
    "alert_mood",
    "component_label",
    "component_mood",
    "dependency_rung",
    "dependents_of",
    "headline",
    "is_acknowledged",
    "is_informational",
    "meta_for",
    "mood",
    "not_reached",
    "open_alert_list",
    "rollup",
    "server_health",
    "server_status",
    "status_label",
    "subline",
    "threshold_status",
    "unacknowledged",
    "worst",
    "worst_component",
]
