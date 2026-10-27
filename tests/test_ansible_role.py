"""The Ansible role writes a ``config/sentinel.yaml`` (and ``config/sentinel.env``) that Sentinel's
own loader reads the way the role variables meant.

Every scenario is rendered by two renderers:

* ``jinja2``: the role's templates with plain Jinja2 and stand-ins for the three Ansible filters
  they use (``bool``, ``combine``, ``to_nice_yaml``), so these tests run wherever the app's
  tests run (CI's test job has no Ansible);
* ``ansible``: one real ``ansible-playbook`` run (skipped without ansible-core 2.15+) in which
  every scenario is an inventory host. That run uses the role defaults, ``host_vars``, a
  vault-encrypted ``group_vars`` file and inline ``!vault`` values, and it puts the role's
  ``tasks/checks.yml`` in front of the templates exactly as ``tasks/main.yml`` does.

The ``ansible`` run also shows that the stand-ins render what Ansible renders, and that the
checks refuse settings Sentinel would reject or silently ignore. CI's ansible-lint job covers
the rest of the tasks.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import string
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jinja2
import pytest
import yaml

from dspace_sentinel.api.app import UnsafeBindError, allowed_hosts, check_bind, host_allowed
from dspace_sentinel.auth import hash_password, login_active, verify_password
from dspace_sentinel.cli import load_env_file
from dspace_sentinel.config import MODULE_IDS, Config, load_config, startup_notices

ROLE = Path(__file__).resolve().parents[1] / "deploy" / "ansible" / "roles" / "dspace_sentinel"

BOT_A = "123456789:" + "A" * 35
BOT_B = "987654321:" + "b1_-" * 9
BOT_C = "4455667:" + "C" * 40
APP_PASSWORD = "abcdefghijklmnop"
"""A Gmail app password. Google shows it in four groups ("abcd efgh ijkl mnop")."""

LOGIN_PASSWORD = "correct horse battery"
LOGIN_HASH = hash_password(LOGIN_PASSWORD)
"""What ``./sentinel.sh hash-password`` prints for the login's password."""

TELEGRAM_KEYS = {"enabled", "chats", "quiet_hours", "daily_digest"}
"""The keys of the rendered ``notifiers.telegram``: never the old single ``bot_token``/``chat_id``."""


def _bot(n: int) -> dict[str, Any]:
    return {"label": f"Bot {n}", "bot_token": f"{100000 + n}:" + "x" * 35, "chat_id": str(1000 + n)}


SCENARIOS: dict[str, dict[str, Any]] = {
    "defaults": {},
    "repo.example.org": {
        "dspace_sentinel_nginx_snippet_enabled": True,
        "dspace_sentinel_allowed_hosts": ["sentinel.example.org", "repo.example.org"],
    },
    "open-bind-allowed": {"dspace_sentinel_host": "0.0.0.0", "dspace_sentinel_allow_unauthenticated": True},
    "open-bind-login": {"dspace_sentinel_host": "0.0.0.0", "dspace_sentinel_login_password_hash": LOGIN_HASH},
    "login": {"dspace_sentinel_login_user": "ops.admin", "dspace_sentinel_login_password_hash": LOGIN_HASH},
    "several-bots": {
        "dspace_sentinel_telegram": {"enabled": True, "quiet_hours": "23:00-07:00", "daily_digest": "08:00"},
        "dspace_sentinel_telegram_bots": [
            {"label": "Danindu", "bot_token": BOT_A, "chat_id": "123456789", "min_severity": "warning"},
            {"label": "Team", "bot_token": BOT_B, "chat_id": -1001234567890, "min_severity": "critical"},
            {"id": "night-shift", "bot_token": BOT_C, "chat_id": "@dspace_alerts"},
        ],
    },
    # An inventory written for the single-bot role.
    "old-single-bot": {
        "dspace_sentinel_telegram": {
            "enabled": True,
            "chat_id": "-1001234567890",
            "quiet_hours": "23:00-07:00",
            "daily_digest": "08:00",
        },
        "dspace_sentinel_telegram_bot_token": BOT_A,
    },
    # The single-bot role's documented alternative: the token in config/sentinel.env.
    "old-token-in-env": {
        "dspace_sentinel_telegram": {
            "enabled": True,
            "chat_id": "123456789",
            "quiet_hours": "",
            "daily_digest": "",
        },
        "dspace_sentinel_env": {"SENTINEL_TELEGRAM_TOKEN": BOT_A},
    },
    "email": {
        "dspace_sentinel_email": {
            "enabled": True,
            "smtp_host": "smtp.gmail.com",
            "smtp_port": 465,
            "security": "ssl",
            "username": "sentinel.alerts@gmail.com",
            "from_address": "",
            "from_name": "Repository monitor",
            "quiet_hours": "23:00-07:00",
            "daily_digest": "07:30",
        },
        "dspace_sentinel_email_recipients": [
            {"address": "ops@example.org", "label": "On call"},
            {"address": "director@example.org", "label": "Director", "min_severity": "critical"},
        ],
        "dspace_sentinel_email_password": "abcd efgh ijkl mnop",
        "dspace_sentinel_reports": {
            "weekly": {"enabled": True, "weekday": 1, "hour": 8, "send_telegram": False, "send_email": True},
            "default_sections": ["summary", "alerts", "recommendations"],
        },
    },
    # Sections copied from config/sentinel.example.yaml: their empty chats, password and
    # recipients are harmless (the role's own variables fill them in).
    "copied-from-the-example-config": {
        "dspace_sentinel_telegram": {"enabled": False, "chats": [], "quiet_hours": "", "daily_digest": ""},
        "dspace_sentinel_email": {
            "enabled": False,
            "smtp_host": "smtp.gmail.com",
            "smtp_port": 587,
            "security": "starttls",
            "username": "",
            "password": "",
            "from_address": "",
            "from_name": "DSpace Sentinel",
            "recipients": [],
            "quiet_hours": "",
            "daily_digest": "",
        },
    },
    # A partial dictionary (Sentinel's defaults fill in the rest) and the app password in the
    # environment instead of the YAML.
    "email-password-in-env": {
        "dspace_sentinel_email": {"enabled": True, "username": "sentinel.alerts@gmail.com"},
        "dspace_sentinel_email_recipients": [{"address": "ops@example.org"}],
        "dspace_sentinel_env": {"SENTINEL_SMTP_PASSWORD": APP_PASSWORD},
    },
}
"""Inventory host -> its ``host_vars`` (overriding the role defaults)."""

REFUSED: dict[str, tuple[dict[str, Any], str, str]] = {
    "six-bots": (
        {"dspace_sentinel_telegram_bots": [_bot(n) for n in range(6)]},
        "dspace_sentinel_telegram_bots | length <= 5",
        "at most 5 bots",
    ),
    "bot-as-text": ({"dspace_sentinel_telegram_bots": [BOT_A]}, "reject('mapping')", "at most 5 bots"),
    "bot-with-a-misspelt-key": (
        {"dspace_sentinel_telegram_bots": [{"token": BOT_A, "chat_id": "123456789"}]},
        "map('list')",
        "at most 5 bots",
    ),
    "bot-with-an-unknown-severity": (
        {"dspace_sentinel_telegram_bots": [{**_bot(1), "min_severity": "info"}]},
        "attribute='min_severity'",
        "at most 5 bots",
    ),
    "bots-in-the-telegram-dict": (
        {"dspace_sentinel_telegram": {"enabled": True, "chats": [_bot(1)]}},
        "dspace_sentinel_telegram.chats | default([])",
        "at most 5 bots",
    ),
    "bots-and-the-old-bot": (
        {"dspace_sentinel_telegram_bots": [_bot(1)], "dspace_sentinel_telegram_bot_token": BOT_A},
        "dspace_sentinel_telegram_bot_token",
        "move the old single bot",
    ),
    "six-recipients": (
        {"dspace_sentinel_email_recipients": [{"address": f"r{n}@example.org"} for n in range(6)]},
        "dspace_sentinel_email_recipients | length <= 5",
        "at most 5 recipients",
    ),
    "recipient-as-text": (
        {"dspace_sentinel_email_recipients": ["ops@example.org"]},
        "reject('mapping')",
        "at most 5 recipients",
    ),
    "recipient-without-an-address": (
        {"dspace_sentinel_email_recipients": [{"label": "On call"}]},
        "attribute='address'",
        "at most 5 recipients",
    ),
    "password-in-the-email-dict": (
        {"dspace_sentinel_email": {"enabled": True, "password": APP_PASSWORD}},
        "dspace_sentinel_email.password | default('')",
        "dspace_sentinel_email_password",
    ),
    "recipients-in-the-email-dict": (
        {"dspace_sentinel_email": {"enabled": True, "recipients": [{"address": "ops@example.org"}]}},
        "dspace_sentinel_email.recipients | default([])",
        "dspace_sentinel_email_recipients",
    ),
    # `daily_digest: 18:00` without quotes: YAML 1.1 reads the number 1080 (base 60).
    "email-digest-time-unquoted": (
        {"dspace_sentinel_email": {"enabled": True, "daily_digest": 18 * 60}},
        "dspace_sentinel_email.daily_digest",
        'daily_digest: "18:00"',
    ),
    "telegram-quiet-hours-unquoted": (
        {"dspace_sentinel_telegram": {"enabled": True, "quiet_hours": 23 * 60}},
        "dspace_sentinel_telegram.quiet_hours",
        'daily_digest: "18:00"',
    ),
    # Sentinel would refuse to start: reachable from other machines with nothing to protect it.
    "open-bind": ({"dspace_sentinel_host": "0.0.0.0"}, "dspace_sentinel_host", "refuses to start"),
    "login-user-with-a-space": (
        {"dspace_sentinel_login_user": "John Smith", "dspace_sentinel_login_password_hash": LOGIN_HASH},
        "dspace_sentinel_login_user",
        "1 to 64 letters",
    ),
    "login-password-instead-of-its-hash": (
        {"dspace_sentinel_login_password_hash": LOGIN_PASSWORD},
        "dspace_sentinel_login_password_hash",
        "./sentinel.sh hash-password",
    ),
}
"""Inventory host -> (its ``host_vars``, part of the failed condition, part of the message):
``tasks/checks.yml`` must stop these before anything is written."""

VAULT_FILE = {
    "vault_sentinel_bot_danindu": BOT_A,
    "vault_sentinel_gmail_app_password": "abcd efgh ijkl mnop",
}
"""``group_vars/dspace_servers/vault.yml``, encrypted as a whole (``ansible-vault create``)."""

INLINE_VAULT = {
    "team_token": BOT_B,
    "old_token": BOT_C,
    "auth_token": "a-long-random-auth-token",
    "pg_password": "read-only-pg-password",
    "login_hash": LOGIN_HASH,
}
"""Values encrypted one by one (``ansible-vault encrypt_string``) straight into ``host_vars``."""

VAULTED_HOSTS = {
    "vaulted": string.Template(
        """\
dspace_sentinel_auth_token: $auth_token
dspace_sentinel_login_password_hash: $login_hash
dspace_sentinel_postgres_password: $pg_password
dspace_sentinel_telegram: {enabled: true}
dspace_sentinel_telegram_bots:
  - label: Danindu
    bot_token: "{{ vault_sentinel_bot_danindu }}"
    chat_id: "123456789"
  - label: Team
    bot_token: $team_token
    chat_id: "-1001234567890"
    min_severity: critical
dspace_sentinel_email: {enabled: true, username: sentinel.alerts@gmail.com}
dspace_sentinel_email_recipients: [{address: ops@example.org}]
dspace_sentinel_email_password: "{{ vault_sentinel_gmail_app_password }}"
"""
    ),
    "old-bot-vaulted": string.Template(
        """\
dspace_sentinel_telegram: {enabled: true, chat_id: "123456789"}
dspace_sentinel_telegram_bot_token: $old_token
"""
    ),
}
"""Inventory host -> ``host_vars`` text (``$name`` = an inline ``!vault`` value of :data:`INLINE_VAULT`)."""

RENDER_PLAYBOOK = """\
- name: Render DSpace Sentinel's configuration for every scenario
  hosts: dspace_servers
  gather_facts: false
  tasks:
    - name: Render one scenario
      block:
        - name: Run the role's checks (this also loads the role defaults)
          ansible.builtin.include_role:
            name: dspace_sentinel
            tasks_from: checks
            public: true
        - name: Write sentinel.yaml
          ansible.builtin.template:
            src: "{{ sentinel_role_dir }}/templates/sentinel.yaml.j2"
            dest: "{{ sentinel_out_dir }}/{{ inventory_hostname }}.yaml"
            mode: "0600"
        - name: Write sentinel.env
          ansible.builtin.template:
            src: "{{ sentinel_role_dir }}/templates/sentinel.env.j2"
            dest: "{{ sentinel_out_dir }}/{{ inventory_hostname }}.env"
            mode: "0600"
          when: dspace_sentinel_env | length > 0
      rescue:
        - name: Keep the refusal
          ansible.builtin.copy:
            content: "{{ ansible_failed_result.assertion | default('') }}\\n{{ ansible_failed_result.msg }}"
            dest: "{{ sentinel_out_dir }}/{{ inventory_hostname }}.error"
            mode: "0600"
"""


@dataclass
class Rendered:
    """One scenario as the role wrote it and as Sentinel loads it."""

    cfg: Config
    raw: dict[str, Any]
    """``config/sentinel.yaml`` as written."""
    text: str
    env: dict[str, str]
    """``config/sentinel.env`` the way ``./sentinel.sh`` and the unit read it (``{}``: not written)."""


def _load(out: Path, name: str) -> Rendered:
    """Load ``<name>.yaml`` like Sentinel does, with ``<name>.env`` exported like the CLI does."""
    refused = out / f"{name}.error"
    if refused.exists():
        pytest.fail(f"tasks/checks.yml refused {name}: {refused.read_text(encoding='utf-8')}")
    text = (out / f"{name}.yaml").read_text(encoding="utf-8")
    env_path = out / f"{name}.env"
    exported = load_env_file(env_path) if env_path.exists() else []
    try:
        env = {key: os.environ[key] for key in exported}
        cfg = load_config(out / f"{name}.yaml", demo=False)
    finally:
        for key in exported:
            os.environ.pop(key, None)
    return Rendered(cfg=cfg, raw=yaml.safe_load(text), text=text, env=env)


def _ansible_bool(value: Any) -> bool:
    return value if isinstance(value, bool) else str(value).strip().lower() in {"1", "true", "yes", "on"}


def _jinja2_render(name: str, overrides: dict[str, Any], out: Path) -> None:
    """Write ``<name>.yaml`` (and ``<name>.env`` when there is environment) like the role's tasks."""
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(ROLE / "templates"),
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,  # the template module's default
    )
    env.filters.update(
        bool=_ansible_bool,
        combine=lambda base, extra: {**base, **extra},
        to_nice_yaml=lambda data, indent=2, width=80: yaml.safe_dump(
            data, indent=indent, width=width, default_flow_style=False, allow_unicode=True
        ),
    )
    variables = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text(encoding="utf-8"))
    variables.update(ansible_managed="Ansible managed", inventory_hostname=name)
    variables.update(copy.deepcopy(overrides))
    (out / f"{name}.yaml").write_text(
        env.get_template("sentinel.yaml.j2").render(**variables), encoding="utf-8"
    )
    if variables["dspace_sentinel_env"]:
        env_path = out / f"{name}.env"
        env_path.write_text(env.get_template("sentinel.env.j2").render(**variables), encoding="utf-8")
        env_path.chmod(0o600)


def _run(args: list[str], env: dict[str, str], cwd: Path) -> str:
    done = subprocess.run(
        args,
        env=env,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if done.returncode != 0:
        pytest.fail(f"{' '.join(args[:2])} failed ({done.returncode}):\n{done.stdout}\n{done.stderr}")
    return done.stdout


def _inline_vault(ansible_vault: str, password_file: Path, env: dict[str, str], cwd: Path) -> dict[str, str]:
    """Each :data:`INLINE_VAULT` value as the ``!vault |`` block ``encrypt_string`` prints for it."""
    names = [arg for name in INLINE_VAULT for arg in ("--name", name)]
    printed = _run(
        [
            ansible_vault,
            "encrypt_string",
            "--vault-password-file",
            str(password_file),
            *names,
            *INLINE_VAULT.values(),
        ],
        env,
        cwd,
    )
    blocks: dict[str, str] = {}
    current = ""
    for line in printed.splitlines():
        if line and not line[0].isspace():
            current, _, first = line.partition(": ")
            blocks[current] = first
        elif current and line.strip():
            blocks[current] += "\n" + line
    assert set(blocks) == set(INLINE_VAULT), printed
    return blocks


@pytest.fixture(scope="module")
def ansible_out(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Render every scenario with one real ``ansible-playbook`` run; the directory with the files."""
    playbook = shutil.which("ansible-playbook")
    if playbook is None:
        pytest.skip("ansible-playbook is not installed")
    work = tmp_path_factory.mktemp("ansible")
    env = {key: value for key, value in os.environ.items() if not key.startswith("ANSIBLE_")}
    env.update(
        ANSIBLE_CONFIG=str(work / "ansible.cfg"),  # empty: no ansible.cfg of the developer's
        ANSIBLE_HOME=str(work / "home"),
        ANSIBLE_ROLES_PATH=str(ROLE.parent),
        ANSIBLE_FORKS="25",
        ANSIBLE_NOCOLOR="1",
        ANSIBLE_NOCOWS="1",
        ANSIBLE_RETRY_FILES_ENABLED="0",
    )
    (work / "ansible.cfg").write_text("", encoding="utf-8")
    version = _run([playbook, "--version"], env, work)
    found = re.search(r"core (\d+)\.(\d+)", version)
    if found is None or (int(found[1]), int(found[2])) < (2, 15):
        pytest.skip(f"the role needs ansible-core 2.15 or later: {version.splitlines()[:1]}")
    ansible_vault = str(Path(playbook).with_name("ansible-vault"))
    password_file = work / "vault-password"
    password_file.write_text("sentinel-tests", encoding="utf-8")

    hosts = [*SCENARIOS, *REFUSED, *VAULTED_HOSTS]
    inventory = {
        "dspace_servers": {
            "hosts": {name: None for name in hosts},
            "vars": {
                "ansible_connection": "local",
                "ansible_python_interpreter": "{{ ansible_playbook_python }}",
            },
        }
    }
    (work / "inventory.yml").write_text(yaml.safe_dump(inventory), encoding="utf-8")
    vault_file = work / "group_vars" / "dspace_servers" / "vault.yml"
    vault_file.parent.mkdir(parents=True)
    vault_file.write_text(yaml.safe_dump(VAULT_FILE), encoding="utf-8")
    _run([ansible_vault, "encrypt", "--vault-password-file", str(password_file), str(vault_file)], env, work)
    host_vars = work / "host_vars"
    host_vars.mkdir()
    for name, overrides in SCENARIOS.items():
        (host_vars / f"{name}.yml").write_text(yaml.safe_dump(overrides), encoding="utf-8")
    for name, (overrides, _, _) in REFUSED.items():
        (host_vars / f"{name}.yml").write_text(yaml.safe_dump(overrides), encoding="utf-8")
    blocks = _inline_vault(ansible_vault, password_file, env, work)
    for name, text in VAULTED_HOSTS.items():
        (host_vars / f"{name}.yml").write_text(text.substitute(blocks), encoding="utf-8")
    (work / "render.yml").write_text(RENDER_PLAYBOOK, encoding="utf-8")
    out = work / "out"
    out.mkdir()
    extra = {"sentinel_role_dir": str(ROLE), "sentinel_out_dir": str(out)}
    _run(
        [
            playbook,
            "-i",
            str(work / "inventory.yml"),
            "--vault-password-file",
            str(password_file),
            "-e",
            json.dumps(extra),
            str(work / "render.yml"),
        ],
        env,
        work,
    )
    return out


@pytest.fixture(params=["jinja2", "ansible"])
def render(request: pytest.FixtureRequest, tmp_path: Path) -> Callable[[str], Rendered]:
    """``render(host)``: that scenario's files as the role writes them, loaded like Sentinel."""
    out = request.getfixturevalue("ansible_out") if request.param == "ansible" else tmp_path

    def _render(name: str) -> Rendered:
        if request.param == "jinja2":
            _jinja2_render(name, SCENARIOS[name], out)
        return _load(out, name)

    return _render


def test_defaults_render_a_valid_config(render: Callable[[str], Rendered]) -> None:
    got = render("defaults")
    cfg = got.cfg
    assert cfg.server.allowed_hosts == []
    assert cfg.server.allow_unauthenticated is False
    assert cfg.setup.completed is True
    assert not host_allowed("defaults", allowed_hosts(cfg))
    assert cfg.notifiers == Config().notifiers  # Telegram, email and webhook: the app's own defaults
    assert cfg.reports == Config().reports
    assert set(got.raw["notifiers"]["telegram"]) == TELEGRAM_KEYS
    assert got.env == {}


def test_the_role_switches_every_module_on_security_included(render: Callable[[str], Rendered]) -> None:
    got = render("defaults")
    modules = got.raw["modules"]
    assert modules["security"] is True and all(value is True for value in modules.values())
    assert set(MODULE_IDS) <= set(modules)  # a module Sentinel knows is never left out
    assert all(got.cfg.module_enabled(mid) for mid in MODULE_IDS)
    assert got.raw["collector"]["security_interval"] == 900
    assert getattr(got.cfg.collector, "security_interval", 900) == 900


def test_sections_copied_from_the_example_config_render_the_defaults(
    render: Callable[[str], Rendered],
) -> None:
    got = render("copied-from-the-example-config")
    assert got.cfg.notifiers == Config().notifiers
    assert set(got.raw["notifiers"]["telegram"]) == TELEGRAM_KEYS


def test_nginx_snippet_adds_the_inventory_host(render: Callable[[str], Rendered]) -> None:
    cfg = render("repo.example.org").cfg
    assert cfg.server.allowed_hosts == ["sentinel.example.org", "repo.example.org"]
    hosts = allowed_hosts(cfg)
    assert host_allowed("repo.example.org", hosts)
    assert host_allowed("sentinel.example.org", hosts)
    assert not host_allowed("evil.example", hosts)


def test_allow_unauthenticated_or_a_login_reaches_the_bind_check(
    render: Callable[[str], Rendered], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SENTINEL_ALLOW_OPEN", raising=False)
    check_bind(render("open-bind-allowed").cfg)
    check_bind(render("open-bind-login").cfg)


def test_an_open_bind_is_refused_by_the_role_and_by_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tasks/checks.yml stops it before anything is written (see the refusals below); rendered
    anyway, Sentinel would refuse to start."""
    monkeypatch.delenv("SENTINEL_ALLOW_OPEN", raising=False)
    overrides, _, _ = REFUSED["open-bind"]
    _jinja2_render("open-bind", overrides, tmp_path)
    with pytest.raises(UnsafeBindError):
        check_bind(_load(tmp_path, "open-bind").cfg)


def test_the_login_variables_render_into_server_login(render: Callable[[str], Rendered]) -> None:
    got = render("login")
    assert got.raw["server"]["login"] == {"username": "ops.admin", "password_hash": LOGIN_HASH}
    login = got.cfg.server.login
    assert login.username == "ops.admin" and verify_password(LOGIN_PASSWORD, login.password_hash)
    assert login_active(got.cfg) and startup_notices(got.cfg) == []
    assert LOGIN_PASSWORD not in got.text
    defaults = render("defaults")
    assert defaults.raw["server"]["login"] == {"username": "admin", "password_hash": ""}
    assert not login_active(defaults.cfg)  # no hash, no login


def test_several_telegram_bots_each_with_its_own_token(render: Callable[[str], Rendered]) -> None:
    got = render("several-bots")
    telegram = got.cfg.notifiers.telegram
    assert telegram.enabled is True
    assert (telegram.quiet_hours, telegram.daily_digest) == ("23:00-07:00", "08:00")
    assert [(c.label, c.bot_token, c.chat_id, c.min_severity) for c in telegram.chats] == [
        ("Danindu", BOT_A, "123456789", "warning"),
        ("Team", BOT_B, "-1001234567890", "critical"),
        ("", BOT_C, "@dspace_alerts", "warning"),
    ]
    assert len(telegram.destinations()) == 3
    # A given id is kept; Sentinel derives the others from the bot's position and chat id.
    ids = [c.id for c in telegram.chats]
    assert ids[2] == "night-shift"
    assert all(re.fullmatch(r"[0-9a-f]{8}", cid) for cid in ids[:2])
    assert "id" not in got.raw["notifiers"]["telegram"]["chats"][0]
    assert set(got.raw["notifiers"]["telegram"]) == TELEGRAM_KEYS


def test_the_old_single_bot_variables_still_work(render: Callable[[str], Rendered]) -> None:
    got = render("old-single-bot")
    telegram = got.cfg.notifiers.telegram
    assert telegram.enabled is True
    assert (telegram.quiet_hours, telegram.daily_digest) == ("23:00-07:00", "08:00")
    assert [(c.label, c.bot_token, c.chat_id, c.min_severity) for c in telegram.chats] == [
        ("", BOT_A, "-1001234567890", "warning")
    ]
    assert set(got.raw["notifiers"]["telegram"]) == TELEGRAM_KEYS  # one entry of chats, not the old keys


def test_the_old_token_in_sentinel_env_still_works(render: Callable[[str], Rendered]) -> None:
    got = render("old-token-in-env")
    assert got.raw["notifiers"]["telegram"]["chats"] == [
        {"label": "", "bot_token": "", "chat_id": "123456789", "min_severity": "warning"}
    ]
    assert BOT_A not in got.text
    assert got.env == {"SENTINEL_TELEGRAM_TOKEN": BOT_A}
    # The token fills in the bot listed without one; no second "From environment" bot appears.
    assert [(c.bot_token, c.chat_id) for c in got.cfg.notifiers.telegram.chats] == [(BOT_A, "123456789")]


def test_email_through_gmail_and_the_weekly_report_by_email(render: Callable[[str], Rendered]) -> None:
    got = render("email")
    email = got.cfg.notifiers.email
    assert email.enabled is True
    assert (email.smtp_host, email.smtp_port, email.security) == ("smtp.gmail.com", 465, "ssl")
    assert email.username == "sentinel.alerts@gmail.com"
    assert email.password == APP_PASSWORD  # pasted in Google's four groups, stored without the spaces
    assert (email.from_address, email.from_name, email.sender) == (
        "",
        "Repository monitor",
        "sentinel.alerts@gmail.com",
    )
    assert [(r.address, r.label, r.min_severity) for r in email.recipients] == [
        ("ops@example.org", "On call", "warning"),
        ("director@example.org", "Director", "critical"),
    ]
    assert (email.quiet_hours, email.daily_digest) == ("23:00-07:00", "07:30")
    assert email.ready()
    assert got.cfg.reports.weekly.send_email is True
    assert got.cfg.reports.weekly.send_telegram is False
    assert got.cfg.reports.default_sections == ["summary", "alerts", "recommendations"]


def test_the_email_app_password_can_come_from_sentinel_env(render: Callable[[str], Rendered]) -> None:
    got = render("email-password-in-env")
    assert got.raw["notifiers"]["email"]["password"] == ""
    assert APP_PASSWORD not in got.text
    assert got.env == {"SENTINEL_SMTP_PASSWORD": APP_PASSWORD}
    email = got.cfg.notifiers.email
    assert email.password == APP_PASSWORD
    assert (email.smtp_host, email.smtp_port, email.security) == ("smtp.gmail.com", 587, "starttls")
    assert email.ready()


def test_secrets_from_ansible_vault_are_written_in_clear_text(ansible_out: Path) -> None:
    got = _load(ansible_out, "vaulted")
    assert "$ANSIBLE_VAULT" not in got.text
    assert "!vault" not in got.text
    chats = got.cfg.notifiers.telegram.chats
    assert [(c.label, c.bot_token, c.chat_id, c.min_severity) for c in chats] == [
        ("Danindu", BOT_A, "123456789", "warning"),
        ("Team", BOT_B, "-1001234567890", "critical"),
    ]
    assert got.cfg.notifiers.email.password == APP_PASSWORD
    assert got.cfg.notifiers.email.ready()
    assert got.cfg.server.auth_token == INLINE_VAULT["auth_token"]
    assert got.cfg.server.login.password_hash == LOGIN_HASH and login_active(got.cfg)
    assert got.cfg.postgres.password == INLINE_VAULT["pg_password"]
    old = _load(ansible_out, "old-bot-vaulted")
    assert "!vault" not in old.text
    assert [(c.bot_token, c.chat_id) for c in old.cfg.notifiers.telegram.chats] == [(BOT_C, "123456789")]


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_the_role_refuses_notification_settings_sentinel_would_not_use(ansible_out: Path, name: str) -> None:
    _, condition, message = REFUSED[name]
    assert not (ansible_out / f"{name}.yaml").exists()
    failed, _, msg = (ansible_out / f"{name}.error").read_text(encoding="utf-8").partition("\n")
    assert condition in failed
    assert message in msg


def test_the_stand_ins_render_what_ansible_renders(ansible_out: Path, tmp_path: Path) -> None:
    for name, overrides in SCENARIOS.items():
        _jinja2_render(name, overrides, tmp_path)
        ours = yaml.safe_load((tmp_path / f"{name}.yaml").read_text(encoding="utf-8"))
        theirs = yaml.safe_load((ansible_out / f"{name}.yaml").read_text(encoding="utf-8"))
        assert ours == theirs, name
        assert (tmp_path / f"{name}.env").exists() == (ansible_out / f"{name}.env").exists(), name
        if (tmp_path / f"{name}.env").exists():
            ours_env = _env_lines(tmp_path / f"{name}.env")
            assert ours_env == _env_lines(ansible_out / f"{name}.env"), name
            assert ours_env, name


def _env_lines(path: Path) -> list[str]:
    return [
        line for line in path.read_text(encoding="utf-8").splitlines() if line and not line.startswith("#")
    ]
