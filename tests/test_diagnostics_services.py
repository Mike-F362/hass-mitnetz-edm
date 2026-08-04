"""Tests for diagnostics output and the import_history service."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import voluptuous as vol

from custom_components.mitnetz_edm.const import (
    ATTR_END,
    ATTR_START,
    DOMAIN,
    SERVICE_IMPORT_HISTORY,
)
from custom_components.mitnetz_edm.diagnostics import (
    async_get_config_entry_diagnostics,
)

BERLIN = ZoneInfo("Europe/Berlin")


@pytest.fixture(autouse=True)
def _env(integration_env):
    yield


# ------------------------------------------------------------- diagnostics ---


async def test_diagnostics_reports_session_and_counts(hass, init_integration):
    data = await async_get_config_entry_diagnostics(hass, init_integration)

    assert data["coordinator"]["measurement_point_count"] == 1
    # 3 lines + a derived power sensor for each of the 2 interval-energy lines.
    assert data["coordinator"]["line_count"] == 3
    assert data["coordinator"]["derived_power_count"] == 2
    assert data["coordinator"]["sensor_count"] == 5
    assert data["coordinator"]["last_update_success"] is True
    assert data["session"]["logged_in"] is True
    assert data["session"]["session_age_seconds"] is not None
    assert data["session"]["last_request_duration_seconds"] == 0.42


async def test_diagnostics_never_leaks_credentials(hass, init_integration):
    """A diagnostics download is shared with strangers - no secrets in it."""
    data = await async_get_config_entry_diagnostics(hass, init_integration)
    serialised = str(data).casefold()

    assert "test-password" not in serialised
    assert "password" not in serialised


async def test_diagnostics_includes_options(hass, init_integration):
    data = await async_get_config_entry_diagnostics(hass, init_integration)

    assert data["options"]["update_interval"] == 15
    assert data["options"]["reload_window"] == 1440


# ---------------------------------------------------------------- services ---


async def test_import_history_service_registered(hass, init_integration):
    assert hass.services.has_service(DOMAIN, SERVICE_IMPORT_HISTORY)


async def test_import_history_fetches_requested_range(
    hass, init_integration, mock_api
):
    mock_api.get_timeseries.reset_mock()
    start = datetime(2026, 6, 1, 0, 0, tzinfo=BERLIN)
    end = datetime(2026, 6, 30, 23, 59, tzinfo=BERLIN)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_IMPORT_HISTORY,
        {ATTR_START: start, ATTR_END: end},
        blocking=True,
    )
    await hass.async_block_till_done()

    ranges = [
        (kwargs["start"], kwargs["end"])
        for _, kwargs in mock_api.get_timeseries.await_args_list
    ]
    assert (start, end) in ranges


async def test_import_history_covers_every_line(hass, init_integration, mock_api):
    mock_api.get_timeseries.reset_mock()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_IMPORT_HISTORY,
        {
            ATTR_START: datetime(2026, 6, 1, tzinfo=BERLIN),
            ATTR_END: datetime(2026, 6, 2, tzinfo=BERLIN),
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    requested_lines = {
        kwargs["line"] for _, kwargs in mock_api.get_timeseries.await_args_list
    }
    assert len(requested_lines) == 3


async def test_import_history_requires_both_bounds(hass, init_integration):
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_IMPORT_HISTORY,
            {ATTR_START: datetime(2026, 6, 1, tzinfo=BERLIN)},
            blocking=True,
        )


async def test_service_removed_when_last_entry_unloads(hass, init_integration):
    await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()

    assert not hass.services.has_service(DOMAIN, SERVICE_IMPORT_HISTORY)
