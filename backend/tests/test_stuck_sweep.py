"""The stuck-work sweep (pos.stuck_sweep): wakes waiting tasks past their date, flags idle work to
the lead, one digest a day; code in the core routines_overdue loop."""

from datetime import datetime, timedelta, timezone

import pytest

from pos import actors, agents, business, comments, integrations, scheduler, stuck_sweep, tasks
from pos.core import TZ, Ctx
from pos.db import connect, migrate


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "s.db")
    migrate(c)
    actors.ensure_builtin(c)
    integrations.register_builtin_agents(c)
    business.ensure_schema(c)
    c.commit()
    yield c
    c.close()


def _agent(conn, owner, tmp_path, name, role, lead=None) -> Ctx:
    aid = agents.create_agent(conn, owner, name=name, purpose=name, lifetime="long_lived", data_dir=tmp_path,
                              permissions=["tasks:read", "tasks:write", "tasks:claim"])["agent"]["id"]
    conn.execute("UPDATE actors SET role = ?, reports_to = ? WHERE id = ?", (role, lead.actor_id if lead else None, aid))
    conn.commit()
    return Ctx(aid, via="mcp")


def _morning() -> datetime:
    """Today 09:00 in Prague (after the digest hour), as UTC."""
    return datetime.now(TZ).replace(hour=9, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def _dms(conn, actor_id):
    return conn.execute("""SELECT m.* FROM chat_messages m JOIN chat_inbox i ON i.message_id = m.id
                           WHERE i.actor_id = ?""", (actor_id,)).fetchall()


def test_waiting_past_its_date_wakes_idle_work_reaches_the_lead_once_a_day(conn, tmp_path):
    owner = Ctx(actors.owner_id(conn), via="api")
    ceo = _agent(conn, owner, tmp_path, "CEO", "ceo")
    cto = _agent(conn, owner, tmp_path, "CTO", "cto", ceo)
    sre = _agent(conn, owner, tmp_path, "SRE", "sre", cto)
    now = _morning()
    yesterday = (now.astimezone(TZ).date() - timedelta(days=1)).isoformat()
    tomorrow = (now.astimezone(TZ).date() + timedelta(days=1)).isoformat()

    def task(title, status, **kw):
        t = tasks.create(conn, cto, {"title": title, "assignee": "SRE", **kw})
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, t["id"]))
        return t["id"]

    past = task("svr03: swap 81 %", "waiting", follow_up=yesterday)  # T-183
    later = task("Ověřit zálohu zítra", "waiting", do_date=tomorrow)
    idle = task("Oprav zálohy", "working")
    busy = task("Rozpracované", "working")
    old = (now - timedelta(hours=30)).isoformat(timespec="seconds")
    conn.execute("UPDATE tasks SET updated_at = ? WHERE id IN (?, ?)", (old, idle, busy))
    comments.log(conn, sre, busy, "Pracuji na tom.", "comment")  # recent activity
    conn.commit()

    out = stuck_sweep.sweep(conn, now=now)
    assert out["woken"] == [tasks.display_id(past)]
    row = conn.execute("SELECT status, progress_note FROM tasks WHERE id = ?", (past,)).fetchone()
    assert row["status"] == "next" and "Probuzeno" in row["progress_note"]
    assert conn.execute("SELECT status FROM tasks WHERE id = ?", (later,)).fetchone()[0] == "waiting"
    assert out["digests"] == {"CTO": 2}  # the SRE's lead: the woken task and the idle one, not the busy one
    body = _dms(conn, cto.actor_id)[-1]["body"]
    assert tasks.display_id(idle) in body and tasks.display_id(past) in body and tasks.display_id(busy) not in body
    assert all(m["author_id"] != owner.actor_id for m in _dms(conn, cto.actor_id))

    # put back to waiting with the same date: not woken again the same day, no second digest
    conn.execute("UPDATE tasks SET status = 'waiting' WHERE id = ?", (past,))
    conn.commit()
    again = stuck_sweep.sweep(conn, now=now + timedelta(minutes=10))
    assert again == {} and len(_dms(conn, cto.actor_id)) == 1
    assert conn.execute("SELECT status FROM tasks WHERE id = ?", (past,)).fetchone()[0] == "waiting"


def test_no_digest_before_the_morning_and_the_sweep_runs_in_the_core_routine_loop(conn, tmp_path):
    owner = Ctx(actors.owner_id(conn), via="api")
    ceo = _agent(conn, owner, tmp_path, "CEO", "ceo")
    dev = _agent(conn, owner, tmp_path, "Dev", "developer", ceo)
    t = tasks.create(conn, ceo, {"title": "Stará práce", "assignee": "Dev"})
    conn.execute("UPDATE tasks SET status = 'working', updated_at = '2026-01-01T00:00:00+00:00' WHERE id = ?",
                 (t["id"],))
    conn.commit()
    early = datetime.now(TZ).replace(hour=6, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    assert stuck_sweep.sweep(conn, now=early) == {}
    assert "routines_overdue" in scheduler.CORE_JOBS  # cannot be switched off
    out = scheduler.routines_overdue(conn)
    if datetime.now(TZ).hour >= stuck_sweep.DIGEST_HOUR:
        assert out["stuck"]["digests"] == {"CEO": 1}  # no lead below the owner: the CEO hears of it
    assert dev.actor_id != ceo.actor_id
