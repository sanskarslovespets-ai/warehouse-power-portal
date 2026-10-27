"""RTO Analytics > RTO Reconciliation: the summary behind the dashboard, built from the Recon cache.

Every row is an AWB the courier still reports as RTO-InTransit 10+ days after the RTO was marked,
with the ERP's latest Delivery Note status alongside. The dashboard shows how old the backlog is,
who holds it, and where the ERP and the courier disagree. Read from rto_recon.snapshot(); nothing
here queries a database.
"""

from collections import Counter

import rto_recon
from analytics_common import (
    breakdown, crosstab, day_of, histogram, in_date_range, parse_date, pick_granularity, sorted_counts,
    stacked_trend,
)

NO_STATUS = rto_recon.NO_STATUS  # label for an AWB with no Delivery Note
ERP_COMPLETED = "Return Completed"
DATE_FIELDS = rto_recon.DATE_FIELDS
AGE_BUCKETS = rto_recon.AGE_BUCKETS  # days since the RTO was marked; the RTO Reconciliation panel filters on the same
LONG_PENDING_DAYS = 30
TREND_COURIER_LIMIT = 7
TREND_STATUS_LIMIT = 6
CHECK_LIST_SIZE = 5


def _erp(row):
    return row.get("erp_status") or NO_STATUS


_age = rto_recon.age_days
_age_bucket = rto_recon.age_bucket


def _matching(rows, return_name, courier, erp_status):
    return [
        r for r in rows
        if (not return_name or r["return_name"] == return_name)
        and (not courier or r["courier_partner"] == courier)
        and (not erp_status or _erp(r) == erp_status)
    ]


def export_rows(date_field, start_str, end_str, return_name=None, courier=None, erp_status=None):
    """The rows the dashboard counts for these filters - what its Download CSV saves."""
    if date_field not in DATE_FIELDS:
        raise ValueError(f"Unsupported date_field: {date_field}")
    in_range = in_date_range(rto_recon.snapshot()["rows"], lambda r: day_of(r.get(date_field)), start_str, end_str)
    return _matching(in_range, return_name, courier, erp_status)


def build_summary(date_field, start_str, end_str, return_name=None, courier=None, erp_status=None):
    if date_field not in DATE_FIELDS:
        raise ValueError(f"Unsupported date_field: {date_field}")
    state = rto_recon.snapshot()
    if state["loading"]:
        return {"loading": True, "available": False, "fetched_at": None, "refreshing": state["refreshing"],
                "error": state["error"], "total": 0}
    start = parse_date(start_str) if start_str else None
    end = parse_date(end_str) if end_str else None

    date_of = lambda r: day_of(r.get(date_field))  # noqa: E731
    in_range = in_date_range(state["rows"], date_of, start_str, end_str)
    options = {
        "return_names": sorted({r["return_name"] for r in in_range if r["return_name"]}),
        "couriers": sorted({r["courier_partner"] for r in in_range if r["courier_partner"]}),
        "erp_statuses": [x["label"] for x in breakdown(in_range, _erp)],
    }
    rows = _matching(in_range, return_name, courier, erp_status)

    ages = [a for a in (_age(r) for r in rows) if a is not None]
    erp_counts = Counter(_erp(r) for r in rows)
    long_pending = [r for r in rows if (_age(r) or 0) >= LONG_PENDING_DAYS]
    erp_done = sorted((r for r in rows if r.get("erp_status") == ERP_COMPLETED), key=lambda r: (-(_age(r) or 0), r["awb"]))
    age_labels = [label for label, _, _ in AGE_BUCKETS]
    hist = histogram(ages, AGE_BUCKETS)

    return {
        "loading": False,
        "available": True,
        "fetched_at": state["fetched_at"],
        "refreshing": state["refreshing"],
        "error": state["error"],
        "total": len(rows),
        "date_field": date_field,
        "range": {"start": start_str, "end": end_str},
        "filters": {"return_name": return_name, "courier": courier, "erp_status": erp_status},
        "options": options,
        "granularity": pick_granularity(start, end),
        "kpis": {
            "awbs": len({r["awb"] for r in rows if r["awb"]}),
            "avg_days": hist["avg"],
            "oldest_days": hist["max"],
            "long_pending": len(long_pending),
            "long_pending_rate": round(100 * len(long_pending) / len(rows), 1) if rows else None,
            "erp_completed": len(erp_done),
            "no_erp_status": erp_counts.get(NO_STATUS, 0),
            "return_names": len({r["return_name"] for r in rows if r["return_name"]}),
            "couriers": len({r["courier_partner"] for r in rows if r["courier_partner"]}),
        },
        "by_erp_status": sorted_counts(erp_counts),
        "by_courier": breakdown(rows, lambda r: r["courier_partner"]),
        "by_return_name": breakdown(rows, lambda r: r["return_name"]),
        "trend_by_courier": stacked_trend(rows, date_of, lambda r: r["courier_partner"], start, end,
                                          limit=TREND_COURIER_LIMIT),
        "trend_by_erp_status": stacked_trend(rows, date_of, _erp, start, end, limit=TREND_STATUS_LIMIT),
        "ageing": hist,
        "courier_by_age": crosstab(rows, lambda r: r["courier_partner"], age_labels, _age_bucket),
        "return_name_by_age": crosstab(rows, lambda r: r["return_name"], age_labels, _age_bucket),
        "checks": {
            "long_pending_days": LONG_PENDING_DAYS,
            "erp_completed": len(erp_done),
            "erp_completed_oldest": [{"awb": r["awb"], "days": _age(r), "courier": r["courier_partner"]}
                                     for r in erp_done[:CHECK_LIST_SIZE]],
            "long_pending_no_status": sum(1 for r in long_pending if not r.get("erp_status")),
            "has_delivery_date": sum(1 for r in rows if r.get("rto_delivery_date")),
        },
    }
