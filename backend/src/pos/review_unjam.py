"""One-off: unjam the review queue (prod 2026-10-05: 70 results in review, 57 of them 3-7 days old,
the QA Reviewer holding 39 and the CEO 17).

For every result waiting for review longer than --days (default 3):

1. closes the obsolete ones:
   - duplicate fix tasks of one incident (T-451..T-457: one container, one commit, six tasks), of
     one assignee and created within INCIDENT_DEDUP_HOURS of the first: the oldest stays, the others
     are closed as its duplicates (pos.business.same_incident);
   - a result from a schedule that a later run of the same schedule superseded (a newer task of
     that schedule is done or in review): the old report has nothing left to review;
   - a deployer's refusal task when a deploy has gone through since (main moved on);
2. reroutes the rest to the right reviewer:
   - Kniha work (project, team or topic kniha) to the Kniha Lead (QA and the CEO cannot judge it,
     T-731), never the Lead's own work;
   - what the review policy routes elsewhere (code to the QA Reviewer, the CEO's non-strategic
     reviews to the team lead: pos.review_policy.decide);
   - a reviewer that cannot review (archived, without tasks:review) to the assignee's team lead;
3. brings the review work items in step (pos.review_work.sync, within the daily review budget):
   items of closed results close, moved results' items move to the new reviewer.

Nothing is auto-accepted. A dry run by default (prints what would happen, as JSON):

    python -m pos.review_unjam              # dry run
    python -m pos.review_unjam --apply      # change the data
    python -m pos.review_unjam --days 1 --db /data/personalos.db
"""

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import actors, audit
from .core import now_iso


def _waiting(conn: sqlite3.Connection, days: float, now: datetime) -> list[sqlite3.Row]:
    cutoff = (now - timedelta(days=days)).isoformat(timespec="seconds")
    return conn.execute("""SELECT * FROM tasks WHERE status = 'review' AND archived_at IS NULL
                           AND COALESCE(source, '') NOT LIKE 'review:%' AND updated_at < ?
                           ORDER BY created_at, id""", (cutoff,)).fetchall()


def _superseded_by(conn: sqlite3.Connection, row) -> int | None:
    """A newer task of the same schedule that is done or in review."""
    src = row["source"] or ""
    if not src.startswith("schedule:"):
        return None
    r = conn.execute("""SELECT id FROM tasks WHERE source = ? AND id > ? AND archived_at IS NULL
                        AND status IN ('done', 'review') ORDER BY id DESC LIMIT 1""", (src, row["id"])).fetchone()
    return r["id"] if r else None


def _deployed_since(conn: sqlite3.Connection, row) -> int | None:
    """A successful deploy after a deployer's refusal task was created."""
    if (row["source"] or "") != "deployer":
        return None
    try:
        r = conn.execute("SELECT id FROM deploys WHERE status = 'ok' AND created_at > ? ORDER BY id LIMIT 1",
                         (row["created_at"],)).fetchone()
    except sqlite3.OperationalError:
        return None
    return r["id"] if r else None


def _close(conn: sqlite3.Connection, row, why: str) -> None:
    from . import business, comments, review_work, tasks, versioning

    ctx = business.system_ctx(conn)
    versioning.update(conn, ctx, tasks.ENTITY, row["id"], {
        "status": "done", "completed_at": now_iso(),
        "progress_note": f"{(row['progress_note'] or '').strip()}\n\nUzavřeno bez revize: {why}".strip()[:4000]},
        action="review_unjam_close")
    comments.log(conn, ctx, row["id"], f"Uzavřeno bez revize (úklid fronty revizí): {why}.", "system")
    audit.log(conn, ctx, "review_unjam_close", "task", row["id"], why=why)
    review_work.close_for(conn, row["id"], "uzavřeno jako zastaralé")


def _reroute(conn: sqlite3.Connection, row, target: int, why: str) -> None:
    from . import business, comments, tasks, versioning

    ctx = business.system_ctx(conn)
    versioning.update(conn, ctx, tasks.ENTITY, row["id"], {"reviewer_id": target}, action="reviewer")
    comments.log(conn, ctx, row["id"], f"Revize přesunuta na {actors.get(conn, target)['name']}: {why}.", "system")
    audit.log(conn, ctx, "review_unjam_reroute", "task", row["id"], to=target, why=why)


def right_reviewer(conn: sqlite3.Connection, row) -> tuple[int | None, str]:
    """(the reviewer this result should have, why) or (None, "") when its reviewer is right."""
    from . import business, review_policy, tasks

    current = tasks.reviewer_of(conn, row)
    owner = actors.owner_id(conn)
    if current == owner:
        return None, ""  # the owner's own reviews stay; the SLA hands them to his stand-in
    if review_policy.is_kniha(conn, row):
        lead = review_policy.kniha_lead_id(conn)
        if lead and lead not in (current, row["assignee_id"]):
            return lead, "výsledky Knihy reviduje Kniha Lead (T-731)"
        if lead and lead == current:
            return None, ""
    d = review_policy.decide(conn, row, row["progress_note"])
    if d.action == "route" and d.reviewer_id and d.reviewer_id != current:
        return d.reviewer_id, d.reason
    r = actors.get(conn, current) if current else None
    if r is None or r["archived_at"] or (not r["is_owner"] and r["kind"] != "human"
                                          and not business._can_review(conn, current)):
        lead = review_policy.team_lead(conn, row["assignee_id"])
        if lead and lead != current:
            return lead, "dosavadní revizor revidovat nemůže"
    return None, ""


def unjam(conn: sqlite3.Connection, apply: bool = False, days: float = 3, now: datetime | None = None) -> dict:
    from . import business, review_work, tasks

    now = now or datetime.now(timezone.utc)
    rows = _waiting(conn, days, now)
    closed, rerouted = [], []
    gone: set[int] = set()
    incidents: list[tuple[sqlite3.Row, set[str]]] = []  # the first task of each incident
    window = timedelta(hours=business.INCIDENT_DEDUP_HOURS)
    for row in rows:
        ref = tasks.display_id(row["id"])
        keys = business._incident_keys(f"{row['title'] or ''}\n{row['notes'] or ''}")
        first = next((f["id"] for f, fk in incidents
                      if f["assignee_id"] == row["assignee_id"] and business.same_incident(fk, keys)
                      and datetime.fromisoformat(row["created_at"]) - datetime.fromisoformat(f["created_at"])
                      <= window), None) if keys else None
        if keys and first is None:
            incidents.append((row, keys))
        why = ""
        if first is not None:
            why = f"duplicitní oprava téhož incidentu jako {tasks.display_id(first)}"
        elif (newer := _superseded_by(conn, row)) is not None:
            why = f"novější běh téže rutiny ({tasks.display_id(newer)}) ho nahradil"
        elif (deploy := _deployed_since(conn, row)) is not None:
            why = f"nasazení od té doby prošlo (deploy #{deploy})"
        if why:
            closed.append(f"{ref} ({why}): {row['title'][:60]}")
            gone.add(row["id"])
            if apply:
                _close(conn, row, why)
    for row in rows:
        if row["id"] in gone:
            continue
        target, why = right_reviewer(conn, row)
        if target:
            rerouted.append(f"{tasks.display_id(row['id'])}→{actors.get(conn, target)['name']} ({why}): "
                            f"{row['title'][:60]}")
            if apply:
                _reroute(conn, row, target, why)
    items = review_work.sync(conn, now=now, apply=apply)
    if apply:
        conn.commit()
    per: dict[str, int] = {}
    for row in conn.execute("""SELECT reviewer_id FROM tasks WHERE status = 'review' AND archived_at IS NULL
                               AND COALESCE(source, '') NOT LIKE 'review:%'""").fetchall():
        rid = row["reviewer_id"]
        name = actors.get(conn, rid)["name"] if rid else "?"
        per[name] = per.get(name, 0) + 1
    return {"looked_at": len(rows), "older_than_days": days, "closed": closed, "rerouted": rerouted,
            "review_items": items, "waiting_per_reviewer": per, "applied": apply}


def main(argv: list[str] | None = None) -> None:
    from .config import get_settings
    from .db import connect

    p = argparse.ArgumentParser(description="One-off: close obsolete reviews and reroute stale ones.")
    p.add_argument("--apply", action="store_true", help="change the data (default: a dry run)")
    p.add_argument("--days", type=float, default=3, help="only results waiting longer than this (default 3)")
    p.add_argument("--db", default=None)
    a = p.parse_args(argv)
    conn = connect(Path(a.db) if a.db else get_settings().db_path)
    try:
        print(json.dumps(unjam(conn, apply=a.apply, days=a.days), ensure_ascii=False, indent=1))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
