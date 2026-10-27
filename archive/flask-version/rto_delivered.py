"""RTO Delivered tab: query + file-backed cache.

The source query takes ~45s and returns ~41K rows, so it must never run
inside a page request. Instead:
- The full result is saved to rto_delivered_cache.json after each successful
  fetch and loaded into memory at startup / first use. Opening the tab reads
  that cache instantly.
- If the cache is older than CACHE_TTL_SECONDS, a background refresh is
  started; the old data keeps being served until the new data replaces it
  (stale-while-revalidate). A failed refresh leaves the old cache untouched.
- Date / return_name filtering and pagination run on the in-memory rows, so
  they never touch MySQL.
- Each row is LEFT JOINed (in Python - the two tables live on different DB
  servers) to the Delivery Note table on awb = custom_awb_number to add a
  `status` column. That table is ~2.4M distinct (awb, status) rows with no
  index on custom_awb_number, so the query is a ~4 minute full scan. It is
  therefore refreshed on its own, much slower schedule (DN_CACHE_TTL_SECONDS)
  and only the statuses for our AWBs are kept (dn_status_cache.json); the
  fast main query re-joins against that map every time it runs.
  Most AWBs have several Delivery Notes (e.g. "Return Issued" then "Return
  Completed"), so only the LATEST status per AWB - by the Delivery Note's
  posting_date (ties broken by posting_time, then modified) - is joined on. That keeps one row per source row; an
  AWB with no Delivery Note keeps its row with a blank status.
- Only the very first load (no cache file yet) has to wait for the DB; the
  API reports `loading: true` in that case and the UI polls.
"""

import json
import os
import tempfile
import threading
import time
import traceback
from datetime import datetime

from rto_process import get_dn_connection, get_scm_connection

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(BASE_DIR, "rto_delivered_cache.json")
CACHE_TTL_SECONDS = 3 * 3600
DN_CACHE_FILE = os.path.join(BASE_DIR, "dn_status_cache.json")
DN_CACHE_TTL_SECONDS = 6 * 3600  # the DN scan is heavy on the ERP DB; don't repeat it often
DN_READ_TIMEOUT_SECONDS = 1500
DN_CACHE_VERSION = 3  # 3 = latest by posting_date (2 = latest by modified, 1 = list of statuses)

COLUMNS = [
    "awb",
    "courier_partner",
    "return_name",
    "rto_delivery_date",
    "created_at",
    "pickup_date",
    "latest_remark",
    "return_city",
    "status",  # from tabDelivery Note via the LEFT JOIN
]
SCM_COLUMNS = COLUMNS[:-1]

QUERY = """
select distinct
awb,courier_partner,return_name,rto_delivery_date,created_at,pickup_date, latest_remark, return_city
from cp_database_N where clickpost_unified_status in ('RTO-Delivered')
and pickup_pincode in (122017, 562123, 421302, 410501, 712250, 600128, 500089)
"""

DN_QUERY = """
select custom_awb_number, status, posting_date, posting_time, modified
from `tabDelivery Note`
where custom_awb_number is not null and custom_awb_number <> ''
"""

NO_STATUS = "__none__"  # status filter value for AWBs with no Delivery Note (blank status)

_state_lock = threading.Lock()
_refresh_lock = threading.Lock()
_rows = []  # list of dicts, sorted by rto_delivery_date desc
_fetched_at = None  # epoch seconds of the data currently in memory
_dn_map = {}  # awb -> latest Delivery Note status (only awbs present in _rows)
_dn_fetched_at = None
_loaded_from_disk = False
_last_error = None
_last_attempt_at = 0.0  # back-off: a failing refresh mustn't be retried on every page view
AUTO_RETRY_SECONDS = 600


def _fmt(value):
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def _sort_key(row):
    return row["rto_delivery_date"] or ""


def _ensure_loaded():
    global _rows, _fetched_at, _loaded_from_disk, _dn_map, _dn_fetched_at
    with _state_lock:
        if _loaded_from_disk:
            return
        _loaded_from_disk = True
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE, encoding="utf-8") as fh:
                    payload = json.load(fh)
                _rows = payload["rows"]
                _fetched_at = payload["fetched_at"]
                if _rows and "status" not in _rows[0]:
                    _fetched_at = 0  # cache predates the status column: serve it, but refresh at once
            except Exception:
                traceback.print_exc()  # corrupt cache: ignore, a refresh will rebuild it
        if os.path.exists(DN_CACHE_FILE):
            try:
                with open(DN_CACHE_FILE, encoding="utf-8") as fh:
                    payload = json.load(fh)
                if payload.get("version") == DN_CACHE_VERSION:  # older formats are rebuilt
                    _dn_map = payload["map"]
                    _dn_fetched_at = payload["fetched_at"]
            except Exception:
                traceback.print_exc()


def _save_json(path, payload):
    # Unique temp file per write: two processes (e.g. Flask's debug reloader parent + child,
    # or the server plus a manual `python3 rto_delivered.py`) can save at the same time.
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)  # atomic: readers never see a half-written file
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _dn_is_stale():
    return _dn_fetched_at is None or time.time() - _dn_fetched_at > DN_CACHE_TTL_SECONDS


def _fetch_dn_map(awbs):
    """Run the Delivery Note query; keep the latest status (by posting_date) for the given awbs."""
    conn = get_dn_connection(read_timeout=DN_READ_TIMEOUT_SECONDS)
    try:
        with conn.cursor() as cur:
            cur.execute(DN_QUERY)
            latest = {}  # awb -> (sort key, status)
            for awb, status, posting_date, posting_time, modified in cur:
                if awb not in awbs or not status:
                    continue
                # posting_date is date-only, so same-day notes are ordered by posting_time
                # (a timedelta), then modified. Missing parts sort oldest.
                stamp = (
                    posting_date.isoformat() if posting_date else "",
                    posting_time.total_seconds() if posting_time else -1.0,
                    modified.isoformat() if modified else "",
                )
                if awb not in latest or stamp > latest[awb][0]:
                    latest[awb] = (stamp, status)
    finally:
        conn.close()
    return {awb: status for awb, (_, status) in latest.items()}


def _left_join(scm_rows, dn_map):
    """LEFT JOIN on awb = custom_awb_number; blank status when there is no Delivery Note."""
    return [{**r, "status": dn_map.get(r["awb"], "")} for r in scm_rows]


def _is_stale():
    return _fetched_at is None or time.time() - _fetched_at > CACHE_TTL_SECONDS


def refresh_cache(force=False):
    """Run the DB queries and replace the cache. Only function here that touches MySQL."""
    global _rows, _fetched_at, _dn_map, _dn_fetched_at, _last_error, _last_attempt_at
    if not _refresh_lock.acquire(blocking=False):
        return  # already refreshing
    try:
        _ensure_loaded()
        _last_attempt_at = time.time()
        if not force and not _is_stale() and not _dn_is_stale():
            return
        conn = get_scm_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(QUERY)
                fetched = cur.fetchall()
        finally:
            conn.close()
        scm_rows = [dict(zip(SCM_COLUMNS, (_fmt(v) for v in r))) for r in fetched]

        dn_map, dn_fetched_at, dn_error = _dn_map, _dn_fetched_at, None
        if force or _dn_is_stale():
            try:
                dn_map = _fetch_dn_map({r["awb"] for r in scm_rows})
                dn_fetched_at = time.time()
                _save_json(DN_CACHE_FILE, {"version": DN_CACHE_VERSION, "fetched_at": dn_fetched_at, "map": dn_map})
            except Exception as e:  # keep joining against the previous statuses
                traceback.print_exc()
                dn_error = f"Delivery Note status refresh failed ({e}); showing previously cached statuses."

        rows = _left_join(scm_rows, dn_map)
        rows.sort(key=_sort_key, reverse=True)
        now = time.time()
        _save_json(CACHE_FILE, {"fetched_at": now, "rows": rows})
        with _state_lock:
            _rows, _fetched_at, _dn_map, _dn_fetched_at = rows, now, dn_map, dn_fetched_at
            _last_error = dn_error
    except Exception as e:
        traceback.print_exc()
        _last_error = str(e)
    finally:
        _refresh_lock.release()


def refresh_cache_async(force=False):
    threading.Thread(target=refresh_cache, args=(force,), daemon=True).start()


def is_refreshing():
    if _refresh_lock.acquire(blocking=False):
        _refresh_lock.release()
        return False
    return True


def _apply_filters(rows, date_from, date_to, return_name, search, status):
    """Returns (rows matching every filter, status facet).

    The facet is {status: distinct AWB count} over the rows matching every filter EXCEPT
    status - so the Status dropdown and status tiles stay comparable (picking one status
    doesn't make the others vanish) while still reacting to the date / return name / search
    filters. Blank status is keyed as NO_STATUS.
    """
    needle = (search or "").strip().lower()
    want = None if not status else ("" if status == NO_STATUS else status)
    filtered = []
    facet = {}
    for r in rows:
        if return_name and r["return_name"] != return_name:
            continue
        if date_from or date_to:
            d = r["rto_delivery_date"][:10]  # rows with no delivery date drop out of a date-filtered view
            if not d or (date_from and d < date_from) or (date_to and d > date_to):
                continue
        if needle and not any(needle in r.get(c, "").lower() for c in ("awb", "courier_partner", "return_name", "return_city", "latest_remark", "status")):
            continue
        st = r.get("status", "")
        facet.setdefault(st or NO_STATUS, set()).add(r["awb"])
        if want is not None and st != want:
            continue
        filtered.append(r)

    counts = [{"status": k, "count": len(v)} for k, v in facet.items()]
    # biggest first, "(No status)" always last
    counts.sort(key=lambda c: (c["status"] == NO_STATUS, -c["count"], c["status"]))
    if status and status not in facet:
        counts.append({"status": status, "count": 0})  # keep the selected option selectable
    return filtered, counts


def _current_rows():
    _ensure_loaded()
    if (_is_stale() or _dn_is_stale()) and not is_refreshing() and time.time() - _last_attempt_at > AUTO_RETRY_SECONDS:
        refresh_cache_async()
    with _state_lock:
        return _rows, _fetched_at


def snapshot():
    """Every cached row (no filtering or pagination) plus the cache state - used by RTO Analytics."""
    rows, fetched_at = _current_rows()
    return {
        "rows": rows,
        "fetched_at": fetched_at,
        "refreshing": is_refreshing(),
        "loading": fetched_at is None,  # no cache yet: first-ever fetch is still running
        "error": _last_error,
    }


def query(date_from=None, date_to=None, return_name=None, search=None, offset=0, limit=200, status=None):
    """Filter the cached rows. Dates are inclusive 'YYYY-MM-DD' strings on rto_delivery_date."""
    rows, fetched_at = _current_rows()
    return_names = sorted({r["return_name"] for r in rows if r["return_name"]})
    filtered, status_counts = _apply_filters(rows, date_from, date_to, return_name, search, status)

    return {
        "rows": filtered[offset : offset + limit],
        "total": len(filtered),
        "cached_total": len(rows),
        "return_names": return_names,
        "status_counts": status_counts,
        "fetched_at": fetched_at,
        "refreshing": is_refreshing(),
        "loading": fetched_at is None,  # no cache yet: first-ever fetch is still running
        "error": _last_error,
    }


def summary(date_from=None, date_to=None, return_name=None, search=None, status=None):
    """KPI counts. `rows` is one per source row; distinct_awb is counted separately in case an
    AWB ever repeats. status_counts follows the facet rules in _apply_filters."""
    rows, _ = _current_rows()
    filtered, status_counts = _apply_filters(rows, date_from, date_to, return_name, search, status)
    return {
        "distinct_awb": len({r["awb"] for r in filtered if r["awb"]}),
        "rows": len(filtered),
        "return_names": len({r["return_name"] for r in filtered if r["return_name"]}),
        "status_counts": status_counts,
    }


def all_filtered(date_from=None, date_to=None, return_name=None, search=None, status=None):
    """Every matching row (no pagination) - used for CSV export."""
    rows, _ = _current_rows()
    return _apply_filters(rows, date_from, date_to, return_name, search, status)[0]


if __name__ == "__main__":
    t0 = time.time()
    refresh_cache(force=True)
    print(f"{len(_rows)} rows cached in {time.time() - t0:.1f}s -> {CACHE_FILE}")
