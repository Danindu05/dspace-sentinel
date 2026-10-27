"""Collector ABC, context and result types (SPEC section 6), plus :func:`run_blocking`."""

from __future__ import annotations

import asyncio
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

from dspace_sentinel.models import CheckResult, SystemSample

if TYPE_CHECKING:  # pragma: no cover
    import httpx

    from dspace_sentinel.config import Config
    from dspace_sentinel.state import State
    from dspace_sentinel.store import Store


@dataclass
class CollectorContext:
    """Everything a collector may look at while collecting (read-only by convention).

    ``state`` gives access to the *current* health of other components, which the
    dependency rungs (``backend.dep.postgres`` …) mirror via ``health.dependency_rung``.
    """

    cfg: Config
    http: httpx.AsyncClient
    store: Store
    state: State
    now: float

    def dependency(self, component_id: str) -> Any:
        """Current :class:`ComponentHealth` of another component, or ``None``."""
        return self.state.components.get(component_id)


@dataclass
class CollectorResult:
    """What one ``collect()`` produced.

    * ``checks`` – the rungs of the component's chain, in ladder order;
    * ``metrics`` – component-specific numbers (see the SPEC table);
    * ``sample`` – only the system collector sets it;
    * ``repository_stats`` – optional snapshot persisted into ``repository_stats``
      (the repository collector sets it; the scheduler stores it);
    * ``history`` – extra ``checks`` rows kept over time for the reports, never part of the
      ladder (``postgres.connections``: connection usage in percent).
    """

    checks: list[CheckResult] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    sample: SystemSample | None = None
    repository_stats: dict[str, Any] | None = None
    history: list[CheckResult] = field(default_factory=list)


class Collector(ABC):
    """One module's collector. Subclasses set the class attributes and implement ``collect``.

    Attributes:
        id: module id and component id (``"postgres"``, ``"solr"`` …; ``"server"`` for the system collector).
        interval_key: which ``collector.*_interval`` schedules it (``"http_interval"`` …).
        label, emoji: display metadata (must match ``health.COMPONENTS``).
        depends_on: component ids whose status is mirrored by dependency rungs.
    """

    id: ClassVar[str] = ""
    interval_key: ClassVar[str] = "service_interval"
    label: ClassVar[str] = ""
    emoji: ClassVar[str] = ""
    depends_on: ClassVar[list[str]] = []
    budget_s: ClassVar[float] = 0.0
    """Worst case of one ``collect``: its rung timeouts in a row (systemd 3 s, TCP 2 s, HTTP and
    database 5 s) plus 5 s of slack. The timeout never goes below it, so a slow but working
    service is measured (and graded) instead of being cut off as a failed check."""

    @abstractmethod
    async def collect(self, ctx: CollectorContext) -> CollectorResult:
        """Run the checks once. May raise: the scheduler turns exceptions into an ``unknown`` rung."""

    def interval(self, cfg: Config) -> int:
        """Effective interval in seconds for this collector under ``cfg``."""
        return cfg.interval_for(self.interval_key)

    def timeout(self, cfg: Config) -> float:
        """Hard timeout for one ``collect``: ``interval - 1``, at least :attr:`budget_s` (and 2 s)."""
        return max(2.0, float(self.interval(cfg)) - 1.0, float(self.budget_s))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.__class__.__name__} id={self.id!r} every={self.interval_key}>"


# ------------------------------------------------------------------ blocking probes

T = TypeVar("T")


class ProbeStalled(RuntimeError):
    """The previous run of a blocking probe with the same key has not returned yet."""

    def __init__(self, key: str, since: float) -> None:
        self.key = key
        self.since = since
        waited = max(0.0, time.time() - since)
        super().__init__(f"{key} still blocked after {waited:.0f} s (a filesystem is not responding)")


@dataclass
class _Probe:
    started: float
    done: threading.Event


_probes: dict[str, _Probe] = {}
_probes_lock = threading.Lock()


def _deliver(waiter: asyncio.Future[Any], ok: bool, value: Any) -> None:
    if waiter.done():
        return
    if ok:
        waiter.set_result(value)
    else:
        waiter.set_exception(value)


async def run_blocking(key: str, fn: Callable[..., T], *args: Any, timeout: float | None = None) -> T:
    """Run blocking ``fn(*args)`` in a worker thread, never more than one call per ``key`` at a time.

    Filesystem calls (``statvfs``, ``stat``, reads) on a hard-mounted NFS/CIFS share whose server
    stopped answering block in the kernel and cannot be interrupted. Running them here keeps the
    event loop free, and the one-call-per-key rule stops a stuck call from being started again
    every cycle until the threads (for ``asyncio.to_thread``, the default executor the store and
    the reports also use) run out. The worker is a daemon thread, so a call that never returns
    cannot hold up the shutdown either.

    Raises:
        ProbeStalled: the previous call with this ``key`` is still running.
        asyncio.TimeoutError: this call did not return within ``timeout``; it goes on in its
            thread and later calls raise :class:`ProbeStalled` until it returns.
    """
    loop = asyncio.get_running_loop()
    waiter: asyncio.Future[T] = loop.create_future()
    with _probes_lock:
        previous = _probes.get(key)
        if previous is not None and not previous.done.is_set():
            raise ProbeStalled(key, previous.started)
        probe = _Probe(time.time(), threading.Event())
        _probes[key] = probe

    def worker() -> None:
        try:
            ok, value = True, fn(*args)
        except BaseException as exc:  # handed to the awaiting coroutine
            ok, value = False, exc
        probe.done.set()
        try:
            loop.call_soon_threadsafe(_deliver, waiter, ok, value)
        except RuntimeError:  # the loop is closed: nobody is waiting any more
            pass

    threading.Thread(target=worker, name=f"sentinel-probe:{key}", daemon=True).start()
    if timeout is None:
        return await waiter
    return await asyncio.wait_for(waiter, timeout)


__all__ = ["Collector", "CollectorContext", "CollectorResult", "ProbeStalled", "run_blocking"]
