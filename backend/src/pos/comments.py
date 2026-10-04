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
            _owner_comment_wakes(conn, ctx, row, body)
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


def _owner_comment_wakes(conn: sqlite3.Connection, ctx: Ctx, task: sqlite3.Row, body: str) -> int | None:
    """The owner commented on a task: its agent hears of it and the work resumes (prod: his comments
    on T-445, T-391 and T-354 woke nobody). The assignee gets a DM with the comment and is woken; a
    task assigned to the owner himself goes to the CEO. A task held back (retry_after) or waiting is
    queued again; a result waiting for review tells its reviewer instead. An ask_owner ticket has
    its own flow (pos.asks); an @mentioned member already got the comment. Returns who was told."""
    from . import chat, wake
    from .business import ceo_id, system_ctx

    if (task["source"] or "") == "ask_owner" or task["status"] == "done":
        return None
    owner = ctx.actor_id
    who = task["assignee_id"]
    if task["status"] == "review":
        who = tasks.reviewer_of(conn, task)
    if who == owner or who is None:
        who = ceo_id(conn) if task["assignee_id"] in (owner, None) else None
    if not who or who == owner:
        return None
    a = conn.execute("SELECT kind, archived_at FROM actors WHERE id = ?", (who,)).fetchone()
    if a is None or a["archived_at"] or a["kind"] == "human":
        return None
    sys_ctx = system_ctx(conn)
    ref = tasks.display_id(task["id"])
    if task["assignee_id"] == who and task["status"] in ("next", "working", "waiting", "inbox", "someday"):
        changes = {"retry_after": None}
        if task["status"] in ("waiting", "inbox", "someday"):
            changes["status"] = "next"
        versioning.update(conn, sys_ctx, tasks.ENTITY, task["id"], changes, action="resume")
    if who not in chat._mentions(conn, body, None):
        chat.send_dm(conn, sys_ctx, who,
                     f"Owner commented on {ref} '{task['title']}': {body[:1500]}"
                     + ("" if task["assignee_id"] == who else
                        " (it is with the owner or waits for review: take it over, answer, or act on it)"),
                     priority="change_plan", attachments=[{"type": "task", "id": task["id"]}], system=True)
    audit.log(conn, ctx, "owner_comment_wake", tasks.ENTITY, task["id"], to=who)
    wake.wake(who)
    return who


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
