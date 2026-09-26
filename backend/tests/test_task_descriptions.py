"""Every task says what it is for, where it came from and what done looks like."""

import json

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp.client import Client

from pos import actors, agents, integrations, mcp_server, org, routing, schedules, task_descriptions, tasks
from pos.config import Settings
from pos.core import Ctx, now_iso
from pos.db import connect, migrate
from pos.main import create_app


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "d.db"
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


@pytest.fixture
def me(conn):
    return Ctx(actors.owner_id(conn))


def _generated(t):
    assert t["description_generated"] == 1
    for part in ("Where from:", "Who:", "Done when:", task_descriptions.GENERATED_FOOTER):
        assert part in t["notes"], part


def test_create_without_notes_gets_a_generated_description(conn, me):
    t = tasks.create(conn, me, {"title": "Pay the Acme invoice", "topic": "Finance", "assignee": "ai"})
    _generated(t)
    assert "“Pay the Acme invoice”" in t["notes"] and "#finance" in t["notes"]
    assert "the owner" in t["notes"] and "AI assistant" in t["notes"]
    # Deterministic: the same fields give the same text.
    again = task_descriptions.build(conn, {**t, "notes": ""}, t["id"])
    assert again == t["notes"]


def test_written_notes_are_kept_and_not_flagged(conn, me):
    t = tasks.create(conn, me, {"title": "Call the bank", "notes": "About the mortgage offer; done when booked."})
    assert t["notes"] == "About the mortgage offer; done when booked." and t["description_generated"] == 0
    t = tasks.capture(conn, me, "Buy milk tomorrow")
    _generated(t)
    assert "Where from:** created in the web UI / REST API, by the owner" in t["notes"]


def test_editing_clears_the_flag_and_clearing_regenerates(conn, me):
    t = tasks.create(conn, me, {"title": "Plan the offsite", "definition_of_done": "Venue booked"})
    assert "Done when:** Venue booked" in t["notes"]
    t = tasks.update(conn, me, t["id"], {"notes": "For the Q4 team meeting in Brno."})
    assert t["description_generated"] == 0
    t = tasks.update(conn, me, t["id"], {"notes": "  "})
    _generated(t)


def test_steps_name_their_project(conn, me):
    p = tasks.create(conn, me, {"title": "Launch the site", "notes": "The new portfolio."})
    s = tasks.create(conn, me, {"title": "Write the about page", "parent_id": p["ref"]})
    _generated(s)
    assert f"step of project {p['ref']} “Launch the site”" in s["notes"]
    assert f"Then project {p['ref']} can move on." in s["notes"]


def test_api_and_mcp_create_fill_the_description(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path, mcp_token="k"))) as client:
        r = client.post("/api/tasks", json={"title": "Renew the domain"})
        assert r.status_code == 201
        _generated(r.json())
        ref = r.json()["ref"]
        t = client.patch(f"/api/tasks/{ref}", json={"notes": "Expires on 1.10."}).json()
        assert t["notes"] == "Expires on 1.10." and t["description_generated"] == 0
        listed = client.get("/api/tasks?view=inbox").json()
        assert any(x["ref"] == ref and x["notes"] == "Expires on 1.10." for x in listed)

    db = tmp_path / "personalos.db"
    server = mcp_server.build(db, default_actor=lambda c: actors.owner_id(c))

    async def scenario():
        async with Client(server) as cl:
            out = await cl.call_tool("create_task", {"title": "Check the backups", "assignee": "me",
                                                     "status": "next"})
            assert not out.is_error, out.content
            return out.structured_content or json.loads(out.content[0].text)

    out = anyio.run(scenario)
    ref = (out.get("result") or out)["ref"]
    c = connect(db)
    try:
        t = tasks.get(c, Ctx(actors.owner_id(c)), tasks.parse_id(ref))
        _generated(t)
        assert "created over MCP" in t["notes"]
    finally:
        c.close()


def test_schedule_firings_say_which_schedule(conn, me):
    bare = schedules.create(conn, me, {"name": "Water the plants", "schedule": "daily 07:00"})
    out = schedules.fire(conn, bare["id"])
    t = tasks.get(conn, me, tasks.parse_id(out["task"]))
    _generated(t)
    assert "the schedule “Water the plants” (daily 07:00)" in t["notes"]

    written = schedules.create(conn, me, {"name": "Invoices", "schedule": "weekly fri 15:00",
                                          "notes": "Send this week's invoices."})
    t = tasks.get(conn, me, tasks.parse_id(schedules.fire(conn, written["id"])["task"]))
    assert t["notes"].startswith("Send this week's invoices.") and "Source: the schedule “Invoices”" in t["notes"]
    assert t["description_generated"] == 0


def test_events_carry_purpose_source_and_done(conn, me):
    out = routing.ingest(conn, me, {"source": "gmail", "title": "Lunch?", "author": "jan@acme.cz"})
    t = tasks.get(conn, me, out["task_id"])
    assert t["notes"].startswith("Purpose: an incoming gmail item") and "jan@acme.cz" in t["notes"]
    assert t["definition_of_done"] and t["description_generated"] == 0


def test_handoff_fills_an_empty_description(conn, me, tmp_path):
    dev = agents.create_agent(conn, me, name="Software Engineer", purpose="code", lifetime="long_lived",
                              data_dir=tmp_path, permissions=["tasks:read", "tasks:claim"])["agent"]["id"]
    agents.create_agent(conn, me, name="Head of Customer Success", purpose="mail", lifetime="long_lived",
                        data_dir=tmp_path, permissions=["tasks:read", "tasks:claim"])
    t = tasks.create(conn, me, {"title": "Reply to the invoice", "assignee": {"type": "agent", "id": dev}})
    conn.execute("UPDATE tasks SET notes = '', description_generated = 0 WHERE id = ?", (t["id"],))  # legacy row
    org.handoff(conn, Ctx(dev), t["id"], "Head of Customer Success", "This is e-mail, not code")
    after = tasks.get(conn, me, t["id"])
    _generated(after)
    assert "Handed off by Software Engineer to Head of Customer Success: This is e-mail, not code" in after["notes"]
    assert "the agent Head of Customer Success" in after["notes"]

    written = tasks.create(conn, me, {"title": "Fix the build", "notes": "CI is red since Monday.",
                                      "assignee": {"type": "agent", "id": dev}})
    org.handoff(conn, Ctx(dev), written["id"], "Head of Customer Success")
    assert tasks.get(conn, me, written["id"])["notes"] == "CI is red since Monday."


def test_backfill_is_idempotent_and_keeps_updated_at(db, conn, me):
    owner = actors.owner_id(conn)
    old = "2026-01-02T03:04:05+00:00"
    for title, source in (("Old capture", "ui"), ("Old mail", "event:gmail")):
        conn.execute("INSERT INTO tasks (title, notes, owner_id, created_by, source, created_at, updated_at) "
                     "VALUES (?, '', ?, ?, ?, ?, ?)", (title, owner, owner, source, old, old))
    kept = tasks.create(conn, me, {"title": "Has notes", "notes": "Real context."})
    mail_id = conn.execute("SELECT id FROM tasks WHERE title = 'Old mail'").fetchone()[0]
    conn.execute("INSERT INTO events (source, kind, ref, title, payload, task_id, received_by, received_at) "
                 "VALUES ('gmail', 'message', 'm1', 'Invoice 42', '{}', ?, ?, ?)", (mail_id, owner, now_iso()))
    conn.commit()

    dry = task_descriptions.backfill(conn, apply=False)
    assert dry["would_fill"] == 2 and dry["filled"] == 0
    assert conn.execute("SELECT COUNT(*) FROM tasks WHERE notes = ''").fetchone()[0] == 2

    assert task_descriptions.main(["backfill", "--db", str(db)]) == 0
    rows = {r["title"]: r for r in conn.execute("SELECT * FROM tasks")}
    for title in ("Old capture", "Old mail"):
        assert rows[title]["description_generated"] == 1 and rows[title]["updated_at"] == old
        assert "Done when:" in rows[title]["notes"] and "on 2026-01-02" in rows[title]["notes"]
    assert "Linked event: gmail message “Invoice 42”" in rows["Old mail"]["notes"]
    assert rows["Has notes"]["notes"] == "Real context." and rows["Has notes"]["description_generated"] == 0
    assert tasks.get(conn, me, kept["id"])["notes"] == "Real context."
    hist = conn.execute("SELECT action FROM history WHERE entity = 'task' AND entity_id = ? ORDER BY version",
                        (mail_id,)).fetchall()
    assert [h[0] for h in hist] == ["describe"]

    again = task_descriptions.backfill(conn)
    assert again["checked"] == 0 and again["filled"] == 0
