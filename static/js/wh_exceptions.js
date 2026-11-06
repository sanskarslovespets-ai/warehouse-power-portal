(function () {
  "use strict";

  // Warehouse Dashboards > Warehouse Exceptions: a port of breach-explorer.apps.internal.supertails.com/warehouse
  // (upstream: dashboards/warehouse_exceptions.template.html in SupertailsPCPL/edd-observability, the same commit
  // as breach_pipeline/README.md names). Five views - Not found, Packing / Putback / RTO initiated pendency and
  // Complaints - over one dataset, /api/breach-explorer/warehouse: the three payloads upstream injects into its
  // page, built by breach_pipeline/scripts/build_warehouse_data.py.
  //
  // Every computation below - what is counted, how rows are aged, sorted and exported - is upstream's, kept line
  // for line (ES5 and all) so the numbers match the original page. What changed is around it:
  // - the data comes from BX.tab (be_common.js): loading, freshness, Refresh data, and new data after a refresh;
  // - ids start "wx-" ($("whtable") is #wx-whtable), the layout is the panel's .bx styles;
  // - controls are wired once, and new data keeps each view's picks (a date preset stays that preset);
  // - CSVs are saved with BX.saveCsv, and cells a spreadsheet would run as a formula are kept as text.

  var $ = function (id) { return document.getElementById("wx-" + id); };
  var fmt = function (n) { return (n || 0).toLocaleString("en-IN"); };
  var pct = function (n, d) { return d ? (100 * n / d) : 0; };
  var esc = function (s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  };

  // A colour per mother warehouse that runs bin determination; grey (--wx-dead) for the others. Upstream had four
  // series colours for seven warehouses, which left a fifth active one without a dot.
  var SER = ["--wx-s1", "--wx-s2", "--wx-s3", "--wx-s4", "--wx-s5", "--wx-s6", "--wx-s7"];

  var LIMIT = 250;
  var D, WH, ACTIVE, DATES, BINS, ITEMS, INAMES, STATUS, facts, base, CANCEL, COLOR;
  var CX, CXWH, BUCKETS, CXCOURIER, CX_MIN, CX_MAX;
  var P, PWH, TODAY;
  var F_WH = 0, F_DT = 1, F_DN = 2, F_BIN = 3, F_ITEM = 4, F_QTY = 5, F_AV = 6, F_ST = 7;

  // Picks that outlive new data. nfPreset / cxPreset: the pressed date chip (-1 = dates typed in by hand).
  var state = {
    from: null, to: null, wh: [],
    whSort: { k: "rate", dir: -1 }, binSort: { k: "lines", dir: -1 }, skuSort: { k: "qty", dir: -1 },
    binWh: "all", skuWh: "all"
  };
  var nfPreset = 1, cxPreset = 1;
  var cxstate = { wh: "all", bucket: "all", dmgWh: "all", from: null, to: null,
                  whSort: { k: "rate", dir: -1 }, dmgSort: { k: "total", dir: -1 } };
  var pstate = {
    packing: { wh: "all", age: "all", sort: { k: "late", dir: -1 }, whSort: { k: "total", dir: -1 } },
    putback: { wh: "all", age: "all", sort: { k: "age", dir: -1 }, whSort: { k: "total", dir: -1 } },
    rto:     { wh: "all", age: "all", sort: { k: "age", dir: -1 }, whSort: { k: "total", dir: -1 } }
  };
  var current = "notfound";
  var wired = false;

  function inRange(di) { var d = DATES[di]; return d >= state.from && d <= state.to; }

  /* ================= not found ================= */
  function compute() {
    var whSet = {}; state.wh.forEach(function (w) { whSet[w] = 1; });
    var perWh = {}, bin = {}, sku = {};
    WH.forEach(function (_, i) { perWh[i] = { orders: new Set(), qty: 0, cancels: new Set(), dns: 0, whCancels: 0 }; });
    for (var i = 0; i < facts.length; i++) {
      var f = facts[i];
      if (!whSet[f[F_WH]] || !inRange(f[F_DT])) continue;
      var p = perWh[f[F_WH]];
      p.orders.add(f[F_DN]); p.qty += f[F_QTY];
      if (f[F_ST] === CANCEL) p.cancels.add(f[F_DN]);

      var bk = f[F_BIN] + "|" + f[F_WH];
      var b = bin[bk] || (bin[bk] = { bin: f[F_BIN], wh: f[F_WH], lines: 0, qty: 0, dns: new Set() });
      b.lines++; b.qty += f[F_QTY]; b.dns.add(f[F_DN]);

      var sk = f[F_ITEM] + "|" + f[F_WH];
      var s = sku[sk] || (sku[sk] = { item: f[F_ITEM], wh: f[F_WH], lines: 0, qty: 0, dns: new Set() });
      s.lines++; s.qty += f[F_QTY]; s.dns.add(f[F_DN]);
    }
    for (var j = 0; j < base.length; j++) {
      var r = base[j];
      if (!whSet[r[0]] || !inRange(r[1])) continue;
      perWh[r[0]].dns += r[2]; perWh[r[0]].whCancels += r[3];
    }
    return { perWh: perWh, bin: bin, sku: sku };
  }

  function sortRows(arr, st) {
    return arr.sort(function (a, b) {
      var va = a[st.k], vb = b[st.k];
      return va < vb ? -st.dir : va > vb ? st.dir : 0;
    });
  }
  function deadMsg(pick, noun) {
    if (pick !== "all" && !ACTIVE[+pick])
      return esc(WH[+pick]) + " does not run bin determination, so no " + noun + " are recorded there.";
    return "No " + noun + " in this range.";
  }
  function sevClass(r) { return r >= 8 ? "l4" : r >= 3 ? "l3" : r >= 1 ? "l2" : r > 0 ? "l1" : ""; }
  function dot(color) { return '<span class="wx-dot" style="background:' + color + '"></span>'; }

  function whData(A) {
    return WH.map(function (name, i) {
      var p = A.perWh[i];
      return { i: i, name: name, dns: p.dns, orders: p.orders.size, qty: p.qty,
               rate: pct(p.orders.size, p.dns), cancels: p.cancels.size,
               whCancels: p.whCancels, on: ACTIVE[i] };
    });
  }
  function renderWh(A) {
    var data = whData(A), maxRate = 0;
    data.forEach(function (d) { if (d.rate > maxRate) maxRate = d.rate; });
    var s = state.whSort;
    data = data.slice().sort(function (a, b) {
      if (a.on !== b.on) return b.on - a.on;
      var va = a[s.k], vb = b[s.k];
      return va < vb ? -s.dir : va > vb ? s.dir : 0;
    });
    var T = { dns: 0, qty: 0, orders: 0, cancels: 0 };
    data.forEach(function (d) { T.dns += d.dns; T.qty += d.qty; T.orders += d.orders; T.cancels += d.cancels; });
    var html = data.map(function (d) {
      if (!d.on) return '<tr class="inactive"><td>' + dot("var(--wx-dead)") + esc(d.name) + "</td>" +
        '<td class="num">' + fmt(d.dns) + "</td>" +
        '<td colspan="4" style="text-align:left">' +
        '<span class="pill dead">Process not in use</span></td></tr>';
      var w = maxRate ? Math.round(70 * d.rate / maxRate) : 0;
      return "<tr><td>" + dot(COLOR[d.i]) + esc(d.name) + "</td>" +
        '<td class="num">' + fmt(d.dns) + "</td>" +
        '<td class="num">' + fmt(d.orders) + "</td>" +
        '<td class="num">' + fmt(d.qty) + "</td>" +
        '<td><div class="ratewrap"><span class="pill ' + sevClass(d.rate) + '">' +
        d.rate.toFixed(2) + '%</span><span class="track">' +
        '<span class="ratebar" style="width:' + w + 'px"></span></span></div></td>' +
        '<td class="num">' + fmt(d.cancels) + "</td></tr>";
    }).join("");
    html += '<tr class="total"><td>All warehouses</td><td class="num">' + fmt(T.dns) + "</td>" +
      '<td class="num">' + fmt(T.orders) + "</td>" +
      '<td class="num">' + fmt(T.qty) + '</td><td class="num">' + pct(T.orders, T.dns).toFixed(2) + "%</td>" +
      '<td class="num">' + fmt(T.cancels) + "</td></tr>";
    $("whtable").querySelector("tbody").innerHTML = html;
    var off = WH.filter(function (_, i) { return !ACTIVE[i]; });
    /* Which warehouses are "not in use" is computed by the exporter from the data, so this
       note names them dynamically and states only what that test proves. */
    $("whnote").textContent = off.length
      ? off.join(", ") + " recorded no not-found (suspense) moves in the last " + (D.dims.activeDays || 30) +
        " days, so the bin-determination step is not in use there right now. Read those rows as " +
        "not reporting, not as zero misses."
      : "";
  }

  function binData(A) {
    var pick = state.binWh;
    var rows = Object.keys(A.bin).map(function (k) {
      var b = A.bin[k];
      return { code: BINS[b.bin], wh: b.wh, lines: b.lines, qty: b.qty, dns: b.dns.size };
    });
    if (pick !== "all") rows = rows.filter(function (r) { return r.wh === +pick; });
    return sortRows(rows, state.binSort);
  }
  function renderBin(A) {
    var rows = binData(A), shown = rows.slice(0, LIMIT);
    var max = 0; shown.forEach(function (r) { if (r.lines > max) max = r.lines; });
    $("bintable").querySelector("tbody").innerHTML = shown.map(function (r, i) {
      var w = max ? Math.round(70 * r.lines / max) : 0;
      return '<tr><td><span class="rank num">' + (i + 1) + '</span><span class="mono">' + esc(r.code) + "</span></td>" +
        "<td>" + dot(COLOR[r.wh]) + esc(WH[r.wh]) + "</td>" +
        '<td><div class="ratewrap"><span class="num">' + fmt(r.lines) + "</span>" +
        '<span class="track"><span class="qtybar" style="width:' + w + 'px"></span></span></div></td>' +
        '<td class="num">' + fmt(r.qty) + '</td><td class="num">' + fmt(r.dns) + "</td></tr>";
    }).join("") || '<tr><td colspan="5" class="dim">' + deadMsg(state.binWh, "bins") + "</td></tr>";
    $("binnote").textContent = rows.length === 0 ? deadMsg(state.binWh, "bins")
      : fmt(rows.length) + " bins in " +
      (state.binWh === "all" ? "range" : WH[+state.binWh]) +
      (rows.length > LIMIT ? "; showing the top " + LIMIT + ". The CSV has all of them." : ".");
  }

  function skuData(A) {
    var pick = state.skuWh, byItem = {};
    Object.keys(A.sku).forEach(function (k) {
      var e = A.sku[k];
      var it = byItem[e.item] || (byItem[e.item] = { lines: 0, qty: 0, dns: 0, whs: [] });
      it.whs.push(e.wh);                        // every warehouse the SKU goes missing in
      if (pick === "all" || e.wh === +pick) {   // measures follow the picked warehouse
        it.lines += e.lines; it.qty += e.qty; it.dns += e.dns.size;
      }
    });
    var rows = Object.keys(byItem).map(function (k) {
      var it = byItem[k];
      return { code: ITEMS[k], name: INAMES[k] || "", lines: it.lines, qty: it.qty, dns: it.dns,
               whs: it.whs.length,
               whList: it.whs.map(function (w) { return WH[w]; }).join(", ") };
    }).filter(function (r) { return r.lines > 0; });
    return sortRows(rows, state.skuSort);
  }
  function renderSku(A) {
    var rows = skuData(A), shown = rows.slice(0, LIMIT);
    var max = 0; shown.forEach(function (r) { if (r.qty > max) max = r.qty; });
    $("skutable").querySelector("tbody").innerHTML = shown.map(function (r, i) {
      var w = max ? Math.round(70 * r.qty / max) : 0;
      return '<tr><td><span class="rank num">' + (i + 1) + '</span><span class="mono">' + esc(r.code) +
        '</span><span class="sub">' + esc(r.name) + "</span></td>" +
        '<td><div class="ratewrap"><span class="num">' + fmt(r.qty) + "</span>" +
        '<span class="track"><span class="qtybar" style="width:' + w + 'px"></span></span></div></td>' +
        '<td class="num">' + fmt(r.lines) + '</td><td class="num">' + fmt(r.dns) + "</td>" +
        '<td class="num" title="' + esc(r.whList) + '">' + r.whs + "</td></tr>";
    }).join("") || '<tr><td colspan="5" class="dim">' + deadMsg(state.skuWh, "SKUs") + "</td></tr>";
    $("skunote").textContent = rows.length === 0 ? deadMsg(state.skuWh, "SKUs")
      : fmt(rows.length) + " SKUs in " +
      (state.skuWh === "all" ? "range" : WH[+state.skuWh]) +
      (rows.length > LIMIT ? "; showing the top " + LIMIT + ". The CSV has all of them." : ".");
  }

  /* ---------- CSV ---------- */
  // Upstream's, plus one guard: a cell a spreadsheet would run as a formula (an item name or a ticket's text
  // starting with = + - @) is kept as text. Plain numbers, negative ones too, are left alone.
  function toCsv(head, rows) {
    var q = function (v) {
      v = String(v == null ? "" : v);
      if (/^[=+\-@\t\r]/.test(v) && !/^-?[0-9.]+$/.test(v)) v = "'" + v;
      return /[",\n\r]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
    };
    return [head.join(",")].concat(rows.map(function (r) { return r.map(q).join(","); })).join("\n");
  }
  /* The tables on screen are aggregates, but an aggregate is where an investigation
     stops. Every export is therefore one row per not-found LINE - the delivery note,
     the bin it was picked from and the SKU that was missing - filtered exactly as the
     view it came from. Group it back up in Excel and you get the table; you cannot go
     the other way from a summary. */
  var NF_DETAIL_HEAD = ["delivery_note", "warehouse", "warehouse_code", "marked_on", "bin",
                        "sku", "item_name", "qty_not_found", "qty_available", "dn_status"];
  function nfDetail(pick) {
    var out = [];
    for (var i = 0; i < facts.length; i++) {
      var f = facts[i];
      if (!inRange(f[F_DT])) continue;
      if (pick !== "all" && f[F_WH] !== +pick) continue;
      out.push([f[F_DN], WH[f[F_WH]], D.dims.whCode[f[F_WH]], DATES[f[F_DT]],
                BINS[f[F_BIN]], ITEMS[f[F_ITEM]], INAMES[f[F_ITEM]],
                f[F_QTY], f[F_AV], STATUS[f[F_ST]]]);
    }
    /* newest first, then grouped by delivery note so one order's lines sit together */
    return out.sort(function (a, b) {
      return a[3] === b[3] ? (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0) : (a[3] < b[3] ? 1 : -1);
    });
  }
  function csvFor(kind) {
    var pick = kind === "bin" ? state.binWh : kind === "sku" ? state.skuWh : "all";
    return toCsv(NF_DETAIL_HEAD, nfDetail(pick));
  }

  function saveCsv(btn) {
    var kind = btn.dataset.wxCsv;
    var nf = (kind === "wh" || kind === "bin" || kind === "sku");
    var cx = (kind === "damaged" || kind === "complaints");
    var text = nf ? csvFor(kind) : (kind === "damaged" ? cxCsv()
        : kind === "complaints" ? cxWarehouseCsv() : pendCsv(kind));
    var name = nf ? "notfound_" + kind + "_" + state.from + "_to_" + state.to + ".csv"
            : cx ? (kind === "complaints" ? "complaints_by_warehouse" : "complaints_damaged") +
                   "_" + cxstate.from + "_to_" + cxstate.to + ".csv"
                 : kind + "_pendency_" + P.today + ".csv";
    BX.saveCsv(name, text);
    BX.flash(btn, "Saved");
  }

  /* ---------- controls ---------- */
  function nfPresets() {
    var back = function (n) { return DATES[Math.max(0, DATES.length - n)]; };
    return [["7 days", back(7)], ["30 days", back(30)], ["90 days", back(90)], ["All", DATES[0]]];
  }
  function buildControls() {
    var last = DATES[DATES.length - 1], presets = nfPresets();
    if (nfPreset >= 0) {
      state.from = presets[nfPreset][1]; state.to = last;
    } else {  // dates picked by hand stay, inside the new data's days
      if (!state.from || state.from < DATES[0]) state.from = DATES[0];
      if (!state.to || state.to > last) state.to = last;
    }
    $("presets").innerHTML = presets.map(function (p, i) {
      return '<button class="chip" type="button" data-i="' + i + '" aria-pressed="' + (i === nfPreset) + '">' + p[0] + "</button>";
    }).join("");
    $("from").value = state.from; $("to").value = state.to;
    $("from").min = $("to").min = DATES[0];
    $("from").max = $("to").max = last;
  }
  function markSort(id, st) {
    [].forEach.call($(id).querySelectorAll("thead th"), function (th) {
      var a = th.querySelector(".arrow"); if (a) a.remove();
      th.classList.toggle("sorted", th.dataset.k === st.k);
      if (th.dataset.k === st.k) {
        var s = document.createElement("span");
        s.className = "arrow"; s.textContent = st.dir < 0 ? "▼" : "▲";
        th.appendChild(s);
      }
    });
  }

  function syncWhSelects() {
    ["binWh", "skuWh"].forEach(function (key) {
      var el = $(key === "binWh" ? "binwh" : "skuwh");
      // a per-view pick that the global chips have since removed falls back to All
      if (state[key] !== "all" && state.wh.indexOf(+state[key]) === -1) state[key] = "all";
      var sel = WH.map(function (_, i) { return i; });
      var live = sel.filter(function (w) { return ACTIVE[w]; }).length;
      var opts = '<option value="all">All warehouses (' + live + " reporting)</option>";
      sel.forEach(function (w) {
        opts += '<option value="' + w + '"' + (String(w) === state[key] ? " selected" : "") + ">" +
                esc(WH[w]) + (ACTIVE[w] ? "" : " - not in use") + "</option>";
      });
      el.innerHTML = opts;
      el.value = state[key];
    });
  }

  function draw() {
    if (state.from > state.to) {
      var t = state.from; state.from = state.to; state.to = t;
      $("from").value = state.from; $("to").value = state.to;
    }
    syncWhSelects();
    var A = compute();
    renderWh(A); renderBin(A); renderSku(A);
    markSort("whtable", state.whSort);
    markSort("bintable", state.binSort);
    markSort("skutable", state.skuSort);
  }

  /* ================= complaints ================= */
  // the "reported as" text, split by what it implies about cause
  var PACKAGING_SUB = { "Inner Packaging is damaged": 1, "Outer Packaging is damaged": 1,
                        "Packaging Damaged (Inner / Outer)": 1 };
  var PRODUCT_SUB = { "Product is broken/damaged": 1, "Product Broken / Damaged": 1,
                      "Product not working": 1, "Used Product Delivered": 1 };
  function damageKind(sub) {
    return PACKAGING_SUB[sub] ? "packaging" : PRODUCT_SUB[sub] ? "product" : "other";
  }

  function cxShiftDays(iso, n) {
    var d = new Date(iso + "T00:00:00Z");
    d.setUTCDate(d.getUTCDate() + n);
    return d.toISOString().slice(0, 10);   // UTC throughout: local time would shift the day in IST
  }

  /* rows: [wh, dateIdx, bucket, subIssue, ref, courierIdx, ticketId, dn] */
  function cxAllRows() {
    return CX.rows.map(function (r) {
      return { wh: r[0], date: CX.date[r[1]], bucket: r[2], sub: r[3], ref: r[4],
               courier: CXCOURIER[r[5]] || "Unknown", ticket: r[6] || "", dn: r[7] || "",
               kind: damageKind(r[3]) };
    }).filter(function (r) { return r.date >= cxstate.from && r.date <= cxstate.to; });
  }

  function cxDamagedRows() {
    return cxAllRows().filter(function (r) {
      if (r.bucket !== "damaged") return false;
      if (cxstate.dmgWh !== "all" && r.wh !== +cxstate.dmgWh) return false;
      return true;
    });
  }

  function cxRate(n, shipments) { return shipments ? 100 * n / shipments : 0; }

  function renderComplaints() {
    var per = {};
    CXWH.forEach(function (_, i) {
      per[i] = { i: i, total: 0, shipments: 0 };
      BUCKETS.forEach(function (b) { per[i][b] = 0; });
    });
    CX.shipDaily.forEach(function (r) {
      if (r[1] >= cxstate.from && r[1] <= cxstate.to) per[r[0]].shipments += r[3];
    });
    CX.rows.forEach(function (r) {
      var d = CX.date[r[1]];
      if (d < cxstate.from || d > cxstate.to) return;
      per[r[0]][r[2]]++; per[r[0]].total++;
    });
    CXWH.forEach(function (_, i) { per[i].rate = cxRate(per[i].total, per[i].shipments); });

    var data = sortBy(CXWH.map(function (_, i) { return per[i]; }), cxstate.whSort);
    var T = { total: 0, shipments: 0 };
    BUCKETS.forEach(function (b) { T[b] = 0; });
    data.forEach(function (d) {
      T.total += d.total; T.shipments += d.shipments;
      BUCKETS.forEach(function (b) { T[b] += d[b]; });
    });

    // one shared scale so the per-10k bars are comparable across warehouses
    var maxRate = 0;
    data.forEach(function (d) { if (d.rate > maxRate) maxRate = d.rate; });

    $("cx-wh").querySelector("tbody").innerHTML = data.map(function (d) {
      var w = maxRate ? Math.round(70 * d.rate / maxRate) : 0;
      return "<tr><td>" + dot(COLOR[d.i]) + esc(CXWH[d.i]) + '</td><td class="num">' + fmt(d.shipments) + "</td>" +
        BUCKETS.map(function (b) { return pcell(d[b]); }).join("") +
        '<td class="num">' + fmt(d.total) + "</td>" +
        '<td><div class="ratewrap"><span class="num">' + d.rate.toFixed(2) + "%</span>" +
        '<span class="track"><span class="qtybar" style="width:' + w + 'px"></span></span></div></td></tr>';
    }).join("") +
      '<tr class="total"><td>All warehouses</td><td class="num">' + fmt(T.shipments) + "</td>" +
      BUCKETS.map(function (b) { return '<td class="num">' + fmt(T[b]) + "</td>"; }).join("") +
      '<td class="num">' + fmt(T.total) + '</td><td class="num">' +
      cxRate(T.total, T.shipments).toFixed(2) + "%</td></tr>";

    var dks = 0, unt = 0, k;
    for (k in (CX.darkstore || {})) dks += CX.darkstore[k];
    for (k in (CX.untraced || {})) unt += CX.untraced[k];
    $("cx-note").textContent =
      "Complaints are traced to a warehouse through the shipment's AWB, falling back to the " +
      "delivery note when the ticket carries one instead. " + fmt(dks) + " more were traced to a " +
      "darkstore rather than a mother warehouse, so they are correctly out of scope here. A " +
      "further " + fmt(unt) + " carry a reference too damaged to trace (NA, 0, blank), which is " +
      "how much of a floor these counts are. Customer-fault and investigation-refuted tickets " +
      "are excluded (" + fmt(CX.refuted) + " of them).";

    renderDamagedByCourier();
    markSort("cx-wh", cxstate.whSort); markSort("cx-dmg", cxstate.dmgSort);
  }

  function renderDamagedByCourier() {
    var per = {};
    CXCOURIER.forEach(function (_, i) {
      per[i] = { i: i, shipments: 0, total: 0, packaging: 0, product: 0, other: 0 };
    });
    CX.shipDaily.forEach(function (r) {
      if (r[1] < cxstate.from || r[1] > cxstate.to) return;
      if (cxstate.dmgWh !== "all" && r[0] !== +cxstate.dmgWh) return;
      per[r[2]].shipments += r[3];
    });
    cxDamagedRows().forEach(function (r) {
      var i = CXCOURIER.indexOf(r.courier);
      if (i < 0) return;
      per[i].total++; per[i][r.kind]++;
    });
    var data = CXCOURIER.map(function (_, i) { return per[i]; })
      .filter(function (d) { return d.total > 0 || d.shipments > 0; });
    data.forEach(function (d) { d.rate = cxRate(d.total, d.shipments); });
    data = sortBy(data, cxstate.dmgSort);

    var T = { shipments: 0, total: 0, packaging: 0, product: 0, other: 0 };
    data.forEach(function (d) { for (var k in T) T[k] += d[k]; });

    var body = data.map(function (d) {
      return "<tr><td>" + esc(CXCOURIER[d.i]) + "</td>" +
        '<td class="num">' + fmt(d.shipments) + "</td>" +
        '<td class="num">' + fmt(d.total) + "</td>" +
        '<td class="num">' + (d.shipments ? d.rate.toFixed(2) + "%" : '<span class="dim">-</span>') + "</td>" +
        pcell(d.packaging) + pcell(d.product) + pcell(d.other) + "</tr>";
    }).join("");
    if (!body) body = '<tr><td colspan="7" class="dim">No damage reported here.</td></tr>';
    body += '<tr class="total"><td>All couriers</td><td class="num">' + fmt(T.shipments) + "</td>" +
      '<td class="num">' + fmt(T.total) + '</td><td class="num">' +
      cxRate(T.total, T.shipments).toFixed(2) + '%</td><td class="num">' + fmt(T.packaging) +
      '</td><td class="num">' + fmt(T.product) + '</td><td class="num">' + fmt(T.other) + "</td></tr>";
    $("cx-dmg").querySelector("tbody").innerHTML = body;

    $("cx-dmg-note").textContent =
      "Reported as: packaging damage covers inner and outer packaging; product broken covers " +
      "the item itself, including not-working and used-product reports. Shipments counts what " +
      "that courier carried from the selected warehouse in the date range, so the percentage " +
      "is the courier's own damage rate. A courier with few shipments swings hard on one " +
      "complaint.";
  }

  /* delivery_note is the join key back into the ERP and the other exports. It is blank
     only where the ticket's reference resolved to no ClickPost shipment at all. */
  function cxCsv() {
    return toCsv(["ticket_id", "raised", "delivery_note", "warehouse", "courier",
                  "reported_as", "cause", "awb_or_dn"],
      cxDamagedRows().map(function (r) {
        return [r.ticket, r.date, r.dn, CXWH[r.wh], r.courier, r.sub, r.kind, r.ref]; }));
  }

  function cxWarehouseCsv() {
    return toCsv(["ticket_id", "raised", "delivery_note", "warehouse", "complaint_type",
                  "reported_as", "courier", "awb_or_dn"],
      cxAllRows().sort(function (a, b) { return a.date < b.date ? 1 : a.date > b.date ? -1 : 0; })
        .map(function (r) {
          return [r.ticket, r.date, r.dn, CXWH[r.wh], CX.bucketLabel[r.bucket], r.sub,
                  r.courier, r.ref]; }));
  }

  function cxSyncInputs() {
    $("cx-from").value = cxstate.from; $("cx-to").value = cxstate.to;
    $("cx-range-note").textContent = "Showing " + cxstate.from + " to " + cxstate.to +
      ". Shipments counts orders placed at that warehouse in the same range and is the " +
      "denominator behind % of shipments. A ticket is usually raised a few days after the " +
      "shipment it refers to, so the two are aligned by period, not order by order.";
  }

  var CX_PRESETS = [["7 days", 7], ["30 days", 30], ["90 days", 90], ["All", null]];
  function buildComplaintsControls() {
    if (cxPreset >= 0) {
      var days = CX_PRESETS[cxPreset][1];
      cxstate.to = CX_MAX;
      cxstate.from = days ? cxShiftDays(CX_MAX, -(days - 1)) : CX_MIN;
    } else {
      if (!cxstate.from || cxstate.from < CX_MIN) cxstate.from = CX_MIN;
      if (!cxstate.to || cxstate.to > CX_MAX) cxstate.to = CX_MAX;
    }
    $("cx-presets").innerHTML = CX_PRESETS.map(function (p, i) {
      return '<button class="chip" type="button" data-i="' + i + '" aria-pressed="' + (i === cxPreset) + '">' + p[0] + "</button>";
    }).join("");
    ["cx-from", "cx-to"].forEach(function (id) { $(id).min = CX_MIN; $(id).max = CX_MAX; });
    cxSyncInputs();
    $("cx-dmg-wh").innerHTML = '<option value="all">All warehouses</option>' +
      CXWH.map(function (n, i) { return '<option value="' + i + '">' + esc(n) + "</option>"; }).join("");
    $("cx-dmg-wh").value = cxstate.dmgWh;
  }

  /* ================= pendency ================= */
  function dayDiff(a, b) {            // whole days between two YYYY-MM-DD strings
    return Math.round((Date.parse(b + "T00:00:00Z") - Date.parse(a + "T00:00:00Z")) / 86400000);
  }
  function ageBucket(d) { return d <= 1 ? "d0_1" : d <= 7 ? "d2_7" : "d8"; }
  function lateClass(d) { return d >= 8 ? "l4" : d >= 3 ? "l3" : d >= 1 ? "l2" : ""; }

  /* packing rows: [wh, dn, status, created, ideal|null, touched] */
  function packingRows() {
    var st = pstate.packing;
    return P.packing.map(function (r) {
      var late = r[4] === null ? null : dayDiff(r[4], TODAY);
      return { wh: r[0], dn: r[1], status: P.status[r[2]], created: r[3], ideal: r[4],
               late: late, touched: r[5] };
    }).filter(function (r) {
      if (st.wh !== "all" && r.wh !== +st.wh) return false;
      if (st.age === "all") return true;
      if (st.age === "noideal") return r.late === null;
      if (r.late === null) return false;
      if (st.age === "due") return r.late <= 0;
      if (st.age === "d1_2") return r.late >= 1 && r.late <= 2;
      if (st.age === "d3_7") return r.late >= 3 && r.late <= 7;
      return r.late >= 8;
    });
  }

  function simpleRows(kind) {
    var st = pstate[kind], src = kind === "putback" ? P.putback : P.rto;
    return src.map(function (r) {
      var o = { wh: r[0], dn: r[1], created: r[2], touched: r[3], age: dayDiff(r[2], TODAY) };
      if (kind === "rto") { o.awb = r[4]; o.courier = P.courier[r[5]] || ""; o.against = r[6]; }
      return o;
    }).filter(function (r) {
      if (st.wh !== "all" && r.wh !== +st.wh) return false;
      return st.age === "all" || ageBucket(r.age) === st.age;
    });
  }

  function sortBy(rows, st) {
    return rows.sort(function (a, b) {
      var va = a[st.k], vb = b[st.k];
      if (va === null) return 1;            // undated rows always sink
      if (vb === null) return -1;
      return va < vb ? -st.dir : va > vb ? st.dir : 0;
    });
  }

  function pcell(v, cls) {
    if (!v) return '<td class="num"><span class="dim">-</span></td>';
    return '<td class="num">' + (cls ? '<span class="pill ' + cls + '">' + fmt(v) + "</span>" : fmt(v)) + "</td>";
  }

  function renderPacking() {
    var per = {};
    PWH.forEach(function (_, i) { per[i] = { i: i, total: 0, due: 0, d1_2: 0, d3_7: 0, d8: 0, noideal: 0 }; });
    P.packing.forEach(function (r) {
      var p = per[r[0]]; p.total++;
      if (r[4] === null) { p.noideal++; return; }
      var d = dayDiff(r[4], TODAY);
      if (d <= 0) p.due++; else if (d <= 2) p.d1_2++; else if (d <= 7) p.d3_7++; else p.d8++;
    });
    var data = sortBy(PWH.map(function (_, i) { return per[i]; }), pstate.packing.whSort);
    var T = { total: 0, due: 0, d1_2: 0, d3_7: 0, d8: 0, noideal: 0 };
    data.forEach(function (d) { for (var k in T) T[k] += d[k]; });

    $("pk-wh").querySelector("tbody").innerHTML = data.map(function (d) {
      return "<tr><td>" + dot(COLOR[d.i]) + esc(PWH[d.i]) + '</td><td class="num">' + fmt(d.total) + "</td>" +
        pcell(d.due) + pcell(d.d1_2, "l2") + pcell(d.d3_7, "l3") + pcell(d.d8, "l4") + pcell(d.noideal) + "</tr>";
    }).join("") + '<tr class="total"><td>All warehouses</td><td class="num">' + fmt(T.total) +
      '</td><td class="num">' + fmt(T.due) + '</td><td class="num">' + fmt(T.d1_2) + '</td><td class="num">' +
      fmt(T.d3_7) + '</td><td class="num">' + fmt(T.d8) + '</td><td class="num">' + fmt(T.noideal) + "</td></tr>";

    $("pk-note").textContent =
      "Counts distinct delivery notes still To Be Picked or Under Picking, excluding the IWT " +
      "channel, against Ideal Dispatch Date = order date + that warehouse's cutoff. " +
      fmt(T.d8) + " of " + fmt(T.total) + " are more than a week past their dispatch date: reconcile " +
      "those separately and work the due-today and 1-7 day columns." +
      (T.noideal ? " " + fmt(T.noideal) + " have no order date and cannot be aged." : "");

    var rows = sortBy(packingRows(), pstate.packing.sort), shown = rows.slice(0, 300);
    $("pk-tbl").querySelector("tbody").innerHTML = shown.map(function (r) {
      return '<tr><td class="mono">' + esc(r.dn) + "</td>" +
        "<td>" + dot(COLOR[r.wh]) + esc(PWH[r.wh]) + "</td><td>" + esc(r.status) + "</td>" +
        '<td class="mono num">' + r.created + "</td>" +
        '<td class="mono num">' + (r.ideal || '<span class="dim">none</span>') + "</td>" +
        '<td class="num">' + (r.late === null ? '<span class="dim">-</span>'
          : '<span class="pill ' + lateClass(r.late) + '">' + (r.late <= 0 ? "due today" : r.late) + "</span>") +
        '</td><td class="mono num">' + r.touched + "</td></tr>";
    }).join("") || '<tr><td colspan="7" class="dim">Nothing pending here.</td></tr>';
    $("pk-tnote").textContent = fmt(rows.length) + " open notes" +
      (rows.length > 300 ? "; showing the first 300. The CSV has all of them." : ".");
  }

  function renderSimple(kind) {
    var cfg = kind === "putback"
      ? { whT: "pb-wh", tblT: "pb-tbl", note: "pb-note", tnote: "pb-tnote", src: P.putback, noun: "putback notes", cols: 5 }
      : { whT: "rt-wh", tblT: "rt-tbl", note: "rt-note", tnote: "rt-tnote", src: P.rto, noun: "open RTO returns", cols: 7 };
    var per = {};
    PWH.forEach(function (_, i) { per[i] = { i: i, total: 0, d0_1: 0, d2_7: 0, d8: 0, oldest: 0 }; });
    cfg.src.forEach(function (r) {
      var p = per[r[0]], d = dayDiff(r[2], TODAY);
      p.total++; p[ageBucket(d)]++; if (d > p.oldest) p.oldest = d;
    });
    var data = sortBy(PWH.map(function (_, i) { return per[i]; }), pstate[kind].whSort);
    var T = { total: 0, d0_1: 0, d2_7: 0, d8: 0, oldest: 0 };
    data.forEach(function (d) {
      T.total += d.total; T.d0_1 += d.d0_1; T.d2_7 += d.d2_7; T.d8 += d.d8;
      if (d.oldest > T.oldest) T.oldest = d.oldest;
    });

    $(cfg.whT).querySelector("tbody").innerHTML = data.map(function (d) {
      return "<tr><td>" + dot(COLOR[d.i]) + esc(PWH[d.i]) + '</td><td class="num">' + fmt(d.total) + "</td>" +
        pcell(d.d0_1) + pcell(d.d2_7, "l2") + pcell(d.d8, "l4") +
        '<td class="num">' + (d.total ? d.oldest + "d" : "-") + "</td></tr>";
    }).join("") + '<tr class="total"><td>All warehouses</td><td class="num">' + fmt(T.total) +
      '</td><td class="num">' + fmt(T.d0_1) + '</td><td class="num">' + fmt(T.d2_7) + '</td><td class="num">' +
      fmt(T.d8) + '</td><td class="num">' + T.oldest + "d</td></tr>";

    $(cfg.note).textContent = fmt(T.d8) + " of " + fmt(T.total) +
      " have been open more than a week; treat those as a reconciliation queue, not a chase list." +
      (kind === "rto" ? " RVP returns sit in the same status and are excluded here (" +
       fmt(P.rvpOpen) + " open)." : "");

    var rows = sortBy(simpleRows(kind), pstate[kind].sort), shown = rows.slice(0, 300);
    $(cfg.tblT).querySelector("tbody").innerHTML = shown.map(function (r) {
      var head = '<tr><td class="mono">' + esc(r.dn) + "</td><td>" + dot(COLOR[r.wh]) + esc(PWH[r.wh]) + "</td>";
      var age = '<td class="num"><span class="pill ' + lateClass(r.age) + '">' + r.age + "</span></td>";
      if (kind === "putback")
        return head + '<td class="mono num">' + r.created + "</td>" + age +
               '<td class="mono num">' + r.touched + "</td></tr>";
      return head + '<td class="mono">' + esc(r.awb || "-") + "</td><td>" + esc(r.courier || "-") + "</td>" +
        '<td class="mono num">' + r.created + "</td>" + age +
        '<td class="mono">' + (r.against ? esc(r.against) : "-") + "</td></tr>";
    }).join("") || '<tr><td colspan="' + cfg.cols + '" class="dim">Nothing pending here.</td></tr>';
    $(cfg.tnote).textContent = fmt(rows.length) + " " + cfg.noun +
      (rows.length > 300 ? "; showing the first 300. The CSV has all of them." : ".");
  }

  function pendCsv(kind) {
    if (kind === "packing") return toCsv(
      ["delivery_note", "warehouse", "status", "created", "ideal_dispatch", "days_late", "last_update"],
      sortBy(packingRows(), pstate.packing.sort).map(function (r) {
        return [r.dn, PWH[r.wh], r.status, r.created, r.ideal || "",
                r.late === null ? "" : r.late, r.touched]; }));
    if (kind === "putback") return toCsv(
      ["delivery_note", "warehouse", "created", "days_open", "last_update"],
      sortBy(simpleRows("putback"), pstate.putback.sort).map(function (r) {
        return [r.dn, PWH[r.wh], r.created, r.age, r.touched]; }));
    return toCsv(
      /* two delivery notes matter here and they are not interchangeable: the original
         outbound order, and the return document raised against it. Original first, since
         that is the one every other export is keyed on. */
      ["original_dn", "return_dn", "warehouse", "awb", "courier", "initiated", "days_open"],
      sortBy(simpleRows("rto"), pstate.rto.sort).map(function (r) {
        return [r.against || "", r.dn, PWH[r.wh], r.awb, r.courier, r.created, r.age]; }));
  }

  var PEND_SELECTS = [["packing", "pk-wh-sel", "pk-age"], ["putback", "pb-wh-sel", "pb-age"], ["rto", "rt-wh-sel", "rt-age"]];
  var AGES = {
    packing: [["all", "All"], ["due", "Due today"], ["d1_2", "1-2 days late"],
              ["d3_7", "3-7 days late"], ["d8", "8+ days late"], ["noideal", "No dispatch date"]],
    putback: [["all", "All"], ["d0_1", "0-1 days"], ["d2_7", "2-7 days"], ["d8", "8+ days"]],
    rto:     [["all", "All"], ["d0_1", "0-1 days"], ["d2_7", "2-7 days"], ["d8", "8+ days"]]
  };
  function buildPendControls() {
    var whOpts = '<option value="all">All warehouses</option>' + PWH.map(function (n, i) {
      return '<option value="' + i + '">' + esc(n) + "</option>"; }).join("");
    PEND_SELECTS.forEach(function (t) {
      var kind = t[0];
      $(t[1]).innerHTML = whOpts;
      $(t[1]).value = pstate[kind].wh;
      $(t[2]).innerHTML = AGES[kind].map(function (o) {
        return '<option value="' + o[0] + '">' + o[1] + "</option>"; }).join("");
      $(t[2]).value = pstate[kind].age;
    });
  }

  // Each view's drawing (upstream's drawPend drew all three pendency views at once).
  var RENDER = {
    notfound: function () { draw(); },
    packing: function () {
      renderPacking();
      markSort("pk-wh", pstate.packing.whSort); markSort("pk-tbl", pstate.packing.sort);
    },
    putback: function () {
      renderSimple("putback");
      markSort("pb-wh", pstate.putback.whSort); markSort("pb-tbl", pstate.putback.sort);
    },
    rto: function () {
      renderSimple("rto");
      markSort("rt-wh", pstate.rto.whSort); markSort("rt-tbl", pstate.rto.sort);
    },
    complaints: function () { renderComplaints(); }
  };
  // Views whose tables are behind the data. Only the view on screen is drawn when data comes in (about half the
  // work: the five views hold ~1,400 rows); each other one is drawn the first time it is opened.
  var stale = {};

  /* ================= views ================= */
  function views() {
    return [
      { id: "notfound", label: "Not found", count: null,
        blurb: "Stock the packer could not physically find during bin determination, moved out of the " +
               "warehouse bin into the suspense bin against a delivery note." },
      { id: "packing", label: "Packing pendency", count: P.packing.length,
        blurb: "Delivery notes still To Be Picked or Under Picking, counted against their ideal " +
               "dispatch date. Same definition as the team's Ideal Dispatch Date Pendency sheet." },
      { id: "putback", label: "Putback pendency", count: P.putback.length,
        blurb: "Stock pulled for an order that did not ship, still waiting to be put back into its bin." },
      { id: "rto", label: "RTO initiated pendency", count: P.rto.length,
        blurb: "RTO returns raised against the warehouse and not yet closed back into stock." },
      { id: "complaints", label: "Complaints", count: CX.rows.length,
        blurb: "Customer complaints about what arrived - wrong item, missing item, damaged or " +
               "expired - attributed back to the warehouse that shipped it." }
    ];
  }

  function buildTabs() {
    $("tabs").innerHTML = views().map(function (t) {
      return '<button type="button" data-view="' + t.id + '" aria-pressed="false">' + esc(t.label) +
        (t.count === null ? "" : '<span class="wx-count">' + fmt(t.count) + "</span>") + "</button>";
    }).join("");
  }

  function showTab(id) {
    current = id;
    if (stale[id]) { delete stale[id]; RENDER[id](); }
    views().forEach(function (t) {
      $("view-" + t.id).hidden = t.id !== id;
      $("tabs").querySelector('[data-view="' + t.id + '"]').setAttribute("aria-pressed", String(t.id === id));
    });
    $("blurb").textContent = views().filter(function (x) { return x.id === id; })[0].blurb;
    // pendency is a live snapshot, so the date range only belongs to the not-found view
    $("daterange").hidden = id !== "notfound";
    $("asof").textContent = id === "notfound"
      ? "ERP read replica · " + D.window.start + " to " + DATES[DATES.length - 1] + " · generated " + D.generated
      : id === "complaints" ? "Generated " + D.generated
      : "Pendency as of " + P.asOf + " IST";
  }

  /* ================= wiring: once, whatever data comes later ================= */
  function sortable(id, get, redraw) {
    $(id).querySelector("thead").addEventListener("click", function (e) {
      var th = e.target.closest("th");
      if (!th || th.classList.contains("no-sort") || !th.dataset.k) return;
      var st = get();
      if (st.k === th.dataset.k) st.dir = -st.dir; else { st.k = th.dataset.k; st.dir = -1; }
      redraw();
    });
  }

  function wire() {
    $("tabs").addEventListener("click", function (e) {
      var b = e.target.closest("button[data-view]"); if (b) showTab(b.dataset.view);
    });

    // not found
    $("presets").addEventListener("click", function (e) {
      var b = e.target.closest(".chip"); if (!b) return;
      nfPreset = +b.dataset.i;
      state.from = nfPresets()[nfPreset][1]; state.to = DATES[DATES.length - 1];
      $("from").value = state.from; $("to").value = state.to;
      [].forEach.call(this.children, function (c) { c.setAttribute("aria-pressed", c === b); });
      draw();
    });
    var clearPresets = function () {
      nfPreset = -1;
      [].forEach.call($("presets").children, function (c) { c.setAttribute("aria-pressed", "false"); });
    };
    $("from").addEventListener("change", function () { state.from = this.value || DATES[0]; clearPresets(); draw(); });
    $("to").addEventListener("change", function () { state.to = this.value || DATES[DATES.length - 1]; clearPresets(); draw(); });
    $("binwh").addEventListener("change", function () { state.binWh = this.value; draw(); });
    $("skuwh").addEventListener("change", function () { state.skuWh = this.value; draw(); });
    sortable("whtable", function () { return state.whSort; }, draw);
    sortable("bintable", function () { return state.binSort; }, draw);
    sortable("skutable", function () { return state.skuSort; }, draw);

    // pendency
    PEND_SELECTS.forEach(function (t) {
      var kind = t[0];
      $(t[1]).addEventListener("change", function () { pstate[kind].wh = this.value; RENDER[kind](); });
      $(t[2]).addEventListener("change", function () { pstate[kind].age = this.value; RENDER[kind](); });
    });
    [["pk-wh", "packing", "whSort"], ["pk-tbl", "packing", "sort"], ["pb-wh", "putback", "whSort"],
     ["pb-tbl", "putback", "sort"], ["rt-wh", "rto", "whSort"], ["rt-tbl", "rto", "sort"]].forEach(function (t) {
      sortable(t[0], function () { return pstate[t[1]][t[2]]; }, RENDER[t[1]]);
    });

    // complaints
    $("cx-presets").addEventListener("click", function (e) {
      var b = e.target.closest(".chip"); if (!b) return;
      cxPreset = +b.dataset.i;
      var days = CX_PRESETS[cxPreset][1];
      cxstate.to = CX_MAX;
      cxstate.from = days ? cxShiftDays(CX_MAX, -(days - 1)) : CX_MIN;
      [].forEach.call(this.children, function (c) { c.setAttribute("aria-pressed", c === b); });
      cxSyncInputs(); renderComplaints();
    });
    ["cx-from", "cx-to"].forEach(function (id) {
      $(id).addEventListener("change", function () {
        cxstate[id === "cx-from" ? "from" : "to"] = this.value || (id === "cx-from" ? CX_MIN : CX_MAX);
        if (cxstate.from > cxstate.to) {
          var t = cxstate.from; cxstate.from = cxstate.to; cxstate.to = t;
        }
        cxPreset = -1;
        [].forEach.call($("cx-presets").children, function (c) { c.setAttribute("aria-pressed", "false"); });
        cxSyncInputs(); renderComplaints();
      });
    });
    $("cx-dmg-wh").addEventListener("change", function () { cxstate.dmgWh = this.value; renderDamagedByCourier(); });
    sortable("cx-wh", function () { return cxstate.whSort; }, renderComplaints);
    sortable("cx-dmg", function () { return cxstate.dmgSort; }, renderComplaints);

    document.getElementById("panel-warehouse-exceptions").addEventListener("click", function (e) {
      var btn = e.target.closest("[data-wx-csv]"); if (btn) saveCsv(btn);
    });
  }

  /* ================= new data ================= */
  function init(data) {
    D = data.notfound; P = data.pendency; CX = data.complaints;
    WH = D.dims.wh; ACTIVE = D.dims.active; DATES = D.dims.date;
    BINS = D.dims.bin; ITEMS = D.dims.item; INAMES = D.dims.itemName; STATUS = D.dims.status;
    facts = D.facts; base = D.base; CANCEL = STATUS.indexOf("Cancel");
    COLOR = {};
    for (var i = 0, ci = 0; i < WH.length; i++) COLOR[i] = ACTIVE[i] ? "var(" + SER[ci++ % SER.length] + ")" : "var(--wx-dead)";
    state.wh = WH.map(function (_, i) { return i; });  // always all; tables filter themselves

    CXWH = CX.wh; BUCKETS = CX.buckets; CXCOURIER = CX.courier;
    // bounds must span BOTH series: tickets are raised after the shipment they refer to,
    // so complaint dates run past the last shipment date and would otherwise be cut off
    var all = CX.date.concat(CX.shipDaily.map(function (r) { return r[1]; })).sort();
    CX_MIN = all[0]; CX_MAX = all[all.length - 1];

    PWH = P.wh; TODAY = P.today;

    $("foot").innerHTML = "<strong>Not found</strong>: a <code>Material Transfer</code> stock entry whose detail line " +
      "carries <code>custom_missing = 1</code> and lands in the warehouse's <code>Suspense-MWH_*</code> " +
      "bin, joined through <code>tabStock Entry</code>, <code>tabStock Entry Detail</code>, " +
      "<code>tabWarehouse</code> and <code>tabDelivery Note</code>. Inventory-adjustment write-offs are a " +
      "separate process and are excluded. Darkstores are out of scope: they log missing stock without a " +
      "delivery note, so orders hit and cancellations cannot be derived there.";

    var banner = document.querySelector("#panel-warehouse-exceptions .bx-banner");
    try {
      if (!wired) { wire(); wired = true; }
      buildControls(); buildTabs(); buildPendControls(); buildComplaintsControls();
      views().forEach(function (t) { stale[t.id] = true; });
      showTab(current);  // draws just this one; the rest when opened
    } catch (err) {
      // upstream's fail(): say so on the page rather than leave half-drawn tables looking complete
      banner.textContent = "This report failed to render: " + (err && err.message ? err.message : err);
      banner.className = "banner error bx-banner";
      console.error(err); // not rethrown: BX.tab would then call it a connection problem
    }
  }

  BX.tab({ panel: "warehouse-exceptions", dataset: "warehouse", init: init });
})();
