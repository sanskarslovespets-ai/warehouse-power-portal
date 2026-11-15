import json
import os
import threading

import google.auth
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CLIENT_SECRETS_FILE = os.path.join(BASE_DIR, "credentials.json")
TOKEN_FILE = os.path.join(BASE_DIR, "token.json")
SERVICE_ACCOUNT_FILE = os.path.join(
    BASE_DIR, os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
)
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]
UPLOAD_CHUNK_SIZE = 10 * 1024 * 1024

SHEET_HEADERS = [
    "Entry ID",
    "Created At",
    "Date (Damage marked)",
    "Date (RTO/RPU received)",
    "Facility Name",
    "Damage Type",
    "AWB Code",
    "SKU Code",
    "POD Link",
    "Video Link",
    "Courier Name",
    "Remarks",
]

_credentials_lock = threading.Lock()


class GoogleNotConnected(Exception):
    pass


# Set on Cloud Run: there the runtime service account (no key file) is used, through
# Application Default Credentials. The Sheet and both Drive folders must be shared with it.
ON_CLOUD_RUN = bool(os.environ.get("K_SERVICE"))


def is_connected():
    return ON_CLOUD_RUN or os.path.exists(SERVICE_ACCOUNT_FILE) or os.path.exists(TOKEN_FILE)


def service_account_email():
    """Returns the service account's address (for sharing instructions), or None."""
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        return None
    try:
        with open(SERVICE_ACCOUNT_FILE) as f:
            return json.load(f).get("client_email")
    except (OSError, ValueError):
        return None


def _credentials():
    # A service account key (if present) needs no browser step and is the
    # preferred path for this headless app.
    if os.path.exists(SERVICE_ACCOUNT_FILE):
        return service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    if ON_CLOUD_RUN:
        return google.auth.default(scopes=SCOPES)[0]

    with _credentials_lock:
        if not os.path.exists(TOKEN_FILE):
            raise GoogleNotConnected(
                "Google account is not connected. Run `python3 google_services.py` once to connect it."
            )
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        if creds.valid:
            return creds
        if not (creds.expired and creds.refresh_token):
            raise GoogleNotConnected(
                "Google login has expired. Run `python3 google_services.py` again to reconnect."
            )
        creds.refresh(Request())
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
        return creds


def diagnostics():
    """Best-effort connectivity check against the configured Sheet + Drive folders.

    Safe to call any time; never touches or prints the private key itself.
    """
    result = {"auth": None, "sheet": None, "pod_folder": None, "video_folder": None, "invoice_folder": None}
    try:
        creds = _credentials()
        result["auth"] = "ok (service account: %s)" % service_account_email() if os.path.exists(
            SERVICE_ACCOUNT_FILE
        ) else "ok (OAuth user)"
    except GoogleNotConnected as e:
        result["auth"] = f"not connected: {e}"
        return result

    try:
        sheets = build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()
        meta = sheets.get(spreadsheetId=os.environ["GOOGLE_SHEET_ID"], fields="properties.title").execute()
        result["sheet"] = "ok: " + meta["properties"]["title"]
    except Exception as e:
        result["sheet"] = f"error: {e}"

    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    for key, env_key in (("pod_folder", "DRIVE_POD_FOLDER_ID"), ("video_folder", "DRIVE_VIDEO_FOLDER_ID"),
                         ("invoice_folder", "DRIVE_INVOICE_FOLDER_ID")):  # the last for BoxVault (boxvault.py)
        try:
            meta = drive.files().get(
                fileId=os.environ[env_key], fields="id, name", supportsAllDrives=True
            ).execute()
            result[key] = "ok: " + meta["name"]
        except Exception as e:
            result[key] = f"error: {e}"
    return result


def upload_file(stream, filename, mimetype, folder_id, description=None):
    """Upload a file into a Drive folder and return its shareable view link."""
    drive = build("drive", "v3", credentials=_credentials(), cache_discovery=False)
    media = MediaIoBaseUpload(
        stream,
        mimetype=mimetype or "application/octet-stream",
        chunksize=UPLOAD_CHUNK_SIZE,
        resumable=True,
    )
    body = {"name": filename, "parents": [folder_id]}
    if description:
        body["description"] = description
    created = drive.files().create(
        body=body,
        media_body=media,
        fields="id, webViewLink",
        supportsAllDrives=True,
    ).execute()
    return created["webViewLink"]


def is_no_storage(error):
    """True for Drive's "Service Accounts do not have storage quota": a service account's upload into a folder in
    someone's My Drive. It owns what it uploads and has no storage there, so the folder must be in a shared drive."""
    return isinstance(error, HttpError) and b"storageQuotaExceeded" in (error.content or b"")


FOLDER_MIMETYPE = "application/vnd.google-apps.folder"


def subfolder(parent_id, name):
    """The id of the folder called `name` directly inside `parent_id`, made there if there isn't one."""
    drive = build("drive", "v3", credentials=_credentials(), cache_discovery=False)
    quoted = name.replace("\\", "\\\\").replace("'", "\\'")
    found = drive.files().list(
        q=f"'{parent_id}' in parents and name = '{quoted}' and mimeType = '{FOLDER_MIMETYPE}' and trashed = false",
        fields="files(id)",
        pageSize=1,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute().get("files", [])
    if found:
        return found[0]["id"]
    return drive.files().create(
        body={"name": name, "mimeType": FOLDER_MIMETYPE, "parents": [parent_id]},
        fields="id",
        supportsAllDrives=True,
    ).execute()["id"]


def _entry_to_row(entry):
    return [
        entry["id"],
        entry["created_at"],
        entry["damage_marked_date"],
        entry["received_date"],
        entry["facility_name"],
        entry["damage_type"],
        entry["awb_code"],
        entry["sku_code"],
        entry["pod_link"],
        entry["video_link"],
        entry["courier_name"],
        entry["remarks"],
    ]


def append_entries_to_sheet(entries):
    spreadsheet_id = os.environ["GOOGLE_SHEET_ID"]
    sheets = build("sheets", "v4", credentials=_credentials(), cache_discovery=False).spreadsheets()

    tab = os.environ.get("GOOGLE_SHEET_TAB")
    if not tab:
        meta = sheets.get(spreadsheetId=spreadsheet_id, fields="sheets.properties.title").execute()
        tab = meta["sheets"][0]["properties"]["title"]
    tab_ref = "'" + tab.replace("'", "''") + "'"

    header = sheets.values().get(spreadsheetId=spreadsheet_id, range=f"{tab_ref}!1:1").execute()
    if not header.get("values"):
        sheets.values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{tab_ref}!A1",
            valueInputOption="RAW",
            body={"values": [SHEET_HEADERS]},
        ).execute()

    # RAW keeps AWB/SKU codes as text (no number conversion) and never evaluates remarks as formulas
    sheets.values().append(
        spreadsheetId=spreadsheet_id,
        range=f"{tab_ref}!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [_entry_to_row(entry) for entry in entries]},
    ).execute()


if __name__ == "__main__":
    if os.path.exists(SERVICE_ACCOUNT_FILE):
        email = service_account_email()
        print(f"Using service account: {email}")
        print(
            "Share the two Drive folders and the Google Sheet with this address "
            "(Editor access) if you haven't already, then run:\n"
            "  python3 -c \"import google_services, json; "
            "print(json.dumps(google_services.diagnostics(), indent=2))\""
        )
        raise SystemExit(0)

    if not os.path.exists(CLIENT_SECRETS_FILE):
        raise SystemExit(
            f"Neither {SERVICE_ACCOUNT_FILE} nor {CLIENT_SECRETS_FILE} was found. See README.md, step 2."
        )
    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_FILE, SCOPES)
    creds = flow.run_local_server(port=0)
    with open(TOKEN_FILE, "w") as f:
        f.write(creds.to_json())
    print(f"Google account connected. Token saved to {TOKEN_FILE}")
