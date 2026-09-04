/* Theme toggle.
   The initial theme is applied inline in <head>; this file only owns
   the button, so a failure here leaves the page correctly themed.

   Divergence from knewman23.github.io and ai-frontier, which both follow
   prefers-color-scheme until the visitor chooses: this site defaults to
   light on a dark OS too. Dark is reachable only through this button, and
   the choice is then remembered. style.css matches — it carries no
   prefers-color-scheme block, so there is one source of truth for the
   default and the two cannot disagree. */
(function () {
  "use strict";

  var root = document.documentElement;
  var btn = document.getElementById("theme");
  var text = document.getElementById("theme-text");
  if (!btn || !text) return;

  function current() {
    return root.dataset.theme === "dark" ? "dark" : "light";
  }

  function label() {
    // The button names the theme you would switch TO.
    var next = current() === "dark" ? "light" : "dark";
    text.textContent = next === "dark" ? "Dark" : "Light";
    btn.setAttribute("aria-label", "Switch to " + next + " theme");
  }

  btn.addEventListener("click", function () {
    var next = current() === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    try { localStorage.setItem("theme", next); } catch (e) { /* private mode */ }
    label();
  });

  label();
})();
