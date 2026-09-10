"""Sun times for the recap and the hourly charts, from Home Assistant's location.

The dawn chorus is a sunrise phenomenon, not a clock one: it drifts by more than two
hours across the year at temperate latitudes, so "05:00–07:30" would be wrong for most
of it. Home Assistant already knows where the house is — ``GET /api/config`` returns
``latitude``, ``longitude``, ``elevation`` and ``time_zone`` — and the add-on already
holds ``homeassistant_api: true`` for notifications, so the location costs one request
at start-up through the Supervisor's Core proxy. The ``sun.sun`` entity is no use here:
it only exposes the *next* rising/setting, and a recap for last Tuesday needs last
Tuesday's.

Sunrise/sunset/civil dawn/dusk are computed locally with the NOAA solar-calculator
sequence (Meeus-derived; about a minute of accuracy below ~72° latitude). No dependency:
it is thirty lines of trigonometry and the add-on must work without outbound internet.

Time zone: every SQL bucket in ``db`` uses SQLite ``'localtime'`` and every view uses
``time.mktime``/``time.localtime`` — the container's zone, which the Supervisor sets from
Home Assistant's. Sun maths uses that same zone so ticks and shading line up; HA's
``time_zone`` is only *compared* against it and a mismatch logged once.

Without a location (not running under the Supervisor, or the request failed) every
``SunTimes`` is ``known=False`` and the chorus falls back to a fixed clock window, which
the recap labels as such.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from typing import Optional

import httpx

log = logging.getLogger("aviary.solar")

_CONFIG_URL = "http://supervisor/core/api/config"
# The house does not move; six hours is about tolerating an edited HA location.
_REFRESH_OK_S = 6 * 3600
_REFRESH_RETRY_S = 60.0

# Dawn chorus window around sunrise. Songbirds start in the half-hour before the sun is
# up and the chorus thins out within an hour or so of it.
DAWN_BEFORE_SUNRISE_S = 30 * 60
DAWN_AFTER_SUNRISE_S = 60 * 60
# Local clock window used when the location (or a sunrise — polar day) is unknown.
FALLBACK_DAWN = ((5, 0), (7, 30))

# Solar zenith angles: the geometric horizon plus refraction and the solar radius for
# rise/set; civil twilight for dawn/dusk.
_ZENITH_RISE_SET = 90.833
_ZENITH_CIVIL = 96.0

_client: Optional[httpx.AsyncClient] = None
_location: Optional["Location"] = None
_tz_warned = False


@dataclass(frozen=True)
class Location:
    lat: float
    lon: float
    elevation: float = 0.0
    time_zone: str = ""


@dataclass(frozen=True)
class SunTimes:
    """One local day's solar events as epoch seconds; ``None`` means the event does not
    happen that day (midnight sun / polar night). ``known`` is False when the chorus
    window is the clock fallback rather than sunrise-derived."""
    day: date
    day_start: float
    day_end: float
    dawn: Optional[float]
    sunrise: Optional[float]
    sunset: Optional[float]
    dusk: Optional[float]
    chorus_start: float
    chorus_end: float
    known: bool

    @property
    def noon(self) -> Optional[float]:
        if self.sunrise is None or self.sunset is None:
            return None
        return (self.sunrise + self.sunset) / 2


# ------------------------------------------------------------------ location fetch

def init_client() -> None:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(15.0))


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def location() -> Optional[Location]:
    return _location


def set_location(loc: Optional[Location]) -> None:
    """Install a location directly (tests; the AVIARY_LAT/AVIARY_LON dev override)."""
    global _location
    _location = loc


def _dev_override() -> Optional[Location]:
    lat, lon = os.environ.get("AVIARY_LAT"), os.environ.get("AVIARY_LON")
    if not lat or not lon:
        return None
    try:
        return Location(float(lat), float(lon))
    except ValueError:
        log.warning("AVIARY_LAT/AVIARY_LON are not numbers; ignoring.")
        return None


def _parse_config(data: dict) -> Optional[Location]:
    try:
        lat, lon = float(data["latitude"]), float(data["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    try:
        elev = float(data.get("elevation") or 0.0)
    except (TypeError, ValueError):
        elev = 0.0
    return Location(lat, lon, elev, str(data.get("time_zone") or ""))


def _warn_tz_mismatch(loc: Location) -> None:
    global _tz_warned
    if _tz_warned or not loc.time_zone:
        return
    container = os.environ.get("TZ", "")
    if container and container != loc.time_zone:
        _tz_warned = True
        log.warning(
            "Home Assistant's time zone is %s but the container's TZ is %s; sun times "
            "and charts follow the container.", loc.time_zone, container)


async def fetch_location() -> Optional[Location]:
    """One request to the Core API for the configured location; None on any failure."""
    if _client is None or not os.environ.get("SUPERVISOR_TOKEN"):
        return None
    headers = {"Authorization": f"Bearer {os.environ.get('SUPERVISOR_TOKEN', '')}"}
    try:
        resp = await _client.get(_CONFIG_URL, headers=headers)
    except httpx.HTTPError as exc:
        log.info("Could not fetch the Home Assistant location: %s", exc)
        return None
    if resp.status_code >= 400:
        log.info("Home Assistant config request returned %s", resp.status_code)
        return None
    try:
        return _parse_config(resp.json())
    except ValueError:
        return None


async def location_loop() -> None:
    """Background task: learn the location, then re-check it every few hours."""
    if not os.environ.get("SUPERVISOR_TOKEN"):
        loc = _dev_override()
        if loc:
            set_location(loc)
            log.info("Location from AVIARY_LAT/AVIARY_LON: %.4f, %.4f", loc.lat, loc.lon)
        else:
            log.info("No SUPERVISOR_TOKEN; location unknown, dawn chorus uses %02d:%02d–%02d:%02d.",
                     *FALLBACK_DAWN[0], *FALLBACK_DAWN[1])
        return
    while True:
        loc = await fetch_location()
        if loc:
            if loc != _location:
                log.info("Location from Home Assistant: %.4f, %.4f (%s)", loc.lat, loc.lon,
                         loc.time_zone or "no time zone")
            set_location(loc)
            _warn_tz_mismatch(loc)
            await asyncio.sleep(_REFRESH_OK_S)
        else:
            await asyncio.sleep(_REFRESH_RETRY_S)


# --------------------------------------------------------------------- the maths

def local_midnight(d: date, tz: Optional[tzinfo] = None) -> float:
    """Epoch of local midnight starting ``d`` — the same boundary the dashboard's "today"
    stats and the recap windows use. ``tz`` (tests only) replaces the OS zone."""
    if tz is not None:
        return datetime(d.year, d.month, d.day, tzinfo=tz).timestamp()
    return time.mktime((d.year, d.month, d.day, 0, 0, 0, 0, 0, -1))


def _utc_offset_minutes(epoch: float, tz: Optional[tzinfo]) -> float:
    dt = datetime.fromtimestamp(epoch, tz) if tz else datetime.fromtimestamp(epoch).astimezone()
    off = dt.utcoffset() or timedelta(0)
    return off.total_seconds() / 60.0


def _solar_position(jd: float) -> tuple[float, float]:
    """(declination in degrees, equation of time in minutes) for a Julian day."""
    jc = (jd - 2451545.0) / 36525.0
    l0 = (280.46646 + jc * (36000.76983 + jc * 0.0003032)) % 360.0
    m = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)
    e = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)
    mr = math.radians(m)
    c = (math.sin(mr) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
         + math.sin(2 * mr) * (0.019993 - 0.000101 * jc)
         + math.sin(3 * mr) * 0.000289)
    true_long = l0 + c
    omega = math.radians(125.04 - 1934.136 * jc)
    app_long = true_long - 0.00569 - 0.00478 * math.sin(omega)
    eps0 = 23.0 + (26.0 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60.0) / 60.0
    eps = math.radians(eps0 + 0.00256 * math.cos(omega))
    decl = math.degrees(math.asin(math.sin(eps) * math.sin(math.radians(app_long))))
    y = math.tan(eps / 2) ** 2
    l0r = math.radians(l0)
    eot = 4.0 * math.degrees(
        y * math.sin(2 * l0r) - 2 * e * math.sin(mr)
        + 4 * e * y * math.sin(mr) * math.cos(2 * l0r)
        - 0.5 * y * y * math.sin(4 * l0r) - 1.25 * e * e * math.sin(2 * mr))
    return decl, eot


def _hour_angle(lat: float, decl: float, zenith: float) -> Optional[float]:
    """Degrees of hour angle at which the sun reaches ``zenith``; None when it never does."""
    latr, dr = math.radians(lat), math.radians(decl)
    cos_ha = (math.cos(math.radians(zenith)) / (math.cos(latr) * math.cos(dr))
              - math.tan(latr) * math.tan(dr))
    if cos_ha < -1.0 or cos_ha > 1.0:
        return None
    return math.degrees(math.acos(cos_ha))


def _fallback_window(d: date, tz: Optional[tzinfo]) -> tuple[float, float]:
    (h1, m1), (h2, m2) = FALLBACK_DAWN
    if tz is not None:
        base = datetime(d.year, d.month, d.day, tzinfo=tz)
        return (base.replace(hour=h1, minute=m1).timestamp(),
                base.replace(hour=h2, minute=m2).timestamp())
    return (time.mktime((d.year, d.month, d.day, h1, m1, 0, 0, 0, -1)),
            time.mktime((d.year, d.month, d.day, h2, m2, 0, 0, 0, -1)))


def sun_times(d: date, loc: Optional[Location] = None, tz: Optional[tzinfo] = None,
              use_default: bool = True) -> SunTimes:
    """Solar events for local day ``d``.

    ``loc`` defaults to the location learned from Home Assistant; pass ``use_default=False``
    to force the unknown-location result. ``tz`` (tests) replaces the OS zone.
    """
    if loc is None and use_default:
        loc = _location
    day_start = local_midnight(d, tz)
    day_end = local_midnight(d + timedelta(days=1), tz)
    fb_start, fb_end = _fallback_window(d, tz)
    if loc is None:
        return SunTimes(d, day_start, day_end, None, None, None, None, fb_start, fb_end, False)

    noon_local = day_start + 43200.0
    off_min = _utc_offset_minutes(noon_local, tz)
    jd = noon_local / 86400.0 + 2440587.5
    decl, eot = _solar_position(jd)
    # Minutes past local midnight of solar noon; each degree of hour angle is 4 minutes.
    noon_min = 720.0 - 4.0 * loc.lon - eot + off_min

    def at(minutes: Optional[float]) -> Optional[float]:
        return None if minutes is None else day_start + minutes * 60.0

    ha_rs = _hour_angle(loc.lat, decl, _ZENITH_RISE_SET)
    ha_civ = _hour_angle(loc.lat, decl, _ZENITH_CIVIL)
    sunrise = at(noon_min - 4.0 * ha_rs) if ha_rs is not None else None
    sunset = at(noon_min + 4.0 * ha_rs) if ha_rs is not None else None
    dawn = at(noon_min - 4.0 * ha_civ) if ha_civ is not None else None
    dusk = at(noon_min + 4.0 * ha_civ) if ha_civ is not None else None
    if sunrise is None:
        return SunTimes(d, day_start, day_end, dawn, None, None, dusk, fb_start, fb_end, False)
    return SunTimes(d, day_start, day_end, dawn, sunrise, sunset, dusk,
                    sunrise - DAWN_BEFORE_SUNRISE_S, sunrise + DAWN_AFTER_SUNRISE_S, True)


# ----------------------------------------------------------------------- helpers

def phase_label(epoch: float, sun: SunTimes) -> str:
    """night | dawn | morning | midday | afternoon | evening for an instant in the day.

    Twilight bounds come from the sun when known; otherwise fixed clock bounds that
    approximate a temperate day, so the label is always something rather than nothing.
    """
    if sun.known and sun.sunrise and sun.sunset:
        dawn = sun.dawn if sun.dawn is not None else sun.sunrise - 1800
        dusk = sun.dusk if sun.dusk is not None else sun.sunset + 1800
        sunrise, sunset = sun.sunrise, sun.sunset
    else:
        def h(hh: float) -> float:
            return sun.day_start + hh * 3600
        dawn, sunrise, sunset, dusk = h(5), h(6), h(19), h(20)
    noon = (sunrise + sunset) / 2
    if epoch < dawn or epoch >= dusk:
        return "night"
    if epoch < sunrise + 2 * 3600:
        return "dawn"
    if epoch >= sunset - 2 * 3600:
        return "evening"
    if abs(epoch - noon) <= 1.5 * 3600:
        return "midday"
    return "morning" if epoch < noon else "afternoon"


def clock(epoch: Optional[float], tz: Optional[tzinfo] = None) -> str:
    """'05:43' for an epoch, '—' for None."""
    if epoch is None:
        return "—"
    try:
        dt = datetime.fromtimestamp(epoch, tz) if tz else datetime.fromtimestamp(epoch)
        return dt.strftime("%H:%M")
    except (OverflowError, OSError, ValueError):
        return "—"


def hours(epoch: Optional[float], sun: SunTimes) -> Optional[float]:
    """Fractional hours past local midnight (for the chart markers); None for None."""
    if epoch is None:
        return None
    return round((epoch - sun.day_start) / 3600.0, 3)


def chart_payload(sun: SunTimes) -> Optional[dict]:
    """What the hourly charts need to draw sunrise/sunset: None when the sun is unknown."""
    if not sun.known:
        return None
    return {
        "sunrise": hours(sun.sunrise, sun), "sunset": hours(sun.sunset, sun),
        "sunrise_label": clock(sun.sunrise), "sunset_label": clock(sun.sunset),
    }
