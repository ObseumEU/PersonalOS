"""Reviews are real work: a result waiting for an agent's review is a task in that agent's queue.

Measured 2026-09-25..10-04: 108-132 tasks sat in `review` while no agent run started for 11 h; the
QA Reviewer ran once in 9 days. A review request was only a DM and a wake, and an agent's worker
starts a run only for a task in its queue (pos.api_worker._next_work), so nobody reviewed.

Now every result waiting for an agent reviewer has one open work item "Review: T-x …" (source
`review:<id>`) assigned to the reviewer:

- `ensure`: create it (or move it to the current reviewer) when a result is handed in, handed to
  another reviewer, or escalated by the SLA (pos.business.review_sla);
- `close_for`: close it when the result leaves `review` (accepted, returned, auto-accepted);
- `stale`: the picker skips (and closes) an item whose result is no longer waiting for this agent;
- `sync`: the hourly SLA job creates missing items and closes stale ones (any path that changed a
  task without these hooks is caught within the hour).

The item closes itself: its reviewer is its assignee (pos.tasks.may_finish), so completing it never
asks for a review of a review. People reviewers (not the owner) keep the DM; the owner sees Needs
review on the board. The review policy's auto-accept (pos.review_policy) is unchanged.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

from . import actors, audit, versioning
from .core import Ctx, now_iso

SOURCE = "review:"
RECREATE_HOURS = 24  # an item completed without reviewing comes back at most once a day (sync)


def source_of(task_id: int) -> str:
    return f"{SOURCE}{task_id}"


def reviewed_id(row) -> int | None:
    """The reviewed task's id when `row` is a review work item."""
    src = (row["source"] or "") if row is not None else ""
    if src.startswith(SOURCE) and src[len(SOURCE):].isdigit():
        return int(src[len(SOURCE):])
    return None


def is_item(row) -> bool:
    return reviewed_id(row) is not None


def _agent_reviewer(conn: sqlite3.Connection, rid: int | None) -> bool:
    if not rid:
        return False
    r = conn.execute("SELECT kind, is_owner, archived_at FROM actors WHERE id = ?", (rid,)).fetchone()
    return r is not None and r["kind"] != "human" and not r["is_owner"] and not r["archived_at"]


def _open_items(conn: sqlite3.Connection, task_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM tasks WHERE source = ? AND archived_at IS NULL AND status != 'done' "
                        "ORDER BY id", (source_of(task_id),)).fetchall()


def _ctx(conn: sqlite3.Connection) -> Ctx:
    from .business import system_ctx

    return system_ctx(conn)


def ensure(conn: sqlite3.Connection, task_id: int, why: str = "") -> int | None:
    """The open review item for `task_id` (created, or moved to its current reviewer); None when the
    task is not waiting for an agent's review. The caller commits."""
    from . import tasks

    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None or row["status"] != "review" or row["archived_at"] or is_item(row):
        return None
    rid = tasks.reviewer_of(conn, row)
    items = _open_items(conn, task_id)
    if not _agent_reviewer(conn, rid) or rid == row["assignee_id"]:
        for it in items:
            _close(conn, it["id"], "the review is no longer an agent's")
        return None
    ctx = _ctx(conn)
    keep, extra = (items[0], items[1:]) if items else (None, [])
    for it in extra:
        _close(conn, it["id"], "a duplicate review item")
    r = actors.get(conn, rid)
    if keep is not None:
        if keep["assignee_id"] != rid:
            versioning.update(conn, ctx, tasks.ENTITY, keep["id"], {
                "assignee_type": "agent" if r["kind"] == "agent" else r["kind"], "assignee_id": rid,
                "assignee_name": r["name"], "reviewer_id": rid, "status": "next", "retry_after": None,
                "progress_note": f"Review moved to {r['name']}" + (f": {why}" if why else "")}, action="assign")
            audit.log(conn, ctx, "review_item_moved", tasks.ENTITY, keep["id"], task=task_id, to=rid)
        return keep["id"]
    ref = tasks.display_id(task_id)
    now = now_iso()
    note = (row["progress_note"] or "").strip()
    values = {
        "title": f"Review: {ref} {row['title']}"[:200],
        "notes": (f"Purpose: review the result of {ref} '{row['title']}' handed in for your review"
                  + (f" ({why})" if why else "") + ".\n"
                  f"Source: {ref} waits in review (pos.review_work).\n\n"
                  f"1. Read {ref} (get_task) and its report: /report/{ref}.\n"
                  f"2. Accept it, or return it with what should change: review_task {ref}.\n"
                  "3. Complete this item with one line (accepted / returned and why).\n\n"
                  + (f"Hand-in note: {note[:1500]}" if note else "")).strip(),
        "definition_of_done": f"{ref} is accepted or returned with a reason.",
        "status": "next", "priority": row["priority"] or 2, "topic": "review",
        "visibility": row["visibility"], "owner_id": row["owner_id"], "source": source_of(task_id),
        "created_by": ctx.actor_id, "created_at": now, "updated_at": now,
        "assignee_type": "agent" if r["kind"] == "agent" else r["kind"], "assignee_id": rid,
        "assignee_name": r["name"], "reviewer_id": rid,
    }
    new =versioning.insert(conn, ctx, tasks.ENTITY, values)["id"]
    audit.log(conn, ctx, "review_item", tasks.ENTITY, new, task=task_id, reviewer=rid)
    return new


def _close(conn: sqlite3.Connection, item_id: int, why: str) -> None:
    from . import comments, tasks

    ctx = _ctx(conn)
    versioning.update(conn, ctx, tasks.ENTITY, item_id, {"status": "done", "progress": 100,
                                                         "completed_at": now_iso(),
                                                         "progress_note": f"Closed: {why}"[:500]}, action="close")
    comments.log(conn, ctx, item_id, f"Review item closed: {why}.", "system")


def close_for(conn: sqlite3.Connection, task_id: int, why: str = "the result left review") -> int:
    """Close the open review items of `task_id` (it was accepted, returned or moved on)."""
    items = _open_items(conn, task_id)
    for it in items:
        _close(conn, it["id"], why)
    return len(items)


def stale(conn: sqlite3.Connection, item) -> bool:
    """A review item whose result no longer waits for its assignee (closed when found)."""
    from . import tasks

    tid = reviewed_id(item)
    if tid is None:
        return False
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
    if row is not None and row["status"] == "review" and not row["archived_at"] \
            and tasks.reviewer_of(conn, row) == item["assignee_id"]:
        return False
    _close(conn, item["id"], "the result no longer waits for this review")
    if row is not None and row["status"] == "review":
        ensure(conn, tid, "the reviewer changed")
    return True


def sync(conn: sqlite3.Connection, now: datetime | None = None, apply: bool = True) -> dict:
    """Every result waiting for an agent's review has its open item; stale items are closed. An item
    completed without a review comes back after RECREATE_HOURS. `apply=False`: only report."""
    from . import tasks

    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(hours=RECREATE_HOURS)).isoformat(timespec="seconds")
    created, moved, closed = [], [], []
    for it in conn.execute("SELECT * FROM tasks WHERE source LIKE 'review:%' AND archived_at IS NULL "
                           "AND status != 'done'").fetchall():
        tid = reviewed_id(it)
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone() if tid else None
        if row is None or row["status"] != "review" or row["archived_at"]:
            closed.append(tasks.display_id(it["id"]))
            if apply:
                _close(conn, it["id"], "the result left review")
    for row in conn.execute("SELECT * FROM tasks WHERE status = 'review' AND archived_at IS NULL "
                            "AND COALESCE(source, '') NOT LIKE 'review:%' ORDER BY updated_at").fetchall():
        rid = tasks.reviewer_of(conn, row)
        if not _agent_reviewer(conn, rid) or rid == row["assignee_id"]:
            continue
        items = _open_items(conn, row["id"])
        if items and items[0]["assignee_id"] == rid:
            continue
        if not items and conn.execute("SELECT 1 FROM tasks WHERE source = ? AND completed_at >= ?",
                                      (source_of(row["id"]), since)).fetchone():
            continue  # its item was completed today without a review: it comes back tomorrow
        name = actors.get(conn, rid)["name"]
        (moved if items else created).append(f"{tasks.display_id(row['id'])}→{name}")
        if apply:
            ensure(conn, row["id"], "waiting for your review")
    if apply:
        conn.commit()
    return {k: v for k, v in (("created", created), ("moved", moved), ("closed", closed)) if v}
