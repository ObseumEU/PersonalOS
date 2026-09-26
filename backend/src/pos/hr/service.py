"""HR agent entry points used by the API, MCP tools, CLI and the core's create_agent."""

import sqlite3
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone

from .. import actors, approvals, audit, roles
from ..budget import service as budget
from ..core import TZ, Ctx, Forbidden, NotFound, now_iso
from . import store
from .limits import OverLimitAction, decide_over_limit
from .metrics import TeamKpis
from .models import CreateAgentRequest, Lifetime
from .platform import CorePlatform, iso
from .policy import SETTING_MAX_ACTIVE, HRPolicy, current
from .report import file_weekly_report
from .review import ReviewResult, run_daily_review

HR_NAME = roles.HR  # "HR agent" until the 2026-09 reorganisation
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
        store.set_profile(conn, Ctx(hr_id, via="system"), hr_id, purpose=HR_PURPOSE,
                          lifetime="long_lived", system=1)
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
        raise Forbidden(f"only the owner or the {HR_NAME} runs the HR review")


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
                 now: datetime | None = None, policy: HRPolicy | None = None) -> dict:
    policy = policy or current(conn)
    hr_ctx = _hr_ctx(conn, ctx.via if ctx else "system")
    if apply and ctx is not None:
        _may_run_hr(conn, ctx)
    platform = CorePlatform(conn, hr_ctx)
    now = now or utcnow()
    coach = conn.execute("SELECT id FROM actors WHERE name = ? AND archived_at IS NULL", (roles.COACH,)).fetchone()
    result = run_daily_review(platform, platform, now, policy, hr_agent_id=str(hr_ctx.actor_id), apply=apply,
                              coach_id=str(coach["id"]) if coach else None)
    names = {a.id: a.name for a in platform.list_agents()}
    report = _serialize(result, names)
    if apply:
        store.save_review(conn, "daily", True, report)
        audit.log(conn, hr_ctx, "hr_daily_review", archived=result.active_before - result.active_after,
                  tasks=len(result.task_ids))
        conn.commit()
    return report


def weekly_report(conn: sqlite3.Connection, ctx: Ctx | None = None, *, now: datetime | None = None,
                  policy: HRPolicy | None = None) -> dict:
    """Dry-run review over the last week and a "k přečtení" task for the owner."""
    policy = policy or current(conn)
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
    tools = tool_usage(conn, now - timedelta(days=7), now)
    if tools:
        from .. import comments

        lines = "\n".join(f"- {t['tool']}: {t['uses']}× ({t['failed']} failed) by {', '.join(t['by'])}" for t in tools)
        comments.log(conn, hr_ctx, int(task_id), f"Sdílené nástroje za týden (co kolegům pomáhá):\n{lines}", "system")
    report = {**_serialize(result, {a.id: a.name for a in agents}), "report_task_id": task_id, "tool_usage": tools}
    store.save_review(conn, "weekly", False, report)
    conn.commit()
    return report


def tool_usage(conn: sqlite3.Connection, since: datetime, until: datetime) -> list[dict]:
    """Which shared tools the team used in the window, how often, by whom, how often they failed."""
    rows = conn.execute(
        """SELECT u.tool, COUNT(*) AS uses, SUM(u.ok = 0) AS failed, GROUP_CONCAT(DISTINCT a.name) AS by_names
           FROM tool_usage u LEFT JOIN actors a ON a.id = u.actor_id WHERE u.at >= ? AND u.at <= ?
           GROUP BY u.tool ORDER BY uses DESC""", (iso(since), iso(until))).fetchall()
    return [{"tool": r["tool"], "uses": r["uses"], "failed": r["failed"] or 0,
             "by": sorted((r["by_names"] or "").split(",")) if r["by_names"] else []} for r in rows]


def register_agent(conn: sqlite3.Connection, actor_id: int, *, purpose: str, lifetime: str = "long_lived",
                   created_by: int | None = None, expires_at: str | None = None, system: bool = False,
                   ctx: Ctx | None = None) -> None:
    """Record the spec 3.1 fields HR needs for an agent the core just created."""
    store.ensure_schema(conn)
    actors.get(conn, actor_id)
    Lifetime(lifetime)
    ctx = ctx or Ctx(created_by or actors.owner_id(conn), via="system")
    store.set_profile(conn, ctx, actor_id, purpose=purpose, lifetime=lifetime, created_by=created_by,
                      expires_at=expires_at, system=int(system))


def _active_agents(platform: CorePlatform) -> list:
    return [a for a in platform.list_agents() if a.active and not a.system]


def room_for_agents(conn: sqlite3.Connection) -> int:
    """How many more non-system agents fit under the active limit (may be <= 0)."""
    platform = CorePlatform(conn, _hr_ctx(conn, "system"))
    return current(conn).max_active_agents - len(_active_agents(platform))


def _created_today(conn: sqlite3.Connection, creator_id: int) -> int:
    start = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    return conn.execute(
        """SELECT COUNT(*) FROM hr_profiles p JOIN actors a ON a.id = p.id
           WHERE p.created_by = ? AND a.created_at >= ?""",
        (creator_id, iso(start)),
    ).fetchone()[0]


def admit_agent(conn: sqlite3.Connection, ctx: Ctx, *, name: str, purpose: str, lifetime: str = "one_shot",
                now: datetime | None = None, policy: HRPolicy | None = None, defer_replace: bool = False) -> dict:
    """Check the spec 3.2 limits before create_agent and decide when one is hit.

    Returns {"allowed": True} when the agent may be created. Otherwise HR's decision:
    reuse an existing agent, replace (HR archives a weak one and then allows), defer
    to tomorrow, or ask the owner (an approval is queued). Never refuses silently.
    """
    policy = policy or current(conn)
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
        out["allowed"] = True
        if defer_replace:  # the caller archives the target once the new agent exists (one transaction)
            out["replace_id"] = int(decision.target_id)
        else:
            platform.archive_agent(decision.target_id, f"HR: místo pro {name}: {decision.reason}")
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
    """Scores and proposals for the Agents screen, from the last saved daily
    review: a GET never runs a review (not even a dry run) and writes nothing."""
    report = dict(store.latest_review(conn, "daily") or {"ratings": [], "proposals": []})
    report["last_daily"] = (store.latest_review(conn, "daily") or {}).get("at")
    report["last_weekly"] = (store.latest_review(conn, "weekly") or {}).get("at")
    return report


def restore(conn: sqlite3.Connection, ctx: Ctx, agent_id: int) -> str:
    from .platform import restore_agent

    _may_run_hr(conn, ctx)
    row = actors.get(conn, agent_id)
    if row["kind"] not in ("ai", "agent"):
        raise NotFound(f"agent {agent_id}")
    key = restore_agent(conn, ctx, agent_id)
    conn.commit()
    return key



def _on_limit_approved(conn: sqlite3.Connection, approval: dict) -> None:
    """The owner approved raise_agent_limit: one more active agent is allowed
    (a versioned setting HRPolicy reads), so the requester can create it now."""
    if approval["action"] != "raise_agent_limit":
        return
    details = approval.get("details") or {}
    active = len(_active_agents(CorePlatform(conn, _hr_ctx(conn, "approval"))))
    new = max(current(conn).max_active_agents, int(details.get("max_active_agents") or 0), active) + 1
    from ..settings_store import put

    owner = Ctx(approval["decided_by"] or actors.owner_id(conn), via="approval")
    put(conn, owner, SETTING_MAX_ACTIVE, new, action="raise_agent_limit")
    audit.log(conn, owner, "hr_limit_raised", "approval", approval["id"], max_active_agents=new)


approvals.on_approved(_on_limit_approved)
