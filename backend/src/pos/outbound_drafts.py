"""The owner's side of e-mail drafts: one "Čeká na tebe" item per draft (grouped per campaign), the sync
that sees him send or discard a draft and closes the item, and the trust numbers ("důvěra v koncepty").

Grouping: drafts with the same `campaign` (or, without one, from the same task) share one item that lists
each draft with its [Otevřít v Gmailu] link and its state; a lone draft is "Koncept e-mailu pro <příjemce>:
<předmět>". The item closes itself when every draft in it is sent or discarded (`sync`, the scheduler's
`outbound_drafts`); the owner may also mark a draft by hand (`mark`, POST /api/outbound/drafts/{id}).
"""

import json
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone

from . import actors, audit, outbound_ledger as ledger, tasks
from .core import Ctx, Forbidden, NotFound, now_iso

TOPIC = "odchozi"
SOURCE = "outbound:drafts"
STATE_CS = {"drafted": "čeká na odeslání", "sent": "odesláno", "discarded": "zahozeno"}


def _ctx(conn: sqlite3.Connection) -> Ctx:
    from . import business

    return business.system_ctx(conn)


def campaign_of(payload: dict, task_id: int | None, row_id: int) -> str:
    c = str(payload.get("campaign") or "").strip()
    if c:
        return f"campaign:{c[:80]}"
    return f"task:{task_id}" if task_id else f"draft:{row_id}"


def _rows(conn: sqlite3.Connection, campaign: str) -> list[sqlite3.Row]:
    return conn.execute("""SELECT s.*, a.name AS actor_name FROM outbound_sends s LEFT JOIN actors a ON a.id = s.actor_id
                           WHERE s.campaign = ? AND s.action = 'email.send'
                           AND s.status IN ('drafted', 'sent', 'discarded') AND s.result LIKE '%"draft_id"%'
                           ORDER BY s.id""", (campaign,)).fetchall()


def _line(r: sqlite3.Row) -> str:
    res = json.loads(r["result"] or "{}")
    why = f" · {res['why'][:140]}" if res.get("why") else ""
    state = STATE_CS.get(r["status"], r["status"])
    if r["status"] == "sent" and res.get("owner_edited") is not None:
        state += " (upraveno)" if res["owner_edited"] else " (beze změny)"
    return (f"- [Otevřít v Gmailu]({res.get('link')}) · **{res.get('to') or r['recipient'] or '?'}** · "
            f"{res.get('subject') or ''} · {state} · od {r['actor_name'] or '?'}{why}")


def render(conn: sqlite3.Connection, campaign: str) -> tuple[str, str, bool]:
    """(title, notes, all_done) of a campaign's item."""
    rows = _rows(conn, campaign)
    waiting = [r for r in rows if r["status"] == "drafted"]
    if len(rows) == 1:
        res = json.loads(rows[0]["result"] or "{}")
        title = f"Koncept e-mailu pro {res.get('to') or rows[0]['recipient'] or '?'}: {res.get('subject') or ''}"
    else:
        name = campaign.split(":", 1)[1] if campaign.startswith("campaign:") else ""
        if campaign.startswith("task:"):
            t = conn.execute("SELECT title FROM tasks WHERE id = ?", (int(campaign[5:]),)).fetchone()
            name = t["title"] if t else name
        title = f"Koncepty e-mailů ({len(waiting)} z {len(rows)} čeká): {name}".rstrip(": ")
    notes = ("### Co udělat\nOtevři koncept v Gmailu, zkontroluj ho a odešli (nebo uprav, nebo smaž). "
             "Nic se neodeslalo samo. Až koncept odešleš nebo smažeš, PersonalOS to při další synchronizaci "
             "pozná a položku zavře.\n\n### Koncepty\n" + "\n".join(_line(r) for r in rows) +
             "\n\n### Proč koncepty\nTvoje rozhodnutí: e-maily agentů jdou zatím jako koncepty. Až budeš "
             "agentům věřit, přepni v Nastavení „E-maily odesílat automaticky“ (důvěra v koncepty je na "
             "stránce Firma).")
    return title[:200], notes, not waiting


def owner_item(conn: sqlite3.Connection, row_id: int) -> dict | None:
    """Create or refresh the owner's one item for the draft's campaign."""
    row = conn.execute("SELECT * FROM outbound_sends WHERE id = ?", (row_id,)).fetchone()
    if row is None or not row["campaign"]:
        return None
    title, notes, done = render(conn, row["campaign"])
    ctx = _ctx(conn)
    existing = conn.execute("""SELECT t.id, t.status FROM outbound_sends s JOIN tasks t ON t.id = s.owner_task_id
                               WHERE s.campaign = ? AND t.archived_at IS NULL AND t.status NOT IN ('done')
                               ORDER BY s.id DESC LIMIT 1""", (row["campaign"],)).fetchone()
    if existing is not None:
        changes = {"title": title, "notes": notes}
        if done:
            changes.update({"status": "done"})
        # The owner's own item: closed in his name (the system may not mark work done).
        t = tasks.update(conn, Ctx(actors.owner_id(conn), via="outbound_drafts"), existing["id"], changes)
        task_id = existing["id"]
    elif done:
        return None
    else:
        t = tasks.create(conn, ctx, {
            "title": title, "notes": notes, "status": "next", "priority": 2, "topic": TOPIC, "source": SOURCE,
            "assignee": {"type": "human", "id": actors.owner_id(conn)},
            "definition_of_done": "Majitel koncepty odeslal, upravil nebo zahodil (zavře se samo)."})
        task_id = t["id"]
    conn.execute("UPDATE outbound_sends SET owner_task_id = ? WHERE campaign = ? AND owner_task_id IS NULL",
                 (task_id, row["campaign"]))
    return t


def _resolve(conn: sqlite3.Connection, r: sqlite3.Row, out: dict, how: str) -> None:
    res = json.loads(r["result"] or "{}")
    if out["state"] == "sent":
        at = out.get("sent_at") or now_iso()
        minutes = max(0, round((datetime.fromisoformat(at) - datetime.fromisoformat(r["created_at"])).total_seconds() / 60))
        res.update({"owner_sent_at": at, "owner_edited": out.get("edited"), "similarity": out.get("similarity"),
                    "minutes_to_send": minutes, "resolved_by": how})
        conn.execute("UPDATE outbound_sends SET status = 'sent', sent_at = ?, result = ? WHERE id = ?",
                     (at, json.dumps(res, ensure_ascii=False), r["id"]))
    else:
        res.update({"resolved_by": how, "discarded_at": now_iso()})
        conn.execute("UPDATE outbound_sends SET status = 'discarded', result = ? WHERE id = ?",
                     (json.dumps(res, ensure_ascii=False), r["id"]))
    audit.log(conn, _ctx(conn), f"outbound_draft_{out['state']}", "outbound_send", r["id"],
              to=res.get("to"), subject=res.get("subject"), edited=out.get("edited"), how=how)


def sync(conn: sqlite3.Connection, *, gmail_factory=None, days: int = 30) -> dict:
    """Drafts the owner sent or discarded since the last look: the ledger, the audit log, his item."""
    from . import outbound_gmail
    from .invoices import gapi

    ledger.ensure_schema(conn)
    factory = gmail_factory or gapi.Gmail
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    rows = conn.execute("""SELECT * FROM outbound_sends WHERE action = 'email.send' AND status = 'drafted'
                           AND created_at >= ? ORDER BY id LIMIT 100""", (since,)).fetchall()
    out = {"checked": 0, "sent": 0, "discarded": 0, "errors": 0}
    touched = set()
    for r in rows:
        conn.commit()  # no write lock while Gmail answers
        try:
            got = outbound_gmail.outcome(dict(r), gmail_factory=factory)
        except Exception:  # noqa: BLE001 - a token or Gmail hiccup: next time
            out["errors"] += 1
            continue
        out["checked"] += 1
        if got:
            _resolve(conn, r, got, "gmail")
            out[got["state"]] += 1
            touched.add(r["id"])
    for rid in touched:
        owner_item(conn, rid)
    conn.commit()
    return {k: v for k, v in out.items() if v}


def mark(conn: sqlite3.Connection, ctx: Ctx, row_id: int, state: str) -> dict:
    """The owner marks a draft by hand: sent (Odesláno) or discarded (Zahodit)."""
    if not actors.get(conn, ctx.actor_id)["is_owner"]:
        raise Forbidden("only the owner marks his drafts")
    if state not in ("sent", "discarded"):
        raise tasks.Invalid("state is sent or discarded")
    ledger.ensure_schema(conn)
    r = conn.execute("SELECT * FROM outbound_sends WHERE id = ? AND action = 'email.send'", (row_id,)).fetchone()
    if r is None:
        raise NotFound(f"draft {row_id}")
    if r["status"] != "drafted":
        raise tasks.Invalid(f"draft {row_id} is already {r['status']}")
    _resolve(conn, r, {"state": state, "sent_at": now_iso()}, "owner")
    owner_item(conn, row_id)
    conn.commit()
    return dict(conn.execute("SELECT id, status, result FROM outbound_sends WHERE id = ?", (row_id,)).fetchone())


def waiting(conn: sqlite3.Connection) -> list[dict]:
    ledger.ensure_schema(conn)
    rows = conn.execute("""SELECT s.*, a.name AS actor_name FROM outbound_sends s LEFT JOIN actors a ON a.id = s.actor_id
                           WHERE s.action = 'email.send' AND s.status = 'drafted' ORDER BY s.id""").fetchall()
    out = []
    for r in rows:
        res = json.loads(r["result"] or "{}")
        out.append({"id": r["id"], "to": res.get("to"), "subject": res.get("subject"), "link": res.get("link"),
                    "agent": r["actor_name"], "created_at": r["created_at"],
                    "owner_task": tasks.display_id(r["owner_task_id"]) if r["owner_task_id"] else None})
    return out


def draft_trust(conn: sqlite3.Connection, days: int = 30) -> dict:
    """How the owner treats the drafts: sent unchanged, sent edited, discarded; how fast. When unchanged is
    high over enough drafts, switching e-mail to automatic is safe ("důvěra v koncepty")."""
    ledger.ensure_schema(conn)
    since = (datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))).isoformat(timespec="seconds")
    rows = conn.execute("""SELECT status, result FROM outbound_sends WHERE action = 'email.send'
                           AND result LIKE '%"draft_id"%' AND created_at >= ?""", (since,)).fetchall()
    c = {"drafted": len(rows), "waiting": 0, "sent_unchanged": 0, "sent_edited": 0, "discarded": 0}
    minutes = []
    for r in rows:
        res = json.loads(r["result"] or "{}")
        if r["status"] == "drafted":
            c["waiting"] += 1
        elif r["status"] == "discarded":
            c["discarded"] += 1
        elif r["status"] == "sent":
            c["sent_edited" if res.get("owner_edited") else "sent_unchanged"] += 1
            if res.get("minutes_to_send") is not None:
                minutes.append(res["minutes_to_send"])
    resolved = c["sent_unchanged"] + c["sent_edited"] + c["discarded"]
    rate = round(c["sent_unchanged"] / resolved, 2) if resolved else None
    ready = resolved >= 20 and (rate or 0) >= 0.9 and c["discarded"] / max(resolved, 1) <= 0.05
    return {**c, "days": days, "resolved": resolved, "unchanged_rate": rate,
            "median_minutes_to_send": statistics.median(minutes) if minutes else None,
            "ready_for_auto": ready,
            "advice": ("Koncepty posíláš skoro beze změn: automatické odesílání je bezpečné zapnout." if ready else
                       f"Zatím {resolved} vyřízených konceptů; automatické odesílání doporučím od 20 s ≥ 90 % "
                       "beze změny.")}


# ------------------------------------------------------------------ agents fix or withdraw their own drafts

def find(conn: sqlite3.Connection, draft_id: str) -> dict:
    """A Gmail draft PersonalOS created and nobody sent yet: {kind: outbound|support, row, result, account,
    thread_id}. Any other draft id is refused (never a draft someone wrote in the mailbox himself)."""
    from .support import service as support

    draft_id = str(draft_id or "").strip()
    if not draft_id:
        raise tasks.Invalid("draft_id is empty (from gmail_create_draft's or request_outbound's result)")
    ledger.ensure_schema(conn)
    for r in conn.execute("""SELECT * FROM outbound_sends WHERE action = 'email.send' AND status = 'drafted'
                             AND result LIKE ? ORDER BY id DESC""", (f'%"draft_id": "{draft_id}"%',)):
        res = json.loads(r["result"] or "{}")
        if res.get("draft_id") == draft_id:
            return {"kind": "outbound", "row": r, "result": res, "account": res.get("account") or r["account"],
                    "thread_id": res.get("thread_id") or r["thread_id"]}
    support.ensure_schema(conn)
    r = conn.execute("SELECT * FROM support_threads WHERE draft_id = ? ORDER BY id DESC LIMIT 1", (draft_id,)).fetchone()
    if r is not None:
        return {"kind": "support", "row": r, "result": {}, "account": r["account"], "thread_id": r["thread_id"]}
    raise NotFound(f"draft {draft_id} is not one PersonalOS created that still waits (sent or deleted drafts, "
                   "and drafts nobody here made, are not touched)")


def _may_touch(conn: sqlite3.Connection, ctx: Ctx, d: dict) -> None:
    from . import agents
    from .support import service as support

    me = actors.get(conn, ctx.actor_id)
    if me["is_owner"]:
        return
    if d["kind"] == "outbound" and d["row"]["actor_id"] == ctx.actor_id:
        return
    if d["kind"] == "support" and ((me["role"] or "") == "customer_success"
                                   or agents.has_permission(conn, ctx.actor_id, "tool:gmail_create_draft")
                                   or agents.has_permission(conn, ctx.actor_id, support.PERMISSION)):
        return
    raise Forbidden("only the agent that wrote the draft (or Customer Success, for reply drafts) changes it")


def update_draft(conn: sqlite3.Connection, ctx: Ctx, draft_id: str, body: str, *, subject: str | None = None,
                 to: str | None = None, cc: str | None = None, gmail_factory=None) -> dict:
    """Replace the text of a draft PersonalOS created (the same draft, no duplicate); the ledger and the
    owner's item follow."""
    from . import outbound_gmail
    from .invoices import gapi
    from .support import gmail as gm
    from .support import service as support

    factory = gmail_factory or gapi.Gmail
    d = find(conn, draft_id)
    _may_touch(conn, ctx, d)
    body = (body or "").strip()
    if len(body) < 20:
        raise tasks.Invalid("the body is too short: the whole new text of the e-mail, without a signature")
    account, row = d["account"], d["row"]
    from . import grounding

    # The new text is checked like a new draft (pos.grounding): no dead links, no promise of what is not live.
    keys = row.keys()
    grounding.gate(conn, ctx, "gmail_update_draft", "\n".join(x for x in (subject, body) if x),
                   task_id=row["task_id"] if "task_id" in keys else None,
                   project_hint=row["project_slug"] if "project_slug" in keys else None)
    conn.commit()  # no write lock while Gmail answers
    if d["kind"] == "outbound":
        res = d["result"]
        reply = bool(res.get("in_reply_to")) and bool(d["thread_id"])
        payload = {"account": account, "body": body, "to": to or res.get("to"),
                   "cc": cc if cc is not None else res.get("cc"), "subject": subject or res.get("subject"),
                   **({"thread_id": d["thread_id"]} if reply else {})}
        p = outbound_gmail.prepare(payload, conn=conn, task_id=row["task_id"], gmail_factory=factory)
        made = gm.update_draft(account, draft_id, p["msg"], d["thread_id"])
        message_id = (made.get("message") or {}).get("id") or res.get("message_id") or ""
        res.update({"to": p["msg"]["To"], "cc": p["msg"]["Cc"], "subject": p["msg"]["Subject"],
                    "message_id": message_id, "link": gm.draft_link(account, message_id),
                    "draft_sha256": outbound_gmail._sha(p["text"]), "draft_text": p["text"][:6000],
                    "agent_updates": int(res.get("agent_updates") or 0) + 1, "updated_at": now_iso()})
        conn.execute("UPDATE outbound_sends SET result = ?, body_sha256 = ? WHERE id = ?",
                     (json.dumps(res, ensure_ascii=False, default=str), ledger.body_hash({"body": body}), row["id"]))
        owner_item(conn, row["id"])
        out = {"to": res["to"], "subject": res["subject"], "link": res["link"]}
    else:
        msgs = gm.thread_headers(account, row["thread_id"], factory)
        text = support._with_signature(body, support.signature(account, row["language"], row["project_slug"]))
        msg = gm.build_reply(msgs, account, text)
        made = gm.update_draft(account, draft_id, msg, row["thread_id"])
        message_id = (made.get("message") or {}).get("id") or row["draft_message_id"] or ""
        support._set(conn, row["id"], draft_message_id=message_id, draft_link=gm.draft_link(account, message_id))
        out = {"to": msg["To"], "subject": msg["Subject"], "link": gm.draft_link(account, message_id)}
    audit.log(conn, ctx, "gmail_update_draft", "outbound_send" if d["kind"] == "outbound" else "support_thread",
              row["id"], account=account, draft_id=draft_id, to=out["to"], subject=out["subject"])
    conn.commit()
    return {"draft_id": draft_id, **out, "updated": True, "sent": False}


def delete_draft(conn: sqlite3.Connection, ctx: Ctx, draft_id: str, reason: str = "") -> dict:
    """Withdraw a wrong or duplicate draft PersonalOS created: Gmail deletes only that draft, the ledger
    marks it discarded by the agent, the owner's item follows."""
    from .support import gmail as gm
    from .support import service as support

    d = find(conn, draft_id)
    _may_touch(conn, ctx, d)
    account, row = d["account"], d["row"]
    conn.commit()
    gm.delete_draft(account, draft_id)
    if d["kind"] == "outbound":
        res = {**d["result"], "resolved_by": "agent", "discarded_at": now_iso(), "discard_reason": reason[:300]}
        conn.execute("UPDATE outbound_sends SET status = 'discarded', result = ? WHERE id = ?",
                     (json.dumps(res, ensure_ascii=False, default=str), row["id"]))
        owner_item(conn, row["id"])
    else:
        support._set(conn, row["id"], draft_id=None, draft_message_id=None, draft_link=None)
        ledger.log_attempt(conn, actor_id=ctx.actor_id, action="email.draft_delete", kind="ordinary",
                           payload={"thread_id": row["thread_id"], "body": f"draft {draft_id}"},
                           result={"status": "draft_deleted", "account": account, "draft_id": draft_id,
                                   "thread_id": row["thread_id"], "reason": reason[:300]})
        if row["needs_task_id"]:  # the owner's "Koncept odpovědi" item: nothing to send until a new draft
            from . import comments

            comments.log(conn, _ctx(conn), row["needs_task_id"],
                         f"Koncept smazal agent{': ' + reason if reason else ''}. Čeká se na nový koncept.",
                         "progress")
    audit.log(conn, ctx, "gmail_delete_draft", "outbound_send" if d["kind"] == "outbound" else "support_thread",
              row["id"], account=account, draft_id=draft_id, reason=reason[:300] or None)
    conn.commit()
    return {"draft_id": draft_id, "deleted": True, "sent": False}


def register_mcp(mcp, session) -> None:
    from mcp.server.mcpserver import Context
    from mcp.server.mcpserver.exceptions import ToolError

    from . import mcp_server
    from .invoices import gapi
    from .outbound_gmail import Refused
    from .support import gmail as gm

    # Whoever may draft e-mail (request_outbound) may fix its own drafts; whose draft it is is checked inside.
    mcp_server.TOOL_PERMISSIONS.setdefault("gmail_update_draft", "approvals:request")
    mcp_server.TOOL_PERMISSIONS.setdefault("gmail_delete_draft", "approvals:request")

    @mcp.tool(description="Fix a Gmail draft PersonalOS created (by gmail_create_draft or request_outbound "
                          "email.send) instead of creating a second one: the same draft gets the new text. "
                          "draft_id: from that call's result. body: the whole new text without a signature "
                          "(it is added). subject/to/cc only for a new e-mail, not a reply. Never sends.")
    def gmail_update_draft(ctx: Context, draft_id: str, body: str, subject: str | None = None,
                           to: str | None = None, cc: str | None = None) -> dict:
        with session(ctx, "gmail_update_draft", draft_id=draft_id) as (conn, c):
            try:
                return update_draft(conn, Ctx(c.actor_id, via="mcp", run_id=c.run_id), draft_id, body,
                                    subject=subject, to=to, cc=cc)
            except (Refused, gm.Refused, gapi.GoogleError) as e:
                conn.rollback()
                raise ToolError(f"gmail_update_draft: {e}") from e

    @mcp.tool(description="Delete a wrong or duplicate Gmail draft PersonalOS created (only such drafts; never "
                          "a sent e-mail or a draft someone wrote himself). reason: one sentence. Recorded in "
                          "the outbound ledger; the owner's item follows. Never sends.")
    def gmail_delete_draft(ctx: Context, draft_id: str, reason: str = "") -> dict:
        with session(ctx, "gmail_delete_draft", draft_id=draft_id) as (conn, c):
            try:
                return delete_draft(conn, Ctx(c.actor_id, via="mcp", run_id=c.run_id), draft_id, reason)
            except (gm.Refused, gapi.GoogleError) as e:
                conn.rollback()
                raise ToolError(f"gmail_delete_draft: {e}") from e
