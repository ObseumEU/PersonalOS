"""REST API for the owner's report on a task (pos.owner_report) and reference previews (pos.refs)."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from . import owner_report, refs, tasks
from .api_tasks import get_ctx, get_db
from .auth import require_user

router = APIRouter(prefix="/api", tags=["reports"], dependencies=[Depends(require_user)])


@router.get("/tasks/{task_id}/report")
def report(task_id: str, generate: bool = True, conn=Depends(get_db), ctx=Depends(get_ctx)):
    """The report for the owner: takeaway, decisions, next step and the details (built from a
    free-form result when the agent gave none; cached per task version)."""
    return owner_report.current(conn, ctx, tasks.parse_id(task_id), generate=generate)


@router.post("/tasks/{task_id}/report/rebuild")
def rebuild(task_id: str, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return owner_report.current(conn, ctx, tasks.parse_id(task_id), rebuild=True)


class DecideIn(BaseModel):
    decision: str
    choice: str = ""
    note: str = ""


@router.post("/tasks/{task_id}/report/decisions")
def decide(task_id: str, body: DecideIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    """The owner's answer to one decision; the last answer resumes the agent's work."""
    return owner_report.decide(conn, ctx, tasks.parse_id(task_id), body.decision, body.choice, body.note)


@router.get("/refs/{ref:path}")
def ref_preview(ref: str, conn=Depends(get_db), ctx=Depends(get_ctx)):
    """What a reference in text points at (note, msg, chunk, task, file), for the inline preview."""
    kind, rid = refs.parse(ref)
    return refs.resolve(conn, ctx, kind, rid)
