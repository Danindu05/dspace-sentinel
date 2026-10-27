# Deploying DSpace Sentinel with Ansible

The `dspace_sentinel` role installs Sentinel from git into `/opt/dspace-sentinel`, creates a
system user and a virtualenv, writes `config/sentinel.yaml` (and, when you give it
`dspace_sentinel_env`, `config/sentinel.env`) from your variables, installs the hardened systemd
unit and starts it. Re-running the playbook is safe: nothing changes unless a variable, the
checked-out version or the requirements changed. Full reference: [docs/ANSIBLE.md](../../docs/ANSIBLE.md).

## Quick start

```bash
cd deploy/ansible
cp inventory.example.ini inventory.ini            # put your DSpace host(s) in [dspace_servers]
mkdir -p group_vars/dspace_servers               # main.yml (settings) + vault.yml (secrets), see below
ansible-galaxy install -r requirements.yml         # community.general (htpasswd, optional nginx snippet)
ansible-playbook -i inventory.ini playbook.yml     # installs, configures, starts; open http://localhost:9765 via SSH tunnel
```

`dspace_sentinel_version` defaults to `main`; pin a release tag such as `v0.1.0` (or a commit)
once releases are published, and change it to upgrade.

Sentinel binds to `127.0.0.1:9765`. To reach it from your workstation either tunnel
(`ssh -L 9765:127.0.0.1:9765 repo.example.org`) or enable the nginx snippet below, then sign in
with the dashboard login. The role never asks for a password: give it the login as a vaulted hash
(`dspace_sentinel_login_password_hash`, see [Secrets](#secrets)); without one the dashboard has no
login.

## Variables

Every setting in `config/sentinel.yaml` has a role variable with the same shape; see
`roles/dspace_sentinel/defaults/main.yml` for the full, commented list. The ones you will most
likely touch:

| Variable | Default | Meaning |
|---|---|---|
| `dspace_sentinel_version` | `main` | git branch, tag or commit to deploy |
| `dspace_sentinel_env` | `{}` | extra environment written to `config/sentinel.env` (0600) when not empty, e.g. `SENTINEL_SMTP_PASSWORD` (vault it) |
| `dspace_sentinel_install_dir` | `/opt/dspace-sentinel` | checkout, venv, data and config live here |
| `dspace_sentinel_user` / `_group` | `sentinel` | system account the unit runs as |
| `dspace_sentinel_extra_groups` | `[]` | e.g. `[adm]` so Sentinel may read `/var/log/*`, or the `dspace` group for `/dspace/log` |
| `dspace_sentinel_hostname` | `""` | label shown in the UI, Telegram messages and email subjects (machine hostname when empty) |
| `dspace_sentinel_persona` | `full` | `full` \| `backend` \| `frontend` \| `server` |
| `dspace_sentinel_modules` | all `true` | per-module toggles, `security` (read-only security status) included; the app's own default is everything off |
| `dspace_sentinel_dspace` | see defaults | install dir, REST/UI/public URLs, assetstore, log dir, systemd units |
| `dspace_sentinel_postgres` + `dspace_sentinel_postgres_password` | `dspace@localhost` | read-only DB access (vault the password) |
| `dspace_sentinel_alerts` | SPEC defaults | thresholds, confirm/clear samples, reminders |
| `dspace_sentinel_telegram` | off | Telegram switch, quiet hours and daily digest, shared by every bot |
| `dspace_sentinel_telegram_bots` | `[]` | up to 5 bots, one per person: `{label, bot_token, chat_id, min_severity}` (vault each token) |
| `dspace_sentinel_email` + `dspace_sentinel_email_recipients` + `dspace_sentinel_email_password` | off, Gmail's server | email alerts (Gmail or any SMTP server) to up to 5 recipients `{address, label, min_severity}`; vault the app password |
| `dspace_sentinel_reports` | weekly report off | `weekly.send_telegram` / `weekly.send_email` send it to every bot / recipient |
| `dspace_sentinel_host` | `127.0.0.1` | bind address; anything else needs `dspace_sentinel_login_password_hash`, `dspace_sentinel_auth_token` or `dspace_sentinel_allow_unauthenticated: true`, or the role's checks stop the play (Sentinel would refuse to start) |
| `dspace_sentinel_allowed_hosts` | `[]` | extra host names Sentinel answers to (the nginx snippet adds `inventory_hostname` itself) |
| `dspace_sentinel_login_user` | `admin` | the dashboard login's username (1 to 64 letters, digits and `. - _ @`) |
| `dspace_sentinel_login_password_hash` | `""` | the dashboard login's password hash, made with `./sentinel.sh hash-password` (vault it; never the password itself); `""` = no login |
| `dspace_sentinel_auth_token` | `""` | a token for scripts (`Authorization: Bearer`, or HTTP Basic with any user name); without a login the dashboard asks for it (vault it) |
| `dspace_sentinel_manage_config` | `true` | `false` = write the config once, then the web UI owns it |
| `dspace_sentinel_nginx_snippet_enabled` | `false` | drop an nginx `location /sentinel/` snippet with htpasswd |

Nested dictionaries such as `dspace_sentinel_alerts`, and lists such as the bots, are replaced
as a whole when overridden, so copy the default block into your `group_vars` and edit it there.
An inventory that copied `dspace_sentinel_modules` (or `dspace_sentinel_collector`) before the
Security module existed keeps that module off until you add `security: true` (and
`security_interval: 900`) to its copy.

Use the directory layout below (`group_vars/dspace_servers/main.yml` + `vault.yml`) and no
`group_vars/dspace_servers.yml` file: once the `group_vars/dspace_servers/` directory exists,
Ansible ignores a `dspace_servers.yml` file next to it without a warning.

### Secrets

Keep secrets out of the inventory with `ansible-vault`:

```yaml
# group_vars/dspace_servers/vault.yml  (ansible-vault create ...)
dspace_sentinel_postgres_password: "s3cret"
dspace_sentinel_login_password_hash: "scrypt$16384$8$1$…$…"   # the output of ./sentinel.sh hash-password
dspace_sentinel_auth_token: "long-random-string"
dspace_sentinel_nginx_htpasswd_password: "another-long-random-string"
vault_sentinel_bot_danindu: "123456789:AA..."        # each person's bot token from @BotFather
vault_sentinel_bot_team: "987654321:BB..."
dspace_sentinel_email_password: "abcd efgh ijkl mnop" # a Gmail app password, not the account's
# or, to keep the app password out of config/sentinel.yaml, pass it through config/sentinel.env:
# dspace_sentinel_env: {SENTINEL_SMTP_PASSWORD: "abcd efgh ijkl mnop"}
```

```yaml
# group_vars/dspace_servers/main.yml
dspace_sentinel_hostname: repo-1
dspace_sentinel_telegram:
  enabled: true
  quiet_hours: "23:00-07:00"      # quote the times
  daily_digest: "08:00"
dspace_sentinel_telegram_bots:    # up to 5, one bot per person
  - {label: Danindu, bot_token: "{{ vault_sentinel_bot_danindu }}", chat_id: "123456789"}
  - {label: Team, bot_token: "{{ vault_sentinel_bot_team }}", chat_id: "-1001234567890", min_severity: critical}
dspace_sentinel_email:
  enabled: true
  smtp_host: smtp.gmail.com
  smtp_port: 587
  security: starttls
  username: sentinel.alerts@gmail.com
  from_address: ""
  from_name: DSpace Sentinel
  quiet_hours: "23:00-07:00"
  daily_digest: ""
dspace_sentinel_email_recipients: # up to 5
  - {address: ops@example.org, label: On call}
  - {address: director@example.org, label: Director, min_severity: critical}
```

The rendered config and environment tasks run with `no_log` so tokens never appear in the play
output. An inventory written for the single bot (`dspace_sentinel_telegram.chat_id` +
`dspace_sentinel_telegram_bot_token`) keeps working as one bot; move it into
`dspace_sentinel_telegram_bots` to add more. Before changing anything the role stops on more than
5 bots or recipients, a misspelt key, an unquoted time, the old bot next to the list, a login user
outside the username rules, a password where its hash belongs, or a non-loopback
`dspace_sentinel_host` with neither a login, a token nor `dspace_sentinel_allow_unauthenticated`;
see [docs/ANSIBLE.md](../../docs/ANSIBLE.md#telegram-and-email) and
[the dashboard login](../../docs/ANSIBLE.md#the-dashboard-login).

Make the login's hash once on any machine where Sentinel is installed (on a host the role already
set up: `sudo -u sentinel /opt/dspace-sentinel/sentinel.sh hash-password`); it asks for the
password twice and prints only the hash. With `dspace_sentinel_manage_config: true` the role
rewrites `server.login` on every run, so a password changed on the server (More › Account or
`./sentinel.sh set-password`) lasts until the next run: put the new hash in the vault.

## Optional nginx snippet

```yaml
dspace_sentinel_nginx_snippet_enabled: true
dspace_sentinel_nginx_location: /sentinel/
dspace_sentinel_nginx_htpasswd_user: admin
dspace_sentinel_nginx_htpasswd_password: "{{ vault_sentinel_password }}"
```

The role writes `/etc/nginx/snippets/dspace-sentinel.conf` and the htpasswd file (using
`community.general.htpasswd`, which needs `python3-passlib` on the host; the role installs it
on Debian/Ubuntu). Add `include /etc/nginx/snippets/dspace-sentinel.conf;` to the `server`
block that serves your repository (the HTTPS one) and reload nginx. The snippet is the same as
`deploy/nginx/sentinel.conf.example`: basic auth, SSE-friendly proxying, relative prefix. With the
dashboard login set, people type the htpasswd password first and then sign in to Sentinel. It
forwards the host name the repository is reached under; the role adds `inventory_hostname` to
Sentinel's allowed hosts, so if the repository answers under another name, set
`dspace_sentinel_dspace.public_url` or `dspace_sentinel_allowed_hosts` (Sentinel only answers to
host names it knows).

## Updating

Change `dspace_sentinel_version` (or leave `main`) and run the playbook again. A changed
checkout or requirement restarts the unit through the handler; an unchanged run is a no-op.

A quick fleet check runs `doctor` as the service user (exit 0 ok, 1 warnings, 2 critical, 4 the
check could not run):

```bash
ansible dspace_servers -i inventory.ini -b --become-user sentinel -a "/opt/dspace-sentinel/sentinel.sh doctor --ascii"
```

## What the role does not do

It never touches DSpace, PostgreSQL, Solr, Tomcat or nginx configuration beyond the optional
snippet; Sentinel only observes. It does not open firewall ports: Sentinel stays on
`127.0.0.1` unless you set `dspace_sentinel_host: 0.0.0.0` on purpose.

## Linting

CI runs, from this directory and as gating steps, `ansible-playbook -i inventory.example.ini
--syntax-check playbook.yml` and `ansible-lint --offline --profile production .` (ansible-lint
26.x); both are clean.
