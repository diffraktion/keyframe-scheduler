# Keyframe Scheduler Development Rules

## Schedule JSON (webapp export = integration input)

```json
{
  "version": 1, "name": "…", "timezone": "Europe/Berlin",
  "location": {"latitude": 52.52, "longitude": 13.405},
  "stepMinutes": 5, "horizonHours": 48,
  "keyframes": [
    {"id": "kf1a2b3c", "time": "20:00", "kelvin": 3500, "dim": 70,
     "mode": "interpolate", "curve": "sinus",
     "transitionSeconds": 600, "transitionDirection": "after",
     "trigger": "sun", "sunEvent": "sunset", "offsetMinutes": 30,
     "notBefore": "18:00", "notAfter": "22:00", "group": "A",
     "validOn": {"weekdays": ["mon", "tue", "wed", "thu", "fri"],
                 "from": "10-01", "to": "03-31"}}
  ],
  "groups": [{"id": "A", "rule": "earliest"}]
}
```

- `time` is `HH:MM`; for sun keyframes it is only a fallback (today's value)
- `dim` is 0–100 %, `kelvin` in K
- `validOn` (optional): weekdays and/or a yearly `MM-DD` period, both ends
  inclusive, `to` before `from` spans New Year — the PICO's `days` filter
- `wrapAround` / `startDateTime` of old files are ignored — every schedule is
  a daily profile that continues across midnight
- New fields must stay optional so old files keep working

## Keep webapp and integration in step

The same rules live in JavaScript and Python — change both:

| Rule | Webapp | Integration |
|------|--------|-------------|
| Sun events | `www/js/astro.js` | `astro.py` |
| Resolve a day (offset, bounds, fallback) | `resolveKeyframeMinutes` | `resolve_keyframe_minutes` |
| Group rules | `resolveFiringMinutes` | `resolve_day` |
| Valid on (weekdays, period) | `isValidOn` | `is_valid_on` |
| Neighbouring days across midnight | `resolveTimeline` | `Evaluator.timeline_for` |
| Transition follow-up (only the *immediately* next keyframe, its curve) | `evaluateSchedule` | `_evaluate_transition` |
| Value inside a transition window (blend from the value before it) | `evaluateSchedule` | `_evaluate_transition`, `_evaluate_at_minutes` |

Reference values (Berlin 21.06.: sunrise 04:43, sunset 21:33; 21.12.:
08:15 / 15:54; DST day 29.03.) are asserted in `tests/test_scheduler.py`.

## Light control

- Never call `light.turn_on` for a light that is not ON
- Every command gets a fresh `Context`; its id goes into the light's
  `own_contexts` — this is how our own state changes are recognised
- Manual change = foreign context with user or parent; device reports only
  via deviation after the fade. Never treat `picolightnode` contexts as manual
- Every light is paced by its own type — never by the strictest type of the
  instance. A command's target is the curve at the end of its fade, never
  the final value of a longer transition (the light would arrive early)
- Respect the light type: fade ≤ `max_transition`, commands ≥ `min_interval`
  apart (defer, do not drop)
- Decisions go into `light_logic.py` (testable), HA calls into `light_control.py`

## Time handling

- Time of day from the local wall clock in the schedule's timezone
  (`Evaluator.local_now`) — never count minutes from a start instant (breaks
  on DST)
- Midnight: evaluate on the timeline — the nearest earlier and later day that
  have keyframes, at −1440·k / +1440·k min. A day without keyframes (valid-on)
  holds the last value, like the PICO

## Webapp

- LKL UI style guide tokens only (`--lkl-*`) in component CSS; data/light
  rendering colours (year view) are commented constants
- All texts in `translations` for de / en / es
- Table shows settings only; times appear in the day/year views
- PICO export (`buildPicoScheduler`) mirrors `evaluateSchedule`: when the
  evaluation rules change, adapt the entry mapping too. As few entries as
  possible — the PICO (ESP32) handles large files badly and DALI must not be
  flooded with small steps; differences go into the warning list instead
- DALI: no fade longer than 15 min (extended fade time ends at 16 min). The
  PICO export splits longer fades (`PICO_DALI_MAX_FADE`); the HA light types
  must follow the same limit

## Tests

`python -m unittest discover -s tests` — no Home Assistant needed
(`tests/ha_stubs.py` provides stand-ins). Add a test for every bug fixed.
