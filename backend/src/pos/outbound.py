"""Outbound actions (constitution Ú1, amended 2026-09-27).

An agent sends with `request(...)` (MCP `request_outbound`). Ordinary work (e-mail
and customer replies, Discord, GitHub comments, issues and reviews) goes out at
once: pos.guard's check_outbound classifies it, the provider runs, and the send is
audited (`outbound:<action>`); the CEO gets a daily digest of everything sent
(`digest`). Only money, commitments (contracts, price quotes) and the owner's
personal channels wait in the approval queue; on approval `execute` runs them.
The sender may pass `kind`; it moves a send toward approval, never away from it.

Providers are configured by the owner with credentials (they stay off until
then). When one is off, an ordinary send returns not_configured (nothing is sent);
an approved item becomes a task for the owner with the prepared content.

- email.send     SMTP (e.g. Gmail with an app password): POS_SMTP_HOST, POS_SMTP_PORT,
                 POS_SMTP_USER, POS_SMTP_PASSWORD, POS_SMTP_FROM
- github.comment GitHub REST: POS_GITHUB_TOKEN
- github.issue   open an issue (repo, title, body, labels?): POS_GITHUB_TOKEN
- github.review  review a pull request (repo, number, body, event COMMENT|APPROVE|REQUEST_CHANGES)
- discord.post   Discord webhook: POS_DISCORD_WEBHOOK_URL
- payment, web.post  never automatic: on approval they become a task for the owner
"""

import json
import os
import smtplib
import sqlite3
from email.message import EmailMessage

import httpx

from datetime import datetime, timedelta, timezone

from . import actors, approvals, audit, tasks
from .core import Ctx, now_iso

ACTIONS = ("email.send", "github.comment", "github.issue", "github.review", "discord.post", "payment", "web.post")
REQUIRED = {
    "email.send": ("to", "subject", "body"),
    "github.comment": ("repo", "number", "body"),
    "github.issue": ("repo", "title", "body"),
    "github.review": ("repo", "number", "body"),
    "discord.post": ("content",),
    "payment": ("to", "amount", "reason"),
    "web.post": ("url", "content"),
}
# Only ever done by a person: approved, they become a task for the owner.
OWNER_ONLY = {"payment", "web.post"}


def configured() -> dict[str, bool]:
    return {
        "email.send": bool(os.environ.get("POS_SMTP_HOST") and os.environ.get("POS_SMTP_USER")),
        "github.comment": bool(os.environ.get("POS_GITHUB_TOKEN")),
        "github.issue": bool(os.environ.get("POS_GITHUB_TOKEN")),
        "github.review": bool(os.environ.get("POS_GITHUB_TOKEN")),
        "discord.post": bool(os.environ.get("POS_DISCORD_WEBHOOK_URL")),
        "payment": False,
        "web.post": False,
    }


def classify(action: str, payload: dict, kind: str | None = None) -> tuple[str, str]:
    """(kind, reason): 'ordinary' is sent now; money, commitment, personal_channel wait for approval."""
    from .guard import policy

    try:
        return policy.classify_outbound(action, payload, kind)
    except ValueError as e:
        raise tasks.Invalid(str(e)) from None


def request(conn: sqlite3.Connection, ctx: Ctx, action: str, payload: dict, task_id: int | None = None,
            why: str = "", kind: str | None = None) -> dict:
    """Send an outbound action. Ordinary work goes out now (audited, in the CEO's daily
    digest); money, commitments and the owner's personal channels go to the approval queue."""
    if action not in ACTIONS:
        raise tasks.Invalid(f"action must be one of {ACTIONS}")
    missing = [k for k in REQUIRED[action] if not payload.get(k)]
    if missing:
        raise tasks.Invalid(f"{action} needs {missing}")
    from .access import service as access

    # The capability is the Access manager's to grant (Ú5); what needs approval is the guard's call (Ú1).
    access.require_outbound(conn, ctx, action)
    found, reason = classify(action, payload, kind)
    if found != "ordinary" or action in OWNER_ONLY:
        details = {"payload": payload, "kind": found, "reason": reason, **({"why": why} if why else {})}
        out = approvals.request(conn, ctx, action, details, task_id)
        return {**out, "sent": False, "kind": found,
                "note": f"waits for the owner's approval ({reason}); it goes out once approved"}
    return send_now(conn, ctx, action, payload, task_id=task_id, why=why, kind=found)


def send_now(conn: sqlite3.Connection, ctx: Ctx, action: str, payload: dict, *, task_id: int | None = None,
             why: str = "", kind: str | None = None) -> dict:
    """Ordinary outbound (Ú1): the guard confirms it needs no approval, the provider runs,
    the audit log records it (the CEO's daily digest reads it from there)."""
    from .guard import policy
    from .integrations import guard_actor

    policy.check_outbound(guard_actor(conn, ctx.actor_id), action, payload=payload,
                          kind=kind).raise_if_not_allowed()
    if not configured()[action]:
        result = {"status": "not_configured",
                  "note": f"the {action} connector is not set up, so nothing was sent; tell your lead"}
    else:
        try:
            result = {"status": "sent", **PROVIDERS[action](payload)}
        except Exception as e:  # noqa: BLE001 - report any provider failure, never retry silently
            result = {"status": "failed", "error": str(e)[:500]}
    audit.log(conn, ctx, f"outbound:{action}", "task" if task_id else None, task_id,
              kind="ordinary", summary=summarize(action, payload), body_sha256=body_hash(payload),
              **({"why": why[:300]} if why else {}), **result)
    if task_id:
        tasks.update(conn, ctx, task_id, {"progress_note": f"{action}: {result['status']}"})
    return {**result, "sent": result["status"] == "sent", "kind": "ordinary", "action": action}


def body_hash(p: dict) -> str:
    """Recognises our own text when it comes back (e.g. our question as a GitHub comment)."""
    import hashlib

    return hashlib.sha256(str(p.get("body") or p.get("content") or "").strip().encode()).hexdigest()


def summarize(action: str, p: dict) -> str:
    """One line for the audit log and the CEO's digest (never the whole body)."""
    if action == "email.send":
        return f"to {p.get('to')}: {str(p.get('subject') or '')[:120]}"
    if action in ("github.comment", "github.review"):
        return f"{p.get('repo')}#{p.get('number')}: {str(p.get('body') or '')[:120]}"
    if action == "github.issue":
        return f"{p.get('repo')}: {str(p.get('title') or '')[:120]}"
    if action == "discord.post":
        return str(p.get("content") or "")[:140]
    return json.dumps(p, ensure_ascii=False)[:140]


# ------------------------------------------------------------------ providers

def _email(p: dict) -> dict:
    msg = EmailMessage()
    msg["From"] = os.environ.get("POS_SMTP_FROM") or os.environ["POS_SMTP_USER"]
    msg["To"] = p["to"]
    msg["Subject"] = p["subject"]
    if p.get("in_reply_to"):
        msg["In-Reply-To"] = p["in_reply_to"]
    msg.set_content(p["body"])
    port = int(os.environ.get("POS_SMTP_PORT", "587"))
    with smtplib.SMTP(os.environ["POS_SMTP_HOST"], port, timeout=30) as s:
        s.starttls()
        s.login(os.environ["POS_SMTP_USER"], os.environ["POS_SMTP_PASSWORD"])
        s.send_message(msg)
    return {"sent_to": p["to"]}


def _github(p: dict) -> dict:
    r = httpx.post(
        f"https://api.github.com/repos/{p['repo']}/issues/{int(p['number'])}/comments",
        headers={"Authorization": f"Bearer {os.environ['POS_GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"},
        json={"body": p["body"]}, timeout=30,
    )
    r.raise_for_status()
    return {"url": r.json().get("html_url")}


def _gh_headers() -> dict:
    return {"Authorization": f"Bearer {os.environ['POS_GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"}


def _github_issue(p: dict) -> dict:
    body = {"title": p["title"], "body": p["body"]}
    if p.get("labels"):
        body["labels"] = list(p["labels"])
    r = httpx.post(f"https://api.github.com/repos/{p['repo']}/issues", headers=_gh_headers(), json=body, timeout=30)
    r.raise_for_status()
    return {"url": r.json().get("html_url"), "number": r.json().get("number")}


def _github_review(p: dict) -> dict:
    event = (p.get("event") or "COMMENT").upper()
    if event not in ("COMMENT", "APPROVE", "REQUEST_CHANGES"):
        raise ValueError("event is COMMENT, APPROVE or REQUEST_CHANGES")
    r = httpx.post(f"https://api.github.com/repos/{p['repo']}/pulls/{int(p['number'])}/reviews",
                   headers=_gh_headers(), json={"body": p["body"], "event": event}, timeout=30)
    r.raise_for_status()
    return {"url": r.json().get("html_url"), "state": r.json().get("state")}


def _discord(p: dict) -> dict:
    r = httpx.post(os.environ["POS_DISCORD_WEBHOOK_URL"], json={"content": p["content"][:2000]}, timeout=30)
    r.raise_for_status()
    return {"http_status": r.status_code}


PROVIDERS = {"email.send": _email, "github.comment": _github, "github.issue": _github_issue,
             "github.review": _github_review, "discord.post": _discord}


def execute(conn: sqlite3.Connection, approval: dict) -> dict:
    """Run an approved outbound action. Called by the approval queue."""
    from .guard import policy
    from .integrations import guard_actor

    action = approval["action"]
    requester = approval["requested_by"]
    payload = approval["details"].get("payload", {})
    policy.check_outbound(guard_actor(conn, requester), action, approval_id=str(approval["id"]),
                          payload=payload).raise_if_not_allowed()
    ctx = Ctx(requester, via="outbound")
    if not configured()[action]:
        owner = actors.owner_id(conn)
        by_hand = action in OWNER_ONLY
        t = tasks.create(conn, Ctx(owner, via="system"), {
            "title": f"Do by hand: {action}" if by_hand else f"Send by hand: {action} (connector not set up)",
            "notes": f"Purpose: you approved {action} (approval {approval['id']}), but its connector is not set "
                     "up, so nothing was sent. Send it by hand, or set up the connector to send automatically.\n"
                     "Source: the approval queue.\n\n"
                     + json.dumps(payload, ensure_ascii=False, indent=2),
            "definition_of_done": "The content was sent by hand (or the connector is set up) and the "
                                  "recipient has it.",
            "priority": 2, "assignee": "me", "status": "next",
            "topic": "connectors",
        })
        result = {"status": "not_configured", "owner_task": t["ref"]}
    else:
        try:
            result = {"status": "sent", **PROVIDERS[action](payload)}
        except Exception as e:  # noqa: BLE001 - report any provider failure, never retry silently
            result = {"status": "failed", "error": str(e)[:500]}
    conn.execute("UPDATE approvals SET result = ?, executed_at = ? WHERE id = ?",
                 (json.dumps(result, ensure_ascii=False), now_iso(), approval["id"]))
    audit.log(conn, ctx, f"outbound:{action}", "approval", approval["id"],
              kind=approval["details"].get("kind"), summary=summarize(action, payload), **result)
    if approval.get("task_id"):
        tasks.update(conn, ctx, approval["task_id"], {"progress_note": f"{action}: {result['status']}"})
    return result


def install() -> None:
    from .guard import policy

    for action in ACTIONS:  # the guard knows every action that has a provider here
        policy.register_outbound_action(action)
    approvals.on_approved(lambda conn, a: execute(conn, a) if a["action"] in ACTIONS else None)


# ------------------------------------------------------------------ the CEO's daily review

DIGEST_KEY = "outbound.digest_at"


def digest(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """Everything sent since the last digest, in one message for the CEO (Ú1: the CEO
    reviews outbound daily). Quiet days send nothing. Without a CEO, the owner gets it."""
    from . import business, chat, settings_store, wake

    now = now or datetime.now(timezone.utc)
    since = settings_store.get(conn, DIGEST_KEY) or (now - timedelta(days=1)).isoformat(timespec="seconds")
    rows = conn.execute(
        """SELECT l.*, a.name AS actor_name FROM audit_log l LEFT JOIN actors a ON a.id = l.actor_id
           WHERE l.action LIKE 'outbound:%' AND l.at > ? AND l.at <= ? ORDER BY l.id""",
        (since, now.isoformat(timespec="seconds"))).fetchall()
    sys_ctx = business.system_ctx(conn)
    settings_store.put(conn, sys_ctx, DIGEST_KEY, now.isoformat(timespec="seconds"))
    if not rows:
        conn.commit()
        return {"sent": False, "items": 0}
    by_status: dict[str, int] = {}
    lines = []
    for r in rows:
        d = json.loads(r["detail"] or "{}")
        st = d.get("status") or "?"
        by_status[st] = by_status.get(st, 0) + 1
        how = "po schválení" if r["entity"] == "approval" else "přímo"
        if len(lines) < 40:
            lines.append(f"- {r['at'][:16].replace('T', ' ')} **{r['actor_name'] or '?'}** "
                         f"`{r['action'][9:]}` ({how}, {st}): {str(d.get('summary') or '')[:140]}")
    if len(rows) > 40:
        lines.append(f"- … a {len(rows) - 40} dalších (audit log, akce outbound:*)")
    summary = " · ".join(f"{k} {v}" for k, v in sorted(by_status.items()))
    body = "\n".join([
        f"**Odchozí za poslední den** ({len(rows)}: {summary})",
        "Ú1: běžné odchozí jde ven bez schválení a ty ho denně procházíš. Něco špatně (tón, adresát, "
        "slib, cena)? Oprav to s odesílatelem; peníze, závazky a osobní kanály majitele patří do schválení.",
        "", *lines])
    ceo = business.ceo_id(conn)
    to = ceo or actors.owner_id(conn)
    chat.send_dm(conn, sys_ctx, to, body[:3900], system=True)
    audit.log(conn, sys_ctx, "outbound_digest", "actor", to, items=len(rows), since=since)
    if ceo:
        wake.wake(ceo)
    conn.commit()
    return {"sent": True, "items": len(rows), "to": to}
