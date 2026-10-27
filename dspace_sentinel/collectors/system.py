"""System collector: host vitals via psutil (SPEC section 6, "System collector details").

Every filesystem call (``statvfs`` of ``/`` and of the assetstore mount) runs in a worker
thread through :func:`~dspace_sentinel.collectors.base.run_blocking`: a hard-mounted NFS/CIFS
assetstore whose server stopped answering must not freeze the event loop. The assetstore mount
gets :data:`MOUNT_PROBE_TIMEOUT_S`; while its probe is stuck it is left out of the samples.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import platform
import socket
import time
from pathlib import Path
from typing import Any, ClassVar

import psutil

from dspace_sentinel.collectors.base import (
    Collector,
    CollectorContext,
    CollectorResult,
    ProbeStalled,
    run_blocking,
)
from dspace_sentinel.models import SystemSample

log = logging.getLogger(__name__)

MOUNT_PROBE_TIMEOUT_S = 3.0
"""How long one cycle waits for the assetstore mount's ``statvfs`` before leaving the mount out."""


def os_release() -> tuple[str, str]:
    """``(name, version)`` of the OS from ``platform.freedesktop_os_release`` or ``/etc/os-release``."""
    info: dict[str, str] = {}
    try:
        info = platform.freedesktop_os_release()
    except (OSError, AttributeError):
        try:
            for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.startswith("#"):
                    key, _, value = line.partition("=")
                    info[key.strip()] = value.strip().strip('"')
        except OSError:
            info = {}
    name = info.get("NAME") or platform.system() or "Linux"
    version = info.get("VERSION_ID") or info.get("VERSION") or platform.version().split()[0] or ""
    return name, version


def mount_point(path: str) -> str | None:
    """Mount point that holds ``path`` (walks up until ``os.path.ismount``); ``None`` if it does not exist."""
    try:
        p = Path(path).resolve()
    except OSError:
        return None
    if not p.exists():
        return None
    while not os.path.ismount(str(p)):
        if p.parent == p:
            break
        p = p.parent
    return str(p)


def inode_usage_pct(path: str) -> float | None:
    """Percent of inodes used on the filesystem holding ``path`` (``None`` when unavailable)."""
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    if st.f_files == 0:
        return None
    used = st.f_files - st.f_ffree
    return round(100.0 * used / st.f_files, 2)


def extra_mount_usage(assetstore_dir: str) -> dict[str, Any] | None:
    """Usage of the mount holding ``assetstore_dir`` when that is not ``/`` (blocking)."""
    mp = mount_point(assetstore_dir)
    if mp and mp != "/":
        return SystemCollector._disk(mp)
    return None


class SystemCollector(Collector):
    """Samples CPU, memory, swap, disks, load, network and uptime with psutil."""

    id: ClassVar[str] = "server"
    interval_key: ClassVar[str] = "system_interval"
    label: ClassVar[str] = "Server"
    emoji: ClassVar[str] = "💻"

    def __init__(self) -> None:
        # prime cpu_percent so the first real call returns a meaningful value
        try:
            psutil.cpu_percent(interval=None)
            psutil.cpu_percent(interval=None, percpu=True)
        except Exception:  # pragma: no cover
            pass
        self._last_net: tuple[float, int, int] | None = self._read_net()
        self._os_name, self._os_version = os_release()
        self._kernel = platform.release()
        self._stalled: set[str] = set()

    @staticmethod
    def _read_net() -> tuple[float, int, int] | None:
        try:
            counters = psutil.net_io_counters()
        except Exception:
            return None
        if counters is None:
            return None
        return time.monotonic(), int(counters.bytes_recv), int(counters.bytes_sent)

    def _net_rates(self) -> tuple[float, float]:
        current = self._read_net()
        if current is None:
            return 0.0, 0.0
        previous = self._last_net
        self._last_net = current
        if previous is None:
            return 0.0, 0.0
        elapsed = current[0] - previous[0]
        if elapsed <= 0:
            return 0.0, 0.0
        rx = max(0, current[1] - previous[1]) / elapsed
        tx = max(0, current[2] - previous[2]) / elapsed
        return round(rx, 1), round(tx, 1)

    @staticmethod
    def _disk(path: str) -> dict[str, Any] | None:
        try:
            usage = psutil.disk_usage(path)
        except (OSError, ValueError):
            return None
        return {
            "path": path,
            "total": int(usage.total),
            "used": int(usage.used),
            "free": int(usage.free),
            "pct": round(float(usage.percent), 2),
        }

    def sample(
        self,
        ctx: CollectorContext | None = None,
        now: float | None = None,
        *,
        disks: list[dict[str, Any]] | None = None,
    ) -> SystemSample:
        """Take one :class:`SystemSample` synchronously (blocking; also used by the CLI).

        ``disks`` are the extra mounts when the caller already measured them (``collect``
        probes the assetstore mount separately, under a timeout); ``None`` measures them here.
        """
        now = now or time.time()
        cpu = float(psutil.cpu_percent(interval=None))
        try:
            per_core = [float(v) for v in psutil.cpu_percent(interval=None, percpu=True)]
        except Exception:
            per_core = []
        vm = psutil.virtual_memory()
        sm = psutil.swap_memory()
        root = self._disk("/") or {"path": "/", "total": 0, "used": 0, "free": 0, "pct": 0.0}
        extra: list[dict[str, Any]] = list(disks) if disks is not None else []
        assetstore_dir = ctx.cfg.dspace.assetstore_dir if ctx is not None else ""
        if disks is None and assetstore_dir:
            d = extra_mount_usage(assetstore_dir)
            if d is not None:
                extra.append(d)
        try:
            load1, load5, load15 = psutil.getloadavg()
        except (OSError, AttributeError):
            load1 = load5 = load15 = 0.0
        cores = psutil.cpu_count(logical=True) or 1
        rx, tx = self._net_rates()
        try:
            uptime = max(0.0, now - psutil.boot_time())
        except Exception:
            uptime = 0.0
        cached = int(getattr(vm, "cached", 0) or 0) + int(getattr(vm, "buffers", 0) or 0)
        hostname = ctx.cfg.hostname if ctx is not None else socket.gethostname()
        return SystemSample(
            ts=now,
            cpu_pct=round(cpu, 2),
            cpu_per_core=per_core,
            mem_total=int(vm.total),
            mem_used=int(vm.total - vm.available),
            mem_available=int(vm.available),
            mem_pct=round(float(vm.percent), 2),
            mem_cached=cached,
            swap_total=int(sm.total),
            swap_used=int(sm.used),
            swap_pct=round(float(sm.percent), 2) if sm.total else 0.0,
            disk_total=root["total"],
            disk_used=root["used"],
            disk_free=root["free"],
            disk_pct=root["pct"],
            disk_path=root["path"],
            inode_pct=inode_usage_pct("/"),
            load1=round(float(load1), 2),
            load5=round(float(load5), 2),
            load15=round(float(load15), 2),
            cores=int(cores),
            net_rx_bps=rx,
            net_tx_bps=tx,
            uptime_s=round(uptime, 0),
            os_name=self._os_name,
            os_version=self._os_version,
            kernel=self._kernel,
            hostname=hostname,
            disks=extra,
        )

    async def _assetstore_mount(self, assetstore_dir: str) -> list[dict[str, Any]]:
        """The assetstore mount's usage (``[]`` when it is ``/``, missing, or not answering)."""
        if not assetstore_dir:
            return []
        try:
            usage = await run_blocking(
                f"statfs:{assetstore_dir}",
                extra_mount_usage,
                assetstore_dir,
                timeout=MOUNT_PROBE_TIMEOUT_S,
            )
        except (ProbeStalled, asyncio.TimeoutError):
            if assetstore_dir not in self._stalled:
                self._stalled.add(assetstore_dir)
                log.warning("the filesystem of %s is not responding; leaving it out", assetstore_dir)
            return []
        self._stalled.discard(assetstore_dir)
        return [usage] if usage is not None else []

    async def collect(self, ctx: CollectorContext) -> CollectorResult:
        """Return a result whose ``sample`` is the fresh :class:`SystemSample` (no chain).

        Raises :class:`ProbeStalled` while a previous sample is still blocked in the kernel, so
        the scheduler reports ``server.error`` instead of starting another stuck thread.
        """
        disks = await self._assetstore_mount(ctx.cfg.dspace.assetstore_dir)
        take = functools.partial(self.sample, ctx, ctx.now, disks=disks)
        sample = await run_blocking("system.sample", take)
        metrics = {
            "cpu_pct": sample.cpu_pct,
            "mem_pct": sample.mem_pct,
            "disk_pct": sample.disk_pct,
            "load1": sample.load1,
            "per_core_load": round(sample.load_per_core, 2),
            "uptime": sample.uptime_s,
            "os": f"{sample.os_name} {sample.os_version}".strip(),
            "kernel": sample.kernel,
        }
        return CollectorResult(checks=[], metrics=metrics, sample=sample)


__all__ = [
    "MOUNT_PROBE_TIMEOUT_S",
    "SystemCollector",
    "extra_mount_usage",
    "inode_usage_pct",
    "mount_point",
    "os_release",
]
