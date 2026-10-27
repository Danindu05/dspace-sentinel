# Security policy

DSpace Sentinel runs next to production repository servers and holds database
credentials, Telegram bot tokens, a mail password and the hash of its own
dashboard password, so we treat security reports seriously.

## Supported versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | Yes       |

Only the latest minor release receives fixes.

## Reporting a vulnerability

Please do **not** open a public issue for security problems.

Use GitHub's private vulnerability reporting instead: on the repository page
open **Security → Report a vulnerability** and describe the problem, the
affected version (`./sentinel.sh version`) and, if you can, a reproduction.
You will get an acknowledgement within 72 hours and a status update at least
every 14 days until the report is resolved. Fixed issues are credited in the
changelog unless you prefer to stay anonymous.

## What counts

Anything that would let Sentinel:

- write to, restart or reconfigure DSpace, PostgreSQL, Solr, Tomcat, Node or
  nginx (the project is observe-only by design, see `docs/SPEC.md` section 0);
- leak a secret (`postgres.password`, the `bot_token` of any
  `notifiers.telegram.chats` entry, `notifiers.email.password`,
  `notifiers.webhook.url`, `server.auth_token`, `server.login.password_hash`,
  the dashboard password itself, a session token, or the database password that
  discovery reads from DSpace's `local.cfg`) through the API, the UI, a report,
  a log line, an events row, an error text, a Telegram message or an email, or
  write a secret that came from an environment variable into
  `config/sentinel.yaml` or the demo's settings file;
- send a secret or a message where it was not meant to go: a stored bot token
  used for another chat, the mail password sent unencrypted to another host, a
  certificate that is not verified, or a test message or email to a chat or an
  address that is not configured;
- put attacker-controlled text into a mail header (a subject, a sender name or
  an address with CR/LF, commas or angle brackets) or unescaped HTML into an
  email;
- be reached without the dashboard login (`server.login`) or the configured
  `server.auth_token`, or bypass the reverse-proxy recipe in
  `docs/REVERSE-PROXY.md`;
- sign someone in without the password, keep a session alive past its limits
  (12 hours idle, 7 days, 30 days with "Keep me signed in") or after the
  password changed or the login was removed, let a wrong password be tried
  faster than the limits below allow, or tell a wrong username from a wrong
  password;
- be read or changed by a web page on another site: a cross-site write that
  gets through, a `Host` name outside the allowlist that is answered (DNS
  rebinding), or a stored PostgreSQL or email password sent to a server or login
  it was not saved for;
- start on a non-loopback address without a login or an auth token when
  `server.allow_unauthenticated` is not set (and `SENTINEL_ALLOW_OPEN=1` is
  not either);
- read files outside the configured log and assetstore paths and the fixed
  list in [What the Security module reads](#what-the-security-module-reads),
  or read the contents of a file the Security module only looks at with
  `stat`;
- open or close a port, change a firewall, run sudo or any command that
  changes something, or ask the public address more than once per Security
  check;
- be pushed over its resource limits by a remote party (the systemd unit sets
  `CPUQuota=25%` and `MemoryMax=300M`, but the process should never need them).

## Hardening checklist for operators

- Create the dashboard login (`./sentinel.sh install` asks for it on a
  terminal; later `./sentinel.sh set-password`), with a long password that you
  use nowhere else. Keep it set even on `127.0.0.1`: without a login or a token
  any local account or process (a web application with an SSRF bug, say) can
  call the API.
- Keep `server.host: 127.0.0.1` and use the SSH tunnel, or put nginx with
  HTTPS (or a VPN) in front when you need remote access, so the password never
  travels in plain text. `docs/REVERSE-PROXY.md` has the recipe.
- Sentinel refuses to listen on anything but a loopback address without the
  login or `server.auth_token` (or `SENTINEL_AUTH_TOKEN`). Set
  `server.allow_unauthenticated: true` (or `SENTINEL_ALLOW_OPEN=1`) only behind
  a firewall or an authenticating proxy on another machine, and serve an
  exposed port over TLS.
- Give scripts `server.auth_token` (sent as `Authorization: Bearer <token>`)
  rather than the dashboard password, and keep it in `config/sentinel.env` as
  `SENTINEL_AUTH_TOKEN`.
- Add only the names you really use to `server.allowed_hosts`, and avoid
  `"*"`: the `Host` allowlist is what keeps DNS-rebinding pages out.
- Give Sentinel its own read-only PostgreSQL role; every session it opens is
  already `READ ONLY` with `statement_timeout=3000`.
- Keep secrets (`SENTINEL_PG_PASSWORD`, `SENTINEL_SMTP_PASSWORD`,
  `SENTINEL_TELEGRAM_TOKEN`, `SENTINEL_AUTH_TOKEN`) in `config/sentinel.env`
  (`chmod 600`; every `./sentinel.sh` command and the systemd unit read it, and
  every command warns when other users can read it) or in a vaulted Ansible
  variable, rather than in `config/sentinel.yaml`. Values from the environment
  are never written into the YAML, not even when the UI saves it. The bot
  tokens typed into More › Telegram and the password typed into More › Email
  live in the YAML, which is mode `0600` like Sentinel's SQLite files.
- Every Telegram bot token is a password for that bot: whoever has it can post
  as the bot. Revoke a token that leaked with @BotFather (`/revoke`) and paste
  the new one.
- For email, give Sentinel a Gmail account of its own and an app password
  (Google Account › Security › 2-Step Verification › App passwords), never an
  account's normal password; revoking the app password cuts Sentinel off
  without touching the account. Sentinel sends the saved password only to the
  server and login it was saved for; changing either needs the password again.
  With `SENTINEL_SMTP_PASSWORD` set, neither the UI nor the API can change the
  password or where it goes. Keep the mail password to letters, digits and
  symbols from the English keyboard: SMTP sends the login as ASCII, so with
  any other character Sentinel sends no email at all and says so in one
  sentence; saving such a password is refused, and the mask `••••••`, pasted
  into the settings file, counts as no password. Use `starttls` or `ssl`:
  `security: none` sends everything in plain text, so Sentinel only sends a
  password that way to a server on the same machine, and it is meant for a
  relay that needs no login.
- Run it as an unprivileged user through the shipped systemd unit
  (`NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`).
- Switch the Security module on (every first-run option does): it warns when
  Sentinel itself listens on another address than `127.0.0.1` with neither a
  login nor a token, reminds you of HTTPS when it listens there with a login,
  and warns when `config/sentinel.yaml` or `config/sentinel.env` is readable by
  other users.
- Update with `./sentinel.sh update` and read the changelog.

## The dashboard login

The web UI asks for one admin account, `server.login` in
`config/sentinel.yaml`, which `./sentinel.sh install` creates on a terminal
and `./sentinel.sh set-password` creates or changes. Demo mode never asks for
it.

- **The password is stored as a hash, never in clear.** Sentinel uses scrypt
  from Python's standard library (16 random salt bytes, n = 2^14, r = 8,
  p = 1, a 32-byte hash) and stores `scrypt$16384$8$1$<salt>$<hash>`; on a
  Python without `hashlib.scrypt` it uses PBKDF2-SHA256 with 600,000
  iterations (`pbkdf2_sha256$600000$<salt>$<hash>`). Hashes are compared in
  constant time. The hash is a secret: `GET /api/config` shows `••••••`, it
  never goes into the export, an events row, a log line or a report, and
  `PATCH /api/config` cannot change it. The password itself is never logged,
  echoed, stored or put in an error text; `set-password` and `hash-password`
  read it without echo (or as one line of standard input with
  `--password-stdin`). The rules are 12 to 256 characters and not the
  username, nothing else. A hash Sentinel cannot read keeps the login on:
  nobody can sign in (every sign-in answers 503 "The login in server.login
  could not be read: run ./sentinel.sh set-password"), and the dashboard never
  opens because of it.
- **Sessions.** Signing in creates a random token (32 bytes from Python's
  `secrets`) for the cookie `sentinel_session`: `HttpOnly`, `SameSite=Strict`,
  `Path=/` (the app's root path), `Max-Age` up to the session's end, and
  `Secure` when the request came over HTTPS (behind nginx on the same machine,
  from its `X-Forwarded-Proto` header). The `sessions` table in `sentinel.db`
  keeps only the SHA-256 of the token, with the username, the times, the
  client's address, its User-Agent and a fingerprint (a SHA-256) of the login
  it signed in with, so a copy of the database holds no session anyone can
  use. A session ends after 12 hours without use and 7 days
  after signing in at the latest; "Keep me signed in for 30 days" makes both
  limits 30 days. Signing out deletes the row. A password change on
  **More › Account** ends every other session; one made with `set-password`
  (the running server notices the changed file within seconds, no restart)
  ends all of them, and so does removing the login. Maintenance purges
  expired rows; **Clear history** keeps the live ones.
- **Brute force.** Per client address: after 5 failed sign-ins within 15
  minutes, every further attempt has to wait 2^(failures − 5) seconds after
  the last failure (at most 300 s) and is answered 429 with `Retry-After`; a
  successful sign-in clears that address. Per username: 20 failures within 15
  minutes, from any addresses, lock it for 15 minutes. A wrong current
  password on **More › Account** counts as a failure too. An unknown username
  is checked against a dummy hash of the same cost, so it takes as long as a
  wrong password and gets the same answer, "Wrong username or password.". The
  counts live in memory, so a restart forgets them. Behind a proxy on the same
  machine the client's address comes from `X-Forwarded-For`; behind a proxy on
  another machine every client counts as that proxy's address.
- **What is logged.** Each of these is an `auth` row in the events log:
  "Signed in: admin (192.0.2.10)", "Sign-in failed for 'admin' (192.0.2.10)"
  (a typed name that breaks the username rules reads "an invalid username",
  so a password typed into the wrong field never lands in the log), "Signed
  out: admin", "Sign-in paused for 192.0.2.10 after 5 failed attempts",
  "Password changed" (on **More › Account** or with `set-password`) and "Login
  removed". Never a password, a hash or a token.
- **The local CLI token.** At every start the server writes a new random
  token to `<data dir>/api-token` (mode 0600, so only Sentinel's user can read
  it); `Authorization: Bearer <that token>` counts as signed in. That is how
  `./sentinel.sh status` keeps showing health with a login set. `doctor`,
  `report` and `discover` run in-process and need no login.
- **`server.auth_token` is a token for scripts.** With a login set it is
  accepted as `Authorization: Bearer <token>` or as HTTP Basic with any user
  name, and a request with neither a session nor a token answers 401 `login
  required` without a `WWW-Authenticate` header, so a browser shows the
  sign-in page instead of its own pop-up. Without a login the token works as
  before: the browser's Basic pop-up asks for it, and Bearer is accepted too.
  Keep it long and random (`openssl rand -hex 24`) and in
  `config/sentinel.env` as `SENTINEL_AUTH_TOKEN`.
- **Demo mode never asks.** It ignores any `server.login`, shows no sign-in
  page and answers `POST /api/login` with 400 "No login is set up on this
  Sentinel.".
- With the Ansible role and `dspace_sentinel_manage_config: true`, the role
  writes `server.login` from `dspace_sentinel_login_password_hash` on every
  run, so a password changed on the server lasts until the next run: put the
  new hash (`./sentinel.sh hash-password`) in the vault.

## What the Security module reads

The Security module (More › Modules › Security) shows how exposed this server
is; it never changes anything. On every check (`collector.security_interval`,
15 minutes by default) it:

- lists the TCP and UDP sockets that wait for connections, from the kernel's
  socket table. Sentinel runs as a normal user, so it can name only the
  programs behind its own user's sockets; the others show no program name,
  and the page says so and names the command for you to run
  (`sudo ss -ltnp`). It checks its own user id to know whether that note
  applies;
- lists the names of the network interfaces (psutil, as for the network
  rate): while Tailscale's `tailscale0` exists, Tailscale's own WireGuard
  port is expected. A port that other machines reach only on Tailscale's
  addresses reads "reachable through Tailscale only" in any case;
- asks `systemctl is-active` about `ufw`, `nftables` and `firewalld`, and
  reads `/etc/ufw/ufw.conf` for `ENABLED=yes`;
- reads `/etc/ssh/sshd_config` and the files its `Include` lines name
  (usually `/etc/ssh/sshd_config.d/*.conf`) for `PermitRootLogin`,
  `PasswordAuthentication` and `Port`;
- reads Ubuntu's `/var/lib/update-notifier/updates-available`, and
  `/var/run/reboot-required` and `/var/run/reboot-required.pkgs`;
- reads `/etc/os-release`, and takes the DSpace, Solr and PostgreSQL versions
  from what those modules already found (nothing is asked again);
- looks at the mode, owner and group (`stat`, never the contents) of
  `<dspace.install_dir>/config/local.cfg` and `dspace.cfg`, and of Sentinel's
  own `config/sentinel.yaml` and `config/sentinel.env`;
- sends one GET to `dspace.public_url` (redirects followed, 10 s) and reads
  the response headers only, never the page;
- reads its own settings: `server.host`, and whether the dashboard login
  (`server.login`) and `server.auth_token` are set (never their values).

It never opens or closes a port, never changes a firewall rule, never runs
sudo and never writes to what it reads. A file it may not read makes that
check "not available" instead of asking for more rights, and what to do about
a warning is a hint for you to follow by hand. Three of its warnings open an
alert (a new port open to the network, no host firewall while ports are
exposed, root may log in over SSH with a password); the others are shown on
the Security page and in reports only. See
[docs/ALERTS.md](docs/ALERTS.md#security) and
[docs/FAQ.md](docs/FAQ.md#security).

Sentinel's own exposure is one of its checks: the Sentinel check warns when
the dashboard listens on another address than `127.0.0.1` with neither a login
nor a token, and when it listens there with a login it stays ok with the hint
"Put the nginx HTTPS recipe in front of Sentinel so the password never travels
in plain text."; the Security page's Sentinel tile shows *Listens on*, *Login*
(set / not set) and *Token* (set / not set). The DSpace files check warns when
`sentinel.yaml` or `sentinel.env` is readable by other users (they hold the
Telegram tokens, the email password, the login's hash and the secrets Sentinel
reads when it starts).

## What Sentinel does for you

- The API answers only to known host names and refuses cross-site writes
  (403) and bodies that are not JSON (415); the sign-in (`POST /api/login`)
  and password change go through the same checks. With a login set, only
  `/healthz`, the page itself (`/`, `/index.html`, `/favicon.svg`,
  `/static/…`), `GET /api/session` and `POST /api/login` answer without a
  session; everything else answers 401 `login required`. The `server.*`
  settings (address, port, auth token, the login, `hostname`, `allowed_hosts`,
  `allow_unauthenticated`) cannot be changed through `PATCH /api/config`; only
  `server.timezone`, the zone of the times in messages, can, because it
  decides nothing about who reaches Sentinel. The login is changed only by
  `./sentinel.sh set-password` on the server and by the signed-in admin's
  **More › Account** (which needs the current password).
- Secrets are masked (`"••••••"`) in every API answer, discovery included;
  masked discovery values are applied server-side, and the PostgreSQL password
  only ever goes to the server it was saved or discovered for.
- Every bot token is redacted from every error text, in any letter case and
  URL-encoded, and the mail password never appears in an error or a log line.
  A mail failure Sentinel has no sentence for is shown by the kind of error
  only ("unexpected error (KeyError)"), never by its own text, and a password
  with a character SMTP cannot send (anything outside ASCII) is named in one
  plain sentence before any connection is opened ("the email password has a
  character that cannot be sent: …"), never with that character or where it
  stands. A Telegram error names the proxy from `HTTPS_PROXY` by host and port
  only, never the user or password in its URL, and Gmail's answer to a refused
  login is shown only after the password has been taken out of it.
  A masked token sent back to the API keeps the token of the entry with the
  same `id`; a mask without a known `id` is refused, so a token cannot be moved
  to another chat.
- Email goes over STARTTLS or TLS with the certificate verified against the
  system's CA store, and a password is never sent unencrypted to another host.
  The saved email password is only sent to the SMTP server and login it was
  saved for (a change of server, port, security or username needs it again),
  and one that comes from `SENTINEL_SMTP_PASSWORD` cannot be changed or
  re-pointed through the API at all.
  Addresses, the sender name and subjects are checked or cleaned so nothing can
  add a mail header, and alert text is escaped in the HTML part.
- The test actions only send to bots and recipients that are configured: an
  unknown id or address answers 400. Demo mode never contacts Telegram or a
  mail server.
- The webhook always verifies TLS and never follows redirects.
- HTML reports are served with a Content-Security-Policy that allows no
  scripts and no outgoing requests, and make no external requests themselves.
