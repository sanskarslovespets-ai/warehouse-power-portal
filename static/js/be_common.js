(function () {
  "use strict";

  // Shared by the Breach Explorer tabs (static/js/be_*.js), each a port of one tab of the SLA Breach
  // Explorer's page (breach_explorer.py says where from): its multi-select filter, CSV export, and
  // BX.tab(), which loads a tab's data from /api/breach-explorer/<dataset> the first time it is opened.
  //
  // Element ids in these panels all start "bx-"; BX.el("t-day") finds #bx-t-day, so the ported code can
  // keep the original page's ids.

  const el = (id) => document.getElementById("bx-" + id);
  const fmt = (n) => n.toLocaleString("en-IN");
  const uniq = (a) => Array.from(new Set(a)).sort((x, y) => x.localeCompare(y));
  const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const pretty = (s) => { const q = s.split("-"); return (+q[2]) + " " + MON[+q[1] - 1] + " " + q[0]; };

  function when(epoch) {
    return new Date(epoch * 1000).toLocaleString(undefined, { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" });
  }

  // ---------- multi-select popover ----------
  // One per filter, kept in the tab's own `registry` under `key`. Made again when new data comes in: the
  // values it had ticked that still exist stay ticked.
  function makeMS(registry, hostId, key, label, values, onChange) {
    const host = el(hostId);
    const prev = registry[key];
    const state = { sel: new Set(prev ? Array.from(prev.sel).filter((v) => values.indexOf(v) !== -1) : []), values, onChange };
    registry[key] = state;
    host.innerHTML =
      '<button class="ms-btn" type="button" aria-haspopup="true" aria-expanded="false">' +
        '<span class="k">' + esc(label) + '</span><span class="v">All</span><span class="caret">▾</span>' +
      "</button>" +
      '<div class="ms-pop" role="dialog" aria-label="' + esc(label) + ' filter">' +
        (values.length > 12 ? '<input class="ms-search" type="text" placeholder="Search…" aria-label="Search ' + esc(label) + '">' : "") +
        '<div class="ms-list"></div>' +
        '<div class="ms-foot"><button type="button" data-a="all">Select all</button><button type="button" data-a="none">Clear</button></div>' +
      "</div>";
    const btn = host.querySelector(".ms-btn");
    const pop = host.querySelector(".ms-pop");
    const list = host.querySelector(".ms-list");
    const search = host.querySelector(".ms-search");

    function paint(q) {
      q = (q || "").toLowerCase();
      const vis = q ? state.values.filter((v) => v.toLowerCase().includes(q)) : state.values;
      list.innerHTML = vis.slice(0, 400).map((v) =>
        '<label class="ms-opt"><input type="checkbox" value="' + esc(v) + '"' + (state.sel.has(v) ? " checked" : "") + "><span>" + esc(v) + "</span></label>"
      ).join("") || '<div class="ms-none">No matches</div>';
      list.querySelectorAll("input").forEach((cb) => cb.addEventListener("change", () => {
        if (cb.checked) state.sel.add(cb.value); else state.sel.delete(cb.value);
        sync(); if (state.onChange) state.onChange();
      }));
    }
    function sync() {
      const n = state.sel.size;
      btn.querySelector(".v").textContent = n === 0 ? "All" : (n === 1 ? Array.from(state.sel)[0] : n + " selected");
      btn.classList.toggle("on", n > 0);
    }
    state.repaint = () => { paint(search ? search.value : ""); sync(); };
    state.setValues = (vals) => {
      state.values = vals;
      state.sel = new Set(Array.from(state.sel).filter((v) => vals.indexOf(v) !== -1));
      state.repaint();
    };
    state.set = (arr) => { state.sel = new Set(arr); state.repaint(); };
    state.clear = () => { state.sel.clear(); state.repaint(); };

    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const wasOpen = host.classList.contains("open");
      closePopovers();
      if (!wasOpen) { host.classList.add("open"); btn.setAttribute("aria-expanded", "true"); if (search) search.focus(); }
    });
    pop.addEventListener("click", (e) => e.stopPropagation());
    if (search) search.addEventListener("input", () => paint(search.value));
    host.querySelectorAll(".ms-foot button").forEach((b) => b.addEventListener("click", () => {
      const q = search ? search.value.toLowerCase() : "";
      const vis = q ? state.values.filter((v) => v.toLowerCase().includes(q)) : state.values;
      if (b.dataset.a === "all") vis.forEach((v) => state.sel.add(v)); else vis.forEach((v) => state.sel.delete(v));
      state.repaint(); if (state.onChange) state.onChange();
    }));
    paint(""); sync();
    return state;
  }

  function closePopovers() {
    document.querySelectorAll(".bx .ms.open").forEach((m) => {
      m.classList.remove("open");
      const b = m.querySelector(".ms-btn, .range");
      if (b) b.setAttribute("aria-expanded", "false");
    });
  }
  document.addEventListener("click", closePopovers);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePopovers(); });

  // A tab's own date pickers, after new data: still on the old data's first/last day (untouched) -> the new
  // data's; anything else is someone's choice and stays.
  function keepRange(fromId, toId, oldMin, oldMax, newMin, newMax) {
    const from = el(fromId), to = el(toId);
    if (!from.value || from.value === oldMin) from.value = newMin;
    if (!to.value || to.value === oldMax) to.value = newMax;
  }

  // ---------- CSV ----------
  function saveCsv(filename, csv) {
    const blob = new Blob([String.fromCharCode(65279) + csv], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  // A table as shown on screen (filters, sort and all), as CSV.
  function tableCsv(id) {
    const t = el(id), NL = String.fromCharCode(10);
    const q = (v) => {
      v = (v === null || v === undefined) ? "" : String(v).trim();
      if (v === "·") v = "";
      // Counts are shown en-IN style ("1,27,112"): without the commas a spreadsheet reads them as
      // numbers. Percentages keep their % (spreadsheets parse those); labels are untouched.
      if (/^-?[0-9,]+([.][0-9]+)?$/.test(v)) v = v.replace(/,/g, "");
      if (/^[=+\-@\t\r]/.test(v) && !/^-?[0-9.]+%?$/.test(v)) v = "'" + v; // a label a spreadsheet would run as a formula
      return /[",\r\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; // a bare \r would start a new cell
    };
    const out = [];
    t.querySelectorAll("thead tr").forEach((tr) => {
      out.push([].map.call(tr.querySelectorAll("th"), (c) => q(c.textContent)).join(","));
    });
    t.querySelectorAll("tbody tr, tfoot tr").forEach((tr) => {
      out.push([].map.call(tr.querySelectorAll("td"), (c) => q(c.textContent)).join(","));
    });
    return out.join(NL);
  }

  function flash(btn, text, failed) {
    const label = btn.dataset.label || btn.textContent;
    btn.dataset.label = label;
    btn.textContent = text;
    btn.classList.toggle("failed", !!failed);
    setTimeout(() => { btn.textContent = label; btn.classList.remove("failed"); }, failed ? 4000 : 2000);
  }

  // Connection view / RAD to OFD: the tab holds aggregates only, so one row per shipment comes from the
  // server (main.breach_explorer_awbs) for the tab's dates and filters.
  async function exportAwbs(dataset, from, to, sel, btn, banner) {
    const q = new URLSearchParams();
    q.set("from", from);
    q.set("to", to);
    Object.keys(sel || {}).forEach((k) => (sel[k] || []).forEach((v) => q.append(k, v)));
    const label = btn.textContent;
    btn.textContent = "Preparing…";
    btn.disabled = true;
    if (banner) banner.className = "banner hidden bx-banner";
    try {
      const res = await fetch("/api/breach-explorer/" + dataset + "/awbs?" + q.toString());
      if (!res.ok) {
        let msg = "Export failed (" + res.status + ").";
        try { msg = (await res.json()).error || msg; } catch (e) { /* no body */ }
        throw new Error(msg);
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = dataset + "_awbs_" + from + "_to_" + to + ".csv";
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      btn.textContent = label;
    } catch (e) {
      btn.textContent = label;
      if (banner) { banner.textContent = e.message; banner.className = "banner error bx-banner"; }
      flash(btn, "Export failed", true);
    } finally {
      btn.disabled = false;
    }
  }

  // ---------- each tab's data ----------
  // BX.tab({panel, dataset, init, onShow}): loads /api/breach-explorer/<dataset> the first time the panel is
  // opened, then init(data). Until there is data it says why: still loading, the first export still running
  // (and follows it), or the export failed. When new data comes in (data_refresh.js fires
  // "panel:data-changed" for breach_explorer) it asks again with the ETag it holds: an empty 304 when this
  // tab's data didn't change, otherwise the new data, and init() runs again with it.
  //
  // Refresh data (editors and admins; the original page's button) has the server re-run this tab's export
  // (main.breach_explorer_refresh), and the tab follows it the same way until the new data is in.
  const STATUS_POLL_MS = 8000;
  const FOLLOW_POLL_MS = 4000; // while this tab's own export is running or waiting its turn
  const NOTICE_MS = 6000;
  const TAB_NAMES = {
    breach: "Breach analysis", pendency: "Pendency", "pendency-detail": "Pendency", complaints: "Complaints",
    rto: "RTO", connection: "Connection view", radofd: "RAD to OFD",
    warehouse: "Warehouse Exceptions", // static/js/wh_exceptions.js, under Warehouse Dashboards
  };

  function tab(opts) {
    const root = document.getElementById("panel-" + opts.panel);
    const stateEl = root.querySelector(".bx-state");
    const body = root.querySelector(".bx-body");
    const banner = root.querySelector(".bx-banner");
    const fresh = root.querySelector(".bx-freshness");
    const refreshBtn = root.querySelector(".bx-refresh");
    const refreshLabel = refreshBtn.querySelector(".bx-refresh-label");
    let opened = false;
    let etag = null; // the data shown; null until there is some
    let shown = null; // the status last shown
    let busy = false;
    let again = false;
    let pollTimer = null;
    let noticeTimer = null;

    function showState(kind, title, text) {
      stateEl.className = "bx-state " + kind;
      stateEl.innerHTML = '<span class="bx-spinner" aria-hidden="true"></span><strong></strong><p></p>';
      stateEl.querySelector("strong").textContent = title;
      stateEl.querySelector("p").textContent = text || "";
      stateEl.hidden = false;
    }

    // Where this tab's export is in a status: "running", "queued" (others go first) or "". By tab name, so
    // Pendency's shipment list (pendency-detail) counts as Pendency's.
    function progress(st) {
      const ours = (d) => TAB_NAMES[d] === TAB_NAMES[opts.dataset];
      if (!st) return "";
      if (st.running && ours(st.running)) return "running";
      return (st.queued || []).some(ours) ? "queued" : "";
    }

    function showFreshness(st) {
      shown = st;
      clearTimeout(noticeTimer);
      const doing = progress(st);
      refreshBtn.disabled = !!doing;
      refreshLabel.textContent = doing ? "Refreshing…" : "Refresh data";
      const mine = st && st.datasets[opts.dataset];
      fresh.classList.remove("busy", "stale", "notice");
      banner.className = "banner hidden bx-banner";
      if (!mine || !mine.fetched_at) { fresh.textContent = ""; return; }
      const from = when(mine.fetched_at);
      if (doing) {
        fresh.textContent = (doing === "running" ? "Refreshing" : st.running ? "Refresh queued after " + TAB_NAMES[st.running] : "Refresh starting") +
          "… showing data from " + from;
        fresh.classList.add("busy");
      } else {
        fresh.textContent = "Data as of " + from;
      }
      if (mine.error && !doing) {
        fresh.classList.add("stale");
        banner.textContent = "The latest refresh of this tab failed (" + mine.error + "), so it shows the data from " + from +
          ". It tries again automatically" + (document.body.dataset.role === "viewer" ? "." : ", or press Refresh data to try now.");
        banner.className = "banner error bx-banner";
      }
    }

    // Refresh data's answer when it can't start (say the data is only minutes old), for a few seconds in place of
    // the freshness line.
    function notice(text) {
      showFreshness(shown);
      fresh.classList.remove("busy");
      fresh.classList.add("notice");
      fresh.textContent = text;
      noticeTimer = setTimeout(() => showFreshness(shown), NOTICE_MS);
    }

    async function status() {
      try {
        const res = await fetch("/api/breach-explorer/status");
        return res.ok ? await res.json() : null;
      } catch (e) {
        return null;
      }
    }

    function follow(ms) {
      clearTimeout(pollTimer);
      pollTimer = setTimeout(load, ms);
    }

    async function load() {
      if (busy) { again = true; return; }
      busy = true;
      clearTimeout(pollTimer);
      let next = 0; // when to look again (ms), if this tab is waiting on an export
      try {
        // The status first: once it says this tab's export is done, the data asked for after it is the new data.
        const st = (await status()) || shown;
        const res = await fetch("/api/breach-explorer/" + opts.dataset, { headers: etag ? { "If-None-Match": etag } : {} });
        showFreshness(st);
        if (progress(st)) next = FOLLOW_POLL_MS;
        if (res.ok) {
          const data = await res.json();
          stateEl.hidden = true;
          body.hidden = false;
          opts.init(data);
          etag = res.headers.get("ETag");
          return;
        }
        if (res.status === 304 || etag) return; // unchanged, or keep showing what we have
        let msg = "";
        try { msg = (await res.json()).error || ""; } catch (e) { /* no body */ }
        const mine = st && st.datasets[opts.dataset];
        if (res.status === 503) next = next || STATUS_POLL_MS; // no data yet: follow the export that builds it
        if (res.status === 503 && st && st.refreshing) {
          // Asking for the data just moved this tab's export to the front of the queue (breach_explorer.hurry), so
          // unless it is the one running, it goes straight after that one.
          const running = st.running && TAB_NAMES[st.running];
          const working = progress(st) === "running" ? " Working on it now."
            : running ? " It's next, as soon as " + running + " finishes" +
              (st.running_for ? " (" + Math.max(1, Math.round(st.running_for / 60)) + " min in so far)." : ".") : "";
          showState("building", "Building this tab's data",
            "It comes from the database the first time, which takes a few minutes. This tab fills in by itself when it's ready." + working);
        } else if (res.status === 503 && mine && mine.error) {
          showState("error", "This tab's data couldn't be built", mine.error + " It tries again automatically within half an hour" +
            (document.body.dataset.role === "viewer" ? "." : ", or press Refresh data to try now."));
        } else if (res.status === 503) {
          showState("building", "Waiting for this tab's data", "The export that builds it is about to start.");
        } else {
          showState("error", "Couldn't load this tab", msg || "Request failed (" + res.status + ").");
        }
      } catch (e) {
        if (!etag) showState("error", "Couldn't load this tab", "Couldn't reach the server. Refresh the page to try again.");
      } finally {
        busy = false;
        if (again) { again = false; load(); }
        else if (next) follow(next);
      }
    }

    refreshBtn.addEventListener("click", async () => {
      refreshBtn.disabled = true;
      refreshLabel.textContent = "Starting…";
      let res = null;
      let data = {};
      try {
        res = await fetch("/api/breach-explorer/" + opts.dataset + "/refresh", { method: "POST" });
        data = await res.json();
      } catch (e) {
        /* no answer, or not JSON: said below */
      }
      if (!res || !res.ok) {
        notice(data.error || (res ? "Couldn't start the refresh (" + res.status + ")." : "Couldn't reach the server. Please try again."));
        return;
      }
      window.dispatchEvent(new CustomEvent("panel:refresh-started")); // the top bar shows it too (data_refresh.js)
      showFreshness(data);
      follow(FOLLOW_POLL_MS);
    });

    document.querySelector('.side-nav-item[data-panel="' + opts.panel + '"]').addEventListener("click", () => {
      if (!opened) {
        opened = true;
        showState("loading", "Loading…", "");
        load();
      } else if (etag && opts.onShow) {
        opts.onShow();
      }
    });
    window.addEventListener("panel:data-changed", (e) => {
      if (opened && e.detail.sources.includes("breach_explorer")) load();
    });
  }

  window.BX = { el, fmt, uniq, esc, pretty, makeMS, closePopovers, keepRange, saveCsv, tableCsv, exportAwbs, flash, tab };
})();
