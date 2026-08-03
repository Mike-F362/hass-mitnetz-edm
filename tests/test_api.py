"""Tests for the HTTP API client.

Covers the two portal quirks that caused real failures:

* API GETs need ``X-XSRF-TOKEN`` and ``X-Requested-With``; without them the
  portal answers 404.
* Timeseries URLs use a non-zero-padded local timestamp with UTC offset.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from aioresponses import aioresponses

from custom_components.mitnetz_edm.api import (
    MeasurementKind,
    MitnetzApiError,
    MitnetzAuthError,
    MitnetzEdmApiClient,
    MitnetzSessionExpiredError,
    _format_portal_datetime,
)
from custom_components.mitnetz_edm.const import (
    LOGIN_CHECK_URL,
    LOGIN_PAGE_URL,
    MEASUREMENT_POINTS_URL,
)

BERLIN = ZoneInfo("Europe/Berlin")

LOGIN_HTML = """
<html><body><form action="/login_check" method="post">
  <input type="text" name="_username" />
  <input type="password" name="_password" />
  <input type="hidden" name="_csrf_token" value="a677ef921.SLRnx9uFQpgj8mP7" />
</form></body></html>
"""

SIDEBAR_JSON = [
    {
        "type": "mp",
        "id": "NMP_TEST_MP_0000001",
        "childs": [
            {"type": "line", "id": "NMP_TEST_LN_0000002", "name": "1-1:1.29.0"},
            {"type": "line", "id": "NMP_TEST_LN_0000003", "name": "1-1:2.29.0"},
        ],
    }
]


@pytest.fixture
async def session(hass):
    """Use the Home Assistant managed client session.

    Creating a bare ``aiohttp.ClientSession`` here would leave a shutdown
    thread behind that the test harness flags during cleanup.
    """
    from homeassistant.helpers.aiohttp_client import async_create_clientsession

    return async_create_clientsession(hass)


@pytest.fixture
def client(session):
    return MitnetzEdmApiClient(
        session=session, username="user", password="secret", timeout=10
    )


# ---------------------------------------------------------------- datetime ---


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        # Summer time -> +02:00, month and day not zero padded.
        (datetime(2026, 7, 6, 0, 0, tzinfo=BERLIN), "2026-7-6T00:00:00+02:00"),
        # Winter time -> +01:00.
        (datetime(2026, 1, 6, 0, 0, tzinfo=BERLIN), "2026-1-6T00:00:00+01:00"),
        # Double digit month/day stay as-is.
        (datetime(2026, 12, 24, 18, 30, 15, tzinfo=BERLIN), "2026-12-24T18:30:15+01:00"),
        # UTC input is converted to portal local time.
        (
            datetime(2026, 7, 5, 22, 0, tzinfo=timezone.utc),
            "2026-7-6T00:00:00+02:00",
        ),
    ],
)
def test_format_portal_datetime(moment, expected):
    assert _format_portal_datetime(moment) == expected


def test_format_portal_datetime_matches_working_script_sample():
    """The exact string shape taken from the known-good requests script."""
    formatted = _format_portal_datetime(datetime(2026, 7, 6, 0, 0, tzinfo=BERLIN))
    assert formatted == "2026-7-6T00:00:00+02:00"


# ------------------------------------------------------------------- login ---


async def test_login_success(client):
    with aioresponses() as mocked:
        mocked.get(LOGIN_PAGE_URL, status=200, body=LOGIN_HTML)
        mocked.post(
            LOGIN_CHECK_URL,
            status=200,
            body="<html>analytics</html>",
            headers={"Content-Type": "text/html"},
        )
        await client.login()

    assert client.is_logged_in is True
    assert client.last_login is not None
    assert client.last_login.tzinfo is not None


async def test_login_extracts_csrf_token(client):
    """The token from the login form must be posted back verbatim."""
    with aioresponses() as mocked:
        mocked.get(LOGIN_PAGE_URL, status=200, body=LOGIN_HTML)
        mocked.post(LOGIN_CHECK_URL, status=200, body="analytics")
        await client.login()

        request_key = next(
            key for key in mocked.requests if str(key[1]).endswith("/login_check")
        )
        posted = mocked.requests[request_key][0].kwargs["data"]

    assert posted["_csrf_token"] == "a677ef921.SLRnx9uFQpgj8mP7"
    assert posted["_username"] == "user"
    assert posted["_password"] == "secret"


async def test_login_without_csrf_token_raises(client):
    with aioresponses() as mocked:
        mocked.get(LOGIN_PAGE_URL, status=200, body="<html>no form here</html>")
        with pytest.raises(MitnetzAuthError, match="CSRF"):
            await client.login()

    assert client.is_logged_in is False


async def test_login_rejected_credentials(client):
    """A bounce back to /login means the credentials were refused."""
    with aioresponses() as mocked:
        mocked.get(LOGIN_PAGE_URL, status=200, body=LOGIN_HTML)
        mocked.post(LOGIN_CHECK_URL, status=200, body="<html>Anmeldung</html>")
        with pytest.raises(MitnetzAuthError):
            await client.login()

    assert client.is_logged_in is False


# ------------------------------------------------------------------ headers ---


async def test_api_get_sends_xsrf_and_xhr_headers(client, session):
    """Without these headers the portal returns 404 instead of JSON."""
    session.cookie_jar.update_cookies({"XSRF-TOKEN": "token-value-123"})

    with aioresponses() as mocked:
        mocked.get(
            MEASUREMENT_POINTS_URL,
            status=200,
            payload=SIDEBAR_JSON,
            headers={"Content-Type": "application/json"},
        )
        await client.get_measurement_points()

        request_key = next(iter(mocked.requests))
        sent_headers = mocked.requests[request_key][0].kwargs["headers"]

    assert sent_headers["X-Requested-With"] == "XMLHttpRequest"
    assert sent_headers["X-XSRF-TOKEN"] == "token-value-123"


async def test_api_get_without_xsrf_cookie_still_sends_xhr_header(client):
    with aioresponses() as mocked:
        mocked.get(
            MEASUREMENT_POINTS_URL,
            status=200,
            payload=SIDEBAR_JSON,
            headers={"Content-Type": "application/json"},
        )
        await client.get_measurement_points()

        request_key = next(iter(mocked.requests))
        sent_headers = mocked.requests[request_key][0].kwargs["headers"]

    assert sent_headers["X-Requested-With"] == "XMLHttpRequest"
    assert "X-XSRF-TOKEN" not in sent_headers


# ------------------------------------------------------- measurement points ---


async def test_get_measurement_points_parses_tree(client):
    with aioresponses() as mocked:
        mocked.get(
            MEASUREMENT_POINTS_URL,
            status=200,
            payload=SIDEBAR_JSON,
            headers={"Content-Type": "application/json"},
        )
        points = await client.get_measurement_points()

    assert len(points) == 1
    assert points[0].id == "NMP_TEST_MP_0000001"
    assert [line.name for line in points[0].lines] == ["1-1:1.29.0", "1-1:2.29.0"]


async def test_get_measurement_points_ignores_non_line_children(client):
    payload = [
        {
            "type": "mp",
            "id": "MP1",
            "childs": [
                {"type": "line", "id": "L1", "name": "1-1:1.29.0"},
                {"type": "folder", "id": "F1", "name": "ignore me"},
            ],
        },
        {"type": "other", "id": "X1", "childs": []},
    ]
    with aioresponses() as mocked:
        mocked.get(
            MEASUREMENT_POINTS_URL,
            status=200,
            payload=payload,
            headers={"Content-Type": "application/json"},
        )
        points = await client.get_measurement_points()

    assert len(points) == 1
    assert len(points[0].lines) == 1


# ------------------------------------------------------- session expiration ---


@pytest.mark.parametrize("status", [401, 403])
async def test_session_expired_on_auth_status(client, status):
    with aioresponses() as mocked:
        mocked.get(MEASUREMENT_POINTS_URL, status=status)
        with pytest.raises(MitnetzSessionExpiredError):
            await client.get_measurement_points()

    assert client.is_logged_in is False


async def test_session_expired_on_login_redirect(client):
    with aioresponses() as mocked:
        mocked.get(
            MEASUREMENT_POINTS_URL,
            status=302,
            headers={"Location": "https://edm.mitnetz-strom.de/login"},
        )
        with pytest.raises(MitnetzSessionExpiredError):
            await client.get_measurement_points()

    assert client.is_logged_in is False


async def test_session_expired_when_html_returned_instead_of_json(client):
    with aioresponses() as mocked:
        mocked.get(
            MEASUREMENT_POINTS_URL,
            status=200,
            body="<html><body>Login</body></html>",
            headers={"Content-Type": "text/html"},
        )
        with pytest.raises(MitnetzSessionExpiredError):
            await client.get_measurement_points()

    assert client.is_logged_in is False


async def test_unexpected_status_raises_api_error(client):
    with aioresponses() as mocked:
        mocked.get(MEASUREMENT_POINTS_URL, status=404)
        with pytest.raises(MitnetzApiError, match="404"):
            await client.get_measurement_points()


# ---------------------------------------------------------------- timeseries ---


async def test_get_timeseries_builds_portal_url(client):
    start = datetime(2026, 7, 6, 0, 0, tzinfo=BERLIN)
    end = datetime(2026, 7, 13, 0, 0, tzinfo=BERLIN)
    expected_url = (
        "https://edm.mitnetz-strom.de/data/mpline/genericto/"
        "NMP_TEST_MP_0000001/NMP_TEST_LN_0000002/"
        "2026-7-6T00:00:00+02:00/2026-7-13T00:00:00+02:00/base"
    )

    with aioresponses() as mocked:
        mocked.get(
            expected_url,
            status=200,
            payload={"timestamps": [], "values": []},
            headers={"Content-Type": "application/json"},
        )
        await client.get_timeseries(
            "NMP_TEST_MP_0000001", "NMP_TEST_LN_0000002", start, end
        )

    requested = [str(key[1]) for key in mocked.requests]
    assert any("2026-7-6T00:00:00+02:00" in url for url in requested)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "timestamps": ["2026-08-01T10:00:00", "2026-08-01T10:15:00"],
            "values": [0.25, 0.30],
        },
        [
            {"timestamp": "2026-08-01T10:00:00", "value": 0.25},
            {"timestamp": "2026-08-01T10:15:00", "value": 0.30},
        ],
        {
            "data": [
                {"x": "2026-08-01T10:00:00", "y": 0.25},
                {"x": "2026-08-01T10:15:00", "y": 0.30},
            ]
        },
    ],
)
async def test_timeseries_payload_shapes(client, payload):
    """The portal's exact response shape is not pinned down, so accept several."""
    with aioresponses() as mocked:
        mocked.get(
            client._build_timeseries_url("MP", "L", datetime.now(BERLIN), datetime.now(BERLIN))
            if hasattr(client, "_build_timeseries_url")
            else __import__("re").compile(r".*genericto.*"),
            status=200,
            payload=payload,
            headers={"Content-Type": "application/json"},
        )
        result = await client.get_timeseries(
            "MP", "L", datetime(2026, 8, 1, tzinfo=BERLIN), datetime(2026, 8, 2, tzinfo=BERLIN)
        )

    assert len(result.points) == 2
    assert result.points[0].value == 0.25
    assert result.points[1].value == 0.30


async def test_timeseries_timestamps_are_timezone_aware(client):
    """Naive portal timestamps must be read as local time, not UTC."""
    import re

    with aioresponses() as mocked:
        mocked.get(
            re.compile(r".*genericto.*"),
            status=200,
            payload={"timestamps": ["2026-08-01T10:00:00"], "values": [1.0]},
            headers={"Content-Type": "application/json"},
        )
        result = await client.get_timeseries(
            "MP", "L", datetime(2026, 8, 1, tzinfo=BERLIN), datetime(2026, 8, 2, tzinfo=BERLIN)
        )

    timestamp = result.points[0].timestamp
    assert timestamp.tzinfo is not None
    assert timestamp.hour == 10
    assert timestamp.utcoffset().total_seconds() == 2 * 3600


async def test_timeseries_skips_unparseable_points(client):
    import re

    with aioresponses() as mocked:
        mocked.get(
            re.compile(r".*genericto.*"),
            status=200,
            payload={
                "timestamps": ["2026-08-01T10:00:00", "nonsense", None],
                "values": [1.0, 2.0, 3.0],
            },
            headers={"Content-Type": "application/json"},
        )
        result = await client.get_timeseries(
            "MP", "L", datetime(2026, 8, 1, tzinfo=BERLIN), datetime(2026, 8, 2, tzinfo=BERLIN)
        )

    assert len(result.points) == 1


async def test_measurement_kind_detects_meter_reading(client):
    import re

    with aioresponses() as mocked:
        mocked.get(
            re.compile(r".*genericto.*"),
            status=200,
            payload={
                "timestamps": [f"2026-08-01T{hour:02d}:00:00" for hour in range(6)],
                "values": [100.0, 101.0, 102.5, 103.0, 104.2, 105.0],
            },
            headers={"Content-Type": "application/json"},
        )
        result = await client.get_timeseries(
            "MP", "L", datetime(2026, 8, 1, tzinfo=BERLIN), datetime(2026, 8, 2, tzinfo=BERLIN)
        )

    assert result.kind is MeasurementKind.METER_READING


async def test_measurement_kind_detects_interval_energy(client):
    import re

    with aioresponses() as mocked:
        mocked.get(
            re.compile(r".*genericto.*"),
            status=200,
            payload={
                "timestamps": [f"2026-08-01T{hour:02d}:00:00" for hour in range(6)],
                "values": [0.25, 0.10, 0.42, 0.05, 0.31, 0.12],
            },
            headers={"Content-Type": "application/json"},
        )
        result = await client.get_timeseries(
            "MP", "L", datetime(2026, 8, 1, tzinfo=BERLIN), datetime(2026, 8, 2, tzinfo=BERLIN)
        )

    assert result.kind is MeasurementKind.INTERVAL_ENERGY


# ------------------------------------------- measurement point tree variants ---


async def test_get_measurement_points_unwraps_root_node(client):
    """The portal nests the entries under a top-level "root" node.

    Ignoring that wrapper makes the integration discover zero measurement
    points and silently create no sensors at all.
    """
    payload = [
        {
            "type": "root",
            "id": "ROOT",
            "childs": [
                {
                    "type": "mp",
                    "id": "MP1",
                    "childs": [
                        {"type": "line", "id": "L1", "name": "1-1:1.29.0"},
                    ],
                }
            ],
        }
    ]
    with aioresponses() as mocked:
        mocked.get(
            MEASUREMENT_POINTS_URL,
            status=200,
            payload=payload,
            headers={"Content-Type": "application/json"},
        )
        points = await client.get_measurement_points()

    assert len(points) == 1
    assert points[0].id == "MP1"
    assert [line.name for line in points[0].lines] == ["1-1:1.29.0"]


async def test_line_name_is_stripped_to_bare_obis_code(client):
    """``name`` is "<obis>, <meter id>" and must not reach the classifier raw.

    ``obis.classify_line`` matches the code anchored end-to-end, so a trailing
    meter id would classify every line as UNKNOWN - no unit, no device class.
    """
    payload = [
        {
            "type": "mp",
            "id": "MP1",
            "childs": [
                {"type": "line", "id": "L1", "name": "1-1:1.29.0, 1TEST0000000000"},
            ],
        }
    ]
    with aioresponses() as mocked:
        mocked.get(
            MEASUREMENT_POINTS_URL,
            status=200,
            payload=payload,
            headers={"Content-Type": "application/json"},
        )
        points = await client.get_measurement_points()

    assert [line.name for line in points[0].lines] == ["1-1:1.29.0"]


async def test_measured_quantity_detail_wins_over_name(client):
    """``details."measured quantity"`` is unambiguous and takes precedence."""
    payload = [
        {
            "type": "mp",
            "id": "MP1",
            "childs": [
                {
                    "type": "line",
                    "id": "L1",
                    "name": "something else, 1TEST0000000000",
                    "details": {"measured quantity": "OBIS 1-1:2.6.0 (kW)"},
                },
            ],
        }
    ]
    with aioresponses() as mocked:
        mocked.get(
            MEASUREMENT_POINTS_URL,
            status=200,
            payload=payload,
            headers={"Content-Type": "application/json"},
        )
        points = await client.get_measurement_points()

    assert [line.name for line in points[0].lines] == ["1-1:2.6.0"]


async def test_extracted_obis_code_classifies_correctly():
    """End-to-end guard: extraction feeds a classifier that must resolve kW."""
    from custom_components.mitnetz_edm.obis import Quantity, classify_line

    code = MitnetzEdmApiClient._extract_obis_code(
        {"name": "1-1:2.6.0, 1TEST0000000000"}
    )
    assert classify_line(code).quantity is Quantity.POWER
    assert classify_line(code).unit == "kW"
