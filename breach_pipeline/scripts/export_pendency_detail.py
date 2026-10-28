"""Shipment-level pendency detail - the sheet you hand to a courier.

One row per open shipment that is already past promise or due today/tomorrow,
with the fields needed to chase it: AWB, current location, last scan, attempt
count and failure reason.

PII: deliberately EXCLUDES the free-text remark columns (`latest_remark`,
`remark_of_last_failed_delivery`, `remark_of_last_pickup`). Those are agent-typed
and routinely contain customer names, phone numbers and addresses, which the
project's standing rule forbids fetching. `reason_for_last_failed_delivery` is
kept because it is a structured reason code. No customer name, phone, email or
street address is selected anywhere here.

Writes both:
  data/pendency_detail.json  - embedded in the dashboard for in-page export
  data/pendency_detail.csv   - a ready-to-send file
"""
import csv
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pandas as pd
from db.connection import get_scm_connection

CREATED_DAYS = int(os.environ.get("PENDENCY_CREATED_DAYS", "60"))
INCLUDE_RTO = os.environ.get("PENDENCY_INCLUDE_RTO", "0") == "1"
TERMINAL = ("Delivered", "RTO-Delivered", "Cancelled", "Lost", "Damaged", "NotServiceable")

# the database session clock is UTC while the data is IST - see export_pendency_data.py
IST_NOW = "DATE_ADD(NOW(), INTERVAL 330 MINUTE)"
IST_TODAY = "DATE(DATE_ADD(NOW(), INTERVAL 330 MINUTE))"

COLUMNS = [
    "awb", "order_id", "courier", "account_code", "warehouse",
    "origin_city", "dest_city", "drop_pincode", "weight_g",
    "status", "current_location", "promise_ts", "days_late", "bucket",
    "ofd_attempts", "last_failed_reason", "dest_hub_in_scan", "last_update", "created_at",
]

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
SELECT awb, order_id, courier_partner, account_code, from_warehouse,
       pickup_pincode, pickup_city, drop_pincode, drop_city, shipment_weight,
       clickpost_unified_status AS status, current_location,
       out_for_delivery_attempts, reason_for_last_failed_delivery,
       destination_hub_in_scan_time, updated_at, created_at,
       promise_ts,
       CASE WHEN promise_ts < {now} THEN 'BREACHED'
            WHEN DATE(promise_ts) = {today} THEN 'DUE_TODAY'
            ELSE 'DUE_TOMORROW' END AS bucket,
       DATEDIFF({today}, DATE(promise_ts)) AS days_late
FROM (
    SELECT awb, order_id, courier_partner, account_code, from_warehouse,
           pickup_pincode, pickup_city, drop_pincode, drop_city, shipment_weight,
           clickpost_unified_status, current_location,
           out_for_delivery_attempts, reason_for_last_failed_delivery,
           destination_hub_in_scan_time, updated_at, created_at,
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
ORDER BY days_late DESC, courier_partner
""".format(now=IST_NOW, today=IST_TODAY, terminal=str(TERMINAL),
           rto=rto_clause, days=CREATED_DAYS)


def fmt(v):
    if v is None:
        return ""
    return str(v)


def main():
    conn = get_scm_connection(read_timeout=400)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT {} AS n".format(IST_NOW))
            ist_now = cur.fetchone()["n"]
            cur.execute(SQL)
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        print("Zero rows - refusing to write an empty detail payload", file=sys.stderr)
        return 1

    out_rows = []
    for r in rows:
        out_rows.append([
            fmt(r["awb"]), fmt(r["order_id"]), fmt(r["courier_partner"]),
            fmt(r["account_code"]).strip(), fmt(r["from_warehouse"]),
            resolve_city(r["pickup_pincode"], r["pickup_city"]),
            resolve_city(r["drop_pincode"], r["drop_city"]),
            fmt(r["drop_pincode"]),
            "" if r["shipment_weight"] is None else str(int(float(r["shipment_weight"]))),
            fmt(r["status"]), fmt(r["current_location"]),
            fmt(r["promise_ts"]),
            "" if r["days_late"] is None else str(int(r["days_late"])),
            fmt(r["bucket"]),
            "" if r["out_for_delivery_attempts"] is None else str(int(r["out_for_delivery_attempts"])),
            fmt(r["reason_for_last_failed_delivery"]),
            fmt(r["destination_hub_in_scan_time"]),
            fmt(r["updated_at"]), fmt(r["created_at"]),
        ])

    base = os.path.join(os.path.dirname(__file__), "..", "data")
    csv_path = os.path.join(base, "pendency_detail.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        w.writerows(out_rows)

    json_path = os.path.join(base, "pendency_detail.json")
    with open(json_path, "w") as f:
        json.dump({"generated_at_ist": ist_now.strftime("%Y-%m-%d %H:%M"),
                   "columns": COLUMNS, "rows": out_rows},
                  f, separators=(",", ":"))

    from collections import Counter
    c = Counter(r[13] for r in out_rows)
    print("rows        : {:,}".format(len(out_rows)), file=sys.stderr)
    for k in ("BREACHED", "DUE_TODAY", "DUE_TOMORROW"):
        print("  {:<13} {:>7,}".format(k, c.get(k, 0)), file=sys.stderr)
    print("csv         : {} ({:.0f} KB)".format(csv_path, os.path.getsize(csv_path) / 1024), file=sys.stderr)
    print("json        : {:.0f} KB".format(os.path.getsize(json_path) / 1024), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
