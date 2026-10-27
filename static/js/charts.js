// Small DOM helper shared with rto_delivered.js (RTO Status KPI tiles).
// The old RTO Analytics dashboard script is kept in archive/analytics_dashboard.js.
(function () {
  "use strict";

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const k in attrs || {}) {
      if (k === "class") node.className = attrs[k];
      else node.setAttribute(k, attrs[k]);
    }
    children.flat().forEach((c) => {
      if (c == null) return;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return node;
  }

  window.RtoCharts = { el };
})();
