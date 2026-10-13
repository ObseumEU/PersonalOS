"""The 2026-10 lean org (docs/REORG.md): pos.reorg_lean archives the cut roles with their history, hands
their real work to the successors, drops the old org's busywork and routines, moves routing rules and
project leads, and gives the kept agents their budgets from agent.json. Idempotent."""

import pytest

from pos import actors, agents, integrations, reorg_lean, routing, schedules, tasks
from pos.core import Ctx
from pos.db import connect, migrate


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "lean.db")
    migrate(c)
    actors.ensure_builtin(c)
    integrations.register_builtin_agents(c)
    yield c
    c.close()


@pytest.fixture
def owner(conn):
    return Ctx(actors.owner_id(conn), via="api")


def _agent(conn, owner, tmp_path, name, role, lead=None):
    made = agents.create_agent(conn, owner, name=name, purpose=name, lifetime="long_lived", data_dir=tmp_path,
                               permissions=["tasks:read", "tasks:write", "tasks:claim", "tasks:review",
                                            "messages:send", "approvals:request"])
    aid = made["agent"]["id"]
    conn.execute("UPDATE actors SET role = ?, reports_to = ? WHERE id = ?", (role, lead, aid))
    conn.commit()
    return aid


@pytest.fixture
def old_org(conn, owner, tmp_path):
    ceo = _agent(conn, owner, tmp_path, "CEO", "ceo")
    ids = {"CEO": ceo}
    for name, role in (("CTO", "cto"), ("Software Engineer", "developer"), ("SRE", "sre"),
                       ("Head of Growth", "growth"), ("Head of Customer Success", "customer_success")):
        ids[name] = _agent(conn, owner, tmp_path, name, role, ceo)
    ids["Hlídač"] = _agent(conn, owner, tmp_path, "Hlídač", "monitor", ids["SRE"])
    return ids


def test_the_lean_org_moves_work_and_archives_the_cut_roles(conn, owner, old_org):
    cto, hl = old_org["CTO"], old_org["Hlídač"]
    real = tasks.create(conn, owner, {"title": "Opravit deploy", "assignee": {"type": "agent", "id": cto}})
    busy = tasks.create(conn, owner, {"title": "Review: T-1 něco", "assignee": {"type": "agent", "id": cto}})
    inc = tasks.create(conn, owner, {"title": "[high] swap 90 %", "assignee": {"type": "agent", "id": hl}})
    routine = schedules.create(conn, owner, {"name": "CTO: technický plán týdne", "schedule": "weekly mon 09:00",
                                             "assignee": {"type": "agent", "id": cto}, "visibility": "team"})
    rule = routing.create_rule(conn, owner, {"name": "Sentinel incident → Hlídač", "source": "sentinel",
                                             "assignee": "Hlídač"})

    dry = reorg_lean.run(conn, apply=False)
    assert any("archive CTO" in line for line in dry["archive"])
    assert actors.get(conn, cto)["archived_at"] is None  # a dry run writes nothing

    reorg_lean.run(conn, apply=True)
    assert actors.get(conn, cto)["archived_at"] and actors.get(conn, hl)["archived_at"]
    se, sre = old_org["Software Engineer"], old_org["SRE"]
    assert conn.execute("SELECT assignee_id FROM tasks WHERE id = ?", (real["id"],)).fetchone()[0] == se
    assert conn.execute("SELECT assignee_id FROM tasks WHERE id = ?", (inc["id"],)).fetchone()[0] == sre
    assert conn.execute("SELECT archived_at FROM tasks WHERE id = ?", (busy["id"],)).fetchone()[0]
    assert conn.execute("SELECT archived_at FROM schedules WHERE id = ?", (routine["id"],)).fetchone()[0]
    moved = conn.execute("SELECT name, assignee FROM routing_rules WHERE id = ?", (rule["id"],)).fetchone()
    assert tuple(moved) == ("Sentinel incident → SRE", "SRE")
    for name in ("Software Engineer", "SRE", "Head of Customer Success"):
        assert actors.get(conn, old_org[name])["reports_to"] == old_org["CEO"], name

    reorg_lean.run(conn, apply=True)  # the system's own "PersonalOS" member appears during the first run
    again = reorg_lean.run(conn, apply=True)
    assert not any(again.values()), again  # idempotent


def test_owner_chat_waiting_for_a_paused_agent_goes_to_the_ceo(conn, owner, old_org):
    se = old_org["Software Engineer"]
    ask = tasks.create(conn, owner, {"title": "Chat: answer Owner (#kniha)", "assignee": {"type": "agent", "id": se}})
    agents.pause(conn, owner, se, True)
    reorg_lean.run(conn, apply=True)
    assert conn.execute("SELECT assignee_id FROM tasks WHERE id = ?", (ask["id"],)).fetchone()[0] == old_org["CEO"]
