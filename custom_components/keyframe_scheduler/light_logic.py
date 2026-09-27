"""
Keyframe Scheduler - light control decisions.

Pure logic without Home Assistant imports, so it can be tested on its own.
The HA side (light_control.py) gathers the facts — light state, context of
a change, time — and asks these functions what to do:

- plan_command:       send a command to a light now, later, or not at all?
- is_manual_change:   was a reported change made by someone else?
- should_resume:      may a manually paused light follow again?
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

try:
    from .const import (
        BRIGHTNESS_TOLERANCE_PCT,
        CONF_FOLLOW_LIGHTS,
        CONF_LIGHTS,
        DEFAULT_LIGHT_TYPE,
        KELVIN_TOLERANCE,
        LIGHT_TYPES,
        RESUME_AFTER_MINUTES,
        RESUME_NEXT_KEYFRAME,
        TRANSITION_GRACE_SECONDS,
    )
except ImportError:  # loaded outside the package (tests)
    from const import (  # type: ignore[no-redef]
        BRIGHTNESS_TOLERANCE_PCT,
        CONF_FOLLOW_LIGHTS,
        CONF_LIGHTS,
        DEFAULT_LIGHT_TYPE,
        KELVIN_TOLERANCE,
        LIGHT_TYPES,
        RESUME_AFTER_MINUTES,
        RESUME_NEXT_KEYFRAME,
        TRANSITION_GRACE_SECONDS,
    )

# Context ids of PICOlightnode's internal updates (Smart Restore, MQTT sync)
# — never a manual change.
PICO_CONTEXT_MARKER = "picolightnode"


@dataclass(frozen=True)
class LightProfile:
    """Timing limits of a light type."""
    max_transition: float
    min_interval: float

    @classmethod
    def for_type(cls, light_type: str, overrides: Optional[Mapping[str, Mapping[str, Any]]] = None) -> "LightProfile":
        base = dict(LIGHT_TYPES.get(light_type, LIGHT_TYPES[DEFAULT_LIGHT_TYPE]))
        base.update({k: v for k, v in ((overrides or {}).get(light_type) or {}).items() if v is not None})
        return cls(max_transition=float(base["max_transition"]), min_interval=float(base["min_interval"]))


@dataclass(frozen=True)
class SentCommand:
    """What was last sent to a light (monotonic time in seconds)."""
    at: float
    brightness_pct: float
    kelvin: Optional[int]
    transition: float


@dataclass(frozen=True)
class Command:
    brightness_pct: float
    kelvin: Optional[int]
    transition: float


@dataclass(frozen=True)
class Plan:
    """Result of plan_command: action is 'send', 'wait' or 'skip'."""
    action: str
    command: Optional[Command] = None
    wait_seconds: float = 0.0


def plan_command(
    profile: LightProfile,
    last_sent: Optional[SentCommand],
    brightness_pct: float,
    kelvin: float,
    transition: float,
    now: float,
    kelvin_range: Optional[Tuple[float, float]],
    force: bool = False,
) -> Plan:
    """Decide what to send to one light that is on and following.

    - kelvin_range None: the light has no colour temperature -> brightness only
    - the fade never exceeds the light type's max_transition
    - within min_interval of the last command: wait (unless forced)
    - nothing changed since the last command: skip (unless forced)
    """
    k: Optional[int] = None
    if kelvin_range is not None:
        lo, hi = kelvin_range
        k = int(round(min(max(kelvin, lo), hi)))
    bri = round(max(0.0, min(100.0, brightness_pct)), 1)
    command = Command(brightness_pct=bri, kelvin=k, transition=max(0.0, min(transition, profile.max_transition)))

    if force or last_sent is None:
        return Plan("send", command)
    if abs(last_sent.brightness_pct - bri) < 0.5 and (k is None or last_sent.kelvin is None or abs(last_sent.kelvin - k) < 10):
        return Plan("skip")
    elapsed = now - last_sent.at
    if elapsed < profile.min_interval:
        return Plan("wait", wait_seconds=profile.min_interval - elapsed)
    return Plan("send", command)


def is_manual_change(
    *,
    own_context: bool,
    context_id: str,
    user_id: Optional[str],
    parent_id: Optional[str],
    last_sent: Optional[SentCommand],
    reported_brightness_pct: Optional[float],
    reported_kelvin: Optional[float],
    now: float,
    detect_non_ha_changes: bool,
) -> bool:
    """Was a change of an ON light made by someone other than the scheduler?

    - our own commands and PICOlightnode's internal updates: no
    - a command from a user or another automation/script/scene (the context
      has a user or a parent): yes
    - a change the light reports by itself (no user, no parent — e.g. a
      dimmer on the bus): yes if it clearly differs from what was sent and
      the last fade is over — only with detect_non_ha_changes
    """
    if own_context or PICO_CONTEXT_MARKER in (context_id or ""):
        return False
    if user_id is not None or parent_id is not None:
        return True
    if not detect_non_ha_changes or last_sent is None:
        return False
    if now < last_sent.at + last_sent.transition + TRANSITION_GRACE_SECONDS:
        return False  # still fading towards our target
    if reported_brightness_pct is not None and abs(reported_brightness_pct - last_sent.brightness_pct) > BRIGHTNESS_TOLERANCE_PCT:
        return True
    if reported_kelvin is not None and last_sent.kelvin is not None and abs(reported_kelvin - last_sent.kelvin) > KELVIN_TOLERANCE:
        return True
    return False


def should_resume(
    mode: str,
    paused_since: datetime,
    now: datetime,
    resume_minutes: float,
    keyframe_times: Iterable[datetime] = (),
) -> bool:
    """May a light that was paused by a manual change follow again?

    (Switching the light off and on again always resumes — that is handled
    where the turn-on is seen, not here.)
    """
    if mode == RESUME_AFTER_MINUTES:
        return resume_minutes > 0 and now - paused_since >= timedelta(minutes=resume_minutes)
    if mode == RESUME_NEXT_KEYFRAME:
        return any(paused_since < t <= now for t in keyframe_times)
    return False


def brightness_pct_from_state(attributes: Mapping[str, Any]) -> Optional[float]:
    """HA brightness 0-255 -> percent (None if not reported)."""
    value = attributes.get("brightness")
    return None if value is None else round(float(value) / 255 * 100, 1)


def kelvin_range_from_state(attributes: Mapping[str, Any]) -> Optional[Tuple[float, float]]:
    """(min, max) Kelvin if the light supports colour temperature, else None."""
    modes = attributes.get("supported_color_modes") or []
    if "color_temp" not in modes:
        return None
    lo = attributes.get("min_color_temp_kelvin") or 2000
    hi = attributes.get("max_color_temp_kelvin") or 6500
    return (float(lo), float(hi))


def group_members(entity_id: str, get_attributes, depth: int = 0) -> Tuple[str, ...]:
    """Expand a light group into its member lights (nested groups too).

    get_attributes(entity_id) returns the entity's attributes or None.
    """
    attrs: Optional[Dict[str, Any]] = get_attributes(entity_id)
    members = (attrs or {}).get("entity_id")
    if depth > 5 or not isinstance(members, (list, tuple)) or not members:
        return (entity_id,)
    result = []
    for member in members:
        if isinstance(member, str) and member.startswith("light."):
            for m in group_members(member, get_attributes, depth + 1):
                if m not in result:
                    result.append(m)
    return tuple(result) or (entity_id,)


def lights_from_options(options: Mapping[str, Any]) -> Dict[str, str]:
    """{light entity_id: light type} from the instance options.

    Migrates the options from before direct control: a plain follow_lights
    list plus one hardware limit (max_transition_seconds) per instance ->
    every light gets the type with that limit.
    """
    lights = options.get(CONF_LIGHTS)
    if isinstance(lights, dict):
        return {e: (t if t in LIGHT_TYPES else DEFAULT_LIGHT_TYPE) for e, t in lights.items()}
    legacy_max = options.get("max_transition_seconds")
    legacy_type = next(
        (t for t, p in LIGHT_TYPES.items() if p["max_transition"] == legacy_max),
        DEFAULT_LIGHT_TYPE,
    )
    return {e: legacy_type for e in options.get(CONF_FOLLOW_LIGHTS) or []}
