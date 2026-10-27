# Notifications: Telegram, email and the webhook

Sentinel tells people when something goes wrong and when it is over. It sends through three
channels, each switched on separately:

- **Telegram**: up to five bots, one per person. Each person creates their own bot with
  @BotFather and gets the alerts in a chat with it; a bot can also post to a group or a channel.
- **Email**: through Gmail (with an app password) or any other SMTP server or relay, to up to
  five recipients.
- **The webhook**: one JSON `POST` per event to a URL of your choice, to bridge to Slack,
  Mattermost, Matrix, ntfy, PagerDuty or your own script.

The channels are independent: one that fails never keeps another from sending, and the checks
never wait for a delivery. Telegram and email are set up in the UI under **More › Telegram** and
**More › Email**; everything can also go into `config/sentinel.yaml`, and the secrets into
`config/sentinel.env`.

## What is sent, and when

| Event | Telegram | Email | Webhook |
|---|---|---|---|
| alert opened, escalated to critical, resolved | yes | yes | yes |
| reminder, every `alerts.reminder_minutes` while an alert is open and not acknowledged (0 = never) | yes | yes | yes |
| incident opened, incident resolved (the Incidents module) | yes | yes | yes |
| daily digest | at `notifiers.telegram.daily_digest` | at `notifiers.email.daily_digest` | with Telegram's digest |
| report: **Send to Telegram** / **Send by email**, the weekly job, `report --telegram` / `--email` | the file as a document | the file as an attachment | no |
| test message | **Send test message**, **Test**, `telegram-test` | **Send test email**, **Test**, `email-test` | no |

Nothing goes out while a channel's switch is off, except its test message: **Send alerts to
Telegram** (`notifiers.telegram.enabled`) and **Send alerts by email**
(`notifiers.email.enabled`) are what let alerts, incidents, reminders, digests and reports
through. A working test alone does not mean alerts will arrive.

Three rules decide who gets what, each explained below: every Telegram bot and every email
recipient takes **All alerts** or **Critical only** ([who gets what](#who-gets-what)), **quiet
hours** hold warnings back, separately for Telegram and for email ([quiet hours](#quiet-hours)),
and in **demo mode** nothing leaves the machine ([demo mode](#demo-mode)). An alert closed
because its module was switched off, or because its mount or log file disappeared, resolves
without a message on any channel.

---

## Telegram

### One bot per person

Each person who wants the alerts creates their own bot, sends it a message, and adds the bot's
token and their own chat id as one row of **More › Telegram**, up to five rows. Sentinel sends
every message through each row's bot to that row's chat, so everyone controls their own chat:
they can mute, block or delete their bot without touching anyone else's. A row can also be a
group (its chat id is negative) or a channel (`@channelname`) for a team that shares one chat.

### 1. Create a bot

1. In Telegram open a chat with **[@BotFather](https://t.me/BotFather)**.
2. Send `/newbot`. Choose a display name (for example *Repo-1 Sentinel*) and a username that
   ends in `bot` (`repo1_sentinel_bot`).
3. BotFather answers with the **token**, a string like `123456789:AAH4x…`. Treat it like a
   password.

### 2. Find the chat id

A bot can only write to chats that have contacted it first.

**Your own chat.** Open your new bot in Telegram and press **Start** (or send it any message).
Then ask **[@userinfobot](https://t.me/userinfobot)** for your id: it answers with a number such
as `987654321`.

**A group.** Add the bot to the group and post a message that mentions it, for example
`/start@repo1_sentinel_bot` (a group with privacy mode on only shows the bot messages that
mention it or start with `/`, which is enough). Group ids are negative (`-123456789`);
supergroups start with `-100`.

**A channel.** Add the bot to the channel as an administrator and use the channel's public
name, `@channelname`, as the chat id.

Telegram itself also tells you the id: after sending the bot a message, open this URL in a
browser, with the bot's token,

```
https://api.telegram.org/bot123456789:AAH4x…/getUpdates
```

and read the chat id from the JSON:

```json
{"ok": true, "result": [{"message": {"chat": {"id": 987654321, "type": "private", …}, …}}]}
```

If `result` is empty, send the bot another message and reload.

A chat id is a number of up to 20 digits (with a minus in front for a group) or `@` followed by
5–32 letters, digits and underscores. A token is 5–12 digits, a colon and 30–50 letters, digits,
`-` and `_`. Sentinel refuses anything else with "not a Telegram chat id: 'me'" or "not a
Telegram bot token (it looks like 123456789:ABC…, from @BotFather)".

### 3. Add the bots in Sentinel

In **More › Telegram**:

1. Switch **Send alerts to Telegram** on.
2. Under **Bots**, fill in one row per person: **Name (optional)** (up to 40 characters; results
   and the events log use it), **Bot token** and **Chat id**, and choose **All alerts** or
   **Critical only**. **＋ Add a bot** adds a row, up to five ("Up to 5 bots, one per person");
   ✕ removes one at once and offers **Undo** ("Removed Team — Save changes to apply.").
3. Click **Save changes**. Then click **Test** on a row to test that bot, or **Send test
   message** to test every bot. Both save the form first.

Each bot sends "👋 DSpace Sentinel is connected · repo-1" within a second, and its row shows the
outcome with the bot's name: "✅ Danindu · @danindu_sentinel_bot · delivered", or why it failed:
"❌ Team · @team_sentinel_bot · chat not found — add the bot to the group first". A line under
the list sums it up ("✅ Test message delivered to both bots.", "❌ 1 of 2 bots got the test
message — each row says why."). A test goes out whatever the bot's **Critical only** setting,
and also while **Send alerts to Telegram** is off.

A saved token is shown as `••••••` and never sent back to the browser; a line under it says
"Saved. Hidden for safety — type a new one to replace it.". Saving with the mask in place keeps
that row's stored token; the first keystroke, paste or delete in the field replaces the whole
mask. With the switch on but no row that has both a token and a chat id, the page says "⚠️
Telegram is on but no bot is set up yet — add a bot token and a chat id so alerts have somewhere
to go.". The **How to set it up** disclosure on the page repeats these steps.

### The bot from `config/sentinel.env`

To keep a token out of `config/sentinel.yaml`, put the token and the chat id in
`config/sentinel.env` next to it ([CONFIG.md](CONFIG.md#configsentinelenv)):

```bash
cat >> config/sentinel.env <<'EOF'
SENTINEL_TELEGRAM_TOKEN='123456789:AAH4x…'
SENTINEL_TELEGRAM_CHAT_ID='987654321'
EOF
chmod 600 config/sentinel.env
./sentinel.sh telegram-test      # getMe, then the test message, through every bot
./sentinel.sh restart            # the running Sentinel reads the file when it starts
```

The pair becomes one more bot, named "From environment". **More › Telegram** shows it
read-only, titled "Chat 987654321" with "From environment · set in config/sentinel.env" under
it and a **Test** button of its own. It takes all alerts, counts toward the five, and is never
written into `config/sentinel.yaml`: change it in `config/sentinel.env` and restart. It is left
out when a row already has the same token and chat id. Remember to switch **Send alerts to
Telegram** on as well (or set `enabled: true` and restart).

- `SENTINEL_TELEGRAM_TOKEN` on its own becomes the token of every row saved without one, so a
  "token in `sentinel.env`, chat id in the config" setup keeps working.
- In an older `config/sentinel.yaml` with the single `bot_token` / `chat_id` keys (from before
  Sentinel had several bots), each variable replaces that bot's value, as it always did.
- With five rows already in the file, the pair is left out and Sentinel starts anyway: the log
  says "the Telegram bot from SENTINEL_TELEGRAM_TOKEN was left out: there are already 5 bots",
  and every start writes one `config` row with the same words to the events log. Remove a row
  and the bot from the environment comes back. Adding a sixth while it is there is refused:
  "Telegram can send to at most 5 bots (the one from SENTINEL_TELEGRAM_TOKEN /
  SENTINEL_TELEGRAM_CHAT_ID counts too)".
- A value that cannot be a token or a chat id stops Sentinel from starting (exit code 4):
  `SENTINEL_TELEGRAM_TOKEN is not a Telegram bot token (it looks like 123456789:ABC…, from
  @BotFather)` or `SENTINEL_TELEGRAM_CHAT_ID is not a Telegram chat id: 'me'`.

With Ansible, list the bots in `dspace_sentinel_telegram_bots` with each token in a vaulted
variable ([ANSIBLE.md](ANSIBLE.md#telegram-and-email)).

### In `config/sentinel.yaml`

```yaml
notifiers:
  telegram:
    enabled: true
    chats:                               # up to 5, one bot each
      - label: Danindu                   # optional, up to 40 characters
        bot_token: "123456789:AAH4x…"     # from @BotFather; a secret
        chat_id: "987654321"             # a person, a group (negative) or @channelname
        min_severity: warning            # warning = all alerts, critical = critical only
      - label: Team
        bot_token: "234567890:BBH4x…"
        chat_id: "-1001234567890"
        min_severity: critical
    quiet_hours: "23:00-07:00"           # only critical during the night; "" = always
    daily_digest: "08:00"                # one summary message per day; "" = off
```

Sentinel gives every row an `id`, eight hexadecimal characters such as `032ad4f2`, the first
time it is saved; keep it, because the UI and the API use it to tell rows apart and to keep each
row's token when the mask comes back. A row without an id gets one that stays the same across
restarts until the file is saved. A hand-written id (up to 32 letters, digits, `-` and `_`) is
kept; `env` is reserved for the bot from the environment. A file that still has a single
`bot_token` and `chat_id` directly under `telegram` loads as one row, and the first save from
the UI writes it as `chats`.

### From the shell

```bash
./sentinel.sh telegram-test                      # every bot: getMe, then the test message
./sentinel.sh telegram-test --to 987654321       # one bot, by its row id or its chat id
./sentinel.sh telegram-test --json               # {ok, bot_name, error, results: [...]}
```

It prints one line per bot, then a summary:

```
  ✅ Danindu · @danindu_sentinel_bot · delivered
  ✅ Team · @team_sentinel_bot · delivered
Connected to Telegram — check your chats for the test message.
```

With a single bot the summary names it: "Connected to Telegram as @danindu_sentinel_bot — check
your chat for the test message.". The exit code is 0 when every bot delivered and 4 otherwise
("error: Telegram test failed: 1 of 2 bots failed — Team: chat not found — add the bot to the
group first"). Without any bot the command says where to add one: "error: Telegram is not
configured: add a bot (its token from @BotFather and your chat id) in More › Telegram, under
notifiers.telegram.chats in config/sentinel.yaml, or with SENTINEL_TELEGRAM_TOKEN +
SENTINEL_TELEGRAM_CHAT_ID.". An unknown `--to` lists the bots there are: "error: no Telegram bot
has the id or chat id 'nope'; configured: 032ad4f2 (Danindu), a75ddc47 (Team)". Like the
buttons, the command sends whether or not **Send alerts to Telegram** is on: it proves that this
machine can reach each bot, nothing more.

### What the messages look like

All messages use HTML parse mode and the configured hostname (`server.hostname` or the machine
name). Every time names the zone of `server.timezone`: its abbreviation (`UTC`, `CEST`), or the
UTC offset when the zone has none (`Asia/Colombo`: 21:29 +0530). Set the zone in **More › Alert
thresholds › Time zone** (an IANA name; empty = the server's own zone,
[CONFIG.md](CONFIG.md#the-time-zone-of-messages-and-schedules)).

Critical alert, with the ladder that failed:

```
🔴 CRITICAL · server-1
PostgreSQL unreachable
service ✅ → TCP ✅ → connect ❌
connection refused (localhost:5432)

Started 10:43 UTC · DSpace Sentinel
```

Warning with context:

```
🟡 WARNING · server-1
Disk 87 % on /dspace
free 53.7 GB · growing 2.1 GB/day

Started 10:41 UTC · DSpace Sentinel
```

Escalation (warning → critical) reuses the critical format with one extra line,
`Escalated from warning`, above the `Started …` footer. Resolution is two lines with the duration:

```
✅ Resolved · server-1
PostgreSQL unreachable · lasted 6 m 12 s
```

Reminder:

```
🔔 Still open · server-1
🔴 PostgreSQL unreachable · open for 32 m 10 s
```

Incident (requires the Incidents module), with the sequence and the affected services:

```
🧭 INCIDENT #24 · server-1
Possible Solr resource exhaustion

10:41 UTC  Memory 94 %
10:42 UTC  Solr response 4.8 s
10:43 UTC  Solr DOWN
10:43 UTC  DSpace search unavailable

Affected: DSpace Search, DSpace Backend
```

and when it is over:

```
✅ Incident #24 resolved · server-1
Possible Solr resource exhaustion · lasted 12 m 40 s
```

Daily digest: the last 24 hours in numbers, then what is open right now (up to five alerts,
oldest first, so the longest-running ones are always listed; more than five end with
`… and N more`):

```
📋 Daily digest · server-1
Last 24 h: 2 alerts (1 critical) · 1 incident
Open now: 1 alert
🟡 Disk 87 % on /dspace · 3 h 12 m

DSpace Sentinel
```

With nothing open the third line reads `Open now: nothing — all clear`. A report arrives as a
document with a two-line caption, "📄 <title>" and "<period> · <from> → <to>" (each time with its
zone: "07 Oct 2026 21:29 +0530"), sent as plain text, so a title with `<` or `&` arrives as typed.

### Telegram errors and what they mean

Sentinel turns Telegram's refusals into advice for the person who set the bot up. The same
sentence appears in the row after a test, in `telegram-test`, in the report snackbar and in the
events log:

| Sentinel says | What to do |
|---|---|
| chat not found — send the bot a message first | The person never opened their bot, or the chat id is wrong. Open the bot, press **Start**, check the id. |
| chat not found — add the bot to the group first | A negative (group) id: the bot is not in that group, or the id is wrong. |
| chat not found — add the bot to the channel as an administrator first | An `@channel` id: add the bot to the channel as an administrator. |
| the bot has not been started in this chat — open it in Telegram and press Start | The person has not started their bot yet. |
| the bot was blocked in this chat — unblock it in Telegram | Unblock the bot in Telegram. |
| the bot was removed from this group — add it again | Add the bot to the group again. |
| the bot is not a member of this chat — add it first | Add the bot to the group or channel. |
| the bot may not post in this chat — allow it to send messages | The group or channel restricts who may post: give the bot that right. |
| this group became a supergroup: its chat id is now -100… | Put the new id into the row. |
| this Telegram account is deactivated | The person's account is gone: remove the row. |
| Telegram refused the bot token (401 Unauthorized) — copy it again from @BotFather | The token is wrong or was revoked in BotFather (also said for 404 Not Found). Paste it again and check for a trailing space. |
| no bot token: paste the token @BotFather gave you | A test of a row without a token. |
| no chat id: send the bot a message, then paste your chat id | A test of a row without a chat id. |
| Telegram has no bot yet: add a bot token and a chat id | Nothing to send to: no row has both. |

A request that never gets an answer from Telegram says what happened and what to check:

| Sentinel says | What to do |
|---|---|
| api.telegram.org did not answer within 10 s: this server may not be able to reach Telegram (a firewall, or a proxy it has to use: set HTTPS_PROXY in config/sentinel.env) | The server may not make outgoing HTTPS connections to `api.telegram.org`: check it with `curl` ([below](#checking-that-the-server-reaches-telegram)). If the network has a proxy, put `HTTPS_PROXY=http://proxy.example:3128` in `config/sentinel.env` and restart. A dead IPv6 route alone is not the cause: Sentinel tries IPv4 as well. |
| api.telegram.org refused the connection: a firewall on this server or its network probably blocks Telegram (allow outgoing HTTPS, port 443, or set HTTPS_PROXY in config/sentinel.env if the network has a proxy) | A firewall rejects the connection: allow outgoing port 443, or use the network's proxy. |
| this server cannot look up api.telegram.org (DNS): check its name servers (/etc/resolv.conf), or set HTTPS_PROXY in config/sentinel.env if the network has a proxy | The server's DNS does not resolve the name. Networks that need a proxy often resolve only internal names. |
| the TLS certificate of api.telegram.org could not be verified (…): check this server's date and time, and whether a proxy or firewall on this network intercepts HTTPS | The server's clock is wrong, or something on the network replaces Telegram's certificate. For a proxy that does this on purpose, point `SSL_CERT_FILE` in `config/sentinel.env` at a CA bundle that holds its certificate. |
| the secure connection to api.telegram.org failed (…): a firewall or proxy on this network may block or intercept Telegram (set HTTPS_PROXY in config/sentinel.env if the network has a proxy) | The TLS handshake was cut off or refused, which is typical of a filter that blocks Telegram. |
| cannot connect to api.telegram.org (network is unreachable): check this server's network and firewall, or set HTTPS_PROXY in config/sentinel.env if the network has a proxy | The server has no route to Telegram. |

With `HTTPS_PROXY` (or `ALL_PROXY`) set, the sentence is about the proxy, which it names by host
and port only, never the user or password in its URL: "api.telegram.org did not answer within
10 s through the proxy proxy.example:3128: check HTTPS_PROXY in config/sentinel.env, and that the
proxy lets this server reach Telegram", "the proxy proxy.example:3128 refused the connection:
check its address in HTTPS_PROXY (config/sentinel.env)", "this server cannot look up the proxy
proxy.example:3128 (DNS): check its address in HTTPS_PROXY (config/sentinel.env)", "cannot connect
to the proxy proxy.example:3128 (…): check its address in HTTPS_PROXY (config/sentinel.env)" or
"the proxy proxy.example:3128 did not connect to api.telegram.org (407 Proxy Authentication
Required): check HTTPS_PROXY in config/sentinel.env (the address, and a login if the proxy needs
one)".

Any other refusal keeps Telegram's own words ("Bad Request: message is too long (error 400)").
With several bots failing, the summary names each one: "2 of 3 bots failed — Team: …; Kim: …".
Every token of the configuration is removed from every error text, in any letter case and also
URL-encoded, before it is shown or logged.

#### Checking that the server reaches Telegram

Run this on the server itself:

```bash
curl -sS -m 10 -o /dev/null -w '%{http_code}\n' https://api.telegram.org/      # directly
curl -4 -sS -m 10 -o /dev/null -w '%{http_code}\n' https://api.telegram.org/   # IPv4 only
curl -sS -m 10 -x http://proxy.example:3128 -o /dev/null -w '%{http_code}\n' https://api.telegram.org/   # through a proxy
```

Any HTTP status code (three digits other than `000`) means the server reaches Telegram that way.
`000` with "Connection timed out", "Could not resolve host" or "Connection refused" confirms what
Sentinel said. When only the line with `-x` (your network's proxy) gets a status code, put that
proxy into `config/sentinel.env` as `HTTPS_PROXY=http://proxy.example:3128` and run
`./sentinel.sh restart`.

---

## Email

### Gmail

Gmail refuses an account's normal password from programs such as Sentinel. Give Sentinel an
**app password** instead; a Gmail account of its own for the repository server is a good idea,
because alerts come *from* that address.

1. Turn on **2-Step Verification** for the Gmail account: Google Account › Security › 2-Step
   Verification.
2. Create an app password, signed in to that same Google account: Google Account › Security ›
   2-Step Verification › App passwords. Name it "DSpace Sentinel"; Google shows **16 letters**
   once, in four groups of four.
3. In Sentinel open **More › Email**, switch **Send alerts by email** on, keep **Gmail**
   selected, enter the **Gmail address** and paste the **App password**. The spaces Google shows
   are fine (Sentinel removes them). Never paste the normal password, nor an app password of
   another Google account: Gmail refuses them, and Sentinel then names the account and Gmail's
   own answer: "Gmail refused the login for sentinel.repo1@gmail.com (535 5.7.8 Username and
   Password not accepted): use an app password made in that same Google account (Google Account ›
   Security › 2-Step Verification › App passwords), not the normal password".
4. Under **Recipients**, add up to five addresses with **＋ Add recipient** ("Up to 5
   recipients"), each with an optional label and **All alerts** or **Critical only**.
5. Click **Save changes**, then **Send test email** for every recipient or **Test** on one row.
   Both save the form first.

**Gmail** means `smtp.gmail.com`, port 587 and STARTTLS, so the server fields stay hidden (a
configuration that already uses another port and security, such as 465 with `ssl`, keeps them,
and the line under **Gmail** names them: "smtp.gmail.com, port 465, SSL/TLS — nothing else to
fill in."). Gmail sends as the account that logs in, so saving with **Gmail** also drops a From
address typed earlier for another server.
**Send as** is the sender's name in the inbox (default "DSpace Sentinel", up to 60 characters).
The **How to set it up** disclosure repeats steps 1–3. To stop Sentinel from sending, remove its
app password in the same Google page.

The test email's subject is "[Sentinel] Test email · repo-1". Each row then shows its outcome,
"✅ On call · sent" or "❌ boss@example.org · refused by the mail server: 550 5.1.1 …", and a line
under the list sums it up ("✅ Test email sent to both recipients — check the inbox.", "❌ 1 of 2
recipients got the test email — each row says why."). A test goes out whatever the recipient's
**Critical only** setting, and also while **Send alerts by email** is off. With the switch on
and no recipient the page says "⚠️ Email is on but has no recipients yet — add one so alerts
have somewhere to go.".

### Other mail servers and relays

Choose **Other SMTP server** to send through your organisation's mail server or a relay:

- **SMTP server** and **Port**.
- **Security**: **STARTTLS** (port 587, the usual submission port), **SSL/TLS** (implicit TLS,
  port 465) or **None**. Switching between STARTTLS and SSL/TLS moves the port between 587 and
  465.
- **Username** and **Password**: leave the username empty for a relay that needs no login. SMTP
  sends the login as plain ASCII, so both take only letters, digits and symbols from the English
  keyboard: saving another character (an `ä`) is refused ("the email password has a character
  that cannot be sent: use letters, digits and symbols from the English keyboard", or the same
  about the SMTP username). A settings file or `SENTINEL_SMTP_PASSWORD` that already holds such a
  password still loads, with email not ready and that sentence at every start ([email
  errors](#email-errors-and-what-they-mean)).
- **From address (optional)**: the sender's address; empty means the username. A relay without a
  login needs it, and so does a login that is not an email address, such as SendGrid's `apikey`,
  an Amazon SES key id or a Postmark token ("Enter the address alerts come from — the username is
  not one."). Saving such a login without a From address is refused: "the SMTP username is not an
  email address: set from_address (the address alerts come from)". A settings file that already
  holds one (edited by hand, or saved before Sentinel checked this) still loads, with email on or
  off: email is then not ready, so no alert email goes out, every start logs "email is not
  ready: the SMTP username is not an email address: set from_address (the address alerts come
  from)" as a warning and writes it as one `config` row to the events log, and the test email
  names the same sentence without connecting.

A saved password is only ever sent to the server and login it was saved for. Changing the
provider, the SMTP server, the port, the security or the username empties the password field
("Enter the password again — the saved one is only sent to the server and login it was saved
for."), and going back to the saved server and login brings the saved password back. Opening
the page changes nothing: only your own change counts, so a Gmail account saved with another
port keeps its password. The API refuses such a change unless the password comes with it:
"enter the email password again: the SMTP server, port, security or username changed and the
saved password is only sent to the server it was saved for". Switching to a relay that needs no
login (an empty username) is always allowed: the stored password stays in the file but is never
sent there.

TLS certificates are verified against the system's CA store. **None** sends everything,
password included, in plain text, so Sentinel sends a password that way only to a server on
this machine (`localhost`, `127.0.0.1`, `::1`). A username with `none` and any other host is
refused with 'security "none" would send the password unencrypted: use starttls or ssl, or
leave the username empty for a relay that needs no login'; a relay that needs no login works
with `none` on any host.

```yaml
notifiers:
  email:
    enabled: true
    smtp_host: mail.example.org
    smtp_port: 25
    security: none                     # a relay on your own network that needs no login
    username: ""
    from_address: sentinel@example.org
    recipients:
      - {address: ops@example.org, label: On call}
```

### In `config/sentinel.yaml` and `config/sentinel.env`

```yaml
notifiers:
  email:
    enabled: true
    smtp_host: smtp.gmail.com
    smtp_port: 587
    security: starttls                 # starttls | ssl | none
    username: sentinel.repo1@gmail.com # the account Sentinel logs in with
    password: ""                       # the app password; better in SENTINEL_SMTP_PASSWORD
    from_address: ""                   # "" = the username
    from_name: DSpace Sentinel
    recipients:                        # up to 5
      - {address: ops@example.org, label: On call, min_severity: warning}
      - {address: head@example.org, label: Head of library, min_severity: critical}
    quiet_hours: ""
    daily_digest: "08:00"
```

To keep the app password out of the YAML, put it in `config/sentinel.env`:

```bash
echo "SENTINEL_SMTP_PASSWORD='abcd efgh ijkl mnop'" >> config/sentinel.env
chmod 600 config/sentinel.env
./sentinel.sh email-test          # the test email, from this shell
./sentinel.sh restart             # the running Sentinel reads the file when it starts
```

`SENTINEL_SMTP_PASSWORD` wins over `notifiers.email.password` and is never written into the
YAML. While it is set, the password is read-only: **More › Email** shows the field locked (a
muted fill; a keystroke changes nothing) with "Set in config/sentinel.env (SENTINEL_SMTP_PASSWORD)
— change it there.", `GET /api/config` reports `notifiers.email.password_from_env: true`, and the
API refuses a new password ("the email password comes from SENTINEL_SMTP_PASSWORD in
config/sentinel.env: change it there"). The server, port, security and username it goes to can
then only be changed in `config/sentinel.yaml`; the API answers "the email password comes from
SENTINEL_SMTP_PASSWORD in config/sentinel.env and is only sent to the server it was set for:
change the SMTP server, port, security or username in config/sentinel.yaml".

Email can send once it has a server, a sender that is an email address (`from_address`, else
the username), the password when a username is set, and at least one recipient; until then
`email-test` and the test button say "email is not set up: add the Gmail address, app password
and at least one recipient". Settings that can never send are named instead, before any
connection is opened: a sender that is not an email address, or a login with a character SMTP
cannot send ([email errors](#email-errors-and-what-they-mean)).
A recipient is one plain address (`ops@example.org`): a display name ("Ops
&lt;ops@example.org&gt;"), spaces or commas are refused ("not an email address: …"), and so is
the same address twice in any letter case ("ops@example.org is listed twice").

### From the shell

```bash
./sentinel.sh email-test                         # one test email to every recipient
./sentinel.sh email-test --to ops@example.org    # only this recipient
./sentinel.sh email-test --json                  # {ok, error, results: [...]}
```

```
  ✅ On call (ops@example.org) · sent
  ✅ boss@example.org · sent
Test email sent through smtp.gmail.com:587 — check the inbox for "[Sentinel] Test email · repo-1".
```

The exit code is 0 when every recipient got it and 4 when any did not ("error: Email test
failed: 1 of 2 recipients failed — boss@example.org: refused by the mail server: 550 …") or
when email is not set up, in which case the message says what is missing and that the app
password can also come from `SENTINEL_SMTP_PASSWORD` in `config/sentinel.env`. Settings that can
never send stop it with one sentence before anything is sent ("error: Email test failed: the
SMTP username is not an email address: set from_address (the address alerts come from)"). A
refused login or connection is one line ("error: Email test failed: Gmail refused the login for
sentinel.repo1@gmail.com (535 5.7.8 Username and Password not accepted): …").
An address that is not a recipient lists the ones there are ("error: nobody@example.org is not
one of the email recipients: ops@example.org, boss@example.org"). `./sentinel.sh report --email`
sends a report ([REPORTS.md](REPORTS.md#generating-one)).

### What the emails look like

One email per event: a single message addressed to every recipient that takes the event, so
they all appear on its To: line. The subjects (the host is `server.hostname` or the machine
name):

| Event | Subject |
|---|---|
| alert opened | `[Sentinel] CRITICAL · repo-1 · PostgreSQL unreachable`, `[Sentinel] Warning · repo-1 · Disk 87 % on /dspace` |
| escalated to critical | `[Sentinel] CRITICAL (escalated) · repo-1 · Disk 92 % on /dspace` |
| reminder | `[Sentinel] Still open · repo-1 · PostgreSQL unreachable` |
| resolved | `[Sentinel] Resolved · repo-1 · PostgreSQL unreachable (lasted 6 m 12 s)` |
| incident opened / resolved | `[Sentinel] Incident #24 · repo-1 · Possible Solr resource exhaustion`, `[Sentinel] Incident #24 resolved · repo-1 · Possible Solr resource exhaustion (lasted 12 m 40 s)` |
| daily digest | `[Sentinel] Daily digest · repo-1` |
| test | `[Sentinel] Test email · repo-1` |
| report | `[Sentinel] Report · repo-1 · <report title>` |

A subject is always one line of at most 200 characters (cut with "…"). The body carries the same
facts as the Telegram message (the chain up to the failing rung and its reason, when it started
or how long it lasted, an incident's timeline and the affected services, every time with its
zone: "Started 10:43 UTC", "10:41 UTC  Memory 94 %") twice: as plain text
and as HTML with inline styles only, no images and no web fonts, readable in light and dark mail
clients. It ends with "Sent by DSpace Sentinel on repo-1. Who gets these emails is set in More ›
Email.". Every message carries `Auto-Submitted: auto-generated` and `X-Auto-Response-Suppress:
All`, so vacation replies stay away. A report is attached as the file that was generated (HTML
or Markdown, labelled as UTF-8 text, or JSON), up to 17 MB as Sentinel counts sizes everywhere
(17 × 1024 × 1024 = 17,825,792 bytes, which the mail encoding turns into about 24.4 million
bytes, inside Gmail's 25 MB); a larger one is not sent: "sentinel-report-20260929-1938-30d.html
is too large to email (17.1 MB; the limit is 17 MB)", with the size rounded up so that it never
reads like the limit. The whole message must reach the mail server within the 20 s timeout, so
over an uplink slower than roughly 10 Mbit/s a large report can fail with "smtp.gmail.com:587
did not answer within 20 s"; send it through Telegram, or make it smaller (a shorter range, or
Markdown or JSON).

### Email errors and what they mean

Sentinel turns SMTP failures into one sentence; the password, or any part of it, never appears
in it or in the log. A failure it has no sentence for is named by the kind of error only,
"unexpected error (KeyError)", never by the error's own text.

| Sentinel says | What to do |
|---|---|
| Gmail refused the login for sentinel.repo1@gmail.com (535 5.7.8 Username and Password not accepted): use an app password made in that same Google account (Google Account › Security › 2-Step Verification › App passwords), not the normal password | Create an app password in the Google account the sentence names ([Gmail](#gmail)) and paste it. The part in brackets is Gmail's own answer, its code and first line (for example "534 5.7.9 Application-specific password required"). |
| the mail server refused the login (535): check the username and password | Another server refused the login. |
| security "none" would send the password unencrypted: use starttls or ssl, or leave the username empty for a relay that needs no login | See [other mail servers](#other-mail-servers-and-relays). |
| smtp.example.org:587 does not offer STARTTLS: use security "ssl" (port 465) or the server's TLS port | The server has no STARTTLS on that port. |
| smtp.example.org:25 does not accept a login: leave the username empty or use its submission port | A relay port: no username, or the submission port (587). |
| smtp.example.org:465 did not answer within 20 s | A firewall drops outgoing SMTP, or the port is wrong. On port 465 with another security setting the sentence adds ' (port 465 needs security "ssl")'. For a large report over a slow uplink, see [the attachment limit](#what-the-emails-look-like). |
| smtp.example.org:465 closed the connection: check the port and security (Gmail: starttls on port 587, ssl on port 465) | Port and security do not match. |
| smtp.gmail.com:587 closed the connection. Gmail does this for a few minutes after a refused login: check the address and the app password, wait, then test again. Otherwise check the port and security (Gmail: starttls on port 587, ssl on port 465) | Gmail drops new sessions for a few minutes after a refused login. Fix the login, wait a few minutes, then test again. |
| TLS with smtp.example.org:465 failed: check the security setting (Gmail: starttls on port 587, ssl on port 465) | The same, found during the TLS handshake. |
| the TLS certificate of 10.0.0.5 could not be verified (…): check the server name | Use the name on the server's certificate, not its IP address. |
| cannot find the mail server smtp.exmaple.org: check its name | DNS does not know the name. |
| smtp.example.org:587 refused the connection: check the server name and port | Nothing listens there. |
| cannot reach smtp.example.org:587: … | The network is in the way. |
| the mail server refused the sender sentinel@example.org: 553 … | The account may not send as that address: fix **From address**. |
| the SMTP username is not an email address: set from_address (the address alerts come from) | A login such as SendGrid's `apikey` or an SES key id: set **From address**. |
| the email password has a character that cannot be sent: use letters, digits and symbols from the English keyboard | SMTP sends the login as plain ASCII, so a password with any other character (an `ä`) can never log in. Saving such a password in **More › Email** or through the API is refused with this sentence. If one comes from `config/sentinel.yaml` or `SENTINEL_SMTP_PASSWORD`, Sentinel still starts but email is not ready: every start logs "email is not ready: the email password has a character that cannot be sent: …", and the test, alerts and reports say the sentence before connecting. Give the account a password without such characters and enter it again. The mask `••••••` pasted into `config/sentinel.yaml` as the password means no password ("email is not set up: …"). |
| the SMTP username has a character that cannot be sent: use letters, digits and symbols from the English keyboard | The same for the login name, which is also refused when saved. |
| refused by the mail server: 550 … (one row) · the mail server refused every recipient | The server does not accept that address. |
| the mail server refused the message: 552 … | Usually its size (a large report). |
| email is not set up: add the Gmail address, app password and at least one recipient | Something is missing, see [above](#in-configsentinelyaml-and-configsentinelenv). |
| unexpected error (KeyError) | A failure Sentinel has no sentence for; only the kind of error is shown, because its text could hold part of the password. Run `./sentinel.sh email-test` to see whether it happens again. |

Other SMTP answers read "the mail server answered 451 …" or "smtp.example.org:587 refused the
session: 554 …", with the server's own code and text. With several recipients refused, the
summary names each: "1 of 2 recipients failed — boss@example.org: refused by the mail server:
550 …".

---

## The webhook

`notifiers.webhook` posts JSON to a URL for alerts (opened, escalated, resolved, reminders),
incidents (opened, resolved) and the daily digest:

```yaml
notifiers:
  webhook:
    enabled: true
    url: https://hooks.example.org/sentinel
```

Each delivery is an HTTP `POST` with `Content-Type: application/json` and a 5 s timeout. Quiet
hours and **Critical only** do not apply to it, and tests and reports go to Telegram and email
only. The destination's TLS certificate is always verified, whatever `dspace.verify_tls` says,
and redirects are never followed: a 3xx answer is recorded as a failure (`HTTP 302 from webhook
(redirects are not followed)`) instead of sending the alert on to another host. The URL counts
as a secret, because many webhook URLs carry their credential in the path: the API and the UI
show it as `"••••••"`.

The body is the notification envelope: the kind and transition, the hostname, the time, a
one-line `summary`, the message as Telegram HTML (`html`) and as plain text (`text`), and the
alert or incident object exactly as the API returns it:

```json
{"kind": "alert", "type": "opened", "severity": "critical", "hostname": "server-1", "ts": 1790169058,
 "summary": "CRITICAL Solr unreachable",
 "html": "🔴 <b>CRITICAL</b> · server-1\n<b>Solr unreachable</b>\nservice ✅ → TCP ❌\n…",
 "text": "🔴 CRITICAL · server-1\nSolr unreachable\nservice ✅ → TCP ❌\n…",
 "alert": {"id": 18, "rule": "component_down:solr", "severity": "critical", "title": "Solr unreachable", "…": "…"},
 "incident": null, "sentinel_version": "0.1.0", "source": "dspace-sentinel", "version": "0.1.0"}
```

```json
{"kind": "incident", "type": "opened", "severity": "critical", "hostname": "server-1", "ts": 1790168913,
 "summary": "Incident #4: Possible Solr resource exhaustion", "html": "…", "text": "…", "alert": null,
 "incident": {"id": 4, "hypothesis": "Possible Solr resource exhaustion", "affected": ["…"], "sequence": ["…"]},
 "sentinel_version": "0.1.0", "source": "dspace-sentinel", "version": "0.1.0"}
```

`kind` is `alert`, `incident` or `digest`; `type` is `opened`, `escalated`, `resolved`,
`reminder` or `digest`. A non-2xx answer or a timeout is recorded as a `notify.error` event;
deliveries are not retried. The webhook has no digest time of its own: it receives the digest
at `notifiers.telegram.daily_digest`, which works while Telegram itself is off.

---

## Who gets what

Every Telegram bot and every email recipient has its own `min_severity`:

- **All alerts** (`warning`, the default): every message.
- **Critical only** (`critical`): only alert and incident messages whose severity is critical,
  that is an alert that opens as critical or escalates to critical, its reminders and its
  resolution, and an incident with at least one critical alert.

Every message carries its alert's severity. A warning that escalates later reaches a **Critical
only** person first with the "escalated" message, and its resolution is then critical too. An
incident is critical when one of its alerts is. Test messages, daily digests and reports go to
every bot and every recipient, whatever their setting. When nobody on a channel takes an event,
that channel skips it.

## Quiet hours

`notifiers.telegram.quiet_hours` and `notifiers.email.quiet_hours` are separate `HH:MM-HH:MM`
windows, read in the zone of `server.timezone` ([below](#the-zone-of-quiet-hours-and-digests));
either may wrap midnight (`23:00-07:00`), and `""` means always send. A pasted en or em dash,
spaces around it and single-digit hours are understood and saved as `HH:MM-HH:MM`
(`"7:00 – 9:30"` becomes `"07:00-09:30"`). Inside its window a channel sends only critical alert
and incident messages. Every message has the severity of its alert, so
the resolution of a warning counts as a warning too: a disk warning that opens at 22:50 and
clears at 23:10 sends neither message on that channel. Messages held back are recorded as
`notify.skipped` events ("telegram: … held back by quiet hours", "email: …") and are not sent
later; the alerts stay visible in the UI. Daily digests and test messages are never held back,
and the webhook has no quiet hours. **Acknowledging** an alert in the UI stops its reminders on
every channel.

The form and the API refuse to save a value they cannot read as such a window: `11pm-7am`,
digits other than 0–9 or a value longer than 40 characters with "quiet hours must look like
23:00-07:00", and a window whose two times are the same, such as `23:00-23:00`, which would never
be quiet, with "quiet hours must start and end at different times". The forms in **More ›
Telegram** and **More › Email** check both before they send anything and say so under the field:
"Use HH:MM-HH:MM, for example 23:00-07:00." or "Quiet hours must start and end at different
times.". A value like that in a hand-edited `config/sentinel.yaml` does not stop Sentinel: it
starts without quiet hours on that channel, logs a warning (`doctor` and `status` print it too)
and writes one `config` row to the events log at every start, for example "The quiet hours
'11pm-7am' in notifiers.telegram.quiet_hours were ignored: quiet hours must look like
23:00-07:00".

A typical split: Telegram quiet at night (`23:00-07:00`), email never quiet, so warnings wait
in the inbox for the morning and only critical alerts reach a phone at night.

### The zone of quiet hours and digests

Quiet hours, both daily digest times and the weekly report's day and hour are read in the zone
of `server.timezone`, the same zone the times in the messages name; when it is empty they follow
the server's own clock. On a server that runs on UTC, `23:00-07:00` therefore means 04:30 to
12:30 for someone at +05:30 until `server.timezone` is set to their zone, for example
`Asia/Colombo`. Set it in **More › Alert thresholds**, group *Times in messages*, field **Time
zone** (an IANA name, help "IANA name such as Asia/Colombo; empty = the server's zone (UTC)."
with the server's own zone in the brackets); it applies at once. **More › Telegram** and **More ›
Email** then name the zone in the help of their fields:

| Field | Help |
|---|---|
| Telegram › Quiet hours | "Only critical alerts during this window, in Asia/Colombo (+0530), for every bot. Leave empty to always notify." |
| Telegram › Daily digest | "One summary message per day at this time, in Asia/Colombo (+0530). Leave empty for none." |
| Email › Quiet hours | "Only critical alerts by email during this window, in Asia/Colombo (+0530). Leave empty to always send." |
| Email › Daily digest | "One summary email per day at this time, in Asia/Colombo (+0530). Leave empty for none." |

With the zone empty they name the server's own ("in Etc/UTC (UTC)"), and from a Sentinel that does
not report a zone they read as before, without the clause. A name the API cannot load is refused
under the field in the server's words ("Unknown time zone 'Mars/Base': use an IANA name such as
Asia/Colombo, or leave it empty for the server's zone"). One in a hand-edited
`config/sentinel.yaml` does not stop Sentinel: it uses the server's zone, logs a warning and
writes one `config` row at every start, "The time zone in server.timezone was ignored, so times
use the server's zone: unknown time zone 'Mars/Base': use an IANA name such as Asia/Colombo, or
leave it empty for the server's zone".

## Daily digests

A daily digest is one message with the last 24 hours in numbers and the alerts open right now
([format](#what-the-messages-look-like)). Each channel has its own time, `""` or `HH:MM` (`8:00` is
saved as `08:00`; the form and the API refuse anything else with "the daily digest time must look
like 08:00"): `daily_digest: "08:00"`
under `notifiers.telegram` (Telegram and the webhook, sent while either is switched on) and
under `notifiers.email` (email only, while email is on), each read in the zone of
`server.timezone` ([above](#the-zone-of-quiet-hours-and-digests)). A newly set time starts with
the next occurrence: set `08:00` at 10:00 and the first digest arrives the next morning. A
digest goes to every bot or recipient, including the **Critical only** ones, quiet hours or not.

Sentinel writes both times in double quotes when it saves the file; keep them when you edit it
by hand. YAML reads an unquoted `daily_digest: 18:00` as the number 1080, which Sentinel reads
back as the time it was written as (a whole number from 60 to 1439, so 1080 is 18:00). Any other
digest time it cannot read (`8 am`, `08.00`, `24:00`) is left out like unreadable quiet hours: no
digest on that channel, a warning in the log and a `config` row at every start ("The daily
digest time '8 am' in notifiers.email.daily_digest was ignored: the daily digest time must look
like 08:00"; for a number the row adds " (YAML reads an unquoted time as a number: write it in
quotes)").

## Delivery and the events log

Deliveries never hold up the checks, and the channels never hold up each other. Every
notification goes into a background queue that keeps one lane per channel: each channel
delivers one event at a time in the order they happened, an event's channels are sent side by
side, and a channel that stalls (a mail server that never answers, an unreachable
`api.telegram.org`) delays only its own later messages, never another channel's, with the two
exceptions below. Each channel has its own limit:

- **Telegram** goes through the bots of an event one after another, 10 s per call, at most 10 s
  per bot plus 5 s for the whole delivery.
- **Email** is one SMTP session per event with a 20 s timeout on every step (connect, TLS,
  login, data), at most 65 s. A delivery given up on (its limit, or a shutdown) cuts the
  connection, so the message is not handed over later, and a mail server that stops answering
  never holds Sentinel up, not even when it stops or restarts.
- **The webhook** has a 5 s timeout per request.

The two exceptions:

- **At most 500 events are under way.** An event stays in the queue until its slowest channel is
  done. Once 500 events wait behind a stalled channel, a new event reaches the other channels
  only when the stalled one finishes or gives up a delivery, and once 500 more are waiting, the
  oldest waiting event is dropped for every channel, with the warning "notification outbox full:
  dropped the oldest waiting notification" in the log.
- **Waiting messages keep their settings.** An event's deliveries are planned with
  the settings in force when it happened. After the mail settings are repaired, or email is
  switched off, the emails already waiting are still attempted with the old settings, each
  leaving its row; the events after the change use the new settings.

On shutdown Sentinel gives the queue up to 10 s. What is still under way or waiting after that
is given up, and the log says how much in one warning: "5 notifications not delivered (Sentinel
is stopping)" (a notification is one event, however many of its channels were left; nothing is
logged when none was). Every attempt leaves an `events` row, written as soon as its channel is
done, with `data.channel` and, for Telegram and email, `data.destinations`, one `{destination,
label, ok, error}` per bot (chat id, plus the row `id`) or recipient (address):

| Kind | Message |
|---|---|
| `notify` | "telegram sent: …", "email sent: …" (with the subject), "webhook sent: …" |
| `notify.error` | "email failed: <sentence>", "telegram failed: 1 of 2 bots failed — …" |
| `notify.skipped` | "telegram: … held back by quiet hours" |
| `notify.demo` | "Notification (demo, not sent): …", "Test message (demo, not sent): …", "Test email (demo, not sent): …" |

Report deliveries are recorded the same way: `notify` for a report sent ("Report sent to
Telegram: <title>", "Report sent by email: <title>") or scheduled, `notify.error` for one that was
not, including a channel that is off or not set up ("Report not sent by email: email is not
enabled"), and `notify.demo` in the demo. Look at them in **More › About › Export
data**, on a component's page (Recent events, whose *Kind* column names the channel) or with
`GET /api/events?kind=notify`, `notify.error`, `notify.skipped` or `notify.demo`
([API.md](API.md#get-apieventslimit100componentkind)). A channel that is switched off is simply
skipped and nothing is recorded for it, apart from the messages that were already waiting for it
when it was switched off (see above).

## Demo mode

In demo mode (`./sentinel.sh demo`, `SENTINEL_DEMO=1`) nothing leaves the machine: no Telegram
request is built and no mail server is contacted. Every would-be delivery is written to the
events log as a `notify.demo` event with the message as HTML and plain text, and for email its
subject, so you can see the formatting without a bot or a mailbox. The test buttons answer
"Demo: logged, not sent" (**Send test message**: "Demo: logged, not sent — Telegram is never
called while the demo runs; the message is in the events log."), `POST
/api/actions/telegram-test` and `email-test` answer `{"ok": false, "skipped": "demo", "error":
"demo mode: nothing is sent to Telegram"}` (or "… by email"), and a report sent from the panel
reads "Report ready · Demo: logged, not sent". Alert notifications in the demo list email among
their channels only while email is switched on; Telegram is always included. Bot tokens, the
email password and the bot from the environment are never saved to the demo's settings file,
`data/demo/demo-sentinel.yaml`.

## Settings and environment variables

Every key with its default is in [CONFIG.md](CONFIG.md#notifiers); in short:

| Setting | What it does |
|---|---|
| `notifiers.telegram.enabled` | **Send alerts to Telegram** |
| `notifiers.telegram.chats` | 0–5 rows `{id, label, bot_token, chat_id, min_severity}`, one bot each; every `bot_token` is a secret |
| `notifiers.telegram.quiet_hours`, `daily_digest` | Telegram's quiet hours and digest time (the webhook shares the digest) |
| `notifiers.email.enabled` | **Send alerts by email** |
| `notifiers.email.smtp_host`, `smtp_port`, `security` | `smtp.gmail.com`, `587`, `starttls` by default; `ssl` for port 465, `none` for a local relay |
| `notifiers.email.username`, `password` | the login (the Gmail address and its app password); the password is a secret |
| `notifiers.email.from_address`, `from_name` | the sender (`""` = the username, which must then be an email address) and its name (`DSpace Sentinel`) |
| `notifiers.email.recipients` | 0–5 `{address, label, min_severity}` |
| `notifiers.email.quiet_hours`, `daily_digest` | email's quiet hours and digest time |
| `notifiers.webhook.enabled`, `url` | the webhook; the URL is a secret |
| `reports.weekly.send_telegram`, `send_email` | deliver the weekly report ([REPORTS.md](REPORTS.md#weekly-schedule)) |
| `server.timezone` | the zone of every time in a message and of quiet hours, digests and the weekly report; `""` = the server's own ([above](#the-zone-of-quiet-hours-and-digests)) |
| `SENTINEL_TELEGRAM_TOKEN` + `SENTINEL_TELEGRAM_CHAT_ID` | one more bot, read-only in the UI ([above](#the-bot-from-configsentinelenv)) |
| `SENTINEL_SMTP_PASSWORD` | the email password, instead of `notifiers.email.password`; read-only in the UI and the API while set |

Keep the three variables in `config/sentinel.env` (`chmod 600`): every `./sentinel.sh` command
and both systemd units read it, and saving from the UI never copies its values into the YAML.
The API and the UI show every secret as `"••••••"`; sending the mask back keeps the stored value
(for a bot token, the value of the row with the same `id`).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| The test works, alerts never arrive | The channel's switch is off (`enabled: false`): tests are sent regardless. Or it is inside that channel's quiet hours and the alerts are warnings; or the bot or recipient is set to **Critical only**; or nothing has breached `confirm_samples` yet; or a secret lives only in your shell: a systemd-run Sentinel never sees variables exported there, so put them in `config/sentinel.env` and restart. |
| One person gets nothing, the others do | Their row's test says why ([Telegram errors](#telegram-errors-and-what-they-mean), [email errors](#email-errors-and-what-they-mean)); `GET /api/events?kind=notify.error` lists every failed delivery with `data.destinations`. |
| Telegram times out or cannot connect | The test names the cause: "api.telegram.org did not answer within 10 s: …", "… refused the connection: …", "this server cannot look up api.telegram.org (DNS): …" ([Telegram errors](#telegram-errors-and-what-they-mean)). The server has no outbound HTTPS to `api.telegram.org`; [check it with curl](#checking-that-the-server-reaches-telegram). Sentinel honours `HTTPS_PROXY`: set it in `config/sentinel.env` and restart. |
| Email times out | Outgoing SMTP is blocked (many networks block port 25, some block 587 and 465 too). Use the port your mail server documents for submission, or a relay inside your network. |
| Gmail: "refused the login for … (…)" | Use an app password made in the Google account the sentence names, never the normal password; the account needs 2-Step Verification to have app passwords at all. If one was working before, create a new one and paste it. Right after a refused login Gmail drops new sessions for a few minutes ("smtp.gmail.com:587 closed the connection. Gmail does this …"): wait, then test again. |
| A large report never arrives by email | It is over 17 MB ("… is too large to email"), or the uplink is too slow to hand it over within 20 s ("… did not answer within 20 s"). Use a shorter range, Markdown or JSON, or Telegram. |
| Quiet hours or a digest do nothing after a hand edit | Sentinel could not read the time and left it out; its log and the events log (`GET /api/events?kind=config`) say so at every start ("The quiet hours '11pm-7am' in notifiers.telegram.quiet_hours were ignored: …"). Write `"23:00-07:00"` and `"08:00"`, with the quotes, and restart. |
| Quiet hours or the digest come at the wrong hour; messages say "Started 15:59 UTC" while your clock says 21:29 | Sentinel reads and writes times in the zone of `server.timezone`, the server's own when it is empty. Set your zone, for example `Asia/Colombo`, in **More › Alert thresholds › Time zone** ([above](#the-zone-of-quiet-hours-and-digests)). If you set it in the file and nothing changed, the log and the events log say why at every start ("The time zone in server.timezone was ignored, so times use the server's zone: …"). |
| No alert email although email is on | The log and the events log say "Email is not ready: the SMTP username is not an email address: …": the login (SendGrid's `apikey`, an SES key id) is not an address, so set **From address**. Or "Email is not ready: the email password has a character that cannot be sent: …": enter a password without such characters (**More › Email**, or `SENTINEL_SMTP_PASSWORD` in `config/sentinel.env`). `./sentinel.sh email-test` names any other reason. |
| Emails land in spam | Send from an address of your own domain through its mail server, or ask the recipients to add the sender to their contacts. |
| Messages arrive twice | Two Sentinel instances send to the same people (a user unit and a system unit, or a demo run with real settings). `./sentinel.sh status` on each host. |
| "Telegram notifier not available in this build" | The alerts module is missing from this checkout; run `./sentinel.sh update`. |
