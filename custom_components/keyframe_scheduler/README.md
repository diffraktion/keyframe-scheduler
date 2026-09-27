# Keyframe Scheduler

Time-based light control with keyframes (clock time or sun events) for Home Assistant. The integration controls the assigned lights itself — no blueprint or automation needed.

Documentation: [English](../../README.md) · [Deutsch](../../README.de.md) · [Español](../../README.es.md)

## Files

| File | Purpose |
|------|---------|
| `__init__.py` | Setup, coordinator (update timing), services |
| `scheduler.py` | Schedule model, daily resolution (sun triggers, bounds, groups), evaluation |
| `astro.py` | Sun position and events — same model as the webapp's `www/js/astro.js` |
| `light_logic.py` | Light control decisions (what to send, manual changes, resuming) — no HA imports |
| `light_control.py` | Drives the lights: commands, turn-on, pause/resume |
| `sensor.py` / `switch.py` | Target value sensors, per-light follow switches |
| `config_flow.py` | Instance setup and options (schedule, lights, light types, resume mode) |
| `www/` | Webapp (sidebar panel) |

Tests (no Home Assistant needed): `python -m unittest discover -s tests` from the repository root.
