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
    pm = actors.find_by_name(c, "Project manager")["id"]
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
    assert out["status"] == "review" and tasks.get(c, me, t["id"])["reviewer_name"] == "Project manager"
    assert chat.inbox_unread(c, pm.actor_id) >= 1  # the PM heard of it
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
