"""Integration tests for the coordinator's session handling.

The important behaviour here is defensive: the portal must not be hammered with
login attempts, because a locked Mitnetz account is a far worse outcome than a
temporarily unavailable sensor.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.helpers.update_coordinator import UpdateFailed
import pytest

from custom_components.mitnetz_edm.api import (
    MitnetzAuthError,
    MitnetzSessionExpiredError,
)
from custom_components.mitnetz_edm.const import (
    DOMAIN,
    LOGIN_RETRY_DELAYS,
    MAX_LOGIN_ATTEMPTS,
)

from .conftest import LINE_BEZUG, LINE_MAX_EINSPEISUNG


@pytest.fixture(autouse=True)
def _env(integration_env):
    yield


@pytest.fixture
def no_sleep():
    """Skip the real backoff delays but record what was requested."""
    with patch(
        "custom_components.mitnetz_edm.coordinator.asyncio.sleep", new=AsyncMock()
    ) as sleep:
        yield sleep


# ------------------------------------------------------- session recovery ---


async def test_expired_session_triggers_relogin(hass, init_integration, mock_api):
    """A mid-update expiry must re-login once and then succeed."""
    coordinator = init_integration.runtime_data.coordinator
    mock_api.login.reset_mock()

    calls = {"count": 0}

    async def _fail_once(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise MitnetzSessionExpiredError("session gone")
        return await _original()

    original_points = mock_api.get_measurement_points.return_value

    async def _original():
        return original_points

    mock_api.get_measurement_points.side_effect = _fail_once

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    mock_api.login.assert_awaited_once()
    assert coordinator.last_update_success is True


async def test_relogin_not_attempted_while_session_valid(
    hass, init_integration, mock_api
):
    """No redundant logins on a healthy poll."""
    coordinator = init_integration.runtime_data.coordinator
    mock_api.login.reset_mock()

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    mock_api.login.assert_not_awaited()


# ------------------------------------------------------ lockout protection ---


async def test_login_retries_are_capped(hass, init_integration, mock_api, no_sleep):
    """Never more than MAX_LOGIN_ATTEMPTS logins in a single update cycle."""
    coordinator = init_integration.runtime_data.coordinator
    mock_api.login.reset_mock()
    mock_api.is_logged_in = False
    mock_api.login.side_effect = MitnetzAuthError("bad credentials")

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert mock_api.login.await_count == MAX_LOGIN_ATTEMPTS
    assert coordinator.last_update_success is False


async def test_backoff_delays_follow_schedule(
    hass, init_integration, mock_api, no_sleep
):
    """Waits must be 5s / 15s between the capped attempts."""
    coordinator = init_integration.runtime_data.coordinator
    mock_api.is_logged_in = False
    mock_api.login.side_effect = MitnetzAuthError("nope")

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    # asyncio.sleep(0) is used elsewhere as a plain loop yield, so only the
    # real backoff waits are of interest here.
    backoff_delays = [
        call_.args[0] for call_ in no_sleep.await_args_list if call_.args[0] > 0
    ]
    # One delay fewer than attempts: no wait after the final failure.
    assert backoff_delays == list(LOGIN_RETRY_DELAYS[: MAX_LOGIN_ATTEMPTS - 1])


async def test_no_further_logins_after_giving_up(
    hass, init_integration, mock_api, no_sleep
):
    """The next poll must not resume hammering the portal beyond the cap."""
    coordinator = init_integration.runtime_data.coordinator
    mock_api.is_logged_in = False
    mock_api.login.side_effect = MitnetzAuthError("nope")

    await coordinator.async_refresh()
    await hass.async_block_till_done()
    first_round = mock_api.login.await_count

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    # Each cycle is independently capped; no runaway accumulation.
    assert mock_api.login.await_count - first_round == MAX_LOGIN_ATTEMPTS


async def test_persistent_notification_created_on_lockout_risk(
    hass, init_integration, mock_api, no_sleep
):
    """The user must be told, since automatic recovery has stopped."""
    coordinator = init_integration.runtime_data.coordinator
    mock_api.is_logged_in = False
    mock_api.login.side_effect = MitnetzAuthError("nope")

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    from homeassistant.components import persistent_notification as pn

    # Persistent notifications are no longer entities; they live in hass.data.
    notifications = pn._async_get_or_create_notifications(hass)
    assert f"{DOMAIN}_login_failed" in notifications


async def test_coordinator_unavailable_after_exhausted_logins(
    hass, init_integration, mock_api, no_sleep
):
    coordinator = init_integration.runtime_data.coordinator
    mock_api.is_logged_in = False
    mock_api.login.side_effect = MitnetzAuthError("nope")

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is False
    assert isinstance(coordinator.last_exception, UpdateFailed)


async def test_recovers_on_later_poll_once_credentials_work(
    hass, init_integration, mock_api, no_sleep
):
    """After a failure the integration must heal itself without a restart."""
    coordinator = init_integration.runtime_data.coordinator
    mock_api.is_logged_in = False
    mock_api.login.side_effect = MitnetzAuthError("nope")

    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.last_update_success is False

    mock_api.login.side_effect = None
    mock_api.is_logged_in = True

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True


# ----------------------------------------------------------- reload window ---


async def test_reload_window_used_on_first_fetch(
    hass, init_integration, mock_api, config_entry_options
):
    """The first poll reaches back by the configured window."""
    from custom_components.mitnetz_edm.const import CONF_RELOAD_WINDOW

    window = timedelta(minutes=config_entry_options[CONF_RELOAD_WINDOW])
    _, kwargs = mock_api.get_timeseries.await_args_list[0]
    span = kwargs["end"] - kwargs["start"]

    assert abs(span - window) < timedelta(seconds=5)


async def test_overlap_applied_on_subsequent_fetch(hass, init_integration, mock_api):
    """Later polls re-fetch from the last known timestamp minus the overlap.

    This is what lets late-arriving meter values get corrected.
    """
    coordinator = init_integration.runtime_data.coordinator
    mock_api.get_timeseries.reset_mock()

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    last_known = coordinator.data.lines[LINE_BEZUG].last_timestamp
    bezug_call = next(
        kwargs
        for _, kwargs in mock_api.get_timeseries.await_args_list
        if kwargs["line"] == LINE_BEZUG
    )

    assert bezug_call["start"] < last_known


# --------------------------------------------------------------- statistics ---


async def test_statistics_imported_for_every_line(
    hass, init_integration, mock_statistics
):
    imported_lines = {
        call_.kwargs["line_id"] for call_ in mock_statistics.await_args_list
    }
    assert LINE_BEZUG in imported_lines
    assert LINE_MAX_EINSPEISUNG in imported_lines


async def test_statistics_receive_all_interval_points(
    hass, init_integration, mock_statistics
):
    """Every fetched interval must be handed to statistics, not just the last."""
    max_call = next(
        call_
        for call_ in mock_statistics.await_args_list
        if call_.kwargs["line_id"] == LINE_MAX_EINSPEISUNG
    )
    assert len(max_call.kwargs["points"]) == 4


async def test_statistics_failure_does_not_break_update(
    hass, init_integration, mock_statistics
):
    """Statistics are best effort; a failure must not take the sensors down."""
    coordinator = init_integration.runtime_data.coordinator
    mock_statistics.side_effect = RuntimeError("recorder unavailable")

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True


# ------------------------------------------------------------------ unload ---


async def test_entry_unloads_cleanly(hass, init_integration):
    from homeassistant.config_entries import ConfigEntryState
    from homeassistant.const import STATE_UNAVAILABLE

    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()

    assert init_integration.state is ConfigEntryState.NOT_LOADED
    # Home Assistant keeps a restored placeholder state after unloading.
    assert hass.states.get("sensor.mitnetz_strom_edm_bezug").state == STATE_UNAVAILABLE
