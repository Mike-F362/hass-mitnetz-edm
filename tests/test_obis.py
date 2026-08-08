"""Unit tests for OBIS code classification.

These are the tests that pin down the bug behind the nonsensical graphs: a
maximum-demand line must come out as a kW power reading, not a kWh energy
total.
"""

from __future__ import annotations

import pytest

from custom_components.mitnetz_edm.obis import (
    UNIT_KW,
    UNIT_KWH,
    Direction,
    Quantity,
    classify_line,
    parse_obis,
)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("1-1:1.29.0", (1, 29)),
        ("1-1:2.29.0", (2, 29)),
        ("1-1:2.6.0", (2, 6)),
        ("1-0:1.8.0", (1, 8)),
        ("1-1:1.29", (1, 29)),  # E field omitted
        ("  1-1:1.29.0  ", (1, 29)),  # tolerate whitespace
    ],
)
def test_parse_obis_valid(code, expected):
    assert parse_obis(code) == expected


@pytest.mark.parametrize(
    "code", ["", "not-an-obis-code", "1-1:x.29.0", "Export Maximum", "1:1.29.0"]
)
def test_parse_obis_invalid(code):
    assert parse_obis(code) is None


@pytest.mark.parametrize(
    ("code", "quantity", "unit", "direction", "name"),
    [
        # Interval energy (load profile) - the two lines the portal reported.
        ("1-1:1.29.0", Quantity.ENERGY_INTERVAL, UNIT_KWH, Direction.IMPORT, "Import"),
        (
            "1-1:2.29.0",
            Quantity.ENERGY_INTERVAL,
            UNIT_KWH,
            Direction.EXPORT,
            "Export",
        ),
        # Maximum demand -> kW, NOT kWh.
        (
            "1-1:2.6.0",
            Quantity.POWER,
            UNIT_KW,
            Direction.EXPORT,
            "Export Maximum",
        ),
        ("1-1:1.6.0", Quantity.POWER, UNIT_KW, Direction.IMPORT, "Import Maximum"),
        # Average demand is also a power figure.
        ("1-1:1.5.0", Quantity.POWER, UNIT_KW, Direction.IMPORT, "Import Last Average"),
        ("1-1:2.4.0", Quantity.POWER, UNIT_KW, Direction.EXPORT, "Export Average"),
        # Cumulative register.
        (
            "1-0:1.8.0",
            Quantity.ENERGY_TOTAL,
            UNIT_KWH,
            Direction.IMPORT,
            "Import Meter Reading",
        ),
    ],
)
def test_classify_known_codes(code, quantity, unit, direction, name):
    result = classify_line(code)
    assert result.quantity is quantity
    assert result.unit == unit
    assert result.direction is direction
    assert result.display_name == name


def test_maximum_demand_is_power_not_energy():
    """Regression guard for the kWh/total_increasing misclassification."""
    result = classify_line("1-1:2.6.0")
    assert result.is_power is True
    assert result.is_energy is False
    assert result.unit == UNIT_KW


def test_interval_energy_is_energy_not_power():
    result = classify_line("1-1:1.29.0")
    assert result.is_energy is True
    assert result.is_power is False


def test_unknown_code_yields_no_unit():
    """Unknown lines still get a sensor, but without misleading metadata."""
    result = classify_line("9-9:99.99.9")
    assert result.quantity is Quantity.UNKNOWN
    assert result.unit is None
    assert result.display_name == "9-9:99.99.9"


@pytest.mark.parametrize(
    ("portal_name", "expected"),
    [
        ("Export Maximum", Quantity.POWER),
        ("Maximale Leistung", Quantity.POWER),
        ("Bezug Arbeit", Quantity.ENERGY_INTERVAL),
        ("Zählerstand", Quantity.ENERGY_INTERVAL),
        ("Irgendwas", Quantity.UNKNOWN),
    ],
)
def test_name_fallback_for_unparseable_codes(portal_name, expected):
    """When the code is not OBIS-shaped, fall back to the portal's own label."""
    result = classify_line("kein-obis-code", portal_name=portal_name)
    assert result.quantity is expected


def test_name_fallback_keeps_portal_name_as_display_name():
    result = classify_line("kein-obis-code", portal_name="Export Maximum")
    assert result.display_name == "Export Maximum"
