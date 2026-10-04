"""One-way floods in a DM: one sender writing to one recipient again and again, with no answer.

The chat loop guard (pos.chat._loop) needs both members writing, so a one-way flood went through:
the Access manager sent the Kniha Lead 164 identical DMs (prod 2026-10-02/03), and the review SLA
sent the CEO ~58 reminders a day (26 % of all chat).

- **Cap**: at most ONE_WAY (default 6) messages an hour from one sender into one DM while the other
  side has not written since; every further message is merged into the sender's last one (a single
  growing digest, marked unread again for the recipient) and wakes nobody. `POS_CHAT_ONEWAY=6/3600`.
- **The Access manager** writes to a member about an agent once per 24 h: a second message to the
  same member naming the same agent(s) within 24 h is merged into the first.

The owner never writes into a digest (people are not capped), a stop is never merged, and the
platform's notices to the owner keep their own channel (asks, incidents).
"""

import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from . import actors, audit, versioning
from .core import Ctx, now_iso

ACCESS_MANAGER_ROLE = "access_manager"
ACCESS_MANAGER_HOURS = 24


def one_way_limit() -> tuple[int, int]:
    raw = os.environ.get("POS_CHAT_ONEWAY", "6/3600")
    try:
        n, s = raw.split("/")
        return max(1, int(n)), max(60, int(s))
    except ValueError:
        return 6, 3600


def _ago(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _named_agents(conn: sqlite3.Connection, body: str, exclude: set[int]) -> set[int]:
    """Agents a message names (by their full name)."""
    low = (body or "").lower()
    out = set()
    for a in conn.execute("SELECT id, name FROM actors WHERE kind != 'human' AND archived_at IS NULL"):
        if a["id"] not in exclude and len(a["name"]) >= 3 and re.search(
                r"(?<!\w)" + re.escape(a["name"].lower()) + r"(?!\w)", low):
            out.add(a["id"])
    return out


def target(conn: sqlite3.Connection, author: sqlite3.Row, ch: sqlite3.Row, body: str, *,
           system: bool, priority: str | None) -> tuple[int, str] | None:
    """(message id to merge into, why) when this DM would flood its recipient; else None."""
    if ch["kind"] != "dm" or author["kind"] == "human" or priority == "stop":
        return None
    others = [r[0] for r in conn.execute("SELECT actor_id FROM channel_members WHERE channel_id = ? AND actor_id != ?",
                                         (ch["id"], author["id"]))]
    if len(others) != 1:
        return None
    other = actors.get(conn, others[0])
    if system and other["is_owner"]:
        return None  # the platform's notices to the owner (asks, incidents) keep their own messages
    last_other = conn.execute("SELECT MAX(id) FROM chat_messages WHERE channel_id = ? AND author_id = ? "
                              "AND archived_at IS NULL", (ch["id"], other["id"])).fetchone()[0] or 0
    if (author["role"] or "") == ACCESS_MANAGER_ROLE:
        named = _named_agents(conn, body, {author["id"], other["id"]})
        if named:
            for m in conn.execute("""SELECT id, body FROM chat_messages WHERE channel_id = ? AND author_id = ?
                                     AND id > ? AND archived_at IS NULL AND created_at >= ? ORDER BY id DESC""",
                                  (ch["id"], author["id"], last_other, _ago(ACCESS_MANAGER_HOURS * 3600))):
                if named <= _named_agents(conn, m["body"], {author["id"], other["id"]}):
                    return m["id"], "access_manager"
    limit, window = one_way_limit()
    rows = conn.execute("""SELECT id FROM chat_messages WHERE channel_id = ? AND author_id = ? AND id > ?
                           AND archived_at IS NULL AND created_at >= ? ORDER BY id DESC""",
                        (ch["id"], author["id"], last_other, _ago(window))).fetchall()
    if len(rows) >= limit:
        return rows[0]["id"], "one_way"
    return None


def merge(conn: sqlite3.Connection, ctx: Ctx, message_id: int, body: str, attachments: list[dict] | None,
          why: str) -> bool:
    """Append `body` to the message (a digest) and mark it unread again for the recipient, without
    a wake. False when the digest is full (the line is only counted)."""
    from .chat import MAX_BODY

    row = conn.execute("SELECT * FROM chat_messages WHERE id = ?", (message_id,)).fetchone()
    stamp = datetime.now(timezone.utc).strftime("%H:%M")
    old = row["body"] or ""
    merged = old if body.strip() in old else f"{old.rstrip()}\n\n[{stamp}] {body.strip()}"
    atts = json.loads(row["attachments"] or "[]")
    for a in attachments or []:
        if a not in atts:
            atts.append(a)
    full = len(merged) > MAX_BODY
    changes = {"edited_at": now_iso(), "attachments": json.dumps(atts)}
    if not full:
        changes["body"] = merged
    versioning.update(conn, ctx, "chat_message", message_id, changes, action="flood_merge")
    if not full:
        conn.execute("UPDATE chat_inbox SET read_at = NULL, delivered_in_run = NULL WHERE message_id = ? "
                     "AND actor_id != ?", (message_id, ctx.actor_id))
    audit.log(conn, ctx, "chat_flood_merge", "chat_message", message_id, why=why, full=full, chars=len(body))
    return not full


def coalesce(conn: sqlite3.Connection, ctx: Ctx, author: sqlite3.Row, ch: sqlite3.Row, body: str,
             attachments: list[dict] | None, *, system: bool, priority: str | None) -> dict | None:
    """pos.chat.send's hook: the message as the author sees it when it was merged into a digest
    (the caller returns it instead of posting), else None."""
    hit = target(conn, author, ch, body, system=system, priority=priority)
    if hit is None:
        return None
    from .chat import message_view

    mid, why = hit
    merge(conn, ctx, mid, body, attachments, why)
    conn.commit()
    out = {**message_view(conn, mid, ctx.actor_id), "duplicate": True, "coalesced": True,
           "delivered_to_run": None, "inbox": []}
    if not system:
        out["platform_note"] = (
            "You already told this member about this agent today; your message was added to that message, "
            "nobody was woken." if why == "access_manager" else
            "Many messages to this member without an answer: yours was added to your last message (a "
            "digest) and woke nobody. Wait for an answer, or put the details into a task.")
    return out
