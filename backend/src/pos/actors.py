"""Actors (people and agents) and their API keys.

Step 1 keeps this minimal: the owner, the built-in AI assistant, and bearer
keys for MCP clients. Agent lifecycle (create_agent, limits, archiving) builds
on these tables in step 2.
"""

import hashlib
import secrets
import sqlite3

from .core import NotFound, now_iso

OWNER_NAME = "Owner"
ASSISTANT_NAME = "Executive Assistant"
# Renamed in place in the 2026-09 reorganisation (docs/REORG.md): the same actor (the platform's
# system identity, id 2 on existing installs), so its history, DMs and the "ai" alias stay.
LEGACY_ASSISTANT_NAMES = ("Assistant",)


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def ensure_builtin(conn: sqlite3.Connection) -> dict[str, int]:
    """Create the built-in actors once; return their ids by name."""
    ids = {}
    _rename_legacy_assistant(conn)
    # The two subsystems from docs/PLAN.md are members from the start, so tasks
    # can be assigned to them before their A2A bridges exist.
    builtin = ((OWNER_NAME, "human", 1), (ASSISTANT_NAME, "ai", 0),
               ("Knowledge agent", "agent", 0), ("Nexus", "agent", 0),
               # Runs the self-deploy pipeline (pos.selfdeploy) and reports deploys.
               ("Deployer", "agent", 0))
    for name, kind, owner in builtin:
        row = conn.execute("SELECT id FROM actors WHERE name = ?", (name,)).fetchone()
        if row is None:
            cur = conn.execute(
                "INSERT INTO actors (kind, name, is_owner, created_at) VALUES (?, ?, ?, ?)",
                (kind, name, owner, now_iso()),
            )
            ids[name] = cur.lastrowid
        else:
            ids[name] = row["id"]
    conn.commit()
    return ids


def _rename_legacy_assistant(conn: sqlite3.Connection) -> None:
    """The built-in assistant under its old name becomes the Executive Assistant (once)."""
    if conn.execute("SELECT 1 FROM actors WHERE name = ?", (ASSISTANT_NAME,)).fetchone():
        return
    for old in LEGACY_ASSISTANT_NAMES:
        row = conn.execute("SELECT id FROM actors WHERE name = ? AND kind = 'ai'", (old,)).fetchone()
        if row:
            conn.execute("UPDATE actors SET name = ? WHERE id = ?", (ASSISTANT_NAME, row["id"]))
            return


def owner_id(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT id FROM actors WHERE is_owner = 1 ORDER BY id LIMIT 1").fetchone()["id"]


def assistant_id(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT id FROM actors WHERE name = ?", (ASSISTANT_NAME,)).fetchone()["id"]


SYSTEM_NAME = "PersonalOS"


def system_id(conn: sqlite3.Connection) -> int:
    """The platform's own voice for notices (a failed run, a budget stop, an answer recorded): the
    "PersonalOS" member, created on first use as a service (no worker, not on the org chart, never
    woken). A notice is signed by it, never by the owner or by an agent that did not write it
    (prod 2026-10: "Owner handed in T-445 …" and "Owner resolved your ask …" read as his words)."""
    row = conn.execute("SELECT id FROM actors WHERE name = ?", (SYSTEM_NAME,)).fetchone()
    if row is not None:
        return row["id"]
    cur = conn.execute("INSERT INTO actors (kind, name, is_owner, runtime, created_at) VALUES ('agent', ?, 0, "
                       "'service', ?)", (SYSTEM_NAME, now_iso()))
    return cur.lastrowid


def is_system(conn: sqlite3.Connection, actor_id: int | None) -> bool:
    if not actor_id:
        return False
    row = conn.execute("SELECT name, runtime FROM actors WHERE id = ?", (actor_id,)).fetchone()
    return row is not None and row["name"] == SYSTEM_NAME and row["runtime"] == "service"


def get(conn: sqlite3.Connection, actor_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM actors WHERE id = ?", (actor_id,)).fetchone()
    if row is None:
        raise NotFound(f"actor {actor_id}")
    return row


def find_by_name(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM actors WHERE name = ? AND archived_at IS NULL", (name,)
    ).fetchone()


def list_actors(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM actors WHERE archived_at IS NULL ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def create_key(conn: sqlite3.Connection, actor_id: int, label: str = "") -> str:
    """Return a new bearer key; only its hash is stored."""
    key = "pos_" + secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO api_keys (actor_id, key_hash, label, created_at) VALUES (?, ?, ?, ?)",
        (actor_id, _hash(key), label, now_iso()),
    )
    conn.commit()
    return key


def ensure_key(conn: sqlite3.Connection, actor_id: int, key: str, label: str) -> None:
    """Register a key given from configuration (e.g. POS_MCP_TOKEN)."""
    conn.execute(
        "INSERT OR IGNORE INTO api_keys (actor_id, key_hash, label, created_at) VALUES (?, ?, ?, ?)",
        (actor_id, _hash(key), label, now_iso()),
    )
    conn.commit()


def actor_for_key(conn: sqlite3.Connection, key: str) -> int | None:
    row = conn.execute(
        """SELECT k.actor_id FROM api_keys k JOIN actors a ON a.id = k.actor_id
           WHERE k.key_hash = ? AND k.revoked_at IS NULL AND a.archived_at IS NULL""",
        (_hash(key),),
    ).fetchone()
    return row["actor_id"] if row else None
