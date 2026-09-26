"""API for Codex workers: the separate app each agent runs as (AGENTS-SPEC 3, 6b).

Authenticated with the agent's own bearer key, not the owner's session.
Waiting for work is a long poll: the worker sleeps on the server side, so an
idle agent never spends tokens (AGENTS-SPEC 4.2).
"""

import json
import sqlite3
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from . import actors, agents, agents_code, approvals, chat, killswitch, mcp_server, runner, tasks
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


class TriageIn(BaseModel):
    verdict: str
    size: str | None = None
    question: str = ""
    reason: str = ""
    run_id: int | None = None


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
        # pos MCP tools it may call; the worker's allowlist is derived from this.
        "pos_tools": mcp_server.allowed_tools(conn, ctx.actor_id), "all_pos_tools": mcp_server.tool_names(),
        "instructions": instructions,
        "guardrails": guard_prompt.agent_guardrails(), "constitution_sha256": guard_prompt.constitution_digest(),
        # Its worker settings from agents/<slug>/agent.json (effort, tools, caps, work folder): the agent
        # pool runs many agents in one container, so each one's settings come from here, not the env.
        "profile": agents_code.worker_profile(row["name"]),
        **_state(conn, ctx.actor_id),
    }


LIVE_RUN_MINUTES = 5  # a run with a heartbeat this recent still has a worker behind it


def _live_run_sql() -> tuple[str, str]:
    """SQL condition "task has a live run of another worker", and its cutoff."""
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=LIVE_RUN_MINUTES)).isoformat(timespec="seconds")
    return ("""EXISTS (SELECT 1 FROM runs r WHERE r.task_id = tasks.id AND r.status = 'running'
               AND COALESCE(r.heartbeat_at, r.started_at) >= ? AND r.id IS NOT ?)""", cutoff)


def _next_work(conn: sqlite3.Connection, ctx: Ctx) -> dict:
    st = _state(conn, ctx.actor_id)
    if st["frozen"] or st["paused"] or st["archived"]:
        return {"state": st}
    unread = chat.inbox_unread(conn, ctx.actor_id)
    # A "working" task is offered again only when no worker is still on it
    # (a second worker of the same agent must not pick up the same task).
    live, cutoff = _live_run_sql()
    row = conn.execute(
        f"""SELECT id FROM tasks WHERE assignee_id = ? AND archived_at IS NULL
           AND (status = 'next' OR (status = 'working' AND NOT {live}))
           AND (retry_after IS NULL OR retry_after <= ?)
           ORDER BY status = 'working' DESC, COALESCE(priority, 4), COALESCE(do_date, '9999'), id LIMIT 1""",
        (ctx.actor_id, cutoff, None, now_iso()),
    ).fetchone()
    out: dict = {"state": st, "unread_messages": unread}
    if row:
        out["task"] = tasks.get(conn, ctx, row["id"])
    return out


POLL_FALLBACK_S = 2.0  # re-check the database this often even without a wake (other processes)


@router.get("/next")
async def next_work(wait: int = 30, ctx: Ctx = Depends(worker_ctx), settings: Settings = Depends(get_settings)):
    """Long poll: returns as soon as there is a task or a message, else after `wait` s.

    A reassignment or a DM wakes the wait at once (pos.wake); the database is
    still re-checked every POLL_FALLBACK_S seconds for changes made elsewhere."""
    from . import wake

    deadline = time.monotonic() + max(0, min(wait, 120))
    async with wake.listener(ctx.actor_id) as woken:
        while True:
            conn = connect(settings.db_path)
            try:
                out = _next_work(conn, ctx)
            finally:
                conn.close()
            remaining = deadline - time.monotonic()
            if out.get("task") or out.get("unread_messages") or remaining <= 0:
                return out
            await wake.wait(woken, min(POLL_FALLBACK_S, remaining))


@router.get("/feedback")
def my_feedback(conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx)):
    """Open feedback for this agent, for the prompt of its next run."""
    from . import feedback

    return feedback.open_for_prompt(conn, ctx.actor_id)


@router.get("/tasks/{task_id}")
def get_task(task_id: str, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx)):
    return tasks.get(conn, ctx, tasks.parse_id(task_id))


@router.post("/tasks/{task_id}/claim")
def claim(task_id: str, run_id: int | None = None, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx)):
    t = tasks.get(conn, ctx, tasks.parse_id(task_id))
    if t["status"] == "working" and t["assignee_id"] == ctx.actor_id:
        live, cutoff = _live_run_sql()
        if conn.execute(f"SELECT 1 FROM tasks WHERE id = ? AND {live}", (t["id"], cutoff, run_id)).fetchone():
            raise HTTPException(409, f"{t['ref']} is already being worked on by another run")
        return t  # resuming its own work after the previous run stopped
    t = tasks.claim(conn, ctx, t["id"])
    conn.commit()
    return t


class NoteIn(BaseModel):
    note: str = ""


def _still_mine(conn: sqlite3.Connection, ctx: Ctx, task_id: int) -> None:
    """A task reassigned meanwhile is no longer this worker's to finish or hand back."""
    row = conn.execute("SELECT assignee_id, assignee_name FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is not None and row["assignee_id"] not in (None, ctx.actor_id):
        raise HTTPException(409, f"{tasks.display_id(task_id)} was reassigned to {row['assignee_name']}")


@router.post("/tasks/{task_id}/complete")
def complete(task_id: str, body: NoteIn, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx)):
    """Hand in the result: it goes to the owner's review."""
    _still_mine(conn, ctx, tasks.parse_id(task_id))
    t = tasks.complete(conn, ctx, tasks.parse_id(task_id), body.note or None)
    conn.commit()
    return t


@router.post("/tasks/{task_id}/progress")
def progress(task_id: str, body: dict, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx)):
    _still_mine(conn, ctx, tasks.parse_id(task_id))
    t = tasks.report_progress(conn, ctx, tasks.parse_id(task_id), int(body.get("percent", 0)), str(body.get("message", "")))
    conn.commit()
    return t


BACKOFF_HOURS = 24  # a handed-back or failed task is not requeued automatically sooner


def back_off(conn: sqlite3.Connection, task_id: int) -> None:
    """No automatic retry of this task for BACKOFF_HOURS; a person assigning it
    or a new event on it (a label, a comment) lifts that (pos.routing)."""
    from datetime import datetime, timedelta, timezone

    until = (datetime.now(timezone.utc) + timedelta(hours=BACKOFF_HOURS)).isoformat(timespec="seconds")
    conn.execute("UPDATE tasks SET retry_after = ? WHERE id = ?", (until, task_id))


def _hand_back(conn: sqlite3.Connection, ctx: Ctx, tid: int, note: str) -> dict:
    name = actors.get(conn, ctx.actor_id)["name"]
    tasks.update(conn, ctx, tid, {"status": "next", "progress_note": f"{name} handed it back: {note}"[:500]})
    t = tasks.assign(conn, ctx, tid, {"type": "human", "id": actors.owner_id(conn)})
    back_off(conn, tid)
    return t


@router.post("/tasks/{task_id}/handback")
def handback(task_id: str, body: NoteIn, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx)):
    """The agent could not finish: the task goes back to the owner, with the reason."""
    tid = tasks.parse_id(task_id)
    _still_mine(conn, ctx, tid)
    t = _hand_back(conn, ctx, tid, body.note)
    conn.commit()
    return t


def _github_issue(conn: sqlite3.Connection, task_id: int) -> tuple[str, int] | None:
    """(repo, issue number) when the task came from a GitHub issue."""
    row = conn.execute("SELECT ref FROM events WHERE task_id = ? AND source = 'github' AND kind = 'issue' "
                       "ORDER BY id LIMIT 1", (task_id,)).fetchone()
    if row is None or "#" not in (row["ref"] or ""):
        return None
    repo, _, number = row["ref"].rpartition("#")
    return (repo, int(number)) if number.isdigit() and "/" in repo else None


VERDICTS = ("clear", "unclear", "too_big", "wrong_repo")


@router.post("/tasks/{task_id}/triage")
def triage(task_id: str, body: TriageIn, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx)):
    """The worker's cheap check before a full run. clear: run it. unclear: one
    clarifying question goes out through the approval queue (a GitHub comment
    on the issue, else an approval for the owner) and the task waits.
    too_big, wrong_repo: back to the owner. Either way no full run for a day."""
    from . import audit, outbound

    if body.verdict not in VERDICTS:
        raise HTTPException(422, f"verdict must be one of {VERDICTS}")
    tid = tasks.parse_id(task_id)
    _still_mine(conn, ctx, tid)
    rctx = Ctx(ctx.actor_id, via="worker", run_id=body.run_id)
    audit.log(conn, rctx, "triage", "task", tid, verdict=body.verdict, size=body.size)
    out: dict = {"verdict": body.verdict, "action": "run"}
    if body.verdict == "unclear" and not agents.has_permission(conn, ctx.actor_id, "approvals:request"):
        _hand_back(conn, rctx, tid, f"unclear, and I may not ask: {body.question}"[:400])
        out["action"] = "handed_back"
    elif body.verdict == "unclear":
        question = (body.question or "What exactly should change, and how will we know it is done?").strip()[:1000]
        issue = _github_issue(conn, tid)
        if issue:
            a = outbound.request(conn, rctx, "github.comment",
                                 {"repo": issue[0], "number": issue[1], "body": question}, tid)
        else:
            a = approvals.request(conn, rctx, "clarify", {"question": question}, tid)
        tasks.update(conn, rctx, tid, {"status": "waiting",
                                       "progress_note": f"Needs clarification (approval #{a['id']}): {question}"[:500]})
        back_off(conn, tid)
        out.update(action="parked", approval_id=a["id"])
    elif body.verdict in ("too_big", "wrong_repo"):
        why = {"too_big": "too big for one run; please split it",
               "wrong_repo": "not about a repository this agent works on"}[body.verdict]
        _hand_back(conn, rctx, tid, f"{why}. {body.reason}".strip()[:400])
        out["action"] = "handed_back"
    conn.commit()
    return out


def _tell_chat_waiting(conn: sqlite3.Connection, tid: int | None, refused: str = "") -> None:
    """A chat task whose run was refused: the person hears why (pos.availability), once per message."""
    if tid is None:
        return
    from . import availability

    try:
        availability.autoreply(conn, tid, refused=refused or None)
    except Exception:  # noqa: BLE001 - the refusal stands either way
        pass


@router.post("/runs", status_code=201)
def start_run(body: RunIn, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx)):
    from . import engines

    tid = tasks.parse_id(body.task_id) if body.task_id else None
    engine, why, model = engines.choose(conn, ctx.actor_id)
    if engine is None:
        _tell_chat_waiting(conn, tid, f"no runtime available: {why}")
        raise HTTPException(409, f"no runtime available: {why}")
    if tid is not None:
        # One live run per task: a second worker (or a retry) is refused here,
        # before a run row exists, not later at claim. The write lock makes the
        # check and the insert one step for concurrent requests.
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        live, cutoff = _live_run_sql()
        if conn.execute(f"SELECT 1 FROM tasks WHERE id = ? AND {live}", (tid, cutoff, None)).fetchone():
            conn.rollback()
            raise HTTPException(409, f"{tasks.display_id(tid)} already has a live run")
    res = runner.start_external(conn, runner.RunRequest(ctx.actor_id, body.kind, "", task_id=tid, engine=engine,
                                                        model=model))
    if res.status == "blocked":
        _tell_chat_waiting(conn, tid, res.error or "")
        raise HTTPException(409, res.error)
    # A run on a chat answer: the agent shows as typing there (no tool call, no tokens).
    chat.typing_on_run_start(conn, ctx.actor_id, res.run_id, tid)
    from .access import service as access

    # The agent's max USD per run (pos.access): the worker hands it to the engine as its cost cap.
    return {"run_id": res.run_id, "engine": engine, "model": model,
            "max_budget_usd": access.run_cap_usd(conn, ctx.actor_id)}


class BeatIn(BaseModel):
    step: str | None = None  # what the completed step was (a tool, a command), for the chat status snapshot
    steps: int | None = None


@router.post("/runs/{run_id}/heartbeat")
def heartbeat(run_id: int, body: BeatIn | None = None, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx)):
    from . import fastlane

    conn.execute("UPDATE runs SET heartbeat_at = ? WHERE id = ? AND actor_id = ?", (now_iso(), run_id, ctx.actor_id))
    conn.commit()
    chat.typing_run_step(run_id)
    fastlane.step(run_id, body.step if body else None, body.steps if body else None)
    return _state(conn, ctx.actor_id, run_id)


@router.post("/runs/{run_id}/alive")
def alive(run_id: int, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx)):
    """The worker's tick between steps (long tool work): keeps a chat run's softer
    "working" indicator, nothing else. Memory only."""
    r = conn.execute("SELECT status FROM runs WHERE id = ? AND actor_id = ?", (run_id, ctx.actor_id)).fetchone()
    if r and r["status"] == "running":
        chat.typing_run_alive(run_id)
    else:
        chat.typing_clear(run_id=run_id)
    return {"ok": True}


@router.post("/runs/{run_id}/finish")
def finish_run(run_id: int, body: FinishIn, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx)):
    from . import engines

    try:
        row = runner.finish_external(conn, run_id, ctx.actor_id, body.status, body.jsonl, body.detail)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    chat.typing_clear(run_id=run_id)
    from . import fastlane

    fastlane.forget(run_id)
    out = dict(row)
    # The run failed because its engine hit the subscription limit: not the task's
    # fault. It goes straight back to the queue; the next run uses the other engine.
    until = engines.paused_until(conn, row["engine"]) if row["engine"] and body.status == "error" else None
    if until and row["task_id"]:
        t = conn.execute("SELECT status, assignee_id FROM tasks WHERE id = ?", (row["task_id"],)).fetchone()
        if t and t["status"] == "working" and t["assignee_id"] == ctx.actor_id:
            tasks.update(conn, ctx, row["task_id"], {
                "status": "next",
                "progress_note": f"{row['engine'].capitalize()} hit its usage limit (until {until}); "
                                 "retrying on the other runtime."})
            conn.commit()
            out["requeued"] = True
    if body.status == "error" and row["task_id"] and not out.get("requeued"):
        back_off(conn, row["task_id"])  # a failed task is not retried at once
        conn.commit()
    return out


@router.get("/inbox")
def inbox(run_id: int | None = None, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx)):
    out = agents.check_inbox(conn, ctx.actor_id, run_id=run_id)
    conn.commit()
    return out


@router.get("/tools")
def my_tools(conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx)):
    """The tools this agent's worker mounts: its personal ones and the shared ones it may use."""
    from . import tools

    out = tools.for_agent(conn, ctx.actor_id)
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
            "notes": f"Purpose: an agent is blocked on a shell command the constitution reserves for the "
                     f"owner. Source: the command guard.\n\n"
                     f"{actors.get(conn, ctx.actor_id)['name']} wanted to run:\n\n{body.command}\n\n"
                     f"Constitution {d.rule}: {d.reason}",
            "definition_of_done": "The owner ran the command (or decided not to) and told the agent.",
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


# ------------------------------------------------------------------ browser (pos.browser)

@router.post("/browser/check")
def browser_check(body: dict, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx),
                  settings: Settings = Depends(get_settings)):
    """Before a browser action: allow, or an approval the guard waits for."""
    from . import agents, browser

    st = _state(conn, ctx.actor_id)
    if st["frozen"] or st["paused"] or st["archived"]:
        return {"decision": "refuse", "reason": "the kill switch is on or this agent is paused"}
    if not agents.has_permission(conn, ctx.actor_id, "browser:use"):
        return {"decision": "refuse", "reason": "this agent lacks browser:use"}
    return browser.check(conn, ctx, settings.data_dir, body)


@router.post("/browser/log")
def browser_log(body: dict, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx),
                settings: Settings = Depends(get_settings)):
    from . import browser

    return browser.record(conn, ctx, settings.data_dir, body)


@router.get("/approvals/{approval_id}")
def approval_state(approval_id: int, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx)):
    a = approvals.get(conn, approval_id)
    if a["requested_by"] != ctx.actor_id:
        raise HTTPException(404, "not your approval")
    return {"id": a["id"], "status": a["status"], "comment": a.get("comment")}
