"""Shared fixtures: temp data dir, demo config, a TestClient running the app in demo mode."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from dspace_sentinel import discovery
from dspace_sentinel.api.app import create_app
from dspace_sentinel.config import Config, apply_demo_overrides
from dspace_sentinel.store import Store

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let the developer's environment leak into the tests."""
    for name in (
        "SENTINEL_CONFIG",
        "SENTINEL_DATA_DIR",
        "SENTINEL_HOST",
        "SENTINEL_PORT",
        "SENTINEL_DEMO",
        "SENTINEL_DEMO_FAST",
        "SENTINEL_TELEGRAM_TOKEN",
        "SENTINEL_TELEGRAM_CHAT_ID",
        "SENTINEL_SMTP_PASSWORD",
        "SENTINEL_PG_PASSWORD",
        "SENTINEL_AUTH_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)


@dataclass
class VersionProbe:
    """What discovery's version probe was asked (``calls``) and tried to send (``reached``)."""

    calls: list[tuple[str | None, str | None]] = field(default_factory=list)
    """``(server_url, solr_url)`` of every :func:`discovery.probe_versions` call (stubbed)."""
    reached: list[str] = field(default_factory=list)
    """Requests a real ``probe_versions`` tried to send with its own client (URLs without credentials)."""


@pytest.fixture(autouse=True)
def version_probe(monkeypatch: pytest.MonkeyPatch) -> Iterator[VersionProbe]:
    """No test asks a real DSpace or Solr for its version.

    Discovery sends two GETs where a backend or Solr seems to run (``<server_url>/api`` and
    ``<solr>/admin/info/system``), which in a test would reach whatever listens on the test
    machine's 8080 and 8983, credentials included. ``discovery.probe_versions`` is stubbed ("nothing
    answered", each call recorded), and its own HTTP client refuses every request: tests of the real
    function inject an ``httpx.MockTransport`` client, and one that does not fails at teardown.
    """
    probe = VersionProbe()

    async def no_versions(server_url: str | None, solr_url: str | None, **kwargs: Any) -> dict[str, Any]:
        probe.calls.append((server_url, solr_url))
        return {"dspace": None, "cris": None, "solr": None}

    def refuse(request: httpx.Request) -> httpx.Response:
        probe.reached.append(discovery.redact_url(str(request.url)))
        raise httpx.ConnectError("tests never ask a real service for its version", request=request)

    def no_network_client(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(refuse))

    monkeypatch.setattr(discovery, "probe_versions", no_versions)
    monkeypatch.setattr(discovery, "make_client", no_network_client)
    yield probe
    assert not probe.reached, f"discovery's version probe tried the network: {probe.reached}"


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """An empty runtime data directory."""
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    """Path for the live YAML config (does not exist yet)."""
    return tmp_path / "config" / "sentinel.yaml"


@pytest.fixture
def demo_cfg() -> Config:
    """Demo configuration: every module on, onboarding pending, hostname demo-dspace."""
    return apply_demo_overrides(Config())


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    """A file-backed store in a temp dir (WAL etc. like production)."""
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def mem_store() -> Iterator[Store]:
    """An in-memory store."""
    s = Store(":memory:")
    yield s
    s.close()


def make_client(cfg: Config, data_dir: Path, config_path: Path, **kwargs: object) -> TestClient:
    """Build a TestClient whose lifespan starts the scheduler (1 day of demo backfill for speed)."""
    kwargs.setdefault("demo_backfill_days", 1)
    app = create_app(cfg, config_path=config_path, data_dir=data_dir, **kwargs)  # type: ignore[arg-type]
    return TestClient(app)


@pytest.fixture
def client(demo_cfg: Config, data_dir: Path, config_path: Path) -> Iterator[TestClient]:
    """The app in demo mode, scheduler started, one cycle already run."""
    with make_client(demo_cfg, data_dir, config_path) as c:
        yield c


@pytest.fixture
def fixtures_dir() -> Path:
    """``tests/fixtures``."""
    return FIXTURES


@pytest.fixture
def no_systemd(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend systemctl is not installed (fast, deterministic discovery tests)."""
    from dspace_sentinel.util import systemd

    monkeypatch.setattr(systemd, "systemctl_path", lambda: None)
    monkeypatch.setattr(os, "environ", dict(os.environ))
