"""The one-off production fix of 2026-09-27 (pos.prodfix_ops): reviews, archived members' grants
and tasks, resolved incidents with open tickets, answered chats."""

from fastapi.testclient import TestClient

from pos import actors, agents, monitor, prodfix_ops, tasks
from pos.config import Settings
from pos.core import Ctx
from pos.db import connect
from pos.main import create_app


def test_prodfix_cleans_up_what_the_bugs_left_and_is_idempotent(tmp_path):
    settings = Settings(data_dir=tmp_path, password="pw", session_secret="t" * 32, scheduler=False)
    client = TestClient(create_app(settings))
    client.__enter__()
    conn = connect(settings.db_path)
    owner = Ctx(actors.owner_id(conn))
    mk = lambda name, perms: agents.create_agent(conn, owner, name=name, purpose=name, lifetime="long_lived",  # noqa
                                                 permissions=perms, data_dir=tmp_path)["agent"]["id"]
    se = mk("Software Engineer", ["tasks:read", "tasks:claim", "tasks:review"])
    dev = mk("Dev agent", ["tasks:read", "tasks:claim", "tasks:review"])
    qa = mk("QA Reviewer", ["tasks:read", "tasks:claim", "tasks:review"])
    writer = mk("Writer", ["tasks:read", "tasks:claim"])
    mk(monitor.NAME, ["tasks:read", "tasks:claim", "tasks:review", "ops:monitor"])
    # the old bugs: an archive that left grants and work, a raw reviewer move nobody heard of
    open_ = tasks.create(conn, owner, {"title": "Fix TZ", "notes": "x", "definition_of_done": "y",
                                       "assignee": {"type": "agent", "id": writer}, "status": "next"})
    held = tasks.create(conn, owner, {"title": "Held review", "notes": "x", "definition_of_done": "y",
                                      "assignee": {"type": "agent", "id": writer}, "status": "next"})
    silent = tasks.create(conn, owner, {"title": "Silent review", "notes": "x", "definition_of_done": "y",
                                        "assignee": {"type": "agent", "id": writer}, "status": "next"})
    conn.execute("UPDATE tasks SET status = 'review', reviewer_id = ? WHERE id = ?", (dev, held["id"]))
    conn.execute("UPDATE tasks SET status = 'review', reviewer_id = ? WHERE id = ?", (qa, silent["id"]))
    conn.execute("UPDATE tasks SET assignee_id = ?, assignee_name = 'Dev agent' WHERE id = ?", (dev, open_["id"]))
    conn.execute("UPDATE actors SET archived_at = '2026-09-26T10:00:00+00:00' WHERE id = ?", (dev,))
    # a resolved incident whose owner ticket stayed open
    from pos import asks

    ticket = asks.ask(conn, Ctx(actors.assistant_id(conn)), title="Incident: web down", why="over the cap",
                      blocking=False)
    monitor.ensure_schema(conn)
    conn.execute("""INSERT INTO sentinel_incidents (incident_id, service, kind, severity, key, title, ticket_id,
                    opened_at, resolved_at) VALUES ('x1', 'personalos', 'health', 'high', 'web', 'down', ?,
                    '2026-09-26T10:00:00+00:00', '2026-09-26T10:20:00+00:00')""", (ticket["ticket_id"],))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM access_grants WHERE agent_id = ? AND ended_at IS NULL",
                        (dev,)).fetchone()[0]

    dry = prodfix_ops.run(conn, apply=False)
    assert dry["grants"] and dry["tasks"] and dry["incidents"] and len(dry["reviews"]) == 2
    report = prodfix_ops.run(conn, apply=True)
    assert tasks.get(conn, owner, open_["id"])["assignee_id"] == se                      # Dev agent -> successor
    assert tasks.get(conn, owner, held["id"])["reviewer_id"] == se
    assert not conn.execute("SELECT 1 FROM access_grants WHERE agent_id = ? AND ended_at IS NULL", (dev,)).fetchone()
    assert tasks.get(conn, owner, ticket["ticket_id"])["status"] == "done"
    assert conn.execute("SELECT status FROM owner_asks WHERE ticket_id = ?", (ticket["ticket_id"],)).fetchone()[0] \
        == "answered"
    inbox = lambda a: [r[0] for r in conn.execute(  # noqa: E731
        "SELECT m.body FROM chat_inbox i JOIN chat_messages m ON m.id = i.message_id WHERE i.actor_id = ?", (a,))]
    assert any(silent["ref"] in b and "review" in b for b in inbox(qa))                 # told again
    assert any(held["ref"] in b and "review" in b for b in inbox(se))
    assert report["reviews"]
    again = prodfix_ops.run(conn, apply=True)
    assert not any(again[k] for k in ("grants", "tasks", "incidents", "chat"))
    conn.close()
    client.__exit__(None, None, None)
