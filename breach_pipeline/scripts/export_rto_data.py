"""RTO aggregate for the dashboard's RTO tab.

EVENT-DATE BASIS (user rule, 30 Aug 2026):

    RTO % =        RTOs marked in the period
             ------------------------------------------
             RTOs marked in period + delivered in period

Every shipment is counted on the date it reached a terminal outcome - the RTO
mark date for shipments that came back, the delivery date for those that did
not. Shipments still in flight, cancelled, lost or damaged belong to neither
side of that ratio and are excluded entirely.

This replaced a cohort basis (count on created_at, RTO as a flag). The cohort
form answered "of what we shipped that day, how much came back", which needed no
delivery data but left recent days permanently understated - a shipment created
yesterday has not had time to become an RTO. The event basis has no such
immaturity because both sides of the ratio are settled outcomes.

The pull covers any shipment whose RTO mark OR delivery falls in the window,
regardless of when it was created, so both sides are complete at the edges.

Window is shorter than the breach tab's (60 days by default) purely to keep the
payload small; the page already carries three other datasets.
"""
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pandas as pd
from db.connection import get_scm_connection, get_erp_connection

WINDOW_DAYS = int(os.environ.get("RTO_DAYS", "100"))
TOP_DEST = int(os.environ.get("RTO_TOP_DEST", "80"))
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


SQL = """
SELECT
    CASE WHEN clickpost_unified_status LIKE 'RTO%%' THEN DATE(rto_mark_date)
         ELSE DATE(delivery_date) END                  AS d,
    courier_partner, shipment_weight, awb, account_code, order_id,
    drop_pincode, drop_city, payment_mode, invoice_value,
    DATE(rto_mark_date)                                AS marked_on,
    reason_for_last_failed_delivery                    AS rto_reason,
    out_for_delivery_attempts                          AS attempts,
    clickpost_unified_status                           AS status,
    (clickpost_unified_status LIKE 'RTO%%')            AS is_rto,
    DATEDIFF(rto_mark_date, created_at)                AS mark_lag,
    from_warehouse                                     AS wh,
    DATE(rto_delivery_date)                            AS received_on,
    -- which dataset each row belongs to, decided in SQL so Python never has to
    -- re-derive it and the two can never drift apart
    (clickpost_unified_status LIKE 'RTO%%'
     AND rto_mark_date >= DATE_SUB({today}, INTERVAL {days} DAY)
     AND rto_mark_date <  DATE_ADD({today}, INTERVAL 1 DAY))
    OR (clickpost_unified_status = 'Delivered'
        AND delivery_date >= DATE_SUB({today}, INTERVAL {days} DAY)
        AND delivery_date <  DATE_ADD({today}, INTERVAL 1 DAY))  AS in_main,
    (clickpost_unified_status = 'RTO-Delivered'
     AND rto_delivery_date >= DATE_SUB({today}, INTERVAL {days} DAY)
     AND rto_delivery_date <  DATE_ADD({today}, INTERVAL 1 DAY))  AS in_returns
FROM cp_database_N
WHERE courier_partner <> 'Delhivery Reverse'
  AND ((clickpost_unified_status LIKE 'RTO%%'
        AND rto_mark_date >= DATE_SUB({today}, INTERVAL {days} DAY)
        AND rto_mark_date <  DATE_ADD({today}, INTERVAL 1 DAY))
    OR (clickpost_unified_status = 'Delivered'
        AND delivery_date  >= DATE_SUB({today}, INTERVAL {days} DAY)
        AND delivery_date  <  DATE_ADD({today}, INTERVAL 1 DAY))
    OR (clickpost_unified_status = 'RTO-Delivered'
        AND rto_delivery_date >= DATE_SUB({today}, INTERVAL {days} DAY)
        AND rto_delivery_date <  DATE_ADD({today}, INTERVAL 1 DAY)))
""".format(today=IST_TODAY, days=WINDOW_DAYS)


def fetch_net_values(days):
    """AWB -> pre-tax value, from the ERP.

    `cp_database_N.invoice_value` is the tax-inclusive figure. The ERP holds the
    split, so net comes from `tabDelivery Note.net_total` reached through
    `custom_awb_number` - the same AWB<->DN link the complaints export uses in
    the opposite direction.

    Pulled as ONE bulk query over a date range rather than batched by AWB list:
    ~500k two-column rows stream back in seconds, where 380 round trips would add
    minutes to the build. The range is padded well past the window because a
    shipment delivered today may have been despatched weeks ago.

    Cancelled documents (docstatus 2) are excluded. Where one AWB carries several
    delivery notes the values are summed, matching how ClickPost reports one
    invoice_value for the shipment.
    """
    sql = ("SELECT custom_awb_number AS awb, SUM(net_total) AS net "
           "FROM `tabDelivery Note` "
           "WHERE posting_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY) "
           "  AND custom_awb_number IS NOT NULL AND custom_awb_number <> '' "
           "  AND docstatus < 2 AND net_total > 0 "
           "GROUP BY custom_awb_number")
    out = {}
    erp = get_erp_connection(read_timeout=900)
    try:
        with erp.cursor() as cur:
            cur.execute(sql, (days + 45,))
            for r in cur.fetchall():
                out[str(r["awb"]).strip()] = float(r["net"] or 0)
    finally:
        erp.close()
    print("Net values fetched for {:,} AWBs".format(len(out)), file=sys.stderr)
    return out


def air_surface(courier, awb, account_code):
    """Air vs surface, per the AIR recon rule: Bluedart AWBs starting 8/9 are air and
    7 is surface; for everyone else the account code carries the mode. Couriers with
    no air product at all fall through to Not applicable."""
    ac = (account_code or "").lower()
    if "air" in ac:
        return "Air"
    a = str(awb or "").strip()
    if (courier or "") == "Bluedart" and a[:1] in ("8", "9"):
        return "Air"
    if (courier or "") == "Bluedart" and a[:1] == "7":
        return "Surface"
    if "surface" in ac or "sfc" in ac:
        return "Surface"
    return "Not applicable"


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
    print("Pulled {:,} terminal shipments (RTO-marked or delivered)".format(len(rows)),
          file=sys.stderr)
    if not rows:
        print("Zero rows - refusing to write an empty RTO payload", file=sys.stderr)
        return 1

    ret_rows = [r for r in rows if r["in_returns"]]
    rows = [r for r in rows if r["in_main"]]
    print("Returns received (RTO-Delivered): {:,}".format(len(ret_rows)), file=sys.stderr)

    net_by_awb = fetch_net_values(WINDOW_DAYS)
    matched = sum(1 for r in rows if str(r["awb"] or "").strip() in net_by_awb)
    print("  net value matched : {:,} of {:,} ({:.1f}%)".format(
        matched, len(rows), matched / len(rows) * 100), file=sys.stderr)

    # cap destination cardinality, same trick as the breach export
    vol = defaultdict(int)
    for r in rows:
        vol[resolve_city(r["drop_pincode"], r["drop_city"])] += 1
    keep = set(sorted(vol, key=lambda c: -vol[c])[:TOP_DEST])
    print("Destinations: {:,} distinct -> keeping top {} ({:.1f}% of volume)".format(
        len(vol), len(keep), sum(vol[c] for c in keep) / len(rows) * 100), file=sys.stderr)

    # 0 terminal (rto + delivered - the denominator), 1 rto, 2 rto_delivered,
    # 3 lag_sum, 4 lag_n, 5 terminal_value, 6 rto_value,
    # 7 terminal_net_value, 8 rto_net_value  (pre-tax, from the ERP)
    agg = defaultdict(lambda: [0, 0, 0, 0, 0, 0.0, 0.0, 0.0, 0.0])
    for r in rows:
        w = r["shipment_weight"]
        slab = "unknown" if w is None else ("0-3kg" if float(w) <= 3000 else "3+kg")
        dest = resolve_city(r["drop_pincode"], r["drop_city"])
        if dest not in keep:
            dest = "Other"
        st = r["status"] or "Unknown"
        a = r["attempts"]
        att = "unknown" if a is None else ("0" if a == 0 else ("1" if a == 1 else ("2" if a == 2 else "3+")))
        key = (r["d"].isoformat(), r["courier_partner"] or "Unknown",
               slab, dest, st if r["is_rto"] else "",
               (r["payment_mode"] or "Unknown").strip().upper(),
               att, air_surface(r["courier_partner"], r["awb"], r["account_code"]))
        a = agg[key]
        a[0] += 1
        val = float(r["invoice_value"] or 0)
        net = net_by_awb.get(str(r["awb"] or "").strip(), 0.0)
        a[5] += val
        a[7] += net
        if r["is_rto"]:
            a[6] += val
            a[8] += net
            a[1] += 1
            if st == "RTO-Delivered":
                a[2] += 1
            if r["mark_lag"] is not None and r["mark_lag"] >= 0:
                a[3] += int(r["mark_lag"])
                a[4] += 1

    dims = {"date": [], "courier": [], "slab": [], "dest": [], "rto_status": [],
            "payment": [], "attempts": [], "mode": []}
    idx = {k: {} for k in dims}

    def enc(f, v):
        m = idx[f]
        if v not in m:
            m[v] = len(dims[f])
            dims[f].append(v)
        return m[v]

    facts = []
    for (d, c, sl, de, st, pay, att, md), a in agg.items():
        facts.append([enc("date", d), enc("courier", c), enc("slab", sl),
                      enc("dest", de), enc("rto_status", st), enc("payment", pay),
                      enc("attempts", att), enc("mode", md)]
                     # whole rupees: paisa precision on a 6-figure aggregate is
                     # noise, and the decimals cost ~1 MB across ~100k fact rows
                     + a[:5] + [round(a[5]), round(a[6]),
                                round(a[7]), round(a[8])])

    ship = sum(a[0] for a in agg.values())
    rto = sum(a[1] for a in agg.values())
    ship_val = sum(a[5] for a in agg.values())
    rto_val = sum(a[6] for a in agg.values())
    ship_net = sum(a[7] for a in agg.values())
    rto_net = sum(a[8] for a in agg.values())
    # ---- returns actually received back at the warehouse ------------------
    # Deliberately a SEPARATE dataset rather than another dimension on `facts`.
    # The main table counts a shipment on the date it reached a terminal outcome
    # - for an RTO that is the MARK date, the day the return was decided. A
    # warehouse reconciling stock needs the day the parcel physically arrived,
    # which is rto_delivery_date and is typically several days later. Mixing two
    # date meanings in one fact table would make every existing view ambiguous.
    ret_dims = {"date": [], "wh": [], "courier": []}
    ret_idx = {k: {} for k in ret_dims}

    def renc(f, v):
        m = ret_idx[f]
        if v not in m:
            m[v] = len(ret_dims[f])
            ret_dims[f].append(v)
        return m[v]

    ret_agg = defaultdict(lambda: [0, 0.0, 0.0])   # count, gross, net
    for r in ret_rows:
        key = (r["received_on"].isoformat(), r["wh"] or "Unknown",
               r["courier_partner"] or "Unknown")
        a = ret_agg[key]
        a[0] += 1
        a[1] += float(r["invoice_value"] or 0)
        a[2] += float(net_by_awb.get(str(r["awb"] or "").strip(), 0) or 0)

    ret_facts = [[renc("date", d), renc("wh", w), renc("courier", c),
                  a[0], round(a[1]), round(a[2])]
                 for (d, w, c), a in ret_agg.items()]
    ret_n = sum(a[0] for a in ret_agg.values())
    print("  returns fact rows : {:,} ({:,} shipments, {:,} warehouses)".format(
        len(ret_facts), ret_n, len(ret_dims["wh"])), file=sys.stderr)

    payload = {"window_days": WINDOW_DAYS, "generated_for_today": today.isoformat(),
               "shipments": ship, "rto": rto,
               "shipped_value": round(ship_val, 2), "rto_value": round(rto_val, 2),
               "shipped_net": round(ship_net, 2), "rto_net": round(rto_net, 2),
               "dims": dims, "facts": facts,
               "returns": {"dims": ret_dims, "facts": ret_facts, "shipments": ret_n}}

    # AWB-level receipts, for the warehouse Google Sheet. Written from the rows
    # already in hand rather than re-queried: a second pass would mean another
    # full scan of cp_database_N, which is exactly what blew the build timeout.
    receipts = [{"awb": str(r["awb"] or "").strip(),
                 "received_on": r["received_on"].isoformat() if r["received_on"] else "",
                 "marked_on": r["marked_on"].isoformat() if r["marked_on"] else "",
                 "days_to_receipt": (r["received_on"] - r["marked_on"]).days
                                    if r["received_on"] and r["marked_on"] else "",
                 "courier": r["courier_partner"] or "",
                 "warehouse": r["wh"] or "Unknown",
                 "order_id": r["order_id"] or "",
                 "drop_city": r["drop_city"] or "",
                 "drop_pincode": r["drop_pincode"] or "",
                 "value_incl_tax": round(float(r["invoice_value"] or 0)),
                 "net_value": round(net_by_awb.get(str(r["awb"] or "").strip(), 0) or 0),
                 "attempts": r["attempts"] if r["attempts"] is not None else "",
                 "rto_reason": (r["rto_reason"] or "")[:120]}
                for r in ret_rows]
    rec_out = os.path.join(os.path.dirname(__file__), "..", "data", "rto_receipts.json")
    with open(rec_out, "w") as f:
        json.dump({"generated_for_today": today.isoformat(), "window_days": WINDOW_DAYS,
                   "rows": receipts}, f, separators=(",", ":"))
    print("  receipts dumped   : {:,} -> {}".format(
        len(receipts), os.path.basename(rec_out)), file=sys.stderr)

    out = os.path.join(os.path.dirname(__file__), "..", "data", "rto_data.json")
    with open(out, "w") as f:
        json.dump(payload, f, separators=(",", ":"))

    print("  shipments : {:,}".format(ship), file=sys.stderr)
    print("  terminal  : {:,}  (rto + delivered)".format(ship), file=sys.stderr)
    print("  rto       : {:,}  ({:.2f}%)".format(rto, rto / ship * 100), file=sys.stderr)
    print("  ship value: {:,.0f}".format(ship_val), file=sys.stderr)
    print("  rto value : {:,.0f}  ({:.2f}% of shipped value)".format(
        rto_val, rto_val / ship_val * 100 if ship_val else 0), file=sys.stderr)
    print("  rto net   : {:,.0f}  (gross {:,.0f}, tax {:.1f}%)".format(
        rto_net, rto_val, (rto_val / rto_net - 1) * 100 if rto_net else 0), file=sys.stderr)
    print("  fact rows : {:,}".format(len(facts)), file=sys.stderr)
    print("Wrote {} ({:.0f} KB)".format(out, os.path.getsize(out) / 1024), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
