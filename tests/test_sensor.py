"""Integration tests for the sensor platform.

The central regression these guard: a maximum-demand line must appear as a kW
power sensor whose value follows the latest measurement interval, instead of a
kWh energy total that Home Assistant treats as monotonically rising.
"""

from __future__ import annotations

from homeassistant.components.sensor import ATTR_STATE_CLASS, SensorStateClass
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
)
from homeassistant.helpers import entity_registry as er
import pytest

from .conftest import (
    LINE_MAX_EINSPEISUNG,
    make_series,
)


@pytest.fixture(autouse=True)
def _env(integration_env):
    """Set up recorder + custom integration loading for every test here."""
    yield


ENTITY_IMPORT = "sensor.mitnetz_strom_edm_import"
ENTITY_EXPORT = "sensor.mitnetz_strom_edm_export"
ENTITY_MAX = "sensor.mitnetz_strom_edm_export_maximum"


async def test_all_lines_create_sensors(hass, init_integration):
    """Every line becomes a sensor, plus a derived kW twin per energy line.

    3 lines (2 interval energy + 1 maximum demand) -> 3 + 2 = 5 entities.
    """
    entity_registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(
        entity_registry, init_integration.entry_id
    )
    assert len(entries) == 5

    derived = [e for e in entries if e.unique_id.endswith("_power")]
    assert len(derived) == 2


async def test_energy_line_units(hass, init_integration):
    """Interval energy lines are kWh."""
    state = hass.states.get(ENTITY_IMPORT)
    assert state is not None
    assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == "kWh"
    assert state.attributes[ATTR_DEVICE_CLASS] == "energy"


async def test_maximum_demand_line_is_kilowatt(hass, init_integration):
    """Regression: maximum demand must be kW / power, never kWh / energy."""
    state = hass.states.get(ENTITY_MAX)
    assert state is not None
    assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == "kW"
    assert state.attributes[ATTR_DEVICE_CLASS] == "power"


async def test_maximum_demand_is_not_total_increasing(hass, init_integration):
    """``total_increasing`` on a fluctuating value causes phantom meter resets."""
    state = hass.states.get(ENTITY_MAX)
    assert state.attributes[ATTR_STATE_CLASS] == SensorStateClass.MEASUREMENT


async def test_interval_energy_is_not_total_increasing(hass, init_integration):
    """Per-interval kWh also rises and falls, so it is a measurement."""
    state = hass.states.get(ENTITY_IMPORT)
    assert state.attributes[ATTR_STATE_CLASS] == SensorStateClass.MEASUREMENT


async def test_state_is_latest_interval_not_window_aggregate(hass, init_integration):
    """The sensor shows the newest interval, not a max/sum over the window."""
    # Series was [4.67, 2.61, 2.13, 0.65]; the newest value is 0.65.
    # An aggregate over the window would have produced 4.67.
    state = hass.states.get(ENTITY_MAX)
    assert float(state.state) == 0.65


async def test_each_line_gets_its_own_value(hass, init_integration):
    assert float(hass.states.get(ENTITY_IMPORT).state) == 0.42
    assert float(hass.states.get(ENTITY_EXPORT).state) == 0.20
    assert float(hass.states.get(ENTITY_MAX).state) == 0.65


async def test_value_follows_new_interval_on_refresh(
    hass, init_integration, timeseries_by_line, mock_api
):
    """A later poll must move the value down as well as up."""
    entry = init_integration
    coordinator = entry.runtime_data.coordinator

    timeseries_by_line[LINE_MAX_EINSPEISUNG] = make_series([0.65, 3.90])

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert float(hass.states.get(ENTITY_MAX).state) == 3.90

    # ... and back down again.
    timeseries_by_line[LINE_MAX_EINSPEISUNG] = make_series([3.90, 0.10])
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert float(hass.states.get(ENTITY_MAX).state) == 0.10


async def test_sensor_attributes_expose_obis_context(hass, init_integration):
    state = hass.states.get(ENTITY_MAX)
    assert state.attributes["obis_code"] == "1-1:2.6.0"
    assert state.attributes["quantity"] == "power"
    assert state.attributes["direction"] == "export"
    assert state.attributes["interval_start"] is not None


async def test_all_sensors_share_one_device(hass, init_integration):
    from homeassistant.helpers import device_registry as dr

    device_registry = dr.async_get(hass)
    devices = dr.async_entries_for_config_entry(
        device_registry, init_integration.entry_id
    )
    assert len(devices) == 1
    assert devices[0].manufacturer == "Mitnetz Strom"


async def test_unknown_obis_code_still_creates_sensor(
    hass, mock_api, mock_statistics, measurement_points, timeseries_by_line
):
    """An unrecognised code must not drop the line silently."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.mitnetz_edm.api import MeasurementLine
    from custom_components.mitnetz_edm.const import DOMAIN

    measurement_points[0].lines.append(
        MeasurementLine(id="LINE_WEIRD", name="9-9:99.99.9")
    )
    timeseries_by_line["LINE_WEIRD"] = make_series([1.0, 2.0])

    entry = MockConfigEntry(domain=DOMAIN, data={"username": "u", "password": "p"})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get("sensor.mitnetz_strom_edm_9_9_99_99_9")
    assert state is not None
    # No unit and no device class rather than a wrong one.
    assert state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) is None
    assert state.attributes.get(ATTR_DEVICE_CLASS) is None


async def test_sensors_unavailable_when_update_fails(hass, init_integration, mock_api):
    """A failing poll must mark entities unavailable, not freeze stale values."""
    from custom_components.mitnetz_edm.api import MitnetzApiError

    coordinator = init_integration.runtime_data.coordinator
    mock_api.get_measurement_points.side_effect = MitnetzApiError("boom")

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(ENTITY_MAX).state == STATE_UNAVAILABLE
