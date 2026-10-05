"""The owner's report never contradicts itself (pos.owner_report): when the text asks the owner to act,
"Co od tebe potřebuju" lists it with a link; a takeaway states the result against the target, not the
delivery time; internal titles and truncated English tails are not shown. Fixtures are modeled on
T-737, T-693 and T-751 of 5. 10. 2026."""

import json

from pos import actors, approval_view, owner_report, tasks
from pos.core import Ctx
from pos.db import connect, migrate


def _conn(tmp_path):
    c = connect(tmp_path / "a.db")
    migrate(c)
    actors.ensure_builtin(c)
    c.commit()
    return c


def _task(conn, owner, title, *, source="manual", status="done", progress="", notes=""):
    t = tasks.create(conn, owner, {"title": title, "assignee": {"type": "human", "id": owner.actor_id},
                                   "status": "next", "notes": notes})
    conn.execute("UPDATE tasks SET status = ?, progress_note = ?, source = ? WHERE id = ?",
                 (status, progress, source, t["id"]))
    conn.commit()
    return tasks.get(conn, owner, t["id"])


def _report(conn, owner, task, rep, source="builder"):
    full = owner_report.normalize(rep)
    owner_report._store(conn, task["id"], 0 if source == "agent" else owner.actor_id, source,
                        owner_report.fingerprint(task), full, {"takeaway_source": "llm"}, None, None)
    conn.commit()
    return owner_report.current(conn, owner, task["id"], generate=False)


# T-737: the owner chose "Provedu to dnes mimo špičku"; the report said "Nic, jen pro informaci" while
# its next step was "Provedeš posun kódu na svr03".
T737_REPORT = {
    "takeaway": "Rozhodl jsi se posunout kopii kódu agentů na serveru na aktuální verzi dnes mimo špičku.",
    "summary": ["Agenti pracují ze staré kopie kódu, proto deployer odmítá jejich změny.",
                "Na server nemá přístup žádný agent, proto krok musí provést Owner."],
    "decisions": [], "next": "Provedeš posun kódu na svr03 dnes mimo špičku, poté SRE ověří stav.",
    "content": "Provedu to dnes mimo špičku — I'm", "changes": [], "verification": [], "sources": [],
}

# T-693: approved, but "chybí jen kód povelu pro HDO Statenice, až budeš mít chvilku".
T693_REPORT = {
    "takeaway": "Schválil jsi cíle na říjen i oba klíče pro tým. Chybí jen kód povelu pro HDO Statenice, "
                "až budeš mít chvilku.",
    "summary": ["Schválil jsi oba klíče: github a knowlage-admin."], "decisions": [],
    "next": "Klíče github a knowlage-admin se založí v trezoru.", "content": "Schválit vše (cíle + oba klíče)",
}

# T-751: the agent's takeaway was only the delivery time; the content has 37 % against a ≥ 50 % target.
T751_REPORT = {
    "takeaway": "Denní přehled za 5. 10. jsi dostal ve 14:02, tedy před termínem 17:00.",
    "decisions": [], "next": "Zítra porovnám čísla s dneškem.",
    "content": ("**Denní přehled 5. 10.**\n\n| Ukazatel | Hodnota |\n|---|---|\n"
                "| Podíl byznysu na nákladech | 37 % (cíl ≥ 50 %) |\n| Zprávy ven | 1 |\n| Oslovené kontakty | 0 |\n"
                "| Položky ke schválení ve firmě | 72 |\n| Koncepty e-mailů v Gmailu u tebe | 12 |\n\n"
                "**Opatření:** vedoucí projdou fronty schvalování."),
    "verification": ["V našem chatu je zpráva ze 14:02 s přiloženým přehledem."],
}


def test_t737_owner_action_is_listed_not_nic(tmp_path):
    conn = _conn(tmp_path)
    owner = Ctx(actors.owner_id(conn))
    t = _task(conn, owner, "Posunout kopii kódu agentů na svr03 na aktuální verzi (jednorázově)",
              source="ask_owner", progress="Provedu to dnes mimo špičku — I'm")
    view = _report(conn, owner, t, T737_REPORT)
    assert view["decisions"] == []
    texts = " ".join(a["text"] for a in view["actions"])
    assert view["actions"], "the body asks the owner to act: the section must not say 'Nic'"
    assert "Provedeš posun kódu" in texts or "Slíbil jsi" in texts
    assert all(a["href"] for a in view["actions"])
    # The truncated English tail is gone from what he reads.
    assert view["details"]["content"] == "Provedu to dnes mimo špičku"
    assert "I'm" not in view["details"]["original"]
    assert view["details"]["content_title"] == "Tvoje odpověď"


def test_t693_until_you_have_a_moment_is_an_action(tmp_path):
    conn = _conn(tmp_path)
    owner = Ctx(actors.owner_id(conn))
    t = _task(conn, owner, "Schval cíle firmy na říjen a dva klíče pro tým", source="ask_owner",
              progress="Schválit vše (cíle + oba klíče)")
    view = _report(conn, owner, t, T693_REPORT)
    assert any("HDO Statenice" in a["text"] for a in view["actions"])


def test_t751_takeaway_states_result_against_target_and_links_drafts(tmp_path):
    conn = _conn(tmp_path)
    owner = Ctx(actors.owner_id(conn))
    t = _task(conn, owner, "Slib Ownerovi: Denní přehled 5. 10.: byznys jen 37 % nákladů", source="promise:2046",
              progress="Slib splněn")
    view = _report(conn, owner, t, T751_REPORT, source="agent")
    tk = view["takeaway"]
    assert "37 %" in tk and "50 %" in tk and "pod cílem" in tk
    assert "72" in tk and "12" in tk
    assert "14:02" not in tk
    drafts = [a for a in view["actions"] if a["href"] == owner_report.GMAIL_DRAFTS]
    assert drafts and "12" in drafts[0]["text"]
    assert view["title"].startswith("Slib: ") and "Ownerovi" not in view["title"]


def test_lint_refuses_a_delivery_only_takeaway():
    rep = owner_report.normalize(T751_REPORT)
    assert any("against the target" in p for p in owner_report.lint(rep))
    ok = owner_report.normalize({**T751_REPORT, "takeaway": "Byznys pokrývá 37 % nákladů proti cíli 50 %."})
    assert not any("against the target" in p for p in owner_report.lint(ok))


def test_nothing_to_do_stays_nic(tmp_path):
    conn = _conn(tmp_path)
    owner = Ctx(actors.owner_id(conn))
    t = _task(conn, owner, "Záloha ověřena", progress="Hotovo")
    view = _report(conn, owner, t, {"takeaway": "Záloha proběhla a obnova z ní funguje.",
                                    "content": "Obnova testovací databáze trvala 3 minuty, data sedí."})
    assert view["actions"] == []


def test_open_ask_and_pending_approval_of_the_task_are_actions(tmp_path):
    conn = _conn(tmp_path)
    owner = Ctx(actors.owner_id(conn))
    t = _task(conn, owner, "Odpověď Filipovi", progress="Koncept je připravený.")
    conn.execute("""INSERT INTO approvals (task_id, requested_by, action, details, created_at)
                    VALUES (?, ?, 'email.send', ?, '2026-10-05T10:00:00+00:00')""",
                 (t["id"], owner.actor_id, json.dumps({"payload": {"to": "filip@example.com", "subject": "Návrh",
                                                                   "body": "Ahoj"}, "kind": "commitment"})))
    conn.commit()
    view = _report(conn, owner, t, {"takeaway": "Koncept odpovědi je hotový.", "content": "Ahoj Filipe, …"})
    assert view["actions"][0]["kind"] == "approval"
    assert view["actions"][0]["href"].startswith("/approvals")
    assert "Návrh" in view["actions"][0]["text"]


# ------------------------------------------------------------------ approvals as the owner reads them

def test_email_approval_view_is_komu_predmet_text_and_says_draft(tmp_path):
    conn = _conn(tmp_path)
    details = {"payload": {"thread_id": "1a1", "account": "david@example.com", "body": "Ahoj Filipe,\n\nnávrh…"},
               "kind": "commitment",
               "reason": "marked as commitment: a contract, price quote or other legal or financial commitment",
               "why": "T-755: koncept odpovědi Filipovi."}
    v = approval_view.view(conn, "email.send", details)
    assert v["email"]["body"].startswith("Ahoj Filipe")
    assert v["email"]["to"] and v["email"]["subject"]
    assert v["approve_label"] == "Uložit jako koncept v Gmailu"
    assert v["kind_label"].startswith("Závazek")
    assert "marked" not in v["reason"] and v["reason"].startswith("Agent to označil jako závazek")


def test_email_approval_in_auto_mode_says_send(tmp_path):
    from pos import outbound_gmail

    conn = _conn(tmp_path)
    outbound_gmail.set_mode(conn, Ctx(actors.owner_id(conn)), "auto")
    v = approval_view.view(conn, "email.send", {"payload": {"to": "a@b.cz", "subject": "S", "body": "B"}})
    assert v["approve_label"] == "Odeslat"


def test_reasons_are_czech():
    assert approval_view.reason_cs("'smlouvu' reads as a contract or a binding offer").startswith("„smlouvu“ zní")
    assert approval_view.reason_cs("linkedin.post posts on the owner's personal channel") == \
        "Jde o příspěvek na tvém osobním profilu."
    assert approval_view.reason_cs("a price with an amount reads as a price quote").startswith("Cena")
    assert approval_view.action_label("linkedin.post") == "Příspěvek na LinkedIn"
