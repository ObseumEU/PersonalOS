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

The bookkeeping lives in `owner_asks`, created on first use (no numbered
migration, so it cannot collide with one added elsewhere).
"""

import re
import sqlite3
import unicodedata

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


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_SCHEMA)
    conn.execute("CREATE INDEX IF NOT EXISTS owner_asks_ticket ON owner_asks (ticket_id)")


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
        links: list[str] | None = None, after: str = "", priority: int | None = None) -> dict:
    """Create the owner's ticket, ping the owner in #team and (when blocking)
    park the asking task. Returns {ticket, ref, message_id, deduped, blocking}."""
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

    notes = _notes(asker=me["name"], kind=kind, title=title, why=why, details=details or "",
                   options=[o for o in (options or []) if str(o).strip()], recommendation=recommendation or "",
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
    conn.execute(
        """INSERT INTO owner_asks (ticket_id, asker_id, source_task_id, topic_key, kind, blocking, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (ticket["id"], ctx.actor_id, task_id, key, kind, int(bool(blocking)), now_iso()))
    ask_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    if source:
        comments.log(conn, ctx, source["id"], f"Asked {owner['name']}: {ticket['ref']} — {title}"
                     + (" (waiting for the answer)" if blocking else ""), "system")
        if blocking and source["status"] not in ("done", "waiting"):
            versioning.update(conn, ctx, tasks.ENTITY, source["id"], {
                "status": "waiting", "progress_note": f"Waiting for {owner['name']}: {ticket['ref']}"[:500]},
                action="wait")
    audit.log(conn, ctx, "ask_owner", "task", ticket["id"], source=task_id, topic=key, blocking=blocking)
    # chat.send commits: the ticket, the bookkeeping and the ping land together.
    mid = _post(conn, ctx, _chat_body(owner=owner["name"], ref=ticket["ref"], title=title, why=why, kind=kind,
                                      blocking=blocking and source is not None, recommendation=recommendation or ""))
    conn.execute("UPDATE owner_asks SET message_id = ? WHERE id = ?", (mid, ask_id))
    return {"ref": ticket["ref"], "ticket_id": ticket["id"], "title": ticket["title"], "status": ticket["status"],
            "message_id": mid, "deduped": False, "blocking": bool(blocking),
            "note": ("Your task waits for the answer; you will get it in your inbox and the task comes back "
                     "to your queue. Finish this run now with a short summary." if blocking and source else
                     "The answer comes to your inbox.")}


# ------------------------------------------------------------------ answers

def _asks_for(conn: sqlite3.Connection, ticket_id: int) -> list[sqlite3.Row]:
    if not _has_table(conn):
        return []
    return conn.execute("SELECT * FROM owner_asks WHERE ticket_id = ?", (ticket_id,)).fetchall()


def _resume(conn: sqlite3.Connection, ctx: Ctx, a: sqlite3.Row, why: str) -> None:
    from . import tasks, versioning

    if not a["blocking"] or not a["source_task_id"]:
        return
    src = conn.execute("SELECT status FROM tasks WHERE id = ?", (a["source_task_id"],)).fetchone()
    if src and src["status"] == "waiting":
        versioning.update(conn, ctx, tasks.ENTITY, a["source_task_id"],
                          {"status": "next", "progress_note": why[:500]}, action="resume")


def _tell(conn: sqlite3.Connection, ctx: Ctx, a: sqlite3.Row, body: str) -> None:
    from . import chat, wake

    asker = actors.get(conn, a["asker_id"])
    if asker["archived_at"] or a["asker_id"] == ctx.actor_id:
        return
    try:
        chat.send_dm(conn, ctx, a["asker_id"], body[:3900], priority="change_plan",
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
    try:
        chat.send_dm(conn, ctx, rid, body, priority="fyi", attachments=atts, system=True)
    except Exception:  # noqa: BLE001
        return
    if r["kind"] != "human":
        wake.wake(rid)
