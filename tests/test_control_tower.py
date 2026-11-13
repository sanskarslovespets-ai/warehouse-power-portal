"""Control Tower (control_tower.py and its routes in main.py): the ERP query's joins done in Python, the sheet's
derived columns, the page's data, rebuilding it, and who may see it. No database: the rows are made up here, in
the CTEs' column order (control_tower.ERP_QUERIES)."""
import gzip
import json
import time
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

import control_tower as ct

LOCAL = ("127.0.0.1", 50000)
T = datetime(2026, 10, 30, 10, 0)  # a morning order
NOW = datetime(2026, 10, 31, 18, 0, tzinfo=ct.daily_refresh.IST)


def so_head(name, shopify_time=T):
    return (name, shopify_time, "560001", 1, 1)


def so_item(so, item, created=T):
    return (so, None, created, item, 1, 100.0, "WH-BIN")


def dn_head(dn, status="To Be Picked", awb=None, recommended="Bluedart | Bluedart_DPBLR", assigned=None,
            channel="Shopify"):
    return (dn, status, None, recommended, assigned, awb, channel)


def dn_item(dn, so, item, warehouse="MWH_Blr_Neelmangla_002"):
    return (dn, T, item, 1, warehouse, so)


def pick_item(pick, dn, item):
    return (pick, T, item, 1, dn)


# ------------------------------------------------------------------ the ERP query's joins


def test_the_join_keeps_one_row_per_dn_like_row_number_and_drops_the_fee_item():
    rows = ct._join(
        soi=[so_item("SO1", "A"), so_item("SO1", "B"), so_item("SO1", ct.FEE_ITEM), so_item("SO2", "C")],
        soh=[so_head("SO1"), so_head("SO2")],
        dni=[dn_item("DN1", "SO1", "A"), dn_item("DN1", "SO1", "B")],
        dnh=[dn_head("DN1")],
        pli=[pick_item("PICK-1", "DN1", "B")],
    )
    by_dn = {r["delivery_note"]: r for r in rows}
    assert set(by_dn) == {"DN1", None}  # SO1's DN once; SO2 has no DN yet but still comes out, as in the query
    assert by_dn["DN1"]["pick_number"] == "PICK-1"  # a tie on creation goes to the item that was picked
    assert by_dn["DN1"]["dn_warehouse"] == "MWH_Blr_Neelmangla_002" and by_dn["DN1"]["dn_status"] == "To Be Picked"
    assert by_dn[None]["sales_order"] == "SO2"  # SO1's fee item had no DN: its row went with the final WHERE


def test_the_join_keeps_the_earliest_item_and_a_dn_per_status_and_awb():
    early, late = datetime(2026, 10, 30, 9), datetime(2026, 10, 30, 11)
    rows = ct._join(
        soi=[so_item("SO1", "B", late), so_item("SO1", "A", early)],
        soh=[so_head("SO1")],
        dni=[dn_item("DN1", "SO1", "A"), dn_item("DN1", "SO1", "B"), dn_item("DN2", "SO1", "B", "DKS_Blr_HSRlayout_002")],
        dnh=[dn_head("DN1", awb="AWB1"), dn_head("DN2")],
        pli=[],
    )
    assert sorted((r["delivery_note"], r["item_code"]) for r in rows) == [("DN1", "A"), ("DN2", "B")]
    assert next(r for r in rows if r["delivery_note"] == "DN1")["awb"] == "AWB1"


# ------------------------------------------------------------------ the derived columns


def test_ideal_pickup_date_uses_the_cutoff_else_the_noon_rule():
    assert ct.ideal_pickup_date(datetime(2026, 10, 30, 15), 0) == date(2026, 10, 30)
    assert ct.ideal_pickup_date(datetime(2026, 10, 30, 9), 1) == date(2026, 10, 31)
    assert ct.ideal_pickup_date(datetime(2026, 10, 30, 9), 2.0) == date(2026, 11, 1)  # Mongo may give a double
    assert ct.ideal_pickup_date(datetime(2026, 10, 30, 11, 59), None) == date(2026, 10, 30)
    assert ct.ideal_pickup_date(datetime(2026, 10, 30, 12, 0), None) == date(2026, 10, 31)
    assert ct.ideal_pickup_date(datetime(2026, 10, 30, 12, 0), "") == date(2026, 10, 31)


def test_the_dumps_cover_whole_days_in_india_time():
    just_after_midnight = datetime(2026, 11, 1, 0, 30, tzinfo=ct.daily_refresh.IST)  # still 31 Oct on a UTC server
    assert ct._window(ct.ERP_DAYS, just_after_midnight) == (date(2026, 10, 26), date(2026, 11, 2))
    assert ct._window(ct.DUMP_DAYS, NOW) == (date(2026, 10, 24), date(2026, 11, 1))  # the CP and EDD dumps' days


class FakeConnection:
    """A database connection whose query fails with each error in `errors`, then answers."""

    def __init__(self, errors, executed):
        self.errors, self.executed, self.open = errors, executed, True

    def cursor(self):
        conn = self

        class Cursor:
            description = [("AWB",), ("Clickpost Unified Status",)]

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params=None):
                conn.executed.append(sql)
                if params is not None and conn.errors:
                    conn.open = False
                    raise conn.errors.pop(0)

            def fetchall(self):
                return [("AWB1", "Delivered")]
        return Cursor()

    def close(self):
        self.open = False


def test_a_dropped_connection_is_tried_again_and_the_server_waits_for_slow_reads(monkeypatch):
    import pymysql
    monkeypatch.setattr(ct.time, "sleep", lambda s: None)
    lost = pymysql.err.OperationalError(2013, "Lost connection to MySQL server during query")
    errors, executed = [lost], []
    connect = lambda read_timeout: FakeConnection(errors, executed)
    assert ct._fetch("Clickpost", connect, "SELECT 1", "SET cap", 8)[1] == [("AWB1", "Delivered")]
    assert executed.count("SELECT 1") == 2
    assert f"SET SESSION net_write_timeout = {ct.STATEMENT_SECONDS}" in executed  # not the server's 60 s

    errors[:] = [lost, lost]
    with pytest.raises(ct.SourceError, match=r"^ERP replica \(dni\): lost the connection \(twice\)$"):
        ct._fetch("ERP replica (dni)", connect, "SELECT 1", "SET cap", 7)
    errors[:] = [pymysql.err.ProgrammingError(1146, "Table 'scm.cp_database' doesn't exist")]
    with pytest.raises(ct.SourceError, match=r"^Clickpost: a table is missing$"):  # not retried
        ct._fetch("Clickpost", connect, "SELECT 1", "SET cap", 8)
    # the page never gets the server's own words: database users and internal hosts stay in the log
    errors[:] = [pymysql.err.OperationalError(1045, "Access denied for user 'ro_user'@'10.190.0.20'")]
    with pytest.raises(ct.SourceError) as caught:
        ct._fetch("ERP replica (soh)", connect, "SELECT 1", "SET cap", 7)
    assert str(caught.value) == "ERP replica (soh): the database refused the user or password"


def test_a_failed_source_is_named_on_the_page(tower, monkeypatch):
    stand_in(monkeypatch)
    def fetch_erp():
        raise ct.SourceError("ERP replica (dni): Lost connection to MySQL server during query (twice)")
    monkeypatch.setattr(ct, "fetch_erp", fetch_erp)
    tower.refresh_cache(force=True)
    assert tower.snapshot()["error"] == "ERP replica (dni): Lost connection to MySQL server during query (twice)"


def test_when_mongodb_fails_the_page_is_still_built_on_the_noon_rule_and_says_why(tower, monkeypatch):
    stand_in(monkeypatch)
    def fetch_edd():
        raise ct.SourceError("Promise engine (MongoDB): No servers found yet, Timeout: 20.0s")
    monkeypatch.setattr(ct, "fetch_edd", fetch_edd)
    tower.refresh_cache(force=True)
    data = served(tower)
    assert data["counts"]["edd_rows"] is None and "No servers found" in data["edd_problem"]
    assert tower.snapshot()["error"] is None  # the build itself worked


def test_a_mongodb_error_never_shows_the_password(monkeypatch):
    class Client:
        def __init__(self, uri, **kw):
            pass

        def __getitem__(self, name):
            raise ct.pymongo.errors.OperationFailure("bad auth for mongodb+srv://ro_user:s3cret@cluster.example.net/x")

        def close(self):
            pass
    monkeypatch.setenv("MONGO_URI", "mongodb+srv://ro_user:s3cret@cluster.example.net/")
    monkeypatch.setenv("MONGO_DB", "orderManagement")
    monkeypatch.setattr(ct.pymongo, "MongoClient", Client)
    with pytest.raises(ct.SourceError) as caught:
        ct.fetch_edd()
    assert "s3cret" not in str(caught.value) and "mongodb+srv://***@cluster.example.net" in str(caught.value)


def test_a_malformed_mongo_uri_is_reported_without_quoting_it_and_the_page_still_builds(tower, monkeypatch):
    # an unescaped "/" in the password: pymongo's own error would quote the rest of it
    monkeypatch.setenv("MONGO_URI", "mongodb://ro_user:Secr3t/Pa ss@db.example.net/")
    with pytest.raises(ct.SourceError) as caught:
        ct.fetch_edd()
    assert str(caught.value) == "Promise engine (MongoDB): MONGO_URI isn't a valid connection string"
    assert caught.value.__cause__ is None and caught.value.__suppress_context__  # nor in the log's traceback

    monkeypatch.setenv("MONGO_URI", "mongodb://ro_user:pw@db.example.net/?appName=x")  # names no database
    monkeypatch.delenv("MONGO_DB", raising=False)
    real_fetch_edd = ct.fetch_edd
    stand_in(monkeypatch)
    monkeypatch.setattr(ct, "fetch_edd", real_fetch_edd)
    tower.refresh_cache(force=True)
    assert "names no database" in served(tower)["edd_problem"] and tower.snapshot()["error"] is None


def test_without_mongo_uri_the_edd_dump_is_left_out(monkeypatch):
    monkeypatch.delenv("MONGO_URI", raising=False)
    assert ct.fetch_edd() is None


def test_courier_is_the_assigned_partner_else_the_recommended_route_s():
    assert ct.courier("Blue Dart Surface", "Delhivery | X") == "Bluedart"  # one spelling of Bluedart
    assert ct.courier("Somebody New", "") == "SELF"  # assigned but unknown, as the sheet's lookup did
    assert ct.courier("", "Pidge HLD | Zippee_Gurgaon_WH") == "Pidge HLD"
    assert ct.courier(None, "Unassigned Courier") == "No Courier"
    assert ct.courier(None, None) == "No Courier"


def test_facility_type_comes_from_the_warehouse_code():
    assert ct.bucket("MWH_Kol_Dankuni_001") == "WH"  # not in the sheet's list, which missed it
    assert ct.bucket("DKS_Del_MalviyaNagar_001") == "DS"
    assert ct.bucket("PWH_Del_Okhla_001") == "DS"
    assert ct.bucket("DPS_Kibbo_DL_001") == "Dropship"
    assert ct.bucket("MWH_Mars_GW_001") == "Dropship" and ct.bucket("DPS_Groom_My_Pet_DL_001") == "DS"
    assert ct.bucket("Unfulfilled WH") == "Other" and ct.bucket(None) == "Other"


def test_status_is_the_courier_s_once_it_has_the_parcel_else_the_dn_s():
    assert ct.status("Completed", "InTransit") == "InTransit"
    assert ct.status("Completed", "RTO-Delivered") == "RTO"
    assert ct.status("Completed", "OutForPickup") == "Packed"  # the courier hasn't got it yet
    assert ct.status("Picked", None) == "Item Picked"
    assert ct.status("Completed", "SomeNewStatus") == "Packed"  # unknown: not yet with the courier, as in the sheet
    assert ct.status("Return Issued", None) == "Return Issued"


# ------------------------------------------------------------------ the page's data


def erp_row(dn, so="SO1", warehouse="MWH_Blr_Neelmangla_002", dn_status="To Be Picked", awb=None,
            order_time=T, pick="PICK-1", recommended="Bluedart | Bluedart_DPBLR", channel="Shopify"):
    return {"sales_order": so, "so_creation": order_time, "item_code": "A", "shopify_order_date": order_time,
            "delivery_note": dn, "dn_warehouse": warehouse, "dn_status": dn_status,
            "recommended_partner": recommended, "assigned_partner": None, "awb": awb, "channel": channel,
            "pick_number": pick}


def cp_row(awb, status="InTransit", order="SO1"):
    return {"AWB": awb, "Clickpost Unified Status": status, "Order ID": order, "Return Name": "MWH_Blr_Neelmangla_002"}


def edd_row(order, warehouse="MWH_Blr_Neelmangla_002", cutoff=0, edd=datetime(2026, 11, 2, 16, 30)):
    return {"orderName": order, "warehouse": warehouse, "TOTAL_CUTOFF": cutoff, "deliveryDateTime": edd,
            "deliveryType": "Standard A", "dayCount": 3, "createdAt": T}


def decode(data):
    """The facts as dicts, with the dims' values in place of their indexes."""
    names = ["facility", "bucket", "date", "date", "dn_status", "status", "courier", "channel"]
    keys = ["facility", "bucket", "ideal", "order", "dn_status", "status", "courier", "channel"]
    out = []
    for f in data["facts"]:
        row = {k: data["dims"][n][f[i]] for i, (k, n) in enumerate(zip(keys, names))}
        row.update(flags=f[8], n=f[9])
        out.append(row)
    return out


def test_warehouse_pendency_is_mother_warehouse_dns_with_no_awb_still_being_worked_on():
    afternoon = datetime(2026, 10, 31, 15)
    erp = [
        erp_row("DN1", dn_status="Completed"),  # packed, no AWB: pending
        erp_row("DN2", dn_status="Completed", awb="AWB2"),  # shipped
        erp_row("DN3", warehouse="DKS_Blr_HSRlayout_002"),  # a darkstore
        erp_row("DN4", dn_status="Cancel"),
        erp_row("DN5", dn_status="Hold"),
        erp_row("DN6", so="SO6", order_time=afternoon),  # after noon, no EDD: due tomorrow
        erp_row("DN7", so="SO7", order_time=afternoon, warehouse="MWH_Kol_Dankuni_001"),
    ]
    cp = [cp_row("AWB2", status="PickedUp")]
    edd = [edd_row("SO7", warehouse="MWH_Kol_Dankuni_001", cutoff=0)]
    data = ct.build(erp, cp, edd, now=NOW)
    rows = {r[1]: dict(zip(data["pendency"]["columns"], r)) for r in data["pendency"]["rows"]}
    assert set(rows) == {"DN1", "DN6", "DN7"}
    assert rows["DN1"]["status"] == "Packed" and rows["DN1"]["ideal_pickup_date"] == "2026-10-30"
    assert rows["DN1"]["pick_number"] == "PICK-1" and rows["DN1"]["courier"] == "Bluedart"
    assert rows["DN6"]["ideal_pickup_date"] == "2026-11-01"
    # the promise engine gave SO7 from Dankuni a cutoff of 0: due today, no AWB needed, not on the noon rule
    assert rows["DN7"]["ideal_pickup_date"] == "2026-10-31"


def test_the_promise_is_the_one_for_the_dn_s_order_and_warehouse_by_its_erp_code():
    afternoon = datetime(2026, 10, 30, 15)
    erp = [
        erp_row("DN1", so="SO1", order_time=afternoon, warehouse="MWH_Ggn_bhajgera_001"),
        erp_row("DN2", so="SO2", order_time=afternoon, warehouse="DKS_Del_MalviyaNagar_001"),
        erp_row("DN3", so="SO3", order_time=afternoon, warehouse="MWH_Pun_Khed_001"),
    ]
    edd = [
        edd_row("SO1", warehouse="WH_Delhi_002", cutoff=1),  # the promise engine's code for Gurgaon
        edd_row("SO1", warehouse="WH_Delhi_002", cutoff=0),  # a later answer: the first one counts, as in the sheet
        edd_row("SO2", warehouse="DKS_Del_MalviyaNagar_001", cutoff=0),  # not in the sheet's list: its own code
        edd_row("SO3", warehouse="MWH_Mum_Bhiwandi_002", cutoff=0),  # another warehouse's promise
        {"orderName": "SO3", "warehouse": None, "TOTAL_CUTOFF": None},  # an answer with no warehouse
    ]
    facts = {f["facility"]: f for f in decode(ct.build(erp, [], edd, now=NOW))}
    assert facts["MWH_Ggn_bhajgera_001"]["ideal"] == "2026-10-31" and facts["MWH_Ggn_bhajgera_001"]["flags"] & ct.HAS_EDD
    assert facts["DKS_Del_MalviyaNagar_001"]["ideal"] == "2026-10-30"
    assert facts["MWH_Pun_Khed_001"]["ideal"] == "2026-10-31" and not facts["MWH_Pun_Khed_001"]["flags"] & ct.HAS_EDD


def test_without_the_edd_dump_every_dn_is_on_the_noon_rule_and_the_page_is_told():
    data = ct.build([erp_row("DN1", order_time=datetime(2026, 10, 30, 15))], [], None, now=NOW)
    assert decode(data)[0]["ideal"] == "2026-10-31" and data["counts"]["edd_rows"] is None
    assert ct.build([erp_row("DN1")], [], [], now=NOW)["counts"]["edd_rows"] == 0


def test_the_dashboard_counts_each_dn_once_with_its_flags():
    erp = [
        erp_row("DN1", so="SO1"), erp_row("DN1", so="SO1b"),  # two orders on one DN
        erp_row("DN2", so="SO2", awb="AWB2", dn_status="Completed"),
        erp_row("DN3", so="SO3", awb="AWB3", dn_status="Completed"),
        erp_row(None, so="SO9"),  # no DN yet: nothing to count
    ]
    cp = [cp_row("AWB2", status="Delivered"), cp_row("AWB3", status="OrderPlaced")]
    edd = [edd_row("SO2"), edd_row("SO3", edd=None)]
    data = ct.build(erp, cp, edd, now=NOW)
    facts = decode(data)
    assert sum(f["n"] for f in facts) == 3 == data["counts"]["dns"]
    by_status = {f["status"]: f for f in facts}
    assert by_status["To Be Picked"]["flags"] == 0
    assert by_status["Delivered"]["flags"] == ct.HAS_AWB | ct.HAS_EDD
    assert by_status["Packed"]["flags"] == ct.HAS_AWB  # shipped, but the promise engine gave no EDD
    assert by_status["Delivered"]["bucket"] == "WH" and by_status["Delivered"]["courier"] == "Bluedart"


def test_each_dn_carries_its_erp_channel_for_the_channel_filter():
    joined = ct._join(soi=[so_item("SO1", "A")], soh=[so_head("SO1")], dni=[dn_item("DN1", "SO1", "A")],
                      dnh=[dn_head("DN1", channel="Shopify Dropship")], pli=[])
    assert joined[0]["channel"] == "Shopify Dropship"
    erp = [erp_row("DN1", so="SO1"), erp_row("DN2", so="SO2", channel="IWT"), erp_row("DN3", so="SO3", channel=None)]
    data = ct.build(erp, [], [], now=NOW)
    assert {f["channel"] for f in decode(data)} == {"Shopify", "IWT", ct.NO_CHANNEL}
    rows = {r[1]: dict(zip(data["pendency"]["columns"], r)) for r in data["pendency"]["rows"]}
    assert rows["DN2"]["channel"] == "IWT" and rows["DN3"]["channel"] == ct.NO_CHANNEL


# ------------------------------------------------------------------ building and serving it


def stand_in(monkeypatch, erp=None, cp=None, edd=None, fail=None):
    def fetch_erp():
        if fail:
            raise RuntimeError(fail)
        return erp if erp is not None else [erp_row("DN1")]
    monkeypatch.setattr(ct, "fetch_erp", fetch_erp)
    monkeypatch.setattr(ct, "fetch_cp", lambda: cp or [])
    monkeypatch.setattr(ct, "fetch_edd", lambda: edd or [])


def served(tower):
    found = tower.payload()
    return json.loads(gzip.decompress(found[0])) if found else None


def test_a_build_is_served_and_a_restart_keeps_it(tower, monkeypatch):
    stand_in(monkeypatch)
    assert tower.payload() is None
    tower.refresh_cache(force=True)
    data = served(tower)
    assert data["counts"]["dns"] == 1 and tower.snapshot()["error"] is None
    tower._loaded, tower._payload, tower._fetched_at = False, None, None  # what a restart leaves
    assert served(tower)["generated_at"] == data["generated_at"]


def test_a_cache_with_an_older_layout_is_rebuilt_not_served_after_a_restart(tower):
    old = {"generated_at": time.time(), "dims": {}, "facts": [], "pendency": {"columns": [], "rows": []}}
    with open(tower.CACHE_FILE, "wb") as fh:
        fh.write(gzip.compress(json.dumps(old).encode()))  # no "schema": built before the channel was added
    assert tower.payload() is None and tower.started  # a page would misread it: a build starts instead


def test_a_failed_build_keeps_the_previous_data_and_says_why(tower, monkeypatch):
    stand_in(monkeypatch)
    tower.refresh_cache(force=True)
    before = tower.payload()
    stand_in(monkeypatch, fail="ERP replica refused the connection")
    tower.refresh_cache(force=True)
    assert tower.payload() == before
    assert "ERP replica refused" in tower.snapshot()["error"]


def test_old_data_is_rebuilt_when_someone_looks_but_not_over_and_over(tower, monkeypatch):
    stand_in(monkeypatch)
    tower.refresh_cache(force=True)
    tower.snapshot()
    assert tower.started == []  # fresh
    monkeypatch.setattr(tower, "_fetched_at", time.time() - tower.FRESH_FOR_SECONDS - 1)
    monkeypatch.setattr(tower, "_last_attempt_at", 0.0)
    tower.snapshot()
    assert tower.started == [False]
    monkeypatch.setattr(tower, "_last_attempt_at", time.time())  # it was just tried (and failed)
    tower.snapshot()
    assert tower.started == [False]


class Stop(Exception):
    pass


def test_data_is_rebuilt_every_30_minutes_whether_or_not_anyone_looks(tower, monkeypatch):
    began = []
    stand_in(monkeypatch)
    erp = tower.fetch_erp
    monkeypatch.setattr(tower, "fetch_erp", lambda: began.append(time.time()) or erp())
    tower.refresh_cache(force=True)
    assert tower._fetched_at <= began[0]  # dated from when the queries started, not when the build ended

    def one_minute():
        """The schedule's thread for one check, with no page open."""
        slept = []
        def sleep(seconds):
            if slept:
                raise Stop
            slept.append(seconds)
        monkeypatch.setattr(tower.time, "sleep", sleep)
        with pytest.raises(Stop):
            tower._schedule()

    one_minute()
    assert tower.started == []  # fresh: nothing to do
    half_an_hour_ago = time.time() - tower.FRESH_FOR_SECONDS - 1
    monkeypatch.setattr(tower, "_fetched_at", half_an_hour_ago)
    monkeypatch.setattr(tower, "_last_attempt_at", half_an_hour_ago)
    one_minute()
    assert tower.started == [False]


@pytest.fixture()
def main(tmp_path, monkeypatch):
    import entries_store

    monkeypatch.setattr(entries_store, "DB_PATH", str(tmp_path / "t.db"))
    import main

    entries_store.init_db()
    return main


def client(main, accounts, username):
    return TestClient(main.app, client=LOCAL, cookies=accounts.cookies(username))


def test_the_page_gets_the_data_gzipped_and_only_again_once_it_changes(main, accounts, tower, monkeypatch):
    admin = client(main, accounts, "tester")
    r = admin.get("/api/control-tower")
    assert r.status_code == 503 and tower.started == [False]  # no data yet: a build is on its way
    stand_in(monkeypatch)
    tower.refresh_cache(force=True)
    r = admin.get("/api/control-tower")
    assert r.status_code == 200 and r.headers["content-encoding"] == "gzip" and r.json()["counts"]["dns"] == 1
    again = admin.get("/api/control-tower", headers={"If-None-Match": r.headers["etag"]})
    assert again.status_code == 304 and again.content == b""
    assert admin.get("/api/control-tower/status").json()["fetched_at"] == tower._fetched_at


def test_the_control_tower_is_only_for_people_given_it_and_refresh_for_its_editors(main, accounts, tower, monkeypatch):
    stand_in(monkeypatch)
    tower.refresh_cache(force=True)
    monkeypatch.setattr(main, "_forced_at", {})
    accounts.add("ct.view", "viewer-pass", "viewer", ["control-tower"])
    accounts.add("ct.ed", "editor-pass", "editor", ["control-tower"])
    accounts.add("rto.only", "rto-only-pass", "editor", ["rto-status"])
    viewer, editor, rto_only = (client(main, accounts, n) for n in ("ct.view", "ct.ed", "rto.only"))

    assert rto_only.get("/api/control-tower").status_code == 403
    assert rto_only.get("/api/control-tower/status").status_code == 403
    assert viewer.get("/api/control-tower").status_code == 200
    assert viewer.post("/api/control-tower/refresh").status_code == 403
    assert "control-tower" in viewer.get("/api/me").json()["panels"]

    r = editor.post("/api/control-tower/refresh")
    assert r.status_code == 202 and tower.started == [True]
    assert editor.post("/api/control-tower/refresh").status_code == 429  # not again within minutes
    assert [s["id"] for s in editor.get("/api/data-status").json()["sources"]] == ["control_tower"]
