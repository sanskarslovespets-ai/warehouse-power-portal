"""Who may open which tabs: each user's role and tabs, kept in scm.warehouse_user_access.

scm.warehouse_users (username, password) says who can sign in (auth.py); warehouse_user_access says
what they see once they have. A "tab" is one of the sidebar's sections, by its data-panel id in
static/index.html (TABS). Roles:
- admin:  every tab, plus Users, where they add people and set everyone's role and tabs;
- editor: the tabs ticked for them, and can change data there (new entries, MDND inputs, Refresh
          from DB, a Breach Explorer tab's Refresh data);
- viewer: the tabs ticked for them, to look at and download only.
Someone with no access row signs in to an empty panel until an admin gives them tabs. ADMIN_USERS
(an environment setting) names admins whatever the table says: the first admin, before anyone has
been given a role, and a way back in should every admin row be lost.

Nothing here writes to the database until an admin saves a change on the Users page; the first
such change creates warehouse_user_access. Until then everyone but ADMIN_USERS has no tabs.

security.Guard checks every request against lookup(), so a change - a tab taken away, a user deleted,
a password reset - reaches people already signed in within CACHE_SECONDS, not at their next sign-in.
New users' passwords are stored the way warehouse_users already stores them: as typed.
"""
import hmac
import os
import re
import threading
import time
from dataclasses import dataclass

from pymysql.constants import ER
from pymysql.err import OperationalError, ProgrammingError

import auth
from rto_process import get_scm_connection

ROLES = ("admin", "editor", "viewer")
TAB_GROUPS = (  # the sidebar's groups, each {data-panel id in static/index.html: name}, in sidebar order
    ("RTO", {
        "rto-analytics": "Analytics",
        "rto-process": "Process",
        "rto-status": "Status",
        "rto-mdnd": "MDND",
        "rto-recon": "Reconciliation",
    }),
    ("Breach Explorer", {  # breach_explorer.py
        "be-breach": "Breach analysis",
        "be-pendency": "Pendency",
        "be-complaints": "Complaints",
        "be-rto": "RTO",
        "be-connection": "Connection view",
        "be-radofd": "RAD to OFD",
    }),
    ("Warehouse Dashboards", {
        "control-tower": "Control Tower",  # control_tower.py
        "warehouse-exceptions": "Warehouse Exceptions",  # breach_explorer.py, dataset "warehouse"
        "boxvault": "BoxVault",  # boxvault.py: invoice uploads
    }),
)
TABS = {tab: name for _group, tabs in TAB_GROUPS for tab, name in tabs.items()}
TAB_GROUP = {tab: group for group, tabs in TAB_GROUPS for tab in tabs}
USERS_PANEL = "users"  # the admins' Users page

# The tab each API serves (the path itself, or anything under it). An /api/ path in none of these,
# ANY_SIGNED_IN or EDITORS is for admins only, so a new endpoint stays closed until it is listed here.
API_TABS = (
    ("/api/analytics", "rto-analytics"),
    ("/api/entries", "rto-process"),
    ("/api/dropdown-options", "rto-process"),
    ("/api/dropdown-search", "rto-process"),
    ("/api/status", "rto-process"),  # the Google connection and the sheet link, both for the Process tab
    ("/api/rto-delivered", "rto-status"),
    ("/api/rto-mdnd", "rto-mdnd"),
    ("/api/rto-recon", "rto-recon"),
    # each Breach Explorer path also covers .../<dataset>/refresh, the tab's Refresh data (a write: editors only)
    ("/api/breach-explorer/breach", "be-breach"),
    ("/api/breach-explorer/pendency", "be-pendency"),
    ("/api/breach-explorer/pendency-detail", "be-pendency"),  # the tab's shipment-level Export CSV
    ("/api/breach-explorer/complaints", "be-complaints"),
    ("/api/breach-explorer/rto", "be-rto"),
    ("/api/breach-explorer/connection", "be-connection"),  # and .../connection/awbs, its Export CSV
    ("/api/breach-explorer/radofd", "be-radofd"),
    ("/api/breach-explorer/warehouse", "warehouse-exceptions"),  # the Warehouse Exceptions page, and its Refresh data
    ("/api/control-tower", "control-tower"),  # and .../status, and .../refresh (a write: editors only)
    ("/api/boxvault", "boxvault"),  # and .../invoices, Upload invoices (a write: editors only)
)
# Any signed-in user: who they are, and how fresh the data is (no data itself)
ANY_SIGNED_IN = {"/api/me", "/api/data-status", "/api/breach-explorer/status"}
EDITORS = {"/api/refresh-all"}  # the top bar's Refresh from DB
READS = {"GET", "HEAD"}

ADMIN_USERS = {name.strip() for name in os.environ.get("ADMIN_USERS", "").split(",") if name.strip()}
USERNAME = re.compile(r"[A-Za-z0-9._@-]{3,64}")
MIN_PASSWORD_LENGTH = 8
CACHE_SECONDS = 60


class Refused(ValueError):
    """A change an admin asked for that can't be made; the message says why."""


class NoSuchUser(LookupError):
    pass


@dataclass(frozen=True)
class Access:
    username: str
    role: str
    tabs: frozenset  # an admin's is every tab
    seal: str  # auth.seal() of their password now: a session from before a password change won't match

    def as_json(self):
        """For the page (/api/me): which panels to show, and whether to show the buttons that change data."""
        panels = [t for t in TABS if t in self.tabs] + ([USERS_PANEL] if self.role == "admin" else [])
        return {"username": self.username, "role": self.role, "panels": panels, "can_edit": self.role != "viewer"}


def denial(access, method, path):
    """Why `access` may not make this request, or None if it may.

    Only /api/ paths are checked: the page and its files are the same for everyone, and only show
    each person their own tabs (static/js/session.js).
    """
    if access.role == "admin" or not path.startswith("/api/") or path in ANY_SIGNED_IN:
        return None
    if path in EDITORS:
        return None if access.role == "editor" else "Your access is view-only."
    tab = next((t for prefix, t in API_TABS if path == prefix or path.startswith(prefix + "/")), None)
    if tab is None:
        return "Only admins can do this."
    if tab not in access.tabs:
        return f"You don't have access to the {TABS[tab]} tab."
    if method not in READS and access.role != "editor":
        return f"Your access to the {TABS[tab]} tab is view-only."
    return None


# ------------------------------------------------------------------ who is signed in

_cache = {}  # username -> (when read, their Access or None)
_cache_lock = threading.Lock()


def lookup(username):
    """`username`'s access as the database has it (at most CACHE_SECONDS old), or None if they are no
    longer in warehouse_users.

    If the database can't be reached, the last answer is kept rather than signing everyone out; with
    no last answer, the error is raised.
    """
    with _cache_lock:
        hit = _cache.get(username)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    try:
        rows = _rows(username)
    except Exception:
        if hit:
            return hit[1]
        raise
    access = _access(rows[0]) if rows else None
    with _cache_lock:
        _cache[username] = (time.time(), access)
    return access


def forget(username):
    """Drops `username`'s cached access, so their next request reads it afresh."""
    with _cache_lock:
        _cache.pop(username, None)


def for_session(token):
    """The Access of whoever holds this session cookie, or None: no cookie, a forged or expired one, one
    from before their password was changed, or they have been removed since."""
    session = auth.read_session(token)
    if not session:
        return None
    access = lookup(session[0])
    if access is None or not hmac.compare_digest(access.seal, session[1]):
        return None
    return access


def _role(username, role):
    if username in ADMIN_USERS:
        return "admin"
    return role if role in ROLES else None  # None: no access row yet


def _access(row):
    username, password, role, tabs = row[:4]
    role = _role(username, role) or "viewer"
    return Access(username, role, frozenset(TABS) if role == "admin" else _tabs(tabs), auth.seal(password))


def _tabs(text):
    return frozenset(t for t in (text or "").split(",") if t in TABS)


# ------------------------------------------------------------------ the Users page (admins)

def list_users():
    """Everyone in warehouse_users with their role and tabs (never their passwords), for the Users page."""
    listed = {}
    for username, _password, role, tabs, updated_at, updated_by in _rows():
        role = _role(username, role)
        listed.setdefault(username, {
            "username": username,
            "role": role,
            "tabs": list(TABS) if role == "admin" else [t for t in TABS if t in _tabs(tabs)],
            "fixed_admin": username in ADMIN_USERS,
            "updated_at": int(updated_at) if updated_at is not None else None,  # epoch seconds
            "updated_by": updated_by,
        })
    return list(listed.values())


def create(username, password, role, tabs, by):
    """Adds a user who can sign in with `password` and open `tabs` as `role`."""
    username = username.strip()
    if not USERNAME.fullmatch(username):
        raise Refused("A username is 3 to 64 letters, digits, dots, dashes, underscores or @.")
    _check(role, tabs)
    _check_password(password)
    if _taken(username):
        raise Refused(f"There is already a user called {username}.")
    _save(username, role, tabs, by, password=password, new=True)
    forget(username)
    print(f"[users] {by!r} added {username!r} as {role} with tabs {sorted(tabs)}", flush=True)


def update(username, role, tabs, by, password=""):
    """Sets `username`'s role and tabs, and their password too if one is given."""
    _check(role, tabs)
    if password:
        _check_password(password)
    rows = _rows(username)
    if not rows:
        raise NoSuchUser(username)
    if role != "admin" and username in ADMIN_USERS:
        raise Refused(f"{username} is an admin through the ADMIN_USERS setting; change that setting first.")
    if username == by and role != _access(rows[0]).role:
        raise Refused("You can't change your own role. Ask another admin to.")
    _save(username, role, tabs, by, password=password or None)
    forget(username)
    print(f"[users] {by!r} set {username!r} to {role} with tabs {sorted(tabs)}"
          f"{' and a new password' if password else ''}", flush=True)


def delete(username, by):
    """Removes `username`: they can no longer sign in, and are signed out within CACHE_SECONDS."""
    if username == by:
        raise Refused("You can't delete your own account. Ask another admin to.")
    if username in ADMIN_USERS:
        raise Refused(f"{username} is an admin through the ADMIN_USERS setting; change that setting first.")
    if not _rows(username):
        raise NoSuchUser(username)
    _remove(username)
    forget(username)
    print(f"[users] {by!r} deleted {username!r}", flush=True)


def _check(role, tabs):
    if role not in ROLES:
        raise Refused("Choose a role: admin, editor or viewer.")
    unknown = [t for t in tabs if t not in TABS]
    if unknown:
        raise Refused(f"There is no tab called {unknown[0]}.")


def _check_password(password):
    if not MIN_PASSWORD_LENGTH <= len(password) <= auth.MAX_FIELD_LENGTH:
        raise Refused(f"A password is {MIN_PASSWORD_LENGTH} to {auth.MAX_FIELD_LENGTH} characters.")


# ------------------------------------------------------------------ the database

TABLE = """
CREATE TABLE IF NOT EXISTS warehouse_user_access (
  username   VARCHAR(255) NOT NULL PRIMARY KEY,
  role       VARCHAR(16)  NOT NULL,
  tabs       VARCHAR(512) NOT NULL DEFAULT '',
  updated_at TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  updated_by VARCHAR(255) NULL
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
"""

# An admin picks the exact name from the list, so reads and changes for one user match it exactly
# (utf8mb4_bin): the tables' own collation ignores case and accents, and "Sánskar" must not reach "Sanskar".
_JOINED = (
    "SELECT u.username, u.password, a.role, a.tabs, UNIX_TIMESTAMP(a.updated_at), a.updated_by "
    "FROM warehouse_users u LEFT JOIN warehouse_user_access a ON a.username = u.username"
)
_USERS_ONLY = "SELECT u.username, u.password, NULL, NULL, NULL, NULL FROM warehouse_users u"
_ONE = " WHERE u.username = %s COLLATE utf8mb4_bin"


def _no_access_table(error):
    return isinstance(error, ProgrammingError) and error.args[0] == ER.NO_SUCH_TABLE


def not_permitted(error):
    """True if MySQL refused a change because the panel's database account lacks the privilege for it."""
    return isinstance(error, OperationalError) and error.args[0] in (ER.TABLEACCESS_DENIED_ERROR, ER.DBACCESS_DENIED_ERROR)


def _rows(username=None):
    """(username, password, role, tabs, updated_at as epoch seconds, updated_by): everyone, or exactly `username`."""
    params = () if username is None else (username,)
    tail = " ORDER BY u.username" if username is None else _ONE
    conn = get_scm_connection()
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(_JOINED + tail, params)
            except ProgrammingError as e:
                if not _no_access_table(e):
                    raise
                cur.execute(_USERS_ONLY + tail, params)  # no one has been given a role yet
            return cur.fetchall()
    finally:
        conn.close()


def _taken(username):
    """True if warehouse_users has this name already, ignoring case and accents (no look-alike users)."""
    conn = get_scm_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM warehouse_users WHERE username = %s LIMIT 1", (username,))
            return cur.fetchone() is not None
    finally:
        conn.close()


def _save(username, role, tabs, by, password=None, new=False):
    """Writes the user's access row, and their warehouse_users row if new or given a password, in one
    transaction. The first save creates warehouse_user_access."""
    conn = get_scm_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = DATABASE() "
                "AND table_name = 'warehouse_user_access'"
            )
            if not cur.fetchone():
                cur.execute(TABLE)
            if new:
                cur.execute("INSERT INTO warehouse_users (username, password) VALUES (%s, %s)", (username, password))
            elif password is not None:
                cur.execute(
                    "UPDATE warehouse_users SET password = %s WHERE username = %s COLLATE utf8mb4_bin",
                    (password, username),
                )
            cur.execute(
                "INSERT INTO warehouse_user_access (username, role, tabs, updated_by) VALUES (%s, %s, %s, %s) AS new "
                "ON DUPLICATE KEY UPDATE role = new.role, tabs = new.tabs, updated_by = new.updated_by",
                (username, role, ",".join(t for t in TABS if t in tabs), by),
            )
        conn.commit()
    finally:
        conn.close()


def _remove(username):
    conn = get_scm_connection()
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("DELETE FROM warehouse_user_access WHERE username = %s COLLATE utf8mb4_bin", (username,))
            except ProgrammingError as e:
                if not _no_access_table(e):
                    raise
            cur.execute("DELETE FROM warehouse_users WHERE username = %s COLLATE utf8mb4_bin", (username,))
        conn.commit()
    finally:
        conn.close()
