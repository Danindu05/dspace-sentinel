# Reports

A report is a self-contained document about one period: how healthy the repository server was,
what went wrong, how fast it answered and how quickly it is growing. It exists so that the person
who asks "how did the repository do last week?" gets one file instead of a screen share.

## Generating one

**In the UI (two clicks).** Click **＋ New report** in the sidebar; the panel opens with
*Last 24 hours* and *HTML* preselected; click **Generate report**. The button reads
"Generating…", the panel closes and a snackbar says "Report ready" with an **Open** action that
opens the HTML in a new tab (*Download* for Markdown and JSON). Reports are listed under
**History › Reports** as tiles titled by the title you typed, else by their range ("Last 24
hours") or, for a custom range, by the days it covers ("Sep 20 – 24"); the subtitle says when it
was generated and how big it is ("… · generated Sep 25, 01:42 PM · 5.7 KB"). Each tile has
*Open*/*View*, *Download* and *Delete* (with Undo); the title links to a detail page
(`#/history/reports/<id>`) with the report's facts ("Generated", "Covers Sep 22 – 23 · 2 days")
that renders it inline.

Panel options: range (Last 24 hours, 7 days, 30 days, Custom with two dates), format (HTML,
Markdown, JSON), an optional title (empty ⇒ "DSpace Sentinel report · <host> · <range>"), and
under *More options* the section checkboxes, *Send to Telegram* ("Deliver the file to every
Telegram bot as soon as it is ready.") and *Send by email* ("Email the file to every recipient as
an attachment."). Each delivery switch is shown only while its channel can deliver: Telegram
switched on with at least one bot that has a token and a chat id, email switched on and set up.
With neither, the panel says "📨 Telegram and email are off — set one up in More › Telegram or
More › Email to get reports delivered.". The snackbar names what happened to each delivery:
"Report ready · sent to Telegram and email", "Report ready · sent by email", "Report ready ·
sent to Telegram · not sent by email: <reason>", or in demo mode "Report ready · Demo: logged,
not sent". Only the sections whose module is on are offered (Response times, Incidents,
Repository and storage, Logs, Security), and the panel always sends its explicit list. It reads its
choices from `GET /api/reports/options`.

**From the shell.**

```bash
./sentinel.sh report                                   # 24 h, HTML; prints the file path
./sentinel.sh report --range 7d --format md
./sentinel.sh report --range 30d --format json
./sentinel.sh report --range 7d --telegram             # also send it through every Telegram bot
./sentinel.sh report --range 7d --email                # also email it to every recipient
./sentinel.sh report --range 7d --telegram --email     # both
```

With `--telegram` or `--email` the command prints what happened to each delivery: `Telegram: sent` /
`Email: sent`, `Email: demo mode: logged, not sent`, or `Email: not sent: <reason>` on stderr with
exit code 4 (the report itself is written either way, and one channel failing does not stop the
other). `--json` adds a `telegram` and an `email` object with the result. A channel that is switched
off answers "not sent: Telegram is not enabled" / "email is not enabled". A file over 17 MB, counted
like every size Sentinel shows (17 × 1024 × 1024 = 17,825,792 bytes), is not emailed ("… is too
large to email (17.1 MB; the limit is 17 MB)", the size rounded up), and over an uplink slower than
roughly 10 Mbit/s even a large file under the limit can fail with "<host>:<port> did not answer
within 20 s", because the whole message must reach the mail server within that time; a shorter
range, Markdown or JSON, or Telegram gets it through. An HTML or Markdown report travels as a UTF-8
text attachment, so its `·`, `→` and emoji arrive intact in mail clients that show it inline.

**From the API.**

```bash
curl -s -X POST http://127.0.0.1:9765/api/reports \
  -H 'content-type: application/json' \
  -d '{"range": "7d", "fmt": "html", "title": "Week 38", "sections": ["summary", "services", "latency", "alerts"]}'
```

`range` is `"24h"`, `"7d"`, `"30d"` or `{"from": <epoch>, "to": <epoch>}`. A custom range must
start within the last 366 days, span at most 366 days and end no more than 5 minutes in the
future. The answer is the report row (`201`, with `filename` and `exists`; plus
`telegram: {ok, error}` when `send_telegram` was set and `email: {ok, error, results}` when
`send_email` was). One report is generated at a time (a
second request meanwhile answers `429` with `Retry-After: 5`), and generation is refused with
`507` when the data filesystem has less than 1 GiB free. `GET /api/reports/{id}` returns the file
(`?download=1` forces a download, `?meta=1` returns the row instead); `GET /api/reports/options`
lists the sections, formats and ranges. See [API.md](API.md#reports).

## Files

Reports are written to `data/reports/` as
`sentinel-report-YYYYMMDD-HHMM-<range>.<html|md|json>` (the date and time in the zone of
`server.timezone`) and indexed in the `reports` table.
Deleting one in the UI is a soft delete (Undo for six seconds; the row keeps `deleted_ts` and
`purge_after_ts`); the file is removed for real by the hourly maintenance job 24 hours later,
after which *restore* answers `409`. `data/` is git-ignored and survives updates.

Sentinel keeps at most 200 report files and 500 MB of them: generating one more removes the
oldest reports (soft-deleted ones included) and logs a `report` event such as "Removed 3 old
reports to stay within 200 reports / 500.0 MB". A report is refused when the data filesystem
has less than 1 GiB free: "not enough free disk space for a report: 812.4 MB free on
/opt/dspace-sentinel/data/reports (Sentinel keeps at least 1.0 GB free)". The CLI prints that and
exits 4; the API answers `507`.

### Times in a report

Every time in a report is in the zone of `server.timezone` (the server's own when it is empty,
[CONFIG.md](CONFIG.md#the-time-zone-of-messages-and-schedules)) and names it:
"07 Oct 2026 21:29 +0530" in the header, the alert and incident lists and the Security heading,
"21:29 +0530" on an incident's timeline, and "2026-09-17 10:00 → 2026-09-24 10:00 UTC" as the title of a custom
range. The chart ticks, the file name and the Telegram caption or email that delivers the report
follow the same clock, and the JSON report names the zone in `meta.timezone`. A file keeps the
zone it was generated in. The dashboard itself (History, the report tiles) shows your browser's
time.

## Sections

The `sections` list (default `reports.default_sections`) selects what is rendered:

| Section | Contents |
|---|---|
| `summary` | health score, availability, incidents, alerts opened (critical count), worst component, server uptime, period and host |
| `server` | CPU, memory, disk, load: average, p95, maximum; free disk now; network averages; CPU/memory/disk line charts |
| `services` | one row per enabled component: availability %, downtime, number of outages, worst status, average latency |
| `latency` | REST API, search API, homepage, PostgreSQL query and Solr search: p50, p95, max with charts and the warning/critical hairlines |
| `alerts` | every alert of the period with open/resolve times and duration |
| `incidents` | every incident with hypothesis, sequence and affected services |
| `repository` | items, bitstreams, communities, collections, DB size, assetstore bytes now; the delta over the period; growth per day; days until the assetstore disk is full |
| `logs` | top error lines by count per log file and totals |
| `security` | the Security module: every check that warns now, with its message and hint; the checks that warned earlier in the period; every check with its status, result and warnings this period; the open-ports table (Port · Program · Address · Reachable · Expected · Verdict, new and exposed ports first, at most 60 rows in HTML and Markdown) and the software table (Software · Version · Supported until · Status); "No security findings." when nothing warns |
| `recommendations` | see below |

A section whose module is off is omitted; a section with no data says so in one line.

### The Security section

"🛡️ Security" sits between Logs and Recommendations and is one of the default sections. It shows
the latest check, whatever the period (its heading says "checked <date time>", so a report about
a past week is not misread), and the history of each check over the period:

- the checks that warn now, each with its message and its hint ("5 security updates pending" /
  "sudo apt update && sudo apt upgrade, then reboot when it says so.");
- *Earlier in this period*: the checks that warned during the period but not now, with "warned in
  N of M checks" and the last time;
- a table of every check with its status (*Note* for one that only informs or could not be
  checked), its result and "Warnings this period";
- the open ports: "7 listening · 5 reachable from the network · 0 unexpected" (ports reachable
  only through Tailscale are not among the reachable ones), then the table, with the verdict
  pills *Expected*, *Local* and *Tailscale* (plain) and *Exposed* and *New* (warning colour); the
  JSON report holds every port;
- the software with the day its support ends, pills *Supported*, *Ends soon* (plain: support ends
  within 90 days), *Ended* and *Affected* (warning colour) and *Unknown*.

When there is nothing to show it says so in one line: "🛡️ The Security module is off.", "🛡️ No
security check has run yet.", "✅ No security findings." or "🛡️ The last security check gave no
result: <reason>" (Markdown: the same words in italics, without the emoji). With the module off
the section stays in the default list and reads "The Security module is off." when it is asked
for; the panel does not offer it then.

## The health score

`summary.health_score` is 0–100: the availability of each service over the period, weighted
by how much a user notices it — backend 30, PostgreSQL 25, Solr 15, frontend 15, nginx 10, every
other component 5 — normalised over the components that are actually enabled. A week with Solr
down for an hour scores around 99; a day with the backend unreachable for two hours scores in
the low 90s. Availability counts `ok` and `warn` as up and `crit` as down, exactly like the
strips on the History tab.

## Recommendations

Plain sentences with a severity (`critical`, `warning`, `info`, `ok`), worst first, generated
from the data:

| Condition | Recommendation |
|---|---|
| no samples in the period | "Not enough data for this period yet — Sentinel needs at least a few minutes of samples." |
| the assetstore or a disk full within 60 days at the measured growth (critical under 14 days) | "Assetstore /dspace is growing 1.2 GB/day; at this rate it is full in 47 days (56.4 GB free)." |
| certificate at or below the critical threshold / below the warning threshold (at least 30 days) | "The SSL certificate expires in 5 days — renew it now." / "… in 21 days — plan the renewal." |
| PostgreSQL connection usage p95 above 70 % of `max_connections` | "PostgreSQL connection usage p95 reached 78 % of max_connections — check the DSpace pool size or raise max_connections." |
| a service below 99.5 % availability (critical under 95 %) | "Solr was available 98.90 % of the time (3 outages, 1 h 12 m down) — below the 99.5 % target." |
| REST API p95 at or above the warning (or critical) threshold | "REST API p95 latency 1.4 s is above the 1.0 s warning threshold." |
| swap in use for at least half the period | "Swap was in use for 64 % of the period (avg 12 %, max 41 %) — check JVM heap sizes against the RAM available." |
| log errors trending up (second half at least 10 and 1.5 × the first) | "Log errors trended up: about 240 in the second half of the period against 80 in the first." |
| incidents / none / module off | "2 incidents this period; the longest lasted 7 m 30 s (Possible Solr resource exhaustion)." / "No incidents this period." / "Incident correlation is off — turn on the Incidents module to group related alerts." |

The PostgreSQL rule reads the connection usage Sentinel records every `db_interval` (history
rows `postgres.connections`, value = percent of `max_connections`, also in `GET /api/export`).
For a range that ends now and has no such history yet, the current value stands in and the
sentence reads "PostgreSQL connection usage is currently at 78 % of max_connections …"; a past
range without history gets no PostgreSQL recommendation.

## The data behind it

`reports/analytics.compute_report_data(store, cfg, t_from, t_to)` returns one dict; the JSON
format *is* that dict, so it can feed a spreadsheet or another dashboard:

```
meta:                 title, hostname, generated_ts, timezone {name, abbreviation},
                      range {from, to, label, title, span_s}, sentinel_version, sections, demo,
                      modules
summary:              health_score, availability_pct, incidents, alerts_opened, alerts_critical,
                      alerts_warning, worst_component, uptime_s, samples, coverage_pct, mood
server:               cpu {avg, p95, max}, mem {…}, swap {…}, disk {…}, load {…}, net {…},
                      mounts, uptime_s, os, kernel, cores, series {cpu: [[ts, v]], mem: […], …}
services:             [{id, label, emoji, availability_pct, downtime_s, outages, worst_status,
                        avg_latency_ms, max_latency_ms, …}]
latency:              rest {p50, p95, max, series, thresholds}, search {…}, home {…},
                      postgres {…}, solr {…}
alerts:               [Alert…]      alert_stats: opened, critical, warning, still_open, …
incidents:            [Incident…]
repository:           now {…}, delta {items, bitstreams, assetstore_bytes, db_size},
                      growth_per_day {…}, forecasts, days_until_disk_full, series
logs:                 files, totals, trend {first_half, second_half, up}, top_errors
ssl:                  days_left, not_after, subject
postgres_connections: p95, max, samples, source ("history" or "current")
security:             enabled, verdict (off | no_data | findings | clear | unchecked), status,
                      checked_ts, reason,
                      findings [{id, label, status, message, hint, alert, informational,
                                 period_checks, period_warnings}],
                      rungs [the same fields, every current check in ladder order],
                      earlier [{id, label, message, warnings, checks, first_ts, last_ts}],
                      ports [{port, proto, addrs, public, process, pid, expected, verdict, …}],
                      port_counts {listening, reachable, exposed},
                      software [{name, version, until, status, note}]
recommendations:      [{severity, rule, text}]
```

## Formats

- **HTML**: one file that makes no external requests (system fonts, no web fonts); inline CSS
  built from the UI tokens (cream background, one card radius, no shadows) and inline SVG charts,
  strips and meters. Opening it from Sentinel serves it with a Content-Security-Policy that allows
  no scripts and no outgoing requests; a copy mailed to a manager contacts nobody either. It
  prints cleanly: `@page` margins, page breaks between sections, charts that fit A4/Letter. Use
  the browser's *Print → Save as PDF* for a PDF.
- **Markdown**: the same sections as headings and tables; charts are replaced by their summary
  numbers. Pastes into a wiki, a ticket or an email.
- **JSON**: the data dict above.

## Weekly schedule

```yaml
reports:
  weekly: {enabled: true, weekday: 1, hour: 8, send_telegram: true, send_email: true}   # Monday 08:00
  default_sections: [summary, server, services, latency, alerts, repository, security, recommendations]
```

The scheduler checks after every cycle whether the weekly report is due (the weekday and hour
in the zone of `server.timezone`, the server's own when empty; once per week, remembered in the
store so a restart does not produce a duplicate) and generates a 7-day HTML report titled
"Weekly report · <host> · week ending 07 Oct 2026" with the default sections in the background,
one at a time, so the checks never wait for it; the weekly job only runs while `sentinel serve`
is up. A report missed because
Sentinel was down is still produced later the same day, and a failed one is retried after 30
minutes.
With `send_telegram: true` the file goes through every Telegram bot with `sendDocument` and a
two-line caption ("📄 <title>" / "<period> · <from> → <to>", plain text, so a title with `<` or
`&` arrives as typed). With `send_email: true` it goes to
every email recipient as an attachment of one message, subject "[Sentinel] Report · <host> ·
<title>" (reports are capped at 17 MB for email, which fits Gmail's 25 MB once encoded). Both
go to every bot and recipient, whatever their **Critical only** setting. A delivery is recorded
as a `notify` event ("Report sent to Telegram: …", "Report sent by email: …") or, when it did not
happen, including a channel that is off or not set up, as `notify.error` ("Report not sent by
email: <reason>"). In demo mode
nothing is sent: each delivery answers `{ok: true, skipped: "demo"}`, the report panel says
"Report ready · Demo: logged, not sent" and a `notify.demo` event is written instead. Configure
it in **More › Reports**, which offers *Send to Telegram* and *Send by email* only while that
channel can deliver ("📨 To have the weekly report delivered, set up More › Telegram or More ›
Email." otherwise); a switch that is hidden keeps its saved value. Its line under the schedule
names the zone the schedule runs in: "Next report: Monday 08:00 Asia/Colombo (+0530) · in 3
days." ("… (server local time)." from a Sentinel that does not report its zone). The zone is
set in **More › Alert thresholds › Time zone**.

## Tips

- A 30-day report reads from hourly roll-ups (topped up with the newest raw samples), so it is
  fast even on a busy server.
- Custom ranges are useful after an incident: `{"from": <start − 1 h>, "to": <end + 1 h>}`.
- Reports use the thresholds in force when they are generated; if you tune alerts, the hairlines
  in old reports do not change.
- Everything in a report is also in `GET /api/export` if you prefer raw data.
