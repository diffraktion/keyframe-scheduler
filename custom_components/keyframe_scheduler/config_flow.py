"""Config flow for Keyframe Scheduler integration."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_DETECT_NON_HA_CHANGES,
    CONF_LIGHTS,
    CONF_RESUME_MINUTES,
    CONF_RESUME_MODE,
    CONF_TYPE_OVERRIDES,
    DEFAULT_LIGHT_TYPE,
    DOMAIN,
    LIGHT_TYPES,
    RESUME_MODES,
    RESUME_NEVER,
)
from .light_logic import lights_from_options
from .scheduler import spec_from_dict

_LOGGER = logging.getLogger(__name__)


class KeyframeSchedulerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Keyframe Scheduler."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Handle the initial step."""
        if user_input is not None:
            title = user_input["name"].strip()
            return self.async_create_entry(title=title, data={}, options={})

        schema = vol.Schema(
            {
                vol.Required("name", default="Living Room"): str,
            }
        )

        return self.async_show_form(step_id="user", data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> KeyframeSchedulerOptionsFlow:
        """Get the options flow for this handler."""
        return KeyframeSchedulerOptionsFlow(config_entry)


class KeyframeSchedulerOptionsFlow(config_entries.OptionsFlow):
    """Options: schedule, lights and their types, behaviour after manual changes.

    Steps: init (schedule, lights, behaviour) -> light_types (one type per
    light) -> type_params (only if "adjust type timings" was ticked).
    """

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self._entry = config_entry
        self._options: Dict[str, Any] = {}
        self._selected_lights: list = []
        self._adjust_types = False

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Schedule JSON, lights and what happens after a manual change."""
        errors = {}
        current = self._entry.options

        if user_input is not None:
            schedule_json = (user_input.get("schedule_json") or "").strip()
            if schedule_json:
                try:
                    if schedule_json.startswith(chr(0xFEFF)):
                        schedule_json = schedule_json[1:]
                    spec_from_dict(json.loads(schedule_json))
                except json.JSONDecodeError as err:
                    _LOGGER.error("JSON decode error at line %s column %s: %s", err.lineno, err.colno, err.msg)
                    errors["schedule_json"] = "invalid_json"
                except Exception as err:
                    _LOGGER.error("Invalid schedule: %s", err)
                    errors["schedule_json"] = "invalid_schedule"

            if not errors:
                self._options = {
                    CONF_RESUME_MODE: user_input.get(CONF_RESUME_MODE, RESUME_NEVER),
                    CONF_RESUME_MINUTES: int(user_input.get(CONF_RESUME_MINUTES) or 0),
                    CONF_DETECT_NON_HA_CHANGES: bool(user_input.get(CONF_DETECT_NON_HA_CHANGES, True)),
                    CONF_TYPE_OVERRIDES: current.get(CONF_TYPE_OVERRIDES) or {},
                }
                if schedule_json:
                    self._options["schedule_json"] = schedule_json
                self._selected_lights = list(user_input.get(CONF_LIGHTS) or [])
                self._adjust_types = bool(user_input.get("adjust_types"))
                if self._selected_lights:
                    return await self.async_step_light_types()
                self._options[CONF_LIGHTS] = {}
                if self._adjust_types:
                    return await self.async_step_type_params()
                return self.async_create_entry(title="", data=self._options)

        schema = vol.Schema(
            {
                vol.Optional(
                    "schedule_json",
                    description={"suggested_value": current.get("schedule_json", "")},
                ): selector.TextSelector(
                    selector.TextSelectorConfig(multiline=True, type=selector.TextSelectorType.TEXT)
                ),
                vol.Optional(
                    CONF_LIGHTS,
                    default=list(lights_from_options(current)),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="light", multiple=True)
                ),
                vol.Required(
                    CONF_RESUME_MODE,
                    default=current.get(CONF_RESUME_MODE, RESUME_NEVER),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=RESUME_MODES,
                        translation_key="resume_mode",
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(
                    CONF_RESUME_MINUTES,
                    default=current.get(CONF_RESUME_MINUTES, 30),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=1440, step=5, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="min"
                    )
                ),
                vol.Required(
                    CONF_DETECT_NON_HA_CHANGES,
                    default=current.get(CONF_DETECT_NON_HA_CHANGES, True),
                ): selector.BooleanSelector(),
                vol.Required("adjust_types", default=False): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)

    async def async_step_light_types(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """One light type (DALI, Zigbee, ...) per assigned light."""
        current = lights_from_options(self._entry.options)

        if user_input is not None:
            self._options[CONF_LIGHTS] = {
                entity_id: user_input.get(entity_id, DEFAULT_LIGHT_TYPE)
                for entity_id in self._selected_lights
            }
            if self._adjust_types:
                return await self.async_step_type_params()
            return self.async_create_entry(title="", data=self._options)

        type_selector = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=list(LIGHT_TYPES),
                translation_key="light_type",
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        )
        schema = vol.Schema(
            {
                vol.Required(entity_id, default=current.get(entity_id, DEFAULT_LIGHT_TYPE)): type_selector
                for entity_id in self._selected_lights
            }
        )
        return self.async_show_form(step_id="light_types", data_schema=schema)

    async def async_step_type_params(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Fade limit and command interval of the light types in use (of all
        types while no light is assigned yet)."""
        used_types = sorted(set(self._options.get(CONF_LIGHTS, {}).values())) or list(LIGHT_TYPES)
        overrides = dict(self._options.get(CONF_TYPE_OVERRIDES) or {})

        if user_input is not None:
            for light_type in used_types:
                overrides[light_type] = {
                    "max_transition": int(user_input[f"{light_type}_max_transition"]),
                    "min_interval": int(user_input[f"{light_type}_min_interval"]),
                }
            self._options[CONF_TYPE_OVERRIDES] = overrides
            return self.async_create_entry(title="", data=self._options)

        fields = {}
        for light_type in used_types:
            values = {**LIGHT_TYPES[light_type], **(overrides.get(light_type) or {})}
            fields[vol.Required(f"{light_type}_max_transition", default=values["max_transition"])] = selector.NumberSelector(
                selector.NumberSelectorConfig(min=0, max=3600, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="s")
            )
            fields[vol.Required(f"{light_type}_min_interval", default=values["min_interval"])] = selector.NumberSelector(
                selector.NumberSelectorConfig(min=1, max=600, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="s")
            )
        return self.async_show_form(step_id="type_params", data_schema=vol.Schema(fields))
