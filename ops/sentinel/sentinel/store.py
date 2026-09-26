"""The sentinel's own SQLite: fingerprints and their per-minute counts
(baselines), status counters, check streaks, container state, incidents,
remediations and the outbox of events PersonalOS has not taken yet.

Per-minute rows older than the baseline window are pruned every tick, so the
file stays small."""

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS fp (
    fp TEXT PRIMARY KEY, service TEXT NOT NULL, container TEXT NOT NULL, key TEXT NOT NULL,
    first_seen REAL NOT NULL, last_seen REAL NOT NULL, total INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS fp_min (fp TEXT NOT NULL, minute INTEGER NOT NULL, n INTEGER NOT NULL,
    PRIMARY KEY (fp, minute));
CREATE TABLE IF NOT EXISTS samples (fp TEXT NOT NULL, h TEXT NOT NULL, line TEXT NOT NULL, at REAL NOT NULL,
    PRIMARY KEY (fp, h));
CREATE TABLE IF NOT EXISTS stat_min (container TEXT NOT NULL, metric TEXT NOT NULL, minute INTEGER NOT NULL,
    n INTEGER NOT NULL, PRIMARY KEY (container, metric, minute));
CREATE TABLE IF NOT EXISTS checks (name TEXT PRIMARY KEY, service TEXT, ok INTEGER, fails INTEGER NOT NULL DEFAULT 0,
    last_ok REAL, last_fail REAL, detail TEXT);
CREATE TABLE IF NOT EXISTS containers (name TEXT PRIMARY KEY, restart_count INTEGER, started_at TEXT, status TEXT,
    health TEXT, oom INTEGER, image TEXT, created TEXT, seen REAL);
CREATE TABLE IF NOT EXISTS restarts (container TEXT NOT NULL, at REAL NOT NULL, n INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT, service TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL,
    severity TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', title TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0, opened_at REAL NOT NULL, last_seen REAL NOT NULL, resolved_at REAL,
    escalate_after REAL, escalated_at REAL, notified_level INTEGER NOT NULL DEFAULT -1,
    notified_count INTEGER NOT NULL DEFAULT 0, notified_severity TEXT, remediated_at REAL,
    container TEXT, detail TEXT NOT NULL DEFAULT '{}', notes TEXT NOT NULL DEFAULT '[]', classification TEXT);
CREATE INDEX IF NOT EXISTS incidents_open ON incidents (status, service, kind, key);
CREATE TABLE IF NOT EXISTS remediations (id INTEGER PRIMARY KEY, container TEXT NOT NULL, at REAL NOT NULL,
    reason TEXT, incident_id INTEGER, ok INTEGER);
CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY, path TEXT NOT NULL, payload TEXT NOT NULL,
    created_at REAL NOT NULL, tries INTEGER NOT NULL DEFAULT 0, last_error TEXT);
"""

SEVERITIES = ("low", "medium", "high", "critical")


def sev_rank(s: str | None) -> int:
    return SEVERITIES.index(s) if s in SEVERITIES else -1


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        if self.meta("created_at") is None:
            self.set_meta("created_at", time.time())

    # ------------------------------------------------------------- meta
    def meta(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_meta(self, key: str, value) -> None:
        self.db.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                        (key, json.dumps(value)))

    def q(self, sql: str, *args) -> list[sqlite3.Row]:
        return self.db.execute(sql, args).fetchall()

    def one(self, sql: str, *args) -> sqlite3.Row | None:
        return self.db.execute(sql, args).fetchone()

    def x(self, sql: str, *args) -> int:
        return self.db.execute(sql, args).lastrowid

    # ------------------------------------------------------------- fingerprints
    def count_fp(self, fp: str, service: str, container: str, key: str, minute: int, n: int, at: float) -> bool:
        """Add n occurrences; True when the fingerprint is new."""
        new = self.one("SELECT 1 FROM fp WHERE fp = ?", fp) is None
        if new:
            self.x("INSERT INTO fp (fp, service, container, key, first_seen, last_seen, total) VALUES (?, ?, ?, ?, ?, ?, ?)",
                   fp, service, container, key[:400], at, at, n)
        else:
            self.x("UPDATE fp SET last_seen = ?, total = total + ? WHERE fp = ?", at, n, fp)
        self.x("INSERT INTO fp_min (fp, minute, n) VALUES (?, ?, ?) ON CONFLICT (fp, minute) DO UPDATE SET n = n + excluded.n",
               fp, minute, n)
        return new

    def add_sample(self, fp: str, h: str, line: str, at: float, keep: int = 20) -> None:
        if self.one("SELECT 1 FROM samples WHERE fp = ? AND h = ?", fp, h):
            self.x("UPDATE samples SET at = ? WHERE fp = ? AND h = ?", at, fp, h)
            return
        self.x("INSERT INTO samples (fp, h, line, at) VALUES (?, ?, ?, ?)", fp, h, line, at)
        self.x("DELETE FROM samples WHERE fp = ? AND h NOT IN (SELECT h FROM samples WHERE fp = ? ORDER BY at DESC LIMIT ?)",
               fp, fp, keep)

    def samples(self, fp: str, limit: int = 20) -> list[str]:
        return [r["line"] for r in self.q("SELECT line FROM samples WHERE fp = ? ORDER BY at DESC LIMIT ?", fp, limit)]

    def fp_sum(self, fp: str, start_min: int, end_min: int) -> int:
        """Occurrences in minutes [start_min, end_min)."""
        row = self.one("SELECT COALESCE(SUM(n), 0) AS n FROM fp_min WHERE fp = ? AND minute >= ? AND minute < ?",
                       fp, start_min, end_min)
        return int(row["n"])

    def stat(self, container: str, metric: str, minute: int, n: int) -> None:
        self.x("INSERT INTO stat_min (container, metric, minute, n) VALUES (?, ?, ?, ?) "
               "ON CONFLICT (container, metric, minute) DO UPDATE SET n = n + excluded.n", container, metric, minute, n)

    def stat_sum(self, container: str, metric: str, start_min: int, end_min: int) -> int:
        row = self.one("SELECT COALESCE(SUM(n), 0) AS n FROM stat_min WHERE container = ? AND metric = ? "
                       "AND minute >= ? AND minute < ?", container, metric, start_min, end_min)
        return int(row["n"])

    def prune(self, now: float, keep_min: int = 1440 * 2) -> None:
        cutoff = int(now // 60) - keep_min
        self.x("DELETE FROM fp_min WHERE minute < ?", cutoff)
        self.x("DELETE FROM stat_min WHERE minute < ?", cutoff)
        self.x("DELETE FROM restarts WHERE at < ?", now - 86400)
        self.x("DELETE FROM samples WHERE at < ?", now - 7 * 86400)
        self.x("DELETE FROM fp WHERE last_seen < ?", now - 30 * 86400)
        self.x("DELETE FROM incidents WHERE status = 'resolved' AND resolved_at < ?", now - 30 * 86400)
