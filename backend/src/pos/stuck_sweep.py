"""The stuck-work sweep: code, no model run, part of the core `routines_overdue` loop (every 10 min).

Prod 2026-10-05: the 18 "hlídání výpadků" routines (schedules 27-42) were paused on 10-04 to save
cost, and nothing woke what they used to: 21 agents' tasks sat in `waiting` past their do_date or
follow-up date (T-183 since 09-28, T-304, ...), because the worker's picker never offers a waiting
task. Every tick, deterministically:

1. **wake**: an agent's `waiting` task whose do_date or follow_up is today or earlier goes back to
   `next` (a system comment says why) and its agent is woken; at most once a day per task, so a task
   put back to `waiting` with the same date does not loop. It is NOT woken while it still waits on
   something real (2026-10-08: T-880, T-885, T-886, T-958, T-1042 were requeued at midnight only to
   re-wait): an open owner item (an open ask, a pending approval, an open browser handoff), an open
   subtask or a task its progress note says it waits on ("Čeká na T-958"), or a follow-up date still
   in the future (a do_date that passed does not override it);
2. **flag**: an agent's `working` task with no activity (a change, a comment, a run) for over
   IDLE_HOURS is flagged to the agent's lead (the CEO when it has none below the owner);
3. **digest**: once a day (from DIGEST_HOUR, Europe/Prague) each lead with something flagged or
   woken in its team gets one DM listing it; nothing to say, nothing sent. A task is flagged in one
   digest a day at most.
"""

import json
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone

from . import actors, audit
from .core import TZ

IDLE_HOURS = 24
DIGEST_HOUR = 8  # Europe/Prague
WAKE_EVERY_HOURS = 24


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _today(now: datetime) -> date:
    return now.astimezone(TZ).date()


def _done_since(conn: sqlite3.Connection, action: str, entity: str, entity_id: int, since: str) -> bool:
    return conn.execute("SELECT 1 FROM audit_log WHERE action = ? AND entity = ? AND entity_id = ? AND at >= ? "
                        "LIMIT 1", (action, entity, entity_id, since)).fetchone() is not None


def _lead(conn: sqlite3.Connection, agent_id: int) -> int | None:
    from . import business, head_alerts

    lead = head_alerts.lead_of(conn, agent_id)
    if lead:
        return lead
    ceo = business.ceo_id(conn)
    return ceo if ceo and ceo != agent_id else None


_REF_RE = re.compile(r"\bT-(\d{1,6})\b")


def _table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def waits_on(conn: sqlite3.Connection, t, today: str) -> str | None:
    """What a waiting task still waits on (a reason in Czech), or None when nothing holds it any more."""
    from . import tasks

    if t["follow_up"] and t["follow_up"] > today:
        return f"další kontrola {t['follow_up']}"
    tid = t["id"]
    if _table(conn, "owner_asks") and conn.execute(
            "SELECT 1 FROM owner_asks WHERE source_task_id = ? AND status = 'open' LIMIT 1", (tid,)).fetchone():
        return "otevřený dotaz na vlastníka"
    if conn.execute("SELECT 1 FROM approvals WHERE task_id = ? AND status = 'pending' LIMIT 1", (tid,)).fetchone():
        return "čeká na schválení"
    if _table(conn, "browser_handoffs"):
        from .handoff import LISTED

        if conn.execute(f"SELECT 1 FROM browser_handoffs WHERE task_id = ? AND status IN ({','.join('?' * len(LISTED))}) "
                        "LIMIT 1", (tid, *LISTED)).fetchone():
            return "otevřené předání vlastníkovi"
    sub = conn.execute("SELECT id FROM tasks WHERE parent_id = ? AND status != 'done' AND archived_at IS NULL "
                       "ORDER BY id LIMIT 1", (tid,)).fetchone()
    if sub:
        return f"otevřený podúkol {tasks.display_id(sub['id'])}"
    refs = {int(m) for m in _REF_RE.findall(t["progress_note"] or "")} - {tid}
    if refs:
        dep = conn.execute(f"SELECT id FROM tasks WHERE id IN ({','.join('?' * len(refs))}) AND status != 'done' "
                           "AND archived_at IS NULL ORDER BY id LIMIT 1", tuple(refs)).fetchone()
        if dep:
            return f"čeká na {tasks.display_id(dep['id'])}"
    return None


def wake_due(conn: sqlite3.Connection, now: datetime, apply: bool = True) -> list[dict]:
    """Agents' waiting tasks past their do_date / follow_up: back to `next`, the agent woken."""
    from . import business, comments, tasks, versioning, wake

    today = _today(now).isoformat()
    since = _iso(now - timedelta(hours=WAKE_EVERY_HOURS))
    out = []
    rows = conn.execute("""SELECT t.* FROM tasks t JOIN actors a ON a.id = t.assignee_id
                           WHERE t.status = 'waiting' AND t.archived_at IS NULL AND a.kind != 'human'
                           AND a.archived_at IS NULL AND a.is_owner = 0
                           AND COALESCE(t.do_date, t.follow_up) IS NOT NULL
                           AND MIN(COALESCE(t.do_date, '9999'), COALESCE(t.follow_up, '9999')) <= ?
                           ORDER BY t.id""", (today,)).fetchall()
    from . import reality

    for t in rows:
        if _done_since(conn, "stuck_wake", "task", t["id"], since):
            continue
        if reality.held(conn, t["id"]) is not None:
            continue  # promotion held until what it promotes is live (pos.reality); the probe job releases it
        if waits_on(conn, t, today):
            continue  # still waits on something real: it is woken by that, not by the calendar
        due = min(d for d in (t["do_date"], t["follow_up"]) if d)
        item = {"id": t["id"], "ref": tasks.display_id(t["id"]), "title": t["title"], "assignee": t["assignee_id"],
                "why": f"čekal do {due}"}
        out.append(item)
        if not apply:
            continue
        ctx = business.system_ctx(conn)
        versioning.update(conn, ctx, tasks.ENTITY, t["id"], {
            "status": "next", "retry_after": None,
            "progress_note": f"Probuzeno: termín čekání ({due}) prošel. Zkontroluj, na co úkol čekal, a pokračuj, "
                             "nebo nastav nový termín."}, action="stuck_wake")
        comments.log(conn, ctx, t["id"], f"Úkol čekal do {due} a termín prošel: vrácen do fronty (stuck sweep).",
                     "system")
        audit.log(conn, ctx, "stuck_wake", "task", t["id"], due=due)
        wake.wake(t["assignee_id"])
    return out


def idle_working(conn: sqlite3.Connection, now: datetime) -> list[dict]:
    """Agents' `working` tasks with no activity for over IDLE_HOURS."""
    from . import head_alerts, tasks

    cutoff = _iso(now - timedelta(hours=IDLE_HOURS))
    rows = conn.execute("""SELECT t.* FROM tasks t JOIN actors a ON a.id = t.assignee_id
                           WHERE t.status = 'working' AND t.archived_at IS NULL AND a.kind != 'human'
                           AND a.is_owner = 0 AND t.updated_at < ? ORDER BY t.id""", (cutoff,)).fetchall()
    moved = head_alerts._moved(conn, [r["id"] for r in rows])
    out = []
    for t in rows:
        last = max(t["updated_at"], moved.get(t["id"], ""))
        if last >= cutoff:
            continue
        hours = int((now - datetime.fromisoformat(last).astimezone(timezone.utc)).total_seconds() // 3600) \
            if last else None
        out.append({"id": t["id"], "ref": tasks.display_id(t["id"]), "title": t["title"],
                    "assignee": t["assignee_id"], "why": f"bez pohybu {hours} h" if hours is not None else "bez pohybu"})
    return out


def _digest_sent_today(conn: sqlite3.Connection, now: datetime) -> bool:
    midnight = now.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    return conn.execute("SELECT 1 FROM audit_log WHERE action = 'stuck_digest' AND at >= ? LIMIT 1",
                        (_iso(midnight),)).fetchone() is not None


def digest(conn: sqlite3.Connection, now: datetime, woken: list[dict], idle: list[dict],
           apply: bool = True) -> dict[str, int]:
    """Once a day, one DM per lead listing its team's woken and idle tasks."""
    from . import business, chat, tasks

    if now.astimezone(TZ).hour < DIGEST_HOUR or _digest_sent_today(conn, now):
        return {}
    since = _iso(now - timedelta(hours=24))
    woken_today = [{"id": r["id"], "ref": tasks.display_id(r["id"]), "title": r["title"], "assignee": r["assignee_id"],
                    "why": f"čekal do {json.loads(r['detail'] or '{}').get('due', '?')}, probuzen"}
                   for r in conn.execute("""SELECT t.id, t.title, t.assignee_id, l.detail FROM audit_log l
                                            JOIN tasks t ON t.id = l.entity_id WHERE l.action = 'stuck_wake'
                                            AND l.entity = 'task' AND l.at >= ? ORDER BY l.id""", (since,))]
    items = {i["id"]: i for i in [*woken_today, *woken, *idle]}.values()
    per_lead: dict[int, list[dict]] = {}
    for i in items:
        lead = _lead(conn, i["assignee"]) if i["assignee"] else None
        if lead:
            per_lead.setdefault(lead, []).append(i)
    sent: dict[str, int] = {}
    ctx = business.system_ctx(conn)
    for lead, its in per_lead.items():
        name = actors.get(conn, lead)["name"]
        sent[name] = len(its)
        if not apply:
            continue
        lines = [f"- {i['ref']} „{i['title'][:70]}“ ({actors.get(conn, i['assignee'])['name']}): {i['why']}"
                 for i in its[:25]]
        more = f"\n… a dalších {len(its) - 25}" if len(its) > 25 else ""
        chat.send_dm(conn, ctx, lead,
                     f"Denní přehled zaseknuté práce týmu: {len(its)} úkol(ů).\n" + "\n".join(lines) + more
                     + "\nCo udělat: úkol bez pohybu posuň (komentář, task_reassign, rozdělit), probuzený úkol "
                       "nech agenta dokončit nebo mu dej nový termín.",
                     priority="fyi", system=True)
        audit.log(conn, ctx, "stuck_digest", "actor", lead, tasks=[i["id"] for i in its])
    if apply and not per_lead:  # nothing to say today: still the day's one pass
        audit.log(conn, ctx, "stuck_digest", "actor", None, tasks=[])
    return sent


def sweep(conn: sqlite3.Connection, now: datetime | None = None, apply: bool = True) -> dict:
    now = now or datetime.now(timezone.utc)
    woken = wake_due(conn, now, apply)
    idle = idle_working(conn, now)
    sent = digest(conn, now, woken, idle, apply)
    if apply:
        conn.commit()
    out: dict = {}
    if woken:
        out["woken"] = [i["ref"] for i in woken]
    if sent:
        out["digests"] = sent
    return out
