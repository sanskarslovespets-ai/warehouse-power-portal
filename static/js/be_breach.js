(function () {
  "use strict";

  // Breach Explorer > Breach analysis: of what was delivered, how much missed the promise. The SLA Breach
  // Explorer's breach tab (see be_common.js), filtering breach_pipeline's breach export in the browser. The
  // date range and the Delivered / RAD basis apply to every view on the tab.

  const BX = window.BX;
  const el = BX.el, fmt = BX.fmt, uniqSorted = BX.uniq, pretty = BX.pretty;
  const root = document.getElementById("panel-be-breach");

  // a fact row: dimension indexes, then counts (export_breach_dashboard_data.py)
  const C = { date:0, courier:1, account:2, slab:3, wh:4, origin:5, dest:6,
              total:7, under:8, day0:9, breach:10, na:11, p2dSum:12, p2dN:13, eddSum:14, eddN:15,
              uD1:16, uD2:17, uD3:18, bD1:19, bD2:20, bD3:21,
              rUnder:22, rDay0:23, rBreach:24, rBD1:25, rBD2:26, rBD3:27, rUD1:28, rUD2:29,
              ptype:30 };
  const METRO7 = ['Bangalore','Mumbai','Delhi','Pune','Hyderabad','Chennai','Kolkata'];
  // QC, SDD, Standard: a fixed order, not alphabetical
  const PTYPE_ORDER = ['QC','SDD','Standard','Unknown'];

  let D = {}, F = [], DMIN = '', DMAX = '';
  let slabVals = [], ptypeVals = [];
  let basis = 'delivered';
  const colMode = { day:'both', ptype:'both', courier:'both', lane:'both' };
  const MS = {};
  let syncing = false;
  let lastLaneMap = new Map();

  const pct = (a,b) => b > 0 ? (a/b*100) : null;
  const p1 = v => v === null ? '·' : v.toFixed(1) + '%';
  const p2 = v => v === null ? '·' : v.toFixed(2);

  // the range button shows the SELECTED range and opens the picker
  function paintRangeLabel() {
    const a = el('f-from').value, b = el('f-to').value;
    const chip = root.querySelector('.dr-chips .chip[aria-pressed="true"]');
    el('range-sub').textContent = (a && b ? pretty(a) + ' – ' + pretty(b) : 'Select dates')
      + (chip ? '  ·  ' + chip.textContent : '') + '   ▾';
  }
  (function wireDatePicker() {
    const host = el('dr'), btn = el('range-sub'), pop = host.querySelector('.ms-pop');
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      const wasOpen = host.classList.contains('open');
      BX.closePopovers();
      if (!wasOpen) { host.classList.add('open'); btn.setAttribute('aria-expanded','true'); }
    });
    pop.addEventListener('click', function (e) { e.stopPropagation(); });
  })();

  // Account codes belonging to a courier selection; empty selection = all.
  // Keeps the 248-entry account list navigable once a courier is picked.
  function accountsFor(courSel) {
    if (!courSel.size) return uniqSorted(D.account);
    const s = new Set();
    for (let i = 0; i < F.length; i++) {
      const r = F[i];
      if (courSel.has(D.courier[r[C.courier]])) s.add(D.account[r[C.account]]);
    }
    return uniqSorted(Array.from(s));
  }
  function refreshAccounts()     { MS.account.setValues(accountsFor(MS.courier.sel)); }
  function refreshLaneAccounts() { MS.lacc.setValues(accountsFor(MS.lcour.sel)); }

  function makeFilters() {
    const make = (hostId, key, label, values, onChange) => BX.makeMS(MS, hostId, key, label, values, onChange);
    const onFilter = () => { clearPreset(); render(); };
    make('ms-courier','courier','Courier', uniqSorted(D.courier), () => { refreshAccounts(); onFilter(); });
    make('ms-account','account','Account', accountsFor(MS.courier ? MS.courier.sel : new Set()), onFilter);
    make('ms-slab','slab','Weight', slabVals, onFilter);
    make('ms-ptype','ptype','Promise type', ptypeVals, onFilter);
    make('ms-wh','wh','Warehouse', uniqSorted(D.warehouse), onFilter);
    make('ms-dest','dest','Destination', uniqSorted(D.dest), function () {
      if (!syncing) {                       // reflect the metro subset in the metro picker
        syncing = true;
        if (MS.metro) MS.metro.set(METRO7.filter(function (m) { return MS.dest.sel.has(m); }));
        syncing = false;
      }
      onFilter();
    });
    // Metro picker: a shortcut into the same Destination filter, scoped to the 7 metros
    make('ms-metro','metro','Metros', METRO7.filter(function (m) { return D.dest.indexOf(m) !== -1; }),
      function () {
        if (syncing) return;
        syncing = true;
        const keep = Array.from(MS.dest.sel).filter(function (v) { return METRO7.indexOf(v) === -1; });
        MS.dest.set(keep.concat(Array.from(MS.metro.sel)));   // preserve non-metro picks
        syncing = false;
        onFilter();
      });
    make('ms-c-wh','cwh','Warehouse', uniqSorted(D.warehouse), () => render());
    make('ms-c-account','cacc','Account', uniqSorted(D.account), () => render());
    make('ms-c-slab','cslab','Weight', slabVals, () => render());
    make('ms-c-ptype','cptype','Promise type', ptypeVals, () => render());
    make('ms-l-courier','lcour','Courier', uniqSorted(D.courier),
      () => { refreshLaneAccounts(); render(); });
    make('ms-l-account','lacc','Account', accountsFor(MS.lcour.sel), () => render());
    make('ms-l-wh','lwh','Warehouse', uniqSorted(D.warehouse), () => render());
    make('ms-l-slab','lslab','Weight', slabVals, () => render());
  }

  /* date presets */
  const iso = d => d.getFullYear() + '-' +
    String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0');
  function clearPreset() { root.querySelectorAll('.chip[data-preset]').forEach(b => b.setAttribute('aria-pressed','false')); }
  function pressedPreset() {
    const b = root.querySelector('.chip[data-preset][aria-pressed="true"]');
    return b ? b.dataset.preset : null;
  }
  function applyPreset(p) {
    const M = new Date(DMAX + 'T00:00:00'); let from, to;
    if (p === 'all') { from = DMIN; to = DMAX; }
    else if (p === 'l7')  { const s = new Date(M); s.setDate(s.getDate()-6);  from = iso(s); to = DMAX; }
    else if (p === 'l14') { const s = new Date(M); s.setDate(s.getDate()-13); from = iso(s); to = DMAX; }
    else if (p === 'mtd') { from = iso(new Date(M.getFullYear(), M.getMonth(), 1)); to = DMAX; }
    else if (p === 'lm')  { from = iso(new Date(M.getFullYear(), M.getMonth()-1, 1)); to = iso(new Date(M.getFullYear(), M.getMonth(), 0)); }
    else if (p === 'lmtd'){ from = iso(new Date(M.getFullYear(), M.getMonth()-1, 1)); to = iso(new Date(M.getFullYear(), M.getMonth()-1, M.getDate())); }
    else                  { from = iso(new Date(M.getFullYear(), M.getMonth()-2, 1)); to = iso(new Date(M.getFullYear(), M.getMonth()-2, M.getDate())); }
    el('f-from').value = from;
    el('f-to').value   = to;
    clearPreset();
    const b = root.querySelector('.chip[data-preset="' + p + '"]');
    if (b) b.setAttribute('aria-pressed','true');
    render();
  }
  root.querySelectorAll('.chip[data-preset]').forEach(b => b.addEventListener('click', () => applyPreset(b.dataset.preset)));
  ['f-from','f-to'].forEach(id => el(id).addEventListener('change', () => { clearPreset(); render(); }));

  /* aggregation */
  function apply() {
    const from = el('f-from').value, to = el('f-to').value;
    const cour = MS.courier.sel, acc = MS.account.sel, slab = MS.slab.sel, wh = MS.wh.sel, dest = MS.dest.sel;
    const pty = MS.ptype.sel;
    const out = [];
    for (let i = 0; i < F.length; i++) {
      const r = F[i], d = D.date[r[C.date]];
      if (d < from || d > to) continue;
      if (cour.size && !cour.has(D.courier[r[C.courier]])) continue;
      if (acc.size  && !acc.has(D.account[r[C.account]])) continue;
      if (slab.size && !slab.has(D.slab[r[C.slab]])) continue;
      if (pty.size  && !pty.has(D.ptype[r[C.ptype]])) continue;
      if (wh.size   && !wh.has(D.warehouse[r[C.wh]])) continue;
      if (dest.size && !dest.has(D.dest[r[C.dest]])) continue;
      out.push(r);
    }
    return out;
  }
  const blank = () => ({ total:0,na:0,p2dSum:0,p2dN:0,eddSum:0,eddN:0,
    under:0,day0:0,breach:0,uD1:0,uD2:0,uD3:0,bD1:0,bD2:0,bD3:0,
    rUnder:0,rDay0:0,rBreach:0,rUD1:0,rUD2:0,rBD1:0,rBD2:0,rBD3:0 });
  function add(a,r){
    a.total+=r[C.total]; a.na+=r[C.na];
    a.p2dSum+=r[C.p2dSum]; a.p2dN+=r[C.p2dN]; a.eddSum+=r[C.eddSum]; a.eddN+=r[C.eddN];
    a.under+=r[C.under]; a.day0+=r[C.day0]; a.breach+=r[C.breach];
    a.uD1+=r[C.uD1]; a.uD2+=r[C.uD2]; a.uD3+=r[C.uD3];
    a.bD1+=r[C.bD1]; a.bD2+=r[C.bD2]; a.bD3+=r[C.bD3];
    a.rUnder+=r[C.rUnder]; a.rDay0+=r[C.rDay0]; a.rBreach+=r[C.rBreach];
    a.rUD1+=r[C.rUD1]; a.rUD2+=r[C.rUD2];
    a.rBD1+=r[C.rBD1]; a.rBD2+=r[C.rBD2]; a.rBD3+=r[C.rBD3];
    return a;
  }
  function groupBy(rows, keyFn) {
    const m = new Map();
    for (let i = 0; i < rows.length; i++) {
      const k = keyFn(rows[i]);
      if (!m.has(k)) m.set(k, blank());
      add(m.get(k), rows[i]);
    }
    return m;
  }
  const summarise = rows => rows.reduce((a,r)=>add(a,r), blank());

  // under-promise is identical in both bases (RAD only moves fractional-EDD
  // orders, which can never be "under promise"), so its D1/D2/D3 split is shared.
  function viewFor(v, b) {
    const x = b === 'rad'
      ? { day0:v.rDay0, breach:v.rBreach, bD1:v.rBD1, bD2:v.rBD2, bD3:v.rBD3 }
      : { day0:v.day0,  breach:v.breach,  bD1:v.bD1,  bD2:v.bD2,  bD3:v.bD3  };
    const t = v.total;
    return {
      total:t, na:v.na,
      under:v.under, uD1:v.uD1, uD2:v.uD2, uD3:v.uD3,
      day0:x.day0, breach:x.breach, bD1:x.bD1, bD2:x.bD2, bD3:x.bD3,
      underPct:pct(v.under,t), day0Pct:pct(x.day0,t), breachPct:pct(x.breach,t), naPct:pct(v.na,t),
      uD1Pct:pct(v.uD1,t), uD2Pct:pct(v.uD2,t), uD3Pct:pct(v.uD3,t),
      bD1Pct:pct(x.bD1,t), bD2Pct:pct(x.bD2,t), bD3Pct:pct(x.bD3,t),
      avgP2D: v.p2dN ? v.p2dSum/v.p2dN : null, avgEDD: v.eddN ? v.eddSum/v.eddN : null
    };
  }
  const view = v => viewFor(v, basis);

  /* tables */
  const sortState = { day:{col:'key',dir:1}, ptype:{col:'key',dir:1},
                      courier:{col:'total',dir:-1}, lane:{col:'total',dir:-1} };
  const pillClass = v => v===null?'':(v>=12?'p-crit':(v>=6?'p-warn':'p-good'));
  const arrow = (st,c) => st.col===c ? (st.dir<0?'↓':'↑') : '';
  const scls = (st,c) => st.col===c ? 'sorted' : '';
  const esc = BX.esc;

  function buildTable(tblId, which, rowsMap, firstHead, opts) {
    opts = opts || {};
    const st = sortState[which], mode = colMode[which];
    const showC = mode !== 'pct', showP = mode !== 'count';
    const showU3 = true;
    const arr = Array.from(rowsMap.entries()).map(function (e) { return Object.assign({ key:e[0] }, view(e[1])); });
    // share of all breaches in this view (denominator spans every group, not just the shown
    // slice) - must be computed BEFORE sorting so the Share column is sortable
    if (opts.share) {
      const denom = opts.totalRow ? viewFor(opts.totalRow, basis).breach
                                  : arr.reduce(function (s,r) { return s + r.breach; }, 0);
      arr.forEach(function (r) { r.breachShare = denom > 0 ? r.breach / denom * 100 : null; });
    }
    arr.sort(function (a,b) {
      let x=a[st.col], y=b[st.col];
      if (st.col === 'key') return st.dir * String(x).localeCompare(String(y));
      x = (x===null||x===undefined)?-Infinity:x; y = (y===null||y===undefined)?-Infinity:y;
      return st.dir*(x-y);
    });
    const shown = opts.limit ? arr.slice(0,opts.limit) : arr;
    const tbl = el(tblId);
    if (!shown.length) { tbl.innerHTML = '<tbody><tr><td class="empty" colspan="30">Nothing matches these filters.</td></tr></tbody>'; return; }

    const H  = (c,t,x) => '<th rowspan="2" data-col="' + c + '" class="srt ' + scls(st,c) + ' ' + (x||'') + '">' + t + '<span class="dim">' + arrow(st,c) + '</span></th>';
    const h2 = (c,t,x) => '<th data-col="' + c + '" class="srt ' + scls(st,c) + ' ' + (x||'') + '">' + t + '<span class="dim">' + arrow(st,c) + '</span></th>';
    const uCols = showU3 ? 4 : 3;

    let r1 = '<th rowspan="2" data-col="key" class="srt ' + scls(st,'key') + '">' + firstHead + '<span class="dim">' + arrow(st,'key') + '</span></th>' +
             H('total','Delivered','edge');
    if (showC) r1 += '<th colspan="' + uCols + '" class="gh g-cnt edge">Under promise</th>' + H('day0','Day 0','g-cnt') +
                     '<th colspan="4" class="gh g-cnt">Breach</th>' + H('na','NA','g-cnt');
    if (showP) r1 += '<th colspan="' + uCols + '" class="gh g-pct edge">Under promise %</th>' + H('day0Pct','Day 0 %','g-pct') +
                     '<th colspan="' + (opts.share ? 5 : 4) + '" class="gh g-pct">Breach %</th>' + H('naPct','NA %','g-pct');
    r1 += H('avgP2D','P2D','edge') + H('avgEDD','EDD');

    let r2 = '';
    if (showC) r2 += h2('uD1','D1','g-cnt edge') + h2('uD2','D2','g-cnt') +
                     (showU3 ? h2('uD3','D3+','g-cnt') : '') + h2('under','All','g-cnt tot') +
                     h2('bD1','D1','g-cnt edge') + h2('bD2','D2','g-cnt') + h2('bD3','D3+','g-cnt') + h2('breach','All','g-cnt tot');
    if (showP) r2 += h2('uD1Pct','D1','g-pct edge') + h2('uD2Pct','D2','g-pct') +
                     (showU3 ? h2('uD3Pct','D3+','g-pct') : '') + h2('underPct','All','g-pct tot') +
                     h2('bD1Pct','D1','g-pct edge') + h2('bD2Pct','D2','g-pct') + h2('bD3Pct','D3+','g-pct') + h2('breachPct','All','g-pct tot') +
                     (opts.share ? h2('breachShare','Share','g-pct share') : '');

    function row(r) {
      let s = '<td>' + esc(r.key) + '</td><td class="edge num">' + fmt(r.total) + '</td>';
      if (showC) s += '<td class="g-cnt edge num">' + fmt(r.uD1) + '</td><td class="g-cnt num">' + fmt(r.uD2) + '</td>' +
                      (showU3 ? '<td class="g-cnt num">' + fmt(r.uD3) + '</td>' : '') +
                      '<td class="g-cnt num tot">' + fmt(r.under) + '</td>' +
                      '<td class="g-cnt num">' + fmt(r.day0) + '</td>' +
                      '<td class="g-cnt edge num">' + fmt(r.bD1) + '</td><td class="g-cnt num">' + fmt(r.bD2) + '</td><td class="g-cnt num">' + fmt(r.bD3) + '</td>' +
                      '<td class="g-cnt num tot">' + fmt(r.breach) + '</td>' +
                      '<td class="g-cnt num dim">' + fmt(r.na) + '</td>';
      if (showP) s += '<td class="g-pct edge num pctcell">' + p1(r.uD1Pct) + '</td><td class="g-pct num pctcell">' + p1(r.uD2Pct) + '</td>' +
                      (showU3 ? '<td class="g-pct num pctcell">' + p1(r.uD3Pct) + '</td>' : '') +
                      '<td class="g-pct num tot">' + p1(r.underPct) + '</td>' +
                      '<td class="g-pct num pctcell">' + p1(r.day0Pct) + '</td>' +
                      '<td class="g-pct edge num pctcell">' + p1(r.bD1Pct) + '</td><td class="g-pct num pctcell">' + p1(r.bD2Pct) + '</td><td class="g-pct num pctcell">' + p1(r.bD3Pct) + '</td>' +
                      '<td class="g-pct num"><span class="pill ' + pillClass(r.breachPct) + '">' + (r.breachPct===null?'·':r.breachPct.toFixed(2)+'%') + '</span></td>' +
                      (opts.share ? '<td class="g-pct num share">' + p1(r.breachShare) + '</td>' : '') +
                      '<td class="g-pct num dim">' + p1(r.naPct) + '</td>';
      s += '<td class="edge num">' + p2(r.avgP2D) + '</td><td class="num">' + p2(r.avgEDD) + '</td>';
      return '<tr>' + s + '</tr>';
    }

    let foot = '';
    if (opts.totalRow) {
      const tr = Object.assign({ key: 'Total · ' + fmt(arr.length) + ' ' + (opts.totalNoun||'') }, view(opts.totalRow));
      if (opts.share) tr.breachShare = tr.breach > 0 ? 100 : null;
      foot = '<tfoot>' + row(tr) + '</tfoot>';
    }
    tbl.innerHTML = '<thead><tr>' + r1 + '</tr><tr>' + r2 + '</tr></thead><tbody>' + shown.map(row).join('') + '</tbody>' + foot;
    tbl.querySelectorAll('thead th[data-col]').forEach(function (th) {
      th.addEventListener('click', function () {
        const c = th.dataset.col;
        if (st.col === c) st.dir *= -1; else { st.col = c; st.dir = c === 'key' ? 1 : -1; }
        render();
      });
    });
  }

  function renderOverall(t) {
    const rows = [
      { key: 'Delivered basis', b: 'delivered' },
      { key: 'RAD basis',       b: 'rad' }
    ].map(function (o) { return Object.assign({ key:o.key, b:o.b }, viewFor(t, o.b)); });

    const H  = (c,txt,x) => '<th rowspan="2" class="' + (x||'') + '">' + txt + '</th>';
    const h2 = (txt,x) => '<th class="' + (x||'') + '">' + txt + '</th>';
    const r1 = '<th rowspan="2">Basis</th>' + H('','Delivered','edge') +
      '<th colspan="4" class="gh g-cnt edge">Under promise</th>' + H('','Day 0','g-cnt') +
      '<th colspan="4" class="gh g-cnt">Breach</th>' + H('','NA','g-cnt') +
      '<th colspan="4" class="gh g-pct edge">Under promise %</th>' + H('','Day 0 %','g-pct') +
      '<th colspan="4" class="gh g-pct">Breach %</th>' + H('','NA %','g-pct') +
      H('','P2D','edge') + H('','EDD');
    const r2 =
      h2('D1','g-cnt edge')+h2('D2','g-cnt')+h2('D3+','g-cnt')+h2('All','g-cnt tot')+
      h2('D1','g-cnt edge')+h2('D2','g-cnt')+h2('D3+','g-cnt')+h2('All','g-cnt tot')+
      h2('D1','g-pct edge')+h2('D2','g-pct')+h2('D3+','g-pct')+h2('All','g-pct tot')+
      h2('D1','g-pct edge')+h2('D2','g-pct')+h2('D3+','g-pct')+h2('All','g-pct tot');

    const body = rows.map(function (r) {
      const on = r.b === basis;
      return '<tr class="' + (on ? 'active' : 'idle') + '">' +
        '<td>' + r.key + (on ? '<span class="basis-tag">showing</span>' : '') + '</td>' +
        '<td class="edge num">' + fmt(r.total) + '</td>' +
        '<td class="g-cnt edge num">' + fmt(r.uD1) + '</td><td class="g-cnt num">' + fmt(r.uD2) + '</td>' +
        '<td class="g-cnt num">' + fmt(r.uD3) + '</td><td class="g-cnt num tot">' + fmt(r.under) + '</td>' +
        '<td class="g-cnt num">' + fmt(r.day0) + '</td>' +
        '<td class="g-cnt edge num">' + fmt(r.bD1) + '</td><td class="g-cnt num">' + fmt(r.bD2) + '</td>' +
        '<td class="g-cnt num">' + fmt(r.bD3) + '</td><td class="g-cnt num tot">' + fmt(r.breach) + '</td>' +
        '<td class="g-cnt num dim">' + fmt(r.na) + '</td>' +
        '<td class="g-pct edge num pctcell">' + p1(r.uD1Pct) + '</td><td class="g-pct num pctcell">' + p1(r.uD2Pct) + '</td>' +
        '<td class="g-pct num pctcell">' + p1(r.uD3Pct) + '</td><td class="g-pct num tot">' + p1(r.underPct) + '</td>' +
        '<td class="g-pct num pctcell">' + p1(r.day0Pct) + '</td>' +
        '<td class="g-pct edge num pctcell">' + p1(r.bD1Pct) + '</td><td class="g-pct num pctcell">' + p1(r.bD2Pct) + '</td>' +
        '<td class="g-pct num pctcell">' + p1(r.bD3Pct) + '</td>' +
        '<td class="g-pct num"><span class="pill ' + pillClass(r.breachPct) + '">' +
          (r.breachPct===null?'·':r.breachPct.toFixed(2)+'%') + '</span></td>' +
        '<td class="g-pct num dim">' + p1(r.naPct) + '</td>' +
        '<td class="edge num">' + p2(r.avgP2D) + '</td><td class="num">' + p2(r.avgEDD) + '</td>' +
      '</tr>';
    }).join('');

    el('t-overall').innerHTML = '<thead><tr>' + r1 + '</tr><tr>' + r2 + '</tr></thead><tbody>' + body + '</tbody>';

    const d = pct(t.breach,t.total), r = pct(t.rBreach,t.total);
    el('ov-note').textContent = 'aggregate for the selected filters · ' +
      el('f-from').value + ' – ' + el('f-to').value +
      (d !== null && r !== null ? ' · RAD is ' + Math.abs(d-r).toFixed(2) +
        ' pts ' + (r < d ? 'lower' : 'higher') : '');
  }

  function render() {
    if (!F.length) return;
    const rows = apply();
    const t = summarise(rows);
    paintRangeLabel();
    renderOverall(t);
    const nDays = new Set(rows.map(r => D.date[r[C.date]])).size;
    el('day-note').textContent = nDays + (nDays === 1 ? ' day' : ' days') + ' · ' + basis + ' basis';

    buildTable('t-day','day', groupBy(rows, r => D.date[r[C.date]]), 'Delivery date', { totalRow:t, totalNoun:'days' });

    // fixed row order (QC, SDD, Standard) rather than the table's own sort default
    const pMap = new Map();
    PTYPE_ORDER.forEach(function (k) { if (ptypeVals.indexOf(k) !== -1) pMap.set(k, blank()); });
    groupBy(rows, r => D.ptype[r[C.ptype]]).forEach(function (v, k) { pMap.set(k, v); });
    buildTable('t-ptype','ptype', pMap, 'Promise type', { totalRow:t, totalNoun:'types', share:true });

    const cRows = rows.filter(r => (!MS.cwh.sel.size || MS.cwh.sel.has(D.warehouse[r[C.wh]])) &&
                                   (!MS.cacc.sel.size || MS.cacc.sel.has(D.account[r[C.account]])) &&
                                   (!MS.cslab.sel.size || MS.cslab.sel.has(D.slab[r[C.slab]])) &&
                                   (!MS.cptype.sel.size || MS.cptype.sel.has(D.ptype[r[C.ptype]])));
    buildTable('t-courier','courier', groupBy(cRows, r => D.courier[r[C.courier]]), 'Courier',
               { totalRow: summarise(cRows), totalNoun:'couriers', share:true });

    const lRows = rows.filter(r => (!MS.lcour.sel.size || MS.lcour.sel.has(D.courier[r[C.courier]])) &&
                                   (!MS.lacc.sel.size || MS.lacc.sel.has(D.account[r[C.account]])) &&
                                   (!MS.lwh.sel.size || MS.lwh.sel.has(D.warehouse[r[C.wh]])) &&
                                   (!MS.lslab.sel.size || MS.lslab.sel.has(D.slab[r[C.slab]])));
    lastLaneMap = groupBy(lRows, r => D.origin[r[C.origin]] + ' → ' + D.dest[r[C.dest]]);
    buildTable('t-lane','lane', lastLaneMap, 'Lane', { limit:60, totalRow: summarise(lRows), totalNoun:'lanes', share:true });
  }

  /* controls */
  function pressBasis() {
    root.querySelectorAll('.seg [data-basis]').forEach(x => x.setAttribute('aria-pressed', String(x.dataset.basis === basis)));
  }
  root.querySelectorAll('.seg [data-basis]').forEach(function (b) {
    b.addEventListener('click', function () {
      basis = b.dataset.basis;
      pressBasis();
      render();
    });
  });
  root.querySelectorAll('.seg.cols').forEach(function (seg) {
    seg.querySelectorAll('button').forEach(function (b) {
      b.addEventListener('click', function () {
        colMode[seg.dataset.for] = b.dataset.cols;
        seg.querySelectorAll('button').forEach(x => x.setAttribute('aria-pressed', String(x === b)));
        render();
      });
    });
  });
  el('btn-reset').addEventListener('click', function () {
    Object.keys(MS).forEach(k => MS[k].clear());
    refreshAccounts();
    refreshLaneAccounts();
    basis = 'delivered';
    pressBasis();
    Object.keys(colMode).forEach(k => { colMode[k] = 'both'; });
    root.querySelectorAll('.seg.cols').forEach(seg => seg.querySelectorAll('button')
      .forEach(x => x.setAttribute('aria-pressed', String(x.dataset.cols === 'both'))));
    applyPreset('l7');
  });

  /* lane CSV: every lane (not just the 60 shown), in the basis on screen */
  function buildCsv() {
    const head = ['lane','delivered','under_d1','under_d2','under_d3plus','under_total','day_0',
      'breach_d1','breach_d2','breach_d3plus','breach_total','na',
      'under_d1_pct','under_d2_pct','under_d3plus_pct','under_pct','day_0_pct',
      'breach_d1_pct','breach_d2_pct','breach_d3plus_pct','breach_pct','na_pct',
      'avg_p2d_days','avg_edd_days','basis'];
    const f = v => v === null ? '' : v.toFixed(2);
    const lines = [head.join(',')];
    Array.from(lastLaneMap.entries()).sort((a,b)=>b[1].total-a[1].total).forEach(function (e) {
      const r = view(e[1]);
      lines.push(['"' + e[0].replace(/"/g,'""') + '"', r.total, r.uD1,r.uD2,r.uD3,r.under, r.day0,
        r.bD1,r.bD2,r.bD3,r.breach, r.na,
        f(r.uD1Pct),f(r.uD2Pct),f(r.uD3Pct),f(r.underPct),f(r.day0Pct),
        f(r.bD1Pct),f(r.bD2Pct),f(r.bD3Pct),f(r.breachPct),f(r.naPct),
        f(r.avgP2D),f(r.avgEDD), basis].join(','));
    });
    return lines.join(String.fromCharCode(10));
  }
  el('btn-csv').addEventListener('click', function () {
    if (!F.length) return;
    BX.saveCsv('lane_breach_' + basis + '_' + el('f-from').value + '_to_' + el('f-to').value + '.csv', buildCsv());
  });

  function init(raw) {
    const preset = pressedPreset();
    const oldMin = DMIN, oldMax = DMAX;
    D = raw.dims || {};
    F = raw.facts || [];
    if (!F.length) return;
    const dates = D.date.slice().sort();
    DMIN = dates[0]; DMAX = dates[dates.length-1];
    el('dr-avail').textContent = 'Data available: ' + pretty(DMIN) + ' – ' + pretty(DMAX);
    slabVals = ['0-3kg','3+kg','unknown'].filter(s => D.slab.indexOf(s) !== -1);
    ptypeVals = PTYPE_ORDER.filter(s => (D.ptype||[]).indexOf(s) !== -1);
    makeFilters();
    // the first data opens on the last 7 days; new data keeps the range someone picked
    if (!oldMax || preset) applyPreset(preset || 'l7');
    else { BX.keepRange('f-from', 'f-to', oldMin, oldMax, DMIN, DMAX); render(); }
  }

  BX.tab({ panel: 'be-breach', dataset: 'breach', init: init, onShow: render });
})();
