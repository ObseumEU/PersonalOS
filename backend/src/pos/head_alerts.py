"""Heads see their team's stuck work (T-417).

`stuck`: the read-only list behind the `stuck_tasks` MCP tool. A team's task is
stuck when it is `working`/`next` and nothing moved on it for `hours` (a task
change, a comment, a run), or it is held back (`retry_after`), or its last run
failed or was stopped by the budget. Tasks of the team that ended up with the
owner (not `ask_owner` tickets) are listed as `owner_assigned`.

`run_failed` / `owner_assigned`: a Czech DM to the agent's head (its lead in the
org chart, never the owner) when a run failed or was stopped by the budget, or
when the agent gave a task to the owner. At most one per (agent, task, reason)
within DEDUP_HOURS (the audit log is the memory). The kill switch, a pause and a
manual stop never alert. Errors are swallowed: the run's finish must not fail.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

from . import actors, audit, chat
from .core import Ctx, Forbidden, NotFound, now_iso

DEDUP_HOURS = 6
PIPELINES = ("system", "support", "runner", "scheduler")  # Ctx.via of platform flows acting for an agent
PIPELINE_SOURCES = ("support:", "taint_hold:")
ALL_SEEING = ("ceo", "coo")  # roles that see every team
OPEN = ("inbox", "next", "working", "waiting")
REASONS = {
    "last_run_failed": "běh skončil chybou",
    "budget_exhausted": "běh zastavil limit rozpočtu",
    "owner_assigned": "úkol předal Ownerovi",
}


def _ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


def _hours_since(at: str | None) -> float | None:
    if not at:
        return None
    t = datetime.fromisoformat(at)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return round((datetime.now(timezone.utc) - t).total_seconds() / 3600, 1)


def team_ids(conn: sqlite3.Connection, head_id: int) -> set[int]:
    """Everyone below `head_id` in the org chart (reports_to, recursively)."""
    rows = conn.execute("SELECT id, reports_to FROM actors WHERE archived_at IS NULL").fetchall()
    below: dict[int, list[int]] = {}
    for r in rows:
        below.setdefault(r["reports_to"], []).append(r["id"])
    out: set[int] = set()
    todo = list(below.get(head_id, []))
    while todo:
        aid = todo.pop()
        if aid not in out and aid != head_id:
            out.add(aid)
            todo += below.get(aid, [])
    return out


def sees_all(conn: sqlite3.Connection, actor_id: int) -> bool:
    a = actors.get(conn, actor_id)
    return bool(a["is_owner"]) or (a["role"] or "").lower() in ALL_SEEING


def lead_of(conn: sqlite3.Connection, agent_id: int) -> int | None:
    """The agent's head: its lead in the org chart, unless that is the owner or a person gone."""
    a = actors.get(conn, agent_id)
    lead = a["reports_to"]
    if not lead or lead == agent_id:
        return None
    row = conn.execute("SELECT is_owner, archived_at FROM actors WHERE id = ?", (lead,)).fetchone()
    return None if row is None or row["is_owner"] or row["archived_at"] else lead


def run_reason(status: str | None, detail: str | None) -> str | None:
    """last_run_failed / budget_exhausted for a run that ended badly; None otherwise
    (ok, still running, cancelled by hand, kill switch, pause)."""
    from .pseudo_tools import MARKER

    if MARKER in (detail or ""):
        return None  # the platform already told the lead itself ("[platforma]", pos.pseudo_tools)
    detail = (detail or "").strip().lower()
    if status == "blocked":
        return "budget_exhausted" if detail.startswith(("budget", "company cap")) else None
    if status == "error":
        return "budget_exhausted" if "budget" in detail[:80] else "last_run_failed"
    return None


def _last_runs(conn: sqlite3.Connection, ids: list[int]) -> dict[int, sqlite3.Row]:
    if not ids:
        return {}
    q = ",".join("?" * len(ids))
    rows = conn.execute(f"""SELECT r.* FROM runs r JOIN (SELECT task_id, MAX(id) AS id FROM runs
                            WHERE task_id IN ({q}) GROUP BY task_id) m ON m.id = r.id""", ids).fetchall()
    return {r["task_id"]: r for r in rows}


def _moved(conn: sqlite3.Connection, ids: list[int]) -> dict[int, str]:
    """The latest movement per task: a comment (progress included) or a run's start, end or heartbeat."""
    if not ids:
        return {}
    q = ",".join("?" * len(ids))
    out: dict[int, str] = {}
    for sql in (f"SELECT task_id, MAX(created_at) AS at FROM task_comments WHERE task_id IN ({q}) GROUP BY task_id",
                f"""SELECT task_id, MAX(MAX(started_at, COALESCE(ended_at, ''), COALESCE(heartbeat_at, '')))
                    AS at FROM runs WHERE task_id IN ({q}) GROUP BY task_id"""):
        for r in conn.execute(sql, ids).fetchall():
            if r["at"] and r["at"] > out.get(r["task_id"], ""):
                out[r["task_id"]] = r["at"]
    return out


def stuck(conn: sqlite3.Connection, caller_id: int, team: str | None = None, hours: float = 6,
          include_owner_assigned: bool = True) -> dict:
    """The stuck tasks of `team` (a head's name; default the caller): see the module doc.
    A head sees only its own team; the owner, CEO and COO see every team."""
    from .tasks import display_id

    head = actors.get(conn, caller_id)
    if team and team.strip().lower() not in ("me", head["name"].lower()):
        found = actors.find_by_name(conn, team.strip())
        if found is None:
            raise NotFound(f"no member called {team!r}")
        head = found
    if head["id"] != caller_id and not sees_all(conn, caller_id):
        from . import org

        if not org.manages(conn, caller_id, head["id"]):
            raise Forbidden(f"{head['name']} is not in your team: you see only your own team")
    members = team_ids(conn, head["id"])
    if not members:
        return {"team": head["name"], "members": 0, "hours": hours, "stuck": []}
    q = ",".join("?" * len(members))
    rows = conn.execute(f"""SELECT * FROM tasks WHERE archived_at IS NULL AND assignee_id IN ({q})
                            AND status IN ('working', 'next')""", list(members)).fetchall()
    owner = actors.owner_id(conn)
    if include_owner_assigned:
        # The team's tasks now with the owner: created by a member, or given to him by one (audit below).
        given = [r["entity_id"] for r in conn.execute(
            f"""SELECT DISTINCT entity_id FROM audit_log WHERE action = 'head_alert' AND entity = 'task'
                AND json_extract(detail, '$.reason') = 'owner_assigned'
                AND json_extract(detail, '$.agent_id') IN ({q})""", list(members)).fetchall()]
        rows += conn.execute(
            f"""SELECT * FROM tasks WHERE archived_at IS NULL AND assignee_id = ? AND status IN
                ({",".join("?" * len(OPEN))}) AND COALESCE(source, '') != 'ask_owner'
                AND (created_by IN ({q}) OR id IN ({",".join("?" * len(given)) or "NULL"}))""",
            [owner, *OPEN, *members, *given]).fetchall()
    ids = [r["id"] for r in rows]
    last, moved = _last_runs(conn, ids), _moved(conn, ids)
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM actors").fetchall()}
    cutoff, now = _ago(hours), now_iso()
    out, seen = [], set()
    for t in rows:
        if t["id"] in seen:
            continue
        seen.add(t["id"])
        run = last.get(t["id"])
        at = max(t["updated_at"] or "", moved.get(t["id"], ""))
        rr = run_reason(run["status"], run["detail"]) if run else None
        if t["assignee_id"] == owner:
            reason = "owner_assigned"
        elif rr:
            reason = rr
        elif t["retry_after"] and t["retry_after"] > now:
            reason = "retry_after"
        elif at < cutoff:
            reason = "no_activity"
        else:
            continue
        out.append({
            "ref": display_id(t["id"]), "title": t["title"], "assignee": names.get(t["assignee_id"]),
            "status": t["status"], "updated_at": t["updated_at"], "hours_idle": _hours_since(at or None),
            "retry_after": t["retry_after"],
            "last_run": ({"at": run["ended_at"] or run["started_at"],
                          "result": {"budget_exhausted": "budget", "last_run_failed": "error"}.get(rr, run["status"])}
                         if run else None),
            "reason": reason,
        })
    out.sort(key=lambda x: -(x["hours_idle"] or 0))
    return {"team": head["name"], "members": len(members), "hours": hours, "stuck": out}


def _recent(conn: sqlite3.Connection, agent_id: int, task_id: int, reason: str) -> bool:
    return conn.execute("""SELECT 1 FROM audit_log WHERE action = 'head_alert' AND entity = 'task'
                           AND entity_id = ? AND json_extract(detail, '$.agent_id') = ?
                           AND json_extract(detail, '$.reason') = ? AND at >= ?""",
                        (task_id, agent_id, reason, _ago(DEDUP_HOURS))).fetchone() is not None


def _body(conn: sqlite3.Connection, agent_id: int, task_id: int, reason: str, why: str) -> str:
    from .tasks import display_id

    t = conn.execute("SELECT title, retry_after FROM tasks WHERE id = ?", (task_id,)).fetchone()
    ref, agent = display_id(task_id), actors.get(conn, agent_id)["name"]
    lines = [f"Upozornění pro vedoucího: {agent} u {ref} „{t['title']}“ – {REASONS[reason]}."]
    if why.strip():
        lines.append(f"Důvod: {why.strip()[:400]}")
    if reason == "owner_assigned":
        lines.append(f"Co můžeš udělat: zkontroluj, jestli {ref} opravdu potřebuje Ownera. Když ne, převezmi ho "
                     "nebo ho předej členovi týmu (task_reassign); otázku pro Ownera patří přes ask_owner.")
    else:
        if t["retry_after"]:
            lines.append(f"Další automatický pokus nejdřív: {t['retry_after']}.")
        act = ("požádej o vyšší limit (request_access) nebo úkol předej jinému agentovi"
               if reason == "budget_exhausted" else
               "podívej se na běh v trace, úkol upřesni komentářem a vrať, nebo ho předej jinému agentovi")
        lines.append(f"Co můžeš udělat: {act}. Přehled týmu: stuck_tasks.")
    return "\n".join(lines)


def notify(conn: sqlite3.Connection, agent_id: int, task_id: int | None, reason: str, why: str = "") -> dict | None:
    """DM the agent's head (see the module doc). Returns the message, or None. The caller commits."""
    if not task_id or reason not in REASONS:
        return None
    try:
        if actors.get(conn, agent_id)["kind"] == "human":
            return None
        lead = lead_of(conn, agent_id)
        if lead is None or _recent(conn, agent_id, task_id, reason):
            return None
        ctx = Ctx(agent_id, via="system")
        out = chat.send_dm(conn, ctx, lead, _body(conn, agent_id, task_id, reason, why),
                           attachments=[{"type": "task", "id": task_id}], system=True)
        audit.log(conn, ctx, "head_alert", "task", task_id, agent_id=agent_id, reason=reason, lead_id=lead,
                  message=out.get("id"))
        return out
    except Exception:  # noqa: BLE001 - the alert must never break a run's finish or an assignment
        return None


def run_ended(conn: sqlite3.Connection, agent_id: int, task_id: int | None, status: str,
              detail: str = "") -> dict | None:
    reason = run_reason(status, detail)
    return notify(conn, agent_id, task_id, reason, detail) if reason else None


def assigned(conn: sqlite3.Connection, ctx: Ctx, task_id: int, before_assignee: int | None = None) -> None:
    """An agent gave a task to the owner (created or reassigned, not an ask_owner ticket): tell its head."""
    try:
        t = conn.execute("SELECT assignee_id, source FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if t is None or t["source"] == "ask_owner" or t["assignee_id"] != actors.owner_id(conn) \
                or before_assignee == t["assignee_id"]:
            return
        if actors.get(conn, ctx.actor_id)["kind"] == "human":
            return
        # A platform pipeline's item for the owner (a support draft's "Čeká na tebe", a security hold) is
        # not the agent's choice: no alert (the support flow promises no chat ping).
        if ctx.via in PIPELINES or (t["source"] or "").startswith(PIPELINE_SOURCES):
            return
        notify(conn, ctx.actor_id, task_id, "owner_assigned")
    except Exception:  # noqa: BLE001
        return
