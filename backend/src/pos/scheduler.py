"""The scheduler for system work (AGENTS-SPEC row 11): recurring jobs inside
PersonalOS until Nexus runs them through its A2A facade.

A job has a simple schedule and a built-in action. Actions are deterministic
code (no tokens); where thinking is needed they create a task for an agent,
which then spends tokens through the normal budget gates.

Schedules:  "every 60m" · "every 2h" · "every 4d" · "every 4d 09:00" · "daily 07:00" · "weekdays 07:00"
            · "weekly fri 15:00"
Times are Europe/Prague.
"""

import json
import logging
import os
import re
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from . import actors, audit, tasks
from .core import TZ, Ctx, now_iso, today

log = logging.getLogger(__name__)
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _at(day, hh: int, mm: int) -> datetime:
    """The UTC instant of a Prague wall-clock time on a date. Timezone-aware: the hour the
    October change repeats counts once (its first occurrence, fold 0); a time the March change
    skips falls on the hour after it."""
    local = datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ, fold=0)
    return local.astimezone(timezone.utc)


def next_run(schedule: str, after: datetime) -> datetime:
    """Next time (UTC) the schedule fires strictly after `after` (UTC).

    Local times are built from the Prague date and compared as UTC instants, never as wall
    clocks: comparing wall clocks returned a time in the past inside the hour the October
    change repeats, and a daily job fired on every scheduler tick for that hour (2026-09-27)."""
    s = schedule.strip().lower()
    if after.tzinfo is None:
        after = after.replace(tzinfo=timezone.utc)
    if m := re.fullmatch(r"every (\d+) ?(m|min|h)", s):
        n = int(m[1]) * (60 if m[2] == "h" else 1)
        if n < 1:
            raise ValueError(f"unknown schedule: {schedule}")
        return after + timedelta(minutes=n)
    if m := re.fullmatch(r"every (\d+) ?(d|day|days)(?: (\d{1,2}):(\d{2}))?", s):
        days_n = int(m[1])
        if days_n < 1:
            raise ValueError(f"unknown schedule: {schedule}")
        if m[3] is None:  # every N days from the last firing
            return after + timedelta(days=days_n)
        hh, mm = int(m[3]), int(m[4])  # every N days at a time of day (Prague): N days on, at that time
        if hh > 23 or mm > 59:
            raise ValueError(f"unknown schedule: {schedule}")
        return _at(after.astimezone(TZ).date() + timedelta(days=days_n), hh, mm)
    if m := re.fullmatch(r"(daily|weekdays) (\d{1,2}):(\d{2})", s):
        kind, hh, mm = m[1], int(m[2]), int(m[3])
        days = range(5) if kind == "weekdays" else range(7)
    elif m := re.fullmatch(r"weekly (mon|tue|wed|thu|fri|sat|sun) (\d{1,2}):(\d{2})", s):
        days, hh, mm = [DAYS.index(m[1])], int(m[2]), int(m[3])
    else:
        raise ValueError(f"unknown schedule: {schedule}")
    if hh > 23 or mm > 59:
        raise ValueError(f"unknown schedule: {schedule}")
    start = after.astimezone(TZ).date()
    for add in range(0, 9):
        day = start + timedelta(days=add)
        if day.weekday() not in days:
            continue
        at = _at(day, hh, mm)
        if at > after:
            return at
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
    from . import asks

    defaults = asks.default_digest(conn)  # decisions he left unanswered: the recommendation was adopted
    body = "\n".join([
        f"Today: {c['today']} planned · inbox {c['inbox']} · waiting {c['waiting']} · to review {c['review']} "
        f"· approvals {pending}",
        *(["", "Rozhodnuto za tebe (bez odpovědi platí doporučení):", *defaults] if defaults else []),
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
            "title": f"Send a friendly reminder to {t['assignee_name'] or 'them'} about: {t['title']}",
            "parent_id": t["id"], "assignee": "ai", "status": "next",
            "notes": f"Purpose: {t['assignee_name'] or 'someone'} has not delivered {tasks.display_id(t['id'])} "
                     f"by its follow-up date; a friendly nudge keeps it moving.\n"
                     "Source: the daily follow-up routine.\n\n"
                     "Send it yourself with request_outbound (ordinary work, constitution Ú1: no approval; "
                     "audited, the CEO reviews it daily).",
            "definition_of_done": "A short, friendly reminder went out (request_outbound status sent).",
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

    out = run(conn)
    return {"level": out["level"], **({"company_cap": out["company_cap"]} if out.get("company_cap") else {})}


SILENT_MINUTES = 5  # the alive tick moves a run's heartbeat every minute, also inside a long tool step
SILENT_DETAIL = "worker went silent for"
SILENT_REQUEUES = 1  # a task whose run died this often already goes to the agent's lead instead


def _lead_for(conn: sqlite3.Connection, actor_id: int) -> int | None:
    """Who looks after the agent's stuck work: its lead (reports_to), else the CEO, else the owner."""
    from . import business

    row = conn.execute("SELECT reports_to FROM actors WHERE id = ?", (actor_id,)).fetchone()
    lead = row["reports_to"] if row else None
    for cand in (lead, business.ceo_id(conn), actors.owner_id(conn)):
        if cand and cand != actor_id:
            return cand
    return None


def _escalate_silent(conn: sqlite3.Connection, r: sqlite3.Row, deaths: int) -> int | None:
    """The task's runs keep dying: it waits for the agent's lead instead of a third run."""
    from . import business, chat, comments, wake

    lead = _lead_for(conn, r["actor_id"])
    ref = tasks.display_id(r["task_id"])
    agent = actors.get(conn, r["actor_id"])["name"]
    why = (f"{deaths} runs of {agent} on this task stopped without a word from its worker "
           f"(no heartbeat for {SILENT_MINUTES} min)")
    tasks.update(conn, Ctx(r["actor_id"], via="scheduler"), r["task_id"],
                 {"status": "waiting", "progress_note": f"{why}; waiting for the lead, not retried automatically."})
    sys_ctx = business.system_ctx(conn)
    comments.log(conn, sys_ctx, r["task_id"], f"Escalated to the lead: {why}.", "system")
    audit.log(conn, sys_ctx, "run_silent_escalate", "task", r["task_id"], run=r["id"], lead=lead, deaths=deaths)
    if lead:
        title = conn.execute("SELECT title FROM tasks WHERE id = ?", (r["task_id"],)).fetchone()["title"]
        try:
            chat.send_dm(conn, sys_ctx, lead,
                         f"{ref} '{title}': {why}. It waits for you: look at the runs (get_agent_status), then "
                         "send it back to the queue, split it, or reassign it.",
                         priority="fyi", attachments=[{"type": "task", "id": r["task_id"]}], system=True)
        except Exception:  # noqa: BLE001 - the comment and the audit line still say it
            log.exception("could not tell the lead about %s", ref)
        wake.wake(lead)
    return lead


def reap_runs(conn: sqlite3.Connection, silent_minutes: int = SILENT_MINUTES) -> dict:
    """Release runs whose worker went silent (crashed, PC restarted, the API restarted under it):
    no heartbeat and no alive tick for `silent_minutes` (the worker ticks /alive every 10 s, also
    inside a long tool step, and the heartbeat column follows every minute). The run is an error;
    its task goes back to the queue once; when a run on it died that way before, the task waits
    for the agent's lead instead (prod 2026-09: the same task's runs died again and again)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=silent_minutes)).isoformat(timespec="seconds")
    stale = conn.execute(
        """SELECT * FROM runs WHERE status = 'running' AND engine IS NOT NULL
           AND COALESCE(heartbeat_at, started_at) < ?""", (cutoff,)
    ).fetchall()
    released, requeued, escalated = [], [], []
    for r in stale:
        conn.execute("UPDATE runs SET status = 'error', ended_at = ?, detail = ? WHERE id = ? AND status = 'running'",
                     (now_iso(), f"{SILENT_DETAIL} {silent_minutes} min", r["id"]))
        released.append(r["id"])
        if not r["task_id"]:
            continue
        t = conn.execute("SELECT status, assignee_id FROM tasks WHERE id = ?", (r["task_id"],)).fetchone()
        if not (t and t["status"] == "working" and t["assignee_id"] == r["actor_id"]):
            continue
        deaths = conn.execute("SELECT COUNT(*) FROM runs WHERE task_id = ? AND actor_id = ? AND status = 'error' "
                              "AND detail LIKE ?", (r["task_id"], r["actor_id"], SILENT_DETAIL + "%")).fetchone()[0]
        if deaths > SILENT_REQUEUES:
            _escalate_silent(conn, r, deaths)
            escalated.append(tasks.display_id(r["task_id"]))
        else:
            tasks.update(conn, Ctx(r["actor_id"], via="scheduler"), r["task_id"],
                         {"status": "next", "progress_note": "The previous run stopped unexpectedly; retrying."})
            requeued.append(tasks.display_id(r["task_id"]))
    conn.commit()
    out: dict = {"released": released}
    if requeued:
        out["requeued"] = requeued
    if escalated:
        out["escalated"] = escalated
    try:  # the owner handoffs of agents' browsers (pos.handoff): expire what is due, one reminder
        from . import handoff

        swept = handoff.sweep(conn)
        if swept["expired"] or swept["reminded"]:
            out["handoffs"] = swept
    except Exception:  # noqa: BLE001 - the runs above are what this job is for
        conn.rollback()
    return out


def claude_selfcheck(conn: sqlite3.Connection) -> dict:
    import os

    from . import engines

    if os.environ.get("POS_CLAUDE_SELFCHECK") != "1":
        return {"skipped": "POS_CLAUDE_SELFCHECK is off (workers use their own CLI)"}
    return engines.claude_selfcheck(conn)


def member_schedules(conn: sqlite3.Connection) -> dict:
    from . import schedules

    return schedules.run_due(conn)


def routines_overdue(conn: sqlite3.Connection) -> dict:
    """Late routines, and the stuck-work sweep (pos.stuck_sweep: code, no model run) in the same core
    loop, so it cannot be switched off with the paused "hlídání výpadků" routines."""
    from . import schedules, stuck_sweep

    from . import frustration

    out = schedules.watch_overdue(conn)
    try:
        stuck = stuck_sweep.sweep(conn)
    except Exception as e:  # noqa: BLE001 - the routine watch stands either way
        log.exception("stuck sweep failed")
        conn.rollback()
        stuck = {"error": str(e)[:300]}
    try:  # the owner's messages nobody answered in 2 h: to the CEO's frustration task (pos.frustration)
        unanswered = frustration.unanswered_sweep(conn)
    except Exception:  # noqa: BLE001
        log.exception("unanswered sweep failed")
        conn.rollback()
        unanswered = []
    if unanswered:
        out = {**out, "unanswered": unanswered}
    return {**out, "stuck": stuck} if stuck else out


def a2a_sync(conn: sqlite3.Connection) -> dict:
    from . import a2a

    return a2a.sync(conn)


def probation_review(conn: sqlite3.Connection) -> dict:
    from . import hiring

    return hiring.probation_review(conn)


def feedback_digest(conn: sqlite3.Connection) -> dict:
    from . import feedback

    return feedback.coach_digest(conn)


def knowlage_files(conn: sqlite3.Connection) -> dict:
    from . import kb_files

    return kb_files.sync_pending(conn)


def access_expire(conn: sqlite3.Connection) -> dict:
    from .access import service

    return service.expire(conn)


def access_watch(conn: sqlite3.Connection) -> dict:
    from .access import service

    return service.watch(conn)


def access_digest(conn: sqlite3.Connection) -> dict:
    from .access import service

    return service.digest(conn)


def access_weekly(conn: sqlite3.Connection) -> dict:
    from .access import service

    return service.weekly(conn)


def weekly_report(conn: sqlite3.Connection) -> dict:
    from . import weekly

    return weekly.weekly_job(conn)


def weekly_meeting_timeouts(conn: sqlite3.Connection) -> dict:
    from . import weekly

    return weekly.meeting_timeouts(conn)


def meetings_tick(conn: sqlite3.Connection) -> dict:
    from . import meetings

    return meetings.tick(conn)


def sentinel_watch(conn: sqlite3.Connection) -> dict:
    from . import monitor

    return monitor.watch(conn)


def sentinel_digest(conn: sqlite3.Connection) -> dict:
    from . import monitor

    return monitor.digest(conn)


def agents_watch(conn: sqlite3.Connection) -> dict:
    from . import workers

    return workers.watch(conn)


def grafana_watch(conn: sqlite3.Connection) -> dict:
    from . import observability

    return observability.watch(conn)


def review_sla(conn: sqlite3.Connection) -> dict:
    from . import business

    return business.review_sla(conn)


def idle_agents(conn: sqlite3.Connection) -> dict:
    from . import business

    return business.idle_agents_job(conn)


def github_triage(conn: sqlite3.Connection) -> dict:
    from . import routing

    return routing.github_poll(conn)


def outbound_digest(conn: sqlite3.Connection) -> dict:
    from . import outbound

    return outbound.digest(conn)


def outbound_drafts(conn: sqlite3.Connection) -> dict:
    """E-mail drafts the owner sent or discarded: close his item, record the outcome (pos.outbound_drafts)."""
    from . import outbound_drafts as drafts

    return drafts.sync(conn)


def outbound_replies(conn: sqlite3.Connection) -> dict:
    """Replies to what we sent (thread follow-ups) and the LinkedIn token's expiry."""
    from . import outbound_ledger, outbound_linkedin

    out = outbound_ledger.check_replies(conn)
    out.update(outbound_linkedin.expiry_check(conn))
    return {k: v for k, v in out.items() if v}


def invoices_poll(conn: sqlite3.Connection) -> dict:
    from .invoices import service

    return service.poll(conn)


def support_intake(conn: sqlite3.Connection) -> dict:
    from .support import service

    return service.job(conn)


def weekly_publish_overdue(conn: sqlite3.Connection) -> dict:
    from . import weekly

    return weekly.publish_overdue(conn)


def projects_weekly(conn: sqlite3.Connection) -> dict:
    from . import project_info

    return project_info.weekly_job(conn)


def learning_weekly(conn: sqlite3.Connection) -> dict:
    from . import learning

    return learning.coach_weekly(conn)


def memories_ensure(conn: sqlite3.Connection) -> dict:
    from . import learning

    out = learning.ensure_memories(conn)
    conn.commit()
    return out if out.get("created") else {}


def heads_sweep(conn: sqlite3.Connection) -> dict:
    from . import head_alerts

    return head_alerts.sweep(conn)


def heads_second_line(conn: sqlite3.Connection) -> dict:
    from . import head_alerts

    return head_alerts.sweep(conn, second_line=True)


def ceo_business_focus(conn: sqlite3.Connection) -> dict:
    from . import effectiveness

    return effectiveness.ceo_digest(conn)


def owner_decision_defaults(conn: sqlite3.Connection) -> dict:
    from . import asks

    return asks.adopt_defaults(conn)


def promises_tick(conn: sqlite3.Connection) -> dict:
    from . import promises

    return promises.tick(conn)


def scorecard_daily(conn: sqlite3.Connection) -> dict:
    from . import scorecard

    return scorecard.daily(conn)


def platform_meeting(conn: sqlite3.Connection) -> dict:
    from . import platform_loop

    return platform_loop.start_meeting(conn)


def platform_retro(conn: sqlite3.Connection) -> dict:
    from . import platform_loop

    return platform_loop.retro(conn)


def reality_tick(conn: sqlite3.Connection) -> dict:
    from . import reality

    return reality.tick(conn)


def improve_daily(conn: sqlite3.Connection) -> dict:
    from .improve import loop

    return loop.daily(conn)


def improve_triage(conn: sqlite3.Connection) -> dict:
    from .improve import loop

    return loop.weekly_triage(conn)


def improve_coach(conn: sqlite3.Connection) -> dict:
    from .improve import loop

    return loop.weekly_coach(conn)


ACTIONS: dict[str, Callable[[sqlite3.Connection], dict]] = {
    "reality_tick": reality_tick,
    "improve_daily": improve_daily,
    "improve_triage": improve_triage,
    "improve_coach": improve_coach,
    "scorecard_daily": scorecard_daily,
    "platform_meeting": platform_meeting,
    "platform_retro": platform_retro,
    "owner_decision_defaults": owner_decision_defaults,
    "promises_tick": promises_tick,
    "learning_weekly": learning_weekly,
    "memories_ensure": memories_ensure,
    "ceo_business_focus": ceo_business_focus,
    "heads_sweep": heads_sweep,
    "heads_sweep_afternoon": heads_sweep,
    "heads_second_line": heads_second_line,
    "projects_weekly": projects_weekly,
    "review_sla": review_sla,
    "idle_agents": idle_agents,
    "github_triage": github_triage,
    "weekly_publish_overdue": weekly_publish_overdue,
    "outbound_digest": outbound_digest,
    "outbound_drafts": outbound_drafts,
    "outbound_replies": outbound_replies,
    "invoices_poll": invoices_poll,
    "support_intake": support_intake,
    "agents_watch": agents_watch,
    "grafana_watch": grafana_watch,
    "sentinel_watch": sentinel_watch,
    "sentinel_digest": sentinel_digest,
    "access_expire": access_expire,
    "access_watch": access_watch,
    "access_digest": access_digest,
    "access_weekly": access_weekly,
    "weekly_report": weekly_report,
    "weekly_meeting_timeouts": weekly_meeting_timeouts,
    "meetings_tick": meetings_tick,
    "knowlage_files": knowlage_files,
    "feedback_digest": feedback_digest,
    "probation_review": probation_review,
    "morning_brief": morning_brief,
    "follow_ups": follow_ups,
    "weekly_review": weekly_review,
    "nightly_retrospective": nightly_retrospective,
    "budget_check": budget_check,
    "a2a_sync": a2a_sync,
    "reap_runs": reap_runs,
    "member_schedules": member_schedules,
    "routines_overdue": routines_overdue,
    "claude_selfcheck": claude_selfcheck,
}

DEFAULT_JOBS = [
    ("Morning brief", "weekdays 07:00", "morning_brief"),
    ("Follow up on waiting-for", "daily 08:00", "follow_ups"),
    ("Weekly review prep", "weekly fri 15:00", "weekly_review"),
    ("Nightly retrospective", "daily 23:00", "nightly_retrospective"),
    ("Budget check", "every 60m", "budget_check"),
    ("A2A: hand tasks to remote agents and collect results", "every 1m", "a2a_sync"),
    ("Release runs of workers that went silent", "every 1m", "reap_runs"),
    ("Schedules of people and agents", "every 1m", "member_schedules"),
    ("Check that the Claude CLI answers with its model", "every 6h", "claude_selfcheck"),
    ("Push files into knowlage (retry what failed)", "every 15m", "knowlage_files"),
    ("Repeated critique of an agent → the Agent coach", "daily 06:30", "feedback_digest"),
    ("Probation ended → the lead decides", "daily 07:15", "probation_review"),
    # The Access manager (pos.access): temporary grants end, spikes pause, the owner's digest, the weekly review.
    ("Access: end temporary grants and raises", "every 5m", "access_expire"),
    ("Access: spend spikes and the company cap", "every 15m", "access_watch"),
    ("Access: daily digest for the owner", "daily 18:00", "access_digest"),
    # Ú1: ordinary outbound goes out without approval; the CEO reviews everything sent, daily.
    ("Outbound: daily review of everything sent (CEO)", "daily 18:30", "outbound_digest"),
    # E-mail goes out as drafts the owner sends himself: see what he sent or discarded, close his item.
    ("Outbound: e-mail drafts the owner sent or discarded", "every 10m", "outbound_drafts"),
    ("Outbound: replies to what we sent", "every 60m", "outbound_replies"),
    ("Access: weekly budget review (Access manager)", "weekly mon 07:30", "access_weekly"),
    # The Chief of Staff's weekly report and meeting (pos.weekly); the time is editable on Automations.
    ("Weekly company report and meeting (Asistent vedení)",
     os.environ.get("POS_WEEKLY_REPORT_SCHEDULE") or "weekly fri 14:00", "weekly_report"),
    ("Weekly meeting: close it after 24 h without an answer", "every 30m", "weekly_meeting_timeouts"),
    # Agents' meetings in a channel (pos.meetings): a turn over its time is skipped, a meeting over its
    # duration or budget goes to the decision.
    ("Meetings: turn and meeting time limits, budget", "every 1m", "meetings_tick"),
    # The sentinel (pos.monitor): its heartbeat must not stop; a daily health digest (quiet days: nothing).
    ("Sentinel: alert when its heartbeat stops", "every 2m", "sentinel_watch"),
    ("Sentinel: daily health digest in #team", "daily 08:00", "sentinel_digest"),
    # Grafana on .186 (pos.observability): when it stops answering, no alert can reach anyone.
    ("Grafana: alert when it stops answering", "every 5m", "grafana_watch"),
    # Every agent has a worker and the owner is never left without an answer (pos.workers): the pool's
    # keys, a worker silent for 10 min, an owner message unanswered for 10 min: an incident each.
    ("Agents: workers running, the owner's messages answered", "every 2m", "agents_watch"),
    # A routine more than an hour late (its loop stopped, the scheduler was off): an incident for the SRE.
    ("Routines: alert when one is more than an hour late", "every 10m", "routines_overdue"),
    # Business value (pos.business): reviews never wait over 24 h (the owner's go to the CEO first),
    # idle agents are flagged to the CEO, the company's GitHub issues and PRs reach the CTO's triage,
    # and a weekly report nobody published is published from its numbers.
    ("Reviews: over 24 h to the reviewer's lead, the owner's to the CEO; a daily digest per reviewer", "every 60m", "review_sla"),
    # Agent effectiveness (pos.learning, pos.effectiveness): lessons → the Performance Coach weekly, every
    # agent has a memory note, the CEO's business-focus digest (target ≥ 50 % of spend on business work).
    ("Lessons of the week → the Performance Coach", "weekly mon 06:45", "learning_weekly"),
    ("Every agent has a memory note", "daily 05:50", "memories_ensure"),
    ("CEO: business focus of the week (cost split, idle agents)", "weekly mon 07:50", "ceo_business_focus"),
    ("Agents without input for 7 days → the CEO", "weekly mon 07:45", "idle_agents"),
    ("GitHub: new issues and PRs in the company's repositories → triage", "every 30m", "github_triage"),
    ("Weekly report: publish a draft nobody published", "every 60m", "weekly_publish_overdue"),
    # Invoice mail -> the owner's Google Drive (pos.invoices): sure ones filed at once, unsure ones to the CFO.
    ("Invoices: file new invoice mail to Google Drive (CFO)", "every 5m", "invoices_poll"),
    # Projects (pos.project_info): fresh status summaries, the week's milestones, stale projects → the COO.
    ("Projects: weekly status, milestones and stale projects (COO)", "weekly mon 07:00", "projects_weekly"),
    # Customer mail (pos.support): a problem or bug → one task for the project's developer; the reply draft
    # after the fix (or when the time box runs out); follow-ups in the same thread.
    ("Customers: new mail → customer issues, reply drafts, follow-ups", "every 2m", "support_intake"),
    # Heads see their team's stuck work (pos.head_alerts.sweep): code, no model run. It replaced the 16 LLM
    # "Hlídání výpadků" routines (2x a day per head) and the COO's 11:00 second line.
    ("Heads: stuck team work (morning)", "daily 09:00", "heads_sweep"),
    ("Heads: stuck team work (afternoon)", "daily 15:00", "heads_sweep_afternoon"),
    ("COO: stuck work across teams (second line)", "daily 11:00", "heads_second_line"),
    # The owner channel (pos.asks, pos.promises): an unanswered decision takes its recommendation after
    # its default time; the CEO's dated promises to the owner are parsed and a missed one is flagged.
    ("Owner decisions: adopt the recommendation after the default time", "every 15m", "owner_decision_defaults"),
    ("CEO promises to the owner: parse and flag missed ones", "every 5m", "promises_tick"),
    # The company scorecard (pos.scorecard): measured goals updated, the day's snapshot for the Firma page
    # and the week-over-week deltas; the CEO's Monday plan and Friday board input read it.
    ("Scorecard: measured goals and the daily snapshot", "daily 06:10", "scorecard_daily"),
    # The agent company improves itself (pos.platform_loop): the CTO's meeting in #platform, the Friday retro.
    ("Platforma: zlepšení týdne (CTO, #platform)", "weekly mon 10:00", "platform_meeting"),
    ("Platforma: páteční retro s čísly (#platform, do týdenního reportu)", "weekly fri 12:00", "platform_retro"),
    # The closed self-improvement loop (pos.improve): the daily signal digest, web smoke and verification
    # of fix targets (code); one weekly triage task for the CTO and one instruction-tuning task for the
    # Performance Coach (two runs a week, under $1).
    ("Samozlepšení: denní signály, smoke webu, ověření oprav", "daily 05:40", "improve_daily"),
    ("Samozlepšení: týdenní triáž (CTO)", "weekly mon 08:30", "improve_triage"),
    ("Samozlepšení: ladění instrukcí (Performance Coach)", "weekly mon 08:40", "improve_coach"),
    # What is live (pos.reality): anonymous probes of every project's capabilities, the 72 h expiry of a
    # verification, and the promotion tasks held until what they promote is live.
    ("Co je živé: ověření funkcí zvenku, expirace, uvolnění propagace", "every 30m", "reality_tick"),
]

# The platform's own loops: they cannot be switched off (the owner switched off jobs 1-9 on
# 2026-09-25 and no routine ran, no stuck run was released and no budget was checked for two
# days). The schedule stays editable; a disabled row still runs (run_due) and is switched back
# on at start-up (enforce_core), with an audit line.
CORE_JOBS = ("member_schedules", "reap_runs", "budget_check", "routines_overdue")


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
    # A silent run is found within 5 min now (reap_runs): its job runs every minute, not every 5.
    conn.execute("UPDATE jobs SET schedule = 'every 1m' WHERE action = 'reap_runs' AND schedule = 'every 5m'")
    conn.commit()
    enforce_core(conn)


def enforce_core(conn: sqlite3.Connection) -> list[str]:
    """Core jobs switched off (by hand, or before they were core) are on again. Runs at start-up
    (scheduler.seed), so a deploy repairs the data by itself; audited."""
    rows = conn.execute(f"SELECT id, action FROM jobs WHERE enabled = 0 AND action IN "
                        f"({','.join('?' * len(CORE_JOBS))})", CORE_JOBS).fetchall()
    if not rows:
        return []
    ctx = Ctx(actors.owner_id(conn), via="scheduler")
    for r in rows:
        conn.execute("UPDATE jobs SET enabled = 1 WHERE id = ?", (r["id"],))
        audit.log(conn, ctx, "job_update", "job", r["id"], enabled=1, reason="core job: always on")
    conn.commit()
    log.warning("core jobs were switched off; switched on again: %s", [r["action"] for r in rows])
    return [r["action"] for r in rows]


def list_jobs(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
    return [{**dict(r), "enabled": bool(r["enabled"]) or r["action"] in CORE_JOBS, "core": r["action"] in CORE_JOBS,
             "last_result": json.loads(r["last_result"]) if r["last_result"] else None} for r in rows]


def update_job(conn: sqlite3.Connection, ctx: Ctx, job_id: int, changes: dict) -> dict:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise tasks.Invalid(f"no job {job_id}")
    sets = {}
    if "enabled" in changes:
        if not changes["enabled"] and row["action"] in CORE_JOBS:
            raise tasks.Invalid(f"“{row['name']}” is one of the platform's own loops and cannot be switched off; "
                                "change its schedule instead")
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
    if job["action"] not in ("a2a_sync", "reap_runs", "member_schedules", "routines_overdue", "knowlage_files",
                             "access_expire",
                             "access_watch", "sentinel_watch", "sentinel_digest", "grafana_watch", "review_sla",
                             "github_triage", "weekly_publish_overdue", "invoices_poll", "support_intake") or result.get("sent") \
            or result.get("finished") or result.get("released") or result.get("fired") or result.get("pushed") \
            or result.get("failed") or result.get("expired") or result.get("paused") or result.get("cap_alerts") \
            or result.get("alerted") or result.get("moved") or result.get("tasks") or result.get("published") \
            or result.get("filed") or result.get("duplicate") or result.get("issue") or result.get("triage")             or result.get("routed") or result.get("follow_up") or result.get("status_drafts")             or result.get("follow_ups") or result.get("errors") or result.get("stuck"):
        audit.log(conn, ctx, f"job:{job['action']}", "job", job["id"], **{k: v for k, v in result.items() if k != "task"})
    conn.commit()
    return result


def run_due(conn: sqlite3.Connection) -> list[str]:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    due = conn.execute(f"SELECT * FROM jobs WHERE (enabled = 1 OR action IN ({','.join('?' * len(CORE_JOBS))})) "
                       "AND next_run_at <= ?", (*CORE_JOBS, now)).fetchall()
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
