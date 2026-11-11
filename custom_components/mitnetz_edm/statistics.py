"""Long-term statistics import for the Mitnetz Strom EDM integration.

The portal delivers *historic* interval data in batches: a poll at 12:00 may
return values timestamped 09:15, 09:30, 09:45 and so on. A plain sensor state
can only ever record "the value at poll time", which is why backfilled interval
data looks like a flat staircase in the history graph.

Home Assistant's long-term statistics accept explicit timestamps, so each
measurement interval can be written to the hour it actually belongs to. We
publish *external* statistics under the ``mitnetz_edm:`` prefix; these live
alongside (and do not conflict with) the statistics the recorder compiles from
the sensor entities themselves.

Statistics rows must be aligned to full hours, so sub-hourly interval values are
aggregated per hour:

* energy lines  -> ``sum`` (and a running cumulative total for the Energy Dashboard)
* power  lines  -> ``mean`` / ``min`` / ``max`` over the intervals in that hour

An hourly row *replaces* whatever was stored for that hour before. Every poll
re-fetches an overlapping window, and a window rarely starts on a full hour, so
its first hour holds only some of that hour's intervals. Writing that partial
hour would overwrite the complete row from an earlier poll; see
:func:`drop_incomplete_leading_hour`.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
import logging

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter, PowerConverter

from .const import DEVICE_NAME, DOMAIN
from .obis import UNIT_KW, UNIT_KWH, Quantity

_LOGGER = logging.getLogger(__name__)


#: Suffix for the derived average-power series of an interval-energy line.
POWER_STATISTIC_SUFFIX = "_power"


def statistic_id_for(line_id: str, suffix: str = "") -> str:
    """Build the external statistic id for a measurement line.

    Args:
        line_id: The portal's line id.
        suffix: Distinguishes additional series derived from the same line,
            e.g. :data:`POWER_STATISTIC_SUFFIX`.

    """
    return f"{DOMAIN}:{line_id.lower()}{suffix}"


def _floor_to_hour(moment: datetime) -> datetime:
    """Return the start of the hour containing ``moment`` (in local time)."""
    local = dt_util.as_local(moment)
    return local.replace(minute=0, second=0, microsecond=0)


def bucket_by_hour(
    points: list[tuple[datetime, float]],
) -> dict[datetime, list[float]]:
    """Group ``(timestamp, value)`` pairs into full-hour buckets.

    Exposed separately so the aggregation can be tested without a running
    Home Assistant instance.
    """
    buckets: dict[datetime, list[float]] = defaultdict(list)
    # Sort first: backfilled batches can arrive out of order, and cumulative
    # registers rely on the last value in a bucket being the latest reading.
    for timestamp, value in sorted(points, key=lambda pair: pair[0]):
        buckets[_floor_to_hour(timestamp)].append(value)
    return dict(buckets)


def drop_incomplete_leading_hour(
    points: list[tuple[datetime, float]],
) -> list[tuple[datetime, float]]:
    """Discard the intervals of a first hour the window only partly covers.

    Each poll re-fetches an overlapping window - the reload window behind the
    newest interval - and that window advances by one interval per poll. Its
    first hour therefore holds three, two, then one of that hour's intervals
    in successive polls, and each of those polls replaced the stored row with
    the partial sum. The last write for almost every hour was the one holding
    only its final quarter, which is why long-term statistics showed about a
    third of the real consumption. Hours came out complete only where the
    window jumped forward, e.g. after the portal's nightly outage.

    The complete row for such an hour was written by an earlier poll, so
    leaving it untouched is correct. A window that starts exactly on the hour
    is kept as it is.
    """
    if not points:
        return points

    ordered = sorted(points, key=lambda pair: pair[0])
    first = dt_util.as_local(ordered[0][0])
    first_hour = _floor_to_hour(first)
    if first == first_hour:
        return ordered

    return [pair for pair in ordered if _floor_to_hour(pair[0]) != first_hour]


def build_statistics(
    points: list[tuple[datetime, float]],
    quantity: Quantity,
    initial_sum: float = 0.0,
) -> list[StatisticData]:
    """Turn interval points into hourly :class:`StatisticData` rows.

    Args:
        points: ``(timestamp, value)`` pairs for a single measurement line.
        quantity: Decides whether the hour is summed or averaged.
        initial_sum: Cumulative total carried over from previously written
            statistics, so the Energy Dashboard sees a continuous series.

    Returns:
        Hourly statistics rows sorted ascending by ``start``.

    """
    buckets = bucket_by_hour(points)
    running_sum = initial_sum
    rows: list[StatisticData] = []

    for hour in sorted(buckets):
        values = buckets[hour]
        if not values:
            continue

        if quantity is Quantity.ENERGY_INTERVAL:
            hourly_total = sum(values)
            running_sum += hourly_total
            rows.append(StatisticData(start=hour, state=hourly_total, sum=running_sum))
        elif quantity is Quantity.ENERGY_TOTAL:
            # Already cumulative: the register reading at the end of the hour.
            latest = values[-1]
            rows.append(StatisticData(start=hour, state=latest, sum=latest))
        else:
            rows.append(
                StatisticData(
                    start=hour,
                    mean=sum(values) / len(values),
                    min=min(values),
                    max=max(values),
                )
            )

    return rows


async def async_import_line_statistics(
    hass: HomeAssistant,
    line_id: str,
    display_name: str,
    quantity: Quantity,
    points: list[tuple[datetime, float]],
    suffix: str = "",
) -> None:
    """Write one line's interval data into long-term statistics.

    Args:
        hass: The Home Assistant instance.
        line_id: The portal's line id, used to build the statistic id.
        display_name: Human-readable name shown in the statistics UI.
        quantity: Decides whether the series is summed or averaged.
        points: ``(timestamp, value)`` pairs to import.
        suffix: Appended to the statistic id, so a line can contribute more than
            one series (its energy plus the power derived from it).

    """
    if not points:
        return

    if quantity is Quantity.UNKNOWN:
        _LOGGER.debug("Skipping statistics for %s: unknown quantity", line_id)
        return

    points = drop_incomplete_leading_hour(points)
    if not points:
        return

    statistic_id = statistic_id_for(line_id, suffix)
    has_sum = quantity in (Quantity.ENERGY_INTERVAL, Quantity.ENERGY_TOTAL)
    unit = UNIT_KWH if has_sum else UNIT_KW

    initial_sum = 0.0
    if quantity is Quantity.ENERGY_INTERVAL:
        initial_sum = await _async_get_previous_sum(hass, statistic_id, points)

    rows = build_statistics(points, quantity, initial_sum=initial_sum)
    if not rows:
        return

    metadata = StatisticMetaData(
        mean_type=StatisticMeanType.NONE if has_sum else StatisticMeanType.ARITHMETIC,
        has_sum=has_sum,
        name=f"{DEVICE_NAME} {display_name}",
        source=DOMAIN,
        statistic_id=statistic_id,
        unit_class=(
            EnergyConverter.UNIT_CLASS if has_sum else PowerConverter.UNIT_CLASS
        ),
        unit_of_measurement=unit,
    )

    _LOGGER.debug(
        "Importing %d hourly statistic row(s) for %s (%s)",
        len(rows),
        statistic_id,
        quantity.value,
    )
    async_add_external_statistics(hass, metadata, rows)


async def async_wait_for_recorder(hass: HomeAssistant) -> None:
    """Wait until the recorder has committed the statistics queued so far.

    ``async_add_external_statistics`` only queues a job. The next import reads
    the previous hour's ``sum`` back from the database, so a long backfill that
    imports chunk after chunk must let each one land first, or the running
    total restarts from a stale value.
    """
    from homeassistant.components.recorder import get_instance

    await get_instance(hass).async_block_till_done()


async def _async_get_previous_sum(
    hass: HomeAssistant,
    statistic_id: str,
    points: list[tuple[datetime, float]],
) -> float:
    """Return the cumulative sum of the hour immediately before our window.

    Two cases have to work at once:

    * a *continuation* -- the new window starts after what is already stored,
      so the running total must carry on from there;
    * an *overlap* -- the reload window deliberately re-fetches hours that were
      already written. Those rows get rewritten, so resuming from the hour
      right before them is what keeps the total from double-counting.

    Looking at the row directly preceding the window handles both, whereas the
    last row overall would be inside the window in the overlap case.
    """
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period

    earliest = _floor_to_hour(min(timestamp for timestamp, _ in points))

    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        earliest - timedelta(hours=1),
        earliest,
        {statistic_id},
        "hour",
        None,
        {"sum"},
    )
    rows = stats.get(statistic_id) if stats else None
    if not rows:
        return 0.0

    return float(rows[-1].get("sum") or 0.0)
