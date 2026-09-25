"""SQLite tables owned by the HR agent.

Like pos.budget, they live in the main database but are created here with
`CREATE TABLE IF NOT EXISTS`, so this module never edits the core migration list.
`hr_profiles` holds the agent fields from AGENTS-SPEC 3.1 that the core's
`actors` table does not have yet (its id is the actor id); when step 2 adds them
there, the adapter in pos.hr.platform reads them from `actors` instead. Profiles
and agent archiving are versioned (pos.versioning), so HR's changes can be
restored or rolled back like any other data.
"""

import json
import sqlite3

from .. import versioning
from ..core import Ctx, now_iso

PROFILE = "hr_profile"
ACTOR = "actor"
versioning.register(PROFILE, "hr_profiles")
# Agents are archived, never deleted (spec 3.3); versioning makes that undoable.
versioning.register(ACTOR, "actors")

SCHEMA = """
CREATE TABLE IF NOT EXISTS hr_profiles (
    id          INTEGER PRIMARY KEY REFERENCES actors(id),
    purpose     TEXT NOT NULL DEFAULT '',
    lifetime    TEXT NOT NULL DEFAULT 'long_lived' CHECK (lifetime IN ('one_shot', 'long_lived')),
    created_by  INTEGER REFERENCES actors(id),
    expires_at  TEXT,
    system      INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Every daily review and weekly report, newest last. The weekly report compares
-- against the KPIs of the previous weekly row.
CREATE TABLE IF NOT EXISTS hr_reviews (
    id        INTEGER PRIMARY KEY,
    at        TEXT NOT NULL,
    kind      TEXT NOT NULL CHECK (kind IN ('daily', 'weekly')),
    applied   INTEGER NOT NULL,
    report    TEXT NOT NULL            -- JSON
);
"""

PROFILE_FIELDS = ("purpose", "lifetime", "created_by", "expires_at", "system")


def ensure_schema(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript(SCHEMA)


def profiles(conn: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    return {r["id"]: r for r in conn.execute("SELECT * FROM hr_profiles")}


def get_profile(conn: sqlite3.Connection, actor_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM hr_profiles WHERE id = ?", (actor_id,)).fetchone()


def set_profile(conn: sqlite3.Connection, ctx: Ctx, actor_id: int, action: str = "update", **fields) -> None:
    unknown = set(fields) - set(PROFILE_FIELDS)
    if unknown:
        raise ValueError(f"unknown profile fields: {sorted(unknown)}")
    if get_profile(conn, actor_id) is None:
        now = now_iso()
        versioning.insert(conn, ctx, PROFILE, {"id": actor_id, **fields, "created_at": now, "updated_at": now})
    elif fields:
        versioning.update(conn, ctx, PROFILE, actor_id, fields, action=action)


def save_review(conn: sqlite3.Connection, kind: str, applied: bool, report: dict) -> int:
    cur = conn.execute(
        "INSERT INTO hr_reviews (at, kind, applied, report) VALUES (?, ?, ?, ?)",
        (report["at"], kind, int(applied), json.dumps(report, ensure_ascii=False)),
    )
    return cur.lastrowid


def latest_review(conn: sqlite3.Connection, kind: str | None = None) -> dict | None:
    sql, args = "SELECT report FROM hr_reviews", []
    if kind:
        sql, args = sql + " WHERE kind = ?", [kind]
    row = conn.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()
    return json.loads(row["report"]) if row else None
