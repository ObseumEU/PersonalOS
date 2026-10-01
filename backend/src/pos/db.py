"""SQLite storage with ordered, append-only migrations."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path


def _has_fts5() -> bool:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


def has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone() is not None


# Full-text search for files and notes. SQLite builds without FTS5 skip these
# tables; pos.notes then falls back to LIKE search. files_fts is dropped again
# by migration 18: files are searched through knowlage (pos.kb_files).
HAS_FTS5 = _has_fts5()
_FTS_SQL = """
    CREATE VIRTUAL TABLE files_fts USING fts5(name, text_extract, content='files', content_rowid='id');
    CREATE TRIGGER files_fts_ai AFTER INSERT ON files BEGIN
        INSERT INTO files_fts (rowid, name, text_extract) VALUES (new.id, new.name, new.text_extract);
    END;
    CREATE TRIGGER files_fts_ad AFTER DELETE ON files BEGIN
        INSERT INTO files_fts (files_fts, rowid, name, text_extract)
        VALUES ('delete', old.id, old.name, old.text_extract);
    END;
    CREATE TRIGGER files_fts_au AFTER UPDATE OF name, text_extract ON files BEGIN
        INSERT INTO files_fts (files_fts, rowid, name, text_extract)
        VALUES ('delete', old.id, old.name, old.text_extract);
        INSERT INTO files_fts (rowid, name, text_extract) VALUES (new.id, new.name, new.text_extract);
    END;
    INSERT INTO files_fts (files_fts) VALUES ('rebuild');
    CREATE VIRTUAL TABLE notes_fts USING fts5(title, body, content='notes', content_rowid='id');
    CREATE TRIGGER notes_fts_ai AFTER INSERT ON notes BEGIN
        INSERT INTO notes_fts (rowid, title, body) VALUES (new.id, new.title, new.body);
    END;
    CREATE TRIGGER notes_fts_ad AFTER DELETE ON notes BEGIN
        INSERT INTO notes_fts (notes_fts, rowid, title, body) VALUES ('delete', old.id, old.title, old.body);
    END;
    CREATE TRIGGER notes_fts_au AFTER UPDATE OF title, body ON notes BEGIN
        INSERT INTO notes_fts (notes_fts, rowid, title, body) VALUES ('delete', old.id, old.title, old.body);
        INSERT INTO notes_fts (rowid, title, body) VALUES (new.id, new.title, new.body);
    END;
    INSERT INTO notes_fts (notes_fts) VALUES ('rebuild');
"""

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
    # 7: deploys by the self-deploying pipeline (step 6).
    """
    CREATE TABLE deploys (
        id           INTEGER PRIMARY KEY,
        old_sha      TEXT NOT NULL,
        new_sha      TEXT NOT NULL,
        status       TEXT NOT NULL CHECK (status IN ('ok', 'reverted', 'rejected', 'error')),
        stage        TEXT NOT NULL DEFAULT '',
        log          TEXT NOT NULL DEFAULT '',
        author       TEXT NOT NULL DEFAULT '',
        reverted_sha TEXT,
        commits      INTEGER NOT NULL DEFAULT 0,
        task_id      INTEGER REFERENCES tasks(id),
        created_at   TEXT NOT NULL
    );
    """,
    # 8: two agent runtimes (Codex CLI, Claude Code CLI) with separate accounting.
    """
    ALTER TABLE actors ADD COLUMN engine TEXT;
    ALTER TABLE actors ADD COLUMN model TEXT;
    ALTER TABLE runs ADD COLUMN engine TEXT;
    CREATE TABLE engine_usage (
        id            INTEGER PRIMARY KEY,
        at            TEXT NOT NULL,
        engine        TEXT NOT NULL,
        actor_id      INTEGER REFERENCES actors(id),
        task_id       INTEGER,
        run_id        INTEGER REFERENCES runs(id),
        input_tokens  INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd      REAL NOT NULL DEFAULT 0
    );
    CREATE TABLE engine_limits (
        engine       TEXT PRIMARY KEY,
        paused_until TEXT,
        reason       TEXT,
        window       TEXT,
        resets_at    TEXT,
        state        TEXT,
        updated_at   TEXT NOT NULL
    );
    """,
    # 9: run heartbeats, so runs of a worker that died are released.
    """
    ALTER TABLE runs ADD COLUMN heartbeat_at TEXT;
    """,
    # 10: schedules that people and agents create for themselves or their team.
    """
    CREATE TABLE schedules (
        id           INTEGER PRIMARY KEY,
        name         TEXT NOT NULL,
        schedule     TEXT NOT NULL,
        template     TEXT NOT NULL DEFAULT '{}',
        assignee_id  INTEGER NOT NULL REFERENCES actors(id),
        visibility   TEXT NOT NULL DEFAULT 'personal',
        status       TEXT NOT NULL DEFAULT 'active',
        next_run_at  TEXT,
        last_run_at  TEXT,
        last_result  TEXT,
        last_task_id INTEGER REFERENCES tasks(id),
        runs         INTEGER NOT NULL DEFAULT 0,
        created_by   INTEGER NOT NULL REFERENCES actors(id),
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL,
        archived_at  TEXT
    );
    CREATE INDEX schedules_due ON schedules(status, next_run_at);
    """,
    # 11: the model a run actually used (after a fallback, the fallback's model).
    """
    ALTER TABLE runs ADD COLUMN model TEXT;
    """,
    # 12: org structure (role, reports_to, team) and handoffs
    """
    ALTER TABLE actors ADD COLUMN role TEXT;
    ALTER TABLE actors ADD COLUMN reports_to INTEGER REFERENCES actors(id);
    ALTER TABLE actors ADD COLUMN team TEXT;
    CREATE TABLE handoffs (
        id         INTEGER PRIMARY KEY,
        task_id    INTEGER NOT NULL REFERENCES tasks(id),
        from_actor INTEGER NOT NULL REFERENCES actors(id),
        to_actor   INTEGER NOT NULL REFERENCES actors(id),
        note       TEXT NOT NULL DEFAULT '',
        message_id INTEGER REFERENCES chat_messages(id),
        run_id     INTEGER REFERENCES runs(id),
        created_at TEXT NOT NULL
    );
    CREATE INDEX handoffs_at ON handoffs (created_at);
    """,
    # 13: tool library (publications, usage)
    """
    CREATE TABLE tool_publications (
        id          INTEGER PRIMARY KEY,
        tool        TEXT NOT NULL,
        from_agent  INTEGER REFERENCES actors(id),
        version     TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'rejected', 'published')),
        findings    TEXT NOT NULL DEFAULT '[]',
        created_at  TEXT NOT NULL,
        decided_at  TEXT
    );
    CREATE TABLE tool_usage (
        id       INTEGER PRIMARY KEY,
        tool     TEXT NOT NULL,
        actor_id INTEGER REFERENCES actors(id),
        run_id   INTEGER,
        at       TEXT NOT NULL,
        ok       INTEGER NOT NULL DEFAULT 1
    );
    CREATE INDEX tool_usage_tool ON tool_usage(tool, at);
    """,
    # 14: chat (channels, members, messages, reactions, reads). The agent
    #     messages from migrations 3-4 become DM channels; `messages` stays as
    #     the frozen legacy copy (archive, never delete). `chat_inbox` is the
    #     per-recipient delivery a worker reads (read, ack, delivered_in_run).
    """
    CREATE TABLE channels (
        id          INTEGER PRIMARY KEY,
        kind        TEXT NOT NULL CHECK (kind IN ('dm', 'group')),
        name        TEXT,
        topic       TEXT NOT NULL DEFAULT '',
        visibility  TEXT NOT NULL DEFAULT 'team' CHECK (visibility IN ('public', 'team', 'private')),
        dm_key      TEXT UNIQUE,
        created_by  INTEGER REFERENCES actors(id),
        created_at  TEXT NOT NULL,
        archived_at TEXT
    );
    CREATE UNIQUE INDEX channels_group_name ON channels (name COLLATE NOCASE)
        WHERE kind = 'group' AND archived_at IS NULL;
    CREATE TABLE channel_members (
        channel_id           INTEGER NOT NULL REFERENCES channels(id),
        actor_id             INTEGER NOT NULL REFERENCES actors(id),
        role                 TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('owner', 'member')),
        joined_at            TEXT NOT NULL,
        last_read_message_id INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (channel_id, actor_id)
    );
    CREATE INDEX channel_members_actor ON channel_members (actor_id);
    CREATE TABLE chat_messages (
        id          INTEGER PRIMARY KEY,
        channel_id  INTEGER NOT NULL REFERENCES channels(id),
        author_id   INTEGER NOT NULL REFERENCES actors(id),
        body        TEXT NOT NULL,
        reply_to    INTEGER REFERENCES chat_messages(id),
        mentions    TEXT NOT NULL DEFAULT '[]',
        attachments TEXT NOT NULL DEFAULT '[]',
        priority    TEXT CHECK (priority IN ('fyi', 'change_plan', 'stop')),
        trust       TEXT NOT NULL CHECK (trust IN ('owner', 'person', 'agent', 'external')),
        created_at  TEXT NOT NULL,
        edited_at   TEXT,
        archived_at TEXT
    );
    CREATE INDEX chat_messages_channel ON chat_messages (channel_id, id);
    CREATE INDEX chat_messages_author ON chat_messages (author_id, created_at);
    CREATE TABLE chat_reactions (
        message_id  INTEGER NOT NULL REFERENCES chat_messages(id),
        actor_id    INTEGER NOT NULL REFERENCES actors(id),
        emoji       TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        archived_at TEXT,
        PRIMARY KEY (message_id, actor_id, emoji)
    );
    CREATE TABLE chat_inbox (
        message_id       INTEGER NOT NULL REFERENCES chat_messages(id),
        actor_id         INTEGER NOT NULL REFERENCES actors(id),
        reason           TEXT NOT NULL CHECK (reason IN ('dm', 'mention', 'priority', 'reply')),
        run_id           INTEGER REFERENCES runs(id),
        read_at          TEXT,
        acked_at         TEXT,
        delivered_in_run INTEGER REFERENCES runs(id),
        PRIMARY KEY (message_id, actor_id)
    );
    CREATE INDEX chat_inbox_unread ON chat_inbox (actor_id, read_at);

    INSERT INTO channels (kind, visibility, dm_key, created_by, created_at)
    SELECT 'dm', 'private', MIN(from_actor, to_actor) || ':' || MAX(from_actor, to_actor),
           MIN(from_actor, to_actor), MIN(created_at)
    FROM messages GROUP BY MIN(from_actor, to_actor), MAX(from_actor, to_actor);
    INSERT INTO channel_members (channel_id, actor_id, joined_at)
    SELECT id, CAST(substr(dm_key, 1, instr(dm_key, ':') - 1) AS INTEGER), created_at FROM channels;
    INSERT OR IGNORE INTO channel_members (channel_id, actor_id, joined_at)
    SELECT id, CAST(substr(dm_key, instr(dm_key, ':') + 1) AS INTEGER), created_at FROM channels;
    INSERT INTO chat_messages (id, channel_id, author_id, body, attachments, priority, trust, created_at)
    SELECT m.id, c.id, m.from_actor, m.body,
           CASE WHEN m.task_id IS NULL THEN '[]'
                ELSE json_array(json_object('type', 'task', 'id', m.task_id)) END,
           m.priority,
           CASE WHEN a.is_owner = 1 THEN 'owner' WHEN a.kind = 'human' THEN 'person' ELSE 'agent' END,
           m.created_at
    FROM messages m JOIN actors a ON a.id = m.from_actor
    JOIN channels c ON c.dm_key = MIN(m.from_actor, m.to_actor) || ':' || MAX(m.from_actor, m.to_actor);
    INSERT INTO chat_inbox (message_id, actor_id, reason, run_id, read_at, acked_at, delivered_in_run)
    SELECT id, to_actor, 'dm', run_id, read_at, acked_at, delivered_in_run FROM messages;
    UPDATE channel_members SET last_read_message_id = COALESCE((
        SELECT MAX(cm.id) FROM chat_messages cm
        WHERE cm.channel_id = channel_members.channel_id AND (cm.author_id = channel_members.actor_id
              OR EXISTS (SELECT 1 FROM chat_inbox i WHERE i.message_id = cm.id
                         AND i.actor_id = channel_members.actor_id AND i.read_at IS NOT NULL))), 0);
    """,
    # 15: files, notes and topics (PLAN 3, items 2, 3 and 6). The files and
    #     notes tables exist since migration 2; this adds what they were missing,
    #     the topics table, and full-text search (_FTS_SQL, when FTS5 exists).
    """
    ALTER TABLE files ADD COLUMN sha256 TEXT;
    ALTER TABLE files ADD COLUMN tags TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE files ADD COLUMN created_by INTEGER REFERENCES actors(id);
    ALTER TABLE files ADD COLUMN text_extract TEXT NOT NULL DEFAULT '';
    CREATE INDEX files_sha256 ON files (sha256);
    CREATE INDEX files_topic ON files (topic);
    ALTER TABLE notes ADD COLUMN tags TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE notes ADD COLUMN created_by INTEGER REFERENCES actors(id);
    CREATE INDEX notes_topic ON notes (topic);
    CREATE TABLE topics (
        id          INTEGER PRIMARY KEY,
        slug        TEXT NOT NULL UNIQUE,
        name        TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        color       TEXT,
        created_by  INTEGER REFERENCES actors(id),
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        archived_at TEXT
    );
    """ + (_FTS_SQL if HAS_FTS5 else ""),
    # 16: how much work a run was: tool calls and model turns (the Agent coach, the model badge).
    """
    ALTER TABLE runs ADD COLUMN tool_calls INTEGER;
    ALTER TABLE runs ADD COLUMN turns INTEGER;
    """,
    # 17: task descriptions PersonalOS wrote itself (pos.task_descriptions), told apart from real ones.
    """
    ALTER TABLE tasks ADD COLUMN description_generated INTEGER NOT NULL DEFAULT 0;
    """,
    # 18: files are searched through knowlage (pos.kb_files), not a second index here:
    #     each file remembers its knowlage document and how pushing it went; files_fts goes.
    """
    ALTER TABLE files ADD COLUMN kb_doc_id TEXT;
    ALTER TABLE files ADD COLUMN kb_status TEXT;
    ALTER TABLE files ADD COLUMN kb_error TEXT;
    ALTER TABLE files ADD COLUMN kb_attempts INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE files ADD COLUMN kb_synced_at TEXT;
    CREATE INDEX files_kb_doc ON files (kb_doc_id);
    DROP TRIGGER IF EXISTS files_fts_ai;
    DROP TRIGGER IF EXISTS files_fts_ad;
    DROP TRIGGER IF EXISTS files_fts_au;
    DROP TABLE IF EXISTS files_fts;
    """,
    # 19: settings the owner changes at run time (pos.settings_store), versioned like tasks,
    #     e.g. hr.max_active_agents raised by an approved raise_agent_limit.
    """
    CREATE TABLE settings (
        id INTEGER PRIMARY KEY,
        key TEXT NOT NULL UNIQUE,
        value TEXT NOT NULL,
        created_by INTEGER REFERENCES actors(id),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        archived_at TEXT
    );
    """,
    # 20: a task's activity (pos.comments): comments, returns, reviews, handoffs and
    #     progress, so nothing is lost when progress_note is overwritten.
    """
    CREATE TABLE task_comments (
        id INTEGER PRIMARY KEY,
        task_id INTEGER NOT NULL REFERENCES tasks(id),
        author_id INTEGER REFERENCES actors(id),
        body TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'comment'
             CHECK (kind IN ('comment', 'return', 'review', 'handoff', 'progress', 'system')),
        created_by INTEGER REFERENCES actors(id),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        archived_at TEXT
    );
    CREATE INDEX task_comments_task ON task_comments (task_id, id);
    """,
    # 21: review between colleagues (REVIZE-FUNKCI 3.2): who reviews a task's result.
    """
    ALTER TABLE tasks ADD COLUMN reviewer_id INTEGER REFERENCES actors(id);
    CREATE INDEX tasks_reviewer ON tasks (reviewer_id, status);
    """,
    # 22: feedback between colleagues (pos.feedback): praise, critique, suggestions.
    """
    CREATE TABLE feedback (
        id INTEGER PRIMARY KEY,
        from_id INTEGER NOT NULL REFERENCES actors(id),
        to_id INTEGER NOT NULL REFERENCES actors(id),
        task_id INTEGER REFERENCES tasks(id),
        kind TEXT NOT NULL CHECK (kind IN ('praise', 'critique', 'suggestion')),
        body TEXT NOT NULL,
        rating INTEGER,
        status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'applied', 'dismissed')),
        resolution TEXT,
        applied_ref TEXT,
        resolved_by INTEGER REFERENCES actors(id),
        resolved_at TEXT,
        created_by INTEGER REFERENCES actors(id),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        archived_at TEXT
    );
    CREATE INDEX feedback_to ON feedback (to_id, status);
    """,
    # 23: hiring a colleague and probation (pos.hiring).
    """
    CREATE TABLE hire_requests (
        id INTEGER PRIMARY KEY,
        requested_by INTEGER NOT NULL REFERENCES actors(id),
        name TEXT NOT NULL,
        purpose TEXT NOT NULL,
        role TEXT,
        lead_id INTEGER REFERENCES actors(id),
        permissions TEXT NOT NULL DEFAULT '[]',
        budget_class TEXT NOT NULL DEFAULT 'normal',
        lifetime TEXT NOT NULL DEFAULT 'long_lived',
        instructions TEXT,
        reason TEXT,
        hr_verdict TEXT,
        needs_owner TEXT,
        decider_id INTEGER REFERENCES actors(id),
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
        decided_by INTEGER REFERENCES actors(id),
        decided_at TEXT,
        decision_note TEXT,
        agent_id INTEGER REFERENCES actors(id),
        created_by INTEGER REFERENCES actors(id),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        archived_at TEXT
    );
    ALTER TABLE actors ADD COLUMN probation_until TEXT;
    """,
    # 24: projects as shared work (pos.projects): lead, members, channel; tasks belong to one.
    """
    CREATE TABLE projects (
        id INTEGER PRIMARY KEY,
        slug TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        goal TEXT,
        definition_of_done TEXT,
        lead_id INTEGER REFERENCES actors(id),
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'done', 'archived')),
        visibility TEXT NOT NULL DEFAULT 'team' CHECK (visibility IN ('public', 'team', 'private')),
        owner_id INTEGER NOT NULL REFERENCES actors(id),
        labels TEXT NOT NULL DEFAULT '[]',
        channel_id INTEGER,
        due TEXT,
        created_by INTEGER REFERENCES actors(id),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        archived_at TEXT
    );
    CREATE TABLE project_members (
        project_id INTEGER NOT NULL REFERENCES projects(id),
        actor_id INTEGER NOT NULL REFERENCES actors(id),
        role TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('lead', 'member')),
        added_at TEXT NOT NULL,
        PRIMARY KEY (project_id, actor_id)
    );
    ALTER TABLE tasks ADD COLUMN project_id INTEGER REFERENCES projects(id);
    CREATE INDEX tasks_project ON tasks (project_id, status);
    """,
    # 25: accounts for people (pos.accounts): e-mail login and invitations.
    """
    CREATE TABLE human_accounts (
        actor_id INTEGER PRIMARY KEY REFERENCES actors(id),
        email TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        disabled INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        last_login_at TEXT
    );
    CREATE TABLE invites (
        id INTEGER PRIMARY KEY,
        token_hash TEXT NOT NULL UNIQUE,
        email TEXT NOT NULL,
        name TEXT NOT NULL,
        role TEXT,
        reports_to INTEGER REFERENCES actors(id),
        permissions TEXT NOT NULL DEFAULT '[]',
        invited_by INTEGER NOT NULL REFERENCES actors(id),
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        used_at TEXT,
        actor_id INTEGER REFERENCES actors(id)
    );
    """,
    # 26: topics have a visibility like everything else (a private topic shows only to its owner).
    """
    ALTER TABLE topics ADD COLUMN visibility TEXT NOT NULL DEFAULT 'team';
    ALTER TABLE topics ADD COLUMN owner_id INTEGER REFERENCES actors(id);
    """,
    # 27: what a run really cost: prompt-cache reads and writes (Claude), and the run's cost.
    """
    ALTER TABLE engine_usage ADD COLUMN cache_read_tokens INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE engine_usage ADD COLUMN cache_creation_tokens INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE runs ADD COLUMN cache_read_tokens INTEGER;
    ALTER TABLE runs ADD COLUMN cost_usd REAL;
    """,
    # 28: back-off: a task an agent handed back or failed is not offered to it again before this
    # time, unless a person reassigns it or a new event on it arrives (pos.api_worker, pos.routing).
    """
    ALTER TABLE tasks ADD COLUMN retry_after TEXT;
    """,
    # 29: why a deploy did not ship, in one line (pos.selfdeploy.reason_of; GET /api/deploys/health).
    """
    ALTER TABLE deploys ADD COLUMN reason TEXT NOT NULL DEFAULT '';
    """,
    # 30: a DM is one flat conversation (pos.chat.send): an answer there quotes the message it answers
    # (quote_of) instead of opening a thread. Threads in channels keep a read marker per person
    # (chat_thread_reads; root_id 0 is the baseline: everything before this migration counts as read).
    """
    ALTER TABLE chat_messages ADD COLUMN quote_of INTEGER REFERENCES chat_messages(id);
    CREATE TABLE chat_thread_reads (
        actor_id     INTEGER NOT NULL REFERENCES actors(id),
        root_id      INTEGER NOT NULL,
        last_read_id INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (actor_id, root_id)
    );
    INSERT INTO chat_thread_reads (actor_id, root_id, last_read_id)
        SELECT id, 0, (SELECT COALESCE(MAX(id), 0) FROM chat_messages) FROM actors WHERE kind = 'human';
    """,
    # 31: files agents make and share (pos.agent_files): a one-line description, where it came from
    # (origin, e.g. sandbox:<agent>:<path>, so sharing the same path again is a new version), quota by creator.
    """
    ALTER TABLE files ADD COLUMN description TEXT NOT NULL DEFAULT '';
    ALTER TABLE files ADD COLUMN origin TEXT;
    CREATE INDEX files_created_by ON files (created_by);
    CREATE INDEX files_origin ON files (origin);
    """,
]


BUSY_TIMEOUT_MS = 10_000  # a writer waits this long for the lock before "database is locked"


def connect(db_path: Path) -> sqlite3.Connection:
    """One connection with the platform's settings: foreign keys enforced (the schema's
    REFERENCES), WAL (readers never block the writer), synchronous=NORMAL (safe with WAL:
    a power cut can lose the last commits, never corrupt the file; no fsync per commit), and
    a 10 s busy timeout so a write that meets another one waits instead of failing (the
    worker heartbeat 500 of run 143)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
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
