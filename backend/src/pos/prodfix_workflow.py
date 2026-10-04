"""One-off production fix (2026-10-04): what the work-flow engine bugs left in the data. The code
fixes (pos.review_work, pos.business.review_sla, pos.flood, the picker, ...) stop new cases; this
cleans up the old ones.

    python -m pos.prodfix_workflow              # dry run: applied to an in-memory copy, reported
    python -m pos.prodfix_workflow --apply      # do it

Idempotent (a second run finds nothing left). Nothing is deleted; every change is versioned and
audit-logged in the platform's voice (the Executive Assistant, "via prodfix"), never the owner's.

1. routines  pause the 16 LLM "Hlídání výpadků" routines and the COO's "Druhá pojistka" (schedules
             #27-#43): pos.head_alerts.sweep does it in code now (~$117 a month); their open tasks
             that nobody started are closed with a note.
2. owner     open tasks agents gave the owner go to the CEO (a chat loop task to the COO), queued
             again, with a comment and one message to each new assignee.
3. t232      the CEO's daily report schedule (#45) was merged into T-232 (a quota alert) by the
             escalation dedup: the schedule no longer points at T-232 (it fires at its next slot)
             and T-232 says which two items were wrongly linked to it.
4. reviews   the review backlog: the review policy runs over it (low risk accepted, routine checks
             whose findings are tracked accepted, code to the QA Reviewer), the SLA moves what waited
             over 24 h, and every result left for an agent reviewer becomes a "Review: T-x" task in
             that reviewer's queue (pos.review_work).
"""

import argparse
import json
import re
import sqlite3

from . import actors, audit, tasks, versioning
from .core import Ctx

WATCH_RE = re.compile(r"hlídání výpadků|druhá pojistka", re.IGNORECASE)
WATCH_IDS = range(27, 44)  # schedules #27-#43 in prod
CEO_REPORT_SCHEDULE = 45
T232 = 232


def _ctx(conn: sqlite3.Connection) -> Ctx:
    from .business import system_ctx

    return Ctx(system_ctx(conn).actor_id, via="prodfix")


def step_routines(conn: sqlite3.Connection, ctx: Ctx, apply: bool) -> list[str]:
    from . import comments, schedules

    out = []
    for s in conn.execute("SELECT * FROM schedules WHERE archived_at IS NULL AND status = 'active' ORDER BY id"):
        if s["id"] not in WATCH_IDS or not WATCH_RE.search(s["name"] or ""):
            continue
        out.append(f"schedule #{s['id']} {s['name']}: pause")
        if apply:
            versioning.update(conn, ctx, schedules.ENTITY, s["id"], {"status": "paused"}, action="pause")
            audit.log(conn, ctx, "schedule_superseded", schedules.ENTITY, s["id"], by="pos.head_alerts.sweep")
        for t in conn.execute("SELECT id, title FROM tasks WHERE source = ? AND status IN ('inbox', 'next') "
                              "AND archived_at IS NULL", (f"schedule:{s['id']}",)).fetchall():
            out.append(f"  {tasks.display_id(t['id'])}: close (superseded)")
            if apply:
                versioning.update(conn, ctx, tasks.ENTITY, t["id"], {"status": "done", "progress": 100,
                                                                     "completed_at": _now()}, action="close")
                comments.log(conn, ctx, t["id"], "Closed: the routine was replaced by code (pos.head_alerts.sweep "
                                                 "sends every head its team's stuck work at 09:00 and 15:00).",
                             "system")
    return out


def _now() -> str:
    from .core import now_iso

    return now_iso()


def step_owner(conn: sqlite3.Connection, ctx: Ctx, apply: bool) -> list[str]:
    """Open tasks with the owner that he did not create for himself."""
    from . import chat, comments, wake
    from .business import ceo_id

    owner = actors.owner_id(conn)
    ceo = ceo_id(conn)
    coo = chat.role_member(conn, "project_manager") or ceo
    if not ceo:
        return ["no CEO: nothing moved"]
    out, told = [], {}
    rows = conn.execute("""SELECT t.* FROM tasks t LEFT JOIN actors a ON a.id = t.created_by
                           WHERE t.assignee_id = ? AND t.archived_at IS NULL AND t.status != 'done'
                           AND COALESCE(t.source, '') NOT IN ('ask_owner')
                           AND (a.kind != 'human' OR t.source = 'system' OR t.source LIKE 'event:%'
                                OR t.topic = 'chat-loop') ORDER BY t.id""", (owner,)).fetchall()
    for t in rows:
        to = coo if t["topic"] == "chat-loop" else ceo
        name = actors.get(conn, to)["name"]
        ref = tasks.display_id(t["id"])
        out.append(f"{ref} '{t['title'][:60]}' ({t['status']}) → {name}")
        if not apply:
            continue
        r = actors.get(conn, to)
        changes = {"assignee_type": r["kind"], "assignee_id": to, "assignee_name": r["name"], "retry_after": None,
                   "status": "next" if t["status"] in ("inbox", "review", "waiting", "someday") else t["status"],
                   "completed_at": None}
        if t["reviewer_id"] in (to, None) or t["topic"] == "chat-loop":
            changes["reviewer_id"] = None
        versioning.update(conn, ctx, tasks.ENTITY, t["id"], changes, action="assign")
        comments.log(conn, ctx, t["id"], f"Moved from the owner to {name}: agents do not give tasks to the owner "
                                         "(he is asked with ask_owner). Read the owner's comments here, finish it, "
                                         "or hand it to whoever should; ask him only what only he can decide.",
                     "system")
        audit.log(conn, ctx, "owner_task_moved", tasks.ENTITY, t["id"], to=to)
        from . import review_work

        review_work.close_for(conn, t["id"], "the task moved from the owner")
        told.setdefault(to, []).append(f"- {ref} '{t['title'][:80]}'")
    for to, lines in told.items():
        chat.send_dm(conn, ctx, to, "These tasks were with the owner; agents gave them to him. They are yours "
                                    "now (read his comments on them):\n" + "\n".join(lines), priority="fyi",
                     system=True)
        wake.wake(to)
    return out


def step_t232(conn: sqlite3.Connection, ctx: Ctx, apply: bool) -> list[str]:
    from . import comments

    out = []
    s = conn.execute("SELECT * FROM schedules WHERE id = ?", (CEO_REPORT_SCHEDULE,)).fetchone()
    if s is not None and s["last_task_id"] == T232:
        out.append(f"schedule #{s['id']} {s['name']}: no longer tied to T-232 (fires at its next slot "
                   f"{s['next_run_at']})")
        if apply:
            conn.execute("UPDATE schedules SET last_task_id = NULL, last_result = ? WHERE id = ?",
                         (json.dumps({"repaired": "was merged into T-232 by the escalation dedup"}), s["id"]))
            audit.log(conn, ctx, "schedule_repair", "schedule", s["id"], unlinked=T232)
    t = conn.execute("SELECT id FROM tasks WHERE id = ?", (T232,)).fetchone()
    noted = conn.execute("SELECT 1 FROM audit_log WHERE action = 'escalation_dedup_undone' AND entity = 'task' "
                         "AND entity_id = ?", (T232,)).fetchone()
    if t is not None and not noted and conn.execute(
            "SELECT 1 FROM audit_log WHERE action = 'escalation_dedup' AND entity = 'task' AND entity_id = ?",
            (T232,)).fetchone():
        out.append("T-232: note the two wrongly linked items (the CEO's daily report, the Kniha deployer summary "
                   "now in T-393/T-394)")
        if apply:
            comments.log(conn, ctx, T232, "Correction: two items were linked here by mistake by the escalation "
                                          "dedup (it followed distant task refs). The CEO's daily report (schedule "
                                          "#45) is its own routine again; the Kniha deployer's reservation summary "
                                          "is tracked in T-393/T-394. This task is only the quota alert.", "system")
            audit.log(conn, ctx, "escalation_dedup_undone", "task", T232, schedule=CEO_REPORT_SCHEDULE)
    return out


def step_reviews(conn: sqlite3.Connection, ctx: Ctx, apply: bool) -> list[str]:
    """The policy first (accept, route), then the review items, then the SLA: its clock starts at the
    review item, so the backlog is not all moved up the chain the hour its items appear; only the
    owner's reviews go to his stand-in at once."""
    from . import business, review_policy, review_work

    swept = review_policy.sweep(conn, apply=apply, sla=False)
    out = [f"waiting for review: {swept['waiting']}"]
    out += [f"auto-accept {x}" for x in swept["auto_accepted"]]
    out += [f"to QA {x}" for x in swept["to_qa"]]
    out += [f"to team lead {x}" for x in swept["to_lead"]]
    items = review_work.sync(conn, apply=apply)
    sla = business.review_sla(conn, dry_run=not apply)
    out += [f"SLA move {x}" for x in sla.get("moved", [])]
    if sla.get("reminded"):
        out.append(f"daily review digests cover {len(sla['reminded'])} result(s)")
    per: dict[str, int] = {}
    for x in items.get("created", []):
        per[x.split("→", 1)[1]] = per.get(x.split("→", 1)[1], 0) + 1
    out.append("review items created: " + (", ".join(f"{k} {v}" for k, v in sorted(per.items(), key=lambda kv: -kv[1]))
                                           or "none"))
    out += [f"review item {x}" for x in items.get("created", [])]
    out += [f"review item moved {x}" for x in items.get("moved", [])]
    out += [f"review item closed {x}" for x in items.get("closed", [])]
    return out


STEPS = (("routines", step_routines), ("owner", step_owner), ("t232", step_t232), ("reviews", step_reviews))


def run(conn: sqlite3.Connection, apply: bool = False, only: list[str] | None = None) -> dict:
    """`apply=False`: the whole fix is applied to an in-memory copy of the database and reported
    (the steps commit as they go, so a dry run on the real file could not roll back)."""
    if not apply:
        mem = sqlite3.connect(":memory:", check_same_thread=False)
        conn.commit()
        conn.backup(mem)
        mem.row_factory = sqlite3.Row
        mem.execute("PRAGMA foreign_keys = ON")
        try:
            return {"dry_run": True, **_run(mem, only)}
        finally:
            mem.close()
    return _run(conn, only)


def _run(conn: sqlite3.Connection, only: list[str] | None) -> dict:
    apply = True
    ctx = _ctx(conn)
    report = {}
    for key, fn in STEPS:
        if only and key not in only:
            continue
        report[key] = fn(conn, ctx, apply)
        conn.commit()
    audit.log(conn, ctx, "prodfix_workflow", None, None, **{k: len(v) for k, v in report.items()})
    conn.commit()
    return report


def main(argv: list[str] | None = None) -> None:
    from pathlib import Path

    from .config import get_settings
    from .db import connect

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write the changes (default: a dry run)")
    ap.add_argument("--only", nargs="*", choices=[k for k, _ in STEPS], help="run only these steps")
    ap.add_argument("--db", default=None, help="a database file (default: the configured one)")
    a = ap.parse_args(argv)
    conn = connect(Path(a.db) if a.db else get_settings().db_path)
    try:
        print(json.dumps(run(conn, apply=a.apply, only=a.only), ensure_ascii=False, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
