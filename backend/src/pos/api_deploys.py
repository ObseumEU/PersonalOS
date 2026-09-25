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
        "SELECT CASE WHEN status = 'ok' THEN new_sha ELSE reverted_sha END AS sha FROM deploys "
        "WHERE status IN ('ok', 'reverted', 'rejected') ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return {"sha": row["sha"] if row else None}


@router.post("", status_code=201)
def record(body: DeployIn, conn=Depends(get_db), ctx: Ctx = Depends(deployer_ctx)):
    if body.status not in ("ok", "reverted", "rejected", "error"):
        raise HTTPException(422, "status must be ok, reverted, rejected or error")
    task_ref = None
    if body.status != "ok":
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
        if body.status == "error":  # the platform may be down: the owner must know
            tasks.create(conn, ctx, {"title": "Deploy failed and could not roll back by itself", "priority": 1,
                                     "notes": f"Purpose: the platform may be down or half-deployed; a person "
                                              f"has to restore it.\nSource: the self-deploy pipeline, stage "
                                              f"{body.stage}. Details and log: {t['ref']}.",
                                     "definition_of_done": "PersonalOS runs a known-good commit and the health "
                                                           "check passes.",
                                     "assignee": "me", "status": "next", "topic": "platform"})
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


@router.get("", dependencies=[Depends(require_user)])
def list_deploys(conn=Depends(get_db)):
    rows = conn.execute("SELECT * FROM deploys ORDER BY id DESC LIMIT 50").fetchall()
    return [{**dict(r), "task_ref": tasks.display_id(r["task_id"]) if r["task_id"] else None} for r in rows]
