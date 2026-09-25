"""HRDataSource and HRActions over the PersonalOS core (actors, tasks, runs, budget).

Agent ids in pos.hr are the actor id as a string, the same key pos.budget uses.
"""

import sqlite3
from datetime import datetime, timezone

from .. import actors, audit, tasks
from ..budget import store as budget_store
from ..core import Ctx, now_iso
from . import store
from .models import AgentRecord, AgentStats, Lifetime, Status

# Agents that run the platform itself; HR rates them but never retires them.
BUILTIN_SYSTEM = {actors.ASSISTANT_NAME, "Knowledge agent", "Nexus"}


def iso(at: datetime) -> str:
    return budget_store.iso(at)


def parse(value: str | None) -> datetime | None:
    if not value:
        return None
    at = datetime.fromisoformat(value)
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


class CorePlatform:
    """Reads and acts as ``ctx`` (normally the HR agent actor)."""

    def __init__(self, conn: sqlite3.Connection, ctx: Ctx):
        self.conn = conn
        self.ctx = ctx
        store.ensure_schema(conn)
        budget_store.ensure_schema(conn)

    # ------------------------------------------------------------ HRDataSource

    def _last_active(self, actor_id: int, row: sqlite3.Row) -> datetime | None:
        run = self.conn.execute("SELECT MAX(started_at) AS at FROM runs WHERE actor_id = ?", (actor_id,)).fetchone()
        task = self.conn.execute(
            "SELECT MAX(at) AS at FROM history WHERE entity = 'task' AND actor_id = ?", (actor_id,)
        ).fetchone()
        stamps = [parse(v) for v in (row["last_seen_at"], run["at"], task["at"]) if v]
        return max(stamps) if stamps else None

    def list_agents(self) -> list[AgentRecord]:
        profiles = store.profiles(self.conn)
        rows = self.conn.execute("SELECT * FROM actors WHERE kind IN ('ai', 'agent') ORDER BY id").fetchall()
        out = []
        for row in rows:
            p = profiles.get(row["id"])
            out.append(
                AgentRecord(
                    id=str(row["id"]),
                    name=row["name"],
                    purpose=p["purpose"] if p else "",
                    created_by=str(p["created_by"]) if p and p["created_by"] else "",
                    lifetime=Lifetime(p["lifetime"]) if p else Lifetime.LONG_LIVED,
                    created_at=parse(row["created_at"]),
                    status=Status.ARCHIVED if row["archived_at"] else Status.ACTIVE,
                    expires_at=parse(p["expires_at"]) if p else None,
                    last_active_at=self._last_active(row["id"], row),
                    system=bool(p and p["system"]) or row["name"] in BUILTIN_SYSTEM,
                )
            )
        return out

    def agent_stats(self, agent_id: str, since: datetime, until: datetime) -> AgentStats:
        aid, s, u = int(agent_id), iso(since), iso(until)
        q = self.conn.execute
        # Accepted by the owner in the window.
        accepted = q(
            """SELECT COUNT(*) AS n, COALESCE(SUM(interventions = 0 AND returned_count = 0), 0) AS clean
               FROM tasks WHERE assignee_id = ? AND status = 'done' AND completed_at >= ? AND completed_at < ?""",
            (aid, s, u),
        ).fetchone()

        def events(action: str) -> int:
            return q(
                """SELECT COUNT(*) FROM history WHERE entity = 'task' AND action = ? AND at >= ? AND at < ?
                   AND json_extract(data, '$.assignee_id') = ?""",
                (action, s, u, aid),
            ).fetchone()[0]

        returned = events("return")
        failed = q(
            "SELECT COUNT(*) FROM runs WHERE actor_id = ? AND status = 'error' AND started_at >= ? AND started_at < ?",
            (aid, s, u),
        ).fetchone()[0]
        open_ = q(
            """SELECT COUNT(*) FROM tasks WHERE assignee_id = ? AND archived_at IS NULL
               AND status NOT IN ('done', 'review', 'someday')""",
            (aid,),
        ).fetchone()[0]
        tokens = budget_store.tokens_between(self.conn, since, until, agent_id)
        return AgentStats(
            agent_id=agent_id,
            # A returned hand-in was still finished work, just badly done.
            tasks_completed=accepted["n"] + returned,
            tasks_completed_unassisted=accepted["clean"],
            tasks_returned=returned,
            tasks_failed=failed,
            tasks_open=open_,
            owner_interventions=events("intervene"),
            tokens_used=tokens,
        )

    # ------------------------------------------------------------ HRActions

    def archive_agent(self, agent_id: str, reason: str) -> None:
        # Archiving keeps the actor, its history and results (spec 3.3); restore_agent undoes it.
        self.conn.execute("UPDATE actors SET archived_at = ? WHERE id = ? AND archived_at IS NULL",
                          (now_iso(), int(agent_id)))
        audit.log(self.conn, self.ctx, "hr_archive_agent", "actor", int(agent_id), reason=reason)

    def set_lifetime(self, agent_id: str, lifetime: Lifetime, reason: str) -> None:
        store.set_profile(self.conn, int(agent_id), lifetime=lifetime.value)
        audit.log(self.conn, self.ctx, "hr_set_lifetime", "actor", int(agent_id),
                  lifetime=lifetime.value, reason=reason)

    def create_task(self, title: str, body: str, assignee: str, kind: str = "task") -> str:
        fields = {"title": title, "notes": body, "topic": "hr", "assignee": {"type": "agent", "id": int(assignee)}}
        if kind == "read":
            fields["assignee"] = {"type": "human", "id": int(assignee)}
            fields["priority"] = 3
        else:
            fields["priority"] = 2
        # A non-human creator makes work on the owner's behalf (tasks.create).
        return str(tasks.create(self.conn, self.ctx, fields)["id"])


def restore_agent(conn: sqlite3.Connection, ctx: Ctx, agent_id: int) -> None:
    conn.execute("UPDATE actors SET archived_at = NULL WHERE id = ?", (agent_id,))
    audit.log(conn, ctx, "hr_restore_agent", "actor", agent_id)
