"""Reassign a task to another member, and the agent starts on it at once.

One place for the whole hand-over, used by the web UI (task list, task detail,
topic view), the REST API and the MCP tool `task_reassign`:

1. Check first whether the new assignee can actually take the task: it is
   active, it may claim tasks (its permissions), it may read the task
   (constitution U6: private data stays private), and it is not held by the
   kill switch, a pause or the budget (Rozpočtář). A problem is a readable
   reason, shown on the task, never a silent failure.
2. Move the task: the previous assignee's claim is released (the task goes
   back to `next`) and its running run for this task is cancelled cleanly
   (the worker sees `run_cancelled` at its next safe point and stops).
3. Record it: a versioned history entry (action `reassign`) and an audit entry.
4. Tell the new assignee: a DM with the task and its description.
5. Wake its worker (pos.wake), so it claims and starts the task right away
   instead of at its next poll.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

from . import actors, agents, audit, killswitch, runner, task_descriptions, tasks, versioning
from .core import Ctx, Forbidden, NotFound, now_iso
from .visibility import can_read

AGENT_KINDS = ("ai", "agent")
# Reasons that come and go (the owner may still queue the task for later with force).
SOFT = ("paused", "frozen", "budget")
WORKER_ONLINE_MINUTES = 3


class Refused(tasks.Invalid):
    """The new assignee cannot take the task; `reasons` says why."""

    def __init__(self, target: str, reasons: list[dict]):
        self.target = target
        self.reasons = reasons
        super().__init__(f"{target} cannot take this task: " + "; ".join(r["text"] for r in reasons))


def _member(conn: sqlite3.Connection, to) -> sqlite3.Row:
    if isinstance(to, dict):
        to = to.get("id") or to.get("name")
    if isinstance(to, int) or (isinstance(to, str) and to.strip().isdigit()):
        return actors.get(conn, int(to))
    name = str(to or "").strip().lstrip("@")
    if name.lower() in ("me", "owner"):
        return actors.get(conn, actors.owner_id(conn))
    if name.lower() in ("ai", "assistant"):
        return actors.get(conn, actors.assistant_id(conn))
    row = actors.find_by_name(conn, name) or conn.execute(
        "SELECT * FROM actors WHERE lower(name) = lower(?) AND archived_at IS NULL", (name,)).fetchone()
    if row is None:
        raise NotFound(f"no member called {to}")
    return row


def blockers(conn: sqlite3.Connection, task_row, target: sqlite3.Row, *, frozen: bool | None = None) -> list[dict]:
    """Why `target` cannot take the task now: [{code, text, soft}], empty when it can."""
    out: list[dict] = []

    def add(code: str, text: str) -> None:
        out.append({"code": code, "text": text, "soft": code in SOFT})

    if target["archived_at"]:
        add("archived", f"{target['name']} is archived")
        return out
    if target["kind"] not in AGENT_KINDS:
        if task_row is not None and not can_read(conn, tasks.ENTITY, task_row, target["id"]):
            add("private", f"constitution U6 (private data stays private): {tasks.display_id(task_row['id'])} "
                           f"is private and not shared with {target['name']}")
        return out
    if not agents.has_permission(conn, target["id"], "tasks:claim"):
        add("permission", f"{target['name']} lacks the permission tasks:claim, so it cannot take tasks "
                          "(constitution U5: permissions are never widened to make work fit)")
    if task_row is not None and not can_read(conn, tasks.ENTITY, task_row, target["id"]):
        add("private", f"constitution U6 (private data stays private): {tasks.display_id(task_row['id'])} "
                       f"is private and not shared with {target['name']}")
    if target["paused_at"]:
        add("paused", f"{target['name']} is paused by the owner")
    if killswitch.is_frozen(conn) if frozen is None else frozen:
        add("frozen", "the kill switch is on; no agent starts work until the owner turns it off")
    from . import engines

    engine, why, _ = engines.choose(conn, target["id"])
    if engine is None:
        add("budget", f"budget (Rozpočtář): {why}")
    return out


def _worker_online(conn: sqlite3.Connection, target: sqlite3.Row) -> bool:
    from . import wake

    if wake.waiting(target["id"]):
        return True
    seen = target["last_seen_at"]
    if not seen:
        return False
    try:
        at = datetime.fromisoformat(seen)
    except ValueError:
        return False
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - at < timedelta(minutes=WORKER_ONLINE_MINUTES)


def candidates(conn: sqlite3.Connection, ctx: Ctx, task_id: int | None = None) -> list[dict]:
    """Everyone the task could go to, for the picker: role, engine/model badge and
    whether they can take it now (with the reason when not)."""
    from .engine_view import Viewer

    row = tasks._row(conn, ctx, task_id) if task_id is not None else None
    view = Viewer(conn)
    frozen = killswitch.is_frozen(conn)
    out = []
    for a in conn.execute("SELECT * FROM actors WHERE archived_at IS NULL ORDER BY is_owner DESC, kind, name"):
        ev = view.for_actor(a)
        reasons = blockers(conn, row, a, frozen=frozen)
        out.append({
            "id": a["id"], "name": a["name"], "kind": a["kind"], "is_owner": bool(a["is_owner"]),
            "role": a["role"], "team": a["team"],
            "engine": ev["now"]["engine"] if ev else None, "model": ev["now"]["model"] if ev else None,
            "engine_label": ev["now"]["label"] if ev else None,
            "current": bool(row is not None and row["assignee_id"] == a["id"]),
            "available": not reasons, "blocked": reasons,
            "worker_online": _worker_online(conn, a) if a["kind"] in AGENT_KINDS else None,
        })
    return out


def reassign(conn: sqlite3.Connection, ctx: Ctx, task_id: int, to, note: str = "", *, force: bool = False) -> dict:
    """Hand the task to `to` (actor id, name, 'me' or 'ai'). See the module docstring.

    `force` queues the task even when only soft reasons (pause, kill switch,
    budget) hold the agent back; the task then shows why it is waiting."""
    from . import chat, wake

    me = actors.get(conn, ctx.actor_id)
    if me["kind"] != "human":
        killswitch.check_agent_may_act(conn, ctx)
        if not agents.has_permission(conn, ctx.actor_id, "tasks:write"):
            raise Forbidden("reassigning a task needs tasks:write")
    row = tasks._row(conn, ctx, task_id)
    ref = tasks.display_id(task_id)
    if row["archived_at"]:
        raise tasks.Invalid(f"{ref} is archived")
    if row["status"] == "done":
        raise tasks.Invalid(f"{ref} is done; reopen it before handing it to someone")
    target = _member(conn, to)
    if target["id"] == row["assignee_id"]:
        raise tasks.Invalid(f"{ref} is already with {target['name']}")
    note = (note or "").strip()

    reasons = blockers(conn, row, target)
    hard = [r for r in reasons if not r["soft"]]
    if hard or (reasons and not force):
        # Nothing moves; the refusal is on the task's audit trail and in the answer.
        audit.log(conn, ctx, "reassign_refused", tasks.ENTITY, task_id, to=target["name"],
                  reasons=[r["text"] for r in reasons])
        conn.commit()
        raise Refused(target["name"], reasons)

    prev_id, prev_name = row["assignee_id"], row["assignee_name"]
    # Release the previous assignee's claim: cancel its live run on this task.
    cancelled = []
    for r in conn.execute("SELECT id, actor_id FROM runs WHERE task_id = ? AND status = 'running'", (task_id,)).fetchall():
        if r["actor_id"] != target["id"] and runner.cancel(conn, r["id"], f"{ref} was reassigned to {target['name']} "
                                                                          f"by {me['name']}"):
            cancelled.append(r["id"])

    changes = tasks.resolve_assignee(conn, ctx, {"type": target["kind"], "id": target["id"]})
    if target["kind"] in AGENT_KINDS or row["status"] in ("inbox", "working", "waiting", "review"):
        changes["status"] = "next"
    changes["progress"] = 0
    by = f"Reassigned from {prev_name or 'nobody'} to {target['name']} by {me['name']}"
    changes["progress_note"] = (f"{by}: {note}" if note else by)[:500]
    if row["description_generated"] or task_descriptions.needs_description(row["notes"]):
        changes["notes"] = task_descriptions.build(conn, {**dict(row), **changes}, task_id,
                                                   extra=[changes["progress_note"]])
        changes["description_generated"] = 1
    versioning.update(conn, ctx, tasks.ENTITY, task_id, changes, action="reassign")

    # Tell the new assignee what the task is and what it is for.
    message_id = None
    if target["id"] != ctx.actor_id:
        desc = (changes.get("notes") or row["notes"] or "").strip()
        dod = row["definition_of_done"]
        body = f"{me['name']} assigned you {ref} '{row['title']}'." + (f" Note: {note}" if note else "")
        body += f"\n\n{desc[:3000]}" if desc else ""
        body += f"\n\nDone when: {dod}" if dod else ""
        message_id = chat.send_dm(conn, ctx, target["id"], body[:chat.MAX_BODY], priority="fyi",
                                  attachments=[{"type": "task", "id": task_id}], system=True)["id"]
    audit.log(conn, ctx, "reassign", tasks.ENTITY, task_id, **{
        "from": prev_name, "from_id": prev_id, "to": target["name"], "to_id": target["id"],
        "cancelled_runs": cancelled, "message_id": message_id, "forced": bool(reasons),
        "waiting_for": [r["text"] for r in reasons]})
    conn.commit()
    woke = wake.wake(target["id"]) if target["kind"] in AGENT_KINDS else 0
    if prev_id and prev_id != target["id"]:
        wake.wake(prev_id)  # its worker re-checks and sees the run is cancelled
    return {
        "task": tasks.get(conn, ctx, task_id), "from": prev_name, "to": target["name"],
        "cancelled_runs": cancelled, "message_id": message_id, "woke_workers": woke,
        "waiting_for": reasons,
    }


def live(conn: sqlite3.Connection, ctx: Ctx, task_id: int) -> dict:
    """The task's live state for its detail view: who has it, the current run
    (engine, model, heartbeat), progress notes, and why it is not moving."""
    from . import wake
    from .engine_view import label, model_for

    row = tasks._row(conn, ctx, task_id)
    assignee = None
    reasons: list[dict] = []
    if row["assignee_id"]:
        a = actors.get(conn, row["assignee_id"])
        assignee = {"id": a["id"], "name": a["name"], "kind": a["kind"], "role": a["role"]}
        if a["kind"] in AGENT_KINDS:
            from .engine_view import Viewer

            ev = Viewer(conn).for_actor(a)
            assignee.update({"engine": ev["now"]["engine"], "model": ev["now"]["model"],
                             "engine_label": ev["now"]["label"], "worker_online": _worker_online(conn, a),
                             "worker_waiting": wake.waiting(a["id"]) > 0})
            if row["status"] in ("next", "working"):
                reasons = blockers(conn, row, a)

    def run_view(r) -> dict:
        model = r["model"] or (model_for(r["engine"], None) if r["engine"] else None)
        return {"id": r["id"], "status": r["status"], "engine": r["engine"], "model": model,
                "label": label(r["engine"], model) if r["engine"] else None, "actor_id": r["actor_id"],
                "actor_name": r["actor_name"], "started_at": r["started_at"], "ended_at": r["ended_at"],
                "heartbeat_at": r["heartbeat_at"], "detail": (r["detail"] or "")[:500] or None}

    runs = conn.execute(
        """SELECT r.*, a.name AS actor_name FROM runs r JOIN actors a ON a.id = r.actor_id
           WHERE r.task_id = ? ORDER BY r.id DESC LIMIT 6""", (task_id,)).fetchall()
    current = next((run_view(r) for r in runs if r["status"] == "running"), None)
    last_blocked = next((r for r in runs if r["status"] == "blocked"), None)
    if last_blocked is not None and not current and row["status"] == "next" and not any(
            r["status"] in ("ok", "error", "cancelled") and r["id"] > last_blocked["id"] for r in runs):
        # The worker tried and was refused: say so (budget, pause, kill switch).
        text = f"last run was blocked: {last_blocked['detail']}"
        if not any(r["text"] in text for r in reasons):
            reasons.append({"code": "run_blocked", "text": text, "soft": True})

    notes = []
    for h in versioning.history(conn, tasks.ENTITY, task_id):
        n = h["data"].get("progress_note")
        if n and (not notes or notes[-1]["note"] != n):
            notes.append({"at": h["at"], "by": h["actor_name"], "note": n, "progress": h["data"].get("progress"),
                          "action": h["action"]})
    if row["status"] == "working" and current:
        state = "working"
    elif reasons and row["status"] in ("next", "working"):
        state = "blocked"
    elif row["status"] == "next" and assignee and assignee.get("kind") in AGENT_KINDS:
        state = "queued"
    else:
        state = row["status"]
    return {
        "ref": tasks.display_id(task_id), "status": row["status"], "state": state,
        "progress": row["progress"], "progress_note": row["progress_note"],
        "assignee": assignee, "run": current, "runs": [run_view(r) for r in runs],
        "blocked": reasons, "notes": notes[-10:][::-1], "at": now_iso(),
    }
