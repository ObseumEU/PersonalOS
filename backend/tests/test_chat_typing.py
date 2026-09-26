"""The chat typing indicator (pos.chat, typing section): set by the platform on a
chat-triggered run, cleared on send, run end and TTL, never shown across channels."""

import time

import pytest
from fastapi.testclient import TestClient

from pos import actors, agents, agents_code, chat, tasks
from pos.config import Settings
from pos.core import Ctx
from pos.db import connect
from pos.main import create_app


@pytest.fixture(autouse=True)
def clean_typing():
    chat._typing.clear()
    yield
    chat._typing.clear()


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


def _ask(conn, owner, agent_id, text="Jak to vypadá?"):
    """The owner writes to the agent in its DM: a chat task for the agent."""
    msg = chat.send_dm(conn, owner, agent_id, text)
    t = conn.execute("SELECT id FROM tasks WHERE assignee_id = ? AND topic = 'chat' ORDER BY id DESC",
                     (agent_id,)).fetchone()
    return msg, tasks.get(conn, owner, t["id"])


def _start(client, h, task):
    run = client.post("/api/worker/runs", json={"task_id": task["ref"]}, headers=h)
    assert run.status_code == 201, run.text
    return run.json()["run_id"]


def test_chat_task_run_types_in_its_channel_and_posting_clears_it(setup):
    client, conn, owner, agent_id, h = setup
    msg, task = _ask(conn, owner, agent_id)
    assert chat.chat_origin(conn, task["id"]) == (msg["channel_id"], msg["id"])
    run = _start(client, h, task)

    state = client.get("/api/chat/typing").json()
    entry = state[str(msg["channel_id"])][0]
    assert entry["id"] == agent_id and entry["name"] == "Hlídač" and entry["state"] == "typing"
    assert entry["thread"] == msg["id"]  # the reply lands in the thread of the owner's message
    # The channel list carries it too (the sidebar badge).
    ch = next(c for c in client.get("/api/chat/channels").json() if c["id"] == msg["channel_id"])
    assert [t["id"] for t in ch["typing"]] == [agent_id]

    client.post(f"/api/worker/runs/{run}/heartbeat", headers=h)  # a step: still typing
    assert client.get("/api/chat/typing").json()[str(msg["channel_id"])][0]["state"] == "typing"

    chat.send(conn, Ctx(agent_id), msg["channel_id"], "Vše v pořádku.", reply_to=msg["id"])
    assert client.get("/api/chat/typing").json() == {}


def test_run_end_clears_typing(setup):
    client, conn, owner, agent_id, h = setup
    msg, task = _ask(conn, owner, agent_id)
    run = _start(client, h, task)
    assert client.get("/api/chat/typing").json()
    client.post(f"/api/worker/runs/{run}/finish", json={"status": "ok"}, headers=h)
    assert client.get("/api/chat/typing").json() == {}
    assert chat._typing == {}


def test_ttl_expires_typing_then_working(setup, monkeypatch):
    client, conn, owner, agent_id, h = setup
    msg, task = _ask(conn, owner, agent_id)
    run = _start(client, h, task)
    cid = str(msg["channel_id"])
    t0 = time.monotonic()
    # 20 s after the last step, with the worker's alive tick: still typing.
    monkeypatch.setattr(chat.time, "monotonic", lambda: t0 + 20)
    client.post(f"/api/worker/runs/{run}/alive", headers=h)
    # 40 s: no step for 40 s but alive 20 s ago: the softer "working" (long tool work).
    monkeypatch.setattr(chat.time, "monotonic", lambda: t0 + 40)
    assert client.get("/api/chat/typing").json()[cid][0]["state"] == "working"
    # 60 s: nothing for 40 s (a crashed worker): gone, and dropped from memory.
    monkeypatch.setattr(chat.time, "monotonic", lambda: t0 + 60)
    assert client.get("/api/chat/typing").json() == {}
    assert chat._typing == {}


def test_human_typing_ttl_and_thread(setup, monkeypatch):
    client, conn, owner, agent_id, h = setup
    g = chat.create_channel(conn, owner, "#ops", [agent_id])
    chat.typing(g["id"], agent_id, thread=7)  # a stand-in for another member typing in a thread
    t0 = time.monotonic()
    got = client.get("/api/chat/typing").json()[str(g["id"])][0]
    assert got["thread"] == 7 and got["state"] == "typing"
    client.post(f"/api/chat/channels/{g['id']}/typing", json={"thread": None})
    assert [e["id"] for e in client.get("/api/chat/typing").json()[str(g["id"])]] == [agent_id]  # never yourself
    monkeypatch.setattr(chat.time, "monotonic", lambda: t0 + chat.HUMAN_TYPING_S + 1)
    assert client.get("/api/chat/typing").json() == {}


def test_no_leak_across_channels(setup):
    client, conn, owner, agent_id, h = setup
    out = agents.create_agent(conn, owner, name="Nahlížeč", purpose="x", lifetime="long_lived",
                              permissions=["tasks:read", "messages:send"], data_dir=conn and None or None) \
        if False else None
    del out
    private = chat.create_channel(conn, owner, "#secret", [], visibility="private")
    team = chat.create_channel(conn, owner, "#open", [agent_id])
    chat.agent_typing(private["id"], owner.actor_id, run_id=None)  # the owner "types" in a private channel
    chat.agent_typing(team["id"], owner.actor_id, run_id=None)
    view = chat.typing_view(conn, agent_id)
    assert str(private["id"]) not in view and str(team["id"]) in view
    # Clearing one channel leaves the other alone.
    chat.typing_clear(team["id"], owner.actor_id)
    assert set(chat.typing_now()) == {str(private["id"])}


def test_mid_run_mention_types_where_it_was_mentioned(setup):
    client, conn, owner, agent_id, h = setup
    t = tasks.create(conn, owner, {"title": "Watch the servers", "assignee": {"type": "agent", "id": agent_id}})
    conn.commit()
    run = _start(client, h, t)
    assert client.get("/api/chat/typing").json() == {}  # not a chat task: nobody types
    g = chat.create_channel(conn, owner, "#ops", [agent_id])
    root = chat.send(conn, owner, g["id"], "Deploy je venku")
    chat.send(conn, owner, g["id"], "@Hlídač vidíš chyby?", reply_to=root["id"])
    items = client.get(f"/api/worker/inbox?run_id={run}", headers=h).json()
    assert items and items[0]["reason"] in ("mention", "reply")
    entry = client.get("/api/chat/typing").json()[str(g["id"])][0]
    assert entry["id"] == agent_id and entry["thread"] == root["id"]
    client.post(f"/api/worker/runs/{run}/finish", json={"status": "ok"}, headers=h)
    assert client.get("/api/chat/typing").json() == {}


def test_stream_presence_carries_typing(setup):
    client, conn, owner, agent_id, h = setup
    msg, task = _ask(conn, owner, agent_id)
    _start(client, h, task)
    body = client.get("/api/chat/stream?timeout=0.3").text
    assert "event: presence" in body and '"state": "typing"' in body and "Hlídač" in body


def test_worker_alive_ticker_ticks_during_a_run_and_stops_after():
    pytest.importorskip("pos_worker")
    from pos_worker.loop import _AliveTicker

    ticks = []

    class Fake:
        def alive(self, run_id):
            ticks.append(run_id)

    with _AliveTicker(Fake(), 5, every=0.01):
        time.sleep(0.1)
    n = len(ticks)
    time.sleep(0.05)
    assert n >= 2 and set(ticks) == {5} and len(ticks) <= n + 1
