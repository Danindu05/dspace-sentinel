# Design principles for the web UI

This page is for people changing `dspace_sentinel/static/`. It explains *why* the UI looks and
behaves the way it does, so new screens feel like they were always there. The normative details
(tokens, layout numbers, DOM contract, screens) are in [SPEC.md section 12](SPEC.md#12-web-ui);
this is the reasoning.

## Who it is for

A DSpace sysadmin or repository manager with the dashboard open in a browser tab on a work
machine, checked a few times a day, often beside a terminal. They want to know in one glance
whether anything is wrong, and if so *where in the chain*. They are not looking at it for fun,
so it must be calm, and it must not look like Grafana.

## Desktop-first

This is a web dashboard for laptops and monitors, not a mobile app. Design, build and verify at
**1280, 1440 and 1920 px** first. Below 1200 px the sidebar collapses to an icon rail; below
900 px it becomes a bottom bar and panels become bottom sheets. That narrow fallback must
*work*, but it is not the design target, and nothing should be compromised at 1440 to make it
nicer.

## The ten principles

### 1. Four tabs and a centre plus

The sidebar has exactly four destinations — 🏠 **Home**, 📚 **Stack** (or ⚙️ **Services**),
📈 **History**, ⚙️ **More** — and one filled call to action between Stack and History:
**＋ New report**. Everything else is reached from those, and depth is capped at two: a tab,
then either a pushed sub-view (`‹ Stack · PostgreSQL`) or a right-side panel. Nothing deeper.
If a feature needs a third level, it belongs in a panel or on a tile.

Keyboard: `1`–`4` switch tabs, `n` opens New report, `r` runs Check now, `Esc` closes,
`?` lists the shortcuts. Never captured while typing, and none of them act while the onboarding
wizard, the sign-in screen (or the boot error) is on screen.

**The sign-in screen** (only while a dashboard login is set, never in the demo) fills the same
gate as the onboarding wizard and replaces everything else: no sidebar, no stream, no snackbars,
no shortcuts. It is one centred column at most 440 px wide in the product's own look, not a
browser pop-up: 🛰️, "Sign in to DSpace Sentinel" in the 28 px heading face, then one tile-like
form (surface fill, 1 px line, card radius) with *Username*, *Password*, the checkbox "Keep me
signed in for 30 days" and one filled full-width *Sign in* button. The server's sentence goes under
the button ("Wrong username or password."); after too many tries the button itself counts down
("Try again in 16 s") instead of a separate message. When a session ends while the app is open,
the same screen comes back with a note on a `--surface-2` band above the form, "Your session ended. Sign in again.",
and the username filled in, and signing in returns to the page that was open. While signed in, the
sidebar foot shows "👤 admin" and a ghost *Sign out* under the theme switch; the icon rail and the
phone bar have no room for it, so More › Account carries a second *Sign out*.

### 2. Modules off by default, and off means gone

Only the server module is always on. A module that is off leaves **no trace**: no tile, no
ladder, no chart, no menu row, no empty state saying "turn me on" except the one sentence in
More. This keeps a bare server install to a Pulse band, four vitals, the Alerts tile and a
footer, and it is why the Stack tab renames itself to Services when nothing DSpace-specific is
enabled.

### 3. One token file

Every raw value — colours, spacing steps, radii, type sizes, font stacks, motion durations —
lives in `css/tokens.css` and nowhere else. `app.css`, `index.html` and the screens consume
tokens. If you need a value that does not exist, add a token; do not write a hex code in a
component. `tests/test_static_tokens.py` enforces this: a raw colour, duration, radius or type
size outside `tokens.css` fails, as do gaps that are not spacing tokens, 4–15 px paddings or
margins, and any `var(--x)` that `tokens.css` does not define (relative `0.95em` monospace and
1–3 px optical nudges are allowed).

The six spacing steps (`--s1`…`--s6`; tiles sit `--s3` = 16 px apart), the card radius
(`--r-card` 22 px), the panel radius (`--r-panel` 28 px), the pill, and two small radii for
things inside a tile — `--r-inner` 12 px (emoji squares, the chart plot, tooltips, the report
preview) and `--r-mark` 6 px (kbd keys, availability cells, the text-link focus) — are the whole
geometric vocabulary. Type sizes are the `--fs-*` scale (`--fs-11` only for the bottom-bar
labels, `--fs-15` for navigation, buttons and labels, `--fs-20` for tile values and emoji).
Text and lines on the accent use `--on-accent` and its tints (`--on-accent-2` for the Pulse
subline, `--on-accent-line`, `--on-accent-wash`, `--on-accent-inset`); `--scrim` darkens what a
dialog covers, `--paper` frames the light-only report preview, and `--dur-panel` (200 ms, 0 under
reduced motion) times the panel. Zero `box-shadow`, no gradients, 1 px `--line` borders on a
cream background; dark mode flips the neutrals through `light-dark()` and the pastels stay
fixed.

### 4. The mood accent

The whole app is tinted by current health: **mint** when everything is ok, **butter** on
warnings, **coral** on a critical, **sky** before the first check and whenever the data is out
of date. `app.js` sets `--accent` from the SSE `status.mood`; the Pulse band, the "+" button, the
active tab pill, toggles and chips pick it up. Because pastels are fixed in both themes, text on
an accent is always dark ink (`--on-accent`). This is the one signature you notice from across
the room; do not add a second one. A warning names its subject wherever there is room: the
headline reads "1 warning · Memory 83 %" or "3 warnings · Memory 83 %, Solr +1 more", not just a
count, and a check that could not run does too ("1 check could not run · Logs", with the reason
in the Alerts card). Acknowledging calms Home: an acknowledged alert no longer colours the mood
or the headline, which reads "All systems healthy · 2 acknowledged" once nothing unacknowledged
is left. The component keeps its own status on its tile, its page, the Stack and History, and a
new alert, or an acknowledged one that escalates, colours Home again. A warning headline never
names a component whose open alerts are all acknowledged. The server decides all of this; right
after an Ack or its undo Home asks for `/api/status` again (`ctx.refreshStatus()`), so the band,
the accent and the sidebar follow at once. The sidebar's line stays short ("All healthy · 8 s
ago") and its tooltip carries the whole headline.

**Out-of-date data never looks healthy.** `store.stale` is `offline` when Sentinel does not
answer (a failed **Check now** sets it at once) and `overdue` when no check has finished for
max(45 s, 3 × `collector.system_interval`). While it is set the accent turns sky, the Pulse reads
"Sentinel is unreachable — last check 11:34" or "Checks are overdue — last check 11:34" with the
subline "<host> · showing the last known state", `html[data-stale]` mutes pills, dots, rungs,
meters and sparklines and greys the ✅ marks on Home, Stack, the component pages and the server
page, counts read "Last known: 10 of 10 healthy" (the Stack header "Sentinel unreachable · last
known: …"), the Alerts card says "🕓 No open alerts at the last check — new ones show here once
Sentinel answers again.", and the sidebar's mood line shows ⚠️ "Sentinel unreachable · last
check 11:34" (a ⚠️ alone in the 1100 px rail, with the text kept for screen readers and as a
tooltip). The next fresh status clears all of it.

### 5. Ladders

A service is never a single dot. It is its dependency chain — systemd → TCP → connect →
`SELECT 1` — drawn as vertical rungs joined by a 1 px rail, each with ✅ / ⚠️ / ❌ / — and a
timing. The first failing rung is the reason; rungs below it read "not reached" in muted ink
(a rung that only crossed its latency threshold is marked slow and does not stop the ones below
it). Dependency rungs ("Solr dependency ⚠️ Solr is down (Critical): connection refused") explain
cascades; a dependency that answered correctly, only too slowly, reads "Solr is slow (Critical):
search took 4.3 s", never "down".
A rung that is unknown only because something is not available on this host (no actuator, no
systemd unit, no log file) is *informational*: it stays in the ladder, muted, as "— not
available", and never becomes the reason or greys the component. A rung that is informational
because of a known upstream bug (DSpace's SEO `DOWN`, `detail.known_issue`) reads "known issue ·
…" in the same muted style, and screen readers hear "Known issue". HTTP rungs without a message
show their status code ("HTTP 200"). On a component's page a rung can also carry a hint: its
collector's advice in one or two plain sentences, in muted ink under its message whatever the
rung's status ("The file belongs to tomcat:tomcat (mode 640): …, then restart Sentinel."). It
starts with a word, never a login or unit name, because the ladder capitalises its first letter.
A hint that starts with a command (`sudo`, `chmod`, `chown`, `chgrp`, `systemctl`, `apt`, `ufw`)
keeps it as it is, and a command between backticks reads in the monospace face, without the
backticks ("Switch a host firewall on: for example `sudo ufw allow OpenSSH`, then `sudo ufw
enable`; …"). The Security rungs word their informational states themselves ("not available ·
sshd_config is not readable by Sentinel's user", "no SSH server found", "no public address
set") and read as they are.
The Stack tab's ladders stay short and leave it out. When you add a check, add it as a rung with
a short label and a short message; do not invent a new visual.

### 6. One tile recipe

Every card on every tab is `ui.tile`: surface fill, 1 px border, radius 22, padding 16, an emoji
in a 40 px `--surface-2` square on the left, a bold title with a small subtitle, and on the right
a value (Space Grotesk), a status pill or a chevron. Stat tiles are the same tile with a big
number and a 12-point sparkline. Tappable tiles get a pointer, `--surface-2` on hover, an 8 %
ink tint on press and a 2 px focus ring. Because Home, Stack, History and More all use it, a new
screen looks finished by default. Lists that are naturally tabular (alerts, events, ports, Solr
cores, mounts) become tables *inside* a tile, not stacks of cards; at phone width a wide table
scrolls inside its tile rather than the page. Whatever a block tile holds under its title row (a
key/value list, a meter list, a chip list, a table) starts and ends at the same distance from it;
a key/value list's `<dl>` has no margin of its own.

### 7. One panel; settings are pages under More

**＋ New report** is the one right-side panel: a `<dialog>` 520 px wide with radius 28 on its
left corners, full height, close button top-right, `Esc` to close, covered content `inert`,
`?panel=report` in the URL so Back closes it. The settings forms — Alert thresholds, Telegram,
Email, DSpace connection, Appearance, Reports, About, and Account while a dashboard login is set —
are sub-views under More (`#/more/<page>`, "‹ More · Telegram" header) because they are longer
than a drawer and people return to them. Account follows the same form rules: a "Signed in as
admin" tile with *Sign out*, then *Change password* (Current password, New password, Repeat new
password); the page checks the length and the repeat under the right field before it posts, and
success is the snackbar "Password changed. Other sessions were signed out."
Both follow the same form rules: each field group is a single column, biggest field first,
visible labels, helper text, inline errors, one filled button, Enter submits. Settings sub-views
use the desktop width: at most 960 px, single inputs stop at half of it (472 px) so they line up
with pairs, and the form's action row sticks to the bottom of the window (it stays in the flow
below 900 px). Alert thresholds lays its rule cards and the Behaviour fields in columns (three at
1280–1920 px) with the group titles across the row. Its last group, *Times in messages*, holds
the **Time zone** (`server.timezone`). It sits there rather than in Appearance because it changes
what Sentinel sends and when it stays quiet, never how the dashboard shows times: Home, History
and the pages keep the browser's zone and say nothing about it, while the quiet-hours and digest
help of Telegram and Email and the next-run line of Reports name the zone ("in Asia/Colombo
(+0530)"). Errors land where they belong: re-pointing
PostgreSQL without the password shows "Enter the password again — the saved one is only sent to
the server it was saved for, …" under the password field, which is focused. Confirm dialogs
exist for exactly two actions: Clear history and Reset Sentinel. The main job of the app — click
**＋ New report**, click **Generate report** — is two clicks because the range and format are
preselected. The report panel and More › Reports offer only the sections whose module is on, and
only the delivery switches (*Send to Telegram*, *Send by email*) whose channel can deliver.

Lists of people are cards, not a table: More › Telegram's bots and More › Email's recipients
share `screens/more/destinations.js`. Each card is a `role="group"` named "Bot 1" / "Recipient 1"
with its fields side by side and visible labels, the *All alerts* / *Critical only* chips, a
*Test* button and ✕; "＋ Add a bot" / "＋ Add recipient" stops at five with a caption that says
so. A test saves the form first and writes one result line into its card ("✅ Danindu ·
@danindu_sentinel_bot · delivered", "❌ Team · … · chat not found — add the bot to the group
first"), with a one-line summary under the list; in the demo it reads "Demo: logged, not sent". A
read-only entry (the Telegram bot from `config/sentinel.env`) is a card without inputs that says
where it is set. A saved bot token, email password or PostgreSQL password (More › DSpace
connection) shows as `••••••`, never the value, and behaves as one unit: the first keystroke,
paste or delete replaces the whole mask. While such a field shows the mask a muted line under it
says "Saved. Hidden for safety — type a new one to replace it.", so the dots never read as "it
did not save"; a placeholder only ever describes an empty field (the PostgreSQL password's
"Empty uses peer or .pgpass authentication"). A field the page cannot change (a password from
`SENTINEL_SMTP_PASSWORD`) looks locked (the `--surface-2` fill, muted ink, a not-allowed cursor),
ignores keystrokes and has no such line. Server errors land under the card and field they
concern, without the settings path the API puts in front, and a result line keeps a mail
server's name as it is ("smtp.gmail.com:587 closed the connection: …"). Settings group titles
are `h2`, and on screens 901 px and wider the snackbar floats just above the sticky action row.

### 8. Forgiving feedback

Ack, delete report, toggle module, remove a bot or a recipient: do it immediately and offer
**Undo** in a snackbar for six seconds (`aria-live="polite"`, never steals focus; its action is
an accent pill with dark ink). Do not ask "are you sure?" for reversible things. Focus always
lands on a real control (`ui.replaceKeepingFocus`): after Ack it moves to the alert's focusable
"Acked" pill, after Delete to the next report's Delete (the last one → "＋ New report"), and Undo
returns it to the restored item; programmatic focus on a page or section heading shows no ring,
every other focused element does. An action keeps its name through the flow: "Generate report" →
"Generating…" → "Report ready". Empty states are one emoji and one sentence that names the next
click: "📄 No reports yet — click ＋ New report to generate your first one." With no module on,
the Services tab reads "💻 Only the server is watched — its vitals are on Home." and shows no
"0 of 0 healthy".

### 9. Minimal motion

Panel slide-in 200 ms ease-out, sub-view crossfade 160 ms, snackbar fade. No chart entrance
animation, no loops, no ripple, no skeleton flash: a chart that is refetching holds its previous
render at 50 % opacity. `prefers-reduced-motion` sets `--dur: 0ms` and everything still works.
Motion exists to show where something came from, not to decorate.

### 10. Plain words, real numbers

Sentence case, plain verbs, no exclamation marks, no jargon the operator does not already use.
Numbers through `Intl.NumberFormat`; disk, mount, assetstore, memory, database and bitstream
totals in GB with one decimal ("1,024.0 GB"), while single files (reports, Solr cores,
Sentinel's own database) and rates keep adaptive units (KB, MB); latency in ms under a second and
s above; relative times as "8 s ago"; a number is glued to its unit with a no-break space so the
two never wrap apart. Texts from the API are shown in the UI's words: events read "Solr: Healthy
→ Warning — search took 1.7 s" and "Solr turned on", DSpace's `UP_WITH_ISSUES` reads "Up, with
non-blocking issues", an actuator answer says how old it is and when it is asked again
("checked 3 m ago · asked at most every 5 minutes", or under a warning "checked 6 s ago · asked
again at the next check"), a Solr core that failed to load is a "Failed" pill with the reason and
the fix under the table, and the Recent events *Kind* column says
Alert, Status change, Incident, Setting, Report, Telegram, Email, Webhook, "Telegram and email
(demo)", a channel with "(demo)", "(failed)" or "(held back)" after it ("Email (failed)"), Demo or
Error. Status is never colour-alone: an emoji or a label is always
next to it. Body text 16 px, click targets at least 40 px, focus visible everywhere, `<nav>`
links carry `aria-current="page"`, charts have `role="img"`, an `aria-label` summary and a *Show
as table* twin; the breadcrumb reads as "Stack : PostgreSQL" to a screen reader.

## Layout at a glance

```
┌ sidebar 248 ┬──────────────────── content, max 1440, padding 32, 12 columns, gap 16 ─────────────────────┐
│ 🛰️ DSpace   │  Home                                                                Next check in 7 s │
│  Sentinel   │ ┌─ Pulse band (12) ─────────────────────────────────────────────────────────────────────┐ │
│  repo-1     │ │ All systems healthy · repo-1 · checked 8 s ago · Demo data              ↻ Check now  │ │
│             │ └───────────────────────────────────────────────────────────────────────────────────────┘ │
│ 🏠 Home     │ ┌ 💻 CPU (3) ┐ ┌ 🧠 Memory (3) ┐ ┌ 💾 Disk (3) ┐ ┌ ⚖️ Load (3) ┐                            │
│ 📚 Stack    │ └────────────┘ └───────────────┘ └─────────────┘ └─────────────┘                            │
│ ＋ New      │ ┌─ DSpace stack (6) ─────────────────┐ ┌─ Alerts (6) ─────────────────────────────────┐   │
│   report    │ │ 🐘 PostgreSQL   Healthy · 1 ms  ● › │ │ ✅ No open alerts — checks run every 15 s      │   │
│ 📈 History  │ │ ☀️ Solr         Healthy · 30 ms ● › │ └──────────────────────────────────────────────┘   │
│ ⚙️ More     │ │ ☕ Backend      Healthy · 117 ms ● ›│ ┌─ Repository (6) ─────────────────────────────┐   │
│             │ └─────────────────────────────────────┘ │ 📚 Items 27 576  🧾 Bitstreams 81 584  …      │   │
│ ● Healthy   │                                         └──────────────────────────────────────────────┘   │
│             │  Ubuntu 24.04 · kernel 6.8 · up 17 days · Sentinel v0.1.0                                  │
│ ◐ theme     │                                                                                            │
└─────────────┴────────────────────────────────────────────────────────────────────────────────────────────┘
```

Stat tiles span 3 columns, list/ladder tiles 6, charts 6 (or 12 for network), the Pulse band 12.
A half-row whose partner is hidden (a module that is off) spans the full 12 columns instead of
leaving a gap. The Home header shows only the countdown ("Next check in 7 s", or "Checking…"),
because the age is already in the Pulse subline; for the same reason the sidebar's ↻ Check now is
hidden while Home is showing and the Pulse band carries the one Check now. A component page's
header shows only "Checked 15 s ago" and its ladder head the pill "Healthy · since 03:34 PM"; an
incident page leaves its header empty (status and duration are in the hero), and History's
incident tiles read "Sep 25, 11:05 AM · ongoing". Report tiles are titled by the operator's title,
else "Last 24 hours" or, for a custom range, the covered days ("Sep 20 – 24"), with "… ·
generated Sep 25, 01:42 PM · 5.7 KB" underneath. The report panel slides in over the content from
the right; the sidebar never scrolls away and tabs keep their scroll position. Between 900 and
1200 px the sidebar is a 76 px icon rail; below 900 px it is a bottom bar and the panel a bottom
sheet.

A component page puts its ladder (6) beside "Right now" (6). The Security page puts its ladder
above its tiles, both across the page (12): its open-ports and software tables keep every column,
and its hints are long. Its tiles use the same recipe, tokens and status pills as every other
page: the verdicts *Expected*, *Local* and *Tailscale* and the support states *Ends soon* and
*Not known* are plain pills, *Exposed*, *New*, *Ended* and *Affected* warnings; no new colour. A
port's note sits beside its verdict ("reachable through Tailscale only", "Tailscale's own port
(WireGuard)", "Sentinel") and drops under the pill when the column is narrow; the check's own
note goes under the table ("Port notes"), its command in the monospace face.

## Charts

Follow the dataviz conventions already encoded in `charts.js`: one y-axis, hairline grid in
`--chart-grid`, 2 px lines with round joins, a 10 % area wash for a single series, threshold
hairlines in `--warn-fill` / `--crit-fill` with a 12 px label in `--warn-ink` / `--crit-ink` at
the left of the plot ("Warning 80 %", "Critical 92 %"; a surface halo keeps it readable when data
runs through it), a crosshair with one tooltip listing every series, ←/→ to move it, a legend
from two series up, `update()` that redraws in place, and a `<table>` twin behind *Show as
table*. Every axis labels its ticks in one unit with step-exact decimals: byte axes pick one
1024-based unit from the top of the domain and step in round numbers of it (0 · 0.5 · 1 · 1.5 ·
2 MB/s), and a flat zero series gets a 0–1 axis. Time ticks keep at least 90 px apart; spans over
two days step in whole local days from midnight and read day and month ("Sep 3"), and the labels
at both ends anchor inward. Availability strips merge neighbouring buckets on narrow rows rather
than drawing 1 px cells. The three series colours (`--chart-1..3`) were validated with the
dataviz palette validator on both surfaces (see `docs/screenshots/contrast.txt`); do not add a
fourth without re-running it.

## Adding a screen or tile

1. Start from `ui.tile` / `ui.section` / `ui.panel`; if you cannot express it with them,
   question the design before adding CSS. A new module file also goes into the import map in
   `index.html`, so it is served with the `?v=` cache-buster like the others.
2. Consume tokens only. Verify at 1440 px light and dark, then at 1100 px (icon rail) and 390 px.
3. Read from `store.js` and re-render on change with minimal DOM patching; all text through
   `textContent`.
4. Add the empty state sentence, the hover state, the focus ring and the keyboard path.
5. Keep the DOM contract the end-to-end tests rely on: the four `<nav>` links by visible text,
   a sidebar button named "New report", a hero region with class `pulse`, a panel heading and
   button "Generate report", the theme chips "System / Light / Dark", the onboarding persona
   tiles and its "Continue" / "Start monitoring" buttons, and on More › Email / More › Telegram
   one `role="group"` card per destination ("Recipient 1" … / "Bot 1" …) with labelled inputs,
   the "All alerts" / "Critical only" radios, a button starting "Test", and the buttons "＋ Add
   recipient" / "＋ Add a bot" and "Save changes"; for the dashboard login, the heading "Sign in
   to DSpace Sentinel", labelled Username / Password inputs and the checkbox "Keep me signed in
   for 30 days" inside `#gate`, the button "Sign in", the server's sentence in `#signin-error`,
   the sidebar's `#account-name` and `#sign-out`, the More row `[data-key="account"]`, and on More
   › Account the labels Current password / New password / Repeat new password and the button
   "Change password". See `tests/e2e/test_ui.py`; the detail pages'
   states (the actuator, rendering, public URL and Solr cores tiles, the Ports table, a rung's
   hint, the key/value lists, and the Security page healthy, with every warning, not available,
   with 40 ports and with Tailscale's ports, its Home tile and its Stack ladder) are checked by
   `tests/e2e/test_detail_states.py`, and the wizard on a server without a web server by
   `tests/e2e/test_onboarding.py`, and the login by `tests/e2e/test_login.py`, which starts a live
   server of its own with `server.login` (the demo never asks for one): the sign-in screen (only
   `/api/session` is asked), a wrong password, signing in, Keep me signed in, Sign out, an ended
   session on More, the 429 countdown and More › Account's password change. `ADDED` in
   `test_detail_states.py` lists components the demo may not have
   yet: a state of one adds it to `/api/status` after Ports and answers its
   `/api/components/<id>` itself. `test_ui.py` checks the wizard's Security row for every option,
   More › Modules and the report panel's Security section, the saved PostgreSQL password's line,
   an acknowledged warning calming Home while its tile and Stack row keep warning, and the
   **Time zone** field (its help, the PATCH and a refused name).
6. Run the e2e suite (`SENTINEL_E2E=1 .venv/bin/python -m pytest tests/e2e`); it writes its
   captures to the git-ignored `build/screenshots/` so reviewers see the change without the
   curated README images being overwritten (the detail-state captures, `detail-*.png`, always go
   there). When a change should update the README, rerun it with
   `SENTINEL_E2E_SHOTS=docs/screenshots` (`test_ui.py` writes `home-light`, `home-dark`, `stack`,
   `history`, `more`, `report-panel`, `component`, `more-email` and `more-telegram` at 1440×900,
   `home-wide` at 1920×1080 and `home-narrow` at 390×844), and recapture `incident.png` by hand:
   start the demo with `SENTINEL_DEMO_FAST=1` (the scripted incident starts 30 s after the start
   and lasts two minutes; Solr is down from about 90 to 115 s), complete onboarding, and open the
   incident from History while it is still open, so the accent is coral, at 1440×900. Look at
   every image before committing: charts fully drawn, no snackbar left over (the dark Home
   capture follows the report test and can still show "Report ready"; retake it by hand then),
   light and dark as named.

## What to avoid

Shadows, gradients, a second accent colour, icon fonts, a third level of navigation, modals for
reversible actions, skeleton loaders, chart animations, status conveyed by colour alone, raw
values outside `tokens.css`, a framework or a bundler.
