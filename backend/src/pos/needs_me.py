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

For the owner also:

- access: an access request only the owner decides (pos.access: owner-only
  capabilities, or one the Access manager escalated; most are approved at
  once and never wait), with the endpoint that grants or denies it;
- publish: an approved LinkedIn post that is not on LinkedIn yet (it waited
  for the connection, or publishing failed): one click publishes it;
- draft: a Gmail draft an agent made that waits for him (pos.outbound_drafts).
  Drafts whose campaign item is already an ask are listed on that ask
  (`drafts`) instead of twice;
- handoff: an agent's live browser waits for one step only he can do (a login,
  a 2FA code, "Allow"): the page is ready, he opens it and finishes it
  (pos.handoff); an expired one stays as "Pokračovat" until he resumes or
  dismisses it.
"""

import json
import re
import sqlite3

from . import actors, asks, tasks
from .core import Ctx

KINDS = ("handoff", "approval", "access", "publish", "draft", "ask", "review", "mention")
# Chat pings that only announce an item listed here (an approval, an owner-only access request).
_APPROVAL_PING = re.compile(r"schválení #\d+|[Žž]ádost o přístup #\d+")
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


def _access(conn: sqlite3.Connection, viewer: sqlite3.Row) -> list[dict]:
    """Access requests waiting on the owner: owner-only ones, and the ones the Access manager escalated."""
    if not viewer["is_owner"] or not _has_table(conn, "access_requests"):
        return []
    from .access import service as access

    rows = conn.execute(
        """SELECT r.*, a.name AS agent_name, a.kind AS agent_kind FROM access_requests r
           JOIN actors a ON a.id = r.agent_id
           WHERE r.status IN ('pending', 'escalated') AND (r.needs_owner = 1 OR r.status = 'escalated')
           ORDER BY r.id DESC""").fetchall()
    out = []
    for r in rows:
        cred = (r["capability"] or "").startswith(access.CRED_PREFIX + ":")
        if r["capability"]:
            what = r["capability"]
        elif r["metric"]:
            what = f"{access.METRICS.get(r['metric'], r['metric'])} {access._fmt(r['metric'], r['amount'])}"
        else:
            what = "kontrola po skoku ve spotřebě"
        out.append({
            "kind": "access", "key": f"access:{r['id']}", "id": r["id"], "ref": None,
            "title": f"Žádost o přístup: {what}"[:200], "detail": (r["why"] or "")[:240],
            "capability": r["capability"], "metric": r["metric"], "amount": r["amount"], "hours": r["hours"],
            "agent_id": r["agent_id"], "from_name": r["agent_name"], "from_kind": r["agent_kind"],
            "at": r["created_at"], "blocking": bool(r["blocking"]),
            "task_ref": tasks.display_id(r["task_id"]) if r["task_id"] else None,
            # Credentials register and grant in one step (pos.credentials), the rest through pos.access.
            "decide_url": (f"/api/credentials/requests/{r['id']}/decide" if cred
                           else f"/api/access/requests/{r['id']}/decide"),
            "link": f"/credentials?request={r['id']}" if cred else f"/agents/{r['agent_id']}",
        })
    return out


def _handoffs(conn: sqlite3.Connection, viewer: sqlite3.Row) -> list[dict]:
    """The agents' browsers that wait for the owner (pos.handoff): only he opens them."""
    if not viewer["is_owner"]:
        return []
    from . import handoff

    return handoff.needs_items(conn)


def _linkedin_connected() -> bool:
    try:
        from . import outbound_linkedin

        return outbound_linkedin.connected() is not None
    except Exception:  # noqa: BLE001 - no token file, no key: not connected
        return False


def _publish(conn: sqlite3.Connection, viewer: sqlite3.Row) -> list[dict]:
    """Approved LinkedIn posts that are not published yet (ready_to_publish)."""
    if not viewer["is_owner"]:
        return []
    rows = conn.execute(
        """SELECT a.id, a.details, a.result, a.decided_at, a.created_at, r.name AS from_name, r.kind AS from_kind
           FROM approvals a LEFT JOIN actors r ON r.id = a.requested_by
           WHERE a.action = 'linkedin.post' AND a.status = 'approved' ORDER BY a.id DESC""").fetchall()
    out = []
    connected = None
    for r in rows:
        res = json.loads(r["result"] or "{}")
        if res.get("status") != "ready_to_publish":
            continue
        if connected is None:
            connected = _linkedin_connected()
        payload = json.loads(r["details"] or "{}").get("payload") or {}
        text = str(payload.get("text") or res.get("text") or "")
        first = " ".join(text.split())[:70]
        out.append({
            "kind": "publish", "key": f"publish:{r['id']}", "id": r["id"], "ref": None,
            "title": f"LinkedIn: {first}" if first else "LinkedIn: schválený příspěvek",
            "detail": " ".join(text.split())[:240], "text": text, "error": res.get("error"),
            "connected": connected, "connect_url": "/api/integrations/linkedin/start",
            "publish_url": f"/api/integrations/linkedin/publish/{r['id']}",
            "from_name": r["from_name"], "from_kind": r["from_kind"], "at": r["decided_at"] or r["created_at"],
            "link": f"/approvals#a{r['id']}",
        })
    return out


_GMAIL = "https://mail.google.com/"


def _waiting_drafts(conn: sqlite3.Connection, viewer: sqlite3.Row) -> list[dict]:
    if not viewer["is_owner"] or not _has_table(conn, "outbound_sends"):
        return []
    rows = conn.execute(
        """SELECT s.id, s.result, s.recipient, s.created_at, s.owner_task_id, a.name AS actor_name,
                  a.kind AS actor_kind FROM outbound_sends s LEFT JOIN actors a ON a.id = s.actor_id
           WHERE s.action = 'email.send' AND s.status = 'drafted' ORDER BY s.id""").fetchall()
    out = []
    for r in rows:
        res = json.loads(r["result"] or "{}")
        link = str(res.get("link") or "")
        out.append({"id": r["id"], "to": res.get("to") or r["recipient"], "subject": res.get("subject") or "",
                    "why": str(res.get("why") or "")[:240], "link": link if link.startswith(_GMAIL) else None,
                    "agent": r["actor_name"], "agent_kind": r["actor_kind"], "at": r["created_at"],
                    "owner_task_id": r["owner_task_id"], "mark_url": f"/api/outbound/drafts/{r['id']}"})
    return out


def _drafts(drafts: list[dict], asks_: list[dict]) -> list[dict]:
    """Drafts on their campaign's ask (`drafts`), the rest one item each."""
    by_task = {a["id"]: a for a in asks_}
    out = []
    for d in drafts:
        ask = by_task.get(d["owner_task_id"])
        if ask is not None:
            ask.setdefault("drafts", []).append(d)
            continue
        out.append({
            "kind": "draft", "key": f"draft:{d['id']}", "id": d["id"], "ref": None,
            "title": f"Koncept e-mailu pro {d['to'] or '?'}: {d['subject']}"[:200], "detail": d["why"],
            "draft": d, "links": [{"label": "Otevřít v Gmailu", "href": d["link"]}] if d["link"] else [],
            "from_name": d["agent"], "from_kind": d["agent_kind"], "at": d["at"],
            "link": "/company",  # the draft trust panel; Gmail is in `links`
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
    drafts = _drafts(_waiting_drafts(conn, viewer), asks_)
    items = [*_handoffs(conn, viewer), *_approvals(conn, viewer), *_access(conn, viewer), *_publish(conn, viewer), *drafts, *asks_, *reviews,
             *_mentions(conn, viewer, skip)]
    counts = {k: sum(1 for i in items if i["kind"] == k) for k in KINDS}
    return {"count": len(items), "counts": counts, "items": items}
