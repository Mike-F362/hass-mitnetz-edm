# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Because this is a Home Assistant integration rather than a library, "breaking"
means anything that changes entity IDs, unique IDs, units, state classes or
statistic IDs — that is what silently breaks a user's dashboards, automations
and recorded history.

## [Unreleased]

### Fixed

- Long-term statistics held only about a third of the real energy. Every poll
  re-fetched a window that started mid-hour and advanced by one interval per
  poll, and each poll replaced that first hour's row with the partial sum - so
  almost every hour ended up holding only its last quarter-hour. A partial
  leading hour is no longer written, and the fetch window starts on a full
  hour. Visible in existing statistics as hourly `_power` rows with
  `min == max`. Statistics written before this fix should be re-imported with
  `mitnetz_edm.import_history` up to the present.
- The portal publishes provisional estimates within minutes and replaces them
  with measured values later, usually the following night. Every poll now
  re-fetches at least back to local midnight two days ago
  (`FINALISATION_LOOKBACK_DAYS`), so those replacements reach the statistics;
  with a 24-hour window the hour after midnight kept its estimate.
- `mitnetz_edm.import_history` fetched the requested range and then discarded
  it. It now fetches in seven-day chunks, imports each line as one continuous
  series and waits for the recorder before the next line.
- When summer time ends, the hour from 02:00 to 03:00 occurs twice. Both were
  written into one statistics row holding eight quarter-hours, and the second
  hour got no row at all. Hours are now keyed in UTC. The day's total was
  always right; to correct the two hours of 25 October 2026, re-import that
  day with `mitnetz_edm.import_history` up to the present.
- The running total restarted at zero when the hour before the fetch window
  had no stored row - after Home Assistant had been offline for longer than
  the window reaches back, for instance. The Energy Dashboard shows that as
  one hour with a large negative value. The total now continues from the last
  row before the window.
- External statistics declare `mean_type` and `unit_class`; Home Assistant
  stops accepting them without both in 2026.11.
- Interval-energy sensors no longer combine device class `energy` with state
  class `measurement`, which Home Assistant rejects with a warning at every
  start. They keep `measurement` and the unit kWh but have no device class, so
  their recorded statistics stay valid. **Breaking** in the sense above: the
  `device_class` attribute disappears from these two entities.

### Added

- `pyproject.toml` with ruff and mypy configuration, and a GitHub Actions
  workflow running ruff, hassfest, HACS validation and the test suite.
- `hacs.json` declaring the minimum Home Assistant version.
- `NOTICE` carrying the trademark statement, so `LICENSE` stays a verbatim MIT
  text that GitHub's licence detection recognises.
- Community files: issue forms that require the diagnostics download and the
  OBIS codes, a pull request template, `CONTRIBUTING.md`, `SECURITY.md`,
  `CODE_OF_CONDUCT.md` and a Dependabot configuration for the workflow actions.
- At debug level the API client logs the raw values of every fetched series,
  so a processing error can be told apart from wrong portal data.

### Changed

- Sensor display names are English and collected as `NAME_*` constants in
  `const.py`. Renaming leaves entity IDs and recorded history intact, since the
  entity registry is keyed by unique ID.
- The persistent notification raised after the login retries are exhausted is
  English and points at the reconfigure flow.
- `MeasurementKind`, `Quantity` and `Direction` are `StrEnum` rather than
  `(str, Enum)`; pairwise iteration uses `itertools.pairwise`.

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

[Unreleased]: https://github.com/Mike-F362/hass-mitnetz-edm/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/Mike-F362/hass-mitnetz-edm/releases/tag/v1.0.0
