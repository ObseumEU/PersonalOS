"""Reply drafts in Gmail: the narrowest write the customer-issue pipeline has. It never sends.

- Reading the thread (headers for the reply) uses the read-only tokens of pos.invoices.gapi
  (POS_GMAIL_TOKEN_<ADDRESS>, `gmail.readonly`).
- Writing uses POS_GMAIL_COMPOSE_TOKEN_<ADDRESS> (`gmail.compose`, the narrowest scope that can create a
  draft), stored in the api's environment and never shown to an agent.

`gmail.compose` could also send; this client cannot: every request goes through `_request`, which allows
only POST .../drafts (create), GET .../drafts/<id> (read back) and, for a draft whose subject starts with
"[TEST]", DELETE .../drafts/<id> (the operator's clean-up after a dry run). Anything with "send" in the path
is refused before a token is even fetched.
"""

import base64
import email.utils
import html
import re
from email.message import EmailMessage

from ..invoices import gapi

GMAIL = gapi.GMAIL
TEST_PREFIX = "[TEST] "
FROM_NAME = "David Roško"
REPLY_HEADERS = ["From", "To", "Cc", "Reply-To", "Subject", "Message-ID", "References", "In-Reply-To", "Date",
                 "Auto-Submitted"]


class Refused(Exception):
    pass


def compose_env(address: str) -> str:
    return "POS_GMAIL_COMPOSE_TOKEN_" + re.sub(r"[^A-Z0-9]", "_", address.upper())


def compose_configured() -> dict:
    import os

    return {a: bool(os.environ.get(compose_env(a))) for a in gapi.mailboxes()}


_DRAFT_ID = re.compile(r"^drafts/[A-Za-z0-9_-]+$")


def _request(address: str, method: str, path: str, **kw):
    """The only way to Gmail with the compose token: drafts only, never a send."""
    if "send" in path.lower():
        raise Refused("sending is never done by PersonalOS (drafts only)")
    allowed = (method == "POST" and path == "drafts") or (method == "GET" and _DRAFT_ID.match(path)) or \
              (method == "DELETE" and _DRAFT_ID.match(path) and kw.pop("_test_only", False))
    if not allowed:
        raise Refused(f"{method} {path} is not allowed (drafts only)")
    env = compose_env(address)
    headers = {"Authorization": f"Bearer {gapi._access_token(env)}", **kw.pop("headers", {})}
    import httpx

    try:
        with gapi._client() as c:
            r = c.request(method, f"{GMAIL}/{path}", headers=headers, **kw)
    except httpx.HTTPError as e:
        raise gapi.GoogleError(f"Gmail is unreachable: {e}"[:300]) from e
    if r.status_code >= 400:
        raise gapi.GoogleError(f"Gmail {method} {path} → {r.status_code}: {r.text[:300]}")
    return r


# ------------------------------------------------------------------ the thread (read-only token)

def thread_headers(address: str, thread_id: str, gmail_factory=gapi.Gmail) -> list[dict]:
    """The thread's messages, oldest first: {id, labels, internal_ms, headers: {lower name: value}}."""
    gm = gmail_factory(address)
    data = gm.get(f"threads/{thread_id}", format="metadata", metadataHeaders=REPLY_HEADERS)
    out = []
    for m in data.get("messages") or []:
        if "DRAFT" in (m.get("labelIds") or []):  # our own drafts are not the conversation
            continue
        heads = {h["name"].lower(): h["value"] for h in (m.get("payload") or {}).get("headers") or []}
        out.append({"id": m["id"], "labels": m.get("labelIds") or [], "internal_ms": int(m.get("internalDate") or 0),
                    "headers": heads})
    return sorted(out, key=lambda m: m["internal_ms"])


def _own(addr: str, account: str, own_domains=("obseum.cz",)) -> bool:
    a = addr.lower()
    return a == account.lower() or a.rpartition("@")[2] in own_domains


def reply_target(messages: list[dict], account: str) -> dict:
    """The message the draft answers: the last one not sent by us (else the last one)."""
    if not messages:
        raise Refused("the thread has no messages")
    for m in reversed(messages):
        sender = email.utils.parseaddr(m["headers"].get("from", ""))[1]
        if "SENT" not in m["labels"] and not _own(sender, account):
            return m
    return messages[-1]


def _subject(original: str, test: bool) -> str:
    s = (original or "").strip()
    if not re.match(r"^(re|odp|aw|sv)\s*:", s, re.I):
        s = f"Re: {s}" if s else "Re:"
    return (TEST_PREFIX + s) if test else s


def to_html(text: str) -> str:
    paras = [p for p in re.split(r"\n\s*\n", (text or "").strip()) if p.strip()]
    return "".join(f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in paras)


def build_reply(messages: list[dict], account: str, body: str, body_html: str | None = None, *,
                test: bool = False) -> EmailMessage:
    """A reply in the thread: To the customer (Reply-To or From), Cc the rest of them (never ourselves),
    In-Reply-To and References set, plain text plus simple HTML."""
    target = reply_target(messages, account)
    h = target["headers"]
    msg_id = (h.get("message-id") or "").strip()
    if not msg_id:
        raise Refused("the message has no Message-ID; the draft could not be threaded")
    to_value = h.get("reply-to") or h.get("from") or ""
    to = [(n, a) for n, a in email.utils.getaddresses([to_value]) if a and not _own(a, account)]
    if not to:  # our own last message: answer whoever we wrote to
        to = [(n, a) for n, a in email.utils.getaddresses([h.get("to", "")]) if a and not _own(a, account)]
    if not to:
        raise Refused("no customer address to reply to in the thread")
    taken = {a.lower() for _, a in to}
    cc = [(n, a) for n, a in email.utils.getaddresses([h.get("to", ""), h.get("cc", "")])
          if a and not _own(a, account) and a.lower() not in taken]
    refs = " ".join(x for x in [(h.get("references") or "").strip(), msg_id] if x)
    msg = EmailMessage()
    msg["From"] = email.utils.formataddr((FROM_NAME, account))
    msg["To"] = ", ".join(email.utils.formataddr(x) for x in to)
    if cc:
        msg["Cc"] = ", ".join(email.utils.formataddr(x) for x in cc)
    msg["Subject"] = _subject(h.get("subject", ""), test)
    msg["In-Reply-To"] = msg_id
    msg["References"] = refs
    text = (TEST_PREFIX + "Testovací koncept (PersonalOS), neodesílat.\n\n" if test else "") + body.strip() + "\n"
    msg.set_content(text)
    msg.add_alternative(body_html or to_html(text), subtype="html")
    return msg


# ------------------------------------------------------------------ drafts (compose token)

def create_draft(address: str, thread_id: str, msg: EmailMessage) -> dict:
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return _request(address, "POST", "drafts", json={"message": {"raw": raw, "threadId": thread_id}}).json()


def get_draft(address: str, draft_id: str) -> dict:
    return _request(address, "GET", f"drafts/{draft_id}", params={"format": "metadata"}).json()


def delete_test_draft(address: str, draft_id: str) -> dict:
    """Only a dry run's draft ("[TEST] …" subject) can be deleted, by the operator's command."""
    d = get_draft(address, draft_id)
    heads = {h["name"].lower(): h["value"] for h in ((d.get("message") or {}).get("payload") or {}).get("headers") or []}
    if not (heads.get("subject") or "").startswith(TEST_PREFIX):
        raise Refused("only a [TEST] draft can be deleted")
    _request(address, "DELETE", f"drafts/{draft_id}", _test_only=True)
    return {"deleted": draft_id, "subject": heads.get("subject")}


def draft_link(address: str, message_id: str) -> str:
    return f"https://mail.google.com/mail/u/?authuser={address}#drafts?compose={message_id}"
