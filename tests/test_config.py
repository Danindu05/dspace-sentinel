"""Config: defaults, YAML merge, env overrides, masking, merge_patch, personas, save."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import pytest
import yaml

from dspace_sentinel.auth import hash_password, verify_password
from dspace_sentinel.config import (
    DEFAULT_CONFIG,
    ENV_DESTINATION_ID,
    LOGIN_UNREADABLE,
    MASK,
    MODULE_IDS,
    PERSONAS,
    Config,
    LoginConfig,
    apply_demo_overrides,
    apply_login,
    apply_persona,
    deep_merge,
    dspace_modules_enabled,
    load_config,
    masked,
    merge_patch,
    save_config,
    startup_notices,
)

# Bot tokens shaped like @BotFather's (the config refuses anything else); made up, and split
# in two so that secret scanners do not report them
TOKEN_A = "123456789:" + "AAH-test-token-for-sentinel-000000001"
TOKEN_B = "987654321:" + "AAH-test-token-for-sentinel-000000002"
ENV_TOKEN = "555555555:" + "AAH-env-only-token-for-sentinel-00000"


def test_defaults_match_spec() -> None:
    cfg = Config()
    assert cfg.server.host == "127.0.0.1"
    assert cfg.server.port == 9765
    assert cfg.collector.system_interval == 15
    assert cfg.collector.repository_interval == 900
    assert cfg.collector.retention_days == 30
    assert cfg.dspace.backend_service == "tomcat9"
    assert cfg.solr.cores == ["search", "statistics", "oai", "authority"]
    assert [p.port for p in cfg.ports] == [22, 80, 443, 4000, 8080, 8983, 5432]
    assert cfg.ports[-1].expect == "local_only"
    assert [f.id for f in cfg.logs.files] == ["dspace", "tomcat", "solr", "nginx"]
    assert cfg.logs.files[0].daily_suffix is True
    assert cfg.alerts.cpu.warning == 80 and cfg.alerts.cpu.critical == 95
    assert cfg.alerts.ssl_expiry_days.critical == 7
    assert cfg.reports.weekly.weekday == 1
    assert cfg.reports.weekly.send_telegram is False and cfg.reports.weekly.send_email is False
    assert cfg.notifiers.telegram.chats == [] and cfg.notifiers.telegram.enabled is False
    email = cfg.notifiers.email
    assert (email.enabled, email.smtp_host, email.smtp_port, email.security) == (
        False,
        "smtp.gmail.com",
        587,
        "starttls",
    )
    assert email.username == email.password == email.from_address == "" and email.recipients == []
    assert email.from_name == "DSpace Sentinel" and email.quiet_hours == email.daily_digest == ""
    assert list(DEFAULT_CONFIG["notifiers"]) == ["telegram", "email", "webhook"]
    assert "chat_id" not in DEFAULT_CONFIG["notifiers"]["telegram"]
    assert "bot_token" not in DEFAULT_CONFIG["notifiers"]["telegram"]
    assert all(getattr(cfg.modules, m) is False for m in MODULE_IDS)
    assert cfg.appearance.theme == "system"
    assert cfg.demo is False
    assert DEFAULT_CONFIG["modules"]["postgres"] is False
    assert "demo" not in DEFAULT_CONFIG


def test_example_yaml_equals_defaults() -> None:
    example = Path(__file__).resolve().parents[1] / "config" / "sentinel.example.yaml"
    data = yaml.safe_load(example.read_text())
    assert Config.model_validate(data).to_yaml_dict() == DEFAULT_CONFIG
    # the notifier sections are spelled out in today's shape, not filled in by the defaults
    assert data["notifiers"] == DEFAULT_CONFIG["notifiers"] and data["reports"] == DEFAULT_CONFIG["reports"]


def test_load_config_missing_file_uses_defaults(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "nope.yaml")
    assert cfg.to_yaml_dict() == DEFAULT_CONFIG


def test_load_config_yaml_then_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "sentinel.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "server": {"port": 9000, "hostname": "repo-1"},
                "modules": {"postgres": True},
                "postgres": {"password": "from-yaml"},
                "unknown_key": {"x": 1},
            }
        )
    )
    monkeypatch.setenv("SENTINEL_PORT", "9100")
    monkeypatch.setenv("SENTINEL_PG_PASSWORD", "from-env")
    monkeypatch.setenv("SENTINEL_TELEGRAM_TOKEN", ENV_TOKEN)
    monkeypatch.setenv("SENTINEL_TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("SENTINEL_SMTP_PASSWORD", "abcdefghijklmnop")
    monkeypatch.setenv("SENTINEL_AUTH_TOKEN", "secret")
    cfg = load_config(path)
    assert cfg.server.port == 9100  # env beats yaml
    assert cfg.server.hostname == "repo-1"
    assert cfg.modules.postgres is True
    assert cfg.postgres.password == "from-env"
    # both Telegram variables add one read-only destination of their own
    (env_bot,) = cfg.notifiers.telegram.chats
    assert (env_bot.id, env_bot.label, env_bot.bot_token, env_bot.chat_id, env_bot.min_severity) == (
        ENV_DESTINATION_ID,
        "From environment",
        ENV_TOKEN,
        "42",
        "warning",
    )
    assert cfg.notifiers.email.password == "abcdefghijklmnop"
    assert cfg.server.auth_token == "secret"
    assert cfg.demo is False


def test_load_config_demo_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTINEL_DEMO", "1")
    cfg = load_config(tmp_path / "x.yaml")
    assert cfg.demo is True
    assert cfg.hostname == "demo-dspace"
    assert cfg.setup.completed is False
    assert all(getattr(cfg.modules, m) for m in MODULE_IDS)


def test_load_config_invalid_yaml(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("server: [not, a, mapping\n")
    with pytest.raises(ValueError):
        load_config(path)


def test_load_config_invalid_value(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump({"server": {"port": 99999}}))
    with pytest.raises(ValueError):
        load_config(path)


def test_deep_merge_replaces_lists() -> None:
    merged = deep_merge({"a": {"b": 1, "c": [1, 2]}, "d": 1}, {"a": {"c": [3]}, "e": 2})
    assert merged == {"a": {"b": 1, "c": [3]}, "d": 1, "e": 2}


def test_masked_and_merge_patch_keep_secrets() -> None:
    cfg = merge_patch(
        Config(),
        {
            "server": {"auth_token": "tok"},
            "postgres": {"password": "pw"},
            "notifiers": {
                "telegram": {"chats": [{"bot_token": TOKEN_A, "chat_id": 7}]},
                "email": {"password": "app-password"},
            },
        },
    )
    m = masked(cfg)
    assert m["server"]["auth_token"] == MASK
    assert m["postgres"]["password"] == MASK
    (bot,) = m["notifiers"]["telegram"]["chats"]
    assert bot["bot_token"] == MASK and bot["chat_id"] == "7" and bot["from_env"] is False
    assert m["notifiers"]["email"]["password"] == MASK
    assert m["demo"] is False
    # empty secrets are not masked (UI must see they are unset)
    assert masked(Config())["postgres"]["password"] == ""
    # sending the mask back keeps the old value
    again = merge_patch(cfg, {"postgres": {"password": MASK, "sslmode": "require"}})
    assert again.postgres.password == "pw"
    assert again.postgres.sslmode == "require"
    # a real new value replaces it
    assert merge_patch(cfg, {"postgres": {"password": "new"}}).postgres.password == "new"


def test_merge_patch_validates_and_preserves_demo() -> None:
    cfg = apply_demo_overrides(Config())
    with pytest.raises(ValueError):
        merge_patch(cfg, {"collector": {"system_interval": 1}})
    with pytest.raises(ValueError):
        merge_patch(cfg, {"appearance": {"theme": "neon"}})
    with pytest.raises(ValueError):
        merge_patch(cfg, ["not", "a", "dict"])  # type: ignore[arg-type]
    new = merge_patch(cfg, {"appearance": {"theme": "dark"}, "demo": False})
    assert new.demo is True  # runtime flag cannot be patched away
    assert new.appearance.theme == "dark"
    assert cfg.appearance.theme == "system"  # original untouched


def test_personas() -> None:
    assert set(PERSONAS) == {"full", "backend", "frontend", "server"}
    full = apply_persona(Config(), "full")
    assert all(getattr(full.modules, m) for m in MODULE_IDS)
    assert full.setup.persona == "full"
    backend = apply_persona(Config(), "backend")
    assert backend.modules.frontend is False and backend.modules.postgres is True
    frontend = apply_persona(Config(), "frontend")
    assert frontend.modules.enabled_ids() == [
        "ports",
        "security",
        "backend",
        "frontend",
        "nginx",
        "ssl",
        "logs",
    ]
    assert [f.id for f in frontend.logs.files] == ["nginx"]
    server = apply_persona(Config(), "server")
    assert server.modules.enabled_ids() == ["ports", "security", "nginx", "ssl", "logs"]
    with pytest.raises(ValueError):
        apply_persona(Config(), "kitchen-sink")


def test_security_module_interval_and_report_section() -> None:
    """SECURITY-CONTRACT: off by default like every module, on in every persona, its own interval."""
    cfg = Config()
    assert MODULE_IDS.index("security") == MODULE_IDS.index("ports") + 1
    assert cfg.modules.security is False and DEFAULT_CONFIG["modules"]["security"] is False
    assert cfg.collector.security_interval == 900 and cfg.interval_for("security_interval") == 900
    for persona in PERSONAS:
        assert PERSONAS[persona]["modules"]["security"] is True, persona
        assert apply_persona(Config(), persona).module_enabled("security"), persona
    for bad in (59, 86401):
        with pytest.raises(ValueError):
            merge_patch(cfg, {"collector": {"security_interval": bad}})
    assert merge_patch(cfg, {"collector": {"security_interval": 60}}).collector.security_interval == 60
    assert apply_demo_overrides(cfg).modules.security is True


def test_security_is_a_default_report_section() -> None:
    """The weekly report includes the Security section unless the operator leaves it out."""
    sections = Config().reports.default_sections
    assert sections == [
        "summary",
        "server",
        "services",
        "latency",
        "alerts",
        "repository",
        "security",
        "recommendations",
    ]


def test_dspace_modules_enabled() -> None:
    assert dspace_modules_enabled(Config()) is False
    assert dspace_modules_enabled(apply_persona(Config(), "server")) is False
    assert dspace_modules_enabled(merge_patch(Config(), {"modules": {"solr": True}})) is True


def test_save_config_atomic_0600(tmp_path: Path) -> None:
    path = tmp_path / "cfg" / "sentinel.yaml"
    cfg = merge_patch(Config(), {"server": {"port": 9001}, "postgres": {"password": "pw"}})
    save_config(cfg, path)
    assert path.exists()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    reloaded = load_config(path)
    assert reloaded.server.port == 9001
    assert reloaded.postgres.password == "pw"
    assert "demo" not in yaml.safe_load(path.read_text())
    leftovers = [p for p in path.parent.iterdir() if p.name.startswith(".sentinel-")]
    assert leftovers == []


def test_hostname_and_helpers() -> None:
    cfg = Config()
    assert cfg.hostname  # falls back to the machine name
    cfg.server.hostname = "repo"
    assert cfg.hostname == "repo"
    assert cfg.interval_for("db_interval") == 60
    assert cfg.module_enabled("server") is True
    assert cfg.module_enabled("solr") is False
    assert cfg.modules.as_dict()["incidents"] is False


def test_load_demo_state_merges_a_previous_demo_session(tmp_path: Path) -> None:
    from dspace_sentinel.config import load_demo_state

    cfg = apply_demo_overrides(Config())
    missing = tmp_path / "demo-sentinel.yaml"
    assert load_demo_state(cfg, missing) is cfg  # no file → unchanged
    missing.write_text(
        "setup: {completed: true, persona: full}\nmodules: {ssl: false}\nappearance: {theme: dark}\n"
    )
    merged = load_demo_state(cfg, missing)
    assert merged.demo is True and merged.hostname == "demo-dspace"
    assert merged.setup.completed is True and merged.setup.persona == "full"
    assert merged.modules.ssl is False and merged.modules.postgres is True
    assert merged.appearance.theme == "dark"
    assert cfg.setup.completed is False  # the input is not mutated
    # a broken file never breaks the demo
    missing.write_text("setup: [not, a, mapping\n")
    assert load_demo_state(cfg, missing) is cfg
    missing.write_text("collector: {system_interval: 1}\n")  # invalid values are ignored as a whole
    assert load_demo_state(cfg, missing) is cfg


ENV_ONLY = {
    "SENTINEL_TELEGRAM_TOKEN": ENV_TOKEN,
    "SENTINEL_SMTP_PASSWORD": "env-only-smtp-pass",
    "SENTINEL_PG_PASSWORD": "env-only-pg-pass",
    "SENTINEL_AUTH_TOKEN": "env-only-auth",
    "SENTINEL_TELEGRAM_CHAT_ID": "424242",
    "SENTINEL_PORT": "9999",
    "SENTINEL_HOST": "0.0.0.0",
}


def test_save_writes_the_file_layer_not_environment_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SPEC §3: the environment overrides the file; a UI save must not copy it into the file."""
    path = tmp_path / "sentinel.yaml"
    path.write_text("appearance: {theme: light}\npostgres: {password: file-pw}\n")
    for name, value in ENV_ONLY.items():
        monkeypatch.setenv(name, value)
    cfg = load_config(path)
    assert cfg.server.port == 9999 and cfg.postgres.password == "env-only-pg-pass"
    new = merge_patch(cfg, {"appearance": {"theme": "dark"}})
    assert new.postgres.password == "env-only-pg-pass"  # the running app keeps using the env values
    save_config(new, path)
    text = path.read_text()
    for value in ENV_ONLY.values():
        assert value not in text
    saved = yaml.safe_load(text)
    assert saved["appearance"]["theme"] == "dark"
    assert saved["postgres"]["password"] == "file-pw"
    assert saved["server"]["host"] == "127.0.0.1" and saved["server"]["port"] == 9765
    telegram = saved["notifiers"]["telegram"]
    assert telegram["chats"] == [] and "bot_token" not in telegram and "chat_id" not in telegram
    assert saved["notifiers"]["email"]["password"] == ""
    # the environment's Telegram bot is in the running config only
    assert [c.id for c in new.notifiers.telegram.chats] == [ENV_DESTINATION_ID]
    # sending the masked env destination back keeps it out of the file, other keys still save
    shown = masked(new)["notifiers"]["telegram"]
    assert shown["chats"][0]["from_env"] is True and shown["chats"][0]["bot_token"] == MASK
    again = merge_patch(
        new,
        {
            "notifiers": {
                "telegram": {"chats": shown["chats"], "quiet_hours": "23:00-07:00"},
                "email": {"password": MASK, "from_name": "Repository monitor"},
            }
        },
    )
    assert [(c.id, c.bot_token) for c in again.notifiers.telegram.chats] == [(ENV_DESTINATION_ID, ENV_TOKEN)]
    assert again.notifiers.email.password == "env-only-smtp-pass"
    save_config(again, path)
    saved = yaml.safe_load(path.read_text())
    assert saved["notifiers"]["telegram"]["chats"] == []
    assert saved["notifiers"]["telegram"]["quiet_hours"] == "23:00-07:00"
    assert saved["notifiers"]["email"]["password"] == ""
    assert saved["notifiers"]["email"]["from_name"] == "Repository monitor"
    # without the variables the next start sees the file's own values
    for name in ENV_ONLY:
        monkeypatch.delenv(name)
    fresh = load_config(path)
    assert fresh.server.host == "127.0.0.1" and fresh.server.port == 9765
    assert fresh.notifiers.telegram.chats == [] and fresh.postgres.password == "file-pw"
    assert fresh.notifiers.email.password == ""
    assert fresh.appearance.theme == "dark"


def test_explicit_values_are_saved_but_cli_flags_are_not(tmp_path: Path) -> None:
    path = tmp_path / "sentinel.yaml"
    cfg = load_config(path)
    cfg.server.host = "0.0.0.0"  # `serve --host/--port` set the running values only
    cfg.server.port = 9999
    typed = {"chats": [{"label": "Ops", "bot_token": TOKEN_A, "chat_id": "42"}]}
    save_config(merge_patch(cfg, {"notifiers": {"telegram": typed}}), path)
    saved = yaml.safe_load(path.read_text())
    assert saved["server"]["host"] == "127.0.0.1" and saved["server"]["port"] == 9765
    (bot,) = saved["notifiers"]["telegram"]["chats"]
    assert bot["bot_token"] == TOKEN_A and bot["label"] == "Ops" and len(bot["id"]) == 8
    # install-service --host/--port patch the file on purpose
    save_config(merge_patch(load_config(path), {"server": {"host": "0.0.0.0", "port": 9001}}), path)
    saved = yaml.safe_load(path.read_text())
    assert saved["server"]["host"] == "0.0.0.0" and saved["server"]["port"] == 9001


def test_demo_state_holds_only_what_the_ui_changed(tmp_path: Path) -> None:
    from dspace_sentinel.config import load_demo_state

    real = tmp_path / "sentinel.yaml"
    real.write_text("alerts: {cpu: {warning: 60, critical: 90}}\n")
    demo_file = tmp_path / "demo-sentinel.yaml"
    cfg = load_demo_state(load_config(real, demo=True), demo_file)
    new = merge_patch(
        cfg,
        {
            "appearance": {"theme": "dark"},
            "modules": {"ssl": False, "solr": True},  # solr is already on in the demo
            "notifiers": {
                "telegram": {"chats": [{"label": "Me", "bot_token": TOKEN_A, "chat_id": "5"}]},
                "email": {"password": "app-pw", "recipients": [{"address": "ops@example.org"}]},
            },
            "postgres": {"password": "pw"},
            "server": {"port": 9999},
        },
    )
    new = merge_patch(new, {"setup": {"completed": True, "persona": "full"}})
    save_config(new, demo_file)
    bot_id = new.notifiers.telegram.chats[0].id
    assert yaml.safe_load(demo_file.read_text()) == {
        "setup": {"completed": True, "persona": "full"},
        "modules": {"ssl": False},
        "notifiers": {
            "telegram": {"chats": [{"id": bot_id, "label": "Me", "chat_id": "5", "min_severity": "warning"}]},
            "email": {"recipients": [{"address": "ops@example.org", "label": "", "min_severity": "warning"}]},
        },
        "appearance": {"theme": "dark"},
    }
    assert TOKEN_A not in demo_file.read_text() and "app-pw" not in demo_file.read_text()
    # the operator changes the real config later: the next demo follows it
    real.write_text("server: {auth_token: added-later}\nalerts: {cpu: {warning: 70, critical: 90}}\n")
    again = load_demo_state(load_config(real, demo=True), demo_file)
    assert again.server.auth_token == "added-later" and again.alerts.cpu.warning == 70
    assert again.appearance.theme == "dark" and again.setup.completed is True and again.modules.ssl is False
    # an older full snapshot never brings back server settings or secrets
    demo_file.write_text(
        yaml.safe_dump(
            {
                "server": {"auth_token": "", "host": "0.0.0.0"},
                "postgres": {"password": "old-pw"},
                "appearance": {"theme": "light"},
            }
        )
    )
    legacy = load_demo_state(load_config(real, demo=True), demo_file)
    assert legacy.server.auth_token == "added-later" and legacy.server.host == "127.0.0.1"
    assert legacy.postgres.password == ""
    assert legacy.appearance.theme == "light"


@pytest.mark.parametrize(
    "change",
    [{"host": "203.0.113.9"}, {"port": 5433}, {"user": "someone"}, {"database": "other"}, {"host": "::1"}],
)
def test_repointing_postgres_needs_the_password_again(change: dict[str, object]) -> None:
    """The saved password is only ever sent to the server it was saved for."""
    cfg = merge_patch(Config(), {"postgres": {"password": "OperatorsRealDbPassword"}})
    for patch in ({**change, "password": MASK}, change):
        with pytest.raises(ValueError, match="enter the PostgreSQL password again"):
            merge_patch(cfg, {"postgres": patch})
    typed = merge_patch(cfg, {"postgres": {**change, "password": "typed-again"}})
    assert typed.postgres.password == "typed-again"
    peer = merge_patch(cfg, {"postgres": {**change, "password": ""}})  # peer auth or .pgpass
    assert peer.postgres.password == ""


def test_postgres_password_survives_other_edits() -> None:
    cfg = merge_patch(Config(), {"postgres": {"password": "pw"}})
    edit = {"host": "LocalHost", "password": MASK, "sslmode": "require", "service": "postgresql@16"}
    same = merge_patch(cfg, {"postgres": edit})
    assert same.postgres.password == "pw" and same.postgres.sslmode == "require"
    # nothing stored, nothing to leak: re-pointing is free
    assert merge_patch(Config(), {"postgres": {"host": "db.example.org"}}).postgres.host == "db.example.org"


GMAIL_LOGIN = {
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 587,
    "security": "starttls",
    "username": "sentinel.alerts@gmail.com",
    "password": "abcdefghijklmnop",
    "recipients": [{"address": "ops@example.org"}],
}


@pytest.mark.parametrize(
    "change",
    [
        {"smtp_host": "127.0.0.1", "smtp_port": 9850, "security": "none"},  # the reviewer's fake server
        {"smtp_host": "mail.example.org"},
        {"smtp_port": 465},
        {"security": "ssl"},
        {"security": "none"},
        {"username": "someone.else@gmail.com"},
    ],
)
def test_repointing_email_needs_the_password_again(change: dict[str, object]) -> None:
    """The saved app password is only ever sent to the server (and login) it was saved for."""
    cfg = merge_patch(Config(), {"notifiers": {"email": GMAIL_LOGIN}})
    for patch in ({**change, "password": MASK}, change):
        with pytest.raises(ValueError, match="enter the email password again") as err:
            merge_patch(cfg, {"notifiers": {"email": patch}})
        assert str(err.value) == (
            "enter the email password again: the SMTP server, port, security or username changed and "
            "the saved password is only sent to the server it was saved for"
        )
        assert "abcdefghijklmnop" not in str(err.value)
    typed = merge_patch(cfg, {"notifiers": {"email": {**change, "password": "qrstuvwxyzabcdef"}}})
    assert typed.notifiers.email.password == "qrstuvwxyzabcdef"
    cleared = merge_patch(cfg, {"notifiers": {"email": {**change, "password": ""}}})
    assert cleared.notifiers.email.password == ""


def test_email_password_survives_other_edits() -> None:
    cfg = merge_patch(Config(), {"notifiers": {"email": GMAIL_LOGIN}})
    same = merge_patch(
        cfg,
        {
            "notifiers": {
                "email": {
                    "smtp_host": "SMTP.Gmail.com",  # the same server, written differently
                    "username": " sentinel.alerts@gmail.com ",
                    "password": MASK,
                    "enabled": True,
                    "from_name": "Repository monitor",
                    "recipients": [{"address": "dean@example.org"}],
                }
            }
        },
    )
    assert same.notifiers.email.password == "abcdefghijklmnop" and same.notifiers.email.enabled
    # a server that needs no login is never sent the password: re-pointing to it is free …
    relay = merge_patch(
        cfg,
        {
            "notifiers": {
                "email": {
                    "smtp_host": "relay.example.org",
                    "smtp_port": 25,
                    "security": "none",
                    "username": "",
                    "from_address": "sentinel@example.org",
                    "password": MASK,
                }
            }
        },
    )
    assert relay.notifiers.email.smtp_host == "relay.example.org"
    # … but a login there needs the password typed again
    with pytest.raises(ValueError, match="enter the email password again"):
        merge_patch(relay, {"notifiers": {"email": {"username": "sentinel.alerts@gmail.com"}}})
    # nothing stored, nothing to leak
    fresh = merge_patch(
        Config(), {"notifiers": {"email": {"smtp_host": "mail.example.org", "username": "a@b.org"}}}
    )
    assert fresh.notifiers.email.smtp_host == "mail.example.org"


def test_personas_expect_only_their_own_ports() -> None:
    """A backend-only host or a plain server must not wait for Angular on 4000 (false 'Ports degraded')."""

    def ports(persona: str) -> list[tuple[int, str]]:
        return [(p.port, p.expect) for p in apply_persona(Config(), persona).ports]

    assert ports("full") == [
        (22, "open"),
        (80, "open"),
        (443, "open"),
        (4000, "open"),
        (8080, "open"),
        (8983, "local_only"),
        (5432, "local_only"),
    ]
    assert ports("backend") == [
        (22, "open"),
        (80, "open"),
        (443, "open"),
        (8080, "open"),
        (8983, "local_only"),
        (5432, "local_only"),
    ]
    assert ports("frontend") == [(22, "open"), (80, "open"), (443, "open"), (4000, "open")]
    assert ports("server") == [(22, "open")]


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("postgres", "service", "--version"),
        ("solr", "service", "-M some-container"),
        ("nginx", "service", "nginx;reboot"),
        ("dspace", "backend_service", "../tomcat9"),
        ("dspace", "frontend_service", "dspace ui"),
        ("dspace", "frontend_service", "u" * 129),
    ],
)
def test_systemd_unit_names_are_validated(section: str, key: str, value: str) -> None:
    """Unit names reach systemctl; an option-looking or odd value must never get that far."""
    with pytest.raises(ValueError, match="systemd unit name"):
        merge_patch(Config(), {section: {key: value}})


def test_valid_systemd_unit_names() -> None:
    cfg = merge_patch(
        Config(),
        {
            "postgres": {"service": "postgresql@16-main"},
            "solr": {"service": ""},  # empty skips the rung
            "nginx": {"service": "nginx.service"},
            "dspace": {"backend_service": " tomcat10 ", "frontend_service": "dspace-ui"},
        },
    )
    assert cfg.postgres.service == "postgresql@16-main" and cfg.solr.service == ""
    assert cfg.dspace.backend_service == "tomcat10"


def test_webhook_url_is_a_secret(tmp_path: Path) -> None:
    """Slack/Discord/Teams/ntfy URLs carry their credential in the path."""
    from dspace_sentinel.config import load_demo_state

    hook = "https://hooks.slack.com/services/T000/B000/XXXXSECRETXXXX"
    cfg = merge_patch(Config(), {"notifiers": {"webhook": {"enabled": True, "url": hook}}})
    assert masked(cfg)["notifiers"]["webhook"]["url"] == MASK
    assert masked(Config())["notifiers"]["webhook"]["url"] == ""
    again = merge_patch(cfg, {"notifiers": {"webhook": {"url": MASK, "enabled": False}}})
    assert again.notifiers.webhook.url == hook and again.notifiers.webhook.enabled is False
    # never saved by, nor read back into, a demo session
    demo_file = tmp_path / "demo-sentinel.yaml"
    demo = merge_patch(apply_demo_overrides(Config()), {"notifiers": {"webhook": {"url": hook}}})
    save_config(demo, demo_file)
    assert "SECRET" not in demo_file.read_text()
    demo_file.write_text(yaml.safe_dump({"notifiers": {"webhook": {"url": hook}}}))
    assert load_demo_state(apply_demo_overrides(Config()), demo_file).notifiers.webhook.url == ""


# ------------------------------------------------------------- the dashboard login

LOGIN_PASSWORD = "correct horse battery"
LOGIN_HASH = hash_password(LOGIN_PASSWORD)


def test_there_is_no_login_by_default() -> None:
    assert DEFAULT_CONFIG["server"]["login"] == {"username": "", "password_hash": ""}
    assert Config().server.login == LoginConfig()
    assert masked(Config())["server"]["login"] == {"username": "", "password_hash": ""}


def test_the_password_hash_is_a_secret(tmp_path: Path) -> None:
    from dspace_sentinel.config import load_demo_state

    cfg = merge_patch(Config(), {"server": {"login": {"username": "admin", "password_hash": LOGIN_HASH}}})
    assert masked(cfg)["server"]["login"] == {"username": "admin", "password_hash": MASK}
    again = merge_patch(cfg, {"server": {"login": {"username": "admin", "password_hash": MASK}}})
    assert again.server.login.password_hash == LOGIN_HASH  # a GET → PATCH round trip keeps it
    # saved like any other setting (mode 0600) …
    path = tmp_path / "sentinel.yaml"
    save_config(cfg, path)
    assert load_config(path).server.login == cfg.server.login
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    # … but never by, nor into, a demo session
    demo_file = tmp_path / "demo-sentinel.yaml"
    save_config(merge_patch(apply_demo_overrides(Config()), {"appearance": {"theme": "dark"}}), demo_file)
    demo_file.write_text(
        yaml.safe_dump({"server": {"login": {"username": "x", "password_hash": LOGIN_HASH}}})
    )
    assert load_demo_state(apply_demo_overrides(Config()), demo_file).server.login == LoginConfig()


def test_a_login_in_the_file_loads_as_text(tmp_path: Path) -> None:
    path = tmp_path / "sentinel.yaml"
    path.write_text("server:\n  login:\n    username: 1234\n    password_hash: 99\n")
    cfg = load_config(path)
    assert (cfg.server.login.username, cfg.server.login.password_hash) == ("1234", "99")
    assert LOGIN_UNREADABLE in startup_notices(cfg)  # set, but nobody can sign in with it
    path.write_text("server:\n  login:\n")
    assert load_config(path).server.login == LoginConfig()
    path.write_text(
        yaml.safe_dump({"server": {"login": {"username": " admin ", "password_hash": LOGIN_HASH}}})
    )
    cfg = load_config(path)
    assert cfg.server.login.username == "admin" and startup_notices(cfg) == []


@pytest.mark.parametrize(
    "login",
    [
        {"username": "admin", "password_hash": "plain-text-password"},
        {"username": "John Smith", "password_hash": LOGIN_HASH},
        {"username": "", "password_hash": LOGIN_HASH},
    ],
)
def test_a_login_that_cannot_be_used_never_stops_the_start(tmp_path: Path, login: dict[str, str]) -> None:
    path = tmp_path / "sentinel.yaml"
    path.write_text(yaml.safe_dump({"server": login}))  # wrong place: ignored like any unknown key
    assert startup_notices(load_config(path)) == []
    path.write_text(yaml.safe_dump({"server": {"login": login}}))
    cfg = load_config(path)
    assert cfg.server.login.password_hash == login["password_hash"]  # kept: the login stays on
    assert startup_notices(cfg) == [LOGIN_UNREADABLE]
    assert LOGIN_UNREADABLE == "The login in server.login could not be read: run ./sentinel.sh set-password"


def test_apply_login_changes_the_running_settings_and_the_file_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "sentinel.yaml"
    path.write_text("server:\n  port: 9001\n")
    monkeypatch.setenv("SENTINEL_AUTH_TOKEN", "EnvOnlyToken")
    cfg = load_config(path)
    apply_login(cfg, "admin", LOGIN_HASH)
    assert cfg.server.login == LoginConfig(username="admin", password_hash=LOGIN_HASH)
    save_config(cfg, path)
    text = path.read_text()
    assert LOGIN_HASH in text and "EnvOnlyToken" not in text and LOGIN_PASSWORD not in text
    assert verify_password(LOGIN_PASSWORD, load_config(path).server.login.password_hash)
    later = merge_patch(cfg, {"appearance": {"theme": "dark"}})  # a later UI save keeps it
    save_config(later, path)
    assert load_config(path).server.login.password_hash == LOGIN_HASH
    apply_login(later, "", "")
    save_config(later, path)
    assert load_config(path).server.login == LoginConfig()


# ------------------------------------------------------------- notifiers: Telegram bots, email


def _bots(cfg: Config) -> list[tuple[str, str, str]]:
    return [(c.label, c.bot_token, c.chat_id) for c in cfg.notifiers.telegram.chats]


def test_old_single_bot_config_loads_as_one_destination(tmp_path: Path) -> None:
    """Everyone who configured one bot_token + chat_id keeps getting alerts after the upgrade."""
    path = tmp_path / "sentinel.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "notifiers": {
                    "telegram": {
                        "enabled": True,
                        "bot_token": TOKEN_A,
                        "chat_id": -1001234,
                        "quiet_hours": "23:00-07:00",
                    }
                }
            }
        )
    )
    cfg = load_config(path)
    (bot,) = cfg.notifiers.telegram.chats
    assert (bot.label, bot.bot_token, bot.chat_id, bot.min_severity) == ("", TOKEN_A, "-1001234", "warning")
    assert len(bot.id) == 8 and all(ch in "0123456789abcdef" for ch in bot.id)
    assert cfg.notifiers.telegram.enabled is True and cfg.notifiers.telegram.quiet_hours == "23:00-07:00"
    assert load_config(path).notifiers.telegram.chats[0].id == bot.id  # stable until the file is saved
    # saving writes the list only, never the old keys again
    save_config(merge_patch(cfg, {"appearance": {"theme": "dark"}}), path)
    saved = yaml.safe_load(path.read_text())["notifiers"]["telegram"]
    assert "bot_token" not in saved and "chat_id" not in saved
    assert saved["chats"] == [
        {"id": bot.id, "label": "", "bot_token": TOKEN_A, "chat_id": "-1001234", "min_severity": "warning"}
    ]
    assert load_config(path).notifiers.telegram.chats[0].id == bot.id
    # an empty old pair is no destination; a list next to the old keys wins
    assert (
        Config.model_validate(
            {"notifiers": {"telegram": {"bot_token": "", "chat_id": ""}}}
        ).notifiers.telegram.chats
        == []
    )
    both = {"chats": [{"bot_token": TOKEN_B, "chat_id": "7"}], "bot_token": TOKEN_A, "chat_id": "42"}
    assert _bots(Config.model_validate({"notifiers": {"telegram": both}})) == [("", TOKEN_B, "7")]


def test_old_single_bot_patch_becomes_the_destination_list() -> None:
    """An old client PATCHing bot_token + chat_id edits the one destination it knows about."""
    cfg = merge_patch(Config(), {"notifiers": {"telegram": {"bot_token": TOKEN_A, "chat_id": "42"}}})
    (bot,) = cfg.notifiers.telegram.chats
    assert (bot.bot_token, bot.chat_id) == (TOKEN_A, "42")
    # the masked token of the old form keeps the stored one; the id stays
    again = merge_patch(
        cfg, {"notifiers": {"telegram": {"bot_token": MASK, "chat_id": "43", "enabled": True}}}
    )
    assert [(c.id, c.bot_token, c.chat_id) for c in again.notifiers.telegram.chats] == [
        (bot.id, TOKEN_A, "43")
    ]
    assert again.notifiers.telegram.enabled is True
    # clearing both fields removes the destination
    assert (
        merge_patch(
            again, {"notifiers": {"telegram": {"bot_token": "", "chat_id": ""}}}
        ).notifiers.telegram.chats
        == []
    )


def test_destination_ids_are_generated_and_kept() -> None:
    cfg = merge_patch(
        Config(),
        {
            "notifiers": {
                "telegram": {"chats": [{"chat_id": "1"}, {"id": "keep-me", "chat_id": "2"}, {"chat_id": "3"}]}
            }
        },
    )
    ids = [c.id for c in cfg.notifiers.telegram.chats]
    assert ids[1] == "keep-me" and len(set(ids)) == 3
    assert all(len(i) == 8 and all(ch in "0123456789abcdef" for ch in i) for i in (ids[0], ids[2]))
    assert [c["id"] for c in masked(cfg)["notifiers"]["telegram"]["chats"]] == ids
    with pytest.raises(ValueError, match="have the same id"):
        merge_patch(
            Config(),
            {
                "notifiers": {
                    "telegram": {"chats": [{"id": "abc", "chat_id": "1"}, {"id": "abc", "chat_id": "2"}]}
                }
            },
        )
    with pytest.raises(ValueError, match="not a destination id"):
        merge_patch(Config(), {"notifiers": {"telegram": {"chats": [{"id": "no spaces", "chat_id": "1"}]}}})


def test_every_bot_token_is_a_secret_kept_by_id() -> None:
    cfg = merge_patch(
        Config(),
        {
            "notifiers": {
                "telegram": {
                    "chats": [
                        {"label": "Danindu", "bot_token": TOKEN_A, "chat_id": "111111"},
                        {
                            "label": "Team",
                            "bot_token": TOKEN_B,
                            "chat_id": "-100222",
                            "min_severity": "critical",
                        },
                    ]
                }
            }
        },
    )
    shown = masked(cfg)["notifiers"]["telegram"]["chats"]
    assert [c["bot_token"] for c in shown] == [MASK, MASK]
    assert TOKEN_A not in str(masked(cfg)) and TOKEN_B not in str(masked(cfg))
    # the UI sends the list back reordered, with one label changed: tokens follow their ids
    shown[0]["label"] = "Danindu (phone)"
    again = merge_patch(cfg, {"notifiers": {"telegram": {"chats": [shown[1], shown[0]]}}})
    assert _bots(again) == [("Team", TOKEN_B, "-100222"), ("Danindu (phone)", TOKEN_A, "111111")]
    assert again.notifiers.telegram.chats[0].min_severity == "critical"
    # lists are replaced, not merged: dropping an entry removes it
    assert _bots(merge_patch(cfg, {"notifiers": {"telegram": {"chats": [shown[1]]}}})) == [
        ("Team", TOKEN_B, "-100222")
    ]
    # a masked token needs the id it belongs to
    for broken, name in (
        ({**shown[0], "id": "gone1234"}, "Danindu \\(phone\\)"),
        ({"bot_token": MASK, "chat_id": "5"}, "5"),
    ):
        with pytest.raises(ValueError, match=f"enter the bot token for {name} again"):
            merge_patch(cfg, {"notifiers": {"telegram": {"chats": [broken]}}})


@pytest.mark.parametrize(
    ("chat", "message"),
    [
        ({"chat_id": "12ab"}, "not a Telegram chat id: '12ab'"),
        ({"chat_id": "@abc"}, "not a Telegram chat id"),  # channel names have 5 to 32 characters
        ({"chat_id": "- 5"}, "not a Telegram chat id"),
        (
            {"bot_token": "123:abc"},
            "not a Telegram bot token \\(it looks like 123456789:ABC…, from @BotFather\\)",
        ),
        ({"bot_token": TOKEN_A + " "}, None),  # surrounding spaces are trimmed
        ({"label": "x" * 41}, "at most 40 characters"),
        ({"label": "two\nlines"}, "at most 40 characters on one line"),
        ({"min_severity": "info"}, "min_severity"),
    ],
)
def test_telegram_destination_validation(chat: dict[str, object], message: str | None) -> None:
    patch = {"notifiers": {"telegram": {"chats": [{"chat_id": "42", **chat}]}}}
    if message is None:
        assert merge_patch(Config(), patch).notifiers.telegram.chats[0].bot_token == TOKEN_A
        return
    with pytest.raises(ValueError, match=message) as err:
        merge_patch(Config(), patch)
    assert "123:abc" not in str(err.value)  # a token never appears in an error
    assert "Value error," not in str(err.value)


def test_telegram_limits_and_duplicates() -> None:
    six = [{"bot_token": TOKEN_A, "chat_id": str(n)} for n in range(6)]
    with pytest.raises(ValueError, match="Telegram can send to at most 5 bots"):
        merge_patch(Config(), {"notifiers": {"telegram": {"chats": six}}})
    assert (
        len(merge_patch(Config(), {"notifiers": {"telegram": {"chats": six[:5]}}}).notifiers.telegram.chats)
        == 5
    )
    twice = [
        {"bot_token": TOKEN_A, "chat_id": "42"},
        {"label": "again", "bot_token": TOKEN_A, "chat_id": "42"},
    ]
    with pytest.raises(ValueError, match="is listed twice"):
        merge_patch(Config(), {"notifiers": {"telegram": {"chats": twice}}})
    # two people's bots may talk to the same group
    shared = [{"bot_token": TOKEN_A, "chat_id": "-42"}, {"bot_token": TOKEN_B, "chat_id": "-42"}]
    assert (
        len(merge_patch(Config(), {"notifiers": {"telegram": {"chats": shared}}}).notifiers.telegram.chats)
        == 2
    )
    # enabled without anything to send to is allowed (the UI shows a hint)
    assert merge_patch(
        Config(), {"notifiers": {"telegram": {"enabled": True, "chats": []}}}
    ).notifiers.telegram.enabled


def test_environment_bot_is_read_only_counts_toward_five_and_is_never_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "sentinel.yaml"
    four = [{"label": f"P{n}", "bot_token": TOKEN_A, "chat_id": str(n)} for n in range(1, 5)]
    path.write_text(yaml.safe_dump({"notifiers": {"telegram": {"chats": four}}}))
    monkeypatch.setenv("SENTINEL_TELEGRAM_TOKEN", ENV_TOKEN)
    monkeypatch.setenv("SENTINEL_TELEGRAM_CHAT_ID", "-100999")
    cfg = load_config(path)
    assert [c.id for c in cfg.notifiers.telegram.chats][-1] == ENV_DESTINATION_ID
    shown = masked(cfg)["notifiers"]["telegram"]["chats"]
    assert [c["from_env"] for c in shown] == [False, False, False, False, True]
    assert shown[-1]["label"] == "From environment" and shown[-1]["chat_id"] == "-100999"
    # a fifth bot of the file's own would make six
    fifth = {"label": "P5", "bot_token": TOKEN_B, "chat_id": "5"}
    with pytest.raises(ValueError, match="at most 5 bots"):
        merge_patch(cfg, {"notifiers": {"telegram": {"chats": [*shown[:4], fifth]}}})
    # whatever the UI sends for it, the environment's bot stays and stays out of the file
    edited = merge_patch(
        cfg, {"notifiers": {"telegram": {"chats": [shown[0], {**shown[4], "chat_id": "1"}]}}}
    )
    assert [c.id for c in edited.notifiers.telegram.chats] == [shown[0]["id"], ENV_DESTINATION_ID]
    assert edited.notifiers.telegram.chats[1].chat_id == "-100999"
    save_config(edited, path)
    text = path.read_text()
    assert ENV_TOKEN not in text and "-100999" not in text and "From environment" not in text
    assert [c["label"] for c in yaml.safe_load(text)["notifiers"]["telegram"]["chats"]] == ["P1"]
    # five in the file plus the environment's: the environment's is left out (see below) …
    path.write_text(yaml.safe_dump({"notifiers": {"telegram": {"chats": four + [fifth]}}}))
    assert [c.label for c in load_config(path).notifiers.telegram.chats] == ["P1", "P2", "P3", "P4", "P5"]
    # … but a value that cannot be a chat id still stops the start with the variable named
    monkeypatch.setenv("SENTINEL_TELEGRAM_CHAT_ID", "not-a-chat")
    with pytest.raises(ValueError, match="SENTINEL_TELEGRAM_CHAT_ID is not a Telegram chat id"):
        load_config(path)


ENV_BOT_LEFT_OUT = "the Telegram bot from SENTINEL_TELEGRAM_TOKEN was left out: there are already 5 bots"


def test_a_sixth_bot_from_the_environment_is_left_out_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """R1#extra1: five bots in the file plus SENTINEL_TELEGRAM_TOKEN + SENTINEL_TELEGRAM_CHAT_ID
    stopped Sentinel from starting; a notification limit must never take the monitor down."""
    import logging

    from dspace_sentinel.config import startup_notices

    path = tmp_path / "sentinel.yaml"
    five = [{"label": f"P{n}", "bot_token": TOKEN_A, "chat_id": str(n)} for n in range(1, 6)]
    path.write_text(yaml.safe_dump({"notifiers": {"telegram": {"enabled": True, "chats": five}}}))
    monkeypatch.setenv("SENTINEL_TELEGRAM_TOKEN", ENV_TOKEN)
    monkeypatch.setenv("SENTINEL_TELEGRAM_CHAT_ID", "-100999")
    with caplog.at_level(logging.WARNING, logger="dspace_sentinel.config"):
        cfg = load_config(path)
    assert [c.label for c in cfg.notifiers.telegram.chats] == ["P1", "P2", "P3", "P4", "P5"]
    assert ENV_BOT_LEFT_OUT in caplog.text
    assert startup_notices(cfg) == [
        "The Telegram bot from SENTINEL_TELEGRAM_TOKEN was left out: there are already 5 bots"
    ]
    assert startup_notices(load_config(path, demo=True)) == startup_notices(cfg)
    assert startup_notices(Config()) == []
    # the UI saves the list as it got it: fine, the environment's bot stays out
    shown = masked(cfg)["notifiers"]["telegram"]["chats"]
    kept = merge_patch(cfg, {"notifiers": {"telegram": {"chats": shown, "quiet_hours": "22:00-06:00"}}})
    assert [c.label for c in kept.notifiers.telegram.chats] == ["P1", "P2", "P3", "P4", "P5"]
    # one bot fewer makes room for it again …
    four = merge_patch(cfg, {"notifiers": {"telegram": {"chats": shown[:4]}}})
    assert [c.id for c in four.notifiers.telegram.chats][-1] == ENV_DESTINATION_ID
    # … and then another bot of the file's own would be a sixth: refused, as before
    four_shown = [c for c in masked(four)["notifiers"]["telegram"]["chats"] if not c["from_env"]]
    another = {"label": "P6", "bot_token": TOKEN_B, "chat_id": "6"}
    with pytest.raises(ValueError, match="at most 5 bots .the one from SENTINEL_TELEGRAM_TOKEN"):
        merge_patch(four, {"notifiers": {"telegram": {"chats": [*four_shown, another]}}})
    # six of the file's own are refused whatever the environment says
    with pytest.raises(ValueError, match="Telegram can send to at most 5 bots"):
        merge_patch(cfg, {"notifiers": {"telegram": {"chats": [*shown, another]}}})


def test_environment_bot_that_is_already_configured_is_not_added_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "sentinel.yaml"
    path.write_text(
        yaml.safe_dump({"notifiers": {"telegram": {"chats": [{"bot_token": ENV_TOKEN, "chat_id": "42"}]}}})
    )
    monkeypatch.setenv("SENTINEL_TELEGRAM_TOKEN", ENV_TOKEN)
    monkeypatch.setenv("SENTINEL_TELEGRAM_CHAT_ID", "42")
    assert _bots(load_config(path)) == [("", ENV_TOKEN, "42")]


@pytest.mark.parametrize(
    ("file_bot", "env", "effective"),
    [
        # the documented old set-up: the token in config/sentinel.env, the chat id in the file
        ({"bot_token": "", "chat_id": "42"}, {"SENTINEL_TELEGRAM_TOKEN": ENV_TOKEN}, [(ENV_TOKEN, "42")]),
        # the variables used to override the file: they still do for an old single-bot file
        (
            {"bot_token": TOKEN_A, "chat_id": "42"},
            {"SENTINEL_TELEGRAM_TOKEN": ENV_TOKEN},
            [(ENV_TOKEN, "42")],
        ),
        ({"bot_token": TOKEN_A, "chat_id": ""}, {"SENTINEL_TELEGRAM_CHAT_ID": "42"}, [(TOKEN_A, "42")]),
        (
            {"bot_token": TOKEN_A, "chat_id": "42"},
            {"SENTINEL_TELEGRAM_TOKEN": ENV_TOKEN, "SENTINEL_TELEGRAM_CHAT_ID": "43"},
            [(ENV_TOKEN, "43")],
        ),
        (
            {"bot_token": "", "chat_id": ""},
            {"SENTINEL_TELEGRAM_TOKEN": ENV_TOKEN, "SENTINEL_TELEGRAM_CHAT_ID": "43"},
            [(ENV_TOKEN, "43")],
        ),
    ],
)
def test_old_single_bot_setups_with_environment_keep_working_after_a_ui_save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    file_bot: dict[str, str],
    env: dict[str, str],
    effective: list[tuple[str, str]],
) -> None:
    path = tmp_path / "sentinel.yaml"
    path.write_text(yaml.safe_dump({"notifiers": {"telegram": {"enabled": True, **file_bot}}}))
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    def pairs(cfg: Config) -> list[tuple[str, str]]:
        return [(c.bot_token, c.chat_id) for c in cfg.notifiers.telegram.chats if c.bot_token and c.chat_id]

    cfg = load_config(path)
    assert pairs(cfg) == effective
    # the UI saves the Telegram form as it got it
    tg = masked(cfg)["notifiers"]["telegram"]
    saved = merge_patch(
        cfg, {"notifiers": {"telegram": {"chats": tg["chats"], "quiet_hours": "22:00-06:00"}}}
    )
    assert pairs(saved) == effective
    save_config(saved, path)
    text = path.read_text()
    assert ENV_TOKEN not in text  # an environment value is never written
    assert pairs(load_config(path)) == effective


def test_email_settings_validation() -> None:
    def bad(email: dict[str, object], message: str) -> None:
        with pytest.raises(ValueError, match=message) as err:
            merge_patch(Config(), {"notifiers": {"email": email}})
        assert "Value error," not in str(err.value)

    for address in (
        "Ops <ops@example.org>",
        "ops@example.org, boss@example.org",
        "ops@example",
        "o ps@example.org",
        "ops@example.org\r\nBcc: x@y.z",
        "@example.org",
        "",
    ):
        bad({"recipients": [{"address": address}]}, "not an email address")
    bad(
        {"recipients": [{"address": f"r{n}@example.org"} for n in range(6)]},
        "email can go to at most 5 recipients",
    )
    bad(
        {"recipients": [{"address": "Ops@Example.org"}, {"address": "ops@example.org"}]},
        "ops@example.org is listed twice",
    )
    bad({"recipients": [{"address": "ops@example.org", "label": "x" * 41}]}, "at most 40 characters")
    bad({"recipients": [{"address": "ops@example.org", "min_severity": "all"}]}, "min_severity")
    bad({"smtp_host": "smtp gmail.com"}, "not an SMTP server name")
    bad({"smtp_host": "smtp.gmail.com\r\nRCPT TO:<x@y.z>"}, "not an SMTP server name")
    bad({"smtp_port": 0}, "smtp_port")
    bad({"smtp_port": 70000}, "smtp_port")
    bad({"security": "tls"}, "security")
    bad({"from_address": "Sentinel <s@example.org>"}, "not an email address")
    bad({"from_name": "n" * 61}, "at most 60 characters")
    bad({"from_name": "DSpace\nBcc: x@y.z"}, "at most 60 characters on one line")
    bad({"username": "me@example.org\nx"}, "one line")
    ok = merge_patch(
        Config(),
        {
            "notifiers": {
                "email": {
                    "enabled": True,
                    "smtp_host": " 10.0.0.25 ",
                    "smtp_port": 465,
                    "security": "ssl",
                    "username": " sentinel@gmail.com ",
                    "password": "abcd efgh ijkl mnop",  # Google shows app passwords in four groups
                    "from_name": "Repository monitor",
                    "recipients": [
                        {"address": " Ops@Example.org ", "label": "On call", "min_severity": "critical"},
                        {"address": "boss+alerts@sub.example.ac.lk"},
                    ],
                }
            }
        },
    ).notifiers.email
    assert (
        ok.smtp_host == "10.0.0.25"
        and ok.username == "sentinel@gmail.com"
        and ok.password == "abcdefghijklmnop"
    )
    assert [(r.address, r.label, r.min_severity) for r in ok.recipients] == [
        ("Ops@Example.org", "On call", "critical"),
        ("boss+alerts@sub.example.ac.lk", "", "warning"),
    ]
    # enabled with nobody to write to is allowed (the UI shows a hint)
    assert merge_patch(Config(), {"notifiers": {"email": {"enabled": True}}}).notifiers.email.enabled


def test_smtp_password_is_a_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dspace_sentinel.config import load_demo_state

    cfg = merge_patch(Config(), {"notifiers": {"email": {"password": "abcdefghijklmnop"}}})
    assert masked(cfg)["notifiers"]["email"]["password"] == MASK
    assert masked(Config())["notifiers"]["email"]["password"] == ""
    again = merge_patch(cfg, {"notifiers": {"email": {"password": MASK, "from_name": "Ops"}}})
    assert again.notifiers.email.password == "abcdefghijklmnop" and again.notifiers.email.from_name == "Ops"
    # never saved by, nor read back into, a demo session
    demo_file = tmp_path / "demo-sentinel.yaml"
    save_config(
        merge_patch(apply_demo_overrides(Config()), {"notifiers": {"email": {"password": "demo-pass"}}}),
        demo_file,
    )
    assert "demo-pass" not in demo_file.read_text()
    demo_file.write_text(yaml.safe_dump({"notifiers": {"email": {"password": "x", "from_name": "Kept"}}}))
    state = load_demo_state(apply_demo_overrides(Config()), demo_file)
    assert state.notifiers.email.password == "" and state.notifiers.email.from_name == "Kept"
    # SENTINEL_SMTP_PASSWORD beats the file and is never written into it
    path = tmp_path / "sentinel.yaml"
    path.write_text(yaml.safe_dump({"notifiers": {"email": {"password": "file-pass"}}}))
    monkeypatch.setenv("SENTINEL_SMTP_PASSWORD", "env-pass")
    loaded = load_config(path)
    assert loaded.notifiers.email.password == "env-pass"
    save_config(merge_patch(loaded, {"notifiers": {"email": {"password": MASK, "enabled": True}}}), path)
    assert "env-pass" not in path.read_text() and "file-pass" in path.read_text()


def test_an_smtp_password_from_the_environment_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1#6: a password changed over the API while SENTINEL_SMTP_PASSWORD is set would only last
    until the next restart; the API says where it comes from and refuses to change it."""
    path = tmp_path / "sentinel.yaml"
    login = {
        "smtp_host": "127.0.0.1",
        "smtp_port": 9850,
        "security": "none",
        "username": "sentinel.alerts@gmail.com",
        "password": "filepassword",
        "recipients": [{"address": "ops@example.org"}],
    }
    path.write_text(yaml.safe_dump({"notifiers": {"email": login}}))
    monkeypatch.setenv("SENTINEL_SMTP_PASSWORD", "envpassword")
    cfg = load_config(path)
    assert cfg.notifiers.email.password == "envpassword"
    shown = masked(cfg)["notifiers"]["email"]
    assert shown["password"] == MASK and shown["password_from_env"] is True
    assert masked(load_config(path, demo=True))["notifiers"]["email"]["password_from_env"] is True
    for typed in ("newrotatedpassword", ""):
        with pytest.raises(ValueError) as err:
            merge_patch(cfg, {"notifiers": {"email": {"password": typed}}})
        assert str(err.value) == (
            "the email password comes from SENTINEL_SMTP_PASSWORD in config/sentinel.env: change it there"
        )
    # the rest of the form still saves: a GET → PATCH round trip, or no password at all
    kept = merge_patch(cfg, {"notifiers": {"email": {**shown, "from_name": "Repository monitor"}}})
    assert kept.notifiers.email.password == "envpassword"
    assert kept.notifiers.email.from_name == "Repository monitor"
    assert masked(kept)["notifiers"]["email"]["password_from_env"] is True
    assert (
        merge_patch(cfg, {"notifiers": {"email": {"enabled": True}}}).notifiers.email.password
        == "envpassword"
    )
    # and, like a saved one, it only goes to the server and login it was set for
    for change in ({"smtp_host": "mail.example.org"}, {"username": "someone.else@gmail.com"}):
        with pytest.raises(ValueError) as err:
            merge_patch(cfg, {"notifiers": {"email": {**change, "password": MASK}}})
        assert str(err.value) == (
            "the email password comes from SENTINEL_SMTP_PASSWORD in config/sentinel.env and is only sent "
            "to the server it was set for: change the SMTP server, port, security or username in "
            "config/sentinel.yaml"
        )
    save_config(kept, path)
    assert "envpassword" not in path.read_text() and "filepassword" in path.read_text()
    # without the variable the file's own password is back, and the API may change it
    monkeypatch.delenv("SENTINEL_SMTP_PASSWORD")
    plain = load_config(path)
    assert plain.notifiers.email.password == "filepassword"
    assert masked(plain)["notifiers"]["email"]["password_from_env"] is False
    rotated = merge_patch(plain, {"notifiers": {"email": {"password": "rotatedpassword"}}})
    assert rotated.notifiers.email.password == "rotatedpassword"
    assert masked(Config())["notifiers"]["email"]["password_from_env"] is False


def test_demo_session_never_saves_bot_tokens_or_the_environment_bot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dspace_sentinel.config import load_demo_state

    monkeypatch.setenv("SENTINEL_TELEGRAM_TOKEN", ENV_TOKEN)
    monkeypatch.setenv("SENTINEL_TELEGRAM_CHAT_ID", "42")
    real = tmp_path / "sentinel.yaml"
    demo_file = tmp_path / "demo-sentinel.yaml"
    cfg = load_demo_state(load_config(real, demo=True), demo_file)
    assert [c.id for c in cfg.notifiers.telegram.chats] == [ENV_DESTINATION_ID]
    shown = masked(cfg)["notifiers"]["telegram"]["chats"]
    new = merge_patch(
        cfg,
        {
            "notifiers": {
                "telegram": {"chats": [*shown, {"label": "Me", "bot_token": TOKEN_A, "chat_id": "7"}]}
            }
        },
    )
    save_config(new, demo_file)
    text = demo_file.read_text()
    assert TOKEN_A not in text and ENV_TOKEN not in text and "From environment" not in text
    (saved,) = yaml.safe_load(text)["notifiers"]["telegram"]["chats"]
    assert saved["label"] == "Me" and "bot_token" not in saved
    # an old demo file that still holds a single chat id loads too
    demo_file.write_text(yaml.safe_dump({"notifiers": {"telegram": {"chat_id": "5"}}}))
    old = load_demo_state(load_config(real, demo=True), demo_file)
    assert ("", ENV_TOKEN, "5") in _bots(old)  # the token alone fills in a bot without one


NOT_AN_ADDRESS_LOGIN = (
    "the SMTP username is not an email address: set from_address (the address alerts come from)"
)


@pytest.mark.parametrize("username", ["apikey", "AKIAIOSFODNN7EXAMPLE", "a@b.org, c@d.org", "ops@example"])
def test_a_login_that_is_not_an_address_needs_a_from_address(username: str, tmp_path: Path) -> None:
    """R1#3: SendGrid ("apikey"), Amazon SES (AKIA…) or Postmark logins are not addresses: without a
    from_address nothing could ever be sent, so the configuration says so instead."""
    with pytest.raises(ValueError) as err:
        merge_patch(Config(), {"notifiers": {"email": {"username": username, "password": "x"}}})
    assert str(err.value) == f"invalid configuration: notifiers.email: {NOT_AN_ADDRESS_LOGIN}"
    email = merge_patch(
        Config(),
        {
            "notifiers": {
                "email": {
                    "smtp_host": "email-smtp.eu-west-1.amazonaws.com",
                    "username": username,
                    "password": "x",
                    "from_address": "sentinel@example.org",
                    "recipients": [{"address": "ops@example.org"}],
                }
            }
        },
    ).notifiers.email
    assert email.sender == "sentinel@example.org" and email.ready()
    # a file that holds such a login starts all the same: see
    # test_a_saved_login_that_is_not_an_address_never_stops_the_start
    path = tmp_path / "sentinel.yaml"
    path.write_text(yaml.safe_dump({"notifiers": {"email": {"username": username, "password": "x"}}}))
    assert not load_config(path).notifiers.email.server_ready()


EMAIL_NOT_READY = f"Email is not ready: {NOT_AN_ADDRESS_LOGIN}"


@pytest.mark.parametrize("enabled", [False, True])
def test_a_saved_login_that_is_not_an_address_never_stops_the_start(
    enabled: bool, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """notify-validation#2: the old API saved {smtp.sendgrid.net, apikey, a password} with no From
    address, and the old form asked for one only while the switch was on. Checked at load, that
    file ended serve, doctor and start with exit 4: a channel that is switched off took the
    monitor down. Now email is simply not ready, and the start says why."""
    import logging

    from dspace_sentinel.config import startup_notices

    path = tmp_path / "sentinel.yaml"
    sendgrid = {
        "enabled": enabled,
        "smtp_host": "smtp.sendgrid.net",
        "username": "apikey",
        "password": "SG.secret-key",
        "recipients": [{"address": "ops@example.org"}],
    }
    path.write_text(yaml.safe_dump({"setup": {"completed": True}, "notifiers": {"email": sendgrid}}))
    before = path.read_text()
    with caplog.at_level(logging.WARNING, logger="dspace_sentinel.config"):
        cfg = load_config(path)
    email = cfg.notifiers.email
    assert (email.enabled, email.username, email.password, email.from_address) == (
        enabled,
        "apikey",
        "SG.secret-key",
        "",
    )
    assert cfg.setup.completed and not email.server_ready() and not email.ready()
    assert email.problem() == NOT_AN_ADDRESS_LOGIN
    assert startup_notices(cfg) == [EMAIL_NOT_READY]
    assert [r.getMessage() for r in caplog.records] == [f"email is not ready: {NOT_AN_ADDRESS_LOGIN}"]
    assert "SG.secret-key" not in caplog.text
    assert startup_notices(load_config(path, demo=True)) == [EMAIL_NOT_READY]
    assert path.read_text() == before
    # everything else can still be saved through the API …
    themed = merge_patch(cfg, {"appearance": {"theme": "dark"}, "notifiers": {"telegram": {"enabled": True}}})
    assert themed.appearance.theme == "dark" and themed.notifiers.email.username == "apikey"
    # … but the email settings only with an address to send from: PATCH is as strict as before
    shown = masked(cfg)["notifiers"]["email"]
    for patch in (shown, {"enabled": True}, {"from_address": ""}, {"recipients": []}):
        with pytest.raises(ValueError) as err:
            merge_patch(cfg, {"notifiers": {"email": patch}})
        assert str(err.value) == f"invalid configuration: notifiers.email: {NOT_AN_ADDRESS_LOGIN}"
    fixed = merge_patch(cfg, {"notifiers": {"email": {**shown, "from_address": "alerts@example.org"}}})
    assert fixed.notifiers.email.ready() and fixed.notifiers.email.problem() is None
    assert fixed.notifiers.email.password == "SG.secret-key"
    save_config(fixed, path)
    assert startup_notices(load_config(path)) == []
    # a login that is an address, or a relay without a login, was never a problem
    for fine in ({"username": "me@gmail.com"}, {"username": "", "from_address": "noreply@example.org"}):
        path.write_text(yaml.safe_dump({"notifiers": {"email": {**sendgrid, **fine}}}))
        assert startup_notices(load_config(path)) == []


def test_a_saved_demo_session_with_such_a_login_is_not_dropped_whole(tmp_path: Path) -> None:
    from dspace_sentinel.config import load_demo_state, startup_notices

    real = tmp_path / "sentinel.yaml"
    real.write_text("{}\n")
    state = tmp_path / "demo-sentinel.yaml"
    session = {
        "setup": {"completed": True, "persona": "full"},
        "appearance": {"theme": "dark"},
        "notifiers": {"email": {"smtp_host": "smtp.sendgrid.net", "username": "apikey"}},
    }
    state.write_text(yaml.safe_dump(session))
    cfg = load_demo_state(load_config(real, demo=True), state)
    assert cfg.setup.completed and cfg.appearance.theme == "dark"
    assert cfg.notifiers.email.username == "apikey" and not cfg.notifiers.email.server_ready()
    assert startup_notices(cfg) == [EMAIL_NOT_READY]
    # the session gave the login its From address later: nothing to say any more
    session["notifiers"]["email"]["from_address"] = "alerts@example.org"
    state.write_text(yaml.safe_dump(session))
    assert startup_notices(load_demo_state(load_config(real, demo=True), state)) == []
    # the real file has the problem and the demo session solved it: the notice does not stay
    real.write_text(yaml.safe_dump({"notifiers": {"email": {"username": "apikey"}}}))
    base = load_config(real, demo=True)
    assert startup_notices(base) == [EMAIL_NOT_READY]
    assert startup_notices(load_demo_state(base, state)) == []


def test_email_is_only_ready_with_an_address_to_send_from() -> None:
    """Belt and braces for a configuration built without validation."""
    from dspace_sentinel.config import EmailConfig, EmailRecipient

    email = EmailConfig.model_construct(
        smtp_host="smtp.example.org",
        username="apikey",
        password="x",
        from_address="",
        recipients=[EmailRecipient(address="ops@example.org")],
    )
    assert email.sender == "apikey" and not email.server_ready() and not email.ready()


def test_when_email_is_ready_to_send() -> None:
    def ready(**email: object) -> tuple[bool, bool]:
        cfg = merge_patch(
            Config(), {"notifiers": {"email": {"recipients": [{"address": "ops@example.org"}], **email}}}
        )
        return cfg.notifiers.email.server_ready(), cfg.notifiers.email.ready()

    assert ready(username="me@gmail.com", password="abcdefghijklmnop") == (True, True)
    assert ready(username="me@gmail.com") == (False, False)  # Gmail needs the app password
    assert ready() == (False, False)  # nobody to send as
    # a relay that needs no login only needs the sender address
    assert ready(smtp_host="relay.example.org", security="none", from_address="noreply@example.org") == (
        True,
        True,
    )
    assert (
        merge_patch(
            Config(), {"notifiers": {"email": {"username": "a@b.org", "password": "x"}}}
        ).notifiers.email.ready()
        is False
    )


NOT_SENDABLE = (
    "the email password has a character that cannot be sent: "
    "use letters, digits and symbols from the English keyboard"
)


def test_a_password_smtp_cannot_send_is_refused_when_saved() -> None:
    """email-password#3: smtplib sends the login as ASCII, so "pässwort-123" was saved and then
    could never log in. Saving it is refused with the sentence the test email says, with a login
    or without one; the mask still keeps the stored password."""
    login = {"username": "sentinel@example.org", "recipients": [{"address": "ops@example.org"}]}
    for typed in ("pässwort-123", "hunter2 ✓", f"{MASK}x", f" {MASK} "):
        for section in (login, {}):
            with pytest.raises(ValueError) as err:
                merge_patch(Config(), {"notifiers": {"email": {**section, "password": typed}}})
            assert str(err.value) == f"invalid configuration: notifiers.email: {NOT_SENDABLE}"
    saved = merge_patch(Config(), {"notifiers": {"email": {**login, "password": "abcdefghijklmnop"}}})
    kept = merge_patch(saved, {"notifiers": {"email": {"password": MASK, "from_name": "Ops"}}})
    assert kept.notifiers.email.password == "abcdefghijklmnop" and kept.notifiers.email.ready()
    # a login name SMTP cannot send is refused as well, naming the username
    with pytest.raises(ValueError) as err:
        merge_patch(saved, {"notifiers": {"email": {"username": "bénédicte@example.org", "password": "x"}}})
    assert str(err.value) == (
        "invalid configuration: notifiers.email: the SMTP username has a character that cannot be "
        "sent: use letters, digits and symbols from the English keyboard"
    )


@pytest.mark.parametrize("source", ["file", "environment"])
def test_a_password_smtp_cannot_send_never_stops_the_start(
    source: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """email-password#3: such a password in config/sentinel.yaml or SENTINEL_SMTP_PASSWORD loads:
    email is not ready, and the start says why in the same sentence."""
    import logging

    from dspace_sentinel.config import startup_notices

    path = tmp_path / "sentinel.yaml"
    email: dict[str, Any] = {
        "enabled": True,
        "username": "sentinel@example.org",
        "recipients": [{"address": "ops@example.org"}],
    }
    if source == "file":
        email["password"] = "pässwort-123"
    else:
        monkeypatch.setenv("SENTINEL_SMTP_PASSWORD", "pässwort-123")
    tree = {"setup": {"completed": True}, "notifiers": {"email": email}}
    path.write_text(yaml.safe_dump(tree, allow_unicode=True), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="dspace_sentinel.config"):
        cfg = load_config(path)
    loaded = cfg.notifiers.email
    assert cfg.setup.completed and loaded.enabled and loaded.password == "pässwort-123"
    assert loaded.problem() == NOT_SENDABLE and not loaded.server_ready() and not loaded.ready()
    assert startup_notices(cfg) == [f"Email is not ready: {NOT_SENDABLE}"]
    assert [r.getMessage() for r in caplog.records] == [f"email is not ready: {NOT_SENDABLE}"]
    assert "pässwort" not in caplog.text
    assert merge_patch(cfg, {"appearance": {"theme": "dark"}}).appearance.theme == "dark"
    shown = masked(cfg)["notifiers"]["email"]
    if source == "file":
        # the email settings save again only with a password that can be sent
        for patch in (shown, {"enabled": False}):
            with pytest.raises(ValueError) as err:
                merge_patch(cfg, {"notifiers": {"email": patch}})
            assert str(err.value) == f"invalid configuration: notifiers.email: {NOT_SENDABLE}"
        fixed = merge_patch(cfg, {"notifiers": {"email": {**shown, "password": "abcdefghijklmnop"}}})
        assert fixed.notifiers.email.ready() and fixed.notifiers.email.problem() is None
    else:
        # a PATCH can neither set nor mend a password from config/sentinel.env: the rest still saves
        off = merge_patch(cfg, {"notifiers": {"email": {**shown, "enabled": False}}})
        assert off.notifiers.email.enabled is False and off.notifiers.email.problem() == NOT_SENDABLE


def test_the_mask_in_the_settings_file_is_no_password(tmp_path: Path) -> None:
    """email-password#3: the mask the UI shows ("••••••") pasted into config/sentinel.yaml as the
    password was sent to the mail server as the password itself. It means no password."""
    from dspace_sentinel.alerts.notifiers import email as mail
    from dspace_sentinel.config import startup_notices

    path = tmp_path / "sentinel.yaml"
    for pasted in (MASK, f"  {MASK} "):
        email = {
            "username": "sentinel@example.org",
            "password": pasted,
            "recipients": [{"address": "ops@example.org"}],
        }
        path.write_text(yaml.safe_dump({"notifiers": {"email": email}}, allow_unicode=True), encoding="utf-8")
        cfg = load_config(path)
        assert cfg.notifiers.email.password == "" and not cfg.notifiers.email.ready()
        assert cfg.notifiers.email.problem() is None and startup_notices(cfg) == []
        assert masked(cfg)["notifiers"]["email"]["password"] == ""
        assert mail.unready(cfg) == mail.NOT_SET_UP  # "… add the Gmail address, app password …"


def test_a_bot_sent_back_without_its_token_keeps_it() -> None:
    """A client that leaves the secret out of an entry (rather than sending the mask) must not
    wipe the token; a new entry without one simply has none yet."""
    cfg = merge_patch(
        Config(),
        {"notifiers": {"telegram": {"chats": [{"label": "A", "bot_token": TOKEN_A, "chat_id": "1"}]}}},
    )
    bot_id = cfg.notifiers.telegram.chats[0].id
    renamed = merge_patch(
        cfg,
        {
            "notifiers": {
                "telegram": {"chats": [{"id": bot_id, "label": "Renamed", "chat_id": "1"}, {"chat_id": "2"}]}
            }
        },
    )
    assert _bots(renamed) == [("Renamed", TOKEN_A, "1"), ("", "", "2")]
    # an explicit empty token does clear it
    cleared = merge_patch(
        cfg, {"notifiers": {"telegram": {"chats": [{"id": bot_id, "bot_token": "", "chat_id": "1"}]}}}
    )
    assert _bots(cleared) == [("", "", "1")]


@pytest.mark.parametrize("channel", ["telegram", "email"])
def test_quiet_hours_and_digest_times_are_checked(channel: str, tmp_path: Path) -> None:
    """R1#5: a window or time the notifiers cannot read used to be saved and silently ignored
    (warnings all night, a digest that never comes); now it is read or refused."""
    import datetime as _dt

    from dspace_sentinel.alerts.notifiers import digest_due_ts, suppressed_by_quiet_hours
    from dspace_sentinel.config import startup_notices

    def saved(**values: str) -> Any:
        return getattr(merge_patch(Config(), {"notifiers": {channel: values}}).notifiers, channel)

    for typed, stored in (
        ("23:00-07:00", "23:00-07:00"),
        ("23:00–07:00", "23:00-07:00"),  # an en dash, as pasted from the docs
        ("23:00 — 07:00", "23:00-07:00"),
        (" 23:00 - 07:00 ", "23:00-07:00"),
        ("7:00-9:30", "07:00-09:30"),
        ("", ""),
    ):
        assert saved(quiet_hours=typed).quiet_hours == stored, typed
    for bad in (
        "11pm-7am",
        "23:00 to 07:00",
        "23:00",
        "24:00-07:00",
        "23:60-07:00",
        "23-07",
        "7:0\u0660-9:3\u0660",  # one Arabic-Indic zero: the forms and the docs promise ASCII HH:MM
        "\u0667:\u0660\u0660-\u0669:\u0663\u0660",
        "\uff12\uff13:\uff10\uff10-\uff10\uff17:\uff10\uff10",  # full-width digits
        "23:00" + " " * 30 + "-07:00",  # 41 characters: longer than any window
    ):
        with pytest.raises(ValueError) as err:
            saved(quiet_hours=bad)
        assert str(err.value) == (
            f"invalid configuration: notifiers.{channel}.quiet_hours: quiet hours must look like 23:00-07:00"
        ), bad
    assert saved(quiet_hours="23:00" + " " * 29 + "-07:00").quiet_hours == "23:00-07:00"  # 40 characters
    # a window that starts and ends at the same minute was saved and never quiet
    for same in ("23:00-23:00", "00:00-00:00", "9:00 – 09:00"):
        with pytest.raises(ValueError) as err:
            saved(quiet_hours=same)
        assert str(err.value) == (
            f"invalid configuration: notifiers.{channel}.quiet_hours: "
            "quiet hours must start and end at different times"
        ), same
    assert saved(quiet_hours="00:00-23:59").quiet_hours == "00:00-23:59"
    assert saved(quiet_hours="23:00-22:59").quiet_hours == "23:00-22:59"
    for typed, stored in (("08:00", "08:00"), ("8:00", "08:00"), (" 07:30 ", "07:30"), ("", "")):
        assert saved(daily_digest=typed).daily_digest == stored, typed
    for bad in ("8 am", "08.00", "8h", "25:00", "08:00:00", "8:0\u0660", "\u0668:\u0660\u0660"):
        with pytest.raises(ValueError) as err:
            saved(daily_digest=bad)
        assert str(err.value) == (
            f"invalid configuration: notifiers.{channel}.daily_digest: "
            "the daily digest time must look like 08:00"
        ), bad
    # the notifiers honour what was saved
    cfg = merge_patch(
        Config(), {"notifiers": {channel: {"quiet_hours": "23:00–07:00", "daily_digest": "8:00"}}}
    )
    night = _dt.datetime(2024, 3, 5, 2, 0).timestamp()
    assert suppressed_by_quiet_hours(cfg, {"kind": "alert", "severity": "warning"}, night, channel)
    assert digest_due_ts(getattr(cfg.notifiers, channel).daily_digest, night) is not None
    # a file with a value that cannot be read says so at start-up, and Sentinel starts
    # (test_a_time_that_cannot_be_read_never_stops_the_start)
    path = tmp_path / "sentinel.yaml"
    path.write_text(yaml.safe_dump({"notifiers": {channel: {"quiet_hours": "11pm-7am"}}}))
    assert "quiet hours must look like 23:00-07:00" in startup_notices(load_config(path))[0]


@pytest.mark.parametrize("channel", ["telegram", "email"])
@pytest.mark.parametrize("key", ["quiet_hours", "daily_digest"])
def test_a_very_long_time_is_refused_at_once(channel: str, key: str) -> None:
    """notify-validation#3: the quiet-hours pattern backtracked quadratically (4 s for 24,000
    characters, on the event loop: no request answered and no check run meanwhile). A value
    longer than any time is now refused before it is matched, and the pattern is linear."""
    import time

    from dspace_sentinel import config as config_module

    message = {
        "quiet_hours": "quiet hours must look like 23:00-07:00",
        "daily_digest": "the daily digest time must look like 08:00",
    }[key]
    size = 100_000
    for value in (
        "-" * size + " x y",
        "a-" * (size // 2) + " x",
        "1-" * (size // 2) + " to 7",
        "a" * size,
        "a-" + " " * size + "-b",
        "23:00-" + " " * size + "07:00x",
        "1" * size + ":00",
    ):
        started = time.perf_counter()
        with pytest.raises(ValueError) as err:
            merge_patch(Config(), {"notifiers": {channel: {key: value}}})
        took = time.perf_counter() - started
        assert str(err.value) == f"invalid configuration: notifiers.{channel}.{key}: {message}"
        assert took < 0.5, f"{took:.2f} s for {value[:12]!r}… ({len(value)} characters)"
        # the pattern by itself is linear too: the length check is not its only guard
        started = time.perf_counter()
        assert config_module._QUIET_WINDOW.match(value) is None
        assert time.perf_counter() - started < 0.5, value[:12]


QUIET_LOOK = "quiet hours must look like 23:00-07:00"
QUIET_EQUAL = "quiet hours must start and end at different times"
DIGEST_LOOK = "the daily digest time must look like 08:00"


@pytest.mark.parametrize("channel", ["telegram", "email"])
def test_a_time_that_cannot_be_read_never_stops_the_start(
    channel: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """notify-validation#1: until the times were checked, the API, a hand-edited file and the
    Ansible role accepted '11pm-7am' or '8 am' and the notifiers ignored them. Checked at load,
    such a file ended serve, doctor and start with exit 4: after an update nothing monitored the
    server. Like the sixth bot, a notification setting never stops the start."""
    import logging

    from dspace_sentinel.config import startup_notices

    path = tmp_path / "sentinel.yaml"
    settings = {
        "setup": {"completed": True},
        "appearance": {"theme": "dark"},
        "notifiers": {channel: {"enabled": True, "quiet_hours": "11pm-7am", "daily_digest": "8 am"}},
    }
    path.write_text(yaml.safe_dump(settings))
    before = path.read_text()
    with caplog.at_level(logging.WARNING, logger="dspace_sentinel.config"):
        cfg = load_config(path)
    section = getattr(cfg.notifiers, channel)
    assert (section.quiet_hours, section.daily_digest) == ("", "")  # what they meant until now
    assert section.enabled and cfg.setup.completed and cfg.appearance.theme == "dark"  # the rest is kept
    notices = [
        f"The quiet hours '11pm-7am' in notifiers.{channel}.quiet_hours were ignored: {QUIET_LOOK}",
        f"The daily digest time '8 am' in notifiers.{channel}.daily_digest was ignored: {DIGEST_LOOK}",
    ]
    assert startup_notices(cfg) == notices
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [n[0].lower() + n[1:] for n in notices]
    assert path.read_text() == before  # loading never rewrites the file
    assert startup_notices(load_config(path, demo=True)) == notices  # the demo starts as well
    # saving through the API stays strict
    for key, value, message in (
        ("quiet_hours", "11pm-7am", QUIET_LOOK),
        ("daily_digest", "8 am", DIGEST_LOOK),
    ):
        with pytest.raises(ValueError) as err:
            merge_patch(cfg, {"notifiers": {channel: {key: value}}})
        assert str(err.value) == f"invalid configuration: notifiers.{channel}.{key}: {message}"
    # the next save writes what is in use, so the unreadable values leave the file with it
    save_config(merge_patch(cfg, {"appearance": {"theme": "light"}}), path)
    written = yaml.safe_load(path.read_text())["notifiers"][channel]
    assert (written["enabled"], written["quiet_hours"], written["daily_digest"]) == (True, "", "")
    assert startup_notices(load_config(path)) == []


@pytest.mark.parametrize(
    ("key", "value", "shown", "reason"),
    [
        ("quiet_hours", "23:00 to 07:00", "'23:00 to 07:00'", QUIET_LOOK),
        ("quiet_hours", "24:00-07:00", "'24:00-07:00'", QUIET_LOOK),
        ("quiet_hours", "23:0-7:0", "'23:0-7:0'", QUIET_LOOK),
        ("quiet_hours", "7:0\u0660-9:3\u0660", "'7:0\u0660-9:3\u0660'", QUIET_LOOK),
        ("quiet_hours", "23:00-23:00", "'23:00-23:00'", QUIET_EQUAL),  # notify-validation#4 at load
        ("quiet_hours", "0:00 \u2013 00:00", "'0:00 \u2013 00:00'", QUIET_EQUAL),
        ("quiet_hours", ["23:00", "07:00"], "['23:00', '07:00']", QUIET_LOOK),
        ("quiet_hours", True, "True", QUIET_LOOK),
        pytest.param("quiet_hours", "x" * 100_000, "'" + "x" * 40 + "'…", QUIET_LOOK, id="100000-characters"),
        ("daily_digest", "08.00", "'08.00'", DIGEST_LOOK),
        ("daily_digest", "8:0", "'8:0'", DIGEST_LOOK),
        ("daily_digest", "25:00", "'25:00'", DIGEST_LOOK),
        ("daily_digest", {"at": "08:00"}, "{'at': '08:00'}", DIGEST_LOOK),
        ("daily_digest", False, "False", DIGEST_LOOK),
    ],
)
def test_every_unreadable_time_in_the_file_is_named_and_left_out(
    key: str, value: Any, shown: str, reason: str, tmp_path: Path
) -> None:
    from dspace_sentinel.config import startup_notices

    what = "quiet hours" if key == "quiet_hours" else "daily digest time"
    verb = "were" if key == "quiet_hours" else "was"
    path = tmp_path / "sentinel.yaml"
    for channel in ("telegram", "email"):
        other = "email" if channel == "telegram" else "telegram"
        fine = {"quiet_hours": "7:00 \u2013 9:30", "daily_digest": "8:00"}
        path.write_text(
            yaml.safe_dump({"notifiers": {channel: {key: value}, other: fine}}, allow_unicode=True)
        )
        cfg = load_config(path)
        assert getattr(getattr(cfg.notifiers, channel), key) == ""
        assert startup_notices(cfg) == [
            f"The {what} {shown} in notifiers.{channel}.{key} {verb} ignored: {reason}"
        ]
        # the other channel's readable times are untouched (and normalised as always)
        kept = getattr(cfg.notifiers, other)
        assert (kept.quiet_hours, kept.daily_digest) == ("07:00-09:30", "08:00")


def test_a_saved_demo_session_with_an_unreadable_time_is_not_dropped_whole(tmp_path: Path) -> None:
    """notify-validation#1: such a session lost everything silently (onboarding again, the theme
    and the thresholds gone); now only the time is left out, and that is said."""
    from dspace_sentinel.config import load_demo_state, startup_notices

    real = tmp_path / "sentinel.yaml"
    real.write_text("{}\n")
    state = tmp_path / "demo-sentinel.yaml"
    session = {
        "setup": {"completed": True, "persona": "full"},
        "appearance": {"theme": "dark"},
        "alerts": {"cpu": {"warning": 70, "critical": 90}},
        "notifiers": {
            "telegram": {"quiet_hours": "11pm-7am", "daily_digest": "07:30"},
            "email": {"daily_digest": "8 am", "quiet_hours": "22:00-06:00"},
        },
    }
    state.write_text(yaml.safe_dump(session))
    before = state.read_text()
    cfg = load_demo_state(load_config(real, demo=True), state)
    assert cfg.demo and cfg.setup.completed and cfg.setup.persona == "full"
    assert cfg.appearance.theme == "dark" and cfg.alerts.cpu.warning == 70
    telegram, email = cfg.notifiers.telegram, cfg.notifiers.email
    assert (telegram.quiet_hours, telegram.daily_digest) == ("", "07:30")
    assert (email.quiet_hours, email.daily_digest) == ("22:00-06:00", "")
    assert startup_notices(cfg) == [
        f"The quiet hours '11pm-7am' in notifiers.telegram.quiet_hours were ignored: {QUIET_LOOK}",
        f"The daily digest time '8 am' in notifiers.email.daily_digest was ignored: {DIGEST_LOOK}",
    ]
    assert state.read_text() == before
    # a session that cannot be used for another reason is still dropped whole, without a notice
    state.write_text(yaml.safe_dump({**session, "collector": {"system_interval": 1}}))
    base = load_config(real, demo=True)
    assert load_demo_state(base, state) is base and startup_notices(base) == []


QUOTE_HINT = " (YAML reads an unquoted time as a number: write it in quotes)"


@pytest.mark.parametrize("channel", ["telegram", "email"])
def test_an_unquoted_digest_time_is_read_as_the_time_it_was_written_as(channel: str, tmp_path: Path) -> None:
    """notify-validation#5: YAML 1.1 reads an unquoted 18:00 as the base-60 number 1080, and the
    file was refused with "the daily digest time must look like 08:00" for a line that read
    `daily_digest: 18:00`. A number YAML makes of a time of day (1:00 … 23:59 → 60 … 1439) is
    read back as that time; any other number is left out and named, with the way to write it."""
    from dspace_sentinel.config import startup_notices

    path = tmp_path / "sentinel.yaml"

    def load(written: str) -> Config:
        path.write_text(f"notifiers:\n  {channel}:\n    daily_digest: {written}\n")
        return load_config(path)

    for written, stored in (
        ("18:00", "18:00"),  # YAML: 1080
        ("9:05", "09:05"),  # 545
        ("1:00", "01:00"),  # 60, the smallest number a time becomes
        ("23:59", "23:59"),  # 1439, the largest
        ("08:00", "08:00"),  # a leading zero keeps it a string
        ("0:30", "00:30"),
        ('"18:00"', "18:00"),
        ("'7:45'", "07:45"),
    ):
        cfg = load(written)
        assert getattr(cfg.notifiers, channel).daily_digest == stored, written
        assert startup_notices(cfg) == [], written
    for written, shown in (
        ("24:00", "1440"),
        ("1800", "1800"),
        ("8", "8"),  # 8 o'clock, or eight minutes past midnight? Never guessed
        ("0", "0"),
        ("59", "59"),
        ("-60", "-60"),
        ("18.30", "18.3"),
    ):
        cfg = load(written)
        assert getattr(cfg.notifiers, channel).daily_digest == "", written
        assert startup_notices(cfg) == [
            f"The daily digest time {shown} in notifiers.{channel}.daily_digest was ignored: "
            f"{DIGEST_LOOK}{QUOTE_HINT}"
        ], written
    cfg = load("yes")  # a YAML boolean is not a number
    assert startup_notices(cfg) == [
        f"The daily digest time True in notifiers.{channel}.daily_digest was ignored: {DIGEST_LOOK}"
    ]
    # the API is as strict as before: JSON has no base-60 numbers, so a number is a mistake
    for number in (1080, 600, 0, 18.5, True):
        with pytest.raises(ValueError) as err:
            merge_patch(Config(), {"notifiers": {channel: {"daily_digest": number}}})
        assert str(err.value) == f"invalid configuration: notifiers.{channel}.daily_digest: {DIGEST_LOOK}"


def test_times_are_saved_in_quotes_so_a_hand_edit_keeps_them(tmp_path: Path) -> None:
    """notify-validation#5: Sentinel saved `daily_digest: 08:00` without quotes; someone who then
    changed that line to 10:00 by hand had written the number 600."""
    from dspace_sentinel.config import load_demo_state

    path = tmp_path / "sentinel.yaml"
    path.write_text("setup: {completed: true}\n")
    patch = {
        "notifiers": {
            "telegram": {"quiet_hours": "23:00 – 7:00", "daily_digest": "8:00"},
            "email": {"daily_digest": "18:30"},
        }
    }
    save_config(merge_patch(load_config(path), patch), path)
    lines = path.read_text().splitlines()
    assert lines.count('    quiet_hours: "23:00-07:00"') == 1 and lines.count('    quiet_hours: ""') == 1
    assert lines.count('    daily_digest: "08:00"') == 1 and lines.count('    daily_digest: "18:30"') == 1
    again = load_config(path)
    assert again.notifiers.telegram.quiet_hours == "23:00-07:00"
    assert (again.notifiers.telegram.daily_digest, again.notifiers.email.daily_digest) == ("08:00", "18:30")
    assert again.notifiers.email.quiet_hours == ""
    # the hand edit, inside the quotes Sentinel wrote
    path.write_text(path.read_text().replace('"08:00"', '"10:00"'))
    assert load_config(path).notifiers.telegram.daily_digest == "10:00"
    # a saved demo session is written the same way
    demo_file = tmp_path / "demo-sentinel.yaml"
    demo = load_demo_state(load_config(path, demo=True), demo_file)
    save_config(merge_patch(demo, {"notifiers": {"email": {"quiet_hours": "22:00-06:00"}}}), demo_file)
    assert '    quiet_hours: "22:00-06:00"' in demo_file.read_text().splitlines()
    assert load_demo_state(load_config(path, demo=True), demo_file).notifiers.email.quiet_hours == (
        "22:00-06:00"
    )
    # a configuration built in code (no file layer) too
    built = tmp_path / "built.yaml"
    save_config(merge_patch(Config(), patch), built)
    assert '    daily_digest: "08:00"' in built.read_text().splitlines()
    assert yaml.safe_load(built.read_text()) == merge_patch(Config(), patch).to_yaml_dict()
