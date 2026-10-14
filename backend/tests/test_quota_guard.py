"""The agent pool's Claude quota (pos.quota_guard, T-1288): measured from rate_limit_event, warnings at 80 / 95 %
once per window, one message when spent, the owner's reserve from 90 % and the order after the reset
(prod 2026-10-08..11: the weekly window ran out unwarned and the company stood 41 h)."""

from datetime import datetime, timedelta, timezone

import pytest

from pos import actors, agents, api_worker, business, engines, integrations, monitor, quota_guard, scorecard, tasks
from pos.core import Ctx, now_iso
from pos.db import connect, migrate


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("POS_AGENT_RUNTIME", "claude")
    monkeypatch.delenv("POS_QUOTA_RESERVE_PCT", raising=False)
    c = connect(tmp_path / "q.db")
    migrate(c)
    actors.ensure_builtin(c)
    integrations.register_builtin_agents(c)
    business.ensure_schema(c)
    c.commit()
    yield c
    c.close()


@pytest.fixture
def owner(conn):
    return Ctx(actors.owner_id(conn), via="api")


def _agent(conn, owner, tmp_path, name, role) -> int:
    found = actors.find_by_name(conn, name)
    if found is not None:
        aid = found["id"]
    else:
        aid = agents.create_agent(conn, owner, name=name, purpose=name, lifetime="long_lived", data_dir=tmp_path,
                                  permissions=["tasks:read", "tasks:write", "tasks:claim"])["agent"]["id"]
    conn.execute("UPDATE actors SET role = ? WHERE id = ?", (role, aid))
    conn.commit()
    return aid


@pytest.fixture
def team(conn, owner, tmp_path):
    return {"ceo": _agent(conn, owner, tmp_path, "CEO", "ceo"),
            "monitor": _agent(conn, owner, tmp_path, monitor.NAME, "oncall"),
            "se": _agent(conn, owner, tmp_path, "Software Engineer", "developer")}


def _ts(d: datetime) -> str:
    return d.isoformat(timespec="seconds")


def _quota(conn, pct, *, window="seven_day", resets=None, status="allowed_warning"):
    resets = resets or datetime.now(timezone.utc) + timedelta(days=2)
    quota_guard.record(conn, {"status": status, "rateLimitType": window, "utilization": pct / 100}, _ts(resets))
    conn.commit()
    return resets


def _incidents(conn):
    return conn.execute("SELECT * FROM tasks WHERE source = 'quota_guard' ORDER BY id").fetchall()


def _ceo_dms(conn):
    return conn.execute("SELECT body FROM chat_messages WHERE body LIKE '%Kvóta agent-pool%' ORDER BY id").fetchall()


# ------------------------------------------------------------------ measuring

@pytest.mark.parametrize("info, pct", [
    ({"utilization": 0.83}, 83.0), ({"utilization": 83}, 83.0), ({"status": "rejected"}, 100.0),
    ({"status": "allowed_warning"}, 80.0), ({"status": "allowed"}, None), ({"utilization": 140}, 100.0),
])
def test_pct_from_rate_limit_info(info, pct):
    assert quota_guard.pct_of(info) == pct


def test_a_claude_run_records_its_window_and_the_share_only_grows_within_it(conn, team):
    resets = int((datetime.now(timezone.utc) + timedelta(days=3)).timestamp())
    conn.execute("INSERT INTO runs (id, actor_id, kind, status, started_at) VALUES (7, ?, 'task', 'ok', ?)",
                 (team["se"], now_iso()))
    run = conn.execute("SELECT * FROM runs WHERE id = 7").fetchone()
    line = ('{"type": "rate_limit_event", "rate_limit_info": {"status": "allowed_warning", '
            f'"resetsAt": {resets}, "rateLimitType": "seven_day", "utilization": 0.86}}}}')
    engines.record_claude(conn, run, line)
    assert quota_guard.current(conn)["pct"] == 86.0
    # a later run in the same window without a number keeps the measured share
    engines.record_claude(conn, run, line.replace(', "utilization": 0.86', "").replace("allowed_warning", "allowed"))
    assert quota_guard.current(conn) == {"pct": 86.0, "window": "seven_day", "resets_at": _ts(
        datetime.fromtimestamp(resets, timezone.utc)), "status": "allowed"}
    # a new window starts from its own number
    _quota(conn, 12, resets=datetime.now(timezone.utc) + timedelta(days=7))
    assert quota_guard.current(conn)["pct"] == 12.0


def test_current_ignores_ended_windows_and_a_paused_claude_is_spent(conn, team):
    _quota(conn, 97, window="five_hour", resets=datetime.now(timezone.utc) - timedelta(minutes=1))
    _quota(conn, 40)
    assert quota_guard.current(conn)["pct"] == 40.0
    until = _ts(datetime.now(timezone.utc) + timedelta(hours=5))
    engines.pause(conn, "claude", until, "Claude AI usage limit reached")
    assert quota_guard.current(conn) == {"pct": 100.0, "window": "limit", "resets_at": until, "status": "rejected"}
    # a broken CLI (the self-check) is not the quota
    engines.pause(conn, "claude", until, "self-check failed (claude-opus-5-5): not logged in")
    assert quota_guard.current(conn)["pct"] == 40.0


# ------------------------------------------------------------------ thresholds and dedup

def test_warnings_at_80_and_95_once_per_window_and_one_message_when_spent(conn, team):
    _quota(conn, 79)
    assert quota_guard.check(conn)["new"] == [] and not _incidents(conn) and not _ceo_dms(conn)

    resets = _quota(conn, 81)
    assert quota_guard.check(conn)["told"] == 80
    inc = _incidents(conn)
    assert len(inc) == 1 and inc[0]["assignee_id"] == team["monitor"] and inc[0]["topic"] == "provoz" \
        and inc[0]["priority"] == 2
    assert len(_ceo_dms(conn)) == 1
    _quota(conn, 84, resets=resets)
    assert "told" not in quota_guard.check(conn)  # dedup: the same window and threshold
    assert len(_incidents(conn)) == 1 and len(_ceo_dms(conn)) == 1

    _quota(conn, 91, resets=resets)
    out = quota_guard.check(conn)
    assert out["new"] == [90] and "told" not in out  # the reserve threshold is recorded, nobody is told

    _quota(conn, 96, resets=resets)
    assert quota_guard.check(conn)["told"] == 95
    inc = _incidents(conn)
    assert len(inc) == 2 and inc[1]["priority"] == 1 and "95 %" in inc[1]["title"]
    assert len(_ceo_dms(conn)) == 2

    engines.pause(conn, "claude", _ts(resets), "Claude AI usage limit reached")
    assert quota_guard.check(conn)["told"] == 100
    assert quota_guard.check(conn).get("told") is None
    dms = _ceo_dms(conn)
    assert len(dms) == 3 and "vyčerpaná" in dms[-1]["body"] and len(_incidents(conn)) == 2  # spent: one summary

    # the next window warns again
    conn.execute("UPDATE engine_limits SET paused_until = NULL")
    _quota(conn, 82, resets=resets + timedelta(days=7))
    assert quota_guard.check(conn)["told"] == 80


def test_a_jump_past_several_thresholds_tells_only_the_highest(conn, team):
    _quota(conn, 97)
    out = quota_guard.check(conn)
    assert out["new"] == [80, 90, 95] and out["told"] == 95
    assert len(_incidents(conn)) == 1 and len(_ceo_dms(conn)) == 1


def test_no_reading_no_alert(conn, team):
    assert quota_guard.check(conn) == {"pct": None}


# ------------------------------------------------------------------ who starts from 90 %

def _work(conn, owner, se, title, **kw) -> int:
    return tasks.create(conn, owner, {"title": title, "assignee": {"type": "agent", "id": se}, **kw})["id"]


def test_over_the_reserve_only_the_owners_answer_p1_incidents_and_meeting_turns_start(conn, owner, team, monkeypatch):
    se = team["se"]
    routine = _work(conn, owner, se, "Denní přehled", source="schedule:4", priority=1)
    _quota(conn, 91)
    out = api_worker._next_work(conn, Ctx(se, via="worker"))
    assert "task" not in out and out["state"]["agent_pool_quota"] == "reserve"
    assert conn.execute("SELECT status FROM tasks WHERE id = ?", (routine,)).fetchone()[0] == "next"  # not failed

    p1 = _work(conn, owner, se, "personalos-api-1 down", source="event:sentinel", topic="provoz", priority=1)
    assert api_worker._next_work(conn, Ctx(se, via="worker"))["task"]["id"] == p1
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (p1,))

    chat = _work(conn, owner, se, "Chat: answer David", topic="chat", priority=2)
    assert api_worker._next_work(conn, Ctx(se, via="worker"))["task"]["id"] == chat
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (chat,))

    # a meeting turn starts too: the meetings of 8.-9. 10. lapsed with the quota (T-1138)
    turn = _work(conn, owner, se, "Porada Kniha: tvůj tah", topic="meeting", source="meeting:3", priority=2)
    assert api_worker._next_work(conn, Ctx(se, via="worker"))["task"]["id"] == turn
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (turn,))

    # the reserve threshold is configurable
    monkeypatch.setenv("POS_QUOTA_RESERVE_PCT", "95")
    assert api_worker._next_work(conn, Ctx(se, via="worker"))["task"]["id"] == routine


def test_an_agent_on_codex_is_not_held_by_the_claude_quota(conn, owner, team, monkeypatch):
    se = team["se"]
    conn.execute("UPDATE actors SET engine = 'codex' WHERE id = ?", (se,))
    routine = _work(conn, owner, se, "Denní přehled", source="schedule:4")
    _quota(conn, 93)
    monkeypatch.setattr(engines, "can_run", lambda conn, engine, actor_id: (True, "ok"))
    assert quota_guard.mode(conn, se) is None
    assert api_worker._next_work(conn, Ctx(se, via="worker"))["task"]["id"] == routine


def test_after_the_reset_the_owner_first_then_p1_and_routines_one_by_one(conn, owner, team):
    se = team["se"]
    now = datetime.now(timezone.utc)
    _quota(conn, 96, resets=now + timedelta(minutes=1))
    quota_guard.check(conn)
    conn.execute("UPDATE agent_pool_quota_alerts SET resets_at = ?", (_ts(now - timedelta(minutes=30)),))
    conn.execute("DELETE FROM agent_pool_quota")
    conn.commit()
    routine = _work(conn, owner, se, "Denní přehled", source="schedule:4", priority=1)
    other = _work(conn, owner, se, "Oprava", priority=1)
    p1 = _work(conn, owner, se, "deployer down", source="event:sentinel", topic="provoz", priority=1)
    chat = _work(conn, owner, se, "Chat: answer David", topic="chat", priority=3)
    assert quota_guard.mode(conn, se) == "recovery"
    order = []
    for _ in range(4):
        got = api_worker._next_work(conn, Ctx(se, via="worker")).get("task")
        if not got:
            break
        order.append(got["id"])
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (got["id"],))
        conn.execute("INSERT INTO runs (actor_id, task_id, kind, status, started_at) VALUES (?, ?, 'task', 'ok', ?)",
                     (se, got["id"], now_iso()))
        conn.commit()
    assert order == [chat, p1, routine, other]
    # a second routine within ROUTINE_GAP_MIN waits; after the gap it starts
    second = _work(conn, owner, se, "Týdenní přehled", source="scheduler")
    assert "task" not in api_worker._next_work(conn, Ctx(se, via="worker"))
    conn.execute("UPDATE runs SET started_at = ?", (_ts(now - timedelta(minutes=quota_guard.ROUTINE_GAP_MIN + 1)),))
    assert api_worker._next_work(conn, Ctx(se, via="worker"))["task"]["id"] == second
    # RECOVERY_H after the reset everything is back to normal
    conn.execute("UPDATE agent_pool_quota_alerts SET resets_at = ?",
                 (_ts(now - timedelta(hours=quota_guard.RECOVERY_H, minutes=1)),))
    assert quota_guard.mode(conn, se) is None


# ------------------------------------------------------------------ the scorecard

def test_the_scorecard_shows_agent_pool_quota_pct(conn, team):
    card = scorecard.live(conn)
    assert card["agents"]["agent_pool_quota_pct"] is None
    resets = _quota(conn, 83)
    card = scorecard.compute(conn)
    assert card["agents"]["agent_pool_quota_pct"] == 83.0
    assert card["agents"]["agent_pool_quota_resets_at"] == _ts(resets)
    assert "- kvóta agent-pool: 83 %, obnova" in scorecard.render_platform(card)
    assert any(k == "agent_pool_quota_pct" for k, _, _ in scorecard.KPIS)
