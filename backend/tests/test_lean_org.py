"""The 2026-10 cut (27 agents to ~9): once the COO, the CTO, the Hlídač, the Performance Coach, the Head of
Growth and the rest are archived, nothing they did by name falls to the owner or to an archived member."""

import pytest

from pos import (actors, agents, command_policy, feedback, hiring, integrations, monitor, notices, ops_runbook, org,
                 roles, routing, taint, tasks, weekly)
from pos.core import Ctx
from pos.db import connect, migrate
from pos.hr import service as hr

PERMS = ["tasks:read", "tasks:write", "tasks:claim", "tasks:review", "messages:send", "agents:create"]


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "o.db")
    migrate(c)
    actors.ensure_builtin(c)
    integrations.register_builtin_agents(c)
    c.commit()
    yield c
    c.close()


@pytest.fixture
def owner(conn):
    return Ctx(actors.owner_id(conn), via="api")


def _agent(conn, owner, tmp_path, name, role=None, lead=None, perms=PERMS) -> int:
    found = actors.find_by_name(conn, name)
    aid = found["id"] if found is not None else agents.create_agent(
        conn, owner, name=name, purpose=f"{name} work", lifetime="long_lived", data_dir=tmp_path,
        permissions=list(perms))["agent"]["id"]
    conn.execute("UPDATE actors SET role = COALESCE(?, role), reports_to = COALESCE(?, reports_to) WHERE id = ?",
                 (role, lead, aid))
    conn.commit()
    return aid


def _archive(conn, owner, name) -> int:
    aid = conn.execute("SELECT id FROM actors WHERE name = ?", (name,)).fetchone()["id"]
    agents.archive(conn, owner, aid, "the 2026-10 cut")
    return aid


@pytest.fixture
def lean(conn, owner, tmp_path):
    """The kept company: the CEO under the owner, the engineer and the SRE under the CEO; the cut ones archived."""
    ceo = _agent(conn, owner, tmp_path, roles.CEO, "ceo", owner.actor_id)
    ids = {"ceo": ceo,
           "se": _agent(conn, owner, tmp_path, roles.ENGINEER, "developer", ceo),
           "sre": _agent(conn, owner, tmp_path, roles.SRE, "sre", ceo),
           "cs": _agent(conn, owner, tmp_path, roles.CUSTOMER_SUCCESS, "customer_success", ceo)}
    for name, role in ((roles.CTO, "cto"), (roles.GROWTH, "growth"), (roles.COACH, "coach"),
                       (roles.ONCALL, "monitor"), (roles.QA, "qa")):
        ids[role] = _agent(conn, owner, tmp_path, name, role, ceo)
    for name in (roles.COO, roles.CTO, roles.GROWTH, roles.COACH, roles.ONCALL, roles.QA):
        _archive(conn, owner, name)
    return ids


def test_the_successor_map_is_one_place_and_follows_the_chain():
    assert roles.LEAN_SUCCESSORS[roles.COO] == roles.CEO
    assert roles.LEAN_SUCCESSORS[roles.KNIHA_MARKETING] == roles.KNIHA_GROWTH
    assert roles.successor_names("Project manager") == [roles.COO, roles.CEO]  # 2026-09 name -> COO -> CEO
    assert roles.successor_names(roles.CEO) == []


def test_an_archived_coo_is_never_the_default_lead_and_is_not_recreated(conn, owner, lean, tmp_path):
    assert org.coo_id(conn) is None and org.pm_id(conn) == lean["ceo"]
    assert org.ensure_pm(conn) is None  # an archived COO stays archived
    assert conn.execute("SELECT COUNT(*) FROM actors WHERE name = ?", (roles.COO,)).fetchone()[0] == 1
    assert org.ensure_standup(conn) is None  # no COO, no standup (the CEO does not inherit it)
    new = _agent(conn, owner, tmp_path, "Helper")
    assert actors.get(conn, new)["reports_to"] == lean["ceo"]
    org.set_org(conn, owner, new, {"reports_to": None})  # out of a lead's hands: back to the default lead
    assert actors.get(conn, new)["reports_to"] == lean["ceo"]
    req = hiring.request(conn, owner, name="Writer", purpose="writes the newsletter")
    assert req["lead_id"] == lean["ceo"] and req["lead_name"] == roles.CEO


def test_an_archived_head_of_people_is_not_recreated_and_its_reviews_skip(conn, owner, lean):
    _archive(conn, owner, roles.HR)
    assert hr.ensure_hr_agent(conn) is None
    assert conn.execute("SELECT COUNT(*) FROM actors WHERE name = ?", (roles.HR,)).fetchone()[0] == 1
    assert "skipped" in hr.daily_review(conn) and "skipped" in hr.weekly_report(conn)
    from pos.hr import schedule

    schedule.run_due(conn)  # the HR loop must not fail
    assert hr.admit_agent(conn, owner, name="X", purpose="something new")["allowed"]  # the limits still answer


def test_names_of_archived_members_reach_their_successors(conn, owner, lean):
    for name, want in ((roles.CTO, lean["se"]), (roles.GROWTH, lean["cs"]), (roles.COO, lean["ceo"]),
                       ("Project manager", lean["ceo"])):
        cols = tasks.resolve_assignee(conn, owner, name)
        assert cols["assignee_id"] == want and cols["assignee_type"] == "agent", name
    # a member of the cut that was never created here (Nabu Specialist) still lands with its successor
    assert tasks.resolve_assignee(conn, owner, roles.NABU_SPECIALIST)["assignee_id"] == lean["se"]
    # an outside name stays outside
    assert tasks.resolve_assignee(conn, owner, "Jana Nováková")["assignee_type"] == "external"
    # an explicit id of an archived member is still refused (whoever chose it hears it)
    with pytest.raises(tasks.Invalid):
        tasks.resolve_assignee(conn, owner, {"type": "agent", "id": lean["cto"]})
    # a routing rule naming the archived CTO (the GitHub triage rule) makes a task in the engineer's queue
    routing.create_rule(conn, owner, {"name": "triage", "source": "github", "match": {"kind": "issue"},
                                      "assignee": roles.CTO, "priority": 3, "topic": "triage", "position": 0})
    out = routing.ingest(conn, owner, {"source": "github", "kind": "issue", "title": "Crash on login", "ref": "gh/1"})
    t = conn.execute("SELECT assignee_id, assignee_type, status FROM tasks WHERE id = ?", (out["task_id"],)).fetchone()
    assert t["assignee_id"] == lean["se"] and t["status"] == "next"
    # DMs and notices to an archived member reach its successor
    assert notices.successor(conn, lean["coach"]) == lean["ceo"]
    assert notices.successor(conn, lean["monitor"]) == lean["sre"]
    assert agents.successor_of(conn, lean["qa"]) == lean["se"]


def test_command_approvals_without_a_cto_go_to_the_lead_then_the_ceo(conn, owner, lean, tmp_path):
    assert command_policy.approver(conn, lean["se"])["id"] == lean["ceo"]  # its lead is the CEO
    lead = _agent(conn, owner, tmp_path, "Kniha Developer Lead", "product_lead", lean["ceo"])
    dev = _agent(conn, owner, tmp_path, roles.KNIHA_DEV, "developer", lead)
    assert command_policy.approver(conn, dev)["id"] == lead
    weak = _agent(conn, owner, tmp_path, "Reader", "specialist", lean["ceo"], perms=["tasks:read"])
    under = _agent(conn, owner, tmp_path, "Under reader", "specialist", weak)
    assert command_policy.approver(conn, under)["id"] == lean["ceo"]  # its lead cannot decide
    assert command_policy.approver(conn, lean["ceo"])["id"] == owner.actor_id  # the CEO's own: the owner
    got = command_policy.request(conn, Ctx(dev, via="mcp"), "git push origin main", why="ship")
    assert got["approver"] == "Kniha Developer Lead"


def test_platform_fallbacks_skip_the_cut_members_and_never_reach_the_owner(conn, owner, lean):
    assert ops_runbook.cto_id(conn) == lean["sre"]
    assert ops_runbook.cto_id(conn, exclude=lean["sre"]) == lean["ceo"]  # the SRE asking: the CEO decides
    assert taint.security_reviewer(conn)["id"] == lean["sre"]
    assert taint.security_reviewer(conn, exclude=lean["sre"])["id"] == lean["ceo"]
    assert monitor.monitor_id(conn) is None and monitor.platform_owner_id(conn) == lean["sre"]
    # the sentinel's digest with incidents and no Hlídač: the SRE's words in #system, not the owner's
    monitor.ensure_schema(conn)
    conn.execute("INSERT INTO sentinel_incidents (incident_id, service, kind, severity, title, opened_at, llm_skipped) "
                 "VALUES ('i1', 'pos', 'http_5xx', 'high', '5xx', ?, 0)", (monitor._utcnow().isoformat(),))
    assert monitor.digest(conn)["sent"] == "code_only"
    author = conn.execute("SELECT author_id FROM chat_messages ORDER BY id DESC LIMIT 1").fetchone()["author_id"]
    assert author == lean["sre"]


def test_no_coach_means_no_feedback_task_and_no_cto_means_no_platform_loop(conn, owner, lean):
    target = lean["cs"]
    for body in ("Vague subject", "Vague subject again"):
        feedback.give(conn, Ctx(lean["se"], via="mcp"), roles.CUSTOMER_SUCCESS, body, "critique")
    conn.commit()
    assert feedback.coach_digest(conn) == {"agents": 0}
    assert not conn.execute("SELECT 1 FROM tasks WHERE title LIKE 'Zpětná vazba pro%' AND assignee_id IS NOT NULL "
                            "AND assignee_id != ?", (target,)).fetchone()
    from pos import platform_loop
    from pos.improve import loop

    assert platform_loop.start_meeting(conn).get("skipped") and platform_loop.retro(conn).get("skipped")
    assert loop.weekly_triage(conn).get("skipped") and loop.weekly_coach(conn).get("skipped")


def test_weekly_without_a_chief_of_staff_speaks_as_the_ceo(conn, owner, lean):
    assert weekly.agent_id(conn) is None
    assert weekly.voice(conn) == lean["ceo"] != owner.actor_id
    out = weekly.weekly_job(conn)  # no Chief of Staff: the packet is stored, no task for anybody
    assert "skipped" in out
    assert not conn.execute("SELECT 1 FROM tasks WHERE source = 'weekly_report'").fetchone()
    cid = weekly.channel_id(conn)
    members = {r[0] for r in conn.execute("SELECT actor_id FROM channel_members WHERE channel_id = ?", (cid,))}
    assert lean["ceo"] in members
