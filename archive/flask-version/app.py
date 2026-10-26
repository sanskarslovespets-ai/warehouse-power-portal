import csv
import io
import os
import re
import traceback
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

import analytics
import analytics_mdnd
import analytics_recon
import analytics_status
import dropdown_cache
import entries_store
import mdnd_inputs
import rto_delivered
import rto_mdnd
import rto_recon
import google_services
from rto_process import awb_exists

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB per request (video uploads)

entries_store.init_db()

DAMAGE_TYPES = ["RTO", "RPU", "WH Damage", "Inward Damage"]

REQUIRED_TEXT_FIELDS = {
    "facility_name": "Facility Name",
    "damage_type": "Damage Type",
    "awb_code": "AWB Code",
    "sku_code": "SKU Code",
    "courier_name": "Courier Name",
}

def _parse_date(value, label, errors):
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError):
        errors.append(f"{label} must be a valid date.")


@app.get("/")
def home():
    return redirect(url_for("rto_process_panel"))


@app.get("/rto-process")
def rto_process_panel():
    # Refresh the facility/courier/SKU/AWB dropdown cache in the background so
    # the page never waits on a live DB query; any newly-seen values get
    # appended to dropdown_cache.csv for next time.
    dropdown_cache.refresh_cache_async()
    sheet_id = os.environ.get("GOOGLE_SHEET_ID", "").strip()
    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit" if sheet_id else None
    return render_template("index.html", sheet_url=sheet_url)


@app.get("/api/status")
def status():
    return jsonify(
        {
            "google_connected": google_services.is_connected(),
            "service_account_email": google_services.service_account_email(),
        }
    )


@app.get("/api/dropdown-options")
def dropdown_options():
    # Facility/Courier only - small enough to preload and filter client-side.
    # Never touches the DB: purely an instant in-memory cache read.
    data = dropdown_cache.get_cached_options(fields=dropdown_cache.SMALL_FIELDS)
    data["damage_type"] = DAMAGE_TYPES
    return jsonify(data)


@app.get("/api/dropdown-search")
def dropdown_search():
    # SKU/AWB - too large to preload (≈476K / ≈1.4M distinct values), so the
    # browser asks here instead. Still never touches the DB: it's an instant
    # in-memory substring search over the same background-refreshed cache.
    field = request.args.get("field", "")
    if field not in dropdown_cache.LARGE_FIELDS:
        return jsonify({"error": "Unknown or unsupported field."}), 400
    q = request.args.get("q", "")
    return jsonify({"results": dropdown_cache.search_cached(field, q)})


@app.get("/api/entries")
def list_entries():
    return jsonify({"entries": entries_store.list_entries()})


@app.get("/api/analytics")
def analytics_summary():
    date_field = request.args.get("date_field", "damage_marked_date")
    if date_field not in entries_store.ANALYTICS_DATE_FIELDS:
        return jsonify({"error": "Invalid date_field."}), 400

    start = request.args.get("from") or None
    end = request.args.get("to") or None
    for label, value in (("from", start), ("to", end)):
        if value:
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                return jsonify({"error": f"Invalid '{label}' date."}), 400

    damage_type = request.args.get("damage_type") or None
    if damage_type and damage_type not in DAMAGE_TYPES:
        return jsonify({"error": "Invalid damage_type."}), 400
    facility = (request.args.get("facility") or "").strip()[:255] or None
    courier = (request.args.get("courier") or "").strip()[:255] or None

    try:
        return jsonify(analytics.build_summary(date_field, start, end, damage_type, facility, courier))
    except Exception:
        traceback.print_exc()
        return jsonify({"error": "Could not compute analytics."}), 500


def _analytics_params(date_fields, filters):
    """Parse the query string shared by the Status / MDND / Reconciliation analytics routes.

    Returns (date_field, start, end, {filter: value}, error_response). `filters` maps the
    query parameter to the keyword the summary builder takes.
    """
    date_field = request.args.get("date_field") or date_fields[0]
    if date_field not in date_fields:
        return None, None, None, None, (jsonify({"error": "Invalid date_field."}), 400)
    bounds = []
    for label in ("from", "to"):
        value = request.args.get(label) or None
        if value:
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                return None, None, None, None, (jsonify({"error": f"Invalid '{label}' date."}), 400)
        bounds.append(value)
    chosen = {kw: (request.args.get(param) or "").strip()[:255] or None for param, kw in filters.items()}
    return date_field, bounds[0], bounds[1], chosen, None


def _analytics_route(module, filters, validators=None):
    date_field, start, end, chosen, err = _analytics_params(module.DATE_FIELDS, filters)
    if err:
        return err
    for kw, allowed in (validators or {}).items():
        if chosen[kw] and chosen[kw] not in allowed:
            return jsonify({"error": f"Invalid {kw}."}), 400
    try:
        return jsonify(module.build_summary(date_field, start, end, **chosen))
    except Exception:
        traceback.print_exc()
        return jsonify({"error": "Could not compute analytics."}), 500


@app.get("/api/analytics/status")
def analytics_status_summary():
    return _analytics_route(analytics_status, {"return_name": "return_name", "courier": "courier", "status": "status"})


@app.get("/api/analytics/mdnd")
def analytics_mdnd_summary():
    return _analytics_route(
        analytics_mdnd,
        {"warehouse": "warehouse", "courier": "courier", "reason": "reason", "state": "state"},
        {"warehouse": rto_mdnd.TABS, "state": ("checked", "pending")},
    )


@app.get("/api/analytics/recon")
def analytics_recon_summary():
    return _analytics_route(analytics_recon, {"return_name": "return_name", "courier": "courier", "erp_status": "erp_status"})


def _delivered_filters():
    """Parse/validate the shared filter params. Returns (filters, error_response)."""
    date_from = request.args.get("from") or None
    date_to = request.args.get("to") or None
    for label, value in (("from", date_from), ("to", date_to)):
        if value:
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                return None, (jsonify({"error": f"Invalid '{label}' date."}), 400)
    return {
        "date_from": date_from,
        "date_to": date_to,
        "return_name": request.args.get("return_name") or None,
        "status": request.args.get("status") or None,
        "search": request.args.get("q") or None,
    }, None


@app.get("/api/rto-delivered")
def rto_delivered_rows():
    # Served from the file-backed cache (rto_delivered.py); never waits on the DB.
    filters, err = _delivered_filters()
    if err:
        return err
    try:
        offset = max(int(request.args.get("offset", 0)), 0)
        limit = min(max(int(request.args.get("limit", 200)), 1), 1000)
    except ValueError:
        return jsonify({"error": "Invalid offset/limit."}), 400
    return jsonify(rto_delivered.query(offset=offset, limit=limit, **filters))


@app.get("/api/rto-delivered/summary")
def rto_delivered_summary():
    filters, err = _delivered_filters()
    if err:
        return err
    return jsonify(rto_delivered.summary(**filters))


@app.post("/api/rto-delivered/refresh")
def rto_delivered_refresh():
    rto_delivered.refresh_cache_async(force=True)
    return jsonify({"refreshing": True}), 202


@app.get("/api/rto-delivered/export")
def rto_delivered_export():
    filters, err = _delivered_filters()
    if err:
        return err
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=rto_delivered.COLUMNS)
    writer.writeheader()
    writer.writerows(rto_delivered.all_filtered(**filters))
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=rto_delivered.csv"},
    )


@app.get("/api/rto-mdnd")
def rto_mdnd_rows():
    tab = request.args.get("tab") or rto_mdnd.TABS[0]  # blank (first load) -> first tab
    if tab not in rto_mdnd.TABS:
        return jsonify({"error": "Unknown tab."}), 400
    data = rto_mdnd.query(tab)
    data["rows"] = mdnd_inputs.apply_inputs(data["rows"])
    return jsonify(data)


@app.post("/api/rto-mdnd/inputs")
def rto_mdnd_save_input():
    body = request.get_json(silent=True) or {}
    awb = str(body.get("awb") or "").strip()
    if not awb:
        return jsonify({"error": "AWB is required."}), 400

    values = {}
    for field in entries_store.MDND_INPUT_FIELDS:
        val = str(body.get(field) or "").strip()
        if len(val) > 1000:
            return jsonify({"error": "Value too long."}), 400
        if field in ("received_date", "date_updated") and val:
            try:
                datetime.strptime(val, "%Y-%m-%d")
            except ValueError:
                return jsonify({"error": "Invalid date."}), 400
        values[field] = val

    if entries_store.entries_by_awb().get(awb):  # auto-filled from the RTO Process form: not editable here
        values["received_at_wh"] = ""
        values["received_date"] = ""
    entries_store.save_mdnd_input(awb, values)
    return jsonify({"saved": True})


@app.post("/api/rto-mdnd/refresh")
def rto_mdnd_refresh():
    rto_mdnd.refresh_cache_async(force=True)
    return jsonify({"refreshing": True}), 202


@app.get("/api/rto-recon")
def rto_recon_rows():
    date_from = request.args.get("from") or None
    date_to = request.args.get("to") or None
    for label, value in (("from", date_from), ("to", date_to)):
        if value:
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                return jsonify({"error": f"Invalid '{label}' date."}), 400
    try:
        offset = max(int(request.args.get("offset", 0)), 0)
        limit = min(max(int(request.args.get("limit", 200)), 1), 1000)
    except ValueError:
        return jsonify({"error": "Invalid offset/limit."}), 400
    return jsonify(rto_recon.query(date_from, date_to, offset, limit))


@app.post("/api/rto-recon/refresh")
def rto_recon_refresh():
    rto_recon.refresh_cache_async(force=True)
    return jsonify({"refreshing": True}), 202


@app.post("/api/entries")
def create_entry():
    form = request.form
    errors = []

    damage_marked_date = form.get("damage_marked_date", "").strip()
    received_date = form.get("received_date", "").strip()
    _parse_date(damage_marked_date, "Date (Damage marked)", errors)
    _parse_date(received_date, "Date (RTO/RPU received)", errors)

    values = {
        "damage_marked_date": damage_marked_date,
        "received_date": received_date,
    }
    for field, label in REQUIRED_TEXT_FIELDS.items():
        val = form.get(field, "").strip()
        if not val:
            errors.append(f"{label} is required.")
        values[field] = val

    if values.get("damage_type") and values["damage_type"] not in DAMAGE_TYPES:
        errors.append("Damage Type must be one of: " + ", ".join(DAMAGE_TYPES))

    values["remarks"] = form.get("remarks", "").strip()

    pod_file = request.files.get("pod_file")
    video_file = request.files.get("video_file")
    if not pod_file or not pod_file.filename:
        errors.append("POD upload is required.")
    if not video_file or not video_file.filename:
        errors.append("Video upload is required.")

    if errors:
        return jsonify({"error": " ".join(errors)}), 400

    try:
        if not awb_exists(values["awb_code"]):
            return jsonify({"error": f"AWB {values['awb_code']} was not found in cp_database_N."}), 400
    except Exception:
        traceback.print_exc()
        return jsonify({"error": "Could not verify AWB against the database."}), 502

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_awb = re.sub(r"[^A-Za-z0-9_-]", "", values["awb_code"]) or "AWB"

    try:
        pod_ext = os.path.splitext(secure_filename(pod_file.filename))[1]
        video_ext = os.path.splitext(secure_filename(video_file.filename))[1]
        pod_link = google_services.upload_file(
            pod_file.stream,
            f"{safe_awb}_POD_{timestamp}{pod_ext}",
            pod_file.mimetype,
            os.environ["DRIVE_POD_FOLDER_ID"],
        )
        video_link = google_services.upload_file(
            video_file.stream,
            f"{safe_awb}_VIDEO_{timestamp}{video_ext}",
            video_file.mimetype,
            os.environ["DRIVE_VIDEO_FOLDER_ID"],
        )
    except google_services.GoogleNotConnected as e:
        return jsonify({"error": str(e)}), 503
    except Exception:
        traceback.print_exc()
        return jsonify({"error": "Upload to Google Drive failed. Please try again."}), 502

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

    return jsonify({"entry": entry}), 201


@app.post("/api/entries/sync")
def sync_entries():
    pending = entries_store.list_unsynced_entries()
    if not pending:
        return jsonify({"synced": 0, "message": "Nothing to sync."})
    try:
        google_services.append_entries_to_sheet(pending)
    except google_services.GoogleNotConnected as e:
        return jsonify({"error": str(e)}), 503
    except Exception:
        traceback.print_exc()
        return jsonify({"error": "Could not sync to the Google Sheet. Please try again."}), 502
    entries_store.mark_synced([e["id"] for e in pending])
    return jsonify({"synced": len(pending)})


@app.errorhandler(413)
def too_large(_e):
    return jsonify({"error": "Upload too large. Please keep each file under 500 MB."}), 413


if __name__ == "__main__":
    # Warm the dropdown cache in the background the moment the server actually
    # starts serving - not just when someone opens the dashboard - so even the
    # very first visitor after a restart isn't waiting on it. Deliberately
    # placed here (not at module level) so merely importing this file - e.g.
    # for a routes/syntax check - never triggers a live DB query on its own.
    # The dropdown/search endpoints never query the DB inline, so they stay
    # fast regardless of how long this takes (SKU/AWB scans can take a while
    # on ~1.4M rows).
    dropdown_cache.refresh_cache_async()
    # No-op if the cache files are still fresh. Only in the reloader's child process:
    # in debug mode the parent also executes this block, and two refreshes would run
    # the slow Delivery Note scan twice against the ERP DB.
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        rto_delivered.refresh_cache_async()
        rto_mdnd.refresh_cache_async()
        rto_recon.refresh_cache_async()
    app.run(host="127.0.0.1", port=5659, debug=True, threaded=True)
