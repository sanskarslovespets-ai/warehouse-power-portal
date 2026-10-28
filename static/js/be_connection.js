(function () {
  "use strict";

  // Breach Explorer > Connection view: pickup to origin-hub-out, against an 08:00 D+1 cutoff. The SLA Breach
  // Explorer's connection tab (see be_common.js), filtering breach_pipeline's connection export in the
  // browser. Export CSV is one row per shipment, from the server (main.breach_explorer_awbs).

  const BX = window.BX;
  const el = BX.el, fmt = BX.fmt, uniq = BX.uniq, esc = BX.esc;

  const C = { date:0, courier:1, origin:2, wh:3, account:4, slab:5,
              tot:6, d1:7, d2:8, d3:9, nc:10, minsSum:11, minsN:12 };
  const p1 = (a,b) => b > 0 ? (a/b*100).toFixed(1) + '%' : '·';
  const MS = {};
  let RAW = {}, D = {}, F = [], DMIN = '', DMAX = '';

  /* A pickup date is only fully judged once 08:00 on D+2 has passed - before
     that, shipments still inside their Day-2 window are counted as Day 3+ or
     Not connected purely because the clock has not run out. Flag rather than
     hide: the volume is real and people are looking for today. */
  function provisional(d) {
    const deadline = new Date(d + 'T08:00:00');
    deadline.setDate(deadline.getDate() + 2);
    return deadline > new Date();
  }

  function rows() {
    const from = el('conn-from').value, to = el('conn-to').value;
    const cs = MS.conncour && MS.conncour.sel,
          ws = MS.connwh && MS.connwh.sel, as = MS.connacct && MS.connacct.sel,
          sl = MS.connslab && MS.connslab.sel;
    return F.filter(function (r) {
      const d = D.date[r[C.date]];
      if ((from && d < from) || (to && d > to)) return false;
      if (cs && cs.size && !cs.has(D.courier[r[C.courier]])) return false;
      if (ws && ws.size && !ws.has(D.warehouse[r[C.wh]])) return false;
      if (as && as.size && !as.has(D.account[r[C.account]])) return false;
      if (sl && sl.size && !sl.has(D.slab[r[C.slab]])) return false;
      return true;
    });
  }

  function summarise(rs) {
    const a = [0,0,0,0,0,0,0];
    rs.forEach(function (r) {
      a[0]+=r[C.tot]; a[1]+=r[C.d1]; a[2]+=r[C.d2]; a[3]+=r[C.d3];
      a[4]+=r[C.nc]; a[5]+=r[C.minsSum]; a[6]+=r[C.minsN];
    });
    return a;
  }

  function table(id, groups, label, opts) {
    opts = opts || {};
    const keys = Array.from(groups.keys()).sort(opts.sortKeys || undefined);
    let h = '<thead><tr><th>' + label + '</th>'
          + '<th>Shipments</th><th>Day 1</th><th>Day 1 %</th>'
          + '<th>Day 2</th><th>By day 2 %</th><th>Day 3+</th><th>By day 3+ %</th>'
          + '<th>Not connected</th></tr></thead><tbody>';
    keys.forEach(function (k) {
      const a = summarise(groups.get(k));
      const flag = opts.dateRows && provisional(k)
        ? ' <span class="pnote">provisional</span>' : '';
      h += '<tr><td>' + esc(k) + flag + '</td>'
         + '<td>' + fmt(a[0]) + '</td><td>' + fmt(a[1]) + '</td>'
         + '<td class="pctcell">' + p1(a[1], a[0]) + '</td>'
         + '<td>' + fmt(a[2]) + '</td>'
         + '<td class="pctcell">' + p1(a[1] + a[2], a[0]) + '</td>'
         + '<td>' + fmt(a[3]) + '</td>'
         + '<td class="pctcell">' + p1(a[1] + a[2] + a[3], a[0]) + '</td>'
         + '<td>' + fmt(a[4]) + '</td></tr>';
    });
    const t = summarise(Array.from(groups.values()).flat());
    h += '</tbody><tfoot><tr><td>Total</td>'
       + '<td>' + fmt(t[0]) + '</td><td>' + fmt(t[1]) + '</td>'
       + '<td class="pctcell">' + p1(t[1], t[0]) + '</td>'
       + '<td>' + fmt(t[2]) + '</td>'
       + '<td class="pctcell">' + p1(t[1] + t[2], t[0]) + '</td>'
       + '<td>' + fmt(t[3]) + '</td>'
       + '<td class="pctcell">' + p1(t[1] + t[2] + t[3], t[0]) + '</td>'
       + '<td>' + fmt(t[4]) + '</td></tr></tfoot>';
    el(id).innerHTML = h;
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

  function renderConn() {
    if (!F.length) return;
    const rs = rows();
    table('t-conn-day', group(rs, r => D.date[r[C.date]]), 'Pickup date',
          { dateRows: true, sortKeys: (x,y) => y.localeCompare(x) });
    table('t-conn-courier', group(rs, r => D.courier[r[C.courier]]), 'Courier');
    table('t-conn-origin', group(rs, r => D.warehouse[r[C.wh]]), 'Warehouse');
  }

  ['conn-from','conn-to'].forEach(id => el(id).addEventListener('change', renderConn));
  el('btn-conn-reset').addEventListener('click', function () {
    ['conncour','connwh','connacct','connslab'].forEach(k => MS[k] && MS[k].clear());
    el('conn-from').value = DMIN; el('conn-to').value = DMAX;
    renderConn();
  });

  function selections() {
    const out = {};
    [['courier','conncour'], ['warehouse','connwh'], ['account','connacct'],
     ['slab','connslab']].forEach(function (p) {
      const m = MS[p[1]];
      if (m && m.sel && m.sel.size) out[p[0]] = Array.from(m.sel);
    });
    return out;
  }
  ['btn-connday-csv', 'btn-conncour-csv', 'btn-connorig-csv'].forEach(function (id) {
    el(id).addEventListener('click', function () {
      if (!F.length) return;
      BX.exportAwbs('connection', el('conn-from').value, el('conn-to').value, selections(), el(id), el('conn-banner'));
    });
  });

  function init(raw) {
    const oldMin = DMIN, oldMax = DMAX;
    RAW = raw;
    D = raw.dims || {};
    F = raw.facts || [];
    if (!F.length) return;
    const dates = D.date.slice().sort();
    DMIN = dates[0]; DMAX = dates[dates.length-1];
    BX.keepRange('conn-from', 'conn-to', oldMin, oldMax, DMIN, DMAX);
    el('conn-asof').innerHTML = 'Bluedart and Delhivery picked <strong>' + DMIN
      + '</strong> to <strong>' + DMAX + '</strong> · '
      + fmt(RAW.shipments || 0) + ' shipments';

    const onF = () => renderConn();
    const make = (hostId, key, label, values, onChange) => BX.makeMS(MS, hostId, key, label, values, onChange);
    make('ms-conn-courier','conncour','Courier', uniq(D.courier), onF);
    make('ms-conn-wh','connwh','Warehouse', uniq(D.warehouse), onF);
    make('ms-conn-account','connacct','Account', uniq(D.account), onF);
    /* fixed order, not alphabetical: 0-3kg then 3+kg reads as a scale */
    make('ms-conn-slab','connslab','Weight',
         ['0-3kg','3+kg','unknown'].filter(x => D.slab.indexOf(x) !== -1), onF);
    renderConn();
  }

  BX.tab({ panel: 'be-connection', dataset: 'connection', init: init, onShow: renderConn });
})();
