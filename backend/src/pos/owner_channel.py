"""The owner's channel: how agents' chat reaches the owner (called from pos.chat.send).

Prod 2026-09-25..10-04: only 13 of 26 owner requests were delivered. What this fixes:

- **Archived members** (`reroute_archived`): a DM to an archived agent went nowhere (#903
  "at to dodelaj!!" to the archived Asistent vedení). It goes to the successor (docs/REORG.md,
  pos.reorg.SUCCESSORS) or the CEO, with a visible note in the message; the archived DM is
  read-only in the UI (`read_only` in pos.chat.channel_view).
- **One answer per owner message** (`merge_target`): 21 owner messages got 2+ replies from the same
  agent (5 to #299; #358 and #359 identical). The agent's first reply after the owner's message is
  the answer; for ANSWER_MERGE_MIN after it, the same agent's further replies are added to it
  (an edit) unless they carry a new file.
- **References he can open** (`rewrite_refs`): 78 % of the CEO's messages to him carried task or
  note ids. Ids in forms the app does not link ("poznámka id 19", "message_id=1095", a bare
  "/report/T-401") become the reference chips or links of the app (web/src/refs.ts, chat/Rich.tsx);
  ids with nothing that says what they are, chunk ids and home-network URLs give the agent a
  warning in its run (the chat_send result's platform_note).
"""

import re
import sqlite3
from datetime import datetime, timezone

from . import actors, audit, versioning
from .core import Ctx, now_iso

ANSWER_MERGE_MIN = 10


# ------------------------------------------------------------------ who the owner's message reaches

def reaches_owner(conn: sqlite3.Connection, ch: sqlite3.Row, members: set[int], mentioned: list[int]) -> bool:
    owner = actors.owner_id(conn)
    return owner in members if ch["kind"] == "dm" else owner in mentioned


# ------------------------------------------------------------------ archived members

def reroute_archived(conn: sqlite3.Connection, ctx: Ctx, ch: sqlite3.Row) -> tuple[sqlite3.Row, dict] | None:
    """A message into a DM whose other member is archived: (the DM with its successor, info)."""
    if ch["kind"] != "dm":
        return None
    from . import chat, notices

    others = [m for m in chat.member_ids(conn, ch["id"]) if m != ctx.actor_id]
    if len(others) != 1:
        return None
    old = actors.get(conn, others[0])
    if not old["archived_at"]:
        return None
    new = notices.successor(conn, old["id"])
    if not new or new == ctx.actor_id:
        return None
    new_ch = chat.dm_channel(conn, ctx.actor_id, new, ctx)
    return new_ch, {"from": old["id"], "from_name": old["name"], "to": new,
                    "to_name": actors.get(conn, new)["name"], "channel": ch["id"]}


def reroute_note(info: dict) -> str:
    return (f"\n\n_(Původně pro {info['from_name']}, který je archivovaný; zprávu přebírá {info['to_name']}.)_")


def archived_dm(conn: sqlite3.Connection, ch: sqlite3.Row, viewer: int) -> dict | None:
    """For the UI: a DM with an archived member is read-only, with where to write instead."""
    if ch["kind"] != "dm":
        return None
    from . import chat, notices

    others = [m for m in chat.member_ids(conn, ch["id"]) if m != viewer]
    if len(others) != 1:
        return None
    a = actors.get(conn, others[0])
    if not a["archived_at"]:
        return None
    new = notices.successor(conn, a["id"])
    return {"archived": a["name"], "successor_id": new,
            "successor_name": actors.get(conn, new)["name"] if new else None}


# ------------------------------------------------------------------ one answer per owner message

def _minutes_ago(iso: str) -> float:
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return 1e9
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds() / 60


def merge_target(conn: sqlite3.Connection, ctx: Ctx, ch: sqlite3.Row, reply_to: int | None, quote_of: int | None,
                 attachments: list[dict] | None) -> int | None:
    """The agent's first answer to the owner's latest message here, when this new message should be
    added to it instead of becoming a second answer; None otherwise."""
    if any(isinstance(a, dict) and a.get("type") == "file" for a in attachments or []):
        return None  # a new file or result is its own message
    author = actors.get(conn, ctx.actor_id)
    if author["kind"] == "human" or actors.is_system(conn, ctx.actor_id):
        return None
    owner = actors.owner_id(conn)
    from . import chat

    if ch["kind"] == "dm":
        if owner not in chat.member_ids(conn, ch["id"]):
            return None
        last = conn.execute("""SELECT id FROM chat_messages WHERE channel_id = ? AND author_id = ?
                               AND archived_at IS NULL ORDER BY id DESC LIMIT 1""", (ch["id"], owner)).fetchone()
        if last is None or (quote_of and quote_of != last["id"]):
            return None  # it answers an older message on purpose (a quote): its own reply
        first = conn.execute("""SELECT id, created_at FROM chat_messages WHERE channel_id = ? AND author_id = ?
                                AND id > ? AND archived_at IS NULL ORDER BY id LIMIT 1""",
                             (ch["id"], ctx.actor_id, last["id"])).fetchone()
    else:
        if not reply_to:
            return None
        root = chat._thread_of(conn, reply_to) or reply_to
        last = conn.execute("""SELECT id FROM chat_messages WHERE channel_id = ? AND author_id = ?
                               AND (id = ? OR reply_to = ?) AND archived_at IS NULL ORDER BY id DESC LIMIT 1""",
                            (ch["id"], owner, root, root)).fetchone()
        if last is None:
            return None
        first = conn.execute("""SELECT id, created_at FROM chat_messages WHERE channel_id = ? AND author_id = ?
                                AND reply_to = ? AND id > ? AND archived_at IS NULL ORDER BY id LIMIT 1""",
                             (ch["id"], ctx.actor_id, root, last["id"])).fetchone()
    if first is None or _minutes_ago(first["created_at"]) > ANSWER_MERGE_MIN:
        return None
    if conn.execute("SELECT 1 FROM chat_messages WHERE id = ? AND attachments LIKE '%\"file\"%'",
                    (first["id"],)).fetchone():
        return None  # the first answer carries a file: it stays as it is
    return first["id"]


SAME = object()  # merge(): the new text is already in the first answer


def merge(conn: sqlite3.Connection, ctx: Ctx, message_id: int, body: str, max_body: int):
    """Join the new words to the first answer: the first answer is archived and the caller posts the
    joined text as the one answer (so the owner sees it as new, once). Returns the joined body,
    SAME when the text is already there, None when it does not fit (then it is its own message)."""
    old = conn.execute("SELECT body FROM chat_messages WHERE id = ?", (message_id,)).fetchone()["body"] or ""
    extra = (body or "").strip()
    if not extra or extra in old:
        audit.log(conn, ctx, "chat_answer_merge", "chat_message", message_id, added=0)
        return SAME
    merged = f"{old.rstrip()}\n\n{extra}"
    if len(merged) > max_body:
        return None
    versioning.update(conn, ctx, "chat_message", message_id, {"archived_at": now_iso()}, action="answer_merge")
    audit.log(conn, ctx, "chat_answer_merge", "chat_message", message_id, added=len(extra))
    return merged


MERGE_NOTE = ("One answer per owner message: your text was joined with your first answer to him into one "
              "message, not sent as a second one. Answer once, completely; a new file or result is its own message.")


# ------------------------------------------------------------------ references the owner can open

_ID_NOTE = re.compile(r"(?i)\b(?:pozn(?:á|a)mk[aeuyo]\w*|note)[\s_]*(?:id|č\.?\s*id)\s*[:=#]?\s*(\d{1,6})\b")
_ID_NOTE2 = re.compile(r"(?i)\bnote_id\s*[:=]?\s*(\d{1,6})\b")
_ID_MSG = re.compile(r"(?i)\b(?:zpr(?:á|a)v[aěuy]?|message|msg)[\s_]*id\s*[:=#]?\s*(\d{1,9})\b")
_ID_TASK = re.compile(r"(?i)\btask[\s_]*id\s*[:=#]?\s*(\d{1,6})\b")
# A bare app path ("/report/T-401", "/chat?c=1&m=2", "/tasks?task=T-4") not already inside a Markdown link.
_PATH = re.compile(r"(?<![\w(\]/.:])(/(?:report|reports|tasks|chat|notes|projects|team|files)"
                   r"(?:[/?][^\s)\];,]*[^\s)\];,.!?:])?)")
_PRIVATE_URL = re.compile(
    r"(?i)\bhttps?://(?:localhost|127\.0\.0\.1|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+|"
    r"[\w.-]+\.(?:local|lan|home\.arpa|internal))(?::\d+)?[^\s)]*")
_CHUNK = re.compile(r"(?<![\w/.:#-])((?:[a-z][a-z0-9]{1,20}\.)?[A-Za-z0-9_-]{6,64}):c\d{1,5}\b")
_TASK = re.compile(r"(?<![\w-])T-(\d{1,6})\b")
_STOP = {"a", "i", "k", "s", "v", "z", "na", "do", "pro", "the", "of", "to", "and", "o", "se", "je"}


def _path_label(path: str) -> str:
    m = re.match(r"/report/(T-\d+)", path)
    if m:
        return f"report {m.group(1)}"
    if path.startswith("/chat"):
        return "zpráva v chatu"
    if path.startswith("/tasks"):
        t = re.search(r"T-\d+", path)
        return t.group(0) if t else "úkoly"
    return path.strip("/").split("?")[0].replace("/", " ") or "odkaz"


def _named(body: str, title: str) -> bool:
    """Does the message say in words what the task is (some significant word of its title)?"""
    words = [w for w in re.findall(r"[\wÀ-ž]{4,}", (title or "").lower()) if w not in _STOP]
    low = body.lower()
    return not words or any(w[:6] in low for w in words[:6])


def rewrite_refs(conn: sqlite3.Connection, body: str) -> tuple[str, list[str]]:
    """(the body with ids turned into chips/links the owner can open, warnings for the agent)."""
    out = _ID_NOTE.sub(lambda m: f"poznámka {m.group(1)}", body)
    out = _ID_NOTE2.sub(lambda m: f"poznámka {m.group(1)}", out)
    out = _ID_MSG.sub(lambda m: f"zpráva {m.group(1)}", out)
    out = _ID_TASK.sub(lambda m: f"T-{int(m.group(1)):03d}", out)
    out = _PATH.sub(lambda m: f"[{_path_label(m.group(1))}]({m.group(1)})", out)
    warnings = []
    private = sorted(set(_PRIVATE_URL.findall(out)))
    if private:
        warnings.append(f"The owner cannot open {', '.join(private[:3])} outside the home network: give what it "
                        "shows in words, or share a file or a PersonalOS link instead.")
    if _CHUNK.search(out):
        warnings.append("Chunk ids (doc:c12) mean nothing to the owner: quote the passage or name the document.")
    bare = []
    for n in dict.fromkeys(_TASK.findall(out)):
        row = conn.execute("SELECT title FROM tasks WHERE id = ?", (int(n),)).fetchone()
        if row is not None and not _named(out, row["title"]):
            bare.append(f"T-{int(n):03d}")
    if bare:
        warnings.append(f"You wrote {', '.join(bare[:5])} without saying what it is: the owner does not know task "
                        "ids. Name the thing in words (the id may follow in brackets).")
    return out, warnings


def owner_note(warnings: list[str]) -> str | None:
    return ("To the owner: " + " ".join(warnings)) if warnings else None
