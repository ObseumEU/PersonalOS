"""Default task descriptions: what a task is for, where it came from, and what
done looks like, built from the task's own fields and links. Deterministic, no
model call, so it is cheap enough for every create and for a backfill.

`tasks.create` uses `build` when a task arrives without notes and marks the
row `description_generated = 1`; a person or agent writing notes clears the
flag. The backfill fills old tasks the same way:

    python -m pos.task_descriptions backfill [--dry-run]

It only touches tasks whose notes are empty, so running it again changes
nothing.
"""

import argparse
import json
import sqlite3
import sys

from .core import Ctx

GENERATED_FOOTER = "_Generated from the task's fields. Edit it to add the real context._"

_SOURCES = {
    "ui": "captured in the web UI",
    "api": "created in the web UI / REST API",
    "mcp": "created over MCP",
    "a2a": "sent by a remote agent over A2A",
    "system": "raised automatically by PersonalOS",
    "scheduler": "raised by a PersonalOS routine",
    "schedule": "raised by a schedule",
    "runner": "created during an agent run",
    "outbound": "raised by an outbound connector",
    "deployer": "raised by the self-deploy pipeline",
}


def _actor(conn: sqlite3.Connection, actor_id) -> sqlite3.Row | None:
    if not actor_id:
        return None
    return conn.execute("SELECT id, kind, name, is_owner FROM actors WHERE id = ?", (actor_id,)).fetchone()


def _who(row: sqlite3.Row | None) -> str | None:
    if row is None:
        return None
    if row["is_owner"]:
        return "the owner"
    return {"ai": f"the AI assistant ({row['name']})", "agent": f"the agent {row['name']}"}.get(
        row["kind"], row["name"])


def _ref(task_id: int) -> str:
    return f"T-{task_id:03d}"


def _schedule(conn: sqlite3.Connection, source: str) -> sqlite3.Row | None:
    try:
        sid = int(source.split(":", 1)[1])
    except (IndexError, ValueError):
        return None
    try:
        return conn.execute("SELECT name, schedule FROM schedules WHERE id = ?", (sid,)).fetchone()
    except sqlite3.OperationalError:
        return None


def _links(conn: sqlite3.Connection, task_id: int | None) -> list[str]:
    """What else points at an existing task: the event it came from, handoffs, messages."""
    if not task_id:
        return []
    out = []
    q = lambda sql: conn.execute(sql, (task_id,)).fetchall()  # noqa: E731
    try:
        for e in q("SELECT source, kind, title FROM events WHERE task_id = ? ORDER BY id LIMIT 1"):
            kind = f" {e['kind']}" if e["kind"] else ""
            out.append(f"Linked event: {e['source']}{kind} “{e['title']}”")
    except sqlite3.OperationalError:
        pass
    try:
        for h in q("""SELECT f.name AS f, t.name AS t, h.note FROM handoffs h
                      JOIN actors f ON f.id = h.from_actor JOIN actors t ON t.id = h.to_actor
                      WHERE h.task_id = ? ORDER BY h.id LIMIT 3"""):
            out.append(f"Handed off by {h['f']} to {h['t']}" + (f": {h['note']}" if h["note"] else ""))
    except sqlite3.OperationalError:
        pass
    try:
        m = conn.execute(
            """SELECT a.name, m.body FROM chat_messages m, json_each(m.attachments) j
               JOIN actors a ON a.id = m.author_id
               WHERE json_extract(j.value, '$.type') = 'task' AND json_extract(j.value, '$.id') = ?
               ORDER BY m.id LIMIT 1""", (task_id,)).fetchone()
        if m:
            body = " ".join(m["body"].split())
            out.append(f"Linked message from {m['name']}: {body[:160]}{'…' if len(body) > 160 else ''}")
    except sqlite3.OperationalError:
        pass
    return out


def _source(conn: sqlite3.Connection, source: str | None) -> tuple[str, str | None]:
    """(where it came from, a purpose hint)."""
    s = (source or "").strip()
    if s.startswith("schedule:"):
        sch = _schedule(conn, s)
        if sch:
            return f"the schedule “{sch['name']}” ({sch['schedule']})", f"recurring work from “{sch['name']}”"
        return "a schedule", "recurring work from a schedule"
    if s.startswith("event:"):
        kind = s.split(":", 1)[1] or "connector"
        return f"an incoming {kind} event", f"follow-up on an incoming {kind} item"
    if s == "a2a":
        return _SOURCES[s], "a request from a remote agent"
    if s in ("system", "scheduler", "deployer", "outbound"):
        return _SOURCES[s], "raised automatically so someone acts on it"
    if s in _SOURCES:
        return _SOURCES[s], None
    return (f"source “{s}”" if s else "an unknown source"), None


def _done(values: dict, assignee: str | None, parent: sqlite3.Row | None) -> str:
    if values.get("definition_of_done"):
        return str(values["definition_of_done"]).strip()
    kind = values.get("assignee_type")
    if kind == "external":
        base = f"{values.get('assignee_name') or 'They'} delivered it and the owner checked the result."
    elif kind in ("ai", "agent"):
        base = (f"{assignee or 'The assignee'} hands the result in with a short summary (complete_task) "
                "and the owner accepts it in review.")
    else:
        base = "What the title asks for is done and the task is marked done."
    if parent is not None:
        base += f" Then project {_ref(parent['id'])} can move on."
    return base


def build(conn: sqlite3.Connection, values: dict, task_id: int | None = None,
          extra: list[str] | None = None) -> str:
    """A description from a task's columns (a row or the values about to be
    inserted); `extra` adds context lines the database does not hold yet."""
    title = " ".join(str(values.get("title") or "").split()) or "Untitled task"
    parent = None
    if values.get("parent_id"):
        parent = conn.execute("SELECT id, title FROM tasks WHERE id = ?", (values["parent_id"],)).fetchone()
    where, hint = _source(conn, values.get("source"))
    creator = _who(_actor(conn, values.get("created_by")))
    if values.get("assignee_id"):
        assignee = _who(_actor(conn, values["assignee_id"]))
    elif values.get("assignee_type") == "external":
        assignee = f"{values.get('assignee_name')} (outside PersonalOS)"
    else:
        assignee = None
    topic = values.get("topic")

    if parent is not None:
        purpose = f"“{title}” is a step of project {_ref(parent['id'])} “{parent['title']}”."
    elif hint:
        purpose = f"“{title}”: {hint}" + (f", topic #{topic}." if topic else ".")
    elif topic:
        purpose = f"“{title}”: work for the #{topic} topic."
    else:
        purpose = f"“{title}”: written down so it gets done and is not forgotten."

    origin = where + (f", by {creator}" if creator else "")
    if values.get("created_at"):
        origin += f" on {str(values['created_at'])[:10]}"
    lines = [
        purpose, "",
        f"- **Where from:** {origin}.",
        f"- **Who:** {assignee or 'not assigned yet'}.",
    ]
    for link in [*_links(conn, task_id), *(extra or [])]:
        lines.append(f"- {link}.")
    lines.append(f"- **Done when:** {_done(values, assignee, parent)}")
    lines += ["", GENERATED_FOOTER]
    return "\n".join(lines)


def needs_description(notes) -> bool:
    return not str(notes or "").strip()


# ------------------------------------------------------------------ backfill

def backfill(conn: sqlite3.Connection, apply: bool = True, limit: int | None = None) -> dict:
    """Fill empty notes on existing tasks (archived ones too) with `build`,
    flagged as generated. Idempotent: tasks with notes are never touched."""
    from . import actors, versioning
    from . import tasks as _tasks  # noqa: F401  registers the task entity

    rows = conn.execute(
        "SELECT * FROM tasks WHERE TRIM(COALESCE(notes, '')) = '' ORDER BY id" + (f" LIMIT {int(limit)}" if limit else "")
    ).fetchall()
    ctx = Ctx(actors.owner_id(conn), via="system")
    filled = []
    for row in rows:
        text = build(conn, dict(row), row["id"])
        if apply:
            # Straight SQL for the value, so updated_at (list order, "recently changed") stays as it was;
            # the history snapshot still records who filled it and when.
            conn.execute("UPDATE tasks SET notes = ?, description_generated = 1 WHERE id = ?", (text, row["id"]))
            versioning._snapshot(conn, ctx, versioning.spec("task"), row["id"], "describe")
        filled.append(_ref(row["id"]))
    if apply:
        from . import audit

        audit.log(conn, ctx, "describe_backfill", "task", None, filled=len(filled))
        conn.commit()
    return {"checked": len(rows), "filled": len(filled) if apply else 0,
            "would_fill": len(filled) if not apply else 0, "tasks": filled[:50]}


def main(argv: list[str] | None = None) -> int:
    from . import actors
    from .config import get_settings
    from .db import connect, migrate

    parser = argparse.ArgumentParser(prog="pos.task_descriptions")
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill", help="fill empty task descriptions (idempotent)")
    b.add_argument("--dry-run", action="store_true")
    b.add_argument("--db", help="path to personalos.db (default: POS_DATA_DIR/personalos.db)")
    b.add_argument("--limit", type=int)
    args = parser.parse_args(argv)

    from pathlib import Path

    conn = connect(Path(args.db) if args.db else get_settings().db_path)
    try:
        migrate(conn)
        actors.ensure_builtin(conn)
        out = backfill(conn, apply=not args.dry_run, limit=args.limit)
        print(json.dumps(out, ensure_ascii=False, indent=2))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
