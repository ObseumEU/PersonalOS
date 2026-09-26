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
    agent answers the person who wrote to it (pos.workers)."""
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


def ensure_team_channel(conn: sqlite3.Connection) -> int:
    """#team with the owner and every active agent; safe to call at every start."""
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
    for a in conn.execute("SELECT id FROM actors WHERE archived_at IS NULL AND (kind != 'human' OR is_owner = 1)"):
        _add_member(conn, cid, a["id"])
    conn.commit()
    return cid


def post_to_team(conn: sqlite3.Connection, author_id: int, body: str, priority: str | None = None) -> dict:
    """Platform posts to #team on a member's behalf (the PM's standup, HR's
    check). Audited like any message; not an agent tool, so no rate limit."""
    cid = ensure_team_channel(conn)
    _add_member(conn, cid, author_id)
    return send(conn, Ctx(author_id, via="system"), cid, body, priority=priority, system=True)


# ------------------------------------------------------------------ messages

def _mentions(conn: sqlite3.Connection, body: str, extra: list[int | str] | None) -> list[int]:
    """@Name anywhere in the body (names may contain spaces; longest wins)."""
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


def send(conn: sqlite3.Connection, ctx: Ctx, channel_id: int, body: str, *, reply_to: int | None = None,
         priority: str | None = None, mentions: list[int | str] | None = None,
         attachments: list[dict] | None = None, system: bool = False) -> dict:
    """Post a message. Returns it (as the author sees it) plus `delivered_to_run`
    (the DM recipient's running run, if any) and `inbox` (who got it)."""
    body = (body or "").strip()
    if not body:
        raise ChatError("empty message")
    if len(body) > MAX_BODY:
        raise ChatError(f"a message is at most {MAX_BODY} characters")
    if priority is not None and priority not in PRIORITIES:
        raise ChatError(f"priority must be one of {PRIORITIES}")
    author = actors.get(conn, ctx.actor_id)
    ch = _channel(conn, channel_id)
    if ch["archived_at"]:
        raise ChatError("this channel is archived")
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

    members = set(member_ids(conn, channel_id))
    mentioned = [m for m in _mentions(conn, body, mentions) if m != ctx.actor_id]
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

    refs = [{"type": "task", "id": int(n)} for n in dict.fromkeys(_TASK_REF.findall(body))]
    atts = list(attachments or [])
    atts += [r for r in refs if r not in atts]
    row = versioning.insert(conn, ctx, "chat_message", {
        "channel_id": channel_id, "author_id": ctx.actor_id, "body": body, "reply_to": reply_to,
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
    targets = others if ch["kind"] == "dm" else mentioned
    runs = {}
    for aid, reason in inbox.items():
        run = conn.execute("SELECT id FROM runs WHERE actor_id = ? AND status = 'running' ORDER BY id DESC LIMIT 1",
                           (aid,)).fetchone()
        runs[aid] = run["id"] if run else None
        conn.execute("INSERT INTO chat_inbox (message_id, actor_id, reason, run_id) VALUES (?, ?, ?, ?)",
                     (mid, aid, reason, runs[aid]))
    conn.execute("UPDATE channel_members SET last_read_message_id = ? WHERE channel_id = ? AND actor_id = ?",
                 (mid, channel_id, ctx.actor_id))
    audit.log(conn, ctx, "chat_send", "chat_message", mid, channel=channel_id, priority=priority,
              mentions=mentioned, inbox=sorted(inbox))

    if not system and author["kind"] == "human" and priority != "stop":
        for aid in targets:
            _ask_to_answer(conn, ctx, ch, aid, mid, body, priority)
        if ch["kind"] == "group" and (ch["name"] or "").lower() == "weekly":
            from . import weekly

            weekly.on_owner_message(conn, ctx, ch, mid)  # the weekly meeting's task comes back to its agent

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
    from . import wake

    for aid in inbox:  # a waiting worker reads it now, not at its next poll
        wake.wake(aid)
    out = message_view(conn, mid, ctx.actor_id)
    dm_target = others[0] if ch["kind"] == "dm" and others else None
    out["delivered_to_run"] = runs.get(dm_target) if dm_target else None
    out["inbox"] = sorted(inbox)
    return out


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
        availability.autoreply(conn, open_["id"], message_id)
        return
    notes = (f"Purpose: {author} wrote to you in chat and waits for an answer.\n"
                 f"Source: chat channel {ch['id']} ({where}), message {message_id}.\n\n{said}\n\n"
             f"Answer with chat_send(channel={ch['id']}, reply_to={message_id}), in the language they wrote "
             f"in (Czech unless they wrote otherwise); read the thread with chat_read if you need context. "
             f"Use your tools (tasks, files, the knowledge base via ask_agent 'Knowledge agent') to answer "
             f"well; create tasks when asked to, or delegate to the agent whose job it is.")
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
    """Members with a running run (the "working" indicator)."""
    return [r[0] for r in conn.execute("SELECT DISTINCT actor_id FROM runs WHERE status = 'running'")]


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
        body = r["body"] if not r["archived_at"] else ""
        if for_agent and r["author_id"] != viewer and body:
            body, d["trust_seen"] = _wrap_for_agent({**dict(r), "from_name": d["author_name"]}, body)
        d["body"] = body
        out.append(d)
    return out


def message_view(conn: sqlite3.Connection, message_id: int, viewer: int) -> dict:
    row = _message_row(conn, message_id)
    for_agent = actors.get(conn, viewer)["kind"] != "human"
    return _view(conn, [row], viewer, _names(conn), for_agent)[0]


def messages(conn: sqlite3.Connection, viewer: int, channel_id: int, *, before: int | None = None,
             after: int | None = None, limit: int = 50, for_agent: bool | None = None) -> dict:
    """A page of a channel, oldest first. `before` pages back, `after` catches up."""
    ch = _channel(conn, channel_id)
    _check_read(conn, ch, viewer)
    limit = max(1, min(limit, 200))
    if for_agent is None:
        for_agent = actors.get(conn, viewer)["kind"] != "human"
    where, params = ["channel_id = ?", "archived_at IS NULL"], [channel_id]
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
             "current": current.get(r["id"])}
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
        if m.get("reason") in ("dm", "mention", "reply") and m.get("channel_id"):
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
