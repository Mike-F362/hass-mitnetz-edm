"""Config flow for the Mitnetz Strom EDM integration."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import MitnetzApiError, MitnetzAuthError, MitnetzEdmApiClient
from .const import (
    CONF_RELOAD_WINDOW,
    CONF_STALE_INTERVALS,
    CONF_TIMEOUT,
    CONF_UPDATE_INTERVAL,
    DEFAULT_RELOAD_WINDOW_HOURS,
    DEFAULT_STALE_INTERVALS,
    DEFAULT_TIMEOUT_SEC,
    DEFAULT_UPDATE_INTERVAL_MIN,
    DOMAIN,
    MAX_RELOAD_WINDOW_MIN,
    MAX_STALE_INTERVALS,
    MAX_UPDATE_INTERVAL_MIN,
    MIN_RELOAD_WINDOW_MIN,
    MIN_STALE_INTERVALS,
    MIN_UPDATE_INTERVAL_MIN,
)

_LOGGER = logging.getLogger(__name__)


class MitnetzEdmConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial setup flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_USERNAME].lower())
            self._abort_if_unique_id_configured()

            session = async_create_clientsession(self.hass)
            client = MitnetzEdmApiClient(
                session=session,
                username=user_input[CONF_USERNAME],
                password=user_input[CONF_PASSWORD],
                timeout=DEFAULT_TIMEOUT_SEC,
            )
            try:
                await client.login()
            except MitnetzAuthError:
                errors["base"] = "invalid_auth"
            except (MitnetzApiError, aiohttp.ClientError):
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title=user_input[CONF_USERNAME],
                    data={
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                    options={
                        CONF_UPDATE_INTERVAL: DEFAULT_UPDATE_INTERVAL_MIN,
                        CONF_RELOAD_WINDOW: DEFAULT_RELOAD_WINDOW_HOURS * 60,
                        CONF_STALE_INTERVALS: DEFAULT_STALE_INTERVALS,
                        CONF_TIMEOUT: DEFAULT_TIMEOUT_SEC,
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Required(
                    CONF_UPDATE_INTERVAL, default=DEFAULT_UPDATE_INTERVAL_MIN
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_UPDATE_INTERVAL_MIN, max=MAX_UPDATE_INTERVAL_MIN),
                ),
                vol.Required(
                    CONF_RELOAD_WINDOW, default=DEFAULT_RELOAD_WINDOW_HOURS * 60
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_RELOAD_WINDOW_MIN, max=MAX_RELOAD_WINDOW_MIN),
                ),
                vol.Required(
                    CONF_STALE_INTERVALS, default=DEFAULT_STALE_INTERVALS
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_STALE_INTERVALS, max=MAX_STALE_INTERVALS),
                ),
                vol.Required(CONF_TIMEOUT, default=DEFAULT_TIMEOUT_SEC): vol.All(
                    vol.Coerce(int), vol.Range(min=5, max=120)
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user update the stored username/password.

        Credentials expire or get rotated; without this step the only way to
        fix a failing login is to delete and re-add the entry, which discards
        the entity registry entries and their history.
        """
        errors: dict[str, str] = {}
        reconfigure_entry = self._get_reconfigure_entry()

        if user_input is not None:
            session = async_create_clientsession(self.hass)
            client = MitnetzEdmApiClient(
                session=session,
                username=user_input[CONF_USERNAME],
                password=user_input[CONF_PASSWORD],
                timeout=DEFAULT_TIMEOUT_SEC,
            )
            try:
                await client.login()
            except MitnetzAuthError:
                errors["base"] = "invalid_auth"
            except (MitnetzApiError, aiohttp.ClientError):
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(
                    reconfigure_entry,
                    data={
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_USERNAME, default=reconfigure_entry.data[CONF_USERNAME]
                ): str,
                vol.Required(CONF_PASSWORD): str,
            }
        )
        return self.async_show_form(
            step_id="reconfigure", data_schema=schema, errors=errors
        )

    @staticmethod
    def async_get_options_flow(_config_entry: ConfigEntry) -> OptionsFlow:
        from .options_flow import MitnetzEdmOptionsFlow

        return MitnetzEdmOptionsFlow()
