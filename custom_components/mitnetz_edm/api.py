"""HTTP API client for the Mitnetz Strom EDM portal.

Implements the login flow and data endpoints exactly as observed via HAR capture:

1. GET  /login          -> sets PHPSESSID + XSRF-TOKEN cookies, page HTML contains a
                            hidden ``_csrf_token`` form field.
2. POST /login_check    -> form-urlencoded body with ``_username``, ``_password``,
                            ``_csrf_token``. On success responds 302 -> "/" -> 302
                            -> "/analytics" (200).
3. GET  /sidebarMultiMp/rlm            -> list of measurement points / lines.
4. GET  /data/mpline/genericto/<mp>/<line>/<from>/<to>/base -> JSON timeseries.

No browser automation (Selenium/Playwright) is used; only aiohttp.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp
from yarl import URL

from .const import (
    ANALYTICS_URL,
    LOGIN_CHECK_URL,
    LOGIN_PAGE_URL,
    MEASUREMENT_POINTS_URL,
    TIMESERIES_URL_TEMPLATE,
)

_LOGGER = logging.getLogger(__name__)

_CSRF_TOKEN_RE = re.compile(
    r'name=["\']_csrf_token["\']\s+value=["\']([^"\']+)["\']'
    r"|value=[\"']([^\"']+)[\"']\s+name=[\"']_csrf_token[\"']"
)

# The portal expects timestamps in local (Europe/Berlin) time including a
# UTC offset, e.g. "2026-7-6T00:00:00+02:00" - note month/day are NOT
# zero-padded; this matches the format the portal's own frontend sends.
_LOCAL_TZ = ZoneInfo("Europe/Berlin")


def _format_portal_datetime(dt: datetime) -> str:
    """Format a datetime the way the EDM portal's frontend does."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(_LOCAL_TZ)
    offset = local.strftime("%z")  # e.g. "+0200"
    offset = f"{offset[:3]}:{offset[3:]}"
    return (
        f"{local.year}-{local.month}-{local.day}T"
        f"{local.hour:02d}:{local.minute:02d}:{local.second:02d}{offset}"
    )


class MitnetzApiError(Exception):
    """Generic API error."""


class MitnetzAuthError(MitnetzApiError):
    """Raised when authentication fails (bad credentials)."""


class MitnetzSessionExpiredError(MitnetzApiError):
    """Raised when the session is no longer valid and must be re-established."""


class MeasurementKind(str, Enum):
    """Kind of values returned by a timeseries line."""

    INTERVAL_ENERGY = "interval_energy"
    METER_READING = "meter_reading"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class MeasurementLine:
    """A single OBIS line beneath a measurement point."""

    id: str
    name: str  # OBIS code, e.g. "1-1:1.29.0"


@dataclass(slots=True)
class MeasurementPoint:
    """A measurement point (Messpunkt) with its child lines."""

    id: str
    lines: list[MeasurementLine] = field(default_factory=list)


@dataclass(slots=True)
class TimeseriesPoint:
    """A single timestamped value."""

    timestamp: datetime
    value: float


@dataclass(slots=True)
class TimeseriesResult:
    """Result of a timeseries fetch, including inferred measurement kind."""

    points: list[TimeseriesPoint]
    kind: MeasurementKind

    @property
    def sorted_points(self) -> list[TimeseriesPoint]:
        """Points in ascending timestamp order (batches can arrive unsorted)."""
        return sorted(self.points, key=lambda p: p.timestamp)

    @property
    def latest(self) -> TimeseriesPoint | None:
        """The most recent interval in this result, if any."""
        if not self.points:
            return None
        return max(self.points, key=lambda p: p.timestamp)

    @property
    def interval(self) -> timedelta | None:
        """The measurement interval, derived from the data itself.

        Uses the *median* gap between consecutive timestamps rather than the
        mean, so a single missing interval or a DST jump does not skew the
        result. Returns ``None`` when there are too few points to tell.
        """
        ordered = self.sorted_points
        if len(ordered) < 2:
            return None

        gaps = sorted(
            (later.timestamp - earlier.timestamp).total_seconds()
            for earlier, later in zip(ordered, ordered[1:], strict=False)
        )
        gaps = [gap for gap in gaps if gap > 0]
        if not gaps:
            return None

        median = gaps[len(gaps) // 2]
        return timedelta(seconds=median)


class MitnetzEdmApiClient:
    """Thin async client around the Mitnetz Strom EDM web portal."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        timeout: int = 30,
    ) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._logged_in = False
        self.last_login: datetime | None = None
        self.last_request_duration: float | None = None

    # ------------------------------------------------------------------
    # Login handling
    # ------------------------------------------------------------------

    @property
    def is_logged_in(self) -> bool:
        """Return whether we believe we currently hold a valid session."""
        return self._logged_in

    async def login(self) -> None:
        """Perform the full login flow.

        Raises:
            MitnetzAuthError: on invalid credentials or missing CSRF token.
            MitnetzApiError: on unexpected transport/HTTP errors.
        """
        _LOGGER.debug("Starting login flow against %s", LOGIN_PAGE_URL)
        self._logged_in = False
        try:
            async with self._session.get(
                LOGIN_PAGE_URL, timeout=self._timeout, allow_redirects=True
            ) as resp:
                if resp.status != 200:
                    raise MitnetzApiError(
                        f"Unexpected status {resp.status} while fetching login page"
                    )
                html = await resp.text()
        except aiohttp.ClientError as err:
            raise MitnetzApiError(f"Network error fetching login page: {err}") from err

        csrf_token = self._extract_csrf_token(html)
        if not csrf_token:
            raise MitnetzAuthError("Could not locate CSRF token on login page")

        form = {
            "_username": self._username,
            "_password": self._password,
            "_csrf_token": csrf_token,
        }

        _LOGGER.debug("Submitting credentials to %s", LOGIN_CHECK_URL)
        try:
            async with self._session.post(
                LOGIN_CHECK_URL,
                data=form,
                timeout=self._timeout,
                allow_redirects=True,
            ) as resp:
                final_url = str(resp.url)
                body = await resp.text()
        except aiohttp.ClientError as err:
            raise MitnetzApiError(f"Network error during login POST: {err}") from err

        if "/login" in final_url and "/login_check" not in final_url:
            # Redirected back to the login page => bad credentials.
            raise MitnetzAuthError("Login rejected - check username/password")

        if "analytics" not in final_url and "analytics" not in body.lower():
            _LOGGER.debug("Login landed on unexpected URL: %s", final_url)
            raise MitnetzAuthError(
                "Login did not redirect to /analytics as expected; "
                "credentials may be invalid"
            )

        self._logged_in = True
        self.last_login = datetime.now(tz=timezone.utc)
        _LOGGER.debug("Login successful, session established")

    @staticmethod
    def _extract_csrf_token(html: str) -> str | None:
        match = _CSRF_TOKEN_RE.search(html)
        if not match:
            return None
        return match.group(1) or match.group(2)

    # ------------------------------------------------------------------
    # Data endpoints
    # ------------------------------------------------------------------

    async def get_measurement_points(self) -> list[MeasurementPoint]:
        """Fetch the list of measurement points and their OBIS lines."""
        data = await self._get_json(MEASUREMENT_POINTS_URL)

        # The endpoint wraps the actual entries in a top-level "root" node,
        # e.g. [{"type": "root", "childs": [{"type": "mp", ...}, ...]}].
        # Accounts without that wrapper return the "mp" entries directly, so
        # handle both shapes.
        entries: list[Any] = []
        for node in data:
            if node.get("type") == "root":
                entries.extend(node.get("childs", []))
            else:
                entries.append(node)

        points: list[MeasurementPoint] = []
        for entry in entries:
            if entry.get("type") != "mp":
                continue
            lines = [
                MeasurementLine(id=child["id"], name=self._extract_obis_code(child))
                for child in entry.get("childs", [])
                if child.get("type") == "line"
            ]
            points.append(MeasurementPoint(id=entry["id"], lines=lines))
        _LOGGER.debug(
            "Discovered %d measurement point(s) with %d line(s) total",
            len(points),
            sum(len(p.lines) for p in points),
        )
        return points

    @staticmethod
    def _extract_obis_code(child: dict[str, Any]) -> str:
        """Extract the bare OBIS code from a "line" entry.

        The portal's ``name`` field is "<obis>, <meter id>" and the
        ``details."measured quantity"`` field is "OBIS <obis> (<unit>)"; prefer
        the latter since it is unambiguous, falling back to splitting ``name``
        on the first comma. Returning the raw ``name`` here would break
        :func:`.obis.classify_line`, which expects a bare code and would
        otherwise classify every line as ``UNKNOWN``.
        """
        measured_quantity = child.get("details", {}).get("measured quantity", "")
        match = re.search(r"OBIS\s+(\S+)", measured_quantity)
        if match:
            return match.group(1)
        return str(child.get("name", "")).split(",", 1)[0].strip()

    async def get_timeseries(
        self,
        meterpoint: str,
        line: str,
        start: datetime,
        end: datetime,
    ) -> TimeseriesResult:
        """Fetch a timeseries for a given meterpoint/line between start and end."""
        url = TIMESERIES_URL_TEMPLATE.format(
            meterpoint=meterpoint,
            line=line,
            start=_format_portal_datetime(start),
            end=_format_portal_datetime(end),
        )
        data = await self._get_json(url)
        points = self._parse_timeseries_payload(data)
        kind = self._infer_measurement_kind(points)
        _LOGGER.debug(
            "Fetched %d datapoint(s) for %s/%s (kind=%s)",
            len(points),
            meterpoint,
            line,
            kind,
        )
        return TimeseriesResult(points=points, kind=kind)

    @staticmethod
    def _parse_timeseries_payload(data: Any) -> list[TimeseriesPoint]:
        """Normalise the various shapes the API may return into a flat point list.

        Points are always anchored to the *start* of their measurement interval.
        That matters because the portal is not: its ``timestamps`` array holds
        the interval **end**, and it ships a parallel ``timestampsLeft`` array
        with the start. Its own report confirms the convention - a value of
        0.803275 kWh covering 14:00-14:15 is listed with "Maximum time 14:15".

        Reading ``timestamps`` and treating it as the start pushes every value
        one interval into the future: the sensor attributes are wrong, and an
        interval falling on an hour boundary is aggregated into the following
        hour of long-term statistics.
        """
        points: list[TimeseriesPoint] = []

        # Expected common shapes:
        # 1) {"timestamps": [...], "values": [...]}  (plus "timestampsLeft")
        # 2) [{"timestamp": ..., "value": ...}, ...]
        # 3) {"data": [{"x": ..., "y": ...}, ...]}
        if isinstance(data, dict) and "timestamps" in data and "values" in data:
            # Prefer the explicit start edge when the portal supplies it.
            starts = data.get("timestampsLeft")
            ends = data["timestamps"]
            use_starts = isinstance(starts, list) and len(starts) == len(ends)

            for ts, value in zip(starts if use_starts else ends, data["values"], strict=False):
                parsed = MitnetzEdmApiClient._parse_point(ts, value)
                if parsed is not None:
                    points.append(parsed)

            if not use_starts:
                # Older or reduced payloads omit timestampsLeft. The timestamps
                # are still interval ends, so shift them back by the interval
                # length, which can only be measured once all points are parsed.
                points = MitnetzEdmApiClient._shift_ends_to_starts(points)
        elif isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
            for row in data["data"]:
                ts = row.get("timestamp") or row.get("x") or row.get("t")
                value = row.get("value") or row.get("y") or row.get("v")
                parsed = MitnetzEdmApiClient._parse_point(ts, value)
                if parsed is not None:
                    points.append(parsed)
        elif isinstance(data, list):
            for row in data:
                if isinstance(row, dict):
                    ts = row.get("timestamp") or row.get("x") or row.get("t")
                    value = row.get("value") or row.get("y") or row.get("v")
                elif isinstance(row, (list, tuple)) and len(row) == 2:
                    ts, value = row
                else:
                    continue
                parsed = MitnetzEdmApiClient._parse_point(ts, value)
                if parsed is not None:
                    points.append(parsed)
        else:
            _LOGGER.warning("Unrecognised timeseries payload shape: %s", type(data))

        return points

    @staticmethod
    def _shift_ends_to_starts(
        points: list[TimeseriesPoint],
    ) -> list[TimeseriesPoint]:
        """Move interval-end timestamps back onto the interval start.

        Uses the median gap so a single missing interval or a DST jump does not
        distort the shift. With fewer than two points the interval is unknown
        and the timestamps are left untouched: guessing 15 minutes would be a
        silent, invisible error, whereas leaving them alone is at worst the
        behaviour we already had.
        """
        if len(points) < 2:
            return points

        ordered = sorted(points, key=lambda p: p.timestamp)
        gaps = sorted(
            (later.timestamp - earlier.timestamp).total_seconds()
            for earlier, later in zip(ordered, ordered[1:], strict=False)
        )
        gaps = [gap for gap in gaps if gap > 0]
        if not gaps:
            return points

        interval = timedelta(seconds=gaps[len(gaps) // 2])
        return [
            TimeseriesPoint(timestamp=p.timestamp - interval, value=p.value)
            for p in points
        ]

    @staticmethod
    def _parse_point(ts: Any, value: Any) -> TimeseriesPoint | None:
        if ts is None or value is None:
            return None
        try:
            if isinstance(ts, (int, float)):
                # Assume epoch seconds or milliseconds.
                epoch = float(ts) / 1000.0 if ts > 10_000_000_000 else float(ts)
                timestamp = datetime.fromtimestamp(epoch, tz=timezone.utc)
            else:
                timestamp = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))

            if timestamp.tzinfo is None:
                # The portal reports local wall-clock time when it omits an
                # offset; interpreting it as UTC would shift every value by
                # one or two hours.
                timestamp = timestamp.replace(tzinfo=_LOCAL_TZ)

            return TimeseriesPoint(timestamp=timestamp, value=float(value))
        except (ValueError, TypeError, OverflowError) as err:
            _LOGGER.debug("Skipping unparseable datapoint (%s, %s): %s", ts, value, err)
            return None

    @staticmethod
    def _infer_measurement_kind(points: list[TimeseriesPoint]) -> MeasurementKind:
        """Guess whether values are cumulative meter readings or interval energy.

        Heuristic: meter readings are monotonically non-decreasing across almost
        all consecutive samples and grow large over time; interval energy values
        fluctuate up and down between consecutive samples.
        """
        if len(points) < 3:
            return MeasurementKind.UNKNOWN

        ordered = sorted(points, key=lambda p: p.timestamp)
        increases = 0
        decreases = 0
        for prev, cur in zip(ordered, ordered[1:], strict=False):
            if cur.value > prev.value:
                increases += 1
            elif cur.value < prev.value:
                decreases += 1

        total = increases + decreases
        if total == 0:
            return MeasurementKind.UNKNOWN

        if decreases / total < 0.05:
            return MeasurementKind.METER_READING
        return MeasurementKind.INTERVAL_ENERGY

    # ------------------------------------------------------------------
    # Low level helpers
    # ------------------------------------------------------------------

    def _xhr_headers(self, url: str) -> dict[str, str]:
        """Build the headers the portal's own frontend sends on API calls.

        The Symfony backend appears to route/validate these endpoints as
        AJAX-only: without ``X-Requested-With`` and the double-submitted
        ``X-XSRF-TOKEN`` header (mirroring the ``XSRF-TOKEN`` cookie set at
        login), it responds with a plain 404 instead of JSON.
        """
        headers = {"X-Requested-With": "XMLHttpRequest"}
        cookies = self._session.cookie_jar.filter_cookies(URL(url))
        xsrf_cookie = cookies.get("XSRF-TOKEN")
        if xsrf_cookie is not None:
            headers["X-XSRF-TOKEN"] = xsrf_cookie.value
        else:
            _LOGGER.debug("No XSRF-TOKEN cookie available for %s", url)
        return headers

    async def _get_json(self, url: str) -> Any:
        """GET a URL, validating the response is authenticated JSON."""
        import time

        start_time = time.monotonic()
        headers = self._xhr_headers(url)
        try:
            async with self._session.get(
                url, headers=headers, timeout=self._timeout, allow_redirects=False
            ) as resp:
                self.last_request_duration = time.monotonic() - start_time

                if resp.status in (401, 403):
                    self._logged_in = False
                    raise MitnetzSessionExpiredError(
                        f"Session expired (HTTP {resp.status}) fetching {url}"
                    )

                if resp.status in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location", "")
                    if "login" in location:
                        self._logged_in = False
                        raise MitnetzSessionExpiredError(
                            f"Redirected to login fetching {url}"
                        )
                    raise MitnetzApiError(
                        f"Unexpected redirect to {location} fetching {url}"
                    )

                if resp.status != 200:
                    raise MitnetzApiError(
                        f"Unexpected status {resp.status} fetching {url}"
                    )

                content_type = resp.headers.get("Content-Type", "")
                text = await resp.text()

                if "json" not in content_type.lower():
                    # Some deployments omit correct content-type; try to parse
                    # anyway, but treat a login-page-like HTML body as expired.
                    stripped = text.lstrip()
                    if stripped.startswith("<"):
                        self._logged_in = False
                        raise MitnetzSessionExpiredError(
                            f"Received HTML instead of JSON fetching {url} "
                            "(session likely expired)"
                        )

                import json

                try:
                    return json.loads(text)
                except json.JSONDecodeError as err:
                    raise MitnetzApiError(
                        f"Could not decode JSON from {url}: {err}"
                    ) from err
        except aiohttp.ClientError as err:
            raise MitnetzApiError(f"Network error fetching {url}: {err}") from err
