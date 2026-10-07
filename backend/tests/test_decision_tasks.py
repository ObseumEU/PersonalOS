"""Approval tasks are resolved only by their decision, and the task waiting on them wakes at once (prod 2026-10-07:
T-397 stayed parked after T-1014..T-1016 were approved; the COO returned approval tasks for a missing "Ověřeno"
line and the CTO's complete_task on them was refused 4x)."""

import pytest

from pos import (actors, agents, api_worker, approvals, asks, business, command_policy, decision_tasks,
                 integrations, tasks)
from pos.core import Ctx, now_iso
from pos.db import connect, migrate


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "d.db")
    migrate(c)
    actors.ensure_builtin(c)
    integrations.register_builtin_agents(c)
    business.ensure_schema(c)
    command_policy.ensure_schema(c)
    c.commit()
    yield c
    c.close()


@pytest.fixture
def owner(conn):
    return Ctx(actors.owner_id(conn), via="api")


def _agent(conn, owner, tmp_path, name, role, lead=None) -> Ctx:
    found = actors.find_by_name(conn, name)
    perms = ["tasks:read", "tasks:write", "tasks:claim", "tasks:review", "messages:send", "approvals:request"]
    if found is not None:
        aid = found["id"]
        agents.set_permissions(conn, owner, aid, perms)
    else:
        aid = agents.create_agent(conn, owner, name=name, purpose=name, lifetime="long_lived", data_dir=tmp_path,
                                  permissions=perms)["agent"]["id"]
    conn.execute("UPDATE actors SET role = ?, reports_to = ?, team = 'engineering' WHERE id = ?",
                 (role, lead.actor_id if lead else None, aid))
    conn.commit()
    return Ctx(aid, via="mcp")


@pytest.fixture
def team(conn, owner, tmp_path):
    ceo = _agent(conn, owner, tmp_path, "CEO", "ceo")
    coo = _agent(conn, owner, tmp_path, "COO", "project_manager", ceo)
    cto = _agent(conn, owner, tmp_path, "CTO", "cto", ceo)
    se = _agent(conn, owner, tmp_path, "Software Engineer", "developer", cto)
    return {"ceo": ceo, "coo": coo, "cto": cto, "se": se}


def _parked(conn, owner, se: Ctx) -> tuple[dict, int]:
    """T-397: the customer's fix, parked waiting with a follow-up date and a back-off, and the run that asked."""
    t = tasks.create(conn, owner, {"title": "Zákaznický problém: ChatPulse u O2", "source": "support:issue",
                                   "value_kind": "business", "assignee": {"type": "agent", "id": se.actor_id}})
    run = conn.execute("INSERT INTO runs (actor_id, task_id, kind, status, started_at) VALUES (?, ?, 'task', "
                       "'running', ?)", (se.actor_id, t["id"], now_iso())).lastrowid
    return t, run


def _park(conn, task_id: int) -> None:
    conn.execute("UPDATE tasks SET status = 'waiting', follow_up = '2099-01-01', retry_after = '2099-01-01T00:00:00+00:00'"
                 " WHERE id = ?", (task_id,))
    conn.commit()


def _status(conn, task_id: int):
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


def test_the_task_waiting_on_command_approvals_wakes_when_the_last_one_is_decided(conn, owner, team):
    se, cto = team["se"], team["cto"]
    t, run = _parked(conn, owner, se)
    rctx = Ctx(se.actor_id, via="worker", run_id=run)
    a1 = command_policy.request(conn, rctx, "git -C /work/x apply p.patch", why="apply")
    a2 = command_policy.request(conn, rctx, "git -C /work/x commit -a -F m.txt", why="commit")
    _park(conn, t["id"])
    command_policy.decide(conn, Ctx(cto.actor_id, via="mcp"), a1["approval"], True, "ok")
    assert _status(conn, t["id"])["status"] == "waiting"  # one more decision is still open
    command_policy.decide(conn, Ctx(cto.actor_id, via="mcp"), a2["approval"], False, "ne, bez push")
    row = _status(conn, t["id"])
    assert row["status"] == "next" and row["retry_after"] is None  # at once, no back-off
    # the approval tasks are done by the decision itself: no hand-in, no review
    for a in (a1, a2):
        done = _status(conn, tasks.parse_id(a["task"]))
        assert done["status"] == "done" and done["reviewer_id"] is None
    conn.execute("UPDATE runs SET status = 'ok'")  # the run that asked ended long ago
    assert api_worker._next_work(conn, Ctx(se.actor_id, via="worker"))["task"]["id"] == t["id"]


def test_an_approval_without_its_task_wakes_the_task_of_the_run_that_asked(conn, owner, team):
    se, cto = team["se"], team["cto"]
    t, run = _parked(conn, owner, se)
    a = command_policy.request(conn, Ctx(se.actor_id, via="worker", run_id=run), "git -C /work/x apply p", why="x")
    conn.execute("UPDATE tasks SET on_behalf_of = NULL WHERE id = ?", (tasks.parse_id(a["task"]),))  # an old one
    _park(conn, t["id"])
    command_policy.decide(conn, Ctx(cto.actor_id, via="mcp"), a["approval"], True, "ok")
    assert _status(conn, t["id"])["status"] == "next"


def test_an_owner_approval_and_an_owner_answer_wake_the_waiting_task(conn, owner, team):
    se = team["se"]
    t, _ = _parked(conn, owner, se)
    appr = approvals.request(conn, Ctx(se.actor_id, via="mcp"), "email.send", {"why": "odpověď zákazníkovi"},
                             task_id=t["id"], ping=False)
    _park(conn, t["id"])
    approvals.decide(conn, owner, appr["id"], True)
    assert _status(conn, t["id"])["status"] == "next" and _status(conn, t["id"])["retry_after"] is None
    # a non-blocking ask the agent then parked its task for: the owner's answer wakes it too
    _park(conn, t["id"])
    asks._resume(conn, owner, {"source_task_id": t["id"], "blocking": 0}, "David resolved T-1")
    assert _status(conn, t["id"])["status"] == "next"


def test_approval_tasks_are_not_completed_or_reviewed_by_agents(conn, owner, team):
    se, cto, coo = team["se"], team["cto"], team["coo"]
    t, run = _parked(conn, owner, se)
    a = command_policy.request(conn, Ctx(se.actor_id, via="worker", run_id=run), "git -C /work/x apply p", why="x")
    tid = tasks.parse_id(a["task"])
    # pending: complete_task is refused with the call that decides it (no review, no "Ověřeno" line)
    with pytest.raises(tasks.Invalid, match="command_approval_decide"):
        tasks.complete(conn, Ctx(cto.actor_id, via="mcp"), tid, "Schváleno.")
    with pytest.raises(tasks.Invalid, match="command_approval_decide"):
        tasks.update(conn, Ctx(cto.actor_id, via="mcp"), tid, {"status": "review"})
    assert _status(conn, tid)["status"] == "next"
    # the run that decides it is told how: the decision closes it
    conn.execute("UPDATE tasks SET status = 'next' WHERE id = ?", (t["id"],))
    served = api_worker._next_work(conn, Ctx(cto.actor_id, via="worker"))["task"]
    assert served["id"] == tid and "command_approval_decide(approval=" in served["definition_of_done"]
    command_policy.decide(conn, Ctx(cto.actor_id, via="mcp"), a["approval"], True, "ok")
    # decided: the CTO's complete_task afterwards is a no-op, not a refusal; the COO cannot return it
    assert tasks.complete(conn, Ctx(cto.actor_id, via="mcp"), tid, "Hotovo")["status"] == "done"
    assert tasks.review(conn, Ctx(coo.actor_id, via="mcp"), tid, False, "Chybí Ověřeno")["status"] == "done"
    row = _status(conn, tid)
    assert row["status"] == "done" and row["returned_count"] == 0


def test_a_legacy_approval_task_in_review_is_closed_by_its_decision_not_returned(conn, owner, team):
    se, cto, coo = team["se"], team["cto"], team["coo"]
    t, run = _parked(conn, owner, se)
    a = command_policy.request(conn, Ctx(se.actor_id, via="worker", run_id=run), "git -C /work/x apply p", why="x")
    tid = tasks.parse_id(a["task"])
    conn.execute("UPDATE command_approvals SET status = 'approved', decision = 'ok' WHERE id = ?", (a["approval"],))
    conn.execute("UPDATE tasks SET status = 'review', reviewer_id = ? WHERE id = ?", (coo.actor_id, tid))
    conn.commit()
    out = tasks.review(conn, Ctx(coo.actor_id, via="mcp"), tid, False, "Chybí řádek Ověřeno")
    assert out["status"] == "done" and _status(conn, tid)["returned_count"] == 0
    # an approval decided elsewhere is never offered as work: closed by the picker
    b = command_policy.request(conn, Ctx(se.actor_id, via="worker", run_id=run), "git -C /work/x log", why="y")
    conn.execute("UPDATE command_approvals SET status = 'approved' WHERE id = ?", (b["approval"],))
    conn.commit()
    assert api_worker._next_work(conn, Ctx(cto.actor_id, via="worker")).get("task") is None
    assert _status(conn, tasks.parse_id(b["task"]))["status"] == "done"
    assert decision_tasks.is_decision(conn, tid)
