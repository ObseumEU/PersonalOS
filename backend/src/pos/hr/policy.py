"""Tunable thresholds. Defaults follow spec 3.2 and 8; the owner may change them."""

import sqlite3
from dataclasses import dataclass, replace
from datetime import timedelta


@dataclass(frozen=True)
class HRPolicy:
    # The company of docs/REORG.md (the owner's request, 2026-09-26): ~21 role agents plus room to hire.
    max_active_agents: int = 26
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


# Settings (pos.settings_store) that override the defaults above.
SETTING_MAX_ACTIVE = "hr.max_active_agents"


def current(conn: sqlite3.Connection | None) -> HRPolicy:
    """The defaults with what the owner changed (an approved raise_agent_limit)."""
    policy = HRPolicy()
    if conn is None:
        return policy
    from ..settings_store import get

    limit = get(conn, SETTING_MAX_ACTIVE)
    return replace(policy, max_active_agents=int(limit)) if limit else policy
