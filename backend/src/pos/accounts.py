"""Accounts for people (REVIZE-FUNKCI 4.1).

Every person is an actor of kind human with an e-mail and a password (scrypt,
from the standard library). The owner or a lead invites someone: a one-time
link valid for 7 days; the invited person sets a password and is in. People
have permissions like agents (default: tasks, review, messages, approvals);
only the owner has the constitution's rights (is_owner).

The single POS_PASSWORD login stays as the owner's emergency account.
"""

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from . import actors, audit, versioning
from .core import Ctx, Forbidden, now_iso

INVITE_DAYS = 7
HUMAN_PERMISSIONS = ["approvals:request", "messages:send", "tasks:claim", "tasks:read", "tasks:review", "tasks:write"]
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AuthError(Exception):
    pass


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(bytes.fromhex(digest)))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got.hex(), digest)


def _check_new_password(password: str) -> None:
    if len(password or "") < 10:
        raise AuthError("a password has at least 10 characters")


def login(conn: sqlite3.Connection, email: str, password: str) -> int:
    """The actor id for this e-mail and password, or AuthError."""
    row = conn.execute(
        """SELECT h.*, a.archived_at FROM human_accounts h JOIN actors a ON a.id = h.actor_id
           WHERE h.email = ? COLLATE NOCASE""", ((email or "").strip(),)).fetchone()
    if row is None or row["disabled"] or row["archived_at"] or not check_password(password, row["password_hash"]):
        hash_password("x")  # the same work for a wrong e-mail as for a wrong password
        raise AuthError("wrong e-mail or password")
    conn.execute("UPDATE human_accounts SET last_login_at = ? WHERE actor_id = ?", (now_iso(), row["actor_id"]))
    conn.commit()
    return row["actor_id"]


def active_actor(conn: sqlite3.Connection, actor_id: int) -> bool:
    row = conn.execute(
        """SELECT a.archived_at, h.disabled FROM actors a LEFT JOIN human_accounts h ON h.actor_id = a.id
           WHERE a.id = ? AND a.kind = 'human'""", (actor_id,)).fetchone()
    return bool(row and not row["archived_at"] and not row["disabled"])


def may_invite(conn: sqlite3.Connection, ctx: Ctx) -> bool:
    from . import agents

    me = actors.get(conn, ctx.actor_id)
    return bool(me["is_owner"] or (me["kind"] == "human" and agents.has_permission(conn, ctx.actor_id, "tasks:write")))


def invite(conn: sqlite3.Connection, ctx: Ctx, *, email: str, name: str, reports_to: int | None = None,
           permissions: list[str] | None = None, role: str | None = None) -> dict:
    """A one-time invitation link token (returned once; only its hash is stored)."""
    from . import agents

    if not may_invite(conn, ctx):
        raise Forbidden("the owner and people with tasks:write invite colleagues")
    email, name = (email or "").strip().lower(), (name or "").strip()
    if not _EMAIL.match(email) or not name:
        raise AuthError("an invitation needs a name and a valid e-mail")
    if conn.execute("SELECT 1 FROM human_accounts WHERE email = ? COLLATE NOCASE", (email,)).fetchone():
        raise AuthError(f"{email} already has an account")
    if conn.execute("SELECT 1 FROM actors WHERE name = ?", (name,)).fetchone():
        raise AuthError(f"someone called {name} already exists")
    perms = sorted(set(permissions if permissions is not None else HUMAN_PERMISSIONS))
    unknown = set(perms) - set(agents.PERMISSIONS)
    if unknown:
        raise AuthError(f"unknown permissions: {sorted(unknown)}")
    me = actors.get(conn, ctx.actor_id)
    if not me["is_owner"] and not set(perms) <= agents.permissions_of(conn, ctx.actor_id):
        raise Forbidden("never more permissions than the one who invites has")  # constitution U5
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(days=INVITE_DAYS)).isoformat(timespec="seconds")
    cur = conn.execute(
        """INSERT INTO invites (token_hash, email, name, role, reports_to, permissions, invited_by, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (hashlib.sha256(token.encode()).hexdigest(), email, name, role, reports_to or ctx.actor_id,
         json.dumps(perms), ctx.actor_id, now_iso(), expires))
    audit.log(conn, ctx, "invite", "invite", cur.lastrowid, email=email)
    conn.commit()
    return {"id": cur.lastrowid, "token": token, "email": email, "name": name, "expires_at": expires}


def list_invites(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("""SELECT i.id, i.email, i.name, i.expires_at, i.used_at, i.actor_id, a.name AS invited_by_name
                           FROM invites i LEFT JOIN actors a ON a.id = i.invited_by ORDER BY i.id DESC LIMIT 50""")
    return [dict(r) for r in rows]


def _invite_row(conn: sqlite3.Connection, token: str):
    row = conn.execute("SELECT * FROM invites WHERE token_hash = ?",
                       (hashlib.sha256((token or "").encode()).hexdigest(),)).fetchone()
    if row is None or row["used_at"] or row["expires_at"] < now_iso():
        raise AuthError("this invitation is not valid (used or expired)")
    return row


def peek(conn: sqlite3.Connection, token: str) -> dict:
    row = _invite_row(conn, token)
    return {"email": row["email"], "name": row["name"], "expires_at": row["expires_at"]}


def accept(conn: sqlite3.Connection, token: str, password: str) -> int:
    """Create the person from the invitation; returns their actor id."""
    row = _invite_row(conn, token)
    _check_new_password(password)
    inviter = Ctx(row["invited_by"], via="invite")
    now = now_iso()
    actor = versioning.insert(conn, inviter, "actor", {
        "kind": "human", "name": row["name"], "is_owner": 0, "created_at": now, "updated_at": now,
        "created_by": row["invited_by"], "permissions": row["permissions"], "runtime": "web",
        "reports_to": row["reports_to"], "role": row["role"]})
    conn.execute("INSERT INTO human_accounts (actor_id, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
                 (actor["id"], row["email"], hash_password(password), now))
    conn.execute("UPDATE invites SET used_at = ?, actor_id = ? WHERE id = ?", (now, actor["id"], row["id"]))
    audit.log(conn, inviter, "invite_accepted", "actor", actor["id"], email=row["email"])
    conn.commit()
    return actor["id"]


def set_password(conn: sqlite3.Connection, actor_id: int, old: str, new: str) -> None:
    row = conn.execute("SELECT * FROM human_accounts WHERE actor_id = ?", (actor_id,)).fetchone()
    if row is None or not check_password(old, row["password_hash"]):
        raise AuthError("wrong password")
    _check_new_password(new)
    conn.execute("UPDATE human_accounts SET password_hash = ? WHERE actor_id = ?", (hash_password(new), actor_id))
    conn.commit()


def owner_account(conn: sqlite3.Connection, email: str, password: str) -> None:
    """Give the owner an e-mail login too (the POS_PASSWORD one stays)."""
    _check_new_password(password)
    owner = actors.owner_id(conn)
    conn.execute("""INSERT INTO human_accounts (actor_id, email, password_hash, created_at) VALUES (?, ?, ?, ?)
                    ON CONFLICT (actor_id) DO UPDATE SET email = excluded.email, password_hash = excluded.password_hash""",
                 (owner, email.strip().lower(), hash_password(password), now_iso()))
    conn.commit()
