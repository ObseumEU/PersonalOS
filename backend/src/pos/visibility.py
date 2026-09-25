"""The three visibility layers (docs/AGENTS-SPEC.md, principle 3).

- public:  everyone, including outside agents over A2A
- team:    every member of PersonalOS, people and agents (the default)
- private: only the owner of the item and actors it is explicitly shared with
"""

import sqlite3

from .core import Forbidden

LAYERS = ("public", "team", "private")
DEFAULT = "team"


def visible_sql(entity: str, actor_id: int, alias: str = "") -> tuple[str, list]:
    """SQL condition (and params) selecting rows `actor_id` may read."""
    p = f"{alias}." if alias else ""
    return (
        f"({p}visibility != 'private' OR {p}owner_id = ? OR EXISTS ("
        f"SELECT 1 FROM shares s WHERE s.entity = ? AND s.entity_id = {p}id AND s.actor_id = ?))",
        [actor_id, entity, actor_id],
    )


def can_read(conn: sqlite3.Connection, entity: str, row: sqlite3.Row | dict, actor_id: int) -> bool:
    if row["visibility"] != "private" or row["owner_id"] == actor_id:
        return True
    return (
        conn.execute(
            "SELECT 1 FROM shares WHERE entity = ? AND entity_id = ? AND actor_id = ?",
            (entity, row["id"], actor_id),
        ).fetchone()
        is not None
    )


def check_read(conn: sqlite3.Connection, entity: str, row, actor_id: int) -> None:
    if not can_read(conn, entity, row, actor_id):
        raise Forbidden(f"{entity} {row['id']} is private")


def can_write(conn: sqlite3.Connection, entity: str, row, actor_id: int) -> bool:
    """Who may change or archive an item (REVIZE-FUNKCI 4.7): the company owner,
    the item's owner, its assignee, whoever created it, the members of its project, the item
    owner's lead, and agents with tasks:write (the platform's staff, e.g. the
    Project manager routing work). Everyone else only reads (and comments)."""
    from . import actors
    from .org import manages

    me = actors.get(conn, actor_id)
    keys = row.keys() if hasattr(row, "keys") else row
    if me["is_owner"] or ("owner_id" in keys and row["owner_id"] == actor_id):
        return True
    if "assignee_id" in keys and row["assignee_id"] == actor_id:
        return True
    if "created_by" in keys and row["created_by"] == actor_id:
        return True  # whoever asked for it (an agent's own task, an A2A sender cancelling)
    if "project_id" in keys and row["project_id"] and conn.execute(
            "SELECT 1 FROM project_members WHERE project_id = ? AND actor_id = ?", (row["project_id"], actor_id)).fetchone():
        return True
    if "owner_id" in keys and row["owner_id"] and manages(conn, actor_id, row["owner_id"]):
        return True
    if me["kind"] != "human":
        from .agents import has_permission

        return has_permission(conn, actor_id, "tasks:write")
    return False


def check_write(conn: sqlite3.Connection, entity: str, row, actor_id: int) -> None:
    if not can_write(conn, entity, row, actor_id):
        raise Forbidden(f"{entity} {row['id']}: only its owner, assignee, project members, the owner's lead "
                        "or the company owner change it")


def share(conn: sqlite3.Connection, entity: str, entity_id: int, with_actor: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO shares (entity, entity_id, actor_id) VALUES (?, ?, ?)",
        (entity, entity_id, with_actor),
    )


SHAREABLE = {"task": "tasks", "note": "notes", "file": "files", "project": "projects"}


def share_item(conn: sqlite3.Connection, ctx, entity: str, entity_id: int, with_ref) -> list[int]:
    """Share a private item with a member, or with every member of a project
    ("project:<slug>"). Only its owner or the company owner. Returns who got it."""
    from . import actors, audit

    if entity not in SHAREABLE:
        raise Forbidden(f"{entity} cannot be shared")
    row = conn.execute(f"SELECT * FROM {SHAREABLE[entity]} WHERE id = ?", (entity_id,)).fetchone()
    if row is None:
        raise Forbidden(f"no {entity} {entity_id}")
    if not (actors.get(conn, ctx.actor_id)["is_owner"] or row["owner_id"] == ctx.actor_id):
        raise Forbidden("only its owner shares it")
    ref = str(with_ref)
    if ref.startswith("project:"):
        from .projects import _row as project_row, members

        ids = [m["actor_id"] for m in members(conn, project_row(conn, ctx, ref.split(":", 1)[1])["id"])]
    else:
        from .org import _member

        ids = [_member(conn, with_ref)["id"]]
    for aid in ids:
        share(conn, entity, entity_id, aid)
    audit.log(conn, ctx, "share", entity, entity_id, with_=ids)
    return ids


def shared_with(conn: sqlite3.Connection, entity: str, entity_id: int) -> list[dict]:
    rows = conn.execute("""SELECT a.id, a.name FROM shares s JOIN actors a ON a.id = s.actor_id
                           WHERE s.entity = ? AND s.entity_id = ? ORDER BY a.name""", (entity, entity_id))
    return [dict(r) for r in rows]


def unshare(conn: sqlite3.Connection, ctx, entity: str, entity_id: int, actor_id: int) -> None:
    from . import actors, audit

    row = conn.execute(f"SELECT * FROM {SHAREABLE[entity]} WHERE id = ?", (entity_id,)).fetchone()
    if row is None or not (actors.get(conn, ctx.actor_id)["is_owner"] or row["owner_id"] == ctx.actor_id):
        raise Forbidden("only its owner stops sharing it")
    conn.execute("DELETE FROM shares WHERE entity = ? AND entity_id = ? AND actor_id = ?", (entity, entity_id, actor_id))
    audit.log(conn, ctx, "unshare", entity, entity_id, actor=actor_id)
