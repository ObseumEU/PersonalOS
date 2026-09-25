"""Hiring a colleague and probation (REVIZE-FUNKCI 3.5)."""

from datetime import datetime, timedelta, timezone

import pytest

from pos import actors, agents, chat, hiring, org, tasks
from pos.core import Ctx, Forbidden
from pos.db import connect, migrate


@pytest.fixture
def co(tmp_path, monkeypatch):
    monkeypatch.setenv("POS_CODEX_DISABLED", "1")
    c = connect(tmp_path / "h.db")
    migrate(c)
    actors.ensure_builtin(c)
    org.ensure(c)
    agents.seed_builtin_permissions(c)
    yield c, Ctx(actors.owner_id(c)), Ctx(org.pm_id(c), via="mcp"), tmp_path
    c.close()


def test_the_lead_decides_and_the_new_agent_starts_on_probation(co):
    c, me, pm, tmp = co
    asked = hiring.request(c, me, name="Researcher", purpose="market research for offers", role="analyst")
    assert asked["decider_id"] == pm.actor_id and asked["lead_name"] == "Project manager"
    assert chat.inbox_unread(c, pm.actor_id) >= 1
    with pytest.raises(Forbidden):  # only the decider (or the owner)
        hiring.decide(c, Ctx(actors.assistant_id(c)), asked["id"], True, data_dir=tmp)
    out = hiring.decide(c, pm, asked["id"], True, "welcome", data_dir=tmp)
    assert out["status"] == "approved" and out["api_key"]
    new = actors.find_by_name(c, "Researcher")
    assert new["reports_to"] == pm.actor_id and new["role"] == "analyst" and hiring.on_probation(c, new["id"])
    # on probation, the lead reviews all its work, even what the owner asked for
    t = tasks.create(c, me, {"title": "Competitor prices", "assignee": {"type": "agent", "id": new["id"]}})
    assert tasks.get(c, me, t["id"])["reviewer_name"] == "Project manager"


def test_more_permissions_than_the_requester_go_to_the_owner(co):
    c, me, pm, tmp = co
    asked = hiring.request(c, pm, name="Builder", purpose="builds tools", permissions=["tasks:read", "agents:create"])
    assert asked["decider_id"] == me.actor_id and "permissions" in asked["needs_owner"]
    with pytest.raises(Forbidden):
        hiring.decide(c, pm, asked["id"], True, data_dir=tmp)
    rejected = hiring.decide(c, me, asked["id"], False, "not now", data_dir=tmp)
    assert rejected["status"] == "rejected" and actors.find_by_name(c, "Builder") is None


def test_probation_end_gives_the_lead_a_decision(co):
    c, me, pm, tmp = co
    asked = hiring.request(c, me, name="Tester", purpose="tests releases")
    hiring.decide(c, me, asked["id"], True, data_dir=tmp)
    new = actors.find_by_name(c, "Tester")["id"]
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds")
    c.execute("UPDATE actors SET probation_until = ? WHERE id = ?", (past, new))
    out = hiring.probation_review(c)
    assert out["ended"] == 1
    t = tasks.get(c, me, tasks.parse_id(out["tasks"][0]))
    assert t["assignee_name"] == "Project manager" and "Tester" in t["title"]
    assert not hiring.on_probation(c, new) and hiring.probation_review(c)["ended"] == 0
