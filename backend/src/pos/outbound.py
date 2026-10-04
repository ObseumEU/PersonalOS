"""Outbound actions (constitution Ú1, amended 2026-09-27; e-mail per the owner's decision of 2026-10-04).

An agent sends with `request(...)` (MCP `request_outbound`). The guard classifies every send
(pos.guard.policy.classify_outbound): money, commitments (contracts, price quotes) and the owner's personal
channels wait in the approval queue and run on approval (`execute`); ordinary work runs at once
(`send_now`). The sender may pass `kind`; it moves a send toward approval, never away from it.

Every run of an executor goes through the ledger (pos.outbound_ledger): idempotent per (action, thread or
target, content hash), rate-limited per agent, per recipient and per company, audited (`outbound:<action>`)
and in the CEO's daily digest (`digest`, with `outbound_stats`).

Executors:
- email.send      **a Gmail draft** for the owner by default (`email.mode = draft`, pos.outbound_gmail): in the
                  right mailbox, as a reply in the thread (thread_id) or a new message (to, subject), with
                  David's signature; the owner gets one "Čeká na tebe" item with the link (pos.outbound_drafts)
                  and sends it himself. `email.mode = auto` (Nastavení) sends it (gmail.send).
- github.comment  comment on an issue or PR (repo, number, body)            POS_GITHUB_TOKEN
- github.issue    open an issue (repo, title, body, labels?)                POS_GITHUB_TOKEN
- github.review   review a pull request (repo, number, body, event?)       POS_GITHUB_TOKEN
- github.pr       open a pull request (repo, title, head, base?, body?, draft?)  POS_GITHUB_TOKEN
- discord.post    a Discord webhook (content)                               POS_DISCORD_WEBHOOK_URL
- linkedin.post   the owner's LinkedIn (text, image_file_id?, image_alt?): always approval first, then the
                  Posts API, or "připraveno k publikaci" until LinkedIn is connected (pos.outbound_linkedin)
- payment, web.post  never automatic: on approval they become a task for the owner
- web.publish     is not an action: the Kniha web goes out through its `production` branch and the
                  kniha-deployer (the Kniha agents' instructions)

POS_OUTBOUND_DRY_RUN=1: everything is classified, deduplicated, limited, built and audited, nothing leaves.
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx

from . import actors, approvals, audit, outbound_ledger as ledger, tasks
from .core import Ctx, now_iso

ACTIONS = ("email.send", "github.comment", "github.issue", "github.review", "github.pr", "discord.post",
           "linkedin.post", "payment", "web.post")
REQUIRED = {
    "email.send": ("body",),
    "github.comment": ("repo", "number", "body"),
    "github.issue": ("repo", "title", "body"),
    "github.review": ("repo", "number", "body"),
    "github.pr": ("repo", "title", "head"),
    "discord.post": ("content",),
    "linkedin.post": ("text",),
    "payment": ("to", "amount", "reason"),
    "web.post": ("url", "content"),
}
# Only ever done by a person: approved, they become a task for the owner.
OWNER_ONLY = {"payment", "web.post"}
WEB_PUBLISH = ("web.publish is not an outbound action: the Kniha web goes out through its production branch "
               "(git checkout production && git merge --ff-only agent/<téma>); the kniha-deployer pushes and "
               "deploys it within ~3 minutes (see your instructions).")


def dry_run() -> bool:
    return os.environ.get("POS_OUTBOUND_DRY_RUN", "").lower() in ("1", "true", "yes")


def configured() -> dict[str, bool]:
    from . import outbound_gmail, outbound_linkedin

    gh = bool(os.environ.get("POS_GITHUB_TOKEN"))
    return {
        "email.send": outbound_gmail.can("draft") or outbound_gmail.can("send"),
        "github.comment": gh, "github.issue": gh, "github.review": gh, "github.pr": gh,
        "discord.post": bool(os.environ.get("POS_DISCORD_WEBHOOK_URL")),
        "linkedin.post": bool(outbound_linkedin.connected()),
        "payment": False,
        "web.post": False,
    }


def classify(action: str, payload: dict, kind: str | None = None) -> tuple[str, str]:
    """(kind, reason): 'ordinary' goes out now; money, commitment, personal_channel wait for approval."""
    from .guard import policy

    try:
        return policy.classify_outbound(action, payload, kind)
    except ValueError as e:
        raise tasks.Invalid(str(e)) from None


def validate(action: str, payload: dict) -> None:
    if action == "web.publish":
        raise tasks.Invalid(WEB_PUBLISH)
    if action not in ACTIONS:
        raise tasks.Invalid(f"action must be one of {ACTIONS}")
    if not isinstance(payload, dict):
        raise tasks.Invalid("payload is an object")
    missing = [k for k in REQUIRED[action] if not payload.get(k)]
    if action == "email.send" and not payload.get("thread_id"):
        missing += [k for k in ("to", "subject") if not payload.get(k)]
    if missing:
        raise tasks.Invalid(f"{action} needs {missing}" + (" (or thread_id for a reply)" if action == "email.send"
                                                           and "to" in missing else ""))


def request(conn: sqlite3.Connection, ctx: Ctx, action: str, payload: dict, task_id: int | None = None,
            why: str = "", kind: str | None = None) -> dict:
    """Send an outbound action. Ordinary work goes out now (e-mail as a draft for the owner, audited, in the
    CEO's daily digest); money, commitments and the owner's personal channels go to the approval queue."""
    validate(action, payload)
    from .access import service as access

    # The capability is the Access manager's to grant (Ú5); what needs approval is the guard's call (Ú1).
    access.require_outbound(conn, ctx, action)
    found, reason = classify(action, payload, kind)
    if found != "ordinary" or action in OWNER_ONLY:
        details = {"payload": payload, "kind": found, "reason": reason, **({"why": why} if why else {})}
        if action == "linkedin.post":  # the card shows the final post: its text and its image
            details["text"] = str(payload.get("text") or "")
            if payload.get("image_file_id"):
                details["image_url"] = f"/api/files/{int(payload['image_file_id'])}/content"
                details["image_alt"] = str(payload.get("image_alt") or "")
        out = approvals.request(conn, ctx, action, details, task_id)
        return {**out, "sent": False, "kind": found,
                "note": f"waits for the owner's approval ({reason}); it goes out once approved"}
    return send_now(conn, ctx, action, payload, task_id=task_id, why=why, kind=found)


def send_now(conn: sqlite3.Connection, ctx: Ctx, action: str, payload: dict, *, task_id: int | None = None,
             why: str = "", kind: str | None = None) -> dict:
    """Ordinary outbound (Ú1): the guard confirms it needs no approval, then the executor runs."""
    from .guard import policy
    from .integrations import guard_actor

    policy.check_outbound(guard_actor(conn, ctx.actor_id), action, payload=payload,
                          kind=kind).raise_if_not_allowed()
    result = _dispatch(conn, ctx, action, payload, kind="ordinary", task_id=task_id, why=why)
    return {**result, "sent": result["status"] == "sent", "kind": "ordinary", "action": action}


def _dispatch(conn: sqlite3.Connection, ctx: Ctx, action: str, payload: dict, *, kind: str,
              task_id: int | None = None, why: str = "", approval_id: int | None = None) -> dict:
    """The ledger around one executor run: duplicate? over a limit? configured? then run, record, audit."""
    def done(result: dict, row_id: int | None = None) -> dict:
        if row_id is None:
            row_id = ledger.log_attempt(conn, actor_id=ctx.actor_id, action=action, kind=kind, payload=payload,
                                        result=result, task_id=task_id, approval_id=approval_id)
        audit.log(conn, ctx, f"outbound:{action}", "approval" if approval_id else ("task" if task_id else None),
                  approval_id or task_id, kind=kind, summary=summarize(action, payload),
                  body_sha256=body_hash(payload), outbound_id=row_id, **({"why": why[:300]} if why else {}),
                  **{k: v for k, v in result.items() if k not in ("draft_text", "why", "kind", "summary")})
        if task_id:
            tasks.update(conn, ctx, task_id, {"progress_note": f"{action}: {result['status']}"})
        return {**result, "outbound_id": row_id}

    prev = ledger.previous(conn, ledger.key_of(action, payload))
    if prev is not None:
        first = json.loads(prev["result"] or "{}")
        return done({"status": "duplicate", "first": prev["id"], "first_status": prev["status"],
                     **{k: first[k] for k in ("link", "url", "message_id") if first.get(k)},
                     "note": "the same content to the same thread or target was already handled; nothing new done"})
    limit = ledger.over_limit(conn, ctx.actor_id, action, payload) if not approval_id else None
    if limit:
        return done({"status": "rate_limited", "note": f"{limit}; nothing was sent. Go on tomorrow or tell your lead."})
    custom = PROVIDERS.get(action) is not _DEFAULT_PROVIDERS.get(action)  # a test's stand-in
    if not custom and not configured()[action] and not dry_run():
        return done({"status": "not_configured",
                     "note": f"the {action} connector is not set up, so nothing was sent; tell your lead"})
    row_id = ledger.claim(conn, actor_id=ctx.actor_id, action=action, kind=kind, payload=payload, task_id=task_id,
                          approval_id=approval_id)
    conn.commit()  # the claim is visible to a parallel identical call; no lock is held while the provider runs
    try:
        if action == "email.send" and not custom:
            result = _email(conn, payload, task_id=task_id, why=why)
        elif dry_run():
            result = {"status": "dry_run"}
        else:
            result = {"status": "sent", **PROVIDERS[action](payload)}
    except Exception as e:  # noqa: BLE001 - report any provider failure, never retry silently
        result = {"status": "failed", "error": str(e)[:500]}
    ledger.record(conn, row_id, result)
    if result["status"] == "drafted":
        from . import outbound_drafts

        conn.execute("UPDATE outbound_sends SET campaign = ? WHERE id = ?",
                     (outbound_drafts.campaign_of(payload, task_id, row_id), row_id))
        item = outbound_drafts.owner_item(conn, row_id)
        if item:
            result["owner_item"] = item["ref"]
        result["note"] = ("a Gmail draft for the owner (the owner's rule for e-mail): he sends it from his "
                          "'Čeká na tebe' item; do not send it again, it counts as done for your task")
    return done(result, row_id)


def _email(conn: sqlite3.Connection, payload: dict, *, task_id: int | None, why: str) -> dict:
    from . import outbound_gmail as og

    how = og.mode_for(conn, payload)
    prepared = og.prepare(payload, conn=conn, task_id=task_id)
    if dry_run():
        return {"status": "dry_run", "mode": how, **og._headers(prepared), "text": prepared["text"][:2000]}
    result = og.draft(prepared) if how == "draft" else og.send(prepared)
    if why:
        result["why"] = why[:300]
    return {**result, "mode": how}


def body_hash(p: dict) -> str:
    """Recognises our own text when it comes back (e.g. our question as a GitHub comment)."""
    import hashlib

    return hashlib.sha256(str(p.get("body") or p.get("content") or p.get("text") or "").strip().encode()).hexdigest()


def summarize(action: str, p: dict) -> str:
    """One line for the audit log and the CEO's digest (never the whole body)."""
    if action == "email.send":
        to = p.get("to") or f"thread {p.get('thread_id')}"
        return f"to {to}: {str(p.get('subject') or '')[:120]}"
    if action in ("github.comment", "github.review"):
        return f"{p.get('repo')}#{p.get('number')}: {str(p.get('body') or '')[:120]}"
    if action in ("github.issue", "github.pr"):
        return f"{p.get('repo')}: {str(p.get('title') or '')[:120]}"
    if action == "discord.post":
        return str(p.get("content") or "")[:140]
    if action == "linkedin.post":
        return str(p.get("text") or "")[:140]
    return json.dumps(p, ensure_ascii=False)[:140]


# ------------------------------------------------------------------ providers

def _gh_headers() -> dict:
    return {"Authorization": f"Bearer {os.environ['POS_GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"}


def _email_unused(p: dict) -> dict:  # e-mail runs through pos.outbound_gmail (needs the connection)
    raise RuntimeError("email.send runs through pos.outbound_gmail")


def _github(p: dict) -> dict:
    r = httpx.post(f"https://api.github.com/repos/{p['repo']}/issues/{int(p['number'])}/comments",
                   headers=_gh_headers(), json={"body": p["body"]}, timeout=30)
    r.raise_for_status()
    return {"url": r.json().get("html_url")}


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


def _github_pr(p: dict) -> dict:
    body = {"title": p["title"], "head": p["head"], "base": p.get("base") or "main", "body": p.get("body") or "",
            "draft": bool(p.get("draft"))}
    r = httpx.post(f"https://api.github.com/repos/{p['repo']}/pulls", headers=_gh_headers(), json=body, timeout=30)
    r.raise_for_status()
    return {"url": r.json().get("html_url"), "number": r.json().get("number")}


def _discord(p: dict) -> dict:
    r = httpx.post(os.environ["POS_DISCORD_WEBHOOK_URL"], json={"content": p["content"][:2000]}, timeout=30)
    r.raise_for_status()
    return {"http_status": r.status_code}


PROVIDERS = {"email.send": _email_unused, "github.comment": _github, "github.issue": _github_issue,
             "github.review": _github_review, "github.pr": _github_pr, "discord.post": _discord}
_DEFAULT_PROVIDERS = dict(PROVIDERS)


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
    kind = approval["details"].get("kind") or "ordinary"
    if action == "linkedin.post":
        from . import outbound_linkedin

        result = outbound_linkedin.execute(conn, approval, payload)
        row = ledger.log_attempt(conn, actor_id=requester, action=action, kind=kind, payload=payload,
                                 result={k: v for k, v in result.items() if k != "text"},
                                 task_id=approval.get("task_id"), approval_id=approval["id"])
        audit.log(conn, ctx, f"outbound:{action}", "approval", approval["id"], kind=kind,
                  summary=summarize(action, payload), outbound_id=row,
                  **{k: v for k, v in result.items() if k != "text"})
    elif action in OWNER_ONLY or (not configured()[action] and PROVIDERS.get(action) is _DEFAULT_PROVIDERS.get(action)):
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
        audit.log(conn, ctx, f"outbound:{action}", "approval", approval["id"], kind=kind,
                  summary=summarize(action, payload), **result)
    else:
        result = _dispatch(conn, ctx, action, payload, kind=kind, task_id=approval.get("task_id"),
                           approval_id=approval["id"])
        result = {k: v for k, v in result.items() if k != "draft_text"}
    conn.execute("UPDATE approvals SET result = ?, executed_at = ? WHERE id = ?",
                 (json.dumps(result, ensure_ascii=False), now_iso(), approval["id"]))
    if approval.get("task_id") and action in ("linkedin.post", *OWNER_ONLY):
        tasks.update(conn, ctx, approval["task_id"], {"progress_note": f"{action}: {result['status']}"})
    return result


def install() -> None:
    from .guard import policy

    for action in ACTIONS:  # the guard knows every action that has a provider here
        policy.register_outbound_action(action)
    approvals.on_approved(lambda conn, a: execute(conn, a) if a["action"] in ACTIONS else None)


def outbound_stats(conn: sqlite3.Connection, days: int = 7) -> dict:
    """Sent per day, per agent and kind, replies received, drafts waiting and the owner's trust in them."""
    return ledger.outbound_stats(conn, days)


# ------------------------------------------------------------------ the CEO's daily review

DIGEST_KEY = "outbound.digest_at"


def digest(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """Everything sent since the last digest, in one message for the CEO (Ú1: the CEO
    reviews outbound daily), with the drafts that wait for the owner and the week's numbers.
    Quiet days send nothing. Without a CEO, the owner gets it."""
    from . import business, chat, outbound_drafts, settings_store, wake

    now = now or datetime.now(timezone.utc)
    since = settings_store.get(conn, DIGEST_KEY) or (now - timedelta(days=1)).isoformat(timespec="seconds")
    rows = conn.execute(
        """SELECT l.*, a.name AS actor_name FROM audit_log l LEFT JOIN actors a ON a.id = l.actor_id
           WHERE l.action LIKE 'outbound:%' AND l.at > ? AND l.at <= ? ORDER BY l.id""",
        (since, now.isoformat(timespec="seconds"))).fetchall()
    sys_ctx = business.system_ctx(conn)
    settings_store.put(conn, sys_ctx, DIGEST_KEY, now.isoformat(timespec="seconds"))
    drafts = outbound_drafts.waiting(conn)
    if not rows and not drafts:
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
    week = outbound_stats(conn, 7)
    trust = week["draft_trust"]
    wait_lines = [f"- [{d['to'] or '?'}: {str(d['subject'] or '')[:80]}]({d['link']}) · {d['agent'] or '?'} · "
                  f"od {d['created_at'][:16].replace('T', ' ')}" for d in drafts[:15]]
    body = "\n".join([
        f"**Odchozí za poslední den** ({len(rows)}: {summary or 'nic'})",
        "Ú1: běžné odchozí jde ven bez schválení a ty ho denně procházíš. Něco špatně (tón, adresát, "
        "slib, cena)? Oprav to s odesílatelem; peníze, závazky a osobní kanály majitele patří do schválení.",
        "", *lines,
        *(["", f"**Koncepty e-mailů čekají na majitele: {len(drafts)}**", *wait_lines] if drafts else []),
        "", f"7 dní: odesláno {week['sent']}, odpovědi {week['replies']} z {week['threads_sent']} vláken; "
            f"koncepty: beze změny {trust['sent_unchanged']}, upravené {trust['sent_edited']}, "
            f"zahozené {trust['discarded']}, čeká {trust['waiting']}."])
    ceo = business.ceo_id(conn)
    to = ceo or actors.owner_id(conn)
    chat.send_dm(conn, sys_ctx, to, body[:3900], system=True)
    audit.log(conn, sys_ctx, "outbound_digest", "actor", to, items=len(rows), drafts=len(drafts), since=since)
    if ceo:
        wake.wake(ceo)
    conn.commit()
    return {"sent": True, "items": len(rows), "to": to}
