# Keyframe Scheduler

Home Assistant integration for time-based light control with keyframes: brightness and colour temperature over the day, by clock time or by the position of the sun.

Works with **all** HA light entities — DALI, Casambi, Zigbee, Philips Hue, Z-Wave, WLED, PICOlightnode and standard lights.

> Auch verfügbar auf [Deutsch](README.de.md) | También disponible en [Español](README.es.md)

---

## How It Works

You define keyframes — each with a trigger, brightness and colour temperature. The integration interpolates between them and **controls the assigned lights itself**. No automation and no blueprint needed.

- **Triggers:** a fixed time or a sun event at the location (twilights, sunrise, solar noon, golden hour, sunset, solar midnight) with an offset in minutes and optional bounds *not before / not after*.
- **Groups:** keyframes with the same role (e.g. “sunset” and “20:00”) can be grouped; per day only the earliest, the latest or all of them apply.
- **One instance = one schedule** for any number of lights. Another instance is only needed for a different schedule.

---

## Installation

### Via HACS (recommended)

1. HACS → Integrations → `+` → search **Keyframe Scheduler**
2. Install → restart Home Assistant

---

## Setup

### Step 1 — Create an instance

1. Settings → Devices & Services → Add Integration → **Keyframe Scheduler**
2. Give it a name (e.g. `Meeting room`)

### Step 2 — Design the schedule

Design the schedule in the webapp (sidebar entry **Keyframe Scheduler**) and export it with **Save as file**. The webapp shows a year and a day view with sun times, marks keyframes that swap order during the year, and suggests suitable bounds.

### Step 3 — Assign schedule and lights

Settings → Devices & Services → Keyframe Scheduler → **Configure**:

| Field | Description |
|-------|-------------|
| **Schedule JSON** | Paste the content of the exported file (leave empty = unchanged) |
| **Lights** | The lights of this instance — light groups too (expanded into their members) |
| **Follow again after a manual change** | Only by off/on or the follow switch · after N minutes · at the next keyframe |
| **Also detect changes made at the device** | Detects e.g. a wall dimmer on the bus (see below) |
| **Adjust light type timings** | Opens a further step for the type timings |

In the next step every light gets its **type**:

| Type | Max. transition | Min. interval between commands |
|------|-----------------|--------------------------------|
| DALI | 90 s | 30 s |
| DALI-2 Extended Fade | 27 min | 30 s |
| Casambi / Bluetooth Mesh | 10 min | 30 s |
| Zigbee | 10 min | 15 s |
| Philips Hue | 10 min | 10 s |
| Z-Wave | 5 min | 30 s |
| Generic / WiFi | 5 min | 15 s |

The intervals are conservative defaults and can be adjusted per instance. This way lights on different buses can share **one** schedule, e.g. DALI and Zigbee in the same meeting room.

Location for sun keyframes: the location from the schedule, otherwise the one configured in Home Assistant.

---

## Light Behaviour

- **Only lights that are on are adjusted.** The integration never switches a light on. Switching off at the wall switch means “off”.
- **When switched on** (app, wall switch, presence sensor …) the light takes the current values at once and follows the schedule from then on.
- **Commands** are sent at most as often as the light type allows; a fade never exceeds its maximum.

### Manual changes

If someone else changes the light, it **pauses**: its follow switch turns off and the integration stops sending to it.

A manual change is:
- a command from a user (dashboard, app)
- a command from a scene, script or another automation
- optionally a change the light reports by itself (e.g. wall dimmer on the bus): detected when the reported value clearly differs from what was sent after the fade is over (> 5 % brightness or > 150 K)

Not a manual change: the integration's own commands and PICOlightnode internal updates (`picolightnode_restore`).

**Resuming:** switching the light off and on again **always** resumes. Depending on the setting also after N minutes or at the next keyframe.

### Follow switch

Every light has a switch:
```
switch.keyframe_<instance>_<light>_follow
```

| State | Meaning |
|-------|---------|
| ON | The light follows the schedule |
| OFF, `pause_reason: manual` | Paused by a manual change — resumes automatically (see above) |
| OFF, `pause_reason: user` | Turned off on purpose — stays off until the switch is turned on again |

Turning the switch back on fades the light to the current values at once.

---

## Sensors

Per instance:

| Sensor | Description |
|--------|-------------|
| `sensor.<name>_target_kelvin` | Current colour temperature target in Kelvin |
| `sensor.<name>_target_brightness` | Current brightness target (0–100 %) |
| `sensor.<name>_target_mired` | Current colour temperature in mired |
| `sensor.<name>_next_change` | Time of the next scheduled value change |

Attributes: `transition_seconds` (fade time until the next update) and `keyframes_today` (when the keyframes fire today, e.g. `["07:00", "22:03 (sunset +30 min)"]`).

---

## Services

| Service | Description |
|---------|-------------|
| `keyframe_scheduler.apply` | Send the current values to the lights now (optionally one instance / specific lights) |
| `keyframe_scheduler.set_manual_control` | Pause lights (`manual: true`) or let them follow again (`manual: false`) |
| `keyframe_scheduler.set_schedule` | Set the schedule as JSON |
| `keyframe_scheduler.upload_from_file` | Load the schedule from a file under `/config/` |

A new schedule via `set_schedule` or `upload_from_file` applies immediately, without reloading the integration.

---

## Migrating from the blueprint

Up to version 3.x a blueprint automation per light applied the values. The blueprint has been removed:

1. **Delete** the existing blueprint automations — otherwise two places control the same light.
2. The previous “Follow Lights” are taken over as lights automatically (with the type matching the previous hardware limit). Check them under **Configure** and set the type per light.

---

## Webapp

After installation **Keyframe Scheduler** appears as a sidebar entry in Home Assistant. Direct URL: `http://<your-ha-host>/keyframe_scheduler/index.html`

Available languages: DE / EN / ES

### PICO lightnode export

**PICO DailyScheduler** (Export & Import) generates the `DAILYSCHEDULER` behavior for a PICO lightnode `setup.json`: paste it into the `behaviors` of a target with space `TC`. Targets, adjust/override behaviors and destinations stay in the setup; latitude, longitude and time zone come from the PICO configuration (`pico.latitude` / `pico.longitude`).

One entry per keyframe keeps the file small: sun triggers, offsets, not-before/not-after bounds and groups (`EARLIEST`/`LATEST`) are translated 1:1. The PICO only fades linearly with a fixed duration, so sine curves become linear and an interpolation anchored to the sun uses the shortest ramp of the year (the target is reached early and held). The export view lists every difference from the simulation.

---

## Requirements

| Component | Minimum version |
|-----------|----------------|
| Home Assistant | 2024.7.0 |
| PICOlightnode *(optional)* | 2.0.18 |

---

## Links

- [Issues & feature requests](https://github.com/mjmijh/keyframe-scheduler/issues)
- [PICOlightnode integration](https://github.com/mjmijh/picolightnode-ha)
- [CCT Astronomy integration](https://github.com/mjmijh/cct-astronomy)
