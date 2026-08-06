"""Tests for the HTTP API client.

Covers the portal quirks that caused real failures:

* API GETs need ``X-XSRF-TOKEN`` and ``X-Requested-With``; without them the
  portal answers 404.
* Timeseries URLs use a non-zero-padded local timestamp with UTC offset.
* The measurement point tree is wrapped in a ``root`` node and lines are named
  "<obis>, <meter id>" rather than by the bare OBIS code.

HTTP is faked with Home Assistant's own ``aioclient_mock`` rather than
``aioresponses``. The latter reaches into ``aiohttp.ClientResponse`` internals
and broke outright when aiohttp made ``stream_writer`` a required argument;
``aioclient_mock`` ships with pytest-homeassistant-custom-component and is
updated in lockstep with the aiohttp version Home Assistant pins.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

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
    TIMESERIES_URL_TEMPLATE,
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

DAY_START = datetime(2026, 8, 1, tzinfo=BERLIN)
DAY_END = datetime(2026, 8, 2, tzinfo=BERLIN)


def timeseries_url(
    meterpoint: str = "MP",
    line: str = "L",
    start: datetime = DAY_START,
    end: datetime = DAY_END,
) -> str:
    """Build the URL the client is expected to request.

    aioclient_mock matches on the exact URL, so the tests have to construct it
    the same way the client does. That is a feature rather than a chore: every
    test now also asserts the portal's unusual timestamp format is produced.
    """
    return TIMESERIES_URL_TEMPLATE.format(
        meterpoint=meterpoint,
        line=line,
        start=_format_portal_datetime(start),
        end=_format_portal_datetime(end),
    )


@pytest.fixture
async def session(hass, aioclient_mock):
    """The Home Assistant managed client session, with HTTP faked.

    ``aioclient_mock`` patches the session factory, so it has to be requested
    *before* the session is built - hence the explicit dependency here.
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


async def test_login_success(client, aioclient_mock):
    aioclient_mock.get(LOGIN_PAGE_URL, text=LOGIN_HTML)
    aioclient_mock.post(LOGIN_CHECK_URL, text="<html>analytics</html>")

    await client.login()

    assert client.is_logged_in is True
    assert client.last_login is not None
    assert client.last_login.tzinfo is not None


async def test_login_extracts_csrf_token(client, aioclient_mock):
    """The token from the login form must be posted back verbatim."""
    aioclient_mock.get(LOGIN_PAGE_URL, text=LOGIN_HTML)
    aioclient_mock.post(LOGIN_CHECK_URL, text="analytics")

    await client.login()

    # mock_calls entries are (method, url, data, headers).
    posted = aioclient_mock.mock_calls[-1][2]
    assert posted["_csrf_token"] == "a677ef921.SLRnx9uFQpgj8mP7"
    assert posted["_username"] == "user"
    assert posted["_password"] == "secret"


async def test_login_without_csrf_token_raises(client, aioclient_mock):
    aioclient_mock.get(LOGIN_PAGE_URL, text="<html>no form here</html>")

    with pytest.raises(MitnetzAuthError, match="CSRF"):
        await client.login()

    assert client.is_logged_in is False


async def test_login_rejected_credentials(client, aioclient_mock):
    """A bounce back to /login means the credentials were refused."""
    aioclient_mock.get(LOGIN_PAGE_URL, text=LOGIN_HTML)
    aioclient_mock.post(LOGIN_CHECK_URL, text="<html>Anmeldung</html>")

    with pytest.raises(MitnetzAuthError):
        await client.login()

    assert client.is_logged_in is False


# ------------------------------------------------------------------ headers ---


async def test_api_get_sends_xsrf_and_xhr_headers(client, session, aioclient_mock):
    """Without these headers the portal returns 404 instead of JSON."""
    session.cookie_jar.update_cookies({"XSRF-TOKEN": "token-value-123"})
    aioclient_mock.get(MEASUREMENT_POINTS_URL, json=SIDEBAR_JSON)

    await client.get_measurement_points()

    sent_headers = aioclient_mock.mock_calls[-1][3]
    assert sent_headers["X-Requested-With"] == "XMLHttpRequest"
    assert sent_headers["X-XSRF-TOKEN"] == "token-value-123"


async def test_api_get_without_xsrf_cookie_still_sends_xhr_header(
    client, aioclient_mock
):
    aioclient_mock.get(MEASUREMENT_POINTS_URL, json=SIDEBAR_JSON)

    await client.get_measurement_points()

    sent_headers = aioclient_mock.mock_calls[-1][3]
    assert sent_headers["X-Requested-With"] == "XMLHttpRequest"
    assert "X-XSRF-TOKEN" not in sent_headers


# ------------------------------------------------------- measurement points ---


async def test_get_measurement_points_parses_tree(client, aioclient_mock):
    aioclient_mock.get(MEASUREMENT_POINTS_URL, json=SIDEBAR_JSON)

    points = await client.get_measurement_points()

    assert len(points) == 1
    assert points[0].id == "NMP_TEST_MP_0000001"
    assert [line.name for line in points[0].lines] == ["1-1:1.29.0", "1-1:2.29.0"]


async def test_get_measurement_points_ignores_non_line_children(
    client, aioclient_mock
):
    aioclient_mock.get(
        MEASUREMENT_POINTS_URL,
        json=[
            {
                "type": "mp",
                "id": "MP1",
                "childs": [
                    {"type": "line", "id": "L1", "name": "1-1:1.29.0"},
                    {"type": "folder", "id": "F1", "name": "ignore me"},
                ],
            },
            {"type": "other", "id": "X1", "childs": []},
        ],
    )

    points = await client.get_measurement_points()

    assert len(points) == 1
    assert len(points[0].lines) == 1


# ------------------------------------------------------- session expiration ---


@pytest.mark.parametrize("status", [401, 403])
async def test_session_expired_on_auth_status(client, aioclient_mock, status):
    aioclient_mock.get(MEASUREMENT_POINTS_URL, status=status)

    with pytest.raises(MitnetzSessionExpiredError):
        await client.get_measurement_points()

    assert client.is_logged_in is False


async def test_session_expired_on_login_redirect(client, aioclient_mock):
    aioclient_mock.get(
        MEASUREMENT_POINTS_URL,
        status=302,
        headers={"Location": "https://edm.mitnetz-strom.de/login"},
    )

    with pytest.raises(MitnetzSessionExpiredError):
        await client.get_measurement_points()

    assert client.is_logged_in is False


async def test_session_expired_when_html_returned_instead_of_json(
    client, aioclient_mock
):
    aioclient_mock.get(
        MEASUREMENT_POINTS_URL,
        text="<html><body>Login</body></html>",
        headers={"Content-Type": "text/html"},
    )

    with pytest.raises(MitnetzSessionExpiredError):
        await client.get_measurement_points()

    assert client.is_logged_in is False


async def test_unexpected_status_raises_api_error(client, aioclient_mock):
    aioclient_mock.get(MEASUREMENT_POINTS_URL, status=404)

    with pytest.raises(MitnetzApiError, match="404"):
        await client.get_measurement_points()


# ---------------------------------------------------------------- timeseries ---


async def test_get_timeseries_builds_portal_url(client, aioclient_mock):
    start = datetime(2026, 7, 6, 0, 0, tzinfo=BERLIN)
    end = datetime(2026, 7, 13, 0, 0, tzinfo=BERLIN)
    expected_url = (
        "https://edm.mitnetz-strom.de/data/mpline/genericto/"
        "NMP_TEST_MP_0000001/NMP_TEST_LN_0000002/"
        "2026-7-6T00:00:00+02:00/2026-7-13T00:00:00+02:00/base"
    )
    assert timeseries_url("NMP_TEST_MP_0000001", "NMP_TEST_LN_0000002", start, end) == (
        expected_url
    )

    aioclient_mock.get(expected_url, json={"timestamps": [], "values": []})

    await client.get_timeseries("NMP_TEST_MP_0000001", "NMP_TEST_LN_0000002", start, end)

    assert str(aioclient_mock.mock_calls[-1][1]) == expected_url


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
async def test_timeseries_payload_shapes(client, aioclient_mock, payload):
    """The portal's exact response shape is not pinned down, so accept several."""
    aioclient_mock.get(timeseries_url(), json=payload)

    result = await client.get_timeseries("MP", "L", DAY_START, DAY_END)

    assert len(result.points) == 2
    assert result.points[0].value == 0.25
    assert result.points[1].value == 0.30


async def test_timeseries_timestamps_are_timezone_aware(client, aioclient_mock):
    """Naive portal timestamps must be read as local time, not UTC."""
    aioclient_mock.get(
        timeseries_url(),
        json={"timestamps": ["2026-08-01T10:00:00"], "values": [1.0]},
    )

    result = await client.get_timeseries("MP", "L", DAY_START, DAY_END)

    timestamp = result.points[0].timestamp
    assert timestamp.tzinfo is not None
    assert timestamp.hour == 10
    assert timestamp.utcoffset().total_seconds() == 2 * 3600


async def test_timeseries_skips_unparseable_points(client, aioclient_mock):
    aioclient_mock.get(
        timeseries_url(),
        json={
            "timestamps": ["2026-08-01T10:00:00", "nonsense", None],
            "values": [1.0, 2.0, 3.0],
        },
    )

    result = await client.get_timeseries("MP", "L", DAY_START, DAY_END)

    assert len(result.points) == 1


async def test_measurement_kind_detects_meter_reading(client, aioclient_mock):
    aioclient_mock.get(
        timeseries_url(),
        json={
            "timestamps": [f"2026-08-01T{hour:02d}:00:00" for hour in range(6)],
            "values": [100.0, 101.0, 102.5, 103.0, 104.2, 105.0],
        },
    )

    result = await client.get_timeseries("MP", "L", DAY_START, DAY_END)

    assert result.kind is MeasurementKind.METER_READING


async def test_measurement_kind_detects_interval_energy(client, aioclient_mock):
    aioclient_mock.get(
        timeseries_url(),
        json={
            "timestamps": [f"2026-08-01T{hour:02d}:00:00" for hour in range(6)],
            "values": [0.25, 0.10, 0.42, 0.05, 0.31, 0.12],
        },
    )

    result = await client.get_timeseries("MP", "L", DAY_START, DAY_END)

    assert result.kind is MeasurementKind.INTERVAL_ENERGY


async def test_timeseries_uses_the_interval_start_edge(client, aioclient_mock):
    """The portal's "timestamps" are interval ends; points must carry the start.

    See tests/test_interval_edges.py for the full reasoning and the check
    against a captured response.
    """
    aioclient_mock.get(
        timeseries_url(),
        json={
            "timestampsLeft": [
                "2026-08-06T12:00:00+00:00",
                "2026-08-06T12:15:00+00:00",
            ],
            "timestamps": [
                "2026-08-06T12:15:00+00:00",
                "2026-08-06T12:30:00+00:00",
            ],
            "values": [0.803275, 0.777550],
        },
    )

    result = await client.get_timeseries("MP", "L", DAY_START, DAY_END)

    peak = max(result.points, key=lambda p: p.value)
    assert peak.timestamp == datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)


# ------------------------------------------- measurement point tree variants ---


async def test_get_measurement_points_unwraps_root_node(client, aioclient_mock):
    """The portal nests the entries under a top-level "root" node.

    Ignoring that wrapper makes the integration discover zero measurement
    points and silently create no sensors at all.
    """
    aioclient_mock.get(
        MEASUREMENT_POINTS_URL,
        json=[
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
        ],
    )

    points = await client.get_measurement_points()

    assert len(points) == 1
    assert points[0].id == "MP1"
    assert [line.name for line in points[0].lines] == ["1-1:1.29.0"]


async def test_line_name_is_stripped_to_bare_obis_code(client, aioclient_mock):
    """``name`` is "<obis>, <meter id>" and must not reach the classifier raw.

    ``obis.classify_line`` matches the code anchored end-to-end, so a trailing
    meter id would classify every line as UNKNOWN - no unit, no device class.
    """
    aioclient_mock.get(
        MEASUREMENT_POINTS_URL,
        json=[
            {
                "type": "mp",
                "id": "MP1",
                "childs": [
                    {
                        "type": "line",
                        "id": "L1",
                        "name": "1-1:1.29.0, DE0001234567890123456789012345678",
                    },
                ],
            }
        ],
    )

    points = await client.get_measurement_points()

    assert [line.name for line in points[0].lines] == ["1-1:1.29.0"]


async def test_measured_quantity_detail_wins_over_name(client, aioclient_mock):
    """``details."measured quantity"`` is unambiguous and takes precedence."""
    aioclient_mock.get(
        MEASUREMENT_POINTS_URL,
        json=[
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
        ],
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
