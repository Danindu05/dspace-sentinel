# Contributing to DSpace Sentinel

Thanks for helping. This page tells you how the project is organised, how to
run it locally and what a good pull request looks like.

## The contract comes first

`docs/SPEC.md` is the specification every part of the codebase is built
against: configuration keys, data models, storage schema, health chains, alert
rules, API routes, CLI commands and the UI design. If your change disagrees
with the spec, change the spec in the same pull request and say why in the
description. Small, deliberate deviations are fine; silent ones are not.

Two rules are not negotiable:

1. **Observe only.** Sentinel never writes to, restarts or reconfigures
   DSpace, PostgreSQL, Solr, Tomcat, Node or nginx. PostgreSQL sessions are
   read-only, Solr queries use `rows=0`, log files are read from the tail.
   The Security module shows the server's security status and never changes
   it: it never opens or closes a port, never changes a firewall, never runs
   sudo or any command that changes something; it reads the files SPEC §6
   lists, the socket table, `systemctl is-active` and, once per run, the
   response headers of `dspace.public_url`.
2. **Cheap and dependency-light.** Python 3.10+, FastAPI, psutil, httpx,
   psycopg, jinja2, pyyaml, pydantic. No Node on the server, no ORM, no
   Prometheus. Every external call has a hard timeout.

## Set up a development environment

```bash
git clone https://github.com/Danindu05/dspace-sentinel.git
cd dspace-sentinel
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/playwright install chromium        # only for the end-to-end tests
```

Run the demo stack (synthetic data, no DSpace needed):

```bash
SENTINEL_DEMO=1 .venv/bin/python -m dspace_sentinel serve     # http://localhost:9765
SENTINEL_DEMO=1 SENTINEL_DEMO_FAST=1 .venv/bin/python -m dspace_sentinel serve   # a scripted incident every 3 min
```

Use `SENTINEL_PORT`, `SENTINEL_DATA_DIR` and `SENTINEL_CONFIG` to keep several
instances apart. The demo keeps its data and settings in `data/demo/` unless
you set those variables.

## Checks to run before you push

```bash
.venv/bin/python -m ruff check dspace_sentinel tests
.venv/bin/python -m ruff format --check dspace_sentinel tests
.venv/bin/python -m pytest                       # unit + API tests (the e2e suite skips itself)
SENTINEL_E2E=1 .venv/bin/python -m pytest tests/e2e            # boots demo servers (and one live server with a login) and drives them with Playwright
shellcheck sentinel.sh
```

CI (`.github/workflows/ci.yml`) runs `ruff check` and `ruff format --check`, the
unit suite on Python 3.10 and 3.12 plus a `doctor --demo` smoke run,
shellcheck, `ansible-lint --profile production` and
`ansible-playbook --syntax-check` on `deploy/ansible` together with
`tests/test_ansible_role.py` under real Ansible (the vaulted secrets, the
pre-flight checks and the rendered config; the unit-suite job has no Ansible,
so those cases skip there), an install smoke test of the README path on
`ubuntu:22.04` and `ubuntu:24.04` containers (install, start, status and doctor
from another directory and through a symlink, stop, unit render) and the
Playwright suite. Every job must pass. Code must stay Python
3.10 compatible: use `from __future__ import annotations`, avoid `tomllib`,
`ExceptionGroup`, `typing.Self` and `match`-only constructs.

## Code style

- `ruff` is the linter and formatter (`ruff.toml`: line length 110, rules
  E/F/I/B/UP/W). Run `ruff format` before committing; CI fails when
  `ruff format --check` would change a file. `requirements-dev.txt` pins ruff
  to 0.16.x because the formatter's output changes between versions.
- Type hints everywhere, docstrings on public functions and classes.
- No placeholder or TODO code in `main`. If something is not implemented yet
  it must fail loudly (`NotImplementedError`, exit code 3), never pretend.
- A collector or notifier exception must never crash the scheduler loop:
  catch it, turn it into a `CheckResult(status="unknown")` or an event, log it.
- Nothing slow runs on the event loop or under the scheduler's cycle lock:
  blocking filesystem calls go through `collectors.base.run_blocking(key, fn,
  *args, timeout=)` (one call per key in flight, `ProbeStalled` while the last
  one hangs), and notifications are queued on the `alerts.notifiers.Outbox`
  instead of being awaited.
- Keep secrets out of logs, events, reports, error texts and API payloads.
  `config.masked` is the only way config reaches a client; a new secret goes
  into `config.SECRET_PATHS`, which masks it, keeps it when the mask is sent
  back and keeps it out of the demo settings file. A secret inside list items
  (every Telegram bot's `bot_token`) goes into `config.SECRET_ITEM_PATHS`
  instead: the mask then keeps the value of the item with the same `id`.
  `save_config` writes only the file's own layer, never values from the
  environment. The dashboard login lives in `dspace_sentinel/auth.py`
  (standard library only): never log, echo or store a password, never put
  one in an exception text, and keep only hashes of passwords (scrypt) and of
  session tokens (SHA-256). `tests/test_auth.py` covers the hashing, the
  rules, sessions, the brute-force limits, the CLI token, the middleware and
  every login route with its exact texts; extend it with any change there.
  The login is changed only by `set-password` and `POST /api/account/password`,
  never by `PATCH /api/config`.
- Never put a real secret in a test. A made-up Telegram bot token is written
  in two parts (`"123456789:" + "AAH-…"`), so that GitHub's secret scanning
  does not report it as a real token. Test passwords for the dashboard login
  are made-up passphrases, and a test login's hash comes from
  `dspace_sentinel.auth.hash_password` at run time, never a pasted hash.
- Never send a real email or Telegram message, not even from a test. The
  Telegram tests use `httpx.MockTransport`; `tests/test_email.py` runs a real
  `smtplib` session against an in-process fake SMTP server; demo mode never
  opens a connection. No test may ask a real service for anything: the autouse
  fixture `version_probe` in `tests/conftest.py` stubs discovery's version
  probe (`discovery.probe_versions`) and gives `discovery.make_client` a
  transport that refuses every request, so a test that tries fails at
  teardown with "discovery's version probe tried the network: […]". A test of
  the real probe passes its own `httpx.MockTransport` client
  (`REAL_PROBE_VERSIONS` in `tests/test_discovery.py`). A few tests in
  `tests/test_cli.py` still look for a Sentinel on this machine's
  127.0.0.1:9765 and :9793, so stop a local Sentinel or demo on those ports
  before you run the suite. A new notification channel goes into
  `alerts/notifiers/__init__.py` (`NOTIFIERS`), returns a `NotifyResult` per
  event, never raises into the scheduler and honours demo mode.
- Anything that depends on the DSpace version reads it through
  `dspace_sentinel/dspace_version.py` (`parse()` and its helpers: the SEO DOWN
  window, the 9.3+ Host rule, the Solr major, the optional cores, end of life),
  never by comparing strings in a collector. Test it with real payloads: the
  REST roots, actuator answers, homepages, Solr STATUS and log lines of DSpace
  7.6 to 10 live in `tests/fixtures/dspace-live/` (`tests/test_dspace_version.py`,
  `tests/test_collectors.py`, `tests/test_logs.py`), and a DSpace 9
  runnable-JAR install in `tests/fixtures/dspace9-jar/`
  (`tests/test_discovery.py`). `docs/COMPATIBILITY.md` says what each version
  changes; keep it in step.
- The Security collector reaches every source through
  `collectors.security.HostSources` (the socket table, the network interface
  names, whether Sentinel runs as root, file reads, `stat`, `systemctl
  is-active`, the one GET of the public address), and
  `tests/test_security.py` passes a fake: no test may depend on this
  machine's real sockets, files or services. Every read is guarded, so a
  failing source makes its rung informational and never raises out of
  `collect()`. The end-of-support dates live in
  `dspace_sentinel/util/support_dates.py` with their sources and the day they
  were checked (`CHECKED_ON`); when you update a date, update its source
  comment, the boundary tests and the table in `docs/SPEC.md` §6.

## Adding a collector

1. Create `dspace_sentinel/collectors/<module>.py` with a `Collector`
   subclass (`id`, `interval_key`, `label`, `emoji`, `depends_on`,
   `async collect(ctx)`). Set `budget_s` to its rung timeouts in a row plus
   a few seconds of slack when that can exceed `interval − 1`, so a slow but
   working service is measured instead of cut off. Rows the reports need but
   the ladder should not show go into `CollectorResult.history`.
2. Register it in `collectors/__init__.py` (`REGISTRY`) and add the module
   flag to `config.ModulesConfig`, `config.MODULE_IDS`, the personas in
   `config.PERSONAS`, the toggle label in `api/routes_config.MODULE_LABELS` and
   `config/sentinel.example.yaml` (`tests/test_config.py` checks that the
   example equals the defaults). The Security module also needed its place in
   `health.COMPONENTS` and `health.COMPONENT_ORDER`,
   `collectors.COLLECTOR_ORDER`, `api/routes_status.CONFIG_SLICES` (the
   settings its page shows), the module rows of
   `static/js/screens/onboarding.js` and `static/js/screens/more/modules.js`,
   and `cli.NETWORK_IDS` (the doctor box); a new module usually does too.
3. Describe the rungs in `docs/SPEC.md` section 6 and in `README.md`'s feature
   table; add the one-line explainer to the More tab and to `docs/CONFIG.md`.
4. Mirror the demo convention for dependencies: when a dependency's module is
   off, omit the `*.dep.*` rung entirely; when it is on but not collected yet,
   use `health.dependency_rung(..., None)`.
5. Add tests under `tests/` that run without a real DSpace (fixtures, fakes,
   `respx` style mocking of httpx is fine).

## Front-end changes

The UI is plain ES modules under `dspace_sentinel/static/`. Read
`docs/DESIGN.md` first: one token file (`css/tokens.css`), one tile recipe,
mood accent, ladders, one right-side report panel and settings sub-views
under More, minimal motion. Raw colours, radii, type sizes and durations live
only in `tokens.css`, and `tests/test_static_tokens.py` enforces it: a raw
colour, duration, radius or type size elsewhere fails, as do gaps that are not
spacing tokens, 4–15 px paddings or margins, and any `var(--x)` that
`tokens.css` does not define (relative `0.95em` monospace and 1–3 px optical
nudges are allowed). Verify at 1440 px wide in light and dark, then 1920 and
1280 px, the 1100 px rail and the narrow fallback at 390 px. Keep the DOM
contract the end-to-end tests rely on (`tests/e2e/test_ui.py` lists the
selectors), and check that the UI still works with the API's checks: writes
send JSON (or no body) from Sentinel's own page. The states of the detail
pages that the demo never shows (an actuator that is down or not exposed, a
client-side shell, a public URL that is down or not checked yet, a Solr core
that failed to load or reload, a rung with a hint, the addresses on the Ports
page, the key/value lists, the Security page healthy, with every warning, not
available and with 40 ports, its Home tile and its Stack ladder) live in
`tests/e2e/test_detail_states.py`: each
case rewrites `/api/status`, one SSE `status` frame and `/api/components/<id>`
with `page.route` and fails on a console error or warning, a page error or a
horizontal scroll bar. `tests/test_collectors.py` runs the real collector in
the situation of every state in `SCENARIOS`, so a new collector state needs
both; a state no collector test replays yet goes into `UI_STATES`. `ADDED`
lists components the demo may not have yet: a state of one adds it to
`/api/status` after Ports and answers its `/api/components/<id>` payload
itself. `tests/e2e/test_ui.py` checks the wizard's Security row for every
option, More › Modules and the report panel's Security section.
`tests/e2e/test_onboarding.py` drives the first-run wizard the same way, with
`/api/actions/discover` answered by `page.route` (a server without a web
server, where nginx and SSL must stay off). `tests/e2e/test_login.py` covers
the dashboard login in the browser: the demo never asks for one, so it starts
a live server of its own (a free port, a private data directory, a config
whose `server.login` holds a hash from `dspace_sentinel.auth.hash_password`,
every module off) and checks the sign-in screen, a wrong password, signing
in, *Keep me signed in*, Sign out, a session that ended, the 429 countdown
(mocked with `page.route`) and More › Account's password change. It stays
below the brute-force limit of five failures; keep it there. Its DOM contract
(`#gate`, `#signin-error`, `#account-name`, `#sign-out`, the More row
`[data-key="account"]`) is listed at the top of the file.

## Commits and pull requests

- Use [Conventional Commits](https://www.conventionalcommits.org/):
  `feat:`, `fix:`, `docs:`, `test:`, `chore:`, `refactor:`.
- One topic per pull request; fill in the template; link the issue.
- Add a line to `CHANGELOG.md` under the unreleased version at the top for
  anything a user would notice.
- New behaviour needs a test. Bug fixes need a regression test.
- Screenshots for UI changes: the e2e suite writes them to `build/screenshots/`;
  `SENTINEL_E2E=1 SENTINEL_E2E_SHOTS=docs/screenshots .venv/bin/python -m pytest tests/e2e`
  refreshes the README images (only `tests/e2e/test_ui.py`'s: the
  `detail-*.png` captures of `test_detail_states.py` always stay in
  `build/screenshots/`), and `docs/DESIGN.md` explains the one taken by hand
  (`incident.png`).

## Reporting bugs and proposing features

Use the issue templates. For bugs, include the output of
`./sentinel.sh doctor --json` (it contains no secrets), `./sentinel.sh version -v`
and the relevant lines of `./sentinel.sh logs -n 100`. For security problems
follow `SECURITY.md` instead.

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). Be kind;
assume good intent; keep discussions about the code.
