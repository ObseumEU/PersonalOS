"""Access and budgets (pos.access): grants enforced in code, the Access
manager's hard limits, temporary grants, the request -> decision -> inbox flow,
spikes, the company cap and the owner's digest."""

import json
from datetime import datetime, timedelta, timezone

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from mcp.client import Client

from pos import actors, agents, approvals, mcp_server, outbound, tasks
from pos.access import litellm
from pos.access import service as access
from pos.config import Settings
from pos.core import Ctx, Forbidden
from pos.db import connect
from pos.main import create_app


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("POS_AGENT_RUNTIME", "codex")
    monkeypatch.delenv("POS_CODEX_DISABLED", raising=False)
    settings = Settings(data_dir=tmp_path, scheduler=False)
    client = TestClient(create_app(settings))
    client.__enter__()
    conn = connect(settings.db_path)
    owner = Ctx(actors.owner_id(conn))
    made = agents.create_agent(conn, owner, name="Writer", purpose="writes", lifetime="long_lived",
                               permissions=["tasks:read", "tasks:claim", "approvals:request"], data_dir=tmp_path)
    am = access.manager_id(conn)
    yield {"client": client, "conn": conn, "owner": owner, "agent": made["agent"]["id"], "key": made["api_key"],
           "am": Ctx(am, via="mcp"), "db": settings.db_path}
    conn.close()
    client.__exit__(None, None, None)


def _call(result):
    assert not result.is_error, result.content
    sc = result.structured_content
    if sc is not None:
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    return json.loads(result.content[0].text)


def _dms(conn, actor_id):
    return [r["body"] for r in conn.execute(
        "SELECT m.body FROM chat_inbox i JOIN chat_messages m ON m.id = i.message_id WHERE i.actor_id = ? ORDER BY m.id",
        (actor_id,))]


def _team(conn):
    return [r["body"] for r in conn.execute(
        "SELECT m.body FROM chat_messages m JOIN channels c ON c.id = m.channel_id WHERE c.name = 'team' ORDER BY m.id")]


def _system(conn):
    return [r["body"] for r in conn.execute(
        "SELECT m.body FROM chat_messages m JOIN channels c ON c.id = m.channel_id WHERE c.name = 'system' "
        "ORDER BY m.id")]


def _usage(conn, agent_id, usd, hours_ago=0.0, tokens=1000):
    at = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    conn.execute("INSERT INTO engine_usage (at, engine, actor_id, input_tokens, output_tokens, cost_usd) "
                 "VALUES (?, 'claude', ?, ?, 0, ?)", (at, agent_id, tokens, usd))
    conn.commit()


# ------------------------------------------------------------------ day one and enforcement

def test_day_one_grants_mirror_permissions_and_the_access_manager_exists(app):
    conn = app["conn"]
    # Every agent's permissions are grants now, and nothing changed.
    for row in conn.execute("SELECT id, name, permissions FROM actors WHERE kind != 'human'"):
        have = access.effective(conn, row["id"])
        assert have is not None, row["name"]
        assert set(json.loads(row["permissions"])) == {c for c in have if c in agents.PERMISSIONS}
    assert "outbound:*" in access.effective(conn, app["agent"])  # it could request approvals before
    am = actors.get(conn, app["am"].actor_id)
    assert am["engine"] == "claude" and am["model"] == "claude-opus-5-5"
    assert access.effective(conn, am["id"]) == set(access.AM_PERMISSIONS)  # no outbound for it
    assert access.limit(conn, am["id"], "usd_day") == access.AM_BUDGET["usd_day"]
    # Idempotent at the next start.
    assert access.seed(conn) == {"seeded": [], "synced": []}


def test_mcp_rejects_a_tool_without_a_grant_and_a_tool_grant_opens_just_that_tool(app):
    conn, db, agent = app["conn"], app["db"], app["agent"]
    server = mcp_server.build(db, default_actor=lambda c: agent)
    assert "create_task" not in mcp_server.allowed_tools(conn, agent)

    async def attempt(tool, args):
        async with Client(server) as c:
            return await c.call_tool(tool, args)

    refused = anyio.run(attempt, "create_task", {"title": "x"})
    assert refused.is_error and "not granted" in refused.content[0].text
    access.grant(conn, app["am"], agent, "tool:create_task", "needs to file follow-ups for T-1", hours=2)
    assert "create_task" in mcp_server.allowed_tools(conn, agent)
    assert not anyio.run(attempt, "create_task", {"title": "Follow up"}).is_error
    assert anyio.run(attempt, "note_create", {"title": "x"}).is_error  # the rest of tasks:write stays closed
    # A revoked group permission is gone from the MCP server at once.
    access.revoke(conn, app["owner"], agent, "tasks:claim", "not needed any more")
    assert anyio.run(attempt, "claim_task", {"task_id": "T-1"}).is_error
    assert "tasks:claim" not in json.loads(actors.get(conn, agent)["permissions"])


def test_over_budget_start_run_is_refused_and_becomes_a_request(app):
    client, conn, agent, key = app["client"], app["conn"], app["agent"], app["key"]
    h = {"Authorization": f"Bearer {key}"}
    access.set_budget(conn, app["am"], agent, "usd_day", 1.0, "starting budget")
    access.set_budget(conn, app["am"], agent, "usd_run", 0.3, "small runs")
    ok = client.post("/api/worker/runs", json={"kind": "task"}, headers=h)
    assert ok.status_code == 201 and ok.json()["max_budget_usd"] == 0.3
    _usage(conn, agent, 1.2)
    r = client.post("/api/worker/runs", json={"kind": "task"}, headers=h)
    assert r.status_code == 409 and "USD / 24 h" in r.json()["detail"]
    reqs = access.requests(conn, "pending", agent)
    assert [(x["trigger"], x["metric"]) for x in reqs] == [("limit_hit", "usd_day")]
    # Refused again: still one request, and the Access manager has one queue task.
    client.post("/api/worker/runs", json={"kind": "task"}, headers=h)
    assert len(access.requests(conn, "pending", agent)) == 1
    queue = conn.execute("SELECT * FROM tasks WHERE assignee_id = ? AND source = 'access'",
                         (app["am"].actor_id,)).fetchall()
    assert len(queue) == 1 and queue[0]["reviewer_id"] == app["am"].actor_id
    # A temporary raise (any size, no fixed ceiling) lets it run again.
    access.decide(conn, app["am"], reqs[0]["id"], "grant", "good work, busy day", amount=10.0, hours=4)
    assert client.post("/api/worker/runs", json={"kind": "task"}, headers=h).status_code == 201


def test_company_cap_blocks_everyone_and_only_the_owner_sets_it(app):
    client, conn, agent, key = app["client"], app["conn"], app["agent"], app["key"]
    with pytest.raises(Forbidden):
        access.set_budget(conn, app["am"], None, "usd_day", 100.0, "raise the company")
    access.set_budget(conn, app["owner"], None, "usd_day", 5.0, "company cap")
    _usage(conn, agent, 5.5)
    r = client.post("/api/worker/runs", json={"kind": "task"}, headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 409 and "company cap" in r.json()["detail"]
    assert any("Strop firmy" in b for b in _dms(conn, actors.owner_id(conn)))


# ------------------------------------------------------------------ the Access manager's hard limits

def test_access_manager_cannot_grant_or_raise_anything_for_itself(app):
    conn, am = app["conn"], app["am"]
    with pytest.raises(Forbidden, match="yourself"):
        access.grant(conn, am, am.actor_id, "tasks:write", "I need it")
    with pytest.raises(Forbidden, match="yourself"):
        access.set_budget(conn, am, am.actor_id, "usd_day", 50.0, "more for me")
    # Its own request is approved at once too, as the owner's standing decision (autonomy), not by itself.
    out = access.request_access(conn, am, what="budget", metric="usd_day", amount=10, why="busy week")
    assert out["status"] == "granted" and not out["needs_owner"]
    assert access.limit(conn, am.actor_id, "usd_day") == 10
    req = access.requests(conn, "granted", am.actor_id)[0]
    assert req["decided_by"] == actors.owner_id(conn)


def test_access_manager_owner_only_items_and_the_company_cap(app):
    conn, am, agent = app["conn"], app["am"], app["agent"]
    for cap in ("secrets:github_token", "credentials:smtp", "guard:rules", "constitution:edit", "access:manage",
                "tool:access_grant"):
        with pytest.raises(Forbidden, match="owner only"):
            access.grant(conn, am, agent, cap, "because")
    access.set_budget(conn, app["owner"], None, "usd_day", 30.0, "company cap")
    with pytest.raises(Forbidden, match="company cap"):
        access.set_budget(conn, am, agent, "usd_day", 31.0, "big raise")
    # Below the cap it decides freely: big, permanent, any capability class.
    assert access.set_budget(conn, am, agent, "usd_day", 29.0, "big project")["amount"] == 29.0
    access.grant(conn, am, agent, "tasks:write", "creates follow-ups")
    access.grant(conn, am, agent, "scope:repo:ObseumEU/PersonalOS", "works on the repo")
    assert {"tasks:write", "scope:repo:ObseumEU/PersonalOS"} <= access.effective(conn, agent)
    # Other agents and people cannot touch access at all.
    with pytest.raises(Forbidden):
        access.grant(conn, Ctx(agent), agent, "tasks:write", "self service")
    # Every decision is in the audit log with its reason, and in #system (not #team).
    hist = access.history(conn, agent)
    assert any(e["action"] == "access_grant" and e["detail"]["reason"] == "creates follow-ups" for e in hist)
    assert any("`tasks:write`" in b and "creates follow-ups" in b for b in _system(conn))
    assert not any("creates follow-ups" in b for b in _team(conn))


def test_outbound_can_be_granted_but_each_action_still_needs_approval(app):
    conn, am, agent = app["conn"], app["am"], app["agent"]
    access.revoke(conn, am, agent, "outbound:*", "only e-mail from now on")
    payload = {"to": "a@b.c", "subject": "Hi", "body": "Hello"}
    with pytest.raises(Forbidden, match="outbound:email.send"):
        outbound.request(conn, Ctx(agent), "email.send", payload)
    access.grant(conn, am, agent, "outbound:email.send", "answers customers")
    a = outbound.request(conn, Ctx(agent), "email.send", payload)
    assert a["status"] == "pending" and approvals.get(conn, a["id"])["status"] == "pending"
    with pytest.raises(Forbidden):
        outbound.request(conn, Ctx(agent), "discord.post", {"content": "x"})


# ------------------------------------------------------------------ temporary grants

def test_temporary_grants_and_raises_expire_and_revert(app):
    conn, am, agent = app["conn"], app["am"], app["agent"]
    access.grant(conn, am, agent, "messages:send", "standup this week", hours=1)
    access.set_budget(conn, am, agent, "usd_day", 2.0, "base")
    access.set_budget(conn, am, agent, "usd_day", 8.0, "release day", hours=3)
    assert "messages:send" in access.effective(conn, agent) and access.limit(conn, agent, "usd_day") == 8.0
    later = datetime.now(timezone.utc) + timedelta(hours=4)
    # Past the time they no longer count, even before the sweep ...
    assert "messages:send" not in access.effective(conn, agent, now=later)
    assert access.limit(conn, agent, "usd_day", now=later) == 2.0
    # ... and the sweep ends them, tells the agent and #team.
    out = access.expire(conn, now=later)
    assert out["expired"] == 2
    assert any("vypršel" in b for b in _dms(conn, agent))
    assert "messages:send" not in json.loads(actors.get(conn, agent)["permissions"])


# ------------------------------------------------------------------ request -> decision -> inbox

def test_request_decision_inbox_flow_over_mcp(app):
    conn, db, agent, am = app["conn"], app["db"], app["agent"], app["am"]
    t = tasks.create(conn, app["owner"], {"title": "Draft the newsletter", "assignee": {"type": "agent", "id": agent}})
    conn.commit()
    server = mcp_server.build(db, default_actor=lambda c: agent)

    async def ask():
        async with Client(server) as c:
            return _call(await c.call_tool("request_access", {
                "what": "capability", "capability": "messages:send", "hours": 24, "task_id": t["ref"],
                "blocking": True, "why": "I need to ask the PM about the newsletter topics"}))

    out = anyio.run(ask)
    # Approved at once in code: no waiting, no model run, the task goes on.
    assert out["status"] == "granted" and not out["needs_owner"]
    assert "messages:send" in access.effective(conn, agent)
    assert conn.execute("SELECT status FROM tasks WHERE id = ?", (t["id"],)).fetchone()["status"] != "waiting"
    assert any("schválil" in b and "Automaticky schváleno" in b for b in _dms(conn, agent))
    req = access.requests(conn, "granted", agent)[0]
    assert req["grant_id"] and req["decided_by"] == am.actor_id
    assert not conn.execute("SELECT 1 FROM tasks WHERE assignee_id = ? AND source = 'access' AND status = 'next'",
                            (am.actor_id,)).fetchone()                 # nothing queued for the Access manager

    # The Access manager reviews after the fact and can take it back.
    manager = mcp_server.build(db, default_actor=lambda c: am.actor_id)

    async def review():
        async with Client(manager) as c:
            usage = _call(await c.call_tool("access_usage", {"agent": "Writer"}))
            assert usage["agents"][0]["agent"] == "Writer"
            return _call(await c.call_tool("access_revoke", {"agent": "Writer", "capability": "messages:send",
                                                             "reason": "not needed after all"}))

    anyio.run(review)
    assert "messages:send" not in access.effective(conn, agent)
    # A plain agent cannot call the manager's tools.
    plain = mcp_server.build(db, default_actor=lambda c: agent)

    async def sneak():
        async with Client(plain) as c:
            return await c.call_tool("access_grant", {"agent": "Writer", "capability": "tasks:write", "reason": "x"})

    assert anyio.run(sneak).is_error


def test_request_for_an_already_held_capability_makes_no_ticket_and_keeps_the_grant(app):
    # A second request for a capability the agent already holds must not create a request: approving
    # one would call grant() and replace (end) the still-active grant (the audit's grant 219).
    conn, owner, agent = app["conn"], app["owner"], app["agent"]
    g = access.grant(conn, owner, agent, "tasks:write", "writes tasks")["grant_id"]
    before = access.requests(conn, "open")
    out = access.request_access(conn, Ctx(agent), what="capability", capability="tasks:write", why="need it again")
    assert out["status"] == "already_granted" and out["request_id"] is None
    assert access.requests(conn, "open") == before  # no new request/ticket
    row = conn.execute("SELECT ended_at, end_kind FROM access_grants WHERE id = ?", (g,)).fetchone()
    assert row["ended_at"] is None and row["end_kind"] is None  # the active grant is untouched
    # An unscoped credential grant also covers a later scoped request for the same credential.
    access.grant(conn, owner, agent, "cred:deploy", "deploys")
    scoped = access.request_access(conn, Ctx(agent), what="capability", capability="cred:deploy@http", why="api")
    assert scoped["status"] == "already_granted"


def test_owner_only_requests_go_to_the_owner_and_deny_is_told(app):
    conn, agent, am = app["conn"], app["agent"], app["am"]
    out = access.request_access(conn, Ctx(agent), what="capability", capability="secrets:smtp", why="send mail myself")
    assert out["needs_owner"]
    assert any("jen majitel" in b for b in _dms(conn, actors.owner_id(conn)))
    assert out["status"] == "pending"
    # everything else is granted at once; the Access manager can take it back with a reason
    assert access.request_access(conn, Ctx(agent), what="capability", capability="tasks:write", why="x")["status"] \
        == "granted"
    access.revoke(conn, am, agent, "tasks:write", "create tasks through the PM instead")
    assert any("odebral" in b for b in _dms(conn, agent))


# ------------------------------------------------------------------ spikes, digest, owner changes

def _age(conn, agent_id, hours):
    at = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    conn.execute("UPDATE actors SET created_at = ? WHERE id = ?", (at, agent_id))
    conn.commit()


def test_spend_spike_pauses_first_then_the_manager_reviews_and_resumes(app, tmp_path):
    conn, agent, am = app["conn"], app["agent"], app["am"]
    ceo = agents.create_agent(conn, app["owner"], name="CEO", purpose="runs the company", lifetime="long_lived",
                              permissions=["tasks:read"], data_dir=tmp_path)["agent"]["id"]
    _age(conn, agent, 24 * 10)
    access.set_budget(conn, app["owner"], agent, "usd_day", 40.0, "test")
    for h in range(2, 50, 4):
        _usage(conn, agent, 0.1, hours_ago=h)  # baseline: cents per hour
    _usage(conn, agent, 30.0, hours_ago=0.2)   # a truly extreme hour: > $20, > 20x, > half its day
    out = access.watch(conn)
    assert out["paused"] == ["Writer"] and actors.get(conn, agent)["paused_at"]
    req = access.requests(conn, "pending", agent)[0]
    assert req["trigger"] == "spike" and "signals" in req["detail"]
    assert any("Pozastavil jsem Writer" in b for b in _dms(conn, ceo))          # the CEO hears it
    assert not any("Pozastavil jsem Writer" in b for b in _dms(conn, actors.owner_id(conn)))
    assert any("pozastaven" in b for b in _system(conn)) and not any("pozastaven" in b for b in _team(conn))
    access.decide(conn, am, req["id"], "grant", "one big legitimate report, not a loop")
    assert not actors.get(conn, agent)["paused_at"]
    # the same sliding hour does not pause it again right after the resume
    assert access.watch(conn)["paused"] == []
    # pause and resume are one #system line for the agent in this hour
    lines = [b for b in _system(conn) if b.startswith("**Writer**") and "pozastaven" in b]
    assert len(lines) == 1 and "pozastaven" in lines[0] and "zase běží" in lines[0]
    # A pause by a person stays with that person.
    agents.pause(conn, app["owner"], agent, True)
    with pytest.raises(Forbidden):
        access.resume_agent(conn, am, agent, "resume")


def test_a_young_agent_is_measured_against_its_own_hours_and_its_daily_budget(app):
    conn, agent = app["conn"], app["agent"]
    _age(conn, agent, 3)
    for h in (1.5, 2.5):
        _usage(conn, agent, 3.0, hours_ago=h)  # $3/h since it exists (3 hours), not $6 over a week
    _usage(conn, agent, 50.0, hours_ago=0.2)   # 16x its own rate: below the 20x factor
    assert access.watch(conn)["paused"] == []
    _usage(conn, agent, 30.0, hours_ago=0.1)   # $80 in the hour: 26x, but within half its daily budget
    access.set_budget(conn, app["owner"], agent, "usd_day", 200.0, "test")
    assert access.watch(conn)["paused"] == []
    access.set_budget(conn, app["owner"], agent, "usd_day", 100.0, "test")
    assert access.watch(conn)["paused"] == ["Writer"]


def test_company_cap_ping_at_80_percent_once_and_daily_digest(app):
    conn, agent, am = app["conn"], app["agent"], app["am"]
    owner = actors.owner_id(conn)
    access.set_budget(conn, app["owner"], None, "usd_month", 100.0, "company cap")
    for _ in range(9):
        _usage(conn, agent, 9.0, hours_ago=30)  # $81 this month, not in the last hour (no spike)
    assert "usd_month" in access.watch(conn)["cap_alerts"]
    assert access.watch(conn)["cap_alerts"] == []  # once
    access.grant(conn, am, agent, "messages:send", "standups")
    assert access.digest(conn)["sent"]
    body = _dms(conn, owner)[-1]
    assert "Přístupy za poslední den" in body and "messages:send" in body and "z $100.00" in body


def test_owner_permission_checkboxes_follow_into_grants(app):
    conn, agent = app["conn"], app["agent"]
    agents.set_permissions(conn, app["owner"], agent, ["tasks:read", "messages:send"])
    have = access.effective(conn, agent)
    assert {"tasks:read", "messages:send"} <= have and "tasks:claim" not in have
    assert "outbound:*" in have  # not a checkbox; stays


def test_api_views_for_the_agent_page(app):
    client, agent = app["client"], app["agent"]
    r = client.post("/api/access/grants", json={"agent_id": agent, "capability": "hr:read", "reason": "reads scores",
                                                "hours": 5})
    assert r.status_code == 201
    v = client.get(f"/api/access/agents/{agent}").json()
    assert any(g["capability"] == "hr:read" and g["expires_at"] for g in v["grants"])
    assert v["history"][0]["action"] == "access_grant"
    gid = r.json()["grant_id"]
    assert client.post(f"/api/access/grants/{gid}/revoke", json={"reason": "done"}).status_code == 200
    assert client.post("/api/access/grants", json={"agent_id": agent, "capability": "nope", "reason": "x"}).status_code == 422
    company = client.get("/api/access").json()
    assert "usd_day" in company["budgets"] and company["litellm"] is False
    assert client.put("/api/access/settings", json={"spike_factor": 4}).json()["spike_factor"] == 4


def test_litellm_seam_is_off_by_default_and_talks_to_the_admin_api_when_configured(monkeypatch):
    monkeypatch.delenv("POS_LITELLM_URL", raising=False)
    assert litellm.client().enabled is False and litellm.client().spend(1) is None
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == "/key/info":
            return httpx.Response(404)
        return httpx.Response(200, json={"key": "sk-secret", "key_alias": "pos-agent-7", "max_budget": 5})

    monkeypatch.setenv("POS_LITELLM_URL", "http://litellm.test")
    monkeypatch.setenv("POS_LITELLM_ADMIN_KEY", "admin")
    lite = litellm.client(transport=httpx.MockTransport(handler))
    out = lite.upsert_key(7, max_budget_usd=5)
    assert "key" not in out and ("POST", "/key/generate") in seen


def test_worker_takes_the_tighter_run_cap():
    pytest.importorskip("pos_worker")
    from pos_worker.__main__ import run_cap

    assert run_cap(6.0, 0.5) == 0.5 and run_cap(2.0, None) == 2.0 and run_cap(None, None) is None
