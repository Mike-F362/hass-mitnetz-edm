# Mitnetz Strom EDM – Home Assistant Integration

> MITNETZ STROM and its logo are trademarks of MITNETZ STROM GmbH. The name is
> used here solely to identify the portal this integration talks to. This
> project is unofficial, is not affiliated with MITNETZ STROM GmbH, and is
> neither endorsed nor authorised by them. No logo is bundled in this
> repository; the integration icon is submitted separately to the
> [home-assistant/brands](https://github.com/home-assistant/brands) repository.

*Deutsche Fassung: [README_de.md](README_de.md)*

Custom integration for the Mitnetz Strom smart meter portal
(`https://edm.mitnetz-strom.de`). Values are fetched purely over the HTTP API
using `aiohttp` — no Selenium, no Playwright, no browser.

## How it works

1. **Login**: `GET /login` (collects session cookies and the CSRF token from
   the HTML), then `POST /login_check` with username, password and CSRF token.
   Success is detected from the redirect to `/analytics`.
2. **Measurement points**: `GET /sidebarMultiMp/rlm` returns every measurement
   point and its OBIS lines automatically — nothing to configure by hand. The
   response nests the entries under a top-level `root` node; the bare OBIS code
   is extracted from `details."measured quantity"` (or, failing that, from the
   part of `name` before the first comma).
3. **Timeseries**: `GET /data/mpline/genericto/<mp>/<line>/<from>/<to>/base`
   returns the actual readings. The last path segment is the aggregation:
   `base` (native resolution), `hours`, `days` and so on.
4. **Session recovery**: on 401/403, a redirect to `/login`, or HTML where JSON
   was expected, the client logs in again (max. 3 attempts, backoff
   5 s / 15 s / 30 s). Once those are exhausted no further login attempts are
   made — this protects the account from being locked out. The coordinator goes
   `unavailable`, an error is logged and a persistent notification is created.

## Installation (HACS)

1. Add `https://github.com/Mike-F362/hass-mitnetz-edm` as a "Custom repository"
   (category: Integration) in HACS.
2. Install "Mitnetz Strom EDM".
3. Restart Home Assistant.
4. Settings → Devices & Services → Add Integration → "Mitnetz Strom EDM".

## Manual installation

Copy the `custom_components/mitnetz_edm` folder into the `custom_components`
directory of your Home Assistant configuration and restart Home Assistant.

## Configuration

The setup dialog asks for:

- **Username / password** — credentials for the EDM portal.
- **Update interval** — 1–1440 minutes (default: 15). Changeable later via the
  options flow.
- **Reload window** — 15 minutes to 30 days (default: 24 hours). Every poll
  refetches from the last known timestamp minus this overlap up to "now", so
  readings that arrive late are corrected automatically.
- **Value valid for max. N measurement intervals** — 1–96 (default: 3). See
  [Stale values](#stale-values).
- **Timeout** — HTTP timeout in seconds.

Credentials can be changed later without deleting the entry: Settings →
Devices & Services → Mitnetz Strom EDM → ⋮ → **Reconfigure**. The new
credentials are verified with a real login before they are stored, so a typo
cannot lock you out of a working entry — and the entity registry (and with it
the recorded history) survives the change.

## Sensors

One sensor is created per OBIS line reported by the portal. Unit, device class
and display name are derived from the OBIS code rather than assumed — see
[Units and quantities](#units-and-quantities). Unknown codes still get a
sensor, but without a unit or device class instead of a wrong one.

Per-sensor attributes: `obis_code`, `measurement_point`, `quantity`,
`direction`, `interval_start`, `interval_end`, `interval_minutes`,
`data_age_minutes`, `intervals_in_window`.

### Derived power sensors

Every interval-energy line (OBIS `D` = 29) additionally gets a **kW sensor**,
e.g. "Import Power" and "Export Power". It carries the average power
of the most recent measurement interval:

    P[kW] = E[kWh] / t[h]

For the usual 15-minute interval that is simply a factor of four. One value per
interval, so the history graph shows a real power curve rather than a single
daily peak.

This is not a home-grown approximation. The portal's own endpoint

    /data/timeseries/moment/0/<start>/<value>/<end>/<rkmgId>/<period>

performs exactly this division server-side — `<value>` is an *input*, the
largest interval energy in kWh, and `<rkmgId>` is the line id found in the
`rkmgId` field of the timeseries response. Probing it confirms the relation:

| Input (kWh) | Period    | Output (kW) |
|-------------|-----------|-------------|
| 1.1104      | `15mins`  | 4.4416      |
| 2.2208      | `15mins`  | 8.8832      |
| 2.0         | `15mins`  | 8           |
| 1.1104      | `hours`   | 1.1104      |

Since the interval is already known from the timestamps, the integration does
the division locally instead of issuing an extra HTTP request per value.

Meters that report native power lines (`D` = 4/5/6/7) keep those and get no
derived twin. Cumulative registers (`D` = 8/9) are not converted.

### Where the min/max figures live

Home Assistant stores power statistics as `mean`/`min`/`max` per hour, so the
derived kW series gives you the per-hour minimum, maximum and average for any
period via the statistics graph card — no extra sensor needed. This mirrors
what the portal shows: requesting the timeseries with `aggregationperiod=hours`
returns populated `minima`/`maxima`/`minTimestamps`/`maxTimestamps` arrays
holding the extremes of the four quarter-hours in each hour. At the base
15-minute resolution those arrays are empty, because there is nothing left to
aggregate.

## Units and quantities

The integration derives the physical meaning of each line from its OBIS code
(IEC 62056-61) instead of assuming kWh throughout. The `D` field decides:

| `D` field | Meaning                | Unit | `state_class`      |
|-----------|------------------------|------|--------------------|
| 4/5       | Average demand (power) | kW   | `measurement`      |
| 6         | Maximum demand (power) | kW   | `measurement`      |
| 7         | Instantaneous value    | kW   | `measurement`      |
| 8         | Meter reading          | kWh  | `total_increasing` |
| 29        | Interval energy        | kWh  | `measurement`      |

The `C` field gives the direction (`1` = import, the portal's "Bezug"; `2` =
export, "Einspeisung"), which is combined into the display name, e.g.
`1-1:2.6.0` → "Export Maximum".

All display names are English and collected in `const.py` (`NAME_*`), so they
can be reviewed and changed in one place. Renaming them leaves entity IDs and
recorded history intact, because the entity registry is keyed by unique ID.

This matters: maximum and average lines are **power figures in kW** that rise
*and fall* every measurement interval. Publishing them as `total_increasing`
would make Home Assistant read every decrease as a meter reset, which is what
produced the nonsensical staircase graphs.

## Measurement intervals, freshness and the history graph

### Which edge of the interval a timestamp names

The portal labels an interval by its **end**. Its own CSV export makes this
explicit: a value of 0.803275 kWh listed for the row "14:00:00 – 14:15:00"
appears in the summary as "local maximum 0.803275 kWh, Maximum time 14:15".

The JSON response carries both edges — `timestampsLeft` (start) and
`timestamps` (end); in a captured response the two differ by exactly one
interval for all 157 points. The integration anchors every point to the
**start**, preferring `timestampsLeft` and falling back to shifting
`timestamps` back by the median gap when that field is absent.

This matters more than it looks. Reading `timestamps` and treating it as the
start pushes every value one interval into the future, so the Home Assistant
curve trails the portal's chart, and an interval landing on an hour boundary
(14:45–15:00, labelled 15:00) is aggregated into the *following* hour of
long-term statistics.

### Interval detection

The measurement interval is derived from the timestamps in the portal data
(median of the gaps, so a single missing interval or a DST jump does not skew
the result).

**Important:** Home Assistant cannot backdate entity states — a state is always
recorded at poll time. If one poll returns several new intervals at once, only
the newest one reaches the state machine; the rest exist in long-term
statistics only (with their correct timestamps). For a complete history graph,
keep the update interval at or below the measurement interval; otherwise a
warning is logged.

### Stale values

Min/max and power values (OBIS `D` = 4/5/6/7) are only ever valid for *one*
measurement interval. If no newer value arrives, the sensor reports `unknown`
after `stale_intervals` intervals instead of repeating the old number. Without
that rule the last peak is dragged across the graph as a flat plateau — even
overnight, when nothing is being fed in at all.

Cumulative meter readings (OBIS `D` = 8/9) are exempt: a meter reading stays
valid until a newer one supersedes it.

Configurable via the **"Value valid for max. N measurement intervals"** option
(default: 3, range 1–96).

## Long-term statistics

The portal delivers readings retroactively: a poll at 12:00 may contain values
for 09:15, 09:30 and so on. A sensor state can only ever represent "value at
poll time", so the integration additionally writes the intervals as *external*
statistics under the `mitnetz_edm:` prefix, where each value keeps its own
timestamp.

Statistics rows must be aligned to full hours, so quarter-hourly values are
aggregated per hour:

- Energy lines → `sum` (running total, usable in the Energy Dashboard)
- Power lines → `mean` / `min` / `max`

Each interval-energy line contributes two series: its energy under
`mitnetz_edm:<line_id>` and the derived power under
`mitnetz_edm:<line_id>_power`.

The reload window deliberately overlaps hours that were already written so late
readings get corrected. The sum calculation starts from the hour *before* the
import window, so nothing is counted twice.

## Diagnostics

"Download diagnostics" exposes session age, last login, last update, the number
of measurement points and sensors, the detected measurement interval and the
last API response time. Credentials are redacted.

## Service: `mitnetz_edm.import_history`

Backfills an arbitrary period on demand:

```yaml
service: mitnetz_edm.import_history
data:
  start: "2026-06-01T00:00:00"
  end: "2026-06-30T23:59:59"
```

## Notes

- Only `aiohttp` is used (no `requests`, no blocking calls, no threads).
- An explicit `Accept-Language` header is sent: the portal formats some numeric
  fields locale-dependently and would otherwise return German number formatting
  (comma as decimal separator), which `float()` cannot parse.
- Measurement point IDs are not configured; they are discovered at runtime via
  the API.
- The integration depends on `recorder` for the long-term statistics import.

## Tests

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-test.txt
pytest tests/ --cov=custom_components.mitnetz_edm
```

The suite runs against a real Home Assistant instance (including the recorder)
via `pytest-homeassistant-custom-component`, which requires the Python version
Home Assistant itself requires (3.13+). HTTP is faked with that package's own
`aioclient_mock` fixture rather than `aioresponses`, which broke when aiohttp
changed an internal signature.

| File                           | Focus                                                          |
|--------------------------------|----------------------------------------------------------------|
| `test_obis.py`                 | OBIS classification, the kW-instead-of-kWh regression           |
| `test_power.py`                | Energy-to-power conversion against portal-verified values       |
| `test_interval_edges.py`       | Points anchored to the interval start, checked against a capture |
| `test_derived_power_sensor.py` | Derived kW entity, staleness, separate statistics id            |
| `test_api.py`                  | Login/CSRF, XSRF and XHR headers, date format, session expiry, `root` unwrapping, OBIS extraction |
| `test_coordinator.py`          | Re-login, backoff, lockout protection, reload window            |
| `test_sensor.py`               | Units, state classes, value follows the interval                |
| `test_interval_staleness.py`   | Interval detection and the staleness rule                       |
| `test_statistics.py`           | Hourly aggregation (pure logic)                                 |
| `test_statistics_import.py`    | Writing to the recorder, no double counting                     |
| `test_config_flow.py`          | Setup, error cases, options flow, reconfigure flow              |
| `test_diagnostics_services.py` | Diagnostics without credentials, `import_history`               |

## Development

```bash
ruff check .          # lint
ruff format .         # format
pytest -m "not live"  # the offline suite
```

Ruff and mypy are configured in `pyproject.toml`. CI runs ruff, hassfest, HACS
validation and the tests on every push and pull request, plus weekly — Home
Assistant releases monthly and hassfest rules change with it.

## Acknowledgements

Developed with the assistance of Claude (Anthropic).

## Licence

[MIT](LICENSE). MITNETZ STROM and its logo are trademarks of MITNETZ STROM GmbH;
the licence grants no rights to those marks.
