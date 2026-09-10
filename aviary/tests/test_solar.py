"""Sun times: the NOAA sequence against published values, plus the fallbacks.

Fixed-offset zones throughout — named zones need ``tzdata`` on Windows dev boxes and the
maths only needs the offset anyway. Reference values are from NOAA's solar calculator
(gml.noaa.gov/grad/solcalc); a few minutes of slack covers refraction assumptions.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app import solar

LONDON = solar.Location(51.5074, -0.1278, 0.0, "Europe/London")
SYDNEY = solar.Location(-33.8688, 151.2093)
DENVER = solar.Location(39.7392, -104.9903)
TROMSO = solar.Location(69.6492, 18.9553)


def tz(hours: float) -> timezone:
    return timezone(timedelta(hours=hours))


def hm(epoch: float, zone: timezone) -> tuple[int, int]:
    dt = datetime.fromtimestamp(epoch, zone)
    return dt.hour, dt.minute


def minutes_off(epoch: float, zone: timezone, h: int, m: int) -> float:
    hh, mm = hm(epoch, zone)
    return abs((hh * 60 + mm) - (h * 60 + m))


@pytest.mark.parametrize("loc, day, off, rise, set_", [
    (LONDON, date(2024, 6, 21), 1, (4, 43), (21, 21)),
    (SYDNEY, date(2024, 6, 21), 10, (7, 1), (16, 54)),
    (DENVER, date(2024, 12, 21), -7, (7, 17), (16, 39)),
])
def test_sunrise_sunset_match_noaa(loc, day, off, rise, set_):
    zone = tz(off)
    s = solar.sun_times(day, loc, tz=zone)
    assert s.known
    assert minutes_off(s.sunrise, zone, *rise) <= 5
    assert minutes_off(s.sunset, zone, *set_) <= 5
    # Civil twilight brackets the day and the chorus brackets sunrise.
    assert s.dawn < s.sunrise < s.sunset < s.dusk
    assert s.chorus_start == s.sunrise - solar.DAWN_BEFORE_SUNRISE_S
    assert s.chorus_end == s.sunrise + solar.DAWN_AFTER_SUNRISE_S
    assert s.day_start <= s.dawn and s.dusk <= s.day_end


def test_midnight_sun_has_no_events_and_falls_back():
    zone = tz(2)
    s = solar.sun_times(date(2024, 6, 21), TROMSO, tz=zone)
    assert s.sunrise is None and s.sunset is None
    assert not s.known
    assert hm(s.chorus_start, zone) == solar.FALLBACK_DAWN[0]
    assert hm(s.chorus_end, zone) == solar.FALLBACK_DAWN[1]


def test_polar_night_keeps_twilight_but_no_sunrise():
    zone = tz(1)
    s = solar.sun_times(date(2024, 12, 21), TROMSO, tz=zone)
    assert s.sunrise is None and s.sunset is None
    # Civil twilight still happens in Tromsø's polar night — a real, if brief, "day".
    assert s.dawn is not None and s.dusk is not None and s.dawn < s.dusk
    assert not s.known


def test_no_location_uses_the_fixed_window():
    zone = timezone.utc
    s = solar.sun_times(date(2024, 6, 21), None, tz=zone, use_default=False)
    assert not s.known
    assert s.sunrise is None
    assert hm(s.chorus_start, zone) == (5, 0)
    assert hm(s.chorus_end, zone) == (7, 30)
    assert s.day_end - s.day_start == 86400


def test_phase_labels_follow_the_sun():
    zone = tz(1)
    s = solar.sun_times(date(2024, 6, 21), LONDON, tz=zone)
    assert solar.phase_label(s.sunrise + 600, s) == "dawn"
    assert solar.phase_label(s.day_start + 3600, s) == "night"
    assert solar.phase_label(s.noon, s) == "midday"
    assert solar.phase_label(s.sunset - 1800, s) == "evening"
    assert solar.phase_label(s.sunrise + 3 * 3600, s) == "morning"
    assert solar.phase_label(s.sunset - 4 * 3600, s) == "afternoon"


def test_phase_labels_without_a_sun_use_clock_bounds():
    zone = timezone.utc
    s = solar.sun_times(date(2024, 3, 1), None, tz=zone, use_default=False)
    assert solar.phase_label(s.day_start + 5.5 * 3600, s) == "dawn"
    assert solar.phase_label(s.day_start + 12.5 * 3600, s) == "midday"
    assert solar.phase_label(s.day_start + 23 * 3600, s) == "night"


def test_chart_payload_is_fractional_hours():
    zone = tz(1)
    s = solar.sun_times(date(2024, 6, 21), LONDON, tz=zone)
    p = solar.chart_payload(s)
    assert 4.6 < p["sunrise"] < 4.8
    assert 21.3 < p["sunset"] < 21.4
    assert solar.chart_payload(
        solar.sun_times(date(2024, 6, 21), None, tz=zone, use_default=False)) is None


def test_parse_config_validates():
    assert solar._parse_config({"latitude": "51.5", "longitude": -0.1, "time_zone": "Europe/London"}) \
        == solar.Location(51.5, -0.1, 0.0, "Europe/London")
    assert solar._parse_config({"latitude": 91, "longitude": 0}) is None
    assert solar._parse_config({"longitude": 0}) is None
    assert solar._parse_config({"latitude": "x", "longitude": 0}) is None
