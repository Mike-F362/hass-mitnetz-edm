"""OBIS code parsing and measurement classification.

The EDM portal identifies each measurement line by an OBIS code such as
``1-1:1.29.0``. The semantics follow IEC 62056-61:

    A-B:C.D.E

* ``C`` -- the measured quantity. ``1`` = active power import (+A, "Bezug"),
  ``2`` = active power export (-A, "Einspeisung").
* ``D`` -- the processing applied. This is what decides whether the value is
  an *energy* (kWh) or a *power* (kW) figure:

  =====  ==========================================  ===============
  ``D``  Meaning                                     Physical unit
  =====  ==========================================  ===============
  4      Current average (rolling demand)            kW
  5      Last average (previous interval demand)     kW
  6      Maximum (maximum demand in the period)      kW
  7      Instantaneous value                         kW
  8      Time integral (cumulative energy register)  kWh
  29     Time integral 5 (per-interval load profile) kWh
  =====  ==========================================  ===============

Getting this wrong is not cosmetic: a maximum-demand line (``D=6``) is a
kW figure that rises *and falls* per measurement interval. Publishing it as
``device_class: energy`` with ``state_class: total_increasing`` makes Home
Assistant treat every decrease as a meter reset, which is what produced the
nonsensical staircase graphs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

# ``1-1:1.29.0`` -> groups: a=1, b=1, c=1, d=29, e=0
_OBIS_RE = re.compile(
    r"^(?P<a>\d+)-(?P<b>\d+):(?P<c>\d+)\.(?P<d>\d+)(?:\.(?P<e>\d+))?$"
)

UNIT_KWH = "kWh"
UNIT_KW = "kW"


class Quantity(str, Enum):
    """What a measurement line physically represents."""

    #: Energy accumulated within one measurement interval (kWh per interval).
    ENERGY_INTERVAL = "energy_interval"
    #: A cumulative, monotonically rising energy register (kWh).
    ENERGY_TOTAL = "energy_total"
    #: A power/demand figure valid for one interval (kW). Rises and falls.
    POWER = "power"
    #: Could not be determined from the OBIS code.
    UNKNOWN = "unknown"


class Direction(str, Enum):
    """Direction of energy flow."""

    IMPORT = "import"  # Bezug
    EXPORT = "export"  # Einspeisung
    UNKNOWN = "unknown"


#: ``D`` field -> quantity.
_D_FIELD_QUANTITY: dict[int, Quantity] = {
    4: Quantity.POWER,  # current average demand
    5: Quantity.POWER,  # last average demand
    6: Quantity.POWER,  # maximum demand
    7: Quantity.POWER,  # instantaneous
    8: Quantity.ENERGY_TOTAL,  # cumulative register
    9: Quantity.ENERGY_TOTAL,  # apparent energy register
    29: Quantity.ENERGY_INTERVAL,  # load profile interval energy
}

#: ``D`` field -> German descriptor appended to the direction name.
_D_FIELD_LABEL: dict[int, str] = {
    4: "Mittelwert",
    5: "Letzter Mittelwert",
    6: "Maximum",
    7: "Momentanwert",
    8: "Zählerstand",
    29: "",  # plain interval energy needs no suffix
}

#: ``C`` field -> direction.
_C_FIELD_DIRECTION: dict[int, Direction] = {
    1: Direction.IMPORT,
    2: Direction.EXPORT,
}

_DIRECTION_LABEL: dict[Direction, str] = {
    Direction.IMPORT: "Bezug",
    Direction.EXPORT: "Einspeisung",
}

#: Name fragments used as a fallback when the OBIS code is unparseable.
_POWER_HINTS = ("maximum", "minimum", "max.", "min.", "leistung", "demand", "kw")
_ENERGY_HINTS = ("arbeit", "energie", "zählerstand", "zaehlerstand", "kwh")


@dataclass(frozen=True, slots=True)
class LineClassification:
    """The resolved physical meaning of a measurement line."""

    quantity: Quantity
    direction: Direction
    unit: str | None
    display_name: str

    @property
    def is_power(self) -> bool:
        return self.quantity is Quantity.POWER

    @property
    def is_energy(self) -> bool:
        return self.quantity in (Quantity.ENERGY_INTERVAL, Quantity.ENERGY_TOTAL)


def parse_obis(code: str) -> tuple[int, int] | None:
    """Return the ``(C, D)`` fields of an OBIS code, or ``None`` if unparseable."""
    match = _OBIS_RE.match(code.strip())
    if not match:
        return None
    return int(match.group("c")), int(match.group("d"))


def classify_line(obis_code: str, portal_name: str | None = None) -> LineClassification:
    """Classify a measurement line from its OBIS code.

    Args:
        obis_code: The OBIS code reported by the portal, e.g. ``1-1:2.6.0``.
        portal_name: Optional human-readable name from the portal, used only as
            a fallback when the OBIS code cannot be parsed.

    Returns:
        A :class:`LineClassification` describing quantity, direction, unit and a
        display name. Unknown codes yield ``Quantity.UNKNOWN`` with a ``None``
        unit so the sensor is still created but carries no misleading metadata.
    """
    parsed = parse_obis(obis_code)

    if parsed is not None:
        c_field, d_field = parsed
        quantity = _D_FIELD_QUANTITY.get(d_field, Quantity.UNKNOWN)
        direction = _C_FIELD_DIRECTION.get(c_field, Direction.UNKNOWN)
    else:
        quantity = _classify_by_name(portal_name)
        direction = Direction.UNKNOWN
        d_field = None

    unit = _unit_for(quantity)
    display_name = _display_name(obis_code, portal_name, direction, d_field)

    return LineClassification(
        quantity=quantity,
        direction=direction,
        unit=unit,
        display_name=display_name,
    )


def _classify_by_name(portal_name: str | None) -> Quantity:
    """Best-effort classification from a free-text name."""
    if not portal_name:
        return Quantity.UNKNOWN
    lowered = portal_name.casefold()
    if any(hint in lowered for hint in _POWER_HINTS):
        return Quantity.POWER
    if any(hint in lowered for hint in _ENERGY_HINTS):
        return Quantity.ENERGY_INTERVAL
    return Quantity.UNKNOWN


def _unit_for(quantity: Quantity) -> str | None:
    if quantity is Quantity.POWER:
        return UNIT_KW
    if quantity in (Quantity.ENERGY_INTERVAL, Quantity.ENERGY_TOTAL):
        return UNIT_KWH
    return None


def _display_name(
    obis_code: str,
    portal_name: str | None,
    direction: Direction,
    d_field: int | None,
) -> str:
    """Build a human-readable name, e.g. "Einspeisung Maximum"."""
    if direction is Direction.UNKNOWN or d_field is None:
        return portal_name or obis_code

    base = _DIRECTION_LABEL[direction]
    suffix = _D_FIELD_LABEL.get(d_field, "")
    return f"{base} {suffix}".strip() if suffix else base
