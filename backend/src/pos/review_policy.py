"""Review burn-down: who reviews a handed-in result, by its risk class.

Measured on 2026-10-01: 42 of 225 tasks sat in `review`, some over 12 h, the oldest 38 h, nearly all
waiting for the CEO. Most of them did not need a person at all. Every hand-in now gets a risk class:

- **low**: auto-accepted at once, with a note in the task's activity and a `review_auto_accept` audit
  line saying why:
  - a routine task from a schedule (a daily or weekly check, a digest) handed in with nothing to act
    on (no finding, no incident: pos.schedules.all_green), with a real summary;
  - a document or note (a digest, summary, standup, report, minutes, research) that asks nobody
    for a decision;
  - an incoming item (an `event:` task: a mail) triaged with nothing to act on and nothing sent;
  - a small task (estimate ≤ 60 min, or sized S by the triage) with a verification line, no code
    and nothing sent outside; or a small one by an experienced agent (≥ 10 results accepted in
    60 days, ≤ 15 % returned) whose tests passed.
- **code**: a code change goes to the **QA Reviewer** (the default reviewer for code), unless it did
  the work itself. Kniha work (project, team or topic kniha) goes to the **Kniha Lead** instead: QA
  cannot see that repository (T-731); the Kniha Lead's own code goes to QA, never to itself.
- **high**: customer replies, money, contracts, anything sent outside or waiting for an approval,
  the owner's own requests, and results that were returned before: never auto-accepted.
- **normal**: the usual reviewer (pos.tasks.reviewer_of).

Whatever would wait for the owner goes to a stand-in first (owner_stand_in, T-472): the **CEO** only
for what needs judgment (plans, money and commitments, customers and outbound, the owner's own
requests, results asking for a decision, and the work of the CEO's direct reports); everything else
to the **assignee's team lead** (reports_to, skipping the assignee and the owner). A result nobody
reviewed in 24 h moves to the reviewer's lead, and each reviewer gets one review digest a day
(pos.business.review_sla); an agent reviewer has each result as a "Review: T-x" task (pos.review_work).

No auto-accept without evidence (prod 10-02..10-05: 68 automatic accepts in 3 days, 38 of them "a
routine task from a schedule", the CEO's and CTO's weekly plans among them): every low rule needs the
result to show what it rests on: a verification line (pos.verification) or a concrete piece of
evidence (pos.evidence.extract: a commit, a URL, a file or note id, a sent message), or, for a
routine check, the tasks its findings are tracked in. A plan (a "plán" anywhere in the title, the
weekly plans from a schedule) is never routine.

The small-task rules need the result's verification line (pos.verification). An explicit
`request_review` (the agent chose its reviewer) and a reviewer the owner set are respected.

The backlog: `python -m pos.review_policy` (a dry run: what would happen to every result waiting
for review) and `python -m pos.review_policy --apply`. The effect over past days:
`python -m pos.review_policy --measure 2026-09-25 2026-10-02`. The Kniha results already waiting
for QA, once: `python -m pos.review_policy --kniha --apply`.
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
# An ask, not "Rozhodnutí: nic" (a triage's verdict) or "frontu schválení" (a digest's line).
DECISION_RE = re.compile(r"rozhodn(?:out|ěte|ete|i|eš|ul|ula)\b|(?:ke|k|na)\s+schválení|schval(?:te|it|íš)\b|"
                         r"schválit|approve\b|approval (?:needed|required)|\bdecide\b|"
                         r"(?:your|owner's|david's) decision|potřebuj[ie]\s+(?:od|tvoje|vaše|rozhodnutí|schválení)|"
                         r"needs? (?:your|the owner|david)|čeká na (?:majitel|davida|ownera|rozhodnutí|schválení)|"
                         r"\?\s*$", re.IGNORECASE)
PLAN_TOPICS = {"plan", "plán", "strategy", "strategie", "roadmap"}
PLAN_RE = re.compile(r"^\s*(?:plán|plan|strategi|roadmap|návrh|navrh|proposal|rozpočet|rozpocet|budget)",
                     re.IGNORECASE)
HIGH_SOURCES = ("support:",)
HIGH_TOPICS = {"zakaznici", "customers", "finance", "legal", "bezpecnost", "security"}
HIGH_RE = re.compile(r"\b(?:platb[auy]|zaplat|faktur|smlouv|contract|payment|invoice|cenov[áa] nabídk|"
                     r"price quote|refund)\w*", re.IGNORECASE)
CODE_TOPICS = {"code", "dev", "kniha-dev"}  # plans with topic engineering are not code
CODE_ROLES = {"developer"}
KNIHA = "kniha"  # the project slug, the team and the topic prefix (kniha-dev)
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


CHANGE_RE = re.compile(r"zlepš|zleps|improv|oprav|fix|implement|návrh|navrh", re.IGNORECASE)  # work, not a check
MIN_SUMMARY = 30  # "Hotovo." is not a check's result


def is_routine(conn: sqlite3.Connection, row) -> bool:
    """A task from a schedule (a recurring check, review or digest), not one that changes things."""
    source = row["source"] or ""
    if not source.startswith("schedule:") or not source[9:].isdigit():
        return False
    s = conn.execute("SELECT name FROM schedules WHERE id = ?", (int(source[9:]),)).fetchone()
    return not CHANGE_RE.search(f"{s['name'] if s else ''} {row['title'] or ''}")


_TREF_RE = re.compile(r"\bT-(\d{1,6})\b")


def tracked_in(conn: sqlite3.Connection, row, note: str | None) -> list[str]:
    """Other tasks the result links (T-123) that exist: where its findings are tracked."""
    out = []
    for x in dict.fromkeys(int(n) for n in _TREF_RE.findall(note or "")):
        if x != row["id"] and conn.execute("SELECT 1 FROM tasks WHERE id = ? AND archived_at IS NULL",
                                           (x,)).fetchone():
            out.append(f"T-{x:03d}")
    return out


def is_triage(row) -> bool:
    """An incoming item (a mail, an event) someone sorted: a reply, a task, or nothing."""
    return (row["source"] or "").startswith("event:")


PLAN_WORD_RE = re.compile(r"\bpl[áa]n\w*|\bplan(?:s|ning)?\b|\bstrategi|\broadmap", re.IGNORECASE)


def is_plan(row) -> bool:
    return (row["topic"] or "").lower() in PLAN_TOPICS or bool(PLAN_RE.search(row["title"] or ""))


def plan_like(row) -> bool:
    """A plan by its topic or any word of its title ("CEO: pondělní plán", "CTO: technický plán týdne")."""
    return is_plan(row) or bool(PLAN_WORD_RE.search(row["title"] or ""))


def evidenced(note: str | None) -> bool:
    """The result shows what it rests on: a verification line or a concrete piece of evidence."""
    from . import evidence, verification

    return verification.has_line(note) or any(evidence.extract(note).values())


def sent_outside(conn: sqlite3.Connection, task_id: int) -> str:
    """What the task sent or asked to send outside ('' when nothing): an approval (money, a
    commitment, the owner's channel) or an ordinary outbound (pos.outbound)."""
    if conn.execute("SELECT 1 FROM approvals WHERE task_id = ? LIMIT 1", (task_id,)).fetchone():
        return "an approval (money, a commitment or the owner's channel)"
    if conn.execute("SELECT 1 FROM audit_log WHERE entity = 'task' AND entity_id = ? AND action LIKE 'outbound:%' "
                    "LIMIT 1", (task_id,)).fetchone():
        return "something sent outside"
    return ""


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
    sent = sent_outside(conn, row["id"])
    if sent:
        return sent
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
                        FROM tasks WHERE assignee_id = ? AND status = 'done' AND completed_at >= ?
                        AND COALESCE(source, '') NOT LIKE 'review:%'""",
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


def is_kniha(conn: sqlite3.Connection, row) -> bool:
    """Work of the Kniha project: its project, a kniha topic or an assignee from team kniha."""
    if (row["topic"] or "").lower().startswith(KNIHA):
        return True
    if row["project_id"] and conn.execute("SELECT 1 FROM projects WHERE id = ? AND slug = ?",
                                          (row["project_id"], KNIHA)).fetchone():
        return True
    return bool(row["assignee_id"] and conn.execute("SELECT 1 FROM actors WHERE id = ? AND lower(team) = ?",
                                                    (row["assignee_id"], KNIHA)).fetchone())


def kniha_lead_id(conn: sqlite3.Connection) -> int | None:
    from .tasks import _can_review_as_agent

    lead = actors.find_by_name(conn, roles.KNIHA_LEAD)
    return lead["id"] if lead is not None and not lead["archived_at"] and _can_review_as_agent(conn, lead["id"]) \
        else None


def code_reviewer(conn: sqlite3.Connection, row) -> tuple[int | None, str]:
    """Who reviews a code change: Kniha work its Kniha Lead (QA cannot see that repository, T-731),
    the rest and the Kniha Lead's own work the QA Reviewer. Never the author."""
    if is_kniha(conn, row):
        lead = kniha_lead_id(conn)
        if lead and lead != row["assignee_id"]:
            return lead, "Kniha code goes to the Kniha Lead"
    qa = qa_id(conn)
    if qa and qa != row["assignee_id"]:
        return qa, "code goes to the QA Reviewer"
    return None, ""


def decide(conn: sqlite3.Connection, row, note: str | None) -> Decision:
    """The risk class of a result handed in with `note` (its result / progress note)."""
    from . import schedules, verification

    high = is_high(conn, row, note)
    from . import evidence

    high = high or evidence.flagged(conn, row["id"])  # a done-claim without evidence: never auto-accepted
    verified = verification.has_line(note)
    code = is_code(conn, row, note)
    summary = len((note or "").strip()) >= MIN_SUMMARY
    shown = evidenced(note)
    routine = is_routine(conn, row) and summary and not plan_like(row)
    if not high:
        if routine and shown and schedules.all_green(note) and not CODE_RE.search(note or ""):
            return Decision("low", "accept", reason="a routine task from a schedule, nothing to act on")
        tracked = tracked_in(conn, row, note) if routine else []
        if tracked and not CODE_RE.search(note or ""):
            # Its findings already have their own tasks (the SRE's check -> T-183): nothing left to review.
            return Decision("low", "accept", reason="a routine check whose findings are tracked in "
                                                    + ", ".join(tracked))
        if is_doc(row, note) and shown and not plan_like(row) and not CODE_RE.search(note or ""):
            return Decision("low", "accept", reason="a document or note that asks nobody for a decision")
        if is_triage(row) and summary and shown and not code and schedules.all_green(note) \
                and not DECISION_RE.search(note or ""):
            return Decision("low", "accept", reason="an incoming item triaged, nothing to act on, nothing sent")
        if verified and not code and is_small(conn, row):
            return Decision("low", "accept", reason="a small verified task, no code, nothing sent")
        if verified and verification.tests_passed(note) and is_small(conn, row) \
                and experienced(conn, row["assignee_id"]):
            return Decision("low", "accept", reason="a small task by an experienced agent, tests passing")
    if code:
        reviewer, why = code_reviewer(conn, row)
        if reviewer:
            return Decision("code" if not high else "high", "route", reviewer, why)
    if not row["reviewer_id"]:
        other = ceo_offload_if_default(conn, row, note)
        if other:
            return Decision("high" if high else "normal", "route", other,
                            "the CEO reviews only owner-facing and strategic results")
    return Decision("high" if high else "normal", "keep", reason=high)


# Who reviews instead of the CEO a result that is neither owner-facing nor strategic: the assignee's
# own lead (below the CEO), else an operations reviewer (the COO), else the QA Reviewer. Prod
# 2026-10-04: 22 of the first 40 review runs were the CEO's (it created the tasks it delegated, so
# pos.tasks.reviewer_of made it the reviewer of all of them), ~$0.21 each and the slowest queue.
GENERAL_REVIEWER_ROLES = ("project_manager",)
STRATEGIC_TOPICS = {"board", "strategy", "strategie", "goals", "cile", "cíle"}


def owner_facing_or_strategic(conn: sqlite3.Connection, row, note: str | None) -> str:
    """Why the CEO itself reviews this result ('' when someone else can): what needs_ceo names
    (plans, money, customers, outbound, the owner's own request, a decision asked), the owner's
    request in the wider sense (pos.business.owner_request), or a board/strategy topic."""
    why = needs_ceo(conn, row, note)
    if why:
        return why
    if (row["topic"] or "").lower() in STRATEGIC_TOPICS:
        return f"topic {row['topic']}"
    from .business import owner_request

    try:
        if owner_request(conn, row):
            return "the owner's request"
    except Exception:  # noqa: BLE001 - unsure: the CEO keeps it
        return "unknown"
    return ""


def ceo_offload(conn: sqlite3.Connection, row) -> int | None:
    """The reviewer instead of the CEO: the assignee's lead below the CEO, a general reviewer, the QA
    Reviewer; None when there is nobody (the CEO keeps it)."""
    from .business import _can_review, ceo_id

    ceo, assignee = ceo_id(conn), row["assignee_id"]
    lead = team_lead(conn, assignee)
    if lead and lead not in (ceo, assignee):
        return lead
    for role in GENERAL_REVIEWER_ROLES:
        r = conn.execute("SELECT id FROM actors WHERE role = ? AND archived_at IS NULL ORDER BY id LIMIT 1",
                         (role,)).fetchone()
        if r and r["id"] not in (ceo, assignee) and _can_review(conn, r["id"]):
            return r["id"]
    qa = qa_id(conn)
    return qa if qa and qa not in (ceo, assignee) else None


def ceo_offload_if_default(conn: sqlite3.Connection, row, note: str | None) -> int | None:
    """Another reviewer when the default one (pos.tasks.reviewer_of) is the CEO and the result is
    neither owner-facing nor strategic; else None."""
    from .business import ceo_id
    from .tasks import reviewer_of

    ceo = ceo_id(conn)
    if not ceo or row["assignee_id"] == ceo or reviewer_of(conn, row) != ceo:
        return None
    if owner_facing_or_strategic(conn, row, note):
        return None
    return ceo_offload(conn, row)


def needs_ceo(conn: sqlite3.Connection, row, note: str | None) -> str:
    """Why a result that would wait for the owner needs the CEO's judgment ('' when its team lead
    can review it): plans, money, customers, outbound, the owner's own requests, a decision asked."""
    high = is_high(conn, row, note)
    if high and high != "returned before":
        return high
    if is_plan(row):
        return "a plan"
    if DECISION_RE.search(note or ""):
        return "asks for a decision"
    return ""


def team_lead(conn: sqlite3.Connection, assignee_id: int | None) -> int | None:
    """The assignee's nearest lead who may review (reports_to upwards), never the owner."""
    from .tasks import _can_review_as_agent

    seen = {assignee_id}
    r = conn.execute("SELECT reports_to FROM actors WHERE id = ?", (assignee_id,)).fetchone() if assignee_id else None
    lead = r["reports_to"] if r else None
    while lead and lead not in seen:
        a = conn.execute("SELECT is_owner, reports_to FROM actors WHERE id = ?", (lead,)).fetchone()
        if a is None or a["is_owner"]:
            return None
        if _can_review_as_agent(conn, lead):
            return lead
        seen.add(lead)
        lead = a["reports_to"]
    return None


def owner_stand_in(conn: sqlite3.Connection, row, note: str | None) -> int | None:
    """Who reviews instead of the owner: the CEO for what needs judgment, else the assignee's team
    lead (the CEO again when the assignee reports to it). None: the owner reviews it himself."""
    from .business import _can_review, ceo_id

    target = None if needs_ceo(conn, row, note) else team_lead(conn, row["assignee_id"])
    if target and target == ceo_id(conn) and not owner_facing_or_strategic(conn, row, note):
        target = ceo_offload(conn, row) or target  # the CEO only for owner-facing and strategic results
    target = target or ceo_id(conn)
    if not target or target == row["assignee_id"] or not _can_review(conn, target):
        return None
    return target


def auto_accept(conn: sqlite3.Connection, task_id: int, reason: str, follow_ups: bool = True) -> None:
    """Accept a low-risk result in the platform's name (the caller commits). `follow_ups`: tell an
    ask_owner asker and retire a one-shot agent (pos.tasks.update does both itself)."""
    from . import asks, business, comments, versioning
    from .agents import retire_if_done

    ctx = business.system_ctx(conn)
    before = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    versioning.update(conn, ctx, "task", task_id, {"status": "done", "progress": 100, "completed_at": now_iso()},
                      action="auto_accept")
    from . import review_work

    review_work.close_for(conn, task_id, "auto-accepted")
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

def sweep(conn: sqlite3.Connection, apply: bool = False, now: datetime | None = None, sla: bool = True) -> dict:
    """Run the policy over every result waiting for review: low risk accepted, code to the QA
    Reviewer, then the SLA (over 12 h to the reviewer's lead, the owner's to the CEO)."""
    from . import business, tasks

    accepted, routed, to_lead = [], [], []
    owner = actors.owner_id(conn)
    ceo = business.ceo_id(conn)
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
        elif d.action == "keep" and ceo and row["reviewer_id"] == ceo and _from_owner(conn, row["id"]):
            # The CEO got it only as the owner's stand-in: its team lead reviews it now (T-472).
            target = owner_stand_in(conn, row, row["progress_note"])
            if target and target != ceo:
                to_lead.append(f"{ref}→{actors.get(conn, target)['name']}: {row['title'][:60]}")
                if apply:
                    tasks.hand_review(conn, business.system_ctx(conn), row["id"], target,
                                      "the owner's review goes to the team lead, the CEO only for judgment calls")
    if apply:
        conn.commit()
    if not sla:  # the caller runs the SLA itself (pos.prodfix_workflow: after the review items exist)
        return {"waiting": len(rows), "auto_accepted": accepted, "to_qa": routed, "to_lead": to_lead, "sla": {},
                "applied": apply}
    sla = business.review_sla(conn, now=now, limit=200, dry_run=not apply)
    if not apply:  # the dry run: what the policy handles above does not wait for the SLA
        handled = {x.split(" ", 1)[0].split("→", 1)[0].rstrip(":") for x in accepted + routed + to_lead}
        sla = {k: [x for x in v if x.split("→", 1)[0] not in handled] for k, v in sla.items()}
    return {"waiting": len(rows), "auto_accepted": accepted, "to_qa": routed, "to_lead": to_lead, "sla": sla,
            "applied": apply}


def redirect_kniha(conn: sqlite3.Connection, apply: bool = False) -> dict:
    """Once (T-731): Kniha results waiting for the QA Reviewer go to the Kniha Lead, except its own."""
    from . import business, tasks

    qa, lead = qa_id(conn), kniha_lead_id(conn)
    moved = []
    if qa and lead:
        for row in conn.execute("SELECT * FROM tasks WHERE status = 'review' AND archived_at IS NULL "
                                "AND reviewer_id = ? ORDER BY updated_at", (qa,)).fetchall():
            if row["assignee_id"] == lead or not is_kniha(conn, row):
                continue
            moved.append(f"{tasks.display_id(row['id'])}→{roles.KNIHA_LEAD}: {row['title'][:60]}")
            if apply:
                tasks.hand_review(conn, business.system_ctx(conn), row["id"], lead,
                                  "Kniha results are reviewed by the Kniha Lead (T-731)")
        if apply:
            conn.commit()
    return {"to_kniha_lead": moved, "applied": apply}


def _from_owner(conn: sqlite3.Connection, task_id: int) -> bool:
    """The result reached its reviewer as the owner's stand-in (a review_triage line)."""
    return conn.execute("SELECT 1 FROM audit_log WHERE action = 'review_triage' AND entity = 'task' "
                        "AND entity_id = ? LIMIT 1", (task_id,)).fetchone() is not None


# ------------------------------------------------------------------ the measurement

def measure(conn: sqlite3.Connection, since: str, until: str) -> dict:
    """Results handed in for review between `since` and `until` (ISO dates, `until` inclusive):
    how many reached the CEO then, and where the current policy would send each (an estimate: the
    task as it was at its last hand-in in the window, judged with today's rules and org chart)."""
    from . import business, tasks

    owner, ceo = actors.owner_id(conn), business.ceo_id(conn)
    end = (datetime.fromisoformat(until) + timedelta(days=1)).date().isoformat()
    snaps: dict[int, list[dict]] = {}
    for h in conn.execute("SELECT entity_id, data FROM history WHERE entity = 'task' AND at >= ? AND at < ? "
                          "ORDER BY id", (since, end)):
        data = json.loads(h["data"] or "{}")
        if data.get("status") == "review":
            snaps.setdefault(h["entity_id"], []).append(data)
    out = {"since": since, "until": until, "handed_in": len(snaps), "ceo_before": 0,
           "after": {"auto_accept": 0, "qa": 0, "team_lead": 0, "ceo": 0, "owner": 0, "other": 0},
           "ceo_after": [], "reasons": {}}
    for task_id, versions in snaps.items():
        current = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if current is None:
            continue
        if ceo and any(v.get("reviewer_id") == ceo for v in versions):
            out["ceo_before"] += 1
        row = {**dict(current), **versions[-1], "id": task_id}
        note = row.get("progress_note")
        d = decide(conn, row, note)
        if d.action == "accept":
            out["after"]["auto_accept"] += 1
            out["reasons"][d.reason] = out["reasons"].get(d.reason, 0) + 1
            continue
        if d.action == "route":
            out["after"]["qa" if d.reviewer_id == qa_id(conn) else
                         "team_lead" if d.reviewer_id == team_lead(conn, row.get("assignee_id")) else "other"] += 1
            continue
        # What hand-in gave it: an explicit reviewer stays, the owner's goes to its stand-in.
        reviewer = versions[0].get("reviewer_id")
        if not reviewer or reviewer == owner or (reviewer == ceo and _from_owner(conn, task_id)):
            first = {**row, "reviewer_id": None}
            reviewer = tasks.reviewer_of(conn, first)
            if reviewer == owner and not (row.get("created_by") == owner and versions[0].get("reviewer_id") == owner):
                reviewer = owner_stand_in(conn, row, note) or owner
        if reviewer == ceo:
            out["after"]["ceo"] += 1
            out["ceo_after"].append(f"{tasks.display_id(task_id)} ({needs_ceo(conn, row, note) or 'CEO is the lead'}): "
                                    f"{(row.get('title') or '')[:60]}")
        elif reviewer == owner:
            out["after"]["owner"] += 1
        elif reviewer == team_lead(conn, row.get("assignee_id")):
            out["after"]["team_lead"] += 1
        else:
            out["after"]["other"] += 1
    return out


def main(argv: list[str] | None = None) -> None:
    from .config import get_settings
    from .db import connect

    p = argparse.ArgumentParser(description="Apply the review policy to the results waiting for review.")
    p.add_argument("--apply", action="store_true", help="change the data (default: a dry run)")
    p.add_argument("--db", default=None)
    p.add_argument("--measure", nargs=2, metavar=("SINCE", "UNTIL"),
                   help="no change: how many results reached the CEO between two dates, and after the policy")
    p.add_argument("--kniha", action="store_true",
                   help="only move the Kniha results waiting for the QA Reviewer to the Kniha Lead")
    a = p.parse_args(argv)
    conn = connect(Path(a.db) if a.db else get_settings().db_path)
    try:
        result = measure(conn, *a.measure) if a.measure else \
            redirect_kniha(conn, apply=a.apply) if a.kniha else sweep(conn, apply=a.apply)
        print(json.dumps(result, ensure_ascii=False, indent=1))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
