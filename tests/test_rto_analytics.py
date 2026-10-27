"""RTO Analytics > RTO Process: the summary built from logged entries (analytics.py) and its API route."""
import importlib
import os
import sys
import types
from datetime import date, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TODAY = date(2026, 10, 5)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """entries_store on a temp SQLite file, with the DB-backed caches switched off."""
    import entries_store

    monkeypatch.setattr(entries_store, "DB_PATH", str(tmp_path / "t.db"))
    entries_store.init_db()
    import analytics

    importlib.reload(analytics)

    def add(marked, received=None, facility="Gurgaon", dtype="RTO", awb="A1", sku="S1", courier="Delhivery", synced=True):
        e = entries_store.add_entry({
            "damage_marked_date": marked, "received_date": received or marked, "facility_name": facility,
            "damage_type": dtype, "awb_code": awb, "sku_code": sku, "pod_link": "p", "video_link": "v",
            "courier_name": courier, "remarks": "",
        })
        if synced:
            entries_store.mark_synced([e["id"]])
        return e

    # no RTO Delivered data unless a test provides some
    fake = types.SimpleNamespace(query=lambda **kw: {"loading": True}, all_filtered=lambda **kw: [])
    monkeypatch.setitem(sys.modules, "rto_delivered", fake)
    return types.SimpleNamespace(add=add, analytics=analytics, store=entries_store, monkeypatch=monkeypatch)


def summary(env, **kw):
    args = {"date_field": "damage_marked_date", "start_str": None, "end_str": None, **kw}
    return env.analytics.build_summary(args.pop("date_field"), args.pop("start_str"), args.pop("end_str"), **args)


def test_totals_distincts_and_breakdowns(env):
    env.add("2026-10-01", awb="A1", sku="S1", facility="Gurgaon", dtype="RTO", courier="Delhivery")
    env.add("2026-10-01", awb="A2", sku="S1", facility="Pune", dtype="RPU", courier="Ecom")
    env.add("2026-10-02", awb="A3", sku="S2", facility="Pune", dtype="RTO", courier="Ecom")
    s = summary(env)
    assert s["total"] == 3 and s["distinct"] == {"awb": 3, "sku": 2, "facility": 2, "courier": 2}
    assert s["by_damage_type"] == {"RTO": 2, "RPU": 1, "WH Damage": 0, "Inward Damage": 0}
    assert s["by_facility"][0] == {"label": "Pune", "count": 2} and s["by_sku"][0] == {"label": "S1", "count": 2}
    assert s["facility_by_type"][0] == {"label": "Pune", "total": 2, "types": {"RTO": 1, "RPU": 1, "WH Damage": 0, "Inward Damage": 0}}
    assert s["courier_by_type"][0]["label"] == "Ecom"
    assert sum(p["count"] for p in s["trend"]) == 3 and s["sheet_sync"] == {"synced": 3, "pending": 0}


def test_filters_narrow_everything_but_not_the_option_lists(env):
    env.add("2026-10-01", facility="Gurgaon", dtype="RTO", courier="Delhivery", awb="A1")
    env.add("2026-10-01", facility="Pune", dtype="RPU", courier="Ecom", awb="A2")
    env.add("2026-10-02", facility="Pune", dtype="RTO", courier="Ecom", awb="A3")
    both = summary(env, facility="Pune", damage_type="RTO")
    assert both["total"] == 1 and both["distinct"]["awb"] == 1
    assert both["options"] == {"facilities": ["Gurgaon", "Pune"], "couriers": ["Delhivery", "Ecom"]}  # pick-lists stay full
    assert summary(env, courier="Ecom")["total"] == 2 and summary(env, damage_type="WH Damage")["total"] == 0
    assert summary(env, facility="Pune")["filters"] == {"damage_type": None, "facility": "Pune", "courier": None}


def test_date_range_and_basis(env):
    env.add("2026-09-01", received="2026-10-01", awb="A1")
    env.add("2026-10-01", received="2026-10-01", awb="A2")
    assert summary(env, start_str="2026-10-01", end_str="2026-10-31")["total"] == 1
    assert summary(env, date_field="received_date", start_str="2026-10-01", end_str="2026-10-31")["total"] == 2


def test_trend_fills_quiet_days_and_picks_granularity(env):
    env.add("2026-10-01", dtype="RTO")
    env.add("2026-10-04", dtype="RPU")
    s = summary(env, start_str="2026-10-01", end_str="2026-10-05")
    assert s["granularity"] == "day"
    assert [p["bucket"] for p in s["trend"]] == ["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"]
    assert [p["count"] for p in s["trend"]] == [1, 0, 0, 1]  # quiet days show as zero, not a gap
    assert s["trend_by_type"][3]["types"] == {"RTO": 0, "RPU": 1, "WH Damage": 0, "Inward Damage": 0}
    assert summary(env, start_str="2026-01-01", end_str="2026-06-30")["granularity"] == "week"
    assert summary(env, start_str="2024-01-01", end_str="2026-06-30")["granularity"] == "month"
    assert summary(env)["granularity"] == "month"  # "all time"


def test_trend_by_facility_uses_the_same_periods_and_folds_small_facilities_into_other(env):
    # 9 facilities: F1 has the most entries, F9 the fewest. Only the top 7 get their own series.
    for i in range(1, 10):
        for n in range(10 - i):
            env.add("2026-10-01" if n % 2 == 0 else "2026-10-04", facility=f"F{i}", awb=f"A{i}-{n}")
    s = summary(env, start_str="2026-10-01", end_str="2026-10-05")
    fac = s["trend_by_facility"]
    assert fac["series"] == ["F1", "F2", "F3", "F4", "F5", "F6", "F7", "Other"]  # biggest first, rest folded
    assert [p["bucket"] for p in fac["points"]] == [p["bucket"] for p in s["trend_by_type"]]  # same filled periods
    quiet = next(p for p in fac["points"] if p["bucket"] == "2026-10-02")
    assert quiet["total"] == 0 and set(quiet["values"].values()) == {0}
    for point, by_type in zip(fac["points"], s["trend_by_type"]):  # both charts agree on every period's total
        assert point["total"] == by_type["total"] == sum(point["values"].values())
    assert sum(p["values"]["Other"] for p in fac["points"]) == 2 + 1  # F8 (2 entries) + F9 (1 entry)


def test_trend_by_facility_follows_the_filters_and_handles_few_or_blank_facilities(env):
    env.add("2026-10-01", facility="Pune", dtype="RTO", awb="A1")
    env.add("2026-10-01", facility="Gurgaon", dtype="RPU", awb="A2")
    env.add("2026-10-02", facility="", dtype="RPU", awb="A3")
    s = summary(env)
    assert sorted(s["trend_by_facility"]["series"]) == ["(blank)", "Gurgaon", "Pune"]  # no "Other" when 7 or fewer
    only = summary(env, damage_type="RTO")["trend_by_facility"]
    assert only["series"] == ["Pune"] and [p["total"] for p in only["points"]] == [1]
    assert summary(env, facility="Gurgaon")["trend_by_facility"]["series"] == ["Gurgaon"]
    empty = summary(env, damage_type="WH Damage")["trend_by_facility"]
    assert empty == {"series": [], "points": []}


def test_month_buckets_cross_a_year_end(env):
    env.add("2025-11-15", awb="A1")
    env.add("2026-02-03", awb="A2")
    s = summary(env)
    assert [p["bucket"] for p in s["trend"]] == ["2025-11-01", "2025-12-01", "2026-01-01", "2026-02-01"]


def test_receipt_lag_stats_and_buckets(env):
    for i, lag in enumerate([0, 1, 2, 4, 8, 20]):
        d = TODAY - timedelta(days=30)
        env.add(d.isoformat(), (d + timedelta(days=lag)).isoformat(), awb=f"L{i}")
    env.add("2026-10-03", "2026-10-01", awb="BAD")  # received before it was marked
    lag = summary(env)["lag"]
    assert lag["n"] == 6 and lag["received_before_marked"] == 1  # the bad row is flagged, not averaged in
    assert lag["avg_days"] == 5.8 and lag["median_days"] == 3.0 and lag["max_days"] == 20
    assert {b["label"]: b["count"] for b in lag["buckets"]} == {
        "Same day": 1, "1 day": 1, "2 days": 1, "3-5 days": 1, "6-10 days": 1, "11+ days": 1}


def test_lag_is_empty_not_an_error_when_nothing_qualifies(env):
    assert summary(env)["lag"]["avg_days"] is None
    env.add("2026-10-03", "2026-10-01")
    lag = summary(env)["lag"]
    assert lag["n"] == 0 and lag["avg_days"] is None and lag["median_days"] is None and lag["received_before_marked"] == 1


def test_repeat_awbs(env):
    for _ in range(3):
        env.add("2026-10-01", awb="DUP")
    env.add("2026-10-01", awb="TWICE")
    env.add("2026-10-02", awb="TWICE")
    env.add("2026-10-02", awb="ONCE")
    r = summary(env)["repeat_awbs"]
    assert r["count"] == 2 and r["top"] == [{"awb": "DUP", "count": 3}, {"awb": "TWICE", "count": 2}]
    assert summary(env)["distinct"]["awb"] == 3


def test_coverage_is_matched_by_awb_against_the_rto_delivered_cache(env):
    env.add("2026-10-01", awb="D1", dtype="WH Damage")  # any damage type counts as logged
    env.add("2026-10-01", awb="D2", dtype="RTO")
    rows = [
        {"awb": "D1", "courier_partner": "Delhivery", "return_name": "Gurgaon"},
        {"awb": "D2", "courier_partner": "Ecom", "return_name": "Pune"},
        {"awb": "D3", "courier_partner": "Ecom", "return_name": "Pune"},
        {"awb": "D4", "courier_partner": "Ecom", "return_name": "Pune"},
    ]
    seen = {}

    def all_filtered(**kw):
        seen.update(kw)
        return [r for r in rows if not kw.get("return_name") or r["return_name"] == kw["return_name"]]

    fake = types.SimpleNamespace(query=lambda **kw: {"loading": False}, all_filtered=all_filtered)
    env.monkeypatch.setitem(sys.modules, "rto_delivered", fake)

    cov = summary(env, start_str="2026-10-01", end_str="2026-10-31")["coverage"]
    assert seen["date_from"] == "2026-10-01" and seen["date_to"] == "2026-10-31"
    assert cov["available"] and cov["delivered"] == 4 and cov["logged"] == 2 and cov["rate"] == 50.0
    pune = next(r for r in cov["by_facility"] if r["label"] == "Pune")
    assert (pune["delivered"], pune["logged"], pune["rate"]) == (3, 1, 33.3)
    assert [r["label"] for r in cov["by_courier"]] == ["Ecom", "Delhivery"]

    only_ecom = summary(env, courier="Ecom")["coverage"]  # the courier filter applies, the damage type one does not
    assert only_ecom["delivered"] == 3 and only_ecom["logged"] == 1
    assert summary(env, damage_type="RPU")["coverage"]["logged"] == 2


def test_coverage_reports_loading_and_unavailable_without_failing(env):
    assert summary(env)["coverage"] == {"available": False, "loading": True, "delivered": 0, "logged": 0, "rate": None,
                                        "by_facility": [], "by_courier": []}

    def boom(**kw):
        raise RuntimeError("cache broken")

    env.monkeypatch.setitem(sys.modules, "rto_delivered", types.SimpleNamespace(query=boom, all_filtered=boom))
    cov = summary(env)["coverage"]
    assert cov["available"] is False and cov["loading"] is False


def test_empty_database_gives_an_empty_but_complete_summary(env):
    s = summary(env)
    assert s["total"] == 0 and s["trend"] == [] and s["trend_by_type"] == [] and s["by_facility"] == []
    assert s["repeat_awbs"] == {"count": 0, "top": []} and s["options"] == {"facilities": [], "couriers": []}


# ------------------------------------------------------------------ the route

@pytest.fixture()
def client(env, monkeypatch):
    monkeypatch.setenv("SCM_DB_HOST", "blocked")
    monkeypatch.setitem(sys.modules, "dropdown_cache", types.SimpleNamespace(
        refresh_cache_async=lambda *a, **k: None, SMALL_FIELDS=[], LARGE_FIELDS=[], get_cached_options=lambda **k: {},
        search_cached=lambda *a, **k: []))
    if "main" in sys.modules:
        del sys.modules["main"]
    import auth
    import main
    from fastapi.testclient import TestClient

    monkeypatch.setattr(main.dropdown_cache, "refresh_cache_async", lambda: None)
    # a local run answers loopback only, and only once signed in
    return TestClient(main.app, client=("127.0.0.1", 50000), cookies={auth.COOKIE: auth.issue("tester")})


def test_download_holds_the_entries_behind_the_dashboard(env, client):
    env.add("2026-10-01", facility="Pune", dtype="RPU", courier="Ecom", awb="A1")
    env.add("2026-10-02", facility="Gurgaon", dtype="RTO", awb="=HYPERLINK(1)")
    resp = client.get("/api/analytics/export?damage_type=RTO")
    assert resp.status_code == 200 and "filename=rto_process_analytics_" in resp.headers["content-disposition"]
    lines = resp.text.lstrip("﻿").splitlines()
    assert lines[0] == ",".join(env.store.EXPORT_COLUMNS)
    assert len(lines) == 2 and ",'=HYPERLINK(1)," in lines[1]  # kept as text, not run as a formula
    assert client.get("/api/analytics/export?damage_type=Nope").status_code == 400


def test_route_passes_filters_through_and_validates_them(env, client):
    env.add("2026-10-01", facility="Pune", dtype="RPU", courier="Ecom")
    env.add("2026-10-01", facility="Gurgaon", dtype="RTO", courier="Delhivery", awb="A2")
    ok = client.get("/api/analytics?date_field=damage_marked_date&damage_type=RPU&facility=Pune&courier=Ecom").json()
    assert ok["total"] == 1 and ok["filters"] == {"damage_type": "RPU", "facility": "Pune", "courier": "Ecom"}
    assert client.get("/api/analytics?damage_type=Nope").status_code == 400
    assert client.get("/api/analytics?date_field=remarks").status_code == 400
    assert client.get("/api/analytics?from=2026-13-40").status_code == 400
    assert client.get("/api/analytics").json()["total"] == 2  # all filters are optional
