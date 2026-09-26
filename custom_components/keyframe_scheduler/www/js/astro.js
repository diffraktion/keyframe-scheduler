/* =============================================================================
   Keyframe Scheduler — astronomy
   Sun position and sun events (dawn, sunrise, noon, sunset, dusk) for a
   location and a LOCAL calendar date in an IANA timezone.

   Position formulas follow the low-precision solar model used by SunCalc
   (Vladimir Agafonkin, BSD-2) — about one minute of accuracy for event times,
   which is well below the resolution the scheduler works at.

   Events are found numerically instead of with the closed-form hour-angle
   equation: the altitude curve of the local day is sampled every 10 minutes
   and every threshold crossing is refined by bisection. This handles DST
   days, timezones far from the location's meridian and polar day/night
   (an event that does not happen simply returns null) without special cases.

   No dependencies. Exposes window.KfAstro.
   ============================================================================= */
(function (global) {
  "use strict";

  var RAD = Math.PI / 180;
  var DAY_MS = 86400000;
  var J1970 = 2440588;
  var J2000 = 2451545;
  var OBLIQUITY = RAD * 23.4397;

  /* Altitudes in degrees. Sunrise/sunset use -0.833° (refraction + solar
     radius), the twilights their standard depression angles, golden hour the
     common +6° convention. */
  var EVENTS = {
    solar_midnight:    { midnight: true },
    astronomical_dawn: { h: -18,    rising: true  },
    nautical_dawn:     { h: -12,    rising: true  },
    civil_dawn:        { h: -6,     rising: true  },
    sunrise:           { h: -0.833, rising: true  },
    golden_hour_end:   { h: 6,      rising: true  },
    solar_noon:        { noon: true },
    golden_hour_start: { h: 6,      rising: false },
    sunset:            { h: -0.833, rising: false },
    civil_dusk:        { h: -6,     rising: false },
    nautical_dusk:     { h: -12,    rising: false },
    astronomical_dusk: { h: -18,    rising: false }
  };
  var EVENT_NAMES = Object.keys(EVENTS);

  // ---- Solar position ------------------------------------------------------

  function altitude(ms, lat, lon) {
    var d = ms / DAY_MS - 0.5 + J1970 - J2000;
    var M = RAD * (357.5291 + 0.98560028 * d);
    var C = RAD * (1.9148 * Math.sin(M) + 0.02 * Math.sin(2 * M) + 0.0003 * Math.sin(3 * M));
    var L = M + C + RAD * 102.9372 + Math.PI;
    var dec = Math.asin(Math.sin(OBLIQUITY) * Math.sin(L));
    var ra = Math.atan2(Math.sin(L) * Math.cos(OBLIQUITY), Math.cos(L));
    var siderealTime = RAD * (280.16 + 360.9856235 * d) + RAD * lon;
    var H = siderealTime - ra;
    var phi = RAD * lat;
    return Math.asin(Math.sin(phi) * Math.sin(dec) + Math.cos(phi) * Math.cos(dec) * Math.cos(H)) / RAD;
  }

  // ---- Timezone helpers ----------------------------------------------------

  var formatters = {};
  function formatter(tz) {
    if (!formatters[tz]) {
      formatters[tz] = new Intl.DateTimeFormat("en-US", {
        timeZone: tz, hourCycle: "h23",
        year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", second: "2-digit"
      });
    }
    return formatters[tz];
  }

  function isValidTimeZone(tz) {
    try { formatter(tz); return true; } catch (e) { return false; }
  }

  /** Wall-clock offset of tz at instant ms, in minutes (UTC+1 → 60). */
  function tzOffsetMinutes(ms, tz) {
    var p = {};
    formatter(tz).formatToParts(new Date(ms)).forEach(function (x) { p[x.type] = x.value; });
    var wall = Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute, +p.second);
    return Math.round((wall - Math.floor(ms / 1000) * 1000) / 60000);
  }

  /** UTC instant of local midnight at the start of y-m-d in tz. */
  function localMidnightUtc(y, m, d, tz) {
    var guess = Date.UTC(y, m - 1, d);
    var ms = guess - tzOffsetMinutes(guess, tz) * 60000;
    // Second pass settles days where the offset changes around midnight.
    return guess - tzOffsetMinutes(ms, tz) * 60000;
  }

  /** Minutes since local midnight of y-m-d (wall clock) for instant ms. */
  function wallMinutes(ms, y, m, d, tz) {
    return (ms + tzOffsetMinutes(ms, tz) * 60000 - Date.UTC(y, m - 1, d)) / 60000;
  }

  // ---- Events --------------------------------------------------------------

  var STEP_MS = 10 * 60000;

  function bisect(f, a, b) {
    var fa = f(a);
    for (var i = 0; i < 24; i++) {          // 10 min / 2^24 → far below 1 s
      var mid = (a + b) / 2, fm = f(mid);
      if ((fm < 0) === (fa < 0)) { a = mid; fa = fm; } else { b = mid; }
    }
    return (a + b) / 2;
  }

  var cache = {};
  var cacheSize = 0;

  /**
   * Sun events of a local calendar day.
   * Returns { events: { sunrise: minutes|null, … }, dayLength: minutes|null,
   *           maxAltitude, minAltitude } — minutes are wall-clock minutes
   * since local midnight.
   */
  function sunTimes(y, m, d, lat, lon, tz) {
    var key = lat + "|" + lon + "|" + tz + "|" + y + "-" + m + "-" + d;
    if (cache[key]) return cache[key];

    var start = localMidnightUtc(y, m, d, tz);
    var end = localMidnightUtc(y, m, d + 1, tz);
    var samples = [];
    for (var t = start; t <= end; t += STEP_MS) samples.push({ t: t, alt: altitude(t, lat, lon) });

    var events = {};
    var maxS = samples[0], minS = samples[0];
    samples.forEach(function (s) {
      if (s.alt > maxS.alt) maxS = s;
      if (s.alt < minS.alt) minS = s;
    });

    // Solar noon: golden-section search for the altitude maximum around the
    // best sample.
    var lo = maxS.t - STEP_MS, hi = maxS.t + STEP_MS, g = (Math.sqrt(5) - 1) / 2;
    for (var i = 0; i < 30; i++) {
      var c = hi - g * (hi - lo), e = lo + g * (hi - lo);
      if (altitude(c, lat, lon) > altitude(e, lat, lon)) hi = e; else lo = c;
    }
    var noonMs = (lo + hi) / 2;

    // Solar midnight: the anti-transit, 12 h from noon (the drift of the
    // equation of time over half a day is a few seconds). The minimum of the
    // sampled day cannot be used directly — around local midnight it sits on
    // the edge of the window. Of the two candidates take the one inside the
    // local day; a 23 h DST day may have none.
    var midnightMs = noonMs - DAY_MS / 2 >= start ? noonMs - DAY_MS / 2
                   : noonMs + DAY_MS / 2 < end ? noonMs + DAY_MS / 2 : null;

    EVENT_NAMES.forEach(function (name) {
      var ev = EVENTS[name];
      events[name] = null;
      if (ev.noon) { events[name] = wallMinutes(noonMs, y, m, d, tz); return; }
      if (ev.midnight) {
        events[name] = midnightMs == null ? null : wallMinutes(midnightMs, y, m, d, tz);
        return;
      }
      var f = function (ms) { return altitude(ms, lat, lon) - ev.h; };
      for (var k = 1; k < samples.length; k++) {
        var p = samples[k - 1].alt - ev.h, q = samples[k].alt - ev.h;
        var crosses = ev.rising ? (p < 0 && q >= 0) : (p >= 0 && q < 0);
        if (crosses) {
          events[name] = wallMinutes(bisect(f, samples[k - 1].t, samples[k].t), y, m, d, tz);
          break;
        }
      }
    });

    var result = {
      events: events,
      dayLength: events.sunrise != null && events.sunset != null ? events.sunset - events.sunrise : null,
      maxAltitude: maxS.alt,
      minAltitude: minS.alt
    };

    if (++cacheSize > 5000) { cache = {}; cacheSize = 0; }
    cache[key] = result;
    return result;
  }

  /** Sun altitude at wall-clock minutes of local day y-m-d. */
  function altitudeAt(y, m, d, minutes, lat, lon, tz) {
    return altitude(localMidnightUtc(y, m, d, tz) + minutes * 60000, lat, lon);
  }

  // ---- Daylight ------------------------------------------------------------

  /* Twilight reference values (sun altitude °, horizontal illuminance lx):
     sunset ≈ several hundred lx, end of civil twilight ≈ 3 lx, end of
     nautical ≈ 0.01 lx, end of astronomical ≈ starlight. Interpolated
     log-linearly, which matches the roughly exponential fall-off. */
  var TWILIGHT = [[0, 800], [-6, 3.4], [-12, 0.008], [-18, 0.0007]];

  /**
   * Potential outdoor horizontal illuminance in lux under a CLEAR sky for a
   * sun altitude in degrees — no clouds, no obstruction. A deliberately simple
   * engineering model:
   *   direct  = 127 500 lx · e^(−0.21·m) · sin h   (m: Kasten–Young air mass)
   *   diffuse = 800 lx + 15 500 lx · √(sin h)       (clear-sky sky light)
   * Gives ≈ 100 klx at 60° and ≈ 20 klx at 10°, typical clear-sky values.
   */
  function clearSkyIlluminance(h) {
    if (h > 0) {
      var s = Math.sin(h * RAD);
      var airMass = 1 / (s + 0.50572 * Math.pow(h + 6.07995, -1.6364));
      return 127500 * Math.exp(-0.21 * airMass) * s + 800 + 15500 * Math.sqrt(s);
    }
    for (var i = 1; i < TWILIGHT.length; i++) {
      var hi = TWILIGHT[i - 1], lo = TWILIGHT[i];
      if (h >= lo[0]) {
        var f = (h - lo[0]) / (hi[0] - lo[0]);
        return Math.pow(10, Math.log10(lo[1]) + f * (Math.log10(hi[1]) - Math.log10(lo[1])));
      }
    }
    return TWILIGHT[TWILIGHT.length - 1][1];
  }

  var api = {
    clearSkyIlluminance: clearSkyIlluminance,
    EVENTS: EVENTS,
    EVENT_NAMES: EVENT_NAMES,
    altitude: altitude,
    altitudeAt: altitudeAt,
    sunTimes: sunTimes,
    localMidnightUtc: localMidnightUtc,
    tzOffsetMinutes: tzOffsetMinutes,
    isValidTimeZone: isValidTimeZone
  };

  global.KfAstro = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : this);
