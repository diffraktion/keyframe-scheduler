"""
Keyframe Scheduler v2 - Core scheduling logic.

Supports three modes:
- instant: Immediate jump to target value
- transition: Timed transition with direction (before/after keyframe time)
- interpolate: Continuous interpolation between keyframes

Curves: linear, sinus (ease-in-out)

Keyframes fire either at a fixed local time or at a sun event (with offset
and optional not-before/not-after bounds), and can be grouped so that only
one member fires per day. `resolve_day` turns the schedule into the concrete
keyframes of one calendar day; everything else works on that list. The rules
mirror the web app (www/index.html: resolveKeyframeMinutes, listGroups).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from math import cos, floor, pi
from typing import Any, Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

try:
    from .astro import EVENTS as SUN_EVENTS, is_rising_event, sun_times
except ImportError:  # loaded outside the package (tests)
    from astro import EVENTS as SUN_EVENTS, is_rising_event, sun_times


def clamp(v: float, lo: float, hi: float) -> float:
    """Clamp value between lo and hi."""
    return max(lo, min(hi, v))


def parse_time(time_str: str) -> int:
    """Parse HH:MM to minutes since midnight."""
    parts = time_str.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time format: {time_str}")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"Time out of range: {time_str}")
    return h * 60 + m


def ease_sinus(t: float) -> float:
    """Sinus ease-in-out curve."""
    t = clamp(t, 0.0, 1.0)
    return 0.5 - 0.5 * cos(pi * t)


def lerp(a: float, b: float, t: float) -> float:
    """Linear interpolation."""
    return a + (b - a) * t


def format_time(minutes: float) -> str:
    """Minutes since midnight -> HH:MM (rounded half up, like the web app)."""
    total = int(floor(minutes + 0.5))
    return f"{(total // 60) % 24:02d}:{total % 60:02d}"


def kelvin_to_mired(kelvin: float) -> int:
    """Convert Kelvin to mired."""
    kelvin = max(1000.0, kelvin)
    return int(round(1_000_000.0 / kelvin))


@dataclass(frozen=True)
class Keyframe:
    """Single keyframe in the schedule.

    For trigger "sun", `time` is only a fallback written by the web app; the
    firing time comes from `sun_event` + `offset_minutes`, clamped to
    [`not_before`, `not_after`] (see resolve_keyframe_minutes).
    """
    time: str  # HH:MM format
    kelvin: float
    dim: float  # 0-100%
    mode: str = "instant"  # instant, transition, interpolate
    curve: str = "linear"  # linear, sinus
    transition_seconds: int = 300
    transition_direction: str = "after"  # after, before (for transition mode)
    trigger: str = "time"  # time, sun
    sun_event: str = "sunset"  # key of astro.EVENTS
    offset_minutes: int = 0
    not_before: Optional[str] = None  # HH:MM
    not_after: Optional[str] = None  # HH:MM
    group: str = ""  # "" or A-H
    id: str = ""  # stable id from the web app (informational)
    # Valid on (the PICO's `days` filter): weekdays 0=Mon..6=Sun (None = every
    # day) and an optional (month, day) period, both ends inclusive; a period
    # with valid_to before valid_from spans New Year. Both must hold.
    valid_weekdays: Optional[Tuple[int, ...]] = None
    valid_from: Optional[Tuple[int, int]] = None
    valid_to: Optional[Tuple[int, int]] = None
    # Set on resolved keyframes: minutes relative to the midnight of the day
    # being evaluated (may lie before 0 or after 1440, see Evaluator.timeline_for)
    minutes: Optional[float] = None


GROUP_IDS = ("A", "B", "C", "D", "E", "F", "G", "H")
GROUP_RULES = ("earliest", "latest", "all")


@dataclass(frozen=True)
class ScheduleSpec:
    """Complete schedule specification.

    A schedule is always a daily profile: keyframes repeat every day and the
    curve continues across midnight.
    """
    timezone: str = "Europe/Berlin"
    step_minutes: int = 5
    horizon_hours: int = 48
    keyframes: Tuple[Keyframe, ...] = ()
    latitude: Optional[float] = None  # needed for sun keyframes
    longitude: Optional[float] = None
    groups: Tuple[Tuple[str, str], ...] = ()  # (group id, rule)

    def group_rule(self, group_id: str) -> str:
        """Rule of a group; groups without an explicit rule use 'earliest'."""
        for gid, rule in self.groups:
            if gid == group_id:
                return rule
        return "earliest"

    @property
    def has_sun_keyframes(self) -> bool:
        return any(kf.trigger == "sun" for kf in self.keyframes)


@dataclass(frozen=True)
class ScheduleValues:
    """Output values at a specific time."""
    kelvin: float
    dim: float
    transition_seconds: int


WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _month_day(value: Any, field: str) -> Tuple[int, int]:
    """'MM-DD' -> (month, day)."""
    try:
        month, day = (int(x) for x in str(value).split("-"))
        date(2000, month, day)  # leap year: 02-29 is allowed
    except (ValueError, TypeError) as err:
        raise ValueError(f"Invalid {field}: {value}") from err
    return month, day


def _valid_on(data: Any) -> Tuple[Optional[Tuple[int, ...]], Optional[Tuple[int, int]], Optional[Tuple[int, int]]]:
    """Parse a keyframe's "validOn" block -> (weekdays, from, to)."""
    if not isinstance(data, dict):
        return None, None, None
    weekdays = None
    if data.get("weekdays") is not None:
        days = data["weekdays"]
        if not isinstance(days, list) or any(d not in WEEKDAY_KEYS for d in days) or not days:
            raise ValueError(f"Invalid validOn.weekdays: {days}")
        weekdays = tuple(sorted({WEEKDAY_KEYS.index(d) for d in days}))
        if len(weekdays) == 7:
            weekdays = None
    start = end = None
    if data.get("from") or data.get("to"):
        start = _month_day(data.get("from"), "validOn.from")
        end = _month_day(data.get("to"), "validOn.to")
    return weekdays, start, end


def is_valid_on(kf: Keyframe, day: date) -> bool:
    """Does `kf` apply on `day` (weekdays and period)? Mirrors isValidOn()."""
    if kf.valid_weekdays is not None and day.weekday() not in kf.valid_weekdays:
        return False
    if kf.valid_from is not None and kf.valid_to is not None:
        md = (day.month, day.day)
        if kf.valid_from <= kf.valid_to:
            return kf.valid_from <= md <= kf.valid_to
        return md >= kf.valid_from or md <= kf.valid_to
    return True


def kf_minutes(kf: Keyframe) -> float:
    """Time of a keyframe on the evaluation axis (see Keyframe.minutes)."""
    return kf.minutes if kf.minutes is not None else float(parse_time(kf.time))


def _optional_time(value: Any, field: str) -> Optional[str]:
    """Validate an optional HH:MM bound ('' / None = no bound)."""
    if value in (None, ""):
        return None
    text = str(value)
    try:
        parse_time(text)
    except ValueError as err:
        raise ValueError(f"Invalid {field}: {value}") from err
    return text


def spec_from_dict(
    data: Dict[str, Any],
    default_location: Optional[Tuple[float, float]] = None,
) -> ScheduleSpec:
    """Create ScheduleSpec from JSON dict.

    `default_location` (latitude, longitude) is used for sun keyframes when
    the file has no "location" block — in HA the configured home location.
    """
    if data.get("version", 1) != 1:
        raise ValueError("Unsupported spec version")

    timezone = data.get("timezone", "Europe/Berlin")
    try:
        ZoneInfo(timezone)
    except Exception as err:
        raise ValueError(f"Unknown timezone: {timezone}") from err
    step_minutes = max(1, min(60, int(data.get("stepMinutes", 5))))
    horizon_hours = max(1, min(168, int(data.get("horizonHours", 48))))
    # "wrapAround" and "startDateTime" in older files are ignored: the
    # one-shot mode they configured has been removed, every schedule is daily.

    latitude = longitude = None
    location = data.get("location")
    if isinstance(location, dict) and location.get("latitude") is not None:
        latitude = float(location["latitude"])
        longitude = float(location["longitude"])
    elif default_location is not None:
        latitude, longitude = default_location
    if latitude is not None and not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError(f"Invalid location: {latitude}, {longitude}")

    keyframes = []
    for kf_data in data.get("keyframes", []):
        # Get transition parameters
        transition_seconds = kf_data.get("transitionSeconds", step_minutes * 60)
        transition_seconds = max(0, int(transition_seconds))
        transition_direction = kf_data.get("transitionDirection", "after")

        trigger = "sun" if kf_data.get("trigger") == "sun" else "time"
        sun_event = str(kf_data.get("sunEvent", "sunset"))
        if trigger == "sun" and sun_event not in SUN_EVENTS:
            raise ValueError(f"Unknown sunEvent: {sun_event}")
        group = str(kf_data.get("group") or "")
        if group and group not in GROUP_IDS:
            raise ValueError(f"Invalid group: {group}")

        time_str = str(kf_data.get("time", "00:00"))
        parse_time(time_str)  # validate (also the fallback of sun keyframes)
        valid_weekdays, valid_from, valid_to = _valid_on(kf_data.get("validOn"))

        keyframes.append(
            Keyframe(
                time=time_str,
                kelvin=float(kf_data.get("kelvin", 4000)),
                dim=float(kf_data.get("dim", 50)),
                mode=str(kf_data.get("mode", "instant")),
                curve=str(kf_data.get("curve", "linear")),
                transition_seconds=transition_seconds,
                transition_direction=transition_direction,
                trigger=trigger,
                sun_event=sun_event,
                offset_minutes=int(kf_data.get("offsetMinutes") or 0),
                not_before=_optional_time(kf_data.get("notBefore"), "notBefore"),
                not_after=_optional_time(kf_data.get("notAfter"), "notAfter"),
                group=group,
                id=str(kf_data.get("id") or ""),
                valid_weekdays=valid_weekdays,
                valid_from=valid_from,
                valid_to=valid_to,
            )
        )

    groups = []
    for g in data.get("groups") or []:
        if isinstance(g, dict) and g.get("id") in GROUP_IDS and g.get("rule") in GROUP_RULES:
            groups.append((g["id"], g["rule"]))

    return ScheduleSpec(
        timezone=timezone,
        step_minutes=step_minutes,
        horizon_hours=horizon_hours,
        keyframes=tuple(keyframes),
        latitude=latitude,
        longitude=longitude,
        groups=tuple(groups),
    )


# ---- Daily resolution -------------------------------------------------------

def resolve_keyframe_minutes(kf: Keyframe, day: date, spec: ScheduleSpec) -> Optional[float]:
    """Minutes since local midnight at which `kf` fires on `day`, or None.

    Sun keyframes: event time + offset, clamped to [not_before, not_after].
    If the event does not happen at all (polar day/night, astronomical
    twilight in a northern summer) or no location is known, the bound takes
    over; without a bound the keyframe is skipped for that day. Days outside
    the keyframe's valid-on filter: None.
    """
    if not is_valid_on(kf, day):
        return None
    if kf.trigger != "sun":
        return float(parse_time(kf.time))

    lo = parse_time(kf.not_before) if kf.not_before else None
    hi = parse_time(kf.not_after) if kf.not_after else None

    event_minutes = None
    if spec.latitude is not None and spec.longitude is not None:
        event_minutes = sun_times(day, spec.latitude, spec.longitude, spec.timezone).events.get(kf.sun_event)

    if event_minutes is None:
        if is_rising_event(kf.sun_event):
            return float(lo) if lo is not None else (float(hi) if hi is not None else None)
        return float(hi) if hi is not None else (float(lo) if lo is not None else None)

    m = event_minutes + kf.offset_minutes
    if lo is not None:
        m = max(m, lo)
    if hi is not None:
        m = min(m, hi)
    return float(clamp(floor(m + 0.5), 0, 1439))


def resolve_day(spec: ScheduleSpec, day: date) -> Tuple[Keyframe, ...]:
    """The keyframes that fire on `day`, with concrete HH:MM times.

    Group rules are applied: per group only the earliest / latest member
    fires ('all' keeps every member). Ties go to the member higher up in the
    list. Keyframes that do not fire are left out; table order is kept.
    """
    minutes: List[Optional[float]] = [resolve_keyframe_minutes(kf, day, spec) for kf in spec.keyframes]

    for group_id in GROUP_IDS:
        members = [i for i, kf in enumerate(spec.keyframes) if kf.group == group_id]
        rule = spec.group_rule(group_id)
        if rule == "all":
            continue
        firing = [i for i in members if minutes[i] is not None]
        if len(firing) < 2:
            continue
        winner = firing[0]
        for i in firing[1:]:
            if (rule == "earliest" and minutes[i] < minutes[winner]) or (
                rule == "latest" and minutes[i] > minutes[winner]
            ):
                winner = i
        for i in firing:
            if i != winner:
                minutes[i] = None

    return tuple(
        replace(kf, time=format_time(m), minutes=m)
        for kf, m in zip(spec.keyframes, minutes)
        if m is not None
    )


class Evaluator:
    """Evaluates schedule at any given time."""

    def __init__(
        self,
        spec: ScheduleSpec,
        default_kelvin: float = 4000.0,
        default_dim: float = 50.0,
    ):
        self.spec = spec
        self.default_kelvin = default_kelvin
        self.default_dim = default_dim
        self._day_cache: Dict[date, Tuple[Keyframe, ...]] = {}
        self._timeline_cache: Dict[date, Tuple[Keyframe, ...]] = {}

    def keyframes_for(self, day: date) -> Tuple[Keyframe, ...]:
        """Resolved keyframes of a local calendar day (cached)."""
        cached = self._day_cache.get(day)
        if cached is None:
            if len(self._day_cache) > 16:
                self._day_cache.clear()
            cached = self._day_cache[day] = resolve_day(self.spec, day)
        return cached

    def timeline_for(self, day: date) -> Tuple[Keyframe, ...]:
        """The keyframes around `day` on one time axis (minutes from its midnight).

        The nearest earlier day that has keyframes (-1440*k), the day itself
        and the nearest later day that has keyframes (+1440*k). The curve
        across midnight comes from the real neighbouring days, and a day
        without keyframes (valid-on filters) holds the last value, as the
        PICO does. Mirrors the web app's resolveTimeline().
        """
        cached = self._timeline_cache.get(day)
        if cached is not None:
            return cached

        def shifted(k: int) -> List[Keyframe]:
            return [
                replace(kf, minutes=kf_minutes(kf) + 1440 * k)
                for kf in self.keyframes_for(day + timedelta(days=k))
            ]

        result: List[Keyframe] = []
        for k in range(-1, -8, -1):
            earlier = shifted(k)
            if earlier:
                result.extend(earlier)
                break
        result.extend(shifted(0))
        for k in range(1, 8):
            later = shifted(k)
            if later:
                result.extend(later)
                break
        if len(self._timeline_cache) > 16:
            self._timeline_cache.clear()
        cached = self._timeline_cache[day] = tuple(result)
        return cached

    def local_now(self, when: datetime) -> datetime:
        """`when` on the wall clock of the schedule's timezone."""
        tz = ZoneInfo(self.spec.timezone)
        if when.tzinfo is None:
            when = when.replace(tzinfo=tz)
        return when.astimezone(tz)

    def evaluate_at(self, when: datetime) -> ScheduleValues:
        """Evaluate schedule at given datetime."""
        local = self.local_now(when)

        # Minutes since local midnight. Taken from the wall clock rather than
        # counted from a fixed start, so DST switches need no restart.
        time_of_day = (
            local.hour * 60
            + local.minute
            + (local.second + local.microsecond / 1_000_000) / 60.0
        )

        return self._evaluate_at_minutes(time_of_day, self.timeline_for(local.date()))

    def _evaluate_at_minutes(
        self, time_minutes: float, keyframes: Tuple[Keyframe, ...]
    ) -> ScheduleValues:
        """Evaluate at minutes since midnight for the day's resolved keyframes.

        `keyframes` is normally a timeline (see timeline_for) that already
        holds the neighbouring days; a plain day list wraps onto itself.
        """
        if not keyframes:
            return ScheduleValues(
                kelvin=self.default_kelvin,
                dim=self.default_dim,
                transition_seconds=self.spec.step_minutes * 60,
            )

        # Sort keyframes by time
        sorted_kf = sorted(keyframes, key=kf_minutes)

        # Find previous and next keyframes
        prev_kf = None
        next_kf = None
        before_prev_kf = None  # Keyframe before prev_kf

        for i, kf in enumerate(sorted_kf):
            kf_time = kf_minutes(kf)

            if kf_time <= time_minutes:
                # Update before_prev before updating prev
                if prev_kf is not None:
                    before_prev_kf = prev_kf
                prev_kf = (kf, kf_time)

            if kf_time > time_minutes and next_kf is None:
                next_kf = (kf, kf_time)

        # Wrap around midnight (every schedule is a daily profile)
        if prev_kf is None and sorted_kf:
            # Before first keyframe - use last from previous day
            last_kf = sorted_kf[-1]
            prev_kf = (last_kf, kf_minutes(last_kf) - 1440)
            # before_prev would be second-to-last
            if len(sorted_kf) > 1:
                before_last_kf = sorted_kf[-2]
                before_prev_kf = (before_last_kf, kf_minutes(before_last_kf) - 1440)

        if next_kf is None and sorted_kf:
            # After last keyframe - use first from next day
            first_kf = sorted_kf[0]
            next_kf = (first_kf, kf_minutes(first_kf) + 1440)

        # No previous keyframe - use default or next
        if prev_kf is None:
            if next_kf:
                kf, _ = next_kf
                return ScheduleValues(
                    kelvin=kf.kelvin,
                    dim=kf.dim,
                    transition_seconds=kf.transition_seconds,
                )
            return ScheduleValues(
                kelvin=self.default_kelvin,
                dim=self.default_dim,
                transition_seconds=self.spec.step_minutes * 60,
            )

        prev, prev_time = prev_kf
        default_transition = self.spec.step_minutes * 60

        # MODE: INSTANT
        if prev.mode == "instant":
            return ScheduleValues(
                kelvin=prev.kelvin,
                dim=prev.dim,
                transition_seconds=prev.transition_seconds,
            )

        # MODE: TRANSITION
        if prev.mode == "transition":
            next_kf_obj = None
            next_time_val = None
            if next_kf is not None:
                next_kf_obj, next_time_val = next_kf
            
            return self._evaluate_transition(
                prev, prev_time, time_minutes, default_transition,
                next_kf_obj, next_time_val, keyframes
            )

        # MODE: INTERPOLATE
        if prev.mode == "interpolate" and next_kf:
            next_kf_obj, next_time = next_kf
            
            # Get before_prev info if available
            before_prev_obj = None
            before_prev_time_val = None
            if before_prev_kf is not None:
                before_prev_obj, before_prev_time_val = before_prev_kf
            
            return self._evaluate_interpolate(
                prev,
                prev_time,
                next_kf_obj,
                next_time,
                time_minutes,
                default_transition,
                before_prev_obj,
                before_prev_time_val,
                keyframes,
            )

        # Fallback: hold previous value
        return ScheduleValues(
            kelvin=prev.kelvin,
            dim=prev.dim,
            transition_seconds=prev.transition_seconds,
        )

    def _evaluate_transition(
        self,
        kf: Keyframe,
        kf_time: float,
        current_time: float,
        default_transition: int,
        next_kf: Optional[Keyframe] = None,
        next_time: Optional[float] = None,
        keyframes: Tuple[Keyframe, ...] = (),
    ) -> ScheduleValues:
        """Evaluate transition mode."""
        transition_duration = kf.transition_seconds / 60.0  # to minutes

        # Calculate transition window
        if kf.transition_direction == "before":
            # Transition ends AT keyframe time
            transition_start = kf_time - transition_duration
            transition_end = kf_time
        else:
            # Transition starts AT keyframe time
            transition_start = kf_time
            transition_end = kf_time + transition_duration

        # Are we in the transition window?
        if transition_start <= current_time <= transition_end:
            # Return target values immediately
            # The integration provides the target, blueprint handles the transition
            return ScheduleValues(
                kelvin=kf.kelvin,
                dim=kf.dim,
                transition_seconds=kf.transition_seconds,
            )

        # After transition is complete
        if current_time >= transition_end:
            # Check if NEXT keyframe is interpolate
            if next_kf is not None and next_kf.mode == "interpolate" and next_time is not None:
                # Interpolate from transition end to next interpolate
                start_time = transition_end
                end_time = next_time
                
                if current_time >= end_time:
                    return ScheduleValues(
                        kelvin=next_kf.kelvin,
                        dim=next_kf.dim,
                        transition_seconds=default_transition,
                    )
                
                duration = end_time - start_time
                if duration > 0:
                    elapsed = current_time - start_time
                    t = clamp(elapsed / duration, 0.0, 1.0)
                    
                    # Use next keyframe's curve
                    if next_kf.curve == "sinus":
                        progress = ease_sinus(t)
                    else:
                        progress = t
                    
                    return ScheduleValues(
                        kelvin=lerp(kf.kelvin, next_kf.kelvin, progress),
                        dim=lerp(kf.dim, next_kf.dim, progress),
                        transition_seconds=default_transition,
                    )
            
            # No next interpolate or not time yet - hold transition end value
            return ScheduleValues(
                kelvin=kf.kelvin,
                dim=kf.dim,
                transition_seconds=0,
            )

        # Before transition starts - return previous value
        before_values = self._evaluate_at_minutes(transition_start - 0.1, keyframes)
        return before_values

    def _evaluate_interpolate(
        self,
        prev_kf: Keyframe,
        prev_time: float,
        next_kf: Keyframe,
        next_time: float,
        current_time: float,
        default_transition: int,
        before_prev_kf: Optional[Keyframe] = None,
        before_prev_time: Optional[float] = None,
        keyframes: Tuple[Keyframe, ...] = (),
    ) -> ScheduleValues:
        """Evaluate interpolate mode.
        
        Three phases:
        1. BEFORE this keyframe: Interpolate TO it from previous
        2. AT this keyframe: Reached target value
        3. AFTER this keyframe: Check if next is also interpolate
           - If yes: Continue interpolating to next
           - If no: Hold value until next keyframe
        """
        sorted_kf = sorted(keyframes, key=kf_minutes)
        
        # Find current keyframe index
        current_index = None
        for i, kf in enumerate(sorted_kf):
            if kf == prev_kf:
                current_index = i
                break
        
        if current_index is None:
            return ScheduleValues(
                kelvin=prev_kf.kelvin,
                dim=prev_kf.dim,
                transition_seconds=default_transition,
            )
        
        # PHASE 1: Are we BEFORE this keyframe? Interpolate TO it
        if current_time < prev_time:
            # Find interpolation START point
            start_time = 0.0
            start_kelvin = prev_kf.kelvin
            start_dim = prev_kf.dim
            
            if before_prev_kf is not None and before_prev_time is not None:
                start_kelvin = before_prev_kf.kelvin
                start_dim = before_prev_kf.dim
                
                if before_prev_kf.mode == "transition":
                    trans_duration = before_prev_kf.transition_seconds / 60.0
                    
                    if before_prev_kf.transition_direction == "after":
                        start_time = before_prev_time + trans_duration
                    else:
                        start_time = before_prev_time
                else:
                    start_time = before_prev_time
            
            # Interpolate to THIS keyframe
            duration = prev_time - start_time
            
            if duration <= 0:
                return ScheduleValues(
                    kelvin=prev_kf.kelvin,
                    dim=prev_kf.dim,
                    transition_seconds=default_transition,
                )
            
            elapsed = current_time - start_time
            t = clamp(elapsed / duration, 0.0, 1.0)
            
            if prev_kf.curve == "sinus":
                progress = ease_sinus(t)
            else:
                progress = t
            
            return ScheduleValues(
                kelvin=lerp(start_kelvin, prev_kf.kelvin, progress),
                dim=lerp(start_dim, prev_kf.dim, progress),
                transition_seconds=default_transition,
            )
        
        # PHASE 2 & 3: We're AT or AFTER this keyframe
        
        # Check if next keyframe exists
        if next_kf is None:
            return ScheduleValues(
                kelvin=prev_kf.kelvin,
                dim=prev_kf.dim,
                transition_seconds=default_transition,
            )
        
        # If next is NOT interpolate, hold value
        if next_kf.mode != "interpolate":
            return ScheduleValues(
                kelvin=prev_kf.kelvin,
                dim=prev_kf.dim,
                transition_seconds=default_transition,
            )
        
        # Next is ALSO interpolate - continue interpolating to it
        
        # Find interpolation START point (may be adjusted by transitions)
        start_time = prev_time
        start_kelvin = prev_kf.kelvin
        start_dim = prev_kf.dim
        
        # Check for transitions/instants between this and next
        for i in range(current_index + 1, len(sorted_kf)):
            kf = sorted_kf[i]
            kf_time = kf_minutes(kf)
            
            if kf_time >= next_time:
                break
            
            if kf.mode == "transition":
                trans_duration = kf.transition_seconds / 60.0
                
                if kf.transition_direction == "after":
                    start_time = kf_time + trans_duration
                else:
                    start_time = kf_time
                
                start_kelvin = kf.kelvin
                start_dim = kf.dim
                
            elif kf.mode == "instant":
                start_time = kf_time
                start_kelvin = kf.kelvin
                start_dim = kf.dim
        
        # Interpolate to next interpolate keyframe
        end_time = next_time
        
        if current_time < start_time:
            return ScheduleValues(
                kelvin=start_kelvin,
                dim=start_dim,
                transition_seconds=default_transition,
            )
        
        if current_time >= end_time:
            return ScheduleValues(
                kelvin=next_kf.kelvin,
                dim=next_kf.dim,
                transition_seconds=default_transition,
            )
        
        # During interpolation to next
        duration = end_time - start_time
        
        if duration <= 0:
            return ScheduleValues(
                kelvin=next_kf.kelvin,
                dim=next_kf.dim,
                transition_seconds=default_transition,
            )
        
        elapsed = current_time - start_time
        t = clamp(elapsed / duration, 0.0, 1.0)
        
        # Use TARGET keyframe's curve
        if next_kf.curve == "sinus":
            progress = ease_sinus(t)
        else:
            progress = t
        
        return ScheduleValues(
            kelvin=lerp(start_kelvin, next_kf.kelvin, progress),
            dim=lerp(start_dim, next_kf.dim, progress),
            transition_seconds=default_transition,
        )
