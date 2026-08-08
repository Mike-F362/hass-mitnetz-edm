"""Integration tests for the config and options flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResultType
import pytest
import voluptuous as vol

from custom_components.mitnetz_edm.api import MitnetzApiError, MitnetzAuthError
from custom_components.mitnetz_edm.const import (
    CONF_RELOAD_WINDOW,
    CONF_TIMEOUT,
    CONF_UPDATE_INTERVAL,
    DOMAIN,
)


@pytest.fixture(autouse=True)
def _env(integration_env):
    yield


@pytest.fixture
def mock_flow_client():
    """Patch the client used by the config flow for credential validation."""
    with patch(
        "custom_components.mitnetz_edm.config_flow.MitnetzEdmApiClient", autospec=True
    ) as client_cls:
        client_cls.return_value.login = AsyncMock()
        yield client_cls.return_value


USER_INPUT = {
    CONF_USERNAME: "test-user",
    CONF_PASSWORD: "test-password",
    CONF_UPDATE_INTERVAL: 15,
    CONF_RELOAD_WINDOW: 1440,
    CONF_TIMEOUT: 30,
}


async def test_user_flow_creates_entry(hass, mock_flow_client):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    with patch("custom_components.mitnetz_edm.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "test-user"
    assert result["data"][CONF_USERNAME] == "test-user"
    # The password is stored in data, never in options.
    assert CONF_PASSWORD not in result["options"]


async def test_user_flow_validates_credentials(hass, mock_flow_client):
    """The flow must actually attempt a login before creating the entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch("custom_components.mitnetz_edm.async_setup_entry", return_value=True):
        await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    mock_flow_client.login.assert_awaited_once()


async def test_invalid_auth_shows_error(hass, mock_flow_client):
    mock_flow_client.login.side_effect = MitnetzAuthError("bad credentials")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_cannot_connect_shows_error(hass, mock_flow_client):
    mock_flow_client.login.side_effect = MitnetzApiError("network down")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_recovers_after_failed_attempt(hass, mock_flow_client):
    """A corrected password on the second try must succeed."""
    mock_flow_client.login.side_effect = MitnetzAuthError("bad credentials")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM

    mock_flow_client.login.side_effect = None
    with patch("custom_components.mitnetz_edm.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_duplicate_account_is_rejected(hass, mock_flow_client):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    MockConfigEntry(
        domain=DOMAIN, unique_id="test-user", data={CONF_USERNAME: "test-user"}
    ).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (CONF_UPDATE_INTERVAL, 0),  # below minimum
        (CONF_UPDATE_INTERVAL, 1441),  # above maximum
        (CONF_RELOAD_WINDOW, 14),  # below 15 minutes
        (CONF_RELOAD_WINDOW, 30 * 24 * 60 + 1),  # above 30 days
    ],
)
async def test_out_of_range_values_rejected(hass, mock_flow_client, field, value):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    bad_input = {**USER_INPUT, field: value}

    # voluptuous rejects the value before the flow ever sees it.
    with pytest.raises(vol.Invalid):
        await hass.config_entries.flow.async_configure(result["flow_id"], bad_input)


async def test_options_flow_updates_interval(hass, init_integration):
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_UPDATE_INTERVAL: 60,
            CONF_RELOAD_WINDOW: 2880,
            CONF_TIMEOUT: 45,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert init_integration.options[CONF_UPDATE_INTERVAL] == 60
    assert init_integration.options[CONF_RELOAD_WINDOW] == 2880


async def test_options_change_applies_to_coordinator(hass, init_integration):
    """Changing the interval must actually reload the coordinator."""
    from datetime import timedelta

    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_UPDATE_INTERVAL: 60,
            CONF_RELOAD_WINDOW: 2880,
            CONF_TIMEOUT: 45,
        },
    )
    await hass.async_block_till_done()

    coordinator = init_integration.runtime_data.coordinator
    assert coordinator.update_interval == timedelta(minutes=60)
    assert coordinator.reload_window == timedelta(minutes=2880)


# ------------------------------------------------------------ reconfigure ---


async def test_reconfigure_updates_credentials(
    hass, init_integration, mock_flow_client
):
    """Rotated credentials must be fixable without losing the entity history."""
    result = await init_integration.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: "new-user", CONF_PASSWORD: "new-password"},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert init_integration.data[CONF_USERNAME] == "new-user"
    assert init_integration.data[CONF_PASSWORD] == "new-password"


async def test_reconfigure_rejects_bad_credentials(
    hass, init_integration, mock_flow_client
):
    mock_flow_client.login.side_effect = MitnetzAuthError("bad credentials")

    result = await init_integration.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: "new-user", CONF_PASSWORD: "wrong"},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    # Nothing is written until the login succeeds.
    assert init_integration.data[CONF_USERNAME] != "new-user"
