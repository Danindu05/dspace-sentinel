"""Discovery: properties parsing, interpolation, precedence, JDBC parsing, discover() on fixtures."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from dspace_sentinel import discovery
from dspace_sentinel.config import Config, merge_patch
from dspace_sentinel.util.net import ListeningSocket

REAL_PROBE_VERSIONS = discovery.probe_versions
"""The real function (taken before tests/conftest.py's ``version_probe`` stubs it for every test)."""
REAL_PROCESS_CMDLINES = discovery.process_cmdlines
"""The real process list reader (taken before this module's ``solr_processes`` stubs it for every test)."""
LIVE = Path(__file__).parent / "fixtures" / "dspace-live"
NO_VERSIONS: dict[str, Any] = {"dspace": None, "cris": None, "solr": None}
SERVICE_LOGS = (discovery.SOLR_LOG, discovery.NGINX_LOG)
"""The default Solr and nginx log entries, which discovery keeps only where they are."""


@pytest.fixture(autouse=True)
def solr_processes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """No test reads this machine's process list: discovery sees no running Solr unless a test passes
    ``cmdlines``. Every look is recorded."""
    looks: list[str] = []

    def no_solr() -> list[list[str]]:
        looks.append("process list")
        return []

    monkeypatch.setattr(discovery, "process_cmdlines", no_solr)
    return looks


def on_disk(*paths: str) -> Callable[[str], bool]:
    """A ``file_exists`` that finds ``paths`` and nothing else."""
    return lambda path: path in paths


def test_parse_properties_and_interpolation() -> None:
    props = discovery.parse_properties(
        '# comment\n! bang comment\nkey = value\ncolon: v2\nquoted = "q"\n'
        "list = a, \\\n   b\nx += 1\nx += 2\n"
        "dir = /d\npath = ${dir}/sub\ndeep = ${path}/x\nmissing = ${nope}\n"
    )
    assert props["key"] == "value" and props["colon"] == "v2" and props["quoted"] == "q"
    assert props["list"] == "a, b"
    assert props["x"] == "1,2"
    out = discovery.interpolate(props)
    assert out["path"] == "/d/sub" and out["deep"] == "/d/sub/x" and out["missing"] == "${nope}"


def test_parse_jdbc_url() -> None:
    assert discovery.parse_jdbc_url("jdbc:postgresql://127.0.0.1:5433/dspace8?sslmode=prefer") == {
        "host": "127.0.0.1",
        "port": 5433,
        "database": "dspace8",
        "sslmode": "prefer",
    }
    plain = discovery.parse_jdbc_url("jdbc:postgresql://db.internal/dspace")
    assert plain == {"host": "db.internal", "port": 5432, "database": "dspace"}
    assert "sslmode" not in plain
    secure = discovery.parse_jdbc_url("jdbc:postgresql://db.internal:5432/dspace?ssl=true&sslmode=require")
    assert secure["sslmode"] == "require" and secure["database"] == "dspace"
    assert discovery.parse_jdbc_url("jdbc:oracle:thin:@x") is None


def test_load_dspace_config_precedence(fixtures_dir: Path) -> None:
    props, sources = discovery.load_dspace_config(fixtures_dir / "dspace-full")
    assert [Path(s).name for s in sources] == ["local.cfg", "dspace.cfg"]
    assert props["dspace.dir"] == "/opt/dspace"  # local.cfg wins
    assert props["assetstore.dir"] == "/opt/dspace/assetstore"  # ${dspace.dir} resolved with local value
    assert props["log.report.dir"] == "/opt/dspace/log"
    assert props["handle.prefix"] == "123456789"  # dspace.cfg fills in missing keys
    assert props["db.username"] == "dspace_ro"
    assert props["event.dispatcher.default.consumers"] == "versioning, discovery, eperson, harvester"


def test_load_dspace_config_no_local(fixtures_dir: Path) -> None:
    install = fixtures_dir / "dspace-nolocal"
    props, sources = discovery.load_dspace_config(install)
    assert len(sources) == 1
    assert props["dspace.dir"] == str(install)
    assert props["assetstore.dir"] == f"{install}/assetstore"
    assert props["solr.server"] == "http://solr.internal:8983/solr"
    assert discovery.load_dspace_config("/definitely/not/here") == ({}, [])


async def test_discover_full_stack(fixtures_dir: Path, no_systemd: None) -> None:
    cfg = merge_patch(Config(), {"dspace": {"install_dir": str(fixtures_dir / "dspace-full")}})
    sockets = [
        ListeningSocket(5433, "127.0.0.1", False, "ipv4"),
        ListeningSocket(8983, "127.0.0.1", False, "ipv4"),
        ListeningSocket(8080, "0.0.0.0", True, "ipv4"),
        ListeningSocket(4000, "::", True, "ipv6"),
        ListeningSocket(443, "0.0.0.0", True, "ipv4"),
        ListeningSocket(443, "::", True, "ipv6"),
    ]
    units = {
        "tomcat9": "active",
        "dspace-ui": "active",
        "postgresql@15-main": "active",
        "nginx": "active",
        "solr": "inactive",
    }
    result = await discovery.discover(cfg, sockets=sockets, unit_states=units)
    patch = result.suggested_config
    assert result.detected_persona == "full"
    assert patch["postgres"] == {
        "host": "127.0.0.1",
        "port": 5433,
        "database": "dspace8",
        "sslmode": "prefer",
        "user": "dspace_ro",
        "password": "s3cret",
        "service": "postgresql@15-main",
    }
    assert patch["solr"] == {"url": "http://127.0.0.1:8983/solr", "service": "solr"}
    assert patch["dspace"]["install_dir"] == "/opt/dspace"
    assert patch["dspace"]["server_url"] == "https://repo.example.org/server"
    assert patch["dspace"]["public_url"] == "https://repo.example.org"
    assert patch["dspace"]["assetstore_dir"] == "/opt/dspace/assetstore"
    assert patch["dspace"]["log_dir"] == "/opt/dspace/log"
    assert patch["dspace"]["backend_service"] == "tomcat9"
    assert patch["dspace"]["frontend_service"] == "dspace-ui"
    assert patch["ssl"]["host"] == "repo.example.org"
    assert patch["nginx"] == {"https_url": "https://repo.example.org", "service": "nginx"}
    assert patch["logs"]["files"][0]["path"] == "/opt/dspace/log/dspace.log"
    assert patch["modules"]["postgres"] and patch["modules"]["solr"] and patch["modules"]["frontend"]
    assert any("Test Repository" in f for f in result.findings)
    assert any(
        f.startswith("Found DSpace backend (Tomcat) on 0.0.0.0:8080 (exposed publicly)")
        for f in result.findings
    )
    assert any("8983 (local only)" in f for f in result.findings)
    assert [p["port"] for p in result.ports] == [443, 4000, 5433, 8080, 8983]
    assert result.ports[0]["addrs"] == ["0.0.0.0", "::"]
    # the patch is a valid config patch
    merged = merge_patch(cfg, patch)
    assert merged.postgres.port == 5433 and merged.modules.backend is True
    # to_dict never returns the password: not in properties, masked in the suggestion
    shown = result.to_dict()
    assert "db.password" not in shown["properties"] and shown["properties"]["db.username"] == "dspace_ro"
    assert shown["suggested_config"]["postgres"]["password"] == "••••••"


async def test_discover_nothing_found(tmp_path: Path, no_systemd: None) -> None:
    cfg = merge_patch(Config(), {"dspace": {"install_dir": str(tmp_path)}})
    result = await discovery.discover(
        cfg, sockets=[ListeningSocket(22, "0.0.0.0", True, "ipv4")], unit_states={}
    )
    assert result.detected_persona == "server"
    assert result.suggested_config["modules"]["postgres"] is False
    assert result.suggested_config["modules"]["ports"] is True
    # SECURITY-CONTRACT: the Security module reads this server, whatever runs on it
    assert result.suggested_config["modules"]["security"] is True
    assert "dspace" not in result.suggested_config
    assert any("No DSpace configuration found" in f for f in result.findings)


async def test_discover_auto_discover_off(fixtures_dir: Path, no_systemd: None) -> None:
    cfg = merge_patch(
        Config(), {"dspace": {"install_dir": str(fixtures_dir / "dspace-full"), "auto_discover": False}}
    )
    result = await discovery.discover(
        cfg, sockets=[ListeningSocket(4000, "127.0.0.1", False, "ipv4")], unit_states={}
    )
    assert result.sources == [] and result.properties == {}
    assert result.detected_persona == "frontend"


async def test_probe_units_without_systemd(no_systemd: None) -> None:
    assert await discovery.probe_units() == {}


@pytest.mark.parametrize(
    ("addr", "public"),
    [
        ("0.0.0.0", True),
        ("::", True),
        ("127.0.0.1", False),
        ("::1", False),
        ("10.0.0.5", True),
        ("*", True),
        # an IPv4-mapped address counts as the IPv4 address it maps (T20)
        ("::ffff:127.0.0.1", False),
        ("::ffff:127.0.1.1", False),
        ("::ffff:10.0.0.5", True),
        ("::ffff:0.0.0.0", True),
    ],
)
def test_is_public_address(addr: str, public: bool) -> None:
    from dspace_sentinel.util.net import is_public_address

    assert is_public_address(addr) is public


async def test_demo_discovery_describes_the_demo_stack_not_this_machine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SPEC §13: the demo swaps every probe; discovery must not report (or apply) the real host."""
    from dspace_sentinel.config import apply_demo_overrides, persisted_dict

    def host_probe(*args: object, **kwargs: object) -> None:
        raise AssertionError("the real host was inspected in demo mode")

    monkeypatch.setattr(discovery, "listening_sockets", host_probe)
    monkeypatch.setattr(discovery, "probe_units", host_probe)
    monkeypatch.setattr(discovery, "load_dspace_config", host_probe)
    monkeypatch.setattr(discovery, "probe_versions", host_probe)
    monkeypatch.setattr(discovery, "_file_exists", host_probe)
    monkeypatch.setattr(discovery, "_may_exist", host_probe)
    monkeypatch.setattr(discovery, "process_cmdlines", host_probe)
    cfg = merge_patch(apply_demo_overrides(Config()), {"dspace": {"install_dir": "/somewhere/else"}})
    result = await discovery.discover(cfg)
    assert result.detected_persona == "full"
    assert result.findings[0].startswith("Demo mode")
    assert any("Demo Institutional Repository" in f for f in result.findings)
    assert [p["port"] for p in result.ports] == [22, 80, 443, 4000, 5432, 8080, 8983]
    assert {p["port"] for p in result.ports if not p["public"]} == {5432, 8983}
    assert result.services == {
        "postgresql": "active",
        "solr": "active",
        "tomcat10": "active",
        "dspace-ui": "active",
        "nginx": "active",
    }
    assert "DSpace 9.4 answers at http://localhost:8080/server" in result.findings
    assert "Solr 9.6.1 answers at http://localhost:8983/solr" in result.findings
    patch = result.suggested_config
    assert patch["dspace"]["install_dir"] == "/dspace" and patch["nginx"]["service"] == "nginx"
    assert "password" not in patch.get("postgres", {})
    assert all(patch["modules"].values())
    # "Start monitoring" with it saves only demo facts (a DSpace 9.4 stack runs Tomcat 10), no host facts
    applied = merge_patch(cfg, {k: v for k, v in patch.items() if k != "modules"})
    saved = persisted_dict(applied)
    assert saved["dspace"] == {"backend_service": "tomcat10"}
    assert [f["path"] for f in saved["logs"]["files"]] == [
        "/dspace/log/dspace.log",
        "/var/log/tomcat10/catalina.out",
        "/var/solr/logs/solr.log",
        "/var/log/nginx/error.log",
    ]
    assert set(saved) == {"dspace", "logs"}


async def test_discovery_keeps_the_other_log_files(tmp_path: Path, no_systemd: None) -> None:
    """Stock dspace.cfg sets log.report.dir: the suggestion must not drop Tomcat, nor the Solr and
    nginx logs that are there."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "dspace.cfg").write_text(
        f"dspace.dir = {tmp_path}\n"
        "db.url = jdbc:postgresql://localhost:5432/dspace\n"
        "solr.server = http://localhost:8983/solr\n"
        "log.report.dir = ${dspace.dir}/log\n"
    )
    cfg = merge_patch(Config(), {"dspace": {"install_dir": str(tmp_path)}})
    result = await discovery.discover(cfg, sockets=[], unit_states={}, file_exists=on_disk(*SERVICE_LOGS))
    files = result.suggested_config["logs"]["files"]
    assert [f["id"] for f in files] == ["dspace", "tomcat", "solr", "nginx"]
    merged = merge_patch(cfg, {k: v for k, v in result.suggested_config.items() if k != "modules"})
    assert [(f.id, f.path) for f in merged.logs.files] == [
        ("dspace", f"{tmp_path}/log/dspace.log"),
        ("tomcat", "/var/log/tomcat9/catalina.out"),
        ("solr", "/var/solr/logs/solr.log"),
        ("nginx", "/var/log/nginx/error.log"),
    ]
    assert merged.logs.files[0].daily_suffix is True
    # custom entries survive; a list without a DSpace entry gets one in front
    web_only = [{"id": "nginx", "label": "Web", "path": "/srv/nginx.log"}]
    custom = merge_patch(cfg, {"logs": {"files": web_only}})
    files = (await discovery.discover(custom, sockets=[], unit_states={})).suggested_config["logs"]["files"]
    assert [(f["id"], f["label"], f["path"]) for f in files] == [
        ("dspace", "DSpace", f"{tmp_path}/log/dspace.log"),
        ("nginx", "Web", "/srv/nginx.log"),
    ]


def stock_install(root: Path, extra: str = "") -> Path:
    """A DSpace install whose dspace.cfg sets log.report.dir (plus ``extra`` lines)."""
    (root / "config").mkdir(parents=True)
    (root / "config" / "dspace.cfg").write_text(
        f"dspace.dir = {root}\n"
        "db.url = jdbc:postgresql://localhost:5432/dspace\n"
        "solr.server = http://localhost:8983/solr\n"
        "log.report.dir = ${dspace.dir}/log\n" + extra
    )
    return root


def nothing_on_disk(path: str) -> bool:
    return False


@pytest.mark.parametrize(
    ("units", "backend_unit", "frontend_unit"),
    [
        ({"tomcat10": "active", "pm2-dspace": "active"}, "tomcat10", "pm2-dspace"),  # Ubuntu 24.04 + PM2
        (
            {"tomcat9": "inactive", "tomcat10": "active"},
            "tomcat10",
            None,
        ),  # 7 → 8 upgrade: tomcat9 left behind
        ({"tomcat11": "active"}, "tomcat11", None),  # Debian 13
        ({"dspace-backend": "active", "dspace-angular": "active"}, "dspace-backend", "dspace-angular"),
        ({"dspace-server": "active", "dspace-frontend": "active"}, "dspace-server", "dspace-frontend"),
        ({"dspace": "active", "dspace-ui": "active"}, "dspace", "dspace-ui"),
        ({"dspace-ui": "active"}, None, "dspace-ui"),  # a UI unit is never taken for the backend
        (
            {"tomcat9": "active", "dspace-sentinel": "active"},
            "tomcat9",
            None,
        ),  # Sentinel itself is no DSpace unit
    ],
)
async def test_discovery_finds_the_units_dspace_8_and_9_use(
    no_systemd: None, units: dict[str, str], backend_unit: str | None, frontend_unit: str | None
) -> None:
    result = await discovery.discover(Config(), sockets=[], unit_states=units, file_exists=nothing_on_disk)
    dspace = result.suggested_config.get("dspace", {})
    assert dspace.get("backend_service") == backend_unit
    assert dspace.get("frontend_service") == frontend_unit
    assert "dspace-sentinel" not in result.services
    pm2 = [f for f in result.findings if "PM2" in f]
    if frontend_unit and frontend_unit.startswith("pm2-"):
        assert pm2 == [
            f"{frontend_unit} is PM2's own unit: it only proves the PM2 daemon runs (the homepage check "
            "shows whether the DSpace UI answers)"
        ]
    else:
        assert pm2 == []


def test_unit_candidates_cover_dspace_8_and_9() -> None:
    for unit in (
        "tomcat9",
        "tomcat10",
        "tomcat11",
        "dspace-backend",
        "dspace-server",
        "dspace-angular",
        "pm2-*",
    ):
        assert unit in discovery.UNIT_CANDIDATES


@pytest.mark.parametrize(
    ("units", "sockets", "tomcat_path", "jar_finding"),
    [
        ({"tomcat9": "active"}, [], "/var/log/tomcat9/catalina.out", False),  # Ubuntu 22.04 (DSpace 7)
        ({"tomcat10": "active"}, [], "/var/log/tomcat10/catalina.out", False),  # Ubuntu 24.04, Debian 12
        ({"dspace-backend": "active"}, [], None, True),  # runnable JAR (DSpace 8+)
        ({}, [ListeningSocket(8080, "127.0.0.1", False, "ipv4")], None, True),  # a JAR in a container
        ({"tomcat": "active"}, [], "/var/log/tomcat9/catalina.out", False),  # RHEL/manual: left as it is
    ],
)
async def test_discovery_tomcat_log_follows_the_runtime(
    tmp_path: Path,
    no_systemd: None,
    units: dict[str, str],
    sockets: list[ListeningSocket],
    tomcat_path: str | None,
    jar_finding: bool,
) -> None:
    cfg = merge_patch(Config(), {"dspace": {"install_dir": str(stock_install(tmp_path))}})
    there = on_disk(*SERVICE_LOGS)  # Solr's and nginx's logs are there; no catalina.out is
    result = await discovery.discover(cfg, sockets=sockets, unit_states=units, file_exists=there)
    files = {f["id"]: f["path"] for f in result.suggested_config["logs"]["files"]}
    assert files.get("tomcat") == tomcat_path
    assert files["dspace"] == f"{tmp_path}/log/dspace.log" and "solr" in files and "nginx" in files
    jar = [f for f in result.findings if f.startswith("No Tomcat unit")]
    assert bool(jar) is jar_finding
    if jar_finding:
        assert jar[0] == (
            "No Tomcat unit: the Tomcat log (/var/log/tomcat9/catalina.out) is left out; the runnable "
            "JAR of DSpace 8+ writes its Tomcat messages to dspace.log"
        )


async def test_discovery_keeps_a_tomcat_log_that_exists_or_is_custom(
    tmp_path: Path, no_systemd: None
) -> None:
    cfg = merge_patch(Config(), {"dspace": {"install_dir": str(stock_install(tmp_path))}})
    # a packaged catalina.out that is really there stays, even without a Tomcat unit
    there = await discovery.discover(
        cfg,
        sockets=[],
        unit_states={"dspace": "active"},
        file_exists=lambda p: p.endswith("catalina.out") or p in SERVICE_LOGS,
    )
    assert {f["id"] for f in there.suggested_config["logs"]["files"]} == {"dspace", "tomcat", "solr", "nginx"}
    # a custom Tomcat path is never rewritten
    custom = merge_patch(
        cfg,
        {"logs": {"files": [{"id": "tomcat", "label": "Tomcat", "path": "/opt/tomcat/logs/catalina.out"}]}},
    )
    result = await discovery.discover(
        custom, sockets=[], unit_states={"tomcat10": "active"}, file_exists=nothing_on_disk
    )
    files = {f["id"]: f["path"] for f in result.suggested_config["logs"]["files"]}
    assert files["tomcat"] == "/opt/tomcat/logs/catalina.out"


async def test_discovery_suggests_the_cli_log_only_when_it_exists(tmp_path: Path, no_systemd: None) -> None:
    """DSpace 7.6.3+ / 8.1+ scripts (cron jobs) log to dspace-cli.log, not dspace.log."""
    install = stock_install(tmp_path)
    cfg = merge_patch(Config(), {"dspace": {"install_dir": str(install)}})

    def there(path: str) -> bool:  # this install's files, and Solr's and nginx's logs
        return path in SERVICE_LOGS or Path(path).is_file()

    without = await discovery.discover(cfg, sockets=[], unit_states={}, file_exists=there)
    assert "dspace-cli" not in [f["id"] for f in without.suggested_config["logs"]["files"]]
    (install / "log").mkdir()
    (install / "log" / "dspace-cli.log").write_text("x\n")
    result = await discovery.discover(cfg, sockets=[], unit_states={}, file_exists=there)
    files = result.suggested_config["logs"]["files"]
    assert [f["id"] for f in files] == ["dspace", "dspace-cli", "tomcat", "solr", "nginx"]
    assert files[1] == {
        "id": "dspace-cli",
        "label": "DSpace CLI",
        "path": f"{install}/log/dspace-cli.log",
        "daily_suffix": True,
        "timezone": "",
    }
    assert all(set(entry) == set(files[0]) for entry in files)  # every entry carries the same keys
    merged = merge_patch(cfg, {"logs": result.suggested_config["logs"]})
    assert merged.logs.files[1].id == "dspace-cli"
    # once configured it is not added twice
    again = await discovery.discover(merged, sockets=[], unit_states={}, file_exists=there)
    assert [f["id"] for f in again.suggested_config["logs"]["files"]].count("dspace-cli") == 1


def test_suggested_log_entries_carry_every_key() -> None:
    """A dspace entry discovery adds to a list without one looks like the configured entries."""
    nginx = Config().logs.files[-1].model_dump(mode="json")
    props = {"dspace.dir": "/dspace", "log.report.dir": "/dspace/log"}
    patch, _ = discovery.suggest_from_properties(props, "/dspace", [nginx], cli_log=True)
    files = patch["logs"]["files"]
    assert [f["id"] for f in files] == ["dspace", "dspace-cli", "nginx"]
    assert files[0] == {
        "id": "dspace",
        "label": "DSpace",
        "path": "/dspace/log/dspace.log",
        "daily_suffix": True,
        "timezone": "",
    }
    assert all(set(entry) == set(nginx) for entry in files)


def test_load_dspace_config_reads_the_assetstore_module(fixtures_dir: Path) -> None:
    """DSpace 7-10 keep assetstore.dir's default in config/modules/assetstore.cfg (lowest precedence)."""
    props, sources = discovery.load_dspace_config(fixtures_dir / "dspace9-jar")
    assert [Path(s).name for s in sources] == ["local.cfg", "dspace.cfg", "assetstore.cfg"]
    assert props["assetstore.dir"] == "/opt/dspace/assetstore"  # ${dspace.dir} from local.cfg
    assert props["assetstore.index.primary"] == "0"
    assert props["solr.multicorePrefix"] == "dspace_"  # local.cfg wins over dspace.cfg's empty value
    # local.cfg and dspace.cfg still win over the module file
    props, _ = discovery.load_dspace_config(fixtures_dir / "dspace-full")
    assert props["assetstore.dir"] == "/opt/dspace/assetstore"


async def test_discovery_on_a_dspace_9_jar_host(fixtures_dir: Path, no_systemd: None) -> None:
    cfg = merge_patch(Config(), {"dspace": {"install_dir": str(fixtures_dir / "dspace9-jar")}})
    result = await discovery.discover(
        cfg,
        sockets=[ListeningSocket(8080, "127.0.0.1", False, "ipv4")],
        unit_states={"dspace-backend": "active", "pm2-dspace": "active"},
        file_exists=on_disk(*SERVICE_LOGS),
    )
    patch = result.suggested_config
    assert patch["dspace"]["assetstore_dir"] == "/opt/dspace/assetstore"
    assert patch["modules"]["assetstore"] is True
    assert patch["solr"]["cores"] == ["dspace_search", "dspace_statistics", "dspace_oai", "dspace_authority"]
    assert "Solr core names start with dspace_ (solr.multicorePrefix)" in result.findings
    assert patch["dspace"]["backend_service"] == "dspace-backend"
    assert patch["dspace"]["frontend_service"] == "pm2-dspace"
    assert [f["id"] for f in patch["logs"]["files"]] == ["dspace", "solr", "nginx"]
    shown = result.to_dict()["properties"]
    assert shown["solr.multicorePrefix"] == "dspace_" and shown["assetstore.index.primary"] == "0"
    merged = merge_patch(cfg, {k: v for k, v in patch.items() if k != "modules"})
    assert merged.solr.cores[0] == "dspace_search"


async def test_discovery_leaves_an_s3_assetstore_alone(tmp_path: Path, no_systemd: None) -> None:
    install = stock_install(tmp_path)
    (install / "config" / "modules").mkdir()
    (install / "config" / "modules" / "assetstore.cfg").write_text(
        "assetstore.dir = ${dspace.dir}/assetstore\nassetstore.index.primary = 1\n"
    )
    cfg = merge_patch(Config(), {"dspace": {"install_dir": str(install)}})
    result = await discovery.discover(cfg, sockets=[], unit_states={})
    patch = result.suggested_config
    assert "assetstore_dir" not in patch["dspace"] and patch["modules"]["assetstore"] is False
    assert (
        "The assetstore is on Amazon S3 (assetstore.index.primary = 1): there is no local directory to "
        "watch, so the Assetstore module stays off" in result.findings
    )


@pytest.mark.parametrize(
    ("dspace", "cris", "solr", "expected"),
    [
        (
            "DSpace 7.6.6",
            None,
            "8.11.4",
            [
                "DSpace 7.6.6 answers at http://localhost:8080/server",
                "DSpace 7.x is end-of-life (7.6.7 was the final release; supported: 8.x, 9.x, 10.x)",
                "Solr 8.11.4 answers at http://localhost:8983/solr",
            ],
        ),
        (
            "DSpace 9.4",
            None,
            "8.11.4",
            [
                "DSpace 9.4 answers at http://localhost:8080/server",
                "Solr 8.11.4 answers at http://localhost:8983/solr",
                "DSpace 9 expects Solr 9 but Solr 8.11.4 answers",
            ],
        ),
        (
            "DSpace 8.5",
            None,
            "9.8.1",
            [
                "DSpace 8.5 answers at http://localhost:8080/server",
                "Solr 9.8.1 answers at http://localhost:8983/solr",
                "DSpace 8 expects Solr 8 but Solr 9.8.1 answers",
            ],
        ),
        (
            "DSpace 8.2",
            "cris-2024.02.04",
            None,
            ["DSpace 8.2 (DSpace-CRIS cris-2024.02.04) answers at http://localhost:8080/server"],
        ),
        (
            "DSpace 10.1",
            None,
            "9.8.1",
            [
                "DSpace 10.1 answers at http://localhost:8080/server",
                "Solr 9.8.1 answers at http://localhost:8983/solr",
            ],
        ),
        (
            "DSpace 11.0-SNAPSHOT",
            None,
            "9.8.1",
            [
                "DSpace 11.0-SNAPSHOT answers at http://localhost:8080/server",
                "Solr 9.8.1 answers at http://localhost:8983/solr",
            ],
        ),
        ("DSpace unknown", None, None, []),
        (None, None, None, []),
    ],
)
def test_version_findings(
    dspace: str | None, cris: str | None, solr: str | None, expected: list[str]
) -> None:
    versions = {"dspace": dspace, "cris": cris, "solr": solr}
    found = discovery.version_findings(
        versions,
        server_url="http://localhost:8080/server",
        solr_url="http://solradmin:pw@localhost:8983/solr",
    )
    assert found == expected


async def test_probe_versions_reads_the_rest_root_and_solr() -> None:
    roots = json.loads((LIVE / "rest-roots.json").read_text())
    asked: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        if request.url.path == "/server/api":
            return httpx.Response(200, json=roots["8.2-cris"])
        if request.url.path == "/solr/admin/info/system":
            return httpx.Response(200, json={"lucene": {"solr-spec-version": "9.8.1"}})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        found = await REAL_PROBE_VERSIONS(
            "http://localhost:8080/server/", "http://localhost:8983/solr", client=client
        )
        assert found == {"dspace": "DSpace 8.2", "cris": "cris-2024.02.04", "solr": "9.8.1"}
        assert [r.method for r in asked] == ["GET", "GET"]
        assert await REAL_PROBE_VERSIONS(None, None, client=client) == NO_VERSIONS
        assert len(asked) == 2  # nothing to ask, nothing asked

    def broken(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/server/api":
            return httpx.Response(200, text="<html>Tomcat</html>")
        raise httpx.ConnectError("Connection refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(broken)) as client:
        found = await REAL_PROBE_VERSIONS(
            "http://localhost:8080/server", "http://localhost:8983/solr", client=client
        )
        assert found == NO_VERSIONS


async def test_no_test_reaches_the_network_to_ask_for_versions(
    version_probe: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The suite's guard (tests/conftest.py): discovery's version probe is stubbed in every test, and a
    real probe_versions without an injected client gets a transport that refuses and records it."""
    import socket

    connects: list[Any] = []

    def no_socket(self: socket.socket, address: Any) -> None:
        connects.append(address)
        raise OSError("no sockets in this test")

    monkeypatch.setattr(socket.socket, "connect", no_socket)
    assert discovery.probe_versions is not REAL_PROBE_VERSIONS
    found = await REAL_PROBE_VERSIONS(
        "http://localhost:8080/server", "http://solradmin:pw@127.0.0.1:8983/solr"
    )
    assert found == NO_VERSIONS and connects == []
    assert sorted(version_probe.reached) == [
        "http://127.0.0.1:8983/solr/admin/info/system",  # the credentials never reach the report
        "http://localhost:8080/server/api",
    ]
    version_probe.reached.clear()  # tripped on purpose: the teardown check would fail this test otherwise


async def test_discover_in_tests_sends_nothing(no_systemd: None, version_probe: Any, tmp_path: Path) -> None:
    """What test_api_security's discover tests do (a backend and a Solr with Basic-auth credentials in
    local.cfg): the stub answers, nothing is sent."""
    install = tmp_path / "dspace"
    (install / "config").mkdir(parents=True)
    (install / "config" / "local.cfg").write_text(LEAKY_LOCAL_CFG)
    cfg = merge_patch(Config(), {"dspace": {"install_dir": str(install)}})
    sockets = [
        ListeningSocket(8080, "127.0.0.1", False, "ipv4"),
        ListeningSocket(8983, "127.0.0.1", False, "ipv4"),
    ]
    await discovery.discover(cfg, sockets=sockets, unit_states={}, file_exists=nothing_on_disk)
    assert version_probe.calls == [
        ("http://localhost:8080/server", "http://solradmin:SolrBasicPw@127.0.0.1:8983/solr")
    ]
    assert version_probe.reached == []


async def test_discover_asks_for_versions_only_where_something_runs(
    tmp_path: Path, no_systemd: None, version_probe: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    no_version_probe = version_probe.calls
    await discovery.discover(Config(), sockets=[], unit_states={})
    assert no_version_probe == [(None, None)]  # nothing listens, nothing is asked
    sockets = [
        ListeningSocket(8080, "127.0.0.1", False, "ipv4"),
        ListeningSocket(8983, "127.0.0.1", False, "ipv4"),
    ]
    await discovery.discover(Config(), sockets=sockets, unit_states={}, file_exists=nothing_on_disk)
    assert no_version_probe[-1] == ("http://localhost:8080/server", "http://localhost:8983/solr")
    # the discovered URLs win over the configured ones
    cfg = merge_patch(Config(), {"dspace": {"install_dir": str(stock_install(tmp_path))}})
    await discovery.discover(cfg, sockets=[], unit_states={})
    assert no_version_probe[-1] == (None, "http://localhost:8983/solr")

    async def answers(server_url: str | None, solr_url: str | None, **kwargs: Any) -> dict[str, Any]:
        return {"dspace": "DSpace 9.2", "cris": None, "solr": "8.11.4"}

    monkeypatch.setattr(discovery, "probe_versions", answers)
    result = await discovery.discover(Config(), sockets=sockets, unit_states={}, file_exists=nothing_on_disk)
    assert "DSpace 9.2 answers at http://localhost:8080/server" in result.findings
    assert "DSpace 9 expects Solr 9 but Solr 8.11.4 answers" in result.findings


LEAKY_LOCAL_CFG = (
    "db.url = jdbc:postgresql://127.0.0.1:5433/dspace?user=dspace&password=UrlEmbeddedPw\n"
    "db.username = dspace\n"
    "db.password = S3cretDbPassw0rd\n"
    "solr.server = http://solradmin:SolrBasicPw@127.0.0.1:8983/solr\n"
    "scopus.apiKey = SCOPUS-API-KEY-123\n"
    "mail.server.password = MailServerPw\n"
    "dspace.server.url = http://localhost:8080/server\n"
)
LEAKED = ("S3cretDbPassw0rd", "UrlEmbeddedPw", "SolrBasicPw", "SCOPUS-API-KEY-123", "MailServerPw")


async def test_discovery_result_never_carries_secrets(tmp_path: Path, no_systemd: None) -> None:
    import json

    from dspace_sentinel.config import MASK

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "local.cfg").write_text(LEAKY_LOCAL_CFG)
    cfg = merge_patch(Config(), {"dspace": {"install_dir": str(tmp_path)}})
    result = await discovery.discover(cfg, sockets=[], unit_states={})
    # in-process (discover --apply) the real values are there
    assert result.suggested_config["postgres"]["password"] == "S3cretDbPassw0rd"
    assert result.suggested_config["solr"]["url"] == "http://solradmin:SolrBasicPw@127.0.0.1:8983/solr"
    data = result.to_dict()
    text = json.dumps(data)
    assert not [s for s in LEAKED if s in text]
    assert data["suggested_config"]["postgres"]["password"] == MASK
    assert data["suggested_config"]["solr"]["url"] == f"http://solradmin:{MASK}@127.0.0.1:8983/solr"
    assert data["properties"]["db.url"] == "jdbc:postgresql://127.0.0.1:5433/dspace"
    assert data["properties"]["solr.server"] == "http://127.0.0.1:8983/solr"
    assert "db.password" not in data["properties"] and "scopus.apiKey" not in data["properties"]
    assert any(f == "Solr at http://127.0.0.1:8983/solr" for f in result.findings)


async def test_discovery_expects_only_the_ports_that_listen(no_systemd: None) -> None:
    sockets = [
        ListeningSocket(22, "0.0.0.0", True, "ipv4"),
        ListeningSocket(8080, "0.0.0.0", True, "ipv4"),
        ListeningSocket(8983, "0.0.0.0", True, "ipv4"),  # exposed: the ports check will say so
        ListeningSocket(5432, "127.0.0.1", False, "ipv4"),
        ListeningSocket(3306, "127.0.0.1", False, "ipv4"),
    ]
    result = await discovery.discover(Config(), sockets=sockets, unit_states={})
    assert result.suggested_config["ports"] == [
        {"port": 22, "label": "SSH", "expect": "open"},
        {"port": 8080, "label": "Backend", "expect": "open"},
        {"port": 8983, "label": "Solr", "expect": "local_only"},
        {"port": 5432, "label": "PostgreSQL", "expect": "local_only"},
    ]
    assert result.suggested_config["modules"]["ports"] is True
    nothing = await discovery.discover(Config(), sockets=[], unit_states={})
    assert "ports" not in nothing.suggested_config
    assert nothing.suggested_config["modules"]["ports"] is False


async def test_discovery_follows_the_database_port(fixtures_dir: Path, no_systemd: None) -> None:
    cfg = merge_patch(Config(), {"dspace": {"install_dir": str(fixtures_dir / "dspace-full")}})
    sockets = [
        ListeningSocket(5433, "127.0.0.1", False, "ipv4"),
        ListeningSocket(22, "0.0.0.0", True, "ipv4"),
    ]
    result = await discovery.discover(cfg, sockets=sockets, unit_states={})
    assert result.suggested_config["ports"] == [
        {"port": 22, "label": "SSH", "expect": "open"},
        {"port": 5433, "label": "PostgreSQL", "expect": "local_only"},
    ]


async def test_discovery_skips_a_web_server_that_does_not_run(no_systemd: None) -> None:
    idle = await discovery.discover(Config(), sockets=[], unit_states={"apache2": "inactive"})
    assert "nginx" not in idle.suggested_config
    assert idle.suggested_config["modules"]["nginx"] is False
    units = {"nginx": "inactive", "apache2": "active"}
    running = await discovery.discover(Config(), sockets=[], unit_states=units)
    assert running.suggested_config["nginx"]["service"] == "apache2"
    assert running.suggested_config["modules"]["nginx"] is True
    crashed = await discovery.discover(Config(), sockets=[], unit_states={"nginx": "failed"})
    assert crashed.suggested_config["nginx"]["service"] == "nginx"  # meant to run: worth watching


HAND_MADE_SOCKETS = [
    ListeningSocket(22, "0.0.0.0", True, "ipv4"),
    ListeningSocket(4000, "0.0.0.0", True, "ipv4"),
    ListeningSocket(8080, "::", True, "ipv6"),
    ListeningSocket(8983, "::", True, "ipv6"),
    ListeningSocket(5432, "127.0.0.1", False, "ipv4"),
]
"""A DSpace 8 server set up by hand: Tomcat 10 and PostgreSQL run as units, Solr and the UI answer
without one, and there is no web server (DSpace is opened directly on :4000)."""
HAND_MADE_UNITS = {"tomcat10": "active", "postgresql": "active"}
NO_PROPERTIES: tuple[dict[str, str], list[str]] = ({}, [])


async def test_discovery_names_no_unit_for_a_service_that_runs_without_one(no_systemd: None) -> None:
    """Solr and the UI answer without a systemd unit: no "unit not found" rung, a finding instead."""
    result = await discovery.discover(
        Config(),
        sockets=HAND_MADE_SOCKETS,
        unit_states=HAND_MADE_UNITS,
        properties=NO_PROPERTIES,
        file_exists=nothing_on_disk,
    )
    patch = result.suggested_config
    assert patch["solr"] == {"service": ""}
    assert patch["dspace"] == {"backend_service": "tomcat10", "frontend_service": ""}
    assert patch["postgres"] == {"service": "postgresql"}
    assert "nginx" not in patch  # no web server, so no name to suggest for one
    assert patch["modules"]["nginx"] is False and patch["modules"]["ssl"] is False
    assert patch["modules"]["solr"] is True and patch["modules"]["frontend"] is True
    assert (
        "Solr answers on 8983 but no systemd unit was found for it: it may not start by itself after a reboot"
        in result.findings
    )
    assert (
        "The DSpace UI answers on 4000 but no systemd unit was found for it: it may not start by itself "
        "after a reboot" in result.findings
    )
    # "Start monitoring" saves the empty names: the Solr and frontend ladders get no systemd rung
    merged = merge_patch(Config(), {k: v for k, v in patch.items() if k != "modules"})
    assert merged.solr.service == "" and merged.dspace.frontend_service == ""
    assert merged.dspace.backend_service == "tomcat10"


async def test_discovery_names_no_unit_for_a_web_server_without_one(no_systemd: None) -> None:
    sockets = [ListeningSocket(80, "0.0.0.0", True, "ipv4"), ListeningSocket(443, "::", True, "ipv6")]
    result = await discovery.discover(
        Config(), sockets=sockets, unit_states={"nginx": "inactive"}, properties=NO_PROPERTIES
    )
    assert result.suggested_config["nginx"] == {"service": ""}
    assert result.suggested_config["modules"]["nginx"] is True
    assert (
        "A web server answers on 80 and 443 but no nginx or apache2 unit runs: Sentinel checks only "
        "whether it answers" in result.findings
    )
    # a unit that runs is named as before, and so is a Solr or UI unit that is there
    units = {"nginx": "active", "solr": "inactive", "pm2-dspace": "active"}
    named = await discovery.discover(
        Config(),
        sockets=[*sockets, *HAND_MADE_SOCKETS],
        unit_states=units,
        properties=NO_PROPERTIES,
        file_exists=nothing_on_disk,
    )
    assert named.suggested_config["nginx"] == {"service": "nginx"}
    assert named.suggested_config["solr"] == {"service": "solr"}
    assert named.suggested_config["dspace"]["frontend_service"] == "pm2-dspace"
    assert not [f for f in named.findings if "no systemd unit" in f or "unit runs" in f]


async def test_discovery_keeps_the_service_names_without_systemd(no_systemd: None) -> None:
    """Without systemctl nothing can be said about units: the names stay as they are, no finding."""
    result = await discovery.discover(
        Config(), sockets=HAND_MADE_SOCKETS, properties=NO_PROPERTIES, file_exists=nothing_on_disk
    )
    assert "solr" not in result.suggested_config and "dspace" not in result.suggested_config
    assert not [f for f in result.findings if "no systemd unit" in f]


async def test_discovery_suggests_no_log_for_what_the_server_does_not_have(
    tmp_path: Path, no_systemd: None, solr_processes: list[str]
) -> None:
    """No web server and no /var/solr/logs/solr.log: no nginx or Solr entry. DSpace's own stay."""
    cfg = merge_patch(Config(), {"dspace": {"install_dir": str(stock_install(tmp_path))}})
    result = await discovery.discover(
        cfg, sockets=HAND_MADE_SOCKETS, unit_states=HAND_MADE_UNITS, file_exists=nothing_on_disk
    )
    files = {f["id"]: f["path"] for f in result.suggested_config["logs"]["files"]}
    assert files == {"dspace": f"{tmp_path}/log/dspace.log", "tomcat": "/var/log/tomcat10/catalina.out"}
    assert "No web server was found: the nginx log (/var/log/nginx/error.log) is left out" in result.findings
    assert "The Solr log /var/solr/logs/solr.log does not exist: it is left out" in result.findings
    assert solr_processes == ["process list"]  # a running Solr was looked for, once
    # a log that is there stays, and so does the nginx entry of a web server that was found
    kept = await discovery.discover(
        cfg, sockets=HAND_MADE_SOCKETS, unit_states=HAND_MADE_UNITS, file_exists=on_disk(*SERVICE_LOGS)
    )
    assert [f["id"] for f in kept.suggested_config["logs"]["files"]] == ["dspace", "tomcat", "solr", "nginx"]
    web = await discovery.discover(
        cfg,
        sockets=[*HAND_MADE_SOCKETS, ListeningSocket(80, "0.0.0.0", True, "ipv4")],
        unit_states={**HAND_MADE_UNITS, "nginx": "active"},
        file_exists=nothing_on_disk,
    )
    assert [f["id"] for f in web.suggested_config["logs"]["files"]] == ["dspace", "tomcat", "nginx"]
    # custom paths are never touched
    custom = merge_patch(
        cfg,
        {
            "logs": {
                "files": [
                    {"id": "solr", "label": "Solr", "path": "/opt/solr/server/logs/solr.log"},
                    {"id": "nginx", "label": "Web", "path": "/srv/web/error.log"},
                ]
            }
        },
    )
    mine = await discovery.discover(
        custom, sockets=HAND_MADE_SOCKETS, unit_states=HAND_MADE_UNITS, file_exists=nothing_on_disk
    )
    assert [f["path"] for f in mine.suggested_config["logs"]["files"]] == [
        f"{tmp_path}/log/dspace.log",
        "/opt/solr/server/logs/solr.log",
        "/srv/web/error.log",
    ]


async def test_discovery_finds_the_log_of_a_solr_started_by_hand(tmp_path: Path, no_systemd: None) -> None:
    """/var/solr/logs/solr.log is missing: the running Solr's -Dsolr.log.dir says where it logs."""
    cfg = merge_patch(Config(), {"dspace": {"install_dir": str(stock_install(tmp_path))}})
    solr = [
        "/usr/lib/jvm/java-11-openjdk-amd64/bin/java",
        "-server",
        "-Xms512m",
        "-Dsolr.log.dir=/opt/solr-8.11.4/server/logs",
        "-Djetty.port=8983",
        "-jar",
        "start.jar",
    ]
    result = await discovery.discover(
        cfg,
        sockets=HAND_MADE_SOCKETS,
        unit_states=HAND_MADE_UNITS,
        file_exists=nothing_on_disk,
        cmdlines=[["/usr/sbin/sshd", "-D"], solr],
    )
    files = result.suggested_config["logs"]["files"]
    assert [f["id"] for f in files] == ["dspace", "tomcat", "solr"]
    assert files[2] == {
        "id": "solr",
        "label": "Solr",
        "path": "/opt/solr-8.11.4/server/logs/solr.log",
        "daily_suffix": False,
        "timezone": "",
    }
    assert (
        "Solr logs to /opt/solr-8.11.4/server/logs/solr.log (its -Dsolr.log.dir): used as the Solr log"
        in result.findings
    )
    assert not [f for f in result.findings if f.startswith("The Solr log")]
    # a Solr installed as a service logs to the default path: the entry stays as it is, no finding
    service = [*solr[:3], "-Dsolr.log.dir=/var/solr/logs", *solr[4:]]
    same = await discovery.discover(
        cfg,
        sockets=HAND_MADE_SOCKETS,
        unit_states=HAND_MADE_UNITS,
        file_exists=nothing_on_disk,
        cmdlines=[service],
    )
    assert {f["id"]: f["path"] for f in same.suggested_config["logs"]["files"]}["solr"] == discovery.SOLR_LOG
    assert not [f for f in same.findings if "Solr log" in f or "Solr logs" in f]


async def test_discovery_leaves_the_log_list_alone_where_dspace_does_not_run(
    no_systemd: None, solr_processes: list[str]
) -> None:
    """A frontend-only server (no DSpace configuration, no backend) keeps the chosen option's list."""
    result = await discovery.discover(
        Config(),
        sockets=[ListeningSocket(4000, "0.0.0.0", True, "ipv4")],
        unit_states={"dspace-ui": "active"},
        properties=NO_PROPERTIES,
        file_exists=nothing_on_disk,
    )
    assert "logs" not in result.suggested_config
    assert not [f for f in result.findings if "left out" in f]
    assert solr_processes == []


@pytest.mark.parametrize(
    ("cmdlines", "folder"),
    [
        ([["java", "-Dsolr.log.dir=/opt/solr/server/logs", "-jar", "start.jar"]], "/opt/solr/server/logs"),
        ([["java", "-Dsolr.log.dir=/var/solr/logs/"]], "/var/solr/logs"),
        ([["java", "-Dsolr.log.dir=server/logs"]], None),  # relative to a working directory unknown here
        ([["java", "-Dsolr.log.dir="]], None),
        ([["sshd", "-D"], ["java", "-Dsolr.log.dir=/srv/solr/logs"]], "/srv/solr/logs"),
        ([], None),
    ],
)
def test_solr_log_dir(cmdlines: list[list[str]], folder: str | None) -> None:
    assert discovery.solr_log_dir(cmdlines) == folder


def test_process_cmdlines_keeps_the_processes_that_name_a_solr_log_folder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Proc:
        def __init__(self, cmdline: list[str] | None) -> None:
            self.info = {"cmdline": cmdline}

    solr = ["/usr/bin/java", "-Dsolr.log.dir=/opt/solr/server/logs", "-Djetty.port=8983", "-jar", "start.jar"]
    procs = [Proc(["/usr/sbin/sshd", "-D"]), Proc(solr), Proc(None)]  # None: a command line not readable
    monkeypatch.setattr(discovery.psutil, "process_iter", lambda attrs=None: iter(procs))
    assert REAL_PROCESS_CMDLINES() == [solr]


def test_a_log_in_a_folder_sentinel_cannot_enter_may_be_there(tmp_path: Path) -> None:
    """Only a log that is certainly missing is left out: /var/solr is often solr:solr 0750."""
    assert discovery._may_exist(str(tmp_path / "missing.log")) is False
    (tmp_path / "file").write_text("x\n")
    assert discovery._may_exist(str(tmp_path / "file" / "solr.log")) is False  # a file on the way
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "solr.log").write_text("x\n")
    locked.chmod(0)
    try:
        assert discovery._may_exist(str(locked / "solr.log")) is True  # denied (or root: there)
    finally:
        locked.chmod(0o755)


async def test_systemctl_gets_unit_names_after_double_dash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dspace_sentinel.util import systemd

    log = tmp_path / "argv.log"
    fake = tmp_path / "systemctl"
    fake.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\necho inactive\n')
    fake.chmod(0o755)
    monkeypatch.setattr(systemd, "systemctl_path", lambda: str(fake))
    await systemd.is_active("-M evil")
    await systemd.list_units("postgresql@*")
    assert log.read_text().splitlines() == [
        "is-active -- -M evil",
        "show --property=LoadState --value -- -M evil",
        "list-units --type=service --all --no-legend --plain --no-pager -- postgresql@*",
    ]
