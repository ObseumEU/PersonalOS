"""SQLite storage with ordered, append-only migrations."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

# Each entry runs once, in order. Never edit a shipped migration; append a new one.
MIGRATIONS: list[str] = [
    """
    CREATE TABLE app_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    # 2: identity, visibility, runs, audit log, version history, tasks and the
    #    other layered content (notes, files, agent memory).
    """
    CREATE TABLE actors (
        id          INTEGER PRIMARY KEY,
        kind        TEXT NOT NULL CHECK (kind IN ('human', 'ai', 'agent')),
        name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
        is_owner    INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT NOT NULL,
        last_seen_at TEXT,
        archived_at TEXT
    );
    CREATE TABLE api_keys (
        id         INTEGER PRIMARY KEY,
        actor_id   INTEGER NOT NULL REFERENCES actors(id),
        key_hash   TEXT NOT NULL UNIQUE,
        label      TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        revoked_at TEXT
    );
    -- Explicit sharing of private items (visibility layer 'private').
    CREATE TABLE shares (
        entity    TEXT NOT NULL,
        entity_id INTEGER NOT NULL,
        actor_id  INTEGER NOT NULL REFERENCES actors(id),
        PRIMARY KEY (entity, entity_id, actor_id)
    );
    CREATE TABLE runs (
        id            INTEGER PRIMARY KEY,
        actor_id      INTEGER NOT NULL REFERENCES actors(id),
        task_id       INTEGER,
        kind          TEXT NOT NULL,
        status        TEXT NOT NULL CHECK (status IN ('running', 'ok', 'error', 'cancelled', 'blocked')),
        started_at    TEXT NOT NULL,
        ended_at      TEXT,
        input_tokens  INTEGER,
        output_tokens INTEGER,
        detail        TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE audit_log (
        id        INTEGER PRIMARY KEY,
        at        TEXT NOT NULL,
        actor_id  INTEGER REFERENCES actors(id),
        run_id    INTEGER REFERENCES runs(id),
        via       TEXT NOT NULL,
        action    TEXT NOT NULL,
        entity    TEXT,
        entity_id INTEGER,
        detail    TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX audit_entity ON audit_log (entity, entity_id);
    CREATE INDEX audit_run ON audit_log (run_id);
    -- Every version of every versioned row; deleting is archiving.
    CREATE TABLE history (
        id        INTEGER PRIMARY KEY,
        entity    TEXT NOT NULL,
        entity_id INTEGER NOT NULL,
        version   INTEGER NOT NULL,
        action    TEXT NOT NULL,
        data      TEXT NOT NULL,
        actor_id  INTEGER REFERENCES actors(id),
        run_id    INTEGER REFERENCES runs(id),
        at        TEXT NOT NULL,
        UNIQUE (entity, entity_id, version)
    );
    CREATE INDEX history_run ON history (run_id);
    CREATE TABLE tasks (
        id                 INTEGER PRIMARY KEY,
        parent_id          INTEGER REFERENCES tasks(id),
        title              TEXT NOT NULL,
        notes              TEXT NOT NULL DEFAULT '',
        status             TEXT NOT NULL DEFAULT 'inbox'
                           CHECK (status IN ('inbox', 'next', 'working', 'review', 'waiting', 'someday', 'done')),
        priority           INTEGER CHECK (priority IN (1, 2, 3)),
        do_date            TEXT,
        deadline           TEXT,
        estimate_min       INTEGER,
        energy             TEXT CHECK (energy IN ('high', 'low')),
        topic              TEXT,
        definition_of_done TEXT,
        visibility         TEXT NOT NULL DEFAULT 'team' CHECK (visibility IN ('public', 'team', 'private')),
        owner_id           INTEGER NOT NULL REFERENCES actors(id),
        assignee_type      TEXT CHECK (assignee_type IN ('human', 'ai', 'agent', 'external')),
        assignee_id        INTEGER REFERENCES actors(id),
        assignee_name      TEXT,
        follow_up          TEXT,
        progress           INTEGER,
        progress_note      TEXT,
        -- Quality signals for the HR agent (docs/HR-AGENT.md): the owner sent
        -- the result back, or had to step in while an agent worked on it.
        returned_count     INTEGER NOT NULL DEFAULT 0,
        interventions      INTEGER NOT NULL DEFAULT 0,
        suggestion         TEXT,
        source             TEXT NOT NULL DEFAULT 'ui',
        position           REAL NOT NULL DEFAULT 0,
        created_by         INTEGER REFERENCES actors(id),
        created_at         TEXT NOT NULL,
        updated_at         TEXT NOT NULL,
        completed_at       TEXT,
        archived_at        TEXT
    );
    CREATE INDEX tasks_parent ON tasks (parent_id);
    CREATE INDEX tasks_status ON tasks (status);
    -- Everything the constitution sends to the owner first (AGENTS-SPEC 6a).
    CREATE TABLE approvals (
        id           INTEGER PRIMARY KEY,
        task_id      INTEGER REFERENCES tasks(id),
        requested_by INTEGER NOT NULL REFERENCES actors(id),
        run_id       INTEGER REFERENCES runs(id),
        action       TEXT NOT NULL,
        details      TEXT NOT NULL DEFAULT '{}',
        status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
        decided_by   INTEGER REFERENCES actors(id),
        decided_at   TEXT,
        comment      TEXT,
        created_at   TEXT NOT NULL
    );
    CREATE TABLE notes (
        id          INTEGER PRIMARY KEY,
        title       TEXT NOT NULL,
        body        TEXT NOT NULL DEFAULT '',
        topic       TEXT,
        visibility  TEXT NOT NULL DEFAULT 'team' CHECK (visibility IN ('public', 'team', 'private')),
        owner_id    INTEGER NOT NULL REFERENCES actors(id),
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        archived_at TEXT
    );
    CREATE TABLE files (
        id          INTEGER PRIMARY KEY,
        name        TEXT NOT NULL,
        path        TEXT NOT NULL,
        mime        TEXT,
        size        INTEGER,
        topic       TEXT,
        visibility  TEXT NOT NULL DEFAULT 'team' CHECK (visibility IN ('public', 'team', 'private')),
        owner_id    INTEGER NOT NULL REFERENCES actors(id),
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        archived_at TEXT
    );
    CREATE TABLE memories (
        id          INTEGER PRIMARY KEY,
        actor_id    INTEGER NOT NULL REFERENCES actors(id),
        body        TEXT NOT NULL,
        visibility  TEXT NOT NULL DEFAULT 'team' CHECK (visibility IN ('public', 'team', 'private')),
        owner_id    INTEGER NOT NULL REFERENCES actors(id),
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        archived_at TEXT
    );
    """,
    # 3: agents as users (AGENTS-SPEC 3.1), the kill switch (5.3) and messages
    #    to agents. Purpose, lifetime and expiry live in pos.hr's profile table.
    """
    ALTER TABLE actors ADD COLUMN created_by INTEGER REFERENCES actors(id);
    ALTER TABLE actors ADD COLUMN runtime TEXT NOT NULL DEFAULT 'builtin';
    ALTER TABLE actors ADD COLUMN a2a_url TEXT;
    ALTER TABLE actors ADD COLUMN instructions_path TEXT;
    ALTER TABLE actors ADD COLUMN permissions TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE actors ADD COLUMN paused_at TEXT;
    ALTER TABLE actors ADD COLUMN updated_at TEXT;
    ALTER TABLE runs ADD COLUMN pid INTEGER;
    CREATE TABLE system_state (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        updated_by INTEGER REFERENCES actors(id)
    );
    CREATE TABLE messages (
        id         INTEGER PRIMARY KEY,
        to_actor   INTEGER NOT NULL REFERENCES actors(id),
        from_actor INTEGER NOT NULL REFERENCES actors(id),
        task_id    INTEGER REFERENCES tasks(id),
        body       TEXT NOT NULL,
        created_at TEXT NOT NULL,
        read_at    TEXT
    );
    CREATE INDEX messages_to ON messages (to_actor, read_at);
    """,
    # 4: agent-to-agent messages with priority and acknowledgement (AGENTS-SPEC 6b).
    """
    ALTER TABLE messages ADD COLUMN priority TEXT NOT NULL DEFAULT 'fyi'
        CHECK (priority IN ('fyi', 'change_plan', 'stop'));
    ALTER TABLE messages ADD COLUMN run_id INTEGER REFERENCES runs(id);
    ALTER TABLE messages ADD COLUMN acked_at TEXT;
    ALTER TABLE messages ADD COLUMN delivered_in_run INTEGER REFERENCES runs(id);
    """,
    # 5: connectors (step 4): routing rules, incoming events, outbound results.
    """
    CREATE TABLE routing_rules (
        id          INTEGER PRIMARY KEY,
        name        TEXT NOT NULL,
        source      TEXT NOT NULL,
        match       TEXT NOT NULL DEFAULT '{}',
        assignee    TEXT,
        priority    INTEGER CHECK (priority IN (1, 2, 3)),
        topic       TEXT,
        enabled     INTEGER NOT NULL DEFAULT 1,
        position    INTEGER NOT NULL DEFAULT 100,
        hits        INTEGER NOT NULL DEFAULT 0,
        created_by  INTEGER REFERENCES actors(id),
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        archived_at TEXT
    );
    CREATE TABLE events (
        id          INTEGER PRIMARY KEY,
        source      TEXT NOT NULL,
        kind        TEXT,
        ref         TEXT,
        title       TEXT NOT NULL,
        payload     TEXT NOT NULL,
        rule_id     INTEGER REFERENCES routing_rules(id),
        task_id     INTEGER REFERENCES tasks(id),
        received_by INTEGER REFERENCES actors(id),
        received_at TEXT NOT NULL,
        signals     TEXT NOT NULL DEFAULT ''
    );
    CREATE UNIQUE INDEX events_ref ON events (source, ref) WHERE ref IS NOT NULL;
    ALTER TABLE approvals ADD COLUMN result TEXT;
    ALTER TABLE approvals ADD COLUMN executed_at TEXT;
    """,
    # 6: scheduler jobs and A2A links to remote agents (step 5).
    """
    CREATE TABLE jobs (
        id          INTEGER PRIMARY KEY,
        name        TEXT NOT NULL,
        schedule    TEXT NOT NULL,
        action      TEXT NOT NULL UNIQUE,
        enabled     INTEGER NOT NULL DEFAULT 1,
        next_run_at TEXT NOT NULL,
        last_run_at TEXT,
        last_result TEXT,
        created_at  TEXT NOT NULL
    );
    CREATE TABLE a2a_links (
        task_id        INTEGER PRIMARY KEY REFERENCES tasks(id),
        member_id      INTEGER NOT NULL REFERENCES actors(id),
        remote_url     TEXT NOT NULL,
        remote_task_id TEXT,
        context_id     TEXT,
        state          TEXT NOT NULL,
        updated_at     TEXT NOT NULL
    );
    """,
]


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations and return the resulting schema version."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for index, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        with conn:
            conn.executescript(sql)
            conn.execute(f"PRAGMA user_version = {index}")
    return conn.execute("PRAGMA user_version").fetchone()[0]


def init_db(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        migrate(conn)
    finally:
        conn.close()


def get_conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()
