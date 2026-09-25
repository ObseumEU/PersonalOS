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


def share(conn: sqlite3.Connection, entity: str, entity_id: int, with_actor: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO shares (entity, entity_id, actor_id) VALUES (?, ?, ?)",
        (entity, entity_id, with_actor),
    )
