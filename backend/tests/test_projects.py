"""Projects as shared work (REVIZE-FUNKCI 3.6)."""

import pytest
from fastapi.testclient import TestClient

from pos import actors, agents, chat, org, projects, tasks
from pos.config import Settings
from pos.core import Ctx, Forbidden
from pos.db import connect, migrate
from pos.main import create_app


@pytest.fixture
def co(tmp_path, monkeypatch):
    monkeypatch.setenv("POS_CODEX_DISABLED", "1")
    c = connect(tmp_path / "p.db")
    migrate(c)
    actors.ensure_builtin(c)
    org.ensure(c)
    agents.seed_builtin_permissions(c)
    me = Ctx(actors.owner_id(c))
    ids = {n: agents.create_agent(c, me, name=n, purpose=n, lifetime="long_lived",
                                  permissions=["tasks:read", "tasks:claim", "tasks:review", "messages:send"],
                                  data_dir=tmp_path)["agent"]["id"] for n in ("Writer", "Designer")}
    yield c, me, ids
    c.close()


def test_a_project_has_a_lead_members_a_channel_and_its_lead_reviews(co):
    c, me, ids = co
    p = projects.create(c, me, name="Podzimní kampaň", goal="Three posts and a newsletter",
                        definition_of_done="All four published", lead="Designer", member_refs=["Writer"],
                        labels=["#Marketing"])
    assert p["slug"] == "podzimni-kampan" and p["lead_name"] == "Designer" and p["labels"] == ["marketing"]
    assert {m["name"]: m["role"] for m in p["members"]} == {"Designer": "lead", "Writer": "member"}
    assert p["channel_id"] and chat.channel_view(c, p["channel_id"], me.actor_id)["name"] == "podzimni-kampan"
    t = tasks.create(c, me, {"title": "Post 1", "assignee": {"type": "agent", "id": ids["Writer"]},
                             "project": p["slug"]})
    assert tasks.get(c, me, t["id"])["reviewer_name"] == "Designer"  # the project lead reviews by default
    tasks.claim(c, Ctx(ids["Writer"]), t["id"])
    tasks.complete(c, Ctx(ids["Writer"]), t["id"])
    assert tasks.review(c, Ctx(ids["Designer"]), t["id"], True)["status"] == "done"
    got = projects.get(c, me, p["slug"])
    assert got["counts"]["done"] == 1 and got["tasks"][0]["ref"] == t["ref"]
    with pytest.raises(Forbidden):
        projects.add_member(c, Ctx(ids["Writer"]), p["slug"], "Executive Assistant")
    assert len(projects.add_member(c, Ctx(ids["Designer"]), p["slug"], "Executive Assistant")["members"]) == 3


def test_tasks_with_steps_become_projects_once(co):
    c, me, ids = co
    root = tasks.create(c, me, {"title": "Website relaunch", "status": "next"})
    tasks.create(c, me, {"title": "New homepage", "parent_id": root["ref"], "assignee": {"type": "agent", "id": ids["Designer"]}})
    assert projects.migrate_step_projects(c) == 1
    p = projects.list_projects(c, me)[0]
    assert p["name"] == "Website relaunch" and p["counts"]["queued"] == 2
    assert any(m["name"] == "Designer" for m in p["members"])
    assert projects.migrate_step_projects(c) == 0  # once
    step = tasks.create(c, me, {"title": "Footer", "parent_id": root["ref"]})
    assert tasks.get(c, me, step["id"])["project_id"] == p["id"]  # new steps join the project


def test_project_api(tmp_path, monkeypatch):
    monkeypatch.setenv("POS_CODEX_DISABLED", "1")
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        p = client.post("/api/projects", json={"name": "Q4 report", "goal": "numbers for the board"}).json()
        t = client.post("/api/tasks", json={"title": "Collect sales", "project": p["slug"]}).json()
        got = client.get(f"/api/projects/{p['slug']}").json()
        assert got["tasks"][0]["ref"] == t["ref"]
        assert client.patch(f"/api/projects/{p['slug']}", json={"status": "paused"}).json()["status"] == "paused"
        assert client.get("/api/projects").json()[0]["status"] == "paused"
