# DSpace Sentinel — Build Specification (v0.1)

This document is the contract every part of the codebase is built against. If code and
this spec disagree, fix one of them deliberately and say so in the commit.

DSpace Sentinel is a lightweight, open‑source observability and health platform built
specifically for DSpace 7, 8 and 9 repository servers. It runs *next to* DSpace on the same
machine, observes (never touches) the stack, tells a dependency‑aware story about health,
alerts through Telegram and email, keeps history, correlates incidents and generates reports.
DSpace 7.x is end-of-life (7.6.7 was its final release); 10.x behaves like 9 as far as
Sentinel is concerned and is handled, but not claimed. What differs between the versions and
what Sentinel does about it is in `docs/COMPATIBILITY.md`.

```
git clone https://github.com/Danindu05/dspace-sentinel.git
cd dspace-sentinel
./sentinel.sh install
./sentinel.sh start        # → http://localhost:9765
```

---

## 0. Non‑negotiables

1. **Observe only.** Sentinel never restarts, writes to, reindexes, or reconfigures
   DSpace, PostgreSQL, Solr, Tomcat, Node or nginx. All PostgreSQL sessions are
   `READ ONLY`, `statement_timeout=3000`, `application_name='dspace-sentinel'`. All Solr
   queries use `rows=0`. Log files are read from the tail only (seek from end). The Security
   module (§6) shows the server's security status and never changes it: it never opens or
   closes a port, never changes a firewall, never runs sudo; it reads the files listed there,
   the socket table, `systemctl is-active`, and once per run the response headers of the
   public URL.
2. **Cheap.** Default footprint target: < 1 % CPU average, < 150 MB RSS. Every external
   check has a hard timeout (TCP 2 s, HTTP 5 s, DB 5 s, subprocess 3 s). The systemd unit
   runs with `Nice=10`, `IOSchedulingClass=best-effort`, `IOSchedulingPriority=7`,
   `CPUQuota=25%`, `MemoryMax=300M`.
3. **Zero build step.** Python 3.10+ standard venv; the web UI is plain HTML/CSS/ES
   modules served by FastAPI. No Node needed on the server. Works offline (fonts fall back).
4. **Port 9765**, bound to `127.0.0.1` by default. Exposing it is an explicit config change
   (documented with an nginx + HTTPS recipe) and is enforced: `serve` refuses a non‑loopback
   `server.host` without the dashboard login (`server.login`) or `server.auth_token` (exit 4)
   unless the operator opts in with `server.allow_unauthenticated: true` or
   `SENTINEL_ALLOW_OPEN=1`. The dashboard login (one admin account: a sign‑in page, sessions,
   `set-password`; never in demo mode) and `server.auth_token` as a token for scripts; the API
   answers only to known `Host` names and refuses cross‑site writes (§9).
5. **Modules are off by default** (except the server module). A module that is off leaves
   no trace: no checks, no tiles, no charts, no menu rows, no alerts.
6. **Everything the UI shows is also reachable from the JSON API and the CLI.**
7. **Python 3.10 compatible** (`from __future__ import annotations`; no 3.11‑only syntax).

---

## 1. Repository layout

```
dspace-sentinel/
├── sentinel.sh                     # the one command: install|start|stop|status|logs|doctor|report|demo|...
├── README.md  LICENSE (MIT)  CHANGELOG.md  CONTRIBUTING.md  SECURITY.md  CODE_OF_CONDUCT.md
├── requirements.txt  requirements-dev.txt  pyproject.toml  ruff.toml
├── config/
│   └── sentinel.example.yaml       # fully commented; copied to config/sentinel.yaml on install (git‑ignored)
├── dspace_sentinel/                # Python package
│   ├── __init__.py                 # __version__ = "0.1.0"
│   ├── __main__.py                 # python -m dspace_sentinel → cli.main()
│   ├── cli.py                      # argparse: serve, doctor, report, discover, telegram-test, email-test, set-password, hash-password, install-service, ...
│   ├── auth.py                     # the dashboard login: scrypt hashing, password rules, sessions, brute-force limits, CLI token (stdlib only)
│   ├── paths.py                    # REPO_DIR, DATA_DIR, CONFIG_PATH, REPORTS_DIR (env‑overridable)
│   ├── config.py                   # Config (pydantic), defaults, YAML load/merge/save, env overrides, DEFAULT_CONFIG
│   ├── dspace_version.py           # parse dspaceVersion; SEO DOWN window, SSR Host rule, Solr major, optional cores, EOL
│   ├── models.py                   # Status, CheckResult, SystemSample, ComponentHealth, Alert, Incident, ...
│   ├── store.py                    # SQLite (WAL) schema + all queries; the only module that touches the DB
│   ├── state.py                    # in‑memory current state (latest sample, components, mood, last cycle)
│   ├── scheduler.py                # asyncio loop: runs collectors on intervals, feeds state/store/alerts, SSE bus
│   ├── bus.py                      # tiny pub/sub for SSE
│   ├── health.py                   # chain roll‑up, dependency graph, mood computation
│   ├── discovery.py                # local.cfg + dspace.cfg + modules/assetstore.cfg, ports, units, version probe
│   ├── util/
│   │   ├── systemd.py              # is_active / unit_exists / list_units via systemctl (timeout 3 s, `--` before the unit)
│   │   ├── net.py                  # tcp_connect(host, port, timeout), listening_sockets(with_process, udp), ip_of (IPv4-mapped → IPv4)
│   │   ├── support_dates.py        # end-of-support dates of DSpace, Solr, PostgreSQL, Ubuntu, Debian (sources inside)
│   │   ├── httpc.py                # shared httpx.AsyncClient factory with timeouts, no redirects surprises
│   │   ├── logs.py                 # tail_lines(path, max_bytes), count errors since ts
│   │   ├── tz.py                   # server.timezone: zone_of(cfg), the zone label ("UTC", "+0530"), /api/meta's timezone
│   │   └── fmt.py                  # bytes/duration/percent/clock formatters (shared by CLI + reports), times with their zone
│   ├── collectors/
│   │   ├── __init__.py             # REGISTRY: module_id → Collector class; enabled_collectors(cfg)
│   │   ├── base.py                 # Collector ABC, CollectorContext, CollectorResult, run_blocking / ProbeStalled
│   │   ├── system.py               # psutil: cpu, mem, swap, disk(s), load, net, uptime, os, kernel
│   │   ├── ports.py                # required ports open / local‑only expectations
│   │   ├── security.py             # read-only security status: ports, firewall, SSH, web, updates, software, files, Sentinel
│   │   ├── services.py             # generic systemd rung helper used by other collectors
│   │   ├── postgres.py             # chain: service → tcp → connect → SELECT 1 ; metrics
│   │   ├── solr.py                 # chain: service → tcp → admin/cores → core pings → search select
│   │   ├── backend.py              # chain: service → tcp → /server/api → actuator → search → deps
│   │   ├── frontend.py             # chain: service → tcp → homepage → public url → dep backend
│   │   ├── nginx.py                # chain: service → http → https
│   │   ├── ssl.py                  # certificate expiry
│   │   ├── logs.py                 # error/warn counts in dspace/tomcat/solr/nginx logs
│   │   ├── repository.py           # items, bitstreams, communities, collections, db size, assetstore bytes
│   │   ├── assetstore.py           # assetstore path, mount usage, free GB
│   │   └── demo.py                 # SENTINEL_DEMO=1: synthetic realistic stack + scripted incident
│   ├── alerts/
│   │   ├── engine.py               # rules, thresholds, hysteresis, state machine, ack, cooldown
│   │   ├── rules.py                # RULES table + evaluate(sample, components, cfg) → list[Observation]
│   │   ├── incidents.py            # correlation window, hypothesis patterns, affected services
│   │   ├── messages.py             # Telegram/plain text formatting of alerts, incidents, resolutions
│   │   ├── email_messages.py       # email subjects, text + HTML bodies built from the Telegram text
│   │   └── notifiers/
│   │       ├── __init__.py         # Notifier ABC, dispatch(cfg, event), notify(), Outbox (background delivery)
│   │       ├── telegram.py         # one bot per destination: sendMessage / sendDocument / getMe via httpx
│   │       ├── email.py            # smtplib in a daemon thread: one message per event, reports attached
│   │       └── webhook.py          # POST JSON
│   ├── reports/
│   │   ├── analytics.py            # compute_report_data(store, cfg, t_from, t_to) → dict
│   │   ├── svg.py                  # server‑side inline SVG line chart / strip / meter for reports
│   │   ├── generator.py            # generate(...) → Report; HTML/Markdown/JSON; storage in data/reports
│   │   ├── schedule.py             # weekly report job (+ Telegram sendDocument, email attachment)
│   │   └── templates/
│   │       ├── report.html.j2
│   │       └── report.md.j2
│   ├── api/
│   │   ├── app.py                  # create_app(cfg) → FastAPI (lifespan starts scheduler, writes the CLI token),
│   │   │                           #   static mount, middleware: Host allowlist, the login / token, cross-site write refusal
│   │   ├── routes_auth.py          # /api/session /api/login /api/logout /api/account/password; AuthService
│   │   ├── routes_status.py        # /api/status /api/meta /api/system /api/components ...
│   │   ├── routes_history.py       # /api/history ... /api/availability
│   │   ├── routes_alerts.py        # /api/alerts /api/incidents
│   │   ├── routes_reports.py       # /api/reports ...
│   │   ├── routes_config.py        # /api/config /api/modules /api/actions/*
│   │   └── stream.py               # /api/stream (SSE)
│   └── static/                     # the web UI (served at /)
│       ├── index.html
│       ├── css/tokens.css          # THE token file (see §9.2)
│       ├── css/app.css
│       ├── js/app.js               # boot, router, tab shell, SSE, theme
│       ├── js/api.js               # fetch wrappers (relative URLs)
│       ├── js/store.js             # client state + subscribers
│       ├── js/ui.js                # tile, section, chip, toggle, button, panel, confirm, snackbar, empty, meter, fmt
│       ├── js/charts.js            # sparkline, lineChart (crosshair+tooltip+table twin), availabilityStrip
│       ├── js/screens/home.js  stack.js  history.js  more.js  onboarding.js  signin.js
│       ├── js/screens/detail/server.js  component.js  incident.js  report.js  security.js
│       ├── js/screens/more/alerts.js  telegram.js  email.js  destinations.js  form.js  connection.js
│       │                   appearance.js  reports.js  about.js  modules.js  account.js
│       ├── js/panels/report.js
│       └── favicon.svg
├── deploy/
│   ├── systemd/dspace-sentinel.service
│   ├── nginx/sentinel.conf.example
│   └── ansible/
│       ├── README.md  playbook.yml  inventory.example.ini  requirements.yml
│       └── roles/dspace_sentinel/{defaults,tasks,templates,handlers,meta}/...
├── docs/
│   ├── SPEC.md (this)  ARCHITECTURE.md  CONFIG.md  ALERTS.md  NOTIFICATIONS.md  COMPATIBILITY.md
│   ├── REPORTS.md  REVERSE-PROXY.md  ANSIBLE.md  API.md  FAQ.md  DESIGN.md  screenshots/
│   └── TELEGRAM.md                 # a pointer to NOTIFICATIONS.md, so old links keep working
├── tests/  (pytest), tests/e2e/ (playwright smoke in demo mode), tests/fixtures/ (real DSpace payloads)
└── .github/workflows/ci.yml
```

Runtime (git‑ignored): `data/sentinel.db`, `data/reports/*.html|md|json`, `data/sentinel.log`,
`data/api-token` (the local CLI token, §9), `config/sentinel.yaml`, the optional
`config/sentinel.env`, `.venv/`; the demo keeps its own `data/demo/`.

---

## 2. Tech stack

Runtime deps (`requirements.txt`, each capped below its next major version): `fastapi>=0.115,<1`,
`uvicorn[standard]>=0.30,<1`, `psutil>=5.9,<8`, `httpx>=0.27,<1`, `psycopg[binary]>=3.1,<4`,
`jinja2>=3.1,<4`, `pyyaml>=6.0,<7`, `pydantic>=2.7,<3`.
Dev deps: `pytest`, `pytest-asyncio`, `ruff` (pinned to 0.16.x: CI checks the formatter's output),
`playwright`.
No Prometheus, no Grafana, no Node on the server, no ORM. Email uses the standard library
(`smtplib`, `email.message`), so it adds no dependency.

---

## 3. Configuration (`config/sentinel.yaml`)

YAML is the single source of truth. The UI edits it through `PATCH /api/config`, which
deep‑merges, validates, writes atomically (temp file + rename, mode 0600) and hot‑reloads. A
save writes the file's own layer (the YAML's values plus every patch), never values that came
from the environment or from `serve --host/--port`, and regenerates the file without comments.
`server.*` is never changed through the API (§9), except `server.timezone`
(`config.API_SERVER_KEYS`; `config.server_access()` is everything else, `server.login`
included), which decides nothing about who reaches the API. The dashboard login is changed only
by `set-password` (§10) and by the signed‑in admin's `POST /api/account/password` (§9), both
through `config.apply_login(cfg, username, password_hash)` (the running settings and the file
layer, so the next save writes it) and `save_config`.
Environment overrides (highest precedence): `SENTINEL_CONFIG`, `SENTINEL_DATA_DIR`,
`SENTINEL_HOST`, `SENTINEL_PORT`, `SENTINEL_DEMO=1`, `SENTINEL_PG_PASSWORD`, `SENTINEL_AUTH_TOKEN`,
`SENTINEL_SMTP_PASSWORD` (`notifiers.email.password`; while it is set the password is read‑only:
`masked()` reports `password_from_env: true`, the API refuses to change it or to re‑point its
server and login, the UI shows it locked); `SENTINEL_ALLOW_OPEN=1` has the effect of
`server.allow_unauthenticated: true`. `SENTINEL_TELEGRAM_TOKEN` + `SENTINEL_TELEGRAM_CHAT_ID`
(`apply_telegram_env`) add the destination `{id: "env", label: "From environment", min_severity:
warning}` to the running configuration only: never saved, `from_env: true` in the API, counted
toward the five, skipped when an entry already has the same token and chat, and left out when
the file already has five ("the Telegram bot from SENTINEL_TELEGRAM_TOKEN was left out: there are
already 5 bots" in the log, and one `config` event per start). The token alone becomes the token
of every entry saved without one; in an old single‑bot file each variable replaces that bot's
value. A value that cannot be a token or a chat id stops the start ("SENTINEL_TELEGRAM_TOKEN is not
a Telegram bot token (it looks like 123456789:ABC…, from @BotFather)", "SENTINEL_TELEGRAM_CHAT_ID
is not a Telegram chat id: '<v>'").

The optional `config/sentinel.env` (next to `SENTINEL_CONFIG`) holds such variables as
`KEY=VALUE` lines (blank lines and `#`/`;` comments skipped, single‑ or double‑quoted values,
no `export`). Every CLI command exports it before loading the config (variables already set
win; demo mode ignores it) and both systemd units read it with `EnvironmentFile=-`. A file
readable by other users is warned about; it is git‑ignored.

```yaml
server:
  host: 127.0.0.1          # non-loopback needs the login, auth_token or allow_unauthenticated (else serve exits 4)
  port: 9765
  auth_token: ""           # a token for scripts: Bearer, or HTTP Basic with any username;
                           # without a login the dashboard asks for it (Basic pop-up)
  login:                   # the dashboard login (one admin account); on while password_hash is set
    username: ""           # 1-64 of A-Z a-z 0-9 . - _ @
    password_hash: ""      # secret; scrypt$16384$8$1$<salt>$<hash> (set-password writes it)
  hostname: ""             # label shown in UI/alerts; default = socket.gethostname()
  allowed_hosts: []        # extra Host names the API answers to ("*" = any); see §9
  allow_unauthenticated: false   # serve a non-loopback host without a login or auth_token
  timezone: ""             # IANA zone of the times Sentinel shows people (messages, emails,
                           # reports, quiet hours, digests, the weekly hour, doctor); "" = the
                           # server's zone; the one server key PATCH may change (util/tz.py)
setup:
  completed: false         # onboarding gate
  persona: ""              # full | backend | frontend | server
collector:
  system_interval: 15      # seconds
  service_interval: 30
  http_interval: 30
  db_interval: 60
  logs_interval: 60
  repository_interval: 900
  security_interval: 900   # Security module (60 … 86400 s), its own interval group
  retention_days: 30       # raw samples/checks; hourly rollups kept 365 days
dspace:
  install_dir: /dspace
  auto_discover: true      # read <install_dir>/config/local.cfg (+ dspace.cfg) on discover
  server_url: http://localhost:8080/server
  ui_url: http://localhost:4000
  public_url: ""           # e.g. https://repo.example.org (checked through nginx when set)
  assetstore_dir: /dspace/assetstore
  log_dir: /dspace/log
  backend_service: tomcat9     # systemd unit; "" to skip the systemd rung
  frontend_service: dspace-ui
  verify_tls: true
postgres:
  host: localhost
  port: 5432
  database: dspace
  user: dspace
  password: ""
  sslmode: prefer
  service: postgresql
solr:
  url: http://localhost:8983/solr
  cores: [search, statistics, oai, authority]
  service: solr
nginx:
  service: nginx
  http_url: http://localhost
  https_url: ""            # e.g. https://repo.example.org
ssl:
  host: ""                 # defaults to public_url host
  port: 443
ports:
  - {port: 22,   label: SSH,        expect: open}
  - {port: 80,   label: HTTP,       expect: open}
  - {port: 443,  label: HTTPS,      expect: open}
  - {port: 4000, label: Angular,    expect: open}
  - {port: 8080, label: Backend,    expect: open}
  - {port: 8983, label: Solr,       expect: local_only}
  - {port: 5432, label: PostgreSQL, expect: local_only}
logs:
  files:                   # any missing file is skipped silently (status unknown, not warn)
    - {id: dspace, label: DSpace,  path: /dspace/log/dspace.log, daily_suffix: true}   # dspace.log.YYYY-MM-DD
    - {id: tomcat, label: Tomcat,  path: /var/log/tomcat9/catalina.out}
    - {id: solr,   label: Solr,    path: /var/solr/logs/solr.log}
    - {id: nginx,  label: nginx,   path: /var/log/nginx/error.log}
    # every entry also takes timezone: "" (default: local time, or UTC when the newest line only
    # matches the file's mtime as UTC), "local", "UTC" or an IANA name such as "Asia/Colombo"
  window_minutes: 5
alerts:
  confirm_samples: 2       # consecutive breaches before an alert opens (anti‑flap)
  clear_samples: 3         # consecutive OK samples before it resolves
  reminder_minutes: 0      # 0 = notify only on open / escalate / resolve
  cpu:                      {warning: 80,   critical: 95}
  memory:                   {warning: 80,   critical: 92}
  swap:                     {warning: 50,   critical: 80}
  disk:                     {warning: 80,   critical: 90}
  load_per_core:            {warning: 1.5,  critical: 3.0}
  rest_latency_ms:          {warning: 1000, critical: 3000}
  search_latency_ms:        {warning: 1500, critical: 4000}
  home_latency_ms:          {warning: 2000, critical: 5000}
  postgres_connections_pct: {warning: 80,   critical: 95}
  ssl_expiry_days:          {warning: 30,   critical: 7}
  log_errors_per_window:    {warning: 20,   critical: 100}
  assetstore_free_gb:       {warning: 50,   critical: 10}
notifiers:
  telegram:
    enabled: false
    chats: []              # 0-5 × {id, label, bot_token (secret), chat_id, min_severity: warning|critical}
    quiet_hours: ""        # "23:00-07:00" → only critical during this window (every bot), in server.timezone
    daily_digest: ""       # "08:00" (server.timezone) → one summary message per day; "" = off
  email:
    enabled: false
    smtp_host: smtp.gmail.com
    smtp_port: 587
    security: starttls     # starttls | ssl | none
    username: ""           # the login (Gmail: its address); an address unless from_address is set
    password: ""           # secret: a Gmail app password; only sent to the server/login it was saved for
    from_address: ""       # "" = username
    from_name: DSpace Sentinel
    recipients: []         # 0-5 × {address, label, min_severity: warning|critical}
    quiet_hours: ""        # same syntax as Telegram's, email only
    daily_digest: ""       # "08:00" → one summary email per day; "" = off
  webhook:
    enabled: false
    url: ""                # a secret (masked); TLS always verified, redirects never followed
reports:
  weekly: {enabled: false, weekday: 1, hour: 8, send_telegram: false, send_email: false}   # weekday 1 = Monday; day and hour in server.timezone
  default_sections: [summary, server, services, latency, alerts, repository, security, recommendations]
modules:                   # everything off by default; server module is always on
  ports: false
  security: false          # read-only: open ports, firewall, SSH, updates and software support
  postgres: false
  solr: false
  backend: false
  frontend: false
  nginx: false
  ssl: false
  logs: false
  repository: false
  assetstore: false
  incidents: false
appearance:
  theme: system            # system | light | dark
```

Python API (`dspace_sentinel/config.py`):

```python
class Config(BaseModel): ...            # mirrors the YAML exactly; nested pydantic models; extra="ignore"
                                         # private _file = the file layer (YAML + patches), _demo_base
DEFAULT_CONFIG: dict                     # the defaults above
def load_config(path: Path | None = None) -> Config      # defaults ← yaml ← env (records the file layer);
                                         # unreadable times left out, notices kept (below)
def startup_notices(cfg: Config) -> list[str]            # sentences serve writes as `config` events
def save_config(cfg: Config, path: Path | None = None) -> None   # atomic, 0600; writes persisted_dict(cfg):
                                         # the file layer (never env/CLI values); in demo mode only the
                                         # difference from the demo base, without server.* or secrets;
                                         # quiet_hours and daily_digest double-quoted
def merge_patch(cfg: Config, patch: dict) -> Config      # deep merge + validate; used by PATCH /api/config;
                                         # masked secrets keep the stored value (a bot token: the entry
                                         # with the same id, else "enter the bot token for <name> again");
                                         # lists (chats, recipients, ports) are replaced; raises ValueError
                                         # when postgres.host/port/database/user, or notifiers.email
                                         # smtp_host/smtp_port/security/username (to a non-empty username),
                                         # change without a typed password while one is stored, and, while
                                         # SENTINEL_SMTP_PASSWORD is set, for a new email password or a
                                         # re-pointed email login; a patch of notifiers.email must leave
                                         # no EmailConfig.problem() (an SENTINEL_SMTP_PASSWORD password
                                         # left out of it), a newly typed email password must be ASCII
                                         # (PASSWORD_NOT_SENDABLE); quiet hours and digest times strict
def masked(cfg: Config) -> dict          # secrets replaced by "••••••" (SECRET_PATHS: server.auth_token,
                                         # server.login.password_hash, postgres.password,
                                         # notifiers.email.password, notifiers.webhook.url;
                                         # SECRET_ITEM_PATHS: every notifiers.telegram.chats[*].bot_token);
                                         # adds chats[*].from_env and notifiers.email.password_from_env
def apply_login(cfg, username, password_hash) -> None   # the login into the running settings and the
                                         # file layer, in place; ("", "") removes it
def apply_telegram_env(tree, token, chat_id, *, leave_out_when_full=False) -> bool
                                         # SENTINEL_TELEGRAM_* → the "env" destination (in place);
                                         # True when it was left out because five are already listed
def dspace_modules_enabled(cfg) -> bool  # any of postgres/solr/backend/frontend/repository/assetstore
```

**Notification settings** are validated on `PATCH` (400 with the message, prefixed "invalid
configuration: <dotted.path>: ") and at load, where a value that does not validate stops the
start (exit 4) except in the cases listed at the end of this paragraph: at most 5 `chats`
("Telegram can send to at most 5 bots", plus " (the one from SENTINEL_TELEGRAM_TOKEN /
SENTINEL_TELEGRAM_CHAT_ID counts too)" when the environment's bot is among them) and 5
`recipients` ("email can go to at most 5 recipients");
`chat_id` is `-?\d{1,20}` or `@` + 5–32 `[A-Za-z0-9_]` ("not a Telegram chat id: '<v>'");
`bot_token` matches `^\d{5,12}:[A-Za-z0-9_-]{30,50}$` or is the mask ("not a Telegram bot token (it
looks like 123456789:ABC…, from @BotFather)", never echoing the value); the same token and chat
twice is refused ("Telegram chat <chat_id> is listed twice with the same bot"). Every entry gets
an `id`: 8 hex characters derived from its position and chat id when missing (stable until the
file is saved), a hand‑written id of up to 32 `[A-Za-z0-9_-]` kept, duplicates refused ("two
Telegram destinations have the same id '<id>'"), `env` reserved. An entry without both a token and
a chat id is kept but not sent to. Labels are ≤ 40 characters on one line; `min_severity` is
`warning` | `critical`. A file or PATCH with the old single `bot_token` / `chat_id` and no `chats`
becomes one entry (an old‑style PATCH edits the first saved entry, which then becomes the
whole list); saving writes `chats` only.
Email: a recipient is one plain address ("not an email address: '<v>'"), listed once whatever
its case ("<address> is listed twice"); `smtp_host` is a host name or IP ("not an SMTP server name:
'<v>' (a host name or IP address)"); `smtp_port` 1–65535; `security` `starttls` | `ssl` | `none`;
`from_address` is "" or a plain address; `from_name` ≤ 60 characters on one line; an app
password pasted in Google's groups is stored without the spaces. `quiet_hours` (both channels) is
"" or two `H:MM`/`HH:MM` times in ASCII digits around a hyphen, any dash or a minus sign, spaces
allowed, at most `TIME_VALUE_MAX` = 40 characters (a longer value is refused before it is
matched), saved as `HH:MM-HH:MM` ("quiet hours must look like 23:00-07:00"), and its two times
differ ("quiet hours must start and end at different times": an empty window would never be
quiet); `daily_digest` is "" or `H:MM`/`HH:MM` under the same rules, saved as `HH:MM` ("the daily
digest time must look like 08:00"). A login SMTP can send and a sender that is an address go
together (`EmailConfig.problem()`, only with a non‑empty `username`, in this order): the password
and the login are ASCII (`smtplib` sends them as ASCII: `PASSWORD_NOT_SENDABLE` "the email
password has a character that cannot be sent: use letters, digits and symbols from the English
keyboard", `USERNAME_NOT_SENDABLE` "the SMTP username has a character that cannot be sent: …"),
and with `from_address` empty the `username` is a plain address ("the SMTP username is not an
email address: set from_address (the address alerts come from)"). `merge_patch` refuses a patch
that touches `notifiers.email` and leaves such a problem (400 "invalid configuration:
notifiers.email: …"; a password from `SENTINEL_SMTP_PASSWORD`, which a patch can neither set nor
mend, is left out of that check), and a newly typed password (anything but the mask) with a
character outside ASCII whether or not a username is set; a patch without `notifiers.email` is not
held up by either. The mask itself (`••••••`, spaces around it allowed) as the password in the
file loads as "": no password. Email is ready when it has `smtp_host`, an address‑shaped sender
(`from_address`, else `username`), the password when a username is set, at least one recipient
and no `problem()`; `security: none` never sends a password to a host other than
localhost/loopback. The stored email password is only sent to the server it was
saved for: a PATCH that changes `smtp_host` (ignoring case), `smtp_port`, `security` or
`username` (to a non‑empty one) while a password is stored needs a typed password (left out or
masked → "enter the email password again: the SMTP server, port, security or username changed and
the saved password is only sent to the server it was saved for"); with `SENTINEL_SMTP_PASSWORD`
set, a new password answers "the email password comes from SENTINEL_SMTP_PASSWORD in
config/sentinel.env: change it there" and a re‑point "the email password comes from
SENTINEL_SMTP_PASSWORD in config/sentinel.env and is only sent to the server it was set for:
change the SMTP server, port, security or username in config/sentinel.yaml".
Five things never stop the start; each is logged as a warning and kept as a start‑up notice
(`startup_notices(cfg)`, capitalised; `serve` writes one `config` event per notice at every
start). (1) `load_config` and `load_demo_state` take a `quiet_hours` or `daily_digest` they cannot
read (an equal‑times window included) as "": "the quiet hours '11pm-7am' in
notifiers.telegram.quiet_hours were ignored: quiet hours must look like 23:00-07:00" (the value
quoted, at most 40 characters of it). A `daily_digest` that YAML 1.1 read as a base‑60 number is a
time again when it is a whole number from 60 to 1439 (`18:00` → 1080 → "18:00"); any other number
is left out with the notice plus " (YAML reads an unquoted time as a number: write it in
quotes)". (2) Email settings with the `problem()` above load as they are, whatever `enabled`
says, also with a password from `SENTINEL_SMTP_PASSWORD`, and email is not ready: "email is not
ready: the SMTP username is not an email address: set from_address (the address alerts come
from)", "email is not ready: the email password has a character that cannot be sent: …". (3) The
environment's bot is left out when it would be a sixth (above). (4) `load_config` takes a
`server.timezone` that names no loadable zone as "" (`config._readable_zone`): "the time zone in
server.timezone was ignored, so times use the server's zone: unknown time zone 'Mars/Base': use an
IANA name such as Asia/Colombo, or leave it empty for the server's zone". (5) A `server.login`
that cannot be used (`auth.login_problem`: a `password_hash` that does not parse, or a username
outside the rules) loads as it is and **stays on**, outside the demo: "The login in server.login
could not be read: run ./sentinel.sh set-password" (`auth.LOGIN_UNREADABLE`); every sign‑in
answers 503 with that sentence, and nothing falls back to "no login". `save_config` writes
`quiet_hours` and `daily_digest` in double quotes, so a hand edit of the line keeps them strings.

**The dashboard login** (`server.login`, `config.LoginConfig`, `dspace_sentinel/auth.py`, which
needs only the standard library). `auth.login_active(cfg)`: `password_hash` set and not the demo.
`hash_password(password)` → scrypt (16 random salt bytes, n=2**14, r=8, p=1, dklen=32) stored as
`scrypt$16384$8$1$<salt>$<hash>` (base64url without padding), or `pbkdf2_sha256$600000$<salt>$<hash>`
where `hashlib.scrypt` is missing; `verify_password(password, encoded)` reads both and compares with
`hmac.compare_digest`; `parse_hash` bounds the cost parameters (a hand‑edited hash can neither run
for minutes nor exhaust memory). Username: 1–64 `[A-Za-z0-9._@-]` (`USERNAME_RULE` "The username must
be 1 to 64 characters: letters, digits and . - _ @"); a sign‑in compares it ignoring case and the
spaces around it. Password: 12–256 characters and not the username, case‑insensitive (`TOO_SHORT`
"Use at least 12 characters.", `TOO_LONG` "Use at most 256 characters.", `SAME_AS_USERNAME` "The
password must differ from the username."); no other rules. No password is ever logged, stored in
clear, echoed or put in an exception text. `password_hash` is a secret (`SECRET_PATHS`): masked in
`GET /api/config`, never in the export, events, logs, reports or the demo's settings file. The
login on a non‑loopback `server.host` satisfies `check_bind` (§9). A running server re‑reads
`server.login` when the config file changes (`AuthService.refresh`: its mtime, size and inode, at
most every 2 s, at the next request), applies it without a restart and ends every session (event
"Password changed", or "Login removed"); a login removed by hand while `check_bind` would then
refuse the bind is kept until a restart (a warning in the log), so the dashboard never opens on
the network.

`server.timezone` (`util/tz.py`) is "" (the server's zone) or a name `zoneinfo` can load: an IANA
name such as `Asia/Colombo`, and `UTC` / `Etc/UTC` even on a system without zone data;
`ServerConfig` validates it with `tz.check_zone` (spaces trimmed; at most `MAX_ZONE_LENGTH` = 64
characters; nothing that starts with `/` or `.` or holds `..` or `\`) and refuses anything else
with `tz.ZONE_ERROR` "unknown time zone
'Mars/Base': use an IANA name such as Asia/Colombo, or leave it empty for the server's zone", plus
`NO_ZONE_DATA` " (this system has no time zone data: install the tzdata package)" when the system
knows no zone, or "the time zone must be text, such as Asia/Colombo". `PATCH /api/config` stays
strict (400). `tz.zone_of(cfg)` gives the tzinfo (`None` = the server's zone; a name that stopped
loading after the start falls back to the server's zone with one log warning). Every time Sentinel
shows people goes through it and names its zone with `tz.label`: the abbreviation when it is a
word (`UTC`, `CEST`), else the UTC offset (`+0530` for Asia/Colombo, whose abbreviation is a
number): `fmt.clock(ts, tz=…, zone=True)` "21:29 +0530", `fmt.date_time(…, zone=True)` "2026-10-07
15:59 UTC", `fmt.day` "07 Oct 2026". It covers alert and incident messages and emails, report
times, chart ticks, file names, captions and the report email, the weekly job's day and hour,
quiet hours, the daily digests and the `doctor` box. Log files keep their own zones
(`logs.files[].timezone`), and the web UI shows the browser's time.

Every systemd unit name (`dspace.backend_service`, `dspace.frontend_service`,
`postgres.service`, `solr.service`, `nginx.service`) is validated: `""` (skip the rung) or
`^[A-Za-z0-9@_.:-]{1,128}$` not starting with `-` ("not a systemd unit name: …").

Persona presets (`config.PERSONAS`) set modules and the expected `ports`: `full` → all modules
on, ports 22/80/443/4000/8080 open + 8983/5432 local only; `backend` → all but frontend, the same
ports without 4000; `frontend` → nginx, ssl, frontend, ports, security, logs(nginx), backend
(remote URL), ports 22/80/443/4000; `server` → ports, security, nginx, ssl, logs, port 22. Every
preset switches `security` on: it reads this server, whatever runs on it (discovery suggests
`modules.security: true` too). The wizard sends its own module toggles in
`config_patch.modules`, which switch nginx and ssl on only with discovery's evidence (§12.5
Onboarding).

---

## 4. Data models (`models.py`)

```python
Status = Literal["ok", "warn", "crit", "unknown", "off"]
STATUS_RANK = {"ok": 0, "unknown": 1, "warn": 2, "crit": 3}      # worst() uses this; "off" never counts

@dataclass
class CheckResult:
    id: str                 # e.g. "postgres.query"
    label: str              # human label of the rung, e.g. "SELECT 1"
    status: Status
    ts: float               # epoch seconds
    latency_ms: float | None = None
    value: float | None = None
    message: str = ""       # short human reason ("connection refused (localhost:5432)")
    detail: dict = field(default_factory=dict)

@dataclass
class SystemSample:
    ts: float
    cpu_pct: float; cpu_per_core: list[float]
    mem_total: int; mem_used: int; mem_available: int; mem_pct: float; mem_cached: int
    swap_total: int; swap_used: int; swap_pct: float
    disk_total: int; disk_used: int; disk_free: int; disk_pct: float; disk_path: str
    inode_pct: float | None
    load1: float; load5: float; load15: float; cores: int
    net_rx_bps: float; net_tx_bps: float          # computed from counters delta
    uptime_s: float
    os_name: str; os_version: str; kernel: str; hostname: str
    disks: list[dict]       # extra mounts: [{path, total, used, free, pct}]

@dataclass
class ComponentHealth:
    id: str                 # server | postgres | solr | backend | frontend | nginx | ssl | ports | security | logs | repository | assetstore
    label: str; emoji: str
    status: Status
    reason: str | None      # message of the first non‑ok rung, or None
    chain: list[CheckResult]
    metrics: dict           # component specific numbers (see §6)
    depends_on: list[str]
    latency_ms: float | None
    since: float | None     # when the current status began
    checked_at: float | None

@dataclass class Alert:  id, rule, component, severity("warning"|"critical"), status("open"|"resolved"),
                         title, message, value, threshold, opened_ts, resolved_ts, acked_ts, incident_id, notified_ts
@dataclass class Incident: id, title, hypothesis, status("open"|"resolved"), started_ts, ended_ts,
                         affected: list[str], sequence: list[dict(ts, text, severity)], alert_ids: list[int]
@dataclass class Report: id, created_ts, title, range_from, range_to, fmt("html"|"md"|"json"), path, size, deleted_ts
```

---

## 5. Storage (`store.py`, SQLite WAL, `data/sentinel.db`)

```sql
samples(ts INTEGER PRIMARY KEY, cpu REAL, mem_pct REAL, mem_used INTEGER, mem_total INTEGER, swap_pct REAL,
        disk_pct REAL, disk_used INTEGER, disk_total INTEGER, load1 REAL, load5 REAL, load15 REAL,
        net_rx REAL, net_tx REAL, extra TEXT)                      -- extra = JSON (per‑core, other mounts)
samples_hourly(ts INTEGER PRIMARY KEY, cpu_avg, cpu_max, mem_avg, mem_max, disk_avg, load_avg, net_rx_avg, net_tx_avg)
checks(ts INTEGER, check_id TEXT, status TEXT, latency_ms REAL, value REAL, message TEXT, PRIMARY KEY(ts, check_id))
component_state(component_id TEXT PRIMARY KEY, status TEXT, since_ts INTEGER, checked_ts INTEGER, reason TEXT,
        latency_ms REAL, metrics TEXT, chain TEXT)                 -- JSON blobs; survives restarts
component_history(ts INTEGER, component_id TEXT, status TEXT, latency_ms REAL, PRIMARY KEY(ts, component_id))
alerts(id INTEGER PRIMARY KEY AUTOINCREMENT, rule TEXT, component TEXT, severity TEXT, status TEXT, title TEXT,
        message TEXT, value REAL, threshold REAL, opened_ts INTEGER, resolved_ts INTEGER, acked_ts INTEGER,
        incident_id INTEGER, notified_ts INTEGER)
incidents(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, hypothesis TEXT, status TEXT, started_ts INTEGER,
        ended_ts INTEGER, affected TEXT, sequence TEXT)
reports(id INTEGER PRIMARY KEY AUTOINCREMENT, created_ts INTEGER, title TEXT, range_from INTEGER, range_to INTEGER,
        fmt TEXT, path TEXT, size INTEGER, deleted_ts INTEGER)
repository_stats(ts INTEGER PRIMARY KEY, items INTEGER, bitstreams INTEGER, communities INTEGER, collections INTEGER,
        db_size INTEGER, assetstore_bytes INTEGER, extra TEXT)
events(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, kind TEXT, component TEXT, message TEXT, data TEXT)
kv(key TEXT PRIMARY KEY, value TEXT)                               -- e.g. schema_version, last_digest_ts,
                                                                   --   last_email_digest_ts
sessions(token_hash TEXT PRIMARY KEY, username TEXT, created_ts REAL, last_seen_ts REAL, expires_ts REAL,
        remember INTEGER, ip TEXT, user_agent TEXT, fingerprint TEXT)
                                                                   -- dashboard login sessions: only the
                                                                   --   SHA-256 of each token (§9)
```

`Store` is synchronous `sqlite3` (check_same_thread=False, one connection, a lock) called from
the async loop via `asyncio.to_thread` for bulk queries. The database file is created with mode
0600 before SQLite opens it (the `-wal`/`-shm` files inherit it); existing files are tightened
on open. Methods (all typed, all tested):
`insert_sample`, `insert_checks`, `upsert_component_state`, `insert_component_history`,
`series(metric, t_from, t_to, bucket_s)` (bucketed avg/max via `GROUP BY ts/bucket`),
`availability(component_id, t_from, t_to, buckets, interval_s)`, `latency_series(check_id, ...)`,
`checks_between(t_from, t_to, check_id)`, alert CRUD, incident CRUD, report CRUD (soft delete +
purge, `prune_reports(max_files, max_bytes)`), `insert_repository_stats`,
`latest_repository_stats`, `repository_delta(t_from, t_to)`, `rollup_hourly(before_ts)`,
`purge(retention_days)`, `export(t_from, t_to, stream=True, meta=…)`, `add_event`,
`events(limit)`, `db_size_bytes()`, and the sessions of the dashboard login: `add_session`,
`get_session`, `sessions()`, `touch_session`, `set_session_fingerprint`, `delete_session`,
`delete_sessions(keep=)`, `purge_sessions(now)`.

Bucket rule for `series`: pick bucket so that ≤ 360 points are returned (1h→15 s, 6h→60 s,
24h→300 s, 7d→1800 s, 14d→3600 s, 30d→7200 s; over 30 days a whole number of hours large
enough to stay under 360 points). Source = `samples` when the range is ≤ 7 d, else
`samples_hourly` merged with per‑hour aggregates of the raw samples newer than the last hourly
roll‑up (so a long range reaches the newest sample) — except `swap`, which has no hourly column
and always reads raw samples.

`purge` deletes raw samples by whole hours (the cutoff is rounded down to the hour), so an hour
that was rolled up is never re‑aggregated from what is left of it, and also purges expired
sessions (past `expires_ts`, or idle longer than 12 h, 30 days for a `remember` session);
`clear_history` keeps `sessions`, and the export never includes them. `fingerprint` is a SHA‑256
of the username and password hash a session signed in with, so a changed or removed login ends
it at its next request. `availability` charges each
row the time until the next one, capped at twice the median spacing (the last row: the typical
spacing, bounded by `t_to`), so a gap in the history is not an outage; `downtime_s` sums that
over the `crit` rows and `covered_s` over all rows. `export(stream=True)` yields one JSON
document (`meta` first) table by table through a separate read‑only connection, without a row
cap. `checks` also holds history‑only rows (`postgres.connections`, see §6).

---

## 6. Collectors and health chains (`collectors/`, `health.py`)

```python
class CollectorContext:  cfg: Config; http: httpx.AsyncClient; store: Store; state: State; now: float
class Collector(ABC):
    id: str                         # module id
    interval_key: str               # which collector.*_interval applies
    label: str; emoji: str; depends_on: list[str] = []
    budget_s: float = 0.0           # worst case of one collect (its rung timeouts in a row + slack)
    async def collect(self, ctx) -> CollectorResult
# CollectorResult(checks: list[CheckResult], metrics: dict, sample: SystemSample|None,
#                 repository_stats: dict|None, history: list[CheckResult])
# history = checks rows kept for the reports but never part of the ladder
```

The scheduler wraps every `collect` in `asyncio.wait_for(timeout=max(interval − 1,
budget_s, 2))` — budgets: postgres 35 s, solr 25 s, backend 25 s, frontend 20 s, nginx 18 s,
security 36 s (its reads 15 s, the firewall units asked together, the GET 10 s) — and
converts a timeout or an exception into a single `CheckResult(id=f"{id}.error",
status="unknown", message=…, detail={"collector_error": True})` (e.g. "check timed out after
29 s"). Group runs share one cycle lock; nothing slow runs under it (§7). A result whose module
was switched off while it was being collected is dropped. Solr's version probe and its per‑core
pings run concurrently. `collectors.COLLECTOR_ORDER` runs `security` after the stack within a
cycle, so its software rung reads the versions the stack reported last; `security` has its own
interval group (`security_interval`, 900 s), and a *Check now* runs it too.

Blocking filesystem calls (`statvfs`, `stat`, log reads) go through
`collectors.base.run_blocking(key, fn, *args, timeout=)`: a daemon worker thread, at most one call
per key in flight; while the previous call with that key hangs (a dead NFS mount) the next one
raises `ProbeStalled` ("… still blocked after N s (a filesystem is not responding)") instead of
starting another thread.

**Rung ids, labels and semantics** (a rung after a failed rung is *not reached* → status
`unknown`, message "not reached"; a rung that answered correctly but is warn/crit only because
of its latency threshold — `backend.rest`, `backend.search`, `frontend.home`, `solr.search` —
carries `detail.slow: true` and does not stop the ladder; the headline then reads
"Critical: <Label> slow"):

| Component (emoji) | Rungs in order | Metrics |
|---|---|---|
| `server` 💻 | (no chain — thresholds on the sample) | cpu_pct, mem_pct, disk_pct, load1, per_core_load, uptime, os, kernel |
| `postgres` 🐘 | `postgres.service` "systemd postgresql" · `postgres.tcp` "TCP :5432" · `postgres.connect` "Connect as dspace" · `postgres.query` "SELECT 1"; history row `postgres.connections` (value = connections_pct, graded by `postgres_connections_pct`, message "n / max"; every `db_interval`, reports only) | connections, max_connections, connections_pct, db_size_bytes, version, active_queries, longest_query_s |
| `solr` ☀️ | `solr.service` · `solr.tcp` "TCP :8983" · `solr.admin` "Admin cores" ("6 cores"; with failures "5 cores, 1 failed to load", detail.failed; with failed reloads "6 cores, 1 failed to reload", detail.reload_failed; both "5 cores, 1 failed to load, 1 failed to reload"; detail.missing) · `solr.core.<name>` "Core <name>" (one rung per configured core, then one per optional DSpace core that STATUS lists behind the configured cores' prefix and `solr.cores` does not name: `qaevent`, `suggestion` (DSpace 8+), `audit` (10+), limited by the version the backend last read, detail.optional, capped at warn; ping, or for a core listed under STATUS `initFailures` and not under `status` (it did not load) crit "failed to load: <first line without Java class names, ≤ 200 chars>" with detail.hint 'start Solr with SOLR_OPTS="-Dsolr.config.lib.enabled=true" (Solr 9.8+)' for the `<lib/>` case, neither pinged nor searched; a core under both (its RELOAD failed and Solr kept the old core serving, `split_failures`) is pinged and searched as usual, a good ping reading warn "reload failed: <reason> (still serving; a restart would not load it)", detail.reload_failed; yearly `statistics-YYYY` shards never get a rung) · `solr.search` "Search query" (select q=*:* rows=0 on core `search`, or `<prefix>search`; crit "core <name> failed to load" without a request when it failed) | cores: [{name, docs, size_bytes, status, optional?}], version, search_docs, search_latency_ms, search_core, init_failures {core: reason} (did not load), reload_failures {core: reason} |
| `backend` ☕ | `backend.service` · `backend.tcp` "TCP :8080" · `backend.rest` "GET /server/api" (200 + JSON with dspaceVersion) · `backend.actuator` "Actuator health" (GET /server/actuator/health, anonymous, asked at most every `ACTUATOR_MIN_INTERVAL_S` = 300 s per collector instance and re-graded every cycle, detail.cached_age_s on a reused answer; only an answer graded ok or informational is reused, keyed on (url, the REST root's raw dspaceVersion), and it is forgotten whenever the ladder stops before the actuator (service, TCP, REST), so a warning, a restart or an upgrade is asked again next cycle; graded by `grade_health(body, version, rest_ok)`: UP ⇒ ok "UP"; UP_WITH_ISSUES ⇒ ok "up, with non-blocking issues"; visible components ⇒ warn naming the DOWN/OUT_OF_SERVICE ones, "status DOWN (db)", except a DOWN whose only down component is `seo` on a version in the SEO window ⇒ the known issue below plus ": the SEO check flags <keys>" (`seo_flags`: the seo component's detail keys whose value is not "OK": `sitemap`, `robots.txt`, `ssr`); an anonymous DOWN on a version in the SEO window (7.6.4–7.6.x, 8.2–8.4, 9.0–9.3, 10.0) while the REST rung answered (ok, or correct but slow) ⇒ *informational* unknown "DSpace <v> reports DOWN while healthy (a known SEO health check issue fixed in 8.5 / 9.4 / 10.1)", detail.known_issue "seo"; any other status ⇒ warn "status DOWN (details need a DSpace admin login)"; 401/403/404 ⇒ *informational* unknown "not exposed (HTTP 404)"; detail.health_status) · `backend.search` "Search API" (GET /server/api/discover/search/objects?size=1) · `backend.dep.postgres` "Database dependency" · `backend.dep.solr` "Solr dependency" | dspace_version, dspace_name, dspace_major, dspace_minor, dspace_patch (null for 9.4), dspace_snapshot, cris_version, rest_latency_ms, search_latency_ms, actuator_status, actuator_components ({} anonymously), server_url |
| `frontend` 🖥️ | `frontend.service` · `frontend.tcp` "TCP :4000" · `frontend.home` "Homepage" (GET ui_url, up to 2 MiB, with `Host: <host of dspace.public_url>` plus `X-Forwarded-Proto: https` for an https public URL when that host differs from ui_url's, the host IDNA‑encoded as a browser sends it (`xn--bcher-kva.example` for `https://bücher.example`; a name with no IDNA form sends no Host header), detail.host_header the Host sent; 200 and a `<ds-app` element or a `<meta name="Generator" content="DSpace …">`, else warn "no DSpace markup in the response"; detail.rendering `ssr` (ng-server-context="ssr", or ng-version) or `csr` (an empty `<ds-app></ds-app>`): the client-side shell stays ok with detail.note "client-side rendering only (server-side rendering is off or failing)", on 9.3+ when the host sent (the public Host header, else ui_url's host) differs from the host of the backend's REST `dspaceUI`, else of public_url, "client-side rendering only: DSpace <v> renders on the server only for the host in its ui.baseUrl (set dspace.public_url)"; the same host, or none to compare with, keeps the general note) · `frontend.public` "Public URL" (only when public_url set; detail.rendering) · `frontend.dep.backend` | home_latency_ms, public_latency_ms, title, rendering, generator, angular, host_header (the public URL's host as written, `bücher.example`), ui_url, public_url |
| `nginx` 🌐 | `nginx.service` · `nginx.http` "HTTP" (a refused request while systemd reports "unit <unit> not found" stays crit and carries detail.hint `NO_WEB_SERVER_HINT`, below) · `nginx.https` "HTTPS" (only when https_url set) | http_latency_ms, https_latency_ms, server_header |
| `ssl` 🔒 | `ssl.cert` "Certificate" | days_left, not_after, issuer, subject, sans |
| `ports` 🔌 | `ports.<port>` "<label> :<port>" (detail.expect, detail.addrs) — `open` expectation: closed ⇒ warn; `local_only`: listening on 0.0.0.0, :: or any other non‑loopback address (an IPv4‑mapped address such as `::ffff:127.0.0.1` is judged by the IPv4 address it maps, `util.net.ip_of`, so that one is loopback) ⇒ warn "exposed publicly (<the non‑loopback addresses>)" with detail.hint by service (`EXPOSED_HINTS`, below), closed ⇒ unknown | listening: [{port, addr, public, process}], other_public, source |
| `security` 🛡️ | no dependencies and no headline check (status = the worst counted rung, reason = the first counted rung that is not ok); every `security_interval`, its own group: `security.ports` "Open ports" · `security.firewall` "Firewall" · `security.ssh` "SSH" · `security.web` "Web" · `security.updates` "Updates" · `security.software` "Software" · `security.files` "DSpace files" · `security.sentinel` "Sentinel" — messages, hints, verdicts and support dates in **Security collector details** below | ports: [{port, proto (`tcp`\|`udp`), addrs, public, process, pid, expected (`open`\|`local_only`\|null), verdict (`expected`\|`local`\|`tailscale`\|`exposed`\|`new`), note?}], listening, reachable (public, Tailscale‑only ones left out), tailscale, exposed (exposed + new), new; firewall {active, name, units}; ssh {port, permit_root_login, password_authentication, readable, unreadable}; web {public_url, scheme, headers_missing, headers_checked, final_url, status_code, error}; updates {pending, security, reboot_required, reboot_pkgs, source}; software: [{name, version, until, status (`ok`\|`soon`\|`ended`\|`affected`\|`unknown`), note}]; files: [{path, mode, owner, group, world_readable, readable}]; sentinel {host, port, auth, login, open} |
| `logs` 📜 | `logs.<id>` "<label> log" — status by errors in window vs thresholds; file missing ⇒ *informational* unknown "file not found (<path>)"; unreadable, or behind a folder Sentinel may not enter (which reads "permission denied", not "file not found"), ⇒ counted unknown "permission denied (<path>)" with detail.hint (`PERMISSION_HINTS`, below); the entry with id `nginx` is left out (no rung, no metrics.files row) while the nginx module is off; a `daily_suffix` entry reads today's `<path>.YYYY-MM-DD` (DSpace 6) when present, else the most recently written of `<path>` and its `-YYYY-MM-DD` (DSpace 7–10) or `.YYYY-MM-DD` siblings, `<path>` winning a tie; line formats: log4j2 `dspace.log`, Solr, Tomcat JULI, the rsyslog `catalina.out` of packaged Tomcat (`[YYYY-MM-DD HH:MM:SS] [crit]…`: `[crit]`/`[err]`/`[alert]`/`[emerg]` errors, `[warning]`/`[warn]` warnings; every line of a record carries its severity, so a stamped line continues the record above it when its message is empty, is a trace line (`at`, `Caused by:`, `... N more`, `Suppressed:`, `[CIRCULAR REFERENCE`) or is a Throwable's first line with the same severity within `RECORD_GAP_S` = 1 s: a stack trace counts once and last_error is the record's first line), nginx; timestamps read in the entry's `timezone` (detail.timezone when not local: "UTC (detected)" or the configured name); lines stamped more than 5 min after now are not counted | per file: errors, warnings, last_error (line, ts), path |
| `repository` 📚 | (metrics only, every repository_interval; DB first, REST fallback) | items, bitstreams, communities, collections, db_size, assetstore_bytes, eperson_count, workflow_items |
| `assetstore` 🗄️ | `assetstore.path` "Assetstore readable" — a filesystem that does not answer within 10 s ⇒ crit "filesystem not responding (<path>)" | path, mount, total, used, free, pct, growth_bytes_per_day (from history) |

Dependency rungs mirror the dependency's current status but are **capped at `warn`**
(message "PostgreSQL is healthy", "Solr is degraded (Warning): <reason>", "Solr is down
(Critical): <reason>", "… could not be checked (Unknown): <reason>", <reason> being the
message of the rung that decides the state (`health._deciding_rung`): for "is down" the first
critical rung that failed, for "is slow" the first critical rung that only answered too slowly,
otherwise the first counted rung with the dependency's status (the roll‑up reason when the
dependency has no chain), so "is down (Critical): HTTP 500" never carries the message of a slow
rung above the failed one; a dependency that is critical only because every one of its critical
rungs carries `detail.slow` reads "Solr is slow (Critical): search took 4.3 s", `health.SLOW_STATE`;
detail `{dependency, dependency_status}`). Dependency graph:
`backend → [postgres, solr]`, `frontend → [backend]`, `nginx → [frontend]`,
virtual capability `search → [backend, solr]`, virtual `repository‑site → [nginx|frontend]`.

**Rung detail conventions.** Every HTTP rung (backend.rest/actuator/search, frontend.home/public,
nginx.http/https, solr.admin/search) carries `detail.status_code`, so `doctor` and the UI can
print "200 / 91 ms". A rung that is `unknown` only because the endpoint is not available on this
host is **informational** — `detail.informational = true` — and never counts towards the
component's status: the actuator answering 401/403/404, DSpace's known SEO `DOWN` on the actuator
(`detail.known_issue: "seo"`), a configured systemd unit that does
not exist or a host without `systemctl`, a log file that is not there, and a dependency rung
whose dependency has not been collected yet ("not monitored"). The ladder still shows such
rungs, muted, as "—  not available", or with `detail.known_issue` as "known issue · <message>"
(screen readers hear "Not available" / "Known issue"); a healthy component without a headline
latency counts them in its subtitle ("4 checks passed · 1 not available · 1 known issue").
`systemctl` not answering, an unreadable log file and
`ports` `local_only` targets that are closed remain *counted* unknowns ("checks could not run").
The Security rungs word their informational states themselves (the ladder shows them as they
are, "not available · sshd_config is not readable by Sentinel's user", "no SSH server found",
"no public address set"), and one that could not be checked at all also carries
`detail.unavailable: true`, which keeps the Security alert rules neutral (§7). `detail.alert:
true` marks the three Security warnings that also open an alert.

A rung may carry `detail.hint`: one or two plain sentences on what the situation is and what to
do, never a secret. The component pages show it as a muted line under the rung's message (§12.5).
Each hint starts with a word, never a user or unit name, because the ladder writes its first
letter in capitals. The collectors set it on:

* `solr.core.<name>`: the `<lib/>` fix, above.
* `nginx.http` (`nginx.NO_WEB_SERVER_HINT`), when the request was refused ("connection refused
  (localhost:80)", which stays crit) while the systemd rung says "unit <unit> not found" (not
  when `systemctl` is missing or no unit is configured): `No web server was found on this machine:
  there is no <unit> unit and nothing answers on <host>:<port>. If DSpace is opened directly on its
  own port (for example :4000), switch this check off in More › Modules. If it runs behind Apache
  or another unit, set its name and address in More › DSpace connection.`
* `ports.<port>` (`ports.EXPOSED_HINTS`), for an exposed `local_only` port, by service: Solr (the
  label contains "solr", or the port of `solr.url`) `Solr has no login: anyone who can reach this
  port can read and change the search index. Make Solr listen on localhost only, or block the port
  in the firewall (Sentinel sees where it listens, not your firewall). To accept it, change this
  rule to "open" in config/sentinel.yaml (ports: port <port>, expect: open) and restart Sentinel.`;
  PostgreSQL (the label contains "postgres", or `postgres.port`) `PostgreSQL holds DSpace's
  database: anyone who can reach this port can try to log in to it. Make PostgreSQL listen on
  localhost only (listen_addresses in postgresql.conf), or block the port in the firewall (Sentinel
  sees where it listens, not your firewall).` plus the same last sentence; any other port `This
  port should only be reachable from this machine: anyone who can reach it can connect to the
  service behind it. Make that service listen on localhost only, or block the port in the firewall
  (Sentinel sees where it listens, not your firewall).` plus the same last sentence. (More › DSpace
  connection has no port rules, so the hint points to the file.)
* `logs.<id>` (`logs.PERMISSION_HINTS`, `logs.permission_hint(path)`), for "permission denied",
  from what can be read about the file: its owner, group and mode (`stat`), or those of the first
  folder on the way that Sentinel may not enter; the user Sentinel runs as; whether the user and
  group databases list that user in the group (`listed_in_group`) and whether this process has the
  group (`process_has_group`). The group may read (a folder: enter) and the user is not in it:
  `The file belongs to <owner>:<group> (mode <mode>): add <user> to the group <group>, log out and
  in again, then restart Sentinel.`; the user is listed in the group but the process lacks it (it
  was started from a login older than the change): `The user <user> was added to the group
  <group>, but this Sentinel process does not have it yet: log out, log in again, then restart
  Sentinel.`; only the owner may read: `The file belongs to <owner>:<group> (mode <mode>), so only
  <owner> can read it: make it readable by the group <group>, add <user> to that group, log out and
  in again, then restart Sentinel.`; a folder on the way says `The folder <folder> belongs to …` in
  the same three forms; when nothing can be examined, the process already has the group, or
  everyone may read: `Sentinel runs as <user>, who may not read this file: give <user> read access
  (usually by adding <user> to the file's group), log out and in again, then restart Sentinel.`
  Building the hint never fails the collector.
* `security.*`: the hints listed under **Security collector details** below. A hint that starts
  with a command (`sudo …`, `chmod …`) keeps it as written: the ladder leaves its first letter
  alone (§12.5).

**Roll‑up** (`health.rollup(component, checks) -> ComponentHealth`): status = worst of the
counted rungs (`unknown` only wins over `ok`; a chain made only of informational rungs is
`unknown`), `reason` = first counted rung whose status ≠ ok, latency = the component's
headline latency (postgres.query, solr.search, backend.rest, frontend.home, nginx.http).

**Mood** (`health.mood(state, cfg, open_alerts) -> "ok"|"warn"|"crit"|"unknown"`): worst of all
enabled components plus server thresholds **and the open alerts**: any open critical alert ⇒
`crit`, any open warning alert ⇒ at least `warn`, whatever the components read right now (an
alert outlives its condition by `clear_samples` cycles, and an incident can be "Ongoing" while
every ladder is green again). `/api/status.mood`, the SSE `status` event, the accent colour and
`doctor`'s exit code all use it. When only alerts raise the mood the headline names them:
"Critical: 120 errors in DSpace log (5 min)" / "1 open alert" / "3 open alerts"; otherwise the
component wording applies. Maps to the UI accent: ok→mint, warn→butter, crit→coral, unknown→sky.
Acknowledging calms it: an acknowledged alert counts as none (`health.unacknowledged`), and a
component whose open alerts are all acknowledged counts as `ok` here
(`health.acknowledged_components`, `component_mood(state, cfg, calm)`; the server component too),
while its own status on the Stack, its page and in History stays what it is. With nothing else
wrong the headline is "All systems healthy · 1 acknowledged" / "· 2 acknowledged" (only the healthy
headline names them; the warning and critical ones count only what is not acknowledged). A new
alert, or an escalation (which clears `acked_ts`, §7), colours the mood again, and a component that
warns without any open alert still does.

**System collector details:** `psutil.cpu_percent(interval=None)` (first call primed at
startup), `virtual_memory`, `swap_memory`, `disk_usage("/")` + `disk_usage(assetstore mount)`
when different, `os.statvfs` for inodes, `getloadavg`, `net_io_counters` delta / elapsed,
`boot_time`, `platform.freedesktop_os_release()` (fallback `/etc/os-release`), `platform.release()`.
The sample is taken in a worker thread (`run_blocking`); the assetstore mount's `disk_usage` has
a 3 s timeout and the mount is left out of the sample (warning logged once) while its probe is
stuck; a stuck root sample becomes a `server.error` rung.

**PostgreSQL collector details:** `psycopg.AsyncConnection.connect(..., connect_timeout=5,
options="-c statement_timeout=3000 -c default_transaction_read_only=on", application_name=...)`,
then `SELECT 1`, `SELECT count(*) FROM pg_stat_activity`, `SHOW max_connections`,
`SELECT pg_database_size(current_database())`, `SELECT version()`, longest running query age.
Connection is closed every cycle (never pooled). Repository counts (DB path):
`SELECT count(*) FROM item WHERE in_archive AND NOT withdrawn`,
`SELECT count(*), coalesce(sum(size_bytes),0) FROM bitstream WHERE NOT deleted`,
`SELECT count(*) FROM community`, `SELECT count(*) FROM collection`, `SELECT count(*) FROM eperson`;
on statement timeout fall back to `pg_class.reltuples` estimates and mark `estimated: true`.

**Security collector details** (`collectors/security.py`, `util/support_dates.py`). It shows,
never changes: no command that changes anything, no sudo. Its sources all go through
`HostSources` (tests pass a fake): the socket table (`util.net.listening_sockets(with_process=True,
udp=True)`: TCP sockets in LISTEN and bound, unconnected UDP sockets; the program is known only for
Sentinel's own user's sockets unless it runs as root), the names of the network interfaces
(`interfaces()`, `psutil.net_if_addrs`), whether Sentinel runs as root (`is_root()`, the
effective user id), `systemctl is-active`, the files named below (at most 256 KB read from each),
`stat`, and one GET of `dspace.public_url` per run. The
blocking reads run in one `run_blocking` call (`READ_TIMEOUT_S` = 15 s), each guarded on its
own: a source that fails makes its rung informational, a rung that raises becomes "not available ·
<exception class>", and nothing escapes `collect()`. Rungs, in order:

1. `security.ports` "Open ports": one entry per (port, protocol), public when any of its
   addresses is (`util.net.is_public_address`: a wildcard or a non‑loopback address; an
   IPv4‑mapped address such as `::ffff:127.0.0.1`, as Java reports an IPv4 socket on a dual
   stack, is judged by the IPv4 address it maps, so that one is loopback). Verdicts, in this
   order: `local` (loopback only, even when a rule covers it); `expected` (reachable from the
   network and a `ports` rule covers it — a public `local_only` port is the Ports component's
   warning, not this rung's — and also Sentinel's own TCP port `server.port`, note "Sentinel", UDP
   68 and 546, notes "DHCP client" / "DHCPv6 client", and UDP `TAILSCALE_UDP_PORT` = 41641 bound to
   a wildcard address on every entry while the interface `tailscale0` exists, note `WIREGUARD_NOTE`
   "Tailscale's own port (WireGuard)"); `tailscale` (every address other machines can reach is in
   `TAILSCALE_NETWORKS`, `100.64.0.0/10` or `fd7a:115c:a1e0::/48`, note `TAILSCALE_NOTE` "reachable
   through Tailscale only"; no `tailscale0` needed; not exposed); `exposed` (reachable, no rule,
   already open when Sentinel started); `new` (reachable, no rule, not in the baseline). The
   baseline (`PortBaseline`) is the set of public (port, protocol) pairs of the first socket table
   that could be read; it lives in memory for the life of the process (a configuration change
   creates a new collector, not a new baseline; a restart takes a new one). Metrics: `listening`
   (every entry), `reachable` (public ones, the `tailscale` ones left out), `tailscale`, `exposed`
   (`exposed` + `new`: `unexpected_ports()`, shared with the firewall rung) and `new`. ok "9
   listening · 5 reachable from the network (22, 80, 443, 4000, 8080) · none unexpected" (at most
   `MAX_LISTED` = 8 ports named, then "+N more"; a UDP port as "68/udp"; "none reachable from the
   network"), with " · 2 through Tailscale only" before the last part while such ports exist; warn
   "9 listening · 6 reachable from the network · 1 unexpected: 3306 (mysqld)" (the program in
   brackets when known), hint "Ports other machines can reach that no rule expects: 3306 (mysqld).
   If a port is not meant for other machines, make its program listen on 127.0.0.1 or block it in
   the firewall; if it is, add a rule for it under ports in config/sentinel.yaml and restart
   Sentinel.", `detail.alert` only when a verdict is `new`. When Sentinel is known not to run as
   root and a port's program is unknown, the rung (ok or warn) carries `detail.note`
   `PROGRAM_NAMES_NOTE` "Program names are known only for Sentinel's own user; run
   `sudo ss -ltnp` on the server to see the others." (not in the demo, where root is not known).
   Unavailable: "not available · the socket table could not be read" (every count `null`).
2. `security.firewall` "Firewall": `systemctl is-active` for `ufw`, `nftables` and `firewalld`
   (asked together) and `ENABLED=yes` in `/etc/ufw/ufw.conf`. Ubuntu keeps `ufw.service` active
   while ufw is disabled, so ufw counts only when `ufw.conf` says `ENABLED=yes` and its unit is not
   inactive or failed (when the file cannot be read: when the unit is active); without systemd
   that line alone decides. ok "ufw active" ("nftables active", "firewalld active"). warn with
   `detail.alert` when no host firewall is active and a port is `exposed` or `new` or a
   `local_only` rule's port is public: "no host firewall is active and 2 ports are reachable from
   the network without a rule" (the count is `unexpected_ports()`, the ports rung's "unexpected"
   and `metrics.exposed`, so the two rungs and the Firewall tile agree), "… without a rule, and 1
   local-only port is public" when such a port is public too, "no host firewall is active and 1
   local-only port is public" when it is the only finding, hint "Switch a host firewall on: for
   example `sudo ufw allow OpenSSH`, then `sudo ufw enable`; or make those programs listen on
   localhost. Sentinel cannot see the network's firewall." Informational when nothing is exposed
   (ports reachable only through Tailscale and Tailscale's WireGuard port count as nothing
   exposed): "no host firewall active (this server relies on the network's firewall, which
   Sentinel cannot see)". Unavailable: "not available · systemctl is not available", "not
   available · systemctl did not answer".
3. `security.ssh` "SSH": `/etc/ssh/sshd_config` and the files its `Include` lines name (relative
   to `/etc/ssh`, globs in sorted order), read the way OpenSSH reads them: the first value of a
   keyword wins, so an `Include` at the top wins over the lines below it; `Match` blocks (up to
   `Match all`) are skipped; OpenSSH's defaults stand for a keyword found nowhere (`Port 22`,
   `PermitRootLogin prohibit-password`, which `without-password` also reads as,
   `PasswordAuthentication yes`). ok "root login: prohibit-password · passwords: yes · port 22"
   (plus " · 1 included file not readable"), with the hint "Keys instead of passwords: set
   PasswordAuthentication no once every admin has a key." while passwords are on. `PermitRootLogin
   yes`: warn with `detail.alert` "root may log in with a password" while passwords are on, warn
   without an alert "root may log in with a key (passwords are off)" when they are off, both with
   the hint "Set PermitRootLogin prohibit-password (or no) in /etc/ssh/sshd_config and reload
   sshd."; when an included file Sentinel cannot read comes before the value, the message ends in
   " (an included file Sentinel cannot read may change this)" and there is no alert. Unavailable:
   "not available · sshd_config is not readable by Sentinel's user", "not available · port 22
   listens, but /etc/ssh/sshd_config was not found". Informational: "no SSH server found" (no file
   and nothing on TCP 22).
4. `security.web` "Web": `dspace.public_url` and one GET of it per run (`WEB_TIMEOUT_S` = 10 s,
   redirects followed; only the response headers are used, the body is never read).
   `headers_missing` lists what the answer lacks of `Strict-Transport-Security` (only when the
   final answer is HTTPS), `X-Content-Type-Options`, and `X-Frame-Options or
   Content-Security-Policy` (one entry, when it has neither). ok "HTTPS on repo.example.org" (plus
   " · response headers not checked (<reason>)" when the GET failed; the certificate is the SSL
   component's job), with the hint "Missing response headers: Strict-Transport-Security,
   X-Content-Type-Options. They are set in the web server in front of DSpace (nginx add_header)."
   when any is missing. warn for an `http://` public URL: "the public address is plain HTTP: logins
   and sessions travel unencrypted", hint "Serve DSpace over HTTPS (for example with a Let's Encrypt
   certificate in nginx) and set dspace.ui.url and dspace.server.url to the https:// addresses.",
   or, when it redirects to HTTPS, "The address redirects to HTTPS, but DSpace's own address starts
   with http://: set dspace.ui.url and dspace.server.url to the https:// addresses." Without a
   public URL: warn "DSpace is served over plain HTTP on port 4000 to the network" when 4000 or
   8080 is reachable from the network, hint "Put nginx with HTTPS in front of DSpace, make Angular
   (4000) and Tomcat (8080) listen on 127.0.0.1, and set dspace.public_url in
   config/sentinel.yaml.", else informational "no public address set". Unavailable: "not available ·
   the public address is not an http:// or https:// address".
5. `security.updates` "Updates": `/var/lib/update-notifier/updates-available` (Ubuntu's
   update-notifier: "N updates can be applied immediately." / "M of these updates are standard
   security updates.", or the older "N packages can be updated." / "M updates are security
   updates."; ESM lines left out), `/var/run/reboot-required` and `/var/run/reboot-required.pkgs`.
   ok "no pending security updates (3 other updates)" or "no pending updates". warn "5 security
   updates pending", "reboot required (linux-image-6.8.0-45-generic)" (at most two packages, then
   "+N more") or both joined by " · ", hint "sudo apt update && sudo apt upgrade, then reboot when
   it says so." Unavailable without the update-notifier file (Debian, other distributions) and no
   reboot pending: "not available · no update information on this system (Ubuntu's
   update-notifier)".
6. `security.software` "Software": the versions the other components already report through
   `ctx.dependency(…).metrics` — `backend` `dspace_version`, `solr` `version`, `postgres`
   `version` — and `/etc/os-release`; nothing is probed again. ok "DSpace 9.4 · Solr 9.6.1 ·
   PostgreSQL 15.8 · Ubuntu 24.04: all supported" (versions as reported), plus " · PostgreSQL 13.4
   support ends 2025-11-13" for a `soon` entry and " · no support date known for Rocky Linux 9.4"
   for an `unknown` one. warn when anything is `ended` or `affected`: the first problem ("Ubuntu
   20.04 reached end of standard support on 2025-05-31", "Solr 8.8.2 is affected by Log4Shell
   (CVE-2021-44228): upgrade to 8.11.1 or later", "DSpace 7.6.5 reached end of life on
   2026-05-28", "Solr 7.7.3 is no longer supported"), then " (+N more)", and with more than one
   problem a hint that lists them all. A version not known yet (module off, not checked yet) is
   left out; only `unknown` entries ⇒ informational; nothing at all ⇒ unavailable "not available ·
   no software versions known yet".
7. `security.files` "DSpace files": `stat` (never the contents) of
   `<dspace.install_dir>/config/local.cfg` and `dspace.cfg`, and of Sentinel's own
   `config/sentinel.yaml` and the `sentinel.env` next to it; a file that is missing or cannot be
   looked at is left out, and `dspace.cfg` is listed but never graded. warn "local.cfg is readable
   by every user on this server and holds the database password (mode 644, tomcat:tomcat)", hint
   "chmod 640 /dspace/config/local.cfg and keep it owned by the Tomcat user and its group."; warn
   "sentinel.yaml is readable by other users (mode 644): it holds the Telegram tokens and the email
   password" or "sentinel.env is readable by other users (mode 644): it holds the secrets Sentinel
   reads when it starts", hint "chmod 600 <path>: only Sentinel's user needs to read it."; with more
   than one problem the first, " (+N more)", and every hint. ok "local.cfg and sentinel.yaml are
   not readable by other users" (the ones found of local.cfg, sentinel.yaml and sentinel.env), plus
   " · local.cfg not found" while a DSpace module is on. Unavailable: "not available · local.cfg
   not found", "not available · no configuration file could be checked".
8. `security.sentinel` "Sentinel": `server.host` and whether the dashboard login
   (`auth.login_active`, never in the demo) and `server.auth_token` are set; `metrics.sentinel` =
   `{host, port, auth, login, open}` (`open`: not loopback, no token, no login). ok "listens on
   127.0.0.1 only" (any loopback address, or `localhost`); ok "listens on 0.0.0.0 with a login",
   hint "Put the nginx HTTPS recipe in front of Sentinel so the password never travels in plain
   text." (also with a token next to the login); ok "listens on 0.0.0.0 with a password" (a token
   and no login); warn "listens on 0.0.0.0 without a password (SENTINEL_ALLOW_OPEN)" or "…
   (server.allow_unauthenticated)", hint "Create the dashboard login with ./sentinel.sh
   set-password (or set server.auth_token), or listen on 127.0.0.1 and reach Sentinel through the
   SSH tunnel or the nginx HTTPS recipe."

Only the three `detail.alert` warnings (ports, firewall, SSH) ever alert; updates, software, web,
files and Sentinel are shown on the Security page and in reports. **Support dates**
(`util/support_dates.py`, each checked against its source on `CHECKED_ON` = 2026-10-05): every
`metrics.software` entry is `{name, version, until, status, note}`, `until` being the last
supported day (`YYYY-MM-DD`) or null, `status` `ok`, `soon` (support ends within `SOON_DAYS` = 90
days; still ok), `ended`, `affected` or `unknown`.

| Software | Rule | Source |
|---|---|---|
| DSpace | 7.x `ended`, end of life on 2026-05-28 (the DSpace 10.0 release, `dspace_version.end_of_life`); older ones "no longer supported"; 8, 9 and 10 supported | DSpace release policy (wiki.lyrasis.org) |
| Solr | 8.0 – 8.11.0 `affected`: "affected by Log4Shell (CVE-2021-44228): upgrade to 8.11.1 or later"; older than 8 `ended`, "no longer supported"; 8.11.1 and later, 9.x supported | solr.apache.org/security.html |
| PostgreSQL | final release of the major: 11 → 2023-11-09, 12 → 2024-11-14, 13 → 2025-11-13, 14 → 2026-11-12, 15 → 2027-11-11, 16 → 2028-11-09, 17 → 2029-11-08; newer than 17 supported, older than 11 no longer supported | postgresql.org/support/versioning |
| Ubuntu | end of standard support: 18.04 → 2023-05-31, 20.04 → 2025-05-31, 22.04 → 2027-04-30, 24.04 → 2029-04-30 | ubuntu.com/about/release-cycle |
| Debian | end of LTS support: 10 → 2024-06-30, 11 → 2026-08-31, 12 → 2028-06-30 | wiki.debian.org/LTS |
| any other OS or version | `unknown`, "no support date known" (no verdict) | — |

**DSpace versions** (`dspace_version.py`): `parse()` reads the REST root's `dspaceVersion`
(`"DSpace 9.4"`, `"DSpace 7.6"` for 7.6.0, `"DSpace 7.6.8-SNAPSHOT"`, `"DSpace 10.2-SNAPSHOT"`;
`None` for `"DSpace unknown"`, `"DSpace none"`, anything else) into `DSpaceVersion(major, minor,
patch|None, qualifier)`; `version_metrics()` adds `crisVersion` (DSpace‑CRIS). The helpers are
the only place version windows live: `seo_reports_down(v)` (`[7.6.4, 7.7)`, `[8.2, 8.5)`, `[9.0,
9.4)`, `[10.0, 10.1)`), `ssr_host_restricted(v)` (≥ 9.3), `expected_solr_major(v)` (7/8 → 8, 9/10
→ 9), `optional_cores(v)` (`qaevent`, `suggestion` from 8, `audit` from 10; every one when the
version is unknown), `end_of_life(v)` (below 8: `SUPPORTED_MAJORS` = 8, 9, 10; `FINAL_7X_RELEASE`
= 7.6.7). The backend publishes the parsed version; the frontend and Solr collectors read it from
`ctx.dependency("backend")`. An unknown version takes the cautious reading (a `DOWN` warns, the
generic CSR note, every optional core). `doctor` marks a 7.x backend "DSpace 7.6.7 (end-of-life)".

**Discovery** (`discovery.discover(cfg) -> DiscoveryResult`): reads `<install_dir>/config/local.cfg`,
then `dspace.cfg`, then `modules/assetstore.cfg` (earlier files win; `${var}` interpolation;
`dspace.dir` defaults to `install_dir`), extracts `db.url` (parse `jdbc:postgresql://host:port/db`,
`sslmode` from its query), `db.username`, `db.password`, `solr.server`, `solr.multicorePrefix`,
`dspace.server.url`, `dspace.ui.url`, `assetstore.dir`, `assetstore.index.primary`,
`log.report.dir`/`log.dir`; lists listening ports; probes systemd units (`postgresql`,
`postgresql@*`, `solr`, `tomcat9`, `tomcat10`, `tomcat11`, `tomcat`, `dspace-backend`,
`dspace-server`, `dspace`, `dspace-ui`, `dspace-angular`, `dspace-frontend`, `pm2-*`, `*dspace*`,
`nginx`, `apache2`; a `*` name lists the loaded units matching the glob; Sentinel's own
`dspace-sentinel` unit is left out). The backend unit is the first of `tomcat9, tomcat10,
tomcat11, tomcat, dspace-backend, dspace-server, dspace`, the UI unit the first of `dspace-ui,
dspace-angular, dspace-frontend, pm2-*`, an active unit winning over an earlier one that is not
(exact names never match by prefix); a `pm2-<user>` pick adds "<unit> is PM2's own unit: it only
proves the PM2 daemon runs (the homepage check shows whether the DSpace UI answers)". Where a port
(8080/8983), a unit or DSpace's configuration shows a backend or Solr, `probe_versions()` sends two
anonymous GETs with a 3 s timeout (`VERSION_TIMEOUT_S`; TLS per `dspace.verify_tls`),
`<server_url>/api` and `<solr>/admin/info/system?wt=json`, and `version_findings()` reports "DSpace
9.4 answers at <url>" (" (DSpace‑CRIS <v>)" after the version when present), "Solr 9.8.1 answers
at <url>", "DSpace 7.x is end-of-life (7.6.7 was the final release; supported: 8.x, 9.x, 10.x)" and
"DSpace 9 expects Solr 9 but Solr 8.11.4 answers". It returns `suggested_config` (a patch) and
`findings` (human sentences: "Found PostgreSQL on 127.0.0.1:5432 (local only)"). The suggestion keeps
the current `logs.files`, moves the `dspace` entry to DSpace's log directory (adding it when
missing) and, when `<log dir>/dspace-cli.log` exists, adds `{id: dspace-cli, label: DSpace CLI,
path, daily_suffix: true, timezone: ""}` after it ("DSpace scripts log to …: added as the DSpace CLI
log"); every suggested entry has these five keys, an inserted `dspace` entry `timezone: ""` too. A
packaged Tomcat log (`/var/log/tomcat*/catalina.out`) follows a `tomcatN` backend unit, and is left
out ("No Tomcat unit: the Tomcat log (…) is left out; the runnable JAR of DSpace 8+ writes its Tomcat
messages to dspace.log") when systemd is visible, no `tomcat*` unit exists, a backend unit or port
8080 does, and the file is absent; a custom path is never rewritten. `solr.multicorePrefix` prefixes
the suggested `solr.cores` ("Solr core names start with <prefix> (solr.multicorePrefix)");
`assetstore.index.primary = 1` (S3) suggests no `assetstore_dir` and leaves the assetstore module
off. `ports` lists only the configured rules whose port is listening (the PostgreSQL rule follows
`db.url`'s port) and `modules.ports` is true only then; `modules.security` is always true (it
reads this server, whatever runs on it); the nginx module and `nginx.service` are
suggested only for an nginx/apache2 unit that is active or failed. When systemd is visible and a
service answers without a unit (Solr on 8983 without a `solr` unit, the UI on 4000 without a UI
unit, a web server on 80/443 without an active or failed nginx/apache2 unit), the suggestion sets
`solr.service`, `dspace.frontend_service` or `nginx.service` to `""` (no systemd rung, so no "unit
… not found") and adds "Solr answers on 8983 but no systemd unit was found for it: it may not start
by itself after a reboot", "The DSpace UI answers on 4000 but no systemd unit was found for it: it
may not start by itself after a reboot" or "A web server answers on 80 and 443 but no nginx or
apache2 unit runs: Sentinel checks only whether it answers" (naming the ports that answer). On a
DSpace host (its configuration was read, or a backend port, unit or URL was found) the default
nginx log entry (`/var/log/nginx/error.log`) stays only for a web server that was found or a log
that is there ("No web server was found: the nginx log (/var/log/nginx/error.log) is left out"),
and the default Solr entry (`/var/solr/logs/solr.log`) only when that log is there; otherwise it
follows the first absolute `-Dsolr.log.dir=<dir>` on a running process's command line
(`<dir>/solr.log`, "Solr logs to <dir>/solr.log (its -Dsolr.log.dir): used as the Solr log"; the
process list is read, with psutil, only then) or is left out ("The Solr log
/var/solr/logs/solr.log does not exist: it is left out"). A log in a folder Sentinel may not
enter counts as there; custom paths, `dspace.log` and `dspace-cli.log` are never touched; where
DSpace does not run, the chosen option's log list stays. `discover()` takes injectable `sockets`,
`unit_states`, `properties`, `file_exists`, `versions` and `cmdlines` (tests, demo).

`DiscoveryResult.to_dict()` (what the API and `discover`/`discover --json` show) never carries a
secret: `suggested_config.postgres.password` and a password inside the Solr URL are masked,
`properties` keeps only `dspace.dir`, `dspace.name`, `dspace.server.url`, `dspace.ui.url`,
`db.url`, `db.username`, `solr.server`, `solr.multicorePrefix`, `assetstore.dir`,
`assetstore.index.primary`, `log.dir`, `log.report.dir` (URLs without credentials or query
strings), and findings show redacted URLs. The API keeps the raw suggestion for 30 minutes;
`restore_secrets` puts masked values back server‑side when a setup or config patch applies it, the
database password only when the patch's PostgreSQL host, port, database and user are the
discovered ones. In demo mode discovery answers from the demo stack (persona `full`, the demo
units `postgresql`, `solr`, `tomcat10`, `dspace-ui`, `nginx` and sockets, versions DSpace 9.4 and
Solr 9.6.1, no secrets, first finding "Demo mode: these findings describe the demo server, not this
machine.") and never inspects the host, its process list or a service (the demo stack has its Solr
and nginx logs); applying it (*Start monitoring*) saves
`dspace.backend_service: tomcat10` and the `tomcat10` log path to the demo state file.

---

## 7. Alerts, incidents, notifications (`alerts/`)

**Rules** (`rules.py`; each yields an `Observation(rule, component, severity|None, value, threshold, title, message)`):

| rule | source | title / message pattern |
|---|---|---|
| cpu | sample.cpu_pct | "CPU 96 %" / "load 7.9 on 4 cores" |
| memory | sample.mem_pct | "Memory 93 %" / "12.9 of 16 GB used, swap 40 %" |
| swap | sample.swap_pct | "Swap 61 %" / "2.4 GB of 4.0 GB used" |
| disk | sample.disk_pct (`disk`) and every extra mount (`disk:<mount>`) | "Disk 87 % on /" / "free 53.7 GB · growing 2.1 GB/day" |
| load | load1 / cores | "Load 3.2 per core" |
| rest_latency | backend.rest latency | "REST API slow: 2.4 s" |
| search_latency | backend.search latency | "Search API slow: 2.6 s" |
| solr_latency | solr.search latency (same thresholds as search_latency) | "Solr search slow: 4.8 s" — sequence text "Solr response 4.8 s" |
| home_latency | frontend.home latency | "Homepage slow: 6.1 s" / "GET <ui_url>" |
| postgres_connections | connections_pct | "PostgreSQL connections 86 / 100" |
| ssl_expiry | days_left (inverted thresholds) | "Certificate expires in 12 days" |
| log_errors:<file id> | per log file errors in window | "48 errors in DSpace log (5 min)" / last error line |
| assetstore_free | free_gb (inverted) | "Assetstore free space 9.1 GB" |
| component_down:<id> | hard failures in the chain: crit ⇒ critical, warn ⇒ warning (rungs graded by their own threshold rule and `*.dep.*` rungs excluded) | "<Label> unreachable" (failing connectivity rung) / "<Label> failing" (other critical rung) / "<Label> degraded" (warning); message = chain summary up to the failing rung, then its reason: "service ✅ → TCP ✅ → connect ❌\nconnection refused (localhost:5432)" |
| security_ports | `security.ports` rung with `detail.alert` (a port reachable from the network that no rule expects opened after Sentinel started), warning only | "New port open to the network" / the rung's message: "9 listening · 6 reachable from the network · 1 unexpected: 3306 (mysqld)" |
| security_firewall | `security.firewall` rung with `detail.alert` (no host firewall active while ports are exposed, or a `local_only` port is public), warning only | "No host firewall while ports are exposed" / "no host firewall is active and 2 ports are reachable from the network without a rule", "… without a rule, and 1 local-only port is public", "no host firewall is active and 1 local-only port is public" |
| security_ssh | `security.ssh` rung with `detail.alert` (`PermitRootLogin yes` with passwords on, nothing unreadable before it), warning only | "Root may log in over SSH with a password" / "root may log in with a password" |

A check that could not run (the `<id>.error` rung, `detail.collector_error`) measured nothing:
`component_down` is neutral for it (neither breach nor OK), as it is for a chain without any
`ok`/`warn`/`crit` rung apart from dependency rungs; the per‑file log rules skip the error rung;
and `_resolve_stale` leaves a component's alerts open while its last check errored. Alerts whose
subject disappeared (module switched off, mount or log file gone) resolve without a notification.

The `security` component has the three Security rules (`rules.SECURITY_RULES`, rule → (rung id,
title); `security_rule(rule, comp)`) instead of `component_down`, because most of its warnings
(updates, software, web, files, Sentinel) are shown on its page and in reports and never alert.
Each rule is a warning while its rung is `warn` with `detail.alert`, OK when the rung was checked
and says otherwise, and neutral when the rung is missing, is the collector's error rung or carries
`detail.unavailable` (could not be checked), so an unreadable file neither opens nor clears the
alert. The message is the rung's message; the keys are `security_ports@security`,
`security_firewall@security` and `security_ssh@security`, and the alerts go through the usual
`confirm_samples` / `clear_samples`, quiet hours and channels.

**Engine** (`engine.py`): per rule+component key, keep `breach_count`, `ok_count`, current
open alert. Open after `confirm_samples` consecutive breaches; escalate warning→critical
immediately when the critical threshold is crossed (`_escalate` sets `acked_ts = None`: a new
situation, so Home, the reminders and `doctor` treat the alert as unacknowledged again); resolve
after `clear_samples` consecutive
OK observations (hysteresis: an observation counts as OK only when value < warning threshold −
2 % of range, or status ok). While an alert is open at the same severity its stored title,
message and value are refreshed silently (no event, no notification) when the situation got worse
(a higher value; a lower one for the inverted rules) or, for `component_down` and the Security
rules, which carry no value, when the title or message changed: a second new port updates the open
`security_ports` alert. `ack(alert_id)` silences reminders and calms Home (an acknowledged alert
leaves the mood and the headline, `health.mood`/`headline`, §9); `unack`. Every transition
writes an `events` row and emits a bus event `alert` with
`{type: opened|escalated|resolved|acked|unacked, alert, ts}`.
Notifications go through `notifiers.dispatch(cfg, event)` to the channels `telegram`, `email` and
`webhook`: `opened`, `escalated`, `resolved` when they happen; reminders every `reminder_minutes`
while open and unacked. Every message carries its alert's severity (an incident is critical when
one of its alerts is). For each channel `dispatch` skips one that is switched off (`disabled`,
checked first, so no quiet‑hours row is written for it), applies that channel's quiet hours
(`notifiers.telegram.quiet_hours`, `notifiers.email.quiet_hours`, read on the wall clock of
`server.timezone`, `tz.wall`; every alert or incident message that is not critical — warning
opens, warning reminders, the resolution of a warning and warning‑only incidents — is dropped on
that channel, not delayed; digests, tests and the webhook are exempt), reports `not configured` /
`no chats` / `no recipients` when it cannot send, and keeps a destination whose `min_severity` is
`critical` to alert and incident events whose severity is critical (tests, digests and reports go
to every destination; `min severity` when nobody takes the event). An event may carry `channels` to limit where it goes (the digests: `["telegram",
"webhook"]` and `["email"]`; never part of the webhook body). The eligible channels are sent side by side, each within its own
cap: Telegram `10 s × bots + 5 s`, email `20 s × 3 + 5 s`, the webhook `NOTIFY_TIMEOUT_S` = 15 s.
The daily digest goes out per channel: at `notifiers.telegram.daily_digest` to Telegram and the
webhook (while either is on, or in demo mode; kv `last_digest_ts`) and at
`notifiers.email.daily_digest` by email (while email is on; kv `last_email_digest_ts`), both
times of day in `server.timezone` (`notifiers.digest_due_ts(spec, now, tz)`); the first
time a digest time is seen (no kv row yet) it is armed without firing for a time already past
today. It sends the last 24 h as counts
(alerts, of which critical, and incidents) and up to five open alerts, **oldest first**, then "…
and N more". Notification failures are logged as events, never raise.

Delivery is asynchronous: the alert and incident engines queue their notification coroutines on
one shared `notifiers.Outbox` that keeps one FIFO lane per channel: a job takes its turn in the
lane of every channel it sends to, so each channel delivers one event at a time in the order they
happened, and a channel that stalls (a mail server that never answers, an unreachable
`api.telegram.org`) delays only its own later messages, never another channel's, with two
exceptions. (1) A job stays running until its slowest channel is done and at most `OUTBOX_MAX` =
500 jobs run at once: with 500 waiting behind a stalled channel the next job starts only when one
of them ends, so the other channels wait too, and up to 500 more jobs wait unstarted, beyond which
the oldest waiting one is dropped for every channel ("notification outbox full: dropped the oldest
waiting notification" in the log). (2) A job takes the configuration when it starts, right after
its event unless 500 are running (an incident's job binds it when it is queued), and `dispatch`
decides the channels, destinations and limits then, before it waits for its lane turn: after a
save, the deliveries already waiting still use the old settings (repaired mail settings reach only
later events, and a channel switched off meanwhile still sends, or fails, what was waiting, with
its rows). Collection
never waits for a delivery. `notify()` writes each channel's audit row as soon as that channel is
done: `notify` (delivered; `data.channel`, plus `data.destinations` `[{destination, id?, label, ok,
error}]` for Telegram and email, `message_id` for Telegram, `subject` for email), `notify.error`
("<channel> failed: <error>"), `notify.skipped` ("<channel>: <summary> held back by quiet hours")
and, in demo mode, `notify.demo` (`data.channels`, `html`, `text`, and `subject` when email is among
them). `notified_ts` is recorded after a successful delivery; `AlertEngine.flush()` /
`IncidentEngine.flush()` wait for the queue, and on shutdown the scheduler gives it up to 10 s
(`STOP_FLUSH_TIMEOUT_S`); what is then still under way (sending, or waiting for its lane turn) or
not started is given up with one warning, "5 notifications not delivered (Sentinel is stopping)"
(`scheduler._undelivered`: the outbox's running jobs that are not done plus `len(outbox)`, one per
event however many of its channels are left; nothing is logged for none). The
webhook's own client always verifies TLS (`dspace.verify_tls` does not apply) and never follows
redirects: a 3xx answer is recorded as "HTTP 302 from webhook (redirects are not followed)". Every
bot token is redacted from every error text, in any case and URL‑encoded.

**Incidents** (`incidents.py`, module `incidents` must be on): when an alert opens, look for an
open incident with activity in the last `window_minutes=5`; else if another alert opened in
the window, create one grouping both. Hypotheses are matched in order against the set of
(rule, component) pairs in the incident:

| signature | hypothesis |
|---|---|
| memory|swap + solr (latency or down) + (backend.search or search_latency) | "Possible Solr resource exhaustion" |
| disk + postgres | "Disk pressure affecting PostgreSQL" |
| postgres down + backend down/latency | "Database outage cascading to the DSpace backend" |
| solr down + backend search / search_latency | "Solr outage: DSpace search unavailable" |
| backend down + frontend down | "Backend outage: the UI cannot reach the REST API" |
| cpu|load + rest_latency|home_latency | "CPU saturation slowing responses" |
| assetstore_free|disk + log_errors | "Storage pressure causing application errors" |
| anything else | "Multiple alerts within 5 minutes" |

`affected` = dependency closure (e.g. solr → ["DSpace Search", "DSpace Backend"]).
`sequence` = alerts (and escalations) ordered by time as
`{ts, text: "Solr response 4.8 s", severity, alert_id}`; the demo backfill writes the same.
An incident resolves when all its alerts are resolved. Every channel gets one message on open
(with the sequence) and one on resolve (duration).

**Telegram message formats** (`messages.py`, parse_mode HTML, hostname from cfg). Every time
names the zone of `server.timezone` (`alert_html(…, tz=)`, `incident_html(…, tz=)`, with
`tz.zone_of(cfg)` passed by the engines): its abbreviation (UTC, CEST), or the UTC offset when the
zone has none (Asia/Colombo: 21:29 +0530). The email's timeline pattern accepts the zone
(`email_messages._SEQUENCE`):

```
🔴 <b>CRITICAL</b> · server-1
<b>PostgreSQL unreachable</b>
service ✅ → TCP ✅ → connect ❌
connection refused (localhost:5432)

Started 10:43 UTC · DSpace Sentinel
```
```
🟡 <b>WARNING</b> · server-1
<b>Disk 87 % on /dspace</b>
free 53.7 GB · growing 2.1 GB/day

Started 10:41 UTC · DSpace Sentinel
```
```
✅ <b>Resolved</b> · server-1
PostgreSQL unreachable · lasted 6 m 12 s
```
```
🧭 <b>INCIDENT #24</b> · server-1
<b>Possible Solr resource exhaustion</b>

10:41 UTC  Memory 94 %
10:42 UTC  Solr response 4.8 s
10:43 UTC  Solr DOWN
10:43 UTC  DSpace search unavailable

Affected: DSpace Search, DSpace Backend
```
**Telegram** (`notifiers/telegram.py`): every `notifiers.telegram.chats` entry is its own bot
(`bot_token`) writing to one chat (`chat_id`); only entries with both are sent to.
`send_message(cfg, html, chats=)` and `send_document(cfg, path, caption, chats=)` go through the
destinations one after another, each with its own token and a 10 s timeout, and answer `{ok,
message_id, error, results: [{id, label, chat_id, ok, error, message_id}]}` (`ok` when every
destination got it; several failures read "2 of 3 bots failed — Team: …; Kim: …"). The document's
caption goes as plain text (no `parse_mode`), so a title with `<` or `&` arrives as typed.
`send_test(cfg, destination_id=None)` calls `getMe` with each destination's token (whether or not
Telegram is on, whatever `min_severity`), then sends "👋 DSpace Sentinel is connected · <host>", and
answers `{ok, bot_name, error, results: [{id, label, chat_id, ok, error, bot_name}]}` (`bot_name` is
"@<username>", or the first name; the top‑level one is the first bot that answered). Refusals
become advice (`friendly_error`): "chat not found — send the bot a message first" (a person), "… —
add the bot to the group first" (a negative id), "… — add the bot to the channel as an
administrator first" (@channel), "the bot has not been started in this chat — open it in Telegram
and press Start", "the bot was blocked in this chat — unblock it in Telegram", "the bot was
removed from this group — add it again", "the bot is not a member of this chat — add it first",
"this Telegram account is deactivated", "the bot may not post in this chat — allow it to send
messages", "this group became a supergroup: its chat id is now <id>", "Telegram refused the bot
token (401 Unauthorized) — copy it again from @BotFather" (also for 404); anything else
"<description> (error <code>)". Nothing to send to: "Telegram has no bot yet: add a bot token and
a chat id"; a test of an entry without a token or chat id: "no bot token: paste the token
@BotFather gave you" / "no chat id: send the bot a message, then paste your chat id". A request
that got no answer (`transport_error(exc)`, which looks along `__cause__`, `__context__` and
exception groups for the socket's own error, since httpx reports most as "All connection attempts
failed") says what happened and what to check: a time‑out "api.telegram.org did not answer within
10 s: this server may not be able to reach Telegram (a firewall, or a proxy it has to use: set
HTTPS_PROXY in config/sentinel.env)"; a refused connection "api.telegram.org refused the
connection: a firewall on this server or its network probably blocks Telegram (allow outgoing
HTTPS, port 443, or set HTTPS_PROXY in config/sentinel.env if the network has a proxy)"; a failed
lookup "this server cannot look up api.telegram.org (DNS): check its name servers
(/etc/resolv.conf), or set HTTPS_PROXY …"; a certificate "the TLS certificate of api.telegram.org
could not be verified (<reason>): check this server's date and time, and whether a proxy or
firewall on this network intercepts HTTPS"; another TLS failure or a cut handshake "the secure
connection to api.telegram.org failed (<reason>): a firewall or proxy on this network may block or
intercept Telegram (set HTTPS_PROXY …)"; any other connect error "cannot connect to
api.telegram.org (<strerror, e.g. network is unreachable>): check this server's network and
firewall, or set HTTPS_PROXY …". With `HTTPS_PROXY` or `ALL_PROXY` set (and `NO_PROXY` not naming
Telegram) the sentence is about the proxy, named by host and port only, never its login: "…
did not answer within 10 s through the proxy <host:port>: check HTTPS_PROXY in config/sentinel.env,
and that the proxy lets this server reach Telegram", "the proxy <host:port> refused the connection:
check its address in HTTPS_PROXY (config/sentinel.env)", "this server cannot look up the proxy
…", "cannot connect to the proxy …", and for an `httpx.ProxyError` "the proxy <host:port> did not
connect to api.telegram.org (407 Proxy Authentication Required): check HTTPS_PROXY in
config/sentinel.env (the address, and a login if the proxy needs one)". Anything else keeps
`util.httpc.describe_error`'s text, which the collectors still use unchanged.

**Email** (`notifiers/email.py`, `email_messages.py`): one SMTP session and ONE message per event,
addressed (To:) to the recipients that take it: From "<from_name> <from_address or username>", To,
Subject, Date, Message‑ID (@ the sender's domain), `Auto-Submitted: auto-generated`,
`X-Auto-Response-Suppress: All`; `multipart/alternative` text/plain + text/html, both
quoted‑printable; the HTML uses inline styles only, loads nothing and reads in light and dark
clients; the bodies are built from the Telegram message so the facts match, with the footer "Sent
by DSpace Sentinel on <host>. Who gets these emails is set in More › Email.". Subjects (one line,
≤ 200 characters): "[Sentinel] CRITICAL · <host> · <title>" / "[Sentinel] Warning · …",
"[Sentinel] CRITICAL (escalated) · …", "[Sentinel] Still open · …", "[Sentinel] Resolved · <host>
· <title> (lasted 6 m 12 s)", "[Sentinel] Incident #<n> · <host> · <hypothesis>", "[Sentinel]
Incident #<n> resolved · <host> · <hypothesis> (lasted 7 m 30 s)", "[Sentinel] Daily digest ·
<host>", "[Sentinel] Test email · <host>", "[Sentinel] Report · <host> · <report title>". The
session runs `smtplib` in a daemon thread with a 20 s timeout on every socket step and EHLO as
this machine's hostname; TLS is verified with `ssl.create_default_context()`: STARTTLS
(`starttls`, 587), implicit TLS (`ssl`, 465) or `none`. A delivery given up on (its cap, or a
shutdown) cuts the SMTP connection (`_Connection.cut_off`; the session looks at the cut again
right after the STARTTLS handshake, during which the socket cannot be shut down), so the message
is not handed over later. Reports are `multipart/mixed` with the file attached (`text/html` and
`text/markdown` with `charset="utf-8"`, `application/json`), at most `MAX_ATTACHMENT_BYTES` = 17
MB as `util.fmt.bytes_` counts sizes (17 × 1024² = 17,825,792 bytes, about 24.4 million bytes on
the wire): "<file> is too large to email (17.1 MB; the limit is 17 MB)", the size rounded up to the
next 0.1 MB; the whole DATA goes out in one send under the 20 s socket timeout, so over an uplink
slower than roughly 10 Mbit/s a large attachment can fail with "<host>:<port> did not answer within
20 s" (smtplib reports that time‑out as a disconnect with the `TimeoutError` in `__context__`,
which reads as the time‑out too).
`send_test(cfg, address=None)` sends "[Sentinel] Test email · <host>" to every recipient (or one)
whether or not email is on, answering `{ok, error, results: [{address, label, ok, error}]}`.
Failures become one sentence without the password: Gmail's 534/535 "Gmail refused the login for
<username> (<code> <first sentence of the first line of Gmail's answer, at most 80 characters, cut
after a whole word>): use an app password made in that same Google account (Google Account ›
Security › 2-Step Verification › App passwords), not the normal password" (`GMAIL_APP_PASSWORD`;
the answer is scrubbed of the password before it is cut, and the whole sentence again), for
example "Gmail refused the login for sentinel@example.org (535 5.7.8 Username and Password not
accepted): …"; other servers "the mail server refused the login (<code>): check the username and
password"; 'security "none" would send the password unencrypted: use starttls or ssl, or leave
the username empty for a relay that needs no login'; "<host>:<port> does not offer STARTTLS: use
security "ssl" (port 465) or the server's TLS port"; "<host>:<port> does not accept a login: leave
the username empty or use its submission port"; "<host>:<port> did not answer within 20 s" (+ '
(port 465 needs security "ssl")'); "<host>:<port> closed the connection: check the port and
security (Gmail: starttls on port 587, ssl on port 465)", for `smtp.gmail.com` and
`smtp.googlemail.com` (a disconnect that is not a time‑out) "smtp.gmail.com:587 closed the
connection. Gmail does this for a few minutes after a refused login: check the address and the app
password, wait, then test again. Otherwise check the port and security (Gmail: starttls on port
587, ssl on port 465)" (`GMAIL_CLOSED_HINT`); "<host>:<port> refused the session: …";
"the TLS certificate of <host> could not be verified (<reason>): check the server name"; "TLS with
<host>:<port> failed: check the security setting (…)"; "cannot find the mail server <host>: check
its name"; "<host>:<port> refused the connection: check the server name and port"; "cannot reach
<host>:<port>: <reason>"; "the mail server refused the sender <addr>: …"; "the mail server refused
every recipient"; "the mail server refused the message: …"; "the mail server answered <code>
<text>"; "the SMTP username is not an email
address: set from_address (the address alerts come from)"; a refused recipient "refused by the
mail server: <code> <text>"; a `UnicodeEncodeError` (smtplib sends the login as ASCII) the
sentence below; anything else "unexpected error (<exception class>)", never the exception's text.
Not ready (`unready(cfg)`, answered before any connection, for the test, alerts and reports): a
login with a character outside ASCII, "the email password has a character that cannot be sent: use
letters, digits and symbols from the English keyboard" (`PASSWORD_NOT_SENDABLE`) or "the SMTP
username has a character that cannot be sent: …" (`USERNAME_NOT_SENDABLE`), both defined in
`config.py`; a sender that is not an address, "the SMTP username is not an email address: …";
otherwise "email is not set up: add the Gmail address, app password and at least one recipient"
(also for the mask pasted into the file as the password, which loads as no password). `PATCH`
refuses such a login when it is typed (§3).

---

## 8. Reports (`reports/`)

`generate(store, cfg, t_from, t_to, fmt="html", title=None, sections=None) -> Report`.
`analytics.compute_report_data` returns:

```
{ meta: {title, hostname, generated_ts, timezone: {name, abbreviation}, range: {from, to, label, title, span_s}, sentinel_version,
         sections, demo, modules},
  summary: {health_score (0‑100 = mean availability weighted: backend 30, postgres 25, solr 15, frontend 15, nginx 10, others 5),
            availability_pct, incidents, alerts_opened, alerts_critical, alerts_warning, worst_component, uptime_s,
            samples, coverage_pct, mood},
  server: {cpu: {avg,p95,max}, mem: {...}, swap: {...}, disk: {...}, load: {...}, net: {...}, mounts, uptime_s, os, kernel,
           cores, series: {cpu:[[ts,v]], mem:[], disk:[]}},
  services: [{id, label, emoji, availability_pct, downtime_s, outages: n, worst_status, avg_latency_ms, max_latency_ms, …}],
  latency: {rest: {p50,p95,max, series, thresholds}, search: {...}, home: {...}, postgres: {...}, solr: {...}},
  alerts: [ ... ], alert_stats: {opened, critical, warning, still_open, …}, incidents: [ ... ],
  repository: {now: {...}, delta: {items:+n, bitstreams:+n, assetstore_bytes:+n, db_size:+n}, growth_per_day: {...},
               forecasts, days_until_disk_full, series},
  logs: {files, totals, trend, top_errors},
  ssl: {days_left, not_after, subject},
  postgres_connections: {p95, max, samples, source: "history"|"current"},
  security: {enabled, verdict: "off"|"no_data"|"findings"|"clear"|"unchecked", status, checked_ts, reason,
             findings: [{id, label, status, message, hint, alert, informational, period_checks, period_warnings}],
             rungs: [same fields, every current check in ladder order],
             earlier: [{id, label, message, warnings, checks, first_ts, last_ts}],
             ports: [{port, proto, addrs, public, process, pid, expected, verdict, …}],
             port_counts: {listening, reachable, exposed}   (the rung's metrics: reachable leaves Tailscale-only ports out),
             software: [{name, version, until, status, note}]},
  recommendations: [{severity, rule, text}] }
```
Report sections (`analytics.ALL_SECTIONS`, in document order): `summary`, `server`, `services`,
`latency`, `alerts`, `incidents`, `repository`, `logs`, `security`, `recommendations`;
`reports.default_sections` is `[summary, server, services, latency, alerts, repository, security,
recommendations]`. The **Security** section ("🛡️ Security", `analytics._security`) is built from
the Security component's current state (its heading says "checked <date time>", so a past-period
report is not misread) and the rung history of the range: `findings` are the rungs that warn now,
each with its message and hint; `earlier` the rungs that warned in the range but not now ("Earlier
in this period", with "warned in N of M checks" and the last time); a table of every check with
its status ("Note" for an informational one), result and "Warnings this period"; the open-ports
table (Port · Program · Address · Reachable · Expected · Verdict, new and exposed first, then by
port; at most `MAX_PORT_ROWS` = 60 rows in HTML and Markdown, all of them in JSON) and the software
table (Software · Version · Supported until · Status, with the note). Pills
(`generator.PORT_VERDICT_LABELS`): Expected, Local and Tailscale plain, Exposed and New warning;
Supported healthy, Ends soon plain, Ended and Affected warning, Unknown. One-line states: "🛡️ The
Security module is off." (`off`), "🛡️ No security check has run yet." (`no_data`), "✅ No security
findings." (`clear`), "🛡️ The last security check gave no result: <reason>" (`unchecked`: the
collector failed, or nothing could be checked on this server); Markdown writes the same words in
italics without the emoji.
Recommendation rules: disk full in < 60 days at current growth; SSL at or below the warning
threshold (at least 30 days); PostgreSQL connection usage p95 > 70 % of `max_connections`, read
from the `postgres.connections` history rows of the range — only a range ending within 10 minutes
of now may fall back to the current value ("is currently at"), a past range without history gets
no recommendation; any service < 99.5 % availability; REST p95 ≥ warning threshold; swap in use
for at least half the period; log errors trend up; no incidents ⇒ "No incidents this period".

HTML report: self‑contained and request‑free (inline CSS using the same tokens, system fonts —
no web fonts — and inline SVG charts from `svg.py`), prints cleanly to PDF (`@page` margins,
page‑break rules); served with `Content-Security-Policy: default-src 'none'; style-src
'unsafe-inline'; img-src data:` and every report file with `X-Content-Type-Options: nosniff`.
Markdown report mirrors the sections. JSON = the data dict. Every time in a report is in the
zone of `server.timezone` (`render(…, tz=)` puts it in the template context; the filters
`datetime` "07 Oct 2026 21:29 +0530", `clock` "21:29 +0530" and `date` "07 Oct 2026" use it), and
so are the chart ticks (`svg.time_ticks(…, tz=)`: day steps start at midnight in that zone), a
custom range's title ("2026-09-17 10:00 → 2026-09-24 10:00 UTC", `analytics.range_title`) and the
file name; `meta.timezone` is `{name, abbreviation}` of the zone at generation. Files:
`data/reports/sentinel-report-YYYYMMDD-HHMM-<range>.<ext>` (the time in `server.timezone`). At
most 200 report files and 500 MB are kept: generating another removes the oldest (soft‑deleted
included, via `Store.prune_reports`) and logs a `report` event. Generation is refused with
`ReportSpaceError` (a `ValueError`; the API answers 507, the CLI exits 4) when the data filesystem
has less than 1 GiB free. The weekly job runs as a single‑flight background task, outside the
scheduler's cycle lock; `schedule.weekly_due` reads `reports.weekly.weekday` and `hour` on the
wall clock of `server.timezone` (the kv day key too), and the weekly report's title is "Weekly
report · <host> · week ending 07 Oct 2026" (`fmt.day` in that zone).

Delivery (`generator.deliver_telegram_async` / `deliver_email_async`, used by the API, the CLI and
the weekly job): Telegram sends the file through every bot with `sendDocument` and the plain‑text
caption "📄 <title>" / "<period> · <from> → <to>" (each time with its zone,
"07 Oct 2026 21:29 +0530", `telegram_caption(report, tz)`); email attaches it (≤ 17 MB, §7) to
one message to every recipient, subject "[Sentinel] Report · <host> · <title>", its body's period
in the same zone. Both reach every destination,
whatever its `min_severity`, and answer `{ok, error, skipped?, results?}` ("Telegram is not
enabled", "Telegram has no bot yet: add a bot token and a chat id", "email is not enabled", "email
is not set up: …" or another not‑ready sentence of §7; demo `{ok: true, error: null, skipped:
"demo"}`). Each outcome is an events row: `notify` for a report sent or scheduled ("Report sent to
Telegram: <title>", "Report sent by email: <title>", "Report Telegram delivery scheduled: <title>",
"Report email delivery scheduled: <title>"), `notify.error` for one not sent, including a channel
that is off or not set up ("Report not sent to Telegram: <error>", "Report not sent by email:
<error>"), `notify.demo` in the demo ("Report (demo, not sent): <title>", "Report by email (demo,
not sent): <title>"). The weekly job delivers per `reports.weekly.send_telegram` / `send_email`.

---

## 9. HTTP API (`api/`)

All JSON. Timestamps are epoch **seconds** (numbers). Errors: `{"error": "message"}` with 4xx/5xx:
400 validation, unknown `Host` and patches that change `server.*` (`server.timezone` aside); 401
auth; 403 cross‑site write;
404 unknown id; 409/410 purged or missing report file; 415 non‑JSON body on a write; 429 busy
export or report generation, or too many failed sign‑ins (with `Retry-After`); 500 unexpected
error (a short message, details in the log only); 501 not in this build; 503 a sign‑in while the
stored login cannot be read; 504 discovery timeout; 507 no disk space for a report.
Every route is relative‑URL friendly (works under a reverse‑proxy prefix). `GET /healthz` →
`{"ok": true}`: the exact path `/healthz` (after any `root_path`) is the only one without auth,
`Host` check or cross‑site check (the page, `/static/**`, `/api/session` and `/api/login` skip only
the auth, middleware item 2).

| Method & path | Returns |
|---|---|
| GET `/api/session` | always open: `{login_required, authenticated, username, expires_ts}`; `login_required` false without a login and in the demo; with a login, a valid session gives `authenticated: true` with its username and absolute end, a valid `server.auth_token` or CLI token `authenticated: true` with `username: null`; `Cache-Control: no-store` |
| POST `/api/login` `{username, password, remember}` | open; 200 `{authenticated: true, username, expires_ts}` + the cookie; 401 "Wrong username or password." (an unknown username is verified against `auth.dummy_hash`, so both take the same time and give the same text); 429 "Too many attempts. Try again in 40 s." with `retry_after` and `Retry-After`; 400 "No login is set up on this Sentinel." (no login, and the demo); 400 "send the fields as text" (non‑string fields); 503 `LOGIN_UNREADABLE` |
| POST `/api/logout` | 200 `{authenticated: false}`; the session row deleted, the cookie cleared |
| POST `/api/account/password` `{current, new}` | needs the session cookie (a token alone: 401 "login required"); 200 `{ok: true}`, the config saved, every other session ended (the caller's moves to the new login); 400 "The current password is wrong." (counts as a failed attempt), a password‑rule message (§3), "The new password is the same as the current one.", "send the fields as text"; 429 as for login; 400/503 as for login; 401 "login required" when the login changed meanwhile (CLI) |
| GET `/api/meta` | `{version, hostname, setup_completed, persona, demo, modules: {id: bool}, dspace_modules_enabled, started_ts, theme, retention_days, system_interval, timezone, db_size_bytes}`; `timezone` (`tz.describe(cfg)`) is `{name, abbreviation, server_name, server_abbreviation}`: the zone of `server.timezone` in use ("Asia/Colombo", "+0530") and the server's own zone ("Etc/UTC", "UTC"; from `TZ`, the `/etc/localtime` link or `/etc/timezone`, else its abbreviation); with `server.timezone` empty both pairs name the server's zone |
| GET `/api/status` | `{ts, mood, headline, subline, hostname, server: {sample fields…, status, spark: {cpu:[…12], mem:[…], disk:[…], load:[…]}}, components: [ComponentHealth…], alerts_open: [Alert…], repository: {latest stats or null}, next_check_in_s}`; `mood` and `headline` (`health.headline(state, cfg, open_alerts)`) leave acknowledged alerts out: a component whose open alerts are all acknowledged counts as `ok` there (`health.acknowledged_components`; its own `status` in `components` is unchanged), and with nothing unacknowledged left the headline is "All systems healthy · 1 acknowledged" / "· 2 acknowledged"; the warning and critical headlines count only what is not acknowledged; a component that warns without any open alert still counts |
| GET `/api/components` / `/api/components/{id}` | list / one ComponentHealth + `latency_series_1h` + `recent_events` + `config_used` (masked; the config sections the component reads, `routes_status.CONFIG_SLICES`: `security` → `{ports, dspace}`) |
| GET `/api/history?metric=cpu|mem|disk|load|net|swap&range=1h|6h|24h|7d|30d` | `{metric, unit, bucket_s, points: [[ts, avg, max]], thresholds: {warning, critical}}` (`net` returns `series: {rx: [...], tx: [...]}`) |
| GET `/api/history/latency?check=backend.rest|backend.search|frontend.home|postgres.query|solr.search&range=` | `{check, points: [[ts, avg_ms, max_ms]], thresholds}` |
| GET `/api/availability?range=` | `{bucket_s, range, from, to, components: [{id, label, emoji, uptime_pct, downtime_s, outages, samples, buckets: [status…]}]}` A bucket without a check of its own takes the status of the last check before it (a check stands until the next one, at most twice the usual spacing or the component's interval); `null` only where nothing was checked. |
| GET `/api/alerts?status=open|resolved|all&limit=` | `[Alert…]` |
| POST `/api/alerts/{id}/ack` · `/unack` | Alert (an `alert` event `acked`/`unacked`, no `status` frame: the UI asks for `/api/status` right after, since the mood and headline change) |
| GET `/api/incidents` · `/api/incidents/{id}` | list / Incident with its alerts |
| GET `/api/reports?include_deleted=&limit=` · `/api/reports/options` | `[Report…]` (not deleted unless asked) / what the report panel offers: `{sections, default_sections, formats, ranges, telegram_available, email_available, weekly, purge_after_s}` (`sections`: `[{id, emoji, label}]` in document order, `{"id": "security", "emoji": "🛡️", "label": "Security"}` among them; `telegram_available`: Telegram on and a bot with token and chat id; `email_available`: email on and ready) |
| POST `/api/reports` `{range: "24h"|"7d"|"30d"|{from,to}, fmt, title?, sections?, send_telegram?, send_email?}` | Report (201) plus `telegram: {ok, error, skipped?}` / `email: {ok, error, skipped?, results?}` for the deliveries asked for (a failed delivery never fails the request; §8); a custom range must be finite, start within the last 366 days, span ≤ 366 days and end ≤ 5 min in the future (400); one generation at a time (429, `Retry-After: 5`); 507 below 1 GiB free |
| GET `/api/reports/{id}?meta=&download=` | the file (Content‑Disposition inline for html, with the CSP of §8) or the row |
| DELETE `/api/reports/{id}` · POST `/api/reports/{id}/restore` | soft delete (purged after 24 h) / undo |
| GET `/api/repository` | latest + 30‑day series for items/bitstreams/assetstore/db_size |
| GET `/api/events?limit=&component=&kind=` | `[{id, ts, kind, component, message, data}]`, newest first; messages in the UI's words ("Solr: Healthy → Warning — …", "Solr turned on", "Security turned off": `routes_config.MODULE_LABELS`, the names of More › Modules); notification rows (`notify`, `notify.error`, `notify.skipped`, `notify.demo`) carry `data.channel` (or `data.channels`) and `data.destinations` (§7) |
| GET `/api/config` | masked config plus the runtime flags `demo`, `notifiers.telegram.chats[].from_env` and `notifiers.email.password_from_env` (ignored when sent back) |
| PATCH `/api/config` `{partial}` | masked config after save (secrets: sending `"••••••"` keeps the stored value; a bot token by the entry's `id`; `chats` and `recipients` are replaced whole); 400 when the patch changes `server.*` but `server.timezone`, `server.login` included (`routes_config.reject_server_changes` compares `config.server_access()`: "server settings are set in config/sentinel.yaml; restart to apply"; sending the current values back is fine), names a `server.timezone` that cannot be loaded ("invalid configuration: server.timezone: unknown time zone 'Mars/Base': use an IANA name such as Asia/Colombo, or leave it empty for the server's zone"), re‑points PostgreSQL or the email login without the password, changes the email password or its login while `SENTINEL_SMTP_PASSWORD` is set, types an email password SMTP cannot send ("…: the email password has a character that cannot be sent: …"), masks a bot token without a known `id` ("enter the bot token for <name> again"), or breaks a notification rule (§3) |
| PATCH `/api/modules` `{postgres: true, …}` | `{modules}` (shortcut for config.modules; scheduler hot‑reloads; one `config` event "PostgreSQL turned on, Solr turned off") |
| POST `/api/actions/check-now` | runs the enabled collectors → `/api/status` payload. Coalesced while the service runs: a call during a running check‑now waits for it, a call within 10 s of the previous one returns the current status (a config reload or Clear history always allows a fresh run); the repository group only runs when its own interval is due. The start‑up cycle and `doctor` are never coalesced |
| POST `/api/actions/telegram-test` `{id?}` | `{ok, bot_name, error, results: [{id, label, chat_id, ok, error, bot_name}]}` for every bot or the one `id` names (400 "no Telegram bot with id '<id>' is configured"); sent whether or not Telegram is on; in demo mode `{ok: false, bot_name: null, error: "demo mode: nothing is sent to Telegram", skipped: "demo", results: []}` plus a `notify.demo` event carrying the message |
| POST `/api/actions/email-test` `{address?}` | `{ok, error, results: [{address, label, ok, error}]}`: one test email to every recipient or the one named (any case; 400 "no email recipient '<address>' is configured"), sent whether or not email is on; not ready: `{ok: false, error: "email is not set up: add the Gmail address, app password and at least one recipient", results: []}`, or another not‑ready sentence of §7 ("the SMTP username is not an email address: …", "the email password has a character that cannot be sent: …"), nothing sent; in demo mode `{ok: false, error: "demo mode: nothing is sent by email", skipped: "demo", results: []}` plus a `notify.demo` event with the subject and both bodies |
| POST `/api/actions/discover` | DiscoveryResult `{findings, suggested_config, detected_persona, sources, properties, ports, services}`, secrets masked (§6); 504 after 45 s |
| POST `/api/actions/setup` `{persona, config_patch}` | applies persona preset (modules and ports) + patch, sets setup.completed, returns meta; masked discovery values restored as in §6; the same 400s as PATCH `/api/config` |
| POST `/api/actions/clear-history` | wipes samples/checks/history/alerts/incidents/events (keeps config, reports and sign‑in sessions) |
| GET `/api/export?range=` | JSON dump (default 30 d): `meta` first, then samples, checks, component_history, alerts, incidents, repository, events; every row, streamed table by table (`Cache-Control: no-store`); one export at a time (429, `Retry-After: 30`) |
| GET `/api/stream` | SSE: `event: status` (same as /api/status; at most every 5 s), `event: component` (`{id, from, to, component}`), `event: alert`, `event: incident`, `event: report`, `event: heartbeat` (30 s); the stream ends when the client disconnects or the server shuts down (within 1 s of SIGTERM/SIGINT; uvicorn's graceful shutdown is capped at 5 s) |

Middleware (`SentinelMiddleware`), in this order:

1. **Host allowlist.** The `Host` header must name this machine: IP literals, `localhost`, the
   machine's hostname, `server.hostname`, `server.host`, `ssl.host`, the hosts of
   `dspace.public_url/ui_url/server_url` and `nginx.http_url/https_url`, and
   `server.allowed_hosts` (`"*"` allows any). Anything else answers 400 "unknown host '<name>':
   add it to server.allowed_hosts in config/sentinel.yaml if Sentinel is reached under that
   name" (DNS‑rebinding protection).
2. **Auth** (`AuthService` on `app.state.auth`, `api/routes_auth.py`; first `refresh()` re‑reads a
   `server.login` changed in the file). **With the login** (`login_active`): `OPEN_PATHS` (`/`,
   `/index.html`, `/favicon.svg`, `/api/session`, `/api/login`) and `/static/**` are open; every
   other request needs a valid session cookie, `server.auth_token` (`Authorization: Basic` with any
   user name, or `Authorization: Bearer <token>`) or the local CLI token (Bearer); otherwise 401
   `{"error": "login required"}` **without** `WWW-Authenticate`, so no browser pop‑up. **Only a
   token**: everything but `/api/session` and `/api/login` needs it (Basic or Bearer) or the CLI
   token; a 401 "authentication required" carries `WWW-Authenticate: Basic realm="DSpace
   Sentinel"` and the browser prompt is the login UI. **Neither**: open.
3. **Cross‑site writes.** POST/PUT/PATCH/DELETE answer 403 "cross-site request refused: open
   Sentinel's own page to make changes" when `Sec-Fetch-Site` is `cross-site`/`same-site` or an
   `Origin` (including `null`) matches neither `Host` nor `X-Forwarded-Host` (a `Host` without a
   port matches any origin port), and 415 "unsupported content type: send application/json (or
   no body)" for any other `Content-Type`. Requiring JSON (instead of a custom header from
   `api.js`) keeps HTML forms out without a preflight while curl and the CLI keep working.
4. Cache headers for static files.

`/healthz` (exact path) skips 1–3. `create_app` raises `UnsafeBindError` (`check_bind`; `serve`
exits 4) for a non‑loopback `server.host` with neither the login nor `auth_token`, unless
`server.allow_unauthenticated` or `SENTINEL_ALLOW_OPEN=1`: "refusing to serve the API without
authentication on 0.0.0.0:9765: create the dashboard login with ./sentinel.sh set-password, set
server.auth_token (or SENTINEL_AUTH_TOKEN), bind to 127.0.0.1 and publish Sentinel through the
nginx recipe, or opt in with server.allow_unauthenticated: true or SENTINEL_ALLOW_OPEN=1".

**Sessions** (`auth.Sessions`, the `sessions` table of §5). A sign‑in creates
`secrets.token_urlsafe(32)`; the store keeps only its SHA‑256 and a fingerprint of the login.
Lifetimes (`auth.IDLE_S`, `MAX_AGE_S`, `REMEMBER_S`): idle 12 h and at most 7 days; with
`remember` idle and absolute 30 days; `last_seen_ts` is written at most once a minute. Cookie
`sentinel_session`: `HttpOnly`, `SameSite=Strict`, `Path` = the app's root path (`/` by default),
`Max-Age` = the time to the absolute end (`expires_ts`; the idle limit is enforced server‑side),
`Secure` when the request's scheme is https (uvicorn's proxy headers, trusted from a proxy on
127.0.0.1). A changed password (API: every other session ends; CLI: all) or a removed login ends the
sessions at their next request. **Brute force** (`auth.Throttle`, in memory): per client IP, after
5 failures within 15 minutes every attempt must wait 2^(failures−5) s (at most 300 s) after the
last failure, 429 with `retry_after` and `Retry-After`; a success clears the IP; per username, 20
failures within 15 minutes from any IPs lock it for 15 minutes. **Events** (kind `auth`, never a
password): "Signed in: admin (192.0.2.10)", "Sign-in failed for 'admin' (192.0.2.10)" ("… for an
invalid username (…)" when the typed name breaks the username rules), "Signed out: admin",
"Sign-in paused for 192.0.2.10 after 5 failed attempts", "Password changed" (API, or a CLI change
noticed), "Login removed". **Local CLI token** (`auth.CliToken`): the lifespan writes a new
`secrets.token_urlsafe(32)` to `<data dir>/api-token` (mode 0600, atomically) at every start;
`Authorization: Bearer <it>` counts as signed in (the token in the file is accepted too, so a
second `serve` that failed to bind locks nobody out). `status` uses it (§10); `doctor`, `report`
and `discover` run in‑process and need no login. Demo mode never asks for a login.

Static: `/` → `index.html`; `/static/*` → files (cache‑control: no‑cache for html, 1 h for assets
with `?v=<version>` query).

---

## 10. CLI and `sentinel.sh`

`sentinel.sh` (bash, `set -euo pipefail`, shellcheck‑clean) lives at repo root. It resolves its
own real path (`readlink -f`, so a symlink such as `/usr/local/bin/sentinel` works), exports
`PYTHONPATH=<checkout>` so every `python -m dspace_sentinel` call works from any directory (cron,
`ansible -a`, an absolute path), and:

* `install` — refuses to run under `sudo` (the files would belong to root; it prints the command
  to run as the normal user). Picks the first of `python3.13 … python3.10, python3, python` (or
  only `PYTHON=…`) that is ≥ 3.10 and has both `venv` and `ensurepip` (else the apt hint
  `sudo apt install python3-venv`), creates `.venv`, `pip install -r requirements.txt`, copies
  `config/sentinel.example.yaml → config/sentinel.yaml` (mode 0600) if absent, checks that the
  config loads (exit 4 otherwise), then **the dashboard login**: when the config has none and
  stdin is a terminal, step "Create the dashboard login" runs `set-password` (a failure only warns
  "No dashboard login yet: run ./sentinel.sh set-password"); without a terminal (CI, Ansible) it
  never prompts and prints that warning; `--no-login` skips the step. Then it runs `discover` and
  prints findings, and prints next steps with the real bind address (`server_facts` prints URL,
  host, port, bind `local|login|token|open|refused` and login `yes|no`): "Sentinel binds to
  HOST:PORT (this machine only)"; with a login on another address "Sentinel binds to HOST:PORT and
  asks for the dashboard login. Put the nginx HTTPS recipe (deploy/nginx/sentinel.conf.example) in
  front so the password never travels in plain text."; a non‑loopback bind with neither a login
  nor a token gets "… without a login or an auth token, so it will refuse to start." with the
  hints "Create the dashboard login first:  ./sentinel.sh set-password", the
  `config/sentinel.env` token line and the nginx line; a running Sentinel gets "Running already:
  ./sentinel.sh restart". Flags: `--service` (install a **systemd system unit** running as the
  invoking user; uses sudo for that step only), `--user-service` (systemd `--user` unit, no
  sudo, with `loginctl enable-linger`), `--port N`, `--host H` (written into the YAML), `--dev`,
  `--no-discover`, `--no-login`. `--service` stops before installing a unit whose bind would be
  refused.
* `install-service [--user] [--print] [--no-start] [--port N] [--host H] [--force]` — (re)installs
  only the unit. It refuses to replace a `dspace-sentinel` unit whose `WorkingDirectory` is
  another checkout (`--force` takes it over), stops a leftover `start` process first, refuses a
  port already in use, restarts an already active unit so a new unit or port applies, waits up
  to 15 s for `/healthz` ("State: active · URL") and prints the last 20 journal lines (exit 4)
  when the unit does not come up. Run as root under `sudo`, it checks as the unit's user that the
  data and config directories are writable and the config readable (exit 4 with a `chown` hint);
  from a root shell without `SUDO_USER` it warns that the unit will run as root.
* `start|stop|restart|status|logs` — when **this checkout's** unit is installed (its
  `WorkingDirectory` is this checkout) use `systemctl`, first stopping a leftover pid‑file
  process (`status` reports one as `background_pid`); otherwise manage a background process with
  `data/sentinel.pid`, logs to `data/sentinel.log` (another checkout's unit is left alone, with a
  one‑time log line). `start` refuses a port that already answers ("… already in use by another
  Sentinel" or "by another program", exit 4), requires the answering listener to be its own
  child (it waits up to `--timeout` seconds, default 20, for `/healthz`), and says when its
  running process does not answer the configured URL (exit 1, restart hint). `status` labels an answer from another instance (`other_instance`) instead of
  showing its health, and plain `stop`/`status` mention a live `start --demo` process. With
  `--demo` these commands manage the background demo (`data/demo/`). `logs [-f] [-n N]`.
* `run` / `serve` — foreground. `demo` — foreground with `SENTINEL_DEMO=1` on port 9765.
* `doctor [--json] [--ascii] [--demo] [--width N]` — one‑shot check of everything enabled,
  printed as the boxed dashboard (Unicode box drawing and ✅/⚠️/❌ marks; `--ascii`, or a stdout
  that cannot encode them, gives plain ASCII with OK/!!/XX; SERVER / DSPACE STACK / NETWORK /
  ALERTS sections like the README; every alert‑message line gets its own row). NETWORK lists
  nginx, SSL certificate, Ports and Security (`cli.NETWORK_IDS`); the Security row shows its
  reason when it is not ok, then its eight checks with their marks, `ports ✅ · firewall ✅ · ssh ✅ ·
  web ✅ · updates ✅ · software ✅ · files ✅ · sentinel ✅` (separate checks, not a chain, so joined
  with " · ", " - " in ASCII, like the ports); with none of the four on, the section reads "nginx /
  SSL / ports / security modules are off". The top row's time is in `server.timezone` and names
  it ("demo-dspace · 2026-09-29 19:37 UTC", `fmt.date_time(now, tz, zone=True)`). The headline and
  the exit code follow the mood of `/api/status`, acknowledged alerts left out (exit 0 when
  everything open is acknowledged); the ALERTS section still lists every open alert, an
  acknowledged one with " · acked". `--json` prints the `/api/status` payload.
* `report [--range 24h|7d|30d] [--format html|md|json] [--title T] [--telegram] [--email] [--json]`
  — generate & print path; `--telegram` sends the file through every Telegram bot and `--email` to
  every email recipient (both may be given; one failing does not stop the other). Each prints
  "Telegram: sent" / "Email: sent", "…: demo mode: logged, not sent" or, on stderr with exit 4,
  "Email: not sent: <reason>" ("email is not enabled", "Telegram is not enabled", …); `--json`
  adds a `telegram` and an `email` object.
* `discover [--apply] [--json]` — print findings and the suggested config (secrets masked, §6);
  `--apply` writes it (with the real values, in‑process).
* `telegram-test [--to ID_OR_CHAT_ID] [--json]` — `getMe`, then the test message, through every
  Telegram bot (or the one whose entry id, else chat id, `--to` names); one line per bot, "  ✅
  Danindu · @danindu_sentinel_bot · delivered" / "  ❌ Team · … · chat not found — add the bot to the
  group first" (OK/XX in ASCII), then "Connected to Telegram[ as @bot] — check your chat(s) for the
  test message." (exit 0) or "error: Telegram test failed: <summary>" (exit 4). Without a bot:
  "error: Telegram is not configured: add a bot (its token from @BotFather and your chat id) in More ›
  Telegram, under notifiers.telegram.chats in config/sentinel.yaml, or with SENTINEL_TELEGRAM_TOKEN +
  SENTINEL_TELEGRAM_CHAT_ID." (exit 4); an unknown `--to` lists the configured ones. `--json` prints
  `{ok, bot_name, error, results}`.
* `set-password [--user NAME] [--password-stdin] [--remove] [--yes]` — creates or changes the
  dashboard login (§3) through the file layer (`apply_login` + `save_config`, mode 0600; the demo
  is never involved). Interactive by default: "Username [admin]: " (the current username when
  it follows the rules, else `admin`; Enter keeps it), then "Password: " and "Repeat the password: "
  without echo (`getpass`), three tries each ("The passwords do not match.", the rule messages of
  §3). `--user NAME` sets the username; `--password-stdin` reads one line for automation (no
  questions). Prints "Login saved for 'admin'. Sessions that were signed in have been signed out."
  (exit 0); a running server applies it within seconds (§3). Without a terminal and without
  `--password-stdin`: "set-password asks for the password on a terminal; use --password-stdin to
  read it from standard input" (exit 4). `--remove` asks "Remove the login for 'admin'? The
  dashboard will not ask for a password. [y/N] " unless `--yes` and prints "Login removed: the
  dashboard no longer asks for a password." / "Kept the login." / "No login is set: the dashboard
  does not ask for one."; it is refused (exit 4) while `check_bind` would then refuse the bind:
  "not removing the login: Sentinel listens on <host> and without a login or server.auth_token it
  would refuse to start; set server.auth_token first, or listen on 127.0.0.1"; without a terminal
  it needs `--yes` ("--remove needs --yes when not run interactively", exit 4).
* `hash-password [--password-stdin]` — prints only the hash of a password (asked twice without
  echo, or one line of standard input; the length rules apply), for the Ansible role's
  `dspace_sentinel_login_password_hash`. Never touches the config.
* `email-test [--to ADDRESS] [--json]` — one test email ("[Sentinel] Test email · <host>") to every
  recipient (or the one `--to` names); one line per recipient ("  ✅ On call (ops@example.org) ·
  sent", "  ❌ boss@example.org · refused by the mail server: 550 …"; a refused login or connection is
  a single "error: Email test failed: <sentence>"), then "Test email sent through <host>:<port> —
  check the inbox for "[Sentinel] Test email · <host>"." Exit 0 when every recipient got it, 4 when
  any did not or email is not set up ("error: email is not set up: add the Gmail address, app
  password and at least one recipient (More › Email in the UI or notifiers.email in
  config/sentinel.yaml; the app password can also come from SENTINEL_SMTP_PASSWORD in
  config/sentinel.env).") or cannot send ("error: Email test failed: <another not‑ready sentence of
  §7>", before anything is sent); never 3. `--json` prints `{ok, error, results}`.
* `update [--no-pip] [--no-restart]` (git pull --ff-only + pip install, then restart the unit or
  the running background process), `uninstall [--purge] [--yes] [--force]` (stop, remove this
  checkout's unit and pid file, keep data; `--purge` deletes data and config; another checkout's
  unit is left alone unless `--force`, and a failing `systemctl disable --now` keeps the unit
  and exits 4; uninstalling a user unit prints the `loginctl disable-linger` hint),
  `version [-v]`, `help`.

Every command except the demo first exports `config/sentinel.env` (§3). Everything except
`install` is `exec .venv/bin/python -m dspace_sentinel "$@"` (`set-password` and `hash-password`
included; `usage()` lists them under *Login*); the Python CLI (`cli.py`, argparse, sub‑commands
above) does the work so the shell stays thin. `status` asks the running server with the local CLI
token from `<data dir>/api-token` (§9), then with `server.auth_token`, so it shows health with a
login set; `doctor`, `report` and `discover` run in‑process and need no login.

**Exit codes** (shared by `sentinel.sh` and the Python CLI; 0/1/2 are reserved for health so a
wrapper can tell "Solr is down" from "the check did not run"):

| Code | Meaning |
|---|---|
| 0 | ok |
| 1 | warnings (`doctor`), not running (`status`, `logs` without a log) |
| 2 | critical (`doctor`) |
| 3 | not available in this build |
| 4 | error or missing prerequisite: the command could not run (not installed yet, no suitable Python, incomplete venv, invalid config, refused bind, busy port, unexpected error, `report --telegram`/`--email` not sent, a failed `telegram-test`/`email-test`, a `set-password`/`hash-password` without a password or refused) |
| 64 | usage error: unknown command or option (sysexits `EX_USAGE`; argparse's own 2 is never used) |
| 130 | interrupted |

---

## 11. Deployment

* `deploy/systemd/dspace-sentinel.service` (templated by the installer: user, WorkingDirectory,
  ExecStart=`"<repo>/.venv/bin/python" -m dspace_sentinel serve`). `Environment=`, `ExecStart=`
  and `ReadWritePaths=` values are double‑quoted and `%` is written as `%%`, so checkout paths
  with spaces work; paths with a double quote, a backslash or a control character are refused
  (exit 4). `EnvironmentFile=-<config dir>/sentinel.env`; `[Unit]` `StartLimitIntervalSec=120`
  and `StartLimitBurst=5` (after five failed starts in two minutes the unit stays failed).
  Hardening: `NoNewPrivileges=yes`, `ProtectSystem=strict`,
  `ReadWritePaths="<repo>/data" "<repo>/config"`, `ProtectHome=read-only`, `PrivateTmp=yes`, plus
  the resource limits from §0. A `--user` unit drops `User=`/`Group=` and the namespace‑based
  sandbox directives.
* `deploy/nginx/sentinel.conf.example`: `location /sentinel/ { proxy_pass http://127.0.0.1:9765/; }`
  with `auth_basic`, SSE‑friendly settings (`proxy_buffering off`, `proxy_read_timeout 1h`) and
  `proxy_set_header Host $host` (accepted once `dspace.public_url` names the repository host; a
  dedicated host name goes into `server.allowed_hosts`).
* Ansible role `dspace_sentinel`: vars for repo/version, port, host, modules (every module on,
  `security` included), collector intervals (`security_interval: 900`), report sections
  (`security` among the defaults), thresholds,
  Telegram (up to five bots in `dspace_sentinel_telegram_bots`, each token vaulted; the old
  `dspace_sentinel_telegram.chat_id` + `dspace_sentinel_telegram_bot_token` still render as one bot),
  email (`dspace_sentinel_email`, `dspace_sentinel_email_recipients`, the vaulted
  `dspace_sentinel_email_password`; `reports.weekly.send_email`), DSpace paths, `dspace_sentinel_allowed_hosts` and
  `dspace_sentinel_allow_unauthenticated` (`server.*`; while the nginx snippet is enabled the
  role adds `inventory_hostname` to `allowed_hosts`), the dashboard login
  (`dspace_sentinel_login_user`, default `admin`, and `dspace_sentinel_login_password_hash`,
  default `""`, vaulted, made with `./sentinel.sh hash-password`, rendered into `server.login`;
  with `dspace_sentinel_manage_config: true` every run rewrites it), and `dspace_sentinel_env`
  (written to `config/sentinel.env`, 0600, when not empty); tasks: pre‑flight checks
  (`tasks/checks.yml`: at most 5 bots and 5 recipients, known keys only, quoted times, no
  bots/recipients/password inside the section dicts, a login user of 1–64 `[A-Za-z0-9._@-]` and a
  hash that starts with `scrypt$` or `pbkdf2_sha256$` (never the password itself), and a
  non‑loopback `dspace_sentinel_host` only with a login hash, `dspace_sentinel_auth_token`,
  `dspace_sentinel_allow_unauthenticated: true` or `SENTINEL_AUTH_TOKEN` / `SENTINEL_ALLOW_OPEN=1`
  in `dspace_sentinel_env`; the play stops before any change), apt deps (git, python3,
  python3-venv, acl
  — `become_user` to the service user needs `setfacl`), user, clone, venv + pip, config from
  template, systemd unit (with the same `EnvironmentFile=`, start limits and quoting), handlers
  restart; optional nginx snippet. `playbook.yml` targets group `dspace_servers`; settings go in
  `group_vars/dspace_servers/main.yml` and secrets in `group_vars/dspace_servers/vault.yml`.
  Idempotent; CI runs `ansible-lint --profile production`, `--syntax-check` and
  `tests/test_ansible_role.py` under real Ansible (vault, the checks, the rendered config) (gating).

---

## 12. Web UI

### 12.1 Design read

> **DESKTOP‑FIRST. This is a web dashboard for laptops and monitors, not a mobile app.**
> Design, build and verify at 1280, 1440 and 1920 px wide first. Narrow viewports only get a
> responsive fallback (§12.3). If any other instruction says "phone‑first", "bottom tab bar" or
> "bottom sheet", this section supersedes it.

Operator utility for DSpace sysadmins and repository managers, kept open in a browser tab on a
work machine and checked several times a day, often side by side with a terminal. Friendly,
calm, unmistakably *not* Grafana. The brief pins the visual language exactly: cream background,
one card radius, zero shadows, 1 px borders, Space Grotesk numbers + Inter body, emoji icons,
pastel accents with dark ink. Two signatures, everything else quiet:

1. **The mood accent.** The whole app is tinted by current health: mint when healthy, butter
   on warnings, coral on a critical incident, sky before the first check and while the data is
   out of date (§12.8). The accent recolours
   the hero band, the "+" button, the active tab pill, toggles and chips. Pastels are fixed in
   both themes, so text on an accent is always dark ink.
2. **Ladders.** Every service is shown as its dependency chain — systemd → TCP → connect →
   SELECT 1 — as vertical rungs joined by a 1 px line, each with its own ✅/⚠️/❌ and timing.
   The first failing rung is the reason; rungs below it read "not reached" (a rung that is only
   slow does not stop the ones after it).

### 12.2 Tokens (`css/tokens.css`) — the only place raw values live

```css
:root {
  color-scheme: light dark;
  /* spacing: six steps */
  --s1: 4px; --s2: 8px; --s3: 16px; --s4: 24px; --s5: 32px; --s6: 48px;
  /* radius: one card radius everywhere, panels/dialogs a touch larger, pills round */
  --r-card: 22px; --r-panel: 28px; --r-pill: 999px;
  --r-inner: 12px;   /* squares and insets inside a tile: emoji squares, chart plot, tooltip, report preview */
  --r-mark: 6px;     /* small marks: kbd keys, availability cells, text-link focus */
  /* type */
  --font-display: "Space Grotesk", ui-sans-serif, system-ui, sans-serif;   /* numbers + headings, 600/700 */
  --font-body: "Inter", ui-sans-serif, system-ui, sans-serif;              /* body 400/500 */
  --fs-11: 0.6875rem;  /* bottom-bar labels in the phone fallback only */
  --fs-12: 0.75rem; --fs-13: 0.8125rem; --fs-14: 0.875rem; --fs-15: 0.9375rem; --fs-16: 1rem; --fs-18: 1.125rem;
  --fs-20: 1.25rem; --fs-22: 1.375rem; --fs-28: 1.75rem; --fs-40: 2.5rem;
  /* neutrals (flip in dark) */
  --bg: light-dark(#F6F2E8, #14130F);          /* cream / warm black */
  --surface: light-dark(#FFFDF8, #1D1B16);     /* tile fill */
  --surface-2: light-dark(#F1ECDF, #26231D);   /* inset (rung rail, chip idle) */
  --line: light-dark(rgba(23,21,15,.12), rgba(244,239,228,.12));
  --ink: light-dark(#17150F, #F4EFE4);
  --ink-2: light-dark(#5B564A, #B8B1A2);
  --ink-3: light-dark(#8A8477, #857E70);       /* ≥3:1 only: captions, axis ticks */
  --press: rgba(23,21,15,.08);                 /* 8 % ink tint on press (light) */
  /* pastels: fixed, never themed; always paired with --on-accent */
  --mint: #BFEFD8; --butter: #FFE58A; --coral: #FFB3A7; --sky: #C9DDFF; --lilac: #DCD0FF;
  --on-accent: #17150F;
  --on-accent-2: rgba(23,21,15,.72);       /* secondary text on the accent (Pulse subline) */
  --on-accent-line: rgba(23,21,15,.32);    /* a ghost button's border on the accent */
  --on-accent-wash: rgba(23,21,15,.08);    /* hover on the accent, the "Demo data" pill */
  --on-accent-inset: rgba(255,253,248,.55); /* the emoji square on the active nav pill */
  --scrim: rgba(23,21,15,.32);             /* behind dialogs and the report panel */
  --paper: #FFFDF8;                        /* the report preview frame: reports are light-only documents */
  --accent: var(--sky);                        /* app.js sets to mint/butter/coral by mood */
  /* status: text (≥4.5:1 on surface) and fills (≥3:1 on surface, used for dots/meters/marks) */
  --ok-ink: light-dark(#0F6B3F, #7ED9A8);  --ok-fill: light-dark(#178F58, #1E9E63);
  --warn-ink: light-dark(#7A4E00, #FFD666); --warn-fill: light-dark(#A86F00, #E39B00);
  --crit-ink: light-dark(#A1261B, #FF9B8C); --crit-fill: #D4432E;
  --unknown-fill: #8A8477;
  /* charts: validated with the dataviz palette validator on both surfaces */
  --chart-1: light-dark(#2F6FD6, #3987E5); --chart-2: light-dark(#E06B2D, #D95926);
  --chart-3: light-dark(#189B72, #199E70); --chart-grid: var(--line);
  /* motion */
  --dur: 160ms; --ease: cubic-bezier(.2,.7,.2,1);
  --dur-panel: 200ms;                      /* the report panel slides in 200 ms ease-out (§12.6) */
}
:root[data-theme="light"] { color-scheme: light; }  :root[data-theme="dark"] { color-scheme: dark; --press: rgba(244,239,228,.10); }
@media (prefers-reduced-motion: reduce) { :root { --dur: 0ms; --dur-panel: 0ms; } }
```
Provide a `@supports not (color: light-dark(#000,#fff))` fallback block using
`@media (prefers-color-scheme: dark)` + `[data-theme]` selectors (see the retrieved dark‑mode
guide). Body background is explicit. Zero `box-shadow` anywhere. No gradients. No ripple.
Tiles sit `--s3` (16 px) apart; tables, rungs and chips use the six spacing steps.
`tests/test_static_tokens.py` enforces the one‑token‑file rule: a raw colour, duration, radius or
type size outside `tokens.css` fails, as do gaps that are not spacing tokens, 4–15 px paddings or
margins and any `var(--x)` not defined in `tokens.css` (relative `0.95em` monospace and 1–3 px
optical nudges are allowed).

### 12.3 Layout and navigation (desktop‑first)

* **Shell = left sidebar + content area.** Sidebar 248 px, full height, `--surface` fill with a
  1 px right border. Top: brand lock‑up "🛰️ **DSpace Sentinel**" with the hostname under it.
  Then the **four tabs as vertical nav items** (emoji in a 36 px `--surface-2` square + label,
  Inter 500 15): 🏠 Home · 📚 Stack (renames to ⚙️ Services when no DSpace module is on) ·
  📈 History · ⚙️ More. The **centre "+" sits between Stack and History** as a full‑width
  accent button "＋ New report" (radius 22, dark ink) — the one filled CTA of the shell.
  Active tab = accent pill behind the item. Sidebar bottom: mood line "● All healthy · checked
  8 s ago", *↻ Check now* ghost button (hidden while the Home tab shows: its Pulse band carries
  the one Check now), theme switch (System/Light/Dark segmented chips), version. While a dashboard
  login is set (`server.login`) and this browser is signed in, the foot also shows the signed‑in
  username ("👤 admin", with a visually hidden "Signed in as") and a ghost *Sign out* button
  (`#account-line`, `#account-name`, `#sign-out`); they are hidden in the 76 px icon rail and the
  phone bar, where Sign out is on More › Account. While the data
  is out of date the mood line shows ⚠️ "Sentinel unreachable · last check 11:34" (a ⚠️ alone in
  the icon rail, the text kept for screen readers and as a tooltip). Between 900 and 1200 px the
  sidebar collapses to a 76 px icon rail (labels become
  tooltips, "+" becomes a round accent button); below 900 px it becomes a bottom bar with the
  "+" in the middle and panels become bottom sheets — a fallback, not the design target.
* **Content area.** `--bg` cream, padding 32 px, max‑width 1440 px (centred beyond that). Each
  tab starts with a page header row: title (Space Grotesk 28) on the left, contextual controls
  on the right (History: the range chips; Home: the countdown "Next check in 7 s" / "Checking…"
  only, since the age is already in the Pulse subline and the sidebar; Stack: "5 of 5 healthy",
  empty when no component is monitored, "Sentinel unreachable · last known: N of M healthy"
  while stale). Below it a **dashboard grid**: `display: grid; grid-template-columns: repeat(12, 1fr);
  gap: 16px`. Stat tiles span 3 columns, list/ladder tiles 6, charts 6 (two per row) or 12 for
  the network chart, the Pulse band 12. Tile grids inside a section use
  `repeat(auto-fill, minmax(240px, 1fr))`. Use the width: Home is Pulse band (12) → Server
  vitals 4 × (3) → left column (6) Stack list + right column (6) Alerts/Repository → footer. A
  half‑row whose partner is hidden (its module is off) spans the full 12 columns.
* **Depth is capped at two:** a tab, then *either* a pushed **sub‑view** inside the content
  area (header row becomes "‹ Stack · PostgreSQL", content replaced, sidebar stays) *or* a
  **panel**: a right‑side drawer 520 px wide (`<dialog closedby="any">`, radius 28 on its left
  corners, full height, 32 px padding, close button top‑right, Esc closes). The Report form is
  the one panel (it is reachable from every tab); the settings forms (Alert thresholds, Telegram,
  Email, DSpace connection, Reports, Appearance, Account) are sub‑views under More (§12.6). Confirm dialogs
  are small centred `<dialog>`s (radius 28). Nothing deeper. Hash router: `#/home`, `#/stack`, `#/stack/postgres`,
  `#/history`, `#/history/incidents/24`, `#/history/reports/12`, `#/more`,
  `#/more/alerts|telegram|email|connection|appearance|reports|about|modules`, `#/more/account`
  (only while a login is set; otherwise it goes to `#/home`), `#/server`. Panels append
  `?panel=report`. Browser Back works (history entries); covered content gets `inert`.
* **Tabs keep their scroll position** (Map tab → scrollTop, restored on switch). Sidebar never scrolls away.
* **Hover is a first‑class state**: tiles that navigate get `--surface-2` on hover and a
  pointer cursor; charts show crosshair tooltips; nav items show the accent wash at 50 % on hover.
* **Keyboard**: `1`–`4` switch tabs, `n` opens New report, `r` runs Check now, `Esc` closes
  panels/sub‑views, `?` shows a shortcuts dialog. Never captured while typing in a field, and
  inert while the onboarding gate, the sign‑in screen or the boot error shows.
* Desktop density: lists that are naturally tabular (alert history, events, ports, Solr cores,
  extra mounts) render as **tables inside a tile** (1 px row separators, tabular numerals,
  sortable header where useful) instead of stacked cards.
* The main job: **click "＋ New report", the range is preselected, click "Generate report".** Two clicks.

### 12.4 Every tile is the same recipe (`ui.tile`)

Surface fill · 1 px `--line` border · radius 22 · padding 16 · emoji on the left in a 40 px
`--surface-2` rounded square · bold title (Inter 500 16) with a small subtitle (Inter 13 `--ink-2`)
· on the right a value (Space Grotesk 600 18–22) or a status pill or a chevron ›. Tappable tiles
get `cursor: pointer`, `:hover` background `--surface-2`, `:active` an 8 % ink tint overlay, and
a 2 px `--ink` focus ring (offset 2). Home, Stack, History and More all use it, so a new screen
looks finished by default. Stat tiles (server vitals) are the same tile with the value large and
a 12‑point sparkline under it. A block tile (`tile--block`) puts its content under the title row:
a key/value list (`.kv`, a `<dl>` without margins of its own, in `.kv-wrap`), a meter list, a chip
list or a table, each `--s2` under the title row, so they all start and end at the same distance
(the Rendering, Connection used, Files watched, Host, About and report‑facts lists).

### 12.5 Screens

**Sign‑in (gate, only with `server.login`)** — `app.js` asks `GET /api/session` first (a 404 from
an older server = no login). Without a session a full page fills the gate (`#gate`, the product's
look) and nothing else loads (no `/api/meta`, no stream, no polling): 🛰️, heading "Sign in to
DSpace Sentinel", *Username* (autocomplete `username`) and *Password* (`current-password`),
checkbox "Keep me signed in for 30 days", filled full‑width button *Sign in* ("Signing in…" while
it posts `POST /api/login`). The server's sentence goes under the form (`#signin-error`: "Wrong
username or password.", "The login in server.login could not be read: run ./sentinel.sh
set-password") and the password field is emptied; a 429 disables the button with the countdown
"Try again in 40 s" (from `retry_after`), then it reads *Sign in* again; empty fields say "Enter
your username." / "Enter your password." without posting; a 400 (the login was removed meanwhile)
asks `/api/session` again and goes on without a login. After signing in the boot goes on (the
onboarding gate while setup is incomplete). A 401 from any other call closes the stream, stops
polling, closes any open panel or dialog and shows the same page with "Your session ended. Sign in
again." above the form and the last username filled in; signing in renders the current route
again and reconnects the stream. When no login was known at boot, a 401 asks `/api/session` once
more (a login set with `set-password` while the page is open); a 401 from `server.auth_token`
alone (no login) is left to the browser's Basic prompt. Shortcuts, panels and snackbars are off
while the page shows. The demo never shows it.

**Onboarding (gate, first run)** — step 1 "What runs on this server?" four persona tiles
(🧩 Full DSpace stack · ☕ Backend only · 🖥️ Frontend only · 💻 Just the server), single select,
caption "You can change everything later.", button *Continue*. Step 2 "We looked around" —
discovery findings as tiles with pre‑set toggles (DB host, Solr URL, services, ports found) and a
*What we found (N)* disclosure with discovery's findings, button *Start monitoring* (→ `POST
/api/actions/setup`). Skippable. The chosen option's modules are pre‑selected and discovery's
evidence can switch more on. nginx and SSL are pre‑selected only when a discovery that ran without
an error found a web server (port 80 or 443, or an active or failed nginx/apache2 unit) for nginx
and HTTPS (port 443 or an https public URL) for SSL; without that evidence their rows stay,
switched off, reading "not found on this machine — …", and *Skip for now* sends them as off too
(`config_patch.modules`). The DSpace parts the option names (PostgreSQL, Solr, backend, frontend)
stay pre‑selected without evidence ("not found" may mean "down right now"); when discovery failed,
the option's preset stands. 🛡️ Security ("Open ports, firewall, SSH, updates and software support:
read-only.", the row after Ports) looks at this server itself, so every option pre‑selects it with
the evidence "this server" ("this server — Open ports, firewall, SSH, updates and software support:
read-only."); that evidence does not count towards the caption "Turn on what Sentinel should
watch. Everything found on this machine is pre-selected." The *Just the server* tile reads
"Vitals, ports, security, nginx, SSL and logs. Add DSpace modules whenever you like."

**Home** — (1) *Pulse band*: accent background, Space Grotesk 28 headline: "All systems healthy" /
"1 warning · Memory 83 %" / "3 warnings · Memory 83 %, Solr +1 more" (a warning names its
subject, and so does a check that could not run: "1 check could not run · Logs") / "Critical:
PostgreSQL unreachable"; acknowledged alerts do not count (the server decides, §9): with nothing
unacknowledged left it reads "All systems healthy · 2 acknowledged" (mood ok), and a warning
headline never names a component whose open alerts are all acknowledged (judged by the server's
`alerts_open`). Home asks for `/api/status` right after an Ack or its undo (`ctx.refreshStatus()`,
§12.8), so the band, the accent and the sidebar follow at once; the sidebar line stays "All
healthy · 8 s ago" and its tooltip shows the whole headline. The Alerts card keeps the
acknowledged alert with its Acked pill, and the component tiles and the Stack keep the real
status; subline "server‑1 · checked 8 s ago";
ghost pill *↻ Check now* (reads "Checking…" while busy). While the data is out of date the band
reads "Sentinel is unreachable — last check 11:34" or "Checks are overdue — last check 11:34"
with the subline "<host> · showing the last known state" (§12.8). (2) Section **Server**: four
stat tiles 💻 CPU · 🧠 Memory · 💾 Disk · ⚖️ Load with sparklines and status dots; tap →
`#/server` detail. (3) **DSpace stack / Services**: one tile per enabled component (emoji · label
· subtitle = reason or "Healthy · 91 ms" · status pill · ›); counts read "Last known: 10 of 10
healthy" while stale. (4) **Alerts**: open alerts as tiles with an *Ack* ghost button (after
Ack the "Acked" pill and the muted line "Reminders are off. It stays listed until the cause is
fixed."), or the empty state "✅ No open alerts — checks run every 15 seconds."; with a warning but
no alert yet "⚠️ Memory is above its warning threshold (83 %) — an alert opens if it stays there."
/ "<Service> needs a look (<reason>) — an alert opens if it stays that way."; when checks could
not run, one clause per component with the reason it gives, joined by "; ": "❔ No open alerts —
Logs cannot be checked: permission denied (/dspace/log/dspace.log)." ("… could not be checked this
time: check timed out after 29 s" only for a check that timed out or raised, the scheduler's
`<id>.error` rung with `detail.collector_error`; "… has not been checked yet" before its first
check); with exactly one such component an *Open Logs* ghost button leads to its page; while stale
"🕓 No open alerts at the last check — new ones show here once Sentinel answers again."
(5) **Repository**
(module on): 📚 Items · 🧾 Bitstreams · 🏛️ Communities · 🗂️ Collections · 🐘 DB size · 🗄️ Assetstore.
(6) Footer line: "Ubuntu 24.04 · kernel 6.8 · up 17 days · Sentinel v0.1.0".

**Stack** — summary line "5 of 5 healthy". One *ladder group* per component: header tile, then
rungs (label left, ✅/⚠️/❌/— right with latency, failing rung message in `--crit-ink`, unreached
rungs muted "not reached"). Storage group with the assetstore meter. With no module on the
Services tab reads "💻 Only the server is watched — its vitals are on Home." and shows no
count. Tap header → component detail: the page header shows only "Checked 15 s ago" (or the
stale text) and the ladder head carries the status pill "Healthy · since 03:34 PM"; metrics
tiles (e.g. connections meter 18 / 100, DB size, version), latency chart (24 h), "Last error"
tile, recent events, "Connection used" tile (host/port/url, secrets masked). The backend's page
spans a DSpace tile across the top (the version, then "DSpace 9.x" and "snapshot build" for a
`-SNAPSHOT`, "DSpace‑CRIS <crisVersion>", and on 7.x the quiet note "7.x is end of life — 7.6.7
was the final release") and an *Actuator health* tile: DSpace's status in words ("Up", "Up, with
non‑blocking issues", "Down · known issue", "Out of service"), toned like the rung (a known issue
stays muted), the rung's message when it says more ("status DOWN (details need a DSpace admin
login)"), the answer's age from `detail.cached_age_s`, ticking with the page header: "checked 3 m
ago · asked at most every 5 minutes" under an ok or informational answer (the ones the backend
reuses) and "checked 6 s ago · asked again at the next check" under a warning (a `DOWN`, an HTTP
error, a time‑out), with no age line when the rung reads "not reached"; the components table only
when DSpace shows them (an admin view), and "Not exposed (HTTP 404)" only when the rung says so.
The frontend's page has a *Rendering* tile ("Server‑side (SSR): the homepage arrives already
rendered" / "Client‑side shell" with the rung's note; Generator
and Angular as the page names them, else "not in the page"; the Host header, "<host> (the public
URL's host)" or "<ui host> (the UI URL's host)"), and a *Public URL* tile that follows the
setting, then the last check: "not set — add it in More › DSpace connection" (value "—") only when
`dspace.public_url` is empty (read from the detail payload's `config_used`, which status frames
keep when they repaint the tiles; without it the last check decides); with a URL configured, an ok
`frontend.public` rung gives "through nginx and TLS, as visitors see it" and the latency; a rung
that is not ok gives its message in its tone, host names breaking at their dots, with its latency
when it has one (a warning, "no DSpace markup in the response") and "—" when it has none (a
critical failure, "not reached"); no public rung (before the first check, after a failed frontend
check, until a check has run with a newly saved URL) gives "not checked yet". The Solr page's
cores table tags `qaevent`, `suggestion` and `audit` "optional", shows a core that failed to load
as a "Failed" pill with "<core> failed to load: <reason>" and "Fix: <hint>" under the table, keeps
its own status for a core whose reload failed with "<core> failed to reload: <reason>" and "Still
serving with its previous configuration; a restart would not load it." under the table (while its
ping answers and the rung reads "reload failed: …"; when the ping fails too, the rung is the
ping's own problem and the second line reads "A restart would not load it either."; the notes sit
in a list labelled "Cores that failed to load", "Cores that failed to reload" or "Cores that failed
to load or reload"), and its subtitle counts the pinged cores ("5 cores pinged every check · 1
failed to load", "… · 1 failed to reload"); the failure of a core that is not in the table
(another application's core on the same Solr) gets a plain note under it and is neither counted
nor coloured. In a ladder an informational rung with `detail.known_issue` reads "known issue · …"
(muted; screen readers hear "Known issue"). On a component's page a rung with `detail.hint` (its
collector's advice, §6: no web server on this machine, a local‑only port that listens on every
interface, a log Sentinel may not read, a Solr core's fix) shows it as a muted line under its
message, first letter capitalised (unless it starts with a command: `sudo`, `chmod`, `chown`,
`chgrp`, `systemctl`, `apt`, `ufw`), whatever the rung's status; a command between backticks reads
in the monospace face, without the backticks; the Stack tab's ladders leave it out. The Security
rungs' informational messages read as they are ("not available · sshd_config is not readable by
Sentinel's user", "no SSH server found", "no public address set"), never with a second "not
available" in front. The Ports page's *Listening* table names the addresses each port listens on ("0.0.0.0, ::",
"::", "127.0.0.1": the rung's `detail.addrs`, else `metrics.listening` for that port), "—" when it
does not listen or when only a TCP probe of localhost answered (the bind address is unknown then).
The **Security page** (`#/stack/security`, `screens/detail/security.js`; its Stack ladder follows
Ports and its Home tile is like every other component's) puts its ladder, with the hints, above
its tiles, both across the page: its two tables keep every column and its hints are long. The
tiles follow the rungs, each with its rung's message as the first line in the rung's tone (muted
when informational; "not checked yet" before the first check): 🔌 *Open ports*, a table Port ·
Program · Address · Reachable · Expected · Verdict, new and exposed ports first, then those only
Tailscale reaches, the expected ones and the local ones (`VERDICT_RANK`), then by port; a UDP
port tagged "udp", Program "—" when Sentinel cannot see it, Reachable "yes"/"no", Expected "open"
/ "local only" / "no rule", Verdict pills *Expected*, *Local* and *Tailscale* (plain: reachable
only through Tailscale's network), *Exposed* and *New* (warning), a port's collector note beside
its pill ("reachable through Tailscale only", "Tailscale's own port (WireGuard)", "Sentinel",
"DHCP client"; it drops under the pill when the column is narrow), addresses breaking at their
dots and colons, so 40 ports never scroll sideways ("Ports appear after the first check." / "No
ports to show."); the rung's `detail.note` goes under the table in a list labelled "Port notes"
("Program names are known only for Sentinel's own user; run `sudo ss -ltnp` on the server to see
the others.", the command in the monospace face); 🧱 *Firewall*: Host firewall "ufw (active)" or
"none active", Reachable without a rule "none" or "2 ports" (`metrics.exposed`, the count the
rung uses; the page never counts on its own); 🔑 *SSH*: Root
login, Passwords, Port (left out when sshd_config cannot be read); 🌐 *Web*: Public address (or
"not set"), Encryption "HTTPS" or "none (plain HTTP)", Missing headers ("none" when none is);
📦 *Updates*: Security updates, Other updates (or Pending updates), Reboot required, Read from; 🏷️
*Software*, a table Software · Version · Supported until · Status with the pills *Supported*,
*Ends soon* (plain: the rung stays ok), *Ended* and *Affected* (warning), *Not known* (plain), and
under it a note for software that ended, is affected or ends soon ("Solr 8.8.2: affected by
Log4Shell (CVE-2021-44228): upgrade to 8.11.1 or later"; "Versions appear once the other
services have been checked." before any); 📄 *Files*: one row per file name, "mode 640 ·
tomcat:tomcat", plus " · readable by every user" when others may read it; 🛰️ *Sentinel*: Listens
on, Login "set" / "not set" and Token "set" / "not set" (from `metrics.sentinel.login` and
`.auth`; without them from the rung's words: "with a login" = a login, "with a password" = a token
and no login, "without a password" = neither). Its *Settings used* tile lists the port
rules, `dspace.public_url` and `dspace.install_dir` from `config_used`, or, when the page brings
none, "Reads the port rules, dspace.public_url and dspace.install_dir from sentinel.yaml." No
control on the page changes a port or a firewall.
An incident page leaves its header controls empty (status and
duration are in the hero); History's incident tiles read "Sep 25, 11:05 AM · ongoing".

**History** — one **range chip row** at the top (1 h · 6 h · 24 h · 7 d · 30 d; persisted) scoping
everything below. Sections: Server (CPU %, Memory %, Disk %, Load; Network RX/TX two‑series
with legend) · Response times (REST, Search, Homepage with warning/critical hairlines) ·
Availability (a strip per component + uptime %) · Incidents (🧭 tiles → detail with hypothesis,
vertical sequence timeline, affected services, alerts) · Alert history (collapsed, *Show more*) ·
Reports (📄 tiles titled by the operator's title, else "Last 24 hours" or the covered days
"Sep 20 – 24", subtitle "… · generated Sep 25, 01:42 PM · 5.7 KB" → open in new tab /
*Download* / *Delete* with Undo; the report page has "Generated" and "Covers Sep 22 – 23 · 2
days" rows). With a module off its traces are gone (e.g. no alerts of that module in Alert
history), and a half‑row whose partner is hidden spans the row. Charts hold the previous render
at 50 % opacity while refetching — no skeleton flash.

**More** — heading sentence "Turn on only what you need. Sentinel stays simple." Module toggles
with one‑line explainers: 🐘 PostgreSQL "Checks the database really answers queries, not just that
the service runs." · ☀️ Solr "Pings every core and times a real search." · ☕ DSpace Backend "REST
API, actuator health and its database and Solr dependencies." · 🖥️ DSpace Frontend "Loads the
Angular homepage and times it." · 🌐 nginx "HTTP and HTTPS through the web server." · 🔒 SSL
certificate "Days until your certificate expires." · 📜 Logs "Counts new errors in DSpace, Tomcat,
Solr and nginx logs." · 📚 Repository stats "Items, bitstreams, communities, collections and
storage growth." · 🗄️ Assetstore "Free space where your files live." · 🔌 Ports "Which ports are
open, and which should stay local only." · 🛡️ Security "Open ports, firewall, SSH, updates and
software support: read-only." · 🧭 Incidents "Groups related alerts into one story."
Then rows (›), each with a one‑line state: ⚠️ Alert thresholds · 📨 Telegram ("On · 3 chats" / "On ·
no chats yet" / "Off", the environment's bot counted) · ✉️ Email ("On · 2 recipients" / "On · no
recipients yet" / "Off") · 🔗 DSpace connection (URLs, dirs, services, *Detect automatically*;
its *nginx and SSL* section starts with "No web server on this machine (DSpace opened directly on
:4000)? Switch nginx off in More › Modules." and the *HTTP URL* help reads "The web server in
front of DSpace, not the DSpace UI's own port (:4000): the DSpace Frontend check covers that.";
its PostgreSQL *Password* is masked as one unit like the other secrets, `form.maskAsOne`: the
first keystroke, paste or delete replaces the whole mask, and while it shows the mask the line
"Saved. Hidden for safety — type a new one to replace it." sits under it; its placeholder only
describes an empty field, "Empty uses peer or .pgpass authentication", and its help reads
"Sessions are READ ONLY with a 3 s statement timeout.") · 🎨 Appearance (System / Light / Dark
chips) · 📄 Reports ("Weekly report every Monday at 08:00", plus " · sent to Telegram and email" /
" · sent to Telegram" / " · sent by email" only for channels the schedule asks for that
`GET /api/reports/options` reports available; "Weekly report off"; on its page the line under the
schedule names the zone it runs in, from `/api/meta.timezone` (`form.zoneWords`): "Next report:
Monday 08:00 Asia/Colombo (+0530) · in 3 days.", or "… · in 3 days (server local time)." when the
server names no zone, and "Off — reports are only generated from the ＋ New report panel." while
the schedule is off) · 👤 Account (only while a login is set: "Signed in as admin", else
"Password and sign out"; `data-key="account"`) ·
ℹ️ About (version, Sentinel uptime, retention, DB size, *Export data*, docs + GitHub links). Danger
zone at the bottom, visually separated: *Clear history* and *Reset Sentinel* (the only two confirm
dialogs in the app; a reset keeps connection, Telegram and email settings). The rows are asked for
again (`GET /api/config`, `GET /api/reports/options`) whenever a settings page on top of More
closes (its Back, the browser's Back, the More tab), so they show what was just saved; an older
answer never paints over a newer one.

**More › Alert thresholds** (`#/more/alerts`) — the rule cards in the groups Server · Response
times · Services · Behaviour (§12.6), then a last group *Times in messages*, subtitle "Alert
messages, emails, reports, quiet hours and the daily digest use this zone. The dashboard shows your
browser's time.", with one field, *Time zone* (placeholder `Asia/Colombo`), help "IANA name such as
Asia/Colombo; empty = the server's zone (UTC).", the parenthesis being
`/api/meta.timezone.server_abbreviation` (else `server_name`; left out when the server names
neither). The value is saved as `server.timezone` and sent only when it changed, so the thresholds
still save on a server that predates the setting. A 400 about the zone shows under the field in
the server's words, without the settings path ("Unknown time zone 'Mars/Base': use an IANA name
such as Asia/Colombo, or leave it empty for the server's zone"), and focuses it; after a saved zone
the page asks for `/api/meta` again (`ctx.refreshMeta()`), so Telegram, Email and Reports name the
new zone. It is here rather than in Appearance because it changes what Sentinel sends and when it
stays quiet, not how the dashboard shows times: Home's "Started …" times and History keep the
browser's zone and say nothing about it.

**More › Telegram** (`#/more/telegram`) — switch *Send alerts to Telegram*; a *Bots* list of 1–5
cards, one per person: *Name (optional)*, *Bot token* (shown as `••••••` and kept on save through
the mask and the card's `id`; the first keystroke, paste or delete replaces the whole mask; while
it shows the mask a muted line under it says "Saved. Hidden for safety — type a new one to replace
it."), *Chat
id*, *All alerts* / *Critical only*, *Test*, ✕ (instant, Undo "Removed <name> — Save changes to
apply."); *＋ Add a bot*, disabled at five (the environment's bot counts), "Up to 5 bots, one per
person"; the environment's bot as a read‑only card "Chat <chat id>" · "From environment · set in
config/sentinel.env" with a severity pill and *Test*; a hint when the switch is on and no bot has
both a token and a chat id; *Quiet hours* and *Daily digest*, whose help names the zone of
`server.timezone` from `/api/meta.timezone` (`form.inZone`: "Only critical alerts during this
window, in Asia/Colombo (+0530), for every bot. Leave empty to always notify." / "One summary
message per day at this time, in Asia/Colombo (+0530). Leave empty for none."; without a zone the
clause is left out; a pasted en or em dash is accepted; quiet hours that are not `HH:MM-HH:MM` or
whose two times are equal are refused under the field before anything is sent: "Use HH:MM-HH:MM,
for example 23:00-07:00." / "Quiet hours must start and end at different times."); *Save changes*
/ *Send test message*; a *How to set it up* disclosure (@BotFather, message the bot,
@userinfobot, paste both).

**More › Email** (`#/more/email`) — switch *Send alerts by email*; *Provider* chips *Gmail*
(smtp.gmail.com, 587, STARTTLS — or the port and security a saved Gmail server already has, which
the line under the chips names: "smtp.gmail.com, port 465, SSL/TLS — nothing else to fill in.";
server fields hidden; saving sends `from_address: ""`, since Gmail sends as its account) and
*Other SMTP server* (*SMTP server*, *Port*, *Security* chips STARTTLS / SSL/TLS / None, which move
the port between 587 and 465, *Username*, *Password*, *From address (optional)*); *Gmail address*;
*App password* (masked, its help names Google Account › Security › 2‑Step Verification › App
passwords; "Saved. Hidden for safety — type a new one to replace it." under it while it shows the
mask); *Send as*; a *Recipients* list of 1–5 cards (*Address*, *Label (optional)*, severity chips,
*Test*, ✕) with *＋ Add recipient* ("Up to 5 recipients"); *Quiet hours* (help "Only critical
alerts by email during this window, in Asia/Colombo (+0530). Leave empty to always send.";
refused under the field before saving like Telegram's); *Daily digest* (help "One summary email
per day at this time, in Asia/Colombo (+0530). Leave empty for none."; the zone as on Telegram's
page); *Save changes* / *Send test email*; a Gmail *How to
set it up* disclosure (2‑Step Verification, the app password, paste). Changing the provider,
server, port, security or login away from the saved ones empties the password field ("Enter the
password again — the saved one is only sent to the server and login it was saved for."), and
returning to them brings the mask back; opening the page changes nothing: only a change by the
person counts. While `password_from_env` the field is read‑only with the help "Set in
config/sentinel.env (SENTINEL_SMTP_PASSWORD) — change it there.", looks locked (the `--surface-2`
fill, `--ink-2` text, a not‑allowed cursor), ignores keystrokes, has no "Saved. Hidden …" line and
is never sent.
A login that is not an address asks for the From address ("Enter the address alerts come from —
the username is not one.").

**More › Account** (`#/more/account`, `screens/more/account.js`, only while a login is set) — a
tile 👤 "Signed in as admin" ("Sessions end after 12 hours without use, or after 30 days with Keep
me signed in.") with a ghost *Sign out* (`ctx.signOut()`: `POST /api/logout`, then the sign‑in
screen), then the group *Change password* ("Other browsers signed in with this login are signed
out; this one stays signed in."): *Current password*, *New password* (help "At least 12
characters, and not the username."), *Repeat new password*, button *Change password*
("Changing…"). The page checks before posting: "Enter the current password.", "Use at least 12
characters.", "Use at most 256 characters.", "The new passwords do not match.". `POST
/api/account/password`; success shows the snackbar "Password changed. Other sessions were signed
out." and empties the fields; "The current password is wrong." (and a 429) goes under *Current
password*, the password rules and "The new password is the same as the current one." under *New
password*, anything else into the snackbar "Password not changed — <error>"; a 401 leaves it to
the sign‑in screen.

On both pages every test saves the form first; each card then shows its result ("✅ Alex ·
@alex_sentinel_bot · delivered", "✅ Ops team · sent", "❌ … · <reason>"; a reason keeps a mail
server's name: "❌ Ops team · smtp.gmail.com:587 closed the connection: …"), a summary follows the
list, and the demo answers "Demo: logged, not sent". Server errors land under their card and
field, without the settings path ("notifiers.email.smtp_host: ") the API puts in front.

**Report panel ("＋ New report")** — right‑side panel titled *Generate report*; range chips
(Last 24 hours ✓, 7 days, 30 days, Custom → two date inputs); format chips (HTML ✓, Markdown,
JSON); optional title; *More options* disclosure: section checkboxes (only sections whose module
is on: Latency, Incidents, Repository, Logs and Security are tied to their modules, `SECTION_MODULES`; the
panel always sends its explicit list), *Send to Telegram* ("Deliver the file to every
Telegram bot as soon as it is ready.") and *Send by email* ("Email the file to every recipient as
an attachment."), each shown only while `telegram_available` / `email_available`; with neither,
"📨 Telegram and email are off — set one up in More › Telegram or More › Email to get reports
delivered."; one full‑width accent button *Generate report* → "Generating…" → panel closes,
snackbar "Report ready" naming each delivery ("Report ready · sent to Telegram and email", "Report
ready · sent by email", "Report ready · sent to Telegram · not sent by email: <reason>", "Report
ready · Demo: logged, not sent") with action *Open* (opens the HTML report in a new tab).

### 12.6 Forms, feedback, empty states, motion, copy

* Forms: each field group is a single column; biggest field first; one filled CTA per
  panel/screen max; inputs radius 22, height 44 px, visible labels, helper text, errors below
  the field (a re‑pointed PostgreSQL without the password shows "Enter the password again — the
  saved one is only sent to the server it was saved for, …" under the password field, which is
  focused), `inputmode` set; Enter submits, Esc closes. Settings sub‑views are at most 960 px
  wide, single inputs stop at half of that (472 px), and the form's action row sticks to the
  bottom of the window (in the flow below 900 px). Alert thresholds lays its rule cards and
  the Behaviour fields in auto‑fill columns (three at 1280–1920 px) with the group titles across
  the row. Discovery results show list values readably ("22 SSH, 80 HTTP, 8983 Solr (local
  only)", "DSpace · /dspace/log/dspace.log (daily files)"), and onboarding step 2 lists the port
  rules *Start monitoring* applies ("Expects 22 SSH, … open · 8983 Solr, 5432 PostgreSQL local
  only.").
* Ack/unack, delete report, toggling a module: instant, with an *Undo* snackbar (6 s,
  `aria-live="polite"`, never steals focus; its action is an accent pill with `--on-accent`
  ink). Focus always lands on a real control: after Ack on the alert's "Acked" pill, after
  Delete on the next report's Delete (the last one → "＋ New report"), after Undo on the
  restored item; programmatic focus on page and section headings shows no ring. Confirm dialogs
  only for Clear history and Reset.
* Empty states = one emoji + one sentence naming the next click: "📄 No reports yet — click
  ＋ New report to generate your first one." · "🧭 No incidents yet — related alerts will be grouped here." ·
  "📨 Telegram and email are off — set one up in More › Telegram or More › Email to get reports
  delivered." ·
  "📈 Collecting the first samples — charts appear after a minute."
* Motion: panel slide‑in from the right 200 ms ease‑out, sub‑view crossfade 160 ms, snackbar
  fade; no chart entrance animation; `prefers-reduced-motion` zeroes durations; nothing loops.
* Copy: sentence case, plain verbs, an action keeps its name through the flow ("Generate
  report" → "Generating…" → "Report ready"). No exclamation marks, no jargon the operator
  doesn't already use. Numbers: `Intl.NumberFormat`; disk, mount, assetstore, memory, database
  and bitstream‑total sizes in GB with one decimal ("1,024.0 GB") while single files (reports,
  Solr cores, Sentinel's own database) and rates keep adaptive units (GB would print "0.0 GB");
  latency in ms below 1 s and s above; relative times "8 s ago"; a number and its unit are joined
  by a no‑break space. API texts are shown in UI words (events "Solr: Healthy → Warning — …",
  "Solr turned on"; the Recent events *Kind* column: Alert, Status change, Incident, Setting,
  Report, Telegram, Email, Webhook, "Telegram and email (demo)", "<Channel> (demo)", "<Channel>
  (failed)", "<Channel> (held back)", Demo, Error; rows written before email existed read
  "Telegram …").
* Accessibility: body 16 px, click targets ≥ 40 px, focus visible everywhere, `<nav>` links with
  `aria-current="page"`, the breadcrumb reads "Stack : PostgreSQL" (a visually hidden ": "),
  panels are real `<dialog>`s, charts have `role="img"` + `aria-label`
  summary + a *Show as table* toggle, status never colour‑alone (emoji/label always present),
  contrast verified (see `docs/screenshots/contrast.txt`).

### 12.7 Charts (`charts.js`, follow the dataviz skill)

* `sparkline(points, {status})` — 12 points, 1.5 px line in `--ink-3`, last point dot in the status fill.
* `lineChart({series, unit, thresholds, height=180})` — SVG; one y‑axis; hairline solid grid
  (`--chart-grid`), ticks in `--ink-3` 12 px tabular; 2 px lines with round joins; area wash 10 %
  for a single series; threshold hairlines in `--warn-fill`/`--crit-fill` with a 12 px label in
  `--warn-ink`/`--crit-ink` reading "Warning 80 %" / "Critical 92 %"; crosshair + one tooltip
  listing every series at that x (values bold, labels secondary, line keys); keyboard: ←/→ move
  the crosshair; legend for ≥ 2 series; *Show as table* renders a `<table>` twin;
  `update(series)` redraws without flashing; ResizeObserver for width. Every axis labels its
  ticks in one unit with step‑exact decimals; byte axes pick one 1024‑based unit from the top of
  the domain and step in round numbers of it (0 · 0.5 · 1 · 1.5 · 2 MB/s); a flat zero series
  gets a 0–1 axis. Time ticks keep ≥ 90 px apart; spans over two days step in whole local days
  from midnight (1/2/3/7/14) and read day + month ("Sep 3"); end labels anchor inward.
* `availabilityStrip(buckets)` — one 6 px‑radius bar per bucket in the status fill, 2 px gaps,
  unknown buckets `--surface-2`; tooltip with time + status.
* `meter({value, max, status})` — 8 px track in `--surface-2`, fill in the status fill, label right.

### 12.8 Client architecture

ES modules, no framework, no bundler. `api.js` uses `new URL('api/…', document.baseURI)` and
sends JSON (or no body) on writes, which the API's cross‑site checks require (§9).
`store.js` holds `meta`, `status`, `range`, `stale`, `session` (`{login_required, authenticated,
username, expires_ts}` from `/api/session`; an older server without the route reads as
`{login_required: false}`), subscribers. `app.js` boots: `GET /api/session` (404 = no login) → the
sign‑in screen, or load meta → onboarding gate or shell; open `EventSource('api/stream')`
(fallback to 15 s polling on error, resume on `visibilitychange`); on `status` events update the
store and set `--accent`. `api.js` has `api.session()`, `api.login({username, password,
remember})`, `api.logout()` and `api.changePassword({current, new})`; its
`setUnauthorizedHandler(fn)` runs for a 401 from any call but `api.session`, `api.login` and
`api.logout`, and `app.js` uses it to show the sign‑in screen (§12.5).
`store.stale` is `'offline'` when Sentinel does not answer (a failed *Check now* sets it at once)
or `'overdue'` when no check has completed for max(45 s, 3 × `collector.system_interval`); while
it is set the accent is sky, `html[data-stale]` mutes status pills, dots, rungs, meters and
sparklines and greys the ✅ marks on Home, Stack, the component and server pages, and the texts
of §12.3/§12.5 apply. The next fresh status clears it. Screens export
`render(root, params, ctx) -> {destroy?}` and re‑render from store changes with minimal DOM
patching (replace section contents with `ui.replaceKeepingFocus`, keep scroll). `ctx` (the
screen contract at the top of `app.js`) carries `store`, `api`, `navigate`, `back`, `openPanel`,
`closePanel`, `setTitle`, `setControls`, `checkNow`, `setTheme`, `refreshMeta()` (re‑fetch
`/api/meta` into the store, after module, setup or time‑zone changes), `refreshStatus()` (re‑fetch
`/api/status` into the store; Home and the incident page call it after an Ack or its undo, which
change the mood and headline but send no `status` frame; it fails quietly, the stream or the next
poll brings the same answer), `signOut()` (`POST /api/logout`, then the sign‑in screen; a 401
there counts as already signed out), `route`, `signal`, and `panel` / `finish` for panels,
onboarding and the sign‑in screen;
`app.js` exports the same helpers for direct use. All text via
`textContent`. Theme: `data-theme` on `<html>` from `appearance.theme` (persisted server‑side and
mirrored in `localStorage`). Fonts (UI only; reports use system fonts): one Google Fonts `<link>`
(Space Grotesk 500/700, Inter 400/500) with `display=swap`; everything works when it fails to
load.

---

## 13. Demo mode

`SENTINEL_DEMO=1` (or `./sentinel.sh demo`) swaps every collector for `collectors/demo.py`: a
realistic full stack (hostname `demo-dspace`, DSpace 9.4 on Ubuntu 24.04 with `tomcat10`: while
`dspace.backend_service` and the Tomcat log keep Sentinel's defaults (`tomcat9`,
`/var/log/tomcat9/catalina.out`), as in `doctor --demo` or after *Skip for now*, the demo's
collectors report its own `tomcat10` unit and log (`DEMO_BACKEND_UNIT`, `DEMO_TOMCAT_LOG`: the
`backend.service` rung "systemd tomcat10" and the `logs.tomcat` path), so the ladder, `/api/status`
and `doctor --demo --json` say `tomcat10`, while what reads the settings (the Connection used and
Files watched tiles, More › DSpace connection, the JSON report's `logs.files`) shows the defaults
until *Start monitoring* applies the demo's discovery; a unit or path set by hand shows as set; as
an anonymous client really sees it: the actuator answers `UP_WITH_ISSUES` without components, and
`DOWN` with HTTP 200 while Solr is down, graded by the real `grade_health`; six Solr cores,
`qaevent` and `suggestion` optional and empty, Solr 9.6.1; a server‑rendered UI with Generator
"DSpace 9.4" and Angular 20.3.30; the Tomcat last‑error line in `tomcat10`'s rsyslog `catalina.out`
format; about 27 600 items and 81 600 bitstreams — counters that start at 27 491 / 81 337 a week
before the first start and keep growing, anchored to that first start across restarts), seven days
of back‑filled history (including `postgres.connections` history and live‑style alert ids such as
`log_errors:dspace`) inserted on first start, live metrics with gentle noise and a scripted incident
every ~20 minutes (memory climbs → Solr latency → Solr down → search alert → recovery) so alerts,
incidents, Telegram and email messages (logged, not sent) and reports all have data. `meta.demo =
true` and the UI shows a small "Demo data" pill in the Pulse band.

The demo's Security component (`DemoWorld._c_security`, built by the real
`security.build_ladder`) is the demo server's: the ports its `ports` rules expect (the open ones
public, the local‑only ones on loopback, Solr's 8983 gone while Solr is down), all expected, with
no program names (as on a real server where Sentinel runs as its own user; whether it runs as root
is not known there, `HostFacts.root` is `None`, so the ports rung carries no `detail.note`), no
Tailscale port (`metrics.tailscale` 0); ufw active; SSH with
`prohibit-password` and passwords on (the hint "Keys instead of passwords: …"); HTTPS on its
public address (`dspace.public_url`, else `DEMO_PUBLIC_URL` = `https://repo.demo-dspace.org`)
without `Strict-Transport-Security` (the missing‑header hint); 3 pending updates of which none is
a security update (`DEMO_UPDATES` = (3, 0): "no pending security updates (3 other updates)");
DSpace 9.4, Solr 9.6.1, PostgreSQL 15.8 and Ubuntu 24.04, all supported; `local.cfg` 640
tomcat:tomcat; Sentinel on `server.host`. Every check is ok, so the demo starts calm (`doctor
--demo` reads "All systems healthy" and exits 0) and shows two hints.

Demo rules:

* **Nothing leaves the machine.** Every Telegram call (`sendMessage`, `sendDocument` for reports,
  `getMe`/`telegram-test`) is refused inside `notifiers/telegram` with
  `error: "demo mode: nothing is sent to Telegram"` before a request is built, and no SMTP
  connection is ever opened (`error: "demo mode: nothing is sent by email"`). Alert
  notifications, the test message and the test email (`POST /api/actions/telegram-test` and
  `email-test` answer `skipped: "demo"`) and report deliveries are written as `notify.demo` events
  instead, with the message as HTML and text and, when email is among the channels, the email
  subject; an alert's row lists `email` only while email is switched on (Telegram always).
  `POST /api/reports` answers `telegram` and `email: {ok: true, skipped: "demo"}`. Discovery
  describes the demo stack, never the host (§6), and `config/sentinel.env` is not read.
* **The real config is never written.** Unless `SENTINEL_DATA_DIR`/`SENTINEL_CONFIG` are set,
  the demo runs with the data directory `data/demo/` and the config path `data/demo/sentinel.yaml`
  (plus the demo overrides), and loads `data/demo/demo-sentinel.yaml` on top. What the UI changes
  while the demo runs (onboarding, modules, thresholds, theme) is saved to that file as the
  difference from the demo configuration, never with `server.*` or secrets — every bot token, the email password, the environment's
  Telegram bot (all are ignored when
  the file is read, so a later real host, token or threshold is never reverted), so a restarted
  demo skips onboarding and `./sentinel.sh start` afterwards still sees the operator's own
  settings. The file's first line reads "# DSpace Sentinel demo settings — only what the UI
  changed; delete the file to start over". Delete it to see onboarding again.
* **No login.** The demo never asks for the dashboard login (`auth.login_active` is false):
  `GET /api/session` answers `login_required: false`, `POST /api/login` 400 "No login is set up on
  this Sentinel.", there is no sign‑in screen, no Sign out and no More › Account, a
  `server.login` is ignored (no start‑up notice either) and never satisfies the bind check, and
  the Security rung never counts one.
* **Demo data is dated now.** The "last error" log lines, incidents and history are generated
  relative to the current time in each log's own date format.
* **`SENTINEL_DEMO_FAST=1`** compresses the *live* cycle to three minutes (calm 30 s → a
  two‑minute incident → calm) and makes the scheduler tick every group every 5 s (repository
  stats excepted; the config intervals are untouched), so memory, search‑latency, Solr‑down,
  backend and log‑error alerts, the incident and its resolution all appear within a few minutes.
  The back‑filled history always uses the 20‑minute timings.

---

## 14. Tests and CI

`tests/`: `test_config.py` (defaults, the example YAML equals them, merge, env, masking,
personas, file layer, Telegram destinations: ids, the old single‑bot shape, the environment's
bot, masked tokens kept by id; the email section and its validation; `server.login`: the secret
masked and kept, an unusable login kept on with its notice), `test_store.py` (schema, series
bucketing, availability, rollup, purge, soft delete, the sessions table and its purge),
`test_auth.py` (the dashboard login: scrypt hashing and the PBKDF2 fallback, unicode passwords,
hashes that do not parse, the dummy hash, the rule messages, sessions that keep only the token's
hash and end after 12 idle hours, 7 days or 30 days with remember, a password change ending the
other sessions, the purge, both brute‑force limits, the CLI token's mode, the middleware with a
login, with only a token, with neither and in the demo, every route with its texts and status
codes, the cookie's flags and path, `Secure` over HTTPS, the hash never shown and never patched,
a CLI change applied without a restart, a hand‑removed login on 0.0.0.0 kept), `test_health.py` (roll‑up, not
reached, dep capping and the rung that decides a dependency's reason, mood, acknowledged alerts
calming the mood and the headline), `test_rules.py`, `test_engine.py` (confirm/clear counts,
escalate, an escalation clearing the acknowledgement, resolve, ack, reminders, quiet hours and
digests per channel, the text of an open Security alert following its rung), `test_incidents.py`
(every hypothesis + affected closure), `test_messages.py` (exact Telegram strings, times in
Asia/Colombo and in UTC), `test_tz.py` (`server.timezone`: names and labels, `/api/meta`'s
`timezone`, the check and the save, a bad name ignored with a notice in the file and refused by
the API, quiet hours across midnight, the digest hour, the weekly report's hour and title, report
times and chart ticks in the chosen zone), `test_notifiers.py`
(delivery through every bot, `min_severity`, quiet hours per channel, token redaction, advice texts,
the sentences of a request that got no answer, directly and through a proxy, the webhook's TLS and
redirect policy, the Outbox), `test_email.py` (subjects, headers and bodies,
TLS and the `none` rule, error sentences, the password scrubbed, a real `smtplib` session against an
in‑process fake SMTP server — never a real mail server), `test_dspace_version.py` (the parser and
every version window, table‑driven over the live `dspaceVersion` strings), `test_discovery.py`
(local.cfg fixtures incl. `${dspace.dir}`, masking, units, services that answer without a unit,
the Tomcat, nginx and Solr log rules with `-Dsolr.log.dir`, prefixes, S3, `dspace-cli.log`, version
findings; `tests/fixtures/dspace9-jar/`; an autouse stub keeps every test off this machine's
process list), `test_logs.py` (tail + counting, time zones, rsyslog `catalina.out`, DSpace 7–10
rotation, the permission hints, the nginx entry while its module is off), `test_collectors.py`
(every ladder against mocked httpx, psycopg, systemd and sockets; the actuator grades, the Host
header and its IDNA form, SSR/CSR, failed and optional Solr cores, the nginx and ports hints, with
the real payloads of `tests/fixtures/dspace-live/`; and the real collectors in the situation of
every scenario of `SCENARIOS` in `tests/e2e/test_detail_states.py`, compared with what the scenario
shows), `test_scheduler.py` (hot reload, module switches during a check, check‑now coalescing,
hooks off the lock, the shutdown warning's count), `test_demo.py` (now‑relative log lines, fast
mode, the Telegram and email guards, the DSpace 9.4 stack, its calm Security component),
`test_security.py` (the Security collector through fake `HostSources`, never this machine's
sockets, files or services: a healthy server, every warning and the three alerts, nothing
readable ⇒ informational and never unknown, a failing source never raising out of `collect()`, the
port baseline (a later port is `new`, the baseline outlives a new collector, none from an
unreadable socket table, Sentinel's own port expected), an IPv4‑mapped loopback address,
Tailscale‑only ports and Tailscale's WireGuard port, the program‑names note, the firewall count
against a real server's socket table (with made‑up addresses), the capped port list, the firewall
states with and without systemd, `sshd_config` parsing (first value wins, includes first, relative
includes, `Match` blocks, defaults, unreadable files), the web rung's headers, redirects and plain
ports with one GET that reads no body and none without an http(s) public URL, update-notifier's
wordings, the support dates a day before and after every boundary and `soon` within 90 days, the
files and Sentinel rungs (the Sentinel rung with a login, with a token and with neither, never a
login in the demo), UDP sockets in `listening_sockets`), `test_reports.py` (analytics on synthetic data,
HTML/MD/JSON render, the Security section in every state, recommendations, delivery), `test_api_core.py`, `test_api_alerts.py`,
`test_api_reports.py`
(TestClient in demo mode: every endpoint, auth on/off, config patch keeps secrets, the test actions,
reports lifecycle and delivery, SSE first event), `test_api_security.py` (Host allowlist, cross‑site
writes, the exact `/healthz` exemption, the open‑bind guard with and without a login, read‑only
server settings), `test_cli.py` (doctor output, exit codes, exit 0 when everything open is
acknowledged, `sentinel.sh`, `telegram-test --to`, `email-test`, `report --email`, the doctor box's
Security row, `set-password` and `hash-password`, `status` with a login against a real server,
`install` without a terminal, the pass‑through and the help), `test_static_tokens.py` (the
one‑token‑file rule), `test_ansible_role.py` (the role's template under Jinja2 stand‑ins and, when
ansible‑core 2.15+ is installed, real Ansible: vault, the pre‑flight checks (a bad login user, a
password where its hash belongs, an open bind with neither a login, a token nor
`allow_unauthenticated`), a vaulted login hash, the rendered config loaded by `load_config`),
`tests/e2e/test_ui.py`
(Playwright, demo mode: onboarding → home → each tab → component detail → report generation →
dark theme; the wizard's Security row pre‑selected with "this server" for every option, More ›
Modules listing Security after Ports, the report panel offering the Security section only while
its module is on; More › Email keeps two recipients and More › Telegram a second bot across a reload;
demo test buttons read "Demo: logged, not sent"; a saved PostgreSQL password shows the "Saved.
Hidden for safety" line; an acknowledged warning calms Home ("All systems healthy · 1
acknowledged", `data-mood` ok, the sidebar "All healthy") while its tile and Stack row keep
warning; the *Time zone* field of More › Alert thresholds with its help, its PATCH and a refused
name, mocked; captures go to the git‑ignored
`build/screenshots/`, or to `SENTINEL_E2E_SHOTS=docs/screenshots` to refresh the README set, at
1440×900 (primary) and 1920×1080, light + dark, plus one 390×844 shot to prove the narrow fallback
works, and `more-email` / `more-telegram`; the README's `incident.png` is captured by hand during a
`SENTINEL_DEMO_FAST=1` incident), `tests/e2e/test_detail_states.py` (gated like `test_ui.py`: its
own demo server on a free port, setup through `POST /api/actions/setup`, Chromium from a worker
thread; `/api/status`, one SSE `status` frame and `/api/components/<id>` rewritten with
`page.route` to show every detail‑page state of the backend, frontend, Solr, nginx, Ports and Logs
pages that the demo never shows (`SCENARIOS`, and `UI_STATES`, which no collector test replays
yet: rungs with a hint, a public URL that was not checked, the test server's ports, two more
failed reloads, and the Security page healthy, with every warning, not available, with 40
ports and with Tailscale's ports (`security-tailscale`, after a real server's socket table with
made‑up addresses), its Home tile and its Stack ladder; `ADDED` lists components the demo may not
have: a state of one adds it to `/api/status` after Ports and answers its `/api/components/<id>`
itself), 45 `test_detail_state[<case>]` cases plus the report facts list; each fails on a
console error or warning, a page error or a horizontal scroll bar; its `detail-*.png` captures
always go to `build/screenshots/`, whatever `SENTINEL_E2E_SHOTS` says),
`tests/e2e/test_onboarding.py` (gated the same way, with a demo server of its own whose setup is
not completed: `/api/actions/discover` answered through `page.route` with the real `discover()`'s
result for a DSpace 8 server set up by hand, without a web server; after *Full DSpace stack* step 2
shows nginx and SSL off with "not found on this machine", and *Start monitoring* and *Skip for
now* both leave them off), `tests/e2e/test_login.py` (gated the same way; the demo never asks for
a login, so it starts a live server of its own, not a demo: a free port, a private data
directory, a config whose `server.login` holds a hash from `dspace_sentinel.auth.hash_password`,
setup completed and every module off. Seven cases: the sign‑in screen shows and only
`/api/session` is asked; a wrong password says "Wrong username or password."; the right one lands
on Home and *Keep me signed in* gives a cookie that lasts more than 8 days; Sign out returns to
the sign‑in screen, after which `/api/session` says unauthenticated and `/api/meta` answers 401;
cleared cookies plus a request on More show "Your session ended. Sign in again." and signing in
renders `#/more` again; a mocked 429 counts down on the button; More › Account runs its own
checks, changes the password, the old one is then refused and the new one signs in, and the test
puts the original back. It stays below the brute‑force limit. DOM contract: the heading "Sign in
to DSpace Sentinel", labelled *Username* / *Password* inputs and the checkbox "Keep me signed in
for 30 days" inside `#gate`, the button *Sign in*, the server's sentence in `#signin-error`, the
sidebar's `#account-name` and `#sign-out`, the More row `[data-key="account"]`, and on More ›
Account the labels *Current password* / *New password* / *Repeat new password* and the button
*Change password*).
No test reaches the network: the autouse fixture `version_probe` in `tests/conftest.py` stubs
`discovery.probe_versions` (it answers that nothing runs and records each call) and gives
`discovery.make_client` a transport that refuses every request and records it, so a test that
tried fails at teardown ("discovery's version probe tried the network: […]"); tests of the real
probe inject an `httpx.MockTransport` client (`REAL_PROBE_VERSIONS` in `test_discovery.py`), and
every email and Telegram test uses fakes. A few `test_cli.py` tests still probe Sentinel's own
ports on the test machine (127.0.0.1:9765 and :9793), and one of them fails when a Sentinel answers
there.
CI (`.github/workflows/ci.yml`, every job gating): `ruff check` and `ruff format --check`, pytest
on 3.10 and 3.12 plus a `doctor --demo` smoke run, shellcheck `sentinel.sh`, `ansible-lint
--offline --profile production`, `ansible-playbook --syntax-check` and `tests/test_ansible_role.py`
under real Ansible in `deploy/ansible` (the pytest job has no Ansible, so those cases skip there),
an install smoke job that follows the README on `ubuntu:22.04` and `ubuntu:24.04` containers
(install, start, `/healthz`, status and doctor from another directory and through a symlink,
`doctor --demo`, stop, unit render), and the Playwright smoke uploading `build/screenshots/` as an
artifact.

---

## 15. Definition of done

* `./sentinel.sh install && ./sentinel.sh demo` on a clean Ubuntu 22.04/24.04 box serves the UI on
  :9765 with no errors in the log; `./sentinel.sh doctor` prints the boxed dashboard.
* All tests green; `ruff check` and `ruff format --check` clean; shellcheck clean; ansible-lint
  (production profile) clean.
* Turning every module off leaves only Server, Alerts and the footer on Home, and the Stack tab
  reads "Services".
* The UI is designed for desktop: at 1440 px the Home tab fills the width with the 12‑column grid,
  the sidebar carries the four tabs and the "＋ New report" button, the report form opens as a
  right‑side panel and the settings as sub‑views under More.
* Nothing in the codebase writes to DSpace, Solr, PostgreSQL or their files, and nothing the Security
  module does opens or closes a port, changes a firewall or runs sudo.
* README has screenshots, the one‑line install, a feature table, the architecture diagram, the
  Telegram and email setup, and a "How it differs from Netdata/Uptime Kuma" paragraph.
