"""The customer-issue pipeline: intake → triage → fix → reply draft → one item for the owner.

1. Intake (job `support_intake`, every 2 minutes): new-mail events from knowlage wait as `support:pending`
   (pos.routing). Each one: the thread is read from Gmail (read-only), a deterministic pre-filter and one
   claude-haiku-4-5 call classify it (pos.support.classify), the customer's history comes from knowlage and
   the project is matched (pos.support.match).
2. Triage: a sure support issue or bug report with a project → one "Zákaznický problém" task for the
   project's developer (priority by severity), created on the Head of Customer Success's behalf. Unclear →
   a task for the Head of Customer Success, who decides with knowlage (`support_issue_open`). Anything else
   is routed by the ordinary rules (invoices → CFO, leads → Growth, the rest → Customer Success).
   One task per thread: a follow-up mail in the thread is a comment on it (and reopens a finished one).
3. Fix: the developer's task (its notes carry the flow and the project's shipping path).
4. The reply draft: when the developer hands the fix in (review or done), or when the time box runs out
   (P1: 1 h, else 6 h), the Head of Customer Success gets "Koncept odpovědi: …" and calls
   `gmail_create_draft`: a reply in the customer's thread, from the mailbox that received it, signed as
   David. Never sent (pos.support.gmail cannot send).
5. The owner: one "Čeká na tebe" item per issue when the draft is ready (a task for him, no chat ping).
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

from .. import actors, audit, roles, tasks
from ..core import TZ, Ctx, now_iso
from ..invoices import gapi
from . import PENDING as _PENDING
from . import classify as cls
from . import gmail as gm
from . import match as projects

log = logging.getLogger(__name__)

PERMISSION = "support:draft"  # nobody has this group: the grants tool:gmail_create_draft etc. open the tools
TOOLS = ("gmail_create_draft", "support_issue_open", "support_threads")
SOURCE_ISSUE = "support:issue"
SOURCE_TRIAGE = "support:triage"
SOURCE_DRAFT = "support:draft"
SOURCE_OWNER = "support:owner"
TOPIC = "zakaznici"
TIMEBOX_H = {"P1": 1, "P2": 6, "P3": 6}
MIN_CONFIDENCE = 0.6
BATCH = 20

SCHEMA = """
CREATE TABLE IF NOT EXISTS support_threads (
    id INTEGER PRIMARY KEY,
    account TEXT NOT NULL DEFAULT '',
    thread_id TEXT NOT NULL,
    event_id INTEGER,
    subject TEXT, sender TEXT, customer TEXT, domain TEXT,
    kind TEXT, severity TEXT, language TEXT, confidence REAL, source TEXT,
    project_slug TEXT, project_id INTEGER, developer TEXT,
    classification TEXT,                 -- JSON: the classifier's whole answer
    issue_task_id INTEGER, triage_task_id INTEGER, draft_task_id INTEGER, draft_mode TEXT, needs_task_id INTEGER,
    draft_id TEXT, draft_message_id TEXT, draft_link TEXT,
    messages_seen INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,                -- routed | triage | issue | drafted
    test INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE (account, thread_id)
);
CREATE INDEX IF NOT EXISTS support_threads_issue ON support_threads (issue_task_id);
"""

# How a fix ships, per project (the developer's notes say it; docs/SUPPORT.md).
SHIP = {
    "personalos": "PersonalOS: commit na větvi `agent/dev` (+ regresní test), `request_review`; deployer ji po "
                  "kontrolách (testy, ústava, QA) nasadí; ověř na https://pos.obseum.cz.",
    "knowlage": "knowlage (ObseumEU/knowlage-agent): oprava v submodulu přes jeho existující push/PR mechanismus; "
                "když to není tvoje repo, předej úkol Knowlage Specialist (`handoff_task`) s reprodukcí.",
    "nexus": "Nexus (ObseumEU/nexus-process-pilot): přes jeho existující push/PR mechanismus; jinak předej "
             "Nexus Specialist (`handoff_task`).",
    "kniha": "Kniha (ObseumEU/Kniha, web = roskodav/web-builder-studio, větev `production`): větev `agent/<téma>` v "
             "/work/kniha/web → commit → `npm ci && npx vite build` → review Kniha Lead → `git checkout production "
             "&& git merge --ff-only agent/<téma>`; kniha-deployer to do ~3 min pushne a nasadí (ověř "
             "/work/kniha/.deploy/status.txt a https://rodinne-pribehy.obseum.cz). `git push` nevolej.",
}


class Refused(Exception):
    """Bad input or not set up."""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _cs_ctx(conn: sqlite3.Connection, via: str = "support") -> Ctx:
    row = actors.find_by_name(conn, roles.CUSTOMER_SUCCESS)
    return Ctx(row["id"] if row is not None else actors.owner_id(conn), via=via)


def _row(conn: sqlite3.Connection, account: str | None, thread_id: str) -> sqlite3.Row | None:
    ensure_schema(conn)
    if account:
        row = conn.execute("SELECT * FROM support_threads WHERE account = ? AND thread_id = ?",
                           (account, thread_id)).fetchone()
        if row is not None:
            return row
    return conn.execute("SELECT * FROM support_threads WHERE thread_id = ? ORDER BY id LIMIT 1", (thread_id,)).fetchone()


def _set(conn: sqlite3.Connection, row_id: int, **fields) -> None:
    fields["updated_at"] = now_iso()
    conn.execute(f"UPDATE support_threads SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                 (*fields.values(), row_id))


def _open_task(conn: sqlite3.Connection, task_id: int | None) -> sqlite3.Row | None:
    if not task_id:
        return None
    return conn.execute("SELECT * FROM tasks WHERE id = ? AND archived_at IS NULL", (task_id,)).fetchone()


# ------------------------------------------------------------------ reading the mail

def thread_id_of(event: dict) -> str | None:
    """knowlage's ref is "<channel>:<thread id>"; its url ends with "#all/<thread id>"."""
    m = re.search(r"#(?:all|inbox)/([0-9a-f]{10,})", event.get("url") or "")
    if m:
        return m.group(1)
    ref = event.get("ref") or ""
    tail = ref.rsplit(":", 1)[-1]
    return tail if re.fullmatch(r"[0-9a-f]{10,}", tail) else None


def load_thread(thread_id: str, account: str | None = None, gmail_factory=gapi.Gmail) -> dict:
    """The thread in the mailbox that has it: the last message a customer sent (not us), with the count."""
    boxes = [account.lower()] if account else gapi.mailboxes()
    last = None
    for box in boxes:
        try:
            msgs = gm.thread_headers(box, thread_id, gmail_factory)
        except gapi.GoogleError as e:
            last = e
            continue
        if not msgs:
            continue
        target = gm.reply_target(msgs, box)
        mail = gmail_factory(box).message(target["id"])
        ours = [m for m in msgs if "SENT" in m["labels"]]
        first_ms = msgs[0]["internal_ms"]
        return {**mail, "account": box, "thread_id": thread_id, "count": len(msgs),
                "replied_by_us": [datetime.fromtimestamp(m["internal_ms"] / 1000, timezone.utc).astimezone(TZ)
                                  .strftime("%Y-%m-%d") for m in ours if m["internal_ms"] > first_ms],
                "age_days": (datetime.now(timezone.utc).timestamp() * 1000 - target["internal_ms"]) / 86_400_000,
                "url": f"https://mail.google.com/mail/u/?authuser={box}#all/{thread_id}"}
    raise Refused(f"thread {thread_id} not found in {', '.join(boxes)}: {last}")


def mail_from_event(event: dict) -> dict:
    """What the event itself carries (Gmail unreachable): knowlage's thread text."""
    body = event.get("body") or ""
    author = event.get("author") or ""
    return {"account": "", "thread_id": thread_id_of(event) or (event.get("ref") or ""), "message_id": "",
            "subject": event.get("title") or "", "sender": author, "to": "", "cc": "",
            "body": body, "attachments": [], "labels": [], "count": 0, "replied_by_us": [], "age_days": 0,
            "url": event.get("url") or ""}


def kb_history(mail: dict) -> str:
    """Earlier threads with this customer from knowlage (cheap search), "" when unavailable."""
    from .. import knowledge_tool

    who = cls.domain(mail.get("sender") or "") or cls.sender_name(mail.get("sender") or "")
    try:
        out = knowledge_tool.search(f"{who} {mail.get('subject', '')}"[:300], k=5, effort=1)
    except Exception as e:  # noqa: BLE001 - history is a help, never a blocker
        log.info("support: knowlage history unavailable: %s", e)
        return ""
    return str(out.get("results") or "")[:4000]


def haiku(conn: sqlite3.Connection):
    """The classifier's model: one tool-less claude-haiku-4-5 call as the Head of Customer Success, or None."""
    from .. import integrations, runner

    if not runner.available("claude"):
        return None

    def call(prompt: str) -> str | None:
        integrations.install()
        res = runner.run(conn, runner.RunRequest(_cs_ctx(conn).actor_id, "support_classify", prompt,
                                                 engine="claude", model=cls.MODEL, timeout_s=90))
        return res.output if res.status == "ok" else None

    return call


def classify_mail(conn: sqlite3.Connection, mail: dict, *, model=None, kb=kb_history) -> tuple[dict, dict | None, str]:
    """(classification, project or None, knowlage history)."""
    sure = cls.prefilter(mail, mailboxes=tuple(gapi.mailboxes()))
    if sure is not None:
        return sure, None, ""
    history = kb(mail) if kb else ""
    cands = projects.catalog(conn)
    c = cls.classify(mail, model=model, projects=[p["name"] for p in cands], history=history,
                     mailboxes=tuple(gapi.mailboxes()))
    project = projects.match(conn, mail, c.get("project_hint") or "", history, cands) \
        if c["kind"] in cls.ISSUE_KINDS else None
    return c, project, history


# ------------------------------------------------------------------ the tasks

def _external(mail: dict) -> str:
    from ..guard.external import wrap_external

    return wrap_external("gmail", f"Od: {mail.get('sender', '')}\nKomu: {mail.get('to', '')}\n"
                                  f"Předmět: {mail.get('subject', '')}\n\n{(mail.get('body') or '')[:4000]}",
                         ref=mail.get("url") or None)


def _source_lines(mail: dict) -> str:
    return (f"Schránka **{mail.get('account') or '?'}**, vlákno `{mail.get('thread_id')}`"
            + (f" ([Gmail]({mail['url']}))" if mail.get("url") else "") + ".")


def _staleness(mail: dict) -> str:
    notes = []
    if mail.get("replied_by_us"):
        notes.append(f"Ve vlákně jsme už odpověděli ({', '.join(mail['replied_by_us'][-3:])}).")
    if (mail.get("age_days") or 0) > 3:
        notes.append(f"Poslední zpráva zákazníka je {int(mail['age_days'])} dní stará.")
    if not notes:
        return ""
    return ("### Pozor: starší vlákno\n" + " ".join(notes) + " Nejdřív ověř, že problém ještě trvá (reprodukce, "
            "knowlage, co jsme psali). Bez toho neměň produkční kód; když už je vyřešený, napiš to do výsledku.\n\n")


def issue_notes(mail: dict, c: dict, project: dict | None, developer: str) -> str:
    steps = c.get("repro_steps") or []
    slug = (project or {}).get("slug") or ""
    ship = SHIP.get(slug) or ("Podle projektu (repo a nasazení na projektové stránce; existující push/PR/deploy "
                              "mechanismus repa)." if project else "Projekt není jasný: urči repo z knowlage a "
                              "z vlákna; PersonalOS → `agent/dev` a deployer.")
    return (
        f"### Shrnutí\n{c.get('summary') or mail.get('subject')}\n\n"
        f"- **Zákazník:** {c.get('customer') or cls.customer_of(mail)} ({cls.address(mail.get('sender') or '')})\n"
        f"- **Závažnost:** {c.get('severity')} (P1 výpadek/ztráta dat, P2 rozbitá funkce, P3 drobnost)\n"
        f"- **Typ:** {c.get('kind')} · jazyk {c.get('language')} · klasifikace {c.get('source')} "
        f"({c.get('confidence')})\n"
        f"- **Projekt:** {(project or {}).get('name') or 'neurčen'}"
        + (f" · repo {', '.join(project.get('repos') or [])}" if project and project.get("repos") else "")
        + (f" · shoda: {', '.join(project.get('why') or [])}" if project and project.get("why") else "") + "\n"
        f"- **Dotčená URL / verze:** {c.get('affected_url') or '—'} / {c.get('version') or '—'}\n\n"
        "### Kroky k reprodukci (z e-mailu)\n"
        + ("\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)) if steps else "Mail je neuvádí; odvoď je z textu.")
        + "\n\n" + _staleness(mail) +
        f"### Postup ({developer})\n"
        "1. Reprodukuj: repo, testy, prohlížeč proti živé aplikaci.\n"
        "2. Oprav příčinu a přidej regresní test.\n"
        f"3. Nasaď: {ship}\n"
        "4. Ověř v produkci (prohlížeč / endpoint z mailu).\n"
        "5. Velká nebo riskantní oprava: nasaď mitigaci, nebo popiš plán; nic nevydávej za hotové.\n"
        "6. `complete_task` s výsledkem: příčina, co se opravilo, **commit (hash)**, jak ověřeno v produkci, co má "
        "zákazník udělat/zkontrolovat. Z toho píše Péče o zákazníky koncept odpovědi (nic neslibuje navíc).\n"
        f"Časový rámec: {'hned (P1)' if c.get('severity') == 'P1' else 'do konce dne'}.\n\n"
        f"### Zdroj\n{_source_lines(mail)}\n\n{_external(mail)}")


def _upsert_thread(conn: sqlite3.Connection, mail: dict, c: dict, project: dict | None, *, event_id: int | None,
                   status: str, developer: str | None = None, test: bool = False) -> int:
    ensure_schema(conn)
    row = _row(conn, mail.get("account"), mail["thread_id"])
    fields = {"event_id": event_id, "subject": (mail.get("subject") or "")[:300],
              "sender": (mail.get("sender") or "")[:200], "customer": (c.get("customer") or "")[:200],
              "domain": cls.domain(mail.get("sender") or ""), "kind": c["kind"], "severity": c.get("severity"),
              "language": c.get("language"), "confidence": c.get("confidence"), "source": c.get("source"),
              "project_slug": (project or {}).get("slug"), "project_id": (project or {}).get("id"),
              "developer": developer, "classification": json.dumps(c, ensure_ascii=False)[:8000],
              "messages_seen": int(mail.get("count") or 0), "status": status, "test": int(test)}
    if row is not None:
        _set(conn, row["id"], **{k: v for k, v in fields.items() if v is not None or k in ("project_slug",)})
        return row["id"]
    now = now_iso()
    cur = conn.execute(
        f"INSERT INTO support_threads (account, thread_id, {', '.join(fields)}, created_at, updated_at) "
        f"VALUES (?, ?, {', '.join('?' * len(fields))}, ?, ?)",
        (mail.get("account") or "", mail["thread_id"], *fields.values(), now, now))
    return cur.lastrowid


def open_issue(conn: sqlite3.Connection, ctx: Ctx, mail: dict, c: dict, project: dict | None, *,
               event_id: int | None = None, developer: str | None = None, test: bool = False) -> dict:
    """One "Zákaznický problém" task per thread for the project's developer (a second call: the same task)."""
    ensure_schema(conn)
    row = _row(conn, mail.get("account"), mail["thread_id"])
    existing = _open_task(conn, row["issue_task_id"]) if row is not None else None
    if existing is not None:
        return {**tasks.get(conn, ctx, existing["id"]), "deduplicated": True}
    developer = developer or projects.developer_for(conn, project)
    title = f"Zákaznický problém: {c.get('customer') or cls.customer_of(mail)} – {c.get('summary') or mail.get('subject')}"
    fields = {"title": ("[TEST] " if test else "") + title[:190], "notes": issue_notes(mail, c, project, developer),
              "assignee": developer, "priority": cls.PRIORITY.get(c.get("severity"), 3), "status": "next",
              "topic": TOPIC, "do_date": datetime.now(TZ).date().isoformat(),
              "definition_of_done": "Problém je reprodukovaný, opravený s regresním testem, nasazený a ověřený v "
                                    "produkci (nebo je nasazená mitigace / popsaný plán); výsledek cituje commit a "
                                    "říká, co má zákazník udělat.",
              "source": SOURCE_ISSUE}
    if project and project.get("id"):
        fields["project"] = project["id"]
    task = tasks.create(conn, ctx, fields)
    row_id = _upsert_thread(conn, mail, c, project, event_id=event_id, status="issue", developer=developer, test=test)
    _set(conn, row_id, issue_task_id=task["id"])
    if row is not None and row["triage_task_id"]:
        from .. import comments

        comments.log(conn, ctx, row["triage_task_id"], f"Založen {task['ref']} pro {developer}.", "system")
    audit.log(conn, ctx, "support_issue", "task", task["id"], thread=mail["thread_id"], account=mail.get("account"),
              kind=c["kind"], severity=c.get("severity"), project=(project or {}).get("slug"), developer=developer,
              test=test or None)
    return task


def open_triage(conn: sqlite3.Connection, ctx: Ctx, mail: dict, c: dict, project: dict | None, history: str,
                *, event_id: int | None = None, test: bool = False) -> dict:
    """Unclear (low confidence or no project): the Head of Customer Success decides with knowlage."""
    ensure_schema(conn)
    row = _row(conn, mail.get("account"), mail["thread_id"])
    existing = _open_task(conn, row["triage_task_id"]) if row is not None else None
    if existing is not None:
        return {**tasks.get(conn, ctx, existing["id"]), "deduplicated": True}
    why = []
    if (c.get("confidence") or 0) < MIN_CONFIDENCE:
        why.append(f"nejistá klasifikace ({c.get('source')}, {c.get('confidence')})")
    if project is None:
        why.append("projekt se nepodařilo určit")
    notes = (
        f"### Co udělat\nZ mailu to vypadá na **{c['kind']}** ({c.get('severity')}), ale {' a '.join(why)}. "
        "Rozhodni s knowlage (`knowledge`, earlier threads with the customer): je to zákaznický problém, a kterého "
        "projektu? Pak `support_issue_open(thread_id, account, summary, severity, project, …)` založí úkol pro "
        "vývojáře projektu (jeden na vlákno). Není to problém → vyřiď to jako běžný mail (odpověď, úkol, nic) a "
        "napiš proč.\n\n"
        f"### Klasifikace\n```json\n{json.dumps(c, ensure_ascii=False, indent=1)[:2500]}\n```\n\n"
        + (f"### Knowlage (historie se zákazníkem)\n{history[:1500]}\n\n" if history else "")
        + _staleness(mail) + f"### Zdroj\n{_source_lines(mail)}\n\n{_external(mail)}")
    task = tasks.create(conn, ctx, {
        "title": (("[TEST] " if test else "") + f"Zákaznický problém? {c.get('customer') or ''} – "
                  f"{mail.get('subject') or ''}")[:200],
        "notes": notes, "assignee": roles.CUSTOMER_SUCCESS, "priority": cls.PRIORITY.get(c.get("severity"), 3),
        "status": "next", "topic": TOPIC, "source": SOURCE_TRIAGE,
        "definition_of_done": "Rozhodnuto: úkol pro vývojáře (support_issue_open) nebo vyřízeno jako běžný mail, "
                              "s důvodem."})
    row_id = _upsert_thread(conn, mail, c, project, event_id=event_id, status="triage", test=test)
    _set(conn, row_id, triage_task_id=task["id"])
    audit.log(conn, ctx, "support_triage", "task", task["id"], thread=mail["thread_id"], why="; ".join(why))
    return task


def follow_up(conn: sqlite3.Connection, ctx: Ctx, row: sqlite3.Row, mail: dict) -> dict | None:
    """A new message in a thread that has a task: a comment there; a finished task is opened again."""
    from .. import comments

    task = _open_task(conn, row["issue_task_id"]) or _open_task(conn, row["triage_task_id"])
    if task is None:
        return None
    comments.log(conn, ctx, task["id"], f"Nová zpráva ve vlákně od {mail.get('sender', '')}:\n\n{_external(mail)}",
                 "comment")
    if task["status"] in ("done", "review"):
        tasks.update(conn, ctx, task["id"], {"status": "next",
                                             "progress_note": "Zákazník napsal znovu ve stejném vlákně."})
    _set(conn, row["id"], messages_seen=max(int(mail.get("count") or 0), row["messages_seen"]))
    audit.log(conn, ctx, "support_follow_up", "task", task["id"], thread=row["thread_id"])
    return tasks.get(conn, ctx, task["id"])


# ------------------------------------------------------------------ the intake job

def handle(conn: sqlite3.Connection, mail: dict, *, event: dict | None = None, event_id: int | None = None,
           model=None, kb=kb_history, test: bool = False) -> dict:
    """One mail through the intake: follow-up, issue task, triage task, or back to the ordinary routing."""
    from .. import routing

    ctx = _cs_ctx(conn)
    row = _row(conn, mail.get("account"), mail["thread_id"]) if mail.get("thread_id") else None
    if row is not None and (row["issue_task_id"] or row["triage_task_id"]):
        t = follow_up(conn, ctx, row, mail)
        if t is not None:
            if event_id:
                conn.execute("UPDATE events SET task_id = ?, signals = 'support:follow_up' WHERE id = ?",
                             (t["id"], event_id))
            return {"outcome": "follow_up", "task_id": t["id"], "task_ref": t["ref"]}
    c, project, history = classify_mail(conn, mail, model=model, kb=kb)
    if c["kind"] in cls.ISSUE_KINDS:
        if (c.get("confidence") or 0) >= MIN_CONFIDENCE and project is not None:
            t = open_issue(conn, ctx, mail, c, project, event_id=event_id, test=test)
            outcome = "issue"
        else:
            t = open_triage(conn, ctx, mail, c, project, history, event_id=event_id, test=test)
            outcome = "triage"
        if event_id:
            conn.execute("UPDATE events SET task_id = ?, signals = ? WHERE id = ?",
                         (t["id"], f"support:{outcome}", event_id))
        return {"outcome": outcome, "task_id": t["id"], "task_ref": t["ref"], "assignee": t["assignee_name"],
                "classification": c, "project": (project or {}).get("slug")}
    note = f"Příjem zákaznické pošty (pos.support): **{c['kind']}** ({c.get('source')}; {c.get('reason')})."
    if mail.get("thread_id"):
        _upsert_thread(conn, mail, c, None, event_id=event_id, status="routed", test=test)
    if event is None:
        return {"outcome": "routed", "classification": c}
    who = _receiver(conn, event_id) if event_id else ctx.actor_id
    out = routing.route_event(conn, Ctx(who, via="support"), event, event_id=event_id, note=note)
    return {"outcome": "routed", "classification": c, **out}


def _receiver(conn: sqlite3.Connection, event_id: int) -> int:
    row = conn.execute("SELECT received_by FROM events WHERE id = ?", (event_id,)).fetchone()
    return row["received_by"] if row is not None and row["received_by"] else actors.owner_id(conn)


def process_pending(conn: sqlite3.Connection, *, gmail_factory=gapi.Gmail, model="default", kb=kb_history) -> dict:
    """The deferred new-mail events, oldest first. Nothing is lost: a failure routes the mail as before."""
    from .. import routing

    ensure_schema(conn)
    rows = conn.execute("SELECT * FROM events WHERE source = 'gmail' AND signals = ? ORDER BY id LIMIT ?",
                        (_PENDING, BATCH)).fetchall()
    if not rows:
        return {}
    if model == "default":
        model = haiku(conn)
    counts: dict[str, int] = {}
    for r in rows:
        event = json.loads(r["payload"] or "{}")
        try:
            tid = thread_id_of(event)
            mail = None
            if tid and any(gapi.configured()["gmail"].values()):
                try:
                    mail = load_thread(tid, gmail_factory=gmail_factory)
                except (Refused, gapi.GoogleError) as e:
                    log.info("support: thread %s not readable: %s", tid, e)
            mail = mail or mail_from_event(event)
            out = handle(conn, mail, event=event, event_id=r["id"], model=model, kb=kb)
            counts[out["outcome"]] = counts.get(out["outcome"], 0) + 1
            conn.commit()
        except Exception as e:  # noqa: BLE001 - the mail still reaches someone
            conn.rollback()
            log.exception("support: intake of event %s failed", r["id"])
            routing.route_event(conn, Ctx(_receiver(conn, r["id"]), via="support"), event, event_id=r["id"],
                                note=f"Příjem zákaznické pošty selhal ({str(e)[:200]}); běžné směrování.")
            counts["errors"] = counts.get("errors", 0) + 1
            conn.commit()
    return counts


# ------------------------------------------------------------------ the reply draft task

def request_draft(conn: sqlite3.Connection, row: sqlite3.Row, mode: str, result: str = "") -> dict | None:
    """A task for the Head of Customer Success: write the reply draft (mode fixed | status)."""
    if row["draft_task_id"] and (row["draft_mode"] == mode or row["draft_mode"] == "fixed"):
        return None
    ctx = _cs_ctx(conn)
    issue = tasks.display_id(row["issue_task_id"]) if row["issue_task_id"] else "?"
    what = ("Vývojář předal opravu" if mode == "fixed" else
            f"Oprava zatím běží déle než {TIMEBOX_H.get(row['severity'] or 'P3', 6)} h")
    notes = (
        f"### Co udělat\n{what} ({issue}). Napiš koncept odpovědi zákazníkovi nástrojem "
        f"`gmail_create_draft(thread_id=\"{row['thread_id']}\", account=\"{row['account']}\", body=…, fixed=…, "
        f"task_id=…)`: v jazyce zákazníka ({row['language'] or 'cs'}), zdvořile a konkrétně: co se stalo, "
        + ("co jsme opravili (commit), co má zkontrolovat nebo udělat, další kroky. "
           if mode == "fixed" else "že na tom pracujeme (bez termínu, který nikdo nedal), co už víme, další krok. ")
        + "Nic neslibuj, co není hotové; opírej se jen o výsledek vývojáře níže. Podpis doplní nástroj (David). "
        "Koncept se **neodesílá**; majitel ho dostane v „Čeká na tebe“.\n\n"
        f"### Výsledek vývojáře ({issue})\n{result or '(zatím žádný)'}\n\n"
        f"### Vlákno\n{row['customer']} · {row['subject']} · schránka {row['account']} · vlákno `{row['thread_id']}`.")
    task = tasks.create(conn, ctx, {
        "title": f"Koncept odpovědi: {row['customer'] or ''} – {row['subject'] or ''}"[:200],
        "notes": notes, "assignee": roles.CUSTOMER_SUCCESS, "status": "next", "topic": TOPIC,
        "priority": cls.PRIORITY.get(row["severity"], 3), "source": SOURCE_DRAFT,
        "definition_of_done": "Koncept odpovědi je v Gmailu ve vlákně zákazníka (gmail_create_draft), neodeslaný."})
    _set(conn, row["id"], draft_task_id=task["id"], draft_mode=mode)
    audit.log(conn, ctx, "support_draft_requested", "task", task["id"], thread=row["thread_id"], mode=mode)
    return task


def on_task_changed(conn: sqlite3.Connection, ctx: Ctx, before: sqlite3.Row, after: dict) -> None:
    """The developer handed the fix in (review) or finished it: the reply draft is next."""
    if (before["source"] or "") != SOURCE_ISSUE:
        return
    if after.get("status") not in ("review", "done") or before["status"] in ("review", "done"):
        return
    ensure_schema(conn)
    row = conn.execute("SELECT * FROM support_threads WHERE issue_task_id = ?", (before["id"],)).fetchone()
    if row is not None:
        request_draft(conn, row, "fixed", after.get("progress_note") or "")


def tick(conn: sqlite3.Connection, *, gmail_factory=gapi.Gmail) -> dict:
    """Time boxes (a "we're on it" draft when the fix takes longer) and follow-ups in open threads."""
    ensure_schema(conn)
    out = {"status_drafts": 0, "follow_ups": 0}
    now = datetime.now(timezone.utc)
    for row in conn.execute("SELECT s.*, t.status AS t_status, t.created_at AS t_created FROM support_threads s "
                            "JOIN tasks t ON t.id = s.issue_task_id WHERE s.draft_task_id IS NULL "
                            "AND t.archived_at IS NULL").fetchall():
        if row["t_status"] in ("review", "done"):
            continue
        age = now - datetime.fromisoformat(row["t_created"])
        if age > timedelta(hours=TIMEBOX_H.get(row["severity"] or "P3", 6)) and request_draft(conn, row, "status"):
            out["status_drafts"] += 1
    conn.commit()
    if not any(gapi.configured()["gmail"].values()):
        return {k: v for k, v in out.items() if v}
    since = (now - timedelta(days=30)).isoformat(timespec="seconds")
    for row in conn.execute("SELECT * FROM support_threads WHERE status IN ('issue', 'triage', 'drafted') "
                            "AND test = 0 AND account != '' AND updated_at >= ?", (since,)).fetchall():
        try:
            msgs = gm.thread_headers(row["account"], row["thread_id"], gmail_factory)
        except gapi.GoogleError:
            continue
        new = [m for m in msgs[row["messages_seen"]:] if "SENT" not in m["labels"] and "DRAFT" not in m["labels"]]
        if row["messages_seen"] and new:
            mail = gmail_factory(row["account"]).message(new[-1]["id"])
            if follow_up(conn, _cs_ctx(conn), row, {**mail, "count": len(msgs), "thread_id": row["thread_id"]}):
                out["follow_ups"] += 1
        elif len(msgs) != row["messages_seen"]:
            _set(conn, row["id"], messages_seen=len(msgs))
        conn.commit()
    return {k: v for k, v in out.items() if v}


def job(conn: sqlite3.Connection) -> dict:
    """The scheduler's `support_intake`: pending mail, time boxes, follow-ups."""
    if not intake_on():
        return {}
    out = process_pending(conn)
    out.update(tick(conn))
    return out


def intake_on() -> bool:
    from . import intake_enabled

    return intake_enabled()


# ------------------------------------------------------------------ the draft and the owner's item

SIGNOFF = {"cs": "S pozdravem", "sk": "S pozdravom", "en": "Best regards,", "de": "Mit freundlichen Grüßen"}


def signature(account: str, language: str | None, project_slug: str | None) -> str:
    """David's sign-off as in his sent mail: the greeting, his name, the company line on the company mailbox."""
    lines = [SIGNOFF.get((language or "cs")[:2], SIGNOFF["en"]), "David Roško"]
    if account.endswith("@obseum.cz"):
        lines += ["Rodinné příběhy" if project_slug == "kniha" else "Obseum s.r.o.", account]
    return "\n".join(lines)


def _with_signature(body: str, sig: str) -> str:
    text = body.rstrip()
    if re.search(r"David Ro[sš]ko\s*$", text[-200:], re.I):
        return text
    return f"{text}\n\n{sig}"


def create_draft(conn: sqlite3.Connection, ctx: Ctx, thread_id: str, account: str, body: str, *,
                 html: str | None = None, fixed: str = "", task_id: int | None = None, test: bool = False,
                 needs_me: bool = True, gmail_factory=gapi.Gmail) -> dict:
    """A reply draft in the customer's thread (never sent), audited; then the owner's one item."""
    ensure_schema(conn)
    account = (account or "").strip().lower()
    if account not in gapi.mailboxes():
        raise Refused(f"unknown mailbox {account!r}; known: {', '.join(gapi.mailboxes())}")
    if not os.environ.get(gm.compose_env(account)):
        raise Refused(f"drafts are not set up for {account} ({gm.compose_env(account)} missing; docs/SUPPORT.md)")
    body = (body or "").strip()
    if len(body) < 20:
        raise Refused("the body is too short: what happened, what we fixed, what they should check, next steps")
    row = _row(conn, account, thread_id)
    conn.commit()  # no write transaction is held while Google answers
    msgs = gm.thread_headers(account, thread_id, gmail_factory)
    lang = (row["language"] if row is not None else None) or cls.language(body)
    text = _with_signature(body, signature(account, lang, row["project_slug"] if row is not None else None))
    msg = gm.build_reply(msgs, account, text, html, test=test)
    made = gm.create_draft(account, thread_id, msg)
    message_id = (made.get("message") or {}).get("id") or ""
    link = gm.draft_link(account, message_id)
    if row is None:
        mail = {"account": account, "thread_id": thread_id, "subject": msgs[-1]["headers"].get("subject", ""),
                "sender": gm.reply_target(msgs, account)["headers"].get("from", ""), "count": len(msgs), "body": ""}
        rid = _upsert_thread(conn, mail, {"kind": "support_issue", "customer": cls.customer_of(mail), "source": "manual",
                                          "language": lang}, None, event_id=None, status="drafted", test=test)
        row = conn.execute("SELECT * FROM support_threads WHERE id = ?", (rid,)).fetchone()
    _set(conn, row["id"], draft_id=made.get("id"), draft_message_id=message_id, draft_link=link, status="drafted",
         messages_seen=max(row["messages_seen"], len(msgs)))
    audit.log(conn, ctx, "gmail_create_draft", "support_thread", row["id"], account=account, thread=thread_id,
              draft_id=made.get("id"), to=msg["To"], cc=msg["Cc"], subject=msg["Subject"],
              in_reply_to=msg["In-Reply-To"], body_sha256=hashlib.sha256(text.encode()).hexdigest(),
              task=task_id, test=test or None)
    out = {"draft_id": made.get("id"), "message_id": message_id, "link": link, "to": msg["To"], "cc": msg["Cc"],
           "subject": msg["Subject"], "in_reply_to": msg["In-Reply-To"], "sent": False}
    if needs_me:
        item = owner_item(conn, row["id"], fixed or row["subject"] or "", link, test=test)
        out["owner_item"] = item["ref"]
    conn.commit()
    return out


def owner_item(conn: sqlite3.Connection, row_id: int, fixed: str, link: str, *, test: bool = False) -> dict:
    """One "Čeká na tebe" item per issue: a task for the owner (no chat ping); a new draft updates it."""
    row = conn.execute("SELECT * FROM support_threads WHERE id = ?", (row_id,)).fetchone()
    ctx = _cs_ctx(conn)
    customer = row["customer"] or row["domain"] or "zákazníka"
    title = (("[TEST] " if test else "") + f"Koncept odpovědi pro {customer} je v Gmailu: {' '.join(fixed.split())}")[:200]
    refs = [f"- Koncept v Gmailu (neodeslaný): {link}"]
    if row["issue_task_id"]:
        refs.append(f"- Úkol s opravou: {tasks.display_id(row['issue_task_id'])} (/tasks?task="
                    f"{tasks.display_id(row['issue_task_id'])})")
    notes = ("### Co udělat\nZkontroluj koncept odpovědi v Gmailu a odešli ho (nebo uprav). Nic se neodeslalo.\n\n"
             f"### Odkazy\n" + "\n".join(refs) + "\n\n"
             f"### Vlákno\n{row['customer']} · {row['subject']} · schránka {row['account']}.")
    existing = _open_task(conn, row["needs_task_id"])
    if existing is not None and existing["status"] not in ("done", "review"):
        tasks.update(conn, ctx, existing["id"], {"title": title, "notes": notes})
        return tasks.get(conn, ctx, existing["id"])
    owner = actors.owner_id(conn)
    task = tasks.create(conn, ctx, {
        "title": title, "notes": notes, "status": "next", "assignee": {"type": "human", "id": owner},
        "priority": cls.PRIORITY.get(row["severity"], 3), "topic": TOPIC, "source": SOURCE_OWNER,
        "definition_of_done": "Majitel koncept odeslal, upravil nebo zahodil."})
    _set(conn, row_id, needs_task_id=task["id"])
    audit.log(conn, ctx, "support_owner_item", "task", task["id"], thread=row["thread_id"])
    return task


def threads(conn: sqlite3.Connection, days: int = 14) -> list[dict]:
    ensure_schema(conn)
    since = (datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))).isoformat(timespec="seconds")
    rows = conn.execute("SELECT id, account, thread_id, subject, customer, kind, severity, language, project_slug, "
                        "developer, status, issue_task_id, triage_task_id, draft_task_id, needs_task_id, draft_link, "
                        "test, updated_at FROM support_threads WHERE updated_at >= ? ORDER BY id DESC LIMIT 200",
                        (since,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("issue_task_id", "triage_task_id", "draft_task_id", "needs_task_id"):
            if d[k]:
                d[k.replace("_id", "")] = tasks.display_id(d[k])
        out.append(d)
    return out


# ------------------------------------------------------------------ MCP tools (the Head of Customer Success)

def register_mcp(mcp, session) -> None:
    from mcp.server.mcpserver import Context
    from mcp.server.mcpserver.exceptions import ToolError

    from .. import mcp_server

    for tool in TOOLS:
        mcp_server.TOOL_PERMISSIONS.setdefault(tool, PERMISSION)

    @mcp.tool(name="gmail_create_draft", description=(
        "Customer Success: a reply DRAFT in the customer's Gmail thread (never sent): from `account` (the mailbox "
        "that received it), In-Reply-To/References set, plain text + simple HTML, David's signature added. body: "
        "the reply without a signature, in the customer's language: what happened, what we fixed (cite the commit "
        "only if the developer's result names it), what they should check or do, next steps; never promise what "
        "is not done. fixed: a few Czech words for the owner's item (\"oprava exportu CSV\"). The owner gets one "
        "'Čeká na tebe' item with the link. Audited."))
    def gmail_create_draft(ctx: Context, thread_id: str, account: str, body: str, fixed: str = "",
                           html: str | None = None, task_id: str | None = None) -> dict:
        with session(ctx, "gmail_create_draft", thread_id=thread_id, account=account, fixed=fixed[:120],
                     task_id=task_id) as (conn, c):
            try:
                tid = tasks.parse_id(task_id) if task_id else None
                return create_draft(conn, Ctx(c.actor_id, via="mcp"), thread_id, account, body, html=html,
                                    fixed=fixed, task_id=tid)
            except (Refused, gm.Refused, gapi.GoogleError) as e:
                conn.rollback()
                raise ToolError(f"gmail_create_draft: {e}") from e

    @mcp.tool(name="support_issue_open", description=(
        "Customer Success: open the one 'Zákaznický problém' task of a customer's mail thread for the project's "
        "developer (the project team's developer, else the Software Engineer), priority by severity (P1 outage/"
        "data loss, P2 broken feature, P3 minor). project: a project slug or name ('' when none fits: Software "
        "Engineer). A thread that already has one returns it (dedup)."))
    def support_issue_open(ctx: Context, thread_id: str, account: str, summary: str, severity: str = "P2",
                           project: str = "", customer: str = "", repro_steps: list[str] | None = None,
                           affected_url: str = "", language: str = "") -> dict:
        with session(ctx, "support_issue_open", thread_id=thread_id, account=account, severity=severity,
                     project=project) as (conn, c):
            try:
                try:
                    mail = load_thread(thread_id, account)
                except (Refused, gapi.GoogleError):
                    mail = {"account": account, "thread_id": thread_id, "subject": summary, "sender": customer,
                            "body": "", "count": 0, "url": ""}
                cand = None
                if project:
                    cand = next((p for p in projects.catalog(conn) if project.lower() in
                                 (p["slug"].lower(), p["name"].lower())), None)
                    if cand is None:
                        raise Refused(f"no project {project!r}; known: "
                                      + ", ".join(p["slug"] for p in projects.catalog(conn)))
                sev = severity.upper() if severity.upper() in cls.SEVERITIES else "P2"
                cl = {"kind": "support_issue", "summary": summary, "severity": sev, "customer": customer or
                      cls.customer_of(mail), "language": language or cls.language(mail.get("body") or summary),
                      "repro_steps": repro_steps or [], "affected_url": affected_url, "version": "",
                      "source": "customer_success", "confidence": 1.0, "reason": "rozhodla Péče o zákazníky"}
                t = open_issue(conn, Ctx(c.actor_id, via="mcp"), mail, cl, cand)
                return {k: t.get(k) for k in ("ref", "title", "assignee_name", "priority", "deduplicated") if k in t}
            except Refused as e:
                conn.rollback()
                raise ToolError(f"support_issue_open: {e}") from e

    @mcp.tool(name="support_threads", description=(
        "Customer Success: customer threads of the last `days` days in the pipeline: kind, severity, project, "
        "developer, the issue / triage / draft / owner tasks, the draft link."))
    def support_threads(ctx: Context, days: int = 14) -> list[dict]:
        with session(ctx, "support_threads", days=days) as (conn, c):
            return threads(conn, days)


# ------------------------------------------------------------------ command line (server side)

def main(argv: list[str] | None = None) -> int:
    """python -m pos.support {status|classify|run|draft|delete-test-draft}: the operator's checks and dry runs."""
    from ..config import get_settings
    from ..db import connect

    p = argparse.ArgumentParser(prog="pos.support")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    c = sub.add_parser("classify", help="classify a thread (no task, no draft)")
    c.add_argument("account")
    c.add_argument("thread_id")
    r = sub.add_parser("run", help="a thread through the intake (the task); --test marks it [TEST]")
    r.add_argument("account")
    r.add_argument("thread_id")
    r.add_argument("--test", action="store_true")
    d = sub.add_parser("draft", help="a reply draft in a thread (body from a file); --test marks it [TEST]")
    d.add_argument("account")
    d.add_argument("thread_id")
    d.add_argument("body_file")
    d.add_argument("--fixed", default="")
    d.add_argument("--test", action="store_true")
    d.add_argument("--no-owner-item", action="store_true")
    x = sub.add_parser("delete-test-draft", help="delete a [TEST] draft (only those)")
    x.add_argument("account")
    x.add_argument("draft_id")
    a = p.parse_args(argv)
    conn = connect(get_settings().db_path)
    try:
        if a.cmd == "status":
            print(json.dumps({"intake": intake_on(), "gmail_read": gapi.configured()["gmail"],
                              "gmail_compose": gm.compose_configured(), "threads": threads(conn, 14)[:20]},
                             ensure_ascii=False, indent=1))
        elif a.cmd == "classify":
            mail = load_thread(a.thread_id, a.account)
            cl, proj, _ = classify_mail(conn, mail, model=haiku(conn))
            print(json.dumps({"classification": cl, "project": proj and {k: proj[k] for k in ("slug", "score", "why")},
                              "developer": projects.developer_for(conn, proj)}, ensure_ascii=False, indent=1))
        elif a.cmd == "run":
            mail = load_thread(a.thread_id, a.account)
            out = handle(conn, mail, model=haiku(conn), test=a.test)
            conn.commit()
            print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
        elif a.cmd == "draft":
            with open(a.body_file, encoding="utf-8") as f:
                body = f.read()
            out = create_draft(conn, _cs_ctx(conn, "operator"), a.thread_id, a.account, body, fixed=a.fixed,
                               test=a.test, needs_me=not a.no_owner_item)
            print(json.dumps(out, ensure_ascii=False, indent=1))
        elif a.cmd == "delete-test-draft":
            out = gm.delete_test_draft(a.account, a.draft_id)
            audit.log(conn, _cs_ctx(conn, "operator"), "gmail_delete_test_draft", None, None, **out)
            conn.commit()
            print(json.dumps(out, ensure_ascii=False))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
