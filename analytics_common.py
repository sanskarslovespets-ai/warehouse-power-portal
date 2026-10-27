"""Building blocks shared by every RTO Analytics tab (RTO Process, Status, MDND, Reconciliation).

All of them aggregate in Python over rows that are already in memory (the entries store or the
refresh-job caches), so nothing here touches a database. Dates are 'YYYY-MM-DD' strings
(a longer 'YYYY-MM-DD HH:MM:SS' value is read by its first 10 characters).
"""

from collections import Counter
from datetime import date, datetime, timedelta
from statistics import median

OTHER_LABEL = "Other"
BLANK_LABEL = "(blank)"


# ------------------------------------------------------------------ dates and buckets

def parse_date(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def day_of(value):
    """The date part of a 'YYYY-MM-DD[ HH:MM:SS]' string, or None when blank / not a date."""
    if not value:
        return None
    try:
        return parse_date(str(value)[:10])
    except ValueError:
        return None


def bucket_key(d, granularity):
    if granularity == "day":
        return d.isoformat()
    if granularity == "week":
        return (d - timedelta(days=d.weekday())).isoformat()  # bucket labeled by its Monday
    return d.replace(day=1).isoformat()  # month


def next_bucket(d, granularity):
    if granularity == "day":
        return d + timedelta(days=1)
    if granularity == "week":
        return d + timedelta(days=7)
    return (d.replace(day=1) + timedelta(days=32)).replace(day=1)


def pick_granularity(start, end):
    if not start or not end:
        return "month"
    span_days = (end - start).days
    if span_days <= 60:
        return "day"
    if span_days <= 730:
        return "week"
    return "month"


def fill_buckets(keys, granularity):
    """Every bucket from the first to the last key, so quiet periods show as zero, not as a gap."""
    if not keys:
        return []
    first, last = parse_date(min(keys)), parse_date(max(keys))
    out, cursor = [], first
    while cursor <= last:
        out.append(cursor.isoformat())
        cursor = next_bucket(cursor, granularity)
    return out


def in_date_range(rows, date_of, start_str, end_str):
    """Rows whose date falls within [start, end] (either bound optional). With a bound set,
    rows that have no date drop out; with none, every row stays."""
    if not (start_str or end_str):
        return list(rows)
    out = []
    for r in rows:
        d = date_of(r)
        if not d:
            continue
        day = d.isoformat()
        if (start_str and day < start_str) or (end_str and day > end_str):
            continue
        out.append(r)
    return out


# ------------------------------------------------------------------ counting

def sorted_counts(counter, blank=BLANK_LABEL):
    """Full breakdown, most-common first - uncapped; the frontend caps/Others it for
    the chart and uses the same complete list for the table view."""
    return [{"label": label or blank, "count": count} for label, count in counter.most_common()]


def breakdown(rows, key_of, blank=BLANK_LABEL):
    return sorted_counts(Counter(key_of(r) for r in rows), blank)


def stacked_trend(rows, date_of, key_of, start, end, series=None, limit=7, blank=BLANK_LABEL):
    """Per-period counts split by a category: {"granularity", "series": [...], "points": [...]}.

    `series` fixes the categories and their order (a category not listed is ignored). Without it
    the `limit` biggest categories are kept and the rest are folded into "Other". Every point is
    {"bucket", "total", "values": {category: n}}, and quiet periods are present with zeros.
    """
    granularity = pick_granularity(start, end)
    labelled = [(r, key_of(r) or blank) for r in rows]
    if series is None:
        totals = Counter(label for _, label in labelled)
        series = [label for label, _ in totals.most_common(limit)]
        if len(totals) > limit:
            series.append(OTHER_LABEL)
        fold = True
    else:
        fold = False
    kept = set(series)

    per_bucket = {}
    for r, label in labelled:
        d = date_of(r)
        if not d:
            continue
        if label not in kept:
            if not fold:
                continue
            label = OTHER_LABEL
        per_bucket.setdefault(bucket_key(d, granularity), Counter())[label] += 1
    return {
        "granularity": granularity,
        "series": series,
        "points": [
            {"bucket": k, "total": sum(per_bucket.get(k, Counter()).values()),
             "values": {name: per_bucket.get(k, Counter()).get(name, 0) for name in series}}
            for k in fill_buckets(per_bucket.keys(), granularity)
        ],
    }


def crosstab(rows, row_key_of, columns, column_of, blank=BLANK_LABEL):
    """One row per row-category with its count in each of `columns`, biggest first:
    {"columns": [...], "rows": [{"label", "total", "values": {column: n}}]}."""
    table = {}
    for r in rows:
        label = row_key_of(r) or blank
        row = table.setdefault(label, {"label": label, "total": 0, "values": {c: 0 for c in columns}})
        row["total"] += 1
        col = column_of(r)
        if col in row["values"]:
            row["values"][col] += 1
    return {"columns": list(columns), "rows": sorted(table.values(), key=lambda r: (-r["total"], r["label"]))}


def histogram(values, buckets):
    """Counts of `values` (whole days) in labelled ranges [(label, low, high-or-None)], plus
    average / median / maximum. Values below every range are not counted in a bucket."""
    out = [{"label": label, "count": sum(1 for v in values if v >= low and (high is None or v <= high))}
           for label, low, high in buckets]
    return {
        "n": len(values),
        "avg": round(sum(values) / len(values), 1) if values else None,
        "median": median(values) if values else None,
        "max": max(values) if values else None,
        "buckets": out,
    }


def days_between(earlier, later):
    """Whole days from `earlier` to `later` (date strings), None when either is missing."""
    a, b = day_of(earlier), day_of(later)
    return None if a is None or b is None else (b - a).days


def age_in_days(value, today):
    d = day_of(value)
    return None if d is None else (today - d).days


def today_date(today=None):
    if isinstance(today, date):
        return today
    return date.today()
