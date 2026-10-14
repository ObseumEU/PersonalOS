"""The agent pool's quota: the Claude subscription the agents run on (T-1288).

8.-11. 10. 2026 its weekly window ran out with nobody warned and the company stood for 41 h (T-1086,
T-1101). Every Claude run's `rate_limit_event` (pos.engines.record_claude) is recorded here per window
(five_hour, seven_day, ...): the share used and when the window resets. From that:

- `current`: the highest share over the windows still open (100 while Claude is paused for its limit);
  the scorecard shows it as agent_pool_quota_pct (pos.scorecard.agents_health);
- `check` (scheduler, every 5 min): at 80 % and 95 % one incident for the Monitor (topic provoz) and one
  DM to the CEO; spent: one DM to the CEO with the reset. Once per window and threshold; a jump past
  several thresholds tells only the highest;
- from POS_QUOTA_RESERVE_PCT (default 90) only the owner's chat answers and P1 incidents start, for an
  agent whose run would use Claude (pos.api_worker._next_work); the rest stays in `next`;
- for RECOVERY_H after a window that reached the reserve resets: the owner's work first, then P1
  incidents, then the rest; routines start one at a time, at most one per ROUTINE_GAP_MIN company-wide.

The command guard, the company cap and the kill switch are not touched. Tables are created on first use
(no numbered migration), as pos.scorecard does.
"""

import os
import sqlite3
from datetime import datetime, timedelta, timezone

from . import actors
from .core import TZ, Ctx, now_iso

WARN_AT = (80, 95)
SPENT = 100
WARNING_FLOOR = 80.0   # status allowed_warning without a utilization: the window is at least near its limit
UNDATED_TTL_H = 1      # a reading without a reset time counts this long
RECOVERY_H = 2
ROUTINE_GAP_MIN = 10
SOURCE = "quota_guard"

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS agent_pool_quota (
        window TEXT PRIMARY KEY,
        pct REAL,
        status TEXT,
        resets_at TEXT,
        at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS agent_pool_quota_alerts (
        window TEXT NOT NULL,
        resets_at TEXT NOT NULL,
        threshold INTEGER NOT NULL,
        at TEXT NOT NULL,
        UNIQUE (window, resets_at, threshold)
    )""",
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    for sql in _SCHEMA:
        conn.execute(sql)


def _has(conn: sqlite3.Connection, table: str) -> bool:
    """Readers on the worker's hot path (every /next poll) only look; the writers create the tables."""
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone() is not None


def reserve_pct() -> float:
    try:
        return float(os.environ.get("POS_QUOTA_RESERVE_PCT") or 90)
    except ValueError:
        return 90.0


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ measuring

def pct_of(info: dict) -> float | None:
    """The used share in % from a rate_limit_info, None when it does not say. `utilization` up to 1 is a
    fraction; rejected is 100; allowed_warning without a number is at least WARNING_FLOOR."""
    u = info.get("utilization")
    if isinstance(u, (int, float)) and not isinstance(u, bool):
        return round(min(float(u) * 100 if u <= 1 else float(u), 100.0), 1)
    status = info.get("status")
    if status == "rejected":
        return 100.0
    if status == "allowed_warning":
        return WARNING_FLOOR
    return None


def record(conn: sqlite3.Connection, info: dict, resets_at: str | None) -> None:
    """One Claude run's rate_limit_info. Within the same window (same reset) the share only grows."""
    if not info:
        return
    ensure_schema(conn)
    window = str(info.get("rateLimitType") or "unknown")
    pct = pct_of(info)
    prev = conn.execute("SELECT pct, resets_at FROM agent_pool_quota WHERE window = ?", (window,)).fetchone()
    if prev and prev["resets_at"] == resets_at and prev["pct"] is not None:
        pct = prev["pct"] if pct is None else max(pct, prev["pct"])
    conn.execute(
        """INSERT INTO agent_pool_quota (window, pct, status, resets_at, at) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (window) DO UPDATE SET pct = excluded.pct, status = excluded.status,
             resets_at = excluded.resets_at, at = excluded.at""",
        (window, pct, info.get("status"), resets_at, now_iso()),
    )


def current(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """{pct, window, resets_at, status}: the most used window still open; pct None when nothing is known."""
    from . import engines

    now = _now(now)
    out = {"pct": None, "window": None, "resets_at": None, "status": None}
    paused = engines.paused_until(conn, "claude")
    if paused:
        why = conn.execute("SELECT reason FROM engine_limits WHERE engine = 'claude'").fetchone()
        if why is None or not (why["reason"] or "").startswith("self-check failed"):  # a broken CLI is no quota
            return {"pct": 100.0, "window": "limit", "resets_at": paused, "status": "rejected"}
    if not _has(conn, "agent_pool_quota"):
        return out
    undated = _iso(now - timedelta(hours=UNDATED_TTL_H))
    for r in conn.execute(
            """SELECT * FROM agent_pool_quota WHERE pct IS NOT NULL
               AND ((resets_at IS NOT NULL AND resets_at > ?) OR (resets_at IS NULL AND at >= ?))
               ORDER BY pct DESC, resets_at DESC LIMIT 1""", (_iso(now), undated)):
        out = {"pct": r["pct"], "window": r["window"], "resets_at": r["resets_at"], "status": r["status"]}
    return out


# ------------------------------------------------------------------ warnings

def _claim(conn: sqlite3.Connection, window: str, resets_at: str | None, threshold: int) -> bool:
    """True the first time this window (until this reset) crosses this threshold."""
    cur = conn.execute("INSERT OR IGNORE INTO agent_pool_quota_alerts (window, resets_at, threshold, at) "
                       "VALUES (?, ?, ?, ?)", (window, resets_at or "", threshold, now_iso()))
    return cur.rowcount == 1


def _local(iso: str | None) -> str:
    if not iso:
        return "neznámo kdy"
    try:
        return datetime.fromisoformat(iso).astimezone(TZ).strftime("%-d. %-m. %H:%M")
    except ValueError:
        return iso


def check(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """Scheduler (every 5 min): warn at 80 / 95 %, tell the CEO once when the quota is spent."""
    from . import monitor, notices, tasks

    ensure_schema(conn)
    cur = current(conn, now)
    pct = cur["pct"]
    if pct is None:
        return {"pct": None}
    reserve = reserve_pct()
    levels = sorted({*WARN_AT, reserve, SPENT})
    new = [lv for lv in levels if pct >= lv and _claim(conn, cur["window"], cur["resets_at"], int(lv))]
    told = [lv for lv in new if lv in (*WARN_AT, SPENT)]
    out: dict = {"pct": pct, "window": cur["window"], "resets_at": cur["resets_at"], "new": [int(x) for x in new]}
    if not told:
        conn.commit()
        return out
    top = int(max(told))
    reset = _local(cur["resets_at"])
    if top >= SPENT:
        body = (f"**Kvóta agent-pool je vyčerpaná** (okno {cur['window']}): běhy na Claude stojí do **{reset}** "
                "(konec okna). Po obnovení jdou první zprávy majitele, pak P1 incidenty, pak ostatní; rutiny "
                f"postupně, nejvýš jedna za {ROUTINE_GAP_MIN} min.")
    else:
        body = (f"**Kvóta agent-pool {round(pct)} %** (práh {top} %, okno {cur['window']}, obnova {reset}). "
                f"Od {round(reserve)} % startují jen odpovědi majiteli a P1 incidenty, ostatní počká v next.")
        mid = monitor.monitor_id(conn)
        if mid:
            t = tasks.create(conn, Ctx(actors.owner_id(conn), via="scheduler"), {
                "title": f"Kvóta agent-pool {top} % (okno {cur['window']}, obnova {reset})",
                "assignee": {"type": "agent", "id": mid}, "status": "next", "priority": 1 if top >= 95 else 2,
                "topic": "provoz", "source": SOURCE, "reviewer": mid,
                "notes": ("### Proč\nPředplatné Claude, na kterém běží agenti, je z "
                          f"{round(pct)} % vyčerpané (okno {cur['window']}, obnova {reset}). 8.–11. 10. stála "
                          "firma 41 h, protože nikdo nebyl varován (T-1086).\n\n"
                          "### Odkud\npos.quota_guard (T-1288), čísla z rate_limit_event běhů Claude.\n\n"
                          "### Co udělat\nZjisti, kdo kvótu teď čerpá (běhy za poslední hodiny po agentech), a "
                          "co jde odložit; doporučení dej CEO. Nic nevypínej."),
                "definition_of_done": "Hlavní spotřebitelé kvóty jsou zjištění a CEO má doporučení, co odložit."})
            out["task"] = t["ref"]
            body += f" Incident pro Hlídače: {t['ref']}."
    notices.dm(conn, notices.ceo(conn), body, priority="fyi")
    out["told"] = top
    conn.commit()
    return out


# ------------------------------------------------------------------ who starts

def is_p1_incident(row) -> bool:
    return row["priority"] == 1 and ((row["source"] or "") == "event:sentinel" or (row["topic"] or "") == "provoz")


def is_routine(row) -> bool:
    src = row["source"] or ""
    return src.startswith("schedule:") or src == "scheduler"


def rank(conn: sqlite3.Connection, row) -> int:
    """0 the owner's answer, 1 a P1 incident, 2 the rest."""
    from . import business

    if business.owner_reply_task(conn, row):
        return 0
    return 1 if is_p1_incident(row) else 2


def _recovering(conn: sqlite3.Connection, now: datetime) -> bool:
    """A window that reached the reserve reset within the last RECOVERY_H hours."""
    if not _has(conn, "agent_pool_quota_alerts"):
        return False
    return conn.execute(
        "SELECT 1 FROM agent_pool_quota_alerts WHERE threshold >= ? AND resets_at != '' AND resets_at <= ? "
        "AND resets_at > ? LIMIT 1",
        (int(reserve_pct()), _iso(now), _iso(now - timedelta(hours=RECOVERY_H)))).fetchone() is not None


def mode(conn: sqlite3.Connection, actor_id: int, now: datetime | None = None) -> str | None:
    """"reserve" (over the reserve and this agent's run would use Claude), "recovery", or None."""
    from . import engines

    now = _now(now)
    pct = current(conn, now)["pct"]
    if pct is not None and pct >= reserve_pct():
        engine, _, _ = engines.choose(conn, actor_id)
        return None if engine == "codex" else "reserve"  # a Codex run does not touch this quota
    return "recovery" if _recovering(conn, now) else None


def routine_gap_ok(conn: sqlite3.Connection, now: datetime | None = None) -> bool:
    """No routine run started company-wide in the last ROUTINE_GAP_MIN minutes."""
    since = _iso(_now(now) - timedelta(minutes=ROUTINE_GAP_MIN))
    return conn.execute(
        """SELECT 1 FROM runs r JOIN tasks t ON t.id = r.task_id WHERE r.started_at >= ?
           AND (t.source LIKE 'schedule:%' OR t.source = 'scheduler') LIMIT 1""", (since,)).fetchone() is None


def admits(conn: sqlite3.Connection, row, how: str, now: datetime | None = None) -> bool:
    """May this task start in this mode? Over the reserve: the owner's answer, a P1 incident, the owner's open
    browser handoff. Recovering: all, a routine only when none started in the last ROUTINE_GAP_MIN."""
    from . import handoff

    if how == "reserve":
        return rank(conn, row) < 2 or handoff.prepare_exempt(conn, row)
    if how == "recovery" and is_routine(row):
        return routine_gap_ok(conn, now)
    return True


def ordered(conn: sqlite3.Connection, rows: list) -> list:
    """Recovering: the owner's work, then P1 incidents, then the rest (stable within each)."""
    return sorted(rows, key=lambda r: rank(conn, r))
