import json

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp.client import Client

from pos import actors, agents, integrations, mcp_server, network, org, schedules, tasks
from pos.config import Settings
from pos.core import Ctx, Forbidden
from pos.db import connect, migrate
from pos.hr import service as hr
from pos.hr.policy import HRPolicy
from pos.main import create_app


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "o.db"
    c = connect(path)
    migrate(c)
    actors.ensure_builtin(c)
    integrations.register_builtin_agents(c)
    c.close()
    return path


@pytest.fixture
def conn(db):
    c = connect(db)
    yield c
    c.close()


def _agent(conn, tmp_path, name, permissions=("tasks:read", "tasks:claim")):
    owner = Ctx(actors.owner_id(conn))
    return agents.create_agent(conn, owner, name=name, purpose=f"{name} work", lifetime="long_lived",
                               data_dir=tmp_path, permissions=list(permissions))["agent"]["id"]


def _call(result):
    assert not result.is_error, result.content
    sc = result.structured_content
    if sc is not None:
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    return json.loads(result.content[0].text)


def test_org_fields_are_seeded_and_idempotent(conn, tmp_path):
    pm = org.pm_id(conn)
    owner = actors.owner_id(conn)
    dev = _agent(conn, tmp_path, "Dev agent")
    by_name = {m["name"]: m for m in org.chart(conn)}
    assert by_name["Owner"]["role"] == "owner" and by_name["Owner"]["reports_to"] is None
    assert by_name["Project manager"]["reports_to"] == owner and by_name["Project manager"]["level"] == 1
    for name, role in (("Assistant", "assistant"), ("Knowledge agent", "knowledge"), ("Nexus", "automation"),
                       ("Deployer", "deployer"), ("HR agent", "hr"), ("Dev agent", "developer")):
        assert by_name[name]["role"] == role and by_name[name]["reports_to"] == pm, name
        assert by_name[name]["level"] == 2
    assert by_name["Dev agent"]["team"] == "engineering" and by_name["Dev agent"]["id"] == dev
    # Running startup again changes nothing and never overwrites the owner's choice.
    org.set_org(conn, Ctx(owner), dev, {"team": "platform"})
    integrations.register_builtin_agents(conn)
    again = {m["name"]: m for m in org.chart(conn)}
    assert again["Dev agent"]["team"] == "platform"
    assert conn.execute("SELECT COUNT(*) FROM actors WHERE name = 'Project manager'").fetchone()[0] == 1
    assert agents.detail(conn, dev)["reports_to_name"] == "Project manager"


def test_pm_exists_within_hr_limits(conn):
    pm = org.pm_id(conn)
    row = actors.get(conn, pm)
    assert row["kind"] == "agent" and row["created_by"] == actors.owner_id(conn)
    assert sorted(agents.permissions_of(conn, pm)) == sorted(org.PM_PERMISSIONS)
    assert not agents.has_permission(conn, pm, "agents:create")  # it routes work, it does not hire
    # A platform agent: HR tracks it but it takes no slot of the active-agent limit.
    from pos.hr import store as hr_store

    assert hr_store.get_profile(conn, pm)["system"] == 1
    listed = {a.name: a for a in hr.CorePlatform(conn, Ctx(pm)).list_agents()}
    assert listed["Project manager"].system
    assert hr.admit_agent(conn, Ctx(actors.owner_id(conn)), name="X", purpose="x")["allowed"]
    assert len(hr._active_agents(hr.CorePlatform(conn, Ctx(pm)))) <= HRPolicy().max_active_agents


def test_handoff_reassigns_and_records_an_edge(db, conn, tmp_path):
    dev = _agent(conn, tmp_path, "Dev agent")
    mail = _agent(conn, tmp_path, "Mail agent")
    owner = Ctx(actors.owner_id(conn))
    t = tasks.create(conn, owner, {"title": "Reply to the invoice", "assignee": "Dev agent"})
    conn.commit()
    server = mcp_server.build(db, default_actor=lambda _c: dev)

    async def scenario():
        async with Client(server) as cl:
            out = _call(await cl.call_tool("handoff_task", {"task": t["ref"], "to": "Mail agent",
                                                            "note": "This is e-mail, not code"}))
            chart = _call(await cl.call_tool("org_chart", {}))
            return out, chart

    out, chart = anyio.run(scenario)
    assert out["to"] == "Mail agent" and out["from"] == "Dev agent"
    assert {"name": "Project manager", "role": "project_manager"}.items() <= next(
        m for m in chart if m["name"] == "Project manager").items()
    after = tasks.get(conn, owner, t["id"])
    assert after["assignee_id"] == mail and after["status"] == "next"
    assert "This is e-mail" in after["progress_note"]
    from pos import versioning

    assert versioning.history(conn, "task", t["id"])[-1]["action"] == "handoff"
    inbox = agents.check_inbox(conn, mail)
    assert len(inbox) == 1 and t["ref"] in inbox[0]["body"]
    row = conn.execute("SELECT * FROM handoffs").fetchone()
    assert (row["from_actor"], row["to_actor"], row["task_id"]) == (dev, mail, t["id"])


def test_network_has_message_handoff_and_org_edges(conn, tmp_path):
    dev = _agent(conn, tmp_path, "Dev agent", permissions=["tasks:read", "tasks:claim", "messages:send"])
    mail = _agent(conn, tmp_path, "Mail agent")
    owner = Ctx(actors.owner_id(conn))
    t = tasks.create(conn, owner, {"title": "Fix the build", "assignee": "Dev agent"})
    agents.send_message(conn, Ctx(dev), mail, "Can you check the invoice thread?")
    org.handoff(conn, Ctx(dev), t["id"], "Mail agent", "yours")
    conn.commit()
    net = network.build(conn, "24h")
    edges = {(e["from"], e["to"], e["kind"]): e for e in net["edges"]}
    assert edges[(dev, mail, "message")]["count"] == 1  # the handoff's own message is not counted twice
    assert edges[(dev, mail, "handoff")]["scope"] == "peer"
    pm = org.pm_id(conn)
    assert edges[(dev, pm, "org")]["scope"] == "org"
    assert edges[(pm, actors.owner_id(conn), "org")]
    assert any(e["kind"] == "handoff" for e in net["events"])
    assert next(n for n in net["nodes"] if n["id"] == dev)["role"] == "developer"


def test_standup_schedule_exists_and_fires_a_task_for_the_pm(conn):
    pm = org.pm_id(conn)
    mine = [s for s in schedules.list_schedules(conn, actor_id=pm) if s["name"] == org.STANDUP_NAME]
    assert len(mine) == 1
    s = mine[0]
    assert s["visibility"] == "team" and s["schedule"] == "weekdays 08:30" and s["assignee_id"] == pm
    org.ensure_standup(conn)  # idempotent
    assert len([x for x in schedules.list_schedules(conn) if x["name"] == org.STANDUP_NAME]) == 1
    fired = schedules.fire(conn, s["id"])
    t = tasks.get(conn, Ctx(actors.owner_id(conn)), tasks.parse_id(fired["task"]))
    assert t["assignee_id"] == pm and t["status"] == "next" and "summary" in t["definition_of_done"]
    # Archived by the owner: startup does not bring it back.
    schedules.archive(conn, Ctx(actors.owner_id(conn)), s["id"])
    assert org.ensure_standup(conn) is None


def test_permission_checks(conn, tmp_path):
    dev = _agent(conn, tmp_path, "Dev agent")  # tasks:read + tasks:claim, no tasks:write
    mail = _agent(conn, tmp_path, "Mail agent")
    owner = Ctx(actors.owner_id(conn))
    theirs = tasks.create(conn, owner, {"title": "Mail triage", "assignee": "Mail agent"})
    with pytest.raises(Forbidden, match="tasks:write"):
        org.handoff(conn, Ctx(dev), theirs["id"], "Dev agent")
    # Its own task it may pass on; the PM (tasks:write) may move anyone's.
    own = tasks.create(conn, owner, {"title": "Code fix", "assignee": "Dev agent"})
    assert org.handoff(conn, Ctx(dev), own["id"], "Mail agent")["to"] == "Mail agent"
    assert org.handoff(conn, Ctx(org.pm_id(conn)), theirs["id"], "Dev agent")["to"] == "Dev agent"
    # Only the owner changes the org chart; no loops.
    with pytest.raises(Forbidden):
        org.set_org(conn, Ctx(org.pm_id(conn)), dev, {"role": "boss"})
    with pytest.raises(tasks.Invalid, match="reports to"):
        org.set_org(conn, owner, org.pm_id(conn), {"reports_to": dev})
    # The kill switch stops handoffs.
    from pos import killswitch

    killswitch.freeze(conn, owner, "test")
    with pytest.raises(Forbidden):
        org.handoff(conn, Ctx(mail), own["id"], "Dev agent")


def test_org_api(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        data = client.get("/api/org").json()
        names = {m["name"]: m for m in data["members"]}
        assert names["Project manager"]["id"] == data["project_manager"]
        assistant = names["Assistant"]["id"]
        r = client.put(f"/api/agents/{assistant}/org", json={"team": "front desk"})
        assert r.status_code == 200 and r.json()["team"] == "front_desk"
        assert r.json()["role"] == "assistant"  # untouched fields stay
        listed = {a["name"]: a for a in client.get("/api/agents").json()["agents"]}
        assert listed["Assistant"]["reports_to_name"] == "Project manager"
