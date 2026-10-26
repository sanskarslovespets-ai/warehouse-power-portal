import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rto_entries.db")

ENTRY_FIELDS = [
    "damage_marked_date",
    "received_date",
    "facility_name",
    "damage_type",
    "awb_code",
    "sku_code",
    "pod_link",
    "video_link",
    "courier_name",
    "remarks",
]
EXPORT_COLUMNS = ["id", "created_at"] + ENTRY_FIELDS + ["sheet_synced"]  # a downloaded entry, column by column


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db():
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rto_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                damage_marked_date TEXT NOT NULL,
                received_date TEXT NOT NULL,
                facility_name TEXT NOT NULL,
                damage_type TEXT NOT NULL,
                awb_code TEXT NOT NULL,
                sku_code TEXT NOT NULL,
                pod_link TEXT NOT NULL,
                video_link TEXT NOT NULL,
                courier_name TEXT NOT NULL,
                remarks TEXT NOT NULL DEFAULT '',
                sheet_synced INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        # Values typed into the input columns of the RTO MDND tab, one row per AWB.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mdnd_inputs (
                awb TEXT PRIMARY KEY,
                received_at_wh TEXT NOT NULL DEFAULT '',
                received_date TEXT NOT NULL DEFAULT '',
                condition TEXT NOT NULL DEFAULT '',
                discrepancy_notes TEXT NOT NULL DEFAULT '',
                checked_by TEXT NOT NULL DEFAULT '',
                date_updated TEXT NOT NULL DEFAULT ''
            )
            """
        )


def add_entry(values):
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    columns = ", ".join(ENTRY_FIELDS)
    placeholders = ", ".join("?" for _ in ENTRY_FIELDS)
    with _connect() as conn:
        cur = conn.execute(
            f"INSERT INTO rto_entries (created_at, {columns}) VALUES (?, {placeholders})",
            [created_at] + [values[field] for field in ENTRY_FIELDS],
        )
        row = conn.execute("SELECT * FROM rto_entries WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


def list_entries():
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM rto_entries ORDER BY id DESC").fetchall()
    return [dict(row) for row in rows]


def list_unsynced_entries():
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM rto_entries WHERE sheet_synced = 0 ORDER BY id").fetchall()
    return [dict(row) for row in rows]


def mark_synced(entry_ids):
    with _connect() as conn:
        conn.executemany(
            "UPDATE rto_entries SET sheet_synced = 1 WHERE id = ?",
            [(entry_id,) for entry_id in entry_ids],
        )


# Date field the RTO Analytics dashboard can filter/group by -> matching column.
# created_at is a full "YYYY-MM-DD HH:MM:SS" timestamp, so it's compared by its
# date() portion; the other two are already plain "YYYY-MM-DD" strings.
ANALYTICS_DATE_FIELDS = {
    "damage_marked_date": "damage_marked_date",
    "received_date": "received_date",
    "created_at": "date(created_at)",
}


def query_entries_in_range(date_field, start=None, end=None):
    """Rows whose `date_field` falls within [start, end] (either bound optional)."""
    if date_field not in ANALYTICS_DATE_FIELDS:
        raise ValueError(f"Unsupported date_field: {date_field}")
    expr = ANALYTICS_DATE_FIELDS[date_field]

    conditions, params = [], []
    if start:
        conditions.append(f"{expr} >= ?")
        params.append(start)
    if end:
        conditions.append(f"{expr} <= ?")
        params.append(end)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    with _connect() as conn:
        rows = conn.execute(f"SELECT * FROM rto_entries {where} ORDER BY id", params).fetchall()
    return [dict(row) for row in rows]


MDND_INPUT_FIELDS = [
    "received_at_wh",
    "received_date",
    "condition",
    "discrepancy_notes",
    "checked_by",
    "date_updated",
]


def entries_by_awb():
    """awb -> latest RTO Process entry logged for that AWB."""
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM rto_entries ORDER BY id").fetchall()
    return {row["awb_code"].strip(): dict(row) for row in rows}  # later entries overwrite earlier ones


def get_mdnd_inputs():
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM mdnd_inputs").fetchall()
    return {row["awb"]: dict(row) for row in rows}


def save_mdnd_input(awb, values):
    columns = ", ".join(MDND_INPUT_FIELDS)
    placeholders = ", ".join("?" for _ in MDND_INPUT_FIELDS)
    updates = ", ".join(f"{f} = excluded.{f}" for f in MDND_INPUT_FIELDS)
    with _connect() as conn:
        conn.execute(
            f"INSERT INTO mdnd_inputs (awb, {columns}) VALUES (?, {placeholders}) "
            f"ON CONFLICT(awb) DO UPDATE SET {updates}",
            [awb] + [values[f] for f in MDND_INPUT_FIELDS],
        )
