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
    # Files uploaded first (POST /api/files): a screenshot pasted into the answer (at most 10).
    attachments: list[int] = []


class CommentIn(BaseModel):
    body: str = ""
    attachments: list[int] = []


def with_files(conn, ctx, text: str | None, ids: list[int]) -> str:
    """The text with a "📎 name (soubor #id)" line per attached file, like a chat message: the
    agent's worker finds the images by these lines and hands them to the model (pos_worker.images).
    Only files the sender may read."""
    from . import files

    if not ids:
        return (text or "").strip()
    _, lines = files.chat_attachments(conn, ctx, ids)
    return "\n".join(x for x in [(text or "").strip(), *lines] if x)


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
    """`scope`: mine (assigned to me), team (me and everyone below me) or all.
    `board` (every open task and what finished in the last week) carries each task's cached summary."""
    if view != "board":
        return tasks.list_tasks(conn, ctx, view, topic=topic, assignee_id=assignee_id, scope=scope)
    from . import task_summary

    out = tasks.list_tasks(conn, ctx, view, topic=topic, assignee_id=assignee_id, scope=scope, limit=500)
    cached = task_summary.cached_for(conn, [t["id"] for t in out], ctx.actor_id)
    for t in out:
        t["summary"] = cached.get(t["id"])
    return out


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
    t = tasks.complete(conn, ctx, tasks.parse_id(task_id), with_files(conn, ctx, body.note, body.attachments) or None)
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


@router.get("/tasks/{task_id}/summary")
def task_summary(task_id: str, generate: bool = True, conn=Depends(get_db), ctx=Depends(get_ctx)):
    """The task's 2–3 line TL;DR (one cached claude-haiku-4-5 call, platform cost; a fallback without it)."""
    from . import task_summary as ts

    return ts.summary(conn, ctx, tasks.parse_id(task_id), generate=generate)


@router.get("/tasks/{task_id}/related")
def task_related(task_id: str, conn=Depends(get_db), ctx=Depends(get_ctx)):
    """What the task detail links to: the owner's open asks raised from this task, the task an
    ask ticket is for, pending approvals on it (owner only) and the tasks its text mentions."""
    import json
    import re

    tid = tasks.parse_id(task_id)
    task = tasks.get(conn, ctx, tid)
    viewer = actors.get(conn, ctx.actor_id)

    def brief(i: int) -> dict | None:
        try:
            t = tasks.get(conn, ctx, i)
        except (NotFound, Forbidden):
            return None
        return {"id": t["id"], "ref": t["ref"], "title": t["title"], "status": t["status"],
                "assignee_name": t.get("assignee_name"), "assignee_type": t.get("assignee_type")}

    has_asks = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'owner_asks'").fetchone() is not None
    asks_open, ask_for = [], None
    if has_asks:
        for a in conn.execute(
                """SELECT o.*, x.name AS asker_name FROM owner_asks o JOIN actors x ON x.id = o.asker_id
                   JOIN tasks t ON t.id = o.ticket_id
                   WHERE o.source_task_id = ? AND o.status = 'open' AND t.status != 'done' AND t.archived_at IS NULL
                   ORDER BY o.id DESC LIMIT 10""", (tid,)):
            b = brief(a["ticket_id"])
            if b:
                b.update(notes=conn.execute("SELECT notes FROM tasks WHERE id = ?", (a["ticket_id"],)).fetchone()[0],
                         asker_name=a["asker_name"], blocking=bool(a["blocking"]), kind=a["kind"])
                asks_open.append(b)
        a = conn.execute("""SELECT o.*, x.name AS asker_name FROM owner_asks o JOIN actors x ON x.id = o.asker_id
                            WHERE o.ticket_id = ? ORDER BY o.id DESC LIMIT 1""", (tid,)).fetchone()
        if a:
            ask_for = {"asker_name": a["asker_name"], "blocking": bool(a["blocking"]), "kind": a["kind"],
                       "status": a["status"], "task": brief(a["source_task_id"]) if a["source_task_id"] else None}
    approvals_ = []
    if viewer["is_owner"]:
        for r in conn.execute(
                """SELECT a.id, a.action, a.details, a.created_at, r.name AS requested_by_name FROM approvals a
                   LEFT JOIN actors r ON r.id = a.requested_by
                   WHERE a.task_id = ? AND a.status = 'pending' ORDER BY a.id""", (tid,)):
            details = json.loads(r["details"] or "{}")
            why = str(details.get("why") or details.get("reason") or details.get("summary") or "")
            shown = {k: v for k, v in details.items() if k not in ("why", "reason", "summary", "screenshot")}
            approvals_.append({"id": r["id"], "action": r["action"], "why": why[:400], "at": r["created_at"],
                               "requested_by_name": r["requested_by_name"], "details": shown})
    text = " ".join(filter(None, [task.get("notes"), task.get("progress_note"), task.get("definition_of_done")]))
    seen = {tid, task.get("parent_id"), *(a["id"] for a in asks_open)}
    if ask_for and ask_for["task"]:
        seen.add(ask_for["task"]["id"])
    mentioned = []
    for m in re.finditer(r"\bT-(\d{1,6})\b", text):
        i = int(m.group(1))
        if i in seen or len(mentioned) >= 12:
            continue
        seen.add(i)
        b = brief(i)
        if b:
            mentioned.append(b)
    return {"asks_open": asks_open, "ask_for": ask_for, "approvals": approvals_, "mentioned": mentioned}


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
    description: str | None = None
    start_date: str | None = None
    links: dict | None = None
    facts: dict | None = None
    kb_workspace: str | None = None
    keywords: list[str] | None = None


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
    details = {k: d.pop(k) for k in ("description", "start_date", "links", "facts", "kb_workspace", "keywords")}
    p = projects.create(conn, ctx, member_refs=d.pop("members"), details=details, **d)
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

    c = comments.add(conn, ctx, tasks.parse_id(task_id), with_files(conn, ctx, body.body, body.attachments))
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
