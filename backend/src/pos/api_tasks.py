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


def get_ctx(conn: sqlite3.Connection = Depends(get_db)) -> Ctx:
    # The web UI is single-user for now: a logged-in session acts as the owner.
    return Ctx(actors.owner_id(conn), via="api")


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


class CaptureIn(BaseModel):
    text: str


class NoteIn(BaseModel):
    note: str | None = None


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
def list_tasks(view: str = "today", topic: str | None = None, assignee_id: int | None = None,
               conn=Depends(get_db), ctx=Depends(get_ctx)):
    return tasks.list_tasks(conn, ctx, view, topic=topic, assignee_id=assignee_id)


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


@router.post("/tasks/{task_id}/restore")
def restore(task_id: str, body: RestoreIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    tid = tasks.parse_id(task_id)
    tasks.get(conn, ctx, tid)
    versioning.restore(conn, ctx, "task", tid, body.version)
    conn.commit()
    return tasks.get(conn, ctx, tid)


@router.get("/actors")
def list_actors(conn=Depends(get_db)):
    return actors.list_actors(conn)


@router.get("/audit")
def audit_log(entity: str | None = None, entity_id: int | None = None, run_id: int | None = None,
              limit: int = 100, conn=Depends(get_db)):
    return audit.entries(conn, entity=entity, entity_id=entity_id, run_id=run_id, limit=min(limit, 500))


@router.get("/runs")
def runs(conn=Depends(get_db)):
    return runner.list_runs(conn)


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
