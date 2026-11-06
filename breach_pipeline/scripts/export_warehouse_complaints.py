"""Complaints payload: wrong item / missing item / damaged / expired, by mother warehouse.

Source CX_data.CDC_Nugget. NOTE the table was rebuilt around 2026-08-14 (there is a
CDC_Nugget_thin_bak_20260814 alongside) and the timestamp column is now `created_at`,
not `created_time`.

Warehouse attribution is the two-hop rule: awb_number -> cp_database_N.awb -> from_warehouse;
when awb_number actually holds a DN reference, DN -> tabDelivery Note.custom_mother_warehouse.
No PII is read: subject, description, phone_number and customer_email are never selected.
"""
import io, os, re, sys, json, datetime
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db.connection import get_scm_connection, get_erp_connection

_today = datetime.date.today()
START = os.environ.get("WH_START", str(_today - datetime.timedelta(days=112)))
END = os.environ.get("WH_END", str(_today + datetime.timedelta(days=1)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Anchored to the repo, not the working directory: the Cloud Run service runs the
# exporters as a subprocess and a cwd-relative path would land the payload wherever
# the caller happened to be.
OUT = os.path.join(ROOT, "data", "complaints_payload.json")

LIVE = ["MWH_Ggn_bhajgera_001", "MWH_Mum_Bhiwandi_002", "MWH_Hyd_Manikonda_001",
        "MWH_Blr_Neelmangla_002", "MWH_Pun_Khed_001", "MWH_Kol_Dankuni_001",
        "MWH_Chn_Kovur_001"]
LABEL = {"MWH_Ggn_bhajgera_001": "Delhi (Ggn)", "MWH_Mum_Bhiwandi_002": "Mumbai",
         "MWH_Hyd_Manikonda_001": "Hyderabad", "MWH_Blr_Neelmangla_002": "Bangalore",
         "MWH_Pun_Khed_001": "Pune", "MWH_Kol_Dankuni_001": "Kolkata",
         "MWH_Chn_Kovur_001": "Chennai"}
IDX = {w: i for i, w in enumerate(LIVE)}

# Four buckets, keyed on `issue`. Customer-fault and refuted rows are excluded so the
# view only carries complaints a warehouse could actually have caused.
BUCKETS = {
    "wrong":   ["Received wrong product", "Wrong Item Sent"],
    "missing": ["Missing item", "Missing Items"],
    "damaged": ["Received damaged product", "Received Damaged Product"],
    "expired": ["Expired Products Delivered", "Received near to expiry product"],
}
BUCKET_LABEL = {"wrong": "Wrong item sent", "missing": "Missing item",
                "damaged": "Damaged item delivered", "expired": "Expired product sent"}
# 'Customer placed wrong item' is a separate issue and is simply never matched above.
EXCLUDE_SUB = {"Cx Placed Wrong Item",                     # customer ordered the wrong thing
               "Product Found with Cx",                    # investigation refuted the claim
               "Product Found in Video Cx not agreed"}

# Agents type the reference by hand, so the cell is not always one clean id: it can be a
# comma-separated list, carry a stray leading slash or dash, or be a placeholder. Splitting
# and trimming recovers ~84 tickets that the verbatim lookup dropped; the placeholders are
# genuinely untraceable and are counted separately rather than silently binned.
JUNK_REF = {"", "-", "0", "na", "n/a", "nil", "none", "null", "nan", "."}


def parse_refs(raw):
    """Every candidate AWB / DN reference inside one awb_number cell."""
    s = (raw or "").strip()
    if s.lower() in JUNK_REF:
        return []
    out = []
    for part in re.split(r"[,;/\s]+", s):
        part = part.strip().strip("-").strip()
        if part and part.lower() not in JUNK_REF:
            out.append(part)
    return out


# Only used on the ERP fallback path, for the handful of delivery notes with no ClickPost
# row at all. Everywhere else ClickPost's own label is taken verbatim. The ERP says "Pidge"
# and (inconsistently) "Elastic Run" where ClickPost says "Pidge HLD" and "ElasticRun", and
# carries service-level names like "Blue Dart Surface 2KG" against ClickPost's "Bluedart".
COURIER_ALIAS = {
    "elasticrun": "ElasticRun", "bluedart": "Bluedart", "delhivery": "Delhivery",
    "pidge": "Pidge HLD", "pidgehld": "Pidge HLD", "shadowfax": "Shadowfax",
    "shipsy": "Shipsy", "shiprocket": "Shiprocket", "xpressbees": "Xpressbees",
    "dtdc": "DTDC", "inamo": "Inamo", "ekartelitelogistics": "Ekart Elite Logistics",
    "urbanebolt": "Urbane Bolt",
}


def canon_courier(name):
    s = (name or "").strip()
    if not s:
        return "Unknown"
    base = re.sub(r"\s*(surface|express|air)\b.*$", "", s, flags=re.I)
    return COURIER_ALIAS.get(re.sub(r"[^a-z0-9]", "", base.lower()), s)


ISSUE_TO_BUCKET = {}
for b, issues in BUCKETS.items():
    for i in issues:
        ISSUE_TO_BUCKET[i.lower()] = b
ALL_ISSUES = [i for v in BUCKETS.values() for i in v]

sc = get_scm_connection(read_timeout=280).cursor()
sc.execute("""
  SELECT ticket_id, DATE(created_at) d, issue, sub_issue, awb_number, order_id, status
  FROM CX_data.CDC_Nugget
  WHERE created_at >= %%s AND created_at < %%s AND issue IN (%s)
    AND (is_spam IS NULL OR is_spam = 0)
""" % ",".join(["%s"] * len(ALL_ISSUES)), tuple([START, END] + ALL_ISSUES))
tickets = [dict(r) for r in sc.fetchall()]
print("complaints in window: %d" % len(tickets))

kept, refuted = [], 0
for t in tickets:
    if (t["sub_issue"] or "").strip() in EXCLUDE_SUB:
        refuted += 1
        continue
    t["bucket"] = ISSUE_TO_BUCKET[(t["issue"] or "").lower()]
    kept.append(t)
print("excluded as customer-fault or refuted: %d" % refuted)

for t in kept:
    t["refs"] = parse_refs(t["awb_number"])
refs = sorted({r for t in kept for r in t["refs"]})
# "DN-" anywhere, not just as a prefix: 45 references arrive as DRSH-DN-2026-nnnnn and a
# prefix test routed them to ClickPost, where they never matched.
dn_like = [r for r in refs if "DN-" in r.upper()]
awb_like = [r for r in refs if "DN-" not in r.upper()]
wh_of = {}


def chunks(seq, n=800):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


courier_of, dn_of = {}, {}


def dn_from_invoice(inv):
    """ClickPost carries the delivery note in invoice_number, prefixed: PRD-DN-26-1203570.

    That is the only cheap route from an AWB back to a DN - tabDelivery Note.custom_awb_number
    has no index, and probing it took 249s per 800 AWBs against 0.6s here. Spot-checked on
    400 shipments: every derived name existed in the ERP and its mother warehouse agreed.
    """
    s = (inv or "").strip()
    return re.sub(r"^PRD-", "", s, flags=re.I)


# Hop 1 - references that are already an AWB resolve straight against ClickPost, which is
# the source of truth for both the dispatching warehouse and the courier. Its labels are
# taken verbatim: they are the vocabulary the shipment denominator counts in.
for c in chunks(awb_like):
    sc.execute("SELECT awb, from_warehouse, courier_partner, invoice_number "
               "FROM cp_database_N WHERE awb IN (%s)"
               % ",".join(["%s"] * len(c)), tuple(c))
    for r in sc.fetchall():
        if r["from_warehouse"]:
            wh_of[r["awb"]] = r["from_warehouse"]
        if r["courier_partner"]:
            courier_of[r["awb"]] = r["courier_partner"]
        if dn_from_invoice(r["invoice_number"]):
            dn_of[r["awb"]] = dn_from_invoice(r["invoice_number"])

# Hop 2 - a DN reference goes through the ERP only to pick up custom_awb_number, the bridge
# back into ClickPost. The ERP's own warehouse and courier are kept purely as a fallback.
ec = get_erp_connection(read_timeout=280).cursor()
dn_awb, erp_wh, erp_cp = {}, {}, {}
for c in chunks(dn_like):
    ec.execute("SELECT name, custom_mother_warehouse w, custom_assigned_delivery_partner cp, "
               "custom_awb_number awb FROM `tabDelivery Note` WHERE name IN (%s)"
               % ",".join(["%s"] * len(c)), tuple(c))
    for r in ec.fetchall():
        if (r["awb"] or "").strip():
            dn_awb[r["name"]] = r["awb"].strip()
        if r["w"]:
            erp_wh[r["name"]] = r["w"]
        if r["cp"]:
            erp_cp[r["name"]] = r["cp"]

# Hop 3 - resolve the bridged AWBs in ClickPost and let them win. This matters for the
# courier: on a darkstore shipment the ERP says "ElasticRun" where ClickPost says
# "Elastic Run HLD", a courier that only ever runs darkstore volume. Trusting the ERP label
# there would file darkstore couriers under mother-warehouse names. The two sources agree on
# the warehouse itself in every one of the 2,576 shipments checked, so only the courier moves.
cp_bridge = {}
for c in chunks(sorted(set(dn_awb.values()))):
    sc.execute("SELECT awb, from_warehouse, courier_partner, invoice_number "
               "FROM cp_database_N WHERE awb IN (%s)"
               % ",".join(["%s"] * len(c)), tuple(c))
    for r in sc.fetchall():
        cp_bridge[r["awb"]] = r

fallback = 0
for dn in dn_like:
    src = cp_bridge.get(dn_awb.get(dn, ""))
    if src and src["from_warehouse"]:
        wh_of[dn] = src["from_warehouse"]
        if src["courier_partner"]:
            courier_of[dn] = src["courier_partner"]
        continue
    if dn in erp_wh:                       # no ClickPost row - ERP is all we have
        fallback += 1
        wh_of[dn] = erp_wh[dn]
        if dn in erp_cp:
            courier_of[dn] = canon_courier(erp_cp[dn])
print("refs resolved: %d of %d (%d delivery notes fell back to the ERP)"
      % (sum(1 for r in refs if r in wh_of), len(refs), fallback))

dates, couriers, rows = {}, {}, []
unattributed = defaultdict(int)


def ix(d, k):
    if k not in d:
        d[k] = len(d)
    return d[k]


outside = defaultdict(int)     # resolved, but fulfilled by a darkstore - not our scope
untraced = defaultdict(int)    # placeholder or unknown reference - a genuine blind spot
for t in kept:
    hit = next(((r, wh_of[r]) for r in t["refs"]
                if wh_of.get(r) in IDX), None)
    if hit is None:
        unattributed[t["bucket"]] += 1
        if any(r in wh_of for r in t["refs"]):
            outside[t["bucket"]] += 1
        else:
            untraced[t["bucket"]] += 1
        continue
    ref, w = hit
    dn = ref if "DN-" in ref.upper() else dn_of.get(ref, "")
    rows.append([IDX[w], ix(dates, str(t["d"])), t["bucket"],
                 (t["sub_issue"] or "").strip(), ref,
                 ix(couriers, courier_of.get(ref) or "Unknown"),
                 str(t["ticket_id"] or ""), dn])

# Denominator: shipments (one row per AWB in ClickPost) ordered from each warehouse.
# Kept per day so the rate moves with the date filter instead of being a fixed total.
sc.execute("""SELECT from_warehouse wh, DATE(order_date) d, courier_partner cp, COUNT(*) c
   FROM cp_database_N
   WHERE order_date >= %s AND order_date < %s AND from_warehouse LIKE 'MWH%%'
   GROUP BY 1, 2, 3""", (START, END))
ship_daily, shipments = [], {}
for r in sc.fetchall():
    if r["wh"] not in IDX:
        continue
    i, n = IDX[r["wh"]], int(r["c"])
    ship_daily.append([i, str(r["d"]), ix(couriers, r["cp"] or "Unknown"), n])
    shipments[i] = shipments.get(i, 0) + n

payload = {
    "window": {"start": START, "end": END},
    "wh": [LABEL[w] for w in LIVE], "whCode": LIVE,
    "buckets": list(BUCKETS), "bucketLabel": BUCKET_LABEL,
    "date": [k for k, _ in sorted(dates.items(), key=lambda x: x[1])],
    "shipments": [shipments.get(i, 0) for i in range(len(LIVE))],
    "courier": [k for k, _ in sorted(couriers.items(), key=lambda x: x[1])],
    "shipDaily": ship_daily,     # [whIdx, "YYYY-MM-DD", courierIdx, count]
    "unattributed": dict(unattributed),
    "darkstore": dict(outside), "untraced": dict(untraced),
    "refuted": refuted,
    "rows": rows,   # [wh, dateIdx, bucket, sub_issue, ref, courierIdx, ticketId, dn]
}
os.makedirs(os.path.dirname(OUT), exist_ok=True)
io.open(OUT, "w", encoding="utf-8").write(json.dumps(payload, separators=(",", ":"),
                                                     ensure_ascii=True))
print("attributed to a mother warehouse: %d | darkstore: %d | untraceable: %d | %.0f KB" % (
    len(rows), sum(outside.values()), sum(untraced.values()),
    os.path.getsize(OUT) / 1024))
