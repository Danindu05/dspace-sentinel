"""Filesystem locations used by Sentinel.

Every location can be overridden through environment variables so the same code
serves a git checkout, a systemd deployment and an isolated test run:

* ``SENTINEL_DATA_DIR``  – runtime data (SQLite DB, reports, log, pid); default ``<repo>/data``
* ``SENTINEL_CONFIG``    – the live YAML config; default ``<repo>/config/sentinel.yaml``

The module-level constants are evaluated at import time for convenience; the
functions read the environment every call and are what the application uses.
"""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_DIR: Path = Path(__file__).resolve().parent
REPO_DIR: Path = PACKAGE_DIR.parent
STATIC_DIR: Path = PACKAGE_DIR / "static"
EXAMPLE_CONFIG_PATH: Path = REPO_DIR / "config" / "sentinel.example.yaml"


def data_dir() -> Path:
    """Return the runtime data directory (``SENTINEL_DATA_DIR`` or ``<repo>/data``)."""
    raw = os.environ.get("SENTINEL_DATA_DIR", "").strip()
    return Path(raw).expanduser() if raw else REPO_DIR / "data"


def config_path() -> Path:
    """Return the live config file path (``SENTINEL_CONFIG`` or ``<repo>/config/sentinel.yaml``)."""
    raw = os.environ.get("SENTINEL_CONFIG", "").strip()
    return Path(raw).expanduser() if raw else REPO_DIR / "config" / "sentinel.yaml"


def demo_config_path(base: Path | None = None) -> Path:
    """Return the YAML file a demo session saves its settings to (``<data dir>/demo-sentinel.yaml``).

    Demo mode never writes ``config/sentinel.yaml``: onboarding, thresholds and the theme chosen
    while the demo runs live next to the demo database, so a restarted demo skips onboarding and
    the real configuration stays untouched. Delete the file to see onboarding again.
    """
    return (base or data_dir()) / "demo-sentinel.yaml"


def reports_dir(base: Path | None = None) -> Path:
    """Return the directory generated reports are written to."""
    return (base or data_dir()) / "reports"


def db_path(base: Path | None = None) -> Path:
    """Return the SQLite database path."""
    return (base or data_dir()) / "sentinel.db"


def log_path(base: Path | None = None) -> Path:
    """Return the application log path used when running detached."""
    return (base or data_dir()) / "sentinel.log"


def pid_path(base: Path | None = None) -> Path:
    """Return the pid file path used by ``sentinel.sh`` when no systemd unit is installed."""
    return (base or data_dir()) / "sentinel.pid"


def ensure_dirs(base: Path | None = None) -> Path:
    """Create the data and reports directories (mode 0700) and return the data dir."""
    base = base or data_dir()
    base.mkdir(parents=True, exist_ok=True)
    reports_dir(base).mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(base, 0o700)
    except OSError:
        pass
    return base


DATA_DIR: Path = data_dir()
CONFIG_PATH: Path = config_path()
REPORTS_DIR: Path = reports_dir(DATA_DIR)
DB_PATH: Path = db_path(DATA_DIR)

__all__ = [
    "CONFIG_PATH",
    "DATA_DIR",
    "DB_PATH",
    "EXAMPLE_CONFIG_PATH",
    "PACKAGE_DIR",
    "REPORTS_DIR",
    "REPO_DIR",
    "STATIC_DIR",
    "config_path",
    "data_dir",
    "db_path",
    "demo_config_path",
    "ensure_dirs",
    "log_path",
    "pid_path",
    "reports_dir",
]
