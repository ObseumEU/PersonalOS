"""The reliability package (2026-10): SQLite settings and short write locks, idempotent
schedules and access-queue tasks, the 5-minute silent-run check with one requeue and then
the agent's lead, and the login rate limit. Incident recurrence: test_monitor.py."""

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from pos import actors, agents, auth, scheduler, schedules, tasks
from pos.access import service as access
from pos.config import Settings
from pos.core import Ctx
from pos.db import BUSY_TIMEOUT_MS, connect
from pos.main import create_app


def _ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat(timespec="seconds")


@pytest.fixture
def env(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, scheduler=False)
    client = TestClient(create_app(settings))
    client.__enter__()
    conn = connect(settings.db_path)
    owner = Ctx(actors.owner_id(conn))
    lead = agents.create_agent(conn, owner, name="Head of Dev", purpose="leads", lifetime="long_lived",
                               permissions=["tasks:read", "tasks:claim"], data_dir=tmp_path)["agent"]["id"]
    made = agents.create_agent(conn, owner, name="Builder", purpose="builds", lifetime="long_lived",
                               permissions=["tasks:read", "tasks:claim"], data_dir=tmp_path)
    conn.execute("UPDATE actors SET reports_to = ? WHERE id = ?", (lead, made["agent"]["id"]))
    conn.commit()
    yield {"client": client, "conn": conn, "owner": owner, "agent": made["agent"]["id"], "key": made["api_key"],
           "lead": lead, "db": settings.db_path, "tmp": tmp_path}
    conn.close()
    client.__exit__(None, None, None)


# ------------------------------------------------------------------ 1. SQLite

def test_connections_wait_for_the_lock_and_skip_the_fsync_per_commit(tmp_path):
    db = tmp_path / "x.db"
    conn = connect(db)
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS == 10_000
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    # A second writer meeting a held lock waits for it instead of "database is locked".
    holder = connect(db)
    holder.execute("INSERT INTO t VALUES (1)")  # opens the write transaction

    def release():
        time.sleep(0.5)
        holder.commit()

    threading.Thread(target=release).start()
    conn.execute("INSERT INTO t VALUES (2)")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2
    holder.close()
    conn.close()


def test_worker_calls_do_not_write_the_seen_time_every_tick(env):
    conn, h = env["conn"], {"Authorization": f"Bearer {env['key']}"}
    fresh = _ago(seconds=5)
    conn.execute("UPDATE actors SET last_seen_at = ? WHERE id = ?", (fresh, env["agent"]))
    conn.commit()
    assert env["client"].get("/api/worker/next?wait=0", headers=h).status_code == 200
    assert actors.get(conn, env["agent"])["last_seen_at"] == fresh       # a fresh one stays: no write
    conn.execute("UPDATE actors SET last_seen_at = ? WHERE id = ?", (_ago(minutes=2), env["agent"]))
    conn.commit()
    env["client"].get("/api/worker/next?wait=0", headers=h)
    assert actors.get(conn, env["agent"])["last_seen_at"] >= _ago(seconds=10)  # an old one moves


# ------------------------------------------------------------------ 2. idempotency

def test_a_schedule_is_unique_by_name_schedule_and_assignee(env):
    conn, owner = env["conn"], env["owner"]
    spec = {"name": "Daily standup", "schedule": "weekdays 08:15", "visibility": "team",
            "assignee": {"type": "agent", "id": env["agent"]}}
    first = schedules.create(conn, owner, spec)
    again = schedules.create(conn, owner, {**spec, "name": " daily STANDUP "})
    assert again["id"] == first["id"] and again["existing"] is True
    other_time = schedules.create(conn, owner, {**spec, "schedule": "weekdays 08:30"})
    assert other_time["id"] != first["id"]
    with pytest.raises(tasks.Invalid):  # moving it onto the first one would make a twin
        schedules.update(conn, owner, other_time["id"], {"schedule": "weekdays 08:15"})
    # Twins already in the data (before this rule): found for the one-off clean-up.
    conn.execute("INSERT INTO schedules (name, schedule, assignee_id, created_by, created_at, updated_at) "
                 "SELECT name, schedule, assignee_id, created_by, created_at, updated_at FROM schedules WHERE id = ?",
                 (first["id"],))
    assert [(k, len(d)) for k, d in schedules.duplicates(conn)] == [(first["id"], 1)]


def test_access_queue_tasks_fold_per_agent_and_kind(env):
    from pos import reliability_cleanup

    conn = env["conn"]
    am = access.manager_id(conn)

    def queue():
        return conn.execute("SELECT id, status, title FROM tasks WHERE assignee_id = ? AND source = 'access' "
                            "ORDER BY id", (am,)).fetchall()

    access.limit_hit(conn, env["agent"], "usd_day", 12.0, 10.0)
    conn.commit()
    [t] = queue()
    assert t["title"] == "Žádosti o přístup: Builder · limit usd_day"
    # The manager decides and closes it; the same agent hits the same limit again: the task comes back.
    conn.execute("UPDATE access_requests SET status = 'granted'")
    tasks.update(conn, Ctx(am), t["id"], {"status": "done", "progress_note": "raised"})
    conn.commit()
    access.limit_hit(conn, env["agent"], "usd_day", 13.0, 10.0)
    conn.commit()
    assert [(r["id"], r["status"]) for r in queue()] == [(t["id"], "next")]
    # While it is open, another request is a line on it.
    conn.execute("UPDATE access_requests SET status = 'granted'")
    access.limit_hit(conn, env["agent"], "runs_day", 50, 40)
    conn.commit()
    assert len(queue()) == 1
    assert reliability_cleanup.plan(conn)["access_queue"] == []


# ------------------------------------------------------------------ 3. silent runs

def _dead_run(conn, actor_id, task_id, minutes):
    rid = conn.execute("INSERT INTO runs (actor_id, task_id, kind, status, started_at, heartbeat_at, engine) "
                       "VALUES (?, ?, 'task', 'running', ?, ?, 'claude')",
                       (actor_id, task_id, _ago(minutes=minutes + 1), _ago(minutes=minutes))).lastrowid
    conn.commit()
    return rid


def test_a_silent_run_is_dead_after_5_min_requeued_once_then_its_lead_takes_over(env):
    conn, owner, agent, lead = env["conn"], env["owner"], env["agent"], env["lead"]
    t = tasks.create(conn, owner, {"title": "Rebuild the index", "assignee": {"type": "agent", "id": agent}})
    tasks.claim(conn, Ctx(agent), t["id"])
    live = _dead_run(conn, agent, t["id"], minutes=3)
    assert scheduler.reap_runs(conn)["released"] == []  # 3 min of silence is not death yet
    conn.execute("UPDATE runs SET heartbeat_at = ? WHERE id = ?", (_ago(minutes=6), live))
    conn.commit()
    out = scheduler.reap_runs(conn)
    assert out["released"] == [live] and out["requeued"] == [t["ref"]]
    assert tasks.get(conn, owner, t["id"])["status"] == "next"
    assert conn.execute("SELECT detail FROM runs WHERE id = ?", (live,)).fetchone()[0] == "worker went silent for 5 min"
    # The next run on it dies the same way: no third run, the agent's lead gets it.
    tasks.claim(conn, Ctx(agent), t["id"])
    again = _dead_run(conn, agent, t["id"], minutes=7)
    out = scheduler.reap_runs(conn)
    assert out["released"] == [again] and out["escalated"] == [t["ref"]]
    assert tasks.get(conn, owner, t["id"])["status"] == "waiting"
    dm = conn.execute("SELECT m.body FROM chat_inbox i JOIN chat_messages m ON m.id = i.message_id "
                      "WHERE i.actor_id = ? ORDER BY m.id DESC", (lead,)).fetchone()
    assert dm and t["ref"] in dm["body"] and "stopped without a word" in dm["body"]


def test_the_reaper_runs_every_minute_also_on_an_old_database(env):
    conn = env["conn"]
    conn.execute("UPDATE jobs SET schedule = 'every 5m' WHERE action = 'reap_runs'")
    conn.commit()
    scheduler.seed(conn)
    assert conn.execute("SELECT schedule FROM jobs WHERE action = 'reap_runs'").fetchone()[0] == "every 1m"


def test_the_alive_tick_covers_the_whole_run_from_the_claim(monkeypatch):
    pytest.importorskip("pos_worker")
    from pos_worker import loop

    monkeypatch.setattr(loop, "ALIVE_S", 0.01)
    ticks: list[int] = []

    class Client:
        def me(self):
            return {"id": 1, "profile": {}}

        def start_run(self, ref):
            return {"run_id": 9, "engine": "codex"}

        def claim(self, ref, run_id):
            return {}

        def alive(self, run_id):
            ticks.append(run_id)

        def triage(self, ref, verdict):
            return {"action": "parked"}

        def finish_run(self, *a, **k):
            return {}

    def slow_check(me, task):  # the cheap check before the session: no step, no heartbeat
        time.sleep(0.2)
        return {"verdict": "unclear", "jsonl": ""}

    w = loop.Worker(Client(), lambda *a: None, sleep=lambda s: None, triage=slow_check)
    assert w.handle_task({"ref": "T-1", "id": 1}) == "triaged"
    assert ticks and set(ticks) == {9}


# ------------------------------------------------------------------ 4. login rate limit

def test_login_is_rate_limited_per_ip_with_a_growing_backoff(tmp_path):
    now = [1000.0]
    limiter = auth.LoginLimiter(clock=lambda: now[0])
    for _ in range(auth.LoginLimiter.LIMIT - 1):
        limiter.failed("1.2.3.4")
    assert limiter.retry_after("1.2.3.4") == 0
    limiter.failed("1.2.3.4")
    assert limiter.retry_after("1.2.3.4") == 61 and limiter.retry_after("5.6.7.8") == 0
    now[0] += 61
    assert limiter.retry_after("1.2.3.4") == 0
    for _ in range(auth.LoginLimiter.LIMIT):
        limiter.failed("1.2.3.4")
    assert limiter.retry_after("1.2.3.4") == 121  # the second lock-out in a row is twice as long
    limiter.succeeded("1.2.3.4")
    assert limiter.retry_after("1.2.3.4") == 0

    with TestClient(create_app(Settings(data_dir=tmp_path, password="pw", session_secret="s"))) as client:
        h = {"X-Forwarded-For": "203.0.113.7, 172.18.0.5"}
        for _ in range(auth.LoginLimiter.LIMIT):
            assert client.post("/api/auth/login", json={"password": "nope"}, headers=h).status_code == 401
        r = client.post("/api/auth/login", json={"password": "pw"}, headers=h)
        assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0  # even the right one waits
        other = {"X-Forwarded-For": "198.51.100.1"}
        assert client.post("/api/auth/login", json={"password": "pw"}, headers=other).status_code == 200
