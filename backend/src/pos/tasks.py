"""Task core (AGENTS-SPEC 6a): fields, projects and steps, assignees, views.

Every write goes through `versioning`, so it is versioned and audited, and
every read is filtered by the caller's visibility layer. Callers are the REST
API (people), the MCP server (agents, Codex, Claude) and the runner.
"""

import json
import sqlite3
from datetime import date, timedelta

from . import actors, capture as capture_syntax, versioning
from .core import Ctx, Forbidden, NotFound, now_iso, today
from .visibility import DEFAULT, LAYERS, check_read, visible_sql

ENTITY = "task"
STATUSES = ("inbox", "next", "working", "review", "waiting", "someday", "done")
ASSIGNEE_TYPES = ("human", "ai", "agent", "external")
OPEN = ("inbox", "next", "working", "review", "waiting", "someday")

EDITABLE = {
    "title", "notes", "status", "priority", "do_date", "deadline", "estimate_min", "energy", "topic",
    "definition_of_done", "visibility", "follow_up", "position", "progress", "progress_note",
}

VIEWS = ("inbox", "today", "upcoming", "next", "agents", "waiting", "review", "someday", "done")


class Invalid(ValueError):
    pass


# ------------------------------------------------------------------ helpers

def display_id(task_id: int) -> str:
    return f"T-{task_id:03d}"


def parse_id(value: str | int) -> int:
    if isinstance(value, int):
        return value
    v = value.strip().upper()
    if v.startswith("T-"):
        v = v[2:]
    if not v.isdigit():
        raise Invalid(f"not a task id: {value}")
    return int(v)


def _validate(fields: dict) -> None:
    if "status" in fields and fields["status"] not in STATUSES:
        raise Invalid(f"status must be one of {STATUSES}")
    if fields.get("priority") not in (None, 1, 2, 3):
        raise Invalid("priority must be 1, 2 or 3")
    if fields.get("energy") not in (None, "high", "low"):
        raise Invalid("energy must be high or low")
    if "visibility" in fields and fields["visibility"] not in LAYERS:
        raise Invalid(f"visibility must be one of {LAYERS}")
    for k in ("do_date", "deadline", "follow_up"):
        if fields.get(k):
            try:
                date.fromisoformat(fields[k])
            except ValueError as e:
                raise Invalid(f"{k} must be YYYY-MM-DD") from e
    if "title" in fields and not str(fields["title"]).strip():
        raise Invalid("title is empty")


def resolve_assignee(conn: sqlite3.Connection, ctx: Ctx, value) -> dict:
    """Turn 'me' / 'ai' / an actor name / an outside name (or a dict) into columns."""
    if value in (None, "", "none"):
        return {"assignee_type": None, "assignee_id": None, "assignee_name": None}
    if isinstance(value, dict):
        kind = value.get("type")
        if kind not in ASSIGNEE_TYPES:
            raise Invalid(f"assignee type must be one of {ASSIGNEE_TYPES}")
        if kind == "external":
            name = (value.get("name") or "").strip()
            if not name:
                raise Invalid("an outside assignee needs a name")
            return {"assignee_type": "external", "assignee_id": None, "assignee_name": name}
        if value.get("id"):
            row = actors.get(conn, int(value["id"]))
            return {"assignee_type": row["kind"], "assignee_id": row["id"], "assignee_name": row["name"]}
        value = value.get("name") or kind
    v = str(value).strip().lstrip("@")
    if v.lower() in ("me", "ja", "já"):
        me = actors.get(conn, ctx.actor_id)
        row = me if me["kind"] == "human" else actors.get(conn, actors.owner_id(conn))
    elif v.lower() in ("ai", "assistant"):
        row = actors.get(conn, actors.assistant_id(conn))
    else:
        row = actors.find_by_name(conn, v)
    if row is None:
        return {"assignee_type": "external", "assignee_id": None, "assignee_name": v}
    return {"assignee_type": row["kind"], "assignee_id": row["id"], "assignee_name": row["name"]}


def _row(conn: sqlite3.Connection, ctx: Ctx, task_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise NotFound(display_id(task_id))
    check_read(conn, ENTITY, row, ctx.actor_id)
    return row


def to_dict(row: sqlite3.Row | dict) -> dict:
    d = dict(row)
    d["ref"] = display_id(d["id"])
    d["suggestion"] = json.loads(d["suggestion"]) if d.get("suggestion") else None
    return d


# ------------------------------------------------------------------ reads

def get(conn: sqlite3.Connection, ctx: Ctx, task_id: int) -> dict:
    row = _row(conn, ctx, task_id)
    task = to_dict(row)
    cond, params = visible_sql(ENTITY, ctx.actor_id)
    steps = conn.execute(
        f"SELECT * FROM tasks WHERE parent_id = ? AND archived_at IS NULL AND {cond} ORDER BY position, id",
        [task_id, *params],
    ).fetchall()
    task["steps"] = [to_dict(s) for s in steps]
    task["steps_done"] = sum(1 for s in steps if s["status"] == "done")
    if row["parent_id"]:
        p = conn.execute("SELECT id, title FROM tasks WHERE id = ?", (row["parent_id"],)).fetchone()
        task["parent"] = {"id": p["id"], "ref": display_id(p["id"]), "title": p["title"]}
    return task


def _view_sql(view: str) -> tuple[str, list, str]:
    """(condition, params, order) for a named view."""
    t = today().isoformat()
    top = "parent_id IS NULL"
    if view == "inbox":
        return "status = 'inbox'", [], "created_at DESC"
    if view == "today":
        return (f"{top} AND status IN ('next', 'working', 'review', 'waiting') "
                "AND ((do_date IS NOT NULL AND do_date <= ?) OR (deadline IS NOT NULL AND deadline <= ?))",
                [t, t], "COALESCE(priority, 4), CASE energy WHEN 'high' THEN 0 ELSE 1 END, position, id")
    if view == "upcoming":
        return (f"{top} AND status IN ('next', 'working', 'waiting') AND do_date > ?", [t],
                "do_date, COALESCE(priority, 4), id")
    if view == "next":
        return (f"{top} AND status = 'next' AND do_date IS NULL "
                "AND (assignee_type IS NULL OR assignee_type = 'human')", [], "COALESCE(priority, 4), position, id")
    if view == "agents":
        return "assignee_type IN ('ai', 'agent') AND status != 'done'", [], "status, COALESCE(priority, 4), id"
    if view == "waiting":
        return ("status != 'done' AND (status = 'waiting' OR assignee_type = 'external')", [],
                "COALESCE(follow_up, '9999'), id")
    if view == "review":
        return "status = 'review'", [], "updated_at"
    if view == "someday":
        return f"{top} AND status = 'someday'", [], "created_at DESC"
    if view == "done":
        return "status = 'done'", [], "completed_at DESC"
    raise Invalid(f"view must be one of {VIEWS}")


def list_tasks(conn: sqlite3.Connection, ctx: Ctx, view: str = "today", *, topic: str | None = None,
               assignee_id: int | None = None, limit: int = 200) -> list[dict]:
    cond, params, order = _view_sql(view)
    vis, vparams = visible_sql(ENTITY, ctx.actor_id)
    sql = f"SELECT * FROM tasks WHERE archived_at IS NULL AND {cond} AND {vis}"
    params = [*params, *vparams]
    if topic:
        sql += " AND topic = ?"
        params.append(topic.lower())
    if assignee_id:
        sql += " AND assignee_id = ?"
        params.append(assignee_id)
    rows = conn.execute(f"{sql} ORDER BY {order} LIMIT ?", [*params, limit]).fetchall()
    out = [to_dict(r) for r in rows]
    # Step progress for projects, in one query.
    ids = [t["id"] for t in out]
    if ids:
        marks = ",".join("?" for _ in ids)
        for r in conn.execute(
            f"""SELECT parent_id, COUNT(*) AS n, SUM(status = 'done') AS done FROM tasks
                WHERE parent_id IN ({marks}) AND archived_at IS NULL GROUP BY parent_id""", ids
        ):
            for t in out:
                if t["id"] == r["parent_id"]:
                    t["steps_total"], t["steps_done"] = r["n"], r["done"]
    return out


def counts(conn: sqlite3.Connection, ctx: Ctx) -> dict[str, int]:
    vis, vparams = visible_sql(ENTITY, ctx.actor_id)
    out = {}
    for view in VIEWS:
        if view == "done":
            continue
        cond, params, _ = _view_sql(view)
        out[view] = conn.execute(
            f"SELECT COUNT(*) FROM tasks WHERE archived_at IS NULL AND {cond} AND {vis}", [*params, *vparams]
        ).fetchone()[0]
    return out


def topics(conn: sqlite3.Connection, ctx: Ctx) -> list[dict]:
    vis, vparams = visible_sql(ENTITY, ctx.actor_id)
    rows = conn.execute(
        f"""SELECT topic, COUNT(*) AS open FROM tasks
            WHERE topic IS NOT NULL AND archived_at IS NULL AND status != 'done' AND {vis}
            GROUP BY topic ORDER BY open DESC, topic""", vparams
    ).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ writes

def create(conn: sqlite3.Connection, ctx: Ctx, fields: dict) -> dict:
    fields = dict(fields)
    assignee = fields.pop("assignee", None)
    parent_id = fields.pop("parent_id", None)
    owner = fields.pop("owner_id", None)
    source = fields.pop("source", ctx.via)
    unknown = set(fields) - EDITABLE
    if unknown:
        raise Invalid(f"unknown fields: {sorted(unknown)}")
    _validate(fields)
    if parent_id is not None:
        parent = _row(conn, ctx, parse_id(parent_id))
        fields.setdefault("topic", parent["topic"])
        fields.setdefault("visibility", parent["visibility"])
        fields.setdefault("status", "next")
    me = actors.get(conn, ctx.actor_id)
    if assignee is None and me["kind"] == "human" and fields.get("status", "inbox") != "inbox":
        # What a person writes down is theirs unless they hand it over.
        assignee = {"type": "human", "id": me["id"]}
    if owner is None:
        # Agents create work on behalf of the owner unless they say otherwise.
        owner = ctx.actor_id if me["kind"] == "human" else actors.owner_id(conn)
    now = now_iso()
    values = {
        "status": "inbox", "visibility": DEFAULT, **fields,
        "parent_id": parse_id(parent_id) if parent_id is not None else None,
        "owner_id": owner, "source": source, "created_by": ctx.actor_id,
        "created_at": now, "updated_at": now,
        **resolve_assignee(conn, ctx, assignee),
    }
    if values.get("topic"):
        values["topic"] = values["topic"].lower().lstrip("#")
    if values["assignee_type"] == "external":
        values["status"] = "waiting" if values["status"] in ("inbox", "next") else values["status"]
        values.setdefault("follow_up", (today() + timedelta(days=3)).isoformat())
    elif values["assignee_type"] in ("ai", "agent") and values["status"] == "inbox":
        values["status"] = "next"
    if values["status"] == "done":
        values["completed_at"] = now
    return get(conn, ctx, versioning.insert(conn, ctx, ENTITY, values)["id"])


def capture(conn: sqlite3.Connection, ctx: Ctx, text: str, source: str | None = None) -> dict:
    """Quick capture. Plain text lands in the inbox; with a date or an
    assignee the task is already clear enough to skip it."""
    parsed = capture_syntax.parse(text)
    status = "next" if parsed.get("do_date") or parsed.get("assignee") or parsed.get("deadline") else "inbox"
    return create(conn, ctx, {"status": status, **parsed, "source": source or ctx.via})


def update(conn: sqlite3.Connection, ctx: Ctx, task_id: int, changes: dict) -> dict:
    changes = dict(changes)
    row = _row(conn, ctx, task_id)
    extra = {}
    if "assignee" in changes:
        extra.update(resolve_assignee(conn, ctx, changes.pop("assignee")))
    unknown = set(changes) - EDITABLE
    if unknown:
        raise Invalid(f"unknown fields: {sorted(unknown)}")
    _validate(changes)
    if "visibility" in changes and changes["visibility"] != row["visibility"]:
        from .integrations import check_visibility_change

        check_visibility_change(conn, ctx, row, changes["visibility"])
    if changes.get("topic"):
        changes["topic"] = changes["topic"].lower().lstrip("#")
    if "status" in changes:
        if changes["status"] == "done" and row["status"] != "done":
            extra["completed_at"] = now_iso()
        elif changes["status"] != "done":
            extra["completed_at"] = None
    versioning.update(conn, ctx, ENTITY, task_id, {**changes, **extra})
    return get(conn, ctx, task_id)


def complete(conn: sqlite3.Connection, ctx: Ctx, task_id: int, note: str | None = None) -> dict:
    """People finish tasks; AI and agents hand results in for review."""
    _row(conn, ctx, task_id)
    kind = actors.get(conn, ctx.actor_id)["kind"]
    changes: dict = {"progress": 100}
    if note:
        changes["progress_note"] = note
    changes["status"] = "done" if kind == "human" else "review"
    return update(conn, ctx, task_id, changes)


def review(conn: sqlite3.Connection, ctx: Ctx, task_id: int, accept: bool, comment: str | None = None) -> dict:
    row = _row(conn, ctx, task_id)
    if row["status"] != "review":
        raise Invalid(f"{display_id(task_id)} is not waiting for review")
    if actors.get(conn, ctx.actor_id)["kind"] != "human":
        raise Forbidden("only people review AI and agent results")
    if accept:
        return update(conn, ctx, task_id, {"status": "done"})
    versioning.update(conn, ctx, ENTITY, task_id, {
        "status": "next", "progress": 0, "completed_at": None,
        "returned_count": row["returned_count"] + 1,
        "progress_note": f"Returned: {comment}" if comment else "Returned",
    }, action="return")
    return get(conn, ctx, task_id)


def intervene(conn: sqlite3.Connection, ctx: Ctx, task_id: int, note: str = "") -> dict:
    """The owner had to step in on work an AI or agent was doing."""
    row = _row(conn, ctx, task_id)
    if actors.get(conn, ctx.actor_id)["kind"] != "human":
        raise Forbidden("only people record an intervention")
    versioning.update(conn, ctx, ENTITY, task_id, {"interventions": row["interventions"] + 1},
                      action="intervene")
    if note:
        update(conn, ctx, task_id, {"progress_note": f"Owner stepped in: {note}"})
    return get(conn, ctx, task_id)


def assign(conn: sqlite3.Connection, ctx: Ctx, task_id: int, assignee) -> dict:
    row = _row(conn, ctx, task_id)
    cols = resolve_assignee(conn, ctx, assignee)
    changes: dict = {}
    if cols["assignee_type"] == "external":
        changes["status"] = "waiting"
        if not row["follow_up"]:
            changes["follow_up"] = (today() + timedelta(days=3)).isoformat()
    elif row["status"] in ("inbox", "waiting"):
        changes["status"] = "next"
    versioning.update(conn, ctx, ENTITY, task_id, {**cols, **changes}, action="assign")
    return get(conn, ctx, task_id)


def claim(conn: sqlite3.Connection, ctx: Ctx, task_id: int) -> dict:
    """An agent takes a task from its queue and starts working."""
    row = _row(conn, ctx, task_id)
    if row["assignee_id"] not in (None, ctx.actor_id):
        raise Forbidden(f"{display_id(task_id)} is assigned to someone else")
    if row["status"] not in ("next", "inbox"):
        raise Invalid(f"{display_id(task_id)} is {row['status']}, not claimable")
    changes = {"status": "working", "progress": 0}
    if row["assignee_id"] is None:
        changes.update(resolve_assignee(conn, ctx, {"type": "agent", "id": ctx.actor_id}))
    versioning.update(conn, ctx, ENTITY, task_id, changes, action="claim")
    return get(conn, ctx, task_id)


def report_progress(conn: sqlite3.Connection, ctx: Ctx, task_id: int, percent: int, message: str = "") -> dict:
    if not 0 <= percent <= 100:
        raise Invalid("percent must be 0 to 100")
    return update(conn, ctx, task_id, {"progress": percent, "progress_note": message or None})


def archive(conn: sqlite3.Connection, ctx: Ctx, task_id: int) -> dict:
    _row(conn, ctx, task_id)
    versioning.archive(conn, ctx, ENTITY, task_id)
    return to_dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())


def unarchive(conn: sqlite3.Connection, ctx: Ctx, task_id: int) -> dict:
    _row(conn, ctx, task_id)
    versioning.unarchive(conn, ctx, ENTITY, task_id)
    return get(conn, ctx, task_id)


def set_suggestion(conn: sqlite3.Connection, ctx: Ctx, task_id: int, suggestion: dict) -> dict:
    _row(conn, ctx, task_id)
    versioning.update(conn, ctx, ENTITY, task_id, {"suggestion": json.dumps(suggestion, ensure_ascii=False)},
                      action="suggest")
    return get(conn, ctx, task_id)


# ------------------------------------------------------------------ clarify (GTD)

CLARIFY_ACTIONS = ("accept", "do_now", "delegate", "someday", "reference", "trash")


def clarify(conn: sqlite3.Connection, ctx: Ctx, task_id: int, action: str, fields: dict | None = None) -> dict:
    """Process one inbox item. `accept` applies the AI suggestion (optionally
    edited through `fields`), including its steps."""
    if action not in CLARIFY_ACTIONS:
        raise Invalid(f"action must be one of {CLARIFY_ACTIONS}")
    row = _row(conn, ctx, task_id)
    fields = dict(fields or {})
    if action == "accept":
        sug = json.loads(row["suggestion"]) if row["suggestion"] else {}
        steps = fields.pop("steps", sug.get("steps") or [])
        base = {k: sug.get(k) for k in ("title", "topic", "priority", "do_date", "deadline", "estimate_min",
                                        "energy", "definition_of_done") if sug.get(k) is not None}
        changes = {**base, **fields, "status": "next", "suggestion": None}
        assignee = changes.pop("assignee", sug.get("assignee"))
        if assignee:
            changes.update(resolve_assignee(conn, ctx, assignee))
        clean = {k: v for k, v in changes.items() if k in EDITABLE or k.startswith("assignee_") or k == "suggestion"}
        _validate(clean)
        versioning.update(conn, ctx, ENTITY, task_id, clean, action="clarify:accept")
        for i, step in enumerate(steps):
            create(conn, ctx, {
                "title": step["title"], "parent_id": task_id, "position": i,
                "estimate_min": step.get("estimate_min"), "assignee": step.get("assignee"),
                "notes": step.get("reason") or "",
            })
        return get(conn, ctx, task_id)
    if action == "do_now":
        return update(conn, ctx, task_id, {"status": "done"})
    if action == "delegate":
        if not fields.get("assignee"):
            raise Invalid("delegate needs an assignee")
        return assign(conn, ctx, task_id, fields["assignee"])
    if action == "someday":
        return update(conn, ctx, task_id, {"status": "someday"})
    if action == "reference":
        # Not actionable but worth keeping: becomes a note, the item is archived.
        now = now_iso()
        versioning.insert(conn, ctx, "note", {
            "title": row["title"], "body": row["notes"], "topic": row["topic"],
            "visibility": row["visibility"], "owner_id": row["owner_id"], "created_at": now, "updated_at": now,
        })
    return archive(conn, ctx, task_id)
