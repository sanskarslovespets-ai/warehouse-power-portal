"""BoxVault's Invoice Entry: each uploaded invoice's line items, kept in scm.boxvault_invoice_lines.

One row per line item, with the warehouse it was uploaded for and a link to its PDF in Drive. An invoice is
known by its supplier's GSTIN and its number, so the same invoice can't be entered twice. The table is
created by the first invoice saved (as users.py does with warehouse_user_access); until then there are none.
"""
from datetime import datetime

from pymysql.constants import ER
from pymysql.err import IntegrityError, ProgrammingError

from rto_process import get_scm_connection

TABLE = "boxvault_invoice_lines"
CREATE = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id INT AUTO_INCREMENT PRIMARY KEY,
    supplier_gstin VARCHAR(15) NOT NULL,
    invoice_number VARCHAR(64) NOT NULL,
    line_no SMALLINT NOT NULL,
    warehouse VARCHAR(64) NOT NULL,
    invoice_date DATE NOT NULL,
    supplier VARCHAR(255) NOT NULL,
    description VARCHAR(512) NOT NULL,
    sku VARCHAR(64) NOT NULL,
    hsn VARCHAR(16) NOT NULL,
    qty DECIMAL(14, 2) NOT NULL,
    rate DECIMAL(14, 2) NOT NULL,
    tax_pct DECIMAL(6, 2) NOT NULL,
    tax_amount DECIMAL(14, 2) NOT NULL,
    amount DECIMAL(14, 2) NOT NULL COMMENT 'before tax',
    total DECIMAL(14, 2) NOT NULL COMMENT 'amount + tax',
    file_name VARCHAR(255) NOT NULL,
    drive_link VARCHAR(512) NOT NULL,
    uploaded_by VARCHAR(64) NOT NULL,
    uploaded_at DATETIME NOT NULL,
    UNIQUE KEY uq_invoice_line (supplier_gstin, invoice_number, line_no),
    KEY idx_warehouse_sku (warehouse, sku)
) COMMENT 'BoxVault Invoice Entry (Warehouse Power Portal, invoice_entries.py)'
"""
FIELDS = ("warehouse", "invoice_number", "invoice_date", "supplier", "supplier_gstin", "line_no", "description", "sku",
          "hsn", "qty", "rate", "tax_pct", "tax_amount", "amount", "total", "file_name", "drive_link", "uploaded_by",
          "uploaded_at")


class AlreadyEntered(Exception):
    pass


def _no_table(error):
    return isinstance(error, ProgrammingError) and error.args[0] == ER.NO_SUCH_TABLE


def entered(supplier_gstin, invoice_number):
    """Whether this invoice's lines are already in Invoice Entry."""
    conn = get_scm_connection()
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(f"SELECT 1 FROM {TABLE} WHERE supplier_gstin = %s AND invoice_number = %s LIMIT 1",
                            (supplier_gstin, invoice_number))
            except ProgrammingError as e:
                if _no_table(e):
                    return False
                raise
            return cur.fetchone() is not None
    finally:
        conn.close()


def add(invoice, warehouse, file_name, drive_link, by):
    """Saves an invoice_pdf.read() invoice's lines in one transaction; AlreadyEntered if it's there already."""
    now = datetime.now().replace(microsecond=0)
    rows = [(warehouse, invoice["invoice_number"], invoice["invoice_date"], invoice["supplier"][:255],
             invoice["supplier_gstin"], ln["line"], ln["description"][:512], ln["sku"], ln["hsn"], ln["qty"],
             ln["rate"], ln["tax_pct"], ln["tax_amount"], ln["amount"], ln["total"], file_name[:255], drive_link, by, now)
            for ln in invoice["lines"]]
    conn = get_scm_connection()
    try:
        with conn.cursor() as cur:
            # created only when missing: CREATE TABLE IF NOT EXISTS needs the CREATE privilege even when it exists
            cur.execute("SELECT 1 FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = %s",
                        (TABLE,))
            if not cur.fetchone():
                cur.execute(CREATE)
            cur.executemany(f"INSERT INTO {TABLE} ({', '.join(FIELDS)}) VALUES ({', '.join(['%s'] * len(FIELDS))})", rows)
        conn.commit()
    except IntegrityError as e:
        conn.rollback()
        if e.args[0] == ER.DUP_ENTRY:
            raise AlreadyEntered from e
        raise
    finally:
        conn.close()
    return len(rows)


def rows():
    """Every line item, newest invoice first: [{field: value}] with dates as YYYY-MM-DD and amounts as numbers."""
    conn = get_scm_connection()
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(f"SELECT {', '.join(FIELDS)} FROM {TABLE} "
                            "ORDER BY invoice_date DESC, invoice_number DESC, line_no")
            except ProgrammingError as e:
                if _no_table(e):
                    return []
                raise
            found = cur.fetchall()
    finally:
        conn.close()
    out = []
    for values in found:
        row = dict(zip(FIELDS, values))
        for key in ("qty", "rate", "tax_pct", "tax_amount", "amount", "total"):
            row[key] = float(row[key])
        row["invoice_date"] = row["invoice_date"].isoformat()
        row["uploaded_at"] = row["uploaded_at"].isoformat(sep=" ")
        out.append(row)
    return out


def remove(supplier_gstin, invoice_number):
    """Takes an invoice's lines out of Invoice Entry (its PDF stays in Drive); returns how many there were."""
    conn = get_scm_connection()
    try:
        with conn.cursor() as cur:
            try:
                count = cur.execute(f"DELETE FROM {TABLE} WHERE supplier_gstin = %s AND invoice_number = %s",
                                    (supplier_gstin, invoice_number))
            except ProgrammingError as e:
                if _no_table(e):
                    return 0
                raise
        conn.commit()
        return count
    finally:
        conn.close()
