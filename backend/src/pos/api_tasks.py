"""REST API for people (the web UI): tasks, history, audit, runs."""

import sqlite3
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import actors, audit, runner, suggest, tasks, versioning
from .auth import require_user
from .config import Settings, get_settings
from .core import Ctx, Forbidden, NotFound
from .db import connect

router = APIRouter(prefix="/api", tags=["tasks"], dependencies=[Depends(require_user)])


def get_db(settings: Settings = Depends(get_settings)) -> Iterator[sqlite3.Connection]:
    conn = connect(settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def get_ctx(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> Ctx:
    """The signed-in person (pos.accounts); the owner's emergency login and a
    setup without a password act as the owner."""
    from . import accounts
    from .auth import session_actor

    aid = session_actor(request)
    if aid is None:
        return Ctx(actors.owner_id(conn), via="api")
    if not accounts.active_actor(conn, aid):
        from fastapi import HTTPException

        raise HTTPException(401, "this account is disabled")
    return Ctx(aid, via="api")


class TaskIn(BaseModel):
    title: str
    notes: str | None = None
    status: str | None = None
    priority: int | None = None
    do_date: str | None = None
    deadline: str | None = None
    estimate_min: int | None = None
    energy: str | None = None
    topic: str | None = None
    definition_of_done: str | None = None
    visibility: str | None = None
    follow_up: str | None = None
    assignee: Any = None
    reviewer: Any = None  # who reviews the result (default: who asked, the lead, the owner)
    project: Any = None  # project id or slug


class CaptureIn(BaseModel):
    text: str


class NoteIn(BaseModel):
    note: str | None = None


class CommentIn(BaseModel):
    body: str


class ReviewIn(BaseModel):
    accept: bool
    comment: str | None = None


class AssignIn(BaseModel):
    assignee: Any


class ClarifyIn(BaseModel):
    action: str
    fields: dict | None = None


class RestoreIn(BaseModel):
    version: int


def _fields(body: BaseModel) -> dict:
    return {k: v for k, v in body.model_dump(exclude_unset=True).items()}


@router.get("/tasks")
def list_tasks(view: str = "today", topic: str | None = None, assignee_id: int | None = None, scope: str = "all",
               conn=Depends(get_db), ctx=Depends(get_ctx)):
    """`scope`: mine (assigned to me), team (me and everyone below me) or all."""
    return tasks.list_tasks(conn, ctx, view, topic=topic, assignee_id=assignee_id, scope=scope)


@router.get("/weekly-review")
def weekly_review(conn=Depends(get_db), ctx=Depends(get_ctx)):
    """The GTD weekly review for the signed-in member: inbox to zero, waiting for,
    someday, and projects without a next step."""
    from . import projects

    no_next = [p for p in projects.list_projects(conn, ctx, "active")
               if not conn.execute("""SELECT 1 FROM tasks WHERE project_id = ? AND archived_at IS NULL
                                      AND status IN ('next', 'working')""", (p["id"],)).fetchone()]
    return {
        "inbox": tasks.list_tasks(conn, ctx, "inbox", scope="mine"),
        "waiting": tasks.list_tasks(conn, ctx, "waiting", scope="mine"),
        "someday": tasks.list_tasks(conn, ctx, "someday", scope="mine"),
        "projects_without_next": [{k: p[k] for k in ("id", "slug", "name", "lead_name", "counts")} for p in no_next],
        "to_review": tasks.list_tasks(conn, ctx, "to_review"),
    }


@router.get("/tasks/counts")
def counts(conn=Depends(get_db), ctx=Depends(get_ctx)):
    return tasks.counts(conn, ctx)


@router.get("/tasks/topics")
def topics(conn=Depends(get_db), ctx=Depends(get_ctx)):
    return tasks.topics(conn, ctx)


@router.post("/tasks", status_code=201)
def create(body: TaskIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    t = tasks.create(conn, ctx, _fields(body))
    conn.commit()
    return t


@router.post("/tasks/capture", status_code=201)
def capture(body: CaptureIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    t = tasks.capture(conn, ctx, body.text, source="ui")
    conn.commit()
    return t


@router.get("/tasks/{task_id}")
def get(task_id: str, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return tasks.get(conn, ctx, tasks.parse_id(task_id))


@router.patch("/tasks/{task_id}")
def patch(task_id: str, body: dict, conn=Depends(get_db), ctx=Depends(get_ctx)):
    t = tasks.update(conn, ctx, tasks.parse_id(task_id), body)
    conn.commit()
    return t


@router.post("/tasks/{task_id}/steps", status_code=201)
def add_step(task_id: str, body: TaskIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    t = tasks.create(conn, ctx, {**_fields(body), "parent_id": tasks.parse_id(task_id)})
    conn.commit()
    return t


@router.post("/tasks/{task_id}/complete")
def complete(task_id: str, body: NoteIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    t = tasks.complete(conn, ctx, tasks.parse_id(task_id), body.note)
    conn.commit()
    return t


@router.post("/tasks/{task_id}/review")
def review(task_id: str, body: ReviewIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    t = tasks.review(conn, ctx, tasks.parse_id(task_id), body.accept, body.comment)
    conn.commit()
    return t


class RequestReviewIn(BaseModel):
    reviewer: Any
    note: str | None = None


@router.post("/tasks/{task_id}/request-review")
def request_review(task_id: str, body: RequestReviewIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    t = tasks.request_review(conn, ctx, tasks.parse_id(task_id), body.reviewer, body.note or "")
    conn.commit()
    return t


@router.post("/tasks/{task_id}/intervene")
def intervene(task_id: str, body: NoteIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    t = tasks.intervene(conn, ctx, tasks.parse_id(task_id), body.note or "")
    conn.commit()
    return t


@router.post("/tasks/{task_id}/assign")
def assign(task_id: str, body: AssignIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    t = tasks.assign(conn, ctx, tasks.parse_id(task_id), body.assignee)
    conn.commit()
    return t


class ReassignIn(BaseModel):
    to: Any
    note: str | None = None
    force: bool = False


@router.get("/tasks/{task_id}/reassign/options")
def reassign_options(task_id: str, conn=Depends(get_db), ctx=Depends(get_ctx)):
    """Who the task could go to: role, engine/model, and why someone cannot take it now."""
    from . import reassign

    return reassign.candidates(conn, ctx, tasks.parse_id(task_id))


@router.post("/tasks/{task_id}/reassign")
def reassign_task(task_id: str, body: ReassignIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    """Hand the task to another member: releases the old claim, cancels its run,
    records it, DMs the new assignee and wakes its worker (pos.reassign)."""
    from . import reassign

    try:
        return reassign.reassign(conn, ctx, tasks.parse_id(task_id), body.to, body.note or "", force=body.force)
    except reassign.Refused as e:
        return JSONResponse(status_code=409, content={"detail": str(e), "to": e.target, "reasons": e.reasons})


@router.get("/tasks/{task_id}/live")
def live(task_id: str, conn=Depends(get_db), ctx=Depends(get_ctx)):
    """Live state for the task detail: current run, engine/model, progress notes, blockers."""
    from . import reassign

    return reassign.live(conn, ctx, tasks.parse_id(task_id))


@router.post("/tasks/{task_id}/archive")
def archive(task_id: str, conn=Depends(get_db), ctx=Depends(get_ctx)):
    t = tasks.archive(conn, ctx, tasks.parse_id(task_id))
    conn.commit()
    return t


@router.post("/tasks/{task_id}/unarchive")
def unarchive(task_id: str, conn=Depends(get_db), ctx=Depends(get_ctx)):
    t = tasks.unarchive(conn, ctx, tasks.parse_id(task_id))
    conn.commit()
    return t


@router.post("/tasks/{task_id}/suggest")
def make_suggestion(task_id: str, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return suggest.suggest(conn, ctx, tasks.parse_id(task_id))


@router.post("/tasks/{task_id}/clarify")
def clarify(task_id: str, body: ClarifyIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    t = tasks.clarify(conn, ctx, tasks.parse_id(task_id), body.action, body.fields)
    conn.commit()
    return t


@router.get("/tasks/{task_id}/history")
def history(task_id: str, conn=Depends(get_db), ctx=Depends(get_ctx)):
    tid = tasks.parse_id(task_id)
    tasks.get(conn, ctx, tid)  # visibility check
    return versioning.history(conn, "task", tid)


class ProjectIn(BaseModel):
    name: str
    goal: str = ""
    definition_of_done: str = ""
    lead: Any = None
    members: list[Any] = []
    visibility: str = "team"
    labels: list[str] = []
    due: str | None = None


class MemberIn(BaseModel):
    member: Any
    role: str = "member"


@router.get("/projects")
def list_projects(status: str | None = None, conn=Depends(get_db), ctx=Depends(get_ctx)):
    from . import projects

    return projects.list_projects(conn, ctx, status)


@router.post("/projects", status_code=201)
def create_project(body: ProjectIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    from . import projects

    d = body.model_dump()
    p = projects.create(conn, ctx, member_refs=d.pop("members"), **d)
    conn.commit()
    return p


@router.get("/projects/{ref}")
def get_project(ref: str, conn=Depends(get_db), ctx=Depends(get_ctx)):
    from . import projects

    return projects.get(conn, ctx, ref)


@router.patch("/projects/{ref}")
def update_project(ref: str, body: dict, conn=Depends(get_db), ctx=Depends(get_ctx)):
    from . import projects

    p = projects.update(conn, ctx, ref, body)
    conn.commit()
    return p


@router.post("/projects/{ref}/members")
def add_project_member(ref: str, body: MemberIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    from . import projects

    p = projects.add_member(conn, ctx, ref, body.member, body.role)
    conn.commit()
    return p


@router.get("/tasks/{task_id}/comments")
def task_comments(task_id: str, limit: int = 100, conn=Depends(get_db), ctx=Depends(get_ctx)):
    from . import comments

    return comments.list_for(conn, ctx, tasks.parse_id(task_id), limit=max(1, min(limit, 500)))


@router.post("/tasks/{task_id}/comments", status_code=201)
def add_task_comment(task_id: str, body: CommentIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    from . import comments

    c = comments.add(conn, ctx, tasks.parse_id(task_id), body.body)
    conn.commit()
    return c


@router.post("/tasks/{task_id}/restore")
def restore(task_id: str, body: RestoreIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    tid = tasks.parse_id(task_id)
    tasks.get(conn, ctx, tid)
    versioning.restore(conn, ctx, "task", tid, body.version)
    conn.commit()
    return tasks.get(conn, ctx, tid)


@router.get("/actors")
def list_actors(conn=Depends(get_db), ctx=Depends(get_ctx)):
    """Members; where an agent's instructions file lives is the owner's business."""
    owner = actors.get(conn, ctx.actor_id)["is_owner"]
    return [a if owner else {k: v for k, v in a.items() if k != "instructions_path"}
            for a in actors.list_actors(conn)]


@router.get("/audit")
def audit_log(entity: str | None = None, entity_id: int | None = None, run_id: int | None = None,
              limit: int = 100, conn=Depends(get_db), ctx=Depends(get_ctx)):
    rows = audit.entries(conn, entity=entity, entity_id=entity_id, run_id=run_id, limit=min(limit, 500))
    return audit.readable(conn, rows, ctx.actor_id)


@router.get("/runs")
def runs(conn=Depends(get_db), ctx=Depends(get_ctx)):
    return runner.list_runs(conn, actor_id=ctx.actor_id)


@router.post("/runs/{run_id}/rollback")
def rollback(run_id: int, conn=Depends(get_db), ctx=Depends(get_ctx)):
    undone = versioning.rollback_run(conn, ctx, run_id)
    conn.commit()
    return {"undone": [{"entity": e, "id": i} for e, i in undone]}


def install_error_handlers(app: FastAPI) -> None:
    for exc, code in ((NotFound, 404), (Forbidden, 403), (tasks.Invalid, 422)):
        def handler(_: Request, e: Exception, code: int = code) -> JSONResponse:
            return JSONResponse(status_code=code, content={"detail": str(e)})

        app.add_exception_handler(exc, handler)


@router.get("/calendar")
def calendar(start: str | None = None, days: int = 7, conn=Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    """Calendar events (iCal feeds) and dated tasks from `start` (YYYY-MM-DD, default today)."""
    from datetime import date

    from . import agenda
    from .core import today

    try:
        first = date.fromisoformat(start) if start else today()
    except ValueError as e:
        raise tasks.Invalid("start must be YYYY-MM-DD") from e
    return agenda.agenda(conn, ctx, first, max(1, min(days, 42)))
