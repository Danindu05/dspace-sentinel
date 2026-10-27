# Alerts and incidents

Sentinel turns thresholds and ladder failures into a small number of well-behaved alerts, groups
related alerts into incidents, and tells you once when something opens, once when it escalates
and once when it is over. This page explains the rules, the state machine and how to tune them.

## Where alerts show up

- **Home › Alerts**: open alerts as tiles with an **Ack** button; an acknowledged one shows
  "Reminders are off. It stays listed until the cause is fixed." next to its **Acked** pill.
  Without an open alert the card says "✅ No open alerts — checks run every 15 seconds."; while
  something is past a threshold but no alert has opened yet it names it ("⚠️ Memory is above its
  warning threshold (83 %) — an alert opens if it stays there."); when checks could not run it
  names each one and why ("❔ No open alerts — Logs cannot be checked: permission denied
  (/dspace/log/dspace.log)."; "could not be checked this time: …" only for a check that timed out
  or raised), with an **Open Logs** button when only one could not run; and while the page has no
  fresh data it reads "🕓 No open alerts at the last check — …".
- **History › Incidents** and **History › Alert history** (collapsed, *Show more*).
- The Pulse band headline and the mood colour (butter for warnings, coral for a critical). An
  acknowledged alert no longer colours the Pulse band or the mood: once every open alert is
  acknowledged the headline reads "All systems healthy · 1 acknowledged" in the healthy colour.
  The alert stays listed with its **Acked** pill and "Reminders are off. It stays listed until the
  cause is fixed.", and the component keeps its state on its tile, its page, the Stack and History
  until the cause is fixed. A warning that escalates to critical is unacknowledged again and
  colours Home, and so does a new alert.
- Telegram, email and the webhook ([NOTIFICATIONS.md](NOTIFICATIONS.md)).
- `GET /api/alerts`, `GET /api/incidents`, the SSE `alert` and `incident` events, `doctor`'s
  ALERTS section, and the alerts section of every report.

## Rules

Every collector cycle the rule table (`alerts/rules.py`) looks at the latest system sample and
the current components and yields observations. Thresholds come from `alerts.*` in the config
([CONFIG.md](CONFIG.md#alerts)).

| Rule | Source | Threshold key | Title / message |
|---|---|---|---|
| `cpu` | `sample.cpu_pct` | `cpu` | "CPU 96 %" / "load 7.9 on 4 cores" |
| `memory` | `sample.mem_pct` | `memory` | "Memory 93 %" / "12.9 of 16 GB used, swap 40 %" |
| `swap` | `sample.swap_pct` | `swap` | "Swap 61 %" |
| `disk` (root) · `disk:<mount>` (each extra mount, e.g. `disk:/dspace`) | `sample.disk_pct` and every extra mount | `disk` | "Disk 87 % on /dspace" / "free 53.7 GB · growing 2.1 GB/day" |
| `load` | `load1 / cores` | `load_per_core` | "Load 3.2 per core" |
| `rest_latency` | `backend.rest` latency | `rest_latency_ms` | "REST API slow: 2.4 s" / "GET /server/api" |
| `search_latency` | `backend.search` latency | `search_latency_ms` | "Search API slow: 4.8 s" / "GET /server/api/discover/search/objects" |
| `solr_latency` | `solr.search` latency | `search_latency_ms` (shared) | "Solr search slow: 4.8 s" / "select q=*:* rows=0 on core search" |
| `home_latency` | `frontend.home` latency | `home_latency_ms` | "Homepage slow: 6.1 s" / "GET http://localhost:4000" |
| `postgres_connections` | `connections_pct` | `postgres_connections_pct` | "PostgreSQL connections 86 / 100" / "86 % of max_connections in use" |
| `ssl_expiry` | `days_left` (inverted) | `ssl_expiry_days` | "Certificate expires in 12 days" ("Certificate expired" at 0) |
| `log_errors:<file id>` (e.g. `log_errors:dspace`) | errors per file in the window | `log_errors_per_window` | "48 errors in DSpace log (5 min)" / last error line |
| `assetstore_free` | free GB (inverted) | `assetstore_free_gb` | "Assetstore free space 9.1 GB" |
| `component_down:<id>` | hard failures in the component's ladder | — | crit ⇒ critical "PostgreSQL unreachable" (the failing rung is a connectivity rung: `service`, `tcp`, `connect`, `admin`, `rest`, `home`, `http`, `https`, `cert`, `path`) or "DSpace Backend failing" (any other rung); warn ⇒ warning "DSpace Backend degraded". Message: the chain summary up to the failing rung, then its reason on the next line: `service ✅ → TCP ✅ → connect ❌` / `connection refused (localhost:5432)` |
| `security_ports` | the `security.ports` check: a port reachable from the network that no rule expects opened after Sentinel started | — | warning "New port open to the network" / the check's message, "9 listening · 6 reachable from the network · 1 unexpected: 3306 (mysqld)" |
| `security_firewall` | the `security.firewall` check: no host firewall active while ports are reachable without a rule, or a `local_only` port is public | — | warning "No host firewall while ports are exposed" / "no host firewall is active and 2 ports are reachable from the network without a rule", "… without a rule, and 1 local-only port is public", "no host firewall is active and 1 local-only port is public" (the count is the Open ports check's unexpected ports) |
| `security_ssh` | the `security.ssh` check: `PermitRootLogin yes` with password login on | — | warning "Root may log in over SSH with a password" / "root may log in with a password" |

`component_down` counts *hard* failures only. A rung that is warn or crit exactly because of its
own threshold (a slow search, a certificate with 12 days left, a log with 48 errors) is left to
that rule, and dependency rungs (`backend.dep.solr`) mirror another component's alert, so
neither opens a `component_down` alert. A rung that is worse than its threshold explains, such as
`backend.search` answering HTTP 500, is a hard failure. An informational rung never counts:
an actuator that is not exposed, or the anonymous `DOWN` that DSpace 7.6.4–7.6.x, 8.2–8.4,
9.0–9.3 and 10.0 report for their SEO health check while the REST API answers (a known DSpace
issue, see
[COMPATIBILITY.md](COMPATIBILITY.md#the-actuator-anonymous-answers-and-the-seo-down-window)).
Any other anonymous `DOWN` is a warning and opens "DSpace Backend degraded".

Rules for modules that are off never fire, and switching a module off resolves its open alerts
at the next evaluation (a few seconds later, not instantly on the toggle), without a
notification. `swap` is skipped on hosts without swap. The rule id after the colon names the
instance (mount, log file, component); the UI always shows the alert's `title` and `message`,
never the raw id.

A check that could not run measured nothing. When a collector times out or raises, its ladder
is one `unknown` rung (`<id>.error`, for example "check timed out after 29 s", marked
`detail.collector_error`); that cycle neither opens nor resolves an alert, and the component's
open alerts are not closed as "no longer observed" meanwhile. `component_down` is neutral in the
same way when a ladder has no `ok`, `warn` or `crit` rung apart from dependency rungs.

## Security

The Security module ([CONFIG.md](CONFIG.md#modules)) has eight checks, and only three of their
warnings open an alert, always a **warning**, never critical:

| Rule | Opens when | Title |
|---|---|---|
| `security_ports` | a port that other machines can reach and that no `ports` rule expects opened after Sentinel started (its verdict is *new*) | New port open to the network |
| `security_firewall` | no host firewall (ufw, nftables, firewalld) is active while a port is reachable from the network without a rule, or a `local_only` port listens publicly | No host firewall while ports are exposed |
| `security_ssh` | `sshd_config` says `PermitRootLogin yes` and password login is on | Root may log in over SSH with a password |

The message is the check's own message. These alerts go through the same `confirm_samples` and
`clear_samples`, quiet hours (a warning is held back during them) and channels as every other
alert; their keys are `security_ports@security`, `security_firewall@security` and
`security_ssh@security`. While one is open, its message follows the check's current message
silently, as for `component_down`: a second new port updates the open `security_ports` alert,
without a new notification. The Security component has no `component_down` rule.

The firewall check counts exactly the ports the Open ports check calls unexpected (verdict
*exposed* or *new*, `metrics.exposed`), so the two numbers on the Security page always agree; a
`local_only` port that listens publicly is named after them ("…, and 1 local-only port is
public"). A port that other machines reach only on Tailscale's addresses (verdict *Tailscale*,
"reachable through Tailscale only") and Tailscale's own WireGuard port count as neither, so they
never open an alert.

The other warnings never alert: pending security updates or a required reboot, software past
its end of support or affected by a known flaw, plain HTTP or missing security headers, a
`local.cfg` or Sentinel configuration file that other users may read, and Sentinel listening
on every interface with neither the dashboard login nor a token (with a login it stays ok, with a
hint to put HTTPS in front). They show on the Security page, in the Pulse band's
warning count (with no alert to acknowledge, they colour Home until the cause is fixed) and in
the Security section of reports. A warning about root login with a key
only (passwords off), or one that an included `sshd_config` file Sentinel cannot read might
change, is shown but does not alert either. A check that could not be checked (a file Sentinel
may not read, no systemd) neither opens nor clears an alert. The `security_ports` alert resolves
once the port is closed or a rule covers it. The ports open when Sentinel starts are its
baseline, so after a restart a port that was *new* reads *exposed*: still a warning on the page,
no longer an alert. Add a rule for a port you want, or close it.

## The state machine

Each `(rule, component)` pair keeps a breach counter, an OK counter and at most one open alert.

```
              breach × confirm_samples                 crit threshold crossed
  (nothing) ─────────────────────────► open/warning ─────────────────────────► open/critical
      ▲                                     │                                        │
      │          OK × clear_samples         │             OK × clear_samples         │
      └─────────────────────────────────────┴────────────────────────────────────────┘
                                          resolved
```

- **Open** after `confirm_samples` (default 2) consecutive breaching observations. With the
  15 s system interval that is 30 s of sustained high CPU before you hear about it; a single
  spike never alerts.
- **Escalate** warning → critical immediately when the critical threshold is crossed (one
  message, the same alert row, `severity` changes). Escalating clears an acknowledgement: the
  alert is unacknowledged again, colours Home and gets reminders again.
- **Resolve** after `clear_samples` (default 3) consecutive OK observations. An alert whose
  subject disappeared (module switched off, mount or log file removed) is resolved without a
  message, with the note "module switched off" or "no longer observed".
- **Hysteresis**: an observation counts as OK only when the value is below the warning
  threshold minus 2 % of the threshold range, or the component is `ok`. A disk hovering at
  79–81 % does not flap.
- **Ack** (`POST /api/alerts/{id}/ack`, the Ack button) sets `acked_ts`; the alert still resolves
  on its own. **Unack** reverts it. Both are instant in the UI with an Undo snackbar, and Home
  asks for the status again at once, so the band, the mood colour and the sidebar follow without
  waiting for the next check. Acknowledging silences reminders and calms Home; an escalation to
  critical makes the alert unacknowledged again. An acknowledged alert no longer colours the Pulse band or the mood: a component whose
  open alerts are all acknowledged counts as healthy there, and with nothing unacknowledged left
  the headline reads "All systems healthy · 2 acknowledged". The alert stays open and listed, with
  "Reminders are off. It stays listed until the cause is fixed." next to its **Acked** pill, and
  the component keeps its own state on its tile, its page, the Stack and History until the cause
  is fixed. `doctor` follows the same rule: its headline and exit code (0 when everything open is
  acknowledged), while its ALERTS section still lists each acknowledged alert with "acked". A
  component that warns without any open alert (a Security check that never alerts, a value past
  its threshold before `confirm_samples` is reached) still colours Home.
- **Reminders**: with `alerts.reminder_minutes > 0`, an open, unacknowledged alert is re-sent
  every N minutes. Default 0 = notify only on open, escalate and resolve.

Every transition writes an `events` row (`kind: alert`) and publishes a bus/SSE event
`alert` with `{type: opened | escalated | resolved | acked | unacked, alert, ts}`.

### The alert object

```json
{"id": 18, "rule": "component_down:solr", "component": "solr", "severity": "critical",
 "status": "open", "title": "Solr unreachable",
 "message": "service ✅ → TCP ❌\nconnection refused (localhost:8983)",
 "value": null, "threshold": null, "opened_ts": 1790169058, "resolved_ts": null,
 "acked_ts": null, "incident_id": 4, "notified_ts": 1790169059}
```

`value` and `threshold` are set for numeric rules; `incident_id` links to the incident the alert
was grouped into; `notified_ts` is when the last notification went out.

## Incidents

With the **Incidents** module on, alerts that belong together become one story.

**Grouping.** When an alert opens, the engine looks for an open incident with activity in the
last five minutes and attaches the alert to it. If there is none but another alert opened within
the window, a new incident is created grouping both. A lone alert stays a lone alert.

**Hypothesis.** The set of `(rule, component)` pairs in the incident is matched, in order,
against these signatures:

| Signature | Hypothesis |
|---|---|
| memory or swap + Solr (latency or down) + backend search or `search_latency` | Possible Solr resource exhaustion |
| disk + PostgreSQL | Disk pressure affecting PostgreSQL |
| PostgreSQL down + backend down or latency | Database outage cascading to the DSpace backend |
| Solr down + backend search / `search_latency` | Solr outage: DSpace search unavailable |
| backend down + frontend down | Backend outage: the UI cannot reach the REST API |
| cpu or load + `rest_latency` or `home_latency` | CPU saturation slowing responses |
| `assetstore_free` or disk + `log_errors` | Storage pressure causing application errors |
| anything else | Multiple alerts within 5 minutes |

The hypothesis is re-evaluated as alerts join, so "Multiple alerts" can become "Possible Solr
resource exhaustion" a minute later.

**Affected** is the dependency closure of the components involved, nearest first:
`solr → ["DSpace Search", "DSpace Backend"]`,
`postgres → ["DSpace Backend", "DSpace Search", "DSpace Frontend", "Repository site", "nginx"]`.
Only enabled components are listed.

**Sequence** is the alerts in time order as `{ts, text, severity}`; it is what the Telegram
message and the incident detail page show as a vertical timeline.

**Resolution.** An incident resolves when its last alert resolves; the duration is recorded and
one closing message goes out. Incidents are listed newest first at `GET /api/incidents` and in
**History › Incidents** (`#/history/incidents/24`). Every step writes an `events` row
(`kind: incident`) and publishes a bus/SSE event `incident` with
`{type: opened | updated | resolved, incident}` — `updated` when an alert joins or the
hypothesis changes.

```json
{"id": 4, "title": "Possible Solr resource exhaustion", "hypothesis": "Possible Solr resource exhaustion",
 "status": "resolved", "started_ts": 1790168848, "ended_ts": 1790169298,
 "affected": ["DSpace Search", "DSpace Backend"],
 "sequence": [{"ts": 1790168848, "text": "Memory 81 %", "severity": "warning", "alert_id": 19},
              {"ts": 1790168913, "text": "Memory 94 %", "severity": "critical", "alert_id": 19},
              {"ts": 1790168993, "text": "Solr response 4.8 s", "severity": "critical", "alert_id": 20},
              {"ts": 1790168993, "text": "Search API slow: 4.3 s", "severity": "critical", "alert_id": 21},
              {"ts": 1790169058, "text": "Solr DOWN", "severity": "critical", "alert_id": 22},
              {"ts": 1790169073, "text": "DSpace search unavailable", "severity": "critical", "alert_id": 23},
              {"ts": 1790169098, "text": "118 errors in DSpace log (5 min)", "severity": "critical", "alert_id": 24}],
 "alert_ids": [19, 20, 21, 22, 23, 24]}
```

## Notifications

`notifiers.dispatch(cfg, event)` fans each transition out to Telegram, email and the webhook, each
channel on its own, so one that fails never keeps another from sending:

- `opened`, `escalated` and `resolved` are sent when they happen, reminders while an alert is
  open and unacknowledged, and one message when an incident opens and when it resolves. Every
  message carries the severity of its alert (an incident is critical when one of its alerts is),
  and that severity decides who gets it and what quiet hours let through.
- **Who gets what.** Every Telegram bot and every email recipient takes **All alerts** or
  **Critical only** (`min_severity: warning | critical`). A Critical only person gets an alert
  that opens as critical or escalates to critical, its reminders and its resolution, and critical
  incidents; tests, digests and reports go to everyone.
- **Quiet hours** are set per channel (`notifiers.telegram.quiet_hours`,
  `notifiers.email.quiet_hours`, for example `"23:00-07:00"`, read in the zone of
  `server.timezone`, the server's own when empty; the form and the API refuse a value
  Sentinel cannot read, and one in a hand-edited file is left out at start-up with a warning in the
  log and a `config` row in the events log, see
  [NOTIFICATIONS.md](NOTIFICATIONS.md#quiet-hours)): inside a channel's window only
  critical alert and incident messages go out on that channel. Warning-level messages are
  dropped, not delayed: a warning's opening, its reminders, its "✅ Resolved" message and an
  incident made only of warnings. A disk warning that opens at 22:50 and clears at 23:10 therefore
  never gets its resolution message on that channel. The daily digest and the test messages
  always go through, and the webhook has no quiet hours.
- **Daily digests** are set per channel too (`daily_digest: "08:00"`, in the zone of
  `server.timezone` like the quiet hours): Telegram's goes
  to every bot and to the webhook (while Telegram or the webhook is switched on), email's to every
  recipient (while email is on). Each is one message: the last 24 hours as counts ("Last 24 h: 7
  alerts (2 critical) · 1 incident"), then the alerts open right now, oldest first, up to five and
  "… and N more".
- Delivery never holds up the checks, and one channel never holds up another. Notifications
  go into a background queue with one lane per channel: each channel sends one event at a time,
  in order, an event's channels go out side by side, and a channel that stalls only delays its
  own later messages, with two exceptions
  ([NOTIFICATIONS.md](NOTIFICATIONS.md#delivery-and-the-events-log)): once 500 events wait behind
  a stalled channel the others wait too (and beyond another 500 the oldest waiting event is
  dropped for every channel, with a warning in the log), and messages already waiting keep the
  settings in force when their event happened. Each channel has its own cap: Telegram 10 s per
  bot plus 5 s, email 20 s for each step of its SMTP session (65 s in all; a given-up session is
  cut off), the webhook 5 s per request. On shutdown Sentinel gives the queue up to 10 s; what is
  still under way or waiting then is given up, and the log counts it in one warning ("5
  notifications not delivered (Sentinel is stopping)").
- Every attempt leaves an `events` row naming its channel, written as soon as that channel is
  done (`data.channel`, and for Telegram and email one entry per bot or recipient in
  `data.destinations`): `notify` (delivered),
  `notify.error` (network, a refused token or login, a blocked bot — never interrupts the
  collector loop), `notify.skipped` (held back by quiet hours) and `notify.demo` (demo mode,
  message logged instead of sent). A channel that is switched off leaves no row, apart from the
  messages that were already waiting for it. `notified_ts` stays `null` until a delivery
  succeeds and is set when it does.

Setup, message formats and error messages are in [NOTIFICATIONS.md](NOTIFICATIONS.md).

## Tuning

| Symptom | What to change |
|---|---|
| Too many CPU alerts on a busy indexing server | raise `alerts.cpu` or `load_per_core`; raise `confirm_samples` to 4 (one minute) |
| Disk alert flaps around 80 % | the hysteresis already helps; raise `disk.warning` or add an extra mount to watch instead of `/` |
| Alerts at night wake you up | set `notifiers.telegram.quiet_hours: "23:00-07:00"` (critical still comes through; warnings in the window are dropped, including their resolution); email has its own `quiet_hours`, so warnings can still wait in the inbox |
| Someone only wants to hear about outages | set their Telegram bot or email recipient to **Critical only** |
| REST latency warnings during backups | raise `rest_latency_ms.warning`; or accept them and read the weekly report's p95 |
| You want a nudge until someone acts | `reminder_minutes: 30`, and use **Ack** when you are on it |
| Solr outage shows as five alerts | switch on the **Incidents** module |
| A log file spams errors you do not care about | remove it from `logs.files` or raise `log_errors_per_window` |
| "New port open to the network" for a port that should be open | add a rule for it under `ports` (`expect: open`) in `config/sentinel.yaml` and restart Sentinel; the Security checks have no thresholds to tune |
| Messages say "Started 15:59 UTC" while you live at +05:30, or quiet hours start at the wrong hour | set `server.timezone` to your zone, for example `Asia/Colombo` (**More › Alert thresholds › Time zone**): message times, quiet hours, digests and the weekly report follow it |

Everything above is editable in **More › Alert thresholds** (a settings page under More) and
applied without a restart; the same thresholds draw the hairlines on the History charts, so what
you see is what alerts. Its last group, *Times in messages*, holds the **Time zone**
(`server.timezone`, [CONFIG.md](CONFIG.md#the-time-zone-of-messages-and-schedules)); quiet hours
and digests are set in **More › Telegram** and **More › Email**.
