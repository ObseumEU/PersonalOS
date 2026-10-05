"""API for Codex workers: the separate app each agent runs as (AGENTS-SPEC 3, 6b).

Authenticated with the agent's own bearer key, not the owner's session.
Waiting for work is a long poll: the worker sleeps on the server side, so an
idle agent never spends tokens (AGENTS-SPEC 4.2).
"""

import json
import logging
import sqlite3
import time
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from . import actors, agents, agents_code, approvals, chat, killswitch, mcp_server, runner, tasks
from .api_tasks import get_db
from .config import Settings, get_settings
from .core import Ctx, Forbidden, now_iso
from .db import connect

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/worker", tags=["worker"])


def worker_ctx(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> Ctx:
    auth = request.headers.get("authorization", "")
    key = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    actor_id = actors.actor_for_key(conn, key) if key else None
    if actor_id is None:
        raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})
    row = actors.get(conn, actor_id)
    if row["kind"] == "human":
        raise HTTPException(403, "worker endpoints are for agents")
    # Every worker call (the alive tick every 10 s, heartbeats, polls) would be a write; the
    # "seen" time moves at most every SEEN_WRITE_S (pos.workers reads it at a 5-min scale).
    if (row["last_seen_at"] or "") < _seen_cutoff():
        conn.execute("UPDATE actors SET last_seen_at = ? WHERE id = ?", (now_iso(), actor_id))
        conn.commit()
    return Ctx(actor_id, via="worker")


SEEN_WRITE_S = 30


def _seen_cutoff() -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(seconds=SEEN_WRITE_S)).isoformat(timespec="seconds")


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
    cwd: str | None = None      # where the shell runs (the CLI's hook event)
    workdir: str | None = None  # the agent's own worktree (the worker's POS_AGENT_WORKDIR)
    cli_allowed: bool | None = None  # would the CLI's allow-list run it (the hook knows; None: no list)


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
    from . import business, cache_policy, evidence, verification
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
        "profile": agents_code.worker_profile(row["name"], role=row["role"]),
        # Platform notes for its prompt (pos.business: e.g. contacting the owner past the chain of command).
        "nudges": business.nudges(conn, ctx.actor_id) + verification.nudges(conn, ctx.actor_id)
        + evidence.nudges(conn, ctx.actor_id),
        # The prompt-cache TTL by how often it runs (pos.cache_policy; agent.json "cache_ttl" wins).
        "cache_ttl": cache_policy.ttl_for(conn, ctx.actor_id),
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
    # A task with a live run is never offered, whatever its status: a "working" one, and a "next"
    # one too (a run between its start and its claim, or a task set back to `next` while its run
    # goes on: a hand-back, an owner's comment). Offering those was the race behind 142 refused
    # POST /runs ("already has a live run") in 72 h (prod 10-05).
    live, cutoff = _live_run_sql()
    from . import business, review_work
    from .access import service as access
    from .core import today

    # Near the company's daily cap only business tasks start (pos.access.business_only); a task already
    # being worked on is finished either way.
    reserve = access.business_only(conn)
    if reserve:
        st["business_only"] = True
    row = None
    # A task planned for a later day (do_date, Europe/Prague) waits for that day unless it is already
    # being worked on (prod: T-516 with do_date 9 Oct was picked 241 times).
    candidates = conn.execute(
        f"""SELECT * FROM tasks WHERE assignee_id = ? AND archived_at IS NULL
           AND status IN ('next', 'working') AND NOT {live}
           AND (retry_after IS NULL OR retry_after <= ?)
           AND (status = 'working' OR do_date IS NULL OR do_date <= ?)
           ORDER BY status = 'working' DESC, COALESCE(priority, 4), COALESCE(do_date, '9999'), id LIMIT 50""",
        (ctx.actor_id, cutoff, None, now_iso(), today().isoformat()),
    ).fetchall()
    for cand in candidates:
        # a review item whose result no longer waits is closed and skipped (pos.review_work)
        if review_work.stale(conn, cand):
            conn.commit()
            continue
        if reserve and cand["status"] != "working" and business.classify(conn, cand) != "business":
            continue
        row = cand
        break
    out: dict = {"state": st, "unread_messages": unread}
    if row:
        # Over its own limit (runs_day, usd_day...): no task until the limit resets, so neither this
        # worker nor the pool's probe starts a run that would only be refused (pos.access).
        until = access.limited_until(conn, ctx.actor_id)
        if until:
            st["limited_until"] = until
            row = None
    if row:
        out["task"] = tasks.get(conn, ctx, row["id"])
        if review_work.is_item(row):
            # The reviewer decides from a packet built here (and similar small reviews in one run),
            # instead of exploring the result itself (pos.review_packet).
            from . import review_packet

            out["task"] = review_packet.serve(conn, row, out["task"], live, (cutoff, None))
        # The owner asked for this himself: the worker allows the profile's higher step cap (max_steps_owner).
        out["task"]["owner_request"] = business.owner_request(conn, conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone())
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


@router.get("/memory")
def my_memory(conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx)):
    """This agent's pinned memory, for the prompt of its next run (a starter note when it has none)."""
    from . import agent_memory, learning

    if learning.ensure_memory(conn, ctx.actor_id):
        conn.commit()
    return agent_memory.get(conn, ctx.actor_id)


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
IDLE_BACKOFF_HOURS = 6  # an ok run that left its task open (next/working) is not requeued sooner


def back_off(conn: sqlite3.Connection, task_id: int) -> None:
    """No automatic retry of this task for BACKOFF_HOURS; a person assigning it
    or a new event on it (a label, a comment) lifts that (pos.routing)."""
    from datetime import datetime, timedelta, timezone

    until = (datetime.now(timezone.utc) + timedelta(hours=BACKOFF_HOURS)).isoformat(timespec="seconds")
    conn.execute("UPDATE tasks SET retry_after = ? WHERE id = ?", (until, task_id))


def idle_back_off(conn: sqlite3.Connection, task_id: int, actor_id: int) -> str | None:
    """An ok run ended and its task is still open with the same agent (`next`, or `working` with
    nothing handed in): no new run at once (prod: T-516 re-queued 241 times). Held IDLE_BACKOFF_HOURS,
    or until its do_date (Europe/Prague) when that is later. A person assigning it, a comment or a
    message lifts it (pos.routing, pos.comments). Returns the new retry_after, or None."""
    from datetime import datetime, timedelta, timezone

    from .core import TZ

    t = conn.execute("SELECT status, assignee_id, do_date, retry_after, topic, source FROM tasks WHERE id = ?",
                     (task_id,)).fetchone()
    if t is None or t["assignee_id"] != actor_id or t["status"] not in ("next", "working"):
        return None
    if (t["topic"] or "") == "chat" or (t["source"] or "").startswith("meeting"):
        return None
    until = datetime.now(timezone.utc) + timedelta(hours=IDLE_BACKOFF_HOURS)
    if t["do_date"]:
        try:
            day = datetime.fromisoformat(t["do_date"]).replace(tzinfo=TZ).astimezone(timezone.utc)
            until = max(until, day)
        except ValueError:
            pass
    at = until.isoformat(timespec="seconds")
    if (t["retry_after"] or "") >= at:
        return None
    conn.execute("UPDATE tasks SET retry_after = ? WHERE id = ?", (at, task_id))
    return at


# A task re-dispatched again and again without a result (T-107: a run that ends without complete_task
# comes back; T-517 ran 176 times, T-516 57): after this many runs of one agent on one task in
# NO_RESULT_WINDOW_HOURS with nothing handed in, it is held for NO_RESULT_HOLD_HOURS and the agent's
# lead is told (pos.head_alerts, reason no_result). The owner's comment or a person's reassignment
# lifts the hold; the next run without a result holds it again at once.
NO_RESULT_RUNS = 4
NO_RESULT_WINDOW_HOURS = 72
NO_RESULT_HOLD_HOURS = 48


def no_result_hold(conn: sqlite3.Connection, task_id: int, actor_id: int) -> str | None:
    """Hold a task its agent ran NO_RESULT_RUNS times without a result; returns the new retry_after."""
    from datetime import datetime, timedelta, timezone

    t = conn.execute("SELECT status, assignee_id, retry_after, topic, source, updated_at FROM tasks WHERE id = ?",
                     (task_id,)).fetchone()
    if t is None or t["assignee_id"] != actor_id or t["status"] not in ("next", "working"):
        return None
    if (t["topic"] or "") == "chat" or (t["source"] or "").startswith("meeting"):
        return None
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=NO_RESULT_WINDOW_HOURS)).isoformat(timespec="seconds")
    # runs since its last hand-in (a result in review or done resets the count)
    last_hand_in = conn.execute("""SELECT MAX(at) FROM history WHERE entity = 'task' AND entity_id = ?
                                   AND json_extract(data, '$.status') IN ('review', 'done')""",
                                (task_id,)).fetchone()[0]
    n = conn.execute("""SELECT COUNT(*) FROM runs WHERE task_id = ? AND actor_id = ? AND started_at >= ?
                        AND status IN ('ok', 'error')""", (task_id, actor_id, max(since, last_hand_in or ""))
                     ).fetchone()[0]
    if n < NO_RESULT_RUNS:
        return None
    at = (now + timedelta(hours=NO_RESULT_HOLD_HOURS)).isoformat(timespec="seconds")
    if (t["retry_after"] or "") >= at:
        return None
    from . import audit, business, comments, head_alerts

    conn.execute("UPDATE tasks SET retry_after = ? WHERE id = ?", (at, task_id))
    comments.log(conn, business.system_ctx(conn), task_id,
                 f"Úkol běžel {n}× bez výsledku (žádné complete_task): pozastaven do {at[:16].replace('T', ' ')} "
                 "UTC, vedoucí agenta to ví. Dřív ho uvolní komentář majitele nebo nové přiřazení.", "system")
    audit.log(conn, business.system_ctx(conn), "no_result_hold", "task", task_id, runs=n, until=at,
              agent_id=actor_id)
    head_alerts.notify(conn, actor_id, task_id, "no_result", f"{n} běhů za {NO_RESULT_WINDOW_HOURS} h bez výsledku")
    return at


def _hand_back(conn: sqlite3.Connection, ctx: Ctx, tid: int, note: str) -> dict:
    from . import meetings, owner_notice

    if meetings.skip_task(conn, tid, f"handed back: {note}"[:200]):
        return tasks.get(conn, ctx, tid)  # a meeting turn: skipped, the next participant speaks (no owner notice)

    name = actors.get(conn, ctx.actor_id)["name"]
    before = conn.execute("SELECT progress_note FROM tasks WHERE id = ?", (tid,)).fetchone()
    tasks.update(conn, ctx, tid, {"status": "next", "progress_note": f"{name} handed it back: {note}"[:500]})
    t = tasks.assign(conn, ctx, tid, {"type": "human", "id": actors.owner_id(conn)})
    back_off(conn, tid)
    # Never silent: a task the owner asked for tells him what happened, in his thread.
    owner_notice.notify(conn, tid, ctx.actor_id, "capped" if (note or "").startswith("step limit") else "handed_back",
                        note, (before["progress_note"] if before else "") or "")
    return t


@router.post("/tasks/{task_id}/handback")
def handback(task_id: str, body: NoteIn, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx)):
    """The agent could not finish: the task goes back to the owner, with the reason."""
    from . import pseudo_tools

    tid = tasks.parse_id(task_id)
    _still_mine(conn, ctx, tid)
    if pseudo_tools.MARKER in (body.note or ""):
        t = fake_tool_calls(conn, ctx, tid)
    else:
        t = _hand_back(conn, ctx, tid, pseudo_tools.clean(body.note))
    conn.commit()
    return t


def fake_tool_calls(conn: sqlite3.Connection, ctx: Ctx, tid: int) -> dict:
    """The run failed because the model wrote its tool calls as text twice (pos_worker.loop): the
    tools never ran, so this is the platform's fault, not the task's. Not done, not the owner's:
    the task waits (back-off) with the agent, the SRE gets an incident (the Monitor routes it) and
    the agent's lead a message. The model's text itself goes nowhere."""
    from . import pseudo_tools, workers

    me = actors.get(conn, ctx.actor_id)
    ref = tasks.display_id(tid)
    lead = conn.execute("SELECT id, name FROM actors WHERE id = ? AND archived_at IS NULL",
                        (me["reports_to"],)).fetchone() if me["reports_to"] else None
    t = tasks.update(conn, ctx, tid, {
        "status": "next",
        "progress_note": f"{me['name']}: model napsal volání nástrojů jako text, nástroje se nespustily "
                         f"(i po jednom opakování). Nahlášeno SRE{' a ' + lead['name'] if lead else ''} jako incident."})
    back_off(conn, tid)
    workers._incident(
        conn, iid=f"fake-tools-{ctx.run_id or 0}-{tid}", kind="fake_tool_calls", key=f"agent:{me['id']}",
        title=f"{me['name']}: the model wrote tool calls as text ({ref})",
        body=(f"{me['name']} ({ref}) answered with tool-call markup as text twice in a row: {pseudo_tools.MARKER}. "
              "Check the agent's worker settings (claude_builtin/claude_tools in its profile, the MCP config, "
              "the engine and model), then requeue the task."),
        detail={"agent_id": me["id"], "task_id": tid, "run_id": ctx.run_id})
    if lead and lead["id"] != ctx.actor_id:
        try:
            chat.send_dm(conn, ctx, lead["id"],
                         f"[platforma] {ref}: můj model napsal volání nástrojů jako text, nic se nespustilo. "
                         "Úkol čeká, SRE má incident; nic z toho textu neber jako hotovou práci.", system=True)
        except Exception as e:  # noqa: BLE001 - the incident stands either way
            import logging

            logging.getLogger(__name__).info("lead not told about %s: %s", ref, e)
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
    clarifying question goes out (a GitHub comment on the issue, sent directly under
    Ú1, else an approval for the owner) and the task waits.
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
        a = outbound.request(conn, rctx, "github.comment",
                             {"repo": issue[0], "number": issue[1], "body": question}, tid) if issue else {}
        if not a.get("id") and a.get("status") != "sent":  # no issue, or the GitHub connector is off
            a = approvals.request(conn, rctx, "clarify", {"question": question}, tid)
        where = f"approval #{a['id']}" if a.get("id") else f"asked on {issue[0]}#{issue[1]}"
        tasks.update(conn, rctx, tid, {"status": "waiting",
                                       "progress_note": f"Needs clarification ({where}): {question}"[:500]})
        back_off(conn, tid)
        from . import owner_notice

        owner_notice.notify(conn, tid, ctx.actor_id, "blocked",
                            f"potřebuje upřesnění ({where}): {question}")
        out.update(action="parked", approval_id=a.get("id"))
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
        if tid is not None and not chat.chat_origin(conn, tid):  # a chat task already got the autoreply
            from . import owner_notice

            if owner_notice.notify(conn, tid, ctx.actor_id, "blocked", res.error or ""):
                conn.commit()
        from . import head_alerts

        if head_alerts.run_ended(conn, ctx.actor_id, tid, "blocked", res.error or ""):
            conn.commit()
        raise HTTPException(409, res.error)
    # A run on a chat answer: the agent shows as typing there (no tool call, no tokens).
    chat.typing_on_run_start(conn, ctx.actor_id, res.run_id, tid)
    # Its question is in the prompt: not injected again at the first step (a double answer).
    chat.take_question(conn, ctx.actor_id, tid)
    from .access import service as access

    # The agent's max USD per run (pos.access): the worker hands it to the engine as its cost cap.
    out = {"run_id": res.run_id, "engine": engine, "model": model,
           "max_budget_usd": access.run_cap_usd(conn, ctx.actor_id)}
    if tid is not None:
        out.update(_run_context(conn, ctx, tid, res.run_id))
    return out


def _run_context(conn: sqlite3.Connection, ctx: Ctx, tid: int, run_id: int) -> dict:
    """What a run starts with besides its task: the tainted-run mark when the task carries outside
    content (pos.taint), and the knowledge base's passages for it (pos.knowledge_first). Fail-open."""
    from . import knowledge_first, taint

    out: dict = {}
    try:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        if row is not None:
            taint.mark_task(conn, ctx.actor_id, row, run_id)
        conn.commit()
        rctx = Ctx(ctx.actor_id, via="worker", run_id=run_id)
        kn = knowledge_first.preload(conn, rctx, {**dict(row), "ref": tasks.display_id(tid)}) if row else None
        if kn:
            # Outside passages are pointers only (pos.knowledge_first): nothing here taints the run;
            # opening one through the knowledge tool does.
            for source in kn["external"]:
                taint.mark(conn, ctx.actor_id, f"knowlage:{source}", kn["text"][:1500], "knowledge pre-load", run_id)
            out["knowledge"] = kn["text"]
            out["knowledge_chunks"] = kn["chunks"]
        conn.commit()
    except Exception:  # noqa: BLE001 - the run starts either way
        log.exception("run context for T-%s", tid)
        conn.rollback()
    return out


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


ALIVE_WRITE_S = 60  # the alive tick comes every 10 s; the heartbeat column moves at most this often


@router.post("/runs/{run_id}/alive")
def alive(run_id: int, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx)):
    """The worker's tick between steps (long tool work): keeps a chat run's softer
    "working" indicator and the run itself alive: its heartbeat moves on (at most once a
    minute), so a step longer than the reaper's 5 min (pos.scheduler.reap_runs) is not released and the task is
    not offered to a second worker (_live_run_sql)."""
    from datetime import datetime, timedelta, timezone

    r = conn.execute("SELECT status FROM runs WHERE id = ? AND actor_id = ?", (run_id, ctx.actor_id)).fetchone()
    if r and r["status"] == "running":
        chat.typing_run_alive(run_id)
        stale = (datetime.now(timezone.utc) - timedelta(seconds=ALIVE_WRITE_S)).isoformat(timespec="seconds")
        conn.execute("UPDATE runs SET heartbeat_at = ? WHERE id = ? AND status = 'running' "
                     "AND (heartbeat_at IS NULL OR heartbeat_at < ?)", (now_iso(), run_id, stale))
        conn.commit()
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
        if row["status"] == "error":  # not when the kill switch or a person stopped it meanwhile
            from . import head_alerts

            head_alerts.run_ended(conn, ctx.actor_id, row["task_id"], "error", body.detail)
        conn.commit()
    if body.status == "error" and not out.get("requeued"):
        from . import learning

        try:  # a failure with a clear lesson goes into the agent's memory (pos.learning)
            if learning.on_failed_run(conn, conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()):
                conn.commit()
        except Exception:  # noqa: BLE001 - the run's end is what matters here
            conn.rollback()
    if body.status == "ok" and row["task_id"] and row["status"] == "ok":
        if idle_back_off(conn, row["task_id"], ctx.actor_id):
            conn.commit()
            out["held_until"] = conn.execute("SELECT retry_after FROM tasks WHERE id = ?",
                                             (row["task_id"],)).fetchone()[0]
    if body.status in ("ok", "error") and row["task_id"] and not out.get("requeued"):
        if no_result_hold(conn, row["task_id"], ctx.actor_id):  # the same task again and again, no result
            conn.commit()
            out["held_until"] = conn.execute("SELECT retry_after FROM tasks WHERE id = ?",
                                             (row["task_id"],)).fetchone()[0]
    if body.status == "cancelled" and row["task_id"] and not (body.detail or "").startswith("could not claim"):
        from . import owner_notice

        if owner_notice.notify(conn, row["task_id"], ctx.actor_id, "cancelled", body.detail or "stopped"):
            conn.commit()
    return out


@router.get("/inbox")
def inbox(run_id: int | None = None, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx)):
    out = agents.check_inbox(conn, ctx.actor_id, run_id=run_id)
    conn.commit()
    return out


@router.get("/files/{file_id}/image")
def file_image(file_id: int, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx),
               settings: Settings = Depends(get_settings)):
    """An image someone attached (a screenshot in chat, a photo in an answer), for the worker to put
    into the run's folder: the model reads it with its Read tool (the MCP tools only give a file's
    text, so a screenshot stayed invisible). Only images the agent itself may read (visibility);
    anything else is 415."""
    from fastapi.responses import FileResponse

    from . import files

    path, meta = files.content(conn, ctx, settings.files_dir, file_id)
    if (meta.get("mime") or "") not in files.INLINE_IMAGES:
        raise HTTPException(415, f"file {file_id} is not an image")
    return FileResponse(path, media_type=meta["mime"],
                        headers={"X-File-Name": quote(meta["name"] or f"file-{file_id}", safe=""),
                                 "X-Content-Type-Options": "nosniff"})


@router.get("/tools")
def my_tools(conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx)):
    """The tools this agent's worker mounts: its personal ones and the shared ones it may use."""
    from . import tools

    out = tools.for_agent(conn, ctx.actor_id)
    conn.commit()
    return out


@router.post("/check-command")
def check_command(body: CommandIn, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx)):
    """Before an agent shell command. The constitution's guard first: NEEDS_OWNER (irreversible, or
    destructive on outside content) becomes a task for the owner. Of what it allows, the command policy
    (pos.command_policy) auto-allows engineering work inside the agent's worktree ("auto": the hook lets
    it past the CLI's allow-list) and sends pushes and network writes to the CTO."""
    from . import command_policy
    from .guard import commands
    from .integrations import guard_actor

    d = commands.evaluate(body.command, guard_actor(conn, ctx.actor_id),
                          commands.Trigger.EXTERNAL if body.external else commands.Trigger.MEMBER)
    out = {"outcome": d.outcome.value, "rule": d.rule, "reason": d.reason}
    if d.outcome.value == "allow" and not actors.get(conn, ctx.actor_id)["is_owner"]:
        rctx = Ctx(ctx.actor_id, via="worker", run_id=body.run_id)
        out = {**out, **command_policy.check(conn, rctx, body.command, body.cwd, body.workdir)}
        _record_denial(conn, ctx, body, out)
        conn.commit()
        return out
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
    _record_denial(conn, ctx, body, out)
    conn.commit()
    return out


def _program(command: str) -> str:
    """The program a shell command runs (its first word after VAR=value prefixes, without the path)."""
    for word in command.split():
        if "=" not in word.split("/")[0]:
            return word.rsplit("/", 1)[-1][:40]
    return ""


def _record_denial(conn, ctx: Ctx, body: CommandIn, out: dict) -> None:
    """A shell command the agent may not run, in the audit log (`command_denied`: the self-improvement
    digest counts them, pos.improve.signals): the guard's deny, a task for the owner or the CTO, or a
    command the guard allows but the CLI's allow-list would refuse (the hook denies it then, with the
    hint to ask the CTO)."""
    from . import audit
    from .observability import redact

    outcome = out.get("outcome")
    if outcome == "allow":
        if out.get("auto") or body.cli_allowed is not False:
            return
        outcome = "cli_denied"
    audit.log(conn, Ctx(ctx.actor_id, via="worker", run_id=body.run_id), "command_denied", "actor", ctx.actor_id,
              outcome=outcome, rule=out.get("rule"), program=_program(body.command),
              command=redact(body.command, 200), task=out.get("owner_task") or out.get("cto_task"))


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
    from . import browser

    st = _state(conn, ctx.actor_id)
    if st["frozen"] or st["paused"] or st["archived"]:
        return {"decision": "refuse", "reason": "the kill switch is on or this agent is paused"}
    if str(body.get("tool") or "").startswith("computer_"):
        if not browser.may_use_computer(conn, ctx.actor_id):
            return {"decision": "refuse", "reason": "this agent lacks tool:computer"}
    elif not browser.may_browse(conn, ctx.actor_id):
        return {"decision": "refuse", "reason": "this agent lacks tool:browser (or browser:use)"}
    return browser.check(conn, ctx, settings.data_dir, body)


@router.get("/browser/policy")
def browser_policy(conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx)):
    """What the guard needs at start: the agent's action hosts and its persistent profiles."""
    from . import agents, browser

    have = agents.permissions_of(conn, ctx.actor_id)
    return {"browser": browser.may_browse(conn, ctx.actor_id), "computer": browser.may_use_computer(conn, ctx.actor_id),
            "action_hosts": browser.action_hosts(conn, ctx.actor_id),
            "profile": browser.may_keep_profile(conn, ctx.actor_id),
            "profiles": sorted(p.split(":", 2)[2] for p in have if p.startswith("scope:browser-profile:"))}


def _live_run(conn, ctx: Ctx, run_id) -> int:
    """This agent's running run (the guard's frames and saved logins belong to one)."""
    try:
        rid = int(run_id or 0)
    except (TypeError, ValueError):
        rid = 0
    row = conn.execute("SELECT actor_id, status FROM runs WHERE id = ?", (rid,)).fetchone() if rid else None
    if row is None or row["actor_id"] != ctx.actor_id or row["status"] != "running":
        raise HTTPException(403, "not a running run of this agent")
    return rid


@router.get("/browser/profile")
def browser_profile_get(run_id: int = 0, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx),
                        settings: Settings = Depends(get_settings)):
    """The agent's kept logins (browser:profile) for its browser at the start of a run; only to the
    guard of a running run, decrypted here (the key never leaves PersonalOS)."""
    from . import browser

    if not browser.may_keep_profile(conn, ctx.actor_id):
        raise HTTPException(403, "this agent keeps no browser profile (browser:profile)")
    _live_run(conn, ctx, run_id)
    return {"state": browser.load_profile(settings.data_dir, settings.session_secret, ctx.actor_id)}


@router.put("/browser/profile")
def browser_profile_put(body: dict, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx),
                        settings: Settings = Depends(get_settings)):
    from . import browser

    if not browser.may_keep_profile(conn, ctx.actor_id):
        raise HTTPException(403, "this agent keeps no browser profile (browser:profile)")
    _live_run(conn, ctx, body.get("run_id"))
    state = body.get("state")
    if not isinstance(state, dict):
        raise HTTPException(422, "state: Playwright's storage state (cookies, origins)")
    try:
        size = browser.save_profile(settings.data_dir, settings.session_secret, ctx.actor_id,
                                    {"cookies": state.get("cookies") or [], "origins": state.get("origins") or []})
    except ValueError as e:
        raise HTTPException(413, str(e)) from e
    return {"ok": True, "bytes": size}


@router.get("/browser/live")
def browser_live_watching(run_id: int = 0, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx),
                          settings: Settings = Depends(get_settings)):
    """Whether the owner has the run page open: the guard sends frames only then."""
    from . import browser

    return {"watching": browser.watching(settings.data_dir, _live_run(conn, ctx, run_id))}


@router.post("/browser/live")
def browser_live_frame(body: dict, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx),
                       settings: Settings = Depends(get_settings)):
    from . import browser

    rid = _live_run(conn, ctx, body.get("run_id"))
    ok = browser.save_live(settings.data_dir, rid, body.get("frame"),
                           {"url": body.get("url"), "kind": str(body.get("kind") or "browser")[:10]})
    return {"ok": ok, "watching": browser.watching(settings.data_dir, rid)}


@router.post("/browser/credential")
def browser_credential(body: dict, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx),
                       x_pos_cred_session: str = Header(default="")):
    """browser_login: one credential value for the guard to fill into a form field on the page's host.
    Needs the run's credential session (only the worker's MCP servers hold it), a grant for the
    credential and the page's host among the credential's allowed hosts. Logged like every use."""
    from urllib.parse import urlparse

    from . import browser
    from .credentials import service as creds

    if not browser.may_browse(conn, ctx.actor_id):
        raise HTTPException(403, "this agent lacks tool:browser")
    try:
        s = creds.check_session(conn, ctx, int(body.get("run_id") or 0), x_pos_cred_session)
    except Forbidden as e:
        raise HTTPException(403, str(e)) from e
    u = urlparse(str(body.get("url") or ""))
    if u.scheme not in ("https", "http") or not u.hostname:
        raise HTTPException(422, "open the login page first (an http(s) URL)")
    try:
        port = u.port
    except ValueError:
        port = None
    host = f"{u.hostname.lower()}:{port}" if port else u.hostname.lower()
    if u.scheme == "http" and not creds.private_host(u.hostname):
        raise HTTPException(403, "a credential goes only over HTTPS (plain HTTP only on the local network)")
    try:
        values = creds.resolve_for(conn, ctx, [str(body.get("name") or "")], "browser", host=host,
                                   run_id=s["run_id"], task_id=s["task_id"])
    except creds.CredentialError as e:
        raise HTTPException(403, str(e)) from e
    (name, v), = values.items()
    return {"name": name, "value": v["value"]}


@router.post("/browser/log")
def browser_log(body: dict, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx),
                settings: Settings = Depends(get_settings)):
    from . import browser, taint

    out = browser.record(conn, ctx, settings.data_dir, body)
    if taint.mark_browser(conn, ctx.actor_id, body.get("url"), browser._run_ctx(conn, ctx, body.get("run_id")).run_id):
        conn.commit()  # a web page read in this run taints it (pos.taint)
    return out


@router.get("/approvals/{approval_id}")
def approval_state(approval_id: int, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx)):
    a = approvals.get(conn, approval_id)
    if a["requested_by"] != ctx.actor_id:
        raise HTTPException(404, "not your approval")
    return {"id": a["id"], "status": a["status"], "comment": a.get("comment")}
