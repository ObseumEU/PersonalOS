"""API for Codex workers: the separate app each agent runs as (AGENTS-SPEC 3, 6b).

Authenticated with the agent's own bearer key, not the owner's session.
Waiting for work is a long poll: the worker sleeps on the server side, so an
idle agent never spends tokens (AGENTS-SPEC 4.2).
"""

import asyncio
import json
import sqlite3
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from . import actors, agents, killswitch, runner, tasks
from .api_tasks import get_db
from .config import Settings, get_settings
from .core import Ctx, now_iso
from .db import connect

router = APIRouter(prefix="/api/worker", tags=["worker"])


def worker_ctx(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> Ctx:
    auth = request.headers.get("authorization", "")
    key = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    actor_id = actors.actor_for_key(conn, key) if key else None
    if actor_id is None:
        raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})
    if actors.get(conn, actor_id)["kind"] == "human":
        raise HTTPException(403, "worker endpoints are for agents")
    conn.execute("UPDATE actors SET last_seen_at = ? WHERE id = ?", (now_iso(), actor_id))
    conn.commit()
    return Ctx(actor_id, via="worker")


class RunIn(BaseModel):
    task_id: str | None = None
    kind: str = "task"


class FinishIn(BaseModel):
    status: str
    jsonl: str = ""
    detail: str = ""


class CommandIn(BaseModel):
    command: str
    external: bool = False
    run_id: int | None = None


def _state(conn: sqlite3.Connection, actor_id: int, run_id: int | None = None) -> dict:
    row = actors.get(conn, actor_id)
    cancelled = False
    if run_id:
        r = conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
        cancelled = bool(r and r["status"] != "running")
    return {"frozen": killswitch.is_frozen(conn), "paused": bool(row["paused_at"]),
            "archived": bool(row["archived_at"]), "run_cancelled": cancelled}


@router.get("/me")
def me(conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx)):
    from .guard import prompt as guard_prompt

    row = actors.get(conn, ctx.actor_id)
    instructions = agents.instructions_of(row) or ""
    return {
        "id": row["id"], "name": row["name"], "kind": row["kind"],
        "permissions": sorted(agents.permissions_of(conn, ctx.actor_id)),
        "instructions": instructions,
        "guardrails": guard_prompt.agent_guardrails(), "constitution_sha256": guard_prompt.constitution_digest(),
        **_state(conn, ctx.actor_id),
    }


def _next_work(conn: sqlite3.Connection, ctx: Ctx) -> dict:
    st = _state(conn, ctx.actor_id)
    if st["frozen"] or st["paused"] or st["archived"]:
        return {"state": st}
    unread = conn.execute("SELECT COUNT(*) FROM messages WHERE to_actor = ? AND read_at IS NULL",
                          (ctx.actor_id,)).fetchone()[0]
    row = conn.execute(
        """SELECT id FROM tasks WHERE assignee_id = ? AND archived_at IS NULL AND status IN ('next', 'working')
           ORDER BY status = 'working' DESC, COALESCE(priority, 4), COALESCE(do_date, '9999'), id LIMIT 1""",
        (ctx.actor_id,),
    ).fetchone()
    out: dict = {"state": st, "unread_messages": unread}
    if row:
        out["task"] = tasks.get(conn, ctx, row["id"])
    return out


@router.get("/next")
async def next_work(wait: int = 30, ctx: Ctx = Depends(worker_ctx), settings: Settings = Depends(get_settings)):
    """Long poll: returns as soon as there is a task or a message, else after `wait` s."""
    deadline = time.monotonic() + max(0, min(wait, 120))
    while True:
        conn = connect(settings.db_path)
        try:
            out = _next_work(conn, ctx)
        finally:
            conn.close()
        if out.get("task") or out.get("unread_messages") or time.monotonic() >= deadline:
            return out
        await asyncio.sleep(2)


@router.get("/tasks/{task_id}")
def get_task(task_id: str, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx)):
    return tasks.get(conn, ctx, tasks.parse_id(task_id))


@router.post("/tasks/{task_id}/claim")
def claim(task_id: str, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx)):
    t = tasks.get(conn, ctx, tasks.parse_id(task_id))
    if t["status"] == "working" and t["assignee_id"] == ctx.actor_id:
        return t  # resuming its own work
    t = tasks.claim(conn, ctx, t["id"])
    conn.commit()
    return t


class NoteIn(BaseModel):
    note: str = ""


@router.post("/tasks/{task_id}/complete")
def complete(task_id: str, body: NoteIn, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx)):
    """Hand in the result: it goes to the owner's review."""
    t = tasks.complete(conn, ctx, tasks.parse_id(task_id), body.note or None)
    conn.commit()
    return t


@router.post("/tasks/{task_id}/progress")
def progress(task_id: str, body: dict, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx)):
    t = tasks.report_progress(conn, ctx, tasks.parse_id(task_id), int(body.get("percent", 0)), str(body.get("message", "")))
    conn.commit()
    return t


@router.post("/tasks/{task_id}/handback")
def handback(task_id: str, body: NoteIn, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx)):
    """The agent could not finish: the task goes back to the owner, with the reason."""
    tid = tasks.parse_id(task_id)
    name = actors.get(conn, ctx.actor_id)["name"]
    tasks.update(conn, ctx, tid, {"status": "next", "progress_note": f"{name} handed it back: {body.note}"[:500]})
    t = tasks.assign(conn, ctx, tid, {"type": "human", "id": actors.owner_id(conn)})
    conn.commit()
    return t


@router.post("/runs", status_code=201)
def start_run(body: RunIn, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx)):
    from . import engines

    tid = tasks.parse_id(body.task_id) if body.task_id else None
    engine, why, model = engines.choose(conn, ctx.actor_id)
    if engine is None:
        raise HTTPException(409, f"no runtime available: {why}")
    res = runner.start_external(conn, runner.RunRequest(ctx.actor_id, body.kind, "", task_id=tid, engine=engine))
    if res.status == "blocked":
        raise HTTPException(409, res.error)
    return {"run_id": res.run_id, "engine": engine, "model": model}


@router.post("/runs/{run_id}/heartbeat")
def heartbeat(run_id: int, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx)):
    return _state(conn, ctx.actor_id, run_id)


@router.post("/runs/{run_id}/finish")
def finish_run(run_id: int, body: FinishIn, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx)):
    try:
        row = runner.finish_external(conn, run_id, ctx.actor_id, body.status, body.jsonl, body.detail)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return dict(row)


@router.get("/inbox")
def inbox(run_id: int | None = None, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx)):
    out = agents.check_inbox(conn, ctx.actor_id, run_id=run_id)
    conn.commit()
    return out


@router.post("/check-command")
def check_command(body: CommandIn, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx)):
    """Before an agent shell command. NEEDS_OWNER becomes a task for the owner."""
    from .guard import commands
    from .integrations import guard_actor

    d = commands.evaluate(body.command, guard_actor(conn, ctx.actor_id),
                          commands.Trigger.EXTERNAL if body.external else commands.Trigger.MEMBER)
    out = {"outcome": d.outcome.value, "rule": d.rule, "reason": d.reason}
    if d.outcome.value == "needs_owner":
        t = tasks.create(conn, ctx, {
            "title": f"Approve or run: {body.command[:80]}",
            "notes": f"{actors.get(conn, ctx.actor_id)['name']} wanted to run:\n\n{body.command}\n\n"
                     f"Constitution {d.rule}: {d.reason}",
            "priority": 1, "assignee": "me", "status": "next",
        })
        out["owner_task"] = t["ref"]
    conn.commit()
    return out


@router.post("/wrap")
def wrap(payload: dict, ctx: Ctx = Depends(worker_ctx)):
    """Mark outside content as untrusted before it goes into a prompt."""
    from .guard.external import scan, wrap_external

    content = str(payload.get("content", ""))
    return {"wrapped": wrap_external(str(payload.get("source", "external")), content, ref=payload.get("ref")),
            "signals": scan(content).signals}


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
