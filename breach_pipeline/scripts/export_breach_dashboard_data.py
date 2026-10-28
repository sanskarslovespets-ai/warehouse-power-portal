import sys
import os
import datetime as _dt
import json
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pandas as pd
from db.connection import get_scm_connection

# Rolling window by default so scheduled refreshes always pick up new data:
# roughly the last 3.5 months, ending tomorrow (exclusive) so today is included.
_today = _dt.date.today()
START_DATE = os.environ.get("DASH_START_DATE", (_today - _dt.timedelta(days=107)).isoformat())
END_DATE_EXCLUSIVE = os.environ.get("DASH_END_DATE_EXCLUSIVE", (_today + _dt.timedelta(days=1)).isoformat())
# Cap destination-city cardinality so the payload stays small over long windows;
# everything outside the top N collapses into a single "Other" bucket.
# Lowered 180 -> 130 on 10 Sep 2026 to make room for the RAD-to-OFD tab: this
# payload is by far the page's largest, and the destination tail is where the
# slack is. Raising it again means finding the space somewhere else first.
TOP_DEST = int(os.environ.get("DASH_TOP_DEST", "130"))

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


conn = get_scm_connection(read_timeout=600)
try:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                DATE(delivery_date) AS d,
                courier_partner, account_code, from_warehouse,
                pickup_pincode, pickup_city, drop_pincode, drop_city,
                shipment_weight, Edd_Sent,
                -- promise type (user rule, 29 Aug 2026):
                --   QC       promise above 0 and under 129 minutes
                --   SDD      Edd_Sent = 0, or 129 minutes up to and including a 1-day promise
                --   Standard anything longer than 1 day
                -- Edd_Sent = 0 sits in SDD, not QC: the breach test treats it as a
                -- whole-day promise (0 = FLOOR(0)) and measures it against
                -- Edd_Delivery_time, so bucketing it as quick commerce would disagree
                -- with how it is actually judged.
                CASE
                  WHEN Edd_Sent IS NULL THEN 'Unknown'
                  WHEN Edd_Sent = 0 THEN 'SDD'
                  WHEN ROUND(Edd_Sent * 24 * 60) < 129 THEN 'QC'
                  WHEN Edd_Sent <= 1 THEN 'SDD'
                  ELSE 'Standard'
                END AS ptype,
                TIMESTAMPDIFF(SECOND, pickup_date, delivery_date) AS p2d_seconds,
                CASE
                  WHEN Edd_Delivery_time IS NULL THEN 'NA'
                  WHEN Edd_Sent = FLOOR(Edd_Sent) AND delivery_date > Edd_Delivery_time THEN 'BREACH'
                  WHEN Edd_Sent <> FLOOR(Edd_Sent)
                       AND delivery_date > DATE_ADD(order_date, INTERVAL ROUND(Edd_Sent * 24 * 60 * 60) SECOND) THEN 'BREACH'
                  WHEN Edd_Sent <> FLOOR(Edd_Sent) THEN 'DAY_0'
                  WHEN DATE(delivery_date) < DATE(Edd_Delivery_time) THEN 'UNDER_PROMISE'
                  ELSE 'DAY_0'
                END AS st,
                -- signed day delta vs the promise date: negative = early, 0 = same day, positive = late
                CASE
                  WHEN Edd_Delivery_time IS NULL THEN NULL
                  WHEN Edd_Sent = FLOOR(Edd_Sent)
                    THEN DATEDIFF(DATE(delivery_date), DATE(Edd_Delivery_time))
                  ELSE DATEDIFF(DATE(delivery_date),
                                DATE(DATE_ADD(order_date, INTERVAL ROUND(Edd_Sent * 24 * 60 * 60) SECOND)))
                END AS day_delta,

                -- ---- RAD basis: fractional EDD measured against rider-reached-drop
                --      (falling back to delivery_date - 5 min handshake buffer) ----
                CASE
                  WHEN Edd_Delivery_time IS NULL THEN 'NA'
                  WHEN Edd_Sent = FLOOR(Edd_Sent) AND delivery_date > Edd_Delivery_time THEN 'BREACH'
                  WHEN Edd_Sent <> FLOOR(Edd_Sent)
                       AND COALESCE(rider_reached_drop_location_first, DATE_SUB(delivery_date, INTERVAL 5 MINUTE))
                           > DATE_ADD(order_date, INTERVAL ROUND(Edd_Sent * 24 * 60 * 60) SECOND) THEN 'BREACH'
                  WHEN Edd_Sent <> FLOOR(Edd_Sent) THEN 'DAY_0'
                  WHEN DATE(delivery_date) < DATE(Edd_Delivery_time) THEN 'UNDER_PROMISE'
                  ELSE 'DAY_0'
                END AS st_rad,
                CASE
                  WHEN Edd_Delivery_time IS NULL THEN NULL
                  WHEN Edd_Sent = FLOOR(Edd_Sent)
                    THEN DATEDIFF(DATE(delivery_date), DATE(Edd_Delivery_time))
                  ELSE DATEDIFF(
                         DATE(COALESCE(rider_reached_drop_location_first, DATE_SUB(delivery_date, INTERVAL 5 MINUTE))),
                         DATE(DATE_ADD(order_date, INTERVAL ROUND(Edd_Sent * 24 * 60 * 60) SECOND)))
                END AS day_delta_rad
            FROM cp_database_N
            WHERE delivery_date >= %s AND delivery_date < %s
              AND courier_partner != 'Delhivery Reverse'
            """,
            (START_DATE, END_DATE_EXCLUSIVE),
        )
        rows = cur.fetchall()
finally:
    conn.close()

print(f"Pulled {len(rows)} rows", file=sys.stderr)

# --- work out which destination cities to keep at full granularity ---
_dest_vol = defaultdict(int)
for r in rows:
    _dest_vol[resolve_city(r["drop_pincode"], r["drop_city"])] += 1
KEEP_DEST = set(sorted(_dest_vol, key=lambda c: -_dest_vol[c])[:TOP_DEST])
_kept = sum(_dest_vol[c] for c in KEEP_DEST)
print(f"Destination cities: {len(_dest_vol)} distinct -> keeping top {len(KEEP_DEST)} "
      f"({_kept/len(rows)*100:.1f}% of volume), rest bucketed as 'Other'", file=sys.stderr)

# grain: date | courier | account_code | weight_slab | warehouse | origin | dest
agg = defaultdict(lambda: [0, 0, 0, 0, 0, 0.0, 0, 0.0, 0, 0, 0, 0, 0, 0, 0,
                           0, 0, 0, 0, 0, 0, 0, 0])
# 0 total, 1 under, 2 day0, 3 breach, 4 na, 5 p2d_sum, 6 p2d_n, 7 edd_sum, 8 edd_n,
# 9 under_d1, 10 under_d2, 11 under_d3plus, 12 breach_d1, 13 breach_d2, 14 breach_d3plus,
# RAD basis: 15 under, 16 day0, 17 breach, 18 u_d1, 19 u_d2, 20 u_d3plus is folded ->
# 15 r_under, 16 r_day0, 17 r_breach, 18 r_bd1, 19 r_bd2, 20 r_bd3plus, 21 r_ud1, 22 r_ud2_plus

for r in rows:
    w = r["shipment_weight"]
    slab = "unknown" if w is None else ("0-3kg" if float(w) <= 3000 else "3+kg")
    origin = resolve_city(r["pickup_pincode"], r["pickup_city"])
    dest = resolve_city(r["drop_pincode"], r["drop_city"])
    if dest not in KEEP_DEST:
        dest = "Other"
    key = (
        r["d"].isoformat(),
        r["courier_partner"] or "Unknown",
        (r["account_code"] or "Unknown").strip() or "Unknown",
        slab,
        r["from_warehouse"] or "Unknown",
        origin,
        dest,
        r["ptype"] or "Unknown",
    )
    a = agg[key]
    a[0] += 1
    st = r["st"]
    dd = r["day_delta"]
    if st == "UNDER_PROMISE":
        a[1] += 1
        # early: delta is negative
        n = -int(dd) if dd is not None else 1
        if n <= 1:
            a[9] += 1
        elif n == 2:
            a[10] += 1
        else:
            a[11] += 1
    elif st == "DAY_0":
        a[2] += 1
    elif st == "BREACH":
        a[3] += 1
        # late: delta positive; a fractional-EDD same-day-late lands at 0 -> counts as day 1
        n = int(dd) if dd is not None else 1
        if n <= 1:
            a[12] += 1
        elif n == 2:
            a[13] += 1
        else:
            a[14] += 1
    else:
        a[4] += 1

    # ---- RAD basis ----
    st_r = r["st_rad"]
    dr = r["day_delta_rad"]
    if st_r == "UNDER_PROMISE":
        a[15] += 1
        n = -int(dr) if dr is not None else 1
        if n <= 1:
            a[21] += 1
        else:
            a[22] += 1
    elif st_r == "DAY_0":
        a[16] += 1
    elif st_r == "BREACH":
        a[17] += 1
        n = int(dr) if dr is not None else 1
        if n <= 1:
            a[18] += 1
        elif n == 2:
            a[19] += 1
        else:
            a[20] += 1

    if r["p2d_seconds"] is not None and r["p2d_seconds"] >= 0:
        a[5] += r["p2d_seconds"] / 86400.0
        a[6] += 1
    if r["Edd_Sent"] is not None:
        a[7] += float(r["Edd_Sent"])
        a[8] += 1

# dictionary-encode string dimensions to keep the payload small
dims = {"date": [], "courier": [], "account": [], "slab": [], "warehouse": [], "origin": [], "dest": [],
        "ptype": []}
idx = {k: {} for k in dims}


def enc(field, val):
    m = idx[field]
    if val not in m:
        m[val] = len(dims[field])
        dims[field].append(val)
    return m[val]


facts = []
for (d, c, ac, sl, wh, o, de, pt), a in agg.items():
    facts.append([
        enc("date", d), enc("courier", c), enc("account", ac), enc("slab", sl),
        enc("warehouse", wh), enc("origin", o), enc("dest", de),
        a[0], a[1], a[2], a[3], a[4],
        round(a[5], 3), a[6], round(a[7], 3), a[8],
        a[9], a[10], a[11], a[12], a[13], a[14],
        a[15], a[16], a[17], a[18], a[19], a[20], a[21], a[22],
        enc("ptype", pt),
    ])

payload = {
    "generated_for": {"start": START_DATE, "end_exclusive": END_DATE_EXCLUSIVE},
    "dims": dims,
    "facts": facts,
}

out = os.path.join(os.path.dirname(__file__), "..", "data", "breach_dashboard_data.json")
with open(out, "w") as f:
    json.dump(payload, f, separators=(",", ":"))

print(f"Fact rows: {len(facts)}", file=sys.stderr)
for k, v in dims.items():
    print(f"  {k}: {len(v)} distinct", file=sys.stderr)
print(f"Wrote {out} ({os.path.getsize(out)/1024/1024:.2f} MB)", file=sys.stderr)
