"""RTO Analytics > RTO MDND: the summary behind the dashboard, built from the MDND cache.

Every row is an RTO-Delivered AWB with a Delivery Note value, in the warehouse it came back to,
plus the check a person typed into the MDND tab (Received at WH?, Condition, ...). The dashboard
shows what came back, what it is worth, and how much of it has been checked. Read from
rto_mdnd.snapshot() and mdnd_inputs.apply_inputs(); nothing here queries a database.
"""

import mdnd_inputs
import rto_mdnd
from analytics_common import (
    age_in_days, breakdown, crosstab, day_of, histogram, in_date_range, parse_date, pick_granularity,
    stacked_trend, today_date,
)

DATE_FIELDS = ("received_on", "marked_on")
CHECKED, PENDING = "Checked", "Pending"
NOT_FILLED = "(Not filled)"
NO_REASON = "(no reason)"

DAYS_BUCKETS = [  # RTO marked -> received, whole days
    ("0-1 days", 0, 1),
    ("2-3 days", 2, 3),
    ("4-5 days", 4, 5),
    ("6-10 days", 6, 10),
    ("11-20 days", 11, 20),
    ("21+ days", 21, None),
]
OVERDUE_DAYS = 7  # received this long ago and still not checked
TREND_WAREHOUSE_LIMIT = len(rto_mdnd.TABS)
TOP_CITIES = 12
TOP_REASONS = 12
CHECK_LIST_SIZE = 5


def _is_checked(row):
    # Same rule as the MDND tab: a row is filled once "Received at WH?" has a value.
    return bool(row["received_at_wh"])


def _damaged(row):
    return row["received_at_wh"] == mdnd_inputs.RECEIVED_AND_DAMAGED or row["condition"] == "Damaged"


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _matching(rows, warehouse, courier, reason, state):
    return [
        r for r in rows
        if (not warehouse or r["tab"] == warehouse)
        and (not courier or r["courier"] == courier)
        and (not reason or (r["rto_reason"] or NO_REASON) == reason)
        and (state is None or _is_checked(r) == (state == "checked"))
    ]


def export_rows(date_field, start_str, end_str, warehouse=None, courier=None, reason=None, state=None):
    """The rows the dashboard counts for these filters, with their typed inputs - what its Download CSV saves."""
    if date_field not in DATE_FIELDS:
        raise ValueError(f"Unsupported date_field: {date_field}")
    if state not in (None, "checked", "pending"):
        raise ValueError(f"Unsupported state: {state}")
    rows = mdnd_inputs.apply_inputs(rto_mdnd.snapshot()["rows"])
    return _matching(in_date_range(rows, lambda r: day_of(r.get(date_field)), start_str, end_str), warehouse, courier, reason, state)


def build_summary(date_field, start_str, end_str, warehouse=None, courier=None, reason=None, state=None, today=None):
    """`warehouse` is a city tab name (Gurgaon, ...); `state` is "checked" / "pending" / None."""
    if date_field not in DATE_FIELDS:
        raise ValueError(f"Unsupported date_field: {date_field}")
    if state not in (None, "checked", "pending"):
        raise ValueError(f"Unsupported state: {state}")
    snap = rto_mdnd.snapshot()
    if snap["loading"]:
        return {"loading": True, "available": False, "fetched_at": None, "refreshing": snap["refreshing"],
                "error": snap["error"], "total": 0}
    today = today_date(today)
    start = parse_date(start_str) if start_str else None
    end = parse_date(end_str) if end_str else None

    date_of = lambda r: day_of(r.get(date_field))  # noqa: E731
    in_range = in_date_range(mdnd_inputs.apply_inputs(snap["rows"]), date_of, start_str, end_str)
    options = {
        "warehouses": [t for t in rto_mdnd.TABS if any(r["tab"] == t for r in in_range)],
        "couriers": sorted({r["courier"] for r in in_range if r["courier"]}),
        "reasons": [x["label"] for x in breakdown(in_range, lambda r: r["rto_reason"], blank=NO_REASON)],
    }
    rows = _matching(in_range, warehouse, courier, reason, state)

    checked = [r for r in rows if _is_checked(r)]
    pending = [r for r in rows if not _is_checked(r)]
    value = lambda rs: sum(r["net_value"] for r in rs)  # noqa: E731
    days = [d for d in (_int(r["days_to_receipt"]) for r in rows) if d is not None and d >= 0]
    state_of = lambda r: CHECKED if _is_checked(r) else PENDING  # noqa: E731

    scorecard = []
    for tab in rto_mdnd.TABS:
        group = [r for r in rows if r["tab"] == tab]
        if not group:
            continue
        done = [r for r in group if _is_checked(r)]
        scorecard.append({
            "label": tab, "count": len(group), "checked": len(done), "pending": len(group) - len(done),
            "checked_rate": round(100 * len(done) / len(group), 1),
            "net_value": value(group), "pending_value": value([r for r in group if not _is_checked(r)]),
        })
    scorecard.sort(key=lambda s: (-s["count"], s["label"]))

    overdue = []
    for r in pending:
        age = age_in_days(r["received_on"], today)
        if age is not None and age >= OVERDUE_DAYS:
            overdue.append((age, r))
    overdue.sort(key=lambda t: (-t[0], -t[1]["net_value"], t[1]["awb"]))

    return {
        "loading": False,
        "available": True,
        "fetched_at": snap["fetched_at"],
        "refreshing": snap["refreshing"],
        "error": snap["error"],
        "total": len(rows),
        "date_field": date_field,
        "range": {"start": start_str, "end": end_str},
        "filters": {"warehouse": warehouse, "courier": courier, "reason": reason, "state": state},
        "options": options,
        "granularity": pick_granularity(start, end),
        "kpis": {
            "awbs": len(rows),
            "net_value": value(rows),
            "checked": len(checked),
            "checked_rate": round(100 * len(checked) / len(rows), 1) if rows else None,
            "pending": len(pending),
            "pending_value": value(pending),
            "damaged": sum(1 for r in rows if _damaged(r)),
            "avg_days": round(sum(days) / len(days), 1) if days else None,
        },
        "scorecard": scorecard,
        "trend_by_warehouse": stacked_trend(rows, date_of, lambda r: r["tab"], start, end, limit=TREND_WAREHOUSE_LIMIT),
        "trend_by_state": stacked_trend(rows, date_of, state_of, start, end, series=[CHECKED, PENDING]),
        "by_warehouse": breakdown(rows, lambda r: r["tab"]),
        "by_courier": breakdown(rows, lambda r: r["courier"]),
        "by_reason": breakdown(rows, lambda r: r["rto_reason"], blank=NO_REASON)[:TOP_REASONS],
        "by_city": breakdown(rows, lambda r: r["drop_city"])[:TOP_CITIES],
        "by_received_at_wh": breakdown(rows, lambda r: r["received_at_wh"], blank=NOT_FILLED),
        "by_condition": breakdown(checked, lambda r: r["condition"], blank="(Not set)"),
        "by_checker": breakdown(checked, lambda r: r["checked_by"], blank="(Not recorded)"),
        "courier_by_state": crosstab(rows, lambda r: r["courier"], [CHECKED, PENDING], state_of),
        "days_to_receipt": {**histogram(days, DAYS_BUCKETS)},
        "checks": {
            "overdue_days": OVERDUE_DAYS,
            "overdue": len(overdue),
            "overdue_value": sum(r["net_value"] for _, r in overdue),
            "oldest_pending": [{"awb": r["awb"], "days": age, "net_value": r["net_value"], "warehouse": r["tab"]}
                               for age, r in overdue[:CHECK_LIST_SIZE]],
            "with_notes": sum(1 for r in rows if (r["discrepancy_notes"] or "").strip()),
        },
    }
