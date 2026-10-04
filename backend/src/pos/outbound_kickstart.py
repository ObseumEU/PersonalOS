"""One-off kick-start after the outbound release (2026-10): re-queue the outbound-ready work that stalled.
Dry run by default; the deployer runs it once after the deploy:

    python -m pos.outbound_kickstart            # what would change
    python -m pos.outbound_kickstart --apply    # do it (as the system, audited; idempotent)

1. **Obseum AI outreach** (Head of Growth): one task to write the first warm outreach (≤ 10 people who
   wrote to us or know us) as e-mail drafts for the owner (campaign `obseum-ai-outreach`), paid ads as
   `kind=money` approvals with the creative and the budget, LinkedIn posts as `linkedin.post`.
2. **Kniha**: the partners' first outreach (≤ 10 drafts, campaign `kniha-partneri`) for Kniha Growth & Sales;
   the warm-outreach batch only when the owner answered "Tvoje dávka 30–50 osobních zpráv" (T-586) with
   a send option (his contacts are his: the drafts go from his work mailbox for him to send).
3. **Customer replies awaiting send**: an agent task whose last word was "not configured" / nothing sent
   goes back to `next` with a comment: e-mail works now (as drafts).
4. **The owner's approved LinkedIn posts** (free-text approvals from before linkedin.post existed): each
   becomes an approved `linkedin.post` → "připraveno k publikaci" (his item with the text) until LinkedIn
   is connected, then one click publishes it. The Head of Growth's LinkedIn tasks hear of it.
Nothing is sent by this script.
"""

import argparse
import json
import sqlite3
import sys

from . import actors, approvals, audit, comments, outbound, settings_store, tasks
from .core import Ctx, now_iso

DONE_KEY = "outbound.kickstart_at"
SRC = "outbound:kickstart"
CONTACTS_TOPIC = "owner decision kniha davka kontaktu"
STALLED = ("not_configured", "connector is not set up", "connector není", "nebylo odesláno", "nic se neodeslalo")


def _agent(conn, name: str) -> int | None:
    r = actors.find_by_name(conn, name)
    return r["id"] if r is not None and not r["archived_at"] else None


def _has_task(conn, source: str) -> bool:
    return conn.execute("SELECT 1 FROM tasks WHERE source = ? AND archived_at IS NULL", (source,)).fetchone() is not None


def _contacts_decision(conn) -> dict | None:
    try:
        r = conn.execute("""SELECT ticket_id, status, decided_option FROM owner_asks WHERE topic_key = ?
                            ORDER BY id DESC LIMIT 1""", (CONTACTS_TOPIC,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return dict(r) if r else None


NEW_TASKS = {
    "obseum-ai": ("Head of Growth", {
        "title": "Obseum AI: první oslovení – koncepty e-mailů pro majitele (max 10)",
        "priority": 2, "topic": "obchod",
        "notes": ("### Proč\nZa 9 dní 0 odeslaných oslovení Obseum AI; plán a sales kit jsou hotové (T-404, T-407). "
                  "E-mail teď funguje: `request_outbound(\"email.send\", …)` vytvoří koncept v Gmailu (firemní "
                  "schránka, podpis Davida) a majitel ho jedním klikem odešle.\n\n"
                  "### Co udělat\n- Vyber nejvýš 10 lidí, kteří nám už psali nebo nás znají (`knowledge`: "
                  "poptávky, bývalí a současní zákazníci); žádné studené seznamy.\n- Každému krátký osobní e-mail "
                  "(jazyk podle jejich posledního e-mailu), `campaign: \"obseum-ai-outreach\"`, odpověď ve vlákně "
                  "přes `thread_id`, kde vlákno existuje.\n- Ceny, slevy a podmínky jen jako `kind=\"commitment\"`; "
                  "placená reklama (Obseum AI ads) jako `kind=\"money\"` s kreativou a rozpočtem.\n"
                  "- Zapiš je do poznámky Pipeline (datum, stav, další krok).\n\n### Odkud\nKick-start odchozí "
                  "komunikace (balík A, 2026-10)."),
        "definition_of_done": "Až 10 konceptů je v Gmailu (request_outbound status drafted), pipeline je aktuální; "
                              "reklama čeká ve schválení s rozpočtem."}),
    "kniha-partneri": ("Kniha Growth & Sales", {
        "title": "Kniha: první oslovení partnerů – koncepty e-mailů pro majitele (max 10)",
        "priority": 2, "topic": "kniha",
        "notes": ("### Proč\nWarm outreach i partneři stojí na nule. E-mail teď funguje: "
                  "`request_outbound(\"email.send\", …)` vytvoří koncept v Gmailu, majitel ho odešle.\n\n"
                  "### Co udělat\n- Z `marketing/partneri.csv` nejvýš 10 partnerů, šablony z T-285.\n"
                  "- `project: \"kniha\"` (podpis Rodinné příběhy), `campaign: \"kniha-partneri\"`.\n"
                  "- Provize, slevy, ceny jen `kind=\"commitment\"`; nic placeného bez `kind=\"money\"`.\n"
                  "- Do `partneri.csv` datum, stav „koncept u majitele“, další krok.\n\n### Odkud\nKick-start "
                  "odchozí komunikace (balík A, 2026-10)."),
        "definition_of_done": "Až 10 konceptů v Gmailu (status drafted), partneri.csv aktualizované."}),
}


def plan(conn: sqlite3.Connection) -> dict:
    out: dict = {"done_before": settings_store.get(conn, DONE_KEY), "new_tasks": [], "requeue": [],
                 "linkedin": [], "kniha_warm": None, "skipped": []}
    for key, (who, t) in NEW_TASKS.items():
        if _has_task(conn, f"{SRC}:{key}"):
            out["skipped"].append(f"{key}: task exists")
        elif _agent(conn, who) is None:
            out["skipped"].append(f"{key}: no {who}")
        else:
            out["new_tasks"].append({"key": key, "assignee": who, "title": t["title"]})
    d = _contacts_decision(conn)
    opt = str((d or {}).get("decided_option") or "")
    if d and opt and not opt.lower().startswith("nepošlu"):
        out["kniha_warm"] = {"ticket": tasks.display_id(d["ticket_id"]), "option": opt}
    else:
        out["skipped"].append("kniha warm outreach: the owner has not chosen a send option in T-586"
                              + (f" (status {d['status']})" if d else " (no decision card)"))
    rows = conn.execute("""SELECT t.id, t.title, t.status, a.name FROM tasks t JOIN actors a ON a.id = t.assignee_id
                           WHERE a.kind != 'human' AND t.archived_at IS NULL AND t.status IN ('waiting', 'next', 'review')
                           ORDER BY t.id""").fetchall()
    for r in rows:
        last = conn.execute("SELECT body FROM task_comments WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                            (r["id"],)).fetchone()
        text = ((last["body"] if last else "") or "").lower()
        if r["status"] == "waiting" and any(s in text for s in STALLED):
            out["requeue"].append({"ref": tasks.display_id(r["id"]), "title": r["title"], "agent": r["name"]})
    for a in conn.execute("""SELECT id, action, requested_by, task_id, details FROM approvals WHERE status = 'approved'
                             AND result IS NULL AND lower(action) LIKE 'linkedin%' AND action != 'linkedin.post'
                             ORDER BY id""").fetchall():
        d = json.loads(a["details"] or "{}")
        if d.get("text"):
            out["linkedin"].append({"approval": a["id"], "title": a["action"][:100]})
    return out


def apply(conn: sqlite3.Connection) -> dict:
    from . import business

    p = plan(conn)
    sys_ctx = business.system_ctx(conn)
    owner_ctx = Ctx(actors.owner_id(conn), via="outbound_kickstart")  # changes others' tasks, like biz_rollout
    made = []
    for item in p["new_tasks"]:
        who, t = NEW_TASKS[item["key"]]
        created = tasks.create(conn, sys_ctx, {**t, "status": "next", "source": f"{SRC}:{item['key']}",
                                               "assignee": {"type": "agent", "id": _agent(conn, who)}})
        made.append(created["ref"])
    if p["kniha_warm"] and not _has_task(conn, f"{SRC}:kniha-warm") and _agent(conn, "Kniha Growth & Sales"):
        t = tasks.create(conn, sys_ctx, {
            "title": "Kniha: warm outreach – koncepty e-mailů pro majitelovy kontakty", "priority": 2,
            "topic": "kniha", "status": "next", "source": f"{SRC}:kniha-warm",
            "assignee": {"type": "agent", "id": _agent(conn, "Kniha Growth & Sales")},
            "notes": (f"### Proč\nMajitel rozhodl v {p['kniha_warm']['ticket']}: „{p['kniha_warm']['option']}“.\n\n"
                      "### Co udělat\nKontakty z jeho odpovědi (komentáře u rozhodnutí) → osobní e-mail podle "
                      "skriptů T-246, `project: \"kniha\"`, `campaign: \"kniha-warm\"`, jako koncepty, které "
                      "majitel odešle. Cena jen orientačně „kolem 2 000 Kč, nezávazně“, jinak kind=commitment."),
            "definition_of_done": "Koncept pro každý kontakt z jeho odpovědi (status drafted)."})
        made.append(t["ref"])
    for r in p["requeue"]:
        tid = tasks.parse_id(r["ref"])
        comments.log(conn, owner_ctx, tid, "E-mail teď funguje: request_outbound(\"email.send\", …) vytvoří koncept "
                                         "v Gmailu pro majitele (GitHub a Discord jdou rovnou). Pokračuj.", "comment")
        tasks.update(conn, owner_ctx, tid, {"status": "next"})
    linked = []
    for item in p["linkedin"]:
        old = approvals.get(conn, item["approval"])
        d = old["details"]
        payload = {"text": d["text"]}
        cur = conn.execute(
            """INSERT INTO approvals (task_id, requested_by, run_id, action, details, status, decided_by, decided_at,
               comment, created_at) VALUES (?, ?, NULL, 'linkedin.post', ?, 'approved', ?, ?, ?, ?)""",
            (old["task_id"], old["requested_by"],
             json.dumps({"payload": payload, "kind": "personal_channel", "text": d["text"],
                         "reason": f"schváleno jako #{old['id']} ({old['action'][:80]})",
                         **({"why": d["why"]} if d.get("why") else {})}, ensure_ascii=False),
             old["decided_by"], old["decided_at"], old.get("comment"), now_iso()))
        new = approvals.get(conn, cur.lastrowid)
        result = outbound.execute(conn, new)
        conn.execute("UPDATE approvals SET result = ? WHERE id = ?",
                     (json.dumps({"status": "migrated", "to": new["id"]}), old["id"]))
        linked.append({"from": old["id"], "to": new["id"], "status": result["status"],
                       "owner_task": result.get("owner_task")})
    hog = _agent(conn, "Head of Growth")
    if linked and hog:
        for r in conn.execute("""SELECT id FROM tasks WHERE assignee_id = ? AND status IN ('waiting', 'review')
                                 AND title LIKE 'LinkedIn%' AND archived_at IS NULL""", (hog,)).fetchall():
            comments.log(conn, owner_ctx, r["id"], "LinkedIn má teď cestu: request_outbound(\"linkedin.post\", {text, "
                                                 "image_file_id?, image_alt?}) → schválení majitele → publikace "
                                                 "(do připojení LinkedIn „připraveno k publikaci“).", "comment")
    settings_store.put(conn, sys_ctx, DONE_KEY, now_iso())
    audit.log(conn, sys_ctx, "outbound_kickstart", None, None, tasks=made, requeued=[r["ref"] for r in p["requeue"]],
              linkedin=linked, skipped=p["skipped"])
    conn.commit()
    return {**p, "created": made, "linkedin_done": linked}


def main(argv: list[str] | None = None) -> int:
    from pathlib import Path

    from .config import get_settings
    from .db import connect, migrate

    ap = argparse.ArgumentParser(prog="pos.outbound_kickstart")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db")
    a = ap.parse_args(argv)
    conn = connect(Path(a.db) if a.db else get_settings().db_path)
    try:
        migrate(conn)
        out = apply(conn) if a.apply else plan(conn)
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
