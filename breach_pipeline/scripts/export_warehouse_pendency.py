"""Snapshot payload for the three pendency tabs: packing, putback, RTO initiated.

Pendency is a live snapshot of OPEN delivery notes, not a date-range aggregate, so this
is regenerated each time the dashboard is rebuilt and stamped with an "as of" time.
The DB session clock runs UTC while the data is IST, so every clock comparison adds
330 minutes (see the db-server-clock-is-utc memory).
"""
import io, os, sys, json, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db.connection import get_erp_connection

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Anchored to the repo, not the working directory: the Cloud Run service runs the
# exporters as a subprocess and a cwd-relative path would land the payload wherever
# the caller happened to be.
OUT = os.path.join(ROOT, "data", "pendency_payload.json")

LIVE = ["MWH_Ggn_bhajgera_001", "MWH_Mum_Bhiwandi_002", "MWH_Hyd_Manikonda_001",
        "MWH_Blr_Neelmangla_002", "MWH_Pun_Khed_001", "MWH_Kol_Dankuni_001",
        "MWH_Chn_Kovur_001"]
LABEL = {"MWH_Ggn_bhajgera_001": "Delhi (Ggn)", "MWH_Mum_Bhiwandi_002": "Mumbai",
         "MWH_Hyd_Manikonda_001": "Hyderabad", "MWH_Blr_Neelmangla_002": "Bangalore",
         "MWH_Pun_Khed_001": "Pune", "MWH_Kol_Dankuni_001": "Kolkata",
         "MWH_Chn_Kovur_001": "Chennai"}
IDX = {w: i for i, w in enumerate(LIVE)}
PLACE = ",".join(["%s"] * len(LIVE))

# Packing pendency mirrors the team's Google Sheet ("Ideal Dispatch Date Pendency"):
#   COUNTUNIQUEIFS(delivery_note, dn_warehouse = X, Ideal Pickup Date = D,
#                  status_x IN ('To Be Picked','Under Picking'), custom_channel <> 'IWT')
# The sheet's Ideal Pickup Date = Order Date + TOTAL_CUTOFF + SBD. SBD is empty on 57,876
# of 57,877 EDD Dump rows (dead column), and TOTAL_CUTOFF is ClickPost's after-cutoff flag.
# That flag only exists once an AWB is generated and pending notes have none, so it is
# reproduced here from each warehouse's own cutoff hour, derived from cp_database_N:
# 90-99.8% agreement with the real flag, against 86-90% for the sheet's noon fallback.
PACKING_STATUS = ["To Be Picked", "Under Picking"]

# The cutoff now comes from promiseEngine.warehouse_cutoffs rather than being fitted.
# That table is keyed warehouse_id + delivery_type; the id space does not resolve to
# warehouse names for the mother warehouses (see the scm-packing-pendency-sheet memory),
# but the seven mother-warehouse ids all carry the SAME Standard A cutoff, so the
# standard flow is unambiguous. SDD/NDD rows differ (10:00-14:00) and cannot be applied
# without a per-order delivery type.
MWH_CUTOFF_IDS = [23, 123, 135, 138, 139, 146, 155]
DEFAULT_CUTOFF_HOUR = 14


def standard_cutoff_hour():
    """Read the Standard A cutoff from the promise engine; fall back if unreachable."""
    try:
        from db.connection import get_promise_connection
        pc = get_promise_connection(read_timeout=60).cursor()
        pc.execute("""SELECT DISTINCT HOUR(cutoff_time) h FROM promiseEngine.warehouse_cutoffs
                      WHERE is_active = 1 AND delivery_type = 'Standard A'
                        AND warehouse_id IN (%s)"""
                   % ",".join(["%s"] * len(MWH_CUTOFF_IDS)), tuple(MWH_CUTOFF_IDS))
        hours = sorted(int(r["h"]) for r in pc.fetchall())
        pc.close()
        if len(hours) == 1:
            print("cutoff from promiseEngine.warehouse_cutoffs: %02d:00 (Standard A, all 7 agree)"
                  % hours[0])
            return hours[0]
        print("WARNING: Standard A cutoffs disagree across the 7 ids %s - using %02d:00"
              % (hours, DEFAULT_CUTOFF_HOUR))
    except Exception as e:
        print("WARNING: promise engine unreachable (%s) - using %02d:00"
              % (str(e)[:70], DEFAULT_CUTOFF_HOUR))
    return DEFAULT_CUTOFF_HOUR


CUTOFF_HOUR_ALL = standard_cutoff_hour()

ec = get_erp_connection(read_timeout=280).cursor()
ec.execute("SELECT DATE(NOW() + INTERVAL 330 MINUTE) d, NOW() + INTERVAL 330 MINUTE ts")
row = ec.fetchone()
TODAY, AS_OF = str(row["d"]), str(row["ts"])[:16]

statuses, couriers = {}, {}


def ix(d, k):
    k = k or ""
    if k not in d:
        d[k] = len(d)
    return d[k]


def dn(name):
    return name or ""   # keep verbatim: names include DN-25-*, DRSH-DN-*


# ---- packing pendency ----
ec.execute("""
  SELECT custom_mother_warehouse wh, name, status,
         DATE(COALESCE(custom_shopify_order_date, creation)) order_date,
         HOUR(COALESCE(custom_shopify_order_date, creation)) order_hour,
         DATE(creation) created, DATE(modified) touched
  FROM `tabDelivery Note`
  WHERE custom_mother_warehouse IN (%s) AND status IN (%s)
    AND (custom_channel IS NULL OR custom_channel <> 'IWT')
    -- internal dark-stock replenishment, not a customer delivery. Today these are
    -- exactly the IWT rows (1,404 of them, zero left after the channel filter), so this
    -- is belt-and-braces in case one is ever raised on a different channel.
    AND (customer_name IS NULL OR customer_name NOT LIKE '%%%%Pets Centric%%%%')
""" % (PLACE, ",".join(["%s"] * len(PACKING_STATUS))), tuple(LIVE) + tuple(PACKING_STATUS))
packing = []
for r in ec.fetchall():
    od = r["order_date"]
    after_cutoff = 0 if (r["order_hour"] or 0) < CUTOFF_HOUR_ALL else 1
    ideal = (od + datetime.timedelta(days=after_cutoff)) if od else None
    packing.append([IDX[r["wh"]], dn(r["name"]), ix(statuses, r["status"]),
                    str(r["created"]), str(ideal) if ideal else None, str(r["touched"])])

# ---- putback pendency ----
ec.execute("""
  SELECT custom_mother_warehouse wh, name, DATE(creation) created, DATE(modified) touched
  FROM `tabDelivery Note`
  WHERE custom_mother_warehouse IN (%s) AND status = 'Put Back Initiated'
""" % PLACE, tuple(LIVE))
putback = [[IDX[r["wh"]], dn(r["name"]), str(r["created"]), str(r["touched"])]
           for r in ec.fetchall()]

# ---- RTO initiated pendency ----
ec.execute("""
  SELECT custom_mother_warehouse wh, name, DATE(creation) created, DATE(modified) touched,
         custom_awb_number awb, custom_assigned_delivery_partner courier, return_against
  FROM `tabDelivery Note`
  WHERE custom_mother_warehouse IN (%s) AND status = 'Return Initiated'
    AND custom_dn_type = 'RTO Return'
""" % PLACE, tuple(LIVE))
rto = [[IDX[r["wh"]], dn(r["name"]), str(r["created"]), str(r["touched"]),
        r["awb"] or "", ix(couriers, r["courier"]), dn(r["return_against"])]
       for r in ec.fetchall()]

# RVP sits in the same status and is deliberately NOT in the RTO tab; counted for the note
ec.execute("""SELECT COUNT(*) n FROM `tabDelivery Note`
  WHERE custom_mother_warehouse IN (%s) AND status='Return Initiated'
    AND custom_dn_type='RVP Return'""" % PLACE, tuple(LIVE))
rvp_open = int(ec.fetchone()["n"])


def keys(d):
    return [k for k, _ in sorted(d.items(), key=lambda x: x[1])]


payload = {
    "asOf": AS_OF, "today": TODAY,
    "wh": [LABEL[w] for w in LIVE], "whCode": LIVE,
    "status": keys(statuses), "courier": keys(couriers),
    "packingStatuses": PACKING_STATUS,
    "rvpOpen": rvp_open,
    "packing": packing,   # [wh, dn, status, created, ideal|null, touched]
    "putback": putback,   # [wh, dn, created, touched]
    "rto": rto,           # [wh, dn, created, touched, awb, courier, returnAgainst]
}

os.makedirs(os.path.dirname(OUT), exist_ok=True)
io.open(OUT, "w", encoding="utf-8").write(json.dumps(payload, separators=(",", ":"),
                                                     ensure_ascii=True))
print("as of %s | packing=%d putback=%d rto=%d (rvp open, excluded=%d) | %.0f KB" % (
    AS_OF, len(packing), len(putback), len(rto), rvp_open,
    os.path.getsize(OUT) / 1024))
