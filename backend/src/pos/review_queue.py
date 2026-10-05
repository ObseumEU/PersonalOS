"""The review queue: one definition, one count, said with whose queue it is.

Results handed in for review (tasks in status `review`, not archived) wait either for the owner
(reviewer = the owner, or no reviewer: results from before reviewers existed) or for a lead (QA
Reviewer, CEO, CTO…). Before this module every page counted on its own (66 on Úkoly with the
viewer's visibility, 70 on Firma, 72 in the CEO's daily report, 99 in the weekly report, "Nic ke
kontrole" in the weekly review because it counted only the owner's part). Every page that shows the
queue reads `queue` and says it the same way: "70 čeká na kontrolu vedoucích, 0 na tebe".

Pending approvals (`approvals`, only the owner decides them) are counted next to it, never mixed in.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

SLA_HOURS_DEFAULT = 24


def _sla_hours() -> int:
    try:
        from . import review_policy

        return int(review_policy.SLA_HOURS)
    except Exception:  # noqa: BLE001 - the queue is counted either way
        return SLA_HOURS_DEFAULT


def _hours(since: str | None, now: datetime) -> float | None:
    if not since:
        return None
    try:
        t = datetime.fromisoformat(since)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (now - t).total_seconds() / 3600


def _plural(n: int, one: str, few: str, many: str) -> str:
    return one if n == 1 else few if 2 <= n <= 4 else many


def text(total: int, for_owner: int) -> str:
    """'70 čeká na kontrolu vedoucích, 0 na tebe' (the owner's part named even when it is 0)."""
    leads = total - for_owner
    if total == 0:
        return "Nic nečeká na kontrolu (ani u vedoucích, ani u tebe)."
    verb = _plural(leads, "čeká", "čekají", "čeká")
    return f"{leads} {verb} na kontrolu vedoucích, {for_owner} na tebe"


def queue(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """{total, for_owner, for_leads, over_sla, oldest_hours, by_reviewer: [{name, n}], approvals, text}."""
    from . import actors

    now = now or datetime.now(timezone.utc)
    owner = actors.owner_id(conn)
    rows = conn.execute("""SELECT t.reviewer_id, t.updated_at, r.name FROM tasks t
                           LEFT JOIN actors r ON r.id = t.reviewer_id
                           WHERE t.status = 'review' AND t.archived_at IS NULL""").fetchall()
    sla = now - timedelta(hours=_sla_hours())
    for_owner = sum(1 for r in rows if r["reviewer_id"] in (None, owner))
    by: dict[str, int] = {}
    for r in rows:
        name = "ty" if r["reviewer_id"] in (None, owner) else (r["name"] or "?")
        by[name] = by.get(name, 0) + 1
    over = 0
    oldest = None
    for r in rows:
        h = _hours(r["updated_at"], now)
        if h is None:
            continue
        oldest = h if oldest is None else max(oldest, h)
        if now - timedelta(hours=h) < sla:
            over += 1
    approvals = conn.execute("SELECT COUNT(*) FROM approvals WHERE status = 'pending'").fetchone()[0]
    total = len(rows)
    return {
        "total": total, "for_owner": for_owner, "for_leads": total - for_owner, "over_sla": over,
        "oldest_hours": round(oldest) if oldest is not None else None,
        "by_reviewer": [{"name": k, "n": v} for k, v in sorted(by.items(), key=lambda x: -x[1])],
        "approvals": approvals, "text": text(total, for_owner),
    }
