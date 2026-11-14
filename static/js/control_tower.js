(function () {
  "use strict";

  // Warehouse Dashboards > Control Tower: the Control Tower sheet's Dashboard and Warehouse Pendency tabs, from
  // /api/control-tower (control_tower.py builds it from the sheet's ERP, CP and EDD dump queries).
  // - Dashboard: the DNs (facts, grouped by every field shown) pivoted by facility and ideal pickup date here,
  //   for the count picked above the table; and a few counts by order date.
  // - Warehouse Pendency: the mother warehouses' DNs with no AWB yet, filtered and listed here.
  // The panel is a .bx, so it uses the Breach Explorer's kit (be_common.js): its filters, CSV and table styles.

  const BX = window.BX;
  const el = BX.el, fmt = BX.fmt, uniq = BX.uniq, esc = BX.esc;
  const root = document.getElementById("panel-control-tower");
  const stateEl = root.querySelector(".bx-state");
  const body = root.querySelector(".bx-body");
  const banner = root.querySelector(".bx-banner");
  const fresh = root.querySelector(".bx-freshness");
  const refreshBtn = root.querySelector(".bx-refresh");
  const refreshLabel = refreshBtn.querySelector(".bx-refresh-label");

  // fact layout (control_tower.build): facility, bucket, ideal pickup date, order date, DN status, status,
  // courier, channel, flags, DNs
  const C = { fac: 0, bucket: 1, ideal: 2, order: 3, dn: 4, status: 5, courier: 6, channel: 7, flags: 8, n: 9 };
  const HAS_AWB = 1, HAS_EDD = 2;
  // pendency row layout: control_tower.PENDENCY_COLUMNS
  const P = { so: 0, dn: 1, wh: 2, pick: 3, ideal: 4, order: 5, time: 6, courier: 7, status: 8, channel: 9 };

  const DEFAULT_BUCKETS = ["WH", "DS"]; // the sheet's dashboard listed warehouses and darkstores
  const DEFAULT_STATUS = "To Be Picked"; // the sheet's status-level block opened on this
  const PICKING = new Set(["To Be Picked", "Under Picking"]);
  const NO_AWB = new Set(["Hold", "To Be Picked", "Under Picking", "Picked"]);
  const PRE_PICKUP = new Set(["Draft", "Hold", "To Be Picked", "Under Picking", "Item Picked", "Packed", "Pickup Pending"]);
  const PENDENCY_STATUSES = ["To Be Picked", "Under Picking", "Item Picked", "Packed"];
  const ROW_LIMIT = 500; // DN rows drawn; Export CSV has them all
  const DAYS = 7; // the Dashboard's tables show today and the six days before, one column each (control_tower.ERP_DAYS)
  const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  const statusSelect = el("ct-status");
  const METRICS = [
    { id: "picking", label: "Picking", title: "Picking pendency", note: "DN status To Be Picked or Under Picking",
      test: (f) => PICKING.has(D.dn_status[f[C.dn]]) },
    { id: "awb", label: "AWB not generated", title: "AWB not generated", note: "Hold, To Be Picked, Under Picking or Picked",
      test: (f) => NO_AWB.has(D.dn_status[f[C.dn]]) },
    { id: "pickup", label: "Pickup pending", title: "Pickup pendency", note: "not yet with the courier",
      test: (f) => PRE_PICKUP.has(D.status[f[C.status]]) },
    { id: "hld", label: "Hyperlocal", title: "Hyperlocal dispatch pendency", note: "pickup pending with a hyperlocal (HLD) partner",
      test: (f) => PRE_PICKUP.has(D.status[f[C.status]]) && / HLD$/.test(D.courier[f[C.courier]]) },
    { id: "status", label: "By status", title: "By status", note: "DNs in the status picked here",
      test: (f) => D.status[f[C.status]] === statusSelect.value },
  ];

  const MS = {};
  const sorts = {};
  let RAW = null, D = {}, F = [], ROWS = [], FAC_BUCKET = {};
  let metric = "picking";
  let view = "dashboard";

  // ---------- dates (IST, as the data is) ----------
  const istToday = () => new Date(Date.now() + 330 * 60000).toISOString().slice(0, 10);
  function addDays(iso, n) {
    const d = new Date(iso + "T00:00:00Z");
    d.setUTCDate(d.getUTCDate() + n);
    return d.toISOString().slice(0, 10);
  }
  const daysBetween = (from, to) => Math.round((Date.parse(to) - Date.parse(from)) / 86400000);
  const short = (iso) => (+iso.slice(8, 10)) + " " + MON[+iso.slice(5, 7) - 1];

  // ---------- a sortable table ----------
  // cols: [{label, num, cls, render}]; rows and foot: arrays of raw values (sorted on, then drawn).
  function table(id, cols, rows, foot, opts) {
    const ss = sorts[id] || opts.sort;
    rows = rows.slice().sort((a, b) => {
      const x = a[ss.col], y = b[ss.col];
      const c = typeof x === "number" ? x - y : String(x).localeCompare(String(y));
      return ss.dir * c || String(a[0]).localeCompare(String(b[0]));
    });
    const shown = opts.limit ? rows.slice(0, opts.limit) : rows;
    const td = (v, c) => {
      const cls = c.cls ? ' class="' + c.cls + '"' : "";
      if (c.render) return "<td" + cls + ">" + c.render(v) + "</td>";
      if (c.num) return "<td" + cls + ">" + (v ? fmt(v) : '<span class="dim">·</span>') + "</td>";
      return "<td" + cls + ">" + esc(v) + "</td>";
    };
    let h = "<thead><tr>" + cols.map((c, i) =>
      '<th class="sortable' + (c.cls ? " " + c.cls : "") + (i === ss.col ? " sorted" : "") + '" data-c="' + i + '">' + esc(c.label) + "</th>"
    ).join("") + "</tr></thead><tbody>";
    if (!shown.length) h += '<tr><td class="empty" colspan="' + cols.length + '">' + esc(opts.empty) + "</td></tr>";
    shown.forEach((r) => { h += "<tr>" + r.map((v, i) => td(v, cols[i])).join("") + "</tr>"; });
    h += "</tbody>";
    if (foot) h += "<tfoot><tr>" + foot.map((v, i) => td(v, cols[i])).join("") + "</tr></tfoot>";
    const t = el(id);
    t.innerHTML = h;
    t.querySelectorAll("th.sortable").forEach((th) => th.addEventListener("click", () => {
      const c = +th.dataset.c;
      const cur = sorts[id] || opts.sort;
      sorts[id] = { col: c, dir: cur.col === c ? -cur.dir : (cols[c].num ? -1 : 1) };
      render();
    }));
    return rows.length;
  }

  const sum = (cells) => cells.reduce((a, b) => a + b, 0);

  // ---------- Dashboard ----------
  function facts() {
    const b = MS.bucket.sel, fa = MS.facility.sel, co = MS.courier.sel, ch = MS.channel.sel;
    return F.filter((f) =>
      (!b.size || b.has(D.bucket[f[C.bucket]])) &&
      (!fa.size || fa.has(D.facility[f[C.fac]])) &&
      (!co.size || co.has(D.courier[f[C.courier]])) &&
      (!ch.size || ch.has(D.channel[f[C.channel]])));
  }

  // The DAYS dates up to today, oldest first.
  const lastDays = (today) => Array.from({ length: DAYS }, (_, i) => addDays(today, i - DAYS + 1));

  // Columns by ideal pickup date: Earlier, the DAYS days up to Today, then Due by today (their sum) and Due later.
  function dayIndex(iso, today) {
    if (iso > today) return DAYS + 1;
    const back = daysBetween(iso, today);
    return back >= DAYS ? 0 : DAYS - back;
  }

  function byFacility(fs, test, today) {
    const m = new Map();
    fs.forEach((f) => {
      if (!test(f)) return;
      const k = D.facility[f[C.fac]];
      if (!m.has(k)) m.set(k, new Array(DAYS + 2).fill(0));
      m.get(k)[dayIndex(D.date[f[C.ideal]], today)] += f[C.n];
    });
    return m;
  }

  function renderCards(fs, today) {
    const pend = pendencyRows(false).filter((r) => r[P.ideal] <= today);
    const cards = METRICS.filter((m) => m.id !== "status").map((m) => {
      let due = 0, later = 0;
      fs.forEach((f) => {
        if (!m.test(f)) return;
        if (D.date[f[C.ideal]] > today) later += f[C.n]; else due += f[C.n];
      });
      return '<button type="button" class="pcard' + (m.id === metric ? " on" : "") + '" data-metric="' + m.id + '">' +
        '<div class="k">' + esc(m.title) + '</div><div class="v">' + fmt(due) + '</div>' +
        '<div class="s">due today or earlier · ' + fmt(later) + " later</div></button>";
    });
    cards.push('<button type="button" class="pcard crit" data-view="pendency">' +
      '<div class="k">Warehouse pendency</div><div class="v">' + fmt(new Set(pend.map((r) => r[P.dn])).size) + "</div>" +
      '<div class="s">mother warehouse DNs due, no AWB yet</div></button>');
    el("ct-cards").innerHTML = cards.join("");
    el("ct-cards").querySelectorAll("[data-metric]").forEach((b) => b.addEventListener("click", () => { metric = b.dataset.metric; render(); }));
    el("ct-cards").querySelector("[data-view]").addEventListener("click", () => showView("pendency"));
  }

  function renderDashboard() {
    const today = istToday();
    const fs = facts();
    renderCards(fs, today);

    const m = METRICS.find((x) => x.id === metric);
    el("ct-metric").querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.metric === metric)));
    statusSelect.hidden = metric !== "status";
    el("ct-metric-title").textContent = metric === "status" ? statusSelect.value || "By status" : m.title;
    el("ct-metric-note").textContent = "by ideal pickup date · " + m.note;

    const cols = [{ label: "Facility" }, { label: "Type" }, { label: "Earlier", num: true }]
      .concat(lastDays(today).map((d) => ({ label: d === today ? "Today" : short(d), num: true })),
        [{ label: "Due by today", num: true, cls: "ct-due edge" }, { label: "Due later", num: true, cls: "ct-later" }]);
    // a facility's cells: earlier and the days (due by today), their sum, then later
    const cells = (c) => c.slice(0, DAYS + 1).concat([sum(c.slice(0, DAYS + 1)), c[DAYS + 1]]);
    const tot = new Array(DAYS + 2).fill(0);
    const rows = Array.from(byFacility(fs, m.test, today).entries()).map(([k, c]) => {
      c.forEach((v, i) => { tot[i] += v; });
      return [k, FAC_BUCKET[k] || ""].concat(cells(c));
    });
    table("t-ct-facility", cols, rows, ["Total", ""].concat(cells(tot)),
      { sort: { col: cols.length - 2, dir: -1 }, empty: "No DNs match." });

    renderOrderDates(today);
  }

  // The sheet's side blocks, by order date and for the whole network: Unfulfilled WH, Hold, and how many shipped
  // DNs the promise engine gave no EDD (left out when it isn't connected: that would be all of them).
  function renderOrderDates(today) {
    const days = lastDays(today);
    const col = (iso) => (iso < days[0] ? 0 : days.indexOf(iso) + 1); // 0 = earlier, then the days
    const blank = () => new Array(DAYS + 1).fill(0);
    const made = blank(), unf = blank(), hold = blank(), shipped = blank(), noEdd = blank();
    F.forEach((f) => {
      const iso = D.date[f[C.order]];
      if (iso > today) return;
      const i = col(iso), n = f[C.n];
      made[i] += n;
      if (D.facility[f[C.fac]].startsWith("Unfulfilled")) unf[i] += n;
      if (D.dn_status[f[C.dn]] === "Hold") hold[i] += n;
      if (f[C.flags] & HAS_AWB) {
        shipped[i] += n;
        if (!(f[C.flags] & HAS_EDD)) noEdd[i] += n;
      }
    });
    const pct = (a, b) => (b ? Math.round((1000 * a) / b) / 10 + "%" : "");
    const cols = [{ label: "" }, { label: "Earlier", num: true }]
      .concat(days.map((d) => ({ label: d === today ? "Today" : short(d), num: true })));
    cols.push({ label: "Total", num: true, cls: "ct-due edge" });
    const line = (label, cells) => [label].concat(cells, [sum(cells)]);
    const rows = [
      line("DNs created", made),
      line("Unfulfilled WH", unf),
      line("On hold", hold),
    ];
    if (RAW.counts.edd_rows !== null) {
      rows.push(line("Shipped DNs with no promise engine EDD", noEdd),
        ["… share of shipped DNs"].concat(noEdd.map((v, i) => pct(v, shipped[i])), [pct(sum(noEdd), sum(shipped))]));
    }
    // drawn in this order, not sortable: one row per measure
    const t = el("t-ct-orderdate");
    t.innerHTML = "<thead><tr>" + cols.map((c) => '<th class="' + (c.cls || "") + '">' + esc(c.label) + "</th>").join("") +
      "</tr></thead><tbody>" + rows.map((r) => "<tr>" + r.map((v, i) =>
        '<td class="' + (cols[i].cls || "") + '">' + (typeof v === "number" ? (v ? fmt(v) : '<span class="dim">·</span>') : esc(v)) + "</td>"
      ).join("") + "</tr>").join("") + "</tbody>";
  }

  // ---------- Warehouse Pendency ----------
  // The rows matching the tab's filters (Show not yet due aside), or all of them.
  function pendencyRows(filtered) {
    if (!filtered) return ROWS;
    const q = el("ct-p-q").value.trim().toLowerCase();
    const w = MS.pwh.sel, s = MS.pstatus.sel, c = MS.pcourier.sel, ch = MS.pchannel.sel;
    return ROWS.filter((r) =>
      (!w.size || w.has(r[P.wh])) && (!s.size || s.has(r[P.status])) && (!c.size || c.has(r[P.courier])) &&
      (!ch.size || ch.has(r[P.channel])) &&
      (!q || r[P.so].toLowerCase().includes(q) || r[P.dn].toLowerCase().includes(q) || r[P.pick].toLowerCase().includes(q)));
  }

  // What the tab shows: due today or earlier, plus the rest when Show not yet due is ticked.
  function shownPendency(today) {
    const all = pendencyRows(true);
    return el("ct-p-later").checked ? all : all.filter((r) => r[P.ideal] <= today);
  }

  function renderPendency() {
    const today = istToday();
    const rs = shownPendency(today);
    const later = el("ct-p-later").checked;
    const dnSet = (pred) => new Set(rs.filter(pred).map((r) => r[P.dn])).size;
    const due = rs.filter((r) => r[P.ideal] <= today);
    const oldest = due.reduce((a, r) => (!a || r[P.ideal] < a ? r[P.ideal] : a), "");
    const notYet = new Set(pendencyRows(true).filter((r) => r[P.ideal] > today).map((r) => r[P.dn])).size;

    el("ct-p-cards").innerHTML =
      '<div class="pcard crit"><div class="k">Pending DNs</div><div class="v">' + fmt(dnSet((r) => r[P.ideal] <= today)) +
        '</div><div class="s">due today or earlier, no AWB yet</div></div>' +
      '<div class="pcard"><div class="k">Overdue</div><div class="v">' + fmt(dnSet((r) => r[P.ideal] < today)) +
        '</div><div class="s">ideal pickup date already passed</div></div>' +
      '<div class="pcard"><div class="k">Due today</div><div class="v">' + fmt(dnSet((r) => r[P.ideal] === today)) +
        '</div><div class="s">ideal pickup date is today</div></div>' +
      '<div class="pcard"><div class="k">Oldest due</div><div class="v">' + (oldest ? esc(short(oldest)) : "—") +
        '</div><div class="s">' + (oldest ? fmt(daysBetween(oldest, today)) + " days late" : "nothing overdue") + "</div></div>" +
      '<div class="pcard"><div class="k">Not yet due</div><div class="v">' + fmt(notYet) +
        '</div><div class="s">' + (later ? "shown below" : "tick Show not yet due to list them") + "</div></div>";

    // by warehouse: DNs per status, and how many are overdue / due today (/ not yet due)
    const per = new Map();
    rs.forEach((r) => {
      if (!per.has(r[P.wh])) per.set(r[P.wh], { st: {}, over: new Set(), today: new Set(), later: new Set(), all: new Set() });
      const g = per.get(r[P.wh]);
      (g.st[r[P.status]] = g.st[r[P.status]] || new Set()).add(r[P.dn]);
      g.all.add(r[P.dn]);
      (r[P.ideal] < today ? g.over : r[P.ideal] === today ? g.today : g.later).add(r[P.dn]);
    });
    const cols = [{ label: "Warehouse" }].concat(PENDENCY_STATUSES.map((s) => ({ label: s, num: true })),
      [{ label: "Overdue", num: true, cls: "edge" }, { label: "Due today", num: true }]);
    if (later) cols.push({ label: "Not yet due", num: true, cls: "ct-later" });
    cols.push({ label: "Total", num: true, cls: "ct-due edge" });
    const rows = Array.from(per.entries()).map(([wh, g]) => [wh].concat(
      PENDENCY_STATUSES.map((s) => (g.st[s] ? g.st[s].size : 0)), [g.over.size, g.today.size],
      later ? [g.later.size] : [], [g.all.size]));
    const foot = ["Total"].concat(PENDENCY_STATUSES.map((s) => dnSet((r) => r[P.status] === s)),
      [dnSet((r) => r[P.ideal] < today), dnSet((r) => r[P.ideal] === today)],
      later ? [dnSet((r) => r[P.ideal] > today)] : [], [dnSet(() => true)]);
    table("t-ct-p-wh", cols, rows, foot, { sort: { col: cols.length - 1, dir: -1 }, empty: "No pending DNs match." });

    const lateCell = (d) => {
      if (d < 0) return '<span class="dim">due in ' + -d + " d</span>";
      if (d === 0) return '<span class="pill p-warn">today</span>';
      return '<span class="pill ' + (d >= 2 ? "p-crit" : "p-warn") + '">' + d + " d</span>";
    };
    const dateCell = (iso) => esc(short(iso));
    const dcols = [
      { label: "Sales order" }, { label: "DN", cls: "ct-left" }, { label: "Warehouse", cls: "ct-left" },
      { label: "Pick list", cls: "ct-left" }, { label: "Ideal pickup", render: dateCell }, { label: "Order date", render: dateCell },
      { label: "Order time" }, { label: "Courier", cls: "ct-left" }, { label: "Status", cls: "ct-left" }, { label: "Channel", cls: "ct-left" },
      { label: "Days late", num: true, render: lateCell },
    ];
    const drows = rs.map((r) => [r[P.so], r[P.dn], r[P.wh], r[P.pick], r[P.ideal], r[P.order], r[P.time], r[P.courier],
      r[P.status], r[P.channel], daysBetween(r[P.ideal], today)]);
    const n = table("t-ct-p-rows", dcols, drows, null, { sort: { col: 10, dir: -1 }, limit: ROW_LIMIT, empty: "No pending DNs match." });
    el("ct-p-shown").textContent = n > ROW_LIMIT
      ? "first " + fmt(ROW_LIMIT) + " of " + fmt(n) + " rows · Export CSV has them all"
      : fmt(n) + " rows";
  }

  function pendencyCsv() {
    const today = istToday();
    const q = (v) => {
      v = v === null || v === undefined ? "" : String(v);
      if (/^[=+\-@\t\r]/.test(v) && !/^-?\d+$/.test(v)) v = "'" + v; // text a spreadsheet would run as a formula
      return /[",\r\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; // a bare \r would start a new cell
    };
    const out = [RAW.pendency.columns.concat(["days_late"]).map(q).join(",")];
    shownPendency(today).forEach((r) => out.push(r.concat([daysBetween(r[P.ideal], today)]).map(q).join(",")));
    BX.saveCsv("warehouse_pendency_" + today + ".csv", out.join(String.fromCharCode(10)));
  }

  // ---------- views and filters ----------
  function render() {
    if (!RAW) return;
    if (view === "dashboard") renderDashboard(); else renderPendency();
  }

  function showView(v) {
    view = v;
    root.querySelectorAll(".ct-views button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.view === v)));
    el("ct-view-dashboard").hidden = v !== "dashboard";
    el("ct-view-pendency").hidden = v !== "pendency";
    try { localStorage.setItem("ct-view", v); } catch (e) { /* private window: just not remembered */ }
    render();
  }
  root.querySelectorAll(".ct-views button").forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));
  try { if (localStorage.getItem("ct-view") === "pendency") view = "pendency"; } catch (e) { /* not remembered */ }

  el("ct-metric").innerHTML = METRICS.map((m) =>
    '<button type="button" data-metric="' + m.id + '" aria-pressed="false">' + esc(m.label) + "</button>").join("");
  el("ct-metric").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => { metric = b.dataset.metric; render(); }));
  statusSelect.addEventListener("change", render);
  el("ct-facility-csv").addEventListener("click", () => {
    const name = metric === "status" ? statusSelect.value : metric;
    BX.saveCsv("control_tower_" + name.replace(/[^A-Za-z0-9]+/g, "_").toLowerCase() + "_" + istToday() + ".csv", BX.tableCsv("t-ct-facility"));
  });
  el("ct-reset").addEventListener("click", () => {
    MS.bucket.set(DEFAULT_BUCKETS.filter((b) => D.bucket.indexOf(b) !== -1));
    MS.facility.setValues(facilitiesFor(MS.bucket.sel));
    MS.facility.clear();
    MS.courier.clear();
    MS.channel.clear();
    render();
  });
  el("ct-p-q").addEventListener("input", render);
  el("ct-p-later").addEventListener("change", render);
  el("ct-p-csv").addEventListener("click", pendencyCsv);
  el("ct-p-reset").addEventListener("click", () => {
    ["pwh", "pstatus", "pcourier", "pchannel"].forEach((k) => MS[k].clear());
    el("ct-p-q").value = "";
    el("ct-p-later").checked = false;
    render();
  });

  const facilitiesFor = (buckets) =>
    uniq(Object.keys(FAC_BUCKET).filter((f) => !buckets.size || buckets.has(FAC_BUCKET[f])));

  function init(raw) {
    const first = !RAW;
    RAW = raw;
    D = raw.dims;
    F = raw.facts;
    ROWS = raw.pendency.rows;
    FAC_BUCKET = {};
    F.forEach((f) => { FAC_BUCKET[D.facility[f[C.fac]]] = D.bucket[f[C.bucket]]; });
    root.querySelectorAll(".ct-window").forEach((w) => { w.textContent = raw.window_days; });
    const edd = raw.counts.edd_rows;
    el("ct-edd-note").hidden = edd !== null;
    el("ct-edd-note").textContent = raw.edd_problem
      ? "The promise engine couldn't be reached for this build (" + raw.edd_problem + "), so every ideal pickup date is on the noon rule. The next build tries again."
      : "The promise engine isn't connected (MONGO_URI isn't set on the server), so every ideal pickup date is on the noon rule.";
    el("ct-asof").innerHTML = "Built <strong>" + esc(raw.generated_at_ist) + " IST</strong> from " + fmt(raw.counts.dns) +
      " DNs created in the last " + raw.window_days + " days (ERP), " + fmt(raw.counts.cp_rows) + " AWBs (Clickpost)" +
      (edd === null ? "" : " and " + fmt(edd) + " promises (promise engine)");

    const make = (host, key, label, values, onChange) => BX.makeMS(MS, host, key, label, values, onChange || render);
    make("ct-ms-bucket", "bucket", "Type", uniq(D.bucket), () => { MS.facility.setValues(facilitiesFor(MS.bucket.sel)); render(); });
    if (first) MS.bucket.set(DEFAULT_BUCKETS.filter((b) => D.bucket.indexOf(b) !== -1));
    make("ct-ms-facility", "facility", "Facility", facilitiesFor(MS.bucket.sel));
    make("ct-ms-courier", "courier", "Courier", uniq(D.courier));
    make("ct-ms-channel", "channel", "Channel", uniq(D.channel));
    make("ct-ms-p-wh", "pwh", "Warehouse", uniq(ROWS.map((r) => r[P.wh])));
    make("ct-ms-p-status", "pstatus", "Status", PENDENCY_STATUSES.filter((s) => ROWS.some((r) => r[P.status] === s)));
    make("ct-ms-p-courier", "pcourier", "Courier", uniq(ROWS.map((r) => r[P.courier])));
    make("ct-ms-p-channel", "pchannel", "Channel", uniq(ROWS.map((r) => r[P.channel])));

    const keep = statusSelect.value || DEFAULT_STATUS;
    const statuses = uniq(D.status.filter(Boolean));
    statusSelect.innerHTML = statuses.map((s) => "<option>" + esc(s) + "</option>").join("");
    statusSelect.value = statuses.indexOf(keep) !== -1 ? keep : statuses[0] || "";
    showView(view);
  }

  // ---------- loading, and following a rebuild ----------
  // The data comes from /api/control-tower the first time the panel is opened. Until there is data it says why:
  // the first build still running (and follows it), or that build failed. A rebuild (Refresh data, the top bar,
  // or the data getting old while someone looks) is followed the same way; data_refresh.js fires
  // "panel:data-changed" for control_tower when it ends, and the page asks again with the ETag it holds.
  const STATUS_POLL_MS = 8000;
  const FOLLOW_POLL_MS = 4000;
  const NOTICE_MS = 6000;
  let opened = false, etag = null, busy = false, again = false, pollTimer = null, noticeTimer = null, shown = null;

  function when(epoch) {
    return new Date(epoch * 1000).toLocaleString(undefined, { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" });
  }

  function showState(kind, title, text) {
    stateEl.className = "bx-state " + kind;
    stateEl.innerHTML = '<span class="bx-spinner" aria-hidden="true"></span><strong></strong><p></p>';
    stateEl.querySelector("strong").textContent = title;
    stateEl.querySelector("p").textContent = text || "";
    stateEl.hidden = false;
  }

  const tryNow = () => (document.body.dataset.role === "viewer" ? "." : ", or press Refresh data to try now.");

  function showFreshness(st) {
    shown = st;
    clearTimeout(noticeTimer);
    const doing = !!(st && st.refreshing);
    refreshBtn.disabled = doing;
    refreshLabel.textContent = doing ? "Refreshing…" : "Refresh data";
    fresh.classList.remove("busy", "stale", "notice");
    banner.className = "banner hidden bx-banner";
    if (!st || !st.fetched_at) { fresh.textContent = ""; return; }
    const from = when(st.fetched_at);
    fresh.textContent = doing ? "Refreshing… showing data from " + from
      : "Data as of " + from + (st.fresh_for_minutes ? " · updates every " + st.fresh_for_minutes + " min" : "");
    if (doing) fresh.classList.add("busy");
    if (st.error && !doing) {
      fresh.classList.add("stale");
      banner.textContent = "The latest refresh failed (" + st.error + "), so this shows the data from " + from +
        ". It tries again automatically" + tryNow();
      banner.className = "banner error bx-banner";
    }
  }

  function notice(text) {
    showFreshness(shown);
    fresh.classList.add("notice");
    fresh.textContent = text;
    noticeTimer = setTimeout(() => showFreshness(shown), NOTICE_MS);
  }

  async function status() {
    try {
      const res = await fetch("/api/control-tower/status");
      return res.ok ? await res.json() : null;
    } catch (e) {
      return null;
    }
  }

  async function load() {
    if (busy) { again = true; return; }
    busy = true;
    clearTimeout(pollTimer);
    let next = 0;
    try {
      const st = (await status()) || shown;
      const res = await fetch("/api/control-tower", { headers: etag ? { "If-None-Match": etag } : {} });
      showFreshness(st);
      if (st && st.refreshing) next = FOLLOW_POLL_MS;
      if (res.ok) {
        const data = await res.json();
        stateEl.hidden = true;
        body.hidden = false;
        init(data);
        etag = res.headers.get("ETag");
        return;
      }
      if (res.status === 304 || etag) return; // unchanged, or keep showing what we have
      if (res.status === 503) {
        next = next || STATUS_POLL_MS;
        if (st && st.refreshing) {
          showState("building", "Building the Control Tower",
            "It reads the ERP, Clickpost and promise engine, which takes about three minutes. This fills in by itself when it's ready.");
        } else if (st && st.error) {
          showState("error", "The Control Tower's data couldn't be built", st.error + " It tries again automatically within ten minutes" + tryNow());
        } else {
          showState("building", "Waiting for the data", "The build is about to start.");
        }
        return;
      }
      let msg = "";
      try { msg = (await res.json()).error || ""; } catch (e) { /* no body */ }
      showState("error", "Couldn't load the Control Tower", msg || "Request failed (" + res.status + ").");
    } catch (e) {
      if (!etag) showState("error", "Couldn't load the Control Tower", "Couldn't reach the server. Refresh the page to try again.");
    } finally {
      busy = false;
      if (again) { again = false; load(); }
      else if (next) pollTimer = setTimeout(load, next);
    }
  }

  refreshBtn.addEventListener("click", async () => {
    refreshBtn.disabled = true;
    refreshLabel.textContent = "Starting…";
    let res = null, data = {};
    try {
      res = await fetch("/api/control-tower/refresh", { method: "POST" });
      data = await res.json();
    } catch (e) { /* no answer, or not JSON: said below */ }
    if (!res || !res.ok) {
      notice(data.error || (res ? "Couldn't start the refresh (" + res.status + ")." : "Couldn't reach the server. Please try again."));
      return;
    }
    window.dispatchEvent(new CustomEvent("panel:refresh-started")); // the top bar shows it too (data_refresh.js)
    showFreshness(data);
    clearTimeout(pollTimer);
    pollTimer = setTimeout(load, FOLLOW_POLL_MS);
  });

  document.querySelector('.side-nav-item[data-panel="control-tower"]').addEventListener("click", () => {
    if (!opened) {
      opened = true;
      showState("loading", "Loading…", "");
      load();
    } else {
      render(); // "Today" may have moved on since it was drawn
    }
  });
  window.addEventListener("panel:data-changed", (e) => {
    if (opened && e.detail.sources.includes("control_tower")) load();
  });
})();
