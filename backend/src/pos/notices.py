"""Platform notices: who hears that something went wrong, and in whose voice.

A failed run, a budget stop, a guard hold or an unanswered hand-in is the platform's news, not
the owner's and not the agent's. It goes, in Czech and signed "PersonalOS" (pos.actors.system_id),
to the one who can act on it:

- a fault of the platform itself (a worker or engine error, an expired key, a 401, a timeout, the
  CLI missing) → the SRE, else the CTO;
- anything else → the agent's lead in the org chart (never the owner: his place in the chain is
  taken by the CEO).

The owner gets only what needs him, and only through the CEO (the CEO's own judgment, from these
notices). Prod 2026-09/10: platform failures went to the owner in English, under his name or the
CEO's, and he could do nothing with them.
"""

import re
import sqlite3

from . import actors
from .core import Ctx

# What marks a failure as the platform's (not the task's): the agent could not even think.
_PLATFORM = re.compile(
    r"(?i)\b(401|403|unauthori[sz]ed|forbidden|authenticat|api[ _-]?key|token (?:expired|invalid)|"
    r"worker error|worker (?:is )?down|no worker|engine|usage limit|rate[ -]?limit|overloaded|timed? ?out|"
    r"timeout|connection (?:refused|reset|error)|econnrefused|cli (?:not found|missing)|exited with|"
    r"unexpected status|internal server error|\b5\d\d\b|bad gateway|service unavailable|traceback)")


def system_ctx(conn: sqlite3.Connection, run_id: int | None = None) -> Ctx:
    return Ctx(actors.system_id(conn), via="system", run_id=run_id)


def is_platform_fault(why: str | None) -> bool:
    return bool(_PLATFORM.search(why or ""))


def _active(conn: sqlite3.Connection, name: str) -> int | None:
    row = conn.execute("SELECT id FROM actors WHERE name = ? AND archived_at IS NULL", (name,)).fetchone()
    return row["id"] if row else None


def platform_contact(conn: sqlite3.Connection) -> int | None:
    """Who fixes the platform: the SRE, else the CTO, else the CEO."""
    from . import roles

    for name in (roles.SRE, roles.CTO, roles.CEO):
        aid = _active(conn, name)
        if aid:
            return aid
    row = conn.execute("SELECT id FROM actors WHERE role = 'ceo' AND archived_at IS NULL").fetchone()
    return row["id"] if row else None


def ceo(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT id FROM actors WHERE role = 'ceo' AND archived_at IS NULL ORDER BY id").fetchone()
    return row["id"] if row else None


def lead_of(conn: sqlite3.Connection, agent_id: int) -> int | None:
    """The agent's lead, never the owner: an agent that reports to the owner (the CEO) or to nobody
    has the CEO as its lead; the CEO itself has none (None)."""
    seen = set()
    cur = agent_id
    while cur and cur not in seen:
        seen.add(cur)
        row = conn.execute("SELECT reports_to FROM actors WHERE id = ?", (cur,)).fetchone()
        lead = row["reports_to"] if row else None
        if not lead:
            break
        a = actors.get(conn, lead)
        if a["is_owner"]:
            break
        if not a["archived_at"]:
            return lead
        cur = lead
    c = ceo(conn)
    return c if c and c != agent_id else None


def recipient(conn: sqlite3.Connection, agent_id: int, why: str = "") -> int | None:
    """Who gets a notice about this agent's failure (see the module doc)."""
    if is_platform_fault(why):
        aid = platform_contact(conn)
        if aid and aid != agent_id:
            return aid
    return lead_of(conn, agent_id)


def successor(conn: sqlite3.Connection, actor_id: int) -> int | None:
    """Who took over an archived member (docs/REORG.md mapping), else the CEO. An active member is
    its own successor."""
    a = actors.get(conn, actor_id)
    if not a["archived_at"]:
        return actor_id
    from .reorg import SUCCESSORS

    new = SUCCESSORS.get(a["name"])
    if new:
        aid = _active(conn, new)
        if aid:
            return aid
    if a["role"]:  # a newer member with the same role (an agent re-hired under another name)
        row = conn.execute("SELECT id FROM actors WHERE role = ? AND archived_at IS NULL AND kind != 'human' "
                           "AND runtime != 'service' ORDER BY id DESC LIMIT 1", (a["role"],)).fetchone()
        if row:
            return row["id"]
    return ceo(conn)


def dm(conn: sqlite3.Connection, to_actor: int | None, body: str, *, attachments: list[dict] | None = None,
       priority: str = "fyi", wake_it: bool = True) -> dict | None:
    """A DM from PersonalOS (a system message: no rate limit or budget gate). An archived recipient's
    notice goes to its successor. None when there is nobody to tell. The caller commits."""
    from . import chat

    if not to_actor:
        return None
    to_actor = successor(conn, to_actor)
    if not to_actor:
        return None
    ctx = system_ctx(conn)
    out = chat.send_dm(conn, ctx, to_actor, body[:3900], priority=priority, attachments=attachments, system=True)
    if wake_it and actors.get(conn, to_actor)["kind"] != "human":
        from . import wake

        wake.wake(to_actor)
    return out
