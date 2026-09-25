"""Hiring a colleague and the probation period (REVIZE-FUNKCI 3.5).

One flow for every new agent: a member asks (`request`), HR's limits run
first and their verdict is kept with the request, then the future lead
decides (`decide`). The owner decides instead when HR sends it to the owner
(over the limit) or when the new agent would get permissions its requester
does not have. On approval the agent is created, reports to its lead and is on
probation for PROBATION_DAYS: its lead reviews all its work, and at the end a
daily job gives the lead a task to keep, extend or archive it.

People (kind human) join through invitations (wave 2); this flow is for agents.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from . import actors, audit, tasks, versioning
from .core import Ctx, Forbidden, now_iso

ENTITY = "hire"
versioning.register(ENTITY, "hire_requests")
PROBATION_DAYS = 7


def _out(conn: sqlite3.Connection, row) -> dict:
    d = dict(row)
    d["permissions"] = json.loads(d["permissions"] or "[]")
    d["hr_verdict"] = json.loads(d["hr_verdict"]) if d.get("hr_verdict") else None
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM actors")}
    d["requested_by_name"] = names.get(d["requested_by"])
    d["lead_name"] = names.get(d["lead_id"])
    d["decider_name"] = names.get(d["decider_id"])
    return d


def get(conn: sqlite3.Connection, hire_id: int) -> dict:
    row = conn.execute("SELECT * FROM hire_requests WHERE id = ?", (hire_id,)).fetchone()
    if row is None:
        raise tasks.Invalid(f"no hire request {hire_id}")
    return _out(conn, row)


def list_requests(conn: sqlite3.Connection, status: str | None = None) -> list[dict]:
    sql = "SELECT * FROM hire_requests WHERE archived_at IS NULL"
    rows = conn.execute(sql + (" AND status = ?" if status else "") + " ORDER BY id DESC LIMIT 100",
                        (status,) if status else ()).fetchall()
    return [_out(conn, r) for r in rows]


def request(conn: sqlite3.Connection, ctx: Ctx, *, name: str, purpose: str, role: str | None = None,
            lead: str | int | None = None, permissions: list[str] | None = None, budget_class: str = "normal",
            lifetime: str = "long_lived", instructions: str = "", reason: str = "") -> dict:
    """Ask for a new colleague. Returns the request with who decides it."""
    from . import agents, org
    from .guard import policy
    from .hr import service as hr
    from .integrations import guard_actor

    me = actors.get(conn, ctx.actor_id)
    if me["kind"] != "human" and not agents.has_permission(conn, ctx.actor_id, "agents:create") \
            and not agents.has_permission(conn, ctx.actor_id, "tasks:write"):
        raise Forbidden("asking for a new colleague needs agents:create or tasks:write")
    name, purpose = (name or "").strip(), (purpose or "").strip()
    if not name or not purpose:
        raise tasks.Invalid("a new colleague needs a name and a purpose")
    if conn.execute("SELECT 1 FROM actors WHERE name = ?", (name,)).fetchone():
        raise tasks.Invalid(f"an actor called {name} already exists")
    if conn.execute("SELECT 1 FROM hire_requests WHERE name = ? AND status = 'pending'", (name,)).fetchone():
        raise tasks.Invalid(f"{name} is already requested")
    if lifetime not in agents.LIFETIMES or budget_class not in agents.BUDGET_CLASSES:
        raise tasks.Invalid("bad lifetime or budget class")
    perms = sorted(set(permissions if permissions is not None else agents.DEFAULT_AGENT_PERMISSIONS))
    unknown = set(perms) - set(agents.PERMISSIONS)
    if unknown:
        raise tasks.Invalid(f"unknown permissions: {sorted(unknown)}")
    lead_row = org._member(conn, lead) if lead not in (None, "") else actors.get(conn, org.pm_id(conn) or actors.owner_id(conn))

    needs_owner = []
    # Constitution U5: never more than the one who asks has.
    grant = policy.check_permission_grant(
        guard_actor(conn, ctx.actor_id),
        ["*"] if me["is_owner"] else sorted(agents.permissions_of(conn, ctx.actor_id)), perms)
    if not grant.allowed:
        needs_owner.append("permissions beyond the requester's")
    verdict = hr.admit_agent(conn, ctx, name=name, purpose=purpose, lifetime=lifetime, defer_replace=True)
    if not verdict.get("allowed"):
        if verdict.get("decision") == "ask_owner":
            needs_owner.append("over the agent limit")
        else:
            # HR says reuse or defer: kept with the request; the decider sees it.
            needs_owner.append(f"HR: {verdict.get('decision')} ({verdict.get('reason') or verdict.get('limit')})")
    decider = actors.owner_id(conn) if needs_owner else lead_row["id"]
    if decider == ctx.actor_id and not me["is_owner"]:
        decider = actors.owner_id(conn)  # nobody approves their own request, unless they own the company
    now = now_iso()
    row = versioning.insert(conn, ctx, ENTITY, {
        "requested_by": ctx.actor_id, "name": name, "purpose": purpose,
        "role": (role or "").strip().lower().replace(" ", "_") or None, "lead_id": lead_row["id"],
        "permissions": json.dumps(perms), "budget_class": budget_class, "lifetime": lifetime,
        "instructions": instructions.strip() or None, "reason": reason.strip() or None,
        "hr_verdict": json.dumps(verdict, ensure_ascii=False, default=str),
        "needs_owner": "; ".join(needs_owner) or None, "decider_id": decider, "status": "pending",
        "created_by": ctx.actor_id, "created_at": now, "updated_at": now})
    out = _out(conn, row)
    if decider != ctx.actor_id:
        from . import chat, wake

        chat.send_dm(conn, ctx, decider,
                     f"{me['name']} asks to hire {name} ({purpose[:200]}), reporting to {lead_row['name']}. "
                     f"Decide with hire_decide #{out['id']}" + (f"; needs the owner: {out['needs_owner']}"
                                                                if out["needs_owner"] else "") + ".",
                     priority="fyi", system=True)
        if actors.get(conn, decider)["kind"] != "human":
            wake.wake(decider)
    audit.log(conn, ctx, "hire_request", ENTITY, out["id"], name=name, decider=decider)
    return out


def decide(conn: sqlite3.Connection, ctx: Ctx, hire_id: int, approve: bool, note: str = "",
           data_dir=None) -> dict:
    """The decider (the lead, or the owner) approves or rejects. Approved: the
    agent is created, reports to its lead and starts on probation."""
    from . import agents

    h = get(conn, hire_id)
    if h["status"] != "pending":
        raise tasks.Invalid(f"hire request {hire_id} is already {h['status']}")
    me = actors.get(conn, ctx.actor_id)
    if not me["is_owner"] and ctx.actor_id != h["decider_id"]:
        raise Forbidden(f"{h['decider_name']} decides this request")
    if ctx.actor_id == h["requested_by"] and not me["is_owner"]:
        raise Forbidden("nobody approves their own request")
    if not approve:
        versioning.update(conn, ctx, ENTITY, hire_id, {"status": "rejected", "decided_by": ctx.actor_id,
                                                       "decided_at": now_iso(), "decision_note": note or None},
                          action="reject")
        _tell_requester(conn, ctx, h, f"Hiring {h['name']} was rejected" + (f": {note}" if note else "."))
        return get(conn, hire_id)
    # The approval is the authorization: the agent is created on the owner's behalf,
    # within the permissions checked when it was asked for.
    owner = Ctx(actors.owner_id(conn), via="hire", run_id=ctx.run_id)
    made = agents.create_agent(conn, owner, name=h["name"], purpose=h["purpose"], lifetime=h["lifetime"],
                               instructions=h["instructions"] or "", permissions=h["permissions"],
                               budget_class=h["budget_class"], data_dir=data_dir or _data_dir())
    if not made.get("created"):
        raise tasks.Invalid(f"HR stopped it: {made.get('decision')} ({made.get('reason')})")
    aid = made["agent"]["id"]
    until = (datetime.now(timezone.utc) + timedelta(days=PROBATION_DAYS)).isoformat(timespec="seconds")
    sets = {"reports_to": h["lead_id"], "probation_until": until}
    if h["role"]:
        sets["role"] = h["role"]
    versioning.update(conn, owner, "actor", aid, sets, action="hired")
    versioning.update(conn, ctx, ENTITY, hire_id, {"status": "approved", "decided_by": ctx.actor_id,
                                                   "decided_at": now_iso(), "decision_note": note or None,
                                                   "agent_id": aid}, action="approve")
    conn.commit()
    _tell_requester(conn, ctx, h, f"{h['name']} is hired and reports to {h['lead_name']}; on probation until "
                                  f"{until[:10]} (its lead reviews its work).")
    return {**get(conn, hire_id), "api_key": made["api_key"]}


def _tell_requester(conn: sqlite3.Connection, ctx: Ctx, h: dict, text: str) -> None:
    if h["requested_by"] != ctx.actor_id:
        from . import chat

        chat.send_dm(conn, ctx, h["requested_by"], text, priority="fyi", system=True)
    audit.log(conn, ctx, "hire_decided", ENTITY, h["id"], text=text[:200])


def _data_dir():
    from .config import get_settings

    return get_settings().data_dir


def on_probation(conn: sqlite3.Connection, actor_id: int | None) -> bool:
    if not actor_id:
        return False
    row = conn.execute("SELECT probation_until FROM actors WHERE id = ?", (actor_id,)).fetchone()
    return bool(row and row["probation_until"] and row["probation_until"] > now_iso())


def probation_review(conn: sqlite3.Connection) -> dict:
    """Daily: an agent whose probation ended gets a decision task for its lead
    (keep, extend or archive), with HR's numbers for the period."""
    from .hr.platform import CorePlatform

    now = datetime.now(timezone.utc)
    rows = conn.execute("""SELECT * FROM actors WHERE probation_until IS NOT NULL AND probation_until <= ?
                           AND archived_at IS NULL""", (now.isoformat(timespec="seconds"),)).fetchall()
    ctx = Ctx(actors.owner_id(conn), via="system")
    made = []
    for a in rows:
        stats = CorePlatform(conn, ctx).agent_stats(str(a["id"]), now - timedelta(days=PROBATION_DAYS + 1), now)
        lead = a["reports_to"] or actors.owner_id(conn)
        t = tasks.create(conn, ctx, {
            "title": f"Zkušební doba {a['name']} skončila: ponechat, prodloužit, nebo archivovat?",
            "assignee": {"type": "human", "id": lead}, "priority": 2, "topic": "hr",
            "notes": f"Účel: rozhodnout o {a['name']} po zkušební době ({PROBATION_DAYS} dní).\n"
                     f"Odkud: denní kontrola zkušebních dob (pos.hiring).\n\n"
                     f"Za dobu: hotovo {stats.tasks_completed}, bez zásahu {stats.tasks_completed_unassisted}, "
                     f"vráceno {stats.tasks_returned}, chyby běhů {stats.tasks_failed}, otevřeno {stats.tasks_open}, "
                     f"tokeny {stats.tokens_used}.",
            "definition_of_done": "Rozhodnuto: ponechat (nic), prodloužit (nové probation_until) nebo archivovat; "
                                  "důvod v poznámce."})
        versioning.update(conn, ctx, "actor", a["id"], {"probation_until": None}, action="probation_ended")
        made.append(t["ref"])
    conn.commit()
    return {"ended": len(made), "tasks": made}
