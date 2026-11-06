"""Build the dictionary-encoded payload for the Not Found dashboard."""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db.connection import get_erp_connection

import datetime
# default the window to the last ~16 weeks up to and including today, so a refresh
# never needs the dates edited by hand
_today = datetime.date.today()
START = os.environ.get("WH_START", str(_today - datetime.timedelta(days=112)))
END = os.environ.get("WH_END", str(_today + datetime.timedelta(days=1)))  # exclusive
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Anchored to the repo, not the working directory: the Cloud Run service runs the
# exporters as a subprocess and a cwd-relative path would land the payload wherever
# the caller happened to be.
OUT = os.path.join(ROOT, "data", "notfound_payload.json")

LABEL = {"MWH_Ggn_bhajgera_001": "Delhi (Ggn)", "MWH_Blr_Neelmangla_002": "Bangalore",
         "MWH_Mum_Bhiwandi_002": "Mumbai", "MWH_Pun_Khed_001": "Pune",
         "MWH_Kol_Dankuni_001": "Kolkata", "MWH_Chn_Kovur_001": "Chennai",
         "MWH_Hyd_Manikonda_001": "Hyderabad"}
ORDER = ["MWH_Ggn_bhajgera_001", "MWH_Mum_Bhiwandi_002", "MWH_Hyd_Manikonda_001",
         "MWH_Blr_Neelmangla_002", "MWH_Pun_Khed_001", "MWH_Kol_Dankuni_001",
         "MWH_Chn_Kovur_001"]

DETAIL_SQL = """
SELECT w.parent_warehouse wh, DATE(sed.creation) d, se.custom_delivery_note dn,
       sed.s_warehouse bin, sed.item_code item, sed.item_name item_name,
       sed.qty qty, sed.custom_available_qty avail, dn.status st
FROM `tabStock Entry Detail` sed
JOIN `tabStock Entry` se ON se.name = sed.parent
JOIN `tabWarehouse` w ON w.name = sed.s_warehouse
LEFT JOIN `tabDelivery Note` dn ON dn.name = se.custom_delivery_note
WHERE sed.creation >= %s AND sed.creation < %s AND sed.custom_missing = 1
  AND se.stock_entry_type = 'Material Transfer'
  -- submitted only: docstatus 2 is a CANCELLED entry, i.e. the transfer to suspense was
  -- reversed (usually the item turned up). 161 such lines in a 112-day window were being
  -- counted (5.4 pct of Bangalore's total, 2.3 pct of Mumbai's). Only 2 of 153 were re-done.
  -- (No bare percent signs in this string: pymysql treats them as placeholders.)
  AND se.docstatus = 1
  AND sed.t_warehouse LIKE 'Suspense-MWH%%'
  AND w.parent_warehouse LIKE 'MWH%%'
  AND (dn.customer_name IS NULL OR dn.customer_name NOT LIKE '%%Pets Centric%%')
  AND (dn.custom_channel IS NULL OR dn.custom_channel <> 'IWT')
ORDER BY sed.creation
"""

# Denominator: forward DNs per warehouse per day. Pets Centric / IWT delivery notes are
# internal dark-stock transfers, not customer deliveries - 8,509 of them in the window. They
# were inflating the denominator (and 461 of them the numerator), and the packing pendency
# tab already excludes them, so both sides now drop them and the rate compares like with like.
BASE_SQL = """
SELECT custom_mother_warehouse wh, DATE(creation) d, COUNT(*) dns,
       SUM(status='Cancel') cancels
FROM `tabDelivery Note`
WHERE creation >= %s AND creation < %s AND is_return = 0
  AND custom_mother_warehouse LIKE 'MWH%%'
  AND (customer_name IS NULL OR customer_name NOT LIKE '%%Pets Centric%%')
  AND (custom_channel IS NULL OR custom_channel <> 'IWT')
GROUP BY 1, 2
"""

ec = get_erp_connection(read_timeout=280).cursor()
ec.execute(DETAIL_SQL, (START, END))
rows = ec.fetchall()
ec.execute(BASE_SQL, (START, END))
base_rows = ec.fetchall()

wh_idx = {w: i for i, w in enumerate(ORDER)}
bins, items, item_names, dates, stats = {}, {}, [], {}, {}


def ix(d, k):
    if k not in d:
        d[k] = len(d)
    return d[k]


facts = []
for r in rows:
    w = r["wh"]
    if w not in wh_idx:
        continue
    ic = r["item"] or ""
    i = ix(items, ic)
    if i == len(item_names):
        item_names.append((r["item_name"] or "")[:60])
    facts.append([
        wh_idx[w],
        ix(dates, str(r["d"])),
        r["dn"] or "",
        ix(bins, r["bin"] or ""),
        i,
        int(r["qty"] or 0),
        int(r["avail"] or 0),
        ix(stats, r["st"] or "(none)"),
    ])

base = []
for r in base_rows:
    if r["wh"] in wh_idx:
        base.append([wh_idx[r["wh"]], ix(dates, str(r["d"])), int(r["dns"]),
                     int(r["cancels"] or 0)])


def keys(d):
    return [k for k, _ in sorted(d.items(), key=lambda x: x[1])]


# "Is the bin-determination process in use?" is read off the data, never hardcoded.
# It was a fixed [1,1,1,1,0,0,0] from the 20 Aug build, when Pune, Kolkata and Chennai had
# no suspense moves at all. Kolkata's suspense bin was created on 25 Aug and it started
# marking not-found on 6 Sep, but the stale flag kept hiding its rows behind "process not
# in use" until a user found a real Kolkata DN (DN-26-1796672). Rule: in use = at least one
# not-found line in the last ACTIVE_DAYS days. A recent window rather than the whole payload
# window, so a single months-old line (Chennai, 3 Jul) does not turn a warehouse that has
# stopped into one that reads as "0% - best".
ACTIVE_DAYS = int(os.environ.get("WH_ACTIVE_DAYS", "30"))
_recent = str(_today - datetime.timedelta(days=ACTIVE_DAYS))
_live = {r["wh"] for r in rows if str(r["d"]) >= _recent}
ACTIVE = [1 if w in _live else 0 for w in ORDER]


payload = {
    "window": {"start": START, "end": END},
    # IST build time. Was hardcoded to the first build date (2026-08-20), so the page header
    # claimed every rebuild since was two months old. The container clock is UTC.
    "generated": (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=330)).strftime("%Y-%m-%d %H:%M IST"),
    "dims": {
        "wh": [LABEL[w] for w in ORDER],
        "whCode": ORDER,
        "active": ACTIVE,          # 1 = any not-found line in the last ACTIVE_DAYS days
        "activeDays": ACTIVE_DAYS,
        "date": keys(dates),
        "bin": keys(bins),
        "item": keys(items),
        "itemName": item_names,
        "status": keys(stats),
    },
    # [wh, date, dn, bin, item, qty, avail, status]
    "facts": facts,
    # [wh, date, dns, cancels]
    "base": base,
}

os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(payload, f, separators=(",", ":"))
print("wrote %s  facts=%d base=%d dates=%d bins=%d items=%d  size=%.0f KB" % (
    OUT, len(facts), len(base), len(dates), len(bins), len(items),
    os.path.getsize(OUT) / 1024))
