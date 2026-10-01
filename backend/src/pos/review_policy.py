"""Review burn-down: who reviews a handed-in result, by its risk class.

Measured on 2026-10-01: 42 of 225 tasks sat in `review`, some over 12 h, the oldest 38 h, nearly all
waiting for the CEO. Most of them did not need a person at all. Every hand-in now gets a risk class:

- **low**: auto-accepted at once, with a note in the task's activity saying why:
  - a routine check (a task from a schedule named as a check, review or scan) handed in all green,
    with a real summary;
  - a document or note (a digest, summary, standup, report, minutes, research) that asks nobody
    for a decision;
  - a small task (estimate ≤ 60 min, or sized S by the triage) by an experienced agent (≥ 10 results
    accepted in 60 days, ≤ 15 % returned) whose result says the tests passed (a verification line).
- **code**: a code change goes to the **QA Reviewer** (the default reviewer for code), unless it did
  the work itself.
- **high**: customer replies, money, contracts, the owner's own requests, and results that were
  returned before: never auto-accepted; the usual reviewer (pos.tasks.reviewer_of).
- **normal**: the usual reviewer.

Whatever would wait for the owner goes to the CEO first (pos.business.review_triage_target): it
accepts what it can and hands him only what truly needs him. A result nobody reviewed in
SLA_HOURS (12 h) moves to the reviewer's lead (pos.business.review_sla).

The small-task rule needs the result's verification line (pos.verification). An explicit
`request_review` (the agent chose its reviewer) and a reviewer the owner set are respected.

The backlog: `python -m pos.review_policy` (a dry run: what would happen to every result waiting
for review) and `python -m pos.review_policy --apply`.
"""

import argparse
import json
import re
from pathlib import Path
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import actors, audit, roles
from .core import Ctx, now_iso

SLA_HOURS = 12
SMALL_MINUTES = 60
EXPERIENCED_ACCEPTED = 10
EXPERIENCED_MAX_RETURN_RATE = 0.15
EXPERIENCE_DAYS = 60

DOC_TOPICS = {"digest", "brief", "standup", "report", "notes", "research", "weekly", "zapis", "zápis"}
DOC_TITLE_RE = re.compile(r"^\s*(?:digest|souhrn|daily standup|standup|report|přehled|prehled|zápis|zapis|"
                          r"rešerše|reserse|research|summary|notes?|poznámk[ay])\b", re.IGNORECASE)
# The result asks someone to decide or approve: a person looks at it.
DECISION_RE = re.compile(r"rozhodn|schval|schvál|approve|approval|decide|decision|potřebuj[ie]\s+(?:od|tvoje|vaše)|"
                         r"needs? (?:your|the owner|david)|čeká na (?:majitel|davida|ownera)|\?\s*$", re.IGNORECASE)
HIGH_SOURCES = ("support:",)
HIGH_TOPICS = {"zakaznici", "customers", "finance", "legal", "bezpecnost", "security"}
HIGH_RE = re.compile(r"\b(?:platb[auy]|zaplat|faktur|smlouv|contract|payment|invoice|cenov[áa] nabídk|"
                     r"price quote|refund)\w*", re.IGNORECASE)
CODE_TOPICS = {"code", "dev", "kniha-dev"}  # plans with topic engineering are not code
CODE_ROLES = {"developer"}
# Strong evidence of a code change in the result (not words like "branch" or "repo" in a plan).
CODE_RE = re.compile(r"\bcommit(?:ted|nut[oý]|)\s+`?[0-9a-f]{7,40}\b|\b[0-9a-f]{7,40}\.\.[0-9a-f]{7,40}\b|"
                     r"\bpull request\b|\bPR\s*#\d+|\bpytest\b|\bnpm (?:test|run)\b|\bgit push\b|\bagent/dev\b",
                     re.IGNORECASE)


@dataclass(frozen=True)
class Decision:
    risk: str            # low | code | normal | high
    action: str          # accept | route | keep
    reviewer_id: int | None = None
    reason: str = ""


def _text(row, note: str | None) -> str:
    return f"{row['title'] or ''}\n{note or ''}"


def _role(conn: sqlite3.Connection, actor_id: int | None) -> str | None:
    if not actor_id:
        return None
    r = conn.execute("SELECT role FROM actors WHERE id = ?", (actor_id,)).fetchone()
    return r["role"] if r else None


CHECK_NAME_RE = re.compile(r"kontrol|check|health|zdrav|revize|review|sken|scan|audit|inventur", re.IGNORECASE)
CHANGE_RE = re.compile(r"zlepš|zleps|improv|oprav|fix|implement|návrh|navrh", re.IGNORECASE)  # work, not a check
MIN_SUMMARY = 30  # "Hotovo." is not a check's result


def is_check(conn: sqlite3.Connection, row) -> bool:
    """A task from a schedule whose name (or the task's title) says it is a check."""
    source = row["source"] or ""
    if not source.startswith("schedule:") or not source[9:].isdigit():
        return False
    s = conn.execute("SELECT name FROM schedules WHERE id = ?", (int(source[9:]),)).fetchone()
    name = f"{s['name'] if s else ''} {row['title'] or ''}"
    return bool(CHECK_NAME_RE.search(name)) and not CHANGE_RE.search(name)


def is_code(conn: sqlite3.Connection, row, note: str | None) -> bool:
    if (row["topic"] or "").lower() in CODE_TOPICS or _role(conn, row["assignee_id"]) in CODE_ROLES:
        return True
    return bool(CODE_RE.search(_text(row, note)))


def is_doc(row, note: str | None) -> bool:
    if (row["topic"] or "").lower() in DOC_TOPICS or DOC_TITLE_RE.search(row["title"] or ""):
        return not DECISION_RE.search(note or "")
    return False


def is_high(conn: sqlite3.Connection, row, note: str | None) -> str:
    """Why this result always gets a person's review ('' when it does not)."""
    source = (row["source"] or "").lower()
    if source.startswith(HIGH_SOURCES):
        return "a customer reply"
    if (row["topic"] or "").lower() in HIGH_TOPICS:
        return f"topic {row['topic']}"
    if row["returned_count"]:
        return "returned before"
    from .business import _owner_asked

    if not source.startswith("schedule:") and _owner_asked(conn, row, actors.owner_id(conn)):
        return "the owner's own request"
    if HIGH_RE.search(_text(row, note)) and not is_doc(row, note):
        return "money or a commitment"  # a digest *about* an invoice is still a note
    return ""


def triage_size(conn: sqlite3.Connection, task_id: int) -> str | None:
    r = conn.execute("""SELECT json_extract(detail, '$.size') FROM audit_log WHERE action = 'triage'
                        AND entity = 'task' AND entity_id = ? ORDER BY id DESC LIMIT 1""", (task_id,)).fetchone()
    return (r[0] or None) if r else None


def experienced(conn: sqlite3.Connection, actor_id: int | None, now: datetime | None = None) -> bool:
    """≥ 10 results done in 60 days and at most 15 % of them returned."""
    if not actor_id:
        return False
    since = ((now or datetime.now(timezone.utc)) - timedelta(days=EXPERIENCE_DAYS)).isoformat(timespec="seconds")
    r = conn.execute("""SELECT COUNT(*) AS n, COALESCE(SUM(CASE WHEN returned_count > 0 THEN 1 ELSE 0 END), 0) AS ret
                        FROM tasks WHERE assignee_id = ? AND status = 'done' AND completed_at >= ?""",
                     (actor_id, since)).fetchone()
    return r["n"] >= EXPERIENCED_ACCEPTED and r["ret"] <= EXPERIENCED_MAX_RETURN_RATE * r["n"]


def is_small(conn: sqlite3.Connection, row) -> bool:
    if row["estimate_min"] and int(row["estimate_min"]) <= SMALL_MINUTES:
        return True
    return (triage_size(conn, row["id"]) or "").upper() in ("XS", "S")


def qa_id(conn: sqlite3.Connection) -> int | None:
    from .tasks import _can_review_as_agent

    qa = actors.find_by_name(conn, roles.QA)
    return qa["id"] if qa is not None and not qa["archived_at"] and _can_review_as_agent(conn, qa["id"]) else None


def decide(conn: sqlite3.Connection, row, note: str | None) -> Decision:
    """The risk class of a result handed in with `note` (its result / progress note)."""
    from . import schedules, verification

    high = is_high(conn, row, note)
    verified = verification.has_line(note)
    if not high:
        if is_check(conn, row) and len((note or "").strip()) >= MIN_SUMMARY and schedules.all_green(note):
            return Decision("low", "accept", reason="a routine check handed in all green")
        if is_doc(row, note) and not CODE_RE.search(note or ""):
            return Decision("low", "accept", reason="a document or note that asks nobody for a decision")
        if verified and verification.tests_passed(note) and is_small(conn, row) \
                and experienced(conn, row["assignee_id"]):
            return Decision("low", "accept", reason="a small task by an experienced agent, tests passing")
    if is_code(conn, row, note):
        qa = qa_id(conn)
        if qa and qa != row["assignee_id"]:
            return Decision("code" if not high else "high", "route", qa, "code goes to the QA Reviewer")
    return Decision("high" if high else "normal", "keep", reason=high)


def auto_accept(conn: sqlite3.Connection, task_id: int, reason: str, follow_ups: bool = True) -> None:
    """Accept a low-risk result in the platform's name (the caller commits). `follow_ups`: tell an
    ask_owner asker and retire a one-shot agent (pos.tasks.update does both itself)."""
    from . import asks, business, comments, versioning
    from .agents import retire_if_done

    ctx = business.system_ctx(conn)
    before = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    versioning.update(conn, ctx, "task", task_id, {"status": "done", "progress": 100, "completed_at": now_iso()},
                      action="auto_accept")
    comments.log(conn, ctx, task_id, f"Auto-accepted (low risk: {reason}). Review policy: pos.review_policy; "
                                     "anyone who may review it can reopen it.", "review")
    audit.log(conn, ctx, "review_auto_accept", "task", task_id, reason=reason)
    if not follow_ups:
        return
    try:
        from .tasks import get

        out = get(conn, Ctx(actors.owner_id(conn), via="system"), task_id)
        asks.on_task_changed(conn, ctx, before, out)
        retire_if_done(conn, ctx, out["assignee_id"])
    except Exception:  # noqa: BLE001 - the accept stands; the follow-ups are best effort
        pass


# ------------------------------------------------------------------ the backlog

def sweep(conn: sqlite3.Connection, apply: bool = False, now: datetime | None = None) -> dict:
    """Run the policy over every result waiting for review: low risk accepted, code to the QA
    Reviewer, then the SLA (over 12 h to the reviewer's lead, the owner's to the CEO)."""
    from . import business, tasks

    accepted, routed = [], []
    owner = actors.owner_id(conn)
    rows = conn.execute("SELECT * FROM tasks WHERE status = 'review' AND archived_at IS NULL ORDER BY updated_at"
                        ).fetchall()
    for row in rows:
        ref = tasks.display_id(row["id"])
        if row["reviewer_id"] == owner and row["created_by"] == owner:
            continue  # the owner asked for it and reviews it himself (the SLA still sends it to the CEO)
        d = decide(conn, row, row["progress_note"])
        if d.action == "accept":
            accepted.append(f"{ref} ({d.reason}): {row['title'][:60]}")
            if apply:
                auto_accept(conn, row["id"], d.reason)
        elif d.action == "route" and d.reviewer_id != tasks.reviewer_of(conn, row):
            routed.append(f"{ref}→{actors.get(conn, d.reviewer_id)['name']}: {row['title'][:60]}")
            if apply:
                tasks.hand_review(conn, business.system_ctx(conn), row["id"], d.reviewer_id, d.reason)
    if apply:
        conn.commit()
    sla = business.review_sla(conn, now=now, limit=200, dry_run=not apply)
    if not apply:  # the dry run: what the policy handles above does not wait for the SLA
        handled = {x.split(" ", 1)[0].split("→", 1)[0] for x in accepted + routed}
        sla = {k: [x for x in v if x.split("→", 1)[0] not in handled] for k, v in sla.items()}
    return {"waiting": len(rows), "auto_accepted": accepted, "to_qa": routed, "sla": sla, "applied": apply}


def main(argv: list[str] | None = None) -> None:
    from .config import get_settings
    from .db import connect

    p = argparse.ArgumentParser(description="Apply the review policy to the results waiting for review.")
    p.add_argument("--apply", action="store_true", help="change the data (default: a dry run)")
    p.add_argument("--db", default=None)
    a = p.parse_args(argv)
    conn = connect(Path(a.db) if a.db else get_settings().db_path)
    try:
        print(json.dumps(sweep(conn, apply=a.apply), ensure_ascii=False, indent=1))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
