"""End-to-end tests for statistics actually landing in the recorder.

These run against a real (in-memory) recorder rather than a mock, because the
risky parts are the recorder interaction and the guard against double-counting
energy when an overlapping window is re-imported.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.mitnetz_edm.const import DOMAIN
from custom_components.mitnetz_edm.obis import Quantity
from custom_components.mitnetz_edm.statistics import (
    async_import_line_statistics,
    statistic_id_for,
)

from .conftest import LINE_BEZUG, LINE_MAX_EINSPEISUNG, make_series


@pytest.fixture(autouse=True)
def _env(integration_env):
    yield


def _base_time() -> datetime:
    """A fixed hour-aligned moment in the past, in local time."""
    return dt_util.as_local(dt_util.utcnow()).replace(
        minute=0, second=0, microsecond=0
    ) - timedelta(days=2)


async def _read_stats(hass, statistic_id, start, types):
    from homeassistant.components.recorder import get_instance

    return await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start - timedelta(hours=1),
        None,
        {statistic_id},
        "hour",
        None,
        types,
    )


async def test_power_statistics_written_to_recorder(hass):
    start = _base_time()
    points = [(start + timedelta(minutes=15 * i), v) for i, v in enumerate([4.0, 2.0, 3.0, 1.0])]

    await async_import_line_statistics(
        hass,
        line_id=LINE_MAX_EINSPEISUNG,
        display_name="Export Maximum",
        quantity=Quantity.POWER,
        points=points,
    )
    await async_wait_recording_done(hass)

    statistic_id = statistic_id_for(LINE_MAX_EINSPEISUNG)
    stats = await _read_stats(hass, statistic_id, start, {"mean", "min", "max"})

    assert statistic_id in stats
    row = stats[statistic_id][0]
    assert row["max"] == pytest.approx(4.0)
    assert row["min"] == pytest.approx(1.0)
    assert row["mean"] == pytest.approx(2.5)


async def test_energy_statistics_written_with_cumulative_sum(hass):
    start = _base_time()
    points = [
        (start + timedelta(minutes=15 * i), 0.5)
        for i in range(8)  # two hours of 0.5 kWh quarters
    ]

    await async_import_line_statistics(
        hass,
        line_id=LINE_BEZUG,
        display_name="Import",
        quantity=Quantity.ENERGY_INTERVAL,
        points=points,
    )
    await async_wait_recording_done(hass)

    statistic_id = statistic_id_for(LINE_BEZUG)
    stats = await _read_stats(hass, statistic_id, start, {"state", "sum"})

    rows = stats[statistic_id]
    assert len(rows) == 2
    assert rows[0]["sum"] == pytest.approx(2.0)
    assert rows[1]["sum"] == pytest.approx(4.0)


async def test_reimporting_same_window_does_not_double_count(hass):
    """The overlap window means the same hours get fetched again every poll."""
    start = _base_time()
    points = [(start + timedelta(minutes=15 * i), 0.5) for i in range(4)]

    for _ in range(3):
        await async_import_line_statistics(
            hass,
            line_id=LINE_BEZUG,
            display_name="Import",
            quantity=Quantity.ENERGY_INTERVAL,
            points=points,
        )
        await async_wait_recording_done(hass)

    statistic_id = statistic_id_for(LINE_BEZUG)
    stats = await _read_stats(hass, statistic_id, start, {"state", "sum"})

    rows = stats[statistic_id]
    assert len(rows) == 1
    # 4 x 0.5 kWh = 2.0, regardless of how often it was imported.
    assert rows[0]["sum"] == pytest.approx(2.0)


async def test_later_window_continues_the_sum(hass):
    """A subsequent, non-overlapping hour must continue the running total."""
    start = _base_time()
    first = [(start + timedelta(minutes=15 * i), 0.5) for i in range(4)]
    second = [(start + timedelta(hours=1, minutes=15 * i), 0.25) for i in range(4)]

    await async_import_line_statistics(
        hass,
        line_id=LINE_BEZUG,
        display_name="Import",
        quantity=Quantity.ENERGY_INTERVAL,
        points=first,
    )
    await async_wait_recording_done(hass)

    await async_import_line_statistics(
        hass,
        line_id=LINE_BEZUG,
        display_name="Import",
        quantity=Quantity.ENERGY_INTERVAL,
        points=second,
    )
    await async_wait_recording_done(hass)

    statistic_id = statistic_id_for(LINE_BEZUG)
    stats = await _read_stats(hass, statistic_id, start, {"state", "sum"})

    rows = stats[statistic_id]
    assert len(rows) == 2
    assert rows[0]["sum"] == pytest.approx(2.0)
    assert rows[1]["sum"] == pytest.approx(3.0)


async def test_unknown_quantity_is_skipped(hass):
    start = _base_time()
    points = [(start, 1.0)]

    await async_import_line_statistics(
        hass,
        line_id="LINE_UNKNOWN",
        display_name="Unbekannt",
        quantity=Quantity.UNKNOWN,
        points=points,
    )
    await async_wait_recording_done(hass)

    statistic_id = statistic_id_for("LINE_UNKNOWN")
    stats = await _read_stats(hass, statistic_id, start, {"mean"})

    assert statistic_id not in stats


async def test_no_points_writes_nothing(hass):
    await async_import_line_statistics(
        hass,
        line_id="LINE_EMPTY",
        display_name="Leer",
        quantity=Quantity.POWER,
        points=[],
    )
    await async_wait_recording_done(hass)

    statistic_id = statistic_id_for("LINE_EMPTY")
    stats = await _read_stats(hass, statistic_id, _base_time(), {"mean"})

    assert statistic_id not in stats


async def test_statistics_appear_after_full_integration_setup(
    hass, mock_api, timeseries_by_line, config_entry_data, config_entry_options
):
    """The whole chain: poll -> parse -> classify -> statistics."""
    start = _base_time()
    timeseries_by_line[LINE_MAX_EINSPEISUNG] = make_series(
        [4.67, 2.61, 2.13, 0.65], start=start
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=config_entry_data,
        options=config_entry_options,
        unique_id="stats-user",
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    statistic_id = statistic_id_for(LINE_MAX_EINSPEISUNG)
    stats = await _read_stats(hass, statistic_id, start, {"mean", "min", "max"})

    assert statistic_id in stats
    row = stats[statistic_id][0]
    # Falling demand must be preserved, not flattened into a rising total.
    assert row["max"] == pytest.approx(4.67)
    assert row["min"] == pytest.approx(0.65)
