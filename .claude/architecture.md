# Keyframe Scheduler - Architecture

## Overview

```
Webapp (www/)                         Integration (custom_components/keyframe_scheduler)
─────────────                         ─────────────────────────────────────────────────
design schedule, simulate year/day    options: schedule JSON, lights + types, resume mode
export JSON  ───────────────────────► spec_from_dict ─► ScheduleSpec
                                          │
                                          ▼
                                      resolve_day(spec, day)      (scheduler.py)
                                          │  sun events (astro.py), offset, bounds,
                                          │  fallback, group rules → concrete HH:MM
                                          ▼
                                      Evaluator.evaluate_at(now) → brightness, kelvin, transition
                                          │
                                          ▼
                                      HybridSchedulerCoordinator  (__init__.py)
                                          │  event-based updates (keyframes, transitions,
                                          │  interpolation ticks) from today's/tomorrow's
                                          │  resolved keyframes
                              ┌───────────┴────────────┐
                              ▼                        ▼
                          sensors (sensor.py)      KeyframeLightController (light_control.py)
                                                       │ per light: type profile, follow switch,
                                                       │ last command, own contexts
                                                       ▼
                                                   light.turn_on (only lights that are ON)
```

## Modules

| Module | Responsibility |
|--------|----------------|
| `astro.py` | Sun altitude and events per local day (port of `www/js/astro.js`) |
| `scheduler.py` | `ScheduleSpec`, `spec_from_dict`, `resolve_day`, `Evaluator` (instant / transition / interpolate) |
| `__init__.py` | Setup, coordinator (update timing), services, options → controller |
| `light_logic.py` | Pure decisions: `plan_command`, `is_manual_change`, `should_resume`, group expansion, options migration |
| `light_control.py` | HA glue: sends commands, watches light state changes, pause/resume, timers |
| `switch.py` | Per-light follow switch (state + `pause_reason`, restored after restart) |
| `sensor.py` | Target kelvin / brightness / mired / next change, attribute `keyframes_today` |
| `config_flow.py` | Instance setup; options: schedule, lights, light types, type timings, resume mode |

## Light control

- **Sending:** on each coordinator update, for every light that follows and
  every group member that is ON: `plan_command` decides send / wait (min
  interval of the light type) / skip (unchanged). Fade = min(schedule
  transition, type max_transition). Brightness only for lights without
  colour temperature; kelvin clamped to the light's range.
- **Turn-on:** OFF→ON of a member → apply at once (1 s fade); a manual pause
  of that light ends.
- **Manual change:** state change of an ON member whose context is not ours:
  user or parent context → manual; no user/parent (device report) → manual
  only if it deviates from the last command after its fade
  (`detect_non_ha_changes`). PICO contexts are ignored.
- **Resume:** off/on always; plus `minutes` (timer) or `next_keyframe`
  (coordinator `keyframe_times_between`). A follow switch turned off by the
  user (`pause_reason: user`) is not resumed automatically.

## Webapp

Single `index.html` (LKL UI style guide in `www/styleguide/`), `www/js/astro.js`.
Year view (heatmap: artificial brightness / colour temperature / daylight),
day view (Chart.js), conflict detection (keyframes swapping order), keyframe
groups, bounds advice. Export format = the integration's schedule JSON.
