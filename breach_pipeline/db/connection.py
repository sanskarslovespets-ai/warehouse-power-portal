"""MySQL connections for the export scripts, from the Warehouse Power Portal's own settings.

The one file in breach_pipeline/ that is not a straight copy of edd-observability (see ../README.md).
The scripts call the same three functions with the same arguments; what changed is where the
credentials come from:

    get_scm_connection()      SCM_DB_*      scm, the analytics replica (upstream: DB_*)
    get_erp_connection()      DN_DB_*       the ERP read replica       (upstream: ERP_DB_*)
    get_promise_connection()  PROMISE_DB_*  the promise engine, same names as upstream. Optional: only
                                            export_warehouse_pendency.py asks, for the dispatch cutoff, and
                                            without it uses 14:00 (Standard A, the same at all seven)

and that passwords are sent as UTF-8 bytes, as rto_process.py does: PyMySQL sends a str password
as latin-1, so a correct password with a non-ASCII character fails with error 1045.

Kept from upstream: DictCursor (the scripts read rows as dicts), the retry on a refused connection
(the ERP replica drops them now and then), and raising the server's statement cap to the read
timeout the caller asked for.
"""
import os
import sys
import time

import pymysql
from dotenv import load_dotenv

load_dotenv()

CONNECT_RETRIES = int(os.environ.get("DB_CONNECT_RETRIES", "4"))
RETRY_BACKOFF = float(os.environ.get("DB_RETRY_BACKOFF_SEC", "5"))


def get_connection(prefix, read_timeout=10):
    last = None
    for attempt in range(1, CONNECT_RETRIES + 1):
        try:
            return _connect(prefix, read_timeout)
        except pymysql.err.OperationalError as e:
            last = e
            if attempt == CONNECT_RETRIES:
                break
            wait = RETRY_BACKOFF * attempt  # 5s, 10s, 15s
            print("{}: connect attempt {}/{} failed ({}); retrying in {:.0f}s".format(
                prefix, attempt, CONNECT_RETRIES, type(e).__name__, wait), file=sys.stderr)
            time.sleep(wait)
    raise last


def _connect(prefix, read_timeout):
    conn = pymysql.connect(
        host=os.environ[f"{prefix}_HOST"],
        port=int(os.environ.get(f"{prefix}_PORT", 3306)),
        user=os.environ[f"{prefix}_USER"],
        password=os.environ[f"{prefix}_PASSWORD"].encode("utf-8"),
        database=os.environ.get(f"{prefix}_NAME") or None,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=read_timeout,
        write_timeout=read_timeout,
    )
    # read_timeout is only the client's socket timeout; the server applies its own statement cap
    # (seen killing exports with error 3024). MySQL and MariaDB name it differently.
    with conn.cursor() as cur:
        try:
            cur.execute("SET SESSION MAX_EXECUTION_TIME = %s", (read_timeout * 1000,))
        except pymysql.err.OperationalError:
            try:
                cur.execute("SET SESSION max_statement_time = %s", (read_timeout,))
            except pymysql.err.OperationalError:
                pass
    return conn


def get_scm_connection(read_timeout=10):
    return get_connection("SCM_DB", read_timeout=read_timeout)


def get_erp_connection(read_timeout=10):
    return get_connection("DN_DB", read_timeout=read_timeout)


def get_promise_connection(read_timeout=10):
    """The promise engine's host (promiseEngine.warehouse_cutoffs). Raises KeyError when PROMISE_DB_* isn't set,
    which export_warehouse_pendency.py treats like an unreachable host: it falls back to its 14:00 cutoff."""
    return get_connection("PROMISE_DB", read_timeout=read_timeout)
