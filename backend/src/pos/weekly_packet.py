"""The week packet: one compact JSON of what happened in a week, built by code.

The Chief of Staff (pos.weekly) writes the weekly report from this packet, so
the model reads numbers, not raw data, and the numbers are the same ones the
report page draws. Nothing here costs tokens.

What it holds (all for one ISO week, Monday 00:00 Europe/Prague, up to now
while the week is still running):

- tasks: done, new, in progress, in review, waiting, overdue; done per day;
  split by project (or topic) and by assignee (people and agents); highlights
  (the highest-priority and largest done items);
- agents: runs, success rate, cost and tokens, cost per accepted task,
  handbacks and returns (runs, engine_usage, history);
- dev work: commits on the default branch per repository (GitHub, with
  POS_GITHUB_TOKEN) and deploys (the deployer's records);
- communication: new mail, Discord and other items in knowlage by origin and
  top channels (one read of its document list, only when configured);
- incidents: failed or reverted deploys, failed runs, kill-switch freezes;
- goals with progress and the change since the last report, and the tasks the
  last meeting created with their state;
- week-over-week deltas: flow numbers against the same part of the previous
  week, snapshot numbers against the previous stored packet.

Outside sources fail soft: a missing token or an unreachable service gives
`available: false` and a note, never an error.
"""

import json
import logging
import os
import sqlite3
from collections import Counter
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone

from . import goals as goals_mod
from .core import TZ, today

log = logging.getLogger(__name__)

EXCLUDED_TOPICS = ("chat",)  # chat answers are conversation, not work; they would drown the numbers
DAY_NAMES = ["Po", "Út", "St", "Čt", "Pá", "So", "Ne"]
HIGHLIGHTS = 8
LIST_MAX = 6
GITHUB_MAX_REPOS = 12

# Outside reads, replaceable in tests: (url, headers, params) -> parsed JSON.
http_get: Callable[[str, dict, dict], object] | None = None


# ------------------------------------------------------------------ weeks

def week_key(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def current_week() -> str:
    return week_key(today())


def parse_week(key: str) -> date:
    """'2026-W39' -> that week's Monday."""
    try:
        y, w = key.strip().upper().split("-W")
        return date.fromisocalendar(int(y), int(w), 1)
    except (ValueError, AttributeError) as e:
        raise ValueError(f"a week is written like 2026-W39, not {key!r}") from e


def previous_week(key: str) -> str:
    return week_key(parse_week(key) - timedelta(days=7))


def bounds(key: str) -> tuple[datetime, datetime]:
    """[Monday 00:00, next Monday 00:00) in Prague time, as UTC datetimes."""
    monday = parse_week(key)
    start = datetime.combine(monday, time(0, 0), tzinfo=TZ)
    return start.astimezone(timezone.utc), (start + timedelta(days=7)).astimezone(timezone.utc)


def _iso(at: datetime) -> str:
    return at.astimezone(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ helpers

def _one(conn: sqlite3.Connection, sql: str, *args) -> int:
    return conn.execute(sql, args).fetchone()[0] or 0


def _has(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (table,)).fetchone() is not None


def _not_excluded(alias: str = "t") -> str:
    """Chat answers, and the seed/demo tasks of the first days (pos.business: the made-up Acme and house
    examples, or value_kind 'demo'), are not work the report counts."""
    from .business import DEMO_TITLES

    marks = ",".join(f"'{t}'" for t in EXCLUDED_TOPICS)
    demo = ",".join("'" + t.replace("'", "''") + "'" for t in sorted(DEMO_TITLES))
    return (f"COALESCE({alias}.topic, '') NOT IN ({marks}, 'acme') AND LOWER({alias}.title) NOT IN ({demo}) "
            f"AND LOWER({alias}.title) NOT LIKE '%acme%' AND COALESCE({alias}.value_kind, '') != 'demo'")


def _delta(value, prev) -> dict:
    out = {"value": value, "prev": prev}
    if isinstance(value, (int, float)) and isinstance(prev, (int, float)):
        out["delta"] = round(value - prev, 4) if isinstance(value, float) or isinstance(prev, float) else value - prev
    else:
        out["delta"] = None
    return out


def _ref(task_id: int) -> str:
    return f"T-{task_id:03d}"


# ------------------------------------------------------------------ tasks

def _task_flow(conn: sqlite3.Connection, s: str, u: str) -> dict:
    ex = _not_excluded()
    done = _one(conn, f"SELECT COUNT(*) FROM tasks t WHERE t.status = 'done' AND t.completed_at >= ? "
                      f"AND t.completed_at < ? AND t.archived_at IS NULL AND {ex}", s, u)
    new = _one(conn, f"SELECT COUNT(*) FROM tasks t WHERE t.created_at >= ? AND t.created_at < ? "
                     f"AND t.archived_at IS NULL AND {ex}", s, u)
    return {"done": done, "new": new}


def _tasks(conn: sqlite3.Connection, start: datetime, until: datetime, prev_start: datetime,
           prev_until: datetime) -> dict:
    s, u = _iso(start), _iso(until)
    ps, pu = _iso(prev_start), _iso(prev_until)
    ex = _not_excluded()
    flow, prev_flow = _task_flow(conn, s, u), _task_flow(conn, ps, pu)
    now_open = {r["status"]: r["n"] for r in conn.execute(
        f"""SELECT t.status, COUNT(*) AS n FROM tasks t WHERE t.archived_at IS NULL AND {ex}
            AND t.status IN ('inbox', 'next', 'working', 'review', 'waiting') GROUP BY t.status""")}
    today_iso = today().isoformat()
    overdue_rows = conn.execute(
        f"""SELECT t.id, t.title, t.deadline, t.assignee_name, t.priority FROM tasks t
            WHERE t.archived_at IS NULL AND t.status NOT IN ('done', 'someday') AND t.deadline IS NOT NULL
            AND t.deadline < ? AND {ex} ORDER BY t.deadline, COALESCE(t.priority, 4) LIMIT 200""",
        (today_iso,)).fetchall()
    waiting_rows = conn.execute(
        f"""SELECT t.id, t.title, t.assignee_name, t.updated_at, t.progress_note FROM tasks t
            WHERE t.archived_at IS NULL AND t.status = 'waiting' AND {ex}
            ORDER BY t.updated_at LIMIT ?""", (LIST_MAX,)).fetchall()

    # Done per day, Monday to Sunday, with the same weekday of the previous week.
    by_day = Counter()
    prev_by_day = Counter()
    for col, lo, hi, bucket in (("completed_at", s, u, by_day), ("completed_at", ps, pu, prev_by_day)):
        for r in conn.execute(
                f"SELECT t.{col} AS at FROM tasks t WHERE t.status = 'done' AND t.{col} >= ? AND t.{col} < ? "
                f"AND t.archived_at IS NULL AND {ex}", (lo, hi)):
            bucket[datetime.fromisoformat(r["at"]).astimezone(TZ).weekday()] += 1
    monday = start.astimezone(TZ).date()
    done_by_day = [{"date": (monday + timedelta(days=i)).isoformat(), "day": DAY_NAMES[i],
                    "done": by_day.get(i, 0), "prev": prev_by_day.get(i, 0)} for i in range(7)]

    # Work split by project (else topic): done this week, new, and open now.
    split: dict[str, dict] = {}

    def bucket(name: str | None) -> dict:
        key = name or "—"
        return split.setdefault(key, {"name": key, "done": 0, "new": 0, "open": 0})

    label = "COALESCE(p.name, '#' || t.topic)"
    for r in conn.execute(f"""SELECT {label} AS name, COUNT(*) AS n FROM tasks t LEFT JOIN projects p ON p.id = t.project_id
                              WHERE t.status = 'done' AND t.completed_at >= ? AND t.completed_at < ?
                              AND t.archived_at IS NULL AND {ex} GROUP BY 1""", (s, u)):
        bucket(r["name"])["done"] = r["n"]
    for r in conn.execute(f"""SELECT {label} AS name, COUNT(*) AS n FROM tasks t LEFT JOIN projects p ON p.id = t.project_id
                              WHERE t.created_at >= ? AND t.created_at < ? AND t.archived_at IS NULL AND {ex}
                              GROUP BY 1""", (s, u)):
        bucket(r["name"])["new"] = r["n"]
    for r in conn.execute(f"""SELECT {label} AS name, COUNT(*) AS n FROM tasks t LEFT JOIN projects p ON p.id = t.project_id
                              WHERE t.archived_at IS NULL AND t.status IN ('next', 'working', 'review', 'waiting')
                              AND {ex} GROUP BY 1"""):
        bucket(r["name"])["open"] = r["n"]
    by_project = sorted(split.values(), key=lambda x: (-x["done"], -x["open"], x["name"]))
    if len(by_project) > 10:  # the rest as one slice, so the chart stays readable
        rest = by_project[9:]
        by_project = by_project[:9] + [{"name": f"ostatní ({len(rest)})", "done": sum(x["done"] for x in rest),
                                        "new": sum(x["new"] for x in rest), "open": sum(x["open"] for x in rest)}]

    # By assignee: people and agents.
    people: dict[str, dict] = {}
    for r in conn.execute(
            f"""SELECT COALESCE(t.assignee_name, '—') AS name, COALESCE(t.assignee_type, '-') AS kind,
                       COALESCE(SUM(t.status = 'done' AND t.completed_at >= ? AND t.completed_at < ?), 0) AS done,
                       COALESCE(SUM(t.status IN ('next', 'working', 'review')), 0) AS open,
                       COALESCE(SUM(t.status = 'waiting'), 0) AS waiting
                FROM tasks t WHERE t.archived_at IS NULL AND {ex} AND t.assignee_name IS NOT NULL
                GROUP BY 1, 2""", (s, u)):
        if r["done"] or r["open"] or r["waiting"]:
            people[r["name"]] = {"name": r["name"], "kind": "agent" if r["kind"] in ("ai", "agent") else r["kind"],
                                 "done": r["done"], "open": r["open"], "waiting": r["waiting"]}
    by_assignee = sorted(people.values(), key=lambda x: (-x["done"], -x["open"], x["name"]))[:15]

    highlights = [
        {"ref": _ref(r["id"]), "title": r["title"], "assignee": r["assignee_name"], "priority": r["priority"],
         "project": r["name"], "estimate_min": r["estimate_min"]}
        for r in conn.execute(
            f"""SELECT t.id, t.title, t.assignee_name, t.priority, t.estimate_min, {label} AS name
                FROM tasks t LEFT JOIN projects p ON p.id = t.project_id
                WHERE t.status = 'done' AND t.completed_at >= ? AND t.completed_at < ? AND t.archived_at IS NULL
                AND t.parent_id IS NULL AND {ex}
                ORDER BY COALESCE(t.priority, 4), COALESCE(t.estimate_min, 0) DESC,
                         (SELECT COUNT(*) FROM tasks c WHERE c.parent_id = t.id) DESC, t.completed_at DESC
                LIMIT ?""", (s, u, HIGHLIGHTS))]

    return {
        "done": flow["done"], "new": flow["new"],
        "prev": prev_flow,
        "in_progress": now_open.get("working", 0) + now_open.get("next", 0),
        "working": now_open.get("working", 0),
        "review": now_open.get("review", 0),
        "waiting": now_open.get("waiting", 0),
        "inbox": now_open.get("inbox", 0),
        "overdue": len(overdue_rows),
        "done_by_day": done_by_day,
        "by_project": by_project,
        "by_assignee": by_assignee,
        "highlights": highlights,
        "overdue_list": [{"ref": _ref(r["id"]), "title": r["title"], "deadline": r["deadline"],
                          "assignee": r["assignee_name"]} for r in overdue_rows[:LIST_MAX]],
        "waiting_list": [{"ref": _ref(r["id"]), "title": r["title"], "assignee": r["assignee_name"],
                          "since": (r["updated_at"] or "")[:10], "why": (r["progress_note"] or "")[:140]}
                         for r in waiting_rows],
    }


# ------------------------------------------------------------------ agents

def _agent_numbers(conn: sqlite3.Connection, aid: int, s: str, u: str) -> dict:
    runs = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) AS n FROM runs WHERE actor_id = ? AND started_at >= ? AND started_at < ? "
        "GROUP BY status", (aid, s, u))}
    ok, err = runs.get("ok", 0), runs.get("error", 0)
    cost = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0), COALESCE(SUM(input_tokens + output_tokens), 0) FROM engine_usage "
        "WHERE actor_id = ? AND at >= ? AND at < ?", (aid, s, u)).fetchone()
    codex_tokens = 0
    if _has(conn, "budget_runs"):
        try:
            from .budget import store as budget_store

            codex_tokens = budget_store.tokens_between(conn, datetime.fromisoformat(s), datetime.fromisoformat(u),
                                                       str(aid))
        except Exception:  # noqa: BLE001 - the budget store's shape is its own; tokens stay partial
            codex_tokens = 0
    accepted = _one(conn, "SELECT COUNT(*) FROM tasks WHERE assignee_id = ? AND status = 'done' "
                          "AND completed_at >= ? AND completed_at < ?", aid, s, u)
    handbacks = _one(conn, """SELECT COUNT(DISTINCT entity_id) FROM history WHERE entity = 'task' AND actor_id = ?
                              AND at >= ? AND at < ? AND json_extract(data, '$.progress_note') LIKE '%handed it back%'""",
                     aid, s, u)
    returned = _one(conn, """SELECT COUNT(*) FROM history WHERE entity = 'task' AND action = 'return' AND at >= ?
                             AND at < ? AND json_extract(data, '$.assignee_id') = ?""", s, u, aid)
    return {"runs": sum(runs.values()), "ok": ok, "errors": err, "cost_usd": round(float(cost[0]), 4),
            "tokens": int(cost[1]) + int(codex_tokens), "accepted": accepted, "handbacks": handbacks,
            "returned": returned}


def _agents(conn: sqlite3.Connection, s: str, u: str, ps: str, pu: str) -> dict:
    rows = conn.execute("SELECT id, name, archived_at FROM actors WHERE kind IN ('ai', 'agent') ORDER BY id").fetchall()
    per, tot, prev = [], Counter(), Counter()
    for r in rows:
        n = _agent_numbers(conn, r["id"], s, u)
        p = _agent_numbers(conn, r["id"], ps, pu)
        for k in ("runs", "ok", "errors", "cost_usd", "tokens", "accepted", "handbacks", "returned"):
            tot[k] += n[k]
            prev[k] += p[k]
        if n["runs"] or n["accepted"] or n["handbacks"]:
            rate = n["ok"] / (n["ok"] + n["errors"]) if (n["ok"] + n["errors"]) else None
            per.append({"id": r["id"], "name": r["name"], "archived": bool(r["archived_at"]), **n,
                        "success_rate": round(rate, 3) if rate is not None else None,
                        "cost_per_accepted": round(n["cost_usd"] / n["accepted"], 3) if n["accepted"] else None})
    per.sort(key=lambda x: (-x["cost_usd"], -x["runs"]))

    def rate(c: Counter) -> float | None:
        return round(c["ok"] / (c["ok"] + c["errors"]), 3) if (c["ok"] + c["errors"]) else None

    return {
        "runs": tot["runs"], "ok": tot["ok"], "errors": tot["errors"], "success_rate": rate(tot),
        "cost_usd": round(tot["cost_usd"], 2), "tokens": tot["tokens"], "accepted": tot["accepted"],
        "cost_per_accepted": round(tot["cost_usd"] / tot["accepted"], 3) if tot["accepted"] else None,
        "handbacks": tot["handbacks"], "returned": tot["returned"],
        "prev": {"runs": prev["runs"], "success_rate": rate(prev), "cost_usd": round(prev["cost_usd"], 2),
                 "accepted": prev["accepted"], "handbacks": prev["handbacks"]},
        "per_agent": per[:12],
    }


# ------------------------------------------------------------------ dev work

def _get_json(url: str, headers: dict, params: dict) -> object:
    if http_get is not None:
        return http_get(url, headers, params)
    import httpx

    r = httpx.get(url, headers=headers, params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def report_repos() -> tuple[list[str], str | None]:
    """(explicit repos, org to list): POS_REPORT_REPOS wins; else POS_REPORT_GITHUB_ORG (default ObseumEU)."""
    explicit = [r.strip() for r in os.environ.get("POS_REPORT_REPOS", "").split(",") if r.strip()]
    return explicit, (None if explicit else os.environ.get("POS_REPORT_GITHUB_ORG", "ObseumEU"))


def _dev(conn: sqlite3.Connection, s: str, u: str, ps: str, pu: str) -> dict:
    deploys = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) AS n FROM deploys WHERE created_at >= ? AND created_at < ? GROUP BY status", (s, u))}
    prev_deploys = _one(conn, "SELECT COUNT(*) FROM deploys WHERE status = 'ok' AND created_at >= ? AND created_at < ?",
                        ps, pu)
    shipped = _one(conn, "SELECT COALESCE(SUM(commits), 0) FROM deploys WHERE status = 'ok' AND created_at >= ? "
                         "AND created_at < ?", s, u)
    out: dict = {"available": False, "note": "", "repos": [], "commits": 0, "merges": 0, "prev_commits": None,
                 "deploys": {"ok": deploys.get("ok", 0), "reverted": deploys.get("reverted", 0),
                             "rejected": deploys.get("rejected", 0), "error": deploys.get("error", 0)},
                 "deploys_prev_ok": prev_deploys, "commits_shipped_by_deployer": shipped}
    token = os.environ.get("POS_GITHUB_TOKEN") or os.environ.get("POS_REPORT_GITHUB_TOKEN")
    if not token:
        out["note"] = "no GitHub token (POS_GITHUB_TOKEN): commits come only from the deployer's records"
        return out
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    explicit, org = report_repos()
    try:
        if explicit:
            repos = explicit[:GITHUB_MAX_REPOS]
        else:
            listed = _get_json(f"https://api.github.com/orgs/{org}/repos", headers,
                               {"sort": "pushed", "direction": "desc", "per_page": 50, "type": "all"}) or []
            repos = [r["full_name"] for r in listed if isinstance(r, dict) and (r.get("pushed_at") or "") >= ps[:19]
                     and not r.get("archived")][:GITHUB_MAX_REPOS]
        total, merges, prev_total = 0, 0, 0
        for repo in repos:
            commits = _get_json(f"https://api.github.com/repos/{repo}/commits", headers,
                                {"since": s, "until": u, "per_page": 100}) or []
            before = _get_json(f"https://api.github.com/repos/{repo}/commits", headers,
                               {"since": ps, "until": pu, "per_page": 100}) or []
            normal = [c for c in commits if len(c.get("parents") or []) < 2]
            merged = len(commits) - len(normal)
            authors = Counter(((c.get("author") or {}).get("login") or (c.get("commit") or {}).get("author", {})
                               .get("name") or "?") for c in normal)
            prev_normal = sum(1 for c in before if len(c.get("parents") or []) < 2)
            total, merges, prev_total = total + len(normal), merges + merged, prev_total + prev_normal
            if commits or before:
                out["repos"].append({"repo": repo, "commits": len(normal), "merges": merged, "prev": prev_normal,
                                     "authors": [a for a, _ in authors.most_common(4)]})
        out["repos"].sort(key=lambda r: -r["commits"])
        out.update(available=True, commits=total, merges=merges, prev_commits=prev_total,
                   note=f"default branches of {len(repos)} repositories" + (f" in {org}" if org else ""))
    except Exception as e:  # noqa: BLE001 - GitHub down or the token lacks access: say so, keep the rest
        log.warning("weekly packet: GitHub read failed: %s", e)
        out["note"] = f"GitHub unavailable: {str(e)[:160]}"
    return out


# ------------------------------------------------------------------ communication

def _communication(s: str, u: str, ps: str, pu: str) -> dict:
    """New items in knowlage this week by origin (mail, discord, …) and top channels
    (customer domains, Discord channels). One read; off unless configured."""
    if not (os.environ.get("POS_KNOWLAGE_API_KEY") or os.environ.get("POS_REPORT_KNOWLAGE") == "1") \
            or os.environ.get("POS_REPORT_KNOWLAGE") == "0":
        return {"available": False, "note": "knowlage not configured for reports (POS_KNOWLAGE_API_KEY)"}
    try:
        from . import knowledge

        if http_get is not None:
            docs = http_get(knowledge.base_url() + "/api/documents", {}, {})
        else:
            docs = knowledge._get("/api/documents", timeout=30)
    except Exception as e:  # noqa: BLE001
        return {"available": False, "note": f"knowlage unavailable: {str(e)[:160]}"}

    def when(d: dict) -> str:
        v = str(d.get("addedAt") or d.get("date") or "")
        try:
            at = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return _iso(at if at.tzinfo else at.replace(tzinfo=timezone.utc))
        except ValueError:
            return ""

    week = [d for d in docs or [] if isinstance(d, dict) and s <= when(d) < u]
    prev = sum(1 for d in docs or [] if isinstance(d, dict) and ps <= when(d) < pu)
    origins = Counter(str(d.get("origin") or d.get("kind") or "other") for d in week)
    channels = Counter((str(d.get("origin") or "other"), str(d.get("channel") or d.get("workspace") or "—"))
                       for d in week if d.get("origin") in ("mail", "email", "gmail", "mailbox", "discord", "slack"))
    return {"available": True, "items": len(week), "prev_items": prev,
            "by_origin": dict(origins.most_common(8)),
            "top_channels": [{"origin": o, "channel": c, "items": n} for (o, c), n in channels.most_common(6)],
            "note": "new documents in knowlage this week (mail threads, Discord, files)"}


# ------------------------------------------------------------------ incidents

def _incidents(conn: sqlite3.Connection, s: str, u: str) -> dict:
    bad = [{"status": r["status"], "stage": r["stage"], "author": r["author"], "at": r["created_at"][:16],
            "sha": (r["new_sha"] or "")[:8]}
           for r in conn.execute("SELECT * FROM deploys WHERE status != 'ok' AND created_at >= ? AND created_at < ? "
                                 "ORDER BY id DESC LIMIT ?", (s, u, LIST_MAX))]
    failed_runs = _one(conn, "SELECT COUNT(*) FROM runs WHERE status = 'error' AND started_at >= ? AND started_at < ?",
                       s, u)
    freezes = _one(conn, "SELECT COUNT(*) FROM audit_log WHERE action IN ('freeze', 'killswitch_freeze') "
                         "AND at >= ? AND at < ?", s, u)
    limits = _one(conn, "SELECT COUNT(*) FROM engine_limits WHERE updated_at >= ? AND updated_at < ? "
                        "AND paused_until IS NOT NULL", s, u)
    return {"failed_deploys": bad, "failed_runs": failed_runs, "freezes": freezes, "engine_limit_hits": limits}


# ------------------------------------------------------------------ goals and the last meeting

def _stored(conn: sqlite3.Connection, week: str) -> sqlite3.Row | None:
    if not _has(conn, "weekly_reports"):
        return None
    return conn.execute("SELECT * FROM weekly_reports WHERE week = ?", (week,)).fetchone()


def _goals(conn: sqlite3.Connection, prev_packet: dict | None) -> list[dict]:
    before = {g["id"]: g.get("progress") for g in (prev_packet or {}).get("goals", [])}
    out = []
    for g in goals_mod.list_goals(conn, "all"):
        if g["status"] == "dropped":
            continue
        if g["status"] == "done" and g["id"] not in before:
            continue  # finished long ago; the report shows only what moved or is live
        b = goals_mod.brief(g)
        b["progress_prev"] = before.get(g["id"])
        b["delta"] = (b["progress"] - b["progress_prev"]) if b["progress_prev"] is not None else None
        out.append(b)
    return out


def _last_meeting(conn: sqlite3.Connection, prev_row: sqlite3.Row | None) -> dict | None:
    if prev_row is None:
        return None
    refs = json.loads(prev_row["created_tasks"] or "[]")
    ids = [int(str(r).upper().removeprefix("T-")) for r in refs if str(r).upper().removeprefix("T-").isdigit()]
    tasks = []
    for tid in ids[:15]:
        t = conn.execute("SELECT id, title, status, assignee_name FROM tasks WHERE id = ?", (tid,)).fetchone()
        if t:
            tasks.append({"ref": _ref(t["id"]), "title": t["title"], "status": t["status"],
                          "assignee": t["assignee_name"]})
    return {"week": prev_row["week"], "status": prev_row["status"],
            "decisions": json.loads(prev_row["decisions"] or "[]")[:8],
            "tasks": tasks, "tasks_done": sum(1 for t in tasks if t["status"] == "done")}


# ------------------------------------------------------------------ build

def build(conn: sqlite3.Connection, week: str | None = None, *, now: datetime | None = None,
          outside: bool = True) -> dict:
    """The packet for `week` (default: the current one). `outside=False` skips GitHub and knowlage."""
    week = (week or current_week()).strip().upper()
    start, end = bounds(week)
    now = now or datetime.now(timezone.utc)
    until = min(end, now)
    if until <= start:
        raise ValueError(f"{week} has not started yet")
    span = until - start
    prev_start = start - timedelta(days=7)
    prev_until = prev_start + span
    s, u, ps, pu = _iso(start), _iso(until), _iso(prev_start), _iso(prev_until)

    prev_row = _stored(conn, previous_week(week))
    prev_packet = json.loads(prev_row["packet"]) if prev_row and prev_row["packet"] else None

    from . import business

    business.ensure_schema(conn)  # tasks.value_kind (the demo filter reads it)
    tasks = _tasks(conn, start, until, prev_start, prev_until)
    agents = _agents(conn, s, u, ps, pu)
    dev = _dev(conn, s, u, ps, pu) if outside else {"available": False, "note": "skipped", "repos": [], "commits": 0,
                                                    "merges": 0, "prev_commits": None, "deploys": {}}
    comm = _communication(s, u, ps, pu) if outside else {"available": False, "note": "skipped"}
    snap_prev = (prev_packet or {}).get("tasks", {})

    kpis = {
        "tasks_done": _delta(tasks["done"], tasks["prev"]["done"]),
        "tasks_new": _delta(tasks["new"], tasks["prev"]["new"]),
        "waiting": _delta(tasks["waiting"], snap_prev.get("waiting")),
        "overdue": _delta(tasks["overdue"], snap_prev.get("overdue")),
        "agent_runs": _delta(agents["runs"], agents["prev"]["runs"]),
        "agent_success_rate": _delta(agents["success_rate"], agents["prev"]["success_rate"]),
        "agent_cost_usd": _delta(agents["cost_usd"], agents["prev"]["cost_usd"]),
        "commits": _delta(dev["commits"], dev.get("prev_commits")) if dev.get("available") else
        _delta(None, None),
        "deploys": _delta((dev.get("deploys") or {}).get("ok", 0), dev.get("deploys_prev_ok")),
    }
    if comm.get("available"):
        kpis["communication"] = _delta(comm["items"], comm.get("prev_items"))
    biz = business.week_section(conn, s, u, outside=outside)
    prev_biz = (prev_packet or {}).get("business") or {}
    prev_split = prev_biz.get("cost_split") or {}
    kpis.update({
        "business_outcomes": _delta(biz["cost_split"]["business_outcomes"], prev_split.get("business_outcomes")),
        "usd_per_business_outcome": _delta(biz["cost_split"]["usd_per_business_outcome"],
                                           prev_split.get("usd_per_business_outcome")),
        "business_cost_share": _delta(biz["cost_split"]["business_share"], prev_split.get("business_share")),
        "owner_minutes": _delta(biz["owner_time"]["minutes"], (prev_biz.get("owner_time") or {}).get("minutes")),
        "customer_threads_open": _delta(biz["customer_threads"]["open"],
                                        (prev_biz.get("customer_threads") or {}).get("open")),
        "drafts_in_approvals": _delta(biz["drafts_in_approvals"]["total"],
                                      (prev_biz.get("drafts_in_approvals") or {}).get("total")),
    })
    if biz["invoices"].get("available"):
        kpis["invoices_sent"] = _delta(biz["invoices"]["sent"], (prev_biz.get("invoices") or {}).get("sent"))
        kpis["invoices_received"] = _delta(biz["invoices"]["received"],
                                           (prev_biz.get("invoices") or {}).get("received"))

    last_day = min(end - timedelta(seconds=1), until).astimezone(TZ).date()
    return {
        "week": week,
        "period": {"start": start.astimezone(TZ).date().isoformat(), "end": last_day.isoformat(),
                   "until": u, "partial": until < end,
                   "compared_with": "the same part of the previous week (flow numbers); the last report (snapshots)"},
        "generated_at": _iso(now),
        "kpis": kpis,
        "tasks": tasks,
        "agents": agents,
        "dev": dev,
        "communication": comm,
        "business": biz,
        "incidents": _incidents(conn, s, u),
        "goals": _goals(conn, prev_packet),
        "last_meeting": _last_meeting(conn, prev_row),
        "platform_improvement": _platform(conn, s, u, until),
    }


def _platform(conn: sqlite3.Connection, s: str, u: str, until: datetime) -> dict:
    """The platform improvement loop (pos.platform_loop): metric deltas and the backlog, for the report."""
    try:
        from . import platform_loop

        return {"available": True, **platform_loop.packet_section(conn, s, u, until)}  # its own flag when past
    except Exception as e:  # noqa: BLE001 - the report goes out without it
        log.exception("platform section failed")
        return {"available": False, "note": str(e)[:200]}


def summary_line(packet: dict) -> str:
    """One Czech line for lists and chat."""
    k = packet["kpis"]
    parts = [f"hotovo {k['tasks_done']['value']}", f"nové {k['tasks_new']['value']}",
             f"čeká {k['waiting']['value']}", f"po termínu {k['overdue']['value']}"]
    if k["agent_cost_usd"]["value"]:
        parts.append(f"agenti ${k['agent_cost_usd']['value']:.2f}")
    if (k.get("business_outcomes") or {}).get("value"):
        parts.append(f"byznys výsledky {k['business_outcomes']['value']}")
    if (k.get("owner_minutes") or {}).get("value") is not None:
        parts.append(f"majitel ~{k['owner_minutes']['value']} min")
    return " · ".join(parts)
