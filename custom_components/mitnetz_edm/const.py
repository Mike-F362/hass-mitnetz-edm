"""Constants for the Mitnetz Strom EDM integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "mitnetz_edm"

#: Shown as the device name and prefixed to every statistic.
DEVICE_NAME: Final = "Mitnetz Strom EDM"
DEVICE_MANUFACTURER: Final = "Mitnetz Strom"
DEVICE_MODEL: Final = "EDM smart meter portal"

# --- Sensor display names ----------------------------------------------------
# All user-facing sensor names are collected here so they can be reviewed and
# changed in one place. English throughout, even though the portal itself is
# German - see NAME_HINTS_* in obis.py for the German strings that are matched
# against portal text rather than displayed.
#
# A name is assembled as "<direction> <qualifier>", e.g. "Export Maximum".
# Changing these renames existing entities but leaves their entity_id and
# recorded history intact, because the entity registry is keyed by unique_id.

#: ``C`` field of the OBIS code -> direction shown to the user.
NAME_DIRECTION_IMPORT: Final = "Import"
NAME_DIRECTION_EXPORT: Final = "Export"

#: ``D`` field of the OBIS code -> qualifier appended to the direction.
NAME_AVERAGE: Final = "Average"
NAME_LAST_AVERAGE: Final = "Last Average"
NAME_MAXIMUM: Final = "Maximum"
NAME_INSTANTANEOUS: Final = "Instantaneous"
NAME_METER_READING: Final = "Meter Reading"

#: Appended to an interval-energy line's name for its derived kW sensor.
NAME_POWER_SUFFIX: Final = "Power"

# --- Config entry keys -----------------------------------------------------
CONF_UPDATE_INTERVAL: Final = "update_interval"
CONF_RELOAD_WINDOW: Final = "reload_window"
CONF_TIMEOUT: Final = "timeout"
CONF_STALE_INTERVALS: Final = "stale_intervals"

#: How many measurement intervals a value may age before the sensor reports
#: ``unknown`` instead of holding the last value. Holding a stale value is what
#: turns a per-interval min/max curve into a meaningless plateau.
DEFAULT_STALE_INTERVALS: Final = 3
MIN_STALE_INTERVALS: Final = 1
MAX_STALE_INTERVALS: Final = 96  # a full day at 15-minute intervals

#: Fallback interval assumed when it cannot be derived from the data.
FALLBACK_MEASUREMENT_INTERVAL_MIN: Final = 15

#: How many whole local days every poll re-fetches, regardless of the reload
#: window. The portal publishes *provisional* (estimated) values within
#: minutes and replaces them with the measured ones later - usually in the
#: night after the day in question, occasionally a day later still. An
#: interval that drops out of the fetch window before it is finalised keeps
#: its estimate in long-term statistics for good.
FINALISATION_LOOKBACK_DAYS: Final = 2

#: ``import_history`` fetches long ranges in chunks of this size, so a single
#: request never asks the portal for months of quarter-hours at once.
HISTORY_CHUNK: Final = timedelta(days=7)

# --- Defaults ---------------------------------------------------------------
DEFAULT_UPDATE_INTERVAL_MIN: Final = 15
DEFAULT_RELOAD_WINDOW_HOURS: Final = 24
DEFAULT_TIMEOUT_SEC: Final = 30

MIN_UPDATE_INTERVAL_MIN: Final = 1
MAX_UPDATE_INTERVAL_MIN: Final = 1440

MIN_RELOAD_WINDOW_MIN: Final = 15
MAX_RELOAD_WINDOW_MIN: Final = 30 * 24 * 60  # 30 days in minutes

# --- URLs ---------------------------------------------------------------
BASE_URL: Final = "https://edm.mitnetz-strom.de"
LOGIN_PAGE_URL: Final = f"{BASE_URL}/login"
LOGIN_CHECK_URL: Final = f"{BASE_URL}/login_check"
ANALYTICS_URL: Final = f"{BASE_URL}/analytics"
MEASUREMENT_POINTS_URL: Final = f"{BASE_URL}/sidebarMultiMp/rlm"
TIMESERIES_URL_TEMPLATE: Final = (
    f"{BASE_URL}/data/mpline/genericto/{{meterpoint}}/{{line}}/{{start}}/{{end}}/base"
)

# --- Login re-try backoff (seconds) -----------------------------------------
LOGIN_RETRY_DELAYS: Final = (5, 15, 30)
MAX_LOGIN_ATTEMPTS: Final = 3

# --- Misc ---------------------------------------------------------------
PLATFORMS: Final = ["sensor"]
SCAN_INTERVAL_FALLBACK: Final = timedelta(minutes=DEFAULT_UPDATE_INTERVAL_MIN)

SERVICE_IMPORT_HISTORY: Final = "import_history"
ATTR_START: Final = "start"
ATTR_END: Final = "end"
