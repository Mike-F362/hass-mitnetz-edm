"""Integration tests for the derived average-power sensor.

The portal exposes no per-interval power line for a load-profile meter, so the
kW figure is derived from the interval energy. These tests pin down that the
entity exists, converts correctly, and obeys the staleness rule.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import STATE_UNKNOWN
from homeassistant.util import dt as dt_util

from custom_components.mitnetz_edm.statistics import (
    POWER_STATISTIC_SUFFIX,
    statistic_id_for,
)

from .conftest import LINE_BEZUG, LINE_EINSPEISUNG, LINE_MAX_EINSPEISUNG, make_series


@pytest.fixture(autouse=True)
def _env(integration_env):
    yield


def _find_power_entity(hass, friendly_name: str) -> str:
    """Locate a derived power entity by its friendly name."""
    for state in hass.states.async_all("sensor"):
        if state.attributes.get("friendly_name", "").endswith(friendly_name):
            return state.entity_id
    raise AssertionError(
        f"no sensor named {friendly_name!r}; "
        f"have {[s.attributes.get('friendly_name') for s in hass.states.async_all('sensor')]}"
    )


async def test_power_sensor_created_for_interval_energy_lines(hass, init_integration):
    """Each kWh interval line gets a companion kW sensor."""
    for name in ("Bezug Leistung", "Einspeisung Leistung"):
        entity_id = _find_power_entity(hass, name)
        state = hass.states.get(entity_id)
        assert state.attributes["device_class"] == SensorDeviceClass.POWER
        assert state.attributes["state_class"] == SensorStateClass.MEASUREMENT
        assert state.attributes["unit_of_measurement"] == "kW"


async def test_no_power_sensor_for_native_power_line(hass, init_integration):
    """A line that is already kW must not get a derived twin."""
    names = [
        s.attributes.get("friendly_name", "") for s in hass.states.async_all("sensor")
    ]
    assert not any(name.endswith("Maximum Leistung") for name in names)


async def test_power_value_is_energy_over_interval(hass, init_integration):
    """0.42 kWh in 15 minutes is 1.68 kW."""
    entity_id = _find_power_entity(hass, "Bezug Leistung")
    assert float(hass.states.get(entity_id).state) == pytest.approx(1.68)


async def test_power_sensor_exposes_source_energy(hass, init_integration):
    entity_id = _find_power_entity(hass, "Einspeisung Leistung")
    attrs = hass.states.get(entity_id).attributes

    assert attrs["derived_from"] == "interval_energy"
    assert attrs["interval_energy_kwh"] == pytest.approx(0.20)
    assert attrs["interval_minutes"] == 15.0
    assert attrs["obis_code"] == "1-1:2.29.0"
    assert attrs["direction"] == "export"
    # 0.20 kWh per quarter hour is 0.8 kW.
    assert float(hass.states.get(entity_id).state) == pytest.approx(0.8)


async def test_power_sensor_goes_unknown_when_stale(
    hass, mock_api, mock_statistics, config_entry_data, config_entry_options
):
    """A demand figure must not be carried forward across many intervals."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    stale_start = dt_util.utcnow() - timedelta(hours=6)
    mock_api.get_timeseries.side_effect = None
    mock_api.get_timeseries.return_value = make_series(
        [0.25, 0.30, 0.10, 0.42], start=stale_start
    )

    entry = MockConfigEntry(
        domain="mitnetz_edm",
        data=config_entry_data,
        options=config_entry_options,
        unique_id="stale-user",
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    entity_id = _find_power_entity(hass, "Bezug Leistung")
    assert hass.states.get(entity_id).state == STATE_UNKNOWN


async def test_power_statistics_use_a_separate_id(hass, init_integration, mock_statistics):
    """Energy and power of one line must not collide in the statistics table."""
    suffixes = {
        call.kwargs.get("suffix", "") for call in mock_statistics.await_args_list
    }
    assert suffixes == {"", POWER_STATISTIC_SUFFIX}

    energy_id = statistic_id_for(LINE_BEZUG)
    power_id = statistic_id_for(LINE_BEZUG, POWER_STATISTIC_SUFFIX)
    assert energy_id != power_id
    assert power_id.endswith("_power")


async def test_power_statistics_written_for_energy_lines_only(
    hass, init_integration, mock_statistics
):
    """The already-kW maximum line must not get a second, derived series."""
    power_calls = [
        call
        for call in mock_statistics.await_args_list
        if call.kwargs.get("suffix") == POWER_STATISTIC_SUFFIX
    ]
    line_ids = {call.kwargs["line_id"] for call in power_calls}

    assert line_ids == {LINE_BEZUG, LINE_EINSPEISUNG}
    assert LINE_MAX_EINSPEISUNG not in line_ids
