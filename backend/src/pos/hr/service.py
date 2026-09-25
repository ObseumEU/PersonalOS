"""HR agent entry points used by the API, MCP tools, CLI and the core's create_agent."""

import sqlite3
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone

from .. import actors, approvals, audit
from ..budget import service as budget
from ..core import TZ, Ctx, Forbidden, NotFound, now_iso
from . import store
from .limits import OverLimitAction, decide_over_limit
from .metrics import TeamKpis
from .models import CreateAgentRequest, Lifetime
from .platform import CorePlatform, iso
from .policy import HRPolicy
from .report import file_weekly_report
from .review import ReviewResult, run_daily_review

HR_NAME = "HR agent"
HR_PURPOSE = "Hlídá počet agentů, jejich efektivitu a životnost; rozhoduje o založení nad limit."


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_hr_agent(conn: sqlite3.Connection) -> int:
    """Create the HR agent actor and its system profile once; return its actor id."""
    store.ensure_schema(conn)
    row = conn.execute("SELECT id FROM actors WHERE name = ?", (HR_NAME,)).fetchone()
    if row is None:
        hr_id = conn.execute(
            "INSERT INTO actors (kind, name, is_owner, created_at) VALUES ('agent', ?, 0, ?)",
            (HR_NAME, now_iso()),
        ).lastrowid
    else:
        hr_id = row["id"]
    if store.get_profile(conn, hr_id) is None:
        store.set_profile(conn, hr_id, purpose=HR_PURPOSE, lifetime="long_lived", system=1)
    conn.commit()
    budget.set_agent_class(conn, str(hr_id), "system")
    conn.commit()
    return hr_id


def _hr_ctx(conn: sqlite3.Connection, via: str) -> Ctx:
    return Ctx(ensure_hr_agent(conn), via=via)


def _may_run_hr(conn: sqlite3.Connection, ctx: Ctx) -> None:
    """Applying a review or filing reports: the owner or the HR agent itself."""
    me = actors.get(conn, ctx.actor_id)
    if not (me["is_owner"] or me["name"] == HR_NAME):
        raise Forbidden("only the owner or the HR agent runs the HR review")


def _serialize(result: ReviewResult, names: dict[str, str]) -> dict:
    return {
        "at": iso(result.at),
        "active_before": result.active_before,
        "active_after": result.active_after,
        "kpis": asdict(result.kpis),
        "ratings": [
            {**asdict(r), "name": names.get(r.agent_id, r.agent_id)}
            for r in sorted(result.ratings.values(), key=lambda r: (r.score is None, -(r.score or 0)))
        ],
        "proposals": [
            {"kind": p.kind.value, "agent_id": p.agent_id, "agent": names.get(p.agent_id, ""),
             "into": p.into, "reason": p.reason, "applied": p in result.applied}
            for p in result.proposals
        ],
        "task_ids": result.task_ids,
    }


def daily_review(conn: sqlite3.Connection, ctx: Ctx | None = None, *, apply: bool = True,
                 now: datetime | None = None, policy: HRPolicy = HRPolicy()) -> dict:
    hr_ctx = _hr_ctx(conn, ctx.via if ctx else "system")
    if apply and ctx is not None:
        _may_run_hr(conn, ctx)
    platform = CorePlatform(conn, hr_ctx)
    now = now or utcnow()
    result = run_daily_review(platform, platform, now, policy, hr_agent_id=str(hr_ctx.actor_id), apply=apply)
    names = {a.id: a.name for a in platform.list_agents()}
    report = _serialize(result, names)
    if apply:
        store.save_review(conn, "daily", True, report)
        audit.log(conn, hr_ctx, "hr_daily_review", archived=result.active_before - result.active_after,
                  tasks=len(result.task_ids))
        conn.commit()
    return report


def weekly_report(conn: sqlite3.Connection, ctx: Ctx | None = None, *, now: datetime | None = None,
                  policy: HRPolicy = HRPolicy()) -> dict:
    """Dry-run review over the last week and a "k přečtení" task for the owner."""
    hr_ctx = _hr_ctx(conn, ctx.via if ctx else "system")
    if ctx is not None:
        _may_run_hr(conn, ctx)
    platform = CorePlatform(conn, hr_ctx)
    now = now or utcnow()
    week = replace(policy, window=timedelta(days=7))
    result = run_daily_review(platform, platform, now, week, apply=False)
    last = store.latest_review(conn, "weekly")
    previous = TeamKpis(**last["kpis"]) if last else None
    agents = platform.list_agents()
    task_id = file_weekly_report(result, agents, platform, owner_id=str(actors.owner_id(conn)), previous=previous)
    report = {**_serialize(result, {a.id: a.name for a in agents}), "report_task_id": task_id}
    store.save_review(conn, "weekly", False, report)
    conn.commit()
    return report


def register_agent(conn: sqlite3.Connection, actor_id: int, *, purpose: str, lifetime: str = "long_lived",
                   created_by: int | None = None, expires_at: str | None = None, system: bool = False) -> None:
    """Record the spec 3.1 fields HR needs for an agent the core just created."""
    store.ensure_schema(conn)
    actors.get(conn, actor_id)
    Lifetime(lifetime)
    store.set_profile(conn, actor_id, purpose=purpose, lifetime=lifetime, created_by=created_by,
                      expires_at=expires_at, system=int(system))


def _active_agents(platform: CorePlatform) -> list:
    return [a for a in platform.list_agents() if a.active and not a.system]


def _created_today(conn: sqlite3.Connection, creator_id: int) -> int:
    start = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    return conn.execute(
        """SELECT COUNT(*) FROM hr_agent_profiles p JOIN actors a ON a.id = p.actor_id
           WHERE p.created_by = ? AND a.created_at >= ?""",
        (creator_id, iso(start)),
    ).fetchone()[0]


def admit_agent(conn: sqlite3.Connection, ctx: Ctx, *, name: str, purpose: str, lifetime: str = "one_shot",
                now: datetime | None = None, policy: HRPolicy = HRPolicy()) -> dict:
    """Check the spec 3.2 limits before create_agent and decide when one is hit.

    Returns {"allowed": True} when the agent may be created. Otherwise HR's decision:
    reuse an existing agent, replace (HR archives a weak one and then allows), defer
    to tomorrow, or ask the owner (an approval is queued). Never refuses silently.
    """
    hr_ctx = _hr_ctx(conn, ctx.via)
    platform = CorePlatform(conn, hr_ctx)
    now = now or utcnow()
    creator = actors.get(conn, ctx.actor_id)
    active = _active_agents(platform)

    reason = None
    if not creator["is_owner"] and _created_today(conn, ctx.actor_id) >= policy.max_new_agents_per_agent_per_day:
        reason = "daily_limit"
    elif len(active) >= policy.max_active_agents:
        reason = "active_limit"
    if reason is None:
        return {"allowed": True}

    request = CreateAgentRequest(name=name, purpose=purpose, requested_by=str(ctx.actor_id),
                                 lifetime=Lifetime(lifetime), reason=reason)
    since = now - policy.window
    stats = {a.id: platform.agent_stats(a.id, since, now) for a in active}
    decision = decide_over_limit(request, active, stats, now, policy)
    out = {"allowed": False, "limit": reason, "decision": decision.action.value,
           "reason": decision.reason, "target_id": decision.target_id}
    if decision.action is OverLimitAction.REPLACE:
        platform.archive_agent(decision.target_id, f"HR: místo pro {name}: {decision.reason}")
        out["allowed"] = True
    elif decision.action is OverLimitAction.ASK_OWNER:
        out["approval_id"] = approvals.request(
            conn, hr_ctx, "raise_agent_limit",
            {"requested_by": creator["name"], "name": name, "purpose": purpose,
             "max_active_agents": policy.max_active_agents, "reason": decision.reason},
        )["id"]
    audit.log(conn, hr_ctx, "hr_over_limit", "actor", ctx.actor_id, name=name, **{
        k: v for k, v in out.items() if k in ("limit", "decision", "target_id")})
    conn.commit()
    return out


def agents_overview(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """Current roster with scores, for the Agents screen. Read-only."""
    report = daily_review(conn, apply=False, now=now)
    report["last_daily"] = (store.latest_review(conn, "daily") or {}).get("at")
    report["last_weekly"] = (store.latest_review(conn, "weekly") or {}).get("at")
    return report


def restore(conn: sqlite3.Connection, ctx: Ctx, agent_id: int) -> None:
    from .platform import restore_agent

    _may_run_hr(conn, ctx)
    row = actors.get(conn, agent_id)
    if row["kind"] not in ("ai", "agent"):
        raise NotFound(f"agent {agent_id}")
    restore_agent(conn, ctx, agent_id)
    conn.commit()

