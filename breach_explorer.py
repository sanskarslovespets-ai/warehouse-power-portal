"""Breach Explorer: the SLA Breach Explorer's six tabs, served by this panel behind its own sign-in.

Same numbers as breach-explorer.apps.internal.supertails.com: they come from that dashboard's own export
scripts, copied unchanged into breach_pipeline/ (its README says from where, and how to update them). Each
export writes one tab's data, a dictionary-encoded aggregate, as JSON, and the tab filters it in the browser
(static/js/be_*.js) the way the original page does. What differs is around them:
- who sees it: this panel's sign-in, and each tab only for people given it on the Users page (users.TABS),
  where the original has Google sign-in and every tab for everyone;
- delivery: one tab's data per request, gzipped, where the original page carries all six (~10 MB);
- refresh: this panel's daily run and Refresh from DB (daily_refresh.py, main.DB_CACHES), with each tab's data
  swapped in on its own. An export that fails (say the ERP replica refuses connections) leaves its tab on the
  previous data while the others update, where the original rebuilt all or nothing. Each tab's Refresh data
  re-runs only that tab's export (refresh_tab), where the original's rebuilt all six.

The exports run one after another, each in its own Python process: the breach export pulls ~3.5 months of
deliveries into memory, and that memory goes back to the system when the process ends. A full run takes about
6-10 minutes. They read scm (SCM_DB_*) and, for Complaints, RTO and Warehouse Exceptions, the ERP replica
(DN_DB_*).

Also here: the Connection view and RAD to OFD tabs' Export CSV, which reads shipment-level rows straight from
cp_database_N because the tabs only hold aggregates (upstream's service/app/detail.py).

And the same site's other page, Warehouse Exceptions (breach-explorer.apps.internal.supertails.com/warehouse),
which is the Warehouse Exceptions tab under Warehouse Dashboards (static/js/wh_exceptions.js): one dataset,
"warehouse", built by breach_pipeline/scripts/build_warehouse_data.py from that page's three exports. Like
upstream's page it is all or nothing: if one of the three fails, the tab keeps the previous data.
"""
import gzip
import json
import os
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

import pymysql

import daily_refresh
from rto_process import get_scm_connection

HERE = Path(__file__).parent
PIPELINE = HERE / "breach_pipeline"
CACHE_DIR = HERE / "breach_cache"  # each dataset's <name>.json.gz as served, and state.json
STATE_FILE = CACHE_DIR / "state.json"

# dataset -> (export script, the file it writes in breach_pipeline/data/, the tab it feeds), in run order.
# Pendency's shipment list goes before its summary, so by the time the tab has a new summary, the shipments its
# Export CSV reads are as new. The two that also read the ERP replica go last.
DATASETS = {
    "breach": ("export_breach_dashboard_data.py", "breach_dashboard_data.json", "Breach analysis"),
    "pendency-detail": ("export_pendency_detail.py", "pendency_detail.json", "Pendency (shipment detail)"),
    "pendency": ("export_pendency_data.py", "pendency_data.json", "Pendency"),
    "connection": ("export_connection_data.py", "connection_data.json", "Connection view"),
    "radofd": ("export_rad_ofd_data.py", "rad_ofd_data.json", "RAD to OFD"),
    "complaints": ("export_complaints_data.py", "complaints_data.json", "Complaints"),
    "rto": ("export_rto_data.py", "rto_data.json", "RTO"),
    "warehouse": ("build_warehouse_data.py", "warehouse_data.json", "Warehouse Exceptions"),  # ERP replica too
}
WAREHOUSE_PARTS = ("notfound", "pendency", "complaints")  # the warehouse dataset: one payload per export
# What each tab's Refresh data re-runs, by the dataset the tab shows: Pendency's also re-pulls the shipment list
# behind its Export CSV, so the file keeps matching the table.
TAB_EXPORTS = {
    "breach": ("breach",),
    "pendency": ("pendency-detail", "pendency"),
    "complaints": ("complaints",),
    "rto": ("rto",),
    "connection": ("connection",),
    "radofd": ("radofd",),
    "warehouse": ("warehouse",),
}
BREACH_FACT_WIDTH = 31  # columns per breach fact row; static/js/be_breach.js reads them by position
STEP_TIMEOUT_SECONDS = 1800  # one export; the breach export's scan normally takes a few minutes
AUTO_RETRY_SECONDS = 1800  # how soon a run that left some tab on old data is retried, when someone looks

_run_lock = threading.Lock()  # one run of the exports at a time
_state_lock = threading.Lock()
_loaded = False
_fetched = {}  # dataset -> when its current data was swapped in (epoch seconds)
_errors = {}  # dataset -> why its last export failed, until one succeeds
_payloads = {}  # dataset -> (gzipped JSON, ETag), read from CACHE_DIR when first asked for
_queue = []  # the exports waiting their turn, in order; a run takes on whatever joins the queue before it ends
_running = None  # the dataset being exported right now
_run_started_at = None
_last_attempt_at = 0.0


class ExportFailed(Exception):
    pass


def _ensure_loaded():
    """Picks up the data the last run left in CACHE_DIR, so a restart serves it straight away."""
    global _loaded
    with _state_lock:
        if _loaded:
            return
        _loaded = True
        try:
            saved = json.loads(STATE_FILE.read_text())["fetched"]
        except (OSError, ValueError, KeyError, TypeError):
            saved = {}
        for name in DATASETS:
            if isinstance(saved.get(name), (int, float)) and (CACHE_DIR / f"{name}.json.gz").exists():
                _fetched[name] = saved[name]


def _save_state():
    with _state_lock:
        state = {"fetched": dict(_fetched)}
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, STATE_FILE)


def _etag(name, fetched_at):
    return f'"{name}-{int(fetched_at * 1000)}"'


def _tail(text, lines):
    return "\n".join((text or "").strip().splitlines()[-lines:])


def _problem(name, data):
    """Why an export's JSON can't be served, or None. The checks upstream's refresh_dashboard.py makes
    before it puts a payload in the page."""
    if name == "warehouse":
        missing = [part for part in WAREHOUSE_PARTS if not isinstance(data.get(part), dict)]
        if missing:
            return f"it has no {' or '.join(missing)} data"
        if not data["notfound"].get("base"):
            return "it found no delivery notes raised in its window"
        if not data["pendency"].get("asOf"):
            return "it has no pendency snapshot time"
        return None
    if name == "pendency-detail":
        rows = data.get("rows")
    elif name == "radofd":
        rows = (data.get("by_city") or {}).get("facts")
    else:
        rows = data.get("facts")
    if not rows:
        return "it produced no rows"
    if name == "breach" and len(rows[0]) != BREACH_FACT_WIDTH:
        return f"its rows have {len(rows[0])} columns where the page expects {BREACH_FACT_WIDTH}"
    if name == "pendency" and not data.get("generated_at_ist"):
        return "it has no snapshot time"
    return None


def _publish(name, raw):
    """Swaps in one dataset's new data: on disk for the next restart, in memory for the next request."""
    gz = gzip.compress(raw, compresslevel=6)
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / f"{name}.json.gz"
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(gz)
    os.replace(tmp, path)
    now = time.time()
    with _state_lock:
        _fetched[name] = now
        _errors.pop(name, None)
        _payloads[name] = (gz, _etag(name, now))
    _save_state()


def _fail(name, why):
    print(f"[breach-explorer] {name}: kept the previous data - {why}", flush=True)
    with _state_lock:
        _errors[name] = why[:300]


def _export(name):
    script, output, _tab = DATASETS[name]
    out = PIPELINE / "data" / output
    started = time.time()
    try:
        out.parent.mkdir(exist_ok=True)
        out.unlink(missing_ok=True)  # so a failed export can't leave the last run's file to be served as new
        run = subprocess.run(
            [sys.executable, str(PIPELINE / "scripts" / script)],
            cwd=PIPELINE, capture_output=True, text=True, timeout=STEP_TIMEOUT_SECONDS,
        )
        if run.returncode != 0:
            print(f"[breach-explorer] {script} failed:\n{_tail(run.stderr, 20)}", flush=True)
            raise ExportFailed(_tail(run.stderr, 1) or f"{script} stopped with exit code {run.returncode}")
        raw = out.read_bytes()
        problem = _problem(name, json.loads(raw))
        if problem:
            raise ExportFailed(f"{script} ran, but {problem}")
        _publish(name, raw)
        print(f"[breach-explorer] {name}: new data in {time.time() - started:.0f}s", flush=True)
    except subprocess.TimeoutExpired:
        _fail(name, f"{script} took over {STEP_TIMEOUT_SECONDS // 60} minutes and was stopped")
    except ExportFailed as e:
        _fail(name, str(e))
    except Exception as e:
        traceback.print_exc()
        _fail(name, f"{type(e).__name__}: {e}")


def _enqueue(names):
    """Queues these exports, but none twice, nor the one running now. Called with _state_lock held."""
    for name in names:
        if name not in _queue and name != _running:
            _queue.append(name)


def refresh_cache(force=False):
    """Exports the data that's due (every dataset when forced). It joins the queue, which is worked through one
    export at a time: by this call, or by a run already going, which takes it on before it ends. With
    refresh_tab, the only way the exports start."""
    _ensure_loaded()
    with _state_lock:
        _enqueue(name for name in DATASETS if force or daily_refresh.is_due(_fetched.get(name)))
    _run()


def refresh_tab(tab, cooldown):
    """A tab's Refresh data: queues the exports behind it (TAB_EXPORTS), to start now or straight after those
    already going, and returns None. A tab whose export is already queued or running just carries on. Queues
    nothing if the tab's data came in under `cooldown` seconds ago, and returns how many seconds ago that was."""
    _ensure_loaded()
    exports = TAB_EXPORTS[tab]
    with _state_lock:
        if _running not in exports and not any(name in _queue for name in exports):
            age = time.time() - _fetched[tab] if tab in _fetched else None
            if age is not None and age < cooldown:
                return age
            _enqueue(exports)
    refresh_cache_async()
    return None


def _run():
    """Works through the queue, one export at a time, until it's empty. Returns at once if another thread is
    already doing that: it takes on whatever is queued before it finishes."""
    global _running, _run_started_at, _last_attempt_at
    while _run_lock.acquire(blocking=False):
        try:
            with _state_lock:
                if not _queue:
                    return
                print(f"[breach-explorer] exporting {', '.join(_queue)}", flush=True)
                _run_started_at = _last_attempt_at = time.time()
            while True:
                with _state_lock:
                    if not _queue:
                        break
                    _running = name = _queue.pop(0)
                _export(name)
        finally:
            with _state_lock:
                _running = _run_started_at = None
            _run_lock.release()
        # Round again: something queued as this run was ending found the lock still held and left it to this one.


def refresh_cache_async(force=False):
    threading.Thread(target=refresh_cache, args=(force,), daemon=True, name="breach-explorer").start()


def is_refreshing():
    if _run_lock.acquire(blocking=False):
        _run_lock.release()
        return False
    return True


def _catch_up():
    """Starts the exports when some tab's data missed its morning run (the server was off or asleep then)."""
    with _state_lock:
        due = any(daily_refresh.is_due(_fetched.get(name)) for name in DATASETS)
    if due and not is_refreshing() and time.time() - _last_attempt_at > AUTO_RETRY_SECONDS:
        refresh_cache_async()


def snapshot():
    """For the top bar (main.data_status): when new data last came in, whether a run is going, what failed."""
    _ensure_loaded()
    _catch_up()
    with _state_lock:
        fetched = [t for t in _fetched.values() if t]
        errors = [f"{DATASETS[name][2]}: {why}" for name, why in _errors.items()]
    return {"fetched_at": max(fetched) if fetched else None, "refreshing": is_refreshing(), "error": "; ".join(errors) or None}


def status():
    """Each dataset's freshness, and what's being exported, for the Breach Explorer tabs (/api/breach-explorer/status)."""
    _ensure_loaded()
    _catch_up()
    with _state_lock:
        return {
            "refreshing": _run_started_at is not None,
            "running": _running,
            "queued": list(_queue),
            "running_for": int(time.time() - _run_started_at) if _run_started_at else None,
            "datasets": {name: {"fetched_at": _fetched.get(name), "error": _errors.get(name)} for name in DATASETS},
        }


def payload(name):
    """(gzipped JSON, ETag) of a dataset as last swapped in, or None if it has never been built."""
    _ensure_loaded()
    with _state_lock:
        if name in _payloads:
            return _payloads[name]
        fetched = _fetched.get(name)
    if fetched is None:
        return None
    try:
        gz = (CACHE_DIR / f"{name}.json.gz").read_bytes()
    except OSError:
        return None
    with _state_lock:
        return _payloads.setdefault(name, (gz, _etag(name, fetched)))


# ------------------------------------------------------------------ shipment-level Export CSV
# Copied from upstream's service/app/detail.py: the bucket CASE expressions must stay identical to the ones in
# breach_pipeline/scripts/export_connection_data.py and export_rad_ofd_data.py. A detail row that disagrees
# with the summary it was exported from is worse than no export at all.

AWB_ROW_CAP = 200_000
AWB_MAX_DAYS = 92  # each export scans cp_database_N over its dates; the tabs themselves hold 30-60 days
AWB_TIMEOUT_SECONDS = 600
_awb_slots = threading.BoundedSemaphore(2)  # at most two such scans at once, whoever asks

# %% not %: the statement is always executed with parameters, so pymysql %-formats it.
MOTHER_WH = "from_warehouse LIKE 'MWH!_%%' ESCAPE '!'"
AWB_COURIERS = ("Bluedart", "Delhivery")

CONNECTION_BUCKET = """
      CASE
        WHEN origin_hub_out_timestamp IS NULL THEN 'Not connected'
        WHEN origin_hub_out_timestamp <=
             TIMESTAMP(DATE_ADD(DATE(pickup_date), INTERVAL 1 DAY), '08:00:00')
          THEN 'Day 1'
        WHEN origin_hub_out_timestamp <=
             TIMESTAMP(DATE_ADD(DATE(pickup_date), INTERVAL 2 DAY), '08:00:00')
          THEN 'Day 2'
        ELSE 'Day 3+'
      END"""

RADOFD_BUCKET = """
      CASE
        WHEN out_for_delivery_1st_attempt IS NULL THEN 'No OFD'
        WHEN DATEDIFF(DATE(out_for_delivery_1st_attempt),
                      DATE(destination_hub_in_scan_time)) <= 0 THEN 'Same day'
        WHEN DATEDIFF(DATE(out_for_delivery_1st_attempt),
                      DATE(destination_hub_in_scan_time)) = 1 THEN 'Next day'
        ELSE 'D+2 or later'
      END"""

AWB_SPECS = {
    "connection": {
        "date_col": "pickup_date",
        "select": """
            awb, order_id, courier_partner AS courier, from_warehouse AS warehouse,
            account_code, shipment_weight AS weight_g,
            pickup_date, origin_hub_out_timestamp AS connected_at,
            TIMESTAMPDIFF(MINUTE, pickup_date, origin_hub_out_timestamp) AS mins_to_connect,
            pickup_city, drop_city, drop_pincode,
            clickpost_unified_status AS status,""",
        "bucket": CONNECTION_BUCKET,
        "cols": ["awb", "order_id", "courier", "warehouse", "account_code", "weight_g",
                 "pickup_date", "connected_at", "mins_to_connect", "pickup_city",
                 "drop_city", "drop_pincode", "status", "bucket"],
    },
    "radofd": {
        "date_col": "destination_hub_in_scan_time",
        "select": """
            awb, order_id, courier_partner AS courier, from_warehouse AS warehouse,
            account_code, shipment_weight AS weight_g,
            destination_hub_in_scan_time AS reached_destination,
            out_for_delivery_1st_attempt AS first_ofd,
            TIMESTAMPDIFF(MINUTE, destination_hub_in_scan_time,
                          out_for_delivery_1st_attempt) AS mins_to_ofd,
            drop_city, drop_pincode,
            clickpost_unified_status AS status,""",
        "bucket": RADOFD_BUCKET,
        "cols": ["awb", "order_id", "courier", "warehouse", "account_code", "weight_g",
                 "reached_destination", "first_ofd", "mins_to_ofd",
                 "drop_city", "drop_pincode", "status", "bucket"],
    },
}


class Busy(Exception):
    """Two shipment exports are already running."""


def _slab_clause(slabs):
    """Weight slabs are derived, not stored, so they can't be an IN list."""
    parts = []
    for s in slabs:
        if s == "0-3kg":
            parts.append("(shipment_weight IS NOT NULL AND shipment_weight <= 3000)")
        elif s == "3+kg":
            parts.append("(shipment_weight > 3000)")
        elif s == "unknown":
            parts.append("(shipment_weight IS NULL)")
    return ("(" + " OR ".join(parts) + ")") if parts else None


def awb_rows(dataset, date_from, date_to, couriers=(), warehouses=(), accounts=(), slabs=()):
    """The shipments behind the Connection view or RAD to OFD tab for its dates and filters, as
    (columns, rows, truncated). Same scope as the tab: mother warehouses, Bluedart and Delhivery; no PII."""
    spec = AWB_SPECS[dataset]
    where = [f"{spec['date_col']} >= %s", f"{spec['date_col']} < DATE_ADD(%s, INTERVAL 1 DAY)", MOTHER_WH]
    params = [date_from, date_to]

    chosen = [c for c in couriers if c in AWB_COURIERS] or list(AWB_COURIERS)
    where.append("courier_partner IN (%s)" % ", ".join(["%s"] * len(chosen)))
    params.extend(chosen)
    for column, values in (("from_warehouse", warehouses), ("account_code", accounts)):
        if values:
            where.append("%s IN (%s)" % (column, ", ".join(["%s"] * len(values))))
            params.extend(values)
    slab = _slab_clause(slabs)
    if slab:
        where.append(slab)

    sql = "SELECT {sel}{bucket} AS bucket FROM cp_database_N WHERE {where} LIMIT {cap}".format(
        sel=spec["select"], bucket=spec["bucket"], where=" AND ".join(where), cap=AWB_ROW_CAP + 1)

    if not _awb_slots.acquire(blocking=False):
        raise Busy()
    try:
        conn = get_scm_connection(read_timeout=AWB_TIMEOUT_SECONDS)
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                try:  # the server's own statement cap, to match (see breach_pipeline/db/connection.py)
                    cur.execute("SET SESSION MAX_EXECUTION_TIME = %s", (AWB_TIMEOUT_SECONDS * 1000,))
                except pymysql.err.OperationalError:
                    pass
                cur.execute(sql, params)
                rows = cur.fetchall()
        finally:
            conn.close()
    finally:
        _awb_slots.release()

    truncated = len(rows) > AWB_ROW_CAP
    print(f"[breach-explorer] {dataset} shipment export: {min(len(rows), AWB_ROW_CAP)} rows "
          f"({date_from}..{date_to}){' TRUNCATED' if truncated else ''}", flush=True)
    return spec["cols"], rows[:AWB_ROW_CAP], truncated
