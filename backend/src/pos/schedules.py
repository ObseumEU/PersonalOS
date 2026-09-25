"""Schedules that members create for themselves or their team: "every day at
07:00 check the inbox", "every 2 h look at open PRs".

A schedule is a recurring task template. Each firing creates an ordinary task,
created as the schedule's creator, so everything that guards tasks guards
schedules too: the creator's permissions (a schedule never grants more), the
budget gate and the approval queue when the task is worked on (outbound actions
still need approval per firing), the kill switch (nothing fires while frozen).

- personal: the creator's own routine, assigned to itself;
- team: shared work, e.g. the HR agent's daily review of all agents; it shows
  on the pages of the creator and the assignee, and in Automations.

Limits for agents (HR policy): at most `max_active_schedules_per_agent` active
schedules, and nothing more often than every `min_schedule_interval_minutes`.
A firing is skipped while the previous task from the same schedule is still
open, and deferred while the assignee has no runtime (budget, usage limits).
Schedules are versioned (history, restore) and archived instead of deleted.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from . import actors, agents, audit, tasks, versioning
from .core import Ctx, NotFound, now_iso
from .hr.policy import HRPolicy
from .scheduler import next_run

ENTITY = "schedule"
versioning.register(ENTITY, "schedules")
VISIBILITIES = ("personal", "team")
TEMPLATE_FIELDS = ("title", "notes", "definition_of_done", "priority", "topic", "estimate_min")
OPEN = ("inbox", "next", "working", "waiting", "review")
DEFER_MINUTES = 15


def interval_minutes(schedule: str) -> int:
    """Shortest gap between two firings, for the minimum-interval limit."""
    start = datetime(2026, 1, 5, tzinfo=timezone.utc)  # a Monday
    first = next_run(schedule, start)
    return int(min((next_run(schedule, t) - t).total_seconds() for t in (first, next_run(schedule, first))) // 60)


def _policy() -> HRPolicy:
    return HRPolicy()


def to_dict(conn: sqlite3.Connection, row: sqlite3.Row | dict) -> dict:
    d = dict(row)
    d["template"] = json.loads(d["template"] or "{}")
    d["last_result"] = json.loads(d["last_result"]) if d.get("last_result") else None
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM actors")}
    d["created_by_name"] = names.get(d["created_by"])
    d["assignee_name"] = names.get(d["assignee_id"])
    d["last_task_ref"] = tasks.display_id(d["last_task_id"]) if d.get("last_task_id") else None
    return d


def get(conn: sqlite3.Connection, schedule_id: int) -> dict:
    row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
    if row is None:
        raise NotFound(f"no schedule {schedule_id}")
    return to_dict(conn, row)


def list_schedules(conn: sqlite3.Connection, *, actor_id: int | None = None, archived: bool = False) -> list[dict]:
    """All schedules, or those an actor created or is assigned (its page)."""
    sql = "SELECT * FROM schedules WHERE (archived_at IS NULL) = ?"
    args: list = [0 if archived else 1]
    if actor_id is not None:
        sql += " AND (created_by = ? OR assignee_id = ?)"
        args += [actor_id, actor_id]
    return [to_dict(conn, r) for r in conn.execute(sql + " ORDER BY id", args)]


def _may_manage(conn: sqlite3.Connection, ctx: Ctx, s: dict) -> None:
    me = actors.get(conn, ctx.actor_id)
    if me["kind"] == "human" or ctx.actor_id in (s["created_by"], s["assignee_id"]):
        return
    raise tasks.Forbidden("only the creator, the assignee or a person can change this schedule")


def _check_assign(conn: sqlite3.Connection, creator_id: int, assignee_id: int) -> None:
    """A schedule may only do what its creator may: tasks for yourself need
    tasks:claim, tasks for someone else need tasks:write."""
    perm = "tasks:claim" if assignee_id == creator_id else "tasks:write"
    if not agents.has_permission(conn, creator_id, perm):
        who = actors.get(conn, creator_id)["name"]
        raise tasks.Forbidden(f"{who} lacks {perm}, so it cannot schedule this")


def create(conn: sqlite3.Connection, ctx: Ctx, fields: dict) -> dict:
    fields = dict(fields)
    schedule = str(fields.get("schedule") or "").strip()
    name = str(fields.get("name") or "").strip()
    if not name:
        raise tasks.Invalid("a schedule needs a name")
    try:
        first = next_run(schedule, datetime.now(timezone.utc))
    except ValueError as e:
        raise tasks.Invalid(f"{e}. Use 'every 30m', 'every 2h', 'daily 07:00', 'weekdays 07:00' "
                            "or 'weekly fri 15:00' (Europe/Prague)") from e
    template = {k: fields[k] for k in TEMPLATE_FIELDS if fields.get(k) not in (None, "")}
    template.setdefault("title", name)
    visibility = fields.get("visibility") or "personal"
    if visibility not in VISIBILITIES:
        raise tasks.Invalid(f"visibility must be one of {VISIBILITIES}")
    assignee = fields.get("assignee")
    if assignee in (None, "") or (isinstance(assignee, str) and assignee.strip().lower() in ("me", "self", "já")):
        assignee = {"type": "agent", "id": ctx.actor_id}  # "me" is the creator itself, person or agent
    target = tasks.resolve_assignee(conn, ctx, assignee)
    if target["assignee_type"] not in ("human", "agent", "ai") or not target["assignee_id"]:
        raise tasks.Invalid("a schedule is assigned to a member (a person or an agent)")
    assignee_id = target["assignee_id"]
    if visibility == "personal" and assignee_id != ctx.actor_id:
        raise tasks.Invalid("a personal schedule is assigned to its creator; use visibility 'team' for others")
    _check_assign(conn, ctx.actor_id, assignee_id)

    me = actors.get(conn, ctx.actor_id)
    if me["kind"] != "human":
        p = _policy()
        if interval_minutes(schedule) < p.min_schedule_interval_minutes:
            raise tasks.Invalid(f"agents schedule at most every {p.min_schedule_interval_minutes} min")
        active = conn.execute("SELECT COUNT(*) FROM schedules WHERE created_by = ? AND archived_at IS NULL "
                              "AND status = 'active'", (ctx.actor_id,)).fetchone()[0]
        if active >= p.max_active_schedules_per_agent:
            raise tasks.Invalid(f"limit of {p.max_active_schedules_per_agent} active schedules reached; "
                                "pause or archive one, or ask the owner")
    now = now_iso()
    row = versioning.insert(conn, ctx, ENTITY, {
        "name": name, "schedule": schedule, "template": json.dumps(template, ensure_ascii=False),
        "assignee_id": assignee_id, "visibility": visibility, "status": "active",
        "next_run_at": first.isoformat(timespec="seconds"), "created_by": ctx.actor_id,
        "created_at": now, "updated_at": now,
    })
    return get(conn, row["id"])


def update(conn: sqlite3.Connection, ctx: Ctx, schedule_id: int, changes: dict) -> dict:
    s = get(conn, schedule_id)
    _may_manage(conn, ctx, s)
    sets: dict = {}
    if "status" in changes:
        if changes["status"] not in ("active", "paused"):
            raise tasks.Invalid("status is active or paused")
        if changes["status"] == "active" and s["status"] != "active" \
                and actors.get(conn, ctx.actor_id)["kind"] != "human":
            p = _policy()
            active = conn.execute("SELECT COUNT(*) FROM schedules WHERE created_by = ? AND archived_at IS NULL "
                                  "AND status = 'active'", (s["created_by"],)).fetchone()[0]
            if active >= p.max_active_schedules_per_agent:
                raise tasks.Invalid(f"limit of {p.max_active_schedules_per_agent} active schedules reached")
        sets["status"] = changes["status"]
        if changes["status"] == "active":
            sets["next_run_at"] = next_run(s["schedule"], datetime.now(timezone.utc)).isoformat(timespec="seconds")
    if "schedule" in changes:
        try:
            nxt = next_run(changes["schedule"], datetime.now(timezone.utc))
        except ValueError as e:
            raise tasks.Invalid(str(e)) from e
        if actors.get(conn, ctx.actor_id)["kind"] != "human" \
                and interval_minutes(changes["schedule"]) < _policy().min_schedule_interval_minutes:
            raise tasks.Invalid(f"agents schedule at most every {_policy().min_schedule_interval_minutes} min")
        sets["schedule"] = changes["schedule"]
        sets["next_run_at"] = nxt.isoformat(timespec="seconds")
    template = dict(s["template"])
    for k in TEMPLATE_FIELDS:
        if k in changes:
            template[k] = changes[k]
    if template != s["template"]:
        sets["template"] = json.dumps(template, ensure_ascii=False)
    if "name" in changes and changes["name"]:
        sets["name"] = changes["name"]
    if sets:
        versioning.update(conn, ctx, ENTITY, schedule_id, sets)
    return get(conn, schedule_id)


def archive(conn: sqlite3.Connection, ctx: Ctx, schedule_id: int) -> dict:
    _may_manage(conn, ctx, get(conn, schedule_id))
    versioning.archive(conn, ctx, ENTITY, schedule_id)
    return get(conn, schedule_id)


# ------------------------------------------------------------------ firing

def fire(conn: sqlite3.Connection, schedule_id: int, *, manual_by: Ctx | None = None) -> dict:
    """Create this firing's task, or say why not. Always moves next_run_at on."""
    from . import engines, killswitch

    s = get(conn, schedule_id)
    now = datetime.now(timezone.utc)
    nxt = next_run(s["schedule"], now).isoformat(timespec="seconds")
    result: dict
    creator = actors.get(conn, s["created_by"])
    assignee = actors.get(conn, s["assignee_id"])
    prev = conn.execute("SELECT status FROM tasks WHERE id = ?", (s["last_task_id"],)).fetchone() \
        if s["last_task_id"] else None
    if killswitch.is_frozen(conn):
        result = {"skipped": "kill switch is on"}
    elif creator["archived_at"] or (creator["kind"] != "human" and creator["paused_at"]):
        result = {"skipped": f"{creator['name']} is paused or archived"}
    elif assignee["archived_at"]:
        result = {"skipped": f"{assignee['name']} is archived"}
    elif prev and prev["status"] in OPEN:
        result = {"skipped": f"{tasks.display_id(s['last_task_id'])} from the last firing is still open"}
    else:
        try:
            _check_assign(conn, creator["id"], assignee["id"])
        except tasks.Forbidden as e:
            result = {"skipped": str(e)}
        else:
            engine, why, _ = engines.choose(conn, assignee["id"]) if assignee["kind"] != "human" else ("-", "", None)
            if engine is None:
                result = {"deferred": why[:300]}
                nxt = (now + timedelta(minutes=DEFER_MINUTES)).isoformat(timespec="seconds")
            else:
                ctx = Ctx(creator["id"], via="schedule")
                template = dict(s["template"])
                if (template.get("notes") or "").strip():
                    # Where it came from; without notes the generated description says it (task_descriptions).
                    template["notes"] = (f"{template['notes'].rstrip()}\n\nSource: the schedule "
                                         f"“{s['name']}” ({s['schedule']}), created by {creator['name']}.")
                t = tasks.create(conn, ctx, {**template, "status": "next",
                                             "source": f"schedule:{s['id']}",
                                             "assignee": {"type": "human" if assignee["kind"] == "human" else "agent",
                                                          "id": assignee["id"]}})
                conn.execute("UPDATE schedules SET last_task_id = ? WHERE id = ?", (t["id"], s["id"]))
                result = {"task": t["ref"]}
    conn.execute(
        "UPDATE schedules SET last_run_at = ?, last_result = ?, next_run_at = ?, runs = runs + ? WHERE id = ?",
        (now.isoformat(timespec="seconds"), json.dumps(result, ensure_ascii=False), nxt,
         1 if "task" in result else 0, s["id"]),
    )
    audit.log(conn, manual_by or Ctx(creator["id"], via="scheduler"), "schedule_fire", ENTITY, s["id"],
              manual=manual_by is not None, **result)
    conn.commit()
    return {**result, "next_run_at": nxt}


def run_due(conn: sqlite3.Connection) -> dict:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    due = conn.execute("SELECT id FROM schedules WHERE status = 'active' AND archived_at IS NULL "
                       "AND next_run_at <= ?", (now,)).fetchall()
    fired = {}
    for r in due:
        try:
            fired[r["id"]] = fire(conn, r["id"])
        except Exception as e:  # noqa: BLE001 - one bad schedule must not stop the others
            conn.rollback()
            fired[r["id"]] = {"error": str(e)[:300]}
    return {"fired": fired} if fired else {}
