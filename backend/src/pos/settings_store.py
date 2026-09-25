"""Settings changed at run time (the owner, or an approval handler), versioned
like tasks so every change has a history and can be restored. Values are JSON."""

import json
import sqlite3

from . import versioning
from .core import Ctx, now_iso

ENTITY = "setting"
versioning.register(ENTITY, "settings")


def get(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ? AND archived_at IS NULL", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def put(conn: sqlite3.Connection, ctx: Ctx, key: str, value, action: str = "update") -> None:
    """Set `key` (the caller commits)."""
    raw = json.dumps(value, ensure_ascii=False)
    row = conn.execute("SELECT id FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        now = now_iso()
        versioning.insert(conn, ctx, ENTITY, {"key": key, "value": raw, "created_by": ctx.actor_id,
                                              "created_at": now, "updated_at": now})
    else:
        versioning.update(conn, ctx, ENTITY, row["id"], {"value": raw, "archived_at": None}, action=action)
