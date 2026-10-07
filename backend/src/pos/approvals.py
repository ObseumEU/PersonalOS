"""The approval queue: what waits for the owner (constitution Ú1 as amended 2026-09-27:
money, commitments and posts on his personal channels; ordinary outbound goes out
directly through pos.outbound).

Agents call `request` (MCP `request_approval`); the owner decides in the UI
(step 2 adds the screens). The approval id is what pos.guard.policy
.check_outbound expects before an outbound action runs.
"""

import json
import sqlite3

from . import actors, audit
from .core import Ctx, Forbidden, NotFound, now_iso

_on_approved = []


def on_approved(fn) -> None:
    """Register fn(conn, approval) to run when the owner approves something
    (e.g. pos.outbound sends the approved e-mail)."""
    _on_approved.append(fn)


def request(conn: sqlite3.Connection, ctx: Ctx, action: str, details: dict | None = None,
            task_id: int | None = None, ping: bool = True) -> dict:
    cur = conn.execute(
        """INSERT INTO approvals (task_id, requested_by, run_id, action, details, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (task_id, ctx.actor_id, ctx.run_id, action, json.dumps(details or {}, ensure_ascii=False), now_iso()),
    )
    audit.log(conn, ctx, "request_approval", "approval", cur.lastrowid, requested=action, task_id=task_id)
    out = get(conn, cur.lastrowid)
    if ping:
        from . import asks

        asks.ping_approval(conn, ctx, out)  # the owner hears of it in #team, like any ask
    return out


def get(conn: sqlite3.Connection, approval_id: int) -> dict:
    row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    if row is None:
        raise NotFound(f"approval {approval_id}")
    out = {**dict(row), "details": json.loads(row["details"])}
    if out.get("result"):
        out["result"] = json.loads(out["result"])
    return out


def pending(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT id FROM approvals WHERE status = 'pending' ORDER BY id").fetchall()
    return [get(conn, r["id"]) for r in rows]


def decide(conn: sqlite3.Connection, ctx: Ctx, approval_id: int, approve: bool, comment: str | None = None) -> dict:
    if not actors.get(conn, ctx.actor_id)["is_owner"]:
        raise Forbidden("only the owner decides approvals")
    row = get(conn, approval_id)
    if row["status"] != "pending":
        raise Forbidden(f"approval {approval_id} is already {row['status']}")
    conn.execute(
        "UPDATE approvals SET status = ?, decided_by = ?, decided_at = ?, comment = ? WHERE id = ?",
        ("approved" if approve else "rejected", ctx.actor_id, now_iso(), comment, approval_id),
    )
    audit.log(conn, ctx, "approve" if approve else "reject", "approval", approval_id)
    if row["task_id"]:
        from . import business

        business.record_intervention(conn, ctx, row["task_id"], "approval", row["action"])
    if approve:
        for fn in _on_approved:
            fn(conn, get(conn, approval_id))
    out = get(conn, approval_id)
    from . import asks, decision_tasks

    asks.tell_decision(conn, ctx, out)  # the requester gets the decision in its inbox
    if row["task_id"]:  # the task that waited for the decision goes on at once (pos.decision_tasks)
        word = "schválil" if approve else "zamítl"
        decision_tasks.resume_waiting(conn, ctx, row["task_id"],
                                      f"Majitel {word} žádost #{approval_id} ({row['action']})"
                                      + (f": {comment}" if comment else ""))
    return out
