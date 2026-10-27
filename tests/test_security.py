"""security.py (who may use the panel, cross-site writes, response headers) and main.py's input hardening."""
import os
import sys
import time
import types

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.auth import crypt
from google.auth import jwt as google_jwt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

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


# ------------------------------------------------------------------ local runs

def test_local_run_answers_this_machine_only(app):
    assert TestClient(app, client=LOCAL).get("/api/ping").status_code == 200
    assert TestClient(app, client=("::1", 50000)).get("/api/ping").status_code == 200
    denied = TestClient(app, client=REMOTE).get("/api/ping")
    assert denied.status_code == 403 and "IAP_AUDIENCE" in denied.json()["error"]


def test_cross_site_writes_are_refused(app):
    c = TestClient(app, client=LOCAL)
    assert c.post("/api/write", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert c.post("/api/write", headers={"Sec-Fetch-Site": "same-site"}).status_code == 403
    assert c.post("/api/write", headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.post("/api/write", headers={"Origin": "null"}).status_code == 403
    assert c.post("/api/write", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
    assert c.post("/api/write", headers={"Origin": "http://testserver"}).status_code == 200
    assert c.post("/api/write").status_code == 200  # not from a browser
    assert c.get("/api/ping", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200  # reads are not writes


def test_writes_are_audit_logged(app, capsys):
    c = TestClient(app, client=LOCAL)
    c.post("/api/write")
    c.get("/api/ping")
    out = capsys.readouterr().out
    assert "[audit] local POST /api/write -> 200" in out and "/api/ping" not in out


def test_security_headers(app):
    r = TestClient(app, client=LOCAL).get("/api/ping")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"
    # the page's own files: cached, but re-checked on every load so an update shows at once
    assert TestClient(app, client=LOCAL).get("/js/app.js").headers["cache-control"] == "no-cache"
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
    c = TestClient(app, client=REMOTE)
    assert c.get("/api/ping", headers=iap()).status_code == 200
    assert c.get("/api/ping").status_code == 401
    assert c.get("/api/ping", headers={"X-Goog-IAP-JWT-Assertion": "not-a-jwt"}).status_code == 401
    assert c.get("/api/ping", headers=iap(aud="/projects/1/global/backendServices/2")).status_code == 401
    assert c.get("/api/ping", headers=iap(expires_in=-120)).status_code == 401
    assert c.get("/api/ping", headers=iap(iss="https://evil.example")).status_code == 403
    assert c.get("/api/ping", headers=iap(email="someone@gmail.com")).status_code == 403
    assert c.get("/api/ping", headers=iap(email="x@supertails.com.evil.example")).status_code == 403
    c.post("/api/write", headers=iap())
    assert "[audit] a.person@supertails.com POST /api/write -> 200" in capsys.readouterr().out


def test_local_requests_still_need_iap_once_it_is_on(app, iap):
    assert TestClient(app, client=LOCAL).get("/api/ping").status_code == 401


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
    c = TestClient(main.app, client=LOCAL)
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
    assert "service_account_email" not in TestClient(main.app, client=LOCAL).get("/api/status").json()
