# Architecture

DSpace Sentinel is one Python process that observes a DSpace server and serves a dashboard
about it. This page explains how the pieces fit, in the order data flows through them. The
authoritative contract is [SPEC.md](SPEC.md); this is the tour.

```
                 ┌──────────────────────────── dspace-sentinel (one Python process) ───────────────────────────┐
                 │                                                                                             │
  psutil ──────► │  collectors/           scheduler.py            store.py (SQLite WAL)      api/ (FastAPI)    │
  systemctl ───► │  system  postgres      asyncio loop, one       samples · samples_hourly   /api/status       │ ◄── browser
  TCP/HTTP ────► │  solr    backend       task per interval       checks · component_*       /api/history      │     (ES modules,
  psycopg RO ──► │  frontend nginx        group, hard timeouts    alerts · incidents         /api/alerts …     │      no bundler)
  log tails ───► │  ssl ports security    ─► health.py roll-up    reports · events · kv      /api/stream (SSE) │
                 │  logs repository       ─► state.py (current)                              /  static UI      │
                 │  assetstore            ─► alerts/engine.py ─► incidents.py ─► notifiers/telegram,           │ ──► Telegram
                 │                                                               email, webhook                │ ──► email
                 │                        ─► reports/schedule.py ─► generator.py (HTML/MD/JSON) ───────────────│ ──► data/reports/
                 └──────────────────────────────────────────────────────────────────────────────────────────────┘
```

## Design constraints that shaped it

- **Observe only.** No module ever writes to DSpace, PostgreSQL, Solr, Tomcat, Node or nginx.
  PostgreSQL sessions are read-only with a 3 s statement timeout; Solr is queried with
  `rows=0`; log files are read from the end. The Security module shows the server's security
  status and never changes it: no port opened or closed, no firewall changed, no sudo.
- **Cheap.** Under 1 % CPU and 150 MB RSS on a typical repository server. One process, one
  SQLite file, no daemons beside it, no Prometheus/Grafana/Node.
- **Zero build step.** Python 3.10+ in a venv; the UI is plain HTML/CSS/ES modules served by
  FastAPI. `git clone && ./sentinel.sh install` is the whole deployment.
- **Modules off by default.** A module that is off leaves no trace anywhere: no checks, no
  tiles, no charts, no alerts, no menu rows.
- **Same data everywhere.** The UI, the JSON API and the CLI all read the same `build_status`
  payload; nothing is UI-only.

## Package map

| Path | Role |
|---|---|
| `dspace_sentinel/cli.py` | argparse entry point: `serve`, `demo`, `doctor`, `discover`, `report`, `telegram-test`, `email-test`, `set-password`, `hash-password`, service management, `version`; exports `config/sentinel.env` before loading the config |
| `auth.py` | the dashboard login, standard library only: scrypt password hashes (PBKDF2 where `hashlib.scrypt` is missing), the password and username rules, `Sessions` (the store's `sessions` table, token hashes only), `Throttle` (the brute-force limits, in memory) and `CliToken` (`<data dir>/api-token`) |
| `paths.py` | repo/data/config locations, all overridable through `SENTINEL_DATA_DIR` and `SENTINEL_CONFIG` |
| `config.py` | pydantic models mirroring `sentinel.yaml`, defaults, load/merge/save, env overrides (including the Telegram bot from the environment), masking of every secret (bot tokens kept by entry `id`; the login's `password_hash`), personas, `apply_login`; the start-up notices for what a file brought that cannot be used (an unreadable quiet-hours or digest time left out, email settings that can never send, a sixth bot from the environment, a login that cannot be read, which stays on), which never stop the start |
| `dspace_version.py` | parses the `dspaceVersion` the REST API reports and answers the version questions: the SEO DOWN window, the 9.3+ SSR Host rule, the Solr major each DSpace expects, the optional Solr cores, 7.x end of life |
| `models.py` | `Status`, `CheckResult`, `SystemSample`, `ComponentHealth`, `Alert`, `Incident`, `Report`, `Event` |
| `store.py` | the only module that touches SQLite: schema, migrations, every query, roll-ups, purge, export |
| `state.py` | in-memory current state: latest sample, components, sparklines, last cycle |
| `bus.py` | tiny asyncio pub/sub feeding the SSE stream |
| `health.py` | dependency graph, chain roll-up, dependency rungs, server thresholds, mood and headline |
| `scheduler.py` | the asyncio loop that runs collectors, feeds state/store/bus and calls the alert, incident and report hooks |
| `discovery.py` | reads `local.cfg`, `dspace.cfg` and `modules/assetstore.cfg`, lists ports, probes systemd units (`tomcat9`/`tomcat10`/`tomcat11`, the runnable JAR's unit, `pm2-*`, …), asks the backend and Solr for their versions, suggests a config: an empty service name for a service that answers without a unit, the nginx and Solr logs only where they are (a hand-started Solr's from the `-Dsolr.log.dir` on its command line, read with psutil) |
| `collectors/` | one file per module plus `system.py` (always on), `base.py` (ABC, `run_blocking`) and `demo.py` (synthetic stack) |
| `alerts/` | `rules.py` (observations), `engine.py` (state machine, reminders, the digests), `incidents.py` (correlation), `messages.py` (Telegram text), `email_messages.py` (email subjects and plain-text + HTML bodies built from the Telegram text), `notifiers/` (dispatch, the `Outbox`, `telegram.py` one bot per destination, `email.py` SMTP, `webhook.py`) |
| `reports/` | `analytics.py` (numbers), `svg.py` (inline charts), `generator.py` (HTML/MD/JSON), `schedule.py` (weekly job), `templates/` |
| `api/` | `app.py` (factory; middleware for the `Host` allowlist, the dashboard login or the token, and cross-site writes; static), `routes_auth.py` (`/api/session`, `/api/login`, `/api/logout`, `/api/account/password`, and `AuthService` on `app.state.auth`), the other `routes_*.py`, `stream.py` (SSE), `deps.py` (ranges, context, single-flight guard) |
| `static/` | the web UI: `index.html`, `css/tokens.css`, `css/app.css`, `js/*.js`, `js/screens/*`, `js/panels/*` |
| `util/` | `net.py` (TCP, listening TCP and UDP sockets), `httpc.py` (httpx client), `systemd.py`, `logs.py` (tail, timestamps), `support_dates.py` (end-of-support dates for the Security module, with their sources), `tz.py` (`server.timezone`: the zone of every time Sentinel sends, quiet hours, digests and the weekly report), `fmt.py` (times with their zone) |

## 1. Collectors

A collector is a small class (`collectors/base.py`):

```python
class Collector(ABC):
    id: str              # module id, e.g. "postgres"
    interval_key: str    # which collector.*_interval applies
    label: str; emoji: str; depends_on: list[str] = []
    budget_s: float = 0.0  # worst case of one collect(): its rung timeouts in a row plus slack
    async def collect(self, ctx: CollectorContext) -> CollectorResult
```

`CollectorContext` carries the config, a shared `httpx.AsyncClient`, the store, the current
state and `now`; `ctx.dependency("postgres")` returns the latest `ComponentHealth` of another
component so the backend can build its dependency rungs. `CollectorResult` holds the list of
`CheckResult` rungs, a `metrics` dict, and for the system collector a `SystemSample`; the
repository collector returns `repository_stats` instead of rungs. `history` carries extra
`checks` rows that are stored for the reports but never shown in the ladder (the PostgreSQL
collector records `postgres.connections`, its connection usage in percent, every
`db_interval`).

Blocking filesystem calls (`statvfs`, `stat`, log reads) go through
`collectors.base.run_blocking(key, fn, *args, timeout=)`: a daemon worker thread with at most
one call per key in flight. A hard-mounted NFS share that stopped answering can then neither
freeze the event loop nor pile up threads: while the previous call with the same key is still
stuck, the next one raises `ProbeStalled` ("… still blocked after N s (a filesystem is not
responding)"). The system collector probes the assetstore mount with a 3 s timeout and leaves
it out while it hangs; the assetstore collector reports "filesystem not responding (<path>)"
after 10 s.

`collectors/__init__.py` has the `REGISTRY` (module id → class) and `enabled_collectors(cfg)`.
A module whose file is missing or broken is logged and skipped; the rest keep running.

### Ladders

Every service collector produces its rungs in a fixed order (SPEC section 6). When a rung fails,
the collector stops and emits the remaining rungs as `unknown` with message `"not reached"`
(`health.not_reached`). A rung that answered correctly but crossed its latency threshold
(`backend.rest`, `backend.search`, `frontend.home`, `solr.search`) carries `detail.slow: true`
and does not stop the collector, so the actuator, the search API and the public URL are still
probed behind a slow REST API. `health.rollup()` then turns the list into one `ComponentHealth`:

- **status** = worst rung, where `unknown` only beats `ok` (`STATUS_RANK`: ok 0, unknown 1,
  warn 2, crit 3). A metrics-only component (repository) with no rungs is `ok` when it produced
  metrics.
- **reason** = message of the first rung that is not `ok`.
- **latency** = the component's headline rung (`postgres.query`, `solr.search`, `backend.rest`,
  `frontend.home`, `nginx.http`).
- **since** = carried over from the previous roll-up while the status is unchanged.

A rung may also carry `detail.hint`: its collector's advice in one or two plain sentences, never a
secret, which the component pages show as a muted line under the rung's message. The Solr
collector sets it for a core's `<lib/>` fix, the nginx collector when nothing answers and systemd
knows no web-server unit (switch the check off), the ports collector for a local-only port that
other machines can reach (why it matters, how to close or accept it) and the logs collector for a
file it may not read (`logs.permission_hint`: the owner, group and mode from `stat`, and whether
the user and group databases already list Sentinel's user in that group while this process does
not have it). The Security collector sets one on most of its warnings (the setting to change or
the command to run by hand) and on two ok checks (SSH that still accepts passwords, missing
response headers). A hint is advice: building it never fails the collector.

### Dependency rungs

`backend → [postgres, solr]`, `frontend → [backend]`, `nginx → [frontend]`. A dependency rung
(`health.dependency_rung`) mirrors the dependency's current status but is **capped at `warn`**:
a dead Solr makes the backend degraded, not down, because the REST API still answers. Its message
names the state in the UI's words: "PostgreSQL is healthy", "Solr is degraded (Warning): …",
"Solr is down (Critical): connection refused", "… could not be checked (Unknown): …", and "Solr is
slow (Critical): search took 4.3 s" for a dependency that is critical only because every one of its
critical rungs answered too slowly (`detail.slow`, `health.SLOW_STATE`); the text after the colon
is the message of the rung that decides the dependency's state (`health._deciding_rung`: the first
critical rung that failed for "is down", the first slow one for "is slow", else the first counted
rung with that status), so a slow REST API never explains "is down". Two conventions every
collector follows:

- the dependency's module is **off** → the `*.dep.*` rung is omitted entirely;
- the module is on but has not been collected yet → the rung is `unknown` "not monitored".

Virtual capabilities `search → [backend, solr]` and `repository-site → [nginx | frontend]` exist
only for the *affected* list of incidents (`health.affected_closure`).

### The server component

The system collector has no ladder. `health.server_status()` classifies each metric (`cpu`,
`memory`, `swap`, `disk`, `load`, plus `disk:<mount>` for extra mounts) against
`alerts.*` thresholds; the worst one is the server status and its text ("Disk 87 % on /dspace")
the reason.

### DSpace versions

The backend collector parses the `dspaceVersion` of `GET /server/api` with
`dspace_version.parse()` and publishes it in its metrics; the frontend and Solr collectors read it
through `ctx.dependency("backend")` (the 9.3+ client-side rendering note, which also compares the
host it sent with the REST root's `dspaceUI`, and which optional Solr cores apply), and discovery
uses the same helpers for its findings. The backend reuses one actuator answer for up to 300 s
per collector instance, because every call runs all of DSpace's health indicators. Only an
answer that graded ok or informational is kept, keyed on the URL and the raw `dspaceVersion`,
and it is dropped whenever the ladder stops before the actuator (service, TCP or REST), so a
failure, a restart or an upgrade is always asked again and the alert rules count real answers.
A configuration reload creates new collector instances, so they ask at once. What the versions
change is in [COMPATIBILITY.md](COMPATIBILITY.md).

## 2. Scheduler

`Scheduler` groups the enabled collectors by `interval_key` (system 15 s, service 30 s, http
30 s, db 60 s, logs 60 s, repository 900 s, security 900 s) and runs one asyncio task per group; the group runs
share one lock, so two groups never collect at the same time. Each `collect()` is wrapped in
`asyncio.wait_for(timeout=max(interval − 1, budget_s, 2 s))`, with budgets PostgreSQL 35 s, Solr
and backend 25 s, frontend 20 s, nginx 18 s, security 36 s; Solr's version probe and its per-core pings run
concurrently. A timeout or exception becomes a single
`CheckResult(id="<module>.error", status="unknown", detail={"collector_error": True})` so the
loop never dies; the alert rules treat such a cycle as neutral (nothing measured, nothing opened
or resolved). A result whose module was switched off while it was being collected is dropped.

After every group run the scheduler:

1. rolls the result up (`health.rollup` / `health.server_health`) and stores the
   `ComponentHealth` in `State`;
2. writes the rungs to `checks`, the sample to `samples`, the roll-up to `component_state`
   (survives restarts) and `component_history` (feeds availability);
3. records a `component` event and publishes it on the bus when the status changed;
4. publishes a `status` bus event (throttled to one per few seconds);
5. calls the hooks, each guarded: `alert_engine.evaluate(sample, components, now)` and
   `incident_engine.check_resolution(now)`. Nothing slow runs under the cycle lock: both engines
   queue their Telegram, email and webhook deliveries on one shared `alerts.notifiers.Outbox`
   that keeps one ordered lane per channel, so a stalled channel only delays itself (until 500
   events are under way, see [section 5](#5-alerts-incidents-notifications)), and
   `reports.schedule.maybe_run(store, cfg, now)` runs as its own single-flight task, so a weekly
   report, an unreachable Telegram API or a mail server that does not answer never delays the
   next check.

A **maintenance task** runs 90 s after start and then hourly: `store.rollup_hourly()` folds raw
samples older than two hours into `samples_hourly`, and `store.purge(retention_days)` deletes raw
rows past retention (samples by whole hours, so an hour that was rolled up is never rebuilt from
what is left of it), hourly rows past 365 days, resolved alerts/incidents past 365 days, trims
`events`, removes report files soft-deleted more than 24 hours ago and deletes expired sign-in
sessions.

`Scheduler.reload(cfg)` is how `PATCH /api/config` and `PATCH /api/modules` take effect without
a restart: groups whose collector set or interval changed are restarted (an unchanged group keeps
its running task and countdown and just gets the new collector instances), a group that gained a
collector runs immediately, components of modules switched off are removed from `State` and
`component_state` (history is kept), and the alert and incident engines get `reload(cfg)`. The
API, not the scheduler, logs the change ("Solr turned on").

`Scheduler.run_all_now()` backs **Check now**. While the service runs it is coalesced: a call
during a running check-now waits for it, and a call within 10 s of the previous one returns at
once (a reload or **Clear history** always allows a fresh run). The repository group (its
`count(*)` scans) only runs when its own interval is due. `doctor` and the start-up cycle are
never coalesced.

On start the scheduler restores the last `component_state` rows into `State` so the dashboard
is not blank while the first cycle runs, then runs every group once. On shutdown it gives the
notification outbox up to 10 s to deliver what is queued; whatever is still under way or waiting
then is dropped with one warning, "N notifications not delivered (Sentinel is stopping)".

## 3. Storage

SQLite in WAL mode at `data/sentinel.db`, opened once, guarded by a lock, called from the event
loop through `asyncio.to_thread` for bulk queries. `Store` is the only module that speaks SQL.
The database file is created with mode 0600 before SQLite opens it (so the `-wal` and `-shm`
files get the same mode), and existing files are tightened on open.

| Table | Contents | Retention |
|---|---|---|
| `samples` | one row per system sample (cpu, mem, swap, disk, load, net, `extra` JSON with per-core and extra mounts) | `retention_days` (30) |
| `samples_hourly` | avg/max roll-ups | 365 days |
| `checks` | every rung result `(ts, check_id, status, latency_ms, value, message)`, plus history-only rows such as `postgres.connections` | `retention_days` |
| `component_state` | latest roll-up per component, JSON chain and metrics | current |
| `component_history` | `(ts, component_id, status, latency_ms)` for availability strips | `retention_days` |
| `alerts`, `incidents` | see [ALERTS.md](ALERTS.md) | resolved rows 365 days |
| `reports` | metadata of generated files, soft delete via `deleted_ts` | files purged 24 h after delete; at most 200 files / 500 MB |
| `repository_stats` | items, bitstreams, communities, collections, db size, assetstore bytes | 365 days |
| `events` | audit trail: status changes, alerts, config edits, notifications, errors | last N rows |
| `kv` | schema version, demo backfill flag, when each daily digest last went out (`last_digest_ts` for Telegram and the webhook, `last_email_digest_ts` for email) | — |
| `sessions` | the dashboard login's sessions: the SHA-256 of each token (never the token), username, created / last seen / expires times, `remember`, the client's address and User-Agent, a fingerprint of the login it signed in with | until it expires or idles out (purged by maintenance); kept by Clear history, never exported |

Query helpers worth knowing: `series(metric, t_from, t_to)` picks the bucket from the range
(≤ 360 points; raw samples up to 7 days, hourly roll-ups beyond, merged with per-hour
aggregates of the raw samples newer than the last roll-up), `availability(component, …)` builds
the strip and uptime from `component_history` (each row counts until the next one, capped at
twice the typical spacing, so a gap is not an outage; it also returns `covered_s`),
`latency_series(check_id, …)` buckets one rung, `repository_delta()` and `repository_series()`
feed the growth numbers, `export(stream=True)` streams a range as one JSON document table by table
through a separate read-only connection, and `prune_reports()` enforces the report cap.

## 4. State and the bus

`State` is the in-memory truth for "now": the latest `SystemSample`, the dict of
`ComponentHealth`, 12-point sparklines per server metric, `last_cycle_ts` and the `checking`
flag. `build_status()` in `api/routes_status.py` renders it into the `/api/status` payload —
the same object the UI receives over SSE and `doctor --json` prints.

`Bus` is a minimal publish/subscribe with bounded per-subscriber queues (a slow browser drops
frames instead of growing memory). Event names: `status`, `component`, `alert`, `incident`,
`report`.

## 5. Alerts, incidents, notifications

`alerts/rules.py` turns a sample and the components into `Observation`s; `engine.py` keeps a
per-rule counter and opens, escalates, resolves and acknowledges `Alert` rows with anti-flap
confirmation and hysteresis; `incidents.py` groups alerts opened within five minutes and names a
hypothesis. Every transition writes an `events` row and a bus event at once; the delivery itself
is queued on the `Outbox` and sent in order in the background (`notified_ts` is set once it
succeeded, and `AlertEngine.flush()` / `IncidentEngine.flush()` wait for the queue in tests and
on shutdown).

`notifiers.dispatch(cfg, event)` sends an event to the three channels side by side, each on its own:
it skips a channel that is off, applies that channel's quiet hours, keeps a **Critical only**
destination to critical alert and incident events, caps each channel's time (Telegram 10 s per bot
plus 5 s, email 65 s, the webhook 15 s) and waits for its turn in that channel's lane of the
`Outbox`, which keeps the channel's events in order. `notify()` writes each channel's events row as
soon as that channel is done. Two limits of the lanes: a job holds its place in the `Outbox` until
its slowest channel is done and at most 500 jobs run at once, so once 500 events wait behind a
stalled channel the others wait too (and beyond another 500 waiting the oldest is dropped for every
channel); and a job decides its channels, destinations and settings when it starts, right after its
event, so deliveries already waiting in a lane keep the settings of that moment after a save.
`notifiers/telegram.py` sends through every destination's own bot (`sendMessage`, `sendDocument`,
`getMe` for the test), one after another with a 10 s timeout each, redacts every token from every
error and turns Telegram's refusals into advice, and a request that got no answer (a time-out, a
refused connection, a failed DNS lookup, a TLS error) into what happened and what to check,
naming the proxy from `HTTPS_PROXY` by host and port when the requests go through one.
`notifiers/email.py` builds one `EmailMessage` per event for all the recipients who take it
(subject and bodies from `email_messages.py`) and sends it with `smtplib` in a daemon thread, 20 s
per socket step, TLS verified with the system CA store, so a hanging mail server can never hold up
a shutdown, and a session given up on (its cap, or a shutdown) is cut off so the message is not
handed over later, also when that happens during the STARTTLS handshake; SMTP failures become one
sentence without the password, a failure it has no sentence for is named by its exception class
only, and a login SMTP cannot send (a character outside ASCII) is named before any connection is
opened (the configuration refuses one when it is saved). `notifiers/webhook.py` posts the JSON
envelope. A failing channel logs an event and never raises. Full description in
[ALERTS.md](ALERTS.md) and [NOTIFICATIONS.md](NOTIFICATIONS.md).

## 6. Reports

`reports/analytics.py` computes one dict (summary with a weighted health score, server
percentiles, per-service availability, latency percentiles, alerts, incidents, repository growth,
log top errors, recommendations) from the store; `generator.py` renders it with Jinja2 into a
self-contained HTML file that makes no external requests (inline CSS from the same tokens, system
fonts, inline SVG charts from `svg.py`), Markdown or JSON under `data/reports/`, keeps at most
200 files / 500 MB and refuses to write below 1 GiB free (`ReportSpaceError`, HTTP 507), and
delivers a file through every Telegram bot or by email (`deliver_telegram_async`,
`deliver_email_async`, each recording a `notify` event); `schedule.py` runs the weekly job and
sends the file on as `reports.weekly.send_telegram` / `send_email` say. See
[REPORTS.md](REPORTS.md).

## 7. API and UI

`api/app.py:create_app()` wires everything: it refuses a non-loopback `server.host` with neither
the dashboard login nor an auth token (`check_bind`, `UnsafeBindError`, unless
`server.allow_unauthenticated` or `SENTINEL_ALLOW_OPEN=1`), builds the store, writes each start-up
notice of the configuration to it as a `config` event, builds the bus, state, engines, scheduler
and `AuthService` (`app.state.auth`: sessions, the brute-force limits, the CLI token and the watch
on the config file), starts the scheduler and writes a new local CLI token to
`<data dir>/api-token` in the FastAPI lifespan, installs the `{"error": …}` exception handlers (an
unexpected error answers a short 500 and goes to the log), mounts the routers and `static/`, and
wraps the app in `SentinelMiddleware`. The middleware checks, in this order: the `Host` header
against the allowlist (400); then, after `AuthService.refresh()` has re-read a `server.login`
changed in the file (at most every 2 s), the login or the token (401: with a login, everything but
the page, `/static/`, `/api/session` and `/api/login` needs a session cookie, `server.auth_token`
or the CLI token, and the 401 has no `WWW-Authenticate`; with only a token, Basic or Bearer and the
Basic challenge); and for writes the cross-site markers and the content type (403, 415); then it
sets cache headers for static files. Only the exact path `/healthz` skips the first three. The
sign-in routes keep only SHA-256 hashes of session tokens in the store, verify passwords in a
worker thread, and record `auth` events (never a password). `PATCH /api/config` and setup refuse to change `server.*`, except
`server.timezone`, which decides nothing about who reaches the API (`config.server_access`);
discovery keeps its raw suggestion (with the secrets) in `app.state` for 30 minutes so a masked
suggestion can be applied. A SIGTERM/SIGINT hook sets `app.state.shutdown_event`, which ends open
SSE streams within a second, and uvicorn's graceful shutdown is capped at 5 s. Routes are documented
in [API.md](API.md).

The UI (`static/`) is ES modules without a framework or bundler: `app.js` boots (`GET
/api/session` → the sign-in screen while a login is set and the browser has no session; else load
`/api/meta` → onboarding gate or shell; a 401 later shows the sign-in screen again with "Your
session ended. Sign in again."), opens `EventSource('api/stream')` with a 15 s polling
fallback, sets the `--accent` token from the mood and marks the page stale (`store.stale`:
`offline`, or `overdue` after max(45 s, 3 × `collector.system_interval`) without a completed
check); `store.js` holds client state; screens export `render(root, params)` and patch the DOM
in place. Design principles are in [DESIGN.md](DESIGN.md).

## 8. CLI and `sentinel.sh`

`sentinel.sh` is a thin bash wrapper. It resolves its own real path (so a symlink such as
`/usr/local/bin/sentinel` works) and exports `PYTHONPATH=<checkout>`, so it runs from any
directory. `install` refuses to run under `sudo`, picks the first Python ≥ 3.10 that has both
`venv` and `ensurepip`, creates the venv, installs requirements, copies the example config,
checks that the config loads, asks for the dashboard login (`set-password`, only on a terminal
and while there is none; `--no-login` skips it, and without a terminal it prints "No dashboard
login yet: run ./sentinel.sh set-password") and runs discovery; everything else is
`exec .venv/bin/python -m dspace_sentinel "$@"`. `set-password` writes `server.login` through the
file layer (a running server notices the file within seconds), `hash-password` prints only a hash
for the Ansible role, and `status` asks the running server with the local CLI token from
`<data dir>/api-token`, so it works with a login set. Every CLI command except the demo first exports
`config/sentinel.env` (variables already set win). `doctor` builds an in-memory store, runs every
enabled collector once and prints `render_doctor()` (exit 0 ok, 1 warnings, 2 critical);
`discover --apply` writes the suggested config; `serve` runs uvicorn in the foreground and
`start` detaches it with a pid file when no systemd unit of this checkout is installed. The exit
codes are shared: 0 ok, 1 warnings or not running, 2 critical, 3 not in this build, 4 the
command could not run, 64 usage error, 130 interrupted.

Service management only touches the `dspace-sentinel` unit whose `WorkingDirectory` is this
checkout (another checkout's unit is left alone and this checkout falls back to its pid file).
`start` refuses a port that already answers and checks that the listener is its own child;
`install-service` stops a leftover `start` process, refuses a busy port, restarts an active unit
so a new unit or port applies, and waits for `/healthz`, printing the journal when the unit does
not come up.

## 9. Demo mode

`SENTINEL_DEMO=1` replaces every collector with `collectors/demo.py` and never asks for the
dashboard login (a `server.login` is ignored, `/api/session` says no login is required). `DemoWorld` is a
deterministic function of time: calm operation with diurnal noise and, every 20 minutes, a
scripted incident (memory climbs → Solr slows → Solr down → backend search fails → recovery).
On first start `DemoSuite.backfill()` inserts seven days of samples, checks, component history,
repository stats, alerts and incidents in about two seconds, so charts, availability strips,
incidents and reports have data immediately (the backfill also writes `postgres.connections`
history and uses live-style alert ids such as `log_errors:dspace`). A demo restarted later keeps
its growth counters anchored to the first start, so repository deltas never go backwards.
`SENTINEL_DEMO_FAST=1` compresses the live cycle to three minutes and ticks every collector group
every 5 s. `./sentinel.sh demo` writes to its own `data/demo/` directory; what the UI changes
during a demo (onboarding, modules, thresholds, theme) is saved to `data/demo/demo-sentinel.yaml`
next to the demo database, as the difference from the demo configuration and never with
`server.*` or secrets, and reloaded by the next demo run, so the real `config/sentinel.yaml` is
never touched. Discovery in demo mode describes the demo stack, never the host. The stack is a
DSpace 9.4 as an anonymous client sees it: an actuator answering `UP_WITH_ISSUES` without
components (and `DOWN` with HTTP 200 while Solr is down), six Solr cores, a server-rendered UI,
`tomcat10`, and a Security component that the real `security.build_ladder` builds from the demo
server's facts (Ubuntu 24.04, ufw active, every check ok, two hints). Nothing leaves the machine in demo mode: every Telegram call is refused inside the
notifier before a request is built and no SMTP connection is opened; the would-be messages, the
test messages and report deliveries are logged as `notify.demo` events.

## 10. Security boundaries

- Binds `127.0.0.1:9765` unless told otherwise, and refuses a non-loopback address with neither
  the dashboard login nor an auth token unless explicitly allowed; the `Host` allowlist keeps
  DNS-rebinding pages out and writes from other sites are refused; nginx HTTPS recipe for remote
  use.
- The dashboard login (`server.login`, one admin account) stores only a scrypt hash of the
  password, the store only the SHA-256 of each session token; the cookie is `HttpOnly`,
  `SameSite=Strict` and `Secure` over HTTPS; sessions end after 12 h idle or 7 days (30 days with
  "Keep me signed in") and with a changed password; failed sign-ins pause per address and lock a
  username after 20. The login is changed only by `set-password` and the signed-in admin's
  password change, never by `PATCH /api/config`; `server.auth_token` stays a token for scripts,
  and the local CLI token (`<data dir>/api-token`, mode 0600) serves the CLI on the same machine.
  Demo mode never asks for a login.
- Secrets live in `config/sentinel.yaml` (mode 0600), `config/sentinel.env` or the environment.
  `config.masked()` is the only path from config to a client (the webhook URL counts as a
  secret, and so do the email password and every Telegram bot token), and a save writes only
  the file's own layer (`Config._file` plus the patch), never environment values. A masked bot
  token is restored from the entry with the same `id`, and a mask without a known `id` is
  refused, so a token never moves to another chat. The email password is likewise only sent
  to the SMTP server and login it was saved for, and one from `SENTINEL_SMTP_PASSWORD` cannot be
  changed or re-pointed through the API. Telegram errors have every token redacted (and name a
  proxy by host and port only, never its login) and SMTP errors the password removed before they
  are logged or shown, Gmail's own answer to a refused login included; an SMTP failure Sentinel has
  no sentence for is named by its exception class only, never by its text. Email is sent over STARTTLS
  or TLS with the certificate verified; `security: none` never sends a password to another host. Discovery masks the secrets it found in its answer and restores them
  server-side, the database password only for the PostgreSQL server it was found for; the
  stored password is likewise never sent to a re-pointed server without being typed again.
- The systemd unit runs as an unprivileged user with `NoNewPrivileges`, `ProtectSystem=strict`,
  `ReadWritePaths` limited to `data/` and `config/`, `PrivateTmp`, `Nice=10`, `CPUQuota=25%`,
  `MemoryMax=300M`, reads `config/sentinel.env` through `EnvironmentFile=-` and gives up after
  five failed starts in two minutes (`StartLimitBurst`).
- The monitoring process runs only `systemctl is-active`, `systemctl show
  --property=LoadState` and, during discovery, `systemctl list-units`, each with a 3 s timeout
  and `--` before the unit name; unit names in the config are validated. Discovery's version
  probe is two anonymous GETs to the backend and Solr (3 s timeout), only where they run.
- The Security collector only reads: the socket table, the names of the network interfaces
  (`psutil.net_if_addrs`, for Tailscale's `tailscale0`), its own effective user id, `systemctl
  is-active` for ufw, nftables and firewalld, `/etc/ufw/ufw.conf`, `/etc/ssh/sshd_config` and its `Include` files, Ubuntu's
  update-notifier file and `/var/run/reboot-required(.pkgs)`, `/etc/os-release`, `stat` of
  `local.cfg`, `dspace.cfg`, `sentinel.yaml` and `sentinel.env`, and one GET of
  `dspace.public_url` per run (10 s, headers only). Every source sits behind
  `security.HostSources`, every read is guarded, and a source that fails makes its rung
  informational, never the component unknown.
