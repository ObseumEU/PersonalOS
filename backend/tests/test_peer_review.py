"""Review between colleagues (REVIZE-FUNKCI 3.2)."""

import pytest

from pos import actors, agents, chat, org, tasks
from pos.core import Ctx, Forbidden
from pos.db import connect, migrate


@pytest.fixture
def team(tmp_path, monkeypatch):
    monkeypatch.setenv("POS_CODEX_DISABLED", "1")
    c = connect(tmp_path / "r.db")
    migrate(c)
    actors.ensure_builtin(c)
    org.ensure(c)
    agents.seed_builtin_permissions(c)
    me = Ctx(actors.owner_id(c))
    pm = actors.find_by_name(c, "COO")["id"]
    made = {}
    for name in ("Writer", "Editor"):
        made[name] = agents.create_agent(c, me, name=name, purpose=name, lifetime="long_lived",
                                         permissions=["tasks:read", "tasks:claim"], data_dir=tmp_path)["agent"]["id"]
    yield c, me, Ctx(pm, via="mcp"), Ctx(made["Writer"], via="mcp"), Ctx(made["Editor"], via="mcp")
    c.close()


def test_whoever_asked_reviews_and_nobody_approves_their_own_work(team):
    c, me, pm, writer, editor = team
    t = tasks.create(c, pm, {"title": "Blog post", "assignee": {"type": "agent", "id": writer.actor_id}})
    tasks.claim(c, writer, t["id"])
    out = tasks.complete(c, writer, t["id"], "draft ready")
    assert out["status"] == "review" and tasks.get(c, me, t["id"])["reviewer_name"] == "COO"
    # the PM heard of it: a review work item in its queue (pos.review_work), which starts its run
    assert c.execute("SELECT assignee_id FROM tasks WHERE source = ? AND status = 'next'",
                     (f"review:{t['id']}",)).fetchone()[0] == pm.actor_id
    with pytest.raises(Forbidden):
        tasks.review(c, writer, t["id"], True)  # own work
    with pytest.raises(Forbidden):
        tasks.review(c, editor, t["id"], True)  # neither reviewer nor lead
    back = tasks.review(c, pm, t["id"], False, "shorter intro")
    assert back["status"] == "next" and back["returned_count"] == 1
    tasks.complete(c, writer, t["id"])
    assert tasks.review(c, pm, t["id"], True)["status"] == "done"


def test_request_review_and_the_to_review_view(team):
    c, me, pm, writer, editor = team
    agents.set_permissions(c, me, editor.actor_id, ["tasks:read", "tasks:claim", "tasks:review"])
    t = tasks.create(c, me, {"title": "Pricing table", "assignee": {"type": "agent", "id": writer.actor_id}})
    tasks.claim(c, writer, t["id"])
    with pytest.raises(tasks.Invalid):
        tasks.request_review(c, writer, t["id"], writer.actor_id)
    tasks.request_review(c, writer, t["id"], "Editor", "please check the numbers")
    assert [x["ref"] for x in tasks.list_tasks(c, editor, "to_review")] == [t["ref"]]
    assert tasks.get(c, editor, t["id"])["can_review"] is True
    assert tasks.review(c, editor, t["id"], True)["status"] == "done"


def test_a_lead_reviews_and_an_agent_never_reviews_its_own_handoff(team):
    c, me, pm, writer, editor = team
    assert org.manages(c, pm.actor_id, writer.actor_id)  # new agents report to the PM
    t = tasks.create(c, me, {"title": "Newsletter", "assignee": {"type": "agent", "id": pm.actor_id}})
    org.handoff(c, pm, t["id"], writer.actor_id, "you write it")
    tasks.claim(c, writer, t["id"])
    tasks.complete(c, writer, t["id"])
    ok, why = tasks.may_review(c, pm, c.execute("SELECT * FROM tasks WHERE id = ?", (t["id"],)).fetchone())
    assert not ok and "handed" in why
    assert tasks.review(c, me, t["id"], True)["status"] == "done"  # the owner always may


def test_a_lead_has_authority_over_its_reports_only(team):
    from pos import schedules

    c, me, pm, writer, editor = team
    # Writer and Editor report to the PM; the PM leads, Editor does not lead Writer
    agents.pause(c, pm, writer.actor_id, True)
    assert actors.get(c, writer.actor_id)["paused_at"]
    agents.pause(c, pm, writer.actor_id, False)
    with pytest.raises(Forbidden):
        agents.pause(c, editor, writer.actor_id, True)
    # stop in chat: from the lead yes, from a peer no
    agents.set_permissions(c, me, editor.actor_id, ["tasks:read", "tasks:claim", "messages:send"])
    chat.send_dm(c, pm, writer.actor_id, "stop, wrong brief", priority="stop")
    with pytest.raises(Forbidden):
        chat.send_dm(c, editor, writer.actor_id, "stop!", priority="stop")
    # the lead hands its report's task on (tasks:claim is enough)
    t = tasks.create(c, me, {"title": "Case study", "assignee": {"type": "agent", "id": writer.actor_id}})
    org.handoff(c, pm, t["id"], editor.actor_id, "Editor has the data")
    assert tasks.get(c, me, t["id"])["assignee_id"] == editor.actor_id
    # moving reports within its own part of the chart, never outside it
    org.set_org(c, pm, editor.actor_id, {"reports_to": pm.actor_id, "role": "editor"})
    with pytest.raises(Forbidden):
        org.set_org(c, pm, editor.actor_id, {"reports_to": me.actor_id})
    with pytest.raises(Forbidden):
        org.set_org(c, editor, writer.actor_id, {"role": "boss"})
    # a lead edits its report's schedule
    s = schedules.create(c, me, {"name": "Weekly draft", "schedule": "weekly fri 09:00", "title": "Draft",
                                 "assignee": {"type": "agent", "id": writer.actor_id}, "visibility": "team"})
    schedules.update(c, pm, s["id"], {"schedule": "weekly mon 09:00"})
