# breach_pipeline/

The export scripts behind the **Breach Explorer** tabs, copied from the SLA Breach Explorer
(`breach-explorer.apps.internal.supertails.com`), whose source is
[SupertailsPCPL/edd-observability](https://github.com/SupertailsPCPL/edd-observability) at commit
`f0a12e6` (15 Oct 2026). Using the same scripts means every number here is computed exactly as on
that dashboard. The portal runs them (`breach_explorer.py`) and serves what they write.

| Here | Upstream | Changed? |
|---|---|---|
| `scripts/export_*.py` (7) | `scripts/` | No, byte-for-byte copies |
| `pincode city mapping .xlsx` | repo root | No |
| `dashboards/*.xlsx` (3 courier hub mappings) | `dashboards/` | No |
| `db/connection.py` | `db/connection.py` | **Yes:** reads the portal's `SCM_DB_*` / `DN_DB_*` settings instead of `DB_*` / `ERP_DB_*`, and sends passwords as UTF-8 |
| `data/` | `data/` | Where the scripts write their JSON. Git-ignored; never served directly |

Not copied: upstream's `refresh_dashboard.py` (it injects the JSON into one big HTML page; here
`breach_explorer.py` checks each export's JSON and serves it per tab), the page template (ported to
`static/js/be_*.js` and `static/css/breach_explorer.css`), the warehouse-exceptions page and the
Google Sheet sync.

| Export | Tab | Reads |
|---|---|---|
| `export_breach_dashboard_data.py` | Breach analysis | `scm.cp_database_N` (full scan of ~107 days of deliveries) |
| `export_pendency_data.py` | Pendency | `scm.cp_database_N` |
| `export_pendency_detail.py` | Pendency's Export CSV | `scm.cp_database_N` |
| `export_complaints_data.py` | Complaints | `CX_data.CDC_Nugget`, `scm.cp_database_N`, ERP `tabDelivery Note` |
| `export_rto_data.py` | RTO | `scm.cp_database_N`, ERP `tabDelivery Note` |
| `export_connection_data.py` | Connection view | `scm.cp_database_N` |
| `export_rad_ofd_data.py` | RAD to OFD | `scm.cp_database_N` + the hub mappings |

## Updating from upstream

When the Breach Explorer's logic changes upstream, copy the changed `scripts/export_*.py` (and any
changed workbook) over these and update the commit above. Then:

- If a script's output **shape** changed (new fact columns, new dims), port the matching change in
  the upstream page template to `static/js/be_<tab>.js`. Also update the check for that dataset in
  `breach_explorer.CHECKS`, for example the breach facts' width of 31.
- If upstream changed `db/connection.py`, carry the change into this copy by hand. Keep the portal's
  settings names and the UTF-8 passwords.
- If upstream's `service/app/detail.py` changed, mirror its SQL in `breach_explorer.AWB_SPECS`. That
  file is the AWB-level Export CSV on the Connection view and RAD to OFD tabs.

To run one export by hand from the project root:
`python3 breach_pipeline/scripts/export_pendency_data.py`. It reads `.env` and writes
`breach_pipeline/data/pendency_data.json`.
