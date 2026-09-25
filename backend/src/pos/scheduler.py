"""The scheduler for system work (AGENTS-SPEC row 11): recurring jobs inside
PersonalOS until Nexus runs them through its A2A facade.

A job has a simple schedule and a built-in action. Actions are deterministic
code (no tokens); where thinking is needed they create a task for an agent,
which then spends tokens through the normal budget gates.

Schedules:  "every 60m" · "every 2h" · "daily 07:00" · "weekdays 07:00" · "weekly fri 15:00"
Times are Europe/Prague.
"""

import json
import logging
import re
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from . import actors, audit, tasks
from .core import TZ, Ctx, now_iso, today

log = logging.getLogger(__name__)
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def next_run(schedule: str, after: datetime) -> datetime:
    """Next time (UTC) the schedule fires strictly after `after` (UTC)."""
    s = schedule.strip().lower()
    if m := re.fullmatch(r"every (\d+) ?(m|min|h)", s):
        n = int(m[1]) * (60 if m[2] == "h" else 1)
        if n < 1:
            raise ValueError(f"unknown schedule: {schedule}")
        return after + timedelta(minutes=n)
    local = after.astimezone(TZ)
    if m := re.fullmatch(r"(daily|weekdays) (\d{1,2}):(\d{2})", s):
        kind, hh, mm = m[1], int(m[2]), int(m[3])
        days = range(5) if kind == "weekdays" else range(7)
    elif m := re.fullmatch(r"weekly (mon|tue|wed|thu|fri|sat|sun) (\d{1,2}):(\d{2})", s):
        days, hh, mm = [DAYS.index(m[1])], int(m[2]), int(m[3])
    else:
        raise ValueError(f"unknown schedule: {schedule}")
    for add in range(0, 8):
        d = (local + timedelta(days=add)).replace(hour=hh, minute=mm, second=0, microsecond=0)
        if d > local and d.weekday() in days:
            return d.astimezone(timezone.utc)
    raise ValueError(schedule)


# ------------------------------------------------------------------ actions

def _owner_task(conn, title: str, notes: str, priority: int = 3, topic: str = "routine", *,
                purpose: str = "", done: str | None = None) -> str:
    ctx = Ctx(actors.owner_id(conn), via="scheduler")
    head = f"Purpose: {purpose}\nSource: a PersonalOS routine (scheduler).\n\n" if purpose else ""
    return tasks.create(conn, ctx, {"title": title, "notes": head + notes, "priority": priority, "topic": topic,
                                    "definition_of_done": done, "assignee": "me", "status": "next",
                                    "do_date": today().isoformat()})["ref"]


def morning_brief(conn: sqlite3.Connection) -> dict:
    """A short read-only summary of the day, as a task for the owner (no tokens)."""
    owner = Ctx(actors.owner_id(conn), via="scheduler")
    c = tasks.counts(conn, owner)
    today_list = tasks.list_tasks(conn, owner, "today")[:8]
    pending = conn.execute("SELECT COUNT(*) FROM approvals WHERE status = 'pending'").fetchone()[0]
    working = conn.execute(
        "SELECT a.name, t.title FROM tasks t JOIN actors a ON a.id = t.assignee_id "
        "WHERE t.status = 'working' AND t.archived_at IS NULL"
    ).fetchall()
    lines = [f"- {t['ref']} {t['title']}" for t in today_list] or ["- nothing planned"]
    body = "\n".join([
        f"Today: {c['today']} planned · inbox {c['inbox']} · waiting {c['waiting']} · to review {c['review']} "
        f"· approvals {pending}",
        "", "Planned:", *lines, "",
        "Agents working now:", *([f"- {w['name']}: {w['title']}" for w in working] or ["- none"]),
    ])
    ref = _owner_task(conn, f"Morning brief · {today().strftime('%a %d %b')}", body, 3, "brief",
                      purpose="start the day knowing what is planned, what waits on you and what agents do.",
                      done="You read it and adjusted today's plan if needed.")
    return {"task": ref}


def follow_ups(conn: sqlite3.Connection) -> dict:
    """Waiting-for items past their follow-up date: the AI drafts a reminder."""
    owner = Ctx(actors.owner_id(conn), via="scheduler")
    due = conn.execute(
        """SELECT id, title, assignee_name FROM tasks WHERE status = 'waiting' AND archived_at IS NULL
           AND follow_up IS NOT NULL AND follow_up <= ?""", (today().isoformat(),)
    ).fetchall()
    made = []
    for t in due:
        step = tasks.create(conn, owner, {
            "title": f"Draft a friendly reminder to {t['assignee_name'] or 'them'} about: {t['title']}",
            "parent_id": t["id"], "assignee": "ai", "status": "next",
            "notes": f"Purpose: {t['assignee_name'] or 'someone'} has not delivered {tasks.display_id(t['id'])} "
                     f"by its follow-up date; a friendly nudge keeps it moving.\n"
                     "Source: the daily follow-up routine.\n\n"
                     "Draft only. Sending needs the owner's approval (request_outbound).",
            "definition_of_done": "A short reminder draft is ready and waits for the owner's approval.",
        })
        tasks.update(conn, owner, t["id"], {"follow_up": (today() + timedelta(days=3)).isoformat()})
        made.append(step["ref"])
    return {"reminders": made}


def weekly_review(conn: sqlite3.Connection) -> dict:
    owner = Ctx(actors.owner_id(conn), via="scheduler")
    c = tasks.counts(conn, owner)
    body = "\n".join([
        "Get clear", f"- [ ] Process the inbox to zero ({c['inbox']})", "- [ ] Capture loose ends", "",
        "Get current", "- [ ] Review last week's calendar and the next two weeks",
        f"- [ ] Waiting for: nudge people ({c['waiting']})", f"- [ ] AI and agents: accept or return ({c['review']})",
        "- [ ] Every topic has a next action", "",
        "Get creative", f"- [ ] Someday: promote or drop ({c['someday']})", "- [ ] Three outcomes for next week",
    ])
    return {"task": _owner_task(conn, f"Weekly review · week {today().isocalendar()[1]}", body, 2, "review",
                                purpose="the GTD weekly review: get clear, get current, get creative.",
                                done="Every checklist item is ticked or consciously skipped.")}


def nightly_retrospective(conn: sqlite3.Connection) -> dict:
    """Collects the day's signals and asks the assistant to propose
    improvements (input for AGENTS-SPEC 6). The analysis costs tokens only
    when a worker picks the task up."""
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    q = lambda sql, *p: conn.execute(sql, p).fetchone()[0]  # noqa: E731
    stats = {
        "runs": q("SELECT COUNT(*) FROM runs WHERE started_at >= ?", since),
        "runs_failed": q("SELECT COUNT(*) FROM runs WHERE started_at >= ? AND status = 'error'", since),
        "returned": q("SELECT COUNT(*) FROM history WHERE entity = 'task' AND action = 'return' AND at >= ?", since),
        "interventions": q("SELECT COUNT(*) FROM history WHERE entity = 'task' AND action = 'intervene' AND at >= ?", since),
        "events_unrouted": q("SELECT COUNT(*) FROM events WHERE received_at >= ? AND rule_id IS NULL", since),
        "tasks_done": q("SELECT COUNT(*) FROM tasks WHERE completed_at >= ?", since),
        "owner_edits_after_ai": q(
            "SELECT COUNT(*) FROM history h JOIN tasks t ON t.id = h.entity_id WHERE h.entity = 'task' "
            "AND h.at >= ? AND h.actor_id = ? AND t.assignee_type IN ('ai', 'agent')", since, actors.owner_id(conn)),
    }
    ctx = Ctx(actors.assistant_id(conn), via="scheduler")
    t = tasks.create(conn, ctx, {
        "title": f"Nightly retrospective · {today().isoformat()}",
        "definition_of_done": "Up to 5 improvements, each with evidence, are in the completion note "
                              "(or 'nothing worth changing').",
        "notes": "Purpose: learn from yesterday so PersonalOS and its agents get better. "
                 "Source: the nightly retrospective routine.\n\n"
                 "Look at yesterday's numbers and the audit log. Propose up to 5 concrete improvements "
                 "(routing rules, agent instructions, defaults, platform issues for the Dev agent), each with "
                 "the evidence. Do not change permissions, limits or the constitution.\n\n"
                 + json.dumps(stats, indent=2),
        "assignee": "ai", "status": "next", "topic": "retrospective", "priority": 3,
    })
    return {"task": t["ref"], **stats}


def budget_check(conn: sqlite3.Connection) -> dict:
    from .integrations import budget_check as run

    report = run(conn)
    return {"level": getattr(report, "level", None)}


def reap_runs(conn: sqlite3.Connection, silent_minutes: int = 20) -> dict:
    """Release runs whose worker went silent (crashed, PC restarted): the run is
    marked as an error and its task goes back to the queue."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=silent_minutes)).isoformat(timespec="seconds")
    stale = conn.execute(
        """SELECT * FROM runs WHERE status = 'running' AND engine IS NOT NULL
           AND COALESCE(heartbeat_at, started_at) < ?""", (cutoff,)
    ).fetchall()
    released = []
    for r in stale:
        conn.execute("UPDATE runs SET status = 'error', ended_at = ?, detail = ? WHERE id = ?",
                     (now_iso(), f"worker went silent for {silent_minutes} min", r["id"]))
        if r["task_id"]:
            t = conn.execute("SELECT status, assignee_id FROM tasks WHERE id = ?", (r["task_id"],)).fetchone()
            if t and t["status"] == "working" and t["assignee_id"] == r["actor_id"]:
                tasks.update(conn, Ctx(r["actor_id"], via="scheduler"), r["task_id"],
                             {"status": "next", "progress_note": "The previous run stopped unexpectedly; retrying."})
        released.append(r["id"])
    conn.commit()
    return {"released": released}


def claude_selfcheck(conn: sqlite3.Connection) -> dict:
    import os

    from . import engines

    if os.environ.get("POS_CLAUDE_SELFCHECK") != "1":
        return {"skipped": "POS_CLAUDE_SELFCHECK is off (workers use their own CLI)"}
    return engines.claude_selfcheck(conn)


def member_schedules(conn: sqlite3.Connection) -> dict:
    from . import schedules

    return schedules.run_due(conn)


def a2a_sync(conn: sqlite3.Connection) -> dict:
    from . import a2a

    return a2a.sync(conn)


def knowlage_files(conn: sqlite3.Connection) -> dict:
    from . import kb_files

    return kb_files.sync_pending(conn)


ACTIONS: dict[str, Callable[[sqlite3.Connection], dict]] = {
    "knowlage_files": knowlage_files,
    "morning_brief": morning_brief,
    "follow_ups": follow_ups,
    "weekly_review": weekly_review,
    "nightly_retrospective": nightly_retrospective,
    "budget_check": budget_check,
    "a2a_sync": a2a_sync,
    "reap_runs": reap_runs,
    "member_schedules": member_schedules,
    "claude_selfcheck": claude_selfcheck,
}

DEFAULT_JOBS = [
    ("Morning brief", "weekdays 07:00", "morning_brief"),
    ("Follow up on waiting-for", "daily 08:00", "follow_ups"),
    ("Weekly review prep", "weekly fri 15:00", "weekly_review"),
    ("Nightly retrospective", "daily 23:00", "nightly_retrospective"),
    ("Budget check", "every 60m", "budget_check"),
    ("A2A: hand tasks to remote agents and collect results", "every 1m", "a2a_sync"),
    ("Release runs of workers that went silent", "every 5m", "reap_runs"),
    ("Schedules of people and agents", "every 1m", "member_schedules"),
    ("Check that the Claude CLI answers with its model", "every 6h", "claude_selfcheck"),
    ("Push files into knowlage (retry what failed)", "every 15m", "knowlage_files"),
]


# ------------------------------------------------------------------ jobs

def seed(conn: sqlite3.Connection) -> None:
    now = datetime.now(timezone.utc)
    for name, schedule, action in DEFAULT_JOBS:
        if conn.execute("SELECT 1 FROM jobs WHERE action = ?", (action,)).fetchone():
            continue
        conn.execute(
            "INSERT INTO jobs (name, schedule, action, enabled, next_run_at, created_at) VALUES (?, ?, ?, 1, ?, ?)",
            (name, schedule, action, next_run(schedule, now).isoformat(timespec="seconds"), now_iso()),
        )
    conn.commit()


def list_jobs(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
    return [{**dict(r), "enabled": bool(r["enabled"]),
             "last_result": json.loads(r["last_result"]) if r["last_result"] else None} for r in rows]


def update_job(conn: sqlite3.Connection, ctx: Ctx, job_id: int, changes: dict) -> dict:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise tasks.Invalid(f"no job {job_id}")
    sets = {}
    if "enabled" in changes:
        sets["enabled"] = 1 if changes["enabled"] else 0
    if "schedule" in changes:
        next_run(changes["schedule"], datetime.now(timezone.utc))  # validates
        sets["schedule"] = changes["schedule"]
        sets["next_run_at"] = next_run(changes["schedule"], datetime.now(timezone.utc)).isoformat(timespec="seconds")
    if sets:
        conn.execute(f"UPDATE jobs SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?", [*sets.values(), job_id])
        audit.log(conn, ctx, "job_update", "job", job_id, **sets)
        conn.commit()
    return next(j for j in list_jobs(conn) if j["id"] == job_id)


def run_job(conn: sqlite3.Connection, job: sqlite3.Row | dict, by: Ctx | None = None) -> dict:
    from . import killswitch

    now = datetime.now(timezone.utc)
    ctx = by or Ctx(actors.owner_id(conn), via="scheduler")
    if killswitch.is_frozen(conn) and job["action"] not in ("budget_check", "morning_brief", "weekly_review"):
        result = {"skipped": "kill switch is on"}
    else:
        try:
            result = ACTIONS[job["action"]](conn)
        except Exception as e:  # noqa: BLE001 - one failing job must not stop the others
            log.exception("job %s failed", job["name"])
            conn.rollback()
            result = {"error": str(e)[:500]}
    conn.execute(
        "UPDATE jobs SET last_run_at = ?, last_result = ?, next_run_at = ? WHERE id = ?",
        (now.isoformat(timespec="seconds"), json.dumps(result, ensure_ascii=False, default=str),
         next_run(job["schedule"], now).isoformat(timespec="seconds"), job["id"]),
    )
    if job["action"] not in ("a2a_sync", "reap_runs", "member_schedules", "knowlage_files") or result.get("sent") \
            or result.get("finished") or result.get("released") or result.get("fired") or result.get("pushed") \
            or result.get("failed"):
        audit.log(conn, ctx, f"job:{job['action']}", "job", job["id"], **{k: v for k, v in result.items() if k != "task"})
    conn.commit()
    return result


def run_due(conn: sqlite3.Connection) -> list[str]:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    due = conn.execute("SELECT * FROM jobs WHERE enabled = 1 AND next_run_at <= ?", (now,)).fetchall()
    for job in due:
        run_job(conn, job)
    return [j["action"] for j in due]


async def loop(db_path, interval_s: int = 30) -> None:
    import asyncio

    from .db import connect

    while True:
        await asyncio.sleep(interval_s)
        try:
            conn = connect(db_path)
            try:
                await asyncio.to_thread(run_due, conn)
            finally:
                conn.close()
        except Exception:  # keep ticking
            log.exception("scheduler tick failed")
