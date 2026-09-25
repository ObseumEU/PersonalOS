"""SQLite tables owned by the budget agent.

They live in the main PersonalOS database but are created here with
`CREATE TABLE IF NOT EXISTS`, so this module never edits the core migration
list in `pos.db`. When the core offers per-module migrations, move them there.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .codex_usage import LimitSnapshot, TokenUsage

SCHEMA = """
CREATE TABLE IF NOT EXISTS budget_runs (
    id                 INTEGER PRIMARY KEY,
    at                 TEXT NOT NULL,           -- ISO time the run finished
    thread_id          TEXT UNIQUE,             -- Codex thread id, dedupes exec vs. session log
    agent_id           TEXT,                    -- NULL = not started by PersonalOS (e.g. owner's own Codex)
    task_id            TEXT,
    source             TEXT NOT NULL,           -- 'exec' | 'session_log'
    input_tokens       INTEGER NOT NULL,
    cached_input_tokens INTEGER NOT NULL,
    output_tokens      INTEGER NOT NULL,
    reasoning_output_tokens INTEGER NOT NULL,
    billable_tokens    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS budget_runs_at ON budget_runs(at);
CREATE INDEX IF NOT EXISTS budget_runs_agent ON budget_runs(agent_id, at);

CREATE TABLE IF NOT EXISTS budget_limit_snapshots (
    at             TEXT NOT NULL,
    window         TEXT NOT NULL,              -- 'primary' | 'secondary'
    used_percent   REAL NOT NULL,
    window_minutes INTEGER,
    resets_at      INTEGER,
    plan_type      TEXT,
    PRIMARY KEY (at, window)
);

-- Per-agent budget class and the cap the last check assigned.
CREATE TABLE IF NOT EXISTS budget_agents (
    agent_id      TEXT PRIMARY KEY,
    budget_class  TEXT NOT NULL DEFAULT 'normal',   -- 'system' | 'normal' | 'low'
    daily_cap     INTEGER,                          -- billable tokens per day, NULL = no cap yet
    updated_at    TEXT NOT NULL
);

-- Result of each hourly check (latest row = current state).
CREATE TABLE IF NOT EXISTS budget_checks (
    id        INTEGER PRIMARY KEY,
    at        TEXT NOT NULL,
    level     TEXT NOT NULL,
    report    TEXT NOT NULL                        -- JSON
);

-- Files already read from $CODEX_HOME/sessions, so a rescan only reads new data.
CREATE TABLE IF NOT EXISTS budget_ingested_logs (
    path   TEXT PRIMARY KEY,
    mtime  REAL NOT NULL
);
"""

BUDGET_CLASSES = ("system", "normal", "low")


def ensure_schema(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript(SCHEMA)


def iso(at: datetime) -> str:
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at.astimezone(timezone.utc).isoformat(timespec="seconds")


def record_run(
    conn: sqlite3.Connection,
    *,
    at: datetime,
    usage: TokenUsage,
    source: str,
    thread_id: str | None = None,
    agent_id: str | None = None,
    task_id: str | None = None,
) -> None:
    """Insert a run; a later record for the same thread updates tokens but keeps who ran it."""
    with conn:
        conn.execute(
            """
            INSERT INTO budget_runs (at, thread_id, agent_id, task_id, source, input_tokens,
                cached_input_tokens, output_tokens, reasoning_output_tokens, billable_tokens)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(thread_id) DO UPDATE SET
                at = excluded.at,
                agent_id = COALESCE(budget_runs.agent_id, excluded.agent_id),
                task_id = COALESCE(budget_runs.task_id, excluded.task_id),
                input_tokens = MAX(budget_runs.input_tokens, excluded.input_tokens),
                cached_input_tokens = MAX(budget_runs.cached_input_tokens, excluded.cached_input_tokens),
                output_tokens = MAX(budget_runs.output_tokens, excluded.output_tokens),
                reasoning_output_tokens = MAX(budget_runs.reasoning_output_tokens,
                                              excluded.reasoning_output_tokens),
                billable_tokens = MAX(budget_runs.billable_tokens, excluded.billable_tokens)
            """,
            (
                iso(at), thread_id, agent_id, task_id, source, usage.input_tokens,
                usage.cached_input_tokens, usage.output_tokens, usage.reasoning_output_tokens,
                usage.billable,
            ),
        )


def record_snapshot(conn: sqlite3.Connection, snap: LimitSnapshot) -> None:
    with conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO budget_limit_snapshots
                (at, window, used_percent, window_minutes, resets_at, plan_type)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (iso(snap.at), w.name, w.used_percent, w.window_minutes, w.resets_at, snap.plan_type)
                for w in snap.windows
            ],
        )


def snapshots(conn: sqlite3.Connection, window: str, since: datetime) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM budget_limit_snapshots WHERE window = ? AND at >= ? ORDER BY at",
        (window, iso(since)),
    ).fetchall()


def tokens_between(
    conn: sqlite3.Connection, start: datetime, end: datetime, agent_id: str | None = None
) -> int:
    sql = "SELECT COALESCE(SUM(billable_tokens), 0) FROM budget_runs WHERE at >= ? AND at < ?"
    args: list = [iso(start), iso(end)]
    if agent_id is not None:
        sql += " AND agent_id = ?"
        args.append(agent_id)
    return conn.execute(sql, args).fetchone()[0]


def tokens_by_agent(conn: sqlite3.Connection, start: datetime, end: datetime) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT COALESCE(agent_id, '') AS agent, SUM(billable_tokens) AS tokens
        FROM budget_runs WHERE at >= ? AND at < ? GROUP BY agent
        """,
        (iso(start), iso(end)),
    ).fetchall()
    return {row["agent"]: row["tokens"] for row in rows}


def set_agent_class(conn: sqlite3.Connection, agent_id: str, budget_class: str, now: datetime) -> None:
    if budget_class not in BUDGET_CLASSES:
        raise ValueError(f"budget_class must be one of {BUDGET_CLASSES}")
    with conn:
        conn.execute(
            """
            INSERT INTO budget_agents (agent_id, budget_class, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(agent_id) DO UPDATE SET budget_class = excluded.budget_class,
                                                updated_at = excluded.updated_at
            """,
            (agent_id, budget_class, iso(now)),
        )


def agents(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {row["agent_id"]: row for row in conn.execute("SELECT * FROM budget_agents")}


def set_caps(conn: sqlite3.Connection, caps: dict[str, int | None], now: datetime) -> None:
    with conn:
        for agent_id, cap in caps.items():
            conn.execute(
                """
                INSERT INTO budget_agents (agent_id, daily_cap, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET daily_cap = excluded.daily_cap,
                                                    updated_at = excluded.updated_at
                """,
                (agent_id, cap, iso(now)),
            )


def save_check(conn: sqlite3.Connection, at: datetime, level: str, report_json: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO budget_checks (at, level, report) VALUES (?, ?, ?)",
            (iso(at), level, report_json),
        )


def latest_check(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM budget_checks ORDER BY id DESC LIMIT 1").fetchone()


def ingested_mtime(conn: sqlite3.Connection, path: str) -> float | None:
    row = conn.execute("SELECT mtime FROM budget_ingested_logs WHERE path = ?", (path,)).fetchone()
    return row[0] if row else None


def mark_ingested(conn: sqlite3.Connection, path: str, mtime: float) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO budget_ingested_logs (path, mtime) VALUES (?, ?)", (path, mtime)
        )
