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


def test_network(conn, me, assistant):
    from pos import network

    t = tasks.create(conn, me, {"title": "Summarise", "assignee": "ai"})
    agents.send_message(conn, me, assistant.actor_id, "hi")
    approvals.request(conn, assistant, "send_email", {}, t["id"])
    net = network.build(conn, "24h")
    kinds = {(e["from"], e["to"], e["type"]) for e in net["edges"]}
    assert (me.actor_id, assistant.actor_id, "assign") in kinds
    assert (me.actor_id, assistant.actor_id, "message") in kinds
    assert (assistant.actor_id, me.actor_id, "approval") in kinds
    node = next(n for n in net["nodes"] if n["id"] == assistant.actor_id)
    assert node["open"] == 2 and net["events"][0]["at"]  # the task and "Chat: answer Owner" (4.3)
    with pytest.raises(ValueError):
        network.build(conn, "1y")


def test_message_priorities_and_trust(conn, me, assistant, tmp_path):
    worker = make(conn, me, tmp_path, "Worker", permissions=["tasks:read", "tasks:claim", "messages:send"])["agent"]["id"]
    rid = conn.execute("INSERT INTO runs (actor_id, kind, status, started_at) VALUES (?, 'task', 'running', 'x')",
                       (worker,)).lastrowid
    sent = agents.send_message(conn, assistant, worker, "The client moved the call to 14:00", priority="change_plan")
    assert sent["delivered_to_run"] == rid
    agents.send_message(conn, me, worker, "Also check the invoice", priority="fyi")
    inbox = agents.check_inbox(conn, worker)
    assert [m["priority"] for m in inbox] == ["change_plan", "fyi"]
    assert inbox[0]["trust"] == "agent" and inbox[0]["body"].startswith("<external source=\"agent:Assistant\"")
    assert inbox[1]["trust"] == "member" and inbox[1]["body"] == "Also check the invoice"
    assert agents.ack_message(conn, Ctx(worker), inbox[0]["id"], "moved my plan")["acked"]
    st = agents.status(conn, worker)
    assert st["run"]["id"] == rid and st["unread_messages"] == 0

    from pos.core import Forbidden

    with pytest.raises(Forbidden):  # a stop comes from a person or the worker's lead, not a peer
        agents.send_message(conn, assistant, worker, "stop, wrong customer", priority="stop")
    agents.send_message(conn, me, worker, "stop, wrong customer", priority="stop")
    assert conn.execute("SELECT status FROM runs WHERE id = ?", (rid,)).fetchone()[0] == "cancelled"
    assert actors.get(conn, worker)["paused_at"] is not None
    assert actors.get(conn, worker)["archived_at"] is None
    with pytest.raises(agents.AgentError):
        agents.send_message(conn, me, worker, "x", priority="urgent")


def test_archive_stops_runs_and_hands_open_work_on_and_restore_gives_a_new_key(tmp_path, monkeypatch):
    from pos import actors, agents, tasks
    from pos.core import Ctx
    from pos.db import connect, migrate
    from pos.hr import service

    monkeypatch.setenv("POS_CODEX_DISABLED", "1")
    conn = connect(tmp_path / "a.db")
    migrate(conn)
    actors.ensure_builtin(conn)
    me = Ctx(actors.owner_id(conn))
    made = agents.create_agent(conn, me, name="Scout", purpose="research", lifetime="long_lived",
                               permissions=["tasks:read", "tasks:claim"], data_dir=tmp_path)
    scout = made["agent"]["id"]
    old_key = made["api_key"]
    t = tasks.create(conn, me, {"title": "Find suppliers", "assignee": "Scout", "status": "next"})
    run = conn.execute("INSERT INTO runs (actor_id, task_id, kind, status, started_at) VALUES (?, ?, 'task', "
                       "'running', '2026-09-25T10:00:00+00:00')", (scout, t["id"])).lastrowid
    agents.archive(conn, me, scout, "not needed")
    assert conn.execute("SELECT status FROM runs WHERE id = ?", (run,)).fetchone()["status"] == "cancelled"
    moved = tasks.get(conn, me, t["id"])
    assert moved["assignee_id"] == me.actor_id and "Scout was archived" in moved["progress_note"]
    assert actors.actor_for_key(conn, old_key) is None
    # HR restore is the same restore: a new key, the old one stays revoked
    new_key = service.restore(conn, me, scout)
    assert new_key and actors.actor_for_key(conn, new_key) == scout and actors.actor_for_key(conn, old_key) is None


def test_hr_replacement_is_archived_only_with_a_created_agent(tmp_path, monkeypatch):
    import pytest

    from pos import actors, agents
    from pos.core import Ctx
    from pos.db import connect, migrate
    from pos.hr import service as hr

    monkeypatch.setenv("POS_CODEX_DISABLED", "1")
    conn = connect(tmp_path / "r.db")
    migrate(conn)
    actors.ensure_builtin(conn)
    me = Ctx(actors.owner_id(conn))
    old = agents.create_agent(conn, me, name="Old", purpose="old work", lifetime="long_lived",
                              permissions=["tasks:read"], data_dir=tmp_path)["agent"]["id"]
    monkeypatch.setattr(hr, "admit_agent", lambda *a, **k: {"allowed": True, "replace_id": old, "reason": "idle"})

    def broken(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(agents, "_write_instructions", broken)
    with pytest.raises(OSError):
        agents.create_agent(conn, me, name="New", purpose="new work", permissions=["tasks:read"], data_dir=tmp_path)
    assert actors.get(conn, old)["archived_at"] is None and actors.find_by_name(conn, "New") is None
    monkeypatch.undo()
    monkeypatch.setenv("POS_CODEX_DISABLED", "1")
    monkeypatch.setattr(hr, "admit_agent", lambda *a, **k: {"allowed": True, "replace_id": old, "reason": "idle"})
    assert agents.create_agent(conn, me, name="New", purpose="new work", permissions=["tasks:read"],
                               data_dir=tmp_path)["created"]
    assert actors.get(conn, old)["archived_at"] is not None


def test_agents_get_is_read_only_and_numbers_agree(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from fastapi.testclient import TestClient

    from pos import actors, agents, network, tasks
    from pos.config import Settings
    from pos.core import Ctx
    from pos.db import connect
    from pos.main import create_app

    monkeypatch.setenv("POS_CODEX_DISABLED", "1")
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        conn = connect(tmp_path / "personalos.db")
        me = Ctx(actors.owner_id(conn))
        scout = agents.create_agent(conn, me, name="Scout", purpose="research", lifetime="long_lived",
                                    permissions=["tasks:read", "tasks:claim"], data_dir=tmp_path)["agent"]["id"]
        # an old return and intervention (last month) and one this week
        t = tasks.create(conn, me, {"title": "Research", "assignee": "Scout", "status": "review"})
        tasks.review(conn, me, t["id"], False, "redo")
        conn.execute("UPDATE history SET at = '2026-01-01T00:00:00+00:00' WHERE entity = 'task' AND action = 'return'")
        tasks.update(conn, me, t["id"], {"status": "review"})
        tasks.review(conn, me, t["id"], False, "again")
        now = datetime.now(timezone.utc)
        conn.execute("INSERT INTO engine_usage (at, engine, actor_id, input_tokens, output_tokens) "
                     "VALUES (?, 'claude', ?, 100, 50)", ((now - timedelta(minutes=1)).isoformat(timespec="seconds"), scout))
        conn.commit()
        audit_before = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        listed = client.get("/api/agents").json()
        assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == audit_before  # GET wrote nothing
        row = next(a for a in listed["agents"] if a["id"] == scout)
        assert row["tokens_24h"] == 150 == agents.tokens_used(conn, scout, now - timedelta(days=1), now + timedelta(seconds=5))
        net = next(n for n in network.build(conn, "24h")["nodes"] if n["id"] == scout)
        assert net["tokens"] == 150
        assert agents.detail(conn, scout)["week"]["returned"] == 1
        conn.close()
