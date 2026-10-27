# Configuration reference

Sentinel reads one YAML file, `config/sentinel.yaml`, created from
[`config/sentinel.example.yaml`](../config/sentinel.example.yaml) by `./sentinel.sh install`.
Everything you leave out falls back to the defaults below. The UI (More tab) edits the file
through `PATCH /api/config`: the patch is deep-merged, validated, written atomically (temp file +
rename, mode `0600`) and hot-reloaded, so no restart is needed. You can also edit it by hand
and run `./sentinel.sh restart`.

What a save from the UI (or onboarding, `discover --apply`, `install --port/--host`,
`install-service --port/--host`) writes: the file's own settings plus the change, as a complete
file with every key. It is
rewritten from scratch, so **your comments and key order are lost** on the first UI save; keep
notes elsewhere, or make every change by hand and restart. Values that came from environment
variables or from `serve --host/--port` are never written into the file.

`server.*` is the exception: the API never changes the settings that decide who can reach
Sentinel. `PATCH /api/config` and onboarding answer HTTP 400 `server settings are set in
config/sentinel.yaml; restart to apply` when a patch would change any `server` key but
`timezone` (sending the current values back is fine), the dashboard login included. Edit the
file and restart; the login has its own command, `./sentinel.sh set-password`
([below](#the-dashboard-login-serverlogin)), and needs no restart.
`server.timezone`, the zone of the times Sentinel sends, decides nothing of the kind: **More ›
Alert thresholds › Time zone** and `PATCH /api/config` may change it, and it applies at once.

Precedence, lowest to highest: **built-in defaults ← YAML file ← environment variables**.

## Environment variables

| Variable | Overrides | Notes |
|---|---|---|
| `SENTINEL_CONFIG` | path of the YAML file | default `<repo>/config/sentinel.yaml`; [`sentinel.env`](#configsentinelenv) is read from the same directory |
| `SENTINEL_DATA_DIR` | runtime data directory | default `<repo>/data`; holds `sentinel.db`, `reports/`, `sentinel.log`, `sentinel.pid` |
| `SENTINEL_HOST` | `server.host` | |
| `SENTINEL_PORT` | `server.port` | applies to processes started from that shell (`run`, `start`, `demo`); a systemd unit only sees its own environment and `config/sentinel.env` |
| `SENTINEL_AUTH_TOKEN` | `server.auth_token` | |
| `SENTINEL_ALLOW_OPEN` | `server.allow_unauthenticated` | only the value `1` counts: serve a non-loopback `server.host` without a login or an auth token |
| `SENTINEL_TELEGRAM_TOKEN` | `notifiers.telegram.chats` (adds a bot) | with `SENTINEL_TELEGRAM_CHAT_ID`: one more Telegram bot, shown read-only in the UI ("From environment"), counted toward the five and never saved; unless an entry already has the same token and chat id. With five entries in the file it is left out instead (a warning in the log and one `config` row in the events log at each start). On its own: the token of every entry saved without one. See [NOTIFICATIONS.md](NOTIFICATIONS.md#the-bot-from-configsentinelenv) |
| `SENTINEL_TELEGRAM_CHAT_ID` | that bot's chat id | only together with the token (in an old file with the single `bot_token` / `chat_id` keys, each variable replaces that bot's value) |
| `SENTINEL_SMTP_PASSWORD` | `notifiers.email.password` | the email password, for Gmail its app password. While it is set the password is read-only: More › Email shows it locked, `GET /api/config` reports `notifiers.email.password_from_env: true`, and a `PATCH` that changes the password, or the server, port, security or username it goes to, answers 400 (change those in the file) |
| `SENTINEL_PG_PASSWORD` | `postgres.password` | |
| `SENTINEL_DEMO` | demo mode (`1`, `true`, `yes`, `on`) | every module on, hostname `demo-dspace`; never written to YAML. Demo mode (`./sentinel.sh demo`, `--demo` or this variable) uses `<repo>/data/demo` as the data directory and `data/demo/sentinel.yaml` as the config unless you set `SENTINEL_DATA_DIR` / `SENTINEL_CONFIG`, so your real config is neither read nor written. What the UI changes during a demo (onboarding, modules, thresholds, theme) is saved to `demo-sentinel.yaml` in the demo data directory (`data/demo/demo-sentinel.yaml` unless `SENTINEL_DATA_DIR` is set). That file holds only the differences from the demo configuration, never `server.*` or secrets, and the next demo run reloads it; delete it to see onboarding again |
| `SENTINEL_DEMO_FAST` | demo incident cycle | `1` compresses the live cycle from 20 minutes to 3 (calm 30 s, a two-minute incident) and runs every collector group every 5 s; the back-filled history keeps the 20-minute timings |
| `SENTINEL_LOG_LEVEL` | console log level | `DEBUG`, `INFO` (default), `WARNING` |

An empty environment value is ignored. Values set through the environment are shown in
`GET /api/config` (masked when secret) but are never saved to the file: a save writes the
file's own value for those keys. A variable wins at every start, so remove it and restart to
fall back to the file. A `SENTINEL_TELEGRAM_TOKEN` or `SENTINEL_TELEGRAM_CHAT_ID` that cannot be
a token or a chat id stops Sentinel from starting (`SENTINEL_TELEGRAM_TOKEN is not a Telegram bot
token (it looks like 123456789:ABC…, from @BotFather)`, exit code 4).

## `config/sentinel.env`

An optional file next to the YAML (`<directory of SENTINEL_CONFIG>/sentinel.env`, by default
`config/sentinel.env`) is the durable home for the variables above, secrets in particular:

```bash
# config/sentinel.env
SENTINEL_TELEGRAM_TOKEN=123456789:AAH…
SENTINEL_TELEGRAM_CHAT_ID=987654321
SENTINEL_SMTP_PASSWORD='abcd efgh ijkl mnop'
SENTINEL_PG_PASSWORD='a password with spaces'
```

- One `KEY=VALUE` per line, without `export`. Blank lines and lines starting with `#` or `;`
  are skipped. A value may be single-quoted (taken literally) or double-quoted (`\"`, `\\`,
  `\$` and `` \` `` are unescaped), the same rules systemd uses.
- Every `./sentinel.sh` / `python -m dspace_sentinel` command exports it before loading the
  config; a variable that is already set in the environment wins. Demo mode (`demo`, `--demo`,
  `SENTINEL_DEMO=1`) ignores the file.
- Both systemd units (`install --service`, `--user-service`, the Ansible role) read it with
  `EnvironmentFile=-<config dir>/sentinel.env`; the `-` makes a missing file fine. A running
  Sentinel reads it when it starts, so restart after a change.
- Keep it `chmod 600`: every command warns when it is readable by other users. It is
  git-ignored. The Ansible role writes it from `dspace_sentinel_env`.

## Secrets

`server.auth_token`, `server.login.password_hash`, `postgres.password`,
`notifiers.email.password`, `notifiers.webhook.url` (webhook URLs carry their credential in the
path) and the `bot_token` of every `notifiers.telegram.chats` entry are secrets. The API never returns them: non-empty values
appear as `"••••••"`. Sending `"••••••"` back in a `PATCH` keeps the stored value, so a form can
round-trip the whole config safely; a secret that comes from an environment variable is not
copied into the file that way, a newly typed one is. Prefer
[`config/sentinel.env`](#configsentinelenv) or a vaulted Ansible variable for production.

The email password follows the same rule: a patch that changes `notifiers.email.smtp_host` (in
any letter case), `smtp_port`, `security` or `username` while a password is stored must carry
the password again, otherwise HTTP 400 `enter the email password again: the SMTP server, port,
security or username changed and the saved password is only sent to the server it was saved
for`. Pointing Sentinel at a relay that needs no login (an empty `username`) is always allowed:
the stored password stays but is never sent there. While `SENTINEL_SMTP_PASSWORD` is set, the
password cannot be changed through the API at all (`the email password comes from
SENTINEL_SMTP_PASSWORD in config/sentinel.env: change it there`), and the server and login it
goes to only in this file.

A masked bot token keeps the token of the entry with the same `id`; an entry sent without
`bot_token` keeps its token too, and `""` clears it. A mask on an entry whose `id` is missing or
unknown is refused with HTTP 400 `enter the bot token for <label, else chat id, else "the new
bot"> again`, so a token can never move to another chat. The entry from the environment (`id:
env`) is ignored in a `PATCH`: it always comes back from the environment.

The PostgreSQL password is only ever sent to the server it was saved for. A patch that changes
`postgres.host`, `port`, `database` or `user` while a password is stored must carry the password
again, otherwise it is refused with HTTP 400 `enter the PostgreSQL password again: postgres.host,
port, database or user changed and the saved password is only sent to the server it was saved
for`. An explicitly empty password (peer authentication or `.pgpass`) is accepted.

Discovery (**Detect automatically**, `./sentinel.sh discover`) never shows the secrets it reads
from `local.cfg`: see [API.md](API.md#post-apiactionsdiscover).

---

## `server`

| Key | Default | Description |
|---|---|---|
| `host` | `127.0.0.1` | Bind address. Anything but a loopback address (`localhost`, `127.0.0.0/8`, `::1`), for example `0.0.0.0`, needs the dashboard login (`login`), `auth_token`, `allow_unauthenticated: true` or `SENTINEL_ALLOW_OPEN=1`; otherwise `serve`, `start` and the systemd unit refuse to start (exit code 4, the reason on stderr: "refusing to serve the API without authentication on 0.0.0.0:9765: create the dashboard login with ./sentinel.sh set-password, set server.auth_token (or SENTINEL_AUTH_TOKEN), bind to 127.0.0.1 and publish Sentinel through the nginx recipe, or opt in with server.allow_unauthenticated: true or SENTINEL_ALLOW_OPEN=1"). With a login, put HTTPS in front so the password never travels in plain text: read [REVERSE-PROXY.md](REVERSE-PROXY.md) first. |
| `port` | `9765` | HTTP port of the UI and API (1–65535). |
| `auth_token` | `""` | A token for scripts: `Authorization: Bearer <token>`, or HTTP Basic with any user name and the token as password. With a login set it is accepted next to the sign-in and the browser never sees a Basic pop-up; without a login the dashboard itself asks for it (the browser's Basic pop-up) on everything except `/healthz`, `/api/session` and `/api/login`. Leave empty when nginx handles authentication. Prefer `SENTINEL_AUTH_TOKEN` in [`config/sentinel.env`](#configsentinelenv). |
| `login.username` | `""` | The dashboard login's username (one admin account): 1 to 64 letters, digits and `. - _ @`. `set-password` writes `admin` unless told otherwise. Sign-ins ignore its letter case and spaces around it. |
| `login.password_hash` | `""` | The login's password hash (a secret, never the password). The login is on while it is set: the web UI asks people to sign in and a non-loopback `host` is allowed. Written by `./sentinel.sh set-password`; `--remove` empties it. See [below](#the-dashboard-login-serverlogin). |
| `hostname` | `""` | Label shown in the UI, in alerts and in reports. Empty = the machine's hostname. |
| `allowed_hosts` | `[]` | Extra names Sentinel answers to in the `Host` header, such as the `server_name` of a dedicated nginx site (`[sentinel.example.org]`); `"*"` allows any name. See below. |
| `allow_unauthenticated` | `false` | `true` lets Sentinel serve a non-loopback `host` without a login or `auth_token` (only behind a firewall, or an authenticating proxy on another machine). `SENTINEL_ALLOW_OPEN=1` does the same. |
| `timezone` | `""` | The zone of the times Sentinel shows people: an IANA name such as `Asia/Colombo`, or `UTC`; `""` = the server's own zone. The one `server` key that **More › Alert thresholds › Time zone** and `PATCH /api/config` may change; it applies at once. See [below](#the-time-zone-of-messages-and-schedules). |

Every request except `/healthz` must name a host Sentinel knows, which keeps DNS-rebinding
pages out. Always accepted: IP addresses, `localhost`, the machine's hostname, `server.hostname`,
`server.host`, `ssl.host` and the host parts of `dspace.public_url`, `dspace.ui_url`,
`dspace.server_url`, `nginx.http_url` and `nginx.https_url`, plus `server.allowed_hosts`. Any
other name gets HTTP 400 `unknown host '<name>': add it to server.allowed_hosts in
config/sentinel.yaml if Sentinel is reached under that name`. The nginx recipe in
[REVERSE-PROXY.md](REVERSE-PROXY.md) forwards the repository's host name, which is accepted once
`dspace.public_url` (or `nginx.https_url`) names it.

With the Security module on, its *Sentinel* check reads `host` and whether the login and
`auth_token` are set: on another address than a loopback one it reads "listens on 0.0.0.0 with a
login" (ok, with the hint "Put the nginx HTTPS recipe in front of Sentinel so the password never
travels in plain text."), "listens on 0.0.0.0 with a password" (ok, a token and no login), or warns
"listens on 0.0.0.0 without a password (server.allow_unauthenticated)" (or
"(SENTINEL_ALLOW_OPEN)") with neither.

### The dashboard login (`server.login`)

```yaml
server:
  login:
    username: admin
    password_hash: "scrypt$16384$8$1$…$…"     # written by ./sentinel.sh set-password
```

- The login is on while `password_hash` is set (never in demo mode, which ignores it). The web UI
  then shows a sign-in page, every other API request needs a session, `auth_token` or the local
  CLI token ([API.md](API.md#the-dashboard-login)), and `host` may be a non-loopback address.
  There is one account.
- `./sentinel.sh set-password` creates or changes it: the username (Enter keeps the current one,
  or `admin`), then the password twice without echo, three tries; `--user NAME` and
  `--password-stdin` (one line of standard input) for scripts. `./sentinel.sh install` runs it on
  a terminal while there is no login (`--no-login` skips it). `set-password --remove` empties it
  (it asks first, `--yes` does not), and is refused while `host` is a non-loopback address with
  nothing else protecting it, because Sentinel would then refuse to start. The signed-in admin
  changes the password on **More › Account** (`POST /api/account/password`). Nothing else changes
  it: `PATCH /api/config` refuses it like every `server` key but `timezone`.
- Both write the file the way a save from the UI does: the whole file is rewritten without
  comments (mode 0600), and values from the environment are never written into it.
- A login written with `set-password` applies without a restart: the running server looks at the
  config file at most every 2 s (at the next request), re-reads `server.login` when the file
  changed and signs every session out ("Password changed" or "Login removed" in the events log).
  A login removed by hand while Sentinel listens on a non-loopback `host` with no `auth_token`
  or `allow_unauthenticated` is kept until a restart (with a warning in the log), so the
  dashboard never opens on the network; the restart then refuses to start.
- Rules: the username is 1 to 64 letters, digits and `. - _ @` ("The username must be 1 to 64
  characters: letters, digits and . - _ @"); the password 12 to 256 characters and not the
  username in any letter case ("Use at least 12 characters.", "Use at most 256 characters.", "The
  password must differ from the username."). There are no other password rules.
- The hash is `scrypt$16384$8$1$<salt>$<hash>` (scrypt with n = 2^14, r = 8, p = 1, a 16-byte
  salt and a 32-byte hash, both base64url without padding), or
  `pbkdf2_sha256$600000$<salt>$<hash>` on a Python without `hashlib.scrypt`. `./sentinel.sh
  hash-password` prints one without touching the config (for the Ansible role's
  `dspace_sentinel_login_password_hash`). Never write the password itself here.
- A login that cannot be used (a hash that does not parse, a username outside the rules) never
  stops the start and stays on: the start-up notice "The login in server.login could not be read:
  run ./sentinel.sh set-password" goes to the log and the events log, and every sign-in answers
  HTTP 503 with that sentence. It never falls back to "no login", which would open the dashboard.
- `password_hash` is a secret: `GET /api/config` shows `"••••••"`, and the demo's settings file and
  the export never hold it.

### The time zone of messages and schedules

`server.timezone` sets the clock of everything Sentinel sends or schedules: the times in alert
and incident messages (Telegram and email), the times, chart ticks and file names of reports and
the caption or email that delivers one, the weekly report's weekday and hour, both channels'
quiet hours and daily digests, and the top row of `doctor`. Every such time names its zone: the
zone's abbreviation when it is a word (`UTC`, `CEST`: "Started 15:59 UTC"), else its UTC offset
("Started 21:29 +0530" for `Asia/Colombo`, whose abbreviation is only a number). The
dashboard's own times (Home, History, the component pages) stay in your browser's zone, and log
files keep theirs ([`logs.files[].timezone`](#logs)). `GET /api/meta` reports the zone in use and the server's own
([API.md](API.md#get-apimeta)).

A name this system cannot load is refused by the API (HTTP 400 `invalid configuration:
server.timezone: unknown time zone 'Mars/Base': use an IANA name such as Asia/Colombo, or leave it
empty for the server's zone`, plus ` (this system has no time zone data: install the tzdata
package)` on a system without any: it knows only `UTC` then), and so is a value that is not text
(`the time zone must be text, such as Asia/Colombo`). In the file such a name never stops the
start: Sentinel uses the server's zone and says so ([below](#times-and-the-email-login-in-the-file)).
In demo mode a zone chosen in the UI lasts until the demo stops, because the demo's settings file
never keeps `server.*`.

## `setup`

| Key | Default | Description |
|---|---|---|
| `completed` | `false` | Onboarding gate. The first-run wizard sets it to `true` through `POST /api/actions/setup`. Set it by hand to skip the wizard. |
| `persona` | `""` | What the wizard picked: `full`, `backend`, `frontend` or `server`. Informational. |

Personas are presets applied once during setup (`config.PERSONAS`). Each also sets the `ports`
rules it expects:

| Persona | Modules switched on | Ports expected |
|---|---|---|
| `full` | every module | 22, 80, 443, 4000, 8080 open; 8983, 5432 local only |
| `backend` | every module except `frontend` | 22, 80, 443, 8080 open; 8983, 5432 local only |
| `frontend` | `nginx`, `ssl`, `frontend`, `ports`, `security`, `logs` (nginx log only), `backend` (point `dspace.server_url` at the remote API) | 22, 80, 443, 4000 open |
| `server` | `ports`, `security`, `nginx`, `ssl`, `logs` | 22 open |

Every persona switches `security` on, and discovery suggests it too: it reads this server, whatever
runs on it. The wizard shows it pre-selected for every option, with "this server" as its evidence.

When discovery ran first, its suggestion replaces the persona's list with the rules whose ports
actually listen. The wizard also sends the modules it shows in step 2: nginx and SSL start
switched on there only when discovery found a web server (port 80 or 443, or an nginx or apache2
unit that is active or failed) and HTTPS (port 443, or an https `dspace.ui.url` in `local.cfg`),
so a server where DSpace is opened directly on :4000 starts with both off, also after **Skip for
now**. When discovery failed, the persona's modules stand.

## `collector`

| Key | Default | Range | Description |
|---|---|---|---|
| `system_interval` | `15` | 5–3600 s | CPU, memory, swap, disk, load, network samples. |
| `service_interval` | `30` | 5–3600 s | systemd unit checks and the port scan. |
| `http_interval` | `30` | 5–3600 s | REST API, Angular homepage, Solr, nginx and SSL checks. |
| `db_interval` | `60` | 5–3600 s | PostgreSQL connect, `SELECT 1` and connection statistics. |
| `logs_interval` | `60` | 5–3600 s | Error counting in log files. |
| `repository_interval` | `900` | 30–86400 s | Item, bitstream, community and collection counts. |
| `security_interval` | `900` | 60–86400 s | Security module: open ports, firewall, SSH, updates, software support, file modes (15 min). A group of its own; **Check now** runs it too. |
| `retention_days` | `30` | 1–3650 d | Raw samples, checks and component history. Hourly roll-ups, repository statistics and resolved alerts/incidents are kept 365 days regardless. |

Each collector runs with a hard timeout of `interval − 1` seconds, but never less than its own
budget (the sum of its rung timeouts plus slack: PostgreSQL 35 s, Solr and backend 25 s,
frontend 20 s, nginx 18 s, security 36 s) or 2 s. A collector that times out or raises produces one `unknown`
rung (`<id>.error`, for example "check timed out after 29 s") and the loop carries on; such a
cycle neither opens nor resolves alerts.

## `dspace`

| Key | Default | Description |
|---|---|---|
| `install_dir` | `/dspace` | DSpace installation directory (`[dspace]` in the DSpace docs). Discovery reads `<install_dir>/config/local.cfg` and `dspace.cfg`; the Security module checks who may read them (their mode, never their contents). |
| `auto_discover` | `true` | Allow **Detect automatically** / `discover` to read the DSpace config files. |
| `server_url` | `http://localhost:8080/server` | REST API base URL (`dspace.server.url`). Rungs: `GET <server_url>/api`, `/actuator/health` (asked at most every 5 minutes and graded the way your DSpace version defines it, see [COMPATIBILITY.md](COMPATIBILITY.md#the-actuator-anonymous-answers-and-the-seo-down-window)), `/api/discover/search/objects?size=1`. |
| `ui_url` | `http://localhost:4000` | Angular UI as served by Node (`dspace.ui.url`). The homepage rung expects HTTP 200 and the UI's `<ds-app>` element, or the `<meta name="Generator" content="DSpace …">` tag of a server-rendered page, in the body. |
| `public_url` | `""` | Public address such as `https://repo.example.org`, the host in the UI's `ui.baseUrl`. When set the frontend is also checked through nginx/TLS the way users see it, `ssl.host` defaults to this host, and, when its host differs from `ui_url`'s, the homepage request to `ui_url` carries `Host: <that host>` (plus `X-Forwarded-Proto: https` for an https address). DSpace 9.3+ renders on the server only for that host ([COMPATIBILITY.md](COMPATIBILITY.md#the-frontend-server-side-rendering-and-the-93-host-rule)). The Security module asks it once per check for its response headers (HTTPS and the security headers); without it, it warns when DSpace's own ports 4000 or 8080 are reachable from the network. |
| `assetstore_dir` | `/dspace/assetstore` | Where bitstreams live (`assetstore.dir`). Used by the assetstore module. |
| `log_dir` | `/dspace/log` | DSpace log directory. Informational; the logs module uses `logs.files`. |
| `backend_service` | `tomcat9` | systemd unit running the REST API: `tomcat9` (DSpace 7, Ubuntu 22.04), `tomcat10` (DSpace 8 and 9 on Ubuntu 24.04 or Debian 12/13), or the unit you wrote for the runnable JAR of DSpace 8+ (`dspace`, `dspace-backend`, …). **Detect automatically** finds it. `""` skips the systemd rung. |
| `frontend_service` | `dspace-ui` | systemd unit running the Angular UI: a unit of its own, or `pm2-<user>`, the unit `pm2 startup` installs (it only proves the PM2 daemon runs). `""` skips the rung. |
| `verify_tls` | `true` | Set `false` for self-signed certificates on the DSpace, Solr, nginx and certificate checks. It never applies to the webhook, which always verifies TLS. |

Every systemd unit name in this file (`dspace.backend_service`, `dspace.frontend_service`,
`postgres.service`, `solr.service`, `nginx.service`) may be set to `""`: the ladder then starts
at the TCP rung and no `systemctl` is ever run for that service. Use it in containers, on hosts
without systemd, for a service that runs on another machine, or for one started without a unit
(Solr from `bin/solr start`, the UI under `node`, or PM2 without `pm2 startup`). **Detect
automatically** suggests `""` itself when such a service answers on its port (Solr on 8983, the
UI on 4000, a web server on 80 or 443) but systemd has no unit for it, and says so under *What we
found*: "Solr answers on 8983 but no systemd unit was found for it: it may not start by itself
after a reboot". Any other value must be a unit
name: 1–128 letters, digits and `@ _ . : -`, not starting with `-`. Anything else is refused
(`not a systemd unit name: …`): the API answers 400 and a hand-edited file fails to load.

## `postgres`

| Key | Default | Description |
|---|---|---|
| `host` | `localhost` | |
| `port` | `5432` | 1–65535 |
| `database` | `dspace` | |
| `user` | `dspace` | A dedicated read-only role works fine; the session is opened `READ ONLY` anyway. |
| `password` | `""` | Secret. Prefer `SENTINEL_PG_PASSWORD` in [`config/sentinel.env`](#configsentinelenv). Changing `host`, `port`, `database` or `user` while a password is stored needs the password again ([Secrets](#secrets)). |
| `sslmode` | `prefer` | `disable`, `allow`, `prefer`, `require`, `verify-ca`, `verify-full` |
| `service` | `postgresql` | systemd unit (`postgresql`, `postgresql@15-main`, …). `""` skips the rung. |

Every connection uses `connect_timeout=5`, `statement_timeout=3000`,
`default_transaction_read_only=on` and `application_name='dspace-sentinel'`, and is closed at the
end of the cycle.

## `solr`

| Key | Default | Description |
|---|---|---|
| `url` | `http://localhost:8983/solr` | `solr.server` in `local.cfg`. |
| `cores` | `[search, statistics, oai, authority]` | One ping rung per core (`solr.core.<name>`); the `search` core also gets a timed `select?q=*:*&rows=0`. With `solr.multicorePrefix` in `local.cfg`, list the prefixed names (`dspace_search`, …); the search query then goes to `<prefix>search`. |
| `service` | `solr` | systemd unit; `""` skips the rung. |

The optional cores of DSpace 8 (`qaevent`, `suggestion`) and 10 (`audit`) need not be listed:
when Solr has one (behind the same prefix), it gets a rung of its own, and a problem with it is at
most a warning. A core Solr could not load is critical with Solr's reason ("failed to load: …")
and is neither pinged nor searched. See
[COMPATIBILITY.md](COMPATIBILITY.md#solr-8-or-9-optional-cores-and-prefixes).

## `nginx`

| Key | Default | Description |
|---|---|---|
| `service` | `nginx` | systemd unit; use `apache2` if that is your web server, `""` to skip the rung. |
| `http_url` | `http://localhost` | Plain HTTP request through the web server in front of DSpace, not the DSpace UI's own port (:4000), which the DSpace Frontend check covers. |
| `https_url` | `""` | For example `https://repo.example.org`; adds the HTTPS rung when set. |

No web server on the machine (DSpace opened directly on :4000)? Switch the nginx module off
(**More › Modules**). While it is on, a machine where systemd knows no such unit and nothing
answers on `http_url` keeps the HTTP rung critical ("connection refused (localhost:80)"), with a
hint on the nginx page: "No web server was found on this machine: there is no nginx unit and
nothing answers on localhost:80. If DSpace is opened directly on its own port (for example
:4000), switch this check off in More › Modules. If it runs behind Apache or another unit, set its
name and address in More › DSpace connection."

## `ssl`

| Key | Default | Description |
|---|---|---|
| `host` | `""` | Host whose certificate is inspected. Empty = host part of `dspace.public_url`. |
| `port` | `443` | 1–65535 |

Thresholds live in `alerts.ssl_expiry_days`.

## `ports`

A list of rules. Each rule:

| Key | Default | Description |
|---|---|---|
| `port` | required | 1–65535 |
| `label` | `""` | Shown as `<label> :<port>`. |
| `expect` | `open` | `open`: closed ⇒ warn. `local_only`: listening on `0.0.0.0`, `::` or another non-loopback address ⇒ warn "exposed publicly (0.0.0.0, ::)", with a hint on what to do (Sentinel sees where a port listens, not your firewall); closed ⇒ unknown. An IPv4-mapped address counts as the IPv4 address it maps: `::ffff:127.0.0.1`, as Java reports a socket on 127.0.0.1, is loopback. |

The hint of an exposed `local_only` port says why it matters, how to close the port and how to
accept it. For Solr (a label with "solr", or the port of `solr.url`): "Solr has no login: anyone
who can reach this port can read and change the search index. Make Solr listen on localhost only,
or block the port in the firewall (Sentinel sees where it listens, not your firewall). To accept
it, change this rule to "open" in config/sentinel.yaml (ports: port 8983, expect: open) and
restart Sentinel." PostgreSQL (a label with "postgres", or `postgres.port`) and any other port get
a sentence of their own. A firewall rule does not end the warning, because Sentinel cannot see
it: once the port is closed to other machines that way, set its rule to `expect: open` in
`config/sentinel.yaml` and restart Sentinel. The rules are changed only there (or with `PATCH
/api/config`); **More › DSpace connection** has no port rules.

Default list:

```yaml
ports:
  - {port: 22,   label: SSH,        expect: open}
  - {port: 80,   label: HTTP,       expect: open}
  - {port: 443,  label: HTTPS,      expect: open}
  - {port: 4000, label: Angular,    expect: open}
  - {port: 8080, label: Backend,    expect: open}
  - {port: 8983, label: Solr,       expect: local_only}
  - {port: 5432, label: PostgreSQL, expect: local_only}
```

Lists are replaced, not merged, when patched: send the whole list.

The Security module reads these rules too. On its Security page a port that other machines can
reach and that no rule names is *exposed* (a warning), or *new* when it opened after Sentinel
started (a warning and the alert "New port open to the network"); a port with a rule is
*expected*, one on loopback only *local*, and one that other machines reach only on Tailscale's
addresses (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`) *Tailscale*, "reachable through Tailscale
only", which is not a warning. To accept such a port, add a rule for it (`expect: open`) and
restart Sentinel. Sentinel's own port, the DHCP clients' UDP 68 and 546 and, while the
`tailscale0` interface exists, Tailscale's WireGuard port UDP 41641 on every interface never need
one.

## `logs`

| Key | Default | Description |
|---|---|---|
| `files` | see below | Log files to watch. |
| `window_minutes` | `5` | Sliding window (1–1440) over which errors are counted. |

Each file entry:

| Key | Default | Description |
|---|---|---|
| `id` | required | Short slug (letters, digits, `-`, `_`); becomes the rung `logs.<id>`. |
| `label` | `""` | Shown as `<label> log`. |
| `path` | required | Absolute path. A missing file yields `unknown`, never a warning. An unreadable one (or one behind a folder Sentinel may not enter) is `unknown` with "permission denied (/dspace/log/dspace.log)" and a hint that names the file's (or the folder's) owner, group and mode and the group to join, for example "The file belongs to tomcat:tomcat (mode 640): add sentinel to the group tomcat, log out and in again, then restart Sentinel." |
| `daily_suffix` | `false` | `true` follows DSpace's daily log files: DSpace 7–10 write `dspace.log` and roll it over to `dspace.log-YYYY-MM-DD`, DSpace 6 wrote `dspace.log.YYYY-MM-DD`. Today's dotted file is read when it exists; otherwise the most recently written of the plain file and its dated files (either spelling), the plain file winning a tie. |
| `timezone` | `""` | Zone of the file's timestamps. `""` = this machine's local time, unless the newest line only matches the file's modification time when read as UTC (Solr's default `-Duser.timezone=UTC`); then the file is read as UTC. `local` turns that detection off; `UTC` or an IANA name such as `Asia/Colombo` fixes the zone. An unknown name is refused (`unknown time zone …`). It is independent of [`server.timezone`](#the-time-zone-of-messages-and-schedules), the zone of the times Sentinel sends. |

```yaml
logs:
  files:
    - {id: dspace, label: DSpace,  path: /dspace/log/dspace.log, daily_suffix: true}
    - {id: tomcat, label: Tomcat,  path: /var/log/tomcat9/catalina.out}
    - {id: solr,   label: Solr,    path: /var/solr/logs/solr.log}
    - {id: nginx,  label: nginx,   path: /var/log/nginx/error.log}
  window_minutes: 5
```

The Tomcat entry is Ubuntu 22.04's `tomcat9` path. DSpace 8 and 9 under `tomcat10` log to
`/var/log/tomcat10/catalina.out`, and the runnable JAR writes no `catalina.out` (its messages go
to `dspace.log`); **Detect automatically** moves or drops the entry to match, and adds
`{id: dspace-cli, label: DSpace CLI, path: <log dir>/dspace-cli.log, daily_suffix: true}` when
that file exists (DSpace 7.6.3+ and 8.1+ scripts and cron jobs log there). On a DSpace host it
also keeps the nginx entry only when it found a web server or the log is there, and the Solr entry
only when `/var/solr/logs/solr.log` is there; otherwise the Solr entry follows the
`-Dsolr.log.dir` of a Solr started by hand (`<dir>/solr.log`) or is left out, each with a line
under *What we found*.

The `nginx` entry is read only while the nginx module is on: with the module off it has no rung
and no row in the Logs page's *Log files* table (the *Files watched* tile, which lists the
settings, still shows it).

Only the last 256 KB of each file are read. Timestamps in DSpace/log4j, Solr, Tomcat JULI,
packaged Tomcat's `catalina.out` (`[2026-09-26 10:41:02] [crit]…`, written through rsyslog) and
nginx formats are recognised. A line without a recognised timestamp belongs to the entry above it
and is never counted, which is how a stack trace counts once; a file whose lines carry no
timestamp Sentinel recognises counts no errors at all. Lines stamped more than 5 minutes after now
come from a zone mismatch and are never counted. When a file is not read in local time, its rung
says which zone was used (`UTC (detected)` or the configured name).

## `alerts`

| Key | Default | Description |
|---|---|---|
| `confirm_samples` | `2` | Consecutive breaching observations before an alert opens (1–100). Anti-flap. |
| `clear_samples` | `3` | Consecutive healthy observations before it resolves (1–100). |
| `reminder_minutes` | `0` | Re-notify every N minutes while open and unacknowledged (0–10080). `0` = only on open, escalate and resolve. |

Thresholds are `{warning, critical}` pairs. "Higher is worse" unless marked inverted.

| Key | Default | Unit / source |
|---|---|---|
| `cpu` | `{warning: 80, critical: 95}` | % |
| `memory` | `{warning: 80, critical: 92}` | % |
| `swap` | `{warning: 50, critical: 80}` | % (ignored when there is no swap) |
| `disk` | `{warning: 80, critical: 90}` | % of `/` and of every extra mount |
| `load_per_core` | `{warning: 1.5, critical: 3.0}` | `load1 / cores` |
| `rest_latency_ms` | `{warning: 1000, critical: 3000}` | `backend.rest` |
| `search_latency_ms` | `{warning: 1500, critical: 4000}` | `backend.search` and `solr.search` |
| `home_latency_ms` | `{warning: 2000, critical: 5000}` | `frontend.home` |
| `postgres_connections_pct` | `{warning: 80, critical: 95}` | connections / `max_connections` |
| `ssl_expiry_days` | `{warning: 30, critical: 7}` | days, **inverted** (lower is worse) |
| `log_errors_per_window` | `{warning: 20, critical: 100}` | errors per file per `logs.window_minutes` |
| `assetstore_free_gb` | `{warning: 50, critical: 10}` | GB, **inverted** |

The same thresholds draw the hairlines on the History charts and colour the server tiles. See
[ALERTS.md](ALERTS.md) for the state machine.

## `notifiers`

### `notifiers.telegram`

| Key | Default | Description |
|---|---|---|
| `enabled` | `false` | **Send alerts to Telegram**: the switch for every Telegram delivery (alerts, incidents, reminders, the daily digest and reports). Tests (**Send test message**, `telegram-test`) are sent even while it is off. |
| `chats` | `[]` | Up to five destinations, each its own bot writing to one chat (below). |
| `quiet_hours` | `""` | `"23:00-07:00"` (in the zone of [`server.timezone`](#the-time-zone-of-messages-and-schedules), the server's own when empty; may wrap midnight): inside the window only critical alert and incident messages go to Telegram. Warning-level messages, including the resolution of a warning and warning-only incidents, are dropped, not delayed. The digest, tests and the webhook are not affected. An en or em dash, spaces around it and single-digit hours are accepted and saved as `HH:MM-HH:MM`. The API refuses anything else (`quiet hours must look like 23:00-07:00`, also for digits other than 0–9 and values over 40 characters) and a window whose two times are the same (`quiet hours must start and end at different times`); in the file such a value is left out at start-up ([below](#times-and-the-email-login-in-the-file)). |
| `daily_digest` | `""` | `"08:00"` (in the zone of `server.timezone`, like the quiet hours): one summary message per day to every bot, posted to the webhook as well. `""` = off. `8:00` is saved as `08:00`; the API refuses anything else (`the daily digest time must look like 08:00`), and the file leaves it out at start-up ([below](#times-and-the-email-login-in-the-file)). |

Each `chats` entry:

| Key | Default | Description |
|---|---|---|
| `id` | set on the first save | Eight hexadecimal characters that identify the entry; keep them, because the UI and the API use them to keep each entry's token. A hand-written id (up to 32 letters, digits, `-` and `_`) is kept; `env` is reserved for the bot from the environment. |
| `label` | `""` | The person's name, up to 40 characters on one line. |
| `bot_token` | `""` | Secret: the token @BotFather gave for this bot (`123456789:` and 30–50 more characters). |
| `chat_id` | `""` | Where the bot writes: a person's id, a group's (negative) or `@channelname`. Numbers are stored as strings. |
| `min_severity` | `warning` | `warning` (All alerts) or `critical` (Critical only: critical alert and incident messages). Tests, digests and reports go to every entry. |

An entry without both a token and a chat id is kept but not sent to. Five entries at most, the
bot from `SENTINEL_TELEGRAM_TOKEN` + `SENTINEL_TELEGRAM_CHAT_ID` included ("Telegram can send to
at most 5 bots"); the same bot and chat twice is refused ("Telegram chat 987654321 is listed
twice with the same bot"). The single `bot_token` and `chat_id` keys directly under `telegram`,
from before Sentinel had several bots, still load as one entry; a save writes `chats`.

### `notifiers.email`

| Key | Default | Description |
|---|---|---|
| `enabled` | `false` | **Send alerts by email**: the switch for every email delivery. Tests (**Send test email**, `email-test`) are sent even while it is off. |
| `smtp_host` | `smtp.gmail.com` | Host name or IP address of the mail server. |
| `smtp_port` | `587` | 1–65535: 587 for STARTTLS, 465 for SSL. |
| `security` | `starttls` | `starttls`, `ssl` (implicit TLS) or `none`. Certificates are verified with the system's CA store. With `none` a password is only ever sent to a server on this machine; a relay that needs no login works on any host. |
| `username` | `""` | The login: for Gmail, the Gmail address. `""` for a relay that needs no login. A login that is not an email address (SendGrid's `apikey`, an Amazon SES key id, a Postmark token) needs `from_address`: the API refuses to save it without one (`notifiers.email: the SMTP username is not an email address: set from_address (the address alerts come from)`), and a file that holds one loads with email not ready ([below](#times-and-the-email-login-in-the-file)). Letters, digits and symbols from the English keyboard only: another character is refused when saved (`notifiers.email: the SMTP username has a character that cannot be sent: use letters, digits and symbols from the English keyboard`), and one in this file loads with email not ready ([below](#times-and-the-email-login-in-the-file)). |
| `password` | `""` | Secret. For Gmail an app password (16 letters; the spaces Google shows are removed), never the account's own password. Only sent to the server and login it was saved for ([Secrets](#secrets)). Prefer `SENTINEL_SMTP_PASSWORD` in [`config/sentinel.env`](#configsentinelenv), which makes it read-only. Letters, digits and symbols from the English keyboard only: saving any other character (an `ä`) through **More › Email** or the API is refused (400) with `the email password has a character that cannot be sent: use letters, digits and symbols from the English keyboard`. One in this file or in `SENTINEL_SMTP_PASSWORD` still lets Sentinel start, but email is not ready and every start says so (`email is not ready: the email password has a character that cannot be sent: …`, [below](#times-and-the-email-login-in-the-file)). The mask `••••••` pasted here means no password. |
| `from_address` | `""` | The sender's address; `""` = `username`. The UI saves `""` with the Gmail choice: Gmail sends as the account that logs in. |
| `from_name` | `DSpace Sentinel` | The sender's name, up to 60 characters on one line. |
| `recipients` | `[]` | Up to five `{address, label, min_severity}`: one plain address each (no display name, spaces or commas), a label of up to 40 characters, and `warning` or `critical` as for Telegram. An address may appear once, whatever its letter case. |
| `quiet_hours` | `""` | As for Telegram, applied to email only. |
| `daily_digest` | `""` | `"08:00"`: one summary email per day while email is on. `""` = off. Checked like Telegram's, and left out of the file like Telegram's. |

Email can send once it has `smtp_host`, a sender that is an email address (`from_address`, else
`username`), the password when a username is set, and at least one recipient.

### Times and the email login in the file

The API and the forms refuse the values below, but a hand-edited `config/sentinel.yaml` that
holds one does not stop Sentinel from starting: it starts anyway, logs the sentence as a warning
(`doctor` and `status` print it too) and writes it as one `config` row to the events log at every
start (`GET /api/events?kind=config`). Any other value that does not validate still stops the
start ([validation errors](#validation-errors)).

- A `quiet_hours` or `daily_digest` that Sentinel cannot read (`"11pm-7am"`, `"8 am"`, digits
  other than 0–9, more than 40 characters, or quiet hours whose two times are the same) is taken
  as `""`: no quiet hours or no digest on that channel. The row names the key and the value: `The quiet hours
  '11pm-7am' in notifiers.telegram.quiet_hours were ignored: quiet hours must look like
  23:00-07:00`.
- Keep the quotes around every time; Sentinel writes them itself when it saves the file.
  Unquoted, YAML reads `daily_digest: 18:00` as the number 1080. A whole number from 60 to 1439
  is read back as the time it was written as (1080 is 18:00); any other number is left out as
  above, and its row adds ` (YAML reads an unquoted time as a number: write it in quotes)`.
- A `server.timezone` that names no zone this system knows (`Mars/Base`, a typo, or any name but
  `UTC` on a system without time zone data) is taken as `""`: times, quiet hours, digests and the
  weekly report use the server's zone. The row reads `The time zone in server.timezone was
  ignored, so times use the server's zone: unknown time zone 'Mars/Base': use an IANA name such as
  Asia/Colombo, or leave it empty for the server's zone`.
- A `username` that is not an email address, without `from_address`, loads as it is, whether
  email is on or off. Email is then not ready: no alert email is sent, the test email names the
  reason without connecting, and the row reads `Email is not ready: the SMTP username is not an
  email address: set from_address (the address alerts come from)`. The API goes on refusing to
  save the email settings (HTTP 400) while the login has no `from_address`; a patch that does not
  touch `notifiers.email` is not held up by it.
- A `password` or `username` with a character SMTP cannot send (outside ASCII, such as an `ä`)
  loads the same way, also a password from `SENTINEL_SMTP_PASSWORD`: email is not ready, and the
  row reads `Email is not ready: the email password has a character that cannot be sent: use
  letters, digits and symbols from the English keyboard` (or the same about the SMTP username).
  The password counts only with a `username` (a relay without a login is never sent one). While
  this file holds such a password, the API refuses to save the email settings until a new one is
  typed; one from `SENTINEL_SMTP_PASSWORD`, which the API cannot change, never holds up a save.

### `notifiers.webhook`

| Key | Default | Description |
|---|---|---|
| `enabled` | `false` | |
| `url` | `""` | Secret (masked like the others). Receives a JSON `POST` for every alert and incident event and Telegram's daily digest; no quiet hours, no tests, no reports. The destination's TLS certificate is always verified, whatever `dspace.verify_tls` says, and redirects are never followed (a 3xx answer is recorded as `HTTP 302 from webhook (redirects are not followed)`). |

Formats and setup: [NOTIFICATIONS.md](NOTIFICATIONS.md).

## `reports`

| Key | Default | Description |
|---|---|---|
| `weekly.enabled` | `false` | Generate a report automatically once a week. It runs from the scheduler hook of a running `sentinel serve`; nothing is generated while Sentinel is stopped (no cron job is installed). |
| `weekly.weekday` | `1` | 0–7; `1` = Monday … `7` = Sunday (ISO); `0` is also Sunday. The day is read in the zone of `server.timezone`. |
| `weekly.hour` | `8` | 0–23, in the zone of [`server.timezone`](#the-time-zone-of-messages-and-schedules) (the server's own when empty). |
| `weekly.send_telegram` | `false` | Also send the file through every Telegram bot (`sendDocument`). |
| `weekly.send_email` | `false` | Also email the file to every recipient, as an attachment. |
| `default_sections` | `[summary, server, services, latency, alerts, repository, security, recommendations]` | Sections preselected in the report panel and used by the weekly job. `logs` and `incidents` are also available. `security` ("🛡️ Security") says "The Security module is off." while that module is off. The panel offers only the sections whose module is on; a hidden default stays in this list when you save. |

## `modules`

Every module is `false` by default. The server module (CPU, memory, disk, load) is always on. A
module that is off leaves no trace: no checks, no tiles, no charts, no alerts.

| Key | What it turns on |
|---|---|
| `ports` | Which ports are open, and which should stay local only. |
| `security` | Open ports, firewall, SSH, updates and software support: read-only. Off by default like every module; every first-run option and discovery switch it on, and the demo has it on. It never opens or closes a port or changes a firewall ([SECURITY.md](../SECURITY.md#what-the-security-module-reads)). |
| `postgres` | Checks the database really answers queries, not just that the service runs. |
| `solr` | Pings every core and times a real search. |
| `backend` | REST API, actuator health and its database and Solr dependencies. |
| `frontend` | Loads the Angular homepage and times it. |
| `nginx` | HTTP and HTTPS through the web server. |
| `ssl` | Days until your certificate expires. |
| `logs` | Counts new errors in DSpace, Tomcat, Solr and nginx logs. |
| `repository` | Items, bitstreams, communities, collections and storage growth. |
| `assetstore` | Free space where your files live. |
| `incidents` | Groups related alerts into one story with a hypothesis. |

`PATCH /api/modules {"postgres": true}` is a shortcut for `PATCH /api/config
{"modules": {"postgres": true}}`. When any of `postgres`, `solr`, `backend`, `frontend`,
`repository` or `assetstore` is on, the Stack tab is called "Stack"; otherwise "Services".

## `appearance`

| Key | Default | Description |
|---|---|---|
| `theme` | `system` | `system`, `light` or `dark`. Persisted server-side and mirrored in the browser. |

---

## Validation errors

An invalid patch is rejected with HTTP 400 and a readable message, for example:

```json
{"error": "invalid configuration: collector.system_interval: Input should be greater than or equal to 5"}
{"error": "invalid configuration: notifiers.telegram.chats.0.chat_id: not a Telegram chat id: 'me'"}
{"error": "invalid configuration: notifiers.email.recipients.0.address: not an email address: 'Ops <ops@example.org>'"}
{"error": "invalid configuration: notifiers.email.quiet_hours: quiet hours must look like 23:00-07:00"}
{"error": "invalid configuration: notifiers.telegram.quiet_hours: quiet hours must start and end at different times"}
{"error": "invalid configuration: notifiers.email: the email password has a character that cannot be sent: use letters, digits and symbols from the English keyboard"}
{"error": "invalid configuration: server.timezone: unknown time zone 'Mars/Base': use an IANA name such as Asia/Colombo, or leave it empty for the server's zone"}
```

Unknown keys are ignored (`extra="ignore"`), so a typo in a key name silently does nothing;
check `GET /api/config` after editing by hand. A hand-edited file that does not validate stops
Sentinel from starting (`invalid configuration in <path>: …`, exit code 4), except for the
notification times, the time zone and the email login, which are reported instead
([above](#times-and-the-email-login-in-the-file)), and a sixth Telegram bot from the environment,
which is left out ([environment variables](#environment-variables)).

## Full default file

The commented example in [`config/sentinel.example.yaml`](../config/sentinel.example.yaml) lists
every key with its default value (the test suite checks that it matches the built-in defaults)
and is the best starting point for a hand-written file. Its comments survive only as long as you
edit by hand: the first save from the UI rewrites the file without them.
