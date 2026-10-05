"""Regressions of 2026-09-27 in the worker, the agent pool, chat and the scheduler:
the pool woke only for tasks, routines stopped with the switched-off jobs, runs died in
a short API outage, /alive did not keep a run alive, chat questions were answered twice,
blocked agents held every pool slot, a daily job fired all through the October DST hour,
and the chat and the Team page disagreed about who is working."""

from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from pos import actors, agents, agents_code, availability, chat, fastlane, scheduler, schedules, tasks, workers
from pos.core import now_iso

pytest.importorskip("pos_worker")
from pos_worker import pool as wpool  # noqa: E402
from pos_worker.client import Blocked  # noqa: E402
from pos_worker.loop import Worker  # noqa: E402
from test_a2a_scheduler import app  # noqa: E402,F401 - fixture
from test_chat_live_run import _age, _busy_agent, clean_memory, db  # noqa: E402,F401 - fixtures
from test_worker import setup  # noqa: E402,F401 - fixture


def _ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat(timespec="seconds")


class Proc:
    def __init__(self, code=None):
        self.returncode = code

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode


def _keys(root, *slugs):
    for s in slugs:
        (root / s).mkdir(parents=True, exist_ok=True)
        (root / s / "key").write_text("k-" + s)


# ------------------------------------------------------------------ 1. the pool wakes for messages

def test_pool_wakes_an_agent_for_unread_messages_not_only_tasks():
    assert wpool.work_rank({"state": {}, "unread_messages": 0}) is None
    assert wpool.work_rank({"state": {}, "unread_messages": 2}) == wpool.RANK_MESSAGES  # a review request, a DM
    assert wpool.work_rank({"state": {"paused": True}, "unread_messages": 2}) is None
    assert wpool.work_rank({"task": {"topic": "chat", "priority": 1}}) == wpool.RANK_OWNER
    assert wpool.work_rank({"task": {"topic": "chat", "priority": 2}}) == wpool.RANK_CHAT
    assert wpool.work_rank({"task": {"topic": "dev", "priority": 1}, "unread_messages": 0}) == wpool.RANK_TASK


def test_pool_probe_reads_unread_messages(monkeypatch):
    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"state": {}, "unread_messages": 1}

    monkeypatch.setattr(httpx, "get", lambda *a, **k: R())
    assert wpool.has_work("http://api", "k") == wpool.RANK_MESSAGES


# ------------------------------------------------------------------ 7. fair slots, blocked agents let go

def test_full_pool_starts_the_owners_message_first_then_round_robin(tmp_path):
    _keys(tmp_path, "a", "b", "c")
    ranks = {"a": wpool.RANK_TASK, "b": wpool.RANK_TASK, "c": wpool.RANK_OWNER}
    clock = [0.0]
    pool = wpool.Pool(tmp_path, tmp_path / "work", spawn=lambda s, k: Proc(), lazy=True, max_running=1,
                      probe=lambda s, k: ranks[s], clock=lambda: clock[0])
    assert pool.tick()["started"] == ["c"]                            # the owner waits: first
    pool.children["c"][0].returncode = 0
    ranks["c"] = None
    clock[0] += 15
    assert pool.tick()["started"] == ["a"]
    pool.children["a"][0].returncode = 0
    clock[0] += 15
    assert pool.tick()["started"] == ["b"]                            # b has waited longest: its turn
    pool.children["b"][0].returncode = 0
    clock[0] += 15
    assert pool.tick()["started"] == ["a"]


def test_a_blocked_worker_gives_its_slot_back_for_a_while(tmp_path):
    _keys(tmp_path, "a", "b")
    clock = [0.0]
    pool = wpool.Pool(tmp_path, tmp_path / "work", spawn=lambda s, k: Proc(), lazy=True, max_running=1,
                      probe=lambda s, k: True, clock=lambda: clock[0])
    assert pool.tick()["started"] == ["a"]
    pool.children["a"][0].returncode = wpool.BLOCKED_EXIT            # over budget: it ended at once
    clock[0] += 15
    assert pool.tick()["started"] == ["b"] and pool.failures == {}
    pool.children["b"][0].returncode = 0
    clock[0] += 15
    assert pool.tick()["started"] == ["b"]                            # a waits out its back-off
    pool.children["b"][0].returncode = 0
    clock[0] += wpool.BLOCKED_BACKOFF_S
    assert "a" in pool.tick()["started"]


def test_a_refused_run_ends_the_pool_worker_instead_of_holding_the_slot():
    class Client:
        def me(self):
            return {"name": "Drahý", "id": 1}

        def next_work(self, wait):
            return {"task": {"ref": "T-1"}}

        def start_run(self, ref):
            raise Blocked("over budget")

    slept = []
    w = Worker(Client(), lambda *a: None, poll_wait=30, sleep=slept.append, exit_idle_s=120, clock=lambda: 0.0)
    assert w.run_forever() == "blocked" and slept == []
    # Without the pool (exit_idle_s 0) it waits and keeps trying, as before.
    w2 = Worker(Client(), lambda *a: None, poll_wait=30, sleep=slept.append)
    assert w2.step() == "blocked" and slept == [30]


def test_a_task_with_a_live_run_is_skipped_not_a_refusal_of_the_agent():
    class Client:
        def me(self):
            return {"name": "Drahý", "id": 1}

        def next_work(self, wait):
            return {"task": {"ref": "T-1"}}

        def start_run(self, ref):
            raise Blocked("T-1 already has a live run")

    slept = []
    w = Worker(Client(), lambda *a: None, poll_wait=30, sleep=slept.append, exit_idle_s=120, clock=lambda: 0.0)
    assert w.step() == "skipped" and slept == []  # no wait, the pool slot is kept


def test_a_next_task_with_a_live_run_is_not_offered_again(setup, monkeypatch):
    """142 refused POST /runs in 72 h ("already has a live run"): /next offered a `next` task whose run
    was still going (started, not yet claimed; or the task set back to `next` during its run)."""
    monkeypatch.setenv("POS_AGENT_RUNTIME", "codex")
    monkeypatch.delenv("POS_CODEX_DISABLED")
    client, conn, owner, agent_id, key = setup
    t = tasks.create(conn, owner, {"title": "Race", "assignee": {"type": "agent", "id": agent_id}})
    conn.commit()
    h = {"Authorization": f"Bearer {key}"}
    rid = client.post("/api/worker/runs", json={"task_id": t["ref"], "kind": "task"}, headers=h).json()["run_id"]
    assert conn.execute("SELECT status FROM tasks WHERE id = ?", (t["id"],)).fetchone()[0] == "next"  # not claimed yet
    assert "task" not in client.get("/api/worker/next?wait=0", headers=h).json()
    # claimed, then set back to `next` while the run goes on: still not offered
    assert client.post(f"/api/worker/tasks/{t['ref']}/claim?run_id={rid}", headers=h).status_code == 200
    conn.execute("UPDATE tasks SET status = 'next' WHERE id = ?", (t["id"],))
    conn.commit()
    assert "task" not in client.get("/api/worker/next?wait=0", headers=h).json()
    # the run ends: offered again
    conn.execute("UPDATE runs SET status = 'ok', ended_at = ? WHERE id = ?", (_ago(minutes=0), rid))
    conn.commit()
    assert client.get("/api/worker/next?wait=0", headers=h).json()["task"]["ref"] == t["ref"]


def test_blocked_and_skipped_steps_do_not_keep_a_worker_alive():
    steps = iter(["skipped", "skipped", "skipped", "skipped", "skipped"])
    now = [0.0]

    class Client:
        def me(self):
            return {"name": "x", "id": 1}

    w = Worker(Client(), lambda *a: None, exit_idle_s=100, clock=lambda: now[0])

    def step():
        now[0] += 40
        return next(steps)

    w.step = step
    assert w.run_forever() == "idle" and now[0] <= 160


# ------------------------------------------------------------------ 3. a short API outage

class Session:
    failed = ""
    jsonl = ""
    last_message = "Hotovo."

    def run(self, prompt):
        for i in range(3):
            yield {"type": "item.completed", "item": {"type": "command_execution", "command": f"step {i}"}}

    def stop(self):
        pass


class FlakyClient:
    """PersonalOS restarts: every call fails `down` times first."""

    def __init__(self, down=2):
        self.down = {"heartbeat": down, "inbox": 100, "finish_run": down, "task": down, "complete": down}
        self.calls = []

    def _maybe_fail(self, name):
        self.calls.append(name)
        if self.down.get(name, 0) > 0:
            self.down[name] -= 1
            raise httpx.ConnectError("connection refused")

    def heartbeat(self, run_id, step=None, steps=None):
        self._maybe_fail("heartbeat")
        return {}

    def inbox(self, run_id=None):
        self._maybe_fail("inbox")
        return []

    def finish_run(self, run_id, status, jsonl, detail=""):
        self._maybe_fail("finish_run")
        self.finished = status
        return {}

    def task(self, ref):
        self._maybe_fail("task")
        return {"assignee_id": 1, "status": "working"}

    def complete(self, ref, note):
        self._maybe_fail("complete")
        self.completed = note
        return {}


def test_a_run_survives_a_short_api_outage_and_still_finishes():
    client, slept = FlakyClient(), []
    w = Worker(client, lambda *a: None, sleep=slept.append)
    w.me = {"id": 1}
    session = Session()
    outcome = w._session_loop(session, "go", 7, "T-1")
    assert outcome == "ok"                                            # inbox never came back: the run went on
    assert client.calls.count("heartbeat") == 5                       # 2 failures + 3 steps
    assert w._after_run("T-1", 7, "codex", session, None, outcome) == "ok"
    assert client.finished == "ok" and client.completed == "Hotovo."  # finish_run retried, not lost
    assert slept and max(slept) <= 60


def test_only_transient_errors_are_retried():
    w = Worker(object(), lambda *a: None, sleep=lambda s: None)
    calls = []

    def refused():
        calls.append(1)
        raise Blocked("no")

    with pytest.raises(Blocked):
        w._call(refused)
    assert calls == [1]

    def gone():
        calls.append(2)
        raise httpx.ConnectError("down")

    with pytest.raises(httpx.ConnectError):
        w._call(gone, delays=(0, 0))
    assert calls.count(2) == 3


def test_a_run_whose_worker_went_silent_is_no_proof_of_a_live_worker(db, tmp_path):
    owner, aid, t, run = _busy_agent(db, tmp_path)
    db.execute("UPDATE actors SET runtime = 'codex_worker', last_seen_at = ? WHERE id = ?", (_ago(hours=1), aid))
    db.commit()
    assert workers.worker_down(db, actors.get(db, aid), stale_s=600) is None    # fresh heartbeat
    db.execute("UPDATE runs SET heartbeat_at = ? WHERE id = ?", (_ago(minutes=30), run))
    db.commit()
    assert workers.worker_down(db, actors.get(db, aid), stale_s=600)            # "running", but silent


# ------------------------------------------------------------------ 4. /alive keeps the run alive

def test_alive_moves_the_heartbeat_so_a_long_step_is_not_reaped_or_offered_again(setup, monkeypatch):
    monkeypatch.setenv("POS_AGENT_RUNTIME", "codex")
    monkeypatch.delenv("POS_CODEX_DISABLED")
    client, conn, owner, agent_id, key = setup
    t = tasks.create(conn, owner, {"title": "Long build", "assignee": {"type": "agent", "id": agent_id}})
    conn.commit()
    h = {"Authorization": f"Bearer {key}"}
    rid = client.post("/api/worker/runs", json={"task_id": t["ref"], "kind": "task"}, headers=h).json()["run_id"]
    assert client.post(f"/api/worker/tasks/{t['ref']}/claim?run_id={rid}", headers=h).status_code == 200
    # A 25-minute step: no heartbeat since, only the alive tick.
    conn.execute("UPDATE runs SET heartbeat_at = ?, started_at = ?, engine = 'codex' WHERE id = ?",
                 (_ago(minutes=25), _ago(minutes=30), rid))
    conn.commit()
    assert client.post(f"/api/worker/runs/{rid}/alive", headers=h).json() == {"ok": True}
    beat = conn.execute("SELECT heartbeat_at FROM runs WHERE id = ?", (rid,)).fetchone()[0]
    assert beat >= _ago(minutes=1)
    assert scheduler.reap_runs(conn)["released"] == []
    assert "task" not in client.get("/api/worker/next?wait=0", headers=h).json()  # not offered to a second worker


# ------------------------------------------------------------------ 5. no double answer from the chat run

def test_the_chat_run_does_not_get_its_own_question_injected(setup, monkeypatch):
    monkeypatch.setenv("POS_AGENT_RUNTIME", "codex")
    monkeypatch.delenv("POS_CODEX_DISABLED")
    monkeypatch.setattr(availability, "autoreply", lambda *a, **k: None)
    client, conn, owner, agent_id, key = setup
    conn.execute("UPDATE actors SET runtime = 'codex_worker' WHERE id = ?", (agent_id,))
    conn.commit()
    monkeypatch.setattr(workers, "reply_path", lambda c, row, *a: {"kind": "pool", "name": "pool/x"})
    msg = chat.send_dm(conn, owner, agent_id, "Kolik testů padá?")
    task = conn.execute("SELECT id FROM tasks WHERE assignee_id = ? AND topic = 'chat'", (agent_id,)).fetchone()
    assert task is not None
    h = {"Authorization": f"Bearer {key}"}
    ref = tasks.display_id(task["id"])
    rid = client.post("/api/worker/runs", json={"task_id": ref, "kind": "task"}, headers=h).json()["run_id"]
    row = conn.execute("SELECT read_at, acked_at FROM chat_inbox WHERE message_id = ? AND actor_id = ?",
                       (msg["id"], agent_id)).fetchone()
    assert row["read_at"] and row["acked_at"]                          # taken with the task, acked
    assert client.get(f"/api/worker/inbox?run_id={rid}", headers=h).json() == []
    # A follow-up joins the task: the prompt does not have it, so the run still gets it.
    more = chat.send_dm(conn, owner, agent_id, "A kolik jich prochází?")
    assert [m["id"] for m in client.get(f"/api/worker/inbox?run_id={rid}", headers=h).json()] == [more["id"]]


def test_an_older_run_on_the_chat_task_never_injects_its_own_question(db, tmp_path, monkeypatch):
    owner, aid, t, run = _busy_agent(db, tmp_path)
    monkeypatch.setattr(agents_code, "has_worker", lambda name: name == "Stavitel")
    monkeypatch.setattr(availability, "autoreply", lambda *a, **k: None)
    msg = chat.send_dm(db, owner, aid, "Stihneš to dnes?")
    chat_task = db.execute("SELECT id FROM tasks WHERE assignee_id = ? AND topic = 'chat'", (aid,)).fetchone()["id"]
    run2 = db.execute("INSERT INTO runs (actor_id, task_id, kind, status, started_at, heartbeat_at) "
                      "VALUES (?, ?, 'task', 'running', ?, ?)", (aid, chat_task, now_iso(), now_iso())).lastrowid
    db.commit()
    assert chat.check_inbox(db, aid, run_id=run2) == []
    assert db.execute("SELECT read_at FROM chat_inbox WHERE message_id = ?", (msg["id"],)).fetchone()[0]


# ------------------------------------------------------------------ 6. the fast lane and the chat run

def test_fast_lane_leaves_a_message_to_the_run_answering_it(db, tmp_path, monkeypatch):
    owner, aid, t, run = _busy_agent(db, tmp_path)
    monkeypatch.setattr(agents_code, "has_worker", lambda name: name == "Stavitel")
    monkeypatch.setattr(availability, "autoreply", lambda *a, **k: None)
    monkeypatch.setattr(fastlane, "_llm_blocked", lambda conn, a: "off")
    db.execute("UPDATE runs SET status = 'ok' WHERE id = ?", (run,))  # the build run ended
    msg = chat.send_dm(db, owner, aid, "Jak to jde?")
    chat_task = db.execute("SELECT id FROM tasks WHERE assignee_id = ? AND topic = 'chat'", (aid,)).fetchone()["id"]
    chat_run = db.execute("INSERT INTO runs (actor_id, task_id, kind, status, started_at, heartbeat_at) "
                          "VALUES (?, ?, 'task', 'running', ?, ?)",
                          (aid, chat_task, _ago(minutes=2), now_iso())).lastrowid
    db.commit()
    _age(db, msg["id"], 90)
    assert fastlane.due(db) == [] and fastlane.respond(db, aid, msg["id"]) is None
    assert fastlane.answers_it(db, db.execute("SELECT * FROM runs WHERE id = ?", (chat_run,)).fetchone(), msg["id"])


def test_fast_lane_waits_until_the_run_has_been_going_30_s(db, tmp_path, monkeypatch):
    owner, aid, t, run = _busy_agent(db, tmp_path)
    monkeypatch.setattr(fastlane, "_llm_blocked", lambda conn, a: "off")
    db.execute("UPDATE runs SET started_at = ? WHERE id = ?", (_ago(seconds=10), run))  # the pool just started it
    db.commit()
    g = chat.create_channel(db, owner, "#build", [aid])
    msg = chat.send(db, owner, g["id"], "@Stavitel jak daleko jsi?")
    _age(db, msg["id"], 60)
    assert fastlane.due(db) == [] and fastlane.respond(db, aid, msg["id"]) is None
    db.execute("UPDATE runs SET started_at = ? WHERE id = ?", (_ago(minutes=2), run))
    db.commit()
    assert fastlane.due(db) == [(aid, msg["id"])]


# ------------------------------------------------------------------ 9. one "working" state

def test_team_page_chat_and_org_chart_share_one_working_state(db, tmp_path):
    owner, aid, t, run = _busy_agent(db, tmp_path)
    from pos import org

    me = next(a for a in agents.overview(db) if a["id"] == aid)
    assert me["status"] == "working" and me["working_on"]["task_ref"] == t["ref"]
    assert me["working_on"]["since"] == db.execute("SELECT started_at FROM runs WHERE id = ?", (run,)).fetchone()[0]
    assert aid in chat.working_ids(db)
    assert next(m for m in org.chart(db) if m["id"] == aid)["status"] == "working"
    # The worker went silent: the run row still says running, the task says working; nobody says working.
    db.execute("UPDATE runs SET heartbeat_at = ? WHERE id = ?", (_ago(minutes=30), run))
    db.execute("UPDATE tasks SET status = 'working' WHERE id = ?", (t["id"],))
    db.commit()
    me = next(a for a in agents.overview(db) if a["id"] == aid)
    assert me["status"] == "idle" and me["working_on"] is None
    assert aid not in chat.working_ids(db)
    assert next(m for m in org.chart(db) if m["id"] == aid)["status"] == "idle"
    assert next(m for m in chat.members_overview(db) if m["id"] == aid)["working"] is False


# ------------------------------------------------------------------ 8. daylight saving time

@pytest.mark.parametrize("day", [date(2026, 3, 29), date(2026, 10, 25)])
@pytest.mark.parametrize("schedule", ["daily 02:30", "daily 02:00", "daily 03:00", "weekly sun 02:30",
                                      "every 1d 02:30", "weekdays 07:00", "daily 00:00"])
def test_next_run_is_always_in_the_future_across_both_dst_changes(day, schedule):
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) - timedelta(hours=12)
    for minute in range(0, 36 * 60, 5):
        after = start + timedelta(minutes=minute)
        assert scheduler.next_run(schedule, after) > after, (schedule, after)
    # A scheduler ticking every 30 s fires a daily job once on the change day, not every tick.
    fired, now, due = [], start, scheduler.next_run(schedule, start)
    while now < start + timedelta(hours=36):
        if due <= now:
            fired.append(due)
            due = scheduler.next_run(schedule, now)
        now += timedelta(seconds=30)
    assert len(fired) <= 3 and len(set(f.astimezone(scheduler.TZ).date() for f in fired)) == len(fired)


def test_daily_0230_on_the_october_change_fires_once_at_the_first_0230():
    base = datetime(2026, 10, 24, 20, 0, tzinfo=timezone.utc)
    first = scheduler.next_run("daily 02:30", base)
    assert first == datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)   # 02:30 CEST
    assert scheduler.next_run("daily 02:30", first) == datetime(2026, 10, 26, 1, 30, tzinfo=timezone.utc)
    # March: 02:30 does not exist; it fires once, at 03:30 CEST.
    spring = scheduler.next_run("daily 02:30", datetime(2026, 3, 28, 20, 0, tzinfo=timezone.utc))
    assert spring == datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc)


# ------------------------------------------------------------------ 2. routines always run

def test_core_jobs_cannot_be_switched_off_and_still_run_when_the_row_says_off(app, monkeypatch):
    client, conn, me = app
    jobs = {j["action"]: j for j in scheduler.list_jobs(conn)}
    for action in ("member_schedules", "reap_runs", "budget_check", "routines_overdue"):
        assert jobs[action]["core"] is True
        with pytest.raises(tasks.Invalid):
            scheduler.update_job(conn, me, jobs[action]["id"], {"enabled": False})
    assert jobs["morning_brief"]["core"] is False
    scheduler.update_job(conn, me, jobs["morning_brief"]["id"], {"enabled": False})  # others still can
    # Switched off before this change (prod: jobs 1-9 on 2026-09-25): it runs anyway...
    conn.execute("UPDATE jobs SET enabled = 0, next_run_at = ? WHERE action = 'member_schedules'", (_ago(minutes=1),))
    conn.commit()
    ran = []
    monkeypatch.setitem(scheduler.ACTIONS, "member_schedules", lambda c: ran.append(1) or {})
    assert "member_schedules" in scheduler.run_due(conn) and ran == [1]
    # ...and start-up switches it back on, with an audit line.
    assert scheduler.enforce_core(conn) == ["member_schedules"]
    assert conn.execute("SELECT enabled FROM jobs WHERE action = 'member_schedules'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'job_update' "
                        "AND json_extract(detail, '$.reason') LIKE 'core job%'").fetchone()[0] == 1


def test_a_routine_an_hour_late_is_an_incident_for_the_sre_once(app, monkeypatch):
    client, conn, me = app
    filed = []
    monkeypatch.setattr(workers, "_incident", lambda c, **kw: filed.append(kw) or {})
    s = schedules.create(conn, me, {"name": "Ranní kontrola", "schedule": "daily 07:00"})
    assert schedules.watch_overdue(conn) == {"late": []} and filed == []
    conn.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?", (_ago(minutes=30), s["id"]))
    conn.commit()
    assert schedules.watch_overdue(conn)["late"] == [] and filed == []          # 30 min: not yet
    conn.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?", (_ago(minutes=90), s["id"]))
    conn.commit()
    out = schedules.watch_overdue(conn)
    assert out["late"] == [s["id"]] and out["alerted"] and len(filed) == 1
    assert filed[0]["kind"] == "routine_overdue" and "Ranní kontrola" in filed[0]["body"]
    assert "alerted" not in schedules.watch_overdue(conn) and len(filed) == 1  # once
    schedules.fire(conn, s["id"])                                               # the loop runs again
    schedules.watch_overdue(conn)
    assert filed[-1].get("resolved") is True and filed[-1]["iid"] == filed[0]["iid"]
    # It is a scheduler job of its own, and a core one.
    assert "routines_overdue" in scheduler.ACTIONS and "routines_overdue" in scheduler.CORE_JOBS
