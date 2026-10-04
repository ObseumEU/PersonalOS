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
             "stránce Tým → Práce).")
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
