"""Sensor platform for Keyframe Scheduler."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


@dataclass(frozen=True, kw_only=True)
class KeyframeSchedulerSensorDescription(SensorEntityDescription):
    """Describe Keyframe Scheduler sensor entity."""

    key: str


SENSOR_TYPES: tuple[KeyframeSchedulerSensorDescription, ...] = (
    KeyframeSchedulerSensorDescription(
        key="kelvin",
        name="Target Kelvin",
        icon="mdi:thermometer",
        native_unit_of_measurement=UnitOfTemperature.KELVIN,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    KeyframeSchedulerSensorDescription(
        key="mired",
        name="Target Mired",
        icon="mdi:thermometer",
        native_unit_of_measurement="mired",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    KeyframeSchedulerSensorDescription(
        key="brightness",
        name="Target Brightness",
        icon="mdi:brightness-percent",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    KeyframeSchedulerSensorDescription(
        key="next_change",
        name="Next Change",
        icon="mdi:clock-outline",
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Keyframe Scheduler sensors."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    
    entities = [
        KeyframeSchedulerSensor(coordinator, entry, description)
        for description in SENSOR_TYPES
    ]
    
    async_add_entities(entities)


class KeyframeSchedulerSensor(CoordinatorEntity, SensorEntity):
    """Keyframe Scheduler sensor."""

    _attr_has_entity_name = True
    entity_description: KeyframeSchedulerSensorDescription

    def __init__(
        self,
        coordinator,
        entry: ConfigEntry,
        description: KeyframeSchedulerSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Keyframe Scheduler",
            model="Light Schedule Controller",
            sw_version="2.5.0",
        )

    @property
    def native_value(self) -> Any:
        """Return the state of the sensor."""
        value = self.coordinator.data.get(self.entity_description.key)
        
        if value is None:
            return None

        # Format numeric values
        if self.entity_description.key in ("kelvin", "mired"):
            return int(round(float(value)))
        
        if self.entity_description.key == "brightness":
            return float(value)

        return value

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return additional attributes."""
        return {
            "entry_id": self._entry.entry_id,
            "instance_name": self._entry.title,
            "transition_seconds": self.coordinator.data.get("transition_seconds"),
            # When each keyframe fires today (sun keyframes and groups vary by day)
            "keyframes_today": self.coordinator.data.get("keyframes_today"),
        }

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success and self.coordinator.data is not None
