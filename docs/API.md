# HTTP API

Everything the UI shows is available as JSON from the same server, on the same port. The
examples below were captured from a demo instance (`SENTINEL_DEMO=1`) and trimmed with `…`.

## Conventions

- Base URL: `http://127.0.0.1:9765`. All routes are relative-URL friendly, so a reverse-proxy
  prefix such as `/sentinel/` works without configuration.
- Timestamps are **epoch seconds** as numbers (`1790246530.35`). Durations are seconds, sizes
  bytes, latencies milliseconds, unless a key says otherwise.
- Errors are `{"error": "message"}` with a 4xx/5xx status: 400 for validation problems and an
  unknown `Host`, 401 without a session or valid credentials, 403 for a cross-site write, 404 for
  unknown ids, 409/410 for report files that are purged or gone, 415 for a write whose body is not
  JSON, 429 while an export or a report generation is already running and after too many failed
  sign-ins (with `Retry-After`), 500 for an unexpected error (`"internal error; the Sentinel log
  has the details"`: the details go to the log, never to the client), 501 for features not in
  this build, 503 for a sign-in while the stored login cannot be read, 504 for a discovery
  timeout and 507 when the data filesystem is too full for a report.
- **Host check.** Every request except `/healthz` must name a host Sentinel knows in its `Host`
  header: IP addresses, `localhost`, the machine's hostname, `server.hostname`, `server.host`,
  `ssl.host`, the hosts of the configured DSpace and nginx URLs, and `server.allowed_hosts`
  ([CONFIG.md](CONFIG.md#server)). Any other name answers
  `400 {"error": "unknown host 'evil.example.com': add it to server.allowed_hosts in config/sentinel.yaml if Sentinel is reached under that name"}`.
  This keeps DNS-rebinding pages from using a browser to read or change Sentinel.
- **Authentication** depends on what is set:
  - **The dashboard login** (`server.login`, [CONFIG.md](CONFIG.md#the-dashboard-login-serverlogin);
    never in demo mode). `/healthz`, the page (`/`, `/index.html`, `/favicon.svg`, `/static/…`),
    `GET /api/session` and `POST /api/login` are open. Every other request needs one of: the
    session cookie `sentinel_session` from [`POST /api/login`](#post-apilogin),
    `server.auth_token` (`Authorization: Bearer <token>`, or HTTP Basic with any user name and the
    token as password), or the local CLI token (`Authorization: Bearer <token>` with the token the
    running server wrote to `<data dir>/api-token`, mode 0600, new on every start; `./sentinel.sh
    status` uses it). Otherwise it answers `401 {"error": "login required"}` **without** a
    `WWW-Authenticate` header, so a browser shows the sign-in page instead of its own pop-up.
  - **Only `server.auth_token`.** Everything except `/healthz`, `GET /api/session` and
    `POST /api/login` needs the token, as Basic (any user name) or Bearer, or the CLI token. A
    missing or wrong token answers `401 {"error": "authentication required"}` with
    `WWW-Authenticate: Basic realm="DSpace Sentinel"`, so the browser asks for it.
  - **Neither** (Sentinel on `127.0.0.1` only): every route is open.
- **Writes** (`POST`, `PUT`, `PATCH`, `DELETE`) are refused with
  `403 {"error": "cross-site request refused: open Sentinel's own page to make changes"}` when
  `Sec-Fetch-Site` is `cross-site` or `same-site`, or when an `Origin` header (also `null`)
  matches neither `Host` nor `X-Forwarded-Host` (a `Host` without a port matches any origin
  port). A body must be JSON: any other `Content-Type` answers
  `415 {"error": "unsupported content type: send application/json (or no body)"}`. curl and
  scripts send no `Origin` and keep working; for a body-less action leave out `-d` (curl's
  `-d ''` sends a form content type) or add `-H 'Content-Type: application/json'`.
- Secrets (`postgres.password`, `server.auth_token`, `server.login.password_hash`,
  `notifiers.email.password`, `notifiers.webhook.url` and the `bot_token` of every
  `notifiers.telegram.chats` entry) are always returned as `"••••••"`.
- Status values: `ok`, `warn`, `crit`, `unknown`, `off`. Severity values: `warning`, `critical`.
- The OpenAPI document is at `GET /api/openapi.json` (no Swagger UI is mounted).

```bash
curl -s http://127.0.0.1:9765/api/status | jq .headline                            # no login, no token
curl -s -H "Authorization: Bearer $SENTINEL_AUTH_TOKEN" http://127.0.0.1:9765/api/status   # with auth_token set
curl -su any:"$SENTINEL_AUTH_TOKEN" http://127.0.0.1:9765/api/status                 # the same as HTTP Basic
curl -s -X POST http://127.0.0.1:9765/api/actions/check-now | jq .mood
```

## Route table

| Method & path | Purpose |
|---|---|
| `GET /healthz` | liveness; no auth, `Host` or cross-site check |
| `GET /api/session` | is a login required, is this browser signed in (always open) |
| `POST /api/login` · `POST /api/logout` | sign in (sets the session cookie) · sign out |
| `POST /api/account/password` | the signed-in admin changes the password |
| `GET /api/meta` | version, hostname, onboarding state, modules, theme, time zone |
| `GET /api/status` | everything the Home tab needs |
| `GET /api/system` | latest system sample only |
| `GET /api/components` · `GET /api/components/{id}` | component health, ladders |
| `GET /api/history?metric=&range=` | bucketed server metrics |
| `GET /api/history/latency?check=&range=` | bucketed latency of one rung |
| `GET /api/availability?range=` | availability strips |
| `GET /api/repository` | repository counters, 30-day series |
| `GET /api/events?limit=&component=&kind=` | audit trail |
| `GET /api/export?range=` | JSON dump of the history, streamed |
| `GET /api/alerts?status=&limit=` · `GET /api/alerts/{id}` | alerts |
| `POST /api/alerts/{id}/ack` · `POST /api/alerts/{id}/unack` | acknowledge |
| `GET /api/incidents?status=&limit=` · `GET /api/incidents/{id}` | incidents |
| `GET /api/reports?include_deleted=&limit=` · `POST /api/reports` | list, generate |
| `GET /api/reports/options` | sections, formats, ranges, Telegram and email availability for the report panel |
| `GET /api/reports/{id}?meta=&download=` · `DELETE /api/reports/{id}` · `POST /api/reports/{id}/restore` | file or row, soft delete, undo |
| `GET /api/config` · `PATCH /api/config` | configuration |
| `GET /api/modules` · `PATCH /api/modules` | module toggles |
| `POST /api/actions/check-now` | run the checks now (coalesced) |
| `POST /api/actions/telegram-test` | send the test message through every Telegram bot, or one |
| `POST /api/actions/email-test` | send the test email to every recipient, or one |
| `POST /api/actions/discover` | look around the machine |
| `POST /api/actions/setup` | finish onboarding |
| `POST /api/actions/clear-history` | wipe history |
| `GET /api/stream` | Server-Sent Events |
| `GET /` · `GET /static/*` · `GET /favicon.svg` | the web UI |

---

## Liveness and metadata

### `GET /healthz`

```json
{"ok": true}
```

The only path without any check at all: no authentication, `Host` check or cross-site check
(exactly `/healthz`, after any reverse-proxy prefix). The page, its static files and the two
sign-in routes below need no session either, but do get the `Host` and cross-site checks.

### `GET /api/meta`

```json
{
  "version": "0.1.0",
  "hostname": "demo-dspace",
  "setup_completed": false,
  "persona": "",
  "demo": true,
  "modules": {"ports": true, "security": true, "postgres": true, "solr": true, "backend": true,
              "frontend": true, "nginx": true, "ssl": true, "logs": true, "repository": true,
              "assetstore": true, "incidents": true},
  "dspace_modules_enabled": true,
  "started_ts": 1790246498.09,
  "theme": "system",
  "retention_days": 30,
  "system_interval": 15,
  "timezone": {"name": "Asia/Colombo", "abbreviation": "+0530",
               "server_name": "Etc/UTC", "server_abbreviation": "UTC"},
  "db_size_bytes": 20518664
}
```

`setup_completed: false` tells the UI to show the onboarding wizard. `dspace_modules_enabled`
decides whether the second tab is called "Stack" or "Services". `db_size_bytes` is the size of
`sentinel.db` (shown under More › About); it is `null` when the file cannot be measured.
`timezone` is the zone of the times Sentinel sends and of its quiet hours, digests and weekly
report (`server.timezone`, [CONFIG.md](CONFIG.md#the-time-zone-of-messages-and-schedules)):
`name` and `abbreviation` are the zone in use (its abbreviation now, or its UTC offset when the
zone has no word for it, as `+0530`), `server_name` and `server_abbreviation` the server's own
zone. With `server.timezone` empty both pairs name the server's zone. The More pages use it for
"empty = the server's zone (UTC)" and ", in Asia/Colombo (+0530)".

## The dashboard login

With `server.login` set (see [Authentication](#conventions)), the web UI asks `GET /api/session`
first and shows its sign-in page until the browser has a session; a `401` from any other route
then means the session ended, and the UI shows the sign-in page again with "Your session ended.
Sign in again.". These four routes are the whole login. The `Host` allowlist and the cross-site
checks apply to them as to every other route, so sign in from Sentinel's own page, or with curl
and a JSON body.

### `GET /api/session`

Always open: no session, token or login needed.

```json
{"login_required": true, "authenticated": true, "username": "admin", "expires_ts": 1790850000.0}
```

`login_required` is `false` without a login and in demo mode (`authenticated` is then `false`,
`username` and `expires_ts` are `null`). With a login, `authenticated` says whether the request
carries a valid session cookie (then `username` and `expires_ts`, the session's absolute end, are
filled in) or a valid `server.auth_token` or CLI token (`authenticated: true` with `username:
null`). Sent with `Cache-Control: no-store`. An older Sentinel without this route answers 404,
which the UI reads as "no login".

### `POST /api/login`

Open. Body `{"username": "admin", "password": "…", "remember": false}`; `remember: true` is *Keep
me signed in for 30 days*. The username ignores letter case and spaces around it.

```bash
curl -s -c cookies.txt -H 'Content-Type: application/json' \
  -d '{"username": "admin", "password": "a made-up long passphrase", "remember": false}' \
  http://127.0.0.1:9765/api/login
curl -s -b cookies.txt http://127.0.0.1:9765/api/status | jq .headline
```

| Status | Body | When |
|---|---|---|
| 200 | `{"authenticated": true, "username": "admin", "expires_ts": 1790850000.0}` and the cookie | signed in |
| 401 | `{"error": "Wrong username or password."}` | a wrong password or an unknown username: the same answer, after the same time |
| 429 | `{"error": "Too many attempts. Try again in 16 s.", "retry_after": 16}` and `Retry-After: 16` | too many failures (below) |
| 400 | `{"error": "No login is set up on this Sentinel."}` | no login is configured, and always in demo mode |
| 400 | `{"error": "send the fields as text"}` | `username` or `password` is not a string |
| 503 | `{"error": "The login in server.login could not be read: run ./sentinel.sh set-password"}` | the stored login cannot be used ([CONFIG.md](CONFIG.md#the-dashboard-login-serverlogin)) |

The cookie is `sentinel_session`: `HttpOnly`, `SameSite=Strict`, `Path=/` (the app's root path),
`Max-Age` = the seconds until the session's absolute end, and `Secure` when the request came over
HTTPS (uvicorn takes the scheme from `X-Forwarded-Proto` when nginx runs on the same machine). The
server keeps only the SHA-256 of the token. A session ends after 12 hours without a request and 7
days after signing in at the latest; with `remember: true` both limits are 30 days. `expires_ts`
is the absolute end; the idle limit is checked on the server.

Too many failures: per client address, after 5 failed sign-ins within 15 minutes every further
attempt has to wait 2^(failures − 5) seconds after the last failure (at most 300 s); per
username, 20 failures within 15 minutes from any addresses lock it for 15 minutes. A successful
sign-in clears the address's count. The counts live in memory and a restart forgets them.

Each attempt writes an `auth` event: "Signed in: admin (192.0.2.10)", "Sign-in failed for 'admin'
(192.0.2.10)" ("Sign-in failed for an invalid username (192.0.2.10)" when the typed name breaks
the username rules, so a password typed into that field never reaches the log), and "Sign-in
paused for 192.0.2.10 after 5 failed attempts" at the fifth failure. Never the password.

### `POST /api/logout`

Ends this browser's session: the row is deleted and the cookie cleared.
`200 {"authenticated": false}`, and the event "Signed out: admin". With a login set it needs a
session or a token like every route that is not open.

### `POST /api/account/password`

The signed-in admin changes the password (**More › Account**). It needs the session cookie: a
token alone answers `401 {"error": "login required"}`. Body `{"current": "…", "new": "…"}`.

| Status | Body | When |
|---|---|---|
| 200 | `{"ok": true}` | saved to `config/sentinel.yaml` (rewritten, mode 0600); every other session ends, this one stays; the event "Password changed" |
| 400 | `{"error": "The current password is wrong."}` | counts as a failed sign-in for the limits above |
| 400 | `{"error": "Use at least 12 characters."}`, `"Use at most 256 characters."` or `"The password must differ from the username."` | the password rules |
| 400 | `{"error": "The new password is the same as the current one."}` | |
| 400 | `{"error": "send the fields as text"}` | `current` or `new` is not a string |
| 429 | as for `POST /api/login` | too many failures from this address |
| 400, 503 | as for `POST /api/login` | no login is set up, or the stored one cannot be read |
| 401 | `{"error": "login required"}` | no valid session, or the login was changed meanwhile with `./sentinel.sh set-password` |

With the Ansible role writing the config (`dspace_sentinel_manage_config: true`), the next play run
puts the inventory's hash back ([ANSIBLE.md](ANSIBLE.md#server-and-onboarding)).

## Current state

### `GET /api/status`

The Home tab in one call; also the payload of the SSE `status` event and of
`sentinel.sh doctor --json`.

```json
{
  "ts": 1790246534.03,
  "mood": "ok",
  "headline": "All systems healthy",
  "subline": "demo-dspace · checked 3 s ago",
  "hostname": "demo-dspace",
  "demo": true,
  "checking": false,
  "checked_at": 1790246530.35,
  "checked_ago_s": 3.0,
  "server": {
    "ts": 1790246530.35,
    "cpu_pct": 17.6, "cpu_per_core": [15.3, 19.1, 21.4, 14.6],
    "mem_total": 17179869184, "mem_used": 10944823624, "mem_available": 6235045560,
    "mem_pct": 63.7, "mem_cached": 3435973836,
    "swap_total": 4294967296, "swap_used": 76601348, "swap_pct": 1.8,
    "disk_total": 214748364800, "disk_used": 102021909518, "disk_free": 112726455282,
    "disk_pct": 47.5, "disk_path": "/", "inode_pct": 12.4,
    "load1": 0.75, "load5": 0.74, "load15": 0.72, "cores": 4,
    "net_rx_bps": 182141.0, "net_tx_bps": 1619394.0,
    "uptime_s": 1479632.0,
    "os_name": "Ubuntu", "os_version": "24.04", "kernel": "6.8.0-45-generic",
    "hostname": "demo-dspace",
    "disks": [{"path": "/dspace", "total": 1099511627776, "used": 451401545233,
               "free": 648110082543, "pct": 41.05}],
    "status": "ok",
    "reason": null,
    "since": 1790246500.34,
    "spark": {"cpu": [16.6, 17.3, 12.6, "…12 points"], "mem": ["…"], "disk": ["…"], "load": ["…"]},
    "per_metric": {"cpu": "ok", "memory": "ok", "swap": "ok", "disk": "ok", "load": "ok",
                   "disk:/dspace": "ok"}
  },
  "components": ["…one ComponentHealth per enabled module, server excluded; see below"],
  "alerts_open": ["…Alert objects"],
  "repository": {"items": 27576, "bitstreams": 81584, "communities": 14, "collections": 97,
                 "db_size": 3346215798, "assetstore_bytes": 451401097654,
                 "eperson_count": 1852, "workflow_items": 9, "estimated": false,
                 "ts": 1790246500},
  "next_check_in_s": 12.0,
  "modules": {"ports": true, "security": true, "postgres": true, "…": "…"},
  "version": "0.1.0"
}
```

`mood` drives the accent colour: `ok` mint, `warn` butter, `crit` coral, `unknown` sky. It is the
worst of the components **and the open alerts nobody acknowledged**: an open critical alert keeps
the mood `crit` and an open warning keeps it at least `warn` even while every ladder is green
again (an alert outlives its condition by `clear_samples` cycles). Acknowledged alerts are left
out of the mood and the headline, and a component whose open alerts are all acknowledged counts
as healthy there (its own `status` in `components` is unchanged); an escalation (warning →
critical) clears the acknowledgement, so the alert counts again. `headline` is one of "All
systems healthy" ("All systems healthy · 1 acknowledged", "… · 2 acknowledged" while
acknowledged alerts are open and nothing else is wrong), "1 warning" / "N warnings",
"Critical: PostgreSQL unreachable (+1 more)", "Waiting for the first check", "N checks could not
run" — or, when only open alerts raise the mood, "1 open alert" / "N open alerts" and "Critical:
120 errors in DSpace log (5 min)" (the alert's title); the counts in the warning and critical
headlines leave acknowledged alerts out. `alerts_open` still lists every open alert, with
`acked_ts`. `doctor` uses the same mood for its exit code (0 when everything open is
acknowledged). `repository` is `null` when the module is off.

### `GET /api/system`

```json
{"ts": 1790246534.03, "server": {"…same object as status.server"}, "hostname": "demo-dspace"}
```

### `GET /api/components`

A list of `ComponentHealth` objects in display order, server first. Only enabled modules appear.

### `GET /api/components/{id}`

One component with its ladder, plus one hour of headline latency, recent events, the slice of
configuration it uses (masked) and what depends on it.

```json
{
  "id": "postgres",
  "label": "PostgreSQL",
  "emoji": "🐘",
  "status": "ok",
  "reason": null,
  "chain": [
    {"id": "postgres.service", "label": "systemd postgresql", "status": "ok", "ts": 1790246500.34,
     "latency_ms": null, "value": null, "message": "active", "detail": {"unit": "postgresql"}},
    {"id": "postgres.tcp", "label": "TCP :5432", "status": "ok", "ts": 1790246500.34,
     "latency_ms": 0.46, "value": null, "message": "", "detail": {}},
    {"id": "postgres.connect", "label": "Connect as dspace", "status": "ok", "ts": 1790246500.34,
     "latency_ms": 5.96, "value": null, "message": "dspace@localhost:5432/dspace", "detail": {}},
    {"id": "postgres.query", "label": "SELECT 1", "status": "ok", "ts": 1790246500.34,
     "latency_ms": 1.18, "value": null, "message": "", "detail": {}}
  ],
  "metrics": {"connections": 22, "max_connections": 100, "connections_pct": 22,
              "db_size_bytes": 3346215798, "version": "PostgreSQL 15.8 (Ubuntu 15.8-1.pgdg24.04+1)",
              "active_queries": 2, "longest_query_s": 0.51},
  "depends_on": [],
  "latency_ms": 1.18,
  "since": 1790246500.34,
  "checked_at": 1790246500.34,
  "latency_series_1h": {"check": "postgres.query", "bucket_s": 15,
                        "points": [[1790242980, 0.78, 0.78], [1790243040, 1.19, 1.19], "…"]},
  "recent_events": [],
  "config_used": {"postgres": {"host": "localhost", "port": 5432, "database": "dspace",
                               "user": "dspace", "password": "", "sslmode": "prefer",
                               "service": "postgresql"}},
  "affected": ["DSpace Backend", "DSpace Search", "DSpace Frontend", "Repository site", "nginx"]
}
```

When a rung fails, the rungs after it carry `status: "unknown"` and `message: "not reached"`, and
`reason` is the failing rung's message. Every HTTP rung (`backend.rest/actuator/search`,
`frontend.home/public`, `nginx.http/https`, `solr.admin/search`) carries `detail.status_code`
(the UI shows "HTTP 200" under a rung without a message; `doctor` prints "200 / 91 ms"). A rung
that is `unknown` only because the endpoint is not available on this host — the actuator
answering 401/403/404, a systemd unit that does not exist, a log file that is missing, a
dependency not yet collected — carries `detail.informational: true`: it stays in the ladder
(muted, "— not available") but never counts towards the component's status or reason.
Dependency rungs look like this on the backend:

```json
{"id": "backend.dep.solr", "label": "Solr dependency", "status": "warn", "latency_ms": null,
 "message": "Solr is down (Critical): connection refused (localhost:8983)",
 "detail": {"dependency": "solr", "dependency_status": "crit"}}
```

A healthy dependency reads "PostgreSQL is healthy"; the others "is degraded (Warning): …" and "could
not be checked (Unknown): …", followed by the message of the rung that decides the dependency's
state: for "is down" the first critical rung that failed, for "is slow" the first critical rung
that only answered too slowly, otherwise the first counted rung with the dependency's status (the
dependency's `reason` when it has no chain). So a backend whose REST API is slow and whose search
fails reads "DSpace Backend is down (Critical): HTTP 500", not the slow rung's message. A
dependency that is critical only because it answered too slowly (every one of its critical rungs
carries `detail.slow`) reads "Solr is slow (Critical): search took 4.3 s" instead of "is down
(Critical)"; the rung itself stays `warn` with `detail.dependency_status: "crit"`. A rung that
answered correctly but crossed its latency threshold (`backend.rest`, `backend.search`,
`frontend.home`, `solr.search`) carries `detail.slow: true`: it does not make the rungs after it
"not reached", and when it is a component's only critical rung the headline reads "Critical: Solr
slow" instead of "unreachable". When the collector itself timed out or raised, the chain is one
`unknown` rung `<id>.error` (`message: "check timed out after 29 s"`, `detail.collector_error:
true`), which neither opens nor resolves alerts.

A rung may carry `detail.hint`: one or two plain sentences on what the situation is and what to
do, never a secret; the component pages show it as a muted line under the rung's message. The
collectors set it on `solr.core.<name>` (the Solr 9.8+ `<lib/>` fix), `nginx.http` (no web server
on this machine: the request was refused and systemd knows no such unit), `ports.<port>` (a
`local_only` port that other machines can reach) and `logs.<id>` (permission denied: the file's,
or a folder's, owner, group and mode and the group to join). A Ports rung also carries
`detail.expect` and `detail.addrs`, the addresses the port listens on:

```json
{"id": "ports.8983", "label": "Solr :8983", "status": "warn", "message": "exposed publicly (::)",
 "detail": {"expect": "local_only", "addrs": ["::"],
            "hint": "Solr has no login: anyone who can reach this port can read and change the search index. …"}}
```

The texts of every hint are in [SPEC.md](SPEC.md#6-collectors-and-health-chains-collectors-healthpy).
The Security rungs carry hints too ([below](#the-security-component)).

Some rungs read differently depending on the DSpace version the backend reports
([COMPATIBILITY.md](COMPATIBILITY.md)); they say so in `detail`:

- `backend.actuator` carries `detail.health_status` (DSpace's own word, e.g. `UP_WITH_ISSUES`)
  and, when the answer was reused, `detail.cached_age_s`: the actuator is asked at most every
  300 s, only an ok or informational answer is reused, and never across a restart or another
  DSpace version. An anonymous `DOWN` inside the SEO window (7.6.4–7.6.x, 8.2–8.4, 9.0–9.3, 10.0)
  while REST answers is `status: "unknown"` with `detail.informational: true`,
  `detail.known_issue: "seo"` and the message "DSpace 9.2 reports DOWN while healthy (a known SEO
  health check issue fixed in 8.5 / 9.4 / 10.1)"; with the components visible, a `DOWN` whose
  only down component is `seo` reads the same and adds what the SEO check flags (": the SEO check
  flags sitemap, ssr"). Any other `DOWN` is `warn`: "status DOWN (details need a DSpace admin
  login)" anonymously, "status DOWN (db)" with the components visible.
- `frontend.home` carries `detail.rendering` (`ssr` or `csr`), `detail.host_header` (the
  `Host` it sent, from `dspace.public_url`, IDNA-encoded as a browser sends it:
  `xn--bcher-kva.example` for `https://bücher.example`; a name without an IDNA form sends none)
  and, for the client-side shell, `detail.note`:
  "client-side rendering only (server-side rendering is off or failing)", or on 9.3+, when the
  request named another host than the backend's `dspaceUI` (else `dspace.public_url`),
  "client-side rendering only: DSpace 9.4 renders on the server only for the host in its
  ui.baseUrl (set dspace.public_url)"; `frontend.public` carries `detail.rendering` too.
- `solr.core.<name>` carries `detail.optional: true` for an optional DSpace core (`qaevent`,
  `suggestion`, `audit`, at most `warn`), and a core that failed to load is `crit` "failed to
  load: <reason>", with `detail.hint` for the Solr 9.8+ `<lib/>` case. A core whose reload
  failed still serves: it is pinged and searched, and reads `warn` "reload failed: <reason>
  (still serving; a restart would not load it)" with `detail.reload_failed`. `solr.admin` ("6
  cores, 1 failed to reload") lists `detail.failed`, `detail.reload_failed` and
  `detail.missing`.

Their components' `metrics` carry the matching facts: the backend `dspace_version`,
`dspace_name`, `dspace_major`, `dspace_minor`, `dspace_patch` (`null` for a two-number version
such as `9.4`), `dspace_snapshot`, `cris_version`, `rest_latency_ms`, `search_latency_ms`,
`actuator_status` and `actuator_components` (`{}` for an anonymous client); the frontend
`home_latency_ms`, `public_latency_ms`, `title`, `rendering`, `generator` (`DSpace 9.4`),
`angular` (`20.3.30`), `host_header` (the public URL's host as written, `bücher.example`),
`ui_url` and `public_url` (`null` when not set); Solr
`cores` (`[{name, docs, size_bytes, status, optional?}]`), `version`, `search_docs`,
`search_latency_ms`, `search_core`, `init_failures` (`{core: reason}`, the cores that did not
load) and `reload_failures` (`{core: reason}`, the ones whose reload failed).

A component whose module is on but has not been collected yet answers with
`status: "unknown"`, `reason: "not checked yet"` and an empty chain. An unknown or disabled id
is `404 {"error": "unknown or disabled component 'x'"}`.

Component ids: `server`, `postgres`, `solr`, `backend`, `frontend`, `nginx`, `ssl`, `ports`,
`security`, `logs`, `repository`, `assetstore`. Headline latency rungs: `postgres.query`, `solr.search`,
`backend.rest`, `frontend.home`, `nginx.http`.

### The Security component

`GET /api/components/security` (module `security`, 🛡️) has no dependencies and no headline
latency: its status is the worst of eight rungs, `security.ports` "Open ports",
`security.firewall` "Firewall", `security.ssh` "SSH", `security.web` "Web", `security.updates`
"Updates", `security.software` "Software", `security.files` "DSpace files" and
`security.sentinel` "Sentinel". Its `config_used` is `{ports, dspace}` (the port rules,
`dspace.public_url` and `dspace.install_dir`). The demo's, abridged:

```json
{
  "id": "security", "label": "Security", "emoji": "🛡️", "status": "ok", "reason": null,
  "chain": [
    {"id": "security.ports", "label": "Open ports", "status": "ok",
     "message": "7 listening · 5 reachable from the network (22, 80, 443, 4000, 8080) · none unexpected", "detail": {}},
    {"id": "security.firewall", "label": "Firewall", "status": "ok", "message": "ufw active", "detail": {}},
    {"id": "security.ssh", "label": "SSH", "status": "ok",
     "message": "root login: prohibit-password · passwords: yes · port 22",
     "detail": {"hint": "Keys instead of passwords: set PasswordAuthentication no once every admin has a key."}},
    {"id": "security.web", "label": "Web", "status": "ok", "message": "HTTPS on repo.demo-dspace.org",
     "detail": {"hint": "Missing response headers: Strict-Transport-Security. They are set in the web server in front of DSpace (nginx add_header)."}},
    {"id": "security.updates", "label": "Updates", "status": "ok",
     "message": "no pending security updates (3 other updates)", "detail": {}},
    {"id": "security.software", "label": "Software", "status": "ok",
     "message": "DSpace 9.4 · Solr 9.6.1 · PostgreSQL 15.8 · Ubuntu 24.04: all supported", "detail": {}},
    {"id": "security.files", "label": "DSpace files", "status": "ok",
     "message": "local.cfg and sentinel.yaml are not readable by other users", "detail": {}},
    {"id": "security.sentinel", "label": "Sentinel", "status": "ok", "message": "listens on 127.0.0.1 only", "detail": {}}
  ],
  "metrics": {
    "ports": [{"port": 22, "proto": "tcp", "addrs": ["0.0.0.0"], "public": true, "process": null, "pid": null,
               "expected": "open", "verdict": "expected"},
              {"port": 5432, "proto": "tcp", "addrs": ["127.0.0.1"], "public": false, "process": null, "pid": null,
               "expected": "local_only", "verdict": "local"}, "…"],
    "listening": 7, "reachable": 5, "tailscale": 0, "exposed": 0, "new": 0,
    "firewall": {"active": true, "name": "ufw", "units": {"ufw": "active", "nftables": "inactive", "firewalld": "missing"}},
    "ssh": {"port": 22, "permit_root_login": "prohibit-password", "password_authentication": "yes",
            "readable": true, "unreadable": null},
    "web": {"public_url": "https://repo.demo-dspace.org", "scheme": "https", "headers_missing": ["Strict-Transport-Security"],
            "headers_checked": true, "final_url": "https://repo.demo-dspace.org/", "status_code": 200, "error": null},
    "updates": {"pending": 3, "security": 0, "reboot_required": false, "reboot_pkgs": null,
                "source": "/var/lib/update-notifier/updates-available"},
    "software": [{"name": "DSpace", "version": "9.4", "until": null, "status": "ok", "note": "supported"},
                 {"name": "PostgreSQL", "version": "15.8", "until": "2027-11-11", "status": "ok",
                  "note": "end of support on 2027-11-11"}, "…"],
    "files": [{"path": "/dspace/config/local.cfg", "mode": "640", "owner": "tomcat", "group": "tomcat",
               "world_readable": false, "readable": true}, "…"],
    "sentinel": {"host": "127.0.0.1", "port": 9765, "auth": false, "login": false, "open": false}
  },
  "depends_on": [], "latency_ms": null,
  "config_used": {"ports": ["…"], "dspace": {"public_url": "", "install_dir": "/dspace", "…": "…"}}
}
```

- `ports[]`: one entry per port and protocol (`proto` `tcp` or `udp`), `public` when any of its
  `addrs` is reachable from the network (an IPv4-mapped address such as `::ffff:127.0.0.1` counts
  as the IPv4 address it maps, so that one is loopback), `process`/`pid` only for Sentinel's own
  user's sockets (unless it runs as root), `expected` the `ports` rule's `expect` (or `null`), and
  `verdict`: `local` (loopback only), `expected` (a rule covers it; also Sentinel's own port, UDP
  68/546 and, while a `tailscale0` interface exists, Tailscale's WireGuard port UDP 41641 on every
  interface, which carry a `note`: "Sentinel", "DHCP client", "DHCPv6 client", "Tailscale's own
  port (WireGuard)"), `tailscale` (reachable only on Tailscale's addresses, `100.64.0.0/10` and
  `fd7a:115c:a1e0::/48`, note "reachable through Tailscale only"; not exposed), `exposed`
  (reachable, no rule, open since Sentinel started) or `new` (reachable, no rule, opened later).
  `reachable` counts the public ports except the Tailscale-only ones, which `tailscale` counts;
  `exposed` counts `exposed` and `new` ports (the Open ports rung's "unexpected", and the number
  the Firewall rung and tile use), `new` the new ones. With Tailscale-only ports the rung's
  message gains a part: "9 listening · 5 reachable from the network (22, 80, 443, 4000, 8080) · 2
  through Tailscale only · none unexpected".
- `security.ports` carries `detail.note` "Program names are known only for Sentinel's own user;
  run `sudo ss -ltnp` on the server to see the others." when Sentinel is known not to run as root
  and a port's program is unknown (the demo, where that is not known, has none).
- `security.firewall` warns "no host firewall is active and 2 ports are reachable from the
  network without a rule" (the count is `exposed`), "…, and 1 local-only port is public" when a
  `local_only` rule's port also listens publicly, or "no host firewall is active and 1
  local-only port is public" alone.
- `sentinel`: where Sentinel listens, `auth` (a `server.auth_token` is set), `login` (the
  dashboard login is set; always `false` in demo mode) and `open` (a non-loopback `host` with
  neither). The `security.sentinel` rung reads "listens on 127.0.0.1 only", "listens on 0.0.0.0
  with a login" (with a hint to put HTTPS in front), "listens on 0.0.0.0 with a password" (a token
  and no login) or warns "listens on 0.0.0.0 without a password (server.allow_unauthenticated)".
- `software[].status`: `ok`, `soon` (support ends within 90 days), `ended`, `affected` or
  `unknown`; `until` is the last supported day or `null`.
- A rung that could not be checked is `status: "unknown"` with `detail.informational: true` and
  `detail.unavailable: true` ("not available · sshd_config is not readable by Sentinel's user");
  its metrics are `null` or empty. A rung that only informs ("no host firewall active (this
  server relies on the network's firewall, which Sentinel cannot see)", "no SSH server found",
  "no public address set") has `detail.informational` alone. Neither counts towards the
  component's status.
- `detail.alert: true` marks a warning that also opens an alert: on `security.ports` (a `new`
  port), `security.firewall` and `security.ssh` only
  ([ALERTS.md](ALERTS.md#security)). Every message and hint is listed in
  [SPEC.md](SPEC.md#6-collectors-and-health-chains-collectors-healthpy).

## History

All history routes accept `range=1h|6h|24h|7d|30d` (default `24h`) and answer with `range`,
`from` and `to` echoed back. Buckets are chosen so a series has at most 360 points:
1h → 15 s, 6h → 60 s, 24h → 300 s, 7d → 1800 s, 30d → 7200 s. Ranges over seven days are served
from hourly roll-ups, merged with hourly aggregates of the raw samples newer than the last
roll-up, so a 30-day chart reaches the newest sample.

### `GET /api/history?metric=cpu&range=1h`

`metric` is one of `cpu`, `mem`, `disk`, `load`, `swap`, `net`.

```json
{
  "metric": "cpu",
  "unit": "%",
  "bucket_s": 15,
  "points": [[1790242980, 15.7, 15.7], [1790243040, 14.8, 14.8], [1790243100, 13.2, 13.2], "…"],
  "thresholds": {"warning": 80.0, "critical": 95.0},
  "range": "1h",
  "from": 1790242933.47,
  "to": 1790246533.47
}
```

Each point is `[ts, avg, max]`. For `load` the thresholds are multiplied by the core count and
carry `"per_core": true, "cores": 4`. `net` returns two series instead of `points`:

```json
{"metric": "net", "unit": "B/s", "bucket_s": 15,
 "series": {"rx": [[1790242980, 204737.0, 231010.0], "…"], "tx": [[1790242980, 1600817.0, 1712400.0], "…"]},
 "thresholds": null, "range": "1h", "from": 1790242933.5, "to": 1790246533.5}
```

An unknown metric is `400 {"error": "unknown metric 'bogus'; use one of cpu, mem, disk, load, swap, net"}`.

### `GET /api/history/latency?check=backend.rest&range=1h`

`check` is a rung id: `backend.rest`, `backend.search`, `frontend.home`, `postgres.query`,
`solr.search`, `nginx.http` (any `backend.*`, `frontend.*`, `postgres.*`, `solr.*` or `nginx.*`
rung is accepted).

```json
{
  "check": "backend.rest",
  "bucket_s": 15,
  "points": [[1790242980, 112.0, 112.0], [1790243040, 117.6, 117.6], "…"],
  "thresholds": {"warning": 1000.0, "critical": 3000.0},
  "unit": "ms",
  "range": "1h", "from": 1790242933.49, "to": 1790246533.49
}
```

Points are `[ts, avg_ms, max_ms]`. `thresholds` is `null` for rungs without a latency rule.

### `GET /api/availability?range=24h`

One strip per enabled component (server included). Bucket counts: 1h → 60, 6h → 72, 24h → 96,
7d → 84, 30d → 60.

```json
{
  "bucket_s": 900,
  "range": "24h", "from": 1790160133.5, "to": 1790246533.5,
  "components": [
    {"id": "server", "label": "Server", "emoji": "💻", "uptime_pct": 99.72, "downtime_s": 239,
     "outages": 1, "samples": 722, "buckets": ["ok", "ok", "warn", "crit", "ok", "…96 entries"]},
    {"id": "postgres", "label": "PostgreSQL", "emoji": "🐘", "uptime_pct": 100.0, "downtime_s": 0,
     "outages": 0, "samples": 720, "buckets": ["ok", "…"]}
  ]
}
```

Each bucket holds the worst status seen in it. A bucket without a check of its own takes the
status of the last check before it, because a check stands until the next one: a component that
is checked every 15 minutes (Repository) is not "no data" in between. A bucket is `null` only
where nothing was checked: before the first check, or more than twice the usual spacing after
the last one (Sentinel was not running). `uptime_pct`
counts `ok` and `warn` as up and `crit` as down; `unknown` points are excluded. `downtime_s`
charges each `crit` row the time until the next row, capped at twice the typical spacing, so a
gap in the history (Sentinel stopped) is not counted as an outage.

### `GET /api/repository`

```json
{
  "enabled": true,
  "latest": {"items": 27576, "bitstreams": 81584, "communities": 14, "collections": 97,
             "db_size": 3346215798, "assetstore_bytes": 451401097654, "eperson_count": 1852,
             "workflow_items": 9, "estimated": false, "ts": 1790246500},
  "series": {"items": [[1789639200, 27492], "…hourly, 30 days"], "bitstreams": ["…"],
             "communities": ["…"], "collections": ["…"], "db_size": ["…"], "assetstore_bytes": ["…"]},
  "delta": {"items": 84, "bitstreams": 246, "communities": 0, "collections": 1,
            "db_size": 17688903, "assetstore_bytes": 9056719108,
            "first_ts": 1789639200, "last_ts": 1790246500, "days": 7.029},
  "growth_per_day": {"items": 11.95, "bitstreams": 35.0, "assetstore_bytes": 1288479030.87,
                     "db_size": 2516560.39},
  "from": 1787654533.54, "to": 1790246533.54
}
```

`estimated: true` means the counts came from `pg_class.reltuples` because the exact `count(*)`
hit the 3 s statement timeout.

### `GET /api/events?limit=100&component=&kind=`

Newest first. `kind` is one of `component` (status transitions), `alert` (opened, escalated,
resolved, acked, unacked), `incident` (opened, updated, resolved), `config`, `report`, `demo`,
`auth` (the dashboard login: signed in, sign-in failed, sign-in paused, signed out, "Password
changed", "Login removed"; [above](#post-apilogin)), and the notification audit trail: `notify` (delivered), `notify.error` (delivery failed),
`notify.skipped` (held back by quiet hours) and `notify.demo` (demo mode: the message that would
have been sent, with its `html` and `text` in `data`). Notification rows carry `data.channel`
(`telegram`, `email` or `webhook`) and, for Telegram and email, `data.destinations`: one
`{destination, label, ok, error}` per bot (`destination` = its chat id, plus the entry's `id`)
or recipient (`destination` = the address). An email row also carries `data.subject`, a
Telegram row `data.message_id`; a `notify.demo` row lists `data.channels` (and the email
`subject` when email is among them). Report deliveries are `notify` rows too ("Report sent by
email: …"); one that was not delivered, including a channel that is off or not set up, is a
`notify.error` row ("Report not sent to Telegram: …", "Report not sent by email: email is not
enabled"). Each channel's row is written as soon as that channel is done, so the rows of one
event appear in the order the channels finish. See
[NOTIFICATIONS.md](NOTIFICATIONS.md#delivery-and-the-events-log).

Messages use the UI's words: a status change reads "Solr: Healthy → Warning — search took 1.7 s"
("Solr: Critical — …" the first time a component is seen), a module toggle "Solr turned on",
"SSL certificate turned off" or "Security turned on" (the names of More › Modules), a settings save "Configuration updated: alerts.disk.warning, …".
Every start also writes one `config` row (no component) for each thing in the settings Sentinel
left out or cannot use: "The quiet hours '11pm-7am' in notifiers.telegram.quiet_hours were
ignored: quiet hours must look like 23:00-07:00", "Email is not ready: the SMTP username is not an
email address: set from_address (the address alerts come from)", "The Telegram bot from
SENTINEL_TELEGRAM_TOKEN was left out: there are already 5 bots".

```json
[
  {"id": 36, "ts": 1790169298, "kind": "incident", "component": "solr",
   "message": "Incident #4 resolved after 7 m 30 s", "data": {"incident_id": 4, "type": "resolved"}},
  {"id": 35, "ts": 1790169178, "kind": "component", "component": "solr",
   "message": "Solr: Critical → Healthy", "data": {"from": "crit", "to": "ok"}}
]
```

### `GET /api/export?range=30d`

A JSON dump for backups or offline analysis (default range `30d`) with every row of the range.
The body is streamed table by table (`Cache-Control: no-store`), so even a 30-day export keeps
Sentinel's memory flat. One export runs at a time; a second request meanwhile answers
`429 {"error": "an export is already running; try again when it has finished"}` with
`Retry-After: 30`. `checks` also holds the history-only `postgres.connections` rows the
reports use.

```json
{
  "meta": {"hostname": "demo-dspace", "exported_ts": 1790246533.65, "range": "30d",
           "bucket_hint_s": 7200, "demo": true},
  "range": {"from": 1787654533, "to": 1790246533},
  "samples": [{"ts": 1790246520, "cpu": 17.6, "mem_pct": 63.7, "mem_used": 10944823624,
               "mem_total": 17179869184, "swap_pct": 1.8, "disk_pct": 47.5, "disk_used": 102021909518,
               "disk_total": 214748364800, "load1": 0.75, "load5": 0.74, "load15": 0.72,
               "net_rx": 182141.0, "net_tx": 1619394.0, "extra": {"cpu_per_core": ["…"], "disks": ["…"]}}],
  "checks": [{"ts": 1790246520, "check_id": "postgres.query", "status": "ok", "latency_ms": 1.18,
              "value": null, "message": ""}],
  "component_history": [{"ts": 1790246520, "component_id": "postgres", "status": "ok", "latency_ms": 1.18}],
  "alerts": ["…Alert objects"],
  "incidents": ["…Incident objects"],
  "repository": ["…repository_stats rows"],
  "events": ["…Event objects"]
}
```

## Alerts and incidents

### `GET /api/alerts?status=open&limit=100`

`status` is `open` (default), `resolved` or `all`; newest first.

```json
[
  {
    "id": 18,
    "rule": "component_down:solr",
    "component": "solr",
    "severity": "critical",
    "status": "resolved",
    "title": "Solr unreachable",
    "message": "service ✅ → TCP ❌\nconnection refused (localhost:8983)",
    "value": null,
    "threshold": null,
    "opened_ts": 1790169058,
    "resolved_ts": 1790169178,
    "acked_ts": null,
    "incident_id": 4,
    "notified_ts": null
  },
  {
    "id": 20,
    "rule": "log_errors:dspace",
    "component": "logs",
    "severity": "critical",
    "status": "resolved",
    "title": "118 errors in DSpace log (5 min)",
    "message": "Solr search core is unavailable: Connection refused",
    "value": 118.0,
    "threshold": 100.0,
    "opened_ts": 1790169098, "resolved_ts": 1790169298, "acked_ts": null,
    "incident_id": 4, "notified_ts": null
  }
]
```

Rule ids: `cpu`, `memory`, `swap`, `disk` (root filesystem) and `disk:<mount>` (every extra
mount, e.g. `disk:/dspace`), `load`, `rest_latency` (`backend.rest`), `search_latency`
(`backend.search`), `solr_latency` (`solr.search`), `home_latency`, `postgres_connections`,
`ssl_expiry`, `log_errors:<file id>` (always with the file id, e.g. `log_errors:dspace`),
`assetstore_free`, `component_down:<component>`. The part after the colon identifies the
instance, so route on `rule.split(":")[0]`; show users the `title` and `message`, never the raw
id. A `component_down` message is the chain summary up to the failing rung, then its reason on
the next line. See [ALERTS.md](ALERTS.md).

### `GET /api/alerts/{id}`

One alert, or `404 {"error": "alert 99 not found"}`.

### `POST /api/alerts/{id}/ack` · `POST /api/alerts/{id}/unack`

Acknowledging silences reminders and calms Home: from then on the alert is left out of
`/api/status`'s `mood` and `headline` ("All systems healthy · 1 acknowledged"), until it resolves
or escalates to critical, which clears `acked_ts`. Both answer with the updated alert (`acked_ts`
set or `null`); neither sends a `status` frame, so the UI asks for `/api/status` right after.

### `GET /api/incidents?status=all&limit=100`

`status` is `all` (default), `open` or `resolved`.

```json
[
  {
    "id": 4,
    "title": "Possible Solr resource exhaustion",
    "hypothesis": "Possible Solr resource exhaustion",
    "status": "resolved",
    "started_ts": 1790168848,
    "ended_ts": 1790169298,
    "affected": ["DSpace Search", "DSpace Backend"],
    "sequence": [
      {"ts": 1790168848, "text": "Memory 81 %", "severity": "warning", "alert_id": 19},
      {"ts": 1790168913, "text": "Memory 94 %", "severity": "critical", "alert_id": 19},
      {"ts": 1790168993, "text": "Solr response 4.8 s", "severity": "critical", "alert_id": 20},
      {"ts": 1790168993, "text": "Search API slow: 4.3 s", "severity": "critical", "alert_id": 21},
      {"ts": 1790169058, "text": "Solr DOWN", "severity": "critical", "alert_id": 22},
      {"ts": 1790169073, "text": "DSpace search unavailable", "severity": "critical", "alert_id": 23},
      {"ts": 1790169098, "text": "118 errors in DSpace log (5 min)", "severity": "critical", "alert_id": 24}
    ],
    "alert_ids": [19, 20, 21, 22, 23, 24]
  }
]
```

### `GET /api/incidents/{id}`

The incident plus `"alerts": [...]` with the full alert objects.

## Reports

### `GET /api/reports?include_deleted=false&limit=200`

Reports newest first; soft-deleted ones only with `include_deleted=true`. Every row carries the
stored `Report` fields plus `filename` and `exists` (is the file still on disk); a soft-deleted
row also has `purge_after_ts` (24 h after `deleted_ts`).

```json
[
  {"id": 3, "created_ts": 1790246900, "title": "DSpace Sentinel report · demo-dspace · Last 7 days",
   "range_from": 1789642100, "range_to": 1790246900, "fmt": "html",
   "path": "/opt/dspace-sentinel/data/reports/sentinel-report-20260924-1048-7d.html",
   "size": 184213, "deleted_ts": null,
   "filename": "sentinel-report-20260924-1048-7d.html", "exists": true}
]
```

### `GET /api/reports/options`

What the report panel can offer, so the form never hard-codes section names:

```json
{
  "sections": [{"id": "summary", "emoji": "📋", "label": "Summary"}, {"id": "server", "emoji": "💻", "label": "Server"}, "…"],
  "default_sections": ["summary", "server", "services", "latency", "alerts", "repository", "security", "recommendations"],
  "formats": ["html", "md", "json"],
  "ranges": [{"id": "24h", "label": "Last 24 hours"}, {"id": "7d", "label": "Last 7 days"}, {"id": "30d", "label": "Last 30 days"}],
  "telegram_available": true,
  "email_available": true,
  "weekly": {"enabled": false, "weekday": 1, "hour": 8, "send_telegram": false, "send_email": false},
  "purge_after_s": 86400
}
```

`telegram_available` is true only when Telegram is switched on *and* at least one bot has both
a token and a chat id; `email_available` when email is switched on and can send (a server, a
sender that is an email address, the password when a username is set, and at least one
recipient). The panel shows its
*Send to Telegram* and *Send by email* switches on those.

`sections` lists every section in document order: `summary`, `server`, `services`, `latency`,
`alerts`, `incidents`, `repository`, `logs`, `security` (`{"id": "security", "emoji": "🛡️",
"label": "Security"}`) and `recommendations`. The panel offers a section tied to a module
(latency, incidents, repository, logs, security) only while that module is on.

### `POST /api/reports`

```json
{"range": "7d", "fmt": "html", "title": "Weekly report", "sections": ["summary", "services", "latency"], "send_telegram": false, "send_email": true}
```

- `range`: `"24h"`, `"7d"`, `"30d"` or `{"from": 1789642100, "to": 1790246900}` (epoch seconds).
- `fmt` (alias `format`): `html` (default), `md` or `json`.
- `title` (trimmed to 200 characters; empty ⇒ "DSpace Sentinel report · <host> · <range>"),
  `sections` (ids from `/api/reports/options`; default `reports.default_sections`),
  `send_telegram` (every bot, as a document) and `send_email` (every recipient, as an
  attachment) are optional.

Answers `201` with the report row (as in the list, `filename` and `exists` included) and
publishes a `report` SSE event of type `created`. With `send_telegram: true` the row also carries
`"telegram": {"ok": true|false, "error": null|"…"}`, and with `send_email: true`
`"email": {"ok": true|false, "error": null|"…", "results": [{"address", "label", "ok", "error"}]}`.
A channel that is off or not set up answers `{"ok": false, "error": "email is not enabled",
"skipped": true}` (or "Telegram is not enabled", "Telegram has no bot yet: add a bot token and a
chat id", "email is not set up: …", or another reason email cannot send, such as "the SMTP
username is not an email address: …"); in demo mode both answer `{"ok": true, "error": null,
"skipped": "demo"}` and nothing is sent. A failed delivery never fails the request; it is
recorded as a `notify.error` event. A file over 17 MB (17,825,792 bytes) is not emailed ("<file>
is too large to email (17.1 MB; the limit is 17 MB)"). Generation runs in a worker thread, so a
30-day report does not block the API.

- Bad input is 400: `"range 'to' must be after 'from'"`, `"range 'from' and 'to' must be finite
  epoch seconds"`, `"range must lie within the last 366 days"` (a custom range may start at most
  366 days ago and span at most 366 days), `"range 'to' must not be in the future"` (more than
  5 minutes ahead), `"fmt must be one of html, md, json"`, `"unknown sections: …"`.
- One report is generated at a time: a second request meanwhile answers
  `429 {"error": "a report is already being generated; try again in a moment"}` with
  `Retry-After: 5`.
- `507 {"error": "not enough free disk space for a report: … (Sentinel keeps at least 1.0 GB free)"}`
  when the data filesystem has less than 1 GiB free.
- `500 {"error": "report generation failed; the Sentinel log has the details"}` for anything
  else that goes wrong.
- A build without the reports module answers `501 {"error": "reports module not built yet"}`.

At most 200 report files and 500 MB are kept: a new report removes the oldest ones (soft-deleted
included) and logs a `report` event; see [REPORTS.md](REPORTS.md).

### `GET /api/reports/{id}?meta=false&download=false`

The file itself. HTML is served `Content-Disposition: inline` so it opens in a tab, with
`Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; img-src data:` (no
scripts, no outgoing requests); Markdown and JSON are attachments. Every format carries
`X-Content-Type-Options: nosniff`. `download=true` forces an attachment for HTML too (the
*Download* buttons).
`meta=true` returns the report row instead of the file — including soft-deleted rows, which is
how the report detail page can still describe a report during the undo window. `410` when the
file was removed from disk, `404` for an unknown id.

### `DELETE /api/reports/{id}` · `POST /api/reports/{id}/restore`

Soft delete: the row gets `deleted_ts` and `purge_after_ts`, the UI shows an Undo snackbar and
the hourly maintenance job removes the file 24 hours later. Restore answers `409` once the file
has been purged. Both answer with the report row and publish a `report` SSE event (`deleted` /
`restored`).

## Configuration

### `GET /api/config`

The effective configuration as one object, secrets masked, plus the runtime flags `demo`,
`notifiers.telegram.chats[].from_env` (the read-only bot from `SENTINEL_TELEGRAM_TOKEN` +
`SENTINEL_TELEGRAM_CHAT_ID`) and `notifiers.email.password_from_env` (true while the email password
comes from `SENTINEL_SMTP_PASSWORD`). Sending the flags back in a `PATCH` is harmless: they are
ignored.

```json
{
  "server": {"host": "127.0.0.1", "port": 9765, "auth_token": "••••••",
             "login": {"username": "admin", "password_hash": "••••••"}, "hostname": "",
             "allowed_hosts": [], "allow_unauthenticated": false, "timezone": ""},
  "setup": {"completed": true, "persona": "full"},
  "collector": {"system_interval": 15, "service_interval": 30, "http_interval": 30,
                "db_interval": 60, "logs_interval": 60, "repository_interval": 900, "security_interval": 900,
                "retention_days": 30},
  "dspace": {"install_dir": "/dspace", "auto_discover": true, "server_url": "http://localhost:8080/server",
             "ui_url": "http://localhost:4000", "public_url": "https://repo.example.org", "…": "…"},
  "postgres": {"host": "localhost", "port": 5432, "database": "dspace", "user": "dspace",
               "password": "••••••", "sslmode": "prefer", "service": "postgresql"},
  "solr": {"…": "…"}, "nginx": {"…": "…"}, "ssl": {"…": "…"}, "ports": ["…"], "logs": {"…": "…"},
  "alerts": {"confirm_samples": 2, "clear_samples": 3, "reminder_minutes": 0,
             "cpu": {"warning": 80.0, "critical": 95.0}, "…": "…"},
  "notifiers": {
    "telegram": {"enabled": true,
                 "chats": [{"id": "032ad4f2", "label": "Danindu", "bot_token": "••••••", "chat_id": "987654321",
                            "min_severity": "warning", "from_env": false},
                           {"id": "env", "label": "From environment", "bot_token": "••••••",
                            "chat_id": "-1001234567890", "min_severity": "warning", "from_env": true}],
                 "quiet_hours": "23:00-07:00", "daily_digest": ""},
    "email": {"enabled": true, "smtp_host": "smtp.gmail.com", "smtp_port": 587, "security": "starttls",
              "username": "sentinel.repo1@gmail.com", "password": "••••••", "from_address": "",
              "from_name": "DSpace Sentinel",
              "recipients": [{"address": "ops@example.org", "label": "On call", "min_severity": "warning"}],
              "quiet_hours": "", "daily_digest": "08:00", "password_from_env": false},
    "webhook": {"enabled": true, "url": "••••••"}},
  "reports": {"weekly": {"enabled": false, "weekday": 1, "hour": 8, "send_telegram": false, "send_email": false},
              "default_sections": ["summary", "server", "services", "latency", "alerts", "repository", "security",
                                   "recommendations"]},
  "modules": {"ports": true, "security": true, "postgres": true, "…": "…"},
  "appearance": {"theme": "system"},
  "demo": false
}
```

### `PATCH /api/config`

Send any subset of the tree; it is deep-merged (lists are replaced), validated, saved atomically
with mode 0600 and applied immediately. Secrets sent as `"••••••"` keep their stored value.
`notifiers.telegram.chats` and `notifiers.email.recipients` are lists, so a patch replaces them
whole: send every entry. A bot token sent as the mask keeps the token of the entry with the same
`id` (an entry without `bot_token` keeps its token too, `""` clears it), new entries get an `id`,
and the read-only entry from the environment (`"id": "env"`) is ignored. An older client that
sends the single `notifiers.telegram.bot_token` / `chat_id` edits the first saved entry.
Answers with the masked config after saving. The file receives its own settings plus the patch:
values that came from `SENTINEL_*` variables or `serve --host/--port` are never written, and
comments in the file are lost ([CONFIG.md](CONFIG.md)).

```bash
curl -s -X PATCH http://127.0.0.1:9765/api/config \
  -H 'content-type: application/json' \
  -d '{"alerts": {"disk": {"warning": 85, "critical": 95}}, "appearance": {"theme": "dark"}}'
```

Invalid input: `400 {"error": "invalid configuration: alerts.disk.warning: Input should be a valid number"}`.
Also 400:

- a patch that changes anything under `server` but `timezone` (host, port, auth token, the login,
  hostname, allowed hosts, `allow_unauthenticated`): `{"error": "server settings are set in
  config/sentinel.yaml; restart to apply"}`. The login changes only with `./sentinel.sh
  set-password` or [`POST /api/account/password`](#post-apiaccountpassword). Sending the current values back (a `GET` → `PATCH`
  round trip) is fine. `server.timezone` decides nothing about who reaches the API, so a patch may
  change it (`{"server": {"timezone": "Asia/Colombo"}}`, or `""` for the server's zone); it
  applies at once. A name this system cannot load answers `{"error": "invalid configuration:
  server.timezone: unknown time zone 'Mars/Base': use an IANA name such as Asia/Colombo, or leave
  it empty for the server's zone"}` (plus ` (this system has no time zone data: install the tzdata
  package)` on a system without any), a value that is not text `…server.timezone: the time zone
  must be text, such as Asia/Colombo`;
- a patch that changes `postgres.host`, `port`, `database` or `user` while a password is stored
  and does not carry the password again: `{"error": "enter the PostgreSQL password again:
  postgres.host, port, database or user changed and the saved password is only sent to the
  server it was saved for"}` (an explicit `""` is accepted);
- a systemd unit name that is not one: `not a systemd unit name: …`;
- a patch that changes `notifiers.email.smtp_host` (any letter case), `smtp_port`, `security` or
  `username` while an email password is stored and does not carry a new one: `{"error": "enter
  the email password again: the SMTP server, port, security or username changed and the saved
  password is only sent to the server it was saved for"}` (an empty `username`, a relay without
  login, is always accepted);
- while `SENTINEL_SMTP_PASSWORD` is set, a new `notifiers.email.password` (anything but the
  mask): `{"error": "the email password comes from SENTINEL_SMTP_PASSWORD in config/sentinel.env:
  change it there"}`, and a patch that re-points its server or login: `{"error": "the email
  password comes from SENTINEL_SMTP_PASSWORD in config/sentinel.env and is only sent to the server
  it was set for: change the SMTP server, port, security or username in config/sentinel.yaml"}`;
- a newly typed `notifiers.email.password` (anything but the mask) with a character outside
  ASCII, with or without a username: `{"error": "invalid configuration: notifiers.email: the email
  password has a character that cannot be sent: use letters, digits and symbols from the English
  keyboard"}`. A patch of `notifiers.email` gets the same answer while the password stored in the
  settings file is such a password (until a new one is typed), and `notifiers.email: the SMTP
  username has a character that cannot be sent: use letters, digits and symbols from the English
  keyboard` for such a login name. A password from `SENTINEL_SMTP_PASSWORD` that cannot be sent
  never holds up a patch of the other email settings (email simply stays not ready);
- a masked bot token on an entry whose `id` is missing or unknown:
  `{"error": "enter the bot token for Danindu again"}` (the label, else the chat id, else "the
  new bot");
- a notification setting the checks refuse, for example `invalid configuration:
  notifiers.telegram.chats: Telegram can send to at most 5 bots`, `…chats.0.chat_id: not a
  Telegram chat id: 'me'`, `…chats.0.bot_token: not a Telegram bot token (it looks like
  123456789:ABC…, from @BotFather)`, `…chats: Telegram chat 987654321 is listed twice with the
  same bot`, `notifiers.email.recipients: email can go to at most 5 recipients`,
  `…recipients.0.address: not an email address: 'Ops <ops@example.org>'`, `…recipients:
  ops@example.org is listed twice`, `notifiers.email.smtp_host: not an SMTP server name: 'smtp
  example.org' (a host name or IP address)`, `…from_name: the sender name must be at most 60
  characters on one line`, `notifiers.email: the SMTP username is not an email address: set
  from_address (the address alerts come from)` (only for a patch that touches `notifiers.email`:
  a settings file that already holds such a login does not hold up other patches),
  `notifiers.telegram.quiet_hours: quiet hours must look like 23:00-07:00` (also for digits other
  than 0–9 and values over 40 characters), `…quiet_hours: quiet hours must start and end at
  different times`, `notifiers.email.daily_digest: the daily digest time must look like 08:00`
  ([CONFIG.md](CONFIG.md#notifiers)). Quiet hours and digest times are normalised when they can
  be read: `"7:00 – 9:30"` is saved as `"07:00-09:30"`, `"8:00"` as `"08:00"`. A JSON number is
  never a time.

Every successful patch writes a `config` event ("Configuration updated: alerts.disk.warning, …").

### `GET /api/modules` · `PATCH /api/modules`

```json
{"modules": {"ports": true, "security": true, "postgres": true, "solr": true, "backend": true,
             "frontend": true, "nginx": true, "ssl": true, "logs": true, "repository": true,
             "assetstore": true, "incidents": true}}
```

`PATCH` takes `{"postgres": true, "solr": false}` (booleans only, known ids only) and answers with
the same `{modules}` shape. The scheduler hot-reloads: a module switched on runs immediately, a
module switched off disappears from state (its history is kept, its open alerts resolve without
a message). The change is logged as one `config` event in the words of the More › Modules
toggles: "PostgreSQL turned on, Solr turned off", "Security turned on".

## Actions

### `POST /api/actions/check-now`

Runs the enabled collectors right away and answers with the `/api/status` payload. The UI's
**Check now** button; the `r` shortcut. Calls are coalesced so a busy button cannot load the
DSpace stack: a call made while a check-now is running waits for it, and a call within 10 s of
the previous one answers with the current status without running anything. A configuration
change (`PATCH /api/config`, `PATCH /api/modules`, setup) or **Clear history** always allows a
fresh run. The repository collector (`count(*)` scans of the DSpace database) only runs when its
own `repository_interval` is due, never because of a check-now, unless it has not run since
Sentinel started. The Security collector runs on a check-now like the others (one GET of
`dspace.public_url`).

### `POST /api/actions/telegram-test`

Optional body: `{"id": "<entry id>"}` to test one bot; without it every bot is tested.

```json
{"ok": false, "bot_name": "@danindu_sentinel_bot",
 "error": "1 of 2 bots failed — Team: chat not found — add the bot to the group first",
 "results": [{"id": "032ad4f2", "label": "Danindu", "chat_id": "987654321", "ok": true,
              "error": null, "bot_name": "@danindu_sentinel_bot"},
             {"id": "a75ddc47", "label": "Team", "chat_id": "-1001234567890", "ok": false,
              "error": "chat not found — add the bot to the group first", "bot_name": "@team_sentinel_bot"}]}
```

Each bot answers `getMe` with its own token, then gets "👋 DSpace Sentinel is connected ·
<host>". `bot_name` is the bot's `@username` (or its first name when it has none); the top-level
`bot_name` is the first bot that answered. `ok` is true only when every tested bot delivered.
The test goes out while `notifiers.telegram.enabled` is false too, and whatever an entry's
`min_severity`. An entry's `error` is the advice text of
[NOTIFICATIONS.md](NOTIFICATIONS.md#telegram-errors-and-what-they-mean), with every token
redacted. An `id` that is not configured answers
`400 {"error": "no Telegram bot with id 'nope' is configured"}`; with no bot at all the answer is
`{"ok": false, "bot_name": null, "error": "Telegram has no bot yet: add a bot token and a chat id", "results": []}`.
In demo mode nothing is sent: the answer is
`{"ok": false, "bot_name": null, "error": "demo mode: nothing is sent to Telegram", "skipped": "demo", "results": []}`
and a `notify.demo` event "Test message (demo, not sent): 👋 DSpace Sentinel is connected ·
demo-dspace" carries the message.

### `POST /api/actions/email-test`

Optional body: `{"address": "<one of the recipients>"}` (any letter case) to test one
recipient; without it the test email goes to every recipient, as one message.

```json
{"ok": true, "error": null,
 "results": [{"address": "ops@example.org", "label": "On call", "ok": true, "error": null},
             {"address": "boss@example.org", "label": "", "ok": true, "error": null}]}
```

The subject is "[Sentinel] Test email · <host>". It goes out while `notifiers.email.enabled` is
false too, and whatever a recipient's `min_severity`. A refused login or connection gives every
recipient the same `error`, one of the sentences of
[NOTIFICATIONS.md](NOTIFICATIONS.md#email-errors-and-what-they-mean), and the password never appears
in it; a recipient the server refuses gets `"refused by the mail server: 550 …"`. When email cannot
send yet the answer is `{"ok": false, "error": "email is not set up: add the Gmail address, app
password and at least one recipient", "results": []}`, and settings that can never send are named
the same way before any connection is opened: `"the SMTP username is not an email address: set
from_address (the address alerts come from)"`, `"the email password has a character that cannot be
sent: use letters, digits and symbols from the English keyboard"`. An address that is not a
recipient answers `400 {"error": "no email recipient 'x@example.org' is configured"}`. In demo mode
nothing is sent: `{"ok": false, "error": "demo mode: nothing is sent by email", "skipped": "demo",
"results": []}` plus a `notify.demo` event "Test email (demo, not sent): [Sentinel] Test email ·
demo-dspace" carrying the subject and both bodies.

### `POST /api/actions/discover`

Reads `<install_dir>/config/local.cfg`, `dspace.cfg` and `modules/assetstore.cfg`, lists
listening ports, probes systemd units and asks the backend and Solr for their versions (45 s
overall timeout → `504`). The answer never contains a secret. On a DSpace 9 host that runs the
backend as the runnable JAR (its own `dspace` unit) and the UI under PM2 it looks like this
(trimmed):

```json
{
  "findings": [
    "DSpace instance \"Jar Repository\" configured in /opt/dspace",
    "Database dspace on localhost:5432 as dspace",
    "Solr at http://localhost:8983/solr",
    "Solr core names start with dspace_ (solr.multicorePrefix)",
    "REST API at http://localhost:8080/server",
    "Angular UI at https://repo9.example.org",
    "Assetstore in /opt/dspace/assetstore",
    "DSpace scripts log to /opt/dspace/log/dspace-cli.log: added as the DSpace CLI log",
    "Found PostgreSQL on 127.0.0.1:5432 (local only)",
    "Found HTTPS on 0.0.0.0:443 (exposed publicly)",
    "systemd unit dspace is active",
    "systemd unit pm2-dspace is active",
    "pm2-dspace is PM2's own unit: it only proves the PM2 daemon runs (the homepage check shows whether the DSpace UI answers)",
    "DSpace 9.4 answers at http://localhost:8080/server",
    "Solr 9.8.1 answers at http://localhost:8983/solr",
    "No Tomcat unit: the Tomcat log (/var/log/tomcat9/catalina.out) is left out; the runnable JAR of DSpace 8+ writes its Tomcat messages to dspace.log"
  ],
  "suggested_config": {
    "postgres": {"host": "localhost", "port": 5432, "database": "dspace", "user": "dspace",
                 "password": "••••••", "service": "postgresql"},
    "solr": {"url": "http://localhost:8983/solr",
             "cores": ["dspace_search", "dspace_statistics", "dspace_oai", "dspace_authority"], "service": "solr"},
    "ssl": {"host": "repo9.example.org"},
    "nginx": {"https_url": "https://repo9.example.org", "service": "nginx"},
    "logs": {"files": [{"id": "dspace", "label": "DSpace", "path": "/opt/dspace/log/dspace.log", "daily_suffix": true, "timezone": ""},
                       {"id": "dspace-cli", "label": "DSpace CLI", "path": "/opt/dspace/log/dspace-cli.log", "daily_suffix": true, "timezone": ""},
                       "…solr and nginx, kept: both logs are there"]},
    "dspace": {"install_dir": "/opt/dspace", "server_url": "http://localhost:8080/server",
               "ui_url": "https://repo9.example.org", "public_url": "https://repo9.example.org",
               "assetstore_dir": "/opt/dspace/assetstore", "log_dir": "/opt/dspace/log",
               "backend_service": "dspace", "frontend_service": "pm2-dspace"},
    "ports": [{"port": 22, "label": "SSH", "expect": "open"}, "…",
              {"port": 5432, "label": "PostgreSQL", "expect": "local_only"}],
    "modules": {"postgres": true, "solr": true, "backend": true, "frontend": true, "nginx": true,
                "ssl": true, "ports": true, "security": true, "logs": true, "repository": true,
                "assetstore": true, "incidents": true}
  },
  "detected_persona": "full",
  "sources": ["/opt/dspace/config/local.cfg", "/opt/dspace/config/dspace.cfg",
              "/opt/dspace/config/modules/assetstore.cfg"],
  "properties": {"dspace.dir": "/opt/dspace", "dspace.name": "Jar Repository",
                 "db.url": "jdbc:postgresql://localhost:5432/dspace", "solr.multicorePrefix": "dspace_",
                 "assetstore.index.primary": "0", "…": "…"},
  "ports": [{"port": 5432, "label": "PostgreSQL", "public": false, "addrs": ["127.0.0.1"]}, "…"],
  "services": {"postgresql": "active", "solr": "active", "dspace": "active", "pm2-dspace": "active", "nginx": "active"}
}
```

- `suggested_config` is a patch for `PATCH /api/config` or the setup action. The PostgreSQL
  password from `local.cfg` and a password inside the Solr URL come back as `"••••••"`
  (`http://user:••••••@host/solr`). The real values stay in the server's memory for 30 minutes:
  applying the suggestion with the masks in place (onboarding, More › DSpace connection ›
  **Apply**) puts them back server-side, the password only when the patch's PostgreSQL host,
  port, database and user are the ones discovery found. `./sentinel.sh discover --apply` applies
  the real values in-process.
- `properties` holds only `dspace.dir`, `dspace.name`, `dspace.server.url`, `dspace.ui.url`,
  `db.url`, `db.username`, `solr.server`, `solr.multicorePrefix`, `assetstore.dir`,
  `assetstore.index.primary`, `log.dir` and `log.report.dir`, with credentials and query strings
  removed from URLs; findings show URLs the same way. The three files are read in that order and
  an earlier one wins; `assetstore.index.primary = 1` (an S3 assetstore) leaves the Assetstore
  module off.
- Units: `postgresql`, `postgresql@*`, `solr`, the backend units `tomcat9`, `tomcat10`,
  `tomcat11`, `tomcat`, `dspace-backend`, `dspace-server`, `dspace` (picked in that order, an
  active one first), the UI units `dspace-ui`, `dspace-angular`, `dspace-frontend`, `pm2-*`, any
  other unit matching `*dspace*` (Sentinel's own `dspace-sentinel` excepted), `nginx` and
  `apache2`. See [COMPATIBILITY.md](COMPATIBILITY.md#discovery-units-logs-and-versions).
- Versions: where a port, a unit or DSpace's configuration shows a backend or Solr, discovery
  sends one anonymous `GET <server_url>/api` and one `GET <solr>/admin/info/system` (3 s
  timeout). The findings then name the versions, "DSpace 7.x is end-of-life (7.6.7 was the final
  release; supported: 8.x, 9.x, 10.x)" and a mismatch such as "DSpace 9 expects Solr 9 but Solr
  8.11.4 answers".
- `logs.files` keeps your current list, points the `dspace` entry at DSpace's log directory
  (adding it when missing), adds a `dspace-cli` entry after it when that file exists, and makes
  the packaged Tomcat log follow the backend's unit (`/var/log/tomcat10/catalina.out` for
  `tomcat10`), leaving it out when no Tomcat unit exists and the file is not there. On a DSpace
  host the default nginx entry stays only for a web server that was found or a log that is there,
  and the default Solr entry only when `/var/solr/logs/solr.log` is there; otherwise the Solr
  entry follows the `-Dsolr.log.dir` of a running Solr (`<dir>/solr.log`) or is left out, each
  with a finding. Custom paths are never touched. `ports` lists
  only the rules whose port is listening now (the PostgreSQL rule follows the port in `db.url`),
  and `modules.ports` is true only when there is such a rule. The `nginx` module and
  `nginx.service` are suggested only for an nginx or apache2 unit that is active (or failed), not
  for one that is merely installed.
- A service that answers without a systemd unit (Solr on 8983 without `solr`, the UI on 4000
  without a UI unit, a web server on 80/443 without an active or failed nginx or apache2 unit) is
  suggested with an empty service name (`""`: no systemd rung, so no "unit … not found") and a
  finding such as "Solr answers on 8983 but no systemd unit was found for it: it may not start by
  itself after a reboot". This needs a visible systemd; without `systemctl` the names stay as they
  are.
- `modules` switches `nginx` on only for a web server (port 80 or 443, or such a unit) and `ssl`
  only for HTTPS (port 443, or an https `dspace.ui.url` that becomes `public_url`); the first-run
  wizard pre-selects them only then. `security` is always `true`: it reads this server, whatever
  runs on it.
- In demo mode discovery describes the demo stack, never this machine, and asks nothing. The
  first finding then reads "Demo mode: these findings describe the demo server, not this
  machine."; the persona is `full`, the units `postgresql`, `solr`, `tomcat10`, `dspace-ui` and
  `nginx` are active, ports 22, 80, 443, 4000 and 8080 are public and 8983 and 5432 local only,
  the findings end with "DSpace 9.4 answers at http://localhost:8080/server" and "Solr 9.6.1
  answers at http://localhost:8983/solr", and nothing is secret.

### `POST /api/actions/setup`

```json
{"persona": "full", "config_patch": {"postgres": {"host": "127.0.0.1"}, "modules": {"ssl": false}}}
```

Applies the persona preset (its modules and its `ports` rules, see
[CONFIG.md](CONFIG.md#setup)), then the patch, sets `setup.completed = true` and answers with
`/api/meta`. `persona` may be empty (custom); `config_patch` must be an object. Masked values
from a recent discovery are restored as described above. The same 400s as `PATCH /api/config`
apply: a patch may not change `server.*` (`server.timezone` aside), and re-pointing PostgreSQL
while a password is stored needs the password again.

### `POST /api/actions/clear-history`

Wipes samples, roll-ups, checks, component history and state, alerts, incidents, events and
repository statistics. Configuration, report files and sign-in sessions are kept.

```json
{"ok": true, "counts": {"samples": 0, "checks": 0, "alerts": 0, "incidents": 0, "reports": 3, "…": "…"}}
```

## Server-Sent Events

### `GET /api/stream`

`text/event-stream`, `Cache-Control: no-cache, no-transform`, `X-Accel-Buffering: no`. The
stream opens with a comment, then a `status` frame immediately.

```
: connected

id: 1
event: status
data: {"ts":1790246534.03,"mood":"ok","headline":"All systems healthy", ...same as /api/status}

id: 2
event: component
data: {"id":"solr","from":"ok","to":"crit","component":{"id":"solr","status":"crit","reason":"connection refused (localhost:8983)", ...}}

id: 3
event: alert
data: {"type":"opened","alert":{"id":21,"rule":"component_down:solr","severity":"critical", ...},"ts":1790246590.2}

id: 4
event: incident
data: {"type":"opened","incident":{"id":5,"title":"Solr outage: DSpace search unavailable", ...}}

id: 5
event: report
data: {"type":"created","report":{"id":4,"fmt":"html", ...}}

id: 6
event: heartbeat
data: {"ts":1790246624.0}
```

| Event | When | Payload |
|---|---|---|
| `status` | after every collector cycle, at most every 5 s | the `/api/status` object |
| `component` | a component changes status | `{id, from, to, component}` (`component` is the full ComponentHealth, `reason` and ladder included) |
| `alert` | an alert changes state | `{type: opened|escalated|resolved|acked|unacked, alert, ts}` |
| `incident` | an incident opens, gains an alert, or resolves | `{type: opened|updated|resolved, incident}` |
| `report` | a report is generated, soft-deleted or restored | `{type: created|deleted|restored, report}` |
| `heartbeat` | after 30 s without any other frame | `{ts}` |

The stream ends when the client disconnects or the server shuts down: an open dashboard never
holds up `stop` or `restart` (it closes within a second of SIGTERM). The UI opens
`EventSource('api/stream')` and falls back to polling `/api/status` every 15 s if the stream
errors (for example behind a proxy that buffers). With nginx use
`proxy_buffering off; proxy_read_timeout 1h;` as in [REVERSE-PROXY.md](REVERSE-PROXY.md).

```bash
curl -N http://127.0.0.1:9765/api/stream
```

## Static files

`GET /` serves `index.html` with `Cache-Control: no-cache`; `/static/*` assets get
`public, max-age=3600`. `index.html` references its stylesheets and `app.js` with
`?v=<version>` and carries an import map that rewrites every static `import` between the ES
modules (and `app.js` adds the same `?v=` to its dynamic imports), so a new release is never
served from the one-hour cache.

## Python client in ten lines

```python
import httpx

BASE = "http://127.0.0.1:9765"
headers = {"Authorization": "Bearer MY_TOKEN"}   # server.auth_token; leave out when neither it nor a login is set

with httpx.Client(base_url=BASE, headers=headers, timeout=10) as c:
    status = c.get("/api/status").json()
    print(status["mood"], status["headline"])
    for comp in status["components"]:
        print(f'{comp["emoji"]} {comp["label"]:<16} {comp["status"]:<8} {comp["reason"] or ""}')
```
