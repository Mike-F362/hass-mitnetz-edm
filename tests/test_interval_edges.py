"""Points must be anchored to the start of their measurement interval.

The portal labels an interval by its **end**. Its own exported report makes the
convention explicit: the value 0.803275 kWh covering 14:00-14:15 is reported as
"local maximum ... Maximum time 06.08.2026, 14:15".

The JSON ships both edges - ``timestampsLeft`` (start) and ``timestamps``
(end) - and in a real captured response the two differ by exactly one interval
for every single point. Reading ``timestamps`` and treating it as the start
shifts every value one interval into the future, which is what made the Home
Assistant curve trail the portal's chart.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.mitnetz_edm.api import MitnetzEdmApiClient

UTC = timezone.utc
QUARTER = timedelta(minutes=15)


def _parse(payload):
    return MitnetzEdmApiClient._parse_timeseries_payload(payload)


# Shape and figures taken from a captured /base response for line 1-1:2.29.0,
# cross-checked against the portal's own CSV export of the same period.
REAL_PAYLOAD = {
    "timestampsLeft": [
        "2026-08-06T12:00:00+00:00",  # 14:00 local
        "2026-08-06T12:15:00+00:00",
        "2026-08-06T12:30:00+00:00",
        "2026-08-06T12:45:00+00:00",
    ],
    "timestamps": [
        "2026-08-06T12:15:00+00:00",  # 14:15 local - the interval END
        "2026-08-06T12:30:00+00:00",
        "2026-08-06T12:45:00+00:00",
        "2026-08-06T13:00:00+00:00",
    ],
    "values": [0.803275, 0.777550, 0.662400, 0.672650],
}


def test_uses_the_start_edge_when_available():
    points = _parse(REAL_PAYLOAD)

    assert [p.timestamp for p in points] == [
        datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
        datetime(2026, 8, 6, 12, 15, tzinfo=UTC),
        datetime(2026, 8, 6, 12, 30, tzinfo=UTC),
        datetime(2026, 8, 6, 12, 45, tzinfo=UTC),
    ]


def test_peak_lands_on_the_interval_the_report_names():
    """0.803275 kWh belongs to 14:00-14:15 local, not to 14:15-14:30."""
    points = _parse(REAL_PAYLOAD)
    peak = max(points, key=lambda p: p.value)

    assert peak.value == pytest.approx(0.803275)
    assert peak.timestamp == datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def test_falls_back_to_shifting_ends_when_left_edge_missing():
    """Payloads without timestampsLeft still must not be one interval late."""
    payload = {
        "timestamps": REAL_PAYLOAD["timestamps"],
        "values": REAL_PAYLOAD["values"],
    }

    points = _parse(payload)

    assert [p.timestamp for p in points] == [p.timestamp for p in _parse(REAL_PAYLOAD)]


def test_mismatched_left_edge_length_is_ignored():
    """A truncated timestampsLeft must not silently mispair values."""
    payload = {
        "timestampsLeft": REAL_PAYLOAD["timestampsLeft"][:2],
        "timestamps": REAL_PAYLOAD["timestamps"],
        "values": REAL_PAYLOAD["values"],
    }

    points = _parse(payload)

    # Falls back to shifting the end timestamps, so the result is still correct.
    assert [p.timestamp for p in points] == [p.timestamp for p in _parse(REAL_PAYLOAD)]


def test_single_point_without_left_edge_is_left_alone():
    """With one point the interval is unknown; guessing would be a silent error."""
    payload = {
        "timestamps": ["2026-08-06T12:15:00+00:00"],
        "values": [0.803275],
    }

    points = _parse(payload)

    assert len(points) == 1
    assert points[0].timestamp == datetime(2026, 8, 6, 12, 15, tzinfo=UTC)


def test_shift_uses_median_so_a_gap_does_not_distort_it():
    """A missing interval must not stretch the shift for every other point."""
    payload = {
        "timestamps": [
            "2026-08-06T12:15:00+00:00",
            "2026-08-06T12:30:00+00:00",
            # 12:45 missing
            "2026-08-06T13:00:00+00:00",
            "2026-08-06T13:15:00+00:00",
        ],
        "values": [1.0, 2.0, 3.0, 4.0],
    }

    points = _parse(payload)

    assert points[0].timestamp == datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    assert points[-1].timestamp == datetime(2026, 8, 6, 13, 0, tzinfo=UTC)


def test_hour_boundary_value_stays_in_its_own_hour():
    """The regression that corrupted long-term statistics.

    An interval running 14:45-15:00 is labelled 15:00 by the portal. Bucketed
    on that label it lands in the 15:00 hour, although it belongs to 14:00.
    """
    payload = {
        "timestampsLeft": [
            "2026-08-06T12:30:00+00:00",
            "2026-08-06T12:45:00+00:00",  # 14:45 local
        ],
        "timestamps": [
            "2026-08-06T12:45:00+00:00",
            "2026-08-06T13:00:00+00:00",  # 15:00 local
        ],
        "values": [0.6624, 0.67265],
    }

    last = _parse(payload)[-1]

    assert last.timestamp.astimezone(UTC).hour == 12  # 14:xx local, not 15:00
