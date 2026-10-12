"""The owner's message to the CEO reaches its worker: message -> "Chat: answer" task -> /api/worker/next
(T-834, prod 2026-10-06 05:34: message 2124 waited while the CEO's worker polled for two minutes and found nothing)."""

import pytest

from pos import actors, agents, api_worker, business, chat, integrations
from pos.core import Ctx
from pos.db import connect, migrate


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "c.db")
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


@pytest.fixture
def ceo(conn, owner, tmp_path):
    found = actors.find_by_name(conn, "CEO")
    if found is not None:
        aid = found["id"]
    else:
        aid = agents.create_agent(conn, owner, name="CEO", purpose="CEO", lifetime="long_lived", data_dir=tmp_path,
                                  permissions=["tasks:read", "tasks:write", "tasks:claim", "messages:send"])["agent"]["id"]
    conn.execute("UPDATE actors SET role = 'ceo', team = 'leadership' WHERE id = ?", (aid,))
    conn.commit()
    return Ctx(aid, via="worker")


def _chat_task(conn, ceo):
    return conn.execute("SELECT * FROM tasks WHERE assignee_id = ? AND title LIKE 'Chat: answer %' "
                        "AND archived_at IS NULL", (ceo.actor_id,)).fetchone()


def test_the_owners_message_gives_the_ceo_a_chat_task_its_worker_gets(conn, owner, ceo):
    chat.send_dm(conn, owner, ceo.actor_id, "Proč mi nikdo neodpověděl?")
    conn.commit()
    task = _chat_task(conn, ceo)
    assert task is not None and task["status"] == "next" and task["topic"] == "chat"
    assert api_worker._next_work(conn, ceo)["task"]["id"] == task["id"]


@pytest.mark.parametrize("held", ["retry_after", "inbox"])
def test_a_new_message_lifts_a_hold_on_the_open_chat_task(conn, owner, ceo, held):
    chat.send_dm(conn, owner, ceo.actor_id, "První zpráva")
    conn.commit()
    task = _chat_task(conn, ceo)
    if held == "retry_after":  # its run failed: the 24 h back-off of a failed task
        api_worker.back_off(conn, task["id"])
    else:
        conn.execute("UPDATE tasks SET status = 'inbox' WHERE id = ?", (task["id"],))
    conn.execute("UPDATE chat_inbox SET read_at = '2026-10-06T05:00:00+00:00' WHERE actor_id = ?", (ceo.actor_id,))
    conn.commit()
    assert "task" not in api_worker._next_work(conn, ceo)  # held: the worker finds nothing (the incident)
    chat.send_dm(conn, owner, ceo.actor_id, "Druhá zpráva, pořád čekám")
    conn.commit()
    again = _chat_task(conn, ceo)
    assert again["id"] == task["id"]  # the message joined the open task, no second one
    assert again["retry_after"] is None and again["status"] == "next"
    assert api_worker._next_work(conn, ceo)["task"]["id"] == task["id"]
