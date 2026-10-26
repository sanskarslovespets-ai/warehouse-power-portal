"""RTO MDND panel: RTO-Delivered AWBs (SCM DB) matched to Delivery Note net value (ERP DB).

Two queries on two different DB servers, matched in Python on AWB. Only AWBs that
appear in BOTH results are shown, grouped into one tab per warehouse city.

Same caching approach as rto_delivered.py: the joined result is saved to a JSON
file and served from memory; once it predates the daily refresh time
(daily_refresh.py) it is refreshed in the background (the old data keeps being
served, and a failed refresh leaves it untouched). The Delivery Note query is a
slow full scan, so its result is cached separately and re-matched against the
fast SCM query on every refresh.
"""

import json
import os
import tempfile
import threading
import time
import traceback
from decimal import ROUND_HALF_UP, Decimal

import pymysql

import daily_refresh
from rto_process import get_dn_connection, get_scm_connection

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(BASE_DIR, "rto_mdnd_cache.json")
NET_CACHE_FILE = os.path.join(BASE_DIR, "rto_mdnd_net_cache.json")
DN_READ_TIMEOUT_SECONDS = 1500
AUTO_RETRY_SECONDS = 600

TABS = ["Gurgaon", "Bangalore", "Mumbai", "Kolkata", "Chennai", "Pune", "Hyderabad"]

COLUMNS = [
    "awb",
    "received_on",
    "marked_on",
    "days_to_receipt",
    "courier",
    "warehouse",
    "order_id",
    "drop_city",
    "rto_reason",
    "net_value",
]
SCM_COLUMNS = ["awb", "received_on", "marked_on", "days_to_receipt", "courier", "warehouse", "order_id", "drop_city", "rto_reason", "tab"]

SCM_QUERY = """
SELECT
    TRIM(awb)                                              AS awb,
    DATE(rto_delivery_date)                                AS received_on,
    DATE(rto_mark_date)                                    AS marked_on,
    DATEDIFF(DATE(rto_delivery_date), DATE(rto_mark_date)) AS days_to_receipt,
    courier_partner                                        AS courier,
    from_warehouse                                         AS warehouse,
    order_id,
    drop_city,
    LEFT(reason_for_last_failed_delivery, 120)             AS rto_reason,
    CASE from_warehouse
        WHEN 'MWH_Ggn_bhajgera_001'   THEN 'Gurgaon'
        WHEN 'MWH_Blr_Neelmangla_002' THEN 'Bangalore'
        WHEN 'MWH_Mum_Bhiwandi_002'   THEN 'Mumbai'
        WHEN 'MWH_Kol_Dankuni_001'    THEN 'Kolkata'
        WHEN 'MWH_Chn_Kovur_001'      THEN 'Chennai'
        WHEN 'MWH_Pun_Khed_001'       THEN 'Pune'
        WHEN 'MWH_Hyd_Manikonda_001'  THEN 'Hyderabad'
    END                                                    AS tab
FROM cp_database_N
WHERE clickpost_unified_status = 'RTO-Delivered'
  AND courier_partner <> 'Delhivery Reverse'
  -- floor: the later of 1 Sep and 60 days ago (IST)
  AND rto_delivery_date >= GREATEST(DATE('2026-09-01'),
                                    DATE(DATE_ADD(NOW(), INTERVAL 330 MINUTE)) - INTERVAL 60 DAY)
  -- up to the end of today (IST)
  AND rto_delivery_date <  DATE(DATE_ADD(NOW(), INTERVAL 330 MINUTE)) + INTERVAL 1 DAY
  AND from_warehouse IN ('MWH_Ggn_bhajgera_001','MWH_Blr_Neelmangla_002','MWH_Mum_Bhiwandi_002',
                         'MWH_Kol_Dankuni_001','MWH_Chn_Kovur_001','MWH_Pun_Khed_001',
                         'MWH_Hyd_Manikonda_001')
ORDER BY tab, received_on, awb
"""

# Equivalent to: SELECT TRIM(custom_awb_number) awb, ROUND(SUM(net_total)) net_value ... GROUP BY
# custom_awb_number. The GROUP BY is done in Python (_fetch_net_map) instead: on the server it is an
# unindexed group over ~800K rows that sends nothing for 2+ minutes, and the connection gets dropped
# ("Lost connection to MySQL server during query"). Streaming plain rows keeps it alive.
NET_QUERY = """
SELECT custom_awb_number, net_total
FROM `tabDelivery Note`
WHERE posting_date >= CURDATE() - INTERVAL 145 DAY
  AND custom_awb_number IS NOT NULL AND custom_awb_number <> ''
  AND docstatus < 2          -- skip cancelled notes
  AND net_total > 0
"""

_state_lock = threading.Lock()
_refresh_lock = threading.Lock()
_rows = []  # matched rows (dicts with COLUMNS + "tab"), already ordered by tab, received_on, awb
_fetched_at = None
_net_map = {}  # awb -> net_value
_net_fetched_at = None
_loaded_from_disk = False
_last_error = None
_last_attempt_at = 0.0


def _text(value):
    return "" if value is None else str(value)


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
    global _rows, _fetched_at, _net_map, _net_fetched_at, _loaded_from_disk
    with _state_lock:
        if _loaded_from_disk:
            return
        _loaded_from_disk = True
        for path, apply in ((CACHE_FILE, "rows"), (NET_CACHE_FILE, "net")):
            if not os.path.exists(path):
                continue
            try:
                with open(path, encoding="utf-8") as fh:
                    payload = json.load(fh)
                if apply == "rows":
                    _rows, _fetched_at = payload["rows"], payload["fetched_at"]
                else:
                    _net_map, _net_fetched_at = payload["map"], payload["fetched_at"]
            except Exception:
                traceback.print_exc()  # corrupt cache: ignore, a refresh will rebuild it


def _is_stale():
    return daily_refresh.is_due(_fetched_at)


def _net_is_stale():
    return daily_refresh.is_due(_net_fetched_at)


def _fetch_scm_rows():
    conn = get_scm_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(SCM_QUERY)
            fetched = cur.fetchall()
    finally:
        conn.close()
    return [dict(zip(SCM_COLUMNS, (_text(v) for v in r))) for r in fetched]


def _fetch_net_map():
    conn = get_dn_connection(read_timeout=DN_READ_TIMEOUT_SECONDS)
    try:
        totals = {}
        with conn.cursor(pymysql.cursors.SSCursor) as cur:  # streamed, not buffered in the client
            cur.execute(NET_QUERY)
            for awb, net in cur:
                awb = (awb or "").strip()
                if awb and net is not None:
                    totals[awb] = totals.get(awb, Decimal(0)) + Decimal(net)
    finally:
        conn.close()
    # MySQL ROUND() rounds halves away from zero (Python's round() would round to even)
    return {awb: int(t.quantize(Decimal(1), rounding=ROUND_HALF_UP)) for awb, t in totals.items()}


def _match(scm_rows, net_map):
    """Keep only the AWBs present in both results, adding net_value."""
    return [{**r, "net_value": net_map[r["awb"]]} for r in scm_rows if r["awb"] in net_map]


def refresh_cache(force=False):
    """Run the DB queries and replace the cache. Only function here that touches MySQL."""
    global _rows, _fetched_at, _net_map, _net_fetched_at, _last_error, _last_attempt_at
    if not _refresh_lock.acquire(blocking=False):
        return  # already refreshing
    try:
        _ensure_loaded()
        _last_attempt_at = time.time()
        if not force and not _is_stale() and not _net_is_stale():
            return
        scm_rows = _fetch_scm_rows()

        net_map, net_fetched_at, net_error = _net_map, _net_fetched_at, None
        if force or _net_is_stale():
            try:
                net_map = _fetch_net_map()
                net_fetched_at = time.time()
                _save_json(NET_CACHE_FILE, {"fetched_at": net_fetched_at, "map": net_map})
            except Exception as e:  # keep matching against the previous net values
                traceback.print_exc()
                if not net_map:
                    raise  # nothing to match against: don't cache an empty table as if it were fresh
                net_error = f"Delivery Note refresh failed ({e}); showing previously cached net values."

        rows = _match(scm_rows, net_map)
        now = time.time()
        _save_json(CACHE_FILE, {"fetched_at": now, "rows": rows})
        with _state_lock:
            _rows, _fetched_at, _net_map, _net_fetched_at = rows, now, net_map, net_fetched_at
            _last_error = net_error
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
    if (_is_stale() or _net_is_stale()) and not is_refreshing() and time.time() - _last_attempt_at > AUTO_RETRY_SECONDS:
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


def query(tab):
    """Rows for one warehouse tab, plus the row count of every tab."""
    rows, fetched_at = _current_rows()
    counts = {t: 0 for t in TABS}
    for r in rows:
        counts[r["tab"]] = counts.get(r["tab"], 0) + 1
    return {
        "rows": [r for r in rows if r["tab"] == tab],
        "tabs": [{"tab": t, "count": counts[t]} for t in TABS],
        "fetched_at": fetched_at,
        "refreshing": is_refreshing(),
        "loading": fetched_at is None,  # no cache yet: first-ever fetch is still running
        "error": _last_error,
    }


if __name__ == "__main__":
    t0 = time.time()
    refresh_cache(force=True)
    print(f"{len(_rows)} matched rows cached in {time.time() - t0:.1f}s (error: {_last_error}) -> {CACHE_FILE}")
