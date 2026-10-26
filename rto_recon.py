"""RTO Reconciliation tab: RTO-InTransit AWBs whose RTO has been pending 10+ days.

Same caching approach as rto_delivered.py: the query result is saved to a JSON file and
served from memory; once it predates the daily refresh time (daily_refresh.py) it is refreshed
in the background (the old data keeps being served, and a failed refresh leaves it untouched).
The filters and pagination run on the in-memory rows, so they never touch MySQL.

Each row is LEFT JOINed (in Python - the tables live on different DB servers) to the ERP
Delivery Note table on awb = custom_awb_number, keeping the LATEST note's status by
posting_date (ties: posting_time, then modified) as `erp_status`. custom_awb_number has no
index, so even a lookup of a few hundred AWBs is a ~4 minute full scan. Those statuses are
therefore cached separately (recon_dn_cache.json) and re-fetched with the daily refresh - or
sooner (DN_NEW_AWB_RETRY_SECONDS) when the main query surfaces AWBs that were never looked up.
A row whose AWB has no Delivery Note keeps a blank erp_status.
"""

import json
import os
import tempfile
import threading
import time
import traceback
from datetime import date, datetime, timedelta

import pymysql

import daily_refresh
from rto_process import get_dn_connection, get_scm_connection

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(BASE_DIR, "rto_recon_cache.json")
AUTO_RETRY_SECONDS = 600
DN_CACHE_FILE = os.path.join(BASE_DIR, "recon_dn_cache.json")
DN_NEW_AWB_RETRY_SECONDS = 3600  # between daily refreshes, re-look-up only for AWBs never looked up, at most this often
DN_READ_TIMEOUT_SECONDS = 1500

COLUMNS = [
    "clickpost_unified_status",
    "courier_partner",
    "awb",
    "return_name",
    "created_at",
    "pickup_date",
    "reference_number",
    "pickup_pincode",
    "drop_pincode",
    "rto_mark_date",
    "rto_delivery_date",
    "rto_date_diff",
    "erp_status",  # from tabDelivery Note via the LEFT JOIN
]
SCM_COLUMNS = COLUMNS[:-1]

# The panel's filters (RTO Analytics > RTO Reconciliation shares the date fields, age buckets and label).
DATE_FIELDS = ("rto_mark_date", "created_at", "pickup_date")
NO_STATUS = "(No status)"  # an AWB with no Delivery Note, in the ERP status filter
AGE_BUCKETS = [  # days since the RTO was marked (rto_date_diff)
    ("10-14 days", 10, 14),
    ("15-29 days", 15, 29),
    ("30-59 days", 30, 59),
    ("60-89 days", 60, 89),
    ("90-179 days", 90, 179),
    ("180+ days", 180, None),
]
AGE_LABELS = [label for label, _, _ in AGE_BUCKETS]
SEARCH_FIELDS = ("awb", "reference_number", "pickup_pincode", "drop_pincode")

# custom_awb_number has no index. `custom_awb_number IN (...)` made even a 30-day window take 15+
# minutes and a single unwindowed query gets its connection dropped ("Lost connection to MySQL
# server during query"); a whole-table stream was slower still. What works is what the RTO MDND
# tab does: plain rows streamed per short posting_date window (indexed, no silent gaps), filtered
# to our AWBs in Python. Each window is retried on a dropped connection.
DN_QUERY = """
select custom_awb_number, status, posting_date, posting_time, modified
from `tabDelivery Note`
where posting_date >= %s and posting_date < %s
  and custom_awb_number is not null and custom_awb_number <> ''
"""
DN_WINDOW_DAYS = 30
DN_WINDOW_ATTEMPTS = 3
DN_RETRY_SECONDS = 6 * 3600  # after a lookup attempt (failed or not), don't start another for this long
DN_START_MARGIN_DAYS = 7  # a note can be posted a little before the shipment's created_at

QUERY = """
SELECT DISTINCT
  clickpost_unified_status,
  courier_partner,
  awb,
  return_name,
  created_at,
  pickup_date,
  reference_number,
  pickup_pincode,
  drop_pincode,
  rto_mark_date,
  rto_delivery_date,
  CASE
    WHEN rto_delivery_date IS NULL
      THEN DATEDIFF(CURDATE(), rto_mark_date)
    ELSE DATEDIFF(rto_delivery_date, rto_mark_date)
  END AS rto_date_diff
FROM cp_database_N
WHERE clickpost_unified_status = 'RTO-InTransit'
HAVING rto_date_diff >= 10
"""

_state_lock = threading.Lock()
_refresh_lock = threading.Lock()
_rows = []  # list of dicts, longest-pending first
_fetched_at = None
_dn_map = {}  # awb -> latest Delivery Note status ("" = looked up, no note); every awb ever looked up is a key
_dn_fetched_at = None
_dn_attempted_at = 0.0  # in-memory only: bounds how often the heavy ERP lookup may be started
_loaded_from_disk = False
_last_error = None
_last_attempt_at = 0.0


def _fmt(value):
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def _sort_key(row):
    # longest-pending first; blank diff sorts last
    return int(row["rto_date_diff"]) if row["rto_date_diff"] else -1


def _save_json(path, payload):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)  # atomic: readers never see a half-written file
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


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
                _rows, _fetched_at = payload["rows"], payload["fetched_at"]
                if _rows and "erp_status" not in _rows[0]:
                    _fetched_at = 0  # cache predates the ERP status column: serve it, but refresh at once
            except Exception:
                traceback.print_exc()  # corrupt cache: ignore, a refresh will rebuild it
        if os.path.exists(DN_CACHE_FILE):
            try:
                with open(DN_CACHE_FILE, encoding="utf-8") as fh:
                    payload = json.load(fh)
                _dn_map, _dn_fetched_at = payload["map"], payload["fetched_at"]
            except Exception:
                traceback.print_exc()


def _is_stale():
    return daily_refresh.is_due(_fetched_at)


def _dn_should_refresh(awbs, force):
    if force:
        return True
    if time.time() - _dn_attempted_at < DN_RETRY_SECONDS:
        return False  # the lookup is heavy on the ERP DB: a recent attempt (even a failed one) is not repeated
    if daily_refresh.is_due(_dn_fetched_at):
        return True
    return time.time() - _dn_fetched_at > DN_NEW_AWB_RETRY_SECONDS and any(a not in _dn_map for a in awbs)


def _query_window(link, start, end, wanted):
    """Rows of the Delivery Notes posted in [start, end) whose awb is in `wanted`.

    `link` is {"conn": connection}; after a dropped connection the connection is replaced
    (a dead pymysql connection raises InterfaceError / AttributeError, not just OperationalError).
    """
    for attempt in range(DN_WINDOW_ATTEMPTS):
        try:
            with link["conn"].cursor(pymysql.cursors.SSCursor) as cur:  # streamed, not buffered in the client
                cur.execute(DN_QUERY, (start, end))
                return [row for row in cur if row[0] in wanted]
        except (pymysql.err.Error, AttributeError):
            if attempt == DN_WINDOW_ATTEMPTS - 1:
                raise
            traceback.print_exc()
            try:
                link["conn"].close()
            except Exception:
                pass
            link["conn"] = get_dn_connection(read_timeout=DN_READ_TIMEOUT_SECONDS)


def _fetch_dn_map(awbs, since):
    """Latest Delivery Note status (by posting_date) for each awb; "" when it has no note.

    `since` is the earliest date (YYYY-MM-DD) any of the awbs could have a note on.
    """
    awbs = sorted(awbs)
    wanted = set(awbs)
    rows = []
    if awbs:
        first = datetime.strptime(since, "%Y-%m-%d").date() - timedelta(days=DN_START_MARGIN_DAYS)
        end = date.today() + timedelta(days=2)  # posting_date is date-only; small margin past today
        link = {"conn": get_dn_connection(read_timeout=DN_READ_TIMEOUT_SECONDS)}
        try:
            start = first
            while start < end:
                stop = min(start + timedelta(days=DN_WINDOW_DAYS), end)
                rows.extend(_query_window(link, start, stop, wanted))
                start = stop
        finally:
            link["conn"].close()
    latest = {}  # awb -> (sort key, status)
    for awb, status, posting_date, posting_time, modified in rows:
        if not status:
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
    return {a: latest[a][1] if a in latest else "" for a in awbs}


def refresh_cache(force=False):
    """Run the DB query and replace the cache. Only function here that touches MySQL."""
    global _rows, _fetched_at, _dn_map, _dn_fetched_at, _dn_attempted_at, _last_error, _last_attempt_at
    if not _refresh_lock.acquire(blocking=False):
        return  # already refreshing
    try:
        _ensure_loaded()
        _last_attempt_at = time.time()
        if not force and not _is_stale():
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
        awbs = {r["awb"] for r in scm_rows if r["awb"]}
        if _dn_should_refresh(awbs, force):
            _dn_attempted_at = time.time()
            try:
                dn_map = _fetch_dn_map(awbs, min(r["created_at"][:10] for r in scm_rows if r["awb"] and r["created_at"]))
                dn_fetched_at = time.time()
                _save_json(DN_CACHE_FILE, {"fetched_at": dn_fetched_at, "map": dn_map})
            except Exception as e:  # keep joining against the previous statuses
                traceback.print_exc()
                dn_error = f"ERP status lookup failed ({e}); " + (
                    "showing previously cached statuses." if dn_map else "ERP Status is blank until it succeeds."
                )

        rows = [{**r, "erp_status": dn_map.get(r["awb"], "")} for r in scm_rows]
        rows.sort(key=_sort_key, reverse=True)
        now = time.time()
        _save_json(CACHE_FILE, {"fetched_at": now, "rows": rows})
        with _state_lock:
            _rows, _fetched_at, _dn_map, _dn_fetched_at, _last_error = rows, now, dn_map, dn_fetched_at, dn_error
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


def _current_rows():
    _ensure_loaded()
    if _is_stale() and not is_refreshing() and time.time() - _last_attempt_at > AUTO_RETRY_SECONDS:
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


def age_days(row):
    try:
        return int(row["rto_date_diff"])
    except (TypeError, ValueError):
        return None


def age_bucket(row):
    age = age_days(row)
    if age is None:
        return None
    for label, low, high in AGE_BUCKETS:
        if age >= low and (high is None or age <= high):
            return label
    return None


# The dropdown filters: keyword -> the value of a row it matches.
FACETS = {
    "courier": lambda r: r["courier_partner"],
    "return_name": lambda r: r["return_name"],
    "erp_status": lambda r: r["erp_status"] or NO_STATUS,
    "age": age_bucket,
}


def _facet_options(key, counts, chosen):
    """One dropdown's options as [{"value", "count"}]: ages in bucket order, ERP statuses biggest first
    with "(No status)" last, couriers and return names A-Z. A chosen value stays listed even at 0."""
    if chosen and chosen not in counts:
        counts[chosen] = 0
    if key == "age":
        values = [label for label in AGE_LABELS if label in counts]
    elif key == "erp_status":
        values = sorted(counts, key=lambda v: (v == NO_STATUS, -counts[v], v))
    else:
        values = sorted(counts, key=str.lower)
    return [{"value": v, "count": counts[v]} for v in values]


def _filter(rows, date_field="rto_mark_date", date_from=None, date_to=None, search=None, **chosen):
    """Returns (rows matching every filter, dropdown options).

    Dates are inclusive 'YYYY-MM-DD' bounds on `date_field`; a row without that date drops out of a
    date-filtered view. `search` is a substring of the AWB, reference number or a pincode.
    `chosen` holds the FACETS filters. Each dropdown's counts cover the rows matching every OTHER
    filter, so picking a courier still shows how many AWBs every other courier has.
    """
    needle = (search or "").strip().lower()
    chosen = {k: v for k, v in chosen.items() if v}
    counts = {k: {} for k in FACETS}
    filtered = []
    for r in rows:
        if date_from or date_to:
            d = r[date_field][:10]
            if not d or (date_from and d < date_from) or (date_to and d > date_to):
                continue
        if needle and not any(needle in r[c].lower() for c in SEARCH_FIELDS):
            continue
        values = {k: of(r) for k, of in FACETS.items()}
        misses = [k for k, v in chosen.items() if values[k] != v]
        if not misses:
            filtered.append(r)
        for k, v in values.items():
            if v and (not misses or misses == [k]):
                counts[k][v] = counts[k].get(v, 0) + 1
    return filtered, {k: _facet_options(k, counts[k], chosen.get(k)) for k in FACETS}


def query(offset=0, limit=200, **filters):
    """One page of the rows matching `filters` (see _filter), the dropdown options and the cache state."""
    rows, fetched_at = _current_rows()
    filtered, options = _filter(rows, **filters)
    return {
        "rows": filtered[offset : offset + limit],
        "total": len(filtered),
        "cached_total": len(rows),
        "options": options,
        "fetched_at": fetched_at,
        "refreshing": is_refreshing(),
        "loading": fetched_at is None,  # no cache yet: first-ever fetch is still running
        "error": _last_error,
    }


def all_filtered(**filters):
    """Every matching row (no pagination) - used for CSV download."""
    rows, _ = _current_rows()
    return _filter(rows, **filters)[0]


if __name__ == "__main__":
    t0 = time.time()
    refresh_cache(force=True)
    print(f"{len(_rows)} rows cached in {time.time() - t0:.1f}s (error: {_last_error}) -> {CACHE_FILE}")
