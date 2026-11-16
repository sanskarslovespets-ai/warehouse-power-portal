(function () {
  "use strict";

  // Warehouse Dashboards > BoxVault (boxvault.py).
  // - Upload invoices: add one or more invoice files (choosing again adds to the list), and they go to
  //   /api/boxvault/invoices, which reads each PDF invoice's warehouse from its Ship To address, puts the file in
  //   Google Drive in that warehouse's folder ("Unsorted" if none was read) and its line items in Invoice Entry.
  // - Invoice Entry: those line items (/api/boxvault/entries), filtered and totalled here; editors can take an
  //   invoice out (a cancelled one, say), which leaves its PDF in Drive. SKU used comes with them, worked out from
  //   the boxes used that editors enter (box_usage.py).
  // - Enter usage (and the step after an upload that added invoices): how many boxes of each SKU a warehouse used
  //   in a month, typed into a table and sent to /api/boxvault/usage.
  // - Analytics (boxvault_analytics.js, the first view): the same lines, totalled and compared.
  // Both are fetched again every hour while the page is open (when it's next looked at, if it's in the background).
  // The warehouses (the filter), the file types and the limits come from /api/boxvault, the first time the panel is opened.

  const $ = (id) => document.getElementById(id);
  const uploadBtn = $("bv-upload");
  const banner = $("bv-banner");
  const stateEl = $("bv-state");
  const stateText = $("bv-state-text");
  const body = $("bv-body");
  const usageProblem = $("bv-usage-problem");
  const analyticsEl = $("bv-analytics");
  const viewButtons = document.querySelectorAll("#panel-boxvault .bv-views button");
  const updatedEl = $("bv-updated");
  const REFRESH_MS = 60 * 60 * 1000;
  const whFilter = $("bv-f-wh");
  const search = $("bv-f-q");
  const overlay = $("bv-modal");
  const form = $("bv-form");
  const formError = $("bv-form-error");
  const fileInput = $("bv-files");
  const fileList = $("bv-file-list");
  const submitBtn = $("bv-submit");
  const modalTitle = $("bv-modal-title");
  const usageBtn = $("bv-enter-usage");
  const usageForm = $("bv-usage-form");
  const usageError = $("bv-usage-error");
  const usageMonth = $("bv-usage-month");
  const usageWh = $("bv-usage-wh");
  const usageRows = $("bv-usage-rows");
  const usageEmpty = $("bv-usage-empty");
  const usageSave = $("bv-usage-save");
  const usageCancel = $("bv-usage-cancel");

  let settings = null; // /api/boxvault: {warehouses: [{id, name}], extensions, max_files, max_total_bytes, ready}
  let access = { role: "viewer" };
  let rows = []; // /api/boxvault/entries
  let usage = []; // the boxes used entered: [{warehouse, sku, month: "YYYY-MM", used, entered_by, entered_at}]
  let usageReady = false; // whether they can be saved here (scm.boxvault_sku_usage exists)
  let usageOnly = null; // in the step after an upload: a Set of its invoices' "warehouse|sku"; null for every SKU
  let uploadLines = null; // that upload's banner lines, kept when the boxes used are saved
  let chosen = []; // the File objects to upload, in the order they were added
  let uploading = false;
  let saving = false;
  let opened = false;
  let view = "analytics"; // or "entries"
  let refreshing = false; // the hourly timer is running
  let stale = false; // the hour came round while the page was in the background

  const CSV_COLUMNS = ["Warehouse", "Item & description", "SKU", "Quantity", "Tax %", "Tax amount", "Total amount",
    "Invoice date", "Invoice", "SKU used", "Used in", "Amount before tax", "Rate", "HSN", "Supplier", "Uploaded by"];

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function size(bytes) {
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + " KB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MB";
  }

  function num(n, decimals) {
    return n.toLocaleString("en-IN", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
  }
  const qty = (n) => num(n, Number.isInteger(n) ? 0 : 2);
  const money = (n) => num(n, 2);
  const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const day = (iso) => `${iso.slice(8, 10)} ${MON[+iso.slice(5, 7) - 1]} ${iso.slice(0, 4)}`;
  const monthLabel = (ym) => `${MON[+ym.slice(5, 7) - 1]} ${ym.slice(0, 4)}`;
  const thisMonth = () => {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
  };
  const monthEnd = (ym) => new Date(Date.UTC(+ym.slice(0, 4), +ym.slice(5, 7), 0)).toISOString().slice(0, 10);

  // "DEL Supertails 15*15*15 Printed 5 Ply carton box" -> "15 × 15 × 15"
  function boxSize(description) {
    const m = /(\d+(?:\.\d+)?)\s*\*\s*(\d+(?:\.\d+)?)\s*\*\s*(\d+(?:\.\d+)?)/.exec(description);
    return m ? `${m[1]} × ${m[2]} × ${m[3]}` : description;
  }

  function extension(name) {
    const dot = name.lastIndexOf(".");
    return dot < 0 ? "" : name.slice(dot).toLowerCase();
  }

  function warehouseName(id) {
    const found = settings && settings.warehouses.find((w) => w.id === id);
    return found ? found.name : id;
  }

  // The banner under the tabs: one message, or several lines (an upload's outcome for each kind of file).
  function showBanner(lines, kind) {
    banner.replaceChildren();
    [].concat(lines).forEach((line, i) => {
      if (i) banner.append(el("br"));
      banner.append(line);
    });
    banner.className = "banner " + kind;
  }

  function showFormError(message) {
    formError.textContent = message;
    formError.classList.remove("hidden");
    form.scrollTop = 0;
  }

  async function getJson(url, options) {
    const res = await fetch(url, options);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
    return data;
  }

  // ---------- the panel ----------

  async function load() {
    try {
      [settings, access] = await Promise.all([getJson("/api/boxvault"), window.PortalAccess.ready]);
    } catch (err) {
      opened = false; // try again next time the panel is opened
      stateText.textContent = "Couldn't load BoxVault: " + err.message;
      return;
    }
    whFilter.replaceChildren(new Option("All warehouses", ""), ...settings.warehouses.map((w) => new Option(w.name, w.id)));
    usageWh.replaceChildren(new Option("All warehouses", ""), ...settings.warehouses.map((w) => new Option(w.name, w.id)));
    fileInput.accept = settings.extensions.join(",");
    $("bv-files-hint").textContent =
      `PDFs, images or spreadsheets, up to ${settings.max_files} files. Each PDF invoice goes into Invoice Entry under the warehouse in its Ship To address, so one upload can cover several warehouses. Choose files again to add more to the list.`;
    uploadBtn.disabled = !settings.ready;
    uploadBtn.title = settings.ready ? "" : "Invoice uploads aren't set up on this server yet";
    await loadEntries();
    startRefreshing();
  }

  const clock = () => new Date().toLocaleTimeString("en-IN", { hour: "numeric", minute: "2-digit" });

  // Every hour, Invoice Entry and its SKU used are fetched again; a page in the background waits
  // until it's looked at.
  function startRefreshing() {
    if (refreshing) return;
    refreshing = true;
    setInterval(() => {
      if (document.hidden) stale = true;
      else loadEntries(true);
    }, REFRESH_MS);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && stale) {
        stale = false;
        loadEntries(true);
      }
    });
  }

  // background: the hourly refresh, which keeps what's shown if it fails (and says so) rather than an error page.
  async function loadEntries(background) {
    try {
      const data = await getJson("/api/boxvault/entries");
      rows = data.rows;
      usage = data.usage || [];
      usageReady = !!data.usage_ready;
      usageBtn.disabled = !usageReady;
      usageBtn.title = usageReady ? "How many boxes of each SKU the warehouses used in a month"
        : "Boxes used can't be entered yet: the scm.boxvault_sku_usage table hasn't been made";
      usageProblem.textContent = data.usage_problem || "";
      usageProblem.classList.toggle("hidden", !data.usage_problem);
      updatedEl.textContent = `Updated ${clock()} · refreshes every hour`;
    } catch (err) {
      if (background && rows.length) {
        updatedEl.textContent = `Couldn't refresh at ${clock()} (${err.message}); showing the last data. Trying again in an hour.`;
        return;
      }
      stateEl.hidden = false;
      body.hidden = analyticsEl.hidden = true;
      stateText.textContent = "Couldn't load Invoice Entry: " + err.message;
      return;
    }
    render();
  }

  function emptyText() {
    if (!settings.ready) {
      return "Invoice uploads aren't set up on this server yet: it needs a Google Drive folder for them (DRIVE_INVOICE_FOLDER_ID). Ask an admin.";
    }
    if (access.role === "viewer") return "No invoices yet. Editors add them with Upload invoices.";
    return "No invoices yet. Choose Upload invoices and add the supplier's PDF invoices: each line item appears here, under the warehouse in its Ship To address.";
  }

  function matching() {
    const wh = whFilter.value;
    const q = search.value.trim().toLowerCase();
    return rows.filter((r) => (!wh || r.warehouse === wh) &&
      (!q || [r.invoice_number, r.sku, r.description].some((v) => v.toLowerCase().includes(q))));
  }

  function card(label, value, note) {
    const c = el("div", "pcard");
    c.append(el("div", "k", label), el("div", "v", value), el("div", "s", note));
    return c;
  }

  function render() {
    stateEl.hidden = rows.length > 0;
    body.hidden = rows.length === 0 || view !== "entries";
    analyticsEl.hidden = rows.length === 0 || view !== "analytics";
    if (!rows.length) {
      stateText.textContent = emptyText();
      return;
    }
    if (view === "analytics") {
      window.BoxVaultAnalytics.render(rows, settings.warehouses);
      return;
    }
    const shown = matching();
    const invoices = new Set(shown.map((r) => r.supplier_gstin + " " + r.invoice_number));
    const sum = (key) => shown.reduce((t, r) => t + r[key], 0);
    $("bv-cards").replaceChildren(
      card("Invoices", qty(invoices.size), `${qty(shown.length)} line items`),
      card("Quantity bought", qty(sum("qty")), "boxes and other items, all SKUs"),
      card("Tax", "₹" + money(sum("tax_amount")), "IGST, or CGST and SGST"),
      card("Total amount", "₹" + money(sum("total")), "before tax ₹" + money(sum("amount")))
    );
    $("bv-shown").textContent = shown.length === rows.length ? `${qty(rows.length)} lines` : `${qty(shown.length)} of ${qty(rows.length)} lines`;

    const seen = new Set();
    $("bv-rows").replaceChildren(...shown.map((r) => {
      const tr = el("tr");
      const link = el("a", "", r.invoice_number);
      link.href = r.drive_link;
      link.target = "_blank";
      link.rel = "noopener";
      link.title = `${r.supplier}, ${r.file_name}: open in Google Drive`;
      const invoiceCell = el("td", "ct-left");
      invoiceCell.append(link);
      const key = r.supplier_gstin + " " + r.invoice_number;
      if (!seen.has(key)) { // editors' Remove, once per invoice, on its first line shown
        seen.add(key);
        const remove = el("button", "linkbtn bv-remove edit-only", "Remove");
        remove.type = "button";
        remove.title = `Take invoice ${r.invoice_number} out of Invoice Entry (its PDF stays in Drive)`;
        remove.addEventListener("click", () => removeInvoice(r));
        invoiceCell.append(remove);
      }
      tr.append(
        el("td", "ct-left", r.warehouse_name),
        el("td", "ct-left bv-desc", r.description),
        el("td", "ct-left bv-sku", r.sku),
        el("td", "num", qty(r.qty)),
        el("td", "num", `${+r.tax_pct}%`),
        el("td", "num", money(r.tax_amount)),
        el("td", "num", money(r.total)),
        el("td", "ct-left", day(r.invoice_date)),
        invoiceCell,
        el("td", r.used == null ? "num bv-na" : "num", r.used == null ? "—" : qty(r.used)),
        usedInCell(r)
      );
      return tr;
    }));
  }

  // Used in: the months whose boxes make up the line's SKU used (with how many from each, when there's more than one).
  const usedInText = (r, sep) => (r.used_in || []).map((m) => `${monthLabel(m.month)}: ${m.used}`).join(sep);
  function usedInCell(r) {
    const months = r.used_in || [];
    if (!months.length) return el("td", "ct-left bv-na", "—");
    const td = el("td", "ct-left bv-used-in");
    td.title = months.map((m) => `${monthLabel(m.month)}: ${qty(m.used)} boxes`).join("\n");
    td.append(...months.map((m) => el("div", "", months.length > 1 ? `${monthLabel(m.month)}: ${qty(m.used)}` : monthLabel(m.month))));
    return td;
  }

  async function removeInvoice(r) {
    const lines = rows.filter((x) => x.supplier_gstin === r.supplier_gstin && x.invoice_number === r.invoice_number).length;
    if (!confirm(`Take invoice ${r.invoice_number} (${lines} line${lines === 1 ? "" : "s"}, ${r.warehouse_name}) out of Invoice Entry?\n\nIts PDF stays in Google Drive. Upload it again to add it back.`)) return;
    const params = new URLSearchParams({ supplier_gstin: r.supplier_gstin, invoice_number: r.invoice_number });
    try {
      await getJson("/api/boxvault/entries?" + params, { method: "DELETE" });
      showBanner(`Took invoice ${r.invoice_number} out of Invoice Entry. Its PDF is still in Google Drive.`, "success");
    } catch (err) {
      showBanner(err.message, "error");
    }
    await loadEntries();
  }

  $("bv-csv").addEventListener("click", () => {
    window.RtoCsv.download("boxvault_invoice_entry", CSV_COLUMNS, matching().map((r) => ({
      "Warehouse": r.warehouse, "Item & description": r.description, "SKU": r.sku, "Quantity": r.qty,
      "Tax %": r.tax_pct, "Tax amount": r.tax_amount, "Total amount": r.total, "Invoice date": r.invoice_date,
      "Invoice": r.invoice_number, "SKU used": r.used == null ? "" : r.used, "Used in": usedInText(r, "; "),
      "Amount before tax": r.amount, "Rate": r.rate, "HSN": r.hsn, "Supplier": r.supplier, "Uploaded by": r.uploaded_by,
    })));
  });
  function showView(v) {
    view = v;
    viewButtons.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.view === v)));
    if (settings) render(); // before the first load, load() renders
  }
  viewButtons.forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));

  whFilter.addEventListener("change", render);
  search.addEventListener("input", render);

  // What an upload did with each file, as banner lines (boxvault.upload's "entry" for each).
  function outcome(files) {
    const by = (status) => files.filter((f) => f.entry.status === status);
    const added = by("added"), already = by("already"), notRead = by("not_read"), notSaved = by("not_saved");
    const lines = [];
    if (added.length) {
      const n = added.reduce((t, f) => t + f.entry.lines, 0);
      const byWarehouse = new Map();
      added.forEach((f) => byWarehouse.set(f.warehouse, [...(byWarehouse.get(f.warehouse) || []), f.entry.invoice]));
      lines.push(`Added ${added.length} invoice${added.length === 1 ? "" : "s"} (${n} line${n === 1 ? "" : "s"}) to Invoice Entry:`);
      byWarehouse.forEach((invoices, wh) => lines.push(`${warehouseName(wh)}: ${invoices.join(", ")}`));
    }
    if (already.length) {
      lines.push(`Already in Invoice Entry, so not added or uploaded again: ${already.map((f) => f.entry.invoice).join(", ")}.`);
    }
    notRead.forEach((f) => lines.push(`${f.file} is saved in Drive${f.warehouse ? "" : " (in Unsorted)"} but not in Invoice Entry: ${f.entry.reason}.`));
    notSaved.forEach((f) => lines.push(`${f.file} (${f.entry.invoice}) is saved in Drive, but its lines couldn't be saved to Invoice Entry. Upload it again to retry.`));
    const kind = notSaved.length ? "error" : notRead.length || !added.length ? "info" : "success";
    return { lines, kind };
  }

  // ---------- Upload invoices ----------

  function renderFiles() {
    fileList.replaceChildren(
      ...chosen.map((file, i) => {
        const li = el("li");
        const remove = el("button", "icon-btn", "×");
        remove.type = "button";
        remove.setAttribute("aria-label", "Remove " + file.name);
        remove.addEventListener("click", () => {
          chosen.splice(i, 1);
          renderFiles();
        });
        li.append(el("span", "bv-file-name", file.name), el("span", "bv-file-size", size(file.size)), remove);
        li.firstChild.title = file.name;
        return li;
      })
    );
    fileList.hidden = !chosen.length;
    submitBtn.textContent = chosen.length > 1 ? `Upload ${chosen.length} files` : "Upload";
  }

  // The dialog shows one form at a time: "files" (Upload invoices) or "usage" (the boxes used).
  function showStep(step) {
    form.hidden = step !== "files";
    usageForm.hidden = step !== "usage";
    overlay.querySelector(".modal").classList.toggle("bv-wide", step === "usage");
    modalTitle.textContent = step === "files" ? "Upload invoices" : usageOnly ? "Boxes used" : "Enter usage";
  }

  function openForm() {
    if (!settings || !settings.ready) return;
    showStep("files");
    chosen = [];
    fileInput.value = "";
    formError.classList.add("hidden");
    renderFiles();
    overlay.classList.remove("hidden");
    fileInput.focus();
  }

  function closeForm() {
    if (uploading || saving) return; // it would carry on unseen
    overlay.classList.add("hidden");
  }

  function setBusy(busy) {
    uploading = busy;
    [fileInput, submitBtn, $("bv-cancel"), $("bv-close"), ...fileList.querySelectorAll("button")].forEach((c) => {
      c.disabled = busy;
    });
    if (busy) submitBtn.textContent = chosen.length > 1 ? `Uploading ${chosen.length} files…` : "Uploading…";
    else renderFiles();
  }

  // Choosing (or dropping onto the picker) adds to the list. Files that couldn't be uploaded are left out, saying why.
  fileInput.addEventListener("change", () => {
    const wrong = [], empty = [];
    for (const file of fileInput.files) {
      if (!settings.extensions.includes(extension(file.name))) wrong.push(file.name);
      else if (file.size === 0) empty.push(file.name);
      else if (!chosen.some((c) => c.name === file.name && c.size === file.size && c.lastModified === file.lastModified)) {
        chosen.push(file);
      }
    }
    fileInput.value = ""; // so choosing the same file again (after removing it) still counts as a change
    const problems = [];
    if (wrong.length) problems.push(`${wrong.join(", ")}: only PDFs, images and spreadsheets can be uploaded.`);
    if (empty.length) problems.push(`${empty.join(", ")}: empty, so not added.`);
    if (problems.length) showFormError(problems.join(" "));
    else formError.classList.add("hidden");
    renderFiles();
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const total = chosen.reduce((sum, f) => sum + f.size, 0);
    if (!chosen.length) return showFormError("Add at least one invoice file.");
    if (chosen.length > settings.max_files) return showFormError(`Upload at most ${settings.max_files} files at a time.`);
    if (total > settings.max_total_bytes) {
      return showFormError(`These files add up to ${size(total)}; upload them in smaller batches (under ${size(settings.max_total_bytes)} each).`);
    }

    const data = new FormData();
    chosen.forEach((file) => data.append("files", file, file.name));
    formError.classList.add("hidden");
    setBusy(true);
    let res = null, answer = {};
    try {
      res = await fetch("/api/boxvault/invoices", { method: "POST", body: data });
      answer = await res.json();
    } catch (err) {
      /* no answer, or not JSON: said below */
    }
    setBusy(false);

    const files = answer.files || [];
    if (files.length) {
      const { lines, kind } = outcome(files);
      showBanner(lines, kind);
      uploadLines = { lines, kind };
      await loadEntries();
    }
    if (res && res.ok) {
      // Invoices added: on to how many of their boxes were used (or close, if that can't be saved here).
      const added = files.filter((f) => f.entry.status === "added");
      const pairs = new Set(rows.filter((r) => r.sku && added.some((f) => f.warehouse === r.warehouse && f.entry.invoice === r.invoice_number))
        .map((r) => r.warehouse + "|" + r.sku));
      if (usageReady && pairs.size) {
        const n = added.length;
        openUsage(pairs, `Added ${n} invoice${n === 1 ? "" : "s"} to Invoice Entry. How many boxes of each SKU did the warehouse use in the month below? You can also do this later with Enter usage.`);
        return;
      }
      closeForm();
      return;
    }
    // Some files went through and some didn't: keep only the others here to send again.
    if (files.length) {
      chosen = chosen.filter((f) => (answer.failed || []).includes(f.name));
      renderFiles();
    }
    showFormError(answer.error || (res ? `Upload failed (${res.status}). Please try again.` : "Couldn't reach the server. Please try again."));
  });

  // ---------- Boxes used (Enter usage, and after an upload) ----------

  // The SKUs to enter boxes used for: each warehouse's SKUs on invoices dated by the end of the month (after an
  // upload, only that upload's), with the quantity bought by then and what's been entered for the month.
  function usageList(month) {
    const end = monthEnd(month);
    const wh = usageOnly ? "" : usageWh.value;
    const byKey = new Map();
    rows.forEach((r) => {
      const key = r.warehouse + "|" + r.sku;
      if (!r.sku || r.invoice_date > end || (wh && r.warehouse !== wh) || (usageOnly && !usageOnly.has(key))) return;
      const s = byKey.get(key) || { warehouse: r.warehouse, sku: r.sku, item: boxSize(r.description), bought: 0 };
      s.bought += r.qty;
      byKey.set(key, s);
    });
    const saved = new Map(usage.filter((u) => u.month === month).map((u) => [u.warehouse + "|" + u.sku, u.used]));
    const order = Object.fromEntries(settings.warehouses.map((w, i) => [w.id, i]));
    return [...byKey.values()].map((s) => ({ ...s, saved: saved.get(s.warehouse + "|" + s.sku) }))
      .sort((a, b) => order[a.warehouse] - order[b.warehouse] || a.sku.localeCompare(b.sku));
  }

  function renderUsage() {
    const month = usageMonth.value || thisMonth();
    const list = usageList(month);
    $("bv-usage-head").textContent = `Used in ${monthLabel(month)}`;
    usageRows.replaceChildren(...list.map((s) => {
      const input = el("input", "bv-usage-input");
      Object.assign(input, { type: "number", min: "0", step: "1", inputMode: "numeric", placeholder: "—" });
      input.value = input.dataset.saved = s.saved == null ? "" : String(s.saved);
      input.dataset.warehouse = s.warehouse;
      input.dataset.sku = s.sku;
      input.setAttribute("aria-label", `Boxes of ${s.sku} used at ${warehouseName(s.warehouse)} in ${monthLabel(month)}`);
      const cell = el("td", "num");
      cell.append(input);
      const tr = el("tr");
      tr.append(el("td", "ct-left", warehouseName(s.warehouse)), el("td", "ct-left bv-sku", s.sku),
        el("td", "ct-left", s.item), el("td", "num", qty(s.bought)), cell);
      return tr;
    }));
    $("bv-usage-table").hidden = !list.length;
    usageEmpty.hidden = list.length > 0;
    usageEmpty.textContent = usageOnly
      ? `None of these invoices is dated by the end of ${monthLabel(month)}, so their boxes can't have been used then. Choose a later month.`
      : `No SKUs were invoiced${usageWh.value ? " at " + warehouseName(usageWh.value) : ""} by the end of ${monthLabel(month)}.`;
    usageSave.disabled = !list.length;
  }

  function openUsage(only, intro) {
    usageOnly = only;
    usageMonth.max = thisMonth();
    usageMonth.value = thisMonth();
    usageWh.value = "";
    usageWh.hidden = !!only;
    $("bv-usage-intro").textContent = intro;
    usageCancel.textContent = only ? "Skip" : "Cancel";
    usageError.classList.add("hidden");
    showStep("usage");
    renderUsage();
    overlay.classList.remove("hidden");
    (usageRows.querySelector("input") || usageMonth).focus();
  }

  function showUsageError(message) {
    usageError.textContent = message;
    usageError.classList.remove("hidden");
    usageForm.scrollTop = 0;
  }

  function setSaving(busy) {
    saving = busy;
    [usageMonth, usageWh, usageSave, usageCancel, $("bv-close"), ...usageRows.querySelectorAll("input")].forEach((c) => {
      c.disabled = busy;
    });
    usageSave.textContent = busy ? "Saving…" : "Save";
  }

  usageMonth.addEventListener("change", () => {
    usageError.classList.add("hidden");
    renderUsage();
  });
  usageWh.addEventListener("change", renderUsage);

  usageForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const month = usageMonth.value;
    if (!/^\d{4}-\d{2}$/.test(month)) return showUsageError("Choose the month the boxes were used in.");
    if (month > thisMonth()) return showUsageError("Boxes used can't be entered for a month that hasn't started yet.");
    const numbers = [], bad = [];
    usageRows.querySelectorAll("input").forEach((input) => {
      const text = input.value.trim();
      if (input.validity.badInput) bad.push(input.dataset.sku);
      else if (text === "" || text === input.dataset.saved) return; // empty or unchanged: left as it is
      else if (!/^\d+$/.test(text)) bad.push(input.dataset.sku);
      else numbers.push({ warehouse: input.dataset.warehouse, sku: input.dataset.sku, used: Number(text) });
    });
    if (bad.length) return showUsageError(`Enter whole numbers of boxes, 0 or more, for ${bad.join(", ")}.`);
    if (!numbers.length) {
      closeForm(); // nothing new to save
      return;
    }
    setSaving(true);
    try {
      await getJson("/api/boxvault/usage", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ month, numbers }),
      });
    } catch (err) {
      setSaving(false);
      return showUsageError(err.message);
    }
    setSaving(false);
    const saved = `Saved the boxes used in ${monthLabel(month)} for ${numbers.length} SKU${numbers.length === 1 ? "" : "s"}.`;
    if (usageOnly && uploadLines) showBanner([...uploadLines.lines, saved], uploadLines.kind);
    else showBanner(saved, "success");
    closeForm();
    loadEntries();
  });

  usageBtn.addEventListener("click", () => {
    if (!settings || !usageReady) return;
    uploadLines = null;
    openUsage(null, "How many boxes of each SKU did each warehouse use in the month below? Only SKUs invoiced by the end of the month are listed.");
  });
  usageCancel.addEventListener("click", closeForm);

  uploadBtn.addEventListener("click", openForm);
  $("bv-close").addEventListener("click", closeForm);
  $("bv-cancel").addEventListener("click", closeForm);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !overlay.classList.contains("hidden")) closeForm();
  });

  document.querySelector('.side-nav-item[data-panel="boxvault"]').addEventListener("click", () => {
    if (!opened) {
      opened = true;
      load();
    }
  });
})();
