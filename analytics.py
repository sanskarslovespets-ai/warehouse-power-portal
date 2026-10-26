"""Aggregates the logged RTO Process entries into the summary the RTO Analytics dashboard renders.

Grouping/counting is done in Python, not SQL - the entries table holds what's been
logged through this portal (hundreds to low thousands of rows, not the
million-row cp_database_N), so pulling the filtered rows and aggregating in
memory is simpler than hand-rolled SQL and plenty fast at this size.

The summary is built around the RTO Process entries (what was logged, where, by which courier,
how long it took to receive) plus one cross-check against the RTO Delivered cache: of the AWBs
that came back RTO-Delivered, how many have damage logged here (the same rule the RTO MDND tab
uses to auto-fill "Received and Damaged").
"""

from collections import Counter
from statistics import median

import entries_store
from analytics_common import (
    bucket_key, day_of, fill_buckets, parse_date, pick_granularity, sorted_counts, stacked_trend,
)

DAMAGE_TYPES = ["RTO", "RPU", "WH Damage", "Inward Damage"]

# Receipt lag = RTO/RPU received date minus damage marked date, in whole days.
LAG_BUCKETS = [
    ("Same day", 0, 0),
    ("1 day", 1, 1),
    ("2 days", 2, 2),
    ("3-5 days", 3, 5),
    ("6-10 days", 6, 10),
    ("11+ days", 11, None),
]

REPEAT_AWB_LIST_SIZE = 5

# "Entries over time by facility" shows this many facilities, the rest are folded into "Other".
TREND_FACILITY_LIMIT = 7


def _entry_date(entry, date_field):
    return day_of(entry.get(date_field))


def _by_type_matrix(entries, key):
    """One row per `key` value with its entry count per damage type, biggest first."""
    rows = {}
    for e in entries:
        label = e[key] or "(blank)"
        row = rows.setdefault(label, {"label": label, "total": 0, "types": {t: 0 for t in DAMAGE_TYPES}})
        row["total"] += 1
        if e["damage_type"] in row["types"]:
            row["types"][e["damage_type"]] += 1
    return sorted(rows.values(), key=lambda r: (-r["total"], r["label"]))


def _facility_trend(entries, date_field, start, end):
    """Per-period entry counts for the biggest facilities (the rest folded into "Other"); covers
    the same periods as the damage-type trend because it counts the same entries."""
    trend = stacked_trend(entries, lambda e: _entry_date(e, date_field), lambda e: e["facility_name"],
                          start, end, limit=TREND_FACILITY_LIMIT)
    return {"series": trend["series"], "points": trend["points"]}


def _lag_days(entry):
    marked, received = _entry_date(entry, "damage_marked_date"), _entry_date(entry, "received_date")
    if not marked or not received:
        return None
    return (received - marked).days


def _lag_summary(entries):
    lags = [d for d in (_lag_days(e) for e in entries) if d is not None]
    valid = [d for d in lags if d >= 0]
    buckets = []
    for label, low, high in LAG_BUCKETS:
        buckets.append({
            "label": label,
            "count": sum(1 for d in valid if d >= low and (high is None or d <= high)),
        })
    return {
        "n": len(valid),
        "avg_days": round(sum(valid) / len(valid), 1) if valid else None,
        "median_days": median(valid) if valid else None,
        "max_days": max(valid) if valid else None,
        "buckets": buckets,
        "received_before_marked": len(lags) - len(valid),
    }


def _repeat_awbs(entries):
    counts = Counter(e["awb_code"].strip() for e in entries if e["awb_code"].strip())
    repeated = [(awb, n) for awb, n in counts.most_common() if n > 1]
    return {
        "count": len(repeated),  # AWBs logged more than once
        "top": [{"awb": awb, "count": n} for awb, n in repeated[:REPEAT_AWB_LIST_SIZE]],
    }


def _coverage(start_str, end_str, facility, courier):
    """Of the RTO-Delivered AWBs in the range, how many have a RTO Process entry (any damage type).

    Read from the RTO Delivered cache (rto_delivered.py): nothing here queries the database. The
    damage-type filter does not apply (an AWB counts as logged whatever type was entered).
    """
    empty = {"available": False, "loading": False, "delivered": 0, "logged": 0, "rate": None,
             "by_facility": [], "by_courier": []}
    try:
        import rto_delivered

        state = rto_delivered.query(date_from=start_str, date_to=end_str, return_name=facility, limit=1)
        if state["loading"]:
            return {**empty, "loading": True}
        rows = rto_delivered.all_filtered(date_from=start_str, date_to=end_str, return_name=facility)
    except Exception:
        return empty
    if courier:
        rows = [r for r in rows if r["courier_partner"] == courier]

    logged_awbs = set(entries_store.entries_by_awb())
    delivered_awbs = {r["awb"].strip() for r in rows if r["awb"].strip()}

    def breakdown(key):
        groups = {}
        for r in rows:
            awb = r["awb"].strip()
            if awb:
                groups.setdefault(r[key] or "(blank)", set()).add(awb)
        out = [
            {"label": label, "delivered": len(awbs), "logged": len(awbs & logged_awbs),
             "rate": round(100 * len(awbs & logged_awbs) / len(awbs), 1)}
            for label, awbs in groups.items()
        ]
        return sorted(out, key=lambda r: (-r["delivered"], r["label"]))

    logged = len(delivered_awbs & logged_awbs)
    return {
        "available": True,
        "loading": False,
        "delivered": len(delivered_awbs),
        "logged": logged,
        "rate": round(100 * logged / len(delivered_awbs), 1) if delivered_awbs else None,
        "by_facility": breakdown("return_name"),
        "by_courier": breakdown("courier_partner"),
    }


def _matching(entries, damage_type, facility, courier):
    return [
        e for e in entries
        if (not damage_type or e["damage_type"] == damage_type)
        and (not facility or e["facility_name"] == facility)
        and (not courier or e["courier_name"] == courier)
    ]


def export_rows(date_field, start_str, end_str, damage_type=None, facility=None, courier=None):
    """The entries the dashboard counts for these filters - what its Download CSV saves."""
    return _matching(entries_store.query_entries_in_range(date_field, start_str, end_str), damage_type, facility, courier)


def build_summary(date_field, start_str, end_str, damage_type=None, facility=None, courier=None):
    start = parse_date(start_str) if start_str else None
    end = parse_date(end_str) if end_str else None

    in_range = entries_store.query_entries_in_range(date_field, start_str, end_str)
    # The facility / courier pick-lists come from the date range only, so choosing one
    # does not make the other options disappear.
    options = {
        "facilities": sorted({e["facility_name"] for e in in_range if e["facility_name"]}),
        "couriers": sorted({e["courier_name"] for e in in_range if e["courier_name"]}),
    }
    entries = _matching(in_range, damage_type, facility, courier)

    damage_type_counts = Counter()
    facility_counts = Counter()
    courier_counts = Counter()
    sku_counts = Counter()
    synced_count = 0
    pending_count = 0
    granularity = pick_granularity(start, end)
    trend = Counter()
    trend_by_type = {}

    for e in entries:
        damage_type_counts[e["damage_type"]] += 1
        facility_counts[e["facility_name"]] += 1
        courier_counts[e["courier_name"]] += 1
        sku_counts[e["sku_code"]] += 1
        if e["sheet_synced"]:
            synced_count += 1
        else:
            pending_count += 1
        d = _entry_date(e, date_field)
        if d:
            key = bucket_key(d, granularity)
            trend[key] += 1
            trend_by_type.setdefault(key, Counter())[e["damage_type"]] += 1

    buckets = fill_buckets(trend.keys(), granularity)

    return {
        "total": len(entries),
        "date_field": date_field,
        "range": {"start": start_str, "end": end_str},
        "filters": {"damage_type": damage_type, "facility": facility, "courier": courier},
        "options": options,
        "granularity": granularity,
        "distinct": {
            "awb": len({e["awb_code"].strip() for e in entries if e["awb_code"].strip()}),
            "sku": len({e["sku_code"] for e in entries if e["sku_code"]}),
            "facility": len({e["facility_name"] for e in entries if e["facility_name"]}),
            "courier": len({e["courier_name"] for e in entries if e["courier_name"]}),
        },
        "by_damage_type": {dt: damage_type_counts.get(dt, 0) for dt in DAMAGE_TYPES},
        "by_facility": sorted_counts(facility_counts),
        "by_courier": sorted_counts(courier_counts),
        "by_sku": sorted_counts(sku_counts),
        "facility_by_type": _by_type_matrix(entries, "facility_name"),
        "courier_by_type": _by_type_matrix(entries, "courier_name"),
        "lag": _lag_summary(entries),
        "repeat_awbs": _repeat_awbs(entries),
        "coverage": _coverage(start_str, end_str, facility, courier),
        "sheet_sync": {"synced": synced_count, "pending": pending_count},
        "trend": [{"bucket": k, "count": trend.get(k, 0)} for k in buckets],
        "trend_by_facility": _facility_trend(entries, date_field, start, end),
        "trend_by_type": [
            {"bucket": k, "total": trend.get(k, 0),
             "types": {t: trend_by_type.get(k, Counter()).get(t, 0) for t in DAMAGE_TYPES}}
            for k in buckets
        ],
    }
