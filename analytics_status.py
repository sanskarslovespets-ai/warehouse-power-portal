"""RTO Analytics > RTO Status: the summary behind the dashboard, built from the RTO Delivered cache.

Every row is an AWB the courier reports as RTO-Delivered, with the latest ERP (Delivery Note)
status joined on - so the dashboard answers "how many came back, where to, via whom, and how far
has the ERP caught up". Read from rto_delivered.snapshot(); nothing here queries a database.
"""

from collections import Counter

import rto_delivered
from analytics_common import (
    age_in_days, breakdown, crosstab, day_of, days_between, histogram, in_date_range, pick_granularity,
    parse_date, sorted_counts, stacked_trend, today_date, OTHER_LABEL,
)

NO_STATUS = "(No status)"  # label for an AWB with no Delivery Note
COMPLETED = "Return Completed"  # the ERP's final state for a return
DATE_FIELDS = ("rto_delivery_date", "pickup_date", "created_at")

TRANSIT_BUCKETS = [  # pickup -> RTO delivered, whole days
    ("0-2 days", 0, 2),
    ("3-5 days", 3, 5),
    ("6-10 days", 6, 10),
    ("11-15 days", 11, 15),
    ("16-30 days", 16, 30),
    ("31+ days", 31, None),
]
STATUS_COLUMNS = 5  # statuses shown in the courier / return name matrix, the rest are "Other"
TREND_STATUS_LIMIT = 6
TREND_RETURN_NAME_LIMIT = 7
STUCK_AFTER_DAYS = 7  # delivered this long ago and the ERP still is not "Return Completed"
CHECK_LIST_SIZE = 5
TOP_REMARKS = 12


def _status(row):
    return row.get("status") or NO_STATUS


def _empty(state, **extra):
    return {"loading": state["loading"], "available": not state["loading"], "fetched_at": state["fetched_at"],
            "refreshing": state["refreshing"], "error": state["error"], **extra}


def _matching(rows, return_name, courier, status):
    return [
        r for r in rows
        if (not return_name or r["return_name"] == return_name)
        and (not courier or r["courier_partner"] == courier)
        and (not status or _status(r) == status)
    ]


def export_rows(date_field, start_str, end_str, return_name=None, courier=None, status=None):
    """The rows the dashboard counts for these filters - what its Download CSV saves."""
    if date_field not in DATE_FIELDS:
        raise ValueError(f"Unsupported date_field: {date_field}")
    in_range = in_date_range(rto_delivered.snapshot()["rows"], lambda r: day_of(r.get(date_field)), start_str, end_str)
    return _matching(in_range, return_name, courier, status)


def build_summary(date_field, start_str, end_str, return_name=None, courier=None, status=None, today=None):
    if date_field not in DATE_FIELDS:
        raise ValueError(f"Unsupported date_field: {date_field}")
    state = rto_delivered.snapshot()
    if state["loading"]:
        return _empty(state, total=0)
    today = today_date(today)
    start = parse_date(start_str) if start_str else None
    end = parse_date(end_str) if end_str else None

    in_range = in_date_range(state["rows"], lambda r: day_of(r.get(date_field)), start_str, end_str)
    # Pick-lists come from the date range only, so choosing one does not hide the other options.
    options = {
        "return_names": sorted({r["return_name"] for r in in_range if r["return_name"]}),
        "couriers": sorted({r["courier_partner"] for r in in_range if r["courier_partner"]}),
        "statuses": [x["label"] for x in breakdown(in_range, _status)],
    }
    rows = _matching(in_range, return_name, courier, status)
    date_of = lambda r: day_of(r.get(date_field))  # noqa: E731

    status_counts = Counter(_status(r) for r in rows)
    shown_statuses = [label for label, _ in status_counts.most_common(STATUS_COLUMNS)]
    columns = shown_statuses + ([OTHER_LABEL] if len(status_counts) > STATUS_COLUMNS else [])
    status_column = lambda r: _status(r) if _status(r) in shown_statuses else OTHER_LABEL  # noqa: E731

    transit = [d for d in (days_between(r["pickup_date"], r["rto_delivery_date"]) for r in rows) if d is not None]
    transit_ok = [d for d in transit if d >= 0]

    completed = status_counts.get(COMPLETED, 0)
    stuck = []
    for r in rows:
        age = age_in_days(r["rto_delivery_date"], today)
        if age is not None and age >= STUCK_AFTER_DAYS and r.get("status") != COMPLETED:
            stuck.append((age, r))
    stuck.sort(key=lambda t: (-t[0], t[1]["awb"]))

    return {
        "loading": False,
        "available": True,
        "fetched_at": state["fetched_at"],
        "refreshing": state["refreshing"],
        "error": state["error"],
        "total": len(rows),
        "date_field": date_field,
        "range": {"start": start_str, "end": end_str},
        "filters": {"return_name": return_name, "courier": courier, "status": status},
        "options": options,
        "granularity": pick_granularity(start, end),
        "kpis": {
            "awbs": len({r["awb"] for r in rows if r["awb"]}),
            "completed": completed,
            "completed_rate": round(100 * completed / len(rows), 1) if rows else None,
            "not_completed": len(rows) - completed,
            "no_status": status_counts.get(NO_STATUS, 0),
            "avg_transit_days": round(sum(transit_ok) / len(transit_ok), 1) if transit_ok else None,
            "return_names": len({r["return_name"] for r in rows if r["return_name"]}),
            "couriers": len({r["courier_partner"] for r in rows if r["courier_partner"]}),
        },
        "by_status": sorted_counts(status_counts),
        "by_return_name": breakdown(rows, lambda r: r["return_name"]),
        "by_courier": breakdown(rows, lambda r: r["courier_partner"]),
        "by_city": breakdown(rows, lambda r: r["return_city"]),
        "by_remark": breakdown(rows, lambda r: r["latest_remark"], blank="(no remark)")[:TOP_REMARKS],
        "trend_by_status": stacked_trend(rows, date_of, _status, start, end, limit=TREND_STATUS_LIMIT),
        "trend_by_return_name": stacked_trend(rows, date_of, lambda r: r["return_name"], start, end,
                                              limit=TREND_RETURN_NAME_LIMIT),
        "courier_by_status": crosstab(rows, lambda r: r["courier_partner"], columns, status_column),
        "return_name_by_status": crosstab(rows, lambda r: r["return_name"], columns, status_column),
        "transit": {**histogram(transit_ok, TRANSIT_BUCKETS), "negative": len(transit) - len(transit_ok)},
        "checks": {
            "stuck_after_days": STUCK_AFTER_DAYS,
            "stuck": len(stuck),
            "stuck_no_note": sum(1 for _, r in stuck if not r.get("status")),
            "oldest": [{"awb": r["awb"], "days": age, "status": _status(r), "courier": r["courier_partner"]}
                       for age, r in stuck[:CHECK_LIST_SIZE]],
        },
    }
