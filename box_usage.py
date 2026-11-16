"""BoxVault's SKU used: how many boxes of each SKU a warehouse used in a month, entered by hand, and what that
means for each invoice line.

Editors type the numbers in BoxVault (after uploading invoices, or with Enter usage): one per warehouse, SKU and
month, kept in scm.boxvault_sku_usage. An invoice line's SKU used comes from them: each month's boxes go against
the oldest of the warehouse's invoices for that SKU dated by the end of the month that hasn't had all its boxes
counted yet, so no box is counted twice; boxes beyond what those invoices bought go on the newest of them, whose
SKU used then goes past its quantity.

The table is made by hand (the portal doesn't create tables), and the portal's database account needs SELECT,
INSERT and UPDATE on it:

    CREATE TABLE scm.boxvault_sku_usage (
        warehouse VARCHAR(64) NOT NULL,
        sku VARCHAR(64) NOT NULL,
        month DATE NOT NULL COMMENT 'first day of the month',
        used INT UNSIGNED NOT NULL,
        entered_by VARCHAR(64) NOT NULL,
        entered_at DATETIME NOT NULL,
        PRIMARY KEY (warehouse, sku, month)
    ) COMMENT 'BoxVault boxes used per warehouse, SKU and month, entered by hand (Warehouse Power Portal, box_usage.py)';
"""
import calendar
from collections import defaultdict
from datetime import date, datetime

from pymysql.constants import ER
from pymysql.err import OperationalError, ProgrammingError

from rto_process import get_scm_connection

TABLE = "boxvault_sku_usage"


class NotSetUp(RuntimeError):
    """scm.boxvault_sku_usage hasn't been made yet."""


class NotAllowed(RuntimeError):
    """The database account may not write to scm.boxvault_sku_usage."""


def _no_table(error):
    return isinstance(error, ProgrammingError) and error.args[0] == ER.NO_SUCH_TABLE


def records():
    """Every number entered: [{"warehouse", "sku", "month": "YYYY-MM", "used", "entered_by", "entered_at"}]."""
    conn = get_scm_connection()
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(f"SELECT warehouse, sku, month, used, entered_by, entered_at FROM {TABLE} "
                            "ORDER BY month, warehouse, sku")
            except ProgrammingError as e:
                if _no_table(e):
                    raise NotSetUp from e
                raise
            found = cur.fetchall()
    finally:
        conn.close()
    return [{"warehouse": warehouse, "sku": sku, "month": month.strftime("%Y-%m"), "used": int(used),
             "entered_by": by, "entered_at": at.isoformat(sep=" ")} for warehouse, sku, month, used, by, at in found]


def save(month, numbers, by):
    """Saves [(warehouse, sku, used)] for `month` (a date, the 1st), replacing what was there; returns how many."""
    now = datetime.now().replace(microsecond=0)
    conn = get_scm_connection()
    try:
        with conn.cursor() as cur:
            cur.executemany(
                f"INSERT INTO {TABLE} (warehouse, sku, month, used, entered_by, entered_at) "
                "VALUES (%s, %s, %s, %s, %s, %s) AS new "
                "ON DUPLICATE KEY UPDATE used = new.used, entered_by = new.entered_by, entered_at = new.entered_at",
                [(warehouse, sku, month, used, by, now) for warehouse, sku, used in numbers])
        conn.commit()
    except ProgrammingError as e:
        if _no_table(e):
            raise NotSetUp from e
        raise
    except OperationalError as e:
        if e.args[0] == ER.TABLEACCESS_DENIED_ERROR:
            raise NotAllowed from e
        raise
    finally:
        conn.close()
    return len(numbers)


def month_end(month):
    """'2026-10' -> date(2026, 10, 31)."""
    year, mon = int(month[:4]), int(month[5:7])
    return date(year, mon, calendar.monthrange(year, mon)[1])


def allocate(invoices, months):
    """How many boxes each invoice line has used.

    invoices: [(invoice date, quantity)] oldest first; months: [(the month's last day, boxes)] oldest first.
    """
    used = [0] * len(invoices)
    for ended, boxes in months:
        dated = [i for i, (invoiced, _) in enumerate(invoices) if invoiced <= ended]
        if not dated:
            continue  # used before the first of these invoices, so not one of their boxes
        for i in dated:
            take = min(boxes, max(invoices[i][1] - used[i], 0))
            used[i] += take
            boxes -= take
            if not boxes:
                break
        used[dated[-1]] += boxes  # more than these invoices bought
    return used


def add(rows, usage):
    """Puts "used" (SKU used) on each Invoice Entry row ({warehouse, sku, qty, invoice_date, invoice_number,
    line_no}) from `usage` (records()); None on a line without a SKU."""
    months = defaultdict(list)
    for u in usage:
        months[(u["warehouse"], u["sku"])].append((month_end(u["month"]), u["used"]))
    groups = defaultdict(list)
    for row in rows:
        row["used"] = None
        if row["sku"]:
            groups[(row["warehouse"], row["sku"])].append(row)
    for key, lines in groups.items():
        lines.sort(key=lambda r: (r["invoice_date"], r["invoice_number"], r["line_no"]))
        used = allocate([(date.fromisoformat(r["invoice_date"]), r["qty"]) for r in lines], sorted(months.get(key, [])))
        for row, n in zip(lines, used):
            row["used"] = n
