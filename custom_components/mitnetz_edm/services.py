"""Services for the Mitnetz Strom EDM integration."""
from __future__ import annotations

import logging
from datetime import datetime

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv

from .const import ATTR_END, ATTR_START, DOMAIN, SERVICE_IMPORT_HISTORY

_LOGGER = logging.getLogger(__name__)

IMPORT_HISTORY_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_START): cv.datetime,
        vol.Required(ATTR_END): cv.datetime,
    }
)


def async_setup_services(hass: HomeAssistant) -> None:
    """Register integration-level services (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_IMPORT_HISTORY):
        return

    async def _handle_import_history(call: ServiceCall) -> None:
        start: datetime = call.data[ATTR_START]
        end: datetime = call.data[ATTR_END]

        entries: list[ConfigEntry] = hass.config_entries.async_entries(DOMAIN)
        if not entries:
            _LOGGER.error("No Mitnetz EDM config entry found for import_history")
            return

        for entry in entries:
            runtime_data = getattr(entry, "runtime_data", None)
            if runtime_data is None:
                continue
            _LOGGER.debug(
                "Importing history for entry %s from %s to %s",
                entry.entry_id,
                start,
                end,
            )
            await runtime_data.coordinator.async_import_history(start, end)

    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_HISTORY,
        _handle_import_history,
        schema=IMPORT_HISTORY_SCHEMA,
    )


def async_unload_services(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Unregister services once no loaded entry needs them anymore.

    ``async_entries`` also returns entries that are merely configured, so the
    remaining *loaded* entries are what matters. The entry currently being
    unloaded is excluded explicitly, because its state has not settled yet at
    this point.
    """
    from homeassistant.config_entries import ConfigEntryState

    still_loaded = [
        other
        for other in hass.config_entries.async_entries(DOMAIN)
        if other.entry_id != entry.entry_id
        and other.state is ConfigEntryState.LOADED
    ]
    if still_loaded:
        return

    if hass.services.has_service(DOMAIN, SERVICE_IMPORT_HISTORY):
        hass.services.async_remove(DOMAIN, SERVICE_IMPORT_HISTORY)
