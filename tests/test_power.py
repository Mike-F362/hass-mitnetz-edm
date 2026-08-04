"""Unit tests for the interval-energy to average-power conversion.

Pure logic, no Home Assistant instance required.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.mitnetz_edm.power import derive_power_points, energy_to_power

QUARTER_HOUR = timedelta(minutes=15)


# The reference values below were obtained from the portal's own
# /data/timeseries/moment/0/<start>/<value>/<end>/<rkmgId>/<period> endpoint,
# which performs the same conversion server-side. They pin the behaviour to
# what the portal itself reports rather than to our arithmetic.
@pytest.mark.parametrize(
    ("energy_kwh", "interval", "expected_kw"),
    [
        (1.1104, QUARTER_HOUR, 4.4416),
        (2.2208, QUARTER_HOUR, 8.8832),
        (2.0, QUARTER_HOUR, 8.0),
        # Same figure at hourly resolution is already a kW value.
        (1.1104, timedelta(hours=1), 1.1104),
        (0.302, QUARTER_HOUR, 1.208),
        (0.0, QUARTER_HOUR, 0.0),
    ],
)
def test_energy_to_power_matches_portal(energy_kwh, interval, expected_kw):
    assert energy_to_power(energy_kwh, interval) == pytest.approx(expected_kw)


@pytest.mark.parametrize("interval", [timedelta(0), timedelta(seconds=-60)])
def test_non_positive_interval_yields_none(interval):
    """A zero or negative interval must not raise or produce infinity."""
    assert energy_to_power(1.0, interval) is None


def test_half_hour_interval_uses_factor_two():
    assert energy_to_power(1.0, timedelta(minutes=30)) == pytest.approx(2.0)


def test_derive_power_points_preserves_timestamps_and_order():
    """The power value belongs to the same interval, so timestamps must not shift."""
    start = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    points = [
        (start, 1.023),
        (start + QUARTER_HOUR, 1.1104),
        (start + 2 * QUARTER_HOUR, 0.5),
    ]

    derived = derive_power_points(points, QUARTER_HOUR)

    assert [ts for ts, _ in derived] == [ts for ts, _ in points]
    assert [round(v, 4) for _, v in derived] == [4.092, 4.4416, 2.0]


def test_derive_power_points_skips_unconvertible():
    start = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    assert derive_power_points([(start, 1.0)], timedelta(0)) == []


def test_derive_power_points_handles_empty_input():
    assert derive_power_points([], QUARTER_HOUR) == []
