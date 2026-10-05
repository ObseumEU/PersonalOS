"""The customer-issue pipeline (pos.support): the classifier, project matching, one task per thread, the
reply draft (a mock Gmail: the thread, the headers, never a send) and the owner's one item."""

import base64
import email
import json

import httpx
import pytest

from pos import actors, agents, integrations, needs_me, projects, routing, tasks
from pos.core import Ctx
from pos.db import connect, migrate
from pos.invoices import gapi
from pos.support import classify as cls
from pos.support import gmail as gm
from pos.support import match, service

OBSEUM, PERSONAL = "david.rosko@obseum.cz", "rosko.dav@gmail.com"


# ------------------------------------------------------------------ fixtures

def _agent(conn, owner, tmp_path, name, role, team=None):
    made = agents.create_agent(conn, owner, name=name, purpose=name, lifetime="long_lived", data_dir=tmp_path,
                               permissions=["tasks:read", "tasks:write", "tasks:claim", "tasks:review",
                                            "messages:send", "approvals:request"])
    aid = made["agent"]["id"]
    conn.execute("UPDATE actors SET role = ?, team = ? WHERE id = ?", (role, team, aid))
    conn.commit()
    return aid


@pytest.fixture
def conn(tmp_path, monkeypatch):
    c = connect(tmp_path / "support.db")
    migrate(c)
    actors.ensure_builtin(c)
    integrations.register_builtin_agents(c)
    monkeypatch.setenv("POS_INVOICE_MAILBOXES", f"{OBSEUM},{PERSONAL}")
    owner = Ctx(actors.owner_id(c), via="test")
    _agent(c, owner, tmp_path, "Head of Customer Success", "customer_success", "customers")
    _agent(c, owner, tmp_path, "Software Engineer", "developer", "engineering")
    _agent(c, owner, tmp_path, "CFO", "cfo", "finance")
    lead = _agent(c, owner, tmp_path, "Kniha Lead", "product_lead", "kniha")
    dev = _agent(c, owner, tmp_path, "Kniha Developer", "developer", "kniha")
    projects.create(c, owner, name="Kniha", goal="Business s rodinnou audioknihou", lead=lead,
                    member_refs=[dev], channel=False)
    c.commit()
    yield c
    c.close()


def owner_ctx(conn):
    return Ctx(actors.owner_id(conn), via="test")


def model_says(answer: dict | None, calls: list | None = None):
    def model(prompt):
        if calls is not None:
            calls.append(prompt)
        return None if answer is None else "```json\n" + json.dumps(answer, ensure_ascii=False) + "\n```"
    return model


def mail(subject, sender, body, **kw):
    return {"account": OBSEUM, "thread_id": kw.pop("thread_id", "18a0000000000001"), "subject": subject,
            "sender": sender, "to": OBSEUM, "body": body, "attachments": kw.pop("attachments", []), "count": 1,
            "url": "", **kw}


# ------------------------------------------------------------------ the classifier (Czech and English)

BUG_CS = mail("Nefunguje odeslání rezervace", "Jana Nováková <jana@rodinnaknihovna.cz>",
              "Dobrý den, na https://rodinne-pribehy.obseum.cz/rezervace po kliknutí na Odeslat vyskočí chyba 500 "
              "a rezervace se neuloží. Zkoušela jsem to v Chrome i na mobilu. Děkuji, Jana")
FEATURE_EN = mail("Feature idea: export to PDF", "Tom Baker <tom@acme.io>",
                  "Hi David, it would be great if PersonalOS could export a project page to PDF. Could you add "
                  "that? Thanks, Tom")
NEWSLETTER = mail("Novinky z e-shopu: podzimní slevy", "Zabublej.cz <obchod@zabublej.cz>",
                  "Ahoj ze Zabublej! Máme pro tebe novinku, věrnostní program.")
INVOICE = mail("Faktura 2026-0912 za hosting", "Fakturace <fakturace@hostingfirma.cz>",
               "Dobrý den, v příloze zasíláme fakturu. Služba nefunguje? Napište nám.",
               attachments=[{"filename": "faktura-2026-0912.pdf"}])
QUESTION_EN = mail("Question about the API", "Anna <anna@client.de>",
                   "Hello, how do I get an API key for knowlage? Is there documentation?")


def test_czech_bug_report_by_the_model():
    calls = []
    out = cls.classify(BUG_CS, model=model_says({
        "kind": "bug_report", "confidence": 0.9, "summary": "Rezervace na webu končí chybou 500.",
        "customer": "Rodinná knihovna", "severity": "P2", "language": "cs", "project_hint": "Kniha",
        "repro_steps": ["Otevřít /rezervace", "Kliknout na Odeslat"],
        "affected_url": "https://rodinne-pribehy.obseum.cz/rezervace", "reason": "chyba 500"}, calls))
    assert out["kind"] == "bug_report" and out["source"] == "llm" and out["severity"] == "P2"
    assert out["language"] == "cs" and out["repro_steps"][1] == "Kliknout na Odeslat"
    assert "untrusted data" in calls[0] and "<mail>" in calls[0]


def test_heuristic_without_a_model_is_unsure():
    bug = cls.classify(BUG_CS, model=model_says(None))
    assert bug["kind"] == "bug_report" and bug["source"] == "heuristic" and bug["confidence"] < service.MIN_CONFIDENCE
    assert bug["affected_url"].startswith("https://rodinne-pribehy.obseum.cz")
    assert cls.classify(FEATURE_EN)["kind"] == "feature_request"
    assert cls.classify(QUESTION_EN)["kind"] == "question"
    assert cls.classify(FEATURE_EN)["language"] == "en" and cls.classify(BUG_CS)["language"] == "cs"


def test_feature_request_and_question_by_the_model():
    fr = cls.classify(FEATURE_EN, model=model_says({"kind": "feature_request", "confidence": 0.85, "severity": "P3",
                                                     "summary": "Export stránky projektu do PDF."}))
    assert fr["kind"] == "feature_request" and fr["kind"] not in cls.ISSUE_KINDS
    q = cls.classify(QUESTION_EN, model=model_says({"kind": "question", "confidence": 0.8, "language": "en"}))
    assert q["kind"] == "question"


def test_invoice_is_never_a_support_issue_and_costs_no_model_call():
    calls = []
    out = cls.classify(INVOICE, model=model_says({"kind": "bug_report", "confidence": 1}, calls))
    assert out["kind"] == "not_customer" and out["source"] == "prefilter" and "CFO" in out["reason"]
    assert calls == []


def test_newsletter_is_not_a_customer():
    calls = []
    out = cls.classify(NEWSLETTER, model=model_says({"kind": "not_customer", "confidence": 0.95}, calls))
    assert out["kind"] == "not_customer"
    # Our own mail, automatic senders and out-of-office replies never reach the model.
    for m in (mail("Re: Nefunguje", "David Roško <david.rosko@obseum.cz>", "Opraveno"),
              mail("Recovered: swap", "Netdata Cloud <no-reply@netdata.cloud>", "System swap 79%"),
              mail("Automatická odpověď: chyba", "Michal <michal@o2.cz>", "I am out of office")):
        assert cls.classify(m, model=model_says({"kind": "bug_report"}, calls))["kind"] == "not_customer"
    assert len(calls) == 1


def test_a_broken_model_answer_falls_back():
    assert cls.parse("no json here", BUG_CS) is None
    assert cls.parse('{"kind": "spam"}', BUG_CS) is None
    out = cls.classify(BUG_CS, model=lambda p: "garbage")
    assert out["source"] == "heuristic"


# ------------------------------------------------------------------ project matching

def test_project_by_domain_and_the_team_developer(conn):
    p = match.match(conn, BUG_CS, hint="")
    assert p["slug"] == "kniha" and any("doména" in w for w in p["why"])
    assert match.developer_for(conn, p) == "Kniha Developer"


def test_product_by_name_goes_to_the_software_engineer(conn):
    p = match.match(conn, FEATURE_EN)
    assert p["slug"] == "personalos" and p["id"] is None
    assert match.developer_for(conn, p) == "Software Engineer"


def test_repo_links_of_a_rich_project_and_the_hint(conn):
    projects.create(conn, owner_ctx(conn), name="Chatpulse", goal="Alerting nad hovory",
                    labels=["repo:ObseumEU/chatpulse", "domain:chatpulse.obseum.cz"], channel=False)
    m = mail("Alerty nechodí", "Michal <michal@o2.cz>", "Od včera nechodí upozornění z chatpulse.obseum.cz.")
    p = match.match(conn, m)
    assert p["slug"] == "chatpulse" and "obseumeu/chatpulse" in [r.lower() for r in p["repos"]]
    assert match.developer_for(conn, p) == "Software Engineer"  # a project without a team developer
    assert match.match(conn, mail("Dotaz", "x@y.cz", "Jak se máte?")) is None
    hinted = match.match(conn, mail("Chyba", "x@y.cz", "Něco nefunguje."), hint="Chatpulse")
    assert hinted["slug"] == "chatpulse"


# ------------------------------------------------------------------ triage: one task per thread

ISSUE = {"kind": "bug_report", "confidence": 0.9, "summary": "Rezervace končí chybou 500.",
         "customer": "Rodinná knihovna", "severity": "P1", "language": "cs", "project_hint": "Kniha",
         "repro_steps": ["Otevřít /rezervace"], "affected_url": "https://rodinne-pribehy.obseum.cz/rezervace"}


def test_bug_becomes_one_task_for_the_project_developer_and_follow_ups_are_comments(conn):
    out = service.handle(conn, BUG_CS, model=model_says(ISSUE), kb=lambda m: "")
    assert out["outcome"] == "issue" and out["assignee"] == "Kniha Developer"
    t = tasks.get(conn, owner_ctx(conn), out["task_id"])
    assert t["title"].startswith("Zákaznický problém: Rodinná knihovna") and t["priority"] == 1
    assert t["project_id"] and "Kroky k reprodukci" in t["notes"] and "commit" in t["notes"]
    assert "kniha-deployer" in t["notes"]  # the project's shipping path
    again = service.handle(conn, {**BUG_CS, "body": "Pořád to nejde.", "count": 2}, model=model_says(ISSUE),
                           kb=lambda m: "")
    assert again["outcome"] == "follow_up" and again["task_id"] == out["task_id"]
    n = conn.execute("SELECT COUNT(*) FROM tasks WHERE source = ?", (service.SOURCE_ISSUE,)).fetchone()[0]
    assert n == 1
    body = conn.execute("SELECT body FROM task_comments WHERE task_id = ? ORDER BY id DESC",
                        (out["task_id"],)).fetchone()["body"]
    assert "Pořád to nejde" in body
    # open_issue itself dedups too (the CS head's tool on the same thread).
    dup = service.open_issue(conn, owner_ctx(conn), BUG_CS, {**ISSUE, "source": "llm"}, None)
    assert dup["deduplicated"] and dup["id"] == out["task_id"]


def test_unclear_goes_to_customer_success(conn):
    o2 = mail("Innogy - nedostupný report", "Zbyněk <zbynek@o2its.cz>",
              "Endpoint vrací 500 Internal Server Error. Prosím o prověření.", thread_id="18a0000000000002")
    out = service.handle(conn, o2, model=model_says({**ISSUE, "project_hint": "", "customer": "O2 ITS"}),
                         kb=lambda m: "")
    assert out["outcome"] == "triage" and out["assignee"] == "Head of Customer Success"
    t = tasks.get(conn, owner_ctx(conn), out["task_id"])
    assert "support_issue_open" in t["notes"] and "projekt se nepodařilo určit" in t["notes"]


def test_old_thread_we_already_answered_is_flagged(conn):
    m = {**BUG_CS, "thread_id": "18a0000000000003", "replied_by_us": ["2026-09-11"], "age_days": 16}
    out = service.handle(conn, m, model=model_says(ISSUE), kb=lambda m: "")
    notes = tasks.get(conn, owner_ctx(conn), out["task_id"])["notes"]
    assert "starší vlákno" in notes and "neměň produkční kód" in notes


def test_routing_defers_mail_and_the_intake_routes_invoices_to_the_cfo(conn, monkeypatch):
    monkeypatch.setenv("POS_SUPPORT_INTAKE", "1")
    me = owner_ctx(conn)
    queued = routing.ingest(conn, me, {"source": "gmail", "kind": "email", "title": "Faktura 2026-0912",
                                       "body": "V příloze faktura.", "ref": "hostingfirma.cz:18a00000000000aa",
                                       "author": "fakturace@hostingfirma.cz"})
    assert queued["queued"] == "customer_issue_intake" and queued["task_id"] is None
    news = routing.ingest(conn, me, {"source": "gmail", "title": "Sale!", "ref": "n1",
                                     "meta": {"headers": {"List-Unsubscribe": "<mailto:x@y>"}}})
    assert news.get("skipped")  # the newsletter prefilter still runs first
    bug = routing.ingest(conn, me, {"source": "gmail", "kind": "email", "title": "Nefunguje rezervace",
                                    "body": BUG_CS["body"], "ref": "rodinnaknihovna.cz:18a00000000000bb",
                                    "author": BUG_CS["sender"]})
    conn.commit()
    out = service.process_pending(conn, model=model_says(ISSUE), kb=lambda m: "")
    assert out == {"routed": 1, "issue": 1}
    inv = conn.execute("SELECT e.signals, t.assignee_name FROM events e JOIN tasks t ON t.id = e.task_id "
                       "WHERE e.id = ?", (queued["event_id"],)).fetchone()
    assert inv["assignee_name"] == "CFO"
    ev = conn.execute("SELECT signals, task_id FROM events WHERE id = ?", (bug["event_id"],)).fetchone()
    assert ev["signals"] == "support:issue" and ev["task_id"]
    assert service.process_pending(conn, model=model_says(ISSUE)) == {}


# ------------------------------------------------------------------ the draft (mock Gmail)

THREAD = "18a0000000000001"


def _msg(mid, frm, to, subject, ms, msgid, refs="", labels=("INBOX",), cc=""):
    heads = [("From", frm), ("To", to), ("Subject", subject), ("Message-ID", msgid), ("Date", "x")]
    if refs:
        heads.append(("References", refs))
    if cc:
        heads.append(("Cc", cc))
    return {"id": mid, "threadId": THREAD, "labelIds": list(labels), "internalDate": str(ms),
            "payload": {"headers": [{"name": n, "value": v} for n, v in heads]}}


THREAD_DATA = {"id": THREAD, "messages": [
    _msg("m1", "Jana Nováková <jana@rodinnaknihovna.cz>", OBSEUM, "Nefunguje odeslání rezervace", 1, "<a1@mail.cz>",
         cc="Petr <petr@rodinnaknihovna.cz>, David <david.rosko@obseum.cz>"),
    _msg("m2", f"David Roško <{OBSEUM}>", "jana@rodinnaknihovna.cz", "Re: Nefunguje odeslání rezervace", 2,
         "<b2@obseum.cz>", refs="<a1@mail.cz>", labels=("SENT",)),
    _msg("m3", "Jana Nováková <jana@rodinnaknihovna.cz>", OBSEUM, "Re: Nefunguje odeslání rezervace", 3,
         "<c3@mail.cz>", refs="<a1@mail.cz> <b2@obseum.cz>", cc="Petr <petr@rodinnaknihovna.cz>"),
    _msg("d9", f"David Roško <{OBSEUM}>", "jana@rodinnaknihovna.cz", "Re: x", 4, "<d9@obseum.cz>", labels=("DRAFT",)),
]}


class FakeReader:
    def __init__(self, address):
        self.address = address

    def get(self, path, **params):
        assert path == f"threads/{THREAD}" and params["format"] == "metadata"
        return THREAD_DATA

    def message(self, mid, labels=None):
        return {"account": self.address, "message_id": mid, "thread_id": THREAD, "subject": "Re: Nefunguje",
                "sender": "Jana Nováková <jana@rodinnaknihovna.cz>", "to": OBSEUM, "cc": "", "body": "Pořád chyba.",
                "attachments": [], "labels": []}


@pytest.fixture
def google(monkeypatch):
    """A mock Gmail behind gapi's transport: the token endpoint and the drafts API. Records every request."""
    seen = []
    drafts = {}

    def handler(request: httpx.Request):
        seen.append((request.method, str(request.url)))
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
        path = request.url.path
        if request.method == "POST" and path.endswith("/drafts"):
            body = json.loads(request.content)
            n = f"r{len(drafts) + 1}"
            drafts[n] = body
            return httpx.Response(200, json={"id": n, "message": {"id": f"msg{n}", "threadId": body["message"]["threadId"]}})
        if request.method == "GET" and "/drafts/" in path:
            raw = base64.urlsafe_b64decode(drafts[path.rsplit("/", 1)[1]]["message"]["raw"])
            subject = email.message_from_bytes(raw)["Subject"]
            return httpx.Response(200, json={"id": "x", "message": {"payload": {"headers": [
                {"name": "Subject", "value": str(email.header.make_header(email.header.decode_header(subject)))}]}}})
        if request.method == "DELETE" and "/drafts/" in path:
            drafts.pop(path.rsplit("/", 1)[1])
            return httpx.Response(204)
        if request.method == "PUT" and "/drafts/" in path:
            n = path.rsplit("/", 1)[1]
            drafts[n] = json.loads(request.content)
            return httpx.Response(200, json={"id": n, "message": {"id": f"msg{n}b"}})
        return httpx.Response(404, json={"error": "unexpected"})

    monkeypatch.setattr(gapi, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(gapi, "_tokens", {})
    monkeypatch.setenv("POS_GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("POS_GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv(gm.compose_env(OBSEUM), "refresh")
    return {"seen": seen, "drafts": drafts}


def _raw(google, n="r1"):
    return email.message_from_bytes(base64.urlsafe_b64decode(google["drafts"][n]["message"]["raw"]))


def test_draft_replies_in_the_thread_with_the_right_headers(conn, google):
    service.handle(conn, BUG_CS, model=model_says(ISSUE), kb=lambda m: "")
    out = service.create_draft(conn, Ctx(actors.find_by_name(conn, "Head of Customer Success")["id"], via="mcp"),
                               THREAD, OBSEUM, "Dobrý den, paní Nováková,\n\nchybu jsme opravili (commit abc1234). "
                               "Zkuste prosím rezervaci odeslat znovu.", fixed="oprava odeslání rezervace",
                               gmail_factory=FakeReader)
    assert out["sent"] is False and out["draft_id"] == "r1" and "#drafts?compose=msgr1" in out["link"]
    assert google["drafts"]["r1"]["message"]["threadId"] == THREAD
    msg = _raw(google)
    assert msg["In-Reply-To"] == "<c3@mail.cz>"  # the customer's last message, not our reply nor a draft
    assert msg["References"] == "<a1@mail.cz> <b2@obseum.cz> <c3@mail.cz>"
    assert "jana@rodinnaknihovna.cz" in msg["To"] and "petr@rodinnaknihovna.cz" in msg["Cc"]
    assert OBSEUM not in (msg["Cc"] or "") and OBSEUM in msg["From"]
    assert str(email.header.make_header(email.header.decode_header(msg["Subject"]))) == "Re: Nefunguje odeslání rezervace"
    parts = {p.get_content_type(): p.get_payload(decode=True).decode() for p in msg.walk() if not p.is_multipart()}
    assert set(parts) == {"text/plain", "text/html"}
    assert parts["text/plain"].rstrip().endswith("Rodinné příběhy\ndavid.rosko@obseum.cz")  # David's signature
    assert "S pozdravem" in parts["text/plain"] and "<p>" in parts["text/html"]
    # Never a send: only the token endpoint and the drafts API were called.
    assert all("/send" not in url for _, url in google["seen"])
    assert [m for m, u in google["seen"] if "gmail" in u] == ["POST"]
    audit = conn.execute("SELECT detail FROM audit_log WHERE action = 'gmail_create_draft'").fetchone()
    assert json.loads(audit["detail"])["in_reply_to"] == "<c3@mail.cz>"


def test_customer_success_fixes_its_reply_draft_in_place_and_can_delete_it(conn, google):
    from pos import outbound_drafts

    service.handle(conn, BUG_CS, model=model_says(ISSUE), kb=lambda m: "")
    cs = Ctx(actors.find_by_name(conn, "Head of Customer Success")["id"], via="mcp")
    service.create_draft(conn, cs, THREAD, OBSEUM, "Dobrý den, chybu jsme opravili, zkuste to prosím znovu.",
                         gmail_factory=FakeReader)
    out = outbound_drafts.update_draft(conn, cs, "r1", "Dobrý den, chybu jsme opravili a nasadili, zkuste to znovu.",
                                       gmail_factory=FakeReader)
    assert out["draft_id"] == "r1" and set(google["drafts"]) == {"r1"}  # the same draft, no second one
    msg = _raw(google)
    assert msg["In-Reply-To"] == "<c3@mail.cz>" and google["drafts"]["r1"]["message"]["threadId"] == THREAD
    assert "nasadili" in next(p for p in msg.walk() if p.get_content_type() == "text/plain").get_payload(decode=True).decode()
    assert outbound_drafts.delete_draft(conn, cs, "r1", "špatný koncept")["deleted"] and not google["drafts"]
    row = conn.execute("SELECT draft_id FROM support_threads WHERE thread_id = ?", (THREAD,)).fetchone()
    assert row["draft_id"] is None
    ledger = conn.execute("SELECT status FROM outbound_sends WHERE action = 'email.draft_delete'").fetchone()
    assert ledger["status"] == "draft_deleted"
    assert all("/send" not in url for _, url in google["seen"])


def test_the_client_refuses_anything_but_drafts(google):
    with pytest.raises(gm.Refused):
        gm._request(OBSEUM, "POST", "messages/send", json={})
    with pytest.raises(gm.Refused):
        gm._request(OBSEUM, "POST", "drafts/send", json={})
    with pytest.raises(gm.Refused):
        gm._request(OBSEUM, "POST", "messages", json={})
    with pytest.raises(gm.Refused):
        gm._request(OBSEUM, "DELETE", "drafts/r1")  # only through delete_test_draft
    assert not google["seen"]  # refused before any token was fetched
    assert not hasattr(gm, "send") and not any("send" in n for n in dir(gm) if not n.startswith("_") and n != "gmail")


def test_test_drafts_are_marked_and_only_they_can_be_deleted(conn, google):
    ctx = owner_ctx(conn)
    out = service.create_draft(conn, ctx, THREAD, OBSEUM, "Testovací odpověď zákazníkovi, prosím neodesílat.",
                               test=True, needs_me=False, gmail_factory=FakeReader)
    assert str(email.header.make_header(email.header.decode_header(_raw(google)["Subject"]))).startswith("[TEST] Re:")
    real = service.create_draft(conn, ctx, THREAD, OBSEUM, "Skutečná odpověď zákazníkovi, bez testu.",
                                needs_me=False, gmail_factory=FakeReader)
    with pytest.raises(gm.Refused):
        gm.delete_test_draft(OBSEUM, real["draft_id"])
    assert gm.delete_test_draft(OBSEUM, out["draft_id"])["deleted"] == "r1"
    assert set(google["drafts"]) == {"r2"}


def test_drafts_need_the_compose_token_and_a_known_mailbox(conn, google, monkeypatch):
    with pytest.raises(service.Refused):
        service.create_draft(conn, owner_ctx(conn), THREAD, "someone@else.cz", "x" * 30, gmail_factory=FakeReader)
    monkeypatch.delenv(gm.compose_env(OBSEUM))
    with pytest.raises(service.Refused, match="not set up"):
        service.create_draft(conn, owner_ctx(conn), THREAD, OBSEUM, "x" * 30, gmail_factory=FakeReader)


# ------------------------------------------------------------------ the fix hand-in and the owner's item

def test_hand_in_asks_for_the_draft_and_the_owner_gets_one_item(conn, google):
    out = service.handle(conn, BUG_CS, model=model_says(ISSUE), kb=lambda m: "")
    dev = Ctx(actors.find_by_name(conn, "Kniha Developer")["id"], via="mcp")
    tasks.complete(conn, dev, out["task_id"], "Příčina: chybějící validace. Opraveno v commitu abc1234, "
                                              "regresní test, ověřeno na produkci.")
    row = conn.execute("SELECT * FROM support_threads WHERE issue_task_id = ?", (out["task_id"],)).fetchone()
    draft_task = tasks.get(conn, owner_ctx(conn), row["draft_task_id"])
    assert draft_task["assignee_name"] == "Head of Customer Success" and row["draft_mode"] == "fixed"
    assert "abc1234" in draft_task["notes"] and "gmail_create_draft" in draft_task["notes"]
    cs = Ctx(actors.find_by_name(conn, "Head of Customer Success")["id"], via="mcp")
    before = needs_me.collect(conn, owner_ctx(conn))["count"]
    service.create_draft(conn, cs, THREAD, OBSEUM, "Dobrý den, chybu jsme opravili (commit abc1234).",
                         fixed="oprava odeslání rezervace", gmail_factory=FakeReader)
    items = [i for i in needs_me.collect(conn, owner_ctx(conn))["items"] if i["title"].startswith("Koncept odpovědi")]
    assert len(items) == 1 and needs_me.collect(conn, owner_ctx(conn))["count"] == before + 1
    assert items[0]["title"] == "Koncept odpovědi pro Rodinná knihovna je v Gmailu: oprava odeslání rezervace"
    notes = tasks.get(conn, owner_ctx(conn), items[0]["id"])["notes"]
    assert "#drafts?compose=" in notes and tasks.display_id(out["task_id"]) in notes
    # A second draft for the same issue updates the item; no second item, no chat ping.
    service.create_draft(conn, cs, THREAD, OBSEUM, "Dobrý den, doplňuji: oprava je nasazená.", fixed="oprava a nasazení",
                         gmail_factory=FakeReader)
    items = [i for i in needs_me.collect(conn, owner_ctx(conn))["items"] if i["title"].startswith("Koncept odpovědi")]
    assert len(items) == 1 and items[0]["title"].endswith("oprava a nasazení")
    assert conn.execute("SELECT COUNT(*) FROM chat_messages WHERE body LIKE '%Koncept odpovědi%'").fetchone()[0] == 0


def test_time_box_asks_for_a_status_draft(conn):
    out = service.handle(conn, BUG_CS, model=model_says(ISSUE), kb=lambda m: "")  # P1: one hour
    conn.execute("UPDATE tasks SET created_at = '2026-01-01T00:00:00+00:00' WHERE id = ?", (out["task_id"],))
    assert service.tick(conn) == {"status_drafts": 1}
    row = conn.execute("SELECT * FROM support_threads WHERE issue_task_id = ?", (out["task_id"],)).fetchone()
    assert row["draft_mode"] == "status"
    assert service.tick(conn) == {}  # once
    # The fix lands later: a second draft task (the real answer).
    dev = Ctx(actors.find_by_name(conn, "Kniha Developer")["id"], via="mcp")
    tasks.complete(conn, dev, out["task_id"], "Opraveno, commit def5678.")
    row = conn.execute("SELECT * FROM support_threads WHERE issue_task_id = ?", (out["task_id"],)).fetchone()
    assert row["draft_mode"] == "fixed"


def test_signature_by_mailbox_and_language():
    assert service.signature(OBSEUM, "cs", None).endswith("Obseum s.r.o.\ndavid.rosko@obseum.cz")
    assert service.signature(PERSONAL, "en", None) == "Best regards,\nDavid Roško"
    assert service._with_signature("Díky.\n\nDavid Roško", "S pozdravem\nDavid Roško") == "Díky.\n\nDavid Roško"


def test_rich_project_page_links_match(conn):
    from pos import project_info

    p = projects.create(conn, owner_ctx(conn), name="Zákaznický portál", goal="Portál pro klienty", channel=False)
    project_info.update_details(conn, owner_ctx(conn), p["id"], {
        "links": {"repos": ["https://github.com/ObseumEU/client-portal"], "website": "https://portal.klient.cz"},
        "keywords": ["mluvii report"]})
    by_site = match.match(conn, mail("Chyba", "Eva <eva@klient.cz>", "Na https://portal.klient.cz/login je chyba."))
    assert by_site["id"] == p["id"]
    by_repo = match.match(conn, mail("Bug", "x@y.io", "client-portal crashes on start"))
    assert by_repo["id"] == p["id"]
    assert match.match(conn, mail("Report", "x@y.io", "Mluvii report vrací 500"))["id"] == p["id"]
