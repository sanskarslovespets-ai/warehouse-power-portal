(function () {
  "use strict";
  // Purely presentational: count-up KPIs, trend-line draw-in, button ripple.
  // Nothing here reads or changes app data.

  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ---------- KPI count-up ----------
  function countUp(node) {
    var text = node.textContent.trim();
    if (!/^[\d,]+$/.test(text)) return; // leave compact values like "1.2K" alone
    var target = parseInt(text.replace(/,/g, ""), 10);
    if (!target || target < 2) return;
    var duration = 800;
    var start = null;
    function step(ts) {
      if (start === null) start = ts;
      var p = Math.min((ts - start) / duration, 1);
      var eased = 1 - Math.pow(1 - p, 3);
      node.textContent = Math.round(target * eased).toLocaleString();
      if (p < 1) requestAnimationFrame(step);
      else node.textContent = text;
    }
    node.textContent = "0";
    requestAnimationFrame(step);
  }

  var kpiRow = document.getElementById("kpi-row");
  if (kpiRow && !reduce) {
    new MutationObserver(function () {
      kpiRow.querySelectorAll(".kpi-value").forEach(function (n) {
        if (n.dataset.counted) return;
        n.dataset.counted = "1";
        countUp(n);
      });
    }).observe(kpiRow, { childList: true });
  }

  // ---------- trend line draw-in ----------
  var trendCard = document.getElementById("card-trend");
  if (trendCard && !reduce) {
    new MutationObserver(function () {
      trendCard.querySelectorAll(".trend-line:not([pathLength])").forEach(function (p) {
        p.setAttribute("pathLength", "1");
      });
    }).observe(trendCard, { childList: true, subtree: true });
  }

  // ---------- button ripple ----------
  document.addEventListener("pointerdown", function (e) {
    var btn = e.target.closest && e.target.closest(".btn");
    if (!btn || btn.disabled || reduce) return;
    var rect = btn.getBoundingClientRect();
    var size = Math.max(rect.width, rect.height) * 2;
    var r = document.createElement("span");
    r.className = "ripple";
    r.style.width = r.style.height = size + "px";
    r.style.left = e.clientX - rect.left - size / 2 + "px";
    r.style.top = e.clientY - rect.top - size / 2 + "px";
    btn.appendChild(r);
    setTimeout(function () { r.remove(); }, 650);
  });
})();
