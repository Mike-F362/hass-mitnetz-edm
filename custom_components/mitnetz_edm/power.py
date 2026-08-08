"""Derivation of average power from interval energy.

The portal exposes load-profile lines (OBIS ``D`` = 29) as *energy per
measurement interval* in kWh. The average power during that interval is simply
that energy divided by the interval length:

    P[kW] = E[kWh] / t[h]

For the usual 15-minute interval that is a factor of four. This is not an
approximation invented here - the portal's own
``/data/timeseries/moment/0/<start>/<value>/<end>/<rkmgId>/<period>`` endpoint
performs exactly this conversion server-side. Probing it with a known input
confirms the relation:

===============  =========  ==========
Input (kWh)      Period     Output (kW)
===============  =========  ==========
1.1104           ``15mins``  4.4416
2.2208           ``15mins``  8.8832
2.0              ``15mins``  8
1.1104           ``hours``   1.1104
===============  =========  ==========

Since the division is trivial and the interval is already known from the
timestamps, the integration does the conversion locally instead of issuing an
extra HTTP request per value.

Kept free of Home Assistant imports so it can be unit-tested standalone.
"""

from __future__ import annotations

from datetime import datetime, timedelta

#: Conversion is undefined for a non-positive interval.
_MIN_INTERVAL_SECONDS = 1e-9


def energy_to_power(energy_kwh: float, interval: timedelta) -> float | None:
    """Convert energy accumulated over ``interval`` into average power in kW.

    Args:
        energy_kwh: Energy consumed or fed in during one measurement interval.
        interval: Length of that interval.

    Returns:
        Average power in kW, or ``None`` if the interval is not positive.

    """
    seconds = interval.total_seconds()
    if seconds <= _MIN_INTERVAL_SECONDS:
        return None
    return energy_kwh / (seconds / 3600.0)


def derive_power_points(
    points: list[tuple[datetime, float]],
    interval: timedelta,
) -> list[tuple[datetime, float]]:
    """Convert a series of interval-energy points into average-power points.

    Each point keeps its original timestamp: the power value is valid for the
    same interval the energy value was, so no shifting is applied.

    Args:
        points: ``(timestamp, energy_kwh)`` pairs.
        interval: The measurement interval the energy values refer to.

    Returns:
        ``(timestamp, power_kw)`` pairs in the same order, skipping any point
        that cannot be converted.

    """
    derived: list[tuple[datetime, float]] = []
    for timestamp, energy in points:
        power = energy_to_power(energy, interval)
        if power is not None:
            derived.append((timestamp, power))
    return derived
