// Shared building blocks for every RTO Analytics tab (RTO Process, RTO Status, RTO MDND, RTO Reconciliation):
// formatting helpers, the floating tooltip, SVG/DOM chart renderers and a small dashboard engine that
// builds a tab's filter bar, loads its API and renders its cards. Everything is drawn with DOM/SVG nodes
// and textContent, never innerHTML, so values from the database cannot inject markup.
(function () {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";
  const TOP_N_BARS = 8;
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const PRESET_LABELS = {
    today: "Today",
    "7d": "Last 7 days",
    "30d": "Last 30 days",
    "90d": "Last 90 days",
    mtd: "Month to date",
    all: "All time",
  };
  const PRESETS = ["today", "7d", "30d", "90d", "mtd", "all"];

  // Series colours for stacked charts. "Other" (the folded-in remainder) is neutral grey.
  const SERIES_COLORS = ["#7c5cc4", "#1597a5", "#d6477f", "#8a9a2b", "#a1673b", "#5b7083", "#3f51b5"];
  const OTHER_COLOR = "#b9b4a3";

  // ---------- helpers ----------
  async function fetchJson(url) {
    const res = await fetch(url);
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
    const node = document.createElementNS(SVG_NS, tag);
    for (const k in attrs || {}) node.setAttribute(k, attrs[k]);
    return node;
  }

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

  function formatCompact(n) {
    if (n < 10000) return n.toLocaleString();
    if (n < 1000000) return (n / 1000).toFixed(1).replace(/\.0$/, "") + "K";
    return (n / 1000000).toFixed(1).replace(/\.0$/, "") + "M";
  }

  // Rupees in lakh / crore, the way finance reads them: ₹72.4L, ₹1.2Cr.
  function formatRupees(n) {
    if (n >= 10000000) return "₹" + (n / 10000000).toFixed(2).replace(/\.?0+$/, "") + "Cr";
    if (n >= 100000) return "₹" + (n / 100000).toFixed(1).replace(/\.0$/, "") + "L";
    return "₹" + Math.round(n).toLocaleString("en-IN");
  }
  const formatRupeesFull = (n) => "₹" + Math.round(n).toLocaleString("en-IN");

  const pad = (n) => String(n).padStart(2, "0");
  const dateStr = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const todayStr = () => dateStr(new Date());

  function addDays(str, delta) {
    const d = new Date(str + "T00:00:00");
    d.setDate(d.getDate() + delta);
    return dateStr(d);
  }

  function computePresetRange(preset) {
    const today = todayStr();
    if (preset === "today") return { start: today, end: today };
    if (preset === "7d") return { start: addDays(today, -6), end: today };
    if (preset === "90d") return { start: addDays(today, -89), end: today };
    if (preset === "mtd") return { start: today.slice(0, 8) + "01", end: today };
    if (preset === "all") return { start: null, end: null };
    return { start: addDays(today, -29), end: today }; // 30d default
  }

  function formatBucketLabel(bucket, granularity) {
    const d = new Date(bucket + "T00:00:00");
    if (granularity === "month") return `${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
    if (granularity === "week") return `Week of ${MONTHS[d.getMonth()]} ${d.getDate()}`;
    return `${MONTHS[d.getMonth()]} ${d.getDate()}`;
  }

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

  function hexToRgb(hex) {
    const h = hex.replace("#", "");
    return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16)).join(",");
  }

  // name -> colour for a list of series: `fixed` colours win, "Other" is grey, the rest take the palette in order.
  function colorMap(series, fixed) {
    const out = {};
    let next = 0;
    series.forEach((name) => {
      if (fixed && fixed[name]) out[name] = fixed[name];
      else if (name === "Other") out[name] = OTHER_COLOR;
      else out[name] = SERIES_COLORS[next++ % SERIES_COLORS.length];
    });
    return out;
  }

  const plural = (n, unit) => (n === 1 ? unit[0] : unit[1]);

  // ---------- tooltip (one shared floating element) ----------
  const tooltip = el("div", { class: "chart-tooltip hidden" }, el("div", { class: "tt-value" }), el("div", { class: "tt-label" }));
  document.body.appendChild(tooltip);

  function showTooltip(x, y, valueText, labelText) {
    tooltip.querySelector(".tt-value").textContent = valueText;
    tooltip.querySelector(".tt-label").textContent = labelText;
    tooltip.style.left = x + "px";
    tooltip.style.top = y + "px";
    tooltip.classList.remove("hidden");
  }
  const hideTooltip = () => tooltip.classList.add("hidden");

  function tooltipAtElement(node, valueText, labelText) {
    const rect = node.getBoundingClientRect();
    showTooltip(rect.left + rect.width / 2 + window.scrollX, rect.top + window.scrollY, valueText, labelText);
  }

  // ---------- small building blocks ----------
  function cardHeader(title, toggles) {
    const header = el("div", { class: "chart-card-header" }, el("h3", {}, title));
    if (toggles) header.appendChild(toggles);
    return header;
  }

  // A "Table view / Chart view" switch between a chart node and its table node.
  function tableToggle(chartNode, tableNode) {
    const btn = el("button", { type: "button", class: "table-toggle-btn" }, "Table view");
    btn.addEventListener("click", () => {
      const showingTable = !tableNode.classList.contains("hidden");
      tableNode.classList.toggle("hidden", showingTable);
      chartNode.classList.toggle("hidden", !showingTable);
      btn.textContent = showingTable ? "Table view" : "Chart view";
    });
    return btn;
  }

  function simpleTable(headers, rows) {
    return el("div", { class: "chart-table-wrap hidden" },
      el("table", {},
        el("thead", {}, el("tr", {}, headers.map((h, i) => el("th", i ? { class: "num" } : {}, h)))),
        el("tbody", {}, rows.map((r) => el("tr", {}, r.map((c, i) => el("td", i ? { class: "num" } : {}, String(c))))))));
  }

  function segmented(options, current, onPick) {
    const wrap = el("div", { class: "seg" });
    options.forEach(([value, label]) => {
      const b = el("button", { type: "button", class: "seg-btn" + (value === current ? " active" : "") }, label);
      b.addEventListener("click", () => onPick(value));
      wrap.appendChild(b);
    });
    return wrap;
  }

  const emptyNote = (text) => el("div", { class: "chart-empty" }, text);
  const subline = (text) => el("p", { class: "chart-card-sub" }, text);

  function legend(series, colorOf) {
    return el("div", { class: "stack-legend" }, series.map((name) =>
      el("span", { class: "stack-legend-item" }, el("span", { class: "stack-swatch", style: `background:${colorOf(name)}` }), name)));
  }

  // values: strings, or [value, label] pairs; the selected value is kept even when it is not in the list.
  function fillSelect(select, values, selected, allLabel) {
    const pairs = values.map((v) => (Array.isArray(v) ? v : [v, v]));
    if (selected && !pairs.some(([value]) => value === selected)) pairs.push([selected, selected]);
    select.textContent = "";
    select.appendChild(el("option", { value: "" }, allLabel));
    pairs.forEach(([value, label]) => select.appendChild(el("option", { value }, label)));
    select.value = selected || "";
  }

  // ---------- KPI row ----------
  // tiles: [{label, value, accent, hint}]
  function renderKpis(row, tiles) {
    row.textContent = "";
    tiles.forEach((t) => {
      const attrs = { class: "kpi-tile", style: `border-left-color:${t.accent}` };
      if (t.hint) attrs.title = t.hint;
      row.appendChild(el("div", attrs, el("div", { class: "kpi-label" }, t.label), el("div", { class: "kpi-value" }, t.value)));
    });
  }

  // ---------- stacked trend (columns stacked by series) ----------
  const SVG_W = 900, SVG_H = 230;
  const PLOT_LEFT = 42, PLOT_RIGHT = 888, PLOT_TOP = 14, PLOT_BOTTOM = 190;

  // cfg = {card, title, sub, ariaLabel, series, colorOf, points: [{bucket, total, values: {series: count}}],
  //        granularity, empty, unit: ["entry", "entries"]}.
  function renderStackedTrend(cfg) {
    const { card, series, colorOf, points, granularity } = cfg;
    const unit = cfg.unit || ["entry", "entries"];
    card.textContent = "";
    const label = (p) => formatBucketLabel(p.bucket, granularity);

    const table = simpleTable(["Period", ...series, "Total"], points.map((p) => [label(p), ...series.map((n) => p.values[n]), p.total]));
    const chart = el("div", { class: "trend-svg-wrap" });
    card.appendChild(cardHeader(cfg.title, points.length ? tableToggle(chart, table) : null));
    card.appendChild(subline(cfg.sub));
    if (!points.length) {
      card.appendChild(emptyNote(cfg.empty));
      return;
    }
    card.appendChild(legend(series, colorOf));

    const maxV = niceMax(Math.max(...points.map((p) => p.total)));
    const n = points.length;
    const slot = (PLOT_RIGHT - PLOT_LEFT) / n;
    const barW = Math.max(Math.min(slot * 0.72, 44), 2);
    const yFor = (v) => PLOT_BOTTOM - (v / maxV) * (PLOT_BOTTOM - PLOT_TOP);

    const svg = svgEl("svg", { class: "trend-svg", viewBox: `0 0 ${SVG_W} ${SVG_H}`, role: "img", "aria-label": cfg.ariaLabel });
    [0, maxV / 2, maxV].forEach((v) => {
      const y = yFor(v);
      svg.appendChild(svgEl("line", { class: "trend-gridline", x1: PLOT_LEFT, x2: PLOT_RIGHT, y1: y, y2: y }));
      const t = svgEl("text", { class: "trend-axis-label", x: PLOT_LEFT - 8, y: y + 3, "text-anchor": "end" });
      t.textContent = formatCompact(Math.round(v));
      svg.appendChild(t);
    });
    pickTickIndices(n, 8).forEach((i) => {
      const t = svgEl("text", { class: "trend-axis-label", x: PLOT_LEFT + slot * i + slot / 2, y: PLOT_BOTTOM + 18, "text-anchor": "middle" });
      t.textContent = formatBucketLabel(points[i].bucket, granularity).replace("Week of ", "");
      svg.appendChild(t);
    });

    points.forEach((p, i) => {
      const x = PLOT_LEFT + slot * i + (slot - barW) / 2;
      let acc = 0;
      const group = svgEl("g", { class: "stack-col" });
      series.forEach((name) => {
        const v = p.values[name];
        if (!v) return;
        const y1 = yFor(acc + v), y0 = yFor(acc);
        group.appendChild(svgEl("rect", { x, y: y1, width: barW, height: Math.max(y0 - y1 - (acc ? 0.8 : 0), 1), rx: 1.5, fill: colorOf(name) }));
        acc += v;
      });
      // wide invisible hit rect so thin or empty columns are still hoverable
      const hit = svgEl("rect", { x: PLOT_LEFT + slot * i, y: PLOT_TOP, width: slot, height: PLOT_BOTTOM - PLOT_TOP, fill: "transparent" });
      const present = series.filter((name) => p.values[name]).sort((a, b) => p.values[b] - p.values[a]);
      const shown = present.slice(0, 5).map((name) => `${name} ${p.values[name]}`);
      if (present.length > 5) shown.push(`+${present.length - 5} more`);
      const detail = shown.join(" · ") || `No ${unit[1]}`;
      hit.addEventListener("mouseenter", () => tooltipAtElement(hit, `${p.total.toLocaleString()} ${plural(p.total, unit)}`, `${label(p)} · ${detail}`));
      hit.addEventListener("mouseleave", hideTooltip);
      group.appendChild(hit);
      svg.appendChild(group);
    });
    chart.appendChild(svg);
    card.appendChild(chart);
    card.appendChild(table);
  }

  // ---------- horizontal bars ----------
  function buildBarDisplayRows(fullData) {
    if (fullData.length <= TOP_N_BARS) return fullData;
    const top = fullData.slice(0, TOP_N_BARS);
    const other = fullData.slice(TOP_N_BARS).reduce((sum, r) => sum + r.count, 0);
    return top.concat([{ label: "Other", count: other, isOther: true }]);
  }

  // opts: {unit, colorFor(row), onPick(row), selected, valueText(row, total), sub}
  function renderBarCard(card, title, fullData, opts) {
    opts = opts || {};
    card.textContent = "";
    if (!fullData.length) {
      card.appendChild(cardHeader(title));
      card.appendChild(emptyNote("No data for these filters."));
      return;
    }
    const total = fullData.reduce((s, r) => s + r.count, 0);
    const rows = buildBarDisplayRows(fullData);
    const maxCount = Math.max(...rows.map((r) => r.count));

    const list = el("div", { class: "hbar-list" });
    rows.forEach((r) => {
      const pct = Math.max((r.count / maxCount) * 100, 2);
      const pickable = opts.onPick && !r.isOther;
      const fill = el("div", { class: "hbar-fill", style: `width:${pct}%` + (opts.colorFor ? `;background:${opts.colorFor(r)}` : "") });
      const row = el("div", { class: "hbar-row" + (r.isOther ? " other" : "") + (pickable ? " clickable" : "") + (opts.selected === r.label ? " selected" : ""), title: r.label },
        el("div", { class: "hbar-label" }, r.label),
        el("div", { class: "hbar-track" }, fill),
        el("div", { class: "hbar-value" }, opts.valueText ? opts.valueText(r, total) : formatCompact(r.count)));
      row.addEventListener("mouseenter", () => tooltipAtElement(row, `${r.count.toLocaleString()} (${Math.round((100 * r.count) / total)}%)`, r.label));
      row.addEventListener("mouseleave", hideTooltip);
      if (pickable) {
        row.addEventListener("click", () => {
          hideTooltip();
          opts.onPick(r);
        });
      }
      list.appendChild(row);
    });

    const table = simpleTable([title, opts.unit || "Entries", "Share"],
      fullData.map((r) => [r.label, r.count, `${Math.round((100 * r.count) / total)}%`]));
    card.appendChild(cardHeader(title, tableToggle(list, table)));
    card.appendChild(subline(opts.sub || (fullData.length > TOP_N_BARS ? `Top ${TOP_N_BARS} of ${fullData.length}` : `${fullData.length} total`)));
    card.appendChild(list);
    card.appendChild(table);
  }

  // The "Click a bar to filter" hint plus how many are hidden behind "Other".
  const pickSub = (fullData) => "Click a bar to filter" + (fullData.length > TOP_N_BARS ? ` · top ${TOP_N_BARS} of ${fullData.length}` : "");

  // ---------- cross-tab heat table ----------
  // cfg = {card, title, sub, firstHeader, data: {columns, rows: [{label, total, values}]}, colorOf(column), rgbOf(column),
  //        toggles, maxRows}
  function renderMatrix(cfg) {
    const { card, data } = cfg;
    const maxRows = cfg.maxRows || 12;
    card.textContent = "";
    card.appendChild(cardHeader(cfg.title, cfg.toggles));
    card.appendChild(subline(cfg.sub));
    if (!data.rows.length) {
      card.appendChild(emptyNote("No data for these filters."));
      return;
    }
    const shown = data.rows.slice(0, maxRows);
    const maxCell = Math.max(1, ...shown.flatMap((r) => data.columns.map((c) => r.values[c])));
    const table = el("table", { class: "matrix-table" },
      el("thead", {}, el("tr", {}, el("th", {}, cfg.firstHeader),
        data.columns.map((c) => el("th", { class: "num" }, el("span", { class: "stack-swatch", style: `background:${cfg.colorOf(c)}` }), c)),
        el("th", { class: "num" }, "Total"))),
      el("tbody", {}, shown.map((r) => el("tr", {},
        el("td", { class: "matrix-label", title: r.label }, r.label),
        data.columns.map((c) => {
          const v = r.values[c];
          const alpha = v ? 0.12 + 0.55 * (v / maxCell) : 0;
          return el("td", { class: "num matrix-cell", style: v ? `background:rgba(${cfg.rgbOf(c)},${alpha.toFixed(2)})` : "" }, v ? v.toLocaleString() : "·");
        }),
        el("td", { class: "num matrix-total" }, r.total.toLocaleString())))));
    card.appendChild(el("div", { class: "matrix-wrap" }, table));
    if (data.rows.length > shown.length) {
      card.appendChild(el("p", { class: "chart-card-sub matrix-more" }, `Showing the top ${shown.length} of ${data.rows.length}.`));
    }
  }

  // ---------- histogram of whole days ----------
  // cfg = {card, title, sub, hist: {n, avg, median, max, buckets: [{label, count}]}, unit: ["entry", "entries"], empty, color, note}
  function renderHistogram(cfg) {
    const { card, hist } = cfg;
    const unit = cfg.unit || ["entry", "entries"];
    card.textContent = "";
    card.appendChild(cardHeader(cfg.title));
    card.appendChild(subline(cfg.sub));
    if (!hist.n) {
      card.appendChild(emptyNote(cfg.empty || "No data for these filters."));
      return;
    }
    const stat = (label, value) => el("div", { class: "mini-stat" }, el("div", { class: "mini-stat-value" }, value), el("div", { class: "mini-stat-label" }, label));
    card.appendChild(el("div", { class: "mini-stats" },
      stat("Average", `${hist.avg}d`), stat("Median", `${hist.median}d`), stat("Longest", `${hist.max}d`), stat(plural(2, unit).replace(/^./, (c) => c.toUpperCase()), hist.n.toLocaleString())));
    const maxC = Math.max(1, ...hist.buckets.map((b) => b.count));
    const barStyle = (b) => `height:${b.count ? Math.max((b.count / maxC) * 100, 3) : 0}%` + (cfg.color ? `;background:${cfg.color}` : "");
    card.appendChild(el("div", { class: "lag-cols", style: `grid-template-columns:repeat(${hist.buckets.length},1fr)` }, hist.buckets.map((b) => {
      const col = el("div", { class: "lag-col", title: `${b.label}: ${b.count}` },
        el("div", { class: "lag-count" }, b.count ? b.count.toLocaleString() : ""),
        el("div", { class: "lag-bar-track" }, el("div", { class: "lag-bar", style: barStyle(b) })),
        el("div", { class: "lag-label" }, b.label));
      col.addEventListener("mouseenter", () => tooltipAtElement(col.querySelector(".lag-bar-track"), `${b.count.toLocaleString()} ${plural(b.count, unit)}`, b.label));
      col.addEventListener("mouseleave", hideTooltip);
      return col;
    })));
    if (cfg.note) card.appendChild(subline(cfg.note));
  }

  // ---------- data checks ----------
  // items: [{count, text, list: [[left, right], ...]}]  (zero-count items are skipped)
  function renderChecks(card, items, intro) {
    card.textContent = "";
    card.appendChild(cardHeader("Data checks"));
    card.appendChild(subline(intro || "Worth a second look"));
    const active = items.filter((i) => i.count);
    if (!active.length) {
      card.appendChild(el("div", { class: "check-ok" }, "Nothing to flag for these filters."));
      return;
    }
    active.forEach((item) => {
      card.appendChild(el("div", { class: "check-row" }, el("span", { class: "check-count" }, item.count.toLocaleString()), el("span", {}, item.text)));
      if (item.list && item.list.length) {
        card.appendChild(el("ul", { class: "check-list" }, item.list.map(([left, right]) =>
          el("li", {}, el("span", { class: "check-awb" }, left), el("span", { class: "check-times" }, right)))));
      }
    });
  }

  // ---------- dashboard engine ----------
  // Builds the filter bar of `#tab-analytics-<id>`, loads cfg.endpoint with the chosen filters and
  // renders the cards. cfg = {
  //   id, endpoint, unit: ["AWB", "AWBs"], what: "RTO Status",
  //   dateFields: [[value, label], ...], filters: [{key, param, all, options(data)}],
  //   layout: ["full-width-card-id", {grid: ["card", {id, span2: true}]}],
  //   kpis(data), render(data, ctx)
  // }
  // ctx = {cards, state, setFilter(key, value), rerender()}.
  function createDashboard(cfg) {
    const section = document.getElementById("tab-analytics-" + cfg.id);
    const tabBtn = document.querySelector(`#analytics-tabs .tab-btn[data-tab="analytics-${cfg.id}"]`);
    if (!section || !tabBtn) return null;

    const preset = cfg.defaultPreset || "30d";
    const initial = computePresetRange(preset);
    const state = {
      preset, start: initial.start, end: initial.end, dateField: cfg.dateFields[0][0],
      filters: Object.fromEntries(cfg.filters.map((f) => [f.key, ""])),
      ui: {},
    };
    let lastData = null;
    let loaded = false;
    let pollTimer = null;
    let loadToken = 0;

    // --- markup ---
    const bar = el("div", { class: "analytics-filters" });
    const rangeLabel = el("span", {});
    const icon = (paths, size, width) => {
      const svg = svgEl("svg", { width: size, height: size, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": width,
        "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" });
      paths.forEach((d) => svg.appendChild(svgEl("path", { d })));
      return svg;
    };
    const trigger = el("button", { type: "button", class: "filter-trigger", "aria-haspopup": "true", "aria-expanded": "false" },
      icon(["M5 5h14a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2z", "M3 10h18M8 3v4M16 3v4"], 15, 2),
      rangeLabel, icon(["m6 9 6 6 6-6"], 12, 2.4));
    const popover = el("div", { class: "date-range-popover hidden" });
    const presetButtons = PRESETS.map((p) => {
      const b = el("button", { type: "button", class: "preset-row" + (p === state.preset ? " active" : ""), "data-preset": p }, PRESET_LABELS[p]);
      return b;
    });
    const fromInput = el("input", { type: "date" });
    const toInput = el("input", { type: "date" });
    const applyBtn = el("button", { type: "button", class: "btn btn-primary btn-sm" }, "Apply range");
    popover.appendChild(el("div", { class: "date-range-presets" }, presetButtons));
    popover.appendChild(el("div", { class: "date-range-custom" }, el("label", {}, "From", fromInput), el("label", {}, "To", toInput), applyBtn));
    bar.appendChild(el("div", { class: "date-range-control" }, trigger, popover));

    let dateSelect = null;
    if (cfg.dateFields.length > 1) {
      dateSelect = el("select", { class: "dimension-select", "aria-label": "Date to analyse by" },
        cfg.dateFields.map(([value, label]) => el("option", { value }, label)));
      bar.appendChild(dateSelect);
    }
    const selects = {};
    cfg.filters.forEach((f) => {
      selects[f.key] = el("select", { class: "dimension-select", "aria-label": f.all }, el("option", { value: "" }, f.all));
      bar.appendChild(selects[f.key]);
    });
    const clearBtn = el("button", { type: "button", class: "btn btn-secondary btn-sm hidden" }, "Clear filters");
    const refreshBtn = el("button", { type: "button", class: "btn btn-secondary btn-sm" }, "Refresh");
    // The rows behind the cards, for the current filters (cfg.endpoint + "/export"); its href follows each load.
    const downloadLink = el("a", { class: "btn btn-secondary btn-sm", href: cfg.endpoint + "/export",
      title: "Downloads the rows behind these charts" }, "Download CSV");
    const updated = el("span", { class: "muted dash-updated" });
    bar.append(clearBtn, refreshBtn, downloadLink, updated);

    const banner = el("div", { class: "banner hidden" });
    const emptyHint = el("div", { class: "empty-hint hidden" });
    const body = el("div", { class: "analytics-body" });
    const kpiRow = el("div", { class: "kpi-row" });
    body.appendChild(kpiRow);
    const cards = {};
    const makeCard = (id, extra) => {
      cards[id] = el("div", { class: "chart-card" + (extra ? " " + extra : ""), "data-card": id });
      return cards[id];
    };
    cfg.layout.forEach((item) => {
      if (typeof item === "string") {
        body.appendChild(makeCard(item, "trend-card"));
      } else {
        body.appendChild(el("div", { class: "chart-grid" }, item.grid.map((g) =>
          typeof g === "string" ? makeCard(g) : makeCard(g.id, g.span2 ? "span-2" : ""))));
      }
    });
    section.append(bar, banner, emptyHint, body);

    // --- date range popover ---
    const openPopover = () => { popover.classList.remove("hidden"); trigger.setAttribute("aria-expanded", "true"); };
    const closePopover = () => { popover.classList.add("hidden"); trigger.setAttribute("aria-expanded", "false"); };
    const updateRangeLabel = () => {
      rangeLabel.textContent = state.preset === "custom" ? `${state.start} → ${state.end}` : PRESET_LABELS[state.preset] || "Custom range";
    };
    trigger.addEventListener("click", () => (popover.classList.contains("hidden") ? openPopover() : closePopover()));
    document.addEventListener("click", (e) => {
      if (!popover.contains(e.target) && !trigger.contains(e.target)) closePopover();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closePopover();
    });
    presetButtons.forEach((btn) => btn.addEventListener("click", () => {
      const range = computePresetRange(btn.dataset.preset);
      state.preset = btn.dataset.preset;
      state.start = range.start;
      state.end = range.end;
      presetButtons.forEach((b) => b.classList.toggle("active", b === btn));
      updateRangeLabel();
      closePopover();
      load();
    }));
    applyBtn.addEventListener("click", () => {
      if (!fromInput.value || !toInput.value || fromInput.value > toInput.value) return; // ignore an incomplete/inverted range
      state.preset = "custom";
      state.start = fromInput.value;
      state.end = toInput.value;
      presetButtons.forEach((b) => b.classList.remove("active"));
      updateRangeLabel();
      closePopover();
      load();
    });

    // --- filters ---
    function setFilter(key, value) {
      state.filters[key] = value;
      load();
    }
    if (dateSelect) dateSelect.addEventListener("change", () => { state.dateField = dateSelect.value; load(); });
    cfg.filters.forEach((f) => selects[f.key].addEventListener("change", () => setFilter(f.key, selects[f.key].value)));
    clearBtn.addEventListener("click", () => {
      cfg.filters.forEach((f) => (state.filters[f.key] = ""));
      load();
    });
    refreshBtn.addEventListener("click", load);

    function syncControls(data) {
      if (dateSelect) dateSelect.value = state.dateField;
      cfg.filters.forEach((f) => fillSelect(selects[f.key], f.options(data), state.filters[f.key], f.all));
      clearBtn.classList.toggle("hidden", !cfg.filters.some((f) => state.filters[f.key]));
    }

    function showBanner(message, kind) {
      banner.textContent = message || "";
      banner.className = message ? "banner " + kind : "banner hidden";
    }

    function showUpdated(data) {
      if (!data.fetched_at) {
        updated.textContent = "";
        return;
      }
      const when = new Date(data.fetched_at * 1000);
      updated.textContent = "Data as of " + when.toLocaleString(undefined, { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" }) +
        (data.refreshing ? " · refreshing…" : "");
    }

    // --- load + render ---
    const ctx = {
      cards, state, setFilter,
      rerender() { if (lastData && lastData.total) renderAll(lastData); },
    };

    function renderAll(data) {
      renderKpis(kpiRow, cfg.kpis(data));
      cfg.render(data, ctx);
    }

    async function load() {
      const myToken = ++loadToken;
      clearTimeout(pollTimer);
      body.classList.add("loading");
      try {
        const params = new URLSearchParams({ date_field: state.dateField });
        if (state.start) params.set("from", state.start);
        if (state.end) params.set("to", state.end);
        cfg.filters.forEach((f) => { if (state.filters[f.key]) params.set(f.param, state.filters[f.key]); });
        downloadLink.href = `${cfg.endpoint}/export?${params.toString()}`;
        const data = await fetchJson(`${cfg.endpoint}?${params.toString()}`);
        if (myToken !== loadToken) return; // a newer request already superseded this one
        loaded = true;
        lastData = data;
        showUpdated(data);
        if (data.loading) {
          // first-ever fetch from the database is still running: say so and look again shortly
          body.classList.add("hidden");
          emptyHint.classList.remove("hidden");
          emptyHint.textContent = `The ${cfg.what} data is being fetched from the database for the first time. This page updates by itself.`;
          pollTimer = setTimeout(load, 5000);
          return;
        }
        showBanner(data.error ? `${cfg.what}: the last database refresh reported a problem (${data.error}). Showing the last good data.` : "", "error");
        syncControls(data);
        if (data.total === 0) {
          body.classList.add("hidden");
          emptyHint.classList.remove("hidden");
          emptyHint.textContent = `No ${cfg.unit[1]} match these filters. Try a wider date range or clear a filter.`;
        } else {
          body.classList.remove("hidden");
          emptyHint.classList.add("hidden");
          renderAll(data);
        }
      } catch (err) {
        if (myToken !== loadToken) return;
        body.classList.add("hidden");
        emptyHint.classList.remove("hidden");
        emptyHint.textContent = "Could not load analytics: " + err.message;
      } finally {
        if (myToken === loadToken) body.classList.remove("loading");
      }
    }

    // Load when the tab is first opened (not at page load); reopening it shows what changed since.
    tabBtn.addEventListener("click", load);
    document.querySelector('.side-nav-item[data-panel="rto-analytics"]').addEventListener("click", () => {
      if (loaded && section.classList.contains("active")) load();
    });
    // New data from the database (the daily refresh, or Refresh from DB) - see data_refresh.js.
    window.addEventListener("panel:data-changed", () => {
      if (loaded) load();
    });

    updateRangeLabel();
    return { load, state };
  }

  window.AnalyticsKit = {
    TOP_N_BARS, SERIES_COLORS, OTHER_COLOR, PRESET_LABELS,
    fetchJson, svgEl, el, formatCompact, formatRupees, formatRupeesFull, dateStr, todayStr, addDays, computePresetRange,
    formatBucketLabel, niceMax, pickTickIndices, hexToRgb, colorMap, plural,
    showTooltip, hideTooltip, tooltipAtElement,
    cardHeader, tableToggle, simpleTable, segmented, emptyNote, subline, legend, fillSelect, pickSub,
    renderKpis, renderStackedTrend, renderBarCard, renderMatrix, renderHistogram, renderChecks, createDashboard,
  };
})();
