"""
Keyframe Scheduler - direct light control.

The integration drives its lights itself (no blueprint automation):

- Every schedule update is sent to all assigned lights that are ON and
  follow the schedule. The integration never switches a light on.
- When a light is switched on (app, wall switch, presence ...) it gets the
  current values at once.
- A change made by someone else pauses the light (its follow switch turns
  off). Switching the light off and on again always resumes; depending on
  the instance option also after some minutes or at the next keyframe.
- Every light has a type (DALI, Zigbee, ...) that limits how long one fade
  may be and how often commands may be sent.

The decisions themselves live in light_logic.py.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from datetime import datetime
from typing import Any, Callable, Deque, Dict, List, Optional

from homeassistant.core import Context, Event, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_LIGHT_TYPE,
    RESUME_AFTER_MINUTES,
    RESUME_NEVER,
    RESUME_NEXT_KEYFRAME,
    TURN_ON_TRANSITION_SECONDS,
)
from .light_logic import (
    LightProfile,
    SentCommand,
    brightness_pct_from_state,
    group_members,
    is_manual_change,
    kelvin_range_from_state,
    plan_command,
    should_resume,
)

_LOGGER = logging.getLogger(__name__)

PAUSE_MANUAL = "manual"  # paused by a detected manual change
PAUSE_USER = "user"      # follow switch turned off on purpose


class ControlledLight:
    """One assigned light (or light group) of an instance."""

    def __init__(self, entity_id: str, light_type: str, profile: LightProfile) -> None:
        self.entity_id = entity_id
        self.light_type = light_type
        self.profile = profile
        self.members: tuple = (entity_id,)
        # Follow state, mirrored by the follow switch (which persists it)
        self.following = True
        self.pause_reason: Optional[str] = None
        self.paused_since: Optional[datetime] = None
        # Per member: what was sent last; our own context ids
        self.last_sent: Dict[str, SentCommand] = {}
        self.own_contexts: Deque[str] = deque(maxlen=50)
        self.switch: Any = None  # KeyframeFollowSwitch, set when it is added
        self._deferred: Dict[str, Callable[[], None]] = {}
        self._resume_timer: Optional[Callable[[], None]] = None

    def cancel_timers(self) -> None:
        for cancel in self._deferred.values():
            cancel()
        self._deferred.clear()
        if self._resume_timer:
            self._resume_timer()
            self._resume_timer = None


class KeyframeLightController:
    """Drives the lights of one Keyframe Scheduler instance."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator,
        lights: Dict[str, str],
        type_overrides: Optional[Dict[str, Dict[str, Any]]] = None,
        resume_mode: str = RESUME_NEVER,
        resume_minutes: float = 0,
        detect_non_ha_changes: bool = True,
    ) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.resume_mode = resume_mode
        self.resume_minutes = resume_minutes
        self.detect_non_ha_changes = detect_non_ha_changes
        self.lights: Dict[str, ControlledLight] = {
            entity_id: ControlledLight(
                entity_id,
                light_type,
                LightProfile.for_type(light_type or DEFAULT_LIGHT_TYPE, type_overrides),
            )
            for entity_id, light_type in lights.items()
        }
        self._by_member: Dict[str, ControlledLight] = {}
        self._unsub: List[Callable[[], None]] = []
        self._unsub_state: Optional[Callable[[], None]] = None

    # ---- setup ------------------------------------------------------------

    @property
    def max_transition(self) -> Optional[float]:
        """Shortest fade limit among the assigned lights (drives the tick rate)."""
        return min((l.profile.max_transition for l in self.lights.values()), default=None)

    async def async_start(self) -> None:
        self._refresh_members()
        self._unsub.append(self.coordinator.async_add_listener(self._on_schedule_update))
        # Group members can appear late during startup: refresh once HA runs
        self._unsub.append(self.hass.bus.async_listen_once("homeassistant_started", self._on_started))
        # Lights that are already on get the current values right away
        self.hass.async_create_task(self.async_apply())

    async def async_stop(self) -> None:
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        for light in self.lights.values():
            light.cancel_timers()

    @callback
    def _on_started(self, _event: Event) -> None:
        self._refresh_members()
        self.hass.async_create_task(self.async_apply(force=True))

    def _refresh_members(self) -> None:
        """Expand groups and (re)subscribe to state changes of all members."""
        def attrs(entity_id: str):
            state = self.hass.states.get(entity_id)
            return dict(state.attributes) if state else None

        self._by_member = {}
        for light in self.lights.values():
            light.members = group_members(light.entity_id, attrs)
            for member in light.members:
                self._by_member[member] = light
            # A group entity itself also reports changes — watch it too
            self._by_member.setdefault(light.entity_id, light)
        if self._unsub_state:
            self._unsub_state()
        self._unsub_state = async_track_state_change_event(
            self.hass, list(self._by_member), self._on_light_state_change
        )

    def register_switch(self, entity_id: str, switch) -> None:
        light = self.lights.get(entity_id)
        if light is None:
            return
        light.switch = switch
        light.following = switch.is_on
        light.pause_reason = switch.pause_reason
        if light.pause_reason == PAUSE_MANUAL:
            # Paused before a restart: count the pause from now on
            self._pause(light)

    # ---- sending ------------------------------------------------------------

    @callback
    def _on_schedule_update(self) -> None:
        self._check_resume_next_keyframe()
        self.hass.async_create_task(self.async_apply())

    async def async_apply(self, force: bool = False, only: Optional[List[str]] = None,
                          transition: Optional[float] = None) -> None:
        """Send the current schedule values to the lights that need them."""
        data = self.coordinator.data
        if not data:
            return
        for light in self.lights.values():
            if only is not None and light.entity_id not in only:
                continue
            if not light.following:
                continue
            for member in light.members:
                await self._apply_member(light, member, data, force, transition)

    async def _apply_member(self, light: ControlledLight, member: str, data: Dict[str, Any],
                            force: bool, transition: Optional[float]) -> None:
        state = self.hass.states.get(member)
        if state is None or state.state != "on":
            return  # never switch a light on
        plan = plan_command(
            light.profile,
            light.last_sent.get(member),
            float(data["brightness"]),
            float(data["kelvin"]),
            float(data["transition_seconds"] if transition is None else transition),
            time.monotonic(),
            kelvin_range_from_state(state.attributes),
            force=force,
        )
        if plan.action == "skip":
            return
        if plan.action == "wait":
            self._defer(light, member, plan.wait_seconds)
            return

        command = plan.command
        service_data: Dict[str, Any] = {
            "entity_id": member,
            "brightness_pct": command.brightness_pct,
            "transition": command.transition,
        }
        if command.kelvin is not None:
            service_data["color_temp_kelvin"] = command.kelvin
        context = Context()
        light.own_contexts.append(context.id)
        light.last_sent[member] = SentCommand(
            at=time.monotonic(),
            brightness_pct=command.brightness_pct,
            kelvin=command.kelvin,
            transition=command.transition,
        )
        cancel = light._deferred.pop(member, None)
        if cancel:
            cancel()
        _LOGGER.debug("%s: %s", member, service_data)
        await self.hass.services.async_call("light", "turn_on", service_data, blocking=False, context=context)

    def _defer(self, light: ControlledLight, member: str, seconds: float) -> None:
        """Send the (then current) values once the light's min interval is over."""
        if member in light._deferred:
            return

        @callback
        def _later(_now) -> None:
            light._deferred.pop(member, None)
            if light.following and self.coordinator.data:
                self.hass.async_create_task(
                    self._apply_member(light, member, self.coordinator.data, False, None)
                )

        light._deferred[member] = async_call_later(self.hass, max(1.0, seconds), _later)

    # ---- watching the lights -------------------------------------------------

    @callback
    def _on_light_state_change(self, event: Event) -> None:
        entity_id = event.data.get("entity_id")
        light = self._by_member.get(entity_id)
        new = event.data.get("new_state")
        old = event.data.get("old_state")
        if light is None or new is None:
            return

        if new.state != "on":
            light.last_sent.pop(entity_id, None)  # next turn-on gets a fresh command
            return

        if old is None or old.state != "on":
            # Switched on (app, wall switch, presence, power back ...):
            # off/on always ends a manual pause, then apply at once.
            if light.pause_reason == PAUSE_MANUAL:
                self._resume(light)
            elif light.following:
                self.hass.async_create_task(
                    self.async_apply(force=True, only=[light.entity_id], transition=TURN_ON_TRANSITION_SECONDS)
                )
            return

        if not light.following:
            return
        context = new.context
        if is_manual_change(
            own_context=context.id in light.own_contexts,
            context_id=context.id,
            user_id=context.user_id,
            parent_id=context.parent_id,
            last_sent=light.last_sent.get(entity_id),
            reported_brightness_pct=brightness_pct_from_state(new.attributes),
            reported_kelvin=new.attributes.get("color_temp_kelvin"),
            now=time.monotonic(),
            detect_non_ha_changes=self.detect_non_ha_changes,
        ):
            _LOGGER.info("%s: manual change detected, pausing %s", entity_id, light.entity_id)
            self._pause(light)

    # ---- pause / resume ------------------------------------------------------

    def _pause(self, light: ControlledLight) -> None:
        light.cancel_timers()
        light.following = False
        light.pause_reason = PAUSE_MANUAL
        light.paused_since = dt_util.now()
        if light.switch is not None:
            light.switch.set_state(False, PAUSE_MANUAL)
        if self.resume_mode == RESUME_AFTER_MINUTES and self.resume_minutes > 0:
            @callback
            def _resume_later(_now) -> None:
                light._resume_timer = None
                if light.pause_reason == PAUSE_MANUAL:
                    self._resume(light)

            light._resume_timer = async_call_later(self.hass, self.resume_minutes * 60, _resume_later)

    def _resume(self, light: ControlledLight) -> None:
        self.set_following(light.entity_id, True, None)

    def set_following(self, entity_id: str, following: bool, reason: Optional[str]) -> None:
        """Follow switch, service or resume changed whether a light follows."""
        light = self.lights.get(entity_id)
        if light is None:
            return
        light.following = following
        light.pause_reason = None if following else reason
        light.paused_since = None if following else dt_util.now()
        if following:
            light.cancel_timers()
            light.last_sent.clear()
            self.hass.async_create_task(
                self.async_apply(force=True, only=[entity_id], transition=TURN_ON_TRANSITION_SECONDS)
            )
        if light.switch is not None:
            light.switch.set_state(following, light.pause_reason)

    def _check_resume_next_keyframe(self) -> None:
        if self.resume_mode != RESUME_NEXT_KEYFRAME:
            return
        now = dt_util.now()
        for light in self.lights.values():
            if light.pause_reason != PAUSE_MANUAL or light.paused_since is None:
                continue
            times = self.coordinator.keyframe_times_between(light.paused_since, now)
            if should_resume(RESUME_NEXT_KEYFRAME, light.paused_since, now, 0, times):
                self._resume(light)
