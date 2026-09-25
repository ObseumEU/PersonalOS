"""Company structure: each member's role (profession), who they report to and
their team; the Project manager agent that splits team work and routes it by
role; handoffs between members; the PM's daily standup.

Everyone reports to the Project manager, the Project manager reports to the
owner. Defaults are seeded at startup and for agents created later; the owner
changes them on the agent's page (set_org). Handoffs are versioned task
changes plus a message to the receiver, and a row in `handoffs` so the
network can draw them.
"""

import json
import sqlite3
from pathlib import Path

from . import actors, agents, audit, tasks, versioning
from .core import Ctx, Forbidden, NotFound, now_iso

PM_NAME = "Project manager"
PM_PURPOSE = ("Takes incoming team work, splits it into steps with a definition of done, assigns them by "
              "role, follows up and escalates to the owner.")
# Never more than its creator (the owner) has; tasks:claim lets it hand in its own tasks.
PM_PERMISSIONS = ["approvals:request", "messages:send", "tasks:claim", "tasks:read", "tasks:write"]

ROLES = ("owner", "project_manager", "assistant", "developer", "mail", "community", "knowledge",
         "automation", "hr", "deployer", "specialist")
# name -> (role, team) for the members PersonalOS knows by name.
DEFAULTS = {
    actors.OWNER_NAME: ("owner", "leadership"),
    PM_NAME: ("project_manager", "leadership"),
    actors.ASSISTANT_NAME: ("assistant", "operations"),
    "HR agent": ("hr", "operations"),
    "Dev agent": ("developer", "engineering"),
    "Deployer": ("deployer", "engineering"),
    "Mail agent": ("mail", "communication"),
    "Community agent": ("community", "communication"),
    "Knowledge agent": ("knowledge", "knowledge"),
    "Nexus": ("automation", "platform"),
}

STANDUP_NAME = "Daily standup"
STANDUP_SCHEDULE = "weekdays 08:30"
STANDUP_TEMPLATE = {
    "title": "Daily standup",
    "topic": "standup",
    "priority": 2,
    "estimate_min": 10,
    "notes": ("1. org_chart: the active agents.\n"
              "2. send_message to each one: 'Standup: done since yesterday, doing now, blocked by?' "
              "(priority fyi, this task's id).\n"
              "3. get_agent_status for each, and read replies already in your inbox.\n"
              "4. create_task for the owner (assignee 'me', topic standup): one line per agent "
              "(done / doing / blocked) and what needs the owner. Late replies go into tomorrow's summary.\n"
              "5. complete_task this one."),
    "definition_of_done": "Every active agent was asked; one summary task for the owner exists.",
}

REPO_AGENTS = Path(__file__).resolve().parents[3] / "agents"


def pm_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT id FROM actors WHERE name = ?", (PM_NAME,)).fetchone()
    return row["id"] if row else None


def ensure_pm(conn: sqlite3.Connection) -> int:
    """Create the Project manager once, as the owner, under the same checks as
    any agent: the constitution (never more than the creator), an HR profile
    and a budget class. Like the HR agent it is a platform agent, so it does
    not take one of the HR limit's slots and HR never retires it."""
    from .budget import service as budget
    from .budget import store as budget_store
    from .guard import policy
    from .hr import service as hr
    from .hr import store as hr_store
    from .integrations import guard_actor

    owner = actors.owner_id(conn)
    ctx = Ctx(owner, via="system")
    pid = pm_id(conn)
    if pid is None:
        policy.check_permission_grant(guard_actor(conn, owner), ["*"], PM_PERMISSIONS).raise_if_not_allowed()
        path = REPO_AGENTS / agents._slug(PM_NAME) / "INSTRUCTIONS.md"
        now = now_iso()
        role, team = DEFAULTS[PM_NAME]
        pid = versioning.insert(conn, ctx, "actor", {
            "kind": "agent", "name": PM_NAME, "is_owner": 0, "created_at": now, "updated_at": now,
            "created_by": owner, "runtime": "codex_worker", "permissions": json.dumps(PM_PERMISSIONS),
            "instructions_path": str(path) if path.exists() else None,
            "role": role, "team": team, "reports_to": owner,
        })["id"]
        audit.log(conn, ctx, "create_agent", "actor", pid, name=PM_NAME, lifetime="long_lived",
                  permissions=PM_PERMISSIONS)
    hr_store.ensure_schema(conn)
    if hr_store.get_profile(conn, pid) is None:
        hr.register_agent(conn, pid, purpose=PM_PURPOSE, lifetime="long_lived", created_by=owner,
                          system=True, ctx=ctx)
    budget_store.ensure_schema(conn)
    if str(pid) not in budget_store.agents(conn):
        budget.set_agent_class(conn, str(pid), "normal")
    conn.commit()
    return pid


def _defaults_for(conn: sqlite3.Connection, row: sqlite3.Row, pm: int | None) -> dict:
    """What an actor without an org position gets; empty if it has one."""
    sets: dict = {}
    role, team = DEFAULTS.get(row["name"], (None, None))
    if row["role"] is None and role:
        sets["role"] = role
    if row["team"] is None and team:
        sets["team"] = team
    if row["reports_to"] is None and not row["is_owner"] and row["kind"] != "human":
        target = actors.owner_id(conn) if row["id"] == pm or pm is None else pm
        if target != row["id"]:
            sets["reports_to"] = target
    return sets


def seed(conn: sqlite3.Connection) -> None:
    """Fill in missing org positions (idempotent; never overwrites the owner's choices)."""
    pm = pm_id(conn)
    for row in conn.execute("SELECT * FROM actors WHERE archived_at IS NULL").fetchall():
        sets = _defaults_for(conn, row, pm)
        if sets:
            conn.execute(f"UPDATE actors SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
                         [*sets.values(), row["id"]])
    conn.commit()


def ensure(conn: sqlite3.Connection) -> None:
    """Startup: the PM, everyone's position, the PM's standup."""
    ensure_pm(conn)
    seed(conn)
    ensure_standup(conn)


def place_new(conn: sqlite3.Connection, actor_id: int) -> None:
    """A freshly created agent reports to the PM (and gets its role if its name is known)."""
    sets = _defaults_for(conn, actors.get(conn, actor_id), pm_id(conn))
    if sets:
        conn.execute(f"UPDATE actors SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
                     [*sets.values(), actor_id])


# ------------------------------------------------------------------ the chart

def _status(row: sqlite3.Row, working: int) -> str:
    if row["archived_at"]:
        return "archived"
    if row["paused_at"]:
        return "paused"
    if row["kind"] == "human":
        return "online"
    return "working" if working else "idle"


def chart(conn: sqlite3.Connection, include_archived: bool = False) -> list[dict]:
    """Members with role, team, reports_to, status and level (0 = the top)."""
    rows = conn.execute("SELECT * FROM actors " + ("" if include_archived else "WHERE archived_at IS NULL ")
                        + "ORDER BY is_owner DESC, id").fetchall()
    by_id = {r["id"]: r for r in rows}
    working = {r["assignee_id"]: r["n"] for r in conn.execute(
        "SELECT assignee_id, COUNT(*) AS n FROM tasks WHERE status = 'working' AND archived_at IS NULL "
        "GROUP BY assignee_id")}

    def level(aid: int) -> int:
        seen, n = {aid}, 0
        up = by_id[aid]["reports_to"]
        while up in by_id and up not in seen:
            seen.add(up)
            n += 1
            up = by_id[up]["reports_to"]
        return n

    return [{
        "id": r["id"], "name": r["name"], "kind": r["kind"], "is_owner": bool(r["is_owner"]),
        "role": r["role"], "team": r["team"], "reports_to": r["reports_to"],
        "reports_to_name": by_id[r["reports_to"]]["name"] if r["reports_to"] in by_id else None,
        "status": _status(r, working.get(r["id"], 0)), "level": level(r["id"]),
    } for r in rows]


def set_org(conn: sqlite3.Connection, ctx: Ctx, actor_id: int, changes: dict) -> dict:
    """Change a member's role, team or manager (owner only). Versioned."""
    if not actors.get(conn, ctx.actor_id)["is_owner"]:
        raise Forbidden("only the owner changes the org structure")
    row = actors.get(conn, actor_id)
    sets: dict = {}
    for key in ("role", "team"):
        if key in changes:
            value = (changes[key] or "").strip().lower().replace(" ", "_") or None
            if value and len(value) > 40:
                raise tasks.Invalid(f"{key} is too long")
            sets[key] = value
    if "reports_to" in changes:
        target = changes["reports_to"]
        if target in (None, "", 0):
            if not row["is_owner"]:
                target = pm_id(conn) if actor_id != pm_id(conn) else actors.owner_id(conn)
            else:
                target = None
        if target is not None:
            boss = actors.get(conn, int(target))
            if boss["archived_at"]:
                raise tasks.Invalid(f"{boss['name']} is archived")
            if row["is_owner"]:
                raise tasks.Invalid("the owner reports to nobody")
            # No loops: walk up from the new manager.
            up, seen = boss["id"], set()
            while up is not None and up not in seen:
                if up == actor_id:
                    raise tasks.Invalid(f"{boss['name']} already reports to {row['name']}")
                seen.add(up)
                up = actors.get(conn, up)["reports_to"]
            target = boss["id"]
        sets["reports_to"] = target
    if sets:
        versioning.update(conn, ctx, "actor", actor_id, sets, action="set_org")
        conn.commit()
    return next(m for m in chart(conn, include_archived=True) if m["id"] == actor_id)


# ------------------------------------------------------------------ handoffs

def _member(conn: sqlite3.Connection, to) -> sqlite3.Row:
    if isinstance(to, int) or (isinstance(to, str) and to.strip().isdigit()):
        row = actors.get(conn, int(to))
    else:
        row = actors.find_by_name(conn, str(to).strip().lstrip("@"))
        if row is None:
            raise NotFound(f"no member called {to}")
    if row["archived_at"]:
        raise tasks.Invalid(f"{row['name']} is archived")
    return row


def handoff(conn: sqlite3.Connection, ctx: Ctx, task_id: int, to, note: str = "") -> dict:
    """Pass a task to another member with a note: a versioned task change, a
    message to the receiver and a handoff record. Agents hand off their own
    tasks with tasks:claim; someone else's task needs tasks:write."""
    from .killswitch import check_agent_may_act

    check_agent_may_act(conn, ctx)
    row = tasks._row(conn, ctx, task_id)
    me = actors.get(conn, ctx.actor_id)
    if me["kind"] != "human":
        own = row["assignee_id"] == ctx.actor_id
        if not (agents.has_permission(conn, ctx.actor_id, "tasks:write")
                or (own and agents.has_permission(conn, ctx.actor_id, "tasks:claim"))):
            raise Forbidden("handing off someone else's task needs tasks:write" if not own
                            else "missing permission tasks:claim")
    if row["status"] == "done":
        raise tasks.Invalid(f"{tasks.display_id(task_id)} is done")
    target = _member(conn, to)
    if target["id"] == row["assignee_id"]:
        raise tasks.Invalid(f"{tasks.display_id(task_id)} is already with {target['name']}")
    note = (note or "").strip()
    changes = tasks.resolve_assignee(conn, ctx, {"type": target["kind"], "id": target["id"]})
    if row["status"] in ("inbox", "working", "waiting"):
        changes["status"] = "next"
    changes["progress_note"] = f"Handed off by {me['name']}" + (f": {note}" if note else "")
    versioning.update(conn, ctx, tasks.ENTITY, task_id, changes, action="handoff")

    ref = tasks.display_id(task_id)
    run = conn.execute("SELECT id FROM runs WHERE actor_id = ? AND status = 'running' ORDER BY id DESC LIMIT 1",
                       (target["id"],)).fetchone()
    body = f"Handoff {ref} '{row['title']}' from {me['name']}." + (f" Note: {note}" if note else "")
    at = now_iso()
    message_id = conn.execute(
        """INSERT INTO messages (to_actor, from_actor, task_id, body, created_at, priority, run_id)
           VALUES (?, ?, ?, ?, ?, 'fyi', ?)""",
        (target["id"], ctx.actor_id, task_id, body, at, run["id"] if run else None),
    ).lastrowid
    handoff_id = conn.execute(
        """INSERT INTO handoffs (task_id, from_actor, to_actor, note, message_id, run_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (task_id, ctx.actor_id, target["id"], note, message_id, ctx.run_id, at),
    ).lastrowid
    audit.log(conn, ctx, "handoff", tasks.ENTITY, task_id, to=target["name"], handoff_id=handoff_id,
              message_id=message_id)
    return {"handoff_id": handoff_id, "task": ref, "from": me["name"], "to": target["name"],
            "status": changes.get("status", row["status"]), "message_id": message_id}


# ------------------------------------------------------------------ standup

def ensure_standup(conn: sqlite3.Connection) -> dict | None:
    """The PM's weekday standup: a team schedule the owner owns, created once.
    If the owner archives it, it stays archived."""
    from . import schedules

    pm = pm_id(conn)
    if pm is None:
        return None
    if conn.execute("SELECT 1 FROM schedules WHERE name = ? AND assignee_id = ?", (STANDUP_NAME, pm)).fetchone():
        return None
    out = schedules.create(conn, Ctx(actors.owner_id(conn), via="system"), {
        "name": STANDUP_NAME, "schedule": STANDUP_SCHEDULE, "visibility": "team",
        "assignee": {"type": "agent", "id": pm}, **STANDUP_TEMPLATE,
    })
    conn.commit()
    return out
