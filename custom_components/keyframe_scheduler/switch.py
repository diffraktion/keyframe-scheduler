"""Switch platform for Keyframe Scheduler - per-light follow switches."""

from __future__ import annotations

import logging
from typing import Any, Optional

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .light_control import PAUSE_MANUAL, PAUSE_USER

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one follow switch per light the instance controls."""
    controller = hass.data[DOMAIN][entry.entry_id]["controller"]
    entities = [
        KeyframeFollowSwitch(entry, controller, light_entity_id)
        for light_entity_id in controller.lights
    ]
    if entities:
        async_add_entities(entities)


class KeyframeFollowSwitch(SwitchEntity, RestoreEntity):
    """Whether one light follows the schedule of this instance.

    ON: the integration sends the schedule to the light (while it is on).
    OFF: the light is paused — either turned off here on purpose
    (pause_reason "user", stays off until turned on again) or by a detected
    manual change (pause_reason "manual", resumes when the light is switched
    off and on, and depending on the instance option after some minutes or
    at the next keyframe). State and reason survive HA restarts.

    Entity ID pattern: switch.keyframe_{instance}_{light_slug}_follow
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:lightbulb-auto"

    def __init__(self, entry: ConfigEntry, controller, light_entity_id: str) -> None:
        """Initialize the follow switch."""
        self._entry = entry
        self._controller = controller
        self._light_entity_id = light_entity_id

        # "light.besprechungsraum_1" → "besprechungsraum_1"
        light_slug = light_entity_id.split(".", 1)[-1]

        self._attr_unique_id = f"{entry.entry_id}_{light_entity_id}_follow"
        self._attr_name = f"{light_slug} follow"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Keyframe Scheduler",
            model="Light Schedule Controller",
        )
        self._is_on: bool = True  # Default: following enabled
        self._pause_reason: Optional[str] = None

    async def async_added_to_hass(self) -> None:
        """Restore state after HA restart, then hand it to the controller."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None:
            self._is_on = last_state.state == "on"
            reason = last_state.attributes.get("pause_reason")
            self._pause_reason = None if self._is_on else (reason if reason in (PAUSE_MANUAL, PAUSE_USER) else PAUSE_USER)
            _LOGGER.debug("Restored follow switch %s → %s (%s)", self._light_entity_id, last_state.state, self._pause_reason)
        self._controller.register_switch(self._light_entity_id, self)

    @property
    def is_on(self) -> bool:
        """Return True if the light is following the scheduler."""
        return self._is_on

    @property
    def pause_reason(self) -> Optional[str]:
        return self._pause_reason

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose which light this switch controls and how."""
        light = self._controller.lights.get(self._light_entity_id)
        return {
            "light_entity_id": self._light_entity_id,
            "instance": self._entry.title,
            "light_type": light.light_type if light else None,
            "members": list(light.members) if light else [],
            "pause_reason": self._pause_reason,
        }

    def set_state(self, is_on: bool, pause_reason: Optional[str]) -> None:
        """Called by the controller (manual change detected, resume, service)."""
        self._is_on = is_on
        self._pause_reason = None if is_on else pause_reason
        if self.hass is not None:
            self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Follow the schedule again (sends the current values right away)."""
        self._controller.set_following(self._light_entity_id, True, None)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Pause on purpose — stays paused until turned on again."""
        self._controller.set_following(self._light_entity_id, False, PAUSE_USER)
