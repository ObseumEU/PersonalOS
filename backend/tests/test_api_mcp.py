import json

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp.client import Client

from pos import actors, mcp_server
from pos.config import Settings
from pos.db import connect, migrate
from pos.main import create_app


@pytest.fixture(autouse=True)
def no_codex(monkeypatch):
    monkeypatch.setenv("POS_CODEX_DISABLED", "1")


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path, mcp_token="owner-key"))) as c:
        yield c


def test_task_flow_over_http(client):
    r = client.post("/api/tasks/capture", json={"text": "acme renewal?? check contract"})
    assert r.status_code == 201 and r.json()["status"] == "inbox"
    ref = r.json()["ref"]
    assert client.get("/api/tasks/counts").json()["inbox"] == 1

    s = client.post(f"/api/tasks/{ref}/suggest").json()
    assert s["engine"] == "rules"
    t = client.post(f"/api/tasks/{ref}/clarify", json={
        "action": "accept",
        "fields": {"title": "Renew or cancel Acme", "topic": "acme", "priority": 1,
                   "steps": [{"title": "Extract terms", "assignee": "ai", "reason": "reading"}]},
    }).json()
    assert t["status"] == "next" and t["steps"][0]["assignee_type"] == "ai"

    t = client.patch(f"/api/tasks/{ref}", json={"priority": 2}).json()
    assert t["priority"] == 2
    hist = client.get(f"/api/tasks/{ref}/history").json()
    assert [h["action"] for h in hist][-1] == "update"
    assert client.post(f"/api/tasks/{ref}/restore", json={"version": hist[-2]["version"]}).json()["priority"] == 1

    assert client.get("/api/tasks/T-999").status_code == 404
    assert client.post("/api/tasks", json={"title": "x", "priority": 7}).status_code == 422
    assert any(e["action"] == "clarify:accept" for e in client.get("/api/audit").json())
    assert client.get("/api/tasks/topics").json() == [{"topic": "acme", "open": 2}]


def test_mcp_http_requires_bearer(client):
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    headers = {"Accept": "application/json, text/event-stream"}
    assert client.post("/mcp", json=body, headers=headers).status_code == 401
    assert client.post("/mcp", json=body, headers={**headers, "Authorization": "Bearer nope"}).status_code == 401
    ok = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
        headers={**headers, "Authorization": "Bearer owner-key"})
    assert ok.status_code == 200


def _call(result):
    assert not result.is_error, result.content
    if result.structured_content is not None:
        sc = result.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    return json.loads(result.content[0].text)


def test_mcp_tools_as_an_agent(tmp_path):
    db = tmp_path / "m.db"
    conn = connect(db)
    migrate(conn)
    ids = actors.ensure_builtin(conn)
    conn.close()
    agent = ids["Knowledge agent"]
    server = mcp_server.build(db, default_actor=lambda c: agent)

    async def scenario():
        async with Client(server) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            assert {"list_tasks", "get_task", "capture", "create_task", "update_task", "complete_task",
                    "assign_task", "claim_task", "heartbeat", "report_progress", "request_approval"} <= names
            t = _call(await c.call_tool("create_task", {"title": "Compare pricing", "assignee": "Knowledge agent"}))
            assert t["status"] == "next"
            hb = _call(await c.call_tool("heartbeat", {}))
            assert [q["ref"] for q in hb["queue"]] == [t["ref"]]
            assert _call(await c.call_tool("claim_task", {"task_id": t["ref"]}))["status"] == "working"
            assert _call(await c.call_tool("report_progress", {"task_id": t["ref"], "percent": 60}))["progress"] == 60
            done = _call(await c.call_tool("complete_task", {"task_id": t["ref"], "note": "table attached"}))
            assert done["status"] == "review"
            ap = _call(await c.call_tool("request_approval", {"action": "send_email", "task_id": t["ref"],
                                                              "details": {"to": "acme"}}))
            assert ap["status"] == "pending"
            bad = await c.call_tool("get_task", {"task_id": "T-999"})
            assert bad.is_error
            res = await c.read_resource("tasks://inbox")
            assert json.loads(res.contents[0].text) == []
            prompt = await c.get_prompt("plan_my_day")
            assert "tasks://today" in prompt.messages[0].content.text

    anyio.run(scenario)

    conn = connect(db)
    actions = [r[0] for r in conn.execute("SELECT action FROM audit_log WHERE via = 'mcp'")]
    assert "mcp:claim_task" in actions and "mcp:get_task:refused" in actions
    conn.close()
