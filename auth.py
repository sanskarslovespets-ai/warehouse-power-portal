"""The panel's own sign-in: a username and password from scm.warehouse_users, on top of IAP.

IAP (security.py) decides which Google accounts can reach the panel at all; this decides which
of them may use it. The right username and password get a session cookie good for SESSION_HOURS,
and security.Guard turns away every request without one except the sign-in page itself
(PUBLIC_PATHS): a page load is sent to /login, an /api/ call gets a 401. Once less than half of a
session is left, the Guard hands its holder a fresh one (renewal), so someone using the panel is
not signed out in the middle of their work; a session nobody uses ends 6 to 12 hours after its
last request.

The cookie is the username, a seal of their password and an expiry, signed with SESSION_SECRET, so
it can't be forged or edited, and every instance sharing that secret accepts it. It is HttpOnly
(page scripts can't read it) and SameSite=Lax. What a signed-in user may open, and whether they
still exist and their password is unchanged, is users.py's part.

Passwords are compared here, in Python, never in SQL: the table's collation (utf8mb4_0900_ai_ci)
ignores case, so `WHERE password = %s` would let "SECRET" in for "secret". Usernames do ignore
case. MAX_FAILURES wrong passwords for one username within FAILURE_WINDOW_SECONDS lock it until
the oldest of them is that old, so a password can't be guessed by trying thousands.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

from rto_process import get_scm_connection

COOKIE = "wpp_session"
SESSION_HOURS = 12
MAX_FIELD_LENGTH = 255  # both columns are varchar(255)

MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 15 * 60

# The sign-in page and what it loads. Everything else needs a session.
PUBLIC_PATHS = {
    "/login", "/api/login", "/api/logout", "/css/style.css", "/js/login.js", "/favicon.png", "/img/supertails-logo.png",
}

def _session_key():
    """The key sessions are signed with. It must be the same on every start and every instance: a session
    signed with another key is refused, and its holder is sent to the sign-in page mid-work.

    SESSION_SECRET when set (the cleanest: its own secret, rotated on its own). Otherwise one derived from
    SCM_DB_PASSWORD, which every server already has and keeps across restarts (Cloud Run: Secret Manager,
    cloudbuild.yaml). That costs nothing in safety: whoever holds that password can already read
    warehouse_users, passwords and all. It goes through HMAC, so the key gives nothing of the password away,
    and changing the password signs everyone out once. Only with neither (a bare local run) is the key made
    up at each start, which signs everyone out on each restart.
    """
    configured = os.environ.get("SESSION_SECRET", "")
    if configured:
        return configured.encode()
    db_password = os.environ.get("SCM_DB_PASSWORD", "")
    if db_password:
        return hmac.new(db_password.encode(), b"warehouse-portal session signing key v1", hashlib.sha256).digest()
    return secrets.token_bytes(32)


_SECRET = _session_key()

_failures = {}  # casefolded username -> times of its recent wrong passwords
_failures_lock = threading.Lock()


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _signature(payload):
    return _b64(hmac.new(_SECRET, payload.encode(), hashlib.sha256).digest())


def seal(password):
    """Stands for a password inside a session: it changes whenever the password does, and reveals nothing of it."""
    return _b64(hmac.new(_SECRET, b"password\0" + password.encode(), hashlib.sha256).digest()[:16])


def issue(username, password_seal):
    """A session token for `username`, good for SESSION_HOURS or until their password changes."""
    claims = {"u": username, "p": password_seal, "exp": int(time.time()) + SESSION_HOURS * 3600}
    payload = _b64(json.dumps(claims).encode())
    return f"{payload}.{_signature(payload)}"


def _claims(token):
    """A session token's claims, or None if it is missing, forged, edited or expired."""
    payload, _, signature = (token or "").partition(".")
    if not signature or not hmac.compare_digest(signature.encode(), _signature(payload).encode()):
        return None
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    if claims["exp"] <= time.time() or "p" not in claims:  # "p": sessions from before seals existed
        return None
    return claims


def read_session(token):
    """(username, password seal) from a session token, or None if it is missing, forged, edited or expired."""
    claims = _claims(token)
    return (claims["u"], claims["p"]) if claims else None


def renewal(token):
    """A fresh token for the same user and password seal once less than half of `token`'s time is left,
    else None. security.Guard asks only after users.for_session has accepted the token."""
    claims = _claims(token)
    if not claims or claims["exp"] - time.time() > SESSION_HOURS * 3600 / 2:
        return None
    return issue(claims["u"], claims["p"])


def set_cookie(response, token, secure):
    """Gives the browser `token` as its session cookie. `secure` (HTTPS only) on a server; a local run is plain http."""
    response.set_cookie(COOKIE, token, max_age=SESSION_HOURS * 3600, httponly=True, samesite="lax", secure=secure)


def _accounts_named(username):
    """(username, password) rows of warehouse_users whose username matches, ignoring case and accents."""
    conn = get_scm_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT username, password FROM warehouse_users WHERE username = %s", (username,))
            return cur.fetchall()
    finally:
        conn.close()


def check_password(username, password):
    """The username as stored if `password` is its password, else None. Raises if the DB can't be reached."""
    for stored_name, stored_password in _accounts_named(username):
        # The SQL match also ignores accents; only a difference in case is meant to.
        if stored_name.casefold() == username.casefold() and hmac.compare_digest(
            stored_password.encode(), password.encode()
        ):
            return stored_name
    return None


def _recent_failures(key, now):
    return [t for t in _failures.get(key, ()) if now - t < FAILURE_WINDOW_SECONDS]


def locked_out(username):
    """True while `username` has had MAX_FAILURES wrong passwords within FAILURE_WINDOW_SECONDS."""
    with _failures_lock:
        return len(_recent_failures(username.casefold(), time.time())) >= MAX_FAILURES


def note_failure(username):
    now = time.time()
    with _failures_lock:
        for key in [k for k in _failures if not _recent_failures(k, now)]:
            del _failures[key]  # long-past failures: forgotten, so the dict can't grow without bound
        _failures[username.casefold()] = _recent_failures(username.casefold(), now) + [now]


def clear_failures(username):
    with _failures_lock:
        _failures.pop(username.casefold(), None)
