"""Options flow for the Mitnetz Strom EDM integration."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlowResult, OptionsFlow
import voluptuous as vol

from .const import (
    CONF_RELOAD_WINDOW,
    CONF_STALE_INTERVALS,
    CONF_TIMEOUT,
    CONF_UPDATE_INTERVAL,
    DEFAULT_RELOAD_WINDOW_HOURS,
    DEFAULT_STALE_INTERVALS,
    DEFAULT_TIMEOUT_SEC,
    DEFAULT_UPDATE_INTERVAL_MIN,
    MAX_RELOAD_WINDOW_MIN,
    MAX_STALE_INTERVALS,
    MAX_UPDATE_INTERVAL_MIN,
    MIN_RELOAD_WINDOW_MIN,
    MIN_STALE_INTERVALS,
    MIN_UPDATE_INTERVAL_MIN,
)


class MitnetzEdmOptionsFlow(OptionsFlow):
    """Allow the user to change polling interval, reload window and timeout."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and store the polling options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_UPDATE_INTERVAL,
                    default=options.get(
                        CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL_MIN
                    ),
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_UPDATE_INTERVAL_MIN, max=MAX_UPDATE_INTERVAL_MIN),
                ),
                vol.Required(
                    CONF_RELOAD_WINDOW,
                    default=options.get(
                        CONF_RELOAD_WINDOW, DEFAULT_RELOAD_WINDOW_HOURS * 60
                    ),
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_RELOAD_WINDOW_MIN, max=MAX_RELOAD_WINDOW_MIN),
                ),
                vol.Required(
                    CONF_STALE_INTERVALS,
                    default=options.get(CONF_STALE_INTERVALS, DEFAULT_STALE_INTERVALS),
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_STALE_INTERVALS, max=MAX_STALE_INTERVALS),
                ),
                vol.Required(
                    CONF_TIMEOUT,
                    default=options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT_SEC),
                ): vol.All(vol.Coerce(int), vol.Range(min=5, max=120)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
