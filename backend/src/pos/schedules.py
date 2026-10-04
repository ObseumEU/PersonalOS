"""Schedules that members create for themselves or their team: "every day at
07:00 check the inbox", "every 2 h look at open PRs".

A schedule is a recurring task template. Each firing creates an ordinary task,
created as the schedule's creator, so everything that guards tasks guards
schedules too: the creator's permissions (a schedule never grants more), the
budget gate and the outbound rule when the task is worked on (Ú1: money, commitments
and personal channels need approval per firing), the kill switch (nothing fires while frozen).

- personal: the creator's own routine, assigned to itself;
- team: shared work, e.g. the HR agent's daily review of all agents; it shows
  on the pages of the creator and the assignee, and in Automations.

Limits for agents (HR policy): at most `max_active_schedules_per_agent` active
schedules, and nothing more often than every `min_schedule_interval_minutes`.
A firing is skipped while the previous task from the same schedule is still
open (a result waiting for review counts as closed), and deferred while the assignee has no runtime (budget, usage limits).
Schedules are versioned (history, restore) and archived instead of deleted.
"""

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from . import actors, agents, audit, tasks, versioning
from .core import Ctx, NotFound, now_iso
from .hr.policy import HRPolicy
from .scheduler import next_run

ENTITY = "schedule"
versioning.register(ENTITY, "schedules")
VISIBILITIES = ("personal", "team")
TEMPLATE_FIELDS = ("title", "notes", "definition_of_done", "priority", "topic", "estimate_min", "kind", "meeting")
KINDS = ("task", "meeting")  # a meeting schedule starts a meeting (pos.meetings) instead of making a task
MEETING_KEYS = ("channel", "topic", "agenda", "participants", "rounds", "facilitator", "budget_usd",
                "max_minutes", "turn_minutes")
# A firing is skipped while the previous task is still being worked on. A result waiting in `review`
# counts as closed for the routine: the work is done, and the daily SRE/Nexus/knowlage checks
# stopped for a week behind T-177..T-179 waiting for the CEO's review (prod 2026-09-27..10-04).
OPEN = ("inbox", "next", "working", "waiting")
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
    from .org import manages

    me = actors.get(conn, ctx.actor_id)
    if me["kind"] == "human" or ctx.actor_id in (s["created_by"], s["assignee_id"]):
        return
    if s["assignee_id"] and manages(conn, ctx.actor_id, s["assignee_id"]):
        return
    raise tasks.Forbidden("only the creator, the assignee, their lead or a person can change this schedule")


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
        raise tasks.Invalid(f"{e}. Use 'every 30m', 'every 2h', 'every 4d', 'every 4d 09:00', 'daily 07:00', 'weekdays 07:00' "
                            "or 'weekly fri 15:00' (Europe/Prague)") from e
    template = {k: fields[k] for k in TEMPLATE_FIELDS if fields.get(k) not in (None, "")}
    template.setdefault("title", name)
    if template.get("kind", "task") not in KINDS:
        raise tasks.Invalid(f"kind is one of {KINDS}")
    if template.get("kind") == "meeting":
        template["meeting"] = _meeting_spec(conn, template.get("meeting"), name)
        if fields.get("assignee") in (None, ""):  # the facilitator owns the routine
            fac = template["meeting"].get("facilitator")
            if fac:
                fields["assignee"] = {"type": "agent", "id": _facilitator_id(conn, fac)}
                if fields.get("visibility") in (None, "", "personal") and fields["assignee"]["id"] != ctx.actor_id:
                    fields["visibility"] = "team"
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
    twin = existing(conn, name, schedule, assignee_id)
    if twin is not None:  # idempotent: the same routine twice is one routine (two "Daily standup", 2026-09)
        return {**get(conn, twin), "existing": True}

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


def existing(conn: sqlite3.Connection, name: str, schedule: str, assignee_id: int,
             exclude: int | None = None) -> int | None:
    """The live (not archived) schedule with this name, schedule and assignee, if any: a
    schedule is unique by (name, schedule, assignee); names compare without case and spaces."""
    row = conn.execute("""SELECT id FROM schedules WHERE archived_at IS NULL AND assignee_id = ?
                          AND LOWER(TRIM(name)) = LOWER(TRIM(?)) AND LOWER(TRIM(schedule)) = LOWER(TRIM(?))
                          AND id IS NOT ? ORDER BY id LIMIT 1""",
                       (assignee_id, name, schedule, exclude)).fetchone()
    return row["id"] if row else None


def duplicates(conn: sqlite3.Connection) -> list[tuple[int, list[int]]]:
    """Live schedules that repeat an older one (same name, schedule, assignee): (kept id, [the repeats])."""
    groups: dict[tuple, list[int]] = {}
    for r in conn.execute("SELECT id, name, schedule, assignee_id FROM schedules WHERE archived_at IS NULL "
                          "ORDER BY id"):
        k = (r["name"].strip().lower(), r["schedule"].strip().lower(), r["assignee_id"])
        groups.setdefault(k, []).append(r["id"])
    return [(ids[0], ids[1:]) for ids in groups.values() if len(ids) > 1]


def _facilitator_id(conn: sqlite3.Connection, ref) -> int:
    from . import chat

    return chat.resolve_actor(conn, ref)["id"]


def _meeting_spec(conn: sqlite3.Connection, spec, name: str) -> dict:
    """A meeting schedule's template: channel, topic, agenda, participants, rounds, facilitator."""
    from . import chat

    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except ValueError as e:
            raise tasks.Invalid("meeting is an object: {channel, topic, agenda, participants, rounds, "
                                "facilitator}") from e
    if not isinstance(spec, dict):
        raise tasks.Invalid("a meeting schedule needs meeting={channel, topic, agenda, participants, rounds, "
                            "facilitator}")
    spec = {k: spec[k] for k in MEETING_KEYS if spec.get(k) not in (None, "", [])}
    spec.setdefault("topic", name)
    if not spec.get("channel") or not spec.get("participants"):
        raise tasks.Invalid("a meeting schedule needs a channel and participants")
    try:
        chat.resolve_channel(conn, spec["channel"])
        for p in spec["participants"]:
            chat.resolve_actor(conn, p)
    except (NotFound, chat.ChatError) as e:
        raise tasks.Invalid(str(e)) from e
    return spec


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
    if changes.get("meeting") not in (None, ""):
        changes = {**changes, "meeting": _meeting_spec(conn, changes["meeting"], s["name"])}
    for k in TEMPLATE_FIELDS:
        if k in changes:
            template[k] = changes[k]
    if template != s["template"]:
        sets["template"] = json.dumps(template, ensure_ascii=False)
    if "name" in changes and changes["name"]:
        sets["name"] = changes["name"]
    if ("name" in sets or "schedule" in sets) and existing(
            conn, sets.get("name", s["name"]), sets.get("schedule", s["schedule"]), s["assignee_id"],
            exclude=schedule_id) is not None:
        raise tasks.Invalid("the same routine (name, schedule, assignee) already exists; change or archive that one")
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
    elif s["template"].get("kind") == "meeting":
        result = _fire_meeting(conn, s, creator)
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


def _fire_meeting(conn: sqlite3.Connection, s: dict, creator: sqlite3.Row) -> dict:
    """A meeting schedule fires: the meeting starts, unless the last one still runs."""
    from . import meetings

    meetings.ensure_schema(conn)
    running = conn.execute("SELECT id FROM meetings WHERE schedule_id = ? AND status = 'running'",
                           (s["id"],)).fetchone()
    if running:
        return {"skipped": f"meeting {running['id']} from the last firing still runs"}
    spec = dict(s["template"].get("meeting") or {})
    try:
        m = meetings.start(conn, Ctx(creator["id"], via="schedule"), spec.pop("channel"), spec.pop("topic", s["name"]),
                           spec.pop("agenda", ""), spec.pop("participants", []), spec.pop("rounds", 2),
                           spec.pop("facilitator", None), schedule_id=s["id"], **spec)
    except Exception as e:  # noqa: BLE001 - the reason goes into last_result
        conn.rollback()
        return {"skipped": f"meeting not started: {e}"[:300]}
    return {"meeting": m["id"]}


OVERDUE_S = 3600  # a routine this late means its loop is not running: an incident for the SRE


def watch_overdue(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """Scheduler (every 10 min): active routines more than an hour past their time are an
    incident for the SRE (through the Monitor, pos.workers._incident; never the owner's
    inbox), once per routine and due time; resolved when none is late any more."""
    from . import monitor, workers

    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(seconds=OVERDUE_S)).isoformat(timespec="seconds")
    late = conn.execute("""SELECT id, name, schedule, next_run_at, last_run_at FROM schedules WHERE status = 'active'
                           AND archived_at IS NULL AND next_run_at < ? ORDER BY id""", (cutoff,)).fetchall()
    monitor.ensure_schema(conn)
    open_ = monitor._state(conn, "routines_overdue") or {}
    out: dict = {"late": [r["id"] for r in late]}
    if late:
        key = ",".join(f"{r['id']}@{r['next_run_at']}" for r in late)
        if key != open_.get("key"):
            iid = open_.get("iid") or f"routines-overdue-{now.strftime('%Y%m%d%H%M')}"
            lines = "\n".join(f"- #{r['id']} {r['name']} ({r['schedule']}): měla běžet {r['next_run_at']}, "
                              f"naposledy {r['last_run_at'] or 'nikdy'}" for r in late)
            workers._incident(
                conn, iid=iid, kind="routine_overdue",
                key="scheduler:routines", title=f"{len(late)} rutin(y) mešká přes hodinu",
                body=(f"Rutiny (plány členů) nespustily včas; jejich smyčka (úloha „Schedules of people and "
                      f"agents“, scheduler) asi neběží.\n\n{lines}\n\nZkontroluj plánovač (stránka Automatizace, "
                      "`docker compose logs --tail 100 api`)."),
                detail={"schedules": [r["id"] for r in late]})
            monitor._set_state(conn, "routines_overdue", {"key": key, "iid": iid})
            out["alerted"] = True
    elif open_.get("iid"):
        workers._incident(conn, iid=open_["iid"], kind="routine_overdue", key="scheduler:routines",
                          title="rutiny zase běží včas", body="", detail={}, resolved=True)
        monitor._set_state(conn, "routines_overdue", {})
    conn.commit()
    return out


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


# ------------------------------------------------------------------ routine checks close themselves

# A daily check (the SRE's capacity check, the Nexus and knowlage health passes) handed in all green
# is done: its one-line summary is the result, nobody reviews it (they were ~3 reviews a day for the
# CEO, T-177..T-179). Only findings create work: a finding no task tracks yet becomes a task for the
# checker's lead; a finding the summary already links (T-123) is tracked there.
CHECK_RE = re.compile(r"kontrol|check|health|zdrav", re.IGNORECASE)
FINDING_RE = re.compile(
    r"⚠|❌|🔴|\bred\b|červen|nad prahem|over (the )?threshold|nález|finding|problém|problem|chyb[ay]|\berrors?\b"
    r"|fail|selh|padá|neběží|nefunguj|\bdown\b|denied|neověřen|unverified|výpad|incident|kritick|critical",
    re.IGNORECASE)
_ZERO_RE = re.compile(r"\b0\s*(?:×\s*)?(?:error|chyb|fail|restart|oom)\w*(?:/\w+)?", re.IGNORECASE)
_REF_RE = re.compile(r"\bT-(\d{1,6})\b")


def _schedule_of(conn: sqlite3.Connection, row) -> sqlite3.Row | None:
    source = (row["source"] or "")
    if not source.startswith("schedule:") or not source[9:].isdigit():
        return None
    return conn.execute("SELECT * FROM schedules WHERE id = ?", (int(source[9:]),)).fetchone()


def is_routine_check(conn: sqlite3.Connection, row) -> bool:
    """A task from a daily routine check (a daily/weekday schedule whose name says check,
    kontrola or health, or whose template sets auto_close)."""
    s = _schedule_of(conn, row)
    if s is None:
        return False
    template = json.loads(s["template"] or "{}")
    if "auto_close" in template:
        return bool(template["auto_close"])
    daily = str(s["schedule"]).startswith(("daily", "weekdays"))
    return daily and bool(CHECK_RE.search(f"{s['name']} {template.get('title', '')}"))


def all_green(summary: str | None) -> bool:
    """The hand-in summary reports nothing to act on. "0 errors" is not a finding."""
    text = _ZERO_RE.sub(" ", summary or "")
    return not FINDING_RE.search(text)


def closes_itself(conn: sqlite3.Connection, ctx: Ctx, row) -> bool:
    """The assignee hands in its own routine check: it is done without a review."""
    return row["assignee_id"] == ctx.actor_id and row["status"] != "review" and is_routine_check(conn, row)


def after_check(conn: sqlite3.Connection, ctx: Ctx, task_id: int, summary: str | None) -> dict:
    """A routine check was closed without review: green is only a comment; a finding no linked
    task tracks becomes one task for the checker's lead (never the owner)."""
    from . import comments

    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    summary = (summary or "").strip()
    if all_green(summary):
        comments.log(conn, ctx, task_id, "Routine check all green: closed without review.", "system")
        return {"green": True}
    linked = [int(x) for x in _REF_RE.findall(summary) if int(x) != task_id and conn.execute(
        "SELECT 1 FROM tasks WHERE id = ? AND archived_at IS NULL", (int(x),)).fetchone()]
    if linked:
        comments.log(conn, ctx, task_id, "Routine check with findings, tracked in "
                     + ", ".join(tasks.display_id(x) for x in linked) + ": closed without review.", "system")
        return {"green": False, "tracked": [tasks.display_id(x) for x in linked]}
    me = actors.get(conn, row["assignee_id"]) if row["assignee_id"] else None
    lead = me["reports_to"] if me is not None else None
    lead_row = actors.get(conn, lead) if lead else None
    if lead_row is None or lead_row["archived_at"] or lead_row["is_owner"]:
        from . import business

        lead = business.ceo_id(conn)
    t = tasks.create(conn, ctx, {
        "title": f"Nález z rutiny: {row['title']}"[:200],
        "notes": (f"Purpose: act on what the routine check {tasks.display_id(task_id)} found.\n"
                  f"Source: {tasks.display_id(task_id)} ({row['title']}), by {me['name'] if me else '?'}.\n\n"
                  f"{summary[:3000]}"),
        "definition_of_done": "Each finding is fixed, or handed to whoever fixes it, or judged harmless with a note.",
        "status": "next", "priority": 2, "topic": row["topic"] or "ops", "source": "routine_finding",
        "assignee": {"type": "agent", "id": lead} if lead else "ai"})
    comments.log(conn, ctx, task_id, f"Routine check with findings: {t['ref']} for the lead; closed without review.",
                 "system")
    return {"green": False, "task": t["ref"]}
