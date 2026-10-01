"""Heads see their team's stuck work (pos.head_alerts, T-417): the stuck_tasks
selection with every reason, the team filter, and the DM to the head on a failed
run, a budget stop or a task given to the owner, at most once in 6 hours."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from pos import actors, agents, head_alerts, tasks
from pos.config import Settings
from pos.core import Ctx, Forbidden, now_iso
from pos.db import connect
from pos.main import create_app


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("POS_CODEX_DISABLED", "1")
    settings = Settings(data_dir=tmp_path)
    client = TestClient(create_app(settings))
    client.__enter__()
    conn = connect(settings.db_path)
    owner = Ctx(actors.owner_id(conn))

    def agent(name, lead=None):
        out = agents.create_agent(conn, owner, name=name, purpose="work", lifetime="long_lived",
                                  permissions=["tasks:read", "tasks:claim", "tasks:write"], data_dir=tmp_path)
        conn.execute("UPDATE actors SET reports_to = ? WHERE id = ?", (lead, out["agent"]["id"]))
        return out["agent"]["id"], out["api_key"]

    head, _ = agent("Head Tester")
    dev, key = agent("Dev Tester", head)
    junior, _ = agent("Junior Tester", dev)
    other_head, _ = agent("Other Head")
    stranger, _ = agent("Stranger Tester", other_head)
    conn.commit()
    yield {"client": client, "conn": conn, "owner": owner, "head": head, "dev": dev, "key": key,
           "junior": junior, "other_head": other_head, "stranger": stranger}
    conn.close()
    client.__exit__(None, None, None)


def _ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


def _task(e, title, assignee, status="next", age=0):
    t = tasks.create(e["conn"], e["owner"], {"title": title, "status": status,
                                             "assignee": {"type": "agent", "id": assignee}})
    e["conn"].execute("UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?", (status, _ago(age), t["id"]))
    return t


def _run(e, task, actor, status, detail="", age=0):
    e["conn"].execute("INSERT INTO runs (actor_id, task_id, kind, status, started_at, ended_at, detail) "
                      "VALUES (?, ?, 'task', ?, ?, ?, ?)", (actor, task["id"], status, _ago(age), _ago(age), detail))


def _dms(conn, to):
    return [r["body"] for r in conn.execute(
        """SELECT m.body FROM chat_messages m JOIN channels c ON c.id = m.channel_id
           JOIN channel_members cm ON cm.channel_id = c.id AND cm.actor_id = ?
           WHERE c.kind = 'dm' AND m.author_id != ? ORDER BY m.id""", (to, to)).fetchall()]


def test_every_reason_is_found_and_fresh_work_is_not(env):
    e, conn = env, env["conn"]
    idle = _task(e, "Idle", e["dev"], "working", age=10)
    fresh = _task(e, "Fresh", e["dev"], "working", age=1)
    held = _task(e, "Held", e["junior"], "next", age=1)
    conn.execute("UPDATE tasks SET retry_after = ? WHERE id = ?",
                 ((datetime.now(timezone.utc) + timedelta(hours=5)).isoformat(timespec="seconds"), held["id"]))
    failed = _task(e, "Failed", e["dev"], "next", age=1)
    _run(e, failed, e["dev"], "error", "worker error: boom")
    broke = _task(e, "Broke", e["junior"], "next", age=1)
    _run(e, broke, e["junior"], "blocked", "budget usd_month reached ($31 of $30)")
    commented = _task(e, "Commented", e["dev"], "working", age=10)
    conn.execute("INSERT INTO task_comments (task_id, author_id, body, kind, created_by, created_at, updated_at) "
                 "VALUES (?, ?, 'progress', 'progress', ?, ?, ?)",
                 (commented["id"], e["dev"], e["dev"], now_iso(), now_iso()))
    killed = _task(e, "Killed", e["dev"], "next", age=1)
    _run(e, killed, e["dev"], "blocked", "kill switch is on")
    conn.commit()

    out = head_alerts.stuck(conn, e["head"], hours=6)
    got = {x["ref"]: x["reason"] for x in out["stuck"]}
    assert got == {idle["ref"]: "no_activity", held["ref"]: "retry_after", failed["ref"]: "last_run_failed",
                   broke["ref"]: "budget_exhausted"}
    assert fresh["ref"] not in got and commented["ref"] not in got and killed["ref"] not in got
    row = next(x for x in out["stuck"] if x["ref"] == broke["ref"])
    assert row["last_run"]["result"] == "budget" and row["assignee"] == "Junior Tester"
    assert next(x for x in out["stuck"] if x["ref"] == idle["ref"])["hours_idle"] >= 9.9


def test_a_head_sees_only_its_own_team(env):
    e, conn = env, env["conn"]
    mine = _task(e, "Mine", e["junior"], "working", age=10)
    theirs = _task(e, "Theirs", e["stranger"], "working", age=10)
    conn.commit()
    assert [x["ref"] for x in head_alerts.stuck(conn, e["head"])["stuck"]] == [mine["ref"]]
    assert [x["ref"] for x in head_alerts.stuck(conn, e["dev"])["stuck"]] == [mine["ref"]]  # junior is below dev
    with pytest.raises(Forbidden):
        head_alerts.stuck(conn, e["head"], team="Other Head")
    assert head_alerts.stuck(conn, e["head"], team="Dev Tester")["team"] == "Dev Tester"  # a sub-team: fine
    conn.execute("UPDATE actors SET role = 'coo' WHERE id = ?", (e["head"],))
    assert [x["ref"] for x in head_alerts.stuck(conn, e["head"], team="Other Head")["stuck"]] == [theirs["ref"]]
    assert [x["ref"] for x in head_alerts.stuck(conn, e["owner"].actor_id, team="Other Head")["stuck"]] == [theirs["ref"]]


def test_a_failed_run_dms_the_head_once_in_six_hours(env):
    e, conn, client = env, env["conn"], env["client"]
    t = _task(e, "Opravit export", e["dev"], "working")
    conn.execute("UPDATE tasks SET status = 'working' WHERE id = ?", (t["id"],))
    h = {"Authorization": f"Bearer {e['key']}"}
    for _ in range(2):
        rid = conn.execute("INSERT INTO runs (actor_id, task_id, kind, status, started_at) "
                           "VALUES (?, ?, 'task', 'running', ?)", (e["dev"], t["id"], now_iso())).lastrowid
        conn.commit()
        r = client.post(f"/api/worker/runs/{rid}/finish", headers=h,
                        json={"status": "error", "detail": "worker error: timeout"})
        assert r.status_code == 200
    dms = _dms(conn, e["head"])
    assert len(dms) == 1
    assert t["ref"] in dms[0] and "Dev Tester" in dms[0] and "skončil chybou" in dms[0] and "timeout" in dms[0]
    assert "Další automatický pokus" in dms[0]  # the back-off is set before the alert
    assert _dms(conn, e["owner"].actor_id) == []  # never the owner
    # another reason on the same task is its own alert
    assert head_alerts.run_ended(conn, e["dev"], t["id"], "blocked", "budget usd_day reached") is not None
    assert "limit rozpočtu" in _dms(conn, e["head"])[-1]


def test_no_alert_for_a_manual_stop_the_kill_switch_or_a_lead_who_is_the_owner(env):
    e, conn = env, env["conn"]
    t = _task(e, "Něco", e["dev"])
    assert head_alerts.run_ended(conn, e["dev"], t["id"], "cancelled", "stopped") is None
    assert head_alerts.run_ended(conn, e["dev"], t["id"], "blocked", "kill switch is on") is None
    assert head_alerts.run_ended(conn, e["dev"], t["id"], "blocked", "agent is paused") is None
    assert head_alerts.run_ended(conn, e["head"], t["id"], "error", "boom") is None  # head reports to nobody
    conn.execute("UPDATE actors SET reports_to = ? WHERE id = ?", (e["owner"].actor_id, e["head"]))
    assert head_alerts.run_ended(conn, e["head"], t["id"], "error", "boom") is None  # the owner is not alerted
    assert _dms(conn, e["owner"].actor_id) == []


def test_a_task_an_agent_gives_the_owner_tells_its_head_and_is_listed(env):
    e, conn = env, env["conn"]
    owner_id = e["owner"].actor_id
    created = tasks.create(conn, Ctx(e["dev"]), {"title": "Zaplatit doménu", "status": "next",
                                                 "assignee": {"type": "human", "id": owner_id}})
    moved = _task(e, "Rozhodnout o API", e["junior"])
    tasks.assign(conn, Ctx(e["junior"]), moved["id"], {"type": "human", "id": owner_id})
    ticket = tasks.create(conn, Ctx(e["dev"]), {"title": "Otázka", "status": "next", "source": "ask_owner",
                                                "assignee": {"type": "human", "id": owner_id}})
    by_owner = tasks.create(conn, e["owner"], {"title": "Moje", "status": "next"})
    conn.commit()
    dms = _dms(conn, e["head"])
    assert len(dms) == 1 and created["ref"] in dms[0] and "Ownerovi" in dms[0]
    assert len(_dms(conn, e["dev"])) == 1 and moved["ref"] in _dms(conn, e["dev"])[0]  # junior's head is dev
    got = {x["ref"]: x["reason"] for x in head_alerts.stuck(conn, e["head"])["stuck"]}
    assert got == {created["ref"]: "owner_assigned", moved["ref"]: "owner_assigned"}
    assert ticket["ref"] not in got and by_owner["ref"] not in got
    assert head_alerts.stuck(conn, e["head"], include_owner_assigned=False)["stuck"] == []
