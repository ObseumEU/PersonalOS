"""Business value: what the agent company delivers for the owner, and what it costs him.

The owner (the board) talks to the CEO, approves as little as possible, and the
company does real work. This module holds the numbers and the small rules that
keep it that way:

- **Business vs platform** (`value_kind`): every task is `business` (work with
  value for the owner: customers, money, Obseum's products, his home, his
  knowledge) or `platform` (running the agent company itself: incidents,
  routines, reviews, PersonalOS). The label is derived from the task's source,
  topic, repository and assignee's role; `tasks.value_kind` overrides it
  (`business`, `platform`, or `demo` for seed/demo tasks that reports skip).
- **Interventions**: the owner's DMs, edits, returns and approvals on an agent's
  task count on that task (`tasks.interventions`, history action `intervene`,
  audit `intervention` with the kind), deduplicated per task and kind for
  30 minutes. Each kind has an estimate of the owner's minutes.
- **Review SLA**: a result waiting over 24 h for an agent reviewer goes to that
  reviewer's lead (one message per new reviewer), and each reviewer gets one review digest a day
  instead of a reminder per task; a result that would wait for the owner goes to the CEO first
  (at hand-in, and after 12 h for older ones); low-risk results are auto-accepted and code goes to
  the QA Reviewer at hand-in (pos.review_policy); the CEO accepts what it can and
  hands the owner only what truly needs him (request_review reviewer=Owner).
- **Escalation dedup**: an agent escalating an item that already has an open
  escalation (the same source task, e.g. one invoice) gets that task back with
  a comment instead of a new task.
- **Idle agents**: an agent with no input for 7 days is flagged to the CEO once
  a week (pausing or archiving is the CEO's decision).
- **Chain of command nudge**: an agent's unsolicited message to the owner is
  delivered (never blocked) but the agent gets a platform note, and a line in
  its next prompts (`nudges`).
- **Week numbers** for the weekly report: business KPIs (invoices and amounts
  from knowlage mail and Drive, open customer threads, the pipeline, goal
  progress), the business/platform cost split, and owner minutes vs work
  delivered.

Nothing here costs model tokens; the knowlage lookups are two cheap searches.
"""

import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from . import actors, audit
from .core import Ctx

log = logging.getLogger(__name__)

VALUE_KINDS = ("business", "platform", "demo")

# ------------------------------------------------------------------ schema (no numbered migration)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """tasks.value_kind (the owner's override of the automatic label; NULL = automatic)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    if cols and "value_kind" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN value_kind TEXT")
        conn.commit()


# ------------------------------------------------------------------ who is who

def role_id(conn: sqlite3.Connection, role: str) -> int | None:
    row = conn.execute("SELECT id FROM actors WHERE role = ? AND archived_at IS NULL AND kind != 'human' "
                       "ORDER BY id LIMIT 1", (role,)).fetchone()
    return row["id"] if row else None


def ceo_id(conn: sqlite3.Connection) -> int | None:
    return role_id(conn, "ceo")


def _is_agent(row) -> bool:
    return row is not None and row["kind"] in ("ai", "agent")


# ------------------------------------------------------------------ business vs platform

# Seed and demo tasks from the first days (the made-up Acme and the house examples).
DEMO_TITLES = {
    "send the signed contract to acme", "draft personalos phase 2 scope", "vat documents from the accountant",
    "summarise the house insurance policy", "book the car service", "renew or cancel the acme agreement",
    "acme renewal?? check contract + tell them", "collect feedback from the obseum team",
    "check mcp from the laptop",
}
DEMO_RE = re.compile(r"\bacme\b", re.IGNORECASE)
PLATFORM_TOPICS = {"provoz", "ops", "incident", "routine", "brief", "review", "weekly", "board", "standup",
                   "digest", "hr", "access", "rozpocet", "budget", "personalos", "dev", "platform", "tools",
                   "agents", "chat", "system"}
BUSINESS_TOPICS = {"finance", "mail", "customers", "customer", "sales", "growth", "leads", "community",
                   "content", "legal", "home", "ha", "homeassistant", "obseum", "knowledge", "servers"}
PLATFORM_ROLES = {"sre", "monitor", "qa", "access_manager", "hr", "coach", "security", "deployer"}
BUSINESS_ROLES = {"customer_success", "growth", "community", "content", "cfo", "legal", "home_automation",
                  "assistant"}
PLATFORM_REPOS = {"obseumeu/personalos"}
PLATFORM_SOURCES = {"event:sentinel", "event:grafana", "scheduler", "weekly_report", "deployer", "system"}
BUSINESS_SOURCES = {"event:gmail", "event:discord", "event:calendar"}
_REPO_RE = re.compile(r"([\w.-]+/[\w.-]+)[#!]\d+")


BUSINESS_TEAMS = {"kniha", "growth", "customers", "finance", "home", "sales", "obseum-ai"}
PLATFORM_TEAMS = {"engineering", "people", "platform"}


def _team_kind(conn: sqlite3.Connection, row) -> str | None:
    """business | platform from the task's project (its slug, else its lead's team) and the
    assignee's (else the creator's) team; None when they say nothing."""
    keys = row.keys() if hasattr(row, "keys") else row
    pid = row["project_id"] if "project_id" in keys else None
    if pid:
        try:
            p = conn.execute("""SELECT p.slug, a.team FROM projects p LEFT JOIN actors a ON a.id = p.lead_id
                                WHERE p.id = ?""", (pid,)).fetchone()
        except sqlite3.OperationalError:
            p = None
        if p is not None:
            for v in (p["slug"], p["team"]):
                if (v or "").lower() in BUSINESS_TEAMS:
                    return "business"
    for who in (row["assignee_id"], row["created_by"] if "created_by" in keys else None):
        if not who:
            continue
        a = conn.execute("SELECT team, kind FROM actors WHERE id = ?", (who,)).fetchone()
        if a is None or a["kind"] == "human":
            continue
        team = (a["team"] or "").lower()
        if team in BUSINESS_TEAMS:
            return "business"
        if team in PLATFORM_TEAMS:
            return "platform"
        break  # the assignee's team decides when it has one
    return None


def is_demo(row) -> bool:
    title = (row["title"] or "").strip().lower()
    return title in DEMO_TITLES or bool(DEMO_RE.search(row["title"] or "")) or (row["topic"] or "") == "acme"


def classify(conn: sqlite3.Connection, row, roles: dict[int, str | None] | None = None) -> str:
    """business | platform | demo for one task row (value_kind wins when set)."""
    keys = row.keys() if hasattr(row, "keys") else row
    if "value_kind" in keys and row["value_kind"] in VALUE_KINDS:
        return row["value_kind"]
    if is_demo(row):
        return "demo"
    source = (row["source"] or "").lower()
    topic = (row["topic"] or "").lower()
    if source == "event:github" or source.startswith("event:github"):
        m = _REPO_RE.search(row["title"] or "")
        return "platform" if not m or m[1].lower() in PLATFORM_REPOS else "business"
    if source in PLATFORM_SOURCES or source.startswith("review:"):
        return "platform"
    # The work's project and team count before its topic: a Kniha developer's "dev" task is the Kniha
    # product, business (prod: Kniha roles were counted as platform).
    team_kind = _team_kind(conn, row)
    if team_kind == "business":
        return "business"
    if topic in PLATFORM_TOPICS:
        return "platform"
    if source in BUSINESS_SOURCES or topic in BUSINESS_TOPICS:
        return "business"
    aid = row["assignee_id"]
    role = None
    if aid:
        if roles is not None and aid in roles:
            role = roles[aid]
        else:
            r = conn.execute("SELECT role FROM actors WHERE id = ?", (aid,)).fetchone()
            role = r["role"] if r else None
    if role in BUSINESS_ROLES:
        return "business"
    if role in PLATFORM_ROLES or team_kind == "platform":
        return "platform"
    # What the owner asks for himself is business; coordination between agents is platform.
    creator = row["created_by"]
    if creator and conn.execute("SELECT 1 FROM actors WHERE id = ? AND is_owner = 1", (creator,)).fetchone():
        return "business"
    if row["parent_id"]:
        parent = conn.execute("SELECT * FROM tasks WHERE id = ?", (row["parent_id"],)).fetchone()
        if parent is not None and parent["id"] != row["id"]:
            return classify(conn, parent, roles)
    return "platform"


def set_value_kind(conn: sqlite3.Connection, ctx: Ctx, task_id: int, kind: str | None) -> dict:
    """The owner (or a lead with tasks:write) overrides the label; None goes back to automatic."""
    from . import tasks, versioning

    ensure_schema(conn)
    if kind not in (None, "", *VALUE_KINDS):
        raise tasks.Invalid(f"value_kind must be one of {VALUE_KINDS} (or empty for automatic)")
    row = tasks._row(conn, ctx, task_id)
    from .visibility import check_write

    check_write(conn, tasks.ENTITY, row, ctx.actor_id)
    versioning.update(conn, ctx, tasks.ENTITY, task_id, {"value_kind": kind or None}, action="value_kind")
    return tasks.get(conn, ctx, task_id)


def _roles(conn: sqlite3.Connection) -> dict[int, str | None]:
    return {r["id"]: r["role"] for r in conn.execute("SELECT id, role FROM actors")}


# ------------------------------------------------------------------ interventions (the owner's time)

# Minutes of the owner's attention each intervention takes (an estimate, shown as such).
OWNER_MINUTES = {"dm": 2, "edit": 2, "return": 5, "approval": 1, "ask": 3, "review": 2}
DEDUP_MINUTES = 30
OWNER_VIAS = ("api", "mcp", "ui", "web")


def record_intervention(conn: sqlite3.Connection, ctx: Ctx, task_id: int, kind: str, note: str = "") -> bool:
    """The owner stepped in on an agent's task: +1 on tasks.interventions (history `intervene`)
    and an audit line with the kind. Only the owner acting in person (web, his own MCP), only on
    tasks an agent works on; the same kind on the same task counts once per 30 minutes."""
    from . import tasks, versioning

    if ctx.via not in OWNER_VIAS:
        return False
    me = conn.execute("SELECT is_owner FROM actors WHERE id = ?", (ctx.actor_id,)).fetchone()
    if not me or not me["is_owner"]:
        return False
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None or row["assignee_type"] not in ("ai", "agent"):
        return False
    since = (datetime.now(timezone.utc) - timedelta(minutes=DEDUP_MINUTES)).isoformat(timespec="seconds")
    if conn.execute("""SELECT 1 FROM audit_log WHERE action = 'intervention' AND entity = 'task' AND entity_id = ?
                       AND at >= ? AND json_extract(detail, '$.kind') = ?""", (task_id, since, kind)).fetchone():
        return False
    versioning.update(conn, ctx, tasks.ENTITY, task_id, {"interventions": (row["interventions"] or 0) + 1},
                      action="intervene")
    audit.log(conn, ctx, "intervention", "task", task_id, kind=kind, minutes=OWNER_MINUTES.get(kind, 2),
              assignee=row["assignee_id"], note=note[:200] or None)
    return True


def owner_minutes(conn: sqlite3.Connection, s: str, u: str) -> dict:
    """The owner's interventions in [s, u) by kind, with the estimated minutes."""
    by_kind: dict[str, int] = {}
    for r in conn.execute("""SELECT json_extract(detail, '$.kind') AS kind, COUNT(*) AS n FROM audit_log
                             WHERE action = 'intervention' AND at >= ? AND at < ? GROUP BY 1""", (s, u)):
        by_kind[r["kind"] or "other"] = r["n"]
    # Accepting an agent's result is the owner's time too (not an intervention: the work was fine).
    owner = actors.owner_id(conn)
    accepts = conn.execute("""SELECT COUNT(*) FROM history h JOIN tasks t ON t.id = h.entity_id
                              WHERE h.entity = 'task' AND h.action = 'accept' AND h.actor_id = ? AND h.at >= ?
                              AND h.at < ? AND t.assignee_type IN ('ai', 'agent')""", (owner, s, u)).fetchone()[0]
    if accepts:
        by_kind["review"] = accepts
    minutes =sum(OWNER_MINUTES.get(k, 2) * n for k, n in by_kind.items())
    return {"interventions": sum(n for k, n in by_kind.items() if k != "review"), "by_kind": by_kind,
            "minutes": minutes}


# ------------------------------------------------------------------ chain of command nudge

# Who may contact the owner directly (docs/REORG.md): the CEO; the Chief of Staff's digest and
# weekly report; the Hlídač's critical incidents; the Access manager's digest; the HA safety OKs.
MAY_CONTACT_OWNER = {"ceo", "chief_of_staff", "monitor", "access_manager", "home_automation"}
NUDGE = ("Platform note: this went straight to the owner. The owner talks to the CEO; agents report to "
         "their lead. Next time ask your lead (org_chart) and let the CEO bring to the owner only what "
         "needs him. Replying when the owner wrote to you is always fine.")


def owner_contact_note(conn: sqlite3.Connection, ctx: Ctx, channel_id: int, owner_targeted: bool) -> str | None:
    """An agent contacted the owner (a DM or an @mention). Solicited (the owner wrote to it in the
    last 24 h, or it answers his chat) or an allowed role: nothing. Else an audit line and the note
    (the message is still delivered: a nudge, never a block)."""
    if not owner_targeted:
        return None
    me = actors.get(conn, ctx.actor_id)
    if me["kind"] == "human" or me["role"] in MAY_CONTACT_OWNER:
        return None
    owner = actors.owner_id(conn)
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    wrote = conn.execute(
        """SELECT 1 FROM chat_messages m WHERE m.author_id = ? AND m.created_at >= ? AND (m.channel_id = ?
           OR EXISTS (SELECT 1 FROM chat_inbox i WHERE i.message_id = m.id AND i.actor_id = ?))""",
        (owner, since, channel_id, ctx.actor_id)).fetchone()
    if wrote:
        return None
    from . import chat

    if chat.may_answer(conn, ctx.actor_id, channel_id):
        return None
    audit.log(conn, ctx, "chain_nudge", "channel", channel_id)
    return NUDGE


def nudges(conn: sqlite3.Connection, actor_id: int) -> list[str]:
    """Platform notes for the agent's next prompts (pos.api_worker /me)."""
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    n = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'chain_nudge' AND actor_id = ? AND at >= ?",
                     (actor_id, since)).fetchone()[0]
    if not n:
        return []
    return [f"In the last 7 days you contacted the owner directly {n}x without him asking. The owner talks "
            "only to the CEO: send decisions, questions and reports to your lead (org_chart); the CEO brings "
            "him what truly needs him. Answering when he writes to you is fine."]


# ------------------------------------------------------------------ owner-assigned tasks (step caps)

def _owner_asked(conn: sqlite3.Connection, row, owner: int) -> bool:
    source = (row["source"] or "").lower()
    if row["created_by"] == owner and not source.startswith("event:") and source not in PLATFORM_SOURCES:
        return True
    return conn.execute("SELECT 1 FROM history WHERE entity = 'task' AND entity_id = ? AND actor_id = ? "
                        "AND action = 'assign' LIMIT 1", (row["id"], owner)).fetchone() is not None


_MSG_RE = re.compile(r"(?:zpráv[aěuy]|message|msg)\s+(\d{1,9})", re.IGNORECASE)


def owner_request(conn: sqlite3.Connection, row) -> bool:
    """The owner asked for this work himself: he created or assigned it, it cites his chat message,
    or it was split from (T-ref) a task he asked for."""
    owner = actors.owner_id(conn)
    if _owner_asked(conn, row, owner):
        return True
    text = _text(row)
    for mid in _MSG_RE.findall(text)[:10]:
        if conn.execute("SELECT 1 FROM chat_messages WHERE id = ? AND author_id = ?", (int(mid), owner)).fetchone():
            return True
    seen: set[int] = set()
    frontier = {r for r in _refs(text) if r != row["id"]}
    for _ in range(3):
        nxt: set[int] = set()
        for tid in frontier - seen:
            seen.add(tid)
            t = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
            if t is None:
                continue
            if _owner_asked(conn, t, owner):
                return True
            nxt |= {r for r in _refs(_text(t)) if r < tid}
        frontier = nxt
        if not frontier:
            break
    return False


# ------------------------------------------------------------------ escalation dedup

_REF_RE = re.compile(r"\bT-(\d{1,6})\b")
ESCALATION_TOPICS = {"digest"}
DEDUP_DAYS = 7


def _text(row) -> str:
    return f"{row['title'] or ''}\n{row['notes'] or ''}"


def _refs(text: str) -> set[int]:
    return {int(x) for x in _REF_RE.findall(text or "")}


def _roots(conn: sqlite3.Connection, ids: set[int], depth: int = 4) -> set[int]:
    """The source items behind task refs: follow each referenced task's own refs to older tasks."""
    out: set[int] = set()
    frontier = set(ids)
    seen: set[int] = set()
    for _ in range(depth):
        nxt: set[int] = set()
        for tid in frontier - seen:
            seen.add(tid)
            row = conn.execute("SELECT id, title, notes FROM tasks WHERE id = ?", (tid,)).fetchone()
            if row is None:
                continue
            older = {r for r in _refs(f"{row['title']}\n{row['notes']}") if r < tid}
            if older:
                nxt |= older
            else:
                out.add(tid)
        if not nxt:
            break
        frontier = nxt
    return out | {t for t in frontier if t not in seen}


def is_escalation(conn: sqlite3.Connection, creator_id: int, assignee_id: int | None, topic: str | None) -> bool:
    """An agent passing an item up: to its lead (or anyone above it), to the CEO, the Chief of Staff's
    digest, or the owner."""
    if (topic or "").lower() in ESCALATION_TOPICS:
        return True
    if not assignee_id:
        return False
    target = conn.execute("SELECT * FROM actors WHERE id = ?", (assignee_id,)).fetchone()
    if target is None:
        return False
    if target["is_owner"]:
        return False  # tickets for the owner (ask_owner) have their own dedup by task and topic (pos.asks)
    if target["role"] in ("ceo", "chief_of_staff"):
        return True
    lead, hops = conn.execute("SELECT reports_to FROM actors WHERE id = ?", (creator_id,)).fetchone(), 0
    lead = lead["reports_to"] if lead else None
    while lead and hops < 6:
        if lead == assignee_id:
            return True
        r = conn.execute("SELECT reports_to FROM actors WHERE id = ?", (lead,)).fetchone()
        lead, hops = (r["reports_to"] if r else None), hops + 1
    return False


# The source item behind an escalation without a T-ref: the mail/Drive link it quotes, or a document
# code (an offer or invoice number such as TKP-N-0088). T-147 -> T-148 -> T-149 was one invoice.
# Only links that name one item (a mail thread, a Drive file, an external block's ref), never a
# generic one (a repository, a dashboard) that unrelated escalations share.
_ITEM_URL_RE = re.compile(r"https?://(?:mail|drive|docs)\.google\.com/[^\s\"'<>)\]]+")
_EXT_REF_RE = re.compile(r"<external [^>]*\bref=\"([^\"]+)\"")
_CODE_RE = re.compile(r"\b(?!T-\d)[A-Z]{2,6}(?:-[A-Z0-9]{1,6})*-\d{3,}\b")


def _source_keys(conn: sqlite3.Connection, text: str, roots: set[int]) -> set[str]:
    """What identifies the item outside the task graph: item links and document codes in the text
    and in the source tasks behind it."""
    texts = [text] + [_text(r) for r in (conn.execute("SELECT title, notes FROM tasks WHERE id = ?", (t,)).fetchone()
                                         for t in roots) if r is not None]
    keys: set[str] = set()
    for t in texts:
        keys |= {u.rstrip(".,;") for u in _ITEM_URL_RE.findall(t or "")}
        keys |= {u.rstrip(".,;") for u in _EXT_REF_RE.findall(t or "")}
        keys |= set(_CODE_RE.findall(t or ""))
    return keys


def _not_an_escalation(source: str | None, created_by, assignee_id) -> bool:
    """Scheduled work, platform items (review work, routines) and an agent's task for itself are no
    escalation: they are never merged into another task, nor another task into them."""
    src = (source or "").lower()
    return (src.startswith(("schedule:", "review:", "meeting")) or src in PLATFORM_SOURCES
            or (created_by is not None and created_by == assignee_id))


def find_duplicate_escalation(conn: sqlite3.Connection, ctx: Ctx, values: dict) -> int | None:
    """An open escalation (last DEDUP_DAYS) of the same item: it is named directly, both name the same
    source task, or both carry the same source link or document code in their own text. None otherwise.

    A shared source reference is required; a chain of older tasks reached by following refs is not one
    (prod 2026-10-02: the CEO's scheduled daily report shared a distant root with T-232, a quota alert, and
    was merged into it, so the promised 16:00 report never ran). Scheduled and self-assigned tasks are
    never merged, and are never the target of a merge."""
    me = conn.execute("SELECT kind FROM actors WHERE id = ?", (ctx.actor_id,)).fetchone()
    if not me or me["kind"] not in ("ai", "agent") or values.get("parent_id"):
        return None
    if _not_an_escalation(values.get("source"), ctx.actor_id, values.get("assignee_id")):
        return None
    text = f"{values.get('title') or ''}\n{values.get('notes') or ''}"
    refs = _refs(text)
    if not is_escalation(conn, ctx.actor_id, values.get("assignee_id"), values.get("topic")):
        return None
    keys = _source_keys(conn, text, set())
    if not refs and not keys:
        return None
    since = (datetime.now(timezone.utc) - timedelta(days=DEDUP_DAYS)).isoformat(timespec="seconds")
    for c in conn.execute(
            """SELECT * FROM tasks WHERE archived_at IS NULL AND status NOT IN ('done', 'someday') AND created_at >= ?
               AND parent_id IS NULL AND created_by IN (SELECT id FROM actors WHERE kind IN ('ai', 'agent'))
               ORDER BY id""", (since,)).fetchall():
        if _not_an_escalation(c["source"], c["created_by"], c["assignee_id"]):
            continue
        if not is_escalation(conn, c["created_by"], c["assignee_id"], c["topic"]):
            continue
        # It names an open escalation directly, both name the same source task, or the same link/code.
        if c["id"] in refs or (refs & _refs(_text(c))) or (keys and _source_keys(conn, _text(c), set()) & keys):
            return c["id"]
    return None


def link_duplicate(conn: sqlite3.Connection, ctx: Ctx, existing: int, values: dict) -> None:
    """The same item escalated again: a comment on the open escalation. When the one escalating it
    holds that task, the task itself moves on to the new assignee (a handoff: reassign + comment),
    so one item stays one task up the chain."""
    from . import comments, tasks

    who = actors.get(conn, ctx.actor_id)["name"]
    body = (f"{who} escalated the same item again ({values.get('title', '')[:200]}); linked here instead of a "
            f"new task.\n\n{(values.get('notes') or '')[:1500]}")
    comments.log(conn, ctx, existing, body, "comment")
    audit.log(conn, ctx, "escalation_dedup", "task", existing, title=values.get("title", "")[:200])
    row = conn.execute("SELECT assignee_id, status, created_by, source FROM tasks WHERE id = ?", (existing,)).fetchone()
    target = values.get("assignee_id")
    creator = actors.get(conn, row["created_by"]) if row["created_by"] else None
    # Passed up only by the one holding it, never the owner's own request or scheduled work, and its
    # creator hears of it (a task is never moved behind its creator's back).
    if (target and row["assignee_id"] == ctx.actor_id and target != ctx.actor_id
            and not actors.get(conn, target)["is_owner"] and creator is not None and not creator["is_owner"]
            and not _not_an_escalation(row["source"], None, None)):
        from . import org, versioning

        try:
            org.handoff(conn, ctx, existing, int(target),
                        note=f"passed up instead of a new task: {values.get('title', '')[:200]}")
            if creator["id"] not in (ctx.actor_id, target) and not creator["archived_at"]:
                from . import chat

                chat.send_dm(conn, ctx, creator["id"],
                             f"{tasks.display_id(existing)} (yours) was passed up to "
                             f"{actors.get(conn, target)['name']} as the same item escalated again.",
                             attachments=[{"type": "task", "id": existing}], priority="fyi", system=True)
            if row["status"] == "review":
                versioning.update(conn, ctx, tasks.ENTITY, existing, {"status": "next"}, action="handoff")
                from . import review_work

                review_work.close_for(conn, existing, "passed up as an escalation")
            return
        except Exception:  # noqa: BLE001 - the comment is there either way
            log.exception("could not pass %s up", existing)
    if target and target != row["assignee_id"]:
        try:
            from . import chat

            a = actors.get(conn, values["assignee_id"])
            if not a["is_owner"]:
                chat.send_dm(conn, ctx, a["id"], f"{who}: {tasks.display_id(existing)} already tracks this item "
                                                 "(escalated earlier); see the comment there.",
                             attachments=[{"type": "task", "id": existing}], system=True)
        except Exception:  # noqa: BLE001 - the link is what matters
            log.exception("could not tell the assignee of the duplicate")


# ------------------------------------------------------------------ review SLA

REVIEW_SLA_HOURS = 24  # a review nobody did this long moves to the reviewer's lead
DIGEST_AFTER_HOURS = 12  # a result waiting this long puts its reviewer on the daily digest


def system_ctx(conn: sqlite3.Connection) -> Ctx:
    """The platform's own voice for reminders it sends by itself: "PersonalOS" (pos.notices), never
    the owner and no agent. A reminder sent as "Owner" counted as the owner's unanswered message in
    the chat watch (T-194/T-195), and "Owner handed in …" read as his words (prod 2026-10)."""
    from .notices import system_ctx as _platform

    return _platform(conn)


def review_triage_target(conn: sqlite3.Connection, row, note: str | None = None) -> int | None:
    """Who reviews instead of the owner (pos.review_policy.owner_stand_in): the CEO for what needs
    judgment, else the assignee's team lead. None when the stand-in did the work, or already
    triaged this result and left it for the owner."""
    from .review_policy import owner_stand_in

    if conn.execute("SELECT 1 FROM audit_log WHERE action = 'review_triage' AND entity = 'task' AND entity_id = ?",
                    (row["id"],)).fetchone():
        return None
    return owner_stand_in(conn, row, note if note is not None else row["progress_note"])


def _can_review(conn: sqlite3.Connection, actor_id: int) -> bool:
    from .tasks import _can_review_as_agent

    return _can_review_as_agent(conn, actor_id)


def _escalation_target(conn: sqlite3.Connection, row, reviewer: int, owner: int, ceo: int | None) -> int | None:
    """Who takes over a review nobody did in REVIEW_SLA_HOURS: the reviewer's nearest lead who may
    review (never the owner, never the assignee), else the CEO; the owner's reviews go to his
    stand-in (the CEO, or the team lead for what needs no judgment). None: it stays (the CEO's own
    reviews: its lead is the owner; it gets the daily digest)."""
    if reviewer == owner:
        return review_triage_target(conn, row)
    if reviewer == ceo:
        return None
    lead = conn.execute("SELECT reports_to FROM actors WHERE id = ?", (reviewer,)).fetchone()
    lead = lead["reports_to"] if lead else None
    hops = 0
    while lead and hops < 6:
        a = conn.execute("SELECT is_owner, reports_to FROM actors WHERE id = ?", (lead,)).fetchone()
        if a is None or a["is_owner"]:
            lead = None
            break
        if lead != row["assignee_id"] and _can_review(conn, lead):
            break
        lead, hops = a["reports_to"], hops + 1
    return lead or (ceo if ceo and ceo not in (reviewer, row["assignee_id"]) else None)


def _digest_due(conn: sqlite3.Connection, reviewer: int, now: datetime) -> bool:
    """One digest per reviewer per day (Europe/Prague)."""
    from .core import TZ

    midnight = now.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    return conn.execute("""SELECT 1 FROM audit_log WHERE action = 'review_digest' AND entity = 'actor'
                           AND entity_id = ? AND at >= ?""",
                        (reviewer, midnight.isoformat(timespec="seconds"))).fetchone() is None


def _age_h(at: str, now: datetime) -> int:
    t = datetime.fromisoformat(at)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return max(0, int((now - t).total_seconds() // 3600))


def review_sla(conn: sqlite3.Connection, now: datetime | None = None, limit: int | None = None,
               dry_run: bool = False) -> dict:
    """The hourly review job (scheduler `review_sla`):

    - a result waiting over REVIEW_SLA_HOURS (24 h) moves to the reviewer's lead (the owner's to his
      stand-in, pos.review_policy.owner_stand_in). Every reviewer's rows are looked at (prod: a
      LIMIT 30 was always filled by the CEO's oldest rows, so the QA/CTO rows never escalated). Each
      new reviewer gets one message listing what moved to it, not one per task;
    - every reviewer (agents and people, never the owner) with results waiting over
      DIGEST_AFTER_HOURS gets one digest a day listing them, instead of a reminder DM per task
      (prod: ~58 reminder DMs a day, 26 % of all chat);
    - the review work items are kept in step (pos.review_work.sync).

    Everything is sent in the platform's voice (system_ctx), never the owner's. `dry_run`: only say
    what would move and who would get a digest. `limit` is kept for callers; it no longer cuts the scan."""
    from . import chat, comments, review_work, tasks, versioning, wake

    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=REVIEW_SLA_HOURS)).isoformat(timespec="seconds")
    digest_cutoff = (now - timedelta(hours=DIGEST_AFTER_HOURS)).isoformat(timespec="seconds")
    owner = actors.owner_id(conn)
    ceo = ceo_id(conn)
    sys_ctx = system_ctx(conn)
    moved: list[str] = []
    reminded: list[str] = []
    moved_to: dict[int, list[str]] = {}
    waiting_sql = """SELECT * FROM tasks WHERE status = 'review' AND archived_at IS NULL
                     AND COALESCE(source, '') NOT LIKE 'review:%' ORDER BY updated_at"""
    for row in conn.execute(waiting_sql).fetchall():
        # The clock runs from when the reviewer had it as work: the hand-in, or its review item (a
        # backlog older than the items is not moved up the chain the hour the items appear).
        item = conn.execute("SELECT MAX(created_at) FROM tasks WHERE source = ?",
                            (review_work.source_of(row["id"]),)).fetchone()[0]
        if max(row["updated_at"], item or "") >= cutoff:
            continue
        reviewer = tasks.reviewer_of(conn, row)
        if item is None and review_work.deferred(conn, row, reviewer):
            continue  # a backlog result the batching has not reached: its clock starts at its item
        target = _escalation_target(conn, row, reviewer, owner, ceo)
        if not target or target == reviewer:
            continue
        ref = tasks.display_id(row["id"])
        name = actors.get(conn, target)["name"]
        moved.append(f"{ref}→{name}")
        if dry_run:
            continue
        why = ("the owner's review goes to a stand-in first (it leaves him only what truly needs him)"
               if reviewer == owner else f"no review in {REVIEW_SLA_HOURS} h: the reviewer's lead takes it over")
        versioning.update(conn, sys_ctx, tasks.ENTITY, row["id"], {"reviewer_id": target}, action="review_escalate")
        comments.log(conn, sys_ctx, row["id"], f"Review moved to {name}: {why}.", "system")
        audit.log(conn, sys_ctx, "review_escalate", "task", row["id"], **{"from": reviewer, "to": target})
        if reviewer == owner:
            audit.log(conn, sys_ctx, "review_triage", "task", row["id"], to=target)
        review_work.ensure(conn, row["id"], why)
        moved_to.setdefault(target, []).append(
            f"- {ref} '{row['title'][:80]}'" + (" (the owner's: hand him only what truly needs him, "
                                                 "request_review reviewer=Owner)" if reviewer == owner else ""))
    for target, lines in moved_to.items():
        chat.send_dm(conn, sys_ctx, target,
                     f"{len(lines)} review(s) moved to you (nobody reviewed them in {REVIEW_SLA_HOURS} h). "
                     "Accept each or return it with what should change (review_task):\n" + "\n".join(lines[:40])
                     + (f"\n… and {len(lines) - 40} more (to_review)" if len(lines) > 40 else ""),
                     priority="fyi", system=True)
        wake.wake(target)
    # The daily digest: one message per reviewer listing everything that waits for it.
    waiting: dict[int, list[sqlite3.Row]] = {}
    for row in conn.execute(waiting_sql).fetchall():
        waiting.setdefault(tasks.reviewer_of(conn, row), []).append(row)
    for reviewer, items in waiting.items():
        r = actors.get(conn, reviewer)
        if r["is_owner"] or r["archived_at"] or not any(i["updated_at"] < digest_cutoff for i in items):
            continue
        if not _digest_due(conn, reviewer, now):
            continue
        reminded += [tasks.display_id(i["id"]) for i in items]
        if dry_run:
            continue
        lines = [f"- {tasks.display_id(i['id'])} '{i['title'][:80]}' waits for your review "
                 f"({_age_h(i['updated_at'], now)} h)" for i in items[:30]]
        more = f"\n… and {len(items) - 30} more (list_tasks view=to_review)" if len(items) > 30 else ""
        work = " Each one is a 'Review: T-x' task in your queue." if r["kind"] != "human" else ""
        chat.send_dm(conn, sys_ctx, reviewer,
                     f"Review digest: {len(items)} result(s) wait for your review.{work} Accept each or return "
                     "it with what should change (review_task); hand the owner only what truly needs him "
                     "(request_review reviewer=Owner).\n" + "\n".join(lines) + more,
                     priority="fyi", system=True)
        audit.log(conn, sys_ctx, "review_digest", "actor", reviewer, tasks=len(items))
        if r["kind"] != "human":
            wake.wake(reviewer)
    if not dry_run:
        review_work.sync(conn, now=now)
        conn.commit()
    return {k: v for k, v in (("moved", moved), ("reminded", reminded)) if v}


# ------------------------------------------------------------------ idle agents

IDLE_DAYS = 7


def idle_agents(conn: sqlite3.Connection, now: datetime | None = None) -> list[dict]:
    """Agents with no input for 7 days: no task assigned or created for them, no message to them,
    no run. Dormant roles and system members are left out."""
    from . import agents_code

    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=IDLE_DAYS)).isoformat(timespec="seconds")
    out = []
    for a in conn.execute("""SELECT * FROM actors WHERE kind IN ('ai', 'agent') AND archived_at IS NULL
                             AND runtime != 'service' AND created_at < ? ORDER BY name""", (since,)).fetchall():
        if a["role"] in ("ceo",) or agents_code.is_dormant(a["name"]):
            continue
        busy = conn.execute(
            """SELECT 1 WHERE EXISTS (SELECT 1 FROM tasks WHERE assignee_id = ? AND (created_at >= ? OR updated_at >= ?))
               OR EXISTS (SELECT 1 FROM chat_inbox i JOIN chat_messages m ON m.id = i.message_id
                          WHERE i.actor_id = ? AND m.created_at >= ?)
               OR EXISTS (SELECT 1 FROM runs WHERE actor_id = ? AND started_at >= ?)""",
            (a["id"], since, since, a["id"], since, a["id"], since)).fetchone()
        if not busy:
            last = conn.execute("SELECT MAX(started_at) FROM runs WHERE actor_id = ?", (a["id"],)).fetchone()[0]
            out.append({"id": a["id"], "name": a["name"], "role": a["role"], "last_run": last})
    return out


def idle_agents_job(conn: sqlite3.Connection) -> dict:
    """Weekly: one task for the CEO listing the idle agents (pause, archive or give them work)."""
    from . import tasks
    from .weekly_packet import current_week

    idle = idle_agents(conn)
    ceo = ceo_id(conn)
    if not idle or not ceo:
        return {"idle": [a["name"] for a in idle]}
    week = current_week()
    title = f"Agenti bez práce 7 dní · {week}"
    if conn.execute("SELECT 1 FROM tasks WHERE title = ? AND archived_at IS NULL", (title,)).fetchone():
        return {"idle": [a["name"] for a in idle], "skipped": "already flagged this week"}
    lines = "\n".join(f"- **{a['name']}** ({a['role'] or '—'}), poslední běh {(a['last_run'] or 'nikdy')[:10]}"
                      for a in idle)
    t = tasks.create(conn, Ctx(actors.owner_id(conn), via="scheduler"), {
        "title": title, "assignee": {"type": "agent", "id": ceo}, "status": "next", "priority": 3,
        "topic": "board", "source": "scheduler",
        "notes": ("### Proč\nTito agenti 7 dní nedostali žádný vstup (úkol, zprávu ani běh). Nečinný agent nic "
                  "nestojí, ale zbytečná role ztěžuje přehled.\n\n### Kdo\n" + lines +
                  "\n\n### Co udělat\nRozhodni u každého: dát mu práci (úkol nebo routinu), pozastavit "
                  "(`manage_agent pause`), nebo navrhnout archivaci Head of People. Majitele to nepotřebuje."),
        "definition_of_done": "U každého agenta ze seznamu je rozhodnuto a zapsáno proč.",
        "reviewer": ceo})
    conn.commit()
    return {"idle": [a["name"] for a in idle], "task": t["ref"]}


# ------------------------------------------------------------------ the cost ledger

def run_cost(conn: sqlite3.Connection, run_id: int) -> None:
    """runs.cost_usd and tokens are derived from engine_usage, the one ledger."""
    u = conn.execute("""SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd), 0) AS c FROM engine_usage WHERE run_id = ?""",
                     (run_id,)).fetchone()
    if u["n"]:
        conn.execute("UPDATE runs SET cost_usd = ? WHERE id = ?", (round(u["c"], 6), run_id))


def reconcile_ledger(conn: sqlite3.Connection) -> dict:
    """Runs whose cost disagrees with engine_usage (older runs recorded before runs.cost_usd existed)
    get the ledger's value. Idempotent."""
    rows = conn.execute("""SELECT r.id FROM runs r JOIN (SELECT run_id, SUM(cost_usd) AS c FROM engine_usage
                           WHERE run_id IS NOT NULL GROUP BY run_id) u ON u.run_id = r.id
                           WHERE r.cost_usd IS NULL OR ABS(r.cost_usd - u.c) > 0.000001""").fetchall()
    for r in rows:
        run_cost(conn, r["id"])
    conn.commit()
    return {"fixed": len(rows)}


def cost_split(conn: sqlite3.Connection, s: str, u: str) -> dict:
    """engine_usage cost in [s, u) split by the task's label, plus cost per business outcome
    (done top-level business tasks in the period)."""
    ensure_schema(conn)
    roles = _roles(conn)
    out = {"business": 0.0, "platform": 0.0, "demo": 0.0, "unlinked": 0.0}
    cache: dict[int, str] = {}
    for r in conn.execute("SELECT task_id, SUM(cost_usd) AS c FROM engine_usage WHERE at >= ? AND at < ? "
                          "GROUP BY task_id", (s, u)):
        if not r["task_id"]:
            out["unlinked"] += r["c"] or 0
            continue
        t = conn.execute("SELECT * FROM tasks WHERE id = ?", (r["task_id"],)).fetchone()
        kind = cache.setdefault(r["task_id"], classify(conn, t, roles) if t else "platform")
        out[kind] += r["c"] or 0
    outcomes = 0
    for t in conn.execute("""SELECT * FROM tasks WHERE status = 'done' AND completed_at >= ? AND completed_at < ?
                             AND archived_at IS NULL AND parent_id IS NULL AND COALESCE(topic, '') != 'chat'""",
                          (s, u)).fetchall():
        if classify(conn, t, roles) == "business":
            outcomes += 1
    total = sum(out.values())
    return {"business_usd": round(out["business"], 2), "platform_usd": round(out["platform"] + out["unlinked"], 2),
            "demo_usd": round(out["demo"], 2), "total_usd": round(total, 2), "business_outcomes": outcomes,
            "usd_per_business_outcome": round(out["business"] / outcomes, 2) if outcomes else None,
            "business_share": round(out["business"] / total, 3) if total else None}


def work_delivered(conn: sqlite3.Connection, s: str, u: str) -> dict:
    """Agents' done top-level tasks in [s, u), with the estimated minutes (estimate_min, else 20)."""
    roles = _roles(conn)
    n, minutes, business = 0, 0, 0
    for t in conn.execute("""SELECT * FROM tasks WHERE status = 'done' AND completed_at >= ? AND completed_at < ?
                             AND archived_at IS NULL AND parent_id IS NULL AND assignee_type IN ('ai', 'agent')
                             AND COALESCE(topic, '') != 'chat' AND COALESCE(source, '') NOT LIKE 'review:%'""",
                          (s, u)).fetchall():
        kind = classify(conn, t, roles)
        if kind == "demo":
            continue
        n += 1
        minutes += int(t["estimate_min"] or 20)
        business += kind == "business"
    return {"tasks": n, "business_tasks": business, "minutes": minutes}


# ------------------------------------------------------------------ business KPIs (knowlage)

AMOUNT_RE = re.compile(r"(\d{1,3}(?:[  .]\d{3})*(?:,\d{2})?|\d+(?:,\d{2})?)\s*(Kč|CZK|EUR|€)", re.IGNORECASE)
_BLOCK_RE = re.compile(r"<external [^>]*>(.*?)</external>", re.DOTALL)
OWN_DOMAINS = ("obseum.cz", "obseum.eu")
INVOICE_QUERY = "faktura invoice zálohová faktura vydaná faktura přijatá"


def _amount(value: str) -> float:
    return float(value.replace(" ", "").replace(" ", "").replace(".", "").replace(",", "."))


def parse_invoices(text: str) -> list[dict]:
    """knowlage search hits → invoices: {title, direction sent|received, amount, currency, doc}.
    The amount is the largest one in the passage (the total), when any; a guess, shown as such."""
    out, seen = [], set()
    for block in _BLOCK_RE.findall(text or "") or ([text] if text else []):
        head = next((line for line in block.splitlines() if line.startswith("### ")), "")
        doc = re.search(r"`([^`]+?):c\d+`", head)
        key = doc[1] if doc else head
        lines = block.splitlines()
        title = next((line.split(" · sekce")[0] for line in lines[2:4] if line.strip()), "")[:160]
        low = block.lower()
        if not re.search(r"faktur|invoice|rechnung|daňový doklad", low) or key in seen:
            continue
        seen.add(key)
        sender = re.search(r"^Od:.*?<?([\w.+-]+@[\w.-]+)>?", block, re.MULTILINE)
        sent = ("vydaná faktura" in low or re.search(r"dodavatel\s+obseum", low) is not None
                or bool(sender and sender[1].lower().endswith(OWN_DOMAINS)))
        amounts = [(_amount(m[1]), m[2].upper().replace("€", "EUR").replace("KČ", "CZK"))
                   for m in AMOUNT_RE.finditer(block)]
        amounts = [a for a in amounts if 0 < a[0] < 50_000_000]
        best = max(amounts, default=None)
        out.append({"title": title, "direction": "sent" if sent else "received",
                    "amount": best[0] if best else None, "currency": best[1] if best else None, "doc": key})
    return out


def _kb_search(query: str, date_from: str, date_to: str, k: int = 40) -> str:
    from . import kb_files

    ws = os.environ.get("POS_KNOWLAGE_WORKSPACE", "firma")
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "search", "arguments": {"query": query, "k": k, "kinds": ["chunk"],
                                                       "date_from": date_from, "date_to": date_to, "effort": 1}}}
    r = kb_files._request("POST", "/mcp", params={"workspace": ws}, json=call, timeout=60,
                          headers={"Accept": "application/json, text/event-stream"})
    return kb_files._tool_text(r)


def invoices(s: str, u: str) -> dict:
    """Invoices received and sent in the week, from knowlage (mail and Drive), with totals by currency."""
    from . import kb_files

    if not kb_files.configured():
        return {"available": False, "note": "knowlage is not configured (POS_KNOWLAGE_API_KEY)"}
    day_to = (datetime.fromisoformat(u) - timedelta(seconds=1)).date().isoformat()
    try:
        items = parse_invoices(_kb_search(INVOICE_QUERY, s[:10], day_to))
    except Exception as e:  # noqa: BLE001 - the report keeps the rest
        return {"available": False, "note": f"knowlage unavailable: {str(e)[:160]}"}
    totals: dict[str, dict[str, float]] = {"sent": {}, "received": {}}
    for i in items:
        if i["amount"] is not None:
            bucket = totals[i["direction"]]
            bucket[i["currency"]] = round(bucket.get(i["currency"], 0) + i["amount"], 2)
    return {"available": True, "sent": sum(1 for i in items if i["direction"] == "sent"),
            "received": sum(1 for i in items if i["direction"] == "received"), "totals": totals,
            "items": items[:12],
            "note": "found by knowlage search (mail and Drive); amounts are the largest sum in each document, a guess"}


def customer_threads(conn: sqlite3.Connection) -> dict:
    """Open customer mail: open tasks of the Head of Customer Success, and from mail events."""
    cs = role_id(conn, "customer_success")
    rows = conn.execute(
        """SELECT id, title, status, created_at FROM tasks WHERE archived_at IS NULL AND status NOT IN ('done', 'someday')
           AND (source = 'event:gmail' OR assignee_id IS ?) AND COALESCE(topic, '') NOT IN ('chat', 'finance')
           ORDER BY created_at""", (cs,)).fetchall()
    return {"open": len(rows), "oldest": [{"ref": f"T-{r['id']:03d}", "title": r["title"],
                                           "since": (r["created_at"] or "")[:10]} for r in rows[:5]]}


def pipeline(conn: sqlite3.Connection, s: str, u: str) -> dict:
    """The Head of Growth's pipeline: open leads and opportunities, new and won this week."""
    g = role_id(conn, "growth")
    if g is None:
        return {"open": 0, "new": 0, "done": 0}
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
    base = "FROM tasks WHERE assignee_id = ? AND archived_at IS NULL AND COALESCE(topic, '') != 'chat'"
    return {"open": q(f"SELECT COUNT(*) {base} AND status NOT IN ('done', 'someday')", g),
            "new": q(f"SELECT COUNT(*) {base} AND created_at >= ? AND created_at < ?", g, s, u),
            "done": q(f"SELECT COUNT(*) {base} AND status = 'done' AND completed_at >= ? AND completed_at < ?", g, s, u)}


def drafts_in_approvals(conn: sqlite3.Connection, s: str, u: str) -> dict:
    """Outbound drafts agents put in Approvals this week (mail, posts, comments): the owner's one-click work."""
    by = {r["action"]: r["n"] for r in conn.execute(
        "SELECT action, COUNT(*) AS n FROM approvals WHERE created_at >= ? AND created_at < ? GROUP BY action", (s, u))}
    return {"total": sum(by.values()), "by_action": by,
            "approved": conn.execute("SELECT COUNT(*) FROM approvals WHERE decided_at >= ? AND decided_at < ? "
                                     "AND status = 'approved'", (s, u)).fetchone()[0]}


def week_section(conn: sqlite3.Connection, s: str, u: str, *, outside: bool = True) -> dict:
    """The business part of the week packet (pos.weekly_packet)."""
    from . import goals as goals_mod

    ensure_schema(conn)
    owner = owner_minutes(conn, s, u)
    work = work_delivered(conn, s, u)
    goals = [goals_mod.brief(g) for g in goals_mod.list_goals(conn, "active")]
    out = {
        "invoices": invoices(s, u) if outside else {"available": False, "note": "skipped"},
        "customer_threads": customer_threads(conn),
        "pipeline": pipeline(conn, s, u),
        "drafts_in_approvals": drafts_in_approvals(conn, s, u),
        "cost_split": cost_split(conn, s, u),
        "owner_time": {**owner, "work": work,
                       "line": (f"Majitel ~{owner['minutes']} min ({owner['interventions']} zásahů) vs. agenti "
                                f"{work['tasks']} hotových úkolů (~{round(work['minutes'] / 60, 1)} h práce, "
                                f"z toho byznys {work['business_tasks']})")},
        "goals": {"active": len(goals),
                  "avg_progress": round(sum(g.get("progress") or 0 for g in goals) / len(goals)) if goals else None},
    }
    return out
