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


def _past_run(conn, agent_id, hours_ago):
    at = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    return conn.execute("INSERT INTO runs (actor_id, kind, status, started_at) VALUES (?, 'task', 'ok', ?)",
                        (agent_id, at)).lastrowid


def test_agent_on_its_runs_day_limit_is_offered_no_task_until_the_limit_resets(app):
    client, conn, agent, key = app["client"], app["conn"], app["agent"], app["key"]
    h = {"Authorization": f"Bearer {key}"}
    t = tasks.create(conn, app["owner"], {"title": "Grow sales", "assignee": {"type": "agent", "id": agent}})
    conn.execute("UPDATE tasks SET status = 'next' WHERE id = ?", (t["id"],))
    access.set_budget(conn, app["am"], agent, "runs_day", 3, "small agent")
    oldest = _past_run(conn, agent, 20)
    _past_run(conn, agent, 10)
    _past_run(conn, agent, 1)
    conn.commit()
    # (a) on the limit: no task for the worker nor the pool's probe, so no run is even attempted
    work = client.get("/api/worker/next", params={"wait": 0}, headers=h).json()
    assert "task" not in work
    until = datetime.fromisoformat(work["state"]["limited_until"])
    assert timedelta(hours=3.9) < until - datetime.now(timezone.utc) < timedelta(hours=4.1)
    assert conn.execute("SELECT COUNT(*) FROM runs WHERE actor_id = ?", (agent,)).fetchone()[0] == 3
    # (c) the oldest run leaves the 24 h window: the agent runs normally again
    conn.execute("UPDATE runs SET started_at = ? WHERE id = ?",
                 ((datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(timespec="seconds"), oldest))
    conn.commit()
    work = client.get("/api/worker/next", params={"wait": 0}, headers=h).json()
    assert work["task"]["id"] == t["id"] and "limited_until" not in work["state"]
    assert client.post("/api/worker/runs", json={"kind": "task", "task_id": t["ref"]}, headers=h).status_code == 201


def test_a_raised_limit_lifts_the_hold_at_once(app):
    conn, agent = app["conn"], app["agent"]
    access.set_budget(conn, app["am"], agent, "runs_day", 1, "small agent")
    _past_run(conn, agent, 2)
    conn.commit()
    assert access.limited_until(conn, agent)
    access.set_budget(conn, app["am"], agent, "runs_day", 5, "busy day")
    assert access.limited_until(conn, agent) is None


def test_repeated_limit_hits_within_a_day_make_one_request_and_never_reopen_the_queue(app):
    conn, agent, am = app["conn"], app["agent"], app["am"]
    first = access.limit_hit(conn, agent, "runs_day", 3, 3)
    access.decide(conn, am, first, "deny", "3 runs a day is enough for now")
    conn.execute("UPDATE tasks SET status = 'done' WHERE assignee_id = ? AND source = 'access'", (am.actor_id,))
    conn.commit()
    # (b) ten more hits of the same limit within 24 h: still the one request, no new or reopened task
    for _ in range(10):
        assert access.limit_hit(conn, agent, "runs_day", 3, 3) == first
    reqs = conn.execute("SELECT id, detail FROM access_requests WHERE agent_id = ? AND trigger = 'limit_hit'",
                        (agent,)).fetchall()
    assert [r["id"] for r in reqs] == [first] and json.loads(reqs[0]["detail"])["repeats"] == 10
    queue = conn.execute("SELECT status FROM tasks WHERE assignee_id = ? AND source = 'access'",
                         (am.actor_id,)).fetchall()
    assert [q["status"] for q in queue] == ["done"]
    audit = lambda a: conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = ?", (a,)).fetchone()[0]  # noqa: E731
    assert audit("access_queue_reopen") == 0
    assert audit("access_limit_hit") == 1 and audit("access_limit_hit_repeat") == 1  # noted once an hour
    # A different limit is its own key; a day later the same limit raises a new request.
    assert access.limit_hit(conn, agent, "usd_day", 5, 5) != first
    conn.execute("UPDATE access_requests SET created_at = ? WHERE id = ?",
                 ((datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(timespec="seconds"), first))
    assert access.limit_hit(conn, agent, "runs_day", 3, 3) != first


def test_the_same_task_started_more_than_n_times_an_hour_is_held_in_code(app):
    from pos import runner

    conn, agent, owner = app["conn"], app["agent"], app["owner"]
    t = tasks.create(conn, owner, {"title": "Smyčka", "assignee": {"type": "agent", "id": agent}, "status": "next"})
    conn.commit()
    req = runner.RunRequest(agent, "task", "x", task_id=t["id"])
    for _ in range(access.CLAIMS_PER_HOUR):
        assert runner.start_external(conn, req).status == "running"
    blocked = runner.start_external(conn, req)  # one more within the hour: a fast loop
    assert blocked.status == "blocked" and "held until" in blocked.error
    row = conn.execute("SELECT retry_after FROM tasks WHERE id = ?", (t["id"],)).fetchone()
    assert row["retry_after"] > datetime.now(timezone.utc).isoformat(timespec="seconds")
    again = runner.start_external(conn, req)  # still held: refused again, one comment only
    assert again.status == "blocked"
    holds = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'access_loop_hold' AND entity_id = ?",
                         (t["id"],)).fetchone()[0]
    assert holds == 1
    # No Access manager run for it: nothing in its queue.
    assert not conn.execute("SELECT 1 FROM tasks WHERE source = 'access'").fetchone()
    # Another task of the same agent still runs.
    other = tasks.create(conn, owner, {"title": "Jiný", "assignee": {"type": "agent", "id": agent}, "status": "next"})
    assert runner.start_external(conn, runner.RunRequest(agent, "task", "x", task_id=other["id"])).status == "running"


def test_a_limit_hit_like_one_already_denied_is_settled_in_code_without_waking_the_manager(app):
    conn, agent, am, owner = app["conn"], app["agent"], app["am"], app["owner"]
    first = access.limit_hit(conn, agent, "runs_day", 3, 3)
    access.decide(conn, am, first, "deny", "Smyčka na jednom úkolu")
    conn.execute("UPDATE tasks SET status = 'done' WHERE assignee_id = ? AND source = 'access'", (am.actor_id,))
    old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(timespec="seconds")
    conn.execute("UPDATE access_requests SET created_at = ? WHERE id = ?", (old, first))
    # The loop goes on: one task run 6 times in 24 h, already held (so it is not held again).
    t = tasks.create(conn, owner, {"title": "T-516", "assignee": {"type": "agent", "id": agent}, "status": "next"})
    later = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat(timespec="seconds")
    conn.execute("UPDATE tasks SET retry_after = ? WHERE id = ?", (later, t["id"]))
    for h in range(6):
        conn.execute("INSERT INTO runs (actor_id, task_id, kind, status, started_at) VALUES (?, ?, 'task', 'ok', ?)",
                     (agent, t["id"], (datetime.now(timezone.utc) - timedelta(hours=2 + h)).isoformat(timespec="seconds")))
    conn.commit()
    second = access.limit_hit(conn, agent, "runs_day", 3, 3)
    r = conn.execute("SELECT * FROM access_requests WHERE id = ?", (second,)).fetchone()
    assert second != first and r["status"] == "denied" and r["decided_by"] is None
    assert f"#{first}" in r["decision_note"] and "v kódu" in r["decision_note"]
    queue = conn.execute("SELECT status FROM tasks WHERE assignee_id = ? AND source = 'access'",
                         (am.actor_id,)).fetchall()
    assert [q["status"] for q in queue] == ["done"]  # the Access manager was not woken
    assert any("zamítnuta v kódu" in b for b in _dms(conn, agent))
    # A loop caught now (its task not held yet) is settled in code at once too.
    conn.execute("UPDATE tasks SET retry_after = NULL WHERE id = ?", (t["id"],))
    conn.execute("UPDATE access_requests SET created_at = ? WHERE id = ?", (old, second))
    third = access.limit_hit(conn, agent, "usd_day", 5, 5)
    r = conn.execute("SELECT * FROM access_requests WHERE id = ?", (third,)).fetchone()
    assert r["status"] == "denied" and "podržen" in r["decision_note"]


def test_company_cap_blocks_everyone_and_only_the_owner_sets_it(app):
    client, conn, agent, key = app["client"], app["conn"], app["agent"], app["key"]
    with pytest.raises(Forbidden):
        access.set_budget(conn, app["am"], None, "usd_day", 100.0, "raise the company")
    access.set_budget(conn, app["owner"], None, "usd_day", 5.0, "company cap")
    _usage(conn, agent, 5.5)
    r = client.post("/api/worker/runs", json={"kind": "task"}, headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 409 and "company cap" in r.json()["detail"]
    assert any("Strop firmy" in b for b in _dms(conn, actors.owner_id(conn)))


def test_a_spent_company_cap_holds_the_queue_without_runs_comments_or_retries(app):
    """prod 2026-10-06: 24 runs created as blocked, T-883 re-dispatched 15x and T-884 11x every ~5.5 min."""
    from pos import api_worker

    client, conn, agent, key = app["client"], app["conn"], app["agent"], app["key"]
    h = {"Authorization": f"Bearer {key}"}
    owner = actors.owner_id(conn)
    t = tasks.create(conn, app["owner"], {"title": "Napiš kapitolu", "assignee": {"type": "agent", "id": agent}})
    conn.execute("UPDATE tasks SET status = 'next' WHERE id = ?", (t["id"],))
    access.set_budget(conn, app["owner"], None, "usd_day", 5.0, "company cap")
    _usage(conn, agent, 3.0, hours_ago=20)
    _usage(conn, agent, 3.0, hours_ago=2)  # $6 of $5: room again when the $3 of 20 h ago leaves the window
    for _ in range(3):  # the pool's probe and the worker poll again and again
        work = client.get("/api/worker/next", params={"wait": 0}, headers=h).json()
        assert "task" not in work and work["state"]["company_capped"] == "usd_day"
        r = client.post("/api/worker/runs", json={"kind": "task", "task_id": t["ref"]}, headers=h)
        assert r.status_code == 409 and "company cap" in r.json()["detail"]
    until = datetime.fromisoformat(work["state"]["company_capped_until"])
    assert timedelta(hours=3.9) < until - datetime.now(timezone.utc) < timedelta(hours=4.1)
    assert until.isoformat(timespec="seconds") in r.json()["detail"]
    # no run row (nothing counts towards the re-dispatch limits or the failure signals), no hold, no comment
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert conn.execute("SELECT retry_after FROM tasks WHERE id = ?", (t["id"],)).fetchone()[0] is None
    assert api_worker.no_result_hold(conn, t["id"], agent) is None
    assert not conn.execute("SELECT 1 FROM task_comments WHERE task_id = ? AND kind = 'system'", (t["id"],)).fetchone()
    assert not conn.execute("SELECT 1 FROM audit_log WHERE action IN ('owner_failure_notice', 'head_alert')").fetchone()
    # the owner hears it once today: until when, and where he raises the cap
    notices = [b for b in _dms(conn, owner) if "Strop firmy" in b]
    assert len(notices) == 1 and "uvolní" in notices[0] and f"/team/{app['am'].actor_id}" in notices[0]
    assert access.cap_notice(conn, access.company_capped(conn)) is False
    # the window has room again: the task comes back by itself
    conn.execute("UPDATE engine_usage SET at = ? WHERE cost_usd = 3.0 AND at < ?",
                 ((datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(timespec="seconds"),
                  (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat(timespec="seconds")))
    conn.commit()
    work = client.get("/api/worker/next", params={"wait": 0}, headers=h).json()
    assert work["task"]["id"] == t["id"] and "company_capped" not in work["state"]
    assert client.post("/api/worker/runs", json={"kind": "task", "task_id": t["ref"]}, headers=h).status_code == 201


def _run_row(conn, agent_id, cost=None, status="ok", minutes_ago=30.0, heartbeat=True):
    at = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    cur = conn.execute("INSERT INTO runs (actor_id, kind, status, started_at, heartbeat_at, cost_usd) "
                       "VALUES (?, 'task', ?, ?, ?, ?)", (agent_id, status, at, at if heartbeat else None, cost))
    conn.commit()
    return cur.lastrowid


def _second_agent(app, name="Builder"):
    made = agents.create_agent(app["conn"], app["owner"], name=name, purpose="builds", lifetime="long_lived",
                               permissions=["tasks:read", "tasks:claim"], data_dir=app["db"].parent)
    return made["agent"]["id"], made["api_key"]


def test_run_estimate_is_the_median_with_a_floor_and_higher_for_coding_agents(app):
    conn, agent = app["conn"], app["agent"]
    assert access.estimate_run_usd(conn, agent) == access.RUN_EST_FLOOR_USD  # no history: the floor
    for c in (0.05, 0.10, 0.12):
        _run_row(conn, agent, c)
    assert access.estimate_run_usd(conn, agent) == access.RUN_EST_FLOOR_USD  # cheap runs: still the floor
    for c in (0.60, 0.70, 0.80, 2.0):
        _run_row(conn, agent, c)
    assert access.estimate_run_usd(conn, agent) == pytest.approx(0.60)  # the median of 7
    _run_row(conn, agent, 50.0, status="blocked")  # refused and running rows are not costs
    _run_row(conn, agent, None, status="running")
    assert access.estimate_run_usd(conn, agent) == pytest.approx(0.60)
    dev, _ = _second_agent(app, "Coder")
    conn.execute("UPDATE actors SET role = 'developer' WHERE id = ?", (dev,))
    assert access.estimate_run_usd(conn, dev) == access.CODING_RUN_EST_FLOOR_USD > access.RUN_EST_FLOOR_USD
    for c in (0.2, 0.3, 0.5, 1.2):
        _run_row(conn, dev, c)
    assert access.estimate_run_usd(conn, dev) == pytest.approx(1.2)  # a coding agent: its p75


def test_admission_counts_the_runs_in_flight_and_admits_one_at_a_time(app):
    """prod 2026-10-06: 19:10 UTC $0.14 free and two runs started ($0.50); 19:23 three runs started ($1.20)."""
    client, conn, agent = app["client"], app["conn"], app["agent"]
    other, other_key = _second_agent(app)
    access.set_budget(conn, app["owner"], None, "usd_day", 5.0, "company cap")
    _usage(conn, agent, 4.45, hours_ago=20)  # $0.55 of room: one run at the $0.30 floor, not two
    # the first agent's run fits and is admitted
    r = client.post("/api/worker/runs", json={"kind": "task"}, headers={"Authorization": f"Bearer {app['key']}"})
    assert r.status_code == 201, r.text
    assert access.in_flight(conn) == {"runs": 1, "usd": access.RUN_EST_FLOOR_USD}
    # the second sees it in flight: $0.55 - $0.30 = $0.25 does not cover its $0.30 run; refused before a run row
    h2 = {"Authorization": f"Bearer {other_key}"}
    r = client.post("/api/worker/runs", json={"kind": "task"}, headers=h2)
    assert r.status_code == 409 and "does not fit" in r.json()["detail"] and "company cap" in r.json()["detail"]
    assert conn.execute("SELECT COUNT(*) FROM runs WHERE actor_id = ?", (other,)).fetchone()[0] == 0
    t = tasks.create(conn, app["owner"], {"title": "Napiš kapitolu", "assignee": {"type": "agent", "id": other}})
    conn.execute("UPDATE tasks SET status = 'next' WHERE id = ?", (t["id"],))
    conn.commit()
    work = client.get("/api/worker/next", params={"wait": 0}, headers=h2).json()
    assert "task" not in work and work["state"]["company_capped"] == "usd_day"
    adm = work["state"]["admission"]
    assert adm["in_flight_runs"] == 1 and adm["estimate"] == access.RUN_EST_FLOOR_USD and adm["headroom"] < 0.3
    # the cap is not spent ($4.45 of $5): no owner notice for a run that waits on the one in flight
    assert not [b for b in _dms(conn, actors.owner_id(conn)) if "Strop firmy" in b]
    # the run in flight ends having spent $0.10: the room is $0.45 again and the task is offered
    run_id = conn.execute("SELECT id FROM runs WHERE actor_id = ?", (agent,)).fetchone()[0]
    conn.execute("UPDATE runs SET status = 'ok', cost_usd = 0.10 WHERE id = ?", (run_id,))
    conn.execute("INSERT INTO engine_usage (at, engine, actor_id, run_id, cost_usd) VALUES (?, 'claude', ?, ?, 0.10)",
                 (datetime.now(timezone.utc).isoformat(timespec="seconds"), agent, run_id))
    conn.commit()
    work = client.get("/api/worker/next", params={"wait": 0}, headers=h2).json()
    assert work["task"]["id"] == t["id"] and "company_capped" not in work["state"]
    assert client.post("/api/worker/runs", json={"kind": "task", "task_id": t["ref"]}, headers=h2).status_code == 201


def test_a_stale_running_row_is_not_in_flight(app):
    conn, agent = app["conn"], app["agent"]
    _run_row(conn, agent, status="running", minutes_ago=5)
    _run_row(conn, agent, status="running", minutes_ago=60)  # no heartbeat for an hour: a dead worker
    _run_row(conn, agent, status="running", minutes_ago=600, heartbeat=False)
    assert access.in_flight(conn)["runs"] == 1
    # what a run in flight has already recorded is not counted twice
    rid = _run_row(conn, agent, status="running", minutes_ago=1)
    conn.execute("INSERT INTO engine_usage (at, engine, actor_id, run_id, cost_usd) VALUES (?, 'claude', ?, ?, 0.25)",
                 (datetime.now(timezone.utc).isoformat(timespec="seconds"), agent, rid))
    assert access.in_flight(conn) == {"runs": 2, "usd": pytest.approx(access.RUN_EST_FLOOR_USD + 0.05)}


def test_the_queue_wakes_when_the_cheapest_waiting_business_run_fits_not_for_cents(app):
    conn, agent = app["conn"], app["agent"]
    access.set_budget(conn, app["owner"], None, "usd_day", 5.0, "company cap")
    _usage(conn, agent, 0.05, hours_ago=23)   # leaves in 1 h: $0.05 is no room for a run
    _usage(conn, agent, 2.0, hours_ago=10)    # leaves in 14 h
    _usage(conn, agent, 3.0, hours_ago=1)     # $5.05 of $5: spent
    hit = access.company_capped(conn)
    assert hit and hit["metric"] == "usd_day"
    until = datetime.fromisoformat(hit["until"]) - datetime.now(timezone.utc)
    assert timedelta(hours=13.9) < until < timedelta(hours=14.1)
    held = access.admission(conn, agent)
    assert held["spent"] and held["queue_until"] == hit["until"]
    # an agent whose runs cost more waits longer than the queue
    access._waiting_cache.clear()
    conn.execute("DELETE FROM engine_usage WHERE cost_usd = 3.0")
    _usage(conn, agent, 2.80, hours_ago=1)    # $4.85 of $5: $0.15 of room, the cap is not spent but no run fits
    assert access.company_capped(conn) is None
    held = access.admission(conn, agent)
    assert held and held["spent"] and held["estimate"] == access.RUN_EST_FLOOR_USD
    queue = datetime.fromisoformat(held["queue_until"]) - datetime.now(timezone.utc)
    assert timedelta(hours=13.9) < queue < timedelta(hours=14.1)


def test_company_free_at_follows_the_rolling_window_and_the_month(app):
    conn, agent = app["conn"], app["agent"]
    now = datetime.now(timezone.utc)
    for hours, usd in ((23, 1.0), (12, 2.0), (1, 4.0)):
        _usage(conn, agent, usd, hours_ago=hours)
    # $7 used: under $6.5 when the $1 leaves (in 1 h), under $4.5 when the $2 leaves too (in 12 h)
    assert abs((access.company_free_at(conn, "usd_day", 6.5, now) - now) - timedelta(hours=1)) < timedelta(minutes=1)
    assert abs((access.company_free_at(conn, "usd_day", 4.5, now) - now) - timedelta(hours=12)) < timedelta(minutes=1)
    nxt = access.company_free_at(conn, "usd_month", 1.0, now)
    assert nxt > now and nxt.astimezone(access.TZ).day == 1 and nxt.astimezone(access.TZ).hour == 0
    assert access.company_capped(conn) is None  # no company cap set


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


def test_outbound_is_granted_ordinary_sends_go_out_money_still_needs_approval(app):
    conn, am, agent = app["conn"], app["am"], app["agent"]
    access.revoke(conn, am, agent, "outbound:*", "only e-mail from now on")
    payload = {"to": "a@b.c", "subject": "Hi", "body": "Hello"}
    with pytest.raises(Forbidden, match="outbound:email.send"):
        outbound.request(conn, Ctx(agent), "email.send", payload)
    access.grant(conn, am, agent, "outbound:email.send", "answers customers")
    a = outbound.request(conn, Ctx(agent), "email.send", payload)
    assert a["kind"] == "ordinary" and a["status"] in ("sent", "not_configured") and "id" not in a
    b = outbound.request(conn, Ctx(agent), "email.send", {**payload, "body": "We will purchase the licence."})
    assert b["status"] == "pending" and approvals.get(conn, b["id"])["details"]["kind"] == "money"
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
    cfg = access.DEFAULT_SETTINGS
    spike = 1.5 * cfg["spike_floor_usd"]
    _age(conn, agent, 24 * 10)
    access.set_budget(conn, app["owner"], agent, "usd_day", spike / cfg["spike_budget_share"] / 1.5, "test")
    for h in range(2, 50, 4):
        _usage(conn, agent, spike / cfg["spike_factor"] / 10, hours_ago=h)  # a quiet baseline
    _usage(conn, agent, spike, hours_ago=0.2)  # a truly extreme hour: > the floor, > the factor, > its day share
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
    cfg = access.DEFAULT_SETTINGS
    factor, share = cfg["spike_factor"], cfg["spike_budget_share"]
    rate = 1.5 * cfg["spike_floor_usd"] / factor  # its own hourly rate, high enough that 0.8x factor tops the floor
    _age(conn, agent, 3)
    for h in (1.5, 2.5):
        _usage(conn, agent, rate, hours_ago=h)  # `rate`/h since it exists (3 hours), not half that over a week
    _usage(conn, agent, 0.8 * factor * rate, hours_ago=0.2)  # above the floor, below the factor
    assert access.watch(conn)["paused"] == []
    _usage(conn, agent, 0.5 * factor * rate, hours_ago=0.1)  # 1.3x the factor, but within its daily budget share
    hour = 1.3 * factor * rate
    access.set_budget(conn, app["owner"], agent, "usd_day", 1.25 * hour / share, "test")
    assert access.watch(conn)["paused"] == []
    access.set_budget(conn, app["owner"], agent, "usd_day", 0.8 * hour / share, "test")
    assert access.watch(conn)["paused"] == ["Writer"]


def test_company_cap_ping_goes_to_the_ceo_not_the_owner(app, tmp_path):
    conn, agent = app["conn"], app["agent"]
    ceo = agents.create_agent(conn, app["owner"], name="CEO", purpose="runs the company", lifetime="long_lived",
                              permissions=["tasks:read"], data_dir=tmp_path)["agent"]["id"]
    access.set_budget(conn, app["owner"], None, "usd_day", 50.0, "company cap")
    access.set_budget(conn, app["owner"], None, "usd_month", 1000.0, "company cap")
    assert access.limit(conn, None, "usd_day") == 50.0 and access.limit(conn, None, "usd_month") == 1000.0
    from pos import integrations

    assert integrations.company_cap(conn)["usd_month"]["cap"] == 1000.0  # the hourly budget check reports it
    ratio = access.DEFAULT_SETTINGS["cap_alert_ratio"]
    for _ in range(9):
        _usage(conn, agent, 50.0 * (ratio + 0.01) / 9, hours_ago=2)  # just over the alert ratio today, no spike
    assert "usd_day" in access.watch(conn)["cap_alerts"]
    assert any("Strop firmy" in b for b in _dms(conn, ceo))
    assert not any("Strop firmy" in b for b in _dms(conn, actors.owner_id(conn)))

def test_company_cap_ping_at_80_percent_once_and_daily_digest(app):
    conn, agent, am = app["conn"], app["agent"], app["am"]
    owner = actors.owner_id(conn)
    access.set_budget(conn, app["owner"], None, "usd_month", 100.0, "company cap")
    ratio = access.DEFAULT_SETTINGS["cap_alert_ratio"]
    for _ in range(9):
        # over the ratio, not in the last hour; 2 h ago stays in this month on the 1st too
        _usage(conn, agent, 100.0 * (ratio + 0.01) / 9, hours_ago=2)
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
