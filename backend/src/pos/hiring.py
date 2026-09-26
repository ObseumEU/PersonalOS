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

from . import actors, audit, roles, tasks, versioning
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


# ------------------------------------------------------------------ hiring without the owner (HR and leads)

HR_NAME = roles.HR
# Only the owner grants these; no hire hands them out.
OWNER_ONLY_PERMISSIONS = {"agents:create", "browser:use", "access:manage"}
# A new agent's own limits by its budget class (the Access manager changes them later).
DEFAULT_BUDGETS = {
    "low": {"usd_day": 1.0, "usd_month": 15.0, "usd_run": 0.5, "runs_day": 30},
    "normal": {"usd_day": 3.0, "usd_month": 45.0, "usd_run": 1.5, "runs_day": 60},
}
MODELS = {"claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5-5"}


def may_hire(conn: sqlite3.Connection, actor_id: int) -> bool:
    """The owner, the HR agent and any lead (someone reports to it) hire directly."""
    me = actors.get(conn, actor_id)
    if me["is_owner"] or me["name"] == HR_NAME:
        return True
    return conn.execute("SELECT 1 FROM actors WHERE reports_to = ? AND archived_at IS NULL AND id != ?",
                        (actor_id, actor_id)).fetchone() is not None


def hire(conn: sqlite3.Connection, ctx: Ctx, *, name: str, purpose: str, job_description: str = "",
         instructions: str = "", role: str | None = None, team: str | None = None, lead: str | int | None = None,
         permissions: list[str] | None = None, budget_class: str = "low", model: str | None = None,
         data_dir=None) -> dict:
    """Create a colleague end to end, without the owner, within the limits in code:
    HR or a lead asks; a lead hires only into its own part of the chart; never
    more permissions than the one who hires has (and never the owner-only ones);
    HR's headcount limits run first (over them it becomes a hire request the
    owner decides). The agent gets its worker in the agent pool at once, its
    grants and a budget by class, 7 days of probation under its lead; its
    instructions go to git through the Dev agent; #team hears about it."""
    from . import agents, chat, org, workers
    from .access import service as access
    from .access import store as access_store
    from .guard import policy
    from .hr import service as hr
    from .integrations import guard_actor

    me = actors.get(conn, ctx.actor_id)
    if not may_hire(conn, ctx.actor_id):
        raise Forbidden("only the owner, the HR agent and leads (members with reports) hire agents")
    name, purpose = (name or "").strip(), (purpose or "").strip()
    if not name or not purpose:
        raise tasks.Invalid("a new colleague needs a name and a purpose")
    if conn.execute("SELECT 1 FROM actors WHERE name = ?", (name,)).fetchone():
        raise tasks.Invalid(f"an actor called {name} already exists")
    if budget_class not in DEFAULT_BUDGETS:
        raise tasks.Invalid(f"budget_class must be one of {sorted(DEFAULT_BUDGETS)}")
    if model is not None and model not in MODELS:
        raise tasks.Invalid(f"model must be one of {sorted(MODELS)}")
    if lead in (None, ""):
        lead_row = me if not me["is_owner"] and me["name"] != HR_NAME else actors.get(
            conn, org.pm_id(conn) or actors.owner_id(conn))
    else:
        lead_row = org._member(conn, lead)
    if lead_row["archived_at"]:
        raise tasks.Invalid(f"{lead_row['name']} is archived")
    if not me["is_owner"] and me["name"] != HR_NAME and lead_row["id"] != me["id"] \
            and not org.manages(conn, me["id"], lead_row["id"]):
        raise Forbidden(f"{me['name']} hires only into its own part of the chart; {lead_row['name']} is not in it")
    perms = sorted(set(permissions if permissions is not None else agents.DEFAULT_AGENT_PERMISSIONS))
    unknown = set(perms) - set(agents.PERMISSIONS)
    if unknown:
        raise tasks.Invalid(f"unknown permissions: {sorted(unknown)}")
    if set(perms) & OWNER_ONLY_PERMISSIONS and not me["is_owner"]:
        raise Forbidden(f"only the owner grants {sorted(set(perms) & OWNER_ONLY_PERMISSIONS)}")
    if not me["is_owner"]:
        mine = agents.permissions_of(conn, me["id"])
        more = [p for p in perms if p not in mine and "*" not in mine]
        if more:
            raise Forbidden(f"no permission escalation: {me['name']} does not have {more}")
        policy.check_permission_grant(guard_actor(conn, me["id"]), sorted(mine), perms).raise_if_not_allowed()

    text = (instructions or "").strip()
    if job_description.strip():
        text = (text + "\n\n" if text else f"# {name}\n\n{purpose}\n\n") + "## Náplň práce\n\n" + job_description.strip()
    verdict = hr.admit_agent(conn, ctx, name=name, purpose=purpose, lifetime="long_lived", defer_replace=True)
    if not verdict.get("allowed"):  # over HR's limits: the owner decides after all
        req = request(conn, ctx, name=name, purpose=purpose, role=role, lead=lead_row["id"], permissions=perms,
                      budget_class="normal" if budget_class == "normal" else "low", instructions=text,
                      reason=f"HR limit: {verdict.get('decision')} ({verdict.get('reason') or verdict.get('limit')})")
        return {"created": False, "hire_request": req["id"], "decider": req["decider_name"],
                "why": verdict.get("reason") or verdict.get("decision")}

    owner = Ctx(actors.owner_id(conn), via=f"hire:{me['name']}", run_id=ctx.run_id)
    made = agents.create_agent(conn, owner, name=name, purpose=purpose, lifetime="long_lived",
                               instructions=text, permissions=perms, budget_class=budget_class,
                               data_dir=data_dir or _data_dir())
    if not made.get("created"):
        raise tasks.Invalid(f"HR stopped it: {made.get('decision')} ({made.get('reason')})")
    aid = made["agent"]["id"]
    until = (datetime.now(timezone.utc) + timedelta(days=PROBATION_DAYS)).isoformat(timespec="seconds")
    sets = {"reports_to": lead_row["id"], "probation_until": until}
    if role:
        sets["role"] = role.strip().lower().replace(" ", "_")
    if team:
        sets["team"] = team.strip().lower()
    if model:
        sets.update({"engine": "claude", "model": model})
    versioning.update(conn, owner, "actor", aid, sets, action="hired")
    if access_store.ready(conn) and not conn.execute(
            "SELECT 1 FROM access_budgets WHERE agent_id = ?", (aid,)).fetchone():
        for metric, amount in DEFAULT_BUDGETS[budget_class].items():
            access._insert_budget(conn, aid, metric, amount, owner.actor_id, "platform",
                                  f"výchozí rozpočet nového agenta ({budget_class}); mění Správce přístupů")
    now = now_iso()
    hire_row = versioning.insert(conn, ctx, ENTITY, {
        "requested_by": ctx.actor_id, "name": name, "purpose": purpose, "role": sets.get("role"),
        "lead_id": lead_row["id"], "permissions": json.dumps(perms), "budget_class": budget_class,
        "lifetime": "long_lived", "instructions": text or None, "reason": "přímé přijetí (HR / vedoucí)",
        "hr_verdict": json.dumps(verdict, ensure_ascii=False, default=str), "needs_owner": None,
        "decider_id": ctx.actor_id, "status": "approved", "decided_by": ctx.actor_id, "decided_at": now,
        "agent_id": aid, "created_by": ctx.actor_id, "created_at": now, "updated_at": now})
    commit_task = _commit_agent_files(conn, ctx, aid, name, purpose, sets, lead_row, perms, budget_class, text)
    pool = workers.reply_path(conn, actors.get(conn, aid))
    audit.log(conn, ctx, "hire_direct", "actor", aid, name=name, lead=lead_row["id"], permissions=perms,
              budget_class=budget_class, worker=pool["name"] if pool else None, hire=hire_row["id"])
    conn.commit()
    try:
        chat.post_to_team(conn, ctx.actor_id,
                          f"Nový kolega: **{name}** ({purpose[:160]}). Vede ho {lead_row['name']}, přijal "
                          f"{me['name']}; zkušební doba do {until[:10]}. Worker běží v agent poolu.")
    except Exception:  # noqa: BLE001 - the hire stands either way
        pass
    return {"created": True, "agent": {"id": aid, "name": name}, "lead": lead_row["name"],
            "worker": pool["name"] if pool else None, "probation_until": until, "hire_id": hire_row["id"],
            "files_task": commit_task}


def _commit_agent_files(conn: sqlite3.Connection, ctx: Ctx, aid: int, name: str, purpose: str, sets: dict,
                        lead_row, perms: list[str], budget_class: str, text: str) -> str | None:
    """The new agent's files in git (agents as code): a task for the Dev agent,
    like every instruction change; the deployer checks and ships it."""
    from . import agents

    dev = actors.find_by_name(conn, roles.ENGINEER)
    if dev is None or dev["archived_at"]:
        return None
    slug = agents._slug(name)
    spec = {"name": name, "purpose": purpose, "role": sets.get("role"), "team": sets.get("team"),
            "reports_to": lead_row["name"], "permissions": perms, "budget_class": budget_class,
            "lifetime": "long_lived", "runtime": "codex_worker", "worker": "pool",
            **({"engine": "claude", "model": sets["model"]} if sets.get("model") else {})}
    spec = {k: v for k, v in spec.items() if v is not None}
    t = tasks.create(conn, ctx, {
        "title": f"Soubory nového agenta {name} do gitu",
        "assignee": {"type": "agent", "id": dev["id"]}, "priority": 2, "topic": "agents",
        "notes": f"Účel: {name} byl přijat (HR / vedoucí) a už běží v agent poolu; jeho definice patří do gitu "
                 "(agents as code).\n\n"
                 f"Vytvoř `agents/{slug}/agent.json`:\n\n```json\n{json.dumps(spec, ensure_ascii=False, indent=2)}\n```\n\n"
                 f"a `agents/{slug}/INSTRUCTIONS.md`:\n\n````markdown\n{text or f'# {name}'}\n````\n\n"
                 "Commitni na agent/dev (deployer to zkontroluje a nasadí).",
        "definition_of_done": f"`agents/{slug}/` je v main (commit prošel deployerem).",
    })
    return t["ref"]
