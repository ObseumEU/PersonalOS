"""Meetings (pos.meetings): turns in order, bounds, the loop-detection exemption, the decision with
its tasks, the owner's input, and meetings started by a schedule."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from pos import actors, agents, api_worker, chat, integrations, meetings, projects, schedules
from pos.core import Ctx
from pos.db import connect, migrate


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


def make(conn, me, tmp_path, name, team="kniha", reports_to=None, perms=("tasks:read", "tasks:claim")):
    aid = agents.create_agent(conn, me, name=name, purpose=f"{name} work", lifetime="long_lived",
                              permissions=list(perms), data_dir=tmp_path)["agent"]["id"]
    conn.execute("UPDATE actors SET team = ?, reports_to = ?, runtime = 'codex_worker' WHERE id = ?",
                 (team, reports_to, aid))
    conn.commit()
    return aid


@pytest.fixture
def team(conn, me, tmp_path):
    lead = make(conn, me, tmp_path, "Book Lead", perms=("tasks:read", "tasks:claim", "messages:send"))
    a = make(conn, me, tmp_path, "Writer", reports_to=lead)
    b = make(conn, me, tmp_path, "Researcher", reports_to=lead)
    ch = chat.create_channel(conn, me, "kniha", [lead, a, b])
    return {"lead": lead, "a": a, "b": b, "ch": ch["id"]}


def open_turn(conn, mid):
    return meetings.open_turn(conn, mid)


def turn_task(conn, mid):
    t = open_turn(conn, mid)
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (t["task_id"],)).fetchone()


def test_turns_in_order_then_the_decision_with_tasks(conn, me, team, tmp_path):
    m = meetings.start(conn, me, "kniha", "Plán kapitoly 3", ["Co z Hormoziho převezmeme", "Termíny"],
                       ["Writer", "Researcher"], rounds=2, facilitator="Book Lead")
    root = m["thread"]
    assert m["now_speaking"] == "Writer" and m["status"] == "running"
    t = turn_task(conn, m["id"])
    assert t["topic"] == "meeting" and "Co z Hormoziho" in t["notes"] and f"reply_to={root}" in t["notes"]
    assert chat.chat_origin(conn, t["id"]) == (team["ch"], root)  # the typing indicator shows in the thread

    # Not your turn: the researcher waits; a turn is short. Agents without messages:send speak on their turn.
    with pytest.raises(chat.ChatError, match="not your turn"):
        chat.send(conn, Ctx(team["b"]), team["ch"], "Já taky!", reply_to=root)
    with pytest.raises(chat.ChatError, match="at most"):
        chat.send(conn, Ctx(team["a"]), team["ch"], "x" * 1600, reply_to=root)
    order = []
    for who, text in [("a", "Navrhuju 3 části [kb:c12]."), ("b", "Data říkají opak [kb:c40]."),
                      ("a", "Souhlas s Researcherem v bodě 2."), ("b", "Stavím na tom: termín pátek.")]:
        order.append(meetings.view(conn, m["id"])["now_speaking"])
        chat.send(conn, Ctx(team[who]), team["ch"], text, reply_to=root)
    assert order == ["Writer", "Researcher", "Writer", "Researcher"]
    assert meetings.view(conn, m["id"])["now_speaking"] == "Book Lead"
    decision_task = turn_task(conn, m["id"])
    assert "meeting_decide" in decision_task["notes"] and "Data říkají opak" in decision_task["notes"]
    # Earlier turn tasks are done.
    assert conn.execute("SELECT COUNT(*) FROM tasks WHERE topic = 'meeting' AND status = 'done'").fetchone()[0] == 4

    make(conn, me, tmp_path, "Outsider", team="jine")
    with pytest.raises(meetings.MeetingError):  # tasks go to people who were in the meeting
        meetings.decide(conn, Ctx(team["lead"]), m["id"], "Tři části",
                        tasks_=[{"title": "x", "assignee": "Outsider"}])
    out = meetings.decide(conn, Ctx(team["lead"]), m["id"], "Kapitola 3 bude mít tři části.", why="Data z [kb:c40].",
                          not_doing="Video.", tasks_=[{"title": "Napsat část 1", "assignee": "Writer",
                                                       "definition_of_done": "Text v repu."}])
    assert out["status"] == "closed" and out["close_reason"] == "decided" and len(out["tasks"]) == 1
    t = conn.execute("SELECT * FROM tasks WHERE title = 'Napsat část 1'").fetchone()
    assert t["assignee_id"] == team["a"] and t["definition_of_done"] == "Text v repu."
    msgs = chat.messages(conn, me.actor_id, team["ch"], thread=root)["messages"]
    assert msgs[0]["meeting"]["kind"] == "agenda"
    assert [x["meeting"]["round"] for x in msgs[1:5]] == [1, 1, 2, 2]
    assert msgs[-1]["meeting"]["kind"] == "decision" and msgs[-1]["body"].startswith("✅ **Rozhodnutí:**")
    assert conn.execute("SELECT 1 FROM audit_log WHERE action = 'meeting_decision'").fetchone()
    assert meetings.for_thread(conn, team["ch"], root) is None  # closed: the thread is ordinary chat again


def test_owner_input_goes_into_the_next_turns_and_asks_for_no_answer(conn, me, team):
    m = meetings.start(conn, me, "kniha", "Obálka", "Barvy", ["Writer", "Researcher"], rounds=1,
                       facilitator="Book Lead")
    root = m["thread"]
    before = conn.execute("SELECT COUNT(*) FROM tasks WHERE title LIKE 'Chat: answer%'").fetchone()[0]
    chat.send(conn, me, team["ch"], "Hlavně žádná růžová.", reply_to=root)
    assert conn.execute("SELECT COUNT(*) FROM tasks WHERE title LIKE 'Chat: answer%'").fetchone()[0] == before
    assert meetings.view(conn, m["id"])["now_speaking"] == "Writer"  # the owner does not take a turn
    chat.send(conn, Ctx(team["a"]), team["ch"], "Modrá.", reply_to=root)
    notes = turn_task(conn, m["id"])["notes"]
    assert "MAJITEL" in notes and "žádná růžová" in notes and "take it into account" in notes


def test_bounds_rounds_budget_time_and_failing_participants(conn, me, team):
    with pytest.raises(meetings.MeetingError):
        meetings.start(conn, me, "kniha", "X", "", ["Writer"], rounds=5)
    with pytest.raises(meetings.MeetingError):
        meetings.start(conn, me, "kniha", "X", "", [])
    m = meetings.start(conn, me, "kniha", "Rozpočet", "", ["Writer", "Researcher"], rounds=2,
                       facilitator="Book Lead", budget_usd=1.0)
    with pytest.raises(meetings.MeetingError):  # one meeting per channel at a time
        meetings.start(conn, me, "kniha", "Druhá", "", ["Writer"])
    # A failing participant is skipped (no owner notice), the next one speaks.
    t = turn_task(conn, m["id"])
    api_worker._hand_back(conn, Ctx(team["a"]), t["id"], "worker error")
    assert meetings.view(conn, m["id"])["now_speaking"] == "Researcher"
    assert conn.execute("SELECT assignee_id FROM tasks WHERE id = ?", (t["id"],)).fetchone()[0] == team["a"]
    # A turn over its time is skipped by the tick.
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    conn.execute("UPDATE meeting_turns SET started_at = ? WHERE status = 'open'", (old,))
    conn.commit()
    meetings.tick(conn)
    assert meetings.view(conn, m["id"])["now_speaking"] == "Writer"  # round 2
    # Over the budget: straight to the decision.
    tid = turn_task(conn, m["id"])["id"]
    conn.execute("INSERT INTO runs (actor_id, kind, status, started_at, task_id, cost_usd) "
                 "VALUES (?, 'task', 'ok', 'x', ?, 1.5)", (team["a"], tid))
    conn.commit()
    meetings.tick(conn)
    v = meetings.view(conn, m["id"])
    assert v["now_speaking"] == "Book Lead" and v["turns"][-1]["kind"] == "decision"
    # The facilitator does not decide in time: the meeting closes without a decision.
    conn.execute("UPDATE meeting_turns SET started_at = ? WHERE status = 'open'", (old,))
    conn.commit()
    meetings.tick(conn)
    v = meetings.view(conn, m["id"])
    assert v["status"] == "closed" and v["decision"] is None and "did not decide" in v["close_reason"]


def test_past_its_duration_the_meeting_goes_to_the_decision(conn, me, team):
    m = meetings.start(conn, me, "kniha", "Čas", "", ["Writer", "Researcher"], rounds=2, facilitator="Book Lead")
    conn.execute("UPDATE meetings SET deadline_at = ? WHERE id = ?",
                 ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds"), m["id"]))
    conn.commit()
    meetings.tick(conn)
    assert meetings.view(conn, m["id"])["now_speaking"] == "Book Lead"
    # A plain message from the facilitator on its turn is the decision.
    chat.send(conn, Ctx(team["lead"]), team["ch"], "Rozhodnutí: jdeme s variantou A.", reply_to=m["thread"])
    v = meetings.view(conn, m["id"])
    assert v["status"] == "closed" and v["decision"].startswith("Rozhodnutí: jdeme")


def test_meeting_threads_are_exempt_from_loop_detection(conn, me, team, monkeypatch, tmp_path):
    monkeypatch.setenv("POS_CHAT_LOOP", "3/1800")
    m = meetings.start(conn, me, "kniha", "Debata", "", ["Writer", "Researcher"], rounds=3, facilitator="Book Lead")
    for i in range(6):
        who = "a" if i % 2 == 0 else "b"
        out = chat.send(conn, Ctx(team[who]), team["ch"], f"Argument {i}", reply_to=m["thread"])
        assert "back and forth" not in (out.get("platform_note") or "")
    assert not conn.execute("SELECT 1 FROM audit_log WHERE action = 'chat_loop'").fetchone()
    assert meetings.view(conn, m["id"])["now_speaking"] == "Book Lead"
    # The same exchange in an ordinary thread is a loop.
    x = make(conn, me, tmp_path, "Talker X", perms=("tasks:read", "messages:send"))
    y = make(conn, me, tmp_path, "Talker Y", perms=("tasks:read", "messages:send"))
    root = chat.send(conn, Ctx(x), team["ch"], "Mimo poradu: jedna věc")
    notes = []
    for i in range(4):
        who = y if i % 2 == 0 else x
        notes.append(chat.send(conn, Ctx(who), team["ch"], f"Odpověď {i}", reply_to=root["id"])
                     .get("platform_note") or "")
    assert any("back and forth" in n for n in notes)


def test_a_schedule_of_kind_meeting_starts_meetings(conn, me, team):
    s = schedules.create(conn, me, {"name": "Kniha: týdenní plánování", "schedule": "weekly mon 09:00",
                                    "kind": "meeting", "meeting": {"channel": "kniha", "topic": "Týdenní plán",
                                                                   "agenda": "- co dál", "rounds": 1,
                                                                   "participants": ["Writer", "Researcher"],
                                                                   "facilitator": "Book Lead"}})
    assert s["template"]["kind"] == "meeting" and s["assignee_id"] == team["lead"]
    first = schedules.fire(conn, s["id"])
    assert "meeting" in first
    v = meetings.view(conn, first["meeting"])
    assert v["topic"] == "Týdenní plán" and v["now_speaking"] == "Writer"
    assert "still runs" in schedules.fire(conn, s["id"])["skipped"]
    with pytest.raises(Exception):
        schedules.create(conn, me, {"name": "bad", "schedule": "daily 07:00", "kind": "meeting", "meeting": {}})


def test_meeting_in_a_project_channel_logs_to_the_project(conn, me, tmp_path):
    lead = make(conn, me, tmp_path, "Lead Two", team="atlas", perms=("tasks:read", "tasks:claim", "messages:send"))
    w = make(conn, me, tmp_path, "Writer Two", team="atlas", reports_to=lead)
    p = projects.create(conn, me, name="Atlas")
    ch = conn.execute("SELECT channel_id FROM projects WHERE id = ?", (p["id"],)).fetchone()[0]
    m = meetings.start(conn, Ctx(lead), ch, "Start", "", ["Writer Two"], rounds=1)
    assert meetings.view(conn, m["id"])["facilitator"] == "Lead Two"  # the channel's team lead by default
    chat.send(conn, Ctx(w), ch, "Navrhuju začít rešerší.", reply_to=m["thread"])
    out = meetings.decide(conn, Ctx(lead), m["id"], "Začneme rešerší.",
                          tasks_=[{"title": "Rešerše", "assignee": "Writer Two"}])
    t = conn.execute("SELECT project_id FROM tasks WHERE title = 'Rešerše'").fetchone()
    assert t["project_id"] == p["id"] and out["status"] == "closed"
    row = conn.execute("SELECT detail FROM audit_log WHERE action = 'meeting_decision'").fetchone()
    assert json.loads(row[0])["project"] == p["id"]
