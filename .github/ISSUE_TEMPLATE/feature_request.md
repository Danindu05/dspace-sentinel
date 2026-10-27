---
name: Feature request
about: Suggest a check, module, report section or UI improvement
title: "feat: "
labels: enhancement
assignees: ""
---

## The problem

What are you trying to see, get alerted on or report that Sentinel does not
cover today? Describe the situation on your DSpace server, not the solution.

## Proposed change

How should it work? If it is a new check, describe the rungs of the ladder
(for example `service → tcp → http`), the metrics, the thresholds and the
alert message you would expect. If it is a UI change, name the tab and sketch
the tile or panel.

## Fit with the project rules

- [ ] It only observes; it never writes to or restarts DSpace, PostgreSQL,
      Solr, Tomcat, Node or nginx.
- [ ] It can be switched off (a module that is off leaves no trace).
- [ ] It works with the existing dependencies (no new services, no Node on
      the server).

## Alternatives you considered

## Additional context

Links to DSpace documentation, sample log lines, API responses.
