"""Every agent's own memory: one pinned Markdown note it reads at the start of
every run and keeps up to date itself (memory_get / memory_update).

It lives in the `memories` table (one active row per agent, versioned like
notes, so every change can be seen and restored), is shown on the agent's
page, and the worker puts it into the task prompt (`/api/worker/me`). It is
capped (MAX_CHARS) so it stays cheap on every run: facts that save the next
run from exploring again, not a diary.
"""

import sqlite3

from . import actors, versioning
from .core import Ctx, Forbidden, now_iso

MAX_CHARS = 8000
ENTITY = "memory"


class MemoryError(ValueError):
    """A write that is refused; the message is safe to show an agent."""


def _row(conn: sqlite3.Connection, actor_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM memories WHERE actor_id = ? AND archived_at IS NULL ORDER BY id DESC LIMIT 1",
                        (actor_id,)).fetchone()


def get(conn: sqlite3.Connection, actor_id: int) -> dict:
    r = _row(conn, actor_id)
    body = r["body"] if r else ""
    return {"body": body, "chars": len(body), "max_chars": MAX_CHARS, "updated_at": r["updated_at"] if r else None}


def text(conn: sqlite3.Connection, actor_id: int) -> str:
    return get(conn, actor_id)["body"]


def set_body(conn: sqlite3.Connection, ctx: Ctx, body: str, agent_id: int | None = None,
             system: bool = False) -> dict:
    """Replace the memory (the whole text). An agent writes its own; the owner any agent's; the
    platform (`system`, pos.learning: lessons and the starter note) any agent's."""
    target = agent_id if agent_id is not None else ctx.actor_id
    if target != ctx.actor_id and not system and not actors.get(conn, ctx.actor_id)["is_owner"]:
        raise Forbidden("an agent's memory is its own: only it and the owner change it")
    from . import pseudo_tools

    body = pseudo_tools.clean((body or "").strip(), marker=False)  # tool calls written as text never ran
    if len(body) > MAX_CHARS:
        raise MemoryError(f"memory is {len(body)} characters, the limit is {MAX_CHARS}: keep the facts, drop the "
                          "history (logs and change records belong in notes)")
    r = _row(conn, target)
    if r is None:
        now = now_iso()
        versioning.insert(conn, ctx, ENTITY, {"actor_id": target, "body": body, "visibility": "team",
                                              "owner_id": actors.owner_id(conn), "created_at": now,
                                              "updated_at": now})
    else:
        versioning.update(conn, ctx, ENTITY, r["id"], {"body": body})
    conn.commit()
    return get(conn, target)


def register_mcp(mcp, session) -> None:
    from mcp.server.mcpserver import Context

    @mcp.tool(description="Your own memory: one pinned Markdown note that is put into your prompt at the start "
                          "of every run. Returns its text.")
    def memory_get(ctx: Context) -> dict:
        with session(ctx, "memory_get") as (conn, c):
            return get(conn, c.actor_id)

    @mcp.tool(description=f"Replace your memory (the whole Markdown text, max {MAX_CHARS} characters). Keep what "
                          "the next run needs so it does not explore again: facts, hosts, paths, versions, "
                          "inventories, known problems, decisions. Versioned; read it with memory_get first and "
                          "send the full new text.")
    def memory_update(ctx: Context, body: str) -> dict:
        with session(ctx, "memory_update", chars=len(body or "")) as (conn, c):
            try:
                return set_body(conn, c, body)
            except MemoryError as e:
                raise Forbidden(str(e)) from None
