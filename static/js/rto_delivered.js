(function () {
  "use strict";

  const PAGE_SIZE = 200;
  const POLL_MS = 4000;
  const NO_STATUS = "__none__"; // must match rto_delivered.NO_STATUS

  const $ = (id) => document.getElementById(id);
  const tbody = $("delivered-tbody");
  const emptyEl = $("delivered-empty");
  const countEl = $("delivered-count");
  const updatedEl = $("delivered-updated");
  const banner = $("banner-delivered");
  const moreBtn = $("btn-delivered-more");
  const refreshBtn = $("btn-delivered-refresh");
  const exportLink = $("btn-delivered-export");
  const searchInput = $("delivered-search");
  const returnNameSel = $("delivered-return-name");
  const statusSel = $("delivered-status");
  const dateFrom = $("delivered-date-from");
  const dateTo = $("delivered-date-to");

  // Status sits right after AWB so it is visible without scrolling the wide table sideways.
  const COLS = ["awb", "status", "courier_partner", "return_name", "rto_delivery_date", "created_at", "pickup_date", "latest_remark", "return_city"];

  // Delivery Note status -> badge colour. Anything unlisted gets the neutral grey badge.
  const STATUS_KIND = {
    "Return Completed": "synced",
    "Completed": "synced",
    "Return Issued": "pending",
    "Return Initiated": "pending",
    "To Bill": "pending",
    "AWB Registered": "pending",
    "Cancel": "cancelled",
    "AWB Cancelled": "cancelled",
  };

  const STATUS_ACCENT = {
    synced: "var(--success)",
    pending: "var(--warn)",
    cancelled: "var(--error)",
    neutral: "var(--chart-baseline)",
  };

  function makeStatusCell(status) {
    const td = document.createElement("td");
    if (!status) {
      td.textContent = "-";
      return td;
    }
    const badge = document.createElement("span");
    badge.className = "badge " + (STATUS_KIND[status] || "neutral");
    badge.textContent = status;
    td.appendChild(badge);
    return td;
  }

  let shown = 0;
  let total = 0;
  let pollTimer = null;
  let requestSeq = 0; // drop out-of-order responses when filters change quickly
  let returnNamesPopulated = false;

  function debounce(fn, wait) {
    let t;
    return (...a) => {
      clearTimeout(t);
      t = setTimeout(() => fn(...a), wait);
    };
  }

  function setBanner(message, kind) {
    banner.textContent = message || "";
    banner.className = message ? "banner " + kind : "banner hidden";
  }

  function filterParams() {
    const p = new URLSearchParams();
    if (dateFrom.value) p.set("from", dateFrom.value);
    if (dateTo.value) p.set("to", dateTo.value);
    if (returnNameSel.value) p.set("return_name", returnNameSel.value);
    if (statusSel.value) p.set("status", statusSel.value);
    if (searchInput.value.trim()) p.set("q", searchInput.value.trim());
    return p;
  }

  function appendRows(rows) {
    rows.forEach((row, i) => {
      const tr = document.createElement("tr");
      const num = document.createElement("td");
      num.textContent = shown + i + 1;
      tr.appendChild(num);
      COLS.forEach((c) => {
        if (c === "status") {
          tr.appendChild(makeStatusCell(row.status));
          return;
        }
        const td = document.createElement("td");
        td.textContent = row[c] || "-";
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    shown += rows.length;
  }

  function populateReturnNames(names) {
    const current = returnNameSel.value;
    returnNameSel.length = 1; // keep the "All return names" option
    names.forEach((n) => {
      const o = document.createElement("option");
      o.value = o.textContent = n;
      returnNameSel.appendChild(o);
    });
    returnNameSel.value = current;
    returnNamesPopulated = names.length > 0;
  }

  function statusLabel(st) {
    return st === NO_STATUS ? "(No status)" : st;
  }

  // Rebuilt on every load: only statuses that exist under the OTHER filters are offered,
  // each with its AWB count.
  function populateStatuses(counts) {
    const current = statusSel.value;
    statusSel.length = 1; // keep the "All statuses" option
    counts.forEach((c) => {
      const o = document.createElement("option");
      o.value = c.status;
      o.textContent = `${statusLabel(c.status)} (${c.count.toLocaleString()})`;
      statusSel.appendChild(o);
    });
    statusSel.value = current;
  }

  function formatUpdated(epoch) {
    return epoch ? "Data as of " + new Date(epoch * 1000).toLocaleString() : "";
  }

  // ---------- distinct-AWB KPIs ----------
  let summarySeq = 0;
  async function loadSummary() {
    const seq = ++summarySeq;
    const C = window.RtoCharts;
    if (!C) return;
    let d;
    try {
      const res = await fetch("/api/rto-delivered/summary?" + filterParams().toString());
      d = await res.json();
      if (!res.ok) throw new Error(d.error || `Request failed (${res.status})`);
    } catch (e) {
      return; // the rows request already surfaces errors in the banner
    }
    if (seq !== summarySeq) return;

    const kpis = $("delivered-kpis");
    kpis.textContent = "";
    const addTile = (label, value, accent, onClick, active) => {
      const tile = C.el("div", { class: "kpi-tile" + (onClick ? " clickable" : "") + (active ? " active" : ""), style: `border-left-color:${accent}` },
        C.el("div", { class: "kpi-label" }, label),
        C.el("div", { class: "kpi-value" }, value.toLocaleString()));
      if (onClick) {
        tile.setAttribute("role", "button");
        tile.tabIndex = 0;
        tile.addEventListener("click", onClick);
        tile.addEventListener("keydown", (e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onClick();
          }
        });
      }
      kpis.appendChild(tile);
    };

    addTile("Distinct AWBs", d.distinct_awb, "var(--chart-series-1)");
    addTile("Rows", d.rows, "var(--chart-baseline)");
    addTile("Return names", d.return_names, "var(--chart-series-2)");

    // One tile per status (AWB count under the other filters). Clicking toggles the Status filter.
    d.status_counts.forEach((c) => {
      const active = statusSel.value === c.status;
      addTile(statusLabel(c.status), c.count, STATUS_ACCENT[STATUS_KIND[c.status] || "neutral"], () => {
        statusSel.value = active ? "" : c.status;
        load(false);
      }, active);
    });
  }

  async function load(append) {
    const seq = ++requestSeq;
    const p = filterParams();
    p.set("offset", append ? shown : 0);
    p.set("limit", PAGE_SIZE);
    if (!append) loadSummary();
    exportLink.href = "/api/rto-delivered/export?" + filterParams().toString();

    let data;
    try {
      const res = await fetch("/api/rto-delivered?" + p.toString());
      data = await res.json();
      if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
    } catch (e) {
      if (seq === requestSeq) setBanner(e.message, "error");
      return;
    }
    if (seq !== requestSeq) return;

    if (!append) {
      tbody.textContent = "";
      shown = 0;
    }
    appendRows(data.rows);
    total = data.total;

    if (!returnNamesPopulated || data.return_names.length !== returnNameSel.length - 1) {
      populateReturnNames(data.return_names);
    }

    populateStatuses(data.status_counts);

    const filteredNote = total === data.cached_total ? "" : ` of ${data.cached_total}`;
    countEl.textContent = `${total}${filteredNote} rows`;
    updatedEl.textContent = formatUpdated(data.fetched_at);
    emptyEl.classList.toggle("hidden", total !== 0 || data.loading);
    moreBtn.classList.toggle("hidden", shown >= total);
    refreshBtn.disabled = data.refreshing;
    refreshBtn.textContent = data.refreshing ? "Refreshing…" : "Refresh from DB";

    if (data.loading) {
      setBanner("Loading RTO Delivered data from the database for the first time (about a minute). It will be cached after this.", "info");
    } else if (data.error && !data.refreshing) {
      setBanner("Last refresh failed (" + data.error + "). Showing previously cached data.", "error");
    } else {
      setBanner("", "");
    }

    // Keep polling while a fetch is in flight, so the table swaps in fresh data on its own.
    clearTimeout(pollTimer);
    if (data.loading || data.refreshing) {
      pollTimer = setTimeout(() => load(false), POLL_MS);
    }
  }

  const reload = debounce(() => load(false), 250);
  [searchInput].forEach((el) => el.addEventListener("input", reload));
  [returnNameSel, statusSel, dateFrom, dateTo].forEach((el) => el.addEventListener("change", () => load(false)));
  moreBtn.addEventListener("click", () => load(true));

  $("btn-delivered-clear").addEventListener("click", () => {
    searchInput.value = "";
    returnNameSel.value = "";
    statusSel.value = "";
    dateFrom.value = "";
    dateTo.value = "";
    load(false);
  });

  refreshBtn.addEventListener("click", async () => {
    refreshBtn.disabled = true;
    try {
      await fetch("/api/rto-delivered/refresh", { method: "POST" });
    } catch (e) {
      /* the next load() reports state either way */
    }
    load(false);
  });

  // Load the first time the RTO Status panel is opened (not on page load) so
  // it costs nothing for users who never look at it.
  let loadedOnce = false;
  document.querySelector('.side-nav-item[data-panel="rto-status"]').addEventListener("click", () => {
    if (!loadedOnce) {
      loadedOnce = true;
      load(false);
    }
  });
  // New data from the database (the daily refresh, or the top bar's Refresh from DB) - see data_refresh.js.
  window.addEventListener("panel:data-changed", (e) => {
    if (loadedOnce && e.detail.sources.includes("rto_delivered")) load(false);
  });
})();
