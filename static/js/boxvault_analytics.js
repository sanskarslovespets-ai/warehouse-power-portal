(function () {
  "use strict";

  // Warehouse Dashboards > BoxVault > Analytics: Invoice Entry's lines (/api/boxvault/entries, loaded by
  // boxvault.js, which calls render() while this view is open), filtered, totalled and compared here.
  //
  // Each warehouse keeps one colour whatever the filters: slots 1-7 of the dataviz reference categorical palette,
  // in boxvault.WAREHOUSES order. Checked with validate_palette.js (light, on --chart-surface #fffdf9): adjacent
  // pairs clear CVD dE 8 and normal-vision dE 15, in this order and in October 2026's (Bangalore, Kolkata, Bhiwandi,
  // Pune, Gurugram). Three are under 3:1 contrast, so every chart has a legend or labels and a table view, and
  // stacked segments have a gap between them.
  const PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6"];
  const ACCENT = "#b9b4a3"; // KPI tiles: neutral, so no tile reads as a warehouse
  const ITEM_BAR = "#77756d"; // Top items: one neutral series (4.4:1 on the surface), not a warehouse's colour
  const LIST_MAX = 3; // entries listed under each data check

  const K = window.AnalyticsKit;
  const { el, formatRupees, formatRupeesFull, formatCompact } = K;
  const $ = (id) => document.getElementById(id);
  const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  const state = { warehouse: "", month: "", sku: "", measure: "spend", granularity: "week" };
  let rows = [];
  let warehouses = []; // [{id, name}] in dropdown order
  let colorById = {}, nameById = {};
  let wired = false;

  // ---------- helpers ----------
  const sum = (list, key) => list.reduce((t, r) => t + (r[key] || 0), 0);
  const qtyText = (n) => n.toLocaleString("en-IN", { maximumFractionDigits: 2 });
  const rate = (n) => "₹" + n.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const pct = (part, whole) => (whole ? Math.round((100 * part) / whole) : 0);
  const invoiceKey = (r) => r.supplier_gstin + " " + r.invoice_number;
  const monthLabel = (ym) => `${MON[+ym.slice(5, 7) - 1]} ${ym.slice(0, 4)}`;
  const dayLabel = (iso) => `${+iso.slice(8, 10)} ${MON[+iso.slice(5, 7) - 1]}`;

  function weekStart(iso) { // the Monday of an ISO date's week
    const d = new Date(iso + "T00:00:00");
    d.setDate(d.getDate() - ((d.getDay() + 6) % 7));
    return K.dateStr(d);
  }

  function nextBucket(bucket) {
    const d = new Date(bucket + "T00:00:00");
    if (state.granularity === "month") d.setMonth(d.getMonth() + 1);
    else d.setDate(d.getDate() + 7);
    return K.dateStr(d);
  }

  // "DEL Supertails 15*15*15 Printed 5 Ply carton box" -> "15 × 15 × 15"; anything without a size (paper bags,
  // shredded paper) is its own description, the same in every city.
  function size(description) {
    const m = /(\d+(?:\.\d+)?)\s*\*\s*(\d+(?:\.\d+)?)\s*\*\s*(\d+(?:\.\d+)?)/.exec(description);
    return m ? `${m[1]} × ${m[2]} × ${m[3]}` : description;
  }

  function group(list, keyOf) {
    const out = new Map();
    list.forEach((r) => {
      const k = keyOf(r);
      if (!out.has(k)) out.set(k, []);
      out.get(k).push(r);
    });
    return out;
  }

  // The lines matching the filters, except those named in `ignore` (a card comparing warehouses ignores the
  // warehouse filter, and so on).
  function filtered(ignore) {
    ignore = ignore || [];
    return rows.filter((r) => (ignore.includes("warehouse") || !state.warehouse || r.warehouse === state.warehouse) &&
      (ignore.includes("month") || !state.month || r.invoice_date.startsWith(state.month)) &&
      (ignore.includes("sku") || !state.sku || r.sku === state.sku));
  }

  // Used / left are null on every line when the ERP couldn't be read (boxvault.entries' usage_problem).
  const usageKnown = (list) => list.some((r) => r.sku) && list.every((r) => !r.sku || r.used != null);

  function setFilter(key, value) {
    state[key] = value;
    renderAll();
  }

  // ---------- filters ----------
  function syncFilters() {
    const present = new Set(rows.map((r) => r.warehouse));
    K.fillSelect($("bv-a-wh"), warehouses.filter((w) => present.has(w.id)).map((w) => [w.id, w.name]), state.warehouse, "All warehouses");
    const months = [...new Set(rows.map((r) => r.invoice_date.slice(0, 7)))].sort().reverse();
    K.fillSelect($("bv-a-month"), months.map((m) => [m, monthLabel(m)]), state.month, "All months");
    const items = new Map();
    filtered(["sku"]).filter((r) => r.sku).forEach((r) => items.set(r.sku, `${r.sku} · ${size(r.description)}`));
    K.fillSelect($("bv-a-item"), [...items].sort((a, b) => a[0].localeCompare(b[0])), state.sku, "All items");
    $("bv-a-clear").hidden = !(state.warehouse || state.month || state.sku);
  }

  function wire() {
    wired = true;
    $("bv-a-wh").addEventListener("change", (e) => setFilter("warehouse", e.target.value));
    $("bv-a-month").addEventListener("change", (e) => setFilter("month", e.target.value));
    $("bv-a-item").addEventListener("change", (e) => setFilter("sku", e.target.value));
    $("bv-a-clear").addEventListener("click", () => {
      state.warehouse = state.month = state.sku = "";
      renderAll();
    });
  }

  // ---------- KPIs ----------
  function kpis(list) {
    const invoices = new Set(list.map(invoiceKey)).size;
    const spend = sum(list, "total");
    const bought = sum(list, "qty");
    const known = usageKnown(list);
    const used = known ? sum(list, "used") : null;
    const left = known ? sum(list, "left") : null;
    const usedHint = known
      ? "Completed ERP delivery notes carrying the box SKUs since their invoices (Invoice Entry's SKU used)." +
        (used === 0 ? " None so far: the ERP isn't putting box SKUs on delivery notes." : "")
      : "The ERP couldn't be read, so boxes used aren't known.";
    return [
      { label: "Spend", value: formatRupees(spend), accent: ACCENT,
        hint: `${formatRupeesFull(spend)}, of which ${formatRupeesFull(sum(list, "tax_amount"))} GST` },
      { label: "Invoices", value: invoices.toLocaleString("en-IN"), accent: ACCENT, hint: `${list.length.toLocaleString("en-IN")} line items` },
      { label: "Average invoice", value: invoices ? formatRupees(spend / invoices) : "—", accent: ACCENT,
        hint: invoices ? `${formatRupeesFull(spend / invoices)} per invoice, with GST` : "" },
      { label: "Bought", value: formatCompact(Math.round(bought)), accent: ACCENT, hint: `${qtyText(bought)} boxes and other items` },
      { label: "Used", value: used == null ? "—" : formatCompact(Math.round(used)), accent: ACCENT, hint: usedHint },
      { label: "Left", value: left == null ? "—" : formatCompact(Math.round(left)), accent: ACCENT, hint: "Bought minus used" },
    ];
  }

  // ---------- spend over time ----------
  function renderTrend(list) {
    const spend = state.measure === "spend";
    const bucketOf = state.granularity === "month" ? (r) => r.invoice_date.slice(0, 7) + "-01" : (r) => weekStart(r.invoice_date);
    const byBucket = group(list, bucketOf);
    const present = warehouses.filter((w) => list.some((r) => r.warehouse === w.id));
    const points = [];
    if (list.length) {
      const keys = [...byBucket.keys()].sort();
      for (let b = keys[0]; b <= keys[keys.length - 1]; b = nextBucket(b)) { // every period, empty ones too
        const lines = byBucket.get(b) || [];
        const values = {};
        present.forEach((w) => {
          const v = sum(lines.filter((r) => r.warehouse === w.id), spend ? "total" : "qty");
          if (v) values[w.name] = v;
        });
        points.push({ bucket: b, total: sum(lines, spend ? "total" : "qty"), values });
      }
    }
    const toggles = [
      K.segmented([["spend", "Spend"], ["qty", "Quantity"]], state.measure, (v) => setFilter("measure", v)),
      K.segmented([["week", "Week"], ["month", "Month"]], state.granularity, (v) => setFilter("granularity", v)),
    ];
    K.renderStackedTrend({
      card: $("bv-a-trend"),
      title: spend ? "Spend over time" : "Quantity bought over time",
      sub: `By invoice date · grouped by ${state.granularity} · stacked by warehouse`,
      ariaLabel: `${spend ? "Spend" : "Quantity bought"} by ${state.granularity}, stacked by warehouse`,
      series: present.map((w) => w.name),
      colorOf: (name) => colorById[warehouses.find((w) => w.name === name).id],
      points,
      granularity: state.granularity,
      format: spend ? formatRupeesFull : (v) => `${qtyText(v)} bought`,
      axisFormat: spend ? formatRupees : (v) => formatCompact(Math.round(v)),
      gap: 2,
      toggles,
      empty: "No invoices for these filters.",
    });
  }

  // ---------- spend by warehouse (ignores the warehouse filter, to compare) ----------
  function renderByWarehouse() {
    const list = filtered(["warehouse"]);
    const total = sum(list, "total");
    const data = warehouses.map((w) => ({ label: w.name, id: w.id, count: sum(list.filter((r) => r.warehouse === w.id), "total") }))
      .filter((r) => r.count > 0).sort((a, b) => b.count - a.count);
    K.renderBarCard($("bv-a-by-wh"), "Spend by warehouse", data, {
      unit: "Spend",
      colorFor: (r) => colorById[r.id],
      selected: nameById[state.warehouse] || null,
      onPick: (r) => setFilter("warehouse", state.warehouse === r.id ? "" : r.id),
      valueText: (r) => formatRupees(r.count),
      tipText: (r) => `${formatRupeesFull(r.count)} (${pct(r.count, total)}%)`,
      tableText: (r) => formatRupeesFull(r.count),
      sub: state.warehouse ? "Click it again to show every warehouse" : "Click a warehouse to filter",
    });
  }

  // ---------- top items (ignores the item filter) ----------
  function renderItems() {
    const list = filtered(["sku"]).filter((r) => r.sku);
    const data = [...group(list, (r) => r.sku)].map(([sku, lines]) => ({
      label: `${sku} · ${size(lines[0].description)}`, sku, count: sum(lines, "total"), qty: sum(lines, "qty"),
    })).sort((a, b) => b.count - a.count);
    const selected = data.find((r) => r.sku === state.sku);
    const rest = data.slice(K.TOP_N_BARS);
    K.renderBarCard($("bv-a-items"), "Top items by spend", data, {
      unit: "Spend",
      dropOther: true,
      colorFor: () => ITEM_BAR,
      selected: selected ? selected.label : null,
      onPick: (r) => setFilter("sku", state.sku === r.sku ? "" : r.sku),
      valueText: (r) => formatRupees(r.count),
      tipText: (r) => `${formatRupeesFull(r.count)} · ${qtyText(r.qty)} bought`,
      tableText: (r) => formatRupeesFull(r.count),
      sub: (rest.length ? `Top ${K.TOP_N_BARS} of ${data.length}; the other ${rest.length} come to ${formatRupees(sum(rest, "count"))} · ` : "") +
        (state.sku ? "click it again to show every item" : "click an item to filter"),
    });
  }

  // ---------- data checks ----------
  function capped(list) {
    return list.length > LIST_MAX ? [...list.slice(0, LIST_MAX), ["…", `${list.length - LIST_MAX} more`]] : list;
  }

  function renderChecksCard(list) {
    // Invoices for one warehouse with exactly the same lines (cancelled and re-issued, say).
    const invoices = [...group(list, invoiceKey).values()].map((lines) => ({
      number: lines[0].invoice_number, date: lines[0].invoice_date, warehouse: lines[0].warehouse, total: sum(lines, "total"),
      sig: lines[0].warehouse + "|" + lines.map((r) => `${r.sku}:${r.qty}:${r.rate}`).sort().join(","), lines: lines.length,
    }));
    const dupes = [...group(invoices, (i) => i.sig).values()].filter((g) => g.length > 1).map((g) => {
      g.sort((a, b) => a.date.localeCompare(b.date) || a.number.localeCompare(b.number));
      const dates = [...new Set(g.map((i) => dayLabel(i.date)))].join(" and ");
      return [g.map((i) => i.number).join(", "),
        `${nameById[g[0].warehouse]} · ${g[0].lines} line${g[0].lines === 1 ? "" : "s"}, ${formatRupeesFull(g[0].total)} each · ${dates}`];
    });

    // A SKU's price at one warehouse changing between invoices.
    const changes = [...group(list.filter((r) => r.sku), (r) => r.warehouse + "|" + r.sku).values()].map((lines) => {
      lines.sort((a, b) => a.invoice_date.localeCompare(b.invoice_date) || a.invoice_number.localeCompare(b.invoice_number));
      const rates = lines.map((r) => r.rate);
      const lo = Math.min(...rates), hi = Math.max(...rates);
      const first = rates[0], last = rates[rates.length - 1];
      return { lines, lo, hi, first, last, change: first === last ? (hi - lo) / lo : (last - first) / first };
    }).filter((c) => c.hi > c.lo).sort((a, b) => Math.abs(b.change) - Math.abs(a.change)).map((c) => {
      const r = c.lines[0];
      const move = c.first === c.last ? `${rate(c.lo)} to ${rate(c.hi)}, back to ${rate(c.last)}`
        : `${rate(c.first)} → ${rate(c.last)} (${c.change > 0 ? "+" : ""}${Math.round(c.change * 100)}%)`;
      return [`${r.sku} · ${nameById[r.warehouse]}`, move];
    });

    const over = list.filter((r) => r.left != null && r.left < 0)
      .map((r) => [`${r.sku} · ${nameById[r.warehouse]}`, `${r.invoice_number}: ${qtyText(r.qty)} bought, ${qtyText(r.used)} used`]);
    const noSku = list.filter((r) => !r.sku).length;
    const unused = usageKnown(list)
      ? [...group(list.filter((r) => r.sku), (r) => r.warehouse + "|" + r.sku).values()].filter((g) => g.every((r) => r.used === 0)).length
      : 0;

    K.renderChecks($("bv-a-checks"), [
      { count: dupes.length, list: capped(dupes),
        text: `${dupes.length === 1 ? "set" : "sets"} of invoices for one warehouse with exactly the same lines: check one wasn't cancelled and re-issued, and Remove it from Invoice Entry if so` },
      { count: changes.length, list: capped(changes),
        text: `${changes.length === 1 ? "SKU" : "SKUs"} whose price changed between invoices at the same warehouse` },
      { count: over.length, list: capped(over),
        text: `invoice ${over.length === 1 ? "line" : "lines"} with more boxes used than bought: an invoice may be missing` },
      { count: noSku, text: `${noSku === 1 ? "line" : "lines"} without a SKU, so no usage can be counted for ${noSku === 1 ? "it" : "them"}` },
      { count: unused, text: `${unused === 1 ? "SKU" : "SKUs"} with no boxes used in the ERP since ${unused === 1 ? "its" : "their"} invoice: boxes aren't being put on delivery notes` },
    ], "Worth a second look, for these filters");
  }

  // ---------- tables ----------
  function cell(text, cls, title) {
    const td = el("td", cls ? { class: cls } : {}, text);
    if (title) td.title = title;
    return td;
  }

  function fillTable(table, head, body, emptyText) {
    table.replaceChildren(el("thead", {}, el("tr", {}, head)),
      el("tbody", {}, body.length ? body : el("tr", {}, el("td", { class: "ct-left bv-na", colspan: String(head.length) }, emptyText))));
  }

  // Price per box: amount before tax / quantity for each size at each warehouse, across the month filter's invoices.
  function renderPrices() {
    const list = filtered(["warehouse", "sku"]).filter((r) => r.qty > 0);
    const whs = warehouses.filter((w) => list.some((r) => r.warehouse === w.id));
    const sizes = [...group(list, (r) => size(r.description).toLowerCase()).values()].map((lines) => {
      const cells = whs.map((w) => {
        const mine = lines.filter((r) => r.warehouse === w.id);
        if (!mine.length) return null;
        const rates = mine.map((r) => r.rate);
        return { value: sum(mine, "amount") / sum(mine, "qty"), qty: sum(mine, "qty"), invoices: new Set(mine.map(invoiceKey)).size,
          lo: Math.min(...rates), hi: Math.max(...rates) };
      });
      const values = cells.filter(Boolean).map((c) => c.value);
      const lo = Math.min(...values), hi = Math.max(...values);
      return { label: size(lines[0].description), cells, lo, hi, spread: values.length > 1 ? (hi - lo) / lo : null };
    }).sort((a, b) => (b.spread ?? -1) - (a.spread ?? -1) || a.label.localeCompare(b.label));

    const head = [el("th", { class: "ct-left" }, "Size / item"),
      ...whs.map((w) => el("th", {}, el("span", { class: "stack-swatch", style: `background:${colorById[w.id]}` }), w.name)),
      el("th", { title: "How much dearer the dearest warehouse is than the cheapest" }, "Spread")];
    const body = sizes.map((s) => el("tr", {},
      cell(s.label, "ct-left"),
      ...s.cells.map((c) => !c ? cell("·", "bv-na") : cell(rate(c.value), s.spread && c.value === s.lo ? "bv-best" : "",
        `${qtyText(c.qty)} bought on ${c.invoices} invoice${c.invoices === 1 ? "" : "s"}` + (c.hi > c.lo ? `, at ${rate(c.lo)} to ${rate(c.hi)}` : ""))),
      cell(s.spread == null ? "—" : `${Math.round(s.spread * 100)}%`, s.spread == null ? "bv-na" : "")));
    fillTable($("bv-a-prices"), head, body, "No lines for these filters.");
    const compared = sizes.filter((s) => s.spread != null).length;
    $("bv-a-prices-note").textContent = `${sizes.length} sizes and items, ${compared} bought at more than one warehouse · lowest price in bold` +
      (state.month ? ` · ${monthLabel(state.month)}` : "");
  }

  // Stock: each warehouse's SKUs, bought / used / left, for the filters.
  function renderStock(list) {
    const known = usageKnown(list);
    const order = Object.fromEntries(warehouses.map((w, i) => [w.id, i]));
    const stock = [...group(list, (r) => r.warehouse + "|" + r.sku).values()].map((lines) => ({
      warehouse: lines[0].warehouse, sku: lines[0].sku, item: size(lines[0].description),
      bought: sum(lines, "qty"), used: known && lines[0].sku ? sum(lines, "used") : null, left: known && lines[0].sku ? sum(lines, "left") : null,
      amount: sum(lines, "amount"), spend: sum(lines, "total"), last: lines.map((r) => r.invoice_date).sort().pop(),
    })).sort((a, b) => order[a.warehouse] - order[b.warehouse] || b.spend - a.spend);

    const head = ["Warehouse", "SKU", "Item"].map((h) => el("th", { class: "ct-left" }, h)).concat(
      ["Bought", "Used", "Left", "Price per box", "Spend", "Last invoice"].map((h) => el("th", {}, h)));
    const body = stock.map((s) => el("tr", {},
      cell(nameById[s.warehouse], "ct-left"), cell(s.sku || "—", "ct-left bv-sku"), cell(s.item, "ct-left"),
      cell(qtyText(s.bought)),
      cell(s.used == null ? "—" : qtyText(s.used), s.used == null ? "bv-na" : ""),
      cell(s.left == null ? "—" : qtyText(s.left), s.left == null ? "bv-na" : s.left < 0 ? "bv-over" : ""),
      cell(s.bought ? rate(s.amount / s.bought) : "—"),
      cell(formatRupeesFull(s.spend)),
      cell(`${dayLabel(s.last)} ${s.last.slice(0, 4)}`)));
    fillTable($("bv-a-stock"), head, body, "No lines for these filters.");
    $("bv-a-stock-note").textContent = `${stock.length} ${stock.length === 1 ? "SKU" : "SKUs"} · biggest spend first in each warehouse`;
  }

  // ---------- all of it ----------
  function renderAll() {
    syncFilters();
    const list = filtered();
    K.renderKpis($("bv-a-kpis"), kpis(list));
    renderTrend(list);
    renderByWarehouse();
    renderItems();
    renderChecksCard(list);
    renderPrices();
    renderStock(list);
  }

  // rows: /api/boxvault/entries' rows; whs: /api/boxvault's warehouses [{id, name}], in the order their colours go.
  function render(rowsIn, whs) {
    rows = rowsIn;
    warehouses = whs;
    colorById = Object.fromEntries(whs.map((w, i) => [w.id, PALETTE[i % PALETTE.length]]));
    nameById = Object.fromEntries(whs.map((w) => [w.id, w.name]));
    if (!wired) wire();
    renderAll();
  }

  window.BoxVaultAnalytics = { render, size, weekStart };
})();
