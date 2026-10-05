"""The company scorecard ("Firma"): one screen that answers "jak se firmě daří", all numbers from code.

Sections (last 7 days unless said otherwise):

- **goals**: the company goals (active and proposed) with baseline → current → target, progress, the
  owner, the due date and a trend (their `current` in the daily snapshots);
- **world**: what reached the world: outbound sent / replies / failed (pos.outbound's
  `outbound_stats` when it exists, else the audit log), publications, deploys, customers helped;
- **owner**: the owner's requests over 30 days (pos.business.owner_request): delivered %, median
  hours to done, the open ones;
- **spend**: the business share of spend against ≥ 50 %, the platform share against the ≤ 30 % cap,
  cost per delivered outcome (pos.business.cost_split);
- **agents**: runs ok / failed / blocked, the review queue (size, oldest), loops caught, failed
  deploys, the owner's frustration flags, double answers and unanswered asks (pos.frustration);
- **problems**: the top 3, computed from the numbers above ("Revize: ve frontě 70", "0 odeslaných
  zpráv za 7 dní").

`view(conn)` is what the page and the CEO read: today's stored snapshot (the slow parts: goals, the
owner's requests, spend; built once a day by the job `scorecard_daily` or on the first read of the
day) with the live counters (world, agents) recomputed on every read, and week-over-week deltas
against the snapshot of 7 days ago. Snapshots live in `scorecard_snapshots`, one row per Prague day
(created on first use, no numbered migration).

`update_goals(conn)` (the same daily job) writes the goals' `current` where it is measurable: the
Kniha contacts and interviews (the Kniha checkout's partneri.csv and outreach table, POS_KNIHA_DIR),
Obseum AI prospects (outbound mail on Obseum work), the support response time and the business share.
"""

import csv
import inspect
import io
import json
import logging
import os
import re
import sqlite3
import statistics
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import actors
from .core import TZ, Ctx, now_iso

log = logging.getLogger(__name__)

BUSINESS_TARGET = 0.5
PLATFORM_CAP = 0.3
OWNER_DAYS = 30
REVIEW_QUEUE_LIMIT = 20
RUN_FAIL_LIMIT = 0.1
TREND_DAYS = 8
LOOP_ACTIONS = ("chat_loop", "access_loop_hold")
PUBLISH_ACTIONS = ("discord.post", "web.post", "github.issue", "github.comment", "github.review")

_SCHEMA = """CREATE TABLE IF NOT EXISTS scorecard_snapshots (
    day        TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    created_at TEXT NOT NULL
)"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_SCHEMA)


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat(timespec="seconds")


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _day(now: datetime) -> str:
    return now.astimezone(TZ).date().isoformat()


def _one(conn: sqlite3.Connection, sql: str, *args) -> int:
    row = conn.execute(sql, args).fetchone()
    return int(row[0] or 0) if row else 0


def _has(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone() is not None


def _hours(a: str, b: str) -> float | None:
    try:
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 3600
    except (TypeError, ValueError):
        return None


def fold(text: str) -> str:
    """Lower case without diacritics ("Oslovené" -> "oslovene")."""
    return "".join(c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c)).lower()


# ------------------------------------------------------------------ goals

def _goals(conn: sqlite3.Connection) -> list[dict]:
    from . import goals as goals_mod

    out = []
    for status in ("active", "proposed"):
        for g in goals_mod.list_goals(conn, status):
            out.append({"id": g["id"], "title": g["title"], "status": g["status"], "owner": g["owner_name"],
                        "metric": g.get("metric"), "baseline": g.get("baseline"), "current": g.get("current"),
                        "target_value": g.get("target_value"), "target": g.get("target"), "due": g.get("due"),
                        "progress": g["progress_effective"], "current_at": g.get("current_at"),
                        "lower_is_better": goals_mod.lower_is_better(g), "met": goals_mod.met(g)})
    return out


def _lower_is_better(g: dict) -> bool:
    from . import goals as goals_mod

    return goals_mod.lower_is_better(g)


# ------------------------------------------------------------------ what reached the world

def _call_outbound_stats(conn: sqlite3.Connection, days: int) -> dict | None:
    """Package A's pos.outbound.outbound_stats(days) (or (conn, days)), when it exists."""
    from . import outbound

    fn = getattr(outbound, "outbound_stats", None)
    if fn is None:
        return None
    try:
        params = list(inspect.signature(fn).parameters)
        raw = fn(conn, days) if params and params[0] in ("conn", "db", "connection") else fn(days)
    except Exception:  # noqa: BLE001 - fall back to the audit log
        log.exception("outbound_stats failed")
        return None
    return raw if isinstance(raw, dict) else None


def _outbound_from_audit(conn: sqlite3.Connection, s: str, u: str) -> dict:
    by: dict[str, dict[str, int]] = {}
    for r in conn.execute("SELECT action, detail FROM audit_log WHERE action LIKE 'outbound:%' AND at >= ? AND at < ?",
                          (s, u)):
        try:
            status = json.loads(r["detail"] or "{}").get("status") or "sent"
        except ValueError:
            status = "sent"
        a = r["action"].split(":", 1)[1]
        by.setdefault(a, {}).setdefault(status, 0)
        by[a][status] += 1
    total = lambda st: sum(v.get(st, 0) for v in by.values())  # noqa: E731
    return {"sent": total("sent"), "failed": total("failed"), "not_configured": total("not_configured"),
            "replies": None, "by_action": {a: v.get("sent", 0) for a, v in by.items()}, "source": "audit"}


def outbound(conn: sqlite3.Connection, now: datetime | None = None, days: int = 7) -> dict:
    """Outbound over the last `days`: {sent, replies, failed, not_configured, by_action, source}."""
    now = _now(now)
    s, u = _iso(now - timedelta(days=days)), _iso(now)
    base = _outbound_from_audit(conn, s, u)
    raw = _call_outbound_stats(conn, days) if now >= datetime.now(timezone.utc) - timedelta(minutes=5) else None
    if raw:
        def pick(*keys):
            for k in keys:
                if raw.get(k) is not None:
                    return raw[k]
            return None

        sent = pick("sent", "total_sent", "sends")
        base.update({k: v for k, v in {
            "sent": int(sent) if isinstance(sent, (int, float)) else None,
            "replies": pick("replies", "replied", "answers"),
            "failed": pick("failed", "errors"),
            "not_configured": pick("not_configured"),
            "by_action": pick("by_action", "by_channel") or base["by_action"]}.items() if v is not None})
        base["source"] = "outbound_stats"
    return base


def world(conn: sqlite3.Connection, now: datetime | None = None, days: int = 7) -> dict:
    now = _now(now)
    s, u = _iso(now - timedelta(days=days)), _iso(now)
    out = outbound(conn, now, days)
    by = out.get("by_action") or {}
    published = sum(int(by.get(a) or 0) for a in PUBLISH_ACTIONS)
    deploys = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) AS n FROM deploys WHERE created_at >= ? AND created_at < ? GROUP BY status", (s, u))}
    helped = _one(conn, """SELECT COUNT(*) FROM tasks WHERE status = 'done' AND completed_at >= ? AND completed_at < ?
                            AND archived_at IS NULL AND (source = 'event:gmail' OR source LIKE 'support:%')""", s, u)
    return {"outbound": out, "published": published, "deploys_ok": int(deploys.get("ok", 0)),
            "deploys_failed": sum(int(v) for k, v in deploys.items() if k != "ok"), "customers_helped": helped}


# ------------------------------------------------------------------ the owner's requests

def owner_requests(conn: sqlite3.Connection, now: datetime | None = None, days: int = OWNER_DAYS) -> dict:
    """Top-level work the owner asked for (pos.business.owner_request) created in the last `days`."""
    from . import business, tasks

    now = _now(now)
    since = _iso(now - timedelta(days=days))
    rows = conn.execute("""SELECT * FROM tasks WHERE created_at >= ? AND archived_at IS NULL AND parent_id IS NULL
                           AND COALESCE(topic, '') != 'chat' ORDER BY id""", (since,)).fetchall()
    mine = [r for r in rows if business.owner_request(conn, r)]
    done = [r for r in mine if r["status"] == "done"]
    hours = [h for h in (_hours(r["created_at"], r["completed_at"]) for r in done) if h is not None and h >= 0]
    open_rows = sorted((r for r in mine if r["status"] != "done"), key=lambda r: r["created_at"])
    return {
        "days": days, "total": len(mine), "done": len(done),
        "delivered_pct": round(100 * len(done) / len(mine)) if mine else None,
        "median_hours": round(statistics.median(hours), 1) if hours else None,
        "open": len(open_rows),
        "oldest_open": [{"ref": tasks.display_id(r["id"]), "title": r["title"][:120], "status": r["status"],
                         "assignee": r["assignee_name"], "age_days": round((_hours(r["created_at"], _iso(now)) or 0) / 24, 1)}
                        for r in open_rows[:5]],
    }


# ------------------------------------------------------------------ spend

def spend(conn: sqlite3.Connection, now: datetime | None = None, days: int = 7) -> dict:
    from . import business

    now = _now(now)
    s, u = _iso(now - timedelta(days=days)), _iso(now)
    split = business.cost_split(conn, s, u)
    total = split["total_usd"] or 0.0
    delivered = _one(conn, """SELECT COUNT(*) FROM tasks WHERE status = 'done' AND completed_at >= ? AND completed_at < ?
                               AND archived_at IS NULL AND parent_id IS NULL AND COALESCE(topic, '') != 'chat'
                               AND COALESCE(source, '') NOT LIKE 'review:%'""", s, u)
    return {**split, "target_share": BUSINESS_TARGET, "platform_cap": PLATFORM_CAP,
            "platform_share": round(split["platform_usd"] / total, 3) if total else None,
            "delivered": delivered, "usd_per_delivered": round(total / delivered, 2) if delivered else None}


# ------------------------------------------------------------------ agent health

def agents_health(conn: sqlite3.Connection, now: datetime | None = None, days: int = 7) -> dict:
    from . import frustration

    now = _now(now)
    s, u = _iso(now - timedelta(days=days)), _iso(now)
    runs = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) AS n FROM runs WHERE started_at >= ? AND started_at < ? GROUP BY status", (s, u))}
    finished = sum(int(runs.get(k, 0)) for k in ("ok", "error"))
    from . import review_queue

    rq = review_queue.queue(conn, now)
    loops = _one(conn, f"SELECT COUNT(*) FROM audit_log WHERE action IN ({','.join('?' * len(LOOP_ACTIONS))}) "
                       "AND at >= ? AND at < ?", *LOOP_ACTIONS, s, u)
    incidents = _one(conn, "SELECT COUNT(*) FROM sentinel_incidents WHERE opened_at >= ? AND opened_at < ?", s, u) \
        if _has(conn, "sentinel_incidents") else 0
    return {
        "runs_ok": int(runs.get("ok", 0)), "runs_failed": int(runs.get("error", 0)),
        "runs_blocked": int(runs.get("blocked", 0)), "runs_cancelled": int(runs.get("cancelled", 0)),
        "fail_rate": round(int(runs.get("error", 0)) / finished, 3) if finished else None,
        # One definition of the queue (pos.review_queue), said with whose it is.
        "review_queue": rq["total"], "review_over_sla": rq["over_sla"], "review_oldest_hours": rq["oldest_hours"],
        "review_for_owner": rq["for_owner"], "review_for_leads": rq["for_leads"], "review_text": rq["text"],
        "approvals_pending": rq["approvals"],
        "loops": loops, "incidents": incidents,
        **frustration.stats(conn, now, days),
    }


# ------------------------------------------------------------------ problems

def problems(card: dict, limit: int = 3) -> list[dict]:
    """The top problems, from the numbers: [{text, why, link, score}], the worst first."""
    found: list[dict] = []
    w, a, sp, o = card.get("world") or {}, card.get("agents") or {}, card.get("spend") or {}, card.get("owner") or {}
    sent = (w.get("outbound") or {}).get("sent")
    if sent == 0:
        found.append({"text": "0 odeslaných zpráv za 7 dní", "link": "/company#world", "score": 90,
                      "why": "Nic z obchodu ani podpory nedošlo ven: bez oslovení nejsou zákazníci."})
    q = a.get("review_queue") or 0
    if q > REVIEW_QUEUE_LIMIT:
        old = a.get("review_oldest_hours")
        found.append({"text": f"Revize: {a.get('review_text') or q}" + (f", nejstarší {round(old / 24)} d" if old and old >= 48 else ""),
                      "link": "/tasks?view=review", "score": 50 + min(q, 100) / 2,
                      "why": f"{a.get('review_over_sla', 0)} čeká přes SLA; hotová práce leží a nedojde k zákazníkům."})
    share = sp.get("business_share")
    if share is not None and share < BUSINESS_TARGET:
        found.append({"text": f"Byznys jen {round(share * 100)} % nákladů (cíl ≥ 50 %)", "link": "/company#spend",
                      "score": 50 + (BUSINESS_TARGET - share) * 100,
                      "why": f"Platforma ${sp.get('platform_usd')} z ${sp.get('total_usd')} za 7 dní."})
    ps = sp.get("platform_share")
    if ps is not None and ps > PLATFORM_CAP and not (share is not None and share < BUSINESS_TARGET):
        found.append({"text": f"Platforma {round(ps * 100)} % nákladů (strop 30 %)", "link": "/company#spend",
                      "score": 45 + (ps - PLATFORM_CAP) * 100, "why": "Zlepšování platformy stojí víc, než je strop."})
    fr = a.get("fail_rate")
    if fr is not None and fr > RUN_FAIL_LIMIT:
        found.append({"text": f"Selhané běhy {a.get('runs_failed')} ({round(fr * 100)} %)", "link": "/company#agents",
                      "score": 40 + fr * 100, "why": "Agent běží, platí se, a nic z toho není."})
    if (a.get("frustrations") or 0) > 0:
        found.append({"text": f"Majitel frustrovaný {a['frustrations']}× za týden", "link": "/company#agents",
                      "score": 60 + 10 * a["frustrations"],
                      "why": "Zprávy s „nefunguje“, „zase“, „!!!“ nebo opakovaný požadavek: příčinu opravit týž den."})
    if (a.get("unanswered") or 0) > 0:
        found.append({"text": f"{a['unanswered']} zpráv majitele bez odpovědi do 2 h", "link": "/chat",
                      "score": 55 + 5 * a["unanswered"], "why": "Majitel napsal a nikdo neodpověděl."})
    if (a.get("double_answers") or 0) > 2:
        found.append({"text": f"Dvojí odpovědi majiteli: {a['double_answers']}×", "link": "/chat",
                      "score": 30 + 3 * a["double_answers"], "why": "Na jednu zprávu odpovídá víc agentů."})
    pct = o.get("delivered_pct")
    if pct is not None and pct < 70 and (o.get("total") or 0) >= 5:
        found.append({"text": f"Požadavky majitele: dodáno jen {pct} %", "link": "/company#owner",
                      "score": 50 + (70 - pct) / 2, "why": f"Otevřených {o.get('open')} z {o.get('total')} za 30 dní."})
    if (a.get("loops") or 0) > 0:
        found.append({"text": f"Zachycené smyčky agentů: {a['loops']}", "link": "/company#agents",
                      "score": 20 + 5 * a["loops"], "why": "Agenti si posílali zprávy dokola."})
    if (w.get("deploys_failed") or 0) > (w.get("deploys_ok") or 0) and (w.get("deploys_failed") or 0) >= 3:
        found.append({"text": f"Odmítnuté deploye {w['deploys_failed']} vs. {w.get('deploys_ok', 0)} úspěšných",
                      "link": "/system", "score": 35, "why": "Změny se nedostávají do provozu."})
    today = date.today()
    for g in card.get("goals") or []:
        if not g.get("due") or g.get("progress") is None:
            continue
        try:
            left = (date.fromisoformat(g["due"]) - today).days
        except ValueError:
            continue
        if 0 <= left <= 21 and g["progress"] < 25:
            found.append({"text": f"Cíl „{g['title']}“: {g['progress']} %, zbývá {left} d", "link": "/reports",
                          "score": 45 + (21 - left), "why": f"{_num(g.get('current'))} z {_num(g.get('target_value'))}."})
    found.sort(key=lambda p: -p["score"])
    return [{**p, "score": round(p["score"], 1)} for p in found[:limit]]


def _num(v) -> str:
    if v is None:
        return "—"
    return str(int(v)) if float(v).is_integer() else f"{v:.1f}"


# ------------------------------------------------------------------ build, store, read

def compute(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """Every section, computed now (the slow parts included)."""
    now = _now(now)
    card = {"day": _day(now), "generated_at": _iso(now), "goals": _goals(conn), "owner": owner_requests(conn, now),
            "spend": spend(conn, now), **live(conn, now)}
    card["problems"] = problems(card)
    return card


def live(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """The cheap counters, recomputed on every read."""
    now = _now(now)
    return {"world": world(conn, now), "agents": agents_health(conn, now), "live_at": _iso(now)}


def store(conn: sqlite3.Connection, card: dict) -> None:
    ensure_schema(conn)
    conn.execute("INSERT INTO scorecard_snapshots (day, data, created_at) VALUES (?, ?, ?) ON CONFLICT(day) DO UPDATE "
                 "SET data = excluded.data, created_at = excluded.created_at",
                 (card["day"], json.dumps(card, ensure_ascii=False, default=str), now_iso()))


def stored(conn: sqlite3.Connection, day: str) -> dict | None:
    ensure_schema(conn)
    row = conn.execute("SELECT data FROM scorecard_snapshots WHERE day = ?", (day,)).fetchone()
    return json.loads(row["data"]) if row else None


def snapshot(conn: sqlite3.Connection, now: datetime | None = None, *, force: bool = False) -> dict:
    """Today's stored snapshot, built (and stored) when missing or `force`."""
    now = _now(now)
    if not force:
        have = stored(conn, _day(now))
        if have:
            return have
    card = compute(conn, now)
    store(conn, card)
    conn.commit()
    return card


def _before(conn: sqlite3.Connection, day: str, back: int, slack: int = 3) -> dict | None:
    """The snapshot `back` days before `day` (or up to `slack` days older)."""
    ensure_schema(conn)
    d = date.fromisoformat(day) - timedelta(days=back)
    row = conn.execute("SELECT data FROM scorecard_snapshots WHERE day <= ? AND day >= ? ORDER BY day DESC LIMIT 1",
                       (d.isoformat(), (d - timedelta(days=slack)).isoformat())).fetchone()
    return json.loads(row["data"]) if row else None


# KPI: (key, path in the card, True when up is good)
KPIS = (
    ("outbound_sent", ("world", "outbound", "sent"), True),
    ("replies", ("world", "outbound", "replies"), True),
    ("published", ("world", "published"), True),
    ("deploys_ok", ("world", "deploys_ok"), True),
    ("customers_helped", ("world", "customers_helped"), True),
    ("owner_delivered_pct", ("owner", "delivered_pct"), True),
    ("owner_median_hours", ("owner", "median_hours"), False),
    ("owner_open", ("owner", "open"), False),
    ("business_share", ("spend", "business_share"), True),
    ("platform_share", ("spend", "platform_share"), False),
    ("usd_per_delivered", ("spend", "usd_per_delivered"), False),
    ("usd_per_business_outcome", ("spend", "usd_per_business_outcome"), False),
    ("runs_failed", ("agents", "runs_failed"), False),
    ("fail_rate", ("agents", "fail_rate"), False),
    ("review_queue", ("agents", "review_queue"), False),
    ("review_oldest_hours", ("agents", "review_oldest_hours"), False),
    ("loops", ("agents", "loops"), False),
    ("frustrations", ("agents", "frustrations"), False),
    ("double_answers", ("agents", "double_answers"), False),
    ("unanswered", ("agents", "unanswered"), False),
)


def _get(card: dict | None, path: tuple) -> float | None:
    v = card
    for k in path:
        if not isinstance(v, dict):
            return None
        v = v.get(k)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def deltas(card: dict, prev: dict | None) -> dict:
    """{kpi: {value, prev, delta, good}} against the snapshot of a week ago (good: None when flat or unknown)."""
    out = {}
    for key, path, up in KPIS:
        v, p = _get(card, path), _get(prev, path)
        d = round(v - p, 3) if v is not None and p is not None else None
        out[key] = {"value": v, "prev": p, "delta": d,
                    "good": None if not d else (d > 0) == up}
    return out


def goal_trends(conn: sqlite3.Connection, day: str, days: int = TREND_DAYS) -> dict[int, list]:
    """{goal id: [[day, current], …]} from the stored snapshots (oldest first)."""
    ensure_schema(conn)
    since = (date.fromisoformat(day) - timedelta(days=days - 1)).isoformat()
    out: dict[int, list] = {}
    for r in conn.execute("SELECT day, data FROM scorecard_snapshots WHERE day >= ? AND day <= ? ORDER BY day",
                          (since, day)):
        for g in json.loads(r["data"]).get("goals") or []:
            out.setdefault(g["id"], []).append([r["day"], g.get("current"), g.get("progress")])
    return out


def view(conn: sqlite3.Connection, now: datetime | None = None, *, refresh: bool = False) -> dict:
    """The page's and the CEO's scorecard: today's snapshot + live counters + deltas + trends + problems."""
    now = _now(now)
    card = dict(snapshot(conn, now, force=refresh))
    card.update(live(conn, now))
    prev = _before(conn, card["day"], 7)
    card["kpis"] = deltas(card, prev)
    card["compared_with"] = prev["day"] if prev else None
    trends = goal_trends(conn, card["day"])
    week_ago = {g["id"]: g for g in (prev or {}).get("goals") or []}
    for g in card["goals"]:
        g["trend"] = trends.get(g["id"], [])
        p = week_ago.get(g["id"])
        g["delta"] = (round(g["current"] - p["current"], 2)
                      if p and g.get("current") is not None and p.get("current") is not None else None)
        g["progress_delta"] = (g["progress"] - p["progress"]) if p and p.get("progress") is not None else None
    card["problems"] = problems(card)
    return card


def historic(conn: sqlite3.Connection, day: str) -> dict | None:
    """A past day's stored snapshot with its deltas (nothing recomputed), or None."""
    card = stored(conn, day)
    if card is None:
        return None
    prev = _before(conn, day, 7)
    card["kpis"] = deltas(card, prev)
    card["compared_with"] = prev["day"] if prev else None
    return card


# ------------------------------------------------------------------ in words (the CEO's input)

def _pct(x) -> str:
    return "—" if x is None else f"{round(100 * x)} %"


def _delta_txt(k: dict) -> str:
    if not k or k.get("delta") in (None, 0):
        return ""
    d = k["delta"]
    s = f"{d:+.0f}" if abs(d) >= 1 or float(d).is_integer() else f"{d * 100:+.0f} b."
    return f" ({s} t/t)"


def render(card: dict) -> str:
    """The scorecard as Czech Markdown: numbers only (the CEO writes the narrative around them)."""
    k = card.get("kpis") or {}
    w, o, sp, a = card["world"], card["owner"], card["spend"], card["agents"]
    ob = w["outbound"]
    lines = ["### Cíle"]
    for g in card["goals"] or []:
        lines.append(f"- {g['title']} ({g.get('owner') or 'bez vlastníka'}{', návrh' if g['status'] == 'proposed' else ''}): "
                     f"{_num(g.get('baseline'))} → **{_num(g.get('current'))}** → {_num(g.get('target_value'))}"
                     f" · {g['progress']} %" + (f" · termín {g['due']}" if g.get("due") else "")
                     + (f" · t/t {g['delta']:+g}" if g.get("delta") else ""))
    if not card["goals"]:
        lines.append("- žádné cíle")
    lines += ["", "### Co se dostalo ven (7 dní)",
              f"- odeslané zprávy **{ob.get('sent', 0)}**{_delta_txt(k.get('outbound_sent'))}, odpovědi "
              f"{'—' if ob.get('replies') is None else ob['replies']}, selhalo {ob.get('failed') or 0}"
              + (f", nenastavený kanál {ob['not_configured']}" if ob.get("not_configured") else ""),
              f"- zveřejněno {w['published']}, nasazeno {w['deploys_ok']} (odmítnuto {w['deploys_failed']}), "
              f"zákazníkům pomoženo {w['customers_helped']}",
              "", f"### Požadavky majitele ({o['days']} dní)",
              f"- dodáno **{'—' if o['delivered_pct'] is None else str(o['delivered_pct']) + ' %'}** ({o['done']} z {o['total']}),"
              f" medián do hotova {'—' if o['median_hours'] is None else str(o['median_hours']) + ' h'}, otevřených {o['open']}"]
    for t in o.get("oldest_open") or []:
        lines.append(f"  - {t['ref']} {t['title']} ({t['assignee'] or '—'}, {t['age_days']} d)")
    lines += ["", "### Náklady (7 dní)",
              f"- byznys **{_pct(sp.get('business_share'))}** (cíl ≥ 50 %){_delta_txt(k.get('business_share'))}, "
              f"platforma {_pct(sp.get('platform_share'))} (strop 30 %), celkem ${sp.get('total_usd')}",
              f"- na dodaný výsledek ${sp.get('usd_per_delivered') or '—'} ({sp.get('delivered')} hotových), "
              f"na byznysový výsledek ${sp.get('usd_per_business_outcome') or '—'}",
              "", "### Zdraví agentů (7 dní)",
              f"- běhy ok {a['runs_ok']}, selhalo {a['runs_failed']} ({_pct(a.get('fail_rate'))}), blokováno {a['runs_blocked']}",
              f"- revize: **{a.get('review_text') or a['review_queue']}** (přes SLA {a['review_over_sla']}, nejstarší "
              f"{'—' if a.get('review_oldest_hours') is None else str(a['review_oldest_hours']) + ' h'})",
              f"- smyčky {a['loops']}, incidenty {a['incidents']}, frustrace majitele {a['frustrations']}, dvojí odpovědi "
              f"{a['double_answers']}, bez odpovědi {a['unanswered']}",
              "", "### Top problémy"]
    lines += [f"{i}. {p['text']}: {p['why']}" for i, p in enumerate(card.get("problems") or [], 1)] or ["- žádné"]
    return "\n".join(lines)


def render_platform(card: dict) -> str:
    """The platform section (the weekly improvement meeting's input)."""
    a, sp = card["agents"], card["spend"]
    k = card.get("kpis") or {}
    return "\n".join([
        f"- běhy: ok {a['runs_ok']}, selhalo {a['runs_failed']} ({_pct(a.get('fail_rate'))}){_delta_txt(k.get('runs_failed'))}, "
        f"blokováno {a['runs_blocked']}",
        f"- revize: {a.get('review_text') or a['review_queue']}{_delta_txt(k.get('review_queue'))}, přes SLA {a['review_over_sla']}, "
        f"nejstarší {a.get('review_oldest_hours') or '—'} h",
        f"- smyčky {a['loops']}, incidenty {a['incidents']}, odmítnuté deploye {card['world']['deploys_failed']}",
        f"- majitel: frustrace {a['frustrations']}, dvojí odpovědi {a['double_answers']}, bez odpovědi {a['unanswered']}",
        f"- náklady: platforma {_pct(sp.get('platform_share'))} (strop 30 %), ${sp.get('platform_usd')} z ${sp.get('total_usd')}",
    ])


CEO_SCHEDULES = ("CEO: pondělní plán", "CEO: podklady pro board")


def with_scorecard(conn: sqlite3.Connection, assignee_id: int, name: str, template: dict) -> dict:
    """The CEO's Monday plan and Friday board input carry the scorecard (pos.schedules.fire)."""
    try:
        if name not in CEO_SCHEDULES or actors.get(conn, assignee_id)["role"] != "ceo":
            return template
        card = view(conn)
    except Exception:  # noqa: BLE001 - the routine still fires without the numbers
        log.exception("scorecard for %s failed", name)
        return template
    cap = card["spend"].get("platform_share")
    warn = (f"\n\n**Strop platformy překročen: {_pct(cap)} > 30 %.** Tento týden zlepšovací backlog nejvýš 2 položky "
            "a jen ty, které snižují náklady; řekni to CTO." if cap is not None and cap > PLATFORM_CAP else "")
    notes = (f"{(template.get('notes') or '').rstrip()}\n\n## Scorecard firmy (čísla z kódu, {card['day']})\n"
             "Čísla ber odsud a neopisuj je odhadem; ty píšeš komentář, priority a rozhodnutí. Stránka: /company.\n\n"
             + render(card) + warn)
    return {**template, "notes": notes}


# ------------------------------------------------------------------ goals' current values, measured

def kniha_dir() -> Path | None:
    for p in (os.environ.get("POS_KNIHA_DIR"), "/agent-work/kniha", "/work/kniha"):
        if p and Path(p).is_dir():
            return Path(p)
    return None


_YES = {"ano", "yes", "y", "1", "true", "x", "✓", "✔"}
_EMPTY = {"", "-", "–", "—", "ne", "no", "0", "n/a"}


def kniha_counts(base: Path | None = None) -> dict | None:
    """Contacts, replies and interviews of the Kniha pilot from its checkout: provoz/partneri.csv
    (stav, datum_osloveni, odpoved) and marketing/warm-outreach-pilot-tabulka.md (Osloveno, Odpověď,
    Rozhovor). None when neither is there."""
    base = base or kniha_dir()
    if base is None:
        return None
    contacts = replies = interviews = 0
    seen = False
    for csv_path in (base / "provoz" / "partneri.csv", base / "marketing" / "partneri.csv"):
        if not csv_path.is_file():
            continue
        seen = True
        for row in csv.DictReader(io.StringIO(csv_path.read_text(encoding="utf-8", errors="replace"))):
            r = {fold(k or "").strip(): (v or "").strip() for k, v in row.items()}
            stav = fold(r.get("stav", ""))
            if r.get("datum_osloveni", "") not in _EMPTY or re.match(r"(osloven|odeslan|kontaktovan|odpoved|rozhovor|schuzk)", stav):
                contacts += 1
            if fold(r.get("odpoved", "")) not in _EMPTY:
                replies += 1
            if re.search(r"rozhovor|schuzk", stav):
                interviews += 1
        break
    table = base / "marketing" / "warm-outreach-pilot-tabulka.md"
    if table.is_file():
        seen = True
        header: list[str] | None = None
        for line in table.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip().startswith("|"):
                header = None if not line.strip() else header
                continue
            cells = [fold(c.strip()) for c in line.strip().strip("|").split("|")]
            if header is None:
                header = cells
                continue
            if all(set(c) <= set("-: ") for c in cells):
                continue
            r = dict(zip(header, cells, strict=False))
            if r.get("osloveno") in _YES:
                contacts += 1
            if r.get("odpoved", "") not in _EMPTY:
                replies += 1
            if r.get("rozhovor") in _YES:
                interviews += 1
    return {"contacts": contacts, "replies": replies, "interviews": interviews} if seen else None


def _outbound_for(conn: sqlite3.Connection, since: str, words: tuple[str, ...]) -> int:
    """Sent e-mails whose task is in a project, topic or title matching one of `words`."""
    n = 0
    for r in conn.execute("""SELECT a.entity_id, a.detail FROM audit_log a WHERE a.action = 'outbound:email.send'
                             AND a.at >= ? AND a.entity = 'task'""", (since,)):
        try:
            if json.loads(r["detail"] or "{}").get("status") != "sent":
                continue
        except ValueError:
            continue
        t = conn.execute("SELECT title, topic, project_id FROM tasks WHERE id = ?", (r["entity_id"],)).fetchone()
        if t is None:
            continue
        hay = fold(f"{t['title']} {t['topic'] or ''}")
        if t["project_id"]:
            p = conn.execute("SELECT name, slug FROM projects WHERE id = ?", (t["project_id"],)).fetchone()
            hay += " " + fold(f"{p['name']} {p['slug']}") if p else ""
        if any(w in hay for w in words):
            n += 1
    return n


def _support_hours(conn: sqlite3.Connection) -> float | None:
    from . import owner_seed

    return owner_seed._support_hours(conn)


def _business_share_pct(conn: sqlite3.Connection) -> float | None:
    s = spend(conn).get("business_share")
    return round(s * 100, 1) if s is not None else None


def measures(conn: sqlite3.Connection, goal: dict) -> float | None:
    """The measured `current` of a goal, or None when it is not measurable here."""
    t = fold(goal["title"])
    since = goal.get("created_at") or "1970"
    if t.startswith("kniha pilot: oslovene kontakty"):
        k = kniha_counts()
        sent = _outbound_for(conn, since, ("kniha",))
        return float(max(k["contacts"] if k else 0, sent)) if (k or sent) else None
    if t.startswith("kniha pilot: rozhovory"):
        k = kniha_counts()
        notes = _one(conn, "SELECT COUNT(*) FROM notes WHERE archived_at IS NULL AND lower(COALESCE(topic, '')) = 'kniha' "
                           "AND lower(title) LIKE 'rozhovor%' AND created_at >= ?", since) if _has(conn, "notes") else 0
        return float(max(k["interviews"] if k else 0, notes)) if (k or notes) else None
    if t.startswith("obseum ai: osloveni prospekti"):
        n = _outbound_for(conn, since, ("obseum",))
        return float(n) if n or goal.get("current") is not None else None
    if t.startswith("zakaznicka podpora: rychlost odpovedi"):
        return _support_hours(conn)
    if t.startswith("platforma: podil byznysu"):
        return _business_share_pct(conn)
    return None


def update_goals(conn: sqlite3.Connection) -> list[dict]:
    """Write every measurable goal's `current` (active and proposed goals); returns what changed."""
    from . import goals as goals_mod

    goals_mod.ensure_schema(conn)
    ctx = Ctx(actors.system_id(conn), via="scorecard")
    changed = []
    for row in conn.execute("SELECT * FROM goals WHERE archived_at IS NULL AND status IN ('active', 'proposed')").fetchall():
        g = dict(row)
        try:
            value = measures(conn, g)
        except Exception:  # noqa: BLE001 - one source failing leaves the goal as it is
            log.exception("measuring goal %s failed", g["id"])
            continue
        if value is None or (g.get("current") is not None and abs(float(g["current"]) - value) < 1e-9):
            continue
        goals_mod.update(conn, ctx, g["id"], {"current": value})
        changed.append({"id": g["id"], "title": g["title"], "from": g.get("current"), "to": value})
    return changed


def daily(conn: sqlite3.Connection) -> dict:
    """The job: measured goals updated, then today's snapshot (re)built."""
    changed = update_goals(conn)
    conn.commit()
    card = snapshot(conn, force=True)
    return {"goals_updated": len(changed), "day": card["day"],
            "problems": [p["text"] for p in card["problems"]]}
