"""
Tests for the direct light control (light_logic.py, light_control.py):
sending, light types, turn-on, manual changes, pause and resume, groups.

Run from the repository root:
    python -m unittest discover -s tests
"""

import time
import unittest
from datetime import datetime, timedelta

from ha_stubs import BERLIN_TZ, Context, FakeHass

from keyframe_scheduler.const import RESUME_AFTER_MINUTES, RESUME_NEVER, RESUME_NEXT_KEYFRAME
from keyframe_scheduler.light_control import PAUSE_MANUAL, PAUSE_USER, KeyframeLightController
from keyframe_scheduler.light_logic import (
    LightProfile,
    SentCommand,
    group_members,
    is_manual_change,
    lights_from_options,
    plan_command,
    should_resume,
)

CT = {"supported_color_modes": ["color_temp"], "min_color_temp_kelvin": 2700, "max_color_temp_kelvin": 6500}


class FakeCoordinator:
    def __init__(self, data):
        self.data = data
        self._listeners = []
        self.keyframes = []

    def async_add_listener(self, fn):
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn)

    def update(self, data):
        self.data = data
        for fn in list(self._listeners):
            fn()

    def keyframe_times_between(self, start, end):
        return [t for t in self.keyframes if start < t <= end]


class FakeSwitch:
    def __init__(self, is_on=True, pause_reason=None):
        self.is_on = is_on
        self.pause_reason = pause_reason

    def set_state(self, is_on, pause_reason):
        self.is_on, self.pause_reason = is_on, pause_reason


def values(brightness=80.0, kelvin=4000, transition=300):
    return {"brightness": brightness, "kelvin": kelvin, "transition_seconds": transition}


def setup(lights, states=None, **options):
    """Controller started like in HA: states set, switches registered, then
    async_start. The coordinator has no values yet, so the first command a
    test sees is the one it triggers (coordinator data is set afterwards)."""
    hass = FakeHass()
    for entity_id, (state, attributes) in (states or {}).items():
        hass.states.set(entity_id, state, attributes)
    coord = FakeCoordinator(None)
    ctrl = KeyframeLightController(hass, coord, lights, **options)
    switches = {}
    for entity_id in lights:
        switches[entity_id] = FakeSwitch()
        ctrl.register_switch(entity_id, switches[entity_id])
    hass.async_create_task(ctrl.async_start())
    hass.run()
    coord.data = values()
    return hass, coord, ctrl, switches


def turn_on_calls(hass):
    return [c[2] for c in hass.services.calls if c[:2] == ("light", "turn_on")]


class LogicTest(unittest.TestCase):
    PROFILE = LightProfile(max_transition=90, min_interval=30)

    def test_plan_clamps_kelvin_and_transition(self):
        plan = plan_command(self.PROFILE, None, 80, 7000, 300, 0, (2700, 6500))
        self.assertEqual(plan.action, "send")
        self.assertEqual(plan.command.kelvin, 6500)
        self.assertEqual(plan.command.transition, 90)  # DALI limit

    def test_plan_brightness_only_light(self):
        plan = plan_command(self.PROFILE, None, 80, 4000, 10, 0, None)
        self.assertIsNone(plan.command.kelvin)

    def test_plan_waits_within_min_interval_and_skips_unchanged(self):
        last = SentCommand(at=100, brightness_pct=80, kelvin=4000, transition=10)
        self.assertEqual(plan_command(self.PROFILE, last, 80, 4000, 10, 105, (2700, 6500)).action, "skip")
        wait = plan_command(self.PROFILE, last, 70, 4000, 10, 105, (2700, 6500))
        self.assertEqual((wait.action, wait.wait_seconds), ("wait", 25))
        self.assertEqual(plan_command(self.PROFILE, last, 70, 4000, 10, 131, (2700, 6500)).action, "send")
        self.assertEqual(plan_command(self.PROFILE, last, 70, 4000, 10, 105, (2700, 6500), force=True).action, "send")

    def test_manual_change_rules(self):
        last = SentCommand(at=100, brightness_pct=80, kelvin=4000, transition=10)
        base = dict(own_context=False, context_id="x", user_id=None, parent_id=None, last_sent=last,
                    reported_brightness_pct=40, reported_kelvin=4000, now=200, detect_non_ha_changes=True)
        self.assertTrue(is_manual_change(**{**base, "user_id": "u1"}))                 # app / dashboard
        self.assertTrue(is_manual_change(**{**base, "parent_id": "automation"}))       # scene / other automation
        self.assertFalse(is_manual_change(**{**base, "user_id": "u1", "own_context": True}))
        self.assertFalse(is_manual_change(**{**base, "context_id": "picolightnode_restore"}))
        self.assertTrue(is_manual_change(**base))                                      # wall dimmer, clear deviation
        self.assertFalse(is_manual_change(**{**base, "reported_brightness_pct": 78}))   # within tolerance
        self.assertFalse(is_manual_change(**{**base, "now": 115}))                      # still fading
        self.assertFalse(is_manual_change(**{**base, "detect_non_ha_changes": False}))

    def test_resume_rules(self):
        t0 = datetime(2026, 6, 21, 12, 0, tzinfo=BERLIN_TZ)
        self.assertTrue(should_resume(RESUME_AFTER_MINUTES, t0, t0 + timedelta(minutes=30), 30))
        self.assertFalse(should_resume(RESUME_AFTER_MINUTES, t0, t0 + timedelta(minutes=29), 30))
        kf = [t0 + timedelta(hours=2)]
        self.assertTrue(should_resume(RESUME_NEXT_KEYFRAME, t0, t0 + timedelta(hours=3), 0, kf))
        self.assertFalse(should_resume(RESUME_NEXT_KEYFRAME, t0, t0 + timedelta(hours=1), 0, kf))
        self.assertFalse(should_resume(RESUME_NEVER, t0, t0 + timedelta(days=1), 30, kf))

    def test_options_migration(self):
        # Before direct control: follow_lights list + one hardware limit per instance
        self.assertEqual(
            lights_from_options({"follow_lights": ["light.a", "light.b"], "max_transition_seconds": 90}),
            {"light.a": "dali", "light.b": "dali"},
        )
        self.assertEqual(lights_from_options({"follow_lights": ["light.a"]}), {"light.a": "generic"})
        self.assertEqual(lights_from_options({"lights": {"light.a": "zigbee", "light.b": "unknown"}}),
                         {"light.a": "zigbee", "light.b": "generic"})
        self.assertEqual(lights_from_options({}), {})

    def test_group_members_nested(self):
        attrs = {
            "light.room": {"entity_id": ["light.a", "light.sub"]},
            "light.sub": {"entity_id": ["light.b", "light.a"]},
            "light.a": {}, "light.b": {},
        }
        self.assertEqual(group_members("light.room", attrs.get), ("light.a", "light.b"))
        self.assertEqual(group_members("light.single", attrs.get), ("light.single",))


class ControllerTest(unittest.TestCase):
    def test_sends_only_to_lights_that_are_on(self):
        hass, coord, ctrl, _ = setup({"light.on": "dali", "light.off": "zigbee"}, states={"light.on": ("on", CT), "light.off": ("off", CT)})
        coord.update(values(80, 4000, 300))
        hass.run()
        calls = turn_on_calls(hass)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], {"entity_id": "light.on", "brightness_pct": 80.0, "transition": 90.0,
                                    "color_temp_kelvin": 4000})

    def test_min_interval_defers_instead_of_flooding(self):
        hass, coord, ctrl, _ = setup({"light.a": "dali"}, states={"light.a": ("on", CT)})
        coord.update(values(80)); hass.run()
        coord.update(values(70)); hass.run()
        self.assertEqual(len(turn_on_calls(hass)), 1)
        self.assertEqual(len(hass.timers), 1)           # sent once DALI's 30 s are over
        self.assertGreater(hass.timers[0][0], 25)

    def test_turn_on_applies_immediately(self):
        hass, coord, ctrl, _ = setup({"light.a": "zigbee"}, states={"light.a": ("off", CT)})
        hass.change_state("light.a", "on", {**CT, "brightness": 255, "color_temp_kelvin": 2700},
                          context=Context(user_id="wall"))
        hass.run()
        calls = turn_on_calls(hass)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["transition"], 1)     # quick fade to the schedule

    def test_manual_change_pauses_and_off_on_resumes(self):
        hass, coord, ctrl, switches = setup({"light.a": "generic"}, states={"light.a": ("on", CT)})
        coord.update(values(80)); hass.run()
        hass.change_state("light.a", "on", {**CT, "brightness": 50}, context=Context(user_id="app"))
        self.assertFalse(switches["light.a"].is_on)
        self.assertEqual(switches["light.a"].pause_reason, PAUSE_MANUAL)
        coord.update(values(60)); hass.run()
        self.assertEqual(len(turn_on_calls(hass)), 1)   # paused: nothing sent
        hass.change_state("light.a", "off", CT)
        hass.change_state("light.a", "on", CT)
        hass.run()
        self.assertTrue(switches["light.a"].is_on)
        self.assertEqual(len(turn_on_calls(hass)), 2)   # resumed and applied

    def test_own_commands_and_pico_updates_do_not_pause(self):
        hass, coord, ctrl, switches = setup({"light.a": "generic"}, states={"light.a": ("on", CT)})
        coord.update(values(80)); hass.run()
        own = hass.services.calls[-1][3]
        hass.change_state("light.a", "on", {**CT, "brightness": 204}, context=own)
        hass.change_state("light.a", "on", {**CT, "brightness": 100}, context=Context(id="picolightnode_restore_1"))
        self.assertTrue(switches["light.a"].is_on)

    def test_user_pause_is_not_undone_by_off_on(self):
        hass, coord, ctrl, switches = setup({"light.a": "generic"}, states={"light.a": ("on", CT)})
        ctrl.set_following("light.a", False, PAUSE_USER)
        hass.change_state("light.a", "off", CT)
        hass.change_state("light.a", "on", CT)
        hass.run()
        self.assertFalse(switches["light.a"].is_on)
        self.assertEqual(turn_on_calls(hass), [])

    def test_resume_after_minutes_sets_timer(self):
        hass, coord, ctrl, switches = setup({"light.a": "generic"}, states={"light.a": ("on", CT)}, resume_mode=RESUME_AFTER_MINUTES, resume_minutes=20)
        hass.change_state("light.a", "on", {**CT, "brightness": 50}, context=Context(user_id="app"))
        delay, action = hass.timers[-1]
        self.assertEqual(delay, 20 * 60)
        action(None)
        hass.run()
        self.assertTrue(switches["light.a"].is_on)

    def test_resume_at_next_keyframe(self):
        hass, coord, ctrl, switches = setup({"light.a": "generic"}, states={"light.a": ("on", CT)}, resume_mode=RESUME_NEXT_KEYFRAME)
        hass.change_state("light.a", "on", {**CT, "brightness": 50}, context=Context(user_id="app"))
        self.assertFalse(switches["light.a"].is_on)
        coord.keyframes = [datetime.now(BERLIN_TZ) - timedelta(seconds=1)]  # fired after the pause began?
        ctrl.lights["light.a"].paused_since = datetime.now(BERLIN_TZ) - timedelta(minutes=5)
        coord.update(values(60)); hass.run()
        self.assertTrue(switches["light.a"].is_on)

    def test_group_is_expanded_with_member_capabilities(self):
        hass, coord, ctrl, _ = setup({"light.room": "dali"}, states={"light.room": ("on", {"entity_id": ["light.ct", "light.dim"]}), "light.ct": ("on", CT), "light.dim": ("on", {"supported_color_modes": ["brightness"]})})
        coord.update(values(50, 3000, 60)); hass.run()
        calls = {c["entity_id"]: c for c in turn_on_calls(hass)}
        self.assertEqual(set(calls), {"light.ct", "light.dim"})
        self.assertEqual(calls["light.ct"]["color_temp_kelvin"], 3000)
        self.assertNotIn("color_temp_kelvin", calls["light.dim"])

    def test_wall_dimmer_detected_after_fade(self):
        hass, coord, ctrl, switches = setup({"light.a": "generic"}, states={"light.a": ("on", CT)})
        coord.update(values(80, 4000, 0)); hass.run()
        # Pretend the command was sent a minute ago, then the bus reports 30 %
        light = ctrl.lights["light.a"]
        light.last_sent["light.a"] = SentCommand(at=time.monotonic() - 60, brightness_pct=80, kelvin=4000, transition=0)
        hass.change_state("light.a", "on", {**CT, "brightness": 77, "color_temp_kelvin": 4000})
        self.assertEqual(switches["light.a"].pause_reason, PAUSE_MANUAL)


if __name__ == "__main__":
    unittest.main()
