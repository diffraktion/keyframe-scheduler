/* =============================================================================
   LKL UI — theme switcher
   Licht Kunst Licht AG
   Version 1.0.0 · 2026-09-09
   =============================================================================
   Framework-free, no build step. Runs in static HTML just as well as in a
   React app (import it in main.jsx there).

   Sets data-theme / data-skin / data-accent on the <html> element and
   remembers the choice in localStorage. "auto" follows prefers-color-scheme
   and reacts live to system changes.

   Usage:
     lklTheme.get()                  -> { theme:"auto", skin:"glass", accent:"blue" }
     lklTheme.resolved()             -> { theme:"night", ... }  ("auto" resolved)
     lklTheme.set({ theme:"night" }) -> applies and persists
     lklTheme.subscribe(fn)          -> fn(resolvedState) on every change

   IMPORTANT: to avoid a flash of the wrong theme, this script must sit in
   <head> — BEFORE the first paint (see index.html).
   ============================================================================= */
(function (global) {
  "use strict";

  var KEY = "lkl-ui";                                  // one localStorage key for everything
  var DEFAULTS = { theme: "auto", skin: "glass", accent: "blue" };
  var VALID = {
    theme:  ["auto", "day", "night"],
    skin:   ["glass", "paper"],
    accent: ["copper", "blue"]
  };

  var state = Object.assign({}, DEFAULTS);
  var listeners = [];
  var mql = global.matchMedia ? global.matchMedia("(prefers-color-scheme: dark)") : null;

  function load() {
    try {
      var raw = global.localStorage.getItem(KEY);
      if (!raw) return;
      var saved = JSON.parse(raw);
      Object.keys(VALID).forEach(function (k) {
        if (saved && VALID[k].indexOf(saved[k]) !== -1) state[k] = saved[k];
      });
    } catch (e) {
      /* Private mode, blocked site data, malformed JSON — the defaults are
         always a valid state, so deliberately no rethrow here. */
    }
  }

  function save() {
    try { global.localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) { /* see above */ }
  }

  function resolvedTheme() {
    if (state.theme !== "auto") return state.theme;
    return mql && mql.matches ? "night" : "day";
  }

  function resolved() {
    return { theme: resolvedTheme(), skin: state.skin, accent: state.accent, themePref: state.theme };
  }

  function apply() {
    var el = global.document.documentElement;
    var r = resolved();
    el.setAttribute("data-theme", r.theme);
    el.setAttribute("data-skin", r.skin);
    el.setAttribute("data-accent", r.accent);
    listeners.forEach(function (fn) { fn(r); });
  }

  function set(patch) {
    Object.keys(patch || {}).forEach(function (k) {
      if (VALID[k] && VALID[k].indexOf(patch[k]) !== -1) state[k] = patch[k];
    });
    save();
    apply();
  }

  // System changes only matter while "auto" is active.
  if (mql) {
    var onChange = function () { if (state.theme === "auto") apply(); };
    if (mql.addEventListener) mql.addEventListener("change", onChange);
    else if (mql.addListener) mql.addListener(onChange);   // Safari < 14
  }

  load();
  apply();

  global.lklTheme = {
    get: function () { return Object.assign({}, state); },
    resolved: resolved,
    set: set,
    subscribe: function (fn) {
      listeners.push(fn);
      fn(resolved());
      return function () { listeners = listeners.filter(function (l) { return l !== fn; }); };
    }
  };
})(window);
