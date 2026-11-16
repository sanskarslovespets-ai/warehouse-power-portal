"""BoxVault (boxvault.py, /api/boxvault*): Upload invoices (what's checked, where files go in Drive, partial
failures, who may upload), Invoice Entry (PDF invoices' lines saved, listed and taken out) and the boxes used
entered, which SKU used comes from (box_usage.py). Google Drive, scm.boxvault_invoice_lines and
scm.boxvault_sku_usage are stand-ins that record what they were sent."""
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

import box_usage
import boxvault
import google_services
import invoice_entries
from pdf_fixture import make_pdf

LOCAL = ("127.0.0.1", 50000)
FOLDER = "invoices-folder-id"
NOT_PDF = "only PDF invoices are read into Invoice Entry"
REAL_RECORDS, REAL_SAVE = box_usage.records, box_usage.save  # the usage fixture stands in for them elsewhere

# Ship To addresses as the supplier's invoices print them
GURUGRAM = ["Supertails-Pets Centric Private Limited, PLOT NO 186, VPO BAJGHERA", "BAJGHERA,", "Gurugram",
            "122017 Haryana", "India"]
BHIWANDI = ["Ground floor K D Logistics Park 3 Building no. N2 Bhawale Bhiwandi", "Kalyan", "Mumbai",
            "421302 Maharashtra", "India"]
PUNE = ["Gate No.302, Moi Road,", "Village-Kuruli,Taluka-Khed,District", "Pune", "410501 Maharashtra", "India"]
JAIPUR = ["Plot 4, Sitapura Industrial Area", "Jaipur", "302022 Rajasthan", "India"]
KARNATAKA_OFFICE = ["Supertails, Koramangala,", "Bangalore", "560034 Karnataka", "India"]


def invoice_pdf(number="INV-20262642", sku="DBSUPTDEL006", qty="916.00", dated="01/10/2026", ship_to=GURUGRAM,
                bill_to=GURUGRAM):
    """A PDF with the supplier's invoice text (see test_invoice_pdf.py): one 15x15x15 box line, 11.00 each, IGST 5%."""
    amount = float(qty.replace(",", "")) * 11
    tax = round(amount * 0.05, 2)
    return make_pdf([
        "DEALBERG TECHNOLOGIES PVT LTD", "GSTIN 29AAFCD7015E1ZT", "TAX INVOICE", f" Invoice# {number}",
        f"  Invoice Date : {dated}", "Bill To Ship To", "Supertails- Pets Centric Private Limited", *bill_to,
        "GSTIN 06AALCP9335K2ZU", *ship_to, "# Item & Description HSN/SAC Qty Rate", "IGST", "Amount% Amt",
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


class FakeUsage:
    """scm.boxvault_sku_usage: {(warehouse, sku, "YYYY-MM"): (used, by)}, as box_usage.records() / save() keep it."""

    def __init__(self):
        self.numbers = {}
        self.missing = False  # the table hasn't been made
        self.fail = False  # reading raises, as if the database were down
        self.denied = False  # saving is refused, as if the account had no INSERT/UPDATE

    def set(self, warehouse, sku, month, used, by="tester"):
        self.numbers[(warehouse, sku, month)] = (used, by)

    def records(self):
        if self.missing:
            raise box_usage.NotSetUp
        if self.fail:
            raise RuntimeError("MySQL went away")
        return [{"warehouse": w, "sku": sku, "month": m, "used": used, "entered_by": by,
                 "entered_at": "2026-11-16 12:00:00"} for (w, sku, m), (used, by) in sorted(self.numbers.items(), key=lambda kv: kv[0][2])]

    def save(self, month, numbers, by):
        if self.missing:
            raise box_usage.NotSetUp
        if self.denied:
            raise box_usage.NotAllowed
        for warehouse, sku, used in numbers:
            self.set(warehouse, sku, month.strftime("%Y-%m"), used, by)
        return len(numbers)


@pytest.fixture(autouse=True)
def usage(monkeypatch):
    fake = FakeUsage()
    monkeypatch.setattr(box_usage, "records", fake.records)
    monkeypatch.setattr(box_usage, "save", fake.save)
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


def upload(c, *files):
    """POSTs `files` as (name, content) pairs, the way the page's FormData does."""
    return c.post("/api/boxvault/invoices",
                  files=[("files", (name, content, "application/octet-stream")) for name, content in files])


def test_the_filter_lists_the_seven_warehouses_as_the_team_names_them(main, accounts, drive, monkeypatch):
    data = client(main, accounts, "tester").get("/api/boxvault").json()
    assert [w["name"] for w in data["warehouses"]] == [
        "MWH_Blr_Neelmangla_002", "MWH_Chn_Kovur_001", "MWH_Hyd_Manikonda_001", "MWH_Kol_Dankuni_001",
        "MWH_Mum_Bhiwandi_002", "MWH_Pun_Khed_001", "WH_Delhi_002"]
    assert data["warehouses"][-1]["id"] == "MWH_Ggn_bhajgera_001"  # kept under its ERP code
    assert data["ready"] is True and data["max_files"] == 20 and ".pdf" in data["extensions"]
    assert data["max_total_bytes"] == main.MAX_CONTENT_LENGTH

    monkeypatch.delenv("DRIVE_INVOICE_FOLDER_ID")
    assert client(main, accounts, "tester").get("/api/boxvault").json()["ready"] is False


def test_each_invoice_goes_to_the_warehouse_in_its_ship_to_address_and_the_rest_to_unsorted(main, accounts, drive,
                                                                                            entries):
    c = client(main, accounts, "tester")
    r = upload(c, ("INV-1.pdf", invoice_pdf("INV-1")), ("pune.PDF", invoice_pdf("INV-2", ship_to=PUNE)),
               ("scan 7.jpg", b"jpeg"), ("INV-1042.pdf", b"%PDF one"))
    assert r.status_code == 201
    assert r.json()["files"] == [
        {"file": "INV-1.pdf", "name": "INV-1.pdf", "link": "https://drive.example/INV-1.pdf",
         "warehouse": "MWH_Ggn_bhajgera_001", "entry": {"status": "added", "invoice": "INV-1", "lines": 1}},
        {"file": "pune.PDF", "name": "pune.pdf", "link": "https://drive.example/pune.pdf",
         "warehouse": "MWH_Pun_Khed_001", "entry": {"status": "added", "invoice": "INV-2", "lines": 1}},
        {"file": "scan 7.jpg", "name": "scan 7.jpg", "link": "https://drive.example/scan 7.jpg", "warehouse": None,
         "entry": {"status": "not_read", "reason": NOT_PDF}},
        {"file": "INV-1042.pdf", "name": "INV-1042.pdf", "link": "https://drive.example/INV-1042.pdf",
         "warehouse": None,
         "entry": {"status": "not_read", "reason": "couldn't read it as an invoice: not a PDF that can be read"}}]
    assert drive.folders == [(FOLDER, "MWH_Ggn_bhajgera_001"), (FOLDER, "MWH_Pun_Khed_001"), (FOLDER, "Unsorted")]
    by = ", uploaded by tester on BoxVault (Warehouse Power Portal)."
    assert [(name, mimetype, folder, described) for name, mimetype, folder, described, _ in drive.files] == [
        ("INV-1.pdf", "application/pdf", "folder:MWH_Ggn_bhajgera_001", "Invoice for MWH_Ggn_bhajgera_001" + by),
        ("pune.pdf", "application/pdf", "folder:MWH_Pun_Khed_001", "Invoice for MWH_Pun_Khed_001" + by),
        ("scan 7.jpg", "image/jpeg", "folder:Unsorted", "Invoice, warehouse not read from it" + by),
        ("INV-1042.pdf", "application/pdf", "folder:Unsorted", "Invoice, warehouse not read from it" + by)]
    assert [(ln["invoice_number"], ln["warehouse"]) for ln in entries.lines] == [
        ("INV-1", "MWH_Ggn_bhajgera_001"), ("INV-2", "MWH_Pun_Khed_001")]

    # a folder is looked up once, then remembered
    upload(c, ("INV-3.pdf", invoice_pdf("INV-3")), ("x.csv", b"a,b"))
    assert len(drive.folders) == 3


def test_an_invoice_whose_address_matches_no_warehouse_is_kept_in_unsorted_and_not_entered(main, accounts, drive,
                                                                                            entries):
    r = upload(client(main, accounts, "tester"), ("jaipur.pdf", invoice_pdf("INV-9", ship_to=JAIPUR)))
    assert r.status_code == 201
    assert r.json()["files"][0]["warehouse"] is None
    assert r.json()["files"][0]["entry"] == {"status": "not_read", "reason": (
        "its Ship To address (Plot 4, Sitapura Industrial Area, Jaipur, 302022 Rajasthan, India) doesn't match any "
        "of the seven warehouses")}
    assert drive.files[0][2] == "folder:Unsorted" and entries.lines == []


def test_the_bill_to_address_doesnt_decide_the_warehouse(main, accounts, drive, entries):
    upload(client(main, accounts, "tester"),
           ("b.pdf", invoice_pdf("INV-5", bill_to=KARNATAKA_OFFICE, ship_to=BHIWANDI)))
    assert entries.lines[0]["warehouse"] == "MWH_Mum_Bhiwandi_002"


@pytest.mark.parametrize("ship_to, warehouse", [
    # the supplier's real Ship To addresses (October 2026)
    ("Sy 7, 109, 14, Makali Village, Makali, Dasanapura, Hobli, Nelamangala Taluk, Bangalore -, Nelamangala, "
     "562123 Karnataka, India", "MWH_Blr_Neelmangla_002"),
    ("Supertails Bangalore warehouse - Makali, Bangalore, 562123 Karnataka, India", "MWH_Blr_Neelmangla_002"),
    ("Supertails-Pets Centric Private Limited, PLOT NO 186, VPO BAJGHERA, BAJGHERA, Gurugram, 122017 Haryana, India",
     "MWH_Ggn_bhajgera_001"),
    ("Ground floor K D Logistics Park 3 Building no. N2 Bhawale Bhiwandi, Kalyan, Mumbai, 421302 Maharashtra, India",
     "MWH_Mum_Bhiwandi_002"),
    # the old Bhiwandi_001 premises: same pincode, and only _002 ships now
    ("Supertails- Pets Centric Private Limited, building/premises at Gala, Nos. 5 & 6, Ground Floor, Kalyan Bypass "
     "Road, Saravali Village, Bhiwandi Taluka, Thane 421302, Bhiwandi, 421302 Maharashtra, India",
     "MWH_Mum_Bhiwandi_002"),
    ("Gate No.302, Moi Road, Village-Kuruli,Taluka-Khed,District, Pune, 410501 Maharashtra, India", "MWH_Pun_Khed_001"),
    # not the Dankuni warehouse's pincode (712250), but Kolkata
    ("571, Mahendra Banerjee Road, Ground Floor, ward no.129, PamasreePally, Pamasree, Kolkata, 700060 West Bengal, "
     "India", "MWH_Kol_Dankuni_001"),
    # the ERP's addresses of the two with no invoices yet
    ("Survey No: 415/2A1, Kovur Village, Kundrathur Taluk, Chennai, 600128", "MWH_Chn_Kovur_001"),
    ("Survey no 76-AA Neknampur, Gandhipet Manikonda, Hyderabad, 500089", "MWH_Hyd_Manikonda_001"),
    # a warehouse's pincode decides before the city names: this road names Mumbai, but it's Khed's pincode
    ("Mumbai-Pune Highway, Khed, 410501 Maharashtra", "MWH_Pun_Khed_001"),
])
def test_the_warehouse_read_from_a_ship_to_address(ship_to, warehouse):
    assert boxvault.warehouse_for(ship_to) == (warehouse, None)


@pytest.mark.parametrize("ship_to, why", [
    ("", "no Ship To address found on it, so its warehouse isn't known"),
    ("Plot 4, Jaipur, 302022 Rajasthan", "its Ship To address (Plot 4, Jaipur, 302022 Rajasthan) doesn't match any of "
                                         "the seven warehouses"),
    ("Pune Road, Mumbai", "its Ship To address (Pune Road, Mumbai) names more than one warehouse's city"),
])
def test_an_address_that_doesnt_name_one_warehouse_says_why(ship_to, why):
    assert boxvault.warehouse_for(ship_to) == (None, why)


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


@pytest.mark.parametrize("files, says", [
    ([], "Add at least one invoice file."),
    ([("a.pdf", b"x"), ("page.html", b"<script>")], "not page.html."),
    ([("a.pdf", b"")], "a.pdf is empty."),
    ([(f"{i}.pdf", b"x") for i in range(21)], "Upload at most 20 files at a time."),
])
def test_an_upload_that_cant_be_taken_sends_nothing_and_says_why(main, accounts, drive, files, says):
    r = upload(client(main, accounts, "tester"), *files)
    assert r.status_code == 400 and says in r.json()["error"]
    assert drive.files == [] and drive.folders == []


def test_without_a_drive_folder_uploads_say_they_arent_set_up(main, accounts, drive, monkeypatch):
    monkeypatch.delenv("DRIVE_INVOICE_FOLDER_ID")
    r = upload(client(main, accounts, "tester"), ("a.pdf", b"x"))
    assert r.status_code == 503 and "DRIVE_INVOICE_FOLDER_ID" in r.json()["error"]
    assert drive.files == []


def test_when_drive_fails_on_some_files_the_rest_still_go_and_the_page_hears_which(main, accounts, drive):
    drive.fail = {"b.pdf"}
    r = upload(client(main, accounts, "tester"), ("a.pdf", b"1"), ("b.pdf", b"2"), ("c.pdf", b"3"))
    assert r.status_code == 502
    data = r.json()
    assert (data["error"], data["failed"]) == (
        "2 of 3 files went through, but these didn't: b.pdf. Please upload those again.", ["b.pdf"])
    assert [(f["file"], f["link"]) for f in data["files"]] == [
        ("a.pdf", "https://drive.example/a.pdf"), ("c.pdf", "https://drive.example/c.pdf")]
    # after a failure the folder is looked up again (in case it was moved or deleted): here, for c.pdf
    assert drive.folders == [(FOLDER, "Unsorted")] * 2

    drive.fail = {"a.pdf"}
    r = upload(client(main, accounts, "tester"), ("a.pdf", b"1"))
    assert r.status_code == 502 and r.json()["error"].startswith("Upload to Google Drive failed, so none")


def test_a_folder_in_a_my_drive_is_said_to_need_a_shared_drive(main, accounts, drive, monkeypatch):
    import httplib2
    from googleapiclient.errors import HttpError

    def no_storage(*args, **kwargs):  # what Drive answers a service account's upload into a My Drive folder
        raise HttpError(httplib2.Response({"status": 403}), b'{"error": {"code": 403, "message": "Service Accounts do '
                        b'not have storage quota.", "errors": [{"reason": "storageQuotaExceeded"}]}}')

    monkeypatch.setattr(google_services, "upload_file", no_storage)
    r = upload(client(main, accounts, "tester"), ("a.pdf", b"x"), ("b.pdf", b"y"))
    assert r.status_code == 503 and r.json()["error"].endswith("It needs to be in a shared drive.")


def test_google_not_connected_is_said_plainly(main, accounts, drive, monkeypatch):
    def not_connected(*args, **kwargs):
        raise google_services.GoogleNotConnected("Google account is not connected.")

    monkeypatch.setattr(google_services, "upload_file", not_connected)
    r = upload(client(main, accounts, "tester"), ("a.pdf", b"x"))
    assert (r.status_code, r.json()["error"]) == (503, "Google account is not connected.")


def test_only_editors_with_the_boxvault_tab_can_upload(main, accounts, drive):
    accounts.add("vee", "viewer-pass", "viewer", ["boxvault"])
    accounts.add("eddie", "editor-pass", "editor", ["boxvault"])
    accounts.add("other", "other-pass", "editor", ["control-tower"])

    assert client(main, accounts, "vee").get("/api/boxvault").status_code == 200
    r = upload(client(main, accounts, "vee"), ("a.pdf", b"x"))
    assert (r.status_code, r.json()["error"]) == (403, "Your access to the BoxVault tab is view-only.")

    r = client(main, accounts, "other").get("/api/boxvault")
    assert (r.status_code, r.json()["error"]) == (403, "You don't have access to the BoxVault tab.")
    assert upload(client(main, accounts, "other"), ("a.pdf", b"x")).status_code == 403

    assert drive.files == []
    assert upload(client(main, accounts, "eddie"), ("a.pdf", b"x")).status_code == 201
    assert drive.files[0][3].endswith("uploaded by eddie on BoxVault (Warehouse Power Portal).")


# ------------------------------------------------------------------ Invoice Entry

def test_a_pdf_invoice_is_uploaded_and_its_lines_listed_under_its_ship_to_warehouse(main, accounts, drive, entries):
    c = client(main, accounts, "tester")
    r = upload(c, ("INV-20262642.pdf", invoice_pdf()))
    assert r.status_code == 201
    assert r.json()["files"] == [{"file": "INV-20262642.pdf", "name": "INV-20262642.pdf",
                                  "link": "https://drive.example/INV-20262642.pdf", "warehouse": "MWH_Ggn_bhajgera_001",
                                  "entry": {"status": "added", "invoice": "INV-20262642", "lines": 1}}]
    assert [name for name, *_ in drive.files] == ["INV-20262642.pdf"]  # the PDF itself is in Drive too

    rows = c.get("/api/boxvault/entries").json()["rows"]
    assert [{k: row[k] for k in ("warehouse", "warehouse_name", "description", "sku", "qty", "tax_pct", "tax_amount",
                                 "total", "invoice_date", "invoice_number", "drive_link", "uploaded_by")}
            for row in rows] == [{
        "warehouse": "MWH_Ggn_bhajgera_001", "warehouse_name": "WH_Delhi_002",
        "description": "DEL Supertails 15*15*15 Printed 5 Ply carton box", "sku": "DBSUPTDEL006", "qty": 916.0,
        "tax_pct": 5.0, "tax_amount": 503.8, "total": 10579.8, "invoice_date": "2026-10-01",
        "invoice_number": "INV-20262642", "drive_link": "https://drive.example/INV-20262642.pdf", "uploaded_by": "tester"}]
    assert rows[0]["used"] == 0  # no delivery note has carried the box yet
    assert "left" not in rows[0] and "used_pct" not in rows[0]


def test_an_invoice_already_entered_is_neither_entered_nor_uploaded_again(main, accounts, drive, entries):
    c = client(main, accounts, "tester")
    upload(c, ("INV-20262642.pdf", invoice_pdf()))
    r = upload(c, ("INV-20262642 (1).pdf", invoice_pdf()), ("INV-20262700.pdf", invoice_pdf("INV-20262700")))
    assert r.status_code == 201
    assert [(f["file"], f["link"], f["entry"]) for f in r.json()["files"]] == [
        ("INV-20262642 (1).pdf", None, {"status": "already", "invoice": "INV-20262642"}),
        ("INV-20262700.pdf", "https://drive.example/INV-20262700.pdf", {"status": "added", "invoice": "INV-20262700", "lines": 1})]
    assert [name for name, *_ in drive.files] == ["INV-20262642.pdf", "INV-20262700.pdf"]
    assert len(c.get("/api/boxvault/entries").json()["rows"]) == 2


def test_files_that_cant_be_read_are_kept_in_drive_and_the_page_hears_why(main, accounts, drive, entries):
    unreadable = make_pdf(["TAX INVOICE", " Invoice# INV-1", "  Invoice Date : 01/10/2026", "GSTIN 29AAFCD7015E1ZT",
                           "Sub Total 1.00", "Total 1.05"])
    r = upload(client(main, accounts, "tester"), ("photo.jpg", b"jpeg"), ("odd.pdf", unreadable))
    assert [(f["file"], f["entry"]) for f in r.json()["files"]] == [
        ("photo.jpg", {"status": "not_read", "reason": NOT_PDF}),
        ("odd.pdf", {"status": "not_read", "reason": "couldn't read it as an invoice: no line items found"})]
    assert [name for name, *_ in drive.files] == ["photo.jpg", "odd.pdf"] and entries.lines == []


def test_when_invoice_entry_cant_be_written_the_pdf_is_still_in_drive_and_the_page_hears(main, accounts, drive, entries):
    entries.fail = True
    r = upload(client(main, accounts, "tester"), ("INV-20262642.pdf", invoice_pdf()))
    assert r.status_code == 201
    assert r.json()["files"][0]["entry"] == {"status": "not_saved", "invoice": "INV-20262642",
                                             "reason": "it's in Drive, but its lines couldn't be saved to Invoice Entry"}
    assert [name for name, *_ in drive.files] == ["INV-20262642.pdf"]


def test_an_invoice_can_be_taken_out_by_an_editor_but_not_a_viewer(main, accounts, drive, entries):
    accounts.add("vee", "viewer-pass", "viewer", ["boxvault"])
    accounts.add("eddie", "editor-pass", "editor", ["boxvault"])
    upload(client(main, accounts, "eddie"), ("INV-20262642.pdf", invoice_pdf()))
    which = {"supplier_gstin": "29AAFCD7015E1ZT", "invoice_number": "INV-20262642"}

    assert len(client(main, accounts, "vee").get("/api/boxvault/entries").json()["rows"]) == 1  # viewers see it
    r = client(main, accounts, "vee").delete("/api/boxvault/entries", params=which)
    assert (r.status_code, r.json()["error"]) == (403, "Your access to the BoxVault tab is view-only.")

    r = client(main, accounts, "eddie").delete("/api/boxvault/entries", params=which)
    assert (r.status_code, r.json()) == (200, {"removed": 1})
    assert entries.lines == [] and drive.files  # the PDF stays in Drive
    r = client(main, accounts, "eddie").delete("/api/boxvault/entries", params=which)
    assert (r.status_code, r.json()["error"]) == (404, "Invoice INV-20262642 isn't in Invoice Entry.")

    # taken out, it can be uploaded again
    r = upload(client(main, accounts, "eddie"), ("INV-20262642.pdf", invoice_pdf()))
    assert r.json()["files"][0]["entry"]["status"] == "added" and len(entries.lines) == 1


def test_invoice_entry_failing_to_load_says_so(main, accounts, monkeypatch):
    def down():
        raise RuntimeError("MySQL went away")

    monkeypatch.setattr(invoice_entries, "rows", down)
    r = client(main, accounts, "tester").get("/api/boxvault/entries")
    assert (r.status_code, r.json()["error"]) == (502, "Could not read Invoice Entry from the database. Please try again.")


# ------------------------------------------------------------------ boxes used and SKU used (box_usage.py)

def test_sku_used_is_the_boxes_used_entered_for_the_warehouse_from_the_invoices_month(main, accounts, drive, entries,
                                                                                       usage):
    c = client(main, accounts, "tester")
    upload(c, ("INV-20262642.pdf", invoice_pdf()))  # 916 boxes at Gurugram, 1 Oct 2026
    usage.set("MWH_Ggn_bhajgera_001", "DBSUPTDEL006", "2026-10", 300)
    usage.set("MWH_Ggn_bhajgera_001", "DBSUPTDEL006", "2026-11", 100)
    usage.set("MWH_Ggn_bhajgera_001", "DBSUPTDEL006", "2026-09", 50)  # before the invoice: not its boxes
    usage.set("MWH_Pun_Khed_001", "DBSUPTDEL006", "2026-10", 999)  # another warehouse's

    data = c.get("/api/boxvault/entries").json()
    assert [r["used"] for r in data["rows"]] == [400]
    assert (data["usage_ready"], data["usage_problem"]) == (True, None)
    assert {(u["warehouse"], u["month"], u["used"]) for u in data["usage"]} == {
        ("MWH_Ggn_bhajgera_001", "2026-09", 50), ("MWH_Ggn_bhajgera_001", "2026-10", 300),
        ("MWH_Ggn_bhajgera_001", "2026-11", 100), ("MWH_Pun_Khed_001", "2026-10", 999)}


def test_boxes_count_against_the_oldest_invoice_first_so_none_is_counted_twice(main, accounts, drive, entries, usage):
    c = client(main, accounts, "tester")
    upload(c, ("a.pdf", invoice_pdf("INV-1", "DBSUPTMUM006", "100.00", "01/10/2026", ship_to=BHIWANDI)),
           ("b.pdf", invoice_pdf("INV-2", "DBSUPTMUM006", "50.00", "15/10/2026", ship_to=BHIWANDI)),
           ("c.pdf", invoice_pdf("INV-3", "DBSUPTMUM006", "40.00", "01/10/2026", ship_to=PUNE)))
    usage.set("MWH_Mum_Bhiwandi_002", "DBSUPTMUM006", "2026-10", 130)
    usage.set("MWH_Pun_Khed_001", "DBSUPTMUM006", "2026-10", 5)

    got = {r["invoice_number"]: r["used"] for r in c.get("/api/boxvault/entries").json()["rows"]}
    # INV-1 takes the first 100 of Bhiwandi's 130, INV-2 (dated in October too) the other 30; Pune counts only its own
    assert got == {"INV-1": 100, "INV-2": 30, "INV-3": 5}


@pytest.mark.parametrize("invoices, months, used", [
    ([(date(2026, 10, 10), 100)], [(date(2026, 9, 30), 8), (date(2026, 10, 31), 2)], [2]),  # before the invoice
    ([(date(2026, 10, 1), 10)], [(date(2026, 10, 31), 15)], [15]),  # more than bought: past the quantity
    # a month's boxes can't be from an invoice dated after it, even when the older invoice is used up
    ([(date(2026, 10, 1), 10), (date(2026, 11, 20), 10)], [(date(2026, 10, 31), 12), (date(2026, 11, 30), 3)], [12, 3]),
    ([(date(2026, 10, 1), 10), (date(2026, 10, 1), 10)], [(date(2026, 10, 31), 25)], [10, 15]),  # beyond both: the newest
    ([(date(2026, 10, 1), 10)], [], [0]),
])
def test_allocating_boxes_to_invoices(invoices, months, used):
    assert box_usage.allocate(invoices, months) == used


@pytest.mark.parametrize("month, end", [("2026-10", date(2026, 10, 31)), ("2026-11", date(2026, 11, 30)),
                                        ("2028-02", date(2028, 2, 29)), ("2026-12", date(2026, 12, 31))])
def test_a_months_boxes_are_dated_its_last_day(month, end):
    assert box_usage.month_end(month) == end


def test_each_line_says_which_months_its_boxes_were_used_in():
    rows = [{"warehouse": "MWH_Pun_Khed_001", "sku": "DBSUPTMUM006", "qty": q, "invoice_date": d,
             "invoice_number": n, "line_no": 1}
            for n, d, q in (("INV-1", "2026-09-05", 100.0), ("INV-2", "2026-10-02", 50.0), ("INV-3", "2026-10-20", 10.0))]
    box_usage.add(rows, [{"warehouse": "MWH_Pun_Khed_001", "sku": "DBSUPTMUM006", "month": m, "used": u}
                         for m, u in (("2026-09", 60), ("2026-10", 70), ("2026-11", 0), ("2026-12", 45))])
    got = {r["invoice_number"]: (r["used"], r["used_in"]) for r in rows}
    assert got == {
        # September's 60 and the first 40 of October's 70 use up INV-1
        "INV-1": (100, [{"month": "2026-09", "used": 60}, {"month": "2026-10", "used": 40}]),
        # the rest of October; December's 45 finish INV-2 (20) and INV-3 (10), the other 15 go past INV-3's quantity
        "INV-2": (50, [{"month": "2026-10", "used": 30}, {"month": "2026-12", "used": 20}]),
        "INV-3": (25, [{"month": "2026-12", "used": 25}]),
    }  # November's 0 shows nowhere


def test_a_line_without_a_sku_has_no_usage():
    rows = [{"warehouse": "MWH_Pun_Khed_001", "sku": "", "qty": 5.0, "invoice_date": "2026-10-01",
             "invoice_number": "INV-9", "line_no": 1}]
    box_usage.add(rows, [{"warehouse": "MWH_Pun_Khed_001", "sku": "", "month": "2026-10", "used": 3}])
    assert rows[0]["used"] is None and rows[0]["used_in"] == []


def test_sku_used_can_go_past_the_quantity_bought():
    rows = [{"warehouse": "MWH_Pun_Khed_001", "sku": "DBSUPTMUM006", "qty": 9.0, "invoice_date": "2026-10-01",
             "invoice_number": "INV-7", "line_no": 1}]
    box_usage.add(rows, [{"warehouse": "MWH_Pun_Khed_001", "sku": "DBSUPTMUM006", "month": m, "used": n}
                         for m, n in (("2026-10", 3), ("2026-11", 9))])
    assert rows[0]["used"] == 12  # more used than bought, which Analytics' data checks list
    assert "left" not in rows[0] and "used_pct" not in rows[0]


def test_before_the_usage_table_is_made_sku_used_is_0_and_the_page_says_why(main, accounts, drive, entries, usage):
    c = client(main, accounts, "tester")
    upload(c, ("INV-20262642.pdf", invoice_pdf()))
    usage.missing = True
    data = c.get("/api/boxvault/entries").json()
    assert [r["used"] for r in data["rows"]] == [0] and data["usage"] == [] and data["usage_ready"] is False
    assert data["usage_problem"] == ("Boxes used can't be entered yet: the scm.boxvault_sku_usage table hasn't been "
                                     "made, so SKU used is 0 for now. Ask an admin.")
    r = c.post("/api/boxvault/usage", json={"month": "2026-10", "numbers": [
        {"warehouse": "MWH_Ggn_bhajgera_001", "sku": "DBSUPTDEL006", "used": 5}]})
    assert (r.status_code, r.json()["error"]) == (
        503, "Boxes used can't be saved yet: the scm.boxvault_sku_usage table hasn't been made. Ask an admin.")


def test_when_the_usage_cant_be_read_the_lines_are_still_listed_and_the_page_hears(main, accounts, drive, entries,
                                                                                     usage):
    c = client(main, accounts, "tester")
    upload(c, ("INV-20262642.pdf", invoice_pdf()))
    usage.fail = True
    r = c.get("/api/boxvault/entries")
    assert r.status_code == 200
    assert [(row["invoice_number"], row["used"]) for row in r.json()["rows"]] == [("INV-20262642", None)]
    assert r.json()["usage_problem"] == (
        "Couldn't read the boxes used that were entered, so SKU used shows \"—\". Reload to try again.")


def test_an_editor_saves_boxes_used_for_a_month_and_sku_used_follows(main, accounts, drive, entries, usage):
    accounts.add("eddie", "editor-pass", "editor", ["boxvault"])
    c = client(main, accounts, "eddie")
    upload(c, ("INV-20262642.pdf", invoice_pdf()))
    r = c.post("/api/boxvault/usage", json={"month": "2026-10", "numbers": [
        {"warehouse": "MWH_Ggn_bhajgera_001", "sku": "DBSUPTDEL006", "used": 120}]})
    assert (r.status_code, r.json()) == (200, {"saved": 1})
    assert usage.numbers == {("MWH_Ggn_bhajgera_001", "DBSUPTDEL006", "2026-10"): (120, "eddie")}
    assert [row["used"] for row in c.get("/api/boxvault/entries").json()["rows"]] == [120]

    # a new number for the same month replaces the old one
    c.post("/api/boxvault/usage", json={"month": "2026-10", "numbers": [
        {"warehouse": "MWH_Ggn_bhajgera_001", "sku": "DBSUPTDEL006", "used": 0}]})
    assert [row["used"] for row in c.get("/api/boxvault/entries").json()["rows"]] == [0]


def test_only_editors_with_the_boxvault_tab_can_enter_boxes_used(main, accounts, usage):
    accounts.add("vee", "viewer-pass", "viewer", ["boxvault"])
    accounts.add("other", "other-pass", "editor", ["rto"])
    sent = {"month": "2026-10", "numbers": [{"warehouse": "MWH_Pun_Khed_001", "sku": "DBSUPTMUM006", "used": 1}]}
    r = client(main, accounts, "vee").post("/api/boxvault/usage", json=sent)
    assert (r.status_code, r.json()["error"]) == (403, "Your access to the BoxVault tab is view-only.")
    assert client(main, accounts, "other").post("/api/boxvault/usage", json=sent).status_code == 403
    assert usage.numbers == {}


@pytest.mark.parametrize("sent, says", [
    ({"month": "", "numbers": [{"warehouse": "MWH_Pun_Khed_001", "sku": "X", "used": 1}]},
     "Choose the month the boxes were used in."),
    ({"month": "2026-13", "numbers": [{"warehouse": "MWH_Pun_Khed_001", "sku": "X", "used": 1}]},
     "Choose the month the boxes were used in."),
    ({"month": "2026-12", "numbers": [{"warehouse": "MWH_Pun_Khed_001", "sku": "X", "used": 1}]},
     "Boxes used can't be entered for a month that hasn't started yet."),
    ({"month": "2026-10", "numbers": []}, "Enter at least one number of boxes used."),
    ({"month": "2026-10", "numbers": [{"warehouse": "Jaipur", "sku": "X", "used": 1}]},
     "Jaipur isn't one of BoxVault's warehouses."),
    ({"month": "2026-10", "numbers": [{"warehouse": "MWH_Pun_Khed_001", "sku": " ", "used": 1}]},
     "Each number needs the SKU it's for."),
    ({"month": "2026-10", "numbers": [{"warehouse": "MWH_Pun_Khed_001", "sku": "X", "used": -1}]},
     "Boxes used for X must be a whole number, 0 or more."),
    ({"month": "2026-10", "numbers": [{"warehouse": "MWH_Pun_Khed_001", "sku": "X", "used": 2.5}]},
     "Boxes used for X must be a whole number, 0 or more."),
    ({"month": "2026-10", "numbers": [{"warehouse": "MWH_Pun_Khed_001", "sku": "X", "used": "7"}]},
     "Boxes used for X must be a whole number, 0 or more."),
    ({"month": "2026-10", "numbers": [{"warehouse": "MWH_Pun_Khed_001", "sku": "X", "used": 1},
                                      {"warehouse": "MWH_Pun_Khed_001", "sku": "X", "used": 2}]},
     "X at MWH_Pun_Khed_001 is in the list twice."),
])
def test_boxes_used_that_cant_be_taken_save_nothing_and_say_why(main, accounts, usage, monkeypatch, sent, says):
    class November(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 16, 12, 0, tzinfo=tz)

    monkeypatch.setattr(boxvault, "datetime", November)
    r = client(main, accounts, "tester").post("/api/boxvault/usage", json=sent)
    assert (r.status_code, r.json()["error"]) == (400, says)
    assert usage.numbers == {}


def test_an_account_not_allowed_to_save_boxes_used_is_said_plainly(main, accounts, usage):
    usage.denied = True
    r = client(main, accounts, "tester").post("/api/boxvault/usage", json={"month": "2026-10", "numbers": [
        {"warehouse": "MWH_Pun_Khed_001", "sku": "DBSUPTMUM006", "used": 1}]})
    assert (r.status_code, r.json()["error"]) == (
        503, "The portal's database account isn't allowed to save boxes used (it needs SELECT, INSERT and UPDATE on "
             "scm.boxvault_sku_usage). Ask a database admin.")


class FakeConn:
    """A pymysql connection that records the SQL it's sent and answers `found`, or raises `error`."""

    def __init__(self, found=(), error=None):
        self.found, self.error, self.sent, self.committed, self.closed = list(found), error, [], False, False

    def cursor(self):
        conn = self

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, args=None):
                conn.sent.append((" ".join(sql.split()), args))
                if conn.error:
                    raise conn.error

            executemany = execute

            def fetchall(self):
                return conn.found

        return Cursor()

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


def test_saving_boxes_used_replaces_the_months_number(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(box_usage, "get_scm_connection", lambda: conn)
    assert REAL_SAVE(date(2026, 10, 1), [("MWH_Pun_Khed_001", "DBSUPTMUM006", 40)], "eddie") == 1
    (sql, args), = conn.sent
    assert sql.startswith("INSERT INTO boxvault_sku_usage (warehouse, sku, month, used, entered_by, entered_at) "
                          "VALUES (%s, %s, %s, %s, %s, %s) AS new ON DUPLICATE KEY UPDATE used = new.used")
    assert [a[:5] for a in args] == [("MWH_Pun_Khed_001", "DBSUPTMUM006", date(2026, 10, 1), 40, "eddie")]
    assert conn.committed and conn.closed


def test_the_usage_table_missing_or_refused_is_told_apart(monkeypatch):
    from pymysql.constants import ER
    from pymysql.err import OperationalError, ProgrammingError

    missing = ProgrammingError(ER.NO_SUCH_TABLE, "Table 'scm.boxvault_sku_usage' doesn't exist")
    monkeypatch.setattr(box_usage, "get_scm_connection", lambda: FakeConn(error=missing))
    with pytest.raises(box_usage.NotSetUp):
        REAL_RECORDS()
    with pytest.raises(box_usage.NotSetUp):
        REAL_SAVE(date(2026, 10, 1), [("MWH_Pun_Khed_001", "X", 1)], "eddie")
    denied = OperationalError(ER.TABLEACCESS_DENIED_ERROR, "INSERT command denied to user")
    monkeypatch.setattr(box_usage, "get_scm_connection", lambda: FakeConn(error=denied))
    with pytest.raises(box_usage.NotAllowed):
        REAL_SAVE(date(2026, 10, 1), [("MWH_Pun_Khed_001", "X", 1)], "eddie")


def test_the_boxes_used_entered_are_read_as_months(monkeypatch):
    conn = FakeConn(found=[("MWH_Pun_Khed_001", "DBSUPTMUM006", date(2026, 10, 1), 40, "eddie",
                            datetime(2026, 11, 16, 12, 30))])
    monkeypatch.setattr(box_usage, "get_scm_connection", lambda: conn)
    assert REAL_RECORDS() == [{"warehouse": "MWH_Pun_Khed_001", "sku": "DBSUPTMUM006", "month": "2026-10", "used": 40,
                               "entered_by": "eddie", "entered_at": "2026-11-16 12:30:00"}]
    assert conn.closed
