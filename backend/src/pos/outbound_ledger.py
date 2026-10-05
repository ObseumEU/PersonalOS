"""The outbound ledger: every send attempt, its idempotency key, the rate limits and the replies.

- **Idempotent** per (action, thread or target, content hash): the same reply to the same thread, the same
  comment on the same issue, goes out once. A second call returns status `duplicate` with the first send's
  result; a send that failed may be tried again.
- **Rate-limited** per agent and action over a rolling 24 h (LIMITS), per recipient (3 e-mails a day to one
  address) and for the whole company (COMPANY_LIMITS). Over a limit: status `rate_limited`, nothing sent.
- **Replies**: an e-mail sent in a Gmail thread is checked later (`check_replies`, the scheduler's
  `outbound_replies`) for a message from someone else after ours: `replied_at`.
- **Stats**: `outbound_stats(days)` for the CEO's digest and the company scorecard.
"""

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from .core import now_iso

# Per agent, per action, rolling 24 h. POS_OUTBOUND_LIMITS='{"email.send": 40}' overrides.
LIMITS = {"email.send": 25, "github.comment": 30, "github.issue": 15, "github.review": 20, "github.pr": 10,
          "discord.post": 20, "linkedin.post": 3}
COMPANY_LIMITS = {"email.send": 120, "discord.post": 60, "linkedin.post": 5}
PER_RECIPIENT_DAY = 3
SENT = ("sent", "drafted", "dry_run", "ready_to_publish")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS outbound_sends (
        id INTEGER PRIMARY KEY, key TEXT NOT NULL, actor_id INTEGER, action TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'ordinary', target TEXT NOT NULL DEFAULT '', recipient TEXT,
        account TEXT, thread_id TEXT, task_id INTEGER, approval_id INTEGER, body_sha256 TEXT,
        status TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
        sent_at TEXT, replied_at TEXT, reply_checked_at TEXT, campaign TEXT, owner_task_id INTEGER)""")
    conn.execute("CREATE INDEX IF NOT EXISTS outbound_sends_key ON outbound_sends (key, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS outbound_sends_actor ON outbound_sends (actor_id, action, created_at)")


def _limits() -> dict:
    out = dict(LIMITS)
    try:
        out.update({k: int(v) for k, v in json.loads(os.environ.get("POS_OUTBOUND_LIMITS") or "{}").items()})
    except (ValueError, AttributeError):
        pass
    return out


def body_of(payload: dict) -> str:
    return str(payload.get("body") or payload.get("content") or payload.get("text") or "").strip()


def target_of(action: str, p: dict) -> str:
    """What one send is addressed to: the thread, the issue, the channel."""
    if action == "email.send":
        if p.get("thread_id"):
            return f"thread:{p['thread_id']}"
        return f"to:{str(p.get('to') or '').strip().lower()}|{str(p.get('subject') or '').strip().lower()}"
    if action in ("github.comment", "github.review"):
        return f"{str(p.get('repo') or '').lower()}#{p.get('number')}"
    if action == "github.issue":
        return f"{str(p.get('repo') or '').lower()}:{str(p.get('title') or '').strip().lower()}"
    if action == "github.pr":
        return f"{str(p.get('repo') or '').lower()}:{p.get('head')}->{p.get('base') or 'main'}"
    if action == "discord.post":
        return f"discord:{p.get('channel') or 'webhook'}"
    if action == "linkedin.post":
        return "linkedin:owner"
    return action


def recipient_of(action: str, p: dict) -> str | None:
    return str(p.get("to") or "").strip().lower() or None if action == "email.send" else None


def key_of(action: str, p: dict) -> str:
    content = " ".join(body_of(p).split()).lower()
    extra = str(p.get("image_file_id") or "")
    return hashlib.sha256(f"{action}|{target_of(action, p)}|{content}|{extra}".encode()).hexdigest()


def body_hash(p: dict) -> str:
    return hashlib.sha256(body_of(p).encode()).hexdigest()


def _ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


def previous(conn: sqlite3.Connection, key: str) -> dict | None:
    """The earlier send with this key that went out (or is going out right now)."""
    ensure_schema(conn)
    row = conn.execute(f"""SELECT * FROM outbound_sends WHERE key = ? AND (status IN ({",".join("?" * len(SENT))})
                           OR (status = 'sending' AND created_at >= ?)) ORDER BY id LIMIT 1""",
                       (key, *SENT, _ago(0.25))).fetchone()
    return dict(row) if row else None


def over_limit(conn: sqlite3.Connection, actor_id: int | None, action: str, payload: dict) -> str | None:
    """Why this send is over a limit, else None."""
    ensure_schema(conn)
    since = _ago(24)
    counted = ("sent", "sending", "drafted", "discarded", "dry_run")
    q = ",".join("?" * len(counted))
    limit = _limits().get(action)
    if limit and actor_id is not None:
        n = conn.execute(f"""SELECT COUNT(*) FROM outbound_sends WHERE actor_id = ? AND action = ?
                             AND status IN ({q}) AND created_at >= ?""", (actor_id, action, *counted, since)).fetchone()[0]
        if n >= limit:
            return f"{action}: {n} of {limit} a day for this agent already"
    total = COMPANY_LIMITS.get(action)
    if total:
        n = conn.execute(f"SELECT COUNT(*) FROM outbound_sends WHERE action = ? AND status IN ({q}) AND created_at >= ?",
                         (action, *counted, since)).fetchone()[0]
        if n >= total:
            return f"{action}: the company's {total} a day are used up"
    rcpt = recipient_of(action, payload)
    if rcpt:
        n = conn.execute(f"""SELECT COUNT(*) FROM outbound_sends WHERE action = ? AND recipient = ?
                             AND status IN ({q}) AND created_at >= ?""", (action, rcpt, *counted, since)).fetchone()[0]
        if n >= PER_RECIPIENT_DAY:
            return f"{rcpt} got {n} e-mails from us in 24 h already"
    return None


def claim(conn: sqlite3.Connection, *, actor_id: int | None, action: str, kind: str, payload: dict,
          task_id: int | None = None, approval_id: int | None = None) -> int:
    """A row in state 'sending' (committed): a parallel identical call sees it and stops."""
    ensure_schema(conn)
    cur = conn.execute(
        """INSERT INTO outbound_sends (key, actor_id, action, kind, target, recipient, thread_id, task_id, approval_id,
           body_sha256, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'sending', ?)""",
        (key_of(action, payload), actor_id, action, kind, target_of(action, payload)[:300],
         recipient_of(action, payload), payload.get("thread_id"), task_id, approval_id, body_hash(payload), now_iso()))
    return cur.lastrowid


def record(conn: sqlite3.Connection, row_id: int, result: dict) -> None:
    status = result.get("status") or "failed"
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) > 4000 and result.get("draft_text"):  # cut the draft's copy, never the JSON (it must parse)
        keep = max(0, len(result["draft_text"]) - (len(text) - 4000) - 50)
        result = {**result, "draft_text": result["draft_text"][:keep]}
    conn.execute("""UPDATE outbound_sends SET status = ?, result = ?, sent_at = ?, account = COALESCE(?, account),
                    thread_id = COALESCE(?, thread_id) WHERE id = ?""",
                 (status, json.dumps(result, ensure_ascii=False, default=str)[:4000],
                  now_iso() if status in SENT else None, result.get("account"), result.get("thread_id"), row_id))


def log_attempt(conn: sqlite3.Connection, *, actor_id: int | None, action: str, kind: str, payload: dict,
                result: dict, task_id: int | None = None, approval_id: int | None = None) -> int:
    """A send that never reached a provider (rate_limited, duplicate, not_configured): counted, not claimed."""
    rid = claim(conn, actor_id=actor_id, action=action, kind=kind, payload=payload, task_id=task_id,
                approval_id=approval_id)
    record(conn, rid, result)
    return rid


# ------------------------------------------------------------------ replies

def check_replies(conn: sqlite3.Connection, days: int = 14, gmail_factory=None) -> dict:
    """E-mails we sent in a Gmail thread: did someone else write after us? (read-only token)."""
    from .invoices import gapi
    from .support import gmail as gm

    ensure_schema(conn)
    factory = gmail_factory or gapi.Gmail
    rows = conn.execute("""SELECT * FROM outbound_sends WHERE action = 'email.send' AND status = 'sent'
                           AND replied_at IS NULL AND thread_id IS NOT NULL AND account IS NOT NULL AND sent_at >= ?
                           AND (reply_checked_at IS NULL OR reply_checked_at < ?) ORDER BY id LIMIT 60""",
                        (_ago(24 * days), _ago(1))).fetchall()
    out = {"checked": 0, "replied": 0, "errors": 0}
    for r in rows:
        conn.commit()  # no write lock while Gmail answers
        try:
            msgs = gm.thread_headers(r["account"], r["thread_id"], factory)
        except Exception:  # noqa: BLE001 - a missing token or a deleted thread: try again later
            out["errors"] += 1
            conn.execute("UPDATE outbound_sends SET reply_checked_at = ? WHERE id = ?", (now_iso(), r["id"]))
            continue
        sent_ms = datetime.fromisoformat(r["sent_at"]).timestamp() * 1000
        replied = None
        for m in msgs:
            sender = gm.email.utils.parseaddr(m["headers"].get("from", ""))[1]
            if m["internal_ms"] > sent_ms and "SENT" not in m["labels"] and not gm._own(sender, r["account"]):
                replied = datetime.fromtimestamp(m["internal_ms"] / 1000, timezone.utc).isoformat(timespec="seconds")
                break
        conn.execute("UPDATE outbound_sends SET reply_checked_at = ?, replied_at = ? WHERE id = ?",
                     (now_iso(), replied, r["id"]))
        out["checked"] += 1
        out["replied"] += bool(replied)
    conn.commit()
    return out


# ------------------------------------------------------------------ stats

def outbound_stats(conn: sqlite3.Connection, days: int = 7) -> dict:
    """Outbound over the last `days`: sent per day, per agent, per kind and action; the replies received;
    what did not go out (failed, not configured, rate limited, duplicates) and what waits for the owner.
    For the CEO's daily digest and the company scorecard."""
    ensure_schema(conn)
    days = max(1, int(days))
    since = _ago(24 * days)
    rows = conn.execute("""SELECT s.*, a.name AS actor_name FROM outbound_sends s LEFT JOIN actors a ON a.id = s.actor_id
                           WHERE s.created_at >= ? ORDER BY s.id""", (since,)).fetchall()
    sent = [r for r in rows if r["status"] == "sent"]
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1

    def count(key) -> dict:
        out: dict[str, int] = {}
        for r in sent:
            k = key(r) or "?"
            out[k] = out.get(k, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    by_day = count(lambda r: (r["sent_at"] or r["created_at"])[:10])
    threads = [r for r in sent if r["action"] == "email.send" and r["thread_id"]]
    replied = [r for r in threads if r["replied_at"]]
    drafts = sum(1 for r in rows if r["status"] == "drafted")
    pending = conn.execute("""SELECT COUNT(*) FROM approvals WHERE status = 'pending' AND action IN
                              ('email.send', 'github.comment', 'github.issue', 'github.review', 'github.pr',
                               'discord.post', 'payment', 'web.post', 'linkedin.post')""").fetchone()[0]
    return {
        "days": days, "since": since, "sent": len(sent),
        "by_day": dict(sorted(by_day.items())),
        "by_agent": count(lambda r: r["actor_name"]),
        "by_kind": count(lambda r: r["kind"]),
        "by_action": count(lambda r: r["action"]),
        "by_agent_kind": count(lambda r: f"{r['actor_name'] or '?'} · {r['kind']}"),
        "replies": len(replied), "threads_sent": len(threads),
        "reply_rate": round(len(replied) / len(threads), 2) if threads else None,
        "failed": by_status.get("failed", 0), "not_configured": by_status.get("not_configured", 0),
        "not_sent": {k: v for k, v in by_status.items() if k not in ("sent", "sending", "drafted")},
        "waiting_for_owner": pending, "drafts_waiting": drafts, "draft_trust": _trust(conn, days),
    }


def _trust(conn: sqlite3.Connection, days: int) -> dict:
    from .outbound_drafts import draft_trust

    return draft_trust(conn, max(days, 30))
