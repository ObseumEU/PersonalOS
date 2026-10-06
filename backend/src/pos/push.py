"""Web Push to the installed app (docs/MOBILE.md): quiet and useful.

What notifies a member who subscribed a device (the owner, in practice):

- chat: a message to them from someone else: any DM, and in a group only a
  message that @mentions them or answers their thread. Never #system (the
  automated notices), never what they have read already, never a chat ping
  that only announces an ask or an approval (the "needs" item covers it);
- needs: a new "Čeká na tebe" item (pos.needs_me: an agent's browser waiting
  for him (a handoff: it opens the live browser at /m/handoff/<id>), an approval, an access
  request only the owner decides, an approved LinkedIn post to publish, a
  Gmail draft, an ask, a result to review) that was not there before;
- urgent: an agent's message with priority stop or change_plan (the CEO's
  urgent messages), and a blocking ask. Urgent ignores the quiet hours.

Per member settings (pos.settings_store `push.prefs.<actor id>`): each category
on or off, quiet hours (default 22:00-07:00 Prague time), and whether the text
shows a preview. During quiet hours chat messages are skipped; new "needs"
items wait and arrive after (one summary when there are many).

One notification per conversation: the tag is the channel (or the thread), so
a newer message replaces the older notification instead of piling up. The
payload carries a title, a short redacted preview and the /m address to open;
the app loads the content itself on click. A subscription the push service
reports as gone (404/410) is deleted.

Approval notifications carry "Schválit" / "Zamítnout" buttons. Each device's
copy carries its own action token (random, stored only as a hash): bound to
the approval, the subscription and the device (its session id), valid for
TTL_S, usable once. POST /api/push/action decides the approval only when the
token, the approval id, the subscription endpoint the service worker sends and
the signed-in session of that same device all match (`use_action`); anything
else is refused and the service worker opens the app at the approval instead.

Sending runs in a background loop in the api (`loop`), every TICK_S seconds.
Tables are created on first use (no numbered migration).
"""

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from . import settings_store
from .core import TZ, Ctx, now_iso

log = logging.getLogger(__name__)

TICK_S = 10
NOISE_CHANNELS = {"system"}  # automated notices: never a push
URGENT_PRIORITIES = {"stop", "change_plan"}
SUMMARY_OVER = 3  # more new "needs" items at once than this: one summary notification
SILENT_WITHIN_S = 45  # the same conversation again this soon: update the notification without a sound
PREVIEW_CHARS = 120
TTL_S = 12 * 3600

DEFAULT_PREFS = {"chat": True, "needs": True, "urgent": True, "quiet": True, "quiet_from": "22:00",
                 "quiet_to": "07:00", "preview": True}

# Browsers' push services only: the server never posts to any other host (no SSRF through a subscription).
PUSH_HOSTS = ("fcm.googleapis.com", "android.googleapis.com", "updates.push.services.mozilla.com",
              "push.services.mozilla.com", "web.push.apple.com", "notify.windows.com")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id INTEGER PRIMARY KEY,
    actor_id INTEGER NOT NULL,
    device_id TEXT,
    endpoint TEXT NOT NULL UNIQUE,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    user_agent TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    last_ok_at TEXT,
    last_error TEXT,
    failures INTEGER NOT NULL DEFAULT 0
);
-- One-time "Schválit"/"Zamítnout" tokens of approval notifications, per device (only the hash is kept).
CREATE TABLE IF NOT EXISTS push_actions (
    id INTEGER PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    sub_id INTEGER NOT NULL,
    actor_id INTEGER NOT NULL,
    device_id TEXT NOT NULL,
    approval_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
);
CREATE INDEX IF NOT EXISTS push_subscriptions_actor ON push_subscriptions(actor_id);
CREATE TABLE IF NOT EXISTS push_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS push_sent (
    actor_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (actor_id, key)
);
"""
_ready: set[str] = set()


class PushError(Exception):
    pass


def ensure_schema(conn: sqlite3.Connection) -> None:
    key = str(conn.execute("PRAGMA database_list").fetchone()[2])
    if key in _ready:
        return
    conn.executescript(_SCHEMA)
    _ready.add(key)


# ------------------------------------------------------------------ keys

def configured(settings) -> bool:
    return bool(settings.vapid_private_key and settings.vapid_public_key)


def generate_vapid() -> tuple[str, str]:
    """(private, public) as base64url: the raw 32-byte key and the uncompressed P-256 point."""
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()  # noqa: E731
    private = key.private_numbers().private_value.to_bytes(32, "big")
    public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return b64(private), b64(public)


# ------------------------------------------------------------------ subscriptions

def valid_endpoint(endpoint: str) -> bool:
    u = urlparse(endpoint or "")
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host == h or host.endswith("." + h) for h in PUSH_HOSTS)


def subscribe(conn: sqlite3.Connection, actor_id: int, sub: dict, *, device_id: str | None,
              user_agent: str = "") -> dict:
    """Store (or move to this member and device) a browser's PushSubscription. The caller commits."""
    ensure_schema(conn)
    endpoint = str(sub.get("endpoint") or "")
    keys = sub.get("keys") or {}
    if not valid_endpoint(endpoint):
        raise PushError("unknown push service")
    if not keys.get("p256dh") or not keys.get("auth"):
        raise PushError("the subscription has no keys")
    conn.execute("""INSERT INTO push_subscriptions (actor_id, device_id, endpoint, p256dh, auth, user_agent, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(endpoint) DO UPDATE SET actor_id = excluded.actor_id, device_id = excluded.device_id,
                      p256dh = excluded.p256dh, auth = excluded.auth, user_agent = excluded.user_agent,
                      failures = 0, last_error = NULL""",
                 (actor_id, device_id, endpoint, str(keys["p256dh"]), str(keys["auth"]), (user_agent or "")[:300],
                  now_iso()))
    _seed_needs(conn, actor_id)
    row = conn.execute("SELECT id FROM push_subscriptions WHERE endpoint = ?", (endpoint,)).fetchone()
    return {"id": row["id"], "ok": True}


def unsubscribe(conn: sqlite3.Connection, actor_id: int, endpoint: str) -> bool:
    ensure_schema(conn)
    cur = conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ? AND actor_id = ?", (endpoint, actor_id))
    return cur.rowcount > 0


def subscriptions(conn: sqlite3.Connection, actor_id: int) -> list[sqlite3.Row]:
    ensure_schema(conn)
    return conn.execute("SELECT * FROM push_subscriptions WHERE actor_id = ? ORDER BY id", (actor_id,)).fetchall()


# ------------------------------------------------------------------ settings

def prefs(conn: sqlite3.Connection, actor_id: int) -> dict:
    stored = settings_store.get(conn, f"push.prefs.{actor_id}", {}) or {}
    return {**DEFAULT_PREFS, **{k: v for k, v in stored.items() if k in DEFAULT_PREFS}}


_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def set_prefs(conn: sqlite3.Connection, ctx: Ctx, changes: dict) -> dict:
    cur = prefs(conn, ctx.actor_id)
    for k, v in changes.items():
        if k not in DEFAULT_PREFS:
            continue
        if k in ("quiet_from", "quiet_to"):
            if not isinstance(v, str) or not _HHMM.match(v):
                raise PushError(f"{k} must be HH:MM")
        elif not isinstance(v, bool):
            raise PushError(f"{k} must be true or false")
        cur[k] = v
    settings_store.put(conn, ctx, f"push.prefs.{ctx.actor_id}", cur)
    return cur


def in_quiet(p: dict, now: datetime | None = None) -> bool:
    """Is it quiet time now (Prague)? The window may cross midnight (22:00-07:00)."""
    if not p.get("quiet"):
        return False
    local = (now or datetime.now(timezone.utc)).astimezone(TZ)
    m = local.hour * 60 + local.minute
    a = int(p["quiet_from"][:2]) * 60 + int(p["quiet_from"][3:])
    b = int(p["quiet_to"][:2]) * 60 + int(p["quiet_to"][3:])
    if a == b:
        return False
    return a <= m < b if a < b else (m >= a or m < b)


def allowed(p: dict, category: str, now: datetime | None = None, base: str = "chat") -> bool:
    """May a notification of this category go out now? Urgent ignores quiet hours; with urgent
    turned off it counts as its base category (chat or needs)."""
    if category == "urgent":
        if p.get("urgent"):
            return True
        category = base
    return bool(p.get(category)) and not in_quiet(p, now)


# ------------------------------------------------------------------ sending

_SECRETISH = re.compile(r"\b(?:sk-[\w-]{8,}|gh[pousr]_\w{8,}|xox[abprs]-[\w-]{8,}|eyJ[\w-]{10,}\.[\w.-]+|"
                        r"[A-Za-z0-9+/_=-]{32,})")


def preview(body: str, limit: int = PREVIEW_CHARS) -> str:
    """A short plain line: no code blocks, no markup, anything key-like replaced."""
    text = re.sub(r"```.*?(```|$)", " [kód] ", body or "", flags=re.S)
    text = re.sub(r"[*_`>#]+", "", text)
    text = _SECRETISH.sub("•••", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _webpush(sub: sqlite3.Row, data: str, *, urgency: str, ttl: int, settings) -> int:
    """POST one encrypted message to the browser's push service; returns the HTTP status."""
    from py_vapid import Vapid
    from pywebpush import WebPushException, webpush

    try:
        resp = webpush({"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}},
                       data=data, vapid_private_key=Vapid.from_raw(settings.vapid_private_key.encode()),
                       vapid_claims={"sub": settings.vapid_subject}, ttl=ttl, headers={"Urgency": urgency},
                       timeout=10)
        return getattr(resp, "status_code", 201)
    except WebPushException as e:
        if e.response is not None:
            return e.response.status_code
        raise


TRANSPORT = _webpush  # tests replace this (no real push service)


def send(conn: sqlite3.Connection, settings, actor_id: int, payload: dict, *, urgent: bool = False,
         device_id: str | None = None, approval_id: int | None = None) -> int:
    """Send to every device of the member (or one device); returns how many accepted it.
    Gone subscriptions (404/410) are deleted. With `approval_id`, each device's copy carries
    the approve/reject buttons and that device's own action token. The caller commits."""
    ensure_schema(conn)
    if not configured(settings):
        return 0
    data = json.dumps(payload, ensure_ascii=False)
    subs = subscriptions(conn, actor_id)
    if device_id:
        subs = [s for s in subs if s["device_id"] == device_id]
    ok = 0
    for s in subs:
        body = data
        if approval_id is not None and s["device_id"]:
            body = json.dumps({**payload, "approval_id": approval_id, "action_token": action_token(conn, s, approval_id),
                               "actions": [{"action": "approve", "title": "Schválit"},
                                           {"action": "reject", "title": "Zamítnout"}]}, ensure_ascii=False)
        try:
            status = TRANSPORT(s, body, urgency="high" if urgent else "normal", ttl=TTL_S, settings=settings)
        except Exception as e:  # noqa: BLE001 - one bad device never stops the others
            conn.execute("UPDATE push_subscriptions SET failures = failures + 1, last_error = ? WHERE id = ?",
                         (str(e)[:200], s["id"]))
            continue
        if status in (404, 410):
            conn.execute("DELETE FROM push_subscriptions WHERE id = ?", (s["id"],))
        elif 200 <= status < 300:
            ok += 1
            conn.execute("UPDATE push_subscriptions SET last_ok_at = ?, failures = 0, last_error = NULL WHERE id = ?",
                         (now_iso(), s["id"]))
        else:
            conn.execute("""UPDATE push_subscriptions SET failures = failures + 1, last_error = ? WHERE id = ?""",
                         (f"HTTP {status}", s["id"]))
            # A subscription that keeps failing for days is dead too.
            conn.execute("DELETE FROM push_subscriptions WHERE id = ? AND failures >= 50", (s["id"],))
    return ok


# ------------------------------------------------------------------ actions on a notification

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def action_token(conn: sqlite3.Connection, sub: sqlite3.Row, approval_id: int, now: datetime | None = None) -> str:
    """A fresh one-time token for this device's approve/reject buttons on this approval (the caller commits)."""
    ensure_schema(conn)
    now = now or datetime.now(timezone.utc)
    token = secrets.token_urlsafe(32)
    conn.execute("""INSERT INTO push_actions (token_hash, sub_id, actor_id, device_id, approval_id, created_at, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                 (_hash(token), sub["id"], sub["actor_id"], sub["device_id"], approval_id,
                  now.isoformat(timespec="seconds"), (now + timedelta(seconds=TTL_S)).isoformat(timespec="seconds")))
    conn.execute("DELETE FROM push_actions WHERE expires_at < ?", ((now - timedelta(days=2)).isoformat(timespec="seconds"),))
    return token


def use_action(conn: sqlite3.Connection, *, token: str, approval_id: int, endpoint: str, actor_id: int,
               device_id: str | None, now: datetime | None = None) -> None:
    """Spend a notification action token, or raise PushError. It must be unused and unexpired, for this
    approval, issued to this member's subscription with this endpoint, and that subscription's device must
    be the session's device. A token shown to a wrong check is spent too (no second try with it).
    The caller commits."""
    ensure_schema(conn)
    now = now or datetime.now(timezone.utc)
    row = conn.execute("SELECT * FROM push_actions WHERE token_hash = ?", (_hash(token or ""),)).fetchone()
    if row is None:
        raise PushError("unknown action token")
    spent = conn.execute("UPDATE push_actions SET used_at = ? WHERE id = ? AND used_at IS NULL",
                         (now.isoformat(timespec="seconds"), row["id"])).rowcount
    if spent != 1:
        raise PushError("the action token was used already")
    if row["expires_at"] <= now.isoformat(timespec="seconds"):
        raise PushError("the action token expired")
    sub = conn.execute("SELECT * FROM push_subscriptions WHERE id = ?", (row["sub_id"],)).fetchone()
    ok = (row["approval_id"] == approval_id and row["actor_id"] == actor_id and sub is not None
          and sub["actor_id"] == actor_id and bool(device_id) and sub["device_id"] == row["device_id"]
          and hmac.compare_digest(str(sub["device_id"]), str(device_id))
          and hmac.compare_digest(str(sub["endpoint"]).encode(), str(endpoint or "").encode()))
    if not ok:
        raise PushError("the action token is not for this approval or device")


# ------------------------------------------------------------------ what notifies

def _state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM push_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _set_state(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute("INSERT INTO push_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, str(value)))


def _ask_pings(conn: sqlite3.Connection) -> set[int]:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'owner_asks'").fetchone():
        return set()
    return {r[0] for r in conn.execute("SELECT message_id FROM owner_asks WHERE message_id IS NOT NULL")}


_APPROVAL_PING = re.compile(r"schválení #\d+|[Žž]ádost o přístup #\d+")  # the needs item notifies instead


def classify(conn: sqlite3.Connection, m: sqlite3.Row, actor_id: int, *, skip: set[int] | None = None) -> str | None:
    """"chat", "urgent" or None: does chat message `m` notify member `actor_id` (see the module doc)?"""
    if m["author_id"] == actor_id or m["archived_at"]:
        return None
    ch = conn.execute("SELECT * FROM channels WHERE id = ?", (m["channel_id"],)).fetchone()
    if ch is None or ch["archived_at"] or (ch["kind"] == "group" and (ch["name"] or "").lower() in NOISE_CHANNELS):
        return None
    member = conn.execute("SELECT last_read_message_id FROM channel_members WHERE channel_id = ? AND actor_id = ?",
                          (ch["id"], actor_id)).fetchone()
    if member is None or (member["last_read_message_id"] or 0) >= m["id"]:
        return None
    if m["id"] in (skip if skip is not None else _ask_pings(conn)) or _APPROVAL_PING.search(m["body"] or ""):
        return None  # the ask or approval itself notifies (needs)
    author = conn.execute("SELECT kind, role FROM actors WHERE id = ?", (m["author_id"],)).fetchone()
    to_me = ch["kind"] == "dm"
    if not to_me:
        to_me = actor_id in json.loads(m["mentions"] or "[]")
    if not to_me and m["reply_to"]:
        root = conn.execute("SELECT author_id FROM chat_messages WHERE id = ?", (m["reply_to"],)).fetchone()
        to_me = root is not None and root["author_id"] == actor_id
    if not to_me:
        return None
    if m["priority"] in URGENT_PRIORITIES and author is not None and author["kind"] != "human":
        return "urgent"
    return "chat"


def _chat_payload(conn: sqlite3.Connection, m: sqlite3.Row, actor_id: int, p: dict, category: str) -> dict:
    ch = conn.execute("SELECT * FROM channels WHERE id = ?", (m["channel_id"],)).fetchone()
    author = conn.execute("SELECT name FROM actors WHERE id = ?", (m["author_id"],)).fetchone()
    name = author["name"] if author else "?"
    thread = m["reply_to"]
    unread = conn.execute("""SELECT COUNT(*) FROM chat_messages x JOIN channel_members cm
                               ON cm.channel_id = x.channel_id AND cm.actor_id = ?
                             WHERE x.channel_id = ? AND x.id > cm.last_read_message_id AND x.author_id != ?
                               AND x.archived_at IS NULL""", (actor_id, ch["id"], actor_id)).fetchone()[0]
    where = "" if ch["kind"] == "dm" else f" v #{ch['name']}"
    title = f"{name}{where}" + (" · vlákno" if thread else "") + (f" ({unread})" if unread > 1 else "")
    if category == "urgent":
        title = f"Naléhavé: {title}"
    tag = f"chat-{ch['id']}" + (f"-t{thread}" if thread else "")
    url = f"/m/chat/{ch['id']}" + (f"?thread={thread}" if thread else "")
    return {"title": title, "body": preview(m["body"]) if p.get("preview") else "Nová zpráva", "tag": tag,
            "url": url, "kind": category}


_NEEDS_TITLE = {"approval": "Ke schválení", "access": "Žádost o přístup", "publish": "K publikaci na LinkedIn",
               "draft": "Koncept e-mailu", "ask": "Otázka pro tebe", "review": "K revizi"}


def _needs_payload(item: dict, p: dict) -> dict:
    if item["kind"] == "handoff":  # the agent's own words: "Přihlas se do LinkedIn – zbytek udělám já"
        who = f"{item['from_name']}: " if item.get("from_name") else ""
        return {"title": str(item.get("title") or "Čeká na tebe v prohlížeči")[:120],
                "body": preview(who + (item.get("detail") or "Otevři prohlížeč agenta a dokonči krok."))
                if p.get("preview") else "Agent čeká v prohlížeči",
                "tag": f"needs-{item['key']}", "url": item.get("m_link") or "/m/needs", "kind": "needs"}
    head = _NEEDS_TITLE.get(item["kind"], "Čeká na tebe")
    who = f" · {item['from_name']}" if item.get("from_name") else ""
    text = str(item.get("title") or "").replace("_", " ")
    if item.get("detail"):
        text = f"{text}: {item['detail']}"
    return {"title": f"{head}{who}", "body": preview(text) if p.get("preview") else "Nová položka",
            "tag": f"needs-{item['key']}", "url": f"/m/needs?item={item['key']}",
            "kind": "urgent" if item.get("blocking") else "needs"}


def _seed_needs(conn: sqlite3.Connection, actor_id: int) -> None:
    """The first subscription: what waits already is not news (no burst of old items)."""
    if _state(conn, f"needs_seeded:{actor_id}"):
        return
    from . import needs_me

    now = now_iso()
    for it in needs_me.collect(conn, Ctx(actor_id))["items"]:
        conn.execute("INSERT OR IGNORE INTO push_sent (actor_id, key, sent_at) VALUES (?, ?, ?)",
                     (actor_id, it["key"], now))
    _set_state(conn, f"needs_seeded:{actor_id}", now)


def _recently(conn: sqlite3.Connection, actor_id: int, tag: str, now: datetime) -> bool:
    row = conn.execute("SELECT sent_at FROM push_sent WHERE actor_id = ? AND key = ?", (actor_id, f"tag:{tag}")).fetchone()
    return row is not None and datetime.fromisoformat(row["sent_at"]) > now - timedelta(seconds=SILENT_WITHIN_S)


def _mark(conn: sqlite3.Connection, actor_id: int, key: str) -> None:
    conn.execute("""INSERT INTO push_sent (actor_id, key, sent_at) VALUES (?, ?, ?)
                    ON CONFLICT(actor_id, key) DO UPDATE SET sent_at = excluded.sent_at""", (actor_id, key, now_iso()))


def _is_owner(conn: sqlite3.Connection, actor_id: int) -> bool:
    row = conn.execute("SELECT is_owner FROM actors WHERE id = ?", (actor_id,)).fetchone()
    return bool(row and row["is_owner"])


def _deliver(conn, settings, actor_id: int, payload: dict, now: datetime, approval_id: int | None = None) -> int:
    payload = {**payload, "silent": _recently(conn, actor_id, payload["tag"], now), "ts": now_iso()}
    n = send(conn, settings, actor_id, payload, urgent=payload.get("kind") == "urgent", approval_id=approval_id)
    _mark(conn, actor_id, f"tag:{payload['tag']}")
    conn.commit()  # the next delivery is a network call: never hold the write lock across it
    return n


def tick(conn: sqlite3.Connection, settings, now: datetime | None = None) -> list[dict]:
    """One pass: new chat messages since the cursor, then new "needs" items. Returns what was sent
    ({actor, payload}), for tests and the log. Commits."""
    ensure_schema(conn)
    now = now or datetime.now(timezone.utc)
    sent: list[dict] = []
    actor_ids = [r[0] for r in conn.execute("SELECT DISTINCT actor_id FROM push_subscriptions")]
    top = conn.execute("SELECT COALESCE(MAX(id), 0) FROM chat_messages").fetchone()[0]
    cursor = _state(conn, "chat_cursor")
    if cursor is None or not actor_ids:
        _set_state(conn, "chat_cursor", top)  # nobody to tell, or the first run: start from now
        conn.commit()
        return sent
    rows = conn.execute("SELECT * FROM chat_messages WHERE id > ? AND id <= ? ORDER BY id",
                        (int(cursor), top)).fetchall()
    skip = _ask_pings(conn)
    for aid in actor_ids:
        p = prefs(conn, aid)
        latest: dict[str, tuple[sqlite3.Row, str]] = {}  # one notification per conversation per pass
        for m in rows:
            cat = classify(conn, m, aid, skip=skip)
            if cat is None or not allowed(p, cat, now, "chat"):
                continue
            tag = f"chat-{m['channel_id']}" + (f"-t{m['reply_to']}" if m["reply_to"] else "")
            prev = latest.get(tag)
            latest[tag] = (m, "urgent" if cat == "urgent" or (prev and prev[1] == "urgent") else cat)
        for m, cat in latest.values():
            payload = _chat_payload(conn, m, aid, p, cat)
            _deliver(conn, settings, aid, payload, now)
            sent.append({"actor": aid, "payload": payload})
        sent += _needs_pass(conn, settings, aid, p, now)
    _set_state(conn, "chat_cursor", top)
    conn.execute("DELETE FROM push_sent WHERE sent_at < ?", ((now - timedelta(days=45)).isoformat(timespec="seconds"),))
    conn.commit()
    return sent


def _needs_pass(conn, settings, aid: int, p: dict, now: datetime) -> list[dict]:
    from . import needs_me

    if not _state(conn, f"needs_seeded:{aid}"):
        _seed_needs(conn, aid)
        return []
    items = [i for i in needs_me.collect(conn, Ctx(aid))["items"] if i["kind"] != "mention"]  # mentions: chat
    seen = {r[0] for r in conn.execute("SELECT key FROM push_sent WHERE actor_id = ?", (aid,))}
    new = [i for i in items if i["key"] not in seen]
    out = []
    due = []
    for it in new:
        payload = _needs_payload(it, p)
        if allowed(p, payload["kind"], now, "needs"):
            due.append((it, payload))
        elif not p.get("needs") and not (payload["kind"] == "urgent" and p.get("urgent")):
            _mark(conn, aid, it["key"])  # that category is off: never later either
        # else: quiet hours, it waits for the morning
    if len(due) > SUMMARY_OVER:
        payload = {"title": f"Čeká na tebe: {len(due)} nových položek", "body": "Schválení, otázky a výsledky k revizi.",
                   "tag": "needs-summary", "url": "/m/needs",
                   "kind": "urgent" if any(pl["kind"] == "urgent" for _, pl in due) else "needs"}
        _deliver(conn, settings, aid, payload, now)
        out.append({"actor": aid, "payload": payload})
    else:
        for it, payload in due:
            # An approval for the owner: "Schválit" / "Zamítnout" right on the notification.
            ap = it["id"] if it["kind"] == "approval" and _is_owner(conn, aid) else None
            _deliver(conn, settings, aid, payload, now, approval_id=ap)
            out.append({"actor": aid, "payload": payload})
    for it, _ in due:
        _mark(conn, aid, it["key"])
    return out


def run_once(db_path, settings) -> list[dict]:
    from .db import connect

    conn = connect(db_path)
    try:
        return tick(conn, settings)
    finally:
        conn.close()


async def loop(db_path, settings, interval_s: float = TICK_S) -> None:
    while True:
        await asyncio.sleep(interval_s)
        try:
            await asyncio.to_thread(run_once, db_path, settings)
        except Exception:  # noqa: BLE001 - keep ticking
            log.exception("push tick failed")
