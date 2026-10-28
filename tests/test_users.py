"""Roles and tab access (users.py): what each role reaches, the Users page's API, and changes reaching
people already signed in. conftest.py stands in for the user tables ("tester" and "wh.user" are admins)."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pymysql.constants import ER
from pymysql.err import OperationalError

import security
import users

LOCAL = ("127.0.0.1", 50000)


@pytest.fixture()
def app():
    """Stand-in routes at the real paths, behind the real Guard."""
    app = FastAPI()
    for path in ("/api/me", "/api/data-status", "/api/entries", "/api/status", "/api/rto-mdnd",
                 "/api/analytics/status/export", "/api/users", "/api/brand-new"):
        app.add_api_route(path, lambda: {"ok": True}, methods=["GET"])
    for path in ("/api/entries", "/api/rto-mdnd/inputs", "/api/refresh-all", "/api/users"):
        app.add_api_route(path, lambda: {"ok": True}, methods=["POST"])
    app.add_middleware(security.Guard)
    return app


@pytest.fixture()
def main(tmp_path, monkeypatch):
    import entries_store

    monkeypatch.setattr(entries_store, "DB_PATH", str(tmp_path / "t.db"))
    import main

    entries_store.init_db()
    return main


def client(app, accounts, username, **kwargs):
    return TestClient(app, client=LOCAL, cookies=accounts.cookies(username), **kwargs)


# ------------------------------------------------------------------ what each role reaches

def test_each_role_reaches_only_its_tabs(app, accounts):
    accounts.add("vee", "viewer-pass", "viewer", ["rto-mdnd", "rto-analytics"])
    accounts.add("eddie", "editor-pass", "editor", ["rto-process"])
    accounts.add("newbie", "newbie-pass")  # in warehouse_users, no access row yet

    requests = [("get", "/api/me"), ("get", "/api/data-status"), ("get", "/api/entries"), ("post", "/api/entries"),
                ("get", "/api/status"), ("get", "/api/rto-mdnd"), ("post", "/api/rto-mdnd/inputs"),
                ("get", "/api/analytics/status/export"), ("post", "/api/refresh-all"), ("get", "/api/users"),
                ("post", "/api/users"), ("get", "/api/brand-new")]

    def codes(username):
        c = client(app, accounts, username)
        return [getattr(c, method)(path).status_code for method, path in requests]

    assert codes("tester") == [200] * len(requests)  # an admin
    assert codes("vee") == [200, 200, 403, 403, 403, 200, 403, 200, 403, 403, 403, 403]
    assert codes("eddie") == [200, 200, 200, 200, 200, 403, 403, 403, 200, 403, 403, 403]
    assert codes("newbie") == [200, 200, 403, 403, 403, 403, 403, 403, 403, 403, 403, 403]

    vee = client(app, accounts, "vee")
    assert vee.post("/api/rto-mdnd/inputs").json() == {"error": "Your access to the MDND tab is view-only."}
    assert vee.get("/api/entries").json() == {"error": "You don't have access to the Process tab."}
    assert vee.get("/api/brand-new").json() == {"error": "Only admins can do this."}  # new endpoints start closed
    assert vee.get("/js/app.js").status_code == 404  # the page's own files are the same for everyone


def test_me_says_which_panels_to_show(main, accounts):
    accounts.add("vee", "viewer-pass", "viewer", ["rto-recon", "rto-analytics"])
    assert client(main.app, accounts, "tester").get("/api/me").json() == {
        "username": "tester", "role": "admin", "panels": [*users.TABS, "users"], "can_edit": True,
    }
    assert client(main.app, accounts, "vee").get("/api/me").json() == {
        "username": "vee", "role": "viewer", "panels": ["rto-analytics", "rto-recon"], "can_edit": False,
    }


# ------------------------------------------------------------------ changes reach people already signed in

def test_changes_reach_people_already_signed_in(app, accounts):
    accounts.add("eddie", "editor-pass", "editor", ["rto-process", "rto-mdnd"])
    eddie = client(app, accounts, "eddie")
    assert eddie.get("/api/rto-mdnd").status_code == 200

    users.update("eddie", "editor", ["rto-process"], by="tester")  # MDND taken away
    assert eddie.get("/api/rto-mdnd").status_code == 403 and eddie.post("/api/entries").status_code == 200
    users.update("eddie", "viewer", ["rto-process"], by="tester")  # made a viewer
    assert eddie.post("/api/entries").status_code == 403 and eddie.get("/api/entries").status_code == 200

    users.update("eddie", "viewer", ["rto-process"], by="tester", password="a-new-password")
    assert eddie.get("/api/entries").status_code == 401  # signed out: the password changed
    eddie = client(app, accounts, "eddie")  # signed in again with the new one
    assert eddie.get("/api/entries").status_code == 200

    users.delete("eddie", by="tester")
    assert eddie.get("/api/entries").status_code == 401


def test_changes_made_elsewhere_arrive_within_cache_seconds(app, accounts, monkeypatch):
    accounts.add("eddie", "editor-pass", "editor", ["rto-mdnd"])
    eddie = client(app, accounts, "eddie")
    assert eddie.get("/api/rto-mdnd").status_code == 200
    accounts.rows["eddie"][2] = ""  # another server, or SQL by hand, takes the tab away
    assert eddie.get("/api/rto-mdnd").status_code == 200  # still the cached answer
    monkeypatch.setattr(users, "CACHE_SECONDS", 0)
    assert eddie.get("/api/rto-mdnd").status_code == 403


def test_database_trouble_keeps_the_last_answer(app, accounts, monkeypatch):
    tester = client(app, accounts, "tester")
    assert tester.get("/api/users").status_code == 200

    def unreachable(*_args):
        raise OSError("can't connect")

    monkeypatch.setattr(users, "_rows", unreachable)
    monkeypatch.setattr(users, "CACHE_SECONDS", 0)
    assert tester.get("/api/users").status_code == 200  # last known access, not a sign-out
    r = client(app, accounts, "wh.user").get("/api/users")  # nothing known about them yet
    assert r.status_code == 503 and "Could not check your access" in r.json()["error"]


def test_a_removed_users_old_cookie_gets_the_sign_in_page(main, accounts):
    accounts.add("gone", "gone-password", "viewer", ["rto-mdnd"])
    c = client(main.app, accounts, "gone", follow_redirects=False)
    del accounts.rows["gone"]
    users.forget("gone")
    assert c.get("/").headers["location"] == "/login"
    assert c.get("/login").status_code == 200  # the form, not a redirect back to / (a loop)


# ------------------------------------------------------------------ the Users page

def test_admins_add_change_and_remove_users(main, accounts, capsys):
    admin = client(main.app, accounts, "tester")
    r = admin.post("/api/users", json={
        "username": "rahul.wh", "password": "warehouse-1", "role": "viewer",
        "tabs": ["rto-recon", "rto-mdnd", "rto-analytics"],
    })
    assert r.status_code == 201
    listing = admin.get("/api/users")
    rahul = {u["username"]: u for u in listing.json()["users"]}["rahul.wh"]
    assert (rahul["role"], rahul["tabs"], rahul["updated_by"]) == ("viewer", ["rto-analytics", "rto-mdnd", "rto-recon"], "tester")
    assert "password" not in listing.text and "tester-pass" not in listing.text and listing.json()["you"] == "tester"

    # they can sign in, and see exactly those three tabs
    rahul = TestClient(main.app, client=LOCAL)
    assert rahul.post("/api/login", json={"username": "rahul.wh", "password": "warehouse-1"}).status_code == 200
    assert rahul.get("/api/me").json()["panels"] == ["rto-analytics", "rto-mdnd", "rto-recon"]

    assert admin.put("/api/users/rahul.wh", json={"role": "editor", "tabs": ["rto-process"]}).status_code == 200
    assert rahul.get("/api/me").json() == {"username": "rahul.wh", "role": "editor", "panels": ["rto-process"], "can_edit": True}

    assert admin.delete("/api/users/rahul.wh").status_code == 200
    assert rahul.get("/api/me").status_code == 401
    assert "rahul.wh" not in {u["username"] for u in admin.get("/api/users").json()["users"]}
    out = capsys.readouterr().out
    assert "[users] 'tester' added 'rahul.wh' as viewer" in out and "[users] 'tester' deleted 'rahul.wh'" in out


def test_the_users_page_refuses_bad_changes(main, accounts):
    admin = client(main.app, accounts, "tester")

    def add(**fields):
        r = admin.post("/api/users", json={"username": "new.person", "password": "long-enough", "role": "viewer",
                                           "tabs": [], **fields})
        return r.status_code, r.json().get("error", "")

    assert add(username="TESTER") == (400, "There is already a user called TESTER.")  # a look-alike
    assert add(username="ab")[0] == 400 and add(username="has space")[0] == 400
    assert add(password="short") == (400, "A password is 8 to 255 characters.")
    assert add(role="owner")[0] == 400 and add(tabs=["rto-nope"]) == (400, "There is no tab called rto-nope.")
    assert add()[0] == 201

    # an admin can't lock themselves out
    r = admin.put("/api/users/tester", json={"role": "viewer", "tabs": []})
    assert (r.status_code, r.json()["error"]) == (400, "You can't change your own role. Ask another admin to.")
    assert admin.delete("/api/users/tester").status_code == 400
    # changes match the exact name: a look-alike of someone is nobody
    assert admin.delete("/api/users/TESTER").status_code == 404
    assert admin.put("/api/users/nobody", json={"role": "viewer"}).status_code == 404

    accounts.add("eddie", "editor-pass", "editor", list(users.TABS))
    eddie = client(main.app, accounts, "eddie")
    assert eddie.get("/api/users").status_code == 403 and eddie.post("/api/users", json={}).status_code == 403


def test_a_database_account_that_may_not_write_users_is_named(main, accounts, monkeypatch):
    def refused(error):
        def save(*args, **kwargs):
            raise error
        monkeypatch.setattr(users, "_save", save)
        r = client(main.app, accounts, "tester").post(
            "/api/users", json={"username": "new.person", "password": "long-enough", "role": "viewer", "tabs": []}
        )
        return r.status_code, r.json()["error"]

    denied = OperationalError(ER.TABLEACCESS_DENIED_ERROR, "INSERT command denied to user 'x'@'%' for table 'warehouse_users'")
    status, error = refused(denied)
    assert status == 503 and "isn't allowed to change users" in error and "scm.warehouse_users" in error
    assert refused(OperationalError(2013, "Lost connection")) == (503, "Could not save the change to the database. Please try again.")


def test_changing_your_own_password_keeps_you_signed_in(main, accounts):
    admin = client(main.app, accounts, "tester")
    elsewhere = client(main.app, accounts, "tester")  # the same person in another browser
    r = admin.put("/api/users/tester", json={"role": "admin", "tabs": [], "password": "brand-new-pass"})
    assert r.status_code == 200
    assert TestClient(main.app, client=LOCAL, cookies={"wpp_session": r.cookies["wpp_session"]}).get("/api/users").status_code == 200
    assert elsewhere.get("/api/users").status_code == 401


def test_admin_users_are_admins_whatever_the_table_says(main, accounts, monkeypatch):
    monkeypatch.setattr(users, "ADMIN_USERS", {"boss"})
    accounts.add("boss", "boss-password")  # no access row at all
    boss = client(main.app, accounts, "boss")
    assert boss.get("/api/me").json()["role"] == "admin"
    listed = {u["username"]: u for u in boss.get("/api/users").json()["users"]}
    assert listed["boss"]["role"] == "admin" and listed["boss"]["fixed_admin"] is True
    admin = client(main.app, accounts, "tester")
    assert admin.put("/api/users/boss", json={"role": "viewer", "tabs": []}).status_code == 400
    assert admin.delete("/api/users/boss").status_code == 400
