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


def test_a_task_still_waiting_on_something_real_is_not_requeued(conn, tmp_path):
    """2026-10-08 00:00: T-880, T-885, T-886, T-958 and T-1042 went back to the queue only to re-wait."""
    from pos import asks, handoff

    owner = Ctx(actors.owner_id(conn), via="api")
    ceo = _agent(conn, owner, tmp_path, "CEO", "ceo")
    dev = _agent(conn, owner, tmp_path, "Dev", "developer", ceo)
    now = _morning()
    today = now.astimezone(TZ).date()
    yesterday, tomorrow = (today - timedelta(days=1)).isoformat(), (today + timedelta(days=1)).isoformat()

    def waiting(title, **kw):
        t = tasks.create(conn, ceo, {"title": title, "assignee": "Dev", **kw})
        conn.execute("UPDATE tasks SET status = 'waiting' WHERE id = ?", (t["id"],))
        return t["id"]

    parent = waiting("M2: ostré kapitoly", do_date=yesterday)  # T-880: open subtasks
    sub = tasks.create(conn, ceo, {"title": "P1: oprava", "assignee": "Dev", "parent_id": parent})
    blocker = waiting("Google Admin: skupina kniha@", do_date=tomorrow)
    dep = waiting("E-maily", do_date=yesterday)  # T-885: its note names what it waits on
    conn.execute("UPDATE tasks SET progress_note = ? WHERE id = ?",
                 (f"Čeká na {tasks.display_id(blocker)} (David) → pak SMTP_HOST.", dep))
    later = waiting("M3", do_date=yesterday, follow_up=tomorrow)  # T-886: a follow-up still ahead
    owner_item = waiting("Google Admin předání", do_date=yesterday)  # T-958: an open handoff
    handoff.ensure_schema(conn)
    conn.execute("INSERT INTO browser_handoffs (actor_id, task_id, title, status, created_at, expires_at) "
                 "VALUES (?, ?, 'login', 'parked', '2026-10-07', '2026-10-08')", (dev.actor_id, owner_item))
    asked = waiting("Rozhodnutí o ceně", do_date=yesterday)  # an open question to the owner
    asks.ensure_schema(conn)
    ticket = tasks.create(conn, owner, {"title": "Cena knihy?"})
    conn.execute("INSERT INTO owner_asks (ticket_id, asker_id, source_task_id, topic_key, kind, created_at) "
                 "VALUES (?, ?, ?, 'cena', 'decision', '2026-10-07')", (ticket["id"], dev.actor_id, asked))
    free = waiting("Nasazení 75d1cc9", do_date=yesterday)  # T-1042: nothing open any more
    conn.commit()

    out = stuck_sweep.sweep(conn, now=now)
    assert out["woken"] == [tasks.display_id(free)]
    for tid in (parent, dep, later, owner_item, asked):
        assert conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == "waiting", tid
    reasons = {tid: stuck_sweep.waits_on(conn, conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone(),
                                         today.isoformat()) for tid in (parent, dep, later, owner_item, asked)}
    assert reasons[parent] == f"otevřený podúkol {tasks.display_id(sub['id'])}"
    assert reasons[dep] == f"čeká na {tasks.display_id(blocker)}" and reasons[later].startswith("další kontrola")
    assert reasons[owner_item] == "otevřené předání vlastníkovi" and reasons[asked] == "otevřený dotaz na vlastníka"

    # once the subtask is done and the owner answered, the calendar wakes them again
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (sub["id"],))
    conn.execute("UPDATE owner_asks SET status = 'answered' WHERE source_task_id = ?", (asked,))
    conn.commit()
    assert set(stuck_sweep.sweep(conn, now=now + timedelta(minutes=10))["woken"]) == {
        tasks.display_id(parent), tasks.display_id(asked)}
