"""Effectiveness of one agent and the team-level KPIs from spec 1."""

from collections.abc import Iterable
from dataclasses import dataclass

from .models import AgentStats
from .policy import HRPolicy


@dataclass(frozen=True)
class Effectiveness:
    agent_id: str
    finished: int
    # Share of finished work that was done and not returned (0..1).
    quality: float | None
    # Share of completed work done without the owner stepping in (0..1).
    autonomy: float | None
    return_rate: float | None
    tokens_per_task: float | None
    # Combined 0..1, or None while there is too little work to judge.
    score: float | None
    tokens_used: int = 0
    cost_usd: float = 0.0
    cost_per_task: float | None = None

    def ineffective(self, policy: HRPolicy) -> bool:
        return self.score is not None and self.score < policy.low_score


def _ratio(part: int, whole: int) -> float | None:
    return part / whole if whole else None


def rate_agent(stats: AgentStats, policy: HRPolicy = HRPolicy()) -> Effectiveness:
    finished = stats.tasks_completed + stats.tasks_failed
    good = max(stats.tasks_completed - stats.tasks_returned, 0)
    quality = _ratio(good, finished)
    autonomy = _ratio(min(stats.tasks_completed_unassisted, stats.tasks_completed), stats.tasks_completed)
    return_rate = _ratio(stats.tasks_returned, stats.tasks_completed)
    # Per task the runs were for (a handed-back task cost tokens too); without run data, per completed task.
    per = stats.tasks_worked or stats.tasks_completed
    tokens_per_task = stats.tokens_used / per if per else None
    cost_per_task = round(stats.cost_usd / per, 4) if per else None

    score = None
    if finished >= policy.min_tasks_for_score:
        # Each "had to babysit it" costs as much as a returned task.
        babysit = min(stats.owner_interventions / finished, 1.0)
        score = 0.6 * (quality or 0.0) + 0.4 * (autonomy or 0.0) - 0.3 * babysit
        score = round(min(max(score, 0.0), 1.0), 3)

    return Effectiveness(
        agent_id=stats.agent_id,
        finished=finished,
        quality=quality,
        autonomy=autonomy,
        return_rate=return_rate,
        tokens_per_task=tokens_per_task,
        score=score,
        tokens_used=stats.tokens_used,
        cost_usd=round(stats.cost_usd, 4),
        cost_per_task=cost_per_task,
    )


@dataclass(frozen=True)
class TeamKpis:
    """Spec 1 table: tracked month over month, compared by the caller."""

    tasks_completed: int
    unassisted_rate: float | None
    returned_rate: float | None
    owner_interventions: int
    tokens_used: int
    cost_usd: float = 0.0


def team_kpis(stats: Iterable[AgentStats]) -> TeamKpis:
    stats = list(stats)
    completed = sum(s.tasks_completed for s in stats)
    return TeamKpis(
        tasks_completed=completed,
        unassisted_rate=_ratio(sum(min(s.tasks_completed_unassisted, s.tasks_completed) for s in stats), completed),
        returned_rate=_ratio(sum(s.tasks_returned for s in stats), completed),
        owner_interventions=sum(s.owner_interventions for s in stats),
        tokens_used=sum(s.tokens_used for s in stats),
        cost_usd=round(sum(s.cost_usd for s in stats), 4),
    )
