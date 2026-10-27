# Changelog

All notable changes to DSpace Sentinel are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - Unreleased

First public release. It is not tagged yet: install from `main` until a
`v0.1.0` tag is published.

### Added

- Observe-only health dashboard for DSpace 7 (end-of-life), 8 and 9 servers,
  served by FastAPI on `127.0.0.1:9765` with no build step (plain HTML/CSS/ES
  modules). What differs between the versions and what Sentinel does about it
  is in `docs/COMPATIBILITY.md`.
- Server module (always on): CPU, memory, swap, disk (root plus extra mounts),
  inodes, load, network rate, uptime, OS and kernel via psutil.
- Opt-in modules, all off by default: PostgreSQL, Solr, DSpace Backend, DSpace
  Frontend, nginx, SSL certificate, ports, security, logs, repository
  statistics, assetstore and incident correlation.
- A read-only Security module (🛡️ Security, pre-selected for every kind of
  server in the first-run wizard): which ports other machines can reach and
  whether a rule expects them (and which opened since Sentinel started), the
  host firewall, SSH root and password login, HTTPS and security headers on
  the public address, pending security updates and a required reboot, the end
  of support of DSpace, Solr, PostgreSQL and the operating system, who may
  read `local.cfg` and Sentinel's own configuration, and how Sentinel itself
  listens. Its page lists every open port and the software with the day its
  support ends, and most warnings come with a hint on what to do; it never
  opens or closes a port, changes a firewall or runs sudo. Three findings open
  a warning alert ("New port open to the network", "No host firewall while
  ports are exposed", "Root may log in over SSH with a password"); reports
  gain a Security section, on by default; `doctor` shows a Security row; the
  Ansible role switches the module on.
- Dependency ladders for every service (systemd → TCP → connect → `SELECT 1`,
  and the DSpace chain backend → PostgreSQL/Solr, frontend → backend,
  nginx → frontend) with "not reached" rungs and dependency rungs capped at
  `warn`.
- Alert engine with warning/critical thresholds, anti-flap confirmation and
  clearing counts, escalation, acknowledgements, reminders, quiet hours and a
  daily digest.
- Incident correlation: alerts opened within a five-minute window are grouped
  into one incident with a hypothesis ("Possible Solr resource exhaustion",
  "Database outage cascading to the DSpace backend", …) and the affected
  services.
- Telegram alerts through up to five bots, one per person, each with its own
  token and chat id (More › Telegram, `notifiers.telegram.chats`), with a test
  for each bot that names it and turns Telegram's refusals into advice ("chat
  not found — send the bot a message first").
- Email alerts through Gmail (with an app password) or any other SMTP server
  or relay, to up to five recipients (More › Email, `notifiers.email`,
  `SENTINEL_SMTP_PASSWORD`): one message per event with a plain subject
  ("[Sentinel] CRITICAL · repo-1 · PostgreSQL unreachable"), the same facts as
  the Telegram message, a test email, and errors in plain sentences ("Gmail
  refused the login: use an app password …").
- **All alerts** or **Critical only** for every Telegram bot and email
  recipient; quiet hours and a daily digest for each channel.
- Every time Sentinel sends names its zone ("Started 21:29 +0530", "Started
  15:59 UTC"): alert and incident messages on Telegram and by email, reports
  and their delivery, and the `doctor` box. The new setting `server.timezone`
  (More › Alert thresholds › Time zone; an IANA name such as `Asia/Colombo`,
  empty for the server's own zone) chooses the zone, and quiet hours, the
  daily digests and the weekly report's day and hour follow it. More ›
  Telegram, More › Email and More › Reports name the zone next to their times,
  and `/api/meta` reports it. A name the system does not know is refused when
  saved, with the reason under the field, and in the file it is ignored with a
  notice, never stopping the start. It is the one `server` setting the UI may
  change. The dashboard keeps your browser's time, and log files keep their
  own zones.
- The Security page's open-ports table says why program names are missing
  when Sentinel is not root: "Program names are known only for Sentinel's own
  user; run `sudo ss -ltnp` on the server to see the others."
- A generic JSON webhook notifier.
- Reports in HTML (self-contained, print-to-PDF friendly), Markdown and JSON
  with health score, availability, latency percentiles, repository growth and
  recommendations; optional weekly schedule; delivery to every Telegram bot and
  by email, from the report panel, the weekly job or `report --telegram` /
  `--email`.
- SQLite (WAL) history with hourly roll-ups, 30-day raw retention and 365-day
  roll-up retention; bucketed history, availability strips and latency series
  in the API.
- Server-Sent Events stream (`/api/stream`) driving the live UI, with polling
  fallback.
- Auto-discovery of `local.cfg` / `dspace.cfg` / `modules/assetstore.cfg`,
  listening ports and systemd units (`tomcat9`, `tomcat10`, `tomcat11`, the
  runnable JAR's unit, `pm2-<user>`), feeding the first-run wizard and
  `discover --apply`. It says which DSpace and Solr versions answer, that 7.x
  is end-of-life and when DSpace and Solr do not match, follows Solr's core
  prefix, leaves an S3 assetstore alone and adds `dspace-cli.log` when it
  exists.
- Demo mode (`SENTINEL_DEMO=1` / `./sentinel.sh demo`): a synthetic DSpace 9.4
  stack, as an anonymous client really sees one, with seven days of
  back-filled history and a scripted incident every twenty minutes.
- CLI: `serve`, `demo`, `doctor` (boxed dashboard, `--ascii` and `--json`,
  exit codes 0/1/2; a 7.x backend reads "DSpace 7.6.7 (end-of-life)"),
  `discover`, `report` (`--telegram`, `--email`), `telegram-test` (`--to` one
  bot), `email-test` (`--to` one recipient), `set-password`,
  `hash-password`, `install-service`, `start`, `stop`, `restart`, `status`,
  `logs`, `update`, `uninstall`, `version`.
- `sentinel.sh` one-command installer with optional systemd system or user
  unit; hardened unit file; nginx reverse-proxy example; Ansible role and
  playbook.
- A dashboard login. `./sentinel.sh install` asks for a username and a
  password (at least 12 characters) when it runs on a terminal (`--no-login`
  skips it), and the web UI then opens on a sign-in screen: *Keep me signed
  in for 30 days*, a countdown on the button after too many attempts, Sign out
  at the foot of the sidebar, "Your session ended. Sign in again." when a
  session ends, and More › Account to change the password.
  `./sentinel.sh set-password` creates, changes or removes the login (a
  running Sentinel picks it up within seconds, no restart), and
  `./sentinel.sh hash-password` prints a hash for the Ansible role's new
  `dspace_sentinel_login_user` and `dspace_sentinel_login_password_hash`. The
  password is kept only as a scrypt hash and sessions only as hashes of their
  tokens; a session ends after 12 hours without use or 7 days (30 days with
  *Keep me signed in*) and whenever the password changes; sign-ins slow down
  after five failures from one address and lock a username after twenty.
  The demo never asks for it.
- `server.auth_token`, a token for scripts; secrets masked in every API
  response, config written atomically with mode 0600.
- Desktop-first web UI: left sidebar with Home, Stack/Services, History and
  More, a "＋ New report" button, mood-tinted Pulse band, 12-column tile grid,
  right-side panels, light/dark themes and keyboard shortcuts.
- Test suite (pytest) covering config, store, health, discovery and the API in
  demo mode, plus Playwright end-to-end tests: a smoke test that also produces
  the documentation screenshots, and every state of the backend, frontend and
  Solr detail pages. No test asks a real DSpace, Solr or mail server for
  anything.
- GitHub Actions CI: ruff (lint and format check), pytest on Python 3.10 and
  3.12, shellcheck, ansible-lint (production profile) with a syntax check and
  the role's tests under real Ansible, an install smoke test on Ubuntu 22.04
  and 24.04, and the Playwright smoke test.

### Changed

- `server.auth_token` is a token for scripts: it is also accepted as
  `Authorization: Bearer <token>`, next to HTTP Basic with any user name. With
  a dashboard login set, a request without a session answers 401 `login
  required` without the browser's Basic pop-up; without a login the dashboard
  asks for the token as before.
- A dashboard login also lets Sentinel listen on another address than
  `127.0.0.1`. The install summary and the Security check ("listens on
  0.0.0.0 with a login") then advise putting the nginx HTTPS recipe in front,
  the Security page's Sentinel tile shows *Login* and *Token* apart, and the
  refusal to start on an open address, like the installer's warning, names
  `./sentinel.sh set-password` first.
- The Ansible role refuses, before it changes anything, a non-loopback
  `dspace_sentinel_host` with neither a login, a token nor
  `dspace_sentinel_allow_unauthenticated`, a login user outside the username
  rules, and a password where its hash belongs.
- Exit codes: usage errors exit 64 (was 2) and a command that could not run
  exits 4, so 0/1/2 always mean ok/warnings/critical.
- Secrets can live in the new `config/sentinel.env`, which every command and
  both systemd units read; the Ansible role writes it from
  `dspace_sentinel_env`.
- New settings `server.allowed_hosts`, `server.allow_unauthenticated` and
  `logs.files[].timezone`.
- **Check now** is coalesced: presses within 10 s return the current status,
  and the repository counts only run on their own interval.
- Personas also set the ports they expect; discovery suggests only listening
  ports and running web servers and keeps your other log files.
- In demo mode discovery describes the demo stack, and
  `data/demo/demo-sentinel.yaml` keeps only what the UI changed.
- Events and module toggles use the UI's words ("Solr: Healthy → Warning — …",
  "Solr turned on"), and modules are named like their components.
- `POST /api/reports` answers 507 when the disk is too full for a report.
- Runtime dependencies are capped below their next major version.
- The Ansible role installs `acl` so Ansible can become the service user.
- The Ansible role sets `server.allowed_hosts` and `server.allow_unauthenticated`
  (`dspace_sentinel_allowed_hosts`, `dspace_sentinel_allow_unauthenticated`);
  with its nginx snippet enabled it adds the inventory host name, so the
  repository's name no longer gets HTTP 400 "unknown host".
- The Ansible role configures up to five Telegram bots
  (`dspace_sentinel_telegram_bots`), email (`dspace_sentinel_email`,
  `dspace_sentinel_email_recipients`, the vaulted
  `dspace_sentinel_email_password`) and `reports.weekly.send_email`, and stops
  before changing anything when a variable is wrong (more than five bots, a
  misspelt key, an unquoted time). The old single-bot variables still work, as
  one bot.
- Telegram settings are a list of bots (`notifiers.telegram.chats`). A config
  with the old single `bot_token` / `chat_id` loads as one bot and is saved as
  the list. `SENTINEL_TELEGRAM_TOKEN` + `SENTINEL_TELEGRAM_CHAT_ID` now add
  one more bot, shown read-only as "From environment" (left out, with a
  warning, when the file already has five); the token alone fills in every bot
  saved without one.
- Quiet hours and the daily digest are set per channel (Telegram and email);
  a channel that is switched off no longer records "held back by quiet hours".
  Their times are checked: "7:00 – 9:30" is understood and saved as
  "07:00-09:30", while the UI and the API refuse a value Sentinel cannot read
  ("11pm-7am") and quiet hours that start and end at the same time, which
  would never be quiet. In a hand-edited file such a value is left out with a
  warning in the log and a line in the events log at every start, instead of
  being silently ignored; it never stops Sentinel from starting. Sentinel
  writes both times in quotes, and an unquoted `daily_digest: 18:00`, which
  YAML reads as a number, is read as 18:00.
- A dependency that answers correctly but too slowly reads "Solr is slow
  (Critical): search took 4.3 s" in the ladders that depend on it, instead of
  "is down (Critical)".
- The backend's actuator is graded the way DSpace defines its statuses:
  `UP_WITH_ISSUES` is healthy ("up, with non-blocking issues"), an anonymous
  `DOWN` reads "status DOWN (details need a DSpace admin login)", and the
  actuator is asked at most every five minutes, because each call makes
  DSpace run all its health checks. Only a healthy answer is reused, and never
  across a restart or an upgrade, so one bad answer counts once.
- The frontend check asks for the homepage as the public host
  (`dspace.public_url`) and tells a page rendered on the server from the
  client-side shell, naming the 9.3+ Host rule only when the request carried
  another host than DSpace's `ui.baseUrl`; a page counts as DSpace only with
  the UI's `<ds-app>` element or DSpace's Generator tag, not the bare word.
- Solr's optional DSpace cores (`qaevent`, `suggestion`, `audit`) get a rung
  when Solr has them, a core that failed to load says why, and a core whose
  reload failed, which Solr keeps serving, is a warning that is still pinged
  and searched.
- More › Settings lists Email next to Telegram with how many bots and
  recipients are set up; the report panel and More › Reports offer "Send by
  email"; the events' Kind column names the channel.
- Configuration errors no longer start with "Value error, ".
- Component pages show a check's advice as a muted line under its message in
  the ladder: that there is no web server on this machine and how to switch
  the nginx check off; why a local-only port that other machines can reach
  matters and how to close or accept it (Sentinel sees where a port listens,
  not the firewall); which owner, group and mode keep Sentinel from reading a
  log and which group to join, or, when the group was added but Sentinel still
  runs without it, to log out and in again before restarting; and a Solr
  core's fix.
- Ack calms Home: an acknowledged alert no longer colours the mood or the
  headline, which reads "All systems healthy · 2 acknowledged" once nothing
  unacknowledged is left, and the band, the colour and the sidebar follow at
  once. The alert stays listed with its Acked pill and "Reminders are off. It
  stays listed until the cause is fixed.", and the component keeps its real
  state on its tile, its page, the Stack and History. An escalation to
  critical clears the acknowledgement, so the alert colours Home and reminds
  again. `doctor` follows: it exits 0 when everything open is acknowledged and
  still lists those alerts with "acked".
- A saved bot token, email password or PostgreSQL password says why it shows
  as dots: "Saved. Hidden for safety — type a new one to replace it."
- More › DSpace connection's nginx section says that its URL is the web server
  in front of DSpace, not the UI's own :4000, and where to switch the check off
  when there is no web server.
- Discovery leaves the service name empty for Solr, the DSpace UI or a web
  server that answers without a systemd unit, so a server set up by hand gets
  no "unit … not found" rungs, and says so under *What we found* ("Solr answers
  on 8983 but no systemd unit was found for it: it may not start by itself
  after a reboot").

### Fixed

- History › Availability no longer shows a healthy component as "unknown" between two
  checks. A component that is checked less often than the strip's cells are wide (Repository:
  every 15 minutes, against one-minute cells in the one-hour view) showed a few green cells
  and the rest grey; each check now stands until the next one, and grey is left for the time
  nothing was checked.
- The dashboard no longer looks healthy when Sentinel is unreachable or its
  checks are overdue.
- A hung NFS or CIFS mount no longer freezes the dashboard; the assetstore
  reports "filesystem not responding".
- Slow Telegram or webhook deliveries and the weekly report no longer delay
  the checks.
- A check that timed out no longer opens, resolves or closes alerts.
- Slow but working services get time to answer instead of being cut off.
- A slow REST API no longer marks the rungs below it "not reached".
- The next-check countdown no longer sticks at 0 after a settings change.
- A module switched off during a check stays off.
- Availability counts downtime from the time between checks, so gaps in the
  history are not outages.
- Charts and reports over seven days reach the newest sample, and roll-ups no
  longer lose data at the retention boundary.
- The report's PostgreSQL connection advice is based on recorded history.
- Log files in another time zone are read correctly; Solr's UTC logs are
  detected, and future-dated lines are ignored.
- The daily digest lists the oldest open alerts first.
- `sentinel.sh` works from any directory and through a symlink.
- `telegram-test` no longer prints "@@" before the bot name.
- `start` refuses a port that another Sentinel or program holds, and `status`
  no longer shows another instance's health as its own.
- Service commands only manage the systemd unit of their own checkout.
- `install-service` stops a leftover background process, applies a new port
  to a running unit and checks that the unit comes up.
- Unit files work with checkout paths that contain spaces, and a unit that
  keeps failing stops after five attempts.
- The installer picks a Python that has `venv` and `ensurepip`, and stops on
  a broken config.
- `stop` and `restart` no longer wait for an open dashboard.
- Exports include every row and keep memory flat.
- `report --telegram` says whether the report was sent.
- `sentinel.sh help` names the right demo settings file.
- `doctor` keeps its box intact around multi-line alert messages.
- Focus lands on a real control after Ack, Delete and Undo.
- The snackbar's Undo and Open buttons are readable in dark mode.
- Numbers stay next to their units, and chart axes use round steps, one unit
  and dated multi-day labels.
- Modules that are off leave no trace in History, report sections or
  Services.
- A warning on Home names its subject ("1 warning · Memory 83 %").
- Keyboard shortcuts do nothing while the setup wizard is open.
- Settings pages use the desktop width and keep Save in reach.
- Discovery's suggested ports and log files are readable, and a PostgreSQL
  password error shows under the password field.
- A healthy DSpace no longer shows a permanent "DSpace Backend degraded":
  neither for `UP_WITH_ISSUES` (common without a GeoLite database) nor for the
  `DOWN` that DSpace 7.6.4–7.6.x, 8.2–8.4 and 9.0–9.3 report for their SEO
  health check (anonymously, or with only the `seo` component down), which
  now reads as a known issue.
- On DSpace 9.3 and later, Sentinel's homepage check no longer gets the
  client-side shell and makes the UI log an SSR error every 30 seconds.
- Errors in the `catalina.out` of packaged Ubuntu and Debian Tomcat are
  counted, a stack trace once; they used to read as zero.
- DSpace 7–10's daily log files (`dspace.log-YYYY-MM-DD`) are followed; only
  DSpace 6's names were understood before.
- Discovery suggests the Tomcat log of the backend's own unit
  (`/var/log/tomcat10/catalina.out`), none for the runnable JAR, and no longer
  picks a lone `dspace-ui` unit as the backend.
- The Ansible role no longer writes inline `!vault` secrets into
  `sentinel.yaml` as ciphertext on ansible-core older than 2.19.
- A snackbar that was dismissed no longer freezes a newer one on screen.
- A mail server that does not answer no longer delays Telegram, and an
  unreachable Telegram no longer delays email: each channel delivers on its
  own, in order.
- A report that could not be delivered is recorded as an error
  (`notify.error`); report attachments are capped at 17 MB, counted like every
  other size Sentinel shows, so they fit Gmail, and a larger report is named
  with its size ("… is too large to email (17.1 MB; the limit is 17 MB)"); a
  report title with `<` or `&` reaches Telegram.
- HTML and Markdown reports sent by email are marked as UTF-8, so their `·`,
  `→` and emoji arrive intact in mail clients that show the attachment inline.
- A large report that cannot reach the mail server within 20 s over a slow
  uplink reads "<host>:<port> did not answer within 20 s" instead of blaming the
  port and security.
- An email given up on while its TLS handshake was still running (its time
  limit, or a shutdown) is no longer delivered afterwards.
- An SMTP login that is not an email address (SendGrid, Amazon SES) asks for a
  From address instead of failing every email, and Gmail always sends from its
  own account, even after a From address was typed for another server. A
  settings file that already holds such a login no longer stops Sentinel from
  starting: email is not ready, and the log and the events log say why.
- Quiet hours or a digest time in the settings file that Sentinel cannot read
  no longer stop it from starting after an update (and `restart` no longer
  stops the running Sentinel and then fails): the value is left out and
  named.
- The Actuator health tile says "asked again at the next check" under a
  warning answer; "asked at most every 5 minutes" is only for the answers
  Sentinel reuses.
- Key/value lists (Rendering, Connection used, Host, About, a report's facts)
  sit right under their tile's title, like the other lists.
- Five Telegram bots in the file plus the one from the environment no longer
  stop Sentinel from starting.
- The More › Reports row names only the channels that really take the weekly
  report.
- The first-run wizard switches nginx and SSL on only when discovery found a
  web server and HTTPS. Otherwise their rows stay off as "not found on this
  machine", also after *Skip for now*, so a DSpace opened directly on :4000 no
  longer starts with a false "nginx unreachable" and an SSL check without a
  host.
- Discovery suggests the nginx and Solr logs only where they are, and finds the
  log of a Solr started by hand from its `-Dsolr.log.dir`.
- A log behind a folder Sentinel may not enter no longer stops the whole Logs
  check: it reads "permission denied" and names that folder.
- The Logs check skips the nginx log while the nginx module is off.
- A dependency rung takes its reason from the rung that decides its state:
  "DSpace Backend is down (Critical): HTTP 500", not the message of a slow
  REST API above the failed search.
- A public URL with a non-ASCII host name (`https://bücher.example`) no longer
  makes the homepage check fail with a UnicodeEncodeError.
- The Telegram test and failed deliveries say what happened and what to check
  instead of "timeout": "api.telegram.org did not answer within 10 s: this
  server may not be able to reach Telegram (a firewall, or a proxy it has to
  use: set HTTPS_PROXY in config/sentinel.env)", with matching sentences for a
  refused connection, a failed DNS lookup and TLS errors, naming the proxy
  when `HTTPS_PROXY` is set.
- Gmail's login refusal names the account and Gmail's own answer ("Gmail
  refused the login for … (535 5.7.8 Username and Password not accepted): use
  an app password made in that same Google account …"), and right after a
  refused login "smtp.gmail.com:587 closed the connection" explains that Gmail
  drops new sessions for a few minutes instead of blaming the port and
  security.
- The email test line keeps the mail server's name: "smtp.gmail.com:587 closed
  the connection: …" used to read "587 closed the connection: …".
- An email password (or SMTP username) with a character outside ASCII is
  refused when it is saved. One in the settings file or in
  `SENTINEL_SMTP_PASSWORD` leaves email not ready with that sentence, and
  Sentinel still starts. The mask `••••••`, pasted into the file as the
  password, means no password instead of being sent to the mail server.
- The shutdown warning counts what is given up, "5 notifications not
  delivered (Sentinel is stopping)", instead of "(0 waiting)".
- Home names a check that could not run and why ("1 check could not run ·
  Logs", "No open alerts — Logs cannot be checked: permission denied
  (/dspace/log/dspace.log)."), with a button to its page when it is the only
  one; "could not be checked this time" is left for a check that failed once.
- More's rows show what a settings page just saved, without a reload.
- A password set in `config/sentinel.env` stays in its locked field: a
  keystroke no longer empties it, and the field looks locked.
- A Gmail account saved with another port and security (465 with SSL) keeps
  them, and its password, when More › Email is opened.
- More › Telegram and More › Email refuse quiet hours that start and end at the
  same time before saving, with the API's sentence.
- The Ports page's Listening table names the addresses each port listens on
  ("0.0.0.0, ::", "127.0.0.1") instead of "—".
- The Public URL tile says "not set" only when no public URL is configured; a
  configured one that has not been checked yet reads "not checked yet".
- The Solr page no longer counts or colours the failed reload of a core
  Sentinel does not monitor, and no longer says "Still serving" for a core
  whose ping fails.
- A port on an IPv4-mapped loopback address (`::ffff:127.0.0.1`, as Java
  shows Solr's stop port) counts as local, no longer as reachable from the
  network, on the Security and Ports pages.
- Ports that other machines reach only through Tailscale (its `100.64.0.0/10`
  and `fd7a:115c:a1e0::/48` addresses) read *Tailscale*, "reachable through
  Tailscale only", instead of unexpected, and Tailscale's own WireGuard port
  (UDP 41641) is expected while Tailscale runs; neither warns or alerts.
- The Firewall check counts the same ports the Open ports check calls
  unexpected, so the two numbers agree; a local-only port that listens
  publicly is named apart ("… and 1 local-only port is public").
- The saved PostgreSQL password in More › DSpace connection behaves as one
  unit: the first keystroke replaces the whole mask instead of editing the
  dots.

### Security

- The API answers only to known host names, which blocks DNS rebinding;
  other names go into `server.allowed_hosts`.
- Cross-site writes are refused (403), and write requests must send JSON
  (415).
- Sentinel refuses to listen on a non-loopback address without a dashboard
  login or an auth token unless `server.allow_unauthenticated` or
  `SENTINEL_ALLOW_OPEN=1` is set.
- Only the exact `/healthz` path skips every check. With a login set, the
  page, its static files, `GET /api/session` and `POST /api/login` load
  without a session, and everything else needs one (or a token).
- The dashboard password is never stored, logged or shown: `server.login`
  holds a scrypt hash (masked in the API, never in the export), the database
  holds only SHA-256 hashes of session tokens, the session cookie is
  `HttpOnly`, `SameSite=Strict` and `Secure` over HTTPS, an unknown username
  takes as long as a wrong password and gets the same answer, and
  `PATCH /api/config` cannot change the login.
- `server.*` settings can no longer be changed through the API, except
  `server.timezone`, which decides nothing about who can reach Sentinel.
- Saving settings no longer writes values from `SENTINEL_*` variables, such
  as tokens and passwords, into `config/sentinel.yaml`.
- Discovery no longer returns the database password from `local.cfg`.
- A saved PostgreSQL password is only sent to the server it was saved for.
- The webhook URL is treated as a secret; the webhook always verifies TLS and
  never follows redirects.
- Every Telegram bot token is redacted from error texts in any case and when
  URL-encoded, and a masked token sent back keeps only the token of the same
  bot, so it can never be moved to another chat.
- The email password is a secret like the others and never appears in an
  error or a log line; mail goes over verified TLS, `security: none` never
  sends a password to another host, and addresses, sender names and subjects
  cannot add mail headers. A password with a character SMTP cannot send is
  named in one plain sentence before any connection, no longer in Python's
  own words, which showed that character and where it stands, and any other
  unexpected mail error names only its kind.
- A very long quiet-hours value sent to the API is refused at once instead of
  keeping the server busy.
- The saved email password is only sent to the SMTP server and login it was
  saved for: changing them needs the password again, as for PostgreSQL. While
  `SENTINEL_SMTP_PASSWORD` is set, More › Email shows the password read-only
  and the API refuses to change it or where it goes.
- systemd unit names from the config are validated before `systemctl` sees
  them.
- HTML reports make no external requests and are served with a
  Content-Security-Policy.
- Unexpected errors return a short message; the details go to the log only.
- Report ranges are bounded, one report or export runs at a time, and stored
  reports are capped at 200 files / 500 MB.
- The SQLite database and its WAL files are created with mode 0600.
- The installer refuses to run under `sudo`, and `install --service` will not
  install a unit that would expose Sentinel without a login or a token.
- A Telegram error that names the proxy from `HTTPS_PROXY` shows only its host
  and port, never the user or password in its URL, and Gmail's answer to a
  refused login is shown only after the password has been taken out of it.
