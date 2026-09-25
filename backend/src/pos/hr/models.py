"""Plain data the HR agent reasons about. Mirrors the agent fields in spec 3.1."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Lifetime(str, Enum):
    ONE_SHOT = "one_shot"
    LONG_LIVED = "long_lived"


class Status(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


@dataclass(frozen=True)
class AgentRecord:
    id: str
    name: str
    purpose: str
    created_by: str
    lifetime: Lifetime
    created_at: datetime
    status: Status = Status.ACTIVE
    expires_at: datetime | None = None
    # Last time the agent claimed, progressed or finished a task.
    last_active_at: datetime | None = None
    # System agents (HR, budget, assistant) are part of the platform and never retired.
    system: bool = False
    permissions: frozenset[str] = field(default_factory=frozenset)

    @property
    def active(self) -> bool:
        return self.status is not Status.ARCHIVED


@dataclass(frozen=True)
class AgentStats:
    """What one agent did in a review window."""

    agent_id: str
    tasks_completed: int = 0
    # Completed without the owner stepping in (spec 1: "bez zásahu majitele").
    tasks_completed_unassisted: int = 0
    # Work the owner sent back as badly done.
    tasks_returned: int = 0
    tasks_failed: int = 0
    # Tasks assigned to the agent and still open at the end of the window.
    tasks_open: int = 0
    # "I had to babysit it" interventions by the owner.
    owner_interventions: int = 0
    tokens_used: int = 0


@dataclass(frozen=True)
class CreateAgentRequest:
    """A create_agent call that hit a limit from spec 3.2 and was handed to HR."""

    name: str
    purpose: str
    requested_by: str
    lifetime: Lifetime
    reason: str = ""  # "active_limit" or "daily_limit"
