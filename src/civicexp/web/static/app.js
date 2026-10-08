// Civic Experimentation Platform — progressive enhancement only.
//
// Every page works without this file. It adds: chart tooltips (the same
// text is in each mark's aria-label and in the table beside the chart),
// confirmation prompts on consequential buttons, copy buttons, and the
// live power check while planning.
(function () {
  "use strict";

  // -- tooltips on chart marks ---------------------------------------------
  var tip = null;
  function showTip(target, x, y) {
    if (!tip) {
      tip = document.createElement("div");
      tip.className = "tooltip";
      tip.setAttribute("role", "presentation");
      document.body.appendChild(tip);
    }
    tip.textContent = target.getAttribute("data-tip"); // untrusted: text only
    var left = Math.min(x + 14, window.innerWidth - tip.offsetWidth - 8);
    tip.style.left = Math.max(8, left) + "px";
    tip.style.top = Math.max(8, y - tip.offsetHeight - 12) + "px";
    tip.hidden = false;
  }
  function hideTip() { if (tip) { tip.hidden = true; } }
  document.addEventListener("pointermove", function (event) {
    var target = event.target.closest && event.target.closest("[data-tip]");
    if (target) { showTip(target, event.clientX, event.clientY); } else { hideTip(); }
  });
  document.addEventListener("focusin", function (event) {
    var target = event.target.closest && event.target.closest("[data-tip]");
    if (target) {
      var box = target.getBoundingClientRect();
      showTip(target, box.left + box.width / 2, box.top);
    }
  });
  document.addEventListener("focusout", hideTip);

  // -- confirmation on consequential actions -------------------------------
  document.addEventListener("submit", function (event) {
    var form = event.target;
    var message = form.getAttribute && form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) { event.preventDefault(); }
  });

  // -- copy buttons --------------------------------------------------------
  document.addEventListener("click", function (event) {
    var button = event.target.closest && event.target.closest("[data-copy]");
    if (!button || !navigator.clipboard) { return; }
    navigator.clipboard.writeText(button.getAttribute("data-copy")).then(function () {
      button.textContent = "Copied";
    });
  });

  // -- live power check while planning -------------------------------------
  var panel = document.getElementById("power-check");
  if (panel) {
    var form = panel.closest("form");
    var timer = null;
    var read = function (name) {
      var input = form.querySelector('[name="' + name + '"]');
      return input ? input.value : "";
    };
    var check = function () {
      var params = new URLSearchParams({
        baseline: read("baseline_rate"),
        effect: read("minimum_effect"),
        monthly: read("monthly_volume"),
        days: read("duration_days")
      });
      fetch("/experiments/power?" + params.toString(), { credentials: "same-origin" })
        .then(function (response) { return response.json(); })
        .then(function (data) {
          if (!data.ready) { return; }
          panel.className = "power-check " + (data.adequate ? "adequate" : "inadequate");
          var p = document.createElement("p");
          var strong = document.createElement("strong");
          strong.textContent = data.adequate ? "Large enough. " : "Too small to answer the question. ";
          p.appendChild(strong);
          p.appendChild(document.createTextNode(data.message));
          panel.replaceChildren(p);
        })
        .catch(function () { /* the server re-checks on submit */ });
    };
    form.addEventListener("input", function () {
      window.clearTimeout(timer);
      timer = window.setTimeout(check, 350);
    });
    check();
  }
})();
