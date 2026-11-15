(function () {
  "use strict";

  // Warehouse Dashboards > BoxVault (boxvault.py).
  // - Upload invoices: add one or more invoice files (choosing again adds to the list), and they go to
  //   /api/boxvault/invoices, which reads each PDF invoice's warehouse from its Ship To address, puts the file in
  //   Google Drive in that warehouse's folder ("Unsorted" if none was read) and its line items in Invoice Entry.
  // - Invoice Entry: those line items (/api/boxvault/entries), filtered and totalled here; editors can take an
  //   invoice out (a cancelled one, say), which leaves its PDF in Drive. SKU used / SKU left come
  //   with them, counted from the ERP's delivery notes (box_usage.py).
  // The warehouses (the filter), the file types and the limits come from /api/boxvault, the first time the panel is opened.

  const $ = (id) => document.getElementById(id);
  const uploadBtn = $("bv-upload");
  const banner = $("bv-banner");
  const stateEl = $("bv-state");
  const stateText = $("bv-state-text");
  const body = $("bv-body");
  const usageProblem = $("bv-usage-problem");
  const whFilter = $("bv-f-wh");
  const search = $("bv-f-q");
  const overlay = $("bv-modal");
  const form = $("bv-form");
  const formError = $("bv-form-error");
  const fileInput = $("bv-files");
  const fileList = $("bv-file-list");
  const submitBtn = $("bv-submit");

  let settings = null; // /api/boxvault: {warehouses: [{id, name}], extensions, max_files, max_total_bytes, ready}
  let access = { role: "viewer" };
  let rows = []; // /api/boxvault/entries
  let chosen = []; // the File objects to upload, in the order they were added
  let uploading = false;
  let opened = false;

  const CSV_COLUMNS = ["Warehouse", "Item & description", "SKU", "Quantity", "Tax %", "Tax amount", "Total amount",
    "Invoice date", "Invoice", "SKU used", "SKU left", "Amount before tax", "Rate", "HSN", "Supplier", "Uploaded by"];

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
    fileInput.accept = settings.extensions.join(",");
    $("bv-files-hint").textContent =
      `PDFs, images or spreadsheets, up to ${settings.max_files} files. Each PDF invoice goes into Invoice Entry under the warehouse in its Ship To address, so one upload can cover several warehouses. Choose files again to add more to the list.`;
    uploadBtn.disabled = !settings.ready;
    uploadBtn.title = settings.ready ? "" : "Invoice uploads aren't set up on this server yet";
    await loadEntries();
  }

  async function loadEntries() {
    try {
      const data = await getJson("/api/boxvault/entries");
      rows = data.rows;
      usageProblem.textContent = data.usage_problem || "";
      usageProblem.classList.toggle("hidden", !data.usage_problem);
    } catch (err) {
      stateEl.hidden = false;
      body.hidden = true;
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
    body.hidden = rows.length === 0;
    if (!rows.length) {
      stateText.textContent = emptyText();
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
        el("td", r.left == null ? "num bv-na" : r.left < 0 ? "num bv-over" : "num", r.left == null ? "—" : qty(r.left))
      );
      return tr;
    }));
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
      "Invoice": r.invoice_number, "SKU used": r.used == null ? "" : r.used, "SKU left": r.left == null ? "" : r.left,
      "Amount before tax": r.amount, "Rate": r.rate, "HSN": r.hsn, "Supplier": r.supplier, "Uploaded by": r.uploaded_by,
    })));
  });
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

  function openForm() {
    if (!settings || !settings.ready) return;
    chosen = [];
    fileInput.value = "";
    formError.classList.add("hidden");
    renderFiles();
    overlay.classList.remove("hidden");
    fileInput.focus();
  }

  function closeForm() {
    if (uploading) return; // the upload would carry on unseen
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
      loadEntries();
    }
    if (res && res.ok) {
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
