"""Every agent has a worker, and the owner is never left without an answer (pos.workers).

2026-09-26: the owner's DMs to the HR agent got no reply at all. HR ran inside the
core without a worker, and the chat only made "answer" tasks for agents with one.
Now every agent has a worker (its own container, the agent pool or A2A), built-in
automation is a service, a message to a service or to an agent that cannot run
gets a code-built reply, and a watch files an incident for a worker that stopped
and for an owner message left unanswered for 10 minutes.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from pos import actors, agents, availability, chat, monitor, tasks, workers
from pos.config import Settings
from pos.core import Ctx
from pos.db import connect
from pos.main import create_app


def _app(tmp_path, monkeypatch, as_code: bool):
    if as_code:
        monkeypatch.setenv("POS_AGENTS_AS_CODE", "1")
    monkeypatch.setenv("POS_KNOWLAGE_A2A_URL", "http://knowlage.test/a2a/default")
    settings = Settings(data_dir=tmp_path, password="pw", session_secret="t" * 32, scheduler=False)
    client = TestClient(create_app(settings))
    client.__enter__()
    return client, connect(settings.db_path)


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """The default seed: built-in members plus the role agents from agents/*/agent.json."""
    client, conn = _app(tmp_path, monkeypatch, as_code=True)
    yield conn
    conn.close()
    client.__exit__(None, None, None)


@pytest.fixture
def db(tmp_path, monkeypatch):
    client, conn = _app(tmp_path, monkeypatch, as_code=False)
    yield conn
    conn.close()
    client.__exit__(None, None, None)


def _owner(conn):
    return Ctx(actors.owner_id(conn))


def _replies(conn, message_id):
    return conn.execute("SELECT * FROM chat_messages WHERE reply_to = ? ORDER BY id", (message_id,)).fetchall()


def _chat_task(conn, aid):
    return conn.execute("SELECT * FROM tasks WHERE assignee_id = ? AND title LIKE 'Chat: answer%' "
                        "ORDER BY id DESC", (aid,)).fetchone()


def _seen_now(conn, aid):
    conn.execute("UPDATE actors SET last_seen_at = ? WHERE id = ?",
                 (datetime.now(timezone.utc).isoformat(timespec="seconds"), aid))
    conn.commit()


# ------------------------------------------------------------------ the audit: every agent has a reply path

def test_every_agent_in_the_default_seed_has_a_worker_or_is_a_service(seeded):
    conn = seeded
    members = conn.execute("SELECT * FROM actors WHERE kind != 'human' AND archived_at IS NULL").fetchall()
    names = {m["name"] for m in members}
    assert {"HR agent", "Monitor", "Project manager", "Assistant", "Deployer", "Nexus", "Knowledge agent",
            "Community agent"} <= names
    for m in members:
        path = workers.reply_path(conn, m)
        assert path is not None or workers.is_service(m), f"{m['name']} has no worker and is not a service"
    services = {m["name"] for m in members if workers.is_service(m)}
    assert services == {"Deployer", "Nexus"}                         # automation, no model: services
    hr = actors.find_by_name(conn, "HR agent")
    assert workers.reply_path(conn, hr) == {"kind": "dedicated", "name": "hr-agent"}
    assert hr["runtime"] == "codex_worker" and hr["engine"] == "claude" and hr["model"] == "claude-haiku-4-5"
    assert agents.has_permission(conn, hr["id"], "messages:send")    # it can answer in chat
    assert workers.reply_path(conn, actors.find_by_name(conn, "Knowledge agent"))["kind"] == "a2a"


def test_every_agent_in_the_default_seed_answers_the_owner_or_the_platform_does(seeded):
    """The audit as a test: a DM from the owner to each member ends in a task for
    its worker, or in an automatic reply plus a task for the Project manager."""
    conn = seeded
    owner = _owner(conn)
    pm = actors.find_by_name(conn, "Project manager")
    for m in conn.execute("SELECT * FROM actors WHERE kind != 'human' AND archived_at IS NULL").fetchall():
        _seen_now(conn, m["id"])
        msg = chat.send_dm(conn, owner, m["id"], f"Test pro {m['name']}: ozvi se")
        if workers.is_service(m):
            reply = _replies(conn, msg["id"])
            assert reply and reply[0]["author_id"] == m["id"], m["name"]
            assert reply[0]["body"].startswith("Automatická odpověď platformy") and "Project managerovi" in reply[0]["body"]
            assert "za " + m["name"] in _chat_task(conn, pm["id"])["title"]
        else:
            t = _chat_task(conn, m["id"])
            assert t is not None and t["priority"] == 1 and t["status"] == "next", m["name"]


def test_hr_answers_the_owner_through_its_worker(seeded):
    """The bug itself: the owner's DM to HR becomes HR's chat task, not silence."""
    conn = seeded
    hr = actors.find_by_name(conn, "HR agent")
    _seen_now(conn, hr["id"])
    msg = chat.send_dm(conn, _owner(conn), hr["id"], "Prosím vytvoř agenta HomeAssistant")
    t = _chat_task(conn, hr["id"])
    assert t is not None and f"reply_to={msg['id']}" in t["notes"]
    assert _replies(conn, msg["id"]) == []                           # its worker runs: no automatic reply


# ------------------------------------------------------------------ services and missing workers

def test_a_message_to_a_service_gets_a_code_reply_and_goes_to_the_project_manager(seeded):
    conn = seeded
    owner = _owner(conn)
    dep, pm = actors.find_by_name(conn, "Deployer"), actors.find_by_name(conn, "Project manager")
    _seen_now(conn, pm["id"])
    msg = chat.send_dm(conn, owner, dep["id"], "Kdy bylo poslední nasazení?")
    reply = _replies(conn, msg["id"])
    assert len(reply) == 1 and "Deployer je služba platformy" in reply[0]["body"] and "v přímé zprávě" in reply[0]["body"]
    t = _chat_task(conn, pm["id"])
    dm = chat.find_dm(conn, owner.actor_id, pm["id"])
    assert f"chat_send(channel={dm['id']})" in t["notes"] and "Kdy bylo poslední nasazení?" in t["notes"]
    # in #team the PM answers in the thread; once per message
    cid = chat.ensure_team_channel(conn)
    msg2 = chat.send(conn, owner, cid, "@Deployer nasaď to prosím")
    t2 = _chat_task(conn, pm["id"])
    assert f"chat_send(channel={cid}, reply_to={msg2['id']})" in t2["notes"]
    assert "tady ve vlákně" in _replies(conn, msg2["id"])[0]["body"]
    availability.forward_unserved(conn, owner, chat._channel(conn, cid), dep, msg2["id"], "znovu")
    assert len(_replies(conn, msg2["id"])) == 1


def test_an_agent_whose_worker_is_down_answers_in_code_at_once(db, tmp_path):
    conn = db
    owner = _owner(conn)
    aid = agents.create_agent(conn, owner, name="Fakturant", purpose="invoices", lifetime="long_lived",
                              permissions=["tasks:read", "tasks:claim", "messages:send"], data_dir=tmp_path)["agent"]["id"]
    assert workers.reply_path(conn, actors.get(conn, aid))["kind"] == "pool"
    old = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(timespec="seconds")
    conn.execute("UPDATE actors SET last_seen_at = ? WHERE id = ?", (old, aid))
    conn.commit()
    msg = chat.send_dm(conn, owner, aid, "Ahoj")
    reply = _replies(conn, msg["id"])
    assert len(reply) == 1 and "worker (pool/fakturant) neběží" in reply[0]["body"]
    assert _chat_task(conn, aid)["status"] == "next"                 # it answers for real once it is back


def test_a_remote_agent_answers_in_the_thread(db):
    conn = db
    owner = _owner(conn)
    kb = actors.find_by_name(conn, "Knowledge agent")
    msg = chat.send_dm(conn, owner, kb["id"], "Co víme o zákazníkovi X?")
    t = _chat_task(conn, kb["id"])
    assert t["notes"].endswith("Co víme o zákazníkovi X?") and "chat_send" not in t["notes"]
    availability.post_answer(conn, t["id"], kb["id"], "Zákazník X: tři zakázky.")
    assert _replies(conn, msg["id"])[0]["body"] == "Zákazník X: tři zakázky."


# ------------------------------------------------------------------ creating agents: never without a worker

def test_creating_an_agent_provisions_its_worker_or_is_refused(db, tmp_path, monkeypatch):
    conn = db
    owner = _owner(conn)
    monkeypatch.setenv("POS_WORKER_KEYS_DIR", str(tmp_path / "keys"))
    with pytest.raises(agents.AgentError, match="needs a worker"):
        agents.create_agent(conn, owner, name="Bez workera", purpose="x", runtime="builtin", data_dir=tmp_path)
    with pytest.raises(agents.AgentError, match="a2a_url"):
        agents.create_agent(conn, owner, name="Vzdálený", purpose="x", runtime="a2a", data_dir=tmp_path)
    made = agents.create_agent(conn, owner, name="Home Assistant", purpose="dům", lifetime="long_lived",
                               data_dir=tmp_path)
    key = (tmp_path / "keys" / "pool" / "home-assistant" / "key").read_text().strip()
    assert actors.actor_for_key(conn, key) == made["agent"]["id"]
    agents.archive(conn, owner, made["agent"]["id"], "test")
    workers.sync_pool_keys(conn)
    assert not (tmp_path / "keys" / "pool" / "home-assistant").exists()


def test_the_pool_starts_stops_and_restarts_workers(tmp_path):
    from pos_worker.pool import Pool

    class Proc:
        def __init__(self):
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

    clock = [0.0]
    spawned = []

    def spawn(slug, key):
        spawned.append((slug, key))
        return Proc()

    pool = Pool(tmp_path, tmp_path / "work", spawn=spawn, clock=lambda: clock[0])
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "key").write_text("k1\n")
    assert pool.tick()["started"] == ["a"]
    assert pool.tick()["started"] == []
    (tmp_path / "a" / "key").write_text("k2\n")                       # a new key: restarted with it
    assert pool.tick() == {"started": ["a"], "stopped": ["a"], "running": ["a"]} and spawned[-1] == ("a", "k2")
    pool.children["a"][0].returncode = 1                              # it crashed: back after a pause
    assert pool.tick()["started"] == []
    clock[0] += 400
    assert pool.tick()["started"] == ["a"]
    (tmp_path / "a" / "key").unlink()                                 # archived: stopped
    assert pool.tick()["stopped"] == ["a"] and pool.children == {}


# ------------------------------------------------------------------ the watch: incidents

@pytest.fixture
def watched(db, tmp_path):
    conn = db
    owner = _owner(conn)
    mid = agents.create_agent(conn, owner, name=monitor.NAME, purpose="triage", lifetime="long_lived",
                              permissions=["tasks:read", "tasks:claim", "tasks:write", "messages:send",
                                           "approvals:request", "ops:monitor"], data_dir=tmp_path)["agent"]["id"]
    aid = agents.create_agent(conn, owner, name="Fakturant", purpose="invoices", lifetime="long_lived",
                              permissions=["tasks:read", "tasks:claim", "messages:send"], data_dir=tmp_path)["agent"]["id"]
    monitor.ensure(conn)
    for a in (mid, aid):
        _seen_now(conn, a)
    workers.watch(conn)                                               # the first watch: from now on
    hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    workers._set_state(conn, "chat_watch_since", hour_ago)            # (here: an hour ago)
    conn.commit()
    return conn, owner, mid, aid


def _incidents(conn, kind):
    return conn.execute("SELECT t.* FROM tasks t JOIN sentinel_incidents s ON s.task_id = t.id WHERE s.kind = ?",
                        (kind,)).fetchall()


def test_an_owner_message_unanswered_for_10_minutes_is_an_incident(watched):
    conn, owner, mid, aid = watched
    msg = chat.send_dm(conn, owner, aid, "Stav faktur?")
    assert workers.watch(conn)["unanswered"] == []                    # not 10 minutes yet
    old = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat(timespec="seconds")
    conn.execute("UPDATE chat_messages SET created_at = ? WHERE id = ?", (old, msg["id"]))
    conn.commit()
    _seen_now(conn, aid)
    out = workers.watch(conn)
    assert out["unanswered"] == [{"message": msg["id"], "agent": "Fakturant"}]
    inc = _incidents(conn, "chat_unanswered")
    assert len(inc) == 1 and "Fakturant neodpověděl" in inc[0]["title"]
    nudge = _replies(conn, msg["id"])
    assert len(nudge) == 1 and "zatím neodpověděl" in nudge[0]["body"] and "Hlídač" in nudge[0]["body"]
    assert workers.watch(conn)["unanswered"] == []                    # once per message
    # a message that was answered is no incident
    msg2 = chat.send_dm(conn, owner, aid, "A ještě?")
    chat.send(conn, Ctx(aid), msg2["channel_id"], "Hotovo.", reply_to=msg2["id"])
    conn.execute("UPDATE chat_messages SET created_at = ? WHERE id = ?", (old, msg2["id"]))
    conn.commit()
    assert workers.watch(conn)["unanswered"] == []


def test_a_worker_silent_for_10_minutes_is_an_incident_until_it_is_back(watched):
    conn, owner, mid, aid = watched
    old = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat(timespec="seconds")
    conn.execute("UPDATE actors SET last_seen_at = ? WHERE id = ?", (old, aid))
    conn.commit()
    assert workers.watch(conn)["down"] == ["Fakturant"]
    inc = _incidents(conn, "worker_down")
    assert len(inc) == 1 and "worker agenta Fakturant neběží" in inc[0]["title"]
    assert workers.watch(conn)["down"] == []                          # one incident while it lasts
    _seen_now(conn, aid)
    assert workers.watch(conn)["back"] == ["Fakturant"]
    row = conn.execute("SELECT resolved_at FROM sentinel_incidents WHERE kind = 'worker_down'").fetchone()
    assert row["resolved_at"]
    assert tasks.get(conn, owner, inc[0]["id"])["status"] == "done"   # closed without a model run


# ------------------------------------------------------------------ an agent always can and does answer

def test_an_agent_without_messages_send_answers_the_person_waiting_for_it(db, tmp_path):
    """2026-09-26 audit: the Mail agent and the Community agent closed their chat tasks
    without a word: chat_send needs messages:send, which they do not have."""
    from pos import mcp_server

    conn = db
    owner = _owner(conn)
    aid = agents.create_agent(conn, owner, name="Pošťák", purpose="mail", lifetime="long_lived",
                              permissions=["tasks:read", "tasks:claim"], data_dir=tmp_path)["agent"]["id"]
    _seen_now(conn, aid)
    assert not mcp_server.may_use(conn, aid, "chat_send")
    msg = chat.send_dm(conn, owner, aid, "Vidíš mě?")
    assert mcp_server.may_use(conn, aid, "chat_send")                 # someone waits for its answer
    reply = chat.send(conn, Ctx(aid), msg["channel_id"], "Vidím.", reply_to=msg["id"])
    assert reply["body"] == "Vidím."
    team = chat.ensure_team_channel(conn)
    with pytest.raises(Exception, match="messages:send"):             # but only there
        chat.send(conn, Ctx(aid), team, "Ahoj všichni")


def test_a_chat_task_closed_without_an_answer_posts_its_result(db, tmp_path):
    conn = db
    owner = _owner(conn)
    aid = agents.create_agent(conn, owner, name="Mlčoch", purpose="x", lifetime="long_lived",
                              permissions=["tasks:read", "tasks:claim"], data_dir=tmp_path)["agent"]["id"]
    _seen_now(conn, aid)
    msg = chat.send_dm(conn, owner, aid, "Kolik máme faktur?")
    t = _chat_task(conn, aid)
    tasks.complete(conn, Ctx(aid), t["id"], "Máme 3 nezaplacené faktury.")
    reply = _replies(conn, msg["id"])
    assert len(reply) == 1 and reply[0]["author_id"] == aid and reply[0]["body"].startswith("Máme 3 nezaplacené")
    assert "Doručila platforma" in reply[0]["body"]
    # an agent that answered itself gets no second message
    msg2 = chat.send_dm(conn, owner, aid, "A kolik je po splatnosti?")
    t2 = _chat_task(conn, aid)
    chat.send(conn, Ctx(aid), msg2["channel_id"], "Jedna.", reply_to=msg2["id"])
    tasks.complete(conn, Ctx(aid), t2["id"], "Odpověděno.")
    assert [r["body"] for r in _replies(conn, msg2["id"])] == ["Jedna."]


def test_a_remote_agent_whose_bridge_is_off_goes_to_the_project_manager(db):
    """2026-09-26 audit: the owner had switched off the A2A job, so the Knowledge
    agent's task sat in the queue and nobody answered."""
    conn = db
    owner = _owner(conn)
    from pos import scheduler

    scheduler.seed(conn)
    conn.execute("UPDATE jobs SET enabled = 0 WHERE action = 'a2a_sync'")
    conn.commit()
    kb, pm = actors.find_by_name(conn, "Knowledge agent"), actors.find_by_name(conn, "Project manager")
    _seen_now(conn, pm["id"])
    msg = chat.send_dm(conn, owner, kb["id"], "Co víme o zákazníkovi X?")
    reply = _replies(conn, msg["id"])
    assert len(reply) == 1 and "Knowledge agent teď neběží, protože jeho spojení" in reply[0]["body"]
    assert "Project managerovi" in reply[0]["body"] and _chat_task(conn, kb["id"]) is None
    assert "za Knowledge agent" in _chat_task(conn, pm["id"])["title"]
    assert "spojení se vzdálenou aplikací je vypnuté" in workers.worker_down(conn, kb)
