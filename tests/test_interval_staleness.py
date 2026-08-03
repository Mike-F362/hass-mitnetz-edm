"""Tests for measurement-interval detection and stale-value handling.

These cover the behaviour that stops a per-interval min/max line from being
drawn as a flat multi-hour plateau: the interval is derived from the data
itself, and a value that has aged past the configured number of intervals is
reported as ``unknown`` rather than carried forward.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.mitnetz_edm.api import (
    MeasurementKind,
    TimeseriesPoint,
    TimeseriesResult,
)
from custom_components.mitnetz_edm.coordinator import LineData

UTC = timezone.utc


def _result(timestamps: list[datetime], values: list[float] | None = None):
    values = values or [float(i) for i in range(len(timestamps))]
    return TimeseriesResult(
        points=[
            TimeseriesPoint(timestamp=ts, value=val)
            for ts, val in zip(timestamps, values, strict=True)
        ],
        kind=MeasurementKind.INTERVAL_ENERGY,
    )


def _series(start: datetime, step_minutes: int, count: int) -> list[datetime]:
    return [start + timedelta(minutes=step_minutes * i) for i in range(count)]


class TestIntervalDetection:
    def test_detects_quarter_hour_interval(self):
        result = _result(_series(datetime(2026, 8, 1, tzinfo=UTC), 15, 10))
        assert result.interval == timedelta(minutes=15)

    def test_detects_hourly_interval(self):
        result = _result(_series(datetime(2026, 8, 1, tzinfo=UTC), 60, 6))
        assert result.interval == timedelta(hours=1)

    def test_single_gap_does_not_skew_result(self):
        """A missing interval must not change the detected spacing."""
        stamps = _series(datetime(2026, 8, 1, tzinfo=UTC), 15, 10)
        del stamps[4]  # drop one interval -> one 30-minute gap
        result = _result(stamps)
        assert result.interval == timedelta(minutes=15)

    def test_unsorted_input_is_handled(self):
        stamps = _series(datetime(2026, 8, 1, tzinfo=UTC), 15, 8)
        shuffled = [stamps[3], stamps[0], stamps[7], stamps[1], stamps[5]]
        result = _result(shuffled)
        # Sorting happens before the gaps are measured, so all gaps are
        # multiples of 15 minutes and the median stays sane.
        assert result.interval is not None
        assert result.interval.total_seconds() % 900 == 0

    def test_too_few_points_returns_none(self):
        assert _result([datetime(2026, 8, 1, tzinfo=UTC)]).interval is None
        assert _result([]).interval is None

    def test_latest_picks_newest_not_largest(self):
        """The reported value follows the newest timestamp, not the peak."""
        stamps = _series(datetime(2026, 8, 1, tzinfo=UTC), 15, 4)
        result = _result(stamps, values=[9.9, 1.0, 2.0, 0.5])
        assert result.latest is not None
        assert result.latest.value == 0.5


class TestStaleness:
    @staticmethod
    def _line(last_ts: datetime, interval: timedelta | None = timedelta(minutes=15)):
        return LineData(
            point_id="MP",
            line_id="LINE",
            obis_code="1-1:2.6.0",
            result=_result([last_ts], values=[4.68]),
            last_value=4.68,
            last_timestamp=last_ts,
            interval=interval,
        )

    def test_fresh_value_is_not_stale(self):
        now = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
        line = self._line(now - timedelta(minutes=15))
        assert line.is_stale(now, max_intervals=3) is False

    def test_value_aged_past_limit_is_stale(self):
        now = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
        # Interval ended 3h ago = 12 intervals, well past a limit of 3.
        line = self._line(now - timedelta(hours=3))
        assert line.is_stale(now, max_intervals=3) is True

    def test_limit_is_measured_from_interval_end(self):
        now = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
        # Interval starts 60 min ago, so it *ended* 45 min ago = 3 intervals.
        line = self._line(now - timedelta(minutes=60))
        assert line.is_stale(now, max_intervals=3) is False
        assert line.is_stale(now, max_intervals=2) is True

    def test_missing_timestamp_counts_as_stale(self):
        now = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
        line = LineData(
            point_id="MP",
            line_id="LINE",
            obis_code="1-1:2.6.0",
            result=_result([]),
            last_value=None,
            last_timestamp=None,
        )
        assert line.is_stale(now, max_intervals=3) is True

    def test_falls_back_to_default_interval_when_unknown(self):
        """With one data point the interval is unknown; 15 min is assumed."""
        now = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
        line = self._line(now - timedelta(minutes=20), interval=None)
        assert line.is_stale(now, max_intervals=3) is False
        line_old = self._line(now - timedelta(minutes=90), interval=None)
        assert line_old.is_stale(now, max_intervals=3) is True

    def test_interval_end_derived_from_start_plus_interval(self):
        start = datetime(2026, 8, 1, 11, 45, tzinfo=UTC)
        line = self._line(start)
        assert line.interval_end == datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

    @pytest.mark.parametrize("limit", [1, 3, 12, 96])
    def test_larger_limits_tolerate_older_data(self, limit):
        now = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
        line = self._line(now - timedelta(minutes=15 * limit))
        # Interval ends 15 min later than it starts, so this sits just inside.
        assert line.is_stale(now, max_intervals=limit) is False


class TestStalePlateauRegression:
    """The reported symptom: a max-demand line held its last peak for hours.

    A poll returning nothing newer must not keep republishing the old peak,
    otherwise the history graph shows a flat plateau at (for example) 4.68 kW
    right through the night.
    """

    def test_old_peak_is_not_carried_forward(self):
        peak_time = datetime(2026, 8, 1, 14, 0, tzinfo=UTC)
        line = LineData(
            point_id="MP",
            line_id="LINE",
            obis_code="1-1:2.6.0",
            result=_result([peak_time], values=[4.68]),
            last_value=4.68,
            last_timestamp=peak_time,
            interval=timedelta(minutes=15),
        )

        # Same data re-read at 22:00 and again at 02:00 the next night.
        for now in (
            datetime(2026, 8, 1, 22, 0, tzinfo=UTC),
            datetime(2026, 8, 2, 2, 0, tzinfo=UTC),
        ):
            assert line.is_stale(now, max_intervals=3) is True

    def test_value_survives_a_single_missed_poll(self):
        """Tolerance must not be so tight that one late poll blanks the sensor."""
        interval_start = datetime(2026, 8, 1, 14, 0, tzinfo=UTC)
        line = LineData(
            point_id="MP",
            line_id="LINE",
            obis_code="1-1:2.6.0",
            result=_result([interval_start], values=[4.68]),
            last_value=4.68,
            last_timestamp=interval_start,
            interval=timedelta(minutes=15),
        )
        just_after = datetime(2026, 8, 1, 14, 30, tzinfo=UTC)
        assert line.is_stale(just_after, max_intervals=3) is False
