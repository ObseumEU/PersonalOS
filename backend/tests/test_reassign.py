"""Reassigning a task to an agent: the agent is told, woken and starts (pos.reassign, pos.wake)."""

import threading
import time
from datetime import datetime, timedelta, timezone

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp.client import Client

from pos import actors, agents, api_worker, audit, engines, mcp_server, org, runner, tasks, versioning, wake
from pos.config import Settings
from pos.core import Ctx
from pos.db import connect
from pos.main import create_app


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("POS_AGENT_RUNTIME", "codex")
    settings = Settings(data_dir=tmp_path)
    with TestClient(create_app(settings)) as client:
        conn = connect(settings.db_path)
        owner = Ctx(actors.owner_id(conn))

        def agent(name, permissions=("tasks:read", "tasks:claim", "approvals:request")):
            out = agents.create_agent(conn, owner, name=name, purpose="work", lifetime="long_lived",
                                      permissions=list(permissions), data_dir=tmp_path)
            return out["agent"]["id"], out["api_key"]

        yield client, conn, owner, agent, settings
        conn.close()


def _task(conn, owner, assignee, **kw):
    t = tasks.create(conn, owner, {"title": "Fix the invoice export", "notes": "Purpose: the accountant needs "
                                   "the CSV. Source: owner.", "definition_of_done": "CSV opens in Excel",
                                   "assignee": {"type": "agent", "id": assignee}, **kw})
    conn.commit()
    return t


def test_reassign_moves_the_task_releases_the_claim_cancels_the_run_and_tells_the_agent(env):
    client, conn, owner, agent, _ = env
    a, _ = agent("Dev agent")
    b, _ = agent("Mail agent")
    t = _task(conn, owner, a)
    tasks.claim(conn, Ctx(a), t["id"])
    run = runner.start_external(conn, runner.RunRequest(a, "task", "", task_id=t["id"], engine="codex"))
    conn.commit()
    assert run.status == "running"

    r = client.post(f"/api/tasks/{t['ref']}/reassign", json={"to": b, "note": "this is e-mail work"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["from"] == "Dev agent" and out["to"] == "Mail agent"
    assert out["cancelled_runs"] == [run.run_id]

    after = tasks.get(conn, owner, t["id"])
    assert after["assignee_id"] == b and after["status"] == "next" and after["progress"] == 0
    assert "Reassigned from Dev agent to Mail agent" in after["progress_note"]
    assert conn.execute("SELECT status FROM runs WHERE id = ?", (run.run_id,)).fetchone()["status"] == "cancelled"
    assert versioning.history(conn, "task", t["id"])[-1]["action"] == "reassign"
    entry = next(e for e in audit.entries(conn, entity="task", entity_id=t["id"]) if e["action"] == "reassign")
    details = entry["detail"]
    assert details["to"] == "Mail agent" and details["cancelled_runs"] == [run.run_id]
    # The new agent got a DM with the task and its description.
    inbox = agents.check_inbox(conn, b)
    assert len(inbox) == 1 and t["ref"] in inbox[0]["body"] and "accountant needs" in inbox[0]["body"]
    assert "CSV opens in Excel" in inbox[0]["body"]
    # The previous worker can no longer hand the task in.
    old_key = actors.create_key(conn, a)
    h = {"Authorization": f"Bearer {old_key}"}
    assert client.post(f"/api/worker/tasks/{t['ref']}/complete", json={"note": "x"}, headers=h).status_code == 409
    # Reassigning to the same member again is refused, any other time it works.
    assert client.post(f"/api/tasks/{t['ref']}/reassign", json={"to": "Mail agent"}).status_code == 422
    back = client.post(f"/api/tasks/{t['ref']}/reassign", json={"to": "me"}).json()
    assert back["to"] == actors.get(conn, actors.owner_id(conn))["name"] and back["message_id"] is None


def test_waiting_worker_wakes_at_once_and_claims_the_task(env, monkeypatch):
    client, conn, owner, agent, _ = env
    monkeypatch.setattr(api_worker, "POLL_FALLBACK_S", 30.0)  # only a wake can end the wait early
    a, _ = agent("Dev agent")
    b, key = agent("Mail agent")
    t = _task(conn, owner, a)
    result: dict = {}

    def poll():
        start = time.monotonic()
        r = client.get("/api/worker/next", params={"wait": 25}, headers={"Authorization": f"Bearer {key}"})
        result["elapsed"] = time.monotonic() - start
        result["body"] = r.json()

    th = threading.Thread(target=poll)
    th.start()
    for _ in range(100):
        if wake.waiting(b):
            break
        time.sleep(0.05)
    assert wake.waiting(b) == 1
    out = client.post(f"/api/tasks/{t['ref']}/reassign", json={"to": "Mail agent"}).json()
    assert out["woke_workers"] >= 1
    th.join(10)
    assert not th.is_alive()
    assert result["elapsed"] < 5, result
    assert result["body"]["task"]["ref"] == t["ref"]
    # The worker then claims and starts it.
    h = {"Authorization": f"Bearer {key}"}
    started = client.post("/api/worker/runs", json={"task_id": t["ref"]}, headers=h).json()
    claimed = client.post(f"/api/worker/tasks/{t['ref']}/claim?run_id={started['run_id']}", headers=h).json()
    assert claimed["status"] == "working" and claimed["assignee_id"] == b
    live = client.get(f"/api/tasks/{t['ref']}/live").json()
    assert live["state"] == "working" and live["run"]["id"] == started["run_id"]
    assert live["run"]["engine"] == "codex" and live["assignee"]["engine_label"].startswith("Codex")
    assert any("Reassigned" in n["note"] for n in live["notes"])


def test_wake_returns_immediately_without_waiting_for_the_poll():
    async def scenario():
        async with wake.listener(4242) as ev:
            assert wake.waiting(4242) == 1
            start = time.monotonic()
            threading.Timer(0.1, wake.wake, args=(4242,)).start()
            assert await wake.wait(ev, 20) is True
            return time.monotonic() - start

    assert anyio.run(scenario) < 2
    assert wake.waiting(4242) == 0 and wake.wake(4242) == 0


def test_agent_without_permission_or_budget_is_refused_with_a_reason(env):
    client, conn, owner, agent, _ = env
    a, _ = agent("Dev agent")
    reader, _ = agent("Reader", permissions=["tasks:read"])
    b, _ = agent("Mail agent")
    t = _task(conn, owner, a)

    r = client.post(f"/api/tasks/{t['ref']}/reassign", json={"to": "Reader"})
    assert r.status_code == 409
    assert [x["code"] for x in r.json()["reasons"]] == ["permission"]
    assert "tasks:claim" in r.json()["detail"]
    assert tasks.get(conn, owner, t["id"])["assignee_id"] == a  # nothing moved
    assert any(e["action"] == "reassign_refused" for e in audit.entries(conn, entity="task", entity_id=t["id"]))
    # Forcing does not override a permission problem.
    assert client.post(f"/api/tasks/{t['ref']}/reassign", json={"to": "Reader", "force": True}).status_code == 409

    # The picker says so before anyone picks.
    opts = {o["name"]: o for o in client.get(f"/api/tasks/{t['ref']}/reassign/options").json()}
    assert opts["Reader"]["available"] is False and opts["Reader"]["blocked"][0]["code"] == "permission"
    assert opts["Mail agent"]["available"] is True and opts["Mail agent"]["engine_label"].startswith("Codex")
    assert opts["Dev agent"]["current"] is True

    # Budget (Rozpočtář): both runtimes at their limit.
    until = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(timespec="seconds")
    engines.pause(conn, "codex", until, "usage limit")
    engines.pause(conn, "claude", until, "usage limit")
    conn.commit()
    r = client.post(f"/api/tasks/{t['ref']}/reassign", json={"to": "Mail agent"})
    assert r.status_code == 409 and r.json()["reasons"][0]["code"] == "budget"
    assert "budget" in r.json()["detail"] and "usage limit" in r.json()["detail"]
    # The owner may queue it anyway; the task then shows why it is not moving.
    r = client.post(f"/api/tasks/{t['ref']}/reassign", json={"to": "Mail agent", "force": True})
    assert r.status_code == 200 and r.json()["waiting_for"][0]["code"] == "budget"
    live = client.get(f"/api/tasks/{t['ref']}/live").json()
    assert live["state"] == "blocked" and "budget" in live["blocked"][0]["text"]

    # A private task never goes to an agent it is not shared with (constitution U6).
    p = tasks.create(conn, owner, {"title": "My health note", "visibility": "private", "status": "next"})
    conn.commit()
    engines.pause(conn, "codex", "2000-01-01T00:00:00+00:00", "reset")
    engines.pause(conn, "claude", "2000-01-01T00:00:00+00:00", "reset")
    conn.commit()
    r = client.post(f"/api/tasks/{p['ref']}/reassign", json={"to": "Dev agent"})
    assert r.status_code == 409 and r.json()["reasons"][0]["code"] == "private" and "U6" in r.json()["detail"]


def test_a_blocked_worker_run_shows_on_the_task(env):
    client, conn, owner, agent, _ = env
    a, key = agent("Dev agent")
    t = _task(conn, owner, a)
    until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    engines.pause(conn, "codex", until, "usage limit")
    engines.pause(conn, "claude", until, "usage limit")
    conn.commit()
    r = client.post("/api/worker/runs", json={"task_id": t["ref"]}, headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 409
    live = client.get(f"/api/tasks/{t['ref']}/live").json()
    assert live["state"] == "blocked" and any(b["code"] == "budget" for b in live["blocked"])


def test_mcp_task_reassign_for_the_pm(env):
    client, conn, owner, agent, settings = env
    a, _ = agent("Dev agent")
    b, _ = agent("Mail agent")
    t = _task(conn, owner, a)
    pm = org.pm_id(conn)
    assert pm is not None and agents.has_permission(conn, pm, "tasks:write")
    no_write, _ = agent("Helper")  # tasks:read/claim only
    server = mcp_server.build(settings.db_path, default_actor=lambda _c: pm)
    weak = mcp_server.build(settings.db_path, default_actor=lambda _c: no_write)

    async def scenario():
        async with Client(server) as c:
            names = {x.name for x in (await c.list_tools()).tools}
            assert "task_reassign" in names
            ok = await c.call_tool("task_reassign", {"task_id": t["ref"], "to": "Mail agent", "note": "mail"})
            assert not ok.is_error, ok.content
            refused = await c.call_tool("task_reassign", {"task_id": t["ref"], "to": "Nobody here"})
            assert refused.is_error
        async with Client(weak) as c:
            denied = await c.call_tool("task_reassign", {"task_id": t["ref"], "to": "Dev agent"})
            assert denied.is_error and "tasks:write" in denied.content[0].text

    anyio.run(scenario)
    after = tasks.get(conn, owner, t["id"])
    assert after["assignee_id"] == b and after["status"] == "next"
    assert "by Project manager" in after["progress_note"]
    assert len(agents.check_inbox(conn, b)) == 1


def test_topic_view_lists_the_assignee_and_reassigns_through_the_same_endpoint(env):
    client, conn, owner, agent, _ = env
    a, _ = agent("Dev agent")
    b, _ = agent("Mail agent")
    t = _task(conn, owner, a, topic="acme")
    topic = client.get("/api/topics/acme").json()
    row = next(x for x in topic["open"] if x["ref"] == t["ref"])
    assert row["assignee_id"] == a and row["assignee_type"] == "agent" and row["assignee_name"] == "Dev agent"
    assert client.post(f"/api/tasks/{t['ref']}/reassign", json={"to": b}).status_code == 200
    row = next(x for x in client.get("/api/topics/acme").json()["open"] if x["ref"] == t["ref"])
    assert row["assignee_id"] == b and row["status"] == "next"
    assert len(agents.check_inbox(conn, b)) == 1
