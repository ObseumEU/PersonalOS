"""Grants, budgets, requests and the Access manager's decisions (see pos.access).

Who may change what (hard limits, checked in `_authorize` for every change):

- the owner: everything;
- the Access manager (an agent with `access:manage`): any capability and any
  budget, permanent or temporary, for any *other* agent, except
  * anything for itself (its own tools and budget are the owner's),
  * the company-wide cap (it may use everything below it) and the kill switch,
  * owner-only capabilities: guard/constitution, secrets and credentials, and
    `access:manage` itself;
- nobody else.

Outbound (`outbound:<action>`) may be granted, but each outbound action still
goes through the owner's approval queue (pos.outbound, request_approval).
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from .. import actors, audit
from ..core import TZ, Ctx, Forbidden, NotFound, now_iso
from . import store

AM_NAME = "Access manager"
AM_DISPLAY = "Správce přístupů"
AM_PURPOSE = ("Správce přístupů: rozhoduje o oprávněních a rozpočtech ostatních agentů, hlídá útratu, "
              "strop firmy a nečekané skoky ve spotřebě.")
PERM = "access:manage"
AM_PERMISSIONS = ["access:manage", "approvals:request", "messages:send", "tasks:claim", "tasks:read"]
# The Access manager's own small budget (owner only to change).
AM_BUDGET = {"usd_day": 3.0, "usd_month": 40.0, "usd_run": 0.5, "runs_day": 40}
AM_ENGINE, AM_MODEL = "claude", "claude-opus-5-5"

METRICS = {
    "usd_day": "USD / 24 h",
    "usd_month": "USD / month",
    "tokens_day": "tokens / 24 h",
    "tokens_month": "tokens / month",
    "usd_run": "max USD per run",
    "runs_day": "runs / 24 h",
}
# Checked before each run (usd_run is a cap the worker applies inside the run).
GATED = ("usd_day", "usd_month", "tokens_day", "tokens_month", "runs_day")
# cred:<name> is one 1Password credential (pos.credentials): the owner grants it, never the Access manager.
OWNER_ONLY_PREFIXES = ("guard", "constitution", "secrets", "credentials", "cred")
SCOPES = ("repo", "connector")

SETTINGS_KEY = "access.settings"
DEFAULT_SETTINGS = {
    "spike_factor": 5.0,          # last hour above this many times the hourly baseline: pause
    "spike_floor_usd": 1.0,       # ... and above this much in the hour (no alarm on pennies)
    "spike_floor_tokens": 300_000,
    "cap_alert_ratio": 0.8,       # ping the owner when the company cap is this full
}


class AccessError(ValueError):
    """A request or change that does not make sense (unknown capability, no reason)."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(at: datetime) -> str:
    return at.astimezone(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ vocabulary

def kind_of(capability: str) -> str:
    """permission | tool | outbound | scope | owner_only; AccessError for anything else."""
    from .. import agents, mcp_server, outbound

    cap = (capability or "").strip()
    head = cap.split(":", 1)[0]
    if cap == PERM or head in OWNER_ONLY_PREFIXES or cap.startswith("tool:access_"):
        return "owner_only"  # the grant tools, one by one too
    if cap in agents.PERMISSIONS:
        return "permission"
    if head == "tool":
        name = cap[5:]
        if name not in mcp_server.tool_names():
            raise AccessError(f"no pos tool called {name!r}")
        return "tool"
    if head == "outbound":
        action = cap[9:]
        if action != "*" and action not in outbound.ACTIONS:
            raise AccessError(f"outbound action must be * or one of {outbound.ACTIONS}")
        return "outbound"
    if head == "scope":
        parts = cap.split(":", 2)
        if len(parts) != 3 or parts[1] not in SCOPES or not parts[2].strip():
            raise AccessError("a scope is scope:repo:<owner/name> or scope:connector:<name>")
        return "scope"
    raise AccessError(f"unknown capability {cap!r}: a permission ({', '.join(sorted(agents.PERMISSIONS))}), "
                      "tool:<pos tool>, outbound:<action>, or scope:repo:<x> / scope:connector:<x>")


def _fmt(metric: str, amount) -> str:
    if amount is None:
        return "bez limitu"
    if metric.startswith("usd"):
        return f"${amount:,.2f}"
    if metric.startswith("tokens"):
        return f"{amount / 1_000_000:.1f}M tok" if amount >= 1_000_000 else f"{int(amount):,} tok".replace(",", " ")
    return f"{int(amount)} běhů"


def _duration(hours: float | None, expires_at: str | None) -> str:
    if not hours:
        return "natrvalo"
    until = datetime.fromisoformat(expires_at).astimezone(TZ).strftime("%d.%m. %H:%M") if expires_at else ""
    return f"na {hours:g} h (do {until})"


# ------------------------------------------------------------------ settings (owner only)

def settings(conn: sqlite3.Connection) -> dict:
    from .. import settings_store

    return {**DEFAULT_SETTINGS, **(settings_store.get(conn, SETTINGS_KEY) or {})}


def set_settings(conn: sqlite3.Connection, ctx: Ctx, changes: dict) -> dict:
    from .. import settings_store

    if not actors.get(conn, ctx.actor_id)["is_owner"]:
        raise Forbidden("only the owner changes the Access manager's settings")
    unknown = set(changes) - set(DEFAULT_SETTINGS)
    if unknown:
        raise AccessError(f"unknown settings: {sorted(unknown)}")
    new = {**settings(conn), **{k: float(v) for k, v in changes.items()}}
    settings_store.put(conn, ctx, SETTINGS_KEY, new)
    audit.log(conn, ctx, "access_settings", None, None, **changes)
    conn.commit()
    return new


# ------------------------------------------------------------------ the Access manager

def manager_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT id FROM actors WHERE name = ? AND archived_at IS NULL", (AM_NAME,)).fetchone()
    return row["id"] if row else None


def ensure_access_manager(conn: sqlite3.Connection) -> int:
    """Create the Access manager once (a platform agent, like the HR agent and
    the PM): its grants, its small budget, Claude at low effort. Idempotent."""
    from .. import versioning
    from ..budget import service as budget
    from ..budget import store as budget_store
    from ..hr import service as hr
    from ..hr import store as hr_store
    from ..org import REPO_AGENTS

    store.ensure_schema(conn)
    owner = actors.owner_id(conn)
    ctx = Ctx(owner, via="system")
    aid = manager_id(conn)
    if aid is None and conn.execute("SELECT 1 FROM actors WHERE name = ?", (AM_NAME,)).fetchone():
        return conn.execute("SELECT id FROM actors WHERE name = ?", (AM_NAME,)).fetchone()["id"]  # archived: owner's call
    if aid is None:
        path = REPO_AGENTS / "access-manager" / "INSTRUCTIONS.md"
        now = now_iso()
        aid = versioning.insert(conn, ctx, "actor", {
            "kind": "agent", "name": AM_NAME, "is_owner": 0, "created_at": now, "updated_at": now,
            "created_by": owner, "runtime": "codex_worker", "permissions": json.dumps(AM_PERMISSIONS),
            "instructions_path": str(path) if path.exists() else None, "engine": AM_ENGINE, "model": AM_MODEL,
            "role": "access_manager", "team": "operations", "reports_to": owner,
        })["id"]
        audit.log(conn, ctx, "create_agent", "actor", aid, name=AM_NAME, lifetime="long_lived",
                  permissions=AM_PERMISSIONS)
    hr_store.ensure_schema(conn)
    if hr_store.get_profile(conn, aid) is None:
        hr.register_agent(conn, aid, purpose=AM_PURPOSE, lifetime="long_lived", created_by=owner, system=True, ctx=ctx)
    budget_store.ensure_schema(conn)
    if str(aid) not in budget_store.agents(conn):
        budget.set_agent_class(conn, str(aid), "system")
    if not store.seeded(conn, aid):
        for cap in AM_PERMISSIONS:
            _insert_grant(conn, aid, cap, owner, "platform", "Správce přístupů: výchozí nástroje (platforma)")
        conn.execute("INSERT INTO access_agents (agent_id, seeded_at) VALUES (?, ?)", (aid, now_iso()))
        refresh_cache(conn, aid)
    if not conn.execute("SELECT 1 FROM access_budgets WHERE agent_id = ?", (aid,)).fetchone():
        for metric, amount in AM_BUDGET.items():
            _insert_budget(conn, aid, metric, amount, owner, "platform",
                           "Správce přístupů: malý vlastní rozpočet (mění jen majitel)")
    conn.commit()
    return aid


# ------------------------------------------------------------------ seeding (day one: nothing changes)

def seed(conn: sqlite3.Connection) -> dict:
    """Every agent's current permissions become grants (source seed), plus
    outbound:* where it may request approvals today, so behaviour is the same
    as before. Agents seeded earlier get permissions the platform added to
    actors.permissions since (never one that was revoked or expired)."""
    store.ensure_schema(conn)
    owner = actors.owner_id(conn)
    am = manager_id(conn)
    seeded, synced = [], []
    for row in conn.execute("SELECT * FROM actors WHERE kind != 'human' AND is_owner = 0").fetchall():
        perms = set(json.loads(row["permissions"] or "[]"))
        if not store.seeded(conn, row["id"]):
            caps = sorted(perms) + (["outbound:*"] if "approvals:request" in perms and row["id"] != am else [])
            for cap in caps:
                _insert_grant(conn, row["id"], cap, owner, "seed", "výchozí stav: oprávnění agenta před správou přístupů")
            conn.execute("INSERT INTO access_agents (agent_id, seeded_at) VALUES (?, ?)", (row["id"], now_iso()))
            seeded.append(row["name"])
        else:
            for cap in sorted(perms):
                if conn.execute("SELECT 1 FROM access_grants WHERE agent_id = ? AND capability = ?",
                                (row["id"], cap)).fetchone() is None:
                    _insert_grant(conn, row["id"], cap, owner, "platform", "přidáno platformou (kód PersonalOS)")
                    synced.append(f"{row['name']}: {cap}")
        refresh_cache(conn, row["id"])
    if seeded or synced:
        audit.log(conn, Ctx(owner, via="system"), "access_seed", None, None, seeded=seeded or None,
                  synced=synced or None)
    conn.commit()
    return {"seeded": seeded, "synced": synced}


def seed_agent(conn: sqlite3.Connection, agent_id: int, granted_by: int) -> None:
    """A newly created agent: its permissions become grants right away."""
    if not store.ready(conn):
        return
    row = actors.get(conn, agent_id)
    if store.seeded(conn, agent_id):
        return
    perms = sorted(set(json.loads(row["permissions"] or "[]")))
    who = actors.get(conn, granted_by)
    for cap in perms + (["outbound:*"] if "approvals:request" in perms else []):
        _insert_grant(conn, agent_id, cap, granted_by, "owner" if who["is_owner"] else "seed",
                      f"založení agenta ({who['name']})")
    conn.execute("INSERT INTO access_agents (agent_id, seeded_at) VALUES (?, ?)", (agent_id, now_iso()))


def sync_owner_permissions(conn: sqlite3.Connection, ctx: Ctx, agent_id: int, permissions: list[str]) -> None:
    """The owner ticked permissions on the agent page (agents.set_permissions):
    the permission grants follow, each change audited like any grant."""
    if not store.ready(conn):
        return
    if not store.seeded(conn, agent_id):
        seed_agent(conn, agent_id, ctx.actor_id)
        return
    now = now_iso()
    active = {r["capability"]: r["id"] for r in conn.execute(
        f"SELECT id, capability FROM access_grants WHERE agent_id = ? AND kind = 'permission' AND {store.ACTIVE}",
        (agent_id, now))}
    gone = [cap for cap in active if cap not in permissions]
    for cap in gone:
        _end(conn, "access_grants", [active[cap]], ctx.actor_id, "revoked", "owner changed the permissions")
    added = [cap for cap in permissions if cap not in active]
    for cap in added:
        _insert_grant(conn, agent_id, cap, ctx.actor_id, "owner", "owner changed the permissions")
    if gone or added:
        audit.log(conn, ctx, "access_owner_permissions", "actor", agent_id, added=added or None, removed=gone or None)
    refresh_cache(conn, agent_id)


# ------------------------------------------------------------------ reading grants

def effective(conn: sqlite3.Connection, agent_id: int, now: datetime | None = None) -> set[str] | None:
    """The agent's active capabilities, or None when it is not managed here yet."""
    if not store.ready(conn) or not store.seeded(conn, agent_id):
        return None
    return {r[0] for r in conn.execute(
        f"SELECT capability FROM access_grants WHERE agent_id = ? AND {store.ACTIVE}",
        (agent_id, _iso(now or utcnow())))}


def refresh_cache(conn: sqlite3.Connection, agent_id: int) -> None:
    """actors.permissions mirrors the active permission grants (older readers, the UI)."""
    from .. import agents

    perms = sorted({r["capability"] for r in conn.execute(
        f"SELECT capability, kind FROM access_grants WHERE agent_id = ? AND {store.ACTIVE}", (agent_id, now_iso()))
        if r["kind"] == "permission" or r["capability"] in agents.PERMISSIONS})
    conn.execute("UPDATE actors SET permissions = ? WHERE id = ?", (json.dumps(perms), agent_id))


def grants(conn: sqlite3.Connection, agent_id: int, include_ended: bool = False, limit: int = 100) -> list[dict]:
    store.ensure_schema(conn)
    now = now_iso()
    where = "" if include_ended else f"AND {store.ACTIVE}"
    rows = conn.execute(
        f"""SELECT g.*, a.name AS granted_by_name FROM access_grants g LEFT JOIN actors a ON a.id = g.granted_by
            WHERE g.agent_id = ? {where} ORDER BY g.ended_at IS NOT NULL, g.capability, g.id DESC LIMIT ?""",
        (agent_id, *(() if include_ended else (now,)), limit)).fetchall()
    return [{**dict(r), "active": r["ended_at"] is None and (r["expires_at"] is None or r["expires_at"] > now)}
            for r in rows]


def require_outbound(conn: sqlite3.Connection, ctx: Ctx, action: str) -> None:
    """request_outbound needs an outbound grant (the approval queue still decides each action)."""
    if actors.get(conn, ctx.actor_id)["kind"] == "human":
        return
    have = effective(conn, ctx.actor_id)
    if have is None or "outbound:*" in have or f"outbound:{action}" in have:
        return
    raise Forbidden(f"no outbound:{action} grant; ask the Access manager with request_access")


# ------------------------------------------------------------------ budgets and usage

def _active_budget(conn: sqlite3.Connection, agent_id: int | None, metric: str,
                   now: datetime | None = None) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT * FROM access_budgets WHERE agent_id IS ? AND metric = ? AND {store.ACTIVE} ORDER BY id DESC LIMIT 1",
        (agent_id, metric, _iso(now or utcnow()))).fetchone()


def limit(conn: sqlite3.Connection, agent_id: int | None, metric: str, now: datetime | None = None) -> float | None:
    if not store.ready(conn):
        return None
    row = _active_budget(conn, agent_id, metric, now)
    return row["amount"] if row else None


def budgets(conn: sqlite3.Connection, agent_id: int | None, now: datetime | None = None) -> dict:
    """Each metric: the active limit (with who set it and until when) and what is used."""
    store.ensure_schema(conn)
    now = now or utcnow()
    out = {}
    for metric, label in METRICS.items():
        row = _active_budget(conn, agent_id, metric, now)
        out[metric] = {
            "label": label, "limit": row["amount"] if row else None,
            "used": None if metric == "usd_run" else used(conn, agent_id, metric, now),
            "expires_at": row["expires_at"] if row else None, "reason": row["reason"] if row else None,
            "set_by": _name(conn, row["granted_by"]) if row else None, "id": row["id"] if row else None,
        }
    return out


def _name(conn: sqlite3.Connection, actor_id: int | None) -> str | None:
    if actor_id is None:
        return None
    row = conn.execute("SELECT name FROM actors WHERE id = ?", (actor_id,)).fetchone()
    return row["name"] if row else None


def _month_start(now: datetime) -> datetime:
    local = now.astimezone(TZ)
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def spend(conn: sqlite3.Connection, agent_id: int | None, start: datetime, end: datetime) -> dict:
    """USD (Claude, from engine_usage), tokens (Claude + Codex) and runs in [start, end);
    agent_id None: every agent together."""
    s, e = _iso(start), _iso(end)
    who = "" if agent_id is None else " AND actor_id = ?"
    args = (s, e) if agent_id is None else (s, e, agent_id)
    claude = conn.execute(
        f"""SELECT COALESCE(SUM(cost_usd), 0) AS usd, COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens
            FROM engine_usage WHERE at >= ? AND at < ?{who}""", args).fetchone()
    codex = 0
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'budget_runs'").fetchone():
        codex = conn.execute(
            "SELECT COALESCE(SUM(billable_tokens), 0) FROM budget_runs WHERE at >= ? AND at < ? AND "
            + ("agent_id IS NOT NULL" if agent_id is None else "agent_id = ?"),
            (s, e) if agent_id is None else (s, e, str(agent_id))).fetchone()[0]
    runs = conn.execute(
        f"SELECT COUNT(*) FROM runs WHERE started_at >= ? AND started_at < ? AND status != 'blocked'{who}",
        args).fetchone()[0]
    return {"usd": round(claude["usd"], 4), "tokens": int(claude["tokens"] + codex), "runs": runs}


def used(conn: sqlite3.Connection, agent_id: int | None, metric: str, now: datetime | None = None) -> float:
    now = now or utcnow()
    start = _month_start(now) if metric.endswith("month") else now - timedelta(days=1)
    s = spend(conn, agent_id, start, now + timedelta(seconds=1))
    return s["usd"] if metric.startswith("usd") else s["tokens"] if metric.startswith("tokens") else s["runs"]


# ------------------------------------------------------------------ the run gate

def budget_gate(conn: sqlite3.Connection, req) -> None:
    """runner.before_run hook: the company cap, then the agent's own limits.
    A refusal becomes a request for the Access manager (once per limit)."""
    from ..runner import RunBlocked

    if not store.ready(conn):
        return
    actor = actors.get(conn, req.actor_id)
    if actor["kind"] == "human":
        return
    now = utcnow()
    for metric in GATED:
        cap = limit(conn, None, metric, now)
        if cap is None:
            continue
        # the run being started already has its row: it does not count against runs_day yet
        u = used(conn, None, metric, now) - (1 if metric == "runs_day" else 0)
        if u >= cap:
            _cap_alert(conn, metric, u, cap, now, full=True)
            raise RunBlocked(f"company cap {METRICS[metric]} reached ({_fmt(metric, u)} of {_fmt(metric, cap)}); "
                             "only the owner raises it")
    for metric in GATED:
        lim = limit(conn, req.actor_id, metric, now)
        if lim is None:
            continue
        u = used(conn, req.actor_id, metric, now) - (1 if metric == "runs_day" else 0)
        if u >= lim:
            ref = limit_hit(conn, req.actor_id, metric, u, lim, task_id=req.task_id)
            raise RunBlocked(f"budget {METRICS[metric]} reached ({_fmt(metric, u)} of {_fmt(metric, lim)}); "
                             f"the Access manager has it as request #{ref}")


def run_cap_usd(conn: sqlite3.Connection, agent_id: int) -> float | None:
    """The agent's max USD per run (the worker passes it to the engine)."""
    return limit(conn, agent_id, "usd_run")


def signals(conn: sqlite3.Connection, agent_id: int, now: datetime | None = None) -> dict:
    """What a runaway looks like: the same task run again and again, the same tool called over and over."""
    now = now or utcnow()
    since = _iso(now - timedelta(days=1))
    per_task = conn.execute(
        """SELECT task_id, COUNT(*) AS n FROM runs WHERE actor_id = ? AND started_at >= ? AND task_id IS NOT NULL
           AND status != 'blocked' GROUP BY task_id ORDER BY n DESC LIMIT 3""", (agent_id, since)).fetchall()
    tools = conn.execute(
        """SELECT action, COUNT(*) AS n FROM audit_log WHERE actor_id = ? AND at >= ? AND action LIKE 'mcp:%'
           AND action NOT LIKE '%:refused' GROUP BY action ORDER BY n DESC LIMIT 3""", (agent_id, since)).fetchall()
    from ..tasks import display_id

    return {"runs_per_task_24h": [{"task": display_id(r["task_id"]), "runs": r["n"]} for r in per_task],
            "top_tools_24h": [{"tool": r["action"][4:], "calls": r["n"]} for r in tools],
            "looks_like_loop": bool((per_task and per_task[0]["n"] >= 5) or (tools and tools[0]["n"] >= 200))}


def limit_hit(conn: sqlite3.Connection, agent_id: int, metric: str, used_: float, lim: float,
              task_id: int | None = None) -> int:
    """A run was refused for the agent's own limit: one open request per limit."""
    store.ensure_schema(conn)
    row = conn.execute(
        """SELECT id FROM access_requests WHERE agent_id = ? AND metric = ? AND trigger = 'limit_hit'
           AND status = 'pending'""", (agent_id, metric)).fetchone()
    if row:
        return row["id"]
    name = actors.get(conn, agent_id)["name"]
    rid = _insert_request(conn, agent_id=agent_id, requested_by=None, trigger="limit_hit", what="budget",
                          metric=metric, amount=None, hours=None, task_id=task_id,
                          why=f"{name} narazil na limit {METRICS[metric]}: {_fmt(metric, used_)} z {_fmt(metric, lim)}.",
                          detail={"used": used_, "limit": lim, "signals": signals(conn, agent_id)})
    audit.log(conn, Ctx(agent_id, via="system"), "access_limit_hit", "actor", agent_id, metric=metric,
              used=used_, limit=lim, request=rid)
    _wake_manager(conn, f"#{rid}: {name} narazil na limit {METRICS[metric]}")
    return rid


# ------------------------------------------------------------------ requests (every agent)

def _insert_request(conn: sqlite3.Connection, **f) -> int:
    detail = f.pop("detail", {}) or {}
    cur = conn.execute(
        """INSERT INTO access_requests (agent_id, requested_by, trigger, what, capability, metric, amount, hours,
               why, task_id, blocking, needs_owner, detail, created_at)
           VALUES (:agent_id, :requested_by, :trigger, :what, :capability, :metric, :amount, :hours, :why,
                   :task_id, :blocking, :needs_owner, :detail, :created_at)""",
        {"capability": None, "metric": None, "amount": None, "hours": None, "task_id": None, "blocking": 0,
         "needs_owner": 0, **f, "detail": json.dumps(detail, ensure_ascii=False, default=str),
         "created_at": now_iso()})
    return cur.lastrowid


def request_access(conn: sqlite3.Connection, ctx: Ctx, *, what: str, why: str, capability: str | None = None,
                   metric: str | None = None, amount: float | None = None, hours: float | None = None,
                   task_id: int | None = None, blocking: bool = False) -> dict:
    """An agent asks for a capability or a budget. The Access manager is woken;
    the decision comes to the agent's inbox; a blocking ask parks its task in waiting."""
    from .. import comments, tasks, versioning

    store.ensure_schema(conn)
    me = actors.get(conn, ctx.actor_id)
    if me["kind"] == "human":
        raise AccessError("people change access in the web app (the owner) or ask the owner")
    why = (why or "").strip()
    if not why:
        raise AccessError("say why you need it (one or two sentences, with the task)")
    if what == "capability":
        kind = kind_of(capability or "")
        if (capability or "").startswith("cred:"):
            from ..credentials import service as credentials

            credentials.validate_request(conn, capability)
        metric, amount = None, None
    elif what == "budget":
        if metric not in METRICS:
            raise AccessError(f"metric must be one of {sorted(METRICS)}")
        if amount is not None and amount <= 0:
            raise AccessError("amount must be positive (or empty for 'no limit')")
        kind, capability = None, None
    else:
        raise AccessError("what must be 'capability' or 'budget'")
    if hours is not None and not (0 < hours <= 24 * 366):
        raise AccessError("hours must be between 0 and a year (empty = permanent)")
    source = tasks.get(conn, ctx, task_id) if task_id else None

    dup = conn.execute(
        """SELECT * FROM access_requests WHERE agent_id = ? AND what = ? AND capability IS ? AND metric IS ?
           AND status IN ('pending', 'escalated')""", (ctx.actor_id, what, capability, metric)).fetchone()
    if dup:
        return {"request_id": dup["id"], "status": dup["status"], "deduped": True,
                "note": "You already asked for this; the decision comes to your inbox."}
    needs_owner = me["name"] == AM_NAME or kind == "owner_only"
    rid = _insert_request(conn, agent_id=ctx.actor_id, requested_by=ctx.actor_id, trigger="request", what=what,
                          capability=capability, metric=metric, amount=amount, hours=hours, why=why[:2000],
                          task_id=task_id, blocking=int(bool(blocking and source)), needs_owner=int(needs_owner))
    audit.log(conn, ctx, "access_request", "actor", ctx.actor_id, request=rid, what=what, capability=capability,
              metric=metric, amount=amount, hours=hours, task=task_id)
    label = capability if what == "capability" else f"{METRICS[metric]} {_fmt(metric, amount)}"
    if what == "capability" and capability.startswith("cred:"):
        from ..credentials import service as credentials

        # A credential: the owner's ask_owner ticket (with the reason), approved with one click.
        credentials.on_access_request(conn, ctx, rid, capability, why, task_id if source else None, hours)
    elif needs_owner:
        _dm_owner(conn, f"Žádost o přístup #{rid} od {me['name']}: `{label}`. Tohle smí rozhodnout jen majitel "
                        f"(stránka agenta → Přístupy). Důvod: {why[:300]}")
    else:
        _wake_manager(conn, f"#{rid}: {me['name']} žádá `{label}`")
    if source and blocking:
        comments.log(conn, ctx, source["id"], f"Asked for access (request #{rid}): `{label}`", "system")
        if source["status"] not in ("done", "waiting"):
            versioning.update(conn, ctx, tasks.ENTITY, source["id"], {
                "status": "waiting", "progress_note": f"Waiting for access request #{rid} ({label})"[:500]},
                action="wait")
    conn.commit()
    return {"request_id": rid, "status": "pending", "deduped": False, "needs_owner": needs_owner,
            "note": ("Only the owner decides this one; they were told." if needs_owner else
                     "The Access manager decides; the answer comes to your inbox")
            + (". Your task waits until then: finish this run with a short summary." if source and blocking else ".")}


def requests(conn: sqlite3.Connection, status: str | None = "pending", agent_id: int | None = None,
             limit_: int = 50) -> list[dict]:
    store.ensure_schema(conn)
    where, args = [], []
    if status == "open":
        where.append("r.status IN ('pending', 'escalated')")
    elif status:
        where.append("r.status = ?")
        args.append(status)
    if agent_id is not None:
        where.append("r.agent_id = ?")
        args.append(agent_id)
    rows = conn.execute(
        f"""SELECT r.*, a.name AS agent_name, d.name AS decided_by_name FROM access_requests r
            JOIN actors a ON a.id = r.agent_id LEFT JOIN actors d ON d.id = r.decided_by
            {'WHERE ' + ' AND '.join(where) if where else ''} ORDER BY r.id DESC LIMIT ?""",
        (*args, limit_)).fetchall()
    from ..tasks import display_id

    return [{**dict(r), "detail": json.loads(r["detail"] or "{}"), "needs_owner": bool(r["needs_owner"]),
             "task_ref": display_id(r["task_id"]) if r["task_id"] else None} for r in rows]


# ------------------------------------------------------------------ decisions (owner, Access manager)

def _authorize(conn: sqlite3.Connection, ctx: Ctx, agent_id: int | None, *, capability: str | None = None,
               metric: str | None = None, amount: float | None = None) -> str:
    """The hard limits. Returns 'owner' or 'access_manager', else raises Forbidden."""
    from .. import agents

    me = actors.get(conn, ctx.actor_id)
    if me["is_owner"]:
        if agent_id is not None and actors.get(conn, agent_id)["kind"] == "human":
            raise AccessError("grants and budgets are for agents")
        return "owner"
    if me["kind"] == "human" or not agents.has_permission(conn, ctx.actor_id, PERM):
        raise Forbidden("only the owner and the Access manager change access")
    if agent_id is None:
        raise Forbidden("the company-wide cap is the owner's; you may use everything below it")
    if agent_id == ctx.actor_id:
        raise Forbidden("you never grant or raise anything for yourself: your own tools and budget are the owner's")
    target = actors.get(conn, agent_id)
    if target["kind"] == "human" or target["is_owner"]:
        raise AccessError("grants and budgets are for agents")
    if capability is not None and kind_of(capability) == "owner_only":
        raise Forbidden(f"{capability} is owner only (guard and constitution, secrets and credentials, the grant "
                        "tools): recommend it to the owner with ask_owner")
    if metric is not None and amount is not None:
        cap_metric = "usd_day" if metric == "usd_run" else metric
        cap = limit(conn, None, cap_metric)
        if cap is not None and amount > cap:
            raise Forbidden(f"{_fmt(metric, amount)} is above the company cap {METRICS[cap_metric]} "
                            f"{_fmt(cap_metric, cap)} (the owner's)")
    return "access_manager"


def _insert_grant(conn: sqlite3.Connection, agent_id: int, capability: str, granted_by: int, source: str,
                  reason: str, *, hours: float | None = None, request_id: int | None = None) -> int:
    expires = _iso(utcnow() + timedelta(hours=hours)) if hours else None
    return conn.execute(
        """INSERT INTO access_grants (agent_id, capability, kind, granted_by, source, reason, request_id,
               created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (agent_id, capability, _kind_or_legacy(capability), granted_by, source, reason, request_id, now_iso(),
         expires)).lastrowid


def _kind_or_legacy(capability: str) -> str:
    try:
        return kind_of(capability)
    except AccessError:
        return "permission"  # a legacy permission name kept in actors.permissions


def _insert_budget(conn: sqlite3.Connection, agent_id: int | None, metric: str, amount: float | None,
                   granted_by: int, source: str, reason: str, *, hours: float | None = None,
                   request_id: int | None = None) -> int:
    expires = _iso(utcnow() + timedelta(hours=hours)) if hours else None
    return conn.execute(
        """INSERT INTO access_budgets (agent_id, metric, amount, granted_by, source, reason, request_id, created_at,
               expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (agent_id, metric, amount, granted_by, source, reason, request_id, now_iso(), expires)).lastrowid


def _end(conn: sqlite3.Connection, table: str, ids: list[int], by: int | None, kind: str, reason: str) -> None:
    for i in ids:
        conn.execute(f"UPDATE {table} SET ended_at = ?, ended_by = ?, end_kind = ?, end_reason = ? WHERE id = ?",
                     (now_iso(), by, kind, reason[:500], i))


def _reason(reason: str) -> str:
    reason = (reason or "").strip()
    if not reason:
        raise AccessError("every decision needs a reason")
    return reason[:1000]


def grant(conn: sqlite3.Connection, ctx: Ctx, agent_id: int, capability: str, reason: str,
          hours: float | None = None, request_id: int | None = None) -> dict:
    store.ensure_schema(conn)
    capability = (capability or "").strip()
    reason = _reason(reason)
    kind = kind_of(capability)
    role = _authorize(conn, ctx, agent_id, capability=capability)
    if hours is not None and not (0 < hours <= 24 * 366):
        raise AccessError("hours must be between 0 and a year (empty = permanent)")
    _ensure_seeded(conn, agent_id)
    now = now_iso()
    active = conn.execute(f"SELECT * FROM access_grants WHERE agent_id = ? AND capability = ? AND {store.ACTIVE}",
                          (agent_id, capability, now)).fetchall()
    if hours and any(r["expires_at"] is None for r in active):
        return {"grant_id": next(r["id"] for r in active if r["expires_at"] is None), "unchanged": True,
                "note": f"{capability} is already granted permanently"}
    _end(conn, "access_grants", [r["id"] for r in active], ctx.actor_id, "replaced", f"replaced: {reason}")
    gid = _insert_grant(conn, agent_id, capability, ctx.actor_id, role, reason, hours=hours, request_id=request_id)
    refresh_cache(conn, agent_id)
    row = conn.execute("SELECT * FROM access_grants WHERE id = ?", (gid,)).fetchone()
    name = actors.get(conn, agent_id)["name"]
    audit.log(conn, ctx, "access_grant", "actor", agent_id, grant=gid, capability=capability, kind=kind,
              hours=hours, expires_at=row["expires_at"], reason=reason, request=request_id)
    note = " Každou odchozí akci dál schvaluje majitel." if kind == "outbound" else ""
    _post_team(conn, ctx, f"Přístupy: {name} dostal `{capability}` {_duration(hours, row['expires_at'])}.{note} "
                          f"Důvod: {reason}")
    conn.commit()
    return {"grant_id": gid, "agent": name, "capability": capability, "expires_at": row["expires_at"]}


def revoke(conn: sqlite3.Connection, ctx: Ctx, agent_id: int, capability: str, reason: str) -> dict:
    store.ensure_schema(conn)
    reason = _reason(reason)
    _authorize(conn, ctx, agent_id, capability=capability)
    _ensure_seeded(conn, agent_id)
    ids = [r["id"] for r in conn.execute(
        f"SELECT id FROM access_grants WHERE agent_id = ? AND capability = ? AND {store.ACTIVE}",
        (agent_id, capability, now_iso()))]
    if not ids:
        raise NotFound(f"{actors.get(conn, agent_id)['name']} has no active {capability}")
    _end(conn, "access_grants", ids, ctx.actor_id, "revoked", reason)
    refresh_cache(conn, agent_id)
    name = actors.get(conn, agent_id)["name"]
    audit.log(conn, ctx, "access_revoke", "actor", agent_id, capability=capability, grants=ids, reason=reason)
    _post_team(conn, ctx, f"Přístupy: {name} už nemá `{capability}`. Důvod: {reason}")
    _tell(conn, ctx, agent_id, f"{actors.get(conn, ctx.actor_id)['name']} ti odebral `{capability}`: {reason}")
    conn.commit()
    return {"revoked": ids, "agent": name, "capability": capability}


def revoke_grant(conn: sqlite3.Connection, ctx: Ctx, grant_id: int, reason: str) -> dict:
    row = conn.execute("SELECT * FROM access_grants WHERE id = ?", (grant_id,)).fetchone()
    if row is None:
        raise NotFound(f"grant {grant_id}")
    return revoke(conn, ctx, row["agent_id"], row["capability"], reason)


def set_budget(conn: sqlite3.Connection, ctx: Ctx, agent_id: int | None, metric: str, amount: float | None,
               reason: str, hours: float | None = None, request_id: int | None = None) -> dict:
    """A new limit (None = no limit). A temporary one sits on top of the
    permanent one and reverts to it when it expires."""
    store.ensure_schema(conn)
    reason = _reason(reason)
    if metric not in METRICS:
        raise AccessError(f"metric must be one of {sorted(METRICS)}")
    if amount is not None and amount < 0:
        raise AccessError("amount must be 0 or more (empty = no limit)")
    if hours is not None and not (0 < hours <= 24 * 366):
        raise AccessError("hours must be between 0 and a year (empty = permanent)")
    role = _authorize(conn, ctx, agent_id, metric=metric, amount=amount)
    now = now_iso()
    before = limit(conn, agent_id, metric)
    same_kind = "expires_at IS NOT NULL" if hours else "expires_at IS NULL"
    old = [r["id"] for r in conn.execute(
        f"SELECT id FROM access_budgets WHERE agent_id IS ? AND metric = ? AND {same_kind} AND {store.ACTIVE}",
        (agent_id, metric, now))]
    _end(conn, "access_budgets", old, ctx.actor_id, "replaced", f"replaced: {reason}")
    bid = _insert_budget(conn, agent_id, metric, amount, ctx.actor_id, role, reason, hours=hours,
                         request_id=request_id)
    row = conn.execute("SELECT * FROM access_budgets WHERE id = ?", (bid,)).fetchone()
    who = "celá firma" if agent_id is None else actors.get(conn, agent_id)["name"]
    audit.log(conn, ctx, "access_budget", "actor" if agent_id else None, agent_id, budget=bid, metric=metric,
              amount=amount, before=before, hours=hours, expires_at=row["expires_at"], reason=reason,
              request=request_id)
    _post_team(conn, ctx, f"Přístupy: {who} má limit {METRICS[metric]} {_fmt(metric, amount)} "
                          f"(dřív {_fmt(metric, before)}) {_duration(hours, row['expires_at'])}. Důvod: {reason}")
    conn.commit()
    return {"budget_id": bid, "agent": who, "metric": metric, "amount": amount, "before": before,
            "expires_at": row["expires_at"]}


def decide(conn: sqlite3.Connection, ctx: Ctx, request_id: int, decision: str, note: str,
           amount: float | None = None, hours: float | None = None) -> dict:
    """grant (what was asked, or an adjusted amount/duration), deny, or escalate
    (owner-only items: the Access manager recommends them to the owner with ask_owner)."""
    from .. import agents

    store.ensure_schema(conn)
    note = _reason(note)
    r = conn.execute("SELECT * FROM access_requests WHERE id = ?", (request_id,)).fetchone()
    if r is None:
        raise NotFound(f"access request {request_id}")
    if r["status"] not in ("pending", "escalated"):
        raise AccessError(f"request #{request_id} is already {r['status']}")
    if decision not in ("grant", "deny", "escalate"):
        raise AccessError("decision must be grant, deny or escalate")
    me = actors.get(conn, ctx.actor_id)
    if not (me["is_owner"] or (me["kind"] != "human" and agents.has_permission(conn, ctx.actor_id, PERM))):
        raise Forbidden("only the owner and the Access manager decide access requests")
    if r["agent_id"] == ctx.actor_id and not me["is_owner"]:
        raise Forbidden("your own requests are the owner's to decide")
    out: dict = {"request_id": request_id, "decision": decision}
    sets = {"status": {"grant": "granted", "deny": "denied", "escalate": "escalated"}[decision],
            "decided_by": ctx.actor_id, "decided_at": now_iso(), "decision_note": note}
    if decision == "grant":
        if r["what"] == "capability":
            g = grant(conn, ctx, r["agent_id"], r["capability"], note, hours if hours is not None else r["hours"],
                      request_id)
            sets["grant_id"] = g["grant_id"]
            out["grant"] = g
        elif r["what"] == "budget":
            b = set_budget(conn, ctx, r["agent_id"], r["metric"], amount if amount is not None else r["amount"], note,
                           hours if hours is not None else r["hours"], request_id)
            sets["budget_id"] = b["budget_id"]
            out["budget"] = b
        else:  # a spike review: grant = let the agent work again
            out["resumed"] = resume_agent(conn, ctx, r["agent_id"], note, _from_request=True)
    elif decision == "escalate":
        if me["is_owner"]:
            raise AccessError("the owner decides: grant or deny")
    conn.execute(f"UPDATE access_requests SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
                 (*sets.values(), request_id))
    audit.log(conn, ctx, f"access_{decision}", "actor", r["agent_id"], request=request_id, note=note)
    name = actors.get(conn, r["agent_id"])["name"]
    label = r["capability"] or (METRICS.get(r["metric"] or "", "") + (f" {_fmt(r['metric'], r['amount'])}" if r["metric"] else "")) or "kontrola"
    if decision == "deny":
        _post_team(conn, ctx, f"Přístupy: žádost #{request_id} od {name} (`{label}`) zamítnuta. Důvod: {note}")
    if decision == "escalate":
        _post_team(conn, ctx, f"Přístupy: žádost #{request_id} od {name} (`{label}`) patří majiteli. {note}")
    word = {"grant": "schválil", "deny": "zamítl", "escalate": "předal majiteli"}[decision]
    _tell(conn, ctx, r["agent_id"], f"{me['name']} {word} tvou žádost o přístup #{request_id} (`{label}`): {note}"
          + (" Pokračuj." if decision == "grant" else ""))
    if decision != "escalate":
        _resume_task(conn, ctx, r, f"Access request #{request_id} {sets['status']}")
    conn.commit()
    return out


# ------------------------------------------------------------------ pausing and resuming (spikes)

def pause_for_spike(conn: sqlite3.Connection, agent_id: int, detail: dict) -> int:
    """Pause first, then review: the agent stops, its runs stop, the Access
    manager gets a review request and the owner an immediate ping."""
    from .. import runner, versioning

    am = manager_id(conn)
    ctx = Ctx(am or actors.owner_id(conn), via="system")
    name = actors.get(conn, agent_id)["name"]
    versioning.update(conn, ctx, "actor", agent_id, {"paused_at": now_iso()}, action="access_pause")
    stopped = runner.cancel_all(conn, f"paused by the Access manager: spend spike", actor_id=agent_id)
    why = (f"{name} utratil za poslední hodinu {detail['last_hour_usd']:.2f} USD / {detail['last_hour_tokens']:,} tok "
           f"(běžně {detail['baseline_usd']:.2f} USD / {int(detail['baseline_tokens']):,} tok za hodinu).").replace(",", " ")
    rid = _insert_request(conn, agent_id=agent_id, requested_by=None, trigger="spike", what="review", why=why,
                          needs_owner=int(agent_id == am), detail={**detail, "runs_stopped": stopped,
                                                                   "signals": signals(conn, agent_id)})
    audit.log(conn, ctx, "access_pause", "actor", agent_id, request=rid, runs=stopped or None, **detail)
    _post_team(conn, ctx, f"Přístupy: pozastavil jsem {name} kvůli skoku ve spotřebě. {why} Prověřím to (#{rid}).")
    _dm_owner(conn, f"Pozastavil jsem {name}: {why} Prověřuju to (žádost #{rid}); pokud to byl omyl, "
                    "pustím ho zpátky sám.")
    if agent_id != am:
        _wake_manager(conn, f"#{rid}: {name} pozastaven kvůli skoku ve spotřebě, prověř to")
    return rid


def resume_agent(conn: sqlite3.Connection, ctx: Ctx, agent_id: int, reason: str, _from_request: bool = False) -> bool:
    """Let an agent the Access manager paused (a spike) work again."""
    from .. import versioning

    reason = _reason(reason)
    _authorize(conn, ctx, agent_id)
    row = actors.get(conn, agent_id)
    if not row["paused_at"]:
        return False
    last = conn.execute("SELECT action FROM history WHERE entity = 'actor' AND entity_id = ? ORDER BY id DESC LIMIT 1",
                        (agent_id,)).fetchone()
    if not actors.get(conn, ctx.actor_id)["is_owner"] and (last is None or last["action"] != "access_pause"):
        raise Forbidden(f"{row['name']} was paused by a person or its lead, not for spend: they resume it")
    versioning.update(conn, ctx, "actor", agent_id, {"paused_at": None}, action="access_resume")
    audit.log(conn, ctx, "access_resume", "actor", agent_id, reason=reason)
    if not _from_request:
        conn.execute("""UPDATE access_requests SET status = 'granted', decided_by = ?, decided_at = ?,
                        decision_note = ? WHERE agent_id = ? AND trigger = 'spike' AND status = 'pending'""",
                     (ctx.actor_id, now_iso(), reason, agent_id))
    _post_team(conn, ctx, f"Přístupy: {row['name']} zase běží. {reason}")
    conn.commit()
    return True


# ------------------------------------------------------------------ watchers (scheduler jobs)

def expire(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """Temporary grants and budgets past their time end (they already stopped
    counting at expires_at); the agent and #team hear of it."""
    if not store.ready(conn):
        return {"expired": 0}
    now_s = _iso(now or utcnow())
    ctx = Ctx(manager_id(conn) or actors.owner_id(conn), via="system")
    out = []
    for g in conn.execute("SELECT * FROM access_grants WHERE ended_at IS NULL AND expires_at <= ?", (now_s,)).fetchall():
        conn.execute("UPDATE access_grants SET ended_at = ?, end_kind = 'expired', end_reason = 'time is up' WHERE id = ?",
                     (now_s, g["id"]))
        refresh_cache(conn, g["agent_id"])
        name = actors.get(conn, g["agent_id"])["name"]
        audit.log(conn, ctx, "access_expire", "actor", g["agent_id"], grant=g["id"], capability=g["capability"])
        _tell(conn, ctx, g["agent_id"], f"Dočasný přístup `{g['capability']}` vypršel.")
        out.append(f"{name}: {g['capability']}")
    for b in conn.execute("SELECT * FROM access_budgets WHERE ended_at IS NULL AND expires_at <= ?", (now_s,)).fetchall():
        conn.execute("UPDATE access_budgets SET ended_at = ?, end_kind = 'expired', end_reason = 'time is up' WHERE id = ?",
                     (now_s, b["id"]))
        back = limit(conn, b["agent_id"], b["metric"], now)
        audit.log(conn, ctx, "access_expire", "actor" if b["agent_id"] else None, b["agent_id"], budget=b["id"],
                  metric=b["metric"], back_to=back)
        who = "firma" if b["agent_id"] is None else actors.get(conn, b["agent_id"])["name"]
        out.append(f"{who}: {METRICS[b['metric']]} zpět na {_fmt(b['metric'], back)}")
    if out:
        _post_team(conn, ctx, "Přístupy: vypršelo dočasné — " + "; ".join(out[:8]) + ("…" if len(out) > 8 else ""))
    conn.commit()
    return {"expired": len(out), "items": out}


def watch(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """Every 15 minutes: spend spikes (pause first, then the Access manager
    reviews) and the company cap (ping the owner at 80 %)."""
    if not store.ready(conn):
        return {}
    now = now or utcnow()
    cfg = settings(conn)
    paused = []
    hour_ago, week_ago = now - timedelta(hours=1), now - timedelta(days=7)
    for a in conn.execute("SELECT id, name, created_at FROM actors WHERE kind != 'human' AND archived_at IS NULL "
                          "AND paused_at IS NULL").fetchall():
        last = spend(conn, a["id"], hour_ago, now + timedelta(seconds=1))
        if last["usd"] < cfg["spike_floor_usd"] and last["tokens"] < cfg["spike_floor_tokens"]:
            continue
        base = spend(conn, a["id"], week_ago, hour_ago)
        hours = 7 * 24 - 1
        b_usd, b_tok = base["usd"] / hours, base["tokens"] / hours
        spiked = ((last["usd"] >= cfg["spike_floor_usd"] and last["usd"] > cfg["spike_factor"] * b_usd)
                  or (last["tokens"] >= cfg["spike_floor_tokens"] and last["tokens"] > cfg["spike_factor"] * b_tok))
        if spiked:
            pause_for_spike(conn, a["id"], {"last_hour_usd": last["usd"], "last_hour_tokens": last["tokens"],
                                            "baseline_usd": round(b_usd, 4), "baseline_tokens": round(b_tok),
                                            "factor": cfg["spike_factor"]})
            paused.append(a["name"])
    alerts = []
    for metric in ("usd_day", "usd_month", "tokens_day", "tokens_month", "runs_day"):
        cap = limit(conn, None, metric, now)
        if cap:
            u = used(conn, None, metric, now)
            if u >= cfg["cap_alert_ratio"] * cap and _cap_alert(conn, metric, u, cap, now, full=u >= cap):
                alerts.append(metric)
    conn.commit()
    return {"paused": paused, "cap_alerts": alerts}


def _cap_alert(conn: sqlite3.Connection, metric: str, u: float, cap: float, now: datetime, full: bool) -> bool:
    """One ping per metric, period and level (80 % / full)."""
    from .. import settings_store

    period = now.astimezone(TZ).strftime("%Y-%m" if metric.endswith("month") else "%Y-%m-%d")
    key = f"{metric}:{period}:{'full' if full else '80'}"
    sent = settings_store.get(conn, "access.cap_alerts", []) or []
    if key in sent:
        return False
    owner = actors.owner_id(conn)
    settings_store.put(conn, Ctx(owner, via="system"), "access.cap_alerts", (sent + [key])[-50:])
    pct = int(100 * u / cap) if cap else 100
    _dm_owner(conn, f"Strop firmy {METRICS[metric]}: {_fmt(metric, u)} z {_fmt(metric, cap)} ({pct} %)."
              + (" Agenti teď nepoběží, dokud strop nezvedneš." if full else
                 " Hlídám to; strop zvedáš jen ty (stránka Správce přístupů)."))
    audit.log(conn, Ctx(manager_id(conn) or owner, via="system"), "access_cap_alert", None, None, metric=metric,
              used=u, cap=cap, full=full)
    return True


DECISIONS = ("access_grant", "access_revoke", "access_budget", "access_deny", "access_escalate", "access_expire",
             "access_pause", "access_resume")


def digest(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """One short daily message to the owner: what was granted, revoked or
    raised, and the spend against the company cap. Nothing waits on the owner."""
    from .. import settings_store

    if not store.ready(conn):
        return {"sent": False}
    now = now or utcnow()
    owner = actors.owner_id(conn)
    since = settings_store.get(conn, "access.digest_at") or _iso(now - timedelta(days=1))
    rows = conn.execute(
        f"""SELECT l.*, a.name AS actor_name, t.name AS target FROM audit_log l LEFT JOIN actors a ON a.id = l.actor_id
            LEFT JOIN actors t ON l.entity = 'actor' AND t.id = l.entity_id
            WHERE l.at > ? AND l.action IN ({','.join('?' * len(DECISIONS))}) ORDER BY l.id""",
        (since, *DECISIONS)).fetchall()
    settings_store.put(conn, Ctx(owner, via="system"), "access.digest_at", _iso(now))
    day = spend(conn, None, now - timedelta(days=1), now + timedelta(seconds=1))
    month = spend(conn, None, _month_start(now), now + timedelta(seconds=1))
    if not rows and not day["usd"] and not day["tokens"]:
        conn.commit()
        return {"sent": False}
    counts: dict[str, int] = {}
    lines = []
    for r in rows:
        counts[r["action"]] = counts.get(r["action"], 0) + 1
        d = json.loads(r["detail"] or "{}")
        what = d.get("capability") or (METRICS.get(d.get("metric", ""), "") + (
            f" → {_fmt(d['metric'], d.get('amount'))}" if d.get("metric") and r["action"] == "access_budget" else ""))
        if len(lines) < 8:
            lines.append(f"- {r['action'][7:]}: {r['target'] or 'firma'} `{what or '#' + str(d.get('request', ''))}`"
                         + (f" — {str(d.get('reason') or d.get('note'))[:120]}" if d.get("reason") or d.get("note") else ""))
    if len(rows) > 8:
        lines.append(f"- … a {len(rows) - 8} dalších (stránka Správce přístupů)")

    def vs(metric: str, value: float) -> str:
        cap = limit(conn, None, metric, now)
        return _fmt(metric, value) + (f" z {_fmt(metric, cap)} ({int(100 * value / cap)} %)" if cap else " (bez stropu)")

    summary = " · ".join(f"{k[7:]} {v}" for k, v in counts.items()) or "žádné změny"
    body = "\n".join([f"**Přístupy za poslední den** — {summary}", *lines, "",
                      f"**Útrata**: 24 h {vs('usd_day', day['usd'])}, {vs('tokens_day', day['tokens'])}; "
                      f"měsíc {vs('usd_month', month['usd'])}."])
    _dm_owner(conn, body)
    conn.commit()
    return {"sent": True, "changes": len(rows)}


def weekly(conn: sqlite3.Connection) -> dict:
    """Monday: a task for the Access manager to right-size budgets by cost per accepted task."""
    ref = _manager_task(conn, "Týdenní revize rozpočtů", (
        "Purpose: right-size every agent's budget by what its accepted work costs.\n"
        "Source: the weekly Access review routine.\n\n"
        "1. `access_usage(days=7)`: spend, runs and cost per accepted task per agent.\n"
        "2. Lower budgets that are far above use, raise ones that keep hitting their limit with good work "
        "(`access_set_budget`, a reason each). Revoke temporary grants nobody used.\n"
        "3. `access_report` with a short Markdown report for the owner (a table: agent, spend, accepted tasks, "
        "cost per task, change), then `complete_task`."), "Report filed with access_report; budgets adjusted with reasons.")
    return {"task": ref}


def report(conn: sqlite3.Connection, ctx: Ctx, title: str, markdown: str) -> dict:
    """The weekly report: a note (topic pristupy) and a short DM to the owner."""
    from .. import notes

    if actors.get(conn, ctx.actor_id)["name"] != AM_NAME and not actors.get(conn, ctx.actor_id)["is_owner"]:
        raise Forbidden("reports here come from the Access manager")
    markdown = (markdown or "").strip()
    if len(markdown) < 20:
        raise AccessError("the report is empty")
    n = notes.create(conn, ctx, {"title": (title or "Týdenní revize přístupů")[:200], "body": markdown[:20000],
                                 "topic": "pristupy"})
    first = next((line.strip("# ").strip() for line in markdown.splitlines() if line.strip()), "")
    _dm_owner(conn, f"Týdenní revize přístupů je v poznámce #{n['id']}: {first[:200]}")
    audit.log(conn, ctx, "access_report", "note", n["id"])
    conn.commit()
    return {"note_id": n["id"]}


# ------------------------------------------------------------------ usage and history (for the manager and the UI)

def usage(conn: sqlite3.Connection, agent_id: int | None = None, days: int = 7) -> dict:
    """Per agent: spend, tokens, runs, accepted tasks and cost per accepted task,
    the active limits, and runaway signals. Numbers only, never content."""
    from . import litellm

    store.ensure_schema(conn)
    now = utcnow()
    start = now - timedelta(days=max(1, min(days, 90)))
    rows = conn.execute("SELECT id, name, paused_at FROM actors WHERE kind != 'human' AND archived_at IS NULL"
                        + (" AND id = ?" if agent_id else "") + " ORDER BY id", (agent_id,) if agent_id else ()).fetchall()
    out = []
    lite = litellm.client()
    for a in rows:
        s = spend(conn, a["id"], start, now + timedelta(seconds=1))
        accepted = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE assignee_id = ? AND status = 'done' AND completed_at >= ?",
            (a["id"], _iso(start))).fetchone()[0]
        out.append({
            "agent": a["name"], "id": a["id"], "paused": bool(a["paused_at"]), **s, "accepted_tasks": accepted,
            "usd_per_accepted_task": round(s["usd"] / accepted, 4) if accepted else None,
            "tokens_per_accepted_task": s["tokens"] // accepted if accepted else None,
            "limits": {m: v["limit"] for m, v in budgets(conn, a["id"], now).items() if v["limit"] is not None},
            "signals": signals(conn, a["id"], now),
            **({"litellm": lite.spend(a["id"])} if lite.enabled else {}),
        })
    company = {m: {"limit": v["limit"], "used": v["used"]} for m, v in budgets(conn, None, now).items()
               if m != "usd_run"}
    return {"days": days, "agents": out, "company": company}


def history(conn: sqlite3.Connection, agent_id: int | None = None, limit_: int = 50) -> list[dict]:
    where = "l.action LIKE 'access_%'" + (" AND l.entity = 'actor' AND l.entity_id = ?" if agent_id else "")
    rows = conn.execute(
        f"""SELECT l.*, a.name AS actor_name FROM audit_log l LEFT JOIN actors a ON a.id = l.actor_id
            WHERE {where} ORDER BY l.id DESC LIMIT ?""", (*((agent_id,) if agent_id else ()), limit_)).fetchall()
    return [{**dict(r), "detail": json.loads(r["detail"] or "{}")} for r in rows]


def agent_view(conn: sqlite3.Connection, agent_id: int) -> dict:
    """Everything the agent page shows about access."""
    store.ensure_schema(conn)
    row = actors.get(conn, agent_id)
    return {"managed": store.seeded(conn, agent_id), "is_manager": row["name"] == AM_NAME,
            "grants": grants(conn, agent_id), "ended": [g for g in grants(conn, agent_id, include_ended=True, limit=40)
                                                        if not g["active"]][:20],
            "budgets": budgets(conn, agent_id), "requests": requests(conn, "open", agent_id),
            "recent_requests": requests(conn, None, agent_id, 10), "history": history(conn, agent_id, 40)}


def company_view(conn: sqlite3.Connection) -> dict:
    store.ensure_schema(conn)
    from .. import killswitch
    from . import litellm

    return {"budgets": budgets(conn, None), "settings": settings(conn), "frozen": killswitch.is_frozen(conn),
            "requests": requests(conn, "open"), "history": history(conn, None, 40), "metrics": METRICS,
            "manager_id": manager_id(conn), "litellm": litellm.client().enabled}


# ------------------------------------------------------------------ plumbing

def _ensure_seeded(conn: sqlite3.Connection, agent_id: int) -> None:
    """An agent not managed yet: its current permissions become grants first."""
    if not store.seeded(conn, agent_id):
        seed_agent(conn, agent_id, actors.owner_id(conn))


def _post_team(conn: sqlite3.Connection, ctx: Ctx, body: str) -> None:
    from .. import chat

    try:
        chat.post_to_team(conn, ctx.actor_id, body[:3900])
    except Exception:  # noqa: BLE001 - the decision stands; the audit log has it
        audit.log(conn, ctx, "access_post_failed", None, None)


def _dm_owner(conn: sqlite3.Connection, body: str) -> None:
    from .. import chat

    am = manager_id(conn)
    owner = actors.owner_id(conn)
    try:
        if am:
            chat.send_dm(conn, Ctx(am, via="system"), owner, body[:3900], priority="fyi", system=True)
        else:
            chat.post_to_team(conn, owner, body[:3900])
    except Exception:  # noqa: BLE001
        audit.log(conn, Ctx(owner, via="system"), "access_post_failed", None, None)


def _tell(conn: sqlite3.Connection, ctx: Ctx, agent_id: int, body: str) -> None:
    """The decision into the agent's inbox, and wake its worker."""
    from .. import chat, wake

    if agent_id == ctx.actor_id or actors.get(conn, agent_id)["archived_at"]:
        return
    try:
        chat.send_dm(conn, ctx, agent_id, body[:3900], priority="change_plan", system=True)
    except Exception:  # noqa: BLE001
        return
    wake.wake(agent_id)


def _resume_task(conn: sqlite3.Connection, ctx: Ctx, r: sqlite3.Row, why: str) -> None:
    from .. import tasks, versioning

    if not r["blocking"] or not r["task_id"]:
        return
    t = conn.execute("SELECT status FROM tasks WHERE id = ?", (r["task_id"],)).fetchone()
    if t and t["status"] == "waiting":
        versioning.update(conn, ctx, tasks.ENTITY, r["task_id"], {"status": "next", "progress_note": why[:500]},
                          action="resume")


def _manager_task(conn: sqlite3.Connection, title: str, notes: str, done: str) -> str | None:
    """A task in the Access manager's own queue that it closes itself (its
    decisions are reported in #team and the digest, not reviewed one by one)."""
    from .. import tasks, wake

    am = manager_id(conn)
    if am is None:
        return None
    ctx = Ctx(am, via="system")
    t = tasks.create(conn, ctx, {"title": title[:200], "notes": notes, "definition_of_done": done,
                                 "assignee": {"type": "agent", "id": am}, "status": "next", "priority": 2,
                                 "topic": "pristupy", "source": "access"})
    conn.execute("UPDATE tasks SET reviewer_id = ? WHERE id = ?", (am, t["id"]))
    wake.wake(am)
    return t["ref"]


def _wake_manager(conn: sqlite3.Connection, line: str) -> None:
    """One open queue task at a time; a new request while it waits only adds a line."""
    from .. import comments

    am = manager_id(conn)
    if am is None:
        return
    open_ = conn.execute("SELECT id FROM tasks WHERE assignee_id = ? AND source = 'access' AND status = 'next' "
                         "AND archived_at IS NULL AND title LIKE 'Žádosti o přístup%' ORDER BY id LIMIT 1",
                         (am,)).fetchone()
    if open_:
        comments.log(conn, Ctx(am, via="system"), open_["id"], f"Nová: {line}", "system")
        return
    _manager_task(conn, "Žádosti o přístup", (
        "Purpose: decide the open access requests and budget limit hits.\n"
        f"Source: {line}.\n\n"
        "1. `access_review_requests` for the open ones.\n"
        "2. Decide each with `access_decide` (grant, deny, or escalate owner-only items) and a reason; look at "
        "`access_usage` for the agent first.\n"
        "3. `complete_task` with one line per decision."),
        "Every open request has a decision with a reason.")
