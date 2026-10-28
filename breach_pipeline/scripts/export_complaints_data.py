"""Complaints aggregate from CX_data.CDC_Nugget for the dashboard's Complaints tab.

NOTE: CX_data.CDC_Nugget was rebuilt on 20 Aug 2026 with snake_case columns -
Created_time -> created_at, Channel -> channel_id, Status -> status, SPAM_FLAG -> is_spam.
Its created_at is the TICKET creation time, not to be confused with cp_database_N.created_at
which is the AWB generation timestamp.

COURIER ATTRIBUTION - two hops, per the user's rule:

  1. `awb_number` matched directly against `scm.cp_database_N.awb`.
  2. Where that fails and the value is a DN number (the awb field is frequently
     populated with a DN instead of an AWB), resolve it through the ERP:
         awb_number -> `tabDelivery Note`.name -> custom_awb_number -> cp_database_N.awb
     94% of DN-form values match a Delivery Note, and 92% of those carry an AWB.
     `tabDelivery Note.courier_partner` is NOT usable - it is null throughout.

  order_id is deliberately NOT used as a fallback: it is not unique (about 1.40
  shipments per order), so joining on it fans out and inflates ticket counts.

The ERP is a separate server from `scm`, so hop 2 cannot be a SQL join - the DN to
AWB map is fetched in batches and applied in Python.

Source-table traps handled (see docs): empty-string rather than NULL on
category/issue/sub_issue; utf8mb4_unicode_ci vs utf8mb4_0900_ai_ci collation on any
cross-schema join; Created_time is IST while the server clock is UTC.
"""
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from db.connection import get_scm_connection, get_erp_connection

WINDOW_DAYS = int(os.environ.get("COMPLAINTS_DAYS", "180"))

# Scope: an explicit list of issue + sub-issue pairs supplied by the user (18 Aug 2026),
# narrowed on 19 Aug to LAST-MILE faults only - Missing Items and Wrong Item Sent were
# dropped as parcel-content/packing issues rather than delivery issues.
# All 24 were verified to exist in the table; strings below are the DB's own spelling
# (the user's "not dispatched from the warehouse" is stored as "Not Dispatched from Warehouse").
PAIRS = [
    ("Cancellation", "Cancelling Due to Delay"),
    ("Cancellation", "Cancelled but Still Delivered"),
    ("Within EDD", "Tracking Not Moving"),
    ("Delayed", "Not Dispatched from Warehouse"),
    ("Delayed", "Stuck in Transit"),
    ("Delayed", "Rx Hold Not Released"),
    ("Failed Attempt", "Fake Attempt (Nobody Came)"),
    ("Failed Attempt", "Missed the Delivery"),
    ("Why Was My Order Cancelled", "Non-Serviceable Area (NSZ)"),
    ("Why Was My Order Cancelled", "RTO After Multiple Attempts"),
    ("MDND", "Product Found"),
    ("MDND", "Product Not Found"),
    ("Received Damaged Product", "Product Broken / Damaged"),
    ("Received Damaged Product", "Packaging Damaged (Inner / Outer)"),
    ("Received Damaged Product", "Used Product Delivered"),
    ("Expired Products Delivered", "Expired Product Delivered"),
    ("Expired Products Delivered", "Near to Expiry (<3 Months)"),
]
PAIR_SQL = ", ".join("(%s, %s)" % (repr(i), repr(sub)) for i, sub in PAIRS).replace("'", "'")

IST_TODAY = "DATE(DATE_ADD(NOW(), INTERVAL 330 MINUTE))"
BATCH = 900

CHANNEL = {"4": "Inbound CDC email", "5": "Inbound CDC call",
           "13": "Customer returns", "21": "Channel 21"}

TICKETS_SQL = """
SELECT
    DATE(g.created_at)                 AS d,
    NULLIF(TRIM(g.category), '')         AS category,
    NULLIF(TRIM(g.issue), '')            AS issue,
    NULLIF(TRIM(g.sub_issue), '')        AS sub_issue,
    g.channel_id                            AS channel,
    NULLIF(TRIM(g.status), '')           AS status,
    NULLIF(TRIM(g.awb_number), '')       AS awb_number,
    c.courier_partner                    AS courier,
    (g.resolved_at IS NOT NULL)          AS resolved
FROM CX_data.CDC_Nugget g
LEFT JOIN scm.cp_database_N c
       ON c.awb = g.awb_number COLLATE utf8mb4_0900_ai_ci
WHERE g.created_at >= DATE_SUB({today}, INTERVAL {days} DAY)
  AND g.created_at <  DATE_ADD({today}, INTERVAL 1 DAY)
  AND (TRIM(g.issue), TRIM(g.sub_issue)) IN ({pairs})
""".format(today=IST_TODAY, days=WINDOW_DAYS, pairs=PAIR_SQL)


DELIVERED_SQL = """
SELECT DATE(delivery_date) AS d, courier_partner AS courier, COUNT(*) AS n
FROM cp_database_N
WHERE delivery_date >= DATE_SUB({today}, INTERVAL {days} DAY)
  AND delivery_date <  DATE_ADD({today}, INTERVAL 1 DAY)
  AND courier_partner <> 'Delhivery Reverse'
GROUP BY d, courier
""".format(today=IST_TODAY, days=WINDOW_DAYS)


def blank(v):
    if v is None:
        return "(blank)"
    v = str(v).strip()
    return v if v else "(blank)"


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def resolve_via_dn(dn_values):
    """DN -> AWB (ERP), then AWB -> courier (scm). Returns {dn: courier}."""
    if not dn_values:
        return {}
    dn_to_awb = {}
    erp = get_erp_connection(read_timeout=300)
    try:
        with erp.cursor() as cur:
            for part in chunks(dn_values, BATCH):
                ph = ",".join(["%s"] * len(part))
                cur.execute(
                    "SELECT name, custom_awb_number FROM `tabDelivery Note` "
                    "WHERE name IN ({}) AND custom_awb_number IS NOT NULL "
                    "AND custom_awb_number <> ''".format(ph), part)
                for r in cur.fetchall():
                    dn_to_awb[r["name"]] = str(r["custom_awb_number"]).strip()
    finally:
        erp.close()
    print("  DN -> AWB resolved      : {:,} of {:,}".format(len(dn_to_awb), len(dn_values)),
          file=sys.stderr)
    if not dn_to_awb:
        return {}

    awbs = sorted(set(dn_to_awb.values()))
    awb_to_courier = {}
    scm = get_scm_connection(read_timeout=300)
    try:
        with scm.cursor() as cur:
            for part in chunks(awbs, BATCH):
                ph = ",".join(["%s"] * len(part))
                cur.execute(
                    "SELECT awb, courier_partner FROM cp_database_N "
                    "WHERE awb IN ({})".format(ph), part)
                for r in cur.fetchall():
                    if r["courier_partner"]:
                        awb_to_courier[str(r["awb"]).strip()] = r["courier_partner"]
    finally:
        scm.close()
    print("  AWB -> courier resolved : {:,} of {:,}".format(len(awb_to_courier), len(awbs)),
          file=sys.stderr)

    return {dn: awb_to_courier[a] for dn, a in dn_to_awb.items() if a in awb_to_courier}


def main():
    conn = get_scm_connection(read_timeout=900)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT {} AS t".format(IST_TODAY))
            today = cur.fetchone()["t"]
            cur.execute(TICKETS_SQL)
            rows = cur.fetchall()
            # denominator for rate views: delivered shipments per courier per day
            cur.execute(DELIVERED_SQL)
            delivered_rows = cur.fetchall()
    finally:
        conn.close()

    print("Pulled {:,} tickets".format(len(rows)), file=sys.stderr)
    if not rows:
        print("Zero rows - refusing to write an empty complaints payload", file=sys.stderr)
        return 1

    direct = sum(1 for r in rows if r["courier"])
    pending_dn = sorted({r["awb_number"].strip() for r in rows
                         if not r["courier"] and r["awb_number"]
                         and r["awb_number"].strip().upper().startswith("DN")})
    print("  matched on AWB directly : {:,}".format(direct), file=sys.stderr)
    print("  DN-form values to try   : {:,}".format(len(pending_dn)), file=sys.stderr)
    dn_courier = resolve_via_dn(pending_dn)

    dims = {"date": [], "category": [], "issue": [], "sub_issue": [],
            "channel": [], "status": [], "courier": [], "source": []}
    idx = {k: {} for k in dims}

    def enc(f, v):
        m = idx[f]
        if v not in m:
            m[v] = len(dims[f])
            dims[f].append(v)
        return m[v]

    agg = defaultdict(lambda: [0, 0])
    via_dn = 0
    for r in rows:
        courier, src = r["courier"], "AWB"
        if not courier and r["awb_number"]:
            c2 = dn_courier.get(r["awb_number"].strip())
            if c2:
                courier, src = c2, "DN"
                via_dn += 1
        if not courier:
            courier, src = "Unmapped", "none"
        ch = str(r["channel"]).strip() if r["channel"] is not None else ""
        key = (r["d"].isoformat(), blank(r["category"]), blank(r["issue"]),
               blank(r["sub_issue"]),
               CHANNEL.get(ch, "Channel " + ch if ch else "(blank)"),
               blank(r["status"]), courier, src)
        a = agg[key]
        a[0] += 1
        a[1] += 1 if r["resolved"] else 0

    facts = []
    for (d, cat, iss, sub, ch, st, cour, src), v in agg.items():
        facts.append([enc("date", d), enc("category", cat), enc("issue", iss),
                      enc("sub_issue", sub), enc("channel", ch), enc("status", st),
                      enc("courier", cour), enc("source", src), v[0], v[1]])

    total = len(rows)
    mapped = direct + via_dn
    delivered = {}
    for r in delivered_rows:
        delivered.setdefault(r["d"].isoformat(), {})[r["courier"] or "Unknown"] = int(r["n"])

    payload = {"pairs": [list(x) for x in PAIRS], "delivered": delivered, "window_days": WINDOW_DAYS, "generated_for_today": today.isoformat(),
               "tickets": total, "mapped": mapped, "mapped_via_dn": via_dn,
               "dims": dims, "facts": facts}

    out = os.path.join(os.path.dirname(__file__), "..", "data", "complaints_data.json")
    with open(out, "w") as f:
        json.dump(payload, f, separators=(",", ":"))

    print("\n  tickets            : {:,}".format(total), file=sys.stderr)
    print("  courier via AWB    : {:,}  ({:.1f}%)".format(direct, direct / total * 100), file=sys.stderr)
    print("  courier via DN hop : {:,}  ({:.1f}%)".format(via_dn, via_dn / total * 100), file=sys.stderr)
    print("  total mapped       : {:,}  ({:.1f}%)".format(mapped, mapped / total * 100), file=sys.stderr)
    print("  fact rows          : {:,}".format(len(facts)), file=sys.stderr)
    print("  delivered days     : {:,} ({:,} shipments)".format(
        len(delivered), sum(sum(v.values()) for v in delivered.values())), file=sys.stderr)
    print("Wrote {} ({:.0f} KB)".format(out, os.path.getsize(out) / 1024), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
