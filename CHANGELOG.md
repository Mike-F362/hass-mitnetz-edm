# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Because this is a Home Assistant integration rather than a library, "breaking"
means anything that changes entity IDs, unique IDs, units, state classes or
statistic IDs — that is what silently breaks a user's dashboards, automations
and recorded history.

## [Unreleased]

### Added

- Nothing yet.

## [1.0.0] - 2026-08-08

First public release. Merged from two parallel development branches; the
entries below describe the resulting integration rather than that history.

### Added

- Automatic discovery of measurement points and OBIS lines — no manual
  configuration of meter IDs.
- Unit and device class derived from the OBIS code (IEC 62056-61) instead of
  assuming kWh throughout. The `D` field decides energy vs. power, the `C`
  field the direction.
- Derived average-power sensor in kW for every interval-energy line
  (`P = E / t`, i.e. ×4 at the usual quarter-hour resolution). Meters whose
  portal account exposes no native power line get a real power curve this way.
- Long-term statistics import under the `mitnetz_edm:` prefix, so backfilled
  intervals keep their own timestamps instead of landing at poll time. Energy
  lines are summed; power lines get `mean`/`min`/`max` per hour, which is where
  per-hour peaks can be queried.
- Staleness handling: a demand value reports `unknown` after a configurable
  number of intervals rather than being carried forward as a flat plateau.
  Cumulative registers are exempt.
- Reconfigure flow for updating credentials without deleting the config entry,
  preserving the entity registry and its recorded history.
- Diagnostics with session age, last login, detected measurement interval and
  entity counts, with credentials redacted.
- `mitnetz_edm.import_history` service for backfilling an arbitrary period.
- Warning when the update interval is longer than the meter's measurement
  interval, since Home Assistant cannot backdate entity states.
- Test suite running against a real Home Assistant instance including the
  recorder, plus an opt-in suite against the live portal
  (`pytest -m live`, see `tests/live_config.example.json`).

### Fixed

- Points are anchored to the **start** of their measurement interval. The
  portal's `timestamps` array holds the interval *end* and ships a parallel
  `timestampsLeft` with the start; reading the former as the start pushed every
  value one interval into the future and mis-bucketed hour-boundary intervals
  in long-term statistics.
- The `root` node of `/sidebarMultiMp/rlm` is unwrapped. Without this, accounts
  whose response uses the wrapper discovered zero measurement points and
  created no sensors at all.
- The bare OBIS code is extracted from a line entry. The portal's `name` is
  `"<obis>, <meter id>"`, which the classifier cannot parse — every line would
  have been classified as unknown, with no unit and no device class.
- An explicit `Accept-Language` header is sent, because the portal formats some
  numeric fields with a German decimal comma that `float()` rejects.
- Timestamps are timezone-aware throughout; naive values are read as portal
  local time rather than UTC.
- Services are only unregistered once no *loaded* config entry needs them.

### Security

- No portal responses, exported reports or real meter identifiers are included.
  A meter point designation combined with a quarter-hourly load profile is
  personal data under the GDPR: load profiles reveal occupancy patterns and are
  well known to be re-identifiable. Test fixtures are pseudonymised, and
  credentials for the live tests are read from a git-ignored config file.

[Unreleased]: https://github.com/OWNER/mitnetz_edm/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/OWNER/mitnetz_edm/releases/tag/v1.0.0
