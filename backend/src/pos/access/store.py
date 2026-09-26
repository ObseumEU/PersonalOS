"""Tables of pos.access.

Like pos.asks and pos.budget they are created here with `CREATE TABLE IF NOT
EXISTS` on first use, so this module never takes a slot in the core migration
list (no collision with a migration added elsewhere at the same time).

A grant or budget row is *active* while it is not ended (revoked, expired or
replaced) and its `expires_at`, if any, is in the future. Rows are never
deleted: the table is the history.
"""

import sqlite3

SCHEMA = """
-- Agents whose permissions are managed here. Until an agent is listed, its
-- permissions come from actors.permissions (older databases, tests).
CREATE TABLE IF NOT EXISTS access_agents (
    agent_id   INTEGER PRIMARY KEY REFERENCES actors(id),
    seeded_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS access_grants (
    id          INTEGER PRIMARY KEY,
    agent_id    INTEGER NOT NULL REFERENCES actors(id),
    capability  TEXT NOT NULL,           -- tasks:write | tool:search | outbound:email.send | scope:repo:x/y
    kind        TEXT NOT NULL,           -- permission | tool | outbound | scope | owner_only
    granted_by  INTEGER REFERENCES actors(id),
    source      TEXT NOT NULL,           -- seed | platform | owner | access_manager
    reason      TEXT NOT NULL,
    request_id  INTEGER,
    created_at  TEXT NOT NULL,
    expires_at  TEXT,                    -- NULL: permanent
    ended_at    TEXT,
    ended_by    INTEGER REFERENCES actors(id),
    end_kind    TEXT,                    -- revoked | expired | replaced
    end_reason  TEXT
);
CREATE INDEX IF NOT EXISTS access_grants_agent ON access_grants (agent_id, capability);

CREATE TABLE IF NOT EXISTS access_budgets (
    id          INTEGER PRIMARY KEY,
    agent_id    INTEGER REFERENCES actors(id),   -- NULL: the company-wide cap (owner only)
    metric      TEXT NOT NULL,                   -- usd_day | usd_month | tokens_day | tokens_month | usd_run | runs_day
    amount      REAL,                            -- NULL: no limit
    granted_by  INTEGER REFERENCES actors(id),
    source      TEXT NOT NULL,
    reason      TEXT NOT NULL,
    request_id  INTEGER,
    created_at  TEXT NOT NULL,
    expires_at  TEXT,
    ended_at    TEXT,
    ended_by    INTEGER REFERENCES actors(id),
    end_kind    TEXT,
    end_reason  TEXT
);
CREATE INDEX IF NOT EXISTS access_budgets_agent ON access_budgets (agent_id, metric);

-- What agents asked for, and what the platform raised on their behalf
-- (a budget limit hit, a spend spike).
CREATE TABLE IF NOT EXISTS access_requests (
    id            INTEGER PRIMARY KEY,
    agent_id      INTEGER NOT NULL REFERENCES actors(id),
    requested_by  INTEGER REFERENCES actors(id),  -- NULL: raised by the platform
    trigger       TEXT NOT NULL DEFAULT 'request', -- request | limit_hit | spike
    what          TEXT NOT NULL,                   -- capability | budget | review
    capability    TEXT,
    metric        TEXT,
    amount        REAL,
    hours         REAL,
    why           TEXT NOT NULL,
    task_id       INTEGER REFERENCES tasks(id),
    blocking      INTEGER NOT NULL DEFAULT 0,
    needs_owner   INTEGER NOT NULL DEFAULT 0,
    detail        TEXT NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL DEFAULT 'pending', -- pending | granted | denied | escalated
    decided_by    INTEGER REFERENCES actors(id),
    decided_at    TEXT,
    decision_note TEXT,
    grant_id      INTEGER,
    budget_id     INTEGER,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS access_requests_status ON access_requests (status, agent_id);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    if not ready(conn):
        with conn:
            conn.executescript(SCHEMA)


def ready(conn: sqlite3.Connection) -> bool:
    """Cheap check used on hot paths (every permission check)."""
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'access_requests'").fetchone() is not None


def seeded(conn: sqlite3.Connection, agent_id: int) -> bool:
    return conn.execute("SELECT 1 FROM access_agents WHERE agent_id = ?", (agent_id,)).fetchone() is not None


ACTIVE = "ended_at IS NULL AND (expires_at IS NULL OR expires_at > ?)"
