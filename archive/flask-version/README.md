# RTO Power Portal

A local web app for logging RTO / RPU / Warehouse / Inward damage entries.

- **RTO Process → RTO Analytics tab**: "+ New Entry" form. Facility, AWB, SKU and
  Courier are searchable dropdowns sourced live from `cp_database_N` in the SCM
  MySQL database. POD and video files are uploaded straight to two Google Drive
  folders. Each saved entry is also appended to a Google Sheet.
- **RTO Process → All Entries tab**: table of every entry saved so far, with links
  to the uploaded POD/video and a sync status badge.

## 1. Install dependencies

Already done in this environment (Flask + Google API client were added via `uv`).
On a fresh machine:

```bash
pip install -r requirements.txt
```

## 2. Google Drive + Sheets connection

**Status: connected**, via a Google **service account** (no browser login needed —
this is the preferred path for a headless app like this one). The key lives in
`service_account.json` in this folder (git-ignored, not committed). Its address is:

```
unified-gsheet@gsheet-automation-505409.iam.gserviceaccount.com
```

Verified live just now:

| Resource | Reachable | Can write |
|---|---|---|
| Google Sheet ("Sanskar \| Damage Entries") | ✅ | ✅ |
| POD Drive folder ("Photos") | ✅ | ❌ Viewer only |
| Video Drive folder ("Videos") | ✅ | ❌ Viewer only |

**Action needed before uploads will work:** the two Drive folders currently only
give this service account *view* access (inherited from the "anyone with the
link can view" share), not edit. Open each folder → **Share** → add
`unified-gsheet@gsheet-automation-505409.iam.gserviceaccount.com` → role
**Editor** (uncheck "notify" — it's a service account, not an inbox):
- [POD folder](https://drive.google.com/drive/folders/1gFXs62BQVonoSrhyhlX6qmIDUSGv9RrW)
- [Video folder](https://drive.google.com/drive/folders/1AjU2wVAKpDoWe-WvgUEdt04mqfiZKX1I)

Then re-check anytime with:

```bash
python3 -c "import google_services, json; print(json.dumps(google_services.diagnostics(), indent=2))"
```

If uploads still fail after that with a **storage quota** error, it means these
folders live in someone's personal "My Drive" rather than a Shared Drive —
service accounts have no storage quota of their own in that case. The fix is to
move (or recreate) the two folders inside a **Shared Drive** that the service
account is a member of.

<details>
<summary>Alternative: connect with your own Google login instead (OAuth)</summary>

If you'd rather uploads/writes happen as your own Google account instead of the
service account, you can switch back to the OAuth flow originally built for this:

1. Go to the [Google Cloud Console](https://console.cloud.google.com/), create or
   pick a project, and enable **Google Drive API** and **Google Sheets API**.
2. APIs & Services → Credentials → **Create Credentials → OAuth client ID** →
   Application type **Desktop app**. Download the JSON as `credentials.json` in
   this folder.
3. Make sure the account you'll sign in with has edit access to the two Drive
   folders and the Sheet above.
4. Run `python3 google_services.py`. A browser opens; sign in and accept. This
   saves `token.json` (git-ignored).

`google_services.py` prefers `service_account.json` when present — delete or
rename it if you want the app to fall back to `token.json`/OAuth instead.
</details>

## 3. Run the app

```bash
python3 app.py
```

Open **http://localhost:5659** in a browser.

## Configuration (`.env`)

Already set up:

```
SCM_DB_HOST=analytics-db.supertails.com
SCM_DB_PORT=3306
SCM_DB_USER=sanskar
SCM_DB_PASSWORD=...
SCM_DB_NAME=scm

GOOGLE_SHEET_ID=1n5mB90-cGl_6z3eT8JqPRlnYyJdAe25OnOeBvflLl-o
GOOGLE_SHEET_TAB=            # blank = first tab in the sheet
DRIVE_POD_FOLDER_ID=1gFXs62BQVonoSrhyhlX6qmIDUSGv9RrW
DRIVE_VIDEO_FOLDER_ID=1AjU2wVAKpDoWe-WvgUEdt04mqfiZKX1I
```

`.env`, `credentials.json`, `token.json` and `service_account.json` all hold
secrets and are git-ignored. The DB password and the service account's private
key have both been visible in plain text in this chat — worth rotating them
when convenient (regenerate the key in Google Cloud Console → IAM & Admin →
Service Accounts → this account → Keys; update the SCM DB password with
whoever administers `analytics-db.supertails.com`).

## How it fits together

| File | Purpose |
|---|---|
| `rto_process.py` | MySQL access — distinct facility/courier/SKU/AWB values, AWB existence check |
| `dropdown_cache.py` | CSV-backed cache (`dropdown_cache.csv`) for the four dropdowns above — see below |
| `entries_store.py` | Local SQLite (`rto_entries.db`) — the source of truth for the All Entries tab |
| `google_services.py` | Drive upload + Sheet append; uses `service_account.json` if present, else falls back to OAuth (`credentials.json` / `token.json`) |
| `app.py` | Flask routes / API |
| `templates/`, `static/` | Frontend (vanilla HTML/CSS/JS, no build step) |

**Design notes / why it's built this way:**

- **Local SQLite + sync status**, rather than writing to the Google Sheet directly
  as the only record: the All Entries tab loads instantly, and if Google is
  unreachable when an entry is saved, the entry still isn't lost — it's marked
  "Pending" and can be retried with the **Sync to Google Sheet** button (this
  also runs automatically right after each new entry).
- **All four dropdowns are backed by `dropdown_cache.csv`**, not a live
  `SELECT DISTINCT` per open — that was slow even in the background, because
  the real counts turned out to be much bigger than expected:

  | Field | Distinct values |
  |---|---:|
  | Courier | 14 |
  | Facility | 2,444 |
  | SKU | ~476,000 |
  | AWB | ~1,416,000 |

  A background refresh (kicked off when `app.py` starts, and again,
  throttled to once per 30 min, whenever the dashboard is opened) appends any
  values not already cached — existing rows are never rewritten — and every
  dropdown/search request only ever reads the in-memory copy of that file, so
  it can't be slow no matter how long a DB refresh takes. Because it's
  append-only, a value renamed/deleted later in `cp_database_N` keeps showing
  up — say the word if you'd rather stale entries prune themselves out.
- **Facility/Courier are small enough to preload** to the browser in full and
  filter client-side (still capped at 200 rendered rows so the DOM stays
  light). **SKU/AWB are not** — at ~476K/~1.4M values, shipping the full list
  to the browser (let alone filtering it there on every keystroke) doesn't
  scale. Those two are searched server-side instead (`/api/dropdown-search`),
  which is still just an in-memory Python substring scan — ~35ms measured
  over the full 1.4M-row AWB list — so it's fast without ever touching MySQL
  once the cache is warm.
- The cache-warming call lives inside `if __name__ == "__main__":` in
  `app.py`, not at module level, specifically so that importing `app.py` (a
  routes check, a linter, a test) can never trigger a live DB query as a side
  effect — only actually running the server does.
- **AWB is re-verified against the database on submit** (not just picked from
  the suggestion list) so a typo, or a value that's fallen out of date in the
  cache, can't silently save a non-existent AWB.
- Dates use native `<input type="date">` pickers (stored as `YYYY-MM-DD`).

## Known limitations

- Flask's built-in dev server (`app.run`) is fine for internal/local use; for
  always-on or multi-user access, put it behind a real WSGI server.
- Upload size cap is 500 MB per request (POD + video combined); adjust
  `MAX_CONTENT_LENGTH` in `app.py` if needed.
- Only one Google account can be connected at a time (single `token.json`).
- `dropdown_cache.csv` is already ~55 MB (≈1.9M rows) and, being append-only,
  will keep growing as new AWBs/SKUs show up over time. Parsing it takes
  ~1.3s the first time any process touches it after a restart (done in the
  background, off the request path) — fine today, but worth revisiting
  (e.g. pruning, or a real local index instead of CSV) if it grows much
  further.
- To manually force a full cache refresh without starting the server (e.g.
  right after deploying), run `python3 dropdown_cache.py`.
