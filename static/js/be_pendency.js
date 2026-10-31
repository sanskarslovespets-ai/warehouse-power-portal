(function () {
  "use strict";

  // Breach Explorer > Pendency: open shipments already past promise, or due today / tomorrow. The SLA Breach
  // Explorer's pendency tab (see be_common.js), filtering breach_pipeline's pendency snapshot in the browser.
  // Export CSV is shipment-level: the "pendency-detail" export, fetched the first time it's asked for.

  const BX = window.BX;
  const el = BX.el, fmt = BX.fmt, uniq = BX.uniq, esc = BX.esc;

  // fact layout: 0 promise date, 1 courier, 2 account, 3 slab, 4 wh, 5 origin, 6 dest, 7 status, 8 bucket, 9 age, 10 n
  const C = { pdate:0, courier:1, account:2, slab:3, wh:4, origin:5, dest:6,
              status:7, bucket:8, age:9, n:10 };
  const MS = {};
  let RAW = {}, D = {}, F = [], PMIN = '', PMAX = '';
  let detail = null; // the shipment-level rows, once fetched: {generated_at_ist, columns, rows}

  const promiseDates = () => (D.pdate || []).filter(x => x !== 'unknown').sort();

  ['p-from','p-to'].forEach(id => el(id).addEventListener('change', () => renderPend()));

  // account codes belonging to a courier selection; empty selection = all
  function accountsFor(courSel) {
    if (!courSel.size) return uniq(D.account);
    const s = new Set();
    for (let i = 0; i < F.length; i++) {
      const r = F[i];
      if (courSel.has(D.courier[r[C.courier]])) s.add(D.account[r[C.account]]);
    }
    return uniq(Array.from(s));
  }
  function refreshStatusAccounts() { MS.psacc.setValues(accountsFor(MS.pscour.sel)); }

  function rows() {
    const c = MS.pcour.sel, a = MS.pacc.sel, s = MS.pslab.sel,
          w = MS.pwh.sel, st = MS.pstat.sel, de = MS.pdest.sel;
    const pf = el('p-from').value,
          pt = el('p-to').value;
    const out = [];
    for (let i = 0; i < F.length; i++) {
      const r = F[i];
      const pd = D.pdate[r[C.pdate]];
      if (pf && pd !== 'unknown' && pd < pf) continue;
      if (pt && pd !== 'unknown' && pd > pt) continue;
      if (c.size  && !c.has(D.courier[r[C.courier]])) continue;
      if (a.size  && !a.has(D.account[r[C.account]])) continue;
      if (s.size  && !s.has(D.slab[r[C.slab]])) continue;
      if (w.size  && !w.has(D.warehouse[r[C.wh]])) continue;
      if (st.size && !st.has(D.status[r[C.status]])) continue;
      if (de.size && !de.has(D.dest[r[C.dest]])) continue;
      out.push(r);
    }
    return out;
  }

  const blank = () => ({ breached:0, today:0, tomorrow:0, D1:0, D2:0, 'D3-7':0, 'D8+':0, total:0 });
  function tally(t, r) {
    const b = D.bucket[r[C.bucket]], n = r[C.n];
    t.total += n;
    if (b === 'BREACHED') { t.breached += n; const g = D.age[r[C.age]]; if (g in t) t[g] += n; }
    else if (b === 'DUE_TODAY') t.today += n;
    else if (b === 'DUE_TOMORROW') t.tomorrow += n;
  }

  const HEAD = ['Breached','D1','D2','D3-7','D8+','Due today','Due tomorrow','Total open'];
  function cells(t) {
    return [t.breached, t.D1, t.D2, t['D3-7'], t['D8+'], t.today, t.tomorrow, t.total];
  }

  const sortState = {};
  function buildTable(id, map, label, limit) {
    const table = el(id);
    let arr = Array.from(map.entries()).map(([k, v]) => ({ k, v }));
    const ss = sortState[id] || { col: 0, dir: -1 };
    arr.sort((x, y) => {
      const a = ss.col === -1 ? x.k : cells(x.v)[ss.col];
      const b = ss.col === -1 ? y.k : cells(y.v)[ss.col];
      if (typeof a === 'string') return ss.dir * a.localeCompare(b);
      return ss.dir * ((a === undefined ? 0 : a) - (b === undefined ? 0 : b));
    });
    const shown = limit ? arr.slice(0, limit) : arr;
    const tot = blank();
    arr.forEach(r => { ['breached','today','tomorrow','D1','D2','D3-7','D8+','total']
      .forEach(k => { tot[k] += r.v[k]; }); });

    let h = '<thead><tr><th class="sortable" data-c="-1">' + esc(label) + '</th>';
    HEAD.forEach((x, i) => { h += '<th class="sortable" data-c="' + i + '">' + x + '</th>'; });
    h += '</tr></thead><tbody>';
    shown.forEach(r => {
      h += '<tr><td>' + esc(r.k) + '</td>';
      cells(r.v).forEach(v => { h += '<td>' + (v ? fmt(v) : '·') + '</td>'; });
      h += '</tr>';
    });
    h += '</tbody><tfoot><tr><td>Total' +
         (limit && arr.length > limit ? ' (' + arr.length + ' rows, top ' + limit + ' shown)' : '') + '</td>';
    cells(tot).forEach(v => { h += '<td>' + (v ? fmt(v) : '·') + '</td>'; });
    h += '</tr></tfoot>';
    table.innerHTML = h;
    table.querySelectorAll('th.sortable').forEach(th => th.addEventListener('click', () => {
      const c = parseInt(th.dataset.c, 10);
      const cur = sortState[id] || { col: 0, dir: -1 };
      sortState[id] = { col: c, dir: (cur.col === c ? -cur.dir : (c === -1 ? 1 : -1)) };
      renderPend();
    }));
  }

  function groupBy(rs, keyFn) {
    const m = new Map();
    rs.forEach(r => {
      const k = keyFn(r);
      if (!m.has(k)) m.set(k, blank());
      tally(m.get(k), r);
    });
    return m;
  }

  function renderPend() {
    if (!F.length) return;
    const rs = rows();
    const t = blank();
    rs.forEach(r => tally(t, r));

    el('pend-cards').innerHTML =
      '<div class="pcard crit"><div class="k">Breached &amp; open</div><div class="v">' + fmt(t.breached) +
        '</div><div class="s">past promise, no terminal status</div></div>' +
      '<div class="pcard"><div class="k">Due today</div><div class="v">' + fmt(t.today) +
        '</div><div class="s">promise falls today</div></div>' +
      '<div class="pcard"><div class="k">Due tomorrow</div><div class="v">' + fmt(t.tomorrow) +
        '</div><div class="s">promise falls tomorrow</div></div>' +
      '<div class="pcard"><div class="k">8+ days late</div><div class="v">' + fmt(t['D8+']) +
        '</div><div class="s">likely stuck, needs a sweep</div></div>';

    buildTable('t-p-courier', groupBy(rs, r => D.courier[r[C.courier]]), 'Courier');
    buildTable('t-p-wh', groupBy(rs, r => D.warehouse[r[C.wh]]), 'Warehouse');
    const sRows = rs.filter(r =>
      (!MS.pscour.sel.size || MS.pscour.sel.has(D.courier[r[C.courier]])) &&
      (!MS.psacc.sel.size  || MS.psacc.sel.has(D.account[r[C.account]])));
    buildTable('t-p-status', groupBy(sRows, r => D.status[r[C.status]]), 'Status');
    buildTable('t-p-lane', groupBy(rs, r => D.origin[r[C.origin]] + ' → ' + D.dest[r[C.dest]]), 'Lane', 40);
  }

  el('btn-p-reset').addEventListener('click', function () {
    ['pcour','pacc','pslab','pwh','pstat','pdest','pscour','psacc']
      .forEach(k => MS[k] && MS[k].clear());
    el('p-from').value = PMIN;
    el('p-to').value = PMAX;
    refreshStatusAccounts();
    renderPend();
  });

  /* ---- shipment-level CSV export, honouring the current pendency filters ---- */
  // detail columns: 2 courier, 3 account, 4 warehouse, 6 dest, 8 weight_g, 9 status, 11 promise_ts
  function detailRows() {
    const c = MS.pcour.sel, a = MS.pacc.sel, s = MS.pslab.sel,
          w = MS.pwh.sel, st = MS.pstat.sel, de = MS.pdest.sel;
    const pf = el('p-from').value, pt = el('p-to').value;
    return detail.rows.filter(function (r) {
      const pd = (r[11] || '').slice(0, 10); // the promise date filters the export as it does the tables
      if (pd && pf && pd < pf) return false;
      if (pd && pt && pd > pt) return false;
      if (c.size  && !c.has(r[2])) return false;
      if (a.size  && !a.has(r[3])) return false;
      if (w.size  && !w.has(r[4])) return false;
      if (de.size && !de.has(r[6])) return false;
      if (st.size && !st.has(r[9])) return false;
      if (s.size) {
        const g = r[8] === '' ? null : parseFloat(r[8]);
        const slab = g === null ? 'unknown' : (g <= 3000 ? '0-3kg' : '3+kg');
        if (!s.has(slab)) return false;
      }
      return true;
    });
  }

  function toCsv(rows) {
    const q = function (v) {
      v = v === null || v === undefined ? '' : String(v);
      // free text (current location) a spreadsheet would run as a formula; not numbers like days_late -1
      if (/^[=+\-@\t\r]/.test(v) && !/^-?\d+(\.\d+)?$/.test(v)) v = "'" + v;
      return /[",\r\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; // a bare \r would start a new cell
    };
    const out = [detail.columns.map(q).join(',')];
    rows.forEach(function (r) { out.push(r.map(q).join(',')); });
    return out.join(String.fromCharCode(10));
  }

  el('btn-p-csv').addEventListener('click', async function () {
    const btn = this;
    if (!detail) {
      btn.disabled = true;
      btn.textContent = 'Preparing…';
      try {
        const res = await fetch('/api/breach-explorer/pendency-detail');
        if (!res.ok) throw new Error();
        detail = await res.json();
      } catch (e) {
        detail = null;
      } finally {
        btn.disabled = false;
        btn.textContent = 'Export CSV';
      }
      if (!detail) { BX.flash(btn, 'Export failed', true); return; }
    }
    const stamp = (detail.generated_at_ist || '').replace(/[^0-9]/g, '').slice(0, 12);
    BX.saveCsv('pendency_' + stamp + '.csv', toCsv(detailRows()));
  });

  function init(raw) {
    const oldMin = PMIN, oldMax = PMAX;
    RAW = raw;
    D = raw.dims || {};
    F = raw.facts || [];
    if (!F.length) return;
    detail = null; // a new snapshot: fetch its shipments afresh at the next export
    const ds = promiseDates();
    PMIN = ds.length ? ds[0] : ''; PMAX = ds.length ? ds[ds.length - 1] : '';
    BX.keepRange('p-from', 'p-to', oldMin, oldMax, PMIN, PMAX);

    el('pend-asof').innerHTML =
      'Snapshot as of <strong>' + esc(RAW.generated_at_ist || 'unknown') + ' IST</strong>' +
      ' · open shipments created in the last ' + (RAW.created_window_days || 60) + ' days' +
      ' · forward shipments only';

    const slabVals = ['0-3kg','3+kg','unknown'].filter(s => D.slab.indexOf(s) !== -1);
    const onF = () => renderPend();
    const make = (hostId, key, label, values, onChange) => BX.makeMS(MS, hostId, key, label, values, onChange);
    make('ms-p-courier','pcour','Courier', uniq(D.courier), onF);
    make('ms-p-account','pacc','Account', uniq(D.account), onF);
    make('ms-p-slab','pslab','Weight', slabVals, onF);
    make('ms-p-wh','pwh','Warehouse', uniq(D.warehouse), onF);
    make('ms-p-status','pstat','Status', uniq(D.status), onF);
    make('ms-p-dest','pdest','Destination', uniq(D.dest), onF);
    make('ms-ps-courier','pscour','Courier', uniq(D.courier),
      () => { refreshStatusAccounts(); renderPend(); });
    make('ms-ps-account','psacc','Account', accountsFor(MS.pscour.sel), onF);
    renderPend();
  }

  BX.tab({ panel: 'be-pendency', dataset: 'pendency', init: init, onShow: renderPend });
})();
