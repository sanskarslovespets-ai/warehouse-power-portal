"""Pendency snapshot: open shipments that are already past promise, or due today/tomorrow.

Unlike the breach views (which look at DELIVERED shipments over a rolling window),
this is a point-in-time snapshot of shipments still in flight. It ages by the hour,
so the generated_at stamp is surfaced prominently in the UI.

TIMEZONE: the database session clock is UTC while the data timestamps are IST.
Every "now"/"today" here is therefore NOW() + 330 minutes. Using bare NOW() silently
under-counts breached pendency by up to 5.5 hours' worth of promises.

Buckets:
  BREACHED     promise timestamp already passed, still no terminal status
  DUE_TODAY    promise falls on today (IST)
  DUE_TOMORROW promise falls on tomorrow (IST)

Scope decisions (see docs/dashboard_logic.md):
  - terminal statuses are excluded outright: the journey is over
  - RTO-* in-flight is excluded by default - those are heading back to origin, so
    a customer promise date no longer means anything. PENDENCY_INCLUDE_RTO=1 keeps them.
  - only shipments created in the last PENDENCY_CREATED_DAYS (default 60) days;
    older open records are overwhelmingly stale rows that were never closed out.
"""
import datetime as _dt
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pandas as pd
from db.connection import get_scm_connection

CREATED_DAYS = int(os.environ.get("PENDENCY_CREATED_DAYS", "60"))
INCLUDE_RTO = os.environ.get("PENDENCY_INCLUDE_RTO", "0") == "1"

TERMINAL = ("Delivered", "RTO-Delivered", "Cancelled", "Lost", "Damaged", "NotServiceable")

# server clock is UTC, data is IST
IST_NOW = "DATE_ADD(NOW(), INTERVAL 330 MINUTE)"
IST_TODAY = "DATE(DATE_ADD(NOW(), INTERVAL 330 MINUTE))"

mapping_path = os.path.join(os.path.dirname(__file__), "..", "pincode city mapping .xlsx")
map_df = pd.read_excel(mapping_path)
pincode_to_city = {}
for _, row in map_df.iterrows():
    try:
        pin = int(row["Pincode"])
    except (ValueError, TypeError):
        continue
    pincode_to_city[pin] = str(row["City"]).strip()


def resolve_city(pincode, fallback_city):
    try:
        pin = int(float(str(pincode).strip()))
    except (ValueError, TypeError):
        pin = None
    if pin is not None and pin in pincode_to_city:
        return pincode_to_city[pin]
    if fallback_city:
        return str(fallback_city).strip()
    return "Unknown"


rto_clause = "" if INCLUDE_RTO else "AND clickpost_unified_status NOT LIKE 'RTO%%'"

SQL = """
SELECT
    clickpost_unified_status AS status,
    courier_partner, account_code, from_warehouse,
    pickup_pincode, pickup_city, drop_pincode, drop_city,
    shipment_weight,
    promise_ts,
    CASE
      WHEN promise_ts < {now} THEN 'BREACHED'
      WHEN DATE(promise_ts) = {today} THEN 'DUE_TODAY'
      ELSE 'DUE_TOMORROW'
    END AS bucket,
    DATEDIFF({today}, DATE(promise_ts)) AS days_late
FROM (
    SELECT
        clickpost_unified_status, courier_partner, account_code, from_warehouse,
        pickup_pincode, pickup_city, drop_pincode, drop_city, shipment_weight,
        CASE WHEN Edd_Sent = FLOOR(Edd_Sent) THEN Edd_Delivery_time
             ELSE DATE_ADD(order_date, INTERVAL ROUND(Edd_Sent * 24 * 60 * 60) SECOND)
        END AS promise_ts
    FROM cp_database_N
    WHERE clickpost_unified_status NOT IN {terminal}
      {rto}
      AND courier_partner <> 'Delhivery Reverse'
      AND Edd_Delivery_time IS NOT NULL
      AND created_at >= DATE_SUB({today}, INTERVAL {days} DAY)
) t
WHERE promise_ts < {now}
   OR DATE(promise_ts) IN ({today}, DATE_ADD({today}, INTERVAL 1 DAY))
""".format(now=IST_NOW, today=IST_TODAY, terminal=str(TERMINAL),
           rto=rto_clause, days=CREATED_DAYS)


def age_bucket(bucket, days_late):
    """How overdue a breached shipment is. Non-breached rows carry no age."""
    if bucket != "BREACHED":
        return ""
    d = int(days_late or 0)
    if d <= 1:
        return "D1"
    if d == 2:
        return "D2"
    if d <= 7:
        return "D3-7"
    return "D8+"


def main():
    conn = get_scm_connection(read_timeout=400)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT {} AS ist_now".format(IST_NOW))
            ist_now = cur.fetchone()["ist_now"]
            cur.execute(SQL)
            rows = cur.fetchall()
    finally:
        conn.close()

    print("Pulled {} open shipments".format(len(rows)), file=sys.stderr)
    if not rows:
        print("Zero rows - refusing to write an empty pendency payload", file=sys.stderr)
        return 1

    agg = defaultdict(int)
    for r in rows:
        w = r["shipment_weight"]
        slab = "unknown" if w is None else ("0-3kg" if float(w) <= 3000 else "3+kg")
        key = (
            r["promise_ts"].date().isoformat() if r["promise_ts"] else "unknown",
            r["courier_partner"] or "Unknown",
            (r["account_code"] or "Unknown").strip() or "Unknown",
            slab,
            r["from_warehouse"] or "Unknown",
            resolve_city(r["pickup_pincode"], r["pickup_city"]),
            resolve_city(r["drop_pincode"], r["drop_city"]),
            r["status"] or "Unknown",
            r["bucket"],
            age_bucket(r["bucket"], r["days_late"]),
        )
        agg[key] += 1

    dims = {"pdate": [], "courier": [], "account": [], "slab": [], "warehouse": [],
            "origin": [], "dest": [], "status": [], "bucket": [], "age": []}
    idx = {k: {} for k in dims}

    def enc(field, val):
        m = idx[field]
        if val not in m:
            m[val] = len(dims[field])
            dims[field].append(val)
        return m[val]

    facts = []
    for (pd, c, a, sl, wh, o, d, st, b, ag), n in agg.items():
        facts.append([enc("pdate", pd), enc("courier", c), enc("account", a), enc("slab", sl),
                      enc("warehouse", wh), enc("origin", o), enc("dest", d),
                      enc("status", st), enc("bucket", b), enc("age", ag), n])

    payload = {
        "generated_at_ist": ist_now.strftime("%Y-%m-%d %H:%M"),
        "created_window_days": CREATED_DAYS,
        "include_rto": INCLUDE_RTO,
        "dims": dims,
        "facts": facts,
    }

    out = os.path.join(os.path.dirname(__file__), "..", "data", "pendency_data.json")
    with open(out, "w") as f:
        json.dump(payload, f, separators=(",", ":"))

    totals = defaultdict(int)
    for (_, _, _, _, _, _, _, _, b, _), n in agg.items():
        totals[b] += n
    print("  as of (IST) : {}".format(payload["generated_at_ist"]), file=sys.stderr)
    for b in ("BREACHED", "DUE_TODAY", "DUE_TOMORROW"):
        print("  {:<13} {:>7,}".format(b, totals.get(b, 0)), file=sys.stderr)
    print("  fact rows   : {:,}".format(len(facts)), file=sys.stderr)
    print("Wrote {} ({:.0f} KB)".format(out, os.path.getsize(out) / 1024), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
