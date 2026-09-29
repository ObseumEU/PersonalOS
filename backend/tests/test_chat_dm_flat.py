"""A DM is one flat conversation (answers quote, never thread); threads in channels carry a
summary and a read marker per person ("Vlákna")."""

import pytest
from fastapi.testclient import TestClient

from pos import actors, agents, chat, integrations
from pos.config import Settings
from pos.core import Ctx
from pos.db import connect, migrate
from pos.main import create_app


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "c.db")
    migrate(c)
    actors.ensure_builtin(c)
    integrations.register_builtin_agents(c)
    integrations.install()
    yield c
    c.close()


@pytest.fixture
def me(conn):
    return Ctx(actors.owner_id(conn))


def make(conn, me, tmp_path, name):
    return agents.create_agent(conn, me, name=name, purpose=f"{name} work", lifetime="long_lived",
                               permissions=["tasks:read", "messages:send"], data_dir=tmp_path)["agent"]["id"]


def test_agent_answer_in_a_dm_is_flat_with_a_quote(conn, me, tmp_path):
    ceo = make(conn, me, tmp_path, "Boss")
    q = chat.send_dm(conn, me, ceo, "Jak jsme na tom s knihou?")
    ch = q["channel_id"]
    # The agent answers the way its prompt says: chat_send(channel, reply_to=<the question>).
    a = chat.send(conn, Ctx(ceo), ch, "Kapitola 3 je hotová.", reply_to=q["id"])
    assert a["reply_to"] is None and a["quote_of"] == q["id"]
    assert a["quote"]["author_name"] == actors.get(conn, me.actor_id)["name"]
    assert a["quote"]["body"].startswith("Jak jsme")
    page = chat.messages(conn, me.actor_id, ch)["messages"]
    assert [m["id"] for m in page] == [q["id"], a["id"]]  # one timeline, in order
    assert all(m["replies"] == 0 for m in page)  # no hidden thread
    # The owner's own reply (the phone's composer, or the desktop's) is flat too.
    b = chat.send(conn, me, ch, "Díky, a co obálka?", reply_to=a["id"])
    assert b["reply_to"] is None and b["quote_of"] == a["id"]
    assert b["inbox"] == [ceo]  # still reaches the agent


def test_threads_stay_in_channels(conn, me, tmp_path):
    dev = make(conn, me, tmp_path, "Dev")
    g = chat.create_channel(conn, me, "kniha", [dev])
    root = chat.send(conn, me, g["id"], "@Dev kdy bude sazba?")
    r = chat.send(conn, Ctx(dev), g["id"], "Zítra.", reply_to=root["id"])
    assert r["reply_to"] == root["id"] and r.get("quote_of") is None


def test_legacy_threaded_dm_replies_still_load(conn, me, tmp_path):
    """Replies an agent posted in a DM thread before the change stay where they are (no rewrite);
    the page carries them with their reply_to, and the apps flatten them."""
    ceo = make(conn, me, tmp_path, "Boss")
    q = chat.send_dm(conn, me, ceo, "Stav?")
    ch = q["channel_id"]
    old = conn.execute("INSERT INTO chat_messages (channel_id, author_id, body, reply_to, trust, created_at) "
                       "VALUES (?, ?, 'Vše běží.', ?, 'agent', '2026-09-28T10:00:00+00:00')",
                       (ch, ceo, q["id"])).lastrowid
    conn.commit()
    page = chat.messages(conn, me.actor_id, ch)["messages"]
    assert [(m["id"], m["reply_to"]) for m in page] == [(q["id"], None), (old, q["id"])]


def test_thread_summary_and_unread(conn, me, tmp_path):
    dev = make(conn, me, tmp_path, "Dev")
    qa = make(conn, me, tmp_path, "QA")
    g = chat.create_channel(conn, me, "team2", [dev, qa])
    root = chat.send(conn, me, g["id"], "@Dev @QA co release?")
    chat.send(conn, Ctx(dev), g["id"], "Build je zelený.", reply_to=root["id"])
    last = chat.send(conn, Ctx(qa), g["id"], "Testy prošly, můžeme.", reply_to=root["id"])

    view = chat.message_view(conn, root["id"], me.actor_id)
    t = view["thread"]
    assert view["replies"] == 2 and t["unread"] == 2
    assert [x["name"] for x in t["repliers"]] == ["QA", "Dev"]  # newest first
    assert t["last"]["id"] == last["id"] and t["last"]["body"].startswith("Testy")
    # Reading the channel does not read its threads.
    chat.mark_read(conn, me, g["id"])
    assert chat.message_view(conn, root["id"], me.actor_id)["thread"]["unread"] == 2

    listed = chat.threads(conn, me.actor_id)
    assert listed["unread"] == 2 and listed["threads"][0]["root"]["id"] == root["id"]
    assert listed["threads"][0]["channel_name"] == "team2"

    chat.mark_thread_read(conn, me, root["id"])
    assert chat.message_view(conn, root["id"], me.actor_id)["thread"]["unread"] == 0
    assert chat.threads(conn, me.actor_id, unread_only=True)["threads"] == []
    # A new reply is unread again; the owner's own reply in the thread reads it.
    chat.send(conn, Ctx(dev), g["id"], "Ještě jedna věc.", reply_to=root["id"])
    assert chat.threads(conn, me.actor_id)["unread"] == 1
    chat.send(conn, me, g["id"], "Jasně.", reply_to=root["id"])
    assert chat.threads(conn, me.actor_id)["unread"] == 0


def test_threads_before_the_migration_count_as_read(tmp_path):
    from pos.db import MIGRATIONS

    c = connect(tmp_path / "old.db")
    for i, sql in enumerate(MIGRATIONS[:29], start=1):
        c.executescript(sql)
        c.execute(f"PRAGMA user_version = {i}")
    c.commit()
    actors.ensure_builtin(c)
    owner = actors.owner_id(c)
    dev = c.execute("INSERT INTO actors (kind, name, created_at) VALUES ('agent', 'Dev', 'x')").lastrowid
    ch = c.execute("INSERT INTO channels (kind, name, created_at) VALUES ('group', 'old', 'x')").lastrowid
    for a in (owner, dev):
        c.execute("INSERT INTO channel_members (channel_id, actor_id, joined_at) VALUES (?, ?, 'x')", (ch, a))
    root = c.execute("INSERT INTO chat_messages (channel_id, author_id, body, trust, created_at) "
                     "VALUES (?, ?, 'q', 'owner', 'x')", (ch, owner)).lastrowid
    c.execute("INSERT INTO chat_messages (channel_id, author_id, body, reply_to, trust, created_at) "
              "VALUES (?, ?, 'a', ?, 'agent', 'x')", (ch, dev, root))
    c.commit()
    migrate(c)
    assert chat.threads(c, owner)["unread"] == 0
    c.close()


def test_threads_api(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path, scheduler=False))) as client:
        r = client.get("/api/chat/threads")
        assert r.status_code == 200 and r.json() == {"threads": [], "unread": 0}
