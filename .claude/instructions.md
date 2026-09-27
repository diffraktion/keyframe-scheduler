# Keyframe Scheduler Development Instructions

## Project Type
Home Assistant custom integration plus a bundled webapp: time-based light
control with keyframes (brightness, colour temperature), triggered by clock
time or sun events.

## What the integration does
- Resolves each day's keyframes (fixed times, sun events with offset and
  not-before/not-after bounds, keyframe groups) and interpolates between them
- **Controls the assigned lights itself** (like Adaptive Lighting) — there is
  no blueprint any more
- Publishes the target values as sensors (for display and custom automations)

## Key principles

### 1. One instance = one schedule, many lights
Lights of different buses (DALI, Zigbee, Casambi, ...) share one instance;
each light has a type that limits fade length and command rate.

### 2. Never switch a light on
Only lights that are ON and follow the schedule get commands. Turning a light
on (app, wall switch, presence) applies the current values at once.

### 3. Respect manual changes
A change made by someone else pauses the light (its follow switch turns off).
Off/on always resumes; optionally after N minutes or at the next keyframe.
PICOlightnode internal updates (context id contains `picolightnode`) and the
integration's own commands are never manual changes.

### 4. Webapp simulation and integration compute the same thing
Sun model, daily resolution, group rules and evaluation exist twice — in
JavaScript (`www/js/astro.js`, `www/index.html`) and in Python (`astro.py`,
`scheduler.py`). Change both together and keep the reference values in the
tests identical.

## Code style
- Type hints, small functions, comments that explain *why*
- HA-independent logic in plain modules (`astro.py`, `scheduler.py`,
  `light_logic.py`) so it can be unit-tested without Home Assistant
- Webapp: LKL UI style guide tokens only (`--lkl-*`), no colour literals in
  component CSS

## Before any commit
1. `python -m unittest discover -s tests` (no Home Assistant needed)
2. Webapp: load `www/index.html` and check the console for errors
3. Midnight and DST: covered by tests — add a case when touching the logic
