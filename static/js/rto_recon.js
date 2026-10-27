(function () {
  "use strict";

  const PAGE_SIZE = 200;
  const POLL_MS = 4000;
  const COLS = ["clickpost_unified_status", "courier_partner", "awb", "return_name", "created_at", "pickup_date", "reference_number", "pickup_pincode", "drop_pincode", "rto_mark_date", "rto_delivery_date", "rto_date_diff", "erp_status"];

  const $ = (id) => document.getElementById(id);
  const tbody = $("recon-tbody");
  const emptyEl = $("recon-empty");
  const countEl = $("recon-count");
  const updatedEl = $("recon-updated");
  const banner = $("banner-recon");
  const moreBtn = $("btn-recon-more");
  const refreshBtn = $("btn-recon-refresh");
  const downloadLink = $("btn-recon-download");
  const searchInput = $("recon-search");
  const dateField = $("recon-date-field");
  const dateFrom = $("recon-date-from");
  const dateTo = $("recon-date-to");

  // Dropdown filters: query param (and key of the response's `options`) -> its <select>. Each is
  // rebuilt on every load with the values present and their AWB counts under the other filters.
  const SELECTS = {
    courier: $("recon-courier"),
    return_name: $("recon-return-name"),
    erp_status: $("recon-erp-status"),
    age: $("recon-age"),
  };

  // Delivery Note status -> badge colour (same mapping as the RTO Status tab); unlisted = neutral grey.
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
  let requestSeq = 0; // drop out-of-order responses when the filter changes quickly

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
    if (searchInput.value.trim()) p.set("q", searchInput.value.trim());
    Object.entries(SELECTS).forEach(([param, sel]) => {
      if (sel.value) p.set(param, sel.value);
    });
    if (dateField.value !== "rto_mark_date") p.set("date_field", dateField.value);
    if (dateFrom.value) p.set("from", dateFrom.value);
    if (dateTo.value) p.set("to", dateTo.value);
    return p;
  }

  function populateSelects(options) {
    Object.entries(SELECTS).forEach(([param, sel]) => {
      const current = sel.value;
      sel.length = 1; // keep the "All ..." option
      options[param].forEach((o) => {
        const opt = document.createElement("option");
        opt.value = o.value;
        opt.textContent = `${param === "age" ? "Pending " + o.value : o.value} (${o.count.toLocaleString()})`;
        sel.appendChild(opt);
      });
      sel.value = current;
    });
  }

  function appendRows(rows) {
    rows.forEach((row, i) => {
      const tr = document.createElement("tr");
      const num = document.createElement("td");
      num.textContent = shown + i + 1;
      tr.appendChild(num);
      COLS.forEach((c) => {
        if (c === "erp_status") {
          tr.appendChild(makeStatusCell(row.erp_status));
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

  async function load(append) {
    const seq = ++requestSeq;
    const p = filterParams();
    p.set("offset", append ? shown : 0);
    p.set("limit", PAGE_SIZE);
    downloadLink.href = "/api/rto-recon/export?" + filterParams().toString();

    let data;
    try {
      const res = await fetch("/api/rto-recon?" + p.toString());
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
    populateSelects(data.options);

    const filteredNote = total === data.cached_total ? "" : ` of ${data.cached_total}`;
    countEl.textContent = `${total}${filteredNote} rows`;
    updatedEl.textContent = data.fetched_at ? "Data as of " + new Date(data.fetched_at * 1000).toLocaleString() : "";
    emptyEl.classList.toggle("hidden", total !== 0 || data.loading);
    moreBtn.classList.toggle("hidden", shown >= total);
    refreshBtn.disabled = data.refreshing;
    refreshBtn.textContent = data.refreshing ? "Refreshing…" : "Refresh from DB";

    if (data.loading) {
      setBanner("Loading RTO Reconciliation data from the database for the first time. It will be cached after this.", "info");
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

  searchInput.addEventListener("input", debounce(() => load(false), 250));
  [...Object.values(SELECTS), dateField, dateFrom, dateTo].forEach((el) => el.addEventListener("change", () => load(false)));
  moreBtn.addEventListener("click", () => load(true));

  $("btn-recon-clear").addEventListener("click", () => {
    searchInput.value = "";
    Object.values(SELECTS).forEach((sel) => (sel.value = ""));
    dateField.value = "rto_mark_date";
    dateFrom.value = "";
    dateTo.value = "";
    load(false);
  });

  refreshBtn.addEventListener("click", async () => {
    refreshBtn.disabled = true;
    try {
      await fetch("/api/rto-recon/refresh", { method: "POST" });
    } catch (e) {
      /* the next load() reports state either way */
    }
    load(false);
  });

  // Load the first time the panel is opened (not on page load).
  let loadedOnce = false;
  document.querySelector('.side-nav-item[data-panel="rto-recon"]').addEventListener("click", () => {
    if (!loadedOnce) {
      loadedOnce = true;
      load(false);
    }
  });
  // New data from the database (the daily refresh, or the top bar's Refresh from DB) - see data_refresh.js.
  window.addEventListener("panel:data-changed", (e) => {
    if (loadedOnce && e.detail.sources.includes("rto_recon")) load(false);
  });
})();
