"""Constants for the Mitnetz Strom EDM integration."""
from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "mitnetz_edm"

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

# --- OBIS code mapping -------------------------------------------------------
OBIS_BEZUG: Final = "1-1:1.29.0"
OBIS_EINSPEISUNG: Final = "1-1:2.29.0"

OBIS_FRIENDLY_NAMES: Final = {
    OBIS_BEZUG: "Bezug",
    OBIS_EINSPEISUNG: "Einspeisung",
}

# --- Misc ---------------------------------------------------------------
PLATFORMS: Final = ["sensor"]
SCAN_INTERVAL_FALLBACK: Final = timedelta(minutes=DEFAULT_UPDATE_INTERVAL_MIN)

SERVICE_IMPORT_HISTORY: Final = "import_history"
ATTR_START: Final = "start"
ATTR_END: Final = "end"
