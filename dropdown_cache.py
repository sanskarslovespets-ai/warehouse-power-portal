"""CSV-backed cache for the facility/courier/SKU/AWB dropdowns.

Querying `SELECT DISTINCT ... FROM cp_database_N` live on every dropdown open
was slow, and turned out to be far worse than expected: on the real data,
facility has ~2.4K distinct values and courier just 14 - fine to preload -
but SKU has ~476K and AWB has ~1.4M. Sending that many rows to the browser
(and filtering them there on every keystroke) was never going to be fast
regardless of caching.

So:
- Known values live in dropdown_cache.csv (field,value rows) and are kept in
  memory for instant reads. A refresh only APPENDS values that aren't already
  cached; nothing already in the CSV is ever rewritten or removed.
- The server process kicks off a background refresh the moment it starts
  (app.py), and again (throttled) whenever the dashboard is opened. This
  NEVER runs inline in a request - the dropdown/search endpoints only ever
  read whatever is already in memory, so they can't be slow no matter how
  long a DB refresh takes.
- SMALL_FIELDS (facility, courier) are sent to the browser in full and
  filtered client-side - the lists are small enough that this is instant.
- LARGE_FIELDS (sku, awb) are never sent to the browser in full. Instead the
  server searches its own in-memory copy (a plain Python substring scan -
  ~35ms over 1.4M AWBs, measured) and returns at most SEARCH_RESULT_LIMIT
  matches. This never touches MySQL once the cache is warm.
"""

import csv
import os
import threading
import time

from rto_process import fetch_distinct_values

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(BASE_DIR, "dropdown_cache.csv")

SMALL_FIELDS = ["facility", "courier"]  # preloaded to the browser in full
LARGE_FIELDS = ["sku", "awb"]  # searched in-memory server-side; never sent in full
FIELDS = SMALL_FIELDS + LARGE_FIELDS

SEARCH_RESULT_LIMIT = 50

# DISTINCT over a 1.4M+ row column is a heavy query for the DB to run even
# once, and it now happens in the background - but there's no reason to fire
# it off again every time someone merely opens the dashboard. New AWBs/SKUs
# showing up in the dropdown within three hours of being created is plenty
# fresh for this workflow.
MIN_REFRESH_INTERVAL_SECONDS = 3 * 3600

_state_lock = threading.Lock()
_refresh_lock = threading.Lock()
_cache = {f: [] for f in FIELDS}
_cache_lower = {f: [] for f in FIELDS}  # parallel lowercased copy, for fast search
_loaded_from_disk = False
_last_refresh_at = 0.0


def _load_from_disk():
    data = {f: [] for f in FIELDS}
    seen = {f: set() for f in FIELDS}
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                field, value = row.get("field"), row.get("value")
                if field in data and value and value not in seen[field]:
                    seen[field].add(value)
                    data[field].append(value)
    for f in data:
        data[f].sort()
    return data


def _append_to_disk(new_rows):
    """new_rows: list of (field, value) tuples. Appends only - never rewrites existing rows."""
    write_header = not os.path.exists(CACHE_FILE)
    with open(CACHE_FILE, "a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if write_header:
            writer.writerow(["field", "value"])
        writer.writerows(new_rows)


def _set_field(field, values):
    """Must be called with _state_lock held. Keeps _cache_lower in sync with _cache."""
    _cache[field] = values
    _cache_lower[field] = [v.lower() for v in values]


def _ensure_loaded():
    global _loaded_from_disk
    with _state_lock:
        if not _loaded_from_disk:
            disk = _load_from_disk()
            for f in FIELDS:
                _set_field(f, disk[f])
            _loaded_from_disk = True


def get_cached_options(fields=None):
    """Instant in-memory read for the API response. Defaults to all fields."""
    _ensure_loaded()
    fields = fields if fields is not None else FIELDS
    with _state_lock:
        return {f: list(_cache[f]) for f in fields}


def search_cached(field, query, limit=SEARCH_RESULT_LIMIT):
    """Instant in-memory substring search - never touches MySQL."""
    _ensure_loaded()
    q = query.strip().lower()
    with _state_lock:
        values = _cache.get(field, [])
        values_lower = _cache_lower.get(field, [])
        if not q:
            return values[:limit]
        results = []
        for value, value_lower in zip(values, values_lower):
            if q in value_lower:
                results.append(value)
                if len(results) >= limit:
                    break
        return results


def has_any_cached_values():
    _ensure_loaded()
    with _state_lock:
        return any(_cache[f] for f in FIELDS)


def refresh_cache(force=False):
    """Fetch fresh distinct values from the DB and append any new ones to the CSV.

    Safe to call from a background thread - this is the only function in this
    module that touches MySQL. A non-forced call is a no-op if a refresh is
    already running or one finished less than MIN_REFRESH_INTERVAL_SECONDS ago.
    """
    global _last_refresh_at

    if force:
        _refresh_lock.acquire()
    else:
        if not _refresh_lock.acquire(blocking=False):
            return  # a refresh is already in flight
        if time.time() - _last_refresh_at < MIN_REFRESH_INTERVAL_SECONDS:
            _refresh_lock.release()
            return

    try:
        _ensure_loaded()
        with _state_lock:
            existing = {f: set(_cache[f]) for f in FIELDS}

        new_rows = []
        newly_added = {f: [] for f in FIELDS}
        for field in FIELDS:
            try:
                fresh_values = fetch_distinct_values(field)
            except Exception:
                continue  # DB unreachable this round; keep serving whatever is already cached
            for value in fresh_values:
                if value not in existing[field]:
                    existing[field].add(value)
                    newly_added[field].append(value)
                    new_rows.append((field, value))

        if new_rows:
            _append_to_disk(new_rows)
            with _state_lock:
                for f in FIELDS:
                    if newly_added[f]:
                        _set_field(f, sorted(existing[f]))

        _last_refresh_at = time.time()
    finally:
        _refresh_lock.release()


def refresh_cache_async():
    threading.Thread(target=refresh_cache, daemon=True).start()


if __name__ == "__main__":
    print("Refreshing dropdown cache from the database (this can take a while - "
          "SKU/AWB are ~476K/~1.4M distinct values)...")
    t0 = time.time()
    refresh_cache(force=True)
    for f in FIELDS:
        print(f"  {f}: {len(_cache[f])} cached value(s)")
    print(f"Done in {time.time() - t0:.1f}s. Saved to {CACHE_FILE}")
