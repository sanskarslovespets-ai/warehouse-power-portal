"""security.py (who may use the panel, cross-site writes, response headers), the sign-in (auth.py) and main.py's
input hardening."""
import json
import os
import sys
import time
import types
import unicodedata

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.auth import crypt
from google.auth import jwt as google_jwt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import auth  # noqa: E402
import security  # noqa: E402

AUDIENCE = "/projects/1/locations/asia-south1/services/rto-panel"
LOCAL = ("127.0.0.1", 50000)
REMOTE = ("203.0.113.9", 50000)


@pytest.fixture()
def app():
    app = FastAPI()

    @app.get("/api/ping")
    def ping():
        return {"ok": True}

    @app.post("/api/write")
    def write():
        return {"ok": True}

    app.add_middleware(security.Guard)
    app.add_middleware(security.SecurityHeaders)
    return app


def signed_in(app, client=LOCAL, user="wh.user"):
    """A client holding a session for `user`, as after signing in on /login."""
    return TestClient(app, client=client, cookies={auth.COOKIE: auth.issue(user)})


# ------------------------------------------------------------------ local runs

def test_local_run_answers_this_machine_only(app):
    assert signed_in(app).get("/api/ping").status_code == 200
    assert signed_in(app, ("::1", 50000)).get("/api/ping").status_code == 200
    denied = signed_in(app, REMOTE).get("/api/ping")
    assert denied.status_code == 403 and "IAP_AUDIENCE" in denied.json()["error"]


def test_cross_site_writes_are_refused(app):
    c = signed_in(app)
    assert c.post("/api/write", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert c.post("/api/write", headers={"Sec-Fetch-Site": "same-site"}).status_code == 403
    assert c.post("/api/write", headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.post("/api/write", headers={"Origin": "null"}).status_code == 403
    assert c.post("/api/write", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
    assert c.post("/api/write", headers={"Origin": "http://testserver"}).status_code == 200
    assert c.post("/api/write").status_code == 200  # not from a browser
    assert c.get("/api/ping", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200  # reads are not writes


def test_writes_are_audit_logged(app, capsys):
    c = signed_in(app)
    c.post("/api/write")
    c.get("/api/ping")
    out = capsys.readouterr().out
    assert "[audit] local as wh.user POST /api/write -> 200" in out and "/api/ping" not in out


def test_security_headers(app):
    r = signed_in(app).get("/api/ping")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"
    # the page's own files: cached, but re-checked on every load so an update shows at once
    assert signed_in(app).get("/js/app.js").headers["cache-control"] == "no-cache"
    denied = TestClient(app, client=REMOTE).get("/api/ping")
    assert denied.headers["x-frame-options"] == "DENY"  # denials get them too


# ------------------------------------------------------------------ behind IAP

@pytest.fixture()
def iap(monkeypatch):
    """IAP switched on, with a test signing key standing in for Google's."""
    key = ec.generate_private_key(ec.SECP256R1())
    private_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    monkeypatch.setattr(security, "IAP_AUDIENCE", AUDIENCE)
    monkeypatch.setattr(security, "_iap_certs", lambda: {"k1": public_pem})
    signer = crypt.ES256Signer.from_string(private_pem, key_id="k1")

    def token(email="a.person@supertails.com", aud=AUDIENCE, iss=security.IAP_ISSUER, expires_in=600):
        now = int(time.time())
        claims = {"iss": iss, "aud": aud, "email": email, "iat": now - 10, "exp": now + expires_in}
        return {"X-Goog-IAP-JWT-Assertion": google_jwt.encode(signer, claims).decode()}

    return token


def test_iap_token_is_verified(app, iap, capsys):
    c = signed_in(app, REMOTE)
    assert c.get("/api/ping", headers=iap()).status_code == 200
    assert c.get("/api/ping").status_code == 401
    assert c.get("/api/ping", headers={"X-Goog-IAP-JWT-Assertion": "not-a-jwt"}).status_code == 401
    assert c.get("/api/ping", headers=iap(aud="/projects/1/global/backendServices/2")).status_code == 401
    assert c.get("/api/ping", headers=iap(expires_in=-120)).status_code == 401
    assert c.get("/api/ping", headers=iap(iss="https://evil.example")).status_code == 403
    assert c.get("/api/ping", headers=iap(email="someone@gmail.com")).status_code == 403
    assert c.get("/api/ping", headers=iap(email="x@supertails.com.evil.example")).status_code == 403
    c.post("/api/write", headers=iap())
    assert "[audit] a.person@supertails.com as wh.user POST /api/write -> 200" in capsys.readouterr().out


def test_local_requests_still_need_iap_once_it_is_on(app, iap):
    assert signed_in(app).get("/api/ping").status_code == 401


# ------------------------------------------------------------------ main.py

@pytest.fixture()
def main(tmp_path, monkeypatch):
    import entries_store

    monkeypatch.setattr(entries_store, "DB_PATH", str(tmp_path / "t.db"))
    import main

    entries_store.init_db()
    monkeypatch.setattr(main, "_forced_at", {})
    return main


def test_csv_cells_that_would_run_as_formulas_are_kept_as_text(main):
    assert main._csv_safe('=HYPERLINK("http://x","y")') == "'=HYPERLINK(\"http://x\",\"y\")"
    assert [main._csv_safe(v) for v in ("+1", "-1", "@SUM(A1)", "\tx")] == ["'+1", "'-1", "'@SUM(A1)", "'\tx"]
    assert main._csv_safe("Delivered") == "Delivered" and main._csv_safe("") == ""


def test_forced_refresh_has_a_cooldown(main):
    started = []
    cache = types.SimpleNamespace(__name__="fake_cache", refresh_cache_async=lambda force: started.append(force))
    assert main._force_refresh(cache) == {"refreshing": True}
    with pytest.raises(main.HTTPException) as e:
        main._force_refresh(cache)
    assert e.value.status_code == 429 and started == [True]


def test_new_entry_refuses_unlisted_file_types_and_oversized_text(main):
    c = signed_in(main.app)
    form = {
        "damage_marked_date": "2026-10-01", "received_date": "2026-10-02", "facility_name": "Gurgaon",
        "damage_type": "RTO", "awb_code": "A1", "sku_code": "S1", "courier_name": "Delhivery",
        "remarks": "x" * (main.MAX_REMARKS_LENGTH + 1),
    }
    files = {"pod_file": ("pod.html", b"<script>", "text/html"), "video_file": ("clip.exe", b"MZ", "video/mp4")}
    r = c.post("/api/entries", data=form, files=files)
    assert r.status_code == 400
    error = r.json()["error"]
    assert "POD must be one of" in error and "Video must be one of" in error and "Remarks must be at most" in error


def test_status_does_not_expose_the_service_account(main, monkeypatch):
    monkeypatch.setattr(main.dropdown_cache, "refresh_cache_async", lambda: None)  # no DB from a test
    assert "service_account_email" not in signed_in(main.app).get("/api/status").json()


# ------------------------------------------------------------------ sign-in (auth.py)

def _loose(text):
    """How MySQL's utf8mb4_0900_ai_ci compares: ignoring case and accents."""
    return "".join(c for c in unicodedata.normalize("NFD", text) if not unicodedata.combining(c)).casefold()


@pytest.fixture()
def accounts(main, monkeypatch):
    """A stand-in warehouse_users table, matched by username the way MySQL would."""
    table = [("Sanskar.WH", "s3cret-Pass")]
    monkeypatch.setattr(auth, "_accounts_named", lambda name: [r for r in table if _loose(r[0]) == _loose(name)])
    monkeypatch.setattr(auth, "_failures", {})
    return table


def sign_in(c, username="Sanskar.WH", password="s3cret-Pass"):
    return c.post("/api/login", json={"username": username, "password": password})


def test_nothing_opens_without_signing_in(main):
    c = TestClient(main.app, client=LOCAL, follow_redirects=False)
    page = c.get("/")
    assert page.status_code == 303 and page.headers["location"] == "/login"
    assert c.get("/index.html").status_code == 303 and c.get("/js/app.js").status_code == 303
    r = c.get("/api/entries")
    assert r.status_code == 401 and r.json() == {"error": "Please sign in."}
    assert c.post("/api/refresh-all").status_code == 401
    # only the sign-in page and what it loads are open
    assert 'id="login-form"' in c.get("/login").text
    for path in ("/css/style.css", "/js/login.js", "/favicon.png", "/img/supertails-logo.png"):
        assert c.get(path).status_code == 200, path
    # and the IAP / this-machine rule still comes first
    assert TestClient(main.app, client=REMOTE).get("/login").status_code == 403


def test_forged_edited_or_expired_sessions_are_refused(main):
    def me(token):
        return TestClient(main.app, client=LOCAL, cookies={auth.COOKIE: token}).get("/api/me")

    token = auth.issue("Sanskar.WH")
    assert me(token).json() == {"username": "Sanskar.WH"}
    signature = token.partition(".")[2]
    edited = auth._b64(json.dumps({"u": "admin", "exp": 9999999999}).encode())
    assert me(f"{edited}.{signature}").status_code == 401
    assert me(f"{edited}.").status_code == 401 and me("garbage").status_code == 401
    expired = auth._b64(json.dumps({"u": "Sanskar.WH", "exp": int(time.time()) - 1}).encode())
    assert me(f"{expired}.{auth._signature(expired)}").status_code == 401
    assert auth.session_user("\u00e9.\u00e9") is None and auth.session_user(None) is None


def test_right_username_and_password_open_the_panel(main, accounts, capsys):
    c = TestClient(main.app, client=LOCAL, follow_redirects=False)
    r = sign_in(c)
    assert r.status_code == 200 and r.json() == {"username": "Sanskar.WH"}
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "max-age=43200" in cookie
    assert "secure" not in cookie  # a local run is plain http://localhost
    assert c.get("/api/me").json() == {"username": "Sanskar.WH"}
    assert c.get("/").status_code == 200
    assert c.get("/login").headers["location"] == "/"  # already signed in
    assert "[auth] 'Sanskar.WH' signed in" in capsys.readouterr().out


def test_wrong_username_or_password_get_the_same_answer(main, accounts):
    c = TestClient(main.app, client=LOCAL)
    for username, password in [
        ("Sanskar.WH", "wrong"),
        ("nobody", "s3cret-Pass"),
        ("Sanskar.WH", "S3CRET-PASS"),  # the table's collation ignores case; the password check must not
        ("S\u00e4nskar.WH", "s3cret-Pass"),  # ... nor accents in the username
        ("Sanskar.WH", "s3cret-Pass "),
    ]:
        r = sign_in(c, username, password)
        assert (r.status_code, r.json()) == (401, {"error": "Incorrect username or password."}), username
        assert "set-cookie" not in r.headers
    assert c.get("/api/me").status_code == 401


def test_username_ignores_case_and_surrounding_spaces(main, accounts):
    r = sign_in(TestClient(main.app, client=LOCAL), "  sanskar.wh ")
    assert r.status_code == 200 and r.json() == {"username": "Sanskar.WH"}


def test_blank_or_overlong_credentials(main, accounts):
    c = TestClient(main.app, client=LOCAL)
    assert sign_in(c, "", "x").status_code == 400 and sign_in(c, "Sanskar.WH", "").status_code == 400
    assert sign_in(c, "Sanskar.WH", "x" * 256).status_code == 401


def test_repeated_wrong_passwords_lock_the_username(main, accounts, monkeypatch):
    c = TestClient(main.app, client=LOCAL)
    for _ in range(auth.MAX_FAILURES):
        assert sign_in(c, password="guess").status_code == 401
    r = sign_in(c, "SANSKAR.wh")  # even the right password, in any case
    assert r.status_code == 429 and "15 minutes" in r.json()["error"]
    assert sign_in(c, "someone.else", "x").status_code == 401  # other usernames are not locked
    monkeypatch.setattr(auth, "FAILURE_WINDOW_SECONDS", 0)  # ... and the lock wears off
    assert sign_in(c).status_code == 200 and "sanskar.wh" not in auth._failures


def test_a_database_problem_is_not_a_wrong_password(main, monkeypatch):
    def unreachable(_name):
        raise OSError("can't connect")

    monkeypatch.setattr(auth, "_accounts_named", unreachable)
    monkeypatch.setattr(auth, "_failures", {})
    r = sign_in(TestClient(main.app, client=LOCAL))
    assert r.status_code == 503 and auth._failures == {}


def test_sign_out_ends_the_session(main, accounts):
    c = TestClient(main.app, client=LOCAL)
    sign_in(c)
    assert c.post("/api/logout").status_code == 200
    assert c.get("/api/me").status_code == 401


def test_sign_in_cannot_be_posted_from_another_site(main, accounts):
    r = TestClient(main.app, client=LOCAL).post(
        "/api/login", json={"username": "Sanskar.WH", "password": "s3cret-Pass"}, headers={"Sec-Fetch-Site": "cross-site"}
    )
    assert r.status_code == 403 and "set-cookie" not in r.headers


def test_session_cookie_is_https_only_on_a_server(main, accounts, iap):
    r = sign_in(TestClient(main.app, client=REMOTE, headers=iap()))
    assert r.status_code == 200 and "secure" in r.headers["set-cookie"].lower()
