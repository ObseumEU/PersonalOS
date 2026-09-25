"""Daily review of the agent roster (spec 4.1)."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from itertools import combinations

from .interfaces import HRActions, HRDataSource
from .metrics import Effectiveness, TeamKpis, rate_agent, team_kpis
from .models import AgentRecord, AgentStats, Lifetime
from .policy import HRPolicy
from .similarity import purpose_similarity


class ProposalKind(str, Enum):
    ARCHIVE_EXPIRED = "archive_expired"
    ARCHIVE_DONE = "archive_done"
    ARCHIVE_IDLE = "archive_idle"
    ARCHIVE_INEFFECTIVE = "archive_ineffective"
    PROMOTE_LONG_LIVED = "promote_long_lived"
    MERGE = "merge"
    REVISE_INSTRUCTIONS = "revise_instructions"
    OVER_CAPACITY = "over_capacity"


# Reversible steps HR takes itself; the rest need judgement and become tasks.
AUTO_APPLY = {
    ProposalKind.ARCHIVE_EXPIRED,
    ProposalKind.ARCHIVE_DONE,
    ProposalKind.ARCHIVE_IDLE,
    ProposalKind.ARCHIVE_INEFFECTIVE,
    ProposalKind.PROMOTE_LONG_LIVED,
}
ARCHIVE_KINDS = {k for k in AUTO_APPLY if k.value.startswith("archive")}


@dataclass(frozen=True)
class Proposal:
    kind: ProposalKind
    agent_id: str
    reason: str
    # For MERGE: the agent that absorbs ``agent_id``.
    into: str | None = None


@dataclass
class ReviewResult:
    at: datetime
    proposals: list[Proposal]
    ratings: dict[str, Effectiveness]
    stats: dict[str, AgentStats]
    kpis: TeamKpis
    active_before: int
    applied: list[Proposal] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)

    @property
    def active_after(self) -> int:
        return self.active_before - sum(1 for p in self.applied if p.kind in ARCHIVE_KINDS)


def _days(delta) -> int:
    return int(delta.total_seconds() // 86400)


def _archive_reason(
    agent: AgentRecord, stats: AgentStats, rating: Effectiveness, now: datetime, policy: HRPolicy
) -> Proposal | None:
    age = now - agent.created_at
    last = agent.last_active_at or agent.created_at

    if agent.expires_at and agent.expires_at <= now:
        return Proposal(ProposalKind.ARCHIVE_EXPIRED, agent.id, f"vypršelo {agent.expires_at:%Y-%m-%d}")

    if (
        agent.lifetime is Lifetime.ONE_SHOT
        and stats.tasks_open == 0
        and stats.tasks_completed + stats.tasks_failed > 0
        and now - last >= policy.one_shot_done_after
    ):
        return Proposal(ProposalKind.ARCHIVE_DONE, agent.id, "jednorázový agent dokončil svůj úkol")

    if stats.tasks_open == 0 and now - last >= policy.idle_after:
        return Proposal(ProposalKind.ARCHIVE_IDLE, agent.id, f"bez práce {_days(now - last)} dní")

    if age >= policy.grace_period and rating.score is not None and rating.score < policy.archive_score:
        return Proposal(
            ProposalKind.ARCHIVE_INEFFECTIVE,
            agent.id,
            f"skóre {rating.score:.2f} pod hranicí {policy.archive_score:.2f}",
        )
    return None


def _merge_proposals(
    agents: list[AgentRecord], ratings: dict[str, Effectiveness], policy: HRPolicy
) -> list[Proposal]:
    def strength(agent: AgentRecord) -> tuple:
        r = ratings[agent.id]
        return (r.score if r.score is not None else -1.0, r.finished, -agent.created_at.timestamp())

    proposals: list[Proposal] = []
    absorbed: set[str] = set()
    pairs = sorted(
        ((purpose_similarity(a.purpose, b.purpose), a, b) for a, b in combinations(agents, 2)),
        key=lambda t: t[0],
        reverse=True,
    )
    for sim, a, b in pairs:
        if sim < policy.duplicate_similarity:
            break
        if a.id in absorbed or b.id in absorbed:
            continue
        keep, drop = (a, b) if strength(a) >= strength(b) else (b, a)
        absorbed.add(drop.id)
        proposals.append(
            Proposal(
                ProposalKind.MERGE,
                drop.id,
                f"stejný účel jako {keep.name} (shoda {sim:.0%})",
                into=keep.id,
            )
        )
    return proposals


def review_agents(
    agents: list[AgentRecord],
    stats: dict[str, AgentStats],
    now: datetime,
    policy: HRPolicy = HRPolicy(),
) -> ReviewResult:
    """Pure decision step: rate every active agent and propose what to change."""
    active = [a for a in agents if a.active]
    stats = {a.id: stats.get(a.id, AgentStats(agent_id=a.id)) for a in active}
    ratings = {a.id: rate_agent(stats[a.id], policy) for a in active}
    proposals: list[Proposal] = []
    remaining: list[AgentRecord] = []

    for agent in active:
        if agent.system:
            continue
        s, r = stats[agent.id], ratings[agent.id]
        archive = _archive_reason(agent, s, r, now, policy)
        if archive:
            proposals.append(archive)
            continue
        remaining.append(agent)

        if agent.lifetime is Lifetime.ONE_SHOT and s.tasks_completed >= policy.promote_after_tasks:
            proposals.append(
                Proposal(
                    ProposalKind.PROMOTE_LONG_LIVED,
                    agent.id,
                    f"jednorázový agent dostal už {s.tasks_completed} úkolů",
                )
            )
        if now - agent.created_at >= policy.grace_period and r.score is not None:
            if r.ineffective(policy) or (r.return_rate or 0) > policy.high_return_rate:
                proposals.append(
                    Proposal(
                        ProposalKind.REVISE_INSTRUCTIONS,
                        agent.id,
                        f"skóre {r.score:.2f}, vráceno {r.return_rate or 0:.0%} práce",
                    )
                )

    proposals.extend(_merge_proposals(remaining, ratings, policy))

    archived = sum(1 for p in proposals if p.kind in ARCHIVE_KINDS)
    merged = sum(1 for p in proposals if p.kind is ProposalKind.MERGE)
    if len(active) - archived - merged > policy.max_active_agents:
        proposals.append(
            Proposal(
                ProposalKind.OVER_CAPACITY,
                "",
                f"{len(active) - archived - merged} aktivních agentů nad limitem {policy.max_active_agents}",
            )
        )

    return ReviewResult(
        at=now,
        proposals=proposals,
        ratings=ratings,
        stats=stats,
        kpis=team_kpis(stats.values()),
        active_before=len(active),
    )


def _task_for(proposal: Proposal, names: dict[str, str]) -> tuple[str, str]:
    name = names.get(proposal.agent_id, proposal.agent_id)
    if proposal.kind is ProposalKind.MERGE:
        into = names.get(proposal.into or "", proposal.into)
        return (
            f"Sloučit agenta {name} do {into}",
            f"{proposal.reason}. Převeď otevřené úkoly a užitečné instrukce z {name} do {into}, "
            f"pak {name} archivuj.",
        )
    if proposal.kind is ProposalKind.REVISE_INSTRUCTIONS:
        return (
            f"Upravit instrukce agenta {name}",
            f"{proposal.reason}. Projdi vrácené úkoly, najdi společnou příčinu a uprav instrukce "
            "(kap. 6 specifikace: testy, merge do main).",
        )
    return (
        "Příliš mnoho aktivních agentů",
        f"{proposal.reason}. Navrhni, které agenty sloučit nebo archivovat, nebo požádej majitele "
        "o zvýšení limitu.",
    )


def run_daily_review(
    source: HRDataSource,
    actions: HRActions,
    now: datetime,
    policy: HRPolicy = HRPolicy(),
    hr_agent_id: str = "hr",
    apply: bool = True,
    coach_id: str | None = None,
) -> ReviewResult:
    """Read the roster, decide, then archive/promote directly and file tasks for the rest."""
    agents = source.list_agents()
    since = now - policy.window
    stats = {a.id: source.agent_stats(a.id, since, now) for a in agents if a.active}
    result = review_agents(agents, stats, now, policy)
    if not apply:
        return result

    names = {a.id: a.name for a in agents}
    by_id = {a.id: a for a in agents}
    for proposal in result.proposals:
        agent = by_id.get(proposal.agent_id)
        if proposal.kind in ARCHIVE_KINDS and agent is not None and agent.seeded:
            # A role agent from git is not HR's to retire: its lead gets the proposal.
            result.task_ids.append(actions.create_task(
                f"Návrh HR: archivovat {agent.name}?",
                f"{proposal.reason}. {agent.name} je role definovaná v repozitáři (agents/), HR ji "
                "sám nearchivuje. Rozhodni: upravit instrukce, dát mu práci, nebo archivovat.",
                assignee=agent.lead_id or hr_agent_id))
        elif proposal.kind in ARCHIVE_KINDS:
            actions.archive_agent(proposal.agent_id, f"HR: {proposal.reason}")
            result.applied.append(proposal)
        elif proposal.kind is ProposalKind.PROMOTE_LONG_LIVED:
            actions.set_lifetime(proposal.agent_id, Lifetime.LONG_LIVED, f"HR: {proposal.reason}")
            result.applied.append(proposal)
        else:
            title, body = _task_for(proposal, names)
            # Merging agents and revising instructions is the Agent coach's job when there is one.
            coach_job = proposal.kind in (ProposalKind.MERGE, ProposalKind.REVISE_INSTRUCTIONS)
            result.task_ids.append(actions.create_task(
                title, body, assignee=coach_id if coach_job and coach_id else hr_agent_id))
    return result
