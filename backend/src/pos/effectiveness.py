"""Agent effectiveness: the numbers the CEO steers by, and its weekly business-focus digest.

Measured on 2026-10-01: ~85 % of the spend was the platform working on itself, agents wrote 2 of
257 commits, 42 of 225 tasks waited in review. The CEO's weekly target is **≥ 50 % of spend on
business work** (pos.business.cost_split: business vs platform by the task's label). Every Monday
the CEO gets one task with:

- last week's cost split (business %, against the target), and the agents spending most on
  platform work;
- the agents with no input for 7 days (pos.business.idle_agents);
- the effectiveness numbers: results waiting for review (and over the 12 h SLA), auto-accepted
  results, knowledge use (pos.knowledge_first.share), memory coverage, lessons recorded
  (pos.learning), hand-ins with a verification line (pos.verification), tainted-run holds
  (pos.taint).

`snapshot(conn)` is the same numbers as a dict (the API, tests).
"""

import sqlite3
from datetime import datetime, timedelta, timezone

from . import actors
from .core import Ctx

BUSINESS_TARGET = 0.5


def _iso(d: datetime) -> str:
    return d.isoformat(timespec="seconds")


def platform_spenders(conn: sqlite3.Connection, s: str, u: str, limit: int = 5) -> list[dict]:
    from . import business

    roles = business._roles(conn)
    by: dict[str, float] = {}
    for r in conn.execute("""SELECT task_id, actor_id, SUM(cost_usd) AS c FROM engine_usage
                             WHERE at >= ? AND at < ? GROUP BY task_id, actor_id""", (s, u)):
        t = conn.execute("SELECT * FROM tasks WHERE id = ?", (r["task_id"],)).fetchone() if r["task_id"] else None
        if t is not None and business.classify(conn, t, roles) != "platform":
            continue
        name = actors.get(conn, r["actor_id"])["name"] if r["actor_id"] else "?"
        by[name] = by.get(name, 0.0) + (r["c"] or 0.0)
    return [{"name": n, "usd": round(c, 2)} for n, c in sorted(by.items(), key=lambda x: -x[1])[:limit] if c > 0]


def snapshot(conn: sqlite3.Connection, now: datetime | None = None, days: int = 7) -> dict:
    from . import business, knowledge_first, taint

    now = now or datetime.now(timezone.utc)
    s, u = _iso(now - timedelta(days=days)), _iso(now)
    from . import review_queue

    rq = review_queue.queue(conn, now)  # one definition of the queue
    waiting, over = rq["total"], rq["over_sla"]

    def count(action: str) -> int:
        return conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = ? AND at >= ? AND at < ?",
                            (action, s, u)).fetchone()[0]

    agents = conn.execute("SELECT COUNT(*) FROM actors WHERE kind != 'human' AND archived_at IS NULL").fetchone()[0]
    with_memory = conn.execute("""SELECT COUNT(DISTINCT m.actor_id) FROM memories m JOIN actors a ON a.id = m.actor_id
                                  WHERE m.archived_at IS NULL AND a.archived_at IS NULL AND a.kind != 'human'
                                  AND length(trim(m.body)) > 0""").fetchone()[0]
    verified, unverified = count("handin_verified"), count("handin_unverified")
    taint.ensure_schema(conn)
    holds = conn.execute("SELECT status, COUNT(*) AS n FROM taint_holds WHERE created_at >= ? GROUP BY status",
                         (s,)).fetchall()
    split = business.cost_split(conn, s, u)
    return {
        "period": {"from": s, "to": u},
        "cost": {**split, "target_share": BUSINESS_TARGET,
                 "on_target": split["business_share"] is not None and split["business_share"] >= BUSINESS_TARGET},
        "platform_spenders": platform_spenders(conn, s, u),
        "idle_agents": [a["name"] for a in business.idle_agents(conn, now)],
        "reviews": {"waiting": waiting, "over_sla": over, "auto_accepted": count("review_auto_accept"),
                    "escalated": count("review_escalate")},
        "knowledge": knowledge_first.share(conn, days, now),
        "memory": {"agents": agents, "with_memory": with_memory},
        "lessons": count("lesson"),
        "verification": {"verified": verified, "unverified": unverified,
                         "share": round(verified / (verified + unverified), 3) if verified + unverified else None},
        "taint_holds": {r["status"]: r["n"] for r in holds},
    }


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{round(100 * x)} %"


def render(snap: dict) -> str:
    c = snap["cost"]
    mark = "✅" if c["on_target"] else "⚠️"
    lines = [
        "### Náklady: obchod vs. platforma (cíl ≥ 50 % na obchodní práci)",
        f"- {mark} **obchod {_pct(c['business_share'])}**: ${c['business_usd']} z ${c['total_usd']} "
        f"(platforma ${c['platform_usd']}); obchodních výsledků {c['business_outcomes']}"
        + (f", ${c['usd_per_business_outcome']} na výsledek" if c["usd_per_business_outcome"] else ""),
    ]
    if snap["platform_spenders"]:
        lines.append("- nejvíc na platformě: " + ", ".join(f"{p['name']} ${p['usd']}" for p in snap["platform_spenders"]))
    lines += ["", "### Agenti bez vstupu 7 dní",
              ("- " + ", ".join(snap["idle_agents"])) if snap["idle_agents"] else "- nikdo"]
    r, k, m, v = snap["reviews"], snap["knowledge"], snap["memory"], snap["verification"]
    lines += ["", "### Efektivita agentů",
              f"- revize: čeká {r['waiting']}, z toho přes 12 h {r['over_sla']}; automaticky přijato {r['auto_accepted']}, "
              f"eskalováno {r['escalated']}",
              f"- znalosti: běhy s předanými pasážemi {k['runs_with_passages']} z {k['runs']} "
              f"({_pct(k['preload_share'])}); nástroj knowledge {_pct(k['knowledge_call_share'])} MCP volání",
              f"- paměť: {m['with_memory']} z {m['agents']} agentů; nových poučení {snap['lessons']}",
              f"- ověření před odevzdáním: {_pct(v['share'])} ({v['verified']} s řádkem Ověřeno, {v['unverified']} bez)",
              "- zasažené běhy (bezpečnost): " + (", ".join(f"{s} {n}" for s, n in snap["taint_holds"].items()) or "žádné")]
    return "\n".join(lines)


def ceo_digest(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """Monday: the CEO's business-focus digest as one task (once a week)."""
    from . import business, tasks

    now = now or datetime.now(timezone.utc)
    ceo = business.ceo_id(conn)
    if not ceo:
        return {"skipped": "no CEO"}
    week = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"
    title = f"CEO: obchodní fokus týdne · {week}"
    if conn.execute("SELECT 1 FROM tasks WHERE title = ? AND archived_at IS NULL", (title,)).fetchone():
        return {"skipped": "already this week"}
    snap = snapshot(conn, now)
    t = tasks.create(conn, Ctx(actors.owner_id(conn), via="scheduler"), {
        "title": title, "assignee": {"type": "agent", "id": ceo}, "status": "next", "priority": 2,
        "topic": "board", "source": "scheduler", "reviewer": ceo,
        "notes": ("### Proč\nTvůj týdenní cíl: **≥ 50 % nákladů na obchodní práci** (zákazníci, peníze, produkty, "
                  "majitelův domov a znalosti), ne na platformu samotnou. Tady jsou čísla za minulý týden.\n\n"
                  + render(snap) +
                  "\n\n### Co udělat\nJe-li obchod pod 50 %: přesuň priority (úkoly vedoucím pro obchodní práci, "
                  "omez platformní rutiny nejdražších agentů), a zapiš to do pondělního plánu. Nečinné agenty: "
                  "dát práci, pozastavit, nebo navrhnout archivaci. Majitele to nepotřebuje."),
        "definition_of_done": "Pondělní plán obsahuje kroky k cíli ≥ 50 % na obchod; nečinní agenti jsou vyřešení."})
    conn.commit()
    return {"task": t["ref"], "business_share": snap["cost"]["business_share"]}
