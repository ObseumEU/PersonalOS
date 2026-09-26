"""Agent runtimes: Codex CLI and Claude Code CLI, each on its own subscription.

Every agent has an engine: `auto` (the default, POS_AGENT_RUNTIME), `codex` or
`claude`. `auto` tries the engines in POS_ENGINE_ORDER (default: Codex first,
Claude as the fallback). When an engine hits its subscription limit it is
paused until the reset, its task goes straight back to the queue and the next
run uses the other engine; after the reset the first engine is used again.
The Claude model is claude-opus-5-5 by default (POS_CLAUDE_MODEL), configurable
per agent. Each subscription has its own accounting:

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
    e = os.environ.get("POS_AGENT_RUNTIME", "auto")
    return e if e in CHOICES else "auto"


def auto_order() -> list[str]:
    """Engines `auto` tries, first to last (POS_ENGINE_ORDER, default codex,claude)."""
    order = [e.strip() for e in os.environ.get("POS_ENGINE_ORDER", "codex,claude").split(",") if e.strip() in ENGINES]
    return order + [e for e in ("codex", "claude") if e not in order]


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


def _codex_reset_passed(conn: sqlite3.Connection) -> None:
    """Codex's limit window has just reset: re-run the budget check now (its
    instant "pause" on the limit would otherwise hold until the hourly check)."""
    row = conn.execute("SELECT paused_until FROM engine_limits WHERE engine = 'codex'").fetchone()
    if row and row["paused_until"]:
        conn.execute("UPDATE engine_limits SET paused_until = NULL WHERE engine = 'codex'")
        from .integrations import budget_check

        budget_check(conn)
        conn.commit()


def can_run(conn: sqlite3.Connection, engine: str, actor_id: int) -> tuple[bool, str]:
    until = paused_until(conn, engine)
    if until:
        return False, f"{engine} usage limit until {until}"
    if engine == "codex":
        _codex_reset_passed(conn)
        from .budget import service as budget

        d = budget.can_run(conn, str(actor_id))
        return d.allowed, d.reason
    return True, "ok"


def choose(conn: sqlite3.Connection, actor_id: int) -> tuple[str | None, str, str | None]:
    """Pick (engine, why, model) for an agent's next run; engine None if none can run."""
    row = conn.execute("SELECT engine, model FROM actors WHERE id = ?", (actor_id,)).fetchone()
    wanted = (row["engine"] if row and row["engine"] else None) or default_engine()
    order = [wanted] if wanted in ENGINES else auto_order()
    reasons = []
    for engine in order:
        ok, why = can_run(conn, engine, actor_id)
        if ok:
            model = (row["model"] if row and row["model"] and engine == "claude" else None) or default_model(engine)
            return engine, why, model
        reasons.append(why)
    return None, "; ".join(reasons), None


def pause(conn: sqlite3.Connection, engine: str, until: str, reason: str) -> None:
    conn.execute(
        """INSERT INTO engine_limits (engine, paused_until, reason, updated_at) VALUES (?, ?, ?, ?)
           ON CONFLICT (engine) DO UPDATE SET paused_until = excluded.paused_until, reason = excluded.reason,
             updated_at = excluded.updated_at""",
        (engine, until, reason[:300], now_iso()),
    )


# ------------------------------------------------------------------ Codex limit

CODEX_AGAIN_RE = re.compile(
    r"try again at ([A-Z][a-z]{2})[a-z]* (\d{1,2})(?:st|nd|rd|th)?,? (\d{4}),? (\d{1,2}):(\d{2}) ?([AP]M)", re.IGNORECASE)


def codex_reset(conn: sqlite3.Connection, message: str) -> datetime:
    """When the exhausted Codex window resets: from the rate-limit snapshots
    (session logs), else from the error message, else in an hour."""
    rows = []
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'budget_limit_snapshots'").fetchone():
        rows = conn.execute(
            """SELECT resets_at FROM budget_limit_snapshots s WHERE used_percent >= 100 AND resets_at > ?
               AND at = (SELECT MAX(at) FROM budget_limit_snapshots WHERE window = s.window)""",
            (int(_utcnow().timestamp()),),
        ).fetchall()
    if rows:
        return datetime.fromtimestamp(max(r["resets_at"] for r in rows), timezone.utc)
    if m := CODEX_AGAIN_RE.search(message):
        mon, day, year, hh, mm, ampm = m.groups()
        try:
            from .core import TZ

            local = datetime.strptime(f"{mon} {day} {year} {hh}:{mm} {ampm}", "%b %d %Y %I:%M %p")
            # Codex names the reset in the local time of the machine it runs on (the owner's, Europe/Prague).
            return local.replace(tzinfo=TZ).astimezone(timezone.utc)
        except ValueError:
            pass
    return _utcnow() + timedelta(hours=1)


def record_codex_limit(conn: sqlite3.Connection, jsonl: str) -> str | None:
    """If a Codex run was refused for the subscription limit, pause Codex until
    the reset and return that time."""
    from .budget.codex_usage import parse_exec_jsonl

    lines = jsonl.splitlines()
    if not parse_exec_jsonl(lines).limit_reached:
        return None
    message = next((line for line in lines if "usage limit" in line.lower()), "Codex usage limit")
    until = codex_reset(conn, message).isoformat(timespec="seconds")
    pause(conn, "codex", until, "Codex usage limit")
    conn.commit()
    return until


# ------------------------------------------------------------------ Claude self-check

def claude_selfcheck(conn: sqlite3.Connection, timeout: int = 90) -> dict:
    """Ask the local Claude CLI for a one-word answer with the configured model.
    If it fails (not logged in, a CLI too old for the model), Claude is marked
    unavailable for 6 hours with the reason, so the fallback is not relied on
    blindly; the System page shows it. Only where workers use this machine's
    CLI (POS_CLAUDE_SELFCHECK=1, e.g. the server)."""
    import subprocess

    from .runner import claude_bin

    model = default_model("claude")
    binary = claude_bin()
    if binary is None:
        ok, why = False, "claude CLI not installed"
    else:
        try:
            p = subprocess.run([binary, "-p", "--model", model, "--output-format", "json", "Reply with: ok"],
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
            out = (p.stdout or "") + (p.stderr or "")
            ok = p.returncode == 0 and '"is_error":false' in out.replace(" ", "")
            why = "ok" if ok else out.strip()[-300:] or f"exit {p.returncode}"
        except (OSError, subprocess.TimeoutExpired) as e:
            ok, why = False, str(e)[:300]
    if ok:
        conn.execute("UPDATE engine_limits SET paused_until = NULL, reason = NULL WHERE engine = 'claude' "
                     "AND reason LIKE 'self-check failed%'")
    else:
        pause(conn, "claude", (_utcnow() + timedelta(hours=6)).isoformat(timespec="seconds"),
              f"self-check failed ({model}): {why}")
    conn.commit()
    return {"ok": ok, "model": model, "detail": why[:300]}


# ------------------------------------------------------------------ Claude accounting

LIMIT_RE = re.compile(r"(usage limit|rate limit|limit reached|out of extra usage)", re.IGNORECASE)


def parse_claude(jsonl: str) -> dict:
    """Totals from `claude -p --output-format stream-json` output (one or more
    `result` events: a triage call and the run, or a resumed session).
    input_tokens counts uncached input plus cache writes; cache reads are
    counted apart (they cost a tenth of an input token)."""
    out = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_creation_tokens": 0,
           "cost_usd": 0.0, "session_id": None, "limit": None, "rate_limit": None}
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
            out["cache_read_tokens"] += int(u.get("cache_read_input_tokens") or 0)
            out["cache_creation_tokens"] += int(u.get("cache_creation_input_tokens") or 0)
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


def record_claude(conn: sqlite3.Connection, run_row: sqlite3.Row, jsonl: str, update_run: bool = True) -> dict:
    """Claude usage of a run into engine_usage and the run. update_run=False: only
    the cost goes on the run (a Claude check before a Codex run keeps Codex's tokens)."""
    u = parse_claude(jsonl)
    conn.execute(
        """INSERT INTO engine_usage (at, engine, actor_id, task_id, run_id, input_tokens, output_tokens, cost_usd,
                                   cache_read_tokens, cache_creation_tokens)
           VALUES (?, 'claude', ?, ?, ?, ?, ?, ?, ?, ?)""",
        (now_iso(), run_row["actor_id"], run_row["task_id"], run_row["id"], u["input_tokens"], u["output_tokens"],
         u["cost_usd"], u["cache_read_tokens"], u["cache_creation_tokens"]),
    )
    if update_run:
        conn.execute("UPDATE runs SET input_tokens = ?, output_tokens = ?, cache_read_tokens = ?, cost_usd = ? "
                     "WHERE id = ?", (u["input_tokens"], u["output_tokens"], u["cache_read_tokens"],
                                      round(u["cost_usd"], 6), run_row["id"]))
    else:
        conn.execute("UPDATE runs SET cost_usd = COALESCE(cost_usd, 0) + ? WHERE id = ?",
                     (round(u["cost_usd"], 6), run_row["id"]))
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
    codex_limit = conn.execute("SELECT * FROM engine_limits WHERE engine = 'codex'").fetchone()
    try:
        codex = budget.status(conn)
    except Exception:  # noqa: BLE001 - the budget module may not have checked yet
        codex = None
    return {
        "default": default_engine(),
        "order": auto_order(),
        "codex": {"report": codex, "can_run": budget.can_run(conn, "0").allowed and not paused_until(conn, "codex"),
                  "paused_until": paused_until(conn, "codex"),
                  "last_limit": dict(codex_limit) if codex_limit else None},
        "claude": {"window_5h": window(5), "window_7d": window(24 * 7),
                   "paused_until": paused_until(conn, "claude"),
                   "last_limit": dict(limit) if limit else None},
    }
