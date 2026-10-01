"""A task's activity: comments, returns, reviews, handoffs and progress
(REVIZE-FUNKCI 3.1). `progress_note` on the task stays the latest state; the
history of what people and agents said about the work lives here, versioned.

An @Name in a comment reaches that member's inbox (a DM with the task attached).
"""

import sqlite3

from . import actors, audit, tasks, versioning
from .core import Ctx, now_iso

ENTITY = "task_comment"
versioning.register(ENTITY, "task_comments")
KINDS = ("comment", "return", "review", "handoff", "progress", "system")
MAX_BODY = 8000


def add(conn: sqlite3.Connection, ctx: Ctx, task_id: int, body: str, kind: str = "comment",
        notify: bool = True) -> dict:
    """Add to the task's activity (the caller commits)."""
    body = (body or "").strip()
    if not body:
        raise tasks.Invalid("an empty comment")
    if kind not in KINDS:
        raise tasks.Invalid(f"kind must be one of {KINDS}")
    row = tasks._row(conn, ctx, task_id)  # read access to the task
    now = now_iso()
    out = versioning.insert(conn, ctx, ENTITY, {
        "task_id": task_id, "author_id": ctx.actor_id, "body": body[:MAX_BODY], "kind": kind,
        "created_by": ctx.actor_id, "created_at": now, "updated_at": now})
    if notify and kind == "comment":
        _notify_mentions(conn, ctx, row, body)
        from . import asks

        asks.on_comment(conn, ctx, row, body)  # an answer on an ask_owner ticket reaches the asker
        if actors.get(conn, ctx.actor_id)["is_owner"]:
            from . import learning

            learning.on_owner_comment(conn, ctx, row, body)  # the owner correcting a result: a lesson
    return _view(conn, out)


def _notify_mentions(conn: sqlite3.Connection, ctx: Ctx, task: sqlite3.Row, body: str) -> None:
    from . import chat
    from .visibility import can_read

    ref = tasks.display_id(task["id"])
    author = actors.get(conn, ctx.actor_id)["name"]
    for aid in chat._mentions(conn, body, None):
        if aid == ctx.actor_id or not can_read(conn, tasks.ENTITY, task, aid):
            continue
        chat.send_dm(conn, ctx, aid, f"{author} on {ref} '{task['title']}': {body[:1500]}",
                     priority="fyi", attachments=[{"type": "task", "id": task["id"]}], system=True)
        audit.log(conn, ctx, "comment_mention", tasks.ENTITY, task["id"], to=aid)


def _view(conn: sqlite3.Connection, row) -> dict:
    d = dict(row)
    a = conn.execute("SELECT name, kind FROM actors WHERE id = ?", (d["author_id"],)).fetchone()
    d["author_name"], d["author_kind"] = (a["name"], a["kind"]) if a else (None, None)
    return d


def list_for(conn: sqlite3.Connection, ctx: Ctx, task_id: int, limit: int = 100) -> list[dict]:
    """The task's activity, oldest first (the last `limit` entries)."""
    tasks._row(conn, ctx, task_id)
    rows = conn.execute(
        """SELECT * FROM (SELECT * FROM task_comments WHERE task_id = ? AND archived_at IS NULL
           ORDER BY id DESC LIMIT ?) ORDER BY id""", (task_id, limit)).fetchall()
    return [_view(conn, r) for r in rows]


def log(conn: sqlite3.Connection, ctx: Ctx, task_id: int, body: str, kind: str) -> None:
    """Record something that happened to the task (return, review, handoff,
    progress); never fails the action it records."""
    try:
        add(conn, ctx, task_id, body, kind, notify=False)
    except Exception:  # noqa: BLE001 - activity is a record, not a gate
        pass
