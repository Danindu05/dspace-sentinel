# Deploying with Ansible

`deploy/ansible/` contains a role, `dspace_sentinel`, and a playbook that install Sentinel on one
or many DSpace servers exactly the way `sentinel.sh install --service` does by hand: system
packages, an unprivileged user, a git checkout, a venv, `config/sentinel.yaml` rendered from
your variables, an optional `config/sentinel.env` for secrets and a hardened systemd unit. It
sends alerts through up to five Telegram bots (one per person) and by email (Gmail or any SMTP
server) when you configure them. The role is idempotent; CI runs `ansible-lint --profile
production` and `ansible-playbook --syntax-check` on it, and both must pass.

The variable names below are the role's defaults; the file
[`deploy/ansible/roles/dspace_sentinel/defaults/main.yml`](../deploy/ansible/roles/dspace_sentinel/defaults/main.yml)
is authoritative if the two ever disagree.

## Requirements

- Ansible 2.15+ (ansible-core) on your workstation (`pipx install ansible`); the role's
  `meta/main.yml` declares `min_ansible_version: "2.15"`.
- Target: Ubuntu 22.04/24.04 or Debian 12 with `python3` (3.10+), SSH access with sudo. Other
  distributions work with `dspace_sentinel_manage_apt: false` and the packages installed by hand
  (`git`, `python3`, `python3-venv` and `acl`).
- `ansible-galaxy collection install -r deploy/ansible/requirements.yml` for `community.general`,
  which only the optional nginx snippet uses (its htpasswd file).

## Quick start

```bash
cd deploy/ansible
cp inventory.example.ini inventory.ini
```

```ini
[dspace_servers]
repo-1.example.org ansible_user=admin
repo-2.example.org ansible_user=admin
```

Put your settings in `group_vars/dspace_servers/main.yml` (see the example below) and the
secrets in `group_vars/dspace_servers/vault.yml`, then run:

```bash
ansible-playbook -i inventory.ini playbook.yml
```

The playbook targets the `dspace_servers` group and applies the role. Afterwards Sentinel runs
as `dspace-sentinel.service` on `127.0.0.1:9765` of each host; use an SSH tunnel or the nginx
snippet below to reach it, and sign in with the dashboard login you gave the role
([below](#the-dashboard-login)). The role never asks for a password: without
`dspace_sentinel_login_password_hash` the dashboard has no login.

## Variables

Set them in `group_vars/dspace_servers/main.yml`, `host_vars/<host>.yml` or the playbook, and
keep the vaulted secrets next to them in `group_vars/dspace_servers/vault.yml`. Use that
directory layout only: when a `group_vars/dspace_servers/` directory exists, Ansible reads the
files inside it and silently ignores a `group_vars/dspace_servers.yml` file next to it, so the
play would succeed with the role defaults (branch `main`, Telegram off, the `dspace` database
user). `ansible-inventory -i inventory.ini --host <host>` shows what a host really gets.

Every section of `config/sentinel.yaml` has a role variable with the same shape (a dictionary
where the YAML has one). Secrets, the Telegram bots and the email recipients have variables of
their own, so a host can override one of them without copying the rest. **Dictionaries and lists
are replaced as a whole when overridden**, so copy the default block from `defaults/main.yml`
into your `group_vars` and edit it there.

### Installation

| Variable | Default | Meaning |
|---|---|---|
| `dspace_sentinel_repo` | `https://github.com/Danindu05/dspace-sentinel.git` | git remote |
| `dspace_sentinel_version` | `main` | branch, tag or commit to check out; pin a release tag such as `v0.1.0` once releases are published |
| `dspace_sentinel_install_dir` | `/opt/dspace-sentinel` | checkout location; `.venv/`, `data/` and `config/` live inside |
| `dspace_sentinel_user` / `dspace_sentinel_group` | `sentinel` | system account that owns and runs it (`nologin`) |
| `dspace_sentinel_extra_groups` | `[]` | supplementary groups, e.g. `[adm]` to read `/var/log`, or the `dspace` group for `/dspace/log` |
| `dspace_sentinel_python` | `python3` | interpreter that creates the venv (3.10+) |
| `dspace_sentinel_apt_packages` | `[git, python3, python3-venv, acl]` | installed with `apt` on Debian-family hosts; `acl` lets Ansible become the unprivileged service user |
| `dspace_sentinel_manage_apt` | `true` | `false` on non-Debian hosts or when packages come from elsewhere |
| `dspace_sentinel_manage_config` | `true` | `false` = write `config/sentinel.yaml` only when it is missing; the web UI then owns it and Ansible never overwrites it |
| `dspace_sentinel_service_enabled` | `true` | enable the unit at boot |
| `dspace_sentinel_service_state` | `started` | `started` or `stopped` |
| `dspace_sentinel_no_log` | `true` | hide the rendered config and `sentinel.env` (they may contain secrets) from the play output |
| `dspace_sentinel_env` | `{}` | vault it. Extra environment written to `config/sentinel.env` (mode 0600, owned by the service user, values double-quoted and escaped) when not empty; the unit and every `./sentinel.sh` command read it, and a change restarts the unit. Example: `{SENTINEL_SMTP_PASSWORD: "{{ vault_sentinel_gmail_app_password }}"}` (see [Telegram and email](#telegram-and-email)). Emptying it later leaves the old file in place; remove it by hand |

### Server and onboarding

| Variable | Default | Meaning |
|---|---|---|
| `dspace_sentinel_host` | `127.0.0.1` | `server.host` (`0.0.0.0` exposes Sentinel directly; prefer the nginx snippet). A non-loopback address needs `dspace_sentinel_login_password_hash`, `dspace_sentinel_auth_token` or `dspace_sentinel_allow_unauthenticated: true` (or `SENTINEL_AUTH_TOKEN` / `SENTINEL_ALLOW_OPEN: "1"` in `dspace_sentinel_env`); the role's checks refuse it otherwise, before anything changes, because Sentinel would refuse to start |
| `dspace_sentinel_port` | `9765` | `server.port` |
| `dspace_sentinel_auth_token` | `""` | `server.auth_token` (vault it): a token for scripts (`Authorization: Bearer <token>`, or HTTP Basic with any user name); without a login the dashboard itself asks for it (the browser's Basic pop-up) |
| `dspace_sentinel_login_user` | `admin` | `server.login.username`: the dashboard login's username, 1 to 64 letters, digits and `. - _ @` |
| `dspace_sentinel_login_password_hash` | `""` | `server.login.password_hash` (vault it): the login's password hash, made with `./sentinel.sh hash-password`; `""` = no login. With it the web UI asks people to sign in and a non-loopback `dspace_sentinel_host` may listen |
| `dspace_sentinel_hostname` | `""` | label in the UI and alerts; `""` = the machine's hostname |
| `dspace_sentinel_allowed_hosts` | `[]` | `server.allowed_hosts`: extra names Sentinel answers to in the `Host` header (see [CONFIG.md](CONFIG.md#server)). While `dspace_sentinel_nginx_snippet_enabled` is true the role adds `inventory_hostname` itself; list any other name the repository is reached under here |
| `dspace_sentinel_allow_unauthenticated` | `false` | `server.allow_unauthenticated`: `true` serves a non-loopback `dspace_sentinel_host` without a login or `dspace_sentinel_auth_token`; only when a firewall already guards the port |
| `dspace_sentinel_setup_completed` | `true` | managed hosts skip the first-run wizard |
| `dspace_sentinel_persona` | `full` | `full`, `backend`, `frontend`, `server` or `""` (informational) |
| `dspace_sentinel_theme` | `system` | `appearance.theme`: `system`, `light` or `dark` |

The role has no variable for `server.timezone` (the zone of the times in alerts, emails and
reports, [CONFIG.md](CONFIG.md#the-time-zone-of-messages-and-schedules)) and leaves it empty, so
those times use the server's own zone. A zone chosen in **More › Alert thresholds** lasts until the
role writes the file again while `dspace_sentinel_manage_config` is `true`.

#### The dashboard login

The role renders `dspace_sentinel_login_user` and `dspace_sentinel_login_password_hash` into
`server.login` ([CONFIG.md](CONFIG.md#the-dashboard-login-serverlogin)). It takes the **hash**,
never the password: make it once on any machine where Sentinel is installed (on a host the role
already set up: `sudo -u sentinel /opt/dspace-sentinel/sentinel.sh hash-password`), which asks
for the password twice without echo and prints only a line such as
`scrypt$16384$8$1$<salt>$<hash>`, and put that line in the vault:

```yaml
# group_vars/dspace_servers/main.yml
dspace_sentinel_login_user: admin
dspace_sentinel_login_password_hash: "{{ vault_sentinel_login_hash }}"
```

```yaml
# group_vars/dspace_servers/vault.yml (shown decrypted)
vault_sentinel_login_hash: "scrypt$16384$8$1$…$…"     # ./sentinel.sh hash-password
```

Before it changes anything, `tasks/checks.yml` stops the play when the login user breaks the
username rules or the hash variable holds something other than a hash ("dspace_sentinel_login_user
is 1 to 64 letters, digits and . - _ @, and dspace_sentinel_login_password_hash is a hash made with
./sentinel.sh hash-password (it starts with scrypt$ or pbkdf2_sha256$), never the password itself.
Vault the hash."), and when `dspace_sentinel_host` is reachable from other machines with neither
a login, a token nor `dspace_sentinel_allow_unauthenticated: true` ("dspace_sentinel_host is
reachable from other machines, so Sentinel refuses to start without a login
(dspace_sentinel_login_password_hash, made with ./sentinel.sh hash-password), a token
(dspace_sentinel_auth_token) or dspace_sentinel_allow_unauthenticated: true. Or keep 127.0.0.1 and
publish Sentinel through the nginx snippet.").

With `dspace_sentinel_manage_config: true` (the default) the role rewrites `server.login` on every
run, so a password changed on the server (**More › Account** or `./sentinel.sh set-password`)
lasts only until the next run, and a login created there without the variable disappears: put the
hash in the vault. A run that writes a different hash
signs every browser out within seconds; one that leaves it unchanged signs nobody out.

### Modules and collector

| Variable | Default | Meaning |
|---|---|---|
| `dspace_sentinel_modules` | every module `true` | `modules.*` — the role switches the full stack on, `security: true` included (read-only: open ports, firewall, SSH, updates, software support); the app's own default is everything off. On a host without a web server (DSpace opened directly on :4000) set `nginx: false` and `ssl: false`, or the nginx check is critical from the first minute; the variable replaces the whole list, so name every module |
| `dspace_sentinel_collector` | `{system_interval: 15, service_interval: 30, http_interval: 30, db_interval: 60, logs_interval: 60, repository_interval: 900, security_interval: 900, retention_days: 30}` | `collector.*`; `security_interval` is 60 … 86400 s |

An inventory that copied `dspace_sentinel_modules` or `dspace_sentinel_collector` before the
Security module existed keeps that module off, because a copied block replaces the role's
default as a whole: add `security: true` to its modules (and `security_interval: 900` to its
collector block, or leave it out to take Sentinel's default). The module only reads; what it
looks at is listed in [SECURITY.md](../SECURITY.md#what-the-security-module-reads); a file
Sentinel's user may not read makes that check "not available", never a reason to give the
service user more rights.

### DSpace and services

| Variable | Default | Meaning |
|---|---|---|
| `dspace_sentinel_dspace` | `{install_dir: /dspace, auto_discover: true, server_url: http://localhost:8080/server, ui_url: http://localhost:4000, public_url: "", assetstore_dir: /dspace/assetstore, log_dir: /dspace/log, backend_service: tomcat9, frontend_service: dspace-ui, verify_tls: true}` | `dspace.*`; a service name of `""` skips that systemd rung. For DSpace 8 and 9 under Tomcat 10 set `backend_service: tomcat10` (or the unit of the runnable JAR) and point the Tomcat entry of `dspace_sentinel_logs` at `/var/log/tomcat10/catalina.out` ([COMPATIBILITY.md](COMPATIBILITY.md#backend-runtimes-tomcat9-tomcat10-or-the-runnable-jar)). Sentinel also answers to the host of `public_url`, which covers the nginx snippet when the repository's name differs from the inventory name; on DSpace 9.3+ `public_url` also lets the UI render on the server for Sentinel's check |
| `dspace_sentinel_postgres` | `{host: localhost, port: 5432, database: dspace, user: dspace, sslmode: prefer, service: postgresql}` | `postgres.*` (without the password) |
| `dspace_sentinel_postgres_password` | `""` | vault it; a read-only role is enough, Sentinel only `SELECT`s |
| `dspace_sentinel_solr` | `{url: http://localhost:8983/solr, cores: [search, statistics, oai, authority], service: solr}` | `solr.*` |
| `dspace_sentinel_nginx` | `{service: nginx, http_url: http://localhost, https_url: ""}` | `nginx.*` |
| `dspace_sentinel_ssl` | `{host: "", port: 443}` | `ssl.*` (`host: ""` = the host of `dspace.public_url`) |
| `dspace_sentinel_ports` | the default list from [CONFIG.md](CONFIG.md#ports) | `ports` (`expect: open` or `local_only`) |
| `dspace_sentinel_logs` | `{files: [dspace, tomcat, solr, nginx …], window_minutes: 5}` | `logs.files` (each entry may also set `timezone`, see [CONFIG.md](CONFIG.md#logs)) and `logs.window_minutes` |

### Thresholds and reports

| Variable | Default | Meaning |
|---|---|---|
| `dspace_sentinel_alerts` | the defaults from [CONFIG.md](CONFIG.md#alerts) (`confirm_samples`, `clear_samples`, `reminder_minutes` and every threshold pair) | `alerts.*` — the whole block; copy it and change the pairs you need |
| `dspace_sentinel_reports` | `{weekly: {enabled: false, weekday: 1, hour: 8, send_telegram: false, send_email: false}, default_sections: [summary, server, services, latency, alerts, repository, security, recommendations]}` | `reports.*`; `send_telegram` / `send_email` send the weekly report through every Telegram bot / to every email recipient |

### Telegram and email

| Variable | Default | Meaning |
|---|---|---|
| `dspace_sentinel_telegram` | `{enabled: false, quiet_hours: "", daily_digest: ""}` | `notifiers.telegram` without the bots: the switch, plus the quiet hours and the daily digest shared by every bot |
| `dspace_sentinel_telegram_bots` | `[]` | up to 5 bots, one per person, each with its own token and chat (keys below); written as `notifiers.telegram.chats` |
| `dspace_sentinel_telegram_bot_token` | `""` | the single bot of older inventories (vault it): with `dspace_sentinel_telegram.chat_id` it becomes one bot, see [below](#inventories-written-for-a-single-bot). Leave both empty once you use `dspace_sentinel_telegram_bots` |
| `dspace_sentinel_email` | `{enabled: false, smtp_host: smtp.gmail.com, smtp_port: 587, security: starttls, username: "", from_address: "", from_name: DSpace Sentinel, quiet_hours: "", daily_digest: ""}` | `notifiers.email` without the recipients and the password. `security`: `starttls` (Gmail on 587), `ssl` (Gmail on 465) or `none` (no TLS: only for a relay that needs no login, or one on the same host). `username` is the Gmail address Sentinel logs in with; `from_address: ""` sends from it. A key you leave out takes Sentinel's default, which is the value shown here |
| `dspace_sentinel_email_recipients` | `[]` | up to 5 recipients (keys below); written as `notifiers.email.recipients`. Each alert goes out as one email to every recipient it concerns |
| `dspace_sentinel_email_password` | `""` | vault it: the Gmail **app password** (Google Account › Security › 2-Step Verification › App passwords; it needs 2-Step Verification). Google's normal account password does not work. Pasting it in Google's four groups (`abcd efgh ijkl mnop`) is fine: Sentinel drops the spaces |
| `dspace_sentinel_webhook` | `{enabled: false, url: ""}` | `notifiers.webhook.*` (the URL is a secret: vault it) |

Each entry of `dspace_sentinel_telegram_bots`:

| Key | Default | Meaning |
|---|---|---|
| `label` | `""` | the person or team, shown in the web UI and in delivery results (up to 40 characters) |
| `bot_token` | `""` | this person's bot token from @BotFather: vault it, as a `"{{ vault_… }}"` reference or an inline `!vault` value |
| `chat_id` | `""` | where the bot writes: a person's id (`"123456789"`), a group's (`"-1001234567890"`) or `"@channelname"`. The person has to send the bot a message first ([NOTIFICATIONS.md](NOTIFICATIONS.md#2-find-the-chat-id) shows how to find the id) |
| `min_severity` | `warning` | `warning` = every alert; `critical` = only what is critical (critical alerts with their reminders and resolutions, and critical incidents). Test messages, the daily digest and reports reach every bot |
| `id` | derived | optional. Sentinel derives an 8-character id from the bot's position and chat id, so it stays the same from run to run as long as the bot keeps its place and chat. An id you set (letters, digits, `-` and `_`, up to 32; not `env`, which is reserved for the environment's bot) is kept. `./sentinel.sh telegram-test --to` takes the id or the chat id |

Each entry of `dspace_sentinel_email_recipients`: `address` (required, one plain address such as
`ops@example.org`), `label` (optional, up to 40 characters) and `min_severity` (`warning`, the
default, or `critical`, as for the bots).

`quiet_hours` (such as `"23:00-07:00"`) holds back warnings in that window (critical alerts still
go out) and `daily_digest` (such as `"08:00"`) sends one summary a day, for each channel on its
own. **Quote the times**: YAML reads an unquoted `18:00` as the number 1080.

**Checks.** Before it changes anything, the role (`tasks/checks.yml`) stops the play, naming the
variables, when:

- there are more than 5 bots or more than 5 recipients;
- an entry is not a mapping, or has a key other than those above or a `min_severity` other than
  `warning` or `critical`; or a recipient has no `address`. Sentinel ignores keys it does not
  know, so a misspelt `bot_token` would otherwise leave that bot silent without a word;
- a quiet-hours or digest time is not quoted;
- the old single bot is set next to `dspace_sentinel_telegram_bots`;
- bots, recipients or the app password sit inside `dspace_sentinel_telegram` or
  `dspace_sentinel_email` instead of their own variables (the empty `chats: []`, `password: ""`
  and `recipients: []` of a section copied from `config/sentinel.example.yaml` are fine).

Sentinel checks the values themselves (token and chat id formats, addresses) when it starts; a
quiet-hours or digest time it cannot read, or an email login that is not an address without
`from_address`, does not stop it but is logged and written to its events log at every start; see
[CONFIG.md](CONFIG.md#times-and-the-email-login-in-the-file).

**Secrets.** Every bot token and the app password come from the vault (see the
[example](#example-group_varsdspace_serversmainyml)); inline `!vault` values from
`ansible-vault encrypt_string` work too, on every supported Ansible version. The app password
follows the role's pattern for secrets: a variable of its own, rendered into
`config/sentinel.yaml` like `dspace_sentinel_postgres_password` and `dspace_sentinel_auth_token`.
That file is as private as `config/sentinel.env` (mode 0600, owned by the service user, written
with `no_log`), and it keeps the whole email set-up in the one place the web UI edits. To keep the
password out of `config/sentinel.yaml` anyway, leave `dspace_sentinel_email_password` empty and set
`SENTINEL_SMTP_PASSWORD` in `dspace_sentinel_env`. More › Email then shows the password read-only,
and the SMTP server, port, security and username it goes to can only be changed in the inventory
(`dspace_sentinel_email`), which Sentinel's API refuses to re-point. With
`dspace_sentinel_manage_config: false` the config is written only once, so a new app password set
in the inventory later arrives only through that environment variable (`config/sentinel.env` is
rewritten on every run).

#### Inventories written for a single bot

The single-bot variables still work. With `dspace_sentinel_telegram_bots` empty, the role writes
`dspace_sentinel_telegram.chat_id` and `dspace_sentinel_telegram_bot_token` as one bot
(`label: ""`, `min_severity: warning`), so an existing inventory keeps alerting after the update
without changes. To add a second person, move that bot into the list and empty the old values.
The role refuses both at once, so the old bot is never dropped without a word:

```yaml
# before
dspace_sentinel_telegram: {enabled: true, chat_id: "-1001234567890", quiet_hours: "", daily_digest: ""}
dspace_sentinel_telegram_bot_token: "{{ vault_sentinel_telegram_token }}"
# after
dspace_sentinel_telegram: {enabled: true, quiet_hours: "", daily_digest: ""}
dspace_sentinel_telegram_bots:
  - {label: Team, bot_token: "{{ vault_sentinel_telegram_token }}", chat_id: "-1001234567890"}
  - {label: Danindu, bot_token: "{{ vault_sentinel_bot_danindu }}", chat_id: "123456789"}
```

Because the role writes that bot as an entry of `notifiers.telegram.chats`,
`SENTINEL_TELEGRAM_TOKEN` and `SENTINEL_TELEGRAM_CHAT_ID` in `dspace_sentinel_env` follow
Sentinel's current rules:

- `SENTINEL_TELEGRAM_TOKEN` fills in the token of every bot listed without one, so the old "chat
  id in the inventory, token in `config/sentinel.env`" set-up keeps working;
- both together add one more bot, "From environment" (read-only in the web UI; it counts toward
  the 5), unless a listed bot already has that token and chat id;
- neither replaces a value the inventory sets, and `SENTINEL_TELEGRAM_CHAT_ID` alone does
  nothing, so put the chat id in the inventory.

### Optional nginx snippet

| Variable | Default | Meaning |
|---|---|---|
| `dspace_sentinel_nginx_snippet_enabled` | `false` | write the `location /sentinel/` snippet from [REVERSE-PROXY.md](REVERSE-PROXY.md) and an htpasswd file |
| `dspace_sentinel_nginx_snippet_path` | `/etc/nginx/snippets/dspace-sentinel.conf` | where the snippet goes |
| `dspace_sentinel_nginx_location` | `/sentinel/` | path prefix (must end with `/`) |
| `dspace_sentinel_nginx_htpasswd_path` | `/etc/nginx/.dspace-sentinel-htpasswd` | password file the snippet references |
| `dspace_sentinel_nginx_htpasswd_user` | `sentinel` | basic-auth user name |
| `dspace_sentinel_nginx_htpasswd_password` | `""` | vault it; required when the snippet is enabled (the role refuses to continue without it) |
| `dspace_sentinel_nginx_htpasswd_group` | `www-data` | group of the password file (mode 0640) |
| `dspace_sentinel_nginx_reload` | `true` | reload nginx after changing the snippet or password |

You still `include /etc/nginx/snippets/dspace-sentinel.conf;` in your site's `server` block; the
role does not edit your site configuration. The snippet always has nginx's basic auth, so with the
dashboard login set people type the htpasswd password first and then sign in to Sentinel; serve
the snippet from your HTTPS server block so neither password travels in plain text. The password file is managed with
`community.general.htpasswd`, which needs `python3-passlib` on the host (installed by the role on
Debian/Ubuntu). The snippet forwards the host name the repository is reached under, and the
role adds `inventory_hostname` to `server.allowed_hosts` while the snippet is enabled. If the
repository answers under another name, set `dspace_sentinel_dspace.public_url` or list the name
in `dspace_sentinel_allowed_hosts`; otherwise Sentinel answers HTTP 400 `unknown host`.

## Example `group_vars/dspace_servers/main.yml`

```yaml
dspace_sentinel_version: main          # pin a release tag such as v0.1.0 once releases are published
dspace_sentinel_hostname: "{{ inventory_hostname_short }}"
dspace_sentinel_persona: full
dspace_sentinel_extra_groups: [adm]
dspace_sentinel_dspace:
  install_dir: /dspace
  auto_discover: true
  server_url: http://localhost:8080/server
  ui_url: http://localhost:4000
  public_url: https://repo.example.org
  assetstore_dir: /dspace/assetstore
  log_dir: /dspace/log
  backend_service: tomcat10
  frontend_service: dspace-ui
  verify_tls: true
dspace_sentinel_postgres:
  host: 127.0.0.1
  port: 5432
  database: dspace
  user: sentinel_ro            # a read-only role
  sslmode: prefer
  service: postgresql
dspace_sentinel_postgres_password: "{{ vault_sentinel_pg_password }}"
dspace_sentinel_alerts:
  confirm_samples: 2
  clear_samples: 3
  reminder_minutes: 30
  cpu: {warning: 80, critical: 95}
  memory: {warning: 80, critical: 92}
  swap: {warning: 50, critical: 80}
  disk: {warning: 85, critical: 95}
  load_per_core: {warning: 1.5, critical: 3.0}
  rest_latency_ms: {warning: 1500, critical: 4000}
  search_latency_ms: {warning: 1500, critical: 4000}
  home_latency_ms: {warning: 2000, critical: 5000}
  postgres_connections_pct: {warning: 80, critical: 95}
  ssl_expiry_days: {warning: 30, critical: 7}
  log_errors_per_window: {warning: 20, critical: 100}
  assetstore_free_gb: {warning: 50, critical: 10}
dspace_sentinel_telegram:
  enabled: true
  quiet_hours: "23:00-07:00"
  daily_digest: "08:00"
dspace_sentinel_telegram_bots:          # one bot per person, up to 5
  - label: Danindu
    bot_token: "{{ vault_sentinel_bot_danindu }}"
    chat_id: "123456789"
  - label: Repository team
    bot_token: "{{ vault_sentinel_bot_team }}"
    chat_id: "-1001234567890"
    min_severity: critical              # critical alerts and incidents only
dspace_sentinel_email:
  enabled: true
  smtp_host: smtp.gmail.com
  smtp_port: 587
  security: starttls
  username: sentinel.alerts@gmail.com   # the Gmail account Sentinel sends from
  from_address: ""
  from_name: DSpace Sentinel
  quiet_hours: "23:00-07:00"
  daily_digest: "08:00"
dspace_sentinel_email_recipients:       # up to 5
  - {address: ops@example.org, label: On call}
  - {address: director@example.org, label: Director, min_severity: critical}
dspace_sentinel_email_password: "{{ vault_sentinel_gmail_app_password }}"
dspace_sentinel_reports:
  weekly: {enabled: true, weekday: 1, hour: 8, send_telegram: true, send_email: true}
  default_sections: [summary, server, services, latency, alerts, incidents, repository, security, recommendations]
dspace_sentinel_login_user: admin
dspace_sentinel_login_password_hash: "{{ vault_sentinel_login_hash }}"   # ./sentinel.sh hash-password
dspace_sentinel_nginx_snippet_enabled: true
dspace_sentinel_nginx_htpasswd_user: admin
dspace_sentinel_nginx_htpasswd_password: "{{ vault_sentinel_nginx_password }}"
```

The vaulted values live next to it, in the same directory:

```bash
ansible-vault create group_vars/dspace_servers/vault.yml     # vault_sentinel_pg_password, vault_sentinel_bot_danindu, vault_sentinel_bot_team, vault_sentinel_gmail_app_password, vault_sentinel_login_hash, vault_sentinel_nginx_password
ansible-playbook -i inventory.ini playbook.yml --ask-vault-pass
```

```yaml
# group_vars/dspace_servers/vault.yml (shown decrypted)
vault_sentinel_pg_password: "…"
vault_sentinel_bot_danindu: "123456789:AA…"     # each person's token from @BotFather
vault_sentinel_bot_team: "987654321:BB…"
vault_sentinel_gmail_app_password: "abcd efgh ijkl mnop"
vault_sentinel_login_hash: "scrypt$16384$8$1$…$…"   # ./sentinel.sh hash-password, never the password
vault_sentinel_nginx_password: "…"
```

To keep the email app password out of `config/sentinel.yaml` as well, leave
`dspace_sentinel_email_password` empty and pass it as an environment variable instead:

```yaml
dspace_sentinel_env:
  SENTINEL_SMTP_PASSWORD: "{{ vault_sentinel_gmail_app_password }}"
```

Bot tokens have no per-bot variable: `SENTINEL_TELEGRAM_TOKEN` + `SENTINEL_TELEGRAM_CHAT_ID` add a
single extra bot (see [Inventories written for a single bot](#inventories-written-for-a-single-bot)).

## What the role does

1. Checks the Telegram, email and login variables and the bind address (`tasks/checks.yml`, see
   [Telegram and email](#telegram-and-email) and [the dashboard login](#the-dashboard-login)) and
   stops before changing anything when one is malformed or Sentinel would refuse to start.
2. `apt`: `git`, `python3`, `python3-venv`, `acl` (Debian family, unless `dspace_sentinel_manage_apt: false`).
3. Creates the system user and group; adds the supplementary groups for log access.
4. Clones or updates the repository at `dspace_sentinel_version` (`git` module, `force: false`,
   so local `data/` and `config/` are untouched).
5. Creates the venv and installs `requirements.txt` (`pip` module, only when the requirements
   changed).
6. Creates `data/`, `data/reports/` (mode 0700) and `config/` (0750), then renders
   `config/sentinel.yaml` (mode 0600, `no_log`) from the variables — secrets included, which is
   why the file is private to the service user. With `dspace_sentinel_manage_config: false` the
   file is written once and then left to the web UI. When `dspace_sentinel_env` is not empty it
   also writes `config/sentinel.env` (mode 0600, `no_log`).
7. Installs `/etc/systemd/system/dspace-sentinel.service` from the template (the hardening and
   resource limits of `deploy/systemd/dspace-sentinel.service`; `SENTINEL_DATA_DIR` and
   `SENTINEL_CONFIG` point into the install directory, `EnvironmentFile=-` reads
   `config/sentinel.env`, and after five failed starts in two minutes the unit stays failed),
   `daemon-reload`, enables and starts it.
8. Handlers: reload systemd, restart Sentinel on a config, environment, unit, checkout or
   requirements change, reload nginx after the snippet changed.
9. Optionally writes the nginx snippet and the htpasswd file.

Nothing in the role touches DSpace, PostgreSQL, Solr, Tomcat or nginx's site configuration, and
it opens no firewall port: Sentinel stays on `127.0.0.1` unless you set
`dspace_sentinel_host: 0.0.0.0` on purpose.

## Day-two operations

```bash
ansible-playbook -i inventory.ini playbook.yml                  # upgrade: follows dspace_sentinel_version
ansible dspace_servers -i inventory.ini -b -m systemd -a "name=dspace-sentinel state=restarted"
ansible dspace_servers -i inventory.ini -b --become-user sentinel -a "/opt/dspace-sentinel/sentinel.sh doctor --ascii"
```

The fleet check runs as the service user: the install directory belongs to `sentinel:sentinel`
with mode 0750 and the config is 0600, so neither your SSH user nor root's working directory
would do. Becoming an unprivileged user needs `acl` on the target (the role installs it).
`sentinel.sh` works from any directory. `doctor` exits 0 (ok), 1 (warnings) or 2 (critical); 4
means the check could not run (for example an unfinished install) and 64 a usage error. Ansible
reports every non-zero exit as FAILED, so read the `rc` of each host. An unchanged playbook run
is a no-op; a changed checkout or requirements file restarts the unit through the handler.

## Removing

```bash
ansible dspace_servers -i inventory.ini -b -m systemd -a "name=dspace-sentinel state=stopped enabled=false"
ansible dspace_servers -i inventory.ini -b -m file -a "path=/etc/systemd/system/dspace-sentinel.service state=absent"
ansible dspace_servers -i inventory.ini -b -m systemd -a "daemon_reload=true"
```

Keep or remove `/opt/dspace-sentinel/data` (history and reports) as you prefer.
