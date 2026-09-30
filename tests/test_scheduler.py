"""
Tests for the schedule logic (astro.py, scheduler.py) — no Home Assistant
needed. Reference values are the same ones the web app's simulation was
checked against, so both sides stay in step.

Run from the repository root:
    python -m unittest discover -s tests
"""

import os
import sys
import unittest
from datetime import date, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components", "keyframe_scheduler"))

import astro  # noqa: E402
from scheduler import Evaluator, resolve_day, spec_from_dict  # noqa: E402

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo

BERLIN = {"latitude": 52.52, "longitude": 13.405}


def hm(minutes):
    """Minutes -> HH:MM (None -> None)."""
    if minutes is None:
        return None
    total = int(minutes + 0.5)
    return f"{total // 60:02d}:{total % 60:02d}"


def kf(time, kelvin, dim, mode, **extra):
    """Keyframe dict in the web app's export format."""
    data = {"time": time, "kelvin": kelvin, "dim": dim, "mode": mode,
            "curve": "sinus", "transitionSeconds": 600, "transitionDirection": "after"}
    data.update(extra)
    return data


def schedule(keyframes, **extra):
    data = {"version": 1, "timezone": "Europe/Berlin", "stepMinutes": 5,
            "location": BERLIN, "keyframes": keyframes}
    data.update(extra)
    return data


def times(spec, day):
    return [k.time for k in resolve_day(spec, day)]


def dim_at(evaluator, day, clock):
    h, m = map(int, clock.split(":"))
    when = datetime(day.year, day.month, day.day, h, m, tzinfo=ZoneInfo("Europe/Berlin"))
    return round(evaluator.evaluate_at(when).dim)


class AstroTest(unittest.TestCase):
    def events(self, day, lat, lon, tz):
        e = astro.sun_times(day, lat, lon, tz).events
        return {k: hm(v) for k, v in e.items()}

    def test_berlin_solstices(self):
        june = self.events(date(2026, 6, 21), 52.52, 13.405, "Europe/Berlin")
        self.assertEqual(june["sunrise"], "04:43")
        self.assertEqual(june["sunset"], "21:33")
        self.assertEqual(june["solar_noon"], "13:08")
        self.assertEqual(june["solar_midnight"], "01:08")
        self.assertIsNone(june["astronomical_dawn"])  # it never gets that dark
        dec = self.events(date(2026, 12, 21), 52.52, 13.405, "Europe/Berlin")
        self.assertEqual(dec["sunrise"], "08:15")
        self.assertEqual(dec["sunset"], "15:54")

    def test_dst_day(self):
        e = self.events(date(2026, 3, 29), 52.52, 13.405, "Europe/Berlin")
        self.assertEqual(e["sunrise"], "06:49")  # already CEST
        self.assertEqual(e["sunset"], "19:35")

    def test_polar_day_and_night(self):
        summer = self.events(date(2026, 6, 21), 69.65, 18.96, "Europe/Oslo")
        self.assertIsNone(summer["sunrise"])
        self.assertIsNone(summer["sunset"])
        winter = self.events(date(2026, 12, 21), 69.65, 18.96, "Europe/Oslo")
        self.assertIsNone(winter["sunrise"])
        self.assertEqual(winter["civil_dawn"], "09:31")

    def test_southern_hemisphere(self):
        e = self.events(date(2026, 6, 21), -33.87, 151.21, "Australia/Sydney")
        self.assertEqual(e["sunrise"], "07:00")
        self.assertEqual(e["sunset"], "16:54")


class ParseTest(unittest.TestCase):
    def test_old_file_still_works(self):
        spec = spec_from_dict({"version": 1, "timezone": "Europe/Berlin", "wrapAround": False,
                               "startDateTime": "2026-01-01T00:00:00",
                               "keyframes": [kf("06:00", 3000, 20, "instant")]})
        self.assertEqual(times(spec, date(2026, 5, 1)), ["06:00"])
        self.assertIsNone(spec.latitude)

    def test_default_location_from_home_assistant(self):
        spec = spec_from_dict({"version": 1, "keyframes": []}, default_location=(48.1, 11.6))
        self.assertEqual((spec.latitude, spec.longitude), (48.1, 11.6))
        spec = spec_from_dict(schedule([]), default_location=(48.1, 11.6))
        self.assertEqual(spec.latitude, 52.52)  # the file's location wins

    def test_invalid_input(self):
        with self.assertRaises(ValueError):
            spec_from_dict(schedule([kf("20:00", 3000, 50, "instant", trigger="sun", sunEvent="moonrise")]))
        with self.assertRaises(ValueError):
            spec_from_dict(schedule([kf("20:00", 3000, 50, "instant", group="Z")]))
        with self.assertRaises(ValueError):
            spec_from_dict(schedule([kf("20:00", 3000, 50, "instant", notBefore="25:00")]))


class ResolveTest(unittest.TestCase):
    def test_sun_offset_and_bounds(self):
        spec = spec_from_dict(schedule([
            kf("20:00", 3500, 70, "interpolate", trigger="sun", sunEvent="sunset",
               offsetMinutes=30, notBefore="18:00", notAfter="22:00"),
        ]))
        self.assertEqual(times(spec, date(2026, 9, 25)), ["19:29"])  # 18:59 + 30
        self.assertEqual(times(spec, date(2026, 6, 21)), ["22:00"])  # 22:03 -> not after 22:00
        self.assertEqual(times(spec, date(2026, 12, 21)), ["18:00"])  # 16:24 -> not before 18:00

    def test_missing_event_uses_bound_or_skips(self):
        with_bound = spec_from_dict(schedule([
            kf("05:00", 3000, 20, "instant", trigger="sun", sunEvent="astronomical_dawn", notBefore="04:00"),
        ]))
        without = spec_from_dict(schedule([
            kf("05:00", 3000, 20, "instant", trigger="sun", sunEvent="astronomical_dawn"),
        ]))
        june = date(2026, 6, 21)
        self.assertEqual(times(with_bound, june), ["04:00"])
        self.assertEqual(times(without, june), [])
        self.assertEqual(times(without, date(2026, 12, 21)), ["06:07"])

    def test_no_location_falls_back_to_bounds(self):
        spec = spec_from_dict({"version": 1, "keyframes": [
            kf("20:00", 3000, 50, "instant", trigger="sun", sunEvent="sunset", notAfter="21:00"),
        ]})
        self.assertEqual(times(spec, date(2026, 6, 21)), ["21:00"])

    def test_groups(self):
        members = [
            kf("12:00", 3500, 70, "interpolate", trigger="sun", sunEvent="sunset", group="A"),
            kf("20:00", 2700, 50, "instant", group="A"),
        ]
        june, dec = date(2026, 6, 21), date(2026, 12, 21)
        earliest = spec_from_dict(schedule(members, groups=[{"id": "A", "rule": "earliest"}]))
        self.assertEqual(times(earliest, june), ["20:00"])
        self.assertEqual(times(earliest, dec), ["15:54"])
        latest = spec_from_dict(schedule(members, groups=[{"id": "A", "rule": "latest"}]))
        self.assertEqual(times(latest, june), ["21:33"])
        self.assertEqual(times(latest, dec), ["20:00"])
        both = spec_from_dict(schedule(members, groups=[{"id": "A", "rule": "all"}]))
        self.assertEqual(times(both, june), ["21:33", "20:00"])  # table order kept
        default = spec_from_dict(schedule(members))  # no rule given -> earliest
        self.assertEqual(times(default, june), ["20:00"])


class EvaluateTest(unittest.TestCase):
    OFFICE = [  # "Büroprofil Lichtdusche"
        kf("06:00", 4000, 50, "interpolate", transitionSeconds=1800),
        kf("08:00", 4500, 80, "interpolate", transitionSeconds=300),
        kf("10:00", 5000, 100, "interpolate"),
        kf("12:00", 4500, 100, "interpolate", transitionSeconds=300),
        kf("14:00", 4000, 100, "interpolate", transitionSeconds=300),
        kf("15:00", 4000, 70, "interpolate", transitionSeconds=300),
        kf("15:30", 4000, 100, "transition", transitionSeconds=30, curve="linear"),
        kf("16:00", 3500, 70, "interpolate", transitionSeconds=300),
        kf("16:30", 3500, 100, "transition", transitionSeconds=30, curve="linear"),
        kf("17:00", 3000, 100, "interpolate", transitionSeconds=300),
        kf("19:00", 2700, 100, "interpolate", transitionSeconds=300),
        kf("22:00", 2700, 100, "interpolate", transitionSeconds=300),
    ]

    def test_continuous_across_midnight(self):
        ev = Evaluator(spec_from_dict(schedule(self.OFFICE)))
        day, next_day = date(2026, 9, 26), date(2026, 9, 27)
        self.assertEqual(dim_at(ev, day, "22:00"), 100)
        self.assertEqual(dim_at(ev, day, "23:59"), 93)   # same as the web app
        self.assertEqual(dim_at(ev, next_day, "00:00"), 93)
        self.assertEqual(dim_at(ev, next_day, "03:00"), 65)
        self.assertEqual(dim_at(ev, next_day, "06:00"), 50)

    def test_sun_keyframe_moves_with_the_season(self):
        spec = spec_from_dict(schedule([
            kf("07:00", 4000, 100, "instant"),
            kf("20:00", 2700, 30, "instant", trigger="sun", sunEvent="sunset"),
        ]))
        ev = Evaluator(spec)
        self.assertEqual(dim_at(ev, date(2026, 12, 21), "17:00"), 30)   # after 15:54
        self.assertEqual(dim_at(ev, date(2026, 6, 21), "21:00"), 100)   # before 21:33
        self.assertEqual(dim_at(ev, date(2026, 6, 21), "21:40"), 30)

    def test_group_winner_drives_the_curve(self):
        # Mixed example, grouped: A = 06:30 / sunrise+60 (earliest), B = sunset / 20:00 (latest)
        spec = spec_from_dict(schedule([
            kf("06:30", 3000, 40, "transition", transitionSeconds=1800, transitionDirection="before", group="A"),
            kf("06:00", 5000, 100, "transition", transitionSeconds=1800, transitionDirection="before",
               trigger="sun", sunEvent="sunrise", offsetMinutes=60, group="A"),
            kf("12:30", 5500, 100, "interpolate"),
            kf("20:00", 3500, 70, "interpolate", trigger="sun", sunEvent="sunset", group="B"),
            kf("20:00", 2700, 50, "instant", group="B"),
            kf("22:30", 2200, 10, "transition", transitionSeconds=1800, transitionDirection="before"),
        ], groups=[{"id": "A", "rule": "earliest"}, {"id": "B", "rule": "latest"}]))
        june = date(2026, 6, 21)
        self.assertEqual(times(spec, june), ["05:43", "12:30", "21:33", "22:30"])
        ev = Evaluator(spec)
        self.assertEqual(dim_at(ev, june, "02:00"), 10)    # night stays at 10 %
        self.assertEqual(dim_at(ev, june, "15:00"), 95)    # 12:30 -> sunset, same as the web app

    def test_dst_uses_wall_clock(self):
        spec = spec_from_dict(schedule([kf("07:00", 4000, 100, "instant"), kf("22:00", 2700, 10, "instant")]))
        ev = Evaluator(spec)
        # 29.03.2026: clocks jump 02:00 -> 03:00; 07:00 CEST is still "after 07:00"
        self.assertEqual(dim_at(ev, date(2026, 3, 29), "07:01"), 100)
        self.assertEqual(dim_at(ev, date(2026, 3, 29), "06:59"), 10)


class ValidOnTest(unittest.TestCase):
    """Valid-on filters (the PICO's `days`): weekdays and a yearly period."""

    # 2026-10-05 is a Monday
    MON, FRI, SAT, SUN = date(2026, 10, 5), date(2026, 10, 9), date(2026, 10, 10), date(2026, 10, 11)

    def test_parse(self):
        spec = spec_from_dict(schedule([
            kf("07:00", 4000, 100, "instant", validOn={"weekdays": ["mon", "fri"], "from": "11-01", "to": "02-28"}),
            kf("08:00", 4000, 100, "instant", validOn={"weekdays": list(("mon", "tue", "wed", "thu", "fri", "sat", "sun"))}),
        ]))
        self.assertEqual(spec.keyframes[0].valid_weekdays, (0, 4))
        self.assertEqual((spec.keyframes[0].valid_from, spec.keyframes[0].valid_to), ((11, 1), (2, 28)))
        self.assertIsNone(spec.keyframes[1].valid_weekdays)   # all seven = every day
        for bad in ({"weekdays": ["xyz"]}, {"weekdays": []}, {"from": "13-01", "to": "01-01"}, {"from": "05-01"}):
            with self.assertRaises(ValueError):
                spec_from_dict(schedule([kf("07:00", 4000, 100, "instant", validOn=bad)]))

    def test_weekdays_and_period_over_new_year(self):
        spec = spec_from_dict(schedule([
            kf("07:00", 4000, 100, "instant", validOn={"weekdays": ["mon", "tue", "wed", "thu", "fri"]}),
            kf("09:00", 3000, 50, "instant", validOn={"from": "12-20", "to": "01-06"}),
            kf("22:00", 2700, 10, "instant"),
        ]))
        self.assertEqual(times(spec, self.FRI), ["07:00", "22:00"])
        self.assertEqual(times(spec, self.SAT), ["22:00"])
        self.assertEqual(times(spec, date(2026, 12, 31)), ["07:00", "09:00", "22:00"])   # Thursday
        self.assertEqual(times(spec, date(2027, 1, 6)), ["07:00", "09:00", "22:00"])
        self.assertEqual(times(spec, date(2027, 1, 7)), ["07:00", "22:00"])

    def test_day_without_keyframes_holds_the_last_value(self):
        # Weekdays only: the weekend keeps Friday evening's value, Monday
        # morning starts from it too
        spec = spec_from_dict(schedule([
            kf("07:00", 4000, 100, "instant", validOn={"weekdays": ["mon", "tue", "wed", "thu", "fri"]}),
            kf("18:00", 2700, 20, "instant", validOn={"weekdays": ["mon", "tue", "wed", "thu", "fri"]}),
        ]))
        ev = Evaluator(spec)
        self.assertEqual(dim_at(ev, self.FRI, "12:00"), 100)
        self.assertEqual(dim_at(ev, self.SAT, "12:00"), 20)
        self.assertEqual(dim_at(ev, self.SUN, "12:00"), 20)
        self.assertEqual(dim_at(ev, self.MON, "06:00"), 20)
        self.assertEqual(dim_at(ev, self.MON, "08:00"), 100)

    def test_midnight_uses_the_real_previous_day(self):
        # Sunday evening differs from the other evenings: Monday 02:00 must
        # show Sunday's value, not Monday's own last keyframe
        spec = spec_from_dict(schedule([
            kf("07:00", 4000, 100, "instant"),
            kf("22:00", 2700, 10, "instant", validOn={"weekdays": ["mon", "tue", "wed", "thu", "fri", "sat"]}),
            kf("22:00", 2700, 40, "instant", validOn={"weekdays": ["sun"]}),
        ]))
        ev = Evaluator(spec)
        self.assertEqual(dim_at(ev, self.MON, "02:00"), 40)
        self.assertEqual(dim_at(ev, date(2026, 10, 6), "02:00"), 10)   # Tuesday: Monday evening

    def test_interpolation_runs_from_the_previous_day(self):
        # 20:00 -> 06:00 (next morning) ramps overnight, starting on the
        # previous day's timeline entry
        spec = spec_from_dict(schedule([
            kf("06:00", 4000, 100, "interpolate", curve="linear"),
            kf("20:00", 2700, 0, "interpolate", curve="linear",
               validOn={"weekdays": ["mon", "tue", "wed", "thu", "fri", "sun"]}),
        ]))
        ev = Evaluator(spec)
        # Tuesday 01:00: 5 h into the 10 h ramp from Monday 20:00 (0 %) to 100 %
        self.assertEqual(dim_at(ev, date(2026, 10, 6), "01:00"), 50)


if __name__ == "__main__":
    unittest.main()
