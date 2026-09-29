"""Chat for people and agents inside PersonalOS (docs/CHAT.md).

Channels are DMs (two members) or named groups. Every message is versioned
(edits keep history) and audited; deleting archives. The agent messages of
AGENTS-SPEC 6b are chat too: `agents.send_message` writes a DM, and an agent's
inbox (`check_inbox`) is its row set in `chat_inbox`:

- a DM to it,
- an @mention of it,
- a reply to one of its messages,
- a message with a priority in a channel it belongs to (`stop` only reaches
  the DM recipient or the members it mentions).

Chat never leaves PersonalOS. Outside systems talk over A2A; their messages
show up here from that remote member with trust `external`.
"""

import asyncio
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import actors, audit, runner, versioning
from .agents import AgentError, PRIORITIES, has_permission
from .core import Ctx, Forbidden, NotFound, now_iso

versioning.register("channel", "channels")
versioning.register("chat_message", "chat_messages")

TEAM = "team"
VISIBILITIES = ("public", "team", "private")
MAX_BODY = 8000
_TASK_REF = re.compile(r"\bT-(\d{1,6})\b")


class ChatError(AgentError):
    pass


# Chat hygiene (the chat audit, prod 2026-09-27: 29 of 155 agent-to-agent DMs were "díky, beru na
# vědomí", 13 of them stopped a running session to be answered; 7 exact duplicates; 24 agent
# messages over 800 characters; an owner message in #kniha without a mention reached nobody).
AGENT_SOFT_BODY = 1200   # longer: the author gets a note to put details into a task or a note
DUP_WINDOW_S = 120       # the same body from the same author in the same place: one message
UNROUTED = ("system", "weekly")  # channels whose unaddressed messages have their own handling


def agent_max_body() -> int:
    """An agent's chat message is a short answer; long content goes into a task or a note."""
    try:
        return max(500, int(os.environ.get("POS_CHAT_AGENT_MAX", "3000")))
    except ValueError:
        return 3000


def loop_limit() -> tuple[int, int]:
    """(messages, window s) two agents may exchange in a DM before it counts as a loop;
    POS_CHAT_LOOP='8/1800'."""
    raw = os.environ.get("POS_CHAT_LOOP", "8/1800")
    try:
        n, s = raw.split("/")
        return max(2, int(n)), max(60, int(s))
    except ValueError:
        return 8, 1800


_ACK_WORDS = set("""
díky dík diky dik děkuji dekuji děkuju dekuju moc mockrát mockrat thanks thank thx ty you ok okay oki okej
jasně jasne jasny jasný rozumím rozumim rozumím. beru na vědomí vedomi vědomí super fajn skvělé skvele skvělý
výborně vyborne paráda parada v pořádku poradku za info informaci informace předání predani to taky i great noted
got it perfect cool sounds good dobře dobre dobrý dobry good k ke all noted 👍 ✅ 🙏 👌 🙂 😊 👏 ❤️
""".split())
_WORD = re.compile(r"[^\W\d_][\w'’-]*", re.UNICODE)


def is_ack(body: str) -> bool:
    """A bare acknowledgement ("díky", "ok, beru na vědomí", "👍"): nothing to answer or act on.
    A question mark, a number or any other word makes it a real message."""
    text = _TASK_REF.sub(" ", (body or "").strip()).lower()
    if not text or "?" in text or len(text) > 120 or any(ch.isdigit() for ch in text):
        return False
    words = _WORD.findall(text)
    if not words:  # emoji and punctuation only
        return not any(ch.isalnum() for ch in text)
    return all(w in _ACK_WORDS for w in words)


def _asked_before(conn: sqlite3.Connection, channel_id: int, reply_to: int | None, author_id: int) -> bool:
    """The other side's last message here (the DM, or the thread) asked something: an "ok" to it
    is an answer, not an acknowledgement."""
    if reply_to:
        root = _thread_of(conn, reply_to)
        row = conn.execute("""SELECT body FROM chat_messages WHERE channel_id = ? AND (id = ? OR reply_to = ?)
                              AND author_id != ? AND archived_at IS NULL ORDER BY id DESC LIMIT 1""",
                           (channel_id, root, root, author_id)).fetchone()
    else:
        row = conn.execute("""SELECT body FROM chat_messages WHERE channel_id = ? AND author_id != ?
                              AND archived_at IS NULL ORDER BY id DESC LIMIT 1""", (channel_id, author_id)).fetchone()
    return bool(row and "?" in row["body"])


def rate_limit() -> tuple[int, int]:
    """(max messages, window in seconds) per agent; POS_CHAT_RATE_LIMIT='20/600'."""
    raw = os.environ.get("POS_CHAT_RATE_LIMIT", "20/600")
    try:
        n, s = raw.split("/")
        return max(1, int(n)), max(1, int(s))
    except ValueError:
        return 20, 600


# ------------------------------------------------------------------ channels

def _channel(conn: sqlite3.Connection, channel_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
    if row is None:
        raise NotFound(f"channel {channel_id}")
    return row


def _is_member(conn: sqlite3.Connection, channel_id: int, actor_id: int) -> bool:
    return conn.execute("SELECT 1 FROM channel_members WHERE channel_id = ? AND actor_id = ?",
                        (channel_id, actor_id)).fetchone() is not None


def member_ids(conn: sqlite3.Connection, channel_id: int) -> list[int]:
    return [r[0] for r in conn.execute(
        "SELECT actor_id FROM channel_members WHERE channel_id = ? ORDER BY actor_id", (channel_id,))]


def can_read(conn: sqlite3.Connection, ch: sqlite3.Row, actor_id: int) -> bool:
    """Members read; team and public groups are open to every member of
    PersonalOS; the owner may read everything (oversight of agents' DMs)."""
    if _is_member(conn, ch["id"], actor_id) or actors.get(conn, actor_id)["is_owner"]:
        return True
    return ch["kind"] == "group" and ch["visibility"] != "private"


def _check_read(conn: sqlite3.Connection, ch: sqlite3.Row, actor_id: int) -> None:
    if not can_read(conn, ch, actor_id):
        raise Forbidden(f"channel {ch['id']} is private")


def _add_member(conn: sqlite3.Connection, channel_id: int, actor_id: int, role: str = "member") -> bool:
    last = conn.execute("SELECT COALESCE(MAX(id), 0) FROM chat_messages WHERE channel_id = ?",
                        (channel_id,)).fetchone()[0]
    cur = conn.execute(
        "INSERT OR IGNORE INTO channel_members (channel_id, actor_id, role, joined_at, last_read_message_id) "
        "VALUES (?, ?, ?, ?, ?)", (channel_id, actor_id, role, now_iso(), last))
    return cur.rowcount > 0


def may_answer(conn: sqlite3.Connection, actor_id: int, channel_id: int | None = None) -> bool:
    """Someone waits for this agent's answer: it has an open "Chat: answer" task
    (for this channel, when given). Answering them needs no messages:send: every
    agent answers the person who wrote to it (pos.workers). A meeting participant whose turn it
    is may speak in that meeting (pos.meetings)."""
    from . import meetings

    if channel_id is None:
        try:
            if conn.execute("SELECT 1 FROM meeting_turns WHERE actor_id = ? AND status = 'open'",
                            (actor_id,)).fetchone():
                return True
        except sqlite3.OperationalError:
            pass
    elif meetings.may_post(conn, actor_id, channel_id):
        return True
    for t in conn.execute("""SELECT id FROM tasks WHERE assignee_id = ? AND topic = 'chat' AND archived_at IS NULL
                             AND status IN ('inbox', 'next', 'working')""", (actor_id,)).fetchall():
        if channel_id is None:
            return True
        if conn.execute("""SELECT 1 FROM audit_log WHERE action = 'chat_task' AND entity = 'task' AND entity_id = ?
                           AND json_extract(detail, '$.channel') = ?""", (t["id"], channel_id)).fetchone():
            return True
    return False


def _require_send(conn: sqlite3.Connection, ctx: Ctx, channel_id: int | None = None) -> None:
    """Agents need messages:send (or someone waiting for their answer in this
    channel, may_answer), may not act while frozen or paused."""
    from . import killswitch

    row = actors.get(conn, ctx.actor_id)
    if row["kind"] == "human":
        return
    if not has_permission(conn, ctx.actor_id, "messages:send") and not (
            channel_id is not None and may_answer(conn, ctx.actor_id, channel_id)):
        raise Forbidden("missing permission messages:send")
    killswitch.check_agent_may_act(conn, ctx)
    if row["paused_at"]:
        raise Forbidden("this agent is paused by the owner")


def resolve_actor(conn: sqlite3.Connection, ref: int | str) -> sqlite3.Row:
    if isinstance(ref, int) or str(ref).isdigit():
        row = actors.get(conn, int(ref))
    else:
        row = actors.find_by_name(conn, str(ref).strip().lstrip("@"))
        if row is None:
            raise NotFound(f"no member called {ref}")
    if row["archived_at"]:
        raise ChatError(f"{row['name']} is archived")
    return row


def resolve_channel(conn: sqlite3.Connection, ref: int | str) -> sqlite3.Row:
    """A channel id, or a group name with or without '#'."""
    if isinstance(ref, int) or str(ref).isdigit():
        return _channel(conn, int(ref))
    row = conn.execute("SELECT * FROM channels WHERE kind = 'group' AND name = ? COLLATE NOCASE "
                       "AND archived_at IS NULL", (str(ref).strip().lstrip("#"),)).fetchone()
    if row is None:
        raise NotFound(f"no channel #{str(ref).lstrip('#')}")
    return row


def find_dm(conn: sqlite3.Connection, a: int, b: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM channels WHERE dm_key = ?", (f"{min(a, b)}:{max(a, b)}",)).fetchone()


def dm_channel(conn: sqlite3.Connection, a: int, b: int, ctx: Ctx | None = None) -> sqlite3.Row:
    """The DM channel between two members, created on first use."""
    if a == b:
        raise ChatError("a DM needs two different members")
    key = f"{min(a, b)}:{max(a, b)}"
    row = find_dm(conn, a, b)
    if row:
        return row
    ctx = ctx or Ctx(a)
    ch = versioning.insert(conn, ctx, "channel", {"kind": "dm", "visibility": "private", "dm_key": key,
                                                  "created_by": ctx.actor_id, "created_at": now_iso()})
    _add_member(conn, ch["id"], a)
    _add_member(conn, ch["id"], b)
    return _channel(conn, ch["id"])


def create_channel(conn: sqlite3.Connection, ctx: Ctx, name: str, members: list[int | str] | None = None,
                   topic: str = "", visibility: str = "team") -> dict:
    """A named group channel. The creator is its owner; members are invited."""
    _require_send(conn, ctx)
    name = name.strip().lstrip("#").strip()
    if not re.fullmatch(r"[\w][\w .-]{0,60}", name):
        raise ChatError("a channel name is 1-60 letters, digits, spaces, '.', '-' or '_'")
    if visibility not in VISIBILITIES:
        raise ChatError(f"visibility must be one of {VISIBILITIES}")
    if conn.execute("SELECT 1 FROM channels WHERE kind = 'group' AND name = ? COLLATE NOCASE "
                    "AND archived_at IS NULL", (name,)).fetchone():
        raise ChatError(f"#{name} already exists")
    ch = versioning.insert(conn, ctx, "channel", {"kind": "group", "name": name, "topic": topic.strip(),
                                                  "visibility": visibility, "created_by": ctx.actor_id,
                                                  "created_at": now_iso()})
    _add_member(conn, ch["id"], ctx.actor_id, "owner")
    for m in members or []:
        _add_member(conn, ch["id"], resolve_actor(conn, m)["id"])
    conn.commit()
    return channel_view(conn, ch["id"], ctx.actor_id)


def invite(conn: sqlite3.Connection, ctx: Ctx, channel_id: int, member: int | str) -> dict:
    """Add a member to a group. Members invite; the owner may invite anywhere."""
    _require_send(conn, ctx)
    ch = _channel(conn, channel_id)
    if ch["kind"] != "group":
        raise ChatError("DMs have exactly two members; create a group instead")
    if ch["archived_at"]:
        raise ChatError("this channel is archived")
    if not _is_member(conn, channel_id, ctx.actor_id) and not actors.get(conn, ctx.actor_id)["is_owner"]:
        raise Forbidden("only members invite to a channel")
    target = resolve_actor(conn, member)
    if _add_member(conn, channel_id, target["id"]):
        audit.log(conn, ctx, "chat_invite", "channel", channel_id, member=target["id"])
    conn.commit()
    return channel_view(conn, channel_id, ctx.actor_id)


def archive_channel(conn: sqlite3.Connection, ctx: Ctx, channel_id: int) -> dict:
    ch = _channel(conn, channel_id)
    role = conn.execute("SELECT role FROM channel_members WHERE channel_id = ? AND actor_id = ?",
                        (channel_id, ctx.actor_id)).fetchone()
    if not actors.get(conn, ctx.actor_id)["is_owner"] and not (role and role["role"] == "owner"):
        raise Forbidden("only the channel's owner archives it")
    if ch["kind"] == "group" and ch["name"] == TEAM:
        raise ChatError("#team stays")
    versioning.archive(conn, ctx, "channel", channel_id)
    conn.commit()
    return channel_view(conn, channel_id, ctx.actor_id)


def _project_team_agents(conn: sqlite3.Connection) -> set[int]:
    """Agents of a project team (actors.team is the slug of a project with its own channel, e.g.
    the Kniha team): they talk in their project channel and DMs, not in #team."""
    try:
        rows = conn.execute("""SELECT a.id FROM actors a JOIN projects p ON lower(p.slug) = lower(a.team)
                               WHERE a.kind != 'human' AND p.channel_id IS NOT NULL
                               AND COALESCE(p.status, 'active') NOT IN ('done', 'archived')""").fetchall()
    except sqlite3.OperationalError:  # no projects table (an old database)
        return set()
    return {r["id"] for r in rows}


def ensure_team_channel(conn: sqlite3.Connection) -> int:
    """#team with the owner and every active agent except project teams (they have their own
    channel, _project_team_agents); safe to call at every start."""
    owner = actors.owner_id(conn)
    row = conn.execute("SELECT id FROM channels WHERE kind = 'group' AND name = ? AND archived_at IS NULL",
                       (TEAM,)).fetchone()
    if row is None:
        ch = versioning.insert(conn, Ctx(owner, via="system"), "channel", {
            "kind": "group", "name": TEAM, "topic": "Everyone in PersonalOS: standups, check-ins, questions.",
            "visibility": "team", "created_by": owner, "created_at": now_iso()})
        cid = ch["id"]
        _add_member(conn, cid, owner, "owner")
    else:
        cid = row["id"]
    scoped = _project_team_agents(conn)
    for a in conn.execute("SELECT id FROM actors WHERE archived_at IS NULL AND (kind != 'human' OR is_owner = 1)"):
        if a["id"] not in scoped:
            _add_member(conn, cid, a["id"])
    for aid in scoped:
        if conn.execute("DELETE FROM channel_members WHERE channel_id = ? AND actor_id = ?", (cid, aid)).rowcount:
            audit.log(conn, Ctx(owner, via="system"), "chat_leave", "channel", cid, member=aid,
                      reason="project team: its own channel")
    conn.commit()
    return cid


def post_to_team(conn: sqlite3.Connection, author_id: int, body: str, priority: str | None = None) -> dict:
    """Platform posts to #team on a member's behalf (the PM's standup, HR's
    check). Audited like any message; not an agent tool, so no rate limit."""
    cid = ensure_team_channel(conn)
    _add_member(conn, cid, author_id)
    return send(conn, Ctx(author_id, via="system"), cid, body, priority=priority, system=True)


SYSTEM = "system"


def ensure_system_channel(conn: sqlite3.Connection) -> int:
    """#system: the platform's automated notices (the Access manager's pauses, grants and
    expiries, the Hlídač's heartbeat and code-built health lines), so #team stays for people
    and real agent messages. The owner is a member; authors join when they post."""
    owner = actors.owner_id(conn)
    row = conn.execute("SELECT id FROM channels WHERE kind = 'group' AND name = ? AND archived_at IS NULL",
                       (SYSTEM,)).fetchone()
    if row is not None:
        return row["id"]
    ch = versioning.insert(conn, Ctx(owner, via="system"), "channel", {
        "kind": "group", "name": SYSTEM, "visibility": "team", "created_by": owner, "created_at": now_iso(),
        "topic": "Automatická hlášení platformy (přístupy, pozastavení, zdraví). Lidé a agenti píšou v #team."})
    _add_member(conn, ch["id"], owner, "owner")
    conn.commit()
    return ch["id"]


def post_system(conn: sqlite3.Connection, author_id: int, body: str, subject: str | None = None) -> dict:
    """An automated notice in #system, grouped: one message per author and subject (the agent
    it is about) per hour; a later notice in the same hour is appended to it as a new line
    (an edit, so the history keeps every version) instead of a new message. Nobody's inbox
    gets it and no agent is woken."""
    cid = ensure_system_channel(conn)
    _add_member(conn, cid, author_id)
    body = (body or "").strip()
    head = f"**{subject}**" if subject else None
    if head:
        body = " ".join(body.split())  # one line per notice
        hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
        prev = conn.execute(
            """SELECT id, body FROM chat_messages WHERE channel_id = ? AND author_id = ? AND archived_at IS NULL
               AND created_at >= ? AND body LIKE ? ORDER BY id DESC LIMIT 1""",
            (cid, author_id, hour, head + ":%")).fetchone()
        if prev is not None and len(prev["body"]) + len(body) + 3 <= MAX_BODY:
            ctx = Ctx(author_id, via="system")
            versioning.update(conn, ctx, "chat_message", prev["id"],
                              {"body": f"{prev['body']} · {body}", "edited_at": now_iso()}, action="edit")
            conn.commit()
            return message_view(conn, prev["id"], author_id)
        body = f"{head}: {body}"
    ctx = Ctx(author_id, via="system")
    row = versioning.insert(conn, ctx, "chat_message", {
        "channel_id": cid, "author_id": author_id, "body": body[:MAX_BODY], "reply_to": None, "mentions": "[]",
        "attachments": "[]", "priority": None, "trust": _trust(ctx, actors.get(conn, author_id)),
        "created_at": now_iso()})
    conn.execute("UPDATE channel_members SET last_read_message_id = ? WHERE channel_id = ? AND actor_id = ?",
                 (row["id"], cid, author_id))
    audit.log(conn, ctx, "chat_send", "chat_message", row["id"], channel=cid, system_notice=True)
    conn.commit()
    return message_view(conn, row["id"], author_id)


# ------------------------------------------------------------------ messages

_AT_TOKEN = re.compile(r"(?<![\w@])@([^\W_][\w-]*)", re.UNICODE)
_TEAM_TOKEN = re.compile(r"(?:t[ýy]m|team)-(.+)")


def _agents_sql() -> str:
    return ("SELECT id, role, reports_to FROM actors WHERE archived_at IS NULL AND kind != 'human' "
            "AND runtime != 'service'")


def _team_members(conn: sqlite3.Connection, slug: str) -> list[int]:
    """@tým-kniha: the agents of that project channel (by channel name or project slug)."""
    ch = conn.execute("SELECT id FROM channels WHERE kind = 'group' AND name = ? COLLATE NOCASE "
                      "AND archived_at IS NULL", (slug,)).fetchone()
    if ch is None:
        try:
            ch = conn.execute("SELECT channel_id AS id FROM projects WHERE slug = ? COLLATE NOCASE "
                              "AND channel_id IS NOT NULL", (slug,)).fetchone()
        except sqlite3.OperationalError:
            ch = None
    if ch is None:
        return []
    return [r["id"] for r in conn.execute(
        f"{_agents_sql()} AND id IN (SELECT actor_id FROM channel_members WHERE channel_id = ?) ORDER BY id",
        (ch["id"],))]


def role_member(conn: sqlite3.Connection, role: str, channel_id: int | None = None) -> int | None:
    """@CTO, @HR, @SRE: the active agent with that role (its role key, '_' or '-' alike). Several
    with one role: the channel's own first, then the one highest in the chain."""
    key = role.strip().lower().replace("-", "_")
    rows = [r for r in conn.execute(_agents_sql() + " ORDER BY id") if (r["role"] or "").lower() == key]
    if not rows:
        return None
    if channel_id is not None and len(rows) > 1:
        inside = set(member_ids(conn, channel_id))
        rows = [r for r in rows if r["id"] in inside] or rows
    ids = {r["id"] for r in rows}
    top = [r for r in rows if r["reports_to"] not in ids]
    return (top or rows)[0]["id"]


def _mentions(conn: sqlite3.Connection, body: str, extra: list[int | str] | None,
              channel_id: int | None = None) -> list[int]:
    """@Name anywhere in the body (names may contain spaces; longest wins), then @role
    (@CTO, @HR: role_member) and @tým-<channel> (every agent of that project channel)."""
    found: list[int] = []
    lowered = body.lower()
    rows = conn.execute("SELECT id, name FROM actors WHERE archived_at IS NULL").fetchall()
    for r in sorted(rows, key=lambda r: -len(r["name"])):
        for m in re.finditer(re.escape("@" + r["name"].lower()), lowered):
            end = m.end()
            if end >= len(lowered) or not (lowered[end].isalnum() or lowered[end] == "_"):
                if r["id"] not in found:
                    found.append(r["id"])
                # Blank the match so "@HR agent" does not also count as "@HR".
                lowered = lowered[:m.start()] + " " * (end - m.start()) + lowered[end:]
                break
    for m in _AT_TOKEN.finditer(lowered):
        tok = m.group(1).rstrip("-")
        team = _TEAM_TOKEN.fullmatch(tok)
        hits = _team_members(conn, team.group(1)) if team else [
            x for x in [role_member(conn, tok, channel_id)] if x is not None]
        found += [h for h in hits if h not in found]
    for ref in extra or []:
        aid = resolve_actor(conn, ref)["id"]
        if aid not in found:
            found.append(aid)
    return found


def _trust(ctx: Ctx, author: sqlite3.Row) -> str:
    if ctx.via == "a2a":
        return "external"
    if author["is_owner"]:
        return "owner"
    return "person" if author["kind"] == "human" else "agent"


def _check_rate(conn: sqlite3.Connection, author_id: int) -> None:
    limit, window = rate_limit()
    since = (datetime.now(timezone.utc) - timedelta(seconds=window)).isoformat(timespec="seconds")
    n = conn.execute("SELECT COUNT(*) FROM chat_messages WHERE author_id = ? AND created_at >= ?",
                     (author_id, since)).fetchone()[0]
    if n >= limit:
        raise ChatError(f"rate limit: at most {limit} chat messages per {window // 60} min; wait a little")


def _check_budget(conn: sqlite3.Connection, author_id: int) -> None:
    """Agents' messages go through the same budget gate as their runs."""
    from .budget import service as budget

    d = budget.can_run(conn, str(author_id))
    if not d.allowed:
        raise ChatError(f"budget: {d.reason}")


def _duplicate(conn: sqlite3.Connection, author_id: int, channel_id: int, body: str,
               reply_to: int | None) -> int | None:
    """The same message from the same author in the same place a moment ago (a double submit,
    a retried tool call): that message's id."""
    since = (datetime.now(timezone.utc) - timedelta(seconds=DUP_WINDOW_S)).isoformat(timespec="seconds")
    row = conn.execute("""SELECT id FROM chat_messages WHERE channel_id = ? AND author_id = ? AND body = ?
                          AND COALESCE(reply_to, 0) = ? AND archived_at IS NULL AND created_at >= ?
                          ORDER BY id DESC LIMIT 1""",
                       (channel_id, author_id, body, reply_to or 0, since)).fetchone()
    return row["id"] if row else None


def channel_lead(conn: sqlite3.Connection, ch: sqlite3.Row) -> int | None:
    """Who answers an unaddressed question in a group channel: the project's lead when an agent
    leads it, else the top agent of the channel's own team (actors.team = the channel name, e.g.
    the Kniha Lead in #kniha), else the CEO (the chain of command)."""
    name = (ch["name"] or "").lower()
    try:
        p = conn.execute("SELECT lead_id FROM projects WHERE channel_id = ? OR lower(slug) = ?",
                         (ch["id"], name)).fetchone()
    except sqlite3.OperationalError:
        p = None
    if p and p["lead_id"]:
        lead = actors.get(conn, p["lead_id"])
        if lead["kind"] != "human" and not lead["archived_at"]:
            return lead["id"]
    team = conn.execute(_agents_sql() + " AND lower(COALESCE(team, '')) = ? ORDER BY id", (name,)).fetchall()
    ids = {r["id"] for r in team}
    top = [r["id"] for r in team if r["reports_to"] not in ids]
    if top:
        return top[0]
    return role_member(conn, "ceo")


def _route_unaddressed(conn: sqlite3.Connection, ch: sqlite3.Row,
                       parent: sqlite3.Row | None) -> tuple[int, str] | None:
    """An owner's message in a group that names nobody still reaches someone: a reply in a thread
    goes to the agent he answers (or the last agent in that thread), anything else to the
    channel's lead (channel_lead; in #team the CEO)."""
    if parent is not None:
        root = parent["reply_to"] or parent["id"]
        author = actors.get(conn, parent["author_id"])
        if author["kind"] != "human" and not author["archived_at"] and author["runtime"] != "service":
            return author["id"], "reply"
        row = conn.execute("""SELECT m.author_id FROM chat_messages m JOIN actors a ON a.id = m.author_id
                              WHERE m.channel_id = ? AND (m.id = ? OR m.reply_to = ?) AND a.kind != 'human'
                              AND a.runtime != 'service' AND a.archived_at IS NULL AND m.archived_at IS NULL
                              ORDER BY m.id DESC LIMIT 1""", (ch["id"], root, root)).fetchone()
        if row:
            return row["author_id"], "reply"
    lead = channel_lead(conn, ch)
    return (lead, "mention") if lead else None  # an addressed message in the inbox (reason CHECK)


def _loop(conn: sqlite3.Connection, ctx: Ctx, ch: sqlite3.Row, others: list[int],
          parent: sqlite3.Row | None = None) -> int | None:
    """Two agents ping-ponging in their DM (loop_limit messages in its window, both writing):
    the other agent's id. The message is kept but wakes nobody, and their lead gets one task to
    sort it out (per window)."""
    n, window = loop_limit()
    since = (datetime.now(timezone.utc) - timedelta(seconds=window)).isoformat(timespec="seconds")
    if ch["kind"] == "dm":
        if len(others) != 1:
            return None
        rows = conn.execute("SELECT author_id FROM chat_messages WHERE channel_id = ? AND created_at >= ? "
                            "AND archived_at IS NULL", (ch["id"], since)).fetchall()
    elif parent is not None:  # two agents answering each other in a group thread
        root = parent["reply_to"] or parent["id"]
        rows = conn.execute("""SELECT author_id FROM chat_messages WHERE channel_id = ? AND (id = ? OR reply_to = ?)
                               AND created_at >= ? AND archived_at IS NULL""", (ch["id"], root, root, since)).fetchall()
    else:
        return None
    authors = {r["author_id"] for r in rows} | {ctx.actor_id}
    if len(rows) + 1 < n or len(authors) != 2:
        return None
    other = actors.get(conn, next(a for a in authors if a != ctx.actor_id))
    if other["kind"] == "human":
        return None
    if not conn.execute("""SELECT 1 FROM audit_log WHERE action = 'chat_loop' AND entity = 'channel'
                           AND entity_id = ? AND at >= ?""", (ch["id"], since)).fetchone():
        _escalate_loop(conn, ctx, ch, other, len(rows), window)
    return other["id"]


def _escalate_loop(conn: sqlite3.Connection, ctx: Ctx, ch: sqlite3.Row, other: sqlite3.Row, n: int,
                   window: int) -> None:
    from . import tasks

    me = actors.get(conn, ctx.actor_id)
    lead_id = me["reports_to"] or other["reports_to"] or role_member(conn, "ceo")
    audit.log(conn, ctx, "chat_loop", "channel", ch["id"], between=[me["id"], other["id"]], messages=n,
              lead=lead_id)
    if not lead_id or lead_id in (me["id"], other["id"]):
        return
    lead = actors.get(conn, lead_id)
    tasks.create(conn, Ctx(lead_id, via="system"), {
        "title": f"Chat loop: {me['name']} ↔ {other['name']}",
        "assignee": {"type": "human" if lead["kind"] == "human" else "agent", "id": lead_id},
        "status": "next", "priority": 2, "topic": "chat-loop", "reviewer": lead_id,
        "notes": (f"Purpose: {me['name']} and {other['name']} exchanged {n} DMs in {window // 60} min "
                  f"(chat channel {ch['id']}). The platform stopped waking them for each other's messages "
                  f"until it calms down.\nSource: loop detection in team chat.\n\nLook at their DM (the chat "
                  f"page, or chat_read), decide what each of them should do and tell them in one message "
                  f"each."),
        "definition_of_done": "Both agents know what to do; the DM is quiet.",
    })


def send(conn: sqlite3.Connection, ctx: Ctx, channel_id: int, body: str, *, reply_to: int | None = None,
         priority: str | None = None, mentions: list[int | str] | None = None,
         attachments: list[dict] | None = None, system: bool = False) -> dict:
    """Post a message. Returns it (as the author sees it) plus `delivered_to_run`
    (the DM recipient's running run, if any) and `inbox` (who got it)."""
    from . import meetings

    body = (body or "").strip()
    if not body:
        raise ChatError("empty message")
    if len(body) > MAX_BODY:
        raise ChatError(f"a message is at most {MAX_BODY} characters")
    if priority is not None and priority not in PRIORITIES:
        raise ChatError(f"priority must be one of {PRIORITIES}")
    author = actors.get(conn, ctx.actor_id)
    if author["kind"] != "human":
        # Tool calls an agent wrote as text never ran; they never reach a person (pos.pseudo_tools).
        from . import pseudo_tools

        if pseudo_tools.contains(body) and not pseudo_tools.clean(body, marker=False):
            raise ChatError("the message was only tool calls written as text; they did not run")
        body = pseudo_tools.clean(body)
        if not body:
            raise ChatError("the message was only tool calls written as text; they did not run")
    ch = _channel(conn, channel_id)
    if ch["archived_at"]:
        raise ChatError("this channel is archived")
    agent_author = author["kind"] != "human" and not system
    if agent_author and len(body) > agent_max_body():
        raise ChatError(f"a chat message from an agent is at most {agent_max_body()} characters: answer in a "
                        f"few sentences and put the details into a task (a comment or its notes) or a note, "
                        f"then link it (T-123)")
    meeting = meetings.for_thread(conn, channel_id, reply_to)
    if meeting is not None and not system:
        meetings.check_turn_message(conn, meeting, ctx.actor_id, body)  # agents speak on their turn only
    if not system:
        _require_send(conn, ctx, channel_id)
        if author["kind"] != "human":
            _check_rate(conn, ctx.actor_id)
            _check_budget(conn, ctx.actor_id)
    if not _is_member(conn, channel_id, ctx.actor_id):
        if ch["kind"] == "group" and (ch["visibility"] != "private" or author["is_owner"]):
            _add_member(conn, channel_id, ctx.actor_id)
        else:
            raise Forbidden("only members post in this channel")
    parent = None
    if reply_to is not None:
        parent = conn.execute("SELECT * FROM chat_messages WHERE id = ?", (reply_to,)).fetchone()
        if parent is None or parent["channel_id"] != channel_id:
            raise ChatError(f"message {reply_to} is not in this channel")
    # A DM is one conversation, like a messenger: an answer goes into it, quoting the message it
    # answers, never into a hidden thread (the owner did not see the CEO's answers on his phone).
    # Threads stay for group channels (and a meeting, which lives in its thread).
    quote_of = None
    if reply_to is not None and ch["kind"] == "dm" and meeting is None:
        quote_of, reply_to, parent = reply_to, None, None
    if not system:
        dup = _duplicate(conn, ctx.actor_id, channel_id, body, reply_to)
        if dup is not None:  # a double submit or a retried call: the message is there already
            return {**message_view(conn, dup, ctx.actor_id), "duplicate": True, "delivered_to_run": None,
                    "inbox": []}

    members = set(member_ids(conn, channel_id))
    mentioned = [m for m in _mentions(conn, body, mentions, channel_id) if m != ctx.actor_id]
    if ch["kind"] == "dm":
        mentioned = [m for m in mentioned if m in members]
    else:
        for m in mentioned:  # agents join a channel when mentioned
            if m not in members and _add_member(conn, channel_id, m):
                audit.log(conn, ctx, "chat_invite", "channel", channel_id, member=m, by_mention=True)
                members.add(m)
    others = [m for m in members if m != ctx.actor_id]
    if priority == "stop" and ch["kind"] == "group" and not mentioned:
        raise ChatError("stop needs a recipient: send it as a DM or @mention who should stop")
    if priority == "stop" and author["kind"] != "human" and not system:
        from .org import manages

        targets = others if ch["kind"] == "dm" else mentioned
        refused = [m for m in targets if not manages(conn, ctx.actor_id, m)]
        if refused:
            names = ", ".join(actors.get(conn, m)["name"] for m in refused)
            raise Forbidden(f"only people and their leads send stop; you do not lead {names}")

    # An acknowledgement ("díky", "ok", "👍") that answers no question wakes nobody, asks for no answer.
    ack = (not system and priority is None and is_ack(body)
           and not _asked_before(conn, channel_id, reply_to, ctx.actor_id))
    looping = _loop(conn, ctx, ch, others, parent) if agent_author and meeting is None else None
    # The owner's message in a group that names nobody still reaches someone (_route_unaddressed).
    routed: dict[int, str] = {}
    if (not system and author["is_owner"] and ch["kind"] == "group" and not mentioned and priority is None
            and not ack and meeting is None and (ch["name"] or "").lower() not in UNROUTED):
        hit = _route_unaddressed(conn, ch, parent)
        if hit and hit[0] != ctx.actor_id:
            routed[hit[0]] = hit[1]
            if hit[0] not in members and _add_member(conn, channel_id, hit[0]):
                members.add(hit[0])

    refs = [{"type": "task", "id": int(n)} for n in dict.fromkeys(_TASK_REF.findall(body))]
    atts = list(attachments or [])
    atts += [r for r in refs if r not in atts]
    row = versioning.insert(conn, ctx, "chat_message", {
        "channel_id": channel_id, "author_id": ctx.actor_id, "body": body, "reply_to": reply_to,
        **({"quote_of": quote_of} if quote_of else {}),
        "mentions": json.dumps(mentioned), "attachments": json.dumps(atts), "priority": priority,
        "trust": _trust(ctx, author), "created_at": now_iso(),
    })
    mid = row["id"]

    # Who gets it in their inbox, and why.
    inbox: dict[int, str] = {}
    if ch["kind"] == "dm":
        inbox.update({m: "dm" for m in others})
    else:
        if priority and priority != "stop":
            inbox.update({m: "priority" for m in others})
        if parent and parent["author_id"] != ctx.actor_id and parent["author_id"] in members:
            inbox[parent["author_id"]] = "reply"
        inbox.update({m: "mention" for m in mentioned})
        for m, why in routed.items():
            inbox.setdefault(m, why)
    targets = others if ch["kind"] == "dm" else mentioned
    # Kept in the chat and the inbox, read already: nobody is woken. A meeting thread wakes nobody either:
    # the platform gives the next speaker the floor (pos.meetings) and later turns read the thread.
    quiet = ack or looping is not None or meeting is not None
    runs = {}
    now = now_iso()
    for aid, reason in inbox.items():
        run = conn.execute("SELECT id FROM runs WHERE actor_id = ? AND status = 'running' ORDER BY id DESC LIMIT 1",
                           (aid,)).fetchone()
        runs[aid] = run["id"] if run else None
        conn.execute("INSERT INTO chat_inbox (message_id, actor_id, reason, run_id, read_at) VALUES (?, ?, ?, ?, ?)",
                     (mid, aid, reason, runs[aid], now if quiet else None))
    conn.execute("UPDATE channel_members SET last_read_message_id = ? WHERE channel_id = ? AND actor_id = ?",
                 (mid, channel_id, ctx.actor_id))
    if reply_to is not None and author["kind"] == "human":  # he wrote in the thread: he has read it
        _thread_read(conn, ctx.actor_id, _thread_of(conn, reply_to) or reply_to, mid)
    audit.log(conn, ctx, "chat_send", "chat_message", mid, channel=channel_id, priority=priority,
              mentions=mentioned, inbox=sorted(inbox), **({"system": True} if system else {}),
              **({"routed": sorted(routed)} if routed else {}), **({"ack": True} if ack else {}),
              **({"loop": True} if looping is not None else {}),
              **({"meeting": meeting["id"]} if meeting is not None else {}))

    platform_note = None
    if not system and author["kind"] != "human":
        from . import business

        owner = actors.owner_id(conn)
        platform_note = business.owner_contact_note(
            conn, ctx, channel_id, owner in (others if ch["kind"] == "dm" else mentioned))
    if not system and author["is_owner"]:
        _owner_dm_interventions(conn, ctx, ch, targets, atts)
    if not system and author["kind"] == "human" and priority != "stop" and not ack and meeting is None:
        for aid in [*targets, *[r for r in routed if r not in targets]]:
            _ask_to_answer(conn, ctx, ch, aid, mid, body, priority)
        if ch["kind"] == "group" and (ch["name"] or "").lower() == "weekly":
            from . import weekly

            weekly.on_owner_message(conn, ctx, ch, mid)  # the weekly meeting's task comes back to its agent
    if not system and author["is_owner"]:
        from . import asks

        asks.on_owner_chat(conn, ctx, channel_id, mid, reply_to, body)  # answers a blocking chat question

    if priority == "stop":
        for aid in targets:
            target = actors.get(conn, aid)
            if target["kind"] == "human":
                continue
            # A stop only pauses: it never archives, deletes or changes permissions.
            runner.cancel_all(conn, f"stop message from {author['name']}: {body[:200]}", actor_id=aid)
            if not target["paused_at"]:
                versioning.update(conn, ctx, "actor", aid, {"paused_at": now_iso()}, action="pause")
    conn.commit()
    typing_clear(channel_id, ctx.actor_id)  # posted: no longer typing here
    if not system and author["kind"] != "human":
        _close_answered(conn, ctx, channel_id, mid)
    if meeting is not None:
        meetings.on_message(conn, meeting, ctx.actor_id, mid, system)  # a turn posted: the next one speaks
    from . import wake

    if not quiet:
        for aid in inbox:  # a waiting worker reads it now, not at its next poll
            wake.wake(aid)
    out = message_view(conn, mid, ctx.actor_id)
    dm_target = others[0] if ch["kind"] == "dm" and others else None
    out["delivered_to_run"] = runs.get(dm_target) if dm_target and not quiet else None
    out["inbox"] = sorted(inbox)
    notes = [platform_note] if platform_note else []
    if agent_author and ack:
        notes.append("An acknowledgement wakes nobody and needs no answer; next time react with chat_react "
                     "(👍) instead of a message.")
    if looping is not None:
        notes.append("You and this agent have been messaging back and forth: your messages no longer wake it "
                     "and your lead got a task to sort it out. Go on with your task; do not answer each "
                     "other's acknowledgements.")
    if agent_author and len(body) > AGENT_SOFT_BODY and meeting is None:
        notes.append(f"Long message ({len(body)} characters): a chat answer is a few sentences; put details "
                     f"into a task comment or a note and link it.")
    if notes:
        out["platform_note"] = " ".join(notes)
    return out


def _owner_dm_interventions(conn: sqlite3.Connection, ctx: Ctx, ch: sqlite3.Row, targets: list[int],
                            atts: list[dict]) -> None:
    """The owner wrote to an agent about its work: an intervention on the task he named (T-123),
    else on the task the agent is working on now (pos.business)."""
    from . import business

    for aid in targets:
        a = actors.get(conn, aid)
        if a["kind"] == "human":
            continue
        named = [x["id"] for x in atts if x.get("type") == "task" and conn.execute(
            "SELECT 1 FROM tasks WHERE id = ? AND assignee_id = ?", (x["id"], aid)).fetchone()]
        if not named:
            cur = conn.execute("SELECT id FROM tasks WHERE assignee_id = ? AND status = 'working' AND archived_at IS NULL "
                               "AND COALESCE(topic, '') != 'chat' ORDER BY updated_at DESC LIMIT 1", (aid,)).fetchone()
            named = [cur["id"]] if cur else []
        for tid in named[:3]:
            business.record_intervention(conn, ctx, tid, "dm")


def _ask_to_answer(conn: sqlite3.Connection, ctx: Ctx, ch: sqlite3.Row, aid: int, message_id: int, body: str,
                   priority: str | None = None) -> None:
    """A person wrote to an agent that answers chat (agent.json answers_chat, e.g.
    the Assistant), or the owner wrote to any agent (the owner is never left
    without an answer; a message with a priority steers work already running and
    is not a question): its worker (own, pool or A2A, pos.workers) gets a task to
    reply in this channel. A service or an agent without a worker answers in code
    and the CEO gets the message (availability.forward_unserved).
    Messages that arrive while that task is still open join it instead of a new
    one. When the agent cannot run now (usage limit, budget, pause, its worker is
    down), the platform answers at once with the reason (pos.availability)."""
    from . import agents_code, availability, comments, tasks, workers

    target = actors.get(conn, aid)
    if target["kind"] == "human" or target["archived_at"]:
        return
    author_row = actors.get(conn, ctx.actor_id)
    to_owner = bool(author_row["is_owner"]) and priority is None
    if not (agents_code.answers_chat(target["name"]) or to_owner):
        return
    path = workers.reply_path(conn, target)
    if path is None:  # a service, or an agent without a worker: never silent (pos.availability)
        if to_owner:
            availability.forward_unserved(conn, ctx, ch, target, message_id, body)
        return
    if path["kind"] == "a2a" and workers.a2a_bridge_off(conn):  # its remote worker is unreachable now
        if to_owner:
            availability.forward_unserved(conn, ctx, ch, target, message_id, body,
                                          why=workers.worker_down(conn, target))
        return
    author = author_row["name"]
    where = f"DM with {author}" if ch["kind"] == "dm" else f"#{ch['name']}"
    title = f"Chat: answer {author} ({where})"
    open_ = conn.execute("""SELECT id FROM tasks WHERE title = ? AND assignee_id = ? AND archived_at IS NULL
                            AND status IN ('inbox', 'next', 'working')""", (title, aid)).fetchone()
    from .guard.external import wrap_external

    said = wrap_external(f"chat:{author}", body, ref=f"message {message_id}")
    if open_:
        comments.log(conn, ctx, open_["id"], f"{author} added (message {message_id}): {body[:1500]}", "comment")
        audit.log(conn, ctx, "chat_task", "task", open_["id"], channel=ch["id"], message=message_id)
        # Stays unread: the task's notes carry only the first message, so the run takes this
        # one from its inbox (_own_question keeps only the first one out).
        availability.autoreply(conn, open_["id"], message_id)
        return
    context = recent_context(conn, ch, message_id)
    notes = (f"Purpose: {author} wrote to you in chat and waits for an answer.\n"
                 f"Source: chat channel {ch['id']} ({where}), message {message_id}.\n\n{said}\n\n"
             + (f"The conversation just before it (oldest first; enough context, no chat_read needed):\n"
                f"{context}\n\n" if context else "")
             + f"Answer once with chat_send(channel={ch['id']}, reply_to={message_id}), in the language they "
             f"wrote in (Czech unless they wrote otherwise): a few sentences; details go into a task or a "
             f"note you link (T-123). Use your tools (tasks, files, the company knowledge base with the "
             f"`knowledge` tool when you have it) to answer well; create tasks when asked to, or delegate to "
             f"the agent whose job it is.")
    msg = conn.execute("SELECT mentions, reply_to FROM chat_messages WHERE id = ?", (message_id,)).fetchone()
    if ch["kind"] == "group" and msg and msg["reply_to"] is None and aid not in json.loads(msg["mentions"] or "[]"):
        notes += (f"\n\nThey addressed nobody by name in {where}; the platform gave it to you as the one "
                  f"responsible there. Answer it, or name in one sentence who takes it (@Name) and hand it on.")
    if author_row["is_owner"] and target["role"] != "ceo":
        notes += ("\n\nChain of command: the owner talks to the CEO. If this is really a company-level request "
                  "(new work across teams, priorities, money, customers, anything beyond your own job), answer "
                  "briefly that the CEO takes it over and hand it to the CEO (send_message to CEO with the "
                  "message id, or handoff_task); do only what is clearly your own job yourself.")
    if path["kind"] == "a2a":  # a remote app: it gets the message itself; pos.a2a posts its answer here
        notes = f"{author} asks in chat ({where}); answer them directly, in the language they wrote in.\n\n{body}"
    t = tasks.create(conn, ctx, {
        "title": title, "assignee": {"type": target["kind"], "id": aid}, "status": "next",
        "priority": 1 if author_row["is_owner"] else 2,
        "topic": "chat",
        "notes": notes,
        "definition_of_done": "The answer is in the chat channel.",
        "reviewer": aid,  # a chat answer needs no review: it is already in front of the person
    })
    audit.log(conn, ctx, "chat_task", "task", t["id"], channel=ch["id"], message=message_id)
    availability.autoreply(conn, t["id"], message_id)


def recent_context(conn: sqlite3.Connection, ch: sqlite3.Row, message_id: int, limit: int = 6,
                   clip: int = 300) -> str:
    """The few messages before this one that it answers (the DM, or its thread), clipped, as one
    block marked untrusted: a chat task carries its context, so the agent needs no chat_read
    (prod: every chat_read read a whole channel, ~26k characters)."""
    from .guard.external import wrap_external

    msg = conn.execute("SELECT reply_to FROM chat_messages WHERE id = ?", (message_id,)).fetchone()
    if msg is None:
        return ""
    if ch["kind"] == "dm":
        rows = conn.execute("""SELECT m.id, m.body, a.name FROM chat_messages m JOIN actors a ON a.id = m.author_id
                               WHERE m.channel_id = ? AND m.id < ? AND m.archived_at IS NULL
                               ORDER BY m.id DESC LIMIT ?""", (ch["id"], message_id, limit)).fetchall()
    elif msg["reply_to"]:
        root = _thread_of(conn, msg["reply_to"])
        rows = conn.execute("""SELECT m.id, m.body, a.name FROM chat_messages m JOIN actors a ON a.id = m.author_id
                               WHERE m.channel_id = ? AND (m.id = ? OR m.reply_to = ?) AND m.id < ?
                               AND m.archived_at IS NULL ORDER BY m.id DESC LIMIT ?""",
                            (ch["id"], root, root, message_id, limit)).fetchall()
    else:
        return ""
    if not rows:
        return ""
    lines = []
    for r in reversed(rows):
        text = " ".join(r["body"].split())
        lines.append(f"- {r['name']} (message {r['id']}): {text[:clip]}{'…' if len(text) > clip else ''}")
    return wrap_external(f"chat:{ch['id']}", "\n".join(lines), ref=f"before message {message_id}")


def _task_question(conn: sqlite3.Connection, task_id: int | None) -> int | None:
    """The message a "Chat: answer" task was created for (its notes carry it); None otherwise."""
    if not task_id:
        return None
    row = conn.execute("""SELECT json_extract(detail, '$.message') FROM audit_log WHERE action = 'chat_task'
                          AND entity = 'task' AND entity_id = ? ORDER BY id LIMIT 1""", (task_id,)).fetchone()
    return row[0] if row and row[0] is not None else None


def take_question(conn: sqlite3.Connection, actor_id: int, task_id: int | None) -> None:
    """A run on a "Chat: answer" task starts: the question it answers is in its prompt, so its
    inbox row is read and acked now. Left unread, the run got its own question injected at its
    first step and answered twice (prod, 2026-09-27: messages 356, 209, 208, 311). Messages
    that joined the task later stay unread: the prompt does not carry them, the run takes them
    from its inbox."""
    mid = _task_question(conn, task_id)
    if mid is None:
        return
    now = now_iso()
    conn.execute("UPDATE chat_inbox SET read_at = COALESCE(read_at, ?), acked_at = COALESCE(acked_at, ?) "
                 "WHERE message_id = ? AND actor_id = ?", (now, now, mid, actor_id))
    conn.commit()


def _own_question(conn: sqlite3.Connection, run_id: int, message_ids: list[int]) -> set[int]:
    """Of these messages, the one the run already has: the question of its own chat task."""
    run = conn.execute("SELECT task_id FROM runs WHERE id = ?", (run_id,)).fetchone()
    mid = _task_question(conn, run["task_id"]) if run is not None else None
    return {mid} & set(message_ids) if mid is not None else set()


def _close_answered(conn: sqlite3.Connection, ctx: Ctx, channel_id: int, reply_id: int) -> None:
    """The agent answered in this channel from a run on other work (a chat message went
    into its running run): its queued "Chat: answer" tasks for messages here, all older
    than this reply, are done, so it does not answer twice."""
    from . import tasks

    for t in conn.execute(
            """SELECT t.id FROM tasks t WHERE t.assignee_id = ? AND t.topic = 'chat' AND t.archived_at IS NULL
               AND t.status IN ('inbox', 'next') AND EXISTS (SELECT 1 FROM audit_log l WHERE l.action = 'chat_task'
               AND l.entity = 'task' AND l.entity_id = t.id AND json_extract(l.detail, '$.channel') = ?)""",
            (ctx.actor_id, channel_id)).fetchall():
        msgs = [r[0] for r in conn.execute(
            "SELECT json_extract(detail, '$.message') FROM audit_log WHERE action = 'chat_task' AND entity = 'task' "
            "AND entity_id = ?", (t["id"],))]
        if msgs and all(m is not None and m < reply_id for m in msgs):
            tasks.update(conn, ctx, t["id"], {"status": "done", "progress_note":
                                              f"Answered in chat (message {reply_id}) during another run."})
            conn.commit()


def send_dm(conn: sqlite3.Connection, ctx: Ctx, to_actor: int, body: str, **kw) -> dict:
    target = actors.get(conn, to_actor)
    if target["archived_at"]:
        raise ChatError(f"{target['name']} is archived")
    ch = dm_channel(conn, ctx.actor_id, to_actor, ctx)
    return send(conn, ctx, ch["id"], body, **kw)


def _message_row(conn: sqlite3.Connection, message_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM chat_messages WHERE id = ?", (message_id,)).fetchone()
    if row is None:
        raise NotFound(f"message {message_id}")
    return row


def edit(conn: sqlite3.Connection, ctx: Ctx, message_id: int, body: str) -> dict:
    """Authors edit their own messages; every version stays in the history."""
    row = _message_row(conn, message_id)
    if row["author_id"] != ctx.actor_id:
        raise Forbidden("only the author edits a message")
    if row["archived_at"]:
        raise ChatError("this message is archived")
    body = (body or "").strip()
    if not body or len(body) > MAX_BODY:
        raise ChatError("empty or too long")
    _require_send(conn, ctx)
    versioning.update(conn, ctx, "chat_message", message_id, {"body": body, "edited_at": now_iso()}, action="edit")
    conn.commit()
    return message_view(conn, message_id, ctx.actor_id)


def archive_message(conn: sqlite3.Connection, ctx: Ctx, message_id: int) -> dict:
    """Deleting a message archives it (the author or the owner)."""
    row = _message_row(conn, message_id)
    if row["author_id"] != ctx.actor_id and not actors.get(conn, ctx.actor_id)["is_owner"]:
        raise Forbidden("only the author or the owner removes a message")
    versioning.archive(conn, ctx, "chat_message", message_id)
    conn.commit()
    return message_view(conn, message_id, ctx.actor_id)


def react(conn: sqlite3.Connection, ctx: Ctx, message_id: int, emoji: str) -> dict:
    """Toggle a reaction. Taking one back archives it."""
    emoji = (emoji or "").strip()
    if not emoji or len(emoji) > 16:
        raise ChatError("one emoji, please")
    row = _message_row(conn, message_id)
    _check_read(conn, _channel(conn, row["channel_id"]), ctx.actor_id)
    _require_send(conn, ctx)
    cur = conn.execute("SELECT archived_at FROM chat_reactions WHERE message_id = ? AND actor_id = ? AND emoji = ?",
                       (message_id, ctx.actor_id, emoji)).fetchone()
    if cur is None:
        conn.execute("INSERT INTO chat_reactions (message_id, actor_id, emoji, created_at) VALUES (?, ?, ?, ?)",
                     (message_id, ctx.actor_id, emoji, now_iso()))
        on = True
    else:
        on = cur["archived_at"] is not None
        conn.execute("UPDATE chat_reactions SET archived_at = ? WHERE message_id = ? AND actor_id = ? AND emoji = ?",
                     (None if on else now_iso(), message_id, ctx.actor_id, emoji))
    audit.log(conn, ctx, "chat_react", "chat_message", message_id, emoji=emoji, on=on)
    conn.commit()
    return message_view(conn, message_id, ctx.actor_id)


def mark_read(conn: sqlite3.Connection, ctx: Ctx, channel_id: int, message_id: int | None = None) -> dict:
    """Move the member's read marker; inbox items up to it count as read."""
    ch = _channel(conn, channel_id)
    _check_read(conn, ch, ctx.actor_id)
    if message_id is None:
        message_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM chat_messages WHERE channel_id = ?",
                                  (channel_id,)).fetchone()[0]
    conn.execute("UPDATE channel_members SET last_read_message_id = MAX(last_read_message_id, ?) "
                 "WHERE channel_id = ? AND actor_id = ?", (message_id, channel_id, ctx.actor_id))
    conn.execute("UPDATE chat_inbox SET read_at = ? WHERE actor_id = ? AND read_at IS NULL AND message_id <= ? "
                 "AND message_id IN (SELECT id FROM chat_messages WHERE channel_id = ?)",
                 (now_iso(), ctx.actor_id, message_id, channel_id))
    conn.commit()
    return {"channel_id": channel_id, "last_read_message_id": message_id}


# ------------------------------------------------------------------ inbox (for workers)

def inbox_unread(conn: sqlite3.Connection, actor_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM chat_inbox i JOIN chat_messages m ON m.id = i.message_id "
        "WHERE i.actor_id = ? AND i.read_at IS NULL AND m.archived_at IS NULL", (actor_id,)).fetchone()[0]


def _wrap_for_agent(row, body: str) -> tuple[str, str]:
    """(body, trust) as an agent sees another member's message (constitution U2)."""
    from .guard.external import wrap_external

    if row["trust"] == "external":
        return wrap_external(f"a2a:{row['from_name']}", body, ref=f"message:{row['id']}"), "external"
    if row["trust"] == "agent":
        return wrap_external(f"agent:{row['from_name']}", body, ref=f"message:{row['id']}"), "agent"
    return body, "member"


def _task_of(attachments: str) -> int | None:
    for a in json.loads(attachments or "[]"):
        if a.get("type") == "task":
            return a.get("id")
    return None


def check_inbox(conn: sqlite3.Connection, actor_id: int, mark_read: bool = True,
                run_id: int | None = None) -> list[dict]:
    """Unread inbox items, most urgent first; bodies from agents and outside wrapped."""
    rows = conn.execute(
        """SELECT m.id, m.body, m.attachments, COALESCE(m.priority, 'fyi') AS priority, m.created_at, m.trust,
                  m.channel_id, m.reply_to, i.reason, i.acked_at, a.name AS from_name, a.kind AS from_kind,
                  c.kind AS channel_kind, c.name AS channel_name
           FROM chat_inbox i JOIN chat_messages m ON m.id = i.message_id
           JOIN actors a ON a.id = m.author_id JOIN channels c ON c.id = m.channel_id
           WHERE i.actor_id = ? AND i.read_at IS NULL AND m.archived_at IS NULL ORDER BY
           CASE COALESCE(m.priority, 'fyi') WHEN 'stop' THEN 0 WHEN 'change_plan' THEN 1 ELSE 2 END, m.id""",
        (actor_id,),
    ).fetchall()
    if rows and mark_read:
        ids = [r["id"] for r in rows]
        conn.execute(
            f"UPDATE chat_inbox SET read_at = ?, delivered_in_run = COALESCE(delivered_in_run, ?) "
            f"WHERE actor_id = ? AND message_id IN ({','.join('?' for _ in ids)})",
            [now_iso(), run_id, actor_id, *ids])
    if rows and run_id:  # the run's own question is in its prompt already: not injected again
        own = _own_question(conn, run_id, [r["id"] for r in rows])
        rows = [r for r in rows if r["id"] not in own]
    out = []
    for r in rows:
        body, trust = _wrap_for_agent(r, r["body"])
        out.append({"id": r["id"], "body": body, "trust": trust, "priority": r["priority"],
                    "created_at": r["created_at"], "acked_at": r["acked_at"], "from_name": r["from_name"],
                    "from_kind": r["from_kind"], "task_id": _task_of(r["attachments"]), "reason": r["reason"],
                    "channel_id": r["channel_id"], "reply_to": r["reply_to"],
                    "channel": f"#{r['channel_name']}" if r["channel_kind"] == "group" else "dm"})
    if run_id and mark_read:
        typing_on_delivery(conn, actor_id, run_id, out)
    return out


def ack(conn: sqlite3.Connection, ctx: Ctx, message_id: int, note: str = "") -> dict:
    row = conn.execute("SELECT m.author_id FROM chat_inbox i JOIN chat_messages m ON m.id = i.message_id "
                       "WHERE i.message_id = ? AND i.actor_id = ?", (message_id, ctx.actor_id)).fetchone()
    if row is None:
        raise NotFound(f"message {message_id}")
    now = now_iso()
    conn.execute("UPDATE chat_inbox SET acked_at = ?, read_at = COALESCE(read_at, ?) "
                 "WHERE message_id = ? AND actor_id = ?", (now, now, message_id, ctx.actor_id))
    audit.log(conn, ctx, "ack_message", "actor", row["author_id"], message_id=message_id, note=note)
    conn.commit()
    return {"id": message_id, "acked": True}


# ------------------------------------------------------------------ views

def _names(conn: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    return {r["id"]: r for r in conn.execute("SELECT id, name, kind, is_owner, archived_at FROM actors")}


def working_ids(conn: sqlite3.Connection) -> list[int]:
    """Members with a live run (the "working" indicator): pos.workers.working_on, the same
    state the Team page shows."""
    from .workers import working_on

    return sorted(working_on(conn))


def _reactions(conn: sqlite3.Connection, ids: list[int]) -> dict[int, list[dict]]:
    out: dict[int, dict[str, list[int]]] = {}
    if not ids:
        return {}
    for r in conn.execute(
            f"SELECT message_id, emoji, actor_id FROM chat_reactions WHERE archived_at IS NULL "
            f"AND message_id IN ({','.join('?' for _ in ids)}) ORDER BY created_at", ids):
        out.setdefault(r["message_id"], {}).setdefault(r["emoji"], []).append(r["actor_id"])
    return {mid: [{"emoji": e, "actors": a, "count": len(a)} for e, a in em.items()] for mid, em in out.items()}


def _view(conn, rows, viewer: int, names, for_agent: bool) -> list[dict]:
    from . import tasks

    ids = [r["id"] for r in rows]
    reacts = _reactions(conn, ids)
    replies = {}
    if ids:
        replies = {r[0]: r[1] for r in conn.execute(
            f"SELECT reply_to, COUNT(*) FROM chat_messages WHERE archived_at IS NULL "
            f"AND reply_to IN ({','.join('?' for _ in ids)}) GROUP BY reply_to", ids)}
    from . import meetings

    marks = meetings.annotations(conn, ids)
    quotes = _quotes(conn, rows, names)
    threads = {} if for_agent else _thread_summaries(conn, [r["id"] for r in rows if replies.get(r["id"])], viewer, names)
    out = []
    for r in rows:
        author = names.get(r["author_id"])
        d = {k: r[k] for k in ("id", "channel_id", "author_id", "reply_to", "priority", "trust", "created_at",
                               "edited_at", "archived_at")}
        d["author_name"] = author["name"] if author else "?"
        d["author_kind"] = author["kind"] if author else "agent"
        d["mentions"] = json.loads(r["mentions"] or "[]")
        d["attachments"] = [{**a, "ref": tasks.display_id(a["id"])} if a.get("type") == "task" else a
                            for a in json.loads(r["attachments"] or "[]")]
        d["reactions"] = reacts.get(r["id"], [])
        d["replies"] = replies.get(r["id"], 0)
        d["quote_of"] = r["quote_of"] if "quote_of" in r.keys() else None
        if d["quote_of"] in quotes:
            d["quote"] = quotes[d["quote_of"]]
        if r["id"] in threads:
            d["thread"] = threads[r["id"]]
        if r["id"] in marks:
            d["meeting"] = marks[r["id"]]
        body = r["body"] if not r["archived_at"] else ""
        if for_agent and r["author_id"] != viewer and body:
            body, d["trust_seen"] = _wrap_for_agent({**dict(r), "from_name": d["author_name"]}, body)
        d["body"] = body
        out.append(d)
    return out


def _snippet(body: str, n: int = 120) -> str:
    text = " ".join((body or "").split())
    return text[:n] + ("…" if len(text) > n else "")


def _quotes(conn: sqlite3.Connection, rows, names) -> dict[int, dict]:
    """The messages quoted by these (a DM answer's quote_of): who wrote them and how they began."""
    ids = sorted({r["quote_of"] for r in rows if "quote_of" in r.keys() and r["quote_of"]})
    if not ids:
        return {}
    out = {}
    for q in conn.execute(f"SELECT id, author_id, body, archived_at FROM chat_messages "
                          f"WHERE id IN ({','.join('?' for _ in ids)})", ids):
        a = names.get(q["author_id"])
        out[q["id"]] = {"id": q["id"], "author_id": q["author_id"], "author_name": a["name"] if a else "?",
                        "body": "" if q["archived_at"] else _snippet(q["body"])}
    return out


def _thread_base(conn: sqlite3.Connection, viewer: int) -> int:
    row = conn.execute("SELECT last_read_id FROM chat_thread_reads WHERE actor_id = ? AND root_id = 0",
                       (viewer,)).fetchone()
    return row["last_read_id"] if row else 0


def _thread_read(conn: sqlite3.Connection, actor_id: int, root_id: int, upto: int) -> None:
    conn.execute("""INSERT INTO chat_thread_reads (actor_id, root_id, last_read_id) VALUES (?, ?, ?)
                    ON CONFLICT (actor_id, root_id) DO UPDATE
                    SET last_read_id = MAX(last_read_id, excluded.last_read_id)""", (actor_id, root_id, upto))


def _thread_summaries(conn: sqlite3.Connection, roots: list[int], viewer: int, names) -> dict[int, dict]:
    """Under a message with replies (a thread in a channel): who answered (the last few, newest
    first), the last reply in one line, when, and how many replies the viewer has not read."""
    if not roots:
        return {}
    marks = ",".join("?" for _ in roots)
    base = _thread_base(conn, viewer)
    reads = {r["root_id"]: r["last_read_id"] for r in conn.execute(
        f"SELECT root_id, last_read_id FROM chat_thread_reads WHERE actor_id = ? AND root_id IN ({marks})",
        [viewer, *roots])}
    out: dict[int, dict] = {}
    for r in conn.execute(f"""SELECT id, reply_to, author_id, body, created_at FROM chat_messages
                              WHERE archived_at IS NULL AND reply_to IN ({marks}) ORDER BY id DESC""", roots):
        seen = reads.get(r["reply_to"], base)
        s = out.setdefault(r["reply_to"], {"repliers": [], "last": None, "unread": 0, "last_read_id": seen})
        a = names.get(r["author_id"])
        if s["last"] is None:
            s["last"] = {"id": r["id"], "author_id": r["author_id"], "author_name": a["name"] if a else "?",
                         "body": _snippet(r["body"], 140), "created_at": r["created_at"]}
        if len(s["repliers"]) < 3 and all(x["id"] != r["author_id"] for x in s["repliers"]):
            s["repliers"].append({"id": r["author_id"], "name": a["name"] if a else "?",
                                  "kind": a["kind"] if a else "agent"})
        if r["id"] > seen and r["author_id"] != viewer:
            s["unread"] += 1
    return out


def threads(conn: sqlite3.Connection, viewer: int, *, unread_only: bool = False, limit: int = 40) -> dict:
    """The threads in the viewer's channels, the latest activity first ("Vlákna"): each root
    message with its summary and channel, and the unread total."""
    limit = max(1, min(limit, 100))
    rows = conn.execute(
        """SELECT r.reply_to AS root, MAX(r.id) AS last_id FROM chat_messages r
           JOIN channels c ON c.id = r.channel_id
           JOIN channel_members cm ON cm.channel_id = c.id AND cm.actor_id = ?
           WHERE r.reply_to IS NOT NULL AND r.archived_at IS NULL AND c.kind = 'group' AND c.archived_at IS NULL
           GROUP BY r.reply_to ORDER BY last_id DESC LIMIT 300""", (viewer,)).fetchall()
    roots = [r["root"] for r in rows]
    names = _names(conn)
    summaries = _thread_summaries(conn, roots, viewer, names)
    total = sum(s["unread"] for s in summaries.values())
    keep = [r for r in roots if not unread_only or summaries.get(r, {}).get("unread")][:limit]
    if not keep:
        return {"threads": [], "unread": total}
    msgs = {r["id"]: r for r in conn.execute(
        f"SELECT * FROM chat_messages WHERE archived_at IS NULL AND id IN ({','.join('?' for _ in keep)})", keep)}
    chans = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM channels WHERE kind = 'group'")}
    views = {v["id"]: v for v in _view(conn, [msgs[i] for i in keep if i in msgs], viewer, names, False)}
    out = []
    for i in keep:
        v = views.get(i)
        if v is None:
            continue
        v["body"] = _snippet(v["body"], 280)
        out.append({"root": v, "channel_id": v["channel_id"], "channel_name": chans.get(v["channel_id"], "?"),
                    "replies": v["replies"], "thread": summaries.get(i)})
    return {"threads": out, "unread": total}


def mark_thread_read(conn: sqlite3.Connection, ctx: Ctx, root_id: int, message_id: int | None = None) -> dict:
    """The viewer read a thread (up to message_id, else all of it)."""
    root = _message_row(conn, root_id)
    _check_read(conn, _channel(conn, root["channel_id"]), ctx.actor_id)
    root_id = root["reply_to"] or root["id"]
    if message_id is None:
        message_id = conn.execute("SELECT COALESCE(MAX(id), ?) FROM chat_messages WHERE reply_to = ?",
                                  (root_id, root_id)).fetchone()[0]
    _thread_read(conn, ctx.actor_id, root_id, message_id)
    conn.commit()
    return {"root": root_id, "last_read_id": message_id}


def message_view(conn: sqlite3.Connection, message_id: int, viewer: int) -> dict:
    row = _message_row(conn, message_id)
    for_agent = actors.get(conn, viewer)["kind"] != "human"
    return _view(conn, [row], viewer, _names(conn), for_agent)[0]


def messages(conn: sqlite3.Connection, viewer: int, channel_id: int, *, before: int | None = None,
             after: int | None = None, limit: int = 50, for_agent: bool | None = None,
             thread: int | None = None, clip: int | None = None) -> dict:
    """A page of a channel, oldest first. `before` pages back, `after` catches up, `thread` keeps
    one thread (its root and replies), `clip` shortens long bodies (an agent's cheap read)."""
    ch = _channel(conn, channel_id)
    _check_read(conn, ch, viewer)
    limit = max(1, min(limit, 200))
    if for_agent is None:
        for_agent = actors.get(conn, viewer)["kind"] != "human"
    where, params = ["channel_id = ?", "archived_at IS NULL"], [channel_id]
    if thread:
        root = _thread_of(conn, thread) or thread
        where.append("(id = ? OR reply_to = ?)")
        params += [root, root]
    if before:
        where.append("id < ?")
        params.append(before)
    if after:
        where.append("id > ?")
        params.append(after)
    order = "ASC" if after else "DESC"
    rows = conn.execute(f"SELECT * FROM chat_messages WHERE {' AND '.join(where)} ORDER BY id {order} LIMIT ?",
                        [*params, limit + 1]).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    if order == "DESC":
        rows = list(reversed(rows))
    if clip:
        rows = [{**dict(r), "body": r["body"][:clip] + f"… [clipped: chat_read(thread={r['id']}, full=true)]"}
                if len(r["body"] or "") > clip else r for r in rows]
    return {"channel_id": channel_id, "messages": _view(conn, rows, viewer, _names(conn), for_agent),
            "has_more": has_more}


def history(conn: sqlite3.Connection, viewer: int, message_id: int) -> list[dict]:
    row = _message_row(conn, message_id)
    _check_read(conn, _channel(conn, row["channel_id"]), viewer)
    return [{"version": h["version"], "action": h["action"], "at": h["at"], "actor_name": h["actor_name"],
             "body": h["data"].get("body")} for h in versioning.history(conn, "chat_message", message_id)]


def channel_view(conn: sqlite3.Connection, channel_id: int, viewer: int, names=None, working=None,
                 current=None) -> dict:
    from . import fastlane

    ch = _channel(conn, channel_id)
    names = names or _names(conn)
    working = set(working if working is not None else working_ids(conn))
    current = current if current is not None else fastlane.current_work(conn)
    mem = conn.execute("SELECT actor_id, role, last_read_message_id FROM channel_members WHERE channel_id = ?",
                       (channel_id,)).fetchall()
    mine = next((m for m in mem if m["actor_id"] == viewer), None)
    last_read = mine["last_read_message_id"] if mine else None
    unread = mentions = 0
    if last_read is not None:
        unread = conn.execute("SELECT COUNT(*) FROM chat_messages WHERE channel_id = ? AND id > ? "
                              "AND author_id != ? AND archived_at IS NULL",
                              (channel_id, last_read, viewer)).fetchone()[0]
        if unread:
            mentions = conn.execute(
                "SELECT COUNT(*) FROM chat_messages m, json_each(m.mentions) j WHERE m.channel_id = ? AND m.id > ? "
                "AND m.archived_at IS NULL AND j.value = ?", (channel_id, last_read, viewer)).fetchone()[0]
    last = conn.execute("SELECT id, author_id, body, created_at FROM chat_messages WHERE channel_id = ? "
                        "AND archived_at IS NULL ORDER BY id DESC LIMIT 1", (channel_id,)).fetchone()
    members = [{"id": m["actor_id"], "name": names[m["actor_id"]]["name"], "kind": names[m["actor_id"]]["kind"],
                "is_owner": bool(names[m["actor_id"]]["is_owner"]), "role": m["role"],
                "working": m["actor_id"] in working, "current": current.get(m["actor_id"])}
               for m in mem if m["actor_id"] in names]
    if ch["kind"] == "dm":
        other = [m for m in members if m["id"] != viewer] or members
        title = " · ".join(m["name"] for m in other) if mine else " ↔ ".join(m["name"] for m in members)
    else:
        title = f"#{ch['name']}"
    return {
        "id": ch["id"], "kind": ch["kind"], "name": ch["name"], "title": title, "topic": ch["topic"],
        "visibility": ch["visibility"], "created_by": ch["created_by"], "created_at": ch["created_at"],
        "archived_at": ch["archived_at"], "member": mine is not None, "members": members,
        "unread": unread, "mentions": mentions, "last_read_message_id": last_read,
        "typing": typing_view(conn, viewer, channel_id).get(str(channel_id), []),
        "last": {"id": last["id"], "author_name": names[last["author_id"]]["name"], "body": last["body"][:140],
                 "created_at": last["created_at"]} if last else None,
    }


def list_channels(conn: sqlite3.Connection, viewer: int, include_all: bool = False) -> list[dict]:
    """Channels the viewer is in, plus open groups. The owner with include_all
    also sees other members' DMs (read-only oversight)."""
    is_owner = actors.get(conn, viewer)["is_owner"]
    rows = conn.execute(
        """SELECT c.id FROM channels c WHERE c.archived_at IS NULL AND (
               EXISTS (SELECT 1 FROM channel_members m WHERE m.channel_id = c.id AND m.actor_id = ?)
               OR (c.kind = 'group' AND c.visibility != 'private') OR ?)
           ORDER BY COALESCE((SELECT MAX(id) FROM chat_messages x WHERE x.channel_id = c.id), 0) DESC, c.id""",
        (viewer, 1 if include_all and is_owner else 0)).fetchall()
    from . import fastlane

    names, working, current = _names(conn), working_ids(conn), fastlane.current_work(conn)
    return [channel_view(conn, r["id"], viewer, names, working, current) for r in rows]


def members_overview(conn: sqlite3.Connection) -> list[dict]:
    """Everyone who can chat, with the working indicator (for @autocomplete)."""
    from . import fastlane

    working, current = set(working_ids(conn)), fastlane.current_work(conn)
    return [{"id": r["id"], "name": r["name"], "kind": r["kind"], "is_owner": bool(r["is_owner"]),
             "remote": bool(r["a2a_url"]), "paused": bool(r["paused_at"]), "working": r["id"] in working,
             "current": current.get(r["id"]), "is_ceo": r["role"] == "ceo" and r["kind"] != "human"}
            for r in conn.execute("SELECT * FROM actors WHERE archived_at IS NULL AND runtime != 'service' "
                                  "ORDER BY is_owner DESC, name")]  # services are no one to chat with


def conversation(conn: sqlite3.Connection, actor_id: int, limit: int = 50) -> list[dict]:
    """A member's inbox traffic in both directions (the agent page's Messages panel)."""
    rows = conn.execute(
        """SELECT m.id, m.author_id AS from_actor, i.actor_id AS to_actor, f.name AS from_name, t.name AS to_name,
                  m.body, COALESCE(m.priority, 'fyi') AS priority, m.created_at, i.read_at, i.acked_at,
                  m.channel_id, i.reason
           FROM chat_inbox i JOIN chat_messages m ON m.id = i.message_id
           JOIN actors f ON f.id = m.author_id JOIN actors t ON t.id = i.actor_id
           WHERE (i.actor_id = ? OR m.author_id = ?) AND m.archived_at IS NULL ORDER BY m.id DESC LIMIT ?""",
        (actor_id, actor_id, limit)).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ typing indicator
#
# In memory only, never in the message history. People: the composer pings
# /typing at most every 3 s and the entry lives HUMAN_TYPING_S. Agents: the
# platform marks them itself, no tool call and no tokens: a worker run that
# starts on a chat task, or gets a DM, mention or thread reply mid-run, types
# in that channel (and thread). The run's step heartbeat keeps it "typing";
# the worker's alive tick keeps a softer "working" (long tool work); it clears
# when the agent posts there, when the run ends, or AGENT_TYPING_S after the
# last sign of life, so a crashed run never leaves a stuck indicator.

HUMAN_TYPING_S = 6
AGENT_TYPING_S = 30
TYPING_S = HUMAN_TYPING_S  # kept for older callers
_typing: dict[int, dict[int, dict]] = {}  # channel -> actor -> entry
_typing_lock = threading.Lock()
_CHAT_ORIGIN = re.compile(r"Source: chat channel (\d+) \(.*?\), message (\d+)\.")


def _thread_of(conn: sqlite3.Connection, message_id: int | None) -> int | None:
    """The thread a reply to this message lands in (threads are one level deep)."""
    if not message_id:
        return None
    row = conn.execute("SELECT reply_to FROM chat_messages WHERE id = ?", (message_id,)).fetchone()
    return (row["reply_to"] or message_id) if row else None


def typing(channel_id: int, actor_id: int, thread: int | None = None) -> None:
    """A person is typing (the composer's throttled ping)."""
    with _typing_lock:
        _typing.setdefault(channel_id, {})[actor_id] = {"kind": "human", "at": time.monotonic(), "thread": thread}


def agent_typing(channel_id: int, actor_id: int, run_id: int | None, thread: int | None = None) -> None:
    """An agent's run is working on a reply here."""
    now = time.monotonic()
    with _typing_lock:
        _typing.setdefault(channel_id, {})[actor_id] = {"kind": "agent", "at": now, "alive": now,
                                                        "run_id": run_id, "thread": thread}


def typing_run_step(run_id: int) -> None:
    """The run completed a step (worker heartbeat): its agent is typing."""
    now = time.monotonic()
    with _typing_lock:
        for who in _typing.values():
            for e in who.values():
                if e.get("run_id") == run_id:
                    e["at"] = e["alive"] = now


def typing_run_alive(run_id: int) -> None:
    """The run's worker is alive but between steps (long tool work)."""
    now = time.monotonic()
    with _typing_lock:
        for who in _typing.values():
            for e in who.values():
                if e.get("run_id") == run_id:
                    e["alive"] = now


def typing_clear(channel_id: int | None = None, actor_id: int | None = None, run_id: int | None = None) -> None:
    """Drop entries: an actor in a channel (it posted), or all of a run (it ended)."""
    with _typing_lock:
        for cid in list(_typing):
            who = _typing[cid]
            for aid in list(who):
                e = who[aid]
                if (run_id is not None and e.get("run_id") == run_id) or (
                        run_id is None and aid == actor_id and cid == channel_id):
                    del who[aid]
            if not who:
                del _typing[cid]


def _state(e: dict, now: float) -> str | None:
    if e["kind"] == "human":
        return "typing" if now - e["at"] < HUMAN_TYPING_S else None
    if now - e["at"] < AGENT_TYPING_S:
        return "typing"
    return "working" if now - e["alive"] < AGENT_TYPING_S else None


def typing_now() -> dict[str, list[int]]:
    """{channel id: [actor ids]} typing or working now (no visibility filter)."""
    now = time.monotonic()
    with _typing_lock:
        return {str(c): sorted(a for a, e in who.items() if _state(e, now))
                for c, who in _typing.items() if any(_state(e, now) for e in who.values())}


def typing_view(conn: sqlite3.Connection, viewer: int, channel_id: int | None = None) -> dict[str, list[dict]]:
    """Who is typing, per channel the viewer may read (never the viewer itself).
    Each entry: id, name, kind, state ("typing" or "working"), thread (root message id or null)."""
    now = time.monotonic()
    with _typing_lock:
        for cid in list(_typing):  # expire as we go: the store never grows
            who = _typing[cid]
            for aid in [a for a, e in who.items() if not _state(e, now)]:
                del who[aid]
            if not who:
                del _typing[cid]
        snap = {c: {a: (_state(e, now), e.get("thread")) for a, e in who.items()} for c, who in _typing.items()
                if channel_id is None or c == channel_id}
    if not snap:
        return {}
    names = _names(conn)
    out: dict[str, list[dict]] = {}
    for cid, who in snap.items():
        try:
            if not can_read(conn, _channel(conn, cid), viewer):
                continue
        except NotFound:
            continue
        rows = [{"id": a, "name": names[a]["name"], "kind": names[a]["kind"], "state": st, "thread": th}
                for a, (st, th) in sorted(who.items()) if a != viewer and a in names]
        if rows:
            out[str(cid)] = rows
    return out


def chat_origin(conn: sqlite3.Connection, task_id: int) -> tuple[int, int] | None:
    """(channel id, message id) when the task is a chat answer (`_ask_to_answer`)."""
    row = conn.execute("SELECT notes FROM tasks WHERE id = ?", (task_id,)).fetchone()
    m = _CHAT_ORIGIN.search(row["notes"] or "") if row else None
    return (int(m.group(1)), int(m.group(2))) if m else None


def typing_on_run_start(conn: sqlite3.Connection, actor_id: int, run_id: int, task_id: int | None) -> None:
    """A run on a chat task starts: its agent types in that channel and thread."""
    origin = chat_origin(conn, task_id) if task_id else None
    if origin:
        agent_typing(origin[0], actor_id, run_id, _thread_of(conn, origin[1]))


def typing_on_delivery(conn: sqlite3.Connection, actor_id: int, run_id: int, items: list[dict]) -> None:
    """A running run gets a DM, mention or thread reply: it will answer, so it types there."""
    for m in items:
        if m.get("reason") in ("dm", "mention", "reply", "routed") and m.get("channel_id"):
            agent_typing(m["channel_id"], actor_id, run_id, m.get("reply_to"))


# ------------------------------------------------------------------ live stream (SSE)

_STREAM_ACTIONS = {("chat_message", "create"): "message", ("chat_message", "edit"): "edit",
                   ("chat_message", "archive"): "archive", ("chat_message", "chat_react"): "reaction",
                   ("channel", "create"): "channel", ("channel", "chat_invite"): "channel",
                   ("channel", "archive"): "channel"}


def cursor_now(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(MAX(id), 0) FROM audit_log").fetchone()[0]


def changes(conn: sqlite3.Connection, viewer: int, cursor: int, limit: int = 200) -> tuple[list[dict], int]:
    """Chat events after an audit-log cursor, as `viewer` may see them."""
    rows = conn.execute(
        "SELECT id, action, entity, entity_id FROM audit_log WHERE id > ? AND entity IN ('chat_message', 'channel') "
        "ORDER BY id LIMIT ?", (cursor, limit)).fetchall()
    events = []
    for r in rows:
        cursor = r["id"]
        kind = _STREAM_ACTIONS.get((r["entity"], r["action"]))
        if not kind:
            continue
        try:
            if r["entity"] == "chat_message":
                msg = message_view(conn, r["entity_id"], viewer)
                if not can_read(conn, _channel(conn, msg["channel_id"]), viewer):
                    continue
                events.append({"id": r["id"], "type": kind, "channel_id": msg["channel_id"], "message": msg})
            else:
                ch = _channel(conn, r["entity_id"])
                if can_read(conn, ch, viewer):
                    events.append({"id": r["id"], "type": kind, "channel_id": ch["id"]})
        except NotFound:
            continue
    if len(rows) < limit:
        cursor = max(cursor, conn.execute("SELECT COALESCE(MAX(id), 0) FROM audit_log").fetchone()[0])
    return events, cursor


def _sse(event: str, data, event_id: int | None = None) -> str:
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


async def stream(db_path: Path, viewer: int, cursor: int | None = None, *, poll: float = 1.0,
                 timeout: float | None = None, disconnected=None):
    """Server-Sent Events: chat changes plus presence (working agents, typing)."""
    from .db import connect

    start = time.monotonic()
    last_presence, last_ping = None, start
    yield "retry: 3000\n\n"
    while True:
        conn = connect(db_path)
        try:
            if cursor is None:
                cursor = cursor_now(conn)
            events, cursor = changes(conn, viewer, cursor)
            presence = {"working": working_ids(conn), "typing": typing_view(conn, viewer)}
        finally:
            conn.close()
        for ev in events:
            yield _sse(ev["type"], ev, ev["id"])
        if presence != last_presence:
            last_presence = presence
            yield _sse("presence", presence)
        now = time.monotonic()
        if now - last_ping > 15:
            last_ping = now
            yield ": ping\n\n"
        if timeout is not None and now - start >= timeout:
            return
        if disconnected is not None and await disconnected():
            return
        await asyncio.sleep(poll)
