"""The daily signal digest: what goes wrong in PersonalOS, counted in code (no model, no tokens).

Every signal has a **stable key** (`run_error:task.timeout`, `tool_error:update_task.follow_up`,
`deploy_rejected:merge`, `owner:unanswered_2h` ...), a count over the last 24 h and 7 days, the
previous period's count for the trend, and up to 5 example ids (runs, tasks, deploys, messages). The
key is what a fix task's target points at, so it must not change with the wording of one message:
causes are normalized (numbers, quotes, URLs and ids dropped; known causes mapped to a fixed name).

Sources (all in the DB):

- **run_error:<kind>.<cause>**: failed runs (`runs.status = 'error'`), the cause from `runs.detail`;
- **tool_error:<tool>.<reason>**: refused tool calls (`audit_log` `mcp:<tool>:refused`, the reason
  normalized) and failed ones (`tool_usage.ok = 0`); per-run transcript summaries add theirs when a
  table `run_tool_errors` exists (filled by the worker's transcript summary);
- **guard:<what>**: the command guard and its kin: refused command approvals, commands sent to the
  owner, taint blocks, refused credentials, audit actions `guard_*` / `command_*deny*`;
- **cost_cap:<which>**: blocked runs (company cap, the agent's day budget, an engine usage limit)
  and runs stopped at their per-run cap (`error_max_budget_usd`);
- **loop:claim_repeat** (tasks claimed ≥ 5× in the window), **loop:chat** (loop holds);
- **stuck:review** (results waiting for review past the SLA) and **stuck:waiting_overdue** (waiting
  tasks past their follow-up or deadline): states now, the previous value from the stored digest;
- **deploy_<status>:<stage>**: rejected, reverted and failed deploys by stage;
- **owner:unanswered_2h**, **owner:frustration**, **owner:double_answer** (pos.frustration);
- **agent_fail:<agent>**: failed runs per agent (with its success rate);
- **spend:<category>**: USD by category (business, platform, demo, total) (pos.business.cost_split);
- **ux:<kind>:<path>**: the web smoke's findings (pos.improve.smoke), when it ran today.

`daily(conn)` computes and stores the day's digest (`improve_signals`, one row per day and key,
created on first use); `latest(conn)` reads the newest; `ranked()` orders by impact × trend.
"""

import json
import logging
import math
import re
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from ..core import TZ, now_iso

log = logging.getLogger(__name__)

EXAMPLES = 5
LOOP_CLAIMS = 5  # claims of one task in the window that make a loop
# How much one occurrence of a category hurts (the owner's pain first); 0 = information only.
WEIGHTS = {"owner": 5.0, "deploy": 3.0, "run_error": 3.0, "loop": 3.0, "ux": 3.0, "tool_error": 2.0,
           "cost_cap": 2.0, "guard": 1.0, "stuck": 1.0, "agent_fail": 0.0, "spend": 0.0}

_SCHEMA = """CREATE TABLE IF NOT EXISTS improve_signals (
    day        TEXT NOT NULL,
    key        TEXT NOT NULL,
    category   TEXT NOT NULL,
    label      TEXT NOT NULL DEFAULT '',
    count_24h  REAL NOT NULL DEFAULT 0,
    prev_24h   REAL,
    count_7d   REAL NOT NULL DEFAULT 0,
    prev_7d    REAL,
    examples   TEXT NOT NULL DEFAULT '[]',
    sample     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (day, key)
)"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_SCHEMA)


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat(timespec="seconds")


def _has(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone() is not None


def day_of(now: datetime) -> str:
    return now.astimezone(TZ).date().isoformat()


# ------------------------------------------------------------------ normalizing causes

_STOP = {"is", "the", "a", "an", "be", "must", "for", "to", "of", "in", "on", "at", "by", "with", "this", "that",
         "it", "and", "or", "was", "were", "has", "have", "its", "your", "you", "are", "please"}
_URL = re.compile(r"https?://\S+")
_REF = re.compile(r"\b[a-z]{1,3}-\d+\b|#\d+")  # task refs (T-556), channels (#32)
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"|`[^`]*`|„[^“]*“")
_HEXISH = re.compile(r"\b[0-9a-f]{7,}\b|\b\d[\w.:+-]*")
_TOKEN = re.compile(r"[a-z][a-z_]*")


def slug(text: str, words: int = 4, cut: bool = True) -> str:
    """A stable short name of a message: no URLs, quotes, numbers or ids; the first few words that mean
    something, joined by "_" ("follow_up must be YYYY-MM-DD" -> "follow_up")."""
    from ..scorecard import fold

    t = fold(text or "")
    t = _URL.sub(" ", t)
    t = _REF.sub(" ", t)
    t = _QUOTED.sub(" ", t)

    def toks_of(s: str) -> list[str]:
        s = _HEXISH.sub(" ", s)
        return [w for w in (x.strip("_") for x in _TOKEN.findall(s)) if len(w) > 1 and w not in _STOP]

    toks = toks_of(re.split(r"[:;(]|\.\s|\n", t, maxsplit=1)[0] if cut else t)
    if cut and len(toks) < 2:  # "you are not in #32: agents read ...": the part before the colon says too little
        toks = toks_of(t)
    if toks and "_" in toks[0]:
        return toks[0][:40]  # a field name says it all ("follow_up must be ...")
    return "_".join(toks[:words])[:40] or "unknown"


RUN_CAUSES = (
    ("worker_silent", re.compile(r"worker went silent|no heartbeat", re.I)),
    ("timeout", re.compile(r"timeout|timed out|time limit", re.I)),
    ("usage_limit", re.compile(r"session limit|usage limit|rate.?limit|\b429\b", re.I)),
    ("auth", re.compile(r"\b401\b|unauthori[sz]ed|\b403\b|forbidden|missing bearer", re.I)),
    ("connection", re.compile(r"connection (lost|reset|refused|error)|api error|overloaded|\b50[0-4]\b", re.I)),
    ("eval_failed", re.compile(r"\beval\b.*(failed|hung)", re.I)),
)
MAX_BUDGET = re.compile(r"max_budget|max budget", re.I)


def run_cause(detail: str) -> str:
    for name, rx in RUN_CAUSES:
        if rx.search(detail or ""):
            return name
    return slug(detail, 4)


CAP_CAUSES = (
    ("company", re.compile(r"company cap", re.I)),
    ("agent_runs_day", re.compile(r"budget runs", re.I)),
    ("agent_usd_day", re.compile(r"budget usd", re.I)),
    ("agent_usd_month", re.compile(r"budget.*month", re.I)),
    ("engine_usage_limit", re.compile(r"usage limit|session limit", re.I)),
    ("paused", re.compile(r"paused|kill switch|frozen", re.I)),
)


def cap_cause(detail: str) -> str:
    for name, rx in CAP_CAUSES:
        if rx.search(detail or ""):
            return name
    return slug(detail, 3)


# ------------------------------------------------------------------ collectors: (conn, s, u) -> {key: item}

class _Acc:
    """Collects signals: count, examples, the first sample text, category and label."""

    def __init__(self):
        self.items: dict[str, dict] = {}

    def add(self, key: str, category: str, label: str, example=None, sample: str = "", n: float = 1) -> None:
        it = self.items.setdefault(key, {"key": key, "category": category, "label": label, "count": 0.0,
                                         "examples": [], "sample": ""})
        it["count"] += n
        if example is not None and example not in it["examples"] and len(it["examples"]) < EXAMPLES:
            it["examples"].append(example)
        if sample and not it["sample"]:
            it["sample"] = " ".join(str(sample).split())[:240]

    def set(self, key: str, category: str, label: str, count: float, examples=(), sample: str = "") -> None:
        self.items[key] = {"key": key, "category": category, "label": label, "count": float(count),
                           "examples": list(examples)[:EXAMPLES], "sample": " ".join(str(sample).split())[:240]}


def _t(task_id) -> str:
    from ..tasks import display_id

    return display_id(task_id)


def _runs(conn, acc: _Acc, s: str, u: str) -> None:
    for r in conn.execute("""SELECT id, kind, status, detail FROM runs WHERE started_at >= ? AND started_at < ?
                             AND status IN ('error', 'blocked') ORDER BY id DESC""", (s, u)):
        detail = r["detail"] or ""
        rid = f"run:{r['id']}"
        if r["status"] == "blocked":
            acc.add(f"cost_cap:{cap_cause(detail)}", "cost_cap", "run blocked by a cap", rid, detail)
        elif MAX_BUDGET.search(detail):
            acc.add("cost_cap:run_usd", "cost_cap", "run stopped at its per-run cap", rid, detail)
        else:
            acc.add(f"run_error:{slug(r['kind'] or 'run', 1, cut=False)}.{run_cause(detail)}", "run_error",
                    f"failed {r['kind']} runs", rid, detail)


def _tools(conn, acc: _Acc, s: str, u: str) -> None:
    for r in conn.execute("""SELECT id, action, detail, run_id FROM audit_log WHERE action LIKE 'mcp:%:refused'
                             AND at >= ? AND at < ? ORDER BY id DESC""", (s, u)):
        tool = r["action"][4:-len(":refused")]
        try:
            reason = (json.loads(r["detail"] or "{}") or {}).get("reason") or ""
        except (TypeError, ValueError):
            reason = ""
        acc.add(f"tool_error:{tool}.{slug(reason)}", "tool_error", f"{tool} refused",
                f"run:{r['run_id']}" if r["run_id"] else f"audit:{r['id']}", reason)
    if _has(conn, "tool_usage"):
        for r in conn.execute("""SELECT tool, run_id, id FROM tool_usage WHERE ok = 0 AND at >= ? AND at < ?
                                 ORDER BY id DESC""", (s, u)):
            acc.add(f"tool_error:{r['tool']}.failed", "tool_error", f"{r['tool']} failed",
                    f"run:{r['run_id']}" if r["run_id"] else f"usage:{r['id']}")
    if _has(conn, "run_tool_errors"):  # the per-run transcript summary's tool errors, when the worker records them
        cols = {c[1] for c in conn.execute("PRAGMA table_info(run_tool_errors)")}
        if {"tool", "run_id"} <= cols:
            msg = "message" if "message" in cols else ("error" if "error" in cols else None)
            at = "at" if "at" in cols else ("created_at" if "created_at" in cols else None)
            if at:
                for r in conn.execute(f"""SELECT tool, run_id{', ' + msg + ' AS msg' if msg else ''} FROM run_tool_errors
                                          WHERE {at} >= ? AND {at} < ?""", (s, u)):
                    text = r["msg"] if msg else ""
                    acc.add(f"tool_error:{r['tool']}.{slug(text) if text else 'failed'}", "tool_error",
                            f"{r['tool']} error", f"run:{r['run_id']}", text)


def _guard(conn, acc: _Acc, s: str, u: str) -> None:
    for r in conn.execute("""SELECT id, action, entity_id, detail FROM audit_log WHERE at >= ? AND at < ? AND (
                                 action IN ('taint_block', 'cred_refused') OR action LIKE 'guard%'
                                 OR action LIKE 'command%deny%' OR action LIKE 'command%refus%'
                                 OR action LIKE 'command%block%' OR action LIKE '%:denied')
                             ORDER BY id DESC""", (s, u)):
        acc.add(f"guard:{slug(r['action'].replace(':', '_'), 3, cut=False)}", "guard", r["action"],
                f"audit:{r['id']}", r["detail"])
    if _has(conn, "command_approvals"):
        for r in conn.execute("""SELECT id, command FROM command_approvals WHERE status = 'refused'
                                 AND COALESCE(decided_at, created_at) >= ? AND COALESCE(decided_at, created_at) < ?""",
                              (s, u)):
            acc.add("guard:command_refused", "guard", "command approval refused", f"approval:{r['id']}", r["command"])
    for r in conn.execute("""SELECT id, title FROM tasks WHERE title LIKE 'Approve or run:%' AND created_at >= ?
                             AND created_at < ?""", (s, u)):
        acc.add("guard:command_needs_owner", "guard", "command sent to the owner", _t(r["id"]), r["title"])


def _loops(conn, acc: _Acc, s: str, u: str) -> None:
    for r in conn.execute("""SELECT entity_id, COUNT(*) AS n FROM audit_log WHERE action = 'claim' AND entity = 'task'
                             AND at >= ? AND at < ? GROUP BY entity_id HAVING COUNT(*) >= ? ORDER BY n DESC""",
                          (s, u, LOOP_CLAIMS)):
        acc.add("loop:claim_repeat", "loop", f"tasks claimed ≥ {LOOP_CLAIMS}×", _t(r["entity_id"]),
                f"{_t(r['entity_id'])} claimed {r['n']}×")
    for r in conn.execute("""SELECT id FROM audit_log WHERE action IN ('chat_loop', 'access_loop_hold')
                             AND at >= ? AND at < ? ORDER BY id DESC""", (s, u)):
        acc.add("loop:chat", "loop", "loop holds (chat, access)", f"audit:{r['id']}")


def _deploys(conn, acc: _Acc, s: str, u: str) -> None:
    if not _has(conn, "deploys"):
        return
    for r in conn.execute("""SELECT id, status, stage, log FROM deploys WHERE status != 'ok' AND created_at >= ?
                             AND created_at < ? ORDER BY id DESC""", (s, u)):
        acc.add(f"deploy_{r['status']}:{slug(r['stage'] or 'unknown', 1, cut=False)}", "deploy",
                f"deploys {r['status']} at {r['stage'] or '?'}", f"deploy:{r['id']}", (r["log"] or "")[:200])


def _owner(conn, acc: _Acc, s: str, u: str) -> None:
    from .. import frustration

    end = datetime.fromisoformat(u)
    days = max(1, round((end - datetime.fromisoformat(s)).total_seconds() / 86400))
    try:
        st = frustration.stats(conn, end, days)
    except Exception:  # noqa: BLE001 - the digest goes on without it
        log.exception("frustration stats failed")
        st = {}
    if st.get("unanswered"):
        acc.set("owner:unanswered_2h", "owner", "owner messages unanswered > 2 h", st["unanswered"])
    if st.get("double_answers"):
        acc.set("owner:double_answer", "owner", "two agent replies to one owner message", st["double_answers"])
    try:
        for f in frustration.flagged(conn, s, u):
            acc.add("owner:frustration", "owner", "owner frustration flags", f"msg:{f['message_id']}", f.get("body") or "")
    except Exception:  # noqa: BLE001
        log.exception("frustration flags failed")


def _agents(conn, acc: _Acc, s: str, u: str) -> None:
    for r in conn.execute("""SELECT a.name, SUM(r.status = 'ok') AS ok, SUM(r.status = 'error') AS err,
                                    MAX(CASE WHEN r.status = 'error' THEN r.id END) AS last
                             FROM runs r JOIN actors a ON a.id = r.actor_id
                             WHERE r.started_at >= ? AND r.started_at < ? AND r.status IN ('ok', 'error')
                             GROUP BY a.name HAVING SUM(r.status = 'error') > 0""", (s, u)):
        rate = r["ok"] / (r["ok"] + r["err"]) if (r["ok"] + r["err"]) else None
        acc.set(f"agent_fail:{slug(r['name'], 4, cut=False)}", "agent_fail", f"{r['name']}: failed runs", r["err"],
                [f"run:{r['last']}"], f"{r['name']}: {r['ok']} ok, {r['err']} failed"
                + (f", success {round(rate * 100)} %" if rate is not None else ""))


def _spend(conn, acc: _Acc, s: str, u: str) -> None:
    from .. import business

    try:
        split = business.cost_split(conn, s, u)
    except Exception:  # noqa: BLE001
        log.exception("cost split failed")
        return
    for cat in ("business", "platform", "demo", "total"):
        v = split.get(f"{cat}_usd")
        if v:
            acc.set(f"spend:{cat}", "spend", f"USD on {cat}", round(v, 2))


EVENT_COLLECTORS = (_runs, _tools, _guard, _loops, _deploys, _owner, _agents, _spend)


def events(conn: sqlite3.Connection, s: str, u: str) -> dict[str, dict]:
    """Every event signal in [s, u): {key: {key, category, label, count, examples, sample}}."""
    acc = _Acc()
    for fn in EVENT_COLLECTORS:
        try:
            fn(conn, acc, s, u)
        except sqlite3.OperationalError:  # a table this install does not have
            log.exception("signal collector %s failed", fn.__name__)
    return acc.items


def snapshot(conn: sqlite3.Connection, now: datetime) -> dict[str, dict]:
    """States now: results waiting for review past the SLA, waiting tasks past their date."""
    from .. import review_policy

    acc = _Acc()
    sla = _iso(now - timedelta(hours=review_policy.SLA_HOURS))
    rows = conn.execute("""SELECT id, title FROM tasks WHERE status = 'review' AND archived_at IS NULL AND updated_at < ?
                           ORDER BY updated_at""", (sla,)).fetchall()
    if rows:
        acc.set("stuck:review", "stuck", f"reviews older than {review_policy.SLA_HOURS} h", len(rows),
                [_t(r["id"]) for r in rows], rows[0]["title"])
    day = now.astimezone(TZ).date().isoformat()
    rows = conn.execute("""SELECT id, title FROM tasks WHERE status = 'waiting' AND archived_at IS NULL
                           AND ((follow_up IS NOT NULL AND follow_up < ?) OR (deadline IS NOT NULL AND deadline < ?))
                           ORDER BY COALESCE(follow_up, deadline)""", (day, day)).fetchall()
    if rows:
        acc.set("stuck:waiting_overdue", "stuck", "waiting tasks past follow-up or deadline", len(rows),
                [_t(r["id"]) for r in rows], rows[0]["title"])
    return acc.items


# ------------------------------------------------------------------ the digest

def compute(conn: sqlite3.Connection, now: datetime | None = None, extra: dict[str, dict] | None = None) -> list[dict]:
    """The digest for `now`: per key the 24 h and 7 d counts with the previous periods, examples."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    windows = {
        "count_24h": events(conn, _iso(now - timedelta(days=1)), _iso(now)),
        "prev_24h": events(conn, _iso(now - timedelta(days=2)), _iso(now - timedelta(days=1))),
        "count_7d": events(conn, _iso(now - timedelta(days=7)), _iso(now)),
        "prev_7d": events(conn, _iso(now - timedelta(days=14)), _iso(now - timedelta(days=7))),
    }
    snap = {**snapshot(conn, now), **(extra or {})}
    day = day_of(now)
    yesterday = _stored_map(conn, (date.fromisoformat(day) - timedelta(days=1)).isoformat())
    week_ago = _stored_map(conn, (date.fromisoformat(day) - timedelta(days=7)).isoformat())
    out: dict[str, dict] = {}
    for col, items in windows.items():
        for key, it in items.items():
            row = out.setdefault(key, {"key": key, "category": it["category"], "label": it["label"], "count_24h": 0.0,
                                       "prev_24h": 0.0, "count_7d": 0.0, "prev_7d": 0.0, "examples": [], "sample": ""})
            row[col] = it["count"]
            if col in ("count_7d", "count_24h"):
                for e in it["examples"]:
                    if e not in row["examples"] and len(row["examples"]) < EXAMPLES:
                        row["examples"].append(e)
                row["sample"] = row["sample"] or it["sample"]
    for key, it in snap.items():  # a state: the same number for both windows, the previous ones stored
        out[key] = {"key": key, "category": it["category"], "label": it["label"], "count_24h": it["count"],
                    "count_7d": it["count"], "prev_24h": (yesterday.get(key) or {}).get("count_24h", 0.0 if yesterday else None),
                    "prev_7d": (week_ago.get(key) or {}).get("count_7d", 0.0 if week_ago else None),
                    "examples": it["examples"], "sample": it["sample"]}
    # A key gone this week but present the week before stays (count 0): the trend is the news.
    return sorted(out.values(), key=lambda r: (r["category"], r["key"]))


def store(conn: sqlite3.Connection, day: str, rows: list[dict]) -> None:
    ensure_schema(conn)
    conn.execute("DELETE FROM improve_signals WHERE day = ?", (day,))
    at = now_iso()
    conn.executemany(
        """INSERT INTO improve_signals (day, key, category, label, count_24h, prev_24h, count_7d, prev_7d, examples,
                                        sample, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [(day, r["key"], r["category"], r["label"], r["count_24h"], r["prev_24h"], r["count_7d"], r["prev_7d"],
          json.dumps(r["examples"], default=str), r["sample"], at) for r in rows])


def _row_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["examples"] = json.loads(d.get("examples") or "[]")
    return d


def stored(conn: sqlite3.Connection, day: str) -> list[dict]:
    ensure_schema(conn)
    return [_row_dict(r) for r in conn.execute("SELECT * FROM improve_signals WHERE day = ? ORDER BY category, key",
                                               (day,))]


def _stored_map(conn: sqlite3.Connection, day: str) -> dict[str, dict]:
    return {r["key"]: r for r in stored(conn, day)}


def has_day(conn: sqlite3.Connection, day: str) -> bool:
    ensure_schema(conn)
    return conn.execute("SELECT 1 FROM improve_signals WHERE day = ? LIMIT 1", (day,)).fetchone() is not None


def latest_day(conn: sqlite3.Connection, on_or_before: str | None = None) -> str | None:
    ensure_schema(conn)
    if on_or_before:
        r = conn.execute("SELECT MAX(day) FROM improve_signals WHERE day <= ?", (on_or_before,)).fetchone()
    else:
        r = conn.execute("SELECT MAX(day) FROM improve_signals").fetchone()
    return r[0] if r else None


def latest(conn: sqlite3.Connection) -> tuple[str | None, list[dict]]:
    day = latest_day(conn)
    return day, (stored(conn, day) if day else [])


def value(conn: sqlite3.Connection, key: str, day: str, window: str = "7d") -> float | None:
    """A key's count in the digest of `day` (0 when that day's digest exists without it; None without a digest)."""
    ensure_schema(conn)
    r = conn.execute(f"SELECT count_{window} FROM improve_signals WHERE day = ? AND key = ?", (day, key)).fetchone()
    if r is not None:
        return float(r[0] or 0)
    return 0.0 if has_day(conn, day) else None


# ------------------------------------------------------------------ ranking

def trend(row: dict) -> float:
    """(this week + 1) / (last week + 1), clamped to 0.5 .. 2."""
    prev = row.get("prev_7d")
    if prev is None:
        return 1.0
    return max(0.5, min(2.0, (float(row["count_7d"]) + 1) / (float(prev) + 1)))


def score(row: dict) -> float:
    """Impact × trend: the category's weight × √(count this week) × the trend."""
    w = WEIGHTS.get(row["category"], 1.0)
    if not w or not row.get("count_7d"):
        return 0.0
    return round(w * math.sqrt(float(row["count_7d"])) * trend(row), 2)


def ranked(rows: list[dict], limit: int | None = None) -> list[dict]:
    out = sorted(({**r, "score": score(r)} for r in rows), key=lambda r: (-r["score"], r["key"]))
    out = [r for r in out if r["score"] > 0]
    return out[:limit] if limit else out


def _num(v) -> str:
    if v is None:
        return "—"
    v = float(v)
    return str(int(v)) if v.is_integer() else f"{v:.2f}"


def _ex(e) -> str:
    return str(e)


def render_line(r: dict) -> str:
    arrow = ""
    if r.get("prev_7d") is not None:
        arrow = " ↑" if r["count_7d"] > r["prev_7d"] else (" ↓" if r["count_7d"] < r["prev_7d"] else " =")
    ex = ", ".join(_ex(e) for e in (r.get("examples") or [])[:EXAMPLES])
    sample = f" — „{r['sample'][:140]}“" if r.get("sample") else ""
    return (f"`{r['key']}` {_num(r['count_7d'])}/7 d (předtím {_num(r.get('prev_7d'))}{arrow}; 24 h "
            f"{_num(r['count_24h'])}){' · příklady: ' + ex if ex else ''}{sample}")


def by_category(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        out[r["category"]].append(r)
    return dict(out)


def daily(conn: sqlite3.Connection, now: datetime | None = None, extra: dict[str, dict] | None = None) -> dict:
    """Compute and store today's digest (idempotent: a second run the same day replaces it)."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    ensure_schema(conn)
    rows = compute(conn, now, extra)
    day = day_of(now)
    store(conn, day, rows)
    conn.commit()
    top = ranked(rows, 3)
    return {"day": day, "signals": len(rows), "top": [f"{r['key']}={_num(r['count_7d'])}" for r in top]}
