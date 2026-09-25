"""HR's decision when create_agent hits a limit from spec 3.2.

Over-limit requests are never dropped silently: the core hands them to HR, and HR
either reuses an existing agent, frees a slot, defers, or asks the owner (only the
owner may raise limits, spec 5.1).
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .metrics import rate_agent
from .models import AgentRecord, AgentStats, CreateAgentRequest
from .policy import HRPolicy
from .similarity import purpose_similarity


class OverLimitAction(str, Enum):
    REUSE = "reuse"  # give the work to an existing agent with the same purpose
    REPLACE = "replace"  # archive a weak or idle agent, then create the new one
    DEFER = "defer"  # daily per-creator limit: try again tomorrow
    ASK_OWNER = "ask_owner"  # request a limit increase through the approval queue


@dataclass(frozen=True)
class OverLimitDecision:
    action: OverLimitAction
    reason: str
    # REUSE: the agent to use. REPLACE: the agent to archive.
    target_id: str | None = None


def decide_over_limit(
    request: CreateAgentRequest,
    agents: list[AgentRecord],
    stats: dict[str, AgentStats],
    now: datetime,
    policy: HRPolicy = HRPolicy(),
) -> OverLimitDecision:
    active = [a for a in agents if a.active]

    best = max(
        ((purpose_similarity(request.purpose, a.purpose), a) for a in active),
        key=lambda t: t[0],
        default=(0.0, None),
    )
    if best[1] is not None and best[0] >= policy.duplicate_similarity:
        return OverLimitDecision(
            OverLimitAction.REUSE,
            f"{best[1].name} už dělá totéž (shoda {best[0]:.0%})",
            best[1].id,
        )

    if request.reason == "daily_limit":
        return OverLimitDecision(
            OverLimitAction.DEFER,
            f"{request.requested_by} dnes už založil {policy.max_new_agents_per_agent_per_day} agenty",
        )

    candidates = []
    for agent in active:
        if agent.system or agent.id == request.requested_by:
            continue
        s = stats.get(agent.id, AgentStats(agent_id=agent.id))
        if s.tasks_open:
            continue
        rating = rate_agent(s, policy)
        idle = now - (agent.last_active_at or agent.created_at)
        old_enough = now - agent.created_at >= policy.grace_period
        if old_enough and (rating.ineffective(policy) or idle >= policy.idle_after / 2):
            candidates.append((rating.score if rating.score is not None else 0.0, -idle.total_seconds(), agent))
    if candidates:
        score, _, weakest = min(candidates, key=lambda t: (t[0], t[1]))
        return OverLimitDecision(
            OverLimitAction.REPLACE,
            f"uvolní místo po {weakest.name} (skóre {score:.2f}, bez otevřené práce)",
            weakest.id,
        )

    return OverLimitDecision(
        OverLimitAction.ASK_OWNER,
        f"všech {len(active)} aktivních agentů pracuje; zvýšit limit {policy.max_active_agents} může jen majitel",
    )
