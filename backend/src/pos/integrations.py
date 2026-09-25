"""Wires the independent modules (pos.budget, pos.guard) into the core.

They never import the core; the core calls them here, through the runner
hooks and small adapters. `install()` runs once at startup.
"""

import sqlite3
from dataclasses import dataclass

from . import actors, audit, runner
from .budget import service as budget
from .core import Ctx
from .guard import policy as guard_policy
from .guard import prompt as guard_prompt

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
    decision = budget.can_run(conn, str(req.actor_id))
    if not decision.allowed:
        raise runner.RunBlocked(f"budget: {decision.reason}")


def _constitution_digest(conn: sqlite3.Connection, req: runner.RunRequest) -> None:
    audit.log(conn, Ctx(req.actor_id, via="runner"), "run_constitution", "task" if req.task_id else None,
              req.task_id, sha256=guard_prompt.constitution_digest())


def _guardrails(conn: sqlite3.Connection, req: runner.RunRequest) -> None:
    """Every codex exec run starts with the constitution and guardrails (spec 5.2)."""
    req.prompt = guard_prompt.with_guardrails(req.prompt)


def _budget_record(conn: sqlite3.Connection, run_row: sqlite3.Row, jsonl: str) -> None:
    if jsonl:
        budget.record_exec(conn, jsonl.splitlines(), agent_id=str(run_row["actor_id"]),
                           task_id=str(run_row["task_id"]) if run_row["task_id"] else None)
        conn.commit()


def install() -> None:
    global _installed
    if _installed:
        return
    runner.before_run(_budget_gate)
    runner.before_run(_constitution_digest)
    runner.before_run(_guardrails)
    runner.after_run(_budget_record)
    _installed = True


def register_builtin_agents(conn: sqlite3.Connection) -> None:
    """The assistant is a system agent for the budget (docs/BUDGET.md)."""
    budget.set_agent_class(conn, str(actors.assistant_id(conn)), "system")
    conn.commit()
