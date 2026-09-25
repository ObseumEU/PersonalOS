"""Feedback between colleagues (REVIZE-FUNKCI 3.4): praise, critique and
suggestions any member gives any other, optionally about a task.

The receiver gets it in the inbox (fyi); an agent sees its open feedback in
the prompt of its next runs. The Agent coach owns the process: a daily job
hands it every agent with repeated open critique, it turns the pattern into an
instruction change (a task for the Dev agent) and resolves the feedback as
applied or dismissed with the reason. Versioned and audited.
"""

import sqlite3

from . import actors, audit, tasks, versioning
from .core import Ctx, Forbidden, now_iso

ENTITY = "feedback"
versioning.register(ENTITY, "feedback")
KINDS = ("praise", "critique", "suggestion")
STATUSES = ("open", "applied", "dismissed")
MAX_BODY = 4000
COACH = "Agent coach"


def give(conn: sqlite3.Connection, ctx: Ctx, to, body: str, kind: str = "critique",
         task_id: int | None = None, rating: int | None = None) -> dict:
    """Give feedback to a member (the caller commits)."""
    from .org import _member

    body = (body or "").strip()
    if not body:
        raise tasks.Invalid("empty feedback")
    if kind not in KINDS:
        raise tasks.Invalid(f"kind must be one of {KINDS}")
    if rating is not None and not 1 <= int(rating) <= 5:
        raise tasks.Invalid("rating is 1 to 5")
    target = _member(conn, to)
    if target["id"] == ctx.actor_id:
        raise tasks.Invalid("feedback is for someone else")
    if task_id is not None:
        tasks._row(conn, ctx, task_id)  # you may only refer to a task you can read
    now = now_iso()
    row = versioning.insert(conn, ctx, ENTITY, {
        "from_id": ctx.actor_id, "to_id": target["id"], "task_id": task_id, "kind": kind,
        "body": body[:MAX_BODY], "rating": rating, "status": "open",
        "created_by": ctx.actor_id, "created_at": now, "updated_at": now})
    author = actors.get(conn, ctx.actor_id)["name"]
    about = f" on {tasks.display_id(task_id)}" if task_id else ""
    from . import chat

    chat.send_dm(conn, ctx, target["id"], f"{kind.capitalize()} from {author}{about}: {body[:1500]}",
                 priority="fyi", attachments=[{"type": "task", "id": task_id}] if task_id else None, system=True)
    if task_id:
        from . import comments

        comments.log(conn, ctx, task_id, f"{kind.capitalize()} for {target['name']}: {body[:500]}", "comment")
    return view(conn, row)


def view(conn: sqlite3.Connection, row) -> dict:
    d = dict(row)
    names = {r["id"]: r["name"] for r in conn.execute(
        "SELECT id, name FROM actors WHERE id IN (?, ?)", (d["from_id"], d["to_id"]))}
    d["from_name"], d["to_name"] = names.get(d["from_id"]), names.get(d["to_id"])
    d["task_ref"] = tasks.display_id(d["task_id"]) if d.get("task_id") else None
    return d


def list_for(conn: sqlite3.Connection, *, to_id: int | None = None, from_id: int | None = None,
             status: str | None = None, limit: int = 100) -> list[dict]:
    where, params = ["archived_at IS NULL"], []
    for col, val in (("to_id", to_id), ("from_id", from_id), ("status", status)):
        if val is not None:
            where.append(f"{col} = ?")
            params.append(val)
    rows = conn.execute(f"SELECT * FROM feedback WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
                        (*params, limit)).fetchall()
    return [view(conn, r) for r in rows]


def may_resolve(conn: sqlite3.Connection, ctx: Ctx, fb: sqlite3.Row) -> bool:
    """The owner, the Agent coach, the receiver's lead, or the receiver for itself."""
    from .org import manages

    me = actors.get(conn, ctx.actor_id)
    return bool(me["is_owner"] or me["name"] == COACH or ctx.actor_id == fb["to_id"]
                or manages(conn, ctx.actor_id, fb["to_id"]))


def resolve(conn: sqlite3.Connection, ctx: Ctx, feedback_id: int, status: str, note: str = "",
            applied_ref: str | None = None) -> dict:
    if status not in ("applied", "dismissed"):
        raise tasks.Invalid("status is applied or dismissed")
    fb = conn.execute("SELECT * FROM feedback WHERE id = ?", (feedback_id,)).fetchone()
    if fb is None:
        raise tasks.Invalid(f"no feedback {feedback_id}")
    if not may_resolve(conn, ctx, fb):
        raise Forbidden("the owner, the Agent coach, the receiver's lead or the receiver resolves feedback")
    if status == "dismissed" and not note.strip():
        raise tasks.Invalid("say why it is dismissed")
    row = versioning.update(conn, ctx, ENTITY, feedback_id, {
        "status": status, "resolution": note.strip()[:1000] or None, "applied_ref": applied_ref,
        "resolved_by": ctx.actor_id, "resolved_at": now_iso()}, action=f"feedback_{status}")
    return view(conn, row)


def open_for_prompt(conn: sqlite3.Connection, actor_id: int, limit: int = 5) -> list[dict]:
    """The latest open feedback for an agent's next run."""
    return [{k: f[k] for k in ("id", "kind", "from_name", "task_ref", "body", "created_at")}
            for f in list_for(conn, to_id=actor_id, status="open", limit=limit)]


def coach_digest(conn: sqlite3.Connection) -> dict:
    """Daily: every agent with two or more open critiques becomes a task for the
    Agent coach (else the Dev agent) to turn the pattern into better instructions."""
    rows = conn.execute(
        """SELECT to_id, COUNT(*) AS n FROM feedback f JOIN actors a ON a.id = f.to_id
           WHERE f.status = 'open' AND f.kind = 'critique' AND f.archived_at IS NULL AND a.kind != 'human'
           GROUP BY to_id HAVING n >= 2""").fetchall()
    coach = actors.find_by_name(conn, COACH) or actors.find_by_name(conn, "Dev agent")
    if coach is None or not rows:
        return {"agents": 0}
    ctx = Ctx(actors.owner_id(conn), via="system")
    made = []
    for r in rows:
        name = actors.get(conn, r["to_id"])["name"]
        title = f"Zpětná vazba pro {name}: najít vzor a upravit instrukce"
        if conn.execute("SELECT 1 FROM tasks WHERE title = ? AND status != 'done' AND archived_at IS NULL",
                        (title,)).fetchone():
            continue
        items = list_for(conn, to_id=r["to_id"], status="open", limit=20)
        lines = "\n".join(f"- #{f['id']} {f['kind']} od {f['from_name']}"
                          + (f" ({f['task_ref']})" if f["task_ref"] else "") + f": {f['body'][:300]}" for f in items)
        t = tasks.create(conn, ctx, {
            "title": title, "assignee": {"type": "agent", "id": coach["id"]}, "priority": 2, "topic": "feedback",
            "notes": f"Účel: {name} dostal opakovanou kritiku ({r['n']}×). Najdi společný vzor a navrhni změnu "
                     f"instrukcí (agents/<slug>/INSTRUCTIONS.md, úkol pro Dev agenta), nebo kritiku zamítni s "
                     f"důvodem.\nOdkud: denní přehled zpětné vazby (pos.feedback).\n\nOtevřená zpětná vazba:\n{lines}",
            "definition_of_done": "Každá položka je vyřešená (feedback_resolve: applied s odkazem na úkol/commit, "
                                  "nebo dismissed s důvodem)."})
        made.append(t["ref"])
    audit.log(conn, ctx, "feedback_digest", None, None, tasks=made)
    conn.commit()
    return {"agents": len(rows), "tasks": made}
