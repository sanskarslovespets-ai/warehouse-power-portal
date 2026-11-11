"""Who may use the panel, and the browser protections added to every response.

On a server the panel sits behind Identity-Aware Proxy (IAP): staff sign in with their company
Google account and IAP signs every request it lets through with a JWT in the
X-Goog-IAP-JWT-Assertion header. With IAP_AUDIENCE set, that JWT is verified here as well
(signature, issuer, audience, expiry and email domain), so a mis-set ingress or an
`--allow-unauthenticated` deploy can't quietly open the panel to the internet.

Without IAP_AUDIENCE (a local run) only requests from this machine are answered. A server started
without it therefore fails closed: everyone else gets a 403, not the data. A server that is not
behind IAP yet can set ALLOW_WITHOUT_IAP=1 instead: then anyone can reach the sign-in page, and the
panel's own sign-in below is the only lock. IAP_AUDIENCE wins when both are set.

Past that, every request also needs the panel's own sign-in (auth.py: a warehouse_users username
and password), except the sign-in page itself, and an /api/ call must be for a tab the user's role
and tabs allow (users.py).

Both middlewares also:
- refuse cross-site writes (CSRF): a POST must come from this page, not another site the signed-in
  user happens to have open;
- log every write with who made it, so changes to entries and MDND inputs are traceable.
"""

import ipaddress
import os
import threading
import time
import traceback
from urllib.parse import urlsplit

import requests
from google.auth import exceptions as google_auth_exceptions
from google.auth import jwt as google_jwt
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse, RedirectResponse, Response

import auth
import users

# e.g. /projects/123456789/locations/asia-south1/services/rto-panel (Cloud Run with IAP), or
# /projects/123456789/global/backendServices/987654321 (IAP on a load balancer). Blank = local run.
IAP_AUDIENCE = os.environ.get("IAP_AUDIENCE", "").strip()
# A server not behind IAP (yet): skip the IAP check and rely on the panel's own sign-in alone.
ALLOW_WITHOUT_IAP = os.environ.get("ALLOW_WITHOUT_IAP", "").strip().lower() in ("1", "true", "yes")
ALLOWED_EMAIL_DOMAINS = {
    d.strip().lower() for d in os.environ.get("ALLOWED_EMAIL_DOMAINS", "supertails.com").split(",") if d.strip()
}

IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"
IAP_ISSUER = "https://cloud.google.com/iap"
CERTS_TTL_SECONDS = 3600
CLOCK_SKEW_SECONDS = 30

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# The page is all same-origin script; only its fonts come from Google Fonts. style-src needs
# 'unsafe-inline' because the charts set style attributes from JS (bar widths, colours).
CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])
SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "same-origin",
    "strict-transport-security": "max-age=31536000",
}
DOCS_PATHS = ("/docs", "/redoc")  # Swagger UI loads its script from a CDN; only served on local runs


class NotAllowed(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


_certs = {}
_certs_fetched_at = 0.0
_certs_lock = threading.Lock()


def _iap_certs():
    """IAP's public signing keys (key id -> PEM), re-fetched at most hourly."""
    global _certs, _certs_fetched_at
    with _certs_lock:
        if not _certs or time.time() - _certs_fetched_at > CERTS_TTL_SECONDS:
            resp = requests.get(IAP_CERTS_URL, timeout=10)
            resp.raise_for_status()
            _certs, _certs_fetched_at = resp.json(), time.time()
        return _certs


def verify_iap(token):
    """The signed-in user's email from an IAP JWT; raises NotAllowed unless it is valid and on an allowed domain."""
    if not token:
        raise NotAllowed(401, "Sign-in required.")
    try:
        certs = _iap_certs()
    except requests.RequestException:
        raise NotAllowed(503, "Could not check your sign-in right now. Please try again.")
    try:
        claims = google_jwt.decode(token, certs=certs, audience=IAP_AUDIENCE, clock_skew_in_seconds=CLOCK_SKEW_SECONDS)
    except (ValueError, google_auth_exceptions.GoogleAuthError):
        raise NotAllowed(401, "Sign-in required.")
    email = str(claims.get("email") or "").lower()
    if claims.get("iss") != IAP_ISSUER or email.rpartition("@")[2] not in ALLOWED_EMAIL_DOMAINS:
        raise NotAllowed(403, "This account is not allowed to use the panel.")
    return email


def on_server():
    """True on a server (IAP, or ALLOW_WITHOUT_IAP), False on a local run."""
    return bool(IAP_AUDIENCE) or ALLOW_WITHOUT_IAP


def _is_loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def cross_site_problem(method, headers):
    """Why a write must be refused as cross-site, or None. Reads only headers browsers set themselves."""
    if method in SAFE_METHODS:
        return None
    site = headers.get("sec-fetch-site")
    if site is not None:
        return None if site in ("same-origin", "none") else "Cross-site request blocked."
    origin = headers.get("origin")
    if origin is not None and urlsplit(origin).netloc != headers.get("host"):
        return "Cross-site request blocked."
    return None  # not sent by a browser (curl, a script): there is no signed-in session to ride on


def _sign_in_first(scope, had_session):
    """No session: opening a page goes to the sign-in page; an /api/ call (or a write) gets a 401.

    A page opened with a session that is no longer good (expired, or from before a password change) goes
    to /login?expired=1, where the page says they were signed out instead of just showing the form.
    """
    if scope["method"] in ("GET", "HEAD") and not scope["path"].startswith("/api/"):
        return RedirectResponse("/login?expired=1" if had_session else "/login", status_code=303)
    return JSONResponse({"error": "Please sign in."}, status_code=401)


def _with_cookie(send, token, secure):
    """`send`, adding a renewed session cookie (auth.renewal) to the response, unless the app set the session
    cookie itself: a password change issues a new one, which must not be overwritten by the old seal's."""
    holder = Response()
    auth.set_cookie(holder, token, secure)
    cookie = holder.headers["set-cookie"]

    async def send_with_cookie(message):
        if message["type"] == "http.response.start":
            out = MutableHeaders(scope=message)
            if not any(value.startswith(f"{auth.COOKIE}=") for value in out.getlist("set-cookie")):
                out.append("set-cookie", cookie)
        await send(message)

    return send_with_cookie


class Guard:
    """Answers 401/403 before the app sees a request from someone who may not use the panel."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        try:
            problem = cross_site_problem(scope["method"], headers)
            if problem:
                raise NotAllowed(403, problem)
            if IAP_AUDIENCE:
                user = await run_in_threadpool(verify_iap, headers.get("x-goog-iap-jwt-assertion", ""))
            elif ALLOW_WITHOUT_IAP:
                user = "web"
            elif _is_loopback((scope.get("client") or ("", 0))[0]):
                user = "local"
            else:
                raise NotAllowed(
                    403, "This panel only answers requests from this machine until IAP_AUDIENCE or ALLOW_WITHOUT_IAP is set."
                )
        except NotAllowed as e:
            return await JSONResponse({"error": str(e)}, status_code=e.status)(scope, receive, send)

        if scope["path"] not in auth.PUBLIC_PATHS:
            token = HTTPConnection(scope).cookies.get(auth.COOKIE)
            try:
                access = await run_in_threadpool(users.for_session, token)
            except Exception:
                traceback.print_exc()
                return await JSONResponse(
                    {"error": "Could not check your access right now. Please try again."}, status_code=503
                )(scope, receive, send)
            if not access:
                return await _sign_in_first(scope, had_session=bool(token))(scope, receive, send)
            denied = users.denial(access, scope["method"], scope["path"])
            if denied:
                return await JSONResponse({"error": denied}, status_code=403)(scope, receive, send)
            user = f"{user} as {access.username}"
            fresh = auth.renewal(token)
            if fresh:
                send = _with_cookie(send, fresh, secure=on_server() or scope.get("scheme") == "https")

        if scope["method"] in SAFE_METHODS:
            return await self.app(scope, receive, send)

        status = 500

        async def send_and_note_status(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_and_note_status)
        finally:
            print(f"[audit] {user} {scope['method']} {scope['path']} -> {status}", flush=True)


class SecurityHeaders:
    """Adds SECURITY_HEADERS and the CSP to every response, keeps /api/* data out of caches, and makes
    browsers re-check the page's own files on every load.

    Without a Cache-Control header a browser guesses how long to reuse a file from its age, so a script
    that had not changed for weeks kept running from cache for days after an update. `no-cache` still
    caches, but asks first: an unchanged file costs a 304 with no body.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                out = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    out.setdefault(name, value)
                if not path.startswith(DOCS_PATHS):
                    out.setdefault("content-security-policy", CONTENT_SECURITY_POLICY)
                if path.startswith("/api/"):
                    out["cache-control"] = "no-store"
                else:
                    out.setdefault("cache-control", "no-cache")
            await send(message)

        await self.app(scope, receive, send_with_headers)
