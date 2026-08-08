"""The Mitnetz Strom EDM integration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import MitnetzEdmApiClient
from .const import (
    CONF_RELOAD_WINDOW,
    CONF_STALE_INTERVALS,
    CONF_TIMEOUT,
    CONF_UPDATE_INTERVAL,
    DEFAULT_RELOAD_WINDOW_HOURS,
    DEFAULT_STALE_INTERVALS,
    DEFAULT_TIMEOUT_SEC,
    DEFAULT_UPDATE_INTERVAL_MIN,
    PLATFORMS,
)
from .coordinator import MitnetzEdmCoordinator
from .services import async_setup_services, async_unload_services

_LOGGER = logging.getLogger(__name__)

type MitnetzEdmConfigEntry = ConfigEntry[MitnetzEdmRuntimeData]


@dataclass(slots=True)
class MitnetzEdmRuntimeData:
    """Data stored on the config entry at runtime (not persisted)."""

    api: MitnetzEdmApiClient
    coordinator: MitnetzEdmCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: MitnetzEdmConfigEntry) -> bool:
    """Set up Mitnetz EDM from a config entry."""
    # The portal formats some numeric fields as locale-dependent strings;
    # without an explicit Accept-Language it defaults to German formatting
    # (comma as decimal separator), which float() cannot parse.
    session = async_create_clientsession(
        hass, headers={"Accept-Language": "en-US,en;q=0.9,de-DE;q=0.8,de;q=0.7"}
    )

    timeout = entry.options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT_SEC)
    api_client = MitnetzEdmApiClient(
        session=session,
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        timeout=timeout,
    )

    update_interval = timedelta(
        minutes=entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL_MIN)
    )
    reload_window = timedelta(
        minutes=entry.options.get(CONF_RELOAD_WINDOW, DEFAULT_RELOAD_WINDOW_HOURS * 60)
    )

    edm_coordinator = MitnetzEdmCoordinator(
        hass,
        config_entry=entry,
        api=api_client,
        update_interval=update_interval,
        reload_window=reload_window,
        stale_intervals=entry.options.get(
            CONF_STALE_INTERVALS, DEFAULT_STALE_INTERVALS
        ),
    )

    await edm_coordinator.async_config_entry_first_refresh()

    entry.runtime_data = MitnetzEdmRuntimeData(
        api=api_client, coordinator=edm_coordinator
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_update_listener))

    async_setup_services(hass)

    return True


async def async_update_listener(
    hass: HomeAssistant, entry: MitnetzEdmConfigEntry
) -> None:
    """Reload the entry when options change (interval/reload window/timeout)."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: MitnetzEdmConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        async_unload_services(hass, entry)
    return unloaded
