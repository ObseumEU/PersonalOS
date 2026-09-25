import json

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp.client import Client

from pos import actors, agents, approvals, integrations, killswitch, mcp_server, runner, tasks
from pos.config import Settings
from pos.core import Ctx, Forbidden
from pos.db import connect, migrate
from pos.guard.rules import ConstitutionViolation
from pos.main import create_app


@pytest.fixture(autouse=True)
def no_codex(monkeypatch):
    monkeypatch.setenv("POS_CODEX_DISABLED", "1")


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "a.db")
    migrate(c)
    actors.ensure_builtin(c)
    integrations.register_builtin_agents(c)
    integrations.install()
    yield c
    c.close()


@pytest.fixture
def me(conn):
    return Ctx(actors.owner_id(conn))


@pytest.fixture
def assistant(conn):
    return Ctx(actors.assistant_id(conn), via="mcp")


def make(conn, ctx, tmp_path, name, **kw):
    return agents.create_agent(conn, ctx, name=name, purpose=f"{name} work", data_dir=tmp_path, **kw)


def test_owner_creates_agent_with_key_and_profile(conn, me, tmp_path):
    out = make(conn, me, tmp_path, "Mail agent", lifetime="long_lived", permissions=["tasks:read", "tasks:claim"])
    assert out["created"] and out["api_key"].startswith("pos_")
    a = out["agent"]
    assert (a["kind"], a["lifetime"], a["purpose"]) == ("agent", "long_lived", "Mail agent work")
    assert a["permissions"] == ["tasks:claim", "tasks:read"]
    assert actors.actor_for_key(conn, out["api_key"]) == a["id"]
    assert "Mail agent work" in a["instructions"]


def test_agent_cannot_grant_more_than_it_has(conn, me, tmp_path):
    knowledge = Ctx(actors.find_by_name(conn, "Knowledge agent")["id"], via="mcp")
    with pytest.raises(Forbidden):  # it may not create agents at all
        make(conn, knowledge, tmp_path, "Child", permissions=["tasks:read"])
    agents.set_permissions(conn, me, knowledge.actor_id, ["tasks:read", "tasks:claim", "agents:create"])
    with pytest.raises(ConstitutionViolation):  # U5: tasks:write is more than it has
        make(conn, knowledge, tmp_path, "Child", permissions=["tasks:read", "tasks:write"])
    assert make(conn, knowledge, tmp_path, "Child", permissions=["tasks:read"])["created"]


def test_agent_can_create_within_its_permissions(conn, assistant, tmp_path):
    out = make(conn, assistant, tmp_path, "Helper", permissions=["tasks:read"])
    assert out["created"] and out["agent"]["created_by"] == assistant.actor_id


def test_daily_limit_goes_to_hr(conn, assistant, tmp_path):
    make(conn, assistant, tmp_path, "One", permissions=["tasks:read"])
    make(conn, assistant, tmp_path, "Two", permissions=["tasks:read"])
    third = make(conn, assistant, tmp_path, "Three", permissions=["tasks:read"])
    assert third["created"] is False and third["limit"] == "daily_limit"


def test_permission_changes_are_owner_only(conn, me, assistant, tmp_path):
    aid = make(conn, me, tmp_path, "Worker")["agent"]["id"]
    with pytest.raises(ConstitutionViolation):
        agents.set_permissions(conn, assistant, aid, ["tasks:read", "tasks:write"])
    assert agents.set_permissions(conn, me, aid, ["tasks:read", "tasks:write"])["permissions"] == ["tasks:read", "tasks:write"]


def test_kill_switch(conn, me, assistant):
    t = tasks.create(conn, me, {"title": "Work", "assignee": "ai"})
    frozen = killswitch.freeze(conn, me, "test")
    assert frozen["frozen"]
    with pytest.raises(Forbidden):
        tasks.claim(conn, assistant, t["id"])
    res = runner.run(conn, runner.RunRequest(assistant.actor_id, "task", "hi"))
    assert res.status == "blocked"
    with pytest.raises(Forbidden):
        killswitch.freeze(conn, assistant)
    with pytest.raises(ConstitutionViolation):
        killswitch.unfreeze(conn, assistant)
    assert killswitch.unfreeze(conn, me)["frozen"] is False
    assert tasks.claim(conn, assistant, t["id"])["status"] == "working"


def test_freeze_stops_running_runs(conn, me, assistant):
    rid = conn.execute("INSERT INTO runs (actor_id, kind, status, started_at) VALUES (?, 'task', 'running', 'x')",
                       (assistant.actor_id,)).lastrowid
    out = killswitch.freeze(conn, me, "stop")
    assert out["stopped_runs"] == [rid]
    assert conn.execute("SELECT status FROM runs WHERE id = ?", (rid,)).fetchone()[0] == "cancelled"


def test_pause_blocks_the_agent(conn, me, assistant):
    agents.pause(conn, me, assistant.actor_id, True)
    assert runner.run(conn, runner.RunRequest(assistant.actor_id, "task", "hi")).status == "blocked"
    assert agents.detail(conn, assistant.actor_id)["status"] == "paused"
    agents.pause(conn, me, assistant.actor_id, False)


def test_one_shot_agent_retires_after_its_work(conn, me, tmp_path):
    aid = make(conn, me, tmp_path, "Once", lifetime="one_shot", permissions=["tasks:read", "tasks:claim"])["agent"]["id"]
    t = tasks.create(conn, me, {"title": "Do it once", "assignee": {"type": "agent", "id": aid}})
    tasks.claim(conn, Ctx(aid), t["id"])
    tasks.complete(conn, Ctx(aid), t["id"])
    assert actors.get(conn, aid)["archived_at"] is None  # waits for the owner's review
    tasks.review(conn, me, t["id"], accept=True)
    assert actors.get(conn, aid)["archived_at"] is not None
    restored = agents.restore(conn, me, aid)
    assert restored["archived"] is False and restored["api_key"]


def test_messages_and_board(conn, me, assistant):
    agents.send_message(conn, me, assistant.actor_id, "Use the new template")
    assert [m["body"] for m in agents.take_messages(conn, assistant.actor_id)] == ["Use the new template"]
    assert agents.take_messages(conn, assistant.actor_id) == []
    t = tasks.create(conn, me, {"title": "Summarise", "assignee": "ai"})
    tasks.claim(conn, assistant, t["id"])
    approvals.request(conn, assistant, "send_email", {"to": "x"}, t["id"])
    row = next(r for r in agents.board(conn) if r["actor"]["id"] == assistant.actor_id)
    assert [x["title"] for x in row["working"]] == ["Summarise"]
    assert row["needs_you"][0]["status"] == "approval"


def test_mcp_permissions_and_freeze(tmp_path):
    db = tmp_path / "m.db"
    c = connect(db)
    migrate(c)
    actors.ensure_builtin(c)
    integrations.register_builtin_agents(c)
    knowledge = actors.find_by_name(c, "Knowledge agent")["id"]
    owner = actors.owner_id(c)
    c.close()
    server = mcp_server.build(db, default_actor=lambda _c: knowledge)

    async def scenario():
        async with Client(server) as cl:
            denied = await cl.call_tool("create_task", {"title": "no write permission"})
            assert denied.is_error and "tasks:write" in denied.content[0].text
            ok = await cl.call_tool("list_tasks", {"view": "inbox"})
            assert not ok.is_error
            hb = await cl.call_tool("heartbeat", {})
            assert not hb.is_error

    anyio.run(scenario)
    c = connect(db)
    killswitch.freeze(c, Ctx(owner), "test")
    c.close()

    async def frozen():
        async with Client(server) as cl:
            r = await cl.call_tool("request_approval", {"action": "x"})
            assert r.is_error and "frozen" in r.content[0].text
            hb = await cl.call_tool("heartbeat", {})
            assert not hb.is_error

    anyio.run(frozen)


def test_http_api(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        r = client.post("/api/agents", json={"name": "Dev agent", "purpose": "GitHub issues to PRs",
                                             "permissions": ["tasks:read", "tasks:claim"]})
        assert r.status_code == 201 and r.json()["created"]
        aid = r.json()["agent"]["id"]
        names = [a["name"] for a in client.get("/api/agents").json()["agents"]]
        assert "Dev agent" in names and "Owner" in names
        assert client.get(f"/api/agents/{aid}").json()["name"] == "Dev agent"
        assert client.post(f"/api/agents/{aid}/pause").json()["paused"] is True
        assert client.post("/api/system/freeze", json={"reason": "t"}).json()["frozen"] is True
        assert client.get("/api/system/freeze").json()["frozen"] is True
        assert client.post("/api/system/unfreeze").json()["frozen"] is False
        assert isinstance(client.get("/api/board").json(), list)
        assert client.get("/api/approvals").json() == []
        assert client.post("/api/agents", json={"name": "Dev agent", "purpose": "dup"}).status_code == 422
