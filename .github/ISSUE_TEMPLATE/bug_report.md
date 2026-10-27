---
name: Bug report
about: Something in DSpace Sentinel does not behave as documented
title: "bug: "
labels: bug
assignees: ""
---

## What happened

A clear description of the problem. If the UI is involved, say which tab or
panel (Home, Stack, History, More, New report) and what you clicked.

## What you expected

## Steps to reproduce

1.
2.
3.

## Environment

- Sentinel version, Python and service mode (`./sentinel.sh version -v`):
- Install method: `sentinel.sh` / systemd system unit / user unit / Ansible / demo
- OS (`head -n 2 /etc/os-release`):
- DSpace version (7.x / 8.x) and which modules are on (More tab or `GET /api/meta`):
- Browser (for UI bugs):

## Diagnostics

Paste the relevant output. Secrets are masked automatically in these commands.

```text
./sentinel.sh doctor --json
```

```text
./sentinel.sh logs -n 100
```

(`logs` reads the systemd journal for a unit and `data/sentinel.log` otherwise.)

## Anything else

Screenshots, `curl` output of the failing endpoint, a `config/sentinel.yaml`
excerpt with secrets removed.
