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


@router.post("", status_code=201)
def record(body: DeployIn, conn=Depends(get_db), ctx: Ctx = Depends(deployer_ctx)):
    if body.status not in ("ok", "reverted", "rejected", "error"):
        raise HTTPException(422, "status must be ok, reverted, rejected or error")
    task_ref = None
    own_revert = body.author == DEPLOYER  # the deployer's own revert failed: nobody's change to fix
    if body.status != "ok" and not own_revert:
        member = actors.find_by_name(conn, body.author) if body.author else None
        assignee = {"type": "agent" if member and member["kind"] != "human" else "human",
                    "id": member["id"] if member else actors.owner_id(conn)}
        what = {"reverted": "was reverted automatically", "rejected": "was refused and reverted",
                "error": "failed and the redeploy needs a person"}[body.status]
        t = tasks.create(conn, ctx, {
            "title": f"Your change {body.old_sha[:8]}..{body.new_sha[:8]} {what} ({body.stage})",
            "notes": f"Purpose: find out why your change did not ship and fix it, so main deploys again.\n"
                     f"Source: the self-deploy pipeline ({body.status} at stage {body.stage}).\n\n"
                     f"The deployer checked {body.commits} commit(s). Stage: {body.stage}.\n"
                     f"Revert commit: {body.reverted_sha or '-'}\n\nLog:\n{body.log[-4000:]}",
            "definition_of_done": "The cause is fixed and the change deploys with all checks green "
                                  "(or it is dropped with a note why).",
            "priority": 1 if body.status == "error" else 2, "topic": "platform", "status": "next",
            "assignee": assignee,
        })
        task_ref = t["ref"]
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
