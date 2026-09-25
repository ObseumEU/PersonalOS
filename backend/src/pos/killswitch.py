"""The kill switch (AGENTS-SPEC 5.3): one switch that freezes every agent.

- `freeze`: no new runs start, running `codex exec` processes are stopped and
  agents' queues stop (they cannot claim or change tasks). Nothing is deleted.
- `unfreeze`: only the owner, checked through the constitution (pos.guard).

The state lives in the database, so `python -m pos freeze` on the server works
even when the web app is down.
"""

import json
import sqlite3

from . import actors, audit, runner
from .core import Ctx, Forbidden, now_iso

KEY = "freeze"


def state(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT value, updated_at, updated_by FROM system_state WHERE key = ?", (KEY,)).fetchone()
    if row is None:
        return {"frozen": False}
    return {**json.loads(row["value"]), "updated_at": row["updated_at"], "updated_by": row["updated_by"]}


def is_frozen(conn: sqlite3.Connection) -> bool:
    return bool(state(conn).get("frozen"))


def _set(conn: sqlite3.Connection, ctx: Ctx, value: dict) -> None:
    conn.execute(
        """INSERT INTO system_state (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)
           ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at,
           updated_by = excluded.updated_by""",
        (KEY, json.dumps(value, ensure_ascii=False), now_iso(), ctx.actor_id),
    )


def freeze(conn: sqlite3.Connection, ctx: Ctx, reason: str = "") -> dict:
    """Any person may pull the switch; agents may not (they would stop each other)."""
    if actors.get(conn, ctx.actor_id)["kind"] != "human":
        raise Forbidden("only people use the kill switch")
    _set(conn, ctx, {"frozen": True, "reason": reason})
    stopped = runner.cancel_all(conn, f"kill switch: {reason}".strip(": "))
    audit.log(conn, ctx, "freeze", reason=reason, stopped_runs=stopped)
    conn.commit()
    return {**state(conn), "stopped_runs": stopped}


def unfreeze(conn: sqlite3.Connection, ctx: Ctx) -> dict:
    from .integrations import guard_actor
    from .guard import policy

    policy.authorize_change(guard_actor(conn, ctx.actor_id), "kill_switch_off").raise_if_not_allowed()
    if not actors.get(conn, ctx.actor_id)["is_owner"]:
        raise Forbidden("only the owner turns the kill switch off")
    _set(conn, ctx, {"frozen": False})
    audit.log(conn, ctx, "unfreeze")
    conn.commit()
    return state(conn)


def check_agent_may_act(conn: sqlite3.Connection, ctx: Ctx) -> None:
    """Called before agent writes and runs: frozen means agents stand still."""
    if is_frozen(conn) and actors.get(conn, ctx.actor_id)["kind"] != "human":
        raise Forbidden("PersonalOS is frozen (kill switch); agents cannot act until the owner unfreezes it")


def _runner_gate(conn: sqlite3.Connection, req: runner.RunRequest) -> None:
    if is_frozen(conn):
        raise runner.RunBlocked("kill switch is on")
    if actors.get(conn, req.actor_id)["paused_at"]:
        raise runner.RunBlocked("agent is paused")


def install() -> None:
    runner.before_run(_runner_gate)
