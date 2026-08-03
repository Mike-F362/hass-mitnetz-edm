# Mitnetz Strom EDM – Home Assistant Integration

<img src="logo.mitnetz.svg" alt="Mitnetz Strom logo" width="200">

> The Mitnetz Strom logo is a trademark of MITNETZ STROM GmbH and is used here
> solely to identify the portal this integration talks to. This project is
> unofficial, is not affiliated with MITNETZ STROM GmbH, and is neither
> endorsed nor authorised by them.

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
   returns the actual readings.
4. **Session recovery**: on 401/403, a redirect to `/login`, or HTML where JSON
   was expected, the client logs in again (max. 3 attempts, backoff
   5 s / 15 s / 30 s). Once those are exhausted no further login attempts are
   made — this protects the account from being locked out. The coordinator goes
   `unavailable`, an error is logged and a persistent notification is created.

## Installation (HACS)

1. Add this repository as a "Custom repository" (category: Integration) in
   HACS.
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

The `C` field gives the direction (`1` = import/"Bezug", `2` =
export/"Einspeisung"), which is combined into the display name, e.g.
`1-1:2.6.0` → "Einspeisung Maximum".

This matters: maximum and average lines are **power figures in kW** that rise
*and fall* every measurement interval. Publishing them as `total_increasing`
would make Home Assistant read every decrease as a meter reset, which is what
produced the nonsensical staircase graphs.

## Measurement intervals, freshness and the history graph

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
Home Assistant itself requires (3.13+).

| File                           | Focus                                                          |
|--------------------------------|----------------------------------------------------------------|
| `test_obis.py`                 | OBIS classification, the kW-instead-of-kWh regression           |
| `test_api.py`                  | Login/CSRF, XSRF and XHR headers, date format, session expiry, `root` unwrapping, OBIS extraction |
| `test_coordinator.py`          | Re-login, backoff, lockout protection, reload window            |
| `test_sensor.py`               | Units, state classes, value follows the interval                |
| `test_interval_staleness.py`   | Interval detection and the staleness rule                       |
| `test_statistics.py`           | Hourly aggregation (pure logic)                                 |
| `test_statistics_import.py`    | Writing to the recorder, no double counting                     |
| `test_config_flow.py`          | Setup, error cases, options flow, reconfigure flow              |
| `test_diagnostics_services.py` | Diagnostics without credentials, `import_history`               |
