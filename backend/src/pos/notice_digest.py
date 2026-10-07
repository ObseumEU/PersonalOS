"""Informational notices to agents: one digest a day, deduplicated per subject, never a run by itself.

The platform's notices to an agent (a refused access request, the company cap, "T-12 waits for your
review", "X handed in T-12", "Y assigned you T-12", the stuck-work digest, a lead alert) each went into
the agent's inbox as its own DM and each could start a run: 30. 9.–6. 10. agents got ~610 such rows
(284 Access-manager denials and loop notices to Kniha Growth & Sales and the Kniha Lead, 219 review
reminders to the CEO, cap and hand-in notices), and with unread messages now starting an inbox run
(pos.chat.ensure_inbox_task) every one of them would cost a run.

A sender marks such a notice with `notice="<type>"` (optionally `"<type>:<subject>"`) on
pos.chat.send / send_dm (system notices in a DM only). For an agent recipient it then goes here instead:

- **one digest message per agent and day** (Prague), from PersonalOS, "Souhrn upozornění …": one line per
  (type, subject), the subject being the explicit one, else the tasks it is about;
- **at most one line per subject per day**: the same type and subject again only updates its line (the
  latest text, ×N) and does not mark the digest unread again; a new subject adds a line and marks it
  unread;
- **informational**: its inbox row is `info = 1`; it never wakes a worker, never makes an inbox task
  (pos.chat.inbox_unread(include_info=False)), and is delivered with the agent's next real run.

People (the owner, a human reviewer) get the notice as before.
"""

import json
import re
import sqlite3
from datetime import datetime, timezone

from . import actors, audit, versioning
from .core import TZ, Ctx, now_iso

TITLE = "Souhrn upozornění"
LINE_CHARS = 420

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notice_digests (
    actor_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    PRIMARY KEY (actor_id, day)
);
CREATE TABLE IF NOT EXISTS notice_digest_items (
    actor_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    key TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    line TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 1,
    first_at TEXT NOT NULL,
    last_at TEXT NOT NULL,
    PRIMARY KEY (actor_id, day, key)
);
"""
_ready: set[str] = set()


def ensure_schema(conn: sqlite3.Connection) -> None:
    key = str(conn.execute("PRAGMA database_list").fetchone()[2])
    if key in _ready:
        return
    conn.executescript(_SCHEMA)
    _ready.add(key)


def _day(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(TZ).date().isoformat()


def subject_of(notice: str, attachments: list[dict] | None) -> tuple[str, str]:
    """(type, subject): the explicit "type:subject", else the tasks the notice is attached to."""
    kind, _, subject = (notice or "notice").partition(":")
    if not subject:
        ids = sorted({int(a["id"]) for a in attachments or [] if a.get("type") == "task" and a.get("id")})
        subject = ",".join(f"T-{i}" for i in ids)
    return kind.strip()[:40] or "notice", subject.strip()[:80]


def recipient(conn: sqlite3.Connection, ch: sqlite3.Row, author_id: int) -> sqlite3.Row | None:
    """The DM's other member when it is an active agent (a person gets the notice as it is)."""
    if ch["kind"] != "dm":
        return None
    row = conn.execute("""SELECT a.* FROM channel_members m JOIN actors a ON a.id = m.actor_id
                          WHERE m.channel_id = ? AND m.actor_id != ?""", (ch["id"], author_id)).fetchone()
    if row is None or row["kind"] == "human" or row["archived_at"] or (row["runtime"] or "") == "service":
        return None
    return row


def _line(body: str) -> str:
    text = re.sub(r"\s+", " ", body or "").strip()
    return text if len(text) <= LINE_CHARS else text[: LINE_CHARS - 1].rstrip() + "…"


def _render(conn: sqlite3.Connection, actor_id: int, day: str) -> str:
    items = conn.execute("""SELECT * FROM notice_digest_items WHERE actor_id = ? AND day = ? ORDER BY first_at, key""",
                         (actor_id, day)).fetchall()
    d = datetime.fromisoformat(day)
    head = (f"{TITLE} {d.day}. {d.month}. (informace od platformy; přišly jako jedna zpráva a samy nic "
            f"nespouštějí, stejné téma nejvýš jednou za den):")
    lines = []
    for it in items:
        at = datetime.fromisoformat(it["last_at"]).astimezone(TZ).strftime("%H:%M")
        who = f"{it['author']}: " if it["author"] and it["author"] != actors.SYSTEM_NAME else ""
        times = f" (×{it['count']}, naposledy {at})" if it["count"] > 1 else f" ({at})"
        lines.append(f"- {who}{it['line']}{times}")
    from .chat import MAX_BODY

    out = head
    for i, ln in enumerate(lines):
        rest = len(lines) - i
        if len(out) + len(ln) + 80 > MAX_BODY:
            out += f"\n… a dalších {rest} upozornění (stejná témata najdeš u úkolů)."
            break
        out += "\n" + ln
    return out


def add(conn: sqlite3.Connection, ctx: Ctx, to: sqlite3.Row, body: str, notice: str,
        attachments: list[dict] | None, now: datetime | None = None) -> dict:
    """Put an informational notice for agent `to` into today's digest; returns the digest message as
    pos.chat.send would (with `digest: True`). The caller (pos.chat.send) commits."""
    from . import chat

    ensure_schema(conn)
    day = _day(now)
    kind, subject = subject_of(notice, attachments)
    key = f"{kind}|{subject}" if subject else kind
    author = actors.get(conn, ctx.actor_id)["name"]
    stamp = now_iso()
    item = conn.execute("SELECT * FROM notice_digest_items WHERE actor_id = ? AND day = ? AND key = ?",
                        (to["id"], day, key)).fetchone()
    if item is not None:
        conn.execute("""UPDATE notice_digest_items SET count = count + 1, line = ?, author = ?, last_at = ?
                        WHERE actor_id = ? AND day = ? AND key = ?""", (_line(body), author, stamp, to["id"], day, key))
    else:
        conn.execute("""INSERT INTO notice_digest_items (actor_id, day, key, author, line, count, first_at, last_at)
                        VALUES (?, ?, ?, ?, ?, 1, ?, ?)""", (to["id"], day, key, author, _line(body), stamp, stamp))
    text = _render(conn, to["id"], day)
    sys_ctx = Ctx(actors.system_id(conn), via="system")
    dig = conn.execute("SELECT message_id FROM notice_digests WHERE actor_id = ? AND day = ?", (to["id"], day)).fetchone()
    msg = (conn.execute("SELECT * FROM chat_messages WHERE id = ? AND archived_at IS NULL", (dig["message_id"],)).fetchone()
           if dig else None)
    if msg is None:
        ch = chat.dm_channel(conn, sys_ctx.actor_id, to["id"], sys_ctx)
        out = chat.send(conn, sys_ctx, ch["id"], text, priority="fyi", attachments=attachments, system=True, info=True)
        conn.execute("""INSERT INTO notice_digests (actor_id, day, message_id) VALUES (?, ?, ?)
                        ON CONFLICT(actor_id, day) DO UPDATE SET message_id = excluded.message_id""",
                     (to["id"], day, out["id"]))
        mid = out["id"]
    else:
        mid = msg["id"]
        atts = json.loads(msg["attachments"] or "[]")
        for a in attachments or []:
            if a not in atts:
                atts.append(a)
        versioning.update(conn, sys_ctx, "chat_message", mid,
                          {"body": text, "attachments": json.dumps(atts), "edited_at": stamp}, action="notice_digest")
        if item is None:  # a new subject today: the digest is news again (delivered with the next real run)
            conn.execute("""UPDATE chat_inbox SET read_at = NULL, delivered_in_run = NULL, info = 1
                            WHERE message_id = ? AND actor_id = ?""", (mid, to["id"]))
    audit.log(conn, ctx, "notice_digest", "chat_message", mid, to=to["id"], kind=kind, subject=subject or None,
              repeat=item is not None)
    conn.commit()
    return {**chat.message_view(conn, mid, ctx.actor_id), "digest": True, "duplicate": item is not None,
            "delivered_to_run": None, "inbox": [to["id"]]}
