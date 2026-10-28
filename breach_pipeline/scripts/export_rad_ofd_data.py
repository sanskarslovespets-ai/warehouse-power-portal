"""RAD-to-OFD aging aggregate for the dashboard's RAD to OFD tab.

WHAT IS BEING MEASURED
    From the shipment reaching the destination hub to it going out for delivery:

        destination_hub_in_scan_time  ->  out_for_delivery_1st_attempt

    NOTE ON THE NAME. "RAD" elsewhere in this dashboard means
    `rider_reached_drop_location_first` - the rider at the customer's door, used
    by the breach tab's RAD basis. Here RAD means arrival at the DESTINATION HUB,
    which is a different and much earlier event. The two are not interchangeable
    and the tab footer says so.

THE SLA (user, 10 Sep 2026)
    OFD should happen the same day the shipment lands, or the next day at worst:

        Same day        DATE(ofd) = DATE(arrival)
        Next day        DATE(ofd) = DATE(arrival) + 1
        D+2 or later    beyond that
        No OFD          no first-attempt timestamp

    Calendar days, not a clock cutoff - unlike the connection tab, which is
    scored against an absolute 08:00. That is what "same day or max next day"
    means as stated.

    36 shipments in 60 days carry an OFD timestamp EARLIER than the arrival scan
    (0.03%, mostly Bluedart). They are counted as Same day rather than given a
    bucket of their own; a negative gap is a scan-ordering artefact, not a
    service outcome.

SCOPE
    The seven mother warehouses, Bluedart and Delhivery only.

DESTINATION HUB
    Resolved from the courier's own hub mapping, keyed on drop pincode:

      Bluedart   `dashboards/BD air hub mapping.xlsx` + `bd surface ...` ->
                 CSCRCDDESC. Checked across all five origin sheets in both
                 files: 12,468 pincodes, ZERO of which map to more than one hub,
                 so one pincode->hub table is safe regardless of origin.

      Delhivery  `dashboards/delhivery hub mapping.xlsx` -> `Dispatch Center`,
                 NOT `incoming_center`. The two differ on 12.3% of pincodes and
                 dispatch is the one that puts a shipment out for delivery,
                 which is the event being timed here.

WINDOW
    30 days, not the 60 the other tabs use. The fact key crosses date x account
    x warehouse x geography, so 60 days came to ~2.0 MB and would have pushed
    the artifact page past its 16 MB limit. Aging is an operational measure -
    the recent month is what anyone acts on - so the shorter window costs little
    (user's call, 10 Sep 2026).

Env: RADOFD_DAYS (default 30), RADOFD_TOP_HUB (1500), RADOFD_TOP_CITY (200).
"""
import json
import os
import sys
import warnings
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
warnings.filterwarnings("ignore")
import pandas as pd
from db.connection import get_scm_connection

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WINDOW_DAYS = int(os.environ.get("RADOFD_DAYS", "30"))
TOP_HUB = int(os.environ.get("RADOFD_TOP_HUB", "1500"))
TOP_CITY = int(os.environ.get("RADOFD_TOP_CITY", "200"))
COURIERS = ("Bluedart", "Delhivery")
IST_TODAY = "DATE(DATE_ADD(NOW(), INTERVAL 330 MINUTE))"
BUCKETS = ["Same day", "Next day", "D+2 or later", "No OFD"]


def load_city_map():
    df = pd.read_excel(os.path.join(BASE, "pincode city mapping .xlsx"))
    out = {}
    for _, r in df.iterrows():
        try:
            out[int(r["Pincode"])] = str(r["City"]).strip()
        except (ValueError, TypeError):
            continue
    return out


def load_hub_maps():
    """pincode -> hub, per courier."""
    bd = {}
    for name in ("BD air hub mapping.xlsx", "bd surface hub mapping.xlsx"):
        path = os.path.join(BASE, "dashboards", name)
        if not os.path.exists(path):
            print("  missing %s - Bluedart hubs will be Unmapped" % name, file=sys.stderr)
            continue
        xl = pd.ExcelFile(path)
        for sh in xl.sheet_names:
            d = xl.parse(sh)
            if "CPINCODE" not in d.columns or "CSCRCDDESC" not in d.columns:
                continue
            for pin, hub in zip(d["CPINCODE"], d["CSCRCDDESC"]):
                try:
                    p = int(pin)
                except (ValueError, TypeError):
                    continue
                if pd.notna(hub):
                    bd.setdefault(p, str(hub).strip())

    dl = {}
    path = os.path.join(BASE, "dashboards", "delhivery hub mapping.xlsx")
    if os.path.exists(path):
        d = pd.read_excel(path, sheet_name="data")
        for pin, hub in zip(d["Pincode"], d["Dispatch Center"]):
            try:
                p = int(pin)
            except (ValueError, TypeError):
                continue
            if pd.notna(hub):
                dl.setdefault(p, str(hub).strip())
    else:
        print("  missing delhivery hub mapping - Delhivery hubs will be Unmapped",
              file=sys.stderr)

    print("hub maps: Bluedart {:,} pincodes, Delhivery {:,} pincodes"
          .format(len(bd), len(dl)), file=sys.stderr)
    # Loud, because the failure mode is silent and convincing: with no mapping
    # every shipment becomes "Unmapped" and the hub view renders a single row
    # that still totals correctly, so nothing looks broken.
    if not bd or not dl:
        print("!" * 70, file=sys.stderr)
        print("WARNING: a hub mapping is EMPTY - the hub view will be all "
              "'Unmapped'. Check the workbooks reached the image/working dir.",
              file=sys.stderr)
        print("!" * 70, file=sys.stderr)
    return {"Bluedart": bd, "Delhivery": dl}


SQL = """
SELECT
    DATE(destination_hub_in_scan_time)                 AS d,
    courier_partner,
    from_warehouse                                     AS wh,
    account_code,
    shipment_weight,
    drop_pincode, drop_city,
    CASE
      WHEN out_for_delivery_1st_attempt IS NULL THEN 'No OFD'
      WHEN DATEDIFF(DATE(out_for_delivery_1st_attempt),
                    DATE(destination_hub_in_scan_time)) <= 0 THEN 'Same day'
      WHEN DATEDIFF(DATE(out_for_delivery_1st_attempt),
                    DATE(destination_hub_in_scan_time)) = 1 THEN 'Next day'
      ELSE 'D+2 or later'
    END                                                AS bucket
FROM cp_database_N
WHERE destination_hub_in_scan_time >= DATE_SUB({today}, INTERVAL {days} DAY)
  AND destination_hub_in_scan_time <  DATE_ADD({today}, INTERVAL 1 DAY)
  AND courier_partner IN ({couriers})
  AND from_warehouse LIKE 'MWH!_%%' ESCAPE '!'
""".format(today=IST_TODAY, days=WINDOW_DAYS,
           couriers=", ".join("'%s'" % c for c in COURIERS))


def main():
    city_map = load_city_map()
    hub_maps = load_hub_maps()

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
    print("Pulled {:,} shipments that reached destination in the last {} days"
          .format(len(rows), WINDOW_DAYS), file=sys.stderr)
    if not rows:
        print("Zero rows - refusing to write an empty payload", file=sys.stderr)
        return 1

    def pin_of(r):
        try:
            return int(float(str(r["drop_pincode"]).strip()))
        except (ValueError, TypeError):
            return None

    # TWO fact tables, not one crossed table. City and hub are both long-tailed
    # (2,540 cities, 5,965 hubs) and crossing them cost 78k rows / 2.3 MB, which
    # the page cannot afford. The tab shows them as separate views anyway, so
    # each gets its own table carrying the shared filter dimensions. Rows scale
    # with city + hub instead of city x hub.
    def build(geo_of, geo_name, keep_set, other_label):
        dims = {"date": [], "courier": [], "warehouse": [], "account": [],
                "slab": [], geo_name: []}
        idx = {k: {} for k in dims}

        def enc(f, v):
            m = idx[f]
            if v not in m:
                m[v] = len(dims[f])
                dims[f].append(v)
            return m[v]

        agg = defaultdict(lambda: [0, 0, 0, 0, 0])
        for r in rows:
            g = geo_of(r)
            if keep_set is not None and g not in keep_set:
                g = other_label
            w = r["shipment_weight"]
            slab = "unknown" if w is None else ("0-3kg" if float(w) <= 3000 else "3+kg")
            key = (r["d"].isoformat(), r["courier_partner"], r["wh"] or "Unknown",
                   r["account_code"] or "Unknown", slab, g)
            a = agg[key]
            a[0] += 1
            a[1 + BUCKETS.index(r["bucket"])] += 1
        facts = [[enc("date", d), enc("courier", c), enc("warehouse", w),
                  enc("account", ac), enc("slab", sl), enc(geo_name, g)] + a
                 for (d, c, w, ac, sl, g), a in agg.items()]
        return {"dims": dims, "facts": facts}

    def city_of(r):
        p = pin_of(r)
        return city_map.get(p) or (str(r["drop_city"]).strip() if r["drop_city"] else "Unknown")

    def hub_of(r):
        return hub_maps.get(r["courier_partner"], {}).get(pin_of(r), "Unmapped")

    cvol, hvol = defaultdict(int), defaultdict(int)
    for r in rows:
        cvol[city_of(r)] += 1
        hvol[hub_of(r)] += 1
    keep_city = set(sorted(cvol, key=lambda x: -cvol[x])[:TOP_CITY])
    keep_hub = set(sorted(hvol, key=lambda x: -hvol[x])[:TOP_HUB]) | {"Unmapped"}
    print("Cities: {:,} distinct -> keeping top {} ({:.1f}% of volume)".format(
        len(cvol), TOP_CITY, sum(cvol[c] for c in keep_city) / len(rows) * 100),
        file=sys.stderr)
    print("Hubs  : {:,} distinct -> keeping top {} ({:.1f}% of volume)".format(
        len(hvol), TOP_HUB, sum(hvol[h] for h in keep_hub if h in hvol) / len(rows) * 100),
        file=sys.stderr)
    unmapped = hvol.get("Unmapped", 0)

    by_city = build(city_of, "city", keep_city, "Other city")
    by_hub = build(hub_of, "hub", keep_hub, "Other hub")
    agg = {0: [len(rows)] + [sum(1 for r in rows if r["bucket"] == b) for b in BUCKETS]}
    agg = {k: v for k, v in agg.items()}

    total = len(rows)
    payload = {"window_days": WINDOW_DAYS, "generated_for_today": today.isoformat(),
               "buckets": BUCKETS, "shipments": total,
               "by_city": by_city, "by_hub": by_hub}

    out = os.path.join(BASE, "data", "rad_ofd_data.json")
    with open(out, "w") as f:
        json.dump(payload, f, separators=(",", ":"))

    print("  shipments : {:,}".format(total), file=sys.stderr)
    for b in BUCKETS:
        n = sum(1 for r in rows if r["bucket"] == b)
        print("    {:<14}{:>8,}  {:>5.1f}%".format(b, n, n / total * 100), file=sys.stderr)
    print("  no hub mapping: {:,} ({:.1f}%)".format(unmapped, unmapped / total * 100),
          file=sys.stderr)
    for nm, t in (("by_city", by_city), ("by_hub", by_hub)):
        print("  {:<8} {:,} fact rows | {}".format(nm, len(t["facts"]),
              ", ".join("%s=%d" % (k, len(v)) for k, v in t["dims"].items())),
              file=sys.stderr)
    print("Wrote {} ({:.0f} KB)".format(out, os.path.getsize(out) / 1024), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
