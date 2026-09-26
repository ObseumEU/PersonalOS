import json

import anyio
import pytest
from mcp.client import Client

from pos import actors, agents, integrations, killswitch, mcp_server, scheduler, schedules, tasks
from pos.core import Ctx
from pos.db import connect, migrate


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "s.db"
    c = connect(path)
    migrate(c)
    actors.ensure_builtin(c)
    agents.seed_builtin_permissions(c)
    integrations.register_builtin_agents(c)
    c.close()
    return path


@pytest.fixture
def conn(db):
    c = connect(db)
    yield c
    c.close()


def _call(result):
    assert not result.is_error, result.content
    sc = result.structured_content
    if sc is not None:
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    return json.loads(result.content[0].text)


def _agent(conn, tmp_path, name="Scout", permissions=("tasks:read", "tasks:claim")):
    owner = Ctx(actors.owner_id(conn))
    return agents.create_agent(conn, owner, name=name, purpose=f"{name} work", data_dir=tmp_path,
                               permissions=list(permissions))["agent"]["id"]


def test_schedule_grammar_and_interval():
    assert schedules.interval_minutes("every 2h") == 120
    assert schedules.interval_minutes("every 20m") == 20
    assert schedules.interval_minutes("daily 07:00") == 24 * 60
    assert schedules.interval_minutes("weekdays 07:00") == 24 * 60
    with pytest.raises(ValueError):
        scheduler.next_run("sometimes", __import__("datetime").datetime.now())


def test_agent_schedules_itself_over_mcp_and_a_firing_creates_its_task(db, conn, tmp_path):
    agent = _agent(conn, tmp_path)
    conn.commit()
    server = mcp_server.build(db, default_actor=lambda _c: agent)

    async def scenario():
        async with Client(server) as cl:
            s = _call(await cl.call_tool("schedule_create", {
                "name": "Morning inbox check", "schedule": "daily 07:00",
                "notes": "Look at new mail and file it", "topic": "mail"}))
            assert s["visibility"] == "personal" and s["assignee_id"] == agent and s["status"] == "active"
            too_often = await cl.call_tool("schedule_create", {"name": "Spam", "schedule": "every 5m"})
            assert too_often.is_error and "15 min" in too_often.content[0].text
            others = await cl.call_tool("schedule_create", {"name": "For the owner", "schedule": "daily 09:00",
                                                            "assignee": "me", "visibility": "team"})
            assert not others.is_error  # "me" is the agent itself
            owner_task = await cl.call_tool("schedule_create", {"name": "For the owner", "schedule": "daily 09:00",
                                                                "assignee": "Owner", "visibility": "team"})
            assert owner_task.is_error and "tasks:write" in owner_task.content[0].text  # no more than its rights
            fired = _call(await cl.call_tool("schedule_run_now", {"schedule_id": s["id"]}))
            assert fired["task"].startswith("T-")
            again = _call(await cl.call_tool("schedule_run_now", {"schedule_id": s["id"]}))
            assert "still open" in again["skipped"]  # no pile-up while the last one is open
            mine = _call(await cl.call_tool("schedule_list", {}))
            assert {m["name"] for m in mine} == {"Morning inbox check", "For the owner"}
            _call(await cl.call_tool("schedule_pause", {"schedule_id": s["id"]}))
            _call(await cl.call_tool("schedule_delete", {"schedule_id": s["id"]}))
            return fired["task"]

    ref = anyio.run(scenario)
    t = tasks.get(conn, Ctx(actors.owner_id(conn)), tasks.parse_id(ref))
    assert t["title"] == "Morning inbox check" and t["assignee_id"] == agent and t["status"] == "next"
    assert t["topic"] == "mail" and t["source"].startswith("schedule:")
    archived = schedules.list_schedules(conn, archived=True)
    assert [a["name"] for a in archived] == ["Morning inbox check"]  # archived, not deleted
    hist = [h["action"] for h in __import__("pos.versioning", fromlist=["x"]).history(conn, "schedule", archived[0]["id"])]
    assert hist[0] == "create" and "archive" in hist


def test_limits_kill_switch_and_due_firing(conn, tmp_path):
    agent = _agent(conn, tmp_path)
    ctx = Ctx(agent, via="mcp")
    for i in range(5):
        schedules.create(conn, ctx, {"name": f"Job {i}", "schedule": "every 1h"})
    with pytest.raises(tasks.Invalid, match="limit of 5"):
        schedules.create(conn, ctx, {"name": "Job 6", "schedule": "every 1h"})
    s = schedules.list_schedules(conn, actor_id=agent)[0]
    conn.execute("UPDATE schedules SET next_run_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (s["id"],))
    owner = Ctx(actors.owner_id(conn))
    killswitch.freeze(conn, owner, "test")
    out = schedules.run_due(conn)
    assert out["fired"][s["id"]] == {"skipped": "kill switch is on", "next_run_at": out["fired"][s["id"]]["next_run_at"]}
    assert conn.execute("SELECT COUNT(*) FROM tasks WHERE source LIKE 'schedule:%'").fetchone()[0] == 0
    killswitch.unfreeze(conn, owner)
    conn.execute("UPDATE schedules SET next_run_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (s["id"],))
    out = schedules.run_due(conn)
    assert out["fired"][s["id"]]["task"].startswith("T-")
    # Pausing the agent stops its schedules from firing.
    conn.execute("UPDATE actors SET paused_at = '2026-01-01' WHERE id = ?", (agent,))
    conn.execute("UPDATE schedules SET next_run_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (s["id"],))
    assert "paused" in schedules.run_due(conn)["fired"][s["id"]]["skipped"]


def test_team_schedule_for_another_agent_needs_tasks_write(conn, tmp_path):
    hr = actors.find_by_name(conn, "Head of People")["id"]  # has tasks:write
    worker = _agent(conn, tmp_path, "Helper")
    s = schedules.create(conn, Ctx(hr, via="mcp"), {
        "name": "Daily agent review", "schedule": "daily 08:00", "visibility": "team",
        "assignee": {"type": "agent", "id": worker}, "notes": "Review open and stuck tasks"})
    assert s["created_by_name"] == "Head of People" and s["assignee_name"] == "Helper"
    assert s in schedules.list_schedules(conn, actor_id=worker)  # shows on both pages
    assert s in schedules.list_schedules(conn, actor_id=hr)
    res = schedules.fire(conn, s["id"])
    assert tasks.get(conn, Ctx(actors.owner_id(conn)), tasks.parse_id(res["task"]))["assignee_id"] == worker
