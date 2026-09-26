# Changelog

## Component

### [Unreleased]

#### Added
- Web app: sun-based keyframes (dawn/dusk twilights, sunrise/sunset, golden hour, solar noon) with offset and optional not-before/not-after bounds
- Web app: location setting (latitude, longitude, time zone), prefilled from Home Assistant when opened as panel
- Web app: year overview heatmap — artificial light brightness, artificial light colour temperature and potential clear-sky daylight (log lux scale) — with sun curves; click a day to open it in the day view
- Web app: day view with date navigation, sun times and night/twilight shading
- Web app: solar midnight as sun trigger (solar noon relabelled "Sonnenmittag")
- Web app: keyframe groups (A–H, any number of members) with a priority rule — earliest applies / latest applies / all (chronological); per day only the winning member fires
- Web app: conflict detection for keyframe pairs that swap order during the year (e.g. sunset vs. 20:00) and are not grouped, independent of the table order; one-click actions to group them; keyframe traces in the year view
- Web app: year view readability — brightness as default mode on a fixed dark → warm-white scale over perceived lightness (CIE L*, so 10 % reads as dim, not off; bright = much light, identical in day and night theme), spread colour-temperature scale, colour legend, sun lines with halo, keyframe labels in a non-overlapping gutter, traces only for moving/skipped keyframes, sun/keyframe overlay toggles, "last keyframe" in the tooltip; conflict list collapsible
- Web app: ungrouped conflicts are marked red on the days of the less frequent order — strip and traces in the year view, markers and notice in the day view, notice with "create group" above the year view
- Web app: hints for setups that silently change the curve over the year — groups mixing modes, interpolate keyframes whose predecessor changes with the day (unless an open conflict already explains it), and interpolations running across midnight
- Web app: three selectable examples — LKL office profile "Büroprofil Lichtdusche" (local time, two light showers), sun only, mixed (with conflicts in Berlin); loading an example also sets the schedule name
- Export: keyframe `id` and `group`, plus a `groups` array with each group's rule
- Export: `location` block and `trigger`/`sunEvent`/`offsetMinutes`/`notBefore`/`notAfter` per keyframe; `time` carries today's resolved time as fallback (HA does not evaluate sun triggers yet)

#### Fixed
- Web app simulation: value froze at midnight when the day's last keyframe was a transition followed by interpolate keyframes (wrapped keyframes lacked their index/day shift) — the curve now continues across midnight
- Web app simulation: after a transition, interpolation now only continues to the immediately following keyframe (with that keyframe's curve), matching the HA integration; previously it skipped ahead to the next interpolate keyframe
- Web app simulation: "before" transitions now ramp during their window instead of jumping at the keyframe time
- Web app: syntax error in the legacy `switchLanguage` script block (introduced with the restyle)

#### Changed
- Web app restyled on the LKL UI style guide 1.0.0 (`www/styleguide/`): design tokens, glass/paper skins, day/night/auto theme with switcher in the header
- Chart colours follow the active theme; PDF export always renders the chart on white
- Day chart: colour temperature on a real Kelvin axis spanning the schedule's own range (was normalised 2000–6500 K → 0–100); summary tiles below the chart removed

#### Removed
- One-shot mode ("Wiederholung: Einmalig", `wrapAround: false`) and `startDateTime`, in the web app and the integration: every schedule is a daily profile that continues across midnight. Both fields in older files are ignored; export no longer writes them.

#### Fixed (integration)
- The evaluator now takes the time of day from the local wall clock instead of counting minutes from the midnight at which it was created — after a DST switch the schedule no longer runs one hour off until HA is restarted

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
