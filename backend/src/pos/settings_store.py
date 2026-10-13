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
    """Set `key` (the caller commits). An atomic upsert: the row is claimed with INSERT … ON CONFLICT(key)
    DO NOTHING (which also takes the write lock, waiting out the busy timeout), so two requests setting the same
    new key at once no longer race into "UNIQUE constraint failed: settings.key" (prod 2026-10-13, PUT
    /api/push/prefs while several switches were flipped). Either way the change is versioned."""
    raw = json.dumps(value, ensure_ascii=False)
    now = now_iso()
    cur = conn.execute(
        """INSERT INTO settings (key, value, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(key) DO NOTHING""",
        (key, raw, ctx.actor_id, now, now),
    )
    if cur.rowcount == 1:
        versioning.created(conn, ctx, ENTITY, cur.lastrowid)
        return
    row = conn.execute("SELECT id FROM settings WHERE key = ?", (key,)).fetchone()
    versioning.update(conn, ctx, ENTITY, row["id"], {"value": raw, "archived_at": None}, action=action)


def begin_write(conn: sqlite3.Connection) -> None:
    """Take the write lock before a read-modify-write (BEGIN IMMEDIATE, waiting out the busy timeout): a deferred
    transaction that read first and then writes after another writer committed fails at once with "database is
    locked" (SQLITE_BUSY_SNAPSHOT), and its merge of the earlier read would lose the other write. No-op inside a
    transaction already open."""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
