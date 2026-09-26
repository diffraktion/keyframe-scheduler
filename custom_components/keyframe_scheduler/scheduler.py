"""
Keyframe Scheduler v2 - Core scheduling logic.

Supports three modes:
- instant: Immediate jump to target value
- transition: Timed transition with direction (before/after keyframe time)
- interpolate: Continuous interpolation between keyframes

Curves: linear, sinus (ease-in-out)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import cos, pi
from typing import Any, Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo


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


def kelvin_to_mired(kelvin: float) -> int:
    """Convert Kelvin to mired."""
    kelvin = max(1000.0, kelvin)
    return int(round(1_000_000.0 / kelvin))


@dataclass(frozen=True)
class Keyframe:
    """Single keyframe in the schedule."""
    time: str  # HH:MM format
    kelvin: float
    dim: float  # 0-100%
    mode: str = "instant"  # instant, transition, interpolate
    curve: str = "linear"  # linear, sinus
    transition_seconds: int = 300
    transition_direction: str = "after"  # after, before (for transition mode)


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


@dataclass(frozen=True)
class ScheduleValues:
    """Output values at a specific time."""
    kelvin: float
    dim: float
    transition_seconds: int


def spec_from_dict(data: Dict[str, Any]) -> ScheduleSpec:
    """Create ScheduleSpec from JSON dict."""
    if data.get("version", 1) != 1:
        raise ValueError("Unsupported spec version")

    timezone = data.get("timezone", "Europe/Berlin")
    step_minutes = max(1, min(60, int(data.get("stepMinutes", 5))))
    horizon_hours = max(1, min(168, int(data.get("horizonHours", 48))))
    # "wrapAround" and "startDateTime" in older files are ignored: the
    # one-shot mode they configured has been removed, every schedule is daily.

    keyframes = []
    for kf_data in data.get("keyframes", []):
        # Get transition parameters
        transition_seconds = kf_data.get("transitionSeconds", step_minutes * 60)
        transition_seconds = max(0, int(transition_seconds))
        transition_direction = kf_data.get("transitionDirection", "after")

        keyframes.append(
            Keyframe(
                time=str(kf_data.get("time", "00:00")),
                kelvin=float(kf_data.get("kelvin", 4000)),
                dim=float(kf_data.get("dim", 50)),
                mode=str(kf_data.get("mode", "instant")),
                curve=str(kf_data.get("curve", "linear")),
                transition_seconds=transition_seconds,
                transition_direction=transition_direction,
            )
        )

    return ScheduleSpec(
        timezone=timezone,
        step_minutes=step_minutes,
        horizon_hours=horizon_hours,
        keyframes=tuple(keyframes),
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

    def evaluate_at(self, when: datetime) -> ScheduleValues:
        """Evaluate schedule at given datetime."""
        tz = ZoneInfo(self.spec.timezone)
        # Ensure timezone, then read the local wall clock of the schedule's zone
        if when.tzinfo is None:
            when = when.replace(tzinfo=tz)
        local = when.astimezone(tz)

        # Minutes since local midnight. Taken from the wall clock rather than
        # counted from a fixed start, so DST switches need no restart.
        time_of_day = (
            local.hour * 60
            + local.minute
            + (local.second + local.microsecond / 1_000_000) / 60.0
        )

        return self._evaluate_at_minutes(time_of_day)

    def _evaluate_at_minutes(self, time_minutes: float) -> ScheduleValues:
        """Evaluate at minutes since midnight."""
        if not self.spec.keyframes:
            return ScheduleValues(
                kelvin=self.default_kelvin,
                dim=self.default_dim,
                transition_seconds=self.spec.step_minutes * 60,
            )

        # Sort keyframes by time
        sorted_kf = sorted(self.spec.keyframes, key=lambda k: parse_time(k.time))

        # Find previous and next keyframes
        prev_kf = None
        next_kf = None
        before_prev_kf = None  # Keyframe before prev_kf

        for i, kf in enumerate(sorted_kf):
            kf_time = parse_time(kf.time)

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
            prev_kf = (last_kf, parse_time(last_kf.time) - 1440)
            # before_prev would be second-to-last
            if len(sorted_kf) > 1:
                before_last_kf = sorted_kf[-2]
                before_prev_kf = (before_last_kf, parse_time(before_last_kf.time) - 1440)

        if next_kf is None and sorted_kf:
            # After last keyframe - use first from next day
            first_kf = sorted_kf[0]
            next_kf = (first_kf, parse_time(first_kf.time) + 1440)

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
                next_kf_obj, next_time_val
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
        before_values = self._evaluate_at_minutes(transition_start - 0.1)
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
    ) -> ScheduleValues:
        """Evaluate interpolate mode.
        
        Three phases:
        1. BEFORE this keyframe: Interpolate TO it from previous
        2. AT this keyframe: Reached target value
        3. AFTER this keyframe: Check if next is also interpolate
           - If yes: Continue interpolating to next
           - If no: Hold value until next keyframe
        """
        sorted_kf = sorted(self.spec.keyframes, key=lambda k: parse_time(k.time))
        
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
            kf_time = parse_time(kf.time)
            
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
