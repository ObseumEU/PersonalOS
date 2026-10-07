"""The owner's messages are answered under the business reserve (prod 2026-10-07 20:18: five messages to the CEO and
six to the Software Engineer waited for hours behind platform tasks the reserve held back)."""

import pytest

from pos import actors, agents, api_worker, business, chat, integrations, tasks
from pos.access import service as access
from pos.core import Ctx, now_iso
from pos.db import connect, migrate


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "o.db")
    migrate(c)
    actors.ensure_builtin(c)
    integrations.register_builtin_agents(c)
    business.ensure_schema(c)
    c.commit()
    yield c
    c.close()


@pytest.fixture
def owner(conn):
    return Ctx(actors.owner_id(conn), via="api")


def _agent(conn, owner, tmp_path, name, role, team) -> Ctx:
    found = actors.find_by_name(conn, name)
    perms = ["tasks:read", "tasks:write", "tasks:claim", "tasks:review", "messages:send"]
    if found is not None:
        aid = found["id"]
    else:
        aid = agents.create_agent(conn, owner, name=name, purpose=name, lifetime="long_lived", data_dir=tmp_path,
                                  permissions=perms)["agent"]["id"]
    conn.execute("UPDATE actors SET role = ?, team = ? WHERE id = ?", (role, team, aid))
    conn.commit()
    return Ctx(aid, via="mcp")


@pytest.fixture
def team(conn, owner, tmp_path):
    return {"ceo": _agent(conn, owner, tmp_path, "CEO", "ceo", "leadership"),
            "cto": _agent(conn, owner, tmp_path, "CTO", "cto", "engineering"),
            "se": _agent(conn, owner, tmp_path, "Software Engineer", "developer", "engineering")}


def _reserve(conn, owner, other: Ctx) -> None:
    access.set_budget(conn, owner, None, "usd_day", 50.0, "company cap")
    conn.execute("INSERT INTO engine_usage (at, engine, actor_id, input_tokens, output_tokens, cost_usd) "
                 "VALUES (?, 'claude', ?, 1000, 0, 41.0)", (now_iso(), other.actor_id))
    conn.commit()
    assert access.business_only(conn)


def _platform_task(conn, by: Ctx, to: Ctx, title: str) -> dict:
    return tasks.create(conn, by, {"title": title, "topic": "ops", "priority": 1,
                                   "assignee": {"type": "agent", "id": to.actor_id}})


def _inbox_task(conn, actor: Ctx):
    return conn.execute("SELECT * FROM tasks WHERE title = ? AND assignee_id = ? AND archived_at IS NULL",
                        (chat.INBOX_TASK_TITLE, actor.actor_id)).fetchone()


def test_agent_messages_get_an_inbox_task_even_when_the_reserve_holds_the_queue(conn, owner, team):
    se, cto = team["se"], team["cto"]
    _platform_task(conn, cto, se, "Úklid logů")
    _reserve(conn, owner, team["ceo"])
    chat.send_dm(conn, Ctx(cto.actor_id, via="mcp"), se.actor_id, "Approval #3 approved: run it again now.")
    conn.commit()
    out = api_worker._next_work(conn, Ctx(se.actor_id, via="worker"))
    # the queue is not empty, but the reserve holds it: the inbox task is made all the same
    inbox = _inbox_task(conn, se)
    assert inbox is not None and inbox["status"] == "next"
    # agents' messages alone do not start under the reserve: they wait for it to lift, in that task
    assert "task" not in out and out["state"]["business_only"] and not out["state"].get("owner_reply")


def test_the_owners_message_starts_under_the_reserve(conn, owner, team):
    ceo = team["ceo"]
    _platform_task(conn, ceo, ceo, "Denní přehled firmy")
    _reserve(conn, owner, team["se"])
    chat.send_dm(conn, owner, ceo.actor_id, "notifikace nejdou zapnout, hlásí to chybu na androidu")
    conn.commit()
    assert business.owner_unread(conn, ceo.actor_id) == 1
    out = api_worker._next_work(conn, Ctx(ceo.actor_id, via="worker"))
    # whichever task carries it (his "Chat: answer" task or the inbox task), it starts: business priority
    assert out.get("task") is not None, out["state"]
    served = conn.execute("SELECT * FROM tasks WHERE id = ?", (out["task"]["id"],)).fetchone()
    assert served["topic"] == "chat" and business.owner_reply_task(conn, served)
    assert out["state"]["owner_reply"] and served["priority"] == 1


def test_the_owners_chat_task_is_an_owner_reply_and_the_inbox_task_only_while_he_waits(conn, owner, team):
    ceo, cto = team["ceo"], team["cto"]
    answer = tasks.create(conn, owner, {"title": "Chat: answer David (DM with David)", "topic": "chat",
                                        "status": "next", "assignee": {"type": "agent", "id": ceo.actor_id}})
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (answer["id"],)).fetchone()
    assert business.classify(conn, row) == "platform" and business.owner_reply_task(conn, row)
    # an agent's "Chat: answer" task is not
    other = tasks.create(conn, cto, {"title": "Chat: answer CTO (DM with CTO)", "topic": "chat", "status": "next",
                                     "assignee": {"type": "agent", "id": ceo.actor_id}})
    assert not business.owner_reply_task(conn, conn.execute("SELECT * FROM tasks WHERE id = ?",
                                                            (other["id"],)).fetchone())
    # the inbox task: only while an unread message of the owner is in it
    chat.send_dm(conn, Ctx(cto.actor_id, via="mcp"), ceo.actor_id, "FYI")
    conn.commit()
    tid = chat.ensure_inbox_task(conn, ceo.actor_id)
    inbox = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert inbox["priority"] == 2 and not business.owner_reply_task(conn, inbox)
    chat.send_dm(conn, owner, ceo.actor_id, "zařiď, ať se to už nestane")
    conn.commit()
    assert chat.ensure_inbox_task(conn, ceo.actor_id) == tid
    inbox = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert inbox["priority"] == 1 and business.owner_reply_task(conn, inbox)


def test_owner_replies_under_the_reserve_have_a_hard_daily_budget(conn, owner, team):
    ceo = team["ceo"]
    _reserve(conn, owner, team["se"])
    answer = tasks.create(conn, owner, {"title": "Chat: answer David (DM with David)", "topic": "chat",
                                        "status": "next", "assignee": {"type": "agent", "id": ceo.actor_id}})
    w = Ctx(ceo.actor_id, via="worker")
    assert api_worker._next_work(conn, w)["task"]["id"] == answer["id"]
    # a run started on it under the reserve counts against the owner-reply budget
    run = conn.execute("INSERT INTO runs (actor_id, kind, status, started_at, task_id) VALUES (?, 'task', 'ok', ?, ?)",
                       (ceo.actor_id, now_iso(), answer["id"])).lastrowid
    conn.execute("UPDATE runs SET cost_usd = 0.4 WHERE id = ?", (run,))
    api_worker._count_owner_reply(conn, w, answer["id"], run)
    got = business.owner_reply_budget(conn)
    assert got["runs"] == 1 and got["usd"] == pytest.approx(0.4) and got["left"]
    for _ in range(business.OWNER_REPLY_RUNS_DAY - 1):
        business.record_owner_reply_run(conn, w, answer["id"], run)
    conn.commit()
    assert not business.owner_reply_budget(conn)["left"]
    out = api_worker._next_work(conn, w)
    assert "task" not in out and out["state"]["business_only"]
    # outside the reserve nothing is counted
    conn.execute("DELETE FROM engine_usage")
    conn.execute("DELETE FROM audit_log WHERE action = ?", (business.OWNER_REPLY_AUDIT,))
    conn.commit()
    api_worker._count_owner_reply(conn, w, answer["id"], run)
    assert business.owner_reply_budget(conn)["runs"] == 0
