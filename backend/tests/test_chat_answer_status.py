"""Where the answer to the owner's message stands (pos.answer_status): the line under his message and the
typing indicator while it is queued, held, being written, or nobody took it ("Připomenout"); and the agents'
chat_edit. Prod 2026-10-13: the owner saw "nikdo nepíše" while T-1398 (the CEO's answer) was working, and did
not know whether anyone took his #kniha message."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from pos import actors, agents, agents_code, answer_status, chat
from pos.config import Settings
from pos.core import Ctx, Forbidden
from pos.db import connect
from pos.main import create_app


@pytest.fixture(autouse=True)
def clean():
    chat._typing.clear()
    answer_status.invalidate()
    yield
    chat._typing.clear()
    answer_status.invalidate()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("POS_CODEX_DISABLED", "1")
    monkeypatch.setenv("POS_AGENT_RUNTIME", "codex")
    settings = Settings(data_dir=tmp_path)
    client = TestClient(create_app(settings))
    client.__enter__()
    conn = connect(settings.db_path)
    owner = Ctx(actors.owner_id(conn))
    out = agents.create_agent(conn, owner, name="Hlídač", purpose="watch", lifetime="long_lived",
                              permissions=["tasks:read", "tasks:claim", "messages:send"], data_dir=tmp_path)
    monkeypatch.setattr(agents_code, "answers_chat", lambda name: name == "Hlídač")
    yield client, conn, owner, out["agent"]["id"], {"Authorization": f"Bearer {out['api_key']}"}
    conn.close()
    client.__exit__(None, None, None)


def _ask(conn, owner, agent_id, text="Jak to vypadá s naší aplikací na tvorbu knihy?"):
    msg = chat.send_dm(conn, owner, agent_id, text)
    task = conn.execute("SELECT * FROM tasks WHERE assignee_id = ? AND topic = 'chat' ORDER BY id DESC",
                        (agent_id,)).fetchone()
    return msg, task


def _later(minutes=3):
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


def test_queued_answer_shows_under_the_message_and_as_typing(setup):
    client, conn, owner, agent_id, h = setup
    msg, task = _ask(conn, owner, agent_id)
    st = client.get("/api/chat/answers").json()[str(msg["channel_id"])]
    assert st["message_id"] == msg["id"] and st["state"] == "queued"
    assert st["agent"] == "Hlídač" and st["task"] == f"T-{task['id']}"
    # The stream's presence: the agent "types" (queued) in that conversation, never elsewhere.
    p = chat.presence_view(conn, owner.actor_id, cached=False)
    assert p["typing"][str(msg["channel_id"])] == [
        {"id": agent_id, "name": "Hlídač", "kind": "agent", "state": "queued", "thread": None, "until": None}]
    assert p["answers"][str(msg["channel_id"])]["state"] == "queued"
    assert set(p["typing"]) == {str(msg["channel_id"])}


def test_working_run_then_answer(setup):
    client, conn, owner, agent_id, h = setup
    msg, task = _ask(conn, owner, agent_id)
    run = client.post("/api/worker/runs", json={"task_id": f"T-{task['id']}"}, headers=h)
    assert run.status_code == 201, run.text
    assert answer_status.compute(conn)[msg["channel_id"]]["state"] == "working"
    typing = chat.presence_view(conn, owner.actor_id, cached=False)["typing"][str(msg["channel_id"])]
    assert [e["state"] for e in typing] == ["typing"]  # the live entry wins, no duplicate
    chat.send(conn, Ctx(agent_id), msg["channel_id"], "Aplikace běží.", reply_to=msg["id"])
    client.post(f"/api/worker/runs/{run.json()['run_id']}/finish", json={"status": "ok"}, headers=h)
    conn.execute("UPDATE tasks SET status = 'done', updated_at = ? WHERE id = ?",
                 (datetime.now(timezone.utc).isoformat(timespec="seconds"), task["id"]))
    conn.commit()
    st = answer_status.compute(conn)[msg["channel_id"]]
    assert st["state"] == "answered" and st["agent"] == "Hlídač"
    assert chat.presence_view(conn, owner.actor_id, cached=False)["typing"] == {}
    # Ten minutes later the line is gone.
    assert msg["channel_id"] not in answer_status.compute(conn, _later(45))


def test_held_for_budget_says_until_when(setup, monkeypatch):
    client, conn, owner, agent_id, h = setup
    from pos.access import service as access

    monkeypatch.setattr(access, "limited_until", lambda c, aid, now=None: "2026-10-13T14:05:00+00:00")
    msg, task = _ask(conn, owner, agent_id)
    st = answer_status.compute(conn)[msg["channel_id"]]
    assert st["state"] == "held" and st["until"] == "2026-10-13T14:05:00+00:00"
    typing = chat.presence_view(conn, owner.actor_id, cached=False)["typing"][str(msg["channel_id"])]
    assert typing[0]["state"] == "held"


def test_unclaimed_after_two_minutes_and_remind_requeues(setup):
    client, conn, owner, agent_id, h = setup
    msg, task = _ask(conn, owner, agent_id)
    conn.execute("UPDATE tasks SET status = 'inbox', retry_after = ? WHERE id = ?",
                 ("2099-01-01T00:00:00+00:00", task["id"]))
    conn.commit()
    assert answer_status.compute(conn)[msg["channel_id"]]["state"] == "queued"
    conn.execute("UPDATE tasks SET retry_after = NULL WHERE id = ?", (task["id"],))
    conn.commit()
    assert answer_status.compute(conn, _later(3))[msg["channel_id"]]["state"] == "unclaimed"
    conn.execute("UPDATE tasks SET retry_after = ? WHERE id = ?", ("2099-01-01T00:00:00+00:00", task["id"]))
    conn.commit()
    r = client.post(f"/api/chat/messages/{msg['id']}/remind")
    assert r.status_code == 200, r.text
    row = conn.execute("SELECT status, retry_after FROM tasks WHERE id = ?", (task["id"],)).fetchone()
    assert row["status"] == "next" and row["retry_after"] is None
    assert conn.execute("SELECT 1 FROM audit_log WHERE action = 'chat_remind' AND entity_id = ?",
                        (task["id"],)).fetchone()


def test_nobody_took_it_remind_routes_again(setup):
    client, conn, owner, agent_id, h = setup
    msg, task = _ask(conn, owner, agent_id)
    conn.execute("UPDATE tasks SET archived_at = ? WHERE id = ?", (msg["created_at"], task["id"]))
    conn.commit()
    assert msg["channel_id"] not in answer_status.compute(conn)  # under two minutes: no alarm yet
    st = answer_status.compute(conn, _later(3))[msg["channel_id"]]
    assert st["state"] == "unclaimed" and st["agent_id"] is None
    r = client.post(f"/api/chat/messages/{msg['id']}/remind")
    assert r.status_code == 200, r.text
    new = conn.execute("SELECT * FROM tasks WHERE assignee_id = ? AND topic = 'chat' AND archived_at IS NULL",
                       (agent_id,)).fetchone()
    assert new is not None and chat.chat_origin(conn, new["id"]) == (msg["channel_id"], msg["id"])
    assert r.json()["state"] == "queued"


def test_only_the_author_reminds(setup):
    client, conn, owner, agent_id, h = setup
    msg, _ = _ask(conn, owner, agent_id)
    with pytest.raises(Forbidden):
        answer_status.remind(conn, Ctx(agent_id), msg["id"])


def test_acknowledgement_and_other_conversations_get_no_status(setup):
    client, conn, owner, agent_id, h = setup
    msg, _ = _ask(conn, owner, agent_id)
    g = chat.create_channel(conn, owner, "obseum-ai", [])
    assert set(answer_status.compute(conn)) == {msg["channel_id"]}
    assert str(g["id"]) not in chat.presence_view(conn, owner.actor_id, cached=False)["typing"]
    chat.send_dm(conn, owner, agent_id, "díky")
    assert msg["channel_id"] not in answer_status.compute(conn)  # his latest is a thank-you: nothing to wait for


def test_agent_edits_own_message_within_window(setup):
    client, conn, owner, agent_id, h = setup
    msg, _ = _ask(conn, owner, agent_id)
    mine = chat.send(conn, Ctx(agent_id), msg["channel_id"], "Odkaz: https://exmaple.cz", reply_to=msg["id"])
    out = chat.edit(conn, Ctx(agent_id), mine["id"], "Odkaz: https://example.cz")
    assert out["body"] == "Odkaz: https://example.cz" and out["edited_at"]
    assert conn.execute("SELECT 1 FROM audit_log WHERE entity = 'chat_message' AND entity_id = ? AND action = 'edit'",
                        (mine["id"],)).fetchone()
    with pytest.raises(Forbidden):
        chat.edit(conn, Ctx(agent_id), msg["id"], "cizí")
    old = (datetime.now(timezone.utc) - timedelta(hours=chat.AGENT_EDIT_HOURS + 1)).isoformat(timespec="seconds")
    conn.execute("UPDATE chat_messages SET created_at = ? WHERE id = ?", (old, mine["id"]))
    conn.commit()
    with pytest.raises(chat.ChatError):
        chat.edit(conn, Ctx(agent_id), mine["id"], "pozdě")
    # The owner (a person) edits without a window.
    conn.execute("UPDATE chat_messages SET created_at = ? WHERE id = ?", (old, msg["id"]))
    conn.commit()
    assert chat.edit(conn, owner, msg["id"], "Jak to vypadá?")["body"] == "Jak to vypadá?"


def test_chat_edit_is_an_agent_tool():
    from pos import mcp_server

    assert mcp_server.TOOL_PERMISSIONS["chat_edit"] == "messages:send"
    assert "chat_edit" in mcp_server.tool_names()
