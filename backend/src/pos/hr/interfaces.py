"""What the HR agent needs from the platform core (thread "Map PersonalOS assistant state").

The core implements these over its own tables; tests use in-memory fakes.
"""

from datetime import datetime
from typing import Protocol

from .models import AgentRecord, AgentStats, Lifetime


class HRDataSource(Protocol):
    def list_agents(self) -> list[AgentRecord]:
        """Every agent, archived ones included."""

    def agent_stats(self, agent_id: str, since: datetime, until: datetime) -> AgentStats:
        """Task and token numbers for one agent in [since, until)."""


class HRActions(Protocol):
    def archive_agent(self, agent_id: str, reason: str) -> None:
        """Archive (never delete) an agent, spec 3.3. Must be restorable."""

    def set_lifetime(self, agent_id: str, lifetime: Lifetime, reason: str) -> None: ...

    def create_task(self, title: str, body: str, assignee: str, kind: str = "task") -> str:
        """Create a task and return its id. ``kind="read"`` is the "k přečtení" type."""
