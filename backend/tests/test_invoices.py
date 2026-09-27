"""Invoice filing (pos.invoices): the rules, the folder conventions, the guard, dedup and create-only."""

import hashlib
from datetime import date

import pytest

from pos import actors, agents, integrations
from pos.core import Ctx
from pos.db import connect, migrate
from pos.invoices import classify as rules
from pos.invoices import drive as paths
from pos.invoices import gapi, service

BIZ, OSOBNI = "ROOT_BIZ", "ROOT_OSOBNI"
ROOTS = {rules.BUSINESS: BIZ, rules.PERSONAL: OSOBNI}
FOLDER = paths.FOLDER


# ------------------------------------------------------------------ fakes

class FakeDrive:
    """An in-memory Drive: folders and files by id, with parents. Records every call."""

    def __init__(self):
        self.items: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.n = 0
        self.add("ROOT_MYDRIVE", "Můj disk", None, folder=True)
        self.add(BIZ, "Obseum Ucetnictvi", "ROOT_MYDRIVE", folder=True)
        self.add(OSOBNI, "Osobni", BIZ, folder=True)
        self.add("OUTSIDE", "Jiná složka", "ROOT_MYDRIVE", folder=True)

    def add(self, fid, name, parent, folder=False, data=b"", created=None):
        self.items[fid] = {"id": fid, "name": name, "mimeType": FOLDER if folder else "application/pdf",
                           "parents": [parent] if parent else [], "createdTime": created or f"2026-01-01T00:00:{len(self.items):02d}",
                           **({} if folder else {"md5Checksum": hashlib.md5(data).hexdigest()})}
        return fid

    def tree(self, *path):
        """Create nested folders under the business root: tree("2026", "Obseum s.r.o.", "Doklady", "09_Zari")."""
        parent = BIZ
        for name in path:
            found = next((i for i in self.items.values() if i["name"] == name and parent in i["parents"]), None)
            parent = found["id"] if found else self.add(f"F{len(self.items)}", name, parent, folder=True)
        return parent

    def children(self, folder_id):
        self.calls.append(("children", folder_id))
        return [dict(i) for i in self.items.values() if folder_id in i["parents"]]

    def parents(self, file_id):
        self.calls.append(("parents", file_id))
        return list(self.items[file_id]["parents"])

    def create_folder(self, parent_id, name):
        self.calls.append(("create_folder", parent_id, name))
        self.n += 1
        return dict(self.items[self.add(f"NEWF{self.n}", name, parent_id, folder=True)])

    def upload(self, parent_id, name, data, mime, description):
        self.calls.append(("upload", parent_id, name))
        self.n += 1
        fid = self.add(f"NEW{self.n}", name, parent_id, data=data)
        return {**self.items[fid], "webViewLink": f"https://drive.google.com/file/d/{fid}/view"}

    def writes(self):
        return [c for c in self.calls if c[0] in ("create_folder", "upload")]


class FakeGmail:
    boxes: dict[str, dict] = {}

    def __init__(self, address):
        self.address = address

    def labels(self):
        return {"L1": "invoice"}

    def search(self, query, limit=200):
        return list(self.boxes.get(self.address, {}))

    def message(self, mid, labels=None):
        if mid not in self.boxes.get(self.address, {}):
            raise gapi.GoogleError("404")
        return {**self.boxes[self.address][mid]["mail"], "account": self.address, "message_id": mid}

    def attachment(self, mid, att):
        return self.boxes[self.address][mid]["data"][att["part_id"]]


def mail(subject, sender, to, files, body="", ms=1790380800000):  # 2026-09-26
    return {"mail": {"subject": subject, "sender": sender, "to": to, "cc": "", "body": body, "labels": [],
                     "thread_id": "t", "internal_ms": ms,
                     "attachments": [{"part_id": str(i + 1), "filename": n, "mime": "application/pdf",
                                      "size": len(d), "attachment_id": f"A{i}"} for i, (n, d) in enumerate(files)]},
            "data": {str(i + 1): d for i, (n, d) in enumerate(files)}}


@pytest.fixture
def conn(tmp_path, monkeypatch):
    c = connect(tmp_path / "inv.db")
    migrate(c)
    actors.ensure_builtin(c)
    integrations.register_builtin_agents(c)
    c.commit()
    monkeypatch.setenv("POS_INVOICES_BUSINESS_ROOT", BIZ)
    monkeypatch.setenv("POS_INVOICES_PERSONAL_ROOT", OSOBNI)
    monkeypatch.setenv("POS_INVOICE_MAILBOXES", "david.rosko@obseum.cz,rosko.dav@gmail.com")
    yield c
    c.close()


def owner(conn):
    return Ctx(actors.owner_id(conn), via="test")


# ------------------------------------------------------------------ the rules (Czech and English)

def test_business_when_the_invoice_names_obseum():
    doc = ("FAKTURA - daňový doklad č. 1202453971 Dodavatel: Seyfor, a. s. Odběratel: Obseum s.r.o. "
           "Rybná 716/24 Praha 11000 IČ : 07098308 DIČ : CZ07098308")
    out = rules.classify({"subject": "Faktura", "sender": "fakturace@nekdo.cz", "to": "rosko.dav@gmail.com"}, doc)
    assert out["suggestion"] == rules.BUSINESS and out["confidence"] == "strong"
    assert any("IČO" in s for s in out["signals"])


def test_business_for_it_suppliers_in_english():
    out = rules.classify({"subject": "Hetzner Online GmbH - Invoice 088001142852",
                          "sender": "Noreply Billing - Hetzner Online GmbH <noreply.billing@hetzner.com>",
                          "to": "rosko.dav@gmail.com"}, "Invoice Cloud Server CX22 Date of issue September 3, 2026")
    assert (out["suggestion"], out["confidence"]) == (rules.BUSINESS, "strong")
    ai = rules.classify({"subject": "Your receipt from Eleven Labs Inc. #2804", "sender": "Eleven Labs Inc.",
                         "to": "rosko.dav@gmail.com"}, "Creator plan subscription")
    assert ai["suggestion"] == rules.BUSINESS and ai["confidence"] == "strong"
    telecom = rules.classify({"subject": "Moje O2 - vyúčtování za služby", "sender": "Moje O2 <mojeo2@o2.cz>",
                              "to": "rosko.dav@gmail.com"}, "Vyúčtování služeb mobilní tarif a internet")
    assert telecom["suggestion"] == rules.BUSINESS


def test_personal_only_when_clearly_unrelated():
    dance = rules.classify({"subject": "Your receipt from Dance Community #2126", "sender": "Dance Community",
                            "to": "rosko.dav@gmail.com", "account": "rosko.dav@gmail.com"},
                           "Invoice Dance class pass")
    assert (dance["suggestion"], dance["confidence"]) == (rules.PERSONAL, "strong")
    food = rules.classify({"subject": "Zdravestravovani.cz - faktura č. 2002635777", "sender": "info2@zdravestravovani.cz",
                           "to": "rosko.dav@gmail.com"},
                          "DAŇOVÝ DOKLAD Zdravé stravování s.r.o. Odběratel: Roško David Velvarská 1152 Horoměřice "
                          "krabičková dieta")
    assert food["suggestion"] == rules.PERSONAL


def test_unsure_goes_to_business_with_the_reason():
    # The air conditioning invoice (T-148): made out privately to the home address, but sent to the work address.
    tkp = rules.classify({"subject": "Zálohová faktura k nabídce TKP-N-0088", "sender": "info@tkprofi.cz",
                          "to": '"David Roško" <david.rosko@obseum.cz>', "account": "david.rosko@obseum.cz"},
                         "ZÁLOHOVÁ FAKTURA č. ZFP262004 David Rosko Statenice 252 62 Horoměřice "
                         "zálohu na dodávku a montáž klimatizace Midea")
    assert (tkp["suggestion"], tkp["confidence"]) == (rules.BUSINESS, "unsure")
    assert any("pracovní adresu" in s for s in tkp["signals"]) and any("klimatizac" in s for s in tkp["signals"])
    parking = rules.classify({"subject": "PIDLítačka - doklad k parkování", "sender": "noreply@pidlitacka.cz",
                              "to": "rosko.dav@gmail.com"}, "Parkovací oprávnění RZ EL823CF Celkem 30.00 CZK")
    assert (parking["suggestion"], parking["confidence"]) == (rules.BUSINESS, "unsure")
    assert "nejisté → firma" in parking["reason"]


def test_what_is_an_invoice():
    stripe = {"subject": "Your receipt from Anthropic Ireland, Limited #2358-6076", "sender": "Anthropic",
              "attachments": [{"filename": "Invoice-9BF0758D-6299301.pdf", "mime": "application/pdf"},
                              {"filename": "Receipt-2358-6076.pdf", "mime": "application/pdf"}]}
    assert rules.looks_like_invoice(stripe)[0]
    assert [a["filename"] for a in rules.pick_documents(stripe["attachments"])] == ["Invoice-9BF0758D-6299301.pdf"]
    quote = {"subject": "Nabidka na skoleni", "sender": "jaro@nowapp.cz",
             "attachments": [{"filename": "Nabidka_workshop.pdf", "mime": "application/pdf"}]}
    assert not rules.looks_like_invoice(quote)[0]
    terms = {"subject": "Už to chystáme. / Obj. č. 1058726948", "sender": "Alza.cz",
             "attachments": [{"filename": "VOP_RR_06082026.pdf", "mime": "application/octet-stream"}]}
    assert not rules.looks_like_invoice(terms)[0]
    no_pdf = {"subject": "Webex Starter renewal notification", "sender": "2Checkout",
              "attachments": [{"filename": "logo.jpg", "mime": "image/jpeg"}]}
    assert not rules.looks_like_invoice(no_pdf)[0]
    labelled = {"subject": "ACRCloud 202455-74109", "sender": "noreply@acrcloud.com", "labels": ["invoice"],
                "attachments": [{"filename": "202455-74109-20260927.pdf", "mime": "application/pdf"}]}
    assert rules.looks_like_invoice(labelled)[0]


def test_issue_date_and_file_name():
    assert rules.issue_date("Datum vystavení dokladu: 25.9. 2026 Datum splatnosti: 9.10.2026") == date(2026, 9, 25)
    assert rules.issue_date("Invoice number YGMQ-0040 Date of issue September 8, 2026") == date(2026, 9, 8)
    assert rules.issue_date("<IssueDate>2026-08-31</IssueDate>") == date(2026, 8, 31)
    assert rules.issue_date("no date here") is None
    assert rules.file_name("Hetzner_2026-09-03_088001142852.pdf", when=date(2026, 9, 3), sender="x",
                           number=None, fallback_id="m") == "Hetzner_2026-09-03_088001142852.pdf"
    assert rules.file_name("danovy-doklad-parkovani.pdf", when=date(2026, 9, 3),
                           sender='"PID Lítačka Parkování" <noreply@pidlitacka.cz>', number="1003178179",
                           fallback_id="m") == "2026-09-03 PID Lítačka Parkování 1003178179.pdf"
    assert rules.file_name("invoice.pdf", when=date(2026, 9, 1), sender="Pražská energetika, a. s. <f@pre.cz>",
                           number=None, fallback_id="abc123") == "2026-09-01 Pražská energetika abc123.pdf"
    assert rules.normalized_name("2611-143-Hetzner_2026-08-03.pdf") == rules.normalized_name("Hetzner_2026-08-03.pdf")


# ------------------------------------------------------------------ folder conventions and paths

def test_month_names():
    assert paths.month_of("09_Zari") == 9
    assert paths.month_of("03_Březen") == 3
    assert paths.month_of("Doklady nové - září") == 9
    assert paths.month_of("Doklady nové - červenec") == 7
    assert paths.month_of("Doklady nové - červen") == 6
    assert paths.month_of("09_2026") == 9 and paths.month_of("2026-09") == 9
    assert paths.month_of("Chybějící faktury 2025") is None
    assert paths.month_folder_name(10, 2026, ["01_Leden", "02_Unor", "03_Březen", "09_Zari"]) == "10_Říjen"
    assert paths.month_folder_name(10, 2026, ["08_2026", "09_2026"]) == "10_2026"
    assert paths.month_folder_name(10, 2026, ["Doklady nové - září"]) == "Doklady nové - říjen"
    assert paths.month_folder_name(1, 2027, []) == "01_Leden"


def test_business_path_uses_the_existing_folders():
    d = FakeDrive()
    for m in ("01_Leden", "02_Unor", "03_Březen", "08_Srpen", "09_Zari", "Chybějící faktury 2025"):
        d.tree("2026", "Obseum s.r.o.", "Doklady", m)
    d.tree("2025", "Obseum s.r.o.", "*Doklady nové", "Doklady nové - září")
    out = paths.resolve(d, rules.BUSINESS, date(2026, 9, 26), ROOTS)
    assert out["path"] == ["2026", "Obseum s.r.o.", "Doklady", "09_Zari"] and out["created"] == []
    assert d.writes() == []


def test_business_path_creates_a_missing_month_in_the_convention():
    d = FakeDrive()
    for m in ("01_Leden", "03_Březen", "09_Zari"):
        d.tree("2026", "obseum s.r.o", "Doklady", m)  # case and the missing dot still match
    out = paths.resolve(d, rules.BUSINESS, date(2026, 10, 2), ROOTS)
    assert out["path"] == ["2026", "obseum s.r.o", "Doklady", "10_Říjen"]
    assert out["created"] == ["2026/obseum s.r.o/Doklady/10_Říjen"]
    new_year = paths.resolve(d, rules.BUSINESS, date(2027, 1, 5), ROOTS)
    assert new_year["path"] == ["2027", "Obseum s.r.o.", "Doklady", "01_Leden"]
    assert len(new_year["created"]) == 4


def test_personal_path_follows_osobni():
    d = FakeDrive()
    assert paths.resolve(d, rules.PERSONAL, date(2026, 9, 1), ROOTS)["folder_id"] == OSOBNI  # empty: directly
    y = d.add("Y26", "2026", OSOBNI, folder=True)
    d.add("M08", "08_Srpen", y, folder=True)
    out = paths.resolve(d, rules.PERSONAL, date(2026, 9, 1), ROOTS)
    assert out["path"] == ["2026", "09_Září"]


def test_guard_refuses_folders_outside_the_two_roots():
    d = FakeDrive()
    inside = d.tree("2026", "Obseum s.r.o.", "Doklady", "09_Zari")
    paths.guard(d, inside, rules.BUSINESS, ROOTS)
    paths.guard(d, OSOBNI, rules.PERSONAL, ROOTS)
    with pytest.raises(paths.Refused):
        paths.guard(d, "OUTSIDE", rules.BUSINESS, ROOTS)
    with pytest.raises(paths.Refused):
        paths.guard(d, "OUTSIDE", rules.PERSONAL, ROOTS)
    with pytest.raises(paths.Refused):  # Osobni lies inside the business root, but business never goes there
        paths.guard(d, OSOBNI, rules.BUSINESS, ROOTS)
    with pytest.raises(paths.Refused):
        paths.guard(d, inside, rules.PERSONAL, ROOTS)
    guarded = service.GuardedDrive(d, rules.BUSINESS, ROOTS)
    with pytest.raises(paths.Refused):
        guarded.upload("OUTSIDE", "x.pdf", b"%PDF", "application/pdf", "")
    with pytest.raises(paths.Refused):
        guarded.create_folder("OUTSIDE", "2026")
    assert d.writes() == []


def test_the_client_never_updates_or_deletes(monkeypatch):
    monkeypatch.setattr(gapi, "_access_token", lambda env: "t")
    for method in ("PATCH", "PUT", "DELETE"):
        with pytest.raises(gapi.GoogleError):
            gapi._call("X", method, "https://www.googleapis.com/drive/v3/files/abc")
    assert not any(hasattr(gapi.Drive, m) for m in ("update", "delete", "move", "trash", "copy"))


# ------------------------------------------------------------------ filing: dedup and create-only

def _setup(conn, boxes):
    FakeGmail.boxes = boxes
    d = FakeDrive()
    month = d.tree("2026", "Obseum s.r.o.", "Doklady", "09_Zari")
    return d, month


def test_file_once_and_dedup_across_mailboxes(conn):
    pdf = b"%PDF-1.4 hetzner invoice bytes"
    boxes = {"david.rosko@obseum.cz": {"m1": mail("Hetzner - Invoice 0880", "billing@hetzner.com",
                                                  "david.rosko@obseum.cz", [("Hetzner_2026-09-03.pdf", pdf)])},
             "rosko.dav@gmail.com": {"m2": mail("Fwd: Hetzner - Invoice 0880", "me", "rosko.dav@gmail.com",
                                                [("Hetzner_2026-09-03.pdf", pdf)])}}
    d, month = _setup(conn, boxes)
    first = service.file_invoice(conn, owner(conn), "m1", None, "business", "hosting (IT)",
                                 "david.rosko@obseum.cz", drive=d, gmail_factory=FakeGmail)
    assert first["status"] == "filed" and first["folder_path"] == "Obseum Ucetnictvi/2026/Obseum s.r.o./Doklady/09_Zari"
    assert first["drive_link"].startswith("https://drive.google.com/")
    again = service.file_invoice(conn, owner(conn), "m2", "1", "business", "hosting (IT)", None,
                                 drive=d, gmail_factory=FakeGmail)  # the other mailbox, found without `account`
    assert again["status"] == "already_filed" and again["drive_file_id"] == first["drive_file_id"]
    assert [c for c in d.writes() if c[0] == "upload"] == [("upload", month, "Hetzner_2026-09-03.pdf")]
    rows = conn.execute("SELECT status, account FROM invoice_filings ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("filed", "david.rosko@obseum.cz"), ("duplicate", "rosko.dav@gmail.com")]
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'invoice_file'").fetchone()[0] == 2
    note = conn.execute("SELECT body, topic FROM notes WHERE title LIKE 'Faktury na Disku %'").fetchone()
    assert note["topic"] == "finance" and "Hetzner_2026-09-03.pdf" in note["body"] and "**firma**" in note["body"]


def test_a_file_already_in_the_folder_is_not_uploaded_again(conn):
    pdf = b"%PDF chainstack"
    d, month = _setup(conn, {"david.rosko@obseum.cz": {"m1": mail("Your receipt from Chainstack", "c",
                                                                  "david.rosko@obseum.cz",
                                                                  [("Invoice-YGMQGXRC-0040.pdf", pdf)])}})
    d.add("EXIST", "2611-150-Invoice-YGMQGXRC-0040.pdf", month, data=b"other bytes, same invoice number")
    out = service.file_invoice(conn, owner(conn), "m1", "1", "business", "IT služby", "david.rosko@obseum.cz",
                               drive=d, gmail_factory=FakeGmail)
    assert out["status"] == "duplicate" and out["drive_file_id"] == "EXIST"
    assert d.writes() == []


def test_never_overwrites_a_taken_name(conn):
    d, month = _setup(conn, {"david.rosko@obseum.cz": {"m1": mail("Faktura", "x", "david.rosko@obseum.cz",
                                                                  [("invoice.pdf", b"%PDF new one")])}})
    d.add("OLD", "2026-09-26 x m1.pdf", month, data=b"%PDF old one")
    out = service.file_invoice(conn, owner(conn), "m1", "1", "business", "IT služby", "david.rosko@obseum.cz",
                               drive=d, gmail_factory=FakeGmail)
    assert out["status"] == "filed" and out["file_name"] == "2026-09-26 x m1 (2).pdf"
    assert d.items["OLD"]["name"] == "2026-09-26 x m1.pdf"  # untouched
    assert all(c[0] in ("children", "parents", "upload") for c in d.calls)


def test_personal_goes_to_osobni_and_bad_input_is_refused(conn):
    d, _ = _setup(conn, {"rosko.dav@gmail.com": {"m9": mail("Your receipt from Dance Community", "Dance",
                                                             "rosko.dav@gmail.com",
                                                             [("Invoice-UXXEIOUP-0001.pdf", b"%PDF dance")])}})
    out = service.file_invoice(conn, owner(conn), "m9", None, "personal", "taneční kurz, nic firemního",
                               "rosko.dav@gmail.com", drive=d, gmail_factory=FakeGmail)
    assert out["folder_path"] == "Osobni" and d.items[out["drive_file_id"]]["parents"] == [OSOBNI]
    with pytest.raises(service.Refused):
        service.file_invoice(conn, owner(conn), "m9", None, "maybe", "x y z", "rosko.dav@gmail.com",
                             drive=d, gmail_factory=FakeGmail)
    with pytest.raises(service.Refused):
        service.file_invoice(conn, owner(conn), "m9", None, "business", "", "rosko.dav@gmail.com",
                             drive=d, gmail_factory=FakeGmail)


def test_poll_files_sure_ones_and_gives_unsure_ones_to_the_cfo(conn, monkeypatch, tmp_path):
    agents.create_agent(conn, owner(conn), name="CFO", purpose="finance", lifetime="long_lived",
                        data_dir=tmp_path)
    conn.commit()
    monkeypatch.setattr(service, "document_text", lambda name, data: data.decode(errors="replace"))
    d, month = _setup(conn, {
        "david.rosko@obseum.cz": {
            "h1": mail("Hetzner Online GmbH - Invoice 0880", "billing@hetzner.com", "david.rosko@obseum.cz",
                       [("Hetzner_2026-09-03_0880.pdf", b"Invoice Cloud Server Date of issue September 3, 2026")]),
            "t1": mail("Zálohová faktura k nabídce TKP-N-0088", "info@tkprofi.cz", "david.rosko@obseum.cz",
                       [("Zálohová_faktura_ZFP262004.pdf", b"David Rosko Statenice montaz klimatizace")]),
            "q1": mail("Nabidka na skoleni", "jaro@nowapp.cz", "david.rosko@obseum.cz",
                       [("Nabidka.pdf", b"nabidka")])},
        "rosko.dav@gmail.com": {}})
    out = service.poll(conn, gmail_factory=FakeGmail, drive=d)
    assert out == {"filed": 1, "tasks": 1, "not_invoice": 1}
    task = conn.execute("SELECT title, assignee_name, notes FROM tasks WHERE source = 'invoices'").fetchone()
    assert task["assignee_name"] == "CFO" and "TKP-N-0088" in task["title"] and "invoice_file" in task["notes"]
    assert service.poll(conn, gmail_factory=FakeGmail, drive=d) == {}  # each mail once
