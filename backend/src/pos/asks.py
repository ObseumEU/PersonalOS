"""Asking the owner: one ticket plus one chat ping, never half of it.

When an agent needs a decision, a confirmation, input or an approval from the
owner it calls `ask` (MCP `ask_owner`). That:

1. creates a task assigned to the owner with a readable Markdown description
   (what is needed, why, the context, the options with the agent's
   recommendation, what happens after the answer) and a definition of done,
   linked to the agent's own task (a comment there, the ref in the notes);
2. posts a short Czech message in #team that @mentions the owner and names
   the ticket (T-123 attaches it);
3. if the agent is blocked, puts its own task in `waiting`.

The same ask (same agent, same task, same topic) is not sent twice: the
existing ticket comes back instead. When the owner comments on the ticket or
finishes it, the asking agent gets it in its inbox (a DM with the ticket
attached) and a blocked task goes back to `next`, so its worker resumes it.

Approvals (pos.approvals) keep their own queue; `ping_approval` gives them
the same chat ping.

A decision with `options` and a `recommendation` is one card in "Čeká na tebe"
with a button per option (`choose`). If the owner does not answer within
`default_after_hours` (72 by default), the recommendation is adopted by itself
(`adopt_defaults`, a scheduler job) and he is told in his next morning brief
(`default_digest`).

The owner's chat message answers only the ask it replies to (a reply to, or a
quote of, the question; the thread of the #team ping), or the single open ask in
that DM when the question is at most ANSWER_WINDOW_MIN old. Anything else is new
input, not an answer (prod 2026-10: #1454 and #1787 closed questions they did
not answer, and he had to send them again).

The bookkeeping lives in `owner_asks`, created on first use (no numbered
migration, so it cannot collide with one added elsewhere).
"""

import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone

from . import actors, audit
from .core import Ctx, now_iso

KINDS = {
    # kind: (label in the ticket, what the chat asks the owner to do); the owner reads both: Czech
    "decision": ("Rozhodnutí", "rozhodni"),
    "confirmation": ("Potvrzení", "potvrď"),
    "input": ("Podklady", "doplň, co potřebuju"),
    "approval": ("Schválení", "schval, nebo zamítni"),
}
TEAM = "team"
DEFAULT_AFTER_HOURS = 72  # an unanswered decision with a recommendation takes it after this long
ANSWER_WINDOW_MIN = 30  # the owner's next DM message answers the one open question asked this recently

_SCHEMA = """CREATE TABLE IF NOT EXISTS owner_asks (
    id             INTEGER PRIMARY KEY,
    ticket_id      INTEGER NOT NULL REFERENCES tasks(id),
    asker_id       INTEGER NOT NULL REFERENCES actors(id),
    source_task_id INTEGER REFERENCES tasks(id),
    topic_key      TEXT NOT NULL,
    kind           TEXT NOT NULL,
    blocking       INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'open',
    message_id     INTEGER,
    created_at     TEXT NOT NULL,
    answered_at    TEXT
)"""


# Added later (the decision card): ALTERed in on first use, like the table itself.
_COLUMNS = {
    "options": "TEXT",              # JSON list of the choices
    "recommendation": "TEXT",       # the asker's advice (one of the options, or free text)
    "default_at": "TEXT",           # when the recommendation is adopted without an answer
    "decided_option": "TEXT",       # what was decided (a button, or the recommendation by default)
    "decided_by": "TEXT",           # owner | default
    "digested_at": "TEXT",          # a default decision the owner was told about (morning brief)
}


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_SCHEMA)
    conn.execute("CREATE INDEX IF NOT EXISTS owner_asks_ticket ON owner_asks (ticket_id)")
    have = {r[1] for r in conn.execute("PRAGMA table_info(owner_asks)")}
    for col, typ in _COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE owner_asks ADD COLUMN {col} {typ}")


def _has_table(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'owner_asks'").fetchone() is not None


def topic_key(text: str) -> str:
    """What makes two asks 'the same': lower case, no accents or punctuation."""
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", t).strip()[:120]


def vocative(name: str) -> str:
    """A friendly Czech address for the owner's first name ('David' -> 'Davide')."""
    first = (name or "").split()[0] if (name or "").strip() else ""
    if not first or first.lower() in ("owner", "me"):
        return "Ahoj"
    low = first.lower()
    if low.endswith("a"):
        return first[:-1] + "o"
    if low.endswith(("š", "ž", "č", "ř", "c", "j", "x")):
        return first + "i"
    if low.endswith("k") and not low.endswith(("ek",)):
        return first + "u"
    if low.endswith("ek"):
        return first[:-2] + "ku"
    if re.search(r"[^aeiouy]r$", low):
        return first[:-1] + "ře"
    if low.endswith("el"):
        return first[:-2] + "le"
    if low[-1] in "bdfglmnpstvz":
        return first + "e"
    return first


def _sentence(text: str, limit: int = 220) -> str:
    """The first sentence of `text`, trimmed, without a closing period."""
    t = " ".join((text or "").split())
    m = re.match(r"(.+?[.!?])(\s|$)", t)
    t = m.group(1) if m else t
    if len(t) > limit:
        t = t[: limit - 1].rstrip() + "…"
    return t.rstrip(".")


def _notes(*, asker: str, kind: str, title: str, why: str, details: str, options: list[str],
           recommendation: str, blocking: bool, source: dict | None, links: list[str], after: str,
           owner: str) -> str:
    label = KINDS[kind][0]
    src = f"{source['ref']} “{source['title']}”" if source else "bez úkolu"
    lines = [
        f"**Ptá se:** {asker} · **K úkolu:** {src} · **Potřebuje:** {label.lower()}"
        + (" · **Blokuje:** agent čeká na tvou odpověď" if blocking else " · neblokuje"),
        "",
        "### Co potřebuju",
        title.strip(),
        "",
        "### Proč",
        why.strip(),
    ]
    ctx_lines = [details.strip()] if details.strip() else []
    if source:
        ctx_lines.append(f"- **Zdrojový úkol:** {source['ref']} — {source['title']}")
    for link in links:
        ctx_lines.append(f"- {link}")
    if ctx_lines:
        lines += ["", "### Souvislosti", *ctx_lines]
    if options:
        lines += ["", "### Možnosti"]
        rec = topic_key(recommendation)
        for i, o in enumerate(options, 1):
            mark = " — *doporučuju*" if rec and topic_key(o) == rec else ""
            lines.append(f"{i}. {o.strip()}{mark}")
    if recommendation.strip():
        lines += ["", "### Moje doporučení", recommendation.strip()]
    lines += ["", "### Až odpovíš",
              after.strip() or (
                  f"Odpověz komentářem (nebo ticket dokonči). {asker} to dostane do schránky"
                  + (f" a pokračuje na {source['ref']}." if source else " a pokračuje.")
                  + (" Do té doby ten úkol čeká." if blocking and source else ""))]
    return "\n".join(lines)


def _chat_body(*, owner: str, ref: str, title: str, why: str, kind: str, blocking: bool,
               recommendation: str) -> str:
    verb = KINDS[kind][1]
    body = (f"{vocative(owner)}, tady je ticket {ref}: {title.strip()}. Prosím {verb} — potřebuju to, "
            f"protože {_sentence(why)}.")
    if recommendation.strip():
        body += f" Doporučuju: {_sentence(recommendation, 160)}."
    if blocking:
        body += " Do té doby na tom stojím."
    return f"{body} @{owner}"


def _post(conn: sqlite3.Connection, ctx: Ctx, body: str) -> int | None:
    """A system post in #team by the asking member (no rate limit or budget
    gate: reaching the owner must not fail on them)."""
    from . import chat

    cid = chat.ensure_team_channel(conn)
    return chat.send(conn, ctx, cid, body, system=True)["id"]


def _existing(conn: sqlite3.Connection, asker_id: int, source_id: int | None, key: str) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT a.* FROM owner_asks a JOIN tasks t ON t.id = a.ticket_id
           WHERE a.asker_id = ? AND a.source_task_id IS ? AND a.topic_key = ? AND t.archived_at IS NULL
           ORDER BY a.id DESC LIMIT 1""", (asker_id, source_id, key)).fetchone()


def ask(conn: sqlite3.Connection, ctx: Ctx, *, title: str, why: str, details: str = "",
        options: list[str] | None = None, recommendation: str = "", blocking: bool = True,
        task_id: int | None = None, kind: str = "decision", topic: str | None = None,
        links: list[str] | None = None, after: str = "", priority: int | None = None,
        chat_message_id: int | None = None, default_after_hours: int | None = DEFAULT_AFTER_HOURS) -> dict:
    """Create the owner's ticket, ping the owner in #team and (when blocking)
    park the asking task. Returns {ticket, ref, message_id, deduped, blocking}.
    chat_message_id: the question is already a chat message to the owner (from_chat); it is
    the ticket's ping, so no second one is posted.
    options + recommendation make it a decision card (buttons in "Čeká na tebe"); without an
    answer in default_after_hours (72; 0 or None: never) the recommendation is adopted."""
    from . import comments, tasks, versioning

    title, why = (title or "").strip(), (why or "").strip()
    if not title:
        raise tasks.Invalid("ask_owner needs a title: what you need from the owner")
    if not why:
        raise tasks.Invalid("ask_owner needs why: the reason you need it, in a sentence")
    if kind not in KINDS:
        raise tasks.Invalid(f"kind must be one of {sorted(KINDS)}")
    ensure_schema(conn)
    me = actors.get(conn, ctx.actor_id)
    owner = actors.get(conn, actors.owner_id(conn))
    source = tasks.get(conn, ctx, task_id) if task_id else None
    if chat_message_id is None:  # (a question from chat was checked before it was posted)
        from . import grounding

        # Grounded: a blocker claim is checked, content the owner is asked to send or approve too.
        text = "\n".join(x for x in (title, why, details or "", *[str(x) for x in (links or [])]) if x)
        grounding.owner_message_gate(conn, ctx, "ask_owner", text, task_id=task_id,
                                     asks_to_send=grounding.asks_to_send(title, kind, details or ""))
    key = topic_key(topic or title)
    if not key:
        raise tasks.Invalid("give a topic or a title with words in it")

    prev = _existing(conn, ctx.actor_id, task_id, key)
    if prev is not None:
        t = conn.execute("SELECT id, title, status FROM tasks WHERE id = ?", (prev["ticket_id"],)).fetchone()
        audit.log(conn, ctx, "ask_owner:dedup", "task", prev["ticket_id"], topic=key)
        return {"ref": tasks.display_id(t["id"]), "ticket_id": t["id"], "title": t["title"],
                "status": t["status"], "ask_status": prev["status"], "deduped": True,
                "blocking": bool(prev["blocking"]),
                "note": ("You already asked this; it is still open. Wait for the answer (it comes to your "
                         "inbox) or add to the ticket with task_comment." if prev["status"] == "open" else
                         "You already asked this and it was answered: read the ticket (get_task) and its "
                         "comments.")}

    opts = [str(o).strip() for o in (options or []) if str(o).strip()][:6]
    rec = _match_option(opts, recommendation or "")
    hours = int(default_after_hours or 0) if opts and rec else 0
    if hours:
        after = after or (f"Vyber jednu z možností (tlačítko v „Čeká na tebe“) nebo odpověz komentářem. Když do "
                          f"{hours} h neodpovíš, platí moje doporučení „{rec}“ a dozvíš se to v ranním přehledu.")
    notes = _notes(asker=me["name"], kind=kind, title=title, why=why, details=details or "",
                   options=opts, recommendation=recommendation or "",
                   blocking=blocking, source=source, links=[str(x) for x in (links or []) if str(x).strip()],
                   after=after or "", owner=owner["name"])
    fields = {
        "title": title[:200], "notes": notes, "status": "next",
        "priority": priority if priority in (1, 2, 3) else (1 if blocking else 2),
        "assignee": {"type": "human", "id": owner["id"]},
        "definition_of_done": (f"{owner['name']} odpověděl ({KINDS[kind][0].lower()}) komentářem nebo "
                               f"dokončením ticketu; {me['name']} to má ve schránce"
                               + (f" a {source['ref']} pokračuje." if source else ".")),
        "source": "ask_owner",
    }
    if source:
        fields["visibility"] = source["visibility"]
        if source.get("topic"):
            fields["topic"] = source["topic"]
    ticket = tasks.create(conn, ctx, fields)
    default_at = ((datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds")
                  if hours else None)
    conn.execute(
        """INSERT INTO owner_asks (ticket_id, asker_id, source_task_id, topic_key, kind, blocking, created_at,
                                   options, recommendation, default_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (ticket["id"], ctx.actor_id, task_id, key, kind, int(bool(blocking)), now_iso(),
         json.dumps(opts, ensure_ascii=False) if opts else None, rec or (recommendation or "").strip() or None,
         default_at))
    ask_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    if source:
        comments.log(conn, ctx, source["id"], f"Asked {owner['name']}: {ticket['ref']} — {title}"
                     + (" (waiting for the answer)" if blocking else ""), "system")
        if blocking and source["status"] not in ("done", "waiting"):
            versioning.update(conn, ctx, tasks.ENTITY, source["id"], {
                "status": "waiting", "progress_note": f"Waiting for {owner['name']}: {ticket['ref']}"[:500]},
                action="wait")
    audit.log(conn, ctx, "ask_owner", "task", ticket["id"], source=task_id, topic=key, blocking=blocking,
              chat_message=chat_message_id)
    if chat_message_id is not None:
        mid = chat_message_id  # the owner already has the question in chat
    else:
        # chat.send commits: the ticket, the bookkeeping and the ping land together.
        body = _chat_body(owner=owner["name"], ref=ticket["ref"], title=title, why=why, kind=kind,
                          blocking=blocking and source is not None, recommendation=recommendation or "")
        if hours:
            body = body.replace(f" @{owner['name']}", f" Bez odpovědi do {hours} h platí doporučení. @{owner['name']}")
        mid = _post(conn, ctx, body)
    conn.execute("UPDATE owner_asks SET message_id = ? WHERE id = ?", (mid, ask_id))
    return {"ref": ticket["ref"], "ticket_id": ticket["id"], "title": ticket["title"], "status": ticket["status"],
            "message_id": mid, "deduped": False, "blocking": bool(blocking), "default_at": default_at,
            "note": ("Your task waits for the answer; you will get it in your inbox and the task comes back "
                     "to your queue. Finish this run now with a short summary." if blocking and source else
                     "The answer comes to your inbox.")}


# ------------------------------------------------------------------ answers

def _asks_for(conn: sqlite3.Connection, ticket_id: int) -> list[sqlite3.Row]:
    if not _has_table(conn):
        return []
    ensure_schema(conn)
    return conn.execute("SELECT * FROM owner_asks WHERE ticket_id = ?", (ticket_id,)).fetchall()


def _resume(conn: sqlite3.Connection, ctx: Ctx, a: sqlite3.Row, why: str) -> None:
    from . import tasks, versioning

    if not a["blocking"] or not a["source_task_id"]:
        return
    src = conn.execute("SELECT status FROM tasks WHERE id = ?", (a["source_task_id"],)).fetchone()
    if src and src["status"] == "waiting":
        versioning.update(conn, ctx, tasks.ENTITY, a["source_task_id"],
                          {"status": "next", "progress_note": why[:500]}, action="resume")
        # The answer is what it waited for: an old back-off (a failed run, T-167) must not hold it a day.
        conn.execute("UPDATE tasks SET retry_after = NULL WHERE id = ?", (a["source_task_id"],))


def _tell(conn: sqlite3.Connection, ctx: Ctx, a: sqlite3.Row, body: str) -> None:
    """The asker hears the answer from PersonalOS: a DM "from the owner" that he did not write
    (prod #1454, #1787: "Owner resolved your ask …" in his own DM) is never sent."""
    from . import chat, notices, wake

    asker = actors.get(conn, a["asker_id"])
    if asker["archived_at"] or a["asker_id"] == ctx.actor_id:
        return
    try:
        chat.send_dm(conn, notices.system_ctx(conn), a["asker_id"], body[:3900], priority="change_plan",
                     attachments=[{"type": "task", "id": a["ticket_id"]}], system=True)
    except Exception:  # noqa: BLE001 - the answer is on the ticket either way
        return
    if asker["kind"] != "human":
        wake.wake(a["asker_id"])


def on_comment(conn: sqlite3.Connection, ctx: Ctx, task: sqlite3.Row, body: str) -> None:
    """Someone (the owner) commented on an ask ticket: the asker hears it and
    a blocked task resumes."""
    from . import chat, tasks

    if actors.get(conn, ctx.actor_id)["kind"] != "human":
        return  # an answer comes from a person, not from another agent's remark
    for a in _asks_for(conn, task["id"]):
        if a["asker_id"] == ctx.actor_id:
            continue
        who = actors.get(conn, ctx.actor_id)["name"]
        ref = tasks.display_id(task["id"])
        src = f" Continue {tasks.display_id(a['source_task_id'])}." if a["source_task_id"] else ""
        if a["asker_id"] not in chat._mentions(conn, body, None):  # a mention already sent the DM
            _tell(conn, ctx, a, f"{who} replied on your ask {ref} '{task['title']}': {body[:3000]}{src}")
        _resume(conn, ctx, a, f"{who} replied on {ref}")
        audit.log(conn, ctx, "ask_owner:reply", "task", task["id"], asker=a["asker_id"])


def on_task_changed(conn: sqlite3.Connection, ctx: Ctx, before: sqlite3.Row, after: dict) -> None:
    """An ask ticket was finished: mark it answered, tell the asker, resume."""
    from . import tasks

    if after.get("status") != "done" or before["status"] == "done":
        return
    for a in _asks_for(conn, before["id"]):
        if a["status"] != "open":
            continue
        conn.execute("UPDATE owner_asks SET status = 'answered', answered_at = ? WHERE id = ?", (now_iso(), a["id"]))
        if a["decided_by"] == "default":
            who_ = f"Bez odpovědi Ownera platí doporučení „{a['decided_option']}“"
            src = f" Continue {tasks.display_id(a['source_task_id'])}." if a["source_task_id"] else ""
            _tell(conn, ctx, a, f"{who_} (ask {tasks.display_id(before['id'])} '{before['title']}'). "
                                f"Go ahead with it; the owner is told in his morning brief and may still change it.{src}")
            _resume(conn, ctx, a, f"default adopted on {tasks.display_id(before['id'])}")
            audit.log(conn, ctx, "ask_owner:default", "task", before["id"], asker=a["asker_id"],
                      option=a["decided_option"])
            continue
        who = actors.get(conn, ctx.actor_id)["name"]
        ref = tasks.display_id(before["id"])
        last = conn.execute(
            """SELECT body FROM task_comments WHERE task_id = ? AND kind = 'comment' AND author_id != ?
               AND archived_at IS NULL ORDER BY id DESC LIMIT 1""", (before["id"], a["asker_id"])).fetchone()
        answer = after.get("progress_note") or (last["body"] if last else "")
        src = f" Continue {tasks.display_id(a['source_task_id'])}." if a["source_task_id"] else ""
        _tell(conn, ctx, a, f"{who} resolved your ask {ref} '{before['title']}'"
                            + (f": {answer[:3000]}" if answer else " (no comment; read the ticket).") + src)
        _resume(conn, ctx, a, f"{who} resolved {ref}")
        audit.log(conn, ctx, "ask_owner:answered", "task", before["id"], asker=a["asker_id"])


# ------------------------------------------------------------------ a blocking question in chat

def message_link(channel_id: int, message_id: int) -> str:
    return f"/chat?c={channel_id}&m={message_id}"


def current_task(conn: sqlite3.Connection, actor_id: int, run_id: int | None = None) -> int | None:
    """The task the agent works on now: its run's task, else its live run's (pos.workers.working_on)."""
    if run_id:
        r = conn.execute("SELECT task_id FROM runs WHERE id = ?", (run_id,)).fetchone()
        if r is not None and r["task_id"]:
            return r["task_id"]
    from .workers import working_on

    return (working_on(conn, actor_id).get(actor_id) or {}).get("task_id")


def from_chat(conn: sqlite3.Connection, ctx: Ctx, message: dict, task_id: int | None = None) -> dict | None:
    """An agent asked the owner a question in chat and its task cannot go on without the answer
    (chat_send blocking=true): the same bookkeeping as ask_owner, without a second ping. The task
    goes to `waiting`, the question shows in the owner's "Čeká na tebe" as an ask with the message
    link, and the owner's reply in that DM (or thread) answers it. None when the message does not
    reach the owner."""
    owner = actors.owner_id(conn)
    if owner not in (message.get("inbox") or []) or actors.get(conn, ctx.actor_id)["kind"] == "human":
        return None
    task_id = task_id or current_task(conn, ctx.actor_id, ctx.run_id)
    row = conn.execute("SELECT id, channel_id, body FROM chat_messages WHERE id = ?", (message["id"],)).fetchone()
    body = " ".join((row["body"] or "").split())
    title = _sentence(body.replace(f"@{actors.get(conn, owner)['name']}", "").strip(), 160) or "Odpověz v chatu"
    link = message_link(row["channel_id"], row["id"])
    return ask(conn, ctx, title=title, why="agent čeká na tvou odpověď v chatu, bez ní úkol nepokračuje",
               details=f"Otázka v chatu (zpráva {message['id']}): {body[:1500]}", kind="input",
               blocking=True, task_id=task_id, topic=f"chat {message['id']}", links=[link],
               after="Odpověz přímo v chatu (v DM nebo ve vlákně): ticket se tím uzavře a úkol pokračuje.",
               chat_message_id=message["id"])


def _ago(iso: str | None) -> float:
    """Minutes since `iso` (0 for a missing or bad value)."""
    try:
        t = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds() / 60


def on_owner_chat(conn: sqlite3.Connection, ctx: Ctx, channel_id: int, message_id: int, reply_to: int | None,
                  body: str, quote_of: int | None = None) -> list[int]:
    """The owner answered in chat: an open ask whose question is a chat message is answered by his
    reply to (or quote of) that very message, or in its thread; in a DM also by his next message
    when it is the only open question there and it was asked at most ANSWER_WINDOW_MIN ago. Any
    other message is new input (its agent, or in a group the channel's lead, gets it as usual), not
    the answer to some older question. The answered ticket is done (the asker hears it and a
    blocked task resumes, on_task_changed). Returns the tickets closed."""
    from . import chat, tasks

    if not _has_table(conn):
        return []
    ensure_schema(conn)
    rows = conn.execute(
        """SELECT a.*, m.channel_id, m.created_at AS asked_at, c.kind AS ch_kind FROM owner_asks a
           JOIN chat_messages m ON m.id = a.message_id JOIN channels c ON c.id = m.channel_id
           JOIN tasks t ON t.id = a.ticket_id
           WHERE a.status = 'open' AND t.status != 'done' AND t.archived_at IS NULL AND m.id < ?
             AND m.channel_id = ?""", (message_id, channel_id)).fetchall()
    if not rows:
        return []
    answered = {x for x in (reply_to, quote_of) if x}
    if reply_to:
        root = chat._thread_of(conn, reply_to)
        if root:
            answered.add(root)
    hit = [a for a in rows if a["message_id"] in answered]
    if not hit and not answered:
        in_dm = [a for a in rows if a["ch_kind"] == "dm"]
        if len(in_dm) == 1 and _ago(in_dm[0]["asked_at"]) <= ANSWER_WINDOW_MIN:
            hit = in_dm
    closed = []
    for a in hit:
        tasks.update(conn, ctx, a["ticket_id"], {
            "status": "done", "progress_note": f"Odpověď v chatu (zpráva {message_id}): {body[:400]}"})
        closed.append(a["ticket_id"])
    if rows and not closed:
        audit.log(conn, ctx, "ask_owner:not_an_answer", "chat_message", message_id,
                  open=[a["ticket_id"] for a in rows])
    return closed


# ------------------------------------------------------------------ the decision card

def _match_option(options: list[str], recommendation: str) -> str:
    """The option the recommendation names (by its words), else the recommendation itself."""
    rec = (recommendation or "").strip()
    if not rec:
        return ""
    key = topic_key(rec)
    for o in options:
        k = topic_key(o)
        if k and (k == key or key.startswith(k + " ")):
            return o
    return rec


def card(conn: sqlite3.Connection, ticket_id: int) -> dict | None:
    """The decision card of an open ask: {options, recommendation, default_at} or None."""
    if not _has_table(conn):
        return None
    ensure_schema(conn)
    a = conn.execute("SELECT * FROM owner_asks WHERE ticket_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1",
                     (ticket_id,)).fetchone()
    if a is None or not a["options"]:
        return None
    return {"options": json.loads(a["options"]), "recommendation": a["recommendation"],
            "default_at": a["default_at"]}


def choose(conn: sqlite3.Connection, ctx: Ctx, ticket_id: int, option: str, note: str = "") -> dict:
    """The owner pressed an option on the card: the ticket is done with it (the asker hears it and
    its task resumes)."""
    from . import tasks

    ensure_schema(conn)
    a = conn.execute("SELECT * FROM owner_asks WHERE ticket_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1",
                     (ticket_id,)).fetchone()
    if a is None:
        raise tasks.Invalid(f"{tasks.display_id(ticket_id)} has no open decision")
    t = conn.execute("SELECT assignee_id FROM tasks WHERE id = ?", (ticket_id,)).fetchone()
    if not (actors.get(conn, ctx.actor_id)["is_owner"] or (t and t["assignee_id"] == ctx.actor_id)):
        raise tasks.Invalid("only the one it is asked of decides it")
    opts = json.loads(a["options"] or "[]")
    option = (option or "").strip()
    if opts and option not in opts:
        raise tasks.Invalid(f"choose one of: {opts}")
    if not option:
        raise tasks.Invalid("an option is needed")
    conn.execute("UPDATE owner_asks SET decided_option = ?, decided_by = 'owner' WHERE id = ?", (option, a["id"]))
    text = f"Rozhodnutí: {option}" + (f" — {note.strip()}" if (note or "").strip() else "")
    tasks.update(conn, ctx, ticket_id, {"status": "done", "progress_note": text[:500]})
    audit.log(conn, ctx, "ask_owner:choose", "task", ticket_id, option=option)
    conn.commit()
    return {"ref": tasks.display_id(ticket_id), "decided": option}


def adopt_defaults(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """Decisions the owner left unanswered past their default time: the recommendation is adopted
    (the ticket is done by PersonalOS, the asker resumes), and the morning brief tells him."""
    from . import tasks

    if not _has_table(conn):
        return {}
    ensure_schema(conn)
    now_s = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    rows = conn.execute(
        """SELECT a.* FROM owner_asks a JOIN tasks t ON t.id = a.ticket_id
           WHERE a.status = 'open' AND a.default_at IS NOT NULL AND a.default_at <= ?
             AND a.recommendation IS NOT NULL AND t.status != 'done' AND t.archived_at IS NULL""",
        (now_s,)).fetchall()
    # The owner's own ticket, closed for him by the platform (its notes and the asker's DM say so):
    # done, not handed in for review.
    sys_ctx = Ctx(actors.owner_id(conn), via="system")
    done = []
    for a in rows:
        conn.execute("UPDATE owner_asks SET decided_option = ?, decided_by = 'default' WHERE id = ?",
                     (a["recommendation"], a["id"]))
        tasks.update(conn, sys_ctx, a["ticket_id"], {
            "status": "done",
            "progress_note": f"Bez odpovědi platí doporučení: {a['recommendation']}"[:500]})
        done.append(tasks.display_id(a["ticket_id"]))
    conn.commit()
    return {"adopted": done} if done else {}


def default_digest(conn: sqlite3.Connection, mark: bool = True) -> list[str]:
    """Czech lines for the owner's morning brief: decisions adopted by default since the last brief."""
    from . import tasks

    if not _has_table(conn):
        return []
    ensure_schema(conn)
    rows = conn.execute(
        """SELECT a.id, a.ticket_id, a.decided_option, t.title FROM owner_asks a JOIN tasks t ON t.id = a.ticket_id
           WHERE a.decided_by = 'default' AND a.digested_at IS NULL ORDER BY a.id""").fetchall()
    lines = [f"- {tasks.display_id(r['ticket_id'])} {r['title']}: platí „{r['decided_option']}“ (můžeš změnit "
             "komentářem v ticketu)" for r in rows]
    if mark and rows:
        conn.execute(f"UPDATE owner_asks SET digested_at = ? WHERE id IN ({','.join('?' * len(rows))})",
                     (now_iso(), *[r["id"] for r in rows]))
    return lines


# ------------------------------------------------------------------ approvals

def ping_approval(conn: sqlite3.Connection, ctx: Ctx, approval: dict) -> None:
    """The chat ping for an approval request (the approval queue stays as it is)."""
    from . import tasks

    me = actors.get(conn, ctx.actor_id)
    if me["is_owner"]:
        return
    owner = actors.get(conn, actors.owner_id(conn))
    d = approval.get("details") or {}
    why = str(d.get("why") or d.get("reason") or "").strip()
    src = f" (k {tasks.display_id(approval['task_id'])})" if approval.get("task_id") else ""
    body = (f"{vocative(owner['name'])}, čeká na tebe schválení #{approval['id']}: {approval['action']}{src}. "
            f"Prosím schval, nebo zamítni v Approvals"
            + (f" — potřebuju to, protože {_sentence(why)}." if why else ".")
            + f" @{owner['name']}")
    try:
        _post(conn, ctx, body)
    except Exception:  # noqa: BLE001 - the approval itself is what matters
        audit.log(conn, ctx, "approval_ping_failed", "approval", approval["id"])


def tell_decision(conn: sqlite3.Connection, ctx: Ctx, approval: dict) -> None:
    """The requester of an approval hears the owner's decision in its inbox."""
    from . import chat, wake

    rid = approval["requested_by"]
    if rid == ctx.actor_id:
        return
    r = actors.get(conn, rid)
    if r["archived_at"]:
        return
    word = "approved" if approval["status"] == "approved" else "rejected"
    who = actors.get(conn, ctx.actor_id)["name"]
    body = f"{who} {word} your request #{approval['id']} ({approval['action']})"
    body += f": {approval['comment']}" if approval.get("comment") else "."
    atts = [{"type": "task", "id": approval["task_id"]}] if approval.get("task_id") else None
    from .notices import system_ctx

    try:
        chat.send_dm(conn, system_ctx(conn), rid, body, priority="fyi", attachments=atts, system=True)
    except Exception:  # noqa: BLE001
        return
    if r["kind"] != "human":
        wake.wake(rid)
