"""Optional tests against the real EDM portal.

Skipped unless ``tests/live_config.json`` exists, so the default suite stays
offline, fast and reproducible. Copy ``live_config.example.json``, fill it in
and run:

    pytest -m live

The config file is git-ignored on purpose. Credentials aside, the meter point
designation combined with a load profile is personal data and must not end up
in the repository.

These tests assert *structure*, never values: they are a canary for the portal
changing its response shape, which is what broke the integration before.
"""

from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path

from homeassistant.util import dt as dt_util
import pytest

from custom_components.mitnetz_edm.api import MitnetzEdmApiClient
from custom_components.mitnetz_edm.obis import Quantity, classify_line

LIVE_CONFIG = Path(__file__).parent / "live_config.json"

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not LIVE_CONFIG.exists(),
        reason="tests/live_config.json missing - see live_config.example.json",
    ),
]


@pytest.fixture(scope="module")
def live_config() -> dict:
    return json.loads(LIVE_CONFIG.read_text(encoding="utf-8"))


@pytest.fixture
async def live_client(hass, live_config):
    from homeassistant.helpers.aiohttp_client import async_create_clientsession

    session = async_create_clientsession(
        hass, headers={"Accept-Language": "en-US,en;q=0.9,de-DE;q=0.8,de;q=0.7"}
    )
    client = MitnetzEdmApiClient(
        session=session,
        username=live_config["username"],
        password=live_config["password"],
        timeout=30,
    )
    await client.login()
    return client


async def test_login_succeeds(live_client):
    assert live_client.is_logged_in is True


async def test_measurement_points_are_discoverable(live_client):
    """Guards the root-node unwrapping and the OBIS extraction."""
    points = await live_client.get_measurement_points()

    assert points, "no measurement points - is the root node still unwrapped?"
    for point in points:
        for line in point.lines:
            classification = classify_line(line.name)
            assert classification.quantity is not Quantity.UNKNOWN, (
                f"line {line.name!r} was not classified - the portal may have "
                "changed how it names lines"
            )


async def test_timeseries_carries_both_interval_edges(live_client, live_config):
    """The interval-start fix depends on timestampsLeft still being sent."""
    end = dt_util.utcnow()
    result = await live_client.get_timeseries(
        meterpoint=live_config["meterpoint_id"],
        line=live_config["line_id"],
        start=end - timedelta(hours=6),
        end=end,
    )

    assert result.points, "no datapoints returned for the last six hours"
    assert result.interval is not None
    assert result.interval <= timedelta(hours=1)

    # Points must be anchored to the interval start, so the newest one cannot
    # claim to cover a period that has not finished yet.
    newest = result.latest
    assert newest.timestamp + result.interval <= end + timedelta(minutes=1)
