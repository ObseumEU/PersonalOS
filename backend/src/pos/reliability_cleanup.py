"""One-off clean-up of duplicates made before the idempotency rules (2026-10, the reliability
package): repeated schedules (same name, schedule and assignee: pos.schedules.existing),
more than one open access-queue task of the Access manager (pos.access.service._wake_manager)
and more than one open incident task for one fingerprint (app, kind, key: pos.monitor).
The oldest stays; the repeats are archived (schedules) or closed with a note pointing to it.

Dry run by default; safe to run again (nothing left to do is a no-op):
    docker exec personalos-api-1 python -m pos.reliability_cleanup           # what it would do
    docker exec personalos-api-1 python -m pos.reliability_cleanup --apply   # do it
"""

import json
import sqlite3
import sys

from . import actors, audit, comments, schedules, tasks, versioning
from .core import Ctx, now_iso

DONE_NOTE = "Duplicate of {ref}: closed by the one-off clean-up (pos.reliability_cleanup); the work goes on there."


def _access_queue(conn: sqlite3.Connection) -> list[tuple[int, list[int]]]:
    from .access import service as access

    am = access.manager_id(conn)
    if am is None:
        return []
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM tasks WHERE assignee_id = ? AND source = 'access' AND status NOT IN ('done') "
        "AND archived_at IS NULL AND title LIKE ? ORDER BY id", (am, access.QUEUE_TITLE + "%"))]
    return [(ids[0], ids[1:])] if len(ids) > 1 else []


def _incidents(conn: sqlite3.Connection) -> list[tuple[int, list[int]]]:
    from . import monitor

    monitor.ensure_schema(conn)
    groups: dict[tuple, list[int]] = {}
    for r in conn.execute(
            """SELECT s.*, t.id AS tid FROM sentinel_incidents s JOIN tasks t ON t.id = COALESCE(s.task_id, s.ticket_id)
               WHERE s.dup_of IS NULL AND t.status != 'done' AND t.archived_at IS NULL AND COALESCE(s.key, '') != ''
               ORDER BY t.id"""):
        ids = groups.setdefault((monitor.canonical_service(r), r["kind"], r["key"]), [])
        if r["tid"] not in ids:
            ids.append(r["tid"])
    return [(ids[0], ids[1:]) for ids in groups.values() if len(ids) > 1]


def plan(conn: sqlite3.Connection) -> dict:
    """What the clean-up would change: {kind: [(kept id, [repeat ids])]}."""
    return {"schedules": schedules.duplicates(conn), "access_queue": _access_queue(conn),
            "incident_tasks": _incidents(conn)}


def apply(conn: sqlite3.Connection) -> dict:
    todo = plan(conn)
    owner = Ctx(actors.owner_id(conn), via="system")
    for keep, repeats in todo["schedules"]:
        for sid in repeats:
            versioning.archive(conn, owner, schedules.ENTITY, sid)
            audit.log(conn, owner, "schedule_dedupe", "schedule", sid, kept=keep)
    for keep, repeats in todo["access_queue"] + todo["incident_tasks"]:
        for tid in repeats:
            note = DONE_NOTE.format(ref=tasks.display_id(keep))
            versioning.update(conn, owner, tasks.ENTITY, tid,
                              {"status": "done", "completed_at": now_iso(), "progress_note": note}, action="dedupe")
            comments.log(conn, owner, tid, note, "system")
            comments.log(conn, owner, keep, f"Merged {tasks.display_id(tid)} into this one (one-off clean-up).",
                         "system")
            audit.log(conn, owner, "task_dedupe", "task", tid, kept=keep)
    conn.commit()
    return todo


def main(argv: list[str] | None = None) -> None:
    from .config import Settings
    from .db import connect

    argv = sys.argv[1:] if argv is None else argv
    conn = connect(Settings().db_path)
    try:
        out = apply(conn) if "--apply" in argv else plan(conn)
    finally:
        conn.close()
    print(json.dumps({"applied": "--apply" in argv, **out}))


if __name__ == "__main__":
    main()
