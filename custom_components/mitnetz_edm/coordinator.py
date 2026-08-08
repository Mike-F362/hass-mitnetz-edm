"""DataUpdateCoordinator for the Mitnetz Strom EDM integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging

from homeassistant.components.persistent_notification import (
    async_create as pn_create,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    MeasurementPoint,
    MitnetzAuthError,
    MitnetzEdmApiClient,
    MitnetzSessionExpiredError,
    TimeseriesResult,
)
from .const import (
    DEFAULT_STALE_INTERVALS,
    DEVICE_NAME,
    DOMAIN,
    FALLBACK_MEASUREMENT_INTERVAL_MIN,
    LOGIN_RETRY_DELAYS,
    MAX_LOGIN_ATTEMPTS,
    NAME_POWER_SUFFIX,
)
from .obis import Quantity, classify_line
from .power import derive_power_points, energy_to_power
from .statistics import POWER_STATISTIC_SUFFIX, async_import_line_statistics

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class LineData:
    """Latest known data for a single OBIS line."""

    point_id: str
    line_id: str
    obis_code: str
    result: TimeseriesResult
    last_value: float | None = None
    last_timestamp: datetime | None = None
    #: Measurement interval derived from the returned timestamps.
    interval: timedelta | None = None

    @property
    def interval_end(self) -> datetime | None:
        """End of the interval the current value belongs to."""
        if self.last_timestamp is None:
            return None
        if self.interval is None:
            return self.last_timestamp
        return self.last_timestamp + self.interval

    def age(self, now: datetime) -> timedelta | None:
        """How long ago the current value's interval ended."""
        end = self.interval_end
        if end is None:
            return None
        return now - end

    def is_stale(self, now: datetime, max_intervals: int) -> bool:
        """Whether the value is too old to still be meaningful.

        A min/max line that has not been refreshed for several intervals is not
        "still 4.7 kW" - it is simply unknown, and reporting the old number
        paints a flat plateau across the history graph.
        """
        age = self.age(now)
        if age is None:
            return True
        interval = self.interval or timedelta(minutes=FALLBACK_MEASUREMENT_INTERVAL_MIN)
        return age > interval * max_intervals

    @property
    def effective_interval(self) -> timedelta:
        """Measurement interval, falling back to the configured default."""
        return self.interval or timedelta(minutes=FALLBACK_MEASUREMENT_INTERVAL_MIN)

    @property
    def last_power(self) -> float | None:
        """Average power of the most recent interval, in kW.

        Only meaningful for interval-energy lines; see :mod:`.power`.
        """
        if self.last_value is None:
            return None
        return energy_to_power(self.last_value, self.effective_interval)

    @property
    def power_points(self) -> list[tuple[datetime, float]]:
        """The whole fetched window converted to ``(timestamp, kW)`` pairs."""
        return derive_power_points(
            [(p.timestamp, p.value) for p in self.result.points],
            self.effective_interval,
        )


@dataclass(slots=True)
class MitnetzEdmData:
    """Aggregated data held by the coordinator between updates."""

    points: list[MeasurementPoint] = field(default_factory=list)
    lines: dict[str, LineData] = field(default_factory=dict)  # keyed by line_id
    last_successful_update: datetime | None = None


class MitnetzEdmCoordinator(DataUpdateCoordinator[MitnetzEdmData]):
    """Coordinates polling of the Mitnetz EDM portal.

    Handles automatic re-login on session expiry with capped retries and
    exponential-ish backoff, to avoid triggering an account lockout on the
    Mitnetz side. After exhausting retries, the coordinator becomes
    unavailable, logs an error, and raises a persistent notification; it
    will not attempt further logins until the next scheduled poll.
    """

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        api: MitnetzEdmApiClient,
        update_interval: timedelta,
        reload_window: timedelta,
        stale_intervals: int = DEFAULT_STALE_INTERVALS,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=config_entry,
            update_interval=update_interval,
        )
        self.api = api
        self.reload_window = reload_window
        self.stale_intervals = stale_intervals
        self._data = MitnetzEdmData()
        self.measurement_point_count = 0
        self.sensor_count = 0
        self.line_count = 0
        self.derived_power_count = 0
        #: Measurement interval detected from the portal data, for diagnostics.
        self.measurement_interval: timedelta | None = None
        self._interval_warning_issued = False

    async def _async_update_data(self) -> MitnetzEdmData:
        """Fetch data from the portal, recovering from session expiry."""
        if not self.api.is_logged_in:
            await self._ensure_logged_in()

        try:
            return await self._fetch_all()
        except MitnetzSessionExpiredError:
            _LOGGER.debug("Session expired mid-update, re-authenticating")
            await self._ensure_logged_in()
            return await self._fetch_all()

    async def _ensure_logged_in(self) -> None:
        """Log in, retrying up to MAX_LOGIN_ATTEMPTS times with backoff."""
        last_error: Exception | None = None
        for attempt in range(1, MAX_LOGIN_ATTEMPTS + 1):
            try:
                await self.api.login()
                return
            except MitnetzAuthError as err:
                last_error = err
                _LOGGER.error(
                    "Login attempt %d/%d failed: %s", attempt, MAX_LOGIN_ATTEMPTS, err
                )
            except Exception as err:
                last_error = err
                _LOGGER.error(
                    "Login attempt %d/%d errored: %s", attempt, MAX_LOGIN_ATTEMPTS, err
                )

            if attempt < MAX_LOGIN_ATTEMPTS:
                delay = LOGIN_RETRY_DELAYS[
                    min(attempt - 1, len(LOGIN_RETRY_DELAYS) - 1)
                ]
                _LOGGER.debug("Waiting %ds before next login attempt", delay)
                await asyncio.sleep(delay)

        # All attempts exhausted.
        pn_create(
            self.hass,
            (
                f"Signing in to the {DEVICE_NAME} portal failed repeatedly. No "
                "further login attempts will be made for now, to avoid locking "
                "the account out. Check the credentials via Reconfigure, then "
                "reload the integration."
            ),
            title=f"{DEVICE_NAME}: login failed",
            notification_id=f"{DOMAIN}_login_failed",
        )
        raise UpdateFailed(
            f"Could not establish a session after {MAX_LOGIN_ATTEMPTS} attempts: "
            f"{last_error}"
        )

    async def _fetch_all(self) -> MitnetzEdmData:
        points = await self.api.get_measurement_points()
        self.measurement_point_count = len(points)

        lines: dict[str, LineData] = {}
        now = dt_util.utcnow()

        for point in points:
            for line in point.lines:
                start = self._compute_start(line.id, now)
                result = await self.api.get_timeseries(
                    meterpoint=point.id, line=line.id, start=start, end=now
                )
                latest = result.latest
                interval = result.interval

                lines[line.id] = LineData(
                    point_id=point.id,
                    line_id=line.id,
                    obis_code=line.name,
                    result=result,
                    last_value=latest.value if latest else None,
                    last_timestamp=latest.timestamp if latest else None,
                    interval=interval,
                )

                self._warn_on_interval_mismatch(line.id, interval)

                await self._async_import_statistics(line.id, line.name, lines[line.id])

        # One sensor per line, plus a derived power sensor for each
        # interval-energy line (see sensor.MitnetzEdmDerivedPowerSensor).
        self.line_count = len(lines)
        self.derived_power_count = sum(
            classify_line(data.obis_code).quantity is Quantity.ENERGY_INTERVAL
            for data in lines.values()
        )
        self.sensor_count = self.line_count + self.derived_power_count
        self._data = MitnetzEdmData(
            points=points,
            lines=lines,
            last_successful_update=now,
        )
        return self._data

    def _warn_on_interval_mismatch(
        self, line_id: str, interval: timedelta | None
    ) -> None:
        """Warn once if we poll more slowly than the meter measures.

        Home Assistant cannot backdate entity states: if a poll returns three
        new intervals at once, only the newest one ever reaches the state
        machine and the other two are visible in long-term statistics only.
        Polling at (or below) the measurement interval keeps the state history
        faithful.
        """
        if interval is None:
            return

        self.measurement_interval = interval

        if self.update_interval is None or self.update_interval <= interval:
            return

        if self._interval_warning_issued:
            return
        self._interval_warning_issued = True

        _LOGGER.warning(
            "Update interval (%s) is longer than the meter's measurement "
            "interval (%s, detected on line %s). Only the newest interval "
            "reaches the sensor state; intermediate values are recorded in "
            "long-term statistics only. Set the update interval to %d minutes "
            "or less for a complete state history.",
            self.update_interval,
            interval,
            line_id,
            max(1, int(interval.total_seconds() // 60)),
        )

    async def _async_import_statistics(
        self, line_id: str, obis_code: str, line_data: LineData
    ) -> None:
        """Write the fetched intervals into long-term statistics.

        Each interval keeps its own timestamp, so backfilled data lands in the
        hour it belongs to instead of at poll time. Failures here must not take
        the whole update down.

        Interval-energy lines contribute a second series: the average power
        derived from them. Home Assistant stores power statistics as
        mean/min/max per hour, which is what makes a per-hour peak query
        possible at all - the energy series alone can only be summed.
        """
        result = line_data.result
        if not result.points:
            return

        classification = classify_line(obis_code)
        pairs = [(p.timestamp, p.value) for p in result.points]
        try:
            await async_import_line_statistics(
                self.hass,
                line_id=line_id,
                display_name=classification.display_name,
                quantity=classification.quantity,
                points=pairs,
            )
        except Exception:
            _LOGGER.exception("Failed to import statistics for line %s", line_id)

        if classification.quantity is not Quantity.ENERGY_INTERVAL:
            return

        try:
            await async_import_line_statistics(
                self.hass,
                line_id=line_id,
                display_name=f"{classification.display_name} {NAME_POWER_SUFFIX}",
                quantity=Quantity.POWER,
                points=line_data.power_points,
                suffix=POWER_STATISTIC_SUFFIX,
            )
        except Exception:
            _LOGGER.exception("Failed to import power statistics for line %s", line_id)

    def _compute_start(self, line_id: str, now: datetime) -> datetime:
        """Compute where to start fetching for a line.

        The last known timestamp minus the overlap window, or the configured
        reload window when nothing has been fetched yet.
        """
        existing = self._data.lines.get(line_id)
        if existing and existing.last_timestamp is not None:
            return existing.last_timestamp - self.reload_window
        return now - self.reload_window

    async def async_import_history(self, start: datetime, end: datetime) -> None:
        """Service handler: re-import an arbitrary historical window."""
        if not self.api.is_logged_in:
            await self._ensure_logged_in()

        points = (
            self.data.points if self.data else await self.api.get_measurement_points()
        )
        for point in points:
            for line in point.lines:
                await self.api.get_timeseries(
                    meterpoint=point.id, line=line.id, start=start, end=end
                )
        await self.async_request_refresh()
