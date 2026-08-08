"""Sensor platform for the Mitnetz Strom EDM integration."""
from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    DEVICE_MANUFACTURER,
    DEVICE_MODEL,
    DEVICE_NAME,
    DOMAIN,
    NAME_POWER_SUFFIX,
)
from .coordinator import LineData, MitnetzEdmCoordinator
from .obis import UNIT_KW, LineClassification, Quantity, classify_line

_LOGGER = logging.getLogger(__name__)

#: Quantity -> (device class, state class).
_QUANTITY_CLASSES: dict[Quantity, tuple[SensorDeviceClass | None, SensorStateClass | None]] = {
    # A per-interval kWh figure that rises and falls: it is *not* a cumulative
    # register, so MEASUREMENT is correct here. The Energy Dashboard is fed
    # from long-term statistics instead (see statistics.py).
    Quantity.ENERGY_INTERVAL: (SensorDeviceClass.ENERGY, SensorStateClass.MEASUREMENT),
    Quantity.ENERGY_TOTAL: (SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING),
    # Maximum/average demand in kW - fluctuates per interval.
    Quantity.POWER: (SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT),
    Quantity.UNKNOWN: (None, None),
}


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors from a config entry."""
    coordinator: MitnetzEdmCoordinator = entry.runtime_data.coordinator

    known_line_ids: set[str] = set()

    @callback
    def _add_new_entities() -> None:
        if not coordinator.data:
            return
        new_entities: list[SensorEntity] = []
        for line_id, line_data in coordinator.data.lines.items():
            if line_id in known_line_ids:
                continue
            new_entities.append(
                MitnetzEdmLineSensor(coordinator, entry, line_id, line_data)
            )
            # An interval-energy line also yields an average-power reading.
            if classify_line(line_data.obis_code).quantity is (
                Quantity.ENERGY_INTERVAL
            ):
                new_entities.append(
                    MitnetzEdmDerivedPowerSensor(
                        coordinator, entry, line_id, line_data
                    )
                )
        known_line_ids.update(coordinator.data.lines)
        if new_entities:
            async_add_entities(new_entities)

    _add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_entities))


class MitnetzEdmLineSensor(CoordinatorEntity[MitnetzEdmCoordinator], SensorEntity):
    """A sensor representing a single OBIS measurement line.

    The state is always the value of the most recent *measurement interval* -
    never an aggregate over the whole polling window. Which unit and classes
    apply is derived from the OBIS code, so maximum-demand lines are published
    as kW power readings rather than kWh energy totals.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: MitnetzEdmCoordinator,
        entry: ConfigEntry,
        line_id: str,
        line_data: LineData,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._line_id = line_id

        classification = classify_line(line_data.obis_code)
        self._classification = classification

        device_class, state_class = _QUANTITY_CLASSES[classification.quantity]

        self._attr_unique_id = f"{entry.entry_id}_{line_id}"
        self._attr_name = classification.display_name
        self._attr_native_unit_of_measurement = classification.unit
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        self._attr_suggested_display_precision = 3

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=DEVICE_NAME,
            manufacturer=DEVICE_MANUFACTURER,
            model=DEVICE_MODEL,
            entry_type="service",
        )

        if classification.quantity is Quantity.UNKNOWN:
            _LOGGER.debug(
                "OBIS code %s could not be classified; sensor created without "
                "unit or device class",
                line_data.obis_code,
            )

    @property
    def classification(self) -> LineClassification:
        """Expose the resolved classification (used by the statistics import)."""
        return self._classification

    @property
    def _line_data(self) -> LineData | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.lines.get(self._line_id)

    @property
    def native_value(self) -> float | None:
        """Value of the most recent measurement interval.

        Returns ``None`` (state ``unknown``) once that interval is older than
        the configured staleness limit. A cumulative register is exempt: a
        meter reading stays valid until it is superseded, whereas a per-interval
        min/max/demand figure does not - carrying the last peak forward is what
        drew flat multi-hour plateaus across the history graph.
        """
        line_data = self._line_data
        if line_data is None:
            return None

        if self._classification.quantity is Quantity.ENERGY_TOTAL:
            return line_data.last_value

        if line_data.is_stale(dt_util.utcnow(), self.coordinator.stale_intervals):
            return None

        return line_data.last_value

    @property
    def available(self) -> bool:
        return super().available and self._line_data is not None

    @property
    def extra_state_attributes(self) -> dict[str, str | int | float | None]:
        line_data = self._line_data
        if line_data is None:
            return {}

        interval = line_data.interval
        age = line_data.age(dt_util.utcnow())

        return {
            "obis_code": line_data.obis_code,
            "measurement_point": line_data.point_id,
            "quantity": self._classification.quantity.value,
            "direction": self._classification.direction.value,
            "interval_start": (
                line_data.last_timestamp.isoformat()
                if line_data.last_timestamp
                else None
            ),
            "interval_end": (
                line_data.interval_end.isoformat()
                if line_data.interval_end
                else None
            ),
            "interval_minutes": (
                round(interval.total_seconds() / 60, 1) if interval else None
            ),
            "data_age_minutes": (
                round(age.total_seconds() / 60, 1) if age is not None else None
            ),
            "intervals_in_window": len(line_data.result.points),
        }


class MitnetzEdmDerivedPowerSensor(
    CoordinatorEntity[MitnetzEdmCoordinator], SensorEntity
):
    """Average power of the most recent interval, derived from interval energy.

    Load-profile lines report kWh *per measurement interval*; dividing by the
    interval length gives the average power during it (see :mod:`.power`). The
    portal itself offers no per-interval power line for such meters - its
    ``/data/timeseries/moment/...`` endpoint performs exactly this division
    server-side - so deriving it locally is both equivalent and cheaper.

    One value per measurement interval, which is what makes a real power curve
    in the history graph possible rather than a single daily peak figure.
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UNIT_KW
    _attr_suggested_display_precision = 3

    def __init__(
        self,
        coordinator: MitnetzEdmCoordinator,
        entry: ConfigEntry,
        line_id: str,
        line_data: LineData,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._line_id = line_id

        classification = classify_line(line_data.obis_code)
        self._classification = classification

        self._attr_unique_id = f"{entry.entry_id}_{line_id}_power"
        self._attr_name = f"{classification.display_name} {NAME_POWER_SUFFIX}"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=DEVICE_NAME,
            manufacturer=DEVICE_MANUFACTURER,
            model=DEVICE_MODEL,
            entry_type="service",
        )

    @property
    def _line_data(self) -> LineData | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.lines.get(self._line_id)

    @property
    def native_value(self) -> float | None:
        """Average power of the most recent interval, in kW.

        Subject to the same staleness rule as any other power figure: a demand
        value belongs to one interval and must not be carried forward.
        """
        line_data = self._line_data
        if line_data is None:
            return None

        if line_data.is_stale(dt_util.utcnow(), self.coordinator.stale_intervals):
            return None

        return line_data.last_power

    @property
    def available(self) -> bool:
        return super().available and self._line_data is not None

    @property
    def extra_state_attributes(self) -> dict[str, str | int | float | None]:
        line_data = self._line_data
        if line_data is None:
            return {}

        interval = line_data.effective_interval
        age = line_data.age(dt_util.utcnow())

        return {
            "obis_code": line_data.obis_code,
            "measurement_point": line_data.point_id,
            "direction": self._classification.direction.value,
            "derived_from": "interval_energy",
            "interval_energy_kwh": line_data.last_value,
            "interval_start": (
                line_data.last_timestamp.isoformat()
                if line_data.last_timestamp
                else None
            ),
            "interval_end": (
                line_data.interval_end.isoformat()
                if line_data.interval_end
                else None
            ),
            "interval_minutes": round(interval.total_seconds() / 60, 1),
            "data_age_minutes": (
                round(age.total_seconds() / 60, 1) if age is not None else None
            ),
        }
