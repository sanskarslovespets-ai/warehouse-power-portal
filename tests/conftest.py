"""Shared by every test: an in-memory stand-in for scm.warehouse_users and warehouse_user_access, so no
test reaches MySQL, with two admins ("tester" and "wh.user") for tests that only need to be signed in;
and the Breach Explorer and the Control Tower kept in temporary folders, never reaching a database in the
background."""
import os
import sys
import time
import unicodedata

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth  # noqa: E402
import breach_explorer  # noqa: E402
import control_tower  # noqa: E402
import users  # noqa: E402


def loose(text):
    """How MySQL's utf8mb4_0900_ai_ci compares: ignoring case and accents."""
    return "".join(c for c in unicodedata.normalize("NFD", text) if not unicodedata.combining(c)).casefold()


class FakeAccounts:
    """Both tables as {username: [password, role, tabs, updated_at, updated_by]}; a role of None is no access row."""

    def __init__(self):
        self.rows = {}
        self.add("tester", "tester-pass", "admin")
        self.add("wh.user", "wh-user-pass", "admin")

    def add(self, username, password, role=None, tabs=()):
        self.rows[username] = [password, role, ",".join(tabs), None, None]

    def cookies(self, username):
        """The session cookie of `username`, as after signing in with their current password."""
        return {auth.COOKIE: auth.issue(username, auth.seal(self.rows[username][0]))}

    # what users.py and auth.py ask the database
    def _rows(self, username=None):
        names = sorted(self.rows) if username is None else [n for n in self.rows if n == username]
        return [(n, *self.rows[n]) for n in names]

    def _taken(self, username):
        return any(loose(n) == loose(username) for n in self.rows)

    def _save(self, username, role, tabs, by, password=None, new=False):
        if new:
            self.rows[username] = [password, None, "", None, None]
        elif password is not None:
            self.rows[username][0] = password
        self.rows[username][1:] = [role, ",".join(t for t in users.TABS if t in tabs), int(time.time()), by]

    def _remove(self, username):
        del self.rows[username]

    def _accounts_named(self, username):
        return [(n, row[0]) for n, row in self.rows.items() if loose(n) == loose(username)]


@pytest.fixture(autouse=True)
def accounts(monkeypatch):
    fake = FakeAccounts()
    for name in ("_rows", "_taken", "_save", "_remove"):
        monkeypatch.setattr(users, name, getattr(fake, name))
    monkeypatch.setattr(auth, "_accounts_named", fake._accounts_named)
    monkeypatch.setattr(users, "_cache", {})
    monkeypatch.setattr(users, "ADMIN_USERS", set())  # not whatever .env says
    monkeypatch.setattr(auth, "_failures", {})
    return fake


@pytest.fixture(autouse=True)
def breach(tmp_path, monkeypatch):
    """breach_explorer.py with no data yet, its files in a temporary folder. Anything that would start the
    exports in the background (a missed morning run, Refresh from DB) is recorded in .started instead; a test
    that wants a run calls refresh_cache() itself, on stand-in exports (test_breach_explorer.write_exports)."""
    monkeypatch.setattr(breach_explorer, "PIPELINE", tmp_path / "breach_pipeline")
    monkeypatch.setattr(breach_explorer, "CACHE_DIR", tmp_path / "breach_cache")
    monkeypatch.setattr(breach_explorer, "STATE_FILE", tmp_path / "breach_cache" / "state.json")
    for name, value in (("_loaded", False), ("_fetched", {}), ("_errors", {}), ("_payloads", {}), ("_queue", []),
                        ("_running", None), ("_run_started_at", None), ("_last_attempt_at", 0.0), ("_hurried", set())):
        monkeypatch.setattr(breach_explorer, name, value)
    started = []
    monkeypatch.setattr(breach_explorer, "refresh_cache_async", lambda force=False: started.append(force))
    monkeypatch.setattr(breach_explorer, "started", started, raising=False)
    return breach_explorer


@pytest.fixture(autouse=True)
def tower(tmp_path, monkeypatch):
    """control_tower.py with no data yet, its cache in a temporary folder. A build it would start in the background
    (stale data, Refresh data) is recorded in .started instead; a test that wants a build calls refresh_cache()
    itself, with fetch_erp and fetch_cp replaced (test_control_tower.py)."""
    monkeypatch.setattr(control_tower, "CACHE_FILE", str(tmp_path / "control_tower_cache.json.gz"))
    for name, value in (("_loaded", False), ("_payload", None), ("_fetched_at", None), ("_last_error", None),
                        ("_last_attempt_at", 0.0)):
        monkeypatch.setattr(control_tower, name, value)
    started = []
    monkeypatch.setattr(control_tower, "refresh_cache_async", lambda force=False: started.append(force))
    monkeypatch.setattr(control_tower, "started", started, raising=False)
    return control_tower
