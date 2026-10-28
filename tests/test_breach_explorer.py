"""Breach Explorer (breach_explorer.py and its routes in main.py): running the exports (each tab's Refresh data
too), serving each tab's data, and who may see which. The exports are stand-ins - tiny scripts that write the JSON shape the page reads (or
fail) - in the temporary folder conftest.py's `breach` fixture points the module at."""
import gzip
import json
import textwrap
import time
import types

import pytest
from fastapi.testclient import TestClient

LOCAL = ("127.0.0.1", 50000)

GOOD = {  # the smallest output of each export that the panel will serve
    "breach": {"dims": {"date": ["2026-10-20"]}, "facts": [[0] * 31]},
    "pendency": {"generated_at_ist": "2026-10-27 09:01", "dims": {}, "facts": [[0] * 11]},
    "pendency-detail": {"columns": ["awb"], "rows": [["A1"]]},
    "connection": {"dims": {}, "facts": [[0] * 13]},
    "radofd": {"by_city": {"dims": {}, "facts": [[0] * 11]}, "by_hub": {"dims": {}, "facts": []}},
    "complaints": {"dims": {}, "facts": [[0] * 10]},
    "rto": {"dims": {}, "facts": [[0] * 17]},
}
ERP_DOWN = "pymysql.err.OperationalError: (2003, \"Can't connect to MySQL server on 'erp'\")"


def write_exports(be, **outputs):
    """Stand-in export scripts. Each writes GOOD[dataset], or outputs[dataset]: a dict to write instead, or a
    str to fail with (printed to stderr, as a real traceback's last line is)."""
    scripts = be.PIPELINE / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    for name, (script, output, _tab) in be.DATASETS.items():
        result = outputs.get(name.replace("-", "_"), GOOD[name])
        if isinstance(result, str):
            body = f"import sys\nsys.exit({result!r})\n"
        else:
            body = textwrap.dedent(f"""
                import json, os
                with open(os.path.join(os.path.dirname(__file__), "..", "data", {output!r}), "w") as f:
                    json.dump({result!r}, f)
            """)
        (scripts / script).write_text(body)


def served(be, name):
    found = be.payload(name)
    return json.loads(gzip.decompress(found[0])) if found else None


def restart(be):
    """What a server restart leaves: nothing in memory, the files on disk."""
    be._loaded, be._fetched, be._errors, be._payloads = False, {}, {}, {}


# ------------------------------------------------------------------ running the exports

def test_a_run_serves_every_dataset_and_a_restart_keeps_it(breach):
    write_exports(breach)
    breach.refresh_cache(force=True)

    state = breach.status()
    assert all(d["fetched_at"] and not d["error"] for d in state["datasets"].values())
    assert state["refreshing"] is False and state["running"] is None
    assert {name: served(breach, name) for name in breach.DATASETS} == GOOD
    etag = breach.payload("rto")[1]

    restart(breach)
    assert served(breach, "rto") == GOOD["rto"] and breach.payload("rto")[1] == etag
    assert breach.status()["datasets"] == state["datasets"]


def test_a_failed_export_leaves_its_tab_on_the_previous_data(breach):
    write_exports(breach)
    breach.refresh_cache(force=True)
    before = breach.status()["datasets"]

    newer = {"dims": {"date": ["2026-10-27"]}, "facts": [[1] * 31]}
    write_exports(breach, complaints=ERP_DOWN, breach=newer)
    time.sleep(0.01)
    breach.refresh_cache(force=True)

    after = breach.status()["datasets"]
    assert after["complaints"] == {"fetched_at": before["complaints"]["fetched_at"], "error": ERP_DOWN}
    assert served(breach, "complaints") == GOOD["complaints"]  # the last good data, still served
    assert after["breach"]["fetched_at"] > before["breach"]["fetched_at"] and not after["breach"]["error"]
    assert served(breach, "breach") == newer  # the others moved on
    assert breach.snapshot()["error"] == f"Complaints: {ERP_DOWN}"

    write_exports(breach)
    breach.refresh_cache(force=True)
    assert breach.status()["datasets"]["complaints"]["error"] is None and breach.snapshot()["error"] is None


def test_output_the_page_cant_read_is_not_served(breach):
    write_exports(breach, breach={"dims": {}, "facts": [[0] * 30]}, pendency={"dims": {}, "facts": [[0] * 11]},
                  radofd={"by_city": {"facts": []}, "by_hub": {"facts": [[0] * 11]}}, rto={"dims": {}, "facts": []})
    breach.refresh_cache(force=True)

    errors = {name: d["error"] for name, d in breach.status()["datasets"].items() if d["error"]}
    assert errors == {
        "breach": "export_breach_dashboard_data.py ran, but its rows have 30 columns where the page expects 31",
        "pendency": "export_pendency_data.py ran, but it has no snapshot time",
        "radofd": "export_rad_ofd_data.py ran, but it produced no rows",
        "rto": "export_rto_data.py ran, but it produced no rows",
    }
    assert all(breach.payload(name) is None for name in errors)
    assert served(breach, "connection") == GOOD["connection"]


def test_an_export_that_writes_nothing_new_is_not_served_as_new(breach):
    write_exports(breach)
    breach.refresh_cache(force=True)
    (breach.PIPELINE / "scripts" / "export_connection_data.py").write_text("pass\n")  # exits 0, writes nothing
    breach.refresh_cache(force=True)
    assert "No such file" in breach.status()["datasets"]["connection"]["error"]


def test_only_data_that_missed_its_morning_run_is_exported_unless_forced(breach):
    write_exports(breach)
    breach.refresh_cache(force=True)
    write_exports(breach, **{name.replace("-", "_"): "should not run" for name in breach.DATASETS})

    breach.refresh_cache()  # everything is from after this morning's run: nothing to do
    assert not any(d["error"] for d in breach.status()["datasets"].values())

    breach._fetched["pendency"] = 0  # pendency missed it
    breach.refresh_cache()
    errors = {name for name, d in breach.status()["datasets"].items() if d["error"]}
    assert errors == {"pendency"}


def test_a_missed_morning_run_starts_when_someone_looks_but_not_over_and_over(breach):
    assert breach.status()["datasets"]["breach"] == {"fetched_at": None, "error": None}
    assert breach.started == [False]  # nothing built yet: the page asking starts the exports

    breach._last_attempt_at = time.time()  # ...which just tried
    breach.snapshot()
    assert breach.started == [False]


def test_a_tab_s_refresh_data_re_runs_only_that_tab_s_exports(breach):
    write_exports(breach)
    breach.refresh_cache(force=True)
    before = {name: d["fetched_at"] for name, d in breach.status()["datasets"].items()}
    time.sleep(0.01)

    assert breach.refresh_tab("pendency", cooldown=0) is None
    assert breach.status()["queued"] == ["pendency-detail", "pendency"]  # with the shipments its Export CSV reads
    assert breach.started == [False]  # in the background...
    breach.refresh_cache()  # ...which runs this (nothing else is due)

    after = {name: d["fetched_at"] for name, d in breach.status()["datasets"].items()}
    assert {name for name in after if after[name] != before[name]} == {"pendency-detail", "pendency"}
    assert breach.status()["queued"] == []

    assert 0 <= breach.refresh_tab("rto", cooldown=600) < 5  # its data is seconds old: nothing queued
    assert breach.status()["queued"] == [] and breach.started == [False]


def test_refreshes_asked_for_during_a_run_join_it_and_none_runs_twice(breach, monkeypatch):
    # Before, a run asked for while another was going was dropped: the top bar's Refresh from DB too.
    write_exports(breach)
    breach.refresh_cache(force=True)
    ran = []
    export = breach._export

    def exporting(name):
        ran.append(name)
        if name == "connection":  # while Connection view's Refresh data runs:
            breach.refresh_tab("rto", cooldown=0)  # someone presses RTO's,
            breach.refresh_cache(force=True)  # and someone the top bar's Refresh from DB
            assert breach.status()["running"] == "connection"
        export(name)

    monkeypatch.setattr(breach, "_export", exporting)
    breach.refresh_tab("connection", cooldown=0)
    breach.refresh_cache()
    assert ran == ["connection", "rto", "breach", "pendency-detail", "pendency", "radofd", "complaints"]
    state = breach.status()
    assert state["queued"] == [] and state["running"] is None and not breach.is_refreshing()


# ------------------------------------------------------------------ the routes

@pytest.fixture()
def main(tmp_path, monkeypatch):
    import entries_store

    monkeypatch.setattr(entries_store, "DB_PATH", str(tmp_path / "t.db"))
    import main

    entries_store.init_db()
    return main


def client(main, accounts, username):
    return TestClient(main.app, client=LOCAL, cookies=accounts.cookies(username))


def test_a_tab_gets_its_data_gzipped_and_only_again_once_it_changes(main, accounts, breach):
    admin = client(main, accounts, "tester")
    r = admin.get("/api/breach-explorer/pendency")
    assert r.status_code == 503 and r.json() == {"error": "This tab has no data yet."}

    write_exports(breach)
    breach.refresh_cache(force=True)
    r = admin.get("/api/breach-explorer/pendency")
    assert r.status_code == 200 and r.json() == GOOD["pendency"]
    assert r.headers["content-encoding"] == "gzip" and r.headers["cache-control"] == "no-store"  # never kept on disk

    again = admin.get("/api/breach-explorer/pendency", headers={"If-None-Match": r.headers["etag"]})
    assert again.status_code == 304 and again.content == b""  # the page already has it
    breach.refresh_cache(force=True)
    newer = admin.get("/api/breach-explorer/pendency", headers={"If-None-Match": r.headers["etag"]})
    assert newer.status_code == 200 and newer.headers["etag"] != r.headers["etag"]

    plain = admin.get("/api/breach-explorer/pendency", headers={"Accept-Encoding": "identity"})
    assert plain.json() == GOOD["pendency"] and "content-encoding" not in plain.headers

    assert admin.get("/api/breach-explorer/nope").status_code == 404
    assert admin.get("/api/breach-explorer/status").json()["datasets"]["pendency"]["error"] is None


def test_each_tab_s_data_is_only_for_people_given_that_tab(main, accounts, breach):
    write_exports(breach)
    breach.refresh_cache(force=True)
    accounts.add("pen", "pendency-pass", "viewer", ["be-pendency"])
    accounts.add("rto.only", "rto-only-pass", "editor", ["rto-status"])
    pen, rto_only = client(main, accounts, "pen"), client(main, accounts, "rto.only")

    assert pen.get("/api/me").json()["panels"] == ["be-pendency"]
    assert pen.get("/api/breach-explorer/pendency").status_code == 200
    assert pen.get("/api/breach-explorer/pendency-detail").json() == GOOD["pendency-detail"]  # its Export CSV
    for dataset, tab in (("breach", "Breach analysis"), ("complaints", "Complaints"), ("rto", "RTO"),
                         ("connection", "Connection view"), ("radofd", "RAD to OFD")):
        r = pen.get(f"/api/breach-explorer/{dataset}")
        assert r.status_code == 403 and r.json() == {"error": f"You don't have access to the {tab} tab."}
    assert pen.get("/api/breach-explorer/connection/awbs?from=2026-10-01&to=2026-10-02").status_code == 403

    assert rto_only.get("/api/breach-explorer/pendency").status_code == 403
    assert rto_only.get("/api/breach-explorer/status").status_code == 200  # freshness only, for everyone


def test_refresh_data_is_for_the_tab_s_editors_and_not_again_within_minutes(main, accounts, breach, monkeypatch):
    write_exports(breach)
    breach.refresh_cache(force=True)  # every tab's data came in just now
    accounts.add("pen.ed", "pendency-pass", "editor", ["be-pendency"])
    accounts.add("pen.view", "pendency-pass", "viewer", ["be-pendency"])
    admin, editor, viewer = (client(main, accounts, name) for name in ("tester", "pen.ed", "pen.view"))
    url = "/api/breach-explorer/pendency/refresh"

    r = editor.post(url)
    assert r.status_code == 429
    assert r.json() == {"error": "This tab's data is less than a minute old. You can refresh it again in 10 min."}
    assert breach.status()["queued"] == [] and breach.started == []

    monkeypatch.setattr(main, "FORCE_REFRESH_COOLDOWN_SECONDS", 0)  # as if ten minutes had passed
    r = editor.post(url)
    assert r.status_code == 202 and r.json()["queued"] == ["pendency-detail", "pendency"]  # the status, to follow
    assert breach.started == [False]
    again = editor.post(url)  # already on its way: it carries on, nothing added
    assert again.status_code == 202 and again.json()["queued"] == ["pendency-detail", "pendency"]

    r = viewer.post(url)
    assert r.status_code == 403 and r.json() == {"error": "Your access to the Pendency tab is view-only."}
    r = editor.post("/api/breach-explorer/rto/refresh")
    assert r.status_code == 403 and r.json() == {"error": "You don't have access to the RTO tab."}
    assert admin.post("/api/breach-explorer/pendency-detail/refresh").status_code == 404  # refreshed with Pendency
    assert admin.post("/api/breach-explorer/nope/refresh").status_code == 404


def test_the_shipment_export_checks_its_dates_and_writes_a_safe_csv(main, accounts, breach, monkeypatch):
    calls = []

    def awb_rows(dataset, date_from, date_to, couriers, warehouses, accounts_, slabs):
        calls.append((dataset, date_from, date_to, couriers, warehouses, accounts_, slabs))
        rows = [{"awb": "123", "order_id": "=HYPERLINK(\"x\")", "status": None, "mins": -5}]
        return ["awb", "order_id", "status", "mins"], rows, True

    monkeypatch.setattr(breach, "awb_rows", awb_rows)
    admin = client(main, accounts, "tester")
    url = "/api/breach-explorer/connection/awbs"

    r = admin.get(url + "?from=2026-10-01&to=2026-10-07&courier=Bluedart&warehouse=MWH_X&warehouse=MWH_Y&slab=3%2Bkg")
    assert r.status_code == 200
    assert r.headers["content-disposition"] == "attachment; filename=connection_awbs_2026-10-01_to_2026-10-07.csv"
    assert r.content.decode("utf-8").split("\n") == [
        "﻿awb,order_id,status,mins", '123,"\'=HYPERLINK(""x"")",,-5',
        "TRUNCATED at 200000 rows - narrow the filters", "",
    ]
    assert calls == [("connection", "2026-10-01", "2026-10-07", ["Bluedart"], ["MWH_X", "MWH_Y"], [], ["3+kg"])]

    def error(query):
        r = admin.get(url + query)
        return r.status_code, r.json()["error"]

    assert error("") == (400, "Pick the dates to export first.")
    assert error("?from=2026-10-07&to=2026-10-01") == (400, "The start date is after the end date.")
    assert error("?from=2026-07-01&to=2026-10-01") == (400, "Export at most 92 days of shipments at a time.")
    assert error("?from=2026-10-01&to=oct") == (400, "Invalid 'to' date.")
    assert admin.get("/api/breach-explorer/rto/awbs?from=2026-10-01&to=2026-10-02").status_code == 404

    def raising(exc):
        def awb_rows(*args):
            raise exc
        return awb_rows

    monkeypatch.setattr(breach, "awb_rows", raising(breach.Busy()))
    assert error("?from=2026-10-01&to=2026-10-02")[0] == 429
    monkeypatch.setattr(breach, "awb_rows", raising(OSError("the database went away")))
    assert error("?from=2026-10-01&to=2026-10-02") == (503, "Could not read the shipments from the database. Please try again.")


def test_shipment_export_sql_binds_every_filter_and_keeps_the_tab_s_scope(breach, monkeypatch):
    seen = {}

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            if not sql.startswith("SET SESSION"):
                seen["sql"], seen["params"] = sql, params

        def fetchall(self):
            return [{"awb": str(n)} for n in range(3)]

    conn = types.SimpleNamespace(cursor=lambda *a: Cursor(), close=lambda: None)
    monkeypatch.setattr(breach, "get_scm_connection", lambda read_timeout=None: conn)
    monkeypatch.setattr(breach, "AWB_ROW_CAP", 2)

    cols, rows, truncated = breach.awb_rows("radofd", "2026-10-01", "2026-10-02", ["Bluedart", "Ekart"],
                                            ["x' OR 1=1 --"], [], ["0-3kg", "bogus"])
    assert cols[0] == "awb" and len(rows) == 2 and truncated
    sql = seen["sql"]
    assert "x' OR" not in sql and seen["params"] == ["2026-10-01", "2026-10-02", "Bluedart", "x' OR 1=1 --"]
    assert "courier_partner IN (%s)" in sql  # Ekart isn't in the tab, so it isn't asked for
    assert "LIKE 'MWH!_%%' ESCAPE '!'" in sql and "(shipment_weight IS NOT NULL AND shipment_weight <= 3000)" in sql
    assert sql.endswith("LIMIT 3") and "destination_hub_in_scan_time >= %s" in sql

    for _ in range(2):
        breach._awb_slots.acquire()
    try:
        with pytest.raises(breach.Busy):
            breach.awb_rows("connection", "2026-10-01", "2026-10-02")
    finally:
        for _ in range(2):
            breach._awb_slots.release()


def test_the_top_bar_shows_and_refreshes_only_the_data_behind_your_tabs(main, accounts, monkeypatch):
    caches = {}
    for name in ("rto_delivered", "rto_mdnd", "rto_recon", "breach_explorer"):
        caches[name] = types.SimpleNamespace(
            __name__=name, started=[],
            snapshot=lambda: {"fetched_at": 100.0, "refreshing": False, "error": None},
            is_refreshing=lambda: False,
        )
        caches[name].refresh_cache_async = lambda force=False, n=name: caches[n].started.append(force)
    monkeypatch.setattr(main, "DB_CACHES", {"RTO Status": caches["rto_delivered"], "RTO MDND": caches["rto_mdnd"],
                                            "RTO Reconciliation": caches["rto_recon"],
                                            "Breach Explorer": caches["breach_explorer"]})
    monkeypatch.setattr(main, "_forced_at", {})
    accounts.add("be.ed", "editor-pass", "editor", ["be-rto", "be-pendency"])
    accounts.add("md.ed", "editor-pass", "editor", ["rto-mdnd"])
    accounts.add("proc", "process-pass", "editor", ["rto-process"])

    def ids(username):
        return [s["id"] for s in client(main, accounts, username).get("/api/data-status").json()["sources"]]

    assert ids("tester") == ["rto_delivered", "rto_mdnd", "rto_recon", "breach_explorer"]
    assert ids("be.ed") == ["breach_explorer"] and ids("md.ed") == ["rto_mdnd"] and ids("proc") == []

    assert client(main, accounts, "md.ed").post("/api/refresh-all").json() == {"refreshing": ["rto_mdnd"]}
    assert caches["breach_explorer"].started == []  # an RTO editor's refresh leaves the ten-minute run alone
    assert client(main, accounts, "be.ed").post("/api/refresh-all").json() == {"refreshing": ["breach_explorer"]}
    assert caches["breach_explorer"].started == [True]


def test_the_users_page_shows_each_tab_under_its_sidebar_group(main, accounts):
    tabs = client(main, accounts, "tester").get("/api/users").json()["tabs"]
    assert [t["group"] for t in tabs] == ["RTO"] * 5 + ["Breach Explorer"] * 6
    assert {"id": "be-rto", "name": "RTO", "group": "Breach Explorer"} in tabs
    r = client(main, accounts, "tester").post("/api/users", json={
        "username": "pendency.only", "password": "long-enough-1", "role": "viewer", "tabs": ["be-pendency", "be-rto"]})
    assert r.status_code == 201
    assert client(main, accounts, "pendency.only").get("/api/me").json()["panels"] == ["be-pendency", "be-rto"]
