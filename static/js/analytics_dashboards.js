// RTO Analytics -> RTO Status, RTO MDND and RTO Reconciliation tabs.
// Each is a config for AnalyticsKit.createDashboard: its filters, KPI tiles and cards. The data comes from
// /api/analytics/{status,mdnd,recon} (analytics_status.py, analytics_mdnd.py, analytics_recon.py), which
// read the same cached data the RTO Status, RTO MDND and RTO Reconciliation tables show.
(function () {
  "use strict";

  const K = window.AnalyticsKit;
  const {
    el, formatCompact, formatRupees, formatRupeesFull, colorMap, hexToRgb, segmented, cardHeader, subline, emptyNote,
    tooltipAtElement, hideTooltip, renderStackedTrend, renderBarCard, renderMatrix, renderHistogram, renderChecks, pickSub,
  } = K;

  const GREEN = "#1baf7a", AMBER = "#eda100", BLUE = "#2a78d6", PINK = "#d6477f", ORANGE = "#eb6834", GREY = "#b9b4a3";
  const WARN = "#b45309";
  const AWB = ["AWB", "AWBs"];
  const pct = (v) => (v === null || v === undefined ? "–" : `${v}%`);
  const num = (v) => (v === null || v === undefined ? "–" : String(v));

  // The ERP's Delivery Note statuses have a meaning, so they get fixed colours; unknown ones take the palette.
  const STATUS_COLORS = {
    "Return Completed": GREEN,
    "Return Initiated": AMBER,
    "Return Issued": BLUE,
    Cancel: PINK,
    "(No status)": GREY,
  };

  // Rows are clickable bars that set a filter; clicking the selected one clears it.
  function pickBars(ctx, card, title, rows, filterKey, extra) {
    renderBarCard(card, title, rows, {
      selected: ctx.state.filters[filterKey],
      onPick: (r) => ctx.setFilter(filterKey, ctx.state.filters[filterKey] === r.label ? "" : r.label),
      sub: pickSub(rows),
      unit: "AWBs",
      ...extra,
    });
  }

  function trendCard(card, trend, cfg) {
    const colors = colorMap(trend.series, cfg.fixed);
    renderStackedTrend({
      card, title: cfg.title, ariaLabel: cfg.title, series: trend.series, colorOf: (n) => colors[n], points: trend.points,
      granularity: cfg.granularity, unit: AWB, empty: cfg.empty || "No dated AWBs in this range.",
      sub: `By ${cfg.basis} · grouped by ${cfg.granularity} · ${cfg.stack}`,
    });
  }

  // =====================================================================  RTO Status
  const STATUS_BASIS = { rto_delivery_date: "RTO delivery date", pickup_date: "pickup date", created_at: "shipment created date" };

  K.createDashboard({
    id: "status",
    what: "RTO Status",
    endpoint: "/api/analytics/status",
    unit: AWB,
    dateFields: [["rto_delivery_date", "By: RTO delivery date"], ["pickup_date", "By: Pickup date"], ["created_at", "By: Created at"]],
    filters: [
      { key: "returnName", param: "return_name", all: "All return names", options: (d) => d.options.return_names },
      { key: "courier", param: "courier", all: "All couriers", options: (d) => d.options.couriers },
      { key: "status", param: "status", all: "All ERP statuses", options: (d) => d.options.statuses },
    ],
    layout: ["trend-status", "trend-return", { grid: ["status", "return-name", "courier", "city", "remark", "transit", { id: "matrix", span2: true }, "checks"] }],
    kpis(d) {
      const k = d.kpis;
      return [
        { label: "RTO delivered", value: formatCompact(k.awbs), accent: "var(--chart-baseline)", hint: "AWBs the courier reports as RTO-Delivered" },
        { label: "ERP completed", value: pct(k.completed_rate), accent: GREEN, hint: `${k.completed.toLocaleString()} AWBs whose latest Delivery Note is Return Completed` },
        { label: "Not yet completed", value: formatCompact(k.not_completed), accent: k.not_completed ? WARN : GREEN, hint: "Delivered, but the latest Delivery Note is not Return Completed (or there is none)" },
        { label: "No Delivery Note", value: formatCompact(k.no_status), accent: k.no_status ? WARN : "var(--chart-baseline)", hint: "Delivered AWBs with no Delivery Note in the ERP" },
        { label: "Avg days in transit", value: num(k.avg_transit_days), accent: AMBER, hint: "Pickup → RTO delivered" },
        { label: "Return names", value: formatCompact(k.return_names), accent: BLUE, hint: `${k.couriers} couriers` },
      ];
    },
    render(d, ctx) {
      const basis = STATUS_BASIS[d.date_field];
      const g = d.granularity;
      trendCard(ctx.cards["trend-status"], d.trend_by_status, { title: "RTO delivered over time", basis, granularity: g, stack: "stacked by ERP status", fixed: STATUS_COLORS });
      trendCard(ctx.cards["trend-return"], d.trend_by_return_name, { title: "RTO delivered over time by return name", basis, granularity: g, stack: "stacked by return name" });
      pickBars(ctx, ctx.cards.status, "ERP status", d.by_status, "status", { colorFor: (r) => STATUS_COLORS[r.label] || "var(--chart-series-1)" });
      pickBars(ctx, ctx.cards["return-name"], "Return name", d.by_return_name, "returnName");
      pickBars(ctx, ctx.cards.courier, "Courier", d.by_courier, "courier");
      renderBarCard(ctx.cards.city, "Return city", d.by_city, { unit: "AWBs" });
      renderBarCard(ctx.cards.remark, "Latest courier remark", d.by_remark, { unit: "AWBs", sub: `Top ${d.by_remark.length}` });
      renderHistogram({
        card: ctx.cards.transit, title: "Days in transit", sub: "Pickup → RTO delivered", hist: d.transit, unit: AWB,
        empty: "No AWBs with both dates.", color: "var(--chart-series-4)",
        note: d.transit.negative ? `${d.transit.negative} AWB${d.transit.negative === 1 ? "" : "s"} with a delivery date before the pickup date are left out.` : "",
      });

      const by = ctx.state.ui.matrixBy || "courier";
      const colors = colorMap(d.courier_by_status.columns, STATUS_COLORS);
      renderMatrix({
        card: ctx.cards.matrix,
        title: `${by === "courier" ? "Courier" : "Return name"} × ERP status`,
        sub: "Where the ERP stands for each group. Darker = more AWBs.",
        firstHeader: by === "courier" ? "Courier" : "Return name",
        data: by === "courier" ? d.courier_by_status : d.return_name_by_status,
        colorOf: (c) => colors[c], rgbOf: (c) => hexToRgb(colors[c]),
        toggles: segmented([["courier", "Courier"], ["return", "Return name"]], by, (v) => { ctx.state.ui.matrixBy = v; ctx.rerender(); }),
      });

      const c = d.checks;
      renderChecks(ctx.cards.checks, [
        {
          count: c.stuck,
          text: `AWB${c.stuck === 1 ? "" : "s"} delivered ${c.stuck_after_days}+ days ago, still not Return Completed in the ERP`,
          list: c.oldest.map((r) => [`${r.awb} · ${r.status}`, `${r.days}d`]),
        },
        { count: c.stuck_no_note, text: `of those ${c.stuck_no_note === 1 ? "has" : "have"} no Delivery Note at all` },
      ], "Delivered AWBs the ERP has not caught up with");
    },
  });

  // =====================================================================  RTO MDND
  const MDND_BASIS = { received_on: "received date", marked_on: "RTO marked date" };
  const CHECK_COLORS = { Checked: GREEN, Pending: ORANGE };
  const RECEIVED_COLORS = { Yes: GREEN, No: PINK, "Received and Damaged": ORANGE, "(Not filled)": GREY };

  function renderScorecard(d, ctx) {
    const card = ctx.cards.scorecard;
    card.textContent = "";
    card.appendChild(cardHeader("Warehouse scorecard"));
    card.appendChild(subline("How much came back to each warehouse, what it is worth, and how much has been checked. Click a row to filter."));
    const maxCount = Math.max(1, ...d.scorecard.map((s) => s.count));
    const selected = ctx.state.filters.warehouse;
    const head = ["Warehouse", "AWBs", "Checked", "Pending", "Net value", "Pending value"];
    const table = el("table", { class: "score-table" },
      el("thead", {}, el("tr", {}, head.map((h, i) => el("th", i ? { class: "num" } : {}, h)))),
      el("tbody", {}, d.scorecard.map((s) => {
        const row = el("tr", { class: "clickable" + (selected === s.label ? " selected" : ""), title: s.label },
          el("td", { class: "matrix-label" }, s.label),
          el("td", { class: "num" }, el("div", { class: "score-cell" },
            el("span", { class: "score-bar" }, el("span", { class: "score-fill", style: `width:${Math.max((s.count / maxCount) * 100, 3)}%` })),
            s.count.toLocaleString())),
          el("td", { class: "num" }, el("div", { class: "score-cell" },
            el("span", { class: "score-bar" }, el("span", { class: "score-fill done", style: `width:${s.checked_rate}%` })),
            `${s.checked_rate}%`)),
          el("td", { class: "num" }, s.pending.toLocaleString()),
          el("td", { class: "num", title: formatRupeesFull(s.net_value) }, formatRupees(s.net_value)),
          el("td", { class: "num", title: formatRupeesFull(s.pending_value) }, formatRupees(s.pending_value)));
        row.addEventListener("click", () => ctx.setFilter("warehouse", selected === s.label ? "" : s.label));
        return row;
      })));
    card.appendChild(el("div", { class: "matrix-wrap" }, table));
  }

  K.createDashboard({
    id: "mdnd",
    what: "RTO MDND",
    endpoint: "/api/analytics/mdnd",
    unit: AWB,
    dateFields: [["received_on", "By: Received date"], ["marked_on", "By: RTO marked date"]],
    filters: [
      { key: "warehouse", param: "warehouse", all: "All warehouses", options: (d) => d.options.warehouses },
      { key: "courier", param: "courier", all: "All couriers", options: (d) => d.options.couriers },
      { key: "reason", param: "reason", all: "All reasons", options: (d) => d.options.reasons },
      { key: "state", param: "state", all: "All checks", options: () => [["checked", "Checked"], ["pending", "Pending check"]] },
    ],
    layout: ["trend-warehouse", "trend-state", { grid: [{ id: "scorecard", span2: true }, "checks", "courier", "reason", "days", "received", "condition", "checker", "city"] }],
    kpis(d) {
      const k = d.kpis;
      return [
        { label: "AWBs received", value: formatCompact(k.awbs), accent: "var(--chart-baseline)", hint: "RTO-Delivered AWBs with a Delivery Note value" },
        { label: "Net value", value: formatRupees(k.net_value), accent: BLUE, hint: formatRupeesFull(k.net_value) },
        { label: "Checked", value: pct(k.checked_rate), accent: GREEN, hint: `${k.checked.toLocaleString()} AWBs with "Received at WH?" filled in` },
        { label: "Pending check", value: formatCompact(k.pending), accent: k.pending ? ORANGE : GREEN, hint: "AWBs still waiting for a warehouse check" },
        { label: "Pending value", value: formatRupees(k.pending_value), accent: k.pending ? WARN : "var(--chart-baseline)", hint: formatRupeesFull(k.pending_value) },
        { label: "Damaged on receipt", value: formatCompact(k.damaged), accent: PINK, hint: 'Received and Damaged, or Condition = Damaged' },
      ];
    },
    render(d, ctx) {
      const basis = MDND_BASIS[d.date_field];
      const g = d.granularity;
      trendCard(ctx.cards["trend-warehouse"], d.trend_by_warehouse, { title: "AWBs received over time", basis, granularity: g, stack: "stacked by warehouse" });
      trendCard(ctx.cards["trend-state"], d.trend_by_state, { title: "Checked vs pending over time", basis, granularity: g, stack: "stacked by check status", fixed: CHECK_COLORS });
      renderScorecard(d, ctx);
      pickBars(ctx, ctx.cards.courier, "Courier", d.by_courier, "courier");
      pickBars(ctx, ctx.cards.reason, "RTO reason", d.by_reason, "reason");
      renderHistogram({
        card: ctx.cards.days, title: "Days to receipt", sub: "RTO marked → received at the warehouse", hist: d.days_to_receipt, unit: AWB,
        empty: "No AWBs with both dates.", color: "var(--chart-series-1)",
      });
      renderBarCard(ctx.cards.received, "Received at WH?", d.by_received_at_wh, {
        unit: "AWBs", sub: "Answers typed in the MDND tab", colorFor: (r) => RECEIVED_COLORS[r.label] || "var(--chart-series-1)",
        valueText: (r, total) => `${formatCompact(r.count)} · ${Math.round((100 * r.count) / total)}%`,
      });
      renderBarCard(ctx.cards.condition, "Condition", d.by_condition, {
        unit: "AWBs", sub: "Among checked AWBs", colorFor: (r) => (r.label === "Good" ? GREEN : r.label === "Damaged" ? PINK : GREY),
      });
      renderBarCard(ctx.cards.checker, "Checked by", d.by_checker, { unit: "AWBs", sub: "Checks completed per person" });
      renderBarCard(ctx.cards.city, "Drop city", d.by_city, { unit: "AWBs", sub: `Top ${d.by_city.length} destinations` });

      const c = d.checks;
      renderChecks(ctx.cards.checks, [
        {
          count: c.overdue,
          text: `AWB${c.overdue === 1 ? "" : "s"} received ${c.overdue_days}+ days ago and still not checked (${formatRupees(c.overdue_value)})`,
          list: c.oldest_pending.map((r) => [`${r.awb} · ${r.warehouse}`, `${r.days}d · ${formatRupees(r.net_value)}`]),
        },
        { count: c.with_notes, text: `AWB${c.with_notes === 1 ? "" : "s"} with discrepancy notes` },
      ], "Items that need follow-up");
    },
  });

  // =====================================================================  RTO Reconciliation
  const RECON_BASIS = { rto_mark_date: "RTO mark date", created_at: "shipment created date", pickup_date: "pickup date" };
  const AGE_COLORS = ["#f2c14e", "#f08a4b", "#e0553c", "#b83a3a", "#8c2f5a", "#5a2a6e"];

  K.createDashboard({
    id: "recon",
    what: "RTO Reconciliation",
    endpoint: "/api/analytics/recon",
    unit: AWB,
    dateFields: [["rto_mark_date", "By: RTO mark date"], ["created_at", "By: Created at"], ["pickup_date", "By: Pickup date"]],
    defaultPreset: "all",
    filters: [
      { key: "returnName", param: "return_name", all: "All return names", options: (d) => d.options.return_names },
      { key: "courier", param: "courier", all: "All couriers", options: (d) => d.options.couriers },
      { key: "erpStatus", param: "erp_status", all: "All ERP statuses", options: (d) => d.options.erp_statuses },
    ],
    layout: ["trend-courier", "trend-erp", { grid: ["erp", "courier", "return-name", "ageing", { id: "matrix", span2: true }, "checks"] }],
    kpis(d) {
      const k = d.kpis;
      return [
        { label: "Stuck AWBs", value: formatCompact(k.awbs), accent: "var(--chart-baseline)", hint: "Courier still says RTO-InTransit 10+ days after the RTO was marked" },
        { label: "Avg days pending", value: num(k.avg_days), accent: AMBER, hint: "Days since the RTO was marked" },
        { label: "Oldest", value: k.oldest_days === null ? "–" : `${k.oldest_days}d`, accent: PINK, hint: "Longest-pending AWB" },
        { label: `${d.checks.long_pending_days}+ days`, value: formatCompact(k.long_pending), accent: k.long_pending ? WARN : GREEN, hint: `${pct(k.long_pending_rate)} of these AWBs` },
        { label: "ERP shows completed", value: formatCompact(k.erp_completed), accent: GREEN, hint: "Latest Delivery Note is Return Completed although the courier still shows in transit" },
        { label: "No ERP status", value: formatCompact(k.no_erp_status), accent: k.no_erp_status ? WARN : "var(--chart-baseline)", hint: "No Delivery Note found for the AWB" },
      ];
    },
    render(d, ctx) {
      const basis = RECON_BASIS[d.date_field];
      const g = d.granularity;
      trendCard(ctx.cards["trend-courier"], d.trend_by_courier, { title: "Stuck AWBs by RTO mark date", basis, granularity: g, stack: "stacked by courier" });
      trendCard(ctx.cards["trend-erp"], d.trend_by_erp_status, { title: "Stuck AWBs by RTO mark date and ERP status", basis, granularity: g, stack: "stacked by ERP status", fixed: STATUS_COLORS });
      pickBars(ctx, ctx.cards.erp, "ERP status", d.by_erp_status, "erpStatus", { colorFor: (r) => STATUS_COLORS[r.label] || "var(--chart-series-1)" });
      pickBars(ctx, ctx.cards.courier, "Courier", d.by_courier, "courier");
      pickBars(ctx, ctx.cards["return-name"], "Return name", d.by_return_name, "returnName");
      renderHistogram({
        card: ctx.cards.ageing, title: "How long they have been stuck", sub: "Days since the RTO was marked", hist: d.ageing, unit: AWB,
        empty: "No AWBs with a mark date.", color: "var(--chart-series-2)",
      });

      const by = ctx.state.ui.matrixBy || "courier";
      const data = by === "courier" ? d.courier_by_age : d.return_name_by_age;
      const colors = Object.fromEntries(data.columns.map((c, i) => [c, AGE_COLORS[i % AGE_COLORS.length]]));
      renderMatrix({
        card: ctx.cards.matrix,
        title: `${by === "courier" ? "Courier" : "Return name"} × age`,
        sub: "Who is holding the oldest AWBs. Darker = more AWBs.",
        firstHeader: by === "courier" ? "Courier" : "Return name",
        data, colorOf: (c) => colors[c], rgbOf: (c) => hexToRgb(colors[c]),
        toggles: segmented([["courier", "Courier"], ["return", "Return name"]], by, (v) => { ctx.state.ui.matrixBy = v; ctx.rerender(); }),
      });

      const c = d.checks;
      renderChecks(ctx.cards.checks, [
        {
          count: c.erp_completed,
          text: `AWB${c.erp_completed === 1 ? "" : "s"} where the ERP shows Return Completed but the courier still says in transit`,
          list: c.erp_completed_oldest.map((r) => [`${r.awb} · ${r.courier}`, `${r.days}d`]),
        },
        { count: c.long_pending_no_status, text: `AWB${c.long_pending_no_status === 1 ? "" : "s"} pending ${c.long_pending_days}+ days with no Delivery Note` },
        { count: c.has_delivery_date, text: `AWB${c.has_delivery_date === 1 ? "" : "s"} with a delivery date although still in transit` },
      ], "Where the courier and the ERP disagree");
    },
  });
})();
