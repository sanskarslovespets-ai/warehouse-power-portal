"""Warehouse Power Portal: a FastAPI app that serves the static UI in static/ and answers its /api/* requests.

Run locally (reads the SCM_DB_*, DN_DB_*, GOOGLE_* and DRIVE_* settings from .env):

    python3 main.py            # same as: python3 -m uvicorn main:app --reload --port 5659

then open http://localhost:5659 (interactive API docs at /docs).

On a server, set IAP_AUDIENCE and put the panel behind Identity-Aware Proxy: until then it only
answers requests from the machine it runs on (see security.py). Either way, everyone also signs
in with a username and password from scm.warehouse_users (see auth.py).
"""
import csv
import gzip
import io
import math
import mimetypes
import os
import re
import time
import traceback
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv
from fastapi import Body, Depends, FastAPI, Form, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException

import analytics
import analytics_mdnd
import auth
import analytics_recon
import analytics_status
import breach_explorer
import daily_refresh
import dropdown_cache
import entries_store
import mdnd_inputs
import rto_delivered
import rto_mdnd
import rto_recon
import google_services
import security
import users
from rto_process import awb_exists

load_dotenv()

STATIC = Path(__file__).parent / "static"
MAX_CONTENT_LENGTH = 500 * 1024 * 1024  # 500 MB per request (video uploads)
TOO_LARGE = "Upload too large. Please keep each file under 500 MB."

entries_store.init_db()

DAMAGE_TYPES = ["RTO", "RPU", "WH Damage", "Inward Damage"]

REQUIRED_TEXT_FIELDS = {
    "facility_name": "Facility Name",
    "damage_type": "Damage Type",
    "awb_code": "AWB Code",
    "sku_code": "SKU Code",
    "courier_name": "Courier Name",
}
MAX_TEXT_LENGTH = 255
MAX_REMARKS_LENGTH = 2000

# What each upload may be: the form's file pickers offer accept="image/*,.pdf" and "video/*".
# Anything else (.html, .exe, ...) is refused rather than dropped into a shared Drive folder.
POD_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".pdf"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".3gp", ".webm", ".mkv", ".avi"}

# The database-backed caches behind the dashboards, by the name the page shows them under. Each is
# re-pulled every morning (daily_refresh.py) and by the top bar's Refresh from DB.
DB_CACHES = {"RTO Status": rto_delivered, "RTO MDND": rto_mdnd, "RTO Reconciliation": rto_recon,
             "Breach Explorer": breach_explorer}
# The tabs each cache feeds: the top bar shows, and refreshes, only the caches behind someone's own tabs
# (so Refresh from DB on the RTO tabs doesn't also start the Breach Explorer's ten-minute run).
CACHE_TABS = {
    "rto_delivered": {"rto-analytics", "rto-status"},
    "rto_mdnd": {"rto-analytics", "rto-mdnd"},
    "rto_recon": {"rto-analytics", "rto-recon"},
    "breach_explorer": {tab for tab, group in users.TAB_GROUP.items() if group == "Breach Explorer"},
}

# A forced refresh re-runs the full source queries (the Delivery Note scan takes minutes on the
# ERP DB), so repeated clicks - or a script - must not be able to keep the DB busy.
FORCE_REFRESH_COOLDOWN_SECONDS = 600
_forced_at = {}

def _parse_date(value, label, errors):
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError):
        errors.append(f"{label} must be a valid date.")


@asynccontextmanager
async def lifespan(_app):
    # Warm the caches in the background the moment the server actually starts serving - not
    # just when someone opens the dashboard - so even the very first visitor after a restart
    # isn't waiting on them. Deliberately here (not at module level) so merely importing this
    # file - e.g. for a routes check or the tests - never triggers a live DB query on its own.
    # Each refresh is a no-op while its cache files are still fresh, and the endpoints never
    # query the DB inline, so they stay fast however long this takes (SKU/AWB scans can take a
    # while on ~1.4M rows). Under --reload only the serving process runs this, so the slow
    # Delivery Note scan never runs twice against the ERP DB.
    dropdown_cache.refresh_cache_async()
    for module in DB_CACHES.values():
        module.refresh_cache_async()
    daily_refresh.start(_daily_refresh)
    yield


def _daily_refresh():
    """The scheduled morning run: every cache is re-pulled in full, whatever its age."""
    print(f"[daily-refresh] {datetime.now():%Y-%m-%d %H:%M} refreshing every cache from the database", flush=True)
    dropdown_cache.refresh_cache_async()
    for module in DB_CACHES.values():
        module.refresh_cache_async(force=True)


# The API docs (/docs, /redoc, /openapi.json) map every endpoint: local runs only.
app = FastAPI(title="Warehouse Power Portal", lifespan=lifespan, openapi_url=None if security.IAP_AUDIENCE else "/openapi.json")


class LimitUploadSize:
    """Answers 413 for request bodies over MAX_CONTENT_LENGTH, before they are read."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        declared = Headers(scope=scope).get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_CONTENT_LENGTH:
            return await JSONResponse({"error": TOO_LARGE}, status_code=413)(scope, receive, send)
        received = 0

        async def limited_receive():
            # A chunked body declares no length: stop reading once it passes the cap. FastAPI
            # re-raises an HTTPException raised here as-is, so this still becomes a 413.
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > MAX_CONTENT_LENGTH:
                raise HTTPException(413, TOO_LARGE)
            return message

        await self.app(scope, limited_receive, send)


# Last added runs first: headers go on every response (denials included), and a request is
# allowed in before any of its body is read.
app.add_middleware(LimitUploadSize)
app.add_middleware(security.Guard)
app.add_middleware(security.SecurityHeaders)


@app.exception_handler(StarletteHTTPException)
async def http_error(_request, exc):
    # Every failure answers {"error": message}: that is what the frontend shows the user.
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def invalid_request(_request, exc):
    # A malformed parameter (e.g. offset=abc) is a 400 like any other bad input, not FastAPI's 422.
    names = sorted({next((p for p in reversed(e["loc"]) if isinstance(p, str)), "request") for e in exc.errors()})
    return JSONResponse({"error": f"Invalid {', '.join(names)}."}, status_code=400)


def _date(value, label):
    """Blank -> None; anything else must be a YYYY-MM-DD date."""
    if not value:
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, f"Invalid '{label}' date.")
    return value


def _date_range(date_from: Annotated[str, Query(alias="from")] = "", to: str = ""):
    """The optional ?from=&to= bounds shared by the filtered routes, as (from, to)."""
    return _date(date_from, "from"), _date(to, "to")


DateRange = Annotated[tuple, Depends(_date_range)]


def _clip(value):
    """A free-text filter: trimmed, capped at 255 characters, blank -> None."""
    return value.strip()[:255] or None


def _summary(build, *args, **kwargs):
    try:
        return build(*args, **kwargs)
    except Exception:
        traceback.print_exc()
        raise HTTPException(500, "Could not compute analytics.")


def _start_forced(module):
    """Starts a forced cache refresh unless one was started in the last FORCE_REFRESH_COOLDOWN_SECONDS."""
    now = time.time()
    if now - _forced_at.get(module.__name__, 0) < FORCE_REFRESH_COOLDOWN_SECONDS:
        return False
    _forced_at[module.__name__] = now
    module.refresh_cache_async(force=True)
    return True


def _force_refresh(module):
    """A panel's Refresh from DB button: one cache, at most once per cooldown."""
    if not _start_forced(module):
        raise HTTPException(429, "This data was refreshed a few minutes ago. Please try again later.")
    return {"refreshing": True}


def _csv_safe(value):
    """Spreadsheet apps run a cell starting with = + - @ (or tab / CR) as a formula; a leading ' keeps it text."""
    return "'" + value if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def _csv_download(name, columns, rows):
    """`rows` as a CSV attachment named <name>_<today>.csv - every Download CSV button that the server answers.

    Remarks, names and typed notes come from couriers, the ERP and staff, so every cell is made
    formula-safe. The leading BOM makes Excel read the file as UTF-8 (₹, non-English names).
    """
    buf = io.StringIO()
    buf.write("﻿")
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows({k: _csv_safe(v) for k, v in row.items()} for row in rows)
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={name}_{datetime.now():%Y-%m-%d}.csv"},
    )


@app.get("/rto-process", include_in_schema=False)
def rto_process_panel():
    return RedirectResponse("/")  # the page's old address; static/index.html is now served at /


# Sign-in (auth.py) and access (users.py). security.Guard sends every other request without a session here.

class Credentials(BaseModel):
    username: str = ""
    password: str = ""


def _access(request):
    """The signed-in user's users.Access (security.Guard has already checked the session)."""
    return users.for_session(request.cookies.get(auth.COOKIE))


def _start_session(request, response, username, password):
    response.set_cookie(
        auth.COOKIE, auth.issue(username, auth.seal(password)), max_age=auth.SESSION_HOURS * 3600, httponly=True,
        samesite="lax", secure=bool(security.IAP_AUDIENCE) or request.url.scheme == "https",  # a local run is plain http
    )


@app.get("/login", include_in_schema=False)
def login_page(request: Request):
    try:
        signed_in = _access(request)
    except Exception:
        signed_in = None  # can't check right now: show the form rather than an error
    if signed_in:
        return RedirectResponse("/", status_code=303)
    return FileResponse(STATIC / "login.html")


@app.post("/api/login")
def login(creds: Credentials, request: Request, response: Response):
    username = creds.username.strip()
    if not username or not creds.password:
        raise HTTPException(400, "Enter your username and password.")
    if auth.locked_out(username):
        raise HTTPException(429, "Too many wrong attempts. Please wait 15 minutes and try again.")
    user = None
    if len(username) <= auth.MAX_FIELD_LENGTH and len(creds.password) <= auth.MAX_FIELD_LENGTH:
        try:
            user = auth.check_password(username, creds.password)
        except Exception:
            traceback.print_exc()
            raise HTTPException(503, "Could not check your sign-in right now. Please try again.")
    if not user:
        # One message for both: saying which was wrong would tell a stranger which usernames exist.
        auth.note_failure(username)
        print(f"[auth] wrong username or password for {username!r}", flush=True)
        raise HTTPException(401, "Incorrect username or password.")
    auth.clear_failures(username)
    users.forget(user)  # read their access afresh: a password changed in the database may still be cached
    print(f"[auth] {user!r} signed in", flush=True)
    _start_session(request, response, user, creds.password)
    return {"username": user}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(auth.COOKIE, httponly=True, samesite="lax")
    return {"signed_out": True}


@app.get("/api/me")
def me(request: Request):
    """The signed-in user, and which panels the page shows them."""
    return _access(request).as_json()


# Users (admins only: users.denial): everyone who can sign in, their role, and which tabs they see.

class UserForm(BaseModel):
    username: str = ""
    password: str = ""
    role: str = "viewer"
    tabs: list[str] = []


def _change_users(change, *args, **kwargs):
    """Runs a users.py change, turning its refusals into the message the Users page shows."""
    try:
        change(*args, **kwargs)
    except users.Refused as e:
        raise HTTPException(400, str(e))
    except users.NoSuchUser:
        raise HTTPException(404, "There is no such user. Reload the list and try again.")
    except Exception:
        traceback.print_exc()
        raise HTTPException(503, "Could not save the change to the database. Please try again.")


@app.get("/api/users")
def list_users(request: Request):
    try:
        listed = users.list_users()
    except Exception:
        traceback.print_exc()
        raise HTTPException(503, "Could not read the users from the database. Please try again.")
    return {
        "users": listed,
        "tabs": [{"id": tab, "name": name, "group": users.TAB_GROUP[tab]} for tab, name in users.TABS.items()],
        "you": _access(request).username,
    }


@app.post("/api/users", status_code=201)
def create_user(form: UserForm, request: Request):
    _change_users(users.create, form.username, form.password, form.role, form.tabs, by=_access(request).username)
    return {"saved": True}


@app.put("/api/users/{username:path}")
def update_user(username: str, form: UserForm, request: Request, response: Response):
    you = _access(request).username
    _change_users(users.update, username, form.role, form.tabs, by=you, password=form.password)
    if username == you and form.password:
        _start_session(request, response, you, form.password)  # a new password ends older sessions, not this one
    return {"saved": True}


@app.delete("/api/users/{username:path}")
def delete_user(username: str, request: Request):
    _change_users(users.delete, username, by=_access(request).username)
    return {"deleted": True}


@app.get("/api/status")
def status():
    # Called on page load for users with the Process tab, so it also refreshes the facility/courier/SKU/AWB dropdown
    # cache in the background (throttled - see dropdown_cache.refresh_cache) so the page never
    # waits on a live DB query; any newly-seen values get appended to dropdown_cache.csv.
    dropdown_cache.refresh_cache_async()
    sheet_id = os.environ.get("GOOGLE_SHEET_ID", "").strip()
    return {
        "google_connected": google_services.is_connected(),
        "sheet_url": f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit" if sheet_id else None,
    }


def _your_caches(request):
    """The DB_CACHES behind the signed-in user's tabs (all of them, for an admin)."""
    tabs = _access(request).tabs
    return {label: module for label, module in DB_CACHES.items() if CACHE_TABS.get(module.__name__, tabs) & tabs}


@app.get("/api/data-status")
def data_status(request: Request):
    """The top bar: when the data behind your tabs was last pulled from the database, and the daily schedule.

    Reading a cache's state also starts its refresh when it has missed the morning run (server off
    or asleep at that time), so the first visit of the day catches up.
    """
    sources = []
    for label, module in _your_caches(request).items():
        state = module.snapshot()
        sources.append({"id": module.__name__, "label": label, "fetched_at": state["fetched_at"],
                        "refreshing": state["refreshing"], "error": state["error"]})
    return {"schedule": daily_refresh.schedule_info(), "sources": sources}


@app.post("/api/refresh-all", status_code=202)
def refresh_all(request: Request):
    """The top bar's Refresh from DB: re-pulls each cache behind your tabs not already re-pulled in the last few minutes."""
    caches = _your_caches(request).values()
    started = [module.__name__ for module in caches if _start_forced(module)]
    if not started and not any(module.is_refreshing() for module in caches):
        raise HTTPException(429, "All data was refreshed from the database a few minutes ago. Please try again later.")
    return {"refreshing": started}


@app.get("/api/dropdown-options")
def dropdown_options():
    # Facility/Courier only - small enough to preload and filter client-side.
    # Never touches the DB: purely an instant in-memory cache read.
    data = dropdown_cache.get_cached_options(fields=dropdown_cache.SMALL_FIELDS)
    data["damage_type"] = DAMAGE_TYPES
    return data


@app.get("/api/dropdown-search")
def dropdown_search(field: str = "", q: str = ""):
    # SKU/AWB - too large to preload (≈476K / ≈1.4M distinct values), so the
    # browser asks here instead. Still never touches the DB: it's an instant
    # in-memory substring search over the same background-refreshed cache.
    if field not in dropdown_cache.LARGE_FIELDS:
        raise HTTPException(400, "Unknown or unsupported field.")
    return {"results": dropdown_cache.search_cached(field, q[:MAX_TEXT_LENGTH])}


@app.get("/api/entries")
def list_entries():
    return {"entries": entries_store.list_entries()}


# Each analytics dashboard has two routes sharing one set of checked filters: the summary its cards
# draw, and /export - the rows behind those cards, for its Download CSV button.

def _process_args(
    dates: DateRange, date_field: str = "damage_marked_date", damage_type: str = "", facility: str = "", courier: str = ""
):
    """The RTO Process dashboard's filters, checked, as keyword arguments of analytics.build_summary / export_rows."""
    if date_field not in entries_store.ANALYTICS_DATE_FIELDS:
        raise HTTPException(400, "Invalid date_field.")
    if damage_type and damage_type not in DAMAGE_TYPES:
        raise HTTPException(400, "Invalid damage_type.")
    return {"date_field": date_field, "start_str": dates[0], "end_str": dates[1],
            "damage_type": damage_type or None, "facility": _clip(facility), "courier": _clip(courier)}


ProcessArgs = Annotated[dict, Depends(_process_args)]


@app.get("/api/analytics")
def analytics_summary(args: ProcessArgs):
    return _summary(analytics.build_summary, **args)


@app.get("/api/analytics/export")
def analytics_export(args: ProcessArgs):
    return _csv_download("rto_process_analytics", entries_store.EXPORT_COLUMNS, analytics.export_rows(**args))


def _tab_args(module, date_field, dates, validators=None, **filters):
    """Shared by the Status / MDND / Reconciliation analytics routes: their filters, checked, as
    keyword arguments of module.build_summary / module.export_rows.

    `filters` are the free-text query params, by the keyword the summary builder takes;
    `validators` limits some of them to a fixed set of values.
    """
    date_field = date_field or module.DATE_FIELDS[0]
    if date_field not in module.DATE_FIELDS:
        raise HTTPException(400, "Invalid date_field.")
    chosen = {kw: _clip(value) for kw, value in filters.items()}
    for kw, allowed in (validators or {}).items():
        if chosen[kw] and chosen[kw] not in allowed:
            raise HTTPException(400, f"Invalid {kw}.")
    return {"date_field": date_field, "start_str": dates[0], "end_str": dates[1], **chosen}


def _status_args(dates: DateRange, date_field: str = "", return_name: str = "", courier: str = "", status: str = ""):
    return _tab_args(analytics_status, date_field, dates, return_name=return_name, courier=courier, status=status)


def _mdnd_args(
    dates: DateRange, date_field: str = "", warehouse: str = "", courier: str = "", reason: str = "", state: str = ""
):
    return _tab_args(
        analytics_mdnd, date_field, dates, {"warehouse": rto_mdnd.TABS, "state": ("checked", "pending")},
        warehouse=warehouse, courier=courier, reason=reason, state=state,
    )


def _recon_args(dates: DateRange, date_field: str = "", return_name: str = "", courier: str = "", erp_status: str = ""):
    return _tab_args(analytics_recon, date_field, dates, return_name=return_name, courier=courier, erp_status=erp_status)


StatusArgs = Annotated[dict, Depends(_status_args)]
MdndArgs = Annotated[dict, Depends(_mdnd_args)]
ReconArgs = Annotated[dict, Depends(_recon_args)]


@app.get("/api/analytics/status")
def analytics_status_summary(args: StatusArgs):
    return _summary(analytics_status.build_summary, **args)


@app.get("/api/analytics/status/export")
def analytics_status_export(args: StatusArgs):
    return _csv_download("rto_status_analytics", rto_delivered.COLUMNS, analytics_status.export_rows(**args))


@app.get("/api/analytics/mdnd")
def analytics_mdnd_summary(args: MdndArgs):
    return _summary(analytics_mdnd.build_summary, **args)


@app.get("/api/analytics/mdnd/export")
def analytics_mdnd_export(args: MdndArgs):
    columns = rto_mdnd.COLUMNS + entries_store.MDND_INPUT_FIELDS
    return _csv_download("rto_mdnd_analytics", columns, analytics_mdnd.export_rows(**args))


@app.get("/api/analytics/recon")
def analytics_recon_summary(args: ReconArgs):
    return _summary(analytics_recon.build_summary, **args)


@app.get("/api/analytics/recon/export")
def analytics_recon_export(args: ReconArgs):
    return _csv_download("rto_reconciliation_analytics", rto_recon.COLUMNS, analytics_recon.export_rows(**args))


def _delivered_filters(dates: DateRange, return_name: str = "", status: str = "", q: str = ""):
    """The filter params shared by the RTO Delivered routes, as rto_delivered keyword arguments."""
    return {
        "date_from": dates[0],
        "date_to": dates[1],
        "return_name": return_name or None,
        "status": status or None,
        "search": q or None,
    }


DeliveredFilters = Annotated[dict, Depends(_delivered_filters)]


@app.get("/api/rto-delivered")
def rto_delivered_rows(filters: DeliveredFilters, offset: int = 0, limit: int = 200):
    # Served from the file-backed cache (rto_delivered.py); never waits on the DB.
    return rto_delivered.query(offset=max(offset, 0), limit=min(max(limit, 1), 1000), **filters)


@app.get("/api/rto-delivered/summary")
def rto_delivered_summary(filters: DeliveredFilters):
    return rto_delivered.summary(**filters)


@app.post("/api/rto-delivered/refresh", status_code=202)
def rto_delivered_refresh():
    return _force_refresh(rto_delivered)


@app.get("/api/rto-delivered/export")
def rto_delivered_export(filters: DeliveredFilters):
    return _csv_download("rto_delivered", rto_delivered.COLUMNS, rto_delivered.all_filtered(**filters))


@app.get("/api/rto-mdnd")
def rto_mdnd_rows(tab: str = ""):
    tab = tab or rto_mdnd.TABS[0]  # blank (first load) -> first tab
    if tab not in rto_mdnd.TABS:
        raise HTTPException(400, "Unknown tab.")
    data = rto_mdnd.query(tab)
    data["rows"] = mdnd_inputs.apply_inputs(data["rows"])
    return data


@app.post("/api/rto-mdnd/inputs")
def rto_mdnd_save_input(body: Annotated[dict, Body()]):
    awb = str(body.get("awb") or "").strip()
    if not awb:
        raise HTTPException(400, "AWB is required.")
    if len(awb) > MAX_TEXT_LENGTH:
        raise HTTPException(400, "Invalid AWB.")

    values = {}
    for field in entries_store.MDND_INPUT_FIELDS:
        val = str(body.get(field) or "").strip()
        if len(val) > 1000:
            raise HTTPException(400, "Value too long.")
        if field in ("received_date", "date_updated") and val:
            try:
                datetime.strptime(val, "%Y-%m-%d")
            except ValueError:
                raise HTTPException(400, "Invalid date.")
        values[field] = val

    if entries_store.entries_by_awb().get(awb):  # auto-filled from the RTO Process form: not editable here
        values["received_at_wh"] = ""
        values["received_date"] = ""
    entries_store.save_mdnd_input(awb, values)
    return {"saved": True}


@app.post("/api/rto-mdnd/refresh", status_code=202)
def rto_mdnd_refresh():
    return _force_refresh(rto_mdnd)


def _recon_filters(dates: DateRange, date_field: str = "", q: str = "", courier: str = "", return_name: str = "",
                   erp_status: str = "", age: str = ""):
    """The RTO Reconciliation panel's filters, checked, as rto_recon keyword arguments."""
    date_field = date_field or rto_recon.DATE_FIELDS[0]
    if date_field not in rto_recon.DATE_FIELDS:
        raise HTTPException(400, "Invalid date_field.")
    if age and age not in rto_recon.AGE_LABELS:
        raise HTTPException(400, "Invalid age.")
    return {"date_field": date_field, "date_from": dates[0], "date_to": dates[1], "search": _clip(q),
            "courier": _clip(courier), "return_name": _clip(return_name), "erp_status": _clip(erp_status),
            "age": age or None}


ReconFilters = Annotated[dict, Depends(_recon_filters)]


@app.get("/api/rto-recon")
def rto_recon_rows(filters: ReconFilters, offset: int = 0, limit: int = 200):
    return rto_recon.query(offset=max(offset, 0), limit=min(max(limit, 1), 1000), **filters)


@app.get("/api/rto-recon/export")
def rto_recon_export(filters: ReconFilters):
    return _csv_download("rto_reconciliation", rto_recon.COLUMNS, rto_recon.all_filtered(**filters))


@app.post("/api/rto-recon/refresh", status_code=202)
def rto_recon_refresh():
    return _force_refresh(rto_recon)


# Breach Explorer (breach_explorer.py): each tab's data as the SLA Breach Explorer's own export script wrote it.
# users.API_TABS gives each dataset to its tab, so someone without a tab can't fetch its data.

@app.get("/api/breach-explorer/status")
def breach_explorer_status():
    return breach_explorer.status()


@app.post("/api/breach-explorer/{dataset}/refresh", status_code=202)
def breach_explorer_refresh(dataset: str):
    """A tab's Refresh data (static/js/be_common.js): re-runs that tab's export now, or straight after the ones
    already running. Answers with the status, which the tab then follows until its new data is in."""
    if dataset not in breach_explorer.TAB_EXPORTS:
        raise HTTPException(404, "There is no such Breach Explorer tab.")
    age = breach_explorer.refresh_tab(dataset, FORCE_REFRESH_COOLDOWN_SECONDS)
    if age is not None:
        old = "less than a minute" if age < 60 else f"{int(age // 60)} min"
        wait = math.ceil((FORCE_REFRESH_COOLDOWN_SECONDS - age) / 60)
        raise HTTPException(429, f"This tab's data is {old} old. You can refresh it again in {wait} min.")
    return breach_explorer.status()


@app.get("/api/breach-explorer/{dataset}/awbs")
def breach_explorer_awbs(
    dataset: str,
    dates: DateRange,
    courier: Annotated[list[str], Query()] = [],
    warehouse: Annotated[list[str], Query()] = [],
    account: Annotated[list[str], Query()] = [],
    slab: Annotated[list[str], Query()] = [],
):
    """Connection view / RAD to OFD: Export CSV, one row per shipment, for the tab's dates and filters."""
    if dataset not in breach_explorer.AWB_SPECS:
        raise HTTPException(404, "Shipment-level export is only on the Connection view and RAD to OFD tabs.")
    date_from, date_to = dates
    if not date_from or not date_to:
        raise HTTPException(400, "Pick the dates to export first.")
    days = (date.fromisoformat(date_to) - date.fromisoformat(date_from)).days + 1
    if days < 1:
        raise HTTPException(400, "The start date is after the end date.")
    if days > breach_explorer.AWB_MAX_DAYS:
        raise HTTPException(400, f"Export at most {breach_explorer.AWB_MAX_DAYS} days of shipments at a time.")
    filters = [[_clip(v) for v in values if _clip(v)][:500] for values in (courier, warehouse, account, slab)]
    try:
        columns, rows, truncated = breach_explorer.awb_rows(dataset, date_from, date_to, *filters)
    except breach_explorer.Busy:
        raise HTTPException(429, "Two shipment exports are already running. Please try again in a minute.")
    except Exception:
        traceback.print_exc()
        raise HTTPException(503, "Could not read the shipments from the database. Please try again.")
    buf = io.StringIO()
    buf.write("﻿")  # Excel reads the file as UTF-8
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(columns)
    writer.writerows([_csv_safe("" if row[c] is None else row[c]) for c in columns] for row in rows)
    if truncated:
        writer.writerow([f"TRUNCATED at {breach_explorer.AWB_ROW_CAP} rows - narrow the filters"])
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={dataset}_awbs_{date_from}_to_{date_to}.csv"},
    )


@app.get("/api/breach-explorer/{dataset}")
def breach_explorer_data(dataset: str, request: Request):
    """One tab's data: the JSON its export wrote, gzipped once when it came in. A page that already holds this
    version sends its ETag back (If-None-Match) and gets an empty 304, so new data for another tab doesn't
    cost this one a download. (API responses are never cached by the browser: security.SecurityHeaders.)"""
    if dataset not in breach_explorer.DATASETS:
        raise HTTPException(404, "There is no such Breach Explorer data.")
    found = breach_explorer.payload(dataset)
    if not found:
        raise HTTPException(503, "This tab has no data yet.")
    gz, etag = found
    headers = {"ETag": etag, "Vary": "Accept-Encoding"}
    if etag in request.headers.get("if-none-match", ""):
        return Response(status_code=304, headers=headers)
    if "gzip" in request.headers.get("accept-encoding", ""):
        return Response(gz, media_type="application/json", headers={**headers, "Content-Encoding": "gzip"})
    return Response(gzip.decompress(gz), media_type="application/json", headers=headers)


class EntryForm(BaseModel):
    """The "+ New Entry" form's text fields. Checked in create_entry, so every problem is reported at once."""

    damage_marked_date: str = ""
    received_date: str = ""
    facility_name: str = ""
    damage_type: str = ""
    awb_code: str = ""
    sku_code: str = ""
    courier_name: str = ""
    remarks: str = ""


def _extension(filename):
    """'clip.MOV' -> '.MOV'. Letters and digits only, so an uploaded name can't shape the Drive file name."""
    ext = re.sub(r"[^A-Za-z0-9]", "", os.path.splitext(filename)[1])
    return "." + ext if ext else ""


@app.post("/api/entries", status_code=201)
def create_entry(
    fields: Annotated[EntryForm, Form()],
    pod_file: UploadFile | None = None,
    video_file: UploadFile | None = None,
):
    form = fields.model_dump()
    errors = []

    damage_marked_date = form["damage_marked_date"].strip()
    received_date = form["received_date"].strip()
    _parse_date(damage_marked_date, "Date (Damage marked)", errors)
    _parse_date(received_date, "Date (RTO/RPU received)", errors)

    values = {
        "damage_marked_date": damage_marked_date,
        "received_date": received_date,
    }
    for field, label in REQUIRED_TEXT_FIELDS.items():
        val = form[field].strip()
        if not val:
            errors.append(f"{label} is required.")
        elif len(val) > MAX_TEXT_LENGTH:
            errors.append(f"{label} is too long.")
        values[field] = val

    if values.get("damage_type") and values["damage_type"] not in DAMAGE_TYPES:
        errors.append("Damage Type must be one of: " + ", ".join(DAMAGE_TYPES))

    values["remarks"] = form["remarks"].strip()
    if len(values["remarks"]) > MAX_REMARKS_LENGTH:
        errors.append(f"Remarks must be at most {MAX_REMARKS_LENGTH} characters.")

    for upload, label, allowed in ((pod_file, "POD", POD_EXTENSIONS), (video_file, "Video", VIDEO_EXTENSIONS)):
        if not upload or not upload.filename:
            errors.append(f"{label} upload is required.")
        elif _extension(upload.filename).lower() not in allowed:
            errors.append(f"{label} must be one of: {', '.join(sorted(allowed))}.")

    if errors:
        raise HTTPException(400, " ".join(errors))

    try:
        found = awb_exists(values["awb_code"])
    except Exception:
        traceback.print_exc()
        raise HTTPException(502, "Could not verify AWB against the database.")
    if not found:
        raise HTTPException(400, f"AWB {values['awb_code']} was not found in cp_database_N.")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_awb = re.sub(r"[^A-Za-z0-9_-]", "", values["awb_code"]) or "AWB"

    pod_name = f"{safe_awb}_POD_{timestamp}{_extension(pod_file.filename).lower()}"
    video_name = f"{safe_awb}_VIDEO_{timestamp}{_extension(video_file.filename).lower()}"
    try:
        # The type Drive stores comes from the checked extension, not the browser's claim.
        pod_link = google_services.upload_file(
            pod_file.file, pod_name, mimetypes.guess_type(pod_name)[0], os.environ["DRIVE_POD_FOLDER_ID"]
        )
        video_link = google_services.upload_file(
            video_file.file, video_name, mimetypes.guess_type(video_name)[0], os.environ["DRIVE_VIDEO_FOLDER_ID"]
        )
    except google_services.GoogleNotConnected as e:
        raise HTTPException(503, str(e))
    except Exception:
        traceback.print_exc()
        raise HTTPException(502, "Upload to Google Drive failed. Please try again.")

    values["pod_link"] = pod_link
    values["video_link"] = video_link

    entry = entries_store.add_entry(values)

    try:
        google_services.append_entries_to_sheet([entry])
        entries_store.mark_synced([entry["id"]])
        entry["sheet_synced"] = 1
    except Exception:
        traceback.print_exc()
        # Entry is safely stored locally; it can be pushed later via /api/entries/sync.

    return {"entry": entry}


@app.post("/api/entries/sync")
def sync_entries():
    pending = entries_store.list_unsynced_entries()
    if not pending:
        return {"synced": 0, "message": "Nothing to sync."}
    try:
        google_services.append_entries_to_sheet(pending)
    except google_services.GoogleNotConnected as e:
        raise HTTPException(503, str(e))
    except Exception:
        traceback.print_exc()
        raise HTTPException(502, "Could not sync to the Google Sheet. Please try again.")
    entries_store.mark_synced([e["id"] for e in pending])
    return {"synced": len(pending)}


# Mount LAST. It catches every path not matched above, so any route declared after this
# line is unreachable. html=True makes / serve static/index.html.
app.mount("/", StaticFiles(directory=STATIC, html=True), name="ui")


if __name__ == "__main__":
    import uvicorn

    # The analytics-scripts/ clone inside this folder is its own project: editing it shouldn't restart this server.
    uvicorn.run("main:app", host="127.0.0.1", port=5659, reload=True, server_header=False,
                reload_excludes=[str(Path(__file__).resolve().parent / "analytics-scripts")])
