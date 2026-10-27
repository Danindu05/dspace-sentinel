# Frequently asked questions

## General

**Does Sentinel change anything on my DSpace server?**
No. It never restarts, writes to, reindexes or reconfigures DSpace, PostgreSQL, Solr, Tomcat,
Node or nginx. Database sessions are read-only with a 3 s statement timeout, Solr queries use
`rows=0`, log files are read from the tail. The only files it writes are its own under `data/`
and `config/sentinel.yaml`. The Security module only reads too: it never opens or closes a port,
changes a firewall or runs sudo ([below](#security)).

**How heavy is it?**
Typically 0.3–0.8 % of one core and 80–120 MB RSS. The systemd unit caps it at 25 % of a core
and 300 MB just in case. The SQLite database grows about 10–20 MB per month with every module
on and 30-day retention.

**Which DSpace versions are supported?**
DSpace 7 (end-of-life since 7.6.7, its final release), 8 and 9: REST API at `/server/api`,
Angular UI, Tomcat 9, Tomcat 10 or the runnable JAR, Solr 8 or 9. Sentinel reads the version
DSpace reports and grades the few answers that differ between versions accordingly; DSpace 10
works the same way but is not claimed yet. What differs and what Sentinel does about it:
[COMPATIBILITY.md](COMPATIBILITY.md). DSpace 6 (XMLUI/JSPUI, REST at `/rest`) is not supported,
though the server, PostgreSQL, Solr, nginx, SSL, ports, security and logs modules work on any
host.

**Which operating systems?**
Linux with systemd, Python 3.10+. Tested on Ubuntu 22.04/24.04 and Debian 12. Without systemd
set every `*_service` key to `""` to skip the systemd rungs; the rest works.

**Does it need root?**
No. Install and run as your own user (`./sentinel.sh install`, `./sentinel.sh start` or
`--user-service`); `./sentinel.sh install` even refuses to run under `sudo`, because the files it
creates would then belong to root. Root/sudo is only needed to install a system-wide unit
(`--service` asks for it just for that step), for reading logs and `local.cfg` owned by another
user (add the Sentinel user to `adm`, the Tomcat group or the group of `/dspace/config`, then log
out and in again before you restart Sentinel) and for `htpasswd` files in `/etc/nginx`.

**Can I run it on a different machine than DSpace?**
Partly. HTTP checks (backend, frontend, nginx, SSL) and PostgreSQL/Solr checks work over the
network if those ports are reachable; the server, ports, security, logs and assetstore modules
describe the machine Sentinel runs on. The `frontend` persona is meant for a separate UI host that
monitors a remote backend URL.

## Setup

**The wizard found nothing. Why?**
Discovery reads `<dspace.install_dir>/config/local.cfg`, `dspace.cfg` and
`modules/assetstore.cfg` (default `/dspace`). If DSpace lives elsewhere set
`dspace.install_dir` first (More › DSpace connection) and press **Detect automatically**, or run
`./sentinel.sh discover`. It also needs read permission on those files. A DSpace configured
through environment variables (as the official Docker images are) may keep little in
`local.cfg`: fill in the URLs by hand then.

**PostgreSQL says "password authentication failed".**
Sentinel reads `db.username`/`db.password` from `local.cfg` during discovery, but many
installations keep the password elsewhere. Set `SENTINEL_PG_PASSWORD` (best in
`config/sentinel.env`) or `postgres.password`, or create a dedicated role:

```sql
CREATE ROLE sentinel_ro LOGIN PASSWORD '…';
GRANT CONNECT ON DATABASE dspace TO sentinel_ro;
GRANT USAGE ON SCHEMA public TO sentinel_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO sentinel_ro;
```

Read access to `item`, `bitstream`, `community`, `collection` and `eperson` is what the
repository module uses; `pg_stat_activity` and `pg_database_size` work for any role. When you
change `postgres.host`, `port`, `database` or `user`, type the password again: Sentinel only
ever sends a saved password to the server it was saved for, and refuses the change otherwise.

**I run Tomcat 10 / the runnable JAR / PM2 / Apache instead of nginx.**
**Detect automatically** (More › DSpace connection, or `./sentinel.sh discover`) finds
`tomcat9`, `tomcat10` and `tomcat11`, a unit of your own for the runnable JAR of DSpace 8+
(`dspace`, `dspace-backend`, `dspace-server`), the UI's unit or PM2's `pm2-<user>`, and nginx or
apache2, and moves the Tomcat log to match (`/var/log/tomcat10/catalina.out`; none for the JAR,
whose messages go to `dspace.log`). By hand: `dspace.backend_service: tomcat10` (or your unit's
name), `nginx.service: apache2`, and the log paths in `logs.files`. Any systemd unit name works;
`""` skips the systemd rung. Solr or the UI started by hand, without a systemd unit, get an empty
service name and a line under *What we found* ("Solr answers on 8983 but no systemd unit was found
for it: it may not start by itself after a reboot"), and the log of a Solr started by hand is
taken from its `-Dsolr.log.dir`. No web server at all, because DSpace is opened directly on :4000?
The wizard then leaves nginx and SSL off; if they are on, see "nginx is Critical, but my DSpace
works" below. See
[COMPATIBILITY.md](COMPATIBILITY.md#backend-runtimes-tomcat9-tomcat10-or-the-runnable-jar).

**The frontend rung says "no DSpace markup in the response".**
The homepage rung (`frontend.home`) expects HTTP 200 and the UI's `<ds-app>` element (or the
`<meta name="Generator" content="DSpace …">` tag of a server-rendered page) in the body of
`dspace.ui_url`. The word "dspace" alone is not enough, so a bot-check page or a maintenance page
in front of the UI shows up here. If Node serves the UI on another port, fix `ui_url`; if a
maintenance or bot-check page is in front, that is the finding.

**The frontend rung says "client-side rendering only".**
The UI answered with its empty client-side shell instead of a page rendered on the server, so
visitors get the site but search engines and link previews see an empty page. From DSpace 9.3
the UI renders on the server only for the host in its `ui.baseUrl`: set `dspace.public_url` to
that address (`https://repo.example.org`) and Sentinel asks for the homepage with that `Host`.
On DSpace 9.3+ the note says so when Sentinel's request named another host than the one DSpace
renders for (the REST root's `dspaceUI`): "client-side rendering only: DSpace 9.4 renders on the
server only for the host in its ui.baseUrl (set dspace.public_url)". Otherwise, also when the
request already carried that host, server-side rendering is switched off or failing in the UI;
its log says why. See
[COMPATIBILITY.md](COMPATIBILITY.md#the-frontend-server-side-rendering-and-the-93-host-rule).

**The actuator rung says "DSpace 9.2 reports DOWN while healthy".**
A known DSpace issue, shown muted and never an alert: DSpace 7.6.4–7.6.x, 8.2–8.4 and 9.0–9.3
answer an anonymous `DOWN` when their SEO health check finds a problem (sitemaps not generated
yet, a `robots.txt` that still names `localhost`, or no server-side rendering), even though the
site works. 8.5, 9.4 and 10.1 report the same as "up, with non-blocking issues". DSpace's own
Health page, opened as an administrator, names the failing check. Where DSpace shows its
components (`show-details=always`), a `DOWN` caused only by `seo` reads the same and names what the
check flags ("…: the SEO check flags sitemap, ssr"). On any other version a `DOWN`
reads "status DOWN (details need a DSpace admin login)" and opens "DSpace Backend degraded":
Sentinel never logs in to DSpace, so the details are only on that page. See
[COMPATIBILITY.md](COMPATIBILITY.md#the-actuator-anonymous-answers-and-the-seo-down-window).

**Solr: a core rung says "core not found (HTTP 404)".**
Edit `solr.cores` to the cores you actually have (`curl http://localhost:8983/solr/admin/cores?wt=json`).
Every DSpace from 7 to 9 ships `search`, `statistics`, `oai` and `authority`; some sites drop
`authority` or shard statistics. With `solr.multicorePrefix` the names carry the prefix
(`dspace_search`, …). The `qaevent` and `suggestion` cores of DSpace 8 and 9 need not be listed:
Sentinel finds them by itself.

**Solr: a core rung says "failed to load: …".**
Solr could not load that core and says why. On DSpace 9 with Solr 9.8 or later the usual reason
is that Solr ignores the `<lib/>` directives of DSpace's cores: start Solr with
`SOLR_OPTS="-Dsolr.config.lib.enabled=true"`, as the rung's hint says. See
[COMPATIBILITY.md](COMPATIBILITY.md#solr-8-or-9-optional-cores-and-prefixes).

**Solr: a core rung says "reload failed: … (still serving; a restart would not load it)".**
Solr tried to reload that core with a new configuration (**Reload** in Solr's admin UI, or
`action=RELOAD` after copying new configsets during an upgrade) and could not. It keeps serving
with the old configuration, so search still works and Sentinel still pings and searches it, but
the next Solr restart would load the new configuration and fail. Fix the core's `schema.xml` or
`solrconfig.xml` (Solr's log names the problem) and reload it again before restarting Solr.

**The logs module shows "unknown" for Tomcat.**
The file is missing or unreadable by the Sentinel user, and the Logs page says which. A missing
file ("file not found") is deliberately `unknown`, never a warning: point `logs.files` at the
right path, `/var/log/tomcat9/catalina.out` for DSpace 7 on Ubuntu 22.04,
`/var/log/tomcat10/catalina.out` for DSpace 8 and 9 on Ubuntu 24.04 or Debian 12/13. The runnable
JAR writes no `catalina.out` at all (its messages are in `dspace.log`), so remove that entry.
"permission denied" comes with a hint that names the file's owner, group and mode, or those of the
folder Sentinel may not enter, and the group to join, for example "The folder /var/log/tomcat10
belongs to tomcat:adm (mode 2750): add sentinel to the group adm, log out and in again, then
restart Sentinel." Run `sudo usermod -aG adm sentinel` (the group and the user the hint names),
then log out and in again before `./sentinel.sh restart` (next question). `/dspace/log/dspace.log`
keeps `daily_suffix: true` for every DSpace from 6 to 9.

**I added my user to the group, but the log still says "permission denied".**
A running program keeps the groups it started with, and so does every shell that was open before
`usermod`: a Sentinel started or restarted from such a shell (`./sentinel.sh start`, `restart`)
still runs without the new group. Log out, log in again (`id` now lists the group), then run
`./sentinel.sh restart`. The Logs page recognises this case and says so: "The user alex was
added to the group tomcat, but this Sentinel process does not have it yet: log out, log in again,
then restart Sentinel." Under the system unit (`./sentinel.sh install --service`) a restart is
enough, because systemd starts the service with the user's current groups. A user unit
(`--user-service`) runs under your systemd user manager, which stays up across logouts and keeps
the groups it started with: restart the machine, or ask root to restart that manager
(`sudo systemctl restart user@$(id -u alex)`).

**A log has fresh errors but the logs rung counts none (or counts them hours late).**
The file's timestamps are in another time zone than the server's clock. Sentinel reads local
time and recognises Solr's UTC log by itself; for any other file set `timezone` on its entry in
`logs.files` (`UTC` or an IANA name such as `Asia/Colombo`). Lines stamped more than 5 minutes in
the future are never counted, and the rung names the zone it used when it is not local time.
This setting is only about reading the file; `server.timezone` (the zone of the times Sentinel
sends) never changes how a log is read.

## The dashboard login

**I forgot the dashboard password.**
Run `./sentinel.sh set-password` on the server, as the user Sentinel runs as (on a host set up by
the Ansible role: `sudo -u sentinel /opt/dspace-sentinel/sentinel.sh set-password`). Press Enter
to keep the username, then type a new password twice; it needs at least 12 characters and may not
be the username. The running Sentinel picks it up within a few seconds and signs every browser
out, no restart needed. There is no email reset: whoever can run commands on the server can set
the password, and nobody else. On a host whose config the Ansible role writes
(`dspace_sentinel_manage_config: true`), the next play run puts the inventory's hash back, so put
the new one in the vault too (`./sentinel.sh hash-password` prints it).

**How do I turn the login off?**
`./sentinel.sh set-password --remove`. It asks "Remove the login for 'admin'? The dashboard will
not ask for a password. [y/N]"; `--yes` skips the question, and a script must pass it ("--remove
needs --yes when not run interactively"). The running Sentinel notices within seconds. It is
refused while Sentinel listens on another address than `127.0.0.1` with no `server.auth_token`
(and no `server.allow_unauthenticated`), because Sentinel would then refuse to start: "not
removing the login: Sentinel listens on 0.0.0.0 and without a login or server.auth_token it would
refuse to start; set server.auth_token first, or listen on 127.0.0.1". Without a login, anyone and
anything on the machine that can reach `127.0.0.1:9765` can use the API; keep one on a shared
server. To install without one, use `./sentinel.sh install --no-login`.

**Can I open Sentinel from another computer now?**
Yes. The SSH tunnel works as before (`ssh -L 9765:127.0.0.1:9765 repo-1`, then
http://localhost:9765 and sign in). Without a tunnel, keep Sentinel on `127.0.0.1` and put nginx
with HTTPS in front of it, using the certificate your repository already has
([REVERSE-PROXY.md](REVERSE-PROXY.md)), so the password never crosses the network in plain text.
With a login, Sentinel may also listen on another address itself (`server.host: 0.0.0.0` in
`config/sentinel.yaml`, then `./sentinel.sh restart`), but that is plain HTTP: do it only on a
trusted network or a VPN. The Security page then reads "listens on 0.0.0.0 with a login" with the
hint "Put the nginx HTTPS recipe in front of Sentinel so the password never travels in plain
text.". Opened by name rather than by IP address, the name must be one Sentinel knows (add it to
`server.allowed_hosts`).

**Why did it sign me out?**
A session ends after 12 hours without use, and 7 days after you signed in at the latest; with
*Keep me signed in for 30 days* both limits are 30 days. It also ends when you click **Sign out**,
when the browser's cookies are cleared, when the password changes (on **More › Account** every
other browser is signed out and yours stays; `./sentinel.sh set-password` signs out every
browser), and when the login is removed or an Ansible run writes a different hash. The page then
says "Your session ended. Sign in again." and, once you sign in, shows the page you were on. A
restart or an update does not sign anyone out: sessions are kept in `data/sentinel.db`, and
**Clear history** keeps them too.

**It says "Too many attempts. Try again in … s".**
After five wrong passwords from one address within 15 minutes, every further try has to wait:
1 s after the last failure, then 2, 4, 8 … up to 5 minutes. The **Sign in** button counts the wait
down. Twenty wrong passwords for one username within 15 minutes, from any addresses, lock that
username for 15 minutes. A successful sign-in clears your address's count, and a restart of
Sentinel forgets all of them. The events log shows each failure with its address ("Sign-in failed
for 'admin' (192.0.2.10)") and the pause ("Sign-in paused for 192.0.2.10 after 5 failed
attempts"). Behind nginx on the same machine each browser counts with its own address; behind a
proxy on another machine, everyone shares that proxy's address.

**How do scripts and the API get in?**
With `server.auth_token`, a token for scripts: put `SENTINEL_AUTH_TOKEN=<a long random string>`
in `config/sentinel.env` (`openssl rand -hex 24` makes one), restart, and send it as
`Authorization: Bearer <token>` (or as HTTP Basic with any user name). With a login set it works
next to the sign-in; without a login the browser's Basic pop-up asks for it too. On the server
itself, `./sentinel.sh status` uses a private token Sentinel writes to `data/api-token` at every
start, `doctor`, `report` and `discover` run without the API, and `/healthz` is always open for a
monitoring system. See [API.md](API.md#conventions).

**Can several people sign in?**
There is one account, so people share its password, and each browser gets its own session. For
one password per person, put nginx's `auth_basic` with an htpasswd file in front as well
([REVERSE-PROXY.md](REVERSE-PROXY.md)).

## Reading the dashboard

**What do the colours mean?**
The whole app is tinted by the worst enabled component and the open alerts: mint = everything
ok, butter = at least one warning, coral = something critical, sky = waiting for the first check.
Sky also means the page has lost touch: when Sentinel does not answer, or no check has finished
for 45 s (or three system intervals, if that is longer), the Pulse reads "Sentinel is unreachable
— last check 11:34" or "Checks are overdue — …" and the last known state stays on screen with
its colours greyed out until fresh data arrives. Every status also has an emoji or a label;
colour is never the only signal.

**What does "unknown" mean?**
Sentinel could not tell. A rung after a failed rung is "not reached" (a rung that answered but
too slowly does not stop the ones after it); a missing log file, a log Sentinel may not read
("permission denied", with a hint that names the group to join), a timed-out check (one rung
"… check" saying "check timed out after 29 s"), an actuator that answers 401/404, DSpace's known
SEO `DOWN` ("DSpace 9.2 reports DOWN while healthy …"), or a module that just got switched on and
has not run yet. Home names each check that could not run and why: "No open alerts — Logs cannot
be checked: permission denied (/dspace/log/dspace.log)." ("could not be checked this time" is only
for a check that timed out once). `unknown` never triggers or resolves an alert and never counts
as downtime in availability.

**nginx is Critical, but my DSpace works.**
Then there is most likely no web server on this machine: DSpace is opened directly on its own
port (`http://repo.example.org:4000`), and the DSpace Frontend check already watches that port.
The nginx check looks for a web server in front of DSpace (nginx or Apache on port 80 or 443);
with no such unit and nothing answering on `localhost:80` it stays critical, "connection refused
(localhost:80)", and the nginx page says under the HTTP rung: "No web server was found on this
machine: there is no nginx unit and nothing answers on localhost:80. If DSpace is opened directly
on its own port (for example :4000), switch this check off in More › Modules. …". Do that: switch
**nginx** off in **More › Modules**, and **SSL certificate** too when the site has no HTTPS. The
alert resolves with "module switched off". Pointing the nginx URL at port 4000 is not the answer,
since that is the UI itself. The first-run wizard leaves nginx and SSL off when it finds no web
server; they are on when they were chosen by hand or discovery could not run. If DSpace does run
behind Apache or another unit, set its service name and HTTP URL in **More › DSpace connection**
instead.

**Ports says "exposed publicly (::)" for Solr's port 8983. Is that a problem?**
Usually, yes. `::` or `0.0.0.0` means Solr listens on every network interface (Solr 8's default),
so any machine that can reach the server can reach Solr, and Solr has no login: whoever reaches
port 8983 can read everything in the index, including records DSpace hides from visitors
(private and withdrawn items) and the usage statistics, and can change or delete it; older Solr
releases have also had flaws that let such a visitor run code on the server. A private network
only narrows who that is. Sentinel sees where a port listens, not your firewall, so it warns
either way; the hint on the Ports page says what to do. To close it, make Solr listen on
localhost only (Solr 9 does so by default; DSpace then still reaches it at
`http://localhost:8983/solr` when both run on this machine), or block 8983 in the firewall for
everything but the DSpace host. When the firewall already closes it, or Solr must stay reachable,
accept it: change the rule in `config/sentinel.yaml` to

```yaml
ports:
  - {port: 8983, label: Solr, expect: open}
```

and run `./sentinel.sh restart` (keep the other rules in the list). **More › DSpace connection**
has no port rules. PostgreSQL on 5432 gets the same warning and hint: anyone who reaches it can
try to log in to the database (`listen_addresses` in `postgresql.conf` sets where it listens).

**The assetstore says "filesystem not responding".**
The assetstore sits on a network mount (NFS, CIFS) whose server stopped answering. Sentinel
runs every filesystem call in a worker thread, one per path at a time, so a hung mount never
freezes the dashboard: the mount is left out of the server's disk list (3 s probe, logged once)
and the Assetstore component turns critical with "filesystem not responding (<path>)" after
10 s, which opens the alert "Assetstore failing". It clears by itself once the mount answers.

**Why is the backend "degraded" and not "down" when Solr is dead?**
Dependency rungs are capped at *warn*: the REST API still answers, only search is broken. The
Solr component itself is critical, and the incident (if the module is on) names both. When Solr
answers correctly but too slowly, the backend's Solr dependency rung says so: "Solr is slow
(Critical): search took 4.3 s" rather than "is down".

**Why did the Stack tab turn into "Services"?**
None of the DSpace-specific modules (PostgreSQL, Solr, backend, frontend, repository,
assetstore) is on, so the tab lists generic services (nginx, SSL, ports, logs) under a generic
name.

**The homepage latency looks high but users do not complain.**
`frontend.home` measures Node rendering the Angular homepage server-side, which is often
1–3 s on a small box; users get cached assets and a client-side app afterwards. Raise
`home_latency_ms` if it is noise for you, or watch `frontend.public` through nginx instead.

**Charts are empty.**
"📈 Collecting the first samples — charts appear after a minute." Ranges over seven days read
hourly roll-ups, topped up with the newest raw samples. If a chart stays empty, check
`./sentinel.sh logs` for a collector error.

## Alerts

**I got one warning and then nothing, is it still on?**
Yes. Sentinel notifies on open, escalate and resolve only, by default. Set
`alerts.reminder_minutes` for periodic reminders while an alert is open and unacknowledged.

**A CPU spike did not alert.**
By design: `confirm_samples: 2` means two consecutive breaching samples (30 s). Lower it to 1
for instant alerts, or accept that indexing bursts are not incidents.

**I clicked Ack. Why is the warning still there?**
Ack switches the reminders off and calms Home: the headline reads "All systems healthy · 1
acknowledged" and the colour goes back to healthy. The alert stays listed with its **Acked** pill
("Reminders are off. It stays listed until the cause is fixed."), and the component (its tile,
its page, the Stack and History) keeps its warning until the cause is fixed; then the alert
resolves by itself. If it escalates to critical first, it is unacknowledged again and colours
Home. A warning without an alert, such as a pending security update, has nothing to
acknowledge and keeps colouring Home. To end the warning, fix the cause, change the rule or
threshold behind it, or switch the module off.

**How do I silence alerts during maintenance?**
Acknowledge the open ones (stops reminders and calms Home), or set quiet hours, or switch the
relevant module off for the duration (a module that is off leaves no trace and produces no
alerts). There is no separate maintenance mode.

**An alert says "Started 15:59 UTC" but my browser shows 21:29.**
Messages, emails and reports use the zone of `server.timezone`, which is the server's own zone
(often UTC) until you set it; the dashboard shows your browser's time. Set `server.timezone`
(**More › Alert thresholds › Time zone**) to your zone, for example `Asia/Colombo`: from then on
the messages read "Started 21:29 +0530", and quiet hours, daily digests and the weekly report run
on that clock too. Every time Sentinel sends names its zone, so a message is never ambiguous
([CONFIG.md](CONFIG.md#the-time-zone-of-messages-and-schedules)).

**Can several people get the alerts, on Telegram or by email?**
Yes: up to five Telegram bots, one per person (each creates their own with @BotFather and adds
its token and their chat id in More › Telegram), and up to five email addresses through Gmail
(with an app password) or any other mail server (More › Email). Each bot and each address can
take all alerts or only critical ones. Setup: [NOTIFICATIONS.md](NOTIFICATIONS.md).

**My bot token (or app password) shows as dots after saving. Was it saved?**
Yes. A saved secret is never sent back to the browser, so the field shows `••••••` with the line
"Saved. Hidden for safety — type a new one to replace it." under it. Saving with the dots in
place keeps the stored value; the first key you type, paste or delete replaces all of them.
**Test** on the row shows that it works. A password set in `config/sentinel.env`
(`SENTINEL_SMTP_PASSWORD`) shows a locked field instead: change it in that file.

**The test works but I never get alerts.**
See [NOTIFICATIONS.md](NOTIFICATIONS.md#troubleshooting): **Send alerts to Telegram** or **Send
alerts by email** (`enabled`) must be on, since the tests are sent regardless; quiet hours drop
warnings on that channel; a bot or recipient set to **Critical only** gets no warnings; a
systemd-run Sentinel never sees variables exported in your shell (put them in
`config/sentinel.env`); and in demo mode nothing is ever sent.

**The Telegram test says "api.telegram.org did not answer within 10 s".**
This server cannot reach Telegram: a firewall blocks outgoing HTTPS, or the network lets
programs out only through a proxy. Check it on the server:

```bash
curl -sS -m 10 -o /dev/null -w '%{http_code}\n' https://api.telegram.org/
```

A status code means Telegram answers; `000` with "Connection timed out" confirms the problem. If
your network has a proxy, add it to `config/sentinel.env` and restart:

```bash
echo 'HTTPS_PROXY=http://proxy.example:3128' >> config/sentinel.env
./sentinel.sh restart
```

From then on an error names the proxy ("… through the proxy proxy.example:3128: …"). Otherwise ask
for outgoing HTTPS (port 443) to `api.telegram.org`. A broken IPv6 route alone is not the cause:
Sentinel tries IPv4 as well. A refused connection, a failed DNS lookup and a TLS error each get
their own sentence ([Telegram errors](NOTIFICATIONS.md#telegram-errors-and-what-they-mean)).

**Gmail says the login was refused.**
Gmail refuses an account's normal password from programs: create an app password (Google Account
› Security › 2-Step Verification › App passwords, 16 letters) and paste that into More › Email,
or put it in `SENTINEL_SMTP_PASSWORD` in `config/sentinel.env`. The sentence names the account
and Gmail's own answer: "Gmail refused the login for alerts@example.org (535 5.7.8 Username and
Password not accepted): …". See [NOTIFICATIONS.md](NOTIFICATIONS.md#gmail).

**Gmail refused the login although I used an app password.**
The app password must be made in the same Google account as the address that sends: one created
while signed in to another Google account is refused. Compare the address in the sentence ("Gmail
refused the login for alerts@example.org …") with the account you made the password in, then
create a new app password in the right one and paste it. Right after a refused login Gmail
drops new connections for a few minutes, so the next test may say "smtp.gmail.com:587 closed the
connection. Gmail does this for a few minutes after a refused login: …": fix the login, wait a
few minutes, then test again.

## Security

**What does the Security check look at, and is it safe?**
It looks at how exposed this server is and changes nothing. Every 15 minutes
(`collector.security_interval`) it lists the ports that wait for connections and grades them
against your `ports` rules, asks `systemctl is-active` whether ufw, nftables or firewalld runs
(and reads `/etc/ufw/ufw.conf`), reads `/etc/ssh/sshd_config` and the files it includes, Ubuntu's
`/var/lib/update-notifier/updates-available` and `/var/run/reboot-required`, and `/etc/os-release`;
it takes the DSpace, Solr and PostgreSQL versions the other modules already found, looks at the
mode and owner of `local.cfg`, `dspace.cfg`, `sentinel.yaml` and `sentinel.env` (never their
contents), checks how Sentinel itself listens, and sends one GET to `dspace.public_url` for its
response headers. No sudo, no command that changes anything, no write. The full list is in
[SECURITY.md](../SECURITY.md#what-the-security-module-reads). Only three findings open an alert
(a new port open to the network, no host firewall while ports are exposed, root may log in over
SSH with a password); the rest stays on the Security page and in reports
([ALERTS.md](ALERTS.md#security)).

**Security says a port is exposed. What now?**
*Exposed* means other machines can reach the port (it listens on `0.0.0.0`, `::` or a public
address) and no rule under `ports` in `config/sentinel.yaml` expects it; *new* means the same,
for a port that opened after Sentinel started (the ports open at start-up are its baseline, kept
until the next restart), and only *new* opens the alert "New port open to the network". If the
port should be reachable, add a rule for it (`{port: 3306, label: MySQL, expect: open}`) and
restart Sentinel. If it should not, make its program listen on `127.0.0.1`, or block it in the
host firewall (`sudo ufw deny 3306`, say). Sentinel sees where a port listens, not what a
firewall lets through, on this host or in your network or cloud provider: a port that a firewall
already blocks still reads exposed here, so give it a rule once you know it is closed from
outside. The Firewall check and its "Reachable without a rule" count exactly the unexpected
ports (exposed and new); a `local_only` port that listens publicly is named apart there ("…, and
1 local-only port is public"). The Program column is empty for ports that belong to other
users, because Sentinel runs as a normal user; a note under the table says so: "Program names
are known only for Sentinel's own user; run `sudo ss -ltnp` on the server to see the others."

**Security lists my Tailscale ports. Are they exposed?**
No. A port that other machines can reach only on Tailscale's addresses (`100.64.0.0/10` and
`fd7a:115c:a1e0::/48`) gets the plain verdict *Tailscale*, with the note "reachable through
Tailscale only": it is not counted as reachable from the network or as unexpected, so it neither
warns nor opens an alert, and the check's message counts it apart ("· 2 through Tailscale only").
While the `tailscale0` interface exists, Tailscale's WireGuard port, UDP 41641 on every
interface, is *Expected* with the note "Tailscale's own port (WireGuard)". Anyone on your tailnet
can still reach such a port, so a service without a login there deserves a look all the same.
A socket on `::ffff:127.0.0.1` (how Java shows one on 127.0.0.1, Solr's stop port for example) is
loopback, *Local*.

**Security warns about my software support. Where do the dates come from?**
From a table in `dspace_sentinel/util/support_dates.py`, checked on 2026-10-05 against each
project's own page: the DSpace release policy (7.x reached its end of life when DSpace 10.0 came
out on 2026-05-28), solr.apache.org/security.html (Solr 8.0 to 8.11.0 ship a Log4j affected by
Log4Shell, CVE-2021-44228: upgrade to 8.11.1 or later), postgresql.org/support/versioning (the
final release of each major, 11 to 17), ubuntu.com/about/release-cycle (end of standard support
of the LTS releases) and wiki.debian.org/LTS. The versions are the ones the Backend, Solr and
PostgreSQL modules report, plus `/etc/os-release`; a module that is off leaves its software out.
Support that ends within 90 days is mentioned but stays green ("PostgreSQL 13.4 support ends
2025-11-13"); a distribution the table does not know gets "no support date known", no verdict.
The table ships with Sentinel, so a newer release can carry newer dates (`./sentinel.sh
update`). Software warnings never alert.

**Can Sentinel close a port for me?**
No, by design. Sentinel is observe-only: it never opens or closes a port, never changes a
firewall rule and never runs sudo, so nothing on the dashboard can lock you out of your own
server. Each warning carries a hint with the command or setting to change ("Switch a
host firewall on: for example `sudo ufw allow OpenSSH`, then `sudo ufw enable`; …"), for you to run
by hand.

**Why does Security say "not available" for SSH or updates?**
Because Sentinel could not read what it needs, and that is not counted against the server. SSH
reads "not available · sshd_config is not readable by Sentinel's user" when
`/etc/ssh/sshd_config` is not readable by the user Sentinel runs as (it usually is, mode 644), and
"not available · port 22 listens, but /etc/ssh/sshd_config was not found" when SSH runs from
elsewhere; with no file and nothing on port 22 it reads "no SSH server found". Updates read "not
available · no update information on this system (Ubuntu's update-notifier)" on Debian and other
distributions, which do not keep `/var/lib/update-notifier/updates-available` (on Ubuntu the
`update-notifier-common` package writes it). A rung that is not available neither opens nor
clears an alert.

## Data and privacy

**Where is my data?**
`data/sentinel.db` (SQLite), `data/reports/`, `data/sentinel.log`, plus `config/sentinel.yaml`.
All git-ignored. `More › About › Export data` gives you a JSON dump; `Clear history` wipes the
history and keeps the config.

**Does it phone home?**
No. The only outbound connections are the ones you configure: your DSpace, PostgreSQL, Solr,
nginx, `api.telegram.org` when a Telegram bot is set up, your mail server when email is, and
your webhook URL. Discovery also asks your own DSpace and Solr for their versions. The web UI's fonts are
loaded from Google Fonts by the browser and fall back to system fonts offline; reports load
nothing (the HTML report uses system fonts and makes no external requests).

**Can several people use it?**
Yes. They share the one dashboard login, each browser with its own session
([above](#the-dashboard-login)); for a password per person put nginx with basic auth in
front as well ([REVERSE-PROXY.md](REVERSE-PROXY.md)). The theme choice is stored server-side, so
it is shared.

**How do I back it up?**
Copy `config/sentinel.yaml` (and `config/sentinel.env` if you use it) and `data/`. The SQLite
file is safe to copy while running (`sqlite3 data/sentinel.db ".backup backup.db"` is safest).

## Operations

**How do I update?**
`./sentinel.sh update` (git pull, pip install, then a restart of the unit or of the running
background process). Your config and data are never touched; new config keys get defaults.

**How do I move it to another port or expose it?**
Set `server.port: 9800` in `config/sentinel.yaml` and run `./sentinel.sh restart`, or let the
installer write it for you: `./sentinel.sh install --port 9800` followed by
`./sentinel.sh restart`, or, with a systemd unit, `./sentinel.sh install-service --port 9800`,
which writes the port and restarts the unit. `SENTINEL_PORT` only applies to processes started
from that shell (`run`, `start`, `demo`): a systemd unit never sees it unless it is in
`config/sentinel.env`. For `server.host: 0.0.0.0` read [REVERSE-PROXY.md](REVERSE-PROXY.md)
first: Sentinel refuses to start on a non-loopback address without the dashboard login or
`auth_token` (unless you set `allow_unauthenticated: true`), and with a login the password then
travels in plain text unless HTTPS is in front.

**Can I run two instances (for example prod and demo) on one machine?**
Yes: different `SENTINEL_PORT`, `SENTINEL_DATA_DIR` and `SENTINEL_CONFIG`. `./sentinel.sh demo`
already uses `data/demo/`. `start` refuses a port that another Sentinel (or another program)
already holds. Every checkout manages only the `dspace-sentinel` systemd unit whose
`WorkingDirectory` is that checkout; a second checkout next to it uses its pid file, and
`install-service` will not replace another checkout's unit without `--force`.

**Can cron or a monitoring system run the check?**
Yes. `sentinel.sh` works from any directory and through a symlink, and `doctor` exits 0 (ok),
1 (warnings) or 2 (critical); 4 means the check could not run and 64 a usage error, so a wrapper
can tell "Solr is down" from "the check did not run". `doctor --ascii` avoids emoji in mail and
plain terminals. On an Ansible-managed host run it as the service user, see
[ANSIBLE.md](ANSIBLE.md#day-two-operations).

**How do I uninstall?**
`./sentinel.sh uninstall` stops Sentinel and removes this checkout's systemd unit and pid file;
`data/` and `config/` stay (`--purge` deletes them too). Delete the directory afterwards if you
want everything gone.

**Something is wrong, what do I send with a bug report?**
`./sentinel.sh doctor --json` (it contains no secrets), the output of `./sentinel.sh logs -n 100`,
`./sentinel.sh version -v`, your OS and the DSpace version. Use the bug report template.
