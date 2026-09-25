"""Agents as users (AGENTS-SPEC 3): creation with limits, permissions, pause,
stop, messages and archiving.

Creating an agent goes through three independent checks:
1. the constitution (pos.guard): an agent never gets more permissions than
   its creator has (U5);
2. the HR agent (pos.hr): the limits from spec 3.2 and what to do when they
   are hit (reuse, replace, defer, ask the owner);
3. the budget keeper (pos.budget): the agent's budget class.

Purpose, lifetime and expiry are kept by pos.hr (hr_agent_profiles).
"""

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import actors, audit, runner, tasks, versioning
from .core import Ctx, NotFound, now_iso
from .core import Forbidden as _Forbidden

# Permission vocabulary. The owner implicitly has all of them ("*").
PERMISSIONS = {
    "tasks:read": "see tasks in the team and public layers",
    "tasks:write": "create and change tasks",
    "tasks:claim": "take tasks from the queue, report progress, hand in results",
    "approvals:request": "ask the owner to approve outbound actions",
    "agents:create": "create new agents (within limits, never with more permissions)",
    "messages:send": "message other members",
}
BUILTIN_PERMISSIONS = {
    actors.ASSISTANT_NAME: ["tasks:read", "tasks:write", "tasks:claim", "approvals:request", "agents:create",
                            "messages:send"],
    "Knowledge agent": ["tasks:read", "tasks:claim", "approvals:request"],
    "Nexus": ["tasks:read", "tasks:write", "tasks:claim", "approvals:request"],
    "HR agent": ["tasks:read", "tasks:write", "approvals:request"],
}
DEFAULT_AGENT_PERMISSIONS = ["tasks:read", "tasks:claim", "approvals:request"]
LIFETIMES = ("one_shot", "long_lived")
BUDGET_CLASSES = ("system", "normal", "low")

versioning.register("actor", "actors")


class AgentError(ValueError):
    pass


def permissions_of(conn: sqlite3.Connection, actor_id: int) -> set[str]:
    row = actors.get(conn, actor_id)
    if row["is_owner"]:
        return {"*"}
    return set(json.loads(row["permissions"] or "[]"))


def has_permission(conn: sqlite3.Connection, actor_id: int, perm: str) -> bool:
    row = actors.get(conn, actor_id)
    if row["kind"] == "human":
        return True
    have = permissions_of(conn, actor_id)
    return "*" in have or perm in have


def require(conn: sqlite3.Connection, ctx: Ctx, perm: str) -> None:
    if not has_permission(conn, ctx.actor_id, perm):
        raise _Forbidden(f"missing permission {perm}")


def seed_builtin_permissions(conn: sqlite3.Connection) -> None:
    for name, perms in BUILTIN_PERMISSIONS.items():
        conn.execute("UPDATE actors SET permissions = ? WHERE name = ? AND permissions = '[]'",
                     (json.dumps(perms), name))
    conn.commit()


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "agent"


def _write_instructions(data_dir: Path, name: str, text: str) -> str:
    # Kept under the data volume for now; step 6 moves agent instructions into
    # git so agents can change them through commits.
    folder = data_dir / "agents" / _slug(name)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "INSTRUCTIONS.md"
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return str(path)


def create_agent(conn: sqlite3.Connection, ctx: Ctx, *, name: str, purpose: str, lifetime: str = "one_shot",
                 instructions: str = "", permissions: list[str] | None = None, budget_class: str = "normal",
                 expires_at: str | None = None, runtime: str = "codex_worker", a2a_url: str | None = None,
                 data_dir: Path) -> dict:
    """Create an agent. Returns {"created": True, "agent": ..., "api_key": ...} or,
    when HR stops it at a limit, {"created": False, **HR's decision}."""
    from .guard import policy
    from .hr import service as hr
    from .budget import service as budget
    from .integrations import guard_actor

    name = name.strip()
    if not name or not purpose.strip():
        raise AgentError("an agent needs a name and a purpose")
    if lifetime not in LIFETIMES:
        raise AgentError(f"lifetime must be one of {LIFETIMES}")
    if budget_class not in BUDGET_CLASSES:
        raise AgentError(f"budget_class must be one of {BUDGET_CLASSES}")
    if actors.find_by_name(conn, name) or conn.execute("SELECT 1 FROM actors WHERE name = ?", (name,)).fetchone():
        raise AgentError(f"an actor called {name} already exists")
    if expires_at:
        datetime.fromisoformat(expires_at)
    requested = sorted(set(permissions if permissions is not None else DEFAULT_AGENT_PERMISSIONS))
    unknown = set(requested) - set(PERMISSIONS)
    if unknown:
        raise AgentError(f"unknown permissions: {sorted(unknown)}")
    if not has_permission(conn, ctx.actor_id, "agents:create"):
        raise _Forbidden("missing permission agents:create")

    creator = actors.get(conn, ctx.actor_id)
    # Constitution U5: never more than the creator has.
    policy.check_permission_grant(
        guard_actor(conn, ctx.actor_id),
        sorted(permissions_of(conn, ctx.actor_id)) if not creator["is_owner"] else ["*"],
        requested,
    ).raise_if_not_allowed()
    # System budget class is for platform agents only; others cannot hand it out.
    if budget_class == "system" and not creator["is_owner"]:
        budget_class = "normal"

    decision = hr.admit_agent(conn, ctx, name=name, purpose=purpose, lifetime=lifetime)
    if not decision.get("allowed"):
        return {"created": False, **decision}

    now = now_iso()
    row = versioning.insert(conn, ctx, "actor", {
        "kind": "agent", "name": name, "is_owner": 0, "created_at": now, "updated_at": now,
        "created_by": ctx.actor_id, "runtime": runtime, "a2a_url": a2a_url,
        "permissions": json.dumps(requested),
        "instructions_path": _write_instructions(data_dir, name, instructions or f"# {name}\n\n{purpose}\n"),
    })
    hr.register_agent(conn, row["id"], purpose=purpose, lifetime=lifetime, created_by=ctx.actor_id,
                      expires_at=expires_at)
    budget.set_agent_class(conn, str(row["id"]), budget_class)
    key = actors.create_key(conn, row["id"], label=f"created by {creator['name']}")
    audit.log(conn, ctx, "create_agent", "actor", row["id"], name=name, lifetime=lifetime, permissions=requested)
    conn.commit()
    return {"created": True, "agent": detail(conn, row["id"]), "api_key": key}


def set_permissions(conn: sqlite3.Connection, ctx: Ctx, agent_id: int, permissions: list[str]) -> dict:
    from .guard import policy
    from .integrations import guard_actor

    policy.authorize_change(guard_actor(conn, ctx.actor_id), "permissions").raise_if_not_allowed()
    unknown = set(permissions) - set(PERMISSIONS)
    if unknown:
        raise AgentError(f"unknown permissions: {sorted(unknown)}")
    _agent_row(conn, agent_id)
    versioning.update(conn, ctx, "actor", agent_id, {"permissions": json.dumps(sorted(set(permissions)))},
                      action="set_permissions")
    conn.commit()
    return detail(conn, agent_id)


def _agent_row(conn: sqlite3.Connection, agent_id: int) -> sqlite3.Row:
    row = actors.get(conn, agent_id)
    if row["kind"] == "human":
        raise NotFound(f"agent {agent_id}")
    return row


def pause(conn: sqlite3.Connection, ctx: Ctx, agent_id: int, paused: bool) -> dict:
    if actors.get(conn, ctx.actor_id)["kind"] != "human":
        raise _Forbidden("only people pause agents")
    _agent_row(conn, agent_id)
    versioning.update(conn, ctx, "actor", agent_id, {"paused_at": now_iso() if paused else None},
                      action="pause" if paused else "resume")
    conn.commit()
    return detail(conn, agent_id)


def stop(conn: sqlite3.Connection, ctx: Ctx, agent_id: int) -> dict:
    """Stop the agent's running work now and pause it."""
    _agent_row(conn, agent_id)
    stopped = runner.cancel_all(conn, f"stopped by {actors.get(conn, ctx.actor_id)['name']}", actor_id=agent_id)
    audit.log(conn, ctx, "stop_agent", "actor", agent_id, runs=stopped)
    return pause(conn, ctx, agent_id, True)


def archive(conn: sqlite3.Connection, ctx: Ctx, agent_id: int, reason: str = "") -> dict:
    _agent_row(conn, agent_id)
    versioning.archive(conn, ctx, "actor", agent_id)
    conn.execute("UPDATE api_keys SET revoked_at = ? WHERE actor_id = ? AND revoked_at IS NULL", (now_iso(), agent_id))
    audit.log(conn, ctx, "archive_agent", "actor", agent_id, reason=reason)
    conn.commit()
    return detail(conn, agent_id)


def restore(conn: sqlite3.Connection, ctx: Ctx, agent_id: int) -> dict:
    """Bring an archived agent back. It needs a new key (old ones stay revoked)."""
    _agent_row(conn, agent_id)
    versioning.unarchive(conn, ctx, "actor", agent_id)
    key = actors.create_key(conn, agent_id, label="restored")
    audit.log(conn, ctx, "restore_agent", "actor", agent_id)
    conn.commit()
    return {**detail(conn, agent_id), "api_key": key}


def retire_if_done(conn: sqlite3.Connection, ctx: Ctx, agent_id: int | None) -> bool:
    """A one-shot agent is archived once it has no open work (spec 3.3)."""
    if not agent_id:
        return False
    from .hr import store as hr_store

    row = actors.get(conn, agent_id)
    hr_store.ensure_schema(conn)
    profile = hr_store.profiles(conn).get(agent_id)
    if row["kind"] != "agent" or row["archived_at"] or not profile or profile["lifetime"] != "one_shot":
        return False
    open_work = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE assignee_id = ? AND archived_at IS NULL AND status NOT IN ('done', 'someday')",
        (agent_id,),
    ).fetchone()[0]
    if open_work:
        return False
    versioning.archive(conn, ctx, "actor", agent_id)
    conn.execute("UPDATE api_keys SET revoked_at = ? WHERE actor_id = ? AND revoked_at IS NULL", (now_iso(), agent_id))
    audit.log(conn, ctx, "retire_one_shot", "actor", agent_id)
    return True


# ------------------------------------------------------------------ messages

def send_message(conn: sqlite3.Connection, ctx: Ctx, to_actor: int, body: str, task_id: int | None = None) -> dict:
    if not body.strip():
        raise AgentError("empty message")
    if actors.get(conn, ctx.actor_id)["kind"] != "human":
        require(conn, ctx, "messages:send")
    actors.get(conn, to_actor)
    cur = conn.execute(
        "INSERT INTO messages (to_actor, from_actor, task_id, body, created_at) VALUES (?, ?, ?, ?, ?)",
        (to_actor, ctx.actor_id, task_id, body.strip(), now_iso()),
    )
    audit.log(conn, ctx, "message", "actor", to_actor, message_id=cur.lastrowid, task_id=task_id)
    conn.commit()
    return {"id": cur.lastrowid}


def take_messages(conn: sqlite3.Connection, actor_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT m.id, m.body, m.task_id, m.created_at, a.name AS from_name FROM messages m
           JOIN actors a ON a.id = m.from_actor WHERE m.to_actor = ? AND m.read_at IS NULL ORDER BY m.id""",
        (actor_id,),
    ).fetchall()
    if rows:
        conn.execute(f"UPDATE messages SET read_at = ? WHERE id IN ({','.join('?' for _ in rows)})",
                     [now_iso(), *[r["id"] for r in rows]])
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ views

def _status(row: sqlite3.Row, working: int, approvals_waiting: int) -> str:
    if row["archived_at"]:
        return "archived"
    if row["paused_at"]:
        return "paused"
    if approvals_waiting:
        return "approval"
    if working:
        return "working"
    return "idle"


def overview(conn: sqlite3.Connection) -> list[dict]:
    """Everyone for the Agents screen: people and agents, with their queue."""
    from .hr import store as hr_store
    from .budget import store as budget_store

    hr_store.ensure_schema(conn)
    budget_store.ensure_schema(conn)
    profiles = hr_store.profiles(conn)
    classes = budget_store.agents(conn)
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    out = []
    for row in conn.execute("SELECT * FROM actors ORDER BY is_owner DESC, archived_at IS NOT NULL, id"):
        q = conn.execute(
            """SELECT SUM(status = 'next') AS queued, SUM(status = 'working') AS working,
                      SUM(status = 'review') AS review, SUM(status = 'done' AND completed_at >= ?) AS done_today
               FROM tasks WHERE assignee_id = ? AND archived_at IS NULL""",
            (since, row["id"]),
        ).fetchone()
        current = conn.execute(
            "SELECT id, title FROM tasks WHERE assignee_id = ? AND status = 'working' AND archived_at IS NULL "
            "ORDER BY updated_at DESC LIMIT 1", (row["id"],)
        ).fetchone()
        waiting = conn.execute(
            "SELECT COUNT(*) FROM approvals WHERE requested_by = ? AND status = 'pending'", (row["id"],)
        ).fetchone()[0]
        p = profiles.get(row["id"])
        c = classes.get(str(row["id"]))
        out.append({
            "id": row["id"], "name": row["name"], "kind": row["kind"], "is_owner": bool(row["is_owner"]),
            "runtime": "web + phone" if row["kind"] == "human" else row["runtime"],
            "a2a_url": row["a2a_url"], "purpose": p["purpose"] if p else None,
            "lifetime": p["lifetime"] if p else None, "system": bool(p and p["system"]),
            "permissions": ["*"] if row["is_owner"] else json.loads(row["permissions"] or "[]"),
            "budget_class": c["budget_class"] if c else None,
            "status": "online" if row["kind"] == "human" and not row["archived_at"] else _status(row, q["working"] or 0, waiting),
            "last_seen_at": row["last_seen_at"], "paused": bool(row["paused_at"]),
            "archived": bool(row["archived_at"]), "created_by": row["created_by"],
            "queued": q["queued"] or 0, "working": q["working"] or 0, "review": q["review"] or 0,
            "done_today": q["done_today"] or 0, "approvals_waiting": waiting,
            "current": {"id": current["id"], "ref": tasks.display_id(current["id"]), "title": current["title"]} if current else None,
        })
    return out


def detail(conn: sqlite3.Connection, agent_id: int) -> dict:
    base = next((a for a in overview(conn) if a["id"] == agent_id), None)
    if base is None:
        raise NotFound(f"actor {agent_id}")
    row = actors.get(conn, agent_id)
    instructions = None
    if row["instructions_path"] and Path(row["instructions_path"]).exists():
        instructions = Path(row["instructions_path"]).read_text(encoding="utf-8")
    queue = conn.execute(
        """SELECT * FROM tasks WHERE assignee_id = ? AND archived_at IS NULL
           AND (status != 'done' OR completed_at >= ?) ORDER BY
           CASE status WHEN 'working' THEN 0 WHEN 'review' THEN 1 WHEN 'next' THEN 2 ELSE 3 END, id DESC LIMIT 30""",
        (agent_id, (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()),
    ).fetchall()
    runs = conn.execute("SELECT * FROM runs WHERE actor_id = ? ORDER BY id DESC LIMIT 10", (agent_id,)).fetchall()
    trace = audit.entries(conn, run_id=runs[0]["id"], limit=60) if runs else []
    if not trace:
        trace = [e for e in audit.entries(conn, limit=200) if e["actor_id"] == agent_id][:40]
    memory = conn.execute(
        "SELECT id, body, visibility, created_at FROM memories WHERE actor_id = ? AND archived_at IS NULL ORDER BY id DESC LIMIT 20",
        (agent_id,),
    ).fetchall()
    week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    stats = conn.execute(
        """SELECT SUM(status = 'done' AND completed_at >= ?) AS done, SUM(returned_count) AS returned,
                  SUM(interventions) AS interventions FROM tasks WHERE assignee_id = ?""",
        (week, agent_id),
    ).fetchone()
    return {
        **base, "instructions": instructions,
        "queue": [tasks.to_dict(t) for t in queue],
        "runs": [dict(r) for r in runs], "trace": list(reversed(trace)),
        "memory": [dict(m) for m in memory],
        "week": {"done": stats["done"] or 0, "returned": stats["returned"] or 0,
                 "interventions": stats["interventions"] or 0},
    }


def board(conn: sqlite3.Connection) -> list[dict]:
    """Work board: per active member, tasks by column (queued, working, needs you, done today)."""
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    rows = []
    for a in overview(conn):
        if a["archived"]:
            continue
        def pick(cond: str, params=()):
            return [
                {"ref": tasks.display_id(r["id"]), "id": r["id"], "title": r["title"], "status": r["status"],
                 "progress": r["progress"]}
                for r in conn.execute(
                    f"SELECT id, title, status, progress FROM tasks WHERE assignee_id = ? AND archived_at IS NULL "
                    f"AND {cond} ORDER BY COALESCE(priority, 4), id LIMIT 8", (a["id"], *params))
            ]

        needs_you = pick("status = 'review'") + [
            {"approval_id": r["id"], "title": f"Approve: {r['action']}", "status": "approval"}
            for r in conn.execute("SELECT id, action FROM approvals WHERE requested_by = ? AND status = 'pending'", (a["id"],))
        ]
        rows.append({
            "actor": {k: a[k] for k in ("id", "name", "kind", "is_owner", "status")},
            "queued": pick("status = 'next'"), "working": pick("status = 'working'"),
            "needs_you": needs_you, "done_today": pick("status = 'done' AND completed_at >= ?", (since,)),
        })
    return rows
