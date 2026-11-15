"""BoxVault (Warehouse Dashboards): invoices uploaded for a mother warehouse, kept in Google Drive.

The panel's Upload invoices sends one warehouse and one or more files to /api/boxvault/invoices. Each
file goes into the Drive folder DRIVE_INVOICE_FOLDER_ID, inside a folder named after the warehouse's ERP
code (made the first time that warehouse gets an invoice), under the name it was uploaded with; the
Drive file's description says who uploaded it. The portal itself keeps nothing.
"""
import mimetypes
import os
import re
import threading
import traceback

import google_services

# The warehouses an invoice can be for, in dropdown order: {ERP warehouse code: what the dropdown shows}.
# The names shown are the team's own; New Bhiwandi warehouse 002 is MWH_Mum_Bhiwandi_002 in the ERP.
WAREHOUSES = {
    "MWH_Ggn_bhajgera_001": "WH_Delhi_002 · ERP: MWH_Ggn_bhajgera_001",
    "MWH_Blr_Neelmangla_002": "MWH_Blr_Neelmangla_002",
    "MWH_Mum_Bhiwandi_002": "New Bhiwandi warehouse 002",
    "MWH_Pun_Khed_001": "MWH_Pun_Khed_001",
    "MWH_Hyd_Manikonda_001": "MWH_Hyd_Manikonda_001",
    "MWH_Chn_Kovur_001": "MWH_Chn_Kovur_001",
    "MWH_Kol_Dankuni_001": "MWH_Kol_Dankuni_001",
}
# What an invoice may be: a PDF, a photo or scan, or a spreadsheet. Anything else (.html, .exe, ...) is
# refused rather than dropped into a shared Drive folder.
EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".xlsx", ".xls", ".csv")
MAX_FILES = 20  # per upload; all of them together are also held to main.MAX_CONTENT_LENGTH
MAX_NAME_LENGTH = 150
FOLDER_SETTING = "DRIVE_INVOICE_FOLDER_ID"

_folders = {}  # {warehouse: its Drive folder id}, found or made once per process
_folders_lock = threading.Lock()  # so two uploads at once can't both make a warehouse's folder


class Refused(ValueError):
    """An upload that can't be taken as sent; the message says what to change."""


class NotSetUp(RuntimeError):
    pass


class PartlyFailed(RuntimeError):
    """Drive took some of the files, or none; the message says which to send again."""

    def __init__(self, uploaded, failed):
        total = len(uploaded) + len(failed)
        if not uploaded:
            message = "Upload to Google Drive failed, so none of the files were saved. Please try again."
        else:
            message = (f"{len(uploaded)} of {total} files were uploaded, but these weren't: {', '.join(failed)}. "
                       "Please upload those again.")
        super().__init__(message)
        self.uploaded = uploaded
        self.failed = failed


def settings():
    """For the page: the dropdown, what the file picker takes, and whether uploads are set up on this server."""
    return {
        "warehouses": [{"id": code, "name": name} for code, name in WAREHOUSES.items()],
        "extensions": list(EXTENSIONS),
        "max_files": MAX_FILES,
        "ready": bool(os.environ.get(FOLDER_SETTING, "").strip()),
    }


def drive_name(filename):
    """The uploaded file's own name, kept readable: 'Inv #42/Oct.PDF' -> 'Oct.pdf', 'Inv #42.PDF' -> 'Inv _42.pdf'.

    Only the last part of a path; letters, digits, spaces and . _ - ( ) & kept, anything else made "_";
    the extension lower-case. Invoice numbers usually live in the name, so it isn't replaced.
    """
    stem, ext = os.path.splitext(filename.replace("\\", "/").rsplit("/", 1)[-1])
    stem = re.sub(r"_+", "_", re.sub(r"[^\w .()&-]", "_", stem, flags=re.ASCII)).strip(" ._") or "invoice"
    return stem[:MAX_NAME_LENGTH] + ext.lower()


def _extension(filename):
    return os.path.splitext(filename)[1].lower()


def _folder(parent, warehouse):
    with _folders_lock:
        if warehouse not in _folders:
            _folders[warehouse] = google_services.subfolder(parent, warehouse)
        return _folders[warehouse]


def upload(warehouse, files, by):
    """Puts `files` (FastAPI UploadFiles) in `warehouse`'s invoice folder, uploaded by `by`.

    Returns [{"name", "link"}] in the order given. Checks everything before sending anything, so a wrong
    file type stops the whole upload; if Drive then fails on some files, the rest are still sent and
    PartlyFailed names the ones that weren't.
    """
    parent = os.environ.get(FOLDER_SETTING, "").strip()
    if not parent:
        raise NotSetUp("Invoice uploads aren't set up on this server yet: it has no Google Drive folder for "
                       f"them ({FOLDER_SETTING}).")

    files = [f for f in files if f.filename]
    errors = []
    if warehouse not in WAREHOUSES:
        errors.append("Choose a warehouse.")
    if not files:
        errors.append("Add at least one invoice file.")
    elif len(files) > MAX_FILES:
        errors.append(f"Upload at most {MAX_FILES} files at a time.")
    wrong = [f.filename for f in files if _extension(f.filename) not in EXTENSIONS]
    if wrong:
        errors.append(f"Only PDFs, images and spreadsheets ({', '.join(EXTENSIONS)}) can be uploaded, "
                      f"not {', '.join(wrong)}.")
    empty = [f.filename for f in files if f.size == 0]
    if empty:
        errors.append(f"{', '.join(empty)} {'is' if len(empty) == 1 else 'are'} empty.")
    if errors:
        raise Refused(" ".join(errors))

    description = f"Invoice for {warehouse}, uploaded by {by} on BoxVault (Warehouse Power Portal)."
    uploaded, failed = [], []
    for f in files:
        name = drive_name(f.filename)
        try:
            # The type Drive stores comes from the checked extension, not the browser's claim.
            link = google_services.upload_file(f.file, name, mimetypes.guess_type(name)[0], _folder(parent, warehouse),
                                               description=description)
        except google_services.GoogleNotConnected:
            raise  # nothing will get through; main.py says why
        except Exception as e:
            if google_services.is_no_storage(e) and not uploaded:  # every file would fail the same way
                raise NotSetUp("Google Drive won't store the invoices: their folder is in a My Drive, where the "
                               "portal's service account has no storage. It needs to be in a shared drive.") from e
            traceback.print_exc()
            with _folders_lock:
                _folders.pop(warehouse, None)  # in case the folder was moved or deleted: look it up again next time
            failed.append(f.filename)
        else:
            uploaded.append({"name": name, "link": link})
    if failed:
        raise PartlyFailed(uploaded, failed)
    return uploaded
