import anyio
import pytest
from fastapi.testclient import TestClient
from mcp.client import Client

from pos import actors, agents, chat, comments, mcp_server, tasks
from pos.config import Settings
from pos.core import Ctx
from pos.db import connect, migrate


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("POS_CODEX_DISABLED", "1")
    c = connect(tmp_path / "c.db")
    migrate(c)
    actors.ensure_builtin(c)
    yield c
    c.close()


def test_activity_keeps_what_progress_note_overwrites(conn, tmp_path):
    me = Ctx(actors.owner_id(conn))
    ai = Ctx(actors.assistant_id(conn), via="mcp")
    t = tasks.create(conn, me, {"title": "Draft offer", "assignee": "ai", "status": "next"})
    tasks.claim(conn, ai, t["id"])
    tasks.report_progress(conn, ai, t["id"], 40, "outline done")
    tasks.report_progress(conn, ai, t["id"], 80, "prices in")
    tasks.complete(conn, ai, t["id"], "ready")
    tasks.review(conn, me, t["id"], False, "add the delivery terms")
    comments.add(conn, me, t["id"], "Use the 2026 price list, @" + actors.get(conn, ai.actor_id)["name"])
    kinds = [(a["kind"], a["body"]) for a in comments.list_for(conn, me, t["id"])]
    assert ("progress", "40 %: outline done") in kinds and ("progress", "80 %: prices in") in kinds
    assert ("return", "Returned: add the delivery terms") in kinds and kinds[-1][0] == "comment"
    # the mention reached the assistant's inbox with the task attached
    assert chat.inbox_unread(conn, ai.actor_id) >= 1


def test_comment_api_and_mcp(tmp_path, monkeypatch):
    monkeypatch.setenv("POS_CODEX_DISABLED", "1")
    settings = Settings(data_dir=tmp_path)
    with TestClient(__import__("pos.main", fromlist=["create_app"]).create_app(settings)) as client:
        t = client.post("/api/tasks", json={"title": "Check the invoice"}).json()
        assert client.post(f"/api/tasks/{t['ref']}/comments", json={"body": "Looks off by 100 CZK"}).status_code == 201
        assert client.get(f"/api/tasks/{t['ref']}/comments").json()[0]["body"] == "Looks off by 100 CZK"
    db = settings.db_path
    c = connect(db)
    agent = actors.find_by_name(c, "Nexus")["id"]
    agents.seed_builtin_permissions(c)
    c.close()
    server = mcp_server.build(db, default_actor=lambda conn: agent)

    async def scenario():
        async with Client(server) as cl:
            res = await cl.call_tool("task_comment", {"task_id": t["ref"], "body": "Confirmed with the supplier"})
            assert not res.is_error, res.content
            got = await cl.call_tool("get_task", {"task_id": t["ref"]})
            text = str(got.structured_content or got.content)
            assert "Confirmed with the supplier" in text and "activity" in text

    anyio.run(scenario)
