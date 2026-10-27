(function () {
  "use strict";

  const PAGE_SIZE = 500;
  const POLL_MS = 4000;
  const COLS = ["awb", "received_on", "marked_on", "days_to_receipt", "courier", "warehouse", "order_id", "drop_city", "rto_reason", "net_value"];

  // Input columns. `options` makes a dropdown, otherwise a text / date box.
  // Dropdown choices can be edited here.
  const INPUTS = [
    { key: "received_at_wh", options: ["Yes", "No", "Received and Damaged"] },
    { key: "received_date", type: "date" },
    { key: "condition", options: ["Good", "Damaged"] },
    { key: "discrepancy_notes", type: "text", placeholder: "Notes" },
    { key: "checked_by", type: "text", placeholder: "Name" },
    { key: "date_updated", type: "date" },
  ];

  const $ = (id) => document.getElementById(id);
  const tabsEl = $("mdnd-tabs");
  const tbody = $("mdnd-tbody");
  const emptyEl = $("mdnd-empty");
  const countEl = $("mdnd-count");
  const updatedEl = $("mdnd-updated");
  const banner = $("banner-mdnd");
  const filledSel = $("mdnd-filled");

  // Dropdown filters: element -> the row field it matches. Options are the distinct values
  // present in the active tab (plus "(Blank)"), rebuilt on every load.
  const BLANK = "__blank__";
  const SELECT_FILTERS = [
    { el: $("mdnd-f-courier"), key: "courier", all: "All couriers" },
    { el: $("mdnd-f-reason"), key: "rto_reason", all: "All reasons" },
    { el: $("mdnd-f-received_at_wh"), key: "received_at_wh", all: "Received at WH? (all)" },
    { el: $("mdnd-f-condition"), key: "condition", all: "All conditions" },
  ];
  // Date-range filters (inclusive): rows with no date drop out once a bound is set.
  const DATE_FILTERS = [
    { from: $("mdnd-f-received-from"), to: $("mdnd-f-received-to"), key: "received_date" },
    { from: $("mdnd-f-updated-from"), to: $("mdnd-f-updated-to"), key: "date_updated" },
  ];
  const moreBtn = $("btn-mdnd-more");
  const refreshBtn = $("btn-mdnd-refresh");

  let activeTab = null;
  let tabRows = []; // every row of the active tab
  let rows = []; // tabRows after the Filled / Not filled filter
  let shown = 0;
  let pollTimer = null;
  let loading = false;
  let requestSeq = 0; // drop out-of-order responses when tabs are switched quickly

  function setBanner(message, kind) {
    banner.textContent = message || "";
    banner.className = message ? "banner " + kind : "banner hidden";
  }

  function renderTabs(tabs) {
    tabsEl.textContent = "";
    tabs.forEach((t) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "tab-btn" + (t.tab === activeTab ? " active" : "");
      btn.setAttribute("role", "tab");
      btn.setAttribute("aria-selected", String(t.tab === activeTab));
      btn.textContent = `${t.tab} (${t.count.toLocaleString()})`;
      btn.addEventListener("click", () => {
        if (t.tab === activeTab) return;
        activeTab = t.tab;
        load();
      });
      tabsEl.appendChild(btn);
    });
  }

  // An entry counts as filled once its "Received at WH?" has a value (typed in, or auto-filled
  // because the AWB is in the RTO Process form).
  const isFilled = (row) => !!row.received_at_wh;

  // Rebuilds the table from tabRows. Called on load and when the filter changes - not after a
  // save, so a row you just filled stays put in the "Not filled" view until you re-apply it.
  function renderRows() {
    const filledCount = tabRows.filter(isFilled).length;
    filledSel.options[1].textContent = `Not filled (${(tabRows.length - filledCount).toLocaleString()})`;
    filledSel.options[2].textContent = `Filled (${filledCount.toLocaleString()})`;

    const want = filledSel.value;
    rows = tabRows.filter((r) => {
      if (want && isFilled(r) !== (want === "filled")) return false;
      for (const f of SELECT_FILTERS) {
        const v = f.el.value;
        if (v && (r[f.key] || BLANK) !== v) return false;
      }
      for (const f of DATE_FILTERS) {
        const d = (r[f.key] || "").slice(0, 10);
        if ((f.from.value || f.to.value) && (!d || (f.from.value && d < f.from.value) || (f.to.value && d > f.to.value))) return false;
      }
      return true;
    });
    shown = 0;
    tbody.textContent = "";
    appendRows();
    countEl.textContent = rows.length === tabRows.length ? `${rows.length.toLocaleString()} rows` : `${rows.length.toLocaleString()} of ${tabRows.length.toLocaleString()} rows`;
    emptyEl.classList.toggle("hidden", rows.length !== 0 || loading);
  }

  function populateFilters() {
    SELECT_FILTERS.forEach((f) => {
      const current = f.el.value;
      const values = new Set(tabRows.map((r) => r[f.key] || BLANK));
      const sorted = [...values].filter((v) => v !== BLANK).sort((a, b) => a.localeCompare(b));
      if (values.has(BLANK)) sorted.push(BLANK);
      f.el.length = 1; // keep the "All ..." option
      sorted.forEach((v) => {
        const o = document.createElement("option");
        o.value = v;
        o.textContent = v === BLANK ? "(Blank)" : v;
        f.el.appendChild(o);
      });
      f.el.value = values.has(current) ? current : ""; // a value absent from this tab resets to "All"
    });
  }

  function todayISO() {
    const d = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  }

  function makeInputCell(row, spec, controls) {
    const td = document.createElement("td");
    const locked = row.locked && (spec.key === "received_at_wh" || spec.key === "received_date");
    let el;
    if (spec.options) {
      el = document.createElement("select");
      el.className = "dimension-select mdnd-input";
      const opts = spec.options.slice();
      if (row[spec.key] && !opts.includes(row[spec.key])) opts.push(row[spec.key]);
      ["", ...opts].forEach((o) => {
        const opt = document.createElement("option");
        opt.value = o;
        opt.textContent = o || "Select";
        el.appendChild(opt);
      });
    } else {
      el = document.createElement("input");
      el.type = spec.type;
      el.className = "mdnd-input";
      if (spec.placeholder) el.placeholder = spec.placeholder;
    }
    el.value = row[spec.key] || "";
    if (locked) {
      el.disabled = true;
      el.title = "Auto-filled: this AWB is in the RTO Process form";
    }
    controls[spec.key] = el;
    el.addEventListener("change", () => save(row, controls, spec.key, el));
    td.appendChild(el);
    return td;
  }

  async function save(row, controls, changedKey, el) {
    // Editing any field stamps "Date Updated" with today (still editable by hand).
    if (changedKey !== "date_updated") controls.date_updated.value = todayISO();
    const body = { awb: row.awb };
    INPUTS.forEach((s) => (body[s.key] = controls[s.key].value));
    el.classList.remove("saved", "failed");
    try {
      const res = await fetch("/api/rto-mdnd/inputs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
      INPUTS.forEach((s) => (row[s.key] = controls[s.key].value)); // keep the in-memory row in step
      el.classList.add("saved");
      setBanner("", "");
    } catch (e) {
      el.classList.add("failed");
      setBanner("Could not save: " + e.message, "error");
    }
  }

  function appendRows() {
    const slice = rows.slice(shown, shown + PAGE_SIZE);
    slice.forEach((row, i) => {
      const tr = document.createElement("tr");
      const num = document.createElement("td");
      num.textContent = shown + i + 1;
      tr.appendChild(num);
      COLS.forEach((c) => {
        const td = document.createElement("td");
        const v = row[c];
        td.textContent = v === "" || v === undefined || v === null ? "-" : c === "net_value" ? Number(v).toLocaleString() : v;
        tr.appendChild(td);
      });
      const controls = {};
      INPUTS.forEach((spec) => tr.appendChild(makeInputCell(row, spec, controls)));
      tbody.appendChild(tr);
    });
    shown += slice.length;
    moreBtn.classList.toggle("hidden", shown >= rows.length);
  }

  async function load() {
    const seq = ++requestSeq;
    let data;
    try {
      const res = await fetch("/api/rto-mdnd?tab=" + encodeURIComponent(activeTab || ""));
      data = await res.json();
      if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
    } catch (e) {
      if (seq === requestSeq) setBanner(e.message, "error");
      return;
    }
    if (seq !== requestSeq) return;

    if (!activeTab) activeTab = data.tabs[0].tab;
    renderTabs(data.tabs);

    tabRows = data.rows;
    loading = data.loading;
    populateFilters();
    renderRows();

    updatedEl.textContent = data.fetched_at ? "Data as of " + new Date(data.fetched_at * 1000).toLocaleString() : "";
    refreshBtn.disabled = data.refreshing;
    refreshBtn.textContent = data.refreshing ? "Refreshing…" : "Refresh from DB";

    if (data.loading) {
      setBanner("Loading RTO MDND data from the database for the first time (can take several minutes). It will be cached after this.", "info");
    } else if (data.error && !data.refreshing) {
      setBanner("Last refresh failed (" + data.error + "). Showing previously cached data.", "error");
    } else {
      setBanner("", "");
    }

    // Keep polling while a fetch is in flight, so the table swaps in fresh data on its own.
    clearTimeout(pollTimer);
    if (data.loading || data.refreshing) {
      pollTimer = setTimeout(load, POLL_MS);
    }
  }

  moreBtn.addEventListener("click", appendRows);
  // Every filtered row of the active tab, not just the ones rendered so far; `rows` already carries
  // whatever has been typed in since the tab loaded.
  $("btn-mdnd-download").addEventListener("click", () =>
    RtoCsv.download("rto_mdnd_" + (activeTab || "all").toLowerCase(), COLS.concat(INPUTS.map((s) => s.key)), rows));
  [filledSel, ...SELECT_FILTERS.map((f) => f.el), ...DATE_FILTERS.flatMap((f) => [f.from, f.to])].forEach((el) => el.addEventListener("change", renderRows));

  $("btn-mdnd-clear").addEventListener("click", () => {
    filledSel.value = "";
    SELECT_FILTERS.forEach((f) => (f.el.value = ""));
    DATE_FILTERS.forEach((f) => (f.from.value = f.to.value = ""));
    renderRows();
  });

  refreshBtn.addEventListener("click", async () => {
    refreshBtn.disabled = true;
    try {
      await fetch("/api/rto-mdnd/refresh", { method: "POST" });
    } catch (e) {
      /* the next load() reports state either way */
    }
    load();
  });

  // Load the first time the panel is opened (not on page load).
  let loadedOnce = false;
  document.querySelector('.side-nav-item[data-panel="rto-mdnd"]').addEventListener("click", () => {
    if (!loadedOnce) {
      loadedOnce = true;
      load();
    }
  });
  // New data from the database (the daily refresh, or the top bar's Refresh from DB) - see data_refresh.js.
  // Not while someone is typing into the table: redrawing it would drop what they haven't saved yet.
  window.addEventListener("panel:data-changed", (e) => {
    if (loadedOnce && e.detail.sources.includes("rto_mdnd") && !tbody.contains(document.activeElement)) load();
  });
})();
