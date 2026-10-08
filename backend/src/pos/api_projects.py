"""REST API for a project's page (pos.project_info): status summary, decisions, files,
activity, people and questions to knowlage. The project itself (list, create, patch,
members) stays in pos.api_tasks."""

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from . import project_info, projects
from .api_tasks import get_ctx, get_db
from .auth import require_user
from .core import NotFound

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


# ------------------------------------------------------------------ "Co je živé" (pos.reality, pos.grounding)

class VerifyIn(BaseModel):
    accept: bool = True
    note: str = ""


class OverrideIn(BaseModel):
    reason: str = ""


@router.get("/{ref}/reality")
def project_reality(ref: str, conn=Depends(get_db), ctx=Depends(get_ctx)):
    """What is usable now, and the content the claim gate blocked for this project (the owner may let it through)."""
    from . import actors, grounding, reality

    row = projects._row(conn, ctx, ref)
    is_owner = bool(actors.get(conn, ctx.actor_id)["is_owner"])
    return {"capabilities": reality.registry(conn, [row["id"]]), "expiry_hours": reality.EXPIRY_HOURS,
            "blocked": grounding.recent(conn, [row["id"]], "block", 10) if is_owner else [],
            "can_override": is_owner, "holds": reality.holds(conn, [row["id"]]),
            "can_release": reality.may_release(conn, ctx, row["id"])}


@router.post("/{ref}/reality/probe")
async def project_reality_probe(ref: str, conn=Depends(get_db), ctx=Depends(get_ctx)):
    """Run the project's probes now (an outsider's view of every URL)."""
    from . import reality

    row = projects._row(conn, ctx, ref)

    def go():
        out = reality.probe_project(conn, row["id"])
        reality.release_holds(conn, row["id"])
        conn.commit()
        return {"results": out, "capabilities": reality.registry(conn, [row["id"]])}

    return await run_in_threadpool(go)


@router.post("/{ref}/reality/{key}/verify")
def project_reality_verify(ref: str, key: str, body: VerifyIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    """A person accepts (or rejects) the evidence that a capability works; its URL must pass the probe."""
    from . import reality

    row = projects._row(conn, ctx, ref)
    out = reality.verify(conn, ctx, row["id"], key, body.accept, body.note)
    conn.commit()
    return out


class ReleaseIn(BaseModel):
    reason: str = ""


@router.post("/{ref}/reality/holds/{task_id}/release")
def project_reality_release(ref: str, task_id: int, body: ReleaseIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    """One click "uvolnit": the owner or the project lead releases a held task (back to the queue, audited)."""
    from . import reality

    row = projects._row(conn, ctx, ref)
    h = reality.held(conn, task_id)
    if h is None or h["project_id"] != row["id"]:
        raise NotFound("this task is not held in this project")
    out = reality.release_by_hand(conn, ctx, task_id, body.reason)
    conn.commit()
    return out


reality_router = APIRouter(prefix="/api/reality", tags=["projects"], dependencies=[Depends(require_user)])


@reality_router.post("/checks/{check_id}/override")
def reality_override(check_id: int, body: OverrideIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    """The owner's escape hatch: let one blocked content through (the agent sends it again unchanged)."""
    from . import grounding

    out = grounding.override(conn, ctx, check_id, body.reason)
    conn.commit()
    return out
