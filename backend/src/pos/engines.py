"""Agent runtimes: Codex CLI and Claude Code CLI, each on its own subscription.

Every agent has an engine: `claude` (the default, POS_AGENT_RUNTIME), `codex`
or `auto`. `auto` prefers Claude and falls back to Codex when Claude hits its
usage limit, and the other way round. The Claude model is claude-opus-5-5 by
default (POS_CLAUDE_MODEL), configurable per agent. Each subscription has its
own accounting:

- Codex: pos.budget (rolling windows from `codex exec --json` and session logs);
- Claude: here. Usage comes from the `result` event of `claude -p
  --output-format stream-json`; a usage-limit message pauses Claude until the
  reset time it names (or an hour).
"""

import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from .core import now_iso

ENGINES = ("codex", "claude")
CHOICES = ("auto", *ENGINES)


def default_engine() -> str:
    e = os.environ.get("POS_AGENT_RUNTIME", "claude")
    return e if e in CHOICES else "claude"


def default_model(engine: str) -> str | None:
    if engine == "claude":
        return os.environ.get("POS_CLAUDE_MODEL", "claude-opus-5-5")
    return os.environ.get("POS_CODEX_MODEL") or None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ availability

def paused_until(conn: sqlite3.Connection, engine: str) -> str | None:
    row = conn.execute("SELECT paused_until FROM engine_limits WHERE engine = ?", (engine,)).fetchone()
    if row and row["paused_until"] and row["paused_until"] > _utcnow().isoformat(timespec="seconds"):
        return row["paused_until"]
    return None


def can_run(conn: sqlite3.Connection, engine: str, actor_id: int) -> tuple[bool, str]:
    if engine == "codex":
        from .budget import service as budget

        d = budget.can_run(conn, str(actor_id))
        return d.allowed, d.reason
    until = paused_until(conn, engine)
    if until:
        return False, f"{engine} usage limit until {until}"
    return True, "ok"


def choose(conn: sqlite3.Connection, actor_id: int) -> tuple[str | None, str, str | None]:
    """Pick (engine, why, model) for an agent's next run; engine None if none can run."""
    row = conn.execute("SELECT engine, model FROM actors WHERE id = ?", (actor_id,)).fetchone()
    wanted = (row["engine"] if row and row["engine"] else None) or default_engine()
    order = [wanted] if wanted in ENGINES else ["claude", "codex"]
    reasons = []
    for engine in order:
        ok, why = can_run(conn, engine, actor_id)
        if ok:
            model = (row["model"] if row and row["model"] and engine == "claude" else None) or default_model(engine)
            return engine, why, model
        reasons.append(why)
    return None, "; ".join(reasons), None


# ------------------------------------------------------------------ Claude accounting

LIMIT_RE = re.compile(r"(usage limit|rate limit|limit reached|out of extra usage)", re.IGNORECASE)


def parse_claude(jsonl: str) -> dict:
    """Totals from `claude -p --output-format stream-json` output."""
    out = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "session_id": None, "limit": None,
           "rate_limit": None}
    for line in jsonl.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("session_id"):
            out["session_id"] = ev["session_id"]
        if ev.get("type") == "rate_limit_event":
            out["rate_limit"] = ev.get("rate_limit_info") or {}
        if ev.get("type") == "result":
            u = ev.get("usage") or {}
            out["input_tokens"] += int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0)
            out["output_tokens"] += int(u.get("output_tokens") or 0)
            out["cost_usd"] += float(ev.get("total_cost_usd") or 0)
            text = str(ev.get("result") or "")
            if ev.get("is_error") and LIMIT_RE.search(text):
                out["limit"] = text
    return out


def reset_time(message: str) -> datetime:
    """Claude writes e.g. 'Claude AI usage limit reached|1759161600'; else wait an hour."""
    if m := re.search(r"\|(\d{10})", message):
        return datetime.fromtimestamp(int(m[1]), timezone.utc)
    return _utcnow() + timedelta(hours=1)


def record_claude(conn: sqlite3.Connection, run_row: sqlite3.Row, jsonl: str) -> dict:
    u = parse_claude(jsonl)
    conn.execute(
        """INSERT INTO engine_usage (at, engine, actor_id, task_id, run_id, input_tokens, output_tokens, cost_usd)
           VALUES (?, 'claude', ?, ?, ?, ?, ?, ?)""",
        (now_iso(), run_row["actor_id"], run_row["task_id"], run_row["id"], u["input_tokens"], u["output_tokens"],
         u["cost_usd"]),
    )
    conn.execute("UPDATE runs SET input_tokens = ?, output_tokens = ? WHERE id = ?",
                 (u["input_tokens"], u["output_tokens"], run_row["id"]))
    rl = u["rate_limit"] or {}
    resets = (datetime.fromtimestamp(int(rl["resetsAt"]), timezone.utc).isoformat(timespec="seconds")
              if rl.get("resetsAt") else None)
    until, reason = None, None
    if u["limit"]:
        until, reason = (resets or reset_time(u["limit"]).isoformat(timespec="seconds")), u["limit"][:300]
    elif rl.get("status") == "rejected":
        until = resets or (_utcnow() + timedelta(hours=1)).isoformat(timespec="seconds")
        reason = "rate limit rejected"
    if until or rl:
        conn.execute(
            """INSERT INTO engine_limits (engine, paused_until, reason, window, resets_at, state, updated_at)
               VALUES ('claude', ?, ?, ?, ?, ?, ?)
               ON CONFLICT (engine) DO UPDATE SET
                 paused_until = COALESCE(excluded.paused_until, engine_limits.paused_until),
                 reason = COALESCE(excluded.reason, engine_limits.reason),
                 window = excluded.window, resets_at = excluded.resets_at, state = excluded.state,
                 updated_at = excluded.updated_at""",
            (until, reason, rl.get("rateLimitType"), resets, rl.get("status"), now_iso()),
        )
    conn.commit()
    return u


def status(conn: sqlite3.Connection) -> dict:
    """Both subscriptions for the System page."""
    from .budget import service as budget

    now = _utcnow()

    def window(hours: int) -> dict:
        row = conn.execute(
            "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) AS t, COALESCE(SUM(cost_usd), 0) AS c, COUNT(*) AS n "
            "FROM engine_usage WHERE engine = 'claude' AND at >= ?",
            ((now - timedelta(hours=hours)).isoformat(timespec="seconds"),),
        ).fetchone()
        return {"tokens": row["t"], "cost_usd": round(row["c"], 4), "runs": row["n"]}

    limit = conn.execute("SELECT * FROM engine_limits WHERE engine = 'claude'").fetchone()
    try:
        codex = budget.status(conn)
    except Exception:  # noqa: BLE001 - the budget module may not have checked yet
        codex = None
    return {
        "default": default_engine(),
        "codex": {"report": codex, "can_run": budget.can_run(conn, "0").allowed},
        "claude": {"window_5h": window(5), "window_7d": window(24 * 7),
                   "paused_until": paused_until(conn, "claude"),
                   "last_limit": dict(limit) if limit else None},
    }
