(function () {
  "use strict";

  // Breach Explorer > RAD to OFD: destination-hub arrival to first out-for-delivery. The SLA Breach Explorer's
  // RAD to OFD tab (see be_common.js), filtering breach_pipeline's RAD-to-OFD export in the browser. Export CSV
  // is one row per shipment, from the server (main.breach_explorer_awbs).

  const BX = window.BX;
  const el = BX.el, fmt = BX.fmt, uniq = BX.uniq, esc = BX.esc;

  /* Two fact tables sharing the same filter dimensions and measure layout; only
     the geography column differs, so one index map serves both. */
  const C = { date:0, courier:1, wh:2, account:3, slab:4, geo:5,
              tot:6, same:7, next:8, d2:9, noofd:10 };
  const p1 = (a,b) => b > 0 ? (a/b*100).toFixed(1) + '%' : '·';
  const MS = {};
  let RAW = {}, CITY = {}, HUB = {}, DMIN = '', DMAX = '';

  function rowsOf(tbl) {
    const dd = tbl.dims, F = tbl.facts || [];
    const from = el('ro-from').value, to = el('ro-to').value;
    const cs = MS.rocour && MS.rocour.sel, ws = MS.rowh && MS.rowh.sel,
          as = MS.roacct && MS.roacct.sel, sl = MS.roslab && MS.roslab.sel;
    return F.filter(function (r) {
      const d = dd.date[r[C.date]];
      if ((from && d < from) || (to && d > to)) return false;
      if (cs && cs.size && !cs.has(dd.courier[r[C.courier]])) return false;
      if (ws && ws.size && !ws.has(dd.warehouse[r[C.wh]])) return false;
      if (as && as.size && !as.has(dd.account[r[C.account]])) return false;
      if (sl && sl.size && !sl.has(dd.slab[r[C.slab]])) return false;
      return true;
    });
  }

  function summarise(rs) {
    const a = [0,0,0,0,0];
    rs.forEach(function (r) {
      a[0]+=r[C.tot]; a[1]+=r[C.same]; a[2]+=r[C.next]; a[3]+=r[C.d2]; a[4]+=r[C.noofd];
    });
    return a;
  }

  function group(rs, keyFn) {
    const m = new Map();
    rs.forEach(function (r) {
      const k = keyFn(r);
      if (!m.has(k)) m.set(k, []);
      m.get(k).push(r);
    });
    return m;
  }

  function table(id, groups, label, opts) {
    opts = opts || {};
    let keys = Array.from(groups.keys());
    if (opts.byDate) {
      keys.sort((x, y) => y.localeCompare(x));          // newest arrival first
    } else {
      /* Rank by volume, not alphabetically. A 1,500-row hub table sorted A-Z
         opens on ADAJAN and ADONI - hubs with a dozen shipments - and buries
         the ones worth looking at.
         The catch-all buckets are pinned last however big they are: "Other hub"
         is the largest single row by far, but it is thousands of sites added
         together, not somewhere anyone can go and fix something. Letting it
         head the table would push out 20 real hubs. */
      const CATCHALL = { 'Other hub': 1, 'Other city': 1, 'Unmapped': 1, 'Unknown': 1 };
      keys.sort(function (x, y) {
        const cx = CATCHALL[x] ? 1 : 0, cy = CATCHALL[y] ? 1 : 0;
        if (cx !== cy) return cx - cy;
        return summarise(groups.get(y))[0] - summarise(groups.get(x))[0];
      });
    }
    const shownAll = keys.length;
    if (opts.limit && keys.length > opts.limit) keys = keys.slice(0, opts.limit);
    if (opts.countInto) {
      el(opts.countInto).textContent = keys.length < shownAll
        ? ('top ' + keys.length + ' of ' + fmt(shownAll) + ' by volume')
        : (fmt(shownAll) + ' rows');
    }
    let h = '<thead><tr><th>' + label + '</th>'
          + '<th>Shipments</th><th>Same day</th><th>Same day %</th>'
          + '<th>Next day</th><th>By next day %</th>'
          + '<th>D+2 or later</th><th>No OFD</th></tr></thead><tbody>';
    keys.forEach(function (k) {
      const a = summarise(groups.get(k));
      h += '<tr><td>' + esc(k) + '</td>'
         + '<td>' + fmt(a[0]) + '</td><td>' + fmt(a[1]) + '</td>'
         + '<td class="pctcell">' + p1(a[1], a[0]) + '</td>'
         + '<td>' + fmt(a[2]) + '</td>'
         + '<td class="pctcell">' + p1(a[1] + a[2], a[0]) + '</td>'
         + '<td>' + fmt(a[3]) + '</td><td>' + fmt(a[4]) + '</td></tr>';
    });
    // Total spans every group, including rows the limit hides, so the footer
    // reconciles to the filtered population rather than to what fits on screen.
    const t = summarise(Array.from(groups.values()).flat());
    h += '</tbody><tfoot><tr><td>Total (all rows)</td>'
       + '<td>' + fmt(t[0]) + '</td><td>' + fmt(t[1]) + '</td>'
       + '<td class="pctcell">' + p1(t[1], t[0]) + '</td>'
       + '<td>' + fmt(t[2]) + '</td>'
       + '<td class="pctcell">' + p1(t[1] + t[2], t[0]) + '</td>'
       + '<td>' + fmt(t[3]) + '</td><td>' + fmt(t[4]) + '</td></tr></tfoot>';
    el(id).innerHTML = h;
  }

  function renderRo() {
    if (!(CITY.facts || []).length) return;
    const cr = rowsOf(CITY), hr = rowsOf(HUB);
    table('t-ro-day', group(cr, r => CITY.dims.date[r[C.date]]), 'Arrival date',
          { byDate: true });
    table('t-ro-city', group(cr, r => CITY.dims.city[r[C.geo]]), 'Destination city',
          { limit: 50, countInto: 'ro-city-note' });
    table('t-ro-hub', group(hr, r => HUB.dims.hub[r[C.geo]]), 'Destination hub',
          { limit: 50, countInto: 'ro-hub-note' });
  }

  ['ro-from','ro-to'].forEach(id => el(id).addEventListener('change', renderRo));
  el('btn-ro-reset').addEventListener('click', function () {
    ['rocour','rowh','roacct','roslab'].forEach(k => MS[k] && MS[k].clear());
    el('ro-from').value = DMIN; el('ro-to').value = DMAX;
    renderRo();
  });

  function selections() {
    const out = {};
    [['courier','rocour'], ['warehouse','rowh'], ['account','roacct'],
     ['slab','roslab']].forEach(function (p) {
      const m = MS[p[1]];
      if (m && m.sel && m.sel.size) out[p[0]] = Array.from(m.sel);
    });
    return out;
  }
  ['btn-roday-csv', 'btn-rocity-csv', 'btn-rohub-csv'].forEach(function (id) {
    el(id).addEventListener('click', function () {
      if (!(CITY.facts || []).length) return;
      BX.exportAwbs('radofd', el('ro-from').value, el('ro-to').value, selections(), el(id), el('ro-banner'));
    });
  });

  function init(raw) {
    const oldMin = DMIN, oldMax = DMAX;
    RAW = raw;
    CITY = RAW.by_city || {};
    HUB = RAW.by_hub || {};
    if (!(CITY.facts || []).length) return;
    const D = CITY.dims;
    const dates = D.date.slice().sort();
    DMIN = dates[0]; DMAX = dates[dates.length-1];
    BX.keepRange('ro-from', 'ro-to', oldMin, oldMax, DMIN, DMAX);
    el('ro-asof').innerHTML = 'Reached destination <strong>' + DMIN + '</strong> to <strong>'
      + DMAX + '</strong> · ' + fmt(RAW.shipments || 0)
      + ' shipments · Bluedart and Delhivery, mother warehouses';

    const onF = () => renderRo();
    const make = (hostId, key, label, values, onChange) => BX.makeMS(MS, hostId, key, label, values, onChange);
    make('ms-ro-courier','rocour','Courier', uniq(D.courier), onF);
    make('ms-ro-wh','rowh','Warehouse', uniq(D.warehouse), onF);
    make('ms-ro-account','roacct','Account', uniq(D.account), onF);
    make('ms-ro-slab','roslab','Weight',
         ['0-3kg','3+kg','unknown'].filter(x => D.slab.indexOf(x) !== -1), onF);
    renderRo();
  }

  BX.tab({ panel: 'be-radofd', dataset: 'radofd', init: init, onShow: renderRo });
})();
