"""Version history, archive-instead-of-delete, and rollback (AGENTS-SPEC 5.4).

Any module can make a table versioned by calling `register(entity, table)`.
Writes then go through `insert`, `update` and `archive`, which store a full
snapshot of the row after each change, tagged with the actor and run. That
gives three kinds of undo:

- `restore(entity, id, version)`: put one row back to an earlier version;
- `rollback_run(run_id)`: undo everything one run changed;
- archiving instead of deleting, with `unarchive`.
"""

import json
import sqlite3
from dataclasses import dataclass

from . import audit
from .core import Ctx, NotFound, now_iso


@dataclass(frozen=True)
class Versioned:
    entity: str
    table: str
    # Columns never restored from a snapshot (identity and bookkeeping).
    frozen: tuple[str, ...] = ("id", "created_at", "created_by")


_REGISTRY: dict[str, Versioned] = {}


def register(entity: str, table: str) -> None:
    _REGISTRY[entity] = Versioned(entity, table)


def spec(entity: str) -> Versioned:
    return _REGISTRY[entity]


def _row(conn: sqlite3.Connection, v: Versioned, entity_id: int) -> dict:
    row = conn.execute(f"SELECT * FROM {v.table} WHERE id = ?", (entity_id,)).fetchone()
    if row is None:
        raise NotFound(f"{v.entity} {entity_id}")
    return dict(row)


def _snapshot(conn: sqlite3.Connection, ctx: Ctx, v: Versioned, entity_id: int, action: str) -> dict:
    data = _row(conn, v, entity_id)
    last = conn.execute(
        "SELECT MAX(version) AS v FROM history WHERE entity = ? AND entity_id = ?", (v.entity, entity_id)
    ).fetchone()["v"]
    conn.execute(
        """INSERT INTO history (entity, entity_id, version, action, data, actor_id, run_id, at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (v.entity, entity_id, (last or 0) + 1, action, json.dumps(data, ensure_ascii=False),
         ctx.actor_id, ctx.run_id, now_iso()),
    )
    return data


def insert(conn: sqlite3.Connection, ctx: Ctx, entity: str, values: dict) -> dict:
    v = spec(entity)
    cols = list(values)
    cur = conn.execute(
        f"INSERT INTO {v.table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
        [values[c] for c in cols],
    )
    row = _snapshot(conn, ctx, v, cur.lastrowid, "create")
    audit.log(conn, ctx, "create", entity, cur.lastrowid)
    return row


def update(conn: sqlite3.Connection, ctx: Ctx, entity: str, entity_id: int, changes: dict,
           action: str = "update") -> dict:
    v = spec(entity)
    before = _row(conn, v, entity_id)
    changed = {k: val for k, val in changes.items() if before.get(k) != val}
    if not changed:
        return before
    if "updated_at" in before:
        changed["updated_at"] = now_iso()
    conn.execute(
        f"UPDATE {v.table} SET {', '.join(f'{k} = ?' for k in changed)} WHERE id = ?",
        [*changed.values(), entity_id],
    )
    row = _snapshot(conn, ctx, v, entity_id, action)
    audit.log(conn, ctx, action, entity, entity_id,
              fields=sorted(k for k in changed if k != "updated_at"))
    return row


def archive(conn: sqlite3.Connection, ctx: Ctx, entity: str, entity_id: int) -> dict:
    return update(conn, ctx, entity, entity_id, {"archived_at": now_iso()}, action="archive")


def unarchive(conn: sqlite3.Connection, ctx: Ctx, entity: str, entity_id: int) -> dict:
    return update(conn, ctx, entity, entity_id, {"archived_at": None}, action="unarchive")


def history(conn: sqlite3.Connection, entity: str, entity_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT h.version, h.action, h.at, h.run_id, h.actor_id, a.name AS actor_name, h.data
           FROM history h LEFT JOIN actors a ON a.id = h.actor_id
           WHERE h.entity = ? AND h.entity_id = ? ORDER BY h.version""",
        (entity, entity_id),
    ).fetchall()
    return [{**dict(r), "data": json.loads(r["data"])} for r in rows]


def restore(conn: sqlite3.Connection, ctx: Ctx, entity: str, entity_id: int, version: int) -> dict:
    v = spec(entity)
    snap = conn.execute(
        "SELECT data FROM history WHERE entity = ? AND entity_id = ? AND version = ?",
        (entity, entity_id, version),
    ).fetchone()
    if snap is None:
        raise NotFound(f"{entity} {entity_id} v{version}")
    data = {k: val for k, val in json.loads(snap["data"]).items() if k not in v.frozen}
    return update(conn, ctx, entity, entity_id, data, action=f"restore:v{version}")


def rollback_run(conn: sqlite3.Connection, ctx: Ctx, run_id: int) -> list[tuple[str, int]]:
    """Undo every change a run made: restore each touched row to the version it
    had before the run; rows the run created are archived. Returns what changed."""
    touched = conn.execute(
        """SELECT entity, entity_id, MIN(version) AS first FROM history
           WHERE run_id = ? GROUP BY entity, entity_id""",
        (run_id,),
    ).fetchall()
    undone = []
    for t in touched:
        if t["entity"] not in _REGISTRY:
            continue
        if t["first"] == 1:
            archive(conn, ctx, t["entity"], t["entity_id"])
        else:
            restore(conn, ctx, t["entity"], t["entity_id"], t["first"] - 1)
        undone.append((t["entity"], t["entity_id"]))
    audit.log(conn, ctx, "rollback_run", "run", run_id, undone=len(undone))
    return undone


for _entity, _table in (("task", "tasks"), ("note", "notes"), ("file", "files"), ("memory", "memories"),
                        ("topic", "topics")):
    register(_entity, _table)
