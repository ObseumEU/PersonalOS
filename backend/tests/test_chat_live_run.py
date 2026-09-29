"""Chatting with a working agent: chat messages go into its live run (both engines),
the fast lane answers while a long step runs, and the queued chat task is not
answered twice (pos.fastlane, pos_worker.loop, docs/CHAT.md)."""

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from pos import actors, agents, agents_code, availability, chat, fastlane, tasks
from pos.core import Ctx, now_iso
from pos.db import connect, migrate
from pos.config import Settings

pytest.importorskip("pos_worker")
from pos_worker import prompt as wprompt  # noqa: E402
from test_worker import fake_claude, fake_codex, setup, worker_for  # noqa: E402,F401 - fixtures


@pytest.fixture(autouse=True)
def clean_memory():
    chat._typing.clear()
    fastlane._steps.clear()
    yield
    chat._typing.clear()
    fastlane._steps.clear()


# ------------------------------------------------------------------ the prompt

def test_injection_tells_where_to_answer_and_how_to_behave():
    m = {"id": 12, "from_name": "Owner", "priority": "fyi", "body": "Kolik testů padá?", "reason": "mention",
         "channel_id": 3, "channel": "#team", "reply_to": 10}
    text = wprompt.injection([m])
    assert "chat_send(channel=3, reply_to=10)" in text and "#team" in text
    assert "first reply briefly" in text and "report_progress with the new plan" in text
    assert "Do not abandon the task" in text and "ack_message" not in text
    dm = {**m, "id": 20, "reply_to": None, "channel": "dm", "reason": "dm"}
    assert "chat_send(channel=3, reply_to=20)" in wprompt.injection([dm])  # a top-level message: its own thread
    plan = {"id": 5, "from_name": "Owner", "priority": "change_plan", "body": "x", "reason": "priority"}
    assert "ack_message" in wprompt.injection([plan]) and "first reply briefly" not in wprompt.injection([plan])


# ------------------------------------------------------------------ into the live run, both engines

def _message_when_running(tmp_path, owner, agent_id, text):
    c = connect(Settings(data_dir=tmp_path).db_path)
    for _ in range(100):
        if c.execute("SELECT 1 FROM runs WHERE actor_id = ? AND status = 'running'", (agent_id,)).fetchone():
            break
        time.sleep(0.1)
    time.sleep(0.3)
    chat.send_dm(c, owner, agent_id, text)  # a plain chat message: no priority
    c.close()


@pytest.mark.parametrize("engine", ["codex", "claude"])
def test_a_plain_chat_message_goes_into_the_running_session(setup, fake_codex, fake_claude, tmp_path,
                                                            monkeypatch, engine):
    monkeypatch.setenv("POS_AGENT_RUNTIME", engine)
    client, conn, owner, agent_id, key = setup
    t = tasks.create(conn, owner, {"title": "Long build", "assignee": {"type": "agent", "id": agent_id}})
    conn.commit()
    worker = worker_for(client, key, fake_claude if engine == "claude" else fake_codex, tmp_path)
    th = threading.Thread(target=_message_when_running, args=(tmp_path, owner, agent_id, "Kolik ti to ještě potrvá?"))
    th.start()
    assert worker.step() == "ok"
    th.join()
    run = conn.execute("SELECT * FROM runs WHERE actor_id = ? ORDER BY id DESC", (agent_id,)).fetchone()
    assert conn.execute("SELECT delivered_in_run FROM chat_inbox WHERE actor_id = ?", (agent_id,)).fetchone()[0] == run["id"]
    note = tasks.get(conn, owner, t["id"])["progress_note"]
    # The session was stopped at a step and resumed with the message (not held until the end).
    assert ("Resumed and adapted" if engine == "codex" else "Adapted") in note
    assert "New information arrived while you were working" in note


# ------------------------------------------------------------------ the fast lane

@pytest.fixture
def db(tmp_path):
    from pos import integrations

    c = connect(tmp_path / "c.db")
    migrate(c)
    actors.ensure_builtin(c)
    integrations.register_builtin_agents(c)
    yield c
    c.close()


def _busy_agent(db, tmp_path, name="Stavitel"):
    owner = Ctx(actors.owner_id(db))
    aid = agents.create_agent(db, owner, name=name, purpose="builds", lifetime="long_lived",
                              permissions=["tasks:read", "tasks:claim", "messages:send"],
                              data_dir=tmp_path)["agent"]["id"]
    t = tasks.create(db, owner, {"title": "Build the release", "assignee": {"type": "agent", "id": aid},
                                 "notes": "Run the test suite, then build."})
    tasks.report_progress(db, Ctx(aid), t["id"], 40, "Plán: 1) testy 2) build 3) zpráva")
    run = db.execute("INSERT INTO runs (actor_id, task_id, kind, status, started_at, heartbeat_at) "
                     "VALUES (?, ?, 'task', 'running', ?, ?)",
                     (aid, t["id"], (datetime.now(timezone.utc) - timedelta(minutes=12)).isoformat(timespec="seconds"),
                      now_iso())).lastrowid
    db.commit()
    fastlane.step(run, "Bash: pytest -q", 12)
    return owner, aid, t, run


def _age(db, message_id, seconds=60):
    at = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds")
    db.execute("UPDATE chat_messages SET created_at = ? WHERE id = ?", (at, message_id))
    db.commit()


def test_fast_lane_answers_in_the_thread_after_30s_once(db, tmp_path, monkeypatch):
    owner, aid, t, run = _busy_agent(db, tmp_path)
    prompts = []
    monkeypatch.setattr(fastlane, "_llm_blocked", lambda conn, a: None)
    monkeypatch.setattr(fastlane, "ask_model", lambda conn, a, p: prompts.append(p) or "Teď běží testy, předám mu to.")
    g = chat.create_channel(db, owner, "#release", [aid])
    root = chat.send(db, owner, g["id"], "Release dnes?")
    msg = chat.send(db, owner, g["id"], "@Stavitel jak daleko jsi?", reply_to=root["id"])
    assert fastlane.due(db) == []  # younger than 30 s: the run may still pick it up
    _age(db, msg["id"])
    assert fastlane.due(db) == [(aid, msg["id"])]
    out = fastlane.respond(db, aid, msg["id"])
    assert out["mode"] == "model" and out["reply_to"] == root["id"] and out["channel_id"] == g["id"]
    assert out["body"] == "Teď běží testy, předám mu to."
    p = prompts[0]
    assert t["ref"] in p and "Bash: pytest -q" in p and "Plán: 1) testy" in p and "12 min" in p
    assert "Nikdy netvrď" in p
    # Once per message; the run still gets the message itself at its next step.
    assert fastlane.due(db) == [] and fastlane.respond(db, aid, msg["id"]) is None
    assert db.execute("SELECT read_at FROM chat_inbox WHERE message_id = ? AND actor_id = ?",
                      (msg["id"], aid)).fetchone()[0] is None
    assert chat.typing_now() == {}  # the reply cleared the typing it showed


def test_fast_lane_stays_quiet_when_the_run_answered_meanwhile(db, tmp_path, monkeypatch):
    owner, aid, t, run = _busy_agent(db, tmp_path)
    msg = chat.send_dm(db, owner, aid, "Hotovo?")
    _age(db, msg["id"])
    monkeypatch.setattr(fastlane, "_llm_blocked", lambda conn, a: None)

    def slow_model(conn, a, p):  # the main run answers while the fast model thinks
        chat.send(conn, Ctx(aid), msg["channel_id"], "Ještě ne, dělám build.", reply_to=msg["id"])
        return "Pracuju na tom."

    monkeypatch.setattr(fastlane, "ask_model", slow_model)
    assert fastlane.respond(db, aid, msg["id"]) is None
    assert db.execute("SELECT COUNT(*) FROM chat_messages WHERE author_id = ?", (aid,)).fetchone()[0] == 1
    assert chat.typing_now() == {}


def test_fast_lane_falls_back_to_a_code_reply(db, tmp_path, monkeypatch):
    owner, aid, t, run = _busy_agent(db, tmp_path)
    monkeypatch.setattr(fastlane, "_llm_blocked", lambda conn, a: None)

    def boom(conn, a, p):
        raise RuntimeError("model down")

    monkeypatch.setattr(fastlane, "ask_model", boom)
    msg = chat.send_dm(db, owner, aid, "Stihneš to do pěti?")
    _age(db, msg["id"])
    out = fastlane.respond(db, aid, msg["id"])
    # A DM has no threads: the fast answer goes into the conversation, quoting the question.
    assert out["mode"] == "code" and out["reply_to"] is None and out["quote_of"] == msg["id"]
    assert f"Pracuju na {t['ref']}" in out["body"] and "Bash: pytest -q" in out["body"] and "12 min" in out["body"]
    assert "rychlý model teď neodpovídá" in out["body"]


def test_fast_lane_without_budget_or_llm_never_calls_the_model(db, tmp_path, monkeypatch):
    owner, aid, t, run = _busy_agent(db, tmp_path)
    monkeypatch.setattr(availability, "why_not", lambda conn, a: {"reason": "jeho rozpočet je vyčerpaný",
                                                                  "retry": "zítra", "until": None})
    monkeypatch.setattr(fastlane, "ask_model", lambda *a: pytest.fail("the model must not be called"))
    # (the chat task's own automatic reply would answer first; here only the fast lane is under test)
    monkeypatch.setattr(availability, "autoreply", lambda *a, **k: None)
    msg = chat.send_dm(db, owner, aid, "Jak to jde?")
    _age(db, msg["id"])
    out = fastlane.respond(db, aid, msg["id"])
    assert out["mode"] == "code" and "jeho rozpočet je vyčerpaný" in out["body"]


def test_fast_lane_skips_read_messages_idle_agents_and_agent_authors(db, tmp_path, monkeypatch):
    owner, aid, t, run = _busy_agent(db, tmp_path)
    monkeypatch.setattr(fastlane, "_llm_blocked", lambda conn, a: "off")
    read = chat.send_dm(db, owner, aid, "Přečteno")
    chat.check_inbox(db, aid, run_id=run)  # the run picked it up at a step
    _age(db, read["id"])
    other = agents.create_agent(db, owner, name="Kolega", purpose="x", lifetime="long_lived",
                                permissions=["messages:send"], data_dir=tmp_path)["agent"]["id"]
    from_agent = chat.send_dm(db, Ctx(other), aid, "Ahoj od agenta")
    _age(db, from_agent["id"])
    idle = agents.create_agent(db, owner, name="Nečinný", purpose="x", lifetime="long_lived",
                               permissions=["messages:send"], data_dir=tmp_path)["agent"]["id"]
    to_idle = chat.send_dm(db, owner, idle, "Hej")
    _age(db, to_idle["id"])
    assert fastlane.due(db) == []
    db.execute("UPDATE runs SET status = 'ok' WHERE id = ?", (run,))
    fresh = chat.send_dm(db, owner, aid, "Po konci běhu")
    _age(db, fresh["id"])
    assert fastlane.due(db) == []  # no live run: the normal chat task path answers


def test_answer_from_the_live_run_closes_the_queued_chat_task(db, tmp_path, monkeypatch):
    owner, aid, t, run = _busy_agent(db, tmp_path)
    monkeypatch.setattr(agents_code, "has_worker", lambda name: name == "Stavitel")
    monkeypatch.setattr(availability, "autoreply", lambda *a, **k: None)
    msg = chat.send_dm(db, owner, aid, "Pošli mi pak shrnutí.")
    chat_task = db.execute("SELECT id, status FROM tasks WHERE assignee_id = ? AND topic = 'chat'", (aid,)).fetchone()
    assert chat_task["status"] == "next"
    # A fast-lane (system) reply is not an answer: the task stays.
    chat.send(db, Ctx(aid, via="system"), msg["channel_id"], "Pracuju na tom.", reply_to=msg["id"], system=True)
    assert tasks.get(db, owner, chat_task["id"])["status"] == "next"
    # The live run answered for real (chat_send): nothing left to answer twice.
    chat.send(db, Ctx(aid), msg["channel_id"], "Jasně, shrnutí pošlu po buildu.", reply_to=msg["id"])
    assert tasks.get(db, owner, chat_task["id"])["status"] == "done"


def test_members_show_the_current_work(db, tmp_path):
    owner, aid, t, run = _busy_agent(db, tmp_path)
    me = next(m for m in chat.members_overview(db) if m["id"] == aid)
    assert me["current"]["task_ref"] == t["ref"] and me["current"]["minutes"] >= 11
    ch = chat.dm_channel(db, owner.actor_id, aid, owner)
    view = chat.channel_view(db, ch["id"], owner.actor_id)
    assert next(m for m in view["members"] if m["id"] == aid)["current"]["task_ref"] == t["ref"]
