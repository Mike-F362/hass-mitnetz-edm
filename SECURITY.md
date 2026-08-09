# Security policy

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Use GitHub's private reporting instead:
[Report a vulnerability](https://github.com/Mike-F362/hass-mitnetz-edm/security/advisories/new).
It is private between you and the maintainer until a fix is published.

Expect a first response within a few days. This is a spare-time project, so
please be patient — but do follow up if you hear nothing after two weeks.

## What is in scope

This integration stores your EDM portal credentials in the Home Assistant
config entry and sends them to `edm.mitnetz-strom.de` over HTTPS. Relevant
classes of problem:

- credentials or session cookies appearing in logs, diagnostics or exceptions;
- the diagnostics download exposing something it should redact;
- certificate validation being weakened or bypassed;
- an injection into the portal URLs built from measurement point or line IDs.

Out of scope: anything about the MITNETZ STROM portal itself. Report that to
MITNETZ STROM, not here.

## A note on your data

A meter point designation (Zählpunktbezeichnung) together with a
quarter-hourly load profile is personal data under the GDPR — load profiles
reveal occupancy patterns and are well known to be re-identifiable. When
attaching diagnostics or logs to *any* issue, check them first. The
diagnostics download redacts credentials, but not measurement point IDs.
