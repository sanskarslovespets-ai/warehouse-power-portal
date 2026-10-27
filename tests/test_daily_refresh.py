"""daily_refresh.py (the 9:00 IST schedule the caches go stale on) and the top bar's /api/data-status + /api/refresh-all."""
import os
import sys
import types
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import daily_refresh  # noqa: E402
from daily_refresh import IST  # noqa: E402

LOCAL = ("127.0.0.1", 50000)


def at(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=IST)


def test_last_and_next_run_default_to_nine_ist(monkeypatch):
    monkeypatch.delenv("DAILY_REFRESH_AT", raising=False)
    assert daily_refresh.last_run(at(26, 8, 59)) == at(25, 9)  # before 9:00 the last run was yesterday's
    assert daily_refresh.last_run(at(26, 9)) == at(26, 9)
    assert daily_refresh.last_run(at(26, 23, 30)) == at(26, 9)
    assert daily_refresh.next_run(at(26, 8, 59)) == at(26, 9) and daily_refresh.next_run(at(26, 9)) == at(27, 9)


def test_schedule_time_is_configurable_and_checked(monkeypatch):
    monkeypatch.setenv("DAILY_REFRESH_AT", "07:30")
    assert daily_refresh.last_run(at(26, 8)) == at(26, 7, 30)
    assert daily_refresh.schedule_info()["daily_at"] == "07:30"
    for bad in ("9am", "25:00", "09:60", "9"):
        monkeypatch.setenv("DAILY_REFRESH_AT", bad)
        with pytest.raises(ValueError, match="DAILY_REFRESH_AT"):
            daily_refresh.scheduled_time()


def test_data_is_due_once_it_predates_the_last_run(monkeypatch):
    monkeypatch.setattr(daily_refresh, "last_run", lambda now=None: at(26, 9))
    assert daily_refresh.is_due(None)
    assert daily_refresh.is_due(at(26, 8, 59).timestamp())  # fetched before this morning's run
    assert not daily_refresh.is_due(at(26, 9, 4).timestamp())


def test_every_cache_goes_stale_on_the_schedule(monkeypatch):
    import rto_delivered
    import rto_mdnd
    import rto_recon

    monkeypatch.setattr(daily_refresh, "last_run", lambda now=None: at(26, 9))
    for module, stamps in ((rto_delivered, ("_fetched_at", "_dn_fetched_at")), (rto_mdnd, ("_fetched_at", "_net_fetched_at")),
                           (rto_recon, ("_fetched_at",))):
        for name in stamps:
            monkeypatch.setattr(module, name, at(26, 9, 5).timestamp())
        assert not module._is_stale()
        monkeypatch.setattr(module, "_fetched_at", at(26, 8).timestamp())
        assert module._is_stale()
    monkeypatch.setattr(rto_delivered, "_dn_fetched_at", at(25, 9, 5).timestamp())
    assert rto_delivered._dn_is_stale()


class StopLoop(Exception):
    pass


def test_the_scheduler_runs_once_per_scheduled_time_and_survives_a_failure(monkeypatch):
    monkeypatch.delenv("DAILY_REFRESH_AT", raising=False)
    clock = {"now": at(26, 8, 58)}
    ticks = [at(26, 8, 59), at(26, 9), at(26, 9, 1), at(26, 18), at(27, 9)]

    def sleep(_seconds):
        if not ticks:
            raise StopLoop
        clock["now"] = ticks.pop(0)

    real_last_run = daily_refresh.last_run
    monkeypatch.setattr(daily_refresh, "last_run", lambda now=None: real_last_run(now or clock["now"]))
    monkeypatch.setattr(daily_refresh, "time", types.SimpleNamespace(sleep=sleep))
    runs = []

    def refresh():
        runs.append(clock["now"])
        if len(runs) == 1:
            raise RuntimeError("database unreachable")

    with pytest.raises(StopLoop):
        daily_refresh._loop(refresh)
    assert runs == [at(26, 9), at(27, 9)]


# ------------------------------------------------------------------ routes

@pytest.fixture()
def panel(monkeypatch):
    import main

    caches = {}
    for name in ("rto_delivered", "rto_mdnd", "rto_recon"):
        caches[name] = types.SimpleNamespace(
            __name__=name, started=[], busy=False,
            snapshot=lambda n=name: {"rows": [], "fetched_at": 100.0, "refreshing": caches[n].busy, "error": None,
                                     "loading": False},
        )
        caches[name].refresh_cache_async = lambda force=False, n=name: caches[n].started.append(force)
        caches[name].is_refreshing = lambda n=name: caches[n].busy
        monkeypatch.setattr(main, name, caches[name])  # the panels' own refresh routes too: no DB from a test
    monkeypatch.setattr(main, "DB_CACHES", {"RTO Status": caches["rto_delivered"], "RTO MDND": caches["rto_mdnd"],
                                            "RTO Reconciliation": caches["rto_recon"]})
    monkeypatch.setattr(main, "_forced_at", {})
    return types.SimpleNamespace(main=main, caches=caches, client=TestClient(main.app, client=LOCAL))


def test_data_status_lists_each_source_and_the_schedule(panel):
    data = panel.client.get("/api/data-status").json()
    assert [s["id"] for s in data["sources"]] == ["rto_delivered", "rto_mdnd", "rto_recon"]
    assert data["sources"][2] == {"id": "rto_recon", "label": "RTO Reconciliation", "fetched_at": 100.0,
                                  "refreshing": False, "error": None}
    assert data["schedule"]["timezone"] == "IST" and data["schedule"]["next_run"] - data["schedule"]["last_run"] == 86400


def test_refresh_all_forces_every_cache_then_cools_down(panel):
    r = panel.client.post("/api/refresh-all")
    assert r.status_code == 202 and r.json() == {"refreshing": ["rto_delivered", "rto_mdnd", "rto_recon"]}
    assert all(c.started == [True] for c in panel.caches.values())

    again = panel.client.post("/api/refresh-all")
    assert again.status_code == 429 and "few minutes ago" in again.json()["error"]

    panel.caches["rto_mdnd"].busy = True  # still running: not an error, nothing new started
    assert panel.client.post("/api/refresh-all").json() == {"refreshing": []}
    assert all(c.started == [True] for c in panel.caches.values())


def test_refresh_all_skips_a_cache_a_panel_just_refreshed(panel):
    assert panel.client.post("/api/rto-recon/refresh").status_code == 202
    assert panel.client.post("/api/refresh-all").json() == {"refreshing": ["rto_delivered", "rto_mdnd"]}
    assert panel.caches["rto_recon"].started == [True]


def test_the_morning_run_forces_every_cache(panel, monkeypatch):
    dropdown = []
    monkeypatch.setattr(panel.main.dropdown_cache, "refresh_cache_async", lambda: dropdown.append(1))
    panel.main._daily_refresh()
    assert dropdown == [1] and all(c.started == [True] for c in panel.caches.values())
    assert panel.main._forced_at == {}  # the schedule doesn't use up anyone's Refresh from DB
