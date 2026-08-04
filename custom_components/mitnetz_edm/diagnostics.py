"""Diagnostics support for the Mitnetz Strom EDM integration."""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_RELOAD_WINDOW, CONF_STALE_INTERVALS, CONF_UPDATE_INTERVAL

TO_REDACT = {"username", "password", "_username", "_password"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime_data = entry.runtime_data
    api = runtime_data.api
    coordinator = runtime_data.coordinator

    session_age_seconds: float | None = None
    if api.last_login is not None:
        from datetime import datetime, timezone

        session_age_seconds = (
            datetime.now(tz=timezone.utc) - api.last_login
        ).total_seconds()

    return {
        "options": {
            CONF_UPDATE_INTERVAL: entry.options.get(CONF_UPDATE_INTERVAL),
            CONF_RELOAD_WINDOW: entry.options.get(CONF_RELOAD_WINDOW),
            CONF_STALE_INTERVALS: entry.options.get(CONF_STALE_INTERVALS),
        },
        "session": {
            "logged_in": api.is_logged_in,
            "last_login": api.last_login.isoformat() if api.last_login else None,
            "session_age_seconds": session_age_seconds,
            "last_request_duration_seconds": api.last_request_duration,
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "last_successful_update": (
                coordinator.data.last_successful_update.isoformat()
                if coordinator.data and coordinator.data.last_successful_update
                else None
            ),
            "measurement_point_count": coordinator.measurement_point_count,
            "sensor_count": coordinator.sensor_count,
            "line_count": coordinator.line_count,
            "derived_power_count": coordinator.derived_power_count,
            "measurement_interval_seconds": (
                coordinator.measurement_interval.total_seconds()
                if coordinator.measurement_interval
                else None
            ),
        },
    }
