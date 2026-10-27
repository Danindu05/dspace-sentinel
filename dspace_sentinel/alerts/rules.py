"""Alert rules (SPEC section 7): sample + component healths → :class:`Observation` list.

Every rule yields one observation per evaluation with a *severity* (``warning`` |
``critical`` | ``None``) and an *ok* flag used for the hysteresis: an observation counts as
OK only when the value is below the warning threshold minus 2 % of the range (above it for
inverted rules such as SSL expiry), or when the component is healthy. An observation with
neither severity nor ok (value inside the hysteresis band, or unmeasurable right now) is
neutral and resets the engine's streak counters.

Rule ids are the SPEC names; per-item rules carry the item in the id so each has its own
state: ``disk`` (root) / ``disk:/dspace`` (extra mounts), ``log_errors:<file id>`` and
``component_down:<component id>``. ``rule.split(":")[0]`` is the *base rule* used by the
incident hypotheses.

``component_down`` only counts *hard* failures: a rung whose warn/crit status is exactly
what its own threshold rule would produce (a slow ``backend.search``, a certificate with 12
days left, a log file with 48 errors) is covered by that rule and ignored here, and
``*.dep.*`` rungs mirror another component's alert, so they are ignored too. A rung that is
worse than its threshold grade (``backend.search`` failing with HTTP 500) is a hard failure.

A check that could not run (the scheduler's ``<id>.error`` rung, ``detail.collector_error``)
measured nothing: ``component_down`` is neutral for it and the per-file log rules skip it, so
a time-out can neither open nor resolve an alert.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from dspace_sentinel.alerts.messages import chain_text
from dspace_sentinel.health import component_label, threshold_status
from dspace_sentinel.models import CheckResult, ComponentHealth, SystemSample
from dspace_sentinel.util.fmt import gb, ms

if TYPE_CHECKING:  # pragma: no cover
    from dspace_sentinel.config import Config, Threshold

GIB = 1024**3

PERCENT_RULES = frozenset({"cpu", "memory", "swap", "disk", "postgres_connections"})
"""Rules whose value is a percentage: the hysteresis margin is 2 points (2 % of 0–100)."""

INVERTED_RULES = frozenset({"ssl_expiry", "assetstore_free"})
"""Rules where a *lower* value is worse."""

CONNECTIVITY_RUNGS = frozenset(
    {"service", "tcp", "connect", "admin", "rest", "home", "http", "https", "cert", "path"}
)
"""Short rung ids whose failure means "unreachable" rather than "failing"."""

LATENCY_RULES: dict[str, tuple[str, str, str, str, str]] = {
    # rule: (component, rung id, threshold key, title prefix, message)
    "rest_latency": ("backend", "backend.rest", "rest_latency_ms", "REST API", "GET /server/api"),
    "search_latency": (
        "backend",
        "backend.search",
        "search_latency_ms",
        "Search API",
        "GET /server/api/discover/search/objects",
    ),
    "solr_latency": (
        "solr",
        "solr.search",
        "search_latency_ms",
        "Solr search",
        "select q=*:* rows=0 on core search",
    ),
    "home_latency": ("frontend", "frontend.home", "home_latency_ms", "Homepage", "GET {url}"),
}
"""Latency rules: the rung's ``latency_ms`` graded against ``alerts.<threshold key>``."""

GRADED_RUNGS: dict[str, tuple[str, str, bool]] = {
    # rung id: (threshold key, metric source "latency" | "value", inverted)
    "solr.search": ("search_latency_ms", "latency", False),
    "backend.rest": ("rest_latency_ms", "latency", False),
    "backend.search": ("search_latency_ms", "latency", False),
    "frontend.home": ("home_latency_ms", "latency", False),
    "ssl.cert": ("ssl_expiry_days", "value", True),
    "assetstore.path": ("assetstore_free_gb", "value", True),
}
"""Rungs whose warn/crit status is produced by a threshold a dedicated rule already covers.
``logs.<id>`` rungs (value = errors in window) are graded by ``log_errors_per_window``."""

LOG_RUNG_SPEC: tuple[str, str, bool] = ("log_errors_per_window", "value", False)

ALL_RULES: tuple[str, ...] = (
    "cpu",
    "memory",
    "swap",
    "disk",
    "load",
    "rest_latency",
    "search_latency",
    "solr_latency",
    "home_latency",
    "postgres_connections",
    "ssl_expiry",
    "log_errors",
    "assetstore_free",
    "component_down",
    "security_ports",
    "security_firewall",
    "security_ssh",
)
"""Every base rule id, in SPEC order (``solr_latency`` is the additive Solr search rule)."""

SECURITY_RULES: dict[str, tuple[str, str]] = {
    "security_ports": ("security.ports", "New port open to the network"),
    "security_firewall": ("security.firewall", "No host firewall while ports are exposed"),
    "security_ssh": ("security.ssh", "Root may log in over SSH with a password"),
}
"""Security rule → (rung id, alert title). The rung's ``detail.alert`` opens the alert; updates,
software, web and files never alert (the Security page and the reports show them)."""


@dataclass
class Observation:
    """One rule's verdict for one subject at one instant.

    Attributes:
        rule: full rule id (``cpu``, ``disk:/dspace``, ``log_errors:dspace``, ``component_down:solr``).
        component: component id the alert is filed under (``server`` for host rules).
        severity: ``warning`` | ``critical`` when breaching, else ``None``.
        value: the measured value (percent, ms, days, GB, count) or ``None``.
        threshold: the threshold that was crossed (``None`` when not breaching).
        title, message: the alert text should this observation open an alert.
        ok: True when the observation counts towards clearing an open alert (hysteresis).
        observed_ts: timestamp of the underlying sample/check, so one measurement is
            counted once even when the engine runs several times before the next one.
    """

    rule: str
    component: str
    severity: str | None
    value: float | None
    threshold: float | None
    title: str
    message: str
    ok: bool = False
    observed_ts: float | None = None

    @property
    def base_rule(self) -> str:
        """``disk:/dspace`` → ``disk``; ``component_down:solr`` → ``component_down``."""
        return self.rule.split(":", 1)[0]

    @property
    def key(self) -> str:
        """The engine's state key: ``<rule>@<component>``."""
        return f"{self.rule}@{self.component}"

    @property
    def breaching(self) -> bool:
        """True when a severity is set."""
        return self.severity is not None

    def to_dict(self) -> dict[str, Any]:
        """Plain dict (plus ``key`` and ``base_rule``)."""
        data = asdict(self)
        data["key"] = self.key
        data["base_rule"] = self.base_rule
        return data


# ------------------------------------------------------------------------ grading


def hysteresis_margin(base_rule: str, th: Threshold) -> float:
    """The clear margin below (above, when inverted) the warning threshold.

    2 % of the range: 2 points for percentage rules, 2 % of ``|critical − warning|`` otherwise.
    """
    if base_rule in PERCENT_RULES:
        return 2.0
    return 0.02 * abs(float(th.critical) - float(th.warning))


def grade(value: float | None, base_rule: str, th: Threshold) -> tuple[str | None, bool, float | None]:
    """Classify ``value`` against ``th``: ``(severity, ok, threshold_crossed)``.

    ``ok`` applies the hysteresis; ``None`` values are neutral (no severity, not ok).
    """
    if value is None:
        return None, False, None
    inverted = base_rule in INVERTED_RULES
    status = threshold_status(value, th.warning, th.critical, inverted)
    if status == "crit":
        return "critical", False, float(th.critical)
    if status == "warn":
        return "warning", False, float(th.warning)
    margin = hysteresis_margin(base_rule, th)
    ok = value > th.warning + margin if inverted else value < th.warning - margin
    return None, ok, None


def rung_grade(rung: CheckResult, cfg: Config) -> str | None:
    """The status the matching threshold rule would give this rung, or ``None`` if none grades it."""
    spec = GRADED_RUNGS.get(rung.id)
    if spec is None and rung.id.startswith("logs."):
        spec = LOG_RUNG_SPEC
    if spec is None:
        return None
    key, source, inverted = spec
    metric = rung.latency_ms if source == "latency" else rung.value
    if metric is None:
        return None
    th = getattr(cfg.alerts, key)
    return threshold_status(float(metric), th.warning, th.critical, inverted)


def is_graded(rung: CheckResult, cfg: Config) -> bool:
    """True when the rung's warn/crit status is exactly its threshold grade (a rule covers it)."""
    return rung.status in ("warn", "crit") and rung_grade(rung, cfg) == rung.status


def is_hard_failure(rung: CheckResult, cfg: Config) -> bool:
    """True when the rung is warn/crit for a reason a threshold rule does not explain."""
    return rung.status in ("warn", "crit") and not is_graded(rung, cfg)


def is_dependency_rung(rung: CheckResult) -> bool:
    """``backend.dep.solr`` style rungs mirror another component and never alert on their own."""
    return ".dep." in rung.id


def is_collector_error(rung: CheckResult) -> bool:
    """The scheduler's ``<id>.error`` rung: the collector timed out or raised, nothing was measured."""
    return bool(rung.detail.get("collector_error"))


def nothing_measured(comp: ComponentHealth) -> bool:
    """True when the last check of ``comp`` could not tell anything about it.

    Its chain is the scheduler's error rung, or holds no rung with a real reading (``ok`` /
    ``warn`` / ``crit`` outside the dependency rungs). A component without any chain
    (metrics only) is not in that state.
    """
    if not comp.chain:
        return False
    if any(is_collector_error(r) for r in comp.chain):
        return True
    return not any(r.status in ("ok", "warn", "crit") and not is_dependency_rung(r) for r in comp.chain)


def find_rung(comp: ComponentHealth, rung_id: str) -> CheckResult | None:
    """The rung with ``rung_id`` in the component's chain, if present."""
    for rung in comp.chain:
        if rung.id == rung_id:
            return rung
    return None


def _neutral(rule: str, component: str, title: str, message: str, ts: float | None) -> Observation:
    return Observation(rule, component, None, None, None, title, message, ok=False, observed_ts=ts)


def _num(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _growth_suffix(components: dict[str, ComponentHealth], mount: str) -> str:
    """`` · growing 2.1 GB/day`` when the assetstore component reports growth for ``mount``."""
    asset = components.get("assetstore")
    if asset is None:
        return ""
    metrics = asset.metrics or {}
    if metrics.get("mount") != mount:
        return ""
    growth = _num(metrics.get("growth_bytes_per_day"))
    if not growth or growth <= 0:
        return ""
    return f" · growing {gb(growth)}/day"


# -------------------------------------------------------------------- server rules


def server_rules(
    sample: SystemSample, components: dict[str, ComponentHealth], cfg: Config
) -> list[Observation]:
    """``cpu``, ``memory``, ``swap``, ``disk`` (root + every extra mount) and ``load``."""
    th = cfg.alerts
    ts = sample.ts
    out: list[Observation] = []

    sev, ok, crossed = grade(sample.cpu_pct, "cpu", th.cpu)
    out.append(
        Observation(
            "cpu",
            "server",
            sev,
            sample.cpu_pct,
            crossed,
            f"CPU {sample.cpu_pct:.0f} %",
            f"load {sample.load1:.1f} on {sample.cores} cores",
            ok,
            ts,
        )
    )

    sev, ok, crossed = grade(sample.mem_pct, "memory", th.memory)
    mem_message = (
        f"{sample.mem_used / GIB:.1f} of {sample.mem_total / GIB:.0f} GB used, swap {sample.swap_pct:.0f} %"
    )
    out.append(
        Observation(
            "memory",
            "server",
            sev,
            sample.mem_pct,
            crossed,
            f"Memory {sample.mem_pct:.0f} %",
            mem_message,
            ok,
            ts,
        )
    )

    if sample.swap_total > 0:
        sev, ok, crossed = grade(sample.swap_pct, "swap", th.swap)
        out.append(
            Observation(
                "swap",
                "server",
                sev,
                sample.swap_pct,
                crossed,
                f"Swap {sample.swap_pct:.0f} %",
                f"{gb(sample.swap_used)} of {gb(sample.swap_total)} used",
                ok,
                ts,
            )
        )

    sev, ok, crossed = grade(sample.disk_pct, "disk", th.disk)
    out.append(
        Observation(
            "disk",
            "server",
            sev,
            sample.disk_pct,
            crossed,
            f"Disk {sample.disk_pct:.0f} % on {sample.disk_path}",
            f"free {gb(sample.disk_free)}" + _growth_suffix(components, sample.disk_path),
            ok,
            ts,
        )
    )
    for mount in sample.disks or []:
        path = str(mount.get("path") or "").strip()
        pct = _num(mount.get("pct"))
        if not path or path == sample.disk_path or pct is None:
            continue
        sev, ok, crossed = grade(pct, "disk", th.disk)
        free = _num(mount.get("free"))
        message = f"free {gb(free)}" if free is not None else f"{pct:.0f} % used"
        out.append(
            Observation(
                f"disk:{path}",
                "server",
                sev,
                pct,
                crossed,
                f"Disk {pct:.0f} % on {path}",
                message + _growth_suffix(components, path),
                ok,
                ts,
            )
        )

    per_core = sample.load_per_core
    sev, ok, crossed = grade(per_core, "load", th.load_per_core)
    out.append(
        Observation(
            "load",
            "server",
            sev,
            round(per_core, 2),
            crossed,
            f"Load {per_core:.1f} per core",
            f"load {sample.load1:.2f} on {sample.cores} cores",
            ok,
            ts,
        )
    )
    return out


# ----------------------------------------------------------------- component rules


def latency_rule(rule: str, comp: ComponentHealth, cfg: Config) -> Observation:
    """One of :data:`LATENCY_RULES` for ``comp``.

    Reads the rung's ``latency_ms`` (falling back to the ``<rung>_latency_ms`` metric); a rung
    that is unknown, has no latency, or is a hard failure yields a *neutral* observation, as
    does a component that did not report the rung at all (a collector time-out must not
    resolve an open latency alert).
    """
    component, rung_id, key, prefix, message = LATENCY_RULES[rule]
    th = getattr(cfg.alerts, key)
    ts = comp.checked_at
    rung = find_rung(comp, rung_id)
    if message == "GET {url}":
        url = (rung.detail.get("url") if rung is not None else None) or cfg.dspace.ui_url
        message = f"GET {url}"
    if rung is not None:
        if rung.status == "unknown" or rung.latency_ms is None or is_hard_failure(rung, cfg):
            return _neutral(rule, component, f"{prefix} latency", message, ts)
        value: float | None = float(rung.latency_ms)
    else:
        value = _num((comp.metrics or {}).get(rung_id.split(".", 1)[1] + "_latency_ms"))
        if value is None:
            return _neutral(rule, component, f"{prefix} latency", message, ts)
    sev, ok, crossed = grade(value, rule, th)
    return Observation(rule, component, sev, value, crossed, f"{prefix} slow: {ms(value)}", message, ok, ts)


def postgres_connections_rule(comp: ComponentHealth, cfg: Config) -> Observation:
    """``postgres_connections``: ``connections_pct`` (or connections / max_connections)."""
    metrics = comp.metrics or {}
    conns = _num(metrics.get("connections"))
    max_conns = _num(metrics.get("max_connections"))
    pct = _num(metrics.get("connections_pct"))
    if pct is None and conns is not None and max_conns:
        pct = 100.0 * conns / max_conns
    ts = comp.checked_at
    if pct is None:
        return _neutral("postgres_connections", "postgres", "PostgreSQL connections", "", ts)
    sev, ok, crossed = grade(pct, "postgres_connections", cfg.alerts.postgres_connections_pct)
    if conns is not None and max_conns:
        title = f"PostgreSQL connections {conns:.0f} / {max_conns:.0f}"
    else:
        title = f"PostgreSQL connections {pct:.0f} %"
    return Observation(
        "postgres_connections",
        "postgres",
        sev,
        round(pct, 1),
        crossed,
        title,
        f"{pct:.0f} % of max_connections in use",
        ok,
        ts,
    )


def ssl_expiry_rule(comp: ComponentHealth, cfg: Config) -> Observation:
    """``ssl_expiry``: days left (inverted thresholds)."""
    metrics = comp.metrics or {}
    rung = find_rung(comp, "ssl.cert")
    days = _num(metrics.get("days_left"))
    if days is None and rung is not None:
        days = _num(rung.value)
    ts = comp.checked_at
    if days is None:
        return _neutral("ssl_expiry", "ssl", "Certificate expiry", "", ts)
    sev, ok, crossed = grade(days, "ssl_expiry", cfg.alerts.ssl_expiry_days)
    if days <= 0:
        title = "Certificate expired"
    elif round(days) == 1:
        title = "Certificate expires in 1 day"
    else:
        title = f"Certificate expires in {days:.0f} days"
    parts = [str(p) for p in (metrics.get("subject"), metrics.get("not_after")) if p]
    message = " · not after ".join(parts) if len(parts) == 2 else (parts[0] if parts else "")
    return Observation("ssl_expiry", "ssl", sev, round(days, 1), crossed, title, message, ok, ts)


def log_error_rules(comp: ComponentHealth, cfg: Config) -> list[Observation]:
    """``log_errors:<id>`` per ``logs.<id>`` rung (value = errors in the window)."""
    metrics = comp.metrics or {}
    files = metrics.get("files") if isinstance(metrics.get("files"), dict) else {}
    window = _num(metrics.get("window_minutes")) or float(cfg.logs.window_minutes)
    th = cfg.alerts.log_errors_per_window
    ts = comp.checked_at
    out: list[Observation] = []
    for rung in comp.chain:
        if not rung.id.startswith("logs.") or is_collector_error(rung):
            continue
        file_id = rung.id.split(".", 1)[1]
        info = files.get(file_id) if isinstance(files, dict) else None
        info = info if isinstance(info, dict) else {}
        label = str(info.get("label") or rung.label.replace(" log", "") or file_id)
        rule = f"log_errors:{file_id}"
        errors = _num(rung.value)
        if errors is None:
            errors = _num((rung.detail or {}).get("errors"))
        if rung.status == "unknown" or errors is None:
            out.append(_neutral(rule, "logs", f"{label} log errors", "", ts))
            continue
        sev, ok, crossed = grade(errors, "log_errors", th)
        last = info.get("last_error") if isinstance(info.get("last_error"), dict) else None
        message = str(last.get("line") or "").strip()[:200] if last else ""
        out.append(
            Observation(
                rule,
                "logs",
                sev,
                errors,
                crossed,
                f"{errors:.0f} errors in {label} log ({window:.0f} min)",
                message,
                ok,
                ts,
            )
        )
    return out


def assetstore_free_rule(comp: ComponentHealth, cfg: Config) -> Observation:
    """``assetstore_free``: free GB on the assetstore mount (inverted thresholds)."""
    metrics = comp.metrics or {}
    free = _num(metrics.get("free"))
    free_gb = free / GIB if free is not None else None
    rung = find_rung(comp, "assetstore.path")
    if free_gb is None and rung is not None:
        free_gb = _num(rung.value)
    ts = comp.checked_at
    if free_gb is None:
        return _neutral("assetstore_free", "assetstore", "Assetstore free space", "", ts)
    sev, ok, crossed = grade(free_gb, "assetstore_free", cfg.alerts.assetstore_free_gb)
    path = str(metrics.get("path") or cfg.dspace.assetstore_dir)
    pct = _num(metrics.get("pct"))
    message = path if pct is None else f"{path} · {pct:.0f} % used"
    mount = str(metrics.get("mount") or "")
    if mount:
        message += _growth_suffix({"assetstore": comp}, mount)
    return Observation(
        "assetstore_free",
        "assetstore",
        sev,
        round(free_gb, 1),
        crossed,
        f"Assetstore free space {free_gb:.1f} GB",
        message,
        ok,
        ts,
    )


def component_down_rule(comp: ComponentHealth, cfg: Config) -> Observation:
    """``component_down:<id>``: hard failures in the chain (crit ⇒ critical, warn ⇒ warning).

    Title: ``"<Label> unreachable"`` when the failing rung is a connectivity rung,
    ``"<Label> failing"`` for other critical failures, ``"<Label> degraded"`` for warnings.
    Message: chain summary up to the failing rung, then its reason on the next line.
    Without hard failures the observation is OK, unless nothing was measured (the collector
    timed out or raised): that is neutral, so it can neither open nor resolve the alert.
    """
    label = comp.label or component_label(comp.id)
    rule = f"component_down:{comp.id}"
    ts = comp.checked_at
    counted = [rung for rung in comp.chain if not is_dependency_rung(rung) and is_hard_failure(rung, cfg)]
    if not counted:
        if nothing_measured(comp):
            return _neutral(rule, comp.id, f"{label} not checked", "", ts)
        return Observation(rule, comp.id, None, None, None, f"{label} healthy", "", ok=True, observed_ts=ts)
    worst = "crit" if any(r.status == "crit" for r in counted) else "warn"
    failing = next(r for r in counted if r.status == worst)
    short = failing.id.split(".", 1)[1] if "." in failing.id else failing.id
    if worst == "crit":
        severity = "critical"
        title = f"{label} unreachable" if short.split(".")[0] in CONNECTIVITY_RUNGS else f"{label} failing"
    else:
        severity = "warning"
        title = f"{label} degraded"
    reason = failing.message or f"{failing.label}: {failing.status}"
    message = f"{chain_text(comp.chain, stop_at=failing.id)}\n{reason}"
    return Observation(rule, comp.id, severity, None, None, title, message, ok=False, observed_ts=ts)


def security_rule(rule: str, comp: ComponentHealth) -> Observation:
    """One of :data:`SECURITY_RULES` for the ``security`` component.

    A warning when the rung carries ``detail.alert`` (a new exposed port, no host firewall while
    ports are exposed, root may log in with a password); OK when the rung was checked and says
    otherwise; neutral when the rung is missing, could not be checked (``detail.unavailable``) or
    the collector could not run, so an unreadable file neither opens nor clears the alert. The
    message is the rung's message.
    """
    rung_id, title = SECURITY_RULES[rule]
    ts = comp.checked_at
    rung = find_rung(comp, rung_id)
    if rung is None or is_collector_error(rung) or rung.detail.get("unavailable"):
        return _neutral(rule, comp.id, title, "", ts)
    if rung.status == "warn" and rung.detail.get("alert"):
        return Observation(rule, comp.id, "warning", None, None, title, rung.message, observed_ts=ts)
    return Observation(rule, comp.id, None, None, None, title, "", ok=True, observed_ts=ts)


def component_rules(comp: ComponentHealth, cfg: Config) -> list[Observation]:
    """Every rule that applies to one (non-server) component.

    The ``security`` component has its three rules instead of ``component_down``: most of its
    warnings (updates, software, web, files) are shown, never alerted.
    """
    if comp.id == "security":
        return [security_rule(rule, comp) for rule in SECURITY_RULES]
    out: list[Observation] = []
    for rule, spec in LATENCY_RULES.items():
        if spec[0] == comp.id:
            out.append(latency_rule(rule, comp, cfg))
    if comp.id == "postgres":
        out.append(postgres_connections_rule(comp, cfg))
    elif comp.id == "ssl":
        out.append(ssl_expiry_rule(comp, cfg))
    elif comp.id == "logs":
        out.extend(log_error_rules(comp, cfg))
    elif comp.id == "assetstore":
        out.append(assetstore_free_rule(comp, cfg))
    out.append(component_down_rule(comp, cfg))
    return out


def evaluate(
    sample: SystemSample | None, components: dict[str, ComponentHealth], cfg: Config
) -> list[Observation]:
    """Run every rule: host rules on ``sample`` (when present) and component rules on each component.

    Components not present (module off, not collected yet) produce nothing, so a module
    that is off leaves no trace. The ``server`` entry of ``components`` is skipped (the
    sample carries its numbers).
    """
    out: list[Observation] = []
    if sample is not None:
        out.extend(server_rules(sample, components, cfg))
    for cid, comp in components.items():
        if cid == "server" or comp is None or comp.status == "off":
            continue
        out.extend(component_rules(comp, cfg))
    return out


__all__ = [
    "ALL_RULES",
    "CONNECTIVITY_RUNGS",
    "GRADED_RUNGS",
    "INVERTED_RULES",
    "LATENCY_RULES",
    "PERCENT_RULES",
    "SECURITY_RULES",
    "Observation",
    "assetstore_free_rule",
    "component_down_rule",
    "component_rules",
    "evaluate",
    "find_rung",
    "grade",
    "hysteresis_margin",
    "is_collector_error",
    "is_dependency_rung",
    "is_graded",
    "is_hard_failure",
    "latency_rule",
    "log_error_rules",
    "nothing_measured",
    "postgres_connections_rule",
    "rung_grade",
    "security_rule",
    "server_rules",
    "ssl_expiry_rule",
]
