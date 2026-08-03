"""Shared fixtures for the Mitnetz EDM test suite."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.util import dt as dt_util

from custom_components.mitnetz_edm.api import (
    MeasurementKind,
    MeasurementLine,
    MeasurementPoint,
    TimeseriesPoint,
    TimeseriesResult,
)
from custom_components.mitnetz_edm.const import (
    CONF_RELOAD_WINDOW,
    CONF_TIMEOUT,
    CONF_UPDATE_INTERVAL,
    DOMAIN,
)

BERLIN = ZoneInfo("Europe/Berlin")

# Mirrors the shape documented in the portal's /sidebarMultiMp/rlm response,
# extended with a maximum-demand line so unit handling is covered.
MP_ID = "NMP_TEST_MP_0000001"
LINE_BEZUG = "NMP_TEST_LN_0000002"
LINE_EINSPEISUNG = "NMP_TEST_LN_0000003"
LINE_MAX_EINSPEISUNG = "NMP_TEST_LN_0000004"


@pytest.fixture(scope="session", autouse=True)
def _warm_up_pycares_thread():
    """Start the pycares shutdown thread before the harness snapshots threads.

    pycares (used by aiohttp for DNS) lazily starts a single daemon thread the
    first time a channel is created. If that happens inside a test, Home
    Assistant's cleanup check reports it as a leaked thread. Starting it up
    front keeps the leak detection meaningful for real leaks.
    """
    try:
        from pycares import _shutdown_manager

        _shutdown_manager.start()
    except Exception:  # pragma: no cover - optional dependency detail
        pass
    yield


@pytest.fixture
def integration_env(recorder_mock, enable_custom_integrations):
    """Environment needed to set up the integration inside Home Assistant.

    ``recorder_mock`` must be resolved *before* ``hass`` is built, so it is
    listed first. Modules that set up the integration opt in via an autouse
    fixture; pure unit tests skip this entirely and stay fast.
    """
    yield


@pytest.fixture
def measurement_points() -> list[MeasurementPoint]:
    """A measurement point with import, export and maximum-demand lines."""
    return [
        MeasurementPoint(
            id=MP_ID,
            lines=[
                MeasurementLine(id=LINE_BEZUG, name="1-1:1.29.0"),
                MeasurementLine(id=LINE_EINSPEISUNG, name="1-1:2.29.0"),
                MeasurementLine(id=LINE_MAX_EINSPEISUNG, name="1-1:2.6.0"),
            ],
        )
    ]


def make_series(
    values: list[float],
    start: datetime | None = None,
    step_minutes: int = 15,
) -> TimeseriesResult:
    """Build a quarter-hourly timeseries starting at ``start``.

    When no ``start`` is given the series is anchored so that its *last*
    interval has just ended. Sensors treat a value that has aged past the
    configured number of intervals as unknown, so a series pinned to a fixed
    date in the past would be reported as ``unknown`` and tests would be
    asserting against staleness rather than against the value logic.
    """
    if start is None:
        start = dt_util.utcnow() - timedelta(minutes=step_minutes * len(values))
    points = [
        TimeseriesPoint(
            timestamp=start + timedelta(minutes=step_minutes * index),
            value=value,
        )
        for index, value in enumerate(values)
    ]
    return TimeseriesResult(points=points, kind=MeasurementKind.INTERVAL_ENERGY)


@pytest.fixture
def timeseries_by_line() -> dict[str, TimeseriesResult]:
    """Distinct series per line so tests can tell the sensors apart."""
    return {
        LINE_BEZUG: make_series([0.25, 0.30, 0.10, 0.42]),
        LINE_EINSPEISUNG: make_series([1.10, 0.90, 1.40, 0.20]),
        # Maximum demand fluctuates - the case that previously produced a
        # nonsensical monotonically-rising graph.
        LINE_MAX_EINSPEISUNG: make_series([4.67, 2.61, 2.13, 0.65]),
    }


@pytest.fixture
def mock_api(measurement_points, timeseries_by_line):
    """Patch the API client so no HTTP traffic happens during setup."""
    with patch(
        "custom_components.mitnetz_edm.MitnetzEdmApiClient", autospec=True
    ) as client_cls:
        client = client_cls.return_value
        client.login = AsyncMock()
        client.is_logged_in = True
        client.last_login = datetime(2026, 8, 1, 9, 0, tzinfo=BERLIN)
        client.last_request_duration = 0.42
        client.get_measurement_points = AsyncMock(return_value=measurement_points)

        async def _get_timeseries(meterpoint, line, start, end):
            return timeseries_by_line[line]

        client.get_timeseries = AsyncMock(side_effect=_get_timeseries)
        yield client


@pytest.fixture
def mock_statistics():
    """Stub out the statistics import (exercised separately)."""
    with patch(
        "custom_components.mitnetz_edm.coordinator.async_import_line_statistics",
        new=AsyncMock(),
    ) as stats:
        yield stats


@pytest.fixture
def config_entry_data() -> dict[str, str]:
    return {CONF_USERNAME: "test-user", CONF_PASSWORD: "test-password"}


@pytest.fixture
def config_entry_options() -> dict[str, int]:
    return {
        CONF_UPDATE_INTERVAL: 15,
        CONF_RELOAD_WINDOW: 24 * 60,
        CONF_TIMEOUT: 30,
    }


@pytest.fixture
async def init_integration(
    hass, mock_api, mock_statistics, config_entry_data, config_entry_options
):
    """Set up the integration with a mocked API and return the config entry."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="test-user",
        data=config_entry_data,
        options=config_entry_options,
        unique_id="test-user",
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry
