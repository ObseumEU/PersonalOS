"""E-mail out of PersonalOS (request_outbound "email.send"): a Gmail draft for the owner, or (later) a send.

The owner's decision (2026-10-04): every e-mail an agent writes becomes a **Gmail draft** in the right
mailbox and thread; the owner opens it from one "Čeká na tebe" item and sends it himself. Once he trusts the
drafts he switches `email.mode` to `auto` (Nastavení, "E-maily odesílat automaticky"); then the same
message is sent with the Gmail API. Per-domain exceptions (`outbound.email.auto_domains`) send to those
domains automatically while the mode stays `draft`.

- **Account**: the company mailbox (david.rosko@obseum.cz) for business; the personal one only for a
  reply in a thread that lives in the personal mailbox, or for a task whose topic is personal.
- **Thread**: `thread_id` → a reply in the thread (In-Reply-To/References, To the customer, Cc the rest,
  pos.support.gmail.build_reply); otherwise a new message (to, subject, cc?).
- **Signature**: David's sign-off as in his sent mail (pos.support.service.signature), "Rodinné příběhy"
  on Kniha mail (`project: "kniha"`).
- **Scopes**: drafts use POS_GMAIL_COMPOSE_TOKEN_<ADDRESS> (`gmail.compose`, through pos.support.gmail's
  drafts-only client); auto sends use POS_GMAIL_SEND_TOKEN_<ADDRESS> (`gmail.send`, the narrowest scope that
  sends; one consent: deploy/prod/gmail-send-login.sh). Threads are read with the read-only tokens.
- **The owner's send** is seen by `sync_drafts` (scheduler `outbound_drafts`): a SENT message in the
  draft's thread → sent (unchanged or edited, compared with the draft), the draft gone without one →
  discarded. The owner's item closes itself; the trust numbers (`draft_trust`) tell him when auto is safe.
"""

import base64
import difflib
import email.utils
import os
import re
import sqlite3
from datetime import datetime, timezone
from email.message import EmailMessage

from .invoices import gapi

WORK = "david.rosko@obseum.cz"
PERSONAL_TOPICS = {"osobni", "osobní", "personal", "rodina", "domacnost", "domácnost"}
MODE_KEY = "outbound.email.mode"
AUTO_DOMAINS_KEY = "outbound.email.auto_domains"
MODES = ("draft", "auto")


class Refused(Exception):
    pass


# ------------------------------------------------------------------ policy

def mode(conn: sqlite3.Connection) -> str:
    from . import settings_store

    m = settings_store.get(conn, MODE_KEY) or os.environ.get("POS_EMAIL_MODE") or "draft"
    return m if m in MODES else "draft"


def auto_domains(conn: sqlite3.Connection) -> list[str]:
    from . import settings_store

    return [d.lower().strip() for d in settings_store.get(conn, AUTO_DOMAINS_KEY) or [] if str(d).strip()]


def mode_for(conn: sqlite3.Connection, payload: dict) -> str:
    """draft or auto for this one e-mail (auto: the owner's switch, or every recipient in an auto domain)."""
    if mode(conn) == "auto":
        return "auto"
    domains = auto_domains(conn)
    rcpts = [a for _, a in email.utils.getaddresses([str(payload.get("to") or "")]) if a]
    if domains and rcpts and all(a.rpartition("@")[2].lower() in domains for a in rcpts):
        return "auto"
    return "draft"


def set_mode(conn: sqlite3.Connection, ctx, value: str, domains: list[str] | None = None) -> dict:
    from . import actors, audit, settings_store
    from .core import Forbidden

    if not actors.get(conn, ctx.actor_id)["is_owner"]:
        raise Forbidden("only the owner switches how e-mail goes out")
    if value not in MODES:
        raise ValueError(f"mode is one of {MODES}")
    settings_store.put(conn, ctx, MODE_KEY, value)
    if domains is not None:
        settings_store.put(conn, ctx, AUTO_DOMAINS_KEY,
                           sorted({d.lower().strip().lstrip("@") for d in domains if d.strip()}))
    audit.log(conn, ctx, "outbound_email_mode", None, None, mode=value, auto_domains=auto_domains(conn))
    conn.commit()
    return policy(conn)


def policy(conn: sqlite3.Connection) -> dict:
    return {"mode": mode(conn), "auto_domains": auto_domains(conn), "modes": list(MODES),
            "configured": configured()}


# ------------------------------------------------------------------ tokens

def _env(prefix: str, address: str) -> str:
    return prefix + re.sub(r"[^A-Z0-9]", "_", address.upper())


def send_env(address: str) -> str:
    return _env("POS_GMAIL_SEND_TOKEN_", address)


def configured() -> dict:
    """Per mailbox: can draft (gmail.compose), can send (gmail.send). Names only, never values."""
    from .support import gmail as gm

    return {a: {"draft": bool(os.environ.get(gm.compose_env(a))), "send": bool(os.environ.get(send_env(a)))}
            for a in gapi.mailboxes()}


def can(how: str) -> bool:
    return any(v[how] for v in configured().values())


# ------------------------------------------------------------------ the message

def _is_personal_task(conn: sqlite3.Connection | None, task_id: int | None) -> bool:
    if conn is None or not task_id:
        return False
    row = conn.execute("SELECT topic FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return bool(row and str(row["topic"] or "").lower() in PERSONAL_TOPICS)


def _thread(account: str, thread_id: str, gmail_factory) -> list[dict] | None:
    from .support import gmail as gm

    try:
        return gm.thread_headers(account, thread_id, gmail_factory)
    except gapi.GoogleError as e:
        if "404" in str(e) or "not set up" in str(e):
            return None
        raise


def choose_account(payload: dict, *, personal_task: bool, gmail_factory=gapi.Gmail) -> tuple[str, list[dict] | None]:
    """(mailbox, the thread's messages or None). Business mail from the company mailbox; the personal
    mailbox only for a personal thread or a personal task."""
    boxes = gapi.mailboxes()
    work = WORK if WORK in boxes else (boxes[0] if boxes else WORK)
    asked = str(payload.get("account") or "").strip().lower() or None
    if asked and asked not in boxes:
        raise Refused(f"unknown mailbox {asked!r}; known: {', '.join(boxes)}")
    thread_id = payload.get("thread_id")
    if thread_id:
        order = [asked] if asked else [work, *[b for b in boxes if b != work]]
        for box in order:
            msgs = _thread(box, str(thread_id), gmail_factory)
            if msgs:
                return box, msgs  # a thread that lives in the personal mailbox is personal by nature
        raise Refused(f"thread {thread_id} is not in {', '.join(order)}")
    if asked and asked != work and not personal_task:
        raise Refused(f"{asked} is the owner's personal mailbox: only for a personal task (topic osobni) or "
                      "a reply in a personal thread; business mail goes from " + work)
    return asked or work, None


def text_with_signature(account: str, payload: dict, existing: dict | None = None) -> str:
    from .support import classify as cls
    from .support import service as support

    body = str(payload.get("body") or "").strip()
    lang = payload.get("language") or (existing or {}).get("language") or cls.language(body)
    project = payload.get("project") or (existing or {}).get("project_slug")
    return support._with_signature(body, support.signature(account, lang, project))


def build(account: str, payload: dict, msgs: list[dict] | None, text: str) -> EmailMessage:
    from .support import gmail as gm

    if msgs:
        msg = gm.build_reply(msgs, account, text)
        if payload.get("to"):  # the agent named the recipient: it wins over the thread's sender
            del msg["To"]
            msg["To"] = str(payload["to"])
        if payload.get("cc") is not None:
            del msg["Cc"]
            if payload.get("cc"):
                msg["Cc"] = str(payload["cc"])
        return msg
    if not payload.get("to") or not payload.get("subject"):
        raise Refused("a new e-mail needs to and subject (or thread_id for a reply)")
    msg = EmailMessage()
    msg["From"] = email.utils.formataddr((gm.FROM_NAME, account))
    msg["To"] = str(payload["to"])
    if payload.get("cc"):
        msg["Cc"] = str(payload["cc"])
    msg["Subject"] = str(payload["subject"])
    if payload.get("in_reply_to"):
        msg["In-Reply-To"] = msg["References"] = str(payload["in_reply_to"])
    msg.set_content(text.rstrip() + "\n")
    msg.add_alternative(gm.to_html(text), subtype="html")
    return msg


def prepare(payload: dict, *, conn: sqlite3.Connection | None = None, task_id: int | None = None,
            gmail_factory=gapi.Gmail) -> dict:
    account, msgs = choose_account(payload, personal_task=_is_personal_task(conn, task_id),
                                   gmail_factory=gmail_factory)
    text = text_with_signature(account, payload)
    msg = build(account, payload, msgs, text)
    thread_id = str(payload["thread_id"]) if payload.get("thread_id") else None
    return {"account": account, "msg": msg, "text": text, "thread_id": thread_id}


def _raw(msg: EmailMessage) -> str:
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def _headers(p: dict) -> dict:
    m = p["msg"]
    return {"account": p["account"], "to": m["To"], "cc": m["Cc"], "subject": m["Subject"],
            "in_reply_to": m["In-Reply-To"]}


# ------------------------------------------------------------------ draft (default) and send (auto)

def draft(p: dict) -> dict:
    """A Gmail draft through the drafts-only compose client: nothing leaves until the owner sends it."""
    from .support import gmail as gm

    if not os.environ.get(gm.compose_env(p["account"])):
        raise Refused(f"drafts are not set up for {p['account']} ({gm.compose_env(p['account'])} missing)")
    body = {"message": {"raw": _raw(p["msg"])}}
    if p["thread_id"]:
        body["message"]["threadId"] = p["thread_id"]
    made = gm._request(p["account"], "POST", "drafts", json=body).json()
    message = made.get("message") or {}
    return {"status": "drafted", **_headers(p), "draft_id": made.get("id"), "message_id": message.get("id"),
            "thread_id": message.get("threadId") or p["thread_id"],
            "link": gm.draft_link(p["account"], message.get("id") or ""),
            "draft_sha256": _sha(p["text"]), "draft_text": p["text"][:6000]}


def send(p: dict) -> dict:
    """The auto path: messages.send with the gmail.send token (only when the owner switched it on)."""
    env = send_env(p["account"])
    if not os.environ.get(env):
        raise Refused(f"automatic sending is not set up for {p['account']} ({env} missing: "
                      "deploy/prod/gmail-send-login.sh)")
    body = {"raw": _raw(p["msg"])}
    if p["thread_id"]:
        body["threadId"] = p["thread_id"]
    import httpx

    headers = {"Authorization": f"Bearer {gapi._access_token(env)}"}
    try:
        with gapi._client() as c:
            r = c.post(f"{gapi.GMAIL}/messages/send", headers=headers, json=body)
    except httpx.HTTPError as e:
        raise gapi.GoogleError(f"Gmail is unreachable: {e}"[:300]) from e
    if r.status_code >= 400:
        raise gapi.GoogleError(f"Gmail send → {r.status_code}: {r.text[:300]}")
    data = r.json()
    return {"status": "sent", **_headers(p), "message_id": data.get("id"),
            "thread_id": data.get("threadId") or p["thread_id"], "sent_to": p["msg"]["To"]}


def _sha(text: str) -> str:
    import hashlib

    return hashlib.sha256(_norm(text).encode()).hexdigest()


def _norm(text: str) -> str:
    return " ".join((text or "").split()).lower()


# ------------------------------------------------------------------ the owner's send, seen on the next sync

def _draft_exists(account: str, draft_id: str) -> bool:
    from .support import gmail as gm

    try:
        gm._request(account, "GET", f"drafts/{draft_id}", params={"format": "minimal"})
        return True
    except gapi.GoogleError as e:
        if "404" in str(e):
            return False
        raise


def outcome(row: dict, *, gmail_factory=gapi.Gmail) -> dict | None:
    """{state: sent|discarded, edited?, sent_at} once the owner acted on the draft; None while it waits."""
    import json

    res = json.loads(row["result"] or "{}")
    account, thread_id = row["account"], row["thread_id"] or res.get("thread_id")
    created_ms = datetime.fromisoformat(row["created_at"]).timestamp() * 1000 - 60_000
    msgs = _thread(account, thread_id, gmail_factory) if thread_id else None
    for m in msgs or []:
        if "SENT" in m["labels"] and "DRAFT" not in m["labels"] and m["internal_ms"] >= created_ms:
            full = gmail_factory(account).message(m["id"])
            sent_text = full.get("body") or ""
            mine = res.get("draft_text") or ""
            ratio = difflib.SequenceMatcher(None, _norm(mine), _norm(sent_text[:len(mine) * 2 + 200])).ratio()
            at = datetime.fromtimestamp(m["internal_ms"] / 1000, timezone.utc).isoformat(timespec="seconds")
            return {"state": "sent", "edited": ratio < 0.97, "similarity": round(ratio, 3), "sent_at": at,
                    "sent_message_id": m["id"]}
    if res.get("draft_id") and not _draft_exists(account, res["draft_id"]):
        return {"state": "discarded"}
    return None
