## Summary

<!-- One or two sentences: what changes and why. Link the issue: Closes #123 -->

## Type of change

- [ ] Bug fix
- [ ] New feature or module
- [ ] Documentation
- [ ] Refactor / chore
- [ ] Test only

## Spec

- [ ] `docs/SPEC.md` still describes the behaviour after this change (or this
      PR updates it and the description says what changed and why).

## Checklist

- [ ] `ruff check dspace_sentinel tests` is clean
- [ ] `pytest --ignore=tests/e2e` passes locally
- [ ] Python 3.10 compatible (`from __future__ import annotations`, no 3.11+ syntax)
- [ ] No writes to DSpace, PostgreSQL, Solr or their files; every external call has a timeout
- [ ] Secrets never reach logs, events, reports or API payloads
- [ ] New behaviour has tests; bug fixes have a regression test
- [ ] `CHANGELOG.md` has an entry under **Unreleased** (if users would notice)
- [ ] UI changes: raw values only in `css/tokens.css`, verified at 1440 px light and dark,
      screenshots attached

## Screenshots / output

<!-- UI: before/after at 1440 px. CLI: paste the terminal output. -->
