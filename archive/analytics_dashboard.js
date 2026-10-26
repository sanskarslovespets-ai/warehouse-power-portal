(function () {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";
  const TOP_N_BARS = 8;
  const PRESET_LABELS = {
    today: "Today",
    "7d": "Last 7 days",
    "30d": "Last 30 days",
    "90d": "Last 90 days",
    mtd: "Month to date",
    all: "All time",
  };

  // ---------- helpers ----------
  async function fetchJson(url, options) {
    const res = await fetch(url, options);
    let data = null;
    try {
      data = await res.json();
    } catch (e) {
      /* no body */
    }
    if (!res.ok) throw new Error((data && data.error) || `Request failed (${res.status})`);
    return data;
  }

  function svgEl(tag, attrs) {
    const el = document.createElementNS(SVG_NS, tag);
    for (const k in attrs || {}) el.setAttribute(k, attrs[k]);
    return el;
  }

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const k in attrs || {}) {
      if (k === "class") node.className = attrs[k];
      else if (k === "html") continue; // never used - textContent only, see below
      else node.setAttribute(k, attrs[k]);
    }
    children.flat().forEach((c) => {
      if (c == null) return;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return node;
  }

  function formatCompact(n) {
    if (n < 10000) return n.toLocaleString();
    if (n < 1000000) return (n / 1000).toFixed(1).replace(/\.0$/, "") + "K";
    return (n / 1000000).toFixed(1).replace(/\.0$/, "") + "M";
  }

  function todayStr() {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  }

  function addDays(dateStr, delta) {
    const d = new Date(dateStr + "T00:00:00");
    d.setDate(d.getDate() + delta);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  }

  function computePresetRange(preset) {
    const today = todayStr();
    if (preset === "today") return { start: today, end: today };
    if (preset === "7d") return { start: addDays(today, -6), end: today };
    if (preset === "90d") return { start: addDays(today, -89), end: today };
    if (preset === "mtd") {
      const d = new Date(today + "T00:00:00");
      return { start: `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-01`, end: today };
    }
    if (preset === "all") return { start: null, end: null };
    return { start: addDays(today, -29), end: today }; // 30d default
  }

  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  function formatBucketLabel(bucket, granularity) {
    const d = new Date(bucket + "T00:00:00");
    if (granularity === "month") return `${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
    return `${MONTHS[d.getMonth()]} ${d.getDate()}`;
  }

  // ---------- state ----------
  const initial = computePresetRange("30d");
  const state = { preset: "30d", start: initial.start, end: initial.end, dateField: "damage_marked_date" };

  // ---------- elements ----------
  const analyticsBody = document.getElementById("analytics-body");
  const analyticsEmpty = document.getElementById("analytics-empty");
  const kpiRow = document.getElementById("kpi-row");
  const cardTrend = document.getElementById("card-trend");
  const cardFacility = document.getElementById("card-facility");
  const cardCourier = document.getElementById("card-courier");
  const cardSku = document.getElementById("card-sku");
  const dateRangeTrigger = document.getElementById("date-range-trigger");
  const dateRangeLabel = document.getElementById("date-range-label");
  const dateRangePopover = document.getElementById("date-range-popover");
  const dateFieldSelect = document.getElementById("date-field-select");
  const rangeFromInput = document.getElementById("range-from");
  const rangeToInput = document.getElementById("range-to");

  // ---------- hide/show dashboard toggle ----------
  const dashboardWrap = document.getElementById("analytics-dashboard-wrap");
  const toggleDashboardBtn = document.getElementById("btn-toggle-dashboard");
  const DASHBOARD_HIDDEN_KEY = "rto-dashboard-hidden";

  function applyDashboardVisibility(hidden) {
    dashboardWrap.classList.toggle("collapsed", hidden);
    toggleDashboardBtn.textContent = hidden ? "Show dashboard" : "Hide dashboard";
  }

  let dashboardHidden = false;
  try {
    dashboardHidden = localStorage.getItem(DASHBOARD_HIDDEN_KEY) === "1";
  } catch (e) {
    /* private browsing / blocked storage - default to shown */
  }
  applyDashboardVisibility(dashboardHidden);

  toggleDashboardBtn.addEventListener("click", () => {
    dashboardHidden = !dashboardHidden;
    applyDashboardVisibility(dashboardHidden);
    try {
      localStorage.setItem(DASHBOARD_HIDDEN_KEY, dashboardHidden ? "1" : "0");
    } catch (e) {
      /* not persisted this session - toggle still works visually */
    }
  });

  // ---------- tooltip (one shared floating element, reused by every chart) ----------
  const tooltip = el("div", { class: "chart-tooltip hidden" },
    el("div", { class: "tt-value" }), el("div", { class: "tt-label" }));
  document.body.appendChild(tooltip);

  function showTooltip(x, y, valueText, labelText) {
    tooltip.querySelector(".tt-value").textContent = valueText;
    tooltip.querySelector(".tt-label").textContent = labelText;
    tooltip.style.left = x + "px";
    tooltip.style.top = y + "px";
    tooltip.classList.remove("hidden");
  }
  function hideTooltip() {
    tooltip.classList.add("hidden");
  }

  // ---------- date range popover ----------
  function updateRangeLabel() {
    if (state.preset === "custom") {
      dateRangeLabel.textContent = `${state.start} → ${state.end}`;
    } else {
      dateRangeLabel.textContent = PRESET_LABELS[state.preset] || "Custom range";
    }
  }

  function openPopover() {
    dateRangePopover.classList.remove("hidden");
    dateRangeTrigger.setAttribute("aria-expanded", "true");
  }
  function closePopover() {
    dateRangePopover.classList.add("hidden");
    dateRangeTrigger.setAttribute("aria-expanded", "false");
  }

  dateRangeTrigger.addEventListener("click", () => {
    dateRangePopover.classList.contains("hidden") ? openPopover() : closePopover();
  });
  document.addEventListener("click", (e) => {
    if (!dateRangePopover.contains(e.target) && e.target !== dateRangeTrigger && !dateRangeTrigger.contains(e.target)) {
      closePopover();
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closePopover();
  });

  document.querySelectorAll(".preset-row").forEach((btn) => {
    btn.addEventListener("click", () => {
      const preset = btn.dataset.preset;
      const range = computePresetRange(preset);
      state.preset = preset;
      state.start = range.start;
      state.end = range.end;
      document.querySelectorAll(".preset-row").forEach((b) => b.classList.toggle("active", b === btn));
      updateRangeLabel();
      closePopover();
      loadAndRender();
    });
  });

  document.getElementById("btn-apply-custom-range").addEventListener("click", () => {
    const from = rangeFromInput.value;
    const to = rangeToInput.value;
    if (!from || !to || from > to) return; // silently ignore an incomplete/inverted range
    state.preset = "custom";
    state.start = from;
    state.end = to;
    document.querySelectorAll(".preset-row").forEach((b) => b.classList.remove("active"));
    updateRangeLabel();
    closePopover();
    loadAndRender();
  });

  dateFieldSelect.addEventListener("change", () => {
    state.dateField = dateFieldSelect.value;
    loadAndRender();
  });

  document.getElementById("btn-refresh-analytics").addEventListener("click", loadAndRender);

  // ---------- KPI row ----------
  function renderKpis(data) {
    kpiRow.textContent = "";
    const tiles = [
      { label: "Total entries", value: data.total, accent: "var(--chart-baseline)" },
      { label: "RTO", value: data.by_damage_type.RTO || 0, accent: "var(--chart-series-1)" },
      { label: "RPU", value: data.by_damage_type.RPU || 0, accent: "var(--chart-series-2)" },
      { label: "WH Damage", value: data.by_damage_type["WH Damage"] || 0, accent: "var(--chart-series-3)" },
      { label: "Inward Damage", value: data.by_damage_type["Inward Damage"] || 0, accent: "var(--chart-series-4)" },
      { label: "Pending sheet sync", value: data.sheet_sync.pending, accent: "#b45309" },
    ];
    tiles.forEach((t) => {
      kpiRow.appendChild(
        el("div", { class: "kpi-tile", style: `border-left-color:${t.accent}` },
          el("div", { class: "kpi-label" }, t.label),
          el("div", { class: "kpi-value" }, formatCompact(t.value)))
      );
    });
  }

  // ---------- horizontal bar chart card (Facility / Courier / SKU) ----------
  function buildBarDisplayRows(fullData) {
    if (fullData.length <= TOP_N_BARS) return fullData;
    const top = fullData.slice(0, TOP_N_BARS);
    const other = fullData.slice(TOP_N_BARS).reduce((sum, r) => sum + r.count, 0);
    return top.concat([{ label: "Other", count: other, isOther: true }]);
  }

  function renderBarCard(container, title, fullData, unit = "Entries") {
    container.textContent = "";
    const header = el("div", { class: "chart-card-header" },
      el("h3", {}, title),
      el("button", { type: "button", class: "table-toggle-btn" }, "Table view"));
    container.appendChild(header);

    if (fullData.length === 0) {
      container.appendChild(el("div", { class: "chart-empty" }, "No data in this range."));
      return;
    }

    const rows = buildBarDisplayRows(fullData);
    const maxCount = Math.max(...rows.map((r) => r.count));

    const list = el("div", { class: "hbar-list" });
    rows.forEach((r) => {
      const pct = Math.max((r.count / maxCount) * 100, 2);
      const row = el("div", { class: "hbar-row" + (r.isOther ? " other" : "") },
        el("div", { class: "hbar-label" }, r.label),
        el("div", { class: "hbar-track" }, el("div", { class: "hbar-fill", style: `width:${pct}%` })),
        el("div", { class: "hbar-value" }, formatCompact(r.count)));
      row.addEventListener("mouseenter", () => {
        const rect = row.getBoundingClientRect();
        showTooltip(rect.left + rect.width / 2 + window.scrollX, rect.top + window.scrollY, formatCompact(r.count), r.label);
      });
      row.addEventListener("mouseleave", hideTooltip);
      list.appendChild(row);
    });
    container.appendChild(list);

    const tableWrap = el("div", { class: "chart-table-wrap hidden" });
    const table = el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, title), el("th", {}, unit))),
      el("tbody", {}, fullData.map((r) => el("tr", {}, el("td", {}, r.label), el("td", { class: "num" }, String(r.count))))));
    tableWrap.appendChild(table);
    container.appendChild(tableWrap);

    header.querySelector(".table-toggle-btn").addEventListener("click", (e) => {
      const showingTable = !tableWrap.classList.contains("hidden");
      tableWrap.classList.toggle("hidden", showingTable);
      list.classList.toggle("hidden", !showingTable);
      e.target.textContent = showingTable ? "Table view" : "Chart view";
    });
  }

  // ---------- trend line + area chart (SVG) ----------
  const SVG_W = 600, SVG_H = 190;
  const PLOT_LEFT = 42, PLOT_RIGHT = 588, PLOT_TOP = 14, PLOT_BOTTOM = 158;

  function niceMax(value) {
    if (value <= 0) return 4;
    const magnitude = Math.pow(10, Math.floor(Math.log10(value)));
    const residual = value / magnitude;
    const step = residual <= 1 ? 1 : residual <= 2 ? 2 : residual <= 5 ? 5 : 10;
    return step * magnitude;
  }

  function pickTickIndices(n, maxTicks) {
    if (n <= maxTicks) return Array.from({ length: n }, (_, i) => i);
    const idx = new Set();
    for (let k = 0; k < maxTicks; k++) idx.add(Math.round((k / (maxTicks - 1)) * (n - 1)));
    return Array.from(idx);
  }

  function renderTrendCard(container, data, title = "Entries over time", unit = "Entries") {
    container.textContent = "";
    const header = el("div", { class: "chart-card-header" },
      el("h3", {}, title),
      el("button", { type: "button", class: "table-toggle-btn" }, "Table view"));
    container.appendChild(header);
    container.appendChild(
      el("p", { class: "chart-card-sub" }, `Grouped by ${data.granularity} · ${data.trend.length} point${data.trend.length === 1 ? "" : "s"}`)
    );

    const points = data.trend;
    if (points.length === 0) {
      container.appendChild(el("div", { class: "chart-empty" }, "No data in this range."));
      return;
    }

    const maxV = niceMax(Math.max(...points.map((p) => p.count)));
    const n = points.length;
    const xFor = (i) => (n <= 1 ? (PLOT_LEFT + PLOT_RIGHT) / 2 : PLOT_LEFT + (i / (n - 1)) * (PLOT_RIGHT - PLOT_LEFT));
    const yFor = (v) => PLOT_BOTTOM - (v / maxV) * (PLOT_BOTTOM - PLOT_TOP);

    const svg = svgEl("svg", { class: "trend-svg", viewBox: `0 0 ${SVG_W} ${SVG_H}`, role: "img", "aria-label": title });

    // gridlines + y labels (0, mid, max)
    [0, maxV / 2, maxV].forEach((v) => {
      const y = yFor(v);
      svg.appendChild(svgEl("line", { class: "trend-gridline", x1: PLOT_LEFT, x2: PLOT_RIGHT, y1: y, y2: y }));
      const t = svgEl("text", { class: "trend-axis-label", x: PLOT_LEFT - 8, y: y + 3, "text-anchor": "end" });
      t.textContent = formatCompact(Math.round(v));
      svg.appendChild(t);
    });

    // x labels (subset)
    pickTickIndices(n, 6).forEach((i) => {
      const t = svgEl("text", { class: "trend-axis-label", x: xFor(i), y: PLOT_BOTTOM + 16, "text-anchor": "middle" });
      t.textContent = formatBucketLabel(points[i].bucket, data.granularity);
      svg.appendChild(t);
    });

    // area + line
    const linePath = points.map((p, i) => `${i === 0 ? "M" : "L"}${xFor(i)},${yFor(p.count)}`).join(" ");
    const areaPath = `${linePath} L${xFor(n - 1)},${PLOT_BOTTOM} L${xFor(0)},${PLOT_BOTTOM} Z`;
    svg.appendChild(svgEl("path", { class: "trend-area", d: areaPath }));
    svg.appendChild(svgEl("path", { class: "trend-line", d: linePath }));

    // hover layer: crosshair + dot, tracked via one transparent hit rect
    const crosshair = svgEl("line", { class: "trend-crosshair", x1: 0, x2: 0, y1: PLOT_TOP, y2: PLOT_BOTTOM, opacity: 0 });
    const dot = svgEl("circle", { class: "trend-dot", r: 4, opacity: 0 });
    svg.appendChild(crosshair);
    svg.appendChild(dot);

    let focusedIndex = n - 1;
    function showAt(i, clientX, clientY) {
      i = Math.max(0, Math.min(n - 1, i));
      focusedIndex = i;
      const x = xFor(i), y = yFor(points[i].count);
      crosshair.setAttribute("x1", x);
      crosshair.setAttribute("x2", x);
      crosshair.setAttribute("opacity", 1);
      dot.setAttribute("cx", x);
      dot.setAttribute("cy", y);
      dot.setAttribute("opacity", 1);
      const rect = svg.getBoundingClientRect();
      const px = clientX != null ? clientX : rect.left + (x / SVG_W) * rect.width;
      const py = clientY != null ? clientY : rect.top + (y / SVG_H) * rect.height;
      showTooltip(px + window.scrollX, py + window.scrollY, formatCompact(points[i].count), formatBucketLabel(points[i].bucket, data.granularity));
    }
    function hideHover() {
      crosshair.setAttribute("opacity", 0);
      dot.setAttribute("opacity", 0);
      hideTooltip();
    }

    const hitArea = svgEl("rect", {
      class: "trend-hit-area",
      x: PLOT_LEFT, y: PLOT_TOP, width: PLOT_RIGHT - PLOT_LEFT, height: PLOT_BOTTOM - PLOT_TOP,
      tabindex: "0",
    });
    hitArea.addEventListener("pointermove", (e) => {
      const rect = svg.getBoundingClientRect();
      const relX = ((e.clientX - rect.left) / rect.width) * SVG_W;
      const nearest = n <= 1 ? 0 : Math.round(((relX - PLOT_LEFT) / (PLOT_RIGHT - PLOT_LEFT)) * (n - 1));
      showAt(nearest, e.clientX, e.clientY);
    });
    hitArea.addEventListener("pointerleave", hideHover);
    hitArea.addEventListener("focus", () => showAt(focusedIndex));
    hitArea.addEventListener("blur", hideHover);
    hitArea.addEventListener("keydown", (e) => {
      if (e.key === "ArrowLeft") { e.preventDefault(); showAt(focusedIndex - 1); }
      else if (e.key === "ArrowRight") { e.preventDefault(); showAt(focusedIndex + 1); }
    });
    svg.appendChild(hitArea);

    container.appendChild(el("div", { class: "trend-svg-wrap" }, svg));

    const tableWrap = el("div", { class: "chart-table-wrap hidden" });
    const table = el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, "Period"), el("th", {}, unit))),
      el("tbody", {}, points.map((p) => el("tr", {}, el("td", {}, formatBucketLabel(p.bucket, data.granularity)), el("td", { class: "num" }, String(p.count))))));
    tableWrap.appendChild(table);
    container.appendChild(tableWrap);

    const wrap = container.querySelector(".trend-svg-wrap");
    header.querySelector(".table-toggle-btn").addEventListener("click", (e) => {
      const showingTable = !tableWrap.classList.contains("hidden");
      tableWrap.classList.toggle("hidden", showingTable);
      wrap.classList.toggle("hidden", !showingTable);
      e.target.textContent = showingTable ? "Table view" : "Chart view";
    });
  }

  // Shared with rto_delivered.js (the RTO Delivered tab reuses these cards).
  window.RtoCharts = { el, formatCompact, renderBarCard, renderTrendCard };

  // ---------- load + render ----------
  let loadToken = 0;
  async function loadAndRender() {
    const myToken = ++loadToken;
    analyticsBody.classList.add("loading");
    try {
      const params = new URLSearchParams({ date_field: state.dateField });
      if (state.start) params.set("from", state.start);
      if (state.end) params.set("to", state.end);
      const data = await fetchJson(`/api/analytics?${params.toString()}`);
      if (myToken !== loadToken) return; // a newer request already superseded this one

      if (data.total === 0) {
        analyticsBody.classList.add("hidden");
        analyticsEmpty.classList.remove("hidden");
      } else {
        analyticsBody.classList.remove("hidden");
        analyticsEmpty.classList.add("hidden");
        renderKpis(data);
        renderTrendCard(cardTrend, data);
        renderBarCard(cardFacility, "Facility", data.by_facility);
        renderBarCard(cardCourier, "Courier", data.by_courier);
        renderBarCard(cardSku, "SKU", data.by_sku);
      }
    } catch (err) {
      if (myToken !== loadToken) return;
      analyticsBody.classList.remove("hidden");
      analyticsEmpty.classList.add("hidden");
      kpiRow.textContent = "";
      [cardTrend, cardFacility, cardCourier, cardSku].forEach((c) => (c.textContent = ""));
      cardTrend.appendChild(el("div", { class: "chart-empty" }, "Could not load analytics: " + err.message));
    } finally {
      if (myToken === loadToken) analyticsBody.classList.remove("loading");
    }
  }

  updateRangeLabel();
  loadAndRender();

  // Exposed so app.js can ask for a refresh right after a new entry is saved.
  window.RTOAnalytics = { refresh: loadAndRender };
})();
