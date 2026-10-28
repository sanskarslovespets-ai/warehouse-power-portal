(function () {
  "use strict";

  // Breach Explorer > Complaints: last-mile complaint tickets (CX_data.CDC_Nugget) by courier and sub-issue,
  // against delivered volume. The SLA Breach Explorer's complaints tab (see be_common.js), filtering
  // breach_pipeline's complaints export in the browser.

  const BX = window.BX;
  const el = BX.el, fmt = BX.fmt, uniq = BX.uniq, esc = BX.esc;

  // 0 date, 1 category, 2 issue, 3 sub_issue, 4 channel, 5 status, 6 courier,
  // 7 how the courier was attributed (AWB direct / DN hop / none), 8 tickets, 9 resolved
  const C = { date:0, cat:1, issue:2, sub:3, channel:4, status:5, courier:6,
              source:7, n:8, res:9 };
  const pct = (a,b) => b > 0 ? (a/b*100).toFixed(1) + '%' : '·';

  // headline groupings over the scoped issue/sub-issue pairs
  const G_DELAY  = ['Delayed', 'Within EDD'];
  const G_FAILED = ['Failed Attempt'];
  const G_MDND   = ['MDND'];
  const G_WMD    = ['Received Damaged Product', 'Expired Products Delivered'];
  const G_CANCEL = ['Cancellation', 'Why Was My Order Cancelled'];
  const FAKE_SUB = 'Fake Attempt (Nobody Came)';

  const MS = {};
  let RAW = {}, D = {}, F = [], DMIN = '', DMAX = '', DELIVERED = {};

  // issue list follows category; sub-issue list follows issue
  function narrow() {
    const cat = MS.cxcat.sel;
    const s = new Set();
    F.forEach(r => { if (!cat.size || cat.has(D.category[r[C.cat]])) s.add(D.issue[r[C.issue]]); });
    MS.cxissue.setValues(uniq(Array.from(s)));
    narrowSub();
  }
  function narrowSub() {
    const cat = MS.cxcat.sel, iss = MS.cxissue.sel;
    const s = new Set();
    F.forEach(r => {
      if (cat.size && !cat.has(D.category[r[C.cat]])) return;
      if (iss.size && !iss.has(D.issue[r[C.issue]])) return;
      s.add(D.sub_issue[r[C.sub]]);
    });
    MS.cxsub.setValues(uniq(Array.from(s)));
  }

  function rows() {
    const from = el('cx-from').value, to = el('cx-to').value;
    const cat = MS.cxcat.sel, iss = MS.cxissue.sel, sub = MS.cxsub.sel,
          cou = MS.cxcour.sel, ch = MS.cxch.sel, st = MS.cxst.sel;
    const out = [];
    for (let i = 0; i < F.length; i++) {
      const r = F[i], d = D.date[r[C.date]];
      if (d < from || d > to) continue;
      if (cat.size && !cat.has(D.category[r[C.cat]])) continue;
      if (iss.size && !iss.has(D.issue[r[C.issue]])) continue;
      if (sub.size && !sub.has(D.sub_issue[r[C.sub]])) continue;
      if (cou.size && !cou.has(D.courier[r[C.courier]])) continue;
      if (ch.size  && !ch.has(D.channel[r[C.channel]])) continue;
      if (st.size  && !st.has(D.status[r[C.status]])) continue;
      out.push(r);
    }
    return out;
  }

  /* ---- Fake attempt rate: tickets measured against delivered volume ---- */
  const fakeSort = { col: 3, dir: -1 };

  // Courier rate view: whatever the filters currently select, expressed against
  // that courier's delivered volume over the same dates. `rs` is already filtered,
  // so the issue / sub-issue pickers drive the numerator directly.
  function renderCourierRate(rs) {
    const from = el('cx-from').value, to = el('cx-to').value;
    const cou = MS.cxcour.sel;

    // panel-level sub-issue picker narrows the numerator only
    const panelSub = (MS.cxcsub && MS.cxcsub.sel.size) ? MS.cxcsub.sel : null;
    const tix = new Map();
    rs.forEach(function (r) {
      if (panelSub && !panelSub.has(D.sub_issue[r[C.sub]])) return;
      const c = D.courier[r[C.courier]];
      tix.set(c, (tix.get(c) || 0) + r[C.n]);
    });

    // denominator: delivered shipments by courier, same dates
    const del = new Map();
    Object.keys(DELIVERED).forEach(function (d) {
      if (d < from || d > to) return;
      const byC = DELIVERED[d];
      Object.keys(byC).forEach(function (c) {
        if (cou.size && !cou.has(c)) return;
        del.set(c, (del.get(c) || 0) + byC[c]);
      });
    });

    const names = Array.from(new Set(Array.from(tix.keys()).concat(Array.from(del.keys()))));
    let arr = names.map(function (c) {
      const t = tix.get(c) || 0, d = del.get(c) || 0;
      return { k: c, t: t, d: d, rate: d > 0 ? t / d * 100 : null };
    }).filter(function (o) { return o.t > 0 || o.d > 0; });

    const val = function (o, c) {
      return c === -1 ? o.k : (c === 0 ? o.t : (c === 1 ? o.d : (o.rate === null ? -1 : o.rate)));
    };
    arr.sort(function (x, y) {
      const a = val(x, fakeSort.col), b = val(y, fakeSort.col);
      return typeof a === 'string' ? fakeSort.dir * a.localeCompare(b) : fakeSort.dir * (a - b);
    });

    // totals exclude Unmapped from the rate - those tickets have no courier to divide by
    let tt = 0, td = 0, tUn = 0;
    arr.forEach(function (o) {
      if (o.k === 'Unmapped') { tUn += o.t; return; }
      tt += o.t; td += o.d;
    });

    const r3 = function (v) { return v === null ? '·' : v.toFixed(3) + '%'; };
    const per1k = function (o) { return o.rate === null ? '·' : (o.rate * 10).toFixed(2); };

    let h = '<thead><tr>'
          + '<th class="sortable" data-c="-1">Courier</th>'
          + '<th class="sortable" data-c="0">Tickets</th>'
          + '<th class="sortable" data-c="1">Delivered</th>'
          + '<th class="sortable share" data-c="2">Rate</th>'
          + '<th class="sortable" data-c="3">Per 1,000</th>'
          + '</tr></thead><tbody>';
    arr.forEach(function (o) {
      const un = o.k === 'Unmapped';
      h += '<tr><td>' + esc(o.k) + '</td>'
         + '<td>' + fmt(o.t) + '</td>'
         + '<td>' + (o.d ? fmt(o.d) : '·') + '</td>'
         + '<td class="share">' + (un ? '·' : r3(o.rate)) + '</td>'
         + '<td class="pctcell">' + (un ? '·' : per1k(o)) + '</td></tr>';
    });
    h += '</tbody><tfoot><tr><td>Total'
       + (tUn ? ' (excl. ' + fmt(tUn) + ' unmapped tickets)' : '')
       + '</td><td>' + fmt(tt) + '</td><td>' + fmt(td) + '</td>'
       + '<td>' + (td ? (tt / td * 100).toFixed(3) + '%' : '·') + '</td>'
       + '<td>' + (td ? (tt / td * 1000).toFixed(2) : '·') + '</td></tr></tfoot>';

    const t = el('t-cx-courier');
    t.innerHTML = h;
    t.querySelectorAll('th.sortable').forEach(function (th) {
      th.addEventListener('click', function () {
        const c = parseInt(th.dataset.c, 10);
        if (fakeSort.col === c) { fakeSort.dir = -fakeSort.dir; }
        else { fakeSort.col = c; fakeSort.dir = (c === -1 ? 1 : -1); }
        renderCourierRate(rows());
      });
    });
  }

  // Sub-issue rate view. Denominator is delivered volume for the selected dates and
  // couriers - a sub-issue has no courier of its own, so the whole selection is the base.
  const subSort = { col: 0, dir: -1 };

  function deliveredTotal(courSel) {
    const from = el('cx-from').value, to = el('cx-to').value;
    let n = 0;
    Object.keys(DELIVERED).forEach(function (d) {
      if (d < from || d > to) return;
      const byC = DELIVERED[d];
      Object.keys(byC).forEach(function (c) {
        if (courSel && courSel.size && !courSel.has(c)) return;
        n += byC[c];
      });
    });
    return n;
  }

  function renderSubRate(rs) {
    // panel-level courier picker wins; otherwise fall back to the global one
    const panelSel = (MS.cxscour && MS.cxscour.sel.size) ? MS.cxscour.sel : null;
    const effSel = panelSel || (MS.cxcour.sel.size ? MS.cxcour.sel : null);

    const use = panelSel
      ? rs.filter(function (r) { return panelSel.has(D.courier[r[C.courier]]); })
      : rs;

    const m = new Map();
    use.forEach(function (r) {
      const k = D.sub_issue[r[C.sub]];
      if (!m.has(k)) m.set(k, { n: 0, res: 0 });
      const o = m.get(k); o.n += r[C.n]; o.res += r[C.res];
    });

    const den = deliveredTotal(effSel);
    let arr = Array.from(m.entries()).map(function (e) {
      return { k: e[0], n: e[1].n, res: e[1].res, rate: den > 0 ? e[1].n / den * 100 : null };
    });

    const val = function (o, c) {
      return c === -1 ? o.k : (c === 0 ? o.n : (c === 1 ? (o.rate === null ? -1 : o.rate)
                                                        : (o.n ? o.res / o.n : -1)));
    };
    arr.sort(function (x, y) {
      const a = val(x, subSort.col), b = val(y, subSort.col);
      return typeof a === 'string' ? subSort.dir * a.localeCompare(b) : subSort.dir * (a - b);
    });

    const tn = arr.reduce(function (s, o) { return s + o.n; }, 0);
    const tr = arr.reduce(function (s, o) { return s + o.res; }, 0);
    const r3 = function (v) { return v === null ? '·' : v.toFixed(3) + '%'; };

    let h = '<thead><tr>'
          + '<th class="sortable" data-c="-1">Sub-issue</th>'
          + '<th class="sortable" data-c="0">Tickets</th>'
          + '<th class="sortable share" data-c="1">Rate</th>'
          + '<th class="sortable" data-c="1">Per 1,000</th>'
          + '<th class="sortable" data-c="2">Resolved %</th>'
          + '</tr></thead><tbody>';
    arr.forEach(function (o) {
      h += '<tr><td>' + esc(o.k) + '</td>'
         + '<td>' + fmt(o.n) + '</td>'
         + '<td class="share">' + r3(o.rate) + '</td>'
         + '<td class="pctcell">' + (o.rate === null ? '·' : (o.rate * 10).toFixed(2)) + '</td>'
         + '<td class="pctcell">' + (o.n ? (o.res / o.n * 100).toFixed(1) + '%' : '·') + '</td></tr>';
    });
    h += '</tbody><tfoot><tr><td>Total</td>'
       + '<td>' + fmt(tn) + '</td>'
       + '<td>' + (den ? (tn / den * 100).toFixed(3) + '%' : '·') + '</td>'
       + '<td>' + (den ? (tn / den * 1000).toFixed(2) : '·') + '</td>'
       + '<td>' + (tn ? (tr / tn * 100).toFixed(1) + '%' : '·') + '</td></tr></tfoot>';

    const t = el('t-cx-sub');
    t.innerHTML = h;
    el('cx-sub-note').textContent = 'against ' + fmt(den) + ' delivered shipments';
    t.querySelectorAll('th.sortable').forEach(function (th) {
      th.addEventListener('click', function () {
        const c = parseInt(th.dataset.c, 10);
        if (subSort.col === c) { subSort.dir = -subSort.dir; }
        else { subSort.col = c; subSort.dir = (c === -1 ? 1 : -1); }
        renderSubRate(rows());
      });
    });
  }

  const faSort = { col: 2, dir: -1 };

  // Standing view: fake attempts per courier, always this sub-issue whatever the
  // issue filters say. Honours only the date range and the global Courier filter.
  function renderFakeAttempt() {
    const from = el('cx-from').value, to = el('cx-to').value;
    const cou = MS.cxcour.sel;

    const tix = new Map();
    for (let i = 0; i < F.length; i++) {
      const r = F[i], d = D.date[r[C.date]];
      if (d < from || d > to) continue;
      if (D.sub_issue[r[C.sub]] !== FAKE_SUB) continue;
      const c = D.courier[r[C.courier]];
      if (cou.size && !cou.has(c)) continue;
      tix.set(c, (tix.get(c) || 0) + r[C.n]);
    }

    const del = new Map();
    Object.keys(DELIVERED).forEach(function (d) {
      if (d < from || d > to) return;
      const byC = DELIVERED[d];
      Object.keys(byC).forEach(function (c) {
        if (cou.size && !cou.has(c)) return;
        del.set(c, (del.get(c) || 0) + byC[c]);
      });
    });

    const names = Array.from(new Set(Array.from(tix.keys()).concat(Array.from(del.keys()))));
    let arr = names.map(function (c) {
      const t = tix.get(c) || 0, d = del.get(c) || 0;
      return { k: c, t: t, d: d, rate: d > 0 ? t / d * 100 : null };
    }).filter(function (o) { return o.t > 0 || o.d > 0; });

    const val = function (o, c) {
      return c === -1 ? o.k : (c === 0 ? o.t : (c === 1 ? o.d : (o.rate === null ? -1 : o.rate)));
    };
    arr.sort(function (x, y) {
      const a = val(x, faSort.col), b = val(y, faSort.col);
      return typeof a === 'string' ? faSort.dir * a.localeCompare(b) : faSort.dir * (a - b);
    });

    let tt = 0, td = 0, tUn = 0;
    arr.forEach(function (o) {
      if (o.k === 'Unmapped') { tUn += o.t; return; }
      tt += o.t; td += o.d;
    });

    let h = '<thead><tr>'
          + '<th class="sortable" data-c="-1">Courier</th>'
          + '<th class="sortable" data-c="0">Fake attempts</th>'
          + '<th class="sortable" data-c="1">Delivered</th>'
          + '<th class="sortable share" data-c="2">Rate</th>'
          + '<th class="sortable" data-c="2">Per 1,000</th>'
          + '</tr></thead><tbody>';
    arr.forEach(function (o) {
      const un = o.k === 'Unmapped';
      h += '<tr><td>' + esc(o.k) + '</td>'
         + '<td>' + fmt(o.t) + '</td>'
         + '<td>' + (o.d ? fmt(o.d) : '·') + '</td>'
         + '<td class="share">' + (un || o.rate === null ? '·' : o.rate.toFixed(3) + '%') + '</td>'
         + '<td class="pctcell">' + (un || o.rate === null ? '·' : (o.rate * 10).toFixed(2)) + '</td></tr>';
    });
    h += '</tbody><tfoot><tr><td>Total'
       + (tUn ? ' (excl. ' + fmt(tUn) + ' unmapped)' : '') + '</td>'
       + '<td>' + fmt(tt) + '</td><td>' + fmt(td) + '</td>'
       + '<td>' + (td ? (tt / td * 100).toFixed(3) + '%' : '·') + '</td>'
       + '<td>' + (td ? (tt / td * 1000).toFixed(2) : '·') + '</td></tr></tfoot>';
    const t = el('t-cx-fakeatt');
    t.innerHTML = h;
    t.querySelectorAll('th.sortable').forEach(function (th) {
      th.addEventListener('click', function () {
        const c = parseInt(th.dataset.c, 10);
        if (faSort.col === c) { faSort.dir = -faSort.dir; }
        else { faSort.col = c; faSort.dir = (c === -1 ? 1 : -1); }
        renderFakeAttempt();
      });
    });
  }

  function renderCx() {
    if (!F.length) return;
    const rs = rows();
    let total = 0, delay = 0, failed = 0, mdnd = 0, wmd = 0, cancel = 0;
    rs.forEach(r => {
      const n = r[C.n], iss = D.issue[r[C.issue]];
      total += n;
      if (G_DELAY.indexOf(iss)  !== -1) delay  += n;
      if (G_FAILED.indexOf(iss) !== -1) failed += n;
      if (G_MDND.indexOf(iss)   !== -1) mdnd   += n;
      if (G_WMD.indexOf(iss)    !== -1) wmd    += n;
      if (G_CANCEL.indexOf(iss) !== -1) cancel += n;
    });
    el('cx-cards').innerHTML =
      '<div class="pcard"><div class="k">Tickets</div><div class="v">' + fmt(total) +
        '</div><div class="s">in the selected range</div></div>' +
      '<div class="pcard crit"><div class="k">Delayed / not moving</div><div class="v">' + fmt(delay) +
        '</div><div class="s">' + pct(delay, total) + '</div></div>' +
      '<div class="pcard crit"><div class="k">Failed attempt</div><div class="v">' + fmt(failed) +
        '</div><div class="s">' + pct(failed, total) + '</div></div>' +
      '<div class="pcard crit"><div class="k">MDND</div><div class="v">' + fmt(mdnd) +
        '</div><div class="s">' + pct(mdnd, total) + ' · found + not found</div></div>' +
      '<div class="pcard crit"><div class="k">Damaged / expired</div><div class="v">' + fmt(wmd) +
        '</div><div class="s">' + pct(wmd, total) + ' · condition on arrival</div></div>' +
      '<div class="pcard"><div class="k">Cancellation</div><div class="v">' + fmt(cancel) +
        '</div><div class="s">' + pct(cancel, total) + '</div></div>';

    renderSubRate(rs);
    renderCourierRate(rs);
    renderFakeAttempt();
  }

  ['cx-from','cx-to'].forEach(id => el(id).addEventListener('change', renderCx));
  el('btn-cx-reset').addEventListener('click', function () {
    ['cxcat','cxissue','cxsub','cxcour','cxch','cxst','cxscour','cxcsub']
      .forEach(k => MS[k] && MS[k].clear());
    el('cx-from').value = DMIN; el('cx-to').value = DMAX;
    narrow();
    renderCx();
  });

  // CSV is read back off the rendered table, so it always matches what is on screen
  function wireCsv(btnId, tableId, name) {
    el(btnId).addEventListener('click', function () {
      if (!F.length) return;
      BX.saveCsv(name + '_' + el('cx-from').value + '_to_' + el('cx-to').value + '.csv', BX.tableCsv(tableId));
    });
  }
  wireCsv('btn-cxc-csv', 't-cx-courier', 'complaints_by_courier');
  wireCsv('btn-cxs-csv', 't-cx-sub', 'complaints_by_subissue');
  wireCsv('btn-cxfa-csv', 't-cx-fakeatt', 'fake_attempt_by_courier');

  function init(raw) {
    const oldMin = DMIN, oldMax = DMAX;
    RAW = raw;
    D = raw.dims || {};
    F = raw.facts || [];
    if (!F.length) return;
    DELIVERED = RAW.delivered || {};
    const dates = D.date.slice().sort();
    DMIN = dates[0]; DMAX = dates[dates.length - 1];
    BX.keepRange('cx-from', 'cx-to', oldMin, oldMax, DMIN, DMAX);
    el('cx-asof').innerHTML = 'Source <strong>CX_data.CDC_Nugget</strong> · '
      + fmt(RAW.tickets || 0) + ' tickets · ' + DMIN + ' to ' + DMAX
      + ' · courier attributed on <strong>' + fmt(RAW.mapped || 0) + '</strong> ('
      + ((RAW.mapped || 0) / (RAW.tickets || 1) * 100).toFixed(0) + '%), of which '
      + fmt(RAW.mapped_via_dn || 0) + ' via the DN hop';

    const onF = () => renderCx();
    const make = (hostId, key, label, values, onChange) => BX.makeMS(MS, hostId, key, label, values, onChange);
    make('ms-cx-cat','cxcat','Category', uniq(D.category), () => { narrow(); onF(); });
    make('ms-cx-issue','cxissue','Issue', uniq(D.issue), () => { narrowSub(); onF(); });
    make('ms-cx-sub','cxsub','Sub-issue', uniq(D.sub_issue), onF);
    make('ms-cx-courier','cxcour','Courier', uniq(D.courier), onF);
    make('ms-cx-channel','cxch','Channel', uniq(D.channel), onF);
    make('ms-cx-status','cxst','Status', uniq(D.status), onF);
    make('ms-cxs-courier','cxscour','Courier', uniq(D.courier), onF);
    make('ms-cxc-sub','cxcsub','Sub-issue', uniq(D.sub_issue), onF);
    if (MS.cxcat.sel.size || MS.cxissue.sel.size) narrow(); // ticks kept from the last data still narrow the lists
    renderCx();
  }

  BX.tab({ panel: 'be-complaints', dataset: 'complaints', init: init, onShow: renderCx });
})();
