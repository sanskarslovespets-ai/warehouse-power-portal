"""The panel's own sign-in: a username and password from scm.warehouse_users, on top of IAP.

IAP (security.py) decides which Google accounts can reach the panel at all; this decides which
of them may use it. The right username and password get a session cookie good for SESSION_HOURS,
and security.Guard turns away every request without one except the sign-in page itself
(PUBLIC_PATHS): a page load is sent to /login, an /api/ call gets a 401.

The cookie is the username and an expiry, signed with SESSION_SECRET, so it can't be forged or
edited, and every instance sharing that secret accepts it. It is HttpOnly (page scripts can't
read it) and SameSite=Lax.

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

# On a server, set SESSION_SECRET (the same value on every instance). Without it each start picks
# its own, so everyone is signed out whenever the server restarts.
_SECRET = os.environ.get("SESSION_SECRET", "").encode() or secrets.token_bytes(32)

_failures = {}  # casefolded username -> times of its recent wrong passwords
_failures_lock = threading.Lock()


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _signature(payload):
    return _b64(hmac.new(_SECRET, payload.encode(), hashlib.sha256).digest())


def issue(username):
    """A session token for `username`, good for SESSION_HOURS."""
    payload = _b64(json.dumps({"u": username, "exp": int(time.time()) + SESSION_HOURS * 3600}).encode())
    return f"{payload}.{_signature(payload)}"


def session_user(token):
    """Who a session token was issued to, or None if it is missing, forged, edited or expired."""
    payload, _, signature = (token or "").partition(".")
    if not signature or not hmac.compare_digest(signature.encode(), _signature(payload).encode()):
        return None
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    return claims["u"] if claims["exp"] > time.time() else None


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
