"""RTO Analytics > Status / MDND / Reconciliation: the summaries built from the cached rows and their API routes."""
import os
import sys
import types
from datetime import date

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TODAY = date(2026, 10, 5)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """entries_store on a temp SQLite file; the three caches are replaced by whatever a test sets."""
    import analytics_mdnd
    import analytics_recon
    import analytics_status
    import entries_store
    import rto_delivered
    import rto_mdnd
    import rto_recon

    monkeypatch.setattr(entries_store, "DB_PATH", str(tmp_path / "t.db"))
    entries_store.init_db()
    state = {"loading": False, "fetched_at": 1.0, "refreshing": False, "error": None}
    holders = {}

    def serve(module, rows, **over):
        holders[module.__name__] = {**state, "rows": rows, **over}
        monkeypatch.setattr(module, "snapshot", lambda m=module: holders[m.__name__])

    return types.SimpleNamespace(serve=serve, store=entries_store, status=analytics_status, mdnd=analytics_mdnd,
                                 recon=analytics_recon, delivered=rto_delivered, mdnd_cache=rto_mdnd, recon_cache=rto_recon)


# ------------------------------------------------------------------ shared helpers

def test_stacked_trend_keeps_top_series_folds_the_rest_and_fills_quiet_days():
    from analytics_common import day_of, parse_date, stacked_trend

    rows = [{"d": "2026-10-01", "k": "a"}] * 3 + [{"d": "2026-10-01", "k": "b"}] * 2 + [{"d": "2026-10-04", "k": "c"}, {"d": "2026-10-04", "k": ""}]
    t = stacked_trend(rows, lambda r: day_of(r["d"]), lambda r: r["k"], parse_date("2026-10-01"), parse_date("2026-10-04"), limit=2)
    assert t["granularity"] == "day" and t["series"] == ["a", "b", "Other"]
    assert [p["bucket"] for p in t["points"]] == ["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"]
    assert t["points"][0]["values"] == {"a": 3, "b": 2, "Other": 0} and t["points"][1]["total"] == 0
    assert t["points"][3]["values"]["Other"] == 2  # "c" and the blank row

    fixed = stacked_trend(rows, lambda r: day_of(r["d"]), lambda r: r["k"], None, None, series=["a", "c"])
    assert fixed["granularity"] == "month" and fixed["series"] == ["a", "c"] and fixed["points"][0]["values"] == {"a": 3, "c": 1}


def test_histogram_and_crosstab():
    from analytics_common import crosstab, histogram

    h = histogram([0, 1, 1, 5, 40], [("0-1", 0, 1), ("2-5", 2, 5), ("6+", 6, None)])
    assert [b["count"] for b in h["buckets"]] == [3, 1, 1] and h["n"] == 5 and h["max"] == 40 and h["median"] == 1
    assert histogram([], [("x", 0, None)])["avg"] is None
    x = crosstab([{"a": "p", "b": "u"}, {"a": "p", "b": "v"}, {"a": "q", "b": "u"}, {"a": "", "b": "zzz"}],
                 lambda r: r["a"], ["u", "v"], lambda r: r["b"])
    assert x["rows"][0] == {"label": "p", "total": 2, "values": {"u": 1, "v": 1}}
    assert {r["label"] for r in x["rows"]} == {"p", "q", "(blank)"}


# ------------------------------------------------------------------ RTO Status

def delivered(awb, courier="Bluedart", name="WH_Delhi_002", on="2026-10-01", status="Return Completed", picked="2026-09-25",
              city="Gurugram", remark="Intact"):
    return {"awb": awb, "courier_partner": courier, "return_name": name, "rto_delivery_date": f"{on} 10:00:00",
            "created_at": "2026-09-24 10:00:00", "pickup_date": f"{picked} 10:00:00" if picked else "",
            "latest_remark": remark, "return_city": city, "status": status}


def test_status_summary_counts_filters_and_checks(env):
    env.serve(env.delivered, [
        delivered("A1"),
        delivered("A2", courier="Delhivery", status="Return Initiated", on="2026-09-20"),
        delivered("A3", courier="Delhivery", status="", on="2026-09-28", name="MWH_Blr_Neelmangla_002", city="Bangalore"),
        delivered("A4", status="Cancel", picked=""),
    ])
    s = env.status.build_summary("rto_delivery_date", None, None, today=TODAY)
    assert s["total"] == 4 and s["kpis"]["completed"] == 1 and s["kpis"]["completed_rate"] == 25.0
    assert s["kpis"]["no_status"] == 1 and s["kpis"]["not_completed"] == 3 and s["kpis"]["couriers"] == 2
    assert {b["label"] for b in s["by_status"]} == {"Return Completed", "Return Initiated", "(No status)", "Cancel"}
    # pickup -> delivered: A1 6 days, A3 3 days; A2 was "delivered" before its pickup date (left out, counted as negative); A4 has no pickup date
    assert s["kpis"]["avg_transit_days"] == 4.5 and s["transit"]["n"] == 2 and s["transit"]["negative"] == 1
    # delivered 7+ days ago and not completed: A2 (15 days), A3 (7 days); A4 (4 days) is too recent
    assert s["checks"]["stuck"] == 2 and [r["awb"] for r in s["checks"]["oldest"]] == ["A2", "A3"]
    assert s["checks"]["stuck_no_note"] == 1
    assert s["courier_by_status"]["rows"][0]["label"] in {"Bluedart", "Delhivery"}

    only = env.status.build_summary("rto_delivery_date", None, None, status="(No status)", today=TODAY)
    assert only["total"] == 1 and only["filters"]["status"] == "(No status)"
    assert only["options"]["statuses"] == [x["label"] for x in env.status.build_summary("rto_delivery_date", None, None)["by_status"]]  # pick-list stays full
    assert env.status.build_summary("rto_delivery_date", None, None, courier="Delhivery", return_name="MWH_Blr_Neelmangla_002")["total"] == 1


def test_status_date_range_basis_and_trend(env):
    env.serve(env.delivered, [delivered("A1", on="2026-10-01"), delivered("A2", on="2026-10-03"), delivered("A3", on="2026-08-01")])
    s = env.status.build_summary("rto_delivery_date", "2026-10-01", "2026-10-05")
    assert s["total"] == 2 and s["granularity"] == "day"
    assert [p["bucket"] for p in s["trend_by_status"]["points"]] == ["2026-10-01", "2026-10-02", "2026-10-03"]
    assert sum(p["total"] for p in s["trend_by_return_name"]["points"]) == 2
    by_pickup = env.status.build_summary("pickup_date", "2026-09-25", "2026-09-25")
    assert by_pickup["total"] == 3
    with pytest.raises(ValueError):
        env.status.build_summary("nope", None, None)


def test_loading_state_is_reported_not_computed(env):
    env.serve(env.delivered, [], loading=True, fetched_at=None)
    env.serve(env.mdnd_cache, [], loading=True, fetched_at=None)
    env.serve(env.recon_cache, [], loading=True, fetched_at=None)
    for mod, field in ((env.status, "rto_delivery_date"), (env.mdnd, "received_on"), (env.recon, "rto_mark_date")):
        s = mod.build_summary(field, None, None)
        assert s["loading"] is True and s["available"] is False and s["total"] == 0


# ------------------------------------------------------------------ RTO MDND

def mdnd_row(awb, tab="Gurgaon", on="2026-10-01", net=1000, courier="Bluedart", reason="Customer unavailable", days="3"):
    return {"awb": awb, "received_on": on, "marked_on": "2026-09-28", "days_to_receipt": days, "courier": courier,
            "warehouse": "MWH", "order_id": "O" + awb, "drop_city": "Delhi", "rto_reason": reason, "tab": tab, "net_value": net}


def checked(env, awb, got="Yes", cond="Good", by="Anita", notes=""):
    env.store.save_mdnd_input(awb, {"received_at_wh": got, "received_date": "2026-10-02", "condition": cond,
                                    "discrepancy_notes": notes, "checked_by": by, "date_updated": "2026-10-02"})


def test_mdnd_summary_value_checked_and_scorecard(env):
    env.serve(env.mdnd_cache, [
        mdnd_row("M1", net=500), mdnd_row("M2", net=1500, tab="Pune"), mdnd_row("M3", net=2000, tab="Pune", on="2026-09-20", reason=""),
        mdnd_row("M4", net=100, courier="Delhivery"),
    ])
    checked(env, "M1")
    checked(env, "M2", got="Received and Damaged", cond="Damaged", notes="box open")
    s = env.mdnd.build_summary("received_on", None, None, today=TODAY)
    k = s["kpis"]
    assert s["total"] == 4 and k["net_value"] == 4100 and k["checked"] == 2 and k["checked_rate"] == 50.0
    assert k["pending"] == 2 and k["pending_value"] == 2100 and k["damaged"] == 1
    pune = next(r for r in s["scorecard"] if r["label"] == "Pune")
    assert pune == {"label": "Pune", "count": 2, "checked": 1, "pending": 1, "checked_rate": 50.0, "net_value": 3500, "pending_value": 2000}
    assert s["by_received_at_wh"] and {b["label"] for b in s["by_received_at_wh"]} == {"Yes", "Received and Damaged", "(Not filled)"}
    assert s["by_checker"] == [{"label": "Anita", "count": 2}]
    assert {b["label"] for b in s["by_reason"]} == {"Customer unavailable", "(no reason)"}
    # overdue = pending and received 7+ days ago: only M3 (15 days). M4 (4 days) is recent, M1/M2 are checked.
    assert s["checks"]["overdue"] == 1 and s["checks"]["overdue_value"] == 2000 and s["checks"]["oldest_pending"][0]["awb"] == "M3"
    assert s["checks"]["with_notes"] == 1
    assert [p["values"] for p in s["trend_by_state"]["points"] if p["total"]][0] == {"Checked": 0, "Pending": 1}  # 2026-09-20: M3 pending


def test_mdnd_filters_and_auto_filled_entries(env):
    env.serve(env.mdnd_cache, [mdnd_row("M1"), mdnd_row("M2", tab="Pune"), mdnd_row("M3", tab="Pune", reason="")])
    env.store.add_entry({"damage_marked_date": "2026-10-01", "received_date": "2026-10-02", "facility_name": "Pune",
                         "damage_type": "RTO", "awb_code": "M2", "sku_code": "S", "pod_link": "p", "video_link": "v",
                         "courier_name": "Bluedart", "remarks": ""})
    s = env.mdnd.build_summary("received_on", None, None, state="checked")
    assert s["total"] == 1 and s["kpis"]["damaged"] == 1  # M2 is auto-filled "Received and Damaged" from the RTO Process entry
    assert env.mdnd.build_summary("received_on", None, None, state="pending")["total"] == 2
    assert env.mdnd.build_summary("received_on", None, None, warehouse="Pune")["total"] == 2
    assert env.mdnd.build_summary("received_on", None, None, reason="(no reason)")["total"] == 1
    full = env.mdnd.build_summary("received_on", None, None, warehouse="Pune")
    assert full["options"]["warehouses"] == ["Gurgaon", "Pune"]  # pick-lists stay full
    with pytest.raises(ValueError):
        env.mdnd.build_summary("received_on", None, None, state="bogus")


# ------------------------------------------------------------------ RTO Reconciliation

def recon_row(awb, days, courier="Delhivery", name="WH_Delhi_002", erp="Return Completed", mark="2026-01-01", delivered_on=""):
    return {"clickpost_unified_status": "RTO-InTransit", "courier_partner": courier, "awb": awb, "return_name": name,
            "created_at": "2025-12-25 10:00:00", "pickup_date": "2025-12-25 12:00:00", "reference_number": "R" + awb,
            "pickup_pincode": "122017", "drop_pincode": "110001", "rto_mark_date": f"{mark} 10:00:00",
            "rto_delivery_date": delivered_on, "rto_date_diff": str(days), "erp_status": erp}


def test_recon_summary_ageing_and_disagreements(env):
    env.serve(env.recon_cache, [
        recon_row("R1", 12), recon_row("R2", 45, erp="Return Initiated"), recon_row("R3", 200, courier="Ekart", erp=""),
        recon_row("R4", 95, name="MWH_Mum_Bhiwandi_002", delivered_on="2026-09-01 10:00:00"),
    ])
    s = env.recon.build_summary("rto_mark_date", None, None)
    k = s["kpis"]
    assert s["total"] == 4 and k["oldest_days"] == 200 and k["long_pending"] == 3 and k["long_pending_rate"] == 75.0
    assert k["erp_completed"] == 2 and k["no_erp_status"] == 1
    assert [b["count"] for b in s["ageing"]["buckets"]] == [1, 0, 1, 0, 1, 1]  # 10-14, 15-29, 30-59, 60-89, 90-179, 180+
    assert [r["awb"] for r in s["checks"]["erp_completed_oldest"]] == ["R4", "R1"]
    assert s["checks"]["long_pending_no_status"] == 1 and s["checks"]["has_delivery_date"] == 1
    assert s["courier_by_age"]["columns"][0] == "10-14 days"
    assert env.recon.build_summary("rto_mark_date", None, None, erp_status="(No status)")["total"] == 1
    assert env.recon.build_summary("rto_mark_date", None, None, courier="Ekart", return_name="WH_Delhi_002")["total"] == 1
    assert env.recon.build_summary("rto_mark_date", "2026-02-01", "2026-02-28")["total"] == 0


# ------------------------------------------------------------------ routes

@pytest.fixture()
def client(env):
    import app as panel
    env.serve(env.delivered, [delivered("A1")])
    env.serve(env.mdnd_cache, [mdnd_row("M1")])
    env.serve(env.recon_cache, [recon_row("R1", 20)])
    return panel.app.test_client()


def test_routes_return_summaries(client):
    s = client.get("/api/analytics/status?from=2026-09-01&to=2026-10-31").get_json()
    assert s["total"] == 1 and s["filters"] == {"return_name": None, "courier": None, "status": None}
    assert client.get("/api/analytics/status?courier=Bluedart&status=Return%20Completed").get_json()["total"] == 1
    assert client.get("/api/analytics/status?courier=Nobody").get_json()["total"] == 0
    m = client.get("/api/analytics/mdnd?warehouse=Gurgaon&state=pending").get_json()
    assert m["total"] == 1 and m["filters"]["warehouse"] == "Gurgaon" and m["filters"]["state"] == "pending"
    r = client.get("/api/analytics/recon?date_field=rto_mark_date").get_json()
    assert r["total"] == 1 and r["date_field"] == "rto_mark_date"


@pytest.mark.parametrize("url", [
    "/api/analytics/status?date_field=bogus",
    "/api/analytics/status?from=not-a-date",
    "/api/analytics/mdnd?to=2026-13-40",
    "/api/analytics/mdnd?warehouse=Atlantis",
    "/api/analytics/mdnd?state=maybe",
    "/api/analytics/recon?date_field=received_on",
])
def test_routes_reject_bad_input(client, url):
    resp = client.get(url)
    assert resp.status_code == 400 and "error" in resp.get_json()
