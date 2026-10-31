"""Control Tower (Warehouse Dashboards in the sidebar): the "Control Tower" Google Sheet's Dashboard and
Warehouse Pendency tabs, built straight from the databases instead of the sheet's ERP, CP and EDD dumps.

Where the rows come from: the sheet's three dumps, by their queries. Each covers whole days in India time:
the ERP dump the last ERP_DAYS days, the CP and EDD dumps a day more (DUMP_DAYS), for orders placed the
evening before their DN was made.
- ERP dump (DN_DB_*, the ERP replica): sales order items with their delivery notes and pick lists. MariaDB
  can't run that query in one statement: it merges the CTEs and joins Pick List Items and Click Post Logs on
  unindexed columns by brute force (still running after 12 minutes). So each CTE runs on its own, as written
  (an indexed range on `creation`, about a second each), and _join() does the joins, the ROW_NUMBER() and the
  final WHERE the way the query does. Three CTEs are left out because nothing here reads them, and a LEFT
  JOIN can't drop a row: Delivery Pick List and Shipping Manifest Item only add columns, and Click Post Logs
  (click_creation) also takes over a minute, its log_type filter not being indexed.
- CP dump (SCM_DB_*, scm.cp_database): one row per AWB, for its Clickpost status. Its dates are text, so
  the window ends before tomorrow: `order_date <= '<today>'` would leave out everything ordered today.
- EDD dump (MONGO_URI, the promise engine's orderpromises): the promise engine's first answer for each order
  and warehouse, for its TOTAL_CUTOFF. Without MONGO_URI, or when MongoDB can't be reached, the page is built
  without it (all on the noon rule) and says so, rather than not at all.

What each delivery note gets: the sheet's ERP Dump columns AC-AK, with its Facts tab as the tables below.
- order date: the Shopify order time, else the sales order item's creation (AC);
- ideal pickup date: order date + TOTAL_CUTOFF days, from the EDD dump's row for this order and this DN's
  warehouse; else the same day for orders placed before noon, the next day after (AF). The sheet also added
  an SBD column, which is always empty;
- status: once the courier has the parcel (CP_STATUS), its Clickpost status group; before that, the DN
  status as the warehouse sees it (DN_STATUS) (AG, AH);
- courier: the assigned partner's name (COURIER_NAMES, "SELF" if unknown), else the recommended partner's
  name before its "|", else "No Courier" (AI);
- facility bucket: from the warehouse code's prefix (BUCKETS), not from a list, so a new darkstore is
  counted the day it opens (AK).

The page (static/js/control_tower.js) gets one JSON: the DNs grouped by those fields (the Dashboard tab
pivots them in the browser) and the Warehouse Pendency rows. It is rebuilt every FRESH_FOR_SECONDS on a
schedule, whether or not anyone is looking (start_schedule), by Refresh data, and at the daily run, never
inside a request.
"""
import gzip
import json
import os
import re
import threading
import time
import traceback
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import pymongo
import pymysql

import daily_refresh
from rto_process import get_dn_connection, get_scm_connection

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(HERE, "control_tower_cache.json.gz")  # as served

ERP_DAYS = 7  # the ERP dump: DNs made today and the six days before (the Dashboard shows each of them)
DUMP_DAYS = ERP_DAYS + 1  # the CP and EDD dumps
FRESH_FOR_SECONDS = 30 * 60  # pendency moves by the hour: rebuilt every 30 minutes (start_schedule)
AUTO_RETRY_SECONDS = 600  # how soon a failed rebuild is retried
SCHEDULE_CHECK_SECONDS = 60
STATEMENT_SECONDS = 300  # the databases' own cap on each statement, so a bad plan can't run on for hours

# ------------------------------------------------------------------ the queries


def _window(days, now=None):
    """The first of the last `days` days (today one of them) and the day after today, in India time."""
    today = (now or datetime.now(daily_refresh.IST)).date()
    return today - timedelta(days=days - 1), today + timedelta(days=1)


WINDOW = "creation >= %(since)s AND creation < %(until)s"

# The ERP dump query's CTEs, each run on its own (see the top). The columns are in the order _join() reads.
ERP_QUERIES = {
    "soh": f"""SELECT name AS sales_order, custom_shopify_order_date, custom_pincode, custom_shopify_paid, docstatus
        FROM `tabSales Order`
        WHERE {WINDOW}""",
    "soi": f"""SELECT parent AS sales_order, custom_product_bundle, creation AS so_creation, item_code, qty, net_amount, warehouse
        FROM `tabSales Order Item`
        WHERE {WINDOW}""",
    "dnh": f"""SELECT name AS delivery_note, status, custom_estimated_delivery_date,
               custom_recommended_delivery_partner, custom_assigned_delivery_partner, custom_awb_number
        FROM `tabDelivery Note`
        WHERE {WINDOW}""",
    "dni": f"""SELECT parent AS delivery_note, creation AS dn_creation, item_code, qty AS delivery_qty,
               custom_mother_warehouse AS dn_warehouse, against_sales_order AS sales_order
        FROM `tabDelivery Note Item`
        WHERE {WINDOW}""",
    "pli": f"""SELECT parent AS pick_number, creation AS pick_creation, item_code, qty AS pick_qty, delivery_note
        FROM `tabDelivery Pick List Item`
        WHERE {WINDOW}""",
}
FEE_ITEM = "Cash on Delivery + Platform Fee + Delivery fee"  # the query's final WHERE leaves it out

CP_SELECT = """
SELECT
  courier_partner            AS `Courier Partner`,
  awb                        AS `AWB`,
  pickup_date                AS `Pickup Date`,
  clickpost_unified_status   AS `Clickpost Unified Status`,
  pickup_pincode             AS `Pickup Pincode`,
  drop_pincode               AS `Drop Pincode`,
  shipment_weight            AS `Shipment Weight`,
  payment_mode               AS `Payment Mode`,
  CASE
    WHEN courier_partner = 'Loginext HLD'
         AND (timestamp_of_first_failed_delivery IS NULL OR timestamp_of_first_failed_delivery <> '')
      THEN timestamp_of_first_failed_delivery
    WHEN courier_partner = 'Loginext HLD' THEN delivery_date
    ELSE out_for_delivery_1st_attempt
  END                        AS `Out For Delivery 1st Attempt`,
  out_for_delivery_attempts  AS `Out For Delivery Attempts`,
  delivery_date              AS `Delivery Date`,
  order_date                 AS `Order Date`,
  order_id                   AS `Order ID`,
  return_name                AS `Return Name`
FROM scm.cp_database
WHERE {dates}
  AND (courier_partner IS NULL OR courier_partner <> 'Delhivery Reverse')
"""
# The CP dump query's (order_date in the window OR created_at in it) as two index ranges and a UNION (the AWB
# is in every row, so it only drops a row both found): for more than about a week MySQL reads the OR by
# scanning all 3.4M rows (222 seconds, against about one).
CP_QUERY = (CP_SELECT.format(dates="order_date >= %(since)s AND order_date < %(until)s") + "UNION"
            + CP_SELECT.format(dates="created_at >= %(since)s AND created_at < %(until)s"))


def edd_pipeline(since, until):
    """The EDD dump query, on orderpromises: for each order and warehouse, the promise engine's first answer
    (by createdAt, then the earliest delivery time)."""
    return [
        {"$match": {"createdAt": {"$gte": since, "$lt": until}}},
        {"$unwind": "$promiseEngineResponse.skuWisEdd"},
        {"$project": {"_id": 0, "orderName": 1, "createdAt": 1,
                      "warehouse": "$promiseEngineResponse.skuWisEdd.warehouse",
                      "deliveryType": "$promiseEngineResponse.skuWisEdd.deliveryType",
                      "dayCount": "$promiseEngineResponse.skuWisEdd.dayCount",
                      "deliveryDateTime": "$promiseEngineResponse.skuWisEdd.deliveryDateTime",
                      "TOTAL_CUTOFF": "$promiseEngineResponse.skuWisEdd.eddConstituents.TOTAL_CUTOFF"}},
        {"$sort": {"createdAt": 1, "warehouse": 1, "deliveryDateTime": 1}},
        {"$group": {"_id": {"orderName": "$orderName", "warehouse": "$warehouse"}, "doc": {"$first": "$$ROOT"}}},
        {"$replaceRoot": {"newRoot": "$doc"}},
        {"$sort": {"createdAt": 1, "warehouse": 1, "deliveryDateTime": 1}},
        {"$project": {"orderName": 1, "createdAt": 1, "dayCount": 1, "deliveryDateTime": 1,
                      "warehouse": 1, "deliveryType": 1, "TOTAL_CUTOFF": 1}},
    ]


EDD_COLLECTION = "orderpromises"  # in MONGO_DB, else the database MONGO_URI names

# ------------------------------------------------------------------ the sheet's Facts tab

# DN status -> what the warehouse calls it (Facts F:G). Any other DN status is shown as it is.
DN_STATUS = {
    "Completed": "Packed", "To Be Picked": "To Be Picked", "Under Picking": "Under Picking", "Cancel": "Cancel",
    "Hold": "Hold", "Picked": "Item Picked", "Draft": "Draft", "Put Back Initiated": "Cancel",
    "AWB Registered": "Packed", "To Bill": "Packed", "Return Initiated": "Cancel",
}

# Clickpost status -> (the courier has the parcel, the status group shown) (Facts I:K). A status not listed
# counts as not yet with the courier, as in the sheet. The last four aren't in the sheet's list: they turned
# up in Clickpost since.
CP_STATUS = {
    "OrderPlaced": (0, "Pickup Pending"), "Awb Registered": (0, "Pickup Pending"), "Delivered": (1, "Delivered"),
    "FailedDelivery": (1, "FailedDelivery"), "Cancelled": (1, "Cancelled"), "RTO-Marked": (1, "RTO"),
    "PickupFailed": (0, "Pickup Pending"), "RTO-Failed": (1, "RTO"), "RTO-Delivered": (1, "RTO"),
    "PickupPending": (0, "Pickup Pending"), "OutForPickup": (0, "Pickup Pending"), "InTransit": (1, "InTransit"),
    "OutForDelivery": (1, "OutForDelivery"), "RTO-OutForDelivery": (1, "RTO"), "Damaged": (1, "Cancelled"),
    "RTO-InTransit": (1, "RTO"), "Lost": (1, "Cancelled"), "RTO-Requested": (1, "RTO"), "PickedUp": (1, "Picked Up"),
    "ShipmentDelayed": (1, "Picked Up"), "DestinationHubIn": (1, "InTransit"), "OriginCityOut": (1, "InTransit"),
    "OriginCityIn": (1, "InTransit"), "NoStatusExist": (1, "Pickup Pending"), "ContactCustomerCare": (1, "Pickup Pending"),
    "ShipmentHeld": (1, "InTransit"),
    "ReachedDropLocation": (1, "OutForDelivery"), "RTO-ShipmentDelay": (1, "RTO"),
    "RiderAllocationAwaited": (0, "Pickup Pending"), "RiderAllocated": (0, "Pickup Pending"),
}

# Assigned delivery partner -> courier (Facts M:N), with one spelling per courier: the sheet had both
# "Bluedart" and "Blue Dart", "XpressBees" and "Xpressbees", which split their counts.
COURIER_NAMES = {
    "SPRINT": "Shipyaari", "Bluedart": "Bluedart", "Delhivery": "Delhivery", "Loginext": "Loginext HLD",
    "Delhivery Surface 10kg": "Delhivery", "Shipsy": "Shipsy", "Blowhorn": "Blowhorn HLD", "Pidge": "Pidge HLD",
    "Delhivery Surface 20kg": "Delhivery", "Delhivery Air": "Delhivery", "Blue Dart Air": "Bluedart",
    "Bluedart Surface - Select  500gm": "Bluedart", "shiprocket": "Shiprocket", "SELF": "SELF",
    "Delhivery Surface": "Delhivery", "Delhivery Surface 5kg": "Delhivery", "Porter": "SELF", "Shipyaari": "Shipyaari",
    "Delhivery Surface 2 Kgs": "Delhivery", "XpressBees": "XpressBees", "Blue Dart Surface": "Bluedart",
    "Bluedart Surface 500 gms- Select": "Bluedart", "Shadowfax": "Shadowfax", "Delhivery Surface 10 Kgs": "Delhivery",
    "Xpressbees 10kg": "XpressBees", "DTDC 2kg": "DTDC", "Xpressbees 20kg": "XpressBees", "Xpressbees 5kg": "XpressBees",
    "Delhivery Surface 5 Kgs": "Delhivery", "Delhivery Surface 20 Kgs": "Delhivery", "Xpressbees Surface": "XpressBees",
    "Blue Dart": "Bluedart", "Xpressbees 2kg": "XpressBees", "DTDC Surface 10Kg": "DTDC",
    "Smartr Logistics": "Smartr Logistics", "DTDC Surface 20Kg": "DTDC", "DTDC Surface": "DTDC", "DTDC 5kg": "DTDC",
    "Shiprocket": "Shiprocket", "PicoXpress": "PicoXpress", "Ecom Premium and ROS": "Ecom Premium and ROS",
    "Xpressbees Surface 5kg": "XpressBees", "DTDC Surface 2kg": "DTDC", "Xpressbees Surface 2kg": "XpressBees",
    "Xpressbees Surface 10kg": "XpressBees", "Xpressbees Surface 20kg": "XpressBees", "DTDC Surface 5kg": "DTDC",
    "Abhilaya (One World Logistics) [B2C]": "Abhilaya (One World Logistics) [B2C]", "Xpressbees Air": "XpressBees",
    "Smartr Logistics Air": "Smartr Logistics", "SY_SPRINT": "Shipyaari", "Ecom Air 500gm": "Ecom",
    "Ecom Express Surface 5kg": "Ecom", "Delhivery 10K Surface": "Delhivery", "Delhivery 10K Air": "Delhivery",
    "Ecom Express Surface 10kg": "Ecom", "Amazon Brands": "Amazon", "Blitz": "Blitz", "ElasticRun": "ElasticRun",
    "Elastic Run": "ElasticRun", "123": "SELF", "Shiprocket-Shadowfax Surface": "Shiprocket",
    "Shiprocket-Amazon COD Surface": "Shiprocket", "Shiprocket-Amazon Prepaid Surface ": "Shiprocket",
    "Shiprocket Cargo-Delhivery": "Shiprocket", "Xpressbees Surface_Network Stress": "XpressBees",
    "Shiprocket-Xpressbees Surface": "Shiprocket", "Shiprocket- Shadowfax Surface": "Shiprocket",
    "Shiprocket-Shadowfax Surface_Network Stress": "Shiprocket", "Shiprocket-Xpressbees Surface_Network Stress": "Shiprocket",
    "Shiprcoket-Amazon Shipping Surface": "Shiprocket", "Shiprocket-Instant pickup - Shadowfax": "Shiprocket",
}

# The promise engine's warehouse -> the ERP's (Facts C -> A), where they differ. The sheet dropped the EDD of
# any warehouse not in its list (about 25 darkstores and dropship partners whose codes are the same in both);
# here an unlisted warehouse is taken as its own ERP code. WH0043 is the promise engine's code for Petterworld.
EDD_WAREHOUSES = {
    "WHBLRDSBTM": "DKS_Blr_btmlayout_001", "CWH-BLR001": "DKS_Blr_kaggdaspura_001",
    "BLRDSKN1": "DKS_Blr_kalyannagar_001", "WHBLRDSVTR": "DKS_Blr_varthur_001", "WH0036": "DPS_15buttons_MH_001",
    "WH35": "DPS_ABK_MH_001", "WH014": "DPS_Chullbull_MP_001", "WH011": "DPS_Dogobow_TL_001",
    "WH010": "DPS_Floof_co_TL_001", "WH0031": "DPS_FTLOD_MH_001", "WH0032": "DPS_Furryliving_RJ_001",
    "WH023": "DPS_Furvanity_DL_001", "WH0029": "DPS_GNV_MH_001", "WH0030": "DPS_GNV_UP_001",
    "WH020": "DPS_Goodpaws_MH_001", "WH019": "DPS_House_of_furry_WB_001", "WH024": "DPS_India_hemp_organic_KA_001",
    "WH0041": "DPS_Kanine_HR_001", "WH006": "DPS_Kibbo_DL_001", "WH0033": "DPS_Kitsters_MH_001",
    "WH0034": "DPS_Letswag_MH_001", "WH0037": "DPS_Mitag_MH_001", "WH013": "DPS_Papawcartel_MH_001",
    "WH005": "DPS_PawgyPets_UP_001", "WH018": "DPS_Pawpourri_DL_001", "WH0026": "DPS_Pawsible_HR_001",
    "WH025": "DPS_Petsetgo_DL_001", "WH016": "DPS_Petsnugs_PB_001", "WH0042": "DPS_Petsway_HR_001",
    "WH0043 - Petter world": "DPS_Petterworld_HR_001", "WH0046-Pretty": "DPS_Prettypaws_HR_001",
    "WH022": "DPS_Ruse_UP_001", "Wh0027": "DPS_Sploot_HR_001", "WH0039": "DPS_Tailstotell_HR_001",
    "WH015": "DPS_TalkingDogclub_DL_001", "WH017": "DPS_Topdog_HR_001", "WH012": "DPS_Unleash_MH_001",
    "WH0038": "DPS_Urban_animal_KA_001", "WH009": "DPS_Valuepets_DL_001", "WH0040": "DPS_Woofieez_DL_001",
    "WN-BLR-0002": "MWH_Blr_Neelmangla_001", "WH_Delhi_002": "MWH_Ggn_bhajgera_001",
    "WHHYD001": "DKS_Hyd_Shaikhpet_001", "WH004": "MWH_Mars_GW_001", "WN-MUM-003": "MWH_Mum_Bhiwandi_001",
    "CLINICKORA001": "OFF_Blr_Kora_001", "CLINICBNSK001": "OFF_Blr_Bnsk_001", "PWH002": "PWH_Blr_kaggadaspura_001",
    "PWH004": "PWH_Chennai_besant_ngr_001", "PWH003": "PWH_Del_Okhla_001", "PWH001": "PWH_Hyd_Kondapur_001",
    "PWH006": "PWH_Mum_andheri_001", "WH0047": "DPS_GNV_TN_001", "WH0049": "DPS_WINDLOCK_MH_001",
    "WH0048": "DPS_CHEEKu_WB_001", "WH0043": "DPS_Petterworld_HR_001",
}

# Warehouse code prefix -> facility bucket (Facts A:D agrees for all but BUCKET_EXCEPTIONS).
BUCKETS = {"MWH": "WH", "DKS": "DS", "PWH": "DS", "DPS": "Dropship", "OFF": "Offline"}
BUCKET_EXCEPTIONS = {"MWH_Mars_GW_001": "Dropship", "DPS_Groom_My_Pet_DL_001": "DS"}

# Warehouse Pendency: a mother warehouse's DNs with no AWB yet, still being worked on. (The sheet's query
# left out Cancel, Hold and Draft; this is the same list, minus statuses the sheet had no name for.)
PENDENCY_STATUSES = ("To Be Picked", "Under Picking", "Item Picked", "Packed")
PENDENCY_COLUMNS = ["sales_order", "delivery_note", "warehouse", "pick_number", "ideal_pickup_date",
                    "order_date", "courier", "status"]

# fact flags (the Dashboard tab reads them by bit)
HAS_AWB, HAS_EDD = 1, 2

# ------------------------------------------------------------------ the derived columns


def _dt(value):
    """A DATETIME from either database (pymysql gives datetime; tests and old caches may give text)."""
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def bucket(warehouse):
    if warehouse in BUCKET_EXCEPTIONS:
        return BUCKET_EXCEPTIONS[warehouse]
    return BUCKETS.get((warehouse or "").split("_", 1)[0], "Other")


def courier(assigned, recommended):
    """The sheet's Courier column: the assigned partner's courier, else the recommended route's courier (the
    text before " | "), else "No Courier"."""
    if assigned:
        return COURIER_NAMES.get(assigned, "SELF")
    bar = (recommended or "").find("|")
    if bar < 1:  # no partner at all, or a route like "Unassigned Courier" with no courier name
        return "No Courier"
    return recommended[:bar - 1]


def ideal_pickup_date(order_time, cutoff):
    """The day the parcel should leave: the promise engine's cutoff days after the order day, else the order
    day if ordered before noon, or the day after."""
    day = order_time.date()
    try:
        return day + timedelta(days=int(float(cutoff)))
    except (TypeError, ValueError):  # no promise, or one without a cutoff
        return day if order_time.hour < 12 else day + timedelta(days=1)


def status(dn_status, cp_status):
    """The status shown: the Clickpost group once the courier has the parcel, else the warehouse's name for the
    DN status."""
    picked, group = CP_STATUS.get(cp_status or "", (0, None))
    if picked:
        return group
    return DN_STATUS.get(dn_status, dn_status or "")


# ------------------------------------------------------------------ the ERP query, joined here


def _join(soi, soh, dni, dnh, pli):
    """The ERP dump query's FROM ... LEFT JOIN, ROW_NUMBER() and WHERE, over its CTEs' rows (tuples in
    ERP_QUERIES' column order). One dict per (sales order, DN warehouse, DN, DN status, AWB): the row with the
    earliest sales order item, as rn = 1 keeps (ties: not the fee item, then a picked item, then by item code,
    where the database would pick any). Then the fee item's rows go, as the query's WHERE has it."""
    so_head = {r[0]: r for r in soh}
    dn_head = {r[0]: r for r in dnh}
    dn_items = defaultdict(list)  # (sales order, item) -> its DN items
    for r in dni:
        dn_items[(r[5], r[2])].append(r)
    picks = defaultdict(list)  # (DN, item) -> its pick list items
    for r in pli:
        picks[(r[4], r[2])].append(r)

    best = {}
    for sales_order, _bundle, so_creation, item_code, *_rest in soi:
        head = so_head.get(sales_order)
        for d in dn_items.get((sales_order, item_code)) or (None,):
            dn = d[0] if d else None
            dh = dn_head.get(dn) if dn else None
            for p in (picks.get((dn, item_code)) if dn else None) or (None,):
                awb = dh[5] if dh else None
                key = (sales_order, d[4] if d else None, dn, dh[1] if dh else None, awb)
                rank = (_dt(so_creation) or datetime.max, item_code == FEE_ITEM, p is None, item_code or "")
                if key in best and best[key][0] <= rank:
                    continue
                best[key] = (rank, {
                    "sales_order": sales_order,
                    "so_creation": _dt(so_creation),
                    "item_code": item_code,
                    "shopify_order_date": _dt(head[1]) if head else None,
                    "delivery_note": dn,
                    "dn_warehouse": d[4] if d else None,
                    "dn_status": dh[1] if dh else None,
                    "recommended_partner": dh[3] if dh else None,
                    "assigned_partner": dh[4] if dh else None,
                    "awb": awb,
                    "pick_number": p[0] if p else None,
                })
    return [row for _rank, row in best.values() if row["item_code"] != FEE_ITEM]


class SourceError(Exception):
    """A query that failed, with which one it was: the page shows the text as it is."""


LOST_CONNECTION = (2006, 2013)  # server gone away, lost connection during query
TRIES = 2
MYSQL_ERRORS = {  # what the page says for a failed query
    2006: "lost the connection", 2013: "lost the connection", 2003: "couldn't connect to the database",
    1045: "the database refused the user or password", 1969: "the query ran too long", 3024: "the query ran too long",
    1146: "a table is missing", 1054: "a column is missing",
}


def _fetch(label, connect, sql, cap, days):
    """One query's columns and rows over `days` days. The server waits up to STATEMENT_SECONDS for us to take
    the rows, not its default 60 seconds: 190k rows take over a minute to come down a slow link, and a stall
    past the default made the server drop the connection. A dropped connection is tried once more."""
    since, until = _window(days)
    for attempt in range(1, TRIES + 1):
        conn = None
        try:
            conn = connect(read_timeout=STATEMENT_SECONDS + 60)
            with conn.cursor() as cur:
                for setting in (cap, f"SET SESSION net_write_timeout = {STATEMENT_SECONDS}"):
                    try:
                        cur.execute(setting)
                    except pymysql.err.OperationalError:
                        pass  # an older server without it: run without it
                cur.execute(sql, {"since": since.isoformat(), "until": until.isoformat()})
                return [c[0] for c in cur.description], cur.fetchall()
        except pymysql.err.MySQLError as e:
            code = e.args[0] if e.args else None
            text = e.args[1] if len(e.args) > 1 else str(e)
            if code in LOST_CONNECTION and attempt < TRIES:
                print(f"[control-tower] {label}: {text}; trying again", flush=True)
                time.sleep(5)
                continue
            # the server's own text (users, hosts) goes to the log only; the page gets what kind of failure it was
            print(f"[control-tower] {label} failed: {code} {text}", flush=True)
            said = MYSQL_ERRORS.get(code, f"database error {code}")
            raise SourceError(f"{label}: {said}" + (" (twice)" if code in LOST_CONNECTION else "")) from None
        finally:
            if conn is not None and conn.open:
                conn.close()


def fetch_erp():
    """The ERP dump query's rows (see _join), from its CTEs."""
    cap = f"SET SESSION max_statement_time = {STATEMENT_SECONDS}"  # MariaDB's statement cap, in seconds
    parts = {}
    for name, sql in ERP_QUERIES.items():
        started = time.time()
        parts[name] = _fetch(f"ERP replica ({name})", get_dn_connection, sql, cap, ERP_DAYS)[1]
        print(f"[control-tower] ERP {name}: {len(parts[name])} rows in {time.time() - started:.0f}s", flush=True)
    return _join(parts["soi"], parts["soh"], parts["dni"], parts["dnh"], parts["pli"])


def fetch_cp():
    """The CP dump query's rows, as dicts by its column names."""
    started = time.time()
    columns, rows = _fetch("Clickpost (scm.cp_database)", get_scm_connection, CP_QUERY,
                           f"SET SESSION MAX_EXECUTION_TIME = {STATEMENT_SECONDS * 1000}", DUMP_DAYS)
    print(f"[control-tower] Clickpost: {len(rows)} rows in {time.time() - started:.0f}s", flush=True)
    return [dict(zip(columns, r)) for r in rows]


def fetch_edd():
    """The EDD dump query's rows (dicts), or None when MONGO_URI isn't set."""
    uri = os.getenv("MONGO_URI", "").strip()
    if not uri:
        print("[control-tower] EDD: MONGO_URI isn't set, so every ideal pickup date is on the noon rule", flush=True)
        return None
    since, until = (datetime.combine(d, datetime.min.time(), daily_refresh.IST) for d in _window(DUMP_DAYS))
    started = time.time()
    client = None
    try:
        # A bad URI's error can quote part of the password, so it is never shown or logged: fixed text instead.
        try:
            # read from a secondary when there is one, not the primary the order flow writes to
            client = pymongo.MongoClient(uri, serverSelectionTimeoutMS=20000, readPreference="secondaryPreferred",
                                         appname="warehouse-portal control tower")
        except Exception:  # InvalidURI, or a bad port's ValueError: either may quote the URI
            raise SourceError("Promise engine (MongoDB): MONGO_URI isn't a valid connection string") from None
        name = os.getenv("MONGO_DB", "").strip()
        try:
            db = client[name] if name else client.get_default_database()
        except pymongo.errors.ConfigurationError:
            raise SourceError(f"Promise engine (MongoDB): MONGO_URI names no database, so set MONGO_DB to the one "
                              f"{EDD_COLLECTION} is in") from None
        rows = list(db[EDD_COLLECTION].aggregate(edd_pipeline(since, until), allowDiskUse=True,
                                                 maxTimeMS=STATEMENT_SECONDS * 1000))
    except pymongo.errors.PyMongoError as e:
        # the page shows this: never a user:password@ from the URI
        raise SourceError(re.sub(r"//[^/@\s]+@", "//***@", f"Promise engine (MongoDB): {e}")[:300]) from e
    finally:
        if client is not None:
            client.close()
    print(f"[control-tower] EDD: {len(rows)} rows in {time.time() - started:.0f}s", flush=True)
    return rows


# ------------------------------------------------------------------ the page's data


class _Dims:
    """Dictionary-encodes values: the page's facts hold indexes into dims[name]."""

    def __init__(self, *names):
        self.values = {n: [] for n in names}
        self._index = {n: {} for n in names}

    def __call__(self, name, value):
        index = self._index[name]
        if value not in index:
            index[value] = len(self.values[name])
            self.values[name].append(value)
        return index[value]


def build(erp_rows, cp_rows, edd_rows, now=None, edd_problem=None):
    """The page's data from the three dumps' rows (edd_rows None: not connected, or edd_problem says why not):
    every DN with its derived columns, grouped for the Dashboard tab, and the Warehouse Pendency rows."""
    now = now or datetime.now(daily_refresh.IST)
    cp_status = {r["AWB"]: r.get("Clickpost Unified Status") for r in cp_rows if r.get("AWB")}
    promises = {}  # (order, ERP warehouse) -> its EDD dump row; the first, as the sheet's XLOOKUP takes
    for r in edd_rows or ():
        where = EDD_WAREHOUSES.get(r.get("warehouse"), r.get("warehouse"))
        if r.get("orderName") and where:
            promises.setdefault((r["orderName"], where), r)

    dims = _Dims("facility", "bucket", "date", "dn_status", "status", "courier")
    facts = defaultdict(int)
    dns_seen = set()
    pendency = {}
    for row in sorted(erp_rows, key=lambda r: (r["so_creation"] or datetime.max, r["delivery_note"] or "")):
        dn = row["delivery_note"]
        if not dn:
            continue  # an order item no DN has been made for: nothing to dispatch yet
        awb = (row["awb"] or "").strip()
        promise = promises.get((row["sales_order"], row["dn_warehouse"]))
        order_time = row["shopify_order_date"] or row["so_creation"]
        if order_time is None:
            continue
        ideal = ideal_pickup_date(order_time, promise.get("TOTAL_CUTOFF") if promise else None)
        shown = status(row["dn_status"], cp_status.get(awb) if awb else None)
        facility = row["dn_warehouse"] or "(no warehouse)"
        where = bucket(row["dn_warehouse"])
        who = courier(row["assigned_partner"], row["recommended_partner"])

        if not awb and where == "WH" and shown in PENDENCY_STATUSES:
            line = (row["sales_order"], dn, facility, row["pick_number"] or "", ideal.isoformat(),
                    order_time.date().isoformat(), who, shown)
            pendency[line] = None
        if dn in dns_seen:
            continue  # the dashboard counts DNs: one more sales order on the same DN changes nothing
        dns_seen.add(dn)
        flags = (HAS_AWB if awb else 0) | (HAS_EDD if promise and _dt(promise.get("deliveryDateTime")) else 0)
        facts[(dims("facility", facility), dims("bucket", where), dims("date", ideal.isoformat()),
               dims("date", order_time.date().isoformat()), dims("dn_status", row["dn_status"] or ""),
               dims("status", shown), dims("courier", who), flags)] += 1

    return {
        "generated_at": now.timestamp(),
        "generated_at_ist": now.strftime("%Y-%m-%d %H:%M"),
        "today": now.date().isoformat(),
        "window_days": ERP_DAYS,
        "dims": dims.values,
        # facility, bucket, ideal pickup date, order date, DN status, status, courier, flags, DNs
        "facts": [list(key) + [n] for key, n in facts.items()],
        "pendency": {"columns": PENDENCY_COLUMNS, "rows": sorted(pendency, key=lambda r: (r[4], r[2], r[1]))},
        "counts": {"erp_rows": len(erp_rows), "cp_rows": len(cp_rows),
                   "edd_rows": None if edd_rows is None else len(edd_rows), "dns": len(dns_seen)},
        "edd_problem": edd_problem,
    }


# ------------------------------------------------------------------ the cache

_state_lock = threading.Lock()
_refresh_lock = threading.Lock()
_loaded = False
_payload = None  # (gzipped JSON, ETag) as served
_fetched_at = None  # epoch seconds of the data in _payload
_last_error = None
_last_attempt_at = 0.0


def _etag(fetched_at):
    return f'"control-tower-{int(fetched_at * 1000)}"'


def _ensure_loaded():
    """Picks up the data the last build left on disk, so a restart serves it straight away."""
    global _loaded, _payload, _fetched_at
    with _state_lock:
        if _loaded:
            return
        _loaded = True
        try:
            with open(CACHE_FILE, "rb") as fh:
                gz = fh.read()
            fetched = json.loads(gzip.decompress(gz))["generated_at"]
        except (OSError, ValueError, KeyError, TypeError, EOFError):
            return  # no cache yet, or a broken one: the next build replaces it
        _payload, _fetched_at = (gz, _etag(fetched)), fetched


def _is_stale():
    return _fetched_at is None or time.time() - _fetched_at > FRESH_FOR_SECONDS


def refresh_cache(force=False):
    """Rebuilds the data from the three dumps' sources, if it is stale (always when forced). One build at a time; a
    failed one keeps the previous data and says why (snapshot)."""
    global _payload, _fetched_at, _last_error, _last_attempt_at
    _ensure_loaded()
    if not force and not _is_stale():
        return
    if not _refresh_lock.acquire(blocking=False):
        return  # one is running already
    started = _last_attempt_at = time.time()
    try:
        # Three different servers: ask them all at once (the ERP takes longest, the others a few seconds).
        with ThreadPoolExecutor(max_workers=2) as pool:
            cp_rows, edd_rows = pool.submit(fetch_cp), pool.submit(fetch_edd)
            erp_rows = fetch_erp()
            try:
                edd, edd_problem = edd_rows.result(), None
            except SourceError as e:  # built on the noon rule, saying why, rather than not built at all
                traceback.print_exc()
                edd, edd_problem = None, str(e)
            # dated from when the queries started, so the next build is due 30 minutes after this one began
            data = build(erp_rows, cp_rows.result(), edd, now=datetime.fromtimestamp(started, daily_refresh.IST),
                         edd_problem=edd_problem)
        gz = gzip.compress(json.dumps(data, separators=(",", ":"), default=str).encode(), compresslevel=6)
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(gz)
        os.replace(tmp, CACHE_FILE)  # readers never see half a file
        with _state_lock:
            _payload, _fetched_at, _last_error = (gz, _etag(data["generated_at"])), data["generated_at"], None
        print(f"[control-tower] new data in {time.time() - started:.0f}s: {data['counts']}", flush=True)
    except Exception as e:
        traceback.print_exc()
        with _state_lock:
            _last_error = (str(e) if isinstance(e, SourceError) else f"{type(e).__name__}: {e}")[:300]
    finally:
        _refresh_lock.release()


def refresh_cache_async(force=False):
    threading.Thread(target=refresh_cache, args=(force,), daemon=True, name="control-tower").start()


def is_refreshing():
    if _refresh_lock.acquire(blocking=False):
        _refresh_lock.release()
        return False
    return True


def _catch_up():
    """Starts a rebuild when the data is stale and no rebuild failed in the last AUTO_RETRY_SECONDS."""
    if _is_stale() and not is_refreshing() and time.time() - _last_attempt_at > AUTO_RETRY_SECONDS:
        refresh_cache_async()


def _schedule():
    while True:
        time.sleep(SCHEDULE_CHECK_SECONDS)
        try:
            _ensure_loaded()
            _catch_up()
        except Exception:
            traceback.print_exc()  # keep the schedule alive


def start_schedule():
    """Rebuilds the data every FRESH_FOR_SECONDS from now on, whether or not anyone is looking (a failed build:
    again after AUTO_RETRY_SECONDS), on a background thread. main.py starts it with the server."""
    threading.Thread(target=_schedule, daemon=True, name="control-tower-schedule").start()


def snapshot():
    """For the top bar and the page: when the data was built, whether a build is running, what last failed."""
    _ensure_loaded()
    _catch_up()
    with _state_lock:
        return {"fetched_at": _fetched_at, "refreshing": is_refreshing(), "error": _last_error,
                "fresh_for_minutes": FRESH_FOR_SECONDS // 60}


def payload():
    """(gzipped JSON, ETag) of the data as last built, or None if it has never been built."""
    _ensure_loaded()
    _catch_up()
    with _state_lock:
        return _payload
