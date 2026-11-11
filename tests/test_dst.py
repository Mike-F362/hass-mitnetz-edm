"""Daylight-saving changes: every hour must keep its own statistics row.

Europe/Berlin repeats 02:00-03:00 when summer time ends (25 October 2026) and
skips it when summer time begins (29 March 2026). Long-term statistics are keyed
by the absolute hour, so the repeated hour is two rows and the skipped hour is
none.

The repeated hour is the trap: Python compares two aware datetimes that share
one ``tzinfo`` object by their wall-clock fields and ignores ``fold``. 02:00
CEST and 02:00 CET are therefore *equal* and hash alike, and a dict keyed by
the local hour silently merges them. That happened on 25 October 2026: one row
held eight quarter-hours and the following hour had none. Hours are keyed in
UTC since.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from homeassistant.util import dt as dt_util
import pytest

from custom_components.mitnetz_edm.obis import Quantity
from custom_components.mitnetz_edm.statistics import (
    bucket_by_hour,
    build_statistics,
    drop_incomplete_leading_hour,
)

BERLIN = ZoneInfo("Europe/Berlin")
QUARTER = timedelta(minutes=15)
HOUR = timedelta(hours=1)

#: 02:00 CEST - the first of the two hours the wall clock calls "02:00".
AUTUMN_FIRST_0200 = datetime(2026, 10, 25, 0, 0, tzinfo=UTC)
#: 02:00 CET - the same wall-clock hour again, one hour later.
AUTUMN_SECOND_0200 = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)
#: Local midnight of the 25-hour day.
AUTUMN_MIDNIGHT = datetime(2026, 10, 24, 22, 0, tzinfo=UTC)
#: Local midnight of the 23-hour day.
SPRING_MIDNIGHT = datetime(2026, 3, 28, 23, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _berlin_time_zone():
    """Local hours are Berlin's, as for the real portal."""
    original = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(BERLIN)
    yield
    dt_util.set_default_time_zone(original)


def _quarters(start: datetime, count: int, value: float = 0.25):
    return [(start + QUARTER * index, value) for index in range(count)]


def _utc_starts(rows) -> list[datetime]:
    return [dt_util.as_utc(row["start"]) for row in rows]


def test_the_trap_itself():
    """Why this module exists: equal on the wall clock, an hour apart in time."""
    first = AUTUMN_FIRST_0200.astimezone(BERLIN)
    second = AUTUMN_SECOND_0200.astimezone(BERLIN)

    assert first == second  # fold is ignored for a shared tzinfo
    assert second - first == timedelta(0)
    assert dt_util.as_utc(second) - dt_util.as_utc(first) == HOUR


def test_repeated_hour_keeps_two_buckets():
    points = _quarters(AUTUMN_FIRST_0200, 8)

    buckets = bucket_by_hour(points)

    assert len(buckets) == 2
    assert all(len(values) == 4 for values in buckets.values())


def test_repeated_hour_gets_two_rows_with_their_own_values():
    points = _quarters(AUTUMN_FIRST_0200, 4, value=1.0)
    points += _quarters(AUTUMN_SECOND_0200, 4, value=2.0)

    rows = build_statistics(points, Quantity.ENERGY_INTERVAL)

    assert _utc_starts(rows) == [AUTUMN_FIRST_0200, AUTUMN_SECOND_0200]
    assert [row["state"] for row in rows] == [4.0, 8.0]
    assert [row["sum"] for row in rows] == [4.0, 12.0]


def test_autumn_day_has_twenty_five_hourly_rows():
    points = _quarters(AUTUMN_MIDNIGHT, 25 * 4)

    rows = build_statistics(points, Quantity.ENERGY_INTERVAL)

    assert _utc_starts(rows) == [AUTUMN_MIDNIGHT + HOUR * index for index in range(25)]
    assert all(row["state"] == pytest.approx(1.0) for row in rows)
    assert rows[-1]["sum"] == pytest.approx(25.0)


def test_autumn_day_keeps_its_total_energy():
    points = _quarters(AUTUMN_MIDNIGHT, 25 * 4)

    rows = build_statistics(points, Quantity.ENERGY_INTERVAL)

    assert rows[-1]["sum"] == pytest.approx(25.0)


def test_repeated_hour_power_is_not_averaged_across_both_hours():
    points = _quarters(AUTUMN_FIRST_0200, 4, value=1.0)
    points += _quarters(AUTUMN_SECOND_0200, 4, value=3.0)

    rows = build_statistics(points, Quantity.POWER)

    assert [(row["min"], row["max"], row["mean"]) for row in rows] == [
        (1.0, 1.0, 1.0),
        (3.0, 3.0, 3.0),
    ]


def test_portal_style_offsets_behave_like_utc_input():
    """The portal sends explicit offsets; they must land in the same hours."""
    points = [
        (datetime.fromisoformat("2026-10-25T01:45:00+02:00"), 1.0),
        (datetime.fromisoformat("2026-10-25T03:00:00+01:00"), 2.0),
    ]

    rows = build_statistics(points, Quantity.ENERGY_INTERVAL)

    assert _utc_starts(rows) == [
        datetime(2026, 10, 24, 23, 0, tzinfo=UTC),
        datetime(2026, 10, 25, 2, 0, tzinfo=UTC),
    ]


def test_partial_first_0200_does_not_take_the_second_one_with_it():
    """A window starting at 02:15 CEST must keep the complete 02:00 CET hour."""
    points = _quarters(AUTUMN_FIRST_0200 + QUARTER, 11)

    kept = drop_incomplete_leading_hour(points)

    assert dt_util.as_utc(kept[0][0]) == AUTUMN_SECOND_0200
    assert len(kept) == 8


def test_partial_second_0200_is_dropped_on_its_own():
    """A window starting at 02:15 CET loses that hour and nothing else."""
    points = _quarters(AUTUMN_SECOND_0200 + QUARTER, 7)

    kept = drop_incomplete_leading_hour(points)

    assert dt_util.as_utc(kept[0][0]) == AUTUMN_SECOND_0200 + HOUR
    assert len(kept) == 4


def test_window_aligned_on_the_first_0200_is_kept_whole():
    points = _quarters(AUTUMN_FIRST_0200, 12)

    assert drop_incomplete_leading_hour(points) == points


def test_spring_day_has_twenty_three_hourly_rows():
    points = _quarters(SPRING_MIDNIGHT, 23 * 4)

    rows = build_statistics(points, Quantity.ENERGY_INTERVAL)

    assert _utc_starts(rows) == [SPRING_MIDNIGHT + HOUR * index for index in range(23)]
    assert all(row["state"] == pytest.approx(1.0) for row in rows)
    # 01:00 CET is followed directly by 03:00 CEST.
    local_hours = [dt_util.as_local(row["start"]).hour for row in rows[:4]]
    assert local_hours == [0, 1, 3, 4]
