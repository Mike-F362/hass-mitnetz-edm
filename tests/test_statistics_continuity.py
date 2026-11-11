"""End-to-end tests: the stored series must be complete and its sum continuous.

``import_history`` exists to repair what an earlier version left behind - rows
holding a single quarter-hour - and the Energy Dashboard reads the *difference*
of the running ``sum`` between two hours. A row that is skipped, written twice
or resumed from the wrong total shows up there as a spike, so these tests check
every hour and every step of the sum against a real (in-memory) recorder.

The portal is replaced by a deterministic stand-in: each quarter-hour has its
own value, so an hour built from fewer than four intervals cannot pass by
accident.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    async_list_statistic_ids,
    statistics_during_period,
)
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter
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
from custom_components.mitnetz_edm.const import DOMAIN, HISTORY_CHUNK
from custom_components.mitnetz_edm.coordinator import MitnetzEdmCoordinator
from custom_components.mitnetz_edm.obis import Quantity
from custom_components.mitnetz_edm.statistics import (
    POWER_STATISTIC_SUFFIX,
    async_import_line_statistics,
    statistic_id_for,
)

from .conftest import LINE_BEZUG, MP_ID

BERLIN = ZoneInfo("Europe/Berlin")
QUARTER = timedelta(minutes=15)
HOUR = timedelta(hours=1)
QUARTER_SECONDS = 900

NOW = datetime(2026, 11, 11, 17, 40, tzinfo=BERLIN)
ENERGY_ID = statistic_id_for(LINE_BEZUG)
POWER_ID = statistic_id_for(LINE_BEZUG, POWER_STATISTIC_SUFFIX)


@pytest.fixture(autouse=True)
def _env(integration_env):
    yield


# ---------------------------------------------------------------------------
# A deterministic stand-in for the portal
# ---------------------------------------------------------------------------


def quarter_value(start: datetime) -> float:
    """Energy of the interval starting at ``start``; differs within every hour."""
    index = int(start.timestamp()) // QUARTER_SECONDS
    return round(0.05 + (index % 97) * 0.01, 2)


def hour_total(hour: datetime) -> float:
    """What a complete hourly row must hold."""
    return sum(quarter_value(hour + QUARTER * index) for index in range(4))


def _portal_time(epoch: int) -> datetime:
    """A timestamp as the API client parses it: a fixed offset, not a zone."""
    local = datetime.fromtimestamp(epoch, BERLIN)
    return local.replace(tzinfo=timezone(local.utcoffset()))


def portal_fetch(start: datetime, end: datetime) -> list[TimeseriesPoint]:
    """Every published interval whose *end* lies in ``[start, end]``.

    Like the real portal this includes the interval ending exactly at
    ``start``, so a request starting on the hour comes back with a leading
    xx:45 the import has to discard.
    """
    last_end = min(end, dt_util.utcnow()).timestamp()
    interval_end = math.ceil(start.timestamp() / QUARTER_SECONDS) * QUARTER_SECONDS
    points = []
    while interval_end <= last_end:
        timestamp = _portal_time(interval_end - QUARTER_SECONDS)
        points.append(TimeseriesPoint(timestamp, quarter_value(timestamp)))
        interval_end += QUARTER_SECONDS
    return points


def _fake_api() -> MagicMock:
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
            points=portal_fetch(start, end), kind=MeasurementKind.INTERVAL_ENERGY
        )

    api.get_timeseries = AsyncMock(side_effect=_get_timeseries)
    return api


@pytest.fixture
async def coordinator(hass, freezer):
    """A coordinator on Berlin time talking to the stand-in portal."""
    original = dt_util.get_default_time_zone()
    await hass.config.async_set_time_zone("Europe/Berlin")
    freezer.move_to(NOW)

    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    coordinator = MitnetzEdmCoordinator(
        hass,
        entry,
        _fake_api(),
        update_interval=QUARTER,
        reload_window=timedelta(hours=24),
    )
    yield coordinator
    await coordinator.async_shutdown()
    dt_util.set_default_time_zone(original)


# ---------------------------------------------------------------------------
# Recorder helpers
# ---------------------------------------------------------------------------


async def _seed_truncated_rows(hass, first_hour: datetime, until: datetime) -> None:
    """Store what the faulty version left behind: only xx:45 of each hour.

    Written straight to the recorder, so the seed does not depend on the code
    under test.
    """
    rows = []
    running_sum = 0.0
    hour = dt_util.as_utc(first_hour)
    while hour + HOUR <= until:
        value = quarter_value(hour + QUARTER * 3)
        running_sum += value
        rows.append(StatisticData(start=hour, state=value, sum=running_sum))
        hour += HOUR

    metadata = StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name="Seed",
        source=DOMAIN,
        statistic_id=ENERGY_ID,
        unit_class=EnergyConverter.UNIT_CLASS,
        unit_of_measurement="kWh",
    )
    async_add_external_statistics(hass, metadata, rows)
    await async_wait_recording_done(hass)


async def _hourly_rows(hass, statistic_id: str, start: datetime, end: datetime):
    from homeassistant.components.recorder import get_instance

    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end,
        {statistic_id},
        "hour",
        None,
        {"state", "sum", "min", "max", "mean"},
    )
    return stats.get(statistic_id, [])


def _assert_complete_and_continuous(rows, first_hour: datetime, carry: float) -> None:
    """Every hour holds four intervals and the sum advances by exactly that."""
    previous_sum = carry
    hour = dt_util.as_utc(first_hour)
    for row in rows:
        assert row["start"] == hour.timestamp(), f"missing row for {hour}"
        assert row["state"] == pytest.approx(hour_total(hour)), hour
        assert row["sum"] - previous_sum == pytest.approx(row["state"]), hour
        previous_sum = row["sum"]
        hour += HOUR


def _hours_between(start: datetime, end: datetime) -> int:
    return int((dt_util.as_utc(end) - dt_util.as_utc(start)) / HOUR)


# ---------------------------------------------------------------------------
# import_history
# ---------------------------------------------------------------------------


async def test_import_history_replaces_truncated_hours(hass, coordinator):
    """The repair run: single-quarter rows become complete, the sum stays whole."""
    start = datetime(2026, 11, 1, 0, 0, tzinfo=BERLIN)
    last_full_hour = NOW.replace(minute=0)
    assert NOW - start > HISTORY_CHUNK  # the range spans more than one chunk

    await _seed_truncated_rows(hass, start - HOUR * 3, NOW)
    before = await _hourly_rows(hass, ENERGY_ID, start - HOUR * 3, last_full_hour)
    carry = before[2]["sum"]  # the hour right before the import range
    # Precondition, the production symptom: an hour holds only its last quarter.
    assert before[3]["state"] == pytest.approx(quarter_value(start + QUARTER * 3))

    await coordinator.async_import_history(start, NOW)
    await async_wait_recording_done(hass)

    rows = await _hourly_rows(hass, ENERGY_ID, start, last_full_hour)
    assert len(rows) == _hours_between(start, last_full_hour)
    _assert_complete_and_continuous(rows, start, carry)

    # The hours before the range are not touched.
    untouched = await _hourly_rows(hass, ENERGY_ID, start - HOUR * 3, start)
    assert [row["sum"] for row in untouched] == [row["sum"] for row in before[:3]]


async def test_import_history_fills_the_derived_power_series(hass, coordinator):
    start = datetime(2026, 11, 9, 0, 0, tzinfo=BERLIN)
    last_full_hour = NOW.replace(minute=0)

    await coordinator.async_import_history(start, NOW)
    await async_wait_recording_done(hass)

    rows = await _hourly_rows(hass, POWER_ID, start, last_full_hour)
    assert len(rows) == _hours_between(start, last_full_hour)
    hour = dt_util.as_utc(start)
    for row in rows:
        quarters = [quarter_value(hour + QUARTER * index) * 4 for index in range(4)]
        assert row["min"] == pytest.approx(min(quarters)), hour
        assert row["max"] == pytest.approx(max(quarters)), hour
        assert row["mean"] == pytest.approx(sum(quarters) / 4), hour
        hour += HOUR


async def test_import_history_twice_changes_nothing(hass, coordinator):
    start = datetime(2026, 11, 9, 0, 0, tzinfo=BERLIN)

    await coordinator.async_import_history(start, NOW)
    await async_wait_recording_done(hass)
    first = await _hourly_rows(hass, ENERGY_ID, start, NOW)

    await coordinator.async_import_history(start, NOW)
    await async_wait_recording_done(hass)
    second = await _hourly_rows(hass, ENERGY_ID, start, NOW)

    assert first
    assert second == first


async def test_polling_after_an_import_continues_the_sum(hass, coordinator, freezer):
    """The regular window rewrites the last days; it must agree with the import."""
    start = datetime(2026, 11, 5, 0, 0, tzinfo=BERLIN)
    await _seed_truncated_rows(hass, start - HOUR * 3, NOW)

    await coordinator.async_import_history(start, NOW)
    await async_wait_recording_done(hass)
    carry = (await _hourly_rows(hass, ENERGY_ID, start - HOUR, start))[0]["sum"]

    later = NOW + timedelta(hours=3)
    moment = NOW
    while moment < later:
        moment += QUARTER
        freezer.move_to(moment)
        coordinator.data = await coordinator._async_update_data()
        await async_wait_recording_done(hass)

    last_full_hour = later.replace(minute=0)
    rows = await _hourly_rows(hass, ENERGY_ID, start, last_full_hour)
    assert len(rows) == _hours_between(start, last_full_hour)
    _assert_complete_and_continuous(rows, start, carry)


async def test_import_history_across_the_autumn_time_change(hass, coordinator, freezer):
    """25 October 2026 has 25 hours; each needs its own complete row."""
    now = datetime(2026, 10, 28, 12, 0, tzinfo=BERLIN)
    freezer.move_to(now)
    start = datetime(2026, 10, 24, 0, 0, tzinfo=BERLIN)
    end_of_check = datetime(2026, 10, 27, 0, 0, tzinfo=BERLIN)

    await _seed_truncated_rows(hass, start - HOUR * 3, now)
    carry = (await _hourly_rows(hass, ENERGY_ID, start - HOUR, start))[0]["sum"]

    await coordinator.async_import_history(start, now)
    await async_wait_recording_done(hass)

    rows = await _hourly_rows(hass, ENERGY_ID, start, end_of_check)
    assert len(rows) == 24 + 25 + 24
    _assert_complete_and_continuous(rows, start, carry)


async def test_import_history_across_the_spring_time_change(hass, coordinator, freezer):
    """29 March 2026 has 23 hours; no row may be invented for the skipped one."""
    now = datetime(2026, 4, 1, 12, 0, tzinfo=BERLIN)
    freezer.move_to(now)
    start = datetime(2026, 3, 28, 0, 0, tzinfo=BERLIN)
    end_of_check = datetime(2026, 3, 31, 0, 0, tzinfo=BERLIN)

    await coordinator.async_import_history(start, now)
    await async_wait_recording_done(hass)

    rows = await _hourly_rows(hass, ENERGY_ID, start, end_of_check)
    assert len(rows) == 24 + 23 + 24
    _assert_complete_and_continuous(rows, start, carry=0.0)


# ---------------------------------------------------------------------------
# The running sum across a hole in the stored series
# ---------------------------------------------------------------------------


async def test_window_after_a_gap_continues_the_sum(hass):
    """Hours without a row between two imports must not reset the total.

    This is what a restart after a few days offline looks like: the next
    window starts later than the stored series ends.
    """
    base = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(
        days=2
    )
    first = [(base + QUARTER * index, 0.5) for index in range(4)]
    later = [(base + HOUR * 3 + QUARTER * index, 0.25) for index in range(4)]

    # The last window is imported twice: once it is stored, the newest row lies
    # inside the window and must not be mistaken for the one to resume from.
    for points in (first, later, later):
        await async_import_line_statistics(
            hass,
            line_id=LINE_BEZUG,
            display_name="Import",
            quantity=Quantity.ENERGY_INTERVAL,
            points=points,
        )
        await async_wait_recording_done(hass)

    rows = await _hourly_rows(hass, ENERGY_ID, base, base + HOUR * 4)
    assert [row["sum"] for row in rows] == pytest.approx([2.0, 3.0])


async def test_window_before_the_first_stored_row_starts_at_zero(hass):
    """Rows *after* the window say nothing about the total before it."""
    base = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(
        days=2
    )
    stored = [(base + HOUR * 3 + QUARTER * index, 0.25) for index in range(4)]
    earlier = [(base + QUARTER * index, 0.5) for index in range(4)]

    for points in (stored, earlier):
        await async_import_line_statistics(
            hass,
            line_id=LINE_BEZUG,
            display_name="Import",
            quantity=Quantity.ENERGY_INTERVAL,
            points=points,
        )
        await async_wait_recording_done(hass)

    rows = await _hourly_rows(hass, ENERGY_ID, base, base + HOUR)
    assert [row["sum"] for row in rows] == pytest.approx([2.0])


async def test_restart_after_days_offline_resumes_the_total(hass, coordinator, freezer):
    """The window reaches back two days; an older series must still be continued."""
    start = datetime(2026, 11, 1, 0, 0, tzinfo=BERLIN)
    offline_from = datetime(2026, 11, 4, 0, 0, tzinfo=BERLIN)
    freezer.move_to(offline_from + timedelta(minutes=5))
    await coordinator.async_import_history(start, offline_from)
    await async_wait_recording_done(hass)
    stored = await _hourly_rows(hass, ENERGY_ID, start, offline_from)
    total_before = stored[-1]["sum"]

    # A fresh coordinator, as after a restart, a week later.
    freezer.move_to(NOW)
    restarted = MitnetzEdmCoordinator(
        hass,
        coordinator.config_entry,
        _fake_api(),
        update_interval=QUARTER,
        reload_window=timedelta(hours=24),
    )
    restarted.data = await restarted._async_update_data()
    await async_wait_recording_done(hass)
    await restarted.async_shutdown()

    window_start = datetime(2026, 11, 9, 0, 0, tzinfo=BERLIN)
    rows = await _hourly_rows(hass, ENERGY_ID, window_start, NOW.replace(minute=0))
    assert len(rows) == _hours_between(window_start, NOW.replace(minute=0))
    _assert_complete_and_continuous(rows, window_start, carry=total_before)


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


async def test_statistics_metadata_declares_unit_class_and_mean_type(hass):
    """Home Assistant 2026.11 rejects external statistics without them."""
    base = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(
        days=2
    )
    points = [(base + QUARTER * index, 0.5) for index in range(4)]
    await async_import_line_statistics(
        hass,
        line_id=LINE_BEZUG,
        display_name="Import",
        quantity=Quantity.ENERGY_INTERVAL,
        points=points,
    )
    await async_import_line_statistics(
        hass,
        line_id=LINE_BEZUG,
        display_name="Import Power",
        quantity=Quantity.POWER,
        points=points,
        suffix=POWER_STATISTIC_SUFFIX,
    )
    await async_wait_recording_done(hass)

    listed = await async_list_statistic_ids(hass, {ENERGY_ID, POWER_ID})
    by_id = {item["statistic_id"]: item for item in listed}

    energy = by_id[ENERGY_ID]
    assert energy["has_sum"] is True
    assert energy["mean_type"] is StatisticMeanType.NONE
    assert energy["unit_class"] == "energy"
    assert energy["statistics_unit_of_measurement"] == "kWh"
    assert energy["source"] == DOMAIN

    power = by_id[POWER_ID]
    assert power["has_sum"] is False
    assert power["mean_type"] is StatisticMeanType.ARITHMETIC
    assert power["unit_class"] == "power"
    assert power["statistics_unit_of_measurement"] == "kW"
