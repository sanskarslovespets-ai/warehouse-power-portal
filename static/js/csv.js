// Saves rows already on the page as a CSV file - the Download CSV buttons of RTO Process and RTO MDND,
// whose filtered rows all live in the browser. Paged tables (RTO Delivered, Reconciliation) and the
// analytics dashboards download from the server's /export routes instead.
(function () {
  "use strict";

  // Same rules as main.py's _csv_download: a text cell starting with = + - @ (or tab / CR) would run as a
  // formula in Excel / Sheets, so a leading ' keeps it text; the BOM makes Excel read the file as UTF-8.
  function cell(value) {
    let s = value == null ? "" : String(value);
    if (typeof value === "string" && /^[=+\-@\t\r]/.test(s)) s = "'" + s;
    return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }

  function download(name, columns, rows) {
    const lines = [columns.map(cell).join(",")];
    rows.forEach((row) => lines.push(columns.map((c) => cell(row[c])).join(",")));
    const blob = new Blob(["﻿" + lines.join("\r\n") + "\r\n"], { type: "text/csv;charset=utf-8" });
    const d = new Date();
    const today = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${name}_${today}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 0);
  }

  window.RtoCsv = { download };
})();
