"""Append-only audit log: who did what, through which channel, in which run."""

import json
import sqlite3

from .core import Ctx, now_iso


def log(conn: sqlite3.Connection, ctx: Ctx, action: str, entity: str | None = None,
        entity_id: int | None = None, **detail) -> None:
    conn.execute(
        """INSERT INTO audit_log (at, actor_id, run_id, via, action, entity, entity_id, detail)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (now_iso(), ctx.actor_id, ctx.run_id, ctx.via, action, entity, entity_id,
         json.dumps(detail, ensure_ascii=False, default=str)),
    )


def entries(conn: sqlite3.Connection, *, entity: str | None = None, entity_id: int | None = None,
            run_id: int | None = None, limit: int = 100) -> list[dict]:
    where, params = [], []
    if entity:
        where.append("l.entity = ?")
        params.append(entity)
    if entity_id is not None:
        where.append("l.entity_id = ?")
        params.append(entity_id)
    if run_id is not None:
        where.append("l.run_id = ?")
        params.append(run_id)
    sql = ("SELECT l.*, a.name AS actor_name FROM audit_log l LEFT JOIN actors a ON a.id = l.actor_id"
           + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY l.id DESC LIMIT ?")
    rows = conn.execute(sql, [*params, limit]).fetchall()
    return [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]
