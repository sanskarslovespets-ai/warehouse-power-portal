"""BoxVault (Warehouse Dashboards): invoices uploaded for a mother warehouse, kept in Google Drive, and the
line items of the PDF ones in Invoice Entry.

The panel's Upload invoices sends one warehouse and one or more files to /api/boxvault/invoices. Each
file goes into the Drive folder DRIVE_INVOICE_FOLDER_ID, inside a folder named after the warehouse's ERP
code (made the first time that warehouse gets an invoice), under the name it was uploaded with; the
Drive file's description says who uploaded it. A supplier's PDF tax invoice is also read (invoice_pdf.py)
and its lines saved for that warehouse (invoice_entries.py); one already entered isn't uploaded again.
"""
import mimetypes
import os
import re
import threading
import traceback

import google_services
import invoice_entries
import invoice_pdf

# The warehouses an invoice can be for, in dropdown order: {ERP warehouse code: what the dropdown shows}.
# Each is kept under its ERP code (the Drive folder, Invoice Entry, cp_database_N's from_warehouse);
# WH_Delhi_002, the name the team uses for Gurgaon, is MWH_Ggn_bhajgera_001 in the ERP.
WAREHOUSES = {
    "MWH_Blr_Neelmangla_002": "MWH_Blr_Neelmangla_002",
    "MWH_Chn_Kovur_001": "MWH_Chn_Kovur_001",
    "MWH_Hyd_Manikonda_001": "MWH_Hyd_Manikonda_001",
    "MWH_Kol_Dankuni_001": "MWH_Kol_Dankuni_001",
    "MWH_Mum_Bhiwandi_002": "MWH_Mum_Bhiwandi_002",
    "MWH_Pun_Khed_001": "MWH_Pun_Khed_001",
    "MWH_Ggn_bhajgera_001": "WH_Delhi_002",
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
    """Drive took some of the files, or none; the message says which to send again. `files` is what happened to
    the others, as upload() returns it."""

    def __init__(self, files, failed):
        if not files:
            message = "Upload to Google Drive failed, so none of the files were saved. Please try again."
        else:
            message = (f"{len(files)} of {len(files) + len(failed)} files went through, but these didn't: "
                       f"{', '.join(failed)}. Please upload those again.")
        super().__init__(message)
        self.files = files
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


def _read(f):
    """invoice_pdf.read() of an uploaded file, or (None, why it wasn't read)."""
    if _extension(f.filename) != ".pdf":
        return None, "only PDF invoices are read into Invoice Entry"
    data = f.file.read()
    f.file.seek(0)  # for the Drive upload
    try:
        return invoice_pdf.read(data), None
    except invoice_pdf.NotReadable as e:
        return None, f"couldn't read it as an invoice: {e}"


def upload(warehouse, files, by):
    """Puts `files` (FastAPI UploadFiles) in `warehouse`'s invoice folder, uploaded by `by`, and each readable PDF
    invoice's lines in Invoice Entry.

    Returns, in the order given, one {"file", "name", "link", "entry"} per file, where entry is one of
    {"status": "added", "invoice", "lines"}, {"status": "already", "invoice"} (already entered: not uploaded
    again, link None), {"status": "not_read", "reason"} or {"status": "not_saved", "invoice", "reason"} (in
    Drive, but Invoice Entry couldn't be written). Checks everything before sending anything, so a wrong file
    type stops the whole upload; if Drive then fails on some files, the rest are still sent and PartlyFailed
    names the ones that weren't.
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
    done, failed = [], []
    for f in files:
        name = drive_name(f.filename)
        invoice, reason = _read(f)
        if invoice:
            key = (invoice["supplier_gstin"], invoice["invoice_number"])
            try:
                already = invoice_entries.entered(*key)
            except Exception:
                traceback.print_exc()
                already = False  # uploaded anyway; saving its lines says if the database is the problem
            if already:
                done.append({"file": f.filename, "name": name, "link": None,
                             "entry": {"status": "already", "invoice": invoice["invoice_number"]}})
                continue
        try:
            # The type Drive stores comes from the checked extension, not the browser's claim.
            link = google_services.upload_file(f.file, name, mimetypes.guess_type(name)[0], _folder(parent, warehouse),
                                               description=description)
        except google_services.GoogleNotConnected:
            raise  # nothing will get through; main.py says why
        except Exception as e:
            if google_services.is_no_storage(e) and not any(d["link"] for d in done):  # all would fail the same way
                raise NotSetUp("Google Drive won't store the invoices: their folder is in a My Drive, where the "
                               "portal's service account has no storage. It needs to be in a shared drive.") from e
            traceback.print_exc()
            with _folders_lock:
                _folders.pop(warehouse, None)  # in case the folder was moved or deleted: look it up again next time
            failed.append(f.filename)
            continue
        done.append({"file": f.filename, "name": name, "link": link, "entry": _enter(invoice, reason, warehouse,
                                                                                         name, link, by)})
    if failed:
        raise PartlyFailed(done, failed)
    return done


def _enter(invoice, reason, warehouse, name, link, by):
    """Saves an uploaded invoice's lines; what happened, as upload() describes it."""
    if not invoice:
        return {"status": "not_read", "reason": reason}
    number = invoice["invoice_number"]
    try:
        lines = invoice_entries.add(invoice, warehouse, name, link, by)
    except invoice_entries.AlreadyEntered:  # entered by someone else since it was checked
        return {"status": "already", "invoice": number}
    except Exception:
        traceback.print_exc()
        return {"status": "not_saved", "invoice": number,
                "reason": "it's in Drive, but its lines couldn't be saved to Invoice Entry"}
    return {"status": "added", "invoice": number, "lines": lines}


def entries():
    """Invoice Entry for the page: every line, with the name the dropdown gives its warehouse."""
    rows = invoice_entries.rows()
    for row in rows:
        row["warehouse_name"] = WAREHOUSES.get(row["warehouse"], row["warehouse"])
    return rows
