// RTO Analytics -> "RTO Process" tab: charts built around the entries logged in the RTO Process form.
// Data comes from /api/analytics (analytics.py). The chart renderers live in analytics_kit.js.
(function () {
  "use strict";

  const K = window.AnalyticsKit;
  const {
    el, fetchJson, formatCompact, computePresetRange, PRESET_LABELS, tooltipAtElement, hideTooltip,
    cardHeader, segmented, emptyNote, subline, fillSelect, renderKpis, renderStackedTrend, renderBarCard,
    renderMatrix, renderHistogram, renderChecks, pickSub, colorMap,
  } = K;

  const DAMAGE_TYPES = ["RTO", "RPU", "WH Damage", "Inward Damage"];
  // One colour per damage type, used by every chart so the meaning carries across cards.
  const TYPE_COLOR = {
    RTO: "var(--chart-series-1)",
    RPU: "var(--chart-series-2)",
    "WH Damage": "var(--chart-series-3)",
    "Inward Damage": "var(--chart-series-4)",
  };
  const TYPE_RGB = { RTO: "42,120,214", RPU: "235,104,52", "WH Damage": "27,175,122", "Inward Damage": "237,161,0" };

  // ---------- state ----------
  const initial = computePresetRange("30d");
  const state = {
    preset: "30d",
    start: initial.start,
    end: initial.end,
    dateField: "damage_marked_date",
    damageType: "",
    facility: "",
    courier: "",
    matrixBy: "facility",
    coverageBy: "facility",
  };
  let lastData = null;

  // ---------- elements ----------
  const $ = (id) => document.getElementById(id);
  const body = $("analytics-body");
  const emptyHint = $("analytics-empty");
  const kpiRow = $("kpi-row");
  const cards = {
    trend: $("card-trend"),
    trendFacility: $("card-trend-facility"),
    type: $("card-type"),
    facility: $("card-facility"),
    courier: $("card-courier"),
    sku: $("card-sku"),
    matrix: $("card-matrix"),
    lag: $("card-lag"),
    coverage: $("card-coverage"),
    checks: $("card-checks"),
  };
  const trigger = $("date-range-trigger");
  const popover = $("date-range-popover");
  const rangeLabel = $("date-range-label");
  const selType = $("an-filter-type");
  const selFacility = $("an-filter-facility");
  const selCourier = $("an-filter-courier");
  const selDateField = $("date-field-select");

  // ---------- filters ----------
  function updateRangeLabel() {
    rangeLabel.textContent = state.preset === "custom" ? `${state.start} → ${state.end}` : PRESET_LABELS[state.preset] || "Custom range";
  }

  function openPopover() {
    popover.classList.remove("hidden");
    trigger.setAttribute("aria-expanded", "true");
  }
  function closePopover() {
    popover.classList.add("hidden");
    trigger.setAttribute("aria-expanded", "false");
  }

  trigger.addEventListener("click", () => (popover.classList.contains("hidden") ? openPopover() : closePopover()));
  document.addEventListener("click", (e) => {
    if (!popover.contains(e.target) && !trigger.contains(e.target)) closePopover();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closePopover();
  });

  document.querySelectorAll("#tab-analytics-process .preset-row").forEach((btn) => {
    btn.addEventListener("click", () => {
      const range = computePresetRange(btn.dataset.preset);
      state.preset = btn.dataset.preset;
      state.start = range.start;
      state.end = range.end;
      document.querySelectorAll("#tab-analytics-process .preset-row").forEach((b) => b.classList.toggle("active", b === btn));
      updateRangeLabel();
      closePopover();
      loadAndRender();
    });
  });

  $("btn-apply-custom-range").addEventListener("click", () => {
    const from = $("range-from").value;
    const to = $("range-to").value;
    if (!from || !to || from > to) return; // silently ignore an incomplete/inverted range
    state.preset = "custom";
    state.start = from;
    state.end = to;
    document.querySelectorAll("#tab-analytics-process .preset-row").forEach((b) => b.classList.remove("active"));
    updateRangeLabel();
    closePopover();
    loadAndRender();
  });

  function setFilter(key, value) {
    state[key] = value;
    loadAndRender();
  }

  selDateField.addEventListener("change", () => setFilter("dateField", selDateField.value));
  selType.addEventListener("change", () => setFilter("damageType", selType.value));
  selFacility.addEventListener("change", () => setFilter("facility", selFacility.value));
  selCourier.addEventListener("change", () => setFilter("courier", selCourier.value));
  $("btn-refresh-analytics").addEventListener("click", loadAndRender);
  $("btn-an-clear").addEventListener("click", () => {
    state.damageType = state.facility = state.courier = "";
    loadAndRender();
  });

  function syncFilterControls(data) {
    selType.value = state.damageType;
    selDateField.value = state.dateField;
    fillSelect(selFacility, data.options.facilities, state.facility, "All facilities");
    fillSelect(selCourier, data.options.couriers, state.courier, "All couriers");
    const active = state.damageType || state.facility || state.courier;
    $("btn-an-clear").classList.toggle("hidden", !active);
  }

  // ---------- KPI row ----------
  function kpis(data) {
    const lag = data.lag;
    const cov = data.coverage;
    const covValue = cov.available && cov.rate !== null ? `${cov.rate}%` : cov.loading ? "…" : "–";
    return [
      { label: "Total entries", value: formatCompact(data.total), accent: "var(--chart-baseline)" },
      { label: "Distinct AWBs", value: formatCompact(data.distinct.awb), accent: "var(--chart-series-1)" },
      { label: "Distinct SKUs", value: formatCompact(data.distinct.sku), accent: "var(--chart-series-3)" },
      { label: "Avg days to receive", value: lag.avg_days === null ? "–" : String(lag.avg_days), accent: "var(--chart-series-4)" },
      { label: "Damage logged rate", value: covValue, accent: "var(--chart-series-2)" },
      { label: "Repeat AWBs", value: formatCompact(data.repeat_awbs.count), accent: data.repeat_awbs.count ? "#b45309" : "var(--chart-baseline)" },
    ];
  }

  // ---------- stacked trends ----------
  const basisText = (field) => (field === "created_at" ? "logged date" : field === "received_date" ? "received date" : "damage marked date");

  function renderTrends(data) {
    renderStackedTrend({
      card: cards.trend,
      title: "Entries over time",
      sub: `By ${basisText(data.date_field)} · grouped by ${data.granularity} · stacked by damage type`,
      ariaLabel: "Entries over time, stacked by damage type",
      series: DAMAGE_TYPES,
      colorOf: (name) => TYPE_COLOR[name],
      points: data.trend_by_type.map((p) => ({ bucket: p.bucket, total: p.total, values: p.types })),
      granularity: data.granularity,
      empty: "No dated entries in this range.",
    });
    const fac = data.trend_by_facility;
    const colors = colorMap(fac.series);
    renderStackedTrend({
      card: cards.trendFacility,
      title: "Entries over time by facility",
      sub: `By ${basisText(data.date_field)} · grouped by ${data.granularity} · stacked by facility` +
        (fac.series.includes("Other") ? " · smaller facilities grouped as Other" : ""),
      ariaLabel: "Entries over time, stacked by facility",
      series: fac.series,
      colorOf: (name) => colors[name],
      points: fac.points,
      granularity: data.granularity,
      empty: "No dated entries in this range.",
    });
  }

  function renderTypeCard(data) {
    const rows = DAMAGE_TYPES.map((t) => ({ label: t, count: data.by_damage_type[t] || 0 })).filter((r) => r.count > 0)
      .sort((a, b) => b.count - a.count);
    renderBarCard(cards.type, "Damage type", rows, {
      colorFor: (r) => TYPE_COLOR[r.label],
      valueText: (r, total) => `${formatCompact(r.count)} · ${Math.round((100 * r.count) / total)}%`,
      selected: state.damageType,
      onPick: (r) => setFilter("damageType", state.damageType === r.label ? "" : r.label),
      sub: "Click a bar to filter",
    });
  }

  // ---------- facility / courier x damage type matrix ----------
  function renderTypeMatrix(data) {
    const by = state.matrixBy;
    const rows = (by === "facility" ? data.facility_by_type : data.courier_by_type).map((r) => ({ label: r.label, total: r.total, values: r.types }));
    renderMatrix({
      card: cards.matrix,
      title: `${by === "facility" ? "Facility" : "Courier"} × damage type`,
      sub: "Where each kind of damage is being logged. Darker = more entries.",
      firstHeader: by === "facility" ? "Facility" : "Courier",
      data: { columns: DAMAGE_TYPES, rows },
      colorOf: (t) => TYPE_COLOR[t],
      rgbOf: (t) => TYPE_RGB[t],
      toggles: segmented([["facility", "Facility"], ["courier", "Courier"]], by, (v) => {
        state.matrixBy = v;
        renderTypeMatrix(data);
      }),
    });
  }

  // ---------- receipt lag histogram ----------
  function renderLag(data) {
    const lag = data.lag;
    renderHistogram({
      card: cards.lag,
      title: "Days to receive",
      sub: "Damage marked → RTO/RPU received",
      hist: { n: lag.n, avg: lag.avg_days, median: lag.median_days, max: lag.max_days, buckets: lag.buckets },
      empty: "No entries with both dates.",
      color: "var(--chart-series-4)",
    });
  }

  // ---------- coverage: damage logged vs RTO delivered ----------
  function renderCoverage(data) {
    const card = cards.coverage;
    card.textContent = "";
    const cov = data.coverage;
    const by = state.coverageBy;
    const toggles = cov.available ? segmented([["facility", "Facility"], ["courier", "Courier"]], by, (v) => {
      state.coverageBy = v;
      renderCoverage(data);
    }) : null;
    card.appendChild(cardHeader("Damage logged rate", toggles));
    card.appendChild(subline("Share of RTO-Delivered AWBs in this range that have damage logged"));
    if (cov.loading) {
      card.appendChild(emptyNote("The RTO Delivered data is still loading. Refresh in a minute."));
      return;
    }
    if (!cov.available) {
      card.appendChild(emptyNote("RTO Delivered data is not available right now."));
      return;
    }
    if (!cov.delivered) {
      card.appendChild(emptyNote("No RTO-Delivered AWBs in this range."));
      return;
    }
    card.appendChild(el("div", { class: "cov-headline" },
      el("span", { class: "cov-rate" }, `${cov.rate}%`),
      el("span", { class: "cov-detail" }, `${cov.logged.toLocaleString()} of ${cov.delivered.toLocaleString()} AWBs`)));

    const rows = (by === "facility" ? cov.by_facility : cov.by_courier).slice(0, 10);
    const list = el("div", { class: "hbar-list" }, rows.map((r) => {
      const row = el("div", { class: "hbar-row", title: r.label },
        el("div", { class: "hbar-label" }, r.label),
        el("div", { class: "hbar-track cov-track" }, el("div", { class: "hbar-fill", style: `width:${Math.max(r.rate, 1.5)}%;background:var(--chart-series-2)` })),
        el("div", { class: "hbar-value" }, `${r.rate}%`));
      row.addEventListener("mouseenter", () => tooltipAtElement(row, `${r.logged} of ${r.delivered} AWBs`, r.label));
      row.addEventListener("mouseleave", hideTooltip);
      return row;
    }));
    card.appendChild(list);
    card.appendChild(el("p", { class: "chart-card-sub cov-note" }, "Matches by AWB, so it ignores the damage type filter."));
  }

  // ---------- data checks ----------
  function renderProcessChecks(data) {
    const repeats = data.repeat_awbs;
    const early = data.lag.received_before_marked;
    renderChecks(cards.checks, [
      {
        count: repeats.count,
        text: `AWB${repeats.count === 1 ? "" : "s"} logged more than once`,
        list: repeats.top.map((r) => [r.awb, `${r.count}×`]),
      },
      { count: early, text: `entr${early === 1 ? "y" : "ies"} received before the damage was marked` },
    ], "Entries worth a second look");
  }

  // ---------- load + render ----------
  function renderAll(data) {
    renderKpis(kpiRow, kpis(data));
    renderTrends(data);
    renderTypeCard(data);
    renderBarCard(cards.facility, "Facility", data.by_facility, {
      selected: state.facility,
      onPick: (r) => setFilter("facility", state.facility === r.label ? "" : r.label),
      sub: pickSub(data.by_facility),
    });
    renderBarCard(cards.courier, "Courier", data.by_courier, {
      selected: state.courier,
      onPick: (r) => setFilter("courier", state.courier === r.label ? "" : r.label),
      sub: pickSub(data.by_courier),
    });
    renderBarCard(cards.sku, "SKU", data.by_sku);
    renderTypeMatrix(data);
    renderLag(data);
    renderCoverage(data);
    renderProcessChecks(data);
  }

  let loadToken = 0;
  async function loadAndRender() {
    const myToken = ++loadToken;
    body.classList.add("loading");
    try {
      const params = new URLSearchParams({ date_field: state.dateField });
      if (state.start) params.set("from", state.start);
      if (state.end) params.set("to", state.end);
      if (state.damageType) params.set("damage_type", state.damageType);
      if (state.facility) params.set("facility", state.facility);
      if (state.courier) params.set("courier", state.courier);
      $("btn-an-download").href = `/api/analytics/export?${params.toString()}`;
      const data = await fetchJson(`/api/analytics?${params.toString()}`);
      if (myToken !== loadToken) return; // a newer request already superseded this one
      lastData = data;
      syncFilterControls(data);
      if (data.total === 0) {
        body.classList.add("hidden");
        emptyHint.classList.remove("hidden");
      } else {
        body.classList.remove("hidden");
        emptyHint.classList.add("hidden");
        renderAll(data);
      }
    } catch (err) {
      if (myToken !== loadToken) return;
      body.classList.remove("hidden");
      emptyHint.classList.add("hidden");
      kpiRow.textContent = "";
      Object.values(cards).forEach((c) => (c.textContent = ""));
      cards.trend.appendChild(emptyNote("Could not load analytics: " + err.message));
    } finally {
      if (myToken === loadToken) body.classList.remove("loading");
    }
  }

  // Tab switching for every tab under RTO Analytics (the other tabs load themselves when opened).
  document.querySelectorAll("#analytics-tabs .tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#analytics-tabs .tab-btn").forEach((b) => {
        b.classList.toggle("active", b === btn);
        b.setAttribute("aria-selected", b === btn ? "true" : "false");
      });
      document.querySelectorAll("#panel-rto-analytics > .tab-panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + btn.dataset.tab));
    });
  });

  // Reopening the panel shows what was logged since (entries are added in the RTO Process panel).
  document.querySelector('.side-nav-item[data-panel="rto-analytics"]').addEventListener("click", () => {
    if (lastData && document.getElementById("tab-analytics-process").classList.contains("active")) loadAndRender();
  });
  // Its RTO Delivered coverage card reads the RTO Status data: redraw when that is re-pulled (data_refresh.js).
  window.addEventListener("panel:data-changed", (e) => {
    if (lastData && e.detail.sources.includes("rto_delivered")) loadAndRender();
  });

  window.RtoCharts = Object.assign(window.RtoCharts || {}, { el, formatCompact });
  // Exposed so app.js can ask for a refresh right after a new entry is saved.
  window.RTOAnalytics = { refresh: loadAndRender };

  updateRangeLabel();
  loadAndRender();
})();
