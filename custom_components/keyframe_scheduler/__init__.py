"""Keyframe Scheduler integration - Event-based coordinator."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from homeassistant.components import frontend
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_ENTRY_ID,
    ATTR_FILE_PATH,
    ATTR_SCHEDULE,
    DEFAULT_DIM,
    DEFAULT_KELVIN,
    DEFAULT_STEP_MINUTES,
    DOMAIN,
    PLATFORMS,
    SERVICE_SET_SCHEDULE,
    SERVICE_UPLOAD_FROM_FILE,
)
from .scheduler import Evaluator, Keyframe, kelvin_to_mired, spec_from_dict
from .store import ScheduleStore

_LOGGER = logging.getLogger(__name__)


class HybridSchedulerCoordinator(DataUpdateCoordinator):
    """Event-based coordinator with hardware limits support."""

    def __init__(
        self,
        hass: HomeAssistant,
        evaluator: Evaluator,
        spec,
        name: str,
        max_transition_seconds: int = 300,
    ):
        """Initialize coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=name,
        )
        
        self.evaluator = evaluator
        self.spec = spec
        self.max_transition_seconds = max_transition_seconds
        self.user_step_seconds = spec.step_minutes * 60
        self._unsub_next_update = None
        
    async def async_setup(self):
        """Setup coordinator after init."""
        await self.async_refresh()
        self._schedule_next_update()
        
    @callback
    def _schedule_next_update(self):
        """Schedule next update based on events."""
        if self._unsub_next_update:
            self._unsub_next_update()
            self._unsub_next_update = None
        
        now = dt_util.now()
        next_time = self._calculate_next_update_time(now)
        
        if next_time:
            delay = (next_time - now).total_seconds()
            
            # Minimum 1 second delay
            if delay < 1:
                delay = 1
            
            _LOGGER.debug(
                "%s: Next update in %.1f seconds at %s",
                self.name,
                delay,
                next_time.strftime("%H:%M:%S")
            )
            
            self._unsub_next_update = async_track_point_in_time(
                self.hass,
                self._handle_scheduled_update,
                next_time
            )
    
    @callback
    async def _handle_scheduled_update(self, _now):
        """Handle scheduled update."""
        await self.async_request_refresh()
        self._schedule_next_update()
    
    def _parse_time(self, time_str: str) -> int:
        """Parse HH:MM to minutes."""
        h, m = map(int, time_str.split(':'))
        return h * 60 + m

    @staticmethod
    def _same_keyframe(a: Keyframe, b: Keyframe) -> bool:
        """Same schedule entry, ignoring the (daily resolved) time."""
        return replace(a, time="") == replace(b, time="")

    def _occurrences(self, now: datetime) -> List[Tuple[Keyframe, datetime]]:
        """Resolved keyframes of today and tomorrow with their local datetimes.

        Sun keyframes and groups make the firing times differ from day to day,
        so every day is resolved on its own (see scheduler.resolve_day).
        """
        local = self.evaluator.local_now(now)
        result = []
        for offset in (0, 1):
            day = local.date() + timedelta(days=offset)
            for kf in self.evaluator.keyframes_for(day):
                h, m = map(int, kf.time.split(':'))
                result.append((kf, datetime(day.year, day.month, day.day, h, m, tzinfo=local.tzinfo)))
        result.sort(key=lambda o: o[1])
        return result

    def _find_keyframe_segment(self, now: datetime):
        """Find previous and next keyframe of today's resolved keyframes."""
        local = self.evaluator.local_now(now)
        today = self.evaluator.keyframes_for(local.date())
        sorted_kf = sorted(today, key=lambda k: self._parse_time(k.time))
        now_minutes = local.hour * 60 + local.minute
        
        prev_kf = None
        next_kf = None
        
        for kf in reversed(sorted_kf):
            kf_minutes = self._parse_time(kf.time)
            if kf_minutes <= now_minutes:
                prev_kf = kf
                break
        
        for kf in sorted_kf:
            kf_minutes = self._parse_time(kf.time)
            if kf_minutes > now_minutes:
                next_kf = kf
                break
        
        # Wrap around midnight (every schedule is a daily profile)
        if not prev_kf and sorted_kf:
            prev_kf = sorted_kf[-1]
        if not next_kf and sorted_kf:
            next_kf = sorted_kf[0]
        
        return prev_kf, next_kf
    
    def _calculate_transition_events(
        self,
        kf,
        kf_dt: datetime,
        now: datetime
    ) -> List[Tuple[str, datetime]]:
        """Calculate transition events with multi-step support."""
        events = []
        
        if kf.mode != "transition":
            return events
        
        total_duration = kf.transition_seconds
        
        # Calculate window
        if kf.transition_direction == "before":
            transition_start = kf_dt - timedelta(seconds=total_duration)
            transition_end = kf_dt
        else:
            transition_start = kf_dt
            transition_end = kf_dt + timedelta(seconds=total_duration)
        
        # Only future events
        if transition_end <= now:
            return events
        
        # Step size: Should be smaller than transition duration for smooth updates
        # Use minimum of: user preference, hardware limit, and half of transition duration
        step_seconds = min(
            self.user_step_seconds,
            self.max_transition_seconds,
            total_duration / 2  # At least 2 updates during transition
        )
        
        # Minimum step: 5 seconds (for very short transitions)
        step_seconds = max(5, step_seconds)
        
        # Generate intermediate steps
        current = max(transition_start, now + timedelta(seconds=1))
        
        # Align to next step boundary
        if current > transition_start:
            elapsed = (current - transition_start).total_seconds()
            steps_passed = int(elapsed / step_seconds)
            current = transition_start + timedelta(seconds=(steps_passed + 1) * step_seconds)
        
        while current < transition_end:
            events.append(("TRANS_STEP", current))
            current += timedelta(seconds=step_seconds)
        
        # Final event at transition end
        if transition_end > now:
            events.append(("TRANS_END", transition_end))
        
        return events
    
    def _calculate_next_update_time(self, now: datetime) -> Optional[datetime]:
        """Calculate next update time."""
        events = []

        occurrences = self._occurrences(now)

        # Transition events (today's occurrence covers a transition that is
        # already running, tomorrow's the next one)
        for kf, kf_dt in occurrences:
            trans_events = self._calculate_transition_events(kf, kf_dt, now)
            events.extend(trans_events)
            
            if kf.mode == "instant":
                if kf_dt > now:
                    events.append(("KEYFRAME", kf_dt))
            
            # BUGFIX: Add interpolate keyframe events
            if kf.mode == "interpolate":
                if kf_dt > now:
                    events.append(("INTERPOLATE_KF", kf_dt))
        
        # Interpolate interval events
        prev_kf, next_kf = self._find_keyframe_segment(now)
        
        # Generate interpolate events if:
        # - Previous is interpolate, OR
        # - Previous is transition and next is interpolate (continue after transition)
        should_interpolate = False
        if prev_kf and next_kf and next_kf.mode == "interpolate":
            if prev_kf.mode == "interpolate":
                should_interpolate = True
            elif prev_kf.mode == "transition":
                # After transition ends, interpolate to next keyframe if it's interpolate mode
                should_interpolate = True
        
        next_kf_dt = None
        if should_interpolate:
            next_kf_dt = next(
                (dt for kf, dt in occurrences if dt > now and self._same_keyframe(kf, next_kf)),
                None,
            )
            should_interpolate = next_kf_dt is not None

        if should_interpolate:

            # CRITICAL: Adjust interval based on hardware limits!
            # If hardware max is less than user's stepMinutes, use hardware max
            # to avoid flickering from interrupted transitions
            user_step_seconds = self.user_step_seconds
            effective_step_seconds = min(user_step_seconds, self.max_transition_seconds)
            
            # OPTIMIZATION: Adaptive step size based on change rate
            # Calculate change rate per minute
            time_to_next_minutes = (next_kf_dt - now).total_seconds() / 60
            kelvin_change_rate = abs(next_kf.kelvin - prev_kf.kelvin) / max(time_to_next_minutes, 1)
            brightness_change_rate = abs(next_kf.dim - prev_kf.dim) / max(time_to_next_minutes, 1)
            
            # If changes are minimal, increase step size (fewer updates)
            # Thresholds are hardware-agnostic
            if kelvin_change_rate < 30 and brightness_change_rate < 0.5:
                # Very slow change - update less frequently
                # Scale based on hardware: use 2-3x the max transition time
                effective_step_seconds = min(effective_step_seconds * 3, self.max_transition_seconds * 2)
            elif kelvin_change_rate < 60 and brightness_change_rate < 1:
                # Moderate change - update moderately
                effective_step_seconds = min(effective_step_seconds * 2, self.max_transition_seconds * 1.5)
            # else: Fast change - use normal step size (hardware max transition)
            
            interval = timedelta(seconds=effective_step_seconds)
            current = now + interval
            
            # Align to interval boundary
            # Use effective step for alignment (not user step!)
            minutes_since_midnight = current.hour * 60 + current.minute
            step_minutes = int(effective_step_seconds // 60)
            if step_minutes < 1:
                step_minutes = 1  # Minimum 1 minute alignment
            
            aligned_minutes = int(((minutes_since_midnight // step_minutes) + 1) * step_minutes)
            
            # Handle day overflow (aligned_minutes >= 1440)
            if aligned_minutes >= 1440:
                # Next day - add timedelta instead of replace
                overflow_minutes = aligned_minutes - 1440
                current = current.replace(hour=0, minute=0, second=0, microsecond=0)
                current = current + timedelta(days=1, minutes=overflow_minutes)
            else:
                current = current.replace(
                    hour=int(aligned_minutes // 60),
                    minute=int(aligned_minutes % 60),
                    second=0,
                    microsecond=0
                )
            
            while current < next_kf_dt:
                events.append(("INTERPOLATE_TICK", current))
                current += interval
        
        # Return earliest event
        if not events:
            return now + timedelta(seconds=self.user_step_seconds)
        
        events.sort(key=lambda e: e[1])
        event_type, event_time = events[0]
        
        _LOGGER.debug("%s: Next event type: %s", self.name, event_type)
        
        return event_time
    
    async def _async_update_data(self) -> Dict[str, Any]:
        """Fetch data."""
        now = dt_util.now()
        
        # Evaluate schedule
        values = self.evaluator.evaluate_at(now)
        
        # Calculate next event
        next_update = self._calculate_next_update_time(now)
        
        if next_update:
            time_to_next = (next_update - now).total_seconds()
            
            # Transition time is the MINIMUM of:
            # 1. Time until next event (avoid interrupts)
            # 2. Schedule's intended transition time
            # 3. Hardware maximum
            transition_seconds = max(
                1,  # Minimum 1 second
                min(
                    time_to_next,              # Don't exceed time to next event
                    values.transition_seconds, # Respect schedule's transition ← ADDED!
                    self.max_transition_seconds # Hardware limit
                )
            )
        else:
            # Fallback if no next event calculated
            transition_seconds = min(
                values.transition_seconds,
                self.max_transition_seconds
            )
        
        kelvin_int = int(round(values.kelvin))

        # Today's firing times, e.g. ["06:00", "19:29 (sunset +30 min)"]
        keyframes_today = []
        for kf in sorted(
            self.evaluator.keyframes_for(self.evaluator.local_now(now).date()),
            key=lambda k: self._parse_time(k.time),
        ):
            label = kf.time
            if kf.trigger == "sun":
                offset = f" {kf.offset_minutes:+d} min" if kf.offset_minutes else ""
                label += f" ({kf.sun_event}{offset})"
            keyframes_today.append(label)

        return {
            "kelvin": kelvin_int,
            "mired": kelvin_to_mired(kelvin_int),
            "brightness": float(values.dim),
            "transition_seconds": int(transition_seconds),
            "next_change": next_update or (now + timedelta(seconds=self.user_step_seconds)),
            "keyframes_today": keyframes_today,
        }
    
    async def async_shutdown(self):
        """Shutdown coordinator."""
        if self._unsub_next_update:
            self._unsub_next_update()
            self._unsub_next_update = None


_PANEL_URL_PATH = "keyframe-scheduler"
_STATIC_URL_PATH = "/keyframe_scheduler"


def _home_location(hass: HomeAssistant) -> Optional[Tuple[float, float]]:
    """Home location configured in HA — used for sun keyframes when the
    schedule file carries no location of its own."""
    if hass.config.latitude is None or hass.config.longitude is None:
        return None
    return (float(hass.config.latitude), float(hass.config.longitude))


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Keyframe Scheduler from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # Register static path for the bundled webapp (once per HA process)
    if not hass.data[DOMAIN].get("static_path_registered"):
        webapp_dir = os.path.join(os.path.dirname(__file__), "www")
        if os.path.isdir(webapp_dir):
            try:
                await hass.http.async_register_static_paths([
                    StaticPathConfig(_STATIC_URL_PATH, webapp_dir, cache_headers=False)
                ])
                hass.data[DOMAIN]["static_path_registered"] = True
            except Exception as err:
                _LOGGER.warning("Could not register static path for webapp: %s", err)

    # Register sidebar panel (once — panels are global, not per entry)
    if not hass.data[DOMAIN].get("panel_registered"):
        try:
            frontend.async_register_built_in_panel(
                hass,
                component_name="iframe",
                sidebar_title="Keyframe Scheduler",
                sidebar_icon="mdi:chart-timeline-variant",
                frontend_url_path=_PANEL_URL_PATH,
                config={"url": f"{_STATIC_URL_PATH}/index.html"},
                require_admin=False,
            )
            hass.data[DOMAIN]["panel_registered"] = True
        except Exception:
            pass

    # Initialize store
    if "store" not in hass.data[DOMAIN]:
        store = ScheduleStore(hass)
        await store.async_load()
        hass.data[DOMAIN]["store"] = store
    else:
        store = hass.data[DOMAIN]["store"]

    # Load schedule
    schedule_dict = await store.async_get(entry.entry_id)
    
    # Check options
    if entry.options.get("schedule_json"):
        try:
            schedule_dict = json.loads(entry.options["schedule_json"])
            await store.async_set(entry.entry_id, schedule_dict)
        except Exception as err:
            _LOGGER.error("Invalid schedule_json in options: %s", err)

    # Default schedule
    if not schedule_dict:
        schedule_dict = {
            "version": 1,
            "timezone": hass.config.time_zone or "Europe/Berlin",
            "stepMinutes": DEFAULT_STEP_MINUTES,
            "horizonHours": 48,
            "keyframes": [],
        }
        await store.async_set(entry.entry_id, schedule_dict)

    # Get hardware limits from options
    max_transition_seconds = entry.options.get("max_transition_seconds", 300)

    # Create evaluator
    try:
        spec = spec_from_dict(schedule_dict, _home_location(hass))
        evaluator = Evaluator(spec, DEFAULT_KELVIN, DEFAULT_DIM)
    except Exception as err:
        _LOGGER.error("Failed to create evaluator: %s", err)
        return False

    # Create hybrid coordinator
    coordinator = HybridSchedulerCoordinator(
        hass,
        evaluator=evaluator,
        spec=spec,
        name=f"{DOMAIN}_{entry.title}",
        max_transition_seconds=max_transition_seconds,
    )

    # Setup coordinator
    await coordinator.async_setup()

    # Store
    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        "evaluator": evaluator,
        "schedule": schedule_dict,
        "title": entry.title,
    }

    # Register service (only once)
    if not hass.services.has_service(DOMAIN, SERVICE_SET_SCHEDULE):

        async def handle_set_schedule(call: ServiceCall) -> None:
            """Handle set_schedule service call."""
            entry_id = call.data.get(ATTR_ENTRY_ID)
            schedule = call.data.get(ATTR_SCHEDULE)

            if not entry_id:
                raise ValueError("entry_id is required")

            # Parse schedule
            if isinstance(schedule, str):
                schedule_obj = json.loads(schedule)
            elif isinstance(schedule, dict):
                schedule_obj = schedule
            else:
                raise ValueError("schedule must be a dict or JSON string")

            # Validate
            try:
                spec = spec_from_dict(schedule_obj, _home_location(hass))
            except Exception as err:
                raise ValueError(f"Invalid schedule: {err}") from err

            # Save
            store = hass.data[DOMAIN]["store"]
            await store.async_set(entry_id, schedule_obj)

            # Update
            if entry_id in hass.data[DOMAIN]:
                # Recreate evaluator
                new_evaluator = Evaluator(spec, DEFAULT_KELVIN, DEFAULT_DIM)
                
                # Get hardware limit
                entry = hass.config_entries.async_get_entry(entry_id)
                max_trans = entry.options.get("max_transition_seconds", 300) if entry else 300
                
                # Recreate coordinator
                new_coordinator = HybridSchedulerCoordinator(
                    hass,
                    evaluator=new_evaluator,
                    spec=spec,
                    name=hass.data[DOMAIN][entry_id]["coordinator"].name,
                    max_transition_seconds=max_trans,
                )
                
                # Shutdown old
                old_coordinator = hass.data[DOMAIN][entry_id]["coordinator"]
                await old_coordinator.async_shutdown()
                
                # Setup new
                await new_coordinator.async_setup()
                
                # Store
                hass.data[DOMAIN][entry_id]["coordinator"] = new_coordinator
                hass.data[DOMAIN][entry_id]["evaluator"] = new_evaluator
                hass.data[DOMAIN][entry_id]["schedule"] = schedule_obj

            _LOGGER.info("Schedule updated for entry %s", entry_id)

        async def handle_upload_from_file(call: ServiceCall) -> None:
            """Handle upload_from_file service call - reads JSON from file system."""
            entry_id = call.data.get(ATTR_ENTRY_ID)
            file_path = call.data.get(ATTR_FILE_PATH)

            if not entry_id:
                raise ValueError("entry_id is required")
            
            if not file_path:
                raise ValueError("file_path is required")

            # Read file
            try:
                _LOGGER.info("Reading schedule from file: %s", file_path)
                
                with open(file_path, 'r', encoding='utf-8') as f:
                    file_content = f.read()
                
                # Remove BOM if present
                if file_content.startswith('\ufeff'):
                    file_content = file_content[1:]
                
                # Parse JSON
                schedule_obj = json.loads(file_content)
                
            except FileNotFoundError:
                raise ValueError(f"File not found: {file_path}") from None
            except PermissionError:
                raise ValueError(f"Permission denied reading file: {file_path}") from None
            except json.JSONDecodeError as err:
                raise ValueError(
                    f"Invalid JSON in file at line {err.lineno}, column {err.colno}: {err.msg}"
                ) from err
            except Exception as err:
                raise ValueError(f"Error reading file: {err}") from err

            # Validate schedule
            try:
                spec = spec_from_dict(schedule_obj, _home_location(hass))
            except Exception as err:
                raise ValueError(f"Invalid schedule format: {err}") from err

            # Save to store
            store = hass.data[DOMAIN]["store"]
            await store.async_set(entry_id, schedule_obj)

            # Update instance (same logic as set_schedule)
            if entry_id in hass.data[DOMAIN]:
                new_evaluator = Evaluator(spec, DEFAULT_KELVIN, DEFAULT_DIM)
                
                entry = hass.config_entries.async_get_entry(entry_id)
                max_trans = entry.options.get("max_transition_seconds", 300) if entry else 300
                
                new_coordinator = HybridSchedulerCoordinator(
                    hass,
                    evaluator=new_evaluator,
                    spec=spec,
                    name=hass.data[DOMAIN][entry_id]["coordinator"].name,
                    max_transition_seconds=max_trans,
                )
                
                old_coordinator = hass.data[DOMAIN][entry_id]["coordinator"]
                await old_coordinator.async_shutdown()
                
                await new_coordinator.async_setup()
                
                hass.data[DOMAIN][entry_id]["coordinator"] = new_coordinator
                hass.data[DOMAIN][entry_id]["evaluator"] = new_evaluator
                hass.data[DOMAIN][entry_id]["schedule"] = schedule_obj

            _LOGGER.info("Schedule uploaded from file %s for entry %s", file_path, entry_id)

        hass.services.async_register(DOMAIN, SERVICE_SET_SCHEDULE, handle_set_schedule)
        hass.services.async_register(DOMAIN, SERVICE_UPLOAD_FROM_FILE, handle_upload_from_file)

    # Reload when options change (e.g. follow_lights list updated)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # Forward to platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload integration when options are changed."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Shutdown coordinator
    if entry.entry_id in hass.data[DOMAIN]:
        coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
        await coordinator.async_shutdown()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

        # Remove panel when the last config entry is removed
        _meta_keys = {"store", "static_path_registered", "panel_registered"}
        remaining_entries = [k for k in hass.data[DOMAIN] if k not in _meta_keys]
        if not remaining_entries and hass.data[DOMAIN].pop("panel_registered", False):
            try:
                frontend.async_remove_panel(hass, _PANEL_URL_PATH)
            except Exception:
                pass

    return unload_ok
