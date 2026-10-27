"""systemd helpers: ``systemctl is-active`` with a hard 3 s timeout; never raises.

Unit names come from the configuration, so every call ends option parsing with ``--`` before
them: a value such as ``--version`` or ``-M host`` is a (missing) unit, never a systemctl option.
"""

from __future__ import annotations

import asyncio
import shutil
from typing import Literal

UnitState = Literal["active", "inactive", "failed", "missing", "unknown"]

SUBPROCESS_TIMEOUT = 3.0

_KNOWN = {"active", "inactive", "failed", "activating", "deactivating", "reloading"}


def systemctl_path() -> str | None:
    """Path of ``systemctl`` or ``None`` when systemd is not installed."""
    return shutil.which("systemctl")


async def _run(*args: str, timeout: float = SUBPROCESS_TIMEOUT) -> tuple[int | None, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        except Exception:
            pass
        return None, "", "timeout"
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def is_active(unit: str, timeout: float = SUBPROCESS_TIMEOUT) -> UnitState:
    """State of a systemd unit: ``active`` | ``inactive`` | ``failed`` | ``missing`` | ``unknown``.

    ``missing`` = systemd knows no such unit (or systemctl is not installed);
    ``unknown`` = the command timed out or produced something unexpected.
    ``activating``/``reloading`` map to ``active``; ``deactivating`` to ``inactive``.
    """
    unit = unit.strip()
    if not unit:
        return "missing"
    binary = systemctl_path()
    if binary is None:
        return "missing"
    try:
        code, out, err = await _run(binary, "is-active", "--", unit, timeout=timeout)
    except (OSError, ValueError):
        return "unknown"
    if code is None:
        return "unknown"
    state = out.strip().splitlines()[0].strip() if out.strip() else ""
    if state in ("activating", "reloading"):
        return "active"
    if state == "deactivating":
        return "inactive"
    if state in ("active", "failed"):
        return state  # type: ignore[return-value]
    if state == "inactive":
        # `inactive` is also what systemctl prints for a unit file that does not exist.
        return "missing" if not await unit_exists(unit, binary, timeout) else "inactive"
    if "not-found" in state or "could not be found" in err.lower():
        return "missing"
    return "unknown"


async def unit_exists(unit: str, binary: str | None = None, timeout: float = SUBPROCESS_TIMEOUT) -> bool:
    """True when systemd has a unit file (or loaded unit) named ``unit``."""
    binary = binary or systemctl_path()
    if binary is None:
        return False
    try:
        code, out, _ = await _run(
            binary, "show", "--property=LoadState", "--value", "--", unit, timeout=timeout
        )
    except (OSError, ValueError):
        return False
    if code is None:
        return False
    return out.strip() not in ("", "not-found", "masked", "bad-setting")


async def list_units(pattern: str, timeout: float = SUBPROCESS_TIMEOUT) -> list[tuple[str, str]]:
    """``[(unit_name, active_state)]`` for units matching a glob such as ``postgresql@*``."""
    binary = systemctl_path()
    if binary is None:
        return []
    try:
        code, out, _ = await _run(
            binary,
            "list-units",
            "--type=service",
            "--all",
            "--no-legend",
            "--plain",
            "--no-pager",
            "--",
            pattern,
            timeout=timeout,
        )
    except (OSError, ValueError):
        return []
    if code is None:
        return []
    units: list[tuple[str, str]] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].endswith(".service"):
            units.append((parts[0][: -len(".service")], parts[2]))
    return units


__all__ = ["SUBPROCESS_TIMEOUT", "UnitState", "is_active", "list_units", "systemctl_path", "unit_exists"]
