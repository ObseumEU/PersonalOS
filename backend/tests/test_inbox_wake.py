"""Chat wake-ups (prod 2026-10: 111 "Chat: nepřečtené zprávy" runs a week, $23): an idle agent gets an inbox
task only for a person's message (the owner's always) or a DM/@mention from its own lead or the CEO. Agents'
chatter stays unread and reaches its next real run."""

import pytest

from pos import actors, agents, api_worker, business, chat, integrations, tasks
from pos.core import Ctx
from pos.db import connect, migrate

PERMS = ["tasks:read", "tasks:write", "tasks:claim", "tasks:review", "messages:send"]


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


@pytest.fixture
def team(conn, owner, tmp_path):
    ids = {}
    for key, name, role, lead in (("ceo", "CEO", "ceo", None), ("sre", "SRE", "sre", "ceo"),
                                  ("se", "Software Engineer", "developer", "sre"),
                                  ("peer", "Kniha Developer", "developer", "ceo")):
        aid = agents.create_agent(conn, owner, name=name, purpose=name, lifetime="long_lived", data_dir=tmp_path,
                                  permissions=PERMS)["agent"]["id"]
        conn.execute("UPDATE actors SET role = ?, reports_to = ? WHERE id = ?",
                     (role, ids[lead] if lead else owner.actor_id, aid))
        ids[key] = aid
    conn.commit()
    return ids


def _inbox_task(conn, aid):
    return conn.execute("SELECT * FROM tasks WHERE title = ? AND assignee_id = ? AND archived_at IS NULL",
                        (chat.INBOX_TASK_TITLE, aid)).fetchone()


def _next(conn, aid) -> dict:
    return api_worker._next_work(conn, Ctx(aid, via="worker"))


def test_a_peers_dm_and_group_chatter_do_not_wake_an_idle_agent(conn, owner, team):
    se = team["se"]
    chat.send_dm(conn, Ctx(team["peer"], via="mcp"), se, "Díky, mám to.")
    g = chat.create_channel(conn, owner, "kniha", [team["peer"], se, team["sre"]])
    chat.send(conn, Ctx(team["peer"], via="mcp"), g["id"], "@Software Engineer viděl jsi ten build?")
    conn.commit()
    assert chat.inbox_unread(conn, se, include_info=False) == 2
    out = _next(conn, se)
    assert "task" not in out and _inbox_task(conn, se) is None
    assert out["unread_messages"] == 0 and out["held_messages"] == 2  # the long poll keeps waiting
    # still unread: the next real task's run takes them
    t = tasks.create(conn, owner, {"title": "Opravit build", "status": "next",
                                   "assignee": {"type": "agent", "id": se}})
    out = _next(conn, se)
    assert out["task"]["id"] == t["id"] and out["unread_messages"] == 2
    assert chat.inbox_unread(conn, se, include_info=False) == 2


def test_the_leads_and_the_ceos_dm_or_mention_wake_it(conn, owner, team):
    se, sre, ceo = team["se"], team["sre"], team["ceo"]
    chat.send_dm(conn, Ctx(sre, via="mcp"), se, "Podívej se prosím na T-12.")  # its own lead
    conn.commit()
    assert api_worker.inbox_wakes(conn, se)
    assert _next(conn, se)["task"]["title"] == chat.INBOX_TASK_TITLE
    # the CEO's @mention in a group wakes an agent it does not lead directly
    peer = team["peer"]
    g = chat.create_channel(conn, owner, "vedeni", [ceo, peer])
    chat.send(conn, Ctx(ceo, via="mcp"), g["id"], "@Kniha Developer pošli stav releasu")
    conn.commit()
    assert api_worker.inbox_wakes(conn, peer)
    assert _next(conn, peer)["task"]["title"] == chat.INBOX_TASK_TITLE


def test_the_owners_dm_always_creates_the_inbox_task(conn, owner, team):
    """e7aa86c: the owner's unread message always makes the inbox task (priority 1, under the reserve too)."""
    se = team["se"]
    chat.send_dm(conn, Ctx(team["peer"], via="mcp"), se, "FYI")  # peer chatter alone would wait
    conn.commit()
    assert not api_worker.inbox_wakes(conn, se)
    chat.send_dm(conn, owner, se, "Kdy bude hotový ten build?")
    # no other task delivers it (its "Chat: answer" task, if one was made, is gone): the inbox task does
    conn.execute("UPDATE tasks SET status = 'done' WHERE assignee_id = ? AND title LIKE 'Chat: answer%'", (se,))
    conn.commit()
    assert api_worker.inbox_wakes(conn, se)
    out = _next(conn, se)
    inbox = _inbox_task(conn, se)
    assert inbox is not None and inbox["priority"] == 1 and out["task"]["id"] == inbox["id"]
    assert business.owner_reply_task(conn, inbox)
