# Warehouse Power Portal

A web app for the warehouse team: logging RTO / RPU / Warehouse / Inward damage
entries, and dashboards over the SCM and ERP databases.

The sidebar groups the sections by area. Today there is one area, **RTO**;
click it to open or close its sections (Analytics, Process, Status, MDND,
Reconciliation). The page remembers which groups you closed.

**Adding a section later** (all in `static/index.html`):
- A new section in an existing area: copy one `.side-nav-item` button into
  that group's list and give it `data-panel="my-section"`; add a
  `<div id="panel-my-section" class="side-panel">` next to the other panels.
  Clicking the button shows that panel; nothing else needs wiring.
- A new area (e.g. Inward): copy a whole `<div class="nav-group">` block and
  give it its own `data-group`, `aria-controls` and matching `id`.

- **RTO → Reconciliation**: AWBs the courier still shows as RTO-InTransit 10+
  days after the RTO was marked, with the ERP's latest Delivery Note status.
  Filter by search (AWB, reference no., pincode), courier, return name, ERP
  status, how long it has been pending, and a date range on the RTO mark,
  created or pickup date. Each dropdown shows how many AWBs each choice has
  under the other filters, and Download CSV saves exactly the filtered rows.
- **RTO Process → RTO Analytics tab**: "+ New Entry" form. Facility, AWB, SKU and
  Courier are searchable dropdowns sourced live from `cp_database_N` in the SCM
  MySQL database. POD and video files are uploaded straight to two Google Drive
  folders. Each saved entry is also appended to a Google Sheet.
- **RTO Process → All Entries tab**: table of every entry saved so far, with links
  to the uploaded POD/video and a sync status badge.

## 1. Install dependencies

Already done in this environment (FastAPI, uvicorn + Google API client were added via `uv`).
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
python3 main.py
```

Open **http://localhost:5659** in a browser. It auto-reloads when a `.py` file
changes; edits under `static/` just need a browser refresh. Every API endpoint
is listed, and can be tried out, at **http://localhost:5659/docs** (local runs
only — the docs are switched off once `IAP_AUDIENCE` is set).

`python3 main.py` is shorthand for `python3 -m uvicorn main:app --reload --port 5659`.

## 4. Data refresh: every morning at 9:00, plus Refresh from DB

The dashboards (Status, MDND, Reconciliation, and the Analytics tabs built on
them) don't query the databases on each page view. They read a cached copy,
which is re-pulled from the SCM and ERP databases **every day at 9:00 IST**
(`daily_refresh.py`):

- **On schedule:** a background thread in the server re-pulls every cache at
  9:00 (the full scans take a few minutes, so expect the new data by about
  9:10). A page left open overnight picks up the new data by itself; the top
  bar checks every 5 minutes.
- **Catch-up:** data counts as out of date once it was fetched before the
  latest 9:00. If the server was off or asleep at 9:00, the first page view
  (or the server starting) after that refreshes it, so the morning data never
  waits for the next day.
- **Refresh from DB** in the top bar re-pulls everything now; each panel's own
  Refresh from DB re-pulls just that panel. The top bar also shows when the
  data was last pulled and when the next auto-refresh is (hover it for each
  source).

Change the time with `DAILY_REFRESH_AT=07:30` (24-hour, India time) in `.env`
or the server's environment; a malformed value stops the server at startup.

When it goes live, the 9:00 run only fires on its own if the server process
is running at 9:00:
- **Cloud Run:** deploy with `--min-instances=1 --no-cpu-throttling`, or the
  instance may be asleep at 9:00 and its CPU is paused between requests. Without
  those, the catch-up above still refreshes on the first visit after 9:00.
- **One process:** run a single uvicorn worker (the default). Each worker would
  run its own 9:00 refresh against the databases.

## 5. Running it on a server

The panel has no login of its own: staff sign in through Google
**Identity-Aware Proxy (IAP)**, and `security.py` checks IAP's signed token on
every request. Until `IAP_AUDIENCE` is set, the app **only answers requests
from the machine it runs on** — anyone else gets a 403 — so a server started
without it fails closed instead of exposing the data.

1. Deploy it private (Cloud Run `--no-allow-unauthenticated`) and ask an owner
   to turn on IAP for it, allowing the company Google group/domain.
2. Set `IAP_AUDIENCE` to the IAP audience of that service — for Cloud Run with
   IAP it is `/projects/<PROJECT_NUMBER>/locations/asia-south1/services/<service>`;
   behind a load balancer `/projects/<PROJECT_NUMBER>/global/backendServices/<id>`.
   Only `@supertails.com` accounts are let in; change with
   `ALLOWED_EMAIL_DOMAINS=supertails.com,other.com`.
3. Run without `--reload`:
   `python3 -m uvicorn main:app --host=0.0.0.0 --port=8080 --no-server-header`.
4. Give it secrets through the platform (Secret Manager → env vars), never by
   copying `.env` or `service_account.json` onto the server. `.dockerignore` /
   `.gcloudignore` keep those files, the SQLite DB and the cache files out of
   any image or upload.

What else is on, everywhere:

- **Cross-site writes are refused** (403): a POST must come from the panel's
  own page, so another site open in the same browser can't submit entries or
  trigger refreshes as the signed-in user.
- **Every write is logged** as `[audit] <email> POST <path> -> <status>`.
- **Security headers** on every response: a Content-Security-Policy (scripts
  only from the panel itself), `X-Frame-Options: DENY`, `nosniff`, HSTS, and
  `Cache-Control: no-store` on `/api/*`.
- **Forced refreshes** (the Refresh from DB buttons, top bar included) run at
  most once per 10 minutes per data source, since each one re-runs the full
  scans on the SCM and ERP databases. The 9:00 run doesn't count against that.
- **Uploads** must be images/PDF (POD) or videos (Video); the Drive file type
  comes from the checked extension. Text fields are length-capped.
- **CSV export** prefixes cells starting with `= + - @` with `'` so a crafted
  courier remark can't run as a formula in Excel / Sheets.

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
| `daily_refresh.py` | The 9:00 IST daily refresh: when a cache counts as out of date, and the thread that re-pulls them all on schedule |
| `rto_delivered.py`, `rto_mdnd.py`, `rto_recon.py` | The RTO Status / MDND / Reconciliation queries, each cached to a JSON file and served from memory |
| `dropdown_cache.py` | CSV-backed cache (`dropdown_cache.csv`) for the four dropdowns above — see below |
| `entries_store.py` | Local SQLite (`rto_entries.db`) — the source of truth for the All Entries tab |
| `google_services.py` | Drive upload + Sheet append; uses `service_account.json` if present, else falls back to OAuth (`credentials.json` / `token.json`) |
| `main.py` | FastAPI app: the `/api/*` endpoints, and serves `static/` (mounted last, at `/`) |
| `security.py` | Who may use the panel (IAP token check, or this machine only), cross-site write blocking, audit log, security headers |
| `static/` | Frontend: `index.html` + `css/` + `js/` — plain static files (vanilla HTML/CSS/JS, no templating, no build step) |
| `archive/flask-version/` | The pre-FastAPI (Flask) `app.py`, template and tests, kept for reference |

This follows the standard layout for internal portals (see
`analytics-scripts/dashboards/README.md`): a small FastAPI app that serves a
static UI from `static/` and answers its data requests under `/api/*`, so
credentials and queries stay server-side and the page and API share one origin.

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

  A background refresh (kicked off when the server starts, and again,
  throttled to once per 30 min, whenever the dashboard is opened — the page
  calls `/api/status` on load) appends any
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
- The cache-warming calls live in the app's `lifespan` startup hook in
  `main.py`, not at module level, specifically so that importing `main.py` (a
  routes check, a linter, a test) can never trigger a live DB query as a side
  effect — only actually running the server does.
- Every API error answers `{"error": "<message>"}` (including bad query
  parameters, which are a 400 rather than FastAPI's default 422) — that is the
  message the frontend shows.
- **AWB is re-verified against the database on submit** (not just picked from
  the suggestion list) so a typo, or a value that's fallen out of date in the
  cache, can't silently save a non-existent AWB.
- Dates use native `<input type="date">` pickers (stored as `YYYY-MM-DD`).

## Known limitations

- `python3 main.py` (auto-reload, bound to localhost) is meant for local use;
  for always-on or multi-user access, see section 5.
- On Cloud Run specifically, three things in this copy don't fit yet (the
  `analytics-scripts/services/rto_panel` copy already solves them): entries
  live in a local SQLite file that is lost whenever an instance is replaced,
  Cloud Run caps request bodies at 32 MiB (so large videos must go straight
  from the browser to Drive), and the cache-refresh threads stall when Cloud
  Run pauses CPU between requests.
- Upload size cap is 500 MB per request (POD + video combined); adjust
  `MAX_CONTENT_LENGTH` in `main.py` if needed.
- Only one Google account can be connected at a time (single `token.json`).
- `dropdown_cache.csv` is already ~55 MB (≈1.9M rows) and, being append-only,
  will keep growing as new AWBs/SKUs show up over time. Parsing it takes
  ~1.3s the first time any process touches it after a restart (done in the
  background, off the request path) — fine today, but worth revisiting
  (e.g. pruning, or a real local index instead of CSV) if it grows much
  further.
- To manually force a full cache refresh without starting the server (e.g.
  right after deploying), run `python3 dropdown_cache.py`.
