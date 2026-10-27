# DSpace compatibility

Sentinel supports **DSpace 7 (end-of-life), 8 and 9**. It checks DSpace the way an anonymous
visitor sees it, through the same REST endpoints, database tables and Solr requests in every
version, and it reads the version DSpace reports so that the few answers that mean different
things in different versions are graded correctly. DSpace 10 works too, and is listed below
for reference, but it is not claimed yet.

This page says what differs between the versions, what Sentinel does about it and what, if
anything, you need to set. The facts come from DSpace's source at each release tag, its
official documentation and anonymous probes of public DSpace sites; the
[sources](#sources) are listed at the end.

## At a glance

| | DSpace 7.x (7.6.x) | DSpace 8.x | DSpace 9.x | DSpace 10.x (works, not claimed) |
|---|---|---|---|---|
| Support status | end-of-life (7.6.7 was the final release) | supported | supported | supported upstream |
| What Sentinel checks | REST root `GET /server/api`, actuator `GET /server/actuator/health`, search `GET /server/api/discover/search/objects?size=1`, PostgreSQL, Solr STATUS/ping/select, logs, assetstore | same | same | same (REST root adds `uniqueType`; nothing else changes) |
| Backend runtime | Tomcat 9 (Ubuntu 22.04 package `tomcat9`) | Tomcat 10.1 (`tomcat10` on Ubuntu 24.04 / Debian 12-13) or the runnable JAR `[dspace]/webapps/server-boot.jar` | same as 8 | same as 8 (Java 21) |
| Anonymous actuator | aggregate status only, components hidden, DOWN served with HTTP 200 | same | same | same |
| `UP_WITH_ISSUES` (e.g. no GeoLite database) | ok: "up, with non-blocking issues" | ok | ok | ok |
| Anonymous DOWN while REST answers | 7.6.0-7.6.3 warning; 7.6.4-7.6.x informational (SEO health check) | 8.0-8.1 warning; 8.2-8.4 informational; 8.5+ warning | 9.0-9.3 informational; 9.4+ warning | 10.0 informational; 10.1+ warning |
| Server-side rendering (SSR) Host rule | none | none (8.4 only requires `ui.baseUrl`) | 9.3+: SSR only for the host of `ui.baseUrl`; other hosts get the client-side shell and an SSR error is logged | yes |
| Solr | 8.x | 8.x | 9.x (Solr 9.8+ needs `SOLR_OPTS="-Dsolr.config.lib.enabled=true"`) | 9.x |
| Solr cores with a rung | search, statistics, oai, authority | + qaevent, suggestion (optional, auto-detected) | + qaevent, suggestion | + qaevent, suggestion, audit |
| DSpace log | `[dspace]/log/dspace.log`, rolled daily to `dspace.log-YYYY-MM-DD` | same | same | same |
| CLI (cron) log | `[dspace]/log/dspace-cli.log` from 7.6.3 | from 8.1 | yes | yes |
| Tomcat log discovery suggests | `/var/log/tomcat9/catalina.out` | `/var/log/tomcat10/catalina.out`, or none for the JAR (its Tomcat messages go to `dspace.log`) | same as 8 | same as 8 (Debian 13 `tomcat11`: `/var/log/tomcat11/catalina.out`) |
| UI process | PM2 (`pm2-<user>` unit) or own unit | same | same | same |

DSpace 6 (XMLUI/JSPUI, REST at `/rest`) is not supported, though the server, PostgreSQL, Solr,
nginx, SSL, ports, security and logs modules work on any host.

## What Sentinel checks in every version

The things Sentinel asks DSpace are the same from 7.0 to 10.1: the REST root (`dspaceVersion`,
`dspaceName`, `dspaceUI`, `dspaceServer`), `discover/search/objects` with its `totalElements`,
the anonymous actuator, the context path `/server` on port 8080, and the PostgreSQL tables the
Repository module counts (`item`, `bitstream`, `community`, `collection`, `eperson`,
`cwf_workflowitem`), none of which was renamed. DSpace documents PostgreSQL 12–17 for 7.x and
8.x and 14–17 for 9.x and 10.x; Sentinel's queries work on all of them.

What changes is how a few answers must be read, so the backend collector parses
`dspaceVersion` once per check: `"DSpace 9.4"`, `"DSpace 7.6"` (x.0 releases print two
numbers), `"DSpace 7.6.8-SNAPSHOT"` and DSpace-CRIS roots (which add `crisVersion`) are all
understood; `"DSpace unknown"` and `"DSpace none"` mean no version, and the version-dependent
rules below then take their most cautious reading. The parsed version is in the backend's
metrics (`dspace_version`, `dspace_major`, `dspace_minor`, `dspace_patch`, `dspace_snapshot`,
`cris_version`) and drives:

- the [actuator grade](#the-actuator-anonymous-answers-and-the-seo-down-window) (the SEO DOWN
  window),
- the [client-side rendering note](#the-frontend-server-side-rendering-and-the-93-host-rule)
  (the 9.3+ Host rule),
- which [optional Solr cores](#solr-8-or-9-optional-cores-and-prefixes) get a rung,
- the [discovery findings](#discovery-units-logs-and-versions) about end of life and the Solr
  version,
- the Security module's *Software* check, which reads the version the backend reported (and
  Solr's, PostgreSQL's and the operating system's) against a table of support dates: 7.x "reached
  end of life on 2026-05-28", the day DSpace 10.0 came out; the dates and their sources are in
  [SPEC.md](SPEC.md#6-collectors-and-health-chains-collectors-healthpy) and the
  [FAQ](FAQ.md#security).

`./sentinel.sh doctor` prints the version next to the backend, marked for 7.x:
"DSpace 7.6.7 (end-of-life)". Discovery says it too: "DSpace 7.x is end-of-life (7.6.7 was the
final release; supported: 8.x, 9.x, 10.x)". On the backend's page (Stack › DSpace Backend) the
DSpace tile shows the version with its line under it ("DSpace 9.x", plus "snapshot build" for a
`-SNAPSHOT`), "DSpace-CRIS cris-2024.02.04" when the REST root names one, and on 7.x the quiet
note "7.x is end of life — 7.6.7 was the final release".

## Backend runtimes: `tomcat9`, `tomcat10` or the runnable JAR

DSpace 7 needs Tomcat 9, which Ubuntu 22.04 packages as `tomcat9`. From DSpace 8 Tomcat is
optional: the backend runs either in Tomcat 10.1 (`tomcat10` on Ubuntu 24.04 and Debian 12/13)
or as a runnable JAR, `java -jar [dspace]/webapps/server-boot.jar`, which is what the official
Docker image uses. DSpace's documentation names no systemd unit for either, so sites use
whatever name they chose (`dspace`, `dspace-backend`, …).

Set `dspace.backend_service` to the unit that runs the backend. The default is still `tomcat9`;
**Detect automatically** (or `./sentinel.sh discover`) finds the right one: it asks systemd for
`tomcat9`, `tomcat10`, `tomcat11`, `tomcat`, `dspace-backend`, `dspace-server` and `dspace`, in
that order, and prefers a unit that is active over one earlier in the list that is not (a
`tomcat9` left behind after moving to `tomcat10`). Set `""` to skip the systemd rung altogether,
for example in a container.

The UI runs under PM2 in DSpace's own instructions (`pm2 start dspace-ui.json`); `pm2 startup`
installs a unit named `pm2-<user>`. Discovery offers it as `dspace.frontend_service` when there
is no `dspace-ui`, `dspace-angular` or `dspace-frontend` unit, with the finding "pm2-dspace is
PM2's own unit: it only proves the PM2 daemon runs (the homepage check shows whether the DSpace
UI answers)". The homepage rung is what tells you whether the UI works.

### The Tomcat log

Packaged Tomcat on Ubuntu and Debian writes `/var/log/tomcatN/catalina.out` through rsyslog, in
its own format:

```
[2026-09-26 10:41:02] [crit]One or more listeners failed to start. Full details will be found in the appropriate container log file
[2026-09-26 10:41:02] [warning]The web application [server] appears to have started a thread named [...] but has failed to stop it
```

Sentinel counts these lines: `[crit]`, `[err]`, `[alert]` and `[emerg]` are errors, `[warning]`
and `[warn]` warnings. Every line of a record arrives stamped with the record's severity, its
stack trace included, so one failed start (`Failed to initialize component
[Connector["http-nio-8080"]]` with a `BindException` when port 8080 is still taken) is twenty or
more `[crit]` lines. Sentinel counts such a record once: a stamped line continues the one above
it when its message is empty, is a trace line (`at …`, `Caused by: …`, `... 8 more`,
`Suppressed: …`) or is an exception's first line (`java.net.BindException: …`) with the same
severity within a second. **Last error** shows the record's first line. Two records written in
the same second still count twice. Tomcat's own `catalina.YYYY-MM-DD.log` (JULI format,
`26-Sep-2026 10:41:02.123 SEVERE …`) is understood as well, and there too a stack trace counts
once. The runnable JAR writes no `catalina.out`: its Spring and embedded Tomcat messages go to
`dspace.log`.

The default Tomcat log entry is `/var/log/tomcat9/catalina.out`. Discovery moves it to
`/var/log/tomcat10/catalina.out` (or `tomcat11`) when that is the backend's unit, and leaves it
out of the suggestion when there is no Tomcat unit at all, the backend runs anyway and the file
does not exist: "No Tomcat unit: the Tomcat log (/var/log/tomcat9/catalina.out) is left out; the
runnable JAR of DSpace 8+ writes its Tomcat messages to dspace.log". A path you set yourself is
never rewritten.

Sentinel's user needs read access to these files. When it has none, the log's rung reads
"permission denied (/var/log/tomcat10/catalina.out)" and its hint names the file's owner, group
and mode, or those of the folder it may not enter, and the group to join, for example "The folder
/var/log/tomcat10 belongs to tomcat:adm (mode 2750): add sentinel to the group adm, log out and in
again, then restart Sentinel." ([FAQ](FAQ.md#setup)).

## The actuator: anonymous answers and the SEO DOWN window

Sentinel reads `GET /server/actuator/health` anonymously, as it reads everything else: it never
logs in to DSpace. DSpace answers an anonymous client with the overall status only (the
components need an administrator login, and `/actuator/health/<component>` answers 404), and it
serves `DOWN` with HTTP 200. The rung `backend.actuator` is graded the way DSpace defines its
statuses:

| DSpace answers | The rung |
|---|---|
| `UP` | ok, "UP" |
| `UP_WITH_ISSUES` | ok, "up, with non-blocking issues". Every version answers this when no GeoLite database is configured for the usage statistics, which is common. |
| `DOWN`, on a version inside the SEO window below, while the REST API answers | informational, "DSpace 9.2 reports DOWN while healthy (a known SEO health check issue fixed in 8.5 / 9.4 / 10.1)"; it never opens an alert |
| `DOWN` otherwise (also `OUT_OF_SERVICE`) | warning, "status DOWN (details need a DSpace admin login)", which opens "DSpace Backend degraded" |
| components visible (an administrator's view, or `show-details=always`) | a `DOWN` or `OUT_OF_SERVICE` one makes a warning that names it, "status DOWN (db)", in every version. The exception: a `DOWN` whose only down component is `seo`, on a version inside the SEO window, is the known issue above, naming what the SEO check flags: "DSpace 9.2 reports DOWN while healthy (a known SEO health check issue fixed in 8.5 / 9.4 / 10.1): the SEO check flags sitemap, ssr" |
| HTTP 401, 403 or 404 | informational, "not exposed (HTTP 404)" |

**The SEO DOWN window.** DSpace 7.6.4, 8.2 and 9.0 added an SEO health check that fetches the
UI's sitemaps, its `robots.txt` and the homepage (server-side rendering) on every health call.
Up to 7.6.x, 8.4, 9.3 and 10.0 any problem there turns the overall status to `DOWN`; 8.5, 9.4
and 10.1 report it as `UP_WITH_ISSUES` instead. So a healthy 7.6.4–7.6.x, 8.2–8.4, 9.0–9.3 or
10.0 site whose sitemaps have not been generated yet, whose `robots.txt` still names
`localhost`, or whose UI does not render on the server answers an anonymous `DOWN`. Public sites
running those versions really do. Inside that window, and only while the REST rung answers
(ok, or correct but slow), Sentinel reads the `DOWN` as this known issue. When DSpace shows its
components (`management.endpoint.health.show-details=always`), Sentinel reads them instead: only
`seo` down is the known issue, with the checks it flags (`sitemap`, `robots.txt`, `ssr`), and any
other component down is a warning. DSpace's own **Health** page, opened as an administrator,
shows which check failed.

Outside the window a `DOWN` is a warning: something DSpace itself considers broken, which only
an administrator's view can name. Sentinel's own PostgreSQL and Solr ladders usually show the
cause already.

**Every five minutes.** Each call to `/actuator/health` runs every DSpace health indicator,
including the SEO check's requests to the UI and a server-side render of its homepage. Sentinel
therefore asks the actuator at most once every 300 seconds and reuses the answer in between
(the rung's `detail.cached_age_s` says how old it is); the REST and search rungs are still
measured every cycle, and `doctor` or a configuration change asks at once. Only an answer that
reads ok or informational is reused: after a `DOWN` that is not the known issue, an error or a
time-out, the next cycle asks again, so a single bad answer counts once toward an alert, as any
other check's would. The answer also belongs to one backend process: a stopped service, a closed
port or a failing REST API forgets it, and a new DSpace version in the REST root asks again, so
a restart or an upgrade is never graded from the process before it. The rung also carries
`detail.health_status` (DSpace's word) and, for the known issue, `detail.known_issue: "seo"`;
the backend's metrics carry `actuator_status`.

**In the UI.** The backend's ladder shows the known issue muted, as "known issue · DSpace 9.2
reports DOWN while healthy (…)" (screen readers hear "Known issue"), never as a failure. The
backend's page has an **Actuator health** tile with DSpace's status in words ("Up", "Up, with
non-blocking issues", "Down · known issue", "Out of service"), the rung's message when it says
more ("status DOWN (details need a DSpace admin login)"), how old the answer is and when it is
asked again: "checked 3 m ago · asked at most every 5 minutes" for an answer the backend reuses
(ok or informational), "checked 6 s ago · asked again at the next check" under a warning (a
`DOWN`, an HTTP error, a time-out), and no such line while the rung reads "not reached". Its
components table appears only when DSpace shows them, which it does for an administrator's view
(with the known issue it still lists `seo` as Down, while the backend stays Healthy), and "Not
exposed (HTTP 404)" only when the rung says so.

## The frontend: server-side rendering and the 9.3+ Host rule

The homepage rung (`frontend.home`) fetches `dspace.ui_url` and expects HTTP 200 with the UI's
`<ds-app>` element, or the `<meta name="Generator" content="DSpace …">` tag DSpace writes into a
server-rendered page. The bare word "dspace" does not count any more: a bot-check page on
`dspace.example.edu` now reads "no DSpace markup in the response" (a warning). Up to 2 MiB of
the page is read, because server-rendered pages inline their CSS.

The rung also tells a **server-side render** (`ng-server-context="ssr"`, or an `ng-version`
attribute) from the **client-side shell** (an empty `<ds-app></ds-app>`, the UI's
`index.html`). The shell keeps the rung ok, with the note "client-side rendering only
(server-side rendering is off or failing)". Visitors still get the site, but search engines
and link previews see an empty page. The frontend's metrics carry `rendering` (`ssr` or
`csr`), `generator` (`DSpace 9.4`), `angular` (the `ng-version`, `20.3.30`) and `host_header`.

**The Host rule of 9.3 and later.** From 9.3 (and in 10.x) the UI renders on the server only for
the host in its `ui.baseUrl`, the fix for CVE-2026-27739. A request for any other host, such as
Sentinel's `GET http://localhost:4000/`, gets the client-side shell, and the UI writes "Error in
server-side rendering (SSR)" to its log every time. Set **`dspace.public_url`** to the address
your users type (`https://repo.example.org`, the host in `ui.baseUrl`) and Sentinel sends the
homepage request to `ui_url` with `Host: repo.example.org`, plus `X-Forwarded-Proto: https` for
an `https` address, exactly what your reverse proxy sends. Nothing needs to change on the DSpace
side. When the request carried another host than the one DSpace renders for, the note says
what to do: "client-side rendering only: DSpace 9.4 renders on the server only for the host in
its ui.baseUrl (set dspace.public_url)". Sentinel knows that host from the REST root's
`dspaceUI` (`dspace.ui.url`, which 9.3+ require to match `ui.baseUrl`), else from `public_url`,
and the version from the backend module, so this note needs the backend module on. When the
request already named that host (for example `ui_url` is the public `https` address, as discovery
suggests), or Sentinel cannot tell which host it is, the Host rule is not the cause and the note
is the general "client-side rendering only (server-side rendering is off or failing)": look at
`ssr.enabled` in the UI's configuration and at the UI's log. The header is sent only when
`public_url` names another host than `ui_url`. The alternative on the DSpace side
is `NG_ALLOWED_HOSTS` in the UI's PM2 environment. Earlier versions render for any host, so
the header changes nothing there.

`dspace.public_url` also adds the `frontend.public` rung (the homepage through nginx and TLS, the
way users see it) and is the default host of the certificate check.

**In the UI.** The frontend's page (Stack › DSpace Frontend) has a **Rendering** tile:
"Server-side (SSR): the homepage arrives already rendered" or "Client-side shell" with the
rung's note, the Generator and Angular versions the page names ("not in the page" otherwise; a
client-side shell never names Angular), and the Host header Sentinel sends: "repo.example.org
(the public URL's host)", or "localhost (the UI URL's host)" when `dspace.public_url` is empty
or names the same host. Its **Public URL** tile reads "not set — add it in More › DSpace
connection" only when `dspace.public_url` is empty. With a URL set it shows what the last check
found: the time through nginx and TLS, or, when that request did not work, why, in the rung's
colour, with the time for a warning ("no DSpace markup in the response") and "—" for a critical
failure ("connection refused (repo.example.org:443)"); and "not checked yet" before the first
check, after a frontend check that failed as a whole, and for up to one check interval after the
URL is saved (`collector.http_interval`, 30 s by default).

## Solr 8 or 9, optional cores and prefixes

DSpace 7 and 8 run on Solr 8.x; DSpace 9 and 10 need Solr 9.x. Discovery compares the two and
says so when they do not match: "DSpace 9 expects Solr 9 but Solr 8.11.4 answers". Solr 9 also
listens on `127.0.0.1` by default where Solr 8 listens on every interface, so on a 7.x or 8.x
host the `8983 Solr (local only)` port rule warns "exposed publicly (::)" unless Solr listens on
localhost only. A firewall does not end the warning: Sentinel sees where a port listens, not what
a firewall lets through (the Security module sees only whether a host firewall is active). The Ports page's hint says why it matters (Solr has no login: anyone who can reach the
port can read and change the search index) and what to do: make Solr listen on localhost only, or
block the port in the firewall and then accept it by changing the rule to `expect: open` in
`config/sentinel.yaml` and restarting Sentinel ([FAQ](FAQ.md#reading-the-dashboard)).

**Cores that failed to load.** A core Solr could not load is listed only under `initFailures`
in `admin/cores?action=STATUS`. Its rung is critical with the reason, "failed to load: Could not
load conf for core search: …" (the first line, without Java class names), it is neither pinged
nor searched (which would only add errors to Solr's log), the search rung reads "core search
failed to load", and the admin rung says "5 cores, 1 failed to load". The usual cause on DSpace 9
is **Solr 9.8 or later**, which ignores the `<lib/>` directives DSpace's `search`, `qaevent` and
`suggestion` cores use: the rung then adds the hint 'start Solr with
SOLR_OPTS="-Dsolr.config.lib.enabled=true" (Solr 9.8+)', as DSpace's installation guide says.

**Cores whose reload failed.** Solr also lists a core under `initFailures` when a RELOAD fails: a
schema or `solrconfig.xml` copied in during an upgrade that does not load, then **Reload** in
Solr's admin UI or `action=RELOAD`. It keeps the old core serving, so STATUS lists the core under
`status` too. Sentinel pings and searches such a core as usual; its rung is a warning, "reload
failed: Unable to reload core [search]: … (still serving; a restart would not load it)", and the
admin rung counts it apart: "6 cores, 1 failed to reload". The next Solr restart would load the
new configuration and fail, so fix the configuration before restarting Solr. The metrics keep the
two apart as well: `init_failures` holds the cores that did not load, `reload_failures` the ones
whose reload failed.

**Optional cores.** Every version needs `search`, `statistics`, `oai` and `authority` (the
default `solr.cores`). DSpace 8 added `qaevent` and `suggestion`, and DSpace 10 added `audit`;
they may be absent (their features are off by default). When Solr's STATUS lists one of them and
it is not in `solr.cores`, it gets a rung of its own, `solr.core.qaevent`, marked optional (a
problem with it is at most a warning). The DSpace version, once the backend module has read it,
limits which ones apply. Yearly statistics shards (`statistics-2025`) never get a rung.

**Prefixes.** `solr.multicorePrefix` in `local.cfg` prefixes every core name. Discovery reads
it, suggests `solr.cores: [dspace_search, dspace_statistics, dspace_oai, dspace_authority]` for
`dspace_`, and says "Solr core names start with dspace_ (solr.multicorePrefix)". The optional
cores and the search query follow the same prefix (`dspace_search/select`).

**In the UI.** The Solr page's cores table tags the optional cores "optional" and shows a core
that failed to load as "Failed", with "search failed to load: …" and, for the Solr 9.8+ case,
"Fix: start Solr with SOLR_OPTS=…" under the table. A core whose reload failed keeps its own
status in the table, and the note under it reads "search failed to reload: …" with "Still serving
with its previous configuration; a restart would not load it.", or, when its ping fails as well
(the row is critical, the rung reads "HTTP 500"), "A restart would not load it either.". The
subtitle counts the cores pinged every check: "5 cores pinged every check · 1 failed to load", "6
cores pinged every check · 1 failed to reload"; the failed reload of a core that is not in the
table (another application's core on the same Solr) gets a plain note and is not counted. On the
Solr page, as on every component's page, a rung's hint (the `<lib/>` fix) also shows as a muted
line under its message in the ladder.

## Logs: names, rotation and formats

Every DSpace from 7.0 to 10.1 writes `[dspace]/log/dspace.log` and rotates it daily to
`dspace.log-YYYY-MM-DD` (with a hyphen), keeping 30 files; DSpace 6 wrote
`dspace.log.YYYY-MM-DD` (with a dot) directly. With `daily_suffix: true`, the default for the
`dspace` entry, Sentinel reads today's dotted file when it exists (DSpace 6) and otherwise the
most recently written of `dspace.log` and its dated files, in either spelling, with
`dspace.log` winning a tie. Nothing needs to change in an existing configuration.

From 7.6.3 and 8.1, command-line scripts and cron jobs (`index-discovery`, `filter-media`, the
sitemaps) log to `[dspace]/log/dspace-cli.log` instead of `dspace.log`. Discovery adds that file
as a log entry of its own when it exists, "DSpace scripts log to /dspace/log/dspace-cli.log:
added as the DSpace CLI log" (`{id: dspace-cli, label: DSpace CLI, daily_suffix: true}`); it is
not in the defaults.

A Solr installed as a service logs to `/var/solr/logs/solr.log`, one started by hand to the
folder its `-Dsolr.log.dir` names (discovery reads it from the running Solr's command line); both
write UTC, which Sentinel detects by itself. The Tomcat log is [above](#the-tomcat-log). The
formats Sentinel reads are DSpace's log4j2 lines, Solr's, Tomcat's JULI and rsyslog
`catalina.out`, and nginx's `error.log`.

## Discovery: units, logs and versions

**Detect automatically** and `./sentinel.sh discover` read `[dspace]/config/local.cfg`, then
`dspace.cfg`, then `modules/assetstore.cfg` (an earlier file wins), because DSpace 7–10 keep
the default `assetstore.dir` in `modules/assetstore.cfg`. An S3 assetstore
(`assetstore.index.primary = 1`) has no local directory to watch, so the Assetstore module stays
off with a finding. A site configured through environment variables (`dspace__P__dir`, as the
official Docker images do) may not have its real values in `local.cfg`: check the suggestion
before applying it.

It asks systemd about `postgresql`, `postgresql@*`, `solr`, the backend units listed
[above](#backend-runtimes-tomcat9-tomcat10-or-the-runnable-jar), `dspace-ui`, `dspace-angular`,
`dspace-frontend`, `pm2-*`, any other unit with `dspace` in its name, `nginx` and `apache2`
(Sentinel's own `dspace-sentinel` unit is left out), then suggests the Tomcat log to match and
the `dspace-cli.log` entry as described above.

On a server set up by hand, Solr (`bin/solr start`) or the UI (`node`, or PM2 without `pm2
startup`) may run without a systemd unit. When systemd is there and the port answers but no unit
exists, discovery leaves the service name empty, so the ladder has no "unit not found" rung, and
says so under *What we found*: "Solr answers on 8983 but no systemd unit was found for it: it may
not start by itself after a reboot" (the same for the DSpace UI on 4000, and "A web server answers
on 80 but no nginx or apache2 unit runs: Sentinel checks only whether it answers"). It suggests the
nginx log only when a web server was found or `/var/log/nginx/error.log` is there, and the Solr log
only where it is. Without any web server (DSpace opened directly on :4000) the first-run wizard
leaves the nginx and SSL modules off.

Where a port, a unit or DSpace's configuration shows a backend or Solr, discovery also asks them
what they are: one anonymous `GET <server_url>/api` and one `GET <solr>/admin/info/system`, with
a 3 s timeout (the same requests the checks send every cycle). The answers become findings such
as "DSpace 9.4 answers at http://localhost:8080/server" (with "(DSpace-CRIS cris-2024.02.04)"
after the version on DSpace-CRIS), "Solr 9.6.1 answers at http://localhost:8983/solr", the
end-of-life notice for 7.x and a Solr mismatch. URLs are shown without credentials. In demo mode
discovery describes the demo stack and asks nothing.

## DSpace 10

DSpace 10.0 (May 2026) and 10.1 (September 2026) are handled: the version parser accepts them,
the REST root's extra `uniqueType` field changes nothing, the `audit` core is optional, 10.0 is in
the SEO DOWN window and the 9.3+ Host rule applies. DSpace 10 needs Java 21, which does not
matter to Sentinel. The documentation claims 7 (end-of-life), 8 and 9 until DSpace 10 has been
tried on real installations.

## Sources

- DSpace 7.6.7 release notes ("the final release of the 7.x.x platform … the supported versions
  now shift to 8.x, 9.x and 10.x"): <https://github.com/DSpace/DSpace/releases/tag/dspace-7.6.7>
- DSpace Software Support Policy:
  <https://wiki.lyrasis.org/spaces/DSPACE/pages/34655626/DSpace+Software+Support+Policy>
- Installing DSpace ([7.x](https://wiki.lyrasis.org/spaces/DSDOC7x/pages/183076533/Installing+DSpace),
  [8.x](https://wiki.lyrasis.org/spaces/DSDOC8x/pages/315720599/Installing+DSpace),
  [9.x](https://wiki.lyrasis.org/spaces/DSDOC9x/pages/379125837/Installing+DSpace),
  [10.x](https://wiki.lyrasis.org/spaces/DSDOC10x/pages/408944708/Installing+DSpace)): Java,
  Tomcat or the runnable JAR, Solr 8 or 9 and the Solr 9.8+ `SOLR_OPTS` flag, PostgreSQL, PM2.
- The actuator's statuses (`UP_WITH_ISSUES`, `DOWN`, `OUT_OF_SERVICE`) and who may see its
  components: <https://github.com/DSpace/RestContract/blob/dspace-9_x/actuator.md>
- The official Docker image runs the backend as the runnable JAR:
  <https://github.com/DSpace/DSpace/blob/dspace-9.4/Dockerfile>
