"""Tables of pos.credentials (created on first use, like pos.access: no numbered migration).

No table here ever holds a secret value: the registry keeps the 1Password
reference (op://vault/item/field), the use log keeps who used what and when,
and a run's injection session keeps only a hash of its token.
"""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS credentials (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,         -- github-deploy: what agents and grants call it
    op_ref         TEXT NOT NULL,                -- op://PersonalOS Agents/GitHub deploy/token
    description    TEXT NOT NULL DEFAULT '',     -- Markdown: what it opens, for whom
    env_var        TEXT,                         -- injected into a command as this variable
    header         TEXT,                         -- HTTP use: 'Authorization: Bearer {value}'
    allowed_hosts  TEXT NOT NULL DEFAULT '[]',   -- JSON: hosts an HTTP call may go to (a.b or *.b)
    allowed_tools  TEXT NOT NULL DEFAULT '[]',   -- JSON: command | http (empty = both)
    allowed_commands TEXT NOT NULL DEFAULT '[]', -- JSON: command prefixes it may be used with ('git push')
    max_uses_hour  INTEGER NOT NULL DEFAULT 60,  -- per agent; above it the grant is paused
    notes          TEXT NOT NULL DEFAULT '',     -- the owner's notes (Markdown)
    created_by     INTEGER REFERENCES actors(id),
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    archived_at    TEXT
);

-- Every resolution, allowed or refused: the audit trail on the credential and agent pages.
CREATE TABLE IF NOT EXISTS credential_uses (
    id             INTEGER PRIMARY KEY,
    at             TEXT NOT NULL,
    credential_id  INTEGER REFERENCES credentials(id),
    name           TEXT NOT NULL,
    agent_id       INTEGER REFERENCES actors(id),  -- NULL: the owner's test
    run_id         INTEGER,
    task_id        INTEGER,
    tool           TEXT NOT NULL,                  -- command | http | test
    host           TEXT,
    ok             INTEGER NOT NULL,
    error          TEXT
);
CREATE INDEX IF NOT EXISTS credential_uses_agent ON credential_uses (agent_id, credential_id, at);
CREATE INDEX IF NOT EXISTS credential_uses_cred ON credential_uses (credential_id, at);

-- A worker run's injection session: the worker-side credential runner proves it
-- belongs to a live run of this agent (the token is shown once, stored hashed).
CREATE TABLE IF NOT EXISTS credential_sessions (
    run_id      INTEGER PRIMARY KEY,
    agent_id    INTEGER NOT NULL REFERENCES actors(id),
    token_hash  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'credential_sessions'").fetchone() is None:
        with conn:
            conn.executescript(SCHEMA)
