"""Tunable thresholds. Defaults follow spec 3.2 and 8; the owner may change them."""

from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True)
class HRPolicy:
    max_active_agents: int = 10
    max_new_agents_per_agent_per_day: int = 2
    # Schedules an agent creates for itself or its team (pos.schedules).
    max_active_schedules_per_agent: int = 5
    min_schedule_interval_minutes: int = 15
    # Review window for effectiveness numbers.
    window: timedelta = timedelta(days=14)
    # A long-lived agent with no activity this long is archived.
    idle_after: timedelta = timedelta(days=14)
    # Agents younger than this are not judged on effectiveness yet.
    grace_period: timedelta = timedelta(days=7)
    # Need at least this many finished tasks before a score means anything.
    min_tasks_for_score: int = 3
    # Score below this is "ineffective"; below the archive bar the agent is retired.
    low_score: float = 0.5
    archive_score: float = 0.25
    # Returned share above this suggests the instructions need work.
    high_return_rate: float = 0.3
    # Purpose word overlap (Jaccard) at or above this marks agents as duplicates.
    duplicate_similarity: float = 0.6
    # A one-shot agent this old with nothing open has done its job.
    one_shot_done_after: timedelta = timedelta(days=1)
    # A one-shot agent that keeps getting work becomes long-lived.
    promote_after_tasks: int = 3
