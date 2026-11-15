"""BoxVault (boxvault.py, /api/boxvault*): Upload invoices (what's checked, where files go in Drive, partial
failures, who may upload) and Invoice Entry (PDF invoices' lines saved, listed and taken out). Google Drive and
scm.boxvault_invoice_lines are stand-ins that record what they were sent."""
import pytest
from fastapi.testclient import TestClient

import boxvault
import google_services
import invoice_entries
from pdf_fixture import make_pdf

LOCAL = ("127.0.0.1", 50000)
FOLDER = "invoices-folder-id"
NOT_PDF = "only PDF invoices are read into Invoice Entry"


def invoice_pdf(number="INV-20262642", sku="DBSUPTDEL006", qty="916.00"):
    """A PDF with the supplier's invoice text (see test_invoice_pdf.py): one 15x15x15 box line, 11.00 each, IGST 5%."""
    amount = float(qty.replace(",", "")) * 11
    tax = round(amount * 0.05, 2)
    return make_pdf([
        "DEALBERG TECHNOLOGIES PVT LTD", "GSTIN 29AAFCD7015E1ZT", "TAX INVOICE", f" Invoice# {number}",
        "  Invoice Date : 01/10/2026", "# Item & Description HSN/SAC Qty Rate", "IGST", "Amount% Amt",
        "1 DEL Supertails 15*15*15 Printed 5 Ply ", " carton box", f" {sku}",
        f"481910 {qty} 11.00 5% {tax:,.2f} {amount:,.2f}", "Total In Words",
        f"Sub Total {amount:,.2f}", f"IGST5 (5%) {tax:,.2f}", f"Total {amount + tax:,.2f}"])


class FakeEntries:
    """scm.boxvault_invoice_lines as a list of the rows invoice_entries.add() would write."""

    def __init__(self):
        self.lines = []
        self.fail = False  # add() raises, as if the database were down

    def entered(self, gstin, number):
        return any(r["supplier_gstin"] == gstin and r["invoice_number"] == number for r in self.lines)

    def add(self, invoice, warehouse, file_name, drive_link, by):
        if self.fail:
            raise RuntimeError("MySQL went away")
        if self.entered(invoice["supplier_gstin"], invoice["invoice_number"]):
            raise invoice_entries.AlreadyEntered
        for ln in invoice["lines"]:
            self.lines.append({
                "warehouse": warehouse, "invoice_number": invoice["invoice_number"],
                "invoice_date": invoice["invoice_date"].isoformat(), "supplier": invoice["supplier"],
                "supplier_gstin": invoice["supplier_gstin"], "line_no": ln["line"], "description": ln["description"],
                "sku": ln["sku"], "hsn": ln["hsn"], **{k: float(ln[k]) for k in ("qty", "rate", "tax_pct", "tax_amount",
                                                                               "amount", "total")},
                "file_name": file_name, "drive_link": drive_link, "uploaded_by": by, "uploaded_at": "2026-11-15 16:00:00"})
        return len(invoice["lines"])

    def rows(self):
        return [dict(r) for r in self.lines]

    def remove(self, gstin, number):
        before = len(self.lines)
        self.lines = [r for r in self.lines if not (r["supplier_gstin"] == gstin and r["invoice_number"] == number)]
        return before - len(self.lines)


@pytest.fixture(autouse=True)
def entries(monkeypatch):
    fake = FakeEntries()
    for name in ("entered", "add", "rows", "remove"):
        monkeypatch.setattr(invoice_entries, name, getattr(fake, name))
    return fake


class FakeDrive:
    def __init__(self):
        self.folders = []  # (parent, name) asked of subfolder()
        self.files = []  # (name, mimetype, folder, description, content)
        self.fail = set()  # file names whose upload raises

    def subfolder(self, parent, name):
        self.folders.append((parent, name))
        return f"folder:{name}"

    def upload_file(self, stream, filename, mimetype, folder_id, description=None):
        if filename in self.fail:
            raise RuntimeError("Drive said no")
        self.files.append((filename, mimetype, folder_id, description, stream.read()))
        return f"https://drive.example/{filename}"


@pytest.fixture()
def drive(monkeypatch):
    fake = FakeDrive()
    monkeypatch.setattr(google_services, "subfolder", fake.subfolder)
    monkeypatch.setattr(google_services, "upload_file", fake.upload_file)
    monkeypatch.setattr(boxvault, "_folders", {})
    monkeypatch.setenv("DRIVE_INVOICE_FOLDER_ID", FOLDER)
    return fake


@pytest.fixture()
def main(tmp_path, monkeypatch):
    import entries_store

    monkeypatch.setattr(entries_store, "DB_PATH", str(tmp_path / "t.db"))
    import main

    entries_store.init_db()
    return main


def client(main, accounts, username):
    return TestClient(main.app, client=LOCAL, cookies=accounts.cookies(username))


def upload(c, warehouse, *files):
    """POSTs `files` as (name, content) pairs, the way the page's FormData does."""
    return c.post("/api/boxvault/invoices", data={"warehouse": warehouse},
                  files=[("files", (name, content, "application/octet-stream")) for name, content in files])


def test_the_dropdown_lists_the_seven_warehouses_as_the_team_names_them(main, accounts, drive, monkeypatch):
    data = client(main, accounts, "tester").get("/api/boxvault").json()
    assert [w["name"] for w in data["warehouses"]] == [
        "WH_Delhi_002 · ERP: MWH_Ggn_bhajgera_001", "MWH_Blr_Neelmangla_002", "New Bhiwandi warehouse 002",
        "MWH_Pun_Khed_001", "MWH_Hyd_Manikonda_001", "MWH_Chn_Kovur_001", "MWH_Kol_Dankuni_001"]
    assert data["warehouses"][2]["id"] == "MWH_Mum_Bhiwandi_002"
    assert data["ready"] is True and data["max_files"] == 20 and ".pdf" in data["extensions"]
    assert data["max_total_bytes"] == main.MAX_CONTENT_LENGTH

    monkeypatch.delenv("DRIVE_INVOICE_FOLDER_ID")
    assert client(main, accounts, "tester").get("/api/boxvault").json()["ready"] is False


def test_files_go_into_the_warehouses_own_folder_under_their_own_names(main, accounts, drive):
    c = client(main, accounts, "tester")
    r = upload(c, "MWH_Pun_Khed_001", ("INV-1042.PDF", b"%PDF one"), ("scan 7.jpg", b"jpeg"))
    assert r.status_code == 201
    assert r.json() == {"warehouse": "MWH_Pun_Khed_001", "files": [
        {"file": "INV-1042.PDF", "name": "INV-1042.pdf", "link": "https://drive.example/INV-1042.pdf",
         "entry": {"status": "not_read", "reason": "couldn't read it as an invoice: not a PDF that can be read"}},
        {"file": "scan 7.jpg", "name": "scan 7.jpg", "link": "https://drive.example/scan 7.jpg",
         "entry": {"status": "not_read", "reason": NOT_PDF}}]}
    assert drive.folders == [(FOLDER, "MWH_Pun_Khed_001")]
    described = "Invoice for MWH_Pun_Khed_001, uploaded by tester on BoxVault (Warehouse Power Portal)."
    assert drive.files == [
        ("INV-1042.pdf", "application/pdf", "folder:MWH_Pun_Khed_001", described, b"%PDF one"),
        ("scan 7.jpg", "image/jpeg", "folder:MWH_Pun_Khed_001", described, b"jpeg")]

    # the folder is looked up once, then remembered; another warehouse gets its own
    upload(c, "MWH_Pun_Khed_001", ("INV-1043.pdf", b"x"))
    upload(c, "MWH_Mum_Bhiwandi_002", ("INV-1044.pdf", b"x"))
    assert drive.folders == [(FOLDER, "MWH_Pun_Khed_001"), (FOLDER, "MWH_Mum_Bhiwandi_002")]


@pytest.mark.parametrize("sent, kept", [
    ("Invoice Oct.PDF", "Invoice Oct.pdf"),
    ("Inv #42 (copy).pdf", "Inv _42 (copy).pdf"),
    ("C:\\Users\\wh\\bill.xlsx", "bill.xlsx"),
    ("../../etc/passwd.csv", "passwd.csv"),
    ("बिल.pdf", "invoice.pdf"),
    ("a" * 300 + ".pdf", "a" * 150 + ".pdf"),
])
def test_drive_names_keep_the_uploaded_name_readable_and_safe(sent, kept):
    assert boxvault.drive_name(sent) == kept


@pytest.mark.parametrize("warehouse, files, says", [
    ("", [("a.pdf", b"x")], "Choose a warehouse."),
    ("MWH_Nowhere_001", [("a.pdf", b"x")], "Choose a warehouse."),
    ("MWH_Pun_Khed_001", [], "Add at least one invoice file."),
    ("MWH_Pun_Khed_001", [("a.pdf", b"x"), ("page.html", b"<script>")], "not page.html."),
    ("MWH_Pun_Khed_001", [("a.pdf", b"")], "a.pdf is empty."),
    ("MWH_Pun_Khed_001", [(f"{i}.pdf", b"x") for i in range(21)], "Upload at most 20 files at a time."),
])
def test_an_upload_that_cant_be_taken_sends_nothing_and_says_why(main, accounts, drive, warehouse, files, says):
    r = upload(client(main, accounts, "tester"), warehouse, *files)
    assert r.status_code == 400 and says in r.json()["error"]
    assert drive.files == [] and drive.folders == []


def test_without_a_drive_folder_uploads_say_they_arent_set_up(main, accounts, drive, monkeypatch):
    monkeypatch.delenv("DRIVE_INVOICE_FOLDER_ID")
    r = upload(client(main, accounts, "tester"), "MWH_Pun_Khed_001", ("a.pdf", b"x"))
    assert r.status_code == 503 and "DRIVE_INVOICE_FOLDER_ID" in r.json()["error"]
    assert drive.files == []


def test_when_drive_fails_on_some_files_the_rest_still_go_and_the_page_hears_which(main, accounts, drive):
    drive.fail = {"b.pdf"}
    r = upload(client(main, accounts, "tester"), "MWH_Chn_Kovur_001", ("a.pdf", b"1"), ("b.pdf", b"2"), ("c.pdf", b"3"))
    assert r.status_code == 502
    data = r.json()
    assert (data["error"], data["failed"]) == (
        "2 of 3 files went through, but these didn't: b.pdf. Please upload those again.", ["b.pdf"])
    assert [(f["file"], f["link"]) for f in data["files"]] == [
        ("a.pdf", "https://drive.example/a.pdf"), ("c.pdf", "https://drive.example/c.pdf")]
    # after a failure the folder is looked up again (in case it was moved or deleted): here, for c.pdf
    assert drive.folders == [(FOLDER, "MWH_Chn_Kovur_001")] * 2

    drive.fail = {"a.pdf"}
    r = upload(client(main, accounts, "tester"), "MWH_Chn_Kovur_001", ("a.pdf", b"1"))
    assert r.status_code == 502 and r.json()["error"].startswith("Upload to Google Drive failed, so none")


def test_a_folder_in_a_my_drive_is_said_to_need_a_shared_drive(main, accounts, drive, monkeypatch):
    import httplib2
    from googleapiclient.errors import HttpError

    def no_storage(*args, **kwargs):  # what Drive answers a service account's upload into a My Drive folder
        raise HttpError(httplib2.Response({"status": 403}), b'{"error": {"code": 403, "message": "Service Accounts do '
                        b'not have storage quota.", "errors": [{"reason": "storageQuotaExceeded"}]}}')

    monkeypatch.setattr(google_services, "upload_file", no_storage)
    r = upload(client(main, accounts, "tester"), "MWH_Pun_Khed_001", ("a.pdf", b"x"), ("b.pdf", b"y"))
    assert r.status_code == 503 and r.json()["error"].endswith("It needs to be in a shared drive.")


def test_google_not_connected_is_said_plainly(main, accounts, drive, monkeypatch):
    def not_connected(*args, **kwargs):
        raise google_services.GoogleNotConnected("Google account is not connected.")

    monkeypatch.setattr(google_services, "upload_file", not_connected)
    r = upload(client(main, accounts, "tester"), "MWH_Pun_Khed_001", ("a.pdf", b"x"))
    assert (r.status_code, r.json()["error"]) == (503, "Google account is not connected.")


def test_only_editors_with_the_boxvault_tab_can_upload(main, accounts, drive):
    accounts.add("vee", "viewer-pass", "viewer", ["boxvault"])
    accounts.add("eddie", "editor-pass", "editor", ["boxvault"])
    accounts.add("other", "other-pass", "editor", ["control-tower"])

    assert client(main, accounts, "vee").get("/api/boxvault").status_code == 200
    r = upload(client(main, accounts, "vee"), "MWH_Pun_Khed_001", ("a.pdf", b"x"))
    assert (r.status_code, r.json()["error"]) == (403, "Your access to the BoxVault tab is view-only.")

    r = client(main, accounts, "other").get("/api/boxvault")
    assert (r.status_code, r.json()["error"]) == (403, "You don't have access to the BoxVault tab.")
    assert upload(client(main, accounts, "other"), "MWH_Pun_Khed_001", ("a.pdf", b"x")).status_code == 403

    assert drive.files == []
    assert upload(client(main, accounts, "eddie"), "MWH_Pun_Khed_001", ("a.pdf", b"x")).status_code == 201
    assert drive.files[0][3].endswith("uploaded by eddie on BoxVault (Warehouse Power Portal).")


# ------------------------------------------------------------------ Invoice Entry

def test_a_pdf_invoice_is_uploaded_and_its_lines_listed_for_the_warehouse(main, accounts, drive, entries):
    c = client(main, accounts, "tester")
    r = upload(c, "MWH_Ggn_bhajgera_001", ("INV-20262642.pdf", invoice_pdf()))
    assert r.status_code == 201
    assert r.json()["files"] == [{"file": "INV-20262642.pdf", "name": "INV-20262642.pdf",
                                  "link": "https://drive.example/INV-20262642.pdf",
                                  "entry": {"status": "added", "invoice": "INV-20262642", "lines": 1}}]
    assert [name for name, *_ in drive.files] == ["INV-20262642.pdf"]  # the PDF itself is in Drive too

    rows = c.get("/api/boxvault/entries").json()["rows"]
    assert [{k: row[k] for k in ("warehouse", "warehouse_name", "description", "sku", "qty", "tax_pct", "tax_amount",
                                 "total", "invoice_date", "invoice_number", "drive_link", "uploaded_by", "used", "left")}
            for row in rows] == [{
        "warehouse": "MWH_Ggn_bhajgera_001", "warehouse_name": "WH_Delhi_002 · ERP: MWH_Ggn_bhajgera_001",
        "description": "DEL Supertails 15*15*15 Printed 5 Ply carton box", "sku": "DBSUPTDEL006", "qty": 916.0,
        "tax_pct": 5.0, "tax_amount": 503.8, "total": 10579.8, "invoice_date": "2026-10-01",
        "invoice_number": "INV-20262642", "drive_link": "https://drive.example/INV-20262642.pdf", "uploaded_by": "tester",
        "used": None, "left": None}]  # nothing records which box an order shipped in (boxvault.entries)


def test_an_invoice_already_entered_is_neither_entered_nor_uploaded_again(main, accounts, drive, entries):
    c = client(main, accounts, "tester")
    upload(c, "MWH_Ggn_bhajgera_001", ("INV-20262642.pdf", invoice_pdf()))
    r = upload(c, "MWH_Ggn_bhajgera_001", ("INV-20262642 (1).pdf", invoice_pdf()), ("INV-20262700.pdf", invoice_pdf("INV-20262700")))
    assert r.status_code == 201
    assert [(f["file"], f["link"], f["entry"]) for f in r.json()["files"]] == [
        ("INV-20262642 (1).pdf", None, {"status": "already", "invoice": "INV-20262642"}),
        ("INV-20262700.pdf", "https://drive.example/INV-20262700.pdf", {"status": "added", "invoice": "INV-20262700", "lines": 1})]
    assert [name for name, *_ in drive.files] == ["INV-20262642.pdf", "INV-20262700.pdf"]
    assert len(c.get("/api/boxvault/entries").json()["rows"]) == 2


def test_files_that_cant_be_read_are_kept_in_drive_and_the_page_hears_why(main, accounts, drive, entries):
    unreadable = make_pdf(["TAX INVOICE", " Invoice# INV-1", "  Invoice Date : 01/10/2026", "GSTIN 29AAFCD7015E1ZT",
                           "Sub Total 1.00", "Total 1.05"])
    r = upload(client(main, accounts, "tester"), "MWH_Pun_Khed_001", ("photo.jpg", b"jpeg"), ("odd.pdf", unreadable))
    assert [(f["file"], f["entry"]) for f in r.json()["files"]] == [
        ("photo.jpg", {"status": "not_read", "reason": NOT_PDF}),
        ("odd.pdf", {"status": "not_read", "reason": "couldn't read it as an invoice: no line items found"})]
    assert [name for name, *_ in drive.files] == ["photo.jpg", "odd.pdf"] and entries.lines == []


def test_when_invoice_entry_cant_be_written_the_pdf_is_still_in_drive_and_the_page_hears(main, accounts, drive, entries):
    entries.fail = True
    r = upload(client(main, accounts, "tester"), "MWH_Pun_Khed_001", ("INV-20262642.pdf", invoice_pdf()))
    assert r.status_code == 201
    assert r.json()["files"][0]["entry"] == {"status": "not_saved", "invoice": "INV-20262642",
                                             "reason": "it's in Drive, but its lines couldn't be saved to Invoice Entry"}
    assert [name for name, *_ in drive.files] == ["INV-20262642.pdf"]


def test_an_invoice_can_be_taken_out_by_an_editor_but_not_a_viewer(main, accounts, drive, entries):
    accounts.add("vee", "viewer-pass", "viewer", ["boxvault"])
    accounts.add("eddie", "editor-pass", "editor", ["boxvault"])
    upload(client(main, accounts, "eddie"), "MWH_Ggn_bhajgera_001", ("INV-20262642.pdf", invoice_pdf()))
    which = {"supplier_gstin": "29AAFCD7015E1ZT", "invoice_number": "INV-20262642"}

    assert len(client(main, accounts, "vee").get("/api/boxvault/entries").json()["rows"]) == 1  # viewers see it
    r = client(main, accounts, "vee").delete("/api/boxvault/entries", params=which)
    assert (r.status_code, r.json()["error"]) == (403, "Your access to the BoxVault tab is view-only.")

    r = client(main, accounts, "eddie").delete("/api/boxvault/entries", params=which)
    assert (r.status_code, r.json()) == (200, {"removed": 1})
    assert entries.lines == [] and drive.files  # the PDF stays in Drive
    r = client(main, accounts, "eddie").delete("/api/boxvault/entries", params=which)
    assert (r.status_code, r.json()["error"]) == (404, "Invoice INV-20262642 isn't in Invoice Entry.")

    # taken out, it can be uploaded again (for the right warehouse, say)
    r = upload(client(main, accounts, "eddie"), "MWH_Kol_Dankuni_001", ("INV-20262642.pdf", invoice_pdf()))
    assert r.json()["files"][0]["entry"]["status"] == "added" and entries.lines[0]["warehouse"] == "MWH_Kol_Dankuni_001"


def test_invoice_entry_failing_to_load_says_so(main, accounts, monkeypatch):
    def down():
        raise RuntimeError("MySQL went away")

    monkeypatch.setattr(invoice_entries, "rows", down)
    r = client(main, accounts, "tester").get("/api/boxvault/entries")
    assert (r.status_code, r.json()["error"]) == (502, "Could not read Invoice Entry from the database. Please try again.")
