"""The prompt-cache TTL per agent: 1 hour or 5 minutes, by how often the agent runs.

Verified on the pool's Claude Code 2.1.289 (2026-10-04, the CLI's settings schema and its TTL choice):
`CLAUDE_CODE_PROMPT_CACHE_TTL` ("5m" | "1h", the environment wins over the `promptCacheTtl` setting),
`ENABLE_PROMPT_CACHING_1H`, `FORCE_PROMPT_CACHING_5M`. Unset, the CLI picks 1 hour on a Claude
subscription within its usage limits and 5 minutes on an API key or in overage. The pool runs on the
subscription: every cache write in the pool's transcripts that day was `ephemeral_1h` (672k tokens).

1 hour is not free: a 1-hour cache write costs 2× an input token, a 5-minute one 1.25×, a read 0.1×.
The CLI uses one TTL for the whole request, so the writes *inside* a run (each tool result, the task
part) pay 2× too, and they never need an hour: the calls of a run are seconds apart. Only the stable
prefix (tools, the system prompt: pos_worker.prompt.stable_prompt) gains, and only for a run that starts
5-60 minutes after the agent's previous one. Per agent, from its runs of the last DAYS days:

    VI = the cache written by a run that found its prefix cached (a run < 5 min after the previous one)
    S  = the stable prefix: what a run > 60 min after the previous one wrote on top of VI
    1h per run = 2·VI + (p<5 + p5-60)·0.1·S + p>60·2·S
    5m per run = 1.25·VI + p<5·0.1·S + (p5-60 + p>60)·1.25·S

(in input-token units). 1h only when it is cheaper by MARGIN. Measured on prod 2026-10-01..04: the
Access manager (runs every few minutes, a 21k prefix, small runs) gains from 1h; the CEO (12.5k written
per run, a 5k prefix) loses ~10k token-units a run. An agent.json "cache_ttl" overrides the choice.

Served in /api/worker/me as `cache_ttl`; the worker sets CLAUDE_CODE_PROMPT_CACHE_TTL for the run.

The measurement (no change), before/after a deploy:
    python -m pos.cache_policy                       # the choice per agent, last 7 days
    python -m pos.cache_policy --ratio 2026-10-01 2026-10-04 --ratio 2026-10-05 2026-10-08
the cache read/write ratio and the write share of the cost per window.
"""

import argparse
import json
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

DAYS = 7
MIN_RUNS = 8        # fewer runs with a gap: no choice (the CLI's own default)
MARGIN = 0.05       # 1h must be this much cheaper
S_DEFAULT = 15000   # the stable prefix when no run measured it (the pool's typical 10-25k)
WRITE_1H, WRITE_5M, READ = 2.0, 1.25, 0.1
TTLS = ("5m", "1h")


def _runs(conn: sqlite3.Connection, actor_id: int | None, since: str) -> list[sqlite3.Row]:
    where = "AND r.actor_id = ?" if actor_id else ""
    args = (since, actor_id) if actor_id else (since,)
    return conn.execute(f"""SELECT r.id, r.actor_id, r.started_at, r.ended_at,
                                   (SELECT SUM(e.cache_creation_tokens) FROM engine_usage e WHERE e.run_id = r.id) AS cw
                            FROM runs r WHERE r.engine = 'claude' AND r.status != 'blocked' AND r.started_at >= ?
                            {where} ORDER BY r.actor_id, r.started_at""", args).fetchall()


def _ts(s: str) -> datetime:
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def profile(rows: list) -> dict:
    """Gap buckets and the cache written per bucket for one agent's runs (in start order)."""
    prev = None
    gaps = {"lt5": 0, "mid": 0, "gt60": 0}
    hit, miss = [], []
    for r in rows:
        start = _ts(r["started_at"])
        if prev is not None:
            g = (start - prev).total_seconds() / 60
            b = "lt5" if g < 5 else "mid" if g < 60 else "gt60"
            gaps[b] += 1
            if r["cw"]:
                (hit if b == "lt5" else miss if b == "gt60" else []).append(r["cw"])
        prev = _ts(r["ended_at"] or r["started_at"])
    return {"gaps": gaps, "hit": hit, "miss": miss}


def choose(p: dict) -> dict:
    """The TTL for an agent's profile (pos.cache_policy.profile), with the per-run cost of each."""
    n = sum(p["gaps"].values())
    if n < MIN_RUNS or not (p["hit"] or p["miss"]):
        return {"ttl": None, "runs": n, "why": "too few runs"}
    vi = statistics.median(p["hit"]) if p["hit"] else max(0.0, statistics.median(p["miss"]) - S_DEFAULT)
    s = max(0.0, statistics.median(p["miss"]) - vi) if p["miss"] else S_DEFAULT
    lt5, mid, gt60 = (p["gaps"][k] / n for k in ("lt5", "mid", "gt60"))
    c1h = WRITE_1H * vi + (lt5 + mid) * READ * s + gt60 * WRITE_1H * s
    c5m = WRITE_5M * vi + lt5 * READ * s + (mid + gt60) * WRITE_5M * s
    ttl = "1h" if c1h < c5m * (1 - MARGIN) else "5m"
    return {"ttl": ttl, "runs": n, "vi": round(vi), "s": round(s), "p": [round(lt5, 2), round(mid, 2), round(gt60, 2)],
            "cost_1h": round(c1h), "cost_5m": round(c5m)}


def ttl_for(conn: sqlite3.Connection, actor_id: int, now: datetime | None = None) -> str | None:
    """The prompt-cache TTL for this agent's runs ("1h" | "5m"), None: the CLI's own default. Fail-open."""
    try:
        since = ((now or datetime.now(timezone.utc)) - timedelta(days=DAYS)).isoformat(timespec="seconds")
        return choose(profile(_runs(conn, actor_id, since)))["ttl"]
    except Exception:  # noqa: BLE001 - the run starts with the CLI's default
        return None


def table(conn: sqlite3.Connection, since: str) -> list[dict]:
    rows = _runs(conn, None, since)
    by: dict[int, list] = {}
    for r in rows:
        by.setdefault(r["actor_id"], []).append(r)
    out = []
    for aid, rs in by.items():
        a = conn.execute("SELECT name FROM actors WHERE id = ?", (aid,)).fetchone()
        out.append({"agent": a["name"] if a else aid, **choose(profile(rs))})
    return sorted(out, key=lambda x: -x["runs"])


def ratio(conn: sqlite3.Connection, since: str, until: str) -> dict:
    """Cache reads per cache write and the cost split of the Claude runs between two dates (inclusive)."""
    end = (datetime.fromisoformat(until) + timedelta(days=1)).date().isoformat()
    r = conn.execute("""SELECT COUNT(DISTINCT run_id) runs, COALESCE(SUM(cache_read_tokens), 0) cr,
                               COALESCE(SUM(cache_creation_tokens), 0) cw, COALESCE(SUM(output_tokens), 0) ot,
                               COALESCE(SUM(input_tokens - cache_creation_tokens), 0) inp, COALESCE(SUM(cost_usd), 0) cost
                        FROM engine_usage WHERE engine = 'claude' AND at >= ? AND at < ?""", (since, end)).fetchone()
    days = max(1, (datetime.fromisoformat(end) - datetime.fromisoformat(since)).days)
    return {"since": since, "until": until, "runs": r["runs"], "cost_usd": round(r["cost"], 2),
            "usd_per_day": round(r["cost"] / days, 2), "usd_per_run": round(r["cost"] / max(r["runs"], 1), 3),
            "read_per_write": round(r["cr"] / max(r["cw"], 1), 2),
            "cache_write_tokens_per_run": round(r["cw"] / max(r["runs"], 1)),
            "cache_read_tokens_per_run": round(r["cr"] / max(r["runs"], 1))}


def main(argv: list[str] | None = None) -> None:
    from .config import get_settings
    from .db import connect

    p = argparse.ArgumentParser(description="Prompt-cache TTL per agent and the cache read/write ratio.")
    p.add_argument("--db", default=None)
    p.add_argument("--days", type=int, default=DAYS)
    p.add_argument("--ratio", nargs=2, action="append", metavar=("SINCE", "UNTIL"),
                   help="the cache ratio between two dates (repeat for before/after)")
    a = p.parse_args(argv)
    conn = connect(Path(a.db) if a.db else get_settings().db_path)
    try:
        if a.ratio:
            out = [ratio(conn, *w) for w in a.ratio]
        else:
            since = (datetime.now(timezone.utc) - timedelta(days=a.days)).isoformat(timespec="seconds")
            out = table(conn, since)
        print(json.dumps(out, ensure_ascii=False, indent=1))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
