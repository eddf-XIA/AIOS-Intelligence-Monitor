/* Small vanilla helpers. Anything dynamic is HTMX; this file only covers
   confirmation prompts, overflow menus, password reveal, and keeping the
   run log pinned to its newest line. */

(function () {
  "use strict";

  /* Confirm destructive actions before the form submits. */
  document.addEventListener("submit", function (event) {
    var form = event.target;
    var message = form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) {
      event.preventDefault();
    }
  });

  /* Overflow (⋯) menus: keeps secondary actions out of sight until wanted. */
  function closeMenus(except) {
    document.querySelectorAll('.menu[data-open="true"]').forEach(function (menu) {
      if (menu !== except) {
        menu.setAttribute("data-open", "false");
        var trigger = menu.querySelector("[data-menu-trigger]");
        if (trigger) trigger.setAttribute("aria-expanded", "false");
      }
    });
  }

  document.addEventListener("click", function (event) {
    var trigger = event.target.closest("[data-menu-trigger]");
    if (trigger) {
      var menu = trigger.closest(".menu");
      var open = menu.getAttribute("data-open") === "true";
      closeMenus(menu);
      menu.setAttribute("data-open", open ? "false" : "true");
      trigger.setAttribute("aria-expanded", open ? "false" : "true");
      event.preventDefault();
      return;
    }
    if (!event.target.closest(".menu-panel")) closeMenus(null);
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") closeMenus(null);
  });

  /* Reveal/hide an API key while typing it in. */
  document.addEventListener("click", function (event) {
    var toggle = event.target.closest("[data-toggle-visibility]");
    if (!toggle) return;
    var input = document.getElementById(toggle.getAttribute("data-toggle-visibility"));
    if (!input) return;
    var showing = input.type === "text";
    input.type = showing ? "password" : "text";
    toggle.textContent = showing ? "显示" : "隐藏";
  });

  /* Make a whole table row clickable without nesting <a> inside <tr>. */
  document.addEventListener("click", function (event) {
    var row = event.target.closest("[data-href]");
    if (!row) return;
    if (event.target.closest("a, button, input, select, label, .menu")) return;
    window.location.href = row.getAttribute("data-href");
  });

  /* Keep the run log pinned to its newest line - but only while the reader is
     already at the bottom. Someone who scrolled up to read an earlier line is
     reading it; yanking them back every two seconds would make the live log
     unusable. */
  var NEAR_BOTTOM_PX = 48;

  function atBottom(node) {
    return node.scrollHeight - node.scrollTop - node.clientHeight <= NEAR_BOTTOM_PX;
  }

  function scrollLog(force) {
    document.querySelectorAll(".log[data-autoscroll]").forEach(function (node) {
      if (force || node.dataset.pinned !== "false") {
        node.scrollTop = node.scrollHeight;
      }
    });
  }

  document.addEventListener("DOMContentLoaded", function () { scrollLog(true); });

  /* Remember whether the reader is following the tail, before the swap moves
     the content under them. */
  document.body.addEventListener("htmx:beforeSwap", function () {
    document.querySelectorAll(".log[data-autoscroll]").forEach(function (node) {
      node.dataset.pinned = atBottom(node) ? "true" : "false";
    });
  });
  document.body.addEventListener("htmx:afterSwap", function () { scrollLog(false); });
  document.addEventListener("scroll", function (event) {
    var node = event.target;
    if (node && node.classList && node.classList.contains("log")) {
      node.dataset.pinned = atBottom(node) ? "true" : "false";
    }
  }, true);

  /* Fill a field from an example button ("try this description"). */
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-fill]");
    if (!button) return;
    var field = document.querySelector(button.getAttribute("data-fill"));
    if (!field) return;
    field.value = button.getAttribute("data-fill-value") || "";
    field.focus();
  });

  /* ----------------------------------------------------------------------
     Live elapsed clock for an in-flight run.

     Always derived from the start timestamp, never incremented: a throttled
     or sleeping background tab would drift badly with `seconds += 1`, whereas
     `Date.now() - started` is correct the instant the tab wakes up.

     Exactly one interval exists process-wide (`elapsedTimer`), so repeated
     HTMX swaps cannot accumulate duplicate timers.
     ---------------------------------------------------------------------- */

  var elapsedTimer = null;

  /* Mirrors cn_duration_text() in aios/timeutil.py. Both must agree, because
     the server renders the first value and the browser renders every one
     after it. */
  function formatElapsed(totalSeconds) {
    var seconds = Math.floor(totalSeconds);
    if (!isFinite(seconds) || seconds < 0) return "";
    if (seconds < 60) return seconds + " 秒";
    var minutes = Math.floor(seconds / 60);
    seconds = seconds % 60;
    if (minutes < 60) return minutes + " 分 " + pad2(seconds) + " 秒";
    var hours = Math.floor(minutes / 60);
    minutes = minutes % 60;
    return hours + " 小时 " + pad2(minutes) + " 分";
  }

  function pad2(value) {
    return value < 10 ? "0" + value : String(value);
  }

  /* The run state that the 2s poll keeps fresh. The clock element lives in the
     page header, outside the polled region, so this marker is how it learns
     the run has ended. */
  function elapsedState() {
    var sync = document.getElementById("run-elapsed-sync");
    if (!sync) return null;
    return {
      live: sync.getAttribute("data-elapsed-live") === "1",
      start: sync.getAttribute("data-elapsed-start"),
      final: sync.getAttribute("data-elapsed-final")
    };
  }

  function tickElapsed() {
    var nodes = document.querySelectorAll("[data-elapsed]");
    var state = elapsedState();
    var stillRunning = false;

    nodes.forEach(function (node) {
      var live = node.getAttribute("data-elapsed-live") === "1";
      var start = node.getAttribute("data-elapsed-start");
      var final = node.getAttribute("data-elapsed-final");

      /* The polled marker outranks whatever the page was rendered with. */
      if (state) {
        live = state.live;
        start = state.start || start;
        final = state.final || final;
      }

      if (!live) {
        /* Terminal: show the canonical server-computed duration and leave it
           alone from here on. */
        if (final) node.textContent = final;
        node.setAttribute("data-elapsed-live", "0");
        return;
      }

      var startedMs = Date.parse(start);
      if (isNaN(startedMs)) return;
      stillRunning = true;
      node.textContent = formatElapsed((Date.now() - startedMs) / 1000);
    });

    return stillRunning;
  }

  /* Start the interval only while something is actually counting, and stop it
     as soon as nothing is. */
  function syncElapsed() {
    var running = tickElapsed();
    if (running && elapsedTimer === null) {
      elapsedTimer = window.setInterval(tickElapsed, 1000);
    } else if (!running && elapsedTimer !== null) {
      window.clearInterval(elapsedTimer);
      elapsedTimer = null;
    }
  }

  document.addEventListener("DOMContentLoaded", syncElapsed);
  document.body.addEventListener("htmx:afterSwap", syncElapsed);
  /* Catch up immediately when a background tab is brought back, instead of
     waiting up to a second for the next tick. */
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) syncElapsed();
  });

  /* When a run finishes, refresh once so the page chrome outside the polled
     region - the status badge, the output counters, the report link - catches
     up with the finished run.

     The marker is looked up in the document rather than read off
     `event.detail.target`: for an `outerHTML` swap the event's target is not
     the element that was swapped in, so keying off it meant the reload never
     fired and the header sat on "监测中" after the run had ended. */
  document.body.addEventListener("htmx:afterSwap", function () {
    if (window.__aiosReloaded) return;
    if (!document.querySelector('[data-run-finished="1"]')) return;
    window.__aiosReloaded = true;
    window.setTimeout(function () { window.location.reload(); }, 900);
  });
})();
