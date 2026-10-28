(function () {
  "use strict";

  // Breach Explorer > RTO: of the shipments that reached an outcome, how many came back, and what they were
  // worth. The SLA Breach Explorer's RTO tab (see be_common.js), filtering breach_pipeline's RTO export in the
  // browser. Its "Returns received at warehouse" table is a second dataset on a different date (rto_delivery_date).

  const BX = window.BX;
  const el = BX.el, fmt = BX.fmt, uniq = BX.uniq, esc = BX.esc;

  // 0 date, 1 courier, 2 slab, 3 dest, 4 rto_status, 5 payment, 6 attempts, 7 mode, 8 shipments, 9 rto,
  // 10 rto_delivered, 11 lag_sum, 12 lag_n, 13-16 values (shipped, returned, and both net of tax)
  const C = { date:0, courier:1, slab:2, dest:3, status:4, payment:5, attempts:6, mode:7,
              ship:8, rto:9, rtoDel:10, lagSum:11, lagN:12,
              shipVal:13, rtoVal:14, netVal:15, netRtoVal:16 };
  const RC = { date:0, wh:1, courier:2, n:3, gross:4, net:5 };
  const p2 = (a,b) => b > 0 ? (a/b*100).toFixed(2) + '%' : '·';

  // Indian money shorthand: 1,23,45,678 -> "1.23 Cr".
  const RS = '₹';
  function money(v) {
    if (v === null || v === undefined || !isFinite(v)) return '·';
    const a = Math.abs(v);
    if (a >= 1e7) return RS + (v / 1e7).toFixed(2) + ' Cr';
    if (a >= 1e5) return RS + (v / 1e5).toFixed(2) + ' L';
    if (a >= 1e3) return RS + Math.round(v).toLocaleString('en-IN');
    return RS + Math.round(v);
  }

  const MS = {};
  let RAW = {}, D = {}, F = [], DMIN = '', DMAX = '', RT = null;

  /* ---- returns received back at the warehouse -------------------------
     A SEPARATE dataset on a SEPARATE date basis. Everything above counts an
     RTO on its mark date - the day the return was decided. This counts it on
     rto_delivery_date, the day the parcel physically arrived back. The median
     gap is about four days and only ~21% arrive within a day, so a warehouse
     reconciling what it has actually received cannot read it off the views
     above. Cumulative answers "how much landed on D-1 or earlier". */
  function renderReturns() {
    if (!RT) { el('t-rto-returns').innerHTML = '<tbody><tr><td class="empty">No returns received in this window.</td></tr></tbody>'; return; }
    const from = el('rto-from').value, to = el('rto-to').value;
    const whSel = (MS.rtrwh && MS.rtrwh.sel.size) ? MS.rtrwh.sel : null;
    const coSel = (MS.rtrcour && MS.rtrcour.sel.size) ? MS.rtrcour.sel : null;
    const by = new Map();
    RT.facts.forEach(function (r) {
      const d = RT.dims.date[r[RC.date]];
      if ((from && d < from) || (to && d > to)) return;
      if (whSel && !whSel.has(RT.dims.wh[r[RC.wh]])) return;
      if (coSel && !coSel.has(RT.dims.courier[r[RC.courier]])) return;
      const a = by.get(d) || [0, 0, 0];
      a[0] += r[RC.n]; a[1] += r[RC.gross]; a[2] += r[RC.net];
      by.set(d, a);
    });
    const days = Array.from(by.keys()).sort();
    let tn = 0, tg = 0, tnet = 0, cum = 0;
    let h = '<thead><tr><th>Received on</th><th>Returns</th>'
          + '<th>Cumulative</th><th>Value (incl. tax)</th><th>Net value</th></tr></thead><tbody>';
    days.forEach(function (d) {
      const a = by.get(d);
      cum += a[0]; tn += a[0]; tg += a[1]; tnet += a[2];
      h += '<tr><td>' + d + '</td><td>' + fmt(a[0]) + '</td>'
         + '<td>' + fmt(cum) + '</td><td>' + money(a[1]) + '</td>'
         + '<td>' + money(a[2]) + '</td></tr>';
    });
    h += '</tbody><tfoot><tr><td>Total</td><td>' + fmt(tn)
       + '</td><td></td><td>' + money(tg) + '</td><td>' + money(tnet) + '</td></tr></tfoot>';
    el('t-rto-returns').innerHTML = h;
  }

  function rows() {
    const from = el('rto-from').value, to = el('rto-to').value;
    const c = MS.rtocour.sel, s = MS.rtoslab.sel, de = MS.rtodest.sel,
          st = MS.rtost.sel, pay = MS.rtopay.sel, md = MS.rtomode.sel;
    const out = [];
    for (let i = 0; i < F.length; i++) {
      const r = F[i], d = D.date[r[C.date]];
      if (d < from || d > to) continue;
      if (c.size  && !c.has(D.courier[r[C.courier]])) continue;
      if (s.size  && !s.has(D.slab[r[C.slab]])) continue;
      if (pay.size && !pay.has(D.payment[r[C.payment]])) continue;
      if (md.size && !md.has(D.mode[r[C.mode]])) continue;
      if (de.size && !de.has(D.dest[r[C.dest]])) continue;
      // an RTO-status filter necessarily restricts to RTO rows
      if (st.size && !st.has(D.rto_status[r[C.status]])) continue;
      out.push(r);
    }
    return out;
  }

  const sortState = {};
  function rateTable(id, map, label, limit) {
    let arr = Array.from(map.entries()).map(function (e) {
      const v = e[1];
      return { k: e[0], ship: v.ship, rto: v.rto, del: v.del,
               rate: v.ship > 0 ? v.rto / v.ship * 100 : null,
               lag: v.lagN > 0 ? v.lagSum / v.lagN : null };
    });
    const totRto = arr.reduce(function (s, o) { return s + o.rto; }, 0);
    arr.forEach(function (o) { o.share = totRto > 0 ? o.rto / totRto * 100 : null; });

    const ss = sortState[id] || { col: 2, dir: -1 };
    const val = function (o, c) {
      return c === -1 ? o.k : (c === 0 ? o.ship : (c === 1 ? o.rto
             : (c === 2 ? (o.rate === null ? -1 : o.rate)
             : (c === 3 ? (o.share === null ? -1 : o.share)
             : (o.lag === null ? -1 : o.lag)))));
    };
    arr.sort(function (x, y) {
      const a = val(x, ss.col), b = val(y, ss.col);
      return typeof a === 'string' ? ss.dir * a.localeCompare(b) : ss.dir * (a - b);
    });
    const shown = limit ? arr.slice(0, limit) : arr;

    const tShip = arr.reduce(function (s,o){ return s+o.ship; }, 0);

    let h = '<thead><tr>'
          + '<th class="sortable" data-c="-1">' + esc(label) + '</th>'
          + '<th class="sortable" data-c="0">Terminal</th>'
          + '<th class="sortable" data-c="1">RTO</th>'
          + '<th class="sortable share" data-c="2">RTO %</th>'
          + '<th class="sortable" data-c="3">Share of RTO</th>'
          + '<th class="sortable" data-c="4">Avg days to mark</th>'
          + '</tr></thead><tbody>';
    shown.forEach(function (o) {
      h += '<tr><td>' + esc(o.k) + '</td>'
         + '<td>' + fmt(o.ship) + '</td>'
         + '<td>' + (o.rto ? fmt(o.rto) : '·') + '</td>'
         + '<td class="share">' + (o.rate === null ? '·' : o.rate.toFixed(2) + '%') + '</td>'
         + '<td class="pctcell">' + (o.share === null || !o.rto ? '·' : o.share.toFixed(1) + '%') + '</td>'
         + '<td class="pctcell">' + (o.lag === null ? '·' : o.lag.toFixed(1)) + '</td></tr>';
    });
    h += '</tbody><tfoot><tr><td>Total'
       + (limit && arr.length > limit ? ' (' + arr.length + ' rows, top ' + limit + ')' : '')
       + '</td><td>' + fmt(tShip) + '</td><td>' + fmt(totRto) + '</td>'
       + '<td>' + p2(totRto, tShip) + '</td><td></td><td></td></tr></tfoot>';

    const t = el(id);
    t.innerHTML = h;
    t.querySelectorAll('th.sortable').forEach(function (th) {
      th.addEventListener('click', function () {
        const c = parseInt(th.dataset.c, 10);
        const cur = sortState[id] || { col: 2, dir: -1 };
        sortState[id] = { col: c, dir: (cur.col === c ? -cur.dir : (c === -1 ? 1 : -1)) };
        renderRto();
      });
    });
  }

  // Value view: same grouping as rateTable, but measured in rupees rather than
  // shipment counts. Kept separate so neither table grows unreadably wide.
  function valueTable(id, map, label, limit) {
    const arr = Array.from(map.entries()).map(function (e) {
      const v = e[1];
      return { k: e[0], sv: v.shipVal, rv: v.rtoVal, rto: v.rto,
               nsv: v.netVal, nrv: v.netRtoVal,
               rate: v.shipVal > 0 ? v.rtoVal / v.shipVal * 100 : null,
               nrate: v.netVal > 0 ? v.netRtoVal / v.netVal * 100 : null,
               avg: v.rto > 0 ? v.rtoVal / v.rto : null };
    });
    const totRv = arr.reduce(function (s, o) { return s + o.rv; }, 0);
    const totSv = arr.reduce(function (s, o) { return s + o.sv; }, 0);
    const totNrv = arr.reduce(function (s, o) { return s + o.nrv; }, 0);
    const totNsv = arr.reduce(function (s, o) { return s + o.nsv; }, 0);
    arr.forEach(function (o) { o.share = totRv > 0 ? o.rv / totRv * 100 : null; });

    const ss = sortState[id] || { col: 1, dir: -1 };
    const val = function (o, c) {
      return c === -1 ? o.k : (c === 0 ? o.sv : (c === 1 ? o.rv
             : (c === 2 ? (o.rate === null ? -1 : o.rate)
             : (c === 3 ? (o.share === null ? -1 : o.share)
             : (c === 5 ? o.nsv : (c === 6 ? o.nrv
             : (c === 7 ? (o.nrate === null ? -1 : o.nrate)
             : (o.avg === null ? -1 : o.avg))))))));
    };
    arr.sort(function (x, y) {
      const a = val(x, ss.col), b = val(y, ss.col);
      return typeof a === 'string' ? ss.dir * a.localeCompare(b) : ss.dir * (a - b);
    });
    const shown = limit ? arr.slice(0, limit) : arr;

    let h = '<thead><tr>'
          + '<th class="sortable" data-c="-1">' + esc(label) + '</th>'
          + '<th class="sortable" data-c="0">Terminal value</th>'
          + '<th class="sortable" data-c="1">Value returned</th>'
          + '<th class="sortable share" data-c="2">RTO value %</th>'
          + '<th class="sortable" data-c="5">Terminal net</th>'
          + '<th class="sortable" data-c="6">Net returned</th>'
          + '<th class="sortable share" data-c="7">Net RTO %</th>'
          + '<th class="sortable" data-c="3">Share of RTO value</th>'
          + '<th class="sortable" data-c="4">Avg value per RTO</th>'
          + '</tr></thead><tbody>';
    shown.forEach(function (o) {
      h += '<tr><td>' + esc(o.k) + '</td>'
         + '<td>' + money(o.sv) + '</td>'
         + '<td>' + (o.rv ? money(o.rv) : '·') + '</td>'
         + '<td class="share">' + (o.rate === null ? '·' : o.rate.toFixed(2) + '%') + '</td>'
         + '<td>' + money(o.nsv) + '</td>'
         + '<td>' + (o.nrv ? money(o.nrv) : '·') + '</td>'
         + '<td class="share">' + (o.nrate === null ? '·' : o.nrate.toFixed(2) + '%') + '</td>'
         + '<td class="pctcell">' + (o.share === null || !o.rv ? '·' : o.share.toFixed(1) + '%') + '</td>'
         + '<td class="pctcell">' + money(o.avg) + '</td></tr>';
    });
    h += '</tbody><tfoot><tr><td>Total'
       + (limit && arr.length > limit ? ' (' + arr.length + ' rows, top ' + limit + ')' : '')
       + '</td><td>' + money(totSv) + '</td><td>' + money(totRv) + '</td>'
       + '<td>' + (totSv > 0 ? (totRv / totSv * 100).toFixed(2) + '%' : '·') + '</td>'
       + '<td>' + money(totNsv) + '</td><td>' + money(totNrv) + '</td>'
       + '<td>' + (totNsv > 0 ? (totNrv / totNsv * 100).toFixed(2) + '%' : '·') + '</td>'
       + '<td></td><td></td></tr></tfoot>';

    const t = el(id);
    t.innerHTML = h;
    t.querySelectorAll('th.sortable').forEach(function (th) {
      th.addEventListener('click', function () {
        const c = parseInt(th.dataset.c, 10);
        const cur = sortState[id] || { col: 1, dir: -1 };
        sortState[id] = { col: c, dir: (cur.col === c ? -cur.dir : (c === -1 ? 1 : -1)) };
        renderRto();
      });
    });
  }

  function group(rs, keyFn) {
    const m = new Map();
    rs.forEach(function (r) {
      const k = keyFn(r);
      if (!m.has(k)) m.set(k, { ship:0, rto:0, del:0, lagSum:0, lagN:0,
                                shipVal:0, rtoVal:0, netVal:0, netRtoVal:0 });
      const o = m.get(k);
      o.ship += r[C.ship]; o.rto += r[C.rto]; o.del += r[C.rtoDel];
      o.lagSum += r[C.lagSum]; o.lagN += r[C.lagN];
      o.shipVal += r[C.shipVal] || 0; o.rtoVal += r[C.rtoVal] || 0;
      o.netVal += r[C.netVal] || 0; o.netRtoVal += r[C.netRtoVal] || 0;
    });
    return m;
  }

  function renderRto() {
    if (!F.length) return;
    const rs = rows();
    let ship = 0, rto = 0, del = 0, lagS = 0, lagN = 0, shipVal = 0, rtoVal = 0;
    rs.forEach(function (r) {
      ship += r[C.ship]; rto += r[C.rto]; del += r[C.rtoDel];
      lagS += r[C.lagSum]; lagN += r[C.lagN];
      shipVal += r[C.shipVal] || 0; rtoVal += r[C.rtoVal] || 0;
    });

    el('rto-cards').innerHTML =
      '<div class="pcard"><div class="k">Terminal</div><div class="v">' + fmt(ship) +
        '</div><div class="s">RTO marked + delivered</div></div>' +
      '<div class="pcard crit"><div class="k">RTO</div><div class="v">' + fmt(rto) +
        '</div><div class="s">marked in range</div></div>' +
      '<div class="pcard crit"><div class="k">RTO rate</div><div class="v">' + p2(rto, ship) +
        '</div><div class="s">RTO / (RTO + delivered)</div></div>' +
      '<div class="pcard"><div class="k">Back at origin</div><div class="v">' + fmt(del) +
        '</div><div class="s">' + p2(del, rto) + ' of RTO completed</div></div>' +
      '<div class="pcard crit"><div class="k">RTO value</div><div class="v">' + money(rtoVal) +
        '</div><div class="s">' + (shipVal > 0 ? (rtoVal / shipVal * 100).toFixed(2) + '%' : '·') +
        ' of ' + money(shipVal) + ' shipped</div></div>' +
      '<div class="pcard"><div class="k">Avg days to mark</div><div class="v">' +
        (lagN ? (lagS / lagN).toFixed(1) : '·') +
        '</div><div class="s">creation to RTO flag</div></div>';

    const attSel = (MS.rtoatt && MS.rtoatt.sel.size) ? MS.rtoatt.sel : null;
    const cRows = attSel
      ? rs.filter(function (r) { return attSel.has(D.attempts[r[C.attempts]]); })
      : rs;
    rateTable('t-rto-courier', group(cRows, r => D.courier[r[C.courier]]), 'Courier');
    valueTable('t-rto-value', group(cRows, r => D.courier[r[C.courier]]), 'Courier');
    rateTable('t-rto-dest', group(rs, r => D.dest[r[C.dest]]), 'Destination', 50);
    rateTable('t-rto-day', group(rs, r => D.date[r[C.date]]), 'Outcome date');

    // status mix: RTO rows only
    const m = new Map();
    rs.forEach(function (r) {
      const st = D.rto_status[r[C.status]];
      if (!st) return;
      m.set(st, (m.get(st) || 0) + r[C.rto]);
    });
    const arr = Array.from(m.entries()).sort(function (a,b) { return b[1]-a[1]; });
    const tot = arr.reduce(function (s,o){ return s+o[1]; }, 0);
    let h = '<thead><tr><th>RTO status</th><th>Shipments</th><th>Share</th></tr></thead><tbody>';
    arr.forEach(function (o) {
      h += '<tr><td>' + esc(o[0]) + '</td><td>' + fmt(o[1]) + '</td>'
         + '<td class="pctcell">' + p2(o[1], tot) + '</td></tr>';
    });
    h += '</tbody><tfoot><tr><td>Total</td><td>' + fmt(tot)
       + '</td><td></td></tr></tfoot>';
    el('t-rto-status').innerHTML = h;
    renderReturns();
  }

  ['rto-from','rto-to'].forEach(id => el(id).addEventListener('change', renderRto));
  el('btn-rto-reset').addEventListener('click', function () {
    ['rtocour','rtoslab','rtodest','rtost','rtopay','rtomode','rtoatt','rtrwh','rtrcour']
      .forEach(k => MS[k] && MS[k].clear());
    el('rto-from').value = DMIN; el('rto-to').value = DMAX;
    renderRto();
  });

  function wire(btn, table, name) {
    el(btn).addEventListener('click', function () {
      if (!F.length) return;
      BX.saveCsv(name + '_' + el('rto-from').value + '_to_' + el('rto-to').value + '.csv', BX.tableCsv(table));
    });
  }
  wire('btn-rtoc-csv','t-rto-courier','rto_by_courier');
  wire('btn-rtoval-csv','t-rto-value','rto_value_by_courier');
  wire('btn-rtos-csv','t-rto-status','rto_status_mix');
  wire('btn-rtod-csv','t-rto-dest','rto_by_destination');
  wire('btn-rtoday-csv','t-rto-day','rto_day_on_day');
  wire('btn-rtr-csv','t-rto-returns','rto_returns_received');

  function init(raw) {
    const oldMin = DMIN, oldMax = DMAX;
    RAW = raw;
    D = raw.dims || {};
    F = raw.facts || [];
    if (!F.length) return;
    const dates = D.date.slice().sort();
    DMIN = dates[0]; DMAX = dates[dates.length-1];
    BX.keepRange('rto-from', 'rto-to', oldMin, oldMax, DMIN, DMAX);
    el('rto-asof').innerHTML = 'RTO marked or delivered <strong>' + DMIN + '</strong> to <strong>' + DMAX
      + '</strong> · ' + fmt(RAW.shipments || 0) + ' terminal shipments, '
      + fmt(RAW.rto || 0) + ' RTO';

    const slabVals = ['0-3kg','3+kg','unknown'].filter(s => D.slab.indexOf(s) !== -1);
    const statusVals = uniq(D.rto_status.filter(s => s !== ''));
    const onF = () => renderRto();
    const make = (hostId, key, label, values, onChange) => BX.makeMS(MS, hostId, key, label, values, onChange);
    make('ms-rto-courier','rtocour','Courier', uniq(D.courier), onF);
    make('ms-rto-slab','rtoslab','Weight', slabVals, onF);
    make('ms-rto-dest','rtodest','Destination', uniq(D.dest), onF);
    make('ms-rto-status','rtost','RTO status', statusVals, onF);
    make('ms-rto-pay','rtopay','Payment', uniq(D.payment), onF);
    make('ms-rto-mode','rtomode','Air / surface', uniq(D.mode), onF);
    make('ms-rto-att','rtoatt','Attempts', uniq(D.attempts), onF);

    RT = (RAW.returns && RAW.returns.facts && RAW.returns.facts.length) ? RAW.returns : null;
    el('ms-rtr-wh').hidden = el('ms-rtr-cour').hidden = !RT;
    if (RT) {
      make('ms-rtr-wh','rtrwh','Warehouse', uniq(RT.dims.wh), renderReturns);
      make('ms-rtr-cour','rtrcour','Courier', uniq(RT.dims.courier), renderReturns);
    }
    renderRto();
  }

  BX.tab({ panel: 'be-rto', dataset: 'rto', init: init, onShow: renderRto });
})();
