"""What needs the signed-in member now ("Čeká na tebe"): one list, one count.

The Home inbox, the sidebar badge and the phone tab bar all read this, so the
number is the same everywhere. Four kinds, newest first within each:

- approval: a pending approval (only the owner decides them);
- ask: an open ticket an agent raised for this member (`ask_owner`, or a
  blocking question in chat: `chat_send blocking=true`, with `source_ref`, the
  task that waits, and `message_link`) or any other open task an agent
  assigned to them;
- review: a result handed in for this member's review (the Tasks view
  `to_review`, the same SQL);
- mention: an unread chat message that @mentions them. Pings that only
  announce an ask or an approval above are left out (the item is already
  in the list).
"""

import json
import re
import sqlite3

from . import actors, asks, tasks
from .core import Ctx

KINDS = ("approval", "ask", "review", "mention")
_APPROVAL_PING = re.compile(r"schválení #\d+")
MENTION_LIMIT = 30


def _approvals(conn: sqlite3.Connection, viewer: sqlite3.Row) -> list[dict]:
    from . import approval_view

    if not viewer["is_owner"]:
        return []
    rows = conn.execute(
        """SELECT a.*, r.name AS requested_by_name, r.kind AS requested_by_kind FROM approvals a
           LEFT JOIN actors r ON r.id = a.requested_by WHERE a.status = 'pending' ORDER BY a.id DESC""").fetchall()
    out = []
    for r in rows:
        details = json.loads(r["details"] or "{}")
        v = approval_view.view(conn, r["action"], details)
        why = v["why"] or v["reason"]
        out.append({
            "kind": "approval", "key": f"approval:{r['id']}", "id": r["id"],
            "title": v["title"], "action": r["action"], "detail": why[:240], "view": v,
            "from_name": r["requested_by_name"], "from_kind": r["requested_by_kind"], "at": r["created_at"],
            "ref": tasks.display_id(r["task_id"]) if r["task_id"] else None,
            "link": f"/approvals#a{r['id']}",
        })
    return out


def _asks(conn: sqlite3.Connection, viewer: sqlite3.Row) -> list[dict]:
    """Open tasks for this member that an agent put there (asks first)."""
    rows = conn.execute(
        """SELECT t.*, c.name AS from_name, c.kind AS from_kind FROM tasks t
           LEFT JOIN actors c ON c.id = t.created_by
           WHERE t.archived_at IS NULL AND t.assignee_id = ? AND t.status NOT IN ('done', 'review', 'someday')
             AND (t.source = 'ask_owner' OR (c.kind IN ('ai', 'agent') AND t.status != 'inbox'))
           ORDER BY CASE t.source WHEN 'ask_owner' THEN 0 ELSE 1 END, COALESCE(t.priority, 4), t.id DESC""",
        (viewer["id"],)).fetchall()
    asker = {}
    if _has_table(conn, "owner_asks"):
        for a in conn.execute("""SELECT o.ticket_id, o.kind, o.blocking, o.source_task_id, o.message_id, m.channel_id,
                                        x.name, x.kind AS akind FROM owner_asks o
                                 JOIN actors x ON x.id = o.asker_id LEFT JOIN chat_messages m ON m.id = o.message_id
                                 WHERE o.status = 'open'"""):
            asker[a["ticket_id"]] = a
    out = []
    for r in rows:
        a = asker.get(r["id"])
        out.append({
            "kind": "ask", "key": f"ask:{r['id']}", "id": r["id"], "ref": tasks.display_id(r["id"]),
            "title": r["title"], "detail": context(r["notes"] or ""), "links": links(r["notes"] or ""),
            "ask_kind": a["kind"] if a else None,
            "blocking": bool(a["blocking"]) if a else False,
            "from_name": a["name"] if a else r["from_name"], "from_kind": a["akind"] if a else r["from_kind"],
            "at": r["created_at"], "link": f"/tasks?task={tasks.display_id(r['id'])}",
            # The task that waits for the answer, and the chat question it came from (a blocking chat ask).
            "source_ref": tasks.display_id(a["source_task_id"]) if a and a["source_task_id"] else None,
            "message_link": (f"/chat?c={a['channel_id']}&m={a['message_id']}"
                             if a and a["message_id"] and a["channel_id"] else None),
        })
        card = asks.card(conn, r["id"]) if a else None
        if card:  # a decision card: a button per option, the recommendation, when it applies by itself
            out[-1].update(options=card["options"], recommendation=card["recommendation"],
                           default_at=card["default_at"])
    return out


_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_SKIP_LINE = re.compile(r"^\s*(#|\*\*Ptá se:\*\*|\*\*Potřebuje|- \*\*Zdrojový úkol|\|)")


def _plain(text: str) -> str:
    t = _MD_LINK.sub(lambda m: m.group(1), text)
    return " ".join(re.sub(r"[*_`>]+", "", t).split())


def context(notes: str, limit: int = 240) -> str:
    """One or two sentences of why the item waits for him: the "Proč" (or "Co udělat") section of an
    ask, else the first plain paragraph. Never the agent's headers or meta lines."""
    sections = re.split(r"(?m)^###\s+", notes or "")
    for head in ("Proč", "Co udělat", "Co potřebuju"):
        for s in sections:
            if s.startswith(head):
                body = s[len(head):].strip()
                para = body.split("\n\n")[0]
                if para.strip():
                    return _plain(para)[:limit]
    for line in (notes or "").split("\n"):
        if line.strip() and not _SKIP_LINE.match(line):
            return _plain(line)[:limit]
    return ""


def links(notes: str) -> list[dict]:
    """Where he acts on it: Gmail drafts for draft items (T-629), else the first web link."""
    text = notes or ""
    m = re.search(r"https://mail\.google\.com/mail/u/\??(?:authuser=([^#&)\s]+))?", text)
    if m and ("koncept" in text.lower() or "#drafts" in text):
        user = f"?authuser={m.group(1)}" if m.group(1) else "0/"
        return [{"label": "Otevřít koncepty v Gmailu", "href": f"https://mail.google.com/mail/u/{user}#drafts"}]
    for lm in _MD_LINK.finditer(text):
        if lm.group(2).startswith("http"):
            return [{"label": _plain(lm.group(1))[:60] or "Otevřít odkaz", "href": lm.group(2)}]
    return []


def _reviews(conn: sqlite3.Connection, ctx: Ctx) -> list[dict]:
    from . import owner_report

    out = []
    rows = tasks.list_tasks(conn, ctx, "to_review", limit=100)
    takeaways = owner_report.takeaways_for(conn, [t["id"] for t in rows], ctx.actor_id)
    for t in rows:
        out.append({
            "kind": "review", "key": f"review:{t['id']}", "id": t["id"], "ref": t["ref"], "title": t["title"],
            "detail": (takeaways.get(t["id"]) or t.get("progress_note") or "")[:240],
            "report_url": f"/report/{t['ref']}", "from_name": t.get("assignee_name"),
            "from_kind": t.get("assignee_type"), "at": t["updated_at"],
            "link": f"/tasks?view=review&task={t['ref']}",
        })
    return out


def _mentions(conn: sqlite3.Connection, viewer: sqlite3.Row, skip: set[int]) -> list[dict]:
    rows = conn.execute(
        """SELECT m.id, m.channel_id, m.body, m.created_at, m.reply_to, a.name AS from_name, a.kind AS from_kind,
                  c.kind AS channel_kind, c.name AS channel_name
           FROM chat_messages m
           JOIN channel_members cm ON cm.channel_id = m.channel_id AND cm.actor_id = ?
           JOIN channels c ON c.id = m.channel_id AND c.archived_at IS NULL
           JOIN actors a ON a.id = m.author_id
           WHERE m.id > cm.last_read_message_id AND m.archived_at IS NULL AND m.author_id != ?
             AND EXISTS (SELECT 1 FROM json_each(m.mentions) j WHERE j.value = ?)
           ORDER BY m.id DESC LIMIT ?""",
        (viewer["id"], viewer["id"], viewer["id"], MENTION_LIMIT * 2)).fetchall()
    out = []
    for r in rows:
        if r["id"] in skip or _APPROVAL_PING.search(r["body"]):
            continue
        where = f"#{r['channel_name']}" if r["channel_kind"] == "group" else "DM"
        out.append({
            "kind": "mention", "key": f"mention:{r['id']}", "id": r["id"], "channel_id": r["channel_id"],
            "thread": r["reply_to"] or r["id"], "title": f"{r['from_name']} v {where}", "detail": r["body"][:280],
            "from_name": r["from_name"], "from_kind": r["from_kind"], "at": r["created_at"], "ref": None,
            "link": f"/chat?c={r['channel_id']}",
        })
    return out[:MENTION_LIMIT]


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone() is not None


def collect(conn: sqlite3.Connection, ctx: Ctx) -> dict:
    """{count, counts: {kind: n}, items: [...]} for the signed-in member."""
    viewer = actors.get(conn, ctx.actor_id)
    asks_ = _asks(conn, viewer)
    ask_ids = {a["id"] for a in asks_}
    reviews = [r for r in _reviews(conn, ctx) if r["id"] not in ask_ids]
    # The chat pings of asks: the ticket is already in the list.
    skip: set[int] = set()
    if _has_table(conn, "owner_asks"):
        skip = {r["message_id"] for r in conn.execute(
            "SELECT message_id FROM owner_asks WHERE message_id IS NOT NULL")}
    items = [*_approvals(conn, viewer), *asks_, *reviews, *_mentions(conn, viewer, skip)]
    counts = {k: sum(1 for i in items if i["kind"] == k) for k in KINDS}
    return {"count": len(items), "counts": counts, "items": items}
