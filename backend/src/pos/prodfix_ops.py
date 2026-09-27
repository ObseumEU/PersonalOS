"""One-off production fix (2026-09-27): what the incident, access-watch and reorganisation bugs
left behind in the data. The code fixes stop new cases; this cleans up the old ones.

    python -m pos.prodfix_ops              # dry run: what would change (nothing is written)
    python -m pos.prodfix_ops --apply      # do it

Idempotent (a second run finds nothing left). Nothing is deleted; every change is versioned
and audit-logged as the owner ("via prodfix").

1. reviews   every task waiting for review: a review held by an archived member moves to its
             successor, and the current reviewer is told again (DM + wake). The reorganisation
             moved T-070/071/072/075/092/105 with a raw UPDATE, so nobody was told.
2. grants    archived members keep no access: their active grants end (51 were left).
3. tasks     open tasks of archived members go to their successors (docs/REORG.md: Dev agent ->
             Software Engineer, …; else the lead, else the owner). T-026 was reopened onto the
             archived Dev agent.
4. incidents incidents already resolved whose task or ticket is still open close the way a
             resolution closes them now (tickets 135, 141, 153 and the owner asks behind them).
5. chat      unanswered-chat incidents whose agent has answered since (or that went to a
             service) resolve (#26, #31-35).
"""

import argparse
import json
import sqlite3

from . import actors, agents, audit, tasks
from .core import Ctx


def _ctx(conn: sqlite3.Connection) -> Ctx:
    return Ctx(actors.owner_id(conn), via="prodfix")


def step_reviews(conn: sqlite3.Connection, ctx: Ctx, apply: bool) -> list[str]:
    out = []
    for t in conn.execute("SELECT * FROM tasks WHERE status = 'review' AND archived_at IS NULL ORDER BY id").fetchall():
        ref = tasks.display_id(t["id"])
        rid = tasks.reviewer_of(conn, t)
        if actors.get(conn, rid)["archived_at"]:
            new = agents.successor_of(conn, rid)
            out.append(f"{ref}: review {actors.get(conn, rid)['name']} -> {actors.get(conn, new)['name']}")
            if apply:
                tasks.hand_review(conn, ctx, t["id"], new, "převzato po archivovaném členovi")
            continue
        r = actors.get(conn, rid)
        if r["is_owner"]:
            continue  # the owner sees Needs review on the board
        if not apply:
            out.append(f"{ref}: remind {r['name']}")
        elif tasks.notify_reviewer(conn, ctx, t["id"], "připomenutí: čeká od reorganizace"):
            out.append(f"{ref}: reminded {r['name']}")
    return out


def step_grants(conn: sqlite3.Connection, ctx: Ctx, apply: bool) -> list[str]:
    from .access import store as access_store

    if not access_store.ready(conn):
        return []
    out = []
    for a in conn.execute("""SELECT a.id, a.name, COUNT(g.id) AS n FROM actors a JOIN access_grants g
                             ON g.agent_id = a.id AND g.ended_at IS NULL WHERE a.archived_at IS NOT NULL
                             GROUP BY a.id ORDER BY a.id""").fetchall():
        out.append(f"{a['name']}: end {a['n']} grant(s)")
        if apply:
            agents.end_grants(conn, ctx, a["id"], f"{a['name']} je archivovaný")
    return out


def step_tasks(conn: sqlite3.Connection, ctx: Ctx, apply: bool) -> list[str]:
    from . import reassign, versioning

    out = []
    for t in conn.execute("""SELECT t.id, t.status, a.id AS aid, a.name FROM tasks t JOIN actors a ON a.id = t.assignee_id
                             WHERE a.archived_at IS NOT NULL AND t.archived_at IS NULL
                               AND t.status NOT IN ('done', 'someday') ORDER BY t.id""").fetchall():
        new = agents.successor_of(conn, t["aid"])
        out.append(f"{tasks.display_id(t['id'])}: {t['name']} -> {actors.get(conn, new)['name']}")
        if not apply:
            continue
        note = f"{t['name']} je archivovaný; práci převzal nástupce (docs/REORG.md)."
        try:
            reassign.reassign(conn, ctx, t["id"], new, note, force=True)
        except tasks.Invalid:
            cols = tasks.resolve_assignee(conn, ctx, {"type": actors.get(conn, new)["kind"], "id": new})
            versioning.update(conn, ctx, tasks.ENTITY, t["id"], cols, action="assign")
        # a result waiting for review keeps its state (the successor must not redo it)
        if t["status"] not in ("next", "working") and conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (t["id"],)).fetchone()["status"] != t["status"]:
            versioning.update(conn, ctx, tasks.ENTITY, t["id"], {"status": t["status"]}, action="prodfix")
    return out


def step_incidents(conn: sqlite3.Connection, ctx: Ctx, apply: bool) -> list[str]:
    from . import monitor

    monitor.ensure_schema(conn)
    mctx = Ctx(monitor.monitor_id(conn) or ctx.actor_id, via="prodfix")
    out = []
    rows = conn.execute("""SELECT s.* FROM sentinel_incidents s JOIN tasks t ON t.id = COALESCE(s.task_id, s.ticket_id)
                           WHERE s.resolved_at IS NOT NULL AND s.dup_of IS NULL AND t.status != 'done'
                             AND t.archived_at IS NULL ORDER BY s.id""").fetchall()
    for r in rows:
        if r["task_id"]:
            t = conn.execute("SELECT status FROM tasks WHERE id = ?", (r["task_id"],)).fetchone()
            if t["status"] != "next" or monitor._runs_on(conn, r["task_id"]):
                continue  # the Hlídač works on it: its own verdict closes it
            out.append(f"{tasks.display_id(r['task_id'])}: close (incident {r['incident_id']} resolved before triage)")
            if apply:
                tasks.complete(conn, mctx, r["task_id"], "### Výsledek\nVyřešilo se samo před tříděním. "
                                                         "**Třída:** transient. Bez běhu modelu.")
                conn.execute("UPDATE sentinel_incidents SET status = 'closed', classification = 'transient', "
                             "closed_at = ?, summary = 'resolved before triage' WHERE id = ?",
                             (r["resolved_at"], r["id"]))
        else:
            out.append(f"{tasks.display_id(r['ticket_id'])}: close ticket (incident {r['incident_id']} resolved)")
            if apply:
                monitor.close_ticket(conn, mctx, r, "Incident se mezitím vyřešil sám (oprava 2026-09-27).")
    return out


def step_chat(conn: sqlite3.Connection, ctx: Ctx, apply: bool) -> list[str]:
    from . import workers

    if not apply:
        n = conn.execute("""SELECT COUNT(*) FROM audit_log l WHERE l.action = 'chat_unanswered' AND NOT EXISTS (
                              SELECT 1 FROM audit_log r WHERE r.action = 'chat_unanswered_resolved'
                              AND r.entity_id = l.entity_id)""").fetchone()[0]
        return [f"{n} unanswered-chat incident(s) to check"] if n else []
    return [f"message {d['message']} ({d['agent']}): resolved, {d['why']}" for d in workers.resolve_answered(conn)]


STEPS = (("reviews", step_reviews), ("grants", step_grants), ("tasks", step_tasks),
         ("incidents", step_incidents), ("chat", step_chat))


def run(conn: sqlite3.Connection, apply: bool = False) -> dict:
    ctx = _ctx(conn)
    report = {}
    for key, fn in STEPS:
        report[key] = fn(conn, ctx, apply)
        if apply:
            conn.commit()
    if apply:
        audit.log(conn, ctx, "prodfix_ops", None, None, **{k: len(v) for k, v in report.items()})
        conn.commit()
    else:
        conn.rollback()
    return report


def main() -> None:
    from .config import get_settings
    from .db import connect

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write the changes (default: a dry run)")
    a = ap.parse_args()
    conn = connect(get_settings().db_path)
    print(json.dumps(run(conn, apply=a.apply), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
