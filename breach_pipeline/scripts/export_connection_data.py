"""Connection aggregate for the dashboard's Connection view tab.

WHAT "CONNECTED" MEANS HERE
    `origin_hub_out_timestamp` - the shipment leaving the origin hub, i.e. put on
    a line-haul connection. NOTE the column is origin_HUB_out, not "origin city
    out": there is no column of that name in cp_database_N, and this is the only
    origin-side departure timestamp there is. Fill is 99% on Bluedart and 94% on
    Delhivery over 30 days, and it never precedes pickup_date on a single row, so
    it behaves like a genuine post-pickup scan.

THE SLA (user, 10 Sep 2026)
    A shipment picked on day D should be connected by 08:00 on D+1. So:

        Day 1     out <= D+1 08:00      met
        Day 2     out <= D+2 08:00      one day late
        Day 3+    later than that
        Not connected                   no timestamp at all

    Note Day 1 is a ~32-hour window for something picked at midnight and an
    8-hour one for something picked at 23:59 - that is the courier's commitment
    as stated, an absolute morning cutoff rather than an elapsed-time budget.

SCOPE
    Mother warehouses only, and Bluedart and Delhivery only. The SDD/NDD network couriers do not run a
    line-haul connection in this sense - their shipments go out for delivery
    from the same city - so a connection SLA does not apply to them.

RECENT DAYS ARE IMMATURE
    A shipment picked today cannot yet have missed an 08:00-tomorrow deadline,
    so the newest pickup date always looks worse than it will settle at. The tab
    labels the affected rows rather than hiding them.

Env: CONN_DAYS (default 60) - pickup-date window.
"""
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pandas as pd
from db.connection import get_scm_connection

WINDOW_DAYS = int(os.environ.get("CONN_DAYS", "60"))
COURIERS = ("Bluedart", "Delhivery")
IST_TODAY = "DATE(DATE_ADD(NOW(), INTERVAL 330 MINUTE))"

mapping_path = os.path.join(os.path.dirname(__file__), "..", "pincode city mapping .xlsx")
map_df = pd.read_excel(mapping_path)
pincode_to_city = {}
for _, row in map_df.iterrows():
    try:
        pincode_to_city[int(row["Pincode"])] = str(row["City"]).strip()
    except (ValueError, TypeError):
        continue


def resolve_city(pincode, fallback):
    try:
        pin = int(float(str(pincode).strip()))
    except (ValueError, TypeError):
        pin = None
    if pin is not None and pin in pincode_to_city:
        return pincode_to_city[pin]
    return str(fallback).strip() if fallback else "Unknown"


SQL = """
SELECT
    DATE(pickup_date)                                  AS d,
    courier_partner,
    from_warehouse                                     AS wh,
    account_code,
    shipment_weight,
    pickup_pincode, pickup_city,
    CASE
      WHEN origin_hub_out_timestamp IS NULL THEN 'Not connected'
      WHEN origin_hub_out_timestamp <=
           TIMESTAMP(DATE_ADD(DATE(pickup_date), INTERVAL 1 DAY), '08:00:00')
        THEN 'Day 1'
      WHEN origin_hub_out_timestamp <=
           TIMESTAMP(DATE_ADD(DATE(pickup_date), INTERVAL 2 DAY), '08:00:00')
        THEN 'Day 2'
      ELSE 'Day 3+'
    END                                                AS bucket,
    TIMESTAMPDIFF(MINUTE, pickup_date, origin_hub_out_timestamp) AS mins
FROM cp_database_N
WHERE pickup_date >= DATE_SUB({today}, INTERVAL {days} DAY)
  AND pickup_date <  DATE_ADD({today}, INTERVAL 1 DAY)
  AND courier_partner IN ({couriers})
  -- Mother warehouses only (user, 10 Sep 2026). Dark stores and dropship
  -- vendors are dropped rather than shown: 73 of the 80 origins were DKS/DPS
  -- sites carrying 10% of volume between them, which buried the seven
  -- warehouses this measure is actually about.
  AND from_warehouse LIKE 'MWH!_%%' ESCAPE '!'
""".format(today=IST_TODAY, days=WINDOW_DAYS,
           couriers=", ".join("'%s'" % c for c in COURIERS))

BUCKETS = ["Day 1", "Day 2", "Day 3+", "Not connected"]


def main():
    conn = get_scm_connection(read_timeout=600)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT {} AS t".format(IST_TODAY))
            today = cur.fetchone()["t"]
            cur.execute(SQL)
            rows = cur.fetchall()
    finally:
        conn.close()

    rows = [r for r in rows if r["d"] is not None]
    print("Pulled {:,} Bluedart/Delhivery shipments picked in the last {} days"
          .format(len(rows), WINDOW_DAYS), file=sys.stderr)
    if not rows:
        print("Zero rows - refusing to write an empty connection payload", file=sys.stderr)
        return 1

    dims = {"date": [], "courier": [], "origin": [], "warehouse": [],
            "account": [], "slab": []}
    idx = {k: {} for k in dims}

    def enc(f, v):
        m = idx[f]
        if v not in m:
            m[v] = len(dims[f])
            dims[f].append(v)
        return m[v]

    # 0 total, 1 day1, 2 day2, 3 day3plus, 4 not_connected, 5 mins_sum, 6 mins_n
    agg = defaultdict(lambda: [0, 0, 0, 0, 0, 0, 0])
    for r in rows:
        origin = resolve_city(r["pickup_pincode"], r["pickup_city"])
        w = r["shipment_weight"]
        # same 3 kg split the breach tab uses, so a slab means the same thing
        # on both tabs rather than two nearly-identical definitions
        slab = "unknown" if w is None else ("0-3kg" if float(w) <= 3000 else "3+kg")
        key = (r["d"].isoformat(), r["courier_partner"], origin, r["wh"] or "Unknown",
               r["account_code"] or "Unknown", slab)
        a = agg[key]
        a[0] += 1
        a[1 + BUCKETS.index(r["bucket"])] += 1
        if r["mins"] is not None and r["mins"] >= 0:
            a[5] += int(r["mins"])
            a[6] += 1

    facts = []
    for (d, c, o, w, acct, slab), a in agg.items():
        facts.append([enc("date", d), enc("courier", c), enc("origin", o),
                      enc("warehouse", w), enc("account", acct),
                      enc("slab", slab)] + a)

    total = sum(a[0] for a in agg.values())
    d1 = sum(a[1] for a in agg.values())
    payload = {"window_days": WINDOW_DAYS, "generated_for_today": today.isoformat(),
               "buckets": BUCKETS, "shipments": total,
               "dims": dims, "facts": facts}

    out = os.path.join(os.path.dirname(__file__), "..", "data", "connection_data.json")
    with open(out, "w") as f:
        json.dump(payload, f, separators=(",", ":"))

    print("  shipments  : {:,}".format(total), file=sys.stderr)
    print("  connected day 1 : {:,} ({:.1f}%)".format(d1, d1 / total * 100), file=sys.stderr)
    for i, b in enumerate(BUCKETS):
        n = sum(a[1 + i] for a in agg.values())
        print("    {:<16}{:>8,}  {:>5.1f}%".format(b, n, n / total * 100), file=sys.stderr)
    print("  fact rows  : {:,}".format(len(facts)), file=sys.stderr)
    for k, v in dims.items():
        print("    {:<10}{:>5} distinct".format(k, len(v)), file=sys.stderr)
    print("Wrote {} ({:.0f} KB)".format(out, os.path.getsize(out) / 1024), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
