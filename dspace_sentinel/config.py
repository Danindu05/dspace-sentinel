"""Configuration: pydantic models mirroring ``config/sentinel.yaml`` (SPEC section 3).

Precedence when loading: built-in defaults ← YAML file ← environment variables.
``PATCH /api/config`` uses :func:`merge_patch` (deep merge + validation) and
:func:`save_config` (atomic write, mode 0600). A save writes the file's own settings plus the
patches, never values that came from ``SENTINEL_*`` variables or ``serve --host/--port``; a demo
save writes only what the UI changed, without server settings or secrets.

Telegram sends through up to five destinations, each its own bot (``notifiers.telegram.chats``:
``{id, label, bot_token, chat_id, min_severity}``); every ``bot_token`` is a secret kept by the
destination's ``id``. The old single ``bot_token`` + ``chat_id`` still load as one destination,
and ``SENTINEL_TELEGRAM_TOKEN`` + ``SENTINEL_TELEGRAM_CHAT_ID`` add a read-only destination
(``id: "env"``) to the running configuration only (see :func:`apply_telegram_env`).
"""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import logging
import os
import re
import secrets
import socket
import tempfile
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    field_validator,
    model_validator,
)

from dspace_sentinel import paths
from dspace_sentinel.auth import LOGIN_UNREADABLE, login_problem

log = logging.getLogger(__name__)

MASK = "••••••"
"""Replacement string for secrets in :func:`masked`; sending it back keeps the old value."""

SECRET_PATHS: tuple[tuple[str, ...], ...] = (
    ("server", "auth_token"),
    ("server", "login", "password_hash"),
    ("postgres", "password"),
    ("notifiers", "email", "password"),
    ("notifiers", "webhook", "url"),
)
"""Dotted paths (as tuples) of every secret inside the config tree (webhook URLs carry their
credential in the path, e.g. Slack's ``/services/T…/B…/<secret>``). The Telegram bot tokens live
inside a list: see :data:`SECRET_ITEM_PATHS`."""

SECRET_ITEM_PATHS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("notifiers", "telegram", "chats"), "bot_token"),
)
"""Secrets inside list items: ``(path of the list, key of the secret in every item)``. A masked
item secret keeps the stored value of the item with the same ``id``."""

TELEGRAM_MAX_BOTS = 5
"""Telegram destinations (one bot each), the environment's included."""

EMAIL_MAX_RECIPIENTS = 5
"""Email recipients."""

ENV_DESTINATION_ID = "env"
"""``id`` of the Telegram destination made from ``SENTINEL_TELEGRAM_TOKEN`` + ``…_CHAT_ID``."""

ENV_DESTINATION_LABEL = "From environment"

BOT_TOKEN_ERROR = "not a Telegram bot token (it looks like 123456789:ABC…, from @BotFather)"

ENV_BOT_LEFT_OUT = (
    f"the Telegram bot from SENTINEL_TELEGRAM_TOKEN was left out: there are already {TELEGRAM_MAX_BOTS} bots"
)
"""Logged (and recorded once in the events log by ``serve``) when the environment's bot would be
the sixth: a notification limit never stops Sentinel from starting."""

_LEGACY_MARK = "_legacy_single_bot"
"""Marks, while a file is loaded, the destination made from the old single bot_token/chat_id."""

DSPACE_MODULE_IDS: tuple[str, ...] = ("postgres", "solr", "backend", "frontend", "repository", "assetstore")
"""Modules whose presence turns the "Services" tab into the "DSpace stack" tab."""

MODULE_IDS: tuple[str, ...] = (
    "ports",
    "security",
    "postgres",
    "solr",
    "backend",
    "frontend",
    "nginx",
    "ssl",
    "logs",
    "repository",
    "assetstore",
    "incidents",
)
"""Every toggleable module id, in menu order. The server module is always on."""

ExpectKind = Literal["open", "local_only"]
ThemeKind = Literal["system", "light", "dark"]
SeverityKind = Literal["warning", "critical"]
SecurityKind = Literal["starttls", "ssl", "none"]


class _Model(BaseModel):
    """Base for every config section: unknown keys are ignored, values are validated on assignment."""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)


_UNIT_NAME = re.compile(r"^[A-Za-z0-9@_.:\-]{1,128}$")


def _unit_name(value: str) -> str:
    """A systemd unit name (``""`` skips the rung); never anything systemctl could read as an option."""
    cleaned = value.strip()
    if cleaned and (cleaned.startswith("-") or not _UNIT_NAME.match(cleaned)):
        raise ValueError(
            f"not a systemd unit name: {cleaned[:40]!r} (letters, digits and @ _ . : - only, "
            "not starting with -)"
        )
    return cleaned


class LoginConfig(_Model):
    """The dashboard login (one admin account): on when ``password_hash`` is set.

    Changed only by ``set-password`` (CLI) and ``POST /api/account/password``; ``password_hash`` is
    a secret (:data:`SECRET_PATHS`). A login that cannot be used (a hash that does not parse, a
    username outside the rules) still loads and stays on: nobody can sign in until
    ``set-password`` writes a new one (:func:`dspace_sentinel.auth.login_problem`).
    """

    username: str = ""
    password_hash: str = ""

    @field_validator("username", "password_hash", mode="before")
    @classmethod
    def _as_text(cls, value: Any) -> str:
        return _text(value).strip()


class ServerConfig(_Model):
    host: str = "127.0.0.1"
    port: int = Field(default=9765, ge=1, le=65535)
    auth_token: str = ""
    login: LoginConfig = Field(default_factory=LoginConfig)
    """The dashboard login; with it set, the web UI asks people to sign in and a non-loopback
    ``host`` is allowed. ``auth_token`` stays a token for scripts."""
    hostname: str = ""
    allowed_hosts: list[str] = Field(default_factory=list)
    """Extra names the API answers to in the ``Host`` header (e.g. a reverse proxy's server_name);
    ``"*"`` allows any. Local names, IP addresses and the DSpace URLs' hosts are always allowed."""
    allow_unauthenticated: bool = False
    """Serve on a non-loopback ``host`` without ``auth_token`` (refused otherwise; see create_app)."""
    timezone: str = ""
    """The zone of the times Sentinel shows people (messages, emails, reports, quiet hours, the
    daily digest, the weekly report hour, ``doctor``): an IANA name such as ``Asia/Colombo``;
    ``""`` = the server's zone. Log files keep their own zones. The one ``server`` setting the
    API may change (:data:`API_SERVER_KEYS`)."""

    @field_validator("timezone", mode="before")
    @classmethod
    def _known_zone(cls, value: Any) -> str:
        from dspace_sentinel.util.tz import check_zone

        return check_zone(value)

    @field_validator("login", mode="before")
    @classmethod
    def _no_login(cls, value: Any) -> Any:
        return {} if value is None or value == "" else value


API_SERVER_KEYS: frozenset[str] = frozenset({"timezone"})
"""``server`` settings ``PATCH /api/config`` may change: none of them decides who reaches the API."""


def server_access(server: ServerConfig) -> dict[str, Any]:
    """The ``server`` settings that decide who can reach the API (all but :data:`API_SERVER_KEYS`)."""
    return server.model_dump(exclude=set(API_SERVER_KEYS))


class SetupConfig(_Model):
    completed: bool = False
    persona: str = ""


class CollectorConfig(_Model):
    system_interval: int = Field(default=15, ge=5, le=3600)
    service_interval: int = Field(default=30, ge=5, le=3600)
    http_interval: int = Field(default=30, ge=5, le=3600)
    db_interval: int = Field(default=60, ge=5, le=3600)
    logs_interval: int = Field(default=60, ge=5, le=3600)
    repository_interval: int = Field(default=900, ge=30, le=86400)
    security_interval: int = Field(default=900, ge=60, le=86400)
    """The Security module's read-only checks (ports, firewall, SSH, updates, software, files)."""
    retention_days: int = Field(default=30, ge=1, le=3650)


class DSpaceConfig(_Model):
    install_dir: str = "/dspace"
    auto_discover: bool = True
    server_url: str = "http://localhost:8080/server"
    ui_url: str = "http://localhost:4000"
    public_url: str = ""
    assetstore_dir: str = "/dspace/assetstore"
    log_dir: str = "/dspace/log"
    backend_service: str = "tomcat9"
    frontend_service: str = "dspace-ui"
    verify_tls: bool = True

    @field_validator("backend_service", "frontend_service")
    @classmethod
    def _units_are_names(cls, value: str) -> str:
        return _unit_name(value)


class PostgresConfig(_Model):
    host: str = "localhost"
    port: int = Field(default=5432, ge=1, le=65535)
    database: str = "dspace"
    user: str = "dspace"
    password: str = ""
    sslmode: str = "prefer"
    service: str = "postgresql"

    @field_validator("service")
    @classmethod
    def _unit_is_a_name(cls, value: str) -> str:
        return _unit_name(value)


class SolrConfig(_Model):
    url: str = "http://localhost:8983/solr"
    cores: list[str] = Field(default_factory=lambda: ["search", "statistics", "oai", "authority"])
    service: str = "solr"

    @field_validator("service")
    @classmethod
    def _unit_is_a_name(cls, value: str) -> str:
        return _unit_name(value)


class NginxConfig(_Model):
    service: str = "nginx"
    http_url: str = "http://localhost"
    https_url: str = ""

    @field_validator("service")
    @classmethod
    def _unit_is_a_name(cls, value: str) -> str:
        return _unit_name(value)


class SSLConfig(_Model):
    host: str = ""
    port: int = Field(default=443, ge=1, le=65535)


class PortRule(_Model):
    port: int = Field(ge=1, le=65535)
    label: str = ""
    expect: ExpectKind = "open"


class LogFileConfig(_Model):
    id: str
    label: str = ""
    path: str
    daily_suffix: bool = False
    timezone: str = ""
    """Zone of the file's timestamps: an IANA name, ``UTC`` or ``local``; ``""`` = local time
    unless the newest line only matches the file's modification time when read as UTC."""

    @field_validator("id")
    @classmethod
    def _id_is_slug(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or any(ch in cleaned for ch in " /.\t"):
            raise ValueError("log file id must be a short slug (letters, digits, - or _)")
        return cleaned

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        from dspace_sentinel.util.logs import zone_for

        cleaned = value.strip()
        zone_for(cleaned)  # raises ValueError for an unknown zone
        return cleaned


class LogsConfig(_Model):
    files: list[LogFileConfig] = Field(
        default_factory=lambda: [
            LogFileConfig(id="dspace", label="DSpace", path="/dspace/log/dspace.log", daily_suffix=True),
            LogFileConfig(id="tomcat", label="Tomcat", path="/var/log/tomcat9/catalina.out"),
            LogFileConfig(id="solr", label="Solr", path="/var/solr/logs/solr.log"),
            LogFileConfig(id="nginx", label="nginx", path="/var/log/nginx/error.log"),
        ]
    )
    window_minutes: int = Field(default=5, ge=1, le=1440)


class Threshold(_Model):
    warning: float
    critical: float


class AlertsConfig(_Model):
    confirm_samples: int = Field(default=2, ge=1, le=100)
    clear_samples: int = Field(default=3, ge=1, le=100)
    reminder_minutes: int = Field(default=0, ge=0, le=10080)
    cpu: Threshold = Field(default_factory=lambda: Threshold(warning=80, critical=95))
    memory: Threshold = Field(default_factory=lambda: Threshold(warning=80, critical=92))
    swap: Threshold = Field(default_factory=lambda: Threshold(warning=50, critical=80))
    disk: Threshold = Field(default_factory=lambda: Threshold(warning=80, critical=90))
    load_per_core: Threshold = Field(default_factory=lambda: Threshold(warning=1.5, critical=3.0))
    rest_latency_ms: Threshold = Field(default_factory=lambda: Threshold(warning=1000, critical=3000))
    search_latency_ms: Threshold = Field(default_factory=lambda: Threshold(warning=1500, critical=4000))
    home_latency_ms: Threshold = Field(default_factory=lambda: Threshold(warning=2000, critical=5000))
    postgres_connections_pct: Threshold = Field(default_factory=lambda: Threshold(warning=80, critical=95))
    ssl_expiry_days: Threshold = Field(default_factory=lambda: Threshold(warning=30, critical=7))
    log_errors_per_window: Threshold = Field(default_factory=lambda: Threshold(warning=20, critical=100))
    assetstore_free_gb: Threshold = Field(default_factory=lambda: Threshold(warning=50, critical=10))


_CHAT_ID = re.compile(r"^(?:-?\d{1,20}|@[A-Za-z0-9_]{5,32})$")
_BOT_TOKEN = re.compile(r"^\d{5,12}:[A-Za-z0-9_-]{30,50}$")
_DESTINATION_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)
_EMAIL = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)
_APP_PASSWORD_GROUPS = re.compile(r"^[A-Za-z]{4}(?: [A-Za-z]{4}){3}$")
_HHMM = re.compile(r"^([01]?[0-9]|2[0-3]):([0-5][0-9])$")
"""A time of day in ASCII digits (``\\d`` would take Arabic-Indic or full-width digits too, which
the forms cannot show and the docs do not promise)."""
_QUIET_WINDOW = re.compile(r"^([0-9]{1,2}:[0-9]{2})\s*[-\u2010-\u2015\u2212]\s*([0-9]{1,2}:[0-9]{2})$")
"""Two times around a hyphen, en or em dash or minus sign (spaces around it are fine). Every part
is bounded or pinned to the dash, so matching takes time linear in the length of the value."""

TIME_VALUE_MAX = 40
"""A ``quiet_hours`` or ``daily_digest`` value longer than this is refused before it is matched:
no window or time is that long."""

QUIET_HOURS_ERROR = "quiet hours must look like 23:00-07:00"
QUIET_HOURS_EQUAL_ERROR = "quiet hours must start and end at different times"
"""A window such as ``23:00-23:00`` is empty: no minute lies inside it, so it would never be quiet."""
DIGEST_TIME_ERROR = "the daily digest time must look like 08:00"


def _text(value: Any) -> str:
    """YAML may hand over numbers (``chat_id: -100123``); ``None`` is empty."""
    return "" if value is None else str(value)


def _one_line(value: str, limit: int, what: str) -> str:
    """``value`` stripped; at most ``limit`` characters and no control characters (CR/LF …)."""
    cleaned = value.strip()
    if len(cleaned) > limit or any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
        raise ValueError(f"{what} must be at most {limit} characters on one line")
    return cleaned


def _hhmm(text: str) -> str | None:
    """``"7:05"`` → ``"07:05"``; ``None`` for anything that is not a time of day."""
    text = text.strip()
    match = _HHMM.match(text) if len(text) <= TIME_VALUE_MAX else None
    return f"{int(match.group(1)):02d}:{match.group(2)}" if match else None


def quiet_hours_value(value: Any) -> str:
    """``quiet_hours`` as the notifiers read it (``"23:00-07:00"``), or ``""``.

    An en or em dash, spaces around the dash and single-digit hours are understood (``"7:00 –
    9:30"`` → ``"07:00-09:30"``); anything else raises ``ValueError(QUIET_HOURS_ERROR)`` instead
    of being saved and then ignored, and so does a window whose two times are the same
    (``ValueError(QUIET_HOURS_EQUAL_ERROR)``: it would never be quiet).
    """
    text = _text(value).strip()
    if not text:
        return ""
    match = _QUIET_WINDOW.match(text) if len(text) <= TIME_VALUE_MAX else None
    start, end = (_hhmm(match.group(1)), _hhmm(match.group(2))) if match else (None, None)
    if start is None or end is None:
        raise ValueError(QUIET_HOURS_ERROR)
    if start == end:
        raise ValueError(QUIET_HOURS_EQUAL_ERROR)
    return f"{start}-{end}"


def digest_time_value(value: Any) -> str:
    """``daily_digest`` as ``"HH:MM"`` (``"8:00"`` → ``"08:00"``), or ``""``; anything else
    raises ``ValueError(DIGEST_TIME_ERROR)``."""
    text = _text(value).strip()
    if not text:
        return ""
    time_of_day = _hhmm(text)
    if time_of_day is None:
        raise ValueError(DIGEST_TIME_ERROR)
    return time_of_day


def is_address(value: str) -> bool:
    """True for one bare email address (``local@domain.tld``); see :func:`plain_address`."""
    cleaned = value.strip()
    return len(cleaned) <= 254 and bool(_EMAIL.match(cleaned))


def plain_address(value: str) -> str:
    """One bare email address (``local@domain.tld``) or ``ValueError("not an email address: …")``.

    No display name, whitespace, CR/LF, commas or angle brackets: the value goes into mail headers.
    """
    cleaned = value.strip()
    if not is_address(cleaned):
        raise ValueError(f"not an email address: {cleaned[:60]!r}")
    return cleaned


SENDER_NOT_AN_ADDRESS = (
    "the SMTP username is not an email address: set from_address (the address alerts come from)"
)
"""A login such as SendGrid's ``apikey`` or an Amazon SES key id cannot be the From address."""

PASSWORD_NOT_SENDABLE = (
    "the email password has a character that cannot be sent: "
    "use letters, digits and symbols from the English keyboard"
)
USERNAME_NOT_SENDABLE = (
    "the SMTP username has a character that cannot be sent: "
    "use letters, digits and symbols from the English keyboard"
)
"""``smtplib`` sends the login as ASCII, so a password or login name with any other character
(an "ä", a "•") can never log in."""

EMAIL_NOT_READY = "email is not ready: "
"""Start of the start-up notice about email settings that loaded but can never send; the reason
(:meth:`EmailConfig.problem`) follows."""


def _derived_id(index: int, seed: str, taken: set[str]) -> str:
    """A stable 8-hex-character id for the destination at ``index`` (so ids survive restarts
    until the file is saved with them), never one already ``taken``."""
    digest = hashlib.sha256(f"dspace-sentinel:{index}:{seed}".encode()).hexdigest()
    for start in range(len(digest) - 7):
        candidate = digest[start : start + 8]
        if candidate not in taken:
            return candidate
    return secrets.token_hex(4)  # pragma: no cover - 57 windows all taken


class TelegramChat(_Model):
    """One Telegram destination: its own bot (``bot_token``) talking to one chat."""

    id: str = ""
    label: str = ""
    bot_token: str = ""
    chat_id: str = ""
    min_severity: SeverityKind = "warning"

    @field_validator("id", "label", "bot_token", "chat_id", mode="before")
    @classmethod
    def _as_text(cls, value: Any) -> str:
        return _text(value)

    @field_validator("id")
    @classmethod
    def _id_is_a_slug(cls, value: str) -> str:
        cleaned = value.strip()
        if cleaned and not _DESTINATION_ID.match(cleaned):
            raise ValueError(f"not a destination id: {cleaned[:40]!r} (letters, digits, - and _)")
        return cleaned

    @field_validator("label")
    @classmethod
    def _label_one_line(cls, value: str) -> str:
        return _one_line(value, 40, "a label")

    @field_validator("chat_id")
    @classmethod
    def _chat_id_shape(cls, value: str) -> str:
        cleaned = value.strip()
        if cleaned and not _CHAT_ID.match(cleaned):
            raise ValueError(f"not a Telegram chat id: {cleaned[:40]!r}")
        return cleaned

    @field_validator("bot_token")
    @classmethod
    def _token_shape(cls, value: str) -> str:
        cleaned = value.strip()
        if cleaned and not _BOT_TOKEN.match(cleaned):
            raise ValueError(BOT_TOKEN_ERROR)  # never echo a secret
        return cleaned

    @property
    def complete(self) -> bool:
        """True when the destination has both a bot token and a chat id."""
        return bool(self.bot_token and self.chat_id)

    @property
    def from_env(self) -> bool:
        """True for the read-only destination made from the environment."""
        return self.id == ENV_DESTINATION_ID

    @property
    def name(self) -> str:
        """What results and messages call it: the label, else the chat id."""
        return self.label or self.chat_id or self.id


def migrate_legacy_telegram(section: dict[str, Any], *, mark: bool = False) -> None:
    """Turn the old single ``bot_token`` + ``chat_id`` of a Telegram section into ``chats`` (in place).

    The old keys are always removed. When the section has no ``chats`` (absent or empty) and one
    of them is set, they become one destination ``{label: "", bot_token, chat_id, min_severity:
    warning}`` (its id is assigned by :func:`assign_destination_ids`); a list next to them wins.
    ``mark`` tags that destination so :func:`apply_telegram_env` can give the environment
    variables their old meaning for it.
    """
    had_old = "bot_token" in section or "chat_id" in section
    token = _text(section.pop("bot_token", None)).strip()
    chat_id = _text(section.pop("chat_id", None)).strip()
    if not had_old or section.get("chats") or not (token or chat_id):
        return
    destination: dict[str, Any] = {
        "label": "",
        "bot_token": token,
        "chat_id": chat_id,
        "min_severity": "warning",
    }
    if mark:
        destination[_LEGACY_MARK] = True
    section["chats"] = [destination]


def assign_destination_ids(chats: list[Any], *, keep_env: bool = False) -> None:
    """Give every destination dict without an id a stable 8-hex id (in place).

    ``keep_env`` keeps the environment destination's reserved id; otherwise an ``"env"`` id
    counts as missing (only the environment may use it).
    """

    def current(item: Any) -> str:
        cid = _text(item.get("id")).strip() if isinstance(item, dict) else ""
        return "" if cid == ENV_DESTINATION_ID and not keep_env else cid

    taken = {current(item) for item in chats} - {""}
    for index, item in enumerate(chats):
        if not isinstance(item, dict):
            continue
        cid = current(item)
        if not cid:
            cid = _derived_id(index, _text(item.get("chat_id")).strip(), taken)
            taken.add(cid)
        item["id"] = cid


class TelegramConfig(_Model):
    enabled: bool = False
    chats: list[TelegramChat] = Field(default_factory=list)
    quiet_hours: str = ""
    daily_digest: str = ""

    @model_validator(mode="before")
    @classmethod
    def _old_single_bot(cls, data: Any) -> Any:
        if isinstance(data, dict) and ("bot_token" in data or "chat_id" in data):
            data = dict(data)
            migrate_legacy_telegram(data)
        return data

    @field_validator("quiet_hours", mode="before")
    @classmethod
    def _quiet_hours(cls, value: Any) -> str:
        return quiet_hours_value(value)

    @field_validator("daily_digest", mode="before")
    @classmethod
    def _digest_time(cls, value: Any) -> str:
        return digest_time_value(value)

    @field_validator("chats")
    @classmethod
    def _chats_ok(cls, chats: list[TelegramChat]) -> list[TelegramChat]:
        if len(chats) > TELEGRAM_MAX_BOTS:
            extra = ""
            if any(c.from_env for c in chats):
                extra = " (the one from SENTINEL_TELEGRAM_TOKEN / SENTINEL_TELEGRAM_CHAT_ID counts too)"
            raise ValueError(f"Telegram can send to at most {TELEGRAM_MAX_BOTS} bots{extra}")
        taken = {c.id for c in chats if c.id}
        seen_ids: set[str] = set()
        seen_pairs: set[tuple[str, str]] = set()
        for index, chat in enumerate(chats):
            if not chat.id:
                chat.id = _derived_id(index, chat.chat_id, taken)
                taken.add(chat.id)
            if chat.id in seen_ids:
                raise ValueError(f"two Telegram destinations have the same id {chat.id!r}")
            seen_ids.add(chat.id)
            if chat.complete:
                pair = (chat.bot_token, chat.chat_id.lower())
                if pair in seen_pairs:
                    raise ValueError(f"Telegram chat {chat.chat_id} is listed twice with the same bot")
                seen_pairs.add(pair)
        return chats

    def destinations(self) -> list[TelegramChat]:
        """The destinations that can be sent to (bot token and chat id both set), in order."""
        return [c for c in self.chats if c.complete]


class EmailRecipient(_Model):
    address: str
    label: str = ""
    min_severity: SeverityKind = "warning"

    @field_validator("address", "label", mode="before")
    @classmethod
    def _as_text(cls, value: Any) -> str:
        return _text(value)

    @field_validator("address")
    @classmethod
    def _plain(cls, value: str) -> str:
        return plain_address(value)

    @field_validator("label")
    @classmethod
    def _label_one_line(cls, value: str) -> str:
        return _one_line(value, 40, "a label")

    @property
    def name(self) -> str:
        """What results call it: the label, else the address."""
        return self.label or self.address


class EmailConfig(_Model):
    enabled: bool = False
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = Field(default=587, ge=1, le=65535)
    security: SecurityKind = "starttls"
    username: str = ""
    password: str = ""
    from_address: str = ""
    from_name: str = "DSpace Sentinel"
    recipients: list[EmailRecipient] = Field(default_factory=list)
    quiet_hours: str = ""
    daily_digest: str = ""

    @field_validator("smtp_host", "username", "password", "from_address", "from_name", mode="before")
    @classmethod
    def _as_text(cls, value: Any) -> str:
        return _text(value)

    @field_validator("smtp_host")
    @classmethod
    def _host(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            return cleaned
        try:
            ipaddress.ip_address(cleaned)
        except ValueError:
            if not _HOSTNAME.match(cleaned):
                raise ValueError(
                    f"not an SMTP server name: {cleaned[:60]!r} (a host name or IP address)"
                ) from None
        return cleaned

    @field_validator("username")
    @classmethod
    def _username(cls, value: str) -> str:
        return _one_line(value, 254, "the SMTP username")

    @field_validator("password")
    @classmethod
    def _app_password(cls, value: str) -> str:
        cleaned = value.strip()
        if cleaned == MASK:  # the UI's "••••••" pasted into the settings file: never a password
            return ""
        if _APP_PASSWORD_GROUPS.match(cleaned):  # Google shows app passwords as "abcd efgh ijkl mnop"
            cleaned = cleaned.replace(" ", "")
        return cleaned

    @field_validator("from_address")
    @classmethod
    def _from_address(cls, value: str) -> str:
        return plain_address(value) if value.strip() else ""

    @field_validator("from_name")
    @classmethod
    def _from_name(cls, value: str) -> str:
        return _one_line(value, 60, "the sender name")

    @field_validator("quiet_hours", mode="before")
    @classmethod
    def _quiet_hours(cls, value: Any) -> str:
        return quiet_hours_value(value)

    @field_validator("daily_digest", mode="before")
    @classmethod
    def _digest_time(cls, value: Any) -> str:
        return digest_time_value(value)

    @field_validator("recipients")
    @classmethod
    def _recipients_ok(cls, recipients: list[EmailRecipient]) -> list[EmailRecipient]:
        if len(recipients) > EMAIL_MAX_RECIPIENTS:
            raise ValueError(f"email can go to at most {EMAIL_MAX_RECIPIENTS} recipients")
        seen: set[str] = set()
        for recipient in recipients:
            key = recipient.address.lower()
            if key in seen:
                raise ValueError(f"{key} is listed twice")
            seen.add(key)
        return recipients

    @property
    def sender(self) -> str:
        """The From address: ``from_address``, else the login ``username``."""
        return self.from_address or self.username

    def problem(self, *, password: bool = True) -> str | None:
        """Why these settings can never send although nothing is missing (``None``: no such reason).

        ``smtplib`` sends the login as ASCII, so a password or login name with any other character
        can never log in: :data:`PASSWORD_NOT_SENDABLE`, :data:`USERNAME_NOT_SENDABLE` (a relay
        that needs no login is never sent the password). Messages come from ``from_address``, else
        from the login, so a login that is not an email address (SendGrid's "apikey", an Amazon SES
        key id, Postmark's token) needs ``from_address``: :data:`SENDER_NOT_AN_ADDRESS`.
        ``password=False`` leaves the password out (one from ``SENTINEL_SMTP_PASSWORD``, which a
        PATCH can neither set nor mend).

        ``PATCH /api/config`` refuses email settings with such a problem (:func:`merge_patch`). A
        settings file (or environment) that holds them still loads: email is then not ready
        (:meth:`server_ready`), which :func:`load_config` says in its start-up notices.
        """
        if not self.username:
            return None
        if password and not self.password.isascii():
            return PASSWORD_NOT_SENDABLE
        if not self.username.isascii():
            return USERNAME_NOT_SENDABLE
        if not self.from_address and not is_address(self.username):
            return SENDER_NOT_AN_ADDRESS
        return None

    def server_ready(self) -> bool:
        """True when a message can be handed over: the server, a sender that is an email address
        and, for a login (Gmail), a password; a relay that needs no login only needs
        ``from_address``. Settings with a :meth:`problem` are never ready."""
        if not (self.smtp_host and is_address(self.sender) and (self.password or not self.username)):
            return False
        return self.problem() is None

    def ready(self) -> bool:
        """True when :meth:`server_ready` and at least one recipient is set."""
        return self.server_ready() and bool(self.recipients)


class WebhookConfig(_Model):
    enabled: bool = False
    url: str = ""


class NotifiersConfig(_Model):
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    webhook: WebhookConfig = Field(default_factory=WebhookConfig)


class WeeklyReportConfig(_Model):
    enabled: bool = False
    weekday: int = Field(default=1, ge=0, le=7)
    hour: int = Field(default=8, ge=0, le=23)
    send_telegram: bool = False
    send_email: bool = False


class ReportsConfig(_Model):
    weekly: WeeklyReportConfig = Field(default_factory=WeeklyReportConfig)
    default_sections: list[str] = Field(
        default_factory=lambda: [
            "summary",
            "server",
            "services",
            "latency",
            "alerts",
            "repository",
            "security",
            "recommendations",
        ]
    )


class ModulesConfig(_Model):
    ports: bool = False
    security: bool = False
    postgres: bool = False
    solr: bool = False
    backend: bool = False
    frontend: bool = False
    nginx: bool = False
    ssl: bool = False
    logs: bool = False
    repository: bool = False
    assetstore: bool = False
    incidents: bool = False

    def enabled_ids(self) -> list[str]:
        """Return the ids of every module that is switched on, in menu order."""
        return [mid for mid in MODULE_IDS if getattr(self, mid)]

    def as_dict(self) -> dict[str, bool]:
        """Return ``{module_id: bool}`` in menu order."""
        return {mid: bool(getattr(self, mid)) for mid in MODULE_IDS}


class AppearanceConfig(_Model):
    theme: ThemeKind = "system"


class Config(_Model):
    """The whole ``sentinel.yaml`` tree.

    ``demo`` is a runtime flag (set from ``SENTINEL_DEMO=1``), never written to YAML.

    Two private attributes decide what :func:`save_config` writes (see :func:`persisted_dict`):
    ``_file`` (set by :func:`load_config`, carried forward by :func:`merge_patch`) is the
    configuration as the YAML file alone describes it plus every patch since, so environment
    overrides and attribute assignments such as ``serve --host`` stay out of the file;
    ``_demo_base`` (set by :func:`apply_demo_overrides`) is the demo configuration before any UI
    change, so a demo save keeps only the difference.
    """

    server: ServerConfig = Field(default_factory=ServerConfig)
    setup: SetupConfig = Field(default_factory=SetupConfig)
    collector: CollectorConfig = Field(default_factory=CollectorConfig)
    dspace: DSpaceConfig = Field(default_factory=DSpaceConfig)
    postgres: PostgresConfig = Field(default_factory=PostgresConfig)
    solr: SolrConfig = Field(default_factory=SolrConfig)
    nginx: NginxConfig = Field(default_factory=NginxConfig)
    ssl: SSLConfig = Field(default_factory=SSLConfig)
    ports: list[PortRule] = Field(
        default_factory=lambda: [
            PortRule(port=22, label="SSH", expect="open"),
            PortRule(port=80, label="HTTP", expect="open"),
            PortRule(port=443, label="HTTPS", expect="open"),
            PortRule(port=4000, label="Angular", expect="open"),
            PortRule(port=8080, label="Backend", expect="open"),
            PortRule(port=8983, label="Solr", expect="local_only"),
            PortRule(port=5432, label="PostgreSQL", expect="local_only"),
        ]
    )
    logs: LogsConfig = Field(default_factory=LogsConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    notifiers: NotifiersConfig = Field(default_factory=NotifiersConfig)
    reports: ReportsConfig = Field(default_factory=ReportsConfig)
    modules: ModulesConfig = Field(default_factory=ModulesConfig)
    appearance: AppearanceConfig = Field(default_factory=AppearanceConfig)
    demo: bool = Field(default=False, exclude=True)
    _file: dict[str, Any] | None = PrivateAttr(default=None)
    _demo_base: dict[str, Any] | None = PrivateAttr(default=None)
    _telegram_env: tuple[str, str] | None = PrivateAttr(default=None)
    """``(SENTINEL_TELEGRAM_TOKEN, SENTINEL_TELEGRAM_CHAT_ID)`` as :func:`load_config` found
    them, so a PATCH of the destination list applies them again (:func:`apply_telegram_env`)."""
    _telegram_env_left_out: bool = PrivateAttr(default=False)
    """True while the environment's Telegram bot is left out because the file already has five
    (see :func:`apply_telegram_env`); a PATCH keeps it out until there is room again."""
    _notices: list[str] = PrivateAttr(default_factory=list)
    """Sentences about how the configuration was loaded, for the events log (:func:`startup_notices`)."""
    _smtp_password_env: bool = PrivateAttr(default=False)
    """True when :func:`load_config` took ``notifiers.email.password`` from
    ``SENTINEL_SMTP_PASSWORD``: the API then shows it as ``password_from_env`` and refuses to
    change it (a changed value would only last until the next restart)."""

    @property
    def hostname(self) -> str:
        """The label shown in the UI and alerts: ``server.hostname`` or the machine's hostname."""
        return self.server.hostname or socket.gethostname()

    def interval_for(self, interval_key: str) -> int:
        """Return the collector interval (seconds) for ``interval_key`` such as ``"http_interval"``."""
        return int(getattr(self.collector, interval_key))

    def module_enabled(self, module_id: str) -> bool:
        """True when ``module_id`` is on (the ``server`` module is always on)."""
        if module_id == "server":
            return True
        return bool(getattr(self.modules, module_id, False))

    def to_yaml_dict(self) -> dict[str, Any]:
        """Return the plain-JSON-compatible dict that is written to YAML (no runtime flags)."""
        return self.model_dump(mode="json")


DEFAULT_CONFIG: dict[str, Any] = Config().to_yaml_dict()
"""The built-in defaults as a plain dict, exactly as documented in SPEC section 3."""


def _port_rules(*ports: int) -> list[dict[str, Any]]:
    """The default ``ports`` rules for ``ports``, in that order."""
    by_port = {rule["port"]: rule for rule in DEFAULT_CONFIG["ports"]}
    return [dict(by_port[port]) for port in ports]


PERSONAS: dict[str, dict[str, Any]] = {
    "full": {
        "modules": {mid: True for mid in MODULE_IDS},
        "ports": _port_rules(22, 80, 443, 4000, 8080, 8983, 5432),
    },
    "backend": {
        "modules": {**{mid: True for mid in MODULE_IDS}, "frontend": False},
        "ports": _port_rules(22, 80, 443, 8080, 8983, 5432),
    },
    "frontend": {
        "modules": {
            **{mid: False for mid in MODULE_IDS},
            "nginx": True,
            "ssl": True,
            "frontend": True,
            "ports": True,
            "security": True,
            "logs": True,
            "backend": True,
        },
        "ports": _port_rules(22, 80, 443, 4000),
        "logs": {
            "files": [{"id": "nginx", "label": "nginx", "path": "/var/log/nginx/error.log"}],
        },
    },
    "server": {
        "modules": {
            **{mid: False for mid in MODULE_IDS},
            "ports": True,
            "security": True,
            "nginx": True,
            "ssl": True,
            "logs": True,
        },
        "ports": _port_rules(22),
    },
}
"""Onboarding persona presets: config patches applied by ``POST /api/actions/setup``.

Every preset switches the Security module on: it reads this server, whatever runs on it.

Each preset also sets the ``ports`` it expects, so a backend-only host or a plain server does not
wait for Angular on 4000 or Tomcat on 8080 (discovery then narrows the list to what listens)."""


# --------------------------------------------------------------------------- helpers


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Return a new dict: ``patch`` merged recursively into ``base`` (lists are replaced, not merged)."""
    out = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _get_path(tree: dict[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = tree
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _set_path(tree: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    node = tree
    for part in path[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[path[-1]] = value


def _del_path(tree: dict[str, Any], path: tuple[str, ...]) -> None:
    """Remove ``path`` from ``tree`` and any parent mapping that becomes empty."""
    parents: list[tuple[dict[str, Any], str]] = []
    node: Any = tree
    for part in path[:-1]:
        if not isinstance(node, dict) or not isinstance(node.get(part), dict):
            return
        parents.append((node, part))
        node = node[part]
    if not isinstance(node, dict) or path[-1] not in node:
        return
    del node[path[-1]]
    for parent, part in reversed(parents):
        if parent[part]:
            break
        del parent[part]


_MISSING = object()


def _changes(new: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    """The part of ``new`` that differs from ``base`` (recursive for mappings; lists compare whole)."""
    out: dict[str, Any] = {}
    for key, value in new.items():
        old = base.get(key, _MISSING)
        if isinstance(value, dict) and isinstance(old, dict):
            sub = _changes(value, old)
            if sub:
                out[key] = sub
        elif value != old:
            out[key] = copy.deepcopy(value)
    return out


def _without_server_and_secrets(tree: dict[str, Any]) -> dict[str, Any]:
    """Copy of ``tree`` without ``server``, ``demo``, every :data:`SECRET_PATHS` entry, every
    :data:`SECRET_ITEM_PATHS` item secret and the environment's Telegram destination."""
    out = copy.deepcopy(tree)
    out.pop("server", None)
    out.pop("demo", None)
    for secret in SECRET_PATHS:
        _del_path(out, secret)
    for list_path, key in SECRET_ITEM_PATHS:
        items = _get_path(out, list_path)
        if not isinstance(items, list):
            continue
        kept = [
            item for item in items if not (isinstance(item, dict) and item.get("id") == ENV_DESTINATION_ID)
        ]
        for item in kept:
            if isinstance(item, dict):
                item.pop(key, None)
        _set_path(out, list_path, kept)
    return out


def _telegram_section(tree: Any) -> dict[str, Any] | None:
    """``tree["notifiers"]["telegram"]`` when both are mappings, else ``None``."""
    notifiers = tree.get("notifiers") if isinstance(tree, dict) else None
    section = notifiers.get("telegram") if isinstance(notifiers, dict) else None
    return section if isinstance(section, dict) else None


def _prepare_file_tree(tree: dict[str, Any], *, mark: bool = False) -> dict[str, Any]:
    """A YAML tree (file or saved demo state) in today's shape: the old single bot becomes a
    destination (``mark``: tagged for :func:`apply_telegram_env`) and every destination has an id."""
    out = copy.deepcopy(tree)
    section = _telegram_section(out)
    if section is not None:
        migrate_legacy_telegram(section, mark=mark)
        if isinstance(section.get("chats"), list):
            assign_destination_ids(section["chats"])
    return out


_FILE_TIMES: tuple[tuple[str, Any, str, str], ...] = (
    ("quiet_hours", quiet_hours_value, "the quiet hours", "were"),
    ("daily_digest", digest_time_value, "the daily digest time", "was"),
)
"""``(key, reader, what a notice calls it, "was"/"were")`` of the times both channels have."""


QUOTE_THE_TIME = " (YAML reads an unquoted time as a number: write it in quotes)"
"""Added to the notice about a ``daily_digest`` that is a number no time of day is read as."""


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _yaml_time(value: Any) -> Any:
    """``value`` as it was written in the file when YAML made a number of a time of day.

    YAML 1.1 reads an unquoted ``18:00`` as the base-60 number 1080. The times it does that to
    are ``1:00`` … ``23:59`` (one with a leading zero stays a string), that is 60 … 1439: a whole
    number in that range is given back as ``HH:MM``. Any other value is returned as it is, a
    number below 60 too: no time becomes one, and ``8`` could as well mean eight o'clock.
    """
    if isinstance(value, int) and not isinstance(value, bool) and 60 <= value <= 1439:
        return f"{value // 60:02d}:{value % 60:02d}"
    return value


def _shown(value: Any, limit: int = 40) -> str:
    """``value`` as a notice quotes it: on one line, and at most ``limit`` characters of it."""
    if isinstance(value, str):
        return repr(value[:limit]) + ("…" if len(value) > limit else "")
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _readable_times(tree: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """A settings tree (the file's, or a saved demo session's) in which every ``quiet_hours`` and
    ``daily_digest`` can be read, and one sentence for each value that could not.

    Such a value is taken as ``""`` (no quiet hours, no digest: what it meant before the times
    were checked) and named with its key, for the log and the events log: a notification setting
    never stops Sentinel from starting. A ``daily_digest`` YAML read as a number is a time again
    (:func:`_yaml_time`). ``PATCH /api/config`` stays strict (:func:`merge_patch`).
    """
    out = copy.deepcopy(tree)
    ignored: list[str] = []
    notifiers = out.get("notifiers")
    for channel in ("telegram", "email"):
        section = notifiers.get(channel) if isinstance(notifiers, dict) else None
        if not isinstance(section, dict):
            continue
        for key, read, what, verb in _FILE_TIMES:
            if key not in section:
                continue
            value = section[key]
            try:
                section[key] = read(_yaml_time(value) if key == "daily_digest" else value)
            except ValueError as exc:
                hint = QUOTE_THE_TIME if key == "daily_digest" and _is_number(value) else ""
                ignored.append(
                    f"{what} {_shown(value)} in notifiers.{channel}.{key} {verb} ignored: {exc}{hint}"
                )
                section[key] = ""
    return out, ignored


def _readable_zone(tree: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """``tree`` with a ``server.timezone`` that names no loadable zone taken as ``""`` (the server's
    zone), and the sentence saying so: like a notification time, the zone never stops the start.
    ``PATCH /api/config`` stays strict."""
    from dspace_sentinel.util.tz import check_zone

    server = tree.get("server") if isinstance(tree, dict) else None
    if not isinstance(server, dict) or "timezone" not in server:
        return tree, []
    try:
        check_zone(server["timezone"])
    except ValueError as exc:
        out = copy.deepcopy(tree)
        out["server"]["timezone"] = ""
        return out, [f"the time zone in server.timezone was ignored, so times use the server's zone: {exc}"]
    return tree, []


def apply_telegram_env(
    tree: dict[str, Any], token: str | None, chat_id: str | None, *, leave_out_when_full: bool = False
) -> bool:
    """Apply ``SENTINEL_TELEGRAM_TOKEN`` / ``SENTINEL_TELEGRAM_CHAT_ID`` to a config tree (in place).

    * A destination made from an old single-bot file (marked by :func:`migrate_legacy_telegram`):
      each variable replaces its value, as it always did.
    * Otherwise the token alone is the bot token of every destination saved without one (an old
      "token in config/sentinel.env, chat id in the file" set-up keeps working after a UI save),
      and both together add the read-only destination ``{id: "env", label: "From environment"}``
      unless a destination already has that same bot and chat.
    * ``leave_out_when_full``: when the list already holds :data:`TELEGRAM_MAX_BOTS` destinations
      the environment's is left out (and ``True`` returned) instead of making a sixth that
      validation would refuse; loading uses it, so a notification limit never stops the start.

    The result is only ever the running configuration: the file layer never sees it.
    """
    token = (token or "").strip()
    chat_id = (chat_id or "").strip()
    left_out = False
    section = _telegram_section(tree)
    if section is None:
        if not (token and chat_id):
            return left_out
        notifiers = tree.setdefault("notifiers", {})
        if not isinstance(notifiers, dict):
            return left_out
        section = notifiers.setdefault("telegram", {})
        if not isinstance(section, dict):
            return left_out
    raw = section.get("chats")
    if raw is not None and not isinstance(raw, list):
        return left_out  # invalid; validation reports it
    chats: list[Any] = [dict(item) if isinstance(item, dict) else item for item in (raw or [])]
    legacy = next((c for c in chats if isinstance(c, dict) and c.pop(_LEGACY_MARK, False)), None)
    for item in chats:
        if isinstance(item, dict):
            item.pop(_LEGACY_MARK, None)
    if legacy is not None:
        if token:
            legacy["bot_token"] = token
        if chat_id:
            legacy["chat_id"] = chat_id
    else:
        if token:
            for item in chats:
                if isinstance(item, dict) and not _text(item.get("bot_token")).strip():
                    item["bot_token"] = token
        already = any(
            isinstance(item, dict)
            and _text(item.get("bot_token")).strip() == token
            and _text(item.get("chat_id")).strip().lower() == chat_id.lower()
            for item in chats
        )
        if token and chat_id and not already:
            if leave_out_when_full and len(chats) >= TELEGRAM_MAX_BOTS:
                left_out = True
            else:
                chats.append(
                    {
                        "id": ENV_DESTINATION_ID,
                        "label": ENV_DESTINATION_LABEL,
                        "bot_token": token,
                        "chat_id": chat_id,
                        "min_severity": "warning",
                    }
                )
    if chats or raw is not None:
        section["chats"] = chats
    return left_out


def _telegram_env_values(env: Any) -> tuple[str, str]:
    """``(token, chat_id)`` from the environment, refusing values that cannot be a bot or a chat."""
    token = (env.get("SENTINEL_TELEGRAM_TOKEN") or "").strip()
    chat_id = (env.get("SENTINEL_TELEGRAM_CHAT_ID") or "").strip()
    if token and not _BOT_TOKEN.match(token):
        raise ValueError(f"SENTINEL_TELEGRAM_TOKEN is {BOT_TOKEN_ERROR}")
    if chat_id and not _CHAT_ID.match(chat_id):
        raise ValueError(f"SENTINEL_TELEGRAM_CHAT_ID is not a Telegram chat id: {chat_id[:40]!r}")
    return token, chat_id


def _normalised(tree: dict[str, Any]) -> dict[str, Any]:
    """``tree`` as a clean full config dict (validated and dumped; kept as is when invalid)."""
    try:
        return Config.model_validate(tree).to_yaml_dict()
    except ValidationError:
        return copy.deepcopy(tree)


def persisted_dict(cfg: Config) -> dict[str, Any]:
    """What :func:`save_config` writes for ``cfg``.

    * a demo config: only what the UI changed (the difference from the demo base), never
      ``server.*`` or a secret — those always come from the real configuration;
    * a config from :func:`load_config`: the file layer (the YAML's own values plus every
      :func:`merge_patch` since), never ``SENTINEL_*`` environment values or CLI flags;
    * any other config (built in code): the effective configuration.
    """
    if cfg.demo:
        base = cfg._demo_base if cfg._demo_base is not None else {}
        return _without_server_and_secrets(_changes(cfg.to_yaml_dict(), base))
    if cfg._file is not None:
        return copy.deepcopy(cfg._file)
    return cfg.to_yaml_dict()


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def read_yaml(path: Path) -> dict[str, Any]:
    """Read a YAML mapping from ``path``; a missing or empty file yields ``{}``.

    Raises ``ValueError`` when the file exists but is not a mapping or fails to parse.
    """
    if not path.exists():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"cannot parse {path}: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} must contain a YAML mapping at the top level")
    return loaded


def apply_env_overrides(
    data: dict[str, Any], env: dict[str, str] | None = None, *, notices: list[str] | None = None
) -> dict[str, Any]:
    """Apply ``SENTINEL_*`` environment overrides to a plain config dict (returns a new dict).

    The Telegram variables go through :func:`apply_telegram_env`; when their bot would be a
    sixth it is left out, :data:`ENV_BOT_LEFT_OUT` is logged and added to ``notices``.
    """
    env = os.environ if env is None else env  # type: ignore[assignment]
    out = copy.deepcopy(data)

    def put(path: tuple[str, ...], raw: str | None, cast: Any = str) -> None:
        if raw is None or raw.strip() == "":
            return
        try:
            _set_path(out, path, cast(raw.strip()))
        except (TypeError, ValueError):
            return

    put(("server", "host"), env.get("SENTINEL_HOST"))
    put(("server", "port"), env.get("SENTINEL_PORT"), int)
    put(("server", "auth_token"), env.get("SENTINEL_AUTH_TOKEN"))
    put(("postgres", "password"), env.get("SENTINEL_PG_PASSWORD"))
    put(("notifiers", "email", "password"), env.get("SENTINEL_SMTP_PASSWORD"))
    token, chat_id = env.get("SENTINEL_TELEGRAM_TOKEN"), env.get("SENTINEL_TELEGRAM_CHAT_ID")
    if apply_telegram_env(out, token, chat_id, leave_out_when_full=True):
        _notice(notices, ENV_BOT_LEFT_OUT)
    return out


def _notice(notices: list[str] | None, text: str) -> None:
    """Log ``text`` as a warning and keep it (as a sentence) for the events log."""
    log.warning("%s", text)
    sentence = text[:1].upper() + text[1:]
    if notices is not None and sentence not in notices:
        notices.append(sentence)


def _email_notice(cfg: Config, notices: list[str]) -> None:
    """Keep ``notices`` in step with :meth:`EmailConfig.problem` (in place): the notice for the
    problem ``cfg`` has is logged and added unless it is there already, and one about a problem
    it no longer has (a saved demo session changed the settings) is dropped."""
    problem = cfg.notifiers.email.problem()
    text = f"{EMAIL_NOT_READY}{problem}" if problem else ""
    sentence = text[:1].upper() + text[1:]
    prefix = EMAIL_NOT_READY[:1].upper() + EMAIL_NOT_READY[1:]
    notices[:] = [n for n in notices if not n.startswith(prefix) or n == sentence]
    if text and sentence not in notices:
        _notice(notices, text)


def startup_notices(cfg: Config) -> list[str]:
    """Sentences about how ``cfg`` was loaded that ``serve`` records once in the events log
    (the environment's Telegram bot left out because there were already five, a ``quiet_hours``
    or ``daily_digest`` in the file that could not be read)."""
    return list(cfg._notices)


def _carry(src: Config, dst: Config) -> Config:
    """Copy the runtime flag and the private layers that follow a config through edits."""
    dst.demo = src.demo
    dst._demo_base = src._demo_base
    dst._telegram_env = src._telegram_env
    dst._telegram_env_left_out = src._telegram_env_left_out
    dst._notices = list(src._notices)
    dst._smtp_password_env = src._smtp_password_env
    return dst


def apply_demo_overrides(cfg: Config) -> Config:
    """Return a copy of ``cfg`` prepared for demo mode.

    Every module is on, onboarding is shown again (``setup.completed=false``), the
    hostname reads ``demo-dspace`` and ``cfg.demo`` is true.
    """
    data = cfg.to_yaml_dict()
    data["modules"] = {mid: True for mid in MODULE_IDS}
    data["setup"] = {"completed": False, "persona": ""}
    data["server"]["hostname"] = "demo-dspace"
    demo = Config.model_validate(data)
    demo.demo = True
    demo._telegram_env = cfg._telegram_env
    demo._telegram_env_left_out = cfg._telegram_env_left_out
    demo._notices = list(cfg._notices)
    demo._smtp_password_env = cfg._smtp_password_env
    demo._demo_base = demo.to_yaml_dict()
    return demo


def load_demo_state(cfg: Config, path: Path | None = None) -> Config:
    """Merge the settings a previous demo session saved (``data/demo/demo-sentinel.yaml``) into ``cfg``.

    ``cfg`` is a demo config (see :func:`apply_demo_overrides`); the file holds whatever the UI
    changed while the demo ran (onboarding, modules, thresholds, theme), so a restarted demo
    skips onboarding. Server settings and secrets in the file are ignored (they always come from
    the real configuration). A ``quiet_hours`` or ``daily_digest`` that cannot be read is left
    out and named in the start-up notices, as :func:`load_config` does; any other missing, empty
    or unusable file leaves ``cfg`` unchanged.
    """
    path = path or paths.demo_config_path()
    try:
        state = read_yaml(path)
    except (OSError, ValueError):
        return cfg
    state, ignored = _readable_times(state)
    state = _without_server_and_secrets(_prepare_file_tree(state))
    if not state:
        return cfg
    merged_tree = deep_merge(cfg.to_yaml_dict(), state)
    section = _telegram_section(state)
    left_out = None
    if section is not None and "chats" in section:  # the saved list replaced the environment's bot
        token, chat_id = cfg._telegram_env or ("", "")
        left_out = apply_telegram_env(merged_tree, token, chat_id, leave_out_when_full=True)
    try:
        merged = Config.model_validate(merged_tree)
    except ValidationError:
        return cfg
    _carry(cfg, merged)
    for text in ignored:
        _notice(merged._notices, text)
    _email_notice(merged, merged._notices)
    if left_out is not None:
        merged._telegram_env_left_out = left_out
        if left_out:
            _notice(merged._notices, ENV_BOT_LEFT_OUT)
    merged._demo_base = cfg._demo_base if cfg._demo_base is not None else cfg.to_yaml_dict()
    return merged


def load_config(path: Path | None = None, *, demo: bool | None = None) -> Config:
    """Load the effective configuration: defaults ← YAML at ``path`` ← environment.

    ``path`` defaults to :func:`paths.config_path`. ``demo`` defaults to
    ``SENTINEL_DEMO`` being truthy; in demo mode :func:`apply_demo_overrides` is applied.
    A missing YAML file is fine (defaults are used); an unparsable one raises ``ValueError``,
    and so does a ``SENTINEL_TELEGRAM_*`` value that cannot be a bot token or a chat id.

    A notification setting never stops the start: a ``quiet_hours`` or ``daily_digest`` that
    cannot be read is taken as ``""``, the environment's Telegram bot is left out when it would be
    a sixth, and email settings that can never send (:meth:`EmailConfig.problem`) load as they
    are, with email not ready; each is logged as a warning and kept for the events log
    (:func:`startup_notices`). So is a ``server.timezone`` that names no zone this system knows:
    times then use the server's zone. A ``server.login`` that cannot be used loads as it is and
    stays on, with the notice :data:`LOGIN_UNREADABLE` (outside the demo): nobody can sign in
    until ``set-password`` writes a new login, and the dashboard never opens because of it.
    """
    path = path or paths.config_path()
    notices: list[str] = []
    raw, ignored = _readable_times(read_yaml(path))
    raw, zone_ignored = _readable_zone(raw)
    for text in [*ignored, *zone_ignored]:
        _notice(notices, text)
    file_layer = deep_merge(DEFAULT_CONFIG, _prepare_file_tree(raw))
    telegram_env = _telegram_env_values(os.environ)
    data = apply_env_overrides(
        deep_merge(DEFAULT_CONFIG, _prepare_file_tree(raw, mark=True)), notices=notices
    )
    try:
        cfg = Config.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"invalid configuration in {path}: {_problems(exc)}") from exc
    _email_notice(cfg, notices)
    if demo is None:
        demo = _env_truthy("SENTINEL_DEMO")
    login_unusable = login_problem(cfg.server.login.username, cfg.server.login.password_hash)
    if login_unusable and not demo:  # the demo never asks for a login
        _notice(notices, login_unusable)
    cfg._file = _normalised(file_layer)
    cfg._telegram_env = telegram_env
    cfg._telegram_env_left_out = any(n.lower() == ENV_BOT_LEFT_OUT.lower() for n in notices)
    cfg._notices = notices
    cfg._smtp_password_env = bool((os.environ.get("SENTINEL_SMTP_PASSWORD") or "").strip())
    if demo:
        cfg = apply_demo_overrides(cfg)
    return cfg


def apply_login(cfg: Config, username: str, password_hash: str) -> None:
    """Put a login into ``cfg`` in place: the running settings and the file layer, so the next
    save (by the UI or the CLI) writes it and never an older one. ``("", "")`` removes the login."""
    cfg.server.login = LoginConfig(username=username, password_hash=password_hash)
    if cfg._file is not None:
        server = cfg._file.get("server")
        if not isinstance(server, dict):
            server = cfg._file["server"] = {}
        login = cfg.server.login
        server["login"] = {"username": login.username, "password_hash": login.password_hash}


class _Quoted(str):
    """A string :func:`save_config` writes in double quotes whatever it holds."""


class _FileDumper(yaml.SafeDumper):
    """``yaml.safe_dump``'s dumper plus :class:`_Quoted`."""


_FileDumper.add_representer(
    _Quoted, lambda dumper, value: dumper.represent_scalar("tag:yaml.org,2002:str", str(value), style='"')
)


def _quote_times(tree: dict[str, Any]) -> dict[str, Any]:
    """``tree`` with every ``quiet_hours`` and ``daily_digest`` marked to be written in quotes.

    PyYAML writes ``08:00`` and ``23:00-07:00`` plain (they read back as strings), so a time
    changed by hand to ``10:00`` on such a line became the number 600. Written as ``"08:00"``,
    the line keeps its quotes through a hand edit.
    """
    for channel in ("telegram", "email"):
        section = _get_path(tree, ("notifiers", channel))
        if not isinstance(section, dict):
            continue
        for key, _read, _what, _verb in _FILE_TIMES:
            if isinstance(section.get(key), str):
                section[key] = _Quoted(section[key])
    return tree


def save_config(cfg: Config, path: Path | None = None) -> None:
    """Write ``cfg`` to YAML atomically (temp file in the same directory + rename), mode 0600.

    What is written is :func:`persisted_dict`: environment values, CLI flags and (in demo mode)
    anything the UI did not change, server settings and secrets stay out of the file. The times
    (``quiet_hours``, ``daily_digest``) are written in double quotes (:func:`_quote_times`).
    """
    path = path or paths.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if cfg.demo:
        text = "# DSpace Sentinel demo settings — only what the UI changed; delete the file to start over\n"
    else:
        text = "# DSpace Sentinel configuration — edited by the UI; see config/sentinel.example.yaml\n"
    text += yaml.dump(
        _quote_times(persisted_dict(cfg)),
        Dumper=_FileDumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    fd, tmp_name = tempfile.mkstemp(prefix=".sentinel-", suffix=".yaml", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


PG_TARGET_FIELDS: tuple[str, ...] = ("host", "port", "database", "user")
"""The PostgreSQL settings that decide which server receives ``postgres.password``."""


def _pg_target(pg: PostgresConfig) -> tuple[str, int, str, str]:
    return pg.host.strip().lower(), pg.port, pg.database.strip(), pg.user.strip()


EMAIL_TARGET_FIELDS: tuple[str, ...] = ("smtp_host", "smtp_port", "security", "username")
"""The email settings that decide which server (and login) receives ``notifiers.email.password``."""

EMAIL_REPOINTED = (
    "enter the email password again: the SMTP server, port, security or username changed and "
    "the saved password is only sent to the server it was saved for"
)

EMAIL_PASSWORD_FROM_ENV = (
    "the email password comes from SENTINEL_SMTP_PASSWORD in config/sentinel.env: change it there"
)

EMAIL_REPOINTED_ENV = (
    "the email password comes from SENTINEL_SMTP_PASSWORD in config/sentinel.env and is only sent "
    "to the server it was set for: change the SMTP server, port, security or username in "
    "config/sentinel.yaml"
)


def _email_section(tree: Any) -> dict[str, Any] | None:
    """``tree["notifiers"]["email"]`` when both are mappings, else ``None``."""
    notifiers = tree.get("notifiers") if isinstance(tree, dict) else None
    section = notifiers.get("email") if isinstance(notifiers, dict) else None
    return section if isinstance(section, dict) else None


def _typed_email_password(patch: dict[str, Any]) -> bool:
    """True when ``patch`` sets a new ``notifiers.email.password`` (anything but the mask)."""
    section = _email_section(patch)
    return section is not None and "password" in section and section["password"] != MASK


def _smtp_target(email: EmailConfig) -> tuple[str, int, str, str]:
    return (
        email.smtp_host.strip().lower().rstrip("."),
        email.smtp_port,
        email.security,
        email.username.strip(),
    )


def _check_email_target(cfg: Config, new_cfg: Config, patch: dict[str, Any]) -> None:
    """Refuse a patch that would send the saved email password to another server or login.

    Like PostgreSQL's: when a password is stored and the patch changes ``smtp_host`` (ignoring
    case), ``smtp_port``, ``security`` or ``username`` without a newly typed password (left out
    or sent as the mask), the stored one stays behind. A server that needs no login (empty
    ``username``) is never sent the password, so pointing Sentinel at one is always fine. A
    password from ``SENTINEL_SMTP_PASSWORD`` cannot be typed again here: its server and login are
    changed in ``config/sentinel.yaml``.
    """
    old, new = cfg.notifiers.email, new_cfg.notifiers.email
    if not old.password or not new.username or _smtp_target(new) == _smtp_target(old):
        return
    if not _typed_email_password(patch):
        raise ValueError(EMAIL_REPOINTED_ENV if cfg._smtp_password_env else EMAIL_REPOINTED)


def _check_email_settings(cfg: Config, new_cfg: Config, patch: dict[str, Any]) -> None:
    """Refuse a patch of the email settings that leaves them unable to ever send.

    The model itself accepts such settings, so that a file holding them (saved before they were
    checked) never stops Sentinel from starting; saving through the API stays strict. A patch
    that does not touch ``notifiers.email`` is not held up by what the file brought along, and a
    password from ``SENTINEL_SMTP_PASSWORD`` (which a patch can neither set nor mend) never holds
    up the other email settings: email then simply stays not ready.
    """
    if _email_section(patch) is None:
        return
    problem = new_cfg.notifiers.email.problem(password=not cfg._smtp_password_env)
    if problem:
        raise ValueError(f"invalid configuration: notifiers.email: {problem}")


def _check_typed_email_password(patch: dict[str, Any]) -> None:
    """Refuse a newly typed email password that SMTP could never send (a character outside
    ASCII: :data:`PASSWORD_NOT_SENDABLE`), with or without a login to send it with."""
    section = _email_section(patch)
    if _typed_email_password(patch) and section is not None and not _text(section["password"]).isascii():
        raise ValueError(f"invalid configuration: notifiers.email: {PASSWORD_NOT_SENDABLE}")


def _problems(exc: ValidationError) -> str:
    """``loc: message; …`` for a validation error (pydantic's "Value error, " prefix dropped)."""
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc'])}: {str(err['msg']).removeprefix('Value error, ')}"
        for err in exc.errors()
    )


def _prepare_telegram_patch(patch: dict[str, Any], cfg: Config) -> bool:
    """Bring a PATCH's Telegram section into today's shape (in place); True when it sets ``chats``.

    * ``chats`` sent: the old single keys are dropped, and so is the environment's destination
      (read-only: it is added back from the environment) and any ``from_env`` flag.
    * only the old ``bot_token`` / ``chat_id`` sent (an older client): they edit the one
      destination such a client knows, the first saved one (its id, label and severity kept; a
      token left out stays masked so the stored one is kept), which becomes the whole list —
      or no list at all when both are empty.
    """
    section = _telegram_section(patch)
    if section is None:
        return False
    if "chats" in section:
        section.pop("bot_token", None)
        section.pop("chat_id", None)
        chats = section["chats"]
        if isinstance(chats, list):
            kept: list[Any] = []
            for item in chats:
                if isinstance(item, dict):
                    if _text(item.get("id")).strip() == ENV_DESTINATION_ID:
                        continue
                    item = {k: v for k, v in item.items() if k != "from_env"}
                kept.append(item)
            section["chats"] = kept
        return True
    if "bot_token" not in section and "chat_id" not in section:
        return False
    first = next((c for c in cfg.notifiers.telegram.chats if not c.from_env), None)
    destination: dict[str, Any] = (
        first.model_dump(mode="json")
        if first is not None
        else {"id": "", "label": "", "bot_token": "", "chat_id": "", "min_severity": "warning"}
    )
    destination["bot_token"] = MASK if first is not None and first.bot_token else ""
    if "bot_token" in section:
        destination["bot_token"] = _text(section.pop("bot_token")).strip()
    if "chat_id" in section:
        destination["chat_id"] = _text(section.pop("chat_id")).strip()
    section["chats"] = [destination] if (destination["bot_token"] or destination["chat_id"]) else []
    return True


def _restore_masked_tokens(patch: dict[str, Any], layer_patch: dict[str, Any], cfg: Config) -> None:
    """Replace every masked item secret by the stored value of the item with the same id.

    In the running config that is the current value; in the file layer it is the file's value,
    or ``""`` when the current value came from the environment (so it keeps coming from there).
    A masked secret without a known id raises ``ValueError``. An item that leaves the secret out
    altogether keeps the stored one too when its id is known (a new item simply has none).
    """
    for list_path, key in SECRET_ITEM_PATHS:
        items = _get_path(patch, list_path)
        layer_items = _get_path(layer_patch, list_path)
        if not isinstance(items, list) or not isinstance(layer_items, list):
            continue
        stored = {
            _text(item.get("id")): _text(item.get(key))
            for item in (_get_path(cfg.to_yaml_dict(), list_path) or [])
            if isinstance(item, dict) and item.get("id") != ENV_DESTINATION_ID
        }
        in_file = None
        if cfg._file is not None:
            in_file = {
                _text(item.get("id")): _text(item.get(key))
                for item in (_get_path(cfg._file, list_path) or [])
                if isinstance(item, dict)
            }
        for item, layer_item in zip(items, layer_items, strict=True):
            if not isinstance(item, dict):
                continue
            item_id = _text(item.get("id")).strip()
            if key not in item and item_id in stored:
                item[key] = layer_item[key] = MASK  # left out: kept, like the mask
            if item.get(key) != MASK:
                continue
            if not item_id or item_id not in stored:
                name = _text(item.get("label")).strip() or _text(item.get("chat_id")).strip() or "the new bot"
                raise ValueError(f"enter the bot token for {name} again")
            item[key] = stored[item_id]
            if in_file is None:
                layer_item[key] = stored[item_id]
            else:
                file_value = in_file.get(item_id, "")
                layer_item[key] = file_value if file_value == stored[item_id] else ""


def merge_patch(cfg: Config, patch: dict[str, Any]) -> Config:
    """Deep-merge ``patch`` into ``cfg`` and validate; used by ``PATCH /api/config``.

    Secrets sent as :data:`MASK` keep their current value (in the file layer: the file's value,
    so a secret from the environment is not copied into the file); a masked Telegram bot token
    keeps the token of the destination with the same ``id``. Lists (``chats``, ``recipients``,
    ``ports`` …) are replaced, not merged. The runtime ``demo`` flag, the file layer and the demo
    base are carried forward. Raises ``ValueError`` with a readable message on invalid input, for
    a masked bot token whose destination id is missing or unknown, and when the patch re-points
    PostgreSQL (``host``, ``port``, ``database`` or ``user``) or the email login (``smtp_host``,
    ``smtp_port``, ``security`` or ``username``) without a new password while one is stored: a
    saved password is only ever sent to the server it was saved for. A patch of the email
    settings that leaves them unable to send (:meth:`EmailConfig.problem`) is refused as well,
    and so is a newly typed email password with a character SMTP cannot send
    (:data:`PASSWORD_NOT_SENDABLE`).
    """
    if not isinstance(patch, dict):
        raise ValueError("patch must be a JSON object")
    current = cfg.to_yaml_dict()
    cleaned = copy.deepcopy(patch)
    cleaned.pop("demo", None)
    email_patch = _email_section(cleaned)
    if email_patch is not None:
        email_patch.pop("password_from_env", None)  # a GET → PATCH round trip sends the flag back
        if cfg._smtp_password_env and _typed_email_password(cleaned):
            raise ValueError(EMAIL_PASSWORD_FROM_ENV)
        _check_typed_email_password(cleaned)
    chats_patched = _prepare_telegram_patch(cleaned, cfg)
    layer_patch = copy.deepcopy(cleaned)
    for secret in SECRET_PATHS:
        if _get_path(cleaned, secret) == MASK:
            _set_path(cleaned, secret, _get_path(current, secret) or "")
            _set_path(layer_patch, secret, _get_path(cfg._file or {}, secret) or "")
    if chats_patched:
        _restore_masked_tokens(cleaned, layer_patch, cfg)
        chats = _get_path(cleaned, ("notifiers", "telegram", "chats"))
        layer_chats = _get_path(layer_patch, ("notifiers", "telegram", "chats"))
        if isinstance(chats, list) and isinstance(layer_chats, list):
            assign_destination_ids(chats)
            for item, layer_item in zip(chats, layer_chats, strict=True):
                if isinstance(item, dict) and isinstance(layer_item, dict):
                    layer_item["id"] = item["id"]
    merged = deep_merge(current, cleaned)
    env_left_out = cfg._telegram_env_left_out
    if chats_patched:
        # while the environment's bot is left out (the file had five) the UI's list keeps it out;
        # otherwise it counts toward five and a sixth bot is refused, as always
        token, chat_id = cfg._telegram_env or ("", "")
        env_left_out = apply_telegram_env(merged, token, chat_id, leave_out_when_full=env_left_out)
    try:
        new_cfg = Config.model_validate(merged)
    except ValidationError as exc:
        raise ValueError(f"invalid configuration: {_problems(exc)}") from exc
    _check_email_settings(cfg, new_cfg, cleaned)
    pg_patch = patch.get("postgres") if isinstance(patch.get("postgres"), dict) else {}
    typed_password = "password" in pg_patch and pg_patch["password"] != MASK
    repointed = _pg_target(new_cfg.postgres) != _pg_target(cfg.postgres)
    if cfg.postgres.password and repointed and not typed_password:
        raise ValueError(
            "enter the PostgreSQL password again: postgres.host, port, database or user changed and "
            "the saved password is only sent to the server it was saved for"
        )
    _check_email_target(cfg, new_cfg, patch)
    _carry(cfg, new_cfg)
    new_cfg._telegram_env_left_out = env_left_out
    if cfg._file is not None:
        new_cfg._file = _normalised(deep_merge(cfg._file, layer_patch))
    return new_cfg


def masked(cfg: Config) -> dict[str, Any]:
    """Return the config as a dict with every non-empty secret replaced by :data:`MASK`.

    Includes ``demo`` (runtime flag) so clients can tell demo mode apart, ``from_env`` on every
    Telegram destination (true for the read-only one made from the environment) and
    ``notifiers.email.password_from_env`` (true when the password comes from
    ``SENTINEL_SMTP_PASSWORD``, which the API then refuses to change).
    """
    data = cfg.to_yaml_dict()
    for secret in SECRET_PATHS:
        if _get_path(data, secret):
            _set_path(data, secret, MASK)
    for list_path, key in SECRET_ITEM_PATHS:
        for item in _get_path(data, list_path) or []:
            if isinstance(item, dict) and item.get(key):
                item[key] = MASK
    for chat in _get_path(data, ("notifiers", "telegram", "chats")) or []:
        chat["from_env"] = chat.get("id") == ENV_DESTINATION_ID
    data["notifiers"]["email"]["password_from_env"] = bool(cfg._smtp_password_env)
    data["demo"] = bool(cfg.demo)
    return data


def dspace_modules_enabled(cfg: Config) -> bool:
    """True when any DSpace-specific module (postgres/solr/backend/frontend/repository/assetstore) is on."""
    return any(getattr(cfg.modules, mid) for mid in DSPACE_MODULE_IDS)


def apply_persona(cfg: Config, persona: str) -> Config:
    """Return ``cfg`` with the :data:`PERSONAS` preset ``persona`` applied and ``setup.persona`` set.

    Raises ``ValueError`` for an unknown persona.
    """
    if persona not in PERSONAS:
        raise ValueError(f"unknown persona {persona!r}; expected one of {', '.join(PERSONAS)}")
    patch = deep_merge(PERSONAS[persona], {"setup": {"persona": persona}})
    return merge_patch(cfg, patch)


__all__ = [
    "API_SERVER_KEYS",
    "BOT_TOKEN_ERROR",
    "DEFAULT_CONFIG",
    "DIGEST_TIME_ERROR",
    "DSPACE_MODULE_IDS",
    "EMAIL_MAX_RECIPIENTS",
    "EMAIL_NOT_READY",
    "EMAIL_PASSWORD_FROM_ENV",
    "EMAIL_REPOINTED",
    "EMAIL_REPOINTED_ENV",
    "EMAIL_TARGET_FIELDS",
    "ENV_BOT_LEFT_OUT",
    "LOGIN_UNREADABLE",
    "ENV_DESTINATION_ID",
    "ENV_DESTINATION_LABEL",
    "MASK",
    "MODULE_IDS",
    "PASSWORD_NOT_SENDABLE",
    "PERSONAS",
    "PG_TARGET_FIELDS",
    "QUIET_HOURS_EQUAL_ERROR",
    "QUIET_HOURS_ERROR",
    "SECRET_ITEM_PATHS",
    "SECRET_PATHS",
    "SENDER_NOT_AN_ADDRESS",
    "TELEGRAM_MAX_BOTS",
    "USERNAME_NOT_SENDABLE",
    "AlertsConfig",
    "AppearanceConfig",
    "CollectorConfig",
    "Config",
    "DSpaceConfig",
    "EmailConfig",
    "EmailRecipient",
    "LogFileConfig",
    "LoginConfig",
    "LogsConfig",
    "ModulesConfig",
    "NginxConfig",
    "NotifiersConfig",
    "PortRule",
    "PostgresConfig",
    "ReportsConfig",
    "SSLConfig",
    "ServerConfig",
    "SetupConfig",
    "SolrConfig",
    "TelegramChat",
    "TelegramConfig",
    "Threshold",
    "WebhookConfig",
    "WeeklyReportConfig",
    "apply_demo_overrides",
    "apply_env_overrides",
    "apply_login",
    "apply_persona",
    "apply_telegram_env",
    "assign_destination_ids",
    "deep_merge",
    "digest_time_value",
    "dspace_modules_enabled",
    "is_address",
    "load_config",
    "load_demo_state",
    "masked",
    "merge_patch",
    "migrate_legacy_telegram",
    "persisted_dict",
    "plain_address",
    "quiet_hours_value",
    "server_access",
    "read_yaml",
    "save_config",
    "startup_notices",
]
