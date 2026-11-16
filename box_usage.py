"""BoxVault's SKU used: how many of an invoice's boxes have gone out, counted from the ERP.

An order shipped in a box has the box's SKU (a "Package Box" item such as DBSUPTDEL006) on its delivery note,
so one completed delivery note carrying the SKU is one box used. Counted for the invoice's warehouse (the
delivery note's custom_mother_warehouse, the ERP code BoxVault keeps an invoice under) from the invoice date:

    SELECT COUNT(DISTINCT dni.parent) FROM `tabDelivery Note Item` dni
    JOIN `tabDelivery Note` dn ON dn.name = dni.parent
    WHERE dni.item_code = 'DBSUPTDEL006' AND dn.custom_mother_warehouse = 'MWH_Ggn_bhajgera_001'
      AND dn.status = 'Completed' AND dn.is_return = 0 AND dn.posting_date >= '<invoice date>'

When a warehouse has several invoices for a SKU, each box goes against the oldest invoice dated on or before its
delivery note that hasn't had all its boxes counted yet, so no box is counted twice; boxes beyond what those invoices
bought go on the newest of them, whose SKU used then goes past its quantity.
"""
from collections import defaultdict
from datetime import date

from rto_process import get_dn_connection

READ_TIMEOUT = 60  # seconds; the query takes well under one (dni.item_code is indexed)


def shipped(skus, warehouses, since):
    """Boxes used: {(warehouse, sku): [(posting date, delivery notes), ...] oldest first}, from `since` on."""
    if not skus or not warehouses:
        return {}
    skus, warehouses = sorted(skus), sorted(warehouses)
    sql = f"""
        SELECT dn.custom_mother_warehouse, dni.item_code, dn.posting_date, COUNT(DISTINCT dni.parent)
        FROM `tabDelivery Note Item` dni
        JOIN `tabDelivery Note` dn ON dn.name = dni.parent
        WHERE dni.item_code IN ({", ".join(["%s"] * len(skus))})
          AND dn.custom_mother_warehouse IN ({", ".join(["%s"] * len(warehouses))})
          AND dn.status = 'Completed'
          AND dn.is_return = 0
          AND dn.posting_date >= %s
        GROUP BY dn.custom_mother_warehouse, dni.item_code, dn.posting_date
        ORDER BY dn.posting_date"""
    conn = get_dn_connection(read_timeout=READ_TIMEOUT)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (*skus, *warehouses, since))
            found = cur.fetchall()
    finally:
        conn.close()
    out = defaultdict(list)
    for warehouse, sku, posted, notes in found:
        out[(warehouse, sku)].append((posted, int(notes)))
    return dict(out)


def allocate(invoices, days):
    """How many boxes each invoice line has used.

    invoices: [(invoice date, quantity)] oldest first; days: [(posting date, boxes)] oldest first.
    """
    used = [0] * len(invoices)
    for posted, boxes in days:
        dated = [i for i, (invoiced, _) in enumerate(invoices) if invoiced <= posted]
        if not dated:
            continue  # went out before the first of these invoices, so not one of its boxes
        for i in dated:
            take = min(boxes, max(invoices[i][1] - used[i], 0))
            used[i] += take
            boxes -= take
            if not boxes:
                break
        used[dated[-1]] += boxes  # more than these invoices bought
    return used


def add(rows):
    """Puts "used" (SKU used) on each Invoice Entry row ({warehouse, sku, qty, invoice_date, invoice_number,
    line_no}); None on a line without a SKU. Raises if the ERP can't be read."""
    groups = defaultdict(list)
    for row in rows:
        row["used"] = None
        if row["sku"]:
            groups[(row["warehouse"], row["sku"])].append(row)
    if not groups:
        return
    since = min(row["invoice_date"] for lines in groups.values() for row in lines)
    out = shipped({sku for _, sku in groups}, {warehouse for warehouse, _ in groups}, date.fromisoformat(since))
    for key, lines in groups.items():
        lines.sort(key=lambda r: (r["invoice_date"], r["invoice_number"], r["line_no"]))
        used = allocate([(date.fromisoformat(r["invoice_date"]), r["qty"]) for r in lines], out.get(key, []))
        for row, n in zip(lines, used):
            row["used"] = n
