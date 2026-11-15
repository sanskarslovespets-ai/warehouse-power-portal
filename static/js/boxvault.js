(function () {
  "use strict";

  // Warehouse Dashboards > BoxVault: Upload invoices (boxvault.py). Pick a warehouse from the dropdown, add one
  // or more invoice files (choosing again adds to the list), and they go to /api/boxvault/invoices, which puts
  // them in Google Drive in that warehouse's folder. The panel lists what was uploaded since the page opened.
  // The dropdown, the file types and the limits come from /api/boxvault, the first time the panel is opened.

  const $ = (id) => document.getElementById(id);
  const uploadBtn = $("bv-upload");
  const banner = $("bv-banner");
  const stateEl = $("bv-state");
  const stateText = $("bv-state-text");
  const overlay = $("bv-modal");
  const form = $("bv-form");
  const formError = $("bv-form-error");
  const select = $("bv-warehouse");
  const fileInput = $("bv-files");
  const fileList = $("bv-file-list");
  const submitBtn = $("bv-submit");

  let settings = null; // /api/boxvault: {warehouses: [{id, name}], extensions, max_files, max_total_bytes, ready}
  let chosen = []; // the File objects to upload, in the order they were added
  let uploading = false;
  let opened = false;

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

  function extension(name) {
    const dot = name.lastIndexOf(".");
    return dot < 0 ? "" : name.slice(dot).toLowerCase();
  }

  function warehouseName(id) {
    const found = settings.warehouses.find((w) => w.id === id);
    return found ? found.name : id;
  }

  function showBanner(message, kind) {
    banner.textContent = message;
    banner.className = "banner " + kind;
  }

  function showFormError(message) {
    formError.textContent = message;
    formError.classList.remove("hidden");
    form.scrollTop = 0;
  }

  // ---------- the panel ----------

  async function load() {
    try {
      const res = await fetch("/api/boxvault");
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
      settings = data;
    } catch (err) {
      opened = false; // try again next time the panel is opened
      stateText.textContent = "Couldn't load BoxVault: " + err.message;
      return;
    }
    const access = await window.PortalAccess.ready;

    select.replaceChildren(new Option("Choose a warehouse", ""), ...settings.warehouses.map((w) => new Option(w.name, w.id)));
    fileInput.accept = settings.extensions.join(",");
    $("bv-files-hint").textContent =
      `PDFs, images or spreadsheets, up to ${settings.max_files} files. Choose files again to add more to the list.`;

    uploadBtn.disabled = !settings.ready;
    uploadBtn.title = settings.ready ? "" : "Invoice uploads aren't set up on this server yet";
    if (!settings.ready) {
      stateText.textContent =
        "Invoice uploads aren't set up on this server yet: it needs a Google Drive folder for them (DRIVE_INVOICE_FOLDER_ID). Ask an admin.";
    } else if (access.role === "viewer") {
      stateText.textContent = "Uploading invoices is for editors. Ask an admin if you need to upload them.";
    } else {
      stateText.textContent =
        "Choose Upload invoices, pick the warehouse, and add one or more invoice files. They're saved to Google Drive, in a folder for each warehouse.";
    }
  }

  function addUploaded(warehouse, uploaded) {
    if (!uploaded.length) return;
    const time = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const rows = $("bv-done-rows");
    uploaded.forEach((file) => {
      const link = el("a", "", file.name);
      link.href = file.link;
      link.target = "_blank";
      link.rel = "noopener";
      const tr = el("tr");
      tr.append(el("td", "num", time), el("td", "", warehouseName(warehouse)), el("td"));
      tr.lastChild.append(link);
      rows.prepend(tr); // newest first
    });
    stateEl.hidden = true;
    $("bv-done").hidden = false;
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
    select.value = "";
    fileInput.value = "";
    formError.classList.add("hidden");
    renderFiles();
    overlay.classList.remove("hidden");
    select.focus();
  }

  function closeForm() {
    if (uploading) return; // the upload would carry on unseen
    overlay.classList.add("hidden");
  }

  function setBusy(busy) {
    uploading = busy;
    [select, fileInput, submitBtn, $("bv-cancel"), $("bv-close"), ...fileList.querySelectorAll("button")].forEach((c) => {
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
    const warehouse = select.value;
    const total = chosen.reduce((sum, f) => sum + f.size, 0);
    if (!warehouse) return showFormError("Choose a warehouse.");
    if (!chosen.length) return showFormError("Add at least one invoice file.");
    if (chosen.length > settings.max_files) return showFormError(`Upload at most ${settings.max_files} files at a time.`);
    if (total > settings.max_total_bytes) {
      return showFormError(`These files add up to ${size(total)}; upload them in smaller batches (under ${size(settings.max_total_bytes)} each).`);
    }

    const body = new FormData();
    body.append("warehouse", warehouse);
    chosen.forEach((file) => body.append("files", file, file.name));
    formError.classList.add("hidden");
    setBusy(true);
    let res = null, data = {};
    try {
      res = await fetch("/api/boxvault/invoices", { method: "POST", body });
      data = await res.json();
    } catch (err) {
      /* no answer, or not JSON: said below */
    }
    setBusy(false);

    if (res && res.ok) {
      addUploaded(warehouse, data.uploaded);
      const n = data.uploaded.length;
      closeForm();
      showBanner(`Uploaded ${n} invoice${n === 1 ? "" : "s"} for ${warehouseName(warehouse)}.`, "success");
      return;
    }
    // Some files went through and some didn't: list the ones that did, and keep only the others here to send again.
    if (data.uploaded && data.uploaded.length) {
      addUploaded(warehouse, data.uploaded);
      chosen = chosen.filter((f) => (data.failed || []).includes(f.name));
      renderFiles();
    }
    showFormError(data.error || (res ? `Upload failed (${res.status}). Please try again.` : "Couldn't reach the server. Please try again."));
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
