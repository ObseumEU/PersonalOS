from datetime import date, timedelta

import pytest

from pos import actors, capture, suggest, tasks, versioning
from pos.core import Ctx, Forbidden, today
from pos.db import connect, migrate


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("POS_CODEX_DISABLED", "1")
    c = connect(tmp_path / "t.db")
    migrate(c)
    actors.ensure_builtin(c)
    yield c
    c.close()


@pytest.fixture
def me(conn):
    return Ctx(actors.owner_id(conn))


@pytest.fixture
def ai(conn):
    return Ctx(actors.assistant_id(conn), via="mcp")


def test_capture_syntax():
    base = date(2026, 9, 25)  # a Friday
    p = capture.parse("Call the bank tomorrow 15m #Finance !high ~low", base)
    assert p == {"title": "Call the bank", "do_date": "2026-09-26", "estimate_min": 15,
                 "topic": "finance", "priority": 1, "energy": "low"}
    p = capture.parse("Renew Acme due 1.10. @Nexus +private 1h30m", base)
    assert p["deadline"] == "2026-10-01" and p["assignee"] == "Nexus"
    assert p["visibility"] == "private" and p["estimate_min"] == 90 and p["title"] == "Renew Acme"
    assert capture.parse("Zavolat po obědě pondělí", base)["do_date"] == "2026-09-28"
    assert capture.parse("Zavolat po obědě pondělí", base)["title"] == "Zavolat po obědě"


def test_capture_czech_deadline_word():
    base = date(2026, 9, 25)
    p = capture.parse("Zaplatit fakturu termín 1.10. #finance", base)
    assert p == {"title": "Zaplatit fakturu", "deadline": "2026-10-01", "topic": "finance"}
    assert capture.parse("Zaplatit fakturu termin 1.10.", base)["deadline"] == "2026-10-01"


def test_capture_routes_to_inbox_or_next(conn, me):
    assert tasks.capture(conn, me, "something vague")["status"] == "inbox"
    t = tasks.capture(conn, me, "Pay invoice today #finance")
    assert t["status"] == "next" and t["do_date"] == today().isoformat()
    assert [x["id"] for x in tasks.list_tasks(conn, me, "today")] == [t["id"]]


def test_assignee_types(conn, me):
    ai_task = tasks.create(conn, me, {"title": "Summarise lease", "assignee": "ai"})
    assert (ai_task["assignee_type"], ai_task["status"]) == ("ai", "next")
    agent = tasks.create(conn, me, {"title": "Compare prices", "assignee": "Knowledge agent"})
    assert agent["assignee_type"] == "agent"
    ext = tasks.create(conn, me, {"title": "VAT docs", "assignee": {"type": "external", "name": "Petr N."}})
    assert (ext["assignee_type"], ext["status"]) == ("external", "waiting") and ext["follow_up"]
    mine = tasks.create(conn, me, {"title": "Decide", "assignee": "me"})
    assert mine["assignee_type"] == "human"
    assert {t["id"] for t in tasks.list_tasks(conn, me, "agents")} == {ai_task["id"], agent["id"]}
    assert [t["id"] for t in tasks.list_tasks(conn, me, "waiting")] == [ext["id"]]


def test_agents_hand_in_for_review_and_owner_can_return(conn, me, ai):
    t = tasks.create(conn, me, {"title": "Summarise", "assignee": "ai"})
    t = tasks.claim(conn, ai, t["id"])
    assert t["status"] == "working"
    t = tasks.complete(conn, ai, t["id"], "summary attached")
    assert t["status"] == "review" and t["progress"] == 100
    with pytest.raises(Forbidden):
        tasks.review(conn, ai, t["id"], accept=True)
    t = tasks.review(conn, me, t["id"], accept=False, comment="too long")
    assert t["status"] == "next" and t["returned_count"] == 1
    t = tasks.intervene(conn, me, t["id"], "fixed the source")
    assert t["interventions"] == 2  # the return counted as one (pos.business), the intervene as another
    assert tasks.complete(conn, me, t["id"])["status"] == "done"


def test_private_layer(conn, me, ai):
    secret = tasks.create(conn, me, {"title": "Doctor", "visibility": "private"})
    team = tasks.create(conn, me, {"title": "Team work", "status": "next"})
    seen = {t["id"] for t in tasks.list_tasks(conn, ai, "inbox") + tasks.list_tasks(conn, ai, "next")}
    assert team["id"] in seen and secret["id"] not in seen
    with pytest.raises(Forbidden):
        tasks.get(conn, ai, secret["id"])
    from pos.visibility import share
    share(conn, "task", secret["id"], ai.actor_id)
    assert tasks.get(conn, ai, secret["id"])["title"] == "Doctor"


def test_history_restore_and_archive(conn, me):
    t = tasks.create(conn, me, {"title": "v1"})
    tasks.update(conn, me, t["id"], {"title": "v2", "priority": 1})
    hist = versioning.history(conn, "task", t["id"])
    assert [h["version"] for h in hist] == [1, 2] and hist[1]["data"]["title"] == "v2"
    versioning.restore(conn, me, "task", t["id"], 1)
    assert tasks.get(conn, me, t["id"])["title"] == "v1"
    tasks.archive(conn, me, t["id"])
    assert tasks.list_tasks(conn, me, "inbox") == []
    tasks.unarchive(conn, me, t["id"])
    assert len(tasks.list_tasks(conn, me, "inbox")) == 1


def test_archived_view_lists_only_archived_tasks_newest_first(conn, me, ai):
    live = tasks.create(conn, me, {"title": "Live"})
    a = tasks.create(conn, me, {"title": "Old"})
    b = tasks.create(conn, me, {"title": "Newer"})
    secret = tasks.create(conn, me, {"title": "Doctor", "visibility": "private"})
    for t in (a, b, secret):
        tasks.archive(conn, me, t["id"])
    conn.execute("UPDATE tasks SET archived_at = '2026-01-01T00:00:00Z' WHERE id = ?", (a["id"],))
    ids = [t["id"] for t in tasks.list_tasks(conn, me, "archived")]
    assert ids[:2] in ([secret["id"], b["id"]], [b["id"], secret["id"]]) and ids[-1] == a["id"]
    assert live["id"] not in ids
    assert secret["id"] not in {t["id"] for t in tasks.list_tasks(conn, ai, "archived")}  # privacy still holds
    tasks.unarchive(conn, me, a["id"])
    assert a["id"] not in {t["id"] for t in tasks.list_tasks(conn, me, "archived")}


def test_rollback_a_run(conn, me):
    from pos import agents

    agents.seed_builtin_permissions(conn)  # the assistant has tasks:write, so it may change others' tasks
    keep = tasks.create(conn, me, {"title": "Keep me", "priority": 2})
    run_id = conn.execute(
        "INSERT INTO runs (actor_id, kind, status, started_at) VALUES (?, 'task', 'ok', '2026-01-01')",
        (actors.assistant_id(conn),),
    ).lastrowid
    bad = Ctx(actors.assistant_id(conn), via="runner", run_id=run_id)
    tasks.update(conn, bad, keep["id"], {"title": "Vandalised", "priority": 3})
    made = tasks.create(conn, bad, {"title": "Spam"})
    undone = versioning.rollback_run(conn, me, run_id)
    assert set(undone) == {("task", keep["id"]), ("task", made["id"])}
    after = tasks.get(conn, me, keep["id"])
    assert (after["title"], after["priority"]) == ("Keep me", 2)
    assert conn.execute("SELECT archived_at FROM tasks WHERE id = ?", (made["id"],)).fetchone()[0]


def test_clarify_accept_creates_steps(conn, me):
    t = tasks.capture(conn, me, "acme renewal?? check contract + tell them")
    tasks.set_suggestion(conn, me, t["id"], {
        "title": "Renew or cancel the Acme agreement", "topic": "acme", "priority": 1,
        "deadline": (today() + timedelta(days=6)).isoformat(), "assignee": "me",
        "steps": [
            {"title": "Extract renewal terms", "assignee": "ai", "reason": "reading", "estimate_min": 2},
            {"title": "Compare pricing", "assignee": "Knowledge agent", "reason": "research", "estimate_min": None},
            {"title": "Decide", "assignee": "me", "reason": "decision", "estimate_min": 15},
        ],
    })
    done = tasks.clarify(conn, me, t["id"], "accept", {"do_date": today().isoformat()})
    assert done["title"] == "Renew or cancel the Acme agreement" and done["status"] == "next"
    assert [s["assignee_type"] for s in done["steps"]] == ["ai", "agent", "human"]
    assert done["suggestion"] is None
    assert tasks.list_tasks(conn, me, "today")[0]["steps_total"] == 3


def test_clarify_reference_becomes_note(conn, me):
    t = tasks.capture(conn, me, "Wifi password is on the router")
    tasks.clarify(conn, me, t["id"], "reference")
    assert conn.execute("SELECT title FROM notes").fetchone()[0] == "Wifi password is on the router"
    assert tasks.list_tasks(conn, me, "inbox") == []


def test_rule_based_suggestion_without_codex(conn, me):
    t = tasks.capture(conn, me, "Summarise the insurance policy")
    s = suggest.suggest(conn, me, t["id"])
    assert s["engine"] == "rules" and s["assignee"] == "ai"
    assert tasks.get(conn, me, t["id"])["suggestion"]["assignee"] == "ai"


def test_views_and_counts(conn, me):
    tasks.capture(conn, me, "later someday")
    tasks.create(conn, me, {"title": "Next thing", "status": "next"})
    tasks.create(conn, me, {"title": "Future", "status": "next",
                            "do_date": (today() + timedelta(days=3)).isoformat()})
    tasks.create(conn, me, {"title": "Maybe", "status": "someday"})
    c = tasks.counts(conn, me)
    assert (c["inbox"], c["next"], c["upcoming"], c["someday"], c["today"]) == (1, 1, 1, 1, 0)


def test_nobody_gets_around_review(conn, me, ai):
    t = tasks.create(conn, me, {"title": "Draft the offer", "assignee": "ai", "status": "next"})
    tasks.claim(conn, ai, t["id"])
    # an agent setting done hands the work in instead
    assert tasks.update(conn, ai, t["id"], {"status": "done"})["status"] == "review"
    # the owner's checkbox (complete) on a task under review is an accept
    out = tasks.complete(conn, me, t["id"])
    assert out["status"] == "done" and out["completed_at"]
    assert versioning.history(conn, "task", t["id"])[-1]["action"] == "accept"


def test_restore_and_logs_respect_visibility(conn, me, ai):
    from pos import audit, runner
    from pos.core import Forbidden

    secret = tasks.create(conn, me, {"title": "Salary review", "visibility": "private"})
    # an agent may neither restore nor see a private task in the log or the run list
    with pytest.raises(Forbidden):
        versioning.restore(conn, ai, "task", secret["id"], 1)
    conn.execute("INSERT INTO runs (actor_id, task_id, kind, status, started_at) VALUES (?, ?, 'task', 'ok', "
                 "'2026-09-25T10:00:00+00:00')", (ai.actor_id, secret["id"]))
    assert all(e["entity_id"] != secret["id"] for e in audit.readable(conn, audit.entries(conn, entity="task"),
                                                                        ai.actor_id))
    assert audit.readable(conn, audit.entries(conn, entity="task"), me.actor_id)
    assert all(r["task_id"] != secret["id"] for r in runner.list_runs(conn, actor_id=ai.actor_id))
    assert any(r["task_id"] == secret["id"] for r in runner.list_runs(conn, actor_id=me.actor_id))
    # a team task restored by someone else keeps today's owner and visibility
    t = tasks.create(conn, me, {"title": "Plan", "visibility": "team"})
    tasks.update(conn, me, t["id"], {"visibility": "public", "title": "Plan v2"})
    out = versioning.restore(conn, ai, "task", t["id"], 1)
    assert out["title"] == "Plan" and out["visibility"] == "public" and out["owner_id"] == me.actor_id


def test_my_day_scope_steps_and_capacity(conn, me, ai):
    from datetime import date

    from pos import agenda

    t = tasks.create(conn, me, {"title": "Launch", "status": "next"})
    tasks.create(conn, me, {"title": "Book the room", "parent_id": t["ref"], "do_date": today().isoformat()})
    tasks.create(conn, me, {"title": "AI summary", "assignee": "ai", "do_date": today().isoformat()})
    todays = [x["title"] for x in tasks.list_tasks(conn, me, "today", scope="mine")]
    assert "Book the room" in todays and "AI summary" not in todays  # steps count, others' work does not
    assert "AI summary" in [x["title"] for x in tasks.list_tasks(conn, me, "today", scope="all")]
    day = date(2026, 9, 28)
    ev = [{"start": "2026-09-28T10:00:00+02:00", "end": "2026-09-28T11:00:00+02:00", "all_day": False},
          {"start": "2026-09-28T10:30:00+02:00", "end": "2026-09-28T12:00:00+02:00", "all_day": False},
          {"start": "2026-09-28T00:00:00+02:00", "end": "2026-09-29T00:00:00+02:00", "all_day": True}]
    cap = agenda.capacity(ev, day)
    assert cap == {"work_min": 480, "meetings_min": 120, "free_min": 360}



def test_who_may_change_a_task(conn, me, tmp_path):
    from pos import accounts
    from pos.core import Forbidden
    from pos.visibility import share_item

    inv = accounts.invite(conn, me, email="eva@firma.cz", name="Eva")
    eva = Ctx(accounts.accept(conn, inv["token"], "eva-password-1"))
    inv2 = accounts.invite(conn, me, email="petr@firma.cz", name="Petr")
    petr = Ctx(accounts.accept(conn, inv2["token"], "petr-password-1"))
    t = tasks.create(conn, eva, {"title": "Eva's plan", "status": "next"})
    with pytest.raises(Forbidden):  # Petr reads team work but does not change Eva's
        tasks.update(conn, petr, t["id"], {"title": "Petr's plan"})
    tasks.create(conn, petr, {"title": "comment instead"})
    assert tasks.update(conn, me, t["id"], {"priority": 1})["priority"] == 1  # the company owner may
    # a private item shared with Petr: he reads it
    secret = tasks.create(conn, eva, {"title": "Salary", "visibility": "private"})
    with pytest.raises(Forbidden):
        tasks.get(conn, petr, secret["id"])
    share_item(conn, eva, "task", secret["id"], "Petr")
    assert tasks.get(conn, petr, secret["id"])["title"] == "Salary"
    with pytest.raises(Forbidden):
        share_item(conn, petr, "task", secret["id"], "Executive Assistant")  # only its owner shares it
