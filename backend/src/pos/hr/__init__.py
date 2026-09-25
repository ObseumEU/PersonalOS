"""HR agent: reviews the agent roster, rates effectiveness and keeps the team small.

Spec: docs/AGENTS-SPEC.md, sections 3 and 4.1. The module is pure logic over two
interfaces (``HRDataSource`` to read, ``HRActions`` to act), so the platform core
plugs in its own storage without this package touching core tables.
"""

from .interfaces import HRActions, HRDataSource
from .limits import OverLimitDecision, decide_over_limit
from .metrics import Effectiveness, TeamKpis, rate_agent, team_kpis
from .models import AgentRecord, AgentStats, CreateAgentRequest, Lifetime, Status
from .policy import HRPolicy
from .report import file_weekly_report, weekly_report
from .review import Proposal, ProposalKind, ReviewResult, review_agents, run_daily_review

__all__ = [
    "AgentRecord",
    "AgentStats",
    "CreateAgentRequest",
    "Effectiveness",
    "HRActions",
    "HRDataSource",
    "HRPolicy",
    "Lifetime",
    "OverLimitDecision",
    "Proposal",
    "ProposalKind",
    "ReviewResult",
    "Status",
    "TeamKpis",
    "decide_over_limit",
    "rate_agent",
    "review_agents",
    "run_daily_review",
    "team_kpis",
    "weekly_report",
    "file_weekly_report",
]
