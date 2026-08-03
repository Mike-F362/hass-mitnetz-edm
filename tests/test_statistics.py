"""Tests for the long-term statistics aggregation.

Interval data arrives backfilled, so each value must land in the hour it was
measured rather than at poll time.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.mitnetz_edm.obis import Quantity
from custom_components.mitnetz_edm.statistics import (
    bucket_by_hour,
    build_statistics,
    statistic_id_for,
)

BERLIN = ZoneInfo("Europe/Berlin")


def quarter_hours(start: datetime, values: list[float]):
    return [
        (start + timedelta(minutes=15 * index), value)
        for index, value in enumerate(values)
    ]


def test_statistic_id_is_namespaced():
    assert statistic_id_for("NMP_TEST_LN_0000002") == "mitnetz_edm:nmp_slp_s_2855186"


def test_bucket_by_hour_groups_quarter_hours():
    start = datetime(2026, 8, 1, 10, 0, tzinfo=BERLIN)
    points = quarter_hours(start, [1.0, 2.0, 3.0, 4.0, 5.0])

    buckets = bucket_by_hour(points)

    assert len(buckets) == 2
    hour_10 = datetime(2026, 8, 1, 10, 0, tzinfo=BERLIN)
    hour_11 = datetime(2026, 8, 1, 11, 0, tzinfo=BERLIN)
    assert buckets[hour_10] == [1.0, 2.0, 3.0, 4.0]
    assert buckets[hour_11] == [5.0]


def test_bucket_by_hour_normalises_arrival_order():
    """Backfilled batches may arrive out of order; buckets must not care."""
    start = datetime(2026, 8, 1, 10, 0, tzinfo=BERLIN)
    points = quarter_hours(start, [1.0, 2.0, 3.0, 4.0])

    assert bucket_by_hour(list(reversed(points))) == bucket_by_hour(points)


def test_meter_reading_uses_latest_value_even_when_unsorted():
    """A cumulative register must report the newest reading of the hour."""
    start = datetime(2026, 8, 1, 10, 0, tzinfo=BERLIN)
    points = quarter_hours(start, [100.0, 101.0, 102.0, 103.0])

    rows = build_statistics(list(reversed(points)), Quantity.ENERGY_TOTAL)

    assert rows[0]["state"] == pytest.approx(103.0)


def test_interval_energy_is_summed_per_hour():
    start = datetime(2026, 8, 1, 10, 0, tzinfo=BERLIN)
    points = quarter_hours(start, [0.25, 0.25, 0.25, 0.25, 1.0])

    rows = build_statistics(points, Quantity.ENERGY_INTERVAL)

    assert len(rows) == 2
    assert rows[0]["state"] == pytest.approx(1.0)
    assert rows[1]["state"] == pytest.approx(1.0)


def test_interval_energy_sum_is_cumulative():
    """The Energy Dashboard needs a continuously rising ``sum``."""
    start = datetime(2026, 8, 1, 10, 0, tzinfo=BERLIN)
    points = quarter_hours(start, [0.5, 0.5, 0.5, 0.5, 2.0, 2.0, 2.0, 2.0])

    rows = build_statistics(points, Quantity.ENERGY_INTERVAL)

    assert rows[0]["sum"] == pytest.approx(2.0)
    assert rows[1]["sum"] == pytest.approx(10.0)


def test_interval_energy_resumes_from_previous_sum():
    start = datetime(2026, 8, 1, 10, 0, tzinfo=BERLIN)
    points = quarter_hours(start, [1.0, 1.0, 1.0, 1.0])

    rows = build_statistics(points, Quantity.ENERGY_INTERVAL, initial_sum=100.0)

    assert rows[0]["sum"] == pytest.approx(104.0)


def test_power_uses_mean_min_max_not_sum():
    """Summing kW values would be physically meaningless."""
    start = datetime(2026, 8, 1, 10, 0, tzinfo=BERLIN)
    points = quarter_hours(start, [4.67, 2.61, 2.13, 0.65])

    rows = build_statistics(points, Quantity.POWER)

    assert len(rows) == 1
    row = rows[0]
    assert "sum" not in row
    assert row["max"] == pytest.approx(4.67)
    assert row["min"] == pytest.approx(0.65)
    assert row["mean"] == pytest.approx((4.67 + 2.61 + 2.13 + 0.65) / 4)


def test_power_statistics_capture_decreases():
    """Regression: falling demand must be preserved, not clamped upward."""
    start = datetime(2026, 8, 1, 10, 0, tzinfo=BERLIN)
    points = quarter_hours(start, [4.0, 4.0, 4.0, 4.0, 1.0, 1.0, 1.0, 1.0])

    rows = build_statistics(points, Quantity.POWER)

    assert rows[0]["mean"] == pytest.approx(4.0)
    assert rows[1]["mean"] == pytest.approx(1.0)
    assert rows[1]["mean"] < rows[0]["mean"]


def test_meter_reading_keeps_last_value_of_hour():
    start = datetime(2026, 8, 1, 10, 0, tzinfo=BERLIN)
    points = quarter_hours(start, [100.0, 101.0, 102.0, 103.0])

    rows = build_statistics(points, Quantity.ENERGY_TOTAL)

    assert rows[0]["state"] == pytest.approx(103.0)
    assert rows[0]["sum"] == pytest.approx(103.0)


def test_rows_are_sorted_by_hour():
    start = datetime(2026, 8, 1, 8, 0, tzinfo=BERLIN)
    points = quarter_hours(start, [1.0] * 12)

    rows = build_statistics(points, Quantity.POWER)
    starts = [row["start"] for row in rows]

    assert starts == sorted(starts)


def test_empty_input_produces_no_rows():
    assert build_statistics([], Quantity.POWER) == []


def test_hours_are_aligned_to_full_hours():
    """Statistics rows must start exactly on the hour."""
    start = datetime(2026, 8, 1, 10, 15, tzinfo=BERLIN)
    points = quarter_hours(start, [1.0, 2.0])

    rows = build_statistics(points, Quantity.POWER)

    assert rows[0]["start"].minute == 0
    assert rows[0]["start"].second == 0
