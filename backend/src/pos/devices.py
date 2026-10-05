"""Signed-in devices: every login is a row, so a session can be listed and revoked.

The session cookie (Starlette's signed cookie) carries `sid`, the id of this
row. A request whose `sid` is revoked, expired or unknown loses its session
(401 on the next protected call). Sessions from before this table get a row
on their next request (they carry no `sid` yet).

Lifetime slides: a device stays signed in while it is used at least once per
TTL (30 days in a browser, a year for the installed app, which logs in with
`app: true`). The cookie itself may live longer (SessionMiddleware max_age);
this table decides. Revoking a device also drops its push subscriptions.

The table is created on first use (no numbered migration).
"""

import hashlib
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from .core import now_iso

WEB_TTL_DAYS = 30
APP_TTL_DAYS = 365
TOUCH_EVERY_S = 300  # last_seen_at is written at most this often per device
_CACHE_S = 30  # a revoke reaches every request within this many seconds

_SCHEMA = """
CREATE TABLE IF NOT EXISTS auth_devices (
    id TEXT PRIMARY KEY,
    actor_id INTEGER,
    owner_login INTEGER NOT NULL DEFAULT 0,
    app INTEGER NOT NULL DEFAULT 0,
    label TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS auth_devices_actor ON auth_devices(actor_id);
"""
_ready: set[str] = set()


def ensure_schema(conn: sqlite3.Connection) -> None:
    key = str(conn.execute("PRAGMA database_list").fetchone()[2])
    if key in _ready:
        return
    conn.executescript(_SCHEMA)
    _ready.add(key)


def label_for(user_agent: str) -> str:
    """"Android · Chrome", "Windows · Edge": enough to tell devices apart."""
    ua = user_agent or ""
    os_ = next((name for token, name in (("Android", "Android"), ("iPhone", "iPhone"), ("iPad", "iPad"),
                                         ("Windows", "Windows"), ("Mac OS", "Mac"), ("Linux", "Linux"))
                if token in ua), "")
    browser = next((name for token, name in (("Edg/", "Edge"), ("OPR/", "Opera"), ("SamsungBrowser", "Samsung"),
                                             ("Firefox/", "Firefox"), ("Chrome/", "Chrome"), ("Safari/", "Safari"))
                    if token in ua), "")
    return " · ".join(x for x in (os_, browser) if x) or "Neznámé zařízení"


def create(conn: sqlite3.Connection, actor_id: int | None, *, owner_login: bool, user_agent: str,
           app: bool = False, sid: str | None = None) -> str:
    """A new device row for a login; returns its id (the caller commits)."""
    ensure_schema(conn)
    sid = sid or secrets.token_urlsafe(24)
    now = now_iso()
    conn.execute("""INSERT INTO auth_devices (id, actor_id, owner_login, app, label, user_agent, created_at,
                                              last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                 (sid, actor_id, int(owner_login), int(app), label_for(user_agent), (user_agent or "")[:300],
                  now, now))
    return sid


def _expired(row: sqlite3.Row, now: datetime) -> bool:
    ttl = APP_TTL_DAYS if row["app"] else WEB_TTL_DAYS
    return datetime.fromisoformat(row["last_seen_at"]) + timedelta(days=ttl) < now


def valid(conn: sqlite3.Connection, sid: str) -> bool:
    ensure_schema(conn)
    row = conn.execute("SELECT * FROM auth_devices WHERE id = ?", (sid,)).fetchone()
    return row is not None and not row["revoked_at"] and not _expired(row, datetime.now(timezone.utc))


def touch(conn: sqlite3.Connection, sid: str) -> None:
    conn.execute("UPDATE auth_devices SET last_seen_at = ? WHERE id = ?", (now_iso(), sid))


def set_app(conn: sqlite3.Connection, sid: str) -> None:
    """The installed app signed in on this device: it keeps the long lifetime."""
    conn.execute("UPDATE auth_devices SET app = 1 WHERE id = ?", (sid,))


def list_for(conn: sqlite3.Connection, actor_id: int, current: str | None) -> list[dict]:
    ensure_schema(conn)
    from . import push

    push.ensure_schema(conn)
    now = datetime.now(timezone.utc)
    rows = conn.execute("""SELECT d.*, (SELECT COUNT(*) FROM push_subscriptions s WHERE s.device_id = d.id) AS push
                           FROM auth_devices d WHERE d.actor_id = ? AND d.revoked_at IS NULL
                           ORDER BY d.last_seen_at DESC""", (actor_id,)).fetchall()
    return [{"id": r["id"], "label": r["label"], "app": bool(r["app"]), "created_at": r["created_at"],
             "last_seen_at": r["last_seen_at"], "current": r["id"] == current, "push": bool(r["push"])}
            for r in rows if not _expired(r, now)]


def legacy_sid(cookie: str) -> str:
    """The device id of a session cookie that carries no `sid`: the same cookie is the same device, so
    a client that keeps sending it (a script, parallel requests before the new cookie lands) does not
    register a new device on every request (5. 10.: ~245 "Neznámé zařízení" in six minutes)."""
    return "c-" + hashlib.sha256(cookie.encode()).hexdigest()[:32]


def revoke_others(conn: sqlite3.Connection, actor_id: int, current: str | None, *, label: str | None = None) -> int:
    """Sign out every other device of this member (only those with `label` when given); returns how
    many. The caller commits."""
    ensure_schema(conn)
    rows = conn.execute("""SELECT id FROM auth_devices WHERE actor_id = ? AND revoked_at IS NULL AND id != ?
                           AND (? IS NULL OR label = ?)""", (actor_id, current or "", label, label)).fetchall()
    for r in rows:
        revoke(conn, actor_id, r["id"])
    return len(rows)


def revoke(conn: sqlite3.Connection, actor_id: int, sid: str) -> bool:
    """Sign a device out (its own actor only). Its push subscriptions go too. The caller commits."""
    ensure_schema(conn)
    row = conn.execute("SELECT id FROM auth_devices WHERE id = ? AND actor_id = ? AND revoked_at IS NULL",
                       (sid, actor_id)).fetchone()
    if row is None:
        return False
    conn.execute("UPDATE auth_devices SET revoked_at = ? WHERE id = ?", (now_iso(), sid))
    from . import push

    push.ensure_schema(conn)
    conn.execute("DELETE FROM push_subscriptions WHERE device_id = ?", (sid,))
    _cache.pop(sid, None)
    return True


# ------------------------------------------------------------------ middleware

_cache: dict[str, tuple[float, bool]] = {}  # sid -> (checked at, valid)
_touched: dict[str, float] = {}


class DeviceSessions:
    """Pure ASGI middleware inside SessionMiddleware: drops sessions of revoked or expired devices
    and registers devices for sessions that have none yet. Only when a login is required."""

    def __init__(self, app, settings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send):
        if (scope["type"] == "http" and self.settings.password and scope["path"].startswith("/api/")
                and "session" in scope):
            session = scope["session"]
            if session.get("user") or session.get("actor_id"):
                self._check(scope, session)
        await self.app(scope, receive, send)

    def _check(self, scope, session: dict) -> None:
        from .db import connect

        sid = session.get("sid")
        now = time.monotonic()
        hit = _cache.get(sid) if sid else None
        if hit and now - hit[0] < _CACHE_S and (not hit[1] or now - _touched.get(sid, 0) < TOUCH_EVERY_S):
            if not hit[1]:
                session.clear()
            return
        conn = connect(self.settings.db_path)
        try:
            if not sid:  # a session from before devices existed: it becomes one, once per cookie
                ua = next((v.decode(errors="replace") for k, v in scope["headers"] if k == b"user-agent"), "")
                from . import actors

                cookie = _session_cookie(scope)
                sid = legacy_sid(cookie) if cookie else None
                row = conn.execute("SELECT * FROM auth_devices WHERE id = ?", (sid,)).fetchone() if sid else None
                if row is not None:
                    ok = not row["revoked_at"] and not _expired(row, datetime.now(timezone.utc))
                    _cache[sid] = (now, ok)
                    if ok:
                        session["sid"] = sid
                    else:
                        session.clear()
                    return
                aid = session.get("actor_id")
                sid = create(conn, int(aid) if aid else actors.owner_id(conn), owner_login=not aid, user_agent=ua,
                             sid=sid)
                conn.commit()
                session["sid"] = sid
                _cache[sid] = (now, True)
                _touched[sid] = now
                return
            ok = valid(conn, sid)
            _cache[sid] = (now, ok)
            if not ok:
                session.clear()
                return
            if now - _touched.get(sid, 0) >= TOUCH_EVERY_S:
                touch(conn, sid)
                conn.commit()
                _touched[sid] = now
        finally:
            conn.close()


def _session_cookie(scope) -> str:
    """The raw session cookie of the request ("" without one)."""
    for k, v in scope.get("headers") or []:
        if k == b"cookie":
            for part in v.decode(errors="replace").split(";"):
                name, _, value = part.strip().partition("=")
                if name in ("pos_session", "session") and value:
                    return value
    return ""
