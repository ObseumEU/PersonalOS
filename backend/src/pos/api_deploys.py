"""Deploy records from the deployer (pos.selfdeploy), shown on the System page.

The deployer authenticates with the key of the built-in "Deployer" member.
A failed deploy (already reverted by the deployer) becomes a task for the
member who made the change, with the log.
"""

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from . import actors, audit, tasks
from .api_tasks import get_db
from .auth import require_user
from .core import Ctx, now_iso

router = APIRouter(prefix="/api/deploys", tags=["deploys"])
DEPLOYER = "Deployer"


def deployer_ctx(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> Ctx:
    auth = request.headers.get("authorization", "")
    key = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    actor_id = actors.actor_for_key(conn, key) if key else None
    if actor_id is None or actors.get(conn, actor_id)["name"] != DEPLOYER:
        raise HTTPException(401, "deployer key required")
    return Ctx(actor_id, via="deployer")


class DeployIn(BaseModel):
    old_sha: str
    new_sha: str
    status: str
    stage: str = ""
    log: str = ""
    author: str = ""
    reverted_sha: str | None = None
    commits: int = 0
    branch: str = ""  # the branch promoted (agent/dev), or main in follow mode; "" from an older deployer


@router.get("/last")
def last_good(conn=Depends(get_db), ctx: Ctx = Depends(deployer_ctx)):
    row = conn.execute(
        # A failed redeploy after a revert ('error' with reverted_sha) still left the revert on main: the
        # next range starts there, or every tick re-deploys old..revert and stacks another revert commit.
        "SELECT CASE WHEN status = 'ok' THEN new_sha ELSE reverted_sha END AS sha FROM deploys "
        "WHERE status IN ('ok', 'reverted', 'rejected') OR (status = 'error' AND reverted_sha IS NOT NULL) "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return {"sha": row["sha"] if row else None}


def responsible(conn: sqlite3.Connection, author: str) -> int | None:
    """Who fixes a change that did not ship: its author when it is an active agent; an archived
    author's successor (docs/REORG.md: Dev agent -> Software Engineer); anything else (a person,
    an unknown name) the branch's owner, the Software Engineer. Never the owner: the SRE or the
    CTO when there is no engineer; None only when none of them exists."""
    from . import agents, monitor, roles

    row = conn.execute("SELECT * FROM actors WHERE name = ? ORDER BY archived_at IS NOT NULL, id DESC LIMIT 1",
                       (author,)).fetchone() if author else None
    if row is not None and row["kind"] != "human" and row["name"] != DEPLOYER and row["runtime"] != "service":
        aid = agents.successor_of(conn, row["id"]) if row["archived_at"] else row["id"]
        if not actors.get(conn, aid)["is_owner"] and not actors.get(conn, aid)["archived_at"]:
            return aid
    eng = conn.execute("SELECT id FROM actors WHERE (name = ? OR role = 'developer') AND archived_at IS NULL "
                       "AND kind != 'human' ORDER BY name = ? DESC, id LIMIT 1",
                       (roles.ENGINEER, roles.ENGINEER)).fetchone()
    return eng["id"] if eng else monitor.platform_owner_id(conn)


def _open_refusal(conn: sqlite3.Connection, branch: str) -> sqlite3.Row | None:
    """The open task for changes on this branch that did not ship (one per branch, not one per tip)."""
    return conn.execute(
        "SELECT * FROM tasks WHERE source = 'deployer' AND title != ? AND status != 'done' AND archived_at IS NULL "
        "AND (notes LIKE ? OR (? = 'main' AND title LIKE 'Your change %' AND notes NOT LIKE '%Branch: %')) "
        "ORDER BY id DESC LIMIT 1",
        (RESTORE_TITLE, f"%Branch: {branch}\n%", branch)).fetchone()


def _refusal_task(conn: sqlite3.Connection, ctx: Ctx, body: DeployIn) -> str:
    """One open task per branch: a repeat (a new tip, the same conflict) is a comment on it, and
    the task comes back to the queue of whoever fixes it."""
    from . import comments, versioning, wake

    branch = (body.branch or "main").strip()
    what = {"reverted": "was reverted automatically", "rejected": "was refused and reverted",
            "error": "failed and the redeploy needs a person"}[body.status]
    rng = f"{body.old_sha[:8]}..{body.new_sha[:8]}"
    fixer = responsible(conn, body.author)
    open_ = _open_refusal(conn, branch)
    if open_ is not None:
        comments.log(conn, ctx, open_["id"],
                     f"Again: {rng} {what} at stage {body.stage} (author {body.author or '?'}, "
                     f"{body.commits} commit(s)).\n\nLog:\n{body.log[-2500:]}", "system")
        changes: dict = {}
        cur = actors.get(conn, open_["assignee_id"]) if open_["assignee_id"] else None
        if fixer and (cur is None or cur["is_owner"] or cur["archived_at"] or cur["kind"] == "human"):
            changes.update(tasks.resolve_assignee(conn, ctx, {"type": "agent", "id": fixer}))
        if open_["status"] in ("review", "waiting", "inbox", "someday"):
            changes.update(status="next", progress_note=f"The deployer refused {rng} again ({body.stage}).")
        if changes:
            versioning.update(conn, ctx, tasks.ENTITY, open_["id"], changes, action="deploy_again")
        who = changes.get("assignee_id") or open_["assignee_id"]
        if who and not actors.get(conn, who)["is_owner"]:
            wake.wake(who)
        return tasks.display_id(open_["id"])
    t = tasks.create(conn, ctx, {
        "title": f"Your change {rng} {what} ({body.stage})",
        "notes": f"Purpose: find out why your change did not ship and fix it, so main deploys again.\n"
                 f"Source: the self-deploy pipeline ({body.status} at stage {body.stage}).\n"
                 f"Branch: {branch}\n\n"
                 f"The deployer checked {body.commits} commit(s). Stage: {body.stage}. Author: {body.author or '?'}.\n"
                 f"Revert commit: {body.reverted_sha or '-'}\n\nFurther refusals of this branch are comments here "
                 f"(one open task per branch).\n\nLog:\n{body.log[-4000:]}",
        "definition_of_done": "The cause is fixed and the change deploys with all checks green "
                              "(or it is dropped with a note why).",
        "priority": 1 if body.status == "error" else 2, "topic": "platform", "status": "next",
        "source": "deployer",
        "assignee": {"type": "agent", "id": fixer} if fixer else "ai",
    })
    return t["ref"]


@router.post("", status_code=201)
def record(body: DeployIn, conn=Depends(get_db), ctx: Ctx = Depends(deployer_ctx)):
    if body.status not in ("ok", "reverted", "rejected", "error"):
        raise HTTPException(422, "status must be ok, reverted, rejected or error")
    task_ref = None
    own_revert = body.author == DEPLOYER  # the deployer's own revert failed: nobody's change to fix
    if body.status != "ok" and not own_revert:
        task_ref = _refusal_task(conn, ctx, body)
    if body.status == "error":  # the platform may be down: the SRE restores it (one open ticket, not one per tick)
        task_ref = _restore_ticket(conn, ctx, body, task_ref) or task_ref
    cur = conn.execute(
        """INSERT INTO deploys (old_sha, new_sha, status, stage, log, author, reverted_sha, commits, task_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (body.old_sha, body.new_sha, body.status, body.stage, body.log[-20000:], body.author, body.reverted_sha,
         body.commits, tasks.parse_id(task_ref) if task_ref else None, now_iso()),
    )
    audit.log(conn, ctx, f"deploy:{body.status}", "deploy", cur.lastrowid, stage=body.stage, author=body.author,
              range=f"{body.old_sha[:10]}..{body.new_sha[:10]}")
    conn.commit()
    return {"id": cur.lastrowid, "task": task_ref}


RESTORE_TITLE = "Deploy failed and could not roll back by itself"


def _restore_ticket(conn: sqlite3.Connection, ctx: Ctx, body: DeployIn, author_ref: str | None) -> str | None:
    """Circuit breaker: one open restore ticket for the SRE (the CTO without one; the owner only
    when there is neither), later failures are a comment on it. Returns its ref."""
    from . import comments, monitor, wake

    who = monitor.platform_owner_id(conn)
    line = (f"{body.old_sha[:8]}..{body.new_sha[:8]} failed at stage {body.stage}; revert commit "
            f"{(body.reverted_sha or '-')[:10]}." + (f" Details and log: {author_ref}." if author_ref else ""))
    open_ = conn.execute("SELECT id FROM tasks WHERE title = ? AND source = 'deployer' AND status != 'done' "
                         "AND archived_at IS NULL ORDER BY id DESC LIMIT 1", (RESTORE_TITLE,)).fetchone()
    if open_:
        comments.log(conn, ctx, open_["id"], f"Again: {line}", "system")
        return tasks.display_id(open_["id"])
    t = tasks.create(conn, ctx, {
        "title": RESTORE_TITLE, "priority": 1, "status": "next", "topic": "platform", "source": "deployer",
        "notes": "Purpose: the platform may be down or half-deployed; restore a known-good version.\n"
                 f"Source: the self-deploy pipeline. {line}\n\nThe deployer does not revert its own revert "
                 f"again (one attempt per range); it waits for a new commit on main.\n\nLog:\n{body.log[-3000:]}",
        "definition_of_done": "PersonalOS runs a known-good commit and the health check passes.",
        "assignee": {"type": "agent", "id": who} if who else "me"})
    if who:
        conn.execute("UPDATE tasks SET reviewer_id = ? WHERE id = ?", (who, t["id"]))
        wake.wake(who)
    return t["ref"]


class ReviewAsk(BaseModel):
    sha: str
    base: str = ""
    author: str = ""
    subject: str = ""
    commits: int = 0


@router.post("/review")
def review(body: ReviewAsk, conn=Depends(get_db), ctx: Ctx = Depends(deployer_ctx)):
    """The QA review gate (pos.deploy_review): pending, approved or returned for this tip."""
    from . import deploy_review

    try:
        return deploy_review.ask(conn, ctx, body.sha, base=body.base, author=body.author, subject=body.subject,
                                 commits=body.commits)
    except tasks.Invalid as e:
        raise HTTPException(422, str(e)) from e


@router.get("", dependencies=[Depends(require_user)])
def list_deploys(conn=Depends(get_db)):
    rows = conn.execute("SELECT * FROM deploys ORDER BY id DESC LIMIT 50").fetchall()
    return [{**dict(r), "task_ref": tasks.display_id(r["task_id"]) if r["task_id"] else None} for r in rows]
