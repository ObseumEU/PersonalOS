"""REST API for a project's page (pos.project_info): status summary, decisions, files,
activity, people and questions to knowlage. The project itself (list, create, patch,
members) stays in pos.api_tasks."""

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from . import project_info, projects
from .api_tasks import get_ctx, get_db
from .auth import require_user

router = APIRouter(prefix="/api/projects", tags=["projects"], dependencies=[Depends(require_user)])


class DecisionIn(BaseModel):
    text: str
    why: str | None = None
    who: str | None = None
    date: str | None = None
    kind: str = "decision"
    source: str | None = None


class FileLinkIn(BaseModel):
    file_id: int


class AskIn(BaseModel):
    question: str
    effort: int = 2


@router.get("/{ref}/summary")
def project_summary(ref: str, generate: bool = False, force: bool = False, conn=Depends(get_db), ctx=Depends(get_ctx)):
    """The status summary: the cached one at once (generate=false), a new one when the project changed."""
    p = projects.get(conn, ctx, ref)
    return project_info.summary(conn, ctx, p, generate=generate, force=force)


@router.get("/{ref}/decisions")
def list_decisions(ref: str, conn=Depends(get_db), ctx=Depends(get_ctx)):
    p = projects._row(conn, ctx, ref)
    return project_info.log_entries(conn, p["id"])


@router.post("/{ref}/decisions", status_code=201)
def add_decision(ref: str, body: DecisionIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    row = projects._row(conn, ctx, ref)
    projects.may_log(conn, ctx, row)
    out = project_info.add_log(conn, ctx, row["id"], **body.model_dump())
    conn.commit()
    return out


@router.delete("/{ref}/decisions/{entry_id}", status_code=204)
def archive_decision(ref: str, entry_id: int, conn=Depends(get_db), ctx=Depends(get_ctx)):
    row = projects._row(conn, ctx, ref)
    projects.may_log(conn, ctx, row)
    project_info.archive_log(conn, ctx, row["id"], entry_id)
    conn.commit()


@router.get("/{ref}/files")
def project_files(ref: str, conn=Depends(get_db), ctx=Depends(get_ctx)):
    """Files linked in PersonalOS, and what knowlage holds for the project (repo docs, Drive, mail)."""
    row = projects._row(conn, ctx, ref)
    return {"files": project_info.local_files(conn, ctx, row["id"]),
            **project_info.project_documents(conn, row["id"])}


@router.post("/{ref}/files", status_code=201)
def link_file(ref: str, body: FileLinkIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    row = projects._row(conn, ctx, ref)
    projects.may_log(conn, ctx, row)
    project_info.link_file(conn, ctx, row["id"], body.file_id)
    conn.commit()
    return {"ok": True}


@router.delete("/{ref}/files/{file_id}", status_code=204)
def unlink_file(ref: str, file_id: int, conn=Depends(get_db), ctx=Depends(get_ctx)):
    row = projects._row(conn, ctx, ref)
    projects.may_log(conn, ctx, row)
    project_info.unlink_file(conn, ctx, row["id"], file_id)
    conn.commit()


@router.get("/{ref}/activity")
def project_activity(ref: str, days: int = 30, conn=Depends(get_db), ctx=Depends(get_ctx)):
    p = projects.get(conn, ctx, ref)
    return {"days": days, "items": project_info.activity(conn, ctx, p, days)}


@router.post("/{ref}/ask")
async def ask_project(ref: str, body: AskIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    p = projects.get(conn, ctx, ref)
    return await run_in_threadpool(project_info.ask, conn, p, body.question, body.effort)
