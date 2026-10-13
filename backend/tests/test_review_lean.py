"""The lean review policy (POS_REVIEW_MODE=lean, the default since the 2026-10 cut): a result is reviewed
only where a person must really look; everything else (code and the owner's own requests too) is accepted
at hand-in, and the hourly review jobs spend no review run on the backlog the lean rules accept."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from pos import approvals, business, review_policy, review_work, tasks
from pos.core import Ctx
from test_agent_effectiveness import _task, env  # noqa: F401 - fixture


@pytest.fixture
def lean(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("POS_REVIEW_MODE", "lean")
    return env


def _reason(conn, task_id) -> str:
    row = conn.execute("SELECT detail FROM audit_log WHERE action = 'review_auto_accept' AND entity_id = ? "
                       "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    return json.loads(row["detail"])["reason"] if row else ""


def test_the_mode_is_lean_by_default_and_classic_on_request(monkeypatch):
    monkeypatch.delenv("POS_REVIEW_MODE", raising=False)
    assert review_policy.mode() == "lean"
    monkeypatch.setenv("POS_REVIEW_MODE", "classic")
    assert review_policy.mode() == "classic"
    monkeypatch.setenv("POS_REVIEW_MODE", "whatever")
    assert review_policy.mode() == "lean"


def test_ordinary_results_code_and_plans_are_accepted_at_hand_in(lean):
    conn, ids = lean["conn"], lean["ids"]
    ceo, w, se = Ctx(ids["CEO"]), Ctx(ids["Writer"], via="mcp"), Ctx(ids["Software Engineer"], via="mcp")
    code = _task(conn, ceo, ids["Software Engineer"], title="Opravit parser", topic="engineering")
    out = tasks.complete(conn, se, code["id"], "Commit abc1234 na agent/dev.\nOvěřeno: pytest 12 passed")
    assert out["status"] == "done"  # no QA review: the deployer's gates and the tests check code
    assert _reason(conn, code["id"]).startswith("lean review policy: code")
    plan = _task(conn, ceo, ids["Writer"], title="Plán obchodu na říjen", topic="plan")
    assert tasks.complete(conn, w, plan["id"], "Plán je v poznámce #3.")["status"] == "done"
    small = _task(conn, ceo, ids["Writer"], title="Upravit text na webu")
    assert tasks.complete(conn, w, small["id"], "Upraveno, text je v poznámce #7.")["status"] == "done"
    assert _reason(conn, small["id"]) == "lean review policy: nothing a person must look at"
    # no "Review: T-x" item anywhere, nobody's review run
    assert not conn.execute("SELECT 1 FROM tasks WHERE source LIKE 'review:%'").fetchone()


def test_the_owners_own_request_is_done_and_he_is_told(lean):
    conn, owner, ids = lean["conn"], lean["owner"], lean["ids"]
    mine = _task(conn, owner, ids["Writer"], title="Souhrn pro mě", topic="digest")
    out = tasks.complete(conn, Ctx(ids["Writer"], via="mcp"), mine["id"], "Hotovo.\nOvěřeno: přečteno")
    assert out["status"] == "done" and out["reviewer_id"] != ids["CEO"]  # no CEO stand-in review
    assert not conn.execute("SELECT 1 FROM audit_log WHERE action = 'review_triage' AND entity_id = ?",
                            (mine["id"],)).fetchone()
    assert "owner's own request" in _reason(conn, mine["id"])
    told = conn.execute("""SELECT m.body FROM chat_messages m JOIN chat_inbox i ON i.message_id = m.id
                           WHERE i.actor_id = ? AND m.body LIKE 'Hotovo: %'""", (owner.actor_id,)).fetchall()
    assert len(told) == 1 and mine["ref"] in told[0]["body"]
    # he can reopen it; handed in again it waits for him (returned before by the owner), not for a stand-in
    tasks.update(conn, owner, mine["id"], {"status": "next"})
    again = tasks.complete(conn, Ctx(ids["Writer"], via="mcp"), mine["id"], "Opraveno podle komentáře.")
    assert again["status"] == "review" and again["reviewer_id"] == owner.actor_id


def test_what_a_person_must_see_still_waits_for_review(lean):
    conn, owner, ids = lean["conn"], lean["owner"], lean["ids"]
    ceo, w = Ctx(ids["CEO"]), Ctx(ids["Writer"], via="mcp")
    reply = _task(conn, ceo, ids["Writer"], title="Odpověď zákazníkovi", source="support:draft")
    money = _task(conn, ceo, ids["Writer"], title="Poslat fakturu Acme")
    legal = _task(conn, ceo, ids["Writer"], title="Projít podmínky", topic="legal")
    sent = _task(conn, ceo, ids["Writer"], title="Oslovit partnera")
    approvals.request(conn, Ctx(ids["Writer"], via="mcp"), "send_email", {"to": "x@example.com"},
                      task_id=sent["id"], ping=False)
    for t, why in ((reply, "a customer reply"), (money, "money or a commitment"), (legal, "topic legal"),
                   (sent, "sent outside and still waiting for an approval")):
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (t["id"],)).fetchone()
        d = review_policy.decide(conn, row, "Hotovo.")
        assert d.action != "accept" and d.reason == f"lean review policy: {why}", (t["title"], d)
        assert tasks.complete(conn, w, t["id"], "Hotovo.")["status"] == "review"
    # returned by an agent (the CEO): handed in again it is accepted; returned by the owner it waits for him
    by_ceo = _task(conn, ceo, ids["Writer"], title="Text do newsletteru")
    tasks.request_review(conn, w, by_ceo["id"], "CEO", "hotovo")  # an explicit request_review is respected
    tasks.review(conn, ceo, by_ceo["id"], False, "kratší")
    assert tasks.complete(conn, w, by_ceo["id"], "Zkráceno.")["status"] == "done"
    by_owner = _task(conn, ceo, ids["Writer"], title="Text na web")
    tasks.request_review(conn, w, by_owner["id"], "CEO", "hotovo")
    tasks.review(conn, owner, by_owner["id"], False, "jinak")
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (by_owner["id"],)).fetchone()
    assert review_policy.decide(conn, row, "Přepsáno.").reason == "lean review policy: returned before by the owner"


def test_the_backlog_waits_for_the_apply_and_the_hourly_jobs_spawn_no_review_runs(lean, capsys):
    conn, owner, ids = lean["conn"], lean["owner"], lean["ids"]
    ceo, qa = ids["CEO"], ids["QA Reviewer"]
    old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat(timespec="seconds")

    def waiting(title, assignee, reviewer, note, by=None, **kw):
        t = tasks.create(conn, by or Ctx(ceo), {"title": title, "assignee": {"type": "agent", "id": assignee}, **kw})
        conn.execute("UPDATE tasks SET status = 'review', reviewer_id = ?, progress_note = ?, updated_at = ? "
                     "WHERE id = ?", (reviewer, note, old, t["id"]))
        return t["id"]

    code = waiting("Fix workeru", ids["Software Engineer"], qa, "commit 1234abcd, Ověřeno: 3 passed")
    doc = waiting("Přehled konkurence", ids["Writer"], ceo, "Je v poznámce #4.")
    risky = waiting("Smlouva s Acme", ids["Writer"], qa, "Návrh smlouvy je v poznámce #5.")
    chosen = tasks.create(conn, owner, {"title": "Moje kontrola", "reviewer": "me",
                                        "assignee": {"type": "agent", "id": ids["Writer"]}})["id"]
    conn.execute("UPDATE tasks SET status = 'review', progress_note = 'Hotovo.', updated_at = ? WHERE id = ?",
                 (old, chosen))
    review_work.ensure(conn, doc)  # an item made before the cut
    conn.commit()
    business.review_sla(conn)  # the hourly job
    items = {r["source"]: r["status"] for r in conn.execute("SELECT source, status FROM tasks "
                                                            "WHERE source LIKE 'review:%'")}
    assert items.get(review_work.source_of(doc)) == "done"  # its open item closed: no review run
    assert review_work.source_of(code) not in items  # none made for what the lean policy accepts
    assert items.get(review_work.source_of(risky)) == "next"  # a contract still gets its reviewer
    assert conn.execute("SELECT reviewer_id FROM tasks WHERE id = ?", (code,)).fetchone()[0] == qa  # not moved
    # the review he chose stays his: no CEO stand-in takes it over after 24 h
    assert conn.execute("SELECT reviewer_id FROM tasks WHERE id = ?", (chosen,)).fetchone()[0] == owner.actor_id
    # the backlog: a dry run first, then the lean accept
    review_policy.main(["--db", str(lean["db"])])
    dry = json.loads(capsys.readouterr().out)
    assert dry["mode"] == "lean" and dry["applied"] is False
    assert {x.split(" ", 1)[0] for x in dry["auto_accepted"]} == {tasks.display_id(code), tasks.display_id(doc)}
    assert conn.execute("SELECT status FROM tasks WHERE id = ?", (code,)).fetchone()[0] == "review"
    review_policy.sweep(conn, apply=True)
    status = {i: conn.execute("SELECT status FROM tasks WHERE id = ?", (i,)).fetchone()[0]
              for i in (code, doc, risky, chosen)}
    assert status == {code: "done", doc: "done", risky: "review", chosen: "review"}
    assert _reason(conn, doc).startswith("lean review policy:")
