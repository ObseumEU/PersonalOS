"""Wires the independent modules (pos.budget, pos.guard, pos.hr) into the core.

They never import the core; the core calls them here, through the runner
hooks and small adapters. `install()` runs once at startup.
"""

import sqlite3
from dataclasses import dataclass

from . import actors, audit, runner, tasks
from .budget import service as budget
from .core import Ctx
from .guard import policy as guard_policy
from .guard import prompt as guard_prompt
from .hr import mcp as hr_mcp
from .hr import service as hr

_installed = False


@dataclass(frozen=True)
class GuardActor:
    """The shape pos.guard expects: a string id and whether it is the owner."""

    id: str
    is_owner: bool


def guard_actor(conn: sqlite3.Connection, actor_id: int) -> GuardActor:
    return GuardActor(str(actor_id), bool(actors.get(conn, actor_id)["is_owner"]))


def check_visibility_change(conn: sqlite3.Connection, ctx: Ctx, row, new: str) -> None:
    """Constitution U6: private data only widens with its owner's consent."""
    guard_policy.check_visibility_change(
        guard_actor(conn, ctx.actor_id), data_owner_id=str(row["owner_id"]),
        current=row["visibility"], new=new,
    ).raise_if_not_allowed()


def _budget_gate(conn: sqlite3.Connection, req: runner.RunRequest) -> None:
    """Each runtime has its own subscription: ask the one this run uses."""
    from . import engines

    ok, why = engines.can_run(conn, req.engine, req.actor_id)
    if not ok:
        raise runner.RunBlocked(f"budget: {why}")


def _constitution_digest(conn: sqlite3.Connection, req: runner.RunRequest) -> None:
    audit.log(conn, Ctx(req.actor_id, via="runner"), "run_constitution", "task" if req.task_id else None,
              req.task_id, sha256=guard_prompt.constitution_digest())


def _guardrails(conn: sqlite3.Connection, req: runner.RunRequest) -> None:
    """Every codex exec run starts with the constitution and guardrails (spec 5.2)."""
    req.prompt = guard_prompt.with_guardrails(req.prompt)


def _budget_record(conn: sqlite3.Connection, run_row: sqlite3.Row, jsonl: str) -> None:
    if jsonl and "engine" in run_row.keys() and run_row["engine"] == "claude":
        from . import engines

        engines.record_claude(conn, run_row, jsonl)
        return
    if jsonl:
        from . import engines

        budget.record_exec(conn, jsonl.splitlines(), agent_id=str(run_row["actor_id"]),
                           task_id=str(run_row["task_id"]) if run_row["task_id"] else None)
        conn.commit()
        engines.record_codex_limit(conn, jsonl)


def install() -> None:
    global _installed
    if _installed:
        return
    runner.before_run(_budget_gate)
    runner.before_run(_constitution_digest)
    runner.before_run(_guardrails)
    runner.after_run(_budget_record)
    from . import killswitch, outbound

    killswitch.install()
    outbound.install()
    _installed = True


def register_builtin_agents(conn: sqlite3.Connection) -> None:
    """The assistant is a system agent for the budget (docs/BUDGET.md); the HR
    agent is a system agent too (docs/HR-AGENT.md)."""
    budget.set_agent_class(conn, str(actors.assistant_id(conn)), "system")
    conn.commit()
    hr.ensure_hr_agent(conn)
    from . import agents, routing

    agents.seed_builtin_permissions(conn)
    routing.seed_defaults(conn)


def register_mcp_tools(mcp, session) -> None:
    """Extra tools on the `pos` MCP server; `session` is its auth + audit helper."""
    hr_mcp.register(mcp, session)


def budget_check(conn: sqlite3.Connection) -> dict:
    """The budget keeper's hourly check, with its actions carried out in the core:
    a level change goes to the audit log, an escalation becomes a task for the owner."""
    report = budget.run_check(conn)
    ctx = Ctx(actors.assistant_id(conn), via="system")
    for action in report.actions:
        if action["type"] == "level_changed":
            audit.log(conn, ctx, "budget_level", None, None, **{"from": action["from"], "to": action["to"]})
        elif action["type"] == "notify_owner":
            tasks.create(conn, ctx, {
                "title": action["title"], "notes": action["body"], "priority": 1,
                "topic": "rozpocet", "assignee": "me",
            })
    conn.commit()
    return {"level": report.level, "actions": report.actions}


async def budget_loop(db_path, interval_min: int) -> None:
    """Runs budget_check every `interval_min` minutes until cancelled (spec 4.2: hourly).
    Stands in for the scheduler until the Nexus one exists."""
    import asyncio
    import logging

    from .db import connect

    while True:
        await asyncio.sleep(interval_min * 60)
        try:
            conn = connect(db_path)
            try:
                await asyncio.to_thread(budget_check, conn)
            finally:
                conn.close()
        except Exception:  # a failed check must not stop the next one
            logging.getLogger(__name__).exception("budget check failed")


async def hr_loop(db_path, interval_min: int = 30) -> None:
    """Runs the HR daily review and weekly report when due (pos.hr.schedule)."""
    import asyncio
    import logging

    from .db import connect
    from .hr import schedule

    while True:
        await asyncio.sleep(interval_min * 60)
        try:
            conn = connect(db_path)
            try:
                await asyncio.to_thread(schedule.run_due, conn)
            finally:
                conn.close()
        except Exception:  # a failed review must not stop the next one
            logging.getLogger(__name__).exception("HR review failed")
