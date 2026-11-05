# Warehouse Power Portal

A web app for the warehouse team: logging RTO / RPU / Warehouse / Inward damage
entries, and dashboards over the SCM and ERP databases.

The sidebar groups the sections by area: **RTO** (Analytics, Process, Status,
MDND, Reconciliation), **Breach Explorer** (Breach analysis, Pendency,
Complaints, RTO, Connection view, RAD to OFD; see below), **Warehouse
Dashboards** (Control Tower, Warehouse Exceptions; see below) and, for admins, **Users**. Click an area to open or close it; the page remembers which you
closed.

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

Open **http://localhost:5659** in a browser and sign in with a username and
password from `scm.warehouse_users` (see *Sign-in* below). It auto-reloads when
a `.py` file changes; edits under `static/` just need a browser refresh. Every
API endpoint is listed, and can be tried out, at **http://localhost:5659/docs**
once signed in (local runs only — the docs are switched off once `IAP_AUDIENCE`
or `ALLOW_WITHOUT_IAP` is set).

### Sign-in

Nothing in the panel opens without signing in: a page load goes to `/login`,
an `/api/` call gets a 401. The username and password are checked against the
**`warehouse_users`** table in the SCM database (`username`, `password`).
Admins add people and set what they can open on the **Users** page (below).

- The username ignores case; the password must match exactly. A wrong username
  and a wrong password get the same message ("Incorrect username or
  password."), so the page doesn't reveal which usernames exist.
- 5 wrong passwords for one username within 15 minutes lock that username for
  15 minutes.
- A sign-in lasts 12 hours (`SESSION_HOURS` in `auth.py`) and renews itself
  while the panel is in use, so a session nobody uses ends 6 to 12 hours after
  its last request; or until **Sign out** in the top bar. Someone signed out
  this way lands on the sign-in page with "You were signed out", not a silent
  bounce. Removing someone, changing their password or their tabs reaches them
  within a minute, even while they're signed in: a new password signs out
  every session that used the old one.
- The session is an HttpOnly cookie signed with `SESSION_SECRET`, so it can't
  be forged or edited. Without that setting, everyone is signed out whenever
  the server restarts or is redeployed. On Cloud Run it comes from the Secret
  Manager secret `warehouse-portal-session-secret` (`cloudbuild.yaml`).
- Every sign-in and wrong attempt is logged (`[auth] ...`), the audit log of
  writes names the warehouse user, and every change on the Users page is
  logged as `[users] ...`.

### Users & access

**Users → Users & access** (admins only) lists everyone who can sign in, with
their role and the tabs they can open. **+ Add user** creates someone with a
password; **Edit** changes their role, tabs or password, or deletes them.

| Role | Sees | Can change data (new entries, MDND inputs, Refresh from DB, Refresh data) |
|---|---|---|
| **Admin** | Every tab, and Users | Yes, and manages everyone's access |
| **Editor** | The tabs ticked for them | Yes, in those tabs |
| **Viewer** | The tabs ticked for them | No: look and download only |

- The server enforces this on every request (`users.py`), not just the page:
  someone without a tab gets a 403 from its API, and an API not yet given a
  tab in `users.API_TABS` is admins-only, so a new endpoint starts closed.
- **`ADMIN_USERS`** (in `.env`, or an environment variable on a server) names
  users who are always admins, whatever the table says: the first admin, and a
  way back in. Set to `Sanskar` locally.
- Roles and tabs live in **`scm.warehouse_user_access`** (`username`, `role`,
  `tabs`, `updated_at`, `updated_by`), created by the first change saved on
  the Users page. Until then everyone except `ADMIN_USERS` has no tabs.
- Someone added straight in the database (`INSERT INTO warehouse_users ...`)
  can sign in but sees no tabs until an admin gives them some.
- An admin can't change their own role or delete themselves, so the panel
  can't be left without an admin. Usernames are 3 to 64 letters, digits, dots,
  dashes, underscores or @, and a look-alike of an existing one (`Rahul` vs
  `rahul`) is refused. Passwords are at least 8 characters.

### Breach Explorer

The six tabs of the SLA Breach Explorer (`breach-explorer.apps.internal.supertails.com`),
behind this panel's sign-in instead of Google sign-in, and given to people tab
by tab on the Users page like any other:

| Tab | Question it answers |
|---|---|
| **Breach analysis** | Of what was delivered, how much missed the promise (Delivered or RAD basis)? |
| **Pendency** | What is open now: already late, or due today / tomorrow? Export CSV is one row per shipment. |
| **Complaints** | Which last-mile faults customers raise (`CX_data.CDC_Nugget`), per courier and against delivered volume |
| **RTO** | Of the shipments that reached an outcome, how many came back, what they were worth, and what reached the warehouse |
| **Connection view** | Pickup to origin-hub-out against the 08:00 D+1 cutoff (Bluedart, Delhivery, mother warehouses) |
| **RAD to OFD** | Destination-hub arrival to first out-for-delivery, by date, city and hub |

- **Same numbers as the original:** the data comes from the original's own
  export scripts, copied unchanged into `breach_pipeline/` (its README says
  from which commit, and how to update them). The tabs are a port of its page
  to `static/js/be_*.js`: same filters, tables and notes, checked table by
  table against the original page built from the same data.
- **Refresh:** the seven exports run with the rest of the daily refresh (9:00
  IST) and with Refresh from DB, one at a time, and take about 5-10 minutes
  together. Each tab's data is swapped in as soon as its export finishes. If
  one fails (say the ERP replica refuses connections), that tab keeps its
  previous data, says so, and is retried within half an hour while someone
  has the panel open. Each tab shows when its data is from.
- **A tab opened before its data exists** (the first run after a restart or a
  deploy: the data lives on the server's own disk) jumps the queue: its export
  runs as soon as the one running finishes, instead of after every other tab's.
  Two such tabs are built in the order they were opened. A server that shuts
  down or restarts stops the export it was running, along with anything that
  export started, rather than leaving it running on its own.
- **Refresh data**, at the top of each tab (editors and admins), re-runs just
  that tab's export: a few seconds for Connection view and RAD to OFD, about a
  minute for Breach analysis, 2-3 minutes for RTO. Pendency's also re-pulls the
  shipment list behind its Export CSV, so the file keeps matching the table.
  The tab shows the new data by itself when it's in. Pressed while other
  exports are running, it waits its turn ("Refresh queued after RTO"); pressed
  within 10 minutes of the tab's data coming in, it says how long to wait.
- **Data:** `breach_cache/` holds what the tabs are served (gzipped JSON,
  about 3 MB in all); `breach_pipeline/data/` is the exports' scratch space.
  Both are local files, kept out of git and out of server images.
- **Access:** each tab's data comes from its own `/api/breach-explorer/<tab>`
  endpoint, so someone given only Pendency can't fetch Breach analysis's data.
  Connection view and RAD to OFD's **Export CSV** runs a shipment-level query
  (at most 92 days, two at a time).
- **Differences from the original page**, all small: the date range and
  Delivered / RAD basis sit in the Breach analysis tab's filter bar, as the
  sidebar replaces its tab strip. Every Export CSV saves a file; on the original,
  Pendency's opened a copy-and-paste box, and By lane's and RTO value's did
  nothing. Pendency's export also follows the promise-date filter now. The
  original's Refresh data rebuilt all six tabs (6-10 minutes) for anyone; here
  it refreshes the tab you're on, for editors and admins.

### Warehouse Dashboards → Control Tower

The "Control Tower" Google Sheet's **Dashboard** and **Warehouse Pendency**
tabs, built from the databases instead of the sheet's ERP, CP and EDD dumps
(`control_tower.py`, `static/js/control_tower.js`). One sidebar item, with the
two as views inside it; given to people on the Users page like any other tab.

| View | What it shows |
|---|---|
| **Dashboard** | Delivery notes per facility by ideal pickup date (each of the last 7 days, earlier, later): picking pendency, AWB not generated, pickup pending, hyperlocal, or any one status; and by order date, Unfulfilled WH, Hold and shipped DNs the promise engine gave no EDD |
| **Warehouse Pendency** | The mother warehouses' DNs with no AWB yet, still being picked or packed: by warehouse and DN by DN, with days late and Export CSV |

- **Data:** the sheet's three dump queries, over whole days in India time:
  - the **ERP dump** (sales order items of today and the 6 days before, with their DNs and pick
    lists) on the ERP replica. Its CTEs run one by one and are joined in Python, because MariaDB
    runs the query as written by brute-force joins (still going after 12 minutes); see the top of
    `control_tower.py`;
  - the **CP dump** on `scm.cp_database`, for each AWB's Clickpost status;
  - the **EDD dump** on the promise engine's `orderpromises` (MongoDB), for each order's cutoff
    days (TOTAL_CUTOFF) at each warehouse. Set `MONGO_URI` (and `MONGO_DB` if the URI names no
    database). Without it, the page still builds, puts every ideal pickup date on the noon rule,
    and says so.

  The CP and EDD dumps go one day further back than the ERP's. The sheet's derived columns (ideal
  pickup date, status, courier, facility type) and its Facts tab are in the same file.
- **Refresh:** every 30 minutes, whether or not anyone has the tab open
  (counted from when the last build started), plus the 9:00 run, Refresh from
  DB, and the tab's own Refresh data (editors and admins; not again within 10
  minutes). A build takes about 3 minutes (all three sources at once). A failed
  build keeps the previous data, says why, and is tried again after 10 minutes.
  An open tab picks up new data within 5 minutes, through the top bar's check.
  The data is served from `control_tower_cache.json.gz`, kept out of git.
- **Differences from the sheet:**
  - Facilities come from the data, so new darkstores and warehouses (Kolkata Dankuni, Chennai
    Kovur) appear without editing a list.
  - Totals add up every row, and courier names have one spelling each.
  - The CP dump includes today; the sheet's `order_date <= '<today>'` compares text and left
    today's AWBs out.
  - A promise for a warehouse missing from the Facts tab's list still counts, under its own code.
    The sheet dropped those (about 25 darkstores and dropship partners).
  - The sheet's DS Dashboard and Dashboard 2.0 tabs, and its Warehouse to Darkstores block (its
    route list no longer matches any data), are not here.

### Warehouse Dashboards → Warehouse Exceptions

The SLA Breach Explorer site's other page,
`breach-explorer.apps.internal.supertails.com/warehouse`, served here behind the
panel's own sign-in like the Breach Explorer tabs. One sidebar item
(**Exceptions**), with the page's five views inside it; given to people on the
Users page as "Warehouse Exceptions".

| View | What it counts |
|---|---|
| **Not found** | Stock the packer couldn't find during bin determination, moved into the `Suspense-MWH_*` bin against a delivery note: by warehouse, bin and SKU, over a date range (last 16 weeks) |
| **Packing pendency** | Open delivery notes still To Be Picked or Under Picking (IWT excluded), aged against their ideal dispatch date: the team's "Ideal Dispatch Date Pendency" sheet |
| **Putback pendency** | Notes in Put Back Initiated: stock pulled for an order that didn't ship |
| **RTO initiated pendency** | Return Initiated notes of type RTO Return (RVP excluded) |
| **Complaints** | Wrong / missing / damaged / expired tickets traced to the shipping warehouse, and damage by courier |

- **Data:** that page's own three export scripts, copied unchanged into
  `breach_pipeline/scripts/export_warehouse_*.py`, run by
  `build_warehouse_data.py` into one dataset (`warehouse`) that
  `breach_explorer.py` serves, refreshes and keeps like the Breach Explorer's.
  They read the ERP replica and `scm` (complaints, ClickPost). A run takes about
  2 minutes. Like the original, it's all or nothing: if one export fails, the
  page keeps the previous data and says why.
- **Dispatch cutoff:** packing pendency's ideal dispatch date uses the promise
  engine's Standard A cutoff (`promiseEngine.warehouse_cutoffs`, read with
  `PROMISE_DB_*`). Without those settings it uses 14:00, which is what all
  seven warehouses are set to today; the original does the same when it can't
  reach that host.
- **Refresh:** the 9:00 run, Refresh from DB, and the page's Refresh data
  (editors and admins; not again within 10 minutes).
- **Same numbers:** checked against the original page built from the same
  data: every table, note and CSV export, after the same filters, sorts and
  date picks, came out identical.
- **Differences from the original:** Export CSV keeps cells a spreadsheet would
  run as a formula as text, and a fifth warehouse running bin determination gets
  its own colour (the original had four).

`python3 main.py` is shorthand for `python3 -m uvicorn main:app --reload --port 5659`.

## 4. Data refresh: every morning at 9:00, plus Refresh from DB

The dashboards (Status, MDND, Reconciliation, the Analytics tabs built on
them, and the Breach Explorer tabs) don't query the databases on each page
view. They read a cached copy, which is re-pulled from the SCM and ERP
databases **every day at 9:00 IST** (`daily_refresh.py`):

- **On schedule:** a background thread in the server re-pulls every cache at
  9:00 (the full scans take a few minutes, so expect the new data by about
  9:10). A page left open overnight picks up the new data by itself; the top
  bar checks every 5 minutes.
- **Catch-up:** data counts as out of date once it was fetched before the
  latest 9:00. If the server was off or asleep at 9:00, the first page view
  (or the server starting) after that refreshes it, so the morning data never
  waits for the next day.
- **Refresh from DB** in the top bar re-pulls the data behind your own tabs
  now: an RTO-only editor's click leaves the Breach Explorer's longer run alone.
  Each panel's own Refresh from DB re-pulls just that panel, and a Breach
  Explorer tab's Refresh data just that tab. The top bar also
  shows when that data was last pulled and when the next auto-refresh is
  (hover it for each source).

Change the time with `DAILY_REFRESH_AT=07:30` (24-hour, India time) in `.env`
or the server's environment; a malformed value stops the server at startup.

When it goes live, the 9:00 run only fires on its own if the server process
is running at 9:00:
- **Cloud Run:** deploy with `--min-instances=1 --no-cpu-throttling`, or the
  instance may be asleep at 9:00 and its CPU is paused between requests. Without
  those, the catch-up above still refreshes on the first visit after 9:00.
- **One process:** run a single uvicorn worker (the default). Each worker would
  run its own 9:00 refresh against the databases.
- **Memory:** give it 2 GiB. The Breach Explorer's breach export holds about
  3.5 months of deliveries in memory while it runs, which is what the original
  Breach Explorer service is sized for. It runs in a separate process, so that
  memory is freed when the export finishes.

## 5. Running it on a server

Two locks, one after the other. First, staff get through Google
**Identity-Aware Proxy (IAP)** with their company account, and `security.py`
checks IAP's signed token on every request. Until `IAP_AUDIENCE` is set, the
app **only answers requests from the machine it runs on** — anyone else gets
a 403 — so a server started without it fails closed instead of exposing the
data. Then they sign in to the panel itself with a `warehouse_users` username
and password (see *Sign-in* in section 3).

**Not behind IAP yet?** Set `ALLOW_WITHOUT_IAP=1` instead of `IAP_AUDIENCE`.
Anyone can then reach the sign-in page, and the panel's own sign-in is the
only lock. That is how the Cloud Run service `warehouse-portal` runs today
(`cloudbuild.yaml`): with `IAP_AUDIENCE` set but IAP not switched on for the
service, every request answered `{"error":"Sign-in required."}`. Once IAP is
on, swap the setting back; `IAP_AUDIENCE` wins if both are set.

1. Deploy it private (Cloud Run `--no-allow-unauthenticated`) and ask an owner
   to turn on IAP for it, allowing the company Google group/domain.
2. Set `IAP_AUDIENCE` to the IAP audience of that service — for Cloud Run with
   IAP it is `/projects/<PROJECT_NUMBER>/locations/asia-south1/services/<service>`;
   behind a load balancer `/projects/<PROJECT_NUMBER>/global/backendServices/<id>`.
   Only `@supertails.com` accounts are let in; change with
   `ALLOWED_EMAIL_DOMAINS=supertails.com,other.com`.
3. Run without `--reload`:
   `python3 -m uvicorn main:app --host=0.0.0.0 --port=8080 --no-server-header`.
4. Set `SESSION_SECRET` (the same value on every instance) so sign-ins
   survive restarts and work whichever instance answers. Make one with
   `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`. Set
   `ADMIN_USERS` too, or no one can open the Users page.
5. Give it secrets through the platform (Secret Manager → env vars), never by
   copying `.env` or `service_account.json` onto the server. `.dockerignore` /
   `.gcloudignore` keep those files, the SQLite DB and the cache files out of
   any image or upload.

What else is on, everywhere:

- **Cross-site writes are refused** (403): a POST must come from the panel's
  own page, so another site open in the same browser can't submit entries or
  trigger refreshes as the signed-in user.
- **Every write is logged** as `[audit] <email> as <warehouse user> POST <path> -> <status>`
  (`local` in place of the email on a local run, `web` with `ALLOW_WITHOUT_IAP`).
- **Security headers** on every response: a Content-Security-Policy (scripts
  only from the panel itself), `X-Frame-Options: DENY`, `nosniff`, HSTS, and
  `Cache-Control: no-store` on `/api/*`.
- **Forced refreshes** (the Refresh from DB buttons, top bar included) run at
  most once per 10 minutes per data source, since each one re-runs the full
  scans on the SCM and ERP databases. The 9:00 run doesn't count against that.
  A Breach Explorer tab's Refresh data waits until its data is 10 minutes old,
  and its exports run one at a time, however many people press it.
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
| `security.py` | Who may use the panel (IAP token check, or this machine only, then a signed-in session), cross-site write blocking, audit log, security headers |
| `auth.py` | The sign-in: username/password check against `scm.warehouse_users`, signed session cookies, lockout after repeated wrong passwords |
| `users.py` | Roles and tab access (`scm.warehouse_user_access`): which API belongs to which tab, what each role may do there, and the Users page's changes |
| `breach_explorer.py` | The Breach Explorer tabs' and Warehouse Exceptions' data: runs the exports in `breach_pipeline/` (daily, on Refresh from DB, and each tab's own Refresh data), checks and serves each tab's JSON from `breach_cache/`, and the shipment-level Export CSV |
| `breach_pipeline/` | The SLA Breach Explorer's export scripts and mapping workbooks, copied unchanged from `edd-observability` (except `db/connection.py`; see its README) |
| `control_tower.py` | The Control Tower's data: the ERP, CP and EDD dump queries, the sheet's derived columns, and the cached JSON its two views read |
| `static/` | Frontend: `index.html` + `login.html` + `css/` + `js/` — plain static files (vanilla HTML/CSS/JS, no templating, no build step). The Breach Explorer tabs are `js/be_*.js` and `css/breach_explorer.css`; Warehouse Exceptions is `js/wh_exceptions.js` (styles in `css/breach_explorer.css`); the Control Tower is `js/control_tower.js` and `css/control_tower.css` |
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
- `warehouse_users.password` holds plain-text passwords, so anyone who can
  read that table (or a DB backup) can sign in as anyone. Storing salted
  hashes instead (and checking them in `auth.check_password`) would close
  that; it means changing how rows are added to the table.
- The 5-wrong-passwords lockout is counted per server process, so with
  several Cloud Run instances an attacker gets 5 tries per instance.
- Each server process caches people's access for a minute
  (`users.CACHE_SECONDS`): a change made on one instance reaches the others
  within that minute.
- The Breach Explorer's export scripts are a copy. When the original
  dashboard's logic changes in `edd-observability`, copy the changed scripts
  into `breach_pipeline/` (and port any page change to `static/js/be_*.js`, or
  for the warehouse page `static/js/wh_exceptions.js`),
  or the two will drift apart. The original service still runs on its own and
  queries the same databases each morning too.
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
