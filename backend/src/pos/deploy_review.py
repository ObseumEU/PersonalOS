"""The QA review gate before the deployer promotes agent/dev (docs/REORG.md).

With DEPLOY_REQUIRE_REVIEW=1 the deployer (pos.selfdeploy) asks PersonalOS
before it merges a new tip of agent/dev: POST /api/deploys/review. The first
ask files a review task for the QA Reviewer and answers "pending"; the
deployer tries again on its next tick. The QA Reviewer decides with the MCP
tool deploy_review(sha, verdict, note): "approve" lets the deployer go on
with its checks, "return" makes it refuse the range, and the refusal becomes
the usual task for the author with the note. Without a QA Reviewer (archived,
never created) the gate answers "approved" with a note, so deploys never
hang on a missing member.
"""

import sqlite3

from . import actors, audit, roles, tasks
from .core import Ctx, Forbidden, now_iso
from .tasks import Invalid

_SCHEMA = """
CREATE TABLE IF NOT EXISTS deploy_reviews (
    sha TEXT PRIMARY KEY,
    base TEXT,
    status TEXT NOT NULL,          -- pending | approved | returned
    note TEXT,
    author TEXT,
    task_id INTEGER,
    decided_by INTEGER,
    created_at TEXT NOT NULL,
    decided_at TEXT
)"""
VERDICTS = {"approve": "approved", "return": "returned"}


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_SCHEMA)


def reviewer(conn: sqlite3.Connection) -> sqlite3.Row | None:
    row = actors.find_by_name(conn, roles.QA)
    return row if row is not None and not row["paused_at"] else None


def ask(conn: sqlite3.Connection, ctx: Ctx, sha: str, *, base: str = "", author: str = "", subject: str = "",
        commits: int = 0) -> dict:
    """The deployer asks about the tip `sha`: {"status": pending | approved | returned, "note", "task"}."""
    ensure_schema(conn)
    sha = (sha or "").strip()
    if len(sha) < 7:
        raise Invalid("sha is a commit id")
    row = conn.execute("SELECT * FROM deploy_reviews WHERE sha = ?", (sha,)).fetchone()
    if row is not None:
        return {"status": row["status"], "note": row["note"],
                "task": tasks.display_id(row["task_id"]) if row["task_id"] else None}
    qa = reviewer(conn)
    if qa is None:
        return {"status": "approved", "note": f"no active {roles.QA}: the review gate is open", "task": None}
    rng = f"{base[:10]}..{sha[:10]}" if base else sha[:10]
    t = tasks.create(conn, ctx, {
        "title": f"Review agent/dev {sha[:8]}: {subject[:80] or 'nová změna'}",
        "notes": ("Purpose: the deployer waits for your review before it promotes this range of agent/dev into "
                  f"main.\nSource: the deploy review gate (DEPLOY_REQUIRE_REVIEW), range `{rng}`, {commits} "
                  f"commit(s), author {author or '?'}.\n\n"
                  f"1. `git log --oneline {base[:10] + '..' if base else ''}{sha[:10]}` and `git diff --stat` in your "
                  "working directory (run `git fetch` first if the commit is missing).\n"
                  "2. Review as your instructions say.\n"
                  f"3. `deploy_review(\"{sha}\", \"approve\" | \"return\", note)`; it finishes this task."),
        "definition_of_done": "deploy_review recorded a verdict for this sha.",
        "assignee": {"type": "agent", "id": qa["id"]}, "status": "next", "priority": 2, "topic": "review",
        "source": "deployer",
    })
    conn.execute("UPDATE tasks SET reviewer_id = ? WHERE id = ?", (qa["id"], t["id"]))  # its own verdict closes it
    conn.execute("INSERT INTO deploy_reviews (sha, base, status, author, task_id, created_at) "
                 "VALUES (?, ?, 'pending', ?, ?, ?)", (sha, base, author, t["id"], now_iso()))
    audit.log(conn, ctx, "deploy_review_asked", "task", t["id"], sha=sha[:12], author=author)
    conn.commit()
    return {"status": "pending", "note": None, "task": t["ref"]}


def decide(conn: sqlite3.Connection, ctx: Ctx, sha: str, verdict: str, note: str = "") -> dict:
    """The QA Reviewer (or its lead, or a person) approves or returns a pending review."""
    from .org import manages

    ensure_schema(conn)
    if verdict not in VERDICTS:
        raise Invalid("verdict is approve or return")
    row = conn.execute("SELECT * FROM deploy_reviews WHERE sha = ? OR sha LIKE ? ORDER BY created_at DESC",
                       ((sha or "").strip(), f"{(sha or '').strip()}%")).fetchone()
    if row is None or len((sha or "").strip()) < 7:
        raise Invalid(f"no review is waiting for {sha}")
    me = actors.get(conn, ctx.actor_id)
    qa = reviewer(conn)
    if not (me["kind"] == "human" or (qa and me["id"] == qa["id"]) or (qa and manages(conn, me["id"], qa["id"]))):
        raise Forbidden(f"only the {roles.QA}, its lead or a person decides a deploy review")
    if verdict == "return" and not (note or "").strip():
        raise Invalid("a returned review says what to change")
    status = VERDICTS[verdict]
    conn.execute("UPDATE deploy_reviews SET status = ?, note = ?, decided_by = ?, decided_at = ? WHERE sha = ?",
                 (status, (note or "").strip()[:4000], me["id"], now_iso(), row["sha"]))
    out = {"sha": row["sha"], "status": status}
    if row["task_id"]:
        t = tasks.get(conn, ctx, row["task_id"])
        if t["status"] != "done":
            t = tasks.complete(conn, ctx, row["task_id"],
                               f"**{'Schváleno' if status == 'approved' else 'Vráceno'}**\n\n{note or ''}"[:4000])
        out["task"] = t["ref"]
    audit.log(conn, ctx, "deploy_review", "task", row["task_id"], sha=row["sha"][:12], verdict=status)
    conn.commit()
    return out


def register_mcp(mcp, session) -> None:
    from mcp.server.mcpserver import Context

    @mcp.tool(description="QA Reviewer: your verdict on a range of agent/dev the deployer waits for. verdict "
                          "'approve' (the deployer goes on with its checks) or 'return' (it refuses the range and the "
                          "author gets a task with your note: a numbered list of what to change). Finishes the "
                          "review task.")
    def deploy_review(ctx: Context, sha: str, verdict: str, note: str = "") -> dict:
        with session(ctx, "deploy_review", sha=sha, verdict=verdict) as (conn, c):
            return decide(conn, c, sha, verdict, note)
