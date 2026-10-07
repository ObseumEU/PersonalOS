"""When a push cannot reach the owner: the fallback channel that works today, an e-mail to his work mailbox.

prod 2026-10-07: the owner handoffs of T-957 and T-958 expired after 25-30 minutes without the owner ever seeing
them. He had no push subscription at all, so nothing reached his phone. Of the channels PersonalOS has, the one
that works without him doing anything is Gmail: the company mailbox's `gmail.send` token
(POS_GMAIL_SEND_TOKEN_DAVID_ROSKO_OBSEUM_CZ, the narrowest scope that sends) is set up on the server, and the
Gmail app on his phone notifies him. The owner chat in PersonalOS would not help (he only sees it when he opens
PersonalOS), and there is no Discord webhook or SMS gateway.

Callers decide when (at most once per item: pos.handoff keeps `notified_at`); this module only answers whether
push reaches him and sends the e-mail. Tests replace `SENDER`.
"""

import logging
import os
import sqlite3
from email.message import EmailMessage

log = logging.getLogger(__name__)

OWNER_EMAIL = "david.rosko@obseum.cz"
FAILURES_DEAD = 3  # a subscription whose last sends failed this many times in a row does not reach him


def public_url(path: str = "") -> str:
    base = (os.environ.get("POS_PUBLIC_URL") or "https://personalos.obseum.cz").rstrip("/")
    return base + path


def push_reaches_owner(conn: sqlite3.Connection) -> bool:
    """Does the owner have a working push subscription (one device whose sends are not failing)?"""
    from . import push

    push.ensure_schema(conn)
    owner = conn.execute("SELECT id FROM actors WHERE is_owner = 1 ORDER BY id LIMIT 1").fetchone()
    if owner is None:
        return False
    return conn.execute("SELECT 1 FROM push_subscriptions WHERE actor_id = ? AND COALESCE(failures, 0) < ? LIMIT 1",
                        (owner["id"], FAILURES_DEAD)).fetchone() is not None


def email_configured() -> bool:
    from .outbound_gmail import send_env

    return bool(os.environ.get(send_env(OWNER_EMAIL)))


def _gmail_send(subject: str, text: str) -> str:
    """messages.send from the owner's work mailbox to itself. Returns the Gmail message id."""
    import base64

    import httpx

    from .invoices import gapi
    from .outbound_gmail import send_env

    env = send_env(OWNER_EMAIL)
    if not os.environ.get(env):
        raise RuntimeError(f"{env} is not set on the server (deploy/prod/gmail-send-login.sh)")
    msg = EmailMessage()
    msg["From"] = f"PersonalOS <{OWNER_EMAIL}>"
    msg["To"] = OWNER_EMAIL
    msg["Subject"] = subject
    msg.set_content(text)
    headers = {"Authorization": f"Bearer {gapi._access_token(env)}"}
    try:
        with gapi._client() as c:
            r = c.post(f"{gapi.GMAIL}/messages/send", headers=headers,
                       json={"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()})
    except httpx.HTTPError as e:
        raise RuntimeError(f"Gmail is unreachable: {e}"[:300]) from e
    if r.status_code >= 400:
        raise RuntimeError(f"Gmail send -> {r.status_code}: {r.text[:200]}")
    return str(r.json().get("id") or "")


SENDER = _gmail_send  # tests: a function (subject, text) -> id


def email_owner(subject: str, text: str) -> tuple[bool, str]:
    """(sent, detail). Never raises; a network call, so the caller must not hold the DB write lock."""
    try:
        mid = SENDER(subject[:200], text)
        return True, f"email {mid}".strip()
    except Exception as e:  # noqa: BLE001 - a notice that cannot go out is reported, never fatal
        log.warning("owner fallback e-mail failed: %s", str(e)[:200])
        return False, str(e)[:200]
