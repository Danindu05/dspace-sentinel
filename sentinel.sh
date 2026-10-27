#!/usr/bin/env bash
# DSpace Sentinel — the one command.
#
#   ./sentinel.sh install        create .venv, install packages, create config and the login, look around
#   ./sentinel.sh start          run in the background (or through systemd once installed)
#   ./sentinel.sh demo           try it with a synthetic DSpace stack on http://localhost:9765
#   ./sentinel.sh help           everything else
#
# Everything except `install` is a thin wrapper around `python -m dspace_sentinel`.
# Sentinel only observes: it never restarts, writes to or reconfigures DSpace, PostgreSQL,
# Solr, Tomcat, Node or nginx.

set -euo pipefail

# The checkout this script belongs to, also when it is called through a symlink
# (e.g. /usr/local/bin/sentinel -> /opt/dspace-sentinel/sentinel.sh).
SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || printf '%s' "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
# The package is not installed into .venv, so put the checkout on the import path: every
# `python -m dspace_sentinel` below then works from any directory (cron, `ansible -a`, an
# absolute path). PYTHONPATH rather than `cd` keeps relative SENTINEL_DATA_DIR / SENTINEL_CONFIG
# values relative to the caller's directory.
export PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}"
VENV="$SCRIPT_DIR/.venv"
PY="$VENV/bin/python"
CONFIG_FILE="${SENTINEL_CONFIG:-$SCRIPT_DIR/config/sentinel.yaml}"
EXAMPLE_CONFIG="$SCRIPT_DIR/config/sentinel.example.yaml"
DATA_DIR="${SENTINEL_DATA_DIR:-$SCRIPT_DIR/data}"
INSTALL_LOG="$DATA_DIR/install.log"
DEFAULT_PORT=9765
# Exit codes (same table as the Python CLI): 0/1/2 are health results (ok / warnings or not
# running / critical), so everything that stops a command from running uses its own code.
EXIT_ERROR=4  # error or missing prerequisite: the command could not run
EXIT_USAGE=64 # unknown command or option (sysexits.h EX_USAGE)

# ------------------------------------------------------------------------------ output

if [[ -t 1 && -z "${NO_COLOR:-}" && "${TERM:-dumb}" != "dumb" ]]; then
  C_BOLD=$'\033[1m'
  C_DIM=$'\033[2m'
  C_GREEN=$'\033[32m'
  C_YELLOW=$'\033[33m'
  C_RED=$'\033[31m'
  C_RESET=$'\033[0m'
else
  C_BOLD=""
  C_DIM=""
  C_GREEN=""
  C_YELLOW=""
  C_RED=""
  C_RESET=""
fi

say() { printf '%s\n' "$*"; }
step() { printf '%s->%s %s\n' "$C_BOLD" "$C_RESET" "$*"; }
ok() { printf '%s ok%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn() { printf '%s !!%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
# die MESSAGE [EXIT_CODE]
die() {
  printf '%s xx %s%s\n' "$C_RED" "$1" "$C_RESET" >&2
  exit "${2:-$EXIT_ERROR}"
}

repo_version() {
  sed -n 's/^__version__ = "\([^"]*\)".*/\1/p' "$SCRIPT_DIR/dspace_sentinel/__init__.py" 2>/dev/null || true
}

usage() {
  local version
  version="$(repo_version)"
  cat <<EOF
${C_BOLD}DSpace Sentinel${C_RESET} v${version:-?} — observe-only health for DSpace 7, 8 and 9 servers

Usage: ./sentinel.sh <command> [options]

${C_BOLD}Setup${C_RESET}
  install [--service|--user-service] [--port N] [--host H] [--dev] [--no-discover]
          [--no-login]
                        create .venv, install packages, create config/sentinel.yaml,
                        ask for the dashboard login (on a terminal; --no-login skips it),
                        look around this machine; --service adds a systemd unit (sudo),
                        --user-service a systemd --user unit (no sudo)
  install-service [--user] [--print] [--no-start] [--port N] [--host H] [--force]
                        (re)install only the systemd unit; --print shows the unit
                        without installing anything; --force replaces a dspace-sentinel
                        unit that runs another checkout
  update [--no-pip] [--no-restart]
                        git pull + pip install, restart if running
  uninstall [--purge] [--yes] [--force]
                        remove this checkout's systemd unit and pid file (data and config
                        are kept; --purge deletes them too, --yes skips its question)

${C_BOLD}Login${C_RESET}
  set-password [--user NAME] [--password-stdin] [--remove] [--yes]
                        create or change the dashboard login: the username (admin by
                        default), then the password twice, hidden; --password-stdin reads
                        one line; --remove deletes the login (--yes skips its question)
  hash-password [--password-stdin]
                        print a password hash for server.login (the Ansible role)

${C_BOLD}Run${C_RESET}
  start [--port N] [--host H] [--timeout S] | stop | restart
  status [--json] | logs [-f] [-n N]
                        through systemd when this checkout's unit is installed, else a
                        background process (data/sentinel.pid, data/sentinel.log);
                        start waits up to --timeout seconds for /healthz (default 20);
                        with --demo they manage a background demo (start --demo)
  run | serve           foreground server (Ctrl-C stops it)
  demo                  foreground demo: synthetic DSpace stack on port ${DEFAULT_PORT}
                        (settings chosen in the demo live in data/demo/demo-sentinel.yaml;
                        SENTINEL_DEMO_FAST=1 compresses the scripted incident to 3 minutes)

${C_BOLD}Check and report${C_RESET}
  doctor [--json] [--ascii] [--width N] [--demo]
                        one-shot boxed dashboard (--width: box columns, default 78);
                        exit 0 ok, 1 warnings, 2 critical
  report [--range 24h|7d|30d] [--format html|md|json] [--title T]
         [--telegram] [--email] [--json]
                        generate a report and print its path (--json: the record);
                        --telegram / --email also send the file (both may be given)
  discover [--apply] [--json]
                        detect DSpace config, ports and services (--apply writes it)
  telegram-test [--to ID_OR_CHAT_ID] [--json]
                        send a test message through every Telegram bot (or the one
                        --to names by id or chat id); one line per bot, exit 4 if any fails
  email-test [--to ADDRESS] [--json]
                        send a test email to every recipient (or the one --to names);
                        one line per recipient, exit 4 if any fails or email is not set up

${C_BOLD}Other${C_RESET}
  version [-v] | help   -v adds the interpreter, paths and service state

Environment: SENTINEL_CONFIG, SENTINEL_DATA_DIR, SENTINEL_HOST, SENTINEL_PORT,
             SENTINEL_AUTH_TOKEN, SENTINEL_TELEGRAM_TOKEN + SENTINEL_TELEGRAM_CHAT_ID (one
             more Telegram bot), SENTINEL_SMTP_PASSWORD (email app password),
             SENTINEL_PG_PASSWORD, SENTINEL_DEMO=1, SENTINEL_ALLOW_OPEN=1 (serve a
             non-loopback host without a login or token), SENTINEL_LOG_LEVEL=DEBUG|INFO|WARNING,
             PYTHON (interpreter for install).
             Keep secrets in config/sentinel.env (KEY=VALUE lines, chmod 600): every
             command and the systemd unit read it; variables already set win.
Exit codes:  0 ok · 1 warnings / not running · 2 critical (doctor) · 3 not available in
             this build · 4 error or missing prerequisite (did not run) · 64 usage error
Docs:        README.md, docs/  ·  https://github.com/Danindu05/dspace-sentinel
EOF
}

need_venv() {
  if [[ ! -x "$PY" ]]; then
    die "Sentinel is not installed yet in $SCRIPT_DIR. Run:  ./sentinel.sh install"
  fi
}

# ----------------------------------------------------------------------------- install

FOUND_PYTHON=""
PYTHON_WITHOUT_VENV=""

find_python() {
  # Sets FOUND_PYTHON to the first interpreter >= 3.10 that can create a virtualenv (venv and
  # ensurepip), or PYTHON_WITHOUT_VENV to the first >= 3.10 one that cannot (for the error).
  # An explicit PYTHON=... is the only candidate.
  local candidate candidates
  if [[ -n "${PYTHON:-}" ]]; then
    candidates=("$PYTHON")
  else
    candidates=(python3.13 python3.12 python3.11 python3.10 python3 python)
  fi
  for candidate in "${candidates[@]}"; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1 || continue
    if "$candidate" -c 'import venv, ensurepip' >/dev/null 2>&1; then
      FOUND_PYTHON="$candidate"
      return 0
    fi
    [[ -n "$PYTHON_WITHOUT_VENV" ]] || PYTHON_WITHOUT_VENV="$candidate"
  done
  return 1
}

python_version() {
  "$1" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null || printf 'unknown\n'
}

pip_install() {
  # pip_install REQUIREMENTS_FILE — quiet, output kept in $INSTALL_LOG, one retry after a pip upgrade
  local req="$1"
  if "$PY" -m pip install --quiet --disable-pip-version-check --progress-bar off -r "$req" >>"$INSTALL_LOG" 2>&1; then
    return 0
  fi
  warn "pip install failed once; upgrading pip and retrying"
  "$PY" -m pip install --quiet --disable-pip-version-check --progress-bar off --upgrade pip wheel >>"$INSTALL_LOG" 2>&1 || true
  "$PY" -m pip install --quiet --disable-pip-version-check --progress-bar off -r "$req" >>"$INSTALL_LOG" 2>&1
}

write_server_overrides() {
  # write_server_overrides PORT HOST — persist --port/--host into the YAML (single source of truth)
  "$PY" - "$1" "$2" <<'PYEOF'
import sys

from dspace_sentinel import paths
from dspace_sentinel.config import DEFAULT_CONFIG, Config, deep_merge, read_yaml, save_config

port, host = sys.argv[1], sys.argv[2]
patch = {"server": {}}
if port:
    patch["server"]["port"] = int(port)
if host:
    patch["server"]["host"] = host
path = paths.config_path()
data = deep_merge(DEFAULT_CONFIG, deep_merge(read_yaml(path), patch))
save_config(Config.model_validate(data), path)
print(f"server.host={data['server']['host']} server.port={data['server']['port']}")
PYEOF
}

server_facts() {
  # server_facts — "URL<TAB>HOST<TAB>PORT<TAB>BIND<TAB>LOGIN" from the effective config
  # (config/sentinel.env included). BIND: local (loopback) | login (the dashboard login is set) |
  # token (auth_token set) | open (explicit opt-in) | refused (serve would not start: reachable
  # from other machines without a login or a token). LOGIN: yes | no.
  # Exits 4 with the reason on stderr when the config is not valid.
  "$PY" - <<'PYEOF'
import sys

from dspace_sentinel import cli, paths
from dspace_sentinel.api.app import UnsafeBindError, check_bind, is_loopback_host
from dspace_sentinel.auth import login_active
from dspace_sentinel.config import load_config

cli.load_env_file()
try:
    cfg = load_config(paths.config_path(), demo=False)
except ValueError as exc:
    print(f"error: {exc}", file=sys.stderr)
    sys.exit(4)
host = cfg.server.host or "0.0.0.0"
login = "yes" if login_active(cfg) else "no"
if is_loopback_host(host):
    bind = "local"
elif login == "yes":
    bind = "login"
elif cfg.server.auth_token:
    bind = "token"
else:
    try:
        check_bind(cfg)
        bind = "open"
    except UnsafeBindError:
        bind = "refused"
print(cli.base_url(cfg), host, cfg.server.port, bind, login, sep="\t")
PYEOF
}

token_hint() {
  local env_file
  env_file="$(dirname "$CONFIG_FILE")/sentinel.env"
  warn "Create the dashboard login first:  ./sentinel.sh set-password"
  warn "or set an auth token, e.g.:  echo \"SENTINEL_AUTH_TOKEN=\$(openssl rand -hex 24)\" >> $env_file && chmod 600 $env_file"
  warn "or keep server.host at 127.0.0.1 and publish Sentinel through deploy/nginx/sentinel.conf.example."
}

NO_LOGIN_HINT="No dashboard login yet: run ./sentinel.sh set-password"

cmd_install() {
  local service_mode="" port="" host="" dev=0 no_discover=0 no_login=0 given="$*"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --service) service_mode="system" ;;
      --user-service) service_mode="user" ;;
      --port)
        [[ $# -ge 2 ]] || die "--port needs a value" "$EXIT_USAGE"
        port="$2"
        shift
        ;;
      --port=*) port="${1#*=}" ;;
      --host)
        [[ $# -ge 2 ]] || die "--host needs a value" "$EXIT_USAGE"
        host="$2"
        shift
        ;;
      --host=*) host="${1#*=}" ;;
      --dev) dev=1 ;;
      --no-discover) no_discover=1 ;;
      --no-login) no_login=1 ;;
      -h | --help)
        usage
        return 0
        ;;
      *) die "unknown install option: $1 (see ./sentinel.sh help)" "$EXIT_USAGE" ;;
    esac
    shift
  done
  if [[ -n "$port" && ! "$port" =~ ^[0-9]+$ ]]; then
    die "--port must be a number (got '$port')" "$EXIT_USAGE"
  fi
  [[ -f "$SCRIPT_DIR/requirements.txt" ]] || die "requirements.txt not found next to sentinel.sh; run from the git checkout"
  # Under sudo every file below would belong to root while the unit runs as $SUDO_USER, which
  # then cannot read its config or write its database (the unit crash-loops).
  if [[ "$(id -u)" == 0 && -n "${SUDO_USER:-}" && "$SUDO_USER" != root ]]; then
    die "do not run the installer with sudo: run it as $SUDO_USER instead,
    ./sentinel.sh install${given:+ $given}   (--service asks for sudo only to install the unit)"
  fi

  say "${C_BOLD}DSpace Sentinel${C_RESET} v$(repo_version) — installing into $SCRIPT_DIR"
  say ""

  # 1. Python >= 3.10 with the venv module
  local sys_py
  if ! find_python; then
    if [[ -n "$PYTHON_WITHOUT_VENV" ]]; then
      local short
      short="$("$PYTHON_WITHOUT_VENV" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo 3)"
      die "the venv module is missing for $PYTHON_WITHOUT_VENV. On Debian/Ubuntu:  sudo apt install python${short}-venv   (or python3-venv)
    or pick an interpreter that has it:  PYTHON=python3 ./sentinel.sh install"
    fi
    die "Python 3.10 or newer was not found${PYTHON:+ (PYTHON=$PYTHON)}. On Debian/Ubuntu:  sudo apt update && sudo apt install python3 python3-venv"
  fi
  sys_py="$FOUND_PYTHON"
  ok "Python $(python_version "$sys_py") ($sys_py)"

  # 2. virtualenv (re-runnable: a healthy .venv is kept)
  if [[ -x "$PY" ]] && "$PY" -c 'import sys' >/dev/null 2>&1; then
    ok "Reusing virtualenv $VENV (Python $(python_version "$PY"))"
  else
    step "Creating virtualenv in $VENV"
    rm -rf "$VENV"
    "$sys_py" -m venv "$VENV" || die "could not create the virtualenv (is python3-venv installed?)"
    ok "Virtualenv created"
  fi

  # 3. packages
  mkdir -p "$DATA_DIR" "$DATA_DIR/reports"
  : >"$INSTALL_LOG"
  local started
  started="$(date +%s)"
  step "Installing Python packages (quiet; first run can take a minute)"
  if ! pip_install "$SCRIPT_DIR/requirements.txt"; then
    tail -n 25 "$INSTALL_LOG" >&2
    die "pip install failed — full log: $INSTALL_LOG"
  fi
  if [[ $dev -eq 1 ]]; then
    step "Installing developer packages (pytest, ruff, playwright)"
    pip_install "$SCRIPT_DIR/requirements-dev.txt" || {
      tail -n 25 "$INSTALL_LOG" >&2
      die "pip install of requirements-dev.txt failed — full log: $INSTALL_LOG"
    }
  fi
  ok "Python packages ready ($(($(date +%s) - started)) s)"

  # 4. config (never overwritten; mode 0600 because it may hold secrets)
  mkdir -p "$(dirname "$CONFIG_FILE")"
  if [[ -f "$CONFIG_FILE" ]]; then
    ok "Keeping existing config $CONFIG_FILE"
  else
    cp "$EXAMPLE_CONFIG" "$CONFIG_FILE"
    chmod 0600 "$CONFIG_FILE"
    ok "Created $CONFIG_FILE (mode 0600)"
  fi
  if [[ -n "$port" || -n "$host" ]]; then
    local written
    written="$(write_server_overrides "$port" "$host")" || die "could not write --port/--host into $CONFIG_FILE"
    ok "Config updated: $written"
  fi
  local facts url bind_host bind_port bind login
  facts="$(server_facts)" || die "$CONFIG_FILE is not valid (see above); fix it, or move it away and run ./sentinel.sh install again"
  IFS=$'\t' read -r url bind_host bind_port bind login <<<"$facts"

  # 5. the dashboard login: asked on a terminal only, so CI and Ansible never wait for an answer
  if [[ "$login" != yes && $no_login -eq 0 ]]; then
    if [[ -t 0 ]]; then
      step "Create the dashboard login"
      if "$PY" -m dspace_sentinel set-password; then
        facts="$(server_facts)" || die "$CONFIG_FILE is not valid (see above); fix it, or move it away and run ./sentinel.sh install again"
        IFS=$'\t' read -r url bind_host bind_port bind login <<<"$facts"
      else
        warn "$NO_LOGIN_HINT"
      fi
    else
      warn "$NO_LOGIN_HINT"
    fi
  fi

  # 6. look around
  if [[ $no_discover -eq 0 ]]; then
    step "Looking around this machine (DSpace config, ports, services)"
    if ! "$PY" -m dspace_sentinel discover; then
      warn "discovery did not complete; run ./sentinel.sh discover later"
    fi
  fi

  # 7. optional systemd unit
  if [[ -n "$service_mode" && "$bind" == refused ]]; then
    warn "Sentinel is set to listen on $bind_host:$bind_port (reachable from other machines) without a"
    warn "login or an auth token, so it would refuse to start and the service would fail."
    token_hint
    die "not installing the systemd unit"
  fi
  case "$service_mode" in
    system)
      step "Installing the systemd system unit (needs sudo)"
      "$PY" -m dspace_sentinel install-service || die "install-service failed; you can retry with ./sentinel.sh install --service"
      ;;
    user)
      step "Installing the systemd --user unit"
      "$PY" -m dspace_sentinel install-service --user || die "install-service --user failed; you can retry with ./sentinel.sh install --user-service"
      ;;
    "") ;;
  esac

  # 8. next steps
  local running=0
  if [[ -z "$service_mode" ]] && "$PY" -m dspace_sentinel status >/dev/null 2>&1; then
    running=1 # started earlier: it keeps the old packages, port and host until restarted
  fi
  say ""
  ok "${C_BOLD}DSpace Sentinel v$(repo_version) is installed.${C_RESET}"
  say ""
  if [[ -n "$service_mode" ]]; then
    say "  Dashboard:        $url"
    say "  Manage:           ./sentinel.sh status | stop | start | logs -f"
  elif [[ $running -eq 1 ]]; then
    say "  Running already:  ./sentinel.sh restart          -> $url   (applies this install)"
    say "  Run at boot:      ./sentinel.sh install --service   (or --user-service without sudo)"
  else
    say "  Start it:         ./sentinel.sh start            -> $url"
    say "  Run at boot:      ./sentinel.sh install --service   (or --user-service without sudo)"
  fi
  say "  Try the demo:     ./sentinel.sh demo             -> http://127.0.0.1:$DEFAULT_PORT (synthetic data)"
  say "  One-shot check:   ./sentinel.sh doctor"
  say "  Configuration:    $CONFIG_FILE   ${C_DIM}(the web UI edits it for you)${C_RESET}"
  say ""
  case "$bind" in
    local)
      say "  Sentinel binds to $bind_host:$bind_port (this machine only). To reach it from elsewhere use"
      say "  the SSH tunnel, or the nginx HTTPS recipe in deploy/nginx/sentinel.conf.example, not an open port."
      ;;
    login)
      say "  Sentinel binds to $bind_host:$bind_port and asks for the dashboard login. Put the nginx HTTPS"
      say "  recipe (deploy/nginx/sentinel.conf.example) in front so the password never travels in plain text."
      ;;
    token)
      say "  Sentinel binds to $bind_host:$bind_port and asks for the auth token (HTTP Basic). Prefer TLS"
      say "  through the nginx recipe in deploy/nginx/sentinel.conf.example over an open port."
      ;;
    open)
      warn "Sentinel binds to $bind_host:$bind_port WITHOUT authentication (allow_unauthenticated /"
      warn "SENTINEL_ALLOW_OPEN): anyone who reaches that port can read and change its settings."
      ;;
    *)
      warn "Sentinel is set to listen on $bind_host:$bind_port (reachable from other machines) without a"
      warn "login or an auth token, so it will refuse to start."
      token_hint
      ;;
  esac
}

# ------------------------------------------------------------------------------ dispatch

main() {
  local cmd="${1:-help}"
  if [[ $# -gt 0 ]]; then shift; fi
  case "$cmd" in
    install) cmd_install "$@" ;;
    help | -h | --help) usage ;;
    version | --version | -V)
      if [[ -x "$PY" ]]; then
        exec "$PY" -m dspace_sentinel version "$@"
      fi
      say "dspace-sentinel $(repo_version) (not installed yet — run ./sentinel.sh install)"
      ;;
    run)
      need_venv
      exec "$PY" -m dspace_sentinel serve "$@"
      ;;
    demo)
      need_venv
      export SENTINEL_DEMO=1
      exec "$PY" -m dspace_sentinel demo "$@"
      ;;
    serve | start | stop | restart | status | logs | doctor | report | discover | telegram-test | email-test | set-password | hash-password | install-service | update | uninstall)
      need_venv
      exec "$PY" -m dspace_sentinel "$cmd" "$@"
      ;;
    *)
      warn "unknown command: $cmd"
      usage >&2
      exit "$EXIT_USAGE"
      ;;
  esac
}

main "$@"
