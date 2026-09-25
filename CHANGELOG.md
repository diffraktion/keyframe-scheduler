# Changelog

## Component

### [Unreleased]

#### Changed
- Web app restyled on the LKL UI style guide 1.0.0 (`www/styleguide/`): design tokens, glass/paper skins, day/night/auto theme with switcher in the header
- Chart colours follow the active theme; PDF export always renders the chart on white

---

### [3.0.10] - 2026-03-13

#### Added
- HACS distribution support (`hacs.json`)
- Fixed documentation and issue tracker URLs in `manifest.json`

#### Changed
- Blueprint now ships with version header (`blueprint_version: 1.0`)

---

### [3.0.10] - Initial tracked release

Core features at this version:
- Time-based keyframe interpolation for brightness and color temperature
- Config flow UI for creating and managing scheduler sensors
- Event-based coordinator with DataUpdateCoordinator
- Context-aware PICO internal update detection (prevents false-positive manual override)
- `set_schedule` and `upload_from_file` services
- Bundled blueprint for PICOlightnode follower automation

---

## Blueprint: keyframe_smart_light_follower

### [1.0] - 2026-03-13

Initial versioned release. Corresponds to the blueprint formerly known as "v4.0" in the blueprint name.

Features:
- Applies Keyframe Scheduler sensor outputs to a target light via `light.turn_on`
- Context-aware manual override detection (true user actions vs. PICO internal updates)
- Ignores PICO Smart Restore context (`picolightnode` in context ID)
- Smooth sync on Follow External enable (3s transition)
- Handles missing/unavailable sensors gracefully
- Auto-scaled DIM input (0..100 or 0..1)
- Reads `transition_seconds` from Keyframe Scheduler sensor attributes
- Triggered every 60s and on sensor state change

Requires: Keyframe Scheduler Component v2.6.0+, PICOlightnode v2.0.18+ (for context tracking)
