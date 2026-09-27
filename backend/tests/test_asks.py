import json

import anyio
import pytest

from pos import actors, agents, approvals, asks, chat, comments, mcp_server, tasks
from pos.core import Ctx
from pos.db import connect, migrate


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "c.db")
    migrate(c)
    actors.ensure_builtin(c)
    c.execute("UPDATE actors SET name = 'David' WHERE is_owner = 1")
    c.commit()
    yield c
    c.close()


def _agent_task(conn):
    me = Ctx(actors.owner_id(conn))
    ai = Ctx(actors.assistant_id(conn), via="mcp")
    t = tasks.create(conn, me, {"title": "Pick an invoice template", "assignee": "ai", "status": "next"})
    tasks.claim(conn, ai, t["id"])
    return me, ai, t


def _team_messages(conn):
    cid = chat.ensure_team_channel(conn)
    return [r["body"] for r in conn.execute("SELECT body FROM chat_messages WHERE channel_id = ? ORDER BY id", (cid,))]


def test_ask_owner_opens_a_ticket_pings_in_czech_and_parks_the_task(conn):
    me, ai, t = _agent_task(conn)
    out = asks.ask(conn, ai, title="Choose the invoice template", why="The client wants the invoice today.",
                   details="Two templates exist.", options=["Classic", "Minimal"], recommendation="Minimal",
                   task_id=t["id"], kind="decision", links=["https://example.com/templates"])
    assert not out["deduped"]
    ticket = tasks.get(conn, me, out["ticket_id"])
    assert ticket["assignee_id"] == actors.owner_id(conn) and ticket["status"] == "next"
    assert ticket["priority"] == 1 and ticket["definition_of_done"]
    for part in ("### Proč", "### Možnosti", "Minimal — *doporučuju*", "### Až odpovíš", t["ref"],
                 "https://example.com/templates"):
        assert part in ticket["notes"]
    # the ping: Czech, the ticket ref, the reason, an @mention of the owner
    msg = _team_messages(conn)[-1]
    assert msg.startswith(f"Davide, tady je ticket {out['ref']}: Choose the invoice template. Prosím rozhodni")
    assert "protože The client wants the invoice today." in msg and "@David" in msg
    assert chat.inbox_unread(conn, actors.owner_id(conn)) >= 1
    # the agent's task waits and links to the ticket
    src = tasks.get(conn, me, t["id"])
    assert src["status"] == "waiting"
    assert any(out["ref"] in c["body"] for c in comments.list_for(conn, me, t["id"]))


def test_same_ask_is_not_sent_twice(conn):
    _, ai, t = _agent_task(conn)
    first = asks.ask(conn, ai, title="Choose the invoice template", why="Needed today.", task_id=t["id"])
    n = len(_team_messages(conn))
    again = asks.ask(conn, ai, title="Choose  the invoice template!", why="Still needed.", task_id=t["id"])
    assert again["deduped"] and again["ref"] == first["ref"]
    assert len(_team_messages(conn)) == n
    other = asks.ask(conn, ai, title="Confirm the due date", why="The contract says 14 days.", task_id=t["id"],
                     kind="confirmation", blocking=False)
    assert not other["deduped"] and other["ref"] != first["ref"]


def test_owner_comment_and_resolution_reach_the_asker_and_resume(conn):
    me, ai, t = _agent_task(conn)
    out = asks.ask(conn, ai, title="Choose the invoice template", why="Needed today.", task_id=t["id"])
    before = chat.inbox_unread(conn, ai.actor_id)
    comments.add(conn, me, out["ticket_id"], "Go with **Minimal**.")
    assert chat.inbox_unread(conn, ai.actor_id) == before + 1
    assert tasks.get(conn, me, t["id"])["status"] == "next"  # back in the agent's queue
    tasks.complete(conn, me, out["ticket_id"], "Minimal it is")
    assert chat.inbox_unread(conn, ai.actor_id) == before + 2
    row = conn.execute("SELECT status FROM owner_asks WHERE ticket_id = ?", (out["ticket_id"],)).fetchone()
    assert row["status"] == "answered"
    # asking again after the answer returns the answered ticket
    again = asks.ask(conn, ai, title="Choose the invoice template", why="x", task_id=t["id"])
    assert again["deduped"] and again["ask_status"] == "answered"


def test_approvals_ping_the_owner_and_tell_the_requester(conn):
    me, ai, t = _agent_task(conn)
    a = approvals.request(conn, ai, "email.send", {"why": "The client asked for the offer."}, t["id"])
    msg = _team_messages(conn)[-1]
    assert f"schválení #{a['id']}" in msg and "@David" in msg and t["ref"] in msg
    before = chat.inbox_unread(conn, ai.actor_id)
    approvals.decide(conn, me, a["id"], False, "not yet")
    assert chat.inbox_unread(conn, ai.actor_id) == before + 1


def test_vocative():
    assert asks.vocative("David") == "Davide"
    assert asks.vocative("Petr Novák") == "Petře"
    assert asks.vocative("Jana") == "Jano"
    assert asks.vocative("Owner") == "Ahoj"


def test_ask_owner_over_mcp(tmp_path):
    db = tmp_path / "m.db"
    c = connect(db)
    migrate(c)
    actors.ensure_builtin(c)
    agents.seed_builtin_permissions(c)
    ai = actors.assistant_id(c)
    t = tasks.create(c, Ctx(actors.owner_id(c)), {"title": "Plan the offsite", "assignee": "ai", "status": "next"})
    c.commit()
    c.close()
    server = mcp_server.build(db, default_actor=lambda conn: ai)
    assert "ask_owner" in mcp_server.tool_names()

    async def scenario():
        from mcp.client import Client

        async with Client(server) as cl:
            res = await cl.call_tool("ask_owner", {"title": "Confirm the budget", "why": "The venue needs a deposit.",
                                                   "task_id": t["ref"], "kind": "confirmation",
                                                   "options": ["20k", "30k"], "recommendation": "20k"})
            assert not res.is_error, res.content
            return res.structured_content or json.loads(res.content[0].text)

    out = anyio.run(scenario)
    assert out["ref"].startswith("T-") and not out["deduped"]


def test_agent_detail_pending_gates_only_when_something_waits_on_the_owner(conn):
    me, ai, t = _agent_task(conn)
    assert agents.detail(conn, ai.actor_id)["pending_gates"] == []
    a = approvals.request(conn, ai, "email.send", {"subject": "Offer for ACME"}, t["id"])
    gates = agents.detail(conn, ai.actor_id)["pending_gates"]
    assert gates == [{"kind": "approval", "id": a["id"], "title": "email.send · Offer for ACME", "link": "/approvals"}]
    approvals.decide(conn, me, a["id"], True)
    assert agents.detail(conn, ai.actor_id)["pending_gates"] == []
    # a non-blocking ask is not a gate, a blocking one is
    asks.ask(conn, ai, title="Confirm the due date", why="x", task_id=t["id"], blocking=False)
    assert agents.detail(conn, ai.actor_id)["pending_gates"] == []
    out = asks.ask(conn, ai, title="Choose the invoice template", why="Needed today.", task_id=t["id"])
    gates = agents.detail(conn, ai.actor_id)["pending_gates"]
    assert [(g["kind"], g["ref"], g["link"]) for g in gates] == [("ask", out["ref"], f"/tasks?task={out['ref']}")]
    tasks.complete(conn, me, out["ticket_id"], "Minimal")
    assert agents.detail(conn, ai.actor_id)["pending_gates"] == []
