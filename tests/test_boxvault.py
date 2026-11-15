"""BoxVault's Upload invoices (boxvault.py, /api/boxvault*): what's checked, where files go in Drive, partial
failures, and who may upload. Google Drive is a stand-in that records what it was sent."""
import pytest
from fastapi.testclient import TestClient

import boxvault
import google_services

LOCAL = ("127.0.0.1", 50000)
FOLDER = "invoices-folder-id"


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
    assert r.json() == {"warehouse": "MWH_Pun_Khed_001", "uploaded": [
        {"name": "INV-1042.pdf", "link": "https://drive.example/INV-1042.pdf"},
        {"name": "scan 7.jpg", "link": "https://drive.example/scan 7.jpg"}]}
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
    assert r.json() == {
        "error": "2 of 3 files were uploaded, but these weren't: b.pdf. Please upload those again.",
        "uploaded": [{"name": "a.pdf", "link": "https://drive.example/a.pdf"},
                     {"name": "c.pdf", "link": "https://drive.example/c.pdf"}],
        "failed": ["b.pdf"]}
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
