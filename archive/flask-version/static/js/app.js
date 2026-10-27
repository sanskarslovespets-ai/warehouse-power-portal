(function () {
  "use strict";

  // ---------- helpers ----------
  function debounce(fn, wait) {
    let t;
    return function (...args) {
      clearTimeout(t);
      t = setTimeout(() => fn.apply(this, args), wait);
    };
  }

  function showBanner(el, message, kind) {
    el.textContent = message;
    el.className = "banner " + kind;
  }

  function hideBanner(el) {
    el.className = "banner hidden";
  }

  async function fetchJson(url, options) {
    const res = await fetch(url, options);
    let data = null;
    try {
      data = await res.json();
    } catch (e) {
      /* no body */
    }
    if (!res.ok) {
      const msg = (data && data.error) || `Request failed (${res.status})`;
      throw new Error(msg);
    }
    return data;
  }

  // ---------- side nav (panels) ----------
  document.querySelectorAll(".side-nav-item").forEach((item) => {
    item.addEventListener("click", () => {
      document.querySelectorAll(".side-nav-item").forEach((i) => i.classList.remove("active"));
      item.classList.add("active");
      document.querySelectorAll(".side-panel").forEach((p) => p.classList.remove("active"));
      document.getElementById("panel-" + item.dataset.panel).classList.add("active");
    });
  });

  // ---------- sidebar hide/show toggle ----------
  const sidebarEl = document.getElementById("sidebar");
  const toggleSidebarBtn = document.getElementById("btn-toggle-sidebar");
  const SIDEBAR_HIDDEN_KEY = "rto-sidebar-hidden";

  function applySidebarVisibility(hidden) {
    sidebarEl.classList.toggle("collapsed", hidden);
    const label = hidden ? "Show sidebar" : "Hide sidebar";
    toggleSidebarBtn.setAttribute("aria-label", label);
    toggleSidebarBtn.setAttribute("aria-expanded", String(!hidden));
    toggleSidebarBtn.title = label;
  }

  let sidebarHidden = false;
  try {
    sidebarHidden = localStorage.getItem(SIDEBAR_HIDDEN_KEY) === "1";
  } catch (e) {
    /* private browsing / blocked storage - default to shown */
  }
  applySidebarVisibility(sidebarHidden);

  toggleSidebarBtn.addEventListener("click", () => {
    sidebarHidden = !sidebarHidden;
    applySidebarVisibility(sidebarHidden);
    try {
      localStorage.setItem(SIDEBAR_HIDDEN_KEY, sidebarHidden ? "1" : "0");
    } catch (e) {
      /* not persisted this session - toggle still works visually */
    }
  });

  // ---------- RTO Process panel ----------
  // Entries are (re)loaded each time the panel is opened.
  document.querySelector('.side-nav-item[data-panel="rto-process"]').addEventListener("click", () => {
    loadEntries();
  });

  // ---------- status banner ----------
  async function checkStatus() {
    try {
      const data = await fetchJson("/api/status");
      if (!data.google_connected) {
        const msg =
          "Google account is not connected yet, so POD/video uploads and sheet sync will fail. " +
          "Run `python3 google_services.py` once from the project folder to connect it.";
        showBanner(document.getElementById("banner-entries"), msg, "info");
      }
    } catch (e) {
      // status check failing isn't fatal; ignore
    }
  }

  // ---------- combo (searchable dropdown) ----------
  // Facility and Courier are small (a few thousand rows at most) and are
  // preloaded once from /api/dropdown-options, then filtered client-side.
  // SKU and AWB are huge (≈476K / ≈1.4M distinct values) - preloading those
  // would be slow to send and slow to filter in the browser, so they're
  // searched on the server instead, via /api/dropdown-search. That search is
  // still an instant in-memory lookup server-side (see dropdown_cache.py),
  // it just never ships the full list to the browser.
  const SERVER_SEARCH_FIELDS = new Set(["sku", "awb"]);
  const SEARCH_DEBOUNCE_MS = 200;
  const MAX_RENDERED_ITEMS = 200; // keep the dropdown DOM light even for a large filtered result

  const preloadedOptions = { facility: [], courier: [] };
  let optionsLoaded = false;

  async function ensureOptionsLoaded() {
    if (optionsLoaded) return;
    const data = await fetchJson("/api/dropdown-options");
    preloadedOptions.facility = data.facility || [];
    preloadedOptions.courier = data.courier || [];
    // If everything came back empty the background cache warm-up probably
    // hasn't finished yet (e.g. right after a server restart) - leave
    // optionsLoaded false so the next focus/open retries instead of being
    // stuck showing "No matches" for the rest of the session.
    optionsLoaded = Boolean(preloadedOptions.facility.length || preloadedOptions.courier.length);
  }

  function setupCombo(wrapEl) {
    const key = wrapEl.dataset.combo;
    const input = wrapEl.querySelector("input");
    const list = wrapEl.querySelector(".combo-list");

    function renderItems(items, opts) {
      list.textContent = "";
      opts = opts || {};
      if (items.length === 0) {
        const div = document.createElement("div");
        div.className = "combo-item disabled";
        div.textContent = opts.emptyText || "No matches";
        list.appendChild(div);
      } else {
        items.slice(0, MAX_RENDERED_ITEMS).forEach((value) => {
          const div = document.createElement("div");
          div.className = "combo-item";
          div.textContent = value;
          div.addEventListener("mousedown", (e) => {
            e.preventDefault();
            input.value = value;
            closeList();
          });
          list.appendChild(div);
        });
        if (items.length > MAX_RENDERED_ITEMS) {
          const div = document.createElement("div");
          div.className = "combo-item disabled";
          div.textContent = `+${items.length - MAX_RENDERED_ITEMS} more — keep typing to narrow`;
          list.appendChild(div);
        }
      }
      const wasHidden = list.classList.contains("hidden");
      list.classList.remove("hidden");
      positionList(wasHidden);
    }

    // The list sits right under the input (absolute, inside .combo). If the
    // modal's scroll area would cut it off, scroll the modal so it's all visible.
    function positionList(allowScroll) {
      if (!allowScroll || list.classList.contains("hidden")) return;
      const body = wrapEl.closest(".modal-body");
      if (!body) return;
      const overflow = list.getBoundingClientRect().bottom - body.getBoundingClientRect().bottom + 12;
      if (overflow > 0) body.scrollTop += overflow;
    }

    function closeList() {
      list.classList.add("hidden");
    }

    if (SERVER_SEARCH_FIELDS.has(key)) {
      const runSearch = debounce(async (q) => {
        try {
          const data = await fetchJson(`/api/dropdown-search?field=${key}&q=${encodeURIComponent(q)}`);
          renderItems(data.results || [], { emptyText: "No matches" });
        } catch (e) {
          renderItems([], { emptyText: "Search failed" });
        }
      }, SEARCH_DEBOUNCE_MS);

      input.addEventListener("focus", () => runSearch(input.value.trim()));
      input.addEventListener("input", () => runSearch(input.value.trim()));
    } else {
      function showFiltered() {
        const q = input.value.trim().toLowerCase();
        const all = preloadedOptions[key] || [];
        const filtered = q ? all.filter((v) => v.toLowerCase().includes(q)) : all;
        renderItems(filtered, { emptyText: "No matches" });
      }

      input.addEventListener("focus", async () => {
        try {
          await ensureOptionsLoaded();
        } catch (e) {
          renderItems([], { emptyText: "Could not load options" });
          return;
        }
        showFiltered();
      });
      input.addEventListener("input", showFiltered);
    }

    input.addEventListener("blur", () => {
      // slight delay so the mousedown handler on combo-item still fires
      setTimeout(closeList, 120);
    });
  }

  document.querySelectorAll(".combo").forEach(setupCombo);

  // ---------- modal ----------
  const overlay = document.getElementById("modal-overlay");
  const form = document.getElementById("entry-form");
  const formError = document.getElementById("form-error");
  const submitBtn = document.getElementById("btn-submit");

  function openModal() {
    form.reset();
    formError.className = "form-error hidden";
    overlay.classList.remove("hidden");
    ensureOptionsLoaded().catch(() => {});
  }

  function closeModal() {
    overlay.classList.add("hidden");
  }

  document.getElementById("btn-new-entry").addEventListener("click", openModal);
  document.getElementById("btn-close-modal").addEventListener("click", closeModal);
  document.getElementById("btn-cancel").addEventListener("click", closeModal);
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) closeModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !overlay.classList.contains("hidden")) closeModal();
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    formError.className = "form-error hidden";
    submitBtn.disabled = true;
    submitBtn.textContent = "Saving...";
    try {
      const formData = new FormData(form);
      const data = await fetchJson("/api/entries", { method: "POST", body: formData });
      closeModal();
      if (window.RTOAnalytics) window.RTOAnalytics.refresh(); // the new entry shows up in RTO Analytics
      showBanner(document.getElementById("banner-entries"), "Entry saved.", "success");
      setTimeout(() => hideBanner(document.getElementById("banner-entries")), 4000);
      loadEntries();
    } catch (err) {
      formError.textContent = err.message;
      formError.className = "form-error";
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = "Save Entry";
    }
  });

  // ---------- entries table ----------
  const tbody = document.getElementById("entries-tbody");
  const entriesEmpty = document.getElementById("entries-empty");
  const entriesCount = document.getElementById("entries-count");
  const syncBtn = document.getElementById("btn-sync-sheet");

  let allEntries = [];

  const searchInput = document.getElementById("entries-search");
  const filterDamageType = document.getElementById("filter-damage-type");
  const filterFacility = document.getElementById("filter-facility");
  const filterCourier = document.getElementById("filter-courier");
  const filterSync = document.getElementById("filter-sync");
  const filterDateFrom = document.getElementById("filter-date-from");
  const filterDateTo = document.getElementById("filter-date-to");

  function makeLinkCell(url, label) {
    const td = document.createElement("td");
    if (url) {
      const a = document.createElement("a");
      a.href = url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.className = "link";
      a.textContent = label;
      td.appendChild(a);
    } else {
      td.textContent = "-";
    }
    return td;
  }

  function makeCell(text, opts) {
    const td = document.createElement("td");
    td.textContent = text == null || text === "" ? "-" : text;
    if (opts && opts.wrap) td.classList.add("wrap");
    return td;
  }

  function renderEntries(entries, totalCount) {
    tbody.textContent = "";
    entriesEmpty.classList.toggle("hidden", entries.length !== 0);
    entriesCount.textContent =
      entries.length === totalCount
        ? entries.length + (entries.length === 1 ? " entry" : " entries")
        : `${entries.length} of ${totalCount} entries`;

    entries.forEach((entry, idx) => {
      const tr = document.createElement("tr");
      tr.appendChild(makeCell(entries.length - idx));
      tr.appendChild(makeCell(entry.damage_marked_date));
      tr.appendChild(makeCell(entry.received_date));
      tr.appendChild(makeCell(entry.facility_name));
      tr.appendChild(makeCell(entry.damage_type));
      tr.appendChild(makeCell(entry.awb_code));
      tr.appendChild(makeCell(entry.sku_code));
      tr.appendChild(makeLinkCell(entry.pod_link, "View"));
      tr.appendChild(makeLinkCell(entry.video_link, "View"));
      tr.appendChild(makeCell(entry.courier_name));
      tr.appendChild(makeCell(entry.remarks, { wrap: true }));

      const syncTd = document.createElement("td");
      const badge = document.createElement("span");
      if (entry.sheet_synced) {
        badge.className = "badge synced";
        badge.textContent = "Synced";
      } else {
        badge.className = "badge pending";
        badge.textContent = "Pending";
      }
      syncTd.appendChild(badge);
      tr.appendChild(syncTd);

      tr.appendChild(makeCell(entry.created_at));
      tbody.appendChild(tr);
    });
  }

  // Sync-button visibility reflects ALL entries, not just the filtered/visible
  // ones - filtering down to "Synced" shouldn't hide a pending count that's
  // simply out of view.
  function updateSyncButtonVisibility() {
    const pending = allEntries.some((e) => !e.sheet_synced);
    syncBtn.classList.toggle("hidden", !pending);
  }

  function distinctSorted(values) {
    return Array.from(new Set(values.filter((v) => v))).sort((a, b) => a.localeCompare(b));
  }

  function populateFilterOptions(selectEl, values) {
    const current = selectEl.value;
    const firstOption = selectEl.querySelector("option");
    selectEl.textContent = "";
    selectEl.appendChild(firstOption);
    values.forEach((v) => {
      const opt = document.createElement("option");
      opt.value = v;
      opt.textContent = v;
      selectEl.appendChild(opt);
    });
    // keep the previous selection if it's still a valid option after refresh
    if (current && values.includes(current)) selectEl.value = current;
  }

  function getFilteredEntries() {
    const q = searchInput.value.trim().toLowerCase();
    const damageType = filterDamageType.value;
    const facility = filterFacility.value;
    const courier = filterCourier.value;
    const sync = filterSync.value;
    const from = filterDateFrom.value;
    const to = filterDateTo.value;

    return allEntries.filter((e) => {
      if (damageType && e.damage_type !== damageType) return false;
      if (facility && e.facility_name !== facility) return false;
      if (courier && e.courier_name !== courier) return false;
      if (sync === "synced" && !e.sheet_synced) return false;
      if (sync === "pending" && e.sheet_synced) return false;
      if (from && e.damage_marked_date < from) return false;
      if (to && e.damage_marked_date > to) return false;
      if (q) {
        const haystack = `${e.awb_code} ${e.sku_code} ${e.facility_name} ${e.courier_name} ${e.remarks}`.toLowerCase();
        if (!haystack.includes(q)) return false;
      }
      return true;
    });
  }

  function applyEntryFilters() {
    renderEntries(getFilteredEntries(), allEntries.length);
  }

  const debouncedApplyFilters = debounce(applyEntryFilters, 150);
  searchInput.addEventListener("input", debouncedApplyFilters);
  [filterDamageType, filterFacility, filterCourier, filterSync, filterDateFrom, filterDateTo].forEach((elm) =>
    elm.addEventListener("change", applyEntryFilters)
  );
  document.getElementById("btn-clear-filters").addEventListener("click", () => {
    searchInput.value = "";
    filterDamageType.value = "";
    filterFacility.value = "";
    filterCourier.value = "";
    filterSync.value = "";
    filterDateFrom.value = "";
    filterDateTo.value = "";
    applyEntryFilters();
  });

  async function loadEntries() {
    try {
      const data = await fetchJson("/api/entries");
      allEntries = data.entries || [];
      populateFilterOptions(filterFacility, distinctSorted(allEntries.map((e) => e.facility_name)));
      populateFilterOptions(filterCourier, distinctSorted(allEntries.map((e) => e.courier_name)));
      updateSyncButtonVisibility();
      applyEntryFilters();
      hideBanner(document.getElementById("banner-entries"));
    } catch (err) {
      showBanner(document.getElementById("banner-entries"), err.message, "error");
    }
  }

  document.getElementById("btn-refresh-entries").addEventListener("click", loadEntries);

  syncBtn.addEventListener("click", async () => {
    syncBtn.disabled = true;
    syncBtn.textContent = "Syncing...";
    try {
      const data = await fetchJson("/api/entries/sync", { method: "POST" });
      showBanner(
        document.getElementById("banner-entries"),
        `Synced ${data.synced || 0} entr${data.synced === 1 ? "y" : "ies"} to the Google Sheet.`,
        "success"
      );
      loadEntries();
    } catch (err) {
      showBanner(document.getElementById("banner-entries"), err.message, "error");
    } finally {
      syncBtn.disabled = false;
      syncBtn.textContent = "Sync to Google Sheet";
    }
  });

  // ---------- init ----------
  checkStatus();
})();
