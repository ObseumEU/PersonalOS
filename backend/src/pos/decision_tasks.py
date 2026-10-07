"""Approval and decision tasks, and the work that waits on them.

A command approval is a task for the CTO (pos.command_policy.request); the owner's approvals and asks
(pos.approvals, pos.asks) and his "Approve or run" tasks are decisions a task waits on. Two rules:

- **Resolved only by the decision.** An approval task is closed by its approve/deny action
  (command_approval_decide), straight to done: no hand-in, no review, no evidence lines. An agent's
  complete_task or review verdict on it is refused while the approval is pending (with the call that
  decides it) and is a no-op once it is decided (prod 2026-10-07: the COO returned T-916, T-1000, T-1014
  and T-1016 for a missing "Ověřeno" line, and the CTO's complete_task was refused 4x, "nobody approves
  their own work").
- **The waiting task wakes at once.** When the last open decision a task waits on (the tasks made on its
  behalf, on_behalf_of) is resolved, a `waiting` task goes back to `next` with no back-off and its agent
  is woken (prod 2026-10-07: T-397's approvals T-1014..T-1016 were decided 18:03-18:05 and T-397 stayed
  parked until its follow-up date).
"""

import sqlite3

from . import audit
from .core import Ctx, now_iso


def command_approval(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row | None:
    """The command approval this task decides, or None."""
    try:
        return conn.execute("SELECT * FROM command_approvals WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                            (task_id,)).fetchone()
    except sqlite3.OperationalError:  # no approvals yet
        return None


def is_decision(conn: sqlite3.Connection, task_id: int) -> bool:
    return command_approval(conn, task_id) is not None


def how_to_resolve(appr: sqlite3.Row) -> str:
    return (f"Decide with command_approval_decide(approval={appr['id']}, approve=true|false, reason=...): that "
            "closes this task by itself. No complete_task, no review, no evidence lines.")


def close(conn: sqlite3.Connection, ctx: Ctx, task_id: int, note: str, origin: int | None = None) -> None:
    """The decision is made: the task is done now (no review), and the task it was made for (`origin`, else the
    task's on_behalf_of) may wake."""
    from . import review_work, tasks, versioning

    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return
    if row["status"] != "done":
        versioning.update(conn, ctx, tasks.ENTITY, task_id,
                          {"status": "done", "progress": 100, "progress_note": note[:1000] or "Decided",
                           "completed_at": now_iso()}, action="decided")
        review_work.close_for(conn, task_id, "the approval is decided")
    resolved(conn, ctx, origin or (row["on_behalf_of"] if "on_behalf_of" in row.keys() else None), why=note)


def guard_finish(conn: sqlite3.Connection, ctx: Ctx, row, human: bool) -> dict | None:
    """An attempt to finish (complete_task, update to review/done, a review verdict) on a decision task.
    Returns the task's state when there is nothing to do (decided already: closed now if it was not);
    raises Invalid while the approval is pending. None when this is not a decision task or a person
    finishes it (the owner may close one by hand)."""
    from . import tasks

    appr = command_approval(conn, row["id"])
    if appr is None:
        return None
    if appr["status"] != "pending":
        if row["status"] != "done":
            close(conn, ctx, row["id"], f"{appr['status'].capitalize()}: {appr['decision'] or ''}".strip())
        return tasks.get(conn, ctx, row["id"])
    if human:
        return None
    raise tasks.Invalid(f"{tasks.display_id(row['id'])} is a command approval: it is not completed or reviewed. "
                        + how_to_resolve(appr))


def resume_waiting(conn: sqlite3.Connection, ctx: Ctx, task_id: int | None, why: str) -> bool:
    """A `waiting` task goes back to `next` at once (no back-off) and its agent is woken."""
    from . import tasks, versioning, wake

    if not task_id:
        return False
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None or row["status"] != "waiting" or row["archived_at"]:
        return False
    versioning.update(conn, ctx, tasks.ENTITY, task_id, {"status": "next", "progress_note": why[:500]},
                      action="resume")
    conn.execute("UPDATE tasks SET retry_after = NULL WHERE id = ?", (task_id,))
    audit.log(conn, ctx, "decision_resume", "task", task_id, why=why[:200])
    if row["assignee_type"] in ("ai", "agent"):
        wake.wake(row["assignee_id"])
    return True


def _open_decisions(conn: sqlite3.Connection, task_id: int) -> int:
    """The decisions made on this task's behalf that are still open."""
    try:
        return conn.execute("""SELECT COUNT(*) FROM tasks WHERE on_behalf_of = ? AND id != ? AND archived_at IS NULL
                               AND status NOT IN ('done', 'someday')""", (task_id, task_id)).fetchone()[0]
    except sqlite3.OperationalError:  # no on_behalf_of column yet
        return 0


def resolved(conn: sqlite3.Connection, ctx: Ctx, origin: int | None, why: str = "") -> bool:
    """A decision made for `origin` is resolved: it wakes when nothing else made for it is still open."""
    if not origin or _open_decisions(conn, origin):
        return False
    from .tasks import display_id

    return resume_waiting(conn, ctx, origin, f"Rozhodnutí, na které úkol čekal, padlo: {why}".strip()
                          if why else f"Rozhodnutí pro {display_id(origin)} padlo.")


def on_done(conn: sqlite3.Connection, ctx: Ctx, row) -> None:
    """tasks.update: a task made on another's behalf (an approval, an owner's "Approve or run", a check) is done."""
    origin = row["on_behalf_of"] if "on_behalf_of" in row.keys() else None
    if origin and origin != row["id"]:
        resolved(conn, ctx, origin, why=f"{_ref(row['id'])} je hotový")


def _ref(task_id: int) -> str:
    from .tasks import display_id

    return display_id(task_id)
