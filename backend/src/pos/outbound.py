"""Outbound actions behind the approval queue (constitution U1, step 4).

An agent asks with `request(...)` (MCP `request_outbound`); nothing leaves
PersonalOS until the owner approves it. On approval, `execute` runs the
provider, after pos.guard's check_outbound confirms the approval.

Providers are configured by the owner with credentials (they stay off until
then). When one is off, the approved item becomes a task for the owner with
the prepared content, so nothing is lost and nothing is sent silently.

- email.send     SMTP (e.g. Gmail with an app password): POS_SMTP_HOST, POS_SMTP_PORT,
                 POS_SMTP_USER, POS_SMTP_PASSWORD, POS_SMTP_FROM
- github.comment GitHub REST: POS_GITHUB_TOKEN
- discord.post   Discord webhook: POS_DISCORD_WEBHOOK_URL
"""

import json
import os
import smtplib
import sqlite3
from email.message import EmailMessage

import httpx

from . import actors, approvals, audit, tasks
from .core import Ctx, now_iso

ACTIONS = ("email.send", "github.comment", "discord.post")
REQUIRED = {
    "email.send": ("to", "subject", "body"),
    "github.comment": ("repo", "number", "body"),
    "discord.post": ("content",),
}


def configured() -> dict[str, bool]:
    return {
        "email.send": bool(os.environ.get("POS_SMTP_HOST") and os.environ.get("POS_SMTP_USER")),
        "github.comment": bool(os.environ.get("POS_GITHUB_TOKEN")),
        "discord.post": bool(os.environ.get("POS_DISCORD_WEBHOOK_URL")),
    }


def request(conn: sqlite3.Connection, ctx: Ctx, action: str, payload: dict, task_id: int | None = None) -> dict:
    if action not in ACTIONS:
        raise tasks.Invalid(f"action must be one of {ACTIONS}")
    missing = [k for k in REQUIRED[action] if not payload.get(k)]
    if missing:
        raise tasks.Invalid(f"{action} needs {missing}")
    return approvals.request(conn, ctx, action, {"payload": payload}, task_id)


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


def _discord(p: dict) -> dict:
    r = httpx.post(os.environ["POS_DISCORD_WEBHOOK_URL"], json={"content": p["content"][:2000]}, timeout=30)
    r.raise_for_status()
    return {"http_status": r.status_code}


PROVIDERS = {"email.send": _email, "github.comment": _github, "discord.post": _discord}


def execute(conn: sqlite3.Connection, approval: dict) -> dict:
    """Run an approved outbound action. Called by the approval queue."""
    from .guard import policy
    from .integrations import guard_actor

    action = approval["action"]
    requester = approval["requested_by"]
    policy.check_outbound(guard_actor(conn, requester), action,
                          approval_id=str(approval["id"])).raise_if_not_allowed()
    payload = approval["details"].get("payload", {})
    ctx = Ctx(requester, via="outbound")
    if not configured()[action]:
        owner = actors.owner_id(conn)
        t = tasks.create(conn, Ctx(owner, via="system"), {
            "title": f"Send by hand: {action} (connector not set up)",
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
    audit.log(conn, ctx, f"outbound:{action}", "approval", approval["id"], **result)
    if approval.get("task_id"):
        tasks.update(conn, ctx, approval["task_id"], {"progress_note": f"{action}: {result['status']}"})
    return result


def install() -> None:
    from .guard import policy

    for action in ACTIONS:  # the guard knows every action that has a provider here
        policy.register_outbound_action(action)
    approvals.on_approved(lambda conn, a: execute(conn, a) if a["action"] in ACTIONS else None)
