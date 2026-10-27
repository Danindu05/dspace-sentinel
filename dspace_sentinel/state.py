"""In-memory current state: latest sample, component health, sparklines, cycle bookkeeping."""

from __future__ import annotations

import time
from collections import deque
from typing import Any

from dspace_sentinel.models import ComponentHealth, SystemSample

SPARK_POINTS = 12
SPARK_KEYS: tuple[str, ...] = ("cpu", "mem", "disk", "load")


class State:
    """Mutable snapshot of "now", owned by the scheduler and read by the API/CLI.

    Attributes:
        started_ts: when this Sentinel process started.
        sample: the newest :class:`SystemSample` (``None`` before the first system cycle).
        components: current :class:`ComponentHealth` keyed by component id (insertion = display order).
        sparks: last 12 values of cpu/mem/disk/load for the Home stat tiles.
        last_cycle_ts: when any collector group last finished.
        checking: true while ``run_all_now`` is in flight (UI shows "Checking…").
    """

    def __init__(self) -> None:
        self.started_ts: float = time.time()
        self.sample: SystemSample | None = None
        self.components: dict[str, ComponentHealth] = {}
        self.sparks: dict[str, deque[float]] = {k: deque(maxlen=SPARK_POINTS) for k in SPARK_KEYS}
        self.last_cycle_ts: float | None = None
        self.last_error: str | None = None
        self.checking: bool = False
        self.cycle_count: int = 0
        self.extra: dict[str, Any] = {}

    # ---------------------------------------------------------------- samples

    def set_sample(self, sample: SystemSample) -> None:
        """Store a new sample and push its headline values onto the sparklines."""
        self.sample = sample
        self.sparks["cpu"].append(round(sample.cpu_pct, 1))
        self.sparks["mem"].append(round(sample.mem_pct, 1))
        self.sparks["disk"].append(round(sample.disk_pct, 1))
        self.sparks["load"].append(round(sample.load1, 2))

    def seed_sparks(self, points: dict[str, list[float]]) -> None:
        """Pre-fill sparklines (e.g. from the store after a restart); keeps the last 12 of each."""
        for key, values in points.items():
            if key in self.sparks:
                self.sparks[key].clear()
                for v in list(values)[-SPARK_POINTS:]:
                    self.sparks[key].append(float(v))

    def spark_dict(self) -> dict[str, list[float]]:
        """``{cpu: [...12], mem: [...], disk: [...], load: [...]}``."""
        return {k: list(v) for k, v in self.sparks.items()}

    # ------------------------------------------------------------- components

    def set_component(self, comp: ComponentHealth) -> ComponentHealth | None:
        """Replace a component's health; returns the previous value (``None`` if new)."""
        previous = self.components.get(comp.id)
        self.components[comp.id] = comp
        return previous

    def remove_component(self, component_id: str) -> None:
        """Forget a component (its module was switched off)."""
        self.components.pop(component_id, None)

    def component(self, component_id: str) -> ComponentHealth | None:
        """Current health of one component."""
        return self.components.get(component_id)

    def components_list(self) -> list[ComponentHealth]:
        """Components in display order (server first)."""
        ordered = sorted(self.components.values(), key=lambda c: (c.id != "server", 0))
        return ordered

    def statuses(self) -> dict[str, str]:
        """``{component_id: status}``."""
        return {cid: c.status for cid, c in self.components.items()}

    def reset(self) -> None:
        """Drop everything except ``started_ts`` (used after *Clear history*)."""
        self.sample = None
        self.components.clear()
        for key in self.sparks:
            self.sparks[key].clear()
        self.last_cycle_ts = None
        self.last_error = None
        self.cycle_count = 0

    def checked_ago_s(self, now: float | None = None) -> float | None:
        """Seconds since the last completed cycle (``None`` before the first)."""
        if self.last_cycle_ts is None:
            return None
        return max(0.0, (now or time.time()) - self.last_cycle_ts)


__all__ = ["SPARK_KEYS", "SPARK_POINTS", "State"]
