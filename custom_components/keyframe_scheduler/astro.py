"""
Keyframe Scheduler - astronomy.

Sun position and sun events (dawn, sunrise, noon, sunset, dusk, ...) for a
location and a LOCAL calendar date in an IANA timezone.

This is a 1:1 port of the web app's www/js/astro.js, so the integration and
the simulation in the web app compute the same times. Position formulas
follow the low-precision solar model used by SunCalc (Vladimir Agafonkin,
BSD-2) — about one minute of accuracy for event times.

Events are found numerically: the altitude curve of the local day is sampled
every 10 minutes and every threshold crossing is refined by bisection. This
handles DST days, timezones far from the location's meridian and polar
day/night (an event that does not happen returns None) without special cases.

No Home Assistant dependency — plain Python, testable on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from math import asin, atan2, cos, pi, sin, sqrt
from typing import Dict, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo

RAD = pi / 180
DAY_MS = 86_400_000
J1970 = 2440588
J2000 = 2451545
OBLIQUITY = RAD * 23.4397
STEP_MS = 10 * 60_000

# Altitudes in degrees. Sunrise/sunset use -0.833 deg (refraction + solar
# radius), the twilights their standard depression angles, golden hour the
# common +6 deg convention. Keep in sync with EVENTS in www/js/astro.js.
EVENTS: Dict[str, dict] = {
    "solar_midnight":    {"midnight": True},
    "astronomical_dawn": {"h": -18.0,   "rising": True},
    "nautical_dawn":     {"h": -12.0,   "rising": True},
    "civil_dawn":        {"h": -6.0,    "rising": True},
    "sunrise":           {"h": -0.833,  "rising": True},
    "golden_hour_end":   {"h": 6.0,     "rising": True},
    "solar_noon":        {"noon": True},
    "golden_hour_start": {"h": 6.0,     "rising": False},
    "sunset":            {"h": -0.833,  "rising": False},
    "civil_dusk":        {"h": -6.0,    "rising": False},
    "nautical_dusk":     {"h": -12.0,   "rising": False},
    "astronomical_dusk": {"h": -18.0,   "rising": False},
}
EVENT_NAMES = tuple(EVENTS)


def is_rising_event(name: str) -> bool:
    """True for morning events (the sun is rising)."""
    return bool(EVENTS.get(name, {}).get("rising"))


# ---- Solar position ---------------------------------------------------------

def altitude(ms: float, lat: float, lon: float) -> float:
    """Sun altitude in degrees at a UTC instant given in milliseconds."""
    d = ms / DAY_MS - 0.5 + J1970 - J2000
    m = RAD * (357.5291 + 0.98560028 * d)
    c = RAD * (1.9148 * sin(m) + 0.02 * sin(2 * m) + 0.0003 * sin(3 * m))
    ecl_lon = m + c + RAD * 102.9372 + pi
    dec = asin(sin(OBLIQUITY) * sin(ecl_lon))
    ra = atan2(sin(ecl_lon) * cos(OBLIQUITY), cos(ecl_lon))
    sidereal_time = RAD * (280.16 + 360.9856235 * d) + RAD * lon
    h = sidereal_time - ra
    phi = RAD * lat
    return asin(sin(phi) * sin(dec) + cos(phi) * cos(dec) * cos(h)) / RAD


# ---- Timezone helpers -------------------------------------------------------

def local_midnight_ms(day: date, tz: str) -> float:
    """UTC instant (ms) of local midnight at the start of `day` in tz."""
    return datetime(day.year, day.month, day.day, tzinfo=ZoneInfo(tz)).timestamp() * 1000


def wall_minutes(ms: float, day: date, tz: str) -> float:
    """Wall-clock minutes since local midnight of `day` for instant ms."""
    local = datetime.fromtimestamp(ms / 1000, ZoneInfo(tz)).replace(tzinfo=None)
    return (local - datetime(day.year, day.month, day.day)).total_seconds() / 60


# ---- Events -----------------------------------------------------------------

def _bisect(f, a: float, b: float) -> float:
    fa = f(a)
    for _ in range(24):  # 10 min / 2^24 -> far below 1 s
        mid = (a + b) / 2
        fm = f(mid)
        if (fm < 0) == (fa < 0):
            a, fa = mid, fm
        else:
            b = mid
    return (a + b) / 2


@dataclass(frozen=True)
class SunTimes:
    """Sun events of one local day, in wall-clock minutes since midnight."""
    events: Dict[str, Optional[float]]
    day_length: Optional[float]
    max_altitude: float
    min_altitude: float


@lru_cache(maxsize=4096)
def sun_times(day: date, lat: float, lon: float, tz: str) -> SunTimes:
    """Sun events of a local calendar day (None = does not occur that day)."""
    start = local_midnight_ms(day, tz)
    end = local_midnight_ms(day + timedelta(days=1), tz)
    samples = []
    t = start
    while t <= end:
        samples.append((t, altitude(t, lat, lon)))
        t += STEP_MS

    max_s = max(samples, key=lambda s: s[1])
    min_s = min(samples, key=lambda s: s[1])

    # Solar noon: golden-section search for the altitude maximum.
    lo, hi, g = max_s[0] - STEP_MS, max_s[0] + STEP_MS, (sqrt(5) - 1) / 2
    for _ in range(30):
        c = hi - g * (hi - lo)
        e = lo + g * (hi - lo)
        if altitude(c, lat, lon) > altitude(e, lat, lon):
            hi = e
        else:
            lo = c
    noon_ms = (lo + hi) / 2

    # Solar midnight: the anti-transit, 12 h from noon. Of the two candidates
    # take the one inside the local day; a 23 h DST day may have none.
    if noon_ms - DAY_MS / 2 >= start:
        midnight_ms: Optional[float] = noon_ms - DAY_MS / 2
    elif noon_ms + DAY_MS / 2 < end:
        midnight_ms = noon_ms + DAY_MS / 2
    else:
        midnight_ms = None

    events: Dict[str, Optional[float]] = {}
    for name, ev in EVENTS.items():
        events[name] = None
        if ev.get("noon"):
            events[name] = wall_minutes(noon_ms, day, tz)
            continue
        if ev.get("midnight"):
            events[name] = None if midnight_ms is None else wall_minutes(midnight_ms, day, tz)
            continue
        h, rising = ev["h"], ev["rising"]

        def f(ms: float, h: float = h) -> float:
            return altitude(ms, lat, lon) - h

        for k in range(1, len(samples)):
            p = samples[k - 1][1] - h
            q = samples[k][1] - h
            crosses = (p < 0 <= q) if rising else (p >= 0 > q)
            if crosses:
                events[name] = wall_minutes(_bisect(f, samples[k - 1][0], samples[k][0]), day, tz)
                break

    sunrise, sunset = events["sunrise"], events["sunset"]
    return SunTimes(
        events=events,
        day_length=(sunset - sunrise) if sunrise is not None and sunset is not None else None,
        max_altitude=max_s[1],
        min_altitude=min_s[1],
    )
