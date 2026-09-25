"""Projects as shared work (REVIZE-FUNKCI 3.6).

A project has a goal, a definition of done, a lead, members (people and
agents), a status and its own chat channel #<slug>. Tasks belong to a project
through tasks.project_id; the project lead reviews its tasks by default
(tasks.reviewer_of). Topics stay labels across everything; a project can carry
several of them.

The old "project = a task with steps" is migrated once at start: every root
task with steps becomes a project, the task and its steps become its tasks.
"""

import json
import re
import sqlite3

from . import actors, audit, tasks, versioning
from .core import Ctx, Forbidden, NotFound, now_iso
from .visibility import DEFAULT, LAYERS, check_read, visible_sql

ENTITY = "project"
versioning.register(ENTITY, "projects")
STATUSES = ("active", "paused", "done", "archived")
ROLES = ("lead", "member")


def slugify(name: str) -> str:
    import unicodedata

    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-")[:50] or "project"


def _row(conn: sqlite3.Connection, ctx: Ctx, ref) -> sqlite3.Row:
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (int(ref),)).fetchone()
    else:
        row = conn.execute("SELECT * FROM projects WHERE slug = ?", (str(ref).lstrip("#"),)).fetchone()
    if row is None:
        raise NotFound(f"project {ref}")
    check_read(conn, ENTITY, row, ctx.actor_id)
    return row


def members(conn: sqlite3.Connection, project_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT m.actor_id, m.role, a.name, a.kind FROM project_members m JOIN actors a ON a.id = m.actor_id
           WHERE m.project_id = ? ORDER BY m.role = 'lead' DESC, a.name""", (project_id,)).fetchall()
    return [dict(r) for r in rows]


def _view(conn: sqlite3.Connection, row) -> dict:
    d = dict(row)
    d["labels"] = json.loads(d["labels"] or "[]")
    lead = actors.get(conn, d["lead_id"]) if d["lead_id"] else None
    d["lead_name"] = lead["name"] if lead else None
    d["members"] = members(conn, d["id"])
    counts = conn.execute(
        """SELECT SUM(status IN ('inbox', 'next', 'waiting', 'someday')) AS queued, SUM(status = 'working') AS working,
                  SUM(status = 'review') AS review, SUM(status = 'done') AS done
           FROM tasks WHERE project_id = ? AND archived_at IS NULL""", (d["id"],)).fetchone()
    d["counts"] = {k: counts[k] or 0 for k in ("queued", "working", "review", "done")}
    return d


def list_projects(conn: sqlite3.Connection, ctx: Ctx, status: str | None = None) -> list[dict]:
    vis, params = visible_sql(ENTITY, ctx.actor_id)
    sql = f"SELECT * FROM projects WHERE {vis}"
    if status:
        sql += " AND status = ?"
        params = [*params, status]
    else:
        sql += " AND status != 'archived'"
    return [_view(conn, r) for r in conn.execute(sql + " ORDER BY status = 'active' DESC, updated_at DESC", params)]


def get(conn: sqlite3.Connection, ctx: Ctx, ref) -> dict:
    row = _row(conn, ctx, ref)
    out = _view(conn, row)
    out["tasks"] = tasks.list_project(conn, ctx, row["id"])
    return out


def _may_edit(conn: sqlite3.Connection, ctx: Ctx, row) -> None:
    me = actors.get(conn, ctx.actor_id)
    if me["is_owner"] or ctx.actor_id in (row["lead_id"], row["created_by"]):
        return
    from .org import manages

    if row["lead_id"] and manages(conn, ctx.actor_id, row["lead_id"]):
        return
    raise Forbidden("the project's lead, its creator, their lead or the owner change it")


def create(conn: sqlite3.Connection, ctx: Ctx, *, name: str, goal: str = "", definition_of_done: str = "",
           lead=None, member_refs: list | None = None, visibility: str = DEFAULT, labels: list[str] | None = None,
           due: str | None = None, channel: bool = True) -> dict:
    from . import agents
    from .org import _member

    me = actors.get(conn, ctx.actor_id)
    if me["kind"] != "human" and not agents.has_permission(conn, ctx.actor_id, "tasks:write"):
        raise Forbidden("creating a project needs tasks:write")
    name = (name or "").strip()
    if not name:
        raise tasks.Invalid("a project needs a name")
    if visibility not in LAYERS:
        raise tasks.Invalid(f"visibility must be one of {LAYERS}")
    slug = base = slugify(name)
    n = 2
    while conn.execute("SELECT 1 FROM projects WHERE slug = ?", (slug,)).fetchone():
        slug, n = f"{base}-{n}", n + 1
    lead_row = _member(conn, lead) if lead not in (None, "") else me
    now = now_iso()
    row = versioning.insert(conn, ctx, ENTITY, {
        "slug": slug, "name": name[:120], "goal": goal.strip() or None,
        "definition_of_done": definition_of_done.strip() or None, "lead_id": lead_row["id"],
        "status": "active", "visibility": visibility, "labels": json.dumps(sorted({l.lower().lstrip("#") for l in labels or []})),
        "due": due, "owner_id": ctx.actor_id if me["kind"] == "human" else actors.owner_id(conn),
        "created_by": ctx.actor_id, "created_at": now, "updated_at": now})
    pid = row["id"]
    _add(conn, pid, lead_row["id"], "lead")
    for ref in member_refs or []:
        mid = _member(conn, ref)["id"]
        if mid != lead_row["id"]:
            _add(conn, pid, mid, "member")
    if channel:
        _open_channel(conn, ctx, pid)
    audit.log(conn, ctx, "project_create", ENTITY, pid, slug=slug)
    return get(conn, ctx, pid)


def _open_channel(conn: sqlite3.Connection, ctx: Ctx, project_id: int) -> None:
    """The project's own chat channel #<slug> with its members (best effort)."""
    from . import chat

    p = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    try:
        ch = chat.create_channel(conn, ctx, p["slug"], [m["actor_id"] for m in members(conn, project_id)],
                                 topic=f"Project {p['name']}" + (f": {p['goal'][:200]}" if p["goal"] else ""),
                                 visibility="private" if p["visibility"] == "private" else "team")
        conn.execute("UPDATE projects SET channel_id = ? WHERE id = ?", (ch["id"], project_id))
    except Exception as e:  # noqa: BLE001 - a name clash or no chat right never stops the project
        audit.log(conn, ctx, "project_channel_failed", ENTITY, project_id, error=str(e)[:200])


def _add(conn: sqlite3.Connection, project_id: int, actor_id: int, role: str) -> None:
    conn.execute("""INSERT INTO project_members (project_id, actor_id, role, added_at) VALUES (?, ?, ?, ?)
                    ON CONFLICT (project_id, actor_id) DO UPDATE SET role = excluded.role""",
                 (project_id, actor_id, role, now_iso()))


def add_member(conn: sqlite3.Connection, ctx: Ctx, ref, member, role: str = "member") -> dict:
    from .org import _member

    row = _row(conn, ctx, ref)
    _may_edit(conn, ctx, row)
    if role not in ROLES:
        raise tasks.Invalid(f"role must be one of {ROLES}")
    m = _member(conn, member)
    _add(conn, row["id"], m["id"], role)
    if role == "lead":
        versioning.update(conn, ctx, ENTITY, row["id"], {"lead_id": m["id"]}, action="set_lead")
    if row["channel_id"]:
        from . import chat

        try:
            chat.invite(conn, ctx, row["channel_id"], m["id"])
        except Exception:  # noqa: BLE001 - already in, or no right to invite
            pass
    audit.log(conn, ctx, "project_member", ENTITY, row["id"], member=m["name"], role=role)
    return get(conn, ctx, row["id"])


def update(conn: sqlite3.Connection, ctx: Ctx, ref, changes: dict) -> dict:
    row = _row(conn, ctx, ref)
    _may_edit(conn, ctx, row)
    allowed = {"name", "goal", "definition_of_done", "status", "visibility", "labels", "due"}
    unknown = set(changes) - allowed
    if unknown:
        raise tasks.Invalid(f"unknown fields: {sorted(unknown)}")
    sets = dict(changes)
    if "status" in sets and sets["status"] not in STATUSES:
        raise tasks.Invalid(f"status must be one of {STATUSES}")
    if "visibility" in sets and sets["visibility"] != row["visibility"]:
        from .integrations import check_visibility_change

        check_visibility_change(conn, ctx, row, sets["visibility"])
    if "labels" in sets:
        sets["labels"] = json.dumps(sorted({l.lower().lstrip("#") for l in sets["labels"] or []}))
    versioning.update(conn, ctx, ENTITY, row["id"], sets)
    return get(conn, ctx, row["id"])


def lead_of_task(conn: sqlite3.Connection, task_row) -> int | None:
    pid = task_row["project_id"] if "project_id" in task_row.keys() else None
    if not pid:
        return None
    r = conn.execute("SELECT lead_id FROM projects WHERE id = ?", (pid,)).fetchone()
    return r["lead_id"] if r else None


def migrate_step_projects(conn: sqlite3.Connection) -> int:
    """Once: every root task with steps becomes a project (idempotent)."""
    rows = conn.execute(
        """SELECT t.* FROM tasks t WHERE t.parent_id IS NULL AND t.project_id IS NULL AND t.archived_at IS NULL
           AND EXISTS (SELECT 1 FROM tasks s WHERE s.parent_id = t.id AND s.archived_at IS NULL)""").fetchall()
    if not rows:
        return 0
    owner = actors.owner_id(conn)
    ctx = Ctx(owner, via="system")
    for t in rows:
        p = create(conn, ctx, name=t["title"], goal=(t["notes"] or "")[:2000],
                   definition_of_done=t["definition_of_done"] or "", lead=t["assignee_id"] or owner,
                   visibility=t["visibility"], labels=[t["topic"]] if t["topic"] else [], due=t["deadline"],
                   channel=False)
        ids = [t["id"]] + [s["id"] for s in conn.execute("SELECT id FROM tasks WHERE parent_id = ?", (t["id"],))]
        for tid in ids:
            conn.execute("UPDATE tasks SET project_id = ? WHERE id = ?", (p["id"], tid))
        for s in conn.execute("SELECT DISTINCT assignee_id FROM tasks WHERE parent_id = ? AND assignee_id IS NOT NULL",
                              (t["id"],)).fetchall():
            _add(conn, p["id"], s["assignee_id"], "member")
        versioning.update(conn, ctx, ENTITY, p["id"], {"status": "done" if t["status"] == "done" else "active"},
                          action="migrated")
    conn.commit()
    return len(rows)
