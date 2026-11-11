"""Regression tests: the overlapping fetch window must not truncate hours.

Symptom in production: the external statistics showed roughly a third of what
the meter at the grid connection recorded - quiet hours sometimes matched to
the watt-hour, busy hours (car charging) were far too low. The hourly ``_power``
statistics gave it away: in almost every hour ``min == max``, i.e. the hour had
been built from a single quarter-hour.

Cause: every poll re-fetched the reload window behind the newest interval. That
window advances by one quarter-hour per poll, so its first hour held three, two
and finally one of its quarter-hours, and each poll *replaced* the stored row.
The last write for every hour kept only its final quarter-hour.

A second effect: the portal publishes provisional estimates first and replaces
them with measured values later, so a window that only covers the last 24 hours
can let an interval drop out before it has been finalised.

The replay below runs the real window and aggregation functions against a fake
portal fed with an anonymised two-day load profile.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.mitnetz_edm.api import (
    MeasurementKind,
    MeasurementLine,
    MeasurementPoint,
    TimeseriesPoint,
    TimeseriesResult,
)
from custom_components.mitnetz_edm.const import DOMAIN
from custom_components.mitnetz_edm.coordinator import (
    MitnetzEdmCoordinator,
    compute_fetch_start,
    merge_points,
)
from custom_components.mitnetz_edm.obis import Quantity
from custom_components.mitnetz_edm.statistics import (
    build_statistics,
    drop_incomplete_leading_hour,
    statistic_id_for,
)

from .conftest import LINE_BEZUG, MP_ID

BERLIN = ZoneInfo("Europe/Berlin")
QUARTER = timedelta(minutes=15)
FIXTURE = Path(__file__).parent / "fixtures" / "load_profile_two_days.json"


@pytest.fixture(autouse=True)
def _berlin_time_zone():
    """Local hours and days are Berlin's, as for the real portal."""
    original = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(BERLIN)
    yield
    dt_util.set_default_time_zone(original)


# ---------------------------------------------------------------------------
# A fake portal that behaves like the real one
# ---------------------------------------------------------------------------


@dataclass
class FakePortal:
    """Serves the fixture the way the EDM portal serves a load profile.

    * An interval becomes visible a few minutes after it ends.
    * Until the night after its day it carries the provisional estimate, then
      the measured value.
    * A request for ``[start, end]`` returns every interval whose *end* lies in
      that range - including the one ending exactly at ``start``, which is how
      a 24-hour request comes back with 98 quarter-hours.
    """

    first_day: date
    final: list[float]
    provisional: list[float]
    publish_delay: timedelta = timedelta(minutes=3)
    finalised_at: timedelta = timedelta(hours=26)  # 02:00 the next night
    now: datetime = field(default_factory=dt_util.utcnow)

    @classmethod
    def from_fixture(cls) -> FakePortal:
        doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
        return cls(
            first_day=date.fromisoformat(doc["first_day"]),
            final=doc["final"],
            provisional=doc["provisional"],
        )

    @property
    def origin(self) -> datetime:
        return datetime.combine(self.first_day, datetime.min.time(), tzinfo=BERLIN)

    def interval_start(self, index: int) -> datetime:
        return self.origin + QUARTER * index

    def value(self, index: int) -> float:
        start = self.interval_start(index)
        day_start = start.replace(hour=0, minute=0)
        if self.now >= day_start + self.finalised_at:
            return self.final[index]
        return self.provisional[index]

    def fetch(self, start: datetime, end: datetime) -> list[TimeseriesPoint]:
        points = []
        for index in range(len(self.final)):
            interval_start = self.interval_start(index)
            interval_end = interval_start + QUARTER
            if interval_end + self.publish_delay > self.now:
                break
            if start <= interval_end <= end:
                points.append(TimeseriesPoint(interval_start, self.value(index)))
        return points

    def expected_hours(self) -> dict[datetime, float]:
        """True hourly totals from the settled values."""
        hours: dict[datetime, float] = {}
        for index, value in enumerate(self.final):
            hour = self.interval_start(index).replace(minute=0)
            hours[hour] = hours.get(hour, 0.0) + value
        return hours


# ---------------------------------------------------------------------------
# Replay without Home Assistant: the window and aggregation logic alone
# ---------------------------------------------------------------------------


def _replay(portal: FakePortal, until: datetime, *, legacy: bool):
    """Poll every 15 minutes and upsert hourly rows into a dict, like the recorder.

    ``legacy`` reproduces the behaviour before the fix: the window starts 24 h
    behind the newest interval, and partial leading hours are written.
    """
    store: dict[datetime, tuple[float, float]] = {}
    last_timestamp: datetime | None = None
    reload_window = timedelta(hours=24)
    portal.now = portal.origin + timedelta(minutes=20)

    while portal.now <= until:
        if legacy:
            anchor = last_timestamp or portal.now
            start = anchor - reload_window
        else:
            start = compute_fetch_start(last_timestamp, portal.now, reload_window)

        points = [(p.timestamp, p.value) for p in portal.fetch(start, portal.now)]
        if points:
            last_timestamp = max(timestamp for timestamp, _ in points)
            if not legacy:
                points = drop_incomplete_leading_hour(points)
        if points:
            first_hour = dt_util.as_local(min(t for t, _ in points)).replace(
                minute=0, second=0, microsecond=0
            )
            previous = store.get(first_hour - timedelta(hours=1))
            initial_sum = previous[1] if previous else 0.0
            for row in build_statistics(
                points, Quantity.ENERGY_INTERVAL, initial_sum=initial_sum
            ):
                store[row["start"]] = (row["state"], row["sum"])

        portal.now += QUARTER
    return store


def test_replay_matches_settled_values_hour_by_hour():
    portal = FakePortal.from_fixture()
    until = portal.origin + timedelta(days=2, hours=6)

    store = _replay(portal, until, legacy=False)

    expected = portal.expected_hours()
    assert set(expected) <= set(store)
    for hour, total in expected.items():
        assert store[hour][0] == pytest.approx(total, abs=1e-9), hour


def test_replay_running_sum_matches_daily_totals():
    portal = FakePortal.from_fixture()
    until = portal.origin + timedelta(days=2, hours=6)

    store = _replay(portal, until, legacy=False)

    day_one = sum(portal.final[:96])
    day_two = sum(portal.final[96:192])
    end_day_one = portal.origin + timedelta(hours=23)
    end_day_two = portal.origin + timedelta(hours=47)
    assert store[end_day_one][1] == pytest.approx(day_one)
    assert store[end_day_two][1] - store[end_day_one][1] == pytest.approx(day_two)


def test_legacy_window_reproduces_the_production_symptom():
    """Documents the bug: each hour ends up holding only its last quarter-hour."""
    portal = FakePortal.from_fixture()
    until = portal.origin + timedelta(days=2, hours=6)

    store = _replay(portal, until, legacy=True)

    expected = portal.expected_hours()
    # Day one, which the legacy window has fully passed by the end.
    day_one_hours = sorted(hour for hour in expected if hour.day == portal.origin.day)
    single_quarter = 0
    for hour in day_one_hours:
        index = int((hour - portal.origin) / QUARTER) + 3  # the hour's xx:45
        if store[hour][0] == pytest.approx(portal.value(index), abs=1e-9):
            single_quarter += 1
    assert single_quarter >= 20

    stored_total = sum(store[hour][0] for hour in day_one_hours)
    assert stored_total < 0.5 * sum(portal.final[:96])


def test_lookback_replaces_provisional_values_at_midnight():
    """A 24 h window lets 00:00-01:00 drop out before the portal settles it."""
    portal = FakePortal.from_fixture()
    portal.provisional = [value + 1.0 for value in portal.final]  # clearly off
    until = portal.origin + timedelta(days=2, hours=6)

    store = _replay(portal, until, legacy=False)
    first_hour = portal.origin
    assert store[first_hour][0] == pytest.approx(sum(portal.final[:4]))


# ---------------------------------------------------------------------------
# Unit tests of the building blocks
# ---------------------------------------------------------------------------


def test_partial_leading_hour_is_dropped():
    start = datetime(2026, 3, 10, 14, 45, tzinfo=BERLIN)
    points = [(start + QUARTER * index, 1.0) for index in range(5)]

    kept = drop_incomplete_leading_hour(points)

    assert [timestamp.minute for timestamp, _ in kept] == [0, 15, 30, 45]
    assert all(timestamp.hour == 15 for timestamp, _ in kept)


def test_aligned_window_is_kept_whole():
    start = datetime(2026, 3, 10, 14, 0, tzinfo=BERLIN)
    points = [(start + QUARTER * index, 1.0) for index in range(6)]

    assert drop_incomplete_leading_hour(points) == points


def test_drop_handles_unsorted_and_empty_input():
    start = datetime(2026, 3, 10, 14, 30, tzinfo=BERLIN)
    points = [(start + QUARTER * index, float(index)) for index in range(4)]

    kept = drop_incomplete_leading_hour(list(reversed(points)))

    assert kept == points[2:]
    assert drop_incomplete_leading_hour([]) == []


def test_fetch_start_is_a_full_hour_at_least_two_days_back():
    now = datetime(2026, 3, 12, 16, 22, tzinfo=BERLIN)
    last = datetime(2026, 3, 12, 16, 0, tzinfo=BERLIN)

    start = compute_fetch_start(last, now, timedelta(hours=24))

    assert start == datetime(2026, 3, 10, 0, 0, tzinfo=BERLIN)


def test_fetch_start_honours_a_longer_reload_window():
    now = datetime(2026, 3, 12, 16, 22, tzinfo=BERLIN)
    last = datetime(2026, 3, 12, 16, 0, tzinfo=BERLIN)

    start = compute_fetch_start(last, now, timedelta(days=5, minutes=20))

    assert start == datetime(2026, 3, 7, 15, 0, tzinfo=BERLIN)


def test_fetch_start_across_daylight_saving_change():
    """The lookback counts calendar days, not 48 hours."""
    now = datetime(2026, 3, 30, 1, 10, tzinfo=BERLIN)  # DST began 29 March

    start = compute_fetch_start(None, now, timedelta(minutes=15))

    assert start == datetime(2026, 3, 28, 0, 0, tzinfo=BERLIN)


def test_merge_points_deduplicates_chunk_boundaries():
    start = datetime(2026, 3, 10, 0, 0, tzinfo=BERLIN)
    first = [TimeseriesPoint(start + QUARTER * index, 1.0) for index in range(4)]
    second = [TimeseriesPoint(start + QUARTER * index, 2.0) for index in range(3, 6)]

    merged = merge_points([second, first])

    assert [p.timestamp for p in merged] == [start + QUARTER * i for i in range(6)]
    # The batch passed last wins for the shared interval.
    assert merged[3].value == 1.0


# ---------------------------------------------------------------------------
# The same replay through the real coordinator and recorder
# ---------------------------------------------------------------------------


@pytest.fixture
def _recorder(integration_env):
    yield


def _fake_api(portal: FakePortal) -> MagicMock:
    api = MagicMock()
    api.is_logged_in = True
    api.get_measurement_points = AsyncMock(
        return_value=[
            MeasurementPoint(
                id=MP_ID, lines=[MeasurementLine(id=LINE_BEZUG, name="1-1:1.29.0")]
            )
        ]
    )

    async def _get_timeseries(meterpoint, line, start, end):
        return TimeseriesResult(
            points=portal.fetch(start, end), kind=MeasurementKind.INTERVAL_ENERGY
        )

    api.get_timeseries = AsyncMock(side_effect=_get_timeseries)
    return api


async def _hourly_rows(hass, statistic_id, start, end):
    from homeassistant.components.recorder import get_instance

    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end,
        {statistic_id},
        "hour",
        None,
        {"state", "sum", "min", "max"},
    )
    return stats.get(statistic_id, [])


async def test_coordinator_replay_writes_complete_hours(_recorder, hass, freezer):
    portal = FakePortal.from_fixture()
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    coordinator = MitnetzEdmCoordinator(
        hass,
        entry,
        _fake_api(portal),
        update_interval=QUARTER,
        reload_window=timedelta(hours=24),
    )

    portal.now = portal.origin + timedelta(minutes=20)
    until = portal.origin + timedelta(days=1, hours=4)
    while portal.now <= until:
        freezer.move_to(portal.now)
        coordinator.data = await coordinator._async_update_data()
        await async_wait_recording_done(hass)
        portal.now += QUARTER

    rows = await _hourly_rows(
        hass,
        statistic_id_for(LINE_BEZUG),
        portal.origin,
        portal.origin + timedelta(days=1),
    )
    expected = portal.expected_hours()
    assert len(rows) == 24
    for row in rows:
        hour = dt_util.as_local(dt_util.utc_from_timestamp(row["start"]))
        assert row["state"] == pytest.approx(expected[hour], abs=1e-9), hour

    power_rows = await _hourly_rows(
        hass,
        statistic_id_for(LINE_BEZUG, "_power"),
        portal.origin,
        portal.origin + timedelta(days=1),
    )
    # Each hour aggregates four quarter-hours; a single-quarter hour has
    # min == max, which is how the bug showed up in production.
    varying = sum(1 for row in power_rows if row["min"] != row["max"])
    assert varying >= 20
